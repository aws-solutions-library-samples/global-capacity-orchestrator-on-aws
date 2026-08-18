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
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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
    / "25-storage-observability-gp3.yaml"
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

    The stub carries the real ``ConfigLoader`` and node plus the mandatory LBC
    runtime identities consumed by ``_helm_chart_value_overrides``. The
    observability helper is bound so the override builder can call it through
    the stub.
    """
    ctx = copy.deepcopy(context)
    obs: dict[str, Any] = {"enabled": enabled}
    if observability:
        obs.update(observability)
    ctx["cluster_observability"] = obs
    app = _MockApp(ctx)
    config = ConfigLoader(app)
    stub = SimpleNamespace(
        config=config,
        node=app.node,
        volcano_mirror_registry=None,
        cluster=SimpleNamespace(cluster_name="test-cluster"),
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
    # Cost monitoring and MLflow helpers ride the same stub: the override
    # builder and the chart-enable list both consult their conjunctions.
    stub._cost_monitoring_active = lambda: RS._cost_monitoring_active(stub)
    stub._opencost_chart_values = lambda: RS._opencost_chart_values(stub)
    stub._mlflow_active = lambda: RS._mlflow_active(stub)
    stub._mlflow_chart_values = lambda: RS._mlflow_chart_values(stub)
    return stub


# --- kubectl-applier gate helper (pure) --------------------------------------


def test_gate_helper_populates_placeholder_when_enabled() -> None:
    assert _compute_kubectl_observability_replacements(
        True, grafana_admin_password_rotation_schedule="0 4 1 * *"
    ) == {
        "{{CLUSTER_OBSERVABILITY_ENABLED}}": "true",
        "{{GRAFANA_ADMIN_PASSWORD_ROTATION_SCHEDULE}}": "0 4 1 * *",
    }


def test_gate_helper_carries_rotation_schedule_verbatim() -> None:
    # The configured cron flows straight into the CronJob schedule placeholder.
    repl = _compute_kubectl_observability_replacements(
        True, grafana_admin_password_rotation_schedule="15 3 * * 0"
    )
    assert repl["{{GRAFANA_ADMIN_PASSWORD_ROTATION_SCHEDULE}}"] == "15 3 * * 0"


def test_gate_helper_empty_when_disabled() -> None:
    # Empty dict -> the manifests keep their unreplaced placeholders -> skipped.
    assert _compute_kubectl_observability_replacements(False) == {}
    assert (
        _compute_kubectl_observability_replacements(
            False, grafana_admin_password_rotation_schedule="0 4 1 * *"
        )
        == {}
    )


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


# --- property: chart + gp3 StorageClass invariant (CP-2) ---------------------


# The test deep-copies valid_cdk_context before mutating it, so the shared
# function-scoped fixture is never corrupted across generated inputs.
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    enabled=st.booleans(),
    regions=st.lists(
        st.sampled_from(["us-east-1", "us-east-2", "us-west-2", "eu-west-1", "ap-southeast-1"]),
        min_size=1,
        max_size=4,
        unique=True,
    ),
    grafana_size=st.integers(min_value=1, max_value=500).map(lambda n: f"{n}Gi"),
    prometheus_size=st.integers(min_value=1, max_value=2000).map(lambda n: f"{n}Gi"),
    alertmanager_size=st.integers(min_value=1, max_value=100).map(lambda n: f"{n}Gi"),
    retention=st.integers(min_value=1, max_value=365).map(lambda n: f"{n}d"),
)
def test_chart_and_storageclass_invariant_across_region_sets(
    valid_cdk_context,
    enabled: bool,
    regions: list[str],
    grafana_size: str,
    prometheus_size: str,
    alertmanager_size: str,
    retention: str,
) -> None:
    """CP-2: kube-prometheus-stack and its gp3-backed persistence are wired in
    exactly when observability is enabled, and that wiring is invariant to the
    deployment's region topology and to the configured sizes/retention.

    The chart-enable and value-override seams are per-stack and region-agnostic,
    so varying ``deployment_regions.regional`` must never change the outcome;
    the sizes/retention the operator sets must flow through verbatim.
    """
    ctx = copy.deepcopy(valid_cdk_context)
    ctx["deployment_regions"]["regional"] = regions
    stub = _stub(
        ctx,
        enabled=enabled,
        observability={
            "grafana": {"persistence_size": grafana_size},
            "prometheus": {"persistence_size": prometheus_size, "retention": retention},
            "alertmanager": {"persistence_size": alertmanager_size},
        },
    )

    charts = RS._get_enabled_helm_charts(stub)
    overrides = RS._helm_chart_value_overrides(stub)

    # Enabled-invariant: present iff enabled, for any region set.
    assert ("kube-prometheus-stack" in charts) is enabled
    assert ("kube-prometheus-stack" in overrides) is enabled

    if not enabled:
        return

    values = overrides["kube-prometheus-stack"]["values"]
    prom_spec = values["prometheus"]["prometheusSpec"]
    am_spec = values["alertmanager"]["alertmanagerSpec"]

    # The gp3 StorageClass backs all three stateful components.
    assert values["grafana"]["persistence"]["storageClassName"] == _OBSERVABILITY_STORAGE_CLASS
    assert (
        prom_spec["storageSpec"]["volumeClaimTemplate"]["spec"]["storageClassName"]
        == _OBSERVABILITY_STORAGE_CLASS
    )
    assert (
        am_spec["storage"]["volumeClaimTemplate"]["spec"]["storageClassName"]
        == _OBSERVABILITY_STORAGE_CLASS
    )

    # Configured sizes/retention flow through unchanged.
    assert values["grafana"]["persistence"]["size"] == grafana_size
    assert prom_spec["retention"] == retention
    assert (
        prom_spec["storageSpec"]["volumeClaimTemplate"]["spec"]["resources"]["requests"]["storage"]
        == prometheus_size
    )
    assert (
        am_spec["storage"]["volumeClaimTemplate"]["spec"]["resources"]["requests"]["storage"]
        == alertmanager_size
    )


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


def test_unscrapable_control_plane_monitors_disabled(kps_entry) -> None:
    # EKS owns etcd/scheduler/controller-manager, and Auto Mode runs no
    # kube-proxy DaemonSet and no CoreDNS pods (DNS is a built-in capability).
    # Each enabled monitor renders a kube-system Service whose selector can
    # never have ready endpoints, which fails live release validation.
    values = kps_entry["values"]
    for component in ("kubeEtcd", "kubeScheduler", "kubeControllerManager", "kubeProxy", "coreDns"):
        assert values[component]["enabled"] is False, component


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


def test_grafana_liveness_tolerates_first_boot_migrations(kps_entry) -> None:
    # Regression: Grafana's first-boot database migrations on a fresh PVC ran
    # past the subchart's default liveness budget (~2.5 min) on a live
    # cluster; kubelet killed it mid-migration and the pod crash-looped.
    # Liveness must allow >= 10 minutes before the first kill, and readiness
    # must stay on the subchart default (no override here).
    grafana = kps_entry["values"]["grafana"]
    liveness = grafana["livenessProbe"]
    assert liveness["httpGet"]["path"] == "/api/health"
    assert liveness["httpGet"]["port"] == 3000
    tolerance = (
        liveness["initialDelaySeconds"] + liveness["failureThreshold"] * liveness["periodSeconds"]
    )
    assert tolerance >= 600
    assert "readinessProbe" not in grafana


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
    assert {
        "health-monitor",
        "manifest-processor",
        "inference-monitor",
        "inference-proxy",
    } <= pm_apps


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


# --- regression guard: DaemonSets must never be karpenter do-not-disrupt ------
#
# Incident: idle GPU nodes sat un-terminated for hours and would have stayed up
# for the full 24h NodePool terminationGracePeriod. Root cause: charts.yaml set
# ``karpenter.sh/do-not-disrupt: "true"`` on the kube-prometheus-stack
# node-exporter. node-exporter is a DaemonSet that runs on EVERY node, so the
# annotation pins every node against graceful termination — blocking
# consolidation, scale-down, spot reclaim, and even manual NodeClaim deletion
# (Karpenter/EKS Auto Mode holds the node until the grace period elapses).
# do-not-disrupt is only for singleton pods you don't want voluntarily
# consolidated (Prometheus, Alertmanager, kube-state-metrics, the operator),
# never for a DaemonSet. These guards keep the annotation off every DaemonSet.

_DO_NOT_DISRUPT = "karpenter.sh/do-not-disrupt"
_MANIFESTS_DIR = _REPO_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"


def test_node_exporter_is_not_marked_do_not_disrupt(kps_entry) -> None:
    # node-exporter is the one DaemonSet in kube-prometheus-stack; it must stay
    # disruptible or it blocks graceful termination of every node in the cluster.
    node_exporter = kps_entry["values"].get("prometheus-node-exporter", {})
    annotations = node_exporter.get("podAnnotations", {})
    assert _DO_NOT_DISRUPT not in annotations, (
        "node-exporter is a DaemonSet; karpenter.sh/do-not-disrupt on it pins "
        "every node against graceful termination until the NodePool "
        "terminationGracePeriod. Remove it from prometheus-node-exporter in "
        "charts.yaml (see the comment there)."
    )


def test_no_daemonset_manifest_is_marked_do_not_disrupt() -> None:
    # Same hazard applied to the manifests GCO ships (dcgm-exporter, ...):
    # a DaemonSet must never carry do-not-disrupt.
    offenders: list[str] = []
    for path in sorted(_MANIFESTS_DIR.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        # Only parse files that actually declare a DaemonSet. Some manifests
        # (e.g. 03-network-policies.yaml) carry structural {{PLACEHOLDER}} tokens
        # that aren't valid YAML until the applier renders them; skipping
        # non-DaemonSet files avoids that and keeps the guard render-free.
        if "kind: DaemonSet" not in text:
            continue
        for doc in yaml.safe_load_all(text):
            if not doc or doc.get("kind") != "DaemonSet":
                continue
            template_annotations = (
                doc.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations") or {}
            )
            if _DO_NOT_DISRUPT in template_annotations:
                offenders.append(f"{path.name}:{doc.get('metadata', {}).get('name')}")
    assert not offenders, (
        "DaemonSet(s) carry karpenter.sh/do-not-disrupt, which blocks graceful "
        f"node termination on every node they run on: {offenders}"
    )
