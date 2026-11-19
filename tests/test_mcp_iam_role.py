"""
The MCP server role (``McpServerRole``) on the regional stack.

With ``GCO_MCP_ROLE_ARN`` set, the MCP server and every ``gco``/``kubectl``
process it starts run as this role, so it must grant what the default
(ungated) tools call and nothing that writes a stack, an image, a model or a
bucket object, assumes or passes a role. ``gco/stacks/mcp_role_permissions.py``
holds the statements; these tests pin them on one real synthesis (the Docker
image assets and the Helm installer builder stood in), check the module's own
contract (default tools it leaves out, ARNs per Region, the cdk-nag details),
the EKS access entry and the RBAC it relies on, and that gco_mcp/README.md
documents the boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import assertions

from gco.stacks.mcp_role_permissions import (
    DEFAULT_TOOLS_OUTSIDE_ROLE,
    MCP_EKS_ACCESS_POLICY,
    MCP_KUBERNETES_GROUP,
    UNSCOPED_READ_ACTIONS,
    McpRoleScope,
    mcp_server_role_statements,
    wildcard_resources,
)

# Module import, not a name import: pytest would collect an imported Test* class again.
from tests import test_regional_stack as regional_fixtures

REPO_ROOT = Path(__file__).resolve().parents[1]
RBAC_MANIFEST = REPO_ROOT / "lambda" / "kubectl-applier-simple" / "manifests" / "02-rbac.yaml"

# Actions the role may hold that are not reads: requests to the GCO API
# (which authorizes each one itself), job submission to the GCO job queues,
# Bedrock prompts for the capacity advisor, and starting a Logs Insights query.
NON_READ_ACTIONS = {
    "execute-api:Invoke",
    "sqs:SendMessage",
    "bedrock:InvokeModel",
    "logs:StartQuery",
}
READ_VERBS = ("Describe", "List", "Get", "Search", "Query", "Scan")


def _synth(app: cdk.App, construct_id: str) -> assertions.Template:
    """Synthesize the regional stack (MockConfigLoader) and return its Template."""
    from gco.stacks.regional_stack import GCORegionalStack

    config = regional_fixtures.MockConfigLoader(app)
    with (
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(
            GCORegionalStack,
            "_create_helm_installer_lambda",
            regional_fixtures.TestRegionalStackSynthesis._mock_helm_installer,
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


@pytest.fixture(scope="module")
def template() -> assertions.Template:
    return _synth(cdk.App(), "test-mcp-role")


def _find_mcp_role(template: assertions.Template) -> tuple[str, dict]:
    """The (logical_id, resource) of the role whose description names the MCP server."""
    matches = [
        (logical_id, resource)
        for logical_id, resource in template.find_resources("AWS::IAM::Role").items()
        if "MCP server" in resource.get("Properties", {}).get("Description", "")
    ]
    assert len(matches) == 1, f"expected one MCP server role, found {len(matches)}"
    return matches[0]


def _all_statements_for_role(template: assertions.Template, role_logical_id: str) -> list[dict]:
    statements: list[dict] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        if {"Ref": role_logical_id} in policy["Properties"].get("Roles", []):
            statements.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return statements


def _actions_as_set(stmt: dict) -> set[str]:
    actions = stmt.get("Action", [])
    return {actions} if isinstance(actions, str) else set(actions)


def _flatten(value: Any) -> str:
    """Render a template resource (string or Fn::Join) as one string."""
    if isinstance(value, str):
        return value
    if "Fn::Join" in value:
        return "".join(_flatten(part) for part in value["Fn::Join"][1])
    if "Ref" in value:
        return f"<{value['Ref']}>"
    if "Fn::GetAtt" in value:
        return "<" + ".".join(value["Fn::GetAtt"]) + ">"
    raise AssertionError(f"unexpected resource shape: {value!r}")


def _resources(stmt: dict) -> list[str]:
    resources = stmt["Resource"]
    return [_flatten(r) for r in (resources if isinstance(resources, list) else [resources])]


def _role_grants(template: assertions.Template) -> set[tuple[frozenset[str], tuple[str, ...]]]:
    role_id, _ = _find_mcp_role(template)
    return {
        (frozenset(_actions_as_set(s)), tuple(sorted(_resources(s))))
        for s in _all_statements_for_role(template, role_id)
    }


class TestMcpServerRole:
    """The role as synthesized from the shipped regional stack."""

    def test_role_holds_exactly_the_default_tool_statements(self, template):
        """Every statement, actions and resources, as MockConfigLoader's deployment renders it."""
        partition, account = "<AWS::Partition>", "123456789012"

        def arn(service: str, region: str, resource: str, owner: str = account) -> str:
            return f"arn:{partition}:{service}:{region}:{owner}:{resource}"

        bucket = "arn:<AWS::Partition>:s3:::<ReadModelBucketName5A20EEB7.Parameter.Value>"
        table = arn("dynamodb", "us-east-2", "table/gco-test-capacity-history")
        endpoints = arn("dynamodb", "us-east-2", "table/gco-test-inference-endpoints")
        log_group = "log-group:/aws/containerinsights/gco-test-*/application"
        expected = {
            (
                frozenset({"execute-api:Invoke"}),
                tuple(
                    sorted(
                        arn("execute-api", region, f"*/prod/*/{path}")
                        for region in ("us-east-1", "us-east-2")
                        for path in ("api/v1/*", "inference/*")
                    )
                ),
            ),
            (
                frozenset(
                    {
                        "cloudformation:DescribeStacks",
                        "cloudformation:ListStackResources",
                        "cloudformation:GetTemplate",
                    }
                ),
                (arn("cloudformation", "*", "stack/gco-test-*/*"),),
            ),
            (
                frozenset({"ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"}),
                tuple(
                    sorted(
                        [
                            arn("ssm", "*", "parameter/gco-test/*"),
                            arn("ssm", "*", "parameter/aws/service/sagemaker-hyperpod/*", ""),
                        ]
                    )
                ),
            ),
            (
                frozenset(
                    {"eks:DescribeCluster", "eks:ListCapabilities", "eks:DescribeCapability"}
                ),
                tuple(
                    sorted(
                        [
                            arn("eks", "*", "cluster/gco-test-*"),
                            arn("eks", "*", "capability/gco-test-*/*"),
                        ]
                    )
                ),
            ),
            (
                frozenset({"sqs:SendMessage", "sqs:GetQueueAttributes"}),
                (arn("sqs", "us-east-1", "gco-test-jobs-us-east-1"),),
            ),
            (
                frozenset({"sqs:GetQueueAttributes"}),
                (arn("sqs", "us-east-1", "gco-test-jobs-dlq-us-east-1"),),
            ),
            (
                frozenset({"logs:StartQuery"}),
                tuple(sorted([arn("logs", "*", log_group), arn("logs", "*", f"{log_group}:*")])),
            ),
            (frozenset({"s3:ListBucket"}), (bucket,)),
            (frozenset({"dynamodb:Query"}), tuple(sorted([table, f"{table}/index/*"]))),
            (frozenset({"dynamodb:Scan"}), (endpoints,)),
            (
                frozenset(
                    {
                        "ecr:DescribeImages",
                        "ecr:GetLifecyclePolicy",
                        "ecr:DescribeImageReplicationStatus",
                    }
                ),
                (arn("ecr", "*", "repository/gco-test/*"),),
            ),
            (
                frozenset(
                    {
                        "sagemaker:DescribeCluster",
                        "sagemaker:ListTags",
                        "sagemaker:DescribeTrainingPlan",
                        "pcs:GetCluster",
                        "pcs:ListComputeNodeGroups",
                        "pcs:GetComputeNodeGroup",
                    }
                ),
                tuple(
                    sorted(
                        [
                            arn("sagemaker", "*", "cluster/*"),
                            arn("sagemaker", "*", "training-plan/*"),
                            arn("pcs", "*", "cluster/*"),
                        ]
                    )
                ),
            ),
            (
                frozenset({"bedrock:InvokeModel"}),
                tuple(
                    sorted(
                        [
                            arn("bedrock", "*", "foundation-model/*", ""),
                            arn("bedrock", "*", "inference-profile/*"),
                        ]
                    )
                ),
            ),
            (
                frozenset({"cognito-idp:DescribeUserPool"}),
                (arn("cognito-idp", "*", "userpool/*"),),
            ),
            (frozenset(UNSCOPED_READ_ACTIONS), ("*",)),
        }
        assert _role_grants(template) == expected

    def test_role_reads_only_except_the_gco_api_and_job_submission(self, template):
        """No role assumption or pass, no stack, image, model or object write."""
        role_id, _ = _find_mcp_role(template)
        for stmt in _all_statements_for_role(template, role_id):
            assert stmt["Effect"] == "Allow"
            assert "NotAction" not in stmt and "NotResource" not in stmt
            for action in _actions_as_set(stmt):
                service, _, verb = action.partition(":")
                assert service not in {"iam", "sts"}, action
                assert "*" not in action, action
                assert action in NON_READ_ACTIONS or verb.startswith(READ_VERBS), action

    def test_trust_policy_leaves_assumption_to_identity_policies(self, template):
        """The account principal, sts:AssumeRole only: callers need their own permission."""
        _, role = _find_mcp_role(template)
        props = role["Properties"]
        [statement] = props["AssumeRolePolicyDocument"]["Statement"]
        assert statement["Action"] == "sts:AssumeRole"
        assert (
            _flatten(statement["Principal"]["AWS"]) == "arn:<AWS::Partition>:iam::123456789012:root"
        )
        assert "Condition" not in statement
        assert "ManagedPolicyArns" not in props and "Policies" not in props
        assert props["MaxSessionDuration"] == 12 * 3600

    def test_access_entry_is_read_only_kubernetes_access(self, template):
        """The view access policy cluster-wide, plus the group 02-rbac.yaml binds."""
        role_id, _ = _find_mcp_role(template)
        entries = [
            e["Properties"]
            for e in template.find_resources("AWS::EKS::AccessEntry").values()
            if e["Properties"]["PrincipalArn"] == {"Fn::GetAtt": [role_id, "Arn"]}
        ]
        assert entries == [
            {
                "AccessPolicies": [
                    {
                        "AccessScope": {"Type": "cluster"},
                        "PolicyArn": {
                            "Fn::Join": [
                                "",
                                [
                                    "arn:",
                                    {"Ref": "AWS::Partition"},
                                    f":eks::aws:cluster-access-policy/{MCP_EKS_ACCESS_POLICY}",
                                ],
                            ]
                        },
                    }
                ],
                "ClusterName": {"Ref": "GCOEksCluster841A896A"},
                "KubernetesGroups": [MCP_KUBERNETES_GROUP],
                "PrincipalArn": {"Fn::GetAtt": [role_id, "Arn"]},
                "Type": "STANDARD",
            }
        ]
        assert MCP_EKS_ACCESS_POLICY == "AmazonEKSViewPolicy"

    def test_mcp_role_cfn_output_present(self, template):
        outputs = template.find_outputs("McpServerRoleArn")
        assert len(outputs) == 1

    def test_mcp_role_disabled_via_context(self):
        """mcp_server.enabled=false creates no role, access entry or output."""
        template = _synth(cdk.App(context={"mcp_server": {"enabled": False}}), "test-mcp-off")
        roles = [
            r
            for r in template.find_resources("AWS::IAM::Role").values()
            if "MCP server" in r.get("Properties", {}).get("Description", "")
        ]
        assert roles == []
        groups = [
            e["Properties"].get("KubernetesGroups")
            for e in template.find_resources("AWS::EKS::AccessEntry").values()
        ]
        assert [MCP_KUBERNETES_GROUP] not in groups
        assert not template.find_outputs("McpServerRoleArn")


class TestMcpRolePermissionsModule:
    """``gco/stacks/mcp_role_permissions.py`` on its own."""

    SCOPE = McpRoleScope(
        partition="aws",
        account="123456789012",
        project_name="acme",
        global_region="us-east-2",
        api_regions=("eu-west-1", "us-east-1", "us-east-2"),
        workload_regions=("us-east-1", "eu-west-1"),
        model_bucket_arn="arn:aws:s3:::acme-global-modelbucket-example",
    )

    def test_one_api_pair_per_region_and_one_queue_per_workload_region(self):
        statements = mcp_server_role_statements(self.SCOPE)
        by_actions = {tuple(sorted(s.actions)): s.resources for s in statements}
        assert by_actions[("execute-api:Invoke",)] == [
            f"arn:aws:execute-api:{region}:123456789012:*/prod/*/{path}"
            for region in ("eu-west-1", "us-east-1", "us-east-2")
            for path in ("api/v1/*", "inference/*")
        ]
        assert by_actions[("sqs:GetQueueAttributes", "sqs:SendMessage")] == [
            "arn:aws:sqs:us-east-1:123456789012:acme-jobs-us-east-1",
            "arn:aws:sqs:eu-west-1:123456789012:acme-jobs-eu-west-1",
        ]
        assert by_actions[("sqs:GetQueueAttributes",)] == [
            "arn:aws:sqs:us-east-1:123456789012:acme-jobs-dlq-us-east-1",
            "arn:aws:sqs:eu-west-1:123456789012:acme-jobs-dlq-eu-west-1",
        ]
        assert by_actions[("s3:ListBucket",)] == ["arn:aws:s3:::acme-global-modelbucket-example"]

    def test_wildcard_resources_are_deduplicated_details(self):
        statements = mcp_server_role_statements(self.SCOPE)
        details = wildcard_resources(statements + statements)
        assert len(details) == len(set(details))
        assert "Resource::*" in details
        assert "Resource::arn:aws:cloudformation:*:123456789012:stack/acme-*/*" in details
        assert "Resource::arn:aws:s3:::acme-global-modelbucket-example" not in details

    def test_unscoped_reads_are_reads(self):
        assert len(set(UNSCOPED_READ_ACTIONS)) == len(UNSCOPED_READ_ACTIONS)
        for action in UNSCOPED_READ_ACTIONS:
            assert action.partition(":")[2].startswith(READ_VERBS), action

    def test_hyperpod_preflight_quota_listings_are_granted(self):
        """Every Service Quotas listing ``hyperpod_preflight`` pages through is granted.

        The instance-type check lists the applied quotas, then AWS's defaults for
        an instance type with none applied. Without the second action, a session
        on the role turned that type's check into a "could not check" warning.
        """
        import boto3

        from cli import hyperpod_preflight
        from cli.pcs_preflight import _Checker

        model = boto3.client("service-quotas", region_name="us-east-1")
        operations: list[str] = []

        class _RecordingQuotas:
            def get_paginator(self, method: str) -> Any:
                operations.append(model.meta.method_to_api_mapping[method])
                return MagicMock(paginate=MagicMock(return_value=[{"Quotas": []}]))

        quotas = _RecordingQuotas()
        checker = _Checker("us-east-1", lambda service, **_: quotas)
        hyperpod_preflight._check_instance_types(
            checker, {"instance_groups": [{"instance_type": "ml.g5.8xlarge", "instance_count": 1}]}
        )

        assert operations == ["ListServiceQuotas", "ListAWSDefaultServiceQuotas"]
        for operation in operations:
            assert f"servicequotas:{operation}" in UNSCOPED_READ_ACTIONS, operation

    def test_images_orphans_endpoint_reads_are_granted(self, monkeypatch):
        """Every DynamoDB call ``images_orphans`` makes for the endpoint records is granted.

        The orphan listing collects the images every inference endpoint names
        and fails when it cannot read them, so on a role without ``dynamodb:Scan``
        on the endpoints table every ``images_orphans`` call failed.
        """
        import boto3
        from botocore.stub import Stubber

        import gco.services.inference_store as inference_store
        from cli.config import GCOConfig
        from cli.images import ImageManager

        config = GCOConfig()
        resource = boto3.resource("dynamodb", region_name=config.global_region)
        client = resource.meta.client
        calls: list[tuple[str, str, str]] = []
        client.meta.events.register(
            "before-parameter-build.dynamodb",
            lambda params, model, **_: calls.append(
                (model.name, client.meta.region_name, params["TableName"])
            ),
        )
        stubber = Stubber(client)
        stubber.add_response("scan", {"Items": []})
        monkeypatch.setattr(inference_store.boto3, "resource", lambda *a, **k: resource)
        with stubber:
            assert ImageManager(config, region="us-east-1")._collect_inference_image_refs() == set()
        stubber.assert_no_pending_responses()

        scope = McpRoleScope(
            partition="aws",
            account="123456789012",
            project_name=config.project_name,
            global_region=config.global_region,
            api_regions=(config.global_region,),
            workload_regions=("us-east-1",),
            model_bucket_arn="arn:aws:s3:::gco-global-modelbucket-example",
        )
        statements = mcp_server_role_statements(scope)
        assert calls == [("Scan", "us-east-2", "gco-inference-endpoints")]
        for operation, region, table in calls:
            arn = f"arn:aws:dynamodb:{region}:123456789012:table/{table}"
            assert any(
                f"dynamodb:{operation}" in statement.actions and arn in statement.resources
                for statement in statements
            ), (operation, arn)


_DEFAULT_TOOLS = (
    "import asyncio, json, sys; sys.path.insert(0, 'gco_mcp'); import run_mcp; "
    "print(json.dumps(sorted(t.name for t in asyncio.run(run_mcp.mcp._list_tools()))))"
)


@pytest.fixture(scope="module")
def default_tool_names() -> set[str]:
    """The tools the server registers with every GCO_ENABLE_* flag unset."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GCO_ENABLE_")}
    proc = subprocess.run(
        [sys.executable, "-c", _DEFAULT_TOOLS],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return set(json.loads(proc.stdout.strip().splitlines()[-1]))


class TestBoundaryIsDocumented:
    """The default tools the role leaves out exist and are documented."""

    def test_tools_outside_the_role_are_default_tools(self, default_tool_names):
        assert len(default_tool_names) > 100
        assert set(DEFAULT_TOOLS_OUTSIDE_ROLE) <= default_tool_names

    def test_readme_names_each_tool_outside_the_role(self):
        readme = (REPO_ROOT / "gco_mcp" / "README.md").read_text(encoding="utf-8")
        section = readme.split("### IAM role (`GCO_MCP_ROLE_ARN`)", 1)[1].split("\n## ", 1)[0]
        for tool in DEFAULT_TOOLS_OUTSIDE_ROLE:
            assert f"`{tool}`" in section, tool

    def test_rbac_binds_the_group_to_read_only_rules(self):
        documents = [d for d in yaml.safe_load_all(RBAC_MANIFEST.read_text()) if d]
        [binding] = [
            d
            for d in documents
            if d["kind"] == "ClusterRoleBinding"
            and any(s.get("name") == MCP_KUBERNETES_GROUP for s in d.get("subjects", []))
        ]
        assert binding["subjects"] == [
            {"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": MCP_KUBERNETES_GROUP}
        ]
        [role] = [
            d
            for d in documents
            if d["kind"] == "ClusterRole" and d["metadata"]["name"] == binding["roleRef"]["name"]
        ]
        assert role["rules"] == [
            {"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list", "watch"]},
            {
                "apiGroups": ["karpenter.sh"],
                "resources": ["nodepools"],
                "verbs": ["get", "list", "watch"],
            },
        ]


class TestNoDangerousPermissionsInStack:
    """Guardrail: no IAM policy in the stack should grant full-admin-style access."""

    def test_no_iam_policy_grants_full_admin(self, template):
        """No IAM policy or managed policy grants '*:*', 'iam:*', or NotAction='*'."""
        forbidden_actions = {"*", "*:*", "iam:*"}
        for policy_type in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            for logical_id, resource in template.find_resources(policy_type).items():
                doc = resource.get("Properties", {}).get("PolicyDocument", {})
                for stmt in doc.get("Statement", []):
                    if stmt.get("Effect") != "Allow":
                        continue
                    assert stmt.get("NotAction") is None, (
                        f"Policy {logical_id} ({policy_type}) uses NotAction: {stmt!r}"
                    )
                    bad = _actions_as_set(stmt) & forbidden_actions
                    assert not bad, f"Policy {logical_id} grants {bad!r}: {stmt!r}"
