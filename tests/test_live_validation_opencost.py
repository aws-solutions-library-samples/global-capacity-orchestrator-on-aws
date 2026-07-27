"""
Tests for the live-validation opencost action and its checks module.

Covers the configuration short-circuit (cost monitoring or observability
disabled in cdk.json passes with a note), the bounded readiness poll over
/api/v1/cost/status (healthy-with-data proceeds; unhealthy or data-less
OpenCost fails with a diagnostic), region-identity verification on the
status transport, the ad-hoc report requirement (S3 key present, non-zero
rows), the S3 object existence/size proof, and the full action wiring that
records per-region evidence into the checkpoint.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.live_release_validation.actions.opencost import action_opencost
from scripts.live_release_validation.checks import opencost as checks_opencost


def _response(status_code: int, payload: dict | None = None, *, text: str = "") -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.ok = 200 <= status_code < 300
    response.text = text
    response.json.return_value = payload or {}
    return response


def _context(*, cdk_context: dict | None = None) -> SimpleNamespace:
    checkpoint = SimpleNamespace(state={})
    settings = SimpleNamespace(
        run_id="run-123",
        poll_interval_seconds=0,
        job_timeout_seconds=30,
    )
    context = SimpleNamespace(
        checkpoint=checkpoint,
        settings=settings,
        state_lock=threading.RLock(),
        deployment_regions=("us-east-1",),
        config=SimpleNamespace(project_name="gco-live", global_region="us-east-2"),
        cdk_context=cdk_context
        if cdk_context is not None
        else {
            "api_gateway": {"regional_api_enabled": True},
            "deployment_regions": {"monitoring": "us-east-2"},
        },
        session=MagicMock(),
        aws_client=MagicMock(),
    )
    context.session.get_partition_for_region.return_value = "aws"
    context.persist_callback = MagicMock()
    return context


def _healthy_status(region: str = "us-east-1") -> dict:
    return {
        "region": region,
        "opencost_healthy": True,
        "opencost_returning_data": True,
        "allocation_names": ["gco-jobs", "monitoring"],
    }


def _report_payload() -> dict:
    return {
        "region": "us-east-1",
        "bucket": "gco-live-cost-reports-123456789012-us-east-2",
        "report": {
            "s3_key": "adhoc/region=us-east-1/date=2026-07-26/allocation-x.parquet",
            "row_count": 4,
            "total_cost": 1.5,
        },
    }


class TestConfigurationGate:
    def test_default_context_enables_the_check(self):
        ctx = _context(cdk_context={})
        assert checks_opencost._cost_monitoring_configured(ctx) is True

    def test_cost_toggle_off_disables_the_check(self):
        ctx = _context(cdk_context={"cost_monitoring": {"enabled": False}})
        assert checks_opencost._cost_monitoring_configured(ctx) is False

    def test_observability_off_disables_the_check(self):
        ctx = _context(cdk_context={"cluster_observability": {"enabled": False}})
        assert checks_opencost._cost_monitoring_configured(ctx) is False

    def test_action_passes_with_a_note_when_disabled(self):
        ctx = _context(cdk_context={"cost_monitoring": {"enabled": False}})
        evidence = action_opencost(ctx)
        assert evidence["cost_monitoring_enabled"] is False
        assert "disabled" in evidence["detail"]
        ctx.aws_client.make_authenticated_request.assert_not_called()


class TestStatusPolling:
    def test_healthy_status_returns_immediately(self):
        ctx = _context()
        ctx.aws_client.make_authenticated_request.return_value = _response(200, _healthy_status())
        status = checks_opencost._wait_for_opencost_data(ctx, "us-east-1")
        assert status["opencost_returning_data"] is True
        request = ctx.aws_client.make_authenticated_request.call_args.kwargs
        assert request["path"] == "/api/v1/cost/status"
        assert request["target_region"] == "us-east-1"

    def test_polls_until_data_arrives(self):
        ctx = _context()
        warming = dict(_healthy_status(), opencost_returning_data=False)
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(200, warming),
            _response(200, warming),
            _response(200, _healthy_status()),
        ]
        status = checks_opencost._wait_for_opencost_data(ctx, "us-east-1")
        assert status["opencost_returning_data"] is True
        assert ctx.aws_client.make_authenticated_request.call_count == 3

    def test_deadline_failure_names_the_observed_state(self, monkeypatch):
        monkeypatch.setattr(checks_opencost, "_OPENCOST_READY_TIMEOUT_SECONDS", 0)
        ctx = _context()
        unhealthy = {
            "region": "us-east-1",
            "opencost_healthy": False,
            "opencost_returning_data": False,
            "last_error": "connection refused",
        }
        ctx.aws_client.make_authenticated_request.return_value = _response(200, unhealthy)
        with pytest.raises(RuntimeError, match="healthy=False"):
            checks_opencost._wait_for_opencost_data(ctx, "us-east-1")

    def test_http_failure_raises(self):
        ctx = _context()
        ctx.aws_client.make_authenticated_request.return_value = _response(
            503, text="cost monitor unavailable"
        )
        with pytest.raises(RuntimeError, match="Cost status for us-east-1 failed"):
            checks_opencost._get_cost_status(ctx, "us-east-1")

    def test_cross_region_answer_is_rejected(self):
        ctx = _context()
        ctx.aws_client.make_authenticated_request.return_value = _response(
            200, _healthy_status(region="us-west-2")
        )
        with pytest.raises(RuntimeError, match="expected 'us-east-1'"):
            checks_opencost._get_cost_status(ctx, "us-east-1")


class TestAdhocReportEvidence:
    def test_successful_report_returns_bucket_and_key(self):
        ctx = _context()
        ctx.aws_client.make_authenticated_request.return_value = _response(201, _report_payload())
        report = checks_opencost._generate_validation_report(ctx, "us-east-1")
        assert report["bucket"].startswith("gco-live-cost-reports")
        assert report["row_count"] == 4
        request = ctx.aws_client.make_authenticated_request.call_args.kwargs
        assert request["method"] == "POST"
        assert request["path"] == "/api/v1/cost/reports"

    def test_zero_row_report_fails_validation(self):
        ctx = _context()
        payload = _report_payload()
        payload["report"]["row_count"] = 0
        ctx.aws_client.make_authenticated_request.return_value = _response(201, payload)
        with pytest.raises(RuntimeError, match="zero allocation rows"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

    def test_missing_key_or_bucket_fails_validation(self):
        ctx = _context()
        payload = _report_payload()
        payload["report"].pop("s3_key")
        ctx.aws_client.make_authenticated_request.return_value = _response(201, payload)
        with pytest.raises(RuntimeError, match="omitted its S3 key"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

        complete = _report_payload()
        complete.pop("bucket")
        ctx.aws_client.make_authenticated_request.return_value = _response(201, complete)
        with pytest.raises(RuntimeError, match="omitted its bucket"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

    def test_http_failure_fails_validation(self):
        ctx = _context()
        ctx.aws_client.make_authenticated_request.return_value = _response(
            503, text="opencost down"
        )
        with pytest.raises(RuntimeError, match="Ad-hoc cost report"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")


class TestReportObjectProof:
    def test_present_object_returns_size_evidence(self):
        ctx = _context()
        s3 = MagicMock()
        s3.head_object.return_value = {"ContentLength": 2048}
        ctx.session.client.return_value = s3
        evidence = checks_opencost._verify_report_object(
            ctx, {"bucket": "bucket-x", "s3_key": "adhoc/x.parquet"}
        )
        assert evidence == {
            "bucket": "bucket-x",
            "key": "adhoc/x.parquet",
            "size_bytes": 2048,
        }
        # The head goes to the monitoring region where the bucket lives.
        assert ctx.session.client.call_args.kwargs["region_name"] == "us-east-2"

    def test_absent_object_fails_validation(self):
        ctx = _context()
        s3 = MagicMock()
        s3.head_object.side_effect = RuntimeError("404 Not Found")
        ctx.session.client.return_value = s3
        with pytest.raises(RuntimeError, match="not readable"):
            checks_opencost._verify_report_object(
                ctx, {"bucket": "bucket-x", "s3_key": "adhoc/x.parquet"}
            )

    def test_empty_object_fails_validation(self):
        ctx = _context()
        s3 = MagicMock()
        s3.head_object.return_value = {"ContentLength": 0}
        ctx.session.client.return_value = s3
        with pytest.raises(RuntimeError, match="empty"):
            checks_opencost._verify_report_object(
                ctx, {"bucket": "bucket-x", "s3_key": "adhoc/x.parquet"}
            )


class TestActionEvidence:
    def test_full_pass_records_per_region_evidence(self):
        ctx = _context()
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(200, _healthy_status()),
            _response(201, _report_payload()),
        ]
        s3 = MagicMock()
        s3.head_object.return_value = {"ContentLength": 2048}
        ctx.session.client.return_value = s3

        evidence = action_opencost(ctx)

        assert evidence["cost_monitoring_enabled"] is True
        region_evidence = evidence["regions"]["us-east-1"]
        assert region_evidence["opencost_healthy"] is True
        assert region_evidence["opencost_returning_data"] is True
        assert region_evidence["report"]["row_count"] == 4
        assert region_evidence["s3_object"]["size_bytes"] == 2048
        assert ctx.checkpoint.state["opencost"] == evidence
        ctx.persist_callback.assert_called_once()

    def test_unhealthy_region_fails_the_action(self, monkeypatch):
        monkeypatch.setattr(checks_opencost, "_OPENCOST_READY_TIMEOUT_SECONDS", 0)
        ctx = _context()
        ctx.aws_client.make_authenticated_request.return_value = _response(
            200,
            {
                "region": "us-east-1",
                "opencost_healthy": True,
                "opencost_returning_data": False,
            },
        )
        with pytest.raises(RuntimeError, match="returning_data=False"):
            action_opencost(ctx)


class TestRegistryWiring:
    def test_opencost_action_is_registered_after_central_queue(self):
        from scripts.live_release_validation.registry import build_action_registry

        registry = build_action_registry()
        names = list(registry)
        assert "opencost" in names
        assert names.index("opencost") > names.index("central-queue")
        assert registry["opencost"].dependencies == ("topology",)
