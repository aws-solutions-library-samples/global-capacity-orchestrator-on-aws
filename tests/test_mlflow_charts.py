"""
Tests for MLflow chart wiring, the backend PVC manifest, and gating.

Mirrors tests/test_cost_opencost_charts.py for the experiment-tracking
pipeline: the static mlflow entry in charts.yaml (chart + image pins in
lockstep, Recreate singleton on a ReadWriteOnce PVC, ClusterIP/no-ingress/
no-app-auth posture, telemetry off), the GCORegionalStack enablement under
the cluster_observability.mlflow conjunction in both directions, the value
overrides that carry the S3 artifact destination and IRSA role annotation,
the {{MLFLOW_ENABLED}}-gated backend PVC and its prune inventory entry,
the tunnel service entry, and helm-installer handle_task convergence.
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
_BACKEND_MANIFEST = (
    _REPO_ROOT / "lambda" / "kubectl-applier-simple" / "manifests" / "post-helm-mlflow-backend.yaml"
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
    def test_chart_is_defined_with_pinned_version(self, charts):
        mlflow = charts["mlflow"]
        assert mlflow["repo_url"] == "https://community-charts.github.io/helm-charts"
        assert mlflow["chart"] == "mlflow"
        assert re.fullmatch(r"\d+\.\d+\.\d+", mlflow["version"])

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

    def test_app_image_tag_is_pinned(self, charts):
        image = charts["mlflow"]["values"]["image"]
        assert image["repository"] == "burakince/mlflow"
        # The chart defaults the tag to its appVersion; the explicit pin
        # feeds the deps-drift report. Bumped in lockstep with `version`.
        assert re.fullmatch(r"\d+\.\d+\.\d+", image["tag"])

    def test_singleton_recreate_strategy_for_the_rwo_volume(self, charts):
        # RollingUpdate with surge would deadlock on the ReadWriteOnce EBS
        # attach, and two servers must never share one SQLite file.
        assert charts["mlflow"]["values"]["strategy"]["type"] == "Recreate"

    def test_singleton_pod_is_protected_from_consolidation(self, charts):
        annotations = charts["mlflow"]["values"]["podAnnotations"]
        assert annotations["karpenter.sh/do-not-disrupt"] == "true"

    def test_service_is_cluster_ip_with_no_ingress(self, charts):
        values = charts["mlflow"]["values"]
        assert values["service"]["type"] == "ClusterIP"
        assert values["ingress"]["enabled"] is False

    def test_usage_telemetry_stays_off(self, charts):
        assert charts["mlflow"]["values"]["telemetry"]["enabled"] is False

    def test_sqlite_backend_lives_on_the_mounted_claim(self, charts):
        values = charts["mlflow"]["values"]
        assert values["backendStore"]["defaultSqlitePath"] == "/mlflow-data/mlflow.db"
        (volume,) = values["extraVolumes"]
        assert volume["persistentVolumeClaim"]["claimName"] == "gco-mlflow-backend"
        (mount,) = values["extraVolumeMounts"]
        assert mount["name"] == volume["name"]
        assert mount["mountPath"] == "/mlflow-data"

    def test_no_artifact_or_credential_values_are_static(self, charts):
        # S3 destination + IRSA annotation are deployment tokens injected by
        # the regional stack; keeping them out of charts.yaml means the
        # offline/online chart validators render without placeholders and a
        # copy-paste deploy cannot ship another account's role.
        values = charts["mlflow"]["values"]
        assert "artifactRoot" not in values
        assert "serviceAccount" not in values

    def test_resources_are_bounded(self, charts):
        resources = charts["mlflow"]["values"]["resources"]
        assert resources["requests"]["cpu"]
        assert resources["limits"]["memory"]

    def test_service_monitor_feeds_prometheus(self, charts):
        assert charts["mlflow"]["values"]["serviceMonitor"]["enabled"] is True


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

    def test_overrides_inject_the_artifact_destination_and_role(self, valid_cdk_context):
        overrides = RS._helm_chart_value_overrides(_stub(valid_cdk_context))
        values = overrides["mlflow"]["values"]
        s3 = values["artifactRoot"]["s3"]
        assert s3["enabled"] is True
        assert s3["bucket"] == "gco-cluster-shared-123456789012-us-east-2"
        # Region-suffixed prefix: each regional tracking server numbers
        # experiments independently, so a shared root would interleave
        # unrelated runs' artifacts.
        assert s3["path"] == "mlflow-artifacts/us-east-1"
        annotation = values["serviceAccount"]["annotations"]["eks.amazonaws.com/role-arn"]
        assert annotation == "arn:aws:iam::123456789012:role/test-mlflow"

    def test_overrides_exclude_mlflow_when_disabled(self, valid_cdk_context):
        overrides = RS._helm_chart_value_overrides(_stub(valid_cdk_context, mlflow_enabled=False))
        assert "mlflow" not in overrides


class TestMlflowBackendManifest:
    @pytest.fixture(scope="class")
    def manifest_text(self) -> str:
        return _BACKEND_MANIFEST.read_text(encoding="utf-8")

    def test_manifest_is_gated_on_the_mlflow_placeholder(self, manifest_text):
        assert "{{MLFLOW_ENABLED}}" in manifest_text
        assert "{{MLFLOW_BACKEND_SIZE}}" in manifest_text

    def test_no_other_upper_snake_tokens_leak(self, manifest_text):
        body = manifest_text.replace("{{MLFLOW_ENABLED}}", "").replace(
            "{{MLFLOW_BACKEND_SIZE}}", ""
        )
        assert not re.search(r"\{\{[A-Z][A-Z0-9_]*\}\}", body)

    @staticmethod
    def _rendered_docs(manifest_text: str) -> dict[str, dict[str, Any]]:
        rendered = manifest_text.replace("{{MLFLOW_ENABLED}}", "true").replace(
            "{{MLFLOW_BACKEND_SIZE}}", "10Gi"
        )
        return {doc["kind"]: doc for doc in yaml.safe_load_all(rendered) if doc}

    def test_manifest_renders_the_backend_claim(self, manifest_text):
        claim = self._rendered_docs(manifest_text)["PersistentVolumeClaim"]
        assert claim["metadata"]["name"] == "gco-mlflow-backend"
        assert claim["metadata"]["namespace"] == "monitoring"
        assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
        assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"

    def test_claim_uses_the_observability_storage_class(self, manifest_text):
        claim = self._rendered_docs(manifest_text)["PersistentVolumeClaim"]
        # Lockstep with the gated StorageClass manifest and the constant the
        # stack injects into kube-prometheus-stack values. The conjunction
        # guarantees the class exists whenever this claim applies.
        assert claim["spec"]["storageClassName"] == _OBSERVABILITY_STORAGE_CLASS

    def test_claim_name_matches_the_chart_volume_wiring(self, manifest_text, charts):
        claim = self._rendered_docs(manifest_text)["PersistentVolumeClaim"]
        (volume,) = charts["mlflow"]["values"]["extraVolumes"]
        assert claim["metadata"]["name"] == volume["persistentVolumeClaim"]["claimName"]

    def test_client_egress_policy_targets_opted_in_pods_only(self, manifest_text):
        """gco-jobs is egress-isolated; only labeled pods may reach the
        tracking server, and only the tracking server's pods."""
        policy = self._rendered_docs(manifest_text)["NetworkPolicy"]
        assert policy["metadata"]["namespace"] == "gco-jobs"
        assert policy["spec"]["podSelector"]["matchLabels"] == {
            "gco.io/mlflow-client": "true"
        }
        assert policy["spec"]["policyTypes"] == ["Egress"]
        (egress,) = policy["spec"]["egress"]
        (to,) = egress["to"]
        assert to["namespaceSelector"]["matchLabels"] == {
            "kubernetes.io/metadata.name": "monitoring"
        }
        assert to["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "mlflow"}
        ports = {entry["port"] for entry in egress["ports"]}
        # 5000 = mlflow container port (post-DNAT); 80 = Service port.
        assert ports == {5000, 80}

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

    def test_prune_inventory_removes_the_backend_claim_when_disabled(self, applier):
        targets = applier._FEATURE_RESOURCE_INVENTORY[("{{MLFLOW_ENABLED}}", True)]
        assert targets == (
            ("v1", "PersistentVolumeClaim", "monitoring", "gco-mlflow-backend"),
            ("networking.k8s.io/v1", "NetworkPolicy", "gco-jobs", "allow-mlflow-clients"),
        )

    def test_inventory_matches_the_gated_manifest_resources(self, applier):
        rendered = (
            _BACKEND_MANIFEST.read_text(encoding="utf-8")
            .replace("{{MLFLOW_ENABLED}}", "true")
            .replace("{{MLFLOW_BACKEND_SIZE}}", "10Gi")
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
        assert manifest_resources == pruned


class TestTunnelServiceEntries:
    def test_monitoring_open_exposes_the_tracking_server(self):
        from cli.commands.monitoring_cmd import _SERVICES

        mlflow = _SERVICES["mlflow"]
        assert mlflow["target"] == "svc/mlflow"
        assert mlflow["remote_port"] == 80
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
