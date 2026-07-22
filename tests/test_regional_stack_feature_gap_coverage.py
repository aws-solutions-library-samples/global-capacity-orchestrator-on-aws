"""Feature-gap coverage for the regional CDK stack.

These tests intentionally execute the real optional-service and Helm workflow
builders. Docker image assets and AWS lookups are replaced with deterministic
fixtures, while the resulting CloudFormation resources remain real CDK
constructs whose security and lifecycle properties can be inspected.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
from aws_cdk import assertions
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_lambda as lambda_

from gco.stacks import regional_stack as rs
from tests.test_regional_stack import MockConfigLoader

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_IMAGE_URI = f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/gco-test/fixture:latest"
_AZS = [f"{_REGION}{suffix}" for suffix in "abc"]
_REAL_HELM_BUILDER = rs.GCORegionalStack._create_helm_installer_lambda


class FeatureConfig(MockConfigLoader):
    """Enable a deterministic matrix of optional regional features."""

    def __init__(self, app, *, optional_features: bool, global_accelerator: bool):
        super().__init__(app, fsx_enabled=optional_features)
        self._optional_features = optional_features
        self._global_accelerator = global_accelerator

    def supports_global_accelerator(self):
        return self._global_accelerator

    def get_valkey_config(self):
        if not self._optional_features:
            return {"enabled": False}
        return {
            "enabled": True,
            "max_data_storage_gb": 37,
            "max_ecpu_per_second": 9000,
            "snapshot_retention_limit": 7,
        }

    def get_aurora_pgvector_config(self):
        if not self._optional_features:
            return {"enabled": False}
        return {
            "enabled": True,
            "min_acu": 0.5,
            "max_acu": 32,
            "backup_retention_days": 14,
            "deletion_protection": True,
        }

    def get_cluster_observability_config(self):
        config = super().get_cluster_observability_config()
        return {**config, "enabled": self._optional_features}

    def get_cluster_observability_enabled(self):
        return self._optional_features


def _app_context(
    *,
    feature_rich: bool,
    retain_provider_log_groups: bool = False,
) -> dict:
    helm_enabled = feature_rich
    return {
        rs._LIVE_VALIDATION_PROVIDER_LOG_CONTEXT: retain_provider_log_groups,
        f"availability-zones:account={_ACCOUNT}:region={_REGION}": _AZS,
        "drift_detection": {"enabled": False},
        "mcp_server": {"enabled": False},
        "volcano_image_mirror": {
            "enabled": feature_rich,
            "ecr_namespace": "gco-test/dockerhub",
        },
        "helm": {
            # KEDA is mandatory; setting false exercises that override.
            "keda": {"enabled": False},
            "aws_efa_device_plugin": {"enabled": helm_enabled},
            "aws_neuron_device_plugin": {"enabled": False},
            "volcano": {"enabled": helm_enabled},
            "kuberay": {"enabled": helm_enabled},
            "cert_manager": {"enabled": helm_enabled},
            "slurm": {"enabled": helm_enabled},
            "yunikorn": {"enabled": helm_enabled},
            "kueue": {"enabled": helm_enabled},
        },
        "queue_processor": {
            "enabled": feature_rich,
            "polling_interval": 17,
            "max_concurrent_jobs": 23,
            "messages_per_job": 2,
            "successful_jobs_history": 31,
            "failed_jobs_history": 13,
        },
        "job_validation_policy": {
            "allowed_namespaces": ["gco-jobs", "research"],
            "allowed_kinds": ["Job", "RayJob"],
            "resource_quotas": {
                "max_cpu_per_manifest": "24",
                "max_memory_per_manifest": "96Gi",
                "max_gpu_per_manifest": 8,
            },
            "trusted_registries": ["ghcr.io"],
            "trusted_dockerhub_orgs": ["pytorch", "rayproject"],
            "require_accelerator_toleration": False,
            "manifest_security_policy": {
                "block_privileged": True,
                "block_privilege_escalation": True,
                "block_host_network": False,
                "block_host_pid": True,
                "block_host_ipc": False,
                "block_host_path": True,
                "block_added_capabilities": True,
                "block_run_as_root": True,
            },
        },
        "vpc_endpoint_cidrs": ["10.41.0.0/16", "10.42.0.0/16"],
        "resource_quota": {
            "max_cpu": "240",
            "max_memory": "1Ti",
            "max_gpu": "64",
            "max_pods": "80",
            "container_max_cpu": "24",
            "container_max_memory": "96Gi",
            "container_max_gpu": "8",
        },
    }


def _create_real_helm_resources_without_docker(stack: rs.GCORegionalStack) -> None:
    """Run the production builder with an imported ECR image as its code source."""
    repository = ecr.Repository.from_repository_name(
        stack,
        "HelmInstallerFixtureRepository",
        "gco-test/helm-installer",
    )
    image_code = lambda_.DockerImageCode.from_ecr(repository, tag_or_digest="fixture")
    with patch.object(lambda_.DockerImageCode, "from_image_asset", return_value=image_code):
        _REAL_HELM_BUILDER(stack)


def _synthesize(
    *,
    feature_rich: bool,
    global_accelerator: bool,
    logical_name: str,
    retain_provider_log_groups: bool = False,
):
    app = cdk.App(
        context=_app_context(
            feature_rich=feature_rich,
            retain_provider_log_groups=retain_provider_log_groups,
        )
    )
    cdk.Tags.of(app).add("Project", "GCO")
    config = FeatureConfig(
        app,
        optional_features=feature_rich,
        global_accelerator=global_accelerator,
    )

    with (
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as docker_asset,
        patch.object(
            rs.GCORegionalStack,
            "_create_helm_installer_lambda",
            _create_real_helm_resources_without_docker,
        ),
        patch.object(rs.GCORegionalStack, "_resolve_unsupported_az_names", return_value=[]),
        patch.object(rs, "_deployment_timestamp", return_value="2026-01-02T03:04:05Z"),
        patch("boto3.client", side_effect=AssertionError("synthesis must not call AWS")),
    ):
        docker_asset.return_value.image_uri = _IMAGE_URI
        stack = rs.GCORegionalStack(
            app,
            logical_name,
            config=config,
            region=_REGION,
            auth_secret_arn=(f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:test-auth"),
            env=cdk.Environment(account=_ACCOUNT, region=_REGION),
        )
        return stack, assertions.Template.from_stack(stack)


@pytest.fixture(scope="module")
def feature_stack():
    return _synthesize(
        feature_rich=True,
        global_accelerator=True,
        logical_name="regional-feature-gap-rich",
    )


@pytest.fixture(scope="module")
def live_validation_feature_stack():
    return _synthesize(
        feature_rich=True,
        global_accelerator=True,
        logical_name="regional-feature-gap-live-validation",
        retain_provider_log_groups=True,
    )


@pytest.fixture(scope="module")
def ga_disabled_stack():
    return _synthesize(
        feature_rich=True,
        global_accelerator=False,
        logical_name="regional-feature-gap-no-ga",
    )


def _single_resource(
    template: assertions.Template,
    resource_type: str,
    logical_id_prefix: str,
) -> tuple[str, dict]:
    matches = [
        (logical_id, resource)
        for logical_id, resource in template.find_resources(resource_type).items()
        if logical_id.startswith(logical_id_prefix)
    ]
    assert len(matches) == 1, (
        f"Expected one {resource_type} with prefix {logical_id_prefix!r}; "
        f"found {[logical_id for logical_id, _ in matches]}"
    )
    return matches[0]


def _render_definition_fragment(value: object) -> str:
    """Replace CloudFormation tokens in a DefinitionString with JSON-safe text."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"Fn::Join"}:
        delimiter, fragments = value["Fn::Join"]
        return str(delimiter).join(_render_definition_fragment(item) for item in fragments)
    return "__CFN_TOKEN__"


def _state_machine_definition(resource: dict) -> dict:
    properties = resource["Properties"]
    if "Definition" in properties:
        return properties["Definition"]
    rendered = _render_definition_fragment(properties["DefinitionString"])
    try:
        return json.loads(rendered)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic for CDK shape drift
        raise AssertionError(f"Could not decode synthesized state machine: {rendered}") from exc


def _depends_on(resource: dict) -> list[str]:
    dependencies = resource.get("DependsOn", [])
    return [dependencies] if isinstance(dependencies, str) else dependencies


def _actions(statement: dict) -> list[str]:
    actions = statement.get("Action", [])
    return [actions] if isinstance(actions, str) else actions


def _policy_statements_for_role(
    template: assertions.Template,
    logical_id_prefix: str,
) -> list[dict]:
    role_id, _ = _single_resource(template, "AWS::IAM::Role", logical_id_prefix)
    role_ref = {"Ref": role_id}
    policies = [
        policy
        for policy in template.find_resources("AWS::IAM::Policy").values()
        if role_ref in policy["Properties"].get("Roles", [])
    ]
    assert policies, f"Expected an inline policy attached to {role_id}"
    return [
        statement
        for policy in policies
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def _assert_retry(state: dict, *, attempts: int, interval: int, max_delay: int) -> None:
    retry = next(entry for entry in state["Retry"] if entry["ErrorEquals"] == ["States.ALL"])
    assert retry == {
        "ErrorEquals": ["States.ALL"],
        "IntervalSeconds": interval,
        "MaxAttempts": attempts,
        "BackoffRate": 2,
        "MaxDelaySeconds": max_delay,
    }


def test_chart_order_loader_preserves_safety_order_and_fails_closed():
    order = rs._load_helm_chart_order()

    assert order[0] == "aws-load-balancer-controller"
    assert order[1] == "keda"
    assert order[-1] == "kueue"
    assert order.index("cert-manager") < order.index("slinky-slurm-operator")
    assert order.index("slinky-slurm-operator") < order.index("slinky-slurm")
    assert order.index("kube-prometheus-stack") < order.index("kueue")
    assert len(order) == len(set(order))

    with (
        patch("builtins.open", side_effect=OSError("fixture read failure")),
        pytest.raises(RuntimeError, match="Unable to load Helm chart order"),
    ):
        rs._load_helm_chart_order()

    with (
        patch.object(rs.yaml, "safe_load", return_value={"charts": ["not-a-mapping"]}),
        pytest.raises(RuntimeError, match="non-empty charts object"),
    ):
        rs._load_helm_chart_order()


def test_lbc_is_mandatory_and_uses_dedicated_oidc_only_irsa(feature_stack):
    _stack, template = feature_stack

    role_id, role = _single_resource(
        template,
        "AWS::IAM::Role",
        "AwsLoadBalancerControllerRole",
    )
    trust_statements = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(trust_statements) == 1
    assert trust_statements[0]["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert "Federated" in trust_statements[0]["Principal"]
    assert "pods.eks.amazonaws.com" not in json.dumps(trust_statements)

    _, oidc_conditions = _single_resource(
        template,
        "Custom::AWSCDKCfnJson",
        "AwsLoadBalancerControllerRoleOidcConditions",
    )
    assert "system:serviceaccount:kube-system:aws-load-balancer-controller" in json.dumps(
        oidc_conditions
    )

    _, controller_policy = _single_resource(
        template,
        "AWS::IAM::Policy",
        "AwsLoadBalancerControllerPolicy",
    )
    assert controller_policy["Properties"]["Roles"] == [{"Ref": role_id}]
    actual_statements = controller_policy["Properties"]["PolicyDocument"]["Statement"]
    expected_statements = rs.aws_load_balancer_controller_policy_document("aws")["Statement"]
    assert len(actual_statements) == len(expected_statements)
    assert {action for statement in actual_statements for action in _actions(statement)} == {
        action for statement in expected_statements for action in _actions(statement)
    }

    general_actions = {
        action
        for statement in _policy_statements_for_role(template, "ServiceAccountRole")
        for action in _actions(statement)
    }
    controller_namespaces = (
        "acm:",
        "cognito-idp:",
        "ec2:",
        "elasticloadbalancing:",
        "iam:",
        "shield:",
        "waf-regional:",
        "wafv2:",
    )
    assert not any(action.startswith(controller_namespaces) for action in general_actions)


def test_unsupported_az_lookup_maps_ids_and_fails_closed(monkeypatch):
    subject = SimpleNamespace(deployment_region=_REGION)
    ec2_client = MagicMock()
    ec2_client.describe_availability_zones.return_value = {
        "AvailabilityZones": [
            {"ZoneId": "use1-az3", "ZoneName": "us-east-1e"},
            {"ZoneId": "use1-az5", "ZoneName": "us-east-1f"},
        ]
    }
    monkeypatch.setenv("CDK_DEFAULT_ACCOUNT", _ACCOUNT)

    with patch("boto3.client", return_value=ec2_client) as client_factory:
        resolved = rs.GCORegionalStack._resolve_unsupported_az_names(subject)

    assert resolved == ["us-east-1e"]
    client_factory.assert_called_once()
    kwargs = client_factory.call_args.kwargs
    assert kwargs["region_name"] == _REGION
    assert kwargs["config"].connect_timeout == 5
    assert kwargs["config"].read_timeout == 5
    assert kwargs["config"].retries == {"max_attempts": 2}
    ec2_client.describe_availability_zones.assert_called_once_with(
        Filters=[
            {
                "Name": "zone-id",
                "Values": list(rs.EKS_UNSUPPORTED_AZ_IDS[_REGION]),
            }
        ]
    )

    with (
        patch("boto3.client", side_effect=RuntimeError("credential lookup failed")),
        pytest.raises(RuntimeError, match="Unable to resolve EKS-unsupported Availability Zones"),
    ):
        rs.GCORegionalStack._resolve_unsupported_az_names(subject)


def test_optional_data_services_are_private_and_discoverable(feature_stack):
    _stack, template = feature_stack
    cache_id, cache = _single_resource(
        template,
        "AWS::ElastiCache::ServerlessCache",
        "ValkeyCache",
    )
    cache_props = cache["Properties"]

    assert cache_props["Engine"] == "valkey"
    assert cache_props["MajorEngineVersion"] == "8"
    assert cache_props["SnapshotRetentionLimit"] == 7
    assert cache_props["CacheUsageLimits"] == {
        "DataStorage": {"Maximum": 37, "Minimum": 1, "Unit": "GB"},
        "ECPUPerSecond": {"Maximum": 9000, "Minimum": 1000},
    }
    assert len(cache_props["SubnetIds"]) >= 2
    assert all("PrivateSubnet" in json.dumps(subnet) for subnet in cache_props["SubnetIds"])
    assert "ValkeySG" in json.dumps(cache_props["SecurityGroupIds"])
    assert {tag["Key"]: tag["Value"] for tag in cache_props["Tags"]} == {
        "Project": "GCO",
        "gco:project": "gco-test",
        "Region": _REGION,
    }

    valkey_sg_id, valkey_sg = _single_resource(
        template,
        "AWS::EC2::SecurityGroup",
        "ValkeySG",
    )
    inline_ingress = valkey_sg["Properties"].get("SecurityGroupIngress", [])
    separate_ingress = [
        resource["Properties"]
        for resource in template.find_resources("AWS::EC2::SecurityGroupIngress").values()
        if valkey_sg_id in json.dumps(resource["Properties"].get("GroupId"))
    ]
    valkey_ingress = [
        rule
        for rule in [*inline_ingress, *separate_ingress]
        if rule.get("FromPort") == 6379 and rule.get("ToPort") == 6379
    ]
    assert valkey_ingress
    assert all(rule["IpProtocol"] == "tcp" for rule in valkey_ingress)
    assert all("GCOVpc" in json.dumps(rule["CidrIp"]) for rule in valkey_ingress)
    assert "0.0.0.0/0" not in json.dumps(valkey_sg["Properties"].get("SecurityGroupEgress", []))

    _, parameter = _single_resource(template, "AWS::SSM::Parameter", "ValkeyEndpointParam")
    assert parameter["Properties"]["Name"] == f"/gco-test/valkey-endpoint-{_REGION}"
    assert parameter["Properties"]["Type"] == "String"
    assert cache_id in json.dumps(parameter["Properties"]["Value"])

    outputs = template.to_json()["Outputs"]
    assert cache_id in json.dumps(outputs["ValkeyEndpoint"]["Value"])
    assert cache_id in json.dumps(outputs["ValkeyPort"]["Value"])

    _, aurora = _single_resource(template, "AWS::RDS::DBCluster", "AuroraPgvectorCluster")
    aurora_props = aurora["Properties"]
    assert aurora_props["DeletionProtection"] is True
    assert aurora_props["BackupRetentionPeriod"] == 14
    assert aurora_props["StorageEncrypted"] is True
    assert aurora_props["EnableIAMDatabaseAuthentication"] is True
    assert aurora_props["ServerlessV2ScalingConfiguration"] == {
        "MinCapacity": 0.5,
        "MaxCapacity": 32,
    }
    _, subnet_group = _single_resource(
        template,
        "AWS::RDS::DBSubnetGroup",
        "AuroraPgvectorSubnetGroup",
    )
    assert all(
        "PrivateSubnet" in json.dumps(subnet) for subnet in subnet_group["Properties"]["SubnetIds"]
    )


def test_convergence_payload_carries_enabled_features_and_security_policy(feature_stack):
    _stack, template = feature_stack
    _, trigger = _single_resource(
        template,
        "AWS::CloudFormation::CustomResource",
        "HelmInstallCharts",
    )
    properties = trigger["Properties"]

    enabled = properties["EnabledCharts"]
    assert enabled[0] == "aws-load-balancer-controller"
    assert "keda" in enabled  # mandatory even though the context says disabled
    assert "aws-efa-device-plugin" in enabled
    assert "aws-neuron-device-plugin" not in enabled
    assert "slinky-slurm-operator" in enabled
    assert "slinky-slurm" in enabled
    assert "yunikorn" in enabled
    assert "kube-prometheus-stack" in enabled

    chart_overrides = properties["Charts"]
    lbc_values = chart_overrides["aws-load-balancer-controller"]["values"]
    assert lbc_values["region"] == _REGION
    assert "GCOEksCluster" in json.dumps(lbc_values["clusterName"])
    assert "GCOVpc" in json.dumps(lbc_values["vpcId"])
    assert "AwsLoadBalancerControllerRole" in json.dumps(
        lbc_values["serviceAccount"]["annotations"]["eks.amazonaws.com/role-arn"]
    )
    image_registry = json.dumps(chart_overrides["volcano"]["values"]["basic"]["image_registry"])
    assert f"{_ACCOUNT}.dkr.ecr.{_REGION}." in image_registry
    assert "AWS::URLSuffix" in image_registry
    assert "/gco-test/dockerhub" in image_registry
    observability = chart_overrides["kube-prometheus-stack"]["values"]
    assert observability["grafana"]["persistence"] == {
        "storageClassName": "gco-observability-gp3",
        "size": "10Gi",
    }
    assert observability["prometheus"]["prometheusSpec"]["retention"] == "15d"
    assert observability["alertmanager"]["enabled"] is True
    assert observability["prometheus-node-exporter"]["tolerations"]

    replacements = properties["ImageReplacements"]
    expected_queue_values = {
        "{{QP_POLLING_INTERVAL}}": "17",
        "{{QP_MAX_CONCURRENT_JOBS}}": "23",
        "{{QP_MESSAGES_PER_JOB}}": "2",
        "{{QP_SUCCESSFUL_JOBS_HISTORY}}": "31",
        "{{QP_FAILED_JOBS_HISTORY}}": "13",
        "{{QP_ALLOWED_NAMESPACES}}": "gco-jobs,research",
        "{{QP_ALLOWED_KINDS}}": "Job,RayJob",
        "{{QP_MAX_GPU_PER_MANIFEST}}": "8",
        "{{QP_MAX_CPU_PER_MANIFEST}}": "24",
        "{{QP_MAX_MEMORY_PER_MANIFEST}}": "96Gi",
        "{{QP_BLOCK_PRIVILEGED}}": "true",
        "{{QP_BLOCK_PRIVILEGE_ESCALATION}}": "true",
        "{{QP_BLOCK_HOST_NETWORK}}": "false",
        "{{QP_BLOCK_HOST_PID}}": "true",
        "{{QP_BLOCK_HOST_IPC}}": "false",
        "{{QP_BLOCK_HOST_PATH}}": "true",
        "{{QP_BLOCK_ADDED_CAPABILITIES}}": "true",
        "{{QP_BLOCK_RUN_AS_ROOT}}": "true",
        "{{QP_REQUIRE_ACCELERATOR_TOLERATION}}": "false",
    }
    for key, expected in expected_queue_values.items():
        assert replacements[key] == expected
    assert replacements["{{QUEUE_PROCESSOR_IMAGE}}"] == _IMAGE_URI
    trusted_registries = json.dumps(replacements["{{QP_TRUSTED_REGISTRIES}}"])
    assert "ghcr.io" in trusted_registries
    assert f"{_ACCOUNT}.dkr.ecr.{_REGION}." in trusted_registries
    assert "AWS::URLSuffix" in trusted_registries
    assert replacements["{{QP_TRUSTED_DOCKERHUB_ORGS}}"] == "pytorch,rayproject"
    assert "10.41.0.0/16" in replacements["{{VPC_ENDPOINT_CIDR_BLOCKS}}"]
    assert "10.42.0.0/16" in replacements["{{VPC_ENDPOINT_CIDR_BLOCKS}}"]

    optional_tokens = {
        "{{VALKEY_ENDPOINT}}": "ValkeyCache",
        "{{VALKEY_PORT}}": "ValkeyCache",
        "{{AURORA_PGVECTOR_ENDPOINT}}": "AuroraPgvectorCluster",
        "{{AURORA_PGVECTOR_READER_ENDPOINT}}": "AuroraPgvectorCluster",
        "{{AURORA_PGVECTOR_SECRET_ARN}}": "AuroraPgvectorClusterSecret",
        "{{FSX_FILE_SYSTEM_ID}}": "GCOFsxLustre",
        "{{FSX_DNS_NAME}}": "GCOFsxLustre",
        "{{FSX_MOUNT_NAME}}": "GCOFsxLustre",
        "{{FSX_SECURITY_GROUP_ID}}": "FsxSecurityGroup",
    }
    for key, logical_prefix in optional_tokens.items():
        assert logical_prefix in json.dumps(replacements[key])

    assert properties["ProjectName"] == "gco-test"
    assert properties["Region"] == _REGION
    assert properties["RegistryRegion"] == "us-east-2"
    assert properties["DeploymentTimestamp"] == "2026-01-02T03:04:05Z"
    assert template.to_json()["Outputs"]["AddonDeploymentToken"]["Value"] == (
        "2026-01-02T03:04:05Z"
    )
    assert "EndpointGroupArn" in properties


def test_install_state_machine_retries_and_continues_per_chart(feature_stack):
    _stack, template = feature_stack
    _, state_machine = _single_resource(
        template,
        "AWS::StepFunctions::StateMachine",
        "HelmInstallStateMachine",
    )
    properties = state_machine["Properties"]
    definition = _state_machine_definition(state_machine)
    states = definition["States"]
    chart_order = rs._load_helm_chart_order()

    assert properties["StateMachineType"] == "STANDARD"
    assert definition["TimeoutSeconds"] == 7200
    assert properties["TracingConfiguration"] == {"Enabled": True}
    assert properties["LoggingConfiguration"]["Level"] == "ALL"
    assert properties["LoggingConfiguration"].get("IncludeExecutionData", True) is True

    assert definition["StartAt"] == "ApplyBaseManifests"
    base_apply = states["ApplyBaseManifests"]
    assert base_apply["Next"] == f"HelmChart-{chart_order[0]}"
    assert "Catch" not in base_apply
    assert base_apply["TimeoutSeconds"] == 900
    _assert_retry(base_apply, attempts=3, interval=30, max_delay=180)
    assert "apply_manifests" in json.dumps(base_apply)
    assert '"PostHelm": "false"' in json.dumps(base_apply, sort_keys=True)

    for index, chart_name in enumerate(chart_order):
        state = states[f"HelmChart-{chart_name}"]
        next_state = (
            f"HelmChart-{chart_order[index + 1]}"
            if index + 1 < len(chart_order)
            else "ApplyPostHelmManifests"
        )
        assert state["Next"] == next_state
        assert state["Catch"] == [
            {
                "ErrorEquals": ["States.ALL"],
                "ResultPath": "$.lastChartError",
                "Next": next_state,
            }
        ]
        assert state["ResultPath"] == "$.lastChart"
        assert state["TimeoutSeconds"] == 960
        _assert_retry(state, attempts=4, interval=30, max_delay=300)
        state_json = json.dumps(state, sort_keys=True)
        assert "install_chart" in state_json
        assert chart_name in state_json
        assert "$.EnabledCharts" in state_json

    post_apply = states["ApplyPostHelmManifests"]
    assert post_apply["Next"] == "ValidateKubernetesManifests"
    assert "Catch" not in post_apply
    _assert_retry(post_apply, attempts=3, interval=30, max_delay=180)
    assert '"PostHelm": "true"' in json.dumps(post_apply, sort_keys=True)

    manifest_validation = states["ValidateKubernetesManifests"]
    assert manifest_validation["Next"] == "ValidateHelmReleases"
    assert manifest_validation["ResultPath"] == "$.manifestValidation"
    assert manifest_validation["TimeoutSeconds"] == 900
    assert "Catch" not in manifest_validation
    _assert_retry(manifest_validation, attempts=4, interval=60, max_delay=180)
    manifest_json = json.dumps(manifest_validation, sort_keys=True)
    assert "validate_manifests" in manifest_json
    assert "$.ImageReplacements" in manifest_json
    assert "$.DeploymentToken" in manifest_json

    helm_validation = states["ValidateHelmReleases"]
    assert helm_validation["Next"] == "PublishGatewayEndpoint"
    assert helm_validation["ResultPath"] == "$.helmValidation"
    assert helm_validation["TimeoutSeconds"] == 960
    assert "Catch" not in helm_validation
    _assert_retry(helm_validation, attempts=4, interval=60, max_delay=180)
    helm_json = json.dumps(helm_validation, sort_keys=True)
    assert "validate_releases" in helm_json
    assert "$.EnabledCharts" in helm_json
    assert "$.Charts" in helm_json
    assert "$.DeploymentToken" in helm_json

    publication = states["PublishGatewayEndpoint"]
    assert publication["Next"] == "HelmInstallComplete"
    assert publication["ResultPath"] == "$.endpointPublication"
    assert publication["TimeoutSeconds"] == 960
    assert "Catch" not in publication
    _assert_retry(publication, attempts=3, interval=30, max_delay=180)
    publication_json = json.dumps(publication, sort_keys=True)
    assert "publish_gateway_endpoint" in publication_json
    assert "$.RegistryRegion" in publication_json
    assert "$.ProjectName" in publication_json
    assert "$.EndpointGroupArn" in publication_json
    assert states["HelmInstallComplete"] == {"Type": "Succeed"}


def test_teardown_waits_then_uninstalls_in_strict_reverse_order(feature_stack):
    _stack, template = feature_stack
    _, state_machine = _single_resource(
        template,
        "AWS::StepFunctions::StateMachine",
        "HelmTeardownStateMachine",
    )
    properties = state_machine["Properties"]
    definition = _state_machine_definition(state_machine)
    states = definition["States"]
    chart_order = rs._load_helm_chart_order()
    lbc_chart = "aws-load-balancer-controller"
    non_lbc_reverse = [name for name in reversed(chart_order) if name != lbc_chart]

    assert properties["StateMachineType"] == "STANDARD"
    assert definition["TimeoutSeconds"] == 3360
    assert properties["TracingConfiguration"] == {"Enabled": True}
    assert properties["LoggingConfiguration"]["Level"] == "ALL"
    assert definition["StartAt"] == "DrainInFlightConvergence"
    assert states["DrainInFlightConvergence"] == {
        "Type": "Wait",
        "SecondsPath": "$.WaitForInFlightSeconds",
        "Next": "CheckRunningConvergence",
    }
    drain_check = states["CheckRunningConvergence"]
    assert drain_check["Next"] == "LateConvergenceFound"
    assert drain_check["ResultPath"] == "$.drainCheck"
    assert drain_check["TimeoutSeconds"] == 60
    late_work = states["LateConvergenceFound"]
    assert late_work["Default"] == "QuiesceHealthMonitor"
    assert late_work["Choices"] == [
        {
            "Variable": "$.drainCheck.StoppedExecutions",
            "NumericGreaterThan": 0,
            "Next": "DrainInFlightConvergence",
        }
    ]

    quiesce = states["QuiesceHealthMonitor"]
    assert quiesce["Next"] == "CleanupEndpointAndCharts"
    assert quiesce["TimeoutSeconds"] == 180
    assert "quiesce_health_monitor" in json.dumps(quiesce)

    parallel_cleanup = states["CleanupEndpointAndCharts"]
    assert parallel_cleanup["Type"] == "Parallel"
    assert parallel_cleanup["Next"] == "DeleteGatewayResources"
    assert parallel_cleanup["ResultPath"] == "$.preGatewayCleanup"
    branches = {branch["StartAt"]: branch for branch in parallel_cleanup["Branches"]}

    endpoint_branch = branches["CleanupGatewayEndpoint"]
    endpoint_cleanup = endpoint_branch["States"]["CleanupGatewayEndpoint"]
    assert endpoint_cleanup["End"] is True
    assert endpoint_cleanup["TimeoutSeconds"] == 900
    endpoint_json = json.dumps(endpoint_cleanup, sort_keys=True)
    assert "cleanup_gateway_endpoint" in endpoint_json
    assert "$.RegistryRegion" in endpoint_json
    assert "$.ProjectName" in endpoint_json
    assert "$.EndpointGroupArn" in endpoint_json

    chart_states = branches[f"HelmUninstallChart-{non_lbc_reverse[0]}"]["States"]
    for index, chart_name in enumerate(non_lbc_reverse):
        state = chart_states[f"HelmUninstallChart-{chart_name}"]
        if index + 1 < len(non_lbc_reverse):
            assert state["Next"] == f"HelmUninstallChart-{non_lbc_reverse[index + 1]}"
        else:
            assert state["End"] is True
        assert state["TimeoutSeconds"] == (240 if chart_name == "keda" else 120)
        assert "Catch" not in state
        assert all("States.ALL" not in retry["ErrorEquals"] for retry in state.get("Retry", []))
        state_json = json.dumps(state, sort_keys=True)
        assert "uninstall_chart" in state_json
        assert chart_name in state_json

    gateway_cleanup = states["DeleteGatewayResources"]
    assert gateway_cleanup["Next"] == f"HelmUninstallChart-{lbc_chart}"
    assert gateway_cleanup["TimeoutSeconds"] == 300
    assert "delete_gateway_resources" in json.dumps(gateway_cleanup)

    lbc_uninstall = states[f"HelmUninstallChart-{lbc_chart}"]
    assert lbc_uninstall["Next"] == "HelmTeardownComplete"
    assert lbc_uninstall["TimeoutSeconds"] == 300
    assert "uninstall_chart" in json.dumps(lbc_uninstall)
    assert states["HelmTeardownComplete"] == {"Type": "Succeed"}


def test_helm_providers_are_observable_and_iam_scoped(feature_stack):
    _stack, template = feature_stack
    lambdas = template.find_resources("AWS::Lambda::Function")
    by_handler = {
        resource["Properties"].get("Handler"): resource
        for resource in lambdas.values()
        if "Handler" in resource["Properties"]
    }

    orchestrator = by_handler["handler.on_event"]
    assert orchestrator["Properties"]["Timeout"] == 60
    assert orchestrator["Properties"]["TracingConfig"] == {"Mode": "Active"}
    assert "HelmInstallStateMachine" in json.dumps(
        orchestrator["Properties"]["Environment"]["Variables"]["STATE_MACHINE_ARN"]
    )

    teardown_on_event = by_handler["teardown_provider.on_event"]
    teardown_is_complete = by_handler["teardown_provider.is_complete"]
    for handler in (teardown_on_event, teardown_is_complete):
        assert handler["Properties"]["Timeout"] == 60
        assert handler["Properties"]["TracingConfig"] == {"Mode": "Active"}
        variables = handler["Properties"]["Environment"]["Variables"]
        assert "HelmInstallStateMachine" in json.dumps(variables["INSTALL_STATE_MACHINE_ARN"])
        assert "HelmTeardownStateMachine" in json.dumps(variables["TEARDOWN_STATE_MACHINE_ARN"])

    installer_statements = _policy_statements_for_role(template, "HelmInstallerLambdaRole")
    orchestrator_statements = _policy_statements_for_role(
        template,
        "HelmOrchestratorOnEventServiceRole",
    )
    teardown_statements = [
        *_policy_statements_for_role(template, "HelmTeardownOnEventServiceRole"),
        *_policy_statements_for_role(template, "HelmTeardownIsCompleteServiceRole"),
    ]
    put_parameter = [
        statement
        for statement in [*installer_statements, *orchestrator_statements]
        if "ssm:PutParameter" in _actions(statement)
    ]
    assert put_parameter
    assert all(
        "parameter/gco-test/addons/*" in json.dumps(statement["Resource"])
        for statement in put_parameter
    )

    start_execution = [
        statement
        for statement in [*orchestrator_statements, *teardown_statements]
        if "states:StartExecution" in _actions(statement)
    ]
    assert start_execution
    assert all(
        "HelmInstallStateMachine" in json.dumps(statement["Resource"])
        or "HelmTeardownStateMachine" in json.dumps(statement["Resource"])
        for statement in start_execution
    )

    execution_controls = [
        statement
        for statement in teardown_statements
        if {"states:StopExecution", "states:DescribeExecution"}.issubset(_actions(statement))
    ]
    assert execution_controls
    for statement in execution_controls:
        resources = statement["Resource"]
        resources = [resources] if isinstance(resources, str) else resources
        assert "*" not in resources
        assert "execution" in json.dumps(resources)
        assert "HelmInstallStateMachine" in json.dumps(resources)

    _, teardown_resource = _single_resource(
        template,
        "AWS::CloudFormation::CustomResource",
        "HelmTeardown",
    )
    teardown_dependencies = _depends_on(teardown_resource)
    assert any(item.startswith("HelmInstallerLambdaAccessEntry") for item in teardown_dependencies)
    assert any(item.startswith("HelmInstallStateMachine") for item in teardown_dependencies)
    assert any(item.startswith("HelmInstallCharts") for item in teardown_dependencies)
    assert any(item.startswith("AwsLoadBalancerControllerPolicy") for item in teardown_dependencies)
    assert not any(item.startswith("GaDeregistration") for item in teardown_dependencies)
    assert teardown_resource["Properties"]["RegistryRegion"] == "us-east-2"
    assert teardown_resource["Properties"]["ProjectName"] == "gco-test"
    assert "EndpointGroupArn" in teardown_resource["Properties"]

    _, convergence = _single_resource(
        template,
        "AWS::CloudFormation::CustomResource",
        "HelmInstallCharts",
    )
    _, ga_deregistration = _single_resource(
        template,
        "AWS::CloudFormation::CustomResource",
        "GaDeregistration",
    )
    assert any(item.startswith("HelmTeardown") for item in _depends_on(ga_deregistration))
    convergence_dependencies = _depends_on(convergence)
    assert any(
        item.startswith("AwsLoadBalancerControllerPolicy") for item in convergence_dependencies
    )
    assert any(
        item.startswith("GaRegistrationLambdaAccessEntry") for item in convergence_dependencies
    )
    assert convergence["Properties"]["ServiceToken"]

    # Ordinary deploys must not accumulate explicit provider groups. Strict
    # live-validation synthesis flips only these groups to Retain so its exact
    # post-stack cleanup can remove the same generation.
    for logical_id_prefix in (
        "HelmInstallerProviderLogGroup",
        "HelmTeardownProviderLogGroup",
        "GaDeregistrationProviderLogGroup",
    ):
        _, log_group = _single_resource(
            template,
            "AWS::Logs::LogGroup",
            logical_id_prefix,
        )
        assert log_group["DeletionPolicy"] == "Delete"
        assert log_group["UpdateReplacePolicy"] == "Delete"


def test_provider_log_retention_is_scoped_to_live_validation(
    live_validation_feature_stack,
):
    _stack, template = live_validation_feature_stack
    for logical_id_prefix in (
        "HelmInstallerProviderLogGroup",
        "HelmTeardownProviderLogGroup",
        "GaDeregistrationProviderLogGroup",
    ):
        _, log_group = _single_resource(
            template,
            "AWS::Logs::LogGroup",
            logical_id_prefix,
        )
        assert log_group["DeletionPolicy"] == "Retain"
        assert log_group["UpdateReplacePolicy"] == "Retain"


def test_non_ga_partition_omits_registration_and_tears_down_directly(ga_disabled_stack):
    _stack, template = ga_disabled_stack
    _, install_machine = _single_resource(
        template,
        "AWS::StepFunctions::StateMachine",
        "HelmInstallStateMachine",
    )
    states = _state_machine_definition(install_machine)["States"]

    assert "RegisterGlobalAccelerator" not in states
    post_apply = states["ApplyPostHelmManifests"]
    assert post_apply["Next"] == "ValidateKubernetesManifests"
    assert "Catch" not in post_apply
    manifest_validation = states["ValidateKubernetesManifests"]
    assert manifest_validation["Next"] == "ValidateHelmReleases"
    assert "Catch" not in manifest_validation
    helm_validation = states["ValidateHelmReleases"]
    assert helm_validation["Next"] == "PublishGatewayEndpoint"
    assert "Catch" not in helm_validation
    publication = states["PublishGatewayEndpoint"]
    assert publication["Next"] == "HelmInstallComplete"
    publication_json = json.dumps(publication, sort_keys=True)
    assert "publish_gateway_endpoint" in publication_json
    assert "$.RegistryRegion" in publication_json
    assert "$.ProjectName" in publication_json
    assert "EndpointGroupArn" not in publication_json

    _, convergence = _single_resource(
        template,
        "AWS::CloudFormation::CustomResource",
        "HelmInstallCharts",
    )
    assert "volcano" in convergence["Properties"]["EnabledCharts"]
    assert "EndpointGroupArn" not in convergence["Properties"]
    replacements = convergence["Properties"]["ImageReplacements"]
    assert replacements["{{QUEUE_PROCESSOR_IMAGE}}"] == _IMAGE_URI
    assert "ValkeyCache" in json.dumps(replacements["{{VALKEY_ENDPOINT}}"])
    assert "AuroraPgvectorCluster" in json.dumps(replacements["{{AURORA_PGVECTOR_ENDPOINT}}"])
    assert "GCOFsxLustre" in json.dumps(replacements["{{FSX_FILE_SYSTEM_ID}}"])

    lambda_ids = template.find_resources("AWS::Lambda::Function")
    assert any(logical_id.startswith("GaRegistrationFunction") for logical_id in lambda_ids)
    assert any(logical_id.startswith("GaDeregistrationFunction") for logical_id in lambda_ids)

    _, deregistration = _single_resource(
        template,
        "AWS::CloudFormation::CustomResource",
        "GaDeregistration",
    )
    assert "EndpointGroupArn" not in deregistration["Properties"]
    assert not [
        logical_id
        for logical_id in template.find_resources("AWS::CloudFormation::CustomResource")
        if logical_id.startswith("GetEndpointGroupArn")
    ]

    registration_actions = {
        action
        for statement in _policy_statements_for_role(template, "GaRegistrationFunctionServiceRole")
        for action in _actions(statement)
    }
    deregistration_actions = {
        action
        for statement in _policy_statements_for_role(
            template, "GaDeregistrationFunctionServiceRole"
        )
        for action in _actions(statement)
    }
    assert not any(action.startswith("globalaccelerator:") for action in registration_actions)
    assert not any(action.startswith("globalaccelerator:") for action in deregistration_actions)

    _, teardown = _single_resource(
        template,
        "AWS::CloudFormation::CustomResource",
        "HelmTeardown",
    )
    dependencies = _depends_on(teardown)
    assert any(item.startswith("HelmInstallCharts") for item in dependencies)
    assert not any(item.startswith("GaDeregistration") for item in dependencies)
    assert any(item.startswith("HelmTeardown") for item in _depends_on(deregistration))
