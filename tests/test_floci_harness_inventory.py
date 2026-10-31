"""Floci layer: the live-validation harness's inventory machinery for real.

``scripts/live_release_validation`` decides what it may destroy from what its
inventory scanners observe, and ``final-inventory`` passes only when the
account is provably clean. The unit suite drives this with patched clients;
here the same functions run against emulator state through the harness's own
``ThrottleResilientSession``: region discovery uses the genuine
partition/available-region cross-check, the fail-closed scanner coverage
executes every service scanner over the wire, and baseline capture and
comparison fingerprint real CloudFormation stacks. This is the foundation the
Floci E2E (test_floci_live_validation_e2e.py) builds on — each piece is
proven here in isolation first, so an E2E failure points at orchestration,
not at scanners.
"""

from __future__ import annotations

import json

import boto3
import pytest

from tests._floci import floci_test_markers, unique_name

pytestmark = floci_test_markers()


@pytest.fixture(scope="module")
def harness_session(verified_floci_endpoint):
    from scripts.live_release_validation.aws_session import ThrottleResilientSession
    from tests._floci import apply_known_floci_gap_shims

    session = ThrottleResilientSession()
    # The documented Floci gaps get local answers (unparseable GetStackPolicy
    # responses; X-Ray absent; no canonical Availability Zone
    # IDs) so the other scanners run against the emulator for real. See
    # tests/_floci_gap_shims.py for the per-gap rationale.
    apply_known_floci_gap_shims(session.events)
    return session


def _deploy_marker_stack(region: str, stack_name: str) -> None:
    cloudformation = boto3.client("cloudformation", region_name=region)
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
            }
        ),
    )
    cloudformation.get_waiter("stack_create_complete").wait(StackName=stack_name)


def _delete_stack(region: str, stack_name: str) -> None:
    cloudformation = boto3.client("cloudformation", region_name=region)
    cloudformation.delete_stack(StackName=stack_name)
    cloudformation.get_waiter("stack_delete_complete").wait(StackName=stack_name)


class TestRegionDiscovery:
    def test_enabled_regions_cross_check_partition_and_ec2(self, harness_session):
        from scripts.live_release_validation.inventory.stacks import discover_enabled_regions

        regions = discover_enabled_regions(harness_session, "us-east-1")
        assert "us-east-1" in regions and "us-east-2" in regions, (
            "the emulator's describe_regions must intersect botocore's partition data "
            f"to a usable region list, got {regions}"
        )

    def test_a_service_without_sdk_endpoint_data_is_placed_by_the_public_parameters(
        self, harness_session
    ):
        """AWS PCS ships only an endpoint ruleset, so botocore names no Region for it.

        The inventory falls back to the public parameters under
        ``/aws/service/global-infrastructure/services/pcs/regions`` (served
        here by the documented gap shim with a fixture list: the Regions the
        emulator enables, without us-west-1) and must come back with the
        parameters' Regions, not botocore's every-Region list: the PR #496
        live runs scanned PCS in zero Regions because the empty SDK list was
        taken at face value.
        """
        from scripts.live_release_validation.inventory.project import _service_regions

        assert harness_session.get_available_regions("pcs", partition_name="aws") == [], (
            "botocore now lists PCS Regions; the public-parameter fallback and its shim "
            "can be retired"
        )
        pcs_regions = _service_regions(
            harness_session, "pcs", partition="aws", seed_region="us-east-1"
        )
        assert pcs_regions == {"us-east-1", "us-east-2", "eu-west-1"}
        assert "us-west-1" not in pcs_regions, "the fixture list leaves us-west-1 out"
        # A service botocore does know needs no parameter read at all.
        sagemaker_regions = _service_regions(
            harness_session, "sagemaker", partition="aws", seed_region="us-east-1"
        )
        assert "us-west-1" in sagemaker_regions


class TestProjectResourceScanners:
    def test_created_resources_are_seen_and_absence_is_proven_after_cleanup(
        self, harness_session, floci_account
    ):
        from scripts.live_release_validation.inventory.project import (
            collect_project_resources,
            project_resources_are_absent,
            summarize_project_resources,
        )

        project = unique_name("gcoscan").replace("-", "")[:14]
        region = "us-east-1"
        sqs = boto3.client("sqs", region_name=region)
        dynamodb = boto3.client("dynamodb", region_name=region)
        ec2 = boto3.client("ec2", region_name=region)
        queue_url = sqs.create_queue(QueueName=f"{project}-jobs")["QueueUrl"]
        dynamodb.create_table(
            TableName=f"{project}-jobs",
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.get_waiter("table_exists").wait(TableName=f"{project}-jobs")
        _deploy_marker_stack(region, f"{project}-{region}")
        # An EBS volume tagged the way the EKS CSI driver tags a PersistentVolume.
        # It carries no CloudFormation or gco:project tag, so every other scanner
        # is blind to it -- the exact leak #268 reported.
        zone = ec2.describe_availability_zones()["AvailabilityZones"][0]["ZoneName"]
        volume_id = ec2.create_volume(
            AvailabilityZone=zone,
            Size=1,
            VolumeType="gp3",
            TagSpecifications=[
                {
                    "ResourceType": "volume",
                    "Tags": [
                        {"Key": f"kubernetes.io/cluster/{project}-{region}", "Value": "owned"},
                        {"Key": "kubernetes.io/created-for/pvc/name", "Value": "prometheus-db"},
                    ],
                }
            ],
        )["VolumeId"]
        ec2.get_waiter("volume_available").wait(VolumeIds=[volume_id])

        inventory = collect_project_resources(
            harness_session,
            enabled_regions=[region],
            expected_account=floci_account,
            project_name=project,
            seed_region=region,
        )
        summary = summarize_project_resources(inventory)
        assert summary["sqs_queues"] >= 1, f"scanner missed the project queue: {summary}"
        assert summary["dynamodb_tables"] >= 1, f"scanner missed the project table: {summary}"
        assert summary["cloudformation_stacks"] >= 1, f"scanner missed the stack: {summary}"
        assert summary["cluster_volumes"] >= 1, (
            f"scanner missed the CSI-provisioned volume: {summary}. Without this the "
            "final-inventory gate would call a teardown clean while billable volumes survive"
        )
        assert volume_id in inventory["regional"][region]["cluster_volumes"]
        assert project_resources_are_absent(inventory) is False
        # The AWS PCS and SageMaker HyperPod scanners ran in this Region (PCS
        # placed by the public parameters, HyperPod by botocore), each listing
        # its service for real through the documented ListClusters shims and
        # finding nothing of the project's: the categories exist and are empty.
        scanner_regions = inventory["coverage"]["scanner_regions"]
        assert scanner_regions["pcs"] == [region], scanner_regions
        assert scanner_regions["hyperpod"] == [region], scanner_regions
        for category in (
            "pcs_clusters",
            "pcs_compute_node_groups",
            "pcs_queues",
            "hyperpod_clusters",
        ):
            assert inventory["regional"][region][category] == [], category

        # Tear the project down and require the scanners to PROVE absence —
        # the exact final-inventory gate that authorizes calling a validation
        # run clean.
        sqs.delete_queue(QueueUrl=queue_url)
        dynamodb.delete_table(TableName=f"{project}-jobs")
        dynamodb.get_waiter("table_not_exists").wait(TableName=f"{project}-jobs")
        _delete_stack(region, f"{project}-{region}")

        # Deleting everything else is not enough: while the volume survives the
        # inventory must still refuse to call the run clean.
        still_leaking = collect_project_resources(
            harness_session,
            enabled_regions=[region],
            expected_account=floci_account,
            project_name=project,
            seed_region=region,
        )
        assert project_resources_are_absent(still_leaking) is False, (
            "a stranded CSI volume must fail the all-zero gate even once every "
            "stack- and project-tagged resource is gone"
        )
        ec2.delete_volume(VolumeId=volume_id)

        after = collect_project_resources(
            harness_session,
            enabled_regions=[region],
            expected_account=floci_account,
            project_name=project,
            seed_region=region,
        )
        assert project_resources_are_absent(after) is True, (
            "with every project resource deleted, the fail-closed inventory must report "
            f"a complete all-zero scan; got {summarize_project_resources(after)} with "
            f"coverage={after.get('coverage')}"
        )


class TestClusterVolumeResidualScoping:
    """The cluster tag is shared with node root volumes, which are not leaks."""

    def test_a_volume_already_being_deleted_is_not_residual(self, harness_session, floci_account):
        from scripts.live_release_validation.inventory.project import (
            collect_project_resources,
            project_resources_are_absent,
        )

        project = unique_name("gcovolstate").replace("-", "")[:14]
        region = "us-east-1"
        ec2 = boto3.client("ec2", region_name=region)
        zone = ec2.describe_availability_zones()["AvailabilityZones"][0]["ZoneName"]
        volume_id = ec2.create_volume(
            AvailabilityZone=zone,
            Size=1,
            VolumeType="gp3",
            TagSpecifications=[
                {
                    "ResourceType": "volume",
                    "Tags": [
                        {"Key": f"kubernetes.io/cluster/{project}-{region}", "Value": "owned"}
                    ],
                }
            ],
        )["VolumeId"]
        ec2.get_waiter("volume_available").wait(VolumeIds=[volume_id])

        def inventory():
            return collect_project_resources(
                harness_session,
                enabled_regions=[region],
                expected_account=floci_account,
                project_name=project,
                seed_region=region,
            )

        # While it exists it is residual and must fail the gate.
        assert project_resources_are_absent(inventory()) is False

        # Once EC2 is removing it, it must not. An EKS Auto Mode node root
        # volume carries the same tag and passes through this state on every
        # teardown; counting it would fail the gate for a volume already gone.
        ec2.delete_volume(VolumeId=volume_id)
        ec2.get_waiter("volume_deleted").wait(VolumeIds=[volume_id])
        assert project_resources_are_absent(inventory()) is True, (
            "a volume EC2 has finished removing must not count against the all-zero teardown gate"
        )


class TestProtectedBaseline:
    def test_baseline_is_stable_and_detects_protected_stack_loss(self, harness_session):
        from scripts.live_release_validation.inventory.project import (
            capture_baseline,
            compare_baseline,
        )

        region = "us-east-1"
        protected_name = unique_name("gcoprotected").replace("-", "")[:20]
        _deploy_marker_stack(region, protected_name)
        try:
            baseline = capture_baseline(
                harness_session,
                enabled_regions=[region],
                ecr_regions=[],
                protected_stack_names=[protected_name],
            )
            assert baseline["protected_stacks"].get(region), (
                "the protected stack must be fingerprinted into the baseline"
            )

            unchanged = capture_baseline(
                harness_session,
                enabled_regions=[region],
                ecr_regions=[],
                protected_stack_names=[protected_name],
            )
            assert compare_baseline(baseline, unchanged) == [], (
                "an untouched account must produce a difference-free baseline comparison"
            )
        finally:
            _delete_stack(region, protected_name)

        after_loss = capture_baseline(
            harness_session,
            enabled_regions=[region],
            ecr_regions=[],
            protected_stack_names=[protected_name],
        )
        differences = compare_baseline(baseline, after_loss)
        assert differences, (
            "losing a protected stack MUST surface as a baseline difference — this is "
            "the guarantee that a validation run cannot silently damage pre-existing "
            "infrastructure"
        )
        assert differences[0]["category"] == "protected_stacks"
