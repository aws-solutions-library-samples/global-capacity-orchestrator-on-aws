"""Tests for the regional wiring of the in-cluster observability chart.

Exercises the regional-stack seams that decide whether kube-prometheus-stack is
installed and how it is configured, without synthesizing a full CDK stack
(which would need Docker for the Lambda image assets). The chart-enable
membership, the value-override builder, and the kubectl-applier gate helper are
plain methods/functions, so they are driven directly against a lightweight stub
``self`` backed by a real ``ConfigLoader``.

Also asserts the static contract that ties the pieces together: the gp3
StorageClass manifest's name matches the Python constant the chart values point
at, the manifest is gated by the same placeholder the helper emits, and the
charts.yaml entry ships the private/native-auth hardening defaults.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from gco.config.config_loader import ConfigLoader
from gco.stacks.regional_stack import (
    _OBSERVABILITY_STORAGE_CLASS,
    _compute_kubectl_observability_replacements,
)
from gco.stacks.regional_stack import (
    GCORegionalStack as RS,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHARTS_YAML = _REPO_ROOT / "lambda" / "helm-installer" / "charts.yaml"
_GP3_MANIFEST = (
    _REPO_ROOT
    / "lambda"
    / "kubectl-applier-simple"
    / "manifests"
    / "24-storage-observability-gp3.yaml"
)


class _MockNode:
    def __init__(self, context: dict[str, Any]):
        self._context = context

    def try_get_context(self, key: str) -> Any:
        return self._context.get(key)


class _MockApp:
    def __init__(self, context: dict[str, Any]):
        self.node = _MockNode(context)


def _stub(context: dict[str, Any], *, enabled: bool, observability: dict[str, Any] | None = None):
    """Build a lightweight stand-in for a GCORegionalStack instance.

    Only the attributes the methods under test touch are populated: ``config``
    (a real ConfigLoader), ``node`` (for the helm block), and
    ``volcano_mirror_registry``. ``_observability_chart_values`` is bound so
    ``_helm_chart_value_overrides`` can call it through the stub.
    """
    ctx = copy.deepcopy(context)
    obs: dict[str, Any] = {"enabled": enabled}
    if observability:
        obs.update(observability)
    ctx["cluster_observability"] = obs
    app = _MockApp(ctx)
    config = ConfigLoader(app)
    stub = SimpleNamespace(config=config, node=app.node, volcano_mirror_registry=None)
    stub._observability_chart_values = lambda: RS._observability_chart_values(stub)
    return stub


# --- kubectl-applier gate helper (pure) --------------------------------------


def test_gate_helper_populates_placeholder_when_enabled() -> None:
    assert _compute_kubectl_observability_replacements(True) == {
        "{{CLUSTER_OBSERVABILITY_ENABLED}}": "true"
    }


def test_gate_helper_empty_when_disabled() -> None:
    # Empty dict -> the manifest keeps its unreplaced placeholder -> skipped.
    assert _compute_kubectl_observability_replacements(False) == {}


# --- chart-enable membership -------------------------------------------------


def test_chart_enabled_when_toggle_on(valid_cdk_context) -> None:
    charts = RS._get_enabled_helm_charts(_stub(valid_cdk_context, enabled=True))
    assert "kube-prometheus-stack" in charts


def test_chart_absent_when_toggle_off(valid_cdk_context) -> None:
    charts = RS._get_enabled_helm_charts(_stub(valid_cdk_context, enabled=False))
    assert "kube-prometheus-stack" not in charts


def test_keda_still_mandatory_regardless_of_observability(valid_cdk_context) -> None:
    charts = RS._get_enabled_helm_charts(_stub(valid_cdk_context, enabled=False))
    assert "keda" in charts


# --- value overrides ---------------------------------------------------------


def test_overrides_include_chart_when_enabled(valid_cdk_context) -> None:
    overrides = RS._helm_chart_value_overrides(_stub(valid_cdk_context, enabled=True))
    assert "kube-prometheus-stack" in overrides


def test_overrides_exclude_chart_when_disabled(valid_cdk_context) -> None:
    overrides = RS._helm_chart_value_overrides(_stub(valid_cdk_context, enabled=False))
    assert "kube-prometheus-stack" not in overrides


def test_override_wires_storage_class_and_sizes(valid_cdk_context) -> None:
    values = RS._observability_chart_values(_stub(valid_cdk_context, enabled=True))["values"]
    assert values["grafana"]["persistence"]["storageClassName"] == _OBSERVABILITY_STORAGE_CLASS
    prom = values["prometheus"]["prometheusSpec"]
    assert prom["retention"] == "15d"
    assert (
        prom["storageSpec"]["volumeClaimTemplate"]["spec"]["storageClassName"]
        == _OBSERVABILITY_STORAGE_CLASS
    )
    assert (
        prom["storageSpec"]["volumeClaimTemplate"]["spec"]["resources"]["requests"]["storage"]
        == "50Gi"
    )


def test_override_reflects_config_overrides(valid_cdk_context) -> None:
    stub = _stub(
        valid_cdk_context,
        enabled=True,
        observability={"prometheus": {"retention": "30d", "persistence_size": "100Gi"}},
    )
    values = RS._observability_chart_values(stub)["values"]
    prom = values["prometheus"]["prometheusSpec"]
    assert prom["retention"] == "30d"
    assert (
        prom["storageSpec"]["volumeClaimTemplate"]["spec"]["resources"]["requests"]["storage"]
        == "100Gi"
    )


def test_node_exporter_tolerates_accelerator_taints(valid_cdk_context) -> None:
    values = RS._observability_chart_values(_stub(valid_cdk_context, enabled=True))["values"]
    tolerations = values["prometheus-node-exporter"]["tolerations"]
    keys = {t.get("key") for t in tolerations}
    assert "nvidia.com/gpu" in keys
    assert "aws.amazon.com/neuron" in keys


# --- gp3 StorageClass manifest -----------------------------------------------


@pytest.fixture
def gp3_manifest() -> dict[str, Any]:
    return yaml.safe_load(_GP3_MANIFEST.read_text(encoding="utf-8"))


def test_gp3_manifest_name_matches_constant(gp3_manifest) -> None:
    # The manifest name is static (a placeholder in metadata.name would fail
    # k8s schema validation); this lockstep check prevents silent drift.
    assert gp3_manifest["metadata"]["name"] == _OBSERVABILITY_STORAGE_CLASS


def test_gp3_manifest_is_gated_by_toggle_placeholder(gp3_manifest) -> None:
    annotations = gp3_manifest["metadata"]["annotations"]
    assert (
        annotations["gco.io/cluster-observability-enabled"] == "{{CLUSTER_OBSERVABILITY_ENABLED}}"
    )


def test_gp3_manifest_uses_auto_mode_provisioner(gp3_manifest) -> None:
    assert gp3_manifest["provisioner"] == "ebs.csi.eks.amazonaws.com"
    assert gp3_manifest["volumeBindingMode"] == "WaitForFirstConsumer"
    assert gp3_manifest["parameters"]["type"] == "gp3"
    assert gp3_manifest["parameters"]["encrypted"] == "true"


# --- charts.yaml static hardening defaults -----------------------------------


@pytest.fixture
def kps_entry() -> dict[str, Any]:
    charts = yaml.safe_load(_CHARTS_YAML.read_text(encoding="utf-8"))["charts"]
    return charts["kube-prometheus-stack"]


def test_chart_entry_defaults(kps_entry) -> None:
    assert kps_entry["enabled"] is False  # inclusion is driven by the toggle, not the helm block
    assert kps_entry["namespace"] == "monitoring"
    assert kps_entry["create_namespace"] is True
    assert kps_entry["wait"] is False


def test_grafana_is_private_and_native_auth(kps_entry) -> None:
    grafana = kps_entry["values"]["grafana"]
    assert grafana["service"]["type"] == "ClusterIP"
    ini = grafana["grafana.ini"]
    assert ini["users"]["allow_sign_up"] is False
    assert ini["auth.anonymous"]["enabled"] is False


def test_no_admin_password_literal_in_values(kps_entry) -> None:
    # The Grafana subchart auto-generates the admin Secret; nothing here sets a
    # password or points at an operator-authored existingSecret.
    grafana = kps_entry["values"]["grafana"]
    blob = yaml.safe_dump(grafana)
    assert "adminPassword" not in blob
    assert "admin_password" not in blob
    assert "existingSecret" not in blob


def test_secure_cookies_enabled(kps_entry) -> None:
    security = kps_entry["values"]["grafana"]["grafana.ini"]["security"]
    assert security["cookie_secure"] is True
    assert security["cookie_samesite"] == "lax"


def test_no_component_is_internet_facing(kps_entry) -> None:
    # Private-only: every component Service is ClusterIP and Grafana has no
    # Ingress, so enabling observability creates no public endpoint.
    values = kps_entry["values"]
    assert values["grafana"]["service"]["type"] == "ClusterIP"
    assert values["grafana"]["ingress"]["enabled"] is False
    assert values["prometheus"]["service"]["type"] == "ClusterIP"
    assert values["alertmanager"]["service"]["type"] == "ClusterIP"


def test_no_loadbalancer_anywhere_in_entry(kps_entry) -> None:
    # Belt-and-suspenders CP-3 check: the entire entry never asks for a
    # LoadBalancer or a NodePort Service, and never enables an Ingress.
    blob = yaml.safe_dump(kps_entry)
    assert "LoadBalancer" not in blob
    assert "NodePort" not in blob


def test_no_credential_literal_anywhere_in_entry(kps_entry) -> None:
    # CP-4 across the whole entry, not just the grafana sub-block.
    blob = yaml.safe_dump(kps_entry).lower()
    for needle in ("adminpassword", "admin_password", "existingsecret", "password:"):
        assert needle not in blob, needle


def test_grafana_persistence_enabled(kps_entry) -> None:
    assert kps_entry["values"]["grafana"]["persistence"]["enabled"] is True


# --- ServiceMonitors for scheduler/operator components -----------------------

_SERVICEMONITORS = (
    _REPO_ROOT
    / "lambda"
    / "kubectl-applier-simple"
    / "manifests"
    / "post-helm-monitoring-servicemonitors.yaml"
)


@pytest.fixture
def servicemonitor_docs() -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(_SERVICEMONITORS.read_text(encoding="utf-8")) if doc]


def test_servicemonitors_is_post_helm() -> None:
    # ServiceMonitor CRDs are installed by the chart, so the monitors must be
    # applied in the post-Helm pass — encoded by the post-helm- filename prefix.
    assert _SERVICEMONITORS.name.startswith("post-helm-")


def test_monitors_cover_schedulers_dcgm_and_gco_services(servicemonitor_docs) -> None:
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for doc in servicemonitor_docs:
        by_kind.setdefault(doc["kind"], []).append(doc)

    # Prometheus-native scheduler/operator components + DCGM front their metrics
    # with a Service, so they are scraped via ServiceMonitors.
    sm_namespaces = {
        ns
        for doc in by_kind.get("ServiceMonitor", [])
        for ns in doc["spec"]["namespaceSelector"]["matchNames"]
    }
    assert {
        "keda",
        "volcano-system",
        "kueue-system",
        "ray-system",
        "yunikorn",
        "kube-system",
    } <= sm_namespaces

    # GCO's own multi-replica services are scraped per-pod via PodMonitors.
    pm_apps = {
        value
        for doc in by_kind.get("PodMonitor", [])
        for key, value in doc["spec"]["selector"]["matchLabels"].items()
        if key == "app"
    }
    assert {"health-monitor", "manifest-processor", "inference-monitor"} <= pm_apps


def test_monitors_are_gated_and_well_formed(servicemonitor_docs) -> None:
    assert servicemonitor_docs, "expected at least one monitor"
    for doc in servicemonitor_docs:
        assert doc["kind"] in {"ServiceMonitor", "PodMonitor"}
        annotations = doc["metadata"]["annotations"]
        assert (
            annotations["gco.io/cluster-observability-enabled"]
            == "{{CLUSTER_OBSERVABILITY_ENABLED}}"
        )
        assert doc["metadata"]["namespace"] == "monitoring"
        # A selector and at least one scrape endpoint make it a usable monitor.
        assert doc["spec"]["selector"]["matchLabels"]
        endpoints = doc["spec"].get("endpoints") or doc["spec"].get("podMetricsEndpoints")
        assert endpoints
