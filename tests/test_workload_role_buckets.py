"""S3 and KMS reach of the shared workload role (``gco-service-account``).

The role used to read every bucket whose name starts with the project name
(``arn:<partition>:s3:::<project>-*``) and to decrypt with any key in the
account through S3 (``kms:*:<account>:key/*``), which covered the analytics
Studio bucket, the cost reports and every access-log bucket. These tests pin
the scoped shape: the model weights bucket (read, its name resolved from SSM),
the cluster-shared bucket and this region's regional-shared bucket (read and
write), nothing by prefix, and every key grant tied to one of those buckets.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from tests import test_regional_stack as regional_fixtures


@pytest.fixture(scope="module")
def resources() -> dict:
    from gco.stacks.regional_stack import GCORegionalStack

    app = cdk.App()
    with (
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(
            GCORegionalStack,
            "_create_helm_installer_lambda",
            regional_fixtures.TestRegionalStackSynthesis._mock_helm_installer,
        ),
    ):
        mock_docker.return_value = MagicMock(
            image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
        )
        stack = GCORegionalStack(
            app,
            "test-workload-role-buckets",
            config=regional_fixtures.MockConfigLoader(app),
            region="us-east-1",
            auth_secret_arn=(
                "arn:aws:secretsmanager:us-east-2:123456789012:secret:test-secret"  # nosec B106  # test fixture ARN
            ),
            env=cdk.Environment(account="123456789012", region="us-east-1"),
        )
        return assertions.Template.from_stack(stack).to_json()["Resources"]


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else [value]


def _logical_id(resources: dict, prefix: str, resource_type: str) -> str:
    [logical_id] = [
        lid for lid, r in resources.items() if lid.startswith(prefix) and r["Type"] == resource_type
    ]
    return logical_id


def _workload_statements(resources: dict) -> list[dict]:
    role_id = _logical_id(resources, "ServiceAccountRole", "AWS::IAM::Role")
    return [
        statement
        for resource in resources.values()
        if resource["Type"] == "AWS::IAM::Policy"
        and {"Ref": role_id} in resource["Properties"]["Roles"]
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]
    ]


def _bucket_source(resource: object) -> str:
    """Which construct an S3 resource ARN is built from (``<id>`` or the literal)."""
    rendered = json.dumps(resource)
    for marker in ("ReadModelBucketName", "ReadClusterSharedBucketArn", "RegionalSharedBucket"):
        if marker in rendered:
            return marker
    return rendered


def _model_bucket_arn(resources: dict, suffix: str = "") -> dict:
    reader = _logical_id(resources, "ReadModelBucketName", "Custom::AWS")
    parts: list = [
        "arn:",
        {"Ref": "AWS::Partition"},
        ":s3:::",
        {"Fn::GetAtt": [reader, "Parameter.Value"]},
    ]
    if suffix:
        parts.append(suffix)
    return {"Fn::Join": ["", parts]}


def test_no_bucket_or_key_is_granted_by_prefix(resources):
    for statement in _workload_statements(resources):
        rendered = json.dumps(statement["Resource"])
        assert "s3:::gco-test-" not in rendered, statement
        assert ":key/" not in rendered, statement


def test_the_role_reaches_exactly_the_three_documented_buckets(resources):
    s3_statements = [
        s
        for s in _workload_statements(resources)
        if any(a.startswith("s3:") for a in _as_list(s["Action"]))
    ]
    reached = {
        _bucket_source(resource) for s in s3_statements for resource in _as_list(s["Resource"])
    }
    assert reached == {"ReadModelBucketName", "ReadClusterSharedBucketArn", "RegionalSharedBucket"}


def test_model_bucket_is_read_only_and_resolved_from_ssm(resources):
    statements = _workload_statements(resources)
    model = [s for s in statements if "ReadModelBucketName" in json.dumps(s)]
    by_actions = {frozenset(_as_list(s["Action"])): s for s in model}
    assert set(by_actions) == {
        frozenset({"s3:GetObject", "s3:ListBucket"}),
        frozenset({"kms:Decrypt"}),
    }
    assert by_actions[frozenset({"s3:GetObject", "s3:ListBucket"})]["Resource"] == [
        _model_bucket_arn(resources),
        _model_bucket_arn(resources, "/*"),
    ]
    kms = by_actions[frozenset({"kms:Decrypt"})]
    assert kms["Resource"] == "*"
    assert kms["Condition"] == {
        "StringEquals": {
            "kms:ViaService": {"Fn::Join": ["", ["s3.us-east-2.", {"Ref": "AWS::URLSuffix"}]]}
        },
        "StringLike": {
            "kms:EncryptionContext:aws:s3:arn": [
                _model_bucket_arn(resources),
                _model_bucket_arn(resources, "/*"),
            ]
        },
    }

    reader = resources[_logical_id(resources, "ReadModelBucketName", "Custom::AWS")]
    shared_role = _logical_id(resources, "AwsCustomResourceRole", "AWS::IAM::Role")
    for key in ("Create", "Update"):
        call = json.loads(reader["Properties"][key])
        assert call["service"] == "SSM"
        assert call["action"] == "getParameter"
        assert call["parameters"] == {"Name": "/gco-test/model-bucket-name"}
        assert call["region"] == "us-east-2"
    assert shared_role in reader["DependsOn"]


def test_cluster_shared_key_grant_is_tied_to_that_bucket(resources):
    [kms] = [
        s
        for s in _workload_statements(resources)
        if set(_as_list(s["Action"])) == {"kms:Decrypt", "kms:GenerateDataKey"}
    ]
    arn_reader = _logical_id(resources, "ReadClusterSharedBucketArn", "Custom::AWS")
    shared_arn = {"Fn::GetAtt": [arn_reader, "Parameter.Value"]}
    assert kms["Resource"] == "*"
    assert kms["Condition"]["StringLike"] == {
        "kms:EncryptionContext:aws:s3:arn": [
            shared_arn,
            {"Fn::Join": ["", [shared_arn, "/*"]]},
        ]
    }
    assert "ReadClusterSharedBucketRegion" in json.dumps(kms["Condition"]["StringEquals"])
