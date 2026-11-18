"""Least-privilege IAM and network scoping of the regional stack's Lambdas and roles.

One real synthesis of ``GCORegionalStack`` from the checked-in ``cdk.json``
(the Helm installer included, service images stood in), then one test per
grant that used to be broader than its code path:

* the kubectl-applier and Helm installer roles hold no ``sts:AssumeRole`` on
  ``*`` and no ``eks:ListClusters``; their handlers call neither;
* the role every ``AwsCustomResource`` runs as reads exactly the SSM
  parameters those custom resources name (it used to hold ``ssm:GetParameter``
  on ``*`` through the cluster-shared readers' ``ANY_RESOURCE`` policies, and
  on ``parameter/<project>/*``).
"""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from tests._cdk_app_builder import PROJECT_ROOT, load_context, offline_image_assets

LAMBDA_DIR = PROJECT_ROOT / "lambda"


@pytest.fixture(scope="module")
def resources() -> dict:
    from cli.stacks import cdk_asset_consumer
    from gco.config.config_loader import ConfigLoader
    from gco.stacks.regional_stack import GCORegionalStack

    app = cdk.App(context=load_context())
    with cdk_asset_consumer(PROJECT_ROOT), offline_image_assets():
        stack = GCORegionalStack(
            app,
            "gco-us-east-1",
            config=ConfigLoader(app),
            region="us-east-1",
            auth_secret_arn=(
                "arn:aws:secretsmanager:us-east-2:123456789012:secret:gco/api-gateway-auth-token"  # nosec B106  # test fixture ARN
            ),
            env=cdk.Environment(account="123456789012", region="us-east-1"),
        )
        return assertions.Template.from_stack(stack).to_json()["Resources"]


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else [value]


def _role_id(resources: dict, prefix: str) -> str:
    [logical_id] = [
        lid
        for lid, r in resources.items()
        if lid.startswith(prefix) and r["Type"] == "AWS::IAM::Role"
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


def _by_actions(statements: list[dict]) -> dict[frozenset[str], list]:
    return {frozenset(_as_list(s["Action"])): _as_list(s["Resource"]) for s in statements}


def _cluster_arn(resources: dict) -> dict:
    [cluster] = [lid for lid, r in resources.items() if r["Type"] == "AWS::EKS::Cluster"]
    return {"Fn::GetAtt": [cluster, "Arn"]}


_TRACING = frozenset({"xray:PutTelemetryRecords", "xray:PutTraceSegments"})


@pytest.mark.parametrize(
    ("role_prefix", "handler"),
    [
        ("KubectlLambdaRole", "kubectl-applier-simple/handler.py"),
        ("HelmInstallerLambdaRole", "helm-installer/handler.py"),
    ],
)
def test_manifest_and_chart_lambdas_hold_only_what_their_handlers_call(
    resources, role_prefix, handler
):
    statements = _by_actions(_statements(resources, _role_id(resources, role_prefix)))
    assert statements == {
        frozenset({"eks:DescribeCluster"}): [_cluster_arn(resources)],
        _TRACING: ["*"],
        frozenset({"ssm:PutParameter"}): [
            "arn:aws:ssm:us-east-1:123456789012:parameter/gco/addons/*"
        ],
    }
    source = (LAMBDA_DIR / handler).read_text(encoding="utf-8")
    assert "assume_role" not in source
    assert "list_clusters" not in source


def test_custom_resource_role_reads_exactly_the_parameters_its_readers_name(resources):
    role_id = _role_id(resources, "AwsCustomResourceRole")
    statements = _statements(resources, role_id)
    granted = sorted(
        json.dumps(resource, sort_keys=True)
        for s in statements
        if "ssm:GetParameter" in _as_list(s["Action"])
        for resource in _as_list(s["Resource"])
    )
    creates = [r["Properties"]["Create"] for r in resources.values() if r["Type"] == "Custom::AWS"]
    # Calls rendered with tokens (the addon updates) are joins; none reads SSM.
    assert not [c for c in creates if not isinstance(c, str) and "getParameter" in json.dumps(c)]
    read = sorted(
        json.dumps(
            f"arn:aws:ssm:{call['region']}:123456789012:parameter{call['parameters']['Name']}"
        )
        for create in creates
        if isinstance(create, str)
        for call in [json.loads(create)]
        if (call["service"], call["action"]) == ("SSM", "getParameter")
    )
    assert len(read) == 6  # endpoint group, backend TLS, 3 cluster-shared, model bucket
    assert granted == read
    # No custom resource brings its own (ANY_RESOURCE) policy any more.
    assert not [
        lid
        for lid, r in resources.items()
        if r["Type"] == "AWS::IAM::Policy" and "CustomResourcePolicy" in lid
    ]
