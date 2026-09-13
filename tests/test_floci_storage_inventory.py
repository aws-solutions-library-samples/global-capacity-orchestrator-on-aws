"""Floci layer: ``gco storage`` inventory and alias resolution over real stacks.

``cli/storage.py`` answers "which buckets does this deployment have, and what
are their names?" from two sources the unit suite fabricates with MagicMocks:
the SSM parameters each stack publishes for its primary bucket, and the
``AWS::S3::Bucket`` resources of the owning stacks (``list_stack_resources``,
hand-paginated) for the CDK-named access-log sinks and the Studio bucket.
Since ADR-0005 *every* bucket is CloudFormation-named, so a wrong assumption
about either source shows up as an inventory that lies.

Here the four owning stacks (global, regional, monitoring, analytics) are real
CloudFormation stacks in the emulator, declared with the logical-id prefixes
``BUCKET_DESCRIPTORS`` keys on and the SSM parameters the CDK stacks publish.
The manager runs unmodified: STS for the account, SSM in each bucket's home
Region, CloudFormation for the stack sweep including the real
``ValidationError: Stack ... does not exist`` shape for an undeployed stack.
"""

from __future__ import annotations

import json

import boto3
import pytest

from tests._floci import floci_test_markers, unique_name

pytestmark = floci_test_markers()

GLOBAL_REGION = "us-east-2"
REGIONAL_REGION = "us-east-1"


def _bucket(logical_id: str) -> dict:
    return {logical_id: {"Type": "AWS::S3::Bucket", "Properties": {}}}


def _parameter(logical_id: str, name: str, value: object) -> dict:
    return {
        logical_id: {
            "Type": "AWS::SSM::Parameter",
            "Properties": {"Name": name, "Type": "String", "Value": value},
        }
    }


def _identity_parameters(prefix: str, bucket_logical_id: str) -> dict:
    """The ``{name,arn,region}`` triple the shared and cost buckets publish."""
    return {
        **_parameter(f"{bucket_logical_id}NameParam", f"{prefix}/name", {"Ref": bucket_logical_id}),
        **_parameter(
            f"{bucket_logical_id}ArnParam",
            f"{prefix}/arn",
            {"Fn::GetAtt": [bucket_logical_id, "Arn"]},
        ),
        **_parameter(f"{bucket_logical_id}RegionParam", f"{prefix}/region", {"Ref": "AWS::Region"}),
    }


def _global_template(project: str) -> dict:
    from gco.stacks.constants import cluster_shared_ssm_parameter_prefix

    return {
        **_bucket("ClusterSharedBucket1A2B3C4D"),
        **_bucket("ClusterSharedAccessLogsBucket5E6F7A8B"),
        **_bucket("ModelWeightsBucket9C0D1E2F"),
        **_bucket("ModelWeightsAccessLogsBucket3A4B5C6D"),
        **_identity_parameters(
            cluster_shared_ssm_parameter_prefix(project), "ClusterSharedBucket1A2B3C4D"
        ),
        **_parameter(
            "ModelBucketNameParam",
            f"/{project}/model-bucket-name",
            {"Ref": "ModelWeightsBucket9C0D1E2F"},
        ),
    }


def _regional_template(project: str) -> dict:
    from gco.stacks.constants import regional_shared_ssm_parameter_prefix

    return {
        **_bucket("RegionalSharedBucket7E8F9A0B"),
        **_bucket("RegionalSharedAccessLogsBucket1C2D3E4F"),
        **_identity_parameters(
            regional_shared_ssm_parameter_prefix(project), "RegionalSharedBucket7E8F9A0B"
        ),
    }


def _monitoring_template(project: str) -> dict:
    from gco.stacks.constants import cost_report_ssm_parameter_prefix

    return {
        **_bucket("CostReportBucket5A6B7C8D"),
        **_bucket("CostReportAccessLogsBucket9E0F1A2B"),
        **_identity_parameters(
            cost_report_ssm_parameter_prefix(project), "CostReportBucket5A6B7C8D"
        ),
    }


def _analytics_template() -> dict:
    return {
        **_bucket("StudioOnlyBucket3C4D5E6F"),
        **_bucket("AnalyticsAccessLogsBucket7A8B9C0D"),
    }


def _deploy(region: str, stack_name: str, resources: dict) -> dict[str, str]:
    """Create a stack and return ``logical id -> physical name`` for its buckets."""
    cloudformation = boto3.client("cloudformation", region_name=region)
    cloudformation.create_stack(
        StackName=stack_name,
        TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": resources}),
    )
    cloudformation.get_waiter("stack_create_complete").wait(
        StackName=stack_name, WaiterConfig={"Delay": 1, "MaxAttempts": 120}
    )
    return {
        resource["LogicalResourceId"]: resource["PhysicalResourceId"]
        for resource in cloudformation.list_stack_resources(StackName=stack_name)[
            "StackResourceSummaries"
        ]
        if resource["ResourceType"] == "AWS::S3::Bucket"
    }


def _destroy(region: str, stack_name: str) -> None:
    cloudformation = boto3.client("cloudformation", region_name=region)
    cloudformation.delete_stack(StackName=stack_name)
    cloudformation.get_waiter("stack_delete_complete").wait(
        StackName=stack_name, WaiterConfig={"Delay": 1, "MaxAttempts": 120}
    )


@pytest.fixture(scope="module")
def deployment(verified_floci_endpoint):
    """One project's four stacks, each with CloudFormation-named buckets."""
    project = unique_name("gcotest").replace("-", "")[:16]
    stacks = {
        "global": (GLOBAL_REGION, f"{project}-global", _global_template(project)),
        "regional": (REGIONAL_REGION, f"{project}-{REGIONAL_REGION}", _regional_template(project)),
        "monitoring": (GLOBAL_REGION, f"{project}-monitoring", _monitoring_template(project)),
        "analytics": (GLOBAL_REGION, f"{project}-analytics", _analytics_template()),
    }
    physical: dict[str, dict[str, str]] = {}
    for scope, (region, stack_name, resources) in stacks.items():
        physical[scope] = _deploy(region, stack_name, resources)
    yield {"project": project, "buckets": physical}
    for region, stack_name, _ in stacks.values():
        _destroy(region, stack_name)


@pytest.fixture(scope="module")
def manager(deployment):
    from cli.config import GCOConfig
    from cli.storage import StorageManager

    return StorageManager(
        GCOConfig(
            project_name=deployment["project"],
            default_region=REGIONAL_REGION,
            api_gateway_region=GLOBAL_REGION,
            global_region=GLOBAL_REGION,
            monitoring_region=GLOBAL_REGION,
            output_format="json",
        )
    )


def _physical(deployment, scope: str, logical_prefix: str) -> str:
    matches = [
        name
        for logical_id, name in deployment["buckets"][scope].items()
        if logical_id.startswith(logical_prefix)
    ]
    assert len(matches) == 1, (scope, logical_prefix, deployment["buckets"][scope])
    return matches[0]


class TestInventoryOverRealStacks:
    def test_every_bucket_is_reported_deployed_under_its_cloudformation_name(
        self, deployment, manager
    ):
        inventory = manager.s3_inventory(region=REGIONAL_REGION)

        by_id = {record["id"]: record for record in inventory["buckets"]}
        expected = {
            "cluster-shared": ("global", "ClusterSharedBucket"),
            "cluster-shared-access-logs": ("global", "ClusterSharedAccessLogsBucket"),
            "model-weights": ("global", "ModelWeightsBucket"),
            "model-weights-access-logs": ("global", "ModelWeightsAccessLogsBucket"),
            f"regional-shared:{REGIONAL_REGION}": ("regional", "RegionalSharedBucket"),
            f"regional-shared-access-logs:{REGIONAL_REGION}": (
                "regional",
                "RegionalSharedAccessLogsBucket",
            ),
            "cost-reports": ("monitoring", "CostReportBucket"),
            "cost-reports-access-logs": ("monitoring", "CostReportAccessLogsBucket"),
            "analytics-studio": ("analytics", "StudioOnlyBucket"),
            "analytics-studio-access-logs": ("analytics", "AnalyticsAccessLogsBucket"),
        }
        assert set(by_id) == set(expected)
        for record_id, (scope, prefix) in expected.items():
            record = by_id[record_id]
            assert record["status"] == "deployed", record
            assert record["bucket"] == _physical(deployment, scope, prefix), record
            assert record["arn"] == f"arn:aws:s3:::{record['bucket']}", record
            assert record["s3_uri"] == f"s3://{record['bucket']}/", record
            assert record["detail"] == ""

        assert inventory["summary"] == {
            "total": 10,
            "deployed": 10,
            "not_deployed": 0,
            "pod_writable": sorted(
                [
                    _physical(deployment, "global", "ClusterSharedBucket"),
                    _physical(deployment, "regional", "RegionalSharedBucket"),
                ]
            ),
        }
        assert inventory["account"] == boto3.client("sts").get_caller_identity()["Account"]
        assert inventory["project_name"] == deployment["project"]

    def test_an_undeployed_region_is_reported_not_deployed_not_omitted(self, deployment, manager):
        """A Region without a stack answers with the real "does not exist" error.

        The sweep must translate CloudFormation's ``ValidationError`` for an
        absent stack into ``not-deployed`` entries and keep going; only that
        exact shape may be swallowed, so this pins it over the wire.
        """
        inventory = manager.s3_inventory(region="eu-west-1")

        regional = [record for record in inventory["buckets"] if record["scope"] == "regional"]
        assert [record["id"] for record in regional] == [
            "regional-shared:eu-west-1",
            "regional-shared-access-logs:eu-west-1",
        ]
        for record in regional:
            assert record["status"] == "not-deployed", record
            assert record["bucket"] is None
            assert record["detail"] == f"{deployment['project']}-eu-west-1 is not deployed"
        assert inventory["summary"]["deployed"] == 8
        assert inventory["summary"]["not_deployed"] == 2


class TestAliasResolutionOverRealStacks:
    def test_each_alias_resolves_to_its_bucket_and_home_region(self, deployment, manager):
        expected = {
            "cluster-shared": (
                _physical(deployment, "global", "ClusterSharedBucket"),
                GLOBAL_REGION,
            ),
            "model-weights": (
                _physical(deployment, "global", "ModelWeightsBucket"),
                GLOBAL_REGION,
            ),
            f"regional-shared:{REGIONAL_REGION}": (
                _physical(deployment, "regional", "RegionalSharedBucket"),
                REGIONAL_REGION,
            ),
            "analytics-studio": (
                _physical(deployment, "analytics", "StudioOnlyBucket"),
                GLOBAL_REGION,
            ),
        }
        for alias, (bucket, region) in expected.items():
            record = manager.resolve_bucket(alias)
            assert record["bucket"] == bucket, alias
            assert record["region"] == region, alias
            assert record["s3_uri"] == f"s3://{bucket}/", alias

        assert {record["alias"] for record in manager.list_buckets(region=REGIONAL_REGION)} == set(
            expected
        )

    def test_missing_stacks_raise_the_not_found_error_not_a_client_error(self, deployment, manager):
        from cli.storage import StorageBucketNotFoundError

        with pytest.raises(StorageBucketNotFoundError, match="eu-west-1"):
            manager.resolve_bucket("regional-shared:eu-west-1")

        from cli.config import GCOConfig
        from cli.storage import StorageManager

        undeployed = StorageManager(
            GCOConfig(
                project_name=unique_name("gcoabsent").replace("-", "")[:16],
                default_region=REGIONAL_REGION,
                api_gateway_region=GLOBAL_REGION,
                global_region=GLOBAL_REGION,
                monitoring_region=GLOBAL_REGION,
                output_format="json",
            )
        )
        for alias in ("cluster-shared", "model-weights", "analytics-studio"):
            with pytest.raises(StorageBucketNotFoundError):
                undeployed.resolve_bucket(alias)
        assert undeployed.list_buckets(region=REGIONAL_REGION) == []
