"""Offline safety contracts for the live release-validation harness.

Every AWS/API boundary is mocked. These tests are intended for CI and must
never create, mutate, or delete live infrastructure.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import sys
import threading
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from cli.jobs import JobManager
from scripts.live_release_validation import (
    constants,
    context,
    inventory,
    protected,
)
from scripts.live_release_validation.actions import central_queue as actions_central_queue
from scripts.live_release_validation.actions import deploy as actions_deploy
from scripts.live_release_validation.actions import destroy as actions_destroy
from scripts.live_release_validation.actions import final_inventory as actions_final_inventory
from scripts.live_release_validation.actions import opencost as actions_opencost
from scripts.live_release_validation.actions import topology as actions_topology
from scripts.live_release_validation.checks import central_queue as checks_central_queue
from scripts.live_release_validation.checks import jobs as checks_jobs
from scripts.live_release_validation.checks import opencost as checks_opencost
from scripts.live_release_validation.checks import topology as checks_topology
from scripts.live_release_validation.cleanup import ecr as cleanup_ecr
from scripts.live_release_validation.cleanup import log_groups as cleanup_log_groups
from scripts.live_release_validation.cleanup import workloads as cleanup_workloads_module
from scripts.live_release_validation.inventory import ecr as inventory_ecr
from scripts.live_release_validation.inventory import scanners as inventory_scanners
from scripts.live_release_validation.ownership import dynamodb_streams as ownership_streams
from scripts.live_release_validation.ownership import ecr as ownership_ecr
from scripts.live_release_validation.ownership import efs_automatic_backups as ownership_efs_backups
from scripts.live_release_validation.ownership import log_groups as ownership_log_groups
from scripts.live_release_validation.ownership import stacks as ownership_stacks
from tests._live_validation_patching import patch_live_validation_helper


def _response(
    status_code: int,
    payload: dict[str, object] | None = None,
    *,
    text: str = "",
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.ok = 200 <= status_code < 300
    response.text = text
    response.json.return_value = payload or {}
    return response


def _context(*, state: dict[str, object] | None = None) -> SimpleNamespace:
    checkpoint = SimpleNamespace(
        state=state or {},
        baseline={"ecr_repositories": {}, "ecr_regions": ["us-east-1"]},
        deployment_attempted=True,
        destroyed=False,
        completed_actions=[],
    )
    settings = SimpleNamespace(
        run_id="run-123",
        expected_account="123456789012",
        poll_interval_seconds=0,
        job_timeout_seconds=30,
        queue_timeout_seconds=30,
        destroy_attempts=1,
        destroy_retry_delay_seconds=0,
        confirm_kms_key_deletion=True,
    )
    context = SimpleNamespace(
        checkpoint=checkpoint,
        settings=settings,
        state_lock=threading.RLock(),
        deployment_regions=("us-east-1",),
        config=SimpleNamespace(
            project_name="gco-live",
            global_region="us-east-1",
        ),
        cdk_context={"api_gateway": {"regional_api_enabled": True}},
        session=MagicMock(),
        aws_client=MagicMock(),
        stack_manager=MagicMock(),
        job_manager=MagicMock(),
        report=SimpleNamespace(final_inventory={}),
    )
    context.persist = MagicMock()
    context.persist_callback = MagicMock()
    context.prepare_job_submission = MagicMock()
    context.begin_job_submission = MagicMock()
    context.finish_job_submission = MagicMock()
    context.bind_central_job_identity = MagicMock()
    context.record_job_uid = MagicMock()
    context.mark_job_not_submitted = MagicMock()
    context.mark_job_deleted = MagicMock()
    context.mark_central_job_cancelled_before_claim = MagicMock()
    context.mark_central_job_not_created_by_worker = MagicMock()
    return context


def _real_context(tmp_path: Path):
    from scripts.live_release_validation.models import (
        RunCheckpoint,
        RunContext,
        RunSettings,
        ValidationReport,
    )

    settings = RunSettings(
        run_id="run-123",
        repo_root=tmp_path,
        report_dir=tmp_path / "report",
        checkpoint_path=tmp_path / "report/checkpoint.json",
        expected_account="123456789012",
        expected_sha="a" * 40,
        expected_branch="chore/test",
        profile="ci",
        requested_actions=("central-queue", "destroy"),
        poll_interval_seconds=0,
    )
    checkpoint = RunCheckpoint(identity=settings.identity())
    report = ValidationReport(
        run_id=settings.run_id,
        identity=settings.identity(),
        selected_actions=list(settings.requested_actions),
        started_at="2026-07-17T00:00:00+00:00",
    )
    return RunContext(
        settings=settings,
        checkpoint=checkpoint,
        report=report,
        cdk_context={},
        deployment_regions=("us-east-1",),
        config=SimpleNamespace(project_name="gco-live", global_region="us-east-1"),
        session=MagicMock(),
        stack_manager=MagicMock(),
        aws_client=MagicMock(),
        job_manager=MagicMock(),
        persist_callback=MagicMock(),
    )


def _central_job(job_id: str, *, status: str = "succeeded") -> dict[str, str]:
    job = {
        "job_id": job_id,
        "job_name": "gco-live-ddb-run-123",
        "namespace": "gco-jobs",
        "target_region": "us-east-1",
        "status": status,
    }
    if status == "succeeded":
        job.update(
            {
                "k8s_job_name": checks_central_queue._central_queue_kubernetes_job_name(
                    job["job_name"],
                    job_id,
                ),
                "k8s_job_namespace": job["namespace"],
                "k8s_job_uid": "uid-central-1",
            }
        )
    return job


class TestCentralQueueResume:
    def test_job_id_matches_deployed_queue_protocol(self) -> None:
        from gco.services.api_routes.queue import _IDEMPOTENCY_NAMESPACE

        key = "gco-live-validation:run-123:central"
        assert checks_central_queue._central_queue_job_id(key) == str(
            uuid.uuid5(_IDEMPOTENCY_NAMESPACE, key)
        )

    def test_actual_identity_binding_preserves_requested_replay_identity_and_deadline(
        self,
        tmp_path: Path,
    ) -> None:
        ctx = _real_context(tmp_path)
        key = "gco-live-validation:run-123:central"
        job_id = checks_central_queue._central_queue_job_id(key)
        record = ctx.register_job(
            name="gco-live-ddb-run-123",
            namespace="gco-jobs",
            region="us-east-1",
            path="dynamodb",
            run_label=checks_jobs._run_token(ctx.settings.run_id),
            transport_region=None,
        )
        envelope = {
            "transport": "central-queue",
            "body": {"manifest": {"kind": "Job"}},
            "idempotency_key": key,
            "job_id": job_id,
            "transport_region": None,
        }
        ctx.prepare_job_submission(record, envelope=envelope, resumable=True)
        ctx.begin_job_submission(record, reconciliation_timeout_seconds=30)
        ctx.finish_job_submission(
            record, {"job": _central_job(job_id)}, appearance_timeout_seconds=5
        )
        stale_deadline = record["appearance_deadline"]
        central_record = checks_central_queue._register_central_job(
            ctx,
            job_id=job_id,
            idempotency_key=key,
            record=record,
            marker="GCO_LIVE_DDB_run-123",
            body=envelope["body"],
        )
        persisted = _central_job(job_id)

        with patch("scripts.live_release_validation.models.time.time", return_value=100.0):
            bound = checks_central_queue._reconcile_central_workload_identity(
                ctx,
                central_record,
                persisted,
                workload_record=record,
            )

        assert bound is record
        assert record["name"] == "gco-live-ddb-run-123"
        assert record["namespace"] == "gco-jobs"
        assert record["submission_envelope"] == envelope
        assert record["k8s_job_name"] == persisted["k8s_job_name"]
        assert record["k8s_job_namespace"] == persisted["k8s_job_namespace"]
        assert record["k8s_job_uid"] == persisted["k8s_job_uid"]
        assert record["uid"] == persisted["k8s_job_uid"]
        assert record["appearance_deadline"] == 100.0 + checks_jobs._job_appearance_timeout(ctx)
        assert record["appearance_deadline"] != stale_deadline
        assert checks_jobs._job_api_path(record) == (
            f"/api/v1/jobs/gco-jobs/{persisted['k8s_job_name']}"
        )

        original_deadline = record["appearance_deadline"]
        with patch("scripts.live_release_validation.models.time.time", return_value=200.0):
            checks_central_queue._reconcile_central_workload_identity(
                ctx,
                central_record,
                persisted,
                workload_record=record,
            )
        assert record["appearance_deadline"] == original_deadline

        changed = {**persisted, "k8s_job_uid": "uid-central-replaced"}
        with pytest.raises(RuntimeError, match="identity changed|UID disagrees"):
            checks_central_queue._reconcile_central_workload_identity(
                ctx,
                central_record,
                changed,
                workload_record=record,
            )

    @pytest.mark.parametrize(
        ("section", "key", "bad_value", "message"),
        [
            ("labels", constants._CENTRAL_MANAGED_BY_LABEL, "foreign", "managed-by"),
            ("labels", constants._CENTRAL_QUEUE_KEY_LABEL, "bad-key", "queue-key"),
            ("annotations", constants._CENTRAL_QUEUE_ID_ANNOTATION, "foreign", "queue ID"),
            (
                "annotations",
                constants._CENTRAL_ORIGINAL_NAME_ANNOTATION,
                "foreign",
                "original-name",
            ),
            ("metadata", "uid", "foreign-uid", "UID differs"),
        ],
    )
    def test_actual_job_lookup_rejects_changed_central_metadata(
        self,
        tmp_path: Path,
        section: str,
        key: str,
        bad_value: str,
        message: str,
    ) -> None:
        ctx = _real_context(tmp_path)
        key_value = "gco-live-validation:run-123:central"
        job_id = checks_central_queue._central_queue_job_id(key_value)
        persisted = _central_job(job_id)
        record = ctx.register_job(
            name=persisted["job_name"],
            namespace=persisted["namespace"],
            region=persisted["target_region"],
            path="dynamodb",
            run_label=checks_jobs._run_token(ctx.settings.run_id),
            transport_region=None,
        )
        ctx.bind_central_job_identity(
            record,
            job_id=job_id,
            name=persisted["k8s_job_name"],
            namespace=persisted["k8s_job_namespace"],
            uid=persisted["k8s_job_uid"],
            appearance_timeout_seconds=30,
        )
        metadata: dict[str, object] = {
            "name": persisted["k8s_job_name"],
            "namespace": persisted["k8s_job_namespace"],
            "uid": persisted["k8s_job_uid"],
            "labels": {
                constants._RUN_JOB_LABEL: checks_jobs._run_token(ctx.settings.run_id),
                constants._PATH_JOB_LABEL: "dynamodb",
                constants._CENTRAL_MANAGED_BY_LABEL: "central-queue",
                constants._CENTRAL_QUEUE_KEY_LABEL: hashlib.sha256(
                    job_id.encode("utf-8")
                ).hexdigest()[:32],
            },
            "annotations": {
                constants._CENTRAL_QUEUE_ID_ANNOTATION: job_id,
                constants._CENTRAL_ORIGINAL_NAME_ANNOTATION: record["name"],
            },
        }
        if section == "metadata":
            metadata[key] = bad_value
        else:
            nested = metadata[section]
            assert isinstance(nested, dict)
            nested[key] = bad_value
        ctx.aws_client.make_authenticated_request.return_value = _response(
            200,
            {"region": "us-east-1", "metadata": metadata},
        )

        with pytest.raises(RuntimeError, match=message):
            checks_jobs._get_owned_job(ctx, record)

        request = ctx.aws_client.make_authenticated_request.call_args.kwargs
        assert request["path"] == f"/api/v1/jobs/gco-jobs/{persisted['k8s_job_name']}"

    def test_persisted_identity_must_match_deterministic_queue_name(self) -> None:
        ctx = _context()
        job_id = checks_central_queue._central_queue_job_id("gco-live-validation:run-123:central")
        persisted = {**_central_job(job_id), "k8s_job_name": "foreign-job"}
        record = {
            "name": persisted["job_name"],
            "namespace": persisted["namespace"],
            "region": persisted["target_region"],
            "path": "dynamodb",
            "transport_region": None,
        }
        central_record = {
            "job_id": job_id,
            "idempotency_key": "gco-live-validation:run-123:central",
            "job_name": persisted["job_name"],
            "namespace": persisted["namespace"],
            "target_region": persisted["target_region"],
            "transport_region": None,
        }

        with pytest.raises(RuntimeError, match="unexpected Kubernetes Job name"):
            checks_central_queue._reconcile_central_workload_identity(
                ctx,
                central_record,
                persisted,
                workload_record=record,
            )
        ctx.bind_central_job_identity.assert_not_called()

    def test_submitting_checkpoint_reconciles_without_second_post(self) -> None:
        ctx = _context()
        key = "gco-live-validation:run-123:central"
        job_id = checks_central_queue._central_queue_job_id(key)
        marker = "GCO_LIVE_DDB_run-123"
        queue_job = _central_job(job_id)
        job_record = {
            "name": queue_job["job_name"],
            "namespace": queue_job["namespace"],
            "region": queue_job["target_region"],
            "path": "dynamodb",
            "transport_region": None,
            "submission_state": "submitting",
            "appearance_deadline": 9999999999.0,
            "central_queue_job_id": job_id,
            "k8s_job_name": queue_job["k8s_job_name"],
            "k8s_job_namespace": queue_job["k8s_job_namespace"],
            "k8s_job_uid": queue_job["k8s_job_uid"],
            "uid": queue_job["k8s_job_uid"],
            "deleted": True,
            "validation_evidence": {
                "name": queue_job["k8s_job_name"],
                "namespace": queue_job["k8s_job_namespace"],
                "uid": queue_job["k8s_job_uid"],
                "central_queue_job_id": job_id,
                "requested_name": queue_job["job_name"],
                "requested_namespace": queue_job["namespace"],
                "region": queue_job["target_region"],
                "marker": marker,
                "status": "succeeded",
            },
        }
        central_record = {
            "job_id": job_id,
            "idempotency_key": key,
            "job_name": queue_job["job_name"],
            "namespace": queue_job["namespace"],
            "target_region": queue_job["target_region"],
            "transport_region": None,
            "marker": marker,
            "appearance_deadline": 9999999999.0,
        }

        with (
            patch_live_validation_helper(
                "_central_manifest",
                return_value=({}, queue_job["job_name"], queue_job["namespace"], marker),
            ),
            patch_live_validation_helper("_register_job", return_value=job_record),
            patch_live_validation_helper("_register_central_job", return_value=central_record),
            patch_live_validation_helper(
                "_get_central_queue_job",
                return_value=queue_job,
            ) as get_queue_job,
            patch_live_validation_helper(
                "_wait_for_central_queue_appearance",
                return_value=queue_job,
            ) as wait_for_appearance,
            patch_live_validation_helper(
                "_wait_for_central_queue_terminal",
                return_value=(queue_job, [{"status": "succeeded", "at": 1.0}]),
            ),
            patch_live_validation_helper("_read_central_job_item", return_value=queue_job),
        ):
            result = actions_central_queue.action_central_queue_lifecycle(ctx)

        assert result["job_id"] == job_id
        get_queue_job.assert_called_once_with(ctx, central_record)
        wait_for_appearance.assert_called_once_with(ctx, central_record)
        ctx.aws_client.make_authenticated_request.assert_not_called()
        ctx.finish_job_submission.assert_called_once()
        ctx.bind_central_job_identity.assert_called_once_with(
            job_record,
            job_id=job_id,
            name=queue_job["k8s_job_name"],
            namespace=queue_job["k8s_job_namespace"],
            uid=queue_job["k8s_job_uid"],
            appearance_timeout_seconds=30,
        )

    def test_submitting_checkpoint_replays_only_exact_persisted_envelope(self) -> None:
        ctx = _context()
        key = "gco-live-validation:run-123:central"
        job_id = checks_central_queue._central_queue_job_id(key)
        marker = "GCO_LIVE_DDB_run-123"
        manifest = {"apiVersion": "batch/v1", "kind": "Job"}
        queue_job = _central_job(job_id)
        body = {
            "manifest": manifest,
            "target_region": "us-east-1",
            "namespace": queue_job["namespace"],
            "priority": 100,
            "labels": {
                constants._RUN_JOB_LABEL: checks_jobs._run_token(ctx.settings.run_id),
            },
        }
        envelope = {
            "transport": "central-queue",
            "body": body,
            "idempotency_key": key,
            "job_id": job_id,
            "transport_region": "us-east-2",
        }
        job_record = {
            "name": queue_job["job_name"],
            "namespace": queue_job["namespace"],
            "region": queue_job["target_region"],
            "path": "dynamodb",
            "transport_region": "us-east-2",
            "submission_state": "submitting",
            "submission_envelope": envelope,
            "appearance_deadline": 9999999999.0,
            "central_queue_job_id": job_id,
            "k8s_job_name": queue_job["k8s_job_name"],
            "k8s_job_namespace": queue_job["k8s_job_namespace"],
            "k8s_job_uid": queue_job["k8s_job_uid"],
            "uid": queue_job["k8s_job_uid"],
            "deleted": True,
            "validation_evidence": {
                "name": queue_job["k8s_job_name"],
                "namespace": queue_job["k8s_job_namespace"],
                "uid": queue_job["k8s_job_uid"],
                "central_queue_job_id": job_id,
                "requested_name": queue_job["job_name"],
                "requested_namespace": queue_job["namespace"],
                "region": queue_job["target_region"],
                "marker": marker,
                "status": "succeeded",
            },
        }
        central_record = {
            "job_id": job_id,
            "idempotency_key": key,
            "job_name": queue_job["job_name"],
            "namespace": queue_job["namespace"],
            "target_region": queue_job["target_region"],
            "transport_region": "us-east-2",
            "marker": marker,
            "appearance_deadline": 9999999999.0,
        }
        ctx.aws_client.make_authenticated_request.return_value = _response(
            201,
            {"job": queue_job},
        )

        with (
            patch_live_validation_helper(
                "_central_manifest",
                return_value=(
                    manifest,
                    queue_job["job_name"],
                    queue_job["namespace"],
                    marker,
                ),
            ),
            patch_live_validation_helper("_register_job", return_value=job_record),
            patch_live_validation_helper("_register_central_job", return_value=central_record),
            patch_live_validation_helper("_get_central_queue_job", return_value=None),
            patch_live_validation_helper(
                "_wait_for_central_queue_appearance",
                return_value=queue_job,
            ) as wait_for_appearance,
            patch_live_validation_helper(
                "_wait_for_central_queue_terminal",
                return_value=(queue_job, [{"status": "succeeded", "at": 1.0}]),
            ),
            patch_live_validation_helper("_read_central_job_item", return_value=queue_job),
        ):
            result = actions_central_queue.action_central_queue_lifecycle(ctx)

        assert result["job_id"] == job_id
        request = ctx.aws_client.make_authenticated_request.call_args.kwargs
        assert request == {
            "method": "POST",
            "path": "/api/v1/queue/jobs",
            "body": body,
            "headers": {"Idempotency-Key": key},
            "target_region": "us-east-2",
        }
        ctx.begin_job_submission.assert_called_once()
        ctx.finish_job_submission.assert_called_once()
        wait_for_appearance.assert_called_once_with(ctx, central_record)

    def test_cleanup_waits_through_early_404_then_cancels_and_reconciles(self) -> None:
        workload = {
            "name": "gco-live-ddb-run-123",
            "namespace": "gco-jobs",
            "region": "us-east-1",
            "path": "dynamodb",
            "transport_region": None,
            "submission_state": "submitting",
            "uid": None,
            "deleted": False,
        }
        ctx = _context(state={"jobs": [workload]})
        job_id = checks_central_queue._central_queue_job_id("gco-live-validation:run-123:central")
        central_record = {
            "job_id": job_id,
            "idempotency_key": "gco-live-validation:run-123:central",
            "job_name": "gco-live-ddb-run-123",
            "namespace": "gco-jobs",
            "target_region": "us-east-1",
            "transport_region": None,
            "appearance_deadline": 9999999999.0,
        }
        queued = _central_job(job_id, status="queued")
        cancelled = _central_job(job_id, status="cancelled")
        ctx.aws_client.make_authenticated_request.return_value = _response(
            200,
            {"message": "cancelled"},
        )

        with (
            patch_live_validation_helper(
                "_get_central_queue_job",
                side_effect=[None, queued, cancelled],
            ) as get_job,
            patch_live_validation_helper("_read_central_job_item", return_value=cancelled),
            patch("time.sleep"),
        ):
            result = cleanup_workloads_module._cleanup_central_job(ctx, central_record)

        assert get_job.call_count == 3
        assert result["terminal_status"] == "cancelled"
        assert result["workload_not_submitted"] is True
        assert central_record["cleanup_complete"] is True
        ctx.mark_central_job_cancelled_before_claim.assert_called_once_with(
            workload,
            job_id=job_id,
        )
        delete_call = ctx.aws_client.make_authenticated_request.call_args.kwargs
        assert delete_call["method"] == "DELETE"
        assert job_id in delete_call["path"]

    def test_real_submission_transition_closes_after_unclaimed_cancellation(
        self,
        tmp_path: Path,
    ) -> None:
        ctx = _real_context(tmp_path)
        key = "gco-live-validation:run-123:central"
        job_id = checks_central_queue._central_queue_job_id(key)
        record = ctx.register_job(
            name="gco-live-ddb-run-123",
            namespace="gco-jobs",
            region="us-east-1",
            path="dynamodb",
            run_label=checks_jobs._run_token(ctx.settings.run_id),
            transport_region=None,
        )
        body = {"manifest": {"kind": "Job"}, "target_region": "us-east-1"}
        ctx.prepare_job_submission(
            record,
            envelope={
                "transport": "central-queue",
                "body": body,
                "idempotency_key": key,
                "job_id": job_id,
                "transport_region": None,
            },
            resumable=True,
        )
        ctx.begin_job_submission(record, reconciliation_timeout_seconds=30)
        central_record = checks_central_queue._register_central_job(
            ctx,
            job_id=job_id,
            idempotency_key=key,
            record=record,
            marker="GCO_LIVE_DDB_run-123",
            body=body,
        )
        queued = _central_job(job_id, status="queued")
        cancelled = _central_job(job_id, status="cancelled")
        ctx.aws_client.make_authenticated_request.return_value = _response(
            200,
            {"message": "cancelled"},
        )

        with (
            patch_live_validation_helper("_wait_for_central_queue_appearance", return_value=queued),
            patch_live_validation_helper(
                "_wait_for_central_queue_terminal",
                return_value=(cancelled, [{"status": "cancelled", "at": 1.0}]),
            ),
            patch_live_validation_helper("_read_central_job_item", return_value=cancelled),
        ):
            result = cleanup_workloads_module._cleanup_central_job(ctx, central_record)

        assert result["workload_not_submitted"] is True
        assert record["submission_state"] == "not_submitted"
        assert record["central_cancelled_before_claim_job_id"] == job_id

        with patch_live_validation_helper("_get_owned_job", return_value=None):
            deletion = checks_jobs._delete_owned_job(ctx, record)
        assert deletion == {"not_submitted": True, "already_absent": True}
        assert record["deleted"] is True

    def test_real_submission_transition_closes_after_worker_proves_no_workload(
        self,
        tmp_path: Path,
    ) -> None:
        ctx = _real_context(tmp_path)
        key = "gco-live-validation:run-123:central"
        job_id = checks_central_queue._central_queue_job_id(key)
        record = ctx.register_job(
            name="gco-live-ddb-run-123",
            namespace="gco-jobs",
            region="us-east-1",
            path="dynamodb",
            run_label=checks_jobs._run_token(ctx.settings.run_id),
            transport_region=None,
        )
        body = {"manifest": {"kind": "Job"}, "target_region": "us-east-1"}
        ctx.prepare_job_submission(
            record,
            envelope={
                "transport": "central-queue",
                "body": body,
                "idempotency_key": key,
                "job_id": job_id,
                "transport_region": None,
            },
            resumable=True,
        )
        ctx.begin_job_submission(record, reconciliation_timeout_seconds=30)
        failed: dict[str, object] = {
            **_central_job(job_id, status="failed"),
            "workload_not_created": True,
        }
        ctx.finish_job_submission(record, {"job": failed}, appearance_timeout_seconds=30)
        central_record = checks_central_queue._register_central_job(
            ctx,
            job_id=job_id,
            idempotency_key=key,
            record=record,
            marker="GCO_LIVE_DDB_run-123",
            body=body,
        )

        with (
            patch_live_validation_helper("_wait_for_central_queue_appearance", return_value=failed),
            patch_live_validation_helper("_read_central_job_item", return_value=failed),
        ):
            result = cleanup_workloads_module._cleanup_central_job(ctx, central_record)

        assert result["terminal_status"] == "failed"
        assert result["workload_not_submitted"] is True
        assert record["submission_state"] == "not_submitted"
        assert record["central_worker_not_created_job_id"] == job_id

        with patch_live_validation_helper("_get_owned_job", return_value=None):
            deletion = checks_jobs._delete_owned_job(ctx, record)
        assert deletion == {"not_submitted": True, "already_absent": True}
        assert record["deleted"] is True

    def test_failed_central_job_without_worker_proof_remains_unresolved(self) -> None:
        job_id = checks_central_queue._central_queue_job_id("gco-live-validation:run-123:central")
        workload = {
            "name": "gco-live-ddb-run-123",
            "namespace": "gco-jobs",
            "region": "us-east-1",
            "path": "dynamodb",
            "transport_region": None,
            "submission_state": "submitted",
            "uid": None,
        }
        ctx = _context(state={"jobs": [workload]})
        central_record = {
            "job_id": job_id,
            "idempotency_key": "gco-live-validation:run-123:central",
            "job_name": workload["name"],
            "namespace": workload["namespace"],
            "target_region": workload["region"],
            "transport_region": None,
        }

        with pytest.raises(RuntimeError, match="omitted worker Kubernetes identity"):
            checks_central_queue._reconcile_central_cleanup_workload(
                ctx,
                central_record,
                _central_job(job_id, status="failed"),
            )

        ctx.mark_central_job_not_created_by_worker.assert_not_called()

    def test_failed_central_job_rejects_conflicting_proof_and_identity(self) -> None:
        job_id = checks_central_queue._central_queue_job_id("gco-live-validation:run-123:central")
        persisted: dict[str, object] = {
            **_central_job(job_id),
            "status": "failed",
            "workload_not_created": True,
        }
        workload = {
            "name": persisted["job_name"],
            "namespace": persisted["namespace"],
            "region": persisted["target_region"],
            "path": "dynamodb",
            "transport_region": None,
            "submission_state": "submitted",
            "uid": None,
        }
        ctx = _context(state={"jobs": [workload]})
        central_record = {
            "job_id": job_id,
            "idempotency_key": "gco-live-validation:run-123:central",
            "job_name": persisted["job_name"],
            "namespace": persisted["namespace"],
            "target_region": persisted["target_region"],
            "transport_region": None,
        }

        with pytest.raises(RuntimeError, match="both no-workload proof and Kubernetes identity"):
            checks_central_queue._reconcile_central_cleanup_workload(ctx, central_record, persisted)

        ctx.mark_central_job_not_created_by_worker.assert_not_called()

    def test_cleanup_binds_succeeded_worker_identity(self, tmp_path: Path) -> None:
        ctx = _real_context(tmp_path)
        key = "gco-live-validation:run-123:central"
        job_id = checks_central_queue._central_queue_job_id(key)
        record = ctx.register_job(
            name="gco-live-ddb-run-123",
            namespace="gco-jobs",
            region="us-east-1",
            path="dynamodb",
            run_label=checks_jobs._run_token(ctx.settings.run_id),
            transport_region=None,
        )
        ctx.prepare_job_submission(
            record,
            envelope={
                "transport": "central-queue",
                "body": {"manifest": {"kind": "Job"}},
                "idempotency_key": key,
                "job_id": job_id,
                "transport_region": None,
            },
            resumable=True,
        )
        ctx.begin_job_submission(record, reconciliation_timeout_seconds=30)
        central_record = checks_central_queue._register_central_job(
            ctx,
            job_id=job_id,
            idempotency_key=key,
            record=record,
            marker="GCO_LIVE_DDB_run-123",
            body={"manifest": {"kind": "Job"}},
        )
        persisted = _central_job(job_id)

        with (
            patch_live_validation_helper(
                "_wait_for_central_queue_appearance", return_value=persisted
            ),
            patch_live_validation_helper("_read_central_job_item", return_value=persisted),
        ):
            result = cleanup_workloads_module._cleanup_central_job(ctx, central_record)

        assert result["terminal_status"] == "succeeded"
        assert result["workload_not_submitted"] is False
        assert record["name"] == "gco-live-ddb-run-123"
        assert record["k8s_job_name"] == persisted["k8s_job_name"]
        assert record["k8s_job_namespace"] == persisted["k8s_job_namespace"]
        assert record["uid"] == persisted["k8s_job_uid"]

    def test_already_complete_cleanup_reconciles_missing_actual_identity(
        self,
        tmp_path: Path,
    ) -> None:
        ctx = _real_context(tmp_path)
        key = "gco-live-validation:run-123:central"
        job_id = checks_central_queue._central_queue_job_id(key)
        record = ctx.register_job(
            name="gco-live-ddb-run-123",
            namespace="gco-jobs",
            region="us-east-1",
            path="dynamodb",
            run_label=checks_jobs._run_token(ctx.settings.run_id),
            transport_region=None,
        )
        ctx.prepare_job_submission(
            record,
            envelope={
                "transport": "central-queue",
                "body": {"manifest": {"kind": "Job"}},
                "idempotency_key": key,
                "job_id": job_id,
                "transport_region": None,
            },
            resumable=True,
        )
        ctx.begin_job_submission(record, reconciliation_timeout_seconds=30)
        central_record = checks_central_queue._register_central_job(
            ctx,
            job_id=job_id,
            idempotency_key=key,
            record=record,
            marker="GCO_LIVE_DDB_run-123",
            body={"manifest": {"kind": "Job"}},
        )
        central_record.update(
            {
                "cleanup_complete": True,
                "status": "succeeded",
                "cleanup_result": {"terminal_status": "succeeded", "complete": True},
            }
        )
        record["deleted"] = True
        record["submission_state"] = "deleted"
        persisted = _central_job(job_id)

        def delete_reopened_workload(_ctx, workload):
            _ctx.mark_job_deleted(workload)
            return {"deleted_after_identity_reconciliation": True}

        with (
            patch_live_validation_helper("_read_central_job_item", return_value=persisted),
            patch_live_validation_helper(
                "_delete_owned_job",
                side_effect=delete_reopened_workload,
            ) as delete_job,
        ):
            result = cleanup_workloads_module.cleanup_workloads(ctx)

        assert result["complete"] is True
        delete_job.assert_called_once_with(ctx, record)
        assert record["deleted"] is True
        assert "requested_identity_deletion_superseded_at" in record
        assert record["name"] == "gco-live-ddb-run-123"
        assert record["k8s_job_name"] == persisted["k8s_job_name"]
        assert record["uid"] == persisted["k8s_job_uid"]

    @pytest.mark.parametrize("checkpoint_identity", ["partial", "conflicting"])
    def test_failed_central_reconciliation_never_mutates_or_deletes_workload(
        self,
        tmp_path: Path,
        checkpoint_identity: str,
    ) -> None:
        ctx = _real_context(tmp_path)
        key = "gco-live-validation:run-123:central"
        job_id = checks_central_queue._central_queue_job_id(key)
        record = ctx.register_job(
            name="gco-live-ddb-run-123",
            namespace="gco-jobs",
            region="us-east-1",
            path="dynamodb",
            run_label=checks_jobs._run_token(ctx.settings.run_id),
            transport_region=None,
        )
        ctx.prepare_job_submission(
            record,
            envelope={
                "transport": "central-queue",
                "body": {"manifest": {"kind": "Job"}},
                "idempotency_key": key,
                "job_id": job_id,
                "transport_region": None,
            },
            resumable=True,
        )
        ctx.begin_job_submission(record, reconciliation_timeout_seconds=30)
        central_record = checks_central_queue._register_central_job(
            ctx,
            job_id=job_id,
            idempotency_key=key,
            record=record,
            marker="GCO_LIVE_DDB_run-123",
            body={"manifest": {"kind": "Job"}},
        )
        central_record.update(
            {
                "cleanup_complete": True,
                "status": "succeeded",
                "cleanup_result": {"terminal_status": "succeeded", "complete": True},
            }
        )
        persisted = _central_job(job_id)
        if checkpoint_identity == "partial":
            central_record["k8s_job_name"] = persisted["k8s_job_name"]
        else:
            central_record.update(
                {
                    "k8s_job_name": persisted["k8s_job_name"],
                    "k8s_job_namespace": persisted["k8s_job_namespace"],
                    "k8s_job_uid": "conflicting-uid",
                    "k8s_identity_source": "dynamodb",
                }
            )
        workload_before = json.loads(json.dumps(record))

        with (
            patch_live_validation_helper("_read_central_job_item", return_value=persisted),
            patch_live_validation_helper("_delete_owned_job") as delete_job,
        ):
            result = cleanup_workloads_module.cleanup_workloads(ctx)

        assert result["complete"] is False
        assert record == workload_before
        delete_job.assert_not_called()
        assert any(item["resource"] == f"central:{job_id}" for item in result["errors"])
        assert any(
            "not reconciled from terminal DynamoDB evidence" in item["error"]
            for item in result["errors"]
            if item["resource"].startswith("us-east-1:")
        )

    def test_cleanup_non_observation_remains_an_unresolved_barrier(self) -> None:
        ctx = _context()
        job_id = checks_central_queue._central_queue_job_id("gco-live-validation:run-123:central")
        central_record = {
            "job_id": job_id,
            "idempotency_key": "gco-live-validation:run-123:central",
            "job_name": "gco-live-ddb-run-123",
            "namespace": "gco-jobs",
            "target_region": "us-east-1",
            "transport_region": None,
            "appearance_deadline": 9999999999.0,
        }

        with (
            patch_live_validation_helper("_wait_for_central_queue_appearance", return_value=None),
            pytest.raises(RuntimeError, match="non-observation is not terminal proof"),
        ):
            cleanup_workloads_module._cleanup_central_job(ctx, central_record)

        assert central_record["cleanup_complete"] is False
        assert central_record["cleanup_result"]["complete"] is False
        ctx.aws_client.make_authenticated_request.assert_not_called()


class TestDeterministicTopologyReadiness:
    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def _environment(
        self,
        regions: tuple[str, ...] = ("us-east-1",),
    ) -> SimpleNamespace:
        stack_names = {region: f"gco-live-{region}" for region in regions}
        stack_ids = {
            region: (
                f"arn:aws:cloudformation:{region}:123456789012:"
                f"stack/{stack_names[region]}/00000000-0000-0000-0000-000000000001"
            )
            for region in regions
        }
        tokens = {region: f"deployment-{region}" for region in regions}
        state_machine_arns = {
            region: (f"arn:aws:states:{region}:123456789012:stateMachine:gco-live-{region}-addons")
            for region in regions
        }
        execution_arns = {
            region: (
                f"arn:aws:states:{region}:123456789012:execution:"
                f"gco-live-{region}-addons:execution-1"
            )
            for region in regions
        }
        started_at = 1_721_260_800
        target_stack_regions = {stack_names[region]: region for region in regions}
        ctx = _context(
            state={
                "target_stack_regions": target_stack_regions,
                "enabled_regions": list(regions),
            }
        )
        ctx.deployment_regions = regions
        ctx.settings.poll_interval_seconds = 0
        ctx.session.get_partition_for_region.return_value = "aws"

        stacks: dict[str, dict[str, object]] = {}
        inputs: dict[str, dict[str, object]] = {}
        metadata: dict[str, dict[str, object]] = {}
        parameter_values: dict[str, str] = {}
        clients: dict[tuple[str, str], MagicMock] = {}
        target_groups: dict[str, list[dict[str, object]]] = {}
        target_group_tags: dict[str, dict[str, dict[str, str]]] = {}
        listeners: dict[str, list[dict[str, object]]] = {}
        listener_certificates: dict[str, list[dict[str, object]]] = {}
        events: list[str] = []

        for region in regions:
            stack_name = stack_names[region]
            stacks[stack_name] = {
                "name": stack_name,
                "stack_id": stack_ids[region],
                "status": "CREATE_COMPLETE",
                "outputs": {
                    "ClusterName": stack_name,
                    "AddonDeploymentToken": tokens[region],
                },
                "tags": {constants._RUN_STACK_TAG: "run-123"},
            }
            execution_input = {
                "ClusterName": stack_name,
                "Region": region,
                "RegistryRegion": ctx.config.global_region,
                "ProjectName": "gco-live",
                "EnabledCharts": ["keda"],
                "Charts": {"keda": {"version": "2.17.2"}},
                "KedaOperatorRoleArn": None,
                "ImageReplacements": {"IMAGE": "example.invalid/image@sha256:abc"},
                "EndpointGroupArn": (
                    "arn:aws:globalaccelerator::123456789012:accelerator/a/"
                    f"listener/b/endpoint-group/{region}"
                ),
                "DeploymentToken": tokens[region],
            }
            input_json = self._canonical(execution_input)
            execution_metadata = {
                "execution_arn": execution_arns[region],
                "state_machine_arn": state_machine_arns[region],
                "deployment_token": tokens[region],
                "cluster_name": stack_name,
                "region": region,
                "input_sha256": hashlib.sha256(input_json.encode("utf-8")).hexdigest(),
                "started_at": started_at,
            }
            inputs[region] = execution_input
            metadata[region] = execution_metadata
            parameter_root = f"/gco-live/addons/{region}"
            # Stored in the orchestrator's zlib+base64 encoding; input_sha256
            # above stays defined over the decoded canonical JSON.
            parameter_values[f"{parameter_root}/_input"] = base64.b64encode(
                zlib.compress(input_json.encode("utf-8"), 9)
            ).decode("ascii")
            parameter_values[f"{parameter_root}/_execution"] = self._canonical(execution_metadata)
            certificate_arn = (
                f"arn:aws:acm:{region}:123456789012:certificate/"
                f"00000000-0000-0000-0000-{region.replace('-', '')}"
            )
            parameter_values[f"/gco-live/backend-tls/certificate-arn/{region}"] = certificate_arn

            ssm = MagicMock()

            def get_parameter(*, Name: str, _region: str = region):
                events.append(f"ssm:{_region}:{Name.rsplit('/', 1)[-1]}")
                return {
                    "Parameter": {
                        "Name": Name,
                        "Type": "String",
                        "Value": parameter_values[Name],
                    }
                }

            ssm.get_parameter.side_effect = get_parameter
            clients[("ssm", region)] = ssm

            paginator = MagicMock()

            def paginate(*, StackName: str, _region: str = region):
                events.append(f"cfn:{_region}")
                assert StackName == stack_ids[_region]
                return [
                    {
                        "StackResourceSummaries": [
                            {
                                "LogicalResourceId": "HelmInstallStateMachineABC123",
                                "PhysicalResourceId": state_machine_arns[_region],
                                "ResourceType": "AWS::StepFunctions::StateMachine",
                                "ResourceStatus": "CREATE_COMPLETE",
                            }
                        ]
                    }
                ]

            paginator.paginate.side_effect = paginate
            cloudformation = MagicMock()
            cloudformation.get_paginator.return_value = paginator
            clients[("cloudformation", region)] = cloudformation

            terminal_output = {
                "manifestValidation": {
                    "status": "validated",
                    "DeploymentToken": tokens[region],
                    "ExpectedCount": 12,
                    "ValidatedCount": 12,
                },
                "helmValidation": {
                    "status": "validated",
                    "DeploymentToken": tokens[region],
                    "expected_release_count": 4,
                    "validated_release_count": 4,
                    "expected_resource_count": 18,
                    "validated_resource_count": 18,
                },
            }
            stepfunctions = MagicMock()
            terminal_response = {
                "executionArn": execution_arns[region],
                "stateMachineArn": state_machine_arns[region],
                "status": "SUCCEEDED",
                "startDate": started_at,
                "input": input_json,
                "output": self._canonical(terminal_output),
            }

            def describe_execution(
                *,
                executionArn: str,
                _region: str = region,
                _response: dict[str, object] = terminal_response,
            ):
                events.append(f"sfn:{_region}")
                assert executionArn == execution_arns[_region]
                return _response

            stepfunctions.describe_execution.side_effect = describe_execution
            stepfunctions.describe_execution.return_value = terminal_response
            clients[("stepfunctions", region)] = stepfunctions

            eks = MagicMock()
            eks.describe_cluster.return_value = {
                "cluster": {
                    "status": "ACTIVE",
                    "arn": f"arn:aws:eks:{region}:123456789012:cluster/{stack_name}",
                    "version": "1.33",
                    "resourcesVpcConfig": {
                        "endpointPublicAccess": False,
                        "endpointPrivateAccess": True,
                    },
                }
            }
            clients[("eks", region)] = eks

            load_balancer_arn = (
                f"arn:aws:elasticloadbalancing:{region}:123456789012:"
                f"loadbalancer/app/gco-live-{region}/abc123"
            )
            regional_target_groups = [
                {
                    "TargetGroupArn": (
                        f"arn:aws:elasticloadbalancing:{region}:123456789012:"
                        f"targetgroup/gco-live-{index}/tg{index}"
                    ),
                    "Protocol": "HTTPS",
                    # A named Kubernetes targetPort is represented by the
                    # controller as a group-wide sentinel; each registration
                    # below carries the effective pod port.
                    "Port": 1,
                    "HealthCheckProtocol": "HTTPS",
                    "HealthCheckPath": "/healthz",
                    "TargetType": "ip",
                }
                for index, _backend in enumerate(
                    ("health-monitor", "manifest-processor", "inference-proxy")
                )
            ]
            target_groups[region] = regional_target_groups
            regional_target_group_tags = {
                str(target_group["TargetGroupArn"]): {
                    "gco.aws/backend": backend,
                    "elbv2.k8s.aws/cluster": stack_name,
                }
                for target_group, backend in zip(
                    regional_target_groups,
                    ("health-monitor", "manifest-processor", "inference-proxy"),
                    strict=True,
                )
            }
            target_group_tags[region] = regional_target_group_tags
            regional_listeners = [
                {
                    "ListenerArn": f"{load_balancer_arn}/listener/https443",
                    "Protocol": "HTTPS",
                    "Port": 443,
                    "SslPolicy": "ELBSecurityPolicy-TLS13-1-2-2021-06",
                    "Certificates": [{"CertificateArn": certificate_arn}],
                }
            ]
            listeners[region] = regional_listeners
            regional_listener_certificates = [
                {"CertificateArn": certificate_arn, "IsDefault": True}
            ]
            listener_certificates[region] = regional_listener_certificates
            load_balancer_paginator = MagicMock()
            load_balancer_paginator.paginate.return_value = [
                {
                    "LoadBalancers": [
                        {
                            "LoadBalancerArn": load_balancer_arn,
                            "Scheme": "internal",
                            "Type": "application",
                            "State": {"Code": "active"},
                        }
                    ]
                }
            ]
            target_group_paginator = MagicMock()
            target_group_paginator.paginate.return_value = [
                {"TargetGroups": regional_target_groups}
            ]
            listener_paginator = MagicMock()
            listener_paginator.paginate.return_value = [{"Listeners": regional_listeners}]
            listener_certificate_paginator = MagicMock()
            listener_certificate_paginator.paginate.return_value = [
                {"Certificates": regional_listener_certificates}
            ]
            paginators = {
                "describe_load_balancers": load_balancer_paginator,
                "describe_target_groups": target_group_paginator,
                "describe_listeners": listener_paginator,
                "describe_listener_certificates": listener_certificate_paginator,
            }
            elbv2 = MagicMock()
            elbv2.get_paginator.side_effect = paginators.__getitem__

            def describe_tags(
                *,
                ResourceArns: list[str],
                _load_balancer_arn: str = load_balancer_arn,
                _target_group_tags: dict[str, dict[str, str]] = regional_target_group_tags,
                _stack_name: str = stack_name,
            ) -> dict[str, list[dict[str, object]]]:
                descriptions: list[dict[str, object]] = []
                for arn in ResourceArns:
                    if arn == _load_balancer_arn:
                        tags = {
                            "gco.aws/gateway": "gco-system/gco-gateway",
                            "elbv2.k8s.aws/cluster": _stack_name,
                        }
                    else:
                        tags = _target_group_tags.get(arn, {})
                    descriptions.append(
                        {
                            "ResourceArn": arn,
                            "Tags": [
                                {"Key": key, "Value": value} for key, value in sorted(tags.items())
                            ],
                        }
                    )
                return {"TagDescriptions": descriptions}

            elbv2.describe_tags.side_effect = describe_tags
            elbv2.describe_target_health.return_value = {
                "TargetHealthDescriptions": [
                    {
                        "Target": {
                            "Id": "10.0.1.10",
                            "Port": 8443,
                            "AvailabilityZone": f"{region}a",
                        },
                        "HealthCheckPort": "8443",
                        "TargetHealth": {"State": "healthy"},
                    },
                    {
                        "Target": {
                            "Id": "10.0.2.10",
                            "Port": 8443,
                            "AvailabilityZone": f"{region}b",
                        },
                        "HealthCheckPort": "8443",
                        "TargetHealth": {"State": "healthy"},
                    },
                ]
            }
            clients[("elbv2", region)] = elbv2

        dynamodb = MagicMock()
        dynamodb.describe_table.return_value = {
            "Table": {
                "TableStatus": "ACTIVE",
                "TableArn": "arn:aws:dynamodb:us-east-1:123456789012:table/gco-live-jobs",
            }
        }
        clients[("dynamodb", "us-east-1")] = dynamodb

        def client(service: str, *, region_name: str):
            return clients[(service, region_name)]

        ctx.session.client.side_effect = client
        ctx.job_manager.get_queue_status.return_value = {
            "messages_available": 0,
            "messages_in_flight": 0,
            "messages_delayed": 0,
            "dlq_messages": 0,
        }
        ctx.aws_client.get_api_endpoint.return_value = SimpleNamespace(
            url="https://global.example.test"
        )
        ctx.aws_client.get_regional_api_endpoint.side_effect = lambda region, **_: SimpleNamespace(
            url=f"https://{region}.example.test"
        )

        def call_api(*, method: str, path: str, region: str | None, max_attempts: int):
            assert method == "GET"
            assert path in ("/api/v1/health", "/api/v1/metrics")
            assert max_attempts == 1
            payload_region = region or regions[0]
            if path == "/api/v1/metrics":
                events.append(f"metrics:{region or 'global'}")
                return {
                    "cluster_id": f"gco-live-{payload_region}",
                    "region": payload_region,
                    "timestamp": "2026-07-18T00:00:00+00:00",
                    "status": "healthy",
                    "resource_utilization": {
                        "cpu_percent": 12.5,
                        "memory_percent": 33.0,
                        "gpu_percent": 0,
                    },
                    "thresholds": {
                        "cpu_threshold": 80,
                        "memory_threshold": 85,
                        "gpu_threshold": 90,
                    },
                    "active_jobs": 0,
                }
            events.append(f"health:{region or 'global'}")
            return {
                "status": "healthy",
                "timestamp": "2026-07-18T00:00:00+00:00",
                "region": payload_region,
                "cluster_id": f"gco-live-{payload_region}",
            }

        ctx.aws_client.call_api.side_effect = call_api
        sleep = MagicMock()
        return SimpleNamespace(
            ctx=ctx,
            stacks=stacks,
            clients=clients,
            target_groups=target_groups,
            target_group_tags=target_group_tags,
            listeners=listeners,
            listener_certificates=listener_certificates,
            events=events,
            inputs=inputs,
            metadata=metadata,
            parameter_values=parameter_values,
            stack_names=stack_names,
            stack_ids=stack_ids,
            state_machine_arns=state_machine_arns,
            execution_arns=execution_arns,
            tokens=tokens,
            sleep=sleep,
        )

    def _sync_execution_parameter(self, environment: SimpleNamespace, region: str) -> None:
        name = f"/gco-live/addons/{region}/_execution"
        environment.parameter_values[name] = self._canonical(environment.metadata[region])

    @staticmethod
    def _invoke(environment: SimpleNamespace) -> dict[str, object]:
        def describe(_session, region: str, stack_name: str):
            assert environment.ctx.checkpoint.state["target_stack_regions"][stack_name] == region
            return environment.stacks[stack_name]

        with (
            patch_live_validation_helper("_reconcile_stack_ownership"),
            patch_live_validation_helper("describe_stack", side_effect=describe),
            patch_live_validation_helper("_record_stack_identity"),
            patch("time.sleep", environment.sleep),
        ):
            return actions_topology.action_topology(environment.ctx)

    def test_exact_current_execution_succeeds_with_persisted_validator_evidence(self) -> None:
        environment = self._environment()

        result = self._invoke(environment)

        convergence = result["convergence"]
        regional = convergence["regions"]["us-east-1"]
        assert convergence["status"] == "succeeded"
        assert regional["result"] == "succeeded"
        assert regional["execution_status"] == "SUCCEEDED"
        assert regional["deployment_token"] == "deployment-us-east-1"
        assert regional["terminal"]["manifestValidation"]["ExpectedCount"] == 12
        assert regional["terminal"]["helmValidation"]["validated_resource_count"] == 18
        tls_evidence = result["alb_https_targets"]["us-east-1"]
        assert tls_evidence["scheme"] == "internal"
        assert tls_evidence["listener"] == {
            "listener_arn": (
                "arn:aws:elasticloadbalancing:us-east-1:123456789012:"
                "loadbalancer/app/gco-live-us-east-1/abc123/listener/https443"
            ),
            "protocol": "HTTPS",
            "port": 443,
            "ssl_policy": "ELBSecurityPolicy-TLS13-1-2-2021-06",
            "certificates": [
                "arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-useast1"
            ],
            "default_certificates": [
                "arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-useast1"
            ],
        }
        assert len(tls_evidence["target_groups"]) == 3
        assert {group["backend"] for group in tls_evidence["target_groups"]} == {
            "health-monitor",
            "manifest-processor",
            "inference-proxy",
        }
        assert all(group["default_port"] == 1 for group in tls_evidence["target_groups"])
        assert all(group["protocol"] == "HTTPS" for group in tls_evidence["target_groups"])
        assert all(
            group["health_check_protocol"] == "HTTPS" for group in tls_evidence["target_groups"]
        )
        assert all(
            group["registered_target_ports"] == [8443] for group in tls_evidence["target_groups"]
        )
        first_observation = tls_evidence["target_groups"][0]["health_observations"][0]
        assert {target["port"] for target in first_observation["registered_targets"]} == {8443}
        assert environment.ctx.checkpoint.state["topology_alb_https_targets"] == {
            "us-east-1": tls_evidence
        }
        assert environment.ctx.checkpoint.state["topology_convergence"] is convergence
        cloudformation = environment.clients[("cloudformation", "us-east-1")]
        cloudformation.get_paginator.assert_called_once_with("list_stack_resources")
        paginator = cloudformation.get_paginator.return_value
        assert paginator.paginate.call_args.kwargs == {
            "StackName": environment.stack_ids["us-east-1"]
        }
        environment.clients[
            ("stepfunctions", "us-east-1")
        ].describe_execution.assert_called_once_with(
            executionArn=environment.execution_arns["us-east-1"]
        )

    def test_health_calls_begin_only_after_every_region_converges(self) -> None:
        environment = self._environment(("us-east-1", "us-west-2"))

        self._invoke(environment)

        convergence_indexes = [
            index
            for index, event in enumerate(environment.events)
            if event.startswith(("ssm:", "cfn:", "sfn:"))
        ]
        health_indexes = [
            index for index, event in enumerate(environment.events) if event.startswith("health:")
        ]
        assert {event for event in environment.events if event.startswith("sfn:")} == {
            "sfn:us-east-1",
            "sfn:us-west-2",
        }
        assert max(convergence_indexes) < min(health_indexes)

    @pytest.mark.parametrize(
        ("stale_kind", "message"),
        [
            ("token", "stale deployment token"),
            ("digest", "stale input SHA-256"),
            ("stack-resource", "not exactly one AWS::StepFunctions::StateMachine resource"),
        ],
    )
    def test_stale_identity_or_stack_resource_is_rejected_before_health(
        self,
        stale_kind: str,
        message: str,
    ) -> None:
        environment = self._environment()
        region = "us-east-1"
        if stale_kind == "token":
            environment.metadata[region]["deployment_token"] = "stale-token"
            self._sync_execution_parameter(environment, region)
        elif stale_kind == "digest":
            environment.metadata[region]["input_sha256"] = "0" * 64
            self._sync_execution_parameter(environment, region)
        else:
            paginator = environment.clients[("cloudformation", region)].get_paginator.return_value
            paginator.paginate.side_effect = None
            paginator.paginate.return_value = [
                {
                    "StackResourceSummaries": [
                        {
                            "LogicalResourceId": "DifferentStateMachine",
                            "PhysicalResourceId": environment.state_machine_arns[region] + "-stale",
                            "ResourceType": "AWS::StepFunctions::StateMachine",
                            "ResourceStatus": "CREATE_COMPLETE",
                        }
                    ]
                }
            ]

        with pytest.raises(RuntimeError, match=message):
            self._invoke(environment)

        environment.ctx.aws_client.call_api.assert_not_called()
        evidence = environment.ctx.checkpoint.state["topology_convergence"]
        assert evidence["status"] == "failed"
        assert evidence["regions"][region]["result"] == "failed"

    def test_terminal_failure_persists_bounded_error_cause_and_output(self) -> None:
        environment = self._environment()
        region = "us-east-1"
        stepfunctions = environment.clients[("stepfunctions", region)]
        failed_response = dict(stepfunctions.describe_execution.return_value)
        failed_response.update(
            {
                "status": "FAILED",
                "error": "E" * 5000,
                "cause": "C" * 5000,
                "output": "O" * 5000,
            }
        )
        stepfunctions.describe_execution.side_effect = lambda **_: failed_response

        with pytest.raises(RuntimeError, match="ended FAILED"):
            self._invoke(environment)

        environment.ctx.aws_client.call_api.assert_not_called()
        terminal = environment.ctx.checkpoint.state["topology_convergence"]["regions"][region][
            "terminal"
        ]
        assert terminal["status"] == "FAILED"
        for field in ("error", "cause", "output"):
            assert len(terminal[field]) <= checks_topology._MAX_TOPOLOGY_EVIDENCE_CHARS
            assert terminal[field].endswith("... [truncated]")

    def test_non_https_listener_is_rejected_before_api_probes(self) -> None:
        environment = self._environment()
        environment.listeners["us-east-1"][0].update({"Protocol": "HTTP", "Port": 80})

        with pytest.raises(RuntimeError, match="exact HTTPS-only contract"):
            self._invoke(environment)

        environment.ctx.aws_client.call_api.assert_not_called()

    def test_unregistered_listener_certificate_is_rejected(self) -> None:
        environment = self._environment()
        environment.listeners["us-east-1"][0]["Certificates"] = [
            {
                "CertificateArn": (
                    "arn:aws:acm:us-east-1:123456789012:certificate/"
                    "11111111-1111-1111-1111-111111111111"
                )
            }
        ]

        with pytest.raises(RuntimeError, match="exact HTTPS-only contract"):
            self._invoke(environment)

        environment.ctx.aws_client.call_api.assert_not_called()

    def test_additional_sni_listener_certificate_is_rejected(self) -> None:
        environment = self._environment()
        environment.listener_certificates["us-east-1"].append(
            {
                "CertificateArn": (
                    "arn:aws:acm:us-east-1:123456789012:certificate/"
                    "22222222-2222-2222-2222-222222222222"
                ),
                "IsDefault": False,
            }
        )

        with pytest.raises(RuntimeError, match="exact HTTPS-only contract"):
            self._invoke(environment)

        environment.ctx.aws_client.call_api.assert_not_called()

    def test_plaintext_target_group_is_rejected_before_api_probes(self) -> None:
        environment = self._environment()
        environment.target_groups["us-east-1"][0]["Protocol"] = "HTTP"

        with pytest.raises(RuntimeError, match="not HTTPS-hardened"):
            self._invoke(environment)

        environment.ctx.aws_client.call_api.assert_not_called()

    def test_missing_target_group_backend_identity_is_rejected(self) -> None:
        environment = self._environment()
        first_arn = str(environment.target_groups["us-east-1"][0]["TargetGroupArn"])
        environment.target_group_tags["us-east-1"][first_arn].pop("gco.aws/backend")

        with pytest.raises(RuntimeError, match="invalid gco.aws/backend identity"):
            self._invoke(environment)

        environment.ctx.aws_client.call_api.assert_not_called()

    def test_duplicate_target_group_backend_identity_is_rejected(self) -> None:
        environment = self._environment()
        second_arn = str(environment.target_groups["us-east-1"][1]["TargetGroupArn"])
        environment.target_group_tags["us-east-1"][second_arn]["gco.aws/backend"] = "health-monitor"

        with pytest.raises(RuntimeError, match="duplicate target groups"):
            self._invoke(environment)

        environment.ctx.aws_client.call_api.assert_not_called()

    def test_missing_expected_target_group_backend_is_rejected(self) -> None:
        environment = self._environment()
        removed = environment.target_groups["us-east-1"].pop()
        environment.target_group_tags["us-east-1"].pop(str(removed["TargetGroupArn"]))

        with pytest.raises(RuntimeError, match="missing target groups.*inference-proxy"):
            self._invoke(environment)

        environment.ctx.aws_client.call_api.assert_not_called()

    def test_numeric_target_group_default_port_is_also_accepted(self) -> None:
        environment = self._environment()
        for target_group in environment.target_groups["us-east-1"]:
            target_group["Port"] = 8443

        result = self._invoke(environment)

        assert all(
            group["default_port"] == 8443
            for group in result["alb_https_targets"]["us-east-1"]["target_groups"]
        )

    def test_unexpected_target_group_default_port_is_rejected_before_api_probes(self) -> None:
        environment = self._environment()
        environment.target_groups["us-east-1"][0]["Port"] = 443

        with pytest.raises(RuntimeError, match="not HTTPS-hardened"):
            self._invoke(environment)

        environment.ctx.aws_client.call_api.assert_not_called()

    def test_wrong_registered_target_port_is_rejected_before_api_probes(self) -> None:
        environment = self._environment()
        elbv2 = environment.clients[("elbv2", "us-east-1")]
        elbv2.describe_target_health.return_value = {
            "TargetHealthDescriptions": [
                {
                    "Target": {
                        "Id": "10.0.1.10",
                        "Port": 9000,
                        "AvailabilityZone": "us-east-1a",
                    },
                    "HealthCheckPort": "9000",
                    "TargetHealth": {"State": "healthy"},
                }
            ]
        }

        with pytest.raises(RuntimeError, match="port other than 8443"):
            self._invoke(environment)

        environment.ctx.aws_client.call_api.assert_not_called()
        observation = environment.ctx.checkpoint.state["topology_alb_https_targets"]["us-east-1"][
            "target_groups"
        ][0]["health_observations"][0]
        assert observation["registered_targets"][0]["port"] == 9000

    def test_wrong_health_check_port_is_rejected_before_api_probes(self) -> None:
        environment = self._environment()
        elbv2 = environment.clients[("elbv2", "us-east-1")]
        elbv2.describe_target_health.return_value["TargetHealthDescriptions"][0][
            "HealthCheckPort"
        ] = "9000"

        with pytest.raises(RuntimeError, match="health-check port other than 8443"):
            self._invoke(environment)

        environment.ctx.aws_client.call_api.assert_not_called()

    def test_wrong_port_draining_target_is_polled_until_absent(self) -> None:
        environment = self._environment()
        elbv2 = environment.clients[("elbv2", "us-east-1")]
        healthy_response = elbv2.describe_target_health.return_value
        draining_response = {
            "TargetHealthDescriptions": [
                *healthy_response["TargetHealthDescriptions"],
                {
                    "Target": {
                        "Id": "10.0.3.10",
                        "Port": 9000,
                        "AvailabilityZone": "us-east-1c",
                    },
                    "HealthCheckPort": "9000",
                    "TargetHealth": {"State": "draining"},
                },
            ]
        }
        elbv2.describe_target_health.side_effect = [
            draining_response,
            healthy_response,
            healthy_response,
            healthy_response,
        ]

        result = self._invoke(environment)

        observations = result["alb_https_targets"]["us-east-1"]["target_groups"][0][
            "health_observations"
        ]
        assert [item["states"] for item in observations] == [
            ["healthy", "healthy", "draining"],
            ["healthy", "healthy"],
        ]
        environment.sleep.assert_any_call(1.0)

    def test_initial_target_registration_is_polled_to_healthy(self) -> None:
        environment = self._environment()
        elbv2 = environment.clients[("elbv2", "us-east-1")]

        def response(state: str) -> dict[str, object]:
            return {
                "TargetHealthDescriptions": [
                    {
                        "Target": {
                            "Id": "10.0.1.10",
                            "Port": 8443,
                            "AvailabilityZone": "us-east-1a",
                        },
                        "HealthCheckPort": "8443",
                        "TargetHealth": {"State": state},
                    }
                ]
            }

        healthy = response("healthy")
        elbv2.describe_target_health.side_effect = [
            response("initial"),
            healthy,
            healthy,
            healthy,
        ]

        result = self._invoke(environment)

        observations = result["alb_https_targets"]["us-east-1"]["target_groups"][0][
            "health_observations"
        ]
        assert [item["states"] for item in observations] == [["initial"], ["healthy"]]
        environment.sleep.assert_any_call(1.0)

    def test_target_group_without_healthy_https_target_is_rejected(self) -> None:
        environment = self._environment()
        elbv2 = environment.clients[("elbv2", "us-east-1")]
        elbv2.describe_target_health.return_value = {
            "TargetHealthDescriptions": [
                {
                    "Target": {
                        "Id": "10.0.1.10",
                        "Port": 8443,
                        "AvailabilityZone": "us-east-1a",
                    },
                    "HealthCheckPort": "8443",
                    "TargetHealth": {
                        "State": "unhealthy",
                        "Reason": "Target.FailedHealthChecks",
                    },
                }
            ]
        }

        with pytest.raises(RuntimeError, match="nonhealthy targets"):
            self._invoke(environment)

        environment.ctx.aws_client.call_api.assert_not_called()

    def test_first_504_is_checkpointed_as_warmup_before_strict_rounds(self) -> None:
        from cli.aws_client import APIRequestError

        environment = self._environment()
        default_call = environment.ctx.aws_client.call_api.side_effect
        failed_once = False

        def call_api(*, method: str, path: str, region: str | None, max_attempts: int):
            nonlocal failed_once
            if path == "/api/v1/health" and region is None and not failed_once:
                failed_once = True
                raise APIRequestError(504, "Endpoint request timed out")
            return default_call(
                method=method,
                path=path,
                region=region,
                max_attempts=max_attempts,
            )

        environment.ctx.aws_client.call_api.side_effect = call_api

        result = self._invoke(environment)

        health_calls = [
            call
            for call in environment.ctx.aws_client.call_api.call_args_list
            if call.kwargs["path"] == "/api/v1/health"
        ]
        assert len(health_calls) == 9
        assert all(call.kwargs["max_attempts"] == 1 for call in health_calls)
        warmup = environment.ctx.checkpoint.state["topology_health_warmup_samples"]
        assert len(warmup) == 3
        assert warmup[0]["scope"] == "global"
        assert warmup[0]["payload"] is None
        assert warmup[0]["status_code"] == 504
        assert "Endpoint request timed out" in warmup[0]["error"]
        assert warmup[1]["scope"] == "global" and warmup[1]["error"] is None
        assert warmup[2]["scope"] == "regional" and warmup[2]["error"] is None
        assert len(result["health_samples"]) == 6

    def test_warmup_resume_preserves_evidence_and_remaining_budget(self) -> None:
        environment = self._environment()
        previous = {
            "scope": "global",
            "region": None,
            "endpoint": "https://global.example.test",
            "attempt": 1,
            "timestamp": "2026-07-18T00:00:00+00:00",
            "latency_seconds": 28.0,
            "payload": None,
            "error": "APIRequestError: API request failed: Endpoint request timed out",
            "status_code": 504,
            "retryable": True,
        }
        environment.ctx.checkpoint.state["topology_health_warmup_samples"] = [previous]

        result = self._invoke(environment)

        warmup = environment.ctx.checkpoint.state["topology_health_warmup_samples"]
        assert warmup[0] == previous
        global_attempts = [sample["attempt"] for sample in warmup if sample["scope"] == "global"]
        assert global_attempts == [1, 2]
        assert len(result["health_samples"]) == 6

    def test_exhausted_checkpointed_warmup_budget_is_not_reset(self) -> None:
        environment = self._environment()
        environment.ctx.checkpoint.state["topology_health_warmup_samples"] = [
            {
                "scope": "global",
                "region": None,
                "endpoint": "https://global.example.test",
                "attempt": attempt,
                "timestamp": "2026-07-18T00:00:00+00:00",
                "latency_seconds": 28.0,
                "payload": None,
                "error": "APIRequestError: API request failed: Gateway timeout",
                "status_code": 504,
                "retryable": True,
            }
            for attempt in range(1, 4)
        ]

        with pytest.raises(RuntimeError, match="attempt budget is exhausted"):
            self._invoke(environment)

        environment.ctx.aws_client.call_api.assert_not_called()
        assert len(environment.ctx.checkpoint.state["topology_health_warmup_samples"]) == 3

    def test_strict_round_still_fails_after_successful_warmup(self) -> None:
        environment = self._environment()
        default_call = environment.ctx.aws_client.call_api.side_effect
        health_calls = 0

        def call_api(*, method: str, path: str, region: str | None, max_attempts: int):
            nonlocal health_calls
            if path == "/api/v1/health":
                health_calls += 1
                if health_calls == 3:
                    raise RuntimeError("API request failed: 504 Gateway Timeout")
            return default_call(
                method=method,
                path=path,
                region=region,
                max_attempts=max_attempts,
            )

        environment.ctx.aws_client.call_api.side_effect = call_api

        with pytest.raises(RuntimeError, match="Health stability call failed.*round 1"):
            self._invoke(environment)

        warmup = environment.ctx.checkpoint.state["topology_health_warmup_samples"]
        assert len(warmup) == 2 and all(sample["error"] is None for sample in warmup)
        strict = environment.ctx.checkpoint.state["topology_health_samples"]
        assert len(strict) == 1
        assert strict[0]["scope"] == "global" and "504" in strict[0]["error"]
        assert not any(event.startswith("metrics:") for event in environment.events)

    def test_health_warmup_exhaustion_fails_before_stability_samples(self) -> None:
        environment = self._environment()
        environment.ctx.aws_client.call_api.side_effect = [
            RuntimeError("API request failed: 504 Gateway Timeout"),
            RuntimeError("API request failed: 504 Gateway Timeout"),
            RuntimeError("API request failed: 504 Gateway Timeout"),
        ]

        with pytest.raises(RuntimeError, match="Health warm-up call failed.*attempt 3"):
            self._invoke(environment)

        warmup = environment.ctx.checkpoint.state["topology_health_warmup_samples"]
        assert len(warmup) == 3
        assert all("504" in sample["error"] for sample in warmup)
        assert "topology_health_samples" not in environment.ctx.checkpoint.state

    def test_nonretryable_health_warmup_error_fails_once(self) -> None:
        environment = self._environment()
        environment.ctx.aws_client.call_api.side_effect = RuntimeError(
            "API request failed: 403 Forbidden"
        )

        with pytest.raises(RuntimeError, match="Health warm-up call failed.*attempt 1"):
            self._invoke(environment)

        assert environment.ctx.aws_client.call_api.call_count == 1
        warmup = environment.ctx.checkpoint.state["topology_health_warmup_samples"]
        assert len(warmup) == 1
        assert "403" in warmup[0]["error"]

    def test_structured_nonretryable_status_overrides_transient_body_text(self) -> None:
        from cli.aws_client import APIRequestError

        environment = self._environment()
        environment.ctx.aws_client.call_api.side_effect = APIRequestError(
            403, "Service unavailable"
        )

        with pytest.raises(RuntimeError, match="Health warm-up call failed.*attempt 1"):
            self._invoke(environment)

        assert environment.ctx.aws_client.call_api.call_count == 1
        sample = environment.ctx.checkpoint.state["topology_health_warmup_samples"][0]
        assert sample["status_code"] == 403
        assert sample["retryable"] is False

    def test_malformed_checkpoint_outcome_cannot_skip_warmup(self) -> None:
        environment = self._environment()
        environment.ctx.checkpoint.state["topology_health_warmup_samples"] = [
            {
                "scope": "global",
                "region": None,
                "endpoint": "https://global.example.test",
                "attempt": 1,
                "timestamp": "2026-07-18T00:00:00+00:00",
                "latency_seconds": 1.0,
                "payload": {
                    "status": "healthy",
                    "timestamp": "2026-07-18T00:00:00+00:00",
                    "region": "us-east-1",
                    "cluster_id": "gco-live-us-east-1",
                },
                "status_code": 200,
                "retryable": False,
            }
        ]

        with pytest.raises(RuntimeError, match="checkpoint outcome is incomplete"):
            self._invoke(environment)

        environment.ctx.aws_client.call_api.assert_not_called()

    def test_malformed_200_fails_immediately_and_is_checkpointed(self) -> None:
        environment = self._environment()
        environment.ctx.aws_client.call_api.side_effect = None
        environment.ctx.aws_client.call_api.return_value = {
            "status": "healthy",
            "timestamp": "not-a-timestamp",
            "region": "us-east-1",
            "cluster_id": "gco-live-us-east-1",
        }

        with pytest.raises(RuntimeError, match="Malformed health warm-up response"):
            self._invoke(environment)

        assert environment.ctx.aws_client.call_api.call_count == 1
        sample = environment.ctx.checkpoint.state["topology_health_warmup_samples"][0]
        assert sample["payload"]["timestamp"] == "not-a-timestamp"
        assert "timestamp" in sample["error"]

    def test_three_rounds_cover_global_and_every_direct_regional_endpoint_once(self) -> None:
        environment = self._environment(("us-east-1", "us-west-2"))
        environment.ctx.settings.poll_interval_seconds = 9

        result = self._invoke(environment)

        calls = environment.ctx.aws_client.call_api.call_args_list
        health_calls = [item for item in calls if item.kwargs["path"] == "/api/v1/health"]
        assert len(health_calls) == 12
        assert [item.kwargs["region"] for item in health_calls] == [
            None,
            "us-east-1",
            "us-west-2",
            None,
            "us-east-1",
            "us-west-2",
            None,
            "us-east-1",
            "us-west-2",
            None,
            "us-east-1",
            "us-west-2",
        ]
        assert all(item.kwargs["max_attempts"] == 1 for item in calls)
        assert [sample["round"] for sample in result["health_samples"]] == [
            1,
            1,
            1,
            2,
            2,
            2,
            3,
            3,
            3,
        ]
        assert len(result["global_api"]["samples"]) == 3
        assert all(
            len(result["regional_apis"][region]["samples"]) == 3
            for region in environment.ctx.deployment_regions
        )
        assert [item.args for item in environment.sleep.call_args_list] == [(5.0,), (5.0,)]

    def test_metrics_probe_covers_global_and_every_direct_regional_endpoint_once(self) -> None:
        environment = self._environment(("us-east-1", "us-west-2"))

        result = self._invoke(environment)

        metrics_calls = [
            item
            for item in environment.ctx.aws_client.call_api.call_args_list
            if item.kwargs["path"] == "/api/v1/metrics"
        ]
        assert [item.kwargs["region"] for item in metrics_calls] == [
            None,
            "us-east-1",
            "us-west-2",
        ]
        assert all(item.kwargs["max_attempts"] == 1 for item in metrics_calls)
        samples = result["metrics_samples"]
        assert [sample["scope"] for sample in samples] == ["global", "regional", "regional"]
        assert all(sample["error"] is None for sample in samples)
        assert environment.ctx.checkpoint.state["topology_metrics_samples"] is samples
        # Health stability completes before the first metrics call, so a
        # metrics-routing failure is attributable to routing, not to an
        # unhealthy cluster.
        health_indexes = [
            index for index, event in enumerate(environment.events) if event.startswith("health:")
        ]
        metrics_indexes = [
            index for index, event in enumerate(environment.events) if event.startswith("metrics:")
        ]
        assert max(health_indexes) < min(metrics_indexes)

    def test_metrics_404_names_the_httproute_and_checkpoints_the_sample(self) -> None:
        """The pre-#196 regression: the ALB catch-all answered 404 for metrics."""
        environment = self._environment()
        healthy = {
            "status": "healthy",
            "timestamp": "2026-07-18T00:00:00+00:00",
            "region": "us-east-1",
            "cluster_id": "gco-live-us-east-1",
        }

        def call_api(*, method: str, path: str, region: str | None, max_attempts: int):
            if path == "/api/v1/metrics":
                raise RuntimeError("API request failed: 404 Not Found")
            return healthy

        environment.ctx.aws_client.call_api.side_effect = call_api

        with pytest.raises(RuntimeError, match="post-helm-gateway.yaml"):
            self._invoke(environment)

        samples = environment.ctx.checkpoint.state["topology_metrics_samples"]
        assert len(samples) == 1
        assert samples[0]["payload"] is None
        assert "404" in samples[0]["error"]

    def test_metrics_response_from_the_wrong_service_fails_shape_validation(self) -> None:
        """A 200 without the health monitor's utilization shape is not reachability."""
        environment = self._environment()
        healthy = {
            "status": "healthy",
            "timestamp": "2026-07-18T00:00:00+00:00",
            "region": "us-east-1",
            "cluster_id": "gco-live-us-east-1",
        }
        wrong_service = {
            "service": "GCO Manifest Processor API",
            "region": "us-east-1",
            "cluster_id": "gco-live-us-east-1",
        }

        def call_api(*, method: str, path: str, region: str | None, max_attempts: int):
            return wrong_service if path == "/api/v1/metrics" else healthy

        environment.ctx.aws_client.call_api.side_effect = call_api

        with pytest.raises(RuntimeError, match="Malformed metrics response.*resource_utilization"):
            self._invoke(environment)

        sample = environment.ctx.checkpoint.state["topology_metrics_samples"][0]
        assert sample["payload"]["service"] == "GCO Manifest Processor API"
        assert "resource_utilization" in sample["error"]


class TestRolledBackLogGroupCheckpoint:
    """A rolled-back create must not fail teardown over its own deleted groups.

    Live failure this pins (example-job validation run ex241-2913b044): the
    regional stack rolled back during create, CloudFormation deleted the
    non-retained state-machine log group, and the pre-destroy checkpoint's
    fail-closed absence check then aborted the guaranteed cleanup — stranding
    every remaining stack. Absence of a log group under a rolled-back stack is
    the expected rollback outcome and must be recorded, then skipped; under a
    live stack it stays a hard failure.
    """

    _REGION = "us-east-1"
    _STACK = "gco-live-us-east-1"
    _GROUP = "gco-live-us-east-1-HelmInstallStateMachineLogGroup-XYZ"

    def _environment(self, *, stack_status: str, resource_status: str) -> SimpleNamespace:
        ctx = _context(
            state={
                "target_stack_regions": {self._STACK: self._REGION},
                "log_group_cleanup_token": "b" * 32,
            }
        )
        ctx.checkpoint.created_at = "2026-08-11T00:00:00+00:00"

        cfn = MagicMock(name="cloudformation")
        cfn.get_paginator.return_value.paginate.return_value = [
            {
                "StackResourceSummaries": [
                    {
                        "ResourceType": "AWS::Logs::LogGroup",
                        "LogicalResourceId": "HelmInstallStateMachineLogGroup",
                        "PhysicalResourceId": self._GROUP,
                        "ResourceStatus": resource_status,
                    }
                ]
            }
        ]
        logs = MagicMock(name="logs")
        lambda_client = MagicMock(name="lambda")

        def client(service: str, *, region_name: str, **_kwargs):
            assert region_name == self._REGION
            return {"cloudformation": cfn, "logs": logs, "lambda": lambda_client}[service]

        ctx.session.client.side_effect = client
        stack_record = {
            "stack_id": (
                f"arn:aws:cloudformation:{self._REGION}:123456789012:stack/{self._STACK}/sid"
            )
        }
        live_stack = {
            "status": stack_status,
            "tags": {constants._RUN_STACK_TAG: "run-123"},
        }
        return SimpleNamespace(ctx=ctx, logs=logs, stack_record=stack_record, live_stack=live_stack)

    def _invoke(self, environment: SimpleNamespace) -> list[dict[str, object]]:
        with (
            patch_live_validation_helper(
                "_owned_stack_record", return_value=environment.stack_record
            ),
            patch_live_validation_helper("describe_stack", return_value=environment.live_stack),
            patch_live_validation_helper(
                "_validated_owned_log_group_identity",
                return_value=(self._REGION, self._GROUP),
            ),
            patch_live_validation_helper(
                "_observe_log_group_stability",
                return_value={"status": "absent"},
            ),
        ):
            return ownership_log_groups._checkpoint_owned_log_groups(environment.ctx)

    def test_rollback_deleted_group_is_recorded_and_skipped(self) -> None:
        environment = self._environment(
            stack_status="ROLLBACK_COMPLETE",
            # Rolled-back creates leave DELETE_COMPLETE tombstones; the widened
            # filter admits them precisely to catch groups that survived.
            resource_status="DELETE_COMPLETE",
        )
        self._invoke(environment)

        incidents = environment.ctx.checkpoint.state["log_group_checkpoint_incidents"]
        assert [item["phase"] for item in incidents] == ["checkpoint-explicit-group-absence"], (
            "the absent group must still be recorded as a checkpoint incident"
        )
        owned = environment.ctx.checkpoint.state.get("owned_log_groups", [])
        assert owned == [], "a genuinely deleted group must not be adopted"
        environment.logs.create_log_group.assert_not_called()

    def test_live_stack_missing_group_still_fails_closed(self) -> None:
        environment = self._environment(
            stack_status="CREATE_COMPLETE",
            resource_status="CREATE_COMPLETE",
        )
        with pytest.raises(RuntimeError, match="absent before teardown"):
            self._invoke(environment)


class TestRetainedLogCleanupGenerationFencing:
    _REGION = "us-east-1"
    _NAME = "gco-live-provider-log"
    _TOKEN = "a" * 32
    _ARN = f"arn:aws:logs:{_REGION}:123456789012:log-group:{_NAME}"

    @classmethod
    def _identity(
        cls,
        creation_time: int,
        *,
        run_tag: str = "run-123",
        cleanup_token: str | None = None,
    ) -> dict[str, object]:
        return {
            "arn": cls._ARN,
            "creation_time": creation_time,
            "tags": {
                constants._RUN_STACK_TAG: run_tag,
                constants._LOG_CLEANUP_TOKEN_TAG: cleanup_token or cls._TOKEN,
            },
        }

    @staticmethod
    def _retryable_error() -> ClientError:
        return ClientError(
            {
                "Error": {
                    "Code": "ThrottlingException",
                    "Message": "retry this observation",
                }
            },
            "DescribeLogGroups",
        )

    def _environment(self, observations: list[object]) -> SimpleNamespace:
        original = self._identity(1_750_000_000_000)
        record = {
            "region": self._REGION,
            "name": self._NAME,
            "stack_name": "gco-live-us-east-1",
            "stack_id": (
                "arn:aws:cloudformation:us-east-1:123456789012:stack/gco-live-us-east-1/stack-id"
            ),
            "source_resource_type": "AWS::Logs::LogGroup",
            "source_logical_id": "ProviderLogGroup",
            "source_physical_id": self._NAME,
            "ownership_authority": "cloudformation-stack-resource-derived",
            "authority_phase": "pre-destroy",
            "run_tag": "run-123",
            "cleanup_token": self._TOKEN,
            "observed_identity": original,
        }
        ctx = _context(
            state={
                "owned_log_groups": [record],
                "log_group_cleanup_token": self._TOKEN,
            }
        )
        normal_logs = MagicMock(name="normal-logs")
        restricted_logs = MagicMock(name="restricted-logs")
        sts = MagicMock(name="sts")
        role_name = "LiveValidationLogCleanup-test"
        role_arn = f"arn:aws:iam::123456789012:role/{role_name}"

        def assume_role(**kwargs):
            return {
                "Credentials": {
                    "AccessKeyId": "access-key",
                    "SecretAccessKey": "secret-key",
                    "SessionToken": "session-token",
                },
                "AssumedRoleUser": {
                    "Arn": (
                        "arn:aws:sts::123456789012:assumed-role/"
                        f"{role_name}/{kwargs['RoleSessionName']}"
                    )
                },
            }

        sts.assume_role.side_effect = assume_role

        def client(service: str, *, region_name: str, **kwargs):
            assert region_name == self._REGION
            if service == "logs":
                return restricted_logs if "aws_access_key_id" in kwargs else normal_logs
            if service == "sts":
                return sts
            raise AssertionError(f"Unexpected service client: {service}")

        ctx.session.client.side_effect = client
        helper = {
            "needed": True,
            "region": self._REGION,
            "stack_id": (
                "arn:aws:cloudformation:us-east-1:123456789012:"
                "stack/LiveValidationLogCleanup-test/helper-id"
            ),
            "role_arn": role_arn,
            "partition": "aws",
            "external_id": self._TOKEN,
            "session_policy": {"Version": "2012-10-17", "Statement": []},
        }
        return SimpleNamespace(
            ctx=ctx,
            record=record,
            original=original,
            normal_logs=normal_logs,
            restricted_logs=restricted_logs,
            sts=sts,
            helper=helper,
            identity_mock=MagicMock(side_effect=observations),
            ensure_mock=MagicMock(return_value=helper),
            helper_cleanup_mock=MagicMock(
                return_value={"needed": True, "deleted": True, "stack_id": helper["stack_id"]}
            ),
            sleep_mock=MagicMock(),
        )

    @staticmethod
    def _invoke(environment: SimpleNamespace) -> dict[str, object]:
        with (
            patch_live_validation_helper(
                "_verify_target_stack_absence",
                return_value={"all_absent": True, "residual": []},
            ),
            patch_live_validation_helper(
                "_validated_owned_log_group_identity",
                return_value=(
                    TestRetainedLogCleanupGenerationFencing._REGION,
                    TestRetainedLogCleanupGenerationFencing._NAME,
                ),
            ),
            patch_live_validation_helper("_ensure_log_cleanup_helper", environment.ensure_mock),
            patch_live_validation_helper(
                "_delete_log_cleanup_helper",
                environment.helper_cleanup_mock,
            ),
            patch_live_validation_helper("_log_group_identity", environment.identity_mock),
            patch("time.sleep", environment.sleep_mock),
        ):
            return cleanup_log_groups._cleanup_owned_log_groups(environment.ctx)

    def test_stable_generation_is_deleted_after_three_consecutive_absence_reads(self) -> None:
        environment = self._environment(
            [
                self._identity(1_750_000_000_000),
                self._identity(1_750_000_000_000),
                self._identity(1_750_000_000_000),
                None,
                None,
                None,
            ]
        )

        result = self._invoke(environment)

        assert result["errors"] == []
        assert result["log_groups"][0]["deleted"] is True
        assert result["log_groups"][0]["absence_observations"] == 3
        assert environment.record["deleted"] is True
        assert environment.record["original_generation_disposition"]["status"] == (
            "deleted-confirmed-absent"
        )
        environment.restricted_logs.delete_log_group.assert_called_once_with(
            logGroupName=self._NAME
        )
        environment.helper_cleanup_mock.assert_called_once_with(environment.ctx)
        assert [call.args for call in environment.sleep_mock.call_args_list] == [
            (1,),
            (1,),
            (1,),
        ]

    def test_confirmed_replacement_before_pending_is_preserved_and_reported(self) -> None:
        replacement = self._identity(1_750_000_000_999, run_tag="replacement")
        environment = self._environment([replacement, replacement])

        with pytest.raises(constants._LogGroupCleanupError) as raised:
            self._invoke(environment)

        environment.ensure_mock.assert_not_called()
        environment.restricted_logs.delete_log_group.assert_not_called()
        environment.helper_cleanup_mock.assert_called_once_with(environment.ctx)
        assert environment.record.get("deleted") is not True
        assert environment.record["original_generation_disposition"]["status"] == (
            "replacement-observed-before-delete"
        )
        assert environment.record["replacement_evidence"]
        blocked = raised.value.details["log_groups"][0]
        assert blocked["blocked"] is True
        assert (
            blocked["observation"]["observed_generation"]["creation_time"]
            == (replacement["creation_time"])
        )

    def test_replacement_immediately_before_delete_is_never_deleted(self) -> None:
        original = self._identity(1_750_000_000_000)
        replacement = self._identity(1_750_000_000_999, run_tag="replacement")
        environment = self._environment([original, original, replacement, replacement])

        with pytest.raises(constants._LogGroupCleanupError):
            self._invoke(environment)

        environment.ensure_mock.assert_called_once_with(environment.ctx)
        environment.restricted_logs.delete_log_group.assert_not_called()
        environment.helper_cleanup_mock.assert_called_once_with(environment.ctx)
        assert environment.record["original_generation_disposition"]["status"] == (
            "replacement-observed-immediately-before-delete"
        )

    def test_authority_tag_drift_blocks_delete_and_cleans_helper(self) -> None:
        drifted = self._identity(1_750_000_000_000, run_tag="foreign-run")
        environment = self._environment([drifted])

        with pytest.raises(constants._LogGroupCleanupError) as raised:
            self._invoke(environment)

        environment.ensure_mock.assert_not_called()
        environment.restricted_logs.delete_log_group.assert_not_called()
        environment.helper_cleanup_mock.assert_called_once_with(environment.ctx)
        assert environment.record["original_generation_disposition"]["status"] == (
            "authority-tag-drift-before-delete"
        )
        assert raised.value.details["log_groups"][0]["observation"]["tag_drift"]

    def test_untagged_regeneration_is_adopted_and_deleted(self) -> None:
        """Teardown-time Lambda log delivery recreates groups this run owns.

        Regression: a single regenerated group used to fence the whole sweep,
        stranding every other tagged group and failing final inventory on all
        of them. An untagged same-name generation observed under proven stack
        absence is adopted (re-tagged) and deleted instead.
        """
        regenerated = {
            "arn": self._ARN,
            "creation_time": 1_750_000_000_999,
            "tags": {},
        }
        adopted = self._identity(1_750_000_000_999)
        environment = self._environment(
            [
                regenerated,
                regenerated,
                adopted,
                adopted,
                adopted,
                None,
                None,
                None,
            ]
        )

        result = self._invoke(environment)

        assert result["errors"] == []
        entry = result["log_groups"][0]
        assert entry["deleted"] is True
        assert entry["adopted"] is True
        environment.normal_logs.tag_resource.assert_called_once_with(
            resourceArn=self._ARN,
            tags={
                constants._RUN_STACK_TAG: "run-123",
                constants._LOG_CLEANUP_TOKEN_TAG: self._TOKEN,
            },
        )
        environment.restricted_logs.delete_log_group.assert_called_once_with(
            logGroupName=self._NAME
        )
        adoptions = environment.record["adopted_generations"]
        assert len(adoptions) == 1
        assert adoptions[0]["generation"]["creation_time"] == 1_750_000_000_999
        assert environment.record["original_generation_disposition"]["status"] == (
            "deleted-confirmed-absent"
        )

    def test_foreign_cloudformation_generation_is_never_adopted(self) -> None:
        """A stack-tagged same-name generation belongs to a real deployment."""
        foreign = {
            "arn": self._ARN,
            "creation_time": 1_750_000_000_999,
            "tags": {"aws:cloudformation:stack-name": "gco-us-east-1"},
        }
        environment = self._environment([foreign, foreign])

        with pytest.raises(constants._LogGroupCleanupError) as raised:
            self._invoke(environment)

        environment.normal_logs.tag_resource.assert_not_called()
        environment.restricted_logs.delete_log_group.assert_not_called()
        blocked = raised.value.details["log_groups"][0]
        assert blocked["blocked"] is True
        assert blocked["retryable"] is False
        assert "cloudformation-owned" in json.dumps(blocked["observation"]["adoption_blockers"])

    def test_retryable_observation_error_resets_stability_without_failing_cleanup(self) -> None:
        original = self._identity(1_750_000_000_000)
        environment = self._environment(
            [self._retryable_error(), original, original, original, None, None, None]
        )

        result = self._invoke(environment)

        assert result["log_groups"][0]["deleted"] is True
        pending = environment.record["identity_observation_history"][0]
        assert pending["observations"][0]["status"] == "retryable-error"
        assert pending["attempt_count"] == 3
        environment.restricted_logs.delete_log_group.assert_called_once_with(
            logGroupName=self._NAME
        )

    def test_delete_then_same_name_recreation_is_preserved_and_reported(self) -> None:
        original = self._identity(1_750_000_000_000)
        replacement = self._identity(1_750_000_001_000, run_tag="replacement")
        environment = self._environment(
            [original, original, original, None, None, replacement, replacement]
        )

        with pytest.raises(constants._LogGroupCleanupError) as raised:
            self._invoke(environment)

        environment.restricted_logs.delete_log_group.assert_called_once_with(
            logGroupName=self._NAME
        )
        environment.helper_cleanup_mock.assert_called_once_with(environment.ctx)
        assert environment.record.get("deleted") is not True
        assert environment.record["original_generation_disposition"]["status"] == (
            "replacement-observed-before-confirmed-absence"
        )
        evidence = raised.value.details["log_groups"][0]["replacement_evidence"]
        assert (
            evidence[-1]["observed_generation"]["creation_time"] == (replacement["creation_time"])
        )

    def test_observation_retries_have_deterministic_bounded_timing(self) -> None:
        errors = [self._retryable_error() for _ in range(6)]
        identity_mock = MagicMock(side_effect=errors)
        sleep = MagicMock()

        with (
            patch_live_validation_helper("_log_group_identity", identity_mock),
            patch("time.sleep", sleep),
        ):
            outcome = ownership_log_groups._observe_log_group_stability(
                MagicMock(),
                self._REGION,
                self._NAME,
                expected_identity=self._identity(1_750_000_000_000),
                expected_tags={
                    constants._RUN_STACK_TAG: "run-123",
                    constants._LOG_CLEANUP_TOKEN_TAG: self._TOKEN,
                },
                required_present=2,
                required_absent=3,
                attempts=6,
                poll_seconds=0.25,
            )

        assert outcome["status"] == "unsettled"
        assert outcome["attempt_count"] == 6
        assert identity_mock.call_count == 6
        assert [call.args for call in sleep.call_args_list] == [(0.25,)] * 5


class TestStrictStackOwnership:
    _STACK_ID = "arn:aws:cloudformation:us-east-1:123456789012:stack/gco-live-global/stack-uuid"

    def _stack(self) -> dict[str, object]:
        return {
            "name": "gco-live-global",
            "stack_id": self._STACK_ID,
            "status": "CREATE_COMPLETE",
            "tags": {constants._RUN_STACK_TAG: "run-123"},
        }

    def test_deploy_never_delegates_private_report_path_to_cdk(self) -> None:
        ctx = _context(
            state={
                "target_stack_regions": {"gco-live-global": "us-east-1"},
                "bootstrap_stacks": {"us-east-1": {"stack_id": "bootstrap-id"}},
                "owned_stacks": {},
            }
        )
        ctx.stack_manager.deploy_orchestrated.return_value = (
            True,
            ["gco-live-global"],
            [],
        )

        with (
            patch_live_validation_helper("_reconcile_stack_ownership"),
            patch_live_validation_helper("_checkpoint_new_ecr_repositories"),
            patch_live_validation_helper("_checkpoint_new_ecr_images"),
            patch_live_validation_helper("_checkpoint_retained_kms_keys"),
        ):
            result = actions_deploy.action_deploy(ctx)

        assert result["overall_success"] is True
        call_kwargs = ctx.stack_manager.deploy_orchestrated.call_args.kwargs
        assert "outputs_file" not in call_kwargs

    def test_live_observation_cannot_create_destructive_authority(self) -> None:
        ctx = _context(state={"owned_stacks": {}})

        with pytest.raises(RuntimeError, match="without prepared-change-set authority"):
            ownership_stacks._record_stack_identity(
                ctx,
                "gco-live-global",
                "us-east-1",
                self._stack(),
            )

        assert ctx.checkpoint.state["owned_stacks"] == {}

    def test_prepared_identity_can_be_verified_but_not_name_adopted(self) -> None:
        ctx = _context(
            state={
                "owned_stacks": {},
                "target_stack_regions": {"gco-live-global": "us-east-1"},
            }
        )
        first_change_set_id = (
            "arn:aws:cloudformation:us-east-1:123456789012:changeSet/run-123/change-set-uuid"
        )
        ownership_stacks._record_prepared_stack_identity(
            ctx,
            "gco-live-global",
            "us-east-1",
            self._STACK_ID,
            first_change_set_id,
            "CREATE",
        )

        record = ownership_stacks._record_stack_identity(
            ctx,
            "gco-live-global",
            "us-east-1",
            self._stack(),
        )

        assert record["authority"] == "prepared-change-set"
        assert record["stack_id"] == self._STACK_ID
        assert record["prepared_change_sets"][first_change_set_id] == {
            "change_set_id": first_change_set_id,
            "stack_id": self._STACK_ID,
            "change_set_type": "CREATE",
        }

        second_change_set_id = (
            "arn:aws:cloudformation:us-east-1:123456789012:"
            "changeSet/run-123-analytics-routes/second-uuid"
        )
        ownership_stacks._record_prepared_stack_identity(
            ctx,
            "gco-live-global",
            "us-east-1",
            self._STACK_ID,
            second_change_set_id,
            "UPDATE",
        )
        authority = ownership_stacks._prepared_change_set_authority(ctx)
        assert set(authority["gco-live-global"]) == {
            first_change_set_id,
            second_change_set_id,
        }
        assert authority["gco-live-global"][first_change_set_id]["change_set_type"] == "CREATE"
        assert authority["gco-live-global"][second_change_set_id]["change_set_type"] == "UPDATE"

    def test_legacy_change_set_history_is_migrated_before_new_records(self) -> None:
        legacy_id = (
            "arn:aws:cloudformation:us-east-1:123456789012:changeSet/run-123-base/legacy-uuid"
        )
        second_id = (
            "arn:aws:cloudformation:us-east-1:123456789012:"
            "changeSet/run-123-analytics-routes/second-uuid"
        )
        third_id = (
            "arn:aws:cloudformation:us-east-1:123456789012:"
            "changeSet/run-123-teardown-drop-analytics-routes/third-uuid"
        )
        ctx = _context(
            state={
                "target_stack_regions": {"gco-live-global": "us-east-1"},
                "owned_stacks": {
                    "us-east-1": {
                        "gco-live-global": {
                            "name": "gco-live-global",
                            "region": "us-east-1",
                            "stack_id": self._STACK_ID,
                            "run_tag": "run-123",
                            "authority": "prepared-change-set",
                            "change_set_id": legacy_id,
                            "change_set_type": "CREATE",
                        }
                    }
                },
            }
        )

        for change_set_id in (second_id, third_id):
            ownership_stacks._record_prepared_stack_identity(
                ctx,
                "gco-live-global",
                "us-east-1",
                self._STACK_ID,
                change_set_id,
                "UPDATE",
            )

        reloaded = _context(state=json.loads(json.dumps(ctx.checkpoint.state)))
        authority = ownership_stacks._prepared_change_set_authority(reloaded)["gco-live-global"]
        assert set(authority) == {legacy_id, second_id, third_id}
        assert authority[legacy_id]["change_set_type"] == "CREATE"
        assert authority[second_id]["change_set_type"] == "UPDATE"
        assert authority[third_id]["change_set_type"] == "UPDATE"


class TestEcrOwnershipCleanup:
    _IDENTITY = {
        "digest": "sha256:abc",
        "manifest_media_type": "application/vnd.oci.image.manifest.v1+json",
        "artifact_media_type": "",
        "manifest": {"schemaVersion": 2},
    }
    _DOCKER_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v2+json"

    @staticmethod
    def _manifest_record(
        media_type: str,
        manifest: object,
        *,
        digest: str = "sha256:abc",
        tag: str | None = None,
    ) -> dict[str, object]:
        image_id = {"imageDigest": digest}
        if tag is not None:
            image_id["imageTag"] = tag
        return {
            "imageId": image_id,
            "imageManifestMediaType": media_type,
            "imageManifest": json.dumps(manifest, sort_keys=True),
        }

    def _ecr_session_with_records(
        self,
        records: list[dict[str, object]],
        *,
        native_media_type: str | None = None,
    ) -> tuple[MagicMock, MagicMock]:
        media_type = native_media_type or str(self._IDENTITY["manifest_media_type"])
        ecr = MagicMock()
        ecr.describe_images.return_value = {
            "imageDetails": [
                {
                    "imageDigest": "sha256:abc",
                    "imageTags": ["run-tag"],
                    "imageManifestMediaType": media_type,
                }
            ]
        }
        ecr.batch_get_image.return_value = {"images": records, "failures": []}
        session = MagicMock()
        session.client.return_value = ecr
        return ecr, session

    def test_describe_tag_captures_exact_manifest(self) -> None:
        ecr = MagicMock()
        ecr.describe_images.return_value = {
            "imageDetails": [
                {
                    "imageDigest": "sha256:abc",
                    "imageTags": ["run-tag"],
                    "imageManifestMediaType": self._IDENTITY["manifest_media_type"],
                }
            ]
        }
        ecr.batch_get_image.return_value = {
            "images": [
                {
                    "imageId": {"imageDigest": "sha256:abc"},
                    "imageManifestMediaType": self._IDENTITY["manifest_media_type"],
                    "imageManifest": json.dumps(self._IDENTITY["manifest"]),
                }
            ],
            "failures": [],
        }
        session = MagicMock()
        session.client.return_value = ecr

        result = inventory.describe_ecr_image_by_tag(
            session,
            region="us-east-1",
            repository_name="baseline/repository",
            tag="run-tag",
        )

        assert ownership_ecr._ecr_image_identity(result or {}) == self._IDENTITY
        ecr.describe_images.assert_called_once_with(
            repositoryName="baseline/repository",
            imageIds=[{"imageTag": "run-tag"}],
        )
        ecr.batch_get_image.assert_called_once_with(
            repositoryName="baseline/repository",
            imageIds=[{"imageDigest": "sha256:abc"}],
            acceptedMediaTypes=list(inventory_ecr._ECR_MANIFEST_MEDIA_TYPES),
        )

    def test_describe_tag_collapses_exact_duplicate_manifest_records(self) -> None:
        record = self._manifest_record(
            str(self._IDENTITY["manifest_media_type"]),
            self._IDENTITY["manifest"],
            tag="first-tag",
        )
        duplicate = self._manifest_record(
            str(self._IDENTITY["manifest_media_type"]),
            self._IDENTITY["manifest"],
            tag="second-tag",
        )
        _ecr, session = self._ecr_session_with_records([record, duplicate])

        result = inventory.describe_ecr_image_by_tag(
            session,
            region="us-east-1",
            repository_name="baseline/repository",
            tag="run-tag",
        )

        assert ownership_ecr._ecr_image_identity(result or {}) == self._IDENTITY

    @pytest.mark.parametrize("reverse", [False, True])
    def test_describe_tag_selects_native_manifest_independent_of_order(self, reverse: bool) -> None:
        native_manifest = {"schemaVersion": 2, "annotations": {"representation": "native"}}
        translated_manifest = {
            "schemaVersion": 2,
            "annotations": {"representation": "translated"},
        }
        native = self._manifest_record(str(self._IDENTITY["manifest_media_type"]), native_manifest)
        translated = self._manifest_record(self._DOCKER_MEDIA_TYPE, translated_manifest)
        records = [native, translated]
        if reverse:
            records.reverse()
        _ecr, session = self._ecr_session_with_records(records)

        result = inventory.describe_ecr_image_by_tag(
            session,
            region="us-east-1",
            repository_name="baseline/repository",
            tag="run-tag",
        )

        assert result is not None
        assert result["manifest_media_type"] == self._IDENTITY["manifest_media_type"]
        assert result["manifest"] == native_manifest

    def test_describe_tag_rejects_conflicting_native_manifests(self) -> None:
        media_type = str(self._IDENTITY["manifest_media_type"])
        first = self._manifest_record(media_type, {"schemaVersion": 2, "variant": 1})
        second = self._manifest_record(media_type, {"schemaVersion": 2, "variant": 2})
        _ecr, session = self._ecr_session_with_records([first, second])

        with pytest.raises(RuntimeError, match="2 unique native record"):
            inventory.describe_ecr_image_by_tag(
                session,
                region="us-east-1",
                repository_name="baseline/repository",
                tag="run-tag",
            )

    @pytest.mark.parametrize("reverse", [False, True])
    def test_bulk_inventory_uses_same_native_manifest_resolver(self, reverse: bool) -> None:
        native_manifest = {"schemaVersion": 2, "kind": "native"}
        native = self._manifest_record(str(self._IDENTITY["manifest_media_type"]), native_manifest)
        translated = self._manifest_record(
            self._DOCKER_MEDIA_TYPE,
            {"schemaVersion": 2, "kind": "translated"},
        )
        records = [native, translated]
        if reverse:
            records.reverse()
        ecr, _session = self._ecr_session_with_records(records)
        ecr.get_paginator.return_value.paginate.return_value = [
            {
                "imageDetails": [
                    {
                        "imageDigest": "sha256:abc",
                        "imageTags": ["run-tag"],
                        "imageManifestMediaType": self._IDENTITY["manifest_media_type"],
                    }
                ]
            }
        ]

        images = inventory_ecr._collect_repository_images(ecr, "baseline/repository")

        assert len(images) == 1
        assert images[0]["manifest_media_type"] == self._IDENTITY["manifest_media_type"]
        assert images[0]["manifest"] == native_manifest

    @pytest.mark.parametrize(
        "error_code",
        ("ImageNotFoundException", "RepositoryNotFoundException"),
    )
    def test_describe_tag_returns_none_only_for_authoritative_absence(
        self,
        error_code: str,
    ) -> None:
        ecr = MagicMock()
        ecr.describe_images.side_effect = ClientError(
            {"Error": {"Code": error_code, "Message": "missing"}},
            "DescribeImages",
        )
        session = MagicMock()
        session.client.return_value = ecr

        assert (
            inventory.describe_ecr_image_by_tag(
                session,
                region="us-east-1",
                repository_name="baseline/repository",
                tag="run-tag",
            )
            is None
        )

    def test_describe_tag_rejects_ambiguous_empty_response(self) -> None:
        ecr = MagicMock()
        ecr.describe_images.return_value = {"imageDetails": []}
        session = MagicMock()
        session.client.return_value = ecr

        with pytest.raises(RuntimeError, match="omitted an authoritative identity"):
            inventory.describe_ecr_image_by_tag(
                session,
                region="us-east-1",
                repository_name="baseline/repository",
                tag="run-tag",
            )

    def test_tag_delta_is_revalidated_and_retained_without_deletion(self) -> None:
        record = {
            "region": "us-east-1",
            "repository": "baseline/repository",
            "tag": "run-tag",
            "identity": self._IDENTITY,
            "cleanup_policy": "retain-no-conditional-delete",
        }
        ctx = _context(state={"retained_ecr_image_deltas": [record]})
        current = {**self._IDENTITY, "tags": ["run-tag"]}

        with patch_live_validation_helper(
            "describe_ecr_image_by_tag",
            return_value=current,
        ) as describe:
            result = cleanup_ecr._cleanup_new_ecr_images(ctx)

        assert result["automatic_deletion"] is False
        assert result["images"][0]["retained"] is True
        describe.assert_called_once_with(
            ctx.session,
            region="us-east-1",
            repository_name="baseline/repository",
            tag="run-tag",
        )
        ctx.session.client.return_value.batch_delete_image.assert_not_called()

    def test_repository_delete_revalidates_run_tag(self) -> None:
        repository = {
            "name": "gco-live/new",
            "arn": "arn:aws:ecr:us-east-1:123456789012:repository/gco-live/new",
            "registry_id": "123456789012",
            "created_at": "2026-07-17T00:00:00+00:00",
            "tags": {"GcoLiveValidationRun": "different-run"},
            "images": [],
        }
        record = {
            "region": "us-east-1",
            "name": repository["name"],
            "arn": repository["arn"],
            "creation_identity": ownership_ecr._ecr_creation_identity(repository),
            "run_tag": "run-123",
            "deleted": False,
        }
        ctx = _context(
            state={
                "created_ecr_repositories": [record],
                "expected_ecr_images": [
                    {
                        "region": "us-east-1",
                        "repository": repository["name"],
                        "tag": "asset-tag",
                    }
                ],
            }
        )

        with (
            patch_live_validation_helper(
                "collect_ecr_inventory",
                return_value={"us-east-1": [repository]},
            ),
            pytest.raises(RuntimeError, match="run ownership changed"),
        ):
            cleanup_ecr._cleanup_new_ecr_repositories(ctx)

        ctx.session.client.return_value.delete_repository.assert_not_called()

    def test_final_inventory_accepts_only_exact_checkpointed_ecr_residuals(self) -> None:
        baseline_repository = {
            "name": "baseline/repository",
            "arn": "arn:aws:ecr:us-east-1:123456789012:repository/baseline/repository",
            "registry_id": "123456789012",
            "created_at": "2026-07-01T00:00:00+00:00",
            "tags": {},
            "images": [],
        }
        created_repository = {
            "name": "gco-live/new",
            "arn": "arn:aws:ecr:us-east-1:123456789012:repository/gco-live/new",
            "registry_id": "123456789012",
            "created_at": "2026-07-17T00:00:00+00:00",
            "tags": {constants._RUN_STACK_TAG: "run-123"},
            "images": [{**self._IDENTITY, "tags": ["asset-tag"]}],
        }
        delta_image = {**self._IDENTITY, "tags": ["run-tag"]}
        final_baseline = {
            "protected_stacks": {},
            "ecr_repositories": {
                "us-east-1": [
                    {**baseline_repository, "images": [delta_image]},
                    created_repository,
                ]
            },
        }
        state = {
            "created_ecr_repositories": [
                {
                    "region": "us-east-1",
                    "name": created_repository["name"],
                    "arn": created_repository["arn"],
                    "creation_identity": ownership_ecr._ecr_creation_identity(created_repository),
                    "run_tag": "run-123",
                }
            ],
            "retained_ecr_image_deltas": [
                {
                    "region": "us-east-1",
                    "repository": baseline_repository["name"],
                    "tag": "run-tag",
                    "identity": self._IDENTITY,
                }
            ],
        }
        ctx = _context(state=state)
        ctx.checkpoint.baseline = {
            "protected_stacks": {},
            "ecr_regions": ["us-east-1"],
            "ecr_repositories": {"us-east-1": [baseline_repository]},
        }

        comparison, accepted = ownership_ecr._strip_expected_retained_ecr(ctx, final_baseline)

        assert comparison["ecr_repositories"] == ctx.checkpoint.baseline["ecr_repositories"]
        assert accepted["repositories"][0]["retained"] is True
        assert accepted["image_deltas"][0]["retained"] is True

        project_inventory = {
            "regional": {
                "us-east-1": {
                    "ecr_repositories": [
                        baseline_repository["name"],
                        created_repository["name"],
                    ]
                }
            }
        }
        residual = ownership_ecr._strip_baseline_ecr(project_inventory, ctx.checkpoint.baseline)
        residual = ownership_ecr._strip_accepted_retained_ecr(residual, accepted)
        assert residual["regional"] == {}


class TestProtectedBaselineIdentity:
    _REGION = "us-east-1"
    _STACK_ID = (
        "arn:aws:cloudformation:us-east-1:123456789012:stack/GCOGitHubOIDCStack/protected-uuid"
    )
    _ROLE_NAME = "gco-live-protected-role"
    _ECR_ARN = "arn:aws:ecr:us-east-1:123456789012:repository/gco-live/protected"

    def _baseline(self) -> dict[str, object]:
        return {
            "protected_stacks": {
                self._REGION: [
                    {
                        "name": "GCOGitHubOIDCStack",
                        "stack_id": self._STACK_ID,
                        "physical_resources": [
                            {
                                "logical_id": "GitHubActionsRole",
                                "resource_type": "AWS::IAM::Role",
                                "physical_id": self._ROLE_NAME,
                            }
                        ],
                    }
                ]
            },
            "ecr_repositories": {
                self._REGION: [
                    {
                        "name": "gco-live/protected",
                        "arn": self._ECR_ARN,
                    }
                ]
            },
        }

    def test_stack_fingerprint_captures_paginated_resources_in_stable_order(self) -> None:
        cloudformation = MagicMock()
        cloudformation.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackName": "GCOGitHubOIDCStack",
                    "StackId": self._STACK_ID,
                    "StackStatus": "CREATE_COMPLETE",
                }
            ]
        }
        cloudformation.get_template.return_value = {"TemplateBody": {"Resources": {}}}
        cloudformation.get_stack_policy.return_value = {"StackPolicyBody": {"Statement": []}}
        paginator = cloudformation.get_paginator.return_value
        paginator.paginate.return_value = [
            {
                "StackResourceSummaries": [
                    {
                        "LogicalResourceId": "ZRole",
                        "ResourceType": "AWS::IAM::Role",
                        "PhysicalResourceId": "z-role",
                    }
                ]
            },
            {
                "StackResourceSummaries": [
                    {
                        "LogicalResourceId": "AProvider",
                        "ResourceType": "Custom::Provider",
                        "PhysicalResourceId": "provider-identity",
                    }
                ]
            },
        ]
        session = MagicMock()
        session.client.return_value = cloudformation

        result = inventory.describe_stack_fingerprint(
            session,
            self._REGION,
            "GCOGitHubOIDCStack",
        )

        assert result is not None
        assert result["physical_resources"] == [
            {
                "logical_id": "AProvider",
                "resource_type": "Custom::Provider",
                "physical_id": "provider-identity",
            },
            {
                "logical_id": "ZRole",
                "resource_type": "AWS::IAM::Role",
                "physical_id": "z-role",
            },
        ]
        cloudformation.get_paginator.assert_called_once_with("list_stack_resources")
        paginator.paginate.assert_called_once_with(StackName=self._STACK_ID)

    def test_comparison_reports_protected_physical_identity_drift(self) -> None:
        expected = self._baseline()
        actual = json.loads(json.dumps(expected))
        actual["protected_stacks"][self._REGION][0]["physical_resources"][0]["physical_id"] = (
            f"{self._ROLE_NAME}-replacement"
        )

        differences = inventory.compare_baseline(expected, actual)

        assert len(differences) == 1
        assert differences[0]["category"] == "protected_stacks"
        assert differences[0]["region"] == self._REGION

    def test_filter_removes_only_exact_stack_role_and_ecr_identities(self) -> None:
        replacement_stack_id = self._STACK_ID.replace("protected-uuid", "replacement-uuid")
        exact_role_arn = f"arn:aws:iam::123456789012:role/path/{self._ROLE_NAME}"
        nearby_role_arn = f"{exact_role_arn}-extra"
        nearby_ecr_arn = f"{self._ECR_ARN}-extra"
        project_inventory = {
            "cloudformation_stacks": {
                self._REGION: [
                    {
                        "name": "GCOGitHubOIDCStack",
                        "stack_id": self._STACK_ID,
                    },
                    {
                        "name": "GCOGitHubOIDCStack",
                        "stack_id": replacement_stack_id,
                    },
                ]
            },
            "regional": {
                self._REGION: {
                    "tagged_resources": [
                        {"arn": self._ECR_ARN, "tags": {}},
                        {"arn": nearby_ecr_arn, "tags": {}},
                    ],
                    "ecr_repositories": [
                        "gco-live/protected",
                        "gco-live/protected-extra",
                    ],
                }
            },
            "iam_roles": [exact_role_arn, nearby_role_arn],
        }

        filtered = ownership_ecr._strip_baseline_ecr(project_inventory, self._baseline())

        assert filtered["cloudformation_stacks"] == {
            self._REGION: [
                {
                    "name": "GCOGitHubOIDCStack",
                    "stack_id": replacement_stack_id,
                }
            ]
        }
        assert filtered["iam_roles"] == [nearby_role_arn]
        assert filtered["regional"][self._REGION]["ecr_repositories"] == [
            "gco-live/protected-extra"
        ]
        assert filtered["regional"][self._REGION]["tagged_resources"] == [
            {"arn": nearby_ecr_arn, "tags": {}}
        ]

    def test_filter_removes_only_exact_protected_target_group(self) -> None:
        target_group_arn = (
            "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/protected/abc123"
        )
        baseline = self._baseline()
        baseline["protected_stacks"][self._REGION][0]["physical_resources"].append(
            {
                "logical_id": "ProtectedTargetGroup",
                "resource_type": "AWS::ElasticLoadBalancingV2::TargetGroup",
                "physical_id": target_group_arn,
            }
        )
        project_inventory = {
            "regional": {
                self._REGION: {"target_groups": [target_group_arn, f"{target_group_arn}-other"]}
            }
        }

        filtered = ownership_ecr._strip_baseline_ecr(project_inventory, baseline)

        assert filtered["regional"][self._REGION]["target_groups"] == [f"{target_group_arn}-other"]

    def test_filter_requires_complete_protected_resource_authority(self) -> None:
        baseline = self._baseline()
        del baseline["protected_stacks"][self._REGION][0]["physical_resources"]

        with pytest.raises(RuntimeError, match="omitted physical resources"):
            ownership_ecr._strip_baseline_ecr({}, baseline)

    @pytest.mark.parametrize("protected_stacks", [[], "", 0, False])
    def test_filter_rejects_falsey_non_object_stack_authority(
        self, protected_stacks: object
    ) -> None:
        with pytest.raises(RuntimeError, match="protected_stacks must be an object"):
            ownership_ecr._strip_baseline_ecr({}, {"protected_stacks": protected_stacks})

    @pytest.mark.parametrize("baseline", [{}, {"protected_stacks": None}])
    def test_missing_or_null_protected_stack_authority_is_empty(
        self, baseline: dict[str, object]
    ) -> None:
        assert protected._baseline_protected_identities(baseline) == ({}, {})

    def test_backup_inventory_matches_cloudformation_physical_ids(self) -> None:
        plan_id = "plan-123"
        other_plan_id = "plan-789"
        selection_id = "selection-456"
        vault_name = "gco-live-vault"
        baseline = {
            "protected_stacks": {
                self._REGION: [
                    {
                        "stack_id": self._STACK_ID,
                        "physical_resources": [
                            {
                                "logical_id": "BackupPlan",
                                "resource_type": "AWS::Backup::BackupPlan",
                                "physical_id": plan_id,
                            },
                            {
                                "logical_id": "BackupSelection",
                                "resource_type": "AWS::Backup::BackupSelection",
                                "physical_id": selection_id,
                            },
                            {
                                "logical_id": "BackupVault",
                                "resource_type": "AWS::Backup::BackupVault",
                                "physical_id": vault_name,
                            },
                        ],
                    }
                ]
            }
        }
        plan_arn = f"arn:aws:backup:{self._REGION}:123456789012:backup-plan:{plan_id}"
        vault_arn = f"arn:aws:backup:{self._REGION}:123456789012:backup-vault:{vault_name}"
        project_inventory = {
            "regional": {
                self._REGION: {
                    "backup_plans": [plan_arn, f"{plan_arn}-other"],
                    "backup_selections": [
                        f"{plan_id}:{selection_id}",
                        f"{other_plan_id}:{selection_id}",
                        f"{plan_id}:{selection_id}-other",
                    ],
                    "backup_vaults": [vault_arn, f"{vault_arn}-other"],
                }
            }
        }

        filtered = ownership_ecr._strip_baseline_ecr(project_inventory, baseline)

        assert filtered["regional"][self._REGION] == {
            "backup_plans": [f"{plan_arn}-other"],
            "backup_selections": [
                f"{other_plan_id}:{selection_id}",
                f"{plan_id}:{selection_id}-other",
            ],
            "backup_vaults": [f"{vault_arn}-other"],
        }

    def test_tagged_inventory_matches_exact_stack_and_physical_identities(self) -> None:
        account_id = "123456789012"
        lambda_name = "gco-protected-function"
        table_name = "gco-protected-table"
        bucket_name = "gco-protected-bucket"
        queue_name = "gco-protected-queue"
        queue_url = f"https://sqs.{self._REGION}.amazonaws.com/{account_id}/{queue_name}"
        key_id = "12345678-1234-1234-1234-123456789012"
        baseline = {
            "protected_stacks": {
                self._REGION: [
                    {
                        "stack_id": self._STACK_ID,
                        "physical_resources": [
                            {
                                "logical_id": "Function",
                                "resource_type": "AWS::Lambda::Function",
                                "physical_id": lambda_name,
                            },
                            {
                                "logical_id": "Table",
                                "resource_type": "AWS::DynamoDB::Table",
                                "physical_id": table_name,
                            },
                            {
                                "logical_id": "Bucket",
                                "resource_type": "AWS::S3::Bucket",
                                "physical_id": bucket_name,
                            },
                            {
                                "logical_id": "Queue",
                                "resource_type": "AWS::SQS::Queue",
                                "physical_id": queue_url,
                            },
                            {
                                "logical_id": "Key",
                                "resource_type": "AWS::KMS::Key",
                                "physical_id": key_id,
                            },
                        ],
                    }
                ]
            }
        }
        exact_arns = [
            f"arn:aws:lambda:{self._REGION}:{account_id}:function:{lambda_name}",
            f"arn:aws:dynamodb:{self._REGION}:{account_id}:table/{table_name}",
            f"arn:aws:s3:::{bucket_name}",
            f"arn:aws:sqs:{self._REGION}:{account_id}:{queue_name}",
            f"arn:aws:kms:{self._REGION}:{account_id}:key/{key_id}",
        ]
        nearby_arns = [f"{arn}-other" for arn in exact_arns]
        stack_tagged_arn = (
            f"arn:aws:ssm:{self._REGION}:{account_id}:parameter/protected-stack-resource"
        )
        nearby_stack_id = self._STACK_ID.replace("protected-uuid", "replacement-uuid")
        project_inventory = {
            "regional": {
                self._REGION: {
                    "tagged_resources": [
                        *({"arn": arn, "tags": {}} for arn in exact_arns),
                        *({"arn": arn, "tags": {}} for arn in nearby_arns),
                        {
                            "arn": stack_tagged_arn,
                            "tags": {"aws:cloudformation:stack-id": self._STACK_ID},
                        },
                        {
                            "arn": f"{stack_tagged_arn}-other",
                            "tags": {"aws:cloudformation:stack-id": nearby_stack_id},
                        },
                    ]
                }
            }
        }

        filtered = ownership_ecr._strip_baseline_ecr(project_inventory, baseline)

        assert filtered["regional"][self._REGION]["tagged_resources"] == [
            *({"arn": arn, "tags": {}} for arn in nearby_arns),
            {
                "arn": f"{stack_tagged_arn}-other",
                "tags": {"aws:cloudformation:stack-id": nearby_stack_id},
            },
        ]

    def test_authoritative_eks_inventory_keeps_unrelated_cluster_names(self) -> None:
        eks = MagicMock()
        eks.get_paginator.return_value.paginate.return_value = [
            {"clusters": ["unrelated", "gco-live-west"]},
            {"clusters": ["gco-live", "unrelated"]},
        ]
        session = MagicMock()
        session.client.return_value = eks

        assert inventory_scanners._list_eks_clusters(session, self._REGION, None) == [
            "gco-live",
            "gco-live-west",
            "unrelated",
        ]
        assert inventory_scanners._list_eks_clusters(session, self._REGION, "gco-live") == [
            "gco-live",
            "gco-live-west",
        ]

    def test_protected_networking_inventory_matches_exact_physical_ids(self) -> None:
        nat_gateway_id = "nat-11111111111111111"
        flow_log_id = "fl-22222222222222222"
        nat_gateway_arn = f"arn:aws:ec2:{self._REGION}:123456789012:natgateway/{nat_gateway_id}"
        flow_log_arn = f"arn:aws:ec2:{self._REGION}:123456789012:vpc-flow-log/{flow_log_id}"
        nearby_nat_gateway_arn = (
            f"arn:aws:ec2:{self._REGION}:123456789012:natgateway/nat-33333333333333333"
        )
        nearby_flow_log_arn = (
            f"arn:aws:ec2:{self._REGION}:123456789012:vpc-flow-log/fl-44444444444444444"
        )
        wrong_account_nat_gateway_arn = nat_gateway_arn.replace(
            "123456789012",
            "999999999999",
        )
        wrong_region_flow_log_arn = flow_log_arn.replace(self._REGION, "us-west-2")
        wrong_partition_nat_gateway_arn = nat_gateway_arn.replace(
            "arn:aws:",
            "arn:aws-us-gov:",
        )
        baseline = {
            "protected_stacks": {
                self._REGION: [
                    {
                        "stack_id": self._STACK_ID,
                        "physical_resources": [
                            {
                                "logical_id": "NatGateway",
                                "resource_type": "AWS::EC2::NatGateway",
                                "physical_id": nat_gateway_id,
                            },
                            {
                                "logical_id": "FlowLog",
                                "resource_type": "AWS::EC2::FlowLog",
                                "physical_id": flow_log_id,
                            },
                        ],
                    }
                ]
            }
        }
        project_inventory = {
            "authority_scope": {"partition": "aws", "account": "123456789012"},
            "regional": {
                self._REGION: {
                    "tagged_resources": [
                        {"arn": nat_gateway_arn, "tags": {}},
                        {"arn": flow_log_arn, "tags": {}},
                        {"arn": nearby_nat_gateway_arn, "tags": {}},
                        {"arn": nearby_flow_log_arn, "tags": {}},
                        {"arn": wrong_account_nat_gateway_arn, "tags": {}},
                        {"arn": wrong_region_flow_log_arn, "tags": {}},
                        {"arn": wrong_partition_nat_gateway_arn, "tags": {}},
                    ],
                    "nat_gateways": [nat_gateway_id, "nat-33333333333333333"],
                    "flow_logs": [flow_log_id, "fl-44444444444444444"],
                }
            },
        }

        filtered = ownership_ecr._strip_baseline_ecr(project_inventory, baseline)

        assert filtered["regional"][self._REGION] == {
            "tagged_resources": [
                {"arn": nearby_nat_gateway_arn, "tags": {}},
                {"arn": nearby_flow_log_arn, "tags": {}},
                {"arn": wrong_account_nat_gateway_arn, "tags": {}},
                {"arn": wrong_region_flow_log_arn, "tags": {}},
                {"arn": wrong_partition_nat_gateway_arn, "tags": {}},
            ],
            "nat_gateways": ["nat-33333333333333333"],
            "flow_logs": ["fl-44444444444444444"],
        }

    def test_protected_networking_tagged_arns_require_trusted_scope(self) -> None:
        nat_gateway_id = "nat-11111111111111111"
        nat_gateway_arn = f"arn:aws:ec2:{self._REGION}:123456789012:natgateway/{nat_gateway_id}"
        baseline = {
            "protected_stacks": {
                self._REGION: [
                    {
                        "stack_id": self._STACK_ID,
                        "physical_resources": [
                            {
                                "logical_id": "NatGateway",
                                "resource_type": "AWS::EC2::NatGateway",
                                "physical_id": nat_gateway_id,
                            }
                        ],
                    }
                ]
            }
        }
        project_inventory = {
            "regional": {
                self._REGION: {
                    "tagged_resources": [{"arn": nat_gateway_arn, "tags": {}}],
                }
            }
        }

        filtered = ownership_ecr._strip_baseline_ecr(project_inventory, baseline)

        assert filtered["regional"][self._REGION]["tagged_resources"] == [
            {"arn": nat_gateway_arn, "tags": {}}
        ]

    def test_instance_inventory_separates_project_owned_from_all_active_ids(self) -> None:
        project_instance = "i-11111111111111111"
        unrelated_instance = "i-22222222222222222"
        ec2 = MagicMock()
        paginator = ec2.get_paginator.return_value
        paginator.paginate.return_value = [
            {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": project_instance,
                                "Tags": [{"Key": "gco:project", "Value": "gco-live"}],
                            },
                            {"InstanceId": unrelated_instance, "Tags": []},
                        ]
                    }
                ]
            }
        ]
        session = MagicMock()
        session.client.return_value = ec2

        project_ids, all_ids = inventory_scanners._list_instance_inventory(
            session,
            self._REGION,
            "gco-live",
        )

        assert project_ids == [project_instance]
        assert all_ids == [project_instance, unrelated_instance]
        ec2.get_paginator.assert_called_once_with("describe_instances")
        paginator.paginate.assert_called_once_with(
            Filters=[
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped", "shutting-down"],
                }
            ]
        )

    def test_cluster_volume_scanner_claims_only_project_cluster_volumes(self) -> None:
        """CSI-provisioned volumes carry only a Kubernetes tag, never a project tag."""
        ec2 = MagicMock()
        paginator = ec2.get_paginator.return_value
        paginator.paginate.return_value = [
            {
                "Volumes": [
                    {
                        "VolumeId": "vol-00000000000000001",
                        "Tags": [
                            {
                                "Key": "kubernetes.io/cluster/gco-live-us-east-1",
                                "Value": "owned",
                            },
                            {
                                "Key": "kubernetes.io/created-for/pvc/name",
                                "Value": "prometheus-db",
                            },
                        ],
                    },
                    {
                        "VolumeId": "vol-00000000000000002",
                        "Tags": [
                            {
                                "Key": "kubernetes.io/cluster/other-project-us-east-1",
                                "Value": "owned",
                            }
                        ],
                    },
                    {"VolumeId": "vol-00000000000000003", "Tags": []},
                ]
            }
        ]
        session = MagicMock()
        session.client.return_value = ec2

        volumes = inventory_scanners._list_cluster_volumes(session, self._REGION, "gco-live")

        assert volumes == ["vol-00000000000000001"]
        ec2.get_paginator.assert_called_once_with("describe_volumes")
        # Enumerated unfiltered and matched client-side, like the sibling EC2
        # scanners: no dependence on server-side tag-key wildcard semantics.
        paginator.paginate.assert_called_once_with()

    def test_cluster_volume_scanner_ignores_volumes_already_being_deleted(self) -> None:
        """EKS Auto Mode node root volumes share the cluster tag and die with
        their instances; one observed mid-``deleting`` is not a leak."""
        ec2 = MagicMock()
        ec2.get_paginator.return_value.paginate.return_value = [
            {
                "Volumes": [
                    {
                        "VolumeId": "vol-00000000000000010",
                        "State": "deleting",
                        "Tags": [
                            {"Key": "kubernetes.io/cluster/gco-live-us-east-1", "Value": "owned"}
                        ],
                    },
                    {
                        "VolumeId": "vol-00000000000000011",
                        "State": "deleted",
                        "Tags": [
                            {"Key": "kubernetes.io/cluster/gco-live-us-east-1", "Value": "owned"}
                        ],
                    },
                ]
            }
        ]
        session = MagicMock()
        session.client.return_value = ec2

        assert inventory_scanners._list_cluster_volumes(session, self._REGION, "gco-live") == []

    @pytest.mark.parametrize("state", ["available", "in-use", "creating", "error"])
    def test_cluster_volume_scanner_counts_every_non_terminal_state(self, state: str) -> None:
        """After teardown nothing should hold a cluster-tagged volume at all."""
        ec2 = MagicMock()
        ec2.get_paginator.return_value.paginate.return_value = [
            {
                "Volumes": [
                    {
                        "VolumeId": "vol-00000000000000020",
                        "State": state,
                        "Tags": [
                            {"Key": "kubernetes.io/cluster/gco-live-us-east-1", "Value": "owned"}
                        ],
                    }
                ]
            }
        ]
        session = MagicMock()
        session.client.return_value = ec2

        volumes = inventory_scanners._list_cluster_volumes(session, self._REGION, "gco-live")

        assert volumes == ["vol-00000000000000020"]

    def test_cluster_volume_scanner_rejects_a_volume_without_an_id(self) -> None:
        ec2 = MagicMock()
        ec2.get_paginator.return_value.paginate.return_value = [{"Volumes": [{"Tags": []}]}]
        session = MagicMock()
        session.client.return_value = ec2

        with pytest.raises(RuntimeError, match="volume without an ID"):
            inventory_scanners._list_cluster_volumes(session, self._REGION, "gco-live")

    def test_ec2_networking_inventory_separates_owned_from_live_authority(self) -> None:
        project_vpc = "vpc-11111111111111111"
        unrelated_vpc = "vpc-22222222222222222"
        project_subnet = "subnet-11111111111111111"
        unrelated_subnet = "subnet-22222222222222222"
        project_nat = "nat-11111111111111111"
        unrelated_nat = "nat-22222222222222222"
        deleted_nat = "nat-33333333333333333"
        project_group = "sg-11111111111111111"
        unrelated_group = "sg-22222222222222222"
        project_interface = "eni-11111111111111111"
        unrelated_interface = "eni-22222222222222222"
        project_flow_log = "fl-11111111111111111"
        unrelated_flow_log = "fl-22222222222222222"
        project_eip = "eipalloc-11111111111111111"
        unrelated_eip = "eipalloc-22222222222222222"
        pages = {
            "describe_vpcs": [
                {
                    "Vpcs": [
                        {
                            "VpcId": project_vpc,
                            "Tags": [{"Key": "gco:project", "Value": "gco-live"}],
                        }
                    ]
                },
                {"Vpcs": [{"VpcId": unrelated_vpc, "Tags": []}]},
            ],
            "describe_subnets": [
                {
                    "Subnets": [
                        {"SubnetId": project_subnet, "VpcId": project_vpc, "Tags": []},
                        {"SubnetId": unrelated_subnet, "VpcId": unrelated_vpc, "Tags": []},
                    ]
                }
            ],
            "describe_nat_gateways": [
                {
                    "NatGateways": [
                        {
                            "NatGatewayId": project_nat,
                            "VpcId": project_vpc,
                            "SubnetId": project_subnet,
                            "State": "available",
                        },
                        {
                            "NatGatewayId": unrelated_nat,
                            "VpcId": unrelated_vpc,
                            "SubnetId": unrelated_subnet,
                            "State": "available",
                        },
                        {
                            "NatGatewayId": deleted_nat,
                            "State": "deleted",
                            "Tags": [{"Key": "gco:project", "Value": "gco-live"}],
                        },
                    ]
                }
            ],
            "describe_security_groups": [
                {
                    "SecurityGroups": [
                        {"GroupId": project_group, "VpcId": project_vpc, "Tags": []},
                        {"GroupId": unrelated_group, "VpcId": unrelated_vpc, "Tags": []},
                    ]
                }
            ],
            "describe_network_interfaces": [
                {
                    "NetworkInterfaces": [
                        {
                            "NetworkInterfaceId": project_interface,
                            "VpcId": project_vpc,
                            "SubnetId": project_subnet,
                            "Groups": [{"GroupId": project_group}],
                        },
                        {
                            "NetworkInterfaceId": unrelated_interface,
                            "VpcId": unrelated_vpc,
                            "SubnetId": unrelated_subnet,
                            "Groups": [{"GroupId": unrelated_group}],
                        },
                    ]
                }
            ],
            "describe_flow_logs": [
                {
                    "FlowLogs": [
                        {"FlowLogId": project_flow_log, "ResourceId": project_vpc},
                        {"FlowLogId": unrelated_flow_log, "ResourceId": unrelated_vpc},
                    ]
                }
            ],
        }
        paginators = {}
        for operation, operation_pages in pages.items():
            paginator = MagicMock()
            paginator.paginate.return_value = operation_pages
            paginators[operation] = paginator
        ec2 = MagicMock()
        ec2.get_paginator.side_effect = paginators.__getitem__
        ec2.describe_addresses.return_value = {
            "Addresses": [
                {"AllocationId": project_eip, "NetworkInterfaceId": project_interface},
                {"AllocationId": unrelated_eip, "NetworkInterfaceId": unrelated_interface},
            ]
        }
        session = MagicMock()
        session.client.return_value = ec2

        project_resources, authoritative_resources = (
            inventory_scanners._list_project_ec2_networking(
                session,
                self._REGION,
                "gco-live",
                ["i-11111111111111111"],
            )
        )

        assert project_resources == {
            "vpcs": [project_vpc],
            "subnets": [project_subnet],
            "nat_gateways": [project_nat],
            "flow_logs": [project_flow_log],
            "network_interfaces": [project_interface],
            "security_groups": [project_group],
            "elastic_ips": [project_eip],
        }
        assert authoritative_resources == {
            "vpcs": [project_vpc, unrelated_vpc],
            "subnets": [project_subnet, unrelated_subnet],
            "nat_gateways": [project_nat, unrelated_nat],
            "flow_logs": [project_flow_log, unrelated_flow_log],
            "network_interfaces": [project_interface, unrelated_interface],
            "security_groups": [project_group, unrelated_group],
            "elastic_ips": [project_eip, unrelated_eip],
        }
        assert deleted_nat not in authoritative_resources["nat_gateways"]
        assert paginators["describe_vpcs"].paginate.call_count == 1

    def test_filter_suppresses_only_authoritatively_absent_ec2_records(self) -> None:
        live_unowned_subnet = "subnet-aaaaaaaaaaaaaaaaa"
        absent_subnet = "subnet-bbbbbbbbbbbbbbbbb"
        absent_nat = "nat-ccccccccccccccccc"
        absent_flow_log = "fl-ddddddddddddddddd"
        absent_instance = "i-eeeeeeeeeeeeeeeee"
        retained_arns = [
            f"arn:aws:ec2:{self._REGION}:123456789012:subnet/{live_unowned_subnet}",
            f"arn:aws-us-gov:ec2:{self._REGION}:123456789012:subnet/{absent_subnet}",
            f"arn:aws:ec2:{self._REGION}:999999999999:subnet/{absent_subnet}",
            f"arn:aws:ec2:us-west-2:123456789012:subnet/{absent_subnet}",
            f"arn:aws:ec2:{self._REGION}:123456789012:subnet/{absent_subnet}-nearby",
            f"arn:aws:ec2:{self._REGION}:123456789012:subnet/subnet-not-hexadecimal",
        ]
        absent_arns = [
            f"arn:aws:ec2:{self._REGION}:123456789012:subnet/{absent_subnet}",
            f"arn:aws:ec2:{self._REGION}:123456789012:natgateway/{absent_nat}",
            f"arn:aws:ec2:{self._REGION}:123456789012:vpc-flow-log/{absent_flow_log}",
            f"arn:aws:ec2:{self._REGION}:123456789012:instance/{absent_instance}",
        ]
        project_inventory = {
            "cloudformation_stacks": {},
            "authority_scope": {"partition": "aws", "account": "123456789012"},
            "coverage": {
                "complete": True,
                "completed_scanners": ["ec2_instances", "ec2_networking"],
                "scanner_regions": {
                    "ec2_instances": [self._REGION],
                    "ec2_networking": [self._REGION],
                },
            },
            "authoritative_ec2_resources": {
                self._REGION: {
                    "instances": [],
                    "subnets": [live_unowned_subnet],
                    "nat_gateways": [],
                    "flow_logs": [],
                }
            },
            "regional": {
                self._REGION: {
                    "tagged_resources": [
                        *({"arn": arn, "tags": {}} for arn in absent_arns),
                        *({"arn": arn, "tags": {}} for arn in retained_arns),
                    ]
                }
            },
        }

        filtered = ownership_ecr._strip_baseline_ecr(
            project_inventory,
            {"protected_stacks": {}, "ecr_repositories": {}},
        )

        assert filtered["regional"][self._REGION]["tagged_resources"] == [
            {"arn": arn, "tags": {}} for arn in retained_arns
        ]

    @pytest.mark.parametrize(
        "authority_case",
        [
            "missing-complete",
            "false-complete",
            "missing-completed-scanners",
            "malformed-scope",
            "unscanned-region",
        ],
    )
    def test_tag_reconciliation_requires_complete_scoped_authority(
        self,
        authority_case: str,
    ) -> None:
        ec2_arn = f"arn:aws:ec2:{self._REGION}:123456789012:subnet/subnet-aaaaaaaaaaaaaaaaa"
        eks_arn = (
            f"arn:aws:eks:{self._REGION}:123456789012:"
            "pod/gco-live-orphan/default/example/11111111-1111-1111-1111-111111111111"
        )
        coverage: dict[str, object] = {
            "complete": True,
            "completed_scanners": ["ec2_instances", "ec2_networking", "eks_clusters"],
            "scanner_regions": {
                "ec2_instances": [self._REGION],
                "ec2_networking": [self._REGION],
                "eks_clusters": [self._REGION],
            },
        }
        authority_scope = {"partition": "aws", "account": "123456789012"}
        if authority_case == "missing-complete":
            coverage.pop("complete")
        elif authority_case == "false-complete":
            coverage["complete"] = False
        elif authority_case == "missing-completed-scanners":
            coverage["completed_scanners"] = []
        elif authority_case == "malformed-scope":
            authority_scope["account"] = "not-an-account"
        elif authority_case == "unscanned-region":
            coverage["scanner_regions"] = {
                "ec2_instances": [],
                "ec2_networking": [],
                "eks_clusters": [],
            }
        project_inventory = {
            "cloudformation_stacks": {},
            "authority_scope": authority_scope,
            "coverage": coverage,
            "authoritative_eks_clusters": {self._REGION: []},
            "authoritative_ec2_resources": {self._REGION: {"subnets": []}},
            "regional": {
                self._REGION: {
                    "tagged_resources": [
                        {"arn": ec2_arn, "tags": {}},
                        {"arn": eks_arn, "tags": {}},
                    ]
                }
            },
        }

        filtered = ownership_ecr._strip_baseline_ecr(
            project_inventory,
            {"protected_stacks": {}, "ecr_repositories": {}},
        )

        assert filtered["regional"][self._REGION]["tagged_resources"] == [
            {"arn": ec2_arn, "tags": {}},
            {"arn": eks_arn, "tags": {}},
        ]

    def test_filter_suppresses_only_authoritatively_orphaned_eks_pods(self) -> None:
        existing_pod = (
            "arn:aws:eks:us-east-1:123456789012:"
            "pod/gco-live-existing/default/example/11111111-1111-1111-1111-111111111111"
        )
        orphaned_pod = (
            "arn:aws:eks:us-east-1:123456789012:"
            "pod/gco-live-orphan/default/example/22222222-2222-2222-2222-222222222222"
        )
        existing_association = (
            "arn:aws:eks:us-east-1:123456789012:"
            "podidentityassociation/gco-live-existing/a-11111111111111111"
        )
        orphaned_association = (
            "arn:aws:eks:us-east-1:123456789012:"
            "podidentityassociation/gco-live-orphan/a-22222222222222222"
        )
        malformed_pod = "arn:aws:eks:us-east-1:123456789012:pod/gco-live-orphan/default/example"
        malformed_complete_pod = (
            "arn:aws:eks:us-east-1:123456789012:"
            "pod/gco-live-orphan/default/example/not-a-canonical-uuid"
        )
        malformed_association = (
            "arn:aws:eks:us-east-1:123456789012:"
            "podidentityassociation/gco-live-orphan/a-not-canonical"
        )
        wrong_partition_pod = orphaned_pod.replace("arn:aws:", "arn:aws-us-gov:")
        wrong_account_pod = orphaned_pod.replace("123456789012", "999999999999")
        non_pod_eks = "arn:aws:eks:us-east-1:123456789012:cluster/gco-live-orphan"
        wrong_region_pod = (
            "arn:aws:eks:us-west-2:123456789012:"
            "pod/gco-live-orphan/default/example/33333333-3333-3333-3333-333333333333"
        )
        unscanned_region_pod = (
            "arn:aws:eks:us-west-2:123456789012:"
            "pod/gco-live-orphan/default/example/44444444-4444-4444-4444-444444444444"
        )
        project_inventory = {
            "cloudformation_stacks": {},
            "authority_scope": {"partition": "aws", "account": "123456789012"},
            "coverage": {
                "complete": True,
                "completed_scanners": ["eks_clusters"],
                "scanner_regions": {"eks_clusters": [self._REGION]},
            },
            "authoritative_eks_clusters": {
                self._REGION: ["gco-live-existing"],
            },
            "regional": {
                self._REGION: {
                    "tagged_resources": [
                        {"arn": orphaned_pod, "tags": {}},
                        {"arn": existing_pod, "tags": {}},
                        {"arn": orphaned_association, "tags": {}},
                        {"arn": existing_association, "tags": {}},
                        {"arn": malformed_pod, "tags": {}},
                        {"arn": malformed_complete_pod, "tags": {}},
                        {"arn": malformed_association, "tags": {}},
                        {"arn": wrong_partition_pod, "tags": {}},
                        {"arn": wrong_account_pod, "tags": {}},
                        {"arn": non_pod_eks, "tags": {}},
                        {"arn": wrong_region_pod, "tags": {}},
                    ]
                },
                "us-west-2": {"tagged_resources": [{"arn": unscanned_region_pod, "tags": {}}]},
            },
        }
        baseline = {"protected_stacks": {}, "ecr_repositories": {}}

        filtered = ownership_ecr._strip_baseline_ecr(project_inventory, baseline)

        east_arns = {
            record["arn"] for record in filtered["regional"][self._REGION]["tagged_resources"]
        }
        assert orphaned_pod not in east_arns
        assert orphaned_association not in east_arns
        assert east_arns == {
            existing_pod,
            existing_association,
            malformed_pod,
            malformed_complete_pod,
            malformed_association,
            wrong_partition_pod,
            wrong_account_pod,
            non_pod_eks,
            wrong_region_pod,
        }
        assert filtered["regional"]["us-west-2"]["tagged_resources"] == [
            {"arn": unscanned_region_pod, "tags": {}}
        ]


class TestStrictDestroyCheckpointing:
    def _destroy_context(self, *, destroyed: bool = False) -> SimpleNamespace:
        stack_id = "arn:aws:cloudformation:us-east-1:123456789012:stack/gco-live-global/stack-uuid"
        change_set_id = (
            "arn:aws:cloudformation:us-east-1:123456789012:changeSet/run-123/change-set-uuid"
        )
        state = {
            "target_stack_regions": {"gco-live-global": "us-east-1"},
            "owned_stacks": {
                "us-east-1": {
                    "gco-live-global": {
                        "name": "gco-live-global",
                        "region": "us-east-1",
                        "stack_id": stack_id,
                        "run_tag": "run-123",
                        "authority": "prepared-change-set",
                        "change_set_id": change_set_id,
                        "change_set_type": "CREATE",
                    }
                }
            },
            "bootstrap_stacks": {
                "us-east-1": {
                    "stack_id": (
                        "arn:aws:cloudformation:us-east-1:123456789012:"
                        "stack/CDKToolkit/toolkit-uuid"
                    ),
                    "status": "CREATE_COMPLETE",
                }
            },
        }
        ctx = _context(state=state)
        ctx.checkpoint.destroyed = destroyed
        ctx.checkpoint.completed_actions = ["destroy", "final-inventory"] if destroyed else []
        ctx.stack_manager.destroy_orchestrated.return_value = (
            True,
            ["gco-live-global"],
            [],
        )
        return ctx

    def test_workload_barrier_hash_normalizes_dynamodb_decimals(self) -> None:
        ctx = self._destroy_context()
        ctx.checkpoint.state["jobs"] = [{"priority": Decimal("100"), "deleted": True}]
        ctx.checkpoint.state["central_jobs"] = [
            {"job_id": "central-job-id", "priority": Decimal("100")}
        ]

        barrier = actions_destroy._record_workload_cleanup_barrier(
            ctx,
            {"complete": True, "errors": [], "unresolved": [], "ended_at": "done"},
        )

        assert len(barrier["snapshot_sha256"]) == 64
        from scripts.live_release_validation.models import to_jsonable

        reloaded = self._destroy_context()
        reloaded.checkpoint.state = json.loads(json.dumps(to_jsonable(ctx.checkpoint.state)))
        assert actions_destroy._validated_workload_cleanup_barrier(reloaded) == barrier

    def test_already_destroyed_workloads_require_a_completed_barrier(self) -> None:
        ctx = self._destroy_context(destroyed=True)
        ctx.checkpoint.state["jobs"] = [{"name": "unreconciled-job"}]
        absent = {
            "all_absent": True,
            "absent": [{"name": "gco-live-global"}],
            "residual": [],
        }

        with (
            patch_live_validation_helper("_verify_target_stack_absence", return_value=absent),
            patch_live_validation_helper("_checkpoint_retained_kms_keys") as checkpoint_kms,
            patch_live_validation_helper("_retained_resource_cleanup") as retained_cleanup,
            pytest.raises(RuntimeError, match="no completed workload cleanup barrier"),
        ):
            actions_destroy.destroy_deployment(ctx)

        checkpoint_kms.assert_not_called()
        retained_cleanup.assert_not_called()

    def test_already_destroyed_empty_legacy_checkpoint_creates_and_proves_barrier(
        self,
    ) -> None:
        ctx = self._destroy_context(destroyed=True)
        absent = {
            "all_absent": True,
            "absent": [{"name": "gco-live-global"}],
            "residual": [],
        }
        cleanup = {
            "complete": True,
            "errors": [],
            "unresolved": [],
            "ended_at": "done",
        }

        with (
            patch_live_validation_helper(
                "_verify_target_stack_absence", side_effect=[absent, absent]
            ),
            patch_live_validation_helper(
                "cleanup_workloads", return_value=cleanup
            ) as cleanup_workloads,
            patch_live_validation_helper("_checkpoint_retained_kms_keys"),
            patch_live_validation_helper("_retained_resource_cleanup", return_value={"errors": []}),
        ):
            result = actions_destroy.destroy_deployment(ctx)

        assert result["already_destroyed"] is True
        assert result["workload_cleanup"] == cleanup
        assert result["workload_cleanup_barrier"]["job_count"] == 0
        assert result["workload_cleanup_barrier"]["central_job_count"] == 0
        assert result["stack_absence"] == absent
        assert ctx.checkpoint.state["target_stacks_absent"]["source"] == (
            "destroy-already-destroyed-completion"
        )
        cleanup_workloads.assert_called_once_with(ctx)
        ctx.stack_manager.destroy_orchestrated.assert_not_called()

    def test_destroy_passes_exact_ids_and_disables_parallel_bootstrap(self) -> None:
        ctx = self._destroy_context()
        initial = {"all_absent": False, "absent": [], "residual": [{"name": "x"}]}
        absent = {"all_absent": True, "absent": [{"name": "x"}], "residual": []}

        with (
            patch_live_validation_helper(
                "_verify_target_stack_absence", side_effect=[initial, absent, absent]
            ),
            patch_live_validation_helper(
                "cleanup_workloads",
                return_value={"complete": True, "errors": [], "unresolved": []},
            ),
            patch_live_validation_helper("_reconcile_stack_ownership"),
            patch_live_validation_helper("_checkpoint_new_ecr_repositories"),
            patch_live_validation_helper("_checkpoint_new_ecr_images"),
            patch_live_validation_helper("_checkpoint_retained_kms_keys"),
            patch_live_validation_helper("_retained_resource_cleanup", return_value={"errors": []}),
        ):
            result = actions_destroy.destroy_deployment(ctx)

        assert result["stack_absence"]["all_absent"] is True
        assert ctx.checkpoint.destroyed is True
        kwargs = ctx.stack_manager.destroy_orchestrated.call_args.kwargs
        assert kwargs["parallel"] is False
        assert kwargs["max_workers"] == 1
        assert kwargs["allow_bootstrap"] is False
        assert kwargs["expected_stack_ids"] == {
            "gco-live-global": ctx.checkpoint.state["owned_stacks"]["us-east-1"]["gco-live-global"][
                "stack_id"
            ]
        }
        change_set_id = ctx.checkpoint.state["owned_stacks"]["us-east-1"]["gco-live-global"][
            "change_set_id"
        ]
        assert kwargs["prepared_change_sets"] == {
            "gco-live-global": {
                change_set_id: {
                    "change_set_id": change_set_id,
                    "stack_id": kwargs["expected_stack_ids"]["gco-live-global"],
                    "change_set_type": "CREATE",
                }
            }
        }
        assert kwargs["bootstrap_stacks"] is ctx.checkpoint.state["bootstrap_stacks"]
        assert callable(kwargs["authorize_stack"])
        assert callable(kwargs["on_cleanup_complete"])
        assert kwargs["strict_deployment_token"] == "run-123-teardown"
        assert callable(kwargs["on_change_set_prepared"])
        assert callable(kwargs["on_ecr_repository_created"])

    def test_unresolved_workload_cleanup_blocks_stack_helpers(self) -> None:
        ctx = self._destroy_context()
        initial = {"all_absent": False, "absent": [], "residual": [{"name": "x"}]}
        unresolved = {
            "complete": False,
            "errors": [{"resource": "central:job", "error": "not terminal"}],
            "unresolved": [{"resource": "central:job", "reason": "not terminal"}],
        }

        with (
            patch_live_validation_helper("_verify_target_stack_absence", return_value=initial),
            patch_live_validation_helper("cleanup_workloads", return_value=unresolved),
            pytest.raises(RuntimeError, match="unresolved teardown barrier"),
        ):
            actions_destroy.destroy_deployment(ctx)

        ctx.stack_manager.destroy_orchestrated.assert_not_called()

    def test_absent_stack_resume_uses_completed_workload_barrier_without_dynamodb(
        self,
    ) -> None:
        ctx = self._destroy_context()
        job_id = "central-job-id"
        actual_name = "gco-live-ddb-run-123-worker"
        workload = {
            "name": "gco-live-ddb-run-123",
            "namespace": "gco-jobs",
            "region": "us-east-1",
            "path": "dynamodb",
            "submission_state": "deleted",
            "deleted": True,
            "central_queue_job_id": job_id,
            "k8s_job_name": actual_name,
            "k8s_job_namespace": "gco-jobs",
            "k8s_job_uid": "uid-central-1",
            "uid": "uid-central-1",
        }
        central_job = {
            "job_id": job_id,
            "cleanup_complete": True,
            "status": "succeeded",
            "k8s_job_name": actual_name,
            "k8s_job_namespace": "gco-jobs",
            "k8s_job_uid": "uid-central-1",
            "k8s_identity_source": "dynamodb",
        }
        ctx.checkpoint.state["jobs"] = [workload]
        ctx.checkpoint.state["central_jobs"] = [central_job]
        actions_destroy._record_workload_cleanup_barrier(
            ctx,
            {"complete": True, "errors": [], "unresolved": [], "ended_at": "done"},
        )
        ctx.persist.reset_mock()
        absent = {
            "all_absent": True,
            "absent": [{"name": "gco-live-global"}],
            "residual": [],
        }

        with (
            patch_live_validation_helper(
                "_verify_target_stack_absence",
                side_effect=[absent, absent],
            ),
            patch_live_validation_helper("cleanup_workloads") as cleanup_workloads,
            patch_live_validation_helper("_read_central_job_item") as read_central_job,
            patch_live_validation_helper("_checkpoint_new_ecr_repositories"),
            patch_live_validation_helper("_checkpoint_new_ecr_images"),
            patch_live_validation_helper("_checkpoint_retained_kms_keys"),
            patch_live_validation_helper(
                "_retained_resource_cleanup",
                return_value={"errors": []},
            ),
        ):
            result = actions_destroy.destroy_deployment(ctx)

        assert result["resumed_after_stack_absence"] is True
        assert result["stack_absence"] == absent
        assert ctx.checkpoint.destroyed is True
        assert ctx.checkpoint.state["target_stacks_absent"]["source"] == (
            "destroy-resume-completion"
        )
        cleanup_workloads.assert_not_called()
        read_central_job.assert_not_called()
        ctx.stack_manager.destroy_orchestrated.assert_not_called()

    def test_stale_destroyed_checkpoint_is_reopened(self) -> None:
        ctx = self._destroy_context(destroyed=True)
        residual = {
            "all_absent": False,
            "absent": [],
            "residual": [{"name": "gco-live-global"}],
        }
        absent = {"all_absent": True, "absent": [], "residual": []}

        with (
            patch_live_validation_helper(
                "_verify_target_stack_absence", side_effect=[residual, absent, absent]
            ),
            patch_live_validation_helper(
                "cleanup_workloads",
                return_value={"complete": True, "errors": [], "unresolved": []},
            ),
            patch_live_validation_helper("_reconcile_stack_ownership"),
            patch_live_validation_helper("_checkpoint_new_ecr_repositories"),
            patch_live_validation_helper("_checkpoint_new_ecr_images"),
            patch_live_validation_helper("_checkpoint_retained_kms_keys"),
            patch_live_validation_helper("_retained_resource_cleanup", return_value={"errors": []}),
        ):
            actions_destroy.destroy_deployment(ctx)

        assert ctx.stack_manager.destroy_orchestrated.called
        assert ctx.checkpoint.destroyed is True
        assert "stale_destroyed_reconciliations" in ctx.checkpoint.state


class TestFinalInventoryReconciliation:
    def test_stale_destroyed_checkpoint_is_cleared_and_persisted(self) -> None:
        ctx = _context(state={"enabled_regions": ["us-east-1"]})
        ctx.settings.protected_stack_names = ("CDKToolkit",)
        ctx.checkpoint.destroyed = True
        ctx.checkpoint.completed_actions = ["destroy", "final-inventory"]
        residual = {
            "all_absent": False,
            "absent": [],
            "residual": [{"name": "gco-live-global", "region": "us-east-1"}],
        }
        project_inventory = {
            "cloudformation_stacks": {},
            "regional": {},
            "global_accelerators": [],
        }
        accepted_efs = [{"recovery_point_arn": "accepted"}]

        with (
            patch_live_validation_helper("_verify_target_stack_absence", return_value=residual),
            patch_live_validation_helper(
                "capture_baseline",
                return_value=ctx.checkpoint.baseline,
            ),
            patch_live_validation_helper("compare_baseline", return_value=[]),
            patch_live_validation_helper(
                "collect_project_resources",
                return_value=project_inventory,
            ),
            patch_live_validation_helper(
                "_strip_baseline_ecr",
                return_value=project_inventory,
            ),
            patch_live_validation_helper(
                "_strip_expected_pending_kms",
                return_value=(project_inventory, []),
            ),
            patch.object(
                actions_final_inventory,
                "_strip_accepted_efs_automatic_backup_recovery_points",
                return_value=(project_inventory, accepted_efs),
            ),
            patch_live_validation_helper("summarize_project_resources", return_value={}),
            patch_live_validation_helper("project_resources_are_absent", return_value=True),
            pytest.raises(RuntimeError, match="Target stacks remain after teardown"),
        ):
            actions_final_inventory.action_final_inventory(ctx)

        assert ctx.report.final_inventory["stack_absence"] == residual
        assert (
            ctx.report.final_inventory["accepted_efs_automatic_backup_recovery_points"]
            == accepted_efs
        )
        assert ctx.checkpoint.state["final_inventory"]["stack_absence"] == residual
        assert ctx.checkpoint.destroyed is False
        assert "destroy" not in ctx.checkpoint.completed_actions
        assert "final-inventory" not in ctx.checkpoint.completed_actions
        reconciliation = ctx.checkpoint.state["stale_destroyed_reconciliations"][-1]
        assert reconciliation["source"] == "final-inventory"
        assert reconciliation["stack_absence"] == residual
        ctx.persist.assert_called_once()


class TestCheckpointPersistence:
    def test_checkpoint_round_trip_preserves_object_state(self, tmp_path: Path) -> None:
        from scripts.live_release_validation import models

        path = tmp_path / "report" / "checkpoint.json"
        checkpoint = models.RunCheckpoint(
            identity={"run_id": "run-123"},
            state={"nested": {"value": 1}},
        )
        models.atomic_write_json(path, checkpoint.to_dict())

        loaded = models.RunCheckpoint.from_path(path)

        assert loaded.identity == {"run_id": "run-123"}
        assert loaded.state == {"nested": {"value": 1}}

    def test_checkpoint_without_state_keeps_compatible_empty_default(self, tmp_path: Path) -> None:
        from scripts.live_release_validation import models

        path = tmp_path / "report" / "checkpoint.json"
        models.atomic_write_json(
            path,
            {"schema_version": models.SCHEMA_VERSION, "identity": {}},
        )

        assert models.RunCheckpoint.from_path(path).state == {}

    @pytest.mark.parametrize(
        "state",
        [None, False, 0, "", [], [["key", "value"]]],
        ids=["null", "false", "zero", "string", "empty-list", "pair-list"],
    )
    def test_checkpoint_rejects_present_nonobject_state(
        self,
        tmp_path: Path,
        state,
    ) -> None:
        from scripts.live_release_validation import models

        path = tmp_path / "report" / "checkpoint.json"
        models.atomic_write_json(
            path,
            {
                "schema_version": models.SCHEMA_VERSION,
                "identity": {},
                "state": state,
            },
        )

        with pytest.raises(ValueError, match="state must be an object"):
            models.RunCheckpoint.from_path(path)

    @pytest.mark.parametrize(
        ("raw_json", "duplicate_key"),
        [
            (
                '{"schema_version":2,"state":{},"state":{"value":1}}',
                "state",
            ),
            (
                '{"schema_version":2,"state":{"attempt":false,"attempt":1}}',
                "attempt",
            ),
        ],
        ids=["root", "nested"],
    )
    def test_checkpoint_rejects_duplicate_json_keys(
        self,
        tmp_path: Path,
        raw_json: str,
        duplicate_key: str,
    ) -> None:
        from scripts.live_release_validation import models

        path = tmp_path / "report" / "checkpoint.json"
        models.atomic_write_text(path, raw_json)

        with pytest.raises(ValueError, match=rf"duplicate JSON key: {duplicate_key}"):
            models.RunCheckpoint.from_path(path)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission model only")
    def test_repeated_atomic_checkpoint_replacements_stay_owner_only(
        self,
        tmp_path: Path,
    ) -> None:
        from scripts.live_release_validation import artifact_io, models

        report_dir = tmp_path / "live-report"
        checkpoint = report_dir / "checkpoint.json"

        replacement_modes: list[int] = []
        real_replace = os.replace

        def tracked_replace(source, destination, **kwargs):
            if kwargs.get("src_dir_fd") is None:
                metadata = Path(source).stat()
            else:
                metadata = os.stat(source, dir_fd=kwargs["src_dir_fd"])
            replacement_modes.append(stat.S_IMODE(metadata.st_mode))
            real_replace(source, destination, **kwargs)

        with patch.object(artifact_io.os, "replace", side_effect=tracked_replace):
            for generation in (1, 2):
                models.atomic_write_json(checkpoint, {"generation": generation})
                assert stat.S_IMODE(report_dir.stat().st_mode) == 0o700
                assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600

        assert replacement_modes == [0o600, 0o600]
        assert json.loads(checkpoint.read_text(encoding="utf-8")) == {"generation": 2}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission model only")
    def test_atomic_write_rejects_ancestor_rebinding(
        self,
        tmp_path: Path,
    ) -> None:
        from scripts.live_release_validation import artifact_io, models

        original_ancestor = tmp_path / "original"
        report_dir = original_ancestor / "live-report"
        checkpoint = report_dir / "checkpoint.json"
        displaced_ancestor = tmp_path / "displaced"
        real_replace = os.replace

        def rebind_then_replace(source, destination, **kwargs):
            original_ancestor.rename(displaced_ancestor)
            replacement_directory = original_ancestor / "live-report"
            replacement_directory.mkdir(parents=True, mode=0o700)
            # This regression fixture must have the exact private-directory mode.
            # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
            os.chmod(replacement_directory, 0o700)
            real_replace(source, destination, **kwargs)

        with (
            patch.object(artifact_io.os, "replace", side_effect=rebind_then_replace),
            pytest.raises(RuntimeError, match="rebound while open"),
        ):
            models.atomic_write_json(checkpoint, {"generation": 1})

        assert not checkpoint.exists()
        displaced_checkpoint = displaced_ancestor / "live-report" / "checkpoint.json"
        assert json.loads(displaced_checkpoint.read_text(encoding="utf-8")) == {"generation": 1}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission model only")
    def test_existing_nonprivate_directory_is_rejected_without_chmod(
        self,
        tmp_path: Path,
    ) -> None:
        from scripts.live_release_validation import models

        report_dir = tmp_path / "shared"
        report_dir.mkdir(mode=0o755)
        # Deliberately nonprivate to verify the production code rejects it.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(report_dir, 0o755)

        with pytest.raises(PermissionError, match="mode 0700"):
            models.atomic_write_json(report_dir / "checkpoint.json", {"generation": 1})

        assert stat.S_IMODE(report_dir.stat().st_mode) == 0o755
        assert not (report_dir / "checkpoint.json").exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission model only")
    def test_private_directory_must_be_dedicated_and_link_free(
        self,
        tmp_path: Path,
    ) -> None:
        from scripts.live_release_validation import models

        report_dir = tmp_path / "private"
        report_dir.mkdir(mode=0o700)
        unrelated = report_dir / "unrelated.txt"
        unrelated.write_text("do not overwrite", encoding="utf-8")
        os.chmod(unrelated, 0o600)

        with pytest.raises(ValueError, match="unrelated entry"):
            models.ensure_private_run_directory(
                report_dir,
                report_dir / "checkpoint.json",
            )

        unrelated.unlink()
        target = tmp_path / "checkpoint-target.json"
        target.write_text("{}", encoding="utf-8")
        os.chmod(target, 0o600)
        (report_dir / "checkpoint.json").symlink_to(target)
        with pytest.raises(ValueError, match="regular file"):
            models.ensure_private_run_directory(
                report_dir,
                report_dir / "checkpoint.json",
            )

    def test_checkpoint_must_be_a_direct_report_directory_child(
        self,
        tmp_path: Path,
    ) -> None:
        from scripts.live_release_validation.models import RunSettings

        with pytest.raises(ValueError, match="direct child"):
            RunSettings(
                run_id="run-123",
                repo_root=tmp_path,
                report_dir=tmp_path / "report",
                checkpoint_path=tmp_path / "checkpoint.json",
                expected_account="123456789012",
                expected_sha="a" * 40,
                expected_branch="chore/test",
                profile="configured",
                requested_actions=("all",),
            )

    @pytest.mark.parametrize(
        "checkpoint_name",
        ("live-release-validation.json", "live-release-validation.md"),
    )
    def test_checkpoint_cannot_collide_with_reserved_report_name(
        self,
        tmp_path: Path,
        checkpoint_name: str,
    ) -> None:
        from scripts.live_release_validation.models import RunSettings

        with pytest.raises(ValueError, match="reserved for a validation report"):
            RunSettings(
                run_id="run-123",
                repo_root=tmp_path,
                report_dir=tmp_path / "report",
                checkpoint_path=tmp_path / "report" / checkpoint_name,
                expected_account="123456789012",
                expected_sha="a" * 40,
                expected_branch="chore/test",
                profile="configured",
                requested_actions=("all",),
            )


class TestLocalOnlyRuntime:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission model only")
    def test_fallback_report_does_not_bypass_dedicated_directory_rejection(
        self,
        tmp_path: Path,
        monkeypatch,
        capsys,
    ) -> None:
        from scripts.live_release_validation import __main__ as live_main
        from scripts.live_release_validation.models import RunSettings

        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        report_dir = tmp_path / "private"
        report_dir.mkdir(mode=0o700)
        unrelated = report_dir / "unrelated.txt"
        unrelated.write_text("preserve", encoding="utf-8")
        os.chmod(unrelated, 0o600)
        settings = RunSettings(
            run_id="run-123",
            repo_root=tmp_path,
            report_dir=report_dir,
            checkpoint_path=report_dir / "checkpoint.json",
            expected_account="123456789012",
            expected_sha="a" * 40,
            expected_branch="chore/test",
            profile="configured",
            requested_actions=("all",),
        )
        parser = MagicMock()
        parser.parse_args.return_value = SimpleNamespace(list_actions=False)

        with (
            patch.object(live_main, "_build_parser", return_value=parser),
            patch.object(live_main, "_settings_from_args", return_value=settings),
            patch.object(
                live_main,
                "LiveValidationRunner",
                side_effect=ValueError("output directory is not dedicated"),
            ),
        ):
            assert live_main.main() == 1

        assert unrelated.read_text(encoding="utf-8") == "preserve"
        assert not (report_dir / "live-release-validation.json").exists()
        assert not (report_dir / "live-release-validation.md").exists()
        assert "output directory is unsafe" in capsys.readouterr().err

    def test_main_rejects_github_actions_before_argument_or_aws_setup(
        self,
        monkeypatch,
        capsys,
    ) -> None:
        from scripts.live_release_validation import __main__ as live_main

        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with patch.object(live_main, "_build_parser") as build_parser:
            assert live_main.main() == 1

        build_parser.assert_not_called()
        assert "local-only" in capsys.readouterr().err

    def test_runner_rejects_github_actions_before_checkpoint_or_session(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        from scripts.live_release_validation import models, runner

        settings = models.RunSettings(
            run_id="run-123",
            repo_root=tmp_path,
            report_dir=tmp_path / "report",
            checkpoint_path=tmp_path / "report/checkpoint.json",
            expected_account="123456789012",
            expected_sha="a" * 40,
            expected_branch="chore/test",
            profile="configured",
            requested_actions=("all",),
        )
        monkeypatch.setenv("GITHUB_ACTIONS", "TRUE")

        with (
            patch.object(runner, "ThrottleResilientSession") as session,
            pytest.raises(RuntimeError, match="must not run in GitHub Actions"),
        ):
            runner.LiveValidationRunner(settings)

        session.assert_not_called()
        assert not settings.report_dir.exists()
        assert not settings.checkpoint_path.exists()

    def test_parser_does_not_consume_github_identity_variables(self, monkeypatch) -> None:
        from scripts.live_release_validation import __main__ as live_main

        monkeypatch.setenv("GITHUB_SHA", "b" * 40)
        monkeypatch.setenv("GITHUB_HEAD_REF", "github-head")
        monkeypatch.setenv("GITHUB_REF_NAME", "github-ref")
        monkeypatch.delenv("GCO_LIVE_EXPECTED_SHA", raising=False)
        monkeypatch.delenv("GCO_LIVE_EXPECTED_BRANCH", raising=False)

        args = live_main._build_parser().parse_args([])

        assert args.expected_sha is None
        assert args.expected_branch is None

    def test_deployed_checkpoint_constructor_failure_reports_blocked_recovery(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from scripts.live_release_validation import __main__ as live_main
        from scripts.live_release_validation import runner
        from scripts.live_release_validation.models import (
            RunCheckpoint,
            RunSettings,
            atomic_write_json,
        )

        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        (tmp_path / "cdk.json").write_text(
            json.dumps(
                {
                    "context": {
                        "project_name": "gco",
                        "deployment_regions": {
                            "global": "us-west-2",
                            "api_gateway": "us-east-1",
                            "monitoring": "us-east-1",
                            "regional": ["us-east-1"],
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        report_dir = tmp_path / "report"
        report_dir.mkdir(mode=0o700)
        settings = RunSettings(
            run_id="run-123",
            repo_root=tmp_path,
            report_dir=report_dir,
            checkpoint_path=report_dir / "checkpoint.json",
            expected_account="123456789012",
            expected_sha="a" * 40,
            expected_branch="chore/test",
            profile="configured",
            requested_actions=("preflight",),
            resume=True,
        )
        checkpoint = RunCheckpoint(identity=settings.identity(), deployment_attempted=True)
        atomic_write_json(settings.checkpoint_path, checkpoint.to_dict())
        parser = MagicMock()
        parser.parse_args.return_value = SimpleNamespace(list_actions=False)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "live_release_validation",
                "--repo-root",
                str(tmp_path),
                "--run-id",
                "run-123",
                "--resume",
            ],
        )

        with (
            patch.object(live_main, "_build_parser", return_value=parser),
            patch.object(live_main, "_settings_from_args", return_value=settings),
            patch.object(
                runner,
                "ThrottleResilientSession",
                side_effect=RuntimeError("session bootstrap failed"),
            ),
            patch.object(runner, "destroy_deployment") as destroy,
        ):
            assert live_main.main() == 1

        destroy.assert_not_called()
        report = json.loads(
            (report_dir / "live-release-validation.json").read_text(encoding="utf-8")
        )
        assert report["cleanup"]["needed"] is True
        assert report["cleanup"]["completed"] is False
        assert "construction failed" in report["cleanup"]["blocked"]
        recovery = report["cleanup"]["recovery_command"]
        assert "-m scripts.live_release_validation" in recovery
        assert "--resume" in recovery
        assert report["status"] == "failed"

    def test_constructor_failure_never_claims_cleanup_for_mismatched_checkpoint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from scripts.live_release_validation import __main__ as live_main
        from scripts.live_release_validation.models import (
            RunCheckpoint,
            RunSettings,
            atomic_write_json,
        )

        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        report_dir = tmp_path / "report"
        report_dir.mkdir(mode=0o700)
        settings = RunSettings(
            run_id="run-123",
            repo_root=tmp_path,
            report_dir=report_dir,
            checkpoint_path=report_dir / "checkpoint.json",
            expected_account="123456789012",
            expected_sha="a" * 40,
            expected_branch="chore/test",
            profile="configured",
            requested_actions=("preflight",),
            resume=True,
        )
        wrong_identity = settings.identity()
        wrong_identity["expected_sha"] = "b" * 40
        checkpoint = RunCheckpoint(identity=wrong_identity, deployment_attempted=True)
        atomic_write_json(settings.checkpoint_path, checkpoint.to_dict())
        parser = MagicMock()
        parser.parse_args.return_value = SimpleNamespace(list_actions=False)

        with (
            patch.object(live_main, "_build_parser", return_value=parser),
            patch.object(live_main, "_settings_from_args", return_value=settings),
            patch.object(
                live_main,
                "LiveValidationRunner",
                side_effect=ValueError("checkpoint identity mismatch"),
            ),
        ):
            assert live_main.main() == 1

        report = json.loads(
            (report_dir / "live-release-validation.json").read_text(encoding="utf-8")
        )
        assert report["cleanup"]["needed"] is True
        assert report["cleanup"]["completed"] is False
        assert "does not match" in report["cleanup"]["blocked"]
        assert "recovery_command" not in report["cleanup"]

    def test_main_inference_cli_inputs_are_checkpoint_identity(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from scripts.live_release_validation import __main__ as live_main
        from scripts.live_release_validation.models import RunSettings

        parser = live_main._build_parser()
        args = parser.parse_args(
            [
                "--expected-account",
                "123456789012",
                "--expected-sha",
                "a" * 40,
                "--expected-branch",
                "chore/test",
                "--actions",
                "inference",
                "--inference-region",
                "us-east-1",
                "--inference-vllm-image",
                "registry.example/vllm@sha256:" + "b" * 64,
                "--inference-vllm-model-id",
                "publisher/vllm-model",
                "--inference-vllm-model-revision",
                "c" * 40,
                "--inference-tgi-image",
                "registry.example/tgi@sha256:" + "d" * 64,
                "--inference-tgi-model-id",
                "publisher/tgi-model",
                "--inference-tgi-model-revision",
                "e" * 40,
                "--inference-gpu-count",
                "1",
                "--confirm-inference-deployment",
            ]
        )
        (tmp_path / "cdk.json").write_text(
            json.dumps(
                {
                    "context": {
                        "inference_proxy": {
                            "tls_proxy_cpu_request_millicores": 125,
                            "tls_proxy_cpu_target_utilization_percentage": 85,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(live_main, "_repository_root", lambda _value: tmp_path)
        settings = live_main._settings_from_args(parser, args)

        assert isinstance(settings, RunSettings)
        assert settings.inference_enabled is True
        full_identity = settings.identity()
        assert full_identity["extra_cdk_context"] == {
            "gco_live_validation_disable_efs_automatic_backups": "true"
        }
        identity = full_identity["inference"]
        assert identity["selected_region"] == "us-east-1"
        assert [runtime["framework"] for runtime in identity["runtimes"]] == ["vllm", "tgi"]
        assert identity["runtimes"][0]["image"].endswith("b" * 64)
        assert identity["runtimes"][0]["model"] == {
            "id": "publisher/vllm-model",
            "revision": "c" * 40,
        }
        assert identity["runtimes"][1]["image"].endswith("d" * 64)
        assert identity["runtimes"][1]["model"] == {
            "id": "publisher/tgi-model",
            "revision": "e" * 40,
        }
        assert identity["endpoint_contract"]["gpu_count"] == 1
        assert identity["shared_proxy_contract"]["tls_cpu_request"] == "125m"
        assert identity["shared_proxy_contract"]["tls_cpu_target"] == 85

    @pytest.mark.parametrize("proxy_value", ["missing", None])
    def test_main_inference_cli_uses_production_tls_defaults_when_block_omitted_or_null(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        proxy_value: object,
    ) -> None:
        from scripts.live_release_validation import __main__ as live_main

        context: dict[str, object] = {}
        if proxy_value != "missing":
            context["inference_proxy"] = proxy_value
        (tmp_path / "cdk.json").write_text(json.dumps({"context": context}), encoding="utf-8")
        parser = live_main._build_parser()
        args = parser.parse_args(
            [
                "--expected-account",
                "123456789012",
                "--expected-sha",
                "a" * 40,
                "--expected-branch",
                "chore/test",
                "--actions",
                "inference",
                "--inference-region",
                "us-east-1",
                "--inference-vllm-image",
                "registry.example/vllm@sha256:" + "b" * 64,
                "--inference-vllm-model-id",
                "publisher/vllm-model",
                "--inference-vllm-model-revision",
                "c" * 40,
                "--inference-tgi-image",
                "registry.example/tgi@sha256:" + "d" * 64,
                "--inference-tgi-model-id",
                "publisher/tgi-model",
                "--inference-tgi-model-revision",
                "e" * 40,
                "--confirm-inference-deployment",
            ]
        )
        monkeypatch.setattr(live_main, "_repository_root", lambda _value: tmp_path)

        settings = live_main._settings_from_args(parser, args)

        assert settings.proxy_tls_cpu_request == "100m"
        assert settings.proxy_tls_cpu_target == 70

    def test_detached_head_does_not_consume_github_branch_variables(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        monkeypatch.setenv("GITHUB_HEAD_REF", "github-head")
        monkeypatch.setenv("GITHUB_REF_NAME", "github-ref")

        with (
            patch_live_validation_helper("_run_git", return_value=""),
            pytest.raises(RuntimeError, match="requires a checked-out branch"),
        ):
            context._resolve_branch(tmp_path)


class TestGuaranteedCleanupResume:
    def test_completed_destroy_dispatch_is_skipped_but_guaranteed_cleanup_revalidates(self) -> None:
        from scripts.live_release_validation import runner

        instance = object.__new__(runner.LiveValidationRunner)
        destroy_definition = SimpleNamespace(
            name="destroy",
            description="destroy deployment",
            handler=MagicMock(),
        )
        instance.registry = {"destroy": destroy_definition}
        instance.checkpoint = SimpleNamespace(
            deployment_attempted=True,
            destroyed=True,
            completed_actions=["destroy"],
            action_results={"destroy": SimpleNamespace(details={"checkpointed": True})},
            baseline=None,
            state={},
        )
        instance.context = MagicMock()
        instance.report = SimpleNamespace(cleanup={})
        instance._identity_verified = True
        instance._persist_checkpoint = MagicMock()

        assert instance._execute_action(destroy_definition) == {"checkpointed": True}
        destroy_definition.handler.assert_not_called()

        repeated = {
            "needed": True,
            "already_destroyed": True,
            "stack_absence": {"all_absent": True},
        }
        with patch.object(runner, "destroy_deployment", return_value=repeated) as destroy:
            instance._guaranteed_cleanup()

        destroy.assert_called_once_with(instance.context)
        assert instance.report.cleanup == {"completed": True, **repeated}


class TestReportCompletionStatus:
    def test_only_complete_registry_is_passed(self) -> None:
        from scripts.live_release_validation import runner

        instance = object.__new__(runner.LiveValidationRunner)
        instance.registry = {"preflight": MagicMock(), "baseline": MagicMock()}

        instance.selected_actions = ("preflight",)
        assert instance._successful_status() == "partial"

        instance.selected_actions = tuple(instance.registry)
        assert instance._successful_status() == "passed"

    def test_partial_markdown_names_selected_action_scope(self) -> None:
        from scripts.live_release_validation.models import ValidationReport

        report = ValidationReport(
            run_id="run-123",
            identity={},
            selected_actions=["preflight"],
            started_at="2026-07-18T00:00:00+00:00",
            ended_at="2026-07-18T00:00:01+00:00",
            status="partial",
        )

        markdown = report.to_markdown()

        assert "- **Status:** **PARTIAL**" in markdown
        assert "- **Selected action scope:** `preflight`" in markdown


class TestSmokeManifestSupplyChain:
    _PINNED_IMAGES = (
        "docker.io/library/busybox:1.38.0@"
        "sha256:dc2d74b28e4cf8984fa52af1f39bc7c3d9c73760b41a74d629f5d11b1ab28616",
        # The Slurm probe needs a Python runtime for its slurmrestd round trip.
        "docker.io/library/python:3.14.7-slim@"
        "sha256:656d12e70054d5fda18a045e2494c96701e9792dd1445f95b3d038df954f57e9",
    )

    def test_smoke_images_are_immutable_and_dependency_scanned(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifests = sorted((root / "scripts/live_release_validation/manifests").glob("*.yaml"))
        assert manifests

        for manifest in manifests:
            content = manifest.read_text(encoding="utf-8")
            assert any(f"image: {image}" in content for image in self._PINNED_IMAGES), (
                f"{manifest.name} must use one of the digest-pinned validation images"
            )
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("image:"):
                    assert "@sha256:" in stripped, (
                        f"{manifest.name} has an unpinned image: {stripped}"
                    )
            assert "imagePullPolicy: IfNotPresent" in content

        dependency_scan = (root / ".github/scripts/dependency-scan.sh").read_text(encoding="utf-8")
        assert "scripts/live_release_validation/manifests/" in dependency_scan


class TestSchedulerValidation:
    """The schedulers action proves enabled schedulers and skips with reasons."""

    @staticmethod
    def _ctx(
        *,
        helm: dict[str, object] | None,
        optional: tuple[str, ...] = (),
    ) -> SimpleNamespace:
        return SimpleNamespace(
            cdk_context={} if helm is None else {"helm": helm},
            settings=SimpleNamespace(run_id="run-123", optional_schedulers=optional),
            checkpoint=SimpleNamespace(state={}),
            persist=MagicMock(),
        )

    def test_enablement_follows_the_cdk_json_helm_block(self) -> None:
        from scripts.live_release_validation.checks import schedulers as checks_schedulers

        ctx = self._ctx(
            helm={
                "volcano": {"enabled": True},
                "kueue": {"enabled": True},
                "yunikorn": {"enabled": False},
                "slurm": {"enabled": False},
            }
        )
        enablement = checks_schedulers.effective_scheduler_enablement(ctx)
        assert enablement["volcano"] == {"enabled": True, "source": "cdk.json helm.volcano"}
        assert enablement["kueue"] == {"enabled": True, "source": "cdk.json helm.kueue"}
        assert enablement["yunikorn"] == {"enabled": False, "source": "cdk.json helm.yunikorn"}
        assert enablement["slurm"] == {"enabled": False, "source": "cdk.json helm.slurm"}

    def test_missing_helm_keys_default_to_enabled_like_the_chart_map(self) -> None:
        from scripts.live_release_validation.checks import schedulers as checks_schedulers

        enablement = checks_schedulers.effective_scheduler_enablement(self._ctx(helm=None))
        assert all(state["enabled"] for state in enablement.values())

    def test_run_overrides_force_optional_schedulers_on(self) -> None:
        from scripts.live_release_validation.checks import schedulers as checks_schedulers

        ctx = self._ctx(
            helm={"yunikorn": {"enabled": False}, "slurm": {"enabled": False}},
            optional=("slurm", "yunikorn"),
        )
        enablement = checks_schedulers.effective_scheduler_enablement(ctx)
        assert enablement["yunikorn"] == {"enabled": True, "source": "run-override"}
        assert enablement["slurm"] == {"enabled": True, "source": "run-override"}

    def test_non_optional_override_names_are_refused(self) -> None:
        from scripts.live_release_validation.checks import schedulers as checks_schedulers

        ctx = self._ctx(helm={}, optional=("volcano",))
        with pytest.raises(RuntimeError, match="not optional schedulers"):
            checks_schedulers.effective_scheduler_enablement(ctx)

    def test_action_probes_enabled_and_skips_disabled_with_reasons(self) -> None:
        from scripts.live_release_validation.actions import schedulers as actions_schedulers

        ctx = self._ctx(
            helm={
                "volcano": {"enabled": True},
                "kueue": {"enabled": True},
                "yunikorn": {"enabled": False},
                "slurm": {"enabled": False},
            }
        )
        probes: list[dict[str, object]] = []

        def fake_lifecycle(_ctx, *, manifest_filename, path, marker_prefix):
            probes.append(
                {
                    "manifest": manifest_filename,
                    "path": path,
                    "marker_prefix": marker_prefix,
                }
            )
            return {"status_marker": marker_prefix}

        with patch_live_validation_helper("_run_api_transport_lifecycle", fake_lifecycle):
            evidence = actions_schedulers.action_schedulers(ctx)

        results = evidence["schedulers"]
        assert [probe["path"] for probe in probes] == ["volcano", "kueue"]
        assert all(probe["manifest"] == f"{probe['path']}-smoke-job.yaml" for probe in probes)
        assert results["volcano"]["status"] == "validated"
        assert results["kueue"]["status"] == "validated"
        assert results["yunikorn"]["status"] == "skipped"
        assert "--optional-schedulers" in results["yunikorn"]["reason"]
        assert "cdk.json helm.yunikorn" in results["yunikorn"]["reason"]
        assert results["slurm"]["status"] == "skipped"
        assert results["keda"]["status"] == "derived"
        assert "sqs action" in results["keda"]["reason"]
        assert results["kuberay"]["status"] == "chart-level"
        assert "kind allowlist" in results["kuberay"]["reason"]
        assert ctx.checkpoint.state["scheduler_validation"] == evidence
        ctx.persist.assert_called_once_with()

    def test_action_runs_every_probe_when_overrides_enable_everything(self) -> None:
        from scripts.live_release_validation.actions import schedulers as actions_schedulers

        ctx = self._ctx(
            helm={"yunikorn": {"enabled": False}, "slurm": {"enabled": False}},
            optional=("slurm", "yunikorn"),
        )
        probed: list[str] = []

        def fake_lifecycle(_ctx, *, manifest_filename, path, marker_prefix):
            probed.append(path)
            return {}

        with patch_live_validation_helper("_run_api_transport_lifecycle", fake_lifecycle):
            evidence = actions_schedulers.action_schedulers(ctx)

        assert probed == ["volcano", "kueue", "yunikorn", "slurm"]
        assert all(evidence["schedulers"][name]["status"] == "validated" for name in probed)
        assert evidence["schedulers"]["yunikorn"]["source"] == "run-override"

    def test_probe_failure_fails_the_action(self) -> None:
        from scripts.live_release_validation.actions import schedulers as actions_schedulers

        ctx = self._ctx(helm={"yunikorn": {"enabled": False}, "slurm": {"enabled": False}})

        def failing_lifecycle(_ctx, *, manifest_filename, path, marker_prefix):
            if path == "kueue":
                raise TimeoutError("workload was never admitted")
            return {}

        with (
            patch_live_validation_helper("_run_api_transport_lifecycle", failing_lifecycle),
            pytest.raises(TimeoutError, match="never admitted"),
        ):
            actions_schedulers.action_schedulers(ctx)

    def test_every_probe_manifest_exists_with_labels_and_marker(self) -> None:
        from scripts.live_release_validation.checks.schedulers import PROBED_SCHEDULERS

        root = Path(__file__).resolve().parents[1]
        manifests_dir = root / "scripts/live_release_validation/manifests"
        for scheduler in PROBED_SCHEDULERS:
            content = (manifests_dir / f"{scheduler}-smoke-job.yaml").read_text(encoding="utf-8")
            assert f"gco.aws/validation-path: {scheduler}" in content
            assert "gco.aws/validation-run: __RUN_TOKEN__" in content
            assert f"GCO_LIVE_{scheduler.upper()}___RUN_TOKEN__" in content
            assert "backoffLimit: 0" in content
            assert "ttlSecondsAfterFinished: 600" in content
        # Scheduling-proof fields: a foreign schedulerName or the Kueue queue
        # label is what makes completion equal scheduling evidence.
        volcano = (manifests_dir / "volcano-smoke-job.yaml").read_text(encoding="utf-8")
        assert "schedulerName: volcano" in volcano
        yunikorn = (manifests_dir / "yunikorn-smoke-job.yaml").read_text(encoding="utf-8")
        assert "schedulerName: yunikorn" in yunikorn
        assert "yunikorn.apache.org/queue: root.default" in yunikorn
        kueue = (manifests_dir / "kueue-smoke-job.yaml").read_text(encoding="utf-8")
        assert "kueue.x-k8s.io/queue-name: gco-default" in kueue
        slurm = (manifests_dir / "slurm-smoke-job.yaml").read_text(encoding="utf-8")
        assert 'gco.aws/slurm-client: "true"' in slurm
        assert "slinky-slurm-auth-jwt" in slurm
        assert "slinky-slurm-restapi.gco-jobs.svc.cluster.local:6820" in slurm

    def test_kueue_probe_queue_exists_in_the_deployed_topology(self) -> None:
        """The queue the probe names must be the queue the applier deploys."""
        root = Path(__file__).resolve().parents[1]
        deployed = (
            root / "lambda/kubectl-applier-simple/manifests/post-helm-kueue-default-queues.yaml"
        ).read_text(encoding="utf-8")
        assert "name: gco-default" in deployed
        assert "kind: LocalQueue" in deployed


class TestLocalRunbookContracts:
    def test_live_validation_has_no_github_actions_workflow(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow_dir = root / ".github/workflows"
        forbidden_workflow = workflow_dir / "live-release-validation.yml"
        workflows = sorted(
            {
                *workflow_dir.rglob("*.yml"),
                *workflow_dir.rglob("*.yaml"),
            }
        )

        assert not forbidden_workflow.exists()
        assert workflows
        for workflow in workflows:
            content = workflow.read_text(encoding="utf-8").casefold()
            for forbidden in (
                "live_release_validation",
                "live-release-validation.md",
                "live-release-validation.json",
                "checkpoint.json",
            ):
                assert forbidden not in content, f"{workflow} contains {forbidden}"

    def test_runbook_requires_local_execution_and_private_reports(self) -> None:
        root = Path(__file__).resolve().parents[1]
        runbook = (root / "docs/LIVE_RELEASE_VALIDATION.md").read_text(encoding="utf-8")
        docs_index = (root / "docs/README.md").read_text(encoding="utf-8")

        contributor_docs = [
            (root / "CONTRIBUTING.md").read_text(encoding="utf-8"),
            (root / ".github/CI.md").read_text(encoding="utf-8"),
            (root / ".github/pull_request_template.md").read_text(encoding="utf-8"),
        ]

        assert "[Live Release Validation](LIVE_RELEASE_VALIDATION.md)" in docs_index
        for required in (
            "local operator process",
            "python -m scripts.live_release_validation",
            "--expected-account",
            "--expected-sha",
            "--expected-branch",
            "--actions all",
            "--confirm-kms-key-deletion",
            "live-release-validation.json",
            "live-release-validation.md",
            "checkpoint.json",
            "--resume",
            "PendingDeletion",
            "seven-day",
            "ECR has no conditional",
            "Usually not required",
            "PARTIAL",
            "selected action scope",
            "sanitized summary",
            "Never post the full report",
            "private maintainer channel",
            "pull request",
        ):
            assert required in runbook

        # Reports enumerate the validation account's ID, ARNs (including KMS
        # keys inside their deletion window), and endpoint URLs. No document
        # may instruct operators to publish them.
        for upload_encouragement in (
            "manually upload",
            "manually attach",
            "attach the generated Markdown report",
            "upload `live-release-validation",
        ):
            assert upload_encouragement not in runbook
            for document in contributor_docs:
                assert upload_encouragement not in document

        for document in contributor_docs:
            normalized = document.casefold()
            assert "live release validation" in normalized
            assert "cli" in normalized
            assert "dependency bump" in normalized
            assert "usually not required" in normalized
            assert "checkpoint.json" in normalized

        assert "There is deliberately no GitHub Actions workflow" in runbook
        assert "Never upload `checkpoint.json`" in runbook
        assert "Ordinary CI runs only mocked/offline contracts" in runbook
        for forbidden in (
            "workflow_dispatch",
            "LIVE_VALIDATION_ROLE_ARN",
            "LIVE_VALIDATION_AWS_ACCOUNT_ID",
            "$RUNNER_TEMP",
        ):
            assert forbidden not in runbook


_MANIFEST_REFERENCE = re.compile(r'_load_manifest\(\s*\w+\s*,\s*"([^"]+)"\s*\)')


class TestManifestPathResolution:
    """Regression guards for run ``retry1-8002d6c80f62``'s ``api`` failure.

    ``_load_manifest`` resolved ``manifests/`` relative to its own module file
    (``Path(__file__).with_name("manifests")``). The modularization moved the
    helper from the package root into ``checks/``, so byte-identical code
    silently pointed at ``checks/manifests/`` — which does not exist — and
    every job-path action died with ``FileNotFoundError`` at runtime, after
    deploy had already run for ~100 minutes. These tests drive the loader's
    real path construction offline (no AWS access) so a module move can never
    ship that failure again.
    """

    @staticmethod
    def _offline_context() -> SimpleNamespace:
        # __init__ builds config + AWS clients, but load_manifests reads no
        # constructor state, so __new__ yields the real production method
        # without touching the network.
        manager = JobManager.__new__(JobManager)
        return SimpleNamespace(
            job_manager=manager,
            settings=SimpleNamespace(run_id="regression-check-1"),
        )

    def test_load_manifest_resolves_every_manifest_on_disk(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest_dir = root / "scripts/live_release_validation/manifests"
        manifest_names = sorted(item.name for item in manifest_dir.glob("*.yaml"))
        assert manifest_names, "manifest fixtures moved; update the harness and this test"
        ctx = self._offline_context()
        for filename in manifest_names:
            manifests, name, namespace = checks_jobs._load_manifest(ctx, filename)
            job_documents = [item for item in manifests if item.get("kind") == "Job"]
            assert job_documents, filename
            assert "regression-check-1" in name, filename
            assert namespace, filename

    def test_manifest_dir_is_the_package_root_manifests_directory(self) -> None:
        root = Path(__file__).resolve().parents[1]
        expected = root / "scripts/live_release_validation/manifests"
        assert expected == constants._MANIFEST_DIR
        assert constants._MANIFEST_DIR.is_dir()

    def test_every_manifest_referenced_by_the_package_exists(self) -> None:
        root = Path(__file__).resolve().parents[1]
        package = root / "scripts/live_release_validation"
        referenced = {
            match.group(1)
            for source in sorted(package.rglob("*.py"))
            for match in _MANIFEST_REFERENCE.finditer(source.read_text(encoding="utf-8"))
        }
        assert referenced, "no _load_manifest call sites found; update the scanner regex"
        for filename in sorted(referenced):
            assert (package / "manifests" / filename).is_file(), filename


class TestThrottleResilientSession:
    """Every harness client gets adaptive throttle retries by default.

    A real ``final-inventory`` failed with ``ThrottlingException … reached
    max retries: 4`` on CloudWatch Logs ``ListTagsForResource`` while
    scanning 17 Regions. The facade must pace and retry that class of
    error without changing any other session behavior.
    """

    def _facade(self):
        from unittest.mock import MagicMock

        from scripts.live_release_validation.aws_session import ThrottleResilientSession

        inner = MagicMock()
        return ThrottleResilientSession(inner), inner

    def test_client_injects_adaptive_retry_config(self) -> None:
        facade, inner = self._facade()

        facade.client("logs", region_name="eu-west-1")

        inner.client.assert_called_once()
        args, kwargs = inner.client.call_args
        assert args == ("logs",)
        assert kwargs["region_name"] == "eu-west-1"
        retries = kwargs["config"].retries
        assert retries["mode"] == "adaptive"
        assert retries["max_attempts"] >= 10

    def test_resource_injects_the_same_retry_config(self) -> None:
        facade, inner = self._facade()

        facade.resource("s3")

        retries = inner.resource.call_args.kwargs["config"].retries
        assert retries["mode"] == "adaptive"

    def test_caller_config_fields_survive_the_merge(self) -> None:
        from botocore.config import Config

        facade, inner = self._facade()

        facade.client("logs", config=Config(read_timeout=7))

        merged = inner.client.call_args.kwargs["config"]
        assert merged.read_timeout == 7
        assert merged.retries["mode"] == "adaptive"

    def test_caller_retry_settings_win_over_the_default(self) -> None:
        from botocore.config import Config

        facade, inner = self._facade()

        facade.client("logs", config=Config(retries={"mode": "standard", "max_attempts": 2}))

        merged = inner.client.call_args.kwargs["config"]
        assert merged.retries == {"mode": "standard", "max_attempts": 2}

    def test_other_session_attributes_delegate_unchanged(self) -> None:
        facade, inner = self._facade()
        inner.get_available_regions.return_value = ["us-east-1"]
        inner.region_name = "us-east-2"

        assert facade.get_available_regions("logs") == ["us-east-1"]
        assert facade.region_name == "us-east-2"

    def test_default_construction_wraps_a_real_boto3_session(self) -> None:
        from unittest.mock import patch

        from scripts.live_release_validation import aws_session

        with patch.object(aws_session.boto3, "Session") as session_cls:
            facade = aws_session.ThrottleResilientSession()

        session_cls.assert_called_once_with()
        assert facade._session is session_cls.return_value

    def test_runner_builds_its_session_through_the_facade(self) -> None:
        """The runner must not hand raw boto3 sessions to the scanners."""
        import inspect

        from scripts.live_release_validation import runner

        source = inspect.getsource(runner.LiveValidationRunner.__init__)
        assert "ThrottleResilientSession()" in source
        assert "boto3.Session()" not in source


class TestStripExpiredTableStreams:
    """``_strip_expired_table_streams`` — deleted-table stream acceptance.

    Pins the exact live failure from 2026-08-20: the previous run's
    ``gco-vector-store`` stream ARNs stayed in the Tagging API index (and the
    streams themselves stayed describable, DISABLED) hours after their tables
    were destroyed, failing the next run's ``baseline`` gate. Acceptance is
    conditional on DynamoDB itself proving the parent table absent; a live
    table keeps its stream entry as genuine residue.
    """

    _ACCOUNT = "123456789012"
    _REGION = "us-east-1"
    _STREAM_ARN = (
        f"arn:aws:dynamodb:{_REGION}:{_ACCOUNT}:table/gco-live-vector-store"
        "/stream/2026-08-20T08:23:45.112"
    )

    @staticmethod
    def _not_found(operation: str) -> ClientError:
        return ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            operation,
        )

    def _inventory(self, *entries: dict[str, object]) -> dict[str, object]:
        return {
            "regional": {
                self._REGION: {
                    "tagged_resources": list(entries),
                    "dynamodb_tables": [],
                }
            }
        }

    def _clients(
        self,
        *,
        table_exists: bool,
        stream_status: str | None = "DISABLED",
    ) -> tuple[SimpleNamespace, MagicMock, MagicMock]:
        dynamodb = MagicMock()
        if table_exists:
            dynamodb.describe_table.return_value = {"Table": {"TableStatus": "ACTIVE"}}
        else:
            dynamodb.describe_table.side_effect = self._not_found("DescribeTable")
        streams = MagicMock()
        if stream_status is None:
            streams.describe_stream.side_effect = self._not_found("DescribeStream")
        else:
            streams.describe_stream.return_value = {
                "StreamDescription": {"StreamStatus": stream_status}
            }
        session = MagicMock()
        session.client.side_effect = lambda service, region_name=None: {
            "dynamodb": dynamodb,
            "dynamodbstreams": streams,
        }[service]
        ctx = _context()
        ctx.session = session
        return ctx, dynamodb, streams

    def test_absent_table_stream_is_stripped_with_evidence(self):
        ctx, dynamodb, streams = self._clients(table_exists=False, stream_status="DISABLED")
        inventory = self._inventory({"arn": self._STREAM_ARN, "tags": {"Project": "GCO"}})
        residual, accepted = ownership_streams._strip_expired_table_streams(ctx, inventory)
        # The stripped entry was the region's only content, so the emptied
        # bucket is popped — the same shape the pending-KMS strip produces.
        assert self._REGION not in residual["regional"]
        assert len(accepted) == 1
        evidence = accepted[0]
        assert evidence["arn"] == self._STREAM_ARN
        assert evidence["table_name"] == "gco-live-vector-store"
        assert evidence["table_absent"] is True
        assert evidence["stream_status"] == "DISABLED"
        assert evidence["tags"] == {"Project": "GCO"}
        dynamodb.describe_table.assert_called_once_with(TableName="gco-live-vector-store")
        streams.describe_stream.assert_called_once_with(StreamArn=self._STREAM_ARN)

    def test_fully_expired_stream_records_absent_status(self):
        ctx, _dynamodb, _streams = self._clients(table_exists=False, stream_status=None)
        inventory = self._inventory({"arn": self._STREAM_ARN, "tags": {}})
        residual, accepted = ownership_streams._strip_expired_table_streams(ctx, inventory)
        assert self._REGION not in residual["regional"]
        assert accepted[0]["stream_status"] == "ABSENT"

    def test_live_table_keeps_its_stream_entry(self):
        ctx, dynamodb, streams = self._clients(table_exists=True)
        entry = {"arn": self._STREAM_ARN, "tags": {}}
        residual, accepted = ownership_streams._strip_expired_table_streams(
            ctx, self._inventory(entry)
        )
        assert residual["regional"][self._REGION]["tagged_resources"] == [entry]
        assert accepted == []
        dynamodb.describe_table.assert_called_once()
        streams.describe_stream.assert_not_called()

    def test_non_stream_and_foreign_entries_are_untouched(self):
        ctx, dynamodb, _streams = self._clients(table_exists=False)
        table_arn = {"arn": f"arn:aws:dynamodb:{self._REGION}:{self._ACCOUNT}:table/gco-live-x"}
        wrong_account = {
            "arn": (
                f"arn:aws:dynamodb:{self._REGION}:999999999999:table/gco-live-y"
                "/stream/2026-01-01T00:00:00.000"
            )
        }
        wrong_region = {
            "arn": (
                f"arn:aws:dynamodb:eu-west-1:{self._ACCOUNT}:table/gco-live-z"
                "/stream/2026-01-01T00:00:00.000"
            )
        }
        nat = {"arn": f"arn:aws:ec2:{self._REGION}:{self._ACCOUNT}:natgateway/nat-abc"}
        residual, accepted = ownership_streams._strip_expired_table_streams(
            ctx, self._inventory(table_arn, wrong_account, wrong_region, nat)
        )
        assert residual["regional"][self._REGION]["tagged_resources"] == [
            table_arn,
            wrong_account,
            wrong_region,
            nat,
        ]
        assert accepted == []
        dynamodb.describe_table.assert_not_called()

    def test_region_bucket_is_popped_when_emptied(self):
        ctx, _dynamodb, _streams = self._clients(table_exists=False)
        inventory = {
            "regional": {
                self._REGION: {
                    "tagged_resources": [{"arn": self._STREAM_ARN, "tags": {}}],
                }
            }
        }
        residual, accepted = ownership_streams._strip_expired_table_streams(ctx, inventory)
        assert residual["regional"] == {}
        assert len(accepted) == 1

    def test_unexpected_describe_table_error_propagates(self):
        ctx, dynamodb, _streams = self._clients(table_exists=False)
        dynamodb.describe_table.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "DescribeTable",
        )
        with pytest.raises(ClientError):
            ownership_streams._strip_expired_table_streams(
                ctx, self._inventory({"arn": self._STREAM_ARN})
            )

    def test_input_inventory_is_not_mutated(self):
        ctx, _dynamodb, _streams = self._clients(table_exists=False)
        inventory = self._inventory({"arn": self._STREAM_ARN, "tags": {}})
        snapshot = json.loads(json.dumps(inventory))
        ownership_streams._strip_expired_table_streams(ctx, inventory)
        assert inventory == snapshot


class TestProjectTargetGroupScanner:
    def test_controller_cluster_tag_owns_orphan_target_group(self):
        client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "TargetGroups": [
                    {
                        "TargetGroupName": "k8s-gco-live-old",
                        "TargetGroupArn": "arn:aws:elasticloadbalancing:us-east-1:123:targetgroup/mine/1",
                    },
                    {
                        "TargetGroupName": "k8s-other-old",
                        "TargetGroupArn": "arn:aws:elasticloadbalancing:us-east-1:123:targetgroup/other/2",
                    },
                ]
            }
        ]
        client.get_paginator.return_value = paginator
        client.describe_tags.return_value = {
            "TagDescriptions": [
                {
                    "ResourceArn": (
                        "arn:aws:elasticloadbalancing:us-east-1:123:targetgroup/mine/1"
                    ),
                    "Tags": [{"Key": "elbv2.k8s.aws/cluster", "Value": "gco-live-us-east-1"}],
                },
                {
                    "ResourceArn": (
                        "arn:aws:elasticloadbalancing:us-east-1:123:targetgroup/other/2"
                    ),
                    "Tags": [{"Key": "elbv2.k8s.aws/cluster", "Value": "other-us-east-1"}],
                },
            ]
        }
        session = MagicMock()
        session.client.return_value = client

        result = inventory_scanners._list_target_groups(session, "us-east-1", "gco-live")

        assert result == ["arn:aws:elasticloadbalancing:us-east-1:123:targetgroup/mine/1"]
        client.describe_tags.assert_called_once()

    def test_pagination_batching_and_all_ownership_signals(self):
        client = MagicMock()
        target_groups = [
            {
                "TargetGroupName": (
                    "gco-live-explicit" if index == 0 else f"k8s-generated-{index}"
                ),
                "TargetGroupArn": (
                    f"arn:aws:elasticloadbalancing:us-east-1:123:targetgroup/group-{index}/id"
                ),
            }
            for index in range(23)
        ]
        client.get_paginator.return_value.paginate.return_value = [
            {"TargetGroups": target_groups[:11]},
            {"TargetGroups": target_groups[11:]},
        ]
        tags_by_index = {
            1: [{"Key": "gco:project", "Value": "gco-live"}],
            2: [{"Key": "elbv2.k8s.aws/cluster", "Value": "gco-live-us-east-1"}],
            3: [{"Key": "eks:eks-cluster-name", "Value": "gco-live-us-east-1"}],
        }

        def describe_tags(*, ResourceArns):
            descriptions = []
            for arn in ResourceArns:
                index = int(arn.split("group-")[1].split("/")[0])
                descriptions.append({"ResourceArn": arn, "Tags": tags_by_index.get(index, [])})
            return {"TagDescriptions": descriptions}

        client.describe_tags.side_effect = describe_tags
        session = MagicMock()
        session.client.return_value = client

        result = inventory_scanners._list_target_groups(session, "us-east-1", "gco-live")

        assert result == sorted(item["TargetGroupArn"] for item in target_groups[:4])
        assert [
            len(call.kwargs["ResourceArns"]) for call in client.describe_tags.call_args_list
        ] == [20, 3]

    def test_target_groups_participate_in_all_zero_gate(self):
        from scripts.live_release_validation.inventory import project as inventory_project

        resources = dict.fromkeys(inventory_project._REGIONAL_PROJECT_RESOURCE_CATEGORIES, [])
        resources["target_groups"] = [
            "arn:aws:elasticloadbalancing:us-east-1:123:targetgroup/orphan/id"
        ]
        inventory_value = {
            "coverage": {
                "complete": True,
                "required_scanners": list(inventory_project._PROJECT_RESOURCE_SCANNERS),
                "completed_scanners": list(inventory_project._PROJECT_RESOURCE_SCANNERS),
                "resource_categories": list(inventory_project._PROJECT_RESOURCE_CATEGORIES),
            },
            "cloudformation_stacks": {},
            "regional": {"us-east-1": resources},
            **{category: [] for category in inventory_project._GLOBAL_PROJECT_RESOURCE_CATEGORIES},
        }

        assert inventory_project.project_resources_are_absent(inventory_value) is False
        resources["target_groups"] = []
        assert inventory_project.project_resources_are_absent(inventory_value) is True

    def test_missing_target_group_arn_fails_closed(self):
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [
            {"TargetGroups": [{"TargetGroupName": "k8s-gco-live-old"}]}
        ]
        session = MagicMock()
        session.client.return_value = client

        with pytest.raises(RuntimeError, match="without an ARN"):
            inventory_scanners._list_target_groups(session, "us-east-1", "gco-live")

        client.describe_tags.assert_not_called()


class TestAcceptedEfsAutomaticBackupRecoveryPoints:
    _REGION = "us-east-1"
    _ACCOUNT = "123456789012"
    _PROJECT = "gco-live"
    _VAULT = "aws/efs/automatic-backup-vault"
    _VAULT_ARN = "arn:aws:backup:us-east-1:123456789012:backup-vault:aws/efs/automatic-backup-vault"
    _POINT_ARN = (
        "arn:aws:backup:us-east-1:123456789012:recovery-point:11111111-2222-3333-4444-555555555555"
    )
    _EFS_ARN = "arn:aws:elasticfilesystem:us-east-1:123456789012:file-system/fs-1234567890abcdef0"

    def _ctx_and_clients(
        self,
        *,
        source_exists: bool = False,
        policy_effect: str = "Deny",
        vault_name: str | None = None,
        efs_error_code: str = "FileSystemNotFound",
    ):
        selected_vault = vault_name or self._VAULT
        vault_arn = (
            self._VAULT_ARN
            if selected_vault == self._VAULT
            else f"arn:aws:backup:{self._REGION}:{self._ACCOUNT}:backup-vault:{selected_vault}"
        )
        backup = MagicMock()
        backup.describe_recovery_point.return_value = {
            "RecoveryPointArn": self._POINT_ARN,
            "BackupVaultName": selected_vault,
            "BackupVaultArn": vault_arn,
            "SourceBackupVaultArn": vault_arn,
            "ResourceType": "EFS",
            "ResourceName": f"{self._PROJECT}-efs-{self._REGION}",
            "ResourceArn": self._EFS_ARN,
            "Status": "COMPLETED",
            "CalculatedLifecycle": {"DeleteAt": datetime.now(UTC) + timedelta(days=35)},
        }
        backup.describe_backup_vault.return_value = {
            "BackupVaultName": selected_vault,
            "BackupVaultArn": vault_arn,
        }
        backup.get_backup_vault_access_policy.return_value = {
            "Policy": json.dumps(
                {
                    "Statement": [
                        {
                            "Effect": policy_effect,
                            "Principal": "*",
                            "Action": "backup:DeleteRecoveryPoint",
                            "Resource": "*",
                        }
                    ]
                }
            )
        }
        backup.list_tags.return_value = {"Tags": {constants._RUN_STACK_TAG: "prior-validation-run"}}
        efs = MagicMock()
        if source_exists:
            efs.describe_file_systems.return_value = {
                "FileSystems": [{"FileSystemId": "fs-1234567890abcdef0"}]
            }
        else:
            efs.describe_file_systems.side_effect = ClientError(
                {"Error": {"Code": efs_error_code, "Message": "gone"}},
                "DescribeFileSystems",
            )
        ctx = _context()
        ctx.config.project_name = self._PROJECT
        ctx.session = MagicMock()
        ctx.session.get_partition_for_region.return_value = "aws"
        ctx.session.client.side_effect = lambda service, region_name=None: {
            "backup": backup,
            "efs": efs,
        }[service]
        return ctx, backup, efs

    def _inventory(
        self,
        *,
        duplicate: bool = False,
        point_arn: str | None = None,
    ) -> dict[str, object]:
        selected_arn = point_arn or self._POINT_ARN
        points = [selected_arn, selected_arn] if duplicate else [selected_arn]
        return {
            "regional": {
                self._REGION: {
                    "backup_recovery_points": points,
                    "tagged_resources": [
                        {"arn": selected_arn, "tags": {"gco:project": self._PROJECT}}
                    ],
                    "dynamodb_tables": [],
                }
            }
        }

    def test_exact_automatic_backup_is_stripped_with_evidence(self):
        ctx, backup, efs = self._ctx_and_clients()
        inventory = self._inventory()
        snapshot = json.loads(json.dumps(inventory))

        cleaned, accepted = (
            ownership_efs_backups._strip_accepted_efs_automatic_backup_recovery_points(
                ctx, inventory
            )
        )

        assert cleaned == {"regional": {}}
        assert inventory == snapshot
        assert len(accepted) == 1
        assert accepted[0]["recovery_point_arn"] == self._POINT_ARN
        assert accepted[0]["source_file_system_absent"] is True
        assert accepted[0]["vault_policy_unconditional_delete_deny"] is True
        assert accepted[0]["validation_run_tag"] == "prior-validation-run"
        backup.describe_recovery_point.assert_called_once_with(
            BackupVaultName=self._VAULT,
            RecoveryPointArn=self._POINT_ARN,
        )
        efs.describe_file_systems.assert_called_once_with(FileSystemId="fs-1234567890abcdef0")

    def test_project_owned_resource_name_is_sufficient_when_aws_drops_tags(self):
        ctx, backup, _efs = self._ctx_and_clients()
        backup.list_tags.return_value = {"Tags": {}}

        cleaned, accepted = (
            ownership_efs_backups._strip_accepted_efs_automatic_backup_recovery_points(
                ctx, self._inventory()
            )
        )

        assert cleaned == {"regional": {}}
        assert accepted[0]["validation_run_tag"] is None

    @pytest.mark.parametrize(
        ("source_exists", "policy_effect", "vault_name"),
        [
            (True, "Deny", None),
            (False, "Allow", None),
            (False, "Deny", "other-vault"),
        ],
        ids=["live-source", "no-delete-deny", "wrong-vault"],
    )
    def test_unproven_shapes_remain_residual(
        self,
        source_exists: bool,
        policy_effect: str,
        vault_name: str | None,
    ):
        ctx, _backup, _efs = self._ctx_and_clients(
            source_exists=source_exists,
            policy_effect=policy_effect,
            vault_name=vault_name,
        )

        cleaned, accepted = (
            ownership_efs_backups._strip_accepted_efs_automatic_backup_recovery_points(
                ctx, self._inventory()
            )
        )

        assert accepted == []
        assert cleaned["regional"][self._REGION]["backup_recovery_points"] == [self._POINT_ARN]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("RecoveryPointArn", "arn:aws:backup:us-east-1:123456789012:recovery-point:other"),
            ("BackupVaultArn", "arn:aws:backup:us-east-1:123456789012:backup-vault:other"),
            ("SourceBackupVaultArn", "arn:aws:backup:us-east-1:123456789012:backup-vault:other"),
            ("ResourceType", "EC2"),
            ("Status", "CREATING"),
            (
                "ResourceArn",
                "arn:aws:elasticfilesystem:us-west-2:123456789012:file-system/fs-1234567890abcdef0",
            ),
            ("ResourceName", "another-project-efs"),
        ],
    )
    def test_authoritative_identity_mismatches_remain_residual(self, field: str, value: str):
        ctx, backup, _efs = self._ctx_and_clients()
        backup.describe_recovery_point.return_value[field] = value

        cleaned, accepted = (
            ownership_efs_backups._strip_accepted_efs_automatic_backup_recovery_points(
                ctx, self._inventory()
            )
        )

        assert accepted == []
        assert cleaned["regional"][self._REGION]["backup_recovery_points"] == [self._POINT_ARN]

    @pytest.mark.parametrize(
        "point_arn",
        [
            "arn:aws-us-gov:backup:us-east-1:123456789012:recovery-point:x",
            "arn:aws:backup:us-west-2:123456789012:recovery-point:x",
            "arn:aws:backup:us-east-1:999999999999:recovery-point:x",
        ],
    )
    def test_candidate_arn_scope_mismatches_never_trigger_acceptance_calls(self, point_arn: str):
        ctx, backup, _efs = self._ctx_and_clients()

        cleaned, accepted = (
            ownership_efs_backups._strip_accepted_efs_automatic_backup_recovery_points(
                ctx, self._inventory(point_arn=point_arn)
            )
        )

        assert accepted == []
        assert cleaned["regional"][self._REGION]["backup_recovery_points"] == [point_arn]
        backup.describe_recovery_point.assert_not_called()

    @pytest.mark.parametrize(
        "delete_at",
        [None, "2030-01-01T00:00:00Z", datetime(2030, 1, 1)],
        ids=["missing", "string", "naive"],
    )
    def test_scheduled_deletion_requires_an_aware_datetime(self, delete_at):
        ctx, backup, _efs = self._ctx_and_clients()
        backup.describe_recovery_point.return_value["CalculatedLifecycle"] = {"DeleteAt": delete_at}

        cleaned, accepted = (
            ownership_efs_backups._strip_accepted_efs_automatic_backup_recovery_points(
                ctx, self._inventory()
            )
        )

        assert accepted == []
        assert cleaned["regional"][self._REGION]["backup_recovery_points"] == [self._POINT_ARN]

    def test_malformed_validation_run_tag_remains_residual(self):
        ctx, backup, _efs = self._ctx_and_clients()
        backup.list_tags.return_value = {"Tags": {constants._RUN_STACK_TAG: " bad tag "}}

        cleaned, accepted = (
            ownership_efs_backups._strip_accepted_efs_automatic_backup_recovery_points(
                ctx, self._inventory()
            )
        )

        assert accepted == []
        assert cleaned["regional"][self._REGION]["backup_recovery_points"] == [self._POINT_ARN]

    def test_unexpected_backup_error_propagates(self):
        ctx, backup, _efs = self._ctx_and_clients()
        backup.describe_recovery_point.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "DescribeRecoveryPoint",
        )
        with pytest.raises(ClientError):
            ownership_efs_backups._strip_accepted_efs_automatic_backup_recovery_points(
                ctx, self._inventory()
            )

    def test_unexpected_efs_error_propagates(self):
        ctx, _backup, _efs = self._ctx_and_clients(efs_error_code="AccessDeniedException")
        with pytest.raises(ClientError):
            ownership_efs_backups._strip_accepted_efs_automatic_backup_recovery_points(
                ctx, self._inventory()
            )

    def test_duplicate_candidate_fails_closed(self):
        ctx, _backup, _efs = self._ctx_and_clients()
        with pytest.raises(RuntimeError, match="Duplicate or empty"):
            ownership_efs_backups._strip_accepted_efs_automatic_backup_recovery_points(
                ctx, self._inventory(duplicate=True)
            )

    def test_vault_policy_must_be_an_unconditional_exact_delete_deny(self):
        valid = {
            "Statement": {
                "Effect": "Deny",
                "Principal": {"AWS": "*"},
                "Action": ["backup:DeleteRecoveryPoint"],
                "Resource": [self._POINT_ARN],
            }
        }
        assert ownership_efs_backups._policy_has_unconditional_delete_deny(
            json.dumps(valid), self._POINT_ARN
        )

        conditional = json.loads(json.dumps(valid))
        conditional["Statement"]["Condition"] = {"Bool": {"aws:SecureTransport": "false"}}
        assert not ownership_efs_backups._policy_has_unconditional_delete_deny(
            json.dumps(conditional), self._POINT_ARN
        )
        wildcard_action = json.loads(json.dumps(valid))
        wildcard_action["Statement"]["Action"] = "backup:*"
        assert not ownership_efs_backups._policy_has_unconditional_delete_deny(
            json.dumps(wildcard_action), self._POINT_ARN
        )
        scoped_principal = json.loads(json.dumps(valid))
        scoped_principal["Statement"]["Principal"] = {"AWS": "arn:aws:iam::123456789012:root"}
        assert not ownership_efs_backups._policy_has_unconditional_delete_deny(
            json.dumps(scoped_principal), self._POINT_ARN
        )
        wrong_resource = json.loads(json.dumps(valid))
        wrong_resource["Statement"]["Resource"] = (
            "arn:aws:backup:us-east-1:123:recovery-point:other"
        )
        assert not ownership_efs_backups._policy_has_unconditional_delete_deny(
            json.dumps(wrong_resource), self._POINT_ARN
        )
        not_action = json.loads(json.dumps(valid))
        not_action["Statement"]["NotAction"] = "backup:StartRestoreJob"
        assert not ownership_efs_backups._policy_has_unconditional_delete_deny(
            json.dumps(not_action), self._POINT_ARN
        )

    def test_duplicate_policy_keys_fail_closed(self):
        with pytest.raises(RuntimeError, match="invalid JSON"):
            ownership_efs_backups._policy_has_unconditional_delete_deny(
                '{"Statement": [], "Statement": []}', self._POINT_ARN
            )


class TestActionBaselineCheckpointPurity:
    """``action_baseline`` — report enrichment must not leak into the checkpoint.

    The action returns ``{**baseline, "accepted_expired_dynamodb_streams"}``
    for the report, but persists the *unadorned* capture as
    ``ctx.checkpoint.baseline``: ``final-inventory`` later feeds that
    checkpoint straight into ``compare_baseline`` against a fresh capture, so
    any extra key smuggled into the persisted copy would surface as a
    phantom protected-baseline difference and fail an otherwise clean run.
    """

    _BASELINE = {
        "protected_stacks": {},
        "ecr_regions": ["us-east-1"],
        "ecr_repositories": {"us-east-1": []},
    }

    @staticmethod
    def _inventory(*tagged: dict[str, object]) -> dict[str, object]:
        """A shape-valid inventory: the absence gate is fail-closed on
        coverage, so the mocked collect_project_resources must return the
        full scanner/category attestation a real scan produces."""
        from scripts.live_release_validation.inventory import project as inventory_project

        return {
            "coverage": {
                "complete": True,
                "required_scanners": list(inventory_project._PROJECT_RESOURCE_SCANNERS),
                "completed_scanners": list(inventory_project._PROJECT_RESOURCE_SCANNERS),
                "resource_categories": list(inventory_project._PROJECT_RESOURCE_CATEGORIES),
            },
            "regional": {"us-east-1": {"tagged_resources": list(tagged)}},
        }

    def _ctx(self):
        ctx = _context(state={"enabled_regions": ["us-east-1"]})
        ctx.checkpoint.baseline = None
        ctx.settings.protected_stack_names = ("CDKToolkit",)
        return ctx

    def _run(self, ctx, inventory, *, accepted_efs=None):
        from scripts.live_release_validation.actions import baseline as actions_baseline

        with (
            patch.object(actions_baseline, "capture_baseline", return_value=dict(self._BASELINE)),
            patch.object(actions_baseline, "collect_project_resources", return_value=inventory),
            patch.object(actions_baseline, "_topology_regions", return_value=["us-east-1"]),
            patch.object(
                actions_baseline,
                "_strip_accepted_efs_automatic_backup_recovery_points",
                side_effect=lambda _ctx, candidate: (candidate, list(accepted_efs or [])),
            ),
        ):
            return actions_baseline.action_baseline(ctx)

    def test_accepted_streams_ride_the_result_not_the_checkpoint(self):
        ctx = self._ctx()
        stream_arn = (
            "arn:aws:dynamodb:us-east-1:123456789012:table/gco-live-vector-store"
            "/stream/2026-08-20T08:23:45.112"
        )
        dynamodb = MagicMock()
        dynamodb.describe_table.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            "DescribeTable",
        )
        streams = MagicMock()
        streams.describe_stream.return_value = {"StreamDescription": {"StreamStatus": "DISABLED"}}
        ctx.session = MagicMock()
        ctx.session.client.side_effect = lambda service, region_name=None: {
            "dynamodb": dynamodb,
            "dynamodbstreams": streams,
        }[service]
        result = self._run(ctx, self._inventory({"arn": stream_arn, "tags": {}}))

        accepted = result["accepted_expired_dynamodb_streams"]
        assert len(accepted) == 1
        assert accepted[0]["arn"] == stream_arn
        # The persisted checkpoint is the pure capture — byte-for-byte what
        # compare_baseline will re-capture against after teardown.
        assert ctx.checkpoint.baseline == self._BASELINE
        assert "accepted_expired_dynamodb_streams" not in ctx.checkpoint.baseline
        ctx.persist.assert_called()

    def test_accepted_efs_backup_rides_result_not_checkpoint(self):
        ctx = self._ctx()
        evidence = {"recovery_point_arn": "arn:aws:backup:us-east-1:123:recovery-point:x"}

        result = self._run(ctx, self._inventory(), accepted_efs=[evidence])

        assert result["accepted_efs_automatic_backup_recovery_points"] == [evidence]
        assert ctx.checkpoint.baseline == self._BASELINE
        assert "accepted_efs_automatic_backup_recovery_points" not in ctx.checkpoint.baseline
        assert ctx.checkpoint.state["baseline_accepted_efs_automatic_backup_recovery_points"] == [
            evidence
        ]

        from scripts.live_release_validation.actions import baseline as actions_baseline

        resumed = actions_baseline.action_baseline(ctx)
        assert resumed["reused_checkpoint_baseline"] is True
        assert resumed["accepted_efs_automatic_backup_recovery_points"] == [evidence]

    def test_live_table_stream_still_fails_the_gate(self):
        """A stream whose parent table exists is genuine residue: hard fail."""
        ctx = self._ctx()
        dynamodb = MagicMock()
        dynamodb.describe_table.return_value = {"Table": {"TableStatus": "ACTIVE"}}
        ctx.session = MagicMock()
        ctx.session.client.return_value = dynamodb
        inventory = self._inventory(
            {
                "arn": (
                    "arn:aws:dynamodb:us-east-1:123456789012:table/"
                    "gco-live-x/stream/2026-01-01T00:00:00.000"
                ),
                "tags": {},
            }
        )

        with pytest.raises(RuntimeError, match="not owned by this run"):
            self._run(ctx, inventory)
        # A failed gate must not persist a baseline for a later resume.
        assert ctx.checkpoint.baseline is None

    def test_clean_account_returns_empty_acceptance(self):
        ctx = self._ctx()
        result = self._run(ctx, self._inventory())
        assert result["accepted_efs_automatic_backup_recovery_points"] == []
        assert result["accepted_expired_dynamodb_streams"] == []
        assert ctx.checkpoint.baseline == self._BASELINE

    def test_reused_checkpoint_short_circuits(self):
        ctx = self._ctx()
        ctx.checkpoint.baseline = dict(self._BASELINE)
        from scripts.live_release_validation.actions import baseline as actions_baseline

        with patch.object(actions_baseline, "capture_baseline") as capture:
            result = actions_baseline.action_baseline(ctx)
        capture.assert_not_called()
        assert result["reused_checkpoint_baseline"] is True


class TestOpenCostLiveValidationRetry:
    """The disposable-account report check retries only the exact ambiguous 504."""

    @staticmethod
    def _success_response():
        return _response(
            201,
            {
                "region": "us-east-1",
                "bucket": "gco-live-cost-reports-123456789012-us-east-1",
                "report": {
                    "s3_key": (
                        "adhoc/region=us-east-1/date=2026-08-29/"
                        "allocation-20260829T000000Z-20260829T010000Z-a1b2c3d4.parquet"
                    ),
                    "row_count": 3,
                    "total_cost": 1.25,
                },
            },
        )

    @staticmethod
    def _bridge_timeout_response():
        payload = {
            "error": "Gateway timeout",
            "message": "Upstream failed after 1 attempt(s)",
        }
        return _response(504, payload, text=json.dumps(payload))

    def test_exact_bridge_timeout_retries_once_and_records_duplicate_risk(self):
        ctx = _context()
        ctx.aws_client.make_authenticated_request.side_effect = [
            self._bridge_timeout_response(),
            self._success_response(),
        ]

        with patch.object(checks_opencost.time, "sleep") as sleep:
            result = checks_opencost._generate_validation_report(ctx, "us-east-1")

        assert result["duplicate_possible"] is True
        assert [item["status_code"] for item in result["request_attempts"]] == [504, 201]
        assert result["request_attempts"][0]["retry_scheduled"] is True
        assert result["request_attempts"][1]["retry_scheduled"] is False
        sleep.assert_called_once_with(checks_opencost._REPORT_RETRY_DELAY_SECONDS)
        assert ctx.aws_client.make_authenticated_request.call_count == 2
        checkpointed = ctx.checkpoint.state["opencost_report_attempts"]["us-east-1"]
        assert checkpointed["duplicate_possible"] is True
        assert len(checkpointed["attempts"]) == 2
        # Intent, first response, second intent, second response, validated report.
        assert ctx.persist_callback.call_count == 5

    def test_resume_consumes_only_remaining_attempt_and_keeps_ambiguity(self):
        timeout_payload = {
            "error": "Gateway timeout",
            "message": "Upstream failed after 1 attempt(s)",
        }
        ctx = _context(
            state={
                "opencost_report_attempts": {
                    "us-east-1": {
                        "attempts": [
                            {
                                "attempt": 1,
                                "state": "completed",
                                "started_at": "2026-08-29T00:00:00+00:00",
                                "ended_at": "2026-08-29T00:00:30+00:00",
                                "status_code": 504,
                                "exact_bridge_timeout": True,
                                "response_text": json.dumps(timeout_payload),
                                "retry_scheduled": True,
                            }
                        ],
                        "duplicate_possible": True,
                        "completed_report": None,
                    }
                }
            }
        )
        ctx.aws_client.make_authenticated_request.return_value = self._success_response()

        with patch.object(checks_opencost.time, "sleep") as sleep:
            result = checks_opencost._generate_validation_report(ctx, "us-east-1")

        ctx.aws_client.make_authenticated_request.assert_called_once()
        sleep.assert_called_once_with(checks_opencost._REPORT_RETRY_DELAY_SECONDS)
        assert result["duplicate_possible"] is True
        assert [item["attempt"] for item in result["request_attempts"]] == [1, 2]
        checkpointed = ctx.checkpoint.state["opencost_report_attempts"]["us-east-1"]
        assert checkpointed["duplicate_possible"] is True
        assert checkpointed["completed_report"]["row_count"] == 3

    def test_resume_reuses_completed_report_without_another_post(self):
        ctx = _context(
            state={
                "opencost_report_attempts": {
                    "us-east-1": {
                        "attempts": [
                            {
                                "attempt": 1,
                                "state": "completed",
                                "started_at": "2026-08-29T00:00:00+00:00",
                                "ended_at": "2026-08-29T00:00:01+00:00",
                                "status_code": 201,
                                "exact_bridge_timeout": False,
                                "response_text": "",
                                "retry_scheduled": False,
                            }
                        ],
                        "duplicate_possible": False,
                        "completed_report": {
                            "region": "us-east-1",
                            "bucket": "gco-live-cost-reports-123456789012-us-east-1",
                            "s3_key": (
                                "adhoc/region=us-east-1/date=2026-08-29/"
                                "allocation-20260829T000000Z-20260829T010000Z-deadbeef.parquet"
                            ),
                            "row_count": 2,
                        },
                    }
                }
            }
        )

        result = checks_opencost._generate_validation_report(ctx, "us-east-1")

        ctx.aws_client.make_authenticated_request.assert_not_called()
        assert result["s3_key"].endswith("deadbeef.parquet")
        assert result["request_attempts"][0]["attempt"] == 1

    @pytest.mark.parametrize(
        ("completed_report", "message"),
        [
            (
                {
                    "region": "us-east-1",
                    "s3_key": (
                        "adhoc/region=us-east-1/date=2026-08-29/"
                        "allocation-20260829T000000Z-20260829T010000Z-deadbeef.parquet"
                    ),
                    "row_count": 2,
                },
                "omitted its bucket",
            ),
            (
                {
                    "region": "us-east-1",
                    "bucket": "gco-live-cost-reports-123456789012-us-east-1",
                    "row_count": 2,
                },
                "omitted its S3 key",
            ),
            (
                {
                    "region": "us-east-1",
                    "bucket": "gco-live-cost-reports-123456789012-us-east-1",
                    "s3_key": (
                        "adhoc/region=us-east-1/date=2026-08-29/"
                        "allocation-20260829T000000Z-20260829T010000Z-deadbeef.parquet"
                    ),
                    "row_count": 0,
                },
                "zero allocation rows",
            ),
            (
                {
                    "region": "us-west-2",
                    "bucket": "gco-live-cost-reports-123456789012-us-east-1",
                    "s3_key": (
                        "adhoc/region=us-west-2/date=2026-08-29/"
                        "allocation-20260829T000000Z-20260829T010000Z-deadbeef.parquet"
                    ),
                    "row_count": 2,
                },
                "returned Region",
            ),
            (
                {
                    "region": "us-east-1",
                    "bucket": "wrong-cost-reports-bucket",
                    "s3_key": (
                        "adhoc/region=us-east-1/date=2026-08-29/"
                        "allocation-20260829T000000Z-20260829T010000Z-deadbeef.parquet"
                    ),
                    "row_count": 2,
                },
                "unexpected bucket",
            ),
            (
                {
                    "region": "us-east-1",
                    "bucket": "gco-live-cost-reports-123456789012-us-east-1",
                    "s3_key": (
                        "adhoc/region=us-west-2/date=2026-08-29/"
                        "allocation-20260829T000000Z-20260829T010000Z-deadbeef.parquet"
                    ),
                    "row_count": 2,
                },
                "unexpected S3 key",
            ),
        ],
        ids=[
            "missing-bucket",
            "missing-key",
            "zero-rows",
            "wrong-region",
            "wrong-bucket",
            "wrong-key-region",
        ],
    )
    def test_resume_rejects_invalid_completed_report(self, completed_report, message):
        ctx = _context(
            state={
                "opencost_report_attempts": {
                    "us-east-1": {
                        "attempts": [
                            {
                                "attempt": 1,
                                "state": "completed",
                                "started_at": "2026-08-29T00:00:00+00:00",
                                "ended_at": "2026-08-29T00:00:01+00:00",
                                "status_code": 201,
                                "exact_bridge_timeout": False,
                                "response_text": "",
                                "retry_scheduled": False,
                            }
                        ],
                        "duplicate_possible": False,
                        "completed_report": completed_report,
                    }
                }
            }
        )

        with pytest.raises(RuntimeError, match=message):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_resume_blocks_an_unresolved_inflight_post(self):
        ctx = _context(
            state={
                "opencost_report_attempts": {
                    "us-east-1": {
                        "attempts": [
                            {
                                "attempt": 1,
                                "state": "started",
                                "started_at": "2026-08-29T00:00:00+00:00",
                            }
                        ],
                        "duplicate_possible": False,
                        "completed_report": None,
                    }
                }
            }
        )

        with pytest.raises(RuntimeError, match="ambiguous in-flight outcome"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

        ctx.aws_client.make_authenticated_request.assert_not_called()

    @pytest.mark.parametrize(
        "journal",
        [
            None,
            {
                "attempts": [],
                "duplicate_possible": False,
                "completed_report": None,
            },
            {
                "attempts": [
                    {
                        "attempt": 1,
                        "state": "completed",
                        "started_at": "2026-08-29T00:00:00+00:00",
                        "ended_at": "2026-08-29T00:00:30+00:00",
                        "exact_bridge_timeout": True,
                        "retry_scheduled": True,
                    }
                ],
                "duplicate_possible": True,
                "completed_report": None,
            },
            {
                "attempts": [
                    {
                        "attempt": 1,
                        "state": "completed",
                        "started_at": "2026-08-29T00:00:00+00:00",
                        "ended_at": "2026-08-29T00:00:30+00:00",
                        "status_code": 503,
                        "exact_bridge_timeout": True,
                        "response_text": "service unavailable",
                        "retry_scheduled": True,
                    }
                ],
                "duplicate_possible": True,
                "completed_report": None,
            },
            {
                "attempts": [
                    {
                        "attempt": 1,
                        "state": "completed",
                        "started_at": "2026-08-29T00:00:00+00:00",
                        "ended_at": "2026-08-29T00:00:30+00:00",
                        "status_code": 504,
                        "exact_bridge_timeout": True,
                        "response_text": json.dumps(
                            {
                                "error": "Gateway timeout",
                                "message": "Upstream failed after 1 attempt(s)",
                            }
                        ),
                        "retry_scheduled": True,
                    }
                ],
                "duplicate_possible": False,
                "completed_report": None,
            },
            {
                "attempts": [
                    {
                        "attempt": 1,
                        "state": "completed",
                        "started_at": "2026-08-29T00:00:00+00:00",
                        "ended_at": "2026-08-29T00:00:01+00:00",
                        "status_code": 503,
                        "exact_bridge_timeout": False,
                        "response_text": "service unavailable",
                        "retry_scheduled": False,
                    },
                    {
                        "attempt": 2,
                        "state": "started",
                        "started_at": "2026-08-29T00:00:02+00:00",
                    },
                ],
                "duplicate_possible": False,
                "completed_report": None,
            },
        ],
        ids=[
            "null-region-history",
            "explicit-empty-history",
            "retry-flags-without-response",
            "timeout-flag-with-non-timeout-response",
            "nonsticky-duplicate-flag",
            "second-attempt-without-timeout-ancestry",
        ],
    )
    def test_corrupt_retry_journal_fails_closed(self, journal):
        ctx = _context(state={"opencost_report_attempts": {"us-east-1": journal}})

        with pytest.raises(RuntimeError, match="OpenCost .*checkpoint"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

        ctx.aws_client.make_authenticated_request.assert_not_called()

    @pytest.mark.parametrize("root", [None, {}], ids=["null", "empty"])
    def test_present_invalid_journal_root_fails_closed(self, root):
        ctx = _context(state={"opencost_report_attempts": root})

        with pytest.raises(RuntimeError, match="checkpoint root is malformed"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_malformed_sibling_journal_blocks_fresh_region_post(self):
        ctx = _context(state={"opencost_report_attempts": {"us-west-2": None}})
        ctx.deployment_regions = ("us-east-1", "us-west-2")

        with pytest.raises(RuntimeError, match="us-west-2.*malformed"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_valid_sibling_journal_allows_fresh_region_post(self):
        sibling = {
            "attempts": [
                {
                    "attempt": 1,
                    "state": "completed",
                    "started_at": "2026-08-29T00:00:00+00:00",
                    "ended_at": "2026-08-29T00:00:01+00:00",
                    "status_code": 503,
                    "exact_bridge_timeout": False,
                    "response_text": "service unavailable",
                    "retry_scheduled": False,
                }
            ],
            "duplicate_possible": False,
            "completed_report": None,
        }
        ctx = _context(state={"opencost_report_attempts": {"us-west-2": sibling}})
        ctx.deployment_regions = ("us-east-1", "us-west-2")
        ctx.aws_client.make_authenticated_request.return_value = self._success_response()

        result = checks_opencost._generate_validation_report(ctx, "us-east-1")

        ctx.aws_client.make_authenticated_request.assert_called_once()
        assert result["region"] == "us-east-1"
        assert set(ctx.checkpoint.state["opencost_report_attempts"]) == {
            "us-east-1",
            "us-west-2",
        }

    def test_unknown_sibling_region_blocks_fresh_region_post(self):
        ctx = _context(state={"opencost_report_attempts": {"moon-1": None}})

        with pytest.raises(RuntimeError, match="unexpected Region 'moon-1'"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

        ctx.aws_client.make_authenticated_request.assert_not_called()

    @pytest.mark.parametrize("attempt_number", [True, 1.0], ids=["boolean", "float"])
    def test_non_integer_attempt_number_cannot_authorize_retry(self, attempt_number):
        timeout = self._bridge_timeout_response()
        journal = {
            "attempts": [
                {
                    "attempt": attempt_number,
                    "state": "completed",
                    "started_at": "2026-08-29T00:00:00+00:00",
                    "ended_at": "2026-08-29T00:00:30+00:00",
                    "status_code": 504,
                    "exact_bridge_timeout": True,
                    "response_text": timeout.text,
                    "retry_scheduled": True,
                }
            ],
            "duplicate_possible": True,
            "completed_report": None,
        }
        ctx = _context(state={"opencost_report_attempts": {"us-east-1": journal}})

        with pytest.raises(RuntimeError, match="invalid ordering"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_duplicate_key_timeout_journal_cannot_authorize_retry(self):
        duplicate_key_body = (
            '{"error":"not-a-timeout","error":"Gateway timeout",'
            '"message":"Upstream failed after 1 attempt(s)"}'
        )
        journal = {
            "attempts": [
                {
                    "attempt": 1,
                    "state": "completed",
                    "started_at": "2026-08-29T00:00:00+00:00",
                    "ended_at": "2026-08-29T00:00:30+00:00",
                    "status_code": 504,
                    "exact_bridge_timeout": True,
                    "response_text": duplicate_key_body,
                    "retry_scheduled": True,
                }
            ],
            "duplicate_possible": True,
            "completed_report": None,
        }
        ctx = _context(state={"opencost_report_attempts": {"us-east-1": journal}})

        with pytest.raises(RuntimeError, match="invalid timeout evidence"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

        ctx.aws_client.make_authenticated_request.assert_not_called()

    @pytest.mark.parametrize(
        "response",
        [
            _response(503, {"error": "Service unavailable"}, text="unavailable"),
            _response(504, {"error": "Gateway timeout"}, text="missing exact message"),
            _response(
                504,
                {
                    "error": "Gateway timeout",
                    "message": "Upstream failed after 1 attempt(s)",
                    "request_id": "unexpected-extra-field",
                },
                text="superset timeout body",
            ),
            _response(
                504,
                {
                    "error": "Gateway timeout",
                    "message": "Upstream failed after 1 attempt(s)",
                },
                text=(
                    '{"error":"not-a-timeout","error":"Gateway timeout",'
                    '"message":"Upstream failed after 1 attempt(s)"}'
                ),
            ),
            _response(422, {"detail": "invalid window"}, text="invalid window"),
        ],
        ids=[
            "service-unavailable",
            "nonexact-504",
            "superset-504",
            "duplicate-key-504",
            "invalid-request",
        ],
    )
    def test_other_failures_are_not_replayed(self, response):
        ctx = _context()
        ctx.aws_client.make_authenticated_request.return_value = response

        with (
            patch.object(checks_opencost.time, "sleep") as sleep,
            pytest.raises(RuntimeError, match="Ad-hoc cost report"),
        ):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

        ctx.aws_client.make_authenticated_request.assert_called_once()
        sleep.assert_not_called()
        checkpointed = ctx.checkpoint.state["opencost_report_attempts"]["us-east-1"]
        assert checkpointed["duplicate_possible"] is False
        assert len(checkpointed["attempts"]) == 1

    def test_second_exact_timeout_fails_with_both_attempts_checkpointed(self):
        ctx = _context()
        ctx.aws_client.make_authenticated_request.side_effect = [
            self._bridge_timeout_response(),
            self._bridge_timeout_response(),
        ]

        with (
            patch.object(checks_opencost.time, "sleep") as sleep,
            pytest.raises(RuntimeError, match="504.*Gateway timeout"),
        ):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

        sleep.assert_called_once_with(checks_opencost._REPORT_RETRY_DELAY_SECONDS)
        checkpointed = ctx.checkpoint.state["opencost_report_attempts"]["us-east-1"]
        assert checkpointed["duplicate_possible"] is True
        assert [item["status_code"] for item in checkpointed["attempts"]] == [504, 504]

    def test_first_attempt_success_is_not_marked_ambiguous(self):
        ctx = _context()
        ctx.aws_client.make_authenticated_request.return_value = self._success_response()

        with patch.object(checks_opencost.time, "sleep") as sleep:
            result = checks_opencost._generate_validation_report(ctx, "us-east-1")

        assert result["duplicate_possible"] is False
        assert [item["status_code"] for item in result["request_attempts"]] == [201]
        sleep.assert_not_called()

    def test_malformed_success_is_checkpointed_and_never_replayed(self):
        ctx = _context()
        malformed = _response(201, text="not-json")
        malformed.json.side_effect = ValueError("invalid JSON")
        ctx.aws_client.make_authenticated_request.return_value = malformed

        with pytest.raises(RuntimeError, match="returned invalid JSON"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")

        checkpointed = ctx.checkpoint.state["opencost_report_attempts"]["us-east-1"]
        assert checkpointed["attempts"][-1]["state"] == "completed"
        assert checkpointed["attempts"][-1]["status_code"] == 201
        assert checkpointed["completed_report"] is None
        assert ctx.persist_callback.call_count == 2

        ctx.aws_client.make_authenticated_request.reset_mock()
        with pytest.raises(RuntimeError, match="successful HTTP outcome.*no validated report"):
            checks_opencost._generate_validation_report(ctx, "us-east-1")
        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_action_checkpoints_healthy_status_before_report_generation(self):
        ctx = _context()
        status = {
            "opencost_healthy": True,
            "opencost_returning_data": True,
            "allocation_names": ["gco-system"],
        }
        with (
            patch.object(actions_opencost, "_cost_monitoring_configured", return_value=True),
            patch.object(actions_opencost, "_wait_for_opencost_data", return_value=status),
            patch.object(
                actions_opencost,
                "_generate_validation_report",
                side_effect=RuntimeError("report failed"),
            ),
            pytest.raises(RuntimeError, match="report failed"),
        ):
            actions_opencost.action_opencost(ctx)

        assert ctx.checkpoint.state["opencost_status"]["us-east-1"] is status
        ctx.persist_callback.assert_called_once_with(ctx.checkpoint)


class TestInferencePreflightPrerequisites:
    def test_missing_session_manager_plugin_fails_before_any_aws_call(self, tmp_path: Path) -> None:
        from scripts.live_release_validation.actions import preflight

        session = MagicMock()
        ctx = SimpleNamespace(
            settings=SimpleNamespace(
                repo_root=tmp_path,
                expected_sha="a" * 40,
                expected_branch="feature/test",
            ),
            report=SimpleNamespace(
                selected_actions=["preflight", "baseline", "deploy", "topology", "inference"]
            ),
            session=session,
        )
        with (
            patch.object(preflight, "_run_git", side_effect=["a" * 40, ""]),
            patch.object(preflight, "_resolve_branch", return_value="feature/test"),
            patch.object(preflight.shutil, "which", return_value=None),
            pytest.raises(RuntimeError, match="Session Manager plugin"),
        ):
            preflight.action_preflight(ctx)

        session.client.assert_not_called()
