"""
CDK assertion tests for the dedicated MCP server IAM role on the regional stack.

GCORegionalStack provisions an McpServerRole that the MCP server can
assume at startup via GCO_MCP_ROLE_ARN. These tests synthesize the stack
(patching DockerImageAsset and the helm-installer builder to avoid a
Docker daemon dependency) and assert against the resulting CloudFormation
template that the role exists exactly once, is described as the MCP
role, and has only the least-privilege actions specified in the design —
no broader access sneaks in via managed policies or catch-all statements.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import aws_cdk as cdk
from aws_cdk import assertions

from tests.test_regional_stack import MockConfigLoader, TestRegionalStackSynthesis


def _synth(app: cdk.App, construct_id: str) -> assertions.Template:
    """Synthesize the regional stack and return its Template.

    Mirrors the helper in tests/test_drift_detection.py: mocks the Docker
    image asset and helm-installer Lambda builder so tests do not depend on a
    running Docker daemon.
    """
    from gco.stacks.regional_stack import GCORegionalStack

    config = MockConfigLoader(app)

    with (
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(
            GCORegionalStack,
            "_create_helm_installer_lambda",
            TestRegionalStackSynthesis._mock_helm_installer,
        ),
    ):
        mock_image = MagicMock()
        mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
        mock_docker.return_value = mock_image

        stack = GCORegionalStack(
            app,
            construct_id,
            config=config,
            region="us-east-1",
            auth_secret_arn=(
                "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret"  # nosec B106  # test fixture ARN
            ),
            env=cdk.Environment(account="123456789012", region="us-east-1"),
        )

        return assertions.Template.from_stack(stack)


def _find_mcp_role(template: assertions.Template) -> tuple[str, dict]:
    """Return the (logical_id, resource) for the MCP server IAM role.

    Identified by the role description mentioning "MCP server".
    """
    roles = template.find_resources("AWS::IAM::Role")
    matches = []
    for logical_id, resource in roles.items():
        description = resource.get("Properties", {}).get("Description", "")
        if "MCP server" in description:
            matches.append((logical_id, resource))
    assert len(matches) == 1, (
        f"Expected exactly one IAM role with 'MCP server' in description, found {len(matches)}"
    )
    return matches[0]


def _policies_for_role(template: assertions.Template, role_logical_id: str) -> list[dict]:
    """Return all AWS::IAM::Policy resources attached to the given role."""
    policies = template.find_resources("AWS::IAM::Policy")
    attached = []
    for policy in policies.values():
        roles = policy.get("Properties", {}).get("Roles", []) or []
        for role_ref in roles:
            if isinstance(role_ref, dict) and role_ref.get("Ref") == role_logical_id:
                attached.append(policy)
                break
    return attached


def _all_statements_for_role(template: assertions.Template, role_logical_id: str) -> list[dict]:
    """Flatten all PolicyDocument statements for every policy attached to role."""
    statements = []
    for policy in _policies_for_role(template, role_logical_id):
        doc = policy.get("Properties", {}).get("PolicyDocument", {})
        statements.extend(doc.get("Statement", []))
    return statements


def _actions_as_set(stmt: dict) -> set[str]:
    """Normalize an Action property (str or list) into a set."""
    actions = stmt.get("Action", [])
    if isinstance(actions, str):
        actions = [actions]
    return set(actions)


class TestMcpIamRole:
    """Assertions for the dedicated MCP server IAM role."""

    def test_mcp_role_exists_with_description(self):
        """An IAM role with 'MCP server' in description is synthesized."""
        template = _synth(cdk.App(), "test-mcp-role-exists")

        _, role = _find_mcp_role(template)
        props = role["Properties"]
        assert "MCP server" in props.get("Description", ""), (
            f"Expected 'MCP server' in role description, got {props.get('Description')!r}"
        )

    def test_mcp_role_has_describe_cluster_scoped_to_cluster_arn(self):
        """eks:DescribeCluster is present and scoped to the cluster ARN (not '*')."""
        template = _synth(cdk.App(), "test-mcp-role-eks")

        role_logical_id, _ = _find_mcp_role(template)
        statements = _all_statements_for_role(template, role_logical_id)

        eks_stmts = [s for s in statements if "eks:DescribeCluster" in _actions_as_set(s)]
        assert eks_stmts, "No statement granting eks:DescribeCluster found"

        for stmt in eks_stmts:
            assert stmt.get("Effect") == "Allow"
            resources = stmt.get("Resource")
            if isinstance(resources, str):
                resources = [resources]
            assert resources != ["*"], (
                "eks:DescribeCluster must be scoped to the cluster ARN, not wildcard"
            )
            # Resource should be a Fn::GetAtt or a Ref to the cluster, not a
            # bare wildcard string.
            for r in resources:
                assert r != "*", f"Unexpected wildcard resource on eks:DescribeCluster: {r!r}"

    def test_mcp_role_has_s3_get_and_list_scoped_to_project_prefix(self):
        """s3:GetObject and s3:ListBucket are scoped to the `{project}-*` prefix."""
        template = _synth(cdk.App(), "test-mcp-role-s3")

        role_logical_id, _ = _find_mcp_role(template)
        statements = _all_statements_for_role(template, role_logical_id)

        s3_stmts = [s for s in statements if _actions_as_set(s) & {"s3:GetObject", "s3:ListBucket"}]
        assert s3_stmts, "No statement granting s3:GetObject / s3:ListBucket found"

        # Gather all S3 actions granted on this role.
        granted_s3_actions: set[str] = set()
        s3_resources: list = []
        for stmt in s3_stmts:
            assert stmt.get("Effect") == "Allow"
            granted_s3_actions.update(a for a in _actions_as_set(stmt) if a.startswith("s3:"))
            resources = stmt.get("Resource")
            if isinstance(resources, str):
                resources = [resources]
            s3_resources.extend(resources)

        assert "s3:GetObject" in granted_s3_actions
        assert "s3:ListBucket" in granted_s3_actions

        # Resources must reference the `{project_name}-*` pattern, never the
        # bare "*" wildcard. MockConfigLoader.get_project_name() returns "gco"
        # so we expect arn:aws:s3:::gco-* style resources.
        assert s3_resources, "No S3 resources on statement"
        for r in s3_resources:
            assert r != "*", "S3 actions must not be granted on '*'"
            if isinstance(r, str):
                assert "arn:aws:s3:::" in r, f"Expected S3 ARN with project prefix, got {r!r}"
                # Must contain a '-*' prefix pattern (e.g. "gco-*").
                assert "-*" in r, f"Expected S3 resource to use '{{project}}-*' pattern, got {r!r}"

    def test_mcp_role_has_cloudwatch_metrics_readonly(self):
        """CloudWatch metrics read APIs are present (wildcard resource is acceptable)."""
        template = _synth(cdk.App(), "test-mcp-role-cw")

        role_logical_id, _ = _find_mcp_role(template)
        statements = _all_statements_for_role(template, role_logical_id)

        required = {
            "cloudwatch:GetMetricData",
            "cloudwatch:GetMetricStatistics",
            "cloudwatch:ListMetrics",
        }
        found: set[str] = set()
        for stmt in statements:
            if stmt.get("Effect") != "Allow":
                continue
            found.update(_actions_as_set(stmt) & required)

        missing = required - found
        assert not missing, f"MCP role is missing CloudWatch actions: {missing!r}"

    def test_mcp_role_has_sqs_scoped_to_job_queue_arn(self):
        """SQS send/describe actions are scoped to the job queue ARN, not wildcard."""
        template = _synth(cdk.App(), "test-mcp-role-sqs")

        role_logical_id, _ = _find_mcp_role(template)
        statements = _all_statements_for_role(template, role_logical_id)

        required = {"sqs:SendMessage", "sqs:GetQueueUrl", "sqs:GetQueueAttributes"}
        sqs_stmts = [s for s in statements if _actions_as_set(s) & required]
        assert sqs_stmts, "No statement granting SQS actions found"

        granted: set[str] = set()
        for stmt in sqs_stmts:
            assert stmt.get("Effect") == "Allow"
            granted.update(a for a in _actions_as_set(stmt) if a.startswith("sqs:"))
            resources = stmt.get("Resource")
            if isinstance(resources, str):
                resources = [resources]
            # SQS actions must NOT be on wildcard - they must reference the
            # job queue ARN (via Fn::GetAtt or similar).
            for r in resources:
                assert r != "*", "SQS actions must be scoped to the job queue ARN, not wildcard"

        missing = required - granted
        assert not missing, f"MCP role is missing SQS actions: {missing!r}"

    def test_mcp_role_has_no_broader_permissions(self):
        """Role must NOT grant iam:*, s3:DeleteObject, s3:PutObject, or *:*."""
        template = _synth(cdk.App(), "test-mcp-role-no-broad")

        role_logical_id, _ = _find_mcp_role(template)
        statements = _all_statements_for_role(template, role_logical_id)

        # Actions that must never appear on the MCP role.
        forbidden_exact = {
            "iam:*",
            "*",
            "*:*",
            "s3:*",
            "s3:DeleteObject",
            "s3:PutObject",
            "s3:DeleteBucket",
            "ec2:*",
            "sts:*",
        }

        # Also disallow any iam:Create/Delete/Put/Attach/Update actions.
        forbidden_iam_prefixes = (
            "iam:Create",
            "iam:Delete",
            "iam:Put",
            "iam:Attach",
            "iam:Update",
            "iam:AddUser",
            "iam:Pass",
        )

        for stmt in statements:
            if stmt.get("Effect") != "Allow":
                continue
            actions = _actions_as_set(stmt)

            for action in actions:
                assert action not in forbidden_exact, (
                    f"MCP role must not grant forbidden action {action!r}; statement: {stmt!r}"
                )
                assert not action.startswith("iam:"), (
                    f"MCP role must not grant any iam:* action, got {action!r}"
                )
                for prefix in forbidden_iam_prefixes:
                    assert not action.startswith(prefix), (
                        f"MCP role must not grant {prefix}*, got {action!r}"
                    )

    def test_mcp_role_cfn_output_present(self):
        """A CfnOutput named McpServerRoleArn must be exported."""
        template = _synth(cdk.App(), "test-mcp-role-output")

        outputs = template.find_outputs("McpServerRoleArn")
        assert outputs, "Expected a CfnOutput named 'McpServerRoleArn'"
        # There should be exactly one such output.
        assert len(outputs) == 1, (
            f"Expected exactly one McpServerRoleArn output, found {len(outputs)}"
        )

    def test_mcp_role_disabled_via_context(self):
        """Setting mcp_server.enabled=false skips role creation entirely."""
        app = cdk.App(context={"mcp_server": {"enabled": False}})
        template = _synth(app, "test-mcp-role-disabled")

        roles = template.find_resources("AWS::IAM::Role")
        mcp_roles = [
            r
            for r in roles.values()
            if "MCP server" in r.get("Properties", {}).get("Description", "")
        ]
        assert mcp_roles == [], (
            f"MCP role should not be created when mcp_server.enabled=false; found {len(mcp_roles)}"
        )

        # The CfnOutput should also be absent.
        outputs = template.find_outputs("McpServerRoleArn")
        assert not outputs, "McpServerRoleArn output should not exist when MCP role is disabled"


class TestNoDangerousPermissionsInStack:
    """Guardrail: no IAM policy in the stack should grant full-admin-style access."""

    def test_no_iam_policy_grants_full_admin(self):
        """No IAM policy or managed policy grants '*:*', 'iam:*', or NotAction='*'."""
        template = _synth(cdk.App(), "test-stack-no-admin")

        # Check inline policies (AWS::IAM::Policy) and managed policies
        # (AWS::IAM::ManagedPolicy).
        policy_types = ["AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"]

        forbidden_actions = {"*", "*:*", "iam:*"}

        for policy_type in policy_types:
            for logical_id, resource in template.find_resources(policy_type).items():
                doc = resource.get("Properties", {}).get("PolicyDocument", {})
                for stmt in doc.get("Statement", []):
                    if stmt.get("Effect") != "Allow":
                        continue

                    # Detect NotAction (effectively inverse wildcard).
                    not_action = stmt.get("NotAction")
                    if not_action is not None:
                        raise AssertionError(
                            f"Policy {logical_id} ({policy_type}) uses NotAction, "
                            f"which is dangerous: {stmt!r}"
                        )

                    actions = _actions_as_set(stmt)
                    bad = actions & forbidden_actions
                    assert not bad, (
                        f"Policy {logical_id} ({policy_type}) grants dangerous "
                        f"action(s) {bad!r}; statement: {stmt!r}"
                    )
