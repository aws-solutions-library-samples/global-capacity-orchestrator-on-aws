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

from collections.abc import Mapping
from typing import Any, cast

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from gco.config.config_loader import ConfigLoader
from gco.stacks.analytics_stack import GCOAnalyticsStack, _parse_removal

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
    ) -> None:
        self._enabled = enabled
        self._efs_removal = efs_removal
        self._cognito_removal = cognito_removal
        self._hyperpod_enabled = hyperpod_enabled
        self._canvas_enabled = canvas_enabled

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
        return "us-east-2"

    def get_api_gateway_region(self) -> str:
        """Return a fixed api-gateway region for IAM resource scoping.

        Section 8's ``_create_execution_role_and_grants`` uses this to
        scope the ``execute-api:Invoke`` statement on the SageMaker role.
        """
        return "us-east-2"

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
        env=cdk.Environment(account="123456789012", region="us-east-2"),
    )
    return assertions.Template.from_stack(stack)


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
        zones. ``max_azs=2`` + a single private-isolated subnet
        configuration yields exactly two subnets.
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

        The bucket name is emitted as a literal string on this stack because
        ``_synth_analytics`` fixes ``account`` and ``region`` in the
        ``cdk.Environment`` — so the template carries
        ``gco-test-analytics-studio-123456789012-us-east-2`` verbatim.
        """
        buckets = template.find_resources("AWS::S3::Bucket")
        matches = [
            (lid, res)
            for lid, res in buckets.items()
            if isinstance(res["Properties"].get("BucketName"), str)
            and res["Properties"]["BucketName"].startswith("gco-test-analytics-studio-")
        ]
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

    def test_studio_only_bucket_name_prefix(self) -> None:
        """Bucket name starts with ``gco-test-analytics-studio-``."""
        template = _synth_analytics()
        _, bucket = self._find_studio_only_bucket(template)
        name = bucket["Properties"].get("BucketName")
        assert isinstance(name, str) and name.startswith("gco-test-analytics-studio-"), (
            f"expected BucketName to start with gco-test-analytics-studio-, got {name!r}"
        )

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
                if not isinstance(res, str):
                    continue
                if "/prod/*/api/v1/*" in res:
                    api_v1_ok = True
                if "/prod/*/inference/*" in res:
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
                if isinstance(res, str):
                    assert "/prod/GET/" not in res, (
                        "execute-api grant must not pin the HTTP method "
                        f"to GET — got {res!r}; blocks POST submissions"
                    )

    def test_role_has_sqs_sendmessage_on_jobs_queue_pattern(self) -> None:
        """``sqs:SendMessage`` on the regional job-queue naming
        pattern ``<project>-jobs-*``."""
        template = _synth_analytics()
        role_lid, _ = self._find_sagemaker_role(template)
        statements = self._collect_role_statements(template, role_lid)

        matches = []
        for stmt in statements:
            if stmt.get("Effect") != "Allow":
                continue
            actions = stmt.get("Action")
            action_set = set(actions) if isinstance(actions, list) else {actions}
            if "sqs:SendMessage" not in action_set:
                continue
            resources = stmt.get("Resource") or []
            if not isinstance(resources, list):
                resources = [resources]
            for res in resources:
                # Mock config sets project name = "gco-test", so the
                # resolved pattern is ``arn:aws:sqs:*:<account>:gco-test-jobs-*``.
                # The general shape is ``gco-*-jobs-*`` by design — assert
                # the core of the pattern without pinning to the mock project.
                if isinstance(res, str) and res.endswith("-jobs-*") and ":sqs:" in res:
                    matches.append(stmt)
                    break

        assert matches, (
            "expected an Allow statement granting sqs:SendMessage on a "
            "resource matching the <project>-jobs-* ARN pattern; "
            f"attached statements={statements!r}"
        )

    def test_role_has_ssm_getparameter_on_cluster_shared_bucket_prefix(self) -> None:
        """``ssm:GetParameter`` on the ``Cluster_Shared_Bucket`` metadata
        parameters in the global region.

        Lets a Studio notebook resolve the shared bucket name/arn/region
        at runtime without a per-user JupyterLab terminal export step.
        The parameters live under
        ``/<project_name>/cluster-shared-bucket`` in the global region where
        ``GCOGlobalStack`` is deployed. The analytics test fixture synthesizes
        with a mock config whose ``project_name`` is ``"gco-test"``.
        """
        from gco.stacks.constants import cluster_shared_ssm_parameter_prefix

        cluster_shared_prefix = cluster_shared_ssm_parameter_prefix("gco-test")
        template = _synth_analytics()
        role_lid, _ = self._find_sagemaker_role(template)
        statements = self._collect_role_statements(template, role_lid)

        matches = []
        for stmt in statements:
            if stmt.get("Effect") != "Allow":
                continue
            actions = stmt.get("Action")
            action_set = set(actions) if isinstance(actions, list) else {actions}
            if "ssm:GetParameter" not in action_set:
                continue
            resources = stmt.get("Resource") or []
            if not isinstance(resources, list):
                resources = [resources]
            for res in resources:
                # Expected shape:
                #   arn:aws:ssm:<global-region>:<account>:parameter/gco/cluster-shared-bucket/*
                if isinstance(res, str) and (
                    ":ssm:" in res and f"parameter{cluster_shared_prefix}/*" in res
                ):
                    matches.append(stmt)
                    break

        assert matches, (
            "expected an Allow statement granting ssm:GetParameter on "
            f"parameter{cluster_shared_prefix}/* in the "
            f"global region; attached statements={statements!r}"
        )


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
    rejects that property (it only belongs on
    ``AWS::SageMaker::UserProfile``); per-user Canvas configuration, when
    we need it, will be applied by the presigned-URL Lambda at user-profile
    creation time. Canvas is still discoverable from Studio purely via IAM
    — the managed policy attachment is sufficient to surface the tile.
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
                "AWS::SageMaker::Domain — it belongs on UserProfile only. "
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
    """The Studio domain mounts the per-user EFS home folder as its only
    custom file system. An earlier revision also tried to mount the
    cluster-shared bucket via ``S3FileSystemConfig`` — aws-cdk-lib
    exposes the property but SageMaker Studio rejects the resource at
    create time (``S3FileSystemConfig for SageMaker AI Studio is not
    supported yet``), so we ship EFS-only and notebooks reach the
    cluster-shared bucket via ``boto3`` instead.
    """

    def test_domain_has_efs_custom_file_system(self) -> None:
        """The Studio domain exposes the per-user EFS mount."""
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
            "so per-user home folders on Studio_EFS work."
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
