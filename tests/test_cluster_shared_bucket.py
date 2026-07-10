"""
Tests for the always-on ``Cluster_Shared_Bucket`` owned by ``GCOGlobalStack``.

Synthesizes ``GCOGlobalStack`` against a minimal ``MockConfigLoader`` and
asserts that the ``Cluster_Shared_Bucket`` invariants hold in the
synthesized CloudFormation template:

- exactly one bucket is named with the ``gco-test-cluster-shared-`` prefix,
- the primary bucket is KMS-encrypted with the new ``Cluster_Shared_KMS_Key``,
- full public-access block and TLS-only posture,
- ``VersioningConfiguration.Status=Enabled``,
- destroy-on-teardown for the primary bucket, the access-logs bucket, and
  the KMS key,
- three SSM parameters at ``/gco-test/cluster-shared-bucket/{name,arn,region}``,
- four ``CfnOutput``s with the documented export names,
- explicit ``aws:SecureTransport=false`` Deny on the primary bucket policy
  (belt-and-suspenders with ``enforce_ssl=True``),
- KMS key has ``EnableKeyRotation=true`` and ``PendingWindowInDays=7``,
- the cluster-shared access-logs bucket exists separately from the
  ``model_bucket_access_logs`` bucket and is KMS-encrypted with the same
  ``Cluster_Shared_KMS_Key``.

No Docker or AWS calls — pure template assertions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import aws_cdk as cdk
from aws_cdk import assertions

from gco.config.config_loader import ConfigLoader
from gco.stacks.global_stack import GCOGlobalStack


# Re-use the mock ConfigLoader pattern from tests/test_model_bucket_access_logs.py
# so this test file stays self-contained and doesn't require a real cdk.json.
class MockConfigLoader:
    """Minimal ConfigLoader stub sufficient for GCOGlobalStack synthesis."""

    def __init__(self, app: cdk.App | None = None) -> None:
        pass

    def get_project_name(self) -> str:
        return "gco-test"

    def get_regions(self) -> list[str]:
        return ["us-east-1"]

    def get_global_region(self) -> str:
        return "us-east-2"

    def get_tags(self) -> dict[str, str]:
        return {"Environment": "test", "Project": "gco"}

    def get_global_accelerator_config(self) -> dict[str, Any]:
        return {
            "name": "gco-test-accelerator",
            "health_check_grace_period": 30,
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "health_check_path": "/api/v1/health",
        }

    def get_capacity_history_enabled(self):
        return False

    def get_capacity_history_config(self):
        return {
            "enabled": False,
            "retention_days": 90,
            "poll_interval_minutes": 15,
            "watch_instance_types": ["g5.xlarge", "p5.48xlarge"],
            "enabled_regions": [],
        }


def _synth(app: cdk.App, construct_id: str = "test-global-stack") -> assertions.Template:
    stack = GCOGlobalStack(app, construct_id, config=cast(ConfigLoader, MockConfigLoader(app)))
    return assertions.Template.from_stack(stack)


def _bucket_name_starts_with_cluster_shared(bucket_name: Any) -> bool:
    """Return True if the CFN ``BucketName`` value resolves to a name starting with
    ``gco-test-cluster-shared-``.

    CDK serializes the bucket name as ``Fn::Join["", ["gco-test-cluster-shared-",
    {"Ref": "AWS::AccountId"}, "-", {"Ref": "AWS::Region"}]]``, so we inspect
    the Join parts rather than comparing to a literal string.
    """
    if isinstance(bucket_name, str):
        return bucket_name.startswith("gco-test-cluster-shared-")
    if isinstance(bucket_name, dict) and "Fn::Join" in bucket_name:
        parts = bucket_name["Fn::Join"][1]
        if parts and isinstance(parts[0], str):
            return parts[0].startswith("gco-test-cluster-shared-")
    return False


def _find_cluster_shared_bucket(template: assertions.Template) -> tuple[str, Mapping[str, Any]]:
    """Return ``(logical_id, resource)`` for the primary ``Cluster_Shared_Bucket``.

    Identified by the ``gco-test-cluster-shared-`` ``BucketName`` prefix (the
    stable ARN prefix contract for this bucket).
    """
    buckets = template.find_resources("AWS::S3::Bucket")
    matches = [
        (lid, res)
        for lid, res in buckets.items()
        if _bucket_name_starts_with_cluster_shared(res.get("Properties", {}).get("BucketName"))
    ]
    assert len(matches) == 1, (
        f"Expected exactly one bucket named gco-test-cluster-shared-*, found {len(matches)}"
    )
    return matches[0]


def _find_cluster_shared_access_logs_bucket(
    template: assertions.Template,
) -> tuple[str, Mapping[str, Any]]:
    """Return ``(logical_id, resource)`` for the cluster-shared access-logs bucket.

    Identified by being the bucket referenced as
    ``LoggingConfiguration.DestinationBucketName`` of the primary cluster-shared
    bucket. This avoids coupling to resource logical-id spelling and cleanly
    disambiguates from ``model_bucket_access_logs``.
    """
    _, primary = _find_cluster_shared_bucket(template)
    logging_cfg = primary["Properties"].get("LoggingConfiguration")
    assert isinstance(logging_cfg, dict) and "DestinationBucketName" in logging_cfg, (
        f"Primary cluster_shared_bucket should have LoggingConfiguration, got {logging_cfg!r}"
    )
    dest = logging_cfg["DestinationBucketName"]
    assert isinstance(dest, dict) and "Ref" in dest, (
        f"LoggingConfiguration.DestinationBucketName should be a Ref, got {dest!r}"
    )
    buckets = template.find_resources("AWS::S3::Bucket")
    dest_lid = dest["Ref"]
    assert dest_lid in buckets, (
        f"LoggingConfiguration references {dest_lid} but no such S3::Bucket exists"
    )
    return dest_lid, buckets[dest_lid]


def _find_cluster_shared_kms_key(template: assertions.Template) -> tuple[str, Mapping[str, Any]]:
    """Return ``(logical_id, resource)`` for ``Cluster_Shared_KMS_Key``.

    Identified by the presence of ``PendingWindowInDays`` — the model-bucket
    KMS key synthesizes without that property, while the cluster-shared key
    sets ``pending_window=Duration.days(7)``. Asserts exactly one match.
    """
    keys = template.find_resources("AWS::KMS::Key")
    matches = [
        (lid, res)
        for lid, res in keys.items()
        if "PendingWindowInDays" in res.get("Properties", {})
    ]
    assert len(matches) == 1, (
        f"Expected exactly one KMS key with PendingWindowInDays, found {len(matches)}"
    )
    return matches[0]


def _find_cluster_shared_bucket_policy(
    template: assertions.Template, bucket_logical_id: str
) -> Mapping[str, Any]:
    """Return the ``AWS::S3::BucketPolicy`` resource for the given bucket logical id."""
    policies = template.find_resources("AWS::S3::BucketPolicy")
    matches = [
        res
        for res in policies.values()
        if res.get("Properties", {}).get("Bucket", {}).get("Ref") == bucket_logical_id
    ]
    assert len(matches) == 1, (
        f"Expected exactly one BucketPolicy for {bucket_logical_id}, found {len(matches)}"
    )
    return matches[0]


class TestClusterSharedBucket:
    """Invariants for ``Cluster_Shared_Bucket`` in ``GCOGlobalStack``.

    Verifies the always-on bucket, its dedicated access-logs bucket, and the
    customer-managed KMS key that encrypts them, plus the SSM parameters and
    CloudFormation outputs that advertise the bucket to downstream consumers
    (regional job-pod roles unconditionally, SageMaker execution role when
    analytics is enabled)."""

    def test_exactly_one_cluster_shared_named_bucket(self):
        """Exactly one bucket has BucketName prefix ``gco-test-cluster-shared-``."""
        template = _synth(cdk.App())
        _find_cluster_shared_bucket(template)  # raises if zero or >1

    def test_primary_bucket_is_kms_encrypted_with_cluster_shared_key(self):
        """Primary bucket's ``BucketEncryption`` references the cluster-shared KMS key via ``Fn::GetAtt``."""
        template = _synth(cdk.App())
        _, primary = _find_cluster_shared_bucket(template)
        kms_lid, _ = _find_cluster_shared_kms_key(template)

        encryption = primary["Properties"]["BucketEncryption"]
        cfgs = encryption["ServerSideEncryptionConfiguration"]
        assert len(cfgs) == 1, f"Expected one SSE configuration, got {len(cfgs)}"

        sse = cfgs[0]["ServerSideEncryptionByDefault"]
        assert sse.get("SSEAlgorithm") == "aws:kms", (
            f"Cluster_Shared_Bucket should use SSE-KMS, got {sse!r}"
        )

        key_id = sse.get("KMSMasterKeyID")
        assert isinstance(key_id, dict) and "Fn::GetAtt" in key_id, (
            f"KMSMasterKeyID should be a Fn::GetAtt reference, got {key_id!r}"
        )
        get_att = key_id["Fn::GetAtt"]
        assert get_att[0] == kms_lid and get_att[1] == "Arn", (
            f"KMSMasterKeyID should GetAtt the cluster-shared KMS key ARN, got {get_att!r}"
        )

    def test_primary_bucket_blocks_all_public_access(self):
        """Primary bucket sets all four PublicAccessBlockConfiguration flags to ``True``."""
        template = _synth(cdk.App())
        _, primary = _find_cluster_shared_bucket(template)
        expected = {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }
        pab = primary["Properties"].get("PublicAccessBlockConfiguration")
        assert pab == expected, (
            f"Cluster_Shared_Bucket does not fully block public access; got {pab!r}"
        )

    def test_primary_bucket_versioning_enabled(self):
        """Primary bucket has ``VersioningConfiguration.Status=Enabled``."""
        template = _synth(cdk.App())
        _, primary = _find_cluster_shared_bucket(template)
        versioning = primary["Properties"].get("VersioningConfiguration")
        assert versioning == {"Status": "Enabled"}, (
            f"Expected VersioningConfiguration.Status=Enabled, got {versioning!r}"
        )

    def test_primary_bucket_and_access_logs_and_kms_key_deletion_policy_delete(self):
        """Primary bucket, access-logs bucket, and KMS key all carry ``DeletionPolicy=Delete``."""
        template = _synth(cdk.App())
        _, primary = _find_cluster_shared_bucket(template)
        _, access_logs = _find_cluster_shared_access_logs_bucket(template)
        _, kms_key = _find_cluster_shared_kms_key(template)

        for label, resource in (
            ("cluster_shared_bucket", primary),
            ("cluster_shared_access_logs_bucket", access_logs),
            ("cluster_shared_kms_key", kms_key),
        ):
            assert resource.get("DeletionPolicy") == "Delete", (
                f"{label} DeletionPolicy should be Delete (DESTROY), got "
                f"{resource.get('DeletionPolicy')!r}"
            )

    def test_ssm_parameters_published_at_documented_paths(self):
        """Three SSM parameters exist at ``/gco-test/cluster-shared-bucket/{name,arn,region}``."""
        template = _synth(cdk.App())
        params = template.find_resources("AWS::SSM::Parameter")
        names = {res["Properties"].get("Name") for res in params.values()}
        expected = {
            "/gco-test/cluster-shared-bucket/name",
            "/gco-test/cluster-shared-bucket/arn",
            "/gco-test/cluster-shared-bucket/region",
        }
        missing = expected - names
        assert not missing, (
            f"Missing expected SSM parameters: {sorted(missing)}. "
            f"Present /gco-test/cluster-shared-bucket/* params: "
            f"{sorted(n for n in names if isinstance(n, str) and n.startswith('/gco-test/cluster-shared-bucket/'))}"
        )

    def test_cfn_outputs_emitted_with_expected_export_names(self):
        """All four ``ClusterShared*`` CfnOutputs are emitted with the project-scoped export names."""
        template = _synth(cdk.App())
        project_name = MockConfigLoader().get_project_name()

        expected_outputs = {
            "ClusterSharedBucketName": f"{project_name}-cluster-shared-bucket-name",
            "ClusterSharedBucketArn": f"{project_name}-cluster-shared-bucket-arn",
            "ClusterSharedBucketRegion": f"{project_name}-cluster-shared-bucket-region",
            "ClusterSharedKmsKeyArn": f"{project_name}-cluster-shared-kms-key-arn",
        }
        for output_key, export_name in expected_outputs.items():
            template.has_output(output_key, {"Export": {"Name": export_name}})

    def test_primary_bucket_policy_denies_insecure_transport(self):
        """Primary bucket policy has an explicit Deny for ``aws:SecureTransport=false`` on the primary bucket.

        We look specifically at the BucketPolicy attached to the primary
        cluster-shared bucket (not the access-logs bucket, which also has an
        ``enforce_ssl=True`` deny). The design explicitly adds a second Deny
        with SID ``DenyInsecureTransport`` as belt-and-suspenders; either
        statement satisfies the requirement, so we just assert at least one
        matching statement exists.
        """
        template = _synth(cdk.App())
        primary_lid, _ = _find_cluster_shared_bucket(template)
        policy = _find_cluster_shared_bucket_policy(template, primary_lid)

        statements = policy["Properties"]["PolicyDocument"]["Statement"]
        deny_statements = [
            s
            for s in statements
            if s.get("Effect") == "Deny"
            and s.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") == "false"
        ]
        assert deny_statements, (
            "Primary cluster_shared_bucket policy must contain at least one Deny "
            "statement with Condition.Bool.aws:SecureTransport=false; "
            f"statements={statements!r}"
        )

    def test_kms_key_has_rotation_and_seven_day_pending_window(self):
        """Cluster-shared KMS key sets ``EnableKeyRotation=true`` and ``PendingWindowInDays=7``."""
        template = _synth(cdk.App())
        _, key = _find_cluster_shared_kms_key(template)

        props = key["Properties"]
        assert props.get("EnableKeyRotation") is True, (
            f"EnableKeyRotation should be True, got {props.get('EnableKeyRotation')!r}"
        )
        assert props.get("PendingWindowInDays") == 7, (
            f"PendingWindowInDays should be 7, got {props.get('PendingWindowInDays')!r}"
        )

    def test_access_logs_bucket_is_separate_and_kms_encrypted_with_cluster_shared_key(self):
        """Cluster-shared access-logs bucket is distinct from ``model_bucket_access_logs`` and KMS-encrypted with the cluster-shared key."""
        template = _synth(cdk.App())

        primary_lid, _ = _find_cluster_shared_bucket(template)
        access_logs_lid, access_logs = _find_cluster_shared_access_logs_bucket(template)
        kms_lid, _ = _find_cluster_shared_kms_key(template)

        # Must be a different bucket than the primary.
        assert access_logs_lid != primary_lid, (
            "Access-logs bucket must be a distinct resource from the primary bucket"
        )

        # Must also differ from the model-weights access-logs bucket, which is
        # SSE-S3-encrypted (SSEAlgorithm=AES256) and has no KMSMasterKeyID.
        # The cluster-shared access-logs bucket is SSE-KMS with the
        # cluster-shared key. This assertion distinguishes the two.
        encryption = access_logs["Properties"]["BucketEncryption"]
        cfgs = encryption["ServerSideEncryptionConfiguration"]
        assert len(cfgs) == 1, f"Expected one SSE configuration, got {len(cfgs)}"
        sse = cfgs[0]["ServerSideEncryptionByDefault"]
        assert sse.get("SSEAlgorithm") == "aws:kms", (
            f"cluster_shared_access_logs_bucket should use SSE-KMS, got {sse!r}"
        )
        key_id = sse.get("KMSMasterKeyID")
        assert isinstance(key_id, dict) and "Fn::GetAtt" in key_id, (
            f"KMSMasterKeyID should be a Fn::GetAtt reference, got {key_id!r}"
        )
        get_att = key_id["Fn::GetAtt"]
        assert get_att[0] == kms_lid and get_att[1] == "Arn", (
            "cluster_shared_access_logs_bucket should be encrypted with the "
            f"cluster-shared KMS key, got GetAtt={get_att!r}"
        )

        # Sanity: the access-logs bucket has PublicAccessBlock fully enabled
        # as well (matches the posture applied to both buckets).
        expected_pab = {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }
        pab = access_logs["Properties"].get("PublicAccessBlockConfiguration")
        assert pab == expected_pab, (
            f"cluster_shared_access_logs_bucket does not fully block public access; got {pab!r}"
        )
