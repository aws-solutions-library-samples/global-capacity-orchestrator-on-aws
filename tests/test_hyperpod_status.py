"""Tests for ``cli/hyperpod_status.py``: the deployed HyperPod cluster behind ``gco stacks hyperpod status``.

Real botocore clients (CloudFormation, SageMaker) run behind ``Stubber`` so
every call and its parameters are asserted without a network.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber

from cli import hyperpod_status as hs

_REGION = "us-east-1"
_STACK = "acme-us-east-1"
_CLUSTER = "acme-hyperpod"
_ARN = f"arn:aws:sagemaker:{_REGION}:123456789012:cluster/abc123"
_PLAN_ARN = f"arn:aws:sagemaker:{_REGION}:123456789012:training-plan/p5-plan"
_NOW = datetime(2026, 10, 1, tzinfo=UTC)


class _Clients:
    def __init__(self) -> None:
        kwargs = {
            "region_name": _REGION,
            "aws_access_key_id": "testing",
            "aws_secret_access_key": "testing",  # nosec B106  # fake credentials for stubbed clients
        }
        self.clients = {
            service: boto3.client(service, **kwargs) for service in ("cloudformation", "sagemaker")
        }
        self.stubs = {service: Stubber(client) for service, client in self.clients.items()}
        self.requested: list[str] = []

    def factory(self, service: str, *, region_name: str) -> Any:
        assert region_name == _REGION
        self.requested.append(service)
        return self.clients[service]

    def __getitem__(self, service: str) -> Stubber:
        return self.stubs[service]


@pytest.fixture
def clients() -> Iterator[_Clients]:
    stubbed = _Clients()
    for stub in stubbed.stubs.values():
        stub.activate()
    yield stubbed
    for stub in stubbed.stubs.values():
        stub.assert_no_pending_responses()
        stub.deactivate()


def _expect_stack(clients: _Clients, outputs: dict[str, str] | None) -> None:
    if outputs is None:
        clients["cloudformation"].add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message=f"Stack with id {_STACK} does not exist",
            expected_params={"StackName": _STACK},
        )
        return
    clients["cloudformation"].add_response(
        "describe_stacks",
        {
            "Stacks": [
                {
                    "StackName": _STACK,
                    "CreationTime": _NOW,
                    "StackStatus": "CREATE_COMPLETE",
                    "Outputs": [
                        {"OutputKey": key, "OutputValue": value} for key, value in outputs.items()
                    ],
                }
            ]
        },
        {"StackName": _STACK},
    )


_HYPERPOD_OUTPUTS = {
    "HyperpodClusterName": _CLUSTER,
    "HyperpodClusterArn": _ARN,
    "ClusterName": "acme-us-east-1",  # the EKS cluster: not a HyperPod output
}


def _expect_cluster(clients: _Clients, **overrides: Any) -> None:
    cluster: dict[str, Any] = {
        "ClusterArn": _ARN,
        "ClusterName": _CLUSTER,
        "ClusterStatus": "InService",
        "CreationTime": _NOW,
        "InstanceGroups": [
            {
                "InstanceGroupName": "accelerated",
                "InstanceType": "ml.g5.8xlarge",
                "CurrentCount": 1,
                "TargetCount": 2,
                "Status": "Updating",
                "TrainingPlanArn": _PLAN_ARN,
            },
            {"InstanceGroupName": "cpu", "InstanceType": "ml.c5.large"},
        ],
    }
    cluster.update(overrides)
    clients["sagemaker"].add_response("describe_cluster", cluster, {"ClusterName": _CLUSTER})


def test_stack_outputs_keeps_only_the_hyperpod_outputs_without_their_prefix(
    clients: _Clients,
) -> None:
    _expect_stack(clients, _HYPERPOD_OUTPUTS)
    outputs = hs.stack_outputs(_REGION, _STACK, client_factory=clients.factory)
    assert outputs == {"ClusterName": _CLUSTER, "ClusterArn": _ARN}


def test_stack_outputs_of_a_missing_stack_are_empty(clients: _Clients) -> None:
    _expect_stack(clients, None)
    assert hs.stack_outputs(_REGION, _STACK, client_factory=clients.factory) == {}


def test_stack_outputs_without_stacks_or_outputs(clients: _Clients) -> None:
    clients["cloudformation"].add_response("describe_stacks", {"Stacks": []}, {"StackName": _STACK})
    assert hs.stack_outputs(_REGION, _STACK, client_factory=clients.factory) == {}
    clients["cloudformation"].add_response(
        "describe_stacks",
        {"Stacks": [{"StackName": _STACK, "CreationTime": _NOW, "StackStatus": "CREATE_COMPLETE"}]},
        {"StackName": _STACK},
    )
    assert hs.stack_outputs(_REGION, _STACK, client_factory=clients.factory) == {}


def test_stack_outputs_other_errors_are_status_errors(clients: _Clients) -> None:
    clients["cloudformation"].add_client_error(
        "describe_stacks",
        service_error_code="AccessDenied",
        service_message="no",
        expected_params={"StackName": _STACK},
    )
    with pytest.raises(hs.HyperpodStatusError, match=f"could not read stack {_STACK} in {_REGION}"):
        hs.stack_outputs(_REGION, _STACK, client_factory=clients.factory)

    class _Cfn:
        def describe_stacks(self, **kwargs: Any) -> dict[str, Any]:
            raise EndpointConnectionError(endpoint_url="https://cfn.example")

    with pytest.raises(hs.HyperpodStatusError, match="could not read stack"):
        hs.stack_outputs(_REGION, _STACK, client_factory=lambda *_a, **_k: _Cfn())


def test_deployed_cluster_describes_the_published_cluster(clients: _Clients) -> None:
    _expect_stack(clients, _HYPERPOD_OUTPUTS)
    _expect_cluster(clients)
    deployed = hs.deployed_cluster(_REGION, _STACK, client_factory=clients.factory)
    assert deployed == hs.DeployedCluster(
        name=_CLUSTER,
        arn=_ARN,
        status="InService",
        failure_message=None,
        instance_groups=[
            hs.InstanceGroupStatus(
                name="accelerated",
                instance_type="ml.g5.8xlarge",
                current=1,
                target=2,
                status="Updating",
                training_plan_arn=_PLAN_ARN,
            ),
            hs.InstanceGroupStatus(
                name="cpu", instance_type="ml.c5.large", current=0, target=0, status=""
            ),
        ],
        outputs={"ClusterName": _CLUSTER, "ClusterArn": _ARN},
    )
    assert deployed.to_dict()["instance_groups"][0] == {
        "name": "accelerated",
        "instance_type": "ml.g5.8xlarge",
        "current": 1,
        "target": 2,
        "status": "Updating",
        "training_plan_arn": _PLAN_ARN,
    }
    assert deployed.to_dict()["outputs"] == {"ClusterName": _CLUSTER, "ClusterArn": _ARN}
    assert clients.requested == ["cloudformation", "sagemaker"]


def test_deployed_cluster_reports_a_failure_message(clients: _Clients) -> None:
    _expect_stack(clients, _HYPERPOD_OUTPUTS)
    _expect_cluster(clients, ClusterStatus="Failed", FailureMessage="insufficient capacity")
    deployed = hs.deployed_cluster(_REGION, _STACK, client_factory=clients.factory)
    assert deployed is not None
    assert (deployed.status, deployed.failure_message) == ("Failed", "insufficient capacity")


def test_deployed_cluster_is_none_without_the_output(clients: _Clients) -> None:
    _expect_stack(clients, {"ClusterName": "acme-us-east-1"})
    assert hs.deployed_cluster(_REGION, _STACK, client_factory=clients.factory) is None
    _expect_stack(clients, None)
    assert hs.deployed_cluster(_REGION, _STACK, client_factory=clients.factory) is None
    assert clients.requested == ["cloudformation", "cloudformation"]


def test_deployed_cluster_that_cannot_be_described_is_a_status_error(clients: _Clients) -> None:
    _expect_stack(clients, _HYPERPOD_OUTPUTS)
    clients["sagemaker"].add_client_error(
        "describe_cluster",
        service_error_code="ResourceNotFound",
        service_message="gone",
        expected_params={"ClusterName": _CLUSTER},
    )
    with pytest.raises(
        hs.HyperpodStatusError,
        match=f"stack {_STACK} publishes HyperPod cluster {_CLUSTER} but it cannot be described",
    ):
        hs.deployed_cluster(_REGION, _STACK, client_factory=clients.factory)
