"""Floci layer: GCOAWSClient stack discovery against real CloudFormation.

``cli/aws_client.py`` is how every CLI command and the live-validation
harness find deployed GCO infrastructure: it reads CloudFormation stacks by
their derived names and consumes their Outputs. The moto unit tests seed
those responses in-process; here the stacks are real emulator state created
through ``create_stack`` — CloudFormation materializes them, resolves
``Ref``s, and serves Outputs over the wire — so the discovery path (client
construction per region, describe calls, output parsing, the missing-stack
``ClientError`` path, and cache behavior) runs exactly as it does against
AWS.
"""

from __future__ import annotations

import json

import boto3
import pytest

from tests._floci import floci_test_markers, unique_name

pytestmark = floci_test_markers()


def _deploy_stack(cloudformation, stack_name: str, template: dict) -> None:
    cloudformation.create_stack(StackName=stack_name, TemplateBody=json.dumps(template))
    cloudformation.get_waiter("stack_create_complete").wait(StackName=stack_name)


@pytest.fixture()
def project(verified_floci_endpoint):
    """A uniquely named GCO project with its config object."""
    from cli.config import GCOConfig

    name = unique_name("gcotest").replace("-", "")[:16]
    config = GCOConfig(
        project_name=name,
        default_region="us-east-1",
        api_gateway_region="us-east-2",
        global_region="us-east-2",
        monitoring_region="us-east-2",
        output_format="json",
    )
    yield config
    for region in ("us-east-1", "us-east-2"):
        cloudformation = boto3.client("cloudformation", region_name=region)
        for summary in cloudformation.list_stacks().get("StackSummaries", []):
            if summary["StackName"].startswith(name) and "DELETE" not in summary["StackStatus"]:
                cloudformation.delete_stack(StackName=summary["StackName"])


class TestRegionalStackDiscovery:
    def test_probe_finds_regional_stack_and_reads_outputs(self, project):
        from cli.aws_client import GCOAWSClient

        cloudformation = boto3.client("cloudformation", region_name="us-east-1")
        _deploy_stack(
            cloudformation,
            f"{project.regional_stack_prefix}-us-east-1",
            {
                "AWSTemplateFormatVersion": "2010-09-09",
                "Resources": {
                    "JobsQueue": {
                        "Type": "AWS::SQS::Queue",
                        "Properties": {"QueueName": f"{project.project_name}-jobs"},
                    }
                },
                "Outputs": {
                    "ClusterName": {"Value": f"{project.project_name}-us-east-1"},
                    "EfsFileSystemId": {"Value": "fs-12345678"},
                },
            },
        )

        client = GCOAWSClient(project)
        stacks = client.discover_regional_stacks()
        assert list(stacks) == ["us-east-1"], "configured-region fast path must find the stack"
        regional = stacks["us-east-1"]
        assert regional.cluster_name == f"{project.project_name}-us-east-1"
        assert regional.efs_file_system_id == "fs-12345678"
        assert regional.status == "CREATE_COMPLETE"

    def test_probe_returns_none_when_stack_absent(self, project):
        from cli.aws_client import GCOAWSClient

        client = GCOAWSClient(project)
        assert client.get_regional_stack("us-east-1") is None, (
            "a missing stack must surface as None (ClientError swallowed), not an exception"
        )


class TestApiEndpointDiscovery:
    def test_global_api_endpoint_resolves_from_stack_outputs(self, project):
        from cli.aws_client import GCOAWSClient

        cloudformation = boto3.client("cloudformation", region_name="us-east-2")
        _deploy_stack(
            cloudformation,
            project.api_gateway_stack_name,
            {
                "AWSTemplateFormatVersion": "2010-09-09",
                # Output shape matches the real api-gateway stack; a Waf/API
                # resource set is unnecessary for the discovery contract.
                "Resources": {
                    "Placeholder": {
                        "Type": "AWS::SSM::Parameter",
                        "Properties": {
                            "Name": f"/{project.project_name}/probe",
                            "Type": "String",
                            "Value": "x",
                        },
                    }
                },
                "Outputs": {
                    "ApiEndpoint": {
                        "Value": "https://abc123.execute-api.us-east-2.amazonaws.com/prod/"
                    }
                },
            },
        )

        client = GCOAWSClient(project)
        endpoint = client.get_api_endpoint()
        assert endpoint.url == "https://abc123.execute-api.us-east-2.amazonaws.com/prod"
        assert endpoint.api_id == "abc123"
        assert endpoint.region == "us-east-2"

        # Second call must serve from cache (no new stack read): delete the
        # stack and confirm the cached endpoint still resolves.
        cloudformation.delete_stack(StackName=project.api_gateway_stack_name)
        cloudformation.get_waiter("stack_delete_complete").wait(
            StackName=project.api_gateway_stack_name
        )
        cached = client.get_api_endpoint()
        assert cached.url == endpoint.url, "TTL cache must serve the discovered endpoint"

    def test_regional_api_endpoint_absent_returns_none(self, project):
        from cli.aws_client import GCOAWSClient

        client = GCOAWSClient(project)
        assert client.get_regional_api_endpoint("us-east-1") is None
