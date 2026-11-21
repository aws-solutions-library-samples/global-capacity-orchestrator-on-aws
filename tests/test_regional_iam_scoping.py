"""Least-privilege IAM and network scoping of the regional stack's Lambdas and roles.

One real synthesis of ``GCORegionalStack`` from the checked-in ``cdk.json``
(the Helm installer included, service images stood in), then one test per
grant that used to be broader than its code path:

* the kubectl-applier and Helm installer roles hold no ``sts:AssumeRole`` on
  ``*`` and no ``eks:ListClusters``; their handlers call neither;
* the role every ``AwsCustomResource`` runs as reads exactly the SSM
  parameters those custom resources name (it used to hold ``ssm:GetParameter``
  on ``*`` through the cluster-shared readers' ``ANY_RESOURCE`` policies, and
  on ``parameter/<project>/*``);
* the GA registration and deregistration Lambdas reach only this Region's
  endpoint-registry parameter and its endpoint group and accelerator (they
  could put/delete every ``/<project>/`` parameter and call Global Accelerator
  on ``*``), and the EKS cluster security group admits the registration
  Lambda by its own security group rather than the whole VPC CIDR;
* the drift detection role holds what CloudFormation's drift detection needs
  (it lacked ``DetectStackResourceDrift``, ``BatchDescribeTypeConfigurations``
  and every resource read, so each run ended ``DETECTION_FAILED``): the stack
  actions on this stack's ARN, and the read-handler permissions of every
  resource type the stack contains, with bucket, parameter and function reads
  on this stack's own resources and no secret value, decrypt or role pass.
"""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from tests import test_regional_stack as regional_fixtures
from tests._cdk_app_builder import PROJECT_ROOT, load_context, offline_image_assets

LAMBDA_DIR = PROJECT_ROOT / "lambda"


def _synth_regional(overrides: dict | None = None) -> dict:
    from cli.stacks import cdk_asset_consumer
    from gco.config.config_loader import ConfigLoader
    from gco.stacks.regional_stack import GCORegionalStack

    app = cdk.App(context=load_context(overrides))
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


@pytest.fixture(scope="module")
def resources() -> dict:
    return _synth_regional()


@pytest.fixture(scope="module")
def all_features_resources() -> dict:
    """Every optional regional data service on, so their types are in the template."""
    return _synth_regional(
        {
            "fsx_lustre": {"enabled": True},
            "valkey": {"enabled": True},
            "aurora_pgvector": {"enabled": True},
            "vector_store": {"enabled": True},
        }
    )


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


def _grants(statements: list[dict]) -> set[tuple[str, str]]:
    """(action, resource) pairs, independent of how CDK merged the statements."""
    assert all(s["Effect"] == "Allow" and "Condition" not in s for s in statements)
    return {
        (action, json.dumps(resource, sort_keys=True))
        for s in statements
        for action in _as_list(s["Action"])
        for resource in _as_list(s["Resource"])
    }


def _pairs(actions: set[str], resources: list) -> set[tuple[str, str]]:
    return {(a, json.dumps(r, sort_keys=True)) for a in actions for r in resources}


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


def _endpoint_group_resources(resources: dict) -> list:
    reader = next(
        lid
        for lid, r in resources.items()
        if lid.startswith("GetEndpointGroupArn") and r["Type"] == "Custom::AWS"
    )
    endpoint_group = {"Fn::GetAtt": [reader, "Parameter.Value"]}
    return [
        endpoint_group,
        {"Fn::Select": [0, {"Fn::Split": ["/listener/", endpoint_group]}]},
    ]


_REGISTRY_PARAMETER = "arn:aws:ssm:us-east-2:123456789012:parameter/gco/alb-hostname-us-east-1"


def test_ga_registration_reaches_only_its_region_parameter_and_endpoint_group(resources):
    grants = _grants(_statements(resources, _role_id(resources, "GaRegistrationFunction")))
    assert grants == (
        _pairs(set(_TRACING), ["*"])
        | _pairs({"eks:DescribeCluster"}, [_cluster_arn(resources)])
        | _pairs(
            {"elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTags"},
            ["*"],
        )
        | _pairs({"ssm:PutParameter", "ssm:DeleteParameter"}, [_REGISTRY_PARAMETER])
        | _pairs(
            {
                "globalaccelerator:AddEndpoints",
                "globalaccelerator:RemoveEndpoints",
                "globalaccelerator:UpdateEndpointGroup",
                "globalaccelerator:DescribeEndpointGroup",
                "globalaccelerator:DescribeAccelerator",
            },
            _endpoint_group_resources(resources),
        )
    )
    # The handler writes and deletes that one parameter and never reads one.
    source = (LAMBDA_DIR / "ga-registration" / "handler.py").read_text(encoding="utf-8")
    assert "get_parameter" not in source
    assert source.count('f"/{project_name}/alb-hostname-{region}"') == 2


def test_ga_deregistration_reaches_only_its_region_parameter_and_endpoint_group(resources):
    grants = _grants(_statements(resources, _role_id(resources, "GaDeregistrationFunction")))
    assert grants == (
        _pairs(set(_TRACING), ["*"])
        | _pairs({"ssm:DeleteParameter"}, [_REGISTRY_PARAMETER])
        | _pairs(
            {
                "globalaccelerator:DescribeAccelerator",
                "globalaccelerator:DescribeEndpointGroup",
                "globalaccelerator:RemoveEndpoints",
                "globalaccelerator:UpdateEndpointGroup",
            },
            _endpoint_group_resources(resources),
        )
    )


def test_cluster_security_group_admits_no_cidr(resources):
    [cluster] = [lid for lid, r in resources.items() if r["Type"] == "AWS::EKS::Cluster"]
    cluster_sg = {"Fn::GetAtt": [cluster, "ClusterSecurityGroupId"]}
    rules = [
        r["Properties"]
        for r in resources.values()
        if r["Type"] == "AWS::EC2::SecurityGroupIngress"
        and r["Properties"]["GroupId"] == cluster_sg
    ]
    assert rules
    assert all("CidrIp" not in rule and "CidrIpv6" not in rule for rule in rules)
    [ga_rule] = [
        rule
        for rule in rules
        if rule["Description"] == "Allow GA registration Lambda to access EKS API"
    ]
    [ga_sg] = [
        lid
        for lid, r in resources.items()
        if lid.startswith("GaRegistrationFunctionSecurityGroup")
        and r["Type"] == "AWS::EC2::SecurityGroup"
    ]
    assert ga_rule["SourceSecurityGroupId"] == {"Fn::GetAtt": [ga_sg, "GroupId"]}
    assert (ga_rule["FromPort"], ga_rule["ToPort"], ga_rule["IpProtocol"]) == (443, 443, "tcp")
    [function] = [
        r["Properties"]
        for lid, r in resources.items()
        if lid.startswith("GaRegistrationFunction") and r["Type"] == "AWS::Lambda::Function"
    ]
    assert function["VpcConfig"]["SecurityGroupIds"] == [{"Fn::GetAtt": [ga_sg, "GroupId"]}]


def test_partitions_without_global_accelerator_grant_no_ga_action():
    """Where Global Accelerator does not exist neither Lambda gets an action for it."""
    from gco.stacks.regional_stack import GCORegionalStack

    class NoGaConfig(regional_fixtures.MockConfigLoader):
        def supports_global_accelerator(self):
            return False

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
            image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/x:y"
        )
        stack = GCORegionalStack(
            app,
            "test-no-ga-iam",
            config=NoGaConfig(app),
            region="us-east-1",
            auth_secret_arn="arn:aws:secretsmanager:us-east-2:123456789012:secret:x",  # nosec B106  # test fixture ARN
            env=cdk.Environment(account="123456789012", region="us-east-1"),
        )
        no_ga = assertions.Template.from_stack(stack).to_json()["Resources"]

    assert not [lid for lid in no_ga if lid.startswith("GetEndpointGroupArn")]
    for prefix in ("GaRegistrationFunction", "GaDeregistrationFunction"):
        actions = {a for a, _ in _grants(_statements(no_ga, _role_id(no_ga, prefix)))}
        assert actions and not [a for a in actions if a.startswith("globalaccelerator:")]


def _drift_statements(resources: dict) -> list[dict]:
    return _statements(resources, _role_id(resources, "DriftDetectionLambdaRole"))


def test_drift_role_holds_the_documented_cloudformation_actions(resources):
    grants = _grants([s for s in _drift_statements(resources) if "Condition" not in s])
    stack_actions = {
        "cloudformation:DetectStackDrift",
        "cloudformation:DetectStackResourceDrift",
        "cloudformation:DescribeStackResourceDrifts",
        "cloudformation:DescribeStackResource",
        "cloudformation:DescribeStackResources",
    }
    assert _pairs(stack_actions, [{"Ref": "AWS::StackId"}]) <= grants
    assert (
        _pairs(
            {
                "cloudformation:DescribeStackDriftDetectionStatus",
                "cloudformation:BatchDescribeTypeConfigurations",
            },
            ["*"],
        )
        <= grants
    )
    assert not [
        a
        for a, r in grants
        if a.startswith("cloudformation:") and a in stack_actions and r == '"*"'
    ]


@pytest.mark.parametrize("fixture", ["resources", "all_features_resources"])
def test_drift_role_can_read_every_resource_in_the_stack(request, fixture):
    from gco.stacks.drift_read_permissions import DRIFT_READ_ACTIONS, is_unchecked_type

    resources = request.getfixturevalue(fixture)
    grants = _grants(_drift_statements(resources))
    granted_actions = {a for a, _ in grants}

    types = {r["Type"] for r in resources.values()}
    unmapped = sorted(t for t in types if not is_unchecked_type(t) and t not in DRIFT_READ_ACTIONS)
    assert not unmapped, f"add these types to gco/stacks/drift_read_permissions.py: {unmapped}"
    for resource_type in types - {t for t in types if is_unchecked_type(t)}:
        missing = set(DRIFT_READ_ACTIONS[resource_type]) - granted_actions
        assert not missing, (resource_type, missing)

    def _resources_for(action: str) -> set[str]:
        return {r for a, r in grants if a == action}

    functions = {
        json.dumps({"Fn::GetAtt": [lid, "Arn"]}, sort_keys=True)
        for lid, r in resources.items()
        if r["Type"] == "AWS::Lambda::Function"
    }
    assert any(lid.startswith("DriftDetectionFunction") for lid in resources)
    assert _resources_for("lambda:GetFunction") == functions
    buckets = {
        json.dumps({"Fn::GetAtt": [lid, "Arn"]}, sort_keys=True)
        for lid, r in resources.items()
        if r["Type"] == "AWS::S3::Bucket"
    }
    assert _resources_for("s3:ListBucket") == buckets
    assert _resources_for("s3:GetBucketPolicy") == buckets
    parameters = sorted(lid for lid, r in resources.items() if r["Type"] == "AWS::SSM::Parameter")
    granted_parameters = sorted(
        ref
        for resource in _resources_for("ssm:GetParameters")
        for part in json.loads(resource)["Fn::Join"][1]
        if isinstance(part, dict) and (ref := part.get("Ref")) and ref != "AWS::Partition"
    )
    assert granted_parameters == parameters

    for action in ("secretsmanager:GetSecretValue", "iam:PassRole"):
        assert action not in granted_actions
    # The only Decrypt is the topic key's, for publishing the alert.
    [topic_key] = [
        lid
        for lid, r in resources.items()
        if lid.startswith("DriftDetectionTopicKey") and r["Type"] == "AWS::KMS::Key"
    ]
    assert _resources_for("kms:Decrypt") == {
        json.dumps({"Fn::GetAtt": [topic_key, "Arn"]}, sort_keys=True)
    }


@pytest.mark.parametrize("fixture", ["resources", "all_features_resources"])
def test_drift_role_inline_policies_fit_the_iam_limit(request, fixture):
    """IAM caps a role's inline policies at 10,240 characters in total."""
    resources = request.getfixturevalue(fixture)
    role_id = _role_id(resources, "DriftDetectionLambdaRole")
    documents = [
        r["Properties"]["PolicyDocument"]
        for r in resources.values()
        if r["Type"] == "AWS::IAM::Policy" and {"Ref": role_id} in r["Properties"]["Roles"]
    ]
    # Every token stands in for an ARN of at most 140 characters once resolved.
    rendered = re.sub(
        r'\{"(Fn::GetAtt|Ref|Fn::Join)":.*?\]\]?\}',
        "x" * 140,
        json.dumps(documents, separators=(",", ":")),
    )
    assert len(rendered) < 10_240


def test_fsx_csi_driver_role_describes_only_its_file_system(all_features_resources):
    """The statically provisioned file system's DescribeFileSystems, nothing else.

    The driver calls no FSx volume (OpenZFS/ONTAP) or EC2 API, and with static
    PersistentVolumes it never creates, resizes, tags or deletes a file system.
    """
    resources = all_features_resources
    [file_system] = [lid for lid, r in resources.items() if r["Type"] == "AWS::FSx::FileSystem"]
    statements = _statements(resources, _role_id(resources, "FsxCsiDriverRole"))
    assert _by_actions(statements) == {
        frozenset({"fsx:DescribeFileSystems"}): [
            {
                "Fn::Join": [
                    "",
                    ["arn:aws:fsx:us-east-1:123456789012:file-system/", {"Ref": file_system}],
                ]
            }
        ]
    }


def test_regional_bucket_key_names_no_service_principal(resources):
    """S3 uses the caller's own KMS permissions and no log group uses the key.

    The policy keeps only CDK's account statement: no ``s3.amazonaws.com`` or
    ``logs.<region>.amazonaws.com`` grant, and no ``logging.s3.amazonaws.com``
    grant, which CDK adds only while the access-logs bucket uses the key (it
    is SSE-S3, as server access logging requires).
    """
    [key] = [
        r
        for lid, r in resources.items()
        if lid.startswith("RegionalSharedKmsKey") and r["Type"] == "AWS::KMS::Key"
    ]
    statements = key["Properties"]["KeyPolicy"]["Statement"]
    assert [s["Principal"] for s in statements] == [
        {"AWS": "arn:aws:iam::123456789012:root"},
    ]
    log_group_keys = [
        r["Properties"].get("KmsKeyId")
        for r in resources.values()
        if r["Type"] == "AWS::Logs::LogGroup"
    ]
    assert not [k for k in log_group_keys if k and "RegionalSharedKmsKey" in json.dumps(k)]


def test_unmapped_types_are_refused():
    from gco.stacks.drift_read_permissions import drift_read_actions

    with pytest.raises(KeyError):
        drift_read_actions(["AWS::Example::Unknown"])
    actions = drift_read_actions(["Custom::AWS", "AWS::CDK::Metadata", "AWS::SQS::Queue"])
    assert actions.unscoped == ("sqs:GetQueueAttributes", "sqs:ListQueueTags")
    assert actions.bucket == actions.parameter == actions.function == ()


def test_drift_read_statements_leave_out_groups_with_no_actions() -> None:
    """A stack with no bucket or parameter of its own gets no statement for them."""
    from gco.stacks.drift_read_permissions import DriftReadActions, scoped_statements

    actions = DriftReadActions(
        unscoped=("ec2:DescribeVpcs",),
        bucket=(),
        parameter=(),
        function=("lambda:GetFunction",),
    )
    statements = scoped_statements(
        actions,
        function_arns=["arn:aws:lambda:us-east-1:123456789012:function:f"],
        bucket_arns=["arn:aws:s3:::unused"],
        parameter_arns=["arn:aws:ssm:us-east-1:123456789012:parameter/unused"],
    )
    assert statements == [
        (["ec2:DescribeVpcs"], ["*"]),
        (["lambda:GetFunction"], ["arn:aws:lambda:us-east-1:123456789012:function:f"]),
    ]
