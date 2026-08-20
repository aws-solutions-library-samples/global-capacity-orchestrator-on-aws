"""Focused offline tests for the shared EBS cleanup renderer and publication."""

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
)
from cli.volume_cleanup_reporting import (
    EBS_VOLUME_CLEANUP_NAME,
    REQUIRED_VOLUME_FIELDS,
    publish_volume_cleanup_outcome,
    render_volume_cleanup_outcome,
    render_volume_cleanup_publication,
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


def volume_record(
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
        error=error,
    )


def target_outcome(
    *,
    policy: VolumePolicy,
    volumes: tuple[VolumeOutcome, ...],
    status: VolumeCleanupStatus = VolumeCleanupStatus.COMPLETED,
    deletion_authorized: bool = False,
    authorization_source: DeletionAuthorizationSource = DeletionAuthorizationSource.NONE,
) -> TargetVolumeCleanupOutcome:
    return TargetVolumeCleanupOutcome(
        stack_name="gco-us-east-1",
        stack_id="arn:aws:cloudformation:us-east-1:123456789012:stack/gco-us-east-1/abc",
        target_region="us-east-1",
        target_cluster="gco-us-east-1",
        cluster_tag_key="kubernetes.io/cluster/gco-us-east-1",
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
    )


def retained_outcome() -> TargetVolumeCleanupOutcome:
    return target_outcome(
        policy=VolumePolicy.RETAIN,
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


class TestSharedRendering:
    def test_single_and_orchestrated_paths_render_identical_fields(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        outcome = retained_outcome()
        single = FakeFormatter()
        render_volume_cleanup_outcome(single, outcome)
        single_text = rendered_text(single, capsys)

        published: list[tuple[str, dict[str, Any]]] = []
        publication = publish_volume_cleanup_outcome(
            outcome,
            publisher=lambda name, details: published.append((name, details)),
        )
        orchestrated = FakeFormatter()
        render_volume_cleanup_outcome(orchestrated, published[0][1])
        orchestrated_text = rendered_text(orchestrated, capsys)

        assert publication.details == published[0][1]
        assert single_text == orchestrated_text

    def test_every_required_volume_field_is_rendered(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        outcome = retained_outcome()
        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, outcome)
        text = rendered_text(formatter, capsys)

        record = outcome.volumes[0]
        assert record.volume_id in text
        assert record.availability_zone in text
        assert f"size={record.size_gib}GiB" in text
        assert f"state={record.observed_state}" in text
        assert "tag=owned" in text
        assert "attachments=none" in text
        assert f"policy={VolumePolicy.RETAIN.value}" in text
        assert f"action={VolumeAction.RETAINED.value}" in text
        assert f"result={VolumeActionResult.SUCCESS.value}" in text
        assert DeletionAuthorizationSource.NONE.value in text
        assert set(REQUIRED_VOLUME_FIELDS) <= set(outcome.to_dict()["volumes"][0])  # type: ignore[index]

    def test_non_success_records_include_reason_and_follow_up(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        outcome = target_outcome(
            policy=VolumePolicy.DELETE,
            deletion_authorized=True,
            authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
            status=VolumeCleanupStatus.FAILED,
            volumes=(
                volume_record(
                    "vol-0000000000000000b",
                    policy=VolumePolicy.DELETE,
                    action=VolumeAction.FAILED,
                    action_result=VolumeActionResult.ERROR,
                    reason_code=VolumeReasonCode.DELETE_ERROR,
                    error=SafeError(None, "RuntimeError", "The delete request failed safely."),
                ),
            ),
        )
        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, outcome)
        text = rendered_text(formatter, capsys)

        assert VolumeReasonCode.DELETE_ERROR.value in text
        assert "Follow-up action for vol-0000000000000000b." in text
        assert "The delete request failed safely." in text
        assert any("failed" in message for message in formatter.errors)
        assert any("may still exist" in message for message in formatter.warnings)

    def test_retained_volumes_warn_about_continuing_cost_and_policy(self) -> None:
        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, retained_outcome())

        warning = "\n".join(formatter.warnings)
        assert "50 GiB" in warning
        assert "continue to incur storage cost" in warning
        assert f"{VolumePolicy.RETAIN.value} volume policy" in warning
        assert "vol-0000000000000000a" in warning

    def test_non_owned_delete_skip_keeps_ownership_safety_visible(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        outcome = target_outcome(
            policy=VolumePolicy.DELETE,
            deletion_authorized=True,
            authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
            status=VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS,
            volumes=(
                volume_record(
                    "vol-0000000000000000c",
                    policy=VolumePolicy.DELETE,
                    action=VolumeAction.SKIPPED,
                    action_result=VolumeActionResult.SAFETY_PRESERVED,
                    reason_code=VolumeReasonCode.OWNERSHIP_SAFETY,
                    tag_value="shared",
                ),
            ),
        )
        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, outcome)
        text = rendered_text(formatter, capsys)

        assert VolumeReasonCode.OWNERSHIP_SAFETY.value in text
        assert f"action={VolumeAction.SKIPPED.value}" in text
        assert "tag=shared" in text
        assert any("continue to incur storage cost" in item for item in formatter.warnings)
        assert formatter.success

    def test_blocked_target_reports_reason_and_follow_up(self) -> None:
        outcome = TargetVolumeCleanupOutcome(
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
        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, outcome)

        warning = "\n".join(formatter.warnings)
        assert "cluster-present" in warning
        assert "The target cluster still exists." in warning
        assert "Confirm cluster deletion before retrying cleanup." in warning
        assert not formatter.success

    def test_machine_readable_format_prints_serialized_details_once(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        formatter = FakeFormatter(config=FakeConfig(output_format="json"))
        outcome = retained_outcome()
        render_volume_cleanup_outcome(formatter, outcome)

        assert formatter.printed == [outcome.to_dict()]
        assert not formatter.success
        assert capsys.readouterr().out == ""

    def test_missing_required_fields_are_reported_without_raising(self) -> None:
        formatter = FakeFormatter()
        render_volume_cleanup_outcome(formatter, {"stack_name": "gco-us-east-1"})

        errors = "\n".join(formatter.errors)
        assert "missing required target field(s)" in errors
        assert "counts" in errors


class TestOutcomePublication:
    def test_publishes_complete_outcome_under_the_stable_name(self) -> None:
        outcome = retained_outcome()
        published: list[tuple[str, dict[str, Any]]] = []

        publication = publish_volume_cleanup_outcome(
            outcome,
            publisher=lambda name, details: published.append((name, details)),
        )

        assert published[0][0] == EBS_VOLUME_CLEANUP_NAME
        assert published[0][1] == outcome.to_dict()
        assert publication.published is True
        assert publication.reporting_successful is True
        assert publication.outcome_successful is True
        assert publication.successful is True

    def test_publication_without_channel_still_returns_details(self) -> None:
        publication = publish_volume_cleanup_outcome(retained_outcome())

        assert publication.published is False
        assert publication.details["stack_name"] == "gco-us-east-1"
        assert publication.successful is True

    def test_reporting_failure_is_independent_of_cleanup_success(self) -> None:
        unreportable = VolumeOutcome(
            volume_id="vol-0000000000000000d",
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
        outcome = target_outcome(
            policy=VolumePolicy.DELETE,
            deletion_authorized=True,
            authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
            volumes=(unreportable,),
        )
        published: list[tuple[str, dict[str, Any]]] = []

        publication = publish_volume_cleanup_outcome(
            outcome,
            publisher=lambda name, details: published.append((name, details)),
        )

        assert published[0][0] == EBS_VOLUME_CLEANUP_NAME
        assert publication.outcome_successful is True
        assert publication.reporting_successful is False
        assert publication.successful is False
        assert publication.reporting_error is not None

        formatter = FakeFormatter()
        render_volume_cleanup_publication(formatter, publication)
        errors = "\n".join(formatter.errors)
        assert "reporting is incomplete" in errors
        assert any("stack deletion status is reported separately" in w for w in formatter.warnings)

    def test_channel_failures_propagate(self) -> None:
        def failing_publisher(name: str, details: dict[str, Any]) -> None:
            raise RuntimeError("callback failed")

        with pytest.raises(RuntimeError, match="callback failed"):
            publish_volume_cleanup_outcome(retained_outcome(), publisher=failing_publisher)
