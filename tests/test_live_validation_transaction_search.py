"""
Tests for the live-validation Transaction Search baseline, restore, and verify.

Covers ``ownership/transaction_search.py`` (the per-Region capture of the X-Ray
trace segment destination, the GCO CloudWatch Logs resource policy reduced to
a document hash and its X-Ray span-delivery shape, and the two span log groups
reduced to settled presence and creation time; strict validation of the
checkpointed record; the baseline-versus-current comparison with its one
accepted retention) and ``cleanup/transaction_search.py`` (switching a
run-enabled Region back to ``XRay`` with bounded ``PENDING`` waits before the
switch and until it is ``ACTIVE`` again, stopping a Region that does not
settle, deleting only a run-written policy and run-created log groups under exact
identity reads, never touching pre-existing state, and carrying partial
evidence when a Region fails while every other Region is still restored).
Every AWS and clock boundary is faked with in-memory X-Ray and CloudWatch Logs
stand-ins; no network is used.
"""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from scripts.live_release_validation.cleanup import transaction_search as cleanup_ts
from scripts.live_release_validation.ownership import log_groups as ownership_log_groups
from scripts.live_release_validation.ownership import transaction_search as ownership_ts
from tests._live_validation_patching import patch_live_validation_helper

REGION = "us-east-1"
OTHER_REGION = "eu-west-1"
RUN_STARTED = "2026-01-01T00:00:00+00:00"
RUN_STARTED_MS = 1_767_225_600_000
SPANS = "aws/spans"
SIGNALS = "/aws/application-signals/data"
POLICY = ownership_ts.TRANSACTION_SEARCH_POLICY_NAME
#: The enabler's policy document, shaped like the AWS-documented example.
XRAY_DOCUMENT = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "TransactionSearchXRayAccess",
                "Effect": "Allow",
                "Principal": {"Service": "xray.amazonaws.com"},
                "Action": "logs:PutLogEvents",
                "Resource": [
                    "arn:aws:logs:us-east-1:123456789012:log-group:aws/spans:*",
                    "arn:aws:logs:us-east-1:123456789012:log-group:/aws/application-signals/data:*",
                ],
            }
        ],
    }
)


def _policy(
    *, document: str = XRAY_DOCUMENT, updated: Any = RUN_STARTED_MS + 120_000
) -> dict[str, Any]:
    record: dict[str, Any] = {"policyName": POLICY, "policyDocument": document}
    if updated is not None:
        record["lastUpdatedTime"] = updated
    return record


def _group(name: str, created: int = RUN_STARTED_MS + 60_000) -> dict[str, Any]:
    return {
        "logGroupName": name,
        "logGroupArn": f"arn:aws:logs:us-east-1:123456789012:log-group:{name}:*",
        "creationTime": created,
    }


class _FakeXRay:
    """X-Ray's destination API: ``pending_reads`` reads report PENDING first.

    An accepted update switches the destination and reports PENDING for the
    next ``pending_after_update`` reads before it is ACTIVE.
    """

    def __init__(
        self,
        destination: str,
        status: str = "ACTIVE",
        *,
        pending_reads: int = 0,
        pending_after_update: int = 0,
        ignore_updates: bool = False,
    ) -> None:
        self.destination = destination
        self.status = status
        self.pending_reads = pending_reads
        self.pending_after_update = pending_after_update
        self.ignore_updates = ignore_updates
        self.updates: list[str] = []
        self.reads = 0

    def get_trace_segment_destination(self) -> dict[str, str]:
        self.reads += 1
        if self.pending_reads > 0:
            self.pending_reads -= 1
            return {"Destination": self.destination, "Status": "PENDING"}
        return {"Destination": self.destination, "Status": self.status}

    def update_trace_segment_destination(self, *, Destination: str) -> dict[str, str]:
        self.updates.append(Destination)
        if not self.ignore_updates:
            self.destination = Destination
            self.status = "ACTIVE"
            self.pending_reads = self.pending_after_update
        return {"Destination": Destination, "Status": "PENDING"}


class _FakeLogs:
    """CloudWatch Logs resource policies and log groups, one policy per page.

    Each log group has a script of states (a describe record or ``None``);
    every read returns the next state until the last one, which sticks.
    """

    def __init__(
        self,
        *,
        groups: Mapping[str, Sequence[dict[str, Any] | None]] | None = None,
        policies: list[Any] | None = None,
        tags: dict[str, dict[str, str]] | None = None,
    ) -> None:
        # Values are describe records or None (absent); ``Any`` keeps the
        # scripted lists the tests splice together assignable.
        self.scripts: dict[str, list[Any]] = {
            name: list(states) for name, states in (groups or {}).items()
        }
        self.policies: list[Any] = list(policies or [])
        self.tags = dict(tags or {})
        self.deleted_groups: list[str] = []
        self.deleted_policies: list[str] = []
        self.sticky_policy = False
        self.after_delete: dict[str, list[Any]] = {}
        self.delete_error: ClientError | None = None

    def describe_resource_policies(self, **kwargs: Any) -> dict[str, Any]:
        index = int(kwargs.get("nextToken") or 0)
        response: dict[str, Any] = {"resourcePolicies": self.policies[index : index + 1]}
        if index + 1 < len(self.policies):
            response["nextToken"] = str(index + 1)
        return response

    def delete_resource_policy(self, *, policyName: str) -> None:
        self.deleted_policies.append(policyName)
        if not self.sticky_policy:
            self.policies = [
                item
                for item in self.policies
                if not isinstance(item, dict) or item.get("policyName") != policyName
            ]

    def describe_log_groups(
        self, *, logGroupNamePrefix: str, limit: int, nextToken: str | None = None
    ) -> dict[str, Any]:
        assert limit == 50
        script = self.scripts.get(logGroupNamePrefix) or [None]
        state = script.pop(0) if len(script) > 1 else script[0]
        return {"logGroups": [] if state is None else [state]}

    def list_tags_for_resource(self, *, resourceArn: str) -> dict[str, Any]:
        return {"tags": self.tags.get(resourceArn, {})}

    def delete_log_group(self, *, logGroupName: str) -> None:
        self.deleted_groups.append(logGroupName)
        if self.delete_error is not None:
            raise self.delete_error
        self.scripts[logGroupName] = list(self.after_delete.get(logGroupName, [None]))


def _state(
    *,
    destination: str = "XRay",
    status: str = "ACTIVE",
    policy: bool = False,
    policy_hash: str | None = None,
    spans: int | None = None,
    signals: int | None = None,
) -> dict[str, Any]:
    """One Region's checkpointed Transaction Search record."""
    return {
        "observed_at": RUN_STARTED,
        "destination": destination,
        "status": status,
        "resource_policy": {
            "present": policy,
            "document_sha256": (policy_hash or "a" * 64) if policy else None,
            "last_updated_time": None,
            "grants_xray_span_delivery": policy,
        },
        "log_groups": {
            SPANS: {"present": spans is not None, "creation_time": spans},
            SIGNALS: {"present": signals is not None, "creation_time": signals},
        },
    }


def _context(
    clients: dict[tuple[str, str], Any],
    *,
    regions: tuple[str, ...] = (REGION,),
    baseline: dict[str, Any] | None = None,
) -> SimpleNamespace:
    session = MagicMock()
    session.client.side_effect = lambda service, region_name=None: clients[(service, region_name)]
    state: dict[str, Any] = {}
    if baseline is not None:
        state[ownership_ts.TRANSACTION_SEARCH_BASELINE_STATE_KEY] = baseline
    return SimpleNamespace(
        settings=SimpleNamespace(run_id="run-123"),
        checkpoint=SimpleNamespace(state=state, created_at=RUN_STARTED),
        state_lock=threading.RLock(),
        deployment_regions=regions,
        session=session,
        persist=MagicMock(),
    )


def _baseline(**regions: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "policy_name": POLICY,
        "regions": {region.replace("_", "-"): state for region, state in regions.items()},
    }


class _Clock:
    def __init__(self, start: float = 1_000.0, *, step: float = 1.0) -> None:
        self.now = start
        self.step = step
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += max(float(seconds), self.step)


@pytest.fixture(autouse=True)
def restore_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Log-group stability reads poll with a sleep; the restore waits on PENDING."""
    monkeypatch.setattr(ownership_log_groups, "time", SimpleNamespace(sleep=lambda _s: None))
    clock = _Clock()
    monkeypatch.setattr(cleanup_ts, "time", clock)
    return clock


@pytest.fixture
def stacks_absent() -> Any:
    with patch_live_validation_helper(
        "_verify_target_stack_absence",
        return_value={"all_absent": True, "absent": [], "residual": []},
    ) as verify:
        yield verify


# ---------------------------------------------------------------------------
# ownership/transaction_search.py
# ---------------------------------------------------------------------------


class TestDestination:
    def test_reads_destination_and_status(self) -> None:
        assert ownership_ts._read_trace_segment_destination(_FakeXRay("CloudWatchLogs")) == {
            "destination": "CloudWatchLogs",
            "status": "ACTIVE",
        }

    @pytest.mark.parametrize(
        "response",
        [{"Destination": "S3", "Status": "ACTIVE"}, {"Destination": "XRay", "Status": "DONE"}, {}],
    )
    def test_an_unknown_shape_fails_closed(self, response: dict[str, str]) -> None:
        xray = MagicMock()
        xray.get_trace_segment_destination.return_value = response
        with pytest.raises(RuntimeError, match="unexpected trace segment destination"):
            ownership_ts._read_trace_segment_destination(xray)


class TestResourcePolicy:
    def test_the_gco_policy_is_found_across_pages(self) -> None:
        other = {"policyName": "someone-elses-policy", "policyDocument": "{}"}
        logs = _FakeLogs(policies=[other, _policy()])
        assert ownership_ts._gco_resource_policy(logs) == _policy()

    def test_an_absent_policy_reads_as_none(self) -> None:
        assert ownership_ts._gco_resource_policy(_FakeLogs(policies=[])) is None

    def test_a_non_object_record_fails_closed(self) -> None:
        with pytest.raises(RuntimeError, match="non-object resource policy record"):
            ownership_ts._gco_resource_policy(_FakeLogs(policies=["not-a-policy"]))

    def test_a_duplicate_listing_fails_closed(self) -> None:
        with pytest.raises(RuntimeError, match=r"listed resource policy .* twice"):
            ownership_ts._gco_resource_policy(_FakeLogs(policies=[_policy(), _policy()]))

    @pytest.mark.parametrize(
        ("statement", "expected"),
        [
            (
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "xray.amazonaws.com"},
                    "Action": "logs:PutLogEvents",
                },
                True,
            ),
            (
                {
                    "Effect": "Allow",
                    "Principal": {"Service": ["logs.amazonaws.com", "xray.amazonaws.com"]},
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                },
                True,
            ),
            (
                {
                    "Effect": "Deny",
                    "Principal": {"Service": "xray.amazonaws.com"},
                    "Action": "logs:PutLogEvents",
                },
                False,
            ),
            (
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "logs.amazonaws.com"},
                    "Action": "logs:PutLogEvents",
                },
                False,
            ),
            (
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "xray.amazonaws.com"},
                    "Action": "logs:GetLogEvents",
                },
                False,
            ),
            ({"Effect": "Allow", "Principal": "*", "Action": "logs:PutLogEvents"}, False),
        ],
    )
    def test_the_xray_span_delivery_shape(self, statement: dict[str, Any], expected: bool) -> None:
        listed = json.dumps({"Statement": [statement, "not-a-statement"]})
        single = json.dumps({"Statement": statement})
        assert ownership_ts._grants_xray_span_delivery(listed) is expected
        assert ownership_ts._grants_xray_span_delivery(single) is expected

    @pytest.mark.parametrize("document", ["not json", "[]", '{"Statement": 3}', "{}"])
    def test_documents_without_statements_never_grant(self, document: str) -> None:
        assert ownership_ts._grants_xray_span_delivery(document) is False

    def test_the_summary_keeps_a_hash_never_the_document(self) -> None:
        summary = ownership_ts._policy_summary(_policy())
        assert summary["present"] is True
        assert len(summary["document_sha256"]) == 64
        assert summary["last_updated_time"] == RUN_STARTED_MS + 120_000
        assert summary["grants_xray_span_delivery"] is True
        assert "123456789012" not in json.dumps(summary)

    @pytest.mark.parametrize("updated", [None, True, "yesterday"])
    def test_an_unusable_write_time_is_dropped(self, updated: Any) -> None:
        assert ownership_ts._policy_summary(_policy(updated=updated))["last_updated_time"] is None

    def test_an_absent_policy_summary(self) -> None:
        assert ownership_ts._policy_summary(None) == {
            "present": False,
            "document_sha256": None,
            "last_updated_time": None,
            "grants_xray_span_delivery": False,
        }


class TestObservation:
    def test_a_region_observation_is_sanitized_counts_and_hashes(self) -> None:
        logs = _FakeLogs(groups={SPANS: [_group(SPANS)]}, policies=[_policy()])
        ctx = _context({("xray", REGION): _FakeXRay("CloudWatchLogs"), ("logs", REGION): logs})

        state = ownership_ts._observe_transaction_search_state(ctx, REGION)

        assert state["destination"] == "CloudWatchLogs"
        assert state["status"] == "ACTIVE"
        assert state["resource_policy"]["present"] is True
        assert state["log_groups"] == {
            SPANS: {"present": True, "creation_time": RUN_STARTED_MS + 60_000},
            SIGNALS: {"present": False, "creation_time": None},
        }
        assert "arn:" not in json.dumps(state)

    def test_a_log_group_that_will_not_settle_fails_closed(self) -> None:
        # A different generation on every read never settles.
        flapping = [_group(SPANS, RUN_STARTED_MS + index) for index in range(10)]
        logs = _FakeLogs(groups={SPANS: flapping})
        with pytest.raises(RuntimeError, match=f"log group {REGION}:{SPANS} did not settle"):
            ownership_ts._log_group_presence(logs, REGION, SPANS)

    def test_the_baseline_covers_every_deployed_region(self) -> None:
        ctx = _context(
            {
                ("xray", REGION): _FakeXRay("XRay"),
                ("logs", REGION): _FakeLogs(),
                ("xray", OTHER_REGION): _FakeXRay("CloudWatchLogs", "PENDING"),
                ("logs", OTHER_REGION): _FakeLogs(groups={SIGNALS: [_group(SIGNALS, 5)]}),
            },
            regions=(REGION, OTHER_REGION),
        )

        baseline = ownership_ts._capture_transaction_search_baseline(ctx)

        assert baseline["schema_version"] == 1
        assert baseline["policy_name"] == POLICY
        assert set(baseline["regions"]) == {REGION, OTHER_REGION}
        assert baseline["regions"][OTHER_REGION]["status"] == "PENDING"
        assert baseline["regions"][OTHER_REGION]["log_groups"][SIGNALS]["creation_time"] == 5
        # What was captured is exactly what the checkpoint validation accepts.
        ctx.checkpoint.state[ownership_ts.TRANSACTION_SEARCH_BASELINE_STATE_KEY] = baseline
        assert ownership_ts._validated_transaction_search_baseline(ctx) is baseline


class TestBaselineValidation:
    @pytest.mark.parametrize(
        "raw",
        [None, "not-a-record", {"schema_version": 2, "regions": {}}],
    )
    def test_a_missing_or_foreign_record_fails_closed(self, raw: Any) -> None:
        ctx = _context({})
        if raw is not None:
            ctx.checkpoint.state[ownership_ts.TRANSACTION_SEARCH_BASELINE_STATE_KEY] = raw
        with pytest.raises(RuntimeError, match="Checkpoint has no Transaction Search baseline"):
            ownership_ts._validated_transaction_search_baseline(ctx)

    @pytest.mark.parametrize(
        "regions", ["not-a-map", {}, {REGION: _state(), OTHER_REGION: _state()}]
    )
    def test_the_record_must_cover_exactly_the_deployed_regions(self, regions: Any) -> None:
        ctx = _context({}, baseline={"schema_version": 1, "regions": regions})
        with pytest.raises(RuntimeError, match="does not cover exactly the deployed Regions"):
            ownership_ts._validated_transaction_search_baseline(ctx)

    @staticmethod
    def _broken(mutate: Any) -> dict[str, Any]:
        state = _state(policy=True, spans=7)
        mutate(state)
        return state

    @pytest.mark.parametrize(
        ("mutate", "match"),
        [
            (lambda s: s.update(destination="S3"), "destination"),
            (lambda s: s.update(status="DONE"), "destination"),
            (lambda s: s.update(resource_policy="present"), "resource policy"),
            (lambda s: s["resource_policy"].update(present="yes"), "resource policy"),
            (lambda s: s["resource_policy"].update(document_sha256=None), "resource policy"),
            (lambda s: s["resource_policy"].update(present=False), "resource policy"),
            (lambda s: s.update(log_groups=[]), "log groups"),
            (lambda s: s["log_groups"].pop(SIGNALS), "log groups"),
            (lambda s: s["log_groups"].update({SPANS: "present"}), f"log group {SPANS}"),
            (lambda s: s["log_groups"][SPANS].update(present="yes"), f"log group {SPANS}"),
            (lambda s: s["log_groups"][SPANS].update(creation_time=None), f"log group {SPANS}"),
            (lambda s: s["log_groups"][SPANS].update(creation_time=True), f"log group {SPANS}"),
            (lambda s: s["log_groups"][SIGNALS].update(creation_time=9), f"log group {SIGNALS}"),
        ],
    )
    def test_a_malformed_region_record_fails_closed(self, mutate: Any, match: str) -> None:
        ctx = _context({}, baseline=_baseline(us_east_1=self._broken(mutate)))
        with pytest.raises(RuntimeError, match=f"baseline for {REGION} is malformed: {match}"):
            ownership_ts._validated_transaction_search_baseline(ctx)

    def test_a_region_record_that_is_not_an_object(self) -> None:
        ctx = _context({}, baseline=_baseline(us_east_1="XRay"))
        with pytest.raises(RuntimeError, match=f"baseline for {REGION} is malformed$"):
            ownership_ts._validated_transaction_search_baseline(ctx)


class TestDifferences:
    def test_an_identical_state_has_no_difference(self) -> None:
        state = _state(policy=True, spans=7, signals=8)
        assert ownership_ts._transaction_search_differences(state, copy.deepcopy(state)) == ([], [])

    def test_every_kind_of_drift_is_named(self) -> None:
        baseline = _state(spans=7)
        current = _state(destination="CloudWatchLogs", policy=True, signals=8)
        differences, accepted = ownership_ts._transaction_search_differences(baseline, current)
        assert differences == [
            "trace segment destination is CloudWatchLogs, baseline XRay",
            f"resource policy {POLICY} is present, baseline absent",
            f"log group {SPANS} existed at baseline and is gone",
            f"log group {SIGNALS} did not exist at baseline and remains",
        ]
        assert accepted == []

    def test_a_removed_or_rewritten_preexisting_policy_and_a_replaced_group(self) -> None:
        baseline = _state(policy=True, spans=7)
        removed = ownership_ts._transaction_search_differences(baseline, _state(spans=7))
        assert removed[0] == [f"resource policy {POLICY} is absent, baseline present"]
        rewritten = _state(policy=True, policy_hash="b" * 64, spans=9)
        assert ownership_ts._transaction_search_differences(baseline, rewritten)[0] == [
            f"resource policy {POLICY} document changed since baseline",
            f"log group {SPANS} was replaced since baseline",
        ]

    def test_a_new_span_group_under_a_preenabled_destination_is_accepted(self) -> None:
        baseline = _state(destination="CloudWatchLogs")
        current = _state(destination="CloudWatchLogs", spans=7)
        differences, accepted = ownership_ts._transaction_search_differences(baseline, current)
        assert differences == []
        assert accepted == [
            {
                "log_group": SPANS,
                "reason": (
                    "Transaction Search was already enabled at baseline; the span log group "
                    "is shared by every span producer in the account"
                ),
            }
        ]

    def test_verify_compares_every_region_and_prefixes_its_differences(self) -> None:
        ctx = _context(
            {
                ("xray", REGION): _FakeXRay("CloudWatchLogs"),
                ("logs", REGION): _FakeLogs(),
                ("xray", OTHER_REGION): _FakeXRay("XRay"),
                ("logs", OTHER_REGION): _FakeLogs(),
            },
            regions=(REGION, OTHER_REGION),
            baseline=_baseline(us_east_1=_state(), eu_west_1=_state()),
        )

        result = ownership_ts._verify_transaction_search_restored(ctx)

        assert result["differences"] == [
            f"{REGION}: trace segment destination is CloudWatchLogs, baseline XRay"
        ]
        assert list(result["regions"]) == [OTHER_REGION, REGION]
        assert result["regions"][OTHER_REGION]["differences"] == []
        assert result["regions"][REGION]["baseline"] == _state()
        assert result["regions"][REGION]["current"]["destination"] == "CloudWatchLogs"
        assert result["regions"][REGION]["accepted_retained"] == []


# ---------------------------------------------------------------------------
# cleanup/transaction_search.py
# ---------------------------------------------------------------------------


def _enabled_by_the_run() -> tuple[_FakeXRay, _FakeLogs]:
    """A Region the run switched: CloudWatchLogs, the GCO policy, both span groups."""
    return (
        _FakeXRay("CloudWatchLogs"),
        _FakeLogs(
            groups={SPANS: [_group(SPANS)], SIGNALS: [_group(SIGNALS)]},
            policies=[{"policyName": "other", "policyDocument": "{}"}, _policy()],
        ),
    )


class TestRestore:
    def test_a_run_enabled_region_is_put_back_exactly(self, stacks_absent: Any) -> None:
        xray, logs = _enabled_by_the_run()
        ctx = _context(
            {("xray", REGION): xray, ("logs", REGION): logs},
            baseline=_baseline(us_east_1=_state()),
        )

        result = cleanup_ts._restore_transaction_search(ctx)

        assert result["errors"] == []
        region = result["regions"][REGION]
        assert xray.updates == ["XRay"]
        assert logs.deleted_policies == [POLICY]
        assert logs.deleted_groups == [SPANS, SIGNALS]
        assert [action["action"] for action in region["actions"]] == [
            "update-trace-segment-destination",
            "delete-resource-policy",
            "delete-log-group",
            "delete-log-group",
        ]
        assert region["actions"][0] == {
            "action": "update-trace-segment-destination",
            "from": "CloudWatchLogs",
            "to": "XRay",
            "status_before": "ACTIVE",
            "response_status": "PENDING",
        }
        assert region["log_groups"] == {
            SPANS: {"disposition": "deleted", "creation_time": RUN_STARTED_MS + 60_000},
            SIGNALS: {"disposition": "deleted", "creation_time": RUN_STARTED_MS + 60_000},
        }
        assert region["before"]["destination"] == "CloudWatchLogs"
        assert region["after"]["destination"] == "XRay"
        assert region["after"]["resource_policy"]["present"] is False
        assert region["baseline"] == _state()
        # The account now compares clean against its baseline.
        assert ownership_ts._transaction_search_differences(_state(), region["after"]) == ([], [])
        attempts = ctx.checkpoint.state["transaction_search_restore_attempts"]
        assert attempts == [result]
        assert attempts[0] is not result
        ctx.persist.assert_called_once_with()
        stacks_absent.assert_called_once_with(ctx)

    def test_a_pending_switch_is_waited_out_before_it_is_undone(
        self, stacks_absent: Any, restore_clock: _Clock
    ) -> None:
        xray, logs = _enabled_by_the_run()
        # PENDING for the "before" observation and the first settle read;
        # ACTIVE after one poll.
        xray.pending_reads = 2
        ctx = _context(
            {("xray", REGION): xray, ("logs", REGION): logs},
            baseline=_baseline(us_east_1=_state()),
        )

        result = cleanup_ts._restore_transaction_search(ctx)

        assert result["regions"][REGION]["actions"][0]["status_before"] == "ACTIVE"
        assert restore_clock.sleeps == [15.0]
        assert xray.updates == ["XRay"]

    def test_the_switch_back_settles_before_the_policy_or_groups_go(
        self, stacks_absent: Any, restore_clock: _Clock
    ) -> None:
        xray, logs = _enabled_by_the_run()
        xray.pending_after_update = 2
        ctx = _context(
            {("xray", REGION): xray, ("logs", REGION): logs},
            baseline=_baseline(us_east_1=_state()),
        )
        seen_pending: list[int] = []
        original = logs.delete_resource_policy

        def delete_policy(*, policyName: str) -> None:
            seen_pending.append(xray.pending_reads)
            original(policyName=policyName)

        logs.delete_resource_policy = delete_policy  # type: ignore[method-assign]

        result = cleanup_ts._restore_transaction_search(ctx)

        assert result["errors"] == []
        assert restore_clock.sleeps == [15.0, 15.0]
        # X-Ray reported ACTIVE on XRay before the policy was deleted.
        assert seen_pending == [0]
        assert logs.deleted_groups == [SPANS, SIGNALS]
        assert result["regions"][REGION]["after"]["status"] == "ACTIVE"

    def test_a_resumed_region_already_back_on_xray_still_waits_for_active(
        self, stacks_absent: Any, restore_clock: _Clock
    ) -> None:
        _xray, logs = _enabled_by_the_run()
        # An earlier attempt's switch back is still settling: "before" plus two
        # settle reads are PENDING.
        xray = _FakeXRay("XRay", pending_reads=3)
        ctx = _context(
            {("xray", REGION): xray, ("logs", REGION): logs},
            baseline=_baseline(us_east_1=_state()),
        )

        result = cleanup_ts._restore_transaction_search(ctx)

        assert xray.updates == []
        assert restore_clock.sleeps == [15.0, 15.0]
        assert logs.deleted_policies == [POLICY]
        assert logs.deleted_groups == [SPANS, SIGNALS]
        assert result["regions"][REGION]["before"]["status"] == "PENDING"

    def test_a_switch_still_pending_at_the_deadline_is_undone_anyway(
        self, stacks_absent: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _Clock(step=cleanup_ts._DESTINATION_SETTLE_SECONDS)
        monkeypatch.setattr(cleanup_ts, "time", clock)
        xray, logs = _enabled_by_the_run()
        xray.pending_reads = 10
        ctx = _context(
            {("xray", REGION): xray, ("logs", REGION): logs},
            baseline=_baseline(us_east_1=_state()),
        )

        result = cleanup_ts._restore_transaction_search(ctx)

        assert result["regions"][REGION]["actions"][0]["status_before"] == "PENDING"
        assert len(clock.sleeps) == 1
        assert xray.updates == ["XRay"]

    def test_a_region_someone_else_switched_back_needs_no_update(self, stacks_absent: Any) -> None:
        xray, logs = _enabled_by_the_run()
        ctx = _context(
            {("xray", REGION): xray, ("logs", REGION): logs},
            baseline=_baseline(us_east_1=_state()),
        )
        reads = {"count": 0}
        original = xray.get_trace_segment_destination

        def switched_back_while_settling() -> dict[str, str]:
            reads["count"] += 1
            if reads["count"] == 2:  # "before", then the settle read
                xray.destination = "XRay"
            return original()

        xray.get_trace_segment_destination = switched_back_while_settling  # type: ignore[method-assign]

        result = cleanup_ts._restore_transaction_search(ctx)

        assert xray.updates == []
        assert result["regions"][REGION]["actions"][0]["action"] == "delete-resource-policy"

    def test_an_already_restored_region_is_left_alone(self, stacks_absent: Any) -> None:
        xray = _FakeXRay("XRay")
        logs = _FakeLogs()
        ctx = _context(
            {("xray", REGION): xray, ("logs", REGION): logs},
            baseline=_baseline(us_east_1=_state()),
        )

        result = cleanup_ts._restore_transaction_search(ctx)

        region = result["regions"][REGION]
        assert region["actions"] == []
        assert region["log_groups"] == {
            SPANS: {"disposition": "absent"},
            SIGNALS: {"disposition": "absent"},
        }
        assert xray.updates == [] and logs.deleted_policies == [] and logs.deleted_groups == []

    def test_preexisting_state_is_never_touched(self, stacks_absent: Any) -> None:
        xray = _FakeXRay("CloudWatchLogs")
        logs = _FakeLogs(
            groups={SPANS: [_group(SPANS, 5)], SIGNALS: [_group(SIGNALS, 6)]},
            policies=[_policy(updated=4)],
        )
        baseline = _state(destination="CloudWatchLogs", policy=True, spans=5, signals=6)
        ctx = _context(
            {("xray", REGION): xray, ("logs", REGION): logs},
            baseline=_baseline(us_east_1=baseline),
        )

        result = cleanup_ts._restore_transaction_search(ctx)

        region = result["regions"][REGION]
        assert region["actions"] == []
        assert region["log_groups"] == {
            SPANS: {"disposition": "preexisting-untouched"},
            SIGNALS: {"disposition": "preexisting-untouched"},
        }
        assert xray.updates == [] and logs.deleted_policies == [] and logs.deleted_groups == []

    def test_a_new_span_group_under_a_preenabled_destination_is_retained(
        self, stacks_absent: Any
    ) -> None:
        xray = _FakeXRay("CloudWatchLogs")
        logs = _FakeLogs(groups={SPANS: [_group(SPANS)]}, policies=[_policy()])
        ctx = _context(
            {("xray", REGION): xray, ("logs", REGION): logs},
            baseline=_baseline(us_east_1=_state(destination="CloudWatchLogs")),
        )

        result = cleanup_ts._restore_transaction_search(ctx)

        region = result["regions"][REGION]
        assert region["log_groups"][SPANS] == {
            "disposition": "retained-transaction-search-enabled-at-baseline"
        }
        assert logs.deleted_groups == []
        # The GCO policy did not exist at baseline and is the run's to remove.
        assert logs.deleted_policies == [POLICY]
        assert xray.updates == []

    def test_a_shared_span_group_is_retained_even_if_someone_else_switched_to_xray(
        self, stacks_absent: Any
    ) -> None:
        # Baseline CloudWatchLogs; another actor moved the Region to XRay during
        # the run. The run never switches such a Region, and the group still
        # holds every other span producer's spans, so it is not the run's.
        xray = _FakeXRay("XRay")
        logs = _FakeLogs(groups={SPANS: [_group(SPANS)]})
        ctx = _context(
            {("xray", REGION): xray, ("logs", REGION): logs},
            baseline=_baseline(us_east_1=_state(destination="CloudWatchLogs")),
        )

        result = cleanup_ts._restore_transaction_search(ctx)

        region = result["regions"][REGION]
        assert region["log_groups"][SPANS] == {
            "disposition": "retained-transaction-search-enabled-at-baseline"
        }
        assert region["log_groups"][SIGNALS] == {"disposition": "absent"}
        assert logs.deleted_groups == [] and xray.updates == []
        assert xray.reads == 2  # only the before/after observations

    def test_a_vanishing_group_and_a_raced_delete_both_end_absent(self, stacks_absent: Any) -> None:
        xray, logs = _enabled_by_the_run()
        # aws/spans disappears on its own between the stable reads (two for the
        # "before" observation, two before deciding) and the immediate
        # pre-delete read; the signals group is deleted by someone else as the
        # harness asks, which surfaces as ResourceNotFound.
        logs.scripts[SPANS] = [_group(SPANS)] * 4 + [None]
        logs.delete_error = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "DeleteLogGroup"
        )
        ctx = _context(
            {("xray", REGION): xray, ("logs", REGION): logs},
            baseline=_baseline(us_east_1=_state()),
        )
        original = logs.delete_log_group

        def delete_then_gone(*, logGroupName: str) -> None:
            logs.scripts[logGroupName] = [None]
            original(logGroupName=logGroupName)

        logs.delete_log_group = delete_then_gone  # type: ignore[method-assign]

        result = cleanup_ts._restore_transaction_search(ctx)

        region = result["regions"][REGION]
        assert region["log_groups"][SPANS]["disposition"] == "absent"
        assert region["log_groups"][SIGNALS]["disposition"] == "deleted"
        assert logs.deleted_groups == [SIGNALS]
        assert [action.get("log_group") for action in region["actions"][2:]] == [SIGNALS]


class TestRestoreFailsClosed:
    @staticmethod
    def _restore(
        xray: _FakeXRay,
        logs: _FakeLogs,
        *,
        baseline: dict[str, Any] | None = None,
    ) -> tuple[SimpleNamespace, str, dict[str, Any]]:
        ctx = _context(
            {("xray", REGION): xray, ("logs", REGION): logs},
            baseline=_baseline(us_east_1=baseline or _state()),
        )
        with pytest.raises(cleanup_ts.TransactionSearchRestoreError) as info:
            cleanup_ts._restore_transaction_search(ctx)
        return ctx, str(info.value), info.value.details

    def test_requires_every_target_stack_absent(self) -> None:
        ctx = _context({}, baseline=_baseline(us_east_1=_state()))
        with (
            patch_live_validation_helper(
                "_verify_target_stack_absence",
                return_value={"all_absent": False, "absent": [], "residual": [{"name": "x"}]},
            ),
            pytest.raises(RuntimeError, match="requires every exact target stack to be absent"),
        ):
            cleanup_ts._restore_transaction_search(ctx)
        ctx.session.client.assert_not_called()

    def test_requires_the_checkpointed_baseline(self, stacks_absent: Any) -> None:
        with pytest.raises(RuntimeError, match="Checkpoint has no Transaction Search baseline"):
            cleanup_ts._restore_transaction_search(_context({}))
        stacks_absent.assert_not_called()

    def test_requires_a_valid_run_start(self, stacks_absent: Any) -> None:
        ctx = _context({}, baseline=_baseline(us_east_1=_state()))
        ctx.checkpoint.created_at = "not-a-timestamp"
        with pytest.raises(RuntimeError, match="created_at is not a valid timestamp"):
            cleanup_ts._restore_transaction_search(ctx)

    def test_a_switch_that_does_not_take_stops_the_region(self, stacks_absent: Any) -> None:
        xray, logs = _enabled_by_the_run()
        xray.ignore_updates = True

        ctx, message, details = self._restore(xray, logs)

        assert "Transaction Search restoration failed" in message
        assert details["errors"] == [
            {
                "region": REGION,
                "error": (
                    f"RuntimeError: {REGION}: trace segment destination is CloudWatchLogs "
                    "(ACTIVE), not ACTIVE on XRay, after restoring it; the resource policy and "
                    "span log groups stay until it is (resume destroy to retry)"
                ),
            }
        ]
        # The policy and the span groups an account on CloudWatchLogs still needs stay.
        assert logs.deleted_policies == [] and logs.deleted_groups == []
        assert ctx.checkpoint.state["transaction_search_restore_attempts"] == [details]

    def test_a_switch_back_still_pending_at_the_deadline_stops_the_region(
        self, stacks_absent: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _Clock(step=cleanup_ts._DESTINATION_SETTLE_SECONDS)
        monkeypatch.setattr(cleanup_ts, "time", clock)
        xray, logs = _enabled_by_the_run()
        xray.pending_after_update = 10

        _ctx, message, details = self._restore(xray, logs)

        assert f"{REGION}: trace segment destination is XRay (PENDING), not ACTIVE" in message
        assert xray.updates == ["XRay"]
        assert len(clock.sleeps) == 1
        # X-Ray may still deliver spans: the policy and span groups stay for a resume.
        assert logs.deleted_policies == [] and logs.deleted_groups == []
        assert details["regions"][REGION] == {"error": details["errors"][0]["error"]}

    def test_one_failing_region_never_skips_another(self, stacks_absent: Any) -> None:
        broken_xray, broken_logs = _enabled_by_the_run()
        broken_xray.ignore_updates = True
        xray, logs = _enabled_by_the_run()
        ctx = _context(
            {
                ("xray", REGION): broken_xray,
                ("logs", REGION): broken_logs,
                ("xray", OTHER_REGION): xray,
                ("logs", OTHER_REGION): logs,
            },
            regions=(REGION, OTHER_REGION),
            baseline=_baseline(us_east_1=_state(), eu_west_1=_state()),
        )

        with pytest.raises(cleanup_ts.TransactionSearchRestoreError) as info:
            cleanup_ts._restore_transaction_search(ctx)

        details = info.value.details
        assert [error["region"] for error in details["errors"]] == [REGION]
        assert details["regions"][OTHER_REGION]["after"]["destination"] == "XRay"
        assert logs.deleted_groups == [SPANS, SIGNALS]

    @pytest.mark.parametrize(
        ("policy", "match"),
        [
            (_policy(document='{"Statement": []}'), "does not have the X-Ray span delivery shape"),
            (_policy(updated=RUN_STARTED_MS - 1), "was not written during this run"),
            (_policy(updated=None), "was not written during this run"),
        ],
        ids=["foreign-shape", "written-before-the-run", "no-write-time"],
    )
    def test_a_policy_the_run_cannot_prove_it_wrote_is_kept(
        self, stacks_absent: Any, policy: dict[str, Any], match: str
    ) -> None:
        xray, logs = _enabled_by_the_run()
        logs.policies = [policy]

        _ctx, message, _details = self._restore(xray, logs)

        assert match in message
        assert logs.deleted_policies == []
        assert logs.deleted_groups == []

    def test_a_policy_that_survives_deletion_fails(self, stacks_absent: Any) -> None:
        xray, logs = _enabled_by_the_run()
        logs.sticky_policy = True

        _ctx, message, _details = self._restore(xray, logs)

        assert "is still present after deletion" in message
        assert logs.deleted_policies == [POLICY]

    def test_a_group_that_will_not_settle_is_kept(self, stacks_absent: Any) -> None:
        xray, logs = _enabled_by_the_run()
        # Stable for the "before" observation, then a new generation per read.
        logs.scripts[SPANS] = [_group(SPANS)] * 2 + [
            _group(SPANS, RUN_STARTED_MS + 1_000 + index) for index in range(10)
        ]

        _ctx, message, _details = self._restore(xray, logs)

        assert f"{REGION}:{SPANS} did not settle before restoration: replacement" in message
        assert logs.deleted_groups == []

    def test_a_group_older_than_the_run_is_kept(self, stacks_absent: Any) -> None:
        xray, logs = _enabled_by_the_run()
        logs.scripts[SPANS] = [_group(SPANS, RUN_STARTED_MS - 1)]

        _ctx, message, _details = self._restore(xray, logs)

        assert f"{REGION}:{SPANS} predates this validation run" in message
        assert logs.deleted_groups == []

    def test_a_group_with_another_owners_markers_is_kept(self, stacks_absent: Any) -> None:
        xray, logs = _enabled_by_the_run()
        arn = _group(SPANS)["logGroupArn"].removesuffix(":*")
        logs.tags[arn] = {"aws:cloudformation:stack-name": "someone-else"}

        _ctx, message, _details = self._restore(xray, logs)

        assert f"{REGION}:{SPANS} carries another owner's markers" in message
        assert "cloudformation-owned generation" in message
        assert logs.deleted_groups == []

    def test_a_generation_swapped_immediately_before_deletion_is_kept(
        self, stacks_absent: Any
    ) -> None:
        xray, logs = _enabled_by_the_run()
        replacement = _group(SPANS, RUN_STARTED_MS + 99_000)
        # Two reads for "before", two stable reads, then a new generation.
        logs.scripts[SPANS] = [_group(SPANS)] * 4 + [replacement]

        _ctx, message, _details = self._restore(xray, logs)

        assert f"{REGION}:{SPANS} changed immediately before deletion" in message
        assert logs.deleted_groups == []

    def test_a_group_that_reappears_after_deletion_fails(self, stacks_absent: Any) -> None:
        xray, logs = _enabled_by_the_run()
        logs.after_delete[SPANS] = [_group(SPANS)]

        _ctx, message, _details = self._restore(xray, logs)

        assert f"{REGION}:{SPANS} did not stay absent after deletion" in message
        assert logs.deleted_groups == [SPANS]

    def test_an_unexpected_delete_error_is_the_regions_failure(self, stacks_absent: Any) -> None:
        xray, logs = _enabled_by_the_run()
        logs.delete_error = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "DeleteLogGroup"
        )

        _ctx, message, _details = self._restore(xray, logs)

        assert "AccessDeniedException" in message
