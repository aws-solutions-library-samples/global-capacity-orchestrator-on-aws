"""Job verdicts and finish times, from the jobs API records to ``gco jobs list``.

Regression tests for two fixed bugs. ``gco jobs list --all-regions`` printed
STATUS ``unknown`` for every Job: the cross-Region aggregator passes the list
route's records (``ManifestProcessor._job_to_dict``) through, those records
carried no ``computed_status``, and the command printed only that field. A
failed Job's runtime counted up forever: Kubernetes sets ``completionTime``
only on a Job that succeeds, so a failed Job never had an end.

Covered here, offline (the manifest processor's Kubernetes config loader is
patched out and the job manager is a mock):

* ``cli.jobs.job_status``: the API's ``computed_status`` when present, else
  the verdict derived from the terminal conditions and the active pod count;
* ``_parse_timestamp`` and ``_finished_at``: ``completionTime``, else the
  ``Failed`` condition's ``lastTransitionTime``;
* ``parse_job_info`` (``JobManager._parse_job_info`` delegates to it):
  absent or null sections, and the Job's ``uid``;
* ``ManifestProcessor._job_to_dict``, fed real ``kubernetes.client`` models:
  the list record carries ``computed_status`` and each condition's
  ``lastTransitionTime``, as the detail route's ``api_shared._parse_job_to_dict``
  does;
* the ``gco jobs list --all-regions`` table, through the click entry point.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from kubernetes.client import (
    V1Container,
    V1Job,
    V1JobCondition,
    V1JobSpec,
    V1JobStatus,
    V1ObjectMeta,
    V1PodSpec,
    V1PodTemplateSpec,
)

from cli.jobs import JobManager, _finished_at, _parse_timestamp, job_status, parse_job_info
from gco.services.api_shared import _parse_job_to_dict
from gco.services.manifest_processor import ManifestProcessor

_REGION = "us-east-1"
_UID = "00000000-0000-4000-8000-000000000001"
_CREATED = datetime(2026, 11, 24, 9, 59, 55, tzinfo=UTC)
_STARTED = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
_ENDED = _STARTED + timedelta(minutes=5)
#: When the Job controller first decided the outcome: Kubernetes 1.31+ adds
#: ``FailureTarget`` / ``SuccessCriteriaMet`` before the terminal condition.
_DECIDED = _ENDED - timedelta(seconds=30)


def _condition(kind: str, status: str = "True", at: datetime | None = _ENDED) -> V1JobCondition:
    return V1JobCondition(
        type=kind, status=status, last_transition_time=at, reason=f"{kind}Reason", message=kind
    )


def _v1_job(
    *,
    active: int | None = None,
    conditions: list[V1JobCondition] | None = None,
    completion_time: datetime | None = None,
) -> V1Job:
    return V1Job(
        metadata=V1ObjectMeta(
            name="trainer",
            namespace="gco-jobs",
            uid=_UID,
            creation_timestamp=_CREATED,
            labels={"app": "trainer"},
        ),
        spec=V1JobSpec(
            parallelism=1,
            completions=1,
            backoff_limit=0,
            template=V1PodTemplateSpec(
                spec=V1PodSpec(containers=[V1Container(name="trainer", image="busybox:1.38.0")])
            ),
        ),
        status=V1JobStatus(
            active=active,
            start_time=_STARTED,
            completion_time=completion_time,
            conditions=conditions,
        ),
    )


#: Every Job state the routes report: how to build it, the verdict a
#: ``gco jobs`` caller reads, and when the Job finished (None: not yet).
_JOB_STATES: dict[str, tuple[Callable[[], V1Job], str, datetime | None]] = {
    "pending": (lambda: _v1_job(), "pending", None),
    "running": (lambda: _v1_job(active=1), "running", None),
    # The failure is decided (FailureTarget) but Failed is not set yet: not terminal.
    "failing": (
        lambda: _v1_job(active=1, conditions=[_condition("FailureTarget", at=_DECIDED)]),
        "running",
        None,
    ),
    "succeeded": (
        lambda: _v1_job(
            conditions=[_condition("SuccessCriteriaMet", at=_DECIDED), _condition("Complete")],
            completion_time=_ENDED,
        ),
        "succeeded",
        _ENDED,
    ),
    # No completionTime: Kubernetes sets it only for a Job that succeeds.
    "failed": (
        lambda: _v1_job(
            conditions=[_condition("FailureTarget", at=_DECIDED), _condition("Failed")]
        ),
        "failed",
        _ENDED,
    ),
}


@pytest.fixture
def processor() -> ManifestProcessor:
    """A manifest processor with the Kubernetes config loader patched out."""
    with patch("gco.services.manifest_processor.config"):
        return ManifestProcessor(
            cluster_id="test-cluster",
            region=_REGION,
            config_dict={"allowed_namespaces": ["gco-jobs"], "validation_enabled": True},
        )


def _true(kind: str) -> dict[str, str]:
    return {"type": kind, "status": "True"}


class TestJobStatus:
    @pytest.mark.parametrize(
        ("record", "expected"),
        [
            pytest.param(
                {"computed_status": "failed", "status": {"active": 1}}, "failed", id="api-wins"
            ),
            pytest.param(
                {"computed_status": "", "status": {"active": 2}}, "running", id="empty-is-absent"
            ),
            pytest.param(
                {"computed_status": None, "status": {"conditions": [_true("Complete")]}},
                "succeeded",
                id="null-is-absent",
            ),
            pytest.param(
                {"computed_status": 3, "status": {"conditions": [_true("Failed")]}},
                "failed",
                id="non-string-is-absent",
            ),
            pytest.param({}, "pending", id="no-status"),
            pytest.param({"status": None}, "pending", id="null-status"),
            pytest.param({"status": "Running"}, "pending", id="non-mapping-status"),
            pytest.param({"status": {"conditions": None, "active": 1}}, "running", id="active"),
            pytest.param({"status": {"active": 0}}, "pending", id="nothing-active"),
            pytest.param({"status": {"active": "3"}}, "pending", id="non-integer-active"),
            pytest.param(
                {"status": {"conditions": [_true("Failed"), _true("Complete")]}},
                "failed",
                id="first-terminal-condition-wins",
            ),
            pytest.param(
                {"status": {"conditions": [_true("Complete"), _true("Failed")]}},
                "succeeded",
                id="first-terminal-condition-wins-either-way",
            ),
            pytest.param(
                {
                    "status": {
                        "active": 1,
                        "conditions": [
                            {"type": "Failed", "status": "False"},
                            {"type": "Complete", "status": "Unknown"},
                        ],
                    }
                },
                "running",
                id="untrue-conditions-are-skipped",
            ),
            pytest.param(
                {"status": {"conditions": ["Failed", None, _true("Complete")]}},
                "succeeded",
                id="malformed-conditions-are-skipped",
            ),
            pytest.param(
                {"status": {"conditions": [_true("Suspended")]}},
                "pending",
                id="non-terminal-true-condition",
            ),
        ],
    )
    def test_verdict(self, record: dict[str, Any], expected: str) -> None:
        assert job_status(record) == expected


class TestParseTimestamp:
    @pytest.mark.parametrize(
        "value",
        ["2026-11-24T10:00:00Z", "2026-11-24T10:00:00+00:00", "2026-11-24T12:00:00+02:00"],
    )
    def test_z_and_offset_forms_read_as_the_same_aware_instant(self, value: str) -> None:
        parsed = _parse_timestamp(value)
        assert parsed == _STARTED
        assert parsed is not None and parsed.tzinfo is not None

    def test_an_offset_is_kept(self) -> None:
        parsed = _parse_timestamp("2026-11-24T12:00:00+02:00")
        assert parsed is not None
        assert parsed.utcoffset() == timedelta(hours=2)

    @pytest.mark.parametrize("value", [None, "", 0, 1_764_000_000, _STARTED])
    def test_anything_but_a_non_empty_string_is_none(self, value: object) -> None:
        assert _parse_timestamp(value) is None


class TestFinishedAt:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            pytest.param(
                {
                    "completionTime": "2026-11-24T10:05:00Z",
                    "conditions": [
                        {**_true("Failed"), "lastTransitionTime": "2026-11-24T11:00:00Z"}
                    ],
                },
                _ENDED,
                id="completion-time-wins",
            ),
            pytest.param(
                {
                    "completionTime": None,
                    "conditions": [
                        {**_true("FailureTarget"), "lastTransitionTime": "2026-11-24T10:04:30Z"},
                        {
                            "type": "Failed",
                            "status": "False",
                            "lastTransitionTime": "2026-11-24T10:01:00Z",
                        },
                        "Failed",
                        {**_true("Failed"), "lastTransitionTime": "2026-11-24T10:05:00Z"},
                    ],
                },
                _ENDED,
                id="the-true-failed-condition",
            ),
            pytest.param(
                {"conditions": [_true("Failed")]},
                None,
                id="bridge-without-transition-times",
            ),
            pytest.param(
                {
                    "conditions": [
                        {**_true("Complete"), "lastTransitionTime": "2026-11-24T10:05:00Z"}
                    ]
                },
                None,
                id="complete-without-completion-time",
            ),
            pytest.param({"conditions": None}, None, id="null-conditions"),
            pytest.param({}, None, id="nothing"),
        ],
    )
    def test_finish_time(self, status: dict[str, Any], expected: datetime | None) -> None:
        assert _finished_at(status) == expected


class TestParseJobInfo:
    def test_a_failed_jobs_runtime_stops_at_its_failure(self) -> None:
        record = {
            "metadata": {"name": "trainer", "namespace": "gco-jobs", "uid": _UID},
            "status": {
                "failed": 1,
                "startTime": "2026-11-24T10:00:00Z",
                "completionTime": None,
                "conditions": [{**_true("Failed"), "lastTransitionTime": "2026-11-24T10:05:00Z"}],
            },
            "spec": {},
        }
        info = parse_job_info(record, _REGION)
        assert info.status == "failed"
        assert info.is_complete
        assert info.completion_time == _ENDED
        # Without the failure time this would be the seconds since 2026-11-24.
        assert info.duration_seconds == 300

    def test_a_full_record(self) -> None:
        record = {
            "metadata": {
                "name": "trainer",
                "namespace": "gco-jobs",
                "creationTimestamp": "2026-11-24T09:59:55Z",
                "labels": {"app": "trainer"},
                "uid": _UID,
            },
            "spec": {
                "parallelism": 2,
                "completions": 4,
                "template": {"spec": {"containers": [{"name": "t", "image": "busybox:1.38.0"}]}},
            },
            "status": {
                "active": 2,
                "succeeded": 1,
                "failed": 0,
                "startTime": "2026-11-24T10:00:00Z",
                "conditions": [],
            },
            "computed_status": "running",
            "keep_warm": {"idle": [], "idle_seconds": 0, "open": False},
            "scheduling": {"node_instance_type": "g6.xlarge", "scheduled_pods": 2},
        }
        info = parse_job_info(record, _REGION)
        assert (info.name, info.namespace, info.region, info.status) == (
            "trainer",
            "gco-jobs",
            _REGION,
            "running",
        )
        assert (info.created_time, info.start_time, info.completion_time) == (
            _CREATED,
            _STARTED,
            None,
        )
        assert (info.active_pods, info.succeeded_pods, info.failed_pods) == (2, 1, 0)
        assert (info.parallelism, info.completions) == (2, 4)
        assert info.labels == {"app": "trainer"}
        assert info.image_refs == ["busybox:1.38.0"]
        assert info.keep_warm == {"idle": [], "idle_seconds": 0, "open": False}
        assert info.uid == _UID
        assert (info.node_instance_type, info.scheduled_pods) == ("g6.xlarge", 2)

    def test_null_sections_read_as_empty(self) -> None:
        record = {
            "metadata": None,
            "spec": None,
            "status": None,
            "keep_warm": "not a mapping",
        }
        info = parse_job_info(record, _REGION)
        assert (info.name, info.namespace, info.status) == ("", "default", "pending")
        assert (info.created_time, info.start_time, info.completion_time) == (None, None, None)
        assert (info.active_pods, info.succeeded_pods, info.failed_pods) == (0, 0, 0)
        assert (info.parallelism, info.completions) == (1, 1)
        assert (info.labels, info.image_refs, info.keep_warm, info.uid) == ({}, [], None, None)

    def test_null_counts_and_labels_read_as_zero_and_empty(self) -> None:
        record = {
            "metadata": {"name": "trainer", "labels": None},
            "status": {"active": None, "succeeded": None, "failed": None},
        }
        info = parse_job_info(record, _REGION)
        assert (info.active_pods, info.succeeded_pods, info.failed_pods) == (0, 0, 0)
        assert info.labels == {}

    @pytest.mark.parametrize("uid", [None, 42, ["x"]])
    def test_a_uid_that_is_not_a_string_is_none(self, uid: object) -> None:
        assert parse_job_info({"metadata": {"name": "trainer", "uid": uid}}, _REGION).uid is None

    def test_the_job_manager_parses_through_the_same_function(self) -> None:
        manager = JobManager.__new__(JobManager)
        record = {
            "metadata": {"name": "trainer", "uid": _UID},
            "status": {
                "conditions": [{**_true("Failed"), "lastTransitionTime": "2026-11-24T10:05:00Z"}]
            },
        }
        assert manager._parse_job_info(record, _REGION) == parse_job_info(record, _REGION)


class TestListRouteRecord:
    def test_a_failed_job_carries_its_verdict_and_condition_times(
        self, processor: ManifestProcessor
    ) -> None:
        job = _v1_job(
            conditions=[_condition("FailureTarget", at=_DECIDED), _condition("Failed", at=None)]
        )
        assert processor._job_to_dict(job) == {
            "metadata": {
                "name": "trainer",
                "namespace": "gco-jobs",
                "creationTimestamp": "2026-11-24T09:59:55+00:00",
                "labels": {"app": "trainer"},
                "uid": _UID,
            },
            "spec": {"parallelism": 1, "completions": 1, "backoffLimit": 0},
            "status": {
                "active": 0,
                "succeeded": 0,
                "failed": 0,
                "startTime": "2026-11-24T10:00:00+00:00",
                "completionTime": None,
                "conditions": [
                    {
                        "type": "FailureTarget",
                        "status": "True",
                        "reason": "FailureTargetReason",
                        "message": "FailureTarget",
                        "lastTransitionTime": "2026-11-24T10:04:30+00:00",
                    },
                    {
                        "type": "Failed",
                        "status": "True",
                        "reason": "FailedReason",
                        "message": "Failed",
                        "lastTransitionTime": None,
                    },
                ],
            },
            "computed_status": "failed",
        }

    def test_a_finished_job_reads_succeeded_not_the_filters_completed(
        self, processor: ManifestProcessor
    ) -> None:
        job = _JOB_STATES["succeeded"][0]()
        assert processor._job_to_dict(job)["computed_status"] == "succeeded"
        # The status filter keeps its own vocabulary (aws_client maps to it).
        assert processor._get_job_status(job) == "completed"

    @pytest.mark.parametrize("state", list(_JOB_STATES))
    def test_list_and_detail_records_agree_and_the_cli_reads_both_alike(
        self, processor: ManifestProcessor, state: str
    ) -> None:
        build, verdict, finished = _JOB_STATES[state]
        listed = processor._job_to_dict(build())
        detailed = _parse_job_to_dict(build())
        assert listed["computed_status"] == detailed["computed_status"] == verdict
        assert listed["status"]["conditions"] == detailed["status"]["conditions"]
        for record in (listed, detailed):
            info = parse_job_info(record, _REGION)
            assert (info.status, info.completion_time) == (verdict, finished)
        # A bridge that predates computed_status: the CLI derives the same verdict.
        legacy = {key: value for key, value in listed.items() if key != "computed_status"}
        assert job_status(legacy) == verdict


class TestGlobalJobsListTable:
    @staticmethod
    def _invoke(jobs: list[dict[str, Any]]) -> Any:
        from cli.main import cli

        manager = MagicMock()
        manager.list_jobs_global.return_value = {
            "total": len(jobs),
            "regions_queried": 1,
            "regions_successful": 1,
            "region_summaries": [{"region": _REGION, "count": len(jobs), "total": len(jobs)}],
            "jobs": jobs,
            "errors": [],
        }
        with patch("cli.commands.jobs_cmd.get_job_manager", return_value=manager):
            return CliRunner().invoke(cli, ["--output", "table", "jobs", "list", "--all-regions"])

    @pytest.mark.parametrize("bridge", ["current", "without-computed-status"])
    def test_status_column_shows_each_jobs_verdict(
        self, processor: ManifestProcessor, bridge: str
    ) -> None:
        jobs = []
        for state, (build, _verdict, _finished) in _JOB_STATES.items():
            record = processor._job_to_dict(build())
            record["metadata"]["name"] = f"job-{state}"
            record["_source_region"] = _REGION
            if bridge == "without-computed-status":
                del record["computed_status"]
            jobs.append(record)

        result = self._invoke(jobs)

        assert result.exit_code == 0, result.output
        rows = {
            fields[0]: fields
            for fields in (line.split() for line in result.output.splitlines())
            if fields and fields[0].startswith("job-")
        }
        assert rows == {
            f"job-{state}": [f"job-{state}", "gco-jobs", _REGION, verdict]
            for state, (_build, verdict, _finished) in _JOB_STATES.items()
        }
        assert "unknown" not in result.output

    def test_a_long_verdict_is_cut_to_the_column(self) -> None:
        job = {
            "metadata": {"name": "trainer", "namespace": "gco-jobs"},
            "_source_region": _REGION,
            "computed_status": "succeeded-with-warnings",
        }
        result = self._invoke([job])
        assert result.exit_code == 0, result.output
        (row,) = [line.split() for line in result.output.splitlines() if "trainer" in line]
        assert row == ["trainer", "gco-jobs", _REGION, "succeeded-"]
