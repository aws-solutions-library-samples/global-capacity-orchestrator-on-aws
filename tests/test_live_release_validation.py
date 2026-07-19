"""Offline safety contracts for the live release-validation harness.

Every AWS/API boundary is mocked. These tests are intended for CI and must
never create, mutate, or delete live infrastructure.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from scripts.live_release_validation import actions, inventory


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
        session=MagicMock(),
        aws_client=MagicMock(),
        stack_manager=MagicMock(),
        report=SimpleNamespace(final_inventory={}),
    )
    context.persist = MagicMock()
    context.persist_callback = MagicMock()
    context.prepare_job_submission = MagicMock()
    context.begin_job_submission = MagicMock()
    context.finish_job_submission = MagicMock()
    context.mark_central_job_cancelled_before_claim = MagicMock()
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
    return {
        "job_id": job_id,
        "job_name": "gco-live-ddb-run-123",
        "namespace": "gco-jobs",
        "target_region": "us-east-1",
        "status": status,
    }


class TestCentralQueueResume:
    def test_job_id_matches_deployed_queue_protocol(self) -> None:
        from gco.services.api_routes.queue import _IDEMPOTENCY_NAMESPACE

        key = "gco-live-validation:run-123:central"
        assert actions._central_queue_job_id(key) == str(uuid.uuid5(_IDEMPOTENCY_NAMESPACE, key))

    def test_submitting_checkpoint_reconciles_without_second_post(self) -> None:
        ctx = _context()
        key = "gco-live-validation:run-123:central"
        job_id = actions._central_queue_job_id(key)
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
            "deleted": True,
            "validation_evidence": {
                "name": queue_job["job_name"],
                "namespace": queue_job["namespace"],
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
            patch.object(
                actions,
                "_central_manifest",
                return_value=({}, queue_job["job_name"], queue_job["namespace"], marker),
            ),
            patch.object(actions, "_register_job", return_value=job_record),
            patch.object(actions, "_register_central_job", return_value=central_record),
            patch.object(
                actions,
                "_get_central_queue_job",
                return_value=queue_job,
            ) as get_queue_job,
            patch.object(
                actions,
                "_wait_for_central_queue_appearance",
                return_value=queue_job,
            ) as wait_for_appearance,
            patch.object(
                actions,
                "_wait_for_central_queue_terminal",
                return_value=(queue_job, [{"status": "succeeded", "at": 1.0}]),
            ),
            patch.object(actions, "_read_central_job_item", return_value={"status": "succeeded"}),
        ):
            result = actions.action_central_queue_lifecycle(ctx)

        assert result["job_id"] == job_id
        get_queue_job.assert_called_once_with(ctx, central_record)
        wait_for_appearance.assert_called_once_with(ctx, central_record)
        ctx.aws_client.make_authenticated_request.assert_not_called()
        ctx.finish_job_submission.assert_called_once()

    def test_submitting_checkpoint_replays_only_exact_persisted_envelope(self) -> None:
        ctx = _context()
        key = "gco-live-validation:run-123:central"
        job_id = actions._central_queue_job_id(key)
        marker = "GCO_LIVE_DDB_run-123"
        manifest = {"apiVersion": "batch/v1", "kind": "Job"}
        queue_job = _central_job(job_id)
        body = {
            "manifest": manifest,
            "target_region": "us-east-1",
            "namespace": queue_job["namespace"],
            "priority": 100,
            "labels": {
                actions._RUN_JOB_LABEL: actions._run_token(ctx.settings.run_id),
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
            "deleted": True,
            "validation_evidence": {
                "name": queue_job["job_name"],
                "namespace": queue_job["namespace"],
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
            patch.object(
                actions,
                "_central_manifest",
                return_value=(
                    manifest,
                    queue_job["job_name"],
                    queue_job["namespace"],
                    marker,
                ),
            ),
            patch.object(actions, "_register_job", return_value=job_record),
            patch.object(actions, "_register_central_job", return_value=central_record),
            patch.object(actions, "_get_central_queue_job", return_value=None),
            patch.object(
                actions,
                "_wait_for_central_queue_appearance",
                return_value=queue_job,
            ) as wait_for_appearance,
            patch.object(
                actions,
                "_wait_for_central_queue_terminal",
                return_value=(queue_job, [{"status": "succeeded", "at": 1.0}]),
            ),
            patch.object(actions, "_read_central_job_item", return_value={"status": "succeeded"}),
        ):
            result = actions.action_central_queue_lifecycle(ctx)

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
        job_id = actions._central_queue_job_id("gco-live-validation:run-123:central")
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
            patch.object(
                actions,
                "_get_central_queue_job",
                side_effect=[None, queued, cancelled],
            ) as get_job,
            patch.object(actions, "_read_central_job_item", return_value=cancelled),
            patch.object(actions.time, "sleep"),
        ):
            result = actions._cleanup_central_job(ctx, central_record)

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
        job_id = actions._central_queue_job_id(key)
        record = ctx.register_job(
            name="gco-live-ddb-run-123",
            namespace="gco-jobs",
            region="us-east-1",
            path="dynamodb",
            run_label=actions._run_token(ctx.settings.run_id),
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
        central_record = actions._register_central_job(
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
            patch.object(actions, "_wait_for_central_queue_appearance", return_value=queued),
            patch.object(
                actions,
                "_wait_for_central_queue_terminal",
                return_value=(cancelled, [{"status": "cancelled", "at": 1.0}]),
            ),
            patch.object(actions, "_read_central_job_item", return_value=cancelled),
        ):
            result = actions._cleanup_central_job(ctx, central_record)

        assert result["workload_not_submitted"] is True
        assert record["submission_state"] == "not_submitted"
        assert record["central_cancelled_before_claim_job_id"] == job_id

        with patch.object(actions, "_get_owned_job", return_value=None):
            deletion = actions._delete_owned_job(ctx, record)
        assert deletion == {"not_submitted": True, "already_absent": True}
        assert record["deleted"] is True

    def test_cleanup_non_observation_remains_an_unresolved_barrier(self) -> None:
        ctx = _context()
        job_id = actions._central_queue_job_id("gco-live-validation:run-123:central")
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
            patch.object(actions, "_wait_for_central_queue_appearance", return_value=None),
            pytest.raises(RuntimeError, match="non-observation is not terminal proof"),
        ):
            actions._cleanup_central_job(ctx, central_record)

        assert central_record["cleanup_complete"] is False
        assert central_record["cleanup_result"]["complete"] is False
        ctx.aws_client.make_authenticated_request.assert_not_called()


class TestStrictStackOwnership:
    _STACK_ID = "arn:aws:cloudformation:us-east-1:123456789012:stack/gco-live-global/stack-uuid"

    def _stack(self) -> dict[str, object]:
        return {
            "name": "gco-live-global",
            "stack_id": self._STACK_ID,
            "status": "CREATE_COMPLETE",
            "tags": {actions._RUN_STACK_TAG: "run-123"},
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
            patch.object(actions, "_reconcile_stack_ownership"),
            patch.object(actions, "_checkpoint_new_ecr_repositories"),
            patch.object(actions, "_checkpoint_new_ecr_images"),
            patch.object(actions, "_checkpoint_retained_kms_keys"),
        ):
            result = actions.action_deploy(ctx)

        assert result["overall_success"] is True
        call_kwargs = ctx.stack_manager.deploy_orchestrated.call_args.kwargs
        assert "outputs_file" not in call_kwargs

    def test_live_observation_cannot_create_destructive_authority(self) -> None:
        ctx = _context(state={"owned_stacks": {}})

        with pytest.raises(RuntimeError, match="without prepared-change-set authority"):
            actions._record_stack_identity(
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
        actions._record_prepared_stack_identity(
            ctx,
            "gco-live-global",
            "us-east-1",
            self._STACK_ID,
            first_change_set_id,
            "CREATE",
        )

        record = actions._record_stack_identity(
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
        actions._record_prepared_stack_identity(
            ctx,
            "gco-live-global",
            "us-east-1",
            self._STACK_ID,
            second_change_set_id,
            "UPDATE",
        )
        authority = actions._prepared_change_set_authority(ctx)
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
            actions._record_prepared_stack_identity(
                ctx,
                "gco-live-global",
                "us-east-1",
                self._STACK_ID,
                change_set_id,
                "UPDATE",
            )

        reloaded = _context(state=json.loads(json.dumps(ctx.checkpoint.state)))
        authority = actions._prepared_change_set_authority(reloaded)["gco-live-global"]
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

        assert actions._ecr_image_identity(result or {}) == self._IDENTITY
        ecr.describe_images.assert_called_once_with(
            repositoryName="baseline/repository",
            imageIds=[{"imageTag": "run-tag"}],
        )
        ecr.batch_get_image.assert_called_once()

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

        with patch.object(
            actions,
            "describe_ecr_image_by_tag",
            return_value=current,
        ) as describe:
            result = actions._cleanup_new_ecr_images(ctx)

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
            "creation_identity": actions._ecr_creation_identity(repository),
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
            patch.object(
                actions,
                "collect_ecr_inventory",
                return_value={"us-east-1": [repository]},
            ),
            pytest.raises(RuntimeError, match="run ownership changed"),
        ):
            actions._cleanup_new_ecr_repositories(ctx)

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
            "tags": {actions._RUN_STACK_TAG: "run-123"},
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
                    "creation_identity": actions._ecr_creation_identity(created_repository),
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

        comparison, accepted = actions._strip_expected_retained_ecr(ctx, final_baseline)

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
        residual = actions._strip_baseline_ecr(project_inventory, ctx.checkpoint.baseline)
        residual = actions._strip_accepted_retained_ecr(residual, accepted)
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

        filtered = actions._strip_baseline_ecr(project_inventory, self._baseline())

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

    def test_filter_requires_complete_protected_resource_authority(self) -> None:
        baseline = self._baseline()
        del baseline["protected_stacks"][self._REGION][0]["physical_resources"]

        with pytest.raises(RuntimeError, match="omitted physical resources"):
            actions._strip_baseline_ecr({}, baseline)

    @pytest.mark.parametrize("protected_stacks", [[], "", 0, False])
    def test_filter_rejects_falsey_non_object_stack_authority(
        self, protected_stacks: object
    ) -> None:
        with pytest.raises(RuntimeError, match="protected_stacks must be an object"):
            actions._strip_baseline_ecr({}, {"protected_stacks": protected_stacks})

    @pytest.mark.parametrize("baseline", [{}, {"protected_stacks": None}])
    def test_missing_or_null_protected_stack_authority_is_empty(
        self, baseline: dict[str, object]
    ) -> None:
        assert actions._baseline_protected_identities(baseline) == ({}, {})

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

        filtered = actions._strip_baseline_ecr(project_inventory, baseline)

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

        filtered = actions._strip_baseline_ecr(project_inventory, baseline)

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

        assert inventory._list_eks_clusters(session, self._REGION, None) == [
            "gco-live",
            "gco-live-west",
            "unrelated",
        ]
        assert inventory._list_eks_clusters(session, self._REGION, "gco-live") == [
            "gco-live",
            "gco-live-west",
        ]

    def test_filter_suppresses_only_authoritatively_orphaned_eks_pods(self) -> None:
        existing_pod = (
            "arn:aws:eks:us-east-1:123456789012:podidentityassociation/gco-live-existing/a-existing"
        )
        orphaned_pod = (
            "arn:aws:eks:us-east-1:123456789012:podidentityassociation/gco-live-orphan/a-orphan"
        )
        malformed_pod = "arn:aws:eks:us-east-1:123456789012:podidentityassociation/gco-live-orphan"
        non_pod_eks = "arn:aws:eks:us-east-1:123456789012:cluster/gco-live-orphan"
        wrong_region_pod = (
            "arn:aws:eks:us-west-2:123456789012:"
            "podidentityassociation/gco-live-orphan/a-wrong-region"
        )
        unscanned_region_pod = (
            "arn:aws:eks:us-west-2:123456789012:podidentityassociation/gco-live-orphan/a-unscanned"
        )
        project_inventory = {
            "cloudformation_stacks": {},
            "authoritative_eks_clusters": {
                self._REGION: ["gco-live-existing"],
            },
            "regional": {
                self._REGION: {
                    "tagged_resources": [
                        {"arn": orphaned_pod, "tags": {}},
                        {"arn": existing_pod, "tags": {}},
                        {"arn": malformed_pod, "tags": {}},
                        {"arn": non_pod_eks, "tags": {}},
                        {"arn": wrong_region_pod, "tags": {}},
                    ]
                },
                "us-west-2": {"tagged_resources": [{"arn": unscanned_region_pod, "tags": {}}]},
            },
        }
        baseline = {"protected_stacks": {}, "ecr_repositories": {}}

        filtered = actions._strip_baseline_ecr(project_inventory, baseline)

        east_arns = {
            record["arn"] for record in filtered["regional"][self._REGION]["tagged_resources"]
        }
        assert orphaned_pod not in east_arns
        assert east_arns == {
            existing_pod,
            malformed_pod,
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

    def test_destroy_passes_exact_ids_and_disables_parallel_bootstrap(self) -> None:
        ctx = self._destroy_context()
        initial = {"all_absent": False, "absent": [], "residual": [{"name": "x"}]}
        absent = {"all_absent": True, "absent": [{"name": "x"}], "residual": []}

        with (
            patch.object(
                actions, "_verify_target_stack_absence", side_effect=[initial, absent, absent]
            ),
            patch.object(
                actions,
                "cleanup_workloads",
                return_value={"complete": True, "errors": [], "unresolved": []},
            ),
            patch.object(actions, "_reconcile_stack_ownership"),
            patch.object(actions, "_checkpoint_new_ecr_repositories"),
            patch.object(actions, "_checkpoint_new_ecr_images"),
            patch.object(actions, "_checkpoint_retained_kms_keys"),
            patch.object(actions, "_retained_resource_cleanup", return_value={"errors": []}),
        ):
            result = actions.destroy_deployment(ctx)

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
            patch.object(actions, "_verify_target_stack_absence", return_value=initial),
            patch.object(actions, "cleanup_workloads", return_value=unresolved),
            pytest.raises(RuntimeError, match="unresolved teardown barrier"),
        ):
            actions.destroy_deployment(ctx)

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
            patch.object(
                actions, "_verify_target_stack_absence", side_effect=[residual, absent, absent]
            ),
            patch.object(
                actions,
                "cleanup_workloads",
                return_value={"complete": True, "errors": [], "unresolved": []},
            ),
            patch.object(actions, "_reconcile_stack_ownership"),
            patch.object(actions, "_checkpoint_new_ecr_repositories"),
            patch.object(actions, "_checkpoint_new_ecr_images"),
            patch.object(actions, "_checkpoint_retained_kms_keys"),
            patch.object(actions, "_retained_resource_cleanup", return_value={"errors": []}),
        ):
            actions.destroy_deployment(ctx)

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

        with (
            patch.object(actions, "_verify_target_stack_absence", return_value=residual),
            patch.object(
                actions,
                "capture_baseline",
                return_value=ctx.checkpoint.baseline,
            ),
            patch.object(actions, "compare_baseline", return_value=[]),
            patch.object(
                actions,
                "collect_project_resources",
                return_value=project_inventory,
            ),
            patch.object(
                actions,
                "_strip_baseline_ecr",
                return_value=project_inventory,
            ),
            patch.object(
                actions,
                "_strip_expected_pending_kms",
                return_value=(project_inventory, []),
            ),
            patch.object(actions, "summarize_project_resources", return_value={}),
            patch.object(actions, "project_resources_are_absent", return_value=True),
            pytest.raises(RuntimeError, match="Target stacks remain after teardown"),
        ):
            actions.action_final_inventory(ctx)

        assert ctx.report.final_inventory["stack_absence"] == residual
        assert ctx.checkpoint.state["final_inventory"]["stack_absence"] == residual
        assert ctx.checkpoint.destroyed is False
        assert "destroy" not in ctx.checkpoint.completed_actions
        assert "final-inventory" not in ctx.checkpoint.completed_actions
        reconciliation = ctx.checkpoint.state["stale_destroyed_reconciliations"][-1]
        assert reconciliation["source"] == "final-inventory"
        assert reconciliation["stack_absence"] == residual
        ctx.persist.assert_called_once()


class TestCheckpointPersistence:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission model only")
    def test_repeated_atomic_checkpoint_replacements_stay_owner_only(
        self,
        tmp_path: Path,
    ) -> None:
        from scripts.live_release_validation import models

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

        with patch.object(models.os, "replace", side_effect=tracked_replace):
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
        from scripts.live_release_validation import models

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
            patch.object(models.os, "replace", side_effect=rebind_then_replace),
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
            patch.object(runner.boto3, "Session") as session,
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

    def test_detached_head_does_not_consume_github_branch_variables(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        monkeypatch.setenv("GITHUB_HEAD_REF", "github-head")
        monkeypatch.setenv("GITHUB_REF_NAME", "github-ref")

        with (
            patch.object(actions, "_run_git", return_value=""),
            pytest.raises(RuntimeError, match="requires a checked-out branch"),
        ):
            actions._resolve_branch(tmp_path)


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
    _BUSYBOX_IMAGE = (
        "docker.io/library/busybox:1.38.0@"
        "sha256:fd8d9aa63ba2f0982b5304e1ee8d3b90a210bc1ffb5314d980eb6962f1a9715d"
    )

    def test_smoke_images_are_immutable_and_dependency_scanned(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifests = sorted((root / "scripts/live_release_validation/manifests").glob("*.yaml"))
        assert manifests

        for manifest in manifests:
            content = manifest.read_text(encoding="utf-8")
            assert f"image: {self._BUSYBOX_IMAGE}" in content
            assert "imagePullPolicy: IfNotPresent" in content

        dependency_scan = (root / ".github/scripts/dependency-scan.sh").read_text(encoding="utf-8")
        assert "scripts/live_release_validation/manifests/" in dependency_scan


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

    def test_runbook_requires_local_execution_and_manual_pr_upload(self) -> None:
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
            "manually upload `live-release-validation.md`",
            "pull request",
        ):
            assert required in runbook

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
