"""Focused tests for ``StackManager.cleanup_regional_volumes_after_destroy``.

The helper is the one place both destroy paths use after stack teardown, so these
tests pin its prerequisite gates: a non-regional stack yields no outcome and no
AWS call, an unresolved identity or unverified cluster absence yields a
zero-discovery blocked outcome, and only a definitive deletion plus verified
absence reaches the injected cleanup service. ``destroy()`` keeps its stack-only
boolean contract, which is asserted here as well.

Requirements: 3.1, 3.2, 3.3, 7.2, 7.4, 7.6
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from cli.stacks import StackManager
from cli.volume_cleanup import (
    ClusterAbsenceVerifier,
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    TargetVolumeCleanupOutcome,
    VolumeCleanupRequest,
    VolumeCleanupStatus,
    VolumePolicy,
)

_PROJECT = "gco"
_REGION = "us-east-1"
_STACK = f"{_PROJECT}-{_REGION}"
_TAG_KEY = f"kubernetes.io/cluster/{_STACK}"
_NOW = datetime(2026, 5, 9, 8, 15, tzinfo=UTC)

_RETAIN = VolumeCleanupRequest(
    policy=VolumePolicy.RETAIN,
    deletion_authorized=False,
    authorization_source=DeletionAuthorizationSource.NONE,
)
_DELETE = VolumeCleanupRequest(
    policy=VolumePolicy.DELETE,
    deletion_authorized=True,
    authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
)


def write_cdk_json(project_root, *, regional=(_REGION,), project_name=_PROJECT):
    context = {
        "deployment_regions": {
            "global": "us-east-2",
            "api_gateway": "us-east-2",
            "monitoring": "us-east-2",
            "regional": list(regional),
        }
    }
    if project_name is not None:
        context["project_name"] = project_name
    (project_root / "cdk.json").write_text(json.dumps({"context": context}), encoding="utf-8")


def make_manager(project_root, *, project_name=_PROJECT):
    config = MagicMock()
    config.project_name = project_name
    return StackManager(config, project_root=project_root)


def absent_eks_factory():
    """Return a factory whose EKS client proves exact cluster absence."""
    eks = MagicMock()
    eks.describe_cluster.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "absent"}},
        "DescribeCluster",
    )
    return MagicMock(return_value=eks), eks


def completed_outcome(**overrides):
    fields = {
        "stack_name": _STACK,
        "stack_id": None,
        "target_region": _REGION,
        "target_cluster": _STACK,
        "cluster_tag_key": _TAG_KEY,
        "policy": VolumePolicy.RETAIN,
        "deletion_authorized": False,
        "authorization_source": DeletionAuthorizationSource.NONE,
        "status": VolumeCleanupStatus.COMPLETED,
        "successful": True,
    }
    fields.update(overrides)
    return TargetVolumeCleanupOutcome(**fields)


def test_destroy_keeps_its_stack_only_boolean_contract():
    signature = inspect.signature(StackManager.destroy)

    assert signature.return_annotation == "bool"
    assert "request" not in signature.parameters
    assert "volume_cleanup_request" not in signature.parameters


def test_non_regional_stack_reports_no_outcome_and_makes_no_aws_call(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)
    factory = MagicMock()
    service = MagicMock()
    manager.set_volume_cleanup_dependencies(client_factory=factory, cleanup_service=service)

    outcome = manager.cleanup_regional_volumes_after_destroy(
        stack_name=f"{_PROJECT}-global",
        stack_deleted=True,
        request=_DELETE,
    )

    assert outcome is None
    factory.assert_not_called()
    service.cleanup.assert_not_called()


@pytest.mark.parametrize(
    "stack_name",
    [f"{_PROJECT}-us-west-2", f"{_PROJECT}-{_REGION}-extra", f"other-{_REGION}"],
)
def test_unconfigured_or_near_miss_regional_names_report_no_outcome(tmp_path, stack_name):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)
    service = MagicMock()
    manager.set_volume_cleanup_dependencies(
        client_factory=MagicMock(),
        cleanup_service=service,
    )

    assert (
        manager.cleanup_regional_volumes_after_destroy(
            stack_name=stack_name,
            stack_deleted=True,
            request=_DELETE,
        )
        is None
    )
    service.cleanup.assert_not_called()


def test_unreadable_configuration_blocks_a_possible_regional_stack(tmp_path):
    manager = make_manager(tmp_path)
    factory = MagicMock()
    service = MagicMock()
    manager.set_volume_cleanup_dependencies(client_factory=factory, cleanup_service=service)

    outcome = manager.cleanup_regional_volumes_after_destroy(
        stack_name=_STACK,
        stack_deleted=True,
        request=_DELETE,
    )

    assert outcome is not None
    assert outcome.status is VolumeCleanupStatus.SKIPPED
    assert outcome.blocking_reason_code == "regional-configuration-unreadable"
    assert outcome.counts.discovered == 0
    assert outcome.successful is False
    # An unresolved target can never imply a Region, cluster, or tag key.
    assert (outcome.target_region, outcome.target_cluster, outcome.cluster_tag_key) == (
        None,
        None,
        None,
    )
    factory.assert_not_called()
    service.cleanup.assert_not_called()


def test_unreadable_configuration_still_excludes_non_regional_names(tmp_path):
    manager = make_manager(tmp_path)
    manager.set_volume_cleanup_dependencies(cleanup_service=MagicMock())

    assert (
        manager.cleanup_regional_volumes_after_destroy(
            stack_name=f"{_PROJECT}-global",
            stack_deleted=True,
            request=_DELETE,
        )
        is None
    )


def test_wrong_project_configuration_blocks_volume_cleanup(tmp_path):
    write_cdk_json(tmp_path, project_name="other")
    manager = make_manager(tmp_path)
    service = MagicMock()
    manager.set_volume_cleanup_dependencies(cleanup_service=service)

    outcome = manager.cleanup_regional_volumes_after_destroy(
        stack_name=_STACK,
        stack_deleted=True,
        request=_DELETE,
    )

    assert outcome is not None
    assert outcome.blocking_reason_code == "regional-configuration-unreadable"
    service.cleanup.assert_not_called()


def test_unverified_stack_deletion_blocks_every_ebs_request(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)
    factory = MagicMock()
    service = MagicMock()
    manager.set_volume_cleanup_dependencies(client_factory=factory, cleanup_service=service)

    outcome = manager.cleanup_regional_volumes_after_destroy(
        stack_name=_STACK,
        stack_deleted=False,
        request=_DELETE,
    )

    assert outcome is not None
    assert outcome.status is VolumeCleanupStatus.SKIPPED
    assert outcome.blocking_reason_code == "stack-deletion-unverified"
    assert outcome.target_region == _REGION
    assert outcome.cluster_tag_key == _TAG_KEY
    assert outcome.counts.discovered == 0
    factory.assert_not_called()
    service.cleanup.assert_not_called()


def test_present_cluster_blocks_discovery_after_stack_deletion(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)
    eks = MagicMock()
    eks.describe_cluster.return_value = {"cluster": {"name": _STACK}}
    factory = MagicMock(return_value=eks)
    service = MagicMock()
    manager.set_volume_cleanup_dependencies(client_factory=factory, cleanup_service=service)

    outcome = manager.cleanup_regional_volumes_after_destroy(
        stack_name=_STACK,
        stack_deleted=True,
        request=_DELETE,
    )

    assert outcome is not None
    assert outcome.blocking_reason_code == "cluster-still-present"
    assert outcome.successful is False
    factory.assert_called_once_with("eks", region_name=_REGION)
    service.cleanup.assert_not_called()


def test_unauthorized_cluster_lookup_blocks_cleanup(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)
    eks = MagicMock()
    eks.describe_cluster.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
        "DescribeCluster",
    )
    service = MagicMock()
    manager.set_volume_cleanup_dependencies(
        client_factory=MagicMock(return_value=eks),
        cleanup_service=service,
    )

    outcome = manager.cleanup_regional_volumes_after_destroy(
        stack_name=_STACK,
        stack_deleted=True,
        request=_RETAIN,
    )

    assert outcome is not None
    assert outcome.blocking_reason_code == "cluster-verification-unauthorized"
    service.cleanup.assert_not_called()


def test_verified_absence_invokes_the_injected_cleanup_service(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)
    factory, eks = absent_eks_factory()
    expected = completed_outcome()
    service = MagicMock()
    service.cleanup.return_value = expected
    manager.set_volume_cleanup_dependencies(
        client_factory=factory,
        absence_verifier=ClusterAbsenceVerifier(factory, clock=lambda: _NOW),
        cleanup_service=service,
    )

    outcome = manager.cleanup_regional_volumes_after_destroy(
        stack_name=_STACK,
        stack_deleted=True,
        request=_RETAIN,
    )

    assert outcome is expected
    eks.describe_cluster.assert_called_once_with(name=_STACK)
    service.cleanup.assert_called_once()
    call = service.cleanup.call_args.kwargs
    assert call["request"] is _RETAIN
    assert call["target"] == RegionalVolumeTarget(
        stack_name=_STACK,
        stack_id=None,
        region=_REGION,
        cluster_name=_STACK,
        cluster_tag_key=_TAG_KEY,
    )
    proof = call["absence"]
    assert proof.matches(call["target"])
    assert proof.verified_at == "2026-05-09T08:15:00Z"


def test_strict_target_authorizes_cleanup_without_reading_configuration(tmp_path):
    manager = make_manager(tmp_path)
    strict_target = RegionalVolumeTarget(
        stack_name=_STACK,
        stack_id=f"arn:aws:cloudformation:{_REGION}:123456789012:stack/{_STACK}/id",
        region=_REGION,
        cluster_name=_STACK,
        cluster_tag_key=_TAG_KEY,
    )
    factory, _eks = absent_eks_factory()
    service = MagicMock()
    service.cleanup.return_value = completed_outcome(stack_id=strict_target.stack_id)
    manager.set_volume_cleanup_dependencies(client_factory=factory, cleanup_service=service)

    outcome = manager.cleanup_regional_volumes_after_destroy(
        stack_name=_STACK,
        stack_deleted=True,
        request=_DELETE,
        strict_target=strict_target,
    )

    assert outcome is not None
    assert outcome.stack_id == strict_target.stack_id
    assert service.cleanup.call_args.kwargs["target"] is strict_target


def test_strict_target_for_another_stack_is_blocked(tmp_path):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)
    factory = MagicMock()
    service = MagicMock()
    manager.set_volume_cleanup_dependencies(client_factory=factory, cleanup_service=service)

    outcome = manager.cleanup_regional_volumes_after_destroy(
        stack_name=_STACK,
        stack_deleted=True,
        request=_DELETE,
        strict_target=RegionalVolumeTarget(
            stack_name=f"{_PROJECT}-us-west-2",
            stack_id=None,
            region="us-west-2",
            cluster_name=f"{_PROJECT}-us-west-2",
            cluster_tag_key=f"kubernetes.io/cluster/{_PROJECT}-us-west-2",
        ),
    )

    assert outcome is not None
    assert outcome.blocking_reason_code == "strict-target-stack-mismatch"
    assert outcome.target_region is None
    factory.assert_not_called()
    service.cleanup.assert_not_called()


@pytest.mark.volume_cleanup_boto3_owner
def test_default_dependencies_create_one_region_scoped_boto3_client(tmp_path, monkeypatch):
    write_cdk_json(tmp_path)
    manager = make_manager(tmp_path)
    eks = MagicMock()
    eks.describe_cluster.return_value = {"cluster": {"name": _STACK}}
    created: list[tuple[str, str]] = []

    def fake_client(service_name, *, region_name):
        created.append((service_name, region_name))
        return eks

    monkeypatch.setattr("boto3.client", fake_client)

    outcome = manager.cleanup_regional_volumes_after_destroy(
        stack_name=_STACK,
        stack_deleted=True,
        request=_RETAIN,
    )

    assert outcome is not None
    assert outcome.blocking_reason_code == "cluster-still-present"
    assert created == [("eks", _REGION)]
