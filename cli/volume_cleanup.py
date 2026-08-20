"""Safe policy and authorization models for regional EBS volume cleanup."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from botocore.exceptions import ClientError

from gco.stacks.constants import (
    cloudformation_region_partitions,
    validated_deployment_partition,
    validated_regional_deployment_regions,
)

#: Exact cluster-tag value that proves GCO owns one discovered volume. Ownership
#: is never inferred from any other value, casing, or absence of the tag.
OWNED_CLUSTER_TAG_VALUE = "owned"


class TargetResolutionKind(StrEnum):
    """Result of resolving a stack into the regional cleanup boundary."""

    TARGET = "target"
    NOT_REGIONAL = "not-regional"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class RegionalVolumeTarget:
    """Exact regional stack and Kubernetes cluster identity used for cleanup."""

    stack_name: str
    stack_id: str | None
    region: str
    cluster_name: str
    cluster_tag_key: str


@dataclass(frozen=True)
class TargetResolution:
    """Explicit target, non-regional, or fail-closed targeting result."""

    kind: TargetResolutionKind
    target: RegionalVolumeTarget | None = None
    reason_code: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.kind is TargetResolutionKind.TARGET:
            if self.target is None or self.reason_code is not None or self.reason is not None:
                raise ValueError("target resolution must contain only an exact target")
        elif self.target is not None or not self.reason_code or not self.reason:
            raise ValueError("non-target resolution requires a reason and no target")


def _non_regional(reason: str) -> TargetResolution:
    return TargetResolution(
        kind=TargetResolutionKind.NOT_REGIONAL,
        reason_code="stack-is-not-configured-regional",
        reason=reason,
    )


def _blocked(reason_code: str, reason: str) -> TargetResolution:
    return TargetResolution(
        kind=TargetResolutionKind.BLOCKED,
        reason_code=reason_code,
        reason=reason,
    )


def _stack_arn_matches(
    stack_id: str,
    *,
    partition: str,
    region: str,
    stack_name: str,
) -> bool:
    parts = stack_id.split(":", 5)
    if len(parts) != 6:
        return False
    arn, actual_partition, service, actual_region, account, resource = parts
    resource_parts = resource.split("/")
    return (
        arn == "arn"
        and actual_partition == partition
        and service == "cloudformation"
        and actual_region == region
        and len(account) == 12
        and account.isdigit()
        and len(resource_parts) == 3
        and resource_parts[0] == "stack"
        and resource_parts[1] == stack_name
        and bool(resource_parts[2])
    )


def resolve_regional_volume_target(
    *,
    project_name: str,
    stack_name: str,
    configured_regions: Collection[str],
    stack_id: str | None = None,
    strict: bool = False,
    strict_resource: Mapping[str, str] | None = None,
    region_partitions: Mapping[str, str] | None = None,
) -> TargetResolution:
    """Resolve an exact configured regional stack without making AWS calls.

    Ordinary resolution authorizes only the exact configured stack name. Strict
    resolution additionally requires the pre-destroy record produced from the
    authorized CloudFormation stack ARN and its single EKS physical ID.
    """
    raw_regions = list(configured_regions)
    matching_regions = [
        region
        for region in raw_regions
        if isinstance(region, str) and stack_name == f"{project_name}-{region}"
    ]
    if len(matching_regions) != 1:
        return _non_regional(f"Stack {stack_name!r} is not one exact configured regional stack")

    metadata = (
        cloudformation_region_partitions() if region_partitions is None else region_partitions
    )
    try:
        regions = validated_regional_deployment_regions(
            raw_regions,
            known_regions=metadata,
        )
        partition = validated_deployment_partition(
            regions,
            region_partitions=metadata,
        )
    except (RuntimeError, ValueError) as exc:
        return _blocked(
            "invalid-regional-configuration",
            f"Cannot authorize regional volume targeting: {exc}",
        )

    region = matching_regions[0]
    if region not in regions or metadata.get(region) != partition:
        return _blocked(
            "region-partition-mismatch",
            f"Region {region!r} is not valid for deployment partition {partition!r}",
        )

    resolved_stack_id = stack_id
    if strict:
        if strict_resource is None:
            return _blocked(
                "missing-strict-resource-identity",
                f"Strict targeting has no pre-destroy identity for {stack_name}",
            )
        identity_error = strict_resource.get("cluster_identity_error")
        if identity_error:
            return _blocked(
                "strict-cluster-identity-unresolved",
                f"Strict targeting cannot establish the EKS identity: {identity_error}",
            )
        if strict_resource.get("stack_name") != stack_name:
            return _blocked(
                "strict-stack-name-mismatch",
                f"Strict identity does not authorize stack {stack_name}",
            )
        if strict_resource.get("region") != region:
            return _blocked(
                "strict-region-mismatch",
                f"Strict identity does not authorize Region {region}",
            )
        if strict_resource.get("cluster_name") != stack_name:
            return _blocked(
                "strict-cluster-name-mismatch",
                f"Strict identity does not authorize cluster {stack_name}",
            )
        strict_stack_id = strict_resource.get("stack_id")
        if not strict_stack_id:
            return _blocked(
                "missing-strict-stack-arn",
                f"Strict identity has no CloudFormation stack ARN for {stack_name}",
            )
        if stack_id is not None and strict_stack_id != stack_id:
            return _blocked(
                "strict-stack-arn-mismatch",
                f"Strict identity changed for stack {stack_name}",
            )
        resolved_stack_id = strict_stack_id

    if resolved_stack_id is not None and not _stack_arn_matches(
        resolved_stack_id,
        partition=partition,
        region=region,
        stack_name=stack_name,
    ):
        return _blocked(
            "invalid-stack-arn",
            f"Stack identity is not an exact CloudFormation ARN for {region}:{stack_name}",
        )

    return TargetResolution(
        kind=TargetResolutionKind.TARGET,
        target=RegionalVolumeTarget(
            stack_name=stack_name,
            stack_id=resolved_stack_id,
            region=region,
            cluster_name=stack_name,
            cluster_tag_key=f"kubernetes.io/cluster/{stack_name}",
        ),
    )


class ClientFactory(Protocol):
    """Create one AWS service client in an explicitly selected Region."""

    def __call__(self, service_name: str, *, region_name: str) -> Any:
        """Return a client for ``service_name`` in exactly ``region_name``."""


@dataclass(frozen=True)
class ClusterAbsenceProof:
    """Evidence that one exact target cluster was absent at a UTC instant."""

    stack_name: str
    region: str
    cluster_name: str
    verified_at: str

    def __post_init__(self) -> None:
        if not self.stack_name or not self.region or not self.cluster_name:
            raise ValueError("cluster-absence proof requires complete target identity")
        try:
            moment = datetime.fromisoformat(self.verified_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValueError("cluster-absence proof requires an ISO-8601 timestamp") from exc
        if moment.tzinfo is None or moment.utcoffset() != UTC.utcoffset(moment):
            raise ValueError("cluster-absence proof timestamp must be in UTC")

    def matches(self, target: RegionalVolumeTarget) -> bool:
        """Return whether this evidence is bound to the complete target identity."""
        return (
            self.stack_name == target.stack_name
            and self.region == target.region
            and self.cluster_name == target.cluster_name
        )

    def require_matches(self, target: RegionalVolumeTarget) -> None:
        """Reject evidence produced for any other stack, Region, or cluster."""
        if not self.matches(target):
            raise ValueError("cluster-absence proof does not match the cleanup target")


class ClusterAbsenceStatus(StrEnum):
    """Fail-closed result of checking one exact EKS cluster identity."""

    VERIFIED_ABSENT = "verified-absent"
    PRESENT = "present"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ClusterAbsenceVerification:
    """Verified evidence or a safe reason that EBS work must remain blocked."""

    status: ClusterAbsenceStatus
    proof: ClusterAbsenceProof | None = None
    reason_code: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is ClusterAbsenceStatus.VERIFIED_ABSENT:
            if self.proof is None or self.reason_code is not None or self.reason is not None:
                raise ValueError("verified absence must contain only a proof")
        elif self.proof is not None or not self.reason_code or not self.reason:
            raise ValueError("non-absent verification requires a blocking reason")

    @property
    def verified_absent(self) -> bool:
        """Return whether exact not-found evidence is available."""
        return self.status is ClusterAbsenceStatus.VERIFIED_ABSENT

    def proof_for(self, target: RegionalVolumeTarget) -> ClusterAbsenceProof:
        """Return evidence only when it is verified and bound to ``target``."""
        if self.proof is None:
            raise ValueError(self.reason or "cluster absence is not verified")
        self.proof.require_matches(target)
        return self.proof


_AUTHORIZATION_ERROR_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "InvalidClientTokenId",
        "NotAuthorizedException",
        "UnauthorizedException",
        "UnrecognizedClientException",
    }
)
_THROTTLING_ERROR_CODES = frozenset(
    {
        "RequestLimitExceeded",
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
    }
)


class ClusterAbsenceVerifier:
    """Verify exact EKS absence after definitive stack deletion, or block safely."""

    def __init__(
        self,
        client_factory: ClientFactory,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _blocked(reason_code: str, reason: str) -> ClusterAbsenceVerification:
        return ClusterAbsenceVerification(
            status=ClusterAbsenceStatus.BLOCKED,
            reason_code=reason_code,
            reason=reason,
        )

    def _verified_absence(self, target: RegionalVolumeTarget) -> ClusterAbsenceVerification:
        try:
            moment = self._clock()
            if moment.tzinfo is None:
                raise ValueError("clock returned a timestamp without a timezone")
            verified_at = moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
            proof = ClusterAbsenceProof(
                stack_name=target.stack_name,
                region=target.region,
                cluster_name=target.cluster_name,
                verified_at=verified_at,
            )
        except Exception as exc:
            return self._blocked(
                "cluster-verification-time-invalid",
                f"Could not bind cluster-absence verification time: {type(exc).__name__}",
            )
        return ClusterAbsenceVerification(
            status=ClusterAbsenceStatus.VERIFIED_ABSENT,
            proof=proof,
        )

    def verify(
        self,
        *,
        target: RegionalVolumeTarget,
        stack_deleted: bool,
    ) -> ClusterAbsenceVerification:
        """Describe the exact cluster only after stack deletion is definitive."""
        if stack_deleted is not True:
            return self._blocked(
                "stack-deletion-unverified",
                f"Stack deletion is not definitive for {target.stack_name}",
            )

        try:
            eks = self._client_factory("eks", region_name=target.region)
        except Exception as exc:
            return self._blocked(
                "eks-client-creation-failed",
                f"Could not create the exact-Region EKS client: {type(exc).__name__}",
            )

        try:
            response = eks.describe_cluster(name=target.cluster_name)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            error_code = error.get("Code") if isinstance(error, Mapping) else None
            if error_code == "ResourceNotFoundException":
                return self._verified_absence(target)
            if error_code in _AUTHORIZATION_ERROR_CODES:
                reason_code = "cluster-verification-unauthorized"
            elif error_code in _THROTTLING_ERROR_CODES:
                reason_code = "cluster-verification-throttled"
            else:
                reason_code = "cluster-verification-error"
            safe_code = error_code if isinstance(error_code, str) else type(exc).__name__
            return self._blocked(
                reason_code,
                f"EKS could not verify cluster absence ({safe_code})",
            )
        except Exception as exc:
            return self._blocked(
                "cluster-verification-error",
                f"EKS could not verify cluster absence ({type(exc).__name__})",
            )

        if not isinstance(response, Mapping):
            return self._blocked(
                "cluster-response-malformed",
                "EKS returned a non-object DescribeCluster response",
            )
        cluster = response.get("cluster")
        if not isinstance(cluster, Mapping):
            return self._blocked(
                "cluster-response-malformed",
                "EKS DescribeCluster response has no cluster object",
            )
        returned_name = cluster.get("name")
        if not isinstance(returned_name, str) or not returned_name:
            return self._blocked(
                "cluster-response-malformed",
                "EKS DescribeCluster response has no valid cluster name",
            )
        if returned_name != target.cluster_name:
            return self._blocked(
                "cluster-identity-mismatch",
                "EKS returned a cluster identity different from the exact target",
            )
        return ClusterAbsenceVerification(
            status=ClusterAbsenceStatus.PRESENT,
            reason_code="cluster-still-present",
            reason=f"Target EKS cluster {target.cluster_name} is still present",
        )


class DestroyCommandKind(StrEnum):
    """Destroy command whose semantics determine the default volume policy."""

    SINGLE = "destroy"
    ALL = "destroy-all"


class VolumePolicy(StrEnum):
    """Disposition requested for dynamically provisioned cluster volumes."""

    RETAIN = "retain"
    DELETE = "delete"


class DeletionAuthorizationSource(StrEnum):
    """Operator action that authorized irreversible volume deletion."""

    NONE = "none"
    INTERACTIVE_VOLUME_CONFIRMATION = "interactive-volume-confirmation"
    EXPLICIT_DELETE_WITH_YES = "explicit-delete-with-yes"
    DESTROY_ALL_WITH_YES = "destroy-all-with-yes"


class VolumePolicyConflictError(ValueError):
    """Raised when mutually exclusive retain and delete policies are selected."""


@dataclass(frozen=True)
class VolumeCleanupRequest:
    """Final volume-cleanup policy and its deletion authorization."""

    policy: VolumePolicy
    deletion_authorized: bool
    authorization_source: DeletionAuthorizationSource

    def __post_init__(self) -> None:
        if self.policy is VolumePolicy.RETAIN and self.deletion_authorized:
            raise ValueError("retain policy cannot authorize volume deletion")
        if (
            self.deletion_authorized
            and self.authorization_source is DeletionAuthorizationSource.NONE
        ):
            raise ValueError("authorized deletion requires an authorization source")
        if (
            not self.deletion_authorized
            and self.authorization_source is not DeletionAuthorizationSource.NONE
        ):
            raise ValueError("an authorization source requires authorized deletion")


@dataclass(frozen=True)
class VolumeCleanupDecision:
    """Pure resolver result, including whether the CLI must obtain confirmation."""

    policy: VolumePolicy
    deletion_authorized: bool
    authorization_source: DeletionAuthorizationSource
    requires_volume_confirmation: bool

    def __post_init__(self) -> None:
        pending_interactive_delete = (
            self.policy is VolumePolicy.DELETE and not self.deletion_authorized
        )
        if self.requires_volume_confirmation != pending_interactive_delete:
            raise ValueError(
                "volume confirmation is required exactly for an unauthorized delete policy"
            )
        VolumeCleanupRequest(
            policy=self.policy,
            deletion_authorized=self.deletion_authorized,
            authorization_source=self.authorization_source,
        )

    @property
    def request(self) -> VolumeCleanupRequest:
        """Return the final request when no interactive authorization is pending."""
        if self.requires_volume_confirmation:
            raise ValueError("interactive volume-deletion confirmation is still required")
        return VolumeCleanupRequest(
            policy=self.policy,
            deletion_authorized=self.deletion_authorized,
            authorization_source=self.authorization_source,
        )

    def confirm_volume_deletion(self) -> VolumeCleanupRequest:
        """Convert an affirmative dedicated volume prompt into authorization."""
        if not self.requires_volume_confirmation:
            raise ValueError("this volume-cleanup decision does not require confirmation")
        return VolumeCleanupRequest(
            policy=VolumePolicy.DELETE,
            deletion_authorized=True,
            authorization_source=DeletionAuthorizationSource.INTERACTIVE_VOLUME_CONFIRMATION,
        )


def resolve_volume_cleanup_request(
    *,
    command: DestroyCommandKind,
    retain_volumes: bool,
    delete_volumes: bool,
    yes: bool,
) -> VolumeCleanupDecision:
    """Resolve command options without consulting stack-manager execution flags.

    Explicit retention takes precedence over the implicit authorized-delete
    behavior of ``destroy-all --yes``. Explicit delete without ``--yes`` is
    returned as a pending decision so the command layer can obtain the dedicated
    irreversible-data confirmation before constructing a cleanup request.
    """
    if not isinstance(command, DestroyCommandKind):
        raise ValueError(f"unsupported destroy command kind: {command!r}")
    if retain_volumes and delete_volumes:
        raise VolumePolicyConflictError(
            "--retain-volumes and --delete-volumes cannot be used together"
        )
    if retain_volumes:
        return VolumeCleanupDecision(
            policy=VolumePolicy.RETAIN,
            deletion_authorized=False,
            authorization_source=DeletionAuthorizationSource.NONE,
            requires_volume_confirmation=False,
        )
    if delete_volumes:
        if yes:
            return VolumeCleanupDecision(
                policy=VolumePolicy.DELETE,
                deletion_authorized=True,
                authorization_source=DeletionAuthorizationSource.EXPLICIT_DELETE_WITH_YES,
                requires_volume_confirmation=False,
            )
        return VolumeCleanupDecision(
            policy=VolumePolicy.DELETE,
            deletion_authorized=False,
            authorization_source=DeletionAuthorizationSource.NONE,
            requires_volume_confirmation=True,
        )
    if command is DestroyCommandKind.ALL and yes:
        return VolumeCleanupDecision(
            policy=VolumePolicy.DELETE,
            deletion_authorized=True,
            authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
            requires_volume_confirmation=False,
        )
    return VolumeCleanupDecision(
        policy=VolumePolicy.RETAIN,
        deletion_authorized=False,
        authorization_source=DeletionAuthorizationSource.NONE,
        requires_volume_confirmation=False,
    )


type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


class VolumeAction(StrEnum):
    """Stable terminal action recorded for one discovered volume."""

    RETAINED = "retained"
    SKIPPED = "skipped"
    DELETE_REQUESTED = "delete-requested"
    ALREADY_ABSENT = "already-absent"
    FAILED = "failed"


class VolumeActionResult(StrEnum):
    """Stable result classification for one volume action."""

    SUCCESS = "success"
    SAFETY_PRESERVED = "safety-preserved"
    IDEMPOTENT_SUCCESS = "idempotent-success"
    BLOCKED = "blocked"
    ERROR = "error"


class VolumeReasonCode(StrEnum):
    """Stable machine-readable reasons used by volume cleanup records."""

    RETAIN_POLICY = "retain-policy"
    OWNERSHIP_SAFETY = "ownership-safety"
    STATE_NOT_AVAILABLE = "state-not-available"
    ATTACHMENTS_PRESENT = "attachments-present"
    SAFETY_RECHECK_CHANGED = "safety-recheck-changed"
    DELETE_REQUEST_ACCEPTED = "delete-request-accepted"
    ALREADY_ABSENT = "already-absent"
    NORMALIZATION_ERROR = "normalization-error"
    EVALUATION_ERROR = "evaluation-error"
    RECHECK_ERROR = "recheck-error"
    DELETE_ERROR = "delete-error"
    REPORTING_ERROR = "reporting-error"


class VolumeCleanupStatus(StrEnum):
    """Stable aggregate status for one exact regional cleanup target."""

    COMPLETED = "completed"
    COMPLETED_WITH_SAFETY_RETENTIONS = "completed-with-safety-retentions"
    SKIPPED = "skipped"
    FAILED = "failed"


_ACCESS_KEY_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_PATTERN = re.compile(
    r"(?i)\b(password|secret(?:_access_key)?|session_token|access_token)\s*[:=]\s*[^\s,;]+"
)
_ERROR_CODE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _safe_error_message(value: object, *, fallback: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    message = " ".join(value.split())[:500]
    message = _ACCESS_KEY_PATTERN.sub("[REDACTED]", message)
    message = _BEARER_PATTERN.sub("Bearer [REDACTED]", message)
    return _SECRET_PATTERN.sub(r"\1=[REDACTED]", message)


@dataclass(frozen=True)
class SafeError:
    """Serializable error metadata with no raw exception or AWS response body."""

    error_code: str | None
    error_type: str
    message: str

    def __post_init__(self) -> None:
        if self.error_code is not None and not _ERROR_CODE_PATTERN.fullmatch(self.error_code):
            raise ValueError("safe error code has an invalid format")
        if not self.error_type or not self.message:
            raise ValueError("safe error metadata requires a type and message")


def normalize_safe_error(error: BaseException) -> SafeError:
    """Normalize an exception without retaining credentials or raw response data."""
    error_type = type(error).__name__
    error_code: str | None = None
    message: object = str(error)
    if isinstance(error, ClientError):
        response_error = error.response.get("Error", {})
        if isinstance(response_error, Mapping):
            candidate_code = response_error.get("Code")
            if isinstance(candidate_code, str) and _ERROR_CODE_PATTERN.fullmatch(candidate_code):
                error_code = candidate_code
            message = response_error.get("Message")
    fallback = f"Operation failed with {error_type}"
    return SafeError(
        error_code=error_code,
        error_type=error_type,
        message=_safe_error_message(message, fallback=fallback),
    )


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_non_negative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def _sorted_unique_ids(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized = tuple(values)
    if any(not isinstance(value, str) or not value for value in normalized):
        raise ValueError(f"{field_name} must contain only non-empty strings")
    return tuple(sorted(set(normalized)))


@dataclass(frozen=True)
class VolumeSnapshot:
    """Normalized immutable EBS facts used for safety decisions and evidence."""

    volume_id: str
    region: str
    availability_zone: str
    size_gib: int
    state: str
    cluster_tag_value: str | None
    attachment_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in ("volume_id", "region", "availability_zone", "state"):
            _require_text(getattr(self, field_name), field_name)
        _require_non_negative_int(self.size_gib, "size_gib")
        if self.cluster_tag_value is not None and not isinstance(self.cluster_tag_value, str):
            raise ValueError("cluster_tag_value must be a string or null")
        object.__setattr__(
            self,
            "attachment_ids",
            _sorted_unique_ids(self.attachment_ids, "attachment_ids"),
        )


class VolumeNormalizationError(ValueError):
    """Raised when an in-scope AWS volume DTO cannot be normalized safely."""

    reason_code = VolumeReasonCode.NORMALIZATION_ERROR


_VOLUME_ID_PATTERN = re.compile(r"^vol-[A-Za-z0-9]+$")
_INSTANCE_ID_PATTERN = re.compile(r"^i-[A-Za-z0-9]+$")
_AZ_SUFFIX_PATTERN = re.compile(r"^(?:[a-z]|-[a-z0-9]+(?:-[a-z0-9]+)*)$")


def _dto_identifier(
    value: object,
    *,
    field_name: str,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise VolumeNormalizationError(f"{field_name} is malformed")
    return value


def normalize_volume_snapshot(
    volume: Mapping[str, object],
    *,
    target: RegionalVolumeTarget,
) -> VolumeSnapshot | None:
    """Normalize one EC2 ``DescribeVolumes`` DTO within an exact target scope.

    ``None`` means the DTO is outside the target boundary and must not enter
    discovered counts or actions. Malformed or ambiguous in-scope DTOs raise a
    machine-classifiable normalization error so callers can fail closed while
    continuing with other volumes.
    """
    if not isinstance(volume, Mapping):
        raise VolumeNormalizationError("volume DTO must be an object")
    _require_text(target.region, "target.region")
    _require_text(target.cluster_tag_key, "target.cluster_tag_key")

    volume_id = _dto_identifier(
        volume.get("VolumeId"),
        field_name="VolumeId",
        pattern=_VOLUME_ID_PATTERN,
    )
    availability_zone = _dto_identifier(
        volume.get("AvailabilityZone"),
        field_name="AvailabilityZone",
    )

    raw_tags = volume.get("Tags")
    if not isinstance(raw_tags, list):
        raise VolumeNormalizationError("Tags must be a list")
    tags: dict[str, str] = {}
    for raw_tag in raw_tags:
        if not isinstance(raw_tag, Mapping):
            raise VolumeNormalizationError("each tag must be an object")
        key = raw_tag.get("Key")
        if not isinstance(key, str) or not key:
            raise VolumeNormalizationError("tag Key is malformed")
        value = raw_tag.get("Value")
        if not isinstance(value, str):
            raise VolumeNormalizationError("tag Value must be a string")
        if key in tags:
            qualifier = "conflicting" if tags[key] != value else "duplicate"
            raise VolumeNormalizationError(f"{qualifier} tag key {key!r}")
        tags[key] = value

    if target.cluster_tag_key not in tags:
        return None
    if not availability_zone.startswith(target.region):
        return None
    zone_suffix = availability_zone[len(target.region) :]
    if _AZ_SUFFIX_PATTERN.fullmatch(zone_suffix) is None:
        return None

    size_gib = volume.get("Size")
    if isinstance(size_gib, bool) or not isinstance(size_gib, int) or size_gib < 0:
        raise VolumeNormalizationError("Size must be a non-negative integer")
    state = _dto_identifier(volume.get("State"), field_name="State")

    raw_attachments = volume.get("Attachments")
    if not isinstance(raw_attachments, list):
        raise VolumeNormalizationError("Attachments must be a list")
    attachment_ids: list[str] = []
    seen_attachment_ids: set[str] = set()
    for raw_attachment in raw_attachments:
        if not isinstance(raw_attachment, Mapping):
            raise VolumeNormalizationError("each attachment must be an object")
        attached_volume_id = raw_attachment.get("VolumeId")
        if attached_volume_id is not None and attached_volume_id != volume_id:
            raise VolumeNormalizationError(
                "attachment VolumeId conflicts with the containing volume"
            )
        instance_id = _dto_identifier(
            raw_attachment.get("InstanceId"),
            field_name="attachment InstanceId",
            pattern=_INSTANCE_ID_PATTERN,
        )
        if instance_id in seen_attachment_ids:
            raise VolumeNormalizationError(f"duplicate attachment identifier {instance_id!r}")
        seen_attachment_ids.add(instance_id)
        attachment_ids.append(instance_id)

    return VolumeSnapshot(
        volume_id=volume_id,
        region=target.region,
        availability_zone=availability_zone,
        size_gib=size_gib,
        state=state,
        cluster_tag_value=tags[target.cluster_tag_key],
        attachment_ids=tuple(sorted(attachment_ids)),
    )


@dataclass(frozen=True)
class VolumeOutcome:
    """Complete terminal reporting record for one in-scope cluster volume."""

    volume_id: str
    region: str
    availability_zone: str
    size_gib: int
    observed_state: str
    cluster_tag_value: str | None
    attachment_ids: tuple[str, ...]
    policy: VolumePolicy
    action: VolumeAction
    action_result: VolumeActionResult
    reason_code: VolumeReasonCode | None = None
    reason: str | None = None
    follow_up: str | None = None
    recheck: VolumeSnapshot | None = None
    error: SafeError | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "volume_id",
            "region",
            "availability_zone",
            "observed_state",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_non_negative_int(self.size_gib, "size_gib")
        if self.cluster_tag_value is not None and not isinstance(self.cluster_tag_value, str):
            raise ValueError("cluster_tag_value must be a string or null")
        object.__setattr__(
            self,
            "attachment_ids",
            _sorted_unique_ids(self.attachment_ids, "attachment_ids"),
        )
        reason_fields = (self.reason_code, self.reason, self.follow_up)
        if any(value is not None for value in reason_fields) and not all(
            value is not None for value in reason_fields
        ):
            raise ValueError("reason code, reason, and follow-up must be provided together")
        if self.action in {
            VolumeAction.RETAINED,
            VolumeAction.SKIPPED,
            VolumeAction.FAILED,
        } and not all(reason_fields):
            raise ValueError("non-delete volume outcomes require an actionable reason")
        if self.action is VolumeAction.DELETE_REQUESTED:
            if self.policy is not VolumePolicy.DELETE:
                raise ValueError("delete-requested requires the delete policy")
            if self.recheck is None:
                raise ValueError("delete-requested requires a recheck snapshot")
        if self.action is VolumeAction.ALREADY_ABSENT and (
            self.action_result is not VolumeActionResult.IDEMPOTENT_SUCCESS
        ):
            raise ValueError("already-absent requires idempotent-success")
        if self.action is VolumeAction.FAILED and (
            self.action_result is not VolumeActionResult.ERROR
        ):
            raise ValueError("failed actions require an error result")
        if self.error is not None and self.action is not VolumeAction.FAILED:
            raise ValueError("safe error metadata is valid only for failed actions")


@dataclass(frozen=True)
class InitialVolumeClassification:
    """Pure initial safety decision and its complete machine-readable reasons."""

    snapshot: VolumeSnapshot
    owned: bool
    delete_candidate: bool
    reason_codes: tuple[VolumeReasonCode, ...]
    outcome: VolumeOutcome | None

    def __post_init__(self) -> None:
        normalized_reasons = tuple(dict.fromkeys(self.reason_codes))
        object.__setattr__(self, "reason_codes", normalized_reasons)
        if self.owned != (self.snapshot.cluster_tag_value == OWNED_CLUSTER_TAG_VALUE):
            raise ValueError("owned must reflect the exact cluster tag value")
        eligible = (
            self.owned and self.snapshot.state == "available" and not self.snapshot.attachment_ids
        )
        if self.delete_candidate:
            if not eligible or normalized_reasons or self.outcome is not None:
                raise ValueError(
                    "delete candidates require exact ownership, available state, "
                    "zero attachments, and no classification failure"
                )
        elif self.outcome is None:
            raise ValueError("a non-candidate classification requires a terminal outcome")


def _volume_outcome(
    snapshot: VolumeSnapshot,
    request: VolumeCleanupRequest,
    *,
    action: VolumeAction,
    action_result: VolumeActionResult,
    reason_code: VolumeReasonCode,
    reason: str,
    follow_up: str,
    recheck: VolumeSnapshot | None = None,
    error: SafeError | None = None,
) -> VolumeOutcome:
    return VolumeOutcome(
        volume_id=snapshot.volume_id,
        region=snapshot.region,
        availability_zone=snapshot.availability_zone,
        size_gib=snapshot.size_gib,
        observed_state=snapshot.state,
        cluster_tag_value=snapshot.cluster_tag_value,
        attachment_ids=snapshot.attachment_ids,
        policy=request.policy,
        action=action,
        action_result=action_result,
        reason_code=reason_code,
        reason=reason,
        follow_up=follow_up,
        recheck=recheck,
        error=error,
    )


def classify_volume(
    snapshot: VolumeSnapshot,
    request: VolumeCleanupRequest,
    *,
    reporting_error: BaseException | None = None,
) -> InitialVolumeClassification:
    """Classify one normalized snapshot without AWS calls or mutable state.

    Exact ownership, state, and attachment facts are evaluated before policy.
    A reporting failure is itself fail-closed: it creates a failed outcome and
    can never turn any volume into a delete candidate.
    """
    owned = snapshot.cluster_tag_value == OWNED_CLUSTER_TAG_VALUE
    safety_reasons: list[VolumeReasonCode] = []
    if not owned:
        safety_reasons.append(VolumeReasonCode.OWNERSHIP_SAFETY)
    if snapshot.state != "available":
        safety_reasons.append(VolumeReasonCode.STATE_NOT_AVAILABLE)
    if snapshot.attachment_ids:
        safety_reasons.append(VolumeReasonCode.ATTACHMENTS_PRESENT)

    if reporting_error is not None:
        reason_codes = (*safety_reasons, VolumeReasonCode.REPORTING_ERROR)
        outcome = _volume_outcome(
            snapshot,
            request,
            action=VolumeAction.FAILED,
            action_result=VolumeActionResult.ERROR,
            reason_code=VolumeReasonCode.REPORTING_ERROR,
            reason="Required volume reporting could not be completed.",
            follow_up=(
                "Review the sanitized reporting error and retry cleanup; the volume "
                "was not selected for deletion."
            ),
            error=normalize_safe_error(reporting_error),
        )
        return InitialVolumeClassification(
            snapshot=snapshot,
            owned=owned,
            delete_candidate=False,
            reason_codes=reason_codes,
            outcome=outcome,
        )

    if request.policy is VolumePolicy.RETAIN:
        primary_reason = (
            VolumeReasonCode.OWNERSHIP_SAFETY if not owned else VolumeReasonCode.RETAIN_POLICY
        )
        reason_codes = (*safety_reasons, VolumeReasonCode.RETAIN_POLICY)
        outcome = _volume_outcome(
            snapshot,
            request,
            action=VolumeAction.RETAINED,
            action_result=(
                VolumeActionResult.SAFETY_PRESERVED
                if safety_reasons
                else VolumeActionResult.SUCCESS
            ),
            reason_code=primary_reason,
            reason=(
                "The exact cluster tag value does not establish GCO ownership."
                if not owned
                else "The selected volume policy retains this volume."
            ),
            follow_up=(
                "Verify ownership independently before taking any manual action."
                if not owned
                else "Delete it later only after verifying that its data is no longer needed."
            ),
        )
        return InitialVolumeClassification(
            snapshot=snapshot,
            owned=owned,
            delete_candidate=False,
            reason_codes=reason_codes,
            outcome=outcome,
        )

    if not request.deletion_authorized:
        outcome = _volume_outcome(
            snapshot,
            request,
            action=VolumeAction.SKIPPED,
            action_result=VolumeActionResult.BLOCKED,
            reason_code=VolumeReasonCode.EVALUATION_ERROR,
            reason="The delete policy lacks explicit deletion authorization.",
            follow_up="Retry with an authorized command policy; no deletion was attempted.",
        )
        return InitialVolumeClassification(
            snapshot=snapshot,
            owned=owned,
            delete_candidate=False,
            reason_codes=(*safety_reasons, VolumeReasonCode.EVALUATION_ERROR),
            outcome=outcome,
        )

    if not safety_reasons:
        return InitialVolumeClassification(
            snapshot=snapshot,
            owned=True,
            delete_candidate=True,
            reason_codes=(),
            outcome=None,
        )

    primary_reason = safety_reasons[0]
    if primary_reason is VolumeReasonCode.OWNERSHIP_SAFETY:
        reason = "The exact cluster tag value does not establish GCO ownership."
        follow_up = "Verify ownership independently before taking any manual action."
    elif primary_reason is VolumeReasonCode.STATE_NOT_AVAILABLE:
        reason = f"The owned volume state is {snapshot.state!r}, not 'available'."
        follow_up = "Detach and wait for the volume to become available before retrying."
    else:
        reason = "The owned volume still has one or more attachments."
        follow_up = "Detach the reported instances and retry after the volume is available."
    outcome = _volume_outcome(
        snapshot,
        request,
        action=VolumeAction.SKIPPED,
        action_result=VolumeActionResult.SAFETY_PRESERVED,
        reason_code=primary_reason,
        reason=reason,
        follow_up=follow_up,
    )
    return InitialVolumeClassification(
        snapshot=snapshot,
        owned=owned,
        delete_candidate=False,
        reason_codes=tuple(safety_reasons),
        outcome=outcome,
    )


@dataclass(frozen=True)
class VolumeOutcomeCounts:
    """A derived partition of final per-volume outcome records."""

    discovered: int
    deleted: int
    retained: int
    skipped: int
    already_absent: int
    failed: int

    def __post_init__(self) -> None:
        values = (
            self.discovered,
            self.deleted,
            self.retained,
            self.skipped,
            self.already_absent,
            self.failed,
        )
        for value in values:
            _require_non_negative_int(value, "volume outcome count")
        if (
            self.deleted + self.retained + self.skipped + self.already_absent + self.failed
            != self.discovered
        ):
            raise ValueError("volume outcome counts must partition discovered records")

    @classmethod
    def from_outcomes(cls, outcomes: Collection[VolumeOutcome]) -> VolumeOutcomeCounts:
        """Derive every count from terminal records; no mutable counters are used."""
        records = tuple(outcomes)
        return cls(
            discovered=len(records),
            deleted=sum(record.action is VolumeAction.DELETE_REQUESTED for record in records),
            retained=sum(record.action is VolumeAction.RETAINED for record in records),
            skipped=sum(record.action is VolumeAction.SKIPPED for record in records),
            already_absent=sum(record.action is VolumeAction.ALREADY_ABSENT for record in records),
            failed=sum(record.action is VolumeAction.FAILED for record in records),
        )


@dataclass(frozen=True)
class TargetVolumeCleanupOutcome:
    """Deterministic structured outcome for one exact regional target."""

    stack_name: str
    stack_id: str | None
    target_region: str | None
    target_cluster: str | None
    cluster_tag_key: str | None
    policy: VolumePolicy
    deletion_authorized: bool
    authorization_source: DeletionAuthorizationSource
    status: VolumeCleanupStatus
    blocking_reason_code: str | None = None
    blocking_reason: str | None = None
    follow_up: str | None = None
    volumes: tuple[VolumeOutcome, ...] = ()
    successful: bool = False
    error: SafeError | None = None
    counts: VolumeOutcomeCounts = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.stack_name, "stack_name")
        VolumeCleanupRequest(
            policy=self.policy,
            deletion_authorized=self.deletion_authorized,
            authorization_source=self.authorization_source,
        )
        ordered = tuple(sorted(self.volumes, key=lambda record: record.volume_id))
        if len({record.volume_id for record in ordered}) != len(ordered):
            raise ValueError("target outcome cannot contain duplicate volume IDs")
        object.__setattr__(self, "volumes", ordered)
        object.__setattr__(self, "counts", VolumeOutcomeCounts.from_outcomes(ordered))
        blocking_fields = (
            self.blocking_reason_code,
            self.blocking_reason,
            self.follow_up,
        )
        if any(value is not None for value in blocking_fields) and not all(
            value is not None for value in blocking_fields
        ):
            raise ValueError(
                "blocking reason code, reason, and follow-up must be provided together"
            )
        if self.status is VolumeCleanupStatus.SKIPPED:
            if ordered or not all(blocking_fields):
                raise ValueError(
                    "skipped target outcomes require a reason and zero discovered volumes"
                )
        elif any(value is not None for value in blocking_fields):
            raise ValueError("only skipped target outcomes may contain a blocking reason")
        expected_success = self.status in {
            VolumeCleanupStatus.COMPLETED,
            VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS,
        }
        if self.successful != expected_success:
            raise ValueError("successful must agree with the target cleanup status")
        if expected_success and not all(
            (self.target_region, self.target_cluster, self.cluster_tag_key)
        ):
            raise ValueError("completed outcomes require complete target identity")
        if any(record.action is VolumeAction.DELETE_REQUESTED for record in ordered) and not (
            self.policy is VolumePolicy.DELETE and self.deletion_authorized
        ):
            raise ValueError("delete-requested records require authorized delete policy")
        if self.error is not None and self.status is not VolumeCleanupStatus.FAILED:
            raise ValueError("target error metadata requires failed status")

    def to_dict(self) -> dict[str, JsonValue]:
        """Return stable JSON-compatible fields with sorted volume records."""
        return _json_mapping(asdict(self))

    def to_json(self) -> str:
        """Return canonical compact JSON for checkpoints and cleanup callbacks."""
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


def _json_mapping(values: Mapping[str, object]) -> dict[str, JsonValue]:
    return {key: _json_value(value) for key, value in values.items()}


def _json_value(value: object) -> JsonValue:
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("serialized cleanup mappings require string keys")
            normalized[key] = _json_value(item)
        return normalized
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported cleanup serialization value: {type(value).__name__}")


_DISCOVERY_FOLLOW_UP = (
    "Resolve the reported EC2 discovery error and retry cleanup; no volume was "
    "deleted from an incomplete inventory."
)
_NORMALIZATION_FOLLOW_UP = (
    "Inspect the reported volume in the target Region and retry cleanup; it was "
    "excluded from every cleanup action."
)
_AMBIGUOUS_FOLLOW_UP = (
    "Re-run discovery so the reported volume resolves to one unambiguous record; "
    "no deletion was requested for it."
)


class VolumeDiscoveryStatus(StrEnum):
    """Whether one Region-scoped discovery pass observed a complete inventory."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class VolumeInventoryError(ValueError):
    """Raised when a ``DescribeVolumes`` page cannot establish a complete inventory."""


@dataclass(frozen=True)
class VolumeDiscoveryFailure:
    """One returned volume that cannot become a complete reportable record."""

    reason_code: VolumeReasonCode
    reason: str
    follow_up: str
    volume_id: str | None = None
    error: SafeError | None = None

    def __post_init__(self) -> None:
        _require_text(self.reason, "reason")
        _require_text(self.follow_up, "follow_up")
        if self.volume_id is not None and (
            not isinstance(self.volume_id, str)
            or _VOLUME_ID_PATTERN.fullmatch(self.volume_id) is None
        ):
            raise ValueError("a discovery failure volume ID must be exact or null")


@dataclass(frozen=True)
class VolumeInventory:
    """Deterministic result of one exact-Region ``DescribeVolumes`` discovery pass.

    ``snapshots`` holds the actionable in-scope volumes and is structurally empty
    whenever the inventory is incomplete, so no caller can request a deletion from
    partial evidence. ``failures`` holds returned volumes that could not be
    normalized into complete records; they never become actionable.
    """

    status: VolumeDiscoveryStatus
    snapshots: tuple[VolumeSnapshot, ...] = ()
    failures: tuple[VolumeDiscoveryFailure, ...] = ()
    error: SafeError | None = None
    reason_code: VolumeReasonCode | None = None
    reason: str | None = None
    follow_up: str | None = None

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.snapshots, key=lambda snapshot: snapshot.volume_id))
        discovered_ids = [snapshot.volume_id for snapshot in ordered]
        if len(set(discovered_ids)) != len(discovered_ids):
            raise ValueError("a volume inventory cannot contain duplicate volume IDs")
        failed_ids = {
            failure.volume_id for failure in self.failures if failure.volume_id is not None
        }
        if failed_ids & set(discovered_ids):
            raise ValueError("a volume cannot be both actionable and unprocessable")
        object.__setattr__(self, "snapshots", ordered)
        object.__setattr__(self, "failures", tuple(self.failures))
        blocking = (self.error, self.reason_code, self.reason, self.follow_up)
        if self.status is VolumeDiscoveryStatus.INCOMPLETE:
            if ordered or not all(value is not None for value in blocking):
                raise ValueError(
                    "an incomplete inventory requires a blocking reason and no actionable snapshots"
                )
        elif any(value is not None for value in blocking):
            raise ValueError("only an incomplete inventory may carry a blocking reason")

    @property
    def complete(self) -> bool:
        """Return whether the full paginated inventory was observed safely."""
        return self.status is VolumeDiscoveryStatus.COMPLETE


def _incomplete_inventory(reason: str, *, error: SafeError) -> VolumeInventory:
    return VolumeInventory(
        status=VolumeDiscoveryStatus.INCOMPLETE,
        error=error,
        reason_code=VolumeReasonCode.EVALUATION_ERROR,
        reason=reason,
        follow_up=_DISCOVERY_FOLLOW_UP,
    )


def _page_volume_dtos(page: object) -> list[object]:
    if not isinstance(page, Mapping):
        raise VolumeInventoryError("EC2 returned a non-object DescribeVolumes page")
    raw_volumes = page.get("Volumes")
    if not isinstance(raw_volumes, list):
        raise VolumeInventoryError("EC2 returned a DescribeVolumes page without a volume list")
    return list(raw_volumes)


def _dto_volume_identifier(dto: object) -> str | None:
    if isinstance(dto, Mapping):
        value = dto.get("VolumeId")
        if isinstance(value, str) and _VOLUME_ID_PATTERN.fullmatch(value) is not None:
            return value
    return None


class _InventoryCollector:
    """Accumulate deterministic in-scope snapshots and unprocessable volumes."""

    def __init__(self, target: RegionalVolumeTarget) -> None:
        self._target = target
        self._snapshots: dict[str, VolumeSnapshot] = {}
        self._failures: dict[str, VolumeDiscoveryFailure] = {}
        self._unidentified: list[VolumeDiscoveryFailure] = []
        self._seen: set[str] = set()

    def add_page(self, page: object) -> None:
        """Re-normalize and re-scope every volume returned by one page."""
        for dto in _page_volume_dtos(page):
            self._add_volume(dto)

    def _add_volume(self, dto: object) -> None:
        if not isinstance(dto, Mapping):
            self._record_unprocessable(
                dto, VolumeNormalizationError("volume DTO must be an object")
            )
            return
        try:
            snapshot = normalize_volume_snapshot(dto, target=self._target)
        except Exception as exc:
            self._record_unprocessable(dto, exc)
            return
        if snapshot is None:
            return
        if snapshot.volume_id in self._seen:
            self._record_ambiguity(snapshot.volume_id)
            return
        self._seen.add(snapshot.volume_id)
        self._snapshots[snapshot.volume_id] = snapshot

    def _record_unprocessable(self, dto: object, error: BaseException) -> None:
        volume_id = _dto_volume_identifier(dto)
        failure = VolumeDiscoveryFailure(
            reason_code=VolumeReasonCode.NORMALIZATION_ERROR,
            reason="A returned volume could not be normalized into a complete record.",
            follow_up=_NORMALIZATION_FOLLOW_UP,
            volume_id=volume_id,
            error=normalize_safe_error(error),
        )
        if volume_id is None:
            self._unidentified.append(failure)
            return
        self._seen.add(volume_id)
        self._snapshots.pop(volume_id, None)
        self._failures[volume_id] = failure

    def _record_ambiguity(self, volume_id: str) -> None:
        self._snapshots.pop(volume_id, None)
        self._failures.setdefault(
            volume_id,
            VolumeDiscoveryFailure(
                reason_code=VolumeReasonCode.EVALUATION_ERROR,
                reason=f"Discovery returned ambiguous duplicate data for {volume_id}.",
                follow_up=_AMBIGUOUS_FOLLOW_UP,
                volume_id=volume_id,
            ),
        )

    def inventory(self) -> VolumeInventory:
        """Return the complete inventory in stable volume-ID order."""
        identified = [self._failures[volume_id] for volume_id in sorted(self._failures)]
        return VolumeInventory(
            status=VolumeDiscoveryStatus.COMPLETE,
            snapshots=tuple(self._snapshots.values()),
            failures=(*identified, *self._unidentified),
        )


def _target_outcome(
    target: RegionalVolumeTarget,
    request: VolumeCleanupRequest,
    *,
    status: VolumeCleanupStatus,
    successful: bool,
    volumes: tuple[VolumeOutcome, ...] = (),
    error: SafeError | None = None,
) -> TargetVolumeCleanupOutcome:
    return TargetVolumeCleanupOutcome(
        stack_name=target.stack_name,
        stack_id=target.stack_id,
        target_region=target.region,
        target_cluster=target.cluster_name,
        cluster_tag_key=target.cluster_tag_key,
        policy=request.policy,
        deletion_authorized=request.deletion_authorized,
        authorization_source=request.authorization_source,
        status=status,
        volumes=volumes,
        successful=successful,
        error=error,
    )


def blocked_target_outcome(
    *,
    stack_name: str,
    request: VolumeCleanupRequest,
    reason_code: str,
    reason: str,
    follow_up: str,
    target: RegionalVolumeTarget | None = None,
) -> TargetVolumeCleanupOutcome:
    """Return one zero-discovery blocked outcome for a missing prerequisite.

    A blocked outcome always reports ``skipped`` with no volume records, because
    the missing prerequisite stopped every EKS and EC2 request before discovery
    could begin. Target identity fields stay ``None`` when the stack could not be
    resolved into an exact regional target at all, so a report can never imply a
    Region, cluster, or tag key that was never authorized.
    """
    return TargetVolumeCleanupOutcome(
        stack_name=stack_name,
        stack_id=None if target is None else target.stack_id,
        target_region=None if target is None else target.region,
        target_cluster=None if target is None else target.cluster_name,
        cluster_tag_key=None if target is None else target.cluster_tag_key,
        policy=request.policy,
        deletion_authorized=request.deletion_authorized,
        authorization_source=request.authorization_source,
        status=VolumeCleanupStatus.SKIPPED,
        blocking_reason_code=reason_code,
        blocking_reason=reason,
        follow_up=follow_up,
        successful=False,
    )


def _blocked_target_outcome(
    target: RegionalVolumeTarget,
    request: VolumeCleanupRequest,
    *,
    reason_code: str,
    reason: str,
    follow_up: str,
) -> TargetVolumeCleanupOutcome:
    return blocked_target_outcome(
        stack_name=target.stack_name,
        request=request,
        reason_code=reason_code,
        reason=reason,
        follow_up=follow_up,
        target=target,
    )


def _inventory_failure_error(failures: tuple[VolumeDiscoveryFailure, ...]) -> SafeError:
    first = failures[0]
    detail = first.error.message if first.error is not None else first.reason
    summary = f"{len(failures)} returned volume(s) could not be processed: {detail}"
    return SafeError(
        error_code=first.error.error_code if first.error is not None else None,
        error_type=(first.error.error_type if first.error is not None else "VolumeInventoryError"),
        message=_safe_error_message(summary, fallback="Volume discovery was incomplete"),
    )


def discovery_target_outcome(
    *,
    target: RegionalVolumeTarget,
    request: VolumeCleanupRequest,
    inventory: VolumeInventory,
) -> TargetVolumeCleanupOutcome | None:
    """Return the terminal outcome that discovery alone establishes, if any.

    An incomplete inventory fails closed with an unsuccessful target outcome and no
    volume records, so no deletion can follow partial evidence. A complete
    inventory with no in-scope volume is a successful no-op. ``None`` means the
    discovered snapshots still require per-volume evaluation.
    """
    if inventory.status is VolumeDiscoveryStatus.INCOMPLETE:
        return _target_outcome(
            target,
            request,
            status=VolumeCleanupStatus.FAILED,
            successful=False,
            error=inventory.error
            or SafeError(
                error_code=None,
                error_type="VolumeInventoryError",
                message="EC2 volume discovery did not return the complete inventory.",
            ),
        )
    if inventory.snapshots:
        return None
    if inventory.failures:
        return _target_outcome(
            target,
            request,
            status=VolumeCleanupStatus.FAILED,
            successful=False,
            error=_inventory_failure_error(inventory.failures),
        )
    return _target_outcome(
        target,
        request,
        status=VolumeCleanupStatus.COMPLETED,
        successful=True,
    )


_EVALUATION_ERROR_FOLLOW_UP = (
    "Resolve the reported volume evaluation error and retry cleanup; no deletion "
    "was requested for this volume."
)
_REPORTING_FAILURE_FOLLOW_UP = (
    "Resolve the reported reporting error and retry cleanup; the volume was "
    "excluded from every cleanup action."
)
_ABSENCE_PROOF_FOLLOW_UP = (
    "Re-verify cluster absence for this exact stack, Region, and cluster before "
    "retrying cleanup; no EBS request was made."
)
_UNAUTHORIZED_DELETE_FOLLOW_UP = (
    "Retry with an authorized command policy; no EBS discovery or deletion request was made."
)

_REQUIRED_RECORD_FIELDS = (
    "volume_id",
    "region",
    "availability_zone",
    "observed_state",
    "policy",
    "action",
    "action_result",
)


class VolumeReportingError(ValueError):
    """Raised when a terminal volume record cannot be reported completely."""

    reason_code = VolumeReasonCode.REPORTING_ERROR


def verify_volume_reporting(record: VolumeOutcome) -> None:
    """Confirm one terminal record carries every required, serializable field.

    Reporting completeness is verified per volume so one unreportable record can
    never suppress the remaining records or weaken a safety decision.
    """
    if not isinstance(record, VolumeOutcome):
        raise VolumeReportingError("a volume record must be a complete outcome object")
    for field_name in _REQUIRED_RECORD_FIELDS:
        value = getattr(record, field_name, None)
        if value is None or value == "":
            raise VolumeReportingError(f"volume record field {field_name!r} is missing")
    if record.action is not VolumeAction.DELETE_REQUESTED and not all(
        (record.reason_code, record.reason, record.follow_up)
    ):
        raise VolumeReportingError("a non-delete volume record requires an actionable reason")
    try:
        _json_value(asdict(record))
    except (TypeError, ValueError) as exc:
        raise VolumeReportingError("a volume record could not be serialized") from exc


def aggregate_target_outcome(
    *,
    target: RegionalVolumeTarget,
    request: VolumeCleanupRequest,
    records: Collection[VolumeOutcome],
    unprocessable: Collection[VolumeDiscoveryFailure] = (),
) -> TargetVolumeCleanupOutcome:
    """Derive the target status and success from terminal per-volume records.

    Nothing is counted independently: every status decision is read back from the
    final records. Authorized deletion fails whenever an owned volume remains
    because of a safety condition or an error, while retention of volumes that GCO
    does not own can still complete successfully.
    """
    ordered = tuple(sorted(records, key=lambda record: record.volume_id))
    unreportable = tuple(unprocessable)
    authorized_delete = request.policy is VolumePolicy.DELETE and request.deletion_authorized
    preserved = tuple(
        record
        for record in ordered
        if record.action in {VolumeAction.RETAINED, VolumeAction.SKIPPED}
    )
    owned_remaining = any(
        record.cluster_tag_value == OWNED_CLUSTER_TAG_VALUE for record in preserved
    )
    failed = any(record.action is VolumeAction.FAILED for record in ordered)

    if failed or unreportable or (authorized_delete and owned_remaining):
        status = VolumeCleanupStatus.FAILED
    elif authorized_delete and preserved:
        status = VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS
    else:
        status = VolumeCleanupStatus.COMPLETED
    return _target_outcome(
        target,
        request,
        status=status,
        successful=status is not VolumeCleanupStatus.FAILED,
        volumes=ordered,
        error=_inventory_failure_error(unreportable) if unreportable else None,
    )


#: Exact EC2 error code that proves one volume is already absent.
VOLUME_NOT_FOUND_ERROR_CODE = "InvalidVolume.NotFound"

_RECHECK_CHANGED_FOLLOW_UP = (
    "Review the reported current volume facts and retry cleanup only after the "
    "volume is again owned, available, and detached; no deletion was requested."
)
_RECHECK_ERROR_FOLLOW_UP = (
    "Resolve the reported just-in-time safety-recheck error and retry cleanup; "
    "no deletion was requested for this volume."
)
_DELETE_ERROR_FOLLOW_UP = (
    "Resolve the reported EC2 deletion error and retry cleanup; the volume may "
    "still exist and continue to incur storage cost."
)
_ALREADY_ABSENT_FOLLOW_UP = "No action is required; the volume was already absent when cleanup ran."
_DELETE_REQUESTED_FOLLOW_UP = (
    "No action is required; confirm in EC2 that the volume no longer exists if "
    "you need independent evidence."
)


@dataclass(frozen=True)
class RecheckEvaluation:
    """Pure comparison of initial and just-in-time volume safety facts."""

    eligible: bool
    changed_facts: tuple[str, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed_facts", tuple(self.changed_facts))
        if self.eligible:
            if self.changed_facts or self.reason is not None:
                raise ValueError("an eligible recheck cannot report changed safety facts")
        elif not self.changed_facts or not self.reason:
            raise ValueError("an ineligible recheck requires changed facts and a reason")


def _attachment_summary(attachment_ids: tuple[str, ...]) -> str:
    return ", ".join(attachment_ids) if attachment_ids else "none"


def evaluate_recheck(
    initial: VolumeSnapshot,
    current: VolumeSnapshot,
    *,
    target: RegionalVolumeTarget,
) -> RecheckEvaluation:
    """Decide whether just-in-time facts still satisfy every deletion predicate.

    Deletion is allowed only when the current snapshot keeps the same volume
    identity and Availability Zone inside the target Region, still carries the
    exact ``owned`` cluster tag value, is still ``available``, and still has zero
    attachments. Any changed fact is reported with its initial and current value.
    """
    comparisons: tuple[tuple[bool, str, str, str], ...] = (
        (
            current.volume_id != initial.volume_id,
            "volume-identity",
            initial.volume_id,
            current.volume_id,
        ),
        (current.region != target.region, "region", initial.region, current.region),
        (
            current.availability_zone != initial.availability_zone,
            "availability-zone",
            initial.availability_zone,
            current.availability_zone,
        ),
        (
            current.cluster_tag_value != OWNED_CLUSTER_TAG_VALUE,
            "cluster-tag-value",
            str(initial.cluster_tag_value),
            str(current.cluster_tag_value),
        ),
        (current.state != "available", "state", initial.state, current.state),
        (
            bool(current.attachment_ids),
            "attachments",
            _attachment_summary(initial.attachment_ids),
            _attachment_summary(current.attachment_ids),
        ),
    )
    changed = [(fact, before, after) for unsafe, fact, before, after in comparisons if unsafe]
    if not changed:
        return RecheckEvaluation(eligible=True)
    details = "; ".join(f"{fact} {before!r} -> {after!r}" for fact, before, after in changed)
    return RecheckEvaluation(
        eligible=False,
        changed_facts=tuple(fact for fact, _, _ in changed),
        reason=(f"Just-in-time safety facts no longer satisfy the deletion criteria: {details}."),
    )


def is_exact_volume_not_found(error: BaseException) -> bool:
    """Return whether ``error`` is exactly the EC2 volume-not-found error."""
    if not isinstance(error, ClientError):
        return False
    response_error = error.response.get("Error", {})
    if not isinstance(response_error, Mapping):
        return False
    return response_error.get("Code") == VOLUME_NOT_FOUND_ERROR_CODE


class _CandidateAbsent(Exception):
    """Raised internally when exact not-found proves a candidate is absent."""


class VolumeCleanupService:
    """Discover and dispose of exact cluster volumes inside one target Region."""

    def __init__(self, client_factory: ClientFactory) -> None:
        self._client_factory = client_factory

    def cleanup(
        self,
        *,
        target: RegionalVolumeTarget,
        absence: ClusterAbsenceProof,
        request: VolumeCleanupRequest,
    ) -> TargetVolumeCleanupOutcome:
        """Dispose of one exact regional target's volumes and report the outcome.

        Cleanup is fail-closed at the boundary: evidence bound to another identity
        and an unauthorized delete policy both produce a blocked outcome with zero
        discovery. Afterwards every processable in-scope volume receives exactly one
        terminal record, because normalization, evaluation, absence-classification,
        deletion, and reporting failures are isolated per volume. The target status
        and success are derived only from those records, so repeating cleanup for the
        same target re-reads current evidence without touching unrelated volumes.
        """
        if not absence.matches(target):
            return _blocked_target_outcome(
                target,
                request,
                reason_code="cluster-absence-proof-mismatch",
                reason=(
                    "The cluster-absence evidence is not bound to this exact stack, "
                    "Region, and cluster."
                ),
                follow_up=_ABSENCE_PROOF_FOLLOW_UP,
            )
        if request.policy is VolumePolicy.DELETE and not request.deletion_authorized:
            return _blocked_target_outcome(
                target,
                request,
                reason_code="deletion-unauthorized",
                reason="The delete policy lacks explicit deletion authorization.",
                follow_up=_UNAUTHORIZED_DELETE_FOLLOW_UP,
            )

        inventory = self.discover_volumes(target=target, absence=absence)
        established = discovery_target_outcome(
            target=target,
            request=request,
            inventory=inventory,
        )
        if established is not None:
            return established

        records, candidates = self._classify_inventory(inventory, request)
        records.extend(
            self.delete_candidates(target=target, request=request, candidates=candidates)
        )

        reportable: list[VolumeOutcome] = []
        unprocessable: list[VolumeDiscoveryFailure] = list(inventory.failures)
        for record in records:
            replacement = self._reportable_record(record, request)
            if replacement is None:
                unprocessable.append(
                    VolumeDiscoveryFailure(
                        reason_code=VolumeReasonCode.REPORTING_ERROR,
                        reason="A volume record could not be reported completely.",
                        follow_up=_REPORTING_FAILURE_FOLLOW_UP,
                    )
                )
                continue
            reportable.append(replacement)
        return aggregate_target_outcome(
            target=target,
            request=request,
            records=reportable,
            unprocessable=unprocessable,
        )

    @classmethod
    def _classify_inventory(
        cls,
        inventory: VolumeInventory,
        request: VolumeCleanupRequest,
    ) -> tuple[list[VolumeOutcome], list[VolumeSnapshot]]:
        """Split the complete inventory into terminal records and delete candidates."""
        records: list[VolumeOutcome] = []
        candidates: list[VolumeSnapshot] = []
        for snapshot in inventory.snapshots:
            try:
                classification = classify_volume(snapshot, request)
                outcome = classification.outcome
                if classification.delete_candidate:
                    candidates.append(classification.snapshot)
                    continue
                if outcome is None:
                    raise VolumeReportingError("volume classification produced no terminal record")
            except Exception as exc:
                records.append(
                    cls._failed_outcome(
                        snapshot,
                        request,
                        reason_code=VolumeReasonCode.EVALUATION_ERROR,
                        reason="The volume safety classification could not be completed.",
                        follow_up=_EVALUATION_ERROR_FOLLOW_UP,
                        error=normalize_safe_error(exc),
                    )
                )
                continue
            records.append(outcome)
        return records, candidates

    @staticmethod
    def _reportable_record(
        record: VolumeOutcome,
        request: VolumeCleanupRequest,
    ) -> VolumeOutcome | None:
        """Return a reportable record, replacing an incomplete one with a failure."""
        try:
            verify_volume_reporting(record)
        except Exception as reporting_error:
            try:
                replacement = classify_volume(
                    VolumeSnapshot(
                        volume_id=record.volume_id,
                        region=record.region,
                        availability_zone=record.availability_zone,
                        size_gib=record.size_gib,
                        state=record.observed_state,
                        cluster_tag_value=record.cluster_tag_value,
                        attachment_ids=record.attachment_ids,
                    ),
                    request,
                    reporting_error=reporting_error,
                ).outcome
            except Exception:
                return None
            return replacement
        return record

    def discover_volumes(
        self,
        *,
        target: RegionalVolumeTarget,
        absence: ClusterAbsenceProof,
    ) -> VolumeInventory:
        """Return the complete in-scope inventory, or fail closed with no snapshots.

        The EC2 client is created only for ``target.region`` and the only filter is
        the exact cluster ``tag-key``. Every page is re-normalized and re-scoped, so
        an out-of-Region or differently tagged object never enters the inventory,
        and any incomplete pagination yields no actionable volume at all.
        """
        absence.require_matches(target)
        try:
            ec2 = self._client_factory("ec2", region_name=target.region)
        except Exception as exc:
            return _incomplete_inventory(
                f"Could not create the exact-Region EC2 client for {target.region}.",
                error=normalize_safe_error(exc),
            )

        try:
            pages = iter(
                ec2.get_paginator("describe_volumes").paginate(
                    Filters=[{"Name": "tag-key", "Values": [target.cluster_tag_key]}]
                )
            )
        except Exception as exc:
            return _incomplete_inventory(
                "EC2 volume discovery could not be started.",
                error=normalize_safe_error(exc),
            )

        collector = _InventoryCollector(target)
        while True:
            try:
                page = next(pages)
            except StopIteration:
                break
            except Exception as exc:
                return _incomplete_inventory(
                    "EC2 volume discovery did not return the complete inventory.",
                    error=normalize_safe_error(exc),
                )
            try:
                collector.add_page(page)
            except VolumeInventoryError as exc:
                return _incomplete_inventory(
                    "EC2 returned a malformed DescribeVolumes page.",
                    error=normalize_safe_error(exc),
                )
        return collector.inventory()

    def delete_candidates(
        self,
        *,
        target: RegionalVolumeTarget,
        request: VolumeCleanupRequest,
        candidates: Collection[VolumeSnapshot],
        ec2: Any | None = None,
    ) -> tuple[VolumeOutcome, ...]:
        """Recheck and delete each candidate with one exact-Region EC2 client.

        Candidates are processed in stable volume-ID order. When no client is
        supplied, exactly one client is created for ``target.region``; a client
        that cannot be created leaves every candidate a safe failed record and
        issues no deletion request.
        """
        ordered = tuple(sorted(candidates, key=lambda snapshot: snapshot.volume_id))
        if not ordered:
            return ()
        client = ec2
        if client is None:
            try:
                client = self._client_factory("ec2", region_name=target.region)
            except Exception as exc:
                return tuple(
                    self._failed_outcome(
                        snapshot,
                        request,
                        reason_code=VolumeReasonCode.RECHECK_ERROR,
                        reason=(
                            "The just-in-time safety recheck could not create the "
                            f"exact-Region EC2 client for {target.region}."
                        ),
                        follow_up=_RECHECK_ERROR_FOLLOW_UP,
                        error=normalize_safe_error(exc),
                    )
                    for snapshot in ordered
                )
        return tuple(
            self._isolated_delete_candidate(
                ec2=client,
                target=target,
                request=request,
                snapshot=snapshot,
            )
            for snapshot in ordered
        )

    def _isolated_delete_candidate(
        self,
        *,
        ec2: Any,
        target: RegionalVolumeTarget,
        request: VolumeCleanupRequest,
        snapshot: VolumeSnapshot,
    ) -> VolumeOutcome:
        """Keep one candidate's unexpected disposition failure from stopping cleanup."""
        try:
            return self.delete_candidate(
                ec2=ec2,
                target=target,
                request=request,
                snapshot=snapshot,
            )
        except Exception as exc:
            return self._failed_outcome(
                snapshot,
                request,
                reason_code=VolumeReasonCode.EVALUATION_ERROR,
                reason="The volume disposition could not be completed or classified.",
                follow_up=_EVALUATION_ERROR_FOLLOW_UP,
                error=normalize_safe_error(exc),
            )

    def delete_candidate(
        self,
        *,
        ec2: Any,
        target: RegionalVolumeTarget,
        request: VolumeCleanupRequest,
        snapshot: VolumeSnapshot,
    ) -> VolumeOutcome:
        """Revalidate one candidate immediately before requesting its deletion.

        The candidate is re-described by exact volume ID in the target Region and
        revalidated against every deletion predicate. Only a still-eligible
        snapshot reaches ``DeleteVolume``. Exact not-found at either stage is an
        idempotent success; every other failure is a safe failed record.
        """
        if request.policy is not VolumePolicy.DELETE or not request.deletion_authorized:
            raise ValueError("just-in-time deletion requires an authorized delete policy")

        try:
            current = self._describe_candidate(ec2, target=target, snapshot=snapshot)
        except _CandidateAbsent:
            return self._already_absent_outcome(snapshot, request)
        except Exception as exc:
            return self._failed_outcome(
                snapshot,
                request,
                reason_code=VolumeReasonCode.RECHECK_ERROR,
                reason="The just-in-time safety recheck could not be completed.",
                follow_up=_RECHECK_ERROR_FOLLOW_UP,
                error=normalize_safe_error(exc),
            )

        if current is None:
            return _volume_outcome(
                snapshot,
                request,
                action=VolumeAction.SKIPPED,
                action_result=VolumeActionResult.SAFETY_PRESERVED,
                reason_code=VolumeReasonCode.SAFETY_RECHECK_CHANGED,
                reason=(
                    "The volume no longer carries the exact cluster tag key "
                    f"{target.cluster_tag_key!r} in Region {target.region}."
                ),
                follow_up=_RECHECK_CHANGED_FOLLOW_UP,
            )

        evaluation = evaluate_recheck(snapshot, current, target=target)
        if not evaluation.eligible:
            return _volume_outcome(
                snapshot,
                request,
                action=VolumeAction.SKIPPED,
                action_result=VolumeActionResult.SAFETY_PRESERVED,
                reason_code=VolumeReasonCode.SAFETY_RECHECK_CHANGED,
                reason=evaluation.reason or "Just-in-time safety facts changed.",
                follow_up=_RECHECK_CHANGED_FOLLOW_UP,
                recheck=current,
            )

        try:
            ec2.delete_volume(VolumeId=current.volume_id)
        except Exception as exc:
            if is_exact_volume_not_found(exc):
                return self._already_absent_outcome(snapshot, request, recheck=current)
            return self._failed_outcome(
                snapshot,
                request,
                reason_code=VolumeReasonCode.DELETE_ERROR,
                reason="The authorized volume deletion request failed.",
                follow_up=_DELETE_ERROR_FOLLOW_UP,
                error=normalize_safe_error(exc),
                recheck=current,
            )

        return _volume_outcome(
            snapshot,
            request,
            action=VolumeAction.DELETE_REQUESTED,
            action_result=VolumeActionResult.SUCCESS,
            reason_code=VolumeReasonCode.DELETE_REQUEST_ACCEPTED,
            reason=(
                "The volume was owned, available, and detached at the just-in-time "
                "safety recheck, and EC2 accepted the deletion request."
            ),
            follow_up=_DELETE_REQUESTED_FOLLOW_UP,
            recheck=current,
        )

    @staticmethod
    def _describe_candidate(
        ec2: Any,
        *,
        target: RegionalVolumeTarget,
        snapshot: VolumeSnapshot,
    ) -> VolumeSnapshot | None:
        """Re-describe one exact volume ID and normalize the current facts."""
        try:
            response = ec2.describe_volumes(VolumeIds=[snapshot.volume_id])
        except Exception as exc:
            if is_exact_volume_not_found(exc):
                raise _CandidateAbsent(snapshot.volume_id) from exc
            raise

        if not isinstance(response, Mapping):
            raise VolumeNormalizationError(
                "EC2 returned a non-object just-in-time DescribeVolumes response"
            )
        volumes = response.get("Volumes")
        if not isinstance(volumes, list) or len(volumes) != 1:
            raise VolumeNormalizationError(
                "the just-in-time DescribeVolumes response is not exactly one volume"
            )
        dto = volumes[0]
        if not isinstance(dto, Mapping):
            raise VolumeNormalizationError("the just-in-time volume DTO must be an object")
        if _dto_volume_identifier(dto) != snapshot.volume_id:
            raise VolumeNormalizationError(
                "the just-in-time response identifies a different volume"
            )
        return normalize_volume_snapshot(dto, target=target)

    @staticmethod
    def _already_absent_outcome(
        snapshot: VolumeSnapshot,
        request: VolumeCleanupRequest,
        *,
        recheck: VolumeSnapshot | None = None,
    ) -> VolumeOutcome:
        return _volume_outcome(
            snapshot,
            request,
            action=VolumeAction.ALREADY_ABSENT,
            action_result=VolumeActionResult.IDEMPOTENT_SUCCESS,
            reason_code=VolumeReasonCode.ALREADY_ABSENT,
            reason=f"EC2 reported {VOLUME_NOT_FOUND_ERROR_CODE} for this exact volume ID.",
            follow_up=_ALREADY_ABSENT_FOLLOW_UP,
            recheck=recheck,
        )

    @staticmethod
    def _failed_outcome(
        snapshot: VolumeSnapshot,
        request: VolumeCleanupRequest,
        *,
        reason_code: VolumeReasonCode,
        reason: str,
        follow_up: str,
        error: SafeError,
        recheck: VolumeSnapshot | None = None,
    ) -> VolumeOutcome:
        return _volume_outcome(
            snapshot,
            request,
            action=VolumeAction.FAILED,
            action_result=VolumeActionResult.ERROR,
            reason_code=reason_code,
            reason=reason,
            follow_up=follow_up,
            recheck=recheck,
            error=error,
        )
