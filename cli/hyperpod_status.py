"""What the regional stack and SageMaker say about one Region's HyperPod cluster.

``gco stacks hyperpod status`` pairs the configured block with this: the
``Hyperpod*`` outputs the regional stack publishes once HyperPod is deployed,
and the cluster SageMaker describes under the published name (its status and
every instance group's current/target counts). Nothing here edits anything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from gco.hyperpod_config import HYPERPOD_OUTPUT_CLUSTER_NAME

__all__ = [
    "DeployedCluster",
    "HyperpodStatusError",
    "InstanceGroupStatus",
    "deployed_cluster",
    "stack_outputs",
]

ClientFactory = Callable[..., Any]

#: The regional stack's HyperPod outputs all carry this prefix.
OUTPUT_PREFIX = "Hyperpod"


class HyperpodStatusError(RuntimeError):
    """The stack or the cluster could not be read; the message says why."""


@dataclass(frozen=True)
class InstanceGroupStatus:
    """One instance group as SageMaker reports it."""

    name: str
    instance_type: str
    current: int
    target: int
    status: str
    training_plan_arn: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "instance_type": self.instance_type,
            "current": self.current,
            "target": self.target,
            "status": self.status,
            "training_plan_arn": self.training_plan_arn,
        }


@dataclass(frozen=True)
class DeployedCluster:
    """The deployed cluster: the stack's outputs and SageMaker's view of it."""

    name: str
    arn: str
    status: str
    failure_message: str | None
    instance_groups: list[InstanceGroupStatus]
    outputs: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arn": self.arn,
            "status": self.status,
            "failure_message": self.failure_message,
            "instance_groups": [group.to_dict() for group in self.instance_groups],
            "outputs": dict(self.outputs),
        }


def stack_outputs(
    region: str, stack_name: str, *, client_factory: ClientFactory = boto3.client
) -> dict[str, str]:
    """The regional stack's ``Hyperpod*`` outputs (prefix stripped), ``{}`` when it does not exist."""
    cfn = client_factory("cloudformation", region_name=region)
    try:
        response = cfn.describe_stacks(StackName=stack_name)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        if error.get("Code") == "ValidationError" and "does not exist" in str(
            error.get("Message", "")
        ):
            return {}
        raise HyperpodStatusError(f"could not read stack {stack_name} in {region}: {exc}") from exc
    except BotoCoreError as exc:
        raise HyperpodStatusError(f"could not read stack {stack_name} in {region}: {exc}") from exc
    stacks = response.get("Stacks") or []
    if not stacks:
        return {}
    return {
        str(output["OutputKey"]).removeprefix(OUTPUT_PREFIX): str(output.get("OutputValue") or "")
        for output in stacks[0].get("Outputs") or []
        if str(output.get("OutputKey") or "").startswith(OUTPUT_PREFIX)
    }


def deployed_cluster(
    region: str, stack_name: str, *, client_factory: ClientFactory = boto3.client
) -> DeployedCluster | None:
    """The cluster the regional stack published, or ``None`` when HyperPod is not deployed there.

    Raises :class:`HyperpodStatusError` when the stack cannot be read or the
    published cluster cannot be described (deleted outside CloudFormation, a
    denied call).
    """
    outputs = stack_outputs(region, stack_name, client_factory=client_factory)
    name = outputs.get(HYPERPOD_OUTPUT_CLUSTER_NAME.removeprefix(OUTPUT_PREFIX))
    if not name:
        return None
    sagemaker = client_factory("sagemaker", region_name=region)
    try:
        cluster = sagemaker.describe_cluster(ClusterName=name)
    except (ClientError, BotoCoreError) as exc:
        raise HyperpodStatusError(
            f"stack {stack_name} publishes HyperPod cluster {name} but it cannot be described: {exc}"
        ) from exc
    groups = [
        InstanceGroupStatus(
            name=str(group.get("InstanceGroupName") or ""),
            instance_type=str(group.get("InstanceType") or ""),
            current=int(group.get("CurrentCount") or 0),
            target=int(group.get("TargetCount") or 0),
            status=str(group.get("Status") or ""),
            training_plan_arn=(
                str(group["TrainingPlanArn"]) if group.get("TrainingPlanArn") else None
            ),
        )
        for group in cluster.get("InstanceGroups") or []
    ]
    failure = cluster.get("FailureMessage")
    return DeployedCluster(
        name=name,
        arn=str(cluster.get("ClusterArn") or ""),
        status=str(cluster.get("ClusterStatus") or ""),
        failure_message=str(failure) if failure else None,
        instance_groups=groups,
        outputs=outputs,
    )
