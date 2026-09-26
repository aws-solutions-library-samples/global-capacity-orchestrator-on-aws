"""Regional-stack wiring for OpenTelemetry -> X-Ray tracing.

Covers everything ``GCORegionalStack`` synthesizes for the cdk.json
``tracing`` block and its ``tracing_overrides`` context:

* the always-present ``{{TRACING_ENABLED}}`` / ``{{TRACING_SAMPLE_RATIO}}``
  manifest replacements (a leftover placeholder would make the applier skip
  a core service Deployment);
* the write-only X-Ray span-export grant on exactly the four traced service
  roles, with cdk-nag reasons that say why it is ``Resource: *``;
* the one-directional CloudWatch Transaction Search custom resource (its
  Lambda, least-privilege role, provider log group and acknowledgments);
* the CloudWatch Observability add-on's Application Signals exclusions,
  validated against the add-on's configuration schema;
* the opt-in ``xray`` interface VPC endpoint.

Stacks synthesize against ``MockConfigLoader`` with Docker image assets and
the Helm installer patched out, like ``tests/test_regional_stack.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import jsonschema
import pytest
import yaml
from aws_cdk import assertions
from aws_cdk import aws_ec2 as ec2

from gco.services import tls_proxy
from gco.stacks import regional_stack as rs
from gco.stacks.nag_suppressions import _ACK_METADATA_KEY
from tests.test_regional_stack import MockConfigLoader, TestRegionalStackSynthesis

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_IMAGE_URI = f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/test:latest"
_TRACED_ROLES = ("HealthMonitorRole", "ManifestProcessorRole", "InferenceProxyRole")
_XRAY_ACTIONS = {"xray:PutTraceSegments", "xray:PutSpans"}

#: Excerpt of the ``describe-addon-configuration`` schema of
#: amazon-cloudwatch-observability v6.6.0-eksbuild.1 (the pinned
#: ``EKS_ADDON_CLOUDWATCH_OBSERVABILITY``): every level GCO writes keeps the
#: add-on's ``additionalProperties: false``, so a misspelled key fails here
#: instead of at the EKS UpdateAddon call. Sibling keys GCO never sets are
#: accepted as-is.
_ADDON_SCHEMA_EXCERPT: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2019-09/schema",
    "type": "object",
    "additionalProperties": False,
    "definitions": {
        "workloads": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "daemonsets": {"type": "array", "items": {"type": "string"}},
                "deployments": {"type": "array", "items": {"type": "string"}},
                "namespaces": {"type": "array", "items": {"type": "string"}},
                "statefulsets": {"type": "array", "items": {"type": "string"}},
            },
        }
    },
    "properties": {
        **{
            key: {}
            for key in (
                "admissionWebhooks",
                "agent",
                "agents",
                "applicationSignals",
                "containerInsights",
                "dcgmExporter",
                "kubeStateMetrics",
                "neuronMonitor",
                "nodeExporter",
                "otelContainerInsights",
            )
        },
        "tolerations": {"type": "array", "items": {"type": "object"}},
        "containerLogs": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"enabled": {"type": "boolean"}, "fluentBit": {}},
        },
        "manager": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                **{
                    key: {}
                    for key in (
                        "affinity",
                        "autoAnnotateAutoInstrumentation",
                        "autoInstrumentationConfiguration",
                        "nodeSelector",
                        "resources",
                        "tolerations",
                    )
                },
                "applicationSignals": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "autoMonitor": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "customSelector": {},
                                "languages": {"type": "array", "items": {"type": "string"}},
                                "monitorAllServices": {"type": "boolean"},
                                "restartPods": {"type": "boolean"},
                                "exclude": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        language: {"$ref": "#/definitions/workloads"}
                                        for language in ("dotnet", "java", "nodejs", "python")
                                    },
                                },
                            },
                        }
                    },
                },
            },
        },
    },
}


class _TracingConfig(MockConfigLoader):
    """A config double that carries ``get_tracing_config`` (the loader path)."""

    def __init__(self, app=None, *, tracing: dict[str, Any], cost_monitoring: bool = True):
        super().__init__(app)
        self._tracing = tracing
        self._cost_monitoring = cost_monitoring

    def get_tracing_config(self):
        return dict(self._tracing)

    def get_cost_monitoring_enabled(self):
        return self._cost_monitoring

    def get_vpc_endpoints_config(self):
        return {"gateway": ["s3", "dynamodb"], "interface": ["xray"]}


def _synth(config_factory, *, context: dict[str, Any] | None = None, name: str):
    app = cdk.App(context=context or {})
    config = config_factory(app)
    with (
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(
            rs.GCORegionalStack,
            "_create_helm_installer_lambda",
            TestRegionalStackSynthesis._mock_helm_installer,
        ),
    ):
        mock_image = MagicMock()
        mock_image.image_uri = _IMAGE_URI
        mock_docker.return_value = mock_image
        stack = rs.GCORegionalStack(
            app,
            name,
            config=config,
            region=_REGION,
            auth_secret_arn=(
                f"arn:aws:secretsmanager:us-east-2:{_ACCOUNT}:secret:gco-test/api-gateway-auth-token"
            ),
            env=cdk.Environment(account=_ACCOUNT, region=_REGION),
        )
    return stack, assertions.Template.from_stack(stack).to_json()["Resources"]


@pytest.fixture(scope="module")
def tracing_on():
    """Default posture: no ``tracing`` context, so the shipped defaults apply."""
    return _synth(MockConfigLoader, name="test-tracing-default")


@pytest.fixture(scope="module")
def tracing_off():
    return _synth(
        MockConfigLoader,
        context={"tracing": {"enabled": False}},
        name="test-tracing-off",
    )


@pytest.fixture(scope="module")
def tracing_without_transaction_search():
    """Loader path: tracing on, Transaction Search opted out, no cost monitoring."""
    return _synth(
        lambda app: _TracingConfig(
            app,
            tracing={"enabled": True, "sample_ratio": 0.25, "enable_transaction_search": False},
            cost_monitoring=False,
        ),
        name="test-tracing-no-transaction-search",
    )


@pytest.fixture(scope="module")
def tracing_overridden_live_validation():
    """The live harness: full sampling via tracing_overrides, retained provider logs."""
    return _synth(
        MockConfigLoader,
        context={
            "tracing_overrides": json.dumps({"sample_ratio": 1.0}),
            rs._LIVE_VALIDATION_PROVIDER_LOG_CONTEXT: "true",
        },
        name="test-tracing-overridden",
    )


def _replacements(resources: dict[str, Any]) -> dict[str, Any]:
    return resources["HelmInstallCharts"]["Properties"]["ImageReplacements"]


def _role_statements(resources: dict[str, Any], role_prefix: str) -> list[dict[str, Any]]:
    return [
        statement
        for logical_id, resource in resources.items()
        if logical_id.startswith(f"{role_prefix}DefaultPolicy")
        and resource["Type"] == "AWS::IAM::Policy"
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]
    ]


def _actions(statement: dict[str, Any]) -> set[str]:
    actions = statement["Action"]
    return {actions} if isinstance(actions, str) else set(actions)


def _xray_statements(resources: dict[str, Any], role_prefix: str) -> list[dict[str, Any]]:
    return [
        statement
        for statement in _role_statements(resources, role_prefix)
        if _actions(statement) & _XRAY_ACTIONS
    ]


def _acknowledgments(construct: Any) -> dict[str, str]:
    acknowledged: dict[str, str] = {}
    for entry in construct.node.metadata:
        if entry.type == _ACK_METADATA_KEY and entry.data:
            for finding, reason in entry.data.items():
                acknowledged.setdefault(finding, reason)
    return acknowledged


def _of_type(resources: dict[str, Any], resource_type: str, prefix: str) -> dict[str, Any]:
    return {
        logical_id: resource
        for logical_id, resource in resources.items()
        if resource["Type"] == resource_type and logical_id.startswith(prefix)
    }


class TestPureHelpers:
    @pytest.mark.parametrize(
        ("value", "rendered"),
        [(0.05, "0.05"), (1.0, "1.0"), (1, "1.0"), (0, "0.0"), (1e-05, "0.00001"), (0.25, "0.25")],
    )
    def test_sample_ratio_renders_as_a_plain_decimal(self, value, rendered):
        assert rs._decimal_string(value) == rendered

    @pytest.mark.parametrize(("enabled", "flag"), [(True, "true"), (False, "false")])
    def test_tracing_replacements_are_always_both_present(self, enabled, flag):
        assert rs._compute_kubectl_tracing_replacements(
            {"enabled": enabled, "sample_ratio": 0.05, "enable_transaction_search": True}
        ) == {"{{TRACING_ENABLED}}": flag, "{{TRACING_SAMPLE_RATIO}}": "0.05"}

    def test_xray_interface_endpoint_maps_to_the_cdk_service(self):
        service = rs._INTERFACE_ENDPOINT_SERVICES["xray"]
        # jsii returns a fresh proxy per static access, so compare identity by name.
        assert service.short_name == ec2.InterfaceVpcEndpointAwsService.XRAY.short_name == "xray"

    def test_chart_sidecar_is_hardened_and_never_gates_readiness(self):
        container, volume = rs._chart_tls_proxy_sidecar(
            name="example-tls-proxy",
            image=_IMAGE_URI,
            port=9443,
            upstream_port=9003,
            secret_name="example-tls",
        )

        assert container["command"] == ["python", "-m", "gco.services.tls_proxy"]
        assert container["ports"] == [{"name": "https", "containerPort": 9443}]
        env = {item["name"]: item["value"] for item in container["env"]}
        assert env == {
            "TLS_PROXY_PORT": "9443",
            "TLS_PROXY_UPSTREAM_HOST": "127.0.0.1",
            "TLS_PROXY_UPSTREAM_PORT": "9003",
            "TLS_PROXY_KEYPAIR_WAIT_SECONDS": "1800",
        }
        assert container["securityContext"] == {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        }
        # The chart pod must become Ready on its own container while the
        # sidecar waits for its Certificate: no readiness/startup probe, and
        # liveness only after the keypair wait budget.
        assert "readinessProbe" not in container
        assert "startupProbe" not in container
        liveness = container["livenessProbe"]
        assert liveness["tcpSocket"] == {"port": "https"}
        assert liveness["initialDelaySeconds"] > int(env["TLS_PROXY_KEYPAIR_WAIT_SECONDS"])
        assert container["resources"]["limits"]["memory"] == "128Mi"
        (mount,) = container["volumeMounts"]
        assert mount == {"name": volume["name"], "mountPath": "/var/run/gco/tls", "readOnly": True}
        # The keypair file env vars are omitted on purpose: the mount is the
        # proxy's default keypair directory.
        assert str(Path(tls_proxy.DEFAULT_CERT_FILE).parent) == mount["mountPath"]
        assert str(Path(tls_proxy.DEFAULT_KEY_FILE).parent) == mount["mountPath"]
        assert volume["secret"] == {
            "secretName": "example-tls",
            "optional": True,
            "defaultMode": 0o444,
        }

    def test_grafana_container_string_is_a_block_list_that_keeps_tokens_intact(self):
        stack = cdk.Stack(cdk.App(), "token-rendering")
        image = f"{cdk.Aws.ACCOUNT_ID}.dkr.ecr.{cdk.Aws.REGION}.{cdk.Aws.URL_SUFFIX}/repo:tag"
        container, _volume = rs._chart_tls_proxy_sidecar(
            name="grafana-tls-proxy",
            image=image,
            port=3443,
            upstream_port=3000,
            secret_name="grafana-tls",
        )

        rendered = rs._helm_container_list_string([container])

        assert rendered.startswith("- name: grafana-tls-proxy\n")
        assert yaml.safe_load(rendered) == [container]
        # The string survives CloudFormation token resolution as one join.
        resolved = stack.resolve(rendered)
        assert set(resolved) == {"Fn::Join"}
        assert {"Ref": "AWS::AccountId"} in resolved["Fn::Join"][1]
        # Grafana's _pod.tpl renders ``tpl . $ | nindent 2`` under containers.
        pod = "containers:\n  - name: grafana\n    image: grafana\n" + "".join(
            f"  {line}\n" for line in rendered.splitlines()
        )
        assert [item["name"] for item in yaml.safe_load(pod)["containers"]] == [
            "grafana",
            "grafana-tls-proxy",
        ]


class TestManifestReplacements:
    def test_default_posture_enables_tracing_at_five_percent(self, tracing_on):
        _stack, resources = tracing_on
        replacements = _replacements(resources)
        assert replacements["{{TRACING_ENABLED}}"] == "true"
        assert replacements["{{TRACING_SAMPLE_RATIO}}"] == "0.05"

    def test_disabled_tracing_still_substitutes_both_placeholders(self, tracing_off):
        _stack, resources = tracing_off
        replacements = _replacements(resources)
        assert replacements["{{TRACING_ENABLED}}"] == "false"
        assert replacements["{{TRACING_SAMPLE_RATIO}}"] == "0.05"

    def test_loader_config_drives_the_sample_ratio(self, tracing_without_transaction_search):
        _stack, resources = tracing_without_transaction_search
        assert _replacements(resources)["{{TRACING_SAMPLE_RATIO}}"] == "0.25"

    def test_tracing_overrides_context_reaches_the_manifests(
        self, tracing_overridden_live_validation
    ):
        _stack, resources = tracing_overridden_live_validation
        replacements = _replacements(resources)
        assert replacements["{{TRACING_ENABLED}}"] == "true"
        assert replacements["{{TRACING_SAMPLE_RATIO}}"] == "1.0"


class TestXRayGrants:
    @pytest.mark.parametrize("role", [*_TRACED_ROLES, "CostMonitorRole"])
    def test_traced_roles_get_one_write_only_export_statement(self, tracing_on, role):
        _stack, resources = tracing_on
        (statement,) = _xray_statements(resources, role)
        assert statement["Effect"] == "Allow"
        assert _actions(statement) == _XRAY_ACTIONS
        assert statement["Resource"] == "*"
        assert "Condition" not in statement

    def test_only_the_traced_service_roles_are_granted(self, tracing_on):
        _stack, resources = tracing_on
        granted = {
            logical_id.split("DefaultPolicy")[0]
            for logical_id, resource in resources.items()
            if resource["Type"] == "AWS::IAM::Policy"
            and any(
                set(_actions(statement)) & {"xray:PutSpans"}
                for statement in resource["Properties"]["PolicyDocument"]["Statement"]
            )
        }
        assert granted == {*_TRACED_ROLES, "CostMonitorRole"}

    def test_existing_reasons_explain_the_export_grant(self, tracing_on):
        stack, _resources = tracing_on
        for role in (
            stack.health_monitor_role,
            stack.manifest_processor_role,
            stack.inference_proxy_role,
        ):
            reason = _acknowledgments(role)["AwsSolutions-IAM5[Resource::*]"]
            assert reason.endswith(rs._TRACING_EXPORT_NAG_REASON)
            assert "xray:PutSpans" in reason
        cost_reason = _acknowledgments(stack.cost_monitor_role)["AwsSolutions-IAM5[Resource::*]"]
        assert cost_reason.startswith("CostMonitorRole's only wildcard is tracing.")

    def test_disabled_tracing_grants_nothing(self, tracing_off):
        stack, resources = tracing_off
        for role in (*_TRACED_ROLES, "CostMonitorRole"):
            assert _xray_statements(resources, role) == []
        reason = _acknowledgments(stack.health_monitor_role)["AwsSolutions-IAM5[Resource::*]"]
        assert "xray" not in reason
        assert "AwsSolutions-IAM5[Resource::*]" not in _acknowledgments(stack.cost_monitor_role)

    def test_grants_follow_cost_monitoring(self, tracing_without_transaction_search):
        stack, resources = tracing_without_transaction_search
        assert not hasattr(stack, "cost_monitor_role")
        for role in _TRACED_ROLES:
            assert len(_xray_statements(resources, role)) == 1


class TestTransactionSearch:
    def test_custom_resource_carries_the_region_identity(self, tracing_on):
        _stack, resources = tracing_on
        resource = resources["TransactionSearch"]
        assert resource["Type"] == "AWS::CloudFormation::CustomResource"
        properties = resource["Properties"]
        assert properties["Region"] == _REGION
        assert properties["AccountId"] == _ACCOUNT
        assert properties["Partition"] == {"Ref": "AWS::Partition"}
        assert properties["ProjectName"] == "gco-test"
        assert "Fn::GetAtt" in properties["ServiceToken"]
        # No deploy-time token: the resource re-runs only when its identity
        # changes, so an unrelated deploy can never wedge on it.
        assert set(properties) == {
            "ServiceToken",
            "Region",
            "AccountId",
            "Partition",
            "ProjectName",
        }
        assert any(
            dependency.startswith("TransactionSearchProviderLogGroup")
            for dependency in resource["DependsOn"]
        )

    def test_handler_lambda_mirrors_the_ga_deregistration_guard(self, tracing_on):
        _stack, resources = tracing_on
        (function,) = _of_type(
            resources, "AWS::Lambda::Function", "TransactionSearchFunction"
        ).values()
        properties = function["Properties"]
        assert properties["Handler"] == "handler.lambda_handler"
        assert properties["Runtime"] == "python3.14"
        assert properties["Timeout"] == 300
        assert properties["MemorySize"] == 256
        assert properties["TracingConfig"] == {"Mode": "Active"}
        assert "VpcConfig" not in properties
        # The asset is the Lambda directory itself.
        assert "S3Key" in properties["Code"]

        (log_group,) = _of_type(
            resources, "AWS::Logs::LogGroup", "TransactionSearchProviderLogGroup"
        ).values()
        assert log_group["Properties"]["RetentionInDays"] == 7
        assert log_group["DeletionPolicy"] == "Delete"

    def test_live_validation_retains_the_provider_log_group(
        self, tracing_overridden_live_validation
    ):
        _stack, resources = tracing_overridden_live_validation
        (log_group,) = _of_type(
            resources, "AWS::Logs::LogGroup", "TransactionSearchProviderLogGroup"
        ).values()
        assert log_group["DeletionPolicy"] == "Retain"
        assert log_group["UpdateReplacePolicy"] == "Retain"

    def test_role_is_least_privilege(self, tracing_on):
        _stack, resources = tracing_on
        statements = _role_statements(resources, "TransactionSearchFunctionServiceRole")
        by_actions = {frozenset(_actions(statement)): statement for statement in statements}

        assert set(by_actions) == {
            frozenset({"xray:PutTraceSegments", "xray:PutTelemetryRecords"}),
            frozenset(
                {
                    "xray:GetTraceSegmentDestination",
                    "xray:UpdateTraceSegmentDestination",
                    "logs:PutResourcePolicy",
                    "logs:DescribeResourcePolicies",
                    "application-signals:StartDiscovery",
                }
            ),
            frozenset({"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutRetentionPolicy"}),
            frozenset({"iam:CreateServiceLinkedRole"}),
            frozenset({"iam:GetRole"}),
            frozenset({"cloudtrail:CreateServiceLinkedChannel"}),
        }
        log_groups = json.dumps(
            by_actions[
                frozenset(
                    {"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutRetentionPolicy"}
                )
            ]["Resource"]
        )
        assert f":logs:{_REGION}:{_ACCOUNT}:log-group:aws/spans:*" in log_groups
        assert f":logs:{_REGION}:{_ACCOUNT}:log-group:/aws/application-signals/data:*" in log_groups

        service_linked_role = by_actions[frozenset({"iam:CreateServiceLinkedRole"})]
        assert service_linked_role["Condition"] == {
            "StringEquals": {"iam:AWSServiceName": "application-signals.cloudwatch.amazonaws.com"}
        }
        role_arn = json.dumps(service_linked_role["Resource"])
        assert (
            "role/aws-service-role/application-signals.cloudwatch.amazonaws.com/"
            "AWSServiceRoleForCloudWatchApplicationSignals" in role_arn
        )
        assert json.dumps(by_actions[frozenset({"iam:GetRole"})]["Resource"]) == role_arn
        channel = json.dumps(
            by_actions[frozenset({"cloudtrail:CreateServiceLinkedChannel"})]["Resource"]
        )
        assert (
            f":cloudtrail:{_REGION}:{_ACCOUNT}:channel/aws-service-channel/application-signals/*"
            in channel
        )

    def test_findings_are_acknowledged_with_exact_details(self, tracing_on):
        stack, _resources = tracing_on
        function = stack.node.find_child("TransactionSearchFunction")
        acknowledged = _acknowledgments(function)
        assert "AwsSolutions-IAM5[Resource::*]" in acknowledged
        for detail in (
            f"Resource::arn:<AWS::Partition>:logs:{_REGION}:{_ACCOUNT}:log-group:aws/spans:*",
            f"Resource::arn:<AWS::Partition>:logs:{_REGION}:{_ACCOUNT}"
            ":log-group:/aws/application-signals/data:*",
            f"Resource::arn:<AWS::Partition>:cloudtrail:{_REGION}:{_ACCOUNT}"
            ":channel/aws-service-channel/application-signals/*",
        ):
            assert f"AwsSolutions-IAM5[{detail}]" in acknowledged

        function_id = stack.get_logical_id(function.node.default_child)
        provider = stack.node.find_child("TransactionSearchProvider")
        assert f"AwsSolutions-IAM5[Resource::<{function_id}.Arn>:*]" in _acknowledgments(provider)

    def test_absent_when_tracing_is_off(self, tracing_off):
        _stack, resources = tracing_off
        assert not [logical_id for logical_id in resources if "TransactionSearch" in logical_id]

    def test_absent_when_opted_out(self, tracing_without_transaction_search):
        stack, resources = tracing_without_transaction_search
        assert not [logical_id for logical_id in resources if "TransactionSearch" in logical_id]
        assert not hasattr(stack, "transaction_search_resource")


class TestCloudWatchObservabilityAddon:
    @staticmethod
    def _configuration(resources: dict[str, Any]) -> dict[str, Any]:
        (addon,) = [
            resource
            for resource in resources.values()
            if resource["Type"] == "AWS::EKS::Addon"
            and resource["Properties"]["AddonName"] == "amazon-cloudwatch-observability"
        ]
        return json.loads(addon["Properties"]["ConfigurationValues"])

    def test_gco_namespaces_are_excluded_from_auto_instrumentation(self, tracing_on):
        _stack, resources = tracing_on
        configuration = self._configuration(resources)

        exclude = configuration["manager"]["applicationSignals"]["autoMonitor"]["exclude"]
        assert exclude == {
            language: {"namespaces": ["gco-system", "gco-inference"]}
            for language in ("java", "python", "dotnet", "nodejs")
        }
        # Container Insights logs and the accelerator tolerations are unchanged.
        assert configuration["containerLogs"] == {"enabled": True}
        assert configuration["tolerations"] == rs.GCORegionalStack._ADDON_NODE_TOLERATIONS

    def test_configuration_matches_the_pinned_addon_schema(self, tracing_on):
        _stack, resources = tracing_on
        jsonschema.Draft201909Validator(_ADDON_SCHEMA_EXCERPT).validate(
            self._configuration(resources)
        )

    def test_schema_excerpt_rejects_a_misspelled_path(self):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft201909Validator(_ADDON_SCHEMA_EXCERPT).validate(
                {"manager": {"applicationSignals": {"autoMonitor": {"excludes": {}}}}}
            )

    def test_exclusions_do_not_depend_on_tracing(self, tracing_off):
        _stack, resources = tracing_off
        configuration = self._configuration(resources)
        assert set(configuration["manager"]["applicationSignals"]["autoMonitor"]["exclude"]) == {
            "java",
            "python",
            "dotnet",
            "nodejs",
        }


class TestXRayInterfaceEndpoint:
    def test_opt_in_xray_endpoint_is_an_interface_endpoint(
        self, tracing_without_transaction_search
    ):
        _stack, resources = tracing_without_transaction_search
        endpoints = [
            resource["Properties"]
            for resource in resources.values()
            if resource["Type"] == "AWS::EC2::VPCEndpoint"
            and resource["Properties"].get("VpcEndpointType") == "Interface"
        ]
        (endpoint,) = endpoints
        assert endpoint["ServiceName"] == f"com.amazonaws.{_REGION}.xray"
        assert endpoint["PrivateDnsEnabled"] is True
