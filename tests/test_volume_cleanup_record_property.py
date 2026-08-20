"""Property test proving non-success volume records are actionable and complete.

Records are produced by the real cleanup service against an offline, Region-scoped
EC2 double so the generated actions are the ones production code can actually
emit. Each generated outcome is then rendered through both reporting paths, in
both the human-readable and machine-readable rendering modes, and the emitted
fields are compared field for field.
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Mapping
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

from cli.volume_cleanup import (
    OWNED_CLUSTER_TAG_VALUE,
    VOLUME_NOT_FOUND_ERROR_CODE,
    ClusterAbsenceProof,
    DeletionAuthorizationSource,
    JsonValue,
    RegionalVolumeTarget,
    TargetVolumeCleanupOutcome,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupRequest,
    VolumeCleanupService,
    VolumePolicy,
    VolumeReasonCode,
    verify_volume_reporting,
)
from cli.volume_cleanup_reporting import (
    EBS_VOLUME_CLEANUP_NAME,
    REQUIRED_TARGET_FIELDS,
    REQUIRED_VOLUME_FIELDS,
    missing_target_fields,
    missing_volume_fields,
    publish_volume_cleanup_outcome,
    render_volume_cleanup_outcome,
)

_PROJECT = "gco"
_REGION = "us-east-1"
_STACK = f"{_PROJECT}-{_REGION}"
_CLUSTER_TAG_KEY = f"kubernetes.io/cluster/{_STACK}"
_VERIFIED_AT = datetime(2026, 1, 1, tzinfo=UTC).isoformat()

_TARGET = RegionalVolumeTarget(
    stack_name=_STACK,
    stack_id=f"arn:aws:cloudformation:{_REGION}:123456789012:stack/{_STACK}/abc",
    region=_REGION,
    cluster_name=_STACK,
    cluster_tag_key=_CLUSTER_TAG_KEY,
)
_ABSENCE = ClusterAbsenceProof(
    stack_name=_STACK,
    region=_REGION,
    cluster_name=_STACK,
    verified_at=_VERIFIED_AT,
)

#: Actions whose record must carry its own reason, reason code, and follow-up.
_NON_SUCCESS_ACTIONS = frozenset({VolumeAction.RETAINED, VolumeAction.SKIPPED, VolumeAction.FAILED})

_VOLUME_IDS = st.from_regex(r"vol-[0-9a-f]{17}", fullmatch=True)
_INSTANCE_IDS = st.from_regex(r"i-[0-9a-f]{8}", fullmatch=True)
_TAG_VALUES = ("owned", "shared", "Owned", "OWNED", "")
_STATES = ("available", "in-use", "creating", "deleting", "error")
_ZONE_SUFFIXES = ("a", "b", "c")
#: Just-in-time behavior applied to a volume that is initially delete-eligible.
_CANDIDATE_FATES = ("delete", "absent", "changed", "recheck-error", "delete-error")


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "mocked failure"}}, operation)


@dataclass
class _VolumeSpec:
    """One generated in-scope EBS volume and its just-in-time behavior."""

    volume_id: str
    tag_value: str
    state: str
    size_gib: int
    zone_suffix: str
    attachments: tuple[str, ...]
    fate: str

    @property
    def availability_zone(self) -> str:
        return f"{_REGION}{self.zone_suffix}"

    def dto(self) -> dict[str, object]:
        return {
            "VolumeId": self.volume_id,
            "AvailabilityZone": self.availability_zone,
            "Size": self.size_gib,
            "State": self.state,
            "Tags": [{"Key": _CLUSTER_TAG_KEY, "Value": self.tag_value}],
            "Attachments": [
                {"VolumeId": self.volume_id, "InstanceId": instance_id}
                for instance_id in self.attachments
            ],
        }


@dataclass
class _FakeEC2:
    """Offline Region-scoped EC2 double for discovery, recheck, and deletion."""

    specs: dict[str, _VolumeSpec]
    region: str = _REGION

    def get_paginator(self, operation_name: str) -> _FakeEC2:
        assert operation_name == "describe_volumes"
        return self

    def paginate(self, **kwargs: Any) -> Iterator[dict[str, object]]:
        filters = kwargs.get("Filters")
        assert filters == [{"Name": "tag-key", "Values": [_CLUSTER_TAG_KEY]}]
        yield {"Volumes": [spec.dto() for spec in self.specs.values()]}

    def describe_volumes(self, **kwargs: Any) -> dict[str, object]:
        volume_id = str(list(kwargs.get("VolumeIds", []))[0])
        spec = self.specs[volume_id]
        if spec.fate == "absent":
            raise _client_error(VOLUME_NOT_FOUND_ERROR_CODE, "DescribeVolumes")
        if spec.fate == "recheck-error":
            raise _client_error("RequestLimitExceeded", "DescribeVolumes")
        current = spec.dto()
        if spec.fate == "changed":
            current["State"] = "in-use"
            current["Attachments"] = [{"VolumeId": volume_id, "InstanceId": "i-0123456789abcdef0"}]
        return {"Volumes": [current]}

    def delete_volume(self, **kwargs: Any) -> dict[str, object]:
        volume_id = str(kwargs["VolumeId"])
        if self.specs[volume_id].fate == "delete-error":
            raise _client_error("UnauthorizedOperation", "DeleteVolume")
        return {}


@dataclass
class _FakeConfig:
    output_format: str = "table"


@dataclass
class _RecordingFormatter:
    """Recording stand-in for the CLI formatter used by both reporting paths."""

    config: _FakeConfig = field(default_factory=_FakeConfig)
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

    def emitted(self, stdout: str) -> dict[str, Any]:
        """Return every field this formatter emitted, for path comparison."""
        return {
            "printed": self.printed,
            "info": self.info,
            "success": self.success,
            "warnings": self.warnings,
            "errors": self.errors,
            "stdout": stdout,
        }


def _render(
    outcome: TargetVolumeCleanupOutcome | Mapping[str, Any],
    *,
    output_format: str,
) -> dict[str, Any]:
    formatter = _RecordingFormatter(config=_FakeConfig(output_format=output_format))
    stream = io.StringIO()
    with redirect_stdout(stream):
        render_volume_cleanup_outcome(formatter, outcome)
    return formatter.emitted(stream.getvalue())


@st.composite
def _cleanup_runs(draw: st.DrawFn) -> tuple[VolumeCleanupRequest, tuple[_VolumeSpec, ...]]:
    policy = draw(st.sampled_from(tuple(VolumePolicy)))
    authorized = policy is VolumePolicy.DELETE
    request = VolumeCleanupRequest(
        policy=policy,
        deletion_authorized=authorized,
        authorization_source=(
            DeletionAuthorizationSource.DESTROY_ALL_WITH_YES
            if authorized
            else DeletionAuthorizationSource.NONE
        ),
    )
    volume_ids = draw(st.lists(_VOLUME_IDS, min_size=1, max_size=6, unique=True))
    specs = tuple(
        _VolumeSpec(
            volume_id=volume_id,
            tag_value=draw(st.sampled_from(_TAG_VALUES)),
            state=draw(st.sampled_from(_STATES)),
            size_gib=draw(st.integers(min_value=0, max_value=16384)),
            zone_suffix=draw(st.sampled_from(_ZONE_SUFFIXES)),
            attachments=tuple(draw(st.lists(_INSTANCE_IDS, max_size=2, unique=True))),
            fate=draw(st.sampled_from(_CANDIDATE_FATES)),
        )
        for volume_id in volume_ids
    )
    return request, specs


@settings(max_examples=150, deadline=None)
@given(generated=_cleanup_runs())
def test_non_success_records_are_actionable_and_complete(
    generated: tuple[VolumeCleanupRequest, tuple[_VolumeSpec, ...]],
) -> None:
    # Feature: cleanup-dynamic-ebs-volumes, Property 10: Non-success records are actionable and complete
    #
    # **Validates: Requirements 4.7, 6.1, 6.2, 6.5**
    request, specs = generated
    ec2 = _FakeEC2(specs={spec.volume_id: spec for spec in specs})
    service = VolumeCleanupService(lambda service_name, *, region_name: ec2)

    outcome = service.cleanup(target=_TARGET, absence=_ABSENCE, request=request)

    for record in outcome.volumes:
        verify_volume_reporting(record)

    published: list[tuple[str, dict[str, Any]]] = []
    publication = publish_volume_cleanup_outcome(
        outcome,
        publisher=lambda name, details: published.append((name, details)),
    )
    assert publication.reporting_successful
    assert published == [(EBS_VOLUME_CLEANUP_NAME, outcome.to_dict())]
    channel_details: dict[str, JsonValue] = dict(published[0][1])

    # Requirement 6.1: every target and per-volume reporting field is present.
    assert missing_target_fields(channel_details) == ()
    for name in REQUIRED_TARGET_FIELDS:
        assert name in channel_details
    records = channel_details["volumes"]
    assert isinstance(records, list)
    assert len(records) == len(specs)

    for serialized in records:
        assert isinstance(serialized, dict)
        assert missing_volume_fields(serialized) == ()
        for name in REQUIRED_VOLUME_FIELDS:
            assert name in serialized
        assert serialized["region"] == _REGION
        assert serialized["policy"] == request.policy.value

        action = serialized["action"]
        # Requirement 6.2: retained, skipped, and failed records stay actionable.
        if action in {member.value for member in _NON_SUCCESS_ACTIONS}:
            for name in ("reason_code", "reason", "follow_up"):
                value = serialized[name]
                assert isinstance(value, str) and value.strip()
            assert serialized["reason_code"] in {member.value for member in VolumeReasonCode}
            assert serialized["action_result"] in {member.value for member in VolumeActionResult}

        # Requirement 4.7: a volume GCO does not own reports an ownership-safety reason.
        if serialized["cluster_tag_value"] != OWNED_CLUSTER_TAG_VALUE:
            assert action != VolumeAction.DELETE_REQUESTED.value
            assert serialized["reason_code"] == VolumeReasonCode.OWNERSHIP_SAFETY.value

    # Requirement 6.5: single destruction presents exactly the orchestrated fields.
    for output_format in ("table", "json", "yaml"):
        single = _render(outcome, output_format=output_format)
        orchestrated = _render(channel_details, output_format=output_format)
        assert single == orchestrated
        if output_format == "table":
            assert all(
                str(serialized["volume_id"]) in single["stdout"]
                for serialized in records
                if isinstance(serialized, dict)
            )
        else:
            assert single["printed"] == [channel_details]
