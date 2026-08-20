"""Command-equivalent strict destroy requests and durable cleanup evidence.

Covers ``ownership/volume_requests.py`` and its wiring into the strict destroy
action, offline:

* each live case resolves through the same command-aware policy resolver Click
  uses — ``retain-override`` from ``destroy-all -y --retain-volumes``, ``delete``
  from ``destroy-all -y`` with ``delete_volumes=False`` — so the implicit-delete
  path is proved to need neither ``--delete-volumes`` nor a volume confirmation;
* the one resolved request reaches ``destroy_orchestrated`` unchanged, and a
  disabled scenario passes none at all; and
* every ``ebs-volumes`` callback is persisted before teardown is marked
  complete, with a missing, duplicated, foreign, or foreign-policy outcome
  blocking completion and persisting its reason.

Every AWS, git, and orchestration boundary is mocked; nothing here touches live
infrastructure or deletes anything.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    DestroyCommandKind,
    TargetVolumeCleanupOutcome,
    VolumeCleanupDecision,
    VolumeCleanupRequest,
    VolumeCleanupStatus,
    VolumePolicy,
)
from scripts.live_release_validation.actions import destroy as destroy_action
from scripts.live_release_validation.models import (
    RunCheckpoint,
    RunContext,
    RunSettings,
    ValidationReport,
)
from scripts.live_release_validation.ownership import volume_requests
from scripts.live_release_validation.ownership import volumes as ownership_volumes

_ACCOUNT = "123456789012"
_CALLER_ARN = f"arn:aws:iam::{_ACCOUNT}:role/live-validation"
_BRANCH = "chore/volumes"
_PROJECT = "gco-live"
_PRIMARY = "us-east-1"
_SECONDARY = "us-west-2"

_RETAIN_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.RETAIN,
    deletion_authorized=False,
    authorization_source=DeletionAuthorizationSource.NONE,
)
_DELETE_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.DELETE,
    deletion_authorized=True,
    authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
)


def _stack_name(region: str) -> str:
    return f"{_PROJECT}-{region}"


def _stack_id(region: str) -> str:
    return f"arn:aws:cloudformation:{region}:{_ACCOUNT}:stack/{_stack_name(region)}/abc"


def _cluster_tag_key(region: str) -> str:
    return f"kubernetes.io/cluster/{_stack_name(region)}"


def _settings(tmp_path: Path, **overrides: Any) -> RunSettings:
    base = RunSettings(
        run_id="run-123",
        repo_root=tmp_path,
        report_dir=tmp_path / "report",
        checkpoint_path=tmp_path / "report" / "checkpoint.json",
        expected_account=_ACCOUNT,
        expected_sha="a" * 40,
        expected_branch=_BRANCH,
        profile="configured",
        requested_actions=("all",),
        volume_scenario_case="retain-override",
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _context(
    settings: RunSettings,
    *,
    regions: tuple[str, ...] = (_PRIMARY,),
    persist_callback: Callable[[RunCheckpoint], None] | None = None,
    **state: Any,
) -> RunContext:
    checkpoint = RunCheckpoint(identity=settings.identity())
    checkpoint.state.update(
        {
            "account_arn": _CALLER_ARN,
            "owned_stacks": {
                region: {_stack_name(region): {"stack_id": _stack_id(region)}} for region in regions
            },
            **state,
        }
    )
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
        cdk_context={"cluster_observability": {"enabled": False}},
        deployment_regions=regions,
        config=SimpleNamespace(project_name=_PROJECT, global_region=_PRIMARY),
        session=MagicMock(),
        stack_manager=MagicMock(),
        aws_client=MagicMock(),
        job_manager=MagicMock(),
        persist_callback=persist_callback or MagicMock(),
    )


def _resolve(ctx: RunContext) -> tuple[VolumeCleanupRequest | None, dict[str, Any]]:
    with (
        patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH),
        patch.object(ownership_volumes, "_authorize_owned_stack"),
    ):
        return volume_requests.resolve_strict_volume_cleanup_request(ctx)


def _published_outcome(
    region: str,
    *,
    request: VolumeCleanupRequest,
    status: VolumeCleanupStatus = VolumeCleanupStatus.COMPLETED,
) -> dict[str, Any]:
    """One real serialized target outcome, exactly as StackManager publishes it."""
    outcome = TargetVolumeCleanupOutcome(
        stack_name=_stack_name(region),
        stack_id=_stack_id(region),
        target_region=region,
        target_cluster=_stack_name(region),
        cluster_tag_key=_cluster_tag_key(region),
        policy=request.policy,
        deletion_authorized=request.deletion_authorized,
        authorization_source=request.authorization_source,
        status=status,
        successful=status is VolumeCleanupStatus.COMPLETED,
    )
    return dict(outcome.to_dict())


class TestCommandEquivalentInputs:
    """Each case stands for one exact operator command, and nothing else."""

    def test_retain_override_is_destroy_all_yes_with_retain_volumes(self) -> None:
        inputs = volume_requests.strict_volume_command_inputs("retain-override")

        assert inputs.command is DestroyCommandKind.ALL
        assert (inputs.yes, inputs.retain_volumes, inputs.delete_volumes) == (True, True, False)
        assert inputs.command_line == "gco stacks destroy-all -y --retain-volumes"

    def test_delete_is_destroy_all_yes_with_no_volume_flag(self) -> None:
        inputs = volume_requests.strict_volume_command_inputs("delete")

        assert inputs.command is DestroyCommandKind.ALL
        assert (inputs.yes, inputs.retain_volumes, inputs.delete_volumes) == (True, False, False)
        assert inputs.command_line == "gco stacks destroy-all -y"
        assert "--delete-volumes" not in inputs.command_line

    def test_a_disabled_case_exercises_no_destroy_command(self) -> None:
        with pytest.raises(ValueError, match="exercises no destroy command"):
            volume_requests.strict_volume_command_inputs("disabled")

    def test_the_driver_instruction_is_not_a_case(self) -> None:
        with pytest.raises(ValueError, match="scenario-driver instruction"):
            volume_requests.strict_volume_command_inputs("both")


class TestResolvedRequests:
    """The harness resolves policy through the CLI's resolver, not its own rules."""

    def test_retain_override_resolves_to_an_unauthorized_retain_request(
        self, tmp_path: Path
    ) -> None:
        ctx = _context(_settings(tmp_path))

        request, evidence = _resolve(ctx)

        assert request == _RETAIN_REQUEST
        assert evidence["status"] == "resolved"
        assert evidence["command_line"] == "gco stacks destroy-all -y --retain-volumes"
        assert evidence["policy"] == "retain"
        assert evidence["deletion_authorized"] is False
        assert evidence["authorization_source"] == "none"

    def test_delete_resolves_to_implicit_authorization_without_flag_or_prompt(
        self, tmp_path: Path
    ) -> None:
        ctx = _context(_settings(tmp_path, volume_scenario_case="delete"))

        request, evidence = _resolve(ctx)

        assert request == _DELETE_REQUEST
        assert evidence["command_line"] == "gco stacks destroy-all -y"
        assert evidence["inputs"]["delete_volumes"] is False
        assert evidence["delete_flag_supplied"] is False
        assert evidence["volume_confirmation_required"] is False
        assert evidence["authorization_source"] == "destroy-all-with-yes"

    def test_the_request_is_persisted_as_scenario_evidence(self, tmp_path: Path) -> None:
        ctx = _context(_settings(tmp_path, volume_scenario_case="delete"))

        _, evidence = _resolve(ctx)

        state = ctx.checkpoint.state["volume_scenario"]
        assert state[volume_requests.STRICT_DESTROY_REQUEST_KEY] == evidence
        assert "strict-volume-request:delete" in state["authorizations"]

    def test_a_disabled_scenario_resolves_no_request(self, tmp_path: Path) -> None:
        ctx = _context(_settings(tmp_path, volume_scenario_case="disabled"))

        request, evidence = _resolve(ctx)

        assert request is None
        assert evidence["status"] == "skipped"
        assert "volume_scenario" not in ctx.checkpoint.state

    def test_a_changed_identity_refuses_to_resolve(self, tmp_path: Path) -> None:
        ctx = _context(_settings(tmp_path))
        ctx.checkpoint.identity = dict(ctx.checkpoint.identity) | {"run_id": "someone-else"}

        with pytest.raises(RuntimeError, match="Checkpoint identity does not match"):
            _resolve(ctx)

    def test_a_command_that_stops_meaning_its_policy_fails_closed(self, tmp_path: Path) -> None:
        ctx = _context(_settings(tmp_path, volume_scenario_case="delete"))

        with (
            patch.object(
                volume_requests,
                "resolve_volume_cleanup_request",
                return_value=VolumeCleanupDecision(
                    policy=VolumePolicy.RETAIN,
                    deletion_authorized=False,
                    authorization_source=DeletionAuthorizationSource.NONE,
                    requires_volume_confirmation=False,
                ),
            ),
            pytest.raises(RuntimeError, match="but case 'delete' exercises policy"),
        ):
            _resolve(ctx)

    def test_a_resolution_needing_a_volume_prompt_fails_closed(self, tmp_path: Path) -> None:
        ctx = _context(_settings(tmp_path, volume_scenario_case="delete"))

        with (
            patch.object(
                volume_requests,
                "resolve_volume_cleanup_request",
                return_value=VolumeCleanupDecision(
                    policy=VolumePolicy.DELETE,
                    deletion_authorized=False,
                    authorization_source=DeletionAuthorizationSource.NONE,
                    requires_volume_confirmation=True,
                ),
            ),
            pytest.raises(RuntimeError, match="pending interactive volume-deletion confirmation"),
        ):
            _resolve(ctx)


class TestDurableCleanupEvidence:
    """Teardown completion is gated on persisted per-target callbacks."""

    @staticmethod
    def _context_with_callbacks(
        tmp_path: Path,
        outcomes: list[dict[str, Any]],
        *,
        case: str = "delete",
        sequence: int = 1,
    ) -> RunContext:
        ctx = _context(
            _settings(tmp_path, volume_scenario_case=case),
            volume_scenario={"case": case},
            destroy_helper_outcomes=[
                {
                    "destroy_sequence": sequence,
                    "name": volume_requests.VOLUME_CLEANUP_CALLBACK_NAME,
                    "at": "2026-07-17T00:00:01+00:00",
                    "details": details,
                }
                for details in outcomes
            ],
        )
        return ctx

    def _verify(
        self,
        ctx: RunContext,
        *,
        request: VolumeCleanupRequest | None = _DELETE_REQUEST,
        expected: tuple[str, ...] = (_stack_name(_PRIMARY),),
        sequence: int = 1,
    ) -> dict[str, Any]:
        with patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH):
            return volume_requests.verify_volume_cleanup_evidence(
                ctx,
                request=request,
                expected_stack_names=expected,
                destroy_sequence=sequence,
            )

    def test_one_outcome_per_target_records_durable_evidence(self, tmp_path: Path) -> None:
        ctx = self._context_with_callbacks(
            tmp_path,
            [
                _published_outcome(region, request=_DELETE_REQUEST)
                for region in (_PRIMARY, _SECONDARY)
            ],
        )

        evidence = self._verify(
            ctx,
            expected=(_stack_name(_PRIMARY), _stack_name(_SECONDARY)),
        )

        assert evidence["status"] == "recorded"
        assert evidence["problems"] == []
        assert sorted(evidence["targets"]) == [_stack_name(_PRIMARY), _stack_name(_SECONDARY)]
        assert evidence["targets"][_stack_name(_PRIMARY)]["status"] == "completed"
        assert evidence["targets"][_stack_name(_PRIMARY)]["counts"]["discovered"] == 0
        assert evidence["policy"] == "delete"
        state = ctx.checkpoint.state["volume_scenario"]
        assert state[volume_requests.STRICT_DESTROY_CLEANUP_EVIDENCE_KEY] == evidence

    def test_a_missing_target_outcome_blocks_completion(self, tmp_path: Path) -> None:
        ctx = self._context_with_callbacks(
            tmp_path,
            [_published_outcome(_PRIMARY, request=_DELETE_REQUEST)],
        )

        with pytest.raises(RuntimeError, match="no persisted ebs-volumes outcome for"):
            self._verify(
                ctx,
                expected=(_stack_name(_PRIMARY), _stack_name(_SECONDARY)),
            )

        evidence = ctx.checkpoint.state["volume_scenario"][
            volume_requests.STRICT_DESTROY_CLEANUP_EVIDENCE_KEY
        ]
        assert evidence["status"] == "blocked"
        assert _stack_name(_SECONDARY) in evidence["problems"][0]

    def test_a_callback_from_another_attempt_does_not_count(self, tmp_path: Path) -> None:
        ctx = self._context_with_callbacks(
            tmp_path,
            [_published_outcome(_PRIMARY, request=_DELETE_REQUEST)],
            sequence=1,
        )

        with pytest.raises(RuntimeError, match="no persisted ebs-volumes outcome for"):
            self._verify(ctx, sequence=2)

    def test_a_duplicated_outcome_blocks_completion(self, tmp_path: Path) -> None:
        ctx = self._context_with_callbacks(
            tmp_path,
            [_published_outcome(_PRIMARY, request=_DELETE_REQUEST)] * 2,
        )

        with pytest.raises(RuntimeError, match="more than one outcome published for"):
            self._verify(ctx)

    def test_an_outcome_for_an_uncaptured_target_blocks_completion(self, tmp_path: Path) -> None:
        ctx = self._context_with_callbacks(
            tmp_path,
            [
                _published_outcome(region, request=_DELETE_REQUEST)
                for region in (_PRIMARY, _SECONDARY)
            ],
        )

        with pytest.raises(RuntimeError, match="targets this run did not capture"):
            self._verify(ctx)

    def test_an_outcome_without_target_identity_blocks_completion(self, tmp_path: Path) -> None:
        ctx = self._context_with_callbacks(tmp_path, [{"status": "completed"}])

        with pytest.raises(RuntimeError, match="carried no target identity"):
            self._verify(ctx)

    def test_an_outcome_published_under_another_policy_blocks_completion(
        self, tmp_path: Path
    ) -> None:
        ctx = self._context_with_callbacks(
            tmp_path,
            [_published_outcome(_PRIMARY, request=_RETAIN_REQUEST)],
        )

        with pytest.raises(RuntimeError, match="published policy 'retain'"):
            self._verify(ctx)

    def test_a_disabled_scenario_requires_no_callback(self, tmp_path: Path) -> None:
        ctx = _context(_settings(tmp_path, volume_scenario_case="disabled"))

        evidence = self._verify(ctx, request=None, expected=())

        assert evidence["status"] == "skipped"
        assert "volume_scenario" not in ctx.checkpoint.state


class TestTheDestroyActionPassesOneRequestAndProvesEvidence:
    """The action hands orchestration the resolved request and gates completion."""

    @staticmethod
    def _destroy_context(
        tmp_path: Path,
        *,
        case: str = "delete",
        persist_callback: Callable[[RunCheckpoint], None] | None = None,
    ) -> RunContext:
        ctx = _context(
            _settings(tmp_path, volume_scenario_case=case),
            persist_callback=persist_callback,
        )
        ctx.checkpoint.deployment_attempted = True
        ctx.checkpoint.state["target_stack_regions"] = {_stack_name(_PRIMARY): _PRIMARY}
        ctx.checkpoint.state["bootstrap_stacks"] = {}
        return ctx

    @staticmethod
    def _run(
        ctx: RunContext,
        *,
        published: tuple[str, ...] = (_PRIMARY,),
        targets: tuple[str, ...] = (_PRIMARY,),
        request: VolumeCleanupRequest = _DELETE_REQUEST,
    ) -> Any:
        def destroy_orchestrated(**kwargs: Any) -> tuple[bool, list[str], list[str]]:
            callback = kwargs["on_cleanup_complete"]
            for region in published:
                callback(
                    volume_requests.VOLUME_CLEANUP_CALLBACK_NAME,
                    _published_outcome(region, request=request),
                )
            return True, [_stack_name(_PRIMARY)], []

        ctx.stack_manager.destroy_orchestrated.side_effect = destroy_orchestrated
        present = {
            "all_absent": False,
            "residual": [{"stack_name": _stack_name(_PRIMARY)}],
            "absent": [],
        }
        absent = {"all_absent": True, "residual": [], "absent": []}
        with (
            patch.object(destroy_action, "cleanup_workloads", return_value={"complete": True}),
            patch.object(destroy_action, "_reconcile_stack_ownership"),
            patch.object(destroy_action, "_checkpoint_new_ecr_repositories"),
            patch.object(destroy_action, "_checkpoint_new_ecr_images"),
            patch.object(destroy_action, "_checkpoint_retained_kms_keys"),
            patch.object(destroy_action, "_ensure_log_cleanup_helper", return_value={}),
            patch.object(destroy_action, "_delete_log_cleanup_helper", return_value={}),
            patch.object(destroy_action, "_prepared_change_set_authority", return_value={}),
            patch.object(destroy_action, "_retained_resource_cleanup", return_value={}),
            patch.object(
                destroy_action,
                "_verify_target_stack_absence",
                side_effect=[present, absent, absent, present, absent, absent],
            ),
            patch.object(
                destroy_action,
                "capture_strict_volume_targets",
                return_value={"status": "captured", "case": ctx.settings.volume_scenario_case},
            ),
            patch.object(
                destroy_action,
                "strict_volume_cleanup_targets",
                return_value={_stack_name(region): MagicMock() for region in targets},
            ),
            # The independent post-destroy observation and the retained-fixture
            # cleanup are covered by their own module; this one is scoped to the
            # resolved request and the durable callback barrier.
            patch.object(
                destroy_action,
                "verify_post_destroy_volume_outcomes",
                return_value={"status": "verified", "targets": {}},
            ),
            patch.object(
                destroy_action,
                "cleanup_validation_fixture_volumes",
                return_value={"status": "skipped"},
            ),
            patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH),
            patch.object(ownership_volumes, "_authorize_owned_stack"),
            patch.object(destroy_action.time, "sleep"),
        ):
            return destroy_action.destroy_deployment(ctx)

    def test_the_delete_case_passes_the_implicit_authorized_request(self, tmp_path: Path) -> None:
        ctx = self._destroy_context(tmp_path)

        result = self._run(ctx)

        kwargs = ctx.stack_manager.destroy_orchestrated.call_args.kwargs
        assert kwargs["volume_cleanup_request"] == _DELETE_REQUEST
        assert result["volume_cleanup_request"]["command_line"] == "gco stacks destroy-all -y"
        assert result["volume_cleanup_evidence"]["status"] == "recorded"

    def test_the_retain_case_passes_the_retain_request(self, tmp_path: Path) -> None:
        ctx = self._destroy_context(tmp_path, case="retain-override")

        result = self._run(ctx, request=_RETAIN_REQUEST)

        kwargs = ctx.stack_manager.destroy_orchestrated.call_args.kwargs
        assert kwargs["volume_cleanup_request"] == _RETAIN_REQUEST
        assert (
            result["volume_cleanup_request"]["command_line"]
            == "gco stacks destroy-all -y --retain-volumes"
        )

    def test_a_disabled_scenario_passes_no_request(self, tmp_path: Path) -> None:
        ctx = self._destroy_context(tmp_path, case="disabled")

        result = self._run(ctx, published=(), targets=())

        kwargs = ctx.stack_manager.destroy_orchestrated.call_args.kwargs
        assert kwargs["volume_cleanup_request"] is None
        assert result["volume_cleanup_evidence"]["status"] == "skipped"

    def test_every_callback_is_persisted_before_teardown_completes(self, tmp_path: Path) -> None:
        snapshots: list[dict[str, Any]] = []

        def persist(checkpoint: RunCheckpoint) -> None:
            outcomes = checkpoint.state.get("destroy_helper_outcomes", [])
            snapshots.append(
                {
                    "destroyed": checkpoint.destroyed,
                    "targets": sorted(
                        str(entry["details"]["stack_name"])
                        for entry in copy.deepcopy(outcomes)
                        if entry["name"] == volume_requests.VOLUME_CLEANUP_CALLBACK_NAME
                    ),
                }
            )

        ctx = self._destroy_context(tmp_path, persist_callback=persist)

        self._run(ctx, published=(_PRIMARY, _SECONDARY), targets=(_PRIMARY, _SECONDARY))

        completed = [index for index, snap in enumerate(snapshots) if snap["destroyed"]]
        assert completed, "teardown never persisted a completed checkpoint"
        expected = sorted(_stack_name(region) for region in (_PRIMARY, _SECONDARY))
        # Both callbacks were durable in a checkpoint written before completion.
        assert snapshots[completed[0] - 1]["targets"] == expected
        assert ctx.checkpoint.destroyed is True

    def test_a_missing_callback_stops_teardown_completion(self, tmp_path: Path) -> None:
        ctx = self._destroy_context(tmp_path)

        with pytest.raises(RuntimeError, match="did not succeed after"):
            self._run(ctx, published=(_PRIMARY,), targets=(_PRIMARY, _SECONDARY))

        assert ctx.checkpoint.destroyed is False
        evidence = ctx.checkpoint.state["volume_scenario"][
            volume_requests.STRICT_DESTROY_CLEANUP_EVIDENCE_KEY
        ]
        assert evidence["status"] == "blocked"
        assert any(_stack_name(_SECONDARY) in problem for problem in evidence["problems"])
