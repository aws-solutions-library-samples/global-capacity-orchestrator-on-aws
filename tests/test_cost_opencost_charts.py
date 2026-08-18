"""
Tests for OpenCost chart wiring, cost manifests, and regional cost gating.

Mirrors tests/test_cluster_observability_charts.py for the cost pipeline:
the static opencost entry in charts.yaml (version pin, monitoring namespace,
Prometheus wiring, ServiceMonitor, MCP disabled, non-blocking install), the
GCORegionalStack chart-enable/override behavior under the toggle
conjunction, the {{COST_MONITORING_ENABLED}}-gated manifests (cost-monitor
Deployment file and the Grafana cost dashboard), and the kubectl-applier
prune inventory that removes exactly those resources when the feature is
disabled.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from gco.config.config_loader import ConfigLoader
from gco.stacks.regional_stack import GCORegionalStack as RS

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHARTS_YAML = _REPO_ROOT / "lambda" / "helm-installer" / "charts.yaml"
_MANIFESTS = _REPO_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"
_COST_MONITOR_MANIFEST = _MANIFESTS / "34-cost-monitor.yaml"
_COST_DASHBOARD_MANIFEST = _MANIFESTS / "post-helm-grafana-cost-dashboard.yaml"


class _MockNode:
    def __init__(self, context: dict[str, Any]):
        self._context = context

    def try_get_context(self, key: str) -> Any:
        return self._context.get(key)


class _MockApp:
    def __init__(self, context: dict[str, Any]):
        self.node = _MockNode(context)


@pytest.fixture
def valid_cdk_context() -> dict[str, Any]:
    return {
        "project_name": "gco",
        "deployment_regions": {
            "global": "us-east-2",
            "api_gateway": "us-east-2",
            "monitoring": "us-east-2",
            "regional": ["us-east-1"],
        },
        "kubernetes_version": "1.36",
        "resource_thresholds": {
            "cpu_threshold": 80,
            "memory_threshold": 85,
            "gpu_threshold": 90,
        },
        "global_accelerator": {
            "health_check_grace_period": 30,
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "health_check_path": "/api/v1/health",
        },
        "alb_config": {
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "healthy_threshold": 2,
            "unhealthy_threshold": 2,
        },
        "manifest_processor": {
            "image": "gco/manifest-processor:latest",
            "replicas": 3,
            "resource_limits": {"cpu": "1000m", "memory": "2Gi"},
        },
        "job_validation_policy": {
            "allowed_namespaces": ["gco-jobs"],
            "resource_quotas": {
                "max_cpu_per_manifest": "10",
                "max_memory_per_manifest": "32Gi",
                "max_gpu_per_manifest": 4,
            },
        },
        "api_gateway": {
            "throttle_rate_limit": 1000,
            "throttle_burst_limit": 2000,
            "log_level": "INFO",
            "metrics_enabled": True,
            "tracing_enabled": True,
        },
        "tags": {"Environment": "test"},
    }


def _stub(
    context: dict[str, Any],
    *,
    cost_enabled: bool = True,
    observability_enabled: bool = True,
):
    """Lightweight GCORegionalStack stand-in with the real ConfigLoader."""
    ctx = copy.deepcopy(context)
    ctx["cluster_observability"] = {"enabled": observability_enabled}
    ctx["cost_monitoring"] = {"enabled": cost_enabled}
    app = _MockApp(ctx)
    config = ConfigLoader(app)
    stub = SimpleNamespace(
        config=config,
        node=app.node,
        volcano_mirror_registry=None,
        cluster=SimpleNamespace(cluster_name="gco-us-east-1"),
        deployment_region="us-east-1",
        vpc=SimpleNamespace(vpc_id="vpc-0123456789abcdef0"),
        aws_load_balancer_controller_role=SimpleNamespace(
            role_arn="arn:aws:iam::123456789012:role/test-lbc-controller"
        ),
        cluster_shared_identity=SimpleNamespace(
            name="gco-cluster-shared-123456789012-us-east-2",
            arn="arn:aws:s3:::gco-cluster-shared-123456789012-us-east-2",
            region="us-east-2",
        ),
        mlflow_role=SimpleNamespace(role_arn="arn:aws:iam::123456789012:role/test-mlflow"),
    )
    stub._observability_chart_values = lambda: RS._observability_chart_values(stub)
    stub._cost_monitoring_active = lambda: RS._cost_monitoring_active(stub)
    stub._opencost_chart_values = lambda: RS._opencost_chart_values(stub)
    stub._mlflow_active = lambda: RS._mlflow_active(stub)
    stub._mlflow_chart_values = lambda: RS._mlflow_chart_values(stub)
    return stub


@pytest.fixture(scope="module")
def charts() -> dict[str, Any]:
    with open(_CHARTS_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)["charts"]


class TestOpencostChartEntry:
    def test_chart_is_defined_with_pinned_version(self, charts):
        opencost = charts["opencost"]
        assert opencost["repo_url"] == "https://opencost.github.io/opencost-helm-chart"
        assert opencost["chart"] == "opencost"
        assert re.fullmatch(r"\d+\.\d+\.\d+", opencost["version"])

    def test_chart_defaults_disabled_and_driven_by_the_stack(self, charts):
        # Like kube-prometheus-stack, inclusion comes from GCORegionalStack.
        assert charts["opencost"]["enabled"] is False

    def test_chart_installs_into_the_monitoring_namespace(self, charts):
        opencost = charts["opencost"]
        assert opencost["namespace"] == "monitoring"
        assert opencost["create_namespace"] is False

    def test_chart_install_is_non_blocking(self, charts):
        assert charts["opencost"]["wait"] is False

    def test_chart_orders_after_prometheus_and_before_kueue(self, charts):
        order = list(charts)
        assert order.index("opencost") > order.index("kube-prometheus-stack")
        assert order.index("opencost") < order.index("kueue")
        assert order[-1] == "kueue"

    def test_prometheus_wiring_targets_kube_prometheus_stack(self, charts):
        prometheus = charts["opencost"]["values"]["opencost"]["prometheus"]
        assert prometheus["external"]["enabled"] is False
        internal = prometheus["internal"]
        assert internal["enabled"] is True
        assert internal["serviceName"] == "kube-prometheus-stack-prometheus"
        assert internal["namespaceName"] == "monitoring"
        assert internal["port"] == 9090

    def test_service_monitor_feeds_the_grafana_dashboard(self, charts):
        metrics = charts["opencost"]["values"]["opencost"]["metrics"]
        assert metrics["serviceMonitor"]["enabled"] is True

    def test_mcp_server_stays_disabled(self, charts):
        assert charts["opencost"]["values"]["opencost"]["mcp"]["enabled"] is False

    def test_ui_is_enabled_for_the_tunnel_commands(self, charts):
        assert charts["opencost"]["values"]["opencost"]["ui"]["enabled"] is True

    def test_singleton_pod_is_protected_from_consolidation(self, charts):
        annotations = charts["opencost"]["values"]["podAnnotations"]
        assert annotations["karpenter.sh/do-not-disrupt"] == "true"


class TestRegionalChartWiring:
    def test_chart_enabled_when_both_toggles_on(self, valid_cdk_context):
        charts = RS._get_enabled_helm_charts(_stub(valid_cdk_context))
        assert "opencost" in charts

    def test_chart_absent_when_cost_toggle_off(self, valid_cdk_context):
        charts = RS._get_enabled_helm_charts(_stub(valid_cdk_context, cost_enabled=False))
        assert "opencost" not in charts

    def test_chart_absent_when_observability_off(self, valid_cdk_context):
        charts = RS._get_enabled_helm_charts(_stub(valid_cdk_context, observability_enabled=False))
        assert "opencost" not in charts
        assert "kube-prometheus-stack" not in charts

    def test_overrides_inject_the_cluster_identity(self, valid_cdk_context):
        overrides = RS._helm_chart_value_overrides(_stub(valid_cdk_context))
        values = overrides["opencost"]["values"]
        assert values["opencost"]["exporter"]["defaultClusterId"] == "gco-us-east-1"

    def test_overrides_never_touch_the_dns_zone_clustername(self, valid_cdk_context):
        """The chart's root ``clusterName`` is the Kubernetes DNS zone
        (``cluster.local``) baked into the Prometheus URL — overriding it with
        the EKS cluster name resolves to a nonexistent host and crash-loops
        the cost model (observed live: ``lookup kube-prometheus-stack-
        prometheus.monitoring.svc.gco-us-east-1 ... no such host``)."""
        overrides = RS._helm_chart_value_overrides(_stub(valid_cdk_context))
        assert "clusterName" not in overrides["opencost"]["values"]

    def test_overrides_exclude_opencost_when_disabled(self, valid_cdk_context):
        overrides = RS._helm_chart_value_overrides(_stub(valid_cdk_context, cost_enabled=False))
        assert "opencost" not in overrides


class TestCostMonitorManifest:
    @pytest.fixture(scope="class")
    def manifest_text(self) -> str:
        return _COST_MONITOR_MANIFEST.read_text(encoding="utf-8")

    def test_manifest_is_gated_on_the_cost_placeholder(self, manifest_text):
        assert "{{COST_MONITORING_ENABLED}}" in manifest_text

    def test_manifest_carries_the_deployment_contract_placeholders(self, manifest_text):
        for placeholder in (
            "{{COST_MONITOR_IMAGE}}",
            "{{COST_MONITOR_ROLE_ARN}}",
            "{{COST_REPORT_BUCKET}}",
            "{{COST_REPORT_INTERVAL_MINUTES}}",
        ):
            assert placeholder in manifest_text

    def test_manifest_documents_are_well_formed_after_replacement(self, manifest_text):
        rendered = manifest_text
        for placeholder, value in {
            "{{COST_MONITORING_ENABLED}}": "true",
            "{{COST_MONITOR_IMAGE}}": "123.dkr.ecr.us-east-1.amazonaws.com/x:latest",
            "{{COST_MONITOR_ROLE_ARN}}": "arn:aws:iam::123:role/cost",
            "{{COST_REPORT_BUCKET}}": "gco-cost-reports-123-us-east-2",
            "{{COST_REPORT_INTERVAL_MINUTES}}": "60",
            "{{CLUSTER_NAME}}": "gco-us-east-1",
            "{{REGION}}": "us-east-1",
            "{{PROJECT_NAME}}": "gco",
            "{{DEPLOYMENT_TIMESTAMP}}": "2026-07-26T00:00:00Z",
        }.items():
            rendered = rendered.replace(placeholder, value)
        documents = [doc for doc in yaml.safe_load_all(rendered) if doc]
        kinds = [(doc["kind"], doc["metadata"]["name"]) for doc in documents]
        assert ("ServiceAccount", "gco-cost-monitor-sa") in kinds
        assert ("Deployment", "cost-monitor") in kinds
        assert ("Service", "cost-monitor") in kinds
        assert (
            "NetworkPolicy",
            "allow-manifest-processor-to-cost-monitor-ingress",
        ) in kinds

    def test_deployment_is_a_recreate_singleton(self, manifest_text):
        rendered = re.sub(r"\{\{[A-Z0-9_]+\}\}", "x", manifest_text)
        deployment = next(
            doc for doc in yaml.safe_load_all(rendered) if doc and doc["kind"] == "Deployment"
        )
        assert deployment["spec"]["replicas"] == 1
        assert deployment["spec"]["strategy"]["type"] == "Recreate"
        pod = deployment["spec"]["template"]["spec"]
        assert pod["serviceAccountName"] == "gco-cost-monitor-sa"
        container = pod["containers"][0]
        security = container["securityContext"]
        assert security["allowPrivilegeEscalation"] is False
        assert security["readOnlyRootFilesystem"] is True
        assert security["capabilities"]["drop"] == ["ALL"]


class TestCostDashboardManifest:
    @pytest.fixture(scope="class")
    def manifest_text(self) -> str:
        return _COST_DASHBOARD_MANIFEST.read_text(encoding="utf-8")

    def test_dashboard_is_gated_on_the_cost_placeholder(self, manifest_text):
        assert "{{COST_MONITORING_ENABLED}}" in manifest_text

    def test_dashboard_configmap_is_sidecar_discoverable(self, manifest_text):
        rendered = manifest_text.replace("{{COST_MONITORING_ENABLED}}", "true")
        (configmap,) = [doc for doc in yaml.safe_load_all(rendered) if doc]
        assert configmap["metadata"]["name"] == "gco-dashboard-cost"
        assert configmap["metadata"]["namespace"] == "monitoring"
        assert configmap["metadata"]["labels"]["grafana_dashboard"] == "1"

    def test_dashboard_json_is_valid_and_uses_opencost_metrics(self, manifest_text):
        rendered = manifest_text.replace("{{COST_MONITORING_ENABLED}}", "true")
        (configmap,) = [doc for doc in yaml.safe_load_all(rendered) if doc]
        dashboard = json.loads(configmap["data"]["gco-cost.json"])
        assert dashboard["uid"] == "gco-cost"
        expressions = json.dumps(dashboard["panels"])
        assert "node_total_hourly_cost" in expressions
        assert "container_cpu_allocation" in expressions
        assert "pv_hourly_cost" in expressions

    def test_no_upper_snake_tokens_leak_into_the_dashboard_body(self, manifest_text):
        # Grafana legend fields must stay lowercase so the applier's
        # feature-gate regex never mistakes them for unresolved gates.
        body = manifest_text.replace("{{COST_MONITORING_ENABLED}}", "")
        assert not re.search(r"\{\{[A-Z][A-Z0-9_]*\}\}", body)


class TestApplierPruneInventory:
    @pytest.fixture(scope="class")
    def inventory(self):
        import sys

        handler_path = str(_REPO_ROOT / "lambda" / "kubectl-applier-simple")
        sys.path.insert(0, handler_path)
        try:
            sys.modules.pop("handler", None)
            import handler

            yield handler._FEATURE_RESOURCE_INVENTORY
        finally:
            sys.path.pop(0)
            sys.modules.pop("handler", None)

    def test_base_phase_prunes_the_cost_monitor_workload(self, inventory):
        base_targets = inventory[("{{COST_MONITORING_ENABLED}}", False)]
        assert ("apps/v1", "Deployment", "gco-system", "cost-monitor") in base_targets
        assert ("v1", "Service", "gco-system", "cost-monitor") in base_targets
        assert ("v1", "ServiceAccount", "gco-system", "gco-cost-monitor-sa") in base_targets
        network_policies = {name for api, kind, _, name in base_targets if kind == "NetworkPolicy"}
        assert network_policies == {
            "allow-manifest-processor-to-cost-monitor-ingress",
            "allow-cost-monitor-to-opencost",
            "allow-manifest-processor-to-cost-monitor-egress",
        }

    def test_post_helm_phase_prunes_the_dashboard(self, inventory):
        post_targets = inventory[("{{COST_MONITORING_ENABLED}}", True)]
        assert ("v1", "ConfigMap", "monitoring", "gco-dashboard-cost") in post_targets

    def test_inventory_matches_the_gated_manifest_resources(self, inventory):
        """Every gated resource in 34-cost-monitor.yaml has a prune entry."""
        rendered = re.sub(
            r"\{\{[A-Z0-9_]+\}\}", "x", _COST_MONITOR_MANIFEST.read_text(encoding="utf-8")
        )
        manifest_resources = {
            (doc["kind"], doc["metadata"]["name"]) for doc in yaml.safe_load_all(rendered) if doc
        }
        pruned = {
            (kind, name) for _, kind, _, name in inventory[("{{COST_MONITORING_ENABLED}}", False)]
        }
        assert manifest_resources == pruned


class TestTunnelServiceEntries:
    def test_monitoring_open_exposes_opencost_targets(self):
        from cli.commands.monitoring_cmd import _SERVICES

        ui = _SERVICES["opencost"]
        assert ui["target"] == "svc/opencost"
        assert ui["remote_port"] == 9090
        # Local default must not collide with the Prometheus forward (9090).
        assert ui["default_local_port"] == 9091
        api = _SERVICES["opencost-api"]
        assert api["remote_port"] == 9003
