"""Focused offline tests for command-level EBS cleanup status and exit mapping."""

from dataclasses import dataclass, field
from typing import Any

import pytest

from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    SafeError,
    TargetVolumeCleanupOutcome,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupStatus,
    VolumeOutcome,
    VolumePolicy,
    VolumeReasonCode,
    VolumeSnapshot,
)
from cli.volume_cleanup_reporting import (
    EXIT_FAILURE,
    EXIT_SUCCESS,
    VolumeCleanupCommandResult,
    VolumeCleanupExitReason,
    VolumeCleanupPublication,
    VolumeCleanupTargetStatus,
    destroy_command_exit_code,
    evaluate_target_cleanup_status,
    evaluate_volume_cleanup_result,
    publish_volume_cleanup_outcome,
    render_volume_cleanup_command_result,
)


@dataclass
class FakeFormatter:
    """Minimal recording stand-in for the CLI output formatter."""

    success: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def print(self, data: Any, columns: list[str] | None = None) -> None:
        pass

    def print_info(self, message: str) -> None:
        pass

    def print_success(self, message: str) -> None:
        self.success.append(message)

    def print_warning(self, message: str) -> None:
        pass

    def print_error(self, message: str) -> None:
        self.errors.append(message)


def record(
    volume_id: str,
    *,
    policy: VolumePolicy,
    action: VolumeAction,
    action_result: VolumeActionResult,
    reason_code: VolumeReasonCode | None,
    tag_value: str | None = "owned",
    state: str = "available",
    attachments: tuple[str, ...] = (),
    error: SafeError | None = None,
) -> VolumeOutcome:
    reason = f"Recorded reason for {reason_code}." if reason_code else None
    follow_up = f"Follow-up action for {volume_id}." if reason_code else None
    recheck = (
        VolumeSnapshot(
            volume_id=volume_id,
            region="us-east-1",
            availability_zone="us-east-1a",
            size_gib=50,
            state=state,
            cluster_tag_value=tag_value,
            attachment_ids=attachments,
        )
        if action is VolumeAction.DELETE_REQUESTED
        else None
    )
    return VolumeOutcome(
        volume_id=volume_id,
        region="us-east-1",
        availability_zone="us-east-1a",
        size_gib=50,
        observed_state=state,
        cluster_tag_value=tag_value,
        attachment_ids=attachments,
        policy=policy,
        action=action,
        action_result=action_result,
        reason_code=reason_code,
        reason=reason,
        follow_up=follow_up,
        recheck=recheck,
        error=error,
    )


def outcome(
    *,
    policy: VolumePolicy,
    volumes: tuple[VolumeOutcome, ...],
    status: VolumeCleanupStatus,
    stack_name: str = "gco-us-east-1",
    deletion_authorized: bool = False,
    authorization_source: DeletionAuthorizationSource = DeletionAuthorizationSource.NONE,
    error: SafeError | None = None,
) -> TargetVolumeCleanupOutcome:
    return TargetVolumeCleanupOutcome(
        stack_name=stack_name,
        stack_id=None,
        target_region="us-east-1",
        target_cluster=stack_name,
        cluster_tag_key=f"kubernetes.io/cluster/{stack_name}",
        policy=policy,
        deletion_authorized=deletion_authorized,
        authorization_source=authorization_source,
        status=status,
        volumes=volumes,
        successful=status
        in {
            VolumeCleanupStatus.COMPLETED,
            VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS,
        },
        error=error,
    )


def authorized_delete_outcome(
    *,
    volumes: tuple[VolumeOutcome, ...],
    status: VolumeCleanupStatus,
    stack_name: str = "gco-us-east-1",
    error: SafeError | None = None,
) -> TargetVolumeCleanupOutcome:
    return outcome(
        policy=VolumePolicy.DELETE,
        volumes=volumes,
        status=status,
        stack_name=stack_name,
        deletion_authorized=True,
        authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
        error=error,
    )


def status_of(target: TargetVolumeCleanupOutcome) -> VolumeCleanupTargetStatus:
    return evaluate_target_cleanup_status(publish_volume_cleanup_outcome(target))


class TestTargetStatusMapping:
    def test_retain_with_owned_volumes_is_successful(self) -> None:
        target = outcome(
            policy=VolumePolicy.RETAIN,
            status=VolumeCleanupStatus.COMPLETED,
            volumes=(
                record(
                    "vol-0000000000000000a",
                    policy=VolumePolicy.RETAIN,
                    action=VolumeAction.RETAINED,
                    action_result=VolumeActionResult.SUCCESS,
                    reason_code=VolumeReasonCode.RETAIN_POLICY,
                ),
            ),
        )

        status = status_of(target)

        assert status.reasons == ()
        assert status.successful is True
        assert status.cleanup_successful is True
        assert status.reporting_successful is True

    def test_authorized_delete_with_owned_safety_skip_is_unsuccessful(self) -> None:
        target = authorized_delete_outcome(
            status=VolumeCleanupStatus.FAILED,
            volumes=(
                record(
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

        status = status_of(target)

        assert VolumeCleanupExitReason.OWNED_VOLUME_REMAINS in status.reasons
        assert VolumeCleanupExitReason.STATUS_DISAGREEMENT not in status.reasons
        assert status.cleanup_successful is False
        assert status.reporting_successful is True

    def test_authorized_delete_with_failed_owned_volume_is_unsuccessful(self) -> None:
        target = authorized_delete_outcome(
            status=VolumeCleanupStatus.FAILED,
            volumes=(
                record(
                    "vol-0000000000000000c",
                    policy=VolumePolicy.DELETE,
                    action=VolumeAction.FAILED,
                    action_result=VolumeActionResult.ERROR,
                    reason_code=VolumeReasonCode.DELETE_ERROR,
                    error=SafeError(None, "RuntimeError", "The delete request failed safely."),
                ),
            ),
        )

        status = status_of(target)

        assert VolumeCleanupExitReason.VOLUME_FAILED in status.reasons
        assert status.successful is False

    def test_non_owned_retention_does_not_fail_authorized_delete(self) -> None:
        target = authorized_delete_outcome(
            status=VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS,
            volumes=(
                record(
                    "vol-0000000000000000d",
                    policy=VolumePolicy.DELETE,
                    action=VolumeAction.SKIPPED,
                    action_result=VolumeActionResult.SAFETY_PRESERVED,
                    reason_code=VolumeReasonCode.OWNERSHIP_SAFETY,
                    tag_value="shared",
                ),
            ),
        )

        status = status_of(target)

        assert status.reasons == ()
        assert status.successful is True

    def test_already_absent_is_successful(self) -> None:
        target = authorized_delete_outcome(
            status=VolumeCleanupStatus.COMPLETED,
            volumes=(
                record(
                    "vol-0000000000000000e",
                    policy=VolumePolicy.DELETE,
                    action=VolumeAction.ALREADY_ABSENT,
                    action_result=VolumeActionResult.IDEMPOTENT_SUCCESS,
                    reason_code=VolumeReasonCode.ALREADY_ABSENT,
                ),
                record(
                    "vol-0000000000000000f",
                    policy=VolumePolicy.DELETE,
                    action=VolumeAction.DELETE_REQUESTED,
                    action_result=VolumeActionResult.SUCCESS,
                    reason_code=None,
                ),
            ),
        )

        status = status_of(target)

        assert status.reasons == ()
        assert status.successful is True

    def test_blocked_target_reports_cleanup_blocked(self) -> None:
        blocked = TargetVolumeCleanupOutcome(
            stack_name="gco-us-east-1",
            stack_id=None,
            target_region="us-east-1",
            target_cluster="gco-us-east-1",
            cluster_tag_key="kubernetes.io/cluster/gco-us-east-1",
            policy=VolumePolicy.RETAIN,
            deletion_authorized=False,
            authorization_source=DeletionAuthorizationSource.NONE,
            status=VolumeCleanupStatus.SKIPPED,
            blocking_reason_code="cluster-present",
            blocking_reason="The target cluster still exists.",
            follow_up="Confirm cluster deletion before retrying cleanup.",
            successful=False,
        )

        status = evaluate_target_cleanup_status(publish_volume_cleanup_outcome(blocked))

        assert status.reasons == (VolumeCleanupExitReason.CLEANUP_BLOCKED,)
        assert status.successful is False

    def test_reporting_failure_is_independent_of_cleanup_success(self) -> None:
        incomplete = VolumeOutcome(
            volume_id="vol-00000000000000010",
            region="us-east-1",
            availability_zone="us-east-1a",
            size_gib=50,
            observed_state="available",
            cluster_tag_value="owned",
            attachment_ids=(),
            policy=VolumePolicy.DELETE,
            action=VolumeAction.ALREADY_ABSENT,
            action_result=VolumeActionResult.IDEMPOTENT_SUCCESS,
        )
        target = authorized_delete_outcome(
            status=VolumeCleanupStatus.COMPLETED,
            volumes=(incomplete,),
        )

        status = status_of(target)

        assert status.reasons == (VolumeCleanupExitReason.REPORTING_INCOMPLETE,)
        assert status.cleanup_successful is True
        assert status.reporting_successful is False
        assert status.successful is False

    def test_recorded_and_derived_status_disagreement_fails_closed(self) -> None:
        target = authorized_delete_outcome(
            status=VolumeCleanupStatus.FAILED,
            volumes=(
                record(
                    "vol-00000000000000011",
                    policy=VolumePolicy.DELETE,
                    action=VolumeAction.DELETE_REQUESTED,
                    action_result=VolumeActionResult.SUCCESS,
                    reason_code=None,
                ),
            ),
            error=SafeError(None, "RuntimeError", "Discovery was incomplete."),
        )
        published = publish_volume_cleanup_outcome(target)
        optimistic = VolumeCleanupPublication(
            cleanup_name=published.cleanup_name,
            details=published.details,
            outcome_successful=True,
            reporting_successful=True,
            published=False,
        )

        status = evaluate_target_cleanup_status(optimistic)

        assert VolumeCleanupExitReason.STATUS_DISAGREEMENT in status.reasons
        assert VolumeCleanupExitReason.CLEANUP_FAILED in status.reasons
        assert status.successful is False


class TestCommandResultAggregation:
    def test_targets_stay_isolated_and_deterministically_ordered(self) -> None:
        failing = authorized_delete_outcome(
            stack_name="gco-us-west-2",
            status=VolumeCleanupStatus.FAILED,
            volumes=(
                record(
                    "vol-00000000000000012",
                    policy=VolumePolicy.DELETE,
                    action=VolumeAction.SKIPPED,
                    action_result=VolumeActionResult.SAFETY_PRESERVED,
                    reason_code=VolumeReasonCode.STATE_NOT_AVAILABLE,
                    state="deleting",
                ),
            ),
        )
        succeeding = authorized_delete_outcome(
            stack_name="gco-us-east-1",
            status=VolumeCleanupStatus.COMPLETED,
            volumes=(
                record(
                    "vol-00000000000000013",
                    policy=VolumePolicy.DELETE,
                    action=VolumeAction.DELETE_REQUESTED,
                    action_result=VolumeActionResult.SUCCESS,
                    reason_code=None,
                ),
            ),
        )

        result = evaluate_volume_cleanup_result(
            publish_volume_cleanup_outcome(item) for item in (failing, succeeding)
        )

        assert [status.stack_name for status in result.targets] == [
            "gco-us-east-1",
            "gco-us-west-2",
        ]
        assert result.successful is False
        assert result.reasons == (
            VolumeCleanupExitReason.CLEANUP_FAILED,
            VolumeCleanupExitReason.OWNED_VOLUME_REMAINS,
        )
        assert [status.stack_name for status in result.unsuccessful_targets] == ["gco-us-west-2"]

    def test_empty_result_is_successful_and_unattempted(self) -> None:
        result = VolumeCleanupCommandResult()

        assert result.attempted is False
        assert result.successful is True

    def test_duplicate_targets_are_rejected(self) -> None:
        status = VolumeCleanupTargetStatus(
            stack_name="gco-us-east-1",
            cleanup_successful=True,
            reporting_successful=True,
        )

        with pytest.raises(ValueError, match="duplicate target stacks"):
            VolumeCleanupCommandResult(targets=(status, status))

    def test_status_flags_must_agree_with_reasons(self) -> None:
        with pytest.raises(ValueError, match="cleanup success must agree"):
            VolumeCleanupTargetStatus(
                stack_name="gco-us-east-1",
                cleanup_successful=True,
                reporting_successful=True,
                reasons=(VolumeCleanupExitReason.CLEANUP_FAILED,),
            )


class TestExitMapping:
    def test_stack_failure_stays_unsuccessful_regardless_of_cleanup(self) -> None:
        assert (
            destroy_command_exit_code(
                stack_successful=False,
                cleanup=VolumeCleanupCommandResult(),
            )
            == EXIT_FAILURE
        )

    def test_stack_success_without_cleanup_preserves_existing_exit(self) -> None:
        assert destroy_command_exit_code(stack_successful=True) == EXIT_SUCCESS
        assert (
            destroy_command_exit_code(
                stack_successful=True,
                cleanup=VolumeCleanupCommandResult(),
            )
            == EXIT_SUCCESS
        )

    def test_unsuccessful_cleanup_fails_a_successful_stack_result(self) -> None:
        result = VolumeCleanupCommandResult(
            targets=(
                VolumeCleanupTargetStatus(
                    stack_name="gco-us-east-1",
                    cleanup_successful=False,
                    reporting_successful=True,
                    reasons=(VolumeCleanupExitReason.OWNED_VOLUME_REMAINS,),
                ),
            )
        )

        assert destroy_command_exit_code(stack_successful=True, cleanup=result) == EXIT_FAILURE

    def test_reporting_failure_alone_fails_the_command(self) -> None:
        result = VolumeCleanupCommandResult(
            targets=(
                VolumeCleanupTargetStatus(
                    stack_name="gco-us-east-1",
                    cleanup_successful=True,
                    reporting_successful=False,
                    reasons=(VolumeCleanupExitReason.REPORTING_INCOMPLETE,),
                ),
            )
        )

        assert result.cleanup_successful is True
        assert destroy_command_exit_code(stack_successful=True, cleanup=result) == EXIT_FAILURE


class TestCommandResultRendering:
    def test_unattempted_cleanup_renders_nothing(self) -> None:
        formatter = FakeFormatter()
        render_volume_cleanup_command_result(formatter, VolumeCleanupCommandResult())

        assert formatter.success == []
        assert formatter.errors == []

    def test_successful_cleanup_reports_target_count(self) -> None:
        formatter = FakeFormatter()
        result = VolumeCleanupCommandResult(
            targets=(
                VolumeCleanupTargetStatus(
                    stack_name="gco-us-east-1",
                    cleanup_successful=True,
                    reporting_successful=True,
                ),
            )
        )

        render_volume_cleanup_command_result(formatter, result)

        assert formatter.success == ["EBS volume cleanup succeeded for 1 target(s)"]

    def test_unsuccessful_cleanup_reports_every_reason(self) -> None:
        formatter = FakeFormatter()
        result = VolumeCleanupCommandResult(
            targets=(
                VolumeCleanupTargetStatus(
                    stack_name="gco-us-west-2",
                    cleanup_successful=False,
                    reporting_successful=False,
                    reasons=(
                        VolumeCleanupExitReason.OWNED_VOLUME_REMAINS,
                        VolumeCleanupExitReason.REPORTING_INCOMPLETE,
                    ),
                ),
            )
        )

        render_volume_cleanup_command_result(formatter, result)

        rendered = "\n".join(formatter.errors)
        assert "unsuccessful for 1 of 1 target(s)" in rendered
        assert VolumeCleanupExitReason.OWNED_VOLUME_REMAINS.value in rendered
        assert VolumeCleanupExitReason.REPORTING_INCOMPLETE.value in rendered
        assert "gco-us-west-2" in rendered
