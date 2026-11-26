"""Unit tests for ``cli.dashboard.frames``: API payloads in, pandas frames out.

Every builder is fed the canned fleet from ``tests/_dashboard_fakes.py`` (the
shapes the GCO API and AWS return) and hand-built edge cases: empty answers,
missing keys, nulls, malformed timestamps and statuses the dashboard has no
colour for. The assertions read the rows, columns and dtypes of the returned
frame. Nothing here touches AWS, the network or Streamlit.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

import pandas as pd
import pytest

from cli.dashboard import frames
from cli.dashboard.data import RegionRead
from cli.jobs import JobInfo, parse_job_info
from tests._dashboard_fakes import (
    NOW,
    REGIONS,
    FakeFleet,
    fleet_status_document,
    job_info,
    metrics_read,
    queue_record,
    sample_endpoint,
    sample_jobs,
    topology,
)

STARTED = NOW - timedelta(minutes=40)
FAILED_AT = NOW - timedelta(minutes=10)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _is_utc_ns(column: pd.Series) -> bool:
    """Whether ``column`` is the ``datetime64[ns, UTC]`` column ``_utc_series`` promises."""
    return str(column.dtype) == "datetime64[ns, UTC]"


def _is_any_utc(column: pd.Series) -> bool:
    """Whether ``column`` holds UTC datetimes at whatever resolution pandas picked."""
    return isinstance(column.dtype, pd.DatetimeTZDtype) and str(column.dtype.tz) == "UTC"


def failed_job_record(**status: Any) -> dict[str, Any]:
    """A Job as the API returns it once it failed: no ``completionTime``, a Failed condition."""
    job_status: dict[str, Any] = {
        "startTime": _iso(STARTED),
        "failed": 1,
        "conditions": [
            {
                "type": "FailureTarget",
                "status": "True",
                "lastTransitionTime": _iso(FAILED_AT - timedelta(seconds=2)),
            },
            {
                "type": "Failed",
                "status": "True",
                "reason": "BackoffLimitExceeded",
                "lastTransitionTime": _iso(FAILED_AT),
            },
        ],
    }
    job_status.update(status)
    return {
        "metadata": {
            "name": "train-oom",
            "namespace": "gco-jobs",
            "creationTimestamp": _iso(STARTED - timedelta(seconds=3)),
            "labels": {"team": "ml"},
            "uid": "uid-train-oom",
        },
        "spec": {
            "completions": 1,
            "template": {"spec": {"containers": [{"name": "main", "image": "busybox:1.38.0"}]}},
        },
        "status": job_status,
        "scheduling": {"node_instance_type": "g5.xlarge", "node_capacity_type": "spot"},
    }


def _job(**fields: Any) -> JobInfo:
    values: dict[str, Any] = {
        "name": "job",
        "namespace": "gco-jobs",
        "region": "us-east-1",
        "status": "running",
        "created_time": NOW - timedelta(minutes=5),
        "start_time": NOW - timedelta(minutes=5),
    }
    values.update(fields)
    return JobInfo(**values)


class TestHelpers:
    def test_as_dict_copies_a_json_object(self) -> None:
        source = {"a": 1}
        copied = frames.as_dict(source)
        assert copied == source
        assert copied is not source

    def test_as_dict_accepts_any_mapping(self) -> None:
        assert frames.as_dict(MappingProxyType({"a": 1})) == {"a": 1}

    @pytest.mark.parametrize("value", [None, [], ["a"], "text", 3])
    def test_as_dict_treats_non_objects_as_empty(self, value: object) -> None:
        assert frames.as_dict(value) == {}

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (["us-east-1", "us-west-2"], ["us-east-1", "us-west-2"]),
            (("eu-west-1",), ["eu-west-1"]),
            ([7, ["odd"]], ["7", "['odd']"]),
            (None, []),
            ("us-east-1", []),
            (3, []),
        ],
        ids=["list", "tuple", "non-strings-by-text", "null", "bare-string", "number"],
    )
    def test_target_regions_reads_only_a_list_of_regions(
        self, value: object, expected: list[str]
    ) -> None:
        """A bare string is not one Region per character, and a number is not a crash."""
        assert frames.target_regions({"target_regions": value}) == expected
        assert frames.target_regions({}) == []

    @pytest.mark.parametrize(
        "value",
        [
            NOW,
            datetime(2026, 11, 25, 18, 0),  # noqa: DTZ001 - a naive value is read as UTC
            "2026-11-25T18:00:00Z",
            "2026-11-25T18:00:00+00:00",
            "2026-11-25T18:00:00",
            "2026-11-25T20:00:00+02:00",
            NOW.timestamp(),
            int(NOW.timestamp()),
        ],
    )
    def test_timestamp_reads_every_api_form_as_an_aware_instant(self, value: object) -> None:
        parsed = frames._timestamp(value)
        assert parsed == NOW
        assert parsed is not None and parsed.tzinfo is not None

    @pytest.mark.parametrize("value", [None, "", "yesterday", "2026-13-45", True, False, {}, []])
    def test_timestamp_is_none_for_anything_else(self, value: object) -> None:
        assert frames._timestamp(value) is None

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(math.nan, id="nan"),
            pytest.param(math.inf, id="infinity"),
            pytest.param(-math.inf, id="minus-infinity"),
            pytest.param(NOW.timestamp() * 1000, id="epoch-milliseconds"),
            pytest.param(int(NOW.timestamp() * 1000), id="epoch-milliseconds-int"),
            pytest.param(-(10**12), id="before-year-1"),
            pytest.param(10**30, id="past-time_t"),
        ],
    )
    def test_timestamp_is_none_for_a_number_no_datetime_can_hold(self, value: float) -> None:
        # NaN is what pandas puts in a row that lacks the field.
        assert frames._timestamp(value) is None

    @pytest.mark.parametrize("error", [ValueError, OverflowError, OSError])
    def test_timestamp_is_none_whatever_the_platform_raises_for_an_epoch(
        self, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
    ) -> None:
        # Which of the three an unrepresentable epoch raises depends on the
        # platform's C library (Windows raises OSError for negative epochs).
        class RejectsEpochs(datetime):
            @classmethod
            def fromtimestamp(cls, timestamp: float, tz: Any = None) -> Any:
                raise error("timestamp out of range")

        monkeypatch.setattr(frames, "datetime", RejectsEpochs)
        assert frames._timestamp(-1.0) is None

    def test_utc_series_normalizes_offsets_and_keeps_gaps(self) -> None:
        series = frames._utc_series([frames._timestamp("2026-11-25T20:00:00+02:00"), None])
        assert _is_utc_ns(series)
        assert series.iloc[0] == NOW
        assert pd.isna(series.iloc[1])

    @pytest.mark.parametrize("values", [[], [None], [None, None], [NOW], [NOW, None]])
    def test_utc_series_is_nanosecond_utc_whatever_it_holds(self, values: list[Any]) -> None:
        # pandas 3 infers a resolution from the values (seconds for an empty
        # or all-missing column, microseconds for datetimes); one fixed unit
        # keeps a later fillna(now) lossless instead of a Pandas4Warning.
        series = frames._utc_series(values)
        assert len(series) == len(values)
        assert _is_utc_ns(series)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (3, 3.0),
            (2.5, 2.5),
            ("4.62", 4.62),
            ("-1e-3", -0.001),
            ("n/a", None),
            ("", None),
            (None, None),
            (True, None),
            ([1], None),
        ],
    )
    def test_number(self, value: object, expected: float | None) -> None:
        assert frames._number(value) == expected


class TestJobsFrame:
    def test_one_row_per_job_newest_first(self) -> None:
        frame = frames.jobs_frame(sample_jobs(), now=NOW)
        assert list(frame.columns) == frames.JOB_COLUMNS
        assert list(frame["name"][:3]) == ["speech-asr-train", "llm-finetune-a", "vision-pretrain"]
        assert len(frame) == 7
        assert frame["created"].is_monotonic_decreasing
        assert list(frame.index) == list(range(7))

    def test_times_are_utc_columns(self) -> None:
        frame = frames.jobs_frame(sample_jobs(), now=NOW)
        assert all(_is_utc_ns(frame[column]) for column in ("created", "started", "finished"))
        assert frame["runtime_s"].dtype == "float64"

    def test_running_job_runtime_counts_up_to_now(self) -> None:
        frame = frames.jobs_frame([job_info("llm-finetune-a", "us-east-1", "running")], now=NOW)
        row = frame.iloc[0]
        assert pd.isna(row["finished"])
        assert row["runtime_s"] == (NOW - row["started"]).total_seconds()

    def test_running_job_runtime_defaults_to_the_wall_clock(self) -> None:
        started = datetime.now(UTC) - timedelta(minutes=5)
        before = datetime.now(UTC)
        frame = frames.jobs_frame([_job(start_time=started)])
        after = datetime.now(UTC)
        runtime = frame["runtime_s"].iloc[0]
        assert (before - started).total_seconds() <= runtime <= (after - started).total_seconds()

    def test_succeeded_job_runtime_ends_at_its_completion(self) -> None:
        frame = frames.jobs_frame([job_info("eval-suite-1", "us-east-1", "succeeded")], now=NOW)
        row = frame.iloc[0]
        assert row["finished"] - row["started"] == timedelta(minutes=10)
        assert row["runtime_s"] == 600.0

    def test_failed_job_ends_at_its_failed_condition_never_now(self) -> None:
        job = parse_job_info(failed_job_record(), "us-east-1")
        for now in (NOW, NOW + timedelta(days=3)):
            row = frames.jobs_frame([job], now=now).iloc[0]
            assert row["status"] == "failed"
            assert row["finished"] == FAILED_AT
            assert row["runtime_s"] == (FAILED_AT - STARTED).total_seconds()
        assert (row["failed"], row["instance_type"], row["capacity_type"]) == (
            1,
            "g5.xlarge",
            "spot",
        )

    @pytest.mark.parametrize("status", ["succeeded", "failed"])
    def test_finished_job_without_an_end_has_an_unknown_runtime(self, status: str) -> None:
        # A bridge that predates reporting the end: the runtime is unknown,
        # not one that grows until someone looks.
        frame = frames.jobs_frame([_job(status=status, completion_time=None)], now=NOW)
        assert pd.isna(frame["finished"].iloc[0])
        assert pd.isna(frame["runtime_s"].iloc[0])

    def test_failed_record_with_no_end_reads_as_unknown_runtime(self) -> None:
        record = failed_job_record(conditions=[{"type": "Failed", "status": "True"}])
        frame = frames.jobs_frame([parse_job_info(record, "us-east-1")], now=NOW)
        assert frame["status"].iloc[0] == "failed"
        assert pd.isna(frame["runtime_s"].iloc[0])

    def test_unstarted_job_has_no_runtime(self) -> None:
        frame = frames.jobs_frame([job_info("speech-asr-train", "eu-west-1", "pending")], now=NOW)
        assert pd.isna(frame["started"].iloc[0])
        assert pd.isna(frame["runtime_s"].iloc[0])

    @pytest.mark.parametrize(
        ("completions", "succeeded", "expected_completions", "progress"),
        [
            (4, 2, 4, 0.5),
            (4, 0, 4, 0.0),
            (4, 6, 4, 1.0),
            (0, 1, 1, 1.0),
            (None, 0, 1, 0.0),
        ],
    )
    def test_progress_is_succeeded_pods_over_completions(
        self, completions: int | None, succeeded: int, expected_completions: int, progress: float
    ) -> None:
        job = _job(completions=completions, succeeded_pods=succeeded)
        row = frames.jobs_frame([job], now=NOW).iloc[0]
        assert (row["completions"], row["succeeded"], row["progress"]) == (
            expected_completions,
            succeeded,
            progress,
        )

    def test_labels_team_and_source(self) -> None:
        frame = frames.jobs_frame(sample_jobs(), now=NOW).set_index("name")
        queued = frame.loc["queued-sweep-lr-1e4-abc"]
        assert queued["source"] == "central queue"
        assert queued["labels"] == ["gco.io/managed-by=central-queue", "team=ml"]
        assert queued["team"] == "ml"
        direct = frame.loc["llm-finetune-a"]
        assert direct["source"] == "direct"
        assert direct["labels"] == ["app=llm", "team=ml"]
        assert direct["image"] == "busybox:1.38.0"
        assert direct["uid"] == "uid-llm-finetune-a"

    def test_missing_optional_fields_read_as_empty(self) -> None:
        job = _job(
            labels={"gco.io/managed-by": "someone-else"},
            image_refs=[],
            uid=None,
            active_pods=None,
            succeeded_pods=None,
            failed_pods=None,
        )
        row = frames.jobs_frame([job], now=NOW).iloc[0]
        assert (row["image"], row["team"], row["uid"], row["source"]) == ("", "", "", "direct")
        assert (row["instance_type"], row["capacity_type"]) == ("", "")
        assert (row["active"], row["succeeded"], row["failed"]) == (0, 0, 0)

    def test_job_without_a_creation_time_sorts_last(self) -> None:
        jobs = [_job(name="undated", created_time=None), _job(name="dated")]
        assert list(frames.jobs_frame(jobs, now=NOW)["name"]) == ["dated", "undated"]

    def test_no_jobs_is_an_empty_frame_with_every_column(self) -> None:
        frame = frames.jobs_frame([], now=NOW)
        assert frame.empty
        assert list(frame.columns) == frames.JOB_COLUMNS
        assert _is_utc_ns(frame["created"]) and _is_utc_ns(frame["finished"])
        assert frame["runtime_s"].dtype == "float64"


@pytest.fixture
def jobs() -> pd.DataFrame:
    return frames.jobs_frame(sample_jobs(), now=NOW)


class TestFilterJobs:
    def test_no_filter_keeps_every_row(self, jobs: pd.DataFrame) -> None:
        assert frames.filter_jobs(jobs).equals(jobs)

    def test_each_filter_narrows_the_rows(self, jobs: pd.DataFrame) -> None:
        assert set(frames.filter_jobs(jobs, regions=["us-west-2"])["name"]) == {
            "vision-pretrain",
            "feature-store-sync",
        }
        assert set(frames.filter_jobs(jobs, statuses=["failed", "pending"])["name"]) == {
            "data-validate",
            "speech-asr-train",
        }
        assert frames.filter_jobs(jobs, namespaces=["other"]).empty
        assert len(frames.filter_jobs(jobs, namespaces=["gco-jobs"])) == len(jobs)

    def test_filters_combine(self, jobs: pd.DataFrame) -> None:
        rows = frames.filter_jobs(jobs, regions=["us-east-1"], statuses=["succeeded"])
        assert sorted(rows["name"]) == ["eval-suite-1", "queued-sweep-lr-1e4-abc"]
        assert list(rows.index) == [0, 1]

    @pytest.mark.parametrize(
        ("text", "names"),
        [
            ("  EVAL ", {"eval-suite-1"}),
            ("lr-1e4", {"queued-sweep-lr-1e4-abc"}),
            ("central-queue", {"queued-sweep-lr-1e4-abc"}),
            ("app=vision", {"vision-pretrain"}),
            ("(", set()),
            ("[a-z]+", set()),
        ],
    )
    def test_text_matches_names_and_labels_literally(
        self, jobs: pd.DataFrame, text: str, names: set[str]
    ) -> None:
        assert set(frames.filter_jobs(jobs, text=text)["name"]) == names

    def test_blank_text_matches_everything(self, jobs: pd.DataFrame) -> None:
        assert len(frames.filter_jobs(jobs, text="   ")) == len(jobs)

    def test_filtering_no_jobs_is_empty(self) -> None:
        rows = frames.filter_jobs(frames.jobs_frame([]), regions=["us-east-1"], text="x")
        assert rows.empty
        assert list(rows.columns) == frames.JOB_COLUMNS


class TestFilterPairs:
    def test_keeps_the_selected_region_status_pairs(self, jobs: pd.DataFrame) -> None:
        picks = [
            {"region": "us-east-1", "status": "succeeded"},
            {"region": "us-west-2", "status": "running"},
        ]
        rows = frames.filter_pairs(jobs, picks)
        assert sorted(rows["name"]) == [
            "eval-suite-1",
            "queued-sweep-lr-1e4-abc",
            "vision-pretrain",
        ]
        assert list(rows.index) == [0, 1, 2]

    @pytest.mark.parametrize("picks", [[], ["us-east-1"], [None, 3]])
    def test_no_usable_selection_returns_the_frame_unchanged(
        self, jobs: pd.DataFrame, picks: list[Any]
    ) -> None:
        assert frames.filter_pairs(jobs, picks) is jobs

    def test_selection_matching_nothing_is_empty(self, jobs: pd.DataFrame) -> None:
        assert frames.filter_pairs(jobs, [{"region": "us-east-1"}]).empty
        assert frames.filter_pairs(frames.jobs_frame([]), [{"region": "a", "status": "b"}]).empty


class TestStatusCounts:
    def test_counts_jobs_per_region_and_status(self, jobs: pd.DataFrame) -> None:
        counts = frames.status_counts(jobs)
        assert counts.to_dict("records") == [
            {"region": "eu-west-1", "status": "pending", "jobs": 1},
            {"region": "us-east-1", "status": "failed", "jobs": 1},
            {"region": "us-east-1", "status": "running", "jobs": 1},
            {"region": "us-east-1", "status": "succeeded", "jobs": 2},
            {"region": "us-west-2", "status": "running", "jobs": 1},
            {"region": "us-west-2", "status": "succeeded", "jobs": 1},
        ]

    def test_no_jobs_has_no_counts(self) -> None:
        counts = frames.status_counts(frames.jobs_frame([]))
        assert counts.empty
        assert list(counts.columns) == ["region", "status", "jobs"]


class TestQueueFrame:
    def test_one_row_per_record(self) -> None:
        frame = frames.queue_frame(FakeFleet().queue_jobs()["jobs"]).set_index("job_id")
        assert sorted(frame.index) == ["q-1", "q-2", "q-3"]
        gated = frame.loc["q-3"]
        assert gated["spot_gate"] == "g5.xlarge <= $0.5/h"
        assert gated["error"] == "image pull failed"
        assert (gated["name"], gated["region"], gated["status"]) == (
            "gated-train",
            "us-west-2",
            "failed",
        )
        assert gated["k8s_job"] == "gated-train-0123"
        queued = frame.loc["q-1"]
        assert (queued["spot_gate"], queued["k8s_job"], queued["claimed_by"]) == ("", "", "")
        assert queued["priority"] == 50
        assert queued["submitted"] == NOW - timedelta(minutes=30)
        assert queued["updated"] == NOW - timedelta(minutes=28)

    def test_newest_submission_first_and_unreadable_times_last(self) -> None:
        records = [
            queue_record(
                "q-old",
                "old",
                "us-east-1",
                "succeeded",
                submitted_at=_iso(NOW - timedelta(hours=2)),
            ),
            queue_record("q-bad", "bad", "us-east-1", "queued", submitted_at="yesterday"),
            queue_record("q-new", "new", "us-west-2", "queued", submitted_at=_iso(NOW)),
        ]
        frame = frames.queue_frame(records)
        assert list(frame["job_id"]) == ["q-new", "q-old", "q-bad"]
        assert _is_utc_ns(frame["submitted"])
        assert pd.isna(frame["submitted"].iloc[2])

    def test_sparse_record_reads_as_blanks(self) -> None:
        row = frames.queue_frame([{"priority": None, "claimed_by": "us-east-1/w/l"}]).iloc[0]
        assert row.to_dict() | {"submitted": None, "updated": None} == {
            "job_id": "",
            "name": "",
            "region": "",
            "namespace": "",
            "status": "",
            "priority": 0,
            "submitted": None,
            "updated": None,
            "claimed_by": "us-east-1/w/l",
            "k8s_job": "",
            "spot_gate": "",
            "error": "",
        }
        assert pd.isna(row["submitted"]) and pd.isna(row["updated"])

    def test_no_records_keeps_the_columns(self) -> None:
        frame = frames.queue_frame([])
        assert frame.empty
        assert "spot_gate" in frame.columns
        assert _is_utc_ns(frame["submitted"]) and _is_utc_ns(frame["updated"])


class TestQueueStatsFrame:
    def test_region_by_status_counts(self) -> None:
        frame = frames.queue_stats_frame(FakeFleet().queue_stats())
        assert sorted(frame.itertuples(index=False, name=None)) == [
            ("eu-west-1", "succeeded", 1),
            ("us-east-1", "queued", 1),
            ("us-west-2", "failed", 1),
        ]

    def test_skips_malformed_regions_and_reads_string_counts(self) -> None:
        stats = {"by_region": {"us-east-1": {"running": "3", "queued": 0}, "us-west-2": None}}
        frame = frames.queue_stats_frame(stats)
        assert frame.to_dict("records") == [
            {"region": "us-east-1", "status": "running", "jobs": 3},
            {"region": "us-east-1", "status": "queued", "jobs": 0},
        ]

    @pytest.mark.parametrize("stats", [{}, {"by_region": None}, {"by_region": {}}])
    def test_nothing_queued(self, stats: dict[str, Any]) -> None:
        frame = frames.queue_stats_frame(stats)
        assert frame.empty
        assert list(frame.columns) == ["region", "status", "jobs"]


class TestStatusHistoryFrame:
    def test_terminal_state_is_a_moment(self) -> None:
        frame = frames.status_history_frame(queue_record("q-2", "dedup", "eu-west-1", "succeeded"))
        assert list(frame["status"]) == ["queued", "claimed", "succeeded"]
        assert list(frame["seconds"]) == [5.0, 115.0, 0.0]
        assert frame["until"].iloc[-1] == frame["timestamp"].iloc[-1]
        assert list(frame["message"])[1] == "Claimed by eu-west-1/worker-1/lease-1"

    @pytest.mark.parametrize("status", ["running", "queued"])
    def test_live_state_stays_open(self, status: str) -> None:
        frame = frames.status_history_frame(queue_record("q-1", "sweep", "us-east-1", status))
        assert frame["status"].iloc[-1] == status
        assert pd.isna(frame["until"].iloc[-1])
        assert pd.isna(frame["seconds"].iloc[-1])

    def test_sorted_oldest_first_skipping_malformed_entries(self) -> None:
        record = {
            "status_history": [
                {"status": "cancelled", "timestamp": _iso(NOW)},
                "not an entry",
                {
                    "status": "queued",
                    "timestamp": _iso(NOW - timedelta(minutes=1)),
                    "message": None,
                },
            ]
        }
        frame = frames.status_history_frame(record)
        assert list(frame["status"]) == ["queued", "cancelled"]
        assert list(frame["message"]) == ["", ""]
        assert list(frame["seconds"]) == [60.0, 0.0]
        assert _is_utc_ns(frame["timestamp"]) and _is_utc_ns(frame["until"])

    @pytest.mark.parametrize("record", [{}, {"status_history": None}, {"status_history": []}])
    def test_no_history(self, record: dict[str, Any]) -> None:
        frame = frames.status_history_frame(record)
        assert frame.empty
        assert list(frame.columns) == ["status", "timestamp", "message", "until", "seconds"]


class TestClaimedBy:
    def test_live_claim_names_the_region(self) -> None:
        record = queue_record(
            "q", "n", "us-west-2", "running", claimed_by="us-west-2/worker-1/lease-9"
        )
        assert frames.claimed_by(record) == "us-west-2"

    def test_released_claim_is_read_from_the_history(self) -> None:
        assert frames.claimed_by(queue_record("q", "n", "eu-west-1", "succeeded")) == "eu-west-1"

    def test_latest_claim_wins(self) -> None:
        record = queue_record("q", "n", "us-east-1", "succeeded")
        record["status_history"].insert(
            1, {"status": "claimed", "message": "Claimed by us-west-2/worker-7/lease-0"}
        )
        assert frames.claimed_by(record) == "us-east-1"

    def test_owner_without_worker_parts(self) -> None:
        assert frames.claimed_by({"claimed_by": "us-east-1"}) == "us-east-1"

    @pytest.mark.parametrize(
        "record",
        [
            queue_record("q", "n", "us-east-1", "queued"),
            {"status_history": None},
            {"status_history": [{"status": "claimed", "message": "claimed by hand"}]},
            {"status_history": [{"status": "running", "message": "Claimed by us-east-1/w/l"}]},
            {"status_history": [{"status": "claimed", "message": None}]},
        ],
    )
    def test_never_claimed(self, record: dict[str, Any]) -> None:
        assert frames.claimed_by(record) is None

    def test_malformed_history_entries_are_skipped(self) -> None:
        record = {
            "claimed_by": None,
            "status_history": [
                "queued",
                None,
                {"status": "claimed", "message": "Claimed by us-west-2/worker-1/lease-1"},
            ],
        }
        assert frames.claimed_by(record) == "us-west-2"


class TestJobDetailFrames:
    def test_pods_frame(self) -> None:
        frame = frames.pods_frame(FakeFleet().job_pods("train", "gco-jobs", "us-east-1"))
        assert frame.to_dict("records") == [
            {
                "pod": "train-x1",
                "phase": "Failed",
                "node": "node-1",
                "instance_type": "m5a.large",
                "capacity_type": "",
                "zone": "",
                "started": NOW - timedelta(minutes=20),
                "restarts": 1,
                "ready": False,
                "containers": ["main", "sidecar"],
            }
        ]
        assert _is_utc_ns(frame["started"])

    def test_pods_frame_falls_back_and_skips_malformed_entries(self) -> None:
        payload = {
            "pods": [
                "not a pod",
                {
                    "metadata": {"name": "train-x2"},
                    "spec": {"nodeName": "node-2"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {"name": "main", "ready": True, "restartCount": None},
                            "not a container",
                        ],
                    },
                    "node": {"labels": {"topology.kubernetes.io/zone": "us-east-1a"}},
                },
                {"metadata": None, "status": None, "node": None},
            ]
        }
        frame = frames.pods_frame(payload)
        running, bare = frame.to_dict("records")
        assert (running["node"], running["zone"], running["restarts"]) == (
            "node-2",
            "us-east-1a",
            0,
        )
        assert (running["ready"], running["containers"]) == (True, ["main"])
        assert (bare["pod"], bare["phase"], bare["node"], bare["ready"]) == ("", "", "", False)
        assert bare["containers"] == [] and pd.isna(bare["started"])

    def test_no_pods(self) -> None:
        frame = frames.pods_frame({"pods": None})
        assert frame.empty
        assert _is_utc_ns(frame["started"])

    def test_events_frame_newest_first(self) -> None:
        frame = frames.events_frame(FakeFleet().job_events("train", "gco-jobs", "us-east-1"))
        assert list(frame["reason"]) == ["BackoffLimitExceeded", "Scheduled"]
        assert list(frame["object"]) == ["Job/train", "Pod/train-x1"]
        assert list(frame["source"]) == ["job-controller", "default-scheduler"]
        assert list(frame["time"]) == [NOW - timedelta(minutes=10), NOW - timedelta(minutes=20)]

    def test_events_frame_reads_event_time_and_tolerates_gaps(self) -> None:
        payload = {
            "events": [
                {"type": "Normal", "reason": "Undated", "count": None},
                {"lastTimestamp": None, "eventTime": _iso(NOW), "reason": "Nominated", "count": 0},
                "not an event",
                {"lastTimestamp": _iso(NOW - timedelta(hours=1)), "reason": "Pulled", "count": 3},
            ]
        }
        frame = frames.events_frame(payload)
        assert list(frame["reason"]) == ["Nominated", "Pulled", "Undated"]
        assert list(frame["count"]) == [1, 3, 1]
        assert list(frame["object"]) == ["/", "/", "/"]
        assert list(frame["source"]) == ["", "", ""]
        assert pd.isna(frame["time"].iloc[-1])

    def test_no_events(self) -> None:
        frame = frames.events_frame({})
        assert frame.empty
        assert _is_utc_ns(frame["time"])

    def test_container_metrics_frame(self) -> None:
        frame = frames.container_metrics_frame(
            FakeFleet().job_metrics("train", "gco-jobs", "us-east-1")
        )
        assert frame.to_dict("records") == [
            {"pod": "train-x1", "container": "main", "cpu_millicores": 5.0, "memory_mib": 12.5}
        ]

    def test_container_metrics_skip_malformed_entries(self) -> None:
        payload = {
            "pods": [
                "not a pod",
                {"pod_name": "p", "containers": ["not a container", {"cpu_millicores": None}]},
                {"pod_name": "q", "containers": None},
            ]
        }
        assert frames.container_metrics_frame(payload).to_dict("records") == [
            {"pod": "p", "container": "", "cpu_millicores": 0.0, "memory_mib": 0.0}
        ]

    def test_no_container_metrics(self) -> None:
        frame = frames.container_metrics_frame({})
        assert frame.empty
        assert list(frame.columns) == ["pod", "container", "cpu_millicores", "memory_mib"]


OK_READ = metrics_read("us-east-1")
UNHEALTHY_READ = RegionRead("us-east-1", data={"status": "unhealthy"})
UNREADABLE = RegionRead("us-east-1", error="timed out")


class TestRegionHealth:
    @pytest.mark.parametrize(
        ("stack", "metrics", "verdict"),
        [
            ("unhealthy", OK_READ, "unhealthy"),
            ("unhealthy", None, "unhealthy"),
            ("healthy", UNHEALTHY_READ, "unhealthy"),
            (None, UNHEALTHY_READ, "unhealthy"),
            ("healthy", OK_READ, "healthy"),
            ("healthy", None, "degraded"),
            ("healthy", UNREADABLE, "degraded"),
            ("in-progress", OK_READ, "degraded"),
            (None, OK_READ, "unknown"),
            ("not-deployed", None, "unknown"),
            ("not-deployed", UNREADABLE, "unknown"),
            # ``gco status`` could not read the stack: nothing is known of it.
            ("unknown", OK_READ, "unknown"),
            ("unknown", None, "unknown"),
            ("unknown", UNREADABLE, "unknown"),
            ("unknown", UNHEALTHY_READ, "unhealthy"),
        ],
    )
    def test_verdict(self, stack: str | None, metrics: RegionRead | None, verdict: str) -> None:
        assert frames._health_of(stack, metrics) == verdict


class TestRegionsFrame:
    def test_one_row_per_workload_region(self, jobs: pd.DataFrame) -> None:
        frame = frames.regions_frame(
            topology(),
            fleet_status_document(),
            [metrics_read(region) for region in REGIONS],
            jobs=jobs,
            endpoints=[sample_endpoint()],
        )
        assert list(frame["region"]) == list(REGIONS)
        rows = frame.set_index("region").to_dict("index")
        assert rows["us-east-1"] == {
            "city": "N. Virginia",
            "name": "US East (N. Virginia)",
            "latitude": 39.04,
            "longitude": -77.49,
            "health": "healthy",
            # The regional stack's status, not the persistent stack beside it.
            "stack_status": "UPDATE_COMPLETE",
            "cpu": 6.0,
            "memory": 40.0,
            "gpu": 50.0,
            "gpu_allocatable": 2,
            "gpu_requested": 1,
            "active_jobs": 2,
            "running": 1,
            "failed": 1,
            "queued": 2,
            "dlq": 0,
            "endpoints": 1,
            "error": "",
        }
        assert (rows["us-west-2"]["running"], rows["us-west-2"]["dlq"]) == (1, 1)
        assert (rows["eu-west-1"]["running"], rows["eu-west-1"]["endpoints"]) == (0, 0)

    def test_regions_without_reads_or_stacks(self) -> None:
        frame = frames.regions_frame(topology(), None, [])
        assert list(frame["health"]) == ["unknown"] * 3
        assert list(frame["stack_status"]) == ["not deployed"] * 3
        assert (
            frame[["cpu", "memory", "gpu", "gpu_allocatable", "gpu_requested"]].isna().all().all()
        )
        assert (
            frame[["active_jobs", "running", "failed", "queued", "dlq", "endpoints"]]
            .eq(0)
            .all()
            .all()
        )

    def test_partial_and_foreign_data(self) -> None:
        fleet = {
            "sections": {
                "stacks": {
                    "data": {
                        "expected": [
                            {
                                "name": "gco-us-east-1",
                                "region": "us-east-1",
                                "status": "UPDATE_IN_PROGRESS",
                                "health": "in-progress",
                            },
                            # Another project's stack in the same Region.
                            {
                                "name": "other-us-west-2",
                                "region": "us-west-2",
                                "status": "CREATE_COMPLETE",
                                "health": "healthy",
                            },
                            "not a stack",
                        ]
                    }
                },
                "queue": {"data": {"by_region": {"us-east-1": None}}},
            }
        }
        frame = frames.regions_frame(
            topology(workload_regions=("us-east-1", "us-west-2", "zz-nowhere-9")),
            fleet,
            [RegionRead("us-east-1", error="timed out"), metrics_read("us-west-2")],
            jobs=frames.jobs_frame([]),
            endpoints=[
                sample_endpoint(),
                sample_endpoint(endpoint_name="retired", desired_state="deleted"),
                sample_endpoint(endpoint_name="draft", target_regions=None),
            ],
        )
        east, west, nowhere = frame.to_dict("records")
        assert (east["health"], east["stack_status"], east["error"]) == (
            "degraded",
            "UPDATE_IN_PROGRESS",
            "timed out",
        )
        assert pd.isna(east["cpu"]) and east["active_jobs"] == 0 and east["queued"] == 0
        assert pd.isna(east["gpu_allocatable"]) and pd.isna(east["gpu_requested"])
        assert (west["gpu_allocatable"], west["gpu_requested"]) == (2, 1)
        assert (west["health"], west["stack_status"], west["cpu"]) == (
            "unknown",
            "not deployed",
            6.0,
        )
        assert (east["endpoints"], west["endpoints"], nowhere["endpoints"]) == (1, 1, 0)
        assert (nowhere["name"], nowhere["city"]) == ("zz-nowhere-9", "zz-nowhere-9")
        assert pd.isna(nowhere["latitude"]) and pd.isna(nowhere["longitude"])

    def test_a_regional_stack_that_could_not_be_read_is_unknown_not_absent(self) -> None:
        fleet = fleet_status_document()
        regional = fleet["sections"]["stacks"]["data"]["expected"][2]
        assert regional["name"] == "gco-us-west-2"
        regional.update(status=None, health="unknown", updated_time=None)
        frame = frames.regions_frame(
            topology(), fleet, [metrics_read(region) for region in REGIONS]
        ).set_index("region")
        # Its health monitor answers, but the stack's state is not known.
        assert (frame.loc["us-west-2", "health"], frame.loc["us-west-2", "stack_status"]) == (
            "unknown",
            "unknown",
        )
        assert frame.loc["us-east-1", "health"] == "healthy"

    @pytest.mark.parametrize(
        ("counts", "expected"),
        [
            ({"gpu_allocatable": 4, "gpu_requested": 3}, (4, 3)),
            ({"gpu_allocatable": 4, "gpu_requested": 0}, (4, 0)),
            ({"gpu_allocatable": 0, "gpu_requested": 0}, (0, 0)),
            ({}, (None, None)),
            ({"gpu_allocatable": None, "gpu_requested": None}, (None, None)),
            ({"gpu_allocatable": 4}, (None, None)),
            ({"gpu_allocatable": True, "gpu_requested": 0}, (None, None)),
            ({"gpu_allocatable": -1, "gpu_requested": 0}, (None, None)),
            ({"gpu_allocatable": "4", "gpu_requested": 1}, (None, None)),
            ({"gpu_allocatable": 4, "gpu_requested": 1.5}, (None, None)),
        ],
        ids=[
            "requested",
            "idle-gpus",
            "no-gpu-nodes",
            "monitor-without-counts",
            "not-measured",
            "one-count",
            "boolean",
            "negative",
            "text",
            "fraction",
        ],
    )
    def test_gpu_counts_are_read_only_as_a_pair_of_whole_numbers(
        self, counts: dict[str, Any], expected: tuple[int | None, int | None]
    ) -> None:
        # Every one of these reads gpu_percent 0; only the counts say why.
        reading = RegionRead(
            "us-east-1", data={"resource_utilization": {"gpu_percent": 0.0, **counts}}
        )
        [row] = frames.regions_frame(
            topology(workload_regions=("us-east-1",)), None, [reading]
        ).to_dict("records")
        read = tuple(
            None if pd.isna(row[key]) else row[key] for key in ("gpu_allocatable", "gpu_requested")
        )
        assert read == expected
        assert row["gpu"] == 0.0

    def test_a_reading_without_a_utilization_object_reads_as_unknown(self) -> None:
        reading = RegionRead("us-east-1", data={"resource_utilization": ["not", "an", "object"]})
        [row] = frames.regions_frame(
            topology(workload_regions=("us-east-1",)), None, [reading]
        ).to_dict("records")
        assert all(pd.isna(row[key]) for key in ("gpu", "gpu_allocatable", "gpu_requested"))

    def test_unhealthy_monitor_wins_over_a_healthy_stack(self) -> None:
        reading = RegionRead("us-east-1", data={"status": "unhealthy", "active_jobs": None})
        frame = frames.regions_frame(
            topology(workload_regions=("us-east-1",)), fleet_status_document(), [reading]
        )
        assert frame["health"].iloc[0] == "unhealthy"
        assert frame["active_jobs"].iloc[0] == 0
        assert pd.isna(frame["cpu"].iloc[0])

    def test_project_name_selects_the_regional_stack(self) -> None:
        fleet = {
            "sections": {
                "stacks": {
                    "data": {
                        "expected": [
                            {"name": "gco-us-east-1", "region": "us-east-1", "status": "X"},
                            {"name": "acme-us-east-1", "region": "us-east-1", "status": "Y"},
                        ]
                    }
                }
            }
        }
        frame = frames.regions_frame(
            topology(project="acme", workload_regions=("us-east-1",)), fleet, []
        )
        assert frame["stack_status"].iloc[0] == "Y"

    def test_no_workload_regions(self) -> None:
        frame = frames.regions_frame(topology(workload_regions=()), fleet_status_document(), [])
        assert frame.empty
        assert "latitude" in frame.columns


class TestStatusDocumentFrames:
    def test_stacks_frame_lists_expected_then_optional(self) -> None:
        frame = frames.stacks_frame(fleet_status_document())
        assert frame["role"].value_counts().to_dict() == {"expected": 5, "optional": 4}
        analytics = frame.set_index("stack").loc["gco-analytics"]
        assert (analytics["status"], analytics["health"], analytics["role"]) == (
            "not deployed",
            "not-deployed",
            "optional",
        )
        assert pd.isna(analytics["updated"])
        assert frame.set_index("stack").loc["gco-us-east-1", "updated"] == NOW - timedelta(hours=3)

    def test_a_stack_that_could_not_be_read_has_an_unknown_status(self) -> None:
        fleet = fleet_status_document()
        stacks = fleet["sections"]["stacks"]["data"]
        stacks["expected"][2].update(status=None, health="unknown", updated_time=None)
        stacks["optional"][0].update(status=None, health="unknown", updated_time=None)
        frame = frames.stacks_frame(fleet).set_index("stack")
        assert frame.loc[["gco-us-west-2", "gco-regional-api-us-east-1"], "status"].tolist() == [
            "unknown",
            "unknown",
        ]
        # Only a stack CloudFormation reports absent reads as not deployed.
        assert frame.loc["gco-analytics", "status"] == "not deployed"

    def test_stacks_frame_skips_malformed_entries(self) -> None:
        fleet = {
            "sections": {
                "stacks": {"data": {"expected": ["x", {"name": "gco-global"}], "optional": None}}
            }
        }
        assert frames.stacks_frame(fleet).to_dict("records")[0] | {"updated": None} == {
            "stack": "gco-global",
            "region": "",
            "status": "not deployed",
            "health": "",
            "updated": None,
            "role": "expected",
        }

    @pytest.mark.parametrize("fleet", [None, {}, {"sections": None}])
    def test_stacks_frame_without_a_document(self, fleet: dict[str, Any] | None) -> None:
        frame = frames.stacks_frame(fleet)
        assert frame.empty
        assert _is_utc_ns(frame["updated"])

    def test_addons_frame_lists_each_regions_records_as_health_verdicts(self) -> None:
        frame = frames.addons_frame(fleet_status_document())
        assert list(frame.columns) == ["region", "addon", "status", "health", "message", "updated"]
        assert frame[["region", "addon", "status", "health"]].to_dict("records")[:2] == [
            {
                "region": "us-east-1",
                "addon": "base-manifests",
                "status": "applied",
                "health": "healthy",
            },
            {"region": "us-east-1", "addon": "keda", "status": "installed", "health": "healthy"},
        ]
        assert len(frame) == 6
        assert frame.loc[0, "updated"] == NOW - timedelta(hours=3)
        assert _is_utc_ns(frame["updated"])

    def test_addons_frame_puts_failures_first_then_teardowns_then_unknown_records(self) -> None:
        def record(name: str, status: str, health: object, **extra: Any) -> dict[str, Any]:
            return {"name": name, "status": status, "health": health, **extra}

        fleet = fleet_status_document()
        fleet["sections"]["addons"]["data"]["by_region"] = {
            "us-east-1": {
                "readable": True,
                "addons": [
                    record("base-manifests", "applied", "healthy"),
                    record("volcano", "deleted", "unknown"),
                    record("kueue", "installing", None),
                ],
            },
            "us-west-2": {
                "readable": True,
                "addons": [
                    record("_teardown", "teardown-active", "in-progress"),
                    record("base-manifests", "failed", "unhealthy", message="04-quotas.yaml"),
                ],
            },
            "eu-west-1": {"readable": False, "addons": []},
        }
        frame = frames.addons_frame(fleet)
        assert frame[["region", "addon", "health"]].values.tolist() == [
            ["us-west-2", "base-manifests", "unhealthy"],
            ["us-west-2", "_teardown", "degraded"],
            ["us-east-1", "volcano", "unknown"],
            # A record without a verdict the dashboard knows is unknown, not healthy.
            ["us-east-1", "kueue", "unknown"],
            ["us-east-1", "base-manifests", "healthy"],
        ]
        assert frame.loc[0, "message"] == "04-quotas.yaml"
        assert frame.loc[1, "message"] == ""
        assert pd.isna(frame.loc[1, "updated"])

    def test_addons_frame_skips_malformed_entries(self) -> None:
        fleet = {
            "sections": {
                "addons": {
                    "data": {
                        "by_region": {
                            "us-east-1": {"addons": ["x", {"name": "keda", "message": None}]},
                            "us-west-2": None,
                            "eu-west-1": {"addons": None},
                        }
                    }
                }
            }
        }
        [row] = frames.addons_frame(fleet).to_dict("records")
        assert row | {"updated": None} == {
            "region": "us-east-1",
            "addon": "keda",
            "status": "",
            "health": "unknown",
            "message": "",
            "updated": None,
        }

    @pytest.mark.parametrize(
        "fleet", [None, {}, {"sections": None}, {"sections": {"addons": {"data": {}}}}]
    )
    def test_addons_frame_without_records(self, fleet: dict[str, Any] | None) -> None:
        frame = frames.addons_frame(fleet)
        assert frame.empty
        assert list(frame.columns) == ["region", "addon", "status", "health", "message", "updated"]
        assert _is_utc_ns(frame["updated"])

    def test_findings_errors_first_then_by_section(self) -> None:
        findings = [
            {"severity": "info", "section": "a", "message": "fyi"},
            {"severity": "warn", "section": "queue", "message": "dlq"},
            {"severity": "error", "section": "stacks", "message": "failed"},
            {"severity": "error", "section": "capacity", "message": "stale"},
        ]
        fleet = fleet_status_document(findings=findings)
        fleet["findings"].append("not a finding")
        frame = frames.findings_frame(fleet)
        assert list(frame.itertuples(index=False, name=None)) == [
            ("error", "capacity", "stale"),
            ("error", "stacks", "failed"),
            ("warn", "queue", "dlq"),
            ("info", "a", "fyi"),
        ]
        assert list(frame.columns) == ["severity", "section", "message"]

    @pytest.mark.parametrize("fleet", [None, fleet_status_document()])
    def test_no_findings(self, fleet: dict[str, Any] | None) -> None:
        frame = frames.findings_frame(fleet)
        assert frame.empty
        assert list(frame.columns) == ["severity", "section", "message"]

    def test_sqs_frame(self) -> None:
        frame = frames.sqs_frame(fleet_status_document())
        assert frame.to_dict("records") == [
            {"region": "us-east-1", "available": 2, "in_flight": 1, "delayed": 0, "dlq": 0},
            {"region": "us-west-2", "available": 0, "in_flight": 0, "delayed": 0, "dlq": 1},
            {"region": "eu-west-1", "available": 0, "in_flight": 0, "delayed": 1, "dlq": 0},
        ]

    def test_sqs_frame_skips_malformed_regions(self) -> None:
        fleet = {
            "sections": {"queue": {"data": {"by_region": {"a": None, "b": {"available": None}}}}}
        }
        assert frames.sqs_frame(fleet).to_dict("records") == [
            {"region": "b", "available": 0, "in_flight": 0, "delayed": 0, "dlq": 0}
        ]

    def test_sqs_frame_without_a_document(self) -> None:
        frame = frames.sqs_frame(None)
        assert frame.empty
        assert list(frame.columns) == ["region", "available", "in_flight", "delayed", "dlq"]

    def test_capacity_frame(self) -> None:
        frame = frames.capacity_frame(fleet_status_document())
        assert frame.to_dict("records") == [
            {
                "region": "us-east-1",
                "gpu": 40.0,
                "gpu_allocatable": None,
                "gpu_requested": None,
                "cpu": 6.5,
                "queue_depth": 2,
                "running_jobs": 2,
                "telemetry": "complete",
                "missing": [],
            },
            {
                "region": "us-west-2",
                "gpu": 0.0,
                "gpu_allocatable": None,
                "gpu_requested": None,
                "cpu": 7.0,
                "queue_depth": 0,
                "running_jobs": 1,
                "telemetry": "partial",
                "missing": ["gpu"],
            },
        ]

    def test_capacity_frame_adds_the_health_monitors_gpu_counts(self) -> None:
        by_region = {
            region: {"gpu_utilization": gpu, "telemetry_status": "complete"}
            for region, gpu in (
                ("us-east-1", 3.5),
                ("us-west-2", 0.0),
                ("eu-west-1", 0.0),
                ("ap-south-1", 0.0),
                ("sa-east-1", 0.0),
            )
        }
        fleet = {"sections": {"capacity": {"data": {"by_region": by_region}}}}
        metrics = [
            metrics_read("us-east-1", gpu=100.0, gpu_counts=(1, 1)),  # allocated, barely busy
            metrics_read("us-west-2", gpu=0.0, gpu_counts=(4, 0)),  # idle GPUs: a real 0%
            metrics_read("eu-west-1", gpu=0.0, gpu_counts=(0, 0)),  # no GPU nodes
            metrics_read("ap-south-1", gpu=0.0, gpu_counts=None),  # a monitor without counts
            RegionRead("sa-east-1", error="timed out"),
        ]
        frame = frames.capacity_frame(fleet, metrics)
        cells = {
            row["region"]: tuple(
                None if pd.isna(row[key]) else row[key]
                for key in ("gpu", "gpu_allocatable", "gpu_requested")
            )
            for row in frame.to_dict("records")
        }
        assert cells == {
            "us-east-1": (3.5, 1, 1),
            "us-west-2": (0.0, 4, 0),
            "eu-west-1": (None, 0, 0),
            "ap-south-1": (0.0, None, None),
            "sa-east-1": (0.0, None, None),
        }

    def test_capacity_frame_tolerates_missing_signals(self) -> None:
        fleet = {
            "sections": {
                "capacity": {
                    "data": {
                        "by_region": {
                            "a": "not an entry",
                            "b": {"gpu_utilization": "12.5", "queue_depth": None},
                        }
                    }
                }
            }
        }
        assert frames.capacity_frame(fleet).to_dict("records") == [
            {
                "region": "b",
                "gpu": 12.5,
                "gpu_allocatable": None,
                "gpu_requested": None,
                "cpu": None,
                "queue_depth": 0,
                "running_jobs": 0,
                "telemetry": "",
                "missing": [],
            }
        ]

    def test_capacity_frame_without_a_document(self) -> None:
        frame = frames.capacity_frame(None)
        assert frame.empty
        assert "missing" in frame.columns


class TestTimeSeriesFrames:
    def test_utilization_frame_sorted_by_metric_region_and_time(self) -> None:
        frame = frames.utilization_frame(FakeFleet().utilization_history(REGIONS))
        assert len(frame) == 3 * 4 * 6
        assert _is_utc_ns(frame["timestamp"])
        first = frame[(frame["metric"] == "active_jobs") & (frame["region"] == "eu-west-1")]
        assert list(first.index) == list(range(6))
        assert first["timestamp"].is_monotonic_increasing
        assert first["timestamp"].iloc[-1] == NOW

    def test_utilization_frame_parses_strings_and_epochs(self) -> None:
        rows = [
            {"region": "a", "metric": "cpu", "timestamp": _iso(NOW), "value": 2.0},
            {"region": "a", "metric": "cpu", "timestamp": NOW.timestamp() - 60, "value": 1.0},
            {"region": "a", "metric": "cpu", "timestamp": "garbage", "value": 3.0},
        ]
        frame = frames.utilization_frame(rows)
        assert list(frame["value"]) == [1.0, 2.0, 3.0]
        assert frame["timestamp"].iloc[0] == NOW - timedelta(minutes=1)
        assert pd.isna(frame["timestamp"].iloc[2])

    def test_utilization_frame_keeps_samples_whose_time_cannot_be_read(self) -> None:
        # pandas fills the row without a timestamp with NaN; epoch milliseconds
        # are tens of thousands of years past what a datetime holds.
        rows = [
            {"region": "a", "metric": "cpu", "timestamp": _iso(NOW), "value": 1.0},
            {"region": "a", "metric": "cpu", "value": 2.0},
            {"region": "a", "metric": "cpu", "timestamp": NOW.timestamp() * 1000, "value": 3.0},
        ]
        frame = frames.utilization_frame(rows)
        assert _is_utc_ns(frame["timestamp"])
        assert (frame["timestamp"].iloc[0], frame["value"].iloc[0]) == (NOW, 1.0)
        assert frame["timestamp"].iloc[1:].isna().all()
        assert sorted(frame["value"].iloc[1:]) == [2.0, 3.0]

    def test_no_utilization(self) -> None:
        frame = frames.utilization_frame([])
        assert frame.empty
        assert _is_utc_ns(frame["timestamp"])

    def test_spot_frame_oldest_first_per_zone(self) -> None:
        frame = frames.spot_frame(FakeFleet().spot_price_history("g5.xlarge", "us-east-1"))
        assert list(frame["availability_zone"]) == ["us-east-1a"] * 3 + ["us-east-1b"] * 3
        assert list(frame["price"][:3]) == pytest.approx([0.98, 0.62, 0.51])
        assert frame["timestamp"].iloc[2] == NOW - timedelta(hours=1)

    def test_spot_frame_keeps_prices_whose_time_cannot_be_read(self) -> None:
        zone = {"region": "us-east-1", "availability_zone": "us-east-1a"}
        rows = [
            {**zone, "timestamp": int(NOW.timestamp() * 1000), "price": 0.62},
            {**zone, "timestamp": NOW.timestamp(), "price": 0.51},
            {**zone, "price": 0.98},
        ]
        frame = frames.spot_frame(rows)
        assert _is_utc_ns(frame["timestamp"])
        assert (frame["timestamp"].iloc[0], frame["price"].iloc[0]) == (NOW, 0.51)
        assert frame["timestamp"].iloc[1:].isna().all()
        assert sorted(frame["price"].iloc[1:]) == [0.62, 0.98]

    def test_no_spot_history(self) -> None:
        frame = frames.spot_frame([])
        assert frame.empty
        assert _is_utc_ns(frame["timestamp"])


class TestCapacityFrames:
    def test_instance_report_prices_and_savings(self) -> None:
        reads = FakeFleet().instance_report("g5.xlarge", REGIONS)
        frame = frames.instance_report_frame(reads).set_index("region")
        assert list(frame["savings"]) == pytest.approx([0.8, 0.7, 0.7])
        assert list(frame["offered"]) == [True, True, False]
        east = frame.loc["us-east-1"]
        assert (east["city"], east["vcpus"], east["gpus"], east["gpu_type"]) == (
            "N. Virginia",
            8,
            1,
            "L4",
        )
        assert (east["on_demand"], east["spot"], east["stability"], east["error"]) == (
            2.5,
            0.5,
            0.9,
            "",
        )

    @pytest.mark.parametrize(
        "data",
        [
            {"on_demand_price": None, "spot_price": 0.5},
            {"on_demand_price": 0.0, "spot_price": 0.5},
            {"on_demand_price": 2.5, "spot_price": None},
            {"on_demand_price": "2.5", "spot_price": 0.5},
        ],
    )
    def test_savings_need_both_prices(self, data: dict[str, Any]) -> None:
        frame = frames.instance_report_frame([RegionRead("us-east-1", data=data)])
        assert frame["savings"].iloc[0] is None

    def test_unreadable_region(self) -> None:
        frame = frames.instance_report_frame([RegionRead("eu-west-1", error="AccessDenied")])
        row = frame.iloc[0]
        assert (row["offered"], row["gpu_type"], row["error"], row["city"]) == (
            None,
            "",
            "AccessDenied",
            "Ireland",
        )

    def test_recommendation_ranks_every_region(self) -> None:
        frame = frames.recommendation_frame(FakeFleet().recommend_region())
        assert frame[["rank", "region", "score", "queue_depth", "recommended"]].to_dict(
            "records"
        ) == [
            {
                "rank": 1,
                "region": "us-west-2",
                "score": 0.12,
                "queue_depth": 0.0,
                "recommended": True,
            },
            {
                "rank": 2,
                "region": "us-east-1",
                "score": 0.4,
                "queue_depth": 2.0,
                "recommended": False,
            },
        ]
        assert frame["spot_placement"].isna().all()

    def test_weighted_recommendation_carries_spot_signals(self) -> None:
        result = {
            "region": "us-east-1",
            "all_regions": [
                "not an entry",
                {
                    "region": "us-east-1",
                    "score": "0.25",
                    "running_jobs": 3,
                    "spot_placement_score": 9,
                    "spot_price_ratio": "0.31",
                    "telemetry_status": "complete",
                },
            ],
        }
        row = frames.recommendation_frame(result).iloc[0]
        assert (row["score"], row["running_jobs"], row["spot_placement"]) == (0.25, 3.0, 9.0)
        assert (row["spot_price_ratio"], row["telemetry"], row["recommended"]) == (
            0.31,
            "complete",
            True,
        )
        assert len(frames.recommendation_frame(result)) == 1

    def test_no_recommendation(self) -> None:
        frame = frames.recommendation_frame({})
        assert frame.empty
        assert list(frame.columns)[:2] == ["rank", "region"]


class TestInferenceFrames:
    def test_endpoints_frame(self) -> None:
        row = frames.endpoints_frame([sample_endpoint()]).iloc[0].to_dict()
        assert row == {
            "endpoint": "qwen-chat",
            "state": "running",
            "regions": ["us-east-1", "us-west-2"],
            "ready": 2,
            "desired": 2,
            "availability": 1.0,
            "model": "Qwen/Qwen2.5-1.5B-Instruct",
            "image": "vllm/vllm-openai:v0.32.0",
            "gpus": 1,
            "replicas": 1,
            "created": NOW - timedelta(hours=3),
        }

    def test_availability_is_ready_over_desired(self) -> None:
        endpoint = sample_endpoint()
        endpoint["region_status"]["us-west-2"]["replicas_ready"] = 0
        endpoint["region_status"]["eu-west-1"] = "not a status"
        row = frames.endpoints_frame([endpoint]).iloc[0]
        assert (row["ready"], row["desired"], row["availability"]) == (1, 2, 0.5)

    def test_endpoint_with_null_fields(self) -> None:
        endpoint = {
            "endpoint_name": "bare",
            "spec": None,
            "region_status": None,
            "target_regions": None,
            "created_at": None,
        }
        row = frames.endpoints_frame([endpoint]).iloc[0]
        assert (row["regions"], row["ready"], row["desired"], row["availability"]) == (
            [],
            0,
            0,
            0.0,
        )
        assert (row["model"], row["image"], row["gpus"], row["replicas"]) == ("", "", 0, 1)
        assert pd.isna(row["created"])

    def test_no_endpoints(self) -> None:
        frame = frames.endpoints_frame([])
        assert frame.empty
        assert _is_utc_ns(frame["created"])

    def test_endpoint_regions_frame(self) -> None:
        endpoint = sample_endpoint(target_regions=["us-east-1", "us-west-2", "eu-west-1"])
        endpoint["region_status"]["us-west-2"]["error"] = "ImagePullBackOff"
        frame = frames.endpoint_regions_frame(endpoint)
        assert list(frame["region"]) == ["us-east-1", "us-west-2", "eu-west-1"]
        assert list(frame["state"]) == ["running", "running", "waiting"]
        assert list(frame["ready"]) == [1, 1, 0]
        assert list(frame["error"]) == ["", "ImagePullBackOff", ""]
        assert frame["last_sync"].iloc[0] == NOW
        assert pd.isna(frame["last_sync"].iloc[2])

    def test_endpoint_regions_without_targets(self) -> None:
        frame = frames.endpoint_regions_frame({"region_status": None})
        assert frame.empty
        assert _is_utc_ns(frame["last_sync"])

    def test_endpoint_regions_name_malformed_targets_as_the_endpoint_list_does(self) -> None:
        # Unhashable entries cannot be dictionary keys; each is looked up by
        # its text, as endpoints_frame shows it.
        endpoint = sample_endpoint(target_regions=["us-east-1", ["us-west-2"], {"r": 1}, 7])
        endpoint["region_status"]["7"] = {"state": "running", "replicas_ready": 1}
        frame = frames.endpoint_regions_frame(endpoint)
        assert list(frame["region"]) == frames.endpoints_frame([endpoint]).iloc[0]["regions"]
        assert list(frame["region"]) == ["us-east-1", "['us-west-2']", "{'r': 1}", "7"]
        assert list(frame["state"]) == ["running", "waiting", "waiting", "running"]
        assert list(frame["ready"]) == [1, 0, 0, 1]

    @pytest.mark.parametrize(
        "region_status",
        [
            pytest.param("synced", id="status-is-a-string"),
            pytest.param(["us-east-1"], id="status-is-a-list"),
            pytest.param({"us-east-1": "running", "us-west-2": None}, id="entries-are-not-objects"),
            pytest.param(
                {"us-east-1": ["running"], "us-west-2": 3}, id="entries-are-lists-or-numbers"
            ),
        ],
    )
    def test_endpoint_regions_read_a_malformed_status_as_waiting(
        self, region_status: object
    ) -> None:
        endpoint = sample_endpoint(target_regions=["us-east-1", "us-west-2"])
        endpoint["region_status"] = region_status
        frame = frames.endpoint_regions_frame(endpoint)
        assert frame.drop(columns="last_sync").to_dict("records") == [
            {"region": "us-east-1", "state": "waiting", "ready": 0, "desired": 0, "error": ""},
            {"region": "us-west-2", "state": "waiting", "ready": 0, "desired": 0, "error": ""},
        ]
        assert frame["last_sync"].isna().all()
        # The endpoints table reads the same payload the same way.
        row = frames.endpoints_frame([endpoint]).iloc[0]
        assert (row["ready"], row["desired"]) == (0, 0)

    def test_health_checks_frame(self) -> None:
        checks = FakeFleet().endpoint_health_all("qwen-chat", REGIONS)
        checks.append({"region": "ap-south-1", "healthy": None, "error": "connect timeout"})
        frame = frames.health_checks_frame(checks)
        assert list(frame["region"]) == ["global", *REGIONS, "ap-south-1"]
        assert list(frame["latency_ms"][:4]) == [250.0, 900.0, 900.0, 900.0]
        failed = frame.iloc[-1]
        assert (failed["healthy"], failed["error"]) == (False, "connect timeout")
        assert pd.isna(failed["latency_ms"]) and pd.isna(failed["http_status"])

    def test_no_health_checks(self) -> None:
        frame = frames.health_checks_frame([])
        assert frame.empty
        assert list(frame.columns) == ["region", "healthy", "http_status", "latency_ms", "error"]


class TestCostFrames:
    def test_cost_frames(self) -> None:
        tables = frames.cost_frames(FakeFleet().costs())
        daily = tables["daily"]
        assert _is_any_utc(daily["date"])
        assert daily["date"].iloc[0] == pd.Timestamp(NOW.date(), tz="UTC")
        assert list(daily["amount"]) == [1.0, 2.0, 3.0, 4.0, 5.0]
        assert list(tables["services"]["amount"]) == [30.0, 12.5]
        assert tables["regions"].to_dict("records") == [
            {"region": "us-east-1", "amount": 30.0},
            {"region": "us-west-2", "amount": 12.5},
        ]

    def test_empty_cost_report(self) -> None:
        tables = frames.cost_frames({"daily": None, "by_service": None, "by_region": None})
        assert all(table.empty for table in tables.values())
        assert _is_any_utc(tables["daily"]["date"])
        assert list(tables["regions"].columns) == ["region", "amount"]

    def test_allocation_frames(self) -> None:
        tables = frames.allocation_frames(FakeFleet().allocation_costs())
        namespaces = tables["namespaces"]
        assert len(namespaces) == 8
        inference = namespaces[namespaces["namespace"] == "gco-inference"]
        assert dict(zip(inference["component"], inference["amount"], strict=True)) == {
            "CPU": 0.1,
            "Memory": 0.02,
            "GPU": 4.5,
            "Storage": 0.0,
        }
        # The Region rows report no storage cost, so no Storage rows appear.
        assert sorted(set(tables["regions"]["component"])) == ["CPU", "GPU", "Memory"]
        assert len(tables["regions"]) == 6
        assert tables["namespace_totals"].to_dict("records") == [
            {"namespace": "gco-inference", "amount": 4.62},
            {"namespace": "gco-jobs", "amount": 0.5},
        ]
        assert list(tables["region_totals"]["amount"]) == [4.59, 0.53]
        trend = tables["trend"]
        assert list(trend["amount"]) == [2.0, 3.1]
        assert trend["period"].iloc[1] == pd.Timestamp(NOW.date(), tz="UTC")

    def test_allocation_frames_tolerate_odd_rows(self) -> None:
        report = {
            "namespaces": [
                "not a row",
                {"namespace": "gco-jobs", "total_cost": "n/a", "cpu_cost": None, "gpu_cost": "1.5"},
            ],
            "regions": None,
            "trend": [
                {"period": "2026-11-25T16:00Z", "total_cost": "0.4"},
                {"period": "not a period", "total_cost": "9"},
                {"period": None},
                "not a row",
            ],
        }
        tables = frames.allocation_frames(report)
        assert tables["namespaces"].to_dict("records") == [
            {"namespace": "gco-jobs", "component": "CPU", "amount": 0.0},
            {"namespace": "gco-jobs", "component": "GPU", "amount": 1.5},
        ]
        assert tables["namespace_totals"].to_dict("records") == [
            {"namespace": "gco-jobs", "amount": 0.0}
        ]
        assert tables["regions"].empty and tables["region_totals"].empty
        assert tables["trend"].to_dict("records") == [
            {"period": pd.Timestamp("2026-11-25T16:00", tz="UTC"), "amount": 0.4}
        ]

    def test_empty_allocation_report(self) -> None:
        tables = frames.allocation_frames({})
        assert sorted(tables) == [
            "namespace_totals",
            "namespaces",
            "region_totals",
            "regions",
            "trend",
        ]
        assert all(table.empty for table in tables.values())
        assert _is_utc_ns(tables["trend"]["period"])

    def test_cost_monitor_frame(self) -> None:
        frame = frames.cost_monitor_frame(FakeFleet().cost_monitors(["us-east-1"]))
        assert frame.iloc[0].to_dict() == {
            "region": "us-east-1",
            "opencost": True,
            "returning_data": True,
            "interval_min": 60,
            "last_window_end": NOW,
            "last_cost": 0.2,
            "namespaces": 2,
            "error": "",
        }

    def test_cost_monitor_errors(self) -> None:
        reads = [
            RegionRead("us-east-1", error="HTTP 503"),
            RegionRead(
                "us-west-2",
                data={
                    "opencost_healthy": False,
                    "last_scheduled_report": "x",
                    "last_error": "boom",
                },
            ),
        ]
        down, failing = frames.cost_monitor_frame(reads).to_dict("records")
        assert (down["opencost"], down["returning_data"], down["error"]) == (None, None, "HTTP 503")
        assert (failing["opencost"], failing["returning_data"], failing["error"]) == (
            False,
            False,
            "boom",
        )
        assert pd.isna(failing["last_window_end"]) and failing["last_cost"] is None
        assert failing["namespaces"] == 0

    def test_no_cost_monitors(self) -> None:
        frame = frames.cost_monitor_frame([])
        assert frame.empty
        assert _is_utc_ns(frame["last_window_end"])


class TestPlatformFrames:
    def test_policy_frame(self) -> None:
        frame = frames.policy_frame(FakeFleet().regional_policy(["us-east-1"]))
        assert frame.iloc[0].to_dict() == {
            "region": "us-east-1",
            "validation": True,
            "namespaces": ["gco-jobs"],
            "kinds": ["Job", "Pod"],
            "max_cpu": "384",
            "max_memory": "4096Gi",
            "max_gpus": 16,
            "container_gpus": "",
            "registries": 2,
            "dockerhub_orgs": ["vllm"],
            "blocked": ["privileged"],
            "error": "",
        }

    def test_policy_frame_reads_the_per_container_gpu_cap_from_the_admission_policy(self) -> None:
        """The cap moved out of the LimitRange, whose GPU entry let every pod onto GPU nodes."""
        cap = {"gco-jobs-container-gpu-limit": {"container_max": {"nvidia.com/gpu": "8"}}}
        reads = [
            RegionRead(
                "us-east-1",
                data={
                    "cluster_enforcement": {
                        "gco-jobs": {"status": "ok", "admission_policies": cap},
                        # Another namespace the policy does not cover reports none.
                        "team-b": {"status": "ok", "admission_policies": {}},
                    }
                },
            ),
            RegionRead(
                "us-west-2",
                data={
                    "cluster_enforcement": {
                        "gco-jobs": {
                            "status": "ok",
                            "admission_policies": {
                                "gco-jobs-container-gpu-limit": {"container_max": {}},
                                "another": {"container_max": {"nvidia.com/gpu": 2}},
                            },
                        },
                        "team-b": {"status": "unavailable", "reason": "403 Forbidden"},
                        "odd": "not a namespace entry",
                    }
                },
            ),
            RegionRead(
                "eu-west-1",
                data={
                    "cluster_enforcement": {
                        "a": {
                            "admission_policies": {"p": {"container_max": {"nvidia.com/gpu": "8"}}}
                        },
                        "b": {
                            "admission_policies": {"p": {"container_max": {"nvidia.com/gpu": "4"}}}
                        },
                    }
                },
            ),
            RegionRead("ap-south-1", data={"cluster_enforcement": None}),
            RegionRead("sa-east-1", error="HTTP 503"),
        ]
        frame = frames.policy_frame(reads)
        assert dict(zip(frame["region"], frame["container_gpus"], strict=True)) == {
            "us-east-1": "8",
            "us-west-2": "2",
            "eu-west-1": "4, 8",
            "ap-south-1": "",
            "sa-east-1": "",
        }

    def test_policy_frame_falls_back_to_a_limitrange_gpu_max_on_an_older_region(self) -> None:
        """A Region not yet redeployed still caps GPUs in its LimitRange's Container max."""
        older = {
            "gco-jobs": {
                "status": "ok",
                "limit_ranges": {
                    "gco-jobs-limits": [
                        {"type": "Pod", "max": {"nvidia.com/gpu": "16"}},
                        {"type": "Container", "max": {"cpu": "192", "nvidia.com/gpu": "8"}},
                        "not a limit",
                    ],
                    "malformed": "not a list",
                },
            }
        }
        both = {
            "gco-jobs": {
                **older["gco-jobs"],
                "admission_policies": {"p": {"container_max": {"nvidia.com/gpu": "4"}}},
            }
        }
        without_gpu = {
            "gco-jobs": {
                "status": "ok",
                "limit_ranges": {"gco-jobs-limits": [{"type": "Container", "max": {"cpu": "2"}}]},
            }
        }
        reads = [
            RegionRead("us-east-1", data={"cluster_enforcement": older}),
            RegionRead("us-west-2", data={"cluster_enforcement": both}),
            RegionRead("eu-west-1", data={"cluster_enforcement": without_gpu}),
        ]
        frame = frames.policy_frame(reads)
        assert dict(zip(frame["region"], frame["container_gpus"], strict=True)) == {
            "us-east-1": "8",
            "us-west-2": "4",
            "eu-west-1": "",
        }

    def test_policy_frame_lists_only_enforced_blocks(self) -> None:
        security = {
            "block_run_as_root": True,
            "block_host_network": True,
            "block_privileged": "yes",
            "block_host_pid": False,
            "allow_list": True,
        }
        read = RegionRead("us-east-1", data={"policy": {"manifest_security_policy": security}})
        row = frames.policy_frame([read]).iloc[0]
        assert row["blocked"] == ["host network", "run as root"]
        assert (row["max_cpu"], row["max_gpus"], row["registries"]) == ("", None, 0)

    def test_unreadable_policy(self) -> None:
        row = frames.policy_frame([RegionRead("us-west-2", error="HTTP 403")]).iloc[0]
        assert (row["validation"], row["namespaces"], row["blocked"], row["error"]) == (
            None,
            [],
            [],
            "HTTP 403",
        )

    def test_services_frame(self) -> None:
        fake = FakeFleet()
        frame = frames.services_frame(fake.global_health(), fake.regional_status(REGIONS))
        assert list(frame["region"]) == sorted(REGIONS)
        east = frame.set_index("region").loc["us-east-1"].to_dict()
        assert east == {
            "health": "healthy",
            "cluster": "gco-us-east-1",
            "processor": True,
            "version": "2.0.0",
            "queue_worker": "running",
            "last_pass": NOW,
            "worker_error": "",
            "templates": 1,
            "webhooks": 0,
            "error": "",
        }

    @pytest.mark.parametrize(
        ("worker", "state"),
        [
            ({"enabled": True, "running": True}, "running"),
            ({"enabled": True, "running": False, "last_error": "throttled"}, "stopped"),
            ({"enabled": False}, "disabled"),
            ({}, ""),
            (None, ""),
            ("running", ""),
        ],
    )
    def test_queue_worker_state(self, worker: Any, state: str) -> None:
        read = RegionRead("us-east-1", data={"central_queue_worker": worker})
        row = frames.services_frame(None, [read]).iloc[0]
        assert row["queue_worker"] == state
        assert row["health"] == "unknown"

    def test_services_frame_merges_health_and_status_regions(self) -> None:
        health = {
            "regions": [
                {"region": "us-east-1", "status": "unhealthy", "cluster_id": None},
                "not a verdict",
            ]
        }
        reads = [
            RegionRead("us-west-2", error="HTTP 502"),
            RegionRead("us-east-1", data={"cluster_id": "gco-us-east-1"}),
        ]
        east, west = frames.services_frame(health, reads).to_dict("records")
        assert (east["region"], east["health"], east["cluster"]) == (
            "us-east-1",
            "unhealthy",
            "gco-us-east-1",
        )
        assert (west["region"], west["health"], west["error"], west["processor"]) == (
            "us-west-2",
            "unknown",
            "HTTP 502",
            False,
        )
        assert pd.isna(west["last_pass"])

    def test_no_services(self) -> None:
        frame = frames.services_frame(None, [])
        assert frame.empty
        assert _is_utc_ns(frame["last_pass"])


class TestRecordsTable:
    def test_largest_list_of_objects_wins(self) -> None:
        table = frames.records_table(FakeFleet().api_get("/api/v1/health"))
        assert table.to_dict("records") == [
            {"region": region, "status": "healthy"} for region in REGIONS
        ]

    def test_nested_list_and_dotted_columns(self) -> None:
        payload = {
            "summary": [{"n": 1}],
            "data": {
                "items": [{"name": "a", "spec": {"gpu": 1}}, {"name": "b", "spec": {"gpu": 2}}]
            },
        }
        table = frames.records_table(payload)
        assert table.to_dict("records") == [
            {"name": "a", "spec.gpu": 1},
            {"name": "b", "spec.gpu": 2},
        ]

    def test_first_of_equal_lists_wins_and_scalars_are_ignored(self) -> None:
        payload = {"first": [{"a": 1}, "x"], "second": [{"b": 2}, 7]}
        assert frames.records_table(payload).to_dict("records") == [{"a": 1}]

    def test_object_without_lists_is_one_row(self) -> None:
        table = frames.records_table({"status": "ok", "worker": {"running": True}, "tags": ["a"]})
        assert table.to_dict("records") == [{"status": "ok", "tags": ["a"], "worker.running": True}]

    @pytest.mark.parametrize("payload", [None, "text", 5, [], ["a", "b"], [[1, 2]]])
    def test_answer_without_objects_is_an_empty_table(self, payload: object) -> None:
        assert frames.records_table(payload).empty
