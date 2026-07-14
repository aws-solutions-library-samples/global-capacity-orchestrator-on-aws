"""
Tests for the curated Grafana dashboard ConfigMaps
(lambda/kubectl-applier-simple/manifests/post-helm-grafana-dashboards.yaml).

The kube-prometheus-stack Grafana sidecar imports any ConfigMap in the cluster
carrying the ``grafana_dashboard: "1"`` label, so these tests assert that the
GCO-curated dashboards (GPU/DCGM, schedulers, KEDA, GCO services) each carry
that label, live in the chart-created ``monitoring`` namespace, are gated on
``{{CLUSTER_OBSERVABILITY_ENABLED}}`` so they are skipped when observability is
off, and embed valid dashboard JSON.

The last test is the regression guard tied to the kubectl-applier skip rule:
the only UPPER_SNAKE placeholder in the file must be the gate itself, so that
once the gate is substituted the dashboards contain no token matching
``_UNRESOLVED_PLACEHOLDER_RE`` and are therefore applied verbatim (Grafana
legend tokens like ``{{gpu}}`` are lower/mixed-case and must survive).
"""

import json
import re
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
DASHBOARDS_FILE = (
    PROJECT_ROOT
    / "lambda"
    / "kubectl-applier-simple"
    / "manifests"
    / "post-helm-grafana-dashboards.yaml"
)

GATE_ANNOTATION = "gco.io/cluster-observability-enabled"
GATE_PLACEHOLDER = "{{CLUSTER_OBSERVABILITY_ENABLED}}"

# Mirrors handler._UNRESOLVED_PLACEHOLDER_RE — a file still matching this after
# substitution is skipped by the applier.
_UPPER_SNAKE_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")

# The four curated dashboards, keyed by their embedded Grafana ``uid``.
EXPECTED_DASHBOARD_UIDS = {
    "gco-gpu-dcgm",
    "gco-schedulers",
    "gco-keda",
    "gco-services",
}


@pytest.fixture(scope="module")
def raw_text() -> str:
    return DASHBOARDS_FILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def configmaps(raw_text: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(raw_text) if doc]


def test_dashboards_file_is_a_post_helm_manifest() -> None:
    """Dashboards depend on the chart-created monitoring namespace + Grafana
    sidecar, so the file must ride the post-Helm pass (``post-helm-`` prefix)."""
    assert DASHBOARDS_FILE.is_file()
    assert DASHBOARDS_FILE.name.startswith("post-helm-")


def test_exactly_four_dashboard_configmaps(configmaps: list[dict]) -> None:
    assert len(configmaps) == 4
    assert all(cm["kind"] == "ConfigMap" for cm in configmaps)
    assert all(cm["apiVersion"] == "v1" for cm in configmaps)


def test_every_configmap_carries_the_sidecar_label(configmaps: list[dict]) -> None:
    """grafana_dashboard: "1" is what the Grafana sidecar watches for."""
    for cm in configmaps:
        labels = cm["metadata"].get("labels", {})
        assert labels.get("grafana_dashboard") == "1", cm["metadata"]["name"]


def test_every_configmap_lives_in_monitoring_namespace(configmaps: list[dict]) -> None:
    for cm in configmaps:
        assert cm["metadata"]["namespace"] == "monitoring", cm["metadata"]["name"]


def test_every_configmap_is_gated_on_observability(configmaps: list[dict]) -> None:
    """The gate annotation carries the UPPER_SNAKE placeholder so the applier
    skips the whole file when observability is disabled."""
    for cm in configmaps:
        annotations = cm["metadata"].get("annotations", {})
        assert annotations.get(GATE_ANNOTATION) == GATE_PLACEHOLDER, cm["metadata"]["name"]


def test_every_data_value_is_valid_dashboard_json(configmaps: list[dict]) -> None:
    """Each ConfigMap embeds exactly one dashboard JSON with title/uid/panels."""
    seen_uids = set()
    for cm in configmaps:
        data = cm["data"]
        assert len(data) == 1, f"{cm['metadata']['name']} should hold one dashboard JSON"
        for filename, blob in data.items():
            assert filename.endswith(".json"), filename
            dashboard = json.loads(blob)
            assert dashboard.get("title"), filename
            assert dashboard.get("uid"), filename
            assert isinstance(dashboard.get("panels"), list) and dashboard["panels"], filename
            seen_uids.add(dashboard["uid"])
    assert seen_uids == EXPECTED_DASHBOARD_UIDS


def test_only_gate_placeholder_is_upper_snake(raw_text: str) -> None:
    """After the gate placeholder is substituted, no UPPER_SNAKE token remains,
    so the applier will not skip the dashboards when observability is enabled.

    Regression guard against re-introducing a substitution placeholder into the
    dashboard bodies (which would silently drop every dashboard). Grafana legend
    tokens ({{gpu}}, {{service}}, {{Hostname}}) are deliberately lower/mixed-case
    and must not match this pattern.
    """
    substituted = raw_text.replace(GATE_PLACEHOLDER, "true")
    leftover = _UPPER_SNAKE_PLACEHOLDER_RE.findall(substituted)
    assert leftover == [], f"unexpected UPPER_SNAKE placeholders remain: {leftover}"
