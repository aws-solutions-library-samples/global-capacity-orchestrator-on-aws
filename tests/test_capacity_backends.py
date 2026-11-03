"""The backend-aware Region picker: SageMaker HyperPod and AWS PCS capacity per Region.

``cli/capacity/backends.py`` reads one Region's HyperPod cluster (SageMaker)
and PCS cluster (PCS, EC2, the regional API's Slurm status) against real
botocore clients behind ``Stubber``; ``cli/capacity/manifest_placement.py``
reads what a manifest asks for; ``MultiRegionCapacityChecker`` ranks the
Regions that run the backend and refuses to pick when none does. The CLI
surfaces (``recommend-region --backend``, ``submit-sqs --auto-region`` on a
HyperPod-routed manifest, ``slurm submit --auto-region``) and the MCP
``recommend_region`` tool are driven through ``CliRunner`` with the checker
faked.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.stub import ANY, Stubber
from click.testing import CliRunner

from cli.aws_client import RegionalStack
from cli.capacity import backends, placement
from cli.capacity.backends import (
    BACKEND_EKS,
    BACKEND_HYPERPOD,
    BACKEND_PCS,
    HyperpodCapacity,
    HyperpodInstanceGroup,
    NoEligibleRegionError,
    PcsCapacity,
    PcsNodeGroup,
    ec2_equivalent,
    hyperpod_capacity,
    ml_instance_type,
    pcs_capacity,
    sum_gpus,
)
from cli.capacity.multi_region import MultiRegionCapacityChecker, RegionCapacity
from cli.config import GCOConfig
from cli.main import cli

_REGION = "us-east-1"
_STACK = "gco-us-east-1"
_HP_CLUSTER = "gco-hyperpod"
_PCS_CLUSTER = "pcs_0123456789abcdef"
_LOGIN_GROUP = "pcs_login00000000"
_CPU_GROUP = "pcs_cpu0000000000"
_GPU_GROUP = "pcs_gpu0000000000"
_NOW = datetime(2026, 10, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Stubbed clients
# ---------------------------------------------------------------------------


class _Clients:
    """Real botocore clients behind Stubber, handed out by a client_factory."""

    def __init__(self, *services: str) -> None:
        kwargs = {
            "region_name": _REGION,
            "aws_access_key_id": "testing",
            "aws_secret_access_key": "testing",  # nosec B106  # fake credentials for stubbed clients
        }
        self.clients = {service: boto3.client(service, **kwargs) for service in services}
        self.stubs = {service: Stubber(client) for service, client in self.clients.items()}
        self.requested: list[str] = []

    def factory(self, service: str, *, region_name: str) -> Any:
        assert region_name == _REGION
        self.requested.append(service)
        return self.clients[service]

    def __getitem__(self, service: str) -> Stubber:
        return self.stubs[service]


def _stubbed(*services: str) -> Iterator[_Clients]:
    stubbed = _Clients(*services)
    for stub in stubbed.stubs.values():
        stub.activate()
    yield stubbed
    for stub in stubbed.stubs.values():
        stub.assert_no_pending_responses()
        stub.deactivate()


@pytest.fixture
def sagemaker_clients() -> Iterator[_Clients]:
    yield from _stubbed("sagemaker")


@pytest.fixture
def pcs_clients() -> Iterator[_Clients]:
    yield from _stubbed("pcs", "ec2")


def _group(
    name: str, instance_type: str, current: int, target: int, status: str = "InService"
) -> dict[str, Any]:
    return {
        "InstanceGroupName": name,
        "InstanceType": instance_type,
        "CurrentCount": current,
        "TargetCount": target,
        "Status": status,
    }


def _describe_cluster(status: str, groups: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ClusterArn": f"arn:aws:sagemaker:{_REGION}:123456789012:cluster/abc",
        "ClusterName": _HP_CLUSTER,
        "ClusterStatus": status,
        "CreationTime": _NOW,
        "InstanceGroups": groups,
    }


# ---------------------------------------------------------------------------
# Helpers and the HyperPod read
# ---------------------------------------------------------------------------


def test_instance_type_spellings() -> None:
    assert ec2_equivalent("ml.g5.8xlarge") == "g5.8xlarge"
    assert ec2_equivalent("g5.8xlarge") == "g5.8xlarge"
    assert ml_instance_type("g5.8xlarge") == "ml.g5.8xlarge"
    assert ml_instance_type("ml.g5.8xlarge") == "ml.g5.8xlarge"


def test_sum_gpus_is_none_when_any_group_is_unknown() -> None:
    known = HyperpodInstanceGroup("a", "ml.g5.8xlarge", 2, 2, "InService", gpus_per_instance=1)
    bigger = HyperpodInstanceGroup("b", "ml.p5.48xlarge", 1, 1, "InService", gpus_per_instance=8)
    unknown = HyperpodInstanceGroup("c", "ml.trn2.48xlarge", 1, 1, "InService")
    assert known.gpus == 2
    assert unknown.gpus is None
    assert sum_gpus([known, bigger]) == 10
    assert sum_gpus([known, unknown]) is None
    assert sum_gpus([]) == 0


def test_no_eligible_region_error_carries_the_reasons() -> None:
    error = NoEligibleRegionError("none", [{"region": "us-east-1", "reason": "off"}])
    assert str(error) == "none"
    assert error.ineligible == [{"region": "us-east-1", "reason": "off"}]


def test_hyperpod_unreadable_outputs_are_a_missing_signal_not_absence() -> None:
    capacity = hyperpod_capacity(
        _REGION, None, client_factory=MagicMock(), gpus_per_instance=lambda _t: 1
    )
    assert capacity.deployed is False
    assert capacity.unavailable_signals == ["cluster"]
    assert capacity.errors == ["Regional stack outputs could not be read"]


def test_hyperpod_not_published_by_the_stack(sagemaker_clients: _Clients) -> None:
    capacity = hyperpod_capacity(
        _REGION,
        {"JobQueueUrl": "https://sqs/q"},
        client_factory=sagemaker_clients.factory,
        gpus_per_instance=lambda _t: 1,
    )
    assert capacity == HyperpodCapacity(deployed=False)
    assert sagemaker_clients.requested == []


def test_hyperpod_cluster_described_with_gpu_pool(sagemaker_clients: _Clients) -> None:
    sagemaker_clients["sagemaker"].add_response(
        "describe_cluster",
        _describe_cluster(
            "InService",
            [
                _group("accelerated", "ml.g5.8xlarge", 2, 2),
                _group("big", "ml.p5.48xlarge", 1, 1),
                _group("odd", "notatype", 1, 1),
            ],
        ),
        {"ClusterName": _HP_CLUSTER},
    )
    asked: list[str] = []

    def gpus(instance_type: str) -> int | None:
        asked.append(instance_type)
        return {"ml.g5.8xlarge": 1, "ml.p5.48xlarge": 8}.get(instance_type)

    capacity = hyperpod_capacity(
        _REGION,
        {"HyperpodClusterName": _HP_CLUSTER},
        client_factory=sagemaker_clients.factory,
        gpus_per_instance=gpus,
    )
    assert capacity.deployed is True
    assert capacity.cluster_name == _HP_CLUSTER
    assert capacity.status == "InService"
    assert capacity.healthy is True
    assert capacity.nodes_current == 4
    assert capacity.nodes_target == 4
    # The group whose type is not an ``ml.*`` type is never looked up and makes
    # the pool unknown rather than smaller.
    assert asked == ["ml.g5.8xlarge", "ml.p5.48xlarge"]
    assert capacity.gpus_total is None
    assert [group.gpus for group in capacity.instance_groups] == [2, 8, None]
    assert capacity.unavailable_signals == []
    assert capacity.errors == []


def test_hyperpod_cluster_not_at_target_is_not_healthy(sagemaker_clients: _Clients) -> None:
    sagemaker_clients["sagemaker"].add_response(
        "describe_cluster",
        _describe_cluster("InService", [_group("accelerated", "ml.g5.8xlarge", 1, 2, "Updating")]),
        {"ClusterName": _HP_CLUSTER},
    )
    capacity = hyperpod_capacity(
        _REGION,
        {"HyperpodClusterName": _HP_CLUSTER},
        client_factory=sagemaker_clients.factory,
        gpus_per_instance=lambda _t: 1,
    )
    assert capacity.healthy is False
    assert capacity.gpus_total == 1
    assert (capacity.nodes_current, capacity.nodes_target) == (1, 2)


def test_hyperpod_cluster_status_other_than_in_service(sagemaker_clients: _Clients) -> None:
    sagemaker_clients["sagemaker"].add_response(
        "describe_cluster",
        _describe_cluster("Updating", [_group("accelerated", "ml.g5.8xlarge", 2, 2)]),
        {"ClusterName": _HP_CLUSTER},
    )
    capacity = hyperpod_capacity(
        _REGION,
        {"HyperpodClusterName": _HP_CLUSTER},
        client_factory=sagemaker_clients.factory,
        gpus_per_instance=lambda _t: 1,
    )
    assert capacity.healthy is False
    assert capacity.status == "Updating"


def test_hyperpod_describe_failure_is_the_cluster_signal(sagemaker_clients: _Clients) -> None:
    sagemaker_clients["sagemaker"].add_client_error(
        "describe_cluster",
        "AccessDeniedException",
        "no",
        expected_params={"ClusterName": _HP_CLUSTER},
    )
    capacity = hyperpod_capacity(
        _REGION,
        {"HyperpodClusterName": _HP_CLUSTER},
        client_factory=sagemaker_clients.factory,
        gpus_per_instance=lambda _t: 1,
    )
    assert capacity.deployed is True
    assert capacity.status is None
    assert capacity.healthy is False
    assert capacity.unavailable_signals == ["cluster"]
    assert capacity.errors[0].startswith(f"HyperPod cluster {_HP_CLUSTER} could not be described")


# ---------------------------------------------------------------------------
# The PCS read
# ---------------------------------------------------------------------------

_PCS_OUTPUTS = {
    "PcsClusterId": _PCS_CLUSTER,
    "PcsClusterName": "gco-pcs",
    "PcsMode": "managed",
    "PcsLoginNodeGroupId": _LOGIN_GROUP,
}


def _summary(group_id: str, name: str, status: str = "ACTIVE") -> dict[str, Any]:
    return {
        "id": group_id,
        "name": name,
        "arn": (
            f"arn:aws:pcs:{_REGION}:123456789012:cluster/{_PCS_CLUSTER}/computenodegroup/{group_id}"
        ),
        "clusterId": _PCS_CLUSTER,
        "createdAt": _NOW,
        "modifiedAt": _NOW,
        "status": status,
    }


def _detail(
    group_id: str, name: str, types: list[str], minimum: int, maximum: int
) -> dict[str, Any]:
    return {
        "computeNodeGroup": {
            **_summary(group_id, name),
            "subnetIds": ["subnet-1"],
            "purchaseOption": "ONDEMAND",
            "customLaunchTemplate": {"id": "lt-1", "version": "1"},
            "iamInstanceProfileArn": "arn:aws:iam::123456789012:instance-profile/x",
            "scalingConfiguration": {"minInstanceCount": minimum, "maxInstanceCount": maximum},
            "instanceConfigs": [{"instanceType": t} for t in types],
        }
    }


def _cluster(status: str = "ACTIVE") -> dict[str, Any]:
    return {
        "cluster": {
            "name": "gco-pcs",
            "id": _PCS_CLUSTER,
            "arn": f"arn:aws:pcs:{_REGION}:123456789012:cluster/{_PCS_CLUSTER}",
            "status": status,
            "createdAt": _NOW,
            "modifiedAt": _NOW,
            "scheduler": {"type": "SLURM", "version": "25.11"},
            "size": "SMALL",
            "networking": {"subnetIds": ["subnet-1"], "securityGroupIds": ["sg-1"]},
        }
    }


def _instances(*group_ids: str) -> dict[str, Any]:
    return {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": f"i-{index:017x}",
                        "Tags": [
                            {"Key": "Name", "Value": "n"},
                            {"Key": "aws:pcs:compute-node-group-id", "Value": group_id},
                        ],
                    }
                    for index, group_id in enumerate(group_ids)
                ]
                + [{"InstanceId": "i-00000000000000fff"}]  # an untagged instance is ignored
            }
        ]
    }


_SLURM_STATUS = {
    "backend": "pcs",
    "jobs": {"total": 5, "by_state": {"PENDING": 3, "RUNNING": 2}},
    "nodes": {"total": 4, "by_state": {"IDLE": 1, "ALLOCATED": 2}, "scaled_in": 1},
}


def _expect_node_groups(clients: _Clients) -> None:
    """Two pages of node groups, each compute group described as its page is read."""
    pcs = clients["pcs"]
    pcs.add_response(
        "list_compute_node_groups",
        {
            "computeNodeGroups": [_summary(_LOGIN_GROUP, "login"), _summary(_CPU_GROUP, "cpu")],
            "nextToken": "page-2",
        },
        {"clusterIdentifier": _PCS_CLUSTER},
    )
    pcs.add_response(
        "get_compute_node_group",
        _detail(_CPU_GROUP, "cpu", ["c6i.large", "c6i.xlarge"], 0, 8),
        {"clusterIdentifier": _PCS_CLUSTER, "computeNodeGroupIdentifier": _CPU_GROUP},
    )
    pcs.add_response(
        "list_compute_node_groups",
        {
            "computeNodeGroups": [
                _summary(_GPU_GROUP, "gpu"),
                _summary("pcs_other000000000", "login"),  # the login name, another id
                {**_summary("", "nameless")},  # no id: skipped
            ]
        },
        {"clusterIdentifier": _PCS_CLUSTER, "nextToken": "page-2"},
    )
    pcs.add_response(
        "get_compute_node_group",
        _detail(_GPU_GROUP, "gpu", ["p5.48xlarge"], 1, 2),
        {"clusterIdentifier": _PCS_CLUSTER, "computeNodeGroupIdentifier": _GPU_GROUP},
    )


def test_pcs_unreadable_outputs_are_missing_signals_not_absence() -> None:
    capacity = pcs_capacity(_REGION, None, client_factory=MagicMock(), slurm_status=dict)
    assert capacity.deployed is False
    assert capacity.unavailable_signals == ["cluster", "nodes", "slurm"]


def test_pcs_not_published_by_the_stack(pcs_clients: _Clients) -> None:
    capacity = pcs_capacity(
        _REGION, {"JobQueueUrl": "x"}, client_factory=pcs_clients.factory, slurm_status=dict
    )
    assert capacity == PcsCapacity(deployed=False)
    assert pcs_clients.requested == []


def test_pcs_cluster_node_groups_instances_and_slurm(pcs_clients: _Clients) -> None:
    pcs_clients["pcs"].add_response("get_cluster", _cluster(), {"clusterIdentifier": _PCS_CLUSTER})
    _expect_node_groups(pcs_clients)
    pcs_clients["ec2"].add_response(
        "describe_instances",
        {**_instances(_CPU_GROUP, _CPU_GROUP, _GPU_GROUP), "NextToken": "more"},
        {
            "Filters": [
                {"Name": "tag:aws:pcs:compute-node-group-id", "Values": [_CPU_GROUP, _GPU_GROUP]},
                {"Name": "instance-state-name", "Values": ["pending", "running"]},
            ]
        },
    )
    pcs_clients["ec2"].add_response(
        "describe_instances",
        _instances(_GPU_GROUP, _LOGIN_GROUP),  # the login node's tag is not counted
        {"Filters": ANY, "NextToken": "more"},
    )
    reads: list[int] = []

    def slurm_status() -> dict[str, Any]:
        reads.append(1)
        return _SLURM_STATUS

    capacity = pcs_capacity(
        _REGION, _PCS_OUTPUTS, client_factory=pcs_clients.factory, slurm_status=slurm_status
    )
    assert capacity.deployed is True
    assert (capacity.cluster_id, capacity.cluster_name, capacity.mode) == (
        _PCS_CLUSTER,
        "gco-pcs",
        "managed",
    )
    assert capacity.status == "ACTIVE"
    assert capacity.healthy is True
    assert capacity.node_groups == [
        PcsNodeGroup(_CPU_GROUP, "cpu", "ACTIVE", ["c6i.large", "c6i.xlarge"], 0, 8, running=2),
        PcsNodeGroup(_GPU_GROUP, "gpu", "ACTIVE", ["p5.48xlarge"], 1, 2, running=2),
    ]
    assert (capacity.max_nodes, capacity.running_nodes, capacity.headroom) == (10, 4, 6)
    assert (
        capacity.slurm_pending_jobs,
        capacity.slurm_running_jobs,
        capacity.slurm_idle_nodes,
    ) == (3, 2, 1)
    assert capacity.unavailable_signals == []
    assert capacity.errors == []
    assert reads == [1]
    # One PCS client serves both the cluster and the node-group reads.
    assert pcs_clients.requested == ["pcs", "ec2"]


def test_pcs_cluster_unreadable_still_reads_node_groups_and_slurm(pcs_clients: _Clients) -> None:
    pcs_clients["pcs"].add_client_error(
        "get_cluster",
        "AccessDeniedException",
        "no",
        expected_params={"clusterIdentifier": _PCS_CLUSTER},
    )
    pcs_clients["pcs"].add_response(
        "list_compute_node_groups", {"computeNodeGroups": []}, {"clusterIdentifier": _PCS_CLUSTER}
    )
    capacity = pcs_capacity(
        _REGION,
        {"PcsClusterId": _PCS_CLUSTER},
        client_factory=pcs_clients.factory,
        slurm_status=lambda: {"jobs": {"by_state": {}}, "nodes": {}},
    )
    assert capacity.status is None
    assert capacity.healthy is False
    assert capacity.cluster_name is None
    assert capacity.mode is None
    assert capacity.unavailable_signals == ["cluster"]
    assert capacity.errors[0].startswith(f"PCS cluster {_PCS_CLUSTER} could not be read")
    # No compute node group: nothing launched, no headroom, EC2 never asked.
    assert (capacity.max_nodes, capacity.running_nodes, capacity.headroom) == (0, 0, 0)
    assert (
        capacity.slurm_pending_jobs,
        capacity.slurm_running_jobs,
        capacity.slurm_idle_nodes,
    ) == (
        0,
        0,
        0,
    )
    assert pcs_clients.requested == ["pcs", "ec2"]


def test_pcs_node_groups_unreadable_is_the_nodes_signal(pcs_clients: _Clients) -> None:
    pcs_clients["pcs"].add_response(
        "get_cluster", _cluster("UPDATING"), {"clusterIdentifier": _PCS_CLUSTER}
    )
    pcs_clients["pcs"].add_client_error(
        "list_compute_node_groups",
        "ThrottlingException",
        "slow",
        expected_params={"clusterIdentifier": _PCS_CLUSTER},
    )
    capacity = pcs_capacity(
        _REGION,
        _PCS_OUTPUTS,
        client_factory=pcs_clients.factory,
        slurm_status=lambda: {"jobs": "not a mapping", "nodes": {"by_state": {"IDLE": "2"}}},
    )
    assert capacity.status == "UPDATING"
    assert capacity.healthy is False
    assert capacity.unavailable_signals == ["nodes"]
    assert capacity.errors[0].startswith(
        f"PCS compute node groups of {_PCS_CLUSTER} could not be read"
    )
    assert capacity.node_groups == []
    assert (capacity.max_nodes, capacity.running_nodes, capacity.headroom) == (None, None, None)
    assert (
        capacity.slurm_pending_jobs,
        capacity.slurm_running_jobs,
        capacity.slurm_idle_nodes,
    ) == (
        0,
        0,
        2,
    )


def test_pcs_ec2_failure_is_the_nodes_signal_too(pcs_clients: _Clients) -> None:
    pcs_clients["pcs"].add_response("get_cluster", _cluster(), {"clusterIdentifier": _PCS_CLUSTER})
    pcs_clients["pcs"].add_response(
        "list_compute_node_groups",
        {"computeNodeGroups": [_summary(_CPU_GROUP, "cpu")]},
        {"clusterIdentifier": _PCS_CLUSTER},
    )
    pcs_clients["pcs"].add_response(
        "get_compute_node_group",
        _detail(_CPU_GROUP, "cpu", ["c6i.large"], 0, 4),
        {"clusterIdentifier": _PCS_CLUSTER, "computeNodeGroupIdentifier": _CPU_GROUP},
    )
    pcs_clients["ec2"].add_client_error("describe_instances", "UnauthorizedOperation", "no")
    capacity = pcs_capacity(
        _REGION,
        _PCS_OUTPUTS,
        client_factory=pcs_clients.factory,
        slurm_status=lambda: "garbage",  # type: ignore[arg-type,return-value]
    )
    assert capacity.unavailable_signals == ["nodes"]
    assert capacity.headroom is None
    assert (
        capacity.slurm_pending_jobs,
        capacity.slurm_running_jobs,
        capacity.slurm_idle_nodes,
    ) == (
        0,
        0,
        0,
    )


def test_pcs_slurm_failure_is_the_slurm_signal(pcs_clients: _Clients) -> None:
    pcs_clients["pcs"].add_response("get_cluster", _cluster(), {"clusterIdentifier": _PCS_CLUSTER})
    pcs_clients["pcs"].add_response(
        "list_compute_node_groups", {"computeNodeGroups": []}, {"clusterIdentifier": _PCS_CLUSTER}
    )

    def slurm_status() -> dict[str, Any]:
        raise RuntimeError("API unreachable")

    capacity = pcs_capacity(
        _REGION, _PCS_OUTPUTS, client_factory=pcs_clients.factory, slurm_status=slurm_status
    )
    assert capacity.unavailable_signals == ["slurm"]
    assert capacity.errors == ["Slurm status of the PCS backend could not be read: API unreachable"]
    assert (
        capacity.slurm_pending_jobs,
        capacity.slurm_running_jobs,
        capacity.slurm_idle_nodes,
    ) == (
        None,
        None,
        None,
    )


def test_pcs_client_construction_failure_costs_both_aws_signals() -> None:
    factory = MagicMock(side_effect=RuntimeError("no credentials"))
    capacity = pcs_capacity(
        _REGION, _PCS_OUTPUTS, client_factory=factory, slurm_status=lambda: _SLURM_STATUS
    )
    assert capacity.deployed is True
    assert capacity.unavailable_signals == ["cluster", "nodes"]
    assert [error.split(":")[0] for error in capacity.errors] == [
        f"PCS cluster {_PCS_CLUSTER} could not be read",
        f"PCS compute node groups of {_PCS_CLUSTER} could not be read",
    ]
    assert capacity.slurm_pending_jobs == 3
    # The client was asked for twice (once per read) and never for EC2.
    assert factory.call_args_list == [(("pcs",), {"region_name": _REGION})] * 2


def test_state_count_tolerates_bad_values() -> None:
    assert backends._state_count({"PENDING": "x"}, "PENDING") == 0
    assert backends._state_count({"PENDING": None}, "PENDING") == 0
    assert backends._state_count({"PENDING": 2.0}, "PENDING") == 2
    assert backends._state_count(["PENDING"], "PENDING") == 0


# ---------------------------------------------------------------------------
# What a manifest asks for
# ---------------------------------------------------------------------------


def _job(labels: dict[str, str] | None = None, **pod_spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "j", "labels": labels or {}},
        "spec": {"template": {"spec": pod_spec}},
    }


def test_manifest_placement_defaults_to_eks() -> None:
    hints = placement.manifest_placement([])
    assert hints == placement.PlacementHints(backend=BACKEND_EKS, gpu_count=0, routing=None)
    assert placement.manifest_placement([None, "text", 3, _job()]).backend == BACKEND_EKS


def test_manifest_placement_reads_the_queue_label_and_gpus() -> None:
    manifest = _job(
        {"kueue.x-k8s.io/queue-name": "gco-hyperpod"},
        containers=[
            {"name": "a", "resources": {"limits": {"nvidia.com/gpu": "2"}}},
            {"name": "b", "resources": {"requests": {"nvidia.com/gpu": 1}}},
            {"name": "c", "resources": {"limits": {"nvidia.com/gpu": "1.5"}}},  # unparsable: 0
            {"name": "d"},
            "not a container",
        ],
    )
    hints = placement.manifest_placement([manifest])
    assert hints == placement.PlacementHints(backend=BACKEND_HYPERPOD, gpu_count=3, routing="queue")


def test_manifest_placement_reads_the_node_selector_and_weights_trainjob_nodes() -> None:
    selector = _job(
        containers=[{"name": "a", "resources": {"limits": {"nvidia.com/gpu": "1"}}}],
        nodeSelector={"gco.aws/capacity-provider": "hyperpod"},
    )
    trainjob = {
        "apiVersion": "trainer.kubeflow.org/v1alpha1",
        "kind": "TrainJob",
        "metadata": {"name": "t"},
        "spec": {
            "trainer": {
                "numNodes": 4,
                "image": "img",
                "resourcesPerNode": {"limits": {"nvidia.com/gpu": "8"}},
            }
        },
    }
    # The first routed manifest decides; a later plain one does not undo it.
    hints = placement.manifest_placement([_job(), selector, trainjob])
    assert hints.backend == BACKEND_HYPERPOD
    assert hints.routing == "selector"
    # 1 GPU from the selector Job plus 4 nodes x 8 GPUs from the TrainJob.
    assert hints.gpu_count == 33


# ---------------------------------------------------------------------------
# The checker: reading the backend per Region
# ---------------------------------------------------------------------------


def _checker() -> MultiRegionCapacityChecker:
    checker = MultiRegionCapacityChecker(GCOConfig(default_region=_REGION))
    checker._session = MagicMock()
    return checker


def _stack() -> RegionalStack:
    return RegionalStack(
        region=_REGION, stack_name=_STACK, cluster_name=_STACK, status="CREATE_COMPLETE"
    )


def _aws_client(outputs: list[dict[str, str]] | Exception) -> tuple[MagicMock, MagicMock]:
    cfn = MagicMock()
    if isinstance(outputs, Exception):
        cfn.describe_stacks.side_effect = outputs
    else:
        cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": outputs}]}
    sqs = MagicMock()
    sqs.get_queue_attributes.return_value = {
        "Attributes": {
            "ApproximateNumberOfMessages": "1",
            "ApproximateNumberOfMessagesNotVisible": "0",
        }
    }
    cloudwatch = MagicMock()
    cloudwatch.get_metric_statistics.return_value = {"Datapoints": [{"Average": 10.0}]}
    clients = {"cloudformation": cfn, "sqs": sqs, "cloudwatch": cloudwatch}
    aws_client = MagicMock()
    aws_client.get_regional_stack.return_value = _stack()
    return aws_client, MagicMock(side_effect=lambda service, **_k: clients[service])


@pytest.mark.parametrize(
    "call",
    [
        lambda checker: checker.get_region_capacity(_REGION, backend="slurm"),
        lambda checker: checker.get_all_regions_capacity(backend="slurm"),
        lambda checker: checker.recommend_region_for_job(backend="slurm"),
    ],
    ids=["get_region_capacity", "get_all_regions_capacity", "recommend_region_for_job"],
)
def test_checker_rejects_an_unknown_backend(call: Any) -> None:
    checker = _checker()
    with pytest.raises(ValueError, match="backend must be one of eks, hyperpod, pcs, not 'slurm'"):
        call(checker)


def test_eks_read_attaches_no_backend() -> None:
    checker = _checker()
    aws_client, client = _aws_client([{"OutputKey": "JobQueueUrl", "OutputValue": "https://q"}])
    checker._session.client = client
    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        capacity = checker.get_region_capacity(_REGION)
    assert capacity.hyperpod is None
    assert capacity.pcs is None
    assert capacity.queue_depth == 1


def test_hyperpod_read_gets_the_stack_outputs_and_a_gpu_lookup() -> None:
    checker = _checker()
    aws_client, client = _aws_client(
        [
            {"OutputKey": "JobQueueUrl", "OutputValue": "https://q"},
            {"OutputKey": "HyperpodClusterName", "OutputValue": _HP_CLUSTER},
        ]
    )
    checker._session.client = client
    read = HyperpodCapacity(deployed=True, cluster_name=_HP_CLUSTER)
    lookup = MagicMock(return_value=1)
    with (
        patch("cli.aws_client.get_aws_client", return_value=aws_client),
        patch("cli.capacity.multi_region.hyperpod_capacity", return_value=read) as probe,
        patch.object(checker, "_ml_gpu_lookup", return_value=lookup) as gpu_lookup,
    ):
        capacity = checker.get_region_capacity(_REGION, backend=BACKEND_HYPERPOD)
    assert capacity.hyperpod is read
    assert capacity.pcs is None
    gpu_lookup.assert_called_once_with(_REGION)
    probe.assert_called_once_with(
        _REGION,
        {"JobQueueUrl": "https://q", "HyperpodClusterName": _HP_CLUSTER},
        client_factory=client,
        gpus_per_instance=lookup,
    )


def test_hyperpod_read_passes_none_when_the_stack_cannot_be_described() -> None:
    checker = _checker()
    aws_client, client = _aws_client(RuntimeError("cfn down"))
    checker._session.client = client
    with (
        patch("cli.aws_client.get_aws_client", return_value=aws_client),
        patch("cli.capacity.multi_region.hyperpod_capacity") as probe,
    ):
        capacity = checker.get_region_capacity(_REGION, backend=BACKEND_HYPERPOD)
    assert probe.call_args.args == (_REGION, None)
    assert "queue" in capacity.unavailable_signals


def test_pcs_read_wires_the_slurm_status_call() -> None:
    checker = _checker()
    aws_client, client = _aws_client([{"OutputKey": "PcsClusterId", "OutputValue": _PCS_CLUSTER}])
    aws_client.call_api.return_value = _SLURM_STATUS
    checker._session.client = client
    read = PcsCapacity(deployed=True, cluster_id=_PCS_CLUSTER)
    with (
        patch("cli.aws_client.get_aws_client", return_value=aws_client),
        patch("cli.capacity.multi_region.pcs_capacity", return_value=read) as probe,
    ):
        capacity = checker.get_region_capacity(_REGION, backend=BACKEND_PCS)
    assert capacity.pcs is read
    assert capacity.hyperpod is None
    assert probe.call_args.args == (_REGION, {"PcsClusterId": _PCS_CLUSTER})
    assert probe.call_args.kwargs["client_factory"] is client
    # The Slurm reader is the regional API's status route for the PCS backend.
    assert probe.call_args.kwargs["slurm_status"]() == _SLURM_STATUS
    aws_client.call_api.assert_called_once_with(
        "GET", "/api/v1/slurm/status", region=_REGION, params={"backend": "pcs"}
    )


def test_ml_gpu_lookup_asks_ec2_for_the_twin_once_per_type() -> None:
    checker = _checker()
    capacity_checker = MagicMock()
    capacity_checker.get_instance_info.side_effect = [MagicMock(gpu_count=8), None]
    with patch("cli.capacity.multi_region.CapacityChecker", return_value=capacity_checker):
        lookup = checker._ml_gpu_lookup(_REGION)
        assert lookup("ml.p5.48xlarge") == 8
        assert lookup("ml.p5.48xlarge") == 8
        assert lookup("ml.trn2.48xlarge") is None
        assert lookup("ml.trn2.48xlarge") is None
    assert capacity_checker.get_instance_info.call_args_list == [
        (("p5.48xlarge",), {"region": _REGION}),
        (("trn2.48xlarge",), {"region": _REGION}),
    ]


def test_all_regions_read_passes_the_backend_through() -> None:
    checker = _checker()
    aws_client = MagicMock()
    aws_client.discover_regional_stacks.return_value = {"us-east-1": _stack(), "eu-west-1": None}
    with (
        patch("cli.aws_client.get_aws_client", return_value=aws_client),
        patch.object(
            checker,
            "get_region_capacity",
            side_effect=[RegionCapacity(region="us-east-1"), RuntimeError("boom")],
        ) as read,
    ):
        capacities = checker.get_all_regions_capacity(backend=BACKEND_PCS)
    assert [cap.region for cap in capacities] == ["us-east-1"]
    assert read.call_args_list == [
        (("us-east-1",), {"backend": "pcs"}),
        (("eu-west-1",), {"backend": "pcs"}),
    ]
    assert checker._last_region_errors == ["eu-west-1: boom"]


# ---------------------------------------------------------------------------
# The checker: ranking HyperPod Regions
# ---------------------------------------------------------------------------


def _hp(
    *,
    status: str = "InService",
    groups: list[tuple[str, int, int, int | None]] = (("accelerated", 2, 2, 1),),
    unavailable: list[str] | None = None,
    errors: list[str] | None = None,
) -> HyperpodCapacity:
    instance_groups = [
        HyperpodInstanceGroup(name, f"ml.{name}", current, target, "InService", gpus)
        for name, current, target, gpus in groups
    ]
    capacity = HyperpodCapacity(
        deployed=True,
        cluster_name=_HP_CLUSTER,
        status=status,
        instance_groups=instance_groups,
        nodes_current=sum(g.current for g in instance_groups),
        nodes_target=sum(g.target for g in instance_groups),
        gpus_total=sum_gpus(instance_groups),
        unavailable_signals=list(unavailable or []),
        errors=list(errors or []),
    )
    capacity.healthy = status == "InService" and all(g.current >= g.target for g in instance_groups)
    return capacity


def _region(
    name: str,
    *,
    queue_depth: int = 0,
    running_jobs: int = 0,
    unavailable: list[str] | None = None,
    hyperpod: HyperpodCapacity | None = None,
    pcs: PcsCapacity | None = None,
) -> RegionCapacity:
    return RegionCapacity(
        region=name,
        queue_depth=queue_depth,
        running_jobs=running_jobs,
        gpu_utilization=12.5,
        telemetry_status="partial" if unavailable else "complete",
        unavailable_signals=list(unavailable or []),
        telemetry_errors=["queue telemetry failed"] if unavailable else [],
        hyperpod=hyperpod,
        pcs=pcs,
    )


def _recommend(checker: MultiRegionCapacityChecker, regions: list[RegionCapacity], **kwargs: Any):
    with patch.object(checker, "get_all_regions_capacity", return_value=regions) as sweep:
        result = checker.recommend_region_for_job(**kwargs)
    sweep.assert_called_once_with(backend=kwargs["backend"])
    return result


def test_hyperpod_ranking_prefers_healthy_fitting_clusters_with_an_empty_queue() -> None:
    checker = _checker()
    regions = [
        # Healthy, fits, but a busy queue.
        _region("ap-south-1", queue_depth=3, running_jobs=2, hyperpod=_hp()),
        # Healthy, fits, idle: the winner.
        _region("eu-west-1", hyperpod=_hp(groups=[("g5", 2, 2, 1)])),
        # Healthy but too small for 2 GPUs.
        _region("us-west-2", hyperpod=_hp(groups=[("g5", 1, 1, 1)])),
        # Not healthy (a node short).
        _region("us-east-2", hyperpod=_hp(groups=[("g5", 1, 2, 1)])),
        # No HyperPod at all.
        _region("us-east-1"),
        _region("ca-central-1", hyperpod=HyperpodCapacity(deployed=False)),
    ]
    result = _recommend(checker, regions, gpu_count=2, backend=BACKEND_HYPERPOD)
    assert result["region"] == "eu-west-1"
    assert result["backend"] == "hyperpod"
    assert result["scoring_method"] == "hyperpod"
    assert result["gpus_required"] == 2
    assert result["instance_type"] is None
    assert result["reason"] == (
        "HyperPod cluster InService (2 of 2 nodes, 2 GPUs), fits the 2 GPUs requested, "
        "empty queue, no running jobs"
    )
    assert [r["region"] for r in result["all_regions"]] == [
        "eu-west-1",
        "ap-south-1",
        "us-west-2",
        "us-east-2",
    ]
    # 1000 per: unhealthy cluster, a pool too small for the request. The
    # node-short cluster has one GPU present, so it fails both.
    assert [r["score"] for r in result["all_regions"]] == [0.0, 40.0, 1000.0, 2000.0]
    assert result["all_regions"][2]["fits"] is False
    assert result["all_regions"][3]["healthy"] is False
    assert result["all_regions"][3]["fits"] is False
    assert result["ineligible_regions"] == [
        {"region": "us-east-1", "reason": "SageMaker HyperPod is not deployed"},
        {"region": "ca-central-1", "reason": "SageMaker HyperPod is not deployed"},
    ]
    assert result["hyperpod"]["cluster_name"] == _HP_CLUSTER
    assert result["telemetry_errors"] is None


def test_hyperpod_ranking_breaks_ties_on_pool_then_nodes_then_name() -> None:
    checker = _checker()
    regions = [
        _region("us-west-2", hyperpod=_hp(groups=[("g5", 2, 2, 1)])),
        _region("us-east-1", hyperpod=_hp(groups=[("g5", 2, 2, 1)])),
        _region("eu-west-1", hyperpod=_hp(groups=[("p5", 1, 1, 8)])),
        _region("ap-south-1", hyperpod=_hp(groups=[("g5", 4, 4, None)])),
    ]
    result = _recommend(checker, regions, backend=BACKEND_HYPERPOD)
    # Equal scores: the largest known GPU pool first; the two equal pools by
    # name; the unknown pool counts as none and comes last despite its four
    # nodes (the node count only orders Regions whose pools are equal).
    assert [r["score"] for r in result["all_regions"]] == [0.0, 0.0, 0.0, 0.0]
    assert [r["region"] for r in result["all_regions"]] == [
        "eu-west-1",
        "us-east-1",
        "us-west-2",
        "ap-south-1",
    ]
    assert result["all_regions"][3]["gpus_available"] is None
    assert (
        result["reason"]
        == "HyperPod cluster InService (1 of 1 nodes, 8 GPUs), empty queue, no running jobs"
    )


def test_hyperpod_ranking_penalises_a_missing_queue_signal_and_an_unreadable_cluster() -> None:
    checker = _checker()
    unreadable = HyperpodCapacity(
        deployed=True,
        cluster_name=_HP_CLUSTER,
        unavailable_signals=["cluster"],
        errors=["HyperPod cluster gco-hyperpod could not be described: denied"],
    )
    regions = [
        _region("us-east-1", unavailable=["queue"], hyperpod=_hp()),
        _region("eu-west-1", hyperpod=unreadable),
    ]
    result = _recommend(checker, regions, gpu_count=4, backend=BACKEND_HYPERPOD)
    # us-east-1: the queue signal missing and a pool too small for 4 GPUs.
    # eu-west-1: the cluster signal missing and therefore not healthy; its
    # pool is unknown, so the fit is not judged. Equal scores, and the known
    # pool breaks the tie.
    assert [r["region"] for r in result["all_regions"]] == ["us-east-1", "eu-west-1"]
    assert [r["score"] for r in result["all_regions"]] == [2000.0, 2000.0]
    assert result["all_regions"][0]["fits"] is False
    assert result["all_regions"][1]["fits"] is None
    assert result["all_regions"][1]["telemetry_errors"] == [
        "HyperPod cluster gco-hyperpod could not be described: denied"
    ]
    assert result["reason"] == (
        "HyperPod cluster InService (2 of 2 nodes, 2 GPUs), cannot fit the 4 GPUs requested "
        "(2 on HyperPod nodes), job queue telemetry unavailable"
    )
    assert result["telemetry_errors"] == ["queue telemetry failed"]


def test_hyperpod_ranking_explains_an_unreadable_or_unhealthy_winner() -> None:
    checker = _checker()
    unreadable = HyperpodCapacity(
        deployed=True, cluster_name=_HP_CLUSTER, unavailable_signals=["cluster"]
    )
    result = _recommend(
        checker, [_region("us-east-1", hyperpod=unreadable)], gpu_count=2, backend=BACKEND_HYPERPOD
    )
    assert result["reason"] == (
        "HyperPod cluster could not be described, GPU count of its HyperPod nodes is unknown, "
        "empty queue, no running jobs"
    )
    updating = _hp(status="Updating", groups=[("g5", 1, 2, None)])
    result = _recommend(
        checker,
        [_region("us-east-1", queue_depth=2, running_jobs=3, hyperpod=updating)],
        backend=BACKEND_HYPERPOD,
    )
    assert result["reason"] == (
        "HyperPod cluster Updating (1 of 2 nodes), low queue depth (2), few running jobs (3)"
    )
    unknown_state = _hp(status="", groups=[("g5", 1, 1, 1)])
    result = _recommend(
        checker,
        [_region("us-east-1", queue_depth=9, running_jobs=9, hyperpod=unknown_state)],
        backend=BACKEND_HYPERPOD,
    )
    assert result["reason"] == "HyperPod cluster in an unknown state (1 of 1 nodes)"


def test_hyperpod_ranking_with_an_instance_type_keeps_only_matching_groups() -> None:
    checker = _checker()
    regions = [
        _region(
            "us-east-1", hyperpod=_hp(groups=[("g5.8xlarge", 2, 2, 1), ("p5.48xlarge", 1, 1, 8)])
        ),
        _region("eu-west-1", hyperpod=_hp(groups=[("g5.8xlarge", 4, 4, 1)])),
        _region(
            "ap-south-1",
            hyperpod=HyperpodCapacity(deployed=True, unavailable_signals=["cluster"]),
        ),
    ]
    result = _recommend(
        checker, regions, instance_type="p5.48xlarge", gpu_count=8, backend=BACKEND_HYPERPOD
    )
    assert result["region"] == "us-east-1"
    assert result["instance_type"] == "ml.p5.48xlarge"
    # The pool is the matching group's GPUs, not the whole cluster's.
    assert result["all_regions"][0]["gpus_available"] == 8
    assert result["all_regions"][0]["fits"] is True
    # An unreadable cluster cannot be ruled out by type, so it stays ranked (last).
    assert [r["region"] for r in result["all_regions"]] == ["us-east-1", "ap-south-1"]
    assert result["ineligible_regions"] == [
        {"region": "eu-west-1", "reason": "no HyperPod instance group of ml.p5.48xlarge"}
    ]


def test_hyperpod_ranking_refuses_when_no_region_qualifies() -> None:
    checker = _checker()
    checker._last_region_errors = ["eu-west-1: timed out"]
    regions = [
        _region("us-east-1"),
        _region(
            "us-west-2",
            hyperpod=HyperpodCapacity(
                deployed=False,
                unavailable_signals=["cluster"],
                errors=["Regional stack outputs could not be read"],
            ),
        ),
    ]
    with pytest.raises(NoEligibleRegionError) as excinfo:
        _recommend(checker, regions, backend=BACKEND_HYPERPOD)
    assert str(excinfo.value) == (
        "No deployed Region runs SageMaker HyperPod (us-east-1: SageMaker HyperPod is not "
        "deployed; us-west-2: Regional stack outputs could not be read; eu-west-1: timed out). "
        "Turn on hyperpod in cdk.json for a Region and deploy it, or choose a Region with --region"
    )
    assert excinfo.value.ineligible == [
        {"region": "us-east-1", "reason": "SageMaker HyperPod is not deployed"},
        {"region": "us-west-2", "reason": "Regional stack outputs could not be read"},
    ]


def test_backend_ranking_refuses_when_no_stack_was_found() -> None:
    checker = _checker()
    with pytest.raises(NoEligibleRegionError, match=r"\(no regional stack was found\)"):
        _recommend(checker, [], backend=BACKEND_PCS)


# ---------------------------------------------------------------------------
# The checker: ranking PCS Regions
# ---------------------------------------------------------------------------


def _pcs(
    *,
    status: str = "ACTIVE",
    max_nodes: int | None = 8,
    running: int | None = 2,
    pending: int | None = 0,
    running_jobs: int | None = 0,
    idle: int | None = 1,
    unavailable: list[str] | None = None,
    errors: list[str] | None = None,
) -> PcsCapacity:
    headroom = None if max_nodes is None or running is None else max(0, max_nodes - running)
    return PcsCapacity(
        deployed=True,
        cluster_id=_PCS_CLUSTER,
        cluster_name="gco-pcs",
        mode="managed",
        status=status or None,
        healthy=status == "ACTIVE",
        max_nodes=max_nodes,
        running_nodes=running,
        headroom=headroom,
        slurm_pending_jobs=pending,
        slurm_running_jobs=running_jobs,
        slurm_idle_nodes=idle,
        unavailable_signals=list(unavailable or []),
        errors=list(errors or []),
    )


def test_pcs_ranking_prefers_active_clusters_with_room_and_an_empty_slurm_queue() -> None:
    checker = _checker()
    regions = [
        _region("ap-south-1", pcs=_pcs(pending=3, running_jobs=2)),
        _region("eu-west-1", pcs=_pcs()),
        _region("us-west-2", pcs=_pcs(status="UPDATING")),
        # At its maximum with no idle node: new work would wait.
        _region("us-east-2", pcs=_pcs(max_nodes=4, running=4, idle=0)),
        # The Region's EKS job queue is irrelevant to Slurm work.
        _region("eu-central-1", queue_depth=50, running_jobs=50, pcs=_pcs(max_nodes=16, running=2)),
        _region("us-east-1"),
        _region("ca-central-1", pcs=PcsCapacity(deployed=False)),
    ]
    result = _recommend(checker, regions, backend=BACKEND_PCS)
    assert result["region"] == "eu-central-1"
    assert result["backend"] == "pcs"
    assert result["scoring_method"] == "pcs"
    assert result["reason"] == (
        "PCS cluster ACTIVE, 2 of 16 compute nodes launched, 14 more can launch, "
        "no pending Slurm jobs, 1 idle Slurm nodes"
    )
    assert [r["region"] for r in result["all_regions"]] == [
        "eu-central-1",
        "eu-west-1",
        "ap-south-1",
        "us-east-2",
        "us-west-2",
    ]
    assert [r["score"] for r in result["all_regions"]] == [0.0, 0.0, 40.0, 500.0, 1000.0]
    assert result["all_regions"][3]["exhausted"] is True
    assert result["all_regions"][4]["healthy"] is False
    assert result["ineligible_regions"] == [
        {"region": "us-east-1", "reason": "AWS PCS is not deployed"},
        {"region": "ca-central-1", "reason": "AWS PCS is not deployed"},
    ]
    assert result["pcs"]["cluster_id"] == _PCS_CLUSTER
    assert result["queue_depth"] == 50


def test_pcs_ranking_breaks_ties_on_headroom_then_idle_nodes_then_name() -> None:
    checker = _checker()
    regions = [
        _region("us-west-2", pcs=_pcs(max_nodes=8, running=4, idle=0)),
        _region("us-east-1", pcs=_pcs(max_nodes=8, running=4, idle=0)),
        _region("eu-west-1", pcs=_pcs(max_nodes=8, running=4, idle=2)),
        _region("ap-south-1", pcs=_pcs(max_nodes=8, running=2, idle=0)),
    ]
    result = _recommend(checker, regions, backend=BACKEND_PCS)
    assert [r["region"] for r in result["all_regions"]] == [
        "ap-south-1",
        "eu-west-1",
        "us-east-1",
        "us-west-2",
    ]
    assert result["reason"] == (
        "PCS cluster ACTIVE, 2 of 8 compute nodes launched, 6 more can launch, no pending Slurm jobs"
    )


def test_pcs_ranking_penalises_each_unreadable_source() -> None:
    checker = _checker()
    regions = [
        _region(
            "us-east-1",
            pcs=_pcs(
                status="",
                unavailable=["cluster"],
                errors=["PCS cluster pcs_0123456789abcdef could not be read: denied"],
            ),
        ),
        _region(
            "eu-west-1",
            pcs=_pcs(max_nodes=None, running=None, unavailable=["nodes"], errors=["groups denied"]),
        ),
        _region(
            "ap-south-1",
            pcs=_pcs(
                pending=None,
                running_jobs=None,
                idle=None,
                unavailable=["slurm"],
                errors=["Slurm status of the PCS backend could not be read: 502"],
            ),
        ),
    ]
    result = _recommend(checker, regions, backend=BACKEND_PCS)
    # One missing signal each; the unreadable cluster is also not ACTIVE. The
    # headroom tie-break puts the Region with known room first.
    assert [r["region"] for r in result["all_regions"]] == ["ap-south-1", "eu-west-1", "us-east-1"]
    assert [r["score"] for r in result["all_regions"]] == [1000.0, 1000.0, 2000.0]
    assert result["reason"] == (
        "PCS cluster ACTIVE, 2 of 8 compute nodes launched, 6 more can launch, Slurm status unavailable"
    )
    assert result["telemetry_errors"] == ["Slurm status of the PCS backend could not be read: 502"]
    # The unreadable sources' winners explain themselves too.
    result = _recommend(checker, regions[:1], backend=BACKEND_PCS)
    assert result["reason"] == (
        "PCS cluster could not be read, 2 of 8 compute nodes launched, 6 more can launch, "
        "no pending Slurm jobs, 1 idle Slurm nodes"
    )
    result = _recommend(checker, regions[1:2], backend=BACKEND_PCS)
    assert result["reason"] == (
        "PCS cluster ACTIVE, node groups unreadable, no pending Slurm jobs, 1 idle Slurm nodes"
    )


def test_pcs_ranking_explains_an_unhealthy_exhausted_winner() -> None:
    checker = _checker()
    region = _region(
        "us-east-1", pcs=_pcs(status="CREATING", max_nodes=2, running=2, idle=0, pending=4)
    )
    result = _recommend(checker, [region], backend=BACKEND_PCS)
    assert result["score"] == 1540.0
    assert result["reason"] == (
        "PCS cluster CREATING, 2 of 2 compute nodes launched, 0 more can launch, "
        "4 pending Slurm jobs, at capacity: new work would wait"
    )
    unknown = _region("us-east-1", pcs=_pcs(status="", max_nodes=0, running=0, idle=0))
    assert _recommend(checker, [unknown], backend=BACKEND_PCS)["reason"].startswith(
        "PCS cluster in an unknown state, "
    )


def test_pcs_ranking_refuses_when_no_region_qualifies() -> None:
    checker = _checker()
    with pytest.raises(NoEligibleRegionError, match="No deployed Region runs AWS PCS") as excinfo:
        _recommend(checker, [_region("us-east-1")], backend=BACKEND_PCS)
    assert "Turn on pcs in cdk.json" in str(excinfo.value)


def test_queue_reasons_cover_every_band() -> None:
    checker = _checker()
    assert checker._queue_reasons(_region("r", unavailable=["queue"])) == [
        "job queue telemetry unavailable"
    ]
    assert checker._queue_reasons(_region("r")) == ["empty queue", "no running jobs"]
    assert checker._queue_reasons(_region("r", queue_depth=4, running_jobs=4)) == [
        "low queue depth (4)",
        "few running jobs (4)",
    ]
    assert checker._queue_reasons(_region("r", queue_depth=5, running_jobs=5)) == []


# ---------------------------------------------------------------------------
# CLI and MCP surfaces
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_recommend_region_backend_hyperpod_verbose_lists_the_hyperpod_signals(
    runner: CliRunner,
) -> None:
    checker = MagicMock()
    checker.recommend_region_for_job.return_value = {
        "region": "eu-west-1",
        "reason": "HyperPod cluster InService (2 of 2 nodes, 2 GPUs)",
        "all_regions": [
            {
                "region": "eu-west-1",
                "score": 0.0,
                "queue_depth": 0,
                "gpus_available": 2,
                "hyperpod": {"status": "InService", "nodes_current": 2, "nodes_target": 2},
            },
            {
                "region": "us-east-1",
                "score": 2000.0,
                "queue_depth": 3,
                "gpus_available": None,
                "hyperpod": {"status": None, "nodes_current": 0, "nodes_target": 0},
            },
        ],
        "ineligible_regions": [
            {"region": "us-west-2", "reason": "SageMaker HyperPod is not deployed"}
        ],
    }
    with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
        result = runner.invoke(
            cli,
            [
                "--verbose",
                "capacity",
                "recommend-region",
                "--backend",
                "hyperpod",
                "--gpu-count",
                "2",
            ],
        )
    assert result.exit_code == 0, result.output
    checker.recommend_region_for_job.assert_called_once_with(
        gpu_required=False, min_gpus=0, instance_type=None, gpu_count=2, backend="hyperpod"
    )
    assert "Recommended region: eu-west-1" in result.output
    assert (
        "  eu-west-1: score=0.0000, hyperpod=InService, nodes=2/2, gpus=2, queue=0" in result.output
    )
    assert (
        "  us-east-1: score=2000.0000, hyperpod=unknown, nodes=0/0, gpus=?, queue=3"
        in result.output
    )
    assert "  us-west-2: not eligible (SageMaker HyperPod is not deployed)" in result.output


def test_recommend_region_backend_pcs_verbose_lists_the_pcs_signals(runner: CliRunner) -> None:
    checker = MagicMock()
    checker.recommend_region_for_job.return_value = {
        "region": "eu-west-1",
        "reason": "PCS cluster ACTIVE",
        "all_regions": [
            {
                "region": "eu-west-1",
                "score": 0.0,
                "headroom": 6,
                "pcs": {"status": "ACTIVE", "slurm_pending_jobs": 0},
            },
            {"region": "us-east-1", "score": 3000.0, "headroom": None, "pcs": {}},
        ],
        "ineligible_regions": [],
    }
    with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
        result = runner.invoke(
            cli, ["--verbose", "capacity", "recommend-region", "--backend", "pcs"]
        )
    assert result.exit_code == 0, result.output
    assert "  eu-west-1: score=0.0000, pcs=ACTIVE, pending=0, headroom=6" in result.output
    assert "  us-east-1: score=3000.0000, pcs=unknown, pending=?, headroom=?" in result.output


def test_recommend_region_rejects_an_unknown_backend(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["capacity", "recommend-region", "--backend", "slurm"])
    assert result.exit_code == 2
    assert "Invalid value for '--backend'" in result.output


def test_recommend_region_reports_no_eligible_region(runner: CliRunner) -> None:
    checker = MagicMock()
    checker.recommend_region_for_job.side_effect = NoEligibleRegionError(
        "No deployed Region runs AWS PCS (us-east-1: AWS PCS is not deployed)", []
    )
    with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
        result = runner.invoke(cli, ["capacity", "recommend-region", "--backend", "pcs"])
    assert result.exit_code == 1
    assert "Failed to get recommendation: No deployed Region runs AWS PCS" in result.output


class TestSlurmSubmitAutoRegion:
    @pytest.fixture
    def api(self) -> Iterator[MagicMock]:
        client = MagicMock()
        client.call_api.return_value = {"job_id": 7, "name": "hello", "backend": "pcs"}
        with patch("cli.aws_client.get_aws_client", return_value=client):
            yield client

    @pytest.fixture
    def script(self, tmp_path: Any) -> Any:
        path = tmp_path / "hello.sbatch"
        path.write_text("#!/bin/bash\necho hi\n")
        return path

    def test_picks_a_pcs_region_and_submits_there(self, runner, api, script) -> None:
        checker = MagicMock()
        checker.recommend_region_for_job.return_value = {
            "region": "eu-west-1",
            "reason": "PCS cluster ACTIVE, 6 more can launch",
        }
        with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
            result = runner.invoke(cli, ["slurm", "submit", str(script), "--auto-region"])
        assert result.exit_code == 0, result.output
        checker.recommend_region_for_job.assert_called_once_with(backend="pcs")
        assert "Selected region: eu-west-1 (PCS cluster ACTIVE, 6 more can launch)" in result.output
        request = api.call_api.call_args.kwargs
        assert request["region"] == "eu-west-1"
        assert request["params"] == {"backend": "pcs"}
        assert "to the default partition in eu-west-1 (pcs)" in result.output

    def test_explicit_region_wins(self, runner, api, script) -> None:
        with patch("cli.capacity.get_multi_region_capacity_checker") as factory:
            result = runner.invoke(
                cli, ["slurm", "submit", str(script), "--auto-region", "-r", "us-east-1"]
            )
        assert result.exit_code == 0, result.output
        factory.assert_not_called()
        assert api.call_api.call_args.kwargs["region"] == "us-east-1"
        assert "params" not in api.call_api.call_args.kwargs

    def test_slinky_has_no_recommendation(self, runner, api, script) -> None:
        result = runner.invoke(
            cli, ["slurm", "submit", str(script), "--auto-region", "--backend", "slinky"]
        )
        assert result.exit_code == 2
        assert "cannot be combined with --backend slinky" in result.output
        api.call_api.assert_not_called()

    def test_recommendation_failure_stops_the_submit(self, runner, api, script) -> None:
        checker = MagicMock()
        checker.recommend_region_for_job.side_effect = NoEligibleRegionError(
            "No deployed Region runs AWS PCS (us-east-1: AWS PCS is not deployed)", []
        )
        with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
            result = runner.invoke(cli, ["slurm", "submit", str(script), "--auto-region"])
        assert result.exit_code == 1
        assert "Failed to recommend a Region: No deployed Region runs AWS PCS" in result.output
        api.call_api.assert_not_called()


def test_mcp_recommend_region_passes_the_backend() -> None:
    import run_mcp

    with patch("cli_runner.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        run_mcp.recommend_region(backend="hyperpod", gpu_count=8)
        cmd = run.call_args[0][0]
    assert cmd[cmd.index("--backend") : cmd.index("--backend") + 2] == ["--backend", "hyperpod"]
    assert cmd[cmd.index("--gpu-count") : cmd.index("--gpu-count") + 2] == ["--gpu-count", "8"]
    with patch("cli_runner.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        run_mcp.recommend_region()
        assert "--backend" not in run.call_args[0][0]
