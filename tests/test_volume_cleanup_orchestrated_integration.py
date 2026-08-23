"""End-to-end mocked integration and regression tests for orchestrated EBS cleanup.

These tests drive the real ``StackManager.destroy_orchestrated`` barrier against the
real ``ClusterAbsenceVerifier`` and ``VolumeCleanupService``, with only the CDK/AWS
collaborators replaced by journaling doubles. They cover what the focused helper,
barrier, and CLI modules cannot see:

* parallel regional stack deletion, where workers finish in an order that differs
  from the deterministic per-target callback order;
* sequential and parallel destruction publishing byte-identical outcomes;
* mixed regional success/failure combined with non-regional exclusion, proving a
  failed stack reaches neither EKS nor EC2 while its Region's volumes survive; and
* partial-teardown retries, where a second attempt processes only the targets whose
  absence is now verified and repeats no action on already-disposed or unrelated
  volumes.

Every AWS interaction is a double: no live AWS client is created.

Requirements: 3.1, 3.2, 3.3, 3.4, 5.1, 5.2, 5.3, 5.4, 5.5, 6.4, 7.1, 7.2, 7.5
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cli.stacks import StackManager
from cli.volume_cleanup import (
    ClusterAbsenceVerifier,
    DeletionAuthorizationSource,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupRequest,
    VolumeCleanupService,
    VolumeCleanupStatus,
    VolumePolicy,
    VolumeReasonCode,
)
from tests.test_volume_cleanup_ec2_integration import FakeAWS, FakeEC2, dto, throttled

_PROJECT = "gco"
_EAST = "us-east-1"
_WEST = "us-west-2"
_IRELAND = "eu-west-1"
_REGIONS = (_EAST, _WEST, _IRELAND)
_GLOBAL_STACK = f"{_PROJECT}-global"
_ANALYTICS_STACK = f"{_PROJECT}-analytics"
_VERIFIED_AT = datetime(2026, 3, 4, 12, 30, tzinfo=UTC)

_DESTROY_ALL_DELETE = VolumeCleanupRequest(
    policy=VolumePolicy.DELETE,
    deletion_authorized=True,
    authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
)


def stack_for(region: str) -> str:
    return f"{_PROJECT}-{region}"


def tag_key_for(region: str) -> str:
    return f"kubernetes.io/cluster/{stack_for(region)}"


def owned_volume_id(region: str) -> str:
    return f"vol-0{region.replace('-', '')}owned"


def unrelated_volume_id(region: str) -> str:
    return f"vol-0{region.replace('-', '')}other"


def owned_dto(region: str) -> dict[str, object]:
    """Return one deletion-eligible volume owned by the Region's exact cluster."""
    return dto(
        owned_volume_id(region),
        key=tag_key_for(region),
        zone=f"{region}a",
    )


def unrelated_dto(region: str) -> dict[str, object]:
    """Return one volume in the same Region that no target cluster tag claims."""
    return dto(
        unrelated_volume_id(region),
        key="kubernetes.io/cluster/other-project-cluster",
        zone=f"{region}a",
    )


def write_cdk_json(project_root: Path, *, regional: Sequence[str] = _REGIONS) -> None:
    (project_root / "cdk.json").write_text(
        json.dumps(
            {
                "context": {
                    "project_name": _PROJECT,
                    "deployment_regions": {
                        "global": "us-east-2",
                        "api_gateway": "us-east-2",
                        "monitoring": "us-east-2",
                        "regional": list(regional),
                    },
                }
            }
        ),
        encoding="utf-8",
    )


def build_aws(regions: Sequence[str]) -> tuple[FakeAWS, dict[str, FakeEC2]]:
    """Return one journaling client factory plus a Region-scoped EC2 double each.

    Each Region starts with one deletion-eligible owned volume and one unrelated
    volume, and no cluster exists in any Region, so absence is verified exactly.
    """
    aws = FakeAWS()
    clients: dict[str, FakeEC2] = {}
    for region in regions:
        volumes = {
            owned_volume_id(region): owned_dto(region),
            unrelated_volume_id(region): unrelated_dto(region),
        }
        client = FakeEC2(region, aws.journal, volumes)
        aws.ec2[region] = client
        clients[region] = client
    return aws, clients


def make_manager(project_root: Path, aws: FakeAWS) -> StackManager:
    """Build a manager whose volume cleanup uses the real service over the doubles."""
    config = MagicMock()
    config.project_name = _PROJECT
    config.global_region = "us-east-2"
    manager = StackManager(config, project_root=project_root)
    manager.set_volume_cleanup_dependencies(
        client_factory=aws,
        absence_verifier=ClusterAbsenceVerifier(aws, clock=lambda: _VERIFIED_AT),
        cleanup_service=VolumeCleanupService(aws),
    )
    return manager


@dataclass
class Teardown:
    """One orchestrated teardown attempt's results and ordered observations."""

    overall: bool
    successful: list[str]
    failed: list[str]
    events: list[tuple[str, str]] = field(default_factory=list)
    cleanups: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    @property
    def volume_outcomes(self) -> dict[str, dict[str, Any]]:
        return {
            details["stack_name"]: details
            for name, details in self.cleanups
            if name == "ebs-volumes"
        }

    @property
    def published_details(self) -> list[dict[str, Any]]:
        return [details for name, details in self.cleanups if name == "ebs-volumes"]

    @property
    def publication_order(self) -> list[str]:
        return [details["stack_name"] for details in self.published_details]

    def ordered(self, kind: str) -> list[str]:
        return [name for event_kind, name in self.events if event_kind == kind]

    def index(self, kind: str, name: str) -> int:
        return self.events.index((kind, name))


def run_teardown(
    manager: StackManager,
    *,
    stacks: Sequence[str],
    request: VolumeCleanupRequest | None = _DESTROY_ALL_DELETE,
    parallel: bool = False,
    destroy_results: Mapping[str, bool] | None = None,
    completion_order: Sequence[str] | None = None,
) -> Teardown:
    """Run one orchestrated teardown with every CDK/AWS collaborator replaced.

    ``completion_order`` forces stack workers to finish in exactly that order, so a
    parallel attempt can complete its regional stacks in an order that differs from
    the deterministic cleanup-publication order without relying on timing.
    """
    results = dict(destroy_results or {})
    lock = Lock()
    events: list[tuple[str, str]] = []
    cleanups: list[tuple[str, dict[str, Any]]] = []
    gates: dict[str, Event] = {}
    order = list(completion_order or ())
    if order:
        gates = {name: Event() for name in order}
        gates[order[0]].set()

    def record(kind: str, name: str) -> None:
        with lock:
            events.append((kind, name))

    def fake_destroy(*, stack_name: str, **_kwargs: Any) -> bool:
        gate = gates.get(stack_name)
        if gate is not None and not gate.wait(timeout=10):
            raise AssertionError(f"the scripted completion gate for {stack_name} never opened")
        record("destroy", stack_name)
        return results.get(stack_name, True)

    def on_stack_complete(stack_name: str, _success: bool) -> None:
        record("complete", stack_name)
        if stack_name in gates:
            position = order.index(stack_name) + 1
            if position < len(order):
                gates[order[position]].set()

    def record_cleanup(name: str, details: dict[str, Any]) -> None:
        with lock:
            events.append(("cleanup", name))
            cleanups.append((name, details))

    def fake_security_groups(stack_name: str, **_kwargs: Any) -> dict[str, Any]:
        record("watchdog", stack_name)
        return {"errors": [], "blocked_by_enis": []}

    patches = [
        patch.object(StackManager, "list_stacks", return_value=list(stacks)),
        patch.object(StackManager, "_image_registry_destroy_preflight", return_value=True),
        patch.object(StackManager, "cleanup_orphaned_bastions", return_value=0),
        patch.object(StackManager, "_cleanup_bastion_iam", return_value={"errors": []}),
        patch.object(StackManager, "_cleanup_backup_vault", return_value={"errors": []}),
        patch.object(StackManager, "_start_eks_sg_watchdog", return_value=MagicMock()),
        patch.object(
            StackManager,
            "_cleanup_eks_security_groups",
            side_effect=fake_security_groups,
        ),
        patch.object(StackManager, "_destroy_phase_remaining_stacks", return_value=[]),
        patch.object(StackManager, "_collect_implicit_log_groups", return_value={}),
        patch.object(StackManager, "destroy", side_effect=fake_destroy),
    ]
    kwargs: dict[str, Any] = {}
    if request is not None:
        kwargs["volume_cleanup_request"] = request

    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        overall, successful, failed = manager.destroy_orchestrated(
            force=True,
            parallel=parallel,
            on_stack_complete=on_stack_complete,
            on_cleanup_complete=record_cleanup,
            **kwargs,
        )
    return Teardown(
        overall=overall,
        successful=successful,
        failed=failed,
        events=events,
        cleanups=cleanups,
    )


def region_entries(aws: FakeAWS, region: str) -> list[str]:
    """Return every journaled request scoped to one Region, in call order."""
    return [
        entry for entry in aws.journal if entry.endswith(f":{region}") or f":{region}:" in entry
    ]


def deleted_record(details: Mapping[str, Any], volume_id: str) -> Mapping[str, Any]:
    return next(record for record in details["volumes"] if record["volume_id"] == volume_id)


@dataclass
class Scenario:
    """One prepared deployment: journaling clients plus a wired stack manager."""

    aws: FakeAWS
    clients: dict[str, FakeEC2]
    manager: StackManager


def scenario(project_root: Path, regions: Sequence[str] = _REGIONS) -> Scenario:
    write_cdk_json(project_root, regional=regions)
    aws, clients = build_aws(regions)
    return Scenario(aws=aws, clients=clients, manager=make_manager(project_root, aws))


@pytest.fixture
def three_regions(tmp_path: Path) -> Scenario:
    """Provide three exact regional targets with the real cleanup service wired."""
    return scenario(tmp_path)


def test_parallel_workers_finish_out_of_order_but_publication_stays_deterministic(
    three_regions: Scenario,
) -> None:
    aws, clients, manager = three_regions.aws, three_regions.clients, three_regions.manager
    # Force a worker completion order that neither the deterministic publication
    # order nor the sequential destruction order could produce.
    completion = [stack_for(_EAST), stack_for(_IRELAND), stack_for(_WEST)]

    result = run_teardown(
        manager,
        stacks=[_GLOBAL_STACK, *(stack_for(region) for region in _REGIONS)],
        parallel=True,
        completion_order=completion,
    )

    assert result.overall is True and result.failed == []
    regional = {stack_for(region) for region in _REGIONS}
    assert [name for name in result.ordered("complete") if name in regional] == completion
    # One outcome per target, always in sorted target order, never worker order.
    assert result.publication_order == sorted(regional)
    assert result.publication_order != completion
    for region in _REGIONS:
        details = result.volume_outcomes[stack_for(region)]
        assert details["target_region"] == region
        assert details["target_cluster"] == stack_for(region)
        assert details["cluster_tag_key"] == tag_key_for(region)
        assert details["status"] == VolumeCleanupStatus.COMPLETED.value
        assert details["counts"] == {
            "discovered": 1,
            "deleted": 1,
            "retained": 0,
            "skipped": 0,
            "already_absent": 0,
            "failed": 0,
        }
        # Each target's records carry only that Region's own volume.
        assert [record["volume_id"] for record in details["volumes"]] == [owned_volume_id(region)]
        record = deleted_record(details, owned_volume_id(region))
        assert record["action"] == VolumeAction.DELETE_REQUESTED.value
        assert record["action_result"] == VolumeActionResult.SUCCESS.value
        assert record["reason_code"] == VolumeReasonCode.DELETE_REQUEST_ACCEPTED.value
        # The exact owned volume is gone; the untagged neighbour is untouched.
        assert owned_volume_id(region) not in clients[region].volumes
        assert unrelated_volume_id(region) in clients[region].volumes
        assert f"ec2:{region}:delete:{unrelated_volume_id(region)}" not in aws.journal


def test_parallel_cleanup_runs_after_every_worker_and_watchdog_before_global(
    three_regions: Scenario,
) -> None:
    result = run_teardown(
        three_regions.manager,
        stacks=[_GLOBAL_STACK, *(stack_for(region) for region in _REGIONS)],
        parallel=True,
        completion_order=[stack_for(_IRELAND), stack_for(_WEST), stack_for(_EAST)],
    )

    assert result.overall is True
    regional_destroys = [
        index
        for index, (kind, name) in enumerate(result.events)
        if kind == "destroy" and name != _GLOBAL_STACK
    ]
    watchdogs = [index for index, (kind, _name) in enumerate(result.events) if kind == "watchdog"]
    publications = [
        index for index, event in enumerate(result.events) if event == ("cleanup", "ebs-volumes")
    ]

    assert len(regional_destroys) == len(_REGIONS)
    assert len(watchdogs) == len(_REGIONS)
    assert max(regional_destroys) < min(watchdogs)
    assert max(watchdogs) < min(publications)
    assert max(publications) < result.index("destroy", _GLOBAL_STACK)


def test_sequential_and_parallel_destruction_publish_identical_outcomes(tmp_path: Path) -> None:
    stacks = [_GLOBAL_STACK, *(stack_for(region) for region in _REGIONS)]
    published: dict[str, list[dict[str, Any]]] = {}

    for mode, project_root in (("sequential", tmp_path / "seq"), ("parallel", tmp_path / "par")):
        project_root.mkdir()
        result = run_teardown(
            scenario(project_root).manager,
            stacks=stacks,
            parallel=mode == "parallel",
            completion_order=(
                [stack_for(_WEST), stack_for(_IRELAND), stack_for(_EAST)]
                if mode == "parallel"
                else None
            ),
        )
        assert result.overall is True, mode
        published[mode] = result.published_details

    assert published["parallel"] == published["sequential"]


def test_mixed_regional_outcomes_exclude_non_regional_stacks_and_spare_failed_regions(
    tmp_path: Path,
) -> None:
    prepared = scenario(tmp_path, (_EAST, _WEST))
    aws, clients = prepared.aws, prepared.clients

    result = run_teardown(
        prepared.manager,
        # The broad regional phase also contains the non-regional analytics stack.
        stacks=[_GLOBAL_STACK, _ANALYTICS_STACK, stack_for(_EAST), stack_for(_WEST)],
        destroy_results={stack_for(_EAST): False},
    )

    assert result.overall is False
    assert result.failed == [stack_for(_EAST)]
    assert stack_for(_WEST) in result.successful
    assert _ANALYTICS_STACK in result.successful
    # The non-regional stack in the regional phase reports no volume outcome at all.
    assert result.publication_order == [stack_for(_EAST), stack_for(_WEST)]

    blocked = result.volume_outcomes[stack_for(_EAST)]
    assert blocked["status"] == VolumeCleanupStatus.SKIPPED.value
    assert blocked["blocking_reason_code"] == "stack-deletion-unverified"
    assert blocked["successful"] is False
    assert blocked["volumes"] == []
    # No EKS or EC2 request was made for the Region whose stack remains, and its
    # volumes are all preserved.
    assert region_entries(aws, _EAST) == []
    assert set(clients[_EAST].volumes) == {owned_volume_id(_EAST), unrelated_volume_id(_EAST)}

    completed = result.volume_outcomes[stack_for(_WEST)]
    assert completed["status"] == VolumeCleanupStatus.COMPLETED.value
    assert completed["successful"] is True
    assert [record["volume_id"] for record in completed["volumes"]] == [owned_volume_id(_WEST)]
    assert owned_volume_id(_WEST) not in clients[_WEST].volumes
    assert region_entries(aws, _WEST) == [
        f"client:eks:{_WEST}",
        f"eks:{_WEST}:describe_cluster:{stack_for(_WEST)}",
        f"client:ec2:{_WEST}",
        f"ec2:{_WEST}:discover:{tag_key_for(_WEST)}",
        f"ec2:{_WEST}:page:0",
        f"client:ec2:{_WEST}",
        f"ec2:{_WEST}:recheck:{owned_volume_id(_WEST)}",
        f"ec2:{_WEST}:delete:{owned_volume_id(_WEST)}",
    ]
    # A cleanup-blocked regional target never lets destruction reach the globals.
    assert ("destroy", _GLOBAL_STACK) not in result.events


def test_retry_after_partial_teardown_only_processes_newly_verified_targets(
    tmp_path: Path,
) -> None:
    prepared = scenario(tmp_path, (_EAST, _WEST))
    aws, clients, manager = prepared.aws, prepared.clients, prepared.manager
    stacks = [_GLOBAL_STACK, stack_for(_EAST), stack_for(_WEST)]

    first = run_teardown(manager, stacks=stacks, destroy_results={stack_for(_EAST): False})

    assert first.overall is False and first.failed == [stack_for(_EAST)]
    assert first.volume_outcomes[stack_for(_EAST)]["blocking_reason_code"] == (
        "stack-deletion-unverified"
    )
    assert region_entries(aws, _EAST) == []
    assert first.volume_outcomes[stack_for(_WEST)]["counts"]["deleted"] == 1
    assert owned_volume_id(_WEST) not in clients[_WEST].volumes
    after_first_attempt = len(aws.journal)

    # The operator retries; the previously failed stack is now gone, so its
    # cluster absence is verified for the first time and only its volumes are
    # processed. The Region that already finished re-reads current evidence.
    second = run_teardown(manager, stacks=stacks)
    retry_journal = aws.journal[after_first_attempt:]

    assert second.overall is True and second.failed == []
    east = second.volume_outcomes[stack_for(_EAST)]
    assert east["status"] == VolumeCleanupStatus.COMPLETED.value
    assert [record["volume_id"] for record in east["volumes"]] == [owned_volume_id(_EAST)]
    assert east["counts"]["deleted"] == 1
    assert owned_volume_id(_EAST) not in clients[_EAST].volumes

    west = second.volume_outcomes[stack_for(_WEST)]
    assert west["status"] == VolumeCleanupStatus.COMPLETED.value
    assert west["successful"] is True
    assert west["volumes"] == []
    assert west["counts"]["discovered"] == 0
    # The completed Region re-discovered current evidence on the retry but issued
    # no second recheck or deletion, and no unrelated volume was ever touched.
    assert f"ec2:{_WEST}:discover:{tag_key_for(_WEST)}" in retry_journal
    assert not [entry for entry in retry_journal if entry.startswith(f"ec2:{_WEST}:recheck:")]
    assert not [entry for entry in retry_journal if entry.startswith(f"ec2:{_WEST}:delete:")]
    assert [entry for entry in aws.journal if ":delete:" in entry] == [
        f"ec2:{_WEST}:delete:{owned_volume_id(_WEST)}",
        f"ec2:{_EAST}:delete:{owned_volume_id(_EAST)}",
    ]
    for region in (_EAST, _WEST):
        assert set(clients[region].volumes) == {unrelated_volume_id(region)}
    assert ("destroy", _GLOBAL_STACK) in second.events


def test_failed_volume_cleanup_withholds_the_success_only_traffic_dial_purge(
    tmp_path: Path,
) -> None:
    """An unaccounted volume is an incomplete teardown, so the dial purge waits.

    ``destroy_orchestrated`` funnels every exit through ``finish(overall)``, and the
    runtime traffic-dial SSM purge inside it is success-only. Volume cleanup feeds
    that same ``overall``, so these two independent teardown sweeps meet here: a
    Region whose volumes could not be disposed of must hold the purge back exactly
    as a surviving stack does, without being reported as a failed stack.
    """
    prepared = scenario(tmp_path, (_EAST, _WEST))
    # Every stack deletes cleanly and only the EC2 deletion fails, so the
    # unsuccessful cleanup is the single reason this teardown is not complete.
    prepared.clients[_EAST].delete_errors[owned_volume_id(_EAST)] = throttled("DeleteVolume")

    with patch.object(
        StackManager,
        "_cleanup_traffic_dial_parameters",
        return_value={"deleted": [], "errors": []},
    ) as dial:
        result = run_teardown(
            prepared.manager,
            stacks=[_GLOBAL_STACK, stack_for(_EAST), stack_for(_WEST)],
        )

    assert result.overall is False
    # Stack results stay untouched: an unsuccessful cleanup never fails a stack
    # nor relabels one that was deleted.
    assert result.failed == []
    assert stack_for(_EAST) in result.successful
    assert result.volume_outcomes[stack_for(_EAST)]["successful"] is False
    # One target's failure never suppresses another's disposal.
    assert result.volume_outcomes[stack_for(_WEST)]["successful"] is True
    assert owned_volume_id(_WEST) not in prepared.clients[_WEST].volumes
    # The barrier gates the global phase, so teardown stopped before it.
    assert ("destroy", _GLOBAL_STACK) not in result.events
    dial.assert_not_called()


def test_complete_teardown_purges_the_dial_tree_after_every_volume_is_disposed(
    tmp_path: Path,
) -> None:
    """The positive control for the seam above: nothing left, so the purge runs."""
    prepared = scenario(tmp_path, (_EAST, _WEST))

    with patch.object(
        StackManager,
        "_cleanup_traffic_dial_parameters",
        return_value={"deleted": [], "errors": []},
    ) as dial:
        result = run_teardown(
            prepared.manager,
            stacks=[_GLOBAL_STACK, stack_for(_EAST), stack_for(_WEST)],
        )

    assert result.overall is True and result.failed == []
    assert all(details["successful"] is True for details in result.published_details)
    assert ("destroy", _GLOBAL_STACK) in result.events
    dial.assert_called_once()
    assert "traffic-dial-parameters" in {name for name, _details in result.cleanups}
