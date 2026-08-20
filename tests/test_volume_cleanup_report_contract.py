"""Terminal-status, warning, reporting-completeness, and exit-status contract tests.

These tests close the gaps between the focused renderer tests and the focused
exit-mapping tests: every terminal target status is rendered side by side, the
single-stack and orchestrated paths are compared field for field across all of
them, retained-cost warnings are checked under both policies, and incomplete
reporting is followed through to the command exit status. All AWS interaction is
absent by construction; outcomes are built from the pure cleanup models.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    SafeError,
    TargetVolumeCleanupOutcome,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupRequest,
    VolumeCleanupStatus,
    VolumeOutcome,
    VolumePolicy,
    VolumeReasonCode,
    VolumeSnapshot,
    aggregate_target_outcome,
    classify_volume,
)
from cli.volume_cleanup_reporting import (
    EXIT_FAILURE,
    EXIT_SUCCESS,
    VolumeCleanupExitReason,
    VolumeCleanupPublication,
    destroy_command_exit_code,
    evaluate_volume_cleanup_result,
    publish_volume_cleanup_outcome,
    render_volume_cleanup_outcome,
    volume_cleanup_publication_from_details,
)

RETAIN_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.RETAIN,
    deletion_authorized=False,
    authorization_source=DeletionAuthorizationSource.NONE,
)
DELETE_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.DELETE,
    deletion_authorized=True,
    authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
)


@dataclass
class FakeConfig:
    output_format: str = "table"


@dataclass
class FakeFormatter:
    """Minimal recording stand-in for the CLI output formatter."""

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


def rendered_text(formatter: FakeFormatter, capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    lines = [
        *formatter.info,
        *formatter.success,
        *formatter.warnings,
        *formatter.errors,
        captured.out,
        captured.err,
    ]
    return "\n".join(lines)


def target_for(stack_name: str) -> RegionalVolumeTarget:
    region = stack_name.removeprefix("gco-")
    return RegionalVolumeTarget(
        stack_name=stack_name,
        stack_id=f"arn:aws:cloudformation:{region}:123456789012:stack/{stack_name}/abc",
        region=region,
        cluster_name=stack_name,
        cluster_tag_key=f"kubernetes.io/cluster/{stack_name}",
    )


def snapshot(
    volume_id: str,
    *,
    region: str = "us-east-1",
    tag_value: str | None = "owned",
    state: str = "available",
    attachments: tuple[str, ...] = (),
    size_gib: int = 50,
) -> VolumeSnapshot:
    return VolumeSnapshot(
        volume_id=volume_id,
        region=region,
        availability_zone=f"{region}a",
        size_gib=size_gib,
        state=state,
        cluster_tag_value=tag_value,
        attachment_ids=attachments,
    )


def classified_record(
    volume_id: str,
    request: VolumeCleanupRequest,
    **snapshot_fields: Any,
) -> VolumeOutcome:
    """Return the terminal record the pure safety engine produces for one volume."""
    classification = classify_volume(snapshot(volume_id, **snapshot_fields), request)
    assert classification.outcome is not None
    return classification.outcome


def delete_requested_record(volume_id: str, **snapshot_fields: Any) -> VolumeOutcome:
    current = snapshot(volume_id, **snapshot_fields)
    return VolumeOutcome(
        volume_id=current.volume_id,
        region=current.region,
        availability_zone=current.availability_zone,
        size_gib=current.size_gib,
        observed_state=current.state,
        cluster_tag_value=current.cluster_tag_value,
        attachment_ids=current.attachment_ids,
        policy=VolumePolicy.DELETE,
        action=VolumeAction.DELETE_REQUESTED,
        action_result=VolumeActionResult.SUCCESS,
        reason_code=VolumeReasonCode.DELETE_REQUEST_ACCEPTED,
        reason="EC2 accepted the authorized deletion request.",
        follow_up="No action is required.",
        recheck=current,
    )


def already_absent_record(volume_id: str, **snapshot_fields: Any) -> VolumeOutcome:
    current = snapshot(volume_id, **snapshot_fields)
    return VolumeOutcome(
        volume_id=current.volume_id,
        region=current.region,
        availability_zone=current.availability_zone,
        size_gib=current.size_gib,
        observed_state=current.state,
        cluster_tag_value=current.cluster_tag_value,
        attachment_ids=current.attachment_ids,
        policy=VolumePolicy.DELETE,
        action=VolumeAction.ALREADY_ABSENT,
        action_result=VolumeActionResult.IDEMPOTENT_SUCCESS,
        reason_code=VolumeReasonCode.ALREADY_ABSENT,
        reason="EC2 reported InvalidVolume.NotFound for this exact volume ID.",
        follow_up="No action is required; the volume was already absent.",
    )


def failed_record(volume_id: str, **snapshot_fields: Any) -> VolumeOutcome:
    current = snapshot(volume_id, **snapshot_fields)
    return VolumeOutcome(
        volume_id=current.volume_id,
        region=current.region,
        availability_zone=current.availability_zone,
        size_gib=current.size_gib,
        observed_state=current.state,
        cluster_tag_value=current.cluster_tag_value,
        attachment_ids=current.attachment_ids,
        policy=VolumePolicy.DELETE,
        action=VolumeAction.FAILED,
        action_result=VolumeActionResult.ERROR,
        reason_code=VolumeReasonCode.DELETE_ERROR,
        reason="The authorized volume deletion request failed.",
        follow_up="Resolve the reported EC2 deletion error and retry cleanup.",
        error=SafeError(None, "RuntimeError", "The delete request failed safely."),
    )


def completed_retain_outcome(stack_name: str = "gco-us-east-1") -> TargetVolumeCleanupOutcome:
    """Retain policy preserving one large and one small owned volume."""
    target = target_for(stack_name)
    return aggregate_target_outcome(
        target=target,
        request=RETAIN_REQUEST,
        records=(
            classified_record("vol-0a11111111111111a", RETAIN_REQUEST, region=target.region),
            classified_record(
                "vol-0b22222222222222b", RETAIN_REQUEST, region=target.region, size_gib=5
            ),
        ),
    )


def completed_delete_outcome(stack_name: str = "gco-us-east-1") -> TargetVolumeCleanupOutcome:
    """Authorized delete where every owned volume is deleted or already absent."""
    target = target_for(stack_name)
    return aggregate_target_outcome(
        target=target,
        request=DELETE_REQUEST,
        records=(
            delete_requested_record("vol-0c33333333333333c", region=target.region),
            already_absent_record("vol-0d44444444444444d", region=target.region, size_gib=5),
        ),
    )


def safety_retention_outcome(stack_name: str = "gco-us-west-2") -> TargetVolumeCleanupOutcome:
    """Authorized delete where only a volume GCO does not own is preserved."""
    target = target_for(stack_name)
    return aggregate_target_outcome(
        target=target,
        request=DELETE_REQUEST,
        records=(
            delete_requested_record("vol-0e55555555555555e", region=target.region),
            classified_record(
                "vol-0f66666666666666f",
                DELETE_REQUEST,
                region=target.region,
                tag_value="shared",
                size_gib=5,
            ),
        ),
    )


def failed_outcome(stack_name: str = "gco-ap-south-1") -> TargetVolumeCleanupOutcome:
    """Authorized delete where an owned volume is preserved and another failed."""
    target = target_for(stack_name)
    return aggregate_target_outcome(
        target=target,
        request=DELETE_REQUEST,
        records=(
            classified_record(
                "vol-0111111111111111a",
                DELETE_REQUEST,
                region=target.region,
                state="in-use",
                attachments=("i-0999999999999999a",),
            ),
            failed_record("vol-0222222222222222b", region=target.region),
        ),
    )


def skipped_outcome(stack_name: str = "gco-eu-west-1") -> TargetVolumeCleanupOutcome:
    """Blocked target: the prerequisite gate stopped all EBS work."""
    target = target_for(stack_name)
    return TargetVolumeCleanupOutcome(
        stack_name=target.stack_name,
        stack_id=target.stack_id,
        target_region=target.region,
        target_cluster=target.cluster_name,
        cluster_tag_key=target.cluster_tag_key,
        policy=DELETE_REQUEST.policy,
        deletion_authorized=DELETE_REQUEST.deletion_authorized,
        authorization_source=DELETE_REQUEST.authorization_source,
        status=VolumeCleanupStatus.SKIPPED,
        blocking_reason_code="cluster-absence-unverified",
        blocking_reason="Cluster absence could not be verified.",
        follow_up="Re-verify cluster absence before retrying cleanup.",
        successful=False,
    )


TERMINAL_OUTCOMES: tuple[tuple[VolumeCleanupStatus, TargetVolumeCleanupOutcome], ...] = (
    (VolumeCleanupStatus.COMPLETED, completed_retain_outcome()),
    (VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS, safety_retention_outcome()),
    (VolumeCleanupStatus.SKIPPED, skipped_outcome()),
    (VolumeCleanupStatus.FAILED, failed_outcome()),
)


def exit_code_for(outcome: TargetVolumeCleanupOutcome) -> int:
    result = evaluate_volume_cleanup_result([publish_volume_cleanup_outcome(outcome)])
    return destroy_command_exit_code(stack_successful=True, cleanup=result)


class TestTerminalStatusRendering:
    @pytest.mark.parametrize(
        ("status", "outcome"),
        TERMINAL_OUTCOMES,
        ids=[status.value for status, _ in TERMINAL_OUTCOMES],
    )
    def test_each_terminal_status_reports_its_own_surface(
        self,
        status: VolumeCleanupStatus,
        outcome: TargetVolumeCleanupOutcome,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, outcome)
        text = rendered_text(formatter, capsys)

        assert f"status: {status.value}" in text
        assert outcome.stack_name in text
        successful = status in {
            VolumeCleanupStatus.COMPLETED,
            VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS,
        }
        assert bool(formatter.success) is successful
        assert bool(formatter.errors) is (status is VolumeCleanupStatus.FAILED)
        assert (status is VolumeCleanupStatus.SKIPPED) is any(
            "was skipped" in message for message in formatter.warnings
        )
        assert exit_code_for(outcome) == (EXIT_SUCCESS if successful else EXIT_FAILURE)

    def test_all_terminal_statuses_render_side_by_side(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        formatter = FakeFormatter()
        for _, outcome in TERMINAL_OUTCOMES:
            render_volume_cleanup_outcome(formatter, outcome)
        text = rendered_text(formatter, capsys)

        for _, outcome in TERMINAL_OUTCOMES:
            assert outcome.stack_name in text
        assert len(formatter.success) == 2
        assert any("gco-eu-west-1" in message for message in formatter.warnings)
        assert any("gco-ap-south-1" in message for message in formatter.errors)
        for status, _ in TERMINAL_OUTCOMES:
            assert f"status: {status.value}" in text

    def test_blocked_target_reports_zero_discovered_volumes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, skipped_outcome())
        text = rendered_text(formatter, capsys)

        assert "discovered=0" in text
        assert "cluster-absence-unverified" in text
        assert exit_code_for(skipped_outcome()) == EXIT_FAILURE


class TestSingleAndOrchestratedEquivalence:
    @pytest.mark.parametrize(
        ("status", "outcome"),
        TERMINAL_OUTCOMES,
        ids=[status.value for status, _ in TERMINAL_OUTCOMES],
    )
    def test_rendered_fields_match_across_both_paths(
        self,
        status: VolumeCleanupStatus,
        outcome: TargetVolumeCleanupOutcome,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        single = FakeFormatter()
        render_volume_cleanup_outcome(single, outcome)
        single_text = rendered_text(single, capsys)

        published: list[tuple[str, dict[str, Any]]] = []
        publish_volume_cleanup_outcome(
            outcome,
            publisher=lambda name, details: published.append((name, details)),
        )
        orchestrated = FakeFormatter()
        render_volume_cleanup_outcome(orchestrated, published[0][1])
        orchestrated_text = rendered_text(orchestrated, capsys)

        assert published[0][1] == outcome.to_dict()
        assert single_text == orchestrated_text
        assert single.warnings == orchestrated.warnings
        assert single.errors == orchestrated.errors
        assert single.success == orchestrated.success

    @pytest.mark.parametrize(
        ("status", "outcome"),
        TERMINAL_OUTCOMES,
        ids=[status.value for status, _ in TERMINAL_OUTCOMES],
    )
    def test_machine_readable_details_match_across_both_paths(
        self,
        status: VolumeCleanupStatus,
        outcome: TargetVolumeCleanupOutcome,
    ) -> None:
        single = FakeFormatter(config=FakeConfig(output_format="json"))
        render_volume_cleanup_outcome(single, outcome)

        orchestrated = FakeFormatter(config=FakeConfig(output_format="json"))
        render_volume_cleanup_outcome(orchestrated, outcome.to_dict())

        assert single.printed == orchestrated.printed == [outcome.to_dict()]


class TestRetainedCostWarning:
    def test_retain_policy_warning_totals_every_preserved_volume(self) -> None:
        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, completed_retain_outcome())

        warning = "\n".join(formatter.warnings)
        assert "2 EBS volume(s) totaling 55 GiB remain in us-east-1" in warning
        assert "continue to incur storage cost" in warning
        assert f"{VolumePolicy.RETAIN.value} volume policy" in warning
        assert "retained vol-0a11111111111111a" in warning
        assert "retained vol-0b22222222222222b" in warning

    def test_authorized_delete_warning_names_only_preserved_volumes(self) -> None:
        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, safety_retention_outcome())

        warning = "\n".join(formatter.warnings)
        assert "1 EBS volume(s) totaling 5 GiB remain in us-west-2" in warning
        assert f"{VolumePolicy.DELETE.value} volume policy" in warning
        assert VolumeReasonCode.OWNERSHIP_SAFETY.value in warning
        assert "vol-0e55555555555555e" not in warning

    def test_completed_delete_without_preserved_volumes_warns_nothing(self) -> None:
        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, completed_delete_outcome())

        assert formatter.warnings == []
        assert formatter.success

    def test_failed_volumes_warn_about_possible_residual_cost(self) -> None:
        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, failed_outcome())

        warning = "\n".join(formatter.warnings)
        assert "may still exist and incur storage cost" in warning
        assert "vol-0222222222222222b" in warning


class TestAlreadyAbsentSuccess:
    def test_already_absent_records_render_idempotent_success(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        outcome = completed_delete_outcome()
        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, outcome)
        text = rendered_text(formatter, capsys)

        assert f"action={VolumeAction.ALREADY_ABSENT.value}" in text
        assert f"result={VolumeActionResult.IDEMPOTENT_SUCCESS.value}" in text
        assert VolumeReasonCode.ALREADY_ABSENT.value in text
        assert "already-absent=1" in text
        assert "deleted=1" in text

    def test_already_absent_only_outcome_exits_successfully(self) -> None:
        target = target_for("gco-us-east-1")
        outcome = aggregate_target_outcome(
            target=target,
            request=DELETE_REQUEST,
            records=(
                already_absent_record("vol-0d44444444444444d", region=target.region),
                already_absent_record("vol-0d55555555555555d", region=target.region),
            ),
        )

        assert outcome.status is VolumeCleanupStatus.COMPLETED
        assert outcome.counts.already_absent == 2
        assert exit_code_for(outcome) == EXIT_SUCCESS


class TestOwnedSkipVersusNonOwnedRetention:
    def test_owned_safety_skip_fails_while_non_owned_retention_succeeds(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = target_for("gco-us-east-1")
        non_owned = classified_record(
            "vol-0aa1111111111111a",
            DELETE_REQUEST,
            region=target.region,
            tag_value="shared",
            size_gib=5,
        )
        owned_attached = classified_record(
            "vol-0bb2222222222222b",
            DELETE_REQUEST,
            region=target.region,
            state="in-use",
            attachments=("i-0aaaaaaaaaaaaaaaa",),
        )

        non_owned_only = aggregate_target_outcome(
            target=target, request=DELETE_REQUEST, records=(non_owned,)
        )
        with_owned = aggregate_target_outcome(
            target=target, request=DELETE_REQUEST, records=(non_owned, owned_attached)
        )

        assert non_owned_only.status is VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS
        assert exit_code_for(non_owned_only) == EXIT_SUCCESS
        assert with_owned.status is VolumeCleanupStatus.FAILED
        assert exit_code_for(with_owned) == EXIT_FAILURE

        reasons = evaluate_volume_cleanup_result(
            [publish_volume_cleanup_outcome(with_owned)]
        ).reasons
        assert VolumeCleanupExitReason.OWNED_VOLUME_REMAINS in reasons
        assert VolumeCleanupExitReason.VOLUME_FAILED not in reasons

        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, with_owned)
        text = rendered_text(formatter, capsys)
        assert VolumeReasonCode.OWNERSHIP_SAFETY.value in text
        assert VolumeReasonCode.STATE_NOT_AVAILABLE.value in text
        assert "attachments=i-0aaaaaaaaaaaaaaaa" in text
        assert "state=in-use" in text
        assert "tag=shared" in text

    def test_retain_policy_keeps_owned_and_non_owned_volumes_successful(self) -> None:
        target = target_for("gco-us-east-1")
        outcome = aggregate_target_outcome(
            target=target,
            request=RETAIN_REQUEST,
            records=(
                classified_record("vol-0cc3333333333333c", RETAIN_REQUEST, region=target.region),
                classified_record(
                    "vol-0dd4444444444444d",
                    RETAIN_REQUEST,
                    region=target.region,
                    tag_value="shared",
                ),
            ),
        )

        assert outcome.status is VolumeCleanupStatus.COMPLETED
        assert outcome.counts.retained == 2
        assert exit_code_for(outcome) == EXIT_SUCCESS


def optimistic_publication(details: dict[str, Any]) -> VolumeCleanupPublication:
    """Publication that claims complete reporting, so derivation must catch gaps."""
    return VolumeCleanupPublication(
        cleanup_name="ebs-volumes",
        details=details,
        outcome_successful=True,
        reporting_successful=True,
        published=True,
    )


class TestMissingRequiredFields:
    def test_missing_target_field_is_reported_and_fails_the_command(self) -> None:
        details = completed_retain_outcome().to_dict()
        del details["counts"]

        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, details)
        status = evaluate_volume_cleanup_result([optimistic_publication(details)])

        errors = "\n".join(formatter.errors)
        assert "missing required target field(s): counts" in errors
        assert status.reasons == (VolumeCleanupExitReason.REPORTING_INCOMPLETE,)
        assert status.cleanup_successful is True
        assert destroy_command_exit_code(stack_successful=True, cleanup=status) == EXIT_FAILURE

    def test_missing_per_volume_field_is_reported_and_fails_the_command(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        details = completed_retain_outcome().to_dict()
        volumes = details["volumes"]
        assert isinstance(volumes, list)
        first = volumes[0]
        assert isinstance(first, dict)
        del first["follow_up"]

        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, details)
        text = rendered_text(formatter, capsys)
        status = evaluate_volume_cleanup_result([optimistic_publication(details)])

        assert "vol-0a11111111111111a is missing required field(s): follow_up" in text
        assert "vol-0b22222222222222b" in text
        assert status.reasons == (VolumeCleanupExitReason.REPORTING_INCOMPLETE,)
        assert status.reporting_successful is False
        assert destroy_command_exit_code(stack_successful=True, cleanup=status) == EXIT_FAILURE

    def test_unreadable_status_fails_cleanup_and_not_only_reporting(self) -> None:
        details = completed_retain_outcome().to_dict()
        del details["status"]

        status = evaluate_volume_cleanup_result([optimistic_publication(details)])

        assert VolumeCleanupExitReason.UNREADABLE_OUTCOME in status.reasons
        assert VolumeCleanupExitReason.STATUS_DISAGREEMENT in status.reasons
        assert status.cleanup_successful is False
        assert destroy_command_exit_code(stack_successful=True, cleanup=status) == EXIT_FAILURE

    def test_stack_failure_stays_unsuccessful_even_with_complete_reporting(self) -> None:
        result = evaluate_volume_cleanup_result(
            [publish_volume_cleanup_outcome(completed_retain_outcome())]
        )

        assert result.successful is True
        assert destroy_command_exit_code(stack_successful=False, cleanup=result) == EXIT_FAILURE


class TestReplayedOrchestratedPublications:
    """Outcomes replayed off the cleanup channel keep the single-path semantics."""

    @pytest.mark.parametrize(
        "build",
        [
            completed_retain_outcome,
            completed_delete_outcome,
            safety_retention_outcome,
            failed_outcome,
            skipped_outcome,
        ],
    )
    def test_replayed_publication_matches_the_direct_publication(self, build) -> None:
        outcome = build()
        direct = publish_volume_cleanup_outcome(outcome)
        published: list[tuple[str, dict[str, Any]]] = []
        publish_volume_cleanup_outcome(
            outcome,
            publisher=lambda name, details: published.append((name, details)),
        )

        replayed = volume_cleanup_publication_from_details(published[0][1])

        assert published[0][0] == replayed.cleanup_name
        assert replayed.details == direct.details
        assert replayed.outcome_successful == direct.outcome_successful
        assert replayed.reporting_successful is True
        assert evaluate_volume_cleanup_result([replayed]) == evaluate_volume_cleanup_result(
            [direct]
        )

    def test_incomplete_replayed_evidence_is_unsuccessful_reporting(self) -> None:
        details = completed_delete_outcome().to_dict()
        del details["counts"]

        replayed = volume_cleanup_publication_from_details(details)
        result = evaluate_volume_cleanup_result([replayed])

        assert replayed.reporting_successful is False
        assert VolumeCleanupExitReason.REPORTING_INCOMPLETE in result.reasons
        assert result.successful is False
        assert destroy_command_exit_code(stack_successful=True, cleanup=result) == EXIT_FAILURE

    def test_replayed_reporting_error_is_preserved(self) -> None:
        details: dict[str, Any] = {
            "stack_name": "gco-us-east-1",
            "status": None,
            "successful": False,
            "reporting_error": {
                "error_code": "InvalidVolume.NotFound",
                "error_type": "VolumeReportingError",
                "message": "the record could not serialize",
            },
        }

        replayed = volume_cleanup_publication_from_details(details)

        assert replayed.reporting_error is not None
        assert replayed.reporting_error.message == "the record could not serialize"
        assert replayed.successful is False

    def test_unreadable_replayed_evidence_never_exits_successfully(self) -> None:
        replayed = volume_cleanup_publication_from_details({1: "not-a-field"})  # type: ignore[dict-item]
        result = evaluate_volume_cleanup_result([replayed])

        assert replayed.successful is False
        assert VolumeCleanupExitReason.UNREADABLE_OUTCOME in result.reasons
        assert destroy_command_exit_code(stack_successful=True, cleanup=result) == EXIT_FAILURE
