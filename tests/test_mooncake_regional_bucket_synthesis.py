"""Synthesis checks for the always-on general-purpose regional bucket.

The regional stack provisions one general-purpose S3 bucket named
``gco-regional-shared-<account>-<region>`` per region, publishes three
discovery parameters under ``/gco/regional-shared-bucket`` (``/name``,
``/arn``, ``/region``), and grants the in-region pod role
(``gco-service-account``) read/write on that bucket plus use of its KMS key —
and nothing else.

These tests synthesize the regional CloudFormation template and assert the
concrete shape of those resources:

* exactly one bucket carrying the ``gco-regional-shared-`` name prefix,
  encrypted with a customer-managed KMS key, with public access fully blocked
  and insecure transport denied;
* exactly the three discovery parameters under the
  ``/gco/regional-shared-bucket`` namespace;
* the pod role's S3 grant points at only that one bucket (its ARN and its
  object-key space) and its KMS grant points at only that bucket's key — no
  other bucket or key is reachable through the regional grant.

The Docker + helm-installer patching pattern is borrowed from
``tests/test_regional_stack.py`` so the synth needs no Docker daemon, and the
synthesized template is cached so every assertion reuses one synth.
"""

from __future__ import annotations

from functools import cache
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
from aws_cdk import assertions

from gco.stacks.constants import (
    regional_shared_bucket_name_prefix,
    regional_shared_ssm_parameter_prefix,
)
from gco.stacks.regional_stack import GCORegionalStack

# Reuse the MockConfigLoader + helm-installer patch helpers from the regional
# stack tests rather than re-implementing a synth fixture.
from tests.test_regional_stack import MockConfigLoader
from tests.test_regional_stack import TestRegionalStackSynthesis as _RegionalStackSynthesisFixtures

# Physical-name prefixes the regional stack derives from ``project_name`` (#139).
# MockConfigLoader.get_project_name() returns "gco-test", so scope to that name.
_PROJECT_NAME = "gco-test"
REGIONAL_SHARED_BUCKET_NAME_PREFIX = regional_shared_bucket_name_prefix(_PROJECT_NAME)
REGIONAL_SHARED_SSM_PARAMETER_PREFIX = regional_shared_ssm_parameter_prefix(_PROJECT_NAME)

_ACCOUNT = "123456789012"
_REGION = "us-east-1"

# The S3 object/bucket-level actions the pod role is granted on the regional
# bucket — read, write, delete, and the list/location actions needed to use it.
_EXPECTED_S3_ACTIONS = {
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:ListBucket",
    "s3:GetBucketLocation",
}

# The KMS actions the pod role is granted on the regional bucket's key.
_EXPECTED_KMS_ACTIONS = {
    "kms:Decrypt",
    "kms:Encrypt",
    "kms:GenerateDataKey",
    "kms:DescribeKey",
}


@cache
def _regional_template_json() -> dict[str, Any]:
    """Synthesize the regional stack once and return its template JSON.

    Mirrors the Docker + helm-installer patching from
    ``tests/test_regional_stack.py`` so no real Docker daemon is needed.
    """
    app = cdk.App()
    config = MockConfigLoader(app)

    with (
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(
            GCORegionalStack,
            "_create_helm_installer_lambda",
            _RegionalStackSynthesisFixtures._mock_helm_installer,
        ),
    ):
        mock_image = MagicMock()
        mock_image.image_uri = f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/test:latest"
        mock_docker.return_value = mock_image

        stack = GCORegionalStack(
            app,
            "test-regional-shared-synthesis",
            config=config,
            region=_REGION,
            auth_secret_arn=f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:test-secret",  # nosec B106
            env=cdk.Environment(account=_ACCOUNT, region=_REGION),
        )
        return assertions.Template.from_stack(stack).to_json()


def _expected_bucket_name() -> str:
    return f"{REGIONAL_SHARED_BUCKET_NAME_PREFIX}-{_ACCOUNT}-{_REGION}"


def _regional_bucket_logical_id(template: dict[str, Any]) -> str:
    """Logical ID of the single general-purpose regional bucket."""
    resources = template.get("Resources", {})
    matches = [
        logical_id
        for logical_id, res in resources.items()
        if res.get("Type") == "AWS::S3::Bucket"
        and res.get("Properties", {}).get("BucketName") == _expected_bucket_name()
    ]
    assert len(matches) == 1, (
        f"expected exactly one bucket named {_expected_bucket_name()!r}, "
        f"found logical IDs: {matches}"
    )
    return matches[0]


def _regional_key_logical_id(template: dict[str, Any], bucket_logical_id: str) -> str:
    """Logical ID of the KMS key that encrypts the regional bucket.

    Read off the bucket's own encryption configuration so the test does not
    depend on the construct's hashed logical-ID suffix.
    """
    bucket = template["Resources"][bucket_logical_id]
    encryption = bucket["Properties"]["BucketEncryption"]
    by_default = encryption["ServerSideEncryptionConfiguration"][0]["ServerSideEncryptionByDefault"]
    key_ref = by_default["KMSMasterKeyID"]
    assert isinstance(key_ref, dict) and "Fn::GetAtt" in key_ref, (
        f"regional bucket must be encrypted with a customer-managed key reference, got {key_ref!r}"
    )
    return key_ref["Fn::GetAtt"][0]


def _references_logical_id(obj: Any, logical_id: str) -> bool:
    """True iff ``obj`` contains an ``Fn::GetAtt`` to ``logical_id`` anywhere."""
    if isinstance(obj, dict):
        getatt = obj.get("Fn::GetAtt")
        if isinstance(getatt, list) and getatt and getatt[0] == logical_id:
            return True
        return any(_references_logical_id(v, logical_id) for v in obj.values())
    if isinstance(obj, list):
        return any(_references_logical_id(item, logical_id) for item in obj)
    return False


def _pod_role_statements(template: dict[str, Any]) -> list[dict[str, Any]]:
    """Every policy statement attached to the ``gco-service-account`` pod role.

    Finds the role by its construct ID prefix (``ServiceAccountRole``), then
    gathers the statements from every ``AWS::IAM::Policy`` bound to that role.
    """
    resources = template.get("Resources", {})
    role_ids = {
        logical_id
        for logical_id, res in resources.items()
        if res.get("Type") == "AWS::IAM::Role" and logical_id.startswith("ServiceAccountRole")
    }
    assert role_ids, "could not locate the gco-service-account pod role in the template"

    statements: list[dict[str, Any]] = []
    for res in resources.values():
        if res.get("Type") != "AWS::IAM::Policy":
            continue
        roles = res.get("Properties", {}).get("Roles", [])
        bound = any(isinstance(ref, dict) and ref.get("Ref") in role_ids for ref in roles)
        if not bound:
            continue
        doc = res.get("Properties", {}).get("PolicyDocument", {})
        statements.extend(doc.get("Statement", []))
    return statements


def _as_action_set(action: Any) -> set[str]:
    if isinstance(action, str):
        return {action}
    return set(action)


def _as_resource_list(resource: Any) -> list[Any]:
    if isinstance(resource, list):
        return resource
    return [resource]


class TestRegionalSharedBucketSynthesis:
    """The regional stack synthesizes exactly one hardened regional bucket,
    its three discovery parameters, and a pod-role grant scoped to only that
    bucket and its key.
    """

    def test_exactly_one_general_purpose_regional_bucket(self) -> None:
        """One and only one bucket carries the regional-shared name prefix."""
        template = _regional_template_json()
        resources = template.get("Resources", {})

        named_regional = [
            res["Properties"]["BucketName"]
            for res in resources.values()
            if res.get("Type") == "AWS::S3::Bucket"
            and isinstance(res.get("Properties", {}).get("BucketName"), str)
            and res["Properties"]["BucketName"].startswith(f"{REGIONAL_SHARED_BUCKET_NAME_PREFIX}-")
        ]

        assert named_regional == [_expected_bucket_name()], (
            f"expected exactly one bucket named {_expected_bucket_name()!r}, found {named_regional}"
        )

    def test_regional_bucket_uses_kms_encryption(self) -> None:
        """The regional bucket is encrypted server-side with a KMS key."""
        template = _regional_template_json()
        bucket_id = _regional_bucket_logical_id(template)
        encryption = template["Resources"][bucket_id]["Properties"]["BucketEncryption"]
        by_default = encryption["ServerSideEncryptionConfiguration"][0][
            "ServerSideEncryptionByDefault"
        ]

        assert by_default["SSEAlgorithm"] == "aws:kms"
        # The key is a concrete customer-managed key in this stack, referenced
        # by ARN rather than the AWS-managed alias.
        assert isinstance(by_default.get("KMSMasterKeyID"), dict)

    def test_regional_bucket_blocks_public_access(self) -> None:
        """Public access is fully blocked on the regional bucket."""
        template = _regional_template_json()
        bucket_id = _regional_bucket_logical_id(template)
        bpa = template["Resources"][bucket_id]["Properties"]["PublicAccessBlockConfiguration"]

        assert bpa == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }

    def test_regional_bucket_denies_insecure_transport(self) -> None:
        """A bucket policy denies any request that is not over TLS."""
        template = _regional_template_json()
        bucket_id = _regional_bucket_logical_id(template)
        resources = template.get("Resources", {})

        deny_statements: list[dict[str, Any]] = []
        for res in resources.values():
            if res.get("Type") != "AWS::S3::BucketPolicy":
                continue
            bucket_ref = res.get("Properties", {}).get("Bucket")
            if not (isinstance(bucket_ref, dict) and bucket_ref.get("Ref") == bucket_id):
                continue
            for stmt in res["Properties"]["PolicyDocument"].get("Statement", []):
                condition = stmt.get("Condition", {})
                secure = condition.get("Bool", {}).get("aws:SecureTransport")
                if stmt.get("Effect") == "Deny" and secure in ("false", False):
                    deny_statements.append(stmt)

        assert deny_statements, (
            "regional bucket policy must deny requests with "
            "aws:SecureTransport=false (TLS enforcement)"
        )

    def test_three_discovery_parameters_published(self) -> None:
        """Exactly name/arn/region are published under the discovery prefix."""
        template = _regional_template_json()
        resources = template.get("Resources", {})

        param_names = [
            res["Properties"]["Name"]
            for res in resources.values()
            if res.get("Type") == "AWS::SSM::Parameter"
            and isinstance(res.get("Properties", {}).get("Name"), str)
            and res["Properties"]["Name"].startswith(REGIONAL_SHARED_SSM_PARAMETER_PREFIX)
        ]

        assert set(param_names) == {
            f"{REGIONAL_SHARED_SSM_PARAMETER_PREFIX}/name",
            f"{REGIONAL_SHARED_SSM_PARAMETER_PREFIX}/arn",
            f"{REGIONAL_SHARED_SSM_PARAMETER_PREFIX}/region",
        }, f"unexpected discovery parameter set: {sorted(param_names)}"

    def test_pod_role_s3_grant_targets_only_the_regional_bucket(self) -> None:
        """The pod role's regional S3 grant reaches only this one bucket."""
        template = _regional_template_json()
        bucket_id = _regional_bucket_logical_id(template)
        statements = _pod_role_statements(template)

        s3_grants = [
            stmt
            for stmt in statements
            if stmt.get("Effect") == "Allow"
            and "s3:PutObject" in _as_action_set(stmt.get("Action", []))
            and _references_logical_id(stmt.get("Resource"), bucket_id)
        ]

        assert len(s3_grants) == 1, (
            f"expected exactly one regional-bucket S3 grant, found {len(s3_grants)}"
        )
        grant = s3_grants[0]
        assert _as_action_set(grant["Action"]) == _EXPECTED_S3_ACTIONS

        # The grant covers the bucket itself and its object-key space, and
        # every resource entry resolves back to this one bucket — no other
        # bucket and no bare "*" wildcard appears.
        resource_entries = _as_resource_list(grant["Resource"])
        assert len(resource_entries) == 2
        for entry in resource_entries:
            assert entry != "*"
            assert _references_logical_id(entry, bucket_id), (
                f"regional S3 grant resource {entry!r} must reference only the regional bucket"
            )

    def test_pod_role_kms_grant_targets_only_the_regional_key(self) -> None:
        """The pod role's regional KMS grant reaches only this bucket's key."""
        template = _regional_template_json()
        bucket_id = _regional_bucket_logical_id(template)
        key_id = _regional_key_logical_id(template, bucket_id)
        statements = _pod_role_statements(template)

        kms_grants = [
            stmt
            for stmt in statements
            if stmt.get("Effect") == "Allow"
            and "kms:Encrypt" in _as_action_set(stmt.get("Action", []))
            and _references_logical_id(stmt.get("Resource"), key_id)
        ]

        assert len(kms_grants) == 1, (
            f"expected exactly one regional-key KMS grant, found {len(kms_grants)}"
        )
        grant = kms_grants[0]
        assert _as_action_set(grant["Action"]) == _EXPECTED_KMS_ACTIONS

        resource_entries = _as_resource_list(grant["Resource"])
        assert len(resource_entries) == 1
        assert resource_entries[0] != "*"
        assert _references_logical_id(resource_entries[0], key_id), (
            "regional KMS grant must reference only the regional bucket's key"
        )
