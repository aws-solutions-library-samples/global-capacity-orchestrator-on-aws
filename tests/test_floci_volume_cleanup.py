"""Floci coverage for the post-teardown orphaned-EBS-volume sweep.

``StackManager._cleanup_cluster_volumes`` deletes the EBS volumes an EKS
cluster's CSI driver provisioned once the cluster is gone (#268). The unit
tests in ``tests/test_stacks_extended_coverage.py`` drive it through
``MagicMock`` boto3 clients, which proves the branching but not the wire
contract: whether the ``tag-key`` + ``status`` filter pair actually selects the
right volumes server-side, whether ``DeleteVolume`` really removes them, and
whether a genuinely absent cluster produces the ``ResourceNotFoundException``
the ordering gate depends on.

Here the volumes are real emulator state created through ``CreateVolume``, and
the production method runs unmodified against them over HTTP. Deletion is
verified by re-reading EC2 afterwards rather than by asserting on a mock.

Pricing is deliberately exercised too: Floci does not implement the Price List
API, so the retain path here is a real test of the degradation Jake asked for —
the sweep must report that it could not establish the cost instead of printing
a stale hardcoded rate.
"""

from __future__ import annotations

import boto3
import pytest

from tests._floci import floci_test_markers, unique_name

pytestmark = floci_test_markers()

_REGION = "us-east-1"


@pytest.fixture()
def project(verified_floci_endpoint):
    """A uniquely named GCO config whose regional stack name is ``<project>-us-east-1``."""
    from cli.config import GCOConfig

    return GCOConfig(
        project_name=unique_name("gcovol").replace("-", "")[:16],
        default_region=_REGION,
        api_gateway_region="us-east-2",
        global_region="us-east-2",
        monitoring_region="us-east-2",
        output_format="json",
    )


@pytest.fixture()
def ec2(verified_floci_endpoint):
    return boto3.client("ec2", region_name=_REGION)


def _zone(ec2_client) -> str:
    """First available zone name in the Region, for CreateVolume."""
    zones = ec2_client.describe_availability_zones()["AvailabilityZones"]
    return str(zones[0]["ZoneName"])


def _create_cluster_volume(
    ec2_client,
    *,
    cluster_name: str,
    size: int = 1,
    pvc: str | None = None,
    owned: bool = True,
) -> str:
    """Create a volume tagged the way the EBS CSI driver tags its PVs."""
    tags = []
    if owned:
        tags.append({"Key": f"kubernetes.io/cluster/{cluster_name}", "Value": "owned"})
    if pvc is not None:
        tags.append({"Key": "kubernetes.io/created-for/pvc/name", "Value": pvc})
    kwargs = {
        "AvailabilityZone": _zone(ec2_client),
        "Size": size,
        "VolumeType": "gp3",
    }
    if tags:
        kwargs["TagSpecifications"] = [{"ResourceType": "volume", "Tags": tags}]
    volume_id = str(ec2_client.create_volume(**kwargs)["VolumeId"])
    ec2_client.get_waiter("volume_available").wait(VolumeIds=[volume_id])
    return volume_id


def _volume_state(ec2_client, volume_id: str) -> str | None:
    """Current state of a volume, or None once EC2 no longer returns it."""
    from botocore.exceptions import ClientError

    try:
        volumes = ec2_client.describe_volumes(VolumeIds=[volume_id]).get("Volumes", [])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "InvalidVolume.NotFound":
            return None
        raise
    if not volumes:
        return None
    return str(volumes[0].get("State") or "")


def _gone(ec2_client, volume_id: str) -> bool:
    """Whether a volume is deleted, tolerating the terminal ``deleting`` state."""
    return _volume_state(ec2_client, volume_id) in (None, "deleted", "deleting")


class TestClusterVolumeSweepOverTheWire:
    def test_absent_cluster_lets_the_sweep_delete_its_orphaned_volumes(self, project, ec2):
        """The whole point of #268: cluster gone, volumes still billing."""
        from cli.stacks import StackManager

        stack_name = f"{project.project_name}-{_REGION}"
        volume_id = _create_cluster_volume(ec2, cluster_name=stack_name, pvc="prometheus-gco-db")

        outcome = StackManager(project)._cleanup_cluster_volumes(stack_name)

        assert outcome["errors"] == []
        assert [record["volume_id"] for record in outcome["deleted"]] == [volume_id]
        assert outcome["deleted"][0]["pvc"] == "prometheus-gco-db"
        assert _gone(ec2, volume_id), "the sweep reported a delete that EC2 did not honor"

    def test_volumes_owned_by_another_cluster_are_never_touched(self, project, ec2):
        """Tag scoping is enforced by EC2, not by client-side filtering."""
        from cli.stacks import StackManager

        stack_name = f"{project.project_name}-{_REGION}"
        mine = _create_cluster_volume(ec2, cluster_name=stack_name)
        theirs = _create_cluster_volume(ec2, cluster_name=f"{project.project_name}-eu-west-9")
        untagged = _create_cluster_volume(ec2, cluster_name=stack_name, owned=False)

        outcome = StackManager(project)._cleanup_cluster_volumes(stack_name)

        assert [record["volume_id"] for record in outcome["deleted"]] == [mine]
        assert _gone(ec2, mine)
        assert _volume_state(ec2, theirs) == "available"
        assert _volume_state(ec2, untagged) == "available"

    def test_retain_leaves_the_volume_and_reports_unavailable_pricing(self, project, ec2, capsys):
        """Floci has no Price List API, so this exercises the real fallback."""
        from cli.stacks import StackManager

        stack_name = f"{project.project_name}-{_REGION}"
        volume_id = _create_cluster_volume(ec2, cluster_name=stack_name)

        outcome = StackManager(project)._cleanup_cluster_volumes(stack_name, retain=True)

        assert outcome["deleted"] == []
        assert [record["volume_id"] for record in outcome["surviving"]] == [volume_id]
        assert _volume_state(ec2, volume_id) == "available"
        # No hardcoded rate may stand in for a lookup that did not happen.
        assert "monthly_cost_usd" not in outcome
        assert "could not retrieve current EBS pricing" in outcome["monthly_cost_unavailable"]
        assert "Ongoing cost: could not retrieve current EBS pricing" in capsys.readouterr().out

        ec2.delete_volume(VolumeId=volume_id)

    def test_present_cluster_stops_the_sweep_before_any_deletion(self, project, ec2):
        """A live cluster means a detached volume may just be between pod restarts."""
        from cli.stacks import StackManager

        eks = boto3.client("eks", region_name=_REGION)
        stack_name = f"{project.project_name}-{_REGION}"
        volume_id = _create_cluster_volume(ec2, cluster_name=stack_name)

        vpc = ec2.create_vpc(CidrBlock="10.43.0.0/16")["Vpc"]["VpcId"]
        subnet = ec2.create_subnet(
            VpcId=vpc, CidrBlock="10.43.1.0/24", AvailabilityZone=_zone(ec2)
        )["Subnet"]["SubnetId"]
        eks.create_cluster(
            name=stack_name,
            roleArn=f"arn:aws:iam::{boto3.client('sts').get_caller_identity()['Account']}"
            ":role/gco-floci-cluster-role",
            resourcesVpcConfig={"subnetIds": [subnet]},
        )

        try:
            outcome = StackManager(project)._cleanup_cluster_volumes(stack_name)

            assert outcome["cluster_present"] is True
            assert outcome["deleted"] == []
            assert outcome["inspected"] == 0
            assert _volume_state(ec2, volume_id) == "available"
        finally:
            eks.delete_cluster(name=stack_name)
            ec2.delete_volume(VolumeId=volume_id)

    def test_public_wrapper_runs_the_sweep_for_a_regional_stack(self, project, ec2):
        """``cleanup_cluster_volumes`` is what the single-stack CLI path calls."""
        from cli.stacks import StackManager

        stack_name = f"{project.project_name}-{_REGION}"
        volume_id = _create_cluster_volume(ec2, cluster_name=stack_name)

        outcome = StackManager(project).cleanup_cluster_volumes(stack_name)

        assert [record["volume_id"] for record in outcome["deleted"]] == [volume_id]
        assert _gone(ec2, volume_id)

    def test_public_wrapper_skips_global_stacks_without_calling_ec2(self, project):
        from cli.stacks import StackManager

        outcome = StackManager(project).cleanup_cluster_volumes(f"{project.project_name}-global")

        assert outcome == {
            "stack": f"{project.project_name}-global",
            "skipped": "not-a-regional-stack",
        }
