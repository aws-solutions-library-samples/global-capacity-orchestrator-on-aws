"""Property test proving multi-target volume cleanup stays authorized and isolated.

The orchestrated barrier in ``cli.stacks`` receives exactly one resolved
``destroy-all -y`` request and a mixed set of exact regional and non-regional
stacks. This module generates those sets together with varying worker completion
orders and asserts the barrier applies the one request to every exact regional
target, never to a non-regional stack, keeps each target's Region, cluster,
records, reasons, and counts to itself, and publishes one deterministic order
regardless of the order the workers finished in.

Only the barrier's collaborators are doubled: target resolution and cluster
absence run for real, the EKS client is a recording double that proves the exact
cluster is absent, and the injected cleanup service returns per-target evidence
instead of talking to EC2. No test here touches live AWS.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

from cli.stacks import StackManager
from cli.volume_cleanup import (
    ClusterAbsenceProof,
    ClusterAbsenceVerifier,
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    SafeError,
    TargetResolution,
    TargetVolumeCleanupOutcome,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupRequest,
    VolumeCleanupService,
    VolumeCleanupStatus,
    VolumeOutcome,
    VolumePolicy,
    VolumeReasonCode,
    VolumeSnapshot,
)
from cli.volume_cleanup_reporting import EBS_VOLUME_CLEANUP_NAME

_PROJECT = "gco"

# Configured regional Regions of one partition; a generated destroy-all run
# selects any non-empty subset of them as its exact regional targets.
_CONFIGURED_REGIONS = ("us-east-1", "us-west-2", "eu-west-1", "ap-southeast-2")

# One stable volume-ID letter per Region so every generated volume identifier
# belongs to exactly one target and cross-target leakage is detectable.
_REGION_LETTERS: Mapping[str, str] = {
    "us-east-1": "a",
    "us-west-2": "b",
    "eu-west-1": "c",
    "ap-southeast-2": "d",
}

# Stacks the regional destruction phase can also contain that are not exact
# configured regional stacks, including a project-scoped unconfigured Region.
_NON_REGIONAL_STACKS = (
    "gco-global",
    "gco-monitoring",
    "gco-experiment",
    "other-us-east-1",
    "gco-us-east-1-observability",
)

# The one resolved request an operator's `gco stacks destroy-all -y` produces.
_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.DELETE,
    deletion_authorized=True,
    authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
)

_VERIFIED_AT = datetime(2026, 1, 1, tzinfo=UTC)

# Per-target cleanup evidence the injected service returns for one target.
_OUTCOME_KINDS = ("deleted", "retentions", "failed", "raises", "empty")


def _stack_name(region: str) -> str:
    return f"{_PROJECT}-{region}"


def _cluster_tag_key(region: str) -> str:
    return f"kubernetes.io/cluster/{_stack_name(region)}"


@dataclass(frozen=True)
class _TargetPlan:
    """One generated regional target, its deletion state, and its evidence."""

    region: str
    deleted: bool
    kind: str
    volume_count: int

    @property
    def stack_name(self) -> str:
        return _stack_name(self.region)

    @property
    def volume_ids(self) -> tuple[str, ...]:
        letter = _REGION_LETTERS[self.region]
        count = 0 if self.kind == "empty" else self.volume_count
        return tuple(f"vol-0{letter}{index:015d}" for index in range(count))

    @property
    def cleanup_attempted(self) -> bool:
        """EKS and EC2 work happens only after a definitive stack deletion."""
        return self.deleted

    @property
    def reports_records(self) -> bool:
        """Only a target whose cleanup service returned evidence reports records."""
        return self.deleted and self.kind != "raises"

    @property
    def expected_volume_ids(self) -> tuple[str, ...]:
        return self.volume_ids if self.reports_records else ()

    @property
    def expected_blocking_reason_code(self) -> str | None:
        if not self.deleted:
            return "stack-deletion-unverified"
        if self.kind == "raises":
            return "cleanup-helper-error"
        return None

    @property
    def expected_successful(self) -> bool:
        if not self.deleted:
            return False
        return self.kind in {"deleted", "retentions", "empty"}

    def _record(self, volume_id: str) -> VolumeOutcome:
        """Build one terminal record whose text belongs only to this target."""
        cluster = self.stack_name
        common: dict[str, Any] = {
            "volume_id": volume_id,
            "region": self.region,
            "availability_zone": f"{self.region}a",
            "size_gib": 50,
            "policy": _REQUEST.policy,
        }
        if self.kind == "retentions":
            return VolumeOutcome(
                **common,
                observed_state="available",
                cluster_tag_value="shared",
                attachment_ids=(),
                action=VolumeAction.SKIPPED,
                action_result=VolumeActionResult.SAFETY_PRESERVED,
                reason_code=VolumeReasonCode.OWNERSHIP_SAFETY,
                reason=f"{volume_id} is not owned by cluster {cluster}",
                follow_up=f"Review {volume_id} in {self.region} before deleting it",
            )
        if self.kind == "failed":
            return VolumeOutcome(
                **common,
                observed_state="available",
                cluster_tag_value="owned",
                attachment_ids=(),
                action=VolumeAction.FAILED,
                action_result=VolumeActionResult.ERROR,
                reason_code=VolumeReasonCode.DELETE_ERROR,
                reason=f"EC2 refused to delete {volume_id} for cluster {cluster}",
                follow_up=f"Retry cleanup for {volume_id} in {self.region}",
                error=SafeError(
                    error_code="RequestLimitExceeded",
                    error_type="ClientError",
                    message=f"throttled while deleting {volume_id}",
                ),
            )
        snapshot = VolumeSnapshot(
            volume_id=volume_id,
            region=self.region,
            availability_zone=f"{self.region}a",
            size_gib=50,
            state="available",
            cluster_tag_value="owned",
            attachment_ids=(),
        )
        return VolumeOutcome(
            **common,
            observed_state="available",
            cluster_tag_value="owned",
            attachment_ids=(),
            action=VolumeAction.DELETE_REQUESTED,
            action_result=VolumeActionResult.SUCCESS,
            reason_code=VolumeReasonCode.DELETE_REQUEST_ACCEPTED,
            reason=f"{volume_id} was deleted for cluster {cluster}",
            follow_up=f"No follow-up is required for {volume_id} in {self.region}",
            recheck=snapshot,
        )

    def outcome(
        self,
        target: RegionalVolumeTarget,
        request: VolumeCleanupRequest,
    ) -> TargetVolumeCleanupOutcome:
        """Return this target's evidence, refusing to delete without authorization."""
        if self.kind in {"deleted", "empty"} and not request.deletion_authorized:
            raise AssertionError("the cleanup service was invoked without authorized delete")
        records = tuple(self._record(volume_id) for volume_id in self.volume_ids)
        status = VolumeCleanupStatus.COMPLETED
        if self.kind == "retentions":
            status = VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS
        elif self.kind == "failed":
            status = VolumeCleanupStatus.FAILED
        successful = status in {
            VolumeCleanupStatus.COMPLETED,
            VolumeCleanupStatus.COMPLETED_WITH_SAFETY_RETENTIONS,
        }
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
            volumes=records,
            successful=successful,
            error=(
                None
                if successful
                else SafeError(
                    error_code=None,
                    error_type="RuntimeError",
                    message=f"cleanup failed for cluster {target.cluster_name}",
                )
            ),
        )


class _AbsentEks:
    """EKS double that records every lookup and proves the exact cluster is gone."""

    def __init__(self, region: str, calls: list[tuple[str, str]]) -> None:
        self._region = region
        self._calls = calls

    def describe_cluster(self, name: str) -> Mapping[str, object]:
        self._calls.append((self._region, name))
        raise ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "absent"}},
            "DescribeCluster",
        )


class _RecordingClients:
    """Client factory that records every Region-scoped client it creates."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.described: list[tuple[str, str]] = []

    def __call__(self, service_name: str, *, region_name: str) -> Any:
        self.created.append((service_name, region_name))
        return _AbsentEks(region_name, self.described)


class _RecordingCleanupService:
    """Cleanup service double returning one target's generated evidence."""

    def __init__(self, plans: Mapping[str, _TargetPlan]) -> None:
        self._plans = plans
        self.calls: list[
            tuple[RegionalVolumeTarget, ClusterAbsenceProof, VolumeCleanupRequest]
        ] = []

    def cleanup(
        self,
        *,
        target: RegionalVolumeTarget,
        absence: ClusterAbsenceProof,
        request: VolumeCleanupRequest,
    ) -> TargetVolumeCleanupOutcome:
        self.calls.append((target, absence, request))
        plan = self._plans[target.stack_name]
        if plan.kind == "raises":
            raise RuntimeError(f"EC2 discovery failed for cluster {target.cluster_name}")
        return plan.outcome(target, request)


@dataclass(frozen=True)
class _DestroyPlan:
    """One generated destroy-all run: its targets, extra stacks, and orderings."""

    plans: tuple[_TargetPlan, ...]
    non_regional: tuple[str, ...]
    barrier_order: tuple[str, ...]
    completion_order: tuple[str, ...]
    failed_also_reported_successful: bool
    strict: bool

    @property
    def by_stack(self) -> Mapping[str, _TargetPlan]:
        return {plan.stack_name: plan for plan in self.plans}

    @property
    def failed_stacks(self) -> tuple[str, ...]:
        return tuple(plan.stack_name for plan in self.plans if not plan.deleted)

    @property
    def successful_stacks(self) -> tuple[str, ...]:
        deleted = [plan.stack_name for plan in self.plans if plan.deleted]
        reported = [*deleted, *self.non_regional]
        if self.failed_also_reported_successful:
            reported.extend(self.failed_stacks)
        return tuple(stack for stack in self.completion_order if stack in set(reported))


@st.composite
def _destroy_plans(draw: st.DrawFn) -> _DestroyPlan:
    regions = draw(
        st.lists(
            st.sampled_from(_CONFIGURED_REGIONS),
            min_size=1,
            max_size=len(_CONFIGURED_REGIONS),
            unique=True,
        )
    )
    plans = tuple(
        _TargetPlan(
            region=region,
            deleted=draw(st.booleans()),
            kind=draw(st.sampled_from(_OUTCOME_KINDS)),
            volume_count=draw(st.integers(min_value=0, max_value=3)),
        )
        for region in regions
    )
    non_regional = tuple(
        draw(
            st.lists(
                st.sampled_from(_NON_REGIONAL_STACKS),
                max_size=len(_NON_REGIONAL_STACKS),
                unique=True,
            )
        )
    )
    every_stack = [*(plan.stack_name for plan in plans), *non_regional]
    barrier_order = tuple(draw(st.permutations(every_stack)))
    completion_order = tuple(draw(st.permutations(every_stack)))
    return _DestroyPlan(
        plans=plans,
        non_regional=non_regional,
        barrier_order=barrier_order,
        completion_order=completion_order,
        failed_also_reported_successful=draw(st.booleans()),
        strict=draw(st.booleans()),
    )


@pytest.fixture(scope="module")
def project_root(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Return a project root whose cdk.json configures every regional Region."""
    root = tmp_path_factory.mktemp("volume-cleanup-multi-target")
    (root / "cdk.json").write_text(
        '{"context": {"project_name": "gco", "deployment_regions": '
        '{"regional": ["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-2"]}}}',
        encoding="utf-8",
    )
    return root


def _strict_resources(plan: _DestroyPlan) -> dict[str, dict[str, str]]:
    """Return complete pre-destroy strict identity for every regional target."""
    return {
        target.stack_name: {
            "stack_name": target.stack_name,
            "stack_id": (
                f"arn:aws:cloudformation:{target.region}:123456789012:"
                f"stack/{target.stack_name}/abc123"
            ),
            "region": target.region,
            "cluster_name": target.stack_name,
        }
        for target in plan.plans
    }


def _run_barrier(
    project_root: Any,
    plan: _DestroyPlan,
    *,
    barrier_order: Sequence[str],
    completion_order: Sequence[str],
) -> tuple[bool, list[tuple[str, dict[str, Any]]], _RecordingClients, _RecordingCleanupService]:
    """Run the barrier once with all AWS-backed collaborators doubled."""
    config = MagicMock()
    config.project_name = _PROJECT
    config.global_region = "us-east-2"
    manager = StackManager(config, project_root=project_root)

    clients = _RecordingClients()
    service = _RecordingCleanupService(plan.by_stack)
    manager.set_volume_cleanup_dependencies(
        absence_verifier=ClusterAbsenceVerifier(clients, clock=lambda: _VERIFIED_AT),
        cleanup_service=cast(VolumeCleanupService, service),
    )

    strict_targets: dict[str, TargetResolution] | None = None
    if plan.strict:
        strict_targets = manager._capture_strict_volume_targets(
            regional_stacks=barrier_order,
            strict_resources=_strict_resources(plan),
        )

    published: list[tuple[str, dict[str, Any]]] = []

    def record_cleanup(name: str, details: dict[str, Any]) -> None:
        published.append((name, details))

    successful = [stack for stack in completion_order if stack in set(plan.successful_stacks)]
    cleanup_successful = manager._regional_volume_cleanup_barrier(
        regional_stacks=list(barrier_order),
        successful=successful,
        failed=list(plan.failed_stacks),
        request=_REQUEST,
        strict_targets=strict_targets,
        record_cleanup=record_cleanup,
    )
    return cleanup_successful, published, clients, service


@settings(max_examples=150, deadline=None)
@given(plan=_destroy_plans())
def test_multi_target_authorization_and_outcomes_remain_isolated(
    project_root: Any,
    plan: _DestroyPlan,
) -> None:
    # Feature: cleanup-dynamic-ebs-volumes, Property 12: Multi-target authorization
    # and outcomes remain isolated
    #
    # **Validates: Requirements 1.4, 3.4, 7.5**
    cleanup_successful, published, clients, service = _run_barrier(
        project_root,
        plan,
        barrier_order=plan.barrier_order,
        completion_order=plan.completion_order,
    )

    targets = plan.by_stack
    every_volume_id = {
        volume_id: target.stack_name for target in plan.plans for volume_id in target.volume_ids
    }

    # Requirement 1.4: the one resolved authorized-delete request reaches every
    # exact regional target whose stack deletion was definitive, and nothing else.
    assert [target.stack_name for target, _proof, _request in service.calls] == sorted(
        name for name, target in targets.items() if target.cleanup_attempted
    )
    for target, proof, request in service.calls:
        assert request is _REQUEST
        assert request.deletion_authorized is True
        assert target.region == target.stack_name.removeprefix(f"{_PROJECT}-")
        assert target.cluster_name == target.stack_name
        assert target.cluster_tag_key == _cluster_tag_key(target.region)
        # Requirement 3.4: absence evidence is bound to this target alone.
        assert proof.matches(target)

    # Requirement 3.4: the exact cluster is verified per target, in its own Region,
    # and only after that stack reached a definitive successful deletion.
    assert sorted(clients.described) == sorted(
        (target.region, target.stack_name) for target in plan.plans if target.cleanup_attempted
    )
    assert all(service_name == "eks" for service_name, _region in clients.created)

    # Requirement 7.2/7.5: a non-regional stack produces no outcome and no AWS work.
    assert set(clients.created).isdisjoint({("eks", stack) for stack in plan.non_regional})
    assert {name for name, _details in published} == (
        {EBS_VOLUME_CLEANUP_NAME} if published else set()
    )
    reported = [details["stack_name"] for _name, details in published]
    assert set(reported).isdisjoint(plan.non_regional)

    # Requirement 7.5: publication order is deterministic and complete.
    assert reported == sorted(targets)

    for details in (payload for _name, payload in published):
        stack_name = details["stack_name"]
        target_plan = targets[stack_name]

        # Requirement 1.4: authorization is identical for every target.
        assert details["policy"] == _REQUEST.policy.value
        assert details["deletion_authorized"] is True
        assert details["authorization_source"] == _REQUEST.authorization_source.value

        # Requirement 7.5: identity, records, reasons, and counts stay per target.
        # A helper failure with no captured identity reports none rather than
        # implying a Region or cluster it never authorized.
        if target_plan.kind == "raises" and target_plan.deleted and not plan.strict:
            assert details["target_region"] is None
            assert details["target_cluster"] is None
            assert details["cluster_tag_key"] is None
        else:
            assert details["target_region"] == target_plan.region
            assert details["target_cluster"] == stack_name
            assert details["cluster_tag_key"] == _cluster_tag_key(target_plan.region)
        records = details["volumes"]
        assert [record["volume_id"] for record in records] == sorted(
            target_plan.expected_volume_ids
        )
        for record in records:
            assert every_volume_id[record["volume_id"]] == stack_name
            assert record["region"] == target_plan.region
            assert record["availability_zone"].startswith(target_plan.region)
            for other in targets:
                if other != stack_name:
                    assert other not in str(record["reason"])
                    assert other not in str(record["follow_up"])
        counts = details["counts"]
        assert counts["discovered"] == len(records)
        assert (
            counts["deleted"]
            + counts["retained"]
            + counts["skipped"]
            + counts["already_absent"]
            + counts["failed"]
            == counts["discovered"]
        )

        # Requirement 3.4: no definitive deletion means a blocked, zero-volume report.
        blocking = target_plan.expected_blocking_reason_code
        if blocking is not None:
            assert details["status"] == VolumeCleanupStatus.SKIPPED.value
            assert details["blocking_reason_code"] == blocking
            assert records == []
        assert details["successful"] is target_plan.expected_successful

    # Requirement 7.5: one target's failure never changes another target's result,
    # and aggregation reflects every target's own published outcome.
    assert cleanup_successful is all(target.expected_successful for target in plan.plans)

    # Requirement 7.5: a different worker completion order publishes the same
    # sequence of outcomes, so parallel and sequential destruction agree.
    _replayed_successful, replayed, _clients, _service = _run_barrier(
        project_root,
        plan,
        barrier_order=tuple(reversed(plan.barrier_order)),
        completion_order=tuple(reversed(plan.completion_order)),
    )
    assert replayed == published
    assert _replayed_successful is cleanup_successful
