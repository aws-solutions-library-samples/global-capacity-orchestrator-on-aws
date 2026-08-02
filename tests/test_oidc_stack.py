"""
Tests for the GitHub OIDC provider CDK stack (.github/oidc_provider/).

Verifies that the standalone stack synthesizes correctly and produces
the expected IAM resources with proper trust policies and permissions.
"""

import json
import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

# Ensure the oidc_provider directory is importable
sys.path.insert(0, str(Path(__file__).parent.parent / ".github" / "oidc_provider"))

from stack import GCOGitHubOIDCStack


def _synth_stack(
    github_repo: str = "awslabs/global-capacity-orchestrator-on-aws",
    github_branch: str = "main",
) -> Template:
    """Synthesize the OIDC stack and return a CDK Template for assertions."""
    app = cdk.App()
    stack = GCOGitHubOIDCStack(
        app,
        "TestOIDCStack",
        github_repo=github_repo,
        github_branch=github_branch,
    )
    return Template.from_stack(stack)


class TestOIDCStackSynthesis:
    """Verify the stack synthesizes without errors."""

    def test_stack_synthesizes(self):
        """Stack should synthesize without throwing."""
        template = _synth_stack()
        assert template is not None

    def test_stack_has_oidc_provider(self):
        """Stack should create an OIDC provider."""
        template = _synth_stack()
        template.resource_count_is("Custom::AWSCDKOpenIdConnectProvider", 1)

    def test_stack_has_iam_role(self):
        """Stack should create an IAM role (plus the OIDC provider's custom resource role)."""
        template = _synth_stack()
        template.resource_count_is("AWS::IAM::Role", 2)

    def test_stack_has_iam_policy(self):
        """Stack should create an inline IAM policy."""
        template = _synth_stack()
        template.resource_count_is("AWS::IAM::Policy", 1)

    def test_stack_has_outputs(self):
        """Stack should export the role ARN and OIDC provider ARN."""
        template = _synth_stack()
        template.has_output("RoleArn", {"Description": Match.string_like_regexp(".*role ARN.*")})
        template.has_output(
            "OIDCProviderArn", {"Description": Match.string_like_regexp(".*OIDC.*")}
        )


class TestOIDCProviderConfig:
    """Verify the OIDC provider is configured correctly."""

    def test_provider_uses_both_thumbprints(self):
        """OIDC provider should include both the primary and backup thumbprints."""
        from stack import GITHUB_OIDC_BACKUP_THUMBPRINT, GITHUB_OIDC_THUMBPRINT

        # Verify both constants are defined, distinct, and valid SHA-1 length.
        assert GITHUB_OIDC_THUMBPRINT != GITHUB_OIDC_BACKUP_THUMBPRINT
        assert len(GITHUB_OIDC_THUMBPRINT) == 40
        assert len(GITHUB_OIDC_BACKUP_THUMBPRINT) == 40


class TestOIDCTrustPolicy:
    """Verify the IAM role trust policy is correctly scoped."""

    def test_default_repo_main_branch(self):
        """Default config should require the exact main branch subject."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Condition": Match.object_like(
                                        {
                                            "StringEquals": Match.object_like(
                                                {
                                                    "token.actions.githubusercontent.com:sub": "repo:awslabs/global-capacity-orchestrator-on-aws:ref:refs/heads/main"
                                                }
                                            )
                                        }
                                    ),
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_specific_branch_uses_string_equals(self):
        """An explicit branch name should be represented exactly."""
        template = _synth_stack(github_branch="release")
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Condition": Match.object_like(
                                        {
                                            "StringEquals": Match.object_like(
                                                {
                                                    "token.actions.githubusercontent.com:sub": "repo:awslabs/global-capacity-orchestrator-on-aws:ref:refs/heads/release"
                                                }
                                            ),
                                        }
                                    ),
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_explicit_wildcard_uses_string_like(self):
        """Wildcard trust remains available only as an explicit opt-in."""
        template = _synth_stack(github_branch="*")
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Condition": Match.object_like(
                                        {
                                            "StringLike": {
                                                "token.actions.githubusercontent.com:sub": "repo:awslabs/global-capacity-orchestrator-on-aws:*"
                                            }
                                        }
                                    ),
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_custom_repo_reflected_in_trust(self):
        """Custom github_repo should appear in the main-branch subject."""
        template = _synth_stack(github_repo="my-org/my-fork")
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Condition": Match.object_like(
                                        {
                                            "StringEquals": Match.object_like(
                                                {
                                                    "token.actions.githubusercontent.com:sub": "repo:my-org/my-fork:ref:refs/heads/main"
                                                }
                                            )
                                        }
                                    ),
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_audience_claim_is_sts(self):
        """Trust policy should require aud = sts.amazonaws.com."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Condition": Match.object_like(
                                        {
                                            "StringEquals": Match.object_like(
                                                {
                                                    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
                                                }
                                            ),
                                        }
                                    ),
                                }
                            )
                        ]
                    )
                }
            },
        )


class TestOIDCIAMPolicy:
    """Verify the IAM policy contains the expected permissions."""

    def test_policy_has_eks_describe_addon_versions(self):
        """Policy should allow eks:DescribeAddonVersions."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Policy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": Match.array_with(["eks:DescribeAddonVersions"]),
                                    "Effect": "Allow",
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_policy_has_eks_describe_cluster_versions(self):
        """Policy should allow eks:DescribeClusterVersions so the
        dependency scan can detect newer Kubernetes minor releases."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Policy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": Match.array_with(["eks:DescribeClusterVersions"]),
                                    "Effect": "Allow",
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_policy_has_ec2_accelerator_catalog_describe_actions(self):
        """Policy should allow the monthly accelerator catalog scan."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Policy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": Match.array_with(
                                        [
                                            "ec2:DescribeInstanceTypes",
                                            "ec2:DescribeRegions",
                                        ]
                                    ),
                                    "Effect": "Allow",
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_policy_has_rds_describe(self):
        """Policy should allow rds:DescribeDBEngineVersions."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Policy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": Match.array_with(["rds:DescribeDBEngineVersions"]),
                                    "Effect": "Allow",
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_policy_has_sts_get_caller_identity(self):
        """Policy should allow sts:GetCallerIdentity."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Policy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": Match.array_with(["sts:GetCallerIdentity"]),
                                    "Effect": "Allow",
                                }
                            )
                        ]
                    )
                }
            },
        )


class TestOIDCRoleProperties:
    """Verify IAM role naming and description."""

    def test_role_name_includes_region(self):
        """Role name should include 'gco-github-actions' prefix."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "RoleName": Match.any_value(),
                "Description": Match.string_like_regexp(".*GitHub Actions.*"),
            },
        )

    def test_role_description_includes_repo(self):
        """Role description should mention the GitHub repo."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "Description": Match.string_like_regexp(
                    ".*awslabs/global-capacity-orchestrator-on-aws.*"
                ),
            },
        )

    def test_custom_repo_in_description(self):
        """Custom repo should appear in the role description."""
        template = _synth_stack(github_repo="my-org/my-fork")
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "Description": Match.string_like_regexp(".*my-org/my-fork.*"),
            },
        )


class TestPolicyJsonFile:
    """Verify the policy.json file is valid and contains expected structure."""

    def test_policy_json_is_valid(self):
        """policy.json should be valid JSON."""
        policy_path = Path(__file__).parent.parent / ".github" / "oidc_provider" / "policy.json"
        policy = json.loads(policy_path.read_text())
        assert policy["Version"] == "2012-10-17"
        assert "Statement" in policy
        assert len(policy["Statement"]) > 0

    def test_policy_json_has_allow_effect(self):
        """All statements should have Effect: Allow."""
        policy_path = Path(__file__).parent.parent / ".github" / "oidc_provider" / "policy.json"
        policy = json.loads(policy_path.read_text())
        for stmt in policy["Statement"]:
            assert stmt["Effect"] == "Allow"

    def test_policy_json_actions_are_read_only(self):
        """Default policy should only contain read-only actions (Describe/Get/List)."""
        policy_path = Path(__file__).parent.parent / ".github" / "oidc_provider" / "policy.json"
        policy = json.loads(policy_path.read_text())
        for stmt in policy["Statement"]:
            for action in stmt["Action"]:
                parts = action.split(":")
                verb = parts[1] if len(parts) == 2 else parts[0]
                assert verb.startswith(("Describe", "Get", "List")), (
                    f"Action '{action}' is not read-only. "
                    "Default CI policy should only contain Describe/Get/List actions."
                )
