"""Single-stack ``gco stacks destroy`` wiring for post-destroy EBS volume cleanup.

These tests pin the command-level contract of the single destroy path: cleanup runs
only after a reconciled successful stack deletion (including a retry against an
already-absent stack), the resolved command request reaches the shared
``StackManager`` helper unchanged, the shared renderer and publication channel
produce the same fields orchestrated destruction uses, and the stack result stays
separate from the cleanup and reporting result.

No AWS client is created here: the stack manager is mocked and only the CLI wiring
is exercised.

Requirements: 3.1, 3.2, 3.3, 5.3, 5.4, 5.5, 6.5, 7.1, 7.2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.commands.stacks_cmd import stacks
from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    TargetVolumeCleanupOutcome,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupRequest,
    VolumeCleanupStatus,
    VolumeOutcome,
    VolumePolicy,
    VolumeReasonCode,
    blocked_target_outcome,
)
from cli.volume_cleanup_reporting import (
    EBS_VOLUME_CLEANUP_NAME,
    render_volume_cleanup_publication,
)

_REGION = "us-east-1"
_STACK = f"gco-{_REGION}"
_TAG_KEY = f"kubernetes.io/cluster/{_STACK}"

_RETAIN_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.RETAIN,
    deletion_authorized=False,
    authorization_source=DeletionAuthorizationSource.NONE,
)
_DELETE_WITH_YES_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.DELETE,
    deletion_authorized=True,
    authorization_source=DeletionAuthorizationSource.EXPLICIT_DELETE_WITH_YES,
)
_INTERACTIVE_DELETE_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.DELETE,
    deletion_authorized=True,
    authorization_source=DeletionAuthorizationSource.INTERACTIVE_VOLUME_CONFIRMATION,
)


@dataclass
class FakeConfig:
    output_format: str = "table"


@dataclass
class FakeFormatter:
    """Recording stand-in for the CLI output formatter."""

    config: FakeConfig = field(default_factory=FakeConfig)
    printed: list[Any] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    success: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def print(self, data: Any, columns: list[str] | None = None) -> None:
        self.printed.append(data)

    def print_info(self, message: str) -> None:
        self.info.append(message)

    def print_success(self, message: str) -> None:
        self.success.append(message)

    def print_warning(self, message: str) -> None:
        self.warnings.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def destroy_cli():
    """Mock the stack manager and formatter used by the single destroy command."""
    manager = MagicMock()
    manager.destroy.return_value = True
    manager.cleanup_regional_volumes_after_destroy.return_value = None
    formatter = FakeFormatter()

    with (
        patch("cli.stacks.get_stack_manager", return_value=manager),
        patch("cli.commands.stacks_cmd.get_output_formatter", return_value=formatter),
    ):
        yield SimpleNamespace(manager=manager, formatter=formatter)


def volume_record(
    volume_id: str,
    *,
    policy: VolumePolicy,
    action: VolumeAction,
    action_result: VolumeActionResult,
    reason_code: VolumeReasonCode,
    tag_value: str | None = "owned",
    state: str = "available",
    attachments: tuple[str, ...] = (),
) -> VolumeOutcome:
    return VolumeOutcome(
        volume_id=volume_id,
        region=_REGION,
        availability_zone=f"{_REGION}a",
        size_gib=50,
        observed_state=state,
        cluster_tag_value=tag_value,
        attachment_ids=attachments,
        policy=policy,
        action=action,
        action_result=action_result,
        reason_code=reason_code,
        reason=f"Recorded reason for {reason_code.value}.",
        follow_up=f"Follow-up action for {volume_id}.",
    )


def target_outcome(
    *,
    request: VolumeCleanupRequest,
    volumes: tuple[VolumeOutcome, ...],
    status: VolumeCleanupStatus = VolumeCleanupStatus.COMPLETED,
) -> TargetVolumeCleanupOutcome:
    return TargetVolumeCleanupOutcome(
        stack_name=_STACK,
        stack_id=None,
        target_region=_REGION,
        target_cluster=_STACK,
        cluster_tag_key=_TAG_KEY,
        policy=request.policy,
        deletion_authorized=request.deletion_authorized,
        authorization_source=request.authorization_source,
        status=status,
        volumes=volumes,
        successful=status
        in {
            VolumeCleanupStatus.COMPLETED,
            VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS,
        },
    )


def retained_outcome() -> TargetVolumeCleanupOutcome:
    return target_outcome(
        request=_RETAIN_REQUEST,
        volumes=(
            volume_record(
                "vol-0000000000000000a",
                policy=VolumePolicy.RETAIN,
                action=VolumeAction.RETAINED,
                action_result=VolumeActionResult.SUCCESS,
                reason_code=VolumeReasonCode.RETAIN_POLICY,
            ),
        ),
    )


def test_retain_cleanup_runs_after_reconciled_stack_success(runner, destroy_cli):
    outcome = retained_outcome()
    destroy_cli.manager.cleanup_regional_volumes_after_destroy.return_value = outcome

    result = runner.invoke(stacks, ["destroy", _STACK, "-y"])

    assert result.exit_code == 0, result.output
    destroy_cli.manager.destroy.assert_called_once_with(stack_name=_STACK, force=True)
    destroy_cli.manager.cleanup_regional_volumes_after_destroy.assert_called_once_with(
        stack_name=_STACK,
        stack_deleted=True,
        request=_RETAIN_REQUEST,
    )
    assert f"Stack {_STACK} destroyed successfully" in destroy_cli.formatter.success
    # Retention completes successfully and still warns about continuing cost.
    assert any("EBS volume cleanup completed" in line for line in destroy_cli.formatter.success)
    assert any("continue to incur storage cost" in line for line in destroy_cli.formatter.warnings)


def test_single_path_publishes_and_renders_the_orchestrated_fields(runner, destroy_cli):
    outcome = retained_outcome()
    destroy_cli.manager.cleanup_regional_volumes_after_destroy.return_value = outcome

    with patch(
        "cli.commands.stacks_cmd.render_volume_cleanup_publication",
        side_effect=render_volume_cleanup_publication,
    ) as renderer:
        result = runner.invoke(stacks, ["destroy", _STACK, "-y"])

    assert result.exit_code == 0, result.output
    renderer.assert_called_once()
    publication = renderer.call_args.args[1]
    assert publication.cleanup_name == EBS_VOLUME_CLEANUP_NAME
    assert publication.details == outcome.to_dict()
    assert publication.reporting_successful is True


def test_stack_failure_performs_no_volume_cleanup(runner, destroy_cli):
    destroy_cli.manager.destroy.return_value = False

    result = runner.invoke(stacks, ["destroy", _STACK, "-y", "--delete-volumes"])

    assert result.exit_code == 1
    destroy_cli.manager.cleanup_regional_volumes_after_destroy.assert_not_called()
    assert "Destroy failed" in destroy_cli.formatter.errors
    assert destroy_cli.formatter.warnings == []


def test_non_regional_stack_keeps_the_existing_exit_path(runner, destroy_cli):
    destroy_cli.manager.cleanup_regional_volumes_after_destroy.return_value = None

    result = runner.invoke(stacks, ["destroy", "gco-global", "-y"])

    assert result.exit_code == 0, result.output
    destroy_cli.manager.cleanup_regional_volumes_after_destroy.assert_called_once_with(
        stack_name="gco-global",
        stack_deleted=True,
        request=_RETAIN_REQUEST,
    )
    assert destroy_cli.formatter.errors == []
    assert destroy_cli.formatter.warnings == []
    assert not any("EBS volume" in line for line in destroy_cli.formatter.info)


def test_authorized_delete_with_remaining_owned_volume_fails_the_command(runner, destroy_cli):
    destroy_cli.manager.cleanup_regional_volumes_after_destroy.return_value = target_outcome(
        request=_DELETE_WITH_YES_REQUEST,
        status=VolumeCleanupStatus.FAILED,
        volumes=(
            volume_record(
                "vol-0000000000000000b",
                policy=VolumePolicy.DELETE,
                action=VolumeAction.SKIPPED,
                action_result=VolumeActionResult.SAFETY_PRESERVED,
                reason_code=VolumeReasonCode.ATTACHMENTS_PRESENT,
                state="in-use",
                attachments=("i-0000000000000000a",),
            ),
        ),
    )

    result = runner.invoke(stacks, ["destroy", _STACK, "-y", "--delete-volumes"])

    assert result.exit_code == 1
    destroy_cli.manager.cleanup_regional_volumes_after_destroy.assert_called_once_with(
        stack_name=_STACK,
        stack_deleted=True,
        request=_DELETE_WITH_YES_REQUEST,
    )
    # The stack result the operator was shown is preserved, not relabeled.
    assert f"Stack {_STACK} destroyed successfully" in destroy_cli.formatter.success
    assert any("owned-volume-remains" in line for line in destroy_cli.formatter.errors)


def test_blocked_cluster_absence_reports_skipped_and_fails_the_command(runner, destroy_cli):
    target = RegionalVolumeTarget(
        stack_name=_STACK,
        stack_id=None,
        region=_REGION,
        cluster_name=_STACK,
        cluster_tag_key=_TAG_KEY,
    )
    destroy_cli.manager.cleanup_regional_volumes_after_destroy.return_value = (
        blocked_target_outcome(
            stack_name=_STACK,
            request=_DELETE_WITH_YES_REQUEST,
            reason_code="cluster-still-present",
            reason=f"Cluster {_STACK} is still present",
            follow_up="Retry destruction once the cluster is absent.",
            target=target,
        )
    )

    result = runner.invoke(stacks, ["destroy", _STACK, "-y", "--delete-volumes"])

    assert result.exit_code == 1
    assert any(
        "EBS volume cleanup was skipped" in line and "cluster-still-present" in line
        for line in destroy_cli.formatter.warnings
    )
    assert any("cleanup-blocked" in line for line in destroy_cli.formatter.errors)


def test_already_absent_retry_uses_the_same_successful_path(runner, destroy_cli):
    destroy_cli.manager.cleanup_regional_volumes_after_destroy.return_value = target_outcome(
        request=_DELETE_WITH_YES_REQUEST,
        volumes=(
            volume_record(
                "vol-0000000000000000c",
                policy=VolumePolicy.DELETE,
                action=VolumeAction.ALREADY_ABSENT,
                action_result=VolumeActionResult.IDEMPOTENT_SUCCESS,
                reason_code=VolumeReasonCode.ALREADY_ABSENT,
            ),
        ),
    )

    result = runner.invoke(stacks, ["destroy", _STACK, "-y", "--delete-volumes"])

    assert result.exit_code == 0, result.output
    destroy_cli.manager.cleanup_regional_volumes_after_destroy.assert_called_once_with(
        stack_name=_STACK,
        stack_deleted=True,
        request=_DELETE_WITH_YES_REQUEST,
    )
    assert any("EBS volume cleanup succeeded" in line for line in destroy_cli.formatter.success)


def test_cleanup_failure_preserves_the_stack_result(runner, destroy_cli):
    destroy_cli.manager.cleanup_regional_volumes_after_destroy.side_effect = RuntimeError(
        "eks lookup exploded"
    )

    result = runner.invoke(stacks, ["destroy", _STACK, "-y"])

    assert result.exit_code == 1
    assert f"Stack {_STACK} destroyed successfully" in destroy_cli.formatter.success
    assert "Destroy failed" not in destroy_cli.formatter.errors
    assert any(
        "EBS volume cleanup could not complete" in line and "eks lookup exploded" in line
        for line in destroy_cli.formatter.errors
    )
    assert any("Re-run the destroy command" in line for line in destroy_cli.formatter.warnings)


def test_interactive_volume_confirmation_reaches_the_cleanup_helper(runner, destroy_cli):
    destroy_cli.manager.cleanup_regional_volumes_after_destroy.return_value = None

    result = runner.invoke(stacks, ["destroy", _STACK, "--delete-volumes"], input="y\ny\n")

    assert result.exit_code == 0, result.output
    destroy_cli.manager.destroy.assert_called_once_with(stack_name=_STACK, force=False)
    destroy_cli.manager.cleanup_regional_volumes_after_destroy.assert_called_once_with(
        stack_name=_STACK,
        stack_deleted=True,
        request=_INTERACTIVE_DELETE_REQUEST,
    )
