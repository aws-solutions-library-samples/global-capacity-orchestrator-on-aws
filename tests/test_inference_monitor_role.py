"""The inference monitor's dedicated IAM role on the regional stack.

The monitor used to run as the shared workload role, which every job and model
pod also assumes as ``gco-service-account``, so any submitted pod could rewrite
the inference endpoints table. These tests pin the split: the monitor role is
trusted only by ``gco-system/gco-inference-monitor-sa`` and holds exactly the
table calls the monitor makes plus the cold-tier bucket lookup; the workload
role holds neither and trusts only ``gco-service-account`` in the two
workload namespaces; the Pod Identity association, the IRSA annotation and the
Deployment's ``AWS_ROLE_ARN`` all name the monitor role.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import assertions

from tests import test_regional_stack as regional_fixtures

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = REPO_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"

#: boto3 Table method -> the DynamoDB IAM action it needs.
_TABLE_CALL_ACTIONS = {
    "batch_get_item": "dynamodb:BatchGetItem",
    "batch_writer": "dynamodb:BatchWriteItem",
    "delete_item": "dynamodb:DeleteItem",
    "get_item": "dynamodb:GetItem",
    "put_item": "dynamodb:PutItem",
    "query": "dynamodb:Query",
    "scan": "dynamodb:Scan",
    "update_item": "dynamodb:UpdateItem",
}


@pytest.fixture(scope="module")
def template() -> dict:
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
            "test-inference-monitor-role",
            config=regional_fixtures.MockConfigLoader(app),
            region="us-east-1",
            auth_secret_arn=(
                "arn:aws:secretsmanager:us-east-2:123456789012:secret:test-secret"  # nosec B106  # test fixture ARN
            ),
            env=cdk.Environment(account="123456789012", region="us-east-1"),
        )
        return assertions.Template.from_stack(stack).to_json()


def _role_id(resources: dict, prefix: str) -> str:
    [logical_id] = [
        logical_id
        for logical_id, resource in resources.items()
        if logical_id.startswith(prefix) and resource["Type"] == "AWS::IAM::Role"
    ]
    return logical_id


def _statements(resources: dict, role_id: str) -> list[dict]:
    return [
        statement
        for resource in resources.values()
        if resource["Type"] == "AWS::IAM::Policy"
        and {"Ref": role_id} in resource["Properties"]["Roles"]
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]
    ]


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else [value]


def _oidc_subjects(resources: dict, role_id: str) -> list[str]:
    """The ``:sub`` values of the role's IRSA trust (rendered through CfnJson)."""
    conditions_id = f"{role_id[: -len('XXXXXXXX')]}OidcConditions"
    [cfn_json] = [
        resource
        for logical_id, resource in resources.items()
        if logical_id.startswith(conditions_id)
    ]
    parts = cfn_json["Properties"]["Value"]["Fn::Join"][1]
    rendered = "".join(part if isinstance(part, str) else "<issuer>" for part in parts)
    return json.loads(rendered)["<issuer>:sub"]


def _monitor_store_methods() -> set[str]:
    source = (REPO_ROOT / "gco" / "services" / "inference_monitor.py").read_text(encoding="utf-8")
    return set(re.findall(r"self\.store\.([a-z_]+)\(", source))


def _table_actions(cls: type, method: str, seen: set[str]) -> set[str]:
    """DynamoDB actions one store method reaches, following ``self._helpers``."""
    if method in seen:
        return set()
    seen.add(method)
    tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(cls, method))))
    actions: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Attribute) and owner.attr == "_table":
            actions.add(_TABLE_CALL_ACTIONS[node.func.attr])
        elif isinstance(owner, ast.Name) and owner.id == "self" and hasattr(cls, node.func.attr):
            actions |= _table_actions(cls, node.func.attr, seen)
    return actions


def test_table_actions_are_exactly_what_the_monitor_calls():
    """The role's action list tracks the store methods the monitor invokes."""
    from gco.services.inference_store import InferenceEndpointStore
    from gco.stacks.regional_stack import INFERENCE_MONITOR_TABLE_ACTIONS

    methods = _monitor_store_methods()
    assert {"list_endpoints", "get_endpoint", "update_region_status", "delete_endpoint"} <= methods
    seen: set[str] = set()
    used = set().union(*(_table_actions(InferenceEndpointStore, m, seen) for m in methods))
    assert used == set(INFERENCE_MONITOR_TABLE_ACTIONS)
    assert "dynamodb:PutItem" not in used, "the monitor never creates endpoint records"


def test_monitor_role_trust_and_statements_are_exact(template):
    resources = template["Resources"]
    role_id = _role_id(resources, "InferenceMonitorRole")
    trust = resources[role_id]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert {"Service": "pods.eks.amazonaws.com"} in [s["Principal"] for s in trust]
    assert _oidc_subjects(resources, role_id) == [
        "system:serviceaccount:gco-system:gco-inference-monitor-sa"
    ]

    by_actions = {
        frozenset(_as_list(s["Action"])): _as_list(s["Resource"])
        for s in _statements(resources, role_id)
    }
    table_arn = {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                ":dynamodb:us-east-2:123456789012:table/gco-test-inference-endpoints",
            ],
        ]
    }
    [name_param_id] = [
        logical_id
        for logical_id, resource in resources.items()
        if logical_id.startswith("RegionalSharedBucketNameParam")
        and resource["Type"] == "AWS::SSM::Parameter"
    ]
    assert by_actions == {
        frozenset(
            {"dynamodb:DeleteItem", "dynamodb:GetItem", "dynamodb:Scan", "dynamodb:UpdateItem"}
        ): [table_arn],
        frozenset({"ssm:GetParameter"}): [
            {
                "Fn::Join": [
                    "",
                    [
                        "arn:",
                        {"Ref": "AWS::Partition"},
                        ":ssm:us-east-1:123456789012:parameter",
                        {"Ref": name_param_id},
                    ],
                ]
            }
        ],
    }


def test_workload_role_has_no_endpoint_table_or_discovery_grant(template):
    resources = template["Resources"]
    role_id = _role_id(resources, "ServiceAccountRole")
    assert _oidc_subjects(resources, role_id) == [
        "system:serviceaccount:gco-jobs:gco-service-account",
        "system:serviceaccount:gco-inference:gco-service-account",
    ]
    for statement in _statements(resources, role_id):
        actions = _as_list(statement["Action"])
        assert "ssm:GetParameter" not in actions
        assert "inference-endpoints" not in json.dumps(statement["Resource"])


def test_association_annotation_and_env_name_the_monitor_role(template):
    resources = template["Resources"]
    role_id = _role_id(resources, "InferenceMonitorRole")
    monitor_role_arn = {"Fn::GetAtt": [role_id, "Arn"]}

    [association] = [
        resource["Properties"]
        for resource in resources.values()
        if resource["Type"] == "AWS::EKS::PodIdentityAssociation"
        and resource["Properties"]["ServiceAccount"] == "gco-inference-monitor-sa"
    ]
    assert association["Namespace"] == "gco-system"
    assert association["RoleArn"] == monitor_role_arn

    trigger = resources["HelmInstallCharts"]
    assert trigger["Properties"]["ImageReplacements"]["{{INFERENCE_MONITOR_ROLE_ARN}}"] == (
        monitor_role_arn
    )
    # The policy is its own resource; convergence (which rolls the monitor
    # onto the role) waits for it, not just for the role.
    [policy_id] = [
        logical_id
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::IAM::Policy"
        and {"Ref": role_id} in resource["Properties"]["Roles"]
    ]
    assert {role_id, policy_id} <= set(trigger["DependsOn"])

    service_accounts = [
        doc
        for doc in yaml.safe_load_all((MANIFESTS / "02-rbac.yaml").read_text(encoding="utf-8"))
        if doc and doc.get("kind") == "ServiceAccount"
    ]
    [monitor_sa] = [
        sa for sa in service_accounts if sa["metadata"]["name"] == "gco-inference-monitor-sa"
    ]
    assert monitor_sa["metadata"]["annotations"]["eks.amazonaws.com/role-arn"] == (
        "{{INFERENCE_MONITOR_ROLE_ARN}}"
    )
    monitor_manifest = (MANIFESTS / "32-inference-monitor.yaml").read_text(encoding="utf-8")
    [deployment] = [
        doc
        for doc in yaml.safe_load_all(
            monitor_manifest.replace("{{INFERENCE_MONITOR_IMAGE}}", "image")
        )
        if doc and doc.get("kind") == "Deployment"
    ]
    [container] = [
        c
        for c in deployment["spec"]["template"]["spec"]["containers"]
        if c["name"] == "inference-monitor"
    ]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["AWS_ROLE_ARN"] == "{{INFERENCE_MONITOR_ROLE_ARN}}"
