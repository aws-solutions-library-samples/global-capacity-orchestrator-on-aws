"""
Tests for the live-validation ``tracing`` action and ``checks/tracing.py``.

Covers the run-scoped ``tracing_overrides`` context (canonical JSON, threaded
through ``RunSettings.extra_cdk_context`` into the resume identity), the
effective configuration (cdk.json ``tracing`` defaults merged with the
override, fail-closed on a malformed value), traffic sizing for a sampling
ratio below one, the Transaction Search destination gate (``ACTIVE``
required, ``PENDING`` waited out, an ``XRay`` destination a skip only when the
operator opted out), the authenticated traffic driven per Region, the Logs
Insights queries on ``aws/spans`` (per-service span counts, the
manifest-processor/cost-monitor trace join, transient and fatal query
failures, bounded polling), which services are expected (cost-monitor only
with cost monitoring, inference-proxy only where the run's inference action
sent requests), and the counts-only evidence. Every AWS, API, and clock
boundary is faked.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from scripts.live_release_validation.actions import tracing as action_module
from scripts.live_release_validation.checks import tracing as checks
from scripts.live_release_validation.models import RunSettings

REGION = "us-east-1"
OTHER_REGION = "eu-west-1"
INFERENCE_STARTED = "2026-01-01T00:00:00+00:00"
INFERENCE_STARTED_MS = 1_767_225_600_000
#: The tracing action runs an hour after the inference action started.
WALL_START = 1_767_229_200.0
TRACE_A = "a" * 32
TRACE_B = "b" * 32
TRACE_C = "c" * 32


class _Clock:
    """Deterministic ``time``: sleeping advances both the monotonic and wall clocks."""

    def __init__(self, *, step: float = 1.0) -> None:
        self.now = 1_000.0
        self.wall = WALL_START
        self.step = step
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        advance = max(float(seconds), self.step)
        self.now += advance
        self.wall += advance


def _install_clock(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> _Clock:
    clock = _Clock(**kwargs)
    monkeypatch.setattr(checks, "time", clock)
    return clock


class _FakeXRay:
    def __init__(self, destination: str = "CloudWatchLogs", *statuses: str) -> None:
        self.destination = destination
        self.statuses = list(statuses or ("ACTIVE",))

    def get_trace_segment_destination(self) -> dict[str, str]:
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"Destination": self.destination, "Status": status}


def _rows(*rows: dict[str, str]) -> list[list[dict[str, str]]]:
    return [[{"field": key, "value": value} for key, value in row.items()] for row in rows]


class _FakeLogs:
    """Logs Insights: each StartQuery takes the next plan for its query kind.

    A plan is ``{"statuses": [...], "rows": [...]}`` (GetQueryResults answers
    the statuses in order, then the rows with the last status) or
    ``{"start_error": ClientError}``.
    """

    def __init__(
        self,
        *,
        counts: list[dict[str, Any]] | None = None,
        correlation: list[dict[str, Any]] | None = None,
    ) -> None:
        self.plans: dict[str, list[dict[str, Any]]] = {
            "counts": list(counts or []),
            "correlation": list(correlation or []),
        }
        self.started: list[dict[str, Any]] = []
        self.pending: dict[str, dict[str, Any]] = {}
        self.stopped: list[str] = []
        self.stop_error: ClientError | None = None

    def start_query(self, **kwargs: Any) -> dict[str, str]:
        kind = "correlation" if "by traceId, service" in kwargs["queryString"] else "counts"
        plans = self.plans[kind]
        plan = plans.pop(0) if len(plans) > 1 else plans[0]
        self.started.append({"kind": kind, **kwargs})
        if "start_error" in plan:
            raise plan["start_error"]
        query_id = f"q{len(self.started)}"
        self.pending[query_id] = {"statuses": list(plan.get("statuses", ["Complete"])), **plan}
        return {"queryId": plan.get("query_id", query_id)}

    def get_query_results(self, *, queryId: str) -> dict[str, Any]:
        plan = self.pending[queryId]
        statuses = plan["statuses"]
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        response: dict[str, Any] = {"status": status}
        if status == "Complete":
            response["results"] = plan.get("rows", [])
        return response

    def stop_query(self, *, queryId: str) -> dict[str, bool]:
        self.stopped.append(queryId)
        if self.stop_error is not None:
            raise self.stop_error
        return {"success": True}


def _complete(*rows: dict[str, str]) -> dict[str, Any]:
    return {"statuses": ["Complete"], "rows": _rows(*rows)}


def _all_counts(*services: str, spans: str = "3") -> dict[str, Any]:
    return _complete(*({"service": service, "spans": spans} for service in services))


_JOINED = _complete(
    {"traceId": TRACE_A, "service": "manifest-processor", "spans": "2"},
    {"traceId": TRACE_A, "service": "cost-monitor", "spans": "1"},
    {"traceId": TRACE_B, "service": "cost-monitor", "spans": "1"},
)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "StartQuery")


def _response(status_code: int = 200) -> SimpleNamespace:
    return SimpleNamespace(status_code=status_code, ok=200 <= status_code < 300)


def _context(
    *,
    cdk_context: dict[str, Any] | None = None,
    regions: tuple[str, ...] = (REGION,),
    clients: dict[tuple[str, str], Any] | None = None,
    inference: Any = "passed",
    inference_enabled: bool = True,
    overrides: str = '{"sample_ratio":1.0}',
) -> SimpleNamespace:
    session = MagicMock()
    session.get_partition_for_region.return_value = "aws"
    resolved = dict(clients or {})
    session.client.side_effect = lambda service, region_name=None: resolved[(service, region_name)]
    aws_client = MagicMock()
    aws_client.make_authenticated_request.return_value = _response()
    action_results: dict[str, Any] = {}
    if inference is not None:
        action_results["inference"] = SimpleNamespace(
            status=inference, started_at=INFERENCE_STARTED
        )
    return SimpleNamespace(
        settings=SimpleNamespace(
            tracing_overrides_json=overrides,
            inference_enabled=inference_enabled,
            selected_region=REGION,
        ),
        checkpoint=SimpleNamespace(state={}, action_results=action_results),
        state_lock=threading.RLock(),
        deployment_regions=regions,
        config=SimpleNamespace(project_name="gco-live", global_region=REGION),
        cdk_context={} if cdk_context is None else cdk_context,
        session=session,
        aws_client=aws_client,
        persist=MagicMock(),
    )


class TestOverrides:
    def test_the_release_override_is_canonical_json(self) -> None:
        assert checks.tracing_overrides_json() == '{"sample_ratio":1.0}'
        assert json.loads(checks.tracing_overrides_json()) == checks.RELEASE_TRACING_OVERRIDES

    def test_the_override_rides_every_cdk_invocation_and_the_identity(self, tmp_path: Any) -> None:
        base = {
            "run_id": "run-123",
            "repo_root": tmp_path,
            "report_dir": tmp_path / "report",
            "checkpoint_path": tmp_path / "report" / "checkpoint.json",
            "expected_account": "123456789012",
            "expected_sha": "a" * 40,
            "expected_branch": "chore/test",
            "profile": "configured",
            "requested_actions": ("tracing",),
        }
        plain = RunSettings(**base)
        assert "tracing_overrides" not in plain.extra_cdk_context()
        traced = RunSettings(**base, tracing_overrides_json=checks.tracing_overrides_json())
        assert traced.extra_cdk_context()["tracing_overrides"] == '{"sample_ratio":1.0}'
        assert traced.identity()["extra_cdk_context"] == traced.extra_cdk_context()
        assert traced.identity() != plain.identity()


class TestEffectiveConfig:
    def test_defaults_apply_without_a_block_or_override(self) -> None:
        assert checks.effective_tracing_config(_context(overrides="")) == {
            "enabled": True,
            "sample_ratio": 0.05,
            "enable_transaction_search": True,
        }

    def test_the_run_override_wins_over_cdk_json(self) -> None:
        ctx = _context(
            cdk_context={
                "tracing": {
                    "enabled": True,
                    "sample_ratio": 0.2,
                    "enable_transaction_search": False,
                }
            }
        )
        assert checks.effective_tracing_config(ctx) == {
            "enabled": True,
            "sample_ratio": 1.0,
            "enable_transaction_search": False,
        }

    def test_an_integer_ratio_is_accepted(self) -> None:
        ctx = _context(cdk_context={"tracing": {"sample_ratio": 1}}, overrides="")
        assert checks.effective_tracing_config(ctx)["sample_ratio"] == 1.0

    @pytest.mark.parametrize(
        ("cdk_context", "overrides", "match"),
        [
            ({"tracing": "on"}, "", "context.tracing must be an object"),
            ({}, "{not json", "tracing_overrides is not valid JSON"),
            ({}, "[1]", "tracing_overrides must be a JSON object"),
            ({"tracing": {"enabled": "yes"}}, "", "must be booleans"),
            ({"tracing": {"enable_transaction_search": 1}}, "", "must be booleans"),
            ({}, '{"sample_ratio": true}', "sample_ratio must be a number"),
            ({}, '{"sample_ratio": 1.5}', "sample_ratio must be a number"),
            ({}, '{"sample_ratio": "all"}', "sample_ratio must be a number"),
        ],
    )
    def test_malformed_configuration_fails_closed(
        self, cdk_context: dict[str, Any], overrides: str, match: str
    ) -> None:
        ctx = _context(cdk_context=cdk_context, overrides=overrides)
        with pytest.raises(checks.TracingValidationError, match=match):
            checks.effective_tracing_config(ctx)

    def test_sibling_settings_without_the_field_use_cdk_json_alone(self) -> None:
        ctx = _context()
        ctx.settings = SimpleNamespace()
        assert checks.effective_tracing_config(ctx)["sample_ratio"] == 0.05


class TestTrafficSizing:
    @pytest.mark.parametrize(
        ("ratio", "rounds"),
        [(1.0, 3), (0.9, 3), (0.5, 10), (0.05, 135), (0.001, 200)],
    )
    def test_rounds_keep_every_request_kind_sampled(self, ratio: float, rounds: int) -> None:
        assert checks._traffic_rounds(ratio) == rounds


class TestExpectedServices:
    def test_all_four_services_where_the_inference_action_ran(self) -> None:
        expected, skipped, started = checks._expected_services(_context(), REGION)
        assert expected == list(checks.TRACED_SERVICES)
        assert skipped == {}
        assert started == INFERENCE_STARTED_MS

    def test_cost_monitoring_off_skips_the_cost_monitor(self) -> None:
        ctx = _context(cdk_context={"cluster_observability": {"enabled": False}})
        expected, skipped, _started = checks._expected_services(ctx, REGION)
        assert "cost-monitor" not in expected
        assert "cost monitoring is disabled" in skipped["cost-monitor"]

    @pytest.mark.parametrize(
        ("changes", "region", "reason"),
        [
            ({"inference_enabled": False}, REGION, "not in this run's scope"),
            ({}, OTHER_REGION, f"went through the proxy in {REGION}"),
            ({"inference": None}, REGION, "has not passed in this run"),
            ({"inference": "failed"}, REGION, "has not passed in this run"),
        ],
    )
    def test_the_inference_proxy_needs_this_runs_inference_traffic(
        self, changes: dict[str, Any], region: str, reason: str
    ) -> None:
        expected, skipped, started = checks._expected_services(_context(**changes), region)
        assert "inference-proxy" not in expected
        assert reason in skipped["inference-proxy"]
        assert started is None

    def test_a_malformed_inference_timestamp_fails_closed(self) -> None:
        ctx = _context()
        ctx.checkpoint.action_results["inference"].started_at = "yesterday"
        with pytest.raises(checks.TracingValidationError, match="Invalid action timestamp"):
            checks._expected_services(ctx, REGION)


class TestTransactionSearchGate:
    @staticmethod
    def _config(transaction_search: bool = True) -> dict[str, Any]:
        return {
            "enabled": True,
            "sample_ratio": 1.0,
            "enable_transaction_search": transaction_search,
        }

    def test_pending_is_waited_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _install_clock(monkeypatch)
        ctx = _context(clients={("xray", REGION): _FakeXRay("CloudWatchLogs", "PENDING", "ACTIVE")})
        state, skip = checks._wait_for_transaction_search(ctx, REGION, self._config())
        assert state == {"destination": "CloudWatchLogs", "status": "ACTIVE"}
        assert skip is None
        assert clock.sleeps == [30.0]

    def test_an_opted_out_account_on_xray_is_a_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_clock(monkeypatch)
        ctx = _context(clients={("xray", REGION): _FakeXRay("XRay")})
        state, skip = checks._wait_for_transaction_search(ctx, REGION, self._config(False))
        assert state["destination"] == "XRay"
        assert skip is not None and "enable_transaction_search is false" in skip

    def test_xray_although_the_enabler_should_have_run_fails_at_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _install_clock(monkeypatch)
        ctx = _context(clients={("xray", REGION): _FakeXRay("XRay")})
        with pytest.raises(checks.TracingValidationError, match="enabler did not switch it"):
            checks._wait_for_transaction_search(ctx, REGION, self._config())
        assert clock.sleeps == []

    def test_pending_past_the_deadline_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_clock(monkeypatch, step=checks._DESTINATION_WAIT_SECONDS)
        ctx = _context(clients={("xray", REGION): _FakeXRay("CloudWatchLogs", "PENDING")})
        with pytest.raises(checks.TracingValidationError, match="stayed PENDING for 900s"):
            checks._wait_for_transaction_search(ctx, REGION, self._config())


class TestTraffic:
    def test_every_traced_service_is_called_through_the_region_transport(self) -> None:
        ctx = _context()
        traffic = checks._drive_traffic(ctx, REGION, rounds=2, cost=True)
        assert traffic == {
            "/api/v1/metrics": {"requests": 2, "status_codes": {"200": 2}},
            "/api/v1/policy": {"requests": 2, "status_codes": {"200": 2}},
            "/api/v1/cost/status": {"requests": 2, "status_codes": {"200": 2}},
        }
        calls = ctx.aws_client.make_authenticated_request.call_args_list
        assert [call.kwargs["path"] for call in calls] == [
            "/api/v1/metrics",
            "/api/v1/policy",
            "/api/v1/cost/status",
        ] * 2
        assert {call.kwargs["method"] for call in calls} == {"GET"}
        # A single-Region run without the regional API rides the global endpoint.
        assert {call.kwargs["target_region"] for call in calls} == {None}

    def test_without_cost_monitoring_the_cost_route_is_not_called(self) -> None:
        traffic = checks._drive_traffic(_context(), REGION, rounds=1, cost=False)
        assert set(traffic) == {"/api/v1/metrics", "/api/v1/policy"}

    def test_a_failing_route_fails_the_region(self) -> None:
        ctx = _context()
        ctx.aws_client.make_authenticated_request.side_effect = [_response(), _response(503)]
        with pytest.raises(
            checks.TracingValidationError, match="GET /api/v1/policy answered HTTP 503"
        ):
            checks._drive_traffic(ctx, REGION, rounds=1, cost=True)


class TestQueries:
    def test_the_queries_filter_on_the_gco_resource_attributes(self) -> None:
        counts = checks._span_count_query(["health-monitor", "cost-monitor"])
        assert "fields `resource.attributes.service.name` as service" in counts
        assert '`resource.attributes.service.namespace` = "gco"' in counts
        assert '`resource.attributes.service.name` in ["health-monitor", "cost-monitor"]' in counts
        assert counts.endswith("| stats count(*) as spans by service")
        correlation = checks._correlation_query()
        assert '["cost-monitor", "manifest-processor"]' in correlation
        assert "| stats count(*) as spans by traceId, service" in correlation
        assert correlation.endswith("| limit 10000")

    def test_a_completed_query_returns_its_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _install_clock(monkeypatch)
        plan = {
            "statuses": ["Scheduled", "Running", "Complete"],
            "rows": [
                [{"field": "service", "value": "health-monitor"}, "not-a-field"],
                "not-a-row",
                [{"field": "spans"}],
            ],
        }
        logs = _FakeLogs(counts=[plan])
        rows = checks._run_query(logs, "fields x", start_s=10, end_s=20)
        assert rows == [{"service": "health-monitor"}, {"spans": ""}]
        assert logs.started[0]["logGroupName"] == "aws/spans"
        assert (logs.started[0]["startTime"], logs.started[0]["endTime"]) == (10, 20)
        assert logs.started[0]["limit"] == 10_000
        assert clock.sleeps == [2.0, 2.0]

    @pytest.mark.parametrize("status", ["Failed", "Cancelled", "Timeout", "Unknown"])
    def test_a_terminal_query_failure_is_transient(
        self, monkeypatch: pytest.MonkeyPatch, status: str
    ) -> None:
        _install_clock(monkeypatch)
        logs = _FakeLogs(counts=[{"statuses": [status]}])
        with pytest.raises(checks._TransientQueryError, match=f"query ended {status}"):
            checks._run_query(logs, "fields x", start_s=1, end_s=2)

    def test_a_query_that_never_completes_is_stopped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_clock(monkeypatch, step=checks._QUERY_WAIT_SECONDS)
        logs = _FakeLogs(counts=[{"statuses": ["Running"]}])
        logs.stop_error = _client_error("InvalidParameterException")
        with pytest.raises(checks._TransientQueryError, match="did not complete within 120s"):
            checks._run_query(logs, "fields x", start_s=1, end_s=2)
        assert logs.stopped == ["q1"]

    @pytest.mark.parametrize(
        "code",
        [
            "ResourceNotFoundException",
            "LimitExceededException",
            "ServiceUnavailableException",
            "ThrottlingException",
        ],
    )
    def test_transient_start_errors(self, monkeypatch: pytest.MonkeyPatch, code: str) -> None:
        _install_clock(monkeypatch)
        logs = _FakeLogs(counts=[{"start_error": _client_error(code)}])
        with pytest.raises(checks._TransientQueryError, match=f"StartQuery failed with {code}"):
            checks._run_query(logs, "fields x", start_s=1, end_s=2)

    def test_a_malformed_query_fails_at_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_clock(monkeypatch)
        logs = _FakeLogs(counts=[{"start_error": _client_error("MalformedQueryException")}])
        with pytest.raises(ClientError, match="MalformedQueryException"):
            checks._run_query(logs, "fields x", start_s=1, end_s=2)

    def test_a_missing_query_id_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_clock(monkeypatch)
        logs = _FakeLogs(counts=[{"query_id": ""}])
        with pytest.raises(checks.TracingValidationError, match="returned no query ID"):
            checks._run_query(logs, "fields x", start_s=1, end_s=2)


class TestSpanSnapshot:
    def test_counts_and_the_trace_join(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_clock(monkeypatch)
        logs = _FakeLogs(
            counts=[
                _complete(
                    {"service": "health-monitor", "spans": "4"},
                    {"service": "cost-monitor", "spans": "not-a-number"},
                    {"service": "cost-monitor", "spans": "inf"},
                    {"service": "someone-else", "spans": "9"},
                    {"spans": "1"},
                )
            ],
            correlation=[
                _complete(
                    {"traceId": TRACE_A, "service": "manifest-processor", "spans": "2"},
                    {"traceId": TRACE_A, "service": "cost-monitor", "spans": "1"},
                    {"traceId": TRACE_B, "service": "manifest-processor", "spans": "1"},
                    {"traceId": TRACE_C, "service": "cost-monitor", "spans": "1"},
                    {"traceId": TRACE_C, "service": "manifest-processor", "spans": "1"},
                    {"service": "cost-monitor", "spans": "1"},
                )
            ],
        )

        counts, correlated = checks._span_snapshot(
            logs,
            ["health-monitor", "cost-monitor"],
            window_start_ms=INFERENCE_STARTED_MS,
            traffic_start_ms=INFERENCE_STARTED_MS + 600_000,
            correlate=True,
        )

        assert counts == {"health-monitor": 4, "cost-monitor": 0}
        assert correlated == 2
        count_query, join_query = logs.started
        # Both windows open two minutes early for skew; the join only covers the traffic.
        assert count_query["startTime"] == INFERENCE_STARTED_MS // 1000 - 120
        assert join_query["startTime"] == (INFERENCE_STARTED_MS + 600_000) // 1000 - 120
        assert count_query["endTime"] == int(WALL_START) + 120

    def test_without_the_cost_monitor_there_is_no_join(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch)
        logs = _FakeLogs(counts=[_all_counts("health-monitor")])
        counts, correlated = checks._span_snapshot(
            logs, ["health-monitor"], window_start_ms=0, traffic_start_ms=0, correlate=False
        )
        assert counts == {"health-monitor": 3}
        assert correlated is None
        assert [query["kind"] for query in logs.started] == ["counts"]


class TestWaitForSpans:
    EXPECTED = ["health-monitor", "manifest-processor", "cost-monitor"]

    def _wait(self, ctx: Any) -> dict[str, Any]:
        return checks._wait_for_spans(
            ctx,
            REGION,
            self.EXPECTED,
            window_start_ms=0,
            traffic_start_ms=0,
            correlate=True,
        )

    def test_spans_that_arrive_late_are_polled_for(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _install_clock(monkeypatch)
        logs = _FakeLogs(
            counts=[
                {"start_error": _client_error("ResourceNotFoundException")},
                _all_counts("health-monitor"),
                _all_counts(*self.EXPECTED),
            ],
            correlation=[_complete(), _JOINED],
        )
        ctx = _context(clients={("logs", REGION): logs})

        result = self._wait(ctx)

        # Poll 1: aws/spans does not exist yet. Poll 2: only the health
        # monitor has delivered and no trace joins yet. Poll 3: everything.
        assert result == {
            "span_counts": dict.fromkeys(self.EXPECTED, 3),
            "correlated_cost_traces": 1,
            "polls": 3,
        }
        assert clock.sleeps == [30.0, 30.0]

    def test_the_deadline_names_what_is_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_clock(monkeypatch, step=checks._SPAN_WAIT_SECONDS / 2)
        logs = _FakeLogs(
            counts=[_all_counts("health-monitor"), {"statuses": ["Failed"]}],
            correlation=[_complete()],
        )
        ctx = _context(clients={("logs", REGION): logs})

        with pytest.raises(checks.TracingValidationError) as info:
            self._wait(ctx)

        message = str(info.value)
        assert message.startswith(f"{REGION}: no spans for manifest-processor, cost-monitor")
        assert "no trace holds both a manifest-processor and a cost-monitor span" in message
        assert "last query: Logs Insights query ended Failed" in message
        assert "in aws/spans within 900s (3 polls)" in message

    def test_every_service_but_no_joined_trace_still_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch, step=checks._SPAN_WAIT_SECONDS)
        logs = _FakeLogs(
            counts=[_all_counts(*self.EXPECTED)],
            correlation=[_complete({"traceId": TRACE_B, "service": "cost-monitor", "spans": "1"})],
        )
        ctx = _context(clients={("logs", REGION): logs})

        with pytest.raises(checks.TracingValidationError) as info:
            self._wait(ctx)

        assert str(info.value) == (
            f"{REGION}: no trace holds both a manifest-processor and a cost-monitor span in "
            "aws/spans within 900s (2 polls)"
        )

    def test_a_region_without_the_join_expectation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_clock(monkeypatch, step=checks._SPAN_WAIT_SECONDS)
        logs = _FakeLogs(counts=[_all_counts("health-monitor")])
        ctx = _context(clients={("logs", REGION): logs})

        with pytest.raises(checks.TracingValidationError) as info:
            checks._wait_for_spans(
                ctx,
                REGION,
                ["health-monitor", "manifest-processor"],
                window_start_ms=0,
                traffic_start_ms=0,
                correlate=False,
            )

        message = str(info.value)
        assert "no spans for manifest-processor" in message
        assert "no trace holds" not in message
        assert "last query" not in message


class TestVerifyRegion:
    @staticmethod
    def _config(**changes: Any) -> dict[str, Any]:
        return {
            "enabled": True,
            "sample_ratio": 1.0,
            "enable_transaction_search": True,
            **changes,
        }

    def test_a_fully_traced_region_passes_with_counts_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch)
        logs = _FakeLogs(counts=[_all_counts(*checks.TRACED_SERVICES)], correlation=[_JOINED])
        ctx = _context(clients={("xray", REGION): _FakeXRay(), ("logs", REGION): logs})

        evidence = checks.verify_region_tracing(ctx, REGION, self._config())

        assert evidence == {
            "status": "passed",
            "transaction_search": {"destination": "CloudWatchLogs", "status": "ACTIVE"},
            "sample_ratio": 1.0,
            "expected_services": list(checks.TRACED_SERVICES),
            "skipped_services": {},
            "traffic_rounds": 3,
            "traffic": {
                path: {"requests": 3, "status_codes": {"200": 3}}
                for path in ("/api/v1/metrics", "/api/v1/policy", "/api/v1/cost/status")
            },
            "span_counts": dict.fromkeys(checks.TRACED_SERVICES, 3),
            "correlated_cost_traces": 1,
            "polls": 1,
        }
        # Counts only: no trace identifier reaches the evidence or the checkpoint.
        assert TRACE_A not in json.dumps(evidence)
        record = ctx.checkpoint.state["tracing"][REGION]
        assert record["evidence"] is evidence
        assert record["traffic"] == evidence["traffic"]
        assert TRACE_A not in json.dumps(record)
        # The count window opens at the inference action's start for the proxy.
        assert logs.started[0]["startTime"] == INFERENCE_STARTED_MS // 1000 - 120
        assert logs.started[1]["startTime"] == int(WALL_START) - 120
        assert ctx.persist.call_count == 2

    def test_a_zero_ratio_samples_nothing_and_is_skipped(self) -> None:
        ctx = _context()
        evidence = checks.verify_region_tracing(ctx, REGION, self._config(sample_ratio=0.0))
        assert evidence == {
            "status": "skipped",
            "reason": "tracing.sample_ratio is 0, so no span is sampled",
        }
        ctx.session.client.assert_not_called()
        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_an_opted_out_region_is_skipped_without_traffic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch)
        ctx = _context(clients={("xray", REGION): _FakeXRay("XRay")})
        evidence = checks.verify_region_tracing(
            ctx, REGION, self._config(enable_transaction_search=False)
        )
        assert evidence["status"] == "skipped"
        assert evidence["transaction_search"]["destination"] == "XRay"
        ctx.aws_client.make_authenticated_request.assert_not_called()
        assert ctx.checkpoint.state["tracing"][REGION]["evidence"] is evidence

    def test_a_failure_is_recorded_before_it_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch)
        ctx = _context(clients={("xray", REGION): _FakeXRay("XRay")})
        with pytest.raises(checks.TracingValidationError, match="enabler did not switch it"):
            checks.verify_region_tracing(ctx, REGION, self._config())
        record = ctx.checkpoint.state["tracing"][REGION]
        assert "enabler did not switch it" in record["failure"]
        assert "evidence" not in record
        ctx.persist.assert_called_once_with()


class TestAction:
    def test_disabled_tracing_passes_with_a_note(self) -> None:
        ctx = _context(cdk_context={"tracing": {"enabled": False}}, overrides="")
        evidence = action_module.action_tracing(ctx)
        assert evidence["tracing"]["enabled"] is False
        assert "no service exports spans" in evidence["note"]
        assert evidence["regions"] == {}
        ctx.session.client.assert_not_called()

    def test_every_region_is_verified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _context(regions=(REGION, OTHER_REGION))
        seen: list[tuple[str, dict[str, Any]]] = []

        def verify(context: Any, region: str, config: dict[str, Any]) -> dict[str, Any]:
            assert context is ctx
            seen.append((region, config))
            return {"status": "passed", "region": region}

        monkeypatch.setattr(action_module, "verify_region_tracing", verify)

        evidence = action_module.action_tracing(ctx)

        assert [region for region, _config in seen] == [REGION, OTHER_REGION]
        assert evidence == {
            "tracing": {"enabled": True, "sample_ratio": 1.0, "enable_transaction_search": True},
            "regions": {
                REGION: {"status": "passed", "region": REGION},
                OTHER_REGION: {"status": "passed", "region": OTHER_REGION},
            },
        }
