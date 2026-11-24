"""
Tests for ``GCOAnalyticsStack``.

This suite covers:

1. **Off-by-default**: when ``analytics_environment.enabled=false``, the
   analytics-specific resource types (``AWS::SageMaker::Domain``,
   ``AWS::EMRServerless::Application``, ``AWS::Cognito::UserPool``) must
   not appear in any synthesized stack.

2. **Stack present when enabled**: when the toggle is ``true``,
   ``GCOAnalyticsStack`` is instantiated in the API gateway region and
   materializes the full resource set — Analytics_KMS_Key, private-
   isolated VPC with the documented interface endpoints, Studio domain,
   EMR Serverless application, Cognito user pool, Studio_EFS, and the
   Studio_Only_Bucket plus its access-logs bucket.

3. **``_parse_removal`` helper** — round-trips ``"retain"`` / ``"destroy"``
   and raises ``ValueError`` on anything else.

These tests synthesize the stack in isolation rather than running the full
``app.py`` pipeline. The full-app synthesis path is already covered by
``tests/test_nag_compliance.py`` and ``tests/test_cdk_synthesis_matrix.py``;
here we want a targeted test that runs in pytest without Docker and without
the heavy regional stack's helm-installer asset.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from gco.config.config_loader import ConfigLoader
from gco.stacks.analytics_stack import GCOAnalyticsStack, _parse_removal

_REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _AnalyticsMockConfig:
    """Minimal stand-in for ``ConfigLoader`` sufficient for
    ``GCOAnalyticsStack.__init__``.

    ``GCOAnalyticsStack`` only reads
    ``config.get_analytics_config()`` during construction, so we expose
    that single method plus the minimum demographic helpers needed by
    the base ``Stack`` identity (project name, tags).
    """

    def __init__(
        self,
        enabled: bool = True,
        efs_removal: str = "destroy",
        cognito_removal: str = "destroy",
        hyperpod_enabled: bool = False,
        canvas_enabled: bool = False,
        region: str = "us-east-2",
        workload_regions: tuple[str, ...] = ("us-east-1", "us-west-2"),
    ) -> None:
        self._enabled = enabled
        self._workload_regions = workload_regions
        self._efs_removal = efs_removal
        self._cognito_removal = cognito_removal
        self._hyperpod_enabled = hyperpod_enabled
        self._canvas_enabled = canvas_enabled
        self._region = region

    def get_project_name(self) -> str:
        return "gco-test"

    def get_tags(self) -> dict[str, str]:
        return {"Environment": "test", "Project": "gco"}

    def get_global_region(self) -> str:
        """Return a fixed global region for cross-region SSM reads.

        Section 8's ``_grant_sagemaker_role_on_cluster_shared_bucket`` uses
        this to build the ``AwsCustomResource`` that reads
        ``/gco/cluster-shared-bucket/arn`` from the global region.
        """
        return self._region

    def get_api_gateway_region(self) -> str:
        """Return a fixed api-gateway region for IAM resource scoping.

        Section 8's ``_create_execution_role_and_grants`` uses this to
        scope the ``execute-api:Invoke`` statement on the SageMaker role.
        """
        return self._region

    def get_regions(self) -> list[str]:
        """Workload Regions, whose job queues the SageMaker role may send to."""
        return list(self._workload_regions)

    def get_analytics_config(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "hyperpod": {"enabled": self._hyperpod_enabled},
            "canvas": {"enabled": self._canvas_enabled},
            "cognito": {
                "domain_prefix": None,
                "removal_policy": self._cognito_removal,
            },
            "efs": {"removal_policy": self._efs_removal},
            "studio": {"user_profile_name_prefix": None},
        }

    def get_analytics_enabled(self) -> bool:
        return self._enabled


def _synth_analytics(
    app: cdk.App | None = None,
    construct_id: str = "test-analytics-stack",
    config: _AnalyticsMockConfig | None = None,
) -> assertions.Template:
    """Synthesize ``GCOAnalyticsStack`` standalone and return its template."""
    if app is None:
        app = cdk.App()
    if config is None:
        config = _AnalyticsMockConfig()
    stack = GCOAnalyticsStack(
        app,
        construct_id,
        config=cast(ConfigLoader, config),
        env=cdk.Environment(account="123456789012", region=config.get_api_gateway_region()),
    )
    return assertions.Template.from_stack(stack)


_DOMAIN_ID = {"Fn::GetAtt": ["StudioDomain", "DomainId"]}


def _render_arn(value: Any) -> str:
    """Render a synthesized ARN: the partition as ``aws``, the domain id as ``<domain>``.

    Literal parts are kept, ``Fn::Join`` parts are concatenated, and any other
    token is rendered as its JSON, so an unexpected reference stays visible.
    """
    if isinstance(value, str):
        return value
    if value == {"Ref": "AWS::Partition"}:
        return "aws"
    if value == _DOMAIN_ID:
        return "<domain>"
    if isinstance(value, dict) and "Fn::Join" in value:
        return "".join(_render_arn(part) for part in value["Fn::Join"][1])
    return json.dumps(value, sort_keys=True)


# ---------------------------------------------------------------------------
# _parse_removal helper
# ---------------------------------------------------------------------------


class TestParseRemoval:
    """Tests for the module-scope ``_parse_removal`` helper."""

    def test_parse_destroy(self) -> None:
        assert _parse_removal("destroy") is cdk.RemovalPolicy.DESTROY

    def test_parse_retain(self) -> None:
        assert _parse_removal("retain") is cdk.RemovalPolicy.RETAIN

    def test_parse_is_case_insensitive(self) -> None:
        assert _parse_removal("DESTROY") is cdk.RemovalPolicy.DESTROY
        assert _parse_removal("Retain") is cdk.RemovalPolicy.RETAIN

    def test_parse_strips_whitespace(self) -> None:
        assert _parse_removal("  destroy  ") is cdk.RemovalPolicy.DESTROY

    def test_parse_rejects_unknown(self) -> None:
        with pytest.raises(ValueError, match="removal_policy must be"):
            _parse_removal("snapshot")

    def test_parse_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="removal_policy must be"):
            _parse_removal("")


# ---------------------------------------------------------------------------
# Off-by-default invariants
# ---------------------------------------------------------------------------


class TestAnalyticsStackOffByDefault:
    """When ``analytics_environment.enabled=false``, ``app.py`` must not
    instantiate ``GCOAnalyticsStack`` at all, so the analytics-specific
    resource types never appear in any synthesized template.

    We test this at the ``app.py`` wiring level: build a minimal ``cdk.App``
    with ``enabled=false``, exercise the same conditional that ``app.py``
    uses, and assert the analytics stack is absent. The equivalent positive
    check (stack present when enabled) is the companion test class below.
    """

    def test_app_skips_analytics_stack_when_disabled(self) -> None:
        """The conditional in ``app.py`` gates ``GCOAnalyticsStack``
        instantiation on ``config.get_analytics_enabled()``.

        We mirror that here: construct a config that reports ``enabled=false``
        and assert no analytics stack is present on the app's node tree.
        """
        app = cdk.App(
            context={
                "analytics_environment": {
                    "enabled": False,
                    "hyperpod": {"enabled": False},
                    "canvas": {"enabled": False},
                    "cognito": {"domain_prefix": None, "removal_policy": "destroy"},
                    "efs": {"removal_policy": "destroy"},
                    "studio": {"user_profile_name_prefix": None},
                }
            }
        )
        config = _AnalyticsMockConfig(enabled=False)

        # Replicate the app.py conditional exactly.
        created_stack: GCOAnalyticsStack | None = None
        if config.get_analytics_enabled():
            created_stack = GCOAnalyticsStack(
                app,
                "gco-analytics",
                config=cast(ConfigLoader, config),
                env=cdk.Environment(account="123456789012", region="us-east-2"),
            )

        assert created_stack is None
        # No stack in the node tree either.
        stacks = [child for child in app.node.children if isinstance(child, cdk.Stack)]
        assert stacks == [], f"Expected no stacks, got {[s.stack_name for s in stacks]}"

    def test_isolated_analytics_stack_has_sagemaker_emr_cognito_resources(
        self,
    ) -> None:
        """With Section 8 landed, ``GCOAnalyticsStack`` materializes the full resource set.

        Counterpart to ``test_app_skips_analytics_stack_when_disabled`` —
        that test guards the off-by-default semantics (resources absent
        when the toggle is off, via the ``app.py`` conditional). This
        test confirms the positive direction: when the stack is actually
        synthesized, the analytics-specific resource types land in the
        template. The shape of each resource is asserted in the dedicated
        section-8 test classes below (Studio domain, EMR, Cognito, EFS).
        """
        template = _synth_analytics()
        template.resource_count_is("AWS::SageMaker::Domain", 1)
        template.resource_count_is("AWS::EMRServerless::Application", 1)
        template.resource_count_is("AWS::Cognito::UserPool", 1)


# ---------------------------------------------------------------------------
# KMS key
# ---------------------------------------------------------------------------


class TestAnalyticsKmsKey:
    """Assertions over the ``Analytics_KMS_Key`` produced by
    ``_create_kms_key``."""

    def test_one_customer_managed_kms_key_is_created(self) -> None:
        template = _synth_analytics()
        template.resource_count_is("AWS::KMS::Key", 1)

    def test_kms_key_has_rotation_and_pending_window(self) -> None:
        """Rotation enabled, 7-day pending window, destroy-by-default."""
        template = _synth_analytics()
        template.has_resource_properties(
            "AWS::KMS::Key",
            {
                "EnableKeyRotation": True,
                "PendingWindowInDays": 7,
            },
        )

    def test_kms_key_removal_policy_is_destroy(self) -> None:
        """``removal_policy=DESTROY`` for the iteration-loop posture."""
        template = _synth_analytics()
        keys = template.find_resources("AWS::KMS::Key")
        assert keys, "expected at least one KMS key"
        for resource in keys.values():
            assert resource["DeletionPolicy"] == "Delete"
            assert resource["UpdateReplacePolicy"] == "Delete"

    def test_kms_key_grants_encrypt_decrypt_to_service_principals(self) -> None:
        """The four documented service principals appear in the key policy."""
        template = _synth_analytics()
        keys = template.find_resources("AWS::KMS::Key")
        assert len(keys) == 1
        key = next(iter(keys.values()))
        statements = key["Properties"]["KeyPolicy"]["Statement"]

        # Collect every service principal referenced by any statement.
        referenced_services: set[str] = set()
        for stmt in statements:
            principal = stmt.get("Principal", {})
            services = principal.get("Service") if isinstance(principal, dict) else None
            if isinstance(services, str):
                referenced_services.add(services)
            elif isinstance(services, list):
                for s in services:
                    if isinstance(s, str):
                        referenced_services.add(s)
                    elif isinstance(s, dict) and "Fn::Join" in s:
                        # e.g. ``logs.<region>.amazonaws.com`` is serialized as a Join.
                        parts = s["Fn::Join"][1]
                        flat = "".join(p if isinstance(p, str) else "" for p in parts)
                        referenced_services.add(flat)

        # ``logs.<region>.amazonaws.com`` contains a Ref to AWS::Region, so
        # the Join flattening above collapses the Ref to the empty string.
        # Assert exact-string set membership for the three static principals
        # (codeql[py/incomplete-url-substring-sanitization] only fires on
        # ``in`` against a URL/host string; asserting equality via
        # ``.issuperset`` leaves no "substring check on a URL" ambiguity).
        required_exact = {
            "sagemaker.amazonaws.com",
            "s3.amazonaws.com",
            "elasticfilesystem.amazonaws.com",
        }
        assert required_exact.issubset(referenced_services), (
            f"KMS key policy is missing required service principals. "
            f"expected superset of {sorted(required_exact)!r}, "
            f"got {sorted(referenced_services)!r}"
        )
        assert any(
            s.startswith("logs.") and s.endswith(".amazonaws.com") for s in referenced_services
        ), f"expected a logs.<region>.amazonaws.com principal, got {sorted(referenced_services)!r}"


# ---------------------------------------------------------------------------
# VPC + endpoints
# ---------------------------------------------------------------------------


class TestAnalyticsVpc:
    """Assertions over the VPC and endpoints produced by
    ``_create_vpc_and_endpoints``."""

    def test_exactly_one_vpc_is_created(self) -> None:
        template = _synth_analytics()
        template.resource_count_is("AWS::EC2::VPC", 1)

    def test_vpc_has_internet_gateway_for_nat(self) -> None:
        """Private-with-egress subnets require a NAT gateway which needs
        an internet gateway in the public subnet.
        """
        template = _synth_analytics()
        template.resource_count_is("AWS::EC2::InternetGateway", 1)

    def test_vpc_has_one_nat_gateway(self) -> None:
        """Single NAT gateway for internet egress (pip install, git clone)."""
        template = _synth_analytics()
        template.resource_count_is("AWS::EC2::NatGateway", 1)

    def test_vpc_has_two_subnets_across_two_azs(self) -> None:
        """At least two subnets across at least two availability
        zones. ``max_azs=2`` with a private (egress) and a public subnet
        tier yields four subnets.
        """
        template = _synth_analytics()
        subnets = template.find_resources("AWS::EC2::Subnet")
        assert len(subnets) >= 2, f"expected >=2 subnets for multi-AZ coverage, got {len(subnets)}"
        azs = {resource["Properties"].get("AvailabilityZone") for resource in subnets.values()}
        assert len(azs) >= 2, f"expected subnets across >=2 availability zones, got {azs!r}"

    def test_s3_gateway_endpoint_exists(self) -> None:
        """Gateway endpoint for S3."""
        template = _synth_analytics()
        endpoints = template.find_resources("AWS::EC2::VPCEndpoint")
        gateway_endpoints = [
            r for r in endpoints.values() if r["Properties"].get("VpcEndpointType") == "Gateway"
        ]
        assert len(gateway_endpoints) == 1
        service_name = gateway_endpoints[0]["Properties"]["ServiceName"]
        # ServiceName is a Join like ``com.amazonaws.<region>.s3``.
        if isinstance(service_name, dict) and "Fn::Join" in service_name:
            joined = "".join(
                part if isinstance(part, str) else "" for part in service_name["Fn::Join"][1]
            )
        else:
            joined = str(service_name)
        assert joined.endswith(".s3"), f"expected S3 gateway endpoint, got {joined!r}"

    def test_interface_endpoints_cover_all_required_services(self) -> None:
        """The nine interface endpoints are present."""
        template = _synth_analytics()
        endpoints = template.find_resources("AWS::EC2::VPCEndpoint")
        interface_endpoints = [
            r for r in endpoints.values() if r["Properties"].get("VpcEndpointType") == "Interface"
        ]

        def _extract_service_suffix(service_name: Any) -> str:
            if isinstance(service_name, dict) and "Fn::Join" in service_name:
                return "".join(
                    part if isinstance(part, str) else "" for part in service_name["Fn::Join"][1]
                )
            return str(service_name)

        suffixes = [
            _extract_service_suffix(r["Properties"]["ServiceName"]) for r in interface_endpoints
        ]

        # Each required service's ServiceName ends with a distinctive suffix.
        # Note: ``SAGEMAKER_STUDIO`` and ``SAGEMAKER_NOTEBOOK`` map to
        # ``aws.sagemaker.<region>.studio`` and ``aws.sagemaker.<region>.notebook``
        # rather than the ``com.amazonaws.<region>.*`` shape — they live in the
        # AWS-owned ``aws.sagemaker`` namespace.
        required_suffixes = [
            ".sagemaker.api",
            ".sagemaker.runtime",
            ".studio",
            ".notebook",
            ".sts",
            ".logs",
            ".ecr.api",
            ".ecr.dkr",
            ".elasticfilesystem",
        ]
        for suffix in required_suffixes:
            assert any(s.endswith(suffix) for s in suffixes), (
                f"expected an interface endpoint whose ServiceName ends with "
                f"{suffix!r}; got {sorted(suffixes)!r}"
            )

        # Exactly nine — no extras, no accidental duplicates.
        assert len(interface_endpoints) == 9, (
            f"expected 9 interface endpoints, got {len(interface_endpoints)} ({sorted(suffixes)!r})"
        )


# ---------------------------------------------------------------------------
# S3 buckets — Studio_Only_Bucket + access-logs
# ---------------------------------------------------------------------------


class TestStudioOnlyBucket:
    """Assertions over ``Studio_Only_Bucket`` and its access-logs sidecar.

    Identified by the ``gco-test-analytics-studio-`` bucket-name prefix (the
    deny-list anchor for bucket-isolation checks). The access-logs sidecar
    is identified indirectly — as the bucket referenced by
    ``LoggingConfiguration.DestinationBucketName`` on ``Studio_Only_Bucket``
    — so the test never couples to CDK's logical id spelling.
    """

    def _find_studio_only_bucket(
        self, template: assertions.Template
    ) -> tuple[str, Mapping[str, Any]]:
        """Return ``(logical_id, resource)`` for ``Studio_Only_Bucket``.

        The bucket carries a CloudFormation-generated physical name (a fixed
        project/account/region name is a collision hazard in S3's global
        namespace), so the construct logical id ``StudioOnlyBucket<hash>`` is
        the handle — never a name prefix.
        """
        buckets = template.find_resources("AWS::S3::Bucket")
        matches = [(lid, res) for lid, res in buckets.items() if lid.startswith("StudioOnlyBucket")]
        assert len(matches) == 1, f"expected exactly one Studio_Only_Bucket, got {len(matches)}"
        return matches[0]

    def _find_access_logs_bucket(
        self, template: assertions.Template
    ) -> tuple[str, Mapping[str, Any]]:
        """Return ``(logical_id, resource)`` for the access-logs sidecar.

        Disambiguated by presence of a lifecycle rule named
        ``ExpireAccessLogs`` (the only bucket in this stack that sets one).
        """
        buckets = template.find_resources("AWS::S3::Bucket")
        matches = []
        for lid, res in buckets.items():
            lifecycle = res["Properties"].get("LifecycleConfiguration") or {}
            rules = lifecycle.get("Rules") or []
            if any(r.get("Id") == "ExpireAccessLogs" for r in rules):
                matches.append((lid, res))
        assert len(matches) == 1, (
            f"expected exactly one access-logs bucket with an "
            f"ExpireAccessLogs rule, got {len(matches)}"
        )
        return matches[0]

    def test_no_bucket_pins_a_physical_name(self) -> None:
        """Every bucket is CloudFormation-named: S3 names are a global namespace and
        a deleted name is not reliably reusable, so a destroy-and-redeploy of a
        fixed ``<project>-analytics-studio-<account>-<region>`` name could collide."""
        template = _synth_analytics()
        self._find_studio_only_bucket(template)
        named = [
            lid
            for lid, res in template.find_resources("AWS::S3::Bucket").items()
            if "BucketName" in res["Properties"]
        ]
        assert named == [], f"buckets with explicit BucketName: {named}"

    def test_studio_only_bucket_is_kms_encrypted_with_analytics_key(self) -> None:
        """``BucketEncryption`` references ``Analytics_KMS_Key`` via
        ``Fn::GetAtt``, not SSE-S3."""
        template = _synth_analytics()
        _, bucket = self._find_studio_only_bucket(template)

        keys = template.find_resources("AWS::KMS::Key")
        assert len(keys) == 1, "expected exactly one analytics KMS key"
        kms_lid = next(iter(keys))

        encryption = bucket["Properties"]["BucketEncryption"]
        cfgs = encryption["ServerSideEncryptionConfiguration"]
        assert len(cfgs) == 1
        sse = cfgs[0]["ServerSideEncryptionByDefault"]
        assert sse.get("SSEAlgorithm") == "aws:kms", (
            f"Studio_Only_Bucket should use SSE-KMS, got {sse!r}"
        )
        key_id = sse.get("KMSMasterKeyID")
        assert isinstance(key_id, dict) and "Fn::GetAtt" in key_id
        get_att = key_id["Fn::GetAtt"]
        assert get_att[0] == kms_lid and get_att[1] == "Arn", (
            f"KMSMasterKeyID should GetAtt the analytics KMS key ARN, got {get_att!r}"
        )

    def test_both_buckets_block_all_public_access(self) -> None:
        """Studio_Only_Bucket and access-logs bucket both have all four
        ``PublicAccessBlockConfiguration`` flags enabled."""
        template = _synth_analytics()
        expected = {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }
        for finder_name, finder in (
            ("studio_only_bucket", self._find_studio_only_bucket),
            ("access_logs_bucket", self._find_access_logs_bucket),
        ):
            _, bucket = finder(template)
            pab = bucket["Properties"].get("PublicAccessBlockConfiguration")
            assert pab == expected, f"{finder_name} does not fully block public access; got {pab!r}"

    def test_studio_only_bucket_versioning_and_deletion_policy(self) -> None:
        """Studio_Only_Bucket has ``VersioningConfiguration.Status=Enabled``
        and ``DeletionPolicy=Delete`` by default."""
        template = _synth_analytics()
        _, bucket = self._find_studio_only_bucket(template)
        versioning = bucket["Properties"].get("VersioningConfiguration")
        assert versioning == {"Status": "Enabled"}, (
            f"expected VersioningConfiguration.Status=Enabled, got {versioning!r}"
        )
        assert bucket.get("DeletionPolicy") == "Delete", (
            f"expected DeletionPolicy=Delete, got {bucket.get('DeletionPolicy')!r}"
        )
        assert bucket.get("UpdateReplacePolicy") == "Delete"

    def test_studio_only_bucket_policy_denies_insecure_transport(self) -> None:
        """The bucket policy contains an explicit ``Deny`` statement
        with ``aws:SecureTransport=false`` on ``Studio_Only_Bucket``.

        This is the belt-and-suspenders Deny the stack adds on top of
        ``enforce_ssl=True`` — verifiable by the ``DenyInsecureTransport``
        SID (or any matching Deny). Either way the statement must exist.
        """
        template = _synth_analytics()
        bucket_lid, _ = self._find_studio_only_bucket(template)

        policies = template.find_resources("AWS::S3::BucketPolicy")
        matches = [
            res
            for res in policies.values()
            if res["Properties"].get("Bucket", {}).get("Ref") == bucket_lid
        ]
        assert len(matches) == 1, (
            f"expected exactly one BucketPolicy for Studio_Only_Bucket, got {len(matches)}"
        )
        statements = matches[0]["Properties"]["PolicyDocument"]["Statement"]
        deny_insecure = [
            s
            for s in statements
            if s.get("Effect") == "Deny"
            and s.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") == "false"
        ]
        assert deny_insecure, (
            "expected at least one Deny statement with "
            "Condition.Bool.aws:SecureTransport=false on Studio_Only_Bucket; "
            f"statements={statements!r}"
        )


# ---------------------------------------------------------------------------
# Studio_EFS
# ---------------------------------------------------------------------------


class TestStudioEfs:
    """Assertions over ``Studio_EFS`` and its dedicated security group."""

    def test_exactly_one_efs_file_system_is_created(self) -> None:
        template = _synth_analytics()
        template.resource_count_is("AWS::EFS::FileSystem", 1)

    @pytest.mark.parametrize("removal", ["destroy", "retain"])
    def test_efs_carries_the_tags_the_doctor_orphan_scan_matches(self, removal: str) -> None:
        """A retained file system is found by its tags after its stack is gone.

        ``gco analytics doctor`` matches ``gco:analytics:managed`` and
        ``gco:project`` (``_managed_by_project``); CDK's ``Name`` tag stays.
        """
        from cli.analytics_user_mgmt import _managed_by_project

        template = _synth_analytics(config=_AnalyticsMockConfig(efs_removal=removal))
        fs = next(iter(template.find_resources("AWS::EFS::FileSystem").values()))
        tags = {tag["Key"]: tag["Value"] for tag in fs["Properties"]["FileSystemTags"]}
        assert tags == {
            "gco:analytics:managed": "true",
            "gco:project": "gco-test",
            "Name": "test-analytics-stack/StudioEfs",
        }
        assert _managed_by_project(tags, "gco-test")
        assert not _managed_by_project(tags, "other-project")

    def test_efs_is_kms_encrypted_with_analytics_key(self) -> None:
        """``Encrypted=true`` and ``KmsKeyId`` references the analytics
        KMS key via ``Fn::GetAtt``."""
        template = _synth_analytics()
        fses = template.find_resources("AWS::EFS::FileSystem")
        assert len(fses) == 1
        fs = next(iter(fses.values()))

        assert fs["Properties"].get("Encrypted") is True, (
            f"expected Encrypted=true, got {fs['Properties'].get('Encrypted')!r}"
        )

        keys = template.find_resources("AWS::KMS::Key")
        assert len(keys) == 1
        kms_lid = next(iter(keys))

        kms_key_id = fs["Properties"].get("KmsKeyId")
        assert isinstance(kms_key_id, dict) and "Fn::GetAtt" in kms_key_id, (
            f"expected KmsKeyId to reference the analytics KMS key via Fn::GetAtt, "
            f"got {kms_key_id!r}"
        )
        get_att = kms_key_id["Fn::GetAtt"]
        assert get_att[0] == kms_lid and get_att[1] == "Arn", (
            f"expected KmsKeyId to GetAtt analytics key ARN, got {get_att!r}"
        )

    def test_efs_client_policy_is_scoped_to_the_sagemaker_role(self) -> None:
        """Only the Studio role receives NFS client actions through mount targets."""
        template = _synth_analytics()
        fs = next(iter(template.find_resources("AWS::EFS::FileSystem").values()))
        statements = fs["Properties"]["FileSystemPolicy"]["Statement"]
        assert len(statements) == 1
        statement = statements[0]

        roles = template.find_resources("AWS::IAM::Role")
        role_ids = [
            logical_id
            for logical_id, role in roles.items()
            if isinstance(role["Properties"].get("RoleName"), str)
            and role["Properties"]["RoleName"].startswith("AmazonSageMaker")
        ]
        assert len(role_ids) == 1
        assert statement["Principal"] == {
            "AWS": {"Fn::GetAtt": [role_ids[0], "Arn"]},
        }
        assert set(statement["Action"]) == {
            "elasticfilesystem:ClientMount",
            "elasticfilesystem:ClientWrite",
            "elasticfilesystem:ClientRootAccess",
        }
        assert statement["Condition"] == {
            "Bool": {"elasticfilesystem:AccessedViaMountTarget": "true"},
        }

    def test_cleanup_role_keeps_efs_control_plane_permissions(self) -> None:
        """Stack teardown retains identity-scoped EFS cleanup authorization."""
        template = _synth_analytics()
        required_actions = {
            "elasticfilesystem:DescribeAccessPoints",
            "elasticfilesystem:DeleteAccessPoint",
            "elasticfilesystem:DescribeMountTargets",
            "elasticfilesystem:DeleteMountTarget",
            "elasticfilesystem:DeleteFileSystem",
            "elasticfilesystem:DeleteFileSystemPolicy",
        }
        policies = template.find_resources("AWS::IAM::ManagedPolicy")
        matches: list[tuple[str, Mapping[str, Any]]] = []
        for logical_id, policy in policies.items():
            statements = policy["Properties"]["PolicyDocument"]["Statement"]
            actions = {
                action
                for statement in statements
                for action in (
                    statement["Action"]
                    if isinstance(statement["Action"], list)
                    else [statement["Action"]]
                )
            }
            if required_actions.issubset(actions):
                matches.append((logical_id, policy))

        assert len(matches) == 1
        policy_id, _ = matches[0]
        attached_roles = [
            logical_id
            for logical_id, role in template.find_resources("AWS::IAM::Role").items()
            if {"Ref": policy_id} in role["Properties"].get("ManagedPolicyArns", [])
        ]
        assert len(attached_roles) == 1
        assert attached_roles[0].startswith("CleanupFunctionServiceRole")

    def test_cleanup_policy_grants_what_the_handler_calls_and_scopes_it(self) -> None:
        """The cleanup policy holds exactly the handler's API calls, each scoped.

        The actions are read from ``lambda/analytics-cleanup/handler.py`` (every
        ``<client>.<operation>(`` and paginator), so a new call fails here until
        it is granted. List calls and ``DescribeSecurityGroups`` take no
        resource; the domain's apps, spaces and profiles are named by domain id;
        the access-point lookup names Studio_EFS; the run-time access points and
        SageMaker's home file system are this Region's; the security-group
        changes are limited to this stack's VPC.
        """
        import re
        from pathlib import Path

        source = (
            Path(__file__).resolve().parent.parent / "lambda/analytics-cleanup/handler.py"
        ).read_text(encoding="utf-8")
        clients = dict(re.findall(r'(\w+) = boto3\.client\("([a-z0-9-]+)"', source))
        prefixes = {"sagemaker": "sagemaker", "efs": "elasticfilesystem", "ec2": "ec2"}
        operations = {
            (clients[var], op)
            for var, op in re.findall(r"\b(\w+)\.(\w+)\(", source)
            if var in clients and op != "get_paginator"
        } | {
            (clients[var], op)
            for var, op in re.findall(r'\b(\w+)\.get_paginator\("(\w+)"\)', source)
        }
        handler_actions = {
            f"{prefixes[service]}:{''.join(word.title() for word in op.split('_'))}"
            for service, op in operations
        }

        template = _synth_analytics().to_json()
        (policy,) = [
            resource
            for logical_id, resource in template["Resources"].items()
            if resource["Type"] == "AWS::IAM::ManagedPolicy"
            and logical_id.startswith("CleanupFunctionPolicy")
        ]
        statements = policy["Properties"]["PolicyDocument"]["Statement"]

        def _actions(statement: Mapping[str, Any]) -> frozenset[str]:
            action = statement["Action"]
            return frozenset(action if isinstance(action, list) else [action])

        granted = {action for statement in statements for action in _actions(statement)}
        assert granted == handler_actions

        _render = _render_arn
        by_actions = {
            _actions(statement): (
                sorted(_render(r) for r in statement["Resource"])
                if isinstance(statement["Resource"], list)
                else [_render(statement["Resource"])],
                {
                    operator: {key: _render(value) for key, value in condition.items()}
                    for operator, condition in statement.get("Condition", {}).items()
                }
                or None,
            )
            for statement in statements
        }
        prefix = "arn:aws:{}:us-east-2:123456789012"
        efs_id = _render({"Fn::GetAtt": [self._efs_logical_id(template), "Arn"]})
        vpc_ids = [lid for lid, r in template["Resources"].items() if r["Type"] == "AWS::EC2::VPC"]
        assert len(vpc_ids) == 1
        assert by_actions == {
            frozenset(
                {
                    "sagemaker:ListApps",
                    "sagemaker:ListSpaces",
                    "sagemaker:ListUserProfiles",
                    "ec2:DescribeSecurityGroups",
                }
            ): (["*"], None),
            frozenset(
                {
                    "sagemaker:DescribeDomain",
                    "sagemaker:DeleteApp",
                    "sagemaker:DeleteSpace",
                    "sagemaker:DeleteUserProfile",
                }
            ): (
                sorted(
                    prefix.format("sagemaker") + suffix
                    for suffix in (
                        ":domain/<domain>",
                        ":app/<domain>/*",
                        ":space/<domain>/*",
                        ":user-profile/<domain>/*",
                    )
                ),
                None,
            ),
            frozenset({"elasticfilesystem:DescribeAccessPoints"}): ([efs_id], None),
            frozenset({"elasticfilesystem:DeleteAccessPoint"}): (
                [prefix.format("elasticfilesystem") + ":access-point/*"],
                None,
            ),
            frozenset(
                {
                    "elasticfilesystem:DeleteFileSystemPolicy",
                    "elasticfilesystem:DescribeMountTargets",
                    "elasticfilesystem:DeleteMountTarget",
                    "elasticfilesystem:DeleteFileSystem",
                }
            ): ([prefix.format("elasticfilesystem") + ":file-system/*"], None),
            frozenset(
                {
                    "ec2:RevokeSecurityGroupIngress",
                    "ec2:RevokeSecurityGroupEgress",
                    "ec2:DeleteSecurityGroup",
                }
            ): (
                [prefix.format("ec2") + ":security-group/*"],
                {
                    "ArnEquals": {
                        "ec2:Vpc": "arn:aws:ec2:us-east-2:123456789012:vpc/"
                        + json.dumps({"Ref": vpc_ids[0]}, sort_keys=True)
                    }
                },
            ),
        }

    @staticmethod
    def _efs_logical_id(template: Mapping[str, Any]) -> str:
        (efs_id,) = [
            lid for lid, r in template["Resources"].items() if r["Type"] == "AWS::EFS::FileSystem"
        ]
        return efs_id

    def test_efs_deletion_policy_is_delete_by_default(self) -> None:
        """Default ``efs_removal="destroy"`` yields
        ``DeletionPolicy=Delete`` on the EFS file system."""
        template = _synth_analytics()
        fses = template.find_resources("AWS::EFS::FileSystem")
        assert len(fses) == 1
        fs = next(iter(fses.values()))
        assert fs.get("DeletionPolicy") == "Delete", (
            f"expected DeletionPolicy=Delete, got {fs.get('DeletionPolicy')!r}"
        )
        assert fs.get("UpdateReplacePolicy") == "Delete"

    def test_efs_security_group_only_allows_nfs_from_vpc_cidr(self) -> None:
        """The Studio_EFS SG has exactly one ingress rule: TCP/2049
        from the VPC CIDR block (``Fn::GetAtt <VPC>.CidrBlock``)."""
        template = _synth_analytics()

        # Locate the Studio_EFS SG by its description — the stack sets a
        # stable prose string that mentions Studio_EFS.
        sgs = template.find_resources("AWS::EC2::SecurityGroup")
        matches = [
            (lid, res)
            for lid, res in sgs.items()
            if "Studio_EFS" in str(res["Properties"].get("GroupDescription", ""))
        ]
        assert len(matches) == 1, (
            f"expected exactly one Studio_EFS security group, got {len(matches)}"
        )
        _, sg = matches[0]

        ingress = sg["Properties"].get("SecurityGroupIngress") or []
        assert len(ingress) == 1, (
            f"expected exactly one ingress rule on Studio_EFS SG, got {len(ingress)}"
        )
        rule = ingress[0]
        assert rule.get("IpProtocol") == "tcp", f"expected tcp, got {rule!r}"
        assert rule.get("FromPort") == 2049, f"expected FromPort=2049, got {rule!r}"
        assert rule.get("ToPort") == 2049, f"expected ToPort=2049, got {rule!r}"

        # CidrIp resolves to the VPC's CidrBlock via Fn::GetAtt.
        cidr = rule.get("CidrIp")
        assert isinstance(cidr, dict) and "Fn::GetAtt" in cidr, (
            f"expected CidrIp to reference the VPC CidrBlock via Fn::GetAtt, got {cidr!r}"
        )
        vpcs = template.find_resources("AWS::EC2::VPC")
        assert len(vpcs) == 1
        vpc_lid = next(iter(vpcs))
        get_att = cidr["Fn::GetAtt"]
        assert get_att[0] == vpc_lid and get_att[1] == "CidrBlock", (
            f"expected ingress CidrIp to GetAtt <VPC>.CidrBlock, got {get_att!r}"
        )


class TestStudioEfsRemovalRetain:
    """Variant fixture: ``analytics_environment.efs.removal_policy=retain``.

    With the retain override, the synthesized EFS resource must carry
    ``DeletionPolicy=Retain`` — the opt-in path operators use before a
    ``cdk destroy gco-analytics`` cycle when they need the notebook home
    directories to survive.
    """

    def test_efs_deletion_policy_is_retain_when_configured(self) -> None:
        """``efs.removal_policy="retain"`` yields ``DeletionPolicy=Retain``."""
        template = _synth_analytics(config=_AnalyticsMockConfig(efs_removal="retain"))
        fses = template.find_resources("AWS::EFS::FileSystem")
        assert len(fses) == 1
        fs = next(iter(fses.values()))
        assert fs.get("DeletionPolicy") == "Retain", (
            f"expected DeletionPolicy=Retain, got {fs.get('DeletionPolicy')!r}"
        )
        assert fs.get("UpdateReplacePolicy") == "Retain"


# ---------------------------------------------------------------------------
# SageMaker execution role
# ---------------------------------------------------------------------------


class TestSageMakerExecutionRole:
    """Assertions over ``SageMaker_Execution_Role`` and its grants.

    Identified by the documented ``AmazonSageMaker`` role-name prefix.
    Grants live in a mix of inline ``Policies`` and standalone
    ``AWS::IAM::Policy`` resources attached to the role — we inspect both.
    """

    def _find_sagemaker_role(self, template: assertions.Template) -> tuple[str, Mapping[str, Any]]:
        """Return ``(logical_id, resource)`` for the SageMaker role."""
        roles = template.find_resources("AWS::IAM::Role")
        matches = [
            (lid, res)
            for lid, res in roles.items()
            if isinstance(res["Properties"].get("RoleName"), str)
            and res["Properties"]["RoleName"].startswith("AmazonSageMaker")
        ]
        assert len(matches) == 1, (
            f"expected exactly one AmazonSageMaker*-named role, got {len(matches)}"
        )
        return matches[0]

    def _collect_role_statements(
        self,
        template: assertions.Template,
        role_logical_id: str,
    ) -> list[dict[str, Any]]:
        """Return every ``Statement`` attached to the role, across inline
        ``Policies`` and standalone ``AWS::IAM::Policy`` resources."""
        _, role = self._find_sagemaker_role(template)
        statements: list[dict[str, Any]] = []
        for pol in role["Properties"].get("Policies") or []:
            doc = pol.get("PolicyDocument") or {}
            statements.extend(doc.get("Statement") or [])

        # Standalone policies attached by Ref to the role.
        managed = template.find_resources("AWS::IAM::Policy")
        for res in managed.values():
            roles_prop = res["Properties"].get("Roles") or []
            if any(isinstance(r, dict) and r.get("Ref") == role_logical_id for r in roles_prop):
                doc = res["Properties"].get("PolicyDocument") or {}
                statements.extend(doc.get("Statement") or [])
        return statements

    def test_role_name_prefix(self) -> None:
        """The role name begins with ``AmazonSageMaker``."""
        template = _synth_analytics()
        _, role = self._find_sagemaker_role(template)
        name = role["Properties"]["RoleName"]
        assert isinstance(name, str) and name.startswith("AmazonSageMaker"), (
            f"expected RoleName to start with AmazonSageMaker, got {name!r}"
        )

    def test_role_has_rw_grant_on_cluster_shared_bucket_arn_token(self) -> None:
        """Inline policy grants ``s3:PutObject|GetObject|...`` on a
        resource token that ``Fn::GetAtt``s the
        ``ReadClusterSharedBucketArn`` AwsCustomResource's ``Parameter.Value``.

        The bucket ARN is resolved at synth time via a cross-region SSM
        read, not a literal; we assert on the Fn::GetAtt shape so the test
        keeps working regardless of the resolved bucket name.
        """
        template = _synth_analytics()
        role_lid, _ = self._find_sagemaker_role(template)
        statements = self._collect_role_statements(template, role_lid)

        # The grant's S3 statement includes all five documented actions.
        expected_actions = {
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject",
            "s3:ListBucket",
            "s3:GetBucketLocation",
        }
        matching = []
        for stmt in statements:
            actions = stmt.get("Action")
            action_set = set(actions) if isinstance(actions, list) else {actions}
            if stmt.get("Effect") != "Allow":
                continue
            if not expected_actions.issubset(action_set):
                continue
            # Resources must include a Fn::GetAtt referencing the
            # ReadClusterSharedBucketArn custom resource's Parameter.Value.
            resources = stmt.get("Resource") or []
            if not isinstance(resources, list):
                resources = [resources]
            refs_custom_resource = False
            for res in resources:
                if isinstance(res, dict) and "Fn::GetAtt" in res:
                    target = res["Fn::GetAtt"]
                    if (
                        isinstance(target, list)
                        and len(target) == 2
                        and str(target[0]).startswith("ReadClusterSharedBucketArn")
                        and target[1] == "Parameter.Value"
                    ):
                        refs_custom_resource = True
                        break
                if isinstance(res, dict) and "Fn::Join" in res:
                    # <arn>/* join — inspect its parts.
                    parts = res["Fn::Join"][1]
                    for part in parts:
                        if isinstance(part, dict) and "Fn::GetAtt" in part:
                            target = part["Fn::GetAtt"]
                            if (
                                isinstance(target, list)
                                and len(target) == 2
                                and str(target[0]).startswith("ReadClusterSharedBucketArn")
                                and target[1] == "Parameter.Value"
                            ):
                                refs_custom_resource = True
                                break
            if refs_custom_resource:
                matching.append(stmt)

        assert matching, (
            "expected an Allow statement granting s3:GetObject|PutObject|"
            "DeleteObject|ListBucket|GetBucketLocation on the "
            "ReadClusterSharedBucketArn.Parameter.Value token; "
            f"attached statements={statements!r}"
        )

    def test_cluster_shared_kms_statement_is_limited_to_s3_and_the_bucket(self) -> None:
        """The cluster-shared KMS statement carries the regional job role's conditions.

        The key lives in the global stack, so the statement is ``Resource: *``;
        ``kms:ViaService`` limits it to S3 in the global Region and
        ``kms:EncryptionContext:aws:s3:arn`` to the bucket (with an S3 Bucket
        Key) or its objects (without one), as ``s3_bucket_kms_conditions``
        builds them for the regional stack.
        """
        template = _synth_analytics()
        role_lid, _ = self._find_sagemaker_role(template)
        statements = self._collect_role_statements(template, role_lid)
        (kms,) = [
            s for s in statements if sorted(s["Action"]) == ["kms:Decrypt", "kms:GenerateDataKey"]
        ]
        assert kms["Resource"] == "*"
        (reader_id,) = [
            lid
            for lid, resource in template.to_json()["Resources"].items()
            if lid.startswith("ReadClusterSharedBucketArn") and resource["Type"] == "Custom::AWS"
        ]
        shared = {"Fn::GetAtt": [reader_id, "Parameter.Value"]}
        via_service = kms["Condition"]["StringEquals"]["kms:ViaService"]
        assert via_service == {"Fn::Join": ["", ["s3.us-east-2.", {"Ref": "AWS::URLSuffix"}]]}
        assert kms["Condition"]["StringLike"] == {
            "kms:EncryptionContext:aws:s3:arn": [shared, {"Fn::Join": ["", [shared, "/*"]]}]
        }

    def test_api_document_describes_the_deployed_presigned_url_function(self) -> None:
        """The OpenAPI document reports the function this stack deploys.

        ``scripts/generate_api_gateway_openapi.py`` synthesizes the API stack
        with an inline stand-in for this function, and the document reports the
        stand-in's runtime, handler, timeout and memory as the backend's. They
        once came from the stand-in alone, so ``/studio/login`` read
        ``python3.13`` / ``index.handler`` while the function ran on
        ``LAMBDA_PYTHON_RUNTIME`` with ``handler.lambda_handler``.
        """
        resources = _synth_analytics().to_json()["Resources"]
        (function,) = [
            resource["Properties"]
            for logical_id, resource in resources.items()
            if resource["Type"] == "AWS::Lambda::Function"
            and logical_id.startswith("PresignedUrlFunction")
        ]
        document = json.loads(
            (_REPO_ROOT / "docs" / "openapi" / "api-gateway-global.json").read_text(
                encoding="utf-8"
            )
        )
        backends = [
            operation["x-gco-backend"]["lambda"]
            for operations in document["paths"].values()
            for operation in operations.values()
            if isinstance(operation, dict)
            and (operation.get("x-gco-backend") or {}).get("lambda", {}).get("name")
            == "analytics-presigned-url"
        ]
        assert backends, "no route of the global API names the presigned-URL function"
        for backend in backends:
            assert backend["runtime"] == function["Runtime"]
            assert backend["handler"] == function["Handler"]
            assert backend["timeoutSeconds"] == function["Timeout"]
            assert backend["memorySizeMb"] == function["MemorySize"]

    def test_presigned_url_role_acts_only_on_this_studio_domain(self) -> None:
        """The presigned-URL role's SageMaker grant names this stack's domain.

        The handler reads the domain id from ``STUDIO_DOMAIN_ID`` and never
        lists domains, so ``ListDomains`` on ``*`` and the ``domain/*`` and
        ``user-profile/*/*`` wildcards are gone; ``DeleteUserProfile`` is
        granted because the handler replaces a ``Failed`` profile.
        """
        resources = _synth_analytics().to_json()["Resources"]
        (role_id,) = [
            logical_id
            for logical_id, resource in resources.items()
            if resource["Type"] == "AWS::IAM::Role"
            and logical_id.startswith("PresignedUrlLambdaRole")
        ]
        statements = [
            statement
            for resource in resources.values()
            if resource["Type"] == "AWS::IAM::Policy"
            and {"Ref": role_id} in resource["Properties"]["Roles"]
            for statement in resource["Properties"]["PolicyDocument"]["Statement"]
        ]
        (sagemaker,) = [
            statement
            for statement in statements
            if any(str(action).startswith("sagemaker:") for action in statement["Action"])
        ]
        assert "sagemaker:DeleteUserProfile" in sagemaker["Action"]
        assert "sagemaker:ListDomains" not in json.dumps(statements)
        domain = {"Fn::GetAtt": ["StudioDomain", "DomainId"]}
        rendered = json.dumps(sagemaker["Resource"])
        assert rendered.count(json.dumps(domain)) == 2, rendered
        assert ":domain/*" not in rendered and ":user-profile/*/*" not in rendered
        wildcard_actions = {
            action
            for statement in statements
            if statement["Resource"] == "*"
            for action in (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
        }
        assert wildcard_actions == {"xray:PutTraceSegments", "xray:PutTelemetryRecords"}

    def test_cluster_shared_arn_reader_reads_only_its_parameter(self) -> None:
        """The cross-Region SSM reader's policy names the one parameter it reads.

        It used ``AwsCustomResourcePolicy.ANY_RESOURCE``, which let the
        custom-resource provider read every SSM parameter in every Region.
        """
        template = _synth_analytics().to_json()
        statements = [
            statement
            for logical_id, resource in template["Resources"].items()
            if resource["Type"] == "AWS::IAM::Policy"
            and logical_id.startswith("ReadClusterSharedBucketArnCustomResourcePolicy")
            for statement in resource["Properties"]["PolicyDocument"]["Statement"]
        ]
        assert statements == [
            {
                "Action": "ssm:GetParameter",
                "Effect": "Allow",
                "Resource": {
                    "Fn::Join": [
                        "",
                        [
                            "arn:",
                            {"Ref": "AWS::Partition"},
                            ":ssm:us-east-2:123456789012:parameter/gco-test/cluster-shared-bucket/arn",
                        ],
                    ]
                },
            }
        ]

    def test_role_has_execute_api_invoke_on_api_v1_and_inference(self) -> None:
        """Full-method ``execute-api:Invoke`` grant on ``/prod/*/api/v1/*``
        and ``/prod/*/inference/*`` for the API gateway region/account.

        Notebooks need POST/PUT/DELETE in addition to GET so analytics
        users can submit jobs, update templates, and manage inference
        endpoints from inside a notebook. The resource ARNs keep the
        HTTP-method segment as ``*`` instead of pinning GET.
        """
        template = _synth_analytics()
        role_lid, _ = self._find_sagemaker_role(template)
        statements = self._collect_role_statements(template, role_lid)

        api_v1_ok = False
        inference_ok = False
        for stmt in statements:
            if stmt.get("Effect") != "Allow":
                continue
            actions = stmt.get("Action")
            action_set = set(actions) if isinstance(actions, list) else {actions}
            if "execute-api:Invoke" not in action_set:
                continue
            resources = stmt.get("Resource") or []
            if not isinstance(resources, list):
                resources = [resources]
            for res in resources:
                rendered = res if isinstance(res, str) else json.dumps(res, sort_keys=True)
                if "/prod/*/api/v1/*" in rendered:
                    api_v1_ok = True
                if "/prod/*/inference/*" in rendered:
                    inference_ok = True

        assert api_v1_ok, (
            "expected execute-api:Invoke on <api-arn>/prod/*/api/v1/* "
            f"(all HTTP methods); attached statements={statements!r}"
        )
        assert inference_ok, (
            "expected execute-api:Invoke on <api-arn>/prod/*/inference/* "
            f"(all HTTP methods); attached statements={statements!r}"
        )

        # And, critically, *none* of the execute-api grants may pin the
        # method segment to GET — that would silently re-break POST
        # submissions. This guard catches future refactors that
        # accidentally re-introduce the old GET-only scope.
        for stmt in statements:
            if stmt.get("Effect") != "Allow":
                continue
            actions = stmt.get("Action")
            action_set = set(actions) if isinstance(actions, list) else {actions}
            if "execute-api:Invoke" not in action_set:
                continue
            resources = stmt.get("Resource") or []
            if not isinstance(resources, list):
                resources = [resources]
            for res in resources:
                rendered = res if isinstance(res, str) else json.dumps(res, sort_keys=True)
                assert "/prod/GET/" not in rendered, (
                    "execute-api grant must not pin the HTTP method "
                    f"to GET — got {res!r}; blocks POST submissions"
                )

    @staticmethod
    def _granted(statements: list[dict], action: str) -> list[str]:
        """Rendered resources of every Allow statement that grants ``action``."""
        resources: list[str] = []
        for stmt in statements:
            actions = stmt.get("Action")
            action_set = set(actions) if isinstance(actions, list) else {actions}
            if stmt.get("Effect") == "Allow" and action in action_set:
                granted = stmt.get("Resource") or []
                for res in granted if isinstance(granted, list) else [granted]:
                    resources.append(_render_arn(res))
        return resources

    def test_role_invokes_the_regional_bridge_of_every_workload_region(self) -> None:
        """A region-pinned ``gco`` call goes to that Region's regional bridge.

        ``gco jobs submit -r``, ``jobs list -r``, ``jobs get`` and ``jobs logs``
        send their requests to ``<project>-regional-api-<region>``, which
        admits a caller on its identity policy alone. The grant used to name
        the API Gateway Region only, so from a Studio terminal every one of
        them was refused in a workload Region.
        """
        template = _synth_analytics()
        role_lid, _ = self._find_sagemaker_role(template)
        statements = self._collect_role_statements(template, role_lid)

        assert sorted(self._granted(statements, "execute-api:Invoke")) == sorted(
            f"arn:aws:execute-api:{region}:123456789012:*/prod/*/{tree}/*"
            for region in ("us-east-2", "us-east-1", "us-west-2")
            for tree in ("api/v1", "inference")
        )

    def test_regional_bridge_grant_names_a_shared_region_once(self) -> None:
        """An API Gateway Region that is also a workload Region is listed once."""
        config = _AnalyticsMockConfig(region="us-east-1", workload_regions=("us-east-1",))
        template = _synth_analytics(config=config)
        role_lid, _ = self._find_sagemaker_role(template)
        statements = self._collect_role_statements(template, role_lid)

        assert self._granted(statements, "execute-api:Invoke") == [
            "arn:aws:execute-api:us-east-1:123456789012:*/prod/*/api/v1/*",
            "arn:aws:execute-api:us-east-1:123456789012:*/prod/*/inference/*",
        ]

    def test_role_reads_this_projects_stacks_for_the_cli(self) -> None:
        """The ``gco`` CLI reads the API endpoints from stack outputs.

        Without ``cloudformation:DescribeStacks`` the CLI fails in a Studio
        terminal before it sends any request (``GCOAWSClient.get_api_endpoint``
        and ``get_regional_api_endpoint``). The grant is read-only and names
        this project's stacks only.
        """
        template = _synth_analytics()
        role_lid, _ = self._find_sagemaker_role(template)
        statements = self._collect_role_statements(template, role_lid)

        describe = [
            stmt
            for stmt in statements
            if "cloudformation:DescribeStacks"
            in (stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]])
        ]
        assert len(describe) == 1
        assert describe[0]["Action"] == "cloudformation:DescribeStacks"
        assert self._granted(statements, "cloudformation:DescribeStacks") == [
            "arn:aws:cloudformation:*:123456789012:stack/gco-test-*/*"
        ]

    @staticmethod
    def _sqs_send_resources(statements: list[dict]) -> list[Any]:
        resources: list[Any] = []
        for stmt in statements:
            actions = stmt.get("Action")
            action_set = set(actions) if isinstance(actions, list) else {actions}
            if stmt.get("Effect") == "Allow" and "sqs:SendMessage" in action_set:
                granted = stmt.get("Resource") or []
                resources.extend(granted if isinstance(granted, list) else [granted])
        return resources

    def test_role_sends_only_to_each_workload_regions_job_queue(self) -> None:
        """``sqs:SendMessage`` names each workload Region's job queue.

        It used to be ``sqs:*:<account>:<project>-jobs-*``, which also
        matched every Region's dead-letter queue
        (``<project>-jobs-dlq-<region>``), broader than the job queues the
        role needs.
        """
        template = _synth_analytics()
        role_lid, _ = self._find_sagemaker_role(template)
        resources = self._sqs_send_resources(self._collect_role_statements(template, role_lid))
        assert resources == [
            {
                "Fn::Join": [
                    "",
                    [
                        "arn:",
                        {"Ref": "AWS::Partition"},
                        f":sqs:{region}:123456789012:gco-test-jobs-{region}",
                    ],
                ]
            }
            for region in ("us-east-1", "us-west-2")
        ]

    def test_role_has_no_sqs_grant_without_workload_regions(self) -> None:
        """With no workload Region there is no queue, so no statement (an
        empty Resource list would be an invalid policy)."""
        template = _synth_analytics(config=_AnalyticsMockConfig(workload_regions=()))
        role_lid, _ = self._find_sagemaker_role(template)
        assert self._sqs_send_resources(self._collect_role_statements(template, role_lid)) == []

    def test_role_reads_exactly_the_three_cluster_shared_parameters(self) -> None:
        """``ssm:GetParameter`` on the three ``Cluster_Shared_Bucket`` metadata
        parameters in the global region, and on nothing else.

        Lets a Studio notebook resolve the shared bucket name/arn/region at
        runtime without a per-user JupyterLab terminal export step.
        ``GCOGlobalStack`` writes exactly ``<prefix>/name``, ``/arn`` and
        ``/region`` under ``/<project_name>/cluster-shared-bucket``; the grant
        names those three instead of the ``<prefix>/*`` path. The fixture's
        ``project_name`` is ``"gco-test"``.
        """
        from gco.stacks.constants import cluster_shared_ssm_parameter_prefix

        prefix = cluster_shared_ssm_parameter_prefix("gco-test")
        template = _synth_analytics()
        role_lid, _ = self._find_sagemaker_role(template)
        statements = self._collect_role_statements(template, role_lid)
        (ssm_statement,) = [
            stmt
            for stmt in statements
            if "ssm:GetParameter"
            in (stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]])
        ]
        assert sorted(ssm_statement["Action"]) == ["ssm:GetParameter", "ssm:GetParameters"]
        assert sorted(_render_arn(arn) for arn in ssm_statement["Resource"]) == [
            f"arn:aws:ssm:us-east-2:123456789012:parameter{prefix}/{name}"
            for name in ("arn", "name", "region")
        ]

    def test_studio_ui_actions_name_this_domain(self) -> None:
        """The Studio UI statement names this stack's domain, not every domain.

        Domain, user-profile, space and app ARNs all start with the domain id,
        so the ``domain/*``, ``user-profile/*/*``, ``space/*/*`` and
        ``app/*/*/*/*`` wildcards are scoped to it. ``ListSpaces`` and
        ``ListApps`` take no resource, so a statement naming ARNs never granted
        them; ``AmazonSageMakerFullAccess`` grants them on ``*``.
        """
        template = _synth_analytics()
        role_lid, _ = self._find_sagemaker_role(template)
        statements = self._collect_role_statements(template, role_lid)

        def _actions(statement: Mapping[str, Any]) -> list[str]:
            action = statement["Action"]
            return action if isinstance(action, list) else [action]

        (studio,) = [s for s in statements if "sagemaker:CreateSpace" in _actions(s)]
        assert "sagemaker:ListSpaces" not in _actions(studio)
        assert "sagemaker:ListApps" not in _actions(studio)
        rendered = sorted(_render_arn(arn) for arn in studio["Resource"])
        arn_prefix = "arn:aws:sagemaker:us-east-2:123456789012"
        assert rendered == sorted(
            arn_prefix + suffix
            for suffix in (
                ":domain/<domain>",
                ":user-profile/<domain>/*",
                ":space/<domain>/*",
                ":app/<domain>/*/*/*",
            )
        )
        dumped = json.dumps(statements)
        for wildcard in (":domain/*", ":user-profile/*/*", ":space/*/*", ":app/*/*/*/*"):
            assert wildcard not in dumped
        managed = json.dumps(
            template.find_resources("AWS::IAM::Role")[role_lid]["Properties"]["ManagedPolicyArns"]
        )
        assert "AmazonSageMakerFullAccess" in managed

    @pytest.mark.parametrize("region", ["cn-north-1", "us-gov-west-1"])
    def test_noncommercial_policies_retain_partition_and_url_suffix_tokens(
        self,
        region: str,
    ) -> None:
        """China and GovCloud synths keep both CloudFormation pseudo-parameters."""
        template = _synth_analytics(
            construct_id=f"test-analytics-{region}",
            config=_AnalyticsMockConfig(region=region),
        )
        role_lid, _ = self._find_sagemaker_role(template)
        rendered = json.dumps(
            self._collect_role_statements(template, role_lid),
            sort_keys=True,
        )

        assert "AWS::Partition" in rendered
        assert "AWS::URLSuffix" in rendered
        assert "arn:aws:sagemaker" not in rendered


# ---------------------------------------------------------------------------
# Cognito user pool
# ---------------------------------------------------------------------------


class TestCognitoUserPool:
    """Assertions over the Cognito user pool that fronts SageMaker Studio.

    Covers the password-policy floor, the standard-threat-protection-mode
    setting (set to ``NO_ENFORCEMENT`` on Lite — the default tier — which
    is what CDK renders as ``UserPoolAddOns.AdvancedSecurityMode=OFF`` on
    the CloudFormation side), and the default destroy-on-teardown posture.
    """

    def test_exactly_one_user_pool_is_created(self) -> None:
        template = _synth_analytics()
        template.resource_count_is("AWS::Cognito::UserPool", 1)

    @pytest.mark.parametrize("removal", ["destroy", "retain"])
    def test_user_pool_carries_the_tags_the_doctor_orphan_scan_matches(self, removal: str) -> None:
        """A retained pool is found by its tags, even after CloudFormation forgets the stack."""
        from cli.analytics_user_mgmt import _managed_by_project

        template = _synth_analytics(config=_AnalyticsMockConfig(cognito_removal=removal))
        pool = next(iter(template.find_resources("AWS::Cognito::UserPool").values()))
        tags = pool["Properties"]["UserPoolTags"]
        assert tags == {"gco:analytics:managed": "true", "gco:project": "gco-test"}
        assert _managed_by_project(tags, "gco-test")

    def test_standard_threat_protection_mode_is_no_enforcement(self) -> None:
        """``UserPoolAddOns.AdvancedSecurityMode=OFF``.

        The stack uses the replacement kwarg
        ``standard_threat_protection_mode=NO_ENFORCEMENT`` (the previous
        ``advanced_security_mode`` kwarg is deprecated in aws-cdk-lib as
        of the Cognito November 2024 tier changes). CDK still renders the
        setting on the CloudFormation side as
        ``UserPoolAddOns.AdvancedSecurityMode`` — the old property name —
        with value ``"OFF"`` for NO_ENFORCEMENT. Operators who want real
        threat protection must opt into the Essentials or Plus feature
        plan and flip the mode to FULL_FUNCTION — see the comment in
        ``_create_cognito_pool``.

        We accept either the nested form or a hypothetical top-level form
        so the test survives future CDK repackaging of the property.
        """
        template = _synth_analytics()
        pools = template.find_resources("AWS::Cognito::UserPool")
        assert len(pools) == 1
        pool = next(iter(pools.values()))
        props = pool["Properties"]

        addons_mode = (props.get("UserPoolAddOns") or {}).get("AdvancedSecurityMode")
        top_level_mode = props.get("AdvancedSecurityMode")
        mode = addons_mode or top_level_mode
        assert mode == "OFF", (
            f"expected AdvancedSecurityMode=OFF (Lite tier + "
            f"NO_ENFORCEMENT), got addons={addons_mode!r}, "
            f"top_level={top_level_mode!r}"
        )

    def test_pool_does_not_enable_enforced_threat_protection(self) -> None:
        """Lite tier + NO_ENFORCEMENT means no ``ENFORCED`` or
        ``AUDIT`` value is ever synthesized for the add-ons.

        Belt-and-suspenders companion to the positive test above — if a
        future refactor flips the mode back to FULL_FUNCTION this test
        fails fast rather than letting the regression slip through.
        """
        template = _synth_analytics()
        pools = template.find_resources("AWS::Cognito::UserPool")
        assert len(pools) == 1
        pool = next(iter(pools.values()))
        props = pool["Properties"]

        addons_mode = (props.get("UserPoolAddOns") or {}).get("AdvancedSecurityMode")
        top_level_mode = props.get("AdvancedSecurityMode")
        for label, value in (
            ("UserPoolAddOns.AdvancedSecurityMode", addons_mode),
            ("AdvancedSecurityMode", top_level_mode),
        ):
            if value is None:
                continue
            assert value not in {"ENFORCED", "AUDIT"}, (
                f"{label}={value!r} — Lite tier does not support this, "
                "and enabling it silently would incur the Essentials/Plus "
                "per-MAU cost we documented against"
            )

    def test_password_policy_matches_documented_floor(self) -> None:
        """Password policy enforces the documented minimums."""
        template = _synth_analytics()
        pools = template.find_resources("AWS::Cognito::UserPool")
        assert len(pools) == 1
        pool = next(iter(pools.values()))
        password_policy = (pool["Properties"].get("Policies") or {}).get("PasswordPolicy")
        assert isinstance(password_policy, dict), (
            f"expected Policies.PasswordPolicy on the user pool, got {password_policy!r}"
        )
        assert password_policy.get("MinimumLength") == 12
        assert password_policy.get("RequireNumbers") is True
        assert password_policy.get("RequireSymbols") is True
        assert password_policy.get("RequireUppercase") is True

    def test_user_pool_deletion_policy_is_delete_by_default(self) -> None:
        """Default ``cognito_removal="destroy"`` yields
        ``DeletionPolicy=Delete`` on the user pool."""
        template = _synth_analytics()
        pools = template.find_resources("AWS::Cognito::UserPool")
        assert len(pools) == 1
        pool = next(iter(pools.values()))
        assert pool.get("DeletionPolicy") == "Delete", (
            f"expected DeletionPolicy=Delete, got {pool.get('DeletionPolicy')!r}"
        )
        assert pool.get("UpdateReplacePolicy") == "Delete"


class TestCognitoRemovalRetain:
    """Variant fixture: ``analytics_environment.cognito.removal_policy=retain``.

    With the retain override, ``DeletionPolicy=Retain`` on the user pool —
    operators opt into this to preserve registered usernames across a
    ``cdk destroy gco-analytics`` cycle.
    """

    def test_user_pool_deletion_policy_is_retain_when_configured(self) -> None:
        """``cognito.removal_policy="retain"`` yields ``DeletionPolicy=Retain``."""
        template = _synth_analytics(config=_AnalyticsMockConfig(cognito_removal="retain"))
        pools = template.find_resources("AWS::Cognito::UserPool")
        assert len(pools) == 1
        pool = next(iter(pools.values()))
        assert pool.get("DeletionPolicy") == "Retain", (
            f"expected DeletionPolicy=Retain, got {pool.get('DeletionPolicy')!r}"
        )
        assert pool.get("UpdateReplacePolicy") == "Retain"


# ---------------------------------------------------------------------------
# EMR Serverless application
# ---------------------------------------------------------------------------


class TestEmrServerlessApp:
    """Assertions over the EMR Serverless Spark application."""

    def test_exactly_one_emr_application_is_created(self) -> None:
        template = _synth_analytics()
        template.resource_count_is("AWS::EMRServerless::Application", 1)

    def test_emr_app_is_spark_with_pinned_release_label(self) -> None:
        """``Type=SPARK`` and ``ReleaseLabel`` matches the pinned
        constant ``EMR_SERVERLESS_RELEASE_LABEL`` from ``gco.stacks.constants``."""
        # Import here so the module-level test discovery doesn't blow up if
        # the constant is relocated — the test still fails cleanly with a
        # descriptive ImportError on name changes.
        from gco.stacks.constants import EMR_SERVERLESS_RELEASE_LABEL

        template = _synth_analytics()
        apps = template.find_resources("AWS::EMRServerless::Application")
        assert len(apps) == 1
        app = next(iter(apps.values()))
        props = app["Properties"]
        assert props.get("Type") == "SPARK", f"expected Type=SPARK, got {props!r}"
        assert props.get("ReleaseLabel") == EMR_SERVERLESS_RELEASE_LABEL, (
            f"expected ReleaseLabel={EMR_SERVERLESS_RELEASE_LABEL!r}, "
            f"got {props.get('ReleaseLabel')!r}"
        )

    def test_emr_app_network_config_uses_private_subnets(self) -> None:
        """``NetworkConfiguration.SubnetIds`` references at least one
        private subnet from the analytics VPC via ``Ref``."""
        template = _synth_analytics()
        apps = template.find_resources("AWS::EMRServerless::Application")
        assert len(apps) == 1
        app = next(iter(apps.values()))

        network = app["Properties"].get("NetworkConfiguration") or {}
        subnet_refs = network.get("SubnetIds") or []
        assert subnet_refs, (
            f"expected NetworkConfiguration.SubnetIds on the EMR app, got {network!r}"
        )

        # Collect every subnet Ref and verify it resolves to one of the
        # private subnets (Private or Isolated).
        all_subnets = template.find_resources("AWS::EC2::Subnet")
        private_lids: set[str] = set()
        for lid, res in all_subnets.items():
            tags = res["Properties"].get("Tags") or []
            for tag in tags:
                if tag.get("Key") == "aws-cdk:subnet-type" and tag.get("Value") in (
                    "Isolated",
                    "Private",
                ):
                    private_lids.add(lid)
                    break
        assert private_lids, (
            "expected at least one private subnet in the template; "
            f"all subnets={list(all_subnets)!r}"
        )

        referenced_lids = {
            ref["Ref"] for ref in subnet_refs if isinstance(ref, dict) and "Ref" in ref
        }
        assert referenced_lids & private_lids, (
            f"expected EMR NetworkConfiguration.SubnetIds to reference at "
            f"least one private subnet; got "
            f"refs={referenced_lids!r}, private={private_lids!r}"
        )


# ---------------------------------------------------------------------------
# Canvas sub-toggle
# ---------------------------------------------------------------------------


class TestCanvasDisabled:
    """Default fixture: ``analytics_environment.canvas.enabled=false``.

    The synthesized template must omit the Canvas managed-policy
    attachment and the ``DefaultUserSettings.CanvasAppSettings`` block
    on the Studio domain. Canvas is opt-in — leaving the toggle off is
    the common case.
    """

    def test_canvas_full_access_not_attached(self) -> None:
        """No IAM::Role has ``AmazonSageMakerCanvasFullAccess`` in its
        ``ManagedPolicyArns``."""
        template = _synth_analytics()
        roles = template.find_resources("AWS::IAM::Role")
        for role in roles.values():
            arns = role.get("Properties", {}).get("ManagedPolicyArns", []) or []
            for entry in arns:
                if isinstance(entry, dict) and "Fn::Join" in entry:
                    # Managed-policy ARNs are rendered as Fn::Join; flatten
                    # the join arguments to a single string for matching.
                    flat = "".join(
                        str(piece) for piece in entry["Fn::Join"][1] if isinstance(piece, str)
                    )
                    assert "AmazonSageMakerCanvasFullAccess" not in flat, (
                        "Canvas managed policy must not be attached when canvas.enabled=false."
                    )
                elif isinstance(entry, str):
                    assert "AmazonSageMakerCanvasFullAccess" not in entry

    def test_canvas_app_settings_absent_on_domain(self) -> None:
        """``DefaultUserSettings.CanvasAppSettings`` must be absent from
        the Studio domain when the toggle is off."""
        template = _synth_analytics()
        domains = template.find_resources("AWS::SageMaker::Domain")
        assert domains, "expected a Studio domain in the template"
        for domain in domains.values():
            default_user_settings = domain["Properties"].get("DefaultUserSettings", {})
            assert "CanvasAppSettings" not in default_user_settings, (
                "CanvasAppSettings must not appear when canvas.enabled=false; "
                f"got {default_user_settings!r}"
            )


class TestCanvasEnabled:
    """Variant fixture: ``analytics_environment.canvas.enabled=true``.

    The Canvas managed policy must be attached to ``SageMaker_Execution_Role``.
    We do **not** inject ``DefaultUserSettings.CanvasAppSettings`` on the
    domain because CloudFormation's ``AWS::SageMaker::Domain`` resource
    rejects that property (so does ``AWS::SageMaker::UserProfile``; only
    the SageMaker API's user settings carry it). Canvas is still
    discoverable from Studio purely via IAM — the managed policy
    attachment is sufficient to surface the tile.
    """

    @staticmethod
    def _synth() -> assertions.Template:
        return _synth_analytics(
            config=_AnalyticsMockConfig(canvas_enabled=True),
        )

    def test_canvas_full_access_attached_to_execution_role(self) -> None:
        """``SageMaker_Execution_Role`` has ``AmazonSageMakerCanvasFullAccess``
        on its ``ManagedPolicyArns``."""
        template = self._synth()
        roles = template.find_resources("AWS::IAM::Role")
        matched = False
        for role in roles.values():
            role_name = role.get("Properties", {}).get("RoleName", "")
            if not (isinstance(role_name, str) and role_name.startswith("AmazonSageMaker")):
                continue
            for entry in role["Properties"].get("ManagedPolicyArns", []) or []:
                if isinstance(entry, dict) and "Fn::Join" in entry:
                    flat = "".join(
                        str(piece) for piece in entry["Fn::Join"][1] if isinstance(piece, str)
                    )
                    if "AmazonSageMakerCanvasFullAccess" in flat:
                        matched = True
                        break
        assert matched, (
            "Expected SageMaker_Execution_Role to have "
            "AmazonSageMakerCanvasFullAccess in ManagedPolicyArns when "
            "canvas.enabled=true"
        )

    def test_domain_does_not_carry_canvas_app_settings(self) -> None:
        """Regression guard: ``CanvasAppSettings`` must NEVER appear on
        ``AWS::SageMaker::Domain``.

        The CloudFormation schema rejects the property at the domain
        level (``Unsupported property [CanvasAppSettings]``) with an
        early-validation error that fails ``cdk deploy`` before any
        resources are created. Keeping this assertion guards against a
        future refactor that re-introduces the domain-level override
        we tried (and reverted) during the Canvas rollout.
        """
        template = self._synth()
        domains = template.find_resources("AWS::SageMaker::Domain")
        assert domains, "expected a Studio domain"
        for domain in domains.values():
            default_user_settings = domain["Properties"].get("DefaultUserSettings", {})
            assert "CanvasAppSettings" not in default_user_settings, (
                "CanvasAppSettings is not a valid property on "
                "AWS::SageMaker::Domain (only the SageMaker API takes it). "
                f"Got {default_user_settings!r}"
            )


# ---------------------------------------------------------------------------
# MLflow (always-on, not gated)
# ---------------------------------------------------------------------------


def _flatten_cfn(value: object) -> str:
    """Flatten a CloudFormation intrinsic-function tree to a single string.

    Used by the Canvas and MLflow tests to assert on resource ARNs and
    other interpolated properties rendered as nested ``Fn::Join`` /
    ``Fn::Select`` / ``Fn::Split`` calls in the synthesized template.
    We care only about the string prefixes/suffixes so any dict leaf
    collapses to an empty string.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_flatten_cfn(item) for item in value)
    if isinstance(value, dict):
        return "".join(_flatten_cfn(v) for v in value.values())
    return ""


class TestMlflowPermissions:
    """Whenever analytics is enabled, the SageMaker execution role must:

    1. Have the AWS-managed ``AmazonSageMakerFullAccess`` attached so
       MLflow Apps (``CreateMlflowApp`` / ``ListMlflowApps`` /
       ``DescribeMlflowApp``) and MLflow Tracking Servers work from
       Studio without inline action enumeration.
    2. Have an inline ``sagemaker-mlflow:*`` statement scoped to
       ``mlflow-tracking-server/*`` and ``mlflow-app/*`` ARNs in the
       api-gateway region. The managed policy covers the
       ``sagemaker:*`` control-plane namespace but not the
       ``sagemaker-mlflow`` data-plane prefix the MLflow SDK's SigV4
       plug-in uses.
    """

    def test_full_access_managed_policy_attached(self) -> None:
        template = _synth_analytics()
        roles = template.find_resources("AWS::IAM::Role")
        matched = False
        for role in roles.values():
            role_name = role.get("Properties", {}).get("RoleName", "")
            if not (isinstance(role_name, str) and role_name.startswith("AmazonSageMaker")):
                continue
            for entry in role["Properties"].get("ManagedPolicyArns", []) or []:
                if isinstance(entry, dict) and "Fn::Join" in entry:
                    flat = "".join(
                        str(piece) for piece in entry["Fn::Join"][1] if isinstance(piece, str)
                    )
                    if "AmazonSageMakerFullAccess" in flat:
                        matched = True
                        break
                elif isinstance(entry, str) and "AmazonSageMakerFullAccess" in entry:
                    matched = True
                    break
        assert matched, (
            "SageMaker_Execution_Role must have AmazonSageMakerFullAccess "
            "in ManagedPolicyArns so MLflow Apps / Tracking Servers work "
            "from Studio."
        )

    def test_sagemaker_mlflow_data_plane_statement_present(self) -> None:
        """A role policy must grant ``sagemaker-mlflow:*`` on
        tracking-server and MLflow-app ARN wildcards."""
        template = _synth_analytics()
        policies = template.find_resources("AWS::IAM::Policy")
        matched = False
        for policy in policies.values():
            for stmt in policy["Properties"]["PolicyDocument"].get("Statement", []):
                actions = stmt.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if "sagemaker-mlflow:*" not in actions:
                    continue
                resources = stmt.get("Resource", [])
                if isinstance(resources, str):
                    resources = [resources]
                flat = [_flatten_cfn(r) for r in resources]
                has_tracking = any("mlflow-tracking-server/*" in r for r in flat)
                has_app = any("mlflow-app/*" in r for r in flat)
                if has_tracking and has_app:
                    matched = True
                    break
            if matched:
                break
        assert matched, (
            "Expected an inline policy granting sagemaker-mlflow:* on both "
            "mlflow-tracking-server/* and mlflow-app/* ARNs (required "
            "because AmazonSageMakerFullAccess does not cover the "
            "sagemaker-mlflow service prefix)."
        )

    def test_no_enumerated_mlflow_tracking_server_actions_inline(self) -> None:
        """Regression guard: the old inline enumeration
        (``sagemaker:CreateMlflowTrackingServer``, ``sagemaker:*ModelPackage*``,
        etc.) must NOT come back — those are now covered by
        ``AmazonSageMakerFullAccess`` and duplicating them inline
        defeats the managed-policy-tracks-new-features rationale.
        """
        template = _synth_analytics()
        forbidden_actions = {
            "sagemaker:CreateMlflowTrackingServer",
            "sagemaker:DescribeMlflowTrackingServer",
            "sagemaker:ListMlflowTrackingServers",
            "sagemaker:CreateModelPackage",
            "sagemaker:CreateModelPackageGroup",
        }
        policies = template.find_resources("AWS::IAM::Policy")
        for policy in policies.values():
            for stmt in policy["Properties"]["PolicyDocument"].get("Statement", []):
                actions = stmt.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                for action in actions:
                    assert action not in forbidden_actions, (
                        f"Inline policy still enumerates {action!r}; this "
                        "action is covered by AmazonSageMakerFullAccess — "
                        "remove the inline entry."
                    )


# ---------------------------------------------------------------------------
# Custom file-system configuration (EFS-only for now)
# ---------------------------------------------------------------------------


class TestDomainCustomFileSystems:
    """The Studio domain offers ``Studio_EFS`` to its spaces as its only
    custom file system. An earlier revision also tried to mount the
    cluster-shared bucket via ``S3FileSystemConfig`` — aws-cdk-lib
    exposes the property but SageMaker Studio rejects the resource at
    create time (``S3FileSystemConfig for SageMaker AI Studio is not
    supported yet``), so we ship EFS-only and notebooks reach the
    cluster-shared bucket via ``boto3`` instead.
    """

    def test_domain_has_efs_custom_file_system(self) -> None:
        """The Studio domain lists ``Studio_EFS`` for its spaces to attach."""
        template = _synth_analytics()
        domains = template.find_resources("AWS::SageMaker::Domain")
        assert domains, "expected a Studio domain"
        efs_seen = False
        for domain in domains.values():
            configs = (
                domain["Properties"]
                .get("DefaultUserSettings", {})
                .get("CustomFileSystemConfigs", [])
                or []
            )
            for entry in configs:
                if "EFSFileSystemConfig" in entry:
                    efs_seen = True
        assert efs_seen, (
            "Expected an EFSFileSystemConfig entry on CustomFileSystemConfigs "
            "so spaces can attach Studio_EFS."
        )

    def test_domain_does_not_declare_s3_file_system_config(self) -> None:
        """Regression guard: ``S3FileSystemConfig`` must NOT appear on the
        domain. SageMaker rejects the resource at create time even though
        CloudFormation accepts it at synth time — re-introducing the
        mount would re-break the analytics stack deploy with
        ``Invalid request provided: S3FileSystemConfig for SageMaker AI
        Studio is not supported yet``.
        """
        template = _synth_analytics()
        domains = template.find_resources("AWS::SageMaker::Domain")
        assert domains
        for domain in domains.values():
            configs = (
                domain["Properties"]
                .get("DefaultUserSettings", {})
                .get("CustomFileSystemConfigs", [])
                or []
            )
            for entry in configs:
                assert "S3FileSystemConfig" not in entry, (
                    "S3FileSystemConfig is not supported on AWS::SageMaker::Domain "
                    "(the Studio service rejects it even though CFN synths it cleanly). "
                    f"Got entry: {entry!r}"
                )
