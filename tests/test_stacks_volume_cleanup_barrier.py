"""Focused tests for the orchestrated regional EBS volume-cleanup barrier.

These cover the barrier itself: where it runs relative to the regional workers,
watchdog finalization, and global destruction; how it publishes one deterministic
``ebs-volumes`` outcome per exact regional target; and how it aggregates cleanup
failure without relabeling a stack that was deleted.
"""

import inspect
import json
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from cli.stacks import StackManager
from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    SafeError,
    TargetVolumeCleanupOutcome,
    VolumeCleanupRequest,
    VolumeCleanupStatus,
    VolumePolicy,
)

_PROJECT = "gco"
_EAST = "gco-us-east-1"
_WEST = "gco-us-west-2"
_GLOBAL = "gco-global"
_REGIONS = ("us-east-1", "us-west-2")
_EAST_ID = f"arn:aws:cloudformation:us-east-1:123456789012:stack/{_EAST}/abc"
_GLOBAL_ID = "arn:aws:cloudformation:us-east-2:123456789012:stack/gco-global/def"

_DELETE_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.DELETE,
    deletion_authorized=True,
    authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
)


def write_cdk_json(project_root) -> None:
    (project_root / "cdk.json").write_text(
        json.dumps(
            {
                "context": {
                    "project_name": _PROJECT,
                    "deployment_regions": {"regional": list(_REGIONS)},
                }
            }
        ),
        encoding="utf-8",
    )


def make_manager(project_root) -> StackManager:
    config = MagicMock()
    config.project_name = _PROJECT
    config.global_region = "us-east-2"
    return StackManager(config, project_root=project_root)


def completed_outcome(stack_name: str, region: str) -> TargetVolumeCleanupOutcome:
    return TargetVolumeCleanupOutcome(
        stack_name=stack_name,
        stack_id=None,
        target_region=region,
        target_cluster=stack_name,
        cluster_tag_key=f"kubernetes.io/cluster/{stack_name}",
        policy=_DELETE_REQUEST.policy,
        deletion_authorized=_DELETE_REQUEST.deletion_authorized,
        authorization_source=_DELETE_REQUEST.authorization_source,
        status=VolumeCleanupStatus.COMPLETED,
        successful=True,
    )


def failed_outcome(stack_name: str, region: str) -> TargetVolumeCleanupOutcome:
    return TargetVolumeCleanupOutcome(
        stack_name=stack_name,
        stack_id=None,
        target_region=region,
        target_cluster=stack_name,
        cluster_tag_key=f"kubernetes.io/cluster/{stack_name}",
        policy=_DELETE_REQUEST.policy,
        deletion_authorized=_DELETE_REQUEST.deletion_authorized,
        authorization_source=_DELETE_REQUEST.authorization_source,
        status=VolumeCleanupStatus.FAILED,
        successful=False,
        error=SafeError(
            error_code=None,
            error_type="RuntimeError",
            message="discovery was incomplete",
        ),
    )


def cluster_not_found(**_kwargs):
    raise ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "absent"}},
        "DescribeCluster",
    )


def run_orchestrated(
    manager,
    *,
    stacks=(_GLOBAL, _EAST, _WEST),
    destroy_results=None,
    request=_DELETE_REQUEST,
    cleanup=None,
    expected_stack_ids=None,
    events=None,
):
    """Run ``destroy_orchestrated`` with every AWS-backed collaborator patched.

    ``events`` records the ordering-relevant steps: each stack deletion, each
    watchdog security-group finalization, and each published cleanup name.
    """
    recorded: list[tuple[str, str]] = [] if events is None else events
    cleanups: list[tuple[str, dict]] = []
    results = dict(destroy_results or {})

    def fake_destroy(*, stack_name, **_kwargs):
        recorded.append(("destroy", stack_name))
        return results.get(stack_name, True)

    def record_cleanup(name, details):
        recorded.append(("cleanup", name))
        cleanups.append((name, details))

    def fake_security_groups(stack_name, **_kwargs):
        recorded.append(("watchdog", stack_name))
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
        patch.object(
            StackManager,
            "_collect_implicit_log_groups",
            return_value={_EAST: {"region": "us-east-1", "log_groups": ["/aws/lambda/x"]}},
        ),
        patch.object(
            StackManager,
            "_cleanup_implicit_log_groups",
            return_value={"deleted": [], "missing": [], "errors": []},
        ),
        patch.object(StackManager, "destroy", side_effect=fake_destroy),
    ]
    if cleanup is not None:
        patches.append(
            patch.object(
                StackManager,
                "cleanup_regional_volumes_after_destroy",
                side_effect=cleanup,
            )
        )

    kwargs = {}
    if request is not None:
        kwargs["volume_cleanup_request"] = request
    if expected_stack_ids is not None:
        kwargs["expected_stack_ids"] = expected_stack_ids
        kwargs["authorize_stack"] = lambda *_args: None

    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        result = manager.destroy_orchestrated(
            force=True,
            on_cleanup_complete=record_cleanup,
            **kwargs,
        )
    return result, recorded, cleanups


def volume_outcomes(cleanups):
    return {payload["stack_name"]: payload for name, payload in cleanups if name == "ebs-volumes"}


def test_destroy_orchestrated_accepts_one_resolved_request_without_requiring_it():
    signature = inspect.signature(StackManager.destroy_orchestrated)

    assert signature.parameters["volume_cleanup_request"].default is None


def test_no_request_keeps_the_existing_stack_only_path(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)
    cleanup = MagicMock()

    (overall, successful, failed), _events, cleanups = run_orchestrated(
        manager,
        request=None,
        cleanup=cleanup,
    )

    assert overall is True
    assert failed == []
    assert sorted(successful) == sorted([_EAST, _WEST, _GLOBAL])
    cleanup.assert_not_called()
    assert volume_outcomes(cleanups) == {}


def test_barrier_publishes_one_outcome_per_target_after_workers_and_before_global(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)
    calls: list[dict] = []

    def cleanup(*, stack_name, stack_deleted, request, strict_target=None):
        calls.append(
            {
                "stack_name": stack_name,
                "stack_deleted": stack_deleted,
                "request": request,
                "strict_target": strict_target,
            }
        )
        return completed_outcome(stack_name, stack_name.removeprefix(f"{_PROJECT}-"))

    (overall, _successful, failed), events, cleanups = run_orchestrated(manager, cleanup=cleanup)

    assert overall is True and failed == []
    # One outcome per exact regional target, in one deterministic order that does
    # not depend on the order the regional workers completed in.
    assert [call["stack_name"] for call in calls] == [_EAST, _WEST]
    assert [call["request"] for call in calls] == [_DELETE_REQUEST, _DELETE_REQUEST]
    assert all(call["stack_deleted"] is True for call in calls)
    assert sorted(volume_outcomes(cleanups)) == [_EAST, _WEST]

    last_regional_destroy = max(
        index
        for index, event in enumerate(events)
        if event in {("destroy", _EAST), ("destroy", _WEST)}
    )
    last_watchdog = max(index for index, event in enumerate(events) if event[0] == "watchdog")
    first_publication = min(
        index for index, event in enumerate(events) if event == ("cleanup", "ebs-volumes")
    )
    global_destroy = events.index(("destroy", _GLOBAL))

    assert last_regional_destroy < last_watchdog < first_publication < global_destroy


def test_published_details_carry_the_complete_serialized_outcome(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)

    def cleanup(*, stack_name, stack_deleted, request, strict_target=None):
        return completed_outcome(stack_name, "us-east-1") if stack_name == _EAST else None

    _result, _events, cleanups = run_orchestrated(manager, cleanup=cleanup)

    published = volume_outcomes(cleanups)
    assert list(published) == [_EAST]
    details = published[_EAST]
    assert details["target_region"] == "us-east-1"
    assert details["cluster_tag_key"] == f"kubernetes.io/cluster/{_EAST}"
    assert details["status"] == VolumeCleanupStatus.COMPLETED.value
    assert details["policy"] == VolumePolicy.DELETE.value
    assert details["deletion_authorized"] is True
    assert details["counts"]["discovered"] == 0
    assert details["volumes"] == []


def test_failed_stack_keeps_stack_results_and_is_never_treated_as_deleted(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)
    calls: list[tuple[str, bool]] = []

    def cleanup(*, stack_name, stack_deleted, request, strict_target=None):
        calls.append((stack_name, stack_deleted))
        return completed_outcome(stack_name, stack_name.removeprefix(f"{_PROJECT}-"))

    (overall, successful, failed), events, _cleanups = run_orchestrated(
        manager,
        destroy_results={_EAST: False},
        cleanup=cleanup,
    )

    assert overall is False
    assert failed == [_EAST]
    # The stack that did delete stays a stack success and is never relabeled.
    assert _WEST in successful
    assert dict(calls) == {_EAST: False, _WEST: True}
    assert ("destroy", _GLOBAL) not in events


def test_failed_stack_publishes_a_blocked_outcome_without_any_eks_or_ec2_client(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)
    factory = MagicMock()
    service = MagicMock()
    manager.set_volume_cleanup_dependencies(client_factory=factory, cleanup_service=service)

    (overall, _successful, failed), _events, cleanups = run_orchestrated(
        manager,
        stacks=(_GLOBAL, _EAST),
        destroy_results={_EAST: False},
    )

    assert overall is False and failed == [_EAST]
    factory.assert_not_called()
    service.cleanup.assert_not_called()

    blocked = volume_outcomes(cleanups)[_EAST]
    assert blocked["status"] == VolumeCleanupStatus.SKIPPED.value
    assert blocked["blocking_reason_code"] == "stack-deletion-unverified"
    assert blocked["successful"] is False
    assert blocked["volumes"] == []


def test_cleanup_failure_alone_fails_overall_without_relabeling_stacks(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)

    def cleanup(*, stack_name, stack_deleted, request, strict_target=None):
        region = stack_name.removeprefix(f"{_PROJECT}-")
        if stack_name == _WEST:
            return failed_outcome(stack_name, region)
        return completed_outcome(stack_name, region)

    (overall, successful, failed), events, cleanups = run_orchestrated(manager, cleanup=cleanup)

    assert overall is False
    assert failed == []
    assert sorted(successful) == sorted([_EAST, _WEST])
    # Unsuccessful cleanup blocks progression to the global stacks.
    assert ("destroy", _GLOBAL) not in events
    # Every exit still routes through finish(...), so the implicit log-group
    # sweep for the stacks that did delete still runs.
    assert "implicit-log-groups" in {name for name, _ in cleanups}


def test_one_target_failure_does_not_stop_another_target(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)

    def cleanup(*, stack_name, stack_deleted, request, strict_target=None):
        if stack_name == _EAST:
            raise RuntimeError("EC2 discovery exploded")
        return completed_outcome(stack_name, "us-west-2")

    (overall, _successful, failed), _events, cleanups = run_orchestrated(manager, cleanup=cleanup)

    assert overall is False
    assert failed == []
    outcomes = volume_outcomes(cleanups)
    assert sorted(outcomes) == [_EAST, _WEST]
    assert outcomes[_EAST]["blocking_reason_code"] == "cleanup-helper-error"
    assert outcomes[_WEST]["status"] == VolumeCleanupStatus.COMPLETED.value


def test_regional_phase_stack_outside_the_configuration_publishes_no_outcome(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)
    eks = MagicMock()
    eks.describe_cluster.side_effect = cluster_not_found
    factory = MagicMock(return_value=eks)
    service = MagicMock()
    service.cleanup.side_effect = lambda *, target, absence, request: completed_outcome(
        target.stack_name, target.region
    )
    manager.set_volume_cleanup_dependencies(client_factory=factory, cleanup_service=service)

    (overall, _successful, failed), _events, cleanups = run_orchestrated(
        manager,
        stacks=(_GLOBAL, _EAST, "gco-monitoring", "gco-experiment"),
    )

    assert overall is True and failed == []
    assert list(volume_outcomes(cleanups)) == [_EAST]
    assert [call.kwargs["target"].stack_name for call in service.cleanup.call_args_list] == [_EAST]


def test_strict_targets_are_captured_before_any_deletion(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)
    strict_resources = {
        _EAST: {
            "stack_name": _EAST,
            "stack_id": _EAST_ID,
            "region": "us-east-1",
            "cluster_name": _EAST,
        }
    }
    events: list[tuple[str, str]] = []
    calls: list[dict] = []
    original_capture = StackManager._capture_strict_volume_targets

    def cleanup(*, stack_name, stack_deleted, request, strict_target=None):
        calls.append({"stack_name": stack_name, "strict_target": strict_target})
        return completed_outcome(stack_name, "us-east-1")

    def capture_strict(**kwargs):
        events.append(("capture", "strict-volume-targets"))
        return original_capture(manager, **kwargs)

    with (
        patch.object(
            StackManager,
            "_resolve_strict_teardown_resources",
            return_value=strict_resources,
        ),
        patch.object(
            StackManager,
            "_capture_strict_volume_targets",
            side_effect=capture_strict,
        ),
    ):
        (overall, _successful, failed), recorded, _cleanups = run_orchestrated(
            manager,
            stacks=(_GLOBAL, _EAST),
            cleanup=cleanup,
            expected_stack_ids={_EAST: _EAST_ID, _GLOBAL: _GLOBAL_ID},
            events=events,
        )

    assert overall is True and failed == []
    first_destroy = min(index for index, event in enumerate(recorded) if event[0] == "destroy")
    assert recorded.index(("capture", "strict-volume-targets")) < first_destroy
    assert calls[0]["strict_target"] == RegionalVolumeTarget(
        stack_name=_EAST,
        stack_id=_EAST_ID,
        region="us-east-1",
        cluster_name=_EAST,
        cluster_tag_key=f"kubernetes.io/cluster/{_EAST}",
    )


def test_strict_capture_blocks_a_missing_or_ambiguous_identity(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)

    captured = manager._capture_strict_volume_targets(
        regional_stacks=[_EAST, _WEST],
        strict_resources={
            _WEST: {
                "stack_name": _WEST,
                "stack_id": f"arn:aws:cloudformation:us-west-2:123456789012:stack/{_WEST}/a",
                "region": "us-west-2",
                "cluster_identity_error": "ambiguous EKS physical IDs",
            }
        },
    )

    assert captured[_EAST].reason_code == "missing-strict-resource-identity"
    assert captured[_WEST].reason_code == "strict-cluster-identity-unresolved"


def test_blocked_strict_capture_publishes_a_blocked_outcome_without_aws(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)
    factory = MagicMock()
    service = MagicMock()
    manager.set_volume_cleanup_dependencies(client_factory=factory, cleanup_service=service)

    with patch.object(
        StackManager,
        "_resolve_strict_teardown_resources",
        return_value={},
    ):
        (overall, _successful, failed), _events, cleanups = run_orchestrated(
            manager,
            stacks=(_GLOBAL, _EAST),
            expected_stack_ids={_EAST: _EAST_ID, _GLOBAL: _GLOBAL_ID},
        )

    assert overall is False
    assert failed == []
    outcomes = volume_outcomes(cleanups)
    assert list(outcomes) == [_EAST]
    assert outcomes[_EAST]["blocking_reason_code"] == "missing-strict-resource-identity"
    factory.assert_not_called()
    service.cleanup.assert_not_called()
