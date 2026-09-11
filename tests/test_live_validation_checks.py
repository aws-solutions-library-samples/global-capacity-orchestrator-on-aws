"""Offline coverage for the reusable ``checks/`` helpers of the live harness.

Covers ``scripts/live_release_validation/checks/{jobs, central_queue,
topology, alb_tls, policy, opencost, inference, inference_runtime,
inference_inventory}`` at the function boundary with faked API transports,
boto3 clients, kubectl runners, subprocesses, and clocks. The behaviours
pinned here are the ones the action-level tests only reach indirectly: the
crash-safe API-transport Job lifecycle (registration, escaped-submission
reconciliation, appearance/terminal/log/deletion polling and every UID/label
ownership refusal), central-queue identity binding and terminal cleanup
reconciliation, add-on convergence and health/metrics probe validation, the
ELBv2 HTTPS-only Gateway contract, the three-layer policy readback, the
OpenCost report journal, and the managed-inference lifecycle's ownership,
command, readiness, HPA, inventory, and absence-proof edge cases. Nothing here
touches AWS, Kubernetes, the network, or a real subprocess.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import zlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from cli.jobs import JobManager
from scripts.live_release_validation import constants
from scripts.live_release_validation import models as models_module
from scripts.live_release_validation.checks import alb_tls as checks_alb_tls
from scripts.live_release_validation.checks import central_queue as checks_central_queue
from scripts.live_release_validation.checks import inference as lifecycle_module
from scripts.live_release_validation.checks import inference_inventory as inventory_module
from scripts.live_release_validation.checks import inference_runtime as runtime_module
from scripts.live_release_validation.checks import jobs as checks_jobs
from scripts.live_release_validation.checks import opencost as checks_opencost
from scripts.live_release_validation.checks import policy as checks_policy
from scripts.live_release_validation.checks import topology as checks_topology
from scripts.live_release_validation.checks.inference_common import (
    InferenceCommandFailure,
    ManagedInferenceValidationError,
)
from tests._live_validation_patching import patch_live_validation_helper
from tests.test_live_release_validation import (
    TestDeterministicTopologyReadiness as _TopologyFixtures,
)
from tests.test_live_release_validation import _central_job, _context, _real_context, _response
from tests.test_live_validation_inference import (
    LIFECYCLE_ID,
    OWNER_NONCE,
    _lifecycle,
    _owned_item,
    _settings,
)
from tests.test_live_validation_inference import (
    TestSharedProxyAutoscalingProof as _SharedProxyFixtures,
)
from tests.test_live_validation_inference import _ctx as _inference_ctx
from tests.test_live_validation_opencost import _completed_report as _opencost_completed_report
from tests.test_live_validation_opencost import _context as _opencost_ctx
from tests.test_live_validation_opencost import _report_payload as _opencost_report_payload
from tests.test_live_validation_policy import _ctx as _policy_ctx
from tests.test_live_validation_policy import _payload as _policy_payload

RUN_LABEL = checks_jobs._run_token("run-123")


class _Clock:
    """Deterministic ``time`` replacement: sleeping advances the clock."""

    def __init__(self, start: float = 1_000.0, *, step: float = 1.0) -> None:
        self.now = start
        self.step = step
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += max(float(seconds), self.step)


def _install_clock(monkeypatch: pytest.MonkeyPatch, *modules: Any, **kwargs: Any) -> _Clock:
    clock = _Clock(**kwargs)
    for module in modules:
        monkeypatch.setattr(module, "time", clock)
    return clock


def _offline_job_manager() -> MagicMock:
    """Real manifest loading (checked-in YAML) with every submission faked."""
    loader = JobManager.__new__(JobManager)
    manager = MagicMock()
    manager.load_manifests.side_effect = loader.load_manifests
    return manager


def _job_context(tmp_path: Path) -> Any:
    ctx = _real_context(tmp_path)
    ctx.job_manager = _offline_job_manager()
    ctx.session.get_partition_for_region.return_value = "aws"
    ctx.cdk_context = {"api_gateway": {"regional_api_enabled": True}}
    return ctx


def _register(ctx: Any, *, path: str = "api", name: str = "gco-live-api-run-123") -> dict[str, Any]:
    return ctx.register_job(
        name=name,
        namespace="gco-jobs",
        region="us-east-1",
        path=path,
        run_label=RUN_LABEL,
        transport_region="us-east-1",
    )


def _job_payload(
    record: dict[str, Any],
    *,
    uid: str = "uid-1",
    status: dict[str, Any] | None = None,
    region: str | None = None,
    name: str | None = None,
    namespace: str | None = None,
    labels: dict[str, Any] | None = None,
    metadata_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    actual_name, actual_namespace = checks_jobs._job_reference_identity(record)
    metadata: dict[str, Any] = {
        "name": name if name is not None else actual_name,
        "namespace": namespace if namespace is not None else actual_namespace,
        "uid": uid,
        "labels": labels
        if labels is not None
        else {
            constants._RUN_JOB_LABEL: record["run_label"],
            constants._PATH_JOB_LABEL: record["path"],
        },
        **(metadata_extra or {}),
    }
    return {
        "region": region if region is not None else record["region"],
        "metadata": metadata,
        "status": status or {},
    }


_SUCCEEDED = {"conditions": [{"type": "Complete", "status": "True"}]}
_FAILED = {"conditions": [{"type": "Failed", "status": "True"}]}


def _logs_payload(record: dict[str, Any], logs: str) -> dict[str, Any]:
    name, namespace = checks_jobs._job_reference_identity(record)
    return {"region": record["region"], "job_name": name, "namespace": namespace, "logs": logs}


# --------------------------------------------------------------------------- jobs


class TestRunTokenAndManifest:
    def test_run_token_normalizes_and_bounds(self) -> None:
        assert checks_jobs._run_token("Run_ID--123") == "run-id-123"
        assert len(checks_jobs._run_token("x" * 40)) == 24

    def test_run_token_without_safe_characters_is_refused(self) -> None:
        with pytest.raises(RuntimeError, match="Kubernetes-safe token"):
            checks_jobs._run_token("!!!")

    def test_replace_token_recurses_and_ignores_scalars(self) -> None:
        value = {"a": ["__RUN_TOKEN__", 3, {"b": "x-__RUN_TOKEN__"}], "n": None}
        assert checks_jobs._replace_token(value, "tok") == {
            "a": ["tok", 3, {"b": "x-tok"}],
            "n": None,
        }

    def test_load_manifest_substitutes_the_run_token(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        manifests, name, namespace = checks_jobs._load_manifest(ctx, "api-smoke-job.yaml")
        assert name == "gco-live-api-run-123"
        assert namespace == "gco-jobs"
        assert manifests[0]["metadata"]["labels"][constants._RUN_JOB_LABEL] == "run-123"


class TestJobIdentityHelpers:
    def test_central_workload_identity_absent_partial_and_empty(self) -> None:
        assert checks_jobs._central_workload_identity({"name": "x"}) is None
        with pytest.raises(RuntimeError, match="partial central"):
            checks_jobs._central_workload_identity({"k8s_job_name": "a"})
        with pytest.raises(RuntimeError, match="empty central"):
            checks_jobs._central_workload_identity(
                {"k8s_job_name": "a", "k8s_job_namespace": "", "k8s_job_uid": "u"}
            )
        assert checks_jobs._central_workload_identity(
            {"k8s_job_name": "a", "k8s_job_namespace": "ns", "k8s_job_uid": "u"}
        ) == ("a", "ns", "u")

    def test_effective_identity_requires_bound_central_identity(self) -> None:
        record = {"path": "dynamodb", "name": "req", "namespace": "ns"}
        with pytest.raises(RuntimeError, match="has not been bound"):
            checks_jobs._effective_job_identity(record)
        bound = {**record, "k8s_job_name": "a", "k8s_job_namespace": "b", "k8s_job_uid": "u"}
        assert checks_jobs._effective_job_identity(bound) == ("a", "b")
        assert checks_jobs._effective_job_identity({"name": "n", "namespace": "s"}) == ("n", "s")

    def test_reference_identity_falls_back_to_requested_names(self) -> None:
        record = {"path": "dynamodb", "name": "req", "namespace": "ns"}
        assert checks_jobs._job_reference_identity(record) == ("req", "ns")
        bound = {**record, "k8s_job_name": "a", "k8s_job_namespace": "b", "k8s_job_uid": "u"}
        assert checks_jobs._job_reference_identity(bound) == ("a", "b")
        assert checks_jobs._job_reference_identity({"name": "n", "namespace": "s"}) == ("n", "s")

    def test_api_path_quotes_identity_and_appends_suffix(self) -> None:
        record = {"name": "a/b", "namespace": "n s", "path": "api"}
        assert checks_jobs._job_api_path(record, "/logs?tail=5") == (
            "/api/v1/jobs/n%20s/a%2Fb/logs?tail=5"
        )

    def test_response_json_rejects_invalid_and_non_object(self) -> None:
        broken = _response(200, text="<html>")
        broken.json.side_effect = ValueError("bad")
        with pytest.raises(RuntimeError, match="invalid JSON: <html>"):
            checks_jobs._response_json(broken, "Job lookup")
        listed = _response(200)
        listed.json.return_value = [1]
        with pytest.raises(RuntimeError, match="non-object JSON"):
            checks_jobs._response_json(listed, "Job lookup")
        assert checks_jobs._response_json(_response(200, {"a": 1}), "x") == {"a": 1}

    def test_verify_response_region(self) -> None:
        checks_jobs._verify_response_region({"region": "us-east-1"}, "us-east-1", "op")
        with pytest.raises(RuntimeError, match="from Region unknown, expected us-east-1"):
            checks_jobs._verify_response_region({}, "us-east-1", "op")
        with pytest.raises(RuntimeError, match="from Region eu-west-1"):
            checks_jobs._verify_response_region({"region": "eu-west-1"}, "us-east-1", "op")


def _central_metadata(record: dict[str, Any], job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    labels = {
        constants._RUN_JOB_LABEL: record["run_label"],
        constants._PATH_JOB_LABEL: "dynamodb",
        constants._CENTRAL_MANAGED_BY_LABEL: "central-queue",
        constants._CENTRAL_QUEUE_KEY_LABEL: hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:32],
    }
    annotations = {
        constants._CENTRAL_QUEUE_ID_ANNOTATION: job_id,
        constants._CENTRAL_ORIGINAL_NAME_ANNOTATION: record["name"],
    }
    return labels, annotations


class TestCentralWorkloadMetadata:
    @staticmethod
    def _record() -> dict[str, Any]:
        return {
            "name": "gco-live-ddb-run-123",
            "namespace": "gco-jobs",
            "region": "us-east-1",
            "path": "dynamodb",
            "run_label": RUN_LABEL,
            "central_queue_job_id": "job-1",
            "k8s_job_name": "gco-live-ddb-run-123-abc",
            "k8s_job_namespace": "gco-jobs",
            "k8s_job_uid": "uid-central",
        }

    def test_missing_authority_is_refused(self) -> None:
        record = {**self._record(), "central_queue_job_id": None}
        with pytest.raises(RuntimeError, match="immutable queue/UID authority"):
            checks_jobs._validate_central_workload_metadata(record, {}, {}, "uid-central")

    def test_uid_mismatch_is_refused(self) -> None:
        with pytest.raises(RuntimeError, match="UID differs"):
            checks_jobs._validate_central_workload_metadata(self._record(), {}, {}, "other")

    def test_annotations_must_be_an_object(self) -> None:
        record = self._record()
        labels, _ = _central_metadata(record, "job-1")
        with pytest.raises(RuntimeError, match="omitted ownership annotations"):
            checks_jobs._validate_central_workload_metadata(
                record, {"annotations": "nope"}, labels, "uid-central"
            )

    def test_exact_metadata_passes(self) -> None:
        record = self._record()
        labels, annotations = _central_metadata(record, "job-1")
        checks_jobs._validate_central_workload_metadata(
            record, {"annotations": annotations}, labels, "uid-central"
        )


class TestGetOwnedJob:
    def test_404_means_absent(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx)
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        assert checks_jobs._get_owned_job(ctx, record) is None
        request = ctx.aws_client.make_authenticated_request.call_args.kwargs
        assert request == {
            "method": "GET",
            "path": "/api/v1/jobs/gco-jobs/gco-live-api-run-123",
            "target_region": "us-east-1",
        }

    def test_transport_failure_raises(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx)
        ctx.aws_client.make_authenticated_request.return_value = _response(500, text="boom")
        with pytest.raises(RuntimeError, match="Job lookup failed .*500 boom"):
            checks_jobs._get_owned_job(ctx, record)

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda p: p.update(region="eu-west-1"), "came from Region eu-west-1"),
            (lambda p: p.update(metadata=None), "omitted metadata"),
            (lambda p: p["metadata"].update(name="other"), "different Kubernetes identity"),
            (lambda p: p["metadata"].update(labels=[]), "omitted ownership labels"),
            (
                lambda p: p["metadata"]["labels"].update({constants._RUN_JOB_LABEL: "x"}),
                "run label does not match",
            ),
            (
                lambda p: p["metadata"]["labels"].update({constants._PATH_JOB_LABEL: "sqs"}),
                "validation-path label",
            ),
            (lambda p: p["metadata"].update(uid=""), "omitted metadata.uid"),
        ],
        ids=[
            "region",
            "metadata",
            "identity",
            "labels",
            "run-label",
            "path-label",
            "uid",
        ],
    )
    def test_ownership_refusals(self, tmp_path: Path, mutate: Any, message: str) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx)
        payload = _job_payload(record)
        mutate(payload)
        ctx.aws_client.make_authenticated_request.return_value = _response(200, payload)
        with pytest.raises(RuntimeError, match=message):
            checks_jobs._get_owned_job(ctx, record)
        assert record["uid"] is None

    def test_success_binds_uid(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx)
        payload = _job_payload(record, uid="uid-9")
        ctx.aws_client.make_authenticated_request.return_value = _response(200, payload)
        assert checks_jobs._get_owned_job(ctx, record) == payload
        assert record["uid"] == "uid-9"
        assert record["submission_state"] == "appeared"

    def test_central_job_validates_worker_metadata(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx, path="dynamodb", name="gco-live-ddb-run-123")
        job_id = "job-1"
        ctx.bind_central_job_identity(
            record,
            job_id=job_id,
            name="gco-live-ddb-run-123-abc",
            namespace="gco-jobs",
            uid="uid-central",
            appearance_timeout_seconds=30,
        )
        labels, annotations = _central_metadata(record, job_id)
        payload = _job_payload(
            record,
            uid="uid-central",
            labels=labels,
            metadata_extra={"annotations": annotations},
        )
        ctx.aws_client.make_authenticated_request.return_value = _response(200, payload)
        assert checks_jobs._get_owned_job(ctx, record) == payload
        assert ctx.aws_client.make_authenticated_request.call_args.kwargs["path"] == (
            "/api/v1/jobs/gco-jobs/gco-live-ddb-run-123-abc"
        )


class TestReactivateDeletedRecord:
    def test_live_record_is_untouched(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx)
        checks_jobs._reactivate_deleted_job_record(ctx, record)
        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_visible_job_blocks_replay(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx)
        record["deleted"] = True
        ctx.aws_client.make_authenticated_request.return_value = _response(
            200, _job_payload(record)
        )
        with pytest.raises(RuntimeError, match="exact UID still exists"):
            checks_jobs._reactivate_deleted_job_record(ctx, record)

    def test_absent_job_resets_submission_state(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx)
        record.update(
            deleted=True,
            uid="uid-old",
            submission_state="deleted",
            submission={"x": 1},
            deleted_at=5.0,
            validation_evidence={"y": 2},
        )
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        ctx.persist_callback.reset_mock()
        checks_jobs._reactivate_deleted_job_record(ctx, record)
        assert record["uid"] is None
        assert record["deleted"] is False
        assert record["submission_state"] == "registered"
        assert record["previous_uids"] == ["uid-old"]
        assert "submission" not in record
        assert "deleted_at" not in record
        assert "validation_evidence" not in record
        ctx.persist_callback.assert_called_once_with(ctx.checkpoint)

    def test_absent_job_without_uid_keeps_no_history(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx)
        record.update(deleted=True, submission_state="deleted")
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        checks_jobs._reactivate_deleted_job_record(ctx, record)
        assert "previous_uids" not in record
        assert record["submission_state"] == "registered"

    def test_register_job_can_skip_reactivation(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx)
        record.update(deleted=True, submission_state="deleted")
        again = checks_jobs._register_job(
            ctx,
            name=record["name"],
            namespace=record["namespace"],
            execution_region="us-east-1",
            path="api",
            reactivate_deleted=False,
        )
        assert again is record
        assert again["deleted"] is True
        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_register_job_reactivates_by_default(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx)
        record.update(deleted=True, submission_state="deleted", uid="uid-old")
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        again = checks_jobs._register_job(
            ctx,
            name=record["name"],
            namespace=record["namespace"],
            execution_region="us-east-1",
            path="api",
        )
        assert again is record
        assert again["deleted"] is False
        assert again["previous_uids"] == ["uid-old"]
        assert again["transport_region"] == "us-east-1"


class TestAppearanceAndReconciliationWaits:
    def test_appearance_timeout_is_the_smaller_budget(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        assert checks_jobs._job_appearance_timeout(ctx) == 900

    def test_appearance_sets_deadline_then_returns_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _job_context(tmp_path)
        clock = _install_clock(monkeypatch, checks_jobs, models_module)
        record = _register(ctx)
        payload = _job_payload(record)
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(404),
            _response(200, payload),
        ]
        assert checks_jobs._wait_for_owned_job_appearance(ctx, record) == payload
        assert record["appearance_deadline"] == 1_000.0 + 900
        assert clock.sleeps == [0.0]

    def test_appearance_deadline_raises_or_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        record = _register(ctx)
        record["appearance_deadline"] = 999.0
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        with pytest.raises(TimeoutError, match="did not appear before the bounded"):
            checks_jobs._wait_for_owned_job_appearance(ctx, record)
        assert (
            checks_jobs._wait_for_owned_job_appearance(ctx, record, raise_on_timeout=False) is None
        )

    def test_ambiguous_reconciliation_requires_deadline(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx)
        with pytest.raises(RuntimeError, match="no reconciliation deadline"):
            checks_jobs._wait_for_ambiguous_job_reconciliation(ctx, record)

    def test_ambiguous_reconciliation_polls_until_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _job_context(tmp_path)
        clock = _install_clock(monkeypatch, checks_jobs, models_module)
        record = _register(ctx)
        record["submission_reconcile_deadline"] = 1_001.5
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        assert checks_jobs._wait_for_ambiguous_job_reconciliation(ctx, record) is None
        assert clock.sleeps == [0.0, 0.0]

        payload = _job_payload(record)
        record["submission_reconcile_deadline"] = clock.now + 10
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(404),
            _response(200, payload),
        ]
        assert checks_jobs._wait_for_ambiguous_job_reconciliation(ctx, record) == payload


class TestTerminalLogsAndAbsence:
    def test_job_status_classification(self) -> None:
        assert checks_jobs._job_status({"status": _SUCCEEDED}) == "succeeded"
        assert checks_jobs._job_status({"status": _FAILED}) == "failed"
        assert checks_jobs._job_status({"status": {"active": 1}}) == "running"
        assert checks_jobs._job_status({}) == "pending"
        mixed = {"status": {"conditions": [{"type": "Complete", "status": "False"}]}}
        assert checks_jobs._job_status(mixed) == "pending"

    def test_terminal_wait_records_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        record = _register(ctx)
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(200, _job_payload(record, status={"active": 1})),
            _response(200, _job_payload(record, status=_SUCCEEDED)),
        ]
        final, history = checks_jobs._wait_for_owned_job_terminal(ctx, record)
        assert checks_jobs._job_status(final) == "succeeded"
        assert [item["status"] for item in history] == ["running", "succeeded"]

    def test_terminal_wait_refuses_disappearance_and_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module, step=2_000.0)
        record = _register(ctx)
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        with pytest.raises(RuntimeError, match="disappeared before reaching a terminal"):
            checks_jobs._wait_for_owned_job_terminal(ctx, record)
        ctx.aws_client.make_authenticated_request.return_value = _response(
            200, _job_payload(record)
        )
        with pytest.raises(TimeoutError, match="did not complete within 1800s"):
            checks_jobs._wait_for_owned_job_terminal(ctx, record)

    def test_logs_require_visible_job_and_matching_identity(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx)
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        with pytest.raises(RuntimeError, match="before its logs were read"):
            checks_jobs._owned_job_logs(ctx, record)

        job = _response(200, _job_payload(record))
        ctx.aws_client.make_authenticated_request.side_effect = [job, _response(502, text="bad")]
        with pytest.raises(RuntimeError, match="Job log lookup failed: 502 bad"):
            checks_jobs._owned_job_logs(ctx, record)

        wrong = {**_logs_payload(record, "x"), "job_name": "other"}
        ctx.aws_client.make_authenticated_request.side_effect = [job, _response(200, wrong)]
        with pytest.raises(RuntimeError, match="different Job identity"):
            checks_jobs._owned_job_logs(ctx, record)

        empty = {**_logs_payload(record, ""), "logs": None}
        ctx.aws_client.make_authenticated_request.side_effect = [job, _response(200, empty)]
        assert checks_jobs._owned_job_logs(ctx, record, tail=7) == ""
        assert ctx.aws_client.make_authenticated_request.call_args.kwargs["path"] == (
            "/api/v1/jobs/gco-jobs/gco-live-api-run-123/logs?tail=7"
        )

    def test_absence_requires_three_consecutive_misses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _job_context(tmp_path)
        clock = _install_clock(monkeypatch, checks_jobs, models_module)
        record = _register(ctx)
        present = _response(200, _job_payload(record))
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(404),
            _response(404),
            present,
            _response(404),
            _response(404),
            _response(404),
        ]
        checks_jobs._wait_for_owned_job_absence(ctx, record)
        assert ctx.aws_client.make_authenticated_request.call_count == 6
        assert clock.sleeps == [0.0] * 5

    def test_absence_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module, step=100.0)
        record = _register(ctx)
        ctx.aws_client.make_authenticated_request.return_value = _response(
            200, _job_payload(record)
        )
        with pytest.raises(TimeoutError, match="remained visible"):
            checks_jobs._wait_for_owned_job_absence(ctx, record)


class TestDeleteOwnedJob:
    def test_unbound_central_record_before_submission_is_marked_absent(
        self, tmp_path: Path
    ) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx, path="dynamodb", name="gco-live-ddb-run-123")
        assert checks_jobs._delete_owned_job(ctx, record) == {
            "not_submitted": True,
            "already_absent": True,
        }
        assert record["deleted"] is True
        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_unbound_central_record_after_submission_is_unresolved(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx, path="dynamodb", name="gco-live-ddb-run-123")
        record["submission_state"] = "submitted"
        with pytest.raises(RuntimeError, match="no worker-persisted Kubernetes identity"):
            checks_jobs._delete_owned_job(ctx, record)

    def test_submitting_without_uid_reconciles_then_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        record = _register(ctx)
        record["submission_state"] = "submitting"
        record["submission_reconcile_deadline"] = 999.0
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        with pytest.raises(RuntimeError, match="no immutable Kubernetes UID was observed"):
            checks_jobs._delete_owned_job(ctx, record)

    def test_submitted_without_uid_waits_for_appearance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        record = _register(ctx)
        record["submission_state"] = "submitted"
        record["appearance_deadline"] = 999.0
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        with pytest.raises(RuntimeError, match="no immutable Kubernetes UID was observed"):
            checks_jobs._delete_owned_job(ctx, record)
        assert ctx.aws_client.make_authenticated_request.call_count == 2

    def test_absent_after_uid_observation_waits_for_stable_absence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        record = _register(ctx)
        record.update(uid="uid-1", submission_state="appeared")
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        assert checks_jobs._delete_owned_job(ctx, record) == {
            "authoritative_absence_after_uid_observation": True
        }
        assert record["deleted"] is True
        assert ctx.aws_client.make_authenticated_request.call_count == 4

    def test_prepared_record_that_never_submitted_is_marked_absent(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx)
        record["submission_state"] = "prepared"
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        assert checks_jobs._delete_owned_job(ctx, record) == {
            "not_submitted": True,
            "already_absent": True,
        }
        assert record["submission_state"] == "deleted"

    def test_visible_job_without_checkpointed_uid_is_refused(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx)
        with (
            patch_live_validation_helper("_get_owned_job", return_value=_job_payload(record)),
            pytest.raises(RuntimeError, match="no checkpointed UID at deletion time"),
        ):
            checks_jobs._delete_owned_job(ctx, record)

    def _deletable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        record = _register(ctx)
        return ctx, record

    def test_delete_404_is_authoritative(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, record = self._deletable(tmp_path, monkeypatch)
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(200, _job_payload(record, uid="uid-1")),
            _response(404),
            _response(404),
            _response(404),
            _response(404),
        ]
        assert checks_jobs._delete_owned_job(ctx, record) == {
            "authoritative_404_after_uid_observation": True
        }
        delete_call = ctx.aws_client.make_authenticated_request.call_args_list[1].kwargs
        assert delete_call["method"] == "DELETE"
        assert (
            delete_call["path"] == "/api/v1/jobs/gco-jobs/gco-live-api-run-123?expected_uid=uid-1"
        )
        assert record["deleted"] is True

    def test_delete_409_means_uid_changed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, record = self._deletable(tmp_path, monkeypatch)
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(200, _job_payload(record)),
            _response(409),
        ]
        with pytest.raises(RuntimeError, match="precondition rejected"):
            checks_jobs._delete_owned_job(ctx, record)

    def test_delete_other_failure_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, record = self._deletable(tmp_path, monkeypatch)
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(200, _job_payload(record)),
            _response(500, text="oops"),
        ]
        with pytest.raises(RuntimeError, match="Job deletion failed: 500 oops"):
            checks_jobs._delete_owned_job(ctx, record)

    def test_delete_response_uid_must_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, record = self._deletable(tmp_path, monkeypatch)
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(200, _job_payload(record, uid="uid-1")),
            _response(200, {"region": "us-east-1", "uid": "uid-2"}),
        ]
        with pytest.raises(RuntimeError, match="deletion response UID did not match"):
            checks_jobs._delete_owned_job(ctx, record)

    def test_delete_success_returns_response_and_waits_for_absence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, record = self._deletable(tmp_path, monkeypatch)
        deletion = {"region": "us-east-1", "uid": "uid-1", "deleted": True}
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(200, _job_payload(record, uid="uid-1")),
            _response(200, deletion),
            _response(404),
            _response(404),
            _response(404),
        ]
        assert checks_jobs._delete_owned_job(ctx, record) == deletion
        assert record["deleted"] is True

    def test_delete_response_without_uid_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, record = self._deletable(tmp_path, monkeypatch)
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(200, _job_payload(record, uid="uid-1")),
            _response(200, {"region": "us-east-1"}),
            _response(404),
            _response(404),
            _response(404),
        ]
        assert checks_jobs._delete_owned_job(ctx, record) == {"region": "us-east-1"}


class TestCompleteJobLifecycle:
    def _scripted(self, ctx: Any, record: dict[str, Any], *, logs: str) -> None:
        job = _job_payload(record, uid="uid-1")
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(200, job),
            _response(200, _job_payload(record, uid="uid-1", status=_SUCCEEDED)),
            _response(200, job),
            _response(200, _logs_payload(record, logs)),
            _response(200, job),
            _response(200, {"region": "us-east-1", "uid": "uid-1"}),
            _response(404),
            _response(404),
            _response(404),
        ]

    def test_success_evidence_and_deletion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        record = _register(ctx)
        self._scripted(ctx, record, logs="hello GCO_LIVE_API_run-123 done")
        evidence = checks_jobs._complete_job_lifecycle(
            ctx, record=record, marker="GCO_LIVE_API_run-123"
        )
        assert evidence["status"] == "succeeded"
        assert evidence["uid"] == "uid-1"
        assert evidence["appearance"] == {"region": "us-east-1", "uid": "uid-1"}
        assert evidence["deletion"] == {"region": "us-east-1", "uid": "uid-1"}
        assert evidence["requested_name"] == "gco-live-api-run-123"
        assert record["validation_evidence"]["marker"] == "GCO_LIVE_API_run-123"
        assert "deletion" not in record["validation_evidence"]

    def test_missing_marker_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        record = _register(ctx)
        self._scripted(ctx, record, logs="no marker here")
        with pytest.raises(RuntimeError, match="did not contain expected marker"):
            checks_jobs._complete_job_lifecycle(ctx, record=record, marker="GCO_LIVE_API_run-123")

    def test_failed_job_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        record = _register(ctx)
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(200, _job_payload(record)),
            _response(200, _job_payload(record, status=_FAILED)),
        ]
        with pytest.raises(RuntimeError, match="finished with status failed"):
            checks_jobs._complete_job_lifecycle(ctx, record=record, marker="M")

    def test_defensive_none_appearance_is_refused(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        record = _register(ctx)
        with (
            patch_live_validation_helper("_wait_for_owned_job_appearance", return_value=None),
            pytest.raises(RuntimeError, match="never appeared in us-east-1"),
        ):
            checks_jobs._complete_job_lifecycle(ctx, record=record, marker="M")


class TestApiTransportLifecycle:
    MARKER = "GCO_LIVE_API_run-123"

    def _run(self, ctx: Any) -> dict[str, Any]:
        return checks_jobs._run_api_transport_lifecycle(
            ctx,
            manifest_filename="api-smoke-job.yaml",
            path="api",
            marker_prefix="API",
        )

    def _completion_responses(self, record: dict[str, Any]) -> list[Any]:
        job = _job_payload(record, uid="uid-1")
        return [
            _response(200, job),
            _response(200, _job_payload(record, uid="uid-1", status=_SUCCEEDED)),
            _response(200, job),
            _response(200, _logs_payload(record, f"log {self.MARKER}")),
            _response(200, job),
            _response(200, {"region": "us-east-1", "uid": "uid-1"}),
            _response(404),
            _response(404),
            _response(404),
        ]

    def _template_record(self) -> dict[str, Any]:
        return {
            "name": "gco-live-api-run-123",
            "namespace": "gco-jobs",
            "region": "us-east-1",
            "path": "api",
            "run_label": RUN_LABEL,
        }

    def test_fresh_submission_runs_the_full_lifecycle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        template = self._template_record()
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(404),
            *self._completion_responses(template),
        ]
        ctx.job_manager.submit_job.return_value = {
            "job_name": "gco-live-api-run-123",
            "namespace": "gco-jobs",
            "region": "us-east-1",
        }
        lifecycle = self._run(ctx)
        assert lifecycle["submission"]["job_name"] == "gco-live-api-run-123"
        assert lifecycle["status"] == "succeeded"
        record = ctx.checkpoint.state["jobs"][0]
        assert record["submission_state"] == "deleted"
        assert record["submission_attempts"] == 1
        submit = ctx.job_manager.submit_job.call_args
        assert submit.kwargs["namespace"] == "gco-jobs"
        assert submit.kwargs["target_region"] == "us-east-1"
        assert submit.kwargs["labels"] == {constants._RUN_JOB_LABEL: "run-123"}
        assert submit.args[0][0]["metadata"]["name"] == "gco-live-api-run-123"

    def test_submission_identity_mismatch_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        ctx.job_manager.submit_job.return_value = {"job_name": "other", "namespace": "gco-jobs"}
        with pytest.raises(RuntimeError, match="submission identity mismatch"):
            self._run(ctx)
        assert ctx.checkpoint.state["jobs"][0]["submission_state"] == "submitting"

    def test_submission_region_mismatch_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        ctx.job_manager.submit_job.return_value = {"region": "eu-west-1"}
        with pytest.raises(RuntimeError, match="executed in eu-west-1, expected us-east-1"):
            self._run(ctx)

    def test_existing_job_skips_submission(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        template = self._template_record()
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(200, _job_payload(template, uid="uid-1")),
            *self._completion_responses(template),
        ]
        lifecycle = self._run(ctx)
        assert lifecycle["submission"] == {"reconciled_existing_job": True}
        ctx.job_manager.submit_job.assert_not_called()

    def _resume_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, **fields: Any
    ) -> Any:
        ctx = _job_context(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        record = _register(ctx)
        manifests, _, namespace = checks_jobs._load_manifest(ctx, "api-smoke-job.yaml")
        ctx.prepare_job_submission(
            record,
            envelope={
                "transport": "api",
                "manifests": manifests,
                "namespace": namespace,
                "execution_region": "us-east-1",
                "transport_region": "us-east-1",
                "labels": {constants._RUN_JOB_LABEL: "run-123"},
            },
            resumable=False,
        )
        record["submission_state"] = state
        record.update(fields)
        return ctx

    def test_submitting_resume_without_job_blocks_replay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = self._resume_context(
            tmp_path, monkeypatch, "submitting", submission_reconcile_deadline=999.0
        )
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        with pytest.raises(RuntimeError, match="automatic replay is forbidden"):
            self._run(ctx)
        record = ctx.checkpoint.state["jobs"][0]
        assert record["submission_state"] == "blocked"
        assert "non-idempotent boundary" in record["submission_blocked_reason"]
        ctx.job_manager.submit_job.assert_not_called()

    def test_submitting_resume_reconciles_an_escaped_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = self._resume_context(
            tmp_path, monkeypatch, "submitting", submission_reconcile_deadline=5_000.0
        )
        template = self._template_record()
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(404),
            _response(200, _job_payload(template, uid="uid-1")),
            *self._completion_responses(template),
        ]
        lifecycle = self._run(ctx)
        assert lifecycle["submission"] == {"reconciled_existing_job": True}
        ctx.job_manager.submit_job.assert_not_called()

    def test_submitted_resume_waits_for_appearance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = self._resume_context(tmp_path, monkeypatch, "submitted", appearance_deadline=5_000.0)
        template = self._template_record()
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(404),
            _response(200, _job_payload(template, uid="uid-1")),
            *self._completion_responses(template),
        ]
        lifecycle = self._run(ctx)
        assert lifecycle["submission"] == {"reconciled_existing_job": True}

    def test_blocked_resume_surfaces_the_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = self._resume_context(
            tmp_path, monkeypatch, "blocked", submission_blocked_reason="earlier failure"
        )
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        with pytest.raises(RuntimeError, match="earlier failure"):
            self._run(ctx)

    def test_blocked_resume_without_reason_uses_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = self._resume_context(tmp_path, monkeypatch, "blocked")
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        with pytest.raises(RuntimeError, match="api submission blocked"):
            self._run(ctx)

    def test_unexpected_state_cannot_submit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = self._resume_context(tmp_path, monkeypatch, "not_submitted")
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        with pytest.raises(RuntimeError, match="Cannot submit api Job from state 'not_submitted'"):
            self._run(ctx)


# ------------------------------------------------------------------ central_queue


class TestCentralQueueHelpers:
    def test_central_manifest_rewrites_identity_and_marker(self, tmp_path: Path) -> None:
        ctx = _job_context(tmp_path)
        manifest, name, namespace, marker = checks_central_queue._central_manifest(ctx)
        assert name == "gco-live-ddb-run-123"
        assert namespace == "gco-jobs"
        assert marker == "GCO_LIVE_DDB_run-123"
        assert manifest["metadata"]["name"] == name
        assert manifest["metadata"]["labels"][constants._PATH_JOB_LABEL] == "dynamodb"
        template = manifest["spec"]["template"]
        assert template["metadata"]["labels"][constants._PATH_JOB_LABEL] == "dynamodb"
        assert template["spec"]["containers"][0]["command"] == ["sh", "-c", f"echo {marker}"]

    def test_deserialize_item_uses_dynamodb_types(self) -> None:
        item = {"job_id": {"S": "j"}, "attempts": {"N": "2"}, "flag": {"BOOL": True}}
        assert checks_central_queue._deserialize_item(item) == {
            "job_id": "j",
            "attempts": Decimal("2"),
            "flag": True,
        }

    def test_read_central_job_item_reads_consistently(self) -> None:
        ctx = _context()
        dynamodb = MagicMock()
        dynamodb.get_item.return_value = {"Item": {"job_id": {"S": "j"}, "status": {"S": "ok"}}}
        ctx.session.client.return_value = dynamodb
        assert checks_central_queue._read_central_job_item(ctx, "j") == {
            "job_id": "j",
            "status": "ok",
        }
        ctx.session.client.assert_called_once_with("dynamodb", region_name="us-east-1")
        dynamodb.get_item.assert_called_once_with(
            TableName="gco-live-jobs",
            Key={"job_id": {"S": "j"}},
            ConsistentRead=True,
        )
        dynamodb.get_item.return_value = {}
        with pytest.raises(RuntimeError, match="was not found in gco-live-jobs"):
            checks_central_queue._read_central_job_item(ctx, "j")

    def test_kubernetes_job_name_is_bounded_and_deterministic(self) -> None:
        job_id = "job-1"
        suffix = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]
        assert checks_central_queue._central_queue_kubernetes_job_name("My Job!", job_id) == (
            f"my-job-{suffix}"
        )
        assert checks_central_queue._central_queue_kubernetes_job_name("!!!", job_id) == (
            f"gco-job-{suffix}"
        )
        long_name = checks_central_queue._central_queue_kubernetes_job_name("a" * 80, job_id)
        assert len(long_name) == 63

    def test_persisted_identity_partial_and_empty_are_refused(self) -> None:
        assert (
            checks_central_queue._central_persisted_kubernetes_identity({}, required=False) is None
        )
        with pytest.raises(RuntimeError, match="omitted worker Kubernetes identity"):
            checks_central_queue._central_persisted_kubernetes_identity({}, required=True)
        with pytest.raises(RuntimeError, match="partial Kubernetes identity"):
            checks_central_queue._central_persisted_kubernetes_identity(
                {"k8s_job_name": "a"}, required=False
            )
        with pytest.raises(RuntimeError, match="empty Kubernetes identity field"):
            checks_central_queue._central_persisted_kubernetes_identity(
                {"k8s_job_name": "a", "k8s_job_namespace": "", "k8s_job_uid": "u"},
                required=False,
            )

    def test_checkpoint_identity_validation(self) -> None:
        identity = {"k8s_job_name": "a", "k8s_job_namespace": "b", "k8s_job_uid": "c"}
        validate = checks_central_queue._validate_central_checkpoint_kubernetes_identity
        validate({}, identity)
        validate({**identity, "k8s_identity_source": "dynamodb"}, identity)
        with pytest.raises(RuntimeError, match="partial Kubernetes identity"):
            validate({"k8s_job_name": "a"}, identity)
        with pytest.raises(RuntimeError, match="unexpected source"):
            validate({**identity, "k8s_identity_source": "guess"}, identity)
        with pytest.raises(RuntimeError, match="source has no Kubernetes identity"):
            validate({"k8s_identity_source": "dynamodb"}, identity)
        with pytest.raises(RuntimeError, match="identity changed: k8s_job_uid"):
            validate({**identity, "k8s_job_uid": "other"}, identity)

    def test_validate_central_job_identity(self) -> None:
        central = {
            "job_id": "j",
            "job_name": "n",
            "namespace": "ns",
            "target_region": "us-east-1",
            "idempotency_key": "k",
        }
        job = {**central}
        checks_central_queue._validate_central_job_identity(central, job)
        checks_central_queue._validate_central_job_identity(
            central, {**job, "idempotency_key": None}
        )
        with pytest.raises(RuntimeError, match="different namespace for 'ns'"):
            checks_central_queue._validate_central_job_identity(central, {**job, "namespace": "x"})
        with pytest.raises(RuntimeError, match="idempotency key changed"):
            checks_central_queue._validate_central_job_identity(
                central, {**job, "idempotency_key": "z"}
            )


def _central_fixture(
    tmp_path: Path,
) -> tuple[Any, dict[str, Any], dict[str, Any], str]:
    """A real context with one prepared dynamodb workload and its central record."""
    ctx = _job_context(tmp_path)
    ctx.session.client.return_value = MagicMock()
    key = "gco-live-validation:run-123:central"
    job_id = checks_central_queue._central_queue_job_id(key)
    record = ctx.register_job(
        name="gco-live-ddb-run-123",
        namespace="gco-jobs",
        region="us-east-1",
        path="dynamodb",
        run_label=RUN_LABEL,
        transport_region="us-east-1",
    )
    ctx.prepare_job_submission(
        record,
        envelope={"transport": "central-queue", "job_id": job_id},
        resumable=True,
    )
    central_record = checks_central_queue._register_central_job(
        ctx,
        job_id=job_id,
        idempotency_key=key,
        record=record,
        marker="GCO_LIVE_DDB_run-123",
        body={"manifest": {"kind": "Job"}},
    )
    return ctx, record, central_record, job_id


def _persisted(job_id: str, **overrides: Any) -> dict[str, Any]:
    job = _central_job(job_id, status=str(overrides.pop("status", "succeeded")))
    job["target_region"] = "us-east-1"
    job.update(overrides)
    return job


class TestRegisterCentralJob:
    def test_registration_persists_and_is_idempotent(self, tmp_path: Path) -> None:
        ctx, record, central_record, job_id = _central_fixture(tmp_path)
        assert central_record["submission_state"] == "prepared"
        assert central_record["cleanup_complete"] is False
        assert central_record["appearance_deadline"] is None
        record["appearance_deadline"] = 42.0
        again = checks_central_queue._register_central_job(
            ctx,
            job_id=job_id,
            idempotency_key="gco-live-validation:run-123:central",
            record=record,
            marker="GCO_LIVE_DDB_run-123",
            body={"manifest": {"kind": "Job"}},
        )
        assert again is central_record
        assert again["appearance_deadline"] == 42.0

    def test_registration_rejects_changed_identity_and_duplicates(self, tmp_path: Path) -> None:
        ctx, record, central_record, job_id = _central_fixture(tmp_path)
        with pytest.raises(RuntimeError, match="identity changed for .*: marker"):
            checks_central_queue._register_central_job(
                ctx,
                job_id=job_id,
                idempotency_key="gco-live-validation:run-123:central",
                record=record,
                marker="OTHER",
                body={"manifest": {"kind": "Job"}},
            )
        ctx.checkpoint.state["central_jobs"].append(dict(central_record))
        with pytest.raises(RuntimeError, match="duplicate central Job records"):
            checks_central_queue._register_central_job(
                ctx,
                job_id=job_id,
                idempotency_key="gco-live-validation:run-123:central",
                record=record,
                marker="GCO_LIVE_DDB_run-123",
                body={"manifest": {"kind": "Job"}},
            )
        ctx.checkpoint.state["central_jobs"] = "corrupt"
        with pytest.raises(RuntimeError, match="central_jobs must be a list of objects"):
            checks_central_queue._register_central_job(
                ctx,
                job_id=job_id,
                idempotency_key="gco-live-validation:run-123:central",
                record=record,
                marker="GCO_LIVE_DDB_run-123",
                body={"manifest": {"kind": "Job"}},
            )

    def test_workload_record_resolution(self, tmp_path: Path) -> None:
        ctx, record, central_record, _ = _central_fixture(tmp_path)
        assert checks_central_queue._central_workload_record(ctx, central_record) is record
        ctx.checkpoint.state["jobs"].append(dict(record))
        with pytest.raises(RuntimeError, match="exactly one checkpointed workload"):
            checks_central_queue._central_workload_record(ctx, central_record)
        ctx.checkpoint.state["jobs"] = [1]
        with pytest.raises(RuntimeError, match="jobs must be a list of objects"):
            checks_central_queue._central_workload_record(ctx, central_record)


class TestReconcileCentralWorkloadIdentity:
    def test_missing_identity_returns_resolved_record(self, tmp_path: Path) -> None:
        ctx, record, central_record, job_id = _central_fixture(tmp_path)
        persisted = _persisted(job_id, status="pending")
        result = checks_central_queue._reconcile_central_workload_identity(
            ctx, central_record, persisted, require_identity=False
        )
        assert result is record
        assert record["uid"] is None

    def test_wrong_name_or_namespace_is_refused(self, tmp_path: Path) -> None:
        ctx, record, central_record, job_id = _central_fixture(tmp_path)
        with pytest.raises(RuntimeError, match="unexpected Kubernetes Job name"):
            checks_central_queue._reconcile_central_workload_identity(
                ctx, central_record, _persisted(job_id, k8s_job_name="foreign")
            )
        with pytest.raises(RuntimeError, match="different Kubernetes namespace"):
            checks_central_queue._reconcile_central_workload_identity(
                ctx, central_record, _persisted(job_id, k8s_job_namespace="other")
            )

    def test_binding_updates_both_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, record, central_record, job_id = _central_fixture(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        persisted = _persisted(job_id)
        result = checks_central_queue._reconcile_central_workload_identity(
            ctx, central_record, persisted
        )
        assert result is record
        assert record["k8s_job_uid"] == "uid-central-1"
        assert central_record["k8s_identity_source"] == "dynamodb"
        assert central_record["k8s_job_name"] == persisted["k8s_job_name"]
        assert central_record["workload_appearance_deadline"] == record["appearance_deadline"]


class TestReconcileCentralCleanupWorkload:
    def test_non_terminal_evidence_is_refused(self, tmp_path: Path) -> None:
        ctx, _, central_record, job_id = _central_fixture(tmp_path)
        with pytest.raises(RuntimeError, match="not terminal"):
            checks_central_queue._reconcile_central_cleanup_workload(
                ctx, central_record, _persisted(job_id, status="running")
            )

    def test_no_workload_proof_requires_failed_status(self, tmp_path: Path) -> None:
        ctx, _, central_record, job_id = _central_fixture(tmp_path)
        with pytest.raises(RuntimeError, match="valid only for a failed queue record"):
            checks_central_queue._reconcile_central_cleanup_workload(
                ctx, central_record, _persisted(job_id, workload_not_created=True)
            )

    def test_succeeded_record_binds_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, record, central_record, job_id = _central_fixture(tmp_path)
        _install_clock(monkeypatch, checks_jobs, models_module)
        workload, proven = checks_central_queue._reconcile_central_cleanup_workload(
            ctx, central_record, _persisted(job_id)
        )
        assert workload is record
        assert proven is False
        assert record["uid"] == "uid-central-1"

    def _failed_uncreated(self, job_id: str) -> dict[str, Any]:
        return _persisted(job_id, status="failed", workload_not_created=True)

    def test_worker_proof_conflicts(self, tmp_path: Path) -> None:
        ctx, record, central_record, job_id = _central_fixture(tmp_path)
        reconcile = checks_central_queue._reconcile_central_cleanup_workload
        with_identity = {
            **self._failed_uncreated(job_id),
            "k8s_job_name": "n",
            "k8s_job_namespace": "ns",
            "k8s_job_uid": "u",
        }
        with pytest.raises(RuntimeError, match="both no-workload proof and Kubernetes identity"):
            reconcile(ctx, central_record, with_identity)

        record.update(k8s_job_name="n", k8s_job_namespace="ns", k8s_job_uid="u")
        with pytest.raises(RuntimeError, match="already has checkpointed workload identity"):
            reconcile(ctx, central_record, self._failed_uncreated(job_id))
        for key in ("k8s_job_name", "k8s_job_namespace", "k8s_job_uid"):
            record.pop(key)

        central_record.update(k8s_job_name="n", k8s_job_namespace="ns", k8s_job_uid="u")
        with pytest.raises(RuntimeError, match="already has central checkpoint identity"):
            reconcile(ctx, central_record, self._failed_uncreated(job_id))
        for key in ("k8s_job_name", "k8s_job_namespace", "k8s_job_uid"):
            central_record.pop(key)

        record["uid"] = "uid-x"
        with pytest.raises(RuntimeError, match="already has Kubernetes UID authority"):
            reconcile(ctx, central_record, self._failed_uncreated(job_id))

    def test_worker_proof_marks_or_verifies_deleted_workload(self, tmp_path: Path) -> None:
        ctx, record, central_record, job_id = _central_fixture(tmp_path)
        reconcile = checks_central_queue._reconcile_central_cleanup_workload
        record["submission_state"] = "submitted"
        workload, proven = reconcile(ctx, central_record, self._failed_uncreated(job_id))
        assert (workload, proven) == (record, True)
        assert record["submission_state"] == "not_submitted"
        assert record["central_worker_not_created_job_id"] == job_id

        record["submission_state"] = "deleted"
        assert reconcile(ctx, central_record, self._failed_uncreated(job_id)) == (record, True)
        record["central_worker_not_created_job_id"] = "other"
        with pytest.raises(RuntimeError, match="lacks matching worker no-workload proof"):
            reconcile(ctx, central_record, self._failed_uncreated(job_id))

    def _cancelled(self, job_id: str) -> dict[str, Any]:
        return _persisted(job_id, status="cancelled")

    def test_cancelled_before_claim_conflicts(self, tmp_path: Path) -> None:
        ctx, record, central_record, job_id = _central_fixture(tmp_path)
        reconcile = checks_central_queue._reconcile_central_cleanup_workload
        with pytest.raises(RuntimeError, match="unexpectedly has workload identity"):
            reconcile(
                ctx,
                central_record,
                {
                    **self._cancelled(job_id),
                    "k8s_job_name": "n",
                    "k8s_job_namespace": "ns",
                    "k8s_job_uid": "u",
                },
            )
        record["uid"] = "uid-x"
        with pytest.raises(RuntimeError, match="already has Kubernetes UID authority"):
            reconcile(ctx, central_record, self._cancelled(job_id))

    def test_cancelled_before_claim_marks_or_verifies_deleted_workload(
        self, tmp_path: Path
    ) -> None:
        ctx, record, central_record, job_id = _central_fixture(tmp_path)
        reconcile = checks_central_queue._reconcile_central_cleanup_workload
        record["submission_state"] = "submitted"
        assert reconcile(ctx, central_record, self._cancelled(job_id)) == (record, True)
        assert record["submission_state"] == "not_submitted"
        assert record["central_cancelled_before_claim_job_id"] == job_id

        record["submission_state"] = "deleted"
        assert reconcile(ctx, central_record, self._cancelled(job_id)) == (record, True)
        record["central_cancelled_before_claim_job_id"] = "other"
        with pytest.raises(RuntimeError, match="lacks matching cancellation proof"):
            reconcile(ctx, central_record, self._cancelled(job_id))


class TestCentralQueuePolling:
    def test_lookup_handles_404_errors_and_malformed_bodies(self, tmp_path: Path) -> None:
        ctx, _, central_record, job_id = _central_fixture(tmp_path)
        lookup = checks_central_queue._get_central_queue_job
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        assert lookup(ctx, central_record) is None
        assert ctx.aws_client.make_authenticated_request.call_args.kwargs["path"] == (
            f"/api/v1/queue/jobs/{job_id}"
        )
        ctx.aws_client.make_authenticated_request.return_value = _response(500, text="x")
        with pytest.raises(RuntimeError, match="Central queue lookup failed: 500 x"):
            lookup(ctx, central_record)
        ctx.aws_client.make_authenticated_request.return_value = _response(200, {"job": []})
        with pytest.raises(RuntimeError, match="omitted job"):
            lookup(ctx, central_record)
        job = _persisted(job_id, status="queued")
        ctx.aws_client.make_authenticated_request.return_value = _response(200, {"job": job})
        assert lookup(ctx, central_record) == job

    def test_appearance_wait_sets_deadline_and_bounds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, _, central_record, job_id = _central_fixture(tmp_path)
        clock = _install_clock(monkeypatch, checks_central_queue, checks_jobs, models_module)
        wait = checks_central_queue._wait_for_central_queue_appearance
        job = _persisted(job_id, status="queued")
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(404),
            _response(200, {"job": job}),
        ]
        assert wait(ctx, central_record) == job
        assert central_record["appearance_deadline"] == 1_000.0 + 900

        central_record["appearance_deadline"] = clock.now - 1
        ctx.aws_client.make_authenticated_request.side_effect = None
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        with pytest.raises(TimeoutError, match="did not appear before the bounded"):
            wait(ctx, central_record)
        assert wait(ctx, central_record, raise_on_timeout=False) is None

    def test_terminal_wait(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx, _, central_record, job_id = _central_fixture(tmp_path)
        _install_clock(monkeypatch, checks_central_queue, checks_jobs, models_module)
        wait = checks_central_queue._wait_for_central_queue_terminal
        ctx.aws_client.make_authenticated_request.side_effect = [
            _response(200, {"job": _persisted(job_id, status="running")}),
            _response(200, {"job": _persisted(job_id)}),
        ]
        job, history = wait(ctx, central_record)
        assert job["status"] == "succeeded"
        assert [item["status"] for item in history] == ["running", "succeeded"]

        ctx.aws_client.make_authenticated_request.side_effect = None
        ctx.aws_client.make_authenticated_request.return_value = _response(404)
        with pytest.raises(RuntimeError, match="disappeared after observation"):
            wait(ctx, central_record)

        _install_clock(monkeypatch, checks_central_queue, checks_jobs, models_module, step=5_000.0)
        ctx.aws_client.make_authenticated_request.return_value = _response(
            200, {"job": _persisted(job_id, status="running")}
        )
        with pytest.raises(TimeoutError, match="did not reach a terminal status"):
            wait(ctx, central_record)


# ----------------------------------------------------------------------- topology


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _topology_environment(regions: tuple[str, ...] = ("us-east-1",)) -> SimpleNamespace:
    return _TopologyFixtures()._environment(regions)


class TestTopologyPureValidators:
    def test_bounded_evidence_handles_absent_unserializable_and_long_values(self) -> None:
        bounded = checks_topology._bounded_topology_evidence
        assert bounded(None) == "<absent>"
        assert bounded("plain") == "plain"
        assert bounded({"b": 1, "a": [2]}) == '{"a": [2], "b": 1}'
        mixed = {1, "a"}
        assert bounded(mixed) == str(mixed)
        long_text = bounded("x" * 3000, limit=40)
        assert len(long_text) == 40
        assert long_text.endswith("... [truncated]")

    def test_json_object_decoder_rejects_non_objects_and_non_canonical(self) -> None:
        decode = checks_topology._topology_json_object
        with pytest.raises(RuntimeError, match="not a non-empty JSON string"):
            decode("", "thing", canonical=False)
        with pytest.raises(RuntimeError, match="invalid JSON: non-standard JSON constant NaN"):
            decode("NaN", "thing", canonical=False)
        with pytest.raises(RuntimeError, match="invalid JSON"):
            decode("{", "thing", canonical=False)
        with pytest.raises(RuntimeError, match="must be a JSON object"):
            decode("[1]", "thing", canonical=False)
        with pytest.raises(RuntimeError, match="not exact canonical JSON"):
            decode('{"b": 1}', "thing", canonical=True)
        assert decode('{"b":1}', "thing", canonical=True) == {"b": 1}
        assert decode('{"b": 1}', "thing", canonical=False) == {"b": 1}

    def test_replay_input_decoding_errors(self) -> None:
        decode = checks_topology._decode_replay_input_parameter
        with pytest.raises(RuntimeError, match="not zlib\\+base64 replay input"):
            decode("not base64!", "param")
        with pytest.raises(RuntimeError, match="not zlib\\+base64 replay input"):
            decode(base64.b64encode(b"raw").decode("ascii"), "param")
        with pytest.raises(RuntimeError, match="not zlib\\+base64 replay input"):
            decode(base64.b64encode(zlib.compress(b"\xff\xfe")).decode("ascii"), "param")
        encoded = base64.b64encode(zlib.compress(b'{"a":1}')).decode("ascii")
        assert decode(encoded, "param") == '{"a":1}'

    def test_ssm_string_parameter_rejects_malformed_responses(self) -> None:
        read = checks_topology._ssm_string_parameter
        client = MagicMock()
        client.get_parameter.return_value = "nope"
        with pytest.raises(RuntimeError, match="response is malformed"):
            read(client, "/p")
        client.get_parameter.return_value = {"Parameter": {"Name": "/other", "Type": "String"}}
        with pytest.raises(RuntimeError, match="identity/type is invalid"):
            read(client, "/p")
        client.get_parameter.return_value = {"Parameter": {"Name": "/p", "Type": "String"}}
        with pytest.raises(RuntimeError, match="has no String value"):
            read(client, "/p")
        client.get_parameter.return_value = {
            "Parameter": {"Name": "/p", "Type": "String", "Value": "v"}
        }
        assert read(client, "/p") == "v"

    def test_epoch_seconds_normalization(self) -> None:
        epoch = checks_topology._epoch_seconds
        moment = datetime(2026, 7, 18, tzinfo=UTC)
        assert epoch(moment, "t") == int(moment.timestamp())
        assert epoch(12.9, "t") == 12
        with pytest.raises(RuntimeError, match="is not a timestamp"):
            epoch(True, "t")
        with pytest.raises(RuntimeError, match="is not a timestamp"):
            epoch("12", "t")
        with pytest.raises(RuntimeError, match="not a finite timestamp"):
            epoch(float("inf"), "t")
        with pytest.raises(RuntimeError, match="not a finite timestamp"):
            epoch(float("nan"), "t")
        with pytest.raises(RuntimeError, match="must be positive"):
            epoch(0, "t")

    def test_addon_arn_validation(self) -> None:
        ctx = _context()
        ctx.session.get_partition_for_region.return_value = "aws"
        good = {
            "stack_id": (
                "arn:aws:cloudformation:us-east-1:123456789012:stack/gco-live-us-east-1/abc"
            ),
            "state_machine_arn": "arn:aws:states:us-east-1:123456789012:stateMachine:sm",
            "execution_arn": "arn:aws:states:us-east-1:123456789012:execution:sm:run-1",
        }

        def validate(**overrides: str) -> None:
            checks_topology._validate_addon_arns(
                ctx,
                region="us-east-1",
                stack_name="gco-live-us-east-1",
                **{**good, **overrides},
            )

        validate()
        with pytest.raises(RuntimeError, match="wrong ARN identity"):
            validate(stack_id="arn:aws:cloudformation:us-east-1:999999999999:stack/x/y")
        with pytest.raises(RuntimeError, match="wrong account/partition/Region"):
            validate(state_machine_arn="arn:aws:states:eu-west-1:123456789012:stateMachine:sm")
        with pytest.raises(RuntimeError, match="is not an execution of"):
            validate(execution_arn="arn:aws:states:us-east-1:123456789012:execution:other:run-1")
        ctx.session.get_partition_for_region.return_value = ""
        with pytest.raises(RuntimeError, match="Could not resolve AWS partition"):
            validate()

    def test_state_machine_stack_resource_requires_healthy_identified_resource(self) -> None:
        ctx = _context()
        arn = "arn:aws:states:us-east-1:123456789012:stateMachine:sm"
        paginator = MagicMock()
        cloudformation = MagicMock()
        cloudformation.get_paginator.return_value = paginator
        ctx.session.client.return_value = cloudformation

        def lookup(summary: dict[str, Any]) -> dict[str, str]:
            paginator.paginate.return_value = [{"StackResourceSummaries": [summary]}]
            return checks_topology._state_machine_stack_resource(
                ctx, region="us-east-1", stack_id="stack-id", state_machine_arn=arn
            )

        base = {
            "ResourceType": "AWS::StepFunctions::StateMachine",
            "PhysicalResourceId": arn,
            "LogicalResourceId": "Machine",
            "ResourceStatus": "UPDATE_COMPLETE",
        }
        assert lookup(base)["status"] == "UPDATE_COMPLETE"
        with pytest.raises(RuntimeError, match="lacks a logical ID"):
            lookup({**base, "LogicalResourceId": ""})
        with pytest.raises(RuntimeError, match="is not complete: UPDATE_IN_PROGRESS"):
            lookup({**base, "ResourceStatus": "UPDATE_IN_PROGRESS"})

    def test_terminal_validator_contract(self) -> None:
        validate = checks_topology._validate_terminal_validator
        good = {
            "status": "validated",
            "DeploymentToken": "tok",
            "ExpectedCount": 3,
            "ValidatedCount": 3,
        }
        pairs = (("ExpectedCount", "ValidatedCount"),)

        def run(validator: Any) -> dict[str, Any]:
            return validate({"v": validator}, key="v", deployment_token="tok", count_pairs=pairs)

        assert run(good) == good
        with pytest.raises(RuntimeError, match="lacks object v"):
            run("nope")
        with pytest.raises(RuntimeError, match="not exactly 'validated'"):
            run({**good, "status": "pending"})
        with pytest.raises(RuntimeError, match="stale deployment token"):
            run({**good, "DeploymentToken": "old"})
        with pytest.raises(RuntimeError, match="invalid counts"):
            run({**good, "ExpectedCount": True})
        with pytest.raises(RuntimeError, match="invalid counts"):
            run({**good, "ValidatedCount": -1})
        with pytest.raises(RuntimeError, match="did not validate every item"):
            run({**good, "ValidatedCount": 2})

    def test_execution_input_schema(self) -> None:
        good = {
            "ClusterName": "gco-live-us-east-1",
            "Region": "us-east-1",
            "RegistryRegion": "us-east-1",
            "ProjectName": "gco-live",
            "EnabledCharts": ["keda"],
            "Charts": {"keda": {}},
            "KedaOperatorRoleArn": None,
            "ImageReplacements": {},
            "DeploymentToken": "tok",
        }

        def validate(**overrides: Any) -> None:
            value = {**good, **overrides}
            for key, item in list(value.items()):
                if item == "__drop__":
                    value.pop(key)
            checks_topology._validate_addon_execution_input(
                value,
                cluster_name="gco-live-us-east-1",
                region="us-east-1",
                registry_region="us-east-1",
                project_name="gco-live",
                deployment_token="tok",
            )

        validate()
        validate(EndpointGroupArn="arn:aws:globalaccelerator::1:accelerator/a")
        cases = [
            ({"Extra": 1}, "exact current schema"),
            ({"Charts": "__drop__"}, "exact current schema"),
            ({"ClusterName": "other"}, "stale cluster name"),
            ({"Region": "eu-west-1"}, "stale Region"),
            ({"RegistryRegion": "eu-west-1"}, "stale registry Region"),
            ({"ProjectName": "other"}, "stale project name"),
            ({"DeploymentToken": "old"}, "stale deployment token"),
            ({"EnabledCharts": ["keda", ""]}, "EnabledCharts must be a string list"),
            ({"EnabledCharts": "keda"}, "EnabledCharts must be a string list"),
            ({"Charts": []}, "Charts must be an object"),
            ({"ImageReplacements": []}, "ImageReplacements must be an object"),
            ({"KedaOperatorRoleArn": 5}, "KedaOperatorRoleArn must be a string or null"),
            ({"EndpointGroupArn": ""}, "EndpointGroupArn must be non-empty"),
        ]
        for overrides, message in cases:
            with pytest.raises(RuntimeError, match=message):
                validate(**overrides)


class TestPollAddonExecution:
    def _poll(
        self,
        environment: SimpleNamespace,
        responses: list[Any],
        *,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        region = "us-east-1"
        stepfunctions = environment.clients[("stepfunctions", region)]
        stepfunctions.describe_execution.side_effect = responses
        return checks_topology._poll_addon_execution(
            environment.ctx,
            region=region,
            execution=environment.metadata[region],
            input_json=_canonical(environment.inputs[region]),
            evidence=evidence if evidence is not None else {},
        )

    def _terminal(self, environment: SimpleNamespace, **overrides: Any) -> dict[str, Any]:
        region = "us-east-1"
        stepfunctions = environment.clients[("stepfunctions", region)]
        return {**stepfunctions.describe_execution.return_value, **overrides}

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"__replace__": "text"}, "malformed response"),
            ({"executionArn": "arn:other"}, "different execution"),
            ({"stateMachineArn": "arn:other"}, "different state machine"),
            ({"input": "{}"}, "stale execution input"),
            ({"startDate": 1}, "start time changed"),
            ({"status": None}, "returned no status"),
            ({"status": "PENDING_REDRIVE"}, "unknown status PENDING_REDRIVE"),
        ],
    )
    def test_describe_execution_identity_guards(
        self, overrides: dict[str, Any], message: str
    ) -> None:
        environment = _topology_environment()
        if "__replace__" in overrides:
            response: Any = overrides["__replace__"]
        else:
            response = self._terminal(environment, **overrides)
        with pytest.raises(RuntimeError, match=message):
            self._poll(environment, [response])

    def test_running_execution_is_polled_to_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        environment = _topology_environment()
        environment.ctx.settings.poll_interval_seconds = 3
        clock = _install_clock(monkeypatch, checks_topology)
        running = self._terminal(environment, status="RUNNING")
        evidence: dict[str, Any] = {}
        terminal = self._poll(
            environment, [running, self._terminal(environment)], evidence=evidence
        )
        assert terminal["status"] == "SUCCEEDED"
        assert [item["status"] for item in evidence["observations"]] == ["RUNNING", "SUCCEEDED"]
        assert evidence["execution_status"] == "SUCCEEDED"
        assert clock.sleeps == [3.0]

    def test_running_execution_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        environment = _topology_environment()
        clock = _install_clock(monkeypatch, checks_topology, step=60.0)
        running = self._terminal(environment, status="RUNNING")
        with pytest.raises(RuntimeError, match="did not finish within"):
            self._poll(environment, [running] * 200)
        assert clock.sleeps
        assert all(sleep == 0.1 for sleep in clock.sleeps)


class TestConvergeRegionAddons:
    def _converge(self, environment: SimpleNamespace, stack: dict[str, Any] | None = None) -> None:
        region = "us-east-1"
        stack_name = environment.stack_names[region]
        checks_topology._converge_region_addons(
            environment.ctx,
            region=region,
            stack_name=stack_name,
            stack=stack if stack is not None else environment.stacks[stack_name],
            evidence={},
        )

    def test_stack_output_guards(self) -> None:
        environment = _topology_environment()
        stack = environment.stacks["gco-live-us-east-1"]
        with pytest.raises(RuntimeError, match="malformed outputs"):
            self._converge(environment, {**stack, "outputs": None})
        outputs = dict(stack["outputs"])
        with pytest.raises(RuntimeError, match="stale ClusterName output"):
            self._converge(environment, {**stack, "outputs": {**outputs, "ClusterName": "x"}})
        with pytest.raises(RuntimeError, match="no AddonDeploymentToken output"):
            self._converge(
                environment, {**stack, "outputs": {**outputs, "AddonDeploymentToken": ""}}
            )

    def _set_execution(self, environment: SimpleNamespace, metadata: dict[str, Any]) -> None:
        environment.parameter_values["/gco-live/addons/us-east-1/_execution"] = _canonical(metadata)

    def test_execution_parameter_guards(self) -> None:
        environment = _topology_environment()
        metadata = environment.metadata["us-east-1"]
        self._set_execution(environment, {**metadata, "unexpected": 1})
        with pytest.raises(RuntimeError, match="unexpected schema"):
            self._converge(environment)
        self._set_execution(environment, {**metadata, "execution_arn": ""})
        with pytest.raises(RuntimeError, match="invalid execution_arn"):
            self._converge(environment)
        self._set_execution(environment, {**metadata, "started_at": True})
        with pytest.raises(RuntimeError, match="invalid started_at"):
            self._converge(environment)
        self._set_execution(environment, {**metadata, "region": "eu-west-1"})
        with pytest.raises(RuntimeError, match="stale regional identity"):
            self._converge(environment)

    def test_exact_environment_converges(self, monkeypatch: pytest.MonkeyPatch) -> None:
        environment = _topology_environment()
        _install_clock(monkeypatch, checks_topology)
        self._converge(environment)
        assert "sfn:us-east-1" in environment.events


class TestHealthAndMetricsPayloads:
    @staticmethod
    def _ctx() -> Any:
        ctx = _context()
        ctx.deployment_regions = ("us-east-1",)
        return ctx

    def _health(self, **overrides: Any) -> dict[str, Any]:
        return {
            "status": "healthy",
            "timestamp": "2026-07-18T00:00:00Z",
            "region": "us-east-1",
            "cluster_id": "gco-live-us-east-1",
            **overrides,
        }

    def test_health_payload_guards(self) -> None:
        ctx = self._ctx()
        validate = checks_topology._validate_health_payload
        assert validate(ctx, self._health(), endpoint_region="us-east-1") == self._health()
        with pytest.raises(RuntimeError, match="not a JSON object"):
            validate(ctx, "healthy", endpoint_region=None)
        with pytest.raises(RuntimeError, match="not exactly 'healthy'"):
            validate(ctx, self._health(status="degraded"), endpoint_region=None)
        with pytest.raises(RuntimeError, match="not an ISO date-time"):
            validate(ctx, self._health(timestamp="yesterday"), endpoint_region=None)
        with pytest.raises(RuntimeError, match="not an ISO date-time"):
            validate(ctx, self._health(timestamp="2026-13-40T99:00:00Z"), endpoint_region=None)
        with pytest.raises(RuntimeError, match="Region is not deployed: 'eu-west-1'"):
            validate(ctx, self._health(region="eu-west-1"), endpoint_region=None)
        with pytest.raises(RuntimeError, match="came from 'us-east-1', expected 'us-west-2'"):
            validate(ctx, self._health(), endpoint_region="us-west-2")
        with pytest.raises(RuntimeError, match="cluster_id is not 'gco-live-us-east-1'"):
            validate(ctx, self._health(cluster_id="other"), endpoint_region=None)

    def _metrics(self, **overrides: Any) -> dict[str, Any]:
        return {
            "region": "us-east-1",
            "cluster_id": "gco-live-us-east-1",
            "resource_utilization": {"cpu_percent": 1, "memory_percent": 2.5, "gpu_percent": 0},
            "thresholds": {"cpu_threshold": 80},
            "active_jobs": 0,
            **overrides,
        }

    def test_metrics_payload_guards(self) -> None:
        ctx = self._ctx()
        validate = checks_topology._validate_metrics_payload
        assert validate(ctx, self._metrics(), endpoint_region="us-east-1") == self._metrics()
        with pytest.raises(RuntimeError, match="not a JSON object"):
            validate(ctx, [], endpoint_region=None)
        with pytest.raises(RuntimeError, match="Region is not deployed: None"):
            validate(ctx, self._metrics(region=None), endpoint_region=None)
        with pytest.raises(RuntimeError, match="came from 'us-east-1', expected 'us-west-2'"):
            validate(ctx, self._metrics(), endpoint_region="us-west-2")
        with pytest.raises(RuntimeError, match="cluster_id is not"):
            validate(ctx, self._metrics(cluster_id="x"), endpoint_region=None)
        with pytest.raises(RuntimeError, match="no resource_utilization object"):
            validate(ctx, self._metrics(resource_utilization=None), endpoint_region=None)
        bad_utilization = {"cpu_percent": -1, "memory_percent": 0, "gpu_percent": 0}
        with pytest.raises(RuntimeError, match="cpu_percent is not a non-negative number: -1"):
            validate(ctx, self._metrics(resource_utilization=bad_utilization), endpoint_region=None)
        with pytest.raises(RuntimeError, match="no thresholds object"):
            validate(ctx, self._metrics(thresholds=[]), endpoint_region=None)
        with pytest.raises(RuntimeError, match="active_jobs is not a non-negative integer: True"):
            validate(ctx, self._metrics(active_jobs=True), endpoint_region=None)


def _warmup_sample(**overrides: Any) -> dict[str, Any]:
    sample = {
        "scope": "global",
        "region": None,
        "endpoint": "https://global.example.test",
        "attempt": 1,
        "timestamp": "2026-07-18T00:00:00+00:00",
        "latency_seconds": 0.1,
        "payload": None,
        "error": "ConnectTimeout: slow",
        "status_code": None,
        "retryable": True,
    }
    sample.update(overrides)
    return sample


class TestHealthWarmupCheckpoint:
    URLS = {"global_url": "https://global.example.test", "regional_urls": {}}

    def _run(self, environment: SimpleNamespace) -> list[dict[str, Any]]:
        return checks_topology._health_warmup_samples(environment.ctx, **self.URLS)

    def _seed(self, environment: SimpleNamespace, *samples: dict[str, Any]) -> None:
        environment.ctx.checkpoint.state["topology_health_warmup_samples"] = list(samples)

    def test_malformed_checkpoint_shapes_are_refused(self) -> None:
        environment = _topology_environment()
        environment.ctx.checkpoint.state["topology_health_warmup_samples"] = "corrupt"
        with pytest.raises(RuntimeError, match="checkpoint is malformed"):
            self._run(environment)

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"latency_seconds": -1}, "outcome is malformed"),
            ({"retryable": "yes"}, "outcome is malformed"),
            ({"status_code": True}, "outcome is malformed"),
            ({"timestamp": ""}, "outcome is malformed"),
            ({"error": None, "payload": {}, "status_code": 500}, "success is malformed"),
            ({"error": None, "payload": {}, "status_code": 200}, "success is malformed"),
            (
                {"error": None, "payload": None, "status_code": 200, "retryable": False},
                "success is malformed",
            ),
            ({"error": ""}, "failure is malformed"),
            ({"error": "boom", "payload": {"x": 1}}, "failure is malformed"),
            ({"scope": "regional", "region": "us-east-1"}, "identity changed"),
            ({"endpoint": "https://other.example.test"}, "identity changed"),
            ({"attempt": 2}, "ordering is invalid"),
        ],
    )
    def test_malformed_samples_are_refused(self, overrides: dict[str, Any], message: str) -> None:
        environment = _topology_environment()
        self._seed(environment, _warmup_sample(**overrides))
        with pytest.raises(RuntimeError, match=message):
            self._run(environment)

    def test_incomplete_sample_is_refused(self) -> None:
        environment = _topology_environment()
        sample = _warmup_sample()
        sample.pop("retryable")
        self._seed(environment, sample)
        with pytest.raises(RuntimeError, match="outcome is incomplete"):
            self._run(environment)

    def test_success_must_be_the_single_final_sample(self) -> None:
        environment = _topology_environment()
        success = _warmup_sample(
            error=None,
            payload={"status": "healthy"},
            status_code=200,
            retryable=False,
        )
        self._seed(environment, success, _warmup_sample(attempt=2))
        with pytest.raises(RuntimeError, match="success is inconsistent"):
            self._run(environment)

    def test_prior_success_skips_the_probe(self) -> None:
        environment = _topology_environment()
        success = _warmup_sample(
            error=None,
            payload={"status": "healthy"},
            status_code=200,
            retryable=False,
        )
        self._seed(environment, success)
        samples = self._run(environment)
        assert samples == [success]
        environment.ctx.aws_client.call_api.assert_not_called()

    def test_prior_non_retryable_failure_is_final(self) -> None:
        environment = _topology_environment()
        self._seed(environment, _warmup_sample(error="RuntimeError: 401", retryable=False))
        with pytest.raises(RuntimeError, match="previously failed .*401"):
            self._run(environment)

    def test_exhausted_budget_is_final(self) -> None:
        environment = _topology_environment()
        self._seed(
            environment,
            _warmup_sample(attempt=1),
            _warmup_sample(attempt=2),
            _warmup_sample(attempt=3),
        )
        with pytest.raises(RuntimeError, match="attempt budget is exhausted"):
            self._run(environment)

    def test_retryable_failure_sleeps_between_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        environment = _topology_environment()
        environment.ctx.settings.poll_interval_seconds = 2
        clock = _install_clock(monkeypatch, checks_topology)
        healthy = {
            "status": "healthy",
            "timestamp": "2026-07-18T00:00:00+00:00",
            "region": "us-east-1",
            "cluster_id": "gco-live-us-east-1",
        }
        environment.ctx.aws_client.call_api.side_effect = [
            ConnectionError("reset"),
            RuntimeError("API request failed: 503 Service Unavailable"),
            healthy,
        ]
        samples = self._run(environment)
        assert [sample["attempt"] for sample in samples] == [1, 2, 3]
        assert [sample["retryable"] for sample in samples] == [True, True, False]
        assert samples[-1]["payload"] == healthy
        assert clock.sleeps == [2.0, 2.0]

    def test_structured_status_code_decides_retryability(self) -> None:
        environment = _topology_environment()

        class ApiError(RuntimeError):
            status_code = 401

        environment.ctx.aws_client.call_api.side_effect = ApiError("denied")
        with pytest.raises(RuntimeError, match="warm-up call failed .* attempt 1: ApiError"):
            self._run(environment)
        sample = environment.ctx.checkpoint.state["topology_health_warmup_samples"][0]
        assert sample["status_code"] == 401
        assert sample["retryable"] is False

    def test_malformed_warmup_response_is_recorded(self) -> None:
        environment = _topology_environment()
        environment.ctx.aws_client.call_api.side_effect = None
        environment.ctx.aws_client.call_api.return_value = {"status": "degraded"}
        with pytest.raises(RuntimeError, match="Malformed health warm-up response"):
            self._run(environment)
        sample = environment.ctx.checkpoint.state["topology_health_warmup_samples"][0]
        assert sample["status_code"] == 200
        assert "not exactly 'healthy'" in sample["error"]


class TestHealthStabilityAndMetricsProbes:
    def test_stability_records_malformed_payload_before_raising(self) -> None:
        environment = _topology_environment()
        environment.ctx.aws_client.call_api.side_effect = None
        environment.ctx.aws_client.call_api.return_value = {"status": "healthy"}
        with pytest.raises(RuntimeError, match="Malformed health response .* round 1"):
            checks_topology._health_stability_samples(
                environment.ctx,
                global_url="https://global.example.test",
                regional_urls={},
            )
        samples = environment.ctx.checkpoint.state["topology_health_samples"]
        assert len(samples) == 1
        assert "ISO date-time" in samples[0]["error"]

    def test_stability_sleeps_between_rounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        environment = _topology_environment()
        environment.ctx.settings.poll_interval_seconds = 9
        clock = _install_clock(monkeypatch, checks_topology)
        samples = checks_topology._health_stability_samples(
            environment.ctx,
            global_url="https://global.example.test",
            regional_urls={"us-east-1": "https://us-east-1.example.test"},
        )
        assert len(samples) == 6
        assert clock.sleeps == [5.0, 5.0]

    def test_stability_call_failure_is_recorded(self) -> None:
        environment = _topology_environment()
        environment.ctx.aws_client.call_api.side_effect = TimeoutError("slow")
        with pytest.raises(RuntimeError, match="Health stability call failed"):
            checks_topology._health_stability_samples(
                environment.ctx,
                global_url="https://global.example.test",
                regional_urls={},
            )
        assert environment.ctx.checkpoint.state["topology_health_samples"][0]["payload"] is None

    def test_metrics_probe_records_failures_and_malformed_payloads(self) -> None:
        environment = _topology_environment()
        probe = checks_topology._metrics_reachability_samples
        urls = {"global_url": "https://global.example.test", "regional_urls": {}}
        environment.ctx.aws_client.call_api.side_effect = RuntimeError("API request failed: 404")
        with pytest.raises(RuntimeError, match="Metrics reachability call failed .* HTTPRoute"):
            probe(environment.ctx, **urls)
        assert environment.ctx.checkpoint.state["topology_metrics_samples"][0]["payload"] is None

        environment.ctx.aws_client.call_api.side_effect = None
        environment.ctx.aws_client.call_api.return_value = {"region": "us-east-1"}
        with pytest.raises(RuntimeError, match="Malformed metrics response"):
            probe(environment.ctx, **urls)
        assert (
            "cluster_id" in environment.ctx.checkpoint.state["topology_metrics_samples"][0]["error"]
        )

    def test_metrics_probe_succeeds_through_every_gateway(self) -> None:
        environment = _topology_environment()
        samples = checks_topology._metrics_reachability_samples(
            environment.ctx,
            global_url="https://global.example.test",
            regional_urls={"us-east-1": "https://us-east-1.example.test"},
        )
        assert [sample["scope"] for sample in samples] == ["global", "regional"]
        assert all(sample["error"] is None for sample in samples)


# ------------------------------------------------------------------------ alb_tls


class TestAlbTlsEvidence:
    REGION = "us-east-1"

    def _evidence(self, environment: SimpleNamespace) -> dict[str, Any]:
        return checks_alb_tls._alb_https_target_evidence(
            environment.ctx,
            region=self.REGION,
            cluster_name=f"gco-live-{self.REGION}",
        )

    def _elbv2(self, environment: SimpleNamespace) -> MagicMock:
        return environment.clients[("elbv2", self.REGION)]

    def test_ssm_parameter_reader_guards(self) -> None:
        read = checks_alb_tls._ssm_string_parameter
        client = MagicMock()
        client.get_parameter.return_value = []
        with pytest.raises(RuntimeError, match="response is malformed"):
            read(client, "/p")
        client.get_parameter.return_value = {"Parameter": {"Name": "/p", "Type": "SecureString"}}
        with pytest.raises(RuntimeError, match="identity/type is invalid"):
            read(client, "/p")
        client.get_parameter.return_value = {
            "Parameter": {"Name": "/p", "Type": "String", "Value": ""}
        }
        with pytest.raises(RuntimeError, match="no String value"):
            read(client, "/p")

    def test_happy_path_matches_the_action_level_evidence(self) -> None:
        environment = _topology_environment()
        evidence = self._evidence(environment)
        assert evidence["state"] == "active"
        assert len(evidence["target_groups"]) == 3

    def test_load_balancer_without_arn_is_refused(self) -> None:
        environment = _topology_environment()
        paginator = self._elbv2(environment).get_paginator("describe_load_balancers")
        paginator.paginate.return_value = [{"LoadBalancers": [{"Scheme": "internal"}]}]
        with pytest.raises(RuntimeError, match="load balancer without an ARN"):
            self._evidence(environment)

    def test_foreign_load_balancers_are_ignored_and_exactly_one_owner_is_required(self) -> None:
        environment = _topology_environment()
        paginator = self._elbv2(environment).get_paginator("describe_load_balancers")
        page = paginator.paginate.return_value[0]
        foreign = {**page["LoadBalancers"][0], "LoadBalancerArn": "arn:aws:elb:foreign"}
        paginator.paginate.return_value = [{"LoadBalancers": [foreign]}]
        with pytest.raises(
            RuntimeError, match="Expected exactly one owned GCO Gateway ALB .* found 0"
        ):
            self._evidence(environment)

    def test_load_balancer_type_scheme_and_state(self) -> None:
        environment = _topology_environment()
        paginator = self._elbv2(environment).get_paginator("describe_load_balancers")
        page = paginator.paginate.return_value[0]
        owned = page["LoadBalancers"][0]
        paginator.paginate.return_value = [
            {"LoadBalancers": [{**owned, "Scheme": "internet-facing"}]}
        ]
        with pytest.raises(RuntimeError, match="invalid type or scheme"):
            self._evidence(environment)
        paginator.paginate.return_value = [
            {"LoadBalancers": [{**owned, "State": {"Code": "provisioning"}}]}
        ]
        with pytest.raises(RuntimeError, match="is not active"):
            self._evidence(environment)

    def test_listener_count_and_arn(self) -> None:
        environment = _topology_environment()
        paginator = self._elbv2(environment).get_paginator("describe_listeners")
        listener = environment.listeners[self.REGION][0]
        paginator.paginate.return_value = [{"Listeners": [listener, listener]}]
        with pytest.raises(RuntimeError, match="has 2 listeners; expected exactly 1"):
            self._evidence(environment)
        paginator.paginate.return_value = [{"Listeners": [{**listener, "ListenerArn": ""}]}]
        with pytest.raises(RuntimeError, match="listener in us-east-1 has no ARN"):
            self._evidence(environment)

    def test_target_group_without_arn_is_refused(self) -> None:
        environment = _topology_environment()
        paginator = self._elbv2(environment).get_paginator("describe_target_groups")
        groups = environment.target_groups[self.REGION]
        paginator.paginate.return_value = [
            {"TargetGroups": [{**groups[0], "TargetGroupArn": ""}, *groups[1:]]}
        ]
        with pytest.raises(RuntimeError, match="target group without an ARN"):
            self._evidence(environment)

    def test_target_group_arn_lost_between_listing_and_polling_is_refused(self) -> None:
        environment = _topology_environment()
        groups = environment.target_groups[self.REGION]
        environment.ctx.persist.side_effect = lambda: [
            group.__setitem__("TargetGroupArn", "") for group in groups
        ]
        with pytest.raises(RuntimeError, match="target group without an ARN"):
            self._evidence(environment)

    def test_targets_that_never_become_healthy_time_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        environment = _topology_environment()
        clock = _install_clock(monkeypatch, checks_alb_tls, step=100.0)
        self._elbv2(environment).describe_target_health.return_value = {
            "TargetHealthDescriptions": [
                {
                    "Target": {"Id": "10.0.1.10", "Port": 8443},
                    "HealthCheckPort": "8443",
                    "TargetHealth": {"State": "initial", "Reason": "Elb.RegistrationInProgress"},
                }
            ]
        }
        with pytest.raises(RuntimeError, match="did not acquire healthy HTTPS targets"):
            self._evidence(environment)
        assert clock.sleeps
        group = environment.ctx.checkpoint.state["topology_alb_https_targets"][self.REGION][
            "target_groups"
        ][0]
        assert group["target_states"] == ["initial"]
        assert len(group["health_observations"]) == len(clock.sleeps) + 1


# ------------------------------------------------------------------------- policy


class TestPolicyGuards:
    def test_manifest_caps_must_be_an_object(self) -> None:
        payload = _policy_payload()
        payload["policy"]["manifest_caps"] = []
        with pytest.raises(RuntimeError, match="omitted manifest_caps"):
            checks_policy._validate_region_policy(_policy_ctx(payload), "us-east-1")

    def test_allowed_kinds_must_be_present(self) -> None:
        payload = _policy_payload()
        payload["policy"]["allowed_kinds"] = []
        with pytest.raises(RuntimeError, match="reported no allowed_kinds"):
            checks_policy._validate_region_policy(_policy_ctx(payload), "us-east-1")

    def test_trusted_registries_must_be_present(self) -> None:
        payload = _policy_payload()
        payload["policy"].pop("trusted_registries")
        with pytest.raises(RuntimeError, match="reported no trusted_registries"):
            checks_policy._validate_region_policy(_policy_ctx(payload), "us-east-1")

    def test_non_object_enforcement_layer_is_degraded(self) -> None:
        payload = _policy_payload()
        payload["cluster_enforcement"]["gco-jobs"] = "forbidden"
        with pytest.raises(RuntimeError, match="gco-jobs: not an object"):
            checks_policy._validate_region_policy(_policy_ctx(payload), "us-east-1")


# ----------------------------------------------------------------------- opencost


def _completed_attempt(number: int = 1, **overrides: Any) -> dict[str, Any]:
    attempt = {
        "attempt": number,
        "state": "completed",
        "started_at": "2026-07-26T12:00:00+00:00",
        "ended_at": "2026-07-26T12:00:01+00:00",
        "status_code": 201,
        "exact_bridge_timeout": False,
        "response_text": "",
        "retry_scheduled": False,
    }
    attempt.update(overrides)
    return attempt


def _started_attempt(number: int = 1, **overrides: Any) -> dict[str, Any]:
    attempt = {"attempt": number, "state": "started", "started_at": "2026-07-26T12:00:00+00:00"}
    attempt.update(overrides)
    return attempt


_TIMEOUT_TEXT = json.dumps(checks_opencost._EXACT_BRIDGE_TIMEOUT_BODY)


class TestOpenCostJournalValidation:
    def test_attempt_field_guards(self) -> None:
        validate = checks_opencost._validate_report_attempt
        with pytest.raises(RuntimeError, match="invalid ordering"):
            validate(_completed_attempt(2), 1, 1)
        with pytest.raises(RuntimeError, match="has invalid fields"):
            validate(_completed_attempt(state="pending"), 1, 1)
        with pytest.raises(RuntimeError, match="started-attempt checkpoint has invalid fields"):
            validate(_started_attempt(extra=True), 1, 1)
        with pytest.raises(RuntimeError, match="interior start"):
            validate(_started_attempt(), 1, 2)
        validate(_started_attempt(), 1, 1)
        with pytest.raises(RuntimeError, match="completed-attempt checkpoint has invalid fields"):
            validate(_completed_attempt(status_code=99), 1, 1)
        with pytest.raises(RuntimeError, match="completed-attempt checkpoint has invalid fields"):
            validate({**_completed_attempt(), "extra": 1}, 1, 1)
        with pytest.raises(RuntimeError, match="invalid timeout evidence"):
            validate(_completed_attempt(exact_bridge_timeout=True), 1, 1)
        with pytest.raises(RuntimeError, match="invalid retry transition"):
            validate(_completed_attempt(retry_scheduled=True), 1, 1)
        with pytest.raises(RuntimeError, match="unexpected response evidence"):
            validate(_completed_attempt(response_text="body"), 1, 1)
        validate(
            _completed_attempt(
                status_code=504,
                exact_bridge_timeout=True,
                response_text=_TIMEOUT_TEXT,
                retry_scheduled=True,
            ),
            1,
            2,
        )

    def test_journal_field_guards(self) -> None:
        ctx = _opencost_ctx()
        validate = checks_opencost._validated_report_journal
        with pytest.raises(RuntimeError, match="checkpoint has invalid fields"):
            validate(ctx, {"attempts": []}, "us-east-1")
        with pytest.raises(RuntimeError, match="checkpoint has invalid fields"):
            validate(
                ctx,
                {"attempts": [], "duplicate_possible": False, "completed_report": None},
                "us-east-1",
            )
        with pytest.raises(RuntimeError, match="invalid retry ancestry"):
            validate(
                ctx,
                {
                    "attempts": [
                        _completed_attempt(1, status_code=500, response_text="x"),
                        _completed_attempt(2),
                    ],
                    "duplicate_possible": False,
                    "completed_report": None,
                },
                "us-east-1",
            )
        with pytest.raises(RuntimeError, match="invalid duplicate evidence"):
            validate(
                ctx,
                {
                    "attempts": [_completed_attempt()],
                    "duplicate_possible": True,
                    "completed_report": None,
                },
                "us-east-1",
            )
        with pytest.raises(RuntimeError, match="no successful attempt"):
            validate(
                ctx,
                {
                    "attempts": [_completed_attempt(status_code=500, response_text="x")],
                    "duplicate_possible": False,
                    "completed_report": _opencost_completed_report(),
                },
                "us-east-1",
            )
        attempts, duplicate, report = validate(
            ctx,
            {
                "attempts": [_completed_attempt()],
                "duplicate_possible": False,
                "completed_report": _opencost_completed_report(),
            },
            "us-east-1",
        )
        assert (len(attempts), duplicate) == (1, False)
        assert report is not None and report["row_count"] == 4

    def test_journal_root_guards(self) -> None:
        ctx = _opencost_ctx()
        validate = checks_opencost._validated_report_journal_root
        with pytest.raises(RuntimeError, match="root is malformed"):
            validate(ctx, [])
        with pytest.raises(RuntimeError, match="root is malformed"):
            validate(ctx, {})
        assert validate(ctx, {}, allow_empty=True) == {}
        with pytest.raises(RuntimeError, match="unexpected Region 'eu-west-1'"):
            validate(ctx, {"eu-west-1": {}})
        with pytest.raises(RuntimeError, match="for us-east-1 is malformed"):
            validate(ctx, {"us-east-1": []})

    def test_region_guards_for_load_and_persist(self) -> None:
        ctx = _opencost_ctx()
        with pytest.raises(RuntimeError, match="unexpected Region 'eu-west-1'"):
            checks_opencost._load_report_journal(ctx, "eu-west-1")
        with pytest.raises(RuntimeError, match="unexpected Region 'eu-west-1'"):
            checks_opencost._persist_report_attempts(ctx, "eu-west-1", [], duplicate_possible=False)
        assert checks_opencost._load_report_journal(ctx, "us-east-1") == ([], False, None)


class TestOpenCostReportResume:
    def _seed(self, ctx: Any, *attempts: dict[str, Any], duplicate: bool = False) -> None:
        ctx.checkpoint.state["opencost_report_attempts"] = {
            "us-east-1": {
                "attempts": list(attempts),
                "duplicate_possible": duplicate,
                "completed_report": None,
            }
        }

    def test_in_flight_attempt_blocks_replay(self) -> None:
        ctx = _opencost_ctx()
        self._seed(ctx, _started_attempt())
        with pytest.raises(RuntimeError, match="ambiguous in-flight outcome"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")
        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_successful_http_without_report_blocks_replay(self) -> None:
        ctx = _opencost_ctx()
        self._seed(ctx, _completed_attempt())
        with pytest.raises(RuntimeError, match="successful HTTP outcome but no validated report"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

    def test_exhausted_retry_budget_blocks_replay(self) -> None:
        ctx = _opencost_ctx()
        self._seed(
            ctx,
            _completed_attempt(
                1,
                status_code=504,
                exact_bridge_timeout=True,
                response_text=_TIMEOUT_TEXT,
                retry_scheduled=True,
            ),
            _completed_attempt(2, status_code=500, response_text="upstream exploded"),
            duplicate=True,
        )
        with pytest.raises(RuntimeError, match="retry budget .* exhausted: 500 upstream exploded"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

    def test_non_timeout_failure_is_not_retryable(self) -> None:
        ctx = _opencost_ctx()
        self._seed(ctx, _completed_attempt(1, status_code=500, response_text="boom"))
        with pytest.raises(RuntimeError, match="not safely retryable: 500 boom"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")
        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_completed_report_is_returned_without_a_request(self) -> None:
        ctx = _opencost_ctx()
        ctx.checkpoint.state["opencost_report_attempts"] = {
            "us-east-1": {
                "attempts": [_completed_attempt()],
                "duplicate_possible": False,
                "completed_report": _opencost_completed_report(),
            }
        }
        report = checks_opencost._generate_validation_report(ctx, "us-east-1")
        assert report["row_count"] == 4
        assert report["duplicate_possible"] is False
        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_non_dict_report_body_is_refused(self) -> None:
        ctx = _opencost_ctx()
        payload = {**_opencost_report_payload(), "report": ["not", "a", "dict"]}
        ctx.aws_client.make_authenticated_request.return_value = _response(201, payload)
        with pytest.raises(RuntimeError, match="omitted its S3 key"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

    def test_verify_report_object_requires_a_deployed_region(self) -> None:
        ctx = _opencost_ctx()
        with pytest.raises(RuntimeError, match="invalid Region 'eu-west-1'"):
            checks_opencost._verify_report_object(
                ctx, {**_opencost_completed_report(), "region": "eu-west-1"}
            )


# ---------------------------------------------------------------------- inference


def _completed(command: list[str], returncode: int, stdout: str, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


class _ScriptedTable:
    """DynamoDB table fake: scripted first responses, then a sticky ``item``."""

    def __init__(self, *responses: Any, item: dict[str, Any] | None = None) -> None:
        self.responses = list(responses)
        self.item = item
        self.get_calls = 0

    def get_item(self, **kwargs: Any) -> Any:
        del kwargs
        self.get_calls += 1
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return {"Item": self.item} if self.item is not None else {}


class _FakeKubectl:
    """kubectl fake keyed on the resource token; heartbeats answer ``ok``."""

    def __init__(self, *, clock: _Clock | None = None, tick: float = 0.0) -> None:
        self.payloads: dict[str, Any] = {}
        self.calls: list[tuple[str, ...]] = []
        self.heartbeat: Any = (0, "ok\n", "")
        # Every kubectl round trip consumes ``tick`` seconds of the fake clock,
        # which is how a phase deadline can expire *during* a read.
        self.clock = clock
        self.tick = tick

    def __call__(self, *args: str, timeout: float | None = None) -> tuple[int, str, str]:
        del timeout
        self.calls.append(args)
        if self.clock is not None:
            self.clock.now += self.tick
        if args[0].startswith("--request-timeout"):
            if isinstance(self.heartbeat, BaseException):
                raise self.heartbeat
            return self.heartbeat() if callable(self.heartbeat) else self.heartbeat
        handler = self.payloads.get(args[1], {"items": []})
        if callable(handler):
            handler = handler(args)
        if isinstance(handler, BaseException):
            raise handler
        if isinstance(handler, tuple):
            return handler
        return 0, json.dumps(handler), ""


def _deployment_payload(name: str, replicas: int, *, ready: int | None = None) -> dict[str, Any]:
    ready_count = replicas if ready is None else ready
    return {
        "metadata": {"name": name, "generation": 1},
        "spec": {"replicas": replicas},
        "status": {
            "readyReplicas": ready_count,
            "availableReplicas": ready_count,
            "updatedReplicas": ready_count,
            "observedGeneration": 1,
        },
    }


def _pods_payload(name: str, count: int, *, phase: str = "Running") -> dict[str, Any]:
    return {
        "items": [
            {
                "metadata": {"name": f"{name}-{index}"},
                "status": {"phase": phase, "containerStatuses": [{"ready": True}]},
            }
            for index in range(count)
        ]
    }


def _hpa_payload(name: str, settings: Any, **overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": name},
        "minReplicas": settings.hpa_min_replicas,
        "maxReplicas": settings.hpa_max_replicas,
        "metrics": [
            {
                "type": "Resource",
                "resource": {
                    "name": "cpu",
                    "target": {
                        "type": "Utilization",
                        "averageUtilization": settings.hpa_cpu_target,
                    },
                },
            }
        ],
    }
    spec.update(overrides)
    return {"spec": spec}


class _FakeCli:
    """``subprocess.run`` stand-in for the real ``gco inference`` argv commands."""

    def __init__(self, runner: Any, plans: tuple[Any, ...], table: Any) -> None:
        self.runner = runner
        self.plans = plans
        self.table = table
        self.stages: list[str] = []
        self.overrides: dict[str, Any] = {}

    def __call__(self, command: list[str], **kwargs: Any) -> Any:
        assert kwargs["shell"] is False
        index = command.index("inference")
        stage = command[index + 1]
        plan = next(item for item in self.plans if item.name == command[index + 2])
        self.stages.append(f"{stage}:{plan.ordinal}")
        override = self.overrides.get(stage)
        if override is not None:
            result = override(plan, command)
            if isinstance(result, BaseException):
                raise result
            return result
        framework = plan.runtime.framework
        if stage == "deploy":
            self.table.item = _owned_item(self.runner.settings, plan, self.runner.owner_nonce)
            return _completed(command, 0, json.dumps({"endpoint_name": plan.name}))
        if stage == "health":
            return _completed(command, 0, json.dumps({"status": "healthy", "http_status": 200}))
        if stage == "models":
            payload = (
                {"data": [{"id": plan.runtime.model_id}]}
                if framework == "vllm"
                else {"model_id": plan.runtime.model_id, "model_sha": plan.runtime.model_revision}
            )
            return _completed(command, 0, "log line\n" + json.dumps(payload))
        if stage == "invoke":
            payload = (
                {"choices": [{"text": " generated "}]}
                if framework == "vllm"
                else {"generated_text": "generated"}
            )
            return _completed(command, 0, json.dumps(payload))
        assert stage == "delete"
        self.table.item = None
        return _completed(command, 0, "")


def _ready_kubectl(kubectl: _FakeKubectl, plans: tuple[Any, ...], settings: Any) -> None:
    """Make every plan's Deployment, pods, and HPA converge immediately."""
    by_name = {plan.name: plan for plan in plans}

    def replicas_for(plan: Any) -> int:
        return settings.hpa_min_replicas if plan.autoscaling else plan.replicas

    kubectl.payloads["deployment"] = lambda args: _deployment_payload(
        args[2], replicas_for(by_name[args[2]])
    )

    def pods(args: tuple[str, ...]) -> dict[str, Any]:
        if "--selector" not in args:
            return {"items": []}
        name = args[args.index("--selector") + 1].removeprefix("app=")
        return _pods_payload(name, replicas_for(by_name[name]))

    kubectl.payloads["pods"] = pods
    kubectl.payloads["horizontalpodautoscaler.autoscaling"] = lambda args: _hpa_payload(
        args[2], settings
    )


def _inference_clock(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> _Clock:
    return _install_clock(monkeypatch, lifecycle_module, runtime_module, inventory_module, **kwargs)


def _generous_settings(tmp_path: Path, **changes: Any) -> Any:
    return _settings(
        tmp_path,
        readiness_timeout_seconds=60,
        hpa_timeout_seconds=60,
        deletion_timeout_seconds=60,
        command_timeout_seconds=30,
        **changes,
    )


class TestInitializeRunStateResume:
    def _resume(self, tmp_path: Path, mutate: Any) -> None:
        settings = _settings(tmp_path)
        ctx = _inference_ctx(tmp_path)
        _, state = lifecycle_module.initialize_run_state(ctx, settings)
        replacement = json.loads(json.dumps(state))
        mutated = mutate(replacement)
        ctx.checkpoint.state[lifecycle_module._STATE_KEY] = (
            replacement if mutated is None else mutated
        )
        lifecycle_module.initialize_run_state(ctx, settings)

    def test_valid_checkpoint_resumes_identically(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        ctx = _inference_ctx(tmp_path)
        plans, state = lifecycle_module.initialize_run_state(ctx, settings)
        again, same_state = lifecycle_module.initialize_run_state(ctx, settings)
        assert again == plans
        assert same_state is state

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda state: "corrupt", "state is invalid"),
            (lambda state: state.update(plan=[]), "plan changed"),
            (lambda state: state.update(endpoints="x"), "endpoint checkpoint is invalid"),
            (
                lambda state: state.update(endpoints=state["endpoints"][:1]),
                "endpoint checkpoint is invalid",
            ),
            (
                lambda state: state.update(endpoints=["x"] * len(state["endpoints"])),
                "endpoint checkpoint is invalid",
            ),
            (lambda state: state["endpoints"][0].update(name="other"), "identity changed"),
            (lambda state: state["endpoints"][0].update(incarnation=0), "incarnation state"),
            (
                lambda state: state["endpoints"][1].update(closed_incarnations=None),
                "incarnation state",
            ),
        ],
        ids=[
            "not-dict",
            "plan",
            "endpoints-type",
            "endpoints-length",
            "record-type",
            "identity",
            "incarnation",
            "closed",
        ],
    )
    def test_resume_guards(self, tmp_path: Path, mutate: Any, message: str) -> None:
        with pytest.raises(ManagedInferenceValidationError, match=message):
            self._resume(tmp_path, mutate)


class TestResponseParsing:
    def test_last_json_document_skips_undecodable_openers(self) -> None:
        output = 'WARN {not json here\n{"a": 1}\n'
        assert lifecycle_module._last_json_document(output) == {"a": 1}

    def test_unknown_framework_is_refused(self) -> None:
        with pytest.raises(ManagedInferenceValidationError, match="unknown managed inference"):
            lifecycle_module.extract_generated_text('{"x": 1}', "triton")

    def test_vllm_list_payload_is_rejected(self) -> None:
        with pytest.raises(ManagedInferenceValidationError, match="schema"):
            lifecycle_module.extract_generated_text('[{"text": "x"}]', "vllm")


class TestLifecycleConstruction:
    def test_state_guards(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        ctx = _inference_ctx(tmp_path)
        plans, _ = lifecycle_module.initialize_run_state(ctx, settings)

        def build(state: dict[str, Any]) -> Any:
            return lifecycle_module.ManagedInferenceLifecycle(
                ctx=ctx,
                settings=settings,
                plans=plans,
                state=state,
                kubectl=_FakeKubectl(),
                kubeconfig_path=settings.kubeconfig_path,
            )

        with pytest.raises(ManagedInferenceValidationError, match="owner nonce is missing"):
            build({"owner_nonce": "", "endpoints": []})
        with pytest.raises(ManagedInferenceValidationError, match="endpoint state is invalid"):
            build({"owner_nonce": OWNER_NONCE, "endpoints": [1]})

    def test_record_failure_repairs_a_corrupt_failure_list(self, tmp_path: Path) -> None:
        runner, _, records, _ = _lifecycle(tmp_path)
        records[0]["failures"] = "corrupt"
        runner._record_failure(records[0], "deploy", RuntimeError("boom"))
        assert records[0]["failures"] == [{"stage": "deploy", "error": "RuntimeError: boom"}]
        assert records[0]["last_failed_phase"] == "deploy"

    def test_table_setup_failure_is_recorded(self, tmp_path: Path) -> None:
        runner, _, _, ctx = _lifecycle(tmp_path)

        def resource(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("no credentials")

        ctx.session = SimpleNamespace(resource=resource)
        with pytest.raises(
            ManagedInferenceValidationError, match="state store could not be opened"
        ):
            _ = runner.table
        assert runner.state["ddb_setup_error"] == "RuntimeError: no credentials"

    def test_strong_get_failures(self, tmp_path: Path) -> None:
        table = _ScriptedTable(RuntimeError("throttled"), {"Item": "not-a-dict"})
        runner, _, records, _ = _lifecycle(tmp_path, table=table)
        with pytest.raises(ManagedInferenceValidationError, match="strong state read failed"):
            runner._strong_get(records[0])
        assert records[0]["failures"][-1]["stage"] == "ddb-strong-read"
        with pytest.raises(ManagedInferenceValidationError, match="state record is malformed"):
            runner._strong_get(records[0])


class TestItemContract:
    def _runner(self, tmp_path: Path) -> tuple[Any, Any, Any, dict[str, Any]]:
        runner, plans, records, _ = _lifecycle(tmp_path)
        plan = plans[0]
        return runner, plan, records[0], _owned_item(runner.settings, plan, runner.owner_nonce)

    def test_lifecycle_identity_guards(self, tmp_path: Path) -> None:
        runner, plan, record, item = self._runner(tmp_path)
        with pytest.raises(ManagedInferenceValidationError, match="no immutable lifecycle"):
            runner._verify_item_contract(plan, {**item, "lifecycle_id": ""}, record)
        record["lifecycle_id"] = "different"
        with pytest.raises(ManagedInferenceValidationError, match="incarnation changed"):
            runner._verify_item_contract(plan, item, record)

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda item: item.update(spec="broken"), "stored spec is malformed"),
            (lambda item: item["spec"].update(port=1), "contract does not match this run"),
            (lambda item: item.update(target_regions=["eu-west-1"]), "target region"),
            (lambda item: item.update(namespace="other"), "namespace does not match"),
            (
                lambda item: item["spec"].update(autoscaling={"enabled": True}),
                "baseline unexpectedly has autoscaling",
            ),
        ],
        ids=["spec", "port", "region", "namespace", "baseline-autoscaling"],
    )
    def test_baseline_contract_guards(self, tmp_path: Path, mutate: Any, message: str) -> None:
        runner, plan, record, item = self._runner(tmp_path)
        mutate(item)
        with pytest.raises(ManagedInferenceValidationError, match=message):
            runner._verify_item_contract(plan, item, record)

    def test_hpa_contract(self, tmp_path: Path) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        plan, record = plans[1], records[1]
        assert plan.autoscaling
        item = _owned_item(runner.settings, plan, runner.owner_nonce)
        runner._verify_item_contract(plan, item, record)
        assert record["lifecycle_id"] == LIFECYCLE_ID
        item["spec"]["autoscaling"]["max_replicas"] = 9
        with pytest.raises(ManagedInferenceValidationError, match="HPA contract does not match"):
            runner._verify_item_contract(plan, item, record)


class TestRunCommandBoundary:
    def test_exhausted_deadline_is_recorded_before_launch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _, records, _ = _lifecycle(tmp_path)
        clock = _inference_clock(monkeypatch)
        launched = MagicMock()
        monkeypatch.setattr(lifecycle_module.subprocess, "run", launched)
        with pytest.raises(InferenceCommandFailure, match="health phase deadline exhausted"):
            runner._run_command(records[0], "health", ["gco"], deadline=clock.now - 1)
        launched.assert_not_called()
        assert records[0]["commands"][-1] == {
            "stage": "health",
            "argv": ["gco"],
            "deadline_exhausted": True,
        }

    @pytest.mark.parametrize("stage", ["invoke", "deploy"])
    def test_timeout_is_recorded_and_marks_invoke_ambiguous(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
    ) -> None:
        runner, _, records, _ = _lifecycle(tmp_path)
        record = records[0]
        record["invoke_journal"] = {"status": "started"}

        def timed_out(command: list[str], **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(
                command, kwargs["timeout"], output="partial", stderr="e"
            )

        monkeypatch.setattr(lifecycle_module.subprocess, "run", timed_out)
        with pytest.raises(InferenceCommandFailure, match=f"{stage} timed out"):
            runner._run_command(record, stage, ["gco"])
        entry = record["commands"][-1]
        assert entry["timed_out"] is True
        assert entry["timeout_seconds"] == 2.0
        assert entry["stdout"] == "partial"
        if stage == "invoke":
            assert record["invoke_journal"] == {
                "status": "ambiguous",
                "reason": "timeout",
                "stdout": "partial",
            }
        else:
            assert record["invoke_journal"] == {"status": "started"}

    @pytest.mark.parametrize("stage", ["invoke", "deploy"])
    def test_launch_error_is_recorded_and_marks_invoke_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
    ) -> None:
        runner, _, records, _ = _lifecycle(tmp_path)
        record = records[0]
        record["invoke_journal"] = {"status": "started"}

        def missing(command: list[str], **kwargs: Any) -> Any:
            raise FileNotFoundError("python vanished")

        monkeypatch.setattr(lifecycle_module.subprocess, "run", missing)
        with pytest.raises(InferenceCommandFailure, match=f"{stage} could not start"):
            runner._run_command(record, stage, ["gco"])
        assert record["commands"][-1]["launch_error"] == "FileNotFoundError: python vanished"
        expected_status = "failed" if stage == "invoke" else "started"
        assert record["invoke_journal"]["status"] == expected_status

    def test_nonzero_exit_marks_invoke_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _, records, _ = _lifecycle(tmp_path)
        record = records[0]
        record["invoke_journal"] = {"status": "started"}
        monkeypatch.setattr(
            lifecycle_module.subprocess,
            "run",
            lambda command, **kwargs: _completed(command, 3, "out", "err"),
        )
        with pytest.raises(InferenceCommandFailure, match="invoke exited nonzero"):
            runner._run_command(record, "invoke", ["gco"])
        assert record["invoke_journal"] == {
            "status": "failed",
            "returncode": 3,
            "stdout": "out",
            "stderr": "err",
        }

    def test_invoke_without_journal_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _, records, _ = _lifecycle(tmp_path)
        monkeypatch.setattr(
            lifecycle_module.subprocess,
            "run",
            lambda command, **kwargs: _completed(command, 0, "{}"),
        )
        with pytest.raises(ManagedInferenceValidationError, match="invoke journal is invalid"):
            runner._run_command(records[0], "invoke", ["gco"])


class TestEnsureOwnedEndpoint:
    def test_existing_owned_record_is_adopted_without_kubernetes(self, tmp_path: Path) -> None:
        kubectl = _FakeKubectl()
        runner, plans, records, _ = _lifecycle(tmp_path, kubectl=kubectl)
        runner.table.item = _owned_item(runner.settings, plans[0], runner.owner_nonce)
        runner.ensure_owned_endpoint(plans[0], records[0])
        assert records[0]["owned"] is True
        assert records[0]["phase"] == "ownership-confirmed"
        assert records[0]["lifecycle_id"] == LIFECYCLE_ID
        assert kubectl.calls == []

    def test_fresh_deploy_waits_for_the_owned_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kubectl = _FakeKubectl()
        runner, plans, records, _ = _lifecycle(tmp_path, kubectl=kubectl)
        _inference_clock(monkeypatch)
        cli = _FakeCli(runner, plans, runner.table)
        monkeypatch.setattr(lifecycle_module.subprocess, "run", cli)
        runner.ensure_owned_endpoint(plans[0], records[0])
        assert cli.stages == ["deploy:1"]
        assert records[0]["phase"] == "ownership-confirmed"
        assert records[0]["owned"] is True
        assert [call[1] for call in kubectl.calls] == [
            kind.resource for kind in inventory_module.KUBERNETES_INVENTORY_KINDS
        ]
        assert records[0]["commands"][-1]["stage"] == "deploy"
        assert "--no-rewrite-image" in records[0]["commands"][-1]["argv"]

    def test_failed_deploy_without_record_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        cli = _FakeCli(runner, plans, runner.table)
        cli.overrides["deploy"] = lambda plan, command: _completed(command, 1, "", "denied")
        monkeypatch.setattr(lifecycle_module.subprocess, "run", cli)
        with pytest.raises(ManagedInferenceValidationError, match="deploy failed"):
            runner.ensure_owned_endpoint(plans[0], records[0])
        assert records[0]["failures"][-1]["stage"] == "deploy"
        assert records[0]["phase"] == "deploy-started"

    def test_failed_deploy_with_owned_record_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        _inference_clock(monkeypatch)
        cli = _FakeCli(runner, plans, runner.table)

        def flaky_deploy(plan: Any, command: list[str]) -> Any:
            runner.table.item = _owned_item(runner.settings, plan, runner.owner_nonce)
            return _completed(command, 1, "", "client timeout after create")

        cli.overrides["deploy"] = flaky_deploy
        monkeypatch.setattr(lifecycle_module.subprocess, "run", cli)
        runner.ensure_owned_endpoint(plans[0], records[0])
        assert records[0]["phase"] == "ownership-confirmed"
        assert records[0]["failures"][-1]["stage"] == "deploy"


class TestHealthProbeGuards:
    def _prepared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **settings: Any
    ) -> tuple[Any, Any, dict[str, Any], _Clock]:
        runner, plans, records, _ = _lifecycle(
            tmp_path, settings=_settings(tmp_path, **settings) if settings else None
        )
        clock = _inference_clock(monkeypatch)
        runner.table.item = _owned_item(runner.settings, plans[0], runner.owner_nonce)
        return runner, plans[0], records[0], clock

    def test_corrupt_attempt_history_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, _ = self._prepared(tmp_path, monkeypatch)
        record["backend_probe_attempts"] = "corrupt"
        with pytest.raises(ManagedInferenceValidationError, match="probe history is invalid"):
            runner._wait_for_healthy_backend(plan, record)

    def test_ownership_loss_and_stopped_endpoint_are_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, _ = self._prepared(tmp_path, monkeypatch)
        runner.table.item = None
        with pytest.raises(
            ManagedInferenceValidationError, match="ownership changed during health"
        ):
            runner._wait_for_healthy_backend(plan, record)
        runner.table.item = {
            **_owned_item(runner.settings, plan, runner.owner_nonce),
            "desired_state": "stopped",
        }
        with pytest.raises(ManagedInferenceValidationError, match="stopped running during health"):
            runner._wait_for_healthy_backend(plan, record)

    def test_command_failure_is_classified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, _ = self._prepared(tmp_path, monkeypatch)
        monkeypatch.setattr(
            lifecycle_module.subprocess,
            "run",
            lambda command, **kwargs: _completed(command, 2, "", "unauthorized"),
        )
        with pytest.raises(ManagedInferenceValidationError, match="health/model probe failed"):
            runner.verify_backend_probes(plan, record)
        attempt = record["backend_probe_attempts"][-1]
        assert attempt["classification"] == "command-failed"
        assert attempt["error"] == "health exited nonzero"

    def test_malformed_contract_is_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, _ = self._prepared(tmp_path, monkeypatch)
        monkeypatch.setattr(
            lifecycle_module.subprocess,
            "run",
            lambda command, **kwargs: _completed(
                command, 0, json.dumps({"status": "healthy", "http_status": True})
            ),
        )
        with pytest.raises(ManagedInferenceValidationError, match="malformed contract"):
            runner._wait_for_healthy_backend(plan, record)
        assert record["backend_probe_attempts"][-1]["classification"] == "malformed-contract"

    def test_slow_retryable_probe_exhausts_the_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, clock = self._prepared(tmp_path, monkeypatch)

        def slow_unhealthy(command: list[str], **kwargs: Any) -> Any:
            clock.now += 5.0
            return _completed(command, 0, json.dumps({"status": "unhealthy", "http_status": 503}))

        monkeypatch.setattr(lifecycle_module.subprocess, "run", slow_unhealthy)
        with pytest.raises(
            ManagedInferenceValidationError, match="did not converge before timeout"
        ):
            runner._wait_for_healthy_backend(plan, record)
        assert record["backend_probe_attempts"][-1]["classification"] == "deadline-exhausted"

    def test_vllm_model_inventory_must_include_the_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        _inference_clock(monkeypatch)
        runner.table.item = _owned_item(runner.settings, plans[0], runner.owner_nonce)
        cli = _FakeCli(runner, plans, runner.table)
        cli.overrides["models"] = lambda plan, command: _completed(
            command, 0, json.dumps({"data": [{"id": "someone-else/model"}]})
        )
        monkeypatch.setattr(lifecycle_module.subprocess, "run", cli)
        with pytest.raises(ManagedInferenceValidationError, match="health/model probe failed"):
            runner.verify_backend_probes(plans[0], records[0])
        assert "omitted the configured model" in records[0]["failures"][-1]["error"]

    def test_tgi_info_must_match_model_and_revision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        _inference_clock(monkeypatch)
        tgi_plan, tgi_record = plans[2], records[2]
        assert tgi_plan.runtime.framework == "tgi"
        runner.table.item = _owned_item(runner.settings, tgi_plan, runner.owner_nonce)
        cli = _FakeCli(runner, plans, runner.table)
        cli.overrides["models"] = lambda plan, command: _completed(
            command, 0, json.dumps({"model_id": plan.runtime.model_id, "model_sha": "0" * 40})
        )
        monkeypatch.setattr(lifecycle_module.subprocess, "run", cli)
        with pytest.raises(ManagedInferenceValidationError, match="health/model probe failed"):
            runner.verify_backend_probes(tgi_plan, tgi_record)
        assert "exact model id and revision" in tgi_record["failures"][-1]["error"]


class TestInvokeJournal:
    def test_fresh_invocation_journals_then_records_evidence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        cli = _FakeCli(runner, plans, runner.table)
        monkeypatch.setattr(lifecycle_module.subprocess, "run", cli)
        runner.invoke(plans[0], records[0])
        journal = records[0]["invoke_journal"]
        assert journal["status"] == "succeeded"
        assert journal["framework"] == "vllm"
        assert journal["argv"] == lifecycle_module.build_invoke_command(runner.settings, plans[0])
        assert records[0]["invoke_evidence"] == {
            "framework": "vllm",
            "generated_text_non_empty": True,
            "generated_text_length": len("generated"),
            "replayed": False,
        }
        assert records[0]["phase"] == "invoked"
        assert cli.stages == ["invoke:1"]

    @pytest.mark.parametrize(
        ("journal", "message"),
        [
            ("corrupt", "invoke journal is invalid"),
            ({"framework": "tgi", "request_path": "/x", "argv": []}, "journal identity changed"),
            ("__valid_with_status__weird", "journal status is invalid"),
        ],
        ids=["type", "identity", "status"],
    )
    def test_persisted_journal_guards(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, journal: Any, message: str
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        plan, record = plans[0], records[0]
        if isinstance(journal, str) and journal.startswith("__valid_with_status__"):
            journal = {
                "status": journal.removeprefix("__valid_with_status__"),
                "framework": plan.runtime.framework,
                "request_path": plan.runtime.request_path,
                "argv": lifecycle_module.build_invoke_command(runner.settings, plan),
            }
        record["invoke_journal"] = journal
        monkeypatch.setattr(lifecycle_module.subprocess, "run", MagicMock())
        with pytest.raises(ManagedInferenceValidationError, match=message):
            runner.invoke(plan, record)
        lifecycle_module.subprocess.run.assert_not_called()

    def test_schema_failure_is_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        cli = _FakeCli(runner, plans, runner.table)
        cli.overrides["invoke"] = lambda plan, command: _completed(command, 0, '{"choices": []}')
        monkeypatch.setattr(lifecycle_module.subprocess, "run", cli)
        with pytest.raises(ManagedInferenceValidationError, match="failed its response contract"):
            runner.invoke(plans[0], records[0])
        assert records[0]["failures"][-1]["stage"] == "invoke"
        assert records[0]["invoke_journal"]["status"] == "succeeded"
        assert "invoke_evidence" not in records[0]


class TestIncarnationGuards:
    def _cleaned_record(self, record: dict[str, Any]) -> None:
        record.update(cleanup_phase="absent", lifecycle_id=LIFECYCLE_ID)

    def test_unstable_absence_blocks_rotation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        self._cleaned_record(records[0])
        monkeypatch.setattr(
            runner, "prove_absence", lambda record: {"stable_absence_observations": 1}
        )
        with pytest.raises(ManagedInferenceValidationError, match="did not retain stable absence"):
            runner._prepare_incarnation_for_resume(plans[0], records[0])

    def test_corrupt_closed_incarnations_block_rotation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        _inference_clock(monkeypatch)
        self._cleaned_record(records[0])
        records[0]["closed_incarnations"] = "corrupt"
        with pytest.raises(ManagedInferenceValidationError, match="closed-incarnation state"):
            runner._prepare_incarnation_for_resume(plans[0], records[0])


class TestCleanupEndpoint:
    def _runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: Any = None
    ) -> tuple[Any, Any, dict[str, Any], _FakeCli, _FakeKubectl, _Clock]:
        kubectl = _FakeKubectl()
        runner, plans, records, _ = _lifecycle(tmp_path, table=table, kubectl=kubectl)
        clock = _inference_clock(monkeypatch)
        cli = _FakeCli(runner, plans, runner.table)
        monkeypatch.setattr(lifecycle_module.subprocess, "run", cli)
        return runner, plans[0], records[0], cli, kubectl, clock

    def test_owned_running_endpoint_is_deleted_then_proven_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, cli, _, clock = self._runner(tmp_path, monkeypatch)
        record["cleanup_attempts"] = "corrupt"
        runner.table.item = _owned_item(runner.settings, plan, runner.owner_nonce)
        evidence = runner.cleanup_endpoint(plan, record)
        assert cli.stages == ["delete:1"]
        delete_argv = record["commands"][-1]["argv"]
        assert delete_argv[delete_argv.index("--expected-lifecycle-id") + 1] == LIFECYCLE_ID
        assert evidence["stable_absence_observations"] == 2
        attempt = record["cleanup_attempts"][-1]
        assert attempt["completed"] is True
        assert attempt["delete_recovered"] is False
        assert record["cleanup_phase"] == "absent"
        assert clock.sleeps == [1.0]

    def test_already_deleting_endpoint_skips_the_delete_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        table = _ScriptedTable()
        runner, plan, record, cli, _, _ = self._runner(tmp_path, monkeypatch, table=table)
        deleting = {
            **_owned_item(runner.settings, plan, runner.owner_nonce),
            "desired_state": "deleted",
        }
        table.responses.append({"Item": deleting})
        runner.cleanup_endpoint(plan, record)
        assert cli.stages == []
        assert record["cleanup_phase"] == "absent"

    def test_missing_lifecycle_identity_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, cli, _, _ = self._runner(tmp_path, monkeypatch)
        runner.table.item = _owned_item(runner.settings, plan, runner.owner_nonce)
        monkeypatch.setattr(runner, "_verify_item_contract", lambda *args: None)
        with pytest.raises(ManagedInferenceValidationError, match="no checkpointed lifecycle"):
            runner.cleanup_endpoint(plan, record)
        assert cli.stages == []
        assert record["cleanup_phase"] == "delete-requested"

    def test_failed_delete_refuses_a_replacement_race(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, cli, _, _ = self._runner(tmp_path, monkeypatch)
        runner.table.item = _owned_item(runner.settings, plan, runner.owner_nonce)

        def racing_delete(plan_: Any, command: list[str]) -> Any:
            runner.table.item = _owned_item(
                runner.settings, plan_, runner.owner_nonce, lifecycle_id="1" * 64
            )
            return _completed(command, 1, "", "ConditionalCheckFailedException")

        cli.overrides["delete"] = racing_delete
        with pytest.raises(ManagedInferenceValidationError, match="refused a replacement"):
            runner.cleanup_endpoint(plan, record)
        assert record["cleanup_attempts"][-1]["refused_replacement_race"] is True
        assert record["failures"][-1]["stage"] == "delete"

    def test_failed_delete_that_still_converges_is_recovered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, cli, _, _ = self._runner(tmp_path, monkeypatch)
        runner.table.item = _owned_item(runner.settings, plan, runner.owner_nonce)

        def flaky_delete(plan_: Any, command: list[str]) -> Any:
            runner.table.item = None
            return _completed(command, 1, "", "read timeout after delete")

        cli.overrides["delete"] = flaky_delete
        runner.cleanup_endpoint(plan, record)
        attempt = record["cleanup_attempts"][-1]
        assert attempt["completed"] is True
        assert attempt["delete_recovered"] is True

    def test_failed_delete_with_lingering_record_reports_both_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, cli, _, _ = self._runner(tmp_path, monkeypatch)
        runner.table.item = _owned_item(runner.settings, plan, runner.owner_nonce)
        cli.overrides["delete"] = lambda plan_, command: _completed(command, 1, "", "denied")
        with pytest.raises(ManagedInferenceValidationError, match="absence was not proven"):
            runner.cleanup_endpoint(plan, record)
        attempt = record["cleanup_attempts"][-1]
        assert attempt["completed"] is False
        assert attempt["error"].startswith("ManagedInferenceValidationError")
        assert attempt["delete_error"] == "InferenceCommandFailure: delete exited nonzero"

    def test_lingering_kubernetes_objects_fail_absence_without_a_delete_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, cli, kubectl, _ = self._runner(tmp_path, monkeypatch)
        kubectl.payloads["deployments.apps"] = {"items": [{"metadata": {"name": plan.name}}]}
        with pytest.raises(ManagedInferenceValidationError, match="absence was not proven"):
            runner.cleanup_endpoint(plan, record)
        attempt = record["cleanup_attempts"][-1]
        assert attempt["completed"] is False
        assert "delete_error" not in attempt
        assert cli.stages == []


class TestExecuteMatrix:
    def test_full_matrix_validates_every_endpoint_and_aggregates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _generous_settings(tmp_path)
        kubectl = _FakeKubectl()
        runner, plans, records, _ = _lifecycle(tmp_path, settings=settings, kubectl=kubectl)
        _ready_kubectl(kubectl, plans, settings)
        _inference_clock(monkeypatch)
        cli = _FakeCli(runner, plans, runner.table)
        monkeypatch.setattr(lifecycle_module.subprocess, "run", cli)

        evidence = runner.execute()

        assert evidence["endpoint_count"] == 4
        assert evidence["validated_or_resumed"] == 4
        assert evidence["newly_validated"] == 4
        assert evidence["all_endpoints_absent"] is True
        assert evidence["shared_proxy_autoscaling_verified"] is False
        assert evidence["invocations"]["completed"] == 4
        assert evidence["frameworks"] == {
            "vllm": {"baseline": True, "hpa": True, "invocations": 2, "model_info": 2},
            "tgi": {"baseline": True, "hpa": True, "invocations": 2, "model_info": 2},
        }
        assert cli.stages == [
            f"{stage}:{ordinal}"
            for ordinal in (1, 2, 3, 4)
            for stage in ("deploy", "health", "models", "invoke", "delete")
        ]
        assert [record["phase"] for record in records] == ["complete"] * 4
        assert records[1]["hpa_stability_observations"][-1]["ready"] == 2
        assert runner.state["phase"] == "complete"

    def test_completed_resume_only_reproves_absence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kubectl = _FakeKubectl()
        runner, _, records, _ = _lifecycle(tmp_path, kubectl=kubectl)
        _inference_clock(monkeypatch)
        launched = MagicMock()
        monkeypatch.setattr(lifecycle_module.subprocess, "run", launched)
        for record in records:
            record["validation_complete"] = True

        evidence = runner.execute()

        launched.assert_not_called()
        assert evidence["validated_or_resumed"] == 4
        assert evidence["newly_validated"] == 0
        assert evidence["invocations"]["completed"] == 0
        assert evidence["frameworks"] == {
            "vllm": {"baseline": True, "hpa": True, "invocations": 0, "model_info": 0},
            "tgi": {"baseline": True, "hpa": True, "invocations": 0, "model_info": 0},
        }
        assert all(record["absence_proven"] is True for record in records)
        inventory_reads = [call for call in kubectl.calls if call[0] == "get"]
        # Three absence proofs per endpoint (run, inner cleanup, final cleanup),
        # each two full sweeps of the inventory kinds.
        assert len(inventory_reads) == 4 * 3 * 2 * len(inventory_module.KUBERNETES_INVENTORY_KINDS)

    def test_cleanup_failure_after_a_successful_endpoint_fails_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _, records, _ = _lifecycle(tmp_path)
        monkeypatch.setattr(runner, "run_endpoint", lambda plan, record: True)

        def failing_cleanup(plan: Any, record: dict[str, Any]) -> dict[str, Any]:
            raise ManagedInferenceValidationError("kubectl unavailable")

        monkeypatch.setattr(runner, "cleanup_endpoint", failing_cleanup)
        with pytest.raises(ManagedInferenceValidationError, match="cleanup failures: 5"):
            runner.execute()
        assert records[0]["failures"][0] == {
            "stage": "endpoint-finally-cleanup",
            "error": "ManagedInferenceValidationError: kubectl unavailable",
        }
        assert records[0]["last_failed_phase"] == "aggregate-finally-cleanup"
        assert runner.state["phase"] == "failed"
        assert runner.state["cleanup_failure_count"] == 5


# --------------------------------------------------------------- inference_runtime


class TestTunnelHeartbeat:
    def test_expired_deadline_is_refused_before_kubectl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kubectl = _FakeKubectl()
        runner, _, records, _ = _lifecycle(tmp_path, kubectl=kubectl)
        clock = _inference_clock(monkeypatch)
        with pytest.raises(ManagedInferenceValidationError, match="heartbeat deadline expired"):
            runner.keep_cluster_tunnel_alive(records[0], float("-inf"), deadline=clock.now - 1)
        assert kubectl.calls == []

    def test_kubectl_launch_failure_is_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kubectl = _FakeKubectl()
        kubectl.heartbeat = OSError("tunnel closed")
        runner, _, records, _ = _lifecycle(tmp_path, kubectl=kubectl)
        _inference_clock(monkeypatch)
        with pytest.raises(ManagedInferenceValidationError, match="tunnel heartbeat failed"):
            runner.keep_cluster_tunnel_alive(records[0], float("-inf"))
        observation = records[0]["tunnel_heartbeats"][-1]
        assert observation["healthy"] is False
        assert observation["error"] == "OSError: tunnel closed"

    def test_corrupt_history_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _, records, _ = _lifecycle(tmp_path, kubectl=_FakeKubectl())
        _inference_clock(monkeypatch)
        records[0]["tunnel_heartbeats"] = "corrupt"
        with pytest.raises(ManagedInferenceValidationError, match="heartbeat history is invalid"):
            runner.keep_cluster_tunnel_alive(records[0], float("-inf"))


class TestDdbWaits:
    def test_owned_record_wait_refuses_collisions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path, kubectl=_FakeKubectl())
        _inference_clock(monkeypatch)
        runner.table.item = _owned_item(runner.settings, plans[0], "0" * 64)
        with pytest.raises(ManagedInferenceValidationError, match="collision detected"):
            runner._wait_for_owned_record(plans[0], records[0])

    def test_owned_record_wait_times_out_after_a_slow_heartbeat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kubectl = _FakeKubectl()
        runner, plans, records, _ = _lifecycle(tmp_path, kubectl=kubectl)
        clock = _inference_clock(monkeypatch)

        def slow_heartbeat() -> tuple[int, str, str]:
            clock.now += 10.0
            return 0, "ok", ""

        kubectl.heartbeat = slow_heartbeat
        with pytest.raises(ManagedInferenceValidationError, match="did not appear before timeout"):
            runner._wait_for_owned_record(plans[0], records[0])
        assert clock.sleeps == []
        assert records[0]["tunnel_heartbeats"][-1]["healthy"] is True

    def test_ddb_running_wait_polls_through_absence_and_refuses_foreign_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kubectl = _FakeKubectl()
        table = _ScriptedTable()
        runner, plans, records, _ = _lifecycle(tmp_path, table=table, kubectl=kubectl)
        clock = _inference_clock(monkeypatch)
        plan, record = plans[0], records[0]
        running = _owned_item(runner.settings, plan, runner.owner_nonce)
        table.responses.extend([{}, {"Item": running}])
        runner.wait_for_ddb_running(plan, record)
        assert record["phase"] == "ddb-running"
        assert clock.sleeps == [1.0]
        assert len(record["tunnel_heartbeats"]) == 1

        runner.table.item = _owned_item(runner.settings, plan, "0" * 64)
        with pytest.raises(
            ManagedInferenceValidationError, match="ownership changed while waiting"
        ):
            runner.wait_for_ddb_running(plan, record)

    def test_ddb_running_wait_times_out_after_a_slow_heartbeat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kubectl = _FakeKubectl()
        runner, plans, records, _ = _lifecycle(tmp_path, kubectl=kubectl)
        clock = _inference_clock(monkeypatch)

        def slow_heartbeat() -> tuple[int, str, str]:
            clock.now += 10.0
            return 0, "ok", ""

        kubectl.heartbeat = slow_heartbeat
        with pytest.raises(ManagedInferenceValidationError, match="not observed before timeout"):
            runner.wait_for_ddb_running(plans[0], records[0])
        assert clock.sleeps == []


class TestKubernetesReadiness:
    def _runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **settings: Any
    ) -> tuple[Any, Any, dict[str, Any], _FakeKubectl, _Clock]:
        kubectl = _FakeKubectl()
        runner, plans, records, _ = _lifecycle(
            tmp_path,
            settings=_settings(tmp_path, **settings) if settings else None,
            kubectl=kubectl,
        )
        clock = _inference_clock(monkeypatch)
        return runner, plans[0], records[0], kubectl, clock

    def test_ready_condition_requires_every_container_ready(self) -> None:
        ready = runtime_module.InferenceRuntimeMixin._ready_condition
        assert ready([{"ready": True}, {"ready": True}]) is True
        assert ready([{"ready": True}, {"ready": False}]) is False
        assert ready([]) is False
        assert ready("nope") is False

    def test_snapshot_handles_missing_or_malformed_deployments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, kubectl, _ = self._runner(tmp_path, monkeypatch)
        kubectl.payloads["deployment"] = (
            1,
            "",
            f'Error from server (NotFound): "{plan.name}" not found',
        )
        assert runner._deployment_ready_snapshot(plan, record, 1, exact=True) == (False, {})
        kubectl.payloads["deployment"] = {"metadata": {}, "spec": [], "status": {}}
        assert runner._deployment_ready_snapshot(plan, record, 1, exact=True) == (False, {})

    def test_snapshot_counts_only_running_ready_pods(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, kubectl, _ = self._runner(tmp_path, monkeypatch)
        kubectl.payloads["deployment"] = _deployment_payload(plan.name, 2)
        kubectl.payloads["pods"] = {
            "items": [
                *_pods_payload(plan.name, 1)["items"],
                *_pods_payload(plan.name, 1, phase="Pending")["items"],
                "not-a-pod",
            ]
        }
        ready, evidence = runner._deployment_ready_snapshot(plan, record, 2, exact=True)
        assert ready is False
        assert evidence == {"desired": 2, "ready": 2, "available": 2, "updated": 2, "ready_pods": 1}
        kubectl.payloads["pods"] = {"items": "corrupt"}
        ready, evidence = runner._deployment_ready_snapshot(plan, record, 2, exact=False)
        assert ready is False
        assert evidence["ready_pods"] == 0

    def test_kubernetes_ready_polls_then_converges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, kubectl, clock = self._runner(tmp_path, monkeypatch)
        attempts: list[int] = []

        def deployment(args: tuple[str, ...]) -> dict[str, Any]:
            attempts.append(1)
            return _deployment_payload(plan.name, 1, ready=0 if len(attempts) == 1 else 1)

        kubectl.payloads["deployment"] = deployment
        kubectl.payloads["pods"] = _pods_payload(plan.name, 1)
        runner.wait_for_kubernetes_ready(plan, record)
        assert record["phase"] == "kubernetes-ready"
        assert record["last_readiness"]["ready"] == 1
        assert clock.sleeps == [1.0]

    def test_kubernetes_ready_times_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, kubectl, clock = self._runner(tmp_path, monkeypatch)
        # Each of the two reads in a snapshot takes 1.5s against a 2s budget,
        # so the phase deadline passes while the pods are being listed.
        kubectl.clock, kubectl.tick = clock, 1.5
        kubectl.payloads["deployment"] = _deployment_payload(plan.name, 1, ready=0)
        with pytest.raises(ManagedInferenceValidationError, match="readiness was not observed"):
            runner.wait_for_kubernetes_ready(plan, record)
        assert record["last_readiness"]["ready_pods"] == 0
        assert clock.sleeps == []

    def test_kubernetes_ready_read_deadline_wins_after_exact_sleeps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, kubectl, clock = self._runner(tmp_path, monkeypatch)
        clock.step = 0.0
        kubectl.payloads["deployment"] = _deployment_payload(plan.name, 1, ready=0)
        with pytest.raises(ManagedInferenceValidationError, match="exceeded its phase deadline"):
            runner.wait_for_kubernetes_ready(plan, record)
        assert clock.sleeps == [1.0, 1.0]


class TestHpaMatching:
    def test_each_contract_field_is_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kubectl = _FakeKubectl()
        runner, plans, records, _ = _lifecycle(tmp_path, kubectl=kubectl)
        _inference_clock(monkeypatch)
        plan, record = plans[1], records[1]
        settings = runner.settings
        good = _hpa_payload(plan.name, settings)
        variants: list[Any] = [
            (1, "", 'Error from server (NotFound): "hpa" not found'),
            {"spec": "corrupt"},
            _hpa_payload(plan.name, settings, scaleTargetRef={"kind": "StatefulSet"}),
            _hpa_payload(plan.name, settings, minReplicas=1),
            _hpa_payload(plan.name, settings, maxReplicas=99),
            _hpa_payload(plan.name, settings, metrics="cpu"),
            _hpa_payload(plan.name, settings, metrics=[{"type": "Pods"}]),
            good,
        ]
        outcomes: list[bool] = []
        for variant in variants:
            kubectl.payloads["horizontalpodautoscaler.autoscaling"] = variant
            outcomes.append(runner._hpa_matches(plan, record))
        assert outcomes == [False] * 7 + [True]

    def test_hpa_contract_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        kubectl = _FakeKubectl()
        runner, plans, records, _ = _lifecycle(tmp_path, kubectl=kubectl)
        clock = _inference_clock(monkeypatch)
        kubectl.clock, kubectl.tick = clock, 0.9
        kubectl.payloads["horizontalpodautoscaler.autoscaling"] = {"spec": {}}
        with pytest.raises(ManagedInferenceValidationError, match="HPA contract was not observed"):
            runner.verify_hpa_stability(plans[1], records[1])
        assert clock.sleeps == [1.0]
        assert "phase" not in records[1] or records[1]["phase"] != "hpa-verified"

    def test_two_replica_wait_polls_then_holds_through_monitor_intervals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _generous_settings(tmp_path)
        kubectl = _FakeKubectl()
        runner, plans, records, _ = _lifecycle(tmp_path, settings=settings, kubectl=kubectl)
        clock = _inference_clock(monkeypatch)
        plan, record = plans[1], records[1]
        snapshots: list[int] = []

        def deployment(args: tuple[str, ...]) -> dict[str, Any]:
            snapshots.append(1)
            return _deployment_payload(plan.name, 2, ready=1 if len(snapshots) == 1 else 2)

        kubectl.payloads["horizontalpodautoscaler.autoscaling"] = _hpa_payload(plan.name, settings)
        kubectl.payloads["deployment"] = deployment
        kubectl.payloads["pods"] = _pods_payload(plan.name, 2)
        runner.verify_hpa_stability(plan, record)
        assert record["phase"] == "hpa-stable"
        assert [item["ready"] for item in record["hpa_stability_observations"]] == [2, 2, 2]
        assert clock.sleeps == [1.0, 1.0, 1.0]

    def test_hpa_replica_convergence_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kubectl = _FakeKubectl()
        runner, plans, records, _ = _lifecycle(tmp_path, kubectl=kubectl)
        clock = _inference_clock(monkeypatch)
        kubectl.clock, kubectl.tick = clock, 0.7
        plan, record = plans[1], records[1]
        kubectl.payloads["horizontalpodautoscaler.autoscaling"] = _hpa_payload(
            plan.name, runner.settings
        )
        kubectl.payloads["deployment"] = _deployment_payload(plan.name, 1)
        kubectl.payloads["pods"] = _pods_payload(plan.name, 1)
        with pytest.raises(ManagedInferenceValidationError, match="two ready replicas"):
            runner.verify_hpa_stability(plan, record)
        assert record["phase"] == "hpa-verified"
        assert record["last_hpa_replica_observation"]["desired"] == 1


class TestSharedProxyAutoscalingShapes:
    def test_corrupt_checkpoint_is_refused(self, tmp_path: Path) -> None:
        runner, _, _, _ = _lifecycle(tmp_path)
        with pytest.raises(ManagedInferenceValidationError, match="shared proxy checkpoint"):
            runner.verify_shared_proxy_autoscaling({"shared_proxy_autoscaling": "corrupt"})

    def test_partial_shapes_are_observed_until_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(tmp_path, hpa_timeout_seconds=9)
        kubectl = _FakeKubectl()
        runner, _, _, _ = _lifecycle(tmp_path, settings=settings, kubectl=kubectl)
        clock = _inference_clock(monkeypatch)
        # Two 0.5s reads plus a 1s poll per round: five rounds fit the 9s budget
        # and the deadline expires during the fifth round's HPA read.
        kubectl.clock, kubectl.tick = clock, 0.5
        good_deployment = _SharedProxyFixtures._deployment()
        good_hpa = _SharedProxyFixtures._hpa()
        no_requests = json.loads(json.dumps(good_deployment))
        no_requests["spec"]["template"]["spec"]["containers"][0]["resources"] = "none"
        two_sidecars = json.loads(json.dumps(good_deployment))
        two_sidecars["spec"]["template"]["spec"]["containers"] *= 2
        wrong_metric_type = json.loads(json.dumps(good_hpa))
        wrong_metric_type["spec"]["metrics"] = [{"type": "Resource"}]
        wrong_metric_type["status"]["currentMetrics"] = [{"type": "Resource"}]
        source_not_dict = json.loads(json.dumps(good_hpa))
        source_not_dict["spec"]["metrics"] = [{"type": "ContainerResource", "containerResource": 1}]
        target_not_dict = json.loads(json.dumps(good_hpa))
        target_not_dict["spec"]["metrics"][0]["containerResource"]["target"] = "70"
        target_not_dict["status"]["currentMetrics"] = []
        missing = (1, "", 'Error from server (NotFound): "inference-proxy" not found')
        scripted: list[tuple[Any, Any]] = [
            (missing, missing),
            (no_requests, wrong_metric_type),
            (two_sidecars, source_not_dict),
            (good_deployment, target_not_dict),
            (good_deployment, {"metadata": [], "spec": [], "status": []}),
        ]
        observed: list[dict[str, Any]] = []

        def kubectl_payload(index: int) -> Any:
            def handler(args: tuple[str, ...]) -> Any:
                round_number = min(len([c for c in kubectl.calls if c[1] == args[1]]) - 1, 4)
                return scripted[round_number][index]

            return handler

        kubectl.payloads["deployment"] = kubectl_payload(0)
        kubectl.payloads["horizontalpodautoscaler.autoscaling"] = kubectl_payload(1)
        original_persist = runner._persist

        def capture() -> None:
            original_persist()
            record = runner.state.get("shared_proxy_autoscaling")
            if isinstance(record, dict) and "last_observed" in record:
                observed.append(dict(record["last_observed"]))

        monkeypatch.setattr(runner, "_persist", capture)
        with pytest.raises(ManagedInferenceValidationError, match="TLS autoscaling"):
            runner.verify_shared_proxy_autoscaling(runner.state)

        assert len(observed) == 5
        assert observed[0] == {}
        assert observed[1]["tls_metric_count"] == 0
        assert "tls_cpu_request" not in observed[1]
        assert observed[2]["tls_metric_count"] == 0
        assert "tls_cpu_request" not in observed[2]
        assert observed[3]["tls_cpu_request"] == "100m"
        assert observed[3]["tls_cpu_target"] is None
        assert observed[3]["active_tls_metric_count"] == 0
        assert observed[4]["target_matches"] is False
        assert observed[4]["observed_generation_current"] is True


# ------------------------------------------------------------- inference_inventory


class TestKubectlJson:
    def _runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Any, dict[str, Any], _FakeKubectl]:
        kubectl = _FakeKubectl()
        runner, _, records, _ = _lifecycle(tmp_path, kubectl=kubectl)
        _inference_clock(monkeypatch)
        return runner, records[0], kubectl

    def test_launch_failure_is_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, record, kubectl = self._runner(tmp_path, monkeypatch)
        kubectl.payloads["pods"] = OSError("kubectl missing")
        with pytest.raises(ManagedInferenceValidationError, match="Kubernetes read failed"):
            runner._kubectl_json(record, "get", "pods")
        assert record["failures"][-1] == {"stage": "kubectl", "error": "OSError: kubectl missing"}

    def test_not_found_is_none_and_other_failures_are_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, record, kubectl = self._runner(tmp_path, monkeypatch)
        kubectl.payloads["pods"] = (1, "", 'Error from server (NotFound): pods "x" not found')
        assert runner._kubectl_json(record, "get", "pods") is None
        kubectl.payloads["pods"] = (1, "partial", "Unable to connect to the server")
        with pytest.raises(ManagedInferenceValidationError, match="Kubernetes read failed"):
            runner._kubectl_json(record, "get", "pods")
        assert record["last_kubectl_error"] == {
            "argv": ["get", "pods"],
            "returncode": 1,
            "stdout": "partial",
            "stderr": "Unable to connect to the server",
        }

    def test_invalid_json_is_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, record, kubectl = self._runner(tmp_path, monkeypatch)
        kubectl.payloads["pods"] = (0, "not json", "")
        with pytest.raises(ManagedInferenceValidationError, match="returned invalid JSON"):
            runner._kubectl_json(record, "get", "pods")
        assert record["failures"][-1]["stage"] == "kubectl-json"

    def test_deadline_bounds_the_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, record, kubectl = self._runner(tmp_path, monkeypatch)
        with pytest.raises(ManagedInferenceValidationError, match="exceeded its phase deadline"):
            runner._kubectl_json(record, "get", "pods", deadline=0.0)
        assert kubectl.calls == []


class TestOwnedInventoryClassification:
    NAME = "gco-mi-abc-vllm-baseline"

    def _owned(
        self, summary_key: str, item: Any, owned_replica_sets: set[str] | None = None
    ) -> bool:
        return inventory_module.InferenceInventoryMixin._owned_inventory_item(
            summary_key, item, self.NAME, owned_replica_sets or set()
        )

    def test_shape_and_kind_guards(self) -> None:
        assert self._owned("pods", {"metadata": "corrupt"}) is False
        assert self._owned("unknown_kind", {"metadata": {"name": self.NAME}}) is False
        assert self._owned("endpoints", {"metadata": {"name": f"{self.NAME}-proxy"}}) is True

    def test_workload_labels_and_owner_references(self) -> None:
        labels = {"app": self.NAME, "project": "gco", "gco.io/type": "inference"}
        assert self._owned("pods", {"metadata": {"name": "p", "labels": {"app": "other"}}}) is False
        assert (
            self._owned(
                "pods", {"metadata": {"name": "p", "labels": {**labels, "project": "other"}}}
            )
            is False
        )
        assert self._owned("pods", {"metadata": {"name": "p", "labels": labels}}) is True
        via_replica_set = {
            "metadata": {
                "name": "p",
                "labels": labels,
                "ownerReferences": ["corrupt", {"kind": "ReplicaSet", "name": f"{self.NAME}-rs"}],
            }
        }
        assert self._owned("pods", via_replica_set, {f"{self.NAME}-rs"}) is True
        assert self._owned("pods", via_replica_set, set()) is False
        assert self._owned("replica_sets", via_replica_set, {f"{self.NAME}-rs"}) is False


class TestInventoryAndAbsence:
    def _runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **settings: Any
    ) -> tuple[Any, Any, dict[str, Any], _FakeKubectl, _Clock]:
        kubectl = _FakeKubectl()
        runner, plans, records, _ = _lifecycle(
            tmp_path,
            settings=_settings(tmp_path, **settings) if settings else None,
            kubectl=kubectl,
        )
        clock = _inference_clock(monkeypatch)
        return runner, plans[0], records[0], kubectl, clock

    def test_malformed_items_are_refused_and_nameless_owned_items_are_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, kubectl, _ = self._runner(tmp_path, monkeypatch)
        kubectl.payloads["endpointslices.discovery.k8s.io"] = {
            "items": [
                {"metadata": {"labels": {"kubernetes.io/service-name": plan.name}}},
                {
                    "metadata": {
                        "name": "named",
                        "labels": {"kubernetes.io/service-name": plan.name},
                    }
                },
            ]
        }
        inventory = runner.kubernetes_inventory(record)
        assert inventory["endpoint_slices"] == ["named"]
        kubectl.payloads["pods"] = {"items": "corrupt"}
        with pytest.raises(ManagedInferenceValidationError, match="inventory is malformed"):
            runner.kubernetes_inventory(record)

    def test_absence_snapshot_honours_its_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _, record, kubectl, clock = self._runner(tmp_path, monkeypatch)
        with pytest.raises(ManagedInferenceValidationError, match="absence was not proven"):
            runner.absence_snapshot(record, deadline=clock.now)
        assert kubectl.calls == []

    def test_prove_absence_repairs_history_and_trims_observations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _, record, _, _ = self._runner(tmp_path, monkeypatch)
        record["absence_observations"] = "corrupt"
        runner.prove_absence(record)
        assert len(record["absence_observations"]) == 2
        record["absence_observations"] = [{"seed": index} for index in range(20)]
        runner.prove_absence(record)
        assert len(record["absence_observations"]) == 20
        assert record["absence_observations"][0] == {"seed": 2}
        assert record["absence_observations"][-1]["ddb_absent"] is True

    def test_prove_absence_times_out_while_the_record_lingers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, _, clock = self._runner(tmp_path, monkeypatch)
        runner.table.item = _owned_item(runner.settings, plan, runner.owner_nonce)
        with pytest.raises(ManagedInferenceValidationError, match="absence was not proven"):
            runner.prove_absence(record)
        assert record["consecutive_absent_observations"] == 0
        assert record["last_absence_observation"]["ddb_present"] is True
        assert clock.sleeps

    def test_prove_absence_deadline_can_expire_during_a_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, kubectl, clock = self._runner(tmp_path, monkeypatch)
        # Twelve 0.17s reads: the last one ends past the 2s deletion budget.
        kubectl.clock, kubectl.tick = clock, 0.17
        runner.table.item = _owned_item(runner.settings, plan, runner.owner_nonce)
        with pytest.raises(ManagedInferenceValidationError, match="absence was not proven"):
            runner.prove_absence(record)
        assert len(kubectl.calls) == len(inventory_module.KUBERNETES_INVENTORY_KINDS)
        assert clock.sleeps == []
        assert len(record["absence_observations"]) == 1
