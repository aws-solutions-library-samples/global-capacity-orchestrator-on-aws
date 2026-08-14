"""
Tests for MLflow chart wiring, the client-egress manifest, and gating.

Mirrors tests/test_cost_opencost_charts.py for the experiment-tracking
pipeline: the static mlflow entry in charts.yaml (the official OCI chart +
server image pinned, chart-managed SQLite claim on the observability gp3
class, ClusterIP/no-ingress/no-app-auth posture, telemetry env kill,
chart-built pod NetworkPolicy), the GCORegionalStack enablement under the
cluster_observability.mlflow conjunction in both directions, the value
overrides that carry the S3 artifact destination, IRSA role annotation and
claim size, the {{MLFLOW_ENABLED}}-gated client egress NetworkPolicy and
the prune inventory (which also owns the chart-managed claim helm
uninstall leaves behind), the tunnel service entry, and helm-installer
handle_task convergence.
"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from gco.config.config_loader import ConfigLoader
from gco.stacks.regional_stack import _OBSERVABILITY_STORAGE_CLASS
from gco.stacks.regional_stack import GCORegionalStack as RS
from tests._lambda_imports import load_lambda_module

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHARTS_YAML = _REPO_ROOT / "lambda" / "helm-installer" / "charts.yaml"
_NETWORK_MANIFEST = (
    _REPO_ROOT / "lambda" / "kubectl-applier-simple" / "manifests" / "post-helm-mlflow-network.yaml"
)


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
    observability_enabled: bool = True,
    mlflow_enabled: bool | None = None,
):
    """GCORegionalStack stand-in with the real ConfigLoader.

    ``mlflow_enabled=None`` leaves the sub-toggle absent so the on-by-default
    behavior is what's under test.
    """
    ctx = copy.deepcopy(context)
    observability: dict[str, Any] = {"enabled": observability_enabled}
    if mlflow_enabled is not None:
        observability["mlflow"] = {"enabled": mlflow_enabled}
    ctx["cluster_observability"] = observability
    ctx["cost_monitoring"] = {"enabled": False}
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


class TestMlflowChartEntry:
    def test_chart_is_the_official_oci_chart_with_pinned_version(self, charts):
        mlflow = charts["mlflow"]
        assert mlflow["repo_url"] == "oci://ghcr.io/mlflow/charts"
        assert mlflow["use_oci"] is True
        assert mlflow["chart"] == "mlflow"
        assert re.fullmatch(r"\d+\.\d+\.\d+", mlflow["version"])

    def test_fullname_override_keeps_the_bare_release_name(self, charts):
        # Without it every resource is named mlflow-mlflow, which would
        # silently break the tunnel target (svc/mlflow), the example's
        # service DNS, the IRSA trust (monitoring/mlflow), and the prune
        # inventory.
        assert charts["mlflow"]["values"]["fullnameOverride"] == "mlflow"

    def test_chart_defaults_disabled_and_driven_by_the_stack(self, charts):
        # Like opencost, inclusion comes from GCORegionalStack (the
        # cluster_observability.mlflow conjunction).
        assert charts["mlflow"]["enabled"] is False

    def test_chart_installs_into_the_monitoring_namespace(self, charts):
        mlflow = charts["mlflow"]
        assert mlflow["namespace"] == "monitoring"
        assert mlflow["create_namespace"] is False

    def test_chart_install_is_non_blocking(self, charts):
        assert charts["mlflow"]["wait"] is False

    def test_chart_orders_after_opencost_and_before_kueue(self, charts):
        order = list(charts)
        assert order.index("mlflow") > order.index("kube-prometheus-stack")
        assert order.index("mlflow") > order.index("opencost")
        assert order.index("mlflow") < order.index("kueue")
        assert order[-1] == "kueue"

    def test_app_image_is_the_pinned_official_full_build(self, charts):
        image = charts["mlflow"]["values"]["image"]
        assert image["repository"] == "ghcr.io/mlflow/mlflow"
        # The chart defaults the tag to v<appVersion>-full; the explicit pin
        # feeds the deps-drift report. -full carries the server extras
        # (SQLAlchemy backend, boto3 for the S3 artifact proxy).
        assert re.fullmatch(r"v\d+\.\d+\.\d+-full", image["tag"])

    def test_no_deployment_strategy_pin_so_the_chart_auto_recreates(self, charts):
        # The chart selects Recreate whenever a SQLite backend store or a
        # ReadWriteOnce claim is configured (verified in the rendered
        # Deployment) — RollingUpdate with surge would deadlock on the EBS
        # attach, and two servers must never share one SQLite file. Pinning
        # deploymentStrategy here could silently reintroduce surge.
        assert "deploymentStrategy" not in charts["mlflow"]["values"]

    def test_singleton_pod_is_protected_from_consolidation(self, charts):
        annotations = charts["mlflow"]["values"]["podAnnotations"]
        assert annotations["karpenter.sh/do-not-disrupt"] == "true"

    def test_service_is_cluster_ip_with_no_ingress(self, charts):
        values = charts["mlflow"]["values"]
        assert values["service"]["type"] == "ClusterIP"
        assert values["ingress"]["enabled"] is False

    def test_usage_telemetry_stays_off_via_env(self, charts):
        # The official chart has no telemetry toggle; the env pair is the
        # documented server kill switch (posture, not just default).
        env = {entry["name"]: entry["value"] for entry in charts["mlflow"]["values"]["env"]}
        assert env["MLFLOW_DISABLE_TELEMETRY"] == "true"
        assert env["DO_NOT_TRACK"] == "true"

    def test_sqlite_backend_lives_on_the_chart_managed_claim(self, charts):
        # First-class chart storage replaces the hand-rolled PVC +
        # extraVolumes wiring the community chart needed: storage.enabled
        # mounts the claim at /mlflow and the SQLite URI points inside it.
        values = charts["mlflow"]["values"]
        assert values["storage"]["enabled"] is True
        assert values["storage"]["storageClassName"] == _OBSERVABILITY_STORAGE_CLASS
        assert values["mlflow"]["backendStoreUri"] == "sqlite:////mlflow/mlflow.db"
        assert "extraVolumes" not in values
        assert "extraVolumeMounts" not in values

    def test_pod_network_policy_is_enabled(self, charts):
        # Chart-built defense-in-depth for the server pod: ingress limited
        # to the server port, egress to DNS + 443 (S3 via VPC endpoints).
        assert charts["mlflow"]["values"]["networkPolicy"]["enabled"] is True

    def test_no_deployment_token_values_are_static(self, charts):
        # S3 destination + IRSA annotation + claim size are deployment
        # tokens injected by the regional stack; keeping them out of
        # charts.yaml means the offline/online chart validators render
        # without placeholders and a copy-paste deploy cannot ship another
        # account's role.
        values = charts["mlflow"]["values"]
        assert "serviceAccount" not in values
        assert "artifactsDestination" not in values["mlflow"]
        assert "size" not in values["storage"]

    def test_resources_are_bounded(self, charts):
        resources = charts["mlflow"]["values"]["resources"]
        assert resources["requests"]["cpu"]
        assert resources["limits"]["memory"]

    def test_metrics_and_service_monitor_feed_prometheus(self, charts):
        # kube-prometheus-stack discovers ServiceMonitors cluster-wide and
        # the mlflow conjunction guarantees it is present. path must
        # accompany enabled: chart 0.1.0 renders the ServiceMonitor
        # endpoint path as null otherwise, the API server rejects it, and
        # every helm upgrade fails (caught live, 2026-08-14).
        metrics = charts["mlflow"]["values"]["metrics"]
        assert metrics["enabled"] is True
        assert metrics["path"] == "/metrics"
        assert charts["mlflow"]["values"]["serviceMonitor"]["enabled"] is True

    def test_single_gunicorn_worker_for_the_sqlite_singleton(self, charts):
        # Chart default is 4 workers; that quadruples the full-MLflow
        # import at startup and starved /health past the chart's fixed
        # liveness window under the CPU request (observed live 2026-08-14
        # as an exit-137 crash-loop). One worker is also the honest
        # concurrency for a single-writer SQLite backend.
        server = charts["mlflow"]["values"]["server"]
        assert server["value_options"]["workers"] == 1


class TestConfigLoaderMlflowToggle:
    def _loader(self, observability: dict[str, Any] | None) -> ConfigLoader:
        context: dict[str, Any] = {}
        if observability is not None:
            context["cluster_observability"] = observability
        return ConfigLoader(_MockApp(context))

    def test_mlflow_defaults_on(self):
        assert self._loader(None).get_mlflow_enabled() is True

    def test_sub_toggle_disables_mlflow_alone(self):
        loader = self._loader({"enabled": True, "mlflow": {"enabled": False}})
        assert loader.get_mlflow_enabled() is False
        assert loader.get_cluster_observability_enabled() is True

    def test_observability_off_switches_mlflow_off_with_it(self):
        loader = self._loader({"enabled": False})
        assert loader.get_mlflow_enabled() is False

    def test_partial_override_keeps_other_mlflow_defaults(self):
        # Deep-merge: overriding only `enabled` must not wipe the
        # persistence_size default (the no-clobber contract every other
        # observability sub-block honors).
        loader = self._loader({"enabled": True, "mlflow": {"enabled": True}})
        mlflow = loader.get_cluster_observability_config()["mlflow"]
        assert mlflow == {"enabled": True, "persistence_size": "10Gi"}


class TestRegionalChartWiring:
    def test_chart_enabled_when_both_toggles_on(self, valid_cdk_context):
        charts = RS._get_enabled_helm_charts(_stub(valid_cdk_context))
        assert "mlflow" in charts

    def test_chart_absent_when_mlflow_sub_toggle_off(self, valid_cdk_context):
        charts = RS._get_enabled_helm_charts(_stub(valid_cdk_context, mlflow_enabled=False))
        assert "mlflow" not in charts

    def test_chart_absent_when_observability_off(self, valid_cdk_context):
        charts = RS._get_enabled_helm_charts(_stub(valid_cdk_context, observability_enabled=False))
        assert "mlflow" not in charts
        assert "kube-prometheus-stack" not in charts

    def test_overrides_inject_destination_role_and_claim_size(self, valid_cdk_context):
        overrides = RS._helm_chart_value_overrides(_stub(valid_cdk_context))
        values = overrides["mlflow"]["values"]
        # Region-suffixed prefix: each regional tracking server numbers
        # experiments independently, so a shared root would interleave
        # unrelated runs' artifacts.
        assert values["mlflow"]["artifactsDestination"] == (
            "s3://gco-cluster-shared-123456789012-us-east-2/mlflow-artifacts/us-east-1"
        )
        annotation = values["serviceAccount"]["annotations"]["eks.amazonaws.com/role-arn"]
        assert annotation == "arn:aws:iam::123456789012:role/test-mlflow"
        # Deep-merged into the static storage block (enabled + class stay).
        assert values["storage"] == {"size": "10Gi"}

    def test_overrides_exclude_mlflow_when_disabled(self, valid_cdk_context):
        overrides = RS._helm_chart_value_overrides(_stub(valid_cdk_context, mlflow_enabled=False))
        assert "mlflow" not in overrides


class TestMlflowNetworkManifest:
    @pytest.fixture(scope="class")
    def manifest_text(self) -> str:
        return _NETWORK_MANIFEST.read_text(encoding="utf-8")

    def test_manifest_is_gated_on_the_mlflow_placeholder(self, manifest_text):
        assert "{{MLFLOW_ENABLED}}" in manifest_text

    def test_no_other_upper_snake_tokens_leak(self, manifest_text):
        body = manifest_text.replace("{{MLFLOW_ENABLED}}", "")
        assert not re.search(r"\{\{[A-Z][A-Z0-9_]*\}\}", body)

    @staticmethod
    def _rendered_docs(manifest_text: str) -> dict[str, dict[str, Any]]:
        rendered = manifest_text.replace("{{MLFLOW_ENABLED}}", "true")
        return {doc["kind"]: doc for doc in yaml.safe_load_all(rendered) if doc}

    def test_manifest_ships_no_claim_anymore(self, manifest_text):
        # The metadata claim is chart-managed now (storage.enabled); a PVC
        # reappearing here would race the chart's own claim for the name.
        assert "PersistentVolumeClaim" not in self._rendered_docs(manifest_text)

    def test_client_egress_policy_targets_opted_in_pods_only(self, manifest_text):
        """gco-jobs is egress-isolated; only labeled pods may reach the
        tracking server, and only the tracking server's pods."""
        policy = self._rendered_docs(manifest_text)["NetworkPolicy"]
        assert policy["metadata"]["namespace"] == "gco-jobs"
        assert policy["spec"]["podSelector"]["matchLabels"] == {"gco.io/mlflow-client": "true"}
        assert policy["spec"]["policyTypes"] == ["Egress"]
        (egress,) = policy["spec"]["egress"]
        (to,) = egress["to"]
        assert to["namespaceSelector"]["matchLabels"] == {
            "kubernetes.io/metadata.name": "monitoring"
        }
        assert to["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "mlflow"}
        ports = {entry["port"] for entry in egress["ports"]}
        # 5000 is both the Service port and the container port (the official
        # chart exposes the server 1:1), covering pre- and post-DNAT CNIs
        # with one entry.
        assert ports == {5000}

    def test_policy_port_matches_the_example_tracking_uri(self, manifest_text):
        """The example's MLFLOW_TRACKING_URI port and the egress allow must
        agree, or the example hangs on connect."""
        example_text = (_REPO_ROOT / "examples" / "mlflow-tracking-job.yaml").read_text(
            encoding="utf-8"
        )
        assert "http://mlflow.monitoring:5000" in example_text
        policy = self._rendered_docs(manifest_text)["NetworkPolicy"]
        (egress,) = policy["spec"]["egress"]
        assert {entry["port"] for entry in egress["ports"]} == {5000}

    def test_example_job_carries_the_client_label(self, manifest_text):
        """The shipped example must actually match the egress policy's
        selector, or it hangs on connect with nothing explaining why."""
        example = yaml.safe_load(
            (_REPO_ROOT / "examples" / "mlflow-tracking-job.yaml").read_text(encoding="utf-8")
        )
        pod_labels = example["spec"]["template"]["metadata"]["labels"]
        policy = self._rendered_docs(manifest_text)["NetworkPolicy"]
        selector = policy["spec"]["podSelector"]["matchLabels"]
        assert selector.items() <= pod_labels.items()


class TestApplierPruneInventory:
    @pytest.fixture(scope="class")
    def applier(self):
        handler_path = str(_REPO_ROOT / "lambda" / "kubectl-applier-simple")
        sys.path.insert(0, handler_path)
        try:
            sys.modules.pop("handler", None)
            import handler

            yield handler
        finally:
            sys.path.pop(0)
            sys.modules.pop("handler", None)

    def test_prune_inventory_removes_claim_and_policy_when_disabled(self, applier):
        targets = applier._FEATURE_RESOURCE_INVENTORY[("{{MLFLOW_ENABLED}}", True)]
        assert targets == (
            ("v1", "PersistentVolumeClaim", "monitoring", "mlflow"),
            ("networking.k8s.io/v1", "NetworkPolicy", "gco-jobs", "allow-mlflow-clients"),
        )

    def test_inventory_covers_manifest_resources_plus_the_chart_claim(self, applier):
        # Everything the gated manifest ships must be pruned, plus exactly
        # one resource that is deliberately NOT in a manifest: the
        # chart-managed metadata claim (fullnameOverride name), which helm
        # uninstall never deletes.
        rendered = _NETWORK_MANIFEST.read_text(encoding="utf-8").replace(
            "{{MLFLOW_ENABLED}}", "true"
        )
        manifest_resources = {
            (doc["kind"], doc["metadata"]["name"]) for doc in yaml.safe_load_all(rendered) if doc
        }
        pruned = {
            (kind, name)
            for _, kind, _, name in applier._FEATURE_RESOURCE_INVENTORY[
                ("{{MLFLOW_ENABLED}}", True)
            ]
        }
        assert pruned == manifest_resources | {("PersistentVolumeClaim", "mlflow")}

    def test_chart_claim_prune_name_matches_the_fullname_override(self, applier, charts):
        # The pruned claim name is whatever the chart names its PVC — the
        # release fullname. If fullnameOverride ever changes, this entry
        # must move with it or disable-time pruning silently leaks the
        # volume.
        (claim_entry,) = [
            entry
            for entry in applier._FEATURE_RESOURCE_INVENTORY[("{{MLFLOW_ENABLED}}", True)]
            if entry[1] == "PersistentVolumeClaim"
        ]
        assert claim_entry[2] == "monitoring"
        assert claim_entry[3] == charts["mlflow"]["values"]["fullnameOverride"]


class TestTunnelServiceEntries:
    def test_monitoring_open_exposes_the_tracking_server(self):
        from cli.commands.monitoring_cmd import _SERVICES

        mlflow = _SERVICES["mlflow"]
        assert mlflow["target"] == "svc/mlflow"
        assert mlflow["remote_port"] == 5000
        # MLflow's canonical local port; must not collide with the Grafana
        # (3000), Prometheus (9090), or OpenCost (9091) defaults.
        assert mlflow["default_local_port"] == 5000
        taken = {
            name: service["default_local_port"]
            for name, service in _SERVICES.items()
            if name != "mlflow"
        }
        assert mlflow["default_local_port"] not in taken.values()


class TestHelmInstallerConvergence:
    """handle_task converges the mlflow chart in both directions."""

    @pytest.fixture(scope="class")
    def helm_handler(self):
        return load_lambda_module("helm-installer")

    def _event(self, enabled: bool) -> dict[str, Any]:
        return {
            "Action": "install_chart",
            "Chart": "mlflow",
            "ClusterName": "gco-us-east-1",
            "Region": "us-east-1",
            "EnabledCharts": ["keda", "mlflow"] if enabled else ["keda"],
            "Charts": {},
        }

    def test_enabled_chart_installs(self, helm_handler):
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(
                helm_handler,
                "install_chart",
                return_value=(True, "Successfully installed mlflow"),
            ) as mock_install,
            patch.object(helm_handler.os, "remove"),
        ):
            result = helm_handler.handle_task(self._event(enabled=True))

        assert result["status"] == "installed"
        assert result["chart"] == "mlflow"
        mock_install.assert_called_once()

    def test_disabled_chart_uninstalls_on_the_same_pass(self, helm_handler):
        # EnabledCharts is the runtime authority: flipping the cdk.json
        # sub-toggle off removes the tracking server on the next deploy.
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(
                helm_handler,
                "uninstall_chart",
                return_value=(True, "Successfully uninstalled"),
            ) as mock_uninstall,
            patch.object(helm_handler, "install_chart") as mock_install,
            patch.object(helm_handler.os, "remove"),
        ):
            result = helm_handler.handle_task(self._event(enabled=False))

        assert result["status"] == "uninstalled"
        mock_uninstall.assert_called_once()
        mock_install.assert_not_called()

    def test_mlflow_is_not_a_finalizer_purge_chart(self, helm_handler):
        """The tracking server creates no custom resources, so uninstall
        needs no kueue-style pre-purge."""
        assert "mlflow" not in helm_handler.CHART_CUSTOM_RESOURCE_API_GROUPS
