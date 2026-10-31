"""Floci emulator layer: ``cli.pcs_login`` over the wire.

``gco slurm login/output --backend pcs`` finds the managed PCS cluster's
login node through three AWS reads the PCS API has no part in — the regional
stack's ``Pcs*`` outputs (CloudFormation), the instance PCS tagged with the
login node group's id (EC2) and a one-shot ``AWS-RunShellScript`` run for a
file read (SSM) — so all of it runs here against real responses: a stack
whose outputs carry the published ids, instances tagged the way PCS tags them
in every state the finder has to tell apart, and an SSM command the emulator
registers but never executes, which is exactly the shape the read's deadline
exists for. The PCS API itself is absent from Floci (docs/FLOCI_TESTING.md,
gap 9); the ``aws ssm start-session`` argv builders are pure and stay in the
unit suite.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
import pytest

from gco.pcs_config import (
    PCS_NODE_GROUP_INSTANCE_TAG,
    PCS_OUTPUT_CLUSTER_ID,
    PCS_OUTPUT_LOGIN_NODE_GROUP_ID,
    PCS_OUTPUT_MODE,
)
from tests._floci import floci_test_markers, unique_name

pytestmark = floci_test_markers()

REGION = "us-east-1"
#: The emulator finishes a stack or an instance launch at once; botocore's
#: default waiter delays (30 s and 15 s between polls) would only add minutes.
_FAST_WAIT = {"Delay": 1, "MaxAttempts": 120}


def _deploy_stack_with_outputs(stack_name: str, outputs: dict[str, str]) -> None:
    """A CloudFormation stack publishing ``outputs`` the way the regional stack does."""
    cloudformation = boto3.client("cloudformation", region_name=REGION)
    cloudformation.create_stack(
        StackName=stack_name,
        TemplateBody=json.dumps(
            {
                "AWSTemplateFormatVersion": "2010-09-09",
                "Resources": {
                    "Marker": {
                        "Type": "AWS::SSM::Parameter",
                        "Properties": {
                            "Name": f"/{stack_name}/marker",
                            "Type": "String",
                            "Value": "present",
                        },
                    }
                },
                "Outputs": {key: {"Value": value} for key, value in outputs.items()},
            }
        ),
    )
    cloudformation.get_waiter("stack_create_complete").wait(
        StackName=stack_name, WaiterConfig=_FAST_WAIT
    )


def _delete_stack(stack_name: str) -> None:
    cloudformation = boto3.client("cloudformation", region_name=REGION)
    cloudformation.delete_stack(StackName=stack_name)
    cloudformation.get_waiter("stack_delete_complete").wait(
        StackName=stack_name, WaiterConfig=_FAST_WAIT
    )


@pytest.fixture(scope="module")
def network(verified_floci_endpoint):
    """A VPC and subnet for the login-node instances; removed with the module."""
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id = ec2.create_vpc(CidrBlock="10.77.0.0/16")["Vpc"]["VpcId"]
    subnet_id = ec2.create_subnet(
        VpcId=vpc_id, CidrBlock="10.77.1.0/24", AvailabilityZone=f"{REGION}a"
    )["Subnet"]["SubnetId"]
    yield {"vpc_id": vpc_id, "subnet_id": subnet_id}
    ec2.delete_subnet(SubnetId=subnet_id)
    ec2.delete_vpc(VpcId=vpc_id)


def _terminate(instance_ids: list[str]) -> None:
    """Terminate each instance in its own call: the emulator holds one call ~15 s per instance."""
    ec2 = boto3.client("ec2", region_name=REGION)
    with ThreadPoolExecutor(max_workers=max(1, len(instance_ids))) as pool:
        list(
            pool.map(
                lambda instance_id: ec2.terminate_instances(InstanceIds=[instance_id]), instance_ids
            )
        )


def _launch_login_instance(subnet_id: str, node_group_id: str) -> str:
    """An instance carrying the tag PCS puts on every compute node group member."""
    ec2 = boto3.client("ec2", region_name=REGION)
    return ec2.run_instances(
        ImageId="ami-0123456789abcdef0",
        InstanceType="c6i.large",
        MinCount=1,
        MaxCount=1,
        SubnetId=subnet_id,
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [{"Key": PCS_NODE_GROUP_INSTANCE_TAG, "Value": node_group_id}],
            }
        ],
    )["Instances"][0]["InstanceId"]


class TestStackOutputs:
    def test_published_outputs_are_read_and_an_absent_stack_is_empty(self, verified_floci_endpoint):
        from cli.pcs_login import stack_outputs

        stack_name = unique_name("gcopcslogin").replace("-", "")[:20]
        assert stack_outputs(REGION, stack_name) == {}, (
            "a Region without the PCS stack must read as not deployed, not as an error"
        )
        _deploy_stack_with_outputs(
            stack_name,
            {
                PCS_OUTPUT_CLUSTER_ID: "pcs_abc123def456",
                PCS_OUTPUT_MODE: "managed",
                PCS_OUTPUT_LOGIN_NODE_GROUP_ID: "pcs_login0000001",
            },
        )
        try:
            outputs = stack_outputs(REGION, stack_name)
        finally:
            _delete_stack(stack_name)
        assert outputs == {
            PCS_OUTPUT_CLUSTER_ID: "pcs_abc123def456",
            PCS_OUTPUT_MODE: "managed",
            PCS_OUTPUT_LOGIN_NODE_GROUP_ID: "pcs_login0000001",
        }


class TestFindLoginNode:
    def test_the_newest_running_tagged_instance_is_the_login_node(
        self, verified_floci_endpoint, network
    ):
        from cli.pcs_login import LoginNode, PcsLoginError, find_login_node

        stack_name = unique_name("gcopcsnode").replace("-", "")[:20]
        node_group_id = unique_name("pcs_login").replace("-", "")[:20]
        ec2 = boto3.client("ec2", region_name=REGION)
        _deploy_stack_with_outputs(
            stack_name,
            {
                PCS_OUTPUT_CLUSTER_ID: "pcs_abc123def456",
                PCS_OUTPUT_MODE: "managed",
                PCS_OUTPUT_LOGIN_NODE_GROUP_ID: node_group_id,
            },
        )
        instances: list[str] = []
        try:
            # Outputs published, no instance yet: PCS launches the node after
            # the deploy, and the finder has to say so rather than fail oddly.
            with pytest.raises(PcsLoginError, match="is not running yet"):
                find_login_node(REGION, stack_name)

            # A node PCS is replacing: the old one stopped, a newer one running
            # beside it, and an unrelated running instance from another group.
            # The emulator takes about fifteen seconds to bring an instance
            # up, so the two older ones launch together.
            with ThreadPoolExecutor(max_workers=2) as pool:
                replaced, other_group = pool.map(
                    _launch_login_instance,
                    [network["subnet_id"], network["subnet_id"]],
                    [node_group_id, f"{node_group_id}x"],
                )
            instances.extend([replaced, other_group])
            ec2.stop_instances(InstanceIds=[replaced])
            time.sleep(1.1)  # LaunchTime is what ranks the survivors
            current = _launch_login_instance(network["subnet_id"], node_group_id)
            instances.append(current)
            ec2.get_waiter("instance_running").wait(InstanceIds=[current], WaiterConfig=_FAST_WAIT)
            ec2.get_waiter("instance_stopped").wait(InstanceIds=[replaced], WaiterConfig=_FAST_WAIT)

            node = find_login_node(REGION, stack_name)
            assert node == LoginNode(
                region=REGION,
                cluster_id="pcs_abc123def456",
                node_group_id=node_group_id,
                instance_id=current,
            )
        finally:
            _terminate(instances)
            _delete_stack(stack_name)

    def test_each_undeployed_shape_is_named_by_its_remedy(self, verified_floci_endpoint):
        from cli.pcs_login import PcsLoginError, find_login_node

        stack_name = unique_name("gcopcsmode").replace("-", "")[:20]
        with pytest.raises(PcsLoginError, match="AWS PCS is not deployed in us-east-1"):
            find_login_node(REGION, stack_name)

        # An operator-run cluster: the stack publishes the cluster but GCO
        # manages no login node for it.
        _deploy_stack_with_outputs(
            stack_name, {PCS_OUTPUT_CLUSTER_ID: "pcs_existing00001", PCS_OUTPUT_MODE: "existing"}
        )
        try:
            with pytest.raises(PcsLoginError, match=r"operator-run \(pcs\.mode existing\)"):
                find_login_node(REGION, stack_name)
        finally:
            _delete_stack(stack_name)

        # A managed cluster deployed with the login node group turned off.
        _deploy_stack_with_outputs(
            stack_name, {PCS_OUTPUT_CLUSTER_ID: "pcs_managed000001", PCS_OUTPUT_MODE: "managed"}
        )
        try:
            with pytest.raises(PcsLoginError, match="has no login node"):
                find_login_node(REGION, stack_name)
        finally:
            _delete_stack(stack_name)


class TestReadFile:
    def test_a_command_the_node_never_finishes_fails_at_the_deadline(
        self, verified_floci_endpoint, network
    ):
        """The emulator registers the command but runs nothing: Pending forever.

        That is the shape a stopped SSM agent produces on a real node, and the
        read must give up at its deadline naming the instance and the command
        rather than poll indefinitely.
        """
        from cli.pcs_login import LoginNode, PcsLoginError, read_file

        node_group_id = unique_name("pcs_login").replace("-", "")[:20]
        instance_id = _launch_login_instance(network["subnet_id"], node_group_id)
        ssm = boto3.client("ssm", region_name=REGION)
        node = LoginNode(
            region=REGION,
            cluster_id="pcs_abc123def456",
            node_group_id=node_group_id,
            instance_id=instance_id,
        )
        try:
            with pytest.raises(PcsLoginError) as excinfo:
                read_file(node, ["cat", "/home/gco-slurm/job.out"], timeout=1.0, poll_interval=0.2)
            message = str(excinfo.value)
            assert message.startswith("timed out after 1s waiting for cat on the PCS login node")
            assert instance_id in message
            command_id = message.rsplit("SSM command ", 1)[-1].rstrip(")")
            # The command the read sent is the one the emulator holds: the
            # reader quoted as one shell line, the floor on the SSM timeout.
            command = ssm.list_commands(CommandId=command_id)["Commands"][0]
            assert command["DocumentName"] == "AWS-RunShellScript"
            assert command["InstanceIds"] == [instance_id]
            assert command["Parameters"]["commands"] == ["cat /home/gco-slurm/job.out"]
            assert command["TimeoutSeconds"] == 30
            assert command["Comment"] == "gco slurm output (PCS login node)"
        finally:
            _terminate([instance_id])
