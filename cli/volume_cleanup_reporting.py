"""Shared rendering and publication of regional EBS volume cleanup outcomes.

Single-stack and orchestrated destruction both report through this module so the
two paths cannot drift: they render the same serialized target and per-volume
fields and publish the same complete outcome under one stable cleanup name.

Presentation of preserved volumes
---------------------------------
The safety engine records a non-owned tagged volume as ``skipped`` with the
``ownership-safety`` reason and a ``safety-preserved`` result, while retain
policy records preservation as ``retained``. Aggregation already treats both as
preservation, and this renderer does the same: every record whose action is
``retained`` or ``skipped`` is reported as preserved storage, counts toward the
continuing-storage-cost warning, and always shows its own reason code, so an
``ownership-safety`` decision stays visible under either action label. The
renderer never rewrites an action recorded by classification.

Command status and exit mapping
-------------------------------
The command layer never invents its own volume semantics. It reads each
published outcome back and re-derives the documented status rules from the
serialized records, so a defect in one layer cannot silently produce a
successful exit. Retention completes successfully once reporting is complete,
authorized deletion is unsuccessful while any owned volume remains preserved or
failed, and an authoritative already-absent record counts as success. The stack
result itself keeps its existing semantics: volume cleanup can only add an
unsuccessful exit for a stack that otherwise succeeded, never relabel a failed
stack or change stack selection, ordering, or retry behavior.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .volume_cleanup import (
    OWNED_CLUSTER_TAG_VALUE,
    JsonValue,
    SafeError,
    TargetVolumeCleanupOutcome,
    VolumeAction,
    VolumeCleanupStatus,
    VolumePolicy,
    VolumeReportingError,
    normalize_safe_error,
    verify_volume_reporting,
)

#: Stable cleanup name used for every published EBS volume cleanup outcome.
EBS_VOLUME_CLEANUP_NAME = "ebs-volumes"

#: Shape of the existing destroy cleanup reporting channel (``record_cleanup``).
CleanupOutcomePublisher = Callable[[str, dict[str, Any]], None]

#: Target-level fields every published and rendered outcome must carry.
REQUIRED_TARGET_FIELDS: tuple[str, ...] = (
    "stack_name",
    "target_region",
    "target_cluster",
    "cluster_tag_key",
    "policy",
    "deletion_authorized",
    "authorization_source",
    "status",
    "counts",
    "volumes",
    "successful",
)

#: Per-volume fields every rendered record must carry (values may be ``null``).
REQUIRED_VOLUME_FIELDS: tuple[str, ...] = (
    "volume_id",
    "region",
    "availability_zone",
    "size_gib",
    "observed_state",
    "cluster_tag_value",
    "attachment_ids",
    "policy",
    "action",
    "action_result",
)

#: The only action whose record carries no separate reason and follow-up, matching
#: ``verify_volume_reporting``; every other action requires both.
_REASON_EXEMPT_ACTION = VolumeAction.DELETE_REQUESTED.value

#: Actions that deliberately preserve a discovered volume under the selected policy.
_PRESERVED_ACTIONS = frozenset(
    {
        VolumeAction.RETAINED.value,
        VolumeAction.SKIPPED.value,
    }
)

_COUNT_FIELDS: tuple[str, ...] = (
    "discovered",
    "deleted",
    "retained",
    "skipped",
    "already_absent",
    "failed",
)

_REPORTING_FOLLOW_UP = (
    "Resolve the reported volume-reporting error and retry cleanup; stack "
    "deletion status is reported separately and safety decisions are unchanged."
)


class CleanupFormatter(Protocol):
    """Output surface required by the shared cleanup renderer."""

    def print(self, data: Any, columns: list[str] | None = None) -> None:
        """Print formatted data in the configured output format."""

    def print_info(self, message: str) -> None:
        """Print an informational message."""

    def print_success(self, message: str) -> None:
        """Print a success message."""

    def print_warning(self, message: str) -> None:
        """Print a warning message."""

    def print_error(self, message: str) -> None:
        """Print an error message."""


@dataclass(frozen=True)
class VolumeCleanupPublication:
    """Result of publishing one target outcome through the cleanup channel.

    ``outcome_successful`` mirrors the cleanup service decision, while
    ``reporting_successful`` is independent: an unreportable record makes
    reporting unsuccessful without changing any safety decision and without
    relabeling the stack deletion result.
    """

    cleanup_name: str
    details: dict[str, JsonValue]
    outcome_successful: bool
    reporting_successful: bool
    published: bool
    reporting_error: SafeError | None = None

    def __post_init__(self) -> None:
        if not self.cleanup_name:
            raise ValueError("a cleanup publication requires the stable cleanup name")
        if self.reporting_successful != (self.reporting_error is None):
            raise ValueError("reporting success must agree with the recorded reporting error")

    @property
    def successful(self) -> bool:
        """Whether cleanup and its required reporting both succeeded."""
        return self.outcome_successful and self.reporting_successful


def volume_cleanup_details(
    outcome: TargetVolumeCleanupOutcome | Mapping[str, Any],
) -> dict[str, JsonValue]:
    """Return the serialized target outcome used by publication and rendering."""
    if isinstance(outcome, TargetVolumeCleanupOutcome):
        return outcome.to_dict()
    if not isinstance(outcome, Mapping):
        raise VolumeReportingError("a cleanup outcome must be an outcome object or mapping")
    details: dict[str, JsonValue] = {}
    for key, value in outcome.items():
        if not isinstance(key, str):
            raise VolumeReportingError("serialized cleanup outcomes require string keys")
        details[key] = value
    return details


def missing_target_fields(details: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the required target fields absent from one serialized outcome."""
    return tuple(field for field in REQUIRED_TARGET_FIELDS if field not in details)


def missing_volume_fields(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the required per-volume fields absent from one serialized record."""
    missing = [field for field in REQUIRED_VOLUME_FIELDS if field not in record]
    action = record.get("action")
    if isinstance(action, str) and action and action != _REASON_EXEMPT_ACTION:
        missing.extend(
            field for field in ("reason_code", "reason", "follow_up") if not record.get(field)
        )
    return tuple(missing)


def publish_volume_cleanup_outcome(
    outcome: TargetVolumeCleanupOutcome,
    *,
    publisher: CleanupOutcomePublisher | None = None,
) -> VolumeCleanupPublication:
    """Publish one complete target outcome under the stable ``ebs-volumes`` name.

    Every per-volume record is verified and the whole outcome is serialized
    before publication. A verification or serialization failure never suppresses
    publication: the best available evidence is still handed to the channel and
    the failure is returned as an unsuccessful reporting status, independent of
    the stack deletion result. A failure raised by the channel itself propagates,
    preserving the existing strict-validation callback contract.
    """
    reporting_error: SafeError | None = None
    try:
        for record in outcome.volumes:
            verify_volume_reporting(record)
        details = outcome.to_dict()
        absent = missing_target_fields(details)
        if absent:
            raise VolumeReportingError(
                f"cleanup outcome is missing required field(s): {', '.join(absent)}"
            )
    except (VolumeReportingError, TypeError, ValueError) as error:
        reporting_error = normalize_safe_error(error)
        details = _fallback_details(outcome, reporting_error)

    published = False
    if publisher is not None:
        publisher(EBS_VOLUME_CLEANUP_NAME, dict(details))
        published = True

    return VolumeCleanupPublication(
        cleanup_name=EBS_VOLUME_CLEANUP_NAME,
        details=details,
        outcome_successful=bool(getattr(outcome, "successful", False)),
        reporting_successful=reporting_error is None,
        published=published,
        reporting_error=reporting_error,
    )


def volume_cleanup_publication_from_details(
    details: Mapping[str, Any],
) -> VolumeCleanupPublication:
    """Rebuild one publication from evidence replayed off the cleanup channel.

    Orchestrated destruction publishes each target outcome through the existing
    ``record_cleanup`` channel, so the command layer receives serialized evidence
    rather than the outcome object the single-stack path holds. Reconstructing the
    publication from that evidence keeps one status derivation for both destroy
    paths: cleanup success is read from the published outcome and reporting
    success is re-derived from the published fields, so incomplete evidence stays
    unsuccessful instead of silently exiting zero.
    """
    try:
        replayed = volume_cleanup_details(details)
    except (VolumeReportingError, TypeError, ValueError) as error:
        unreadable = normalize_safe_error(error)
        return VolumeCleanupPublication(
            cleanup_name=EBS_VOLUME_CLEANUP_NAME,
            details={"reporting_error": _safe_error_details(unreadable)},
            outcome_successful=False,
            reporting_successful=False,
            published=True,
            reporting_error=unreadable,
        )

    reporting_error = _replayed_reporting_error(replayed)
    if reporting_error is None and not derive_reporting_complete(replayed):
        reporting_error = SafeError(
            error_code=None,
            error_type="VolumeReportingError",
            message="the published cleanup outcome is missing required reporting field(s)",
        )
    return VolumeCleanupPublication(
        cleanup_name=EBS_VOLUME_CLEANUP_NAME,
        details=replayed,
        outcome_successful=bool(replayed.get("successful")),
        reporting_successful=reporting_error is None,
        published=True,
        reporting_error=reporting_error,
    )


def _replayed_reporting_error(details: Mapping[str, Any]) -> SafeError | None:
    """Return the reporting failure a published outcome already carries, if any."""
    raw = details.get("reporting_error")
    if not isinstance(raw, Mapping):
        return None
    code = raw.get("error_code")
    message = _text(raw.get("message"), fallback="volume cleanup reporting failed")
    error_type = _text(raw.get("error_type"), fallback="VolumeReportingError")
    try:
        return SafeError(
            error_code=code if isinstance(code, str) and code else None,
            error_type=error_type,
            message=message,
        )
    except ValueError:
        return SafeError(
            error_code=None,
            error_type="VolumeReportingError",
            message=message,
        )


def _safe_error_details(error: SafeError) -> dict[str, JsonValue]:
    return {
        "error_code": error.error_code,
        "error_type": error.error_type,
        "message": error.message,
    }


def _fallback_details(
    outcome: TargetVolumeCleanupOutcome,
    error: SafeError,
) -> dict[str, JsonValue]:
    """Return minimal safe evidence when an outcome cannot serialize completely."""
    status = getattr(outcome, "status", None)
    return {
        "stack_name": str(getattr(outcome, "stack_name", "") or "unknown"),
        "status": status.value if isinstance(status, VolumeCleanupStatus) else None,
        "successful": bool(getattr(outcome, "successful", False)),
        "reporting_error": _safe_error_details(error),
    }


def render_volume_cleanup_publication(
    formatter: CleanupFormatter,
    publication: VolumeCleanupPublication,
) -> None:
    """Render one published outcome and any independent reporting failure."""
    render_volume_cleanup_outcome(formatter, publication.details)
    if publication.reporting_error is not None:
        formatter.print_error(
            f"EBS volume cleanup reporting is incomplete: {publication.reporting_error.message}"
        )
        formatter.print_warning(_REPORTING_FOLLOW_UP)


def render_volume_cleanup_outcome(
    formatter: CleanupFormatter,
    outcome: TargetVolumeCleanupOutcome | Mapping[str, Any],
) -> None:
    """Render one target outcome identically for single and orchestrated destroy.

    Rendering is read-only and never raises for incomplete evidence: a missing
    required field is reported as a reporting problem so the operator still sees
    every record that could be produced.
    """
    details = volume_cleanup_details(outcome)
    records = _volume_records(details)
    machine_readable = _machine_readable(formatter)

    if machine_readable:
        formatter.print(details)
    else:
        _render_target_header(formatter, details)
        for record in records:
            _render_volume_record(record)

    _render_incomplete_fields(formatter, details, records)
    _render_status(formatter, details, machine_readable=machine_readable)
    _render_retained_cost_warning(formatter, details, records)


def _machine_readable(formatter: CleanupFormatter) -> bool:
    output_format = getattr(getattr(formatter, "config", None), "output_format", "table")
    return isinstance(output_format, str) and output_format.lower() in {"json", "yaml"}


def _volume_records(details: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    raw = details.get("volumes")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    records = [dict(item) for item in raw if isinstance(item, Mapping)]
    return tuple(sorted(records, key=lambda record: str(record.get("volume_id", ""))))


def _text(value: Any, *, fallback: str = "unknown") -> str:
    if isinstance(value, str) and value:
        return value
    if value is None:
        return fallback
    return str(value)


def _render_target_header(formatter: CleanupFormatter, details: Mapping[str, Any]) -> None:
    formatter.print_info(
        f"EBS volume cleanup for {_text(details.get('stack_name'))} "
        f"(region: {_text(details.get('target_region'), fallback='unresolved')}, "
        f"cluster: {_text(details.get('target_cluster'), fallback='unresolved')})"
    )
    authorized = "yes" if details.get("deletion_authorized") else "no"
    print(
        f"  policy: {_text(details.get('policy'))} "
        f"(deletion authorized: {authorized}, "
        f"authorization: {_text(details.get('authorization_source'), fallback='none')})"
    )
    print(f"  cluster tag key: {_text(details.get('cluster_tag_key'), fallback='unresolved')}")
    print(f"  status: {_text(details.get('status'))}")
    print(f"  counts: {_counts_summary(details.get('counts'))}")


def _counts_summary(counts: Any) -> str:
    if not isinstance(counts, Mapping):
        return "unavailable"
    return " ".join(
        f"{field.replace('_', '-')}={_text(counts.get(field), fallback='?')}"
        for field in _COUNT_FIELDS
    )


def _render_volume_record(record: Mapping[str, Any]) -> None:
    print(f"  {_text(record.get('volume_id'))}")
    print(
        f"      region={_text(record.get('region'))} "
        f"az={_text(record.get('availability_zone'))} "
        f"size={_text(record.get('size_gib'), fallback='?')}GiB "
        f"state={_text(record.get('observed_state'))} "
        f"tag={_text(record.get('cluster_tag_value'), fallback='unset')} "
        f"attachments={_attachments(record.get('attachment_ids'))}"
    )
    print(
        f"      policy={_text(record.get('policy'))} "
        f"action={_text(record.get('action'))} "
        f"result={_text(record.get('action_result'))}"
    )
    reason = record.get("reason")
    if reason:
        print(
            f"      reason ({_text(record.get('reason_code'), fallback='unspecified')}): {reason}"
        )
    follow_up = record.get("follow_up")
    if follow_up:
        print(f"      follow-up: {follow_up}")
    recheck = record.get("recheck")
    if isinstance(recheck, Mapping):
        print(
            f"      current: state={_text(recheck.get('state'))} "
            f"tag={_text(recheck.get('cluster_tag_value'), fallback='unset')} "
            f"attachments={_attachments(recheck.get('attachment_ids'))}"
        )
    error = record.get("error")
    if isinstance(error, Mapping):
        print(
            f"      error ({_text(error.get('error_code'), fallback=_text(error.get('error_type')))}"
            f"): {_text(error.get('message'))}"
        )


def _attachments(value: Any) -> str:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        identifiers = [str(item) for item in value if str(item)]
        return ",".join(identifiers) if identifiers else "none"
    return "none"


def _render_incomplete_fields(
    formatter: CleanupFormatter,
    details: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> None:
    absent_target = missing_target_fields(details)
    if absent_target:
        formatter.print_error(
            "EBS volume cleanup report is missing required target field(s): "
            f"{', '.join(absent_target)}"
        )
    for record in records:
        absent_record = missing_volume_fields(record)
        if absent_record:
            formatter.print_error(
                f"EBS volume record {_text(record.get('volume_id'))} is missing required "
                f"field(s): {', '.join(absent_record)}"
            )
    reporting_error = details.get("reporting_error")
    if isinstance(reporting_error, Mapping):
        formatter.print_error(
            f"EBS volume cleanup reporting failed: {_text(reporting_error.get('message'))}"
        )


def _render_status(
    formatter: CleanupFormatter,
    details: Mapping[str, Any],
    *,
    machine_readable: bool,
) -> None:
    status = details.get("status")
    stack_name = _text(details.get("stack_name"))
    if status == VolumeCleanupStatus.COMPLETED.value:
        if not machine_readable:
            formatter.print_success(f"EBS volume cleanup completed for {stack_name}")
        return
    if status == VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS.value:
        if not machine_readable:
            formatter.print_success(
                f"EBS volume cleanup completed for {stack_name} with safety-preserved volumes"
            )
        return
    if status == VolumeCleanupStatus.SKIPPED.value:
        formatter.print_warning(
            f"EBS volume cleanup was skipped for {stack_name}: "
            f"{_text(details.get('blocking_reason'), fallback='no reason recorded')} "
            f"[{_text(details.get('blocking_reason_code'), fallback='unspecified')}]"
        )
        follow_up = details.get("follow_up")
        if follow_up:
            formatter.print_warning(f"Follow-up: {follow_up}")
        return
    formatter.print_error(f"EBS volume cleanup failed for {stack_name}")
    error = details.get("error")
    if isinstance(error, Mapping):
        formatter.print_error(f"Cleanup error: {_text(error.get('message'))}")


def _render_retained_cost_warning(
    formatter: CleanupFormatter,
    details: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> None:
    preserved = [
        record for record in records if str(record.get("action", "")) in _PRESERVED_ACTIONS
    ]
    failed = [
        record for record in records if str(record.get("action", "")) == VolumeAction.FAILED.value
    ]
    if failed:
        formatter.print_warning(
            f"{len(failed)} EBS volume(s) could not be processed and may still exist "
            "and incur storage cost: "
            f"{', '.join(_text(record.get('volume_id')) for record in failed)}"
        )
    if not preserved:
        return
    total_gib = sum(
        int(record["size_gib"])
        for record in preserved
        if isinstance(record.get("size_gib"), int) and not isinstance(record["size_gib"], bool)
    )
    region = _text(details.get("target_region"), fallback="the target Region")
    formatter.print_warning(
        f"{len(preserved)} EBS volume(s) totaling {total_gib} GiB remain in {region} "
        f"and continue to incur storage cost; the "
        f"{_text(details.get('policy'), fallback='selected')} volume policy of this "
        f"destroy command preserved them."
    )
    for record in preserved:
        formatter.print_warning(
            f"  retained {_text(record.get('volume_id'))} "
            f"({_text(record.get('size_gib'), fallback='?')} GiB, "
            f"reason: {_text(record.get('reason_code'), fallback='unspecified')})"
        )


# ---------------------------------------------------------------------------
# Command-level cleanup status and exit mapping
# ---------------------------------------------------------------------------

#: Exit status returned when the command succeeded, matching existing behavior.
EXIT_SUCCESS = 0

#: Exit status returned for any unsuccessful destroy command, unchanged from the
#: existing stack-only behavior.
EXIT_FAILURE = 1

_COMPLETED_STATUSES = frozenset(
    {
        VolumeCleanupStatus.COMPLETED.value,
        VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS.value,
    }
)


class VolumeCleanupExitReason(StrEnum):
    """Stable machine-readable reason one target made the command unsuccessful."""

    CLEANUP_FAILED = "cleanup-failed"
    CLEANUP_BLOCKED = "cleanup-blocked"
    OWNED_VOLUME_REMAINS = "owned-volume-remains"
    VOLUME_FAILED = "volume-failed"
    REPORTING_INCOMPLETE = "reporting-incomplete"
    STATUS_DISAGREEMENT = "status-disagreement"
    UNREADABLE_OUTCOME = "unreadable-outcome"


#: Reasons that describe the cleanup result rather than its reporting.
_CLEANUP_REASONS = frozenset(
    {
        VolumeCleanupExitReason.CLEANUP_FAILED,
        VolumeCleanupExitReason.CLEANUP_BLOCKED,
        VolumeCleanupExitReason.OWNED_VOLUME_REMAINS,
        VolumeCleanupExitReason.VOLUME_FAILED,
        VolumeCleanupExitReason.STATUS_DISAGREEMENT,
        VolumeCleanupExitReason.UNREADABLE_OUTCOME,
    }
)

_EXIT_REASON_MESSAGES: Mapping[VolumeCleanupExitReason, str] = {
    VolumeCleanupExitReason.CLEANUP_FAILED: "volume cleanup failed",
    VolumeCleanupExitReason.CLEANUP_BLOCKED: "volume cleanup was blocked before any EBS request",
    VolumeCleanupExitReason.OWNED_VOLUME_REMAINS: (
        "an owned volume remains after authorized deletion"
    ),
    VolumeCleanupExitReason.VOLUME_FAILED: "at least one volume could not be processed",
    VolumeCleanupExitReason.REPORTING_INCOMPLETE: "the cleanup report is missing required fields",
    VolumeCleanupExitReason.STATUS_DISAGREEMENT: (
        "the recorded and re-derived cleanup statuses disagree"
    ),
    VolumeCleanupExitReason.UNREADABLE_OUTCOME: "the cleanup outcome status is unreadable",
}


@dataclass(frozen=True)
class VolumeCleanupTargetStatus:
    """Command-level volume-cleanup status for one exact regional target."""

    stack_name: str
    cleanup_successful: bool
    reporting_successful: bool
    reasons: tuple[VolumeCleanupExitReason, ...] = ()

    def __post_init__(self) -> None:
        if not self.stack_name:
            raise ValueError("a cleanup status requires the target stack name")
        normalized = tuple(dict.fromkeys(self.reasons))
        if any(not isinstance(reason, VolumeCleanupExitReason) for reason in normalized):
            raise ValueError("cleanup status reasons must be known exit reasons")
        object.__setattr__(self, "reasons", normalized)
        expected_cleanup = not any(reason in _CLEANUP_REASONS for reason in normalized)
        expected_reporting = VolumeCleanupExitReason.REPORTING_INCOMPLETE not in normalized
        if self.cleanup_successful != expected_cleanup:
            raise ValueError("cleanup success must agree with the recorded cleanup reasons")
        if self.reporting_successful != expected_reporting:
            raise ValueError("reporting success must agree with the recorded reporting reason")

    @property
    def successful(self) -> bool:
        """Whether this target's cleanup and its required reporting both succeeded."""
        return self.cleanup_successful and self.reporting_successful


@dataclass(frozen=True)
class VolumeCleanupCommandResult:
    """Aggregate volume-cleanup status for one destroy command invocation."""

    targets: tuple[VolumeCleanupTargetStatus, ...] = ()

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.targets, key=lambda status: status.stack_name))
        if len({status.stack_name for status in ordered}) != len(ordered):
            raise ValueError("a command result cannot contain duplicate target stacks")
        object.__setattr__(self, "targets", ordered)

    @property
    def attempted(self) -> bool:
        """Whether volume cleanup produced an outcome for at least one target."""
        return bool(self.targets)

    @property
    def cleanup_successful(self) -> bool:
        """Whether every target's cleanup succeeded."""
        return all(status.cleanup_successful for status in self.targets)

    @property
    def reporting_successful(self) -> bool:
        """Whether every target reported every required field."""
        return all(status.reporting_successful for status in self.targets)

    @property
    def successful(self) -> bool:
        """Whether every target's cleanup and reporting both succeeded."""
        return self.cleanup_successful and self.reporting_successful

    @property
    def unsuccessful_targets(self) -> tuple[VolumeCleanupTargetStatus, ...]:
        """Deterministically ordered targets that make the command unsuccessful."""
        return tuple(status for status in self.targets if not status.successful)

    @property
    def reasons(self) -> tuple[VolumeCleanupExitReason, ...]:
        """Every distinct unsuccessful reason across all targets, in stable order."""
        collected = [reason for status in self.targets for reason in status.reasons]
        return tuple(sorted(dict.fromkeys(collected), key=lambda reason: reason.value))


def derive_cleanup_exit_reasons(details: Mapping[str, Any]) -> tuple[VolumeCleanupExitReason, ...]:
    """Re-derive the documented cleanup exit reasons from one serialized outcome.

    The derivation reads only published evidence, so it stays valid for the
    single-stack path and for outcomes replayed from the orchestrated cleanup
    channel. Already-absent records are authoritative successes and never appear
    here; non-owned tagged volumes are safely preserved and do not by themselves
    make authorized deletion unsuccessful.
    """
    reasons: list[VolumeCleanupExitReason] = []
    status = details.get("status")
    if status == VolumeCleanupStatus.FAILED.value:
        reasons.append(VolumeCleanupExitReason.CLEANUP_FAILED)
    elif status == VolumeCleanupStatus.SKIPPED.value:
        reasons.append(VolumeCleanupExitReason.CLEANUP_BLOCKED)
    elif status not in _COMPLETED_STATUSES:
        reasons.append(VolumeCleanupExitReason.UNREADABLE_OUTCOME)

    records = _volume_records(details)
    if any(record.get("action") == VolumeAction.FAILED.value for record in records):
        reasons.append(VolumeCleanupExitReason.VOLUME_FAILED)

    authorized_delete = details.get("policy") == VolumePolicy.DELETE.value and bool(
        details.get("deletion_authorized")
    )
    if authorized_delete and any(
        record.get("cluster_tag_value") == OWNED_CLUSTER_TAG_VALUE
        and str(record.get("action", "")) in _PRESERVED_ACTIONS
        for record in records
    ):
        reasons.append(VolumeCleanupExitReason.OWNED_VOLUME_REMAINS)
    return tuple(dict.fromkeys(reasons))


def derive_reporting_complete(details: Mapping[str, Any]) -> bool:
    """Return whether one serialized outcome carries every required field."""
    if missing_target_fields(details):
        return False
    return not any(missing_volume_fields(record) for record in _volume_records(details))


def evaluate_target_cleanup_status(
    publication: VolumeCleanupPublication,
) -> VolumeCleanupTargetStatus:
    """Map one published outcome to this target's command-level status.

    Both the status recorded by the cleanup service and the status re-derived
    from the published records must agree. A disagreement is treated as
    unsuccessful so no layer can weaken the documented rules on its own.
    """
    details = publication.details
    reasons = list(derive_cleanup_exit_reasons(details))
    if publication.outcome_successful != (not reasons):
        reasons.append(VolumeCleanupExitReason.STATUS_DISAGREEMENT)
    if not publication.reporting_successful or not derive_reporting_complete(details):
        reasons.append(VolumeCleanupExitReason.REPORTING_INCOMPLETE)
    normalized = tuple(dict.fromkeys(reasons))
    return VolumeCleanupTargetStatus(
        stack_name=_text(details.get("stack_name")),
        cleanup_successful=not any(reason in _CLEANUP_REASONS for reason in normalized),
        reporting_successful=(VolumeCleanupExitReason.REPORTING_INCOMPLETE not in normalized),
        reasons=normalized,
    )


def evaluate_volume_cleanup_result(
    publications: Iterable[VolumeCleanupPublication],
) -> VolumeCleanupCommandResult:
    """Aggregate every published target outcome into one command-level result.

    Targets stay isolated: each contributes its own status and reasons, and a
    command with no cleanup outcome at all is successful, which keeps non-regional
    and cleanup-free invocations on their existing exit path.
    """
    return VolumeCleanupCommandResult(
        targets=tuple(evaluate_target_cleanup_status(item) for item in publications)
    )


def destroy_command_exit_code(
    *,
    stack_successful: bool,
    cleanup: VolumeCleanupCommandResult | None = None,
) -> int:
    """Map the stack result and volume-cleanup result to the command exit status.

    Stack failure remains unsuccessful under the existing rules, unchanged by
    volume cleanup. For a stack result that already succeeded, an unsuccessful
    volume-cleanup or reporting status is the only new source of a non-zero exit.
    """
    if not stack_successful:
        return EXIT_FAILURE
    if cleanup is not None and not cleanup.successful:
        return EXIT_FAILURE
    return EXIT_SUCCESS


def render_volume_cleanup_command_result(
    formatter: CleanupFormatter,
    result: VolumeCleanupCommandResult,
) -> None:
    """Render why volume cleanup made the command unsuccessful, if it did.

    Per-target evidence is already rendered by the shared renderer; this adds only
    the command-level conclusion so the operator sees one authoritative summary.
    """
    if not result.attempted:
        return
    unsuccessful = result.unsuccessful_targets
    if not unsuccessful:
        formatter.print_success(f"EBS volume cleanup succeeded for {len(result.targets)} target(s)")
        return
    formatter.print_error(
        f"EBS volume cleanup was unsuccessful for {len(unsuccessful)} of "
        f"{len(result.targets)} target(s)"
    )
    for status in unsuccessful:
        detail = ", ".join(
            f"{reason.value} ({_EXIT_REASON_MESSAGES[reason]})" for reason in status.reasons
        )
        formatter.print_error(f"  {status.stack_name}: {detail}")
