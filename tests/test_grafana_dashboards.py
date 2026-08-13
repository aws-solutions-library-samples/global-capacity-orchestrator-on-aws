"""Static validation of the curated Grafana dashboard ConfigMaps.

The dashboards ship as JSON payloads embedded in ConfigMap manifests under
``lambda/kubectl-applier-simple/manifests/`` and are imported at runtime by
the kube-prometheus-stack Grafana sidecar, which surfaces a malformed
dashboard only as a silently missing UI entry. These tests parse the payloads
through the applier's real planning path — the same substitution and
feature-gating production uses — so a stray comma, a duplicate uid, or a
templating change that eats Grafana's ``{{...}}`` legend tokens fails CI
instead of shipping. The CI workflow additionally boots the pinned chart's
Grafana image and asserts it provisions these payloads; here we hold the
extraction script and the applier path in lockstep without any container.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFESTS_DIR = PROJECT_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"
SCRIPT_PATH = PROJECT_ROOT / ".github" / "scripts" / "validate_grafana_dashboards.py"

DASHBOARD_MANIFESTS = (
    "post-helm-grafana-dashboards.yaml",
    "post-helm-grafana-cost-dashboard.yaml",
)

# The applier gates files on unresolved {{UPPER_SNAKE}} placeholders; the
# replacement values themselves are irrelevant to dashboard structure.
REPLACEMENTS = {
    "{{CLUSTER_OBSERVABILITY_ENABLED}}": "true",
    "{{COST_MONITORING_ENABLED}}": "true",
}

# Grafana's dashboard grid is 24 columns wide and rejects uids longer than
# 40 characters on import.
GRID_COLUMNS = 24
UID_MAX_LENGTH = 40
UID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _load_script():
    """Load the extraction script by file path.

    ``.github/scripts`` is intentionally not a Python package, so import by
    path rather than adding an ``__init__.py`` — mirrors the helm-charts and
    k8s-manifest validator tests.
    """
    spec = importlib.util.spec_from_file_location("validate_grafana_dashboards", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load_script()


@pytest.fixture(scope="module")
def handler_module():
    """Import the kubectl-applier handler for its real planning path."""
    handler_path = str(PROJECT_ROOT / "lambda" / "kubectl-applier-simple")
    sys.path.insert(0, handler_path)
    try:
        sys.modules.pop("handler", None)
        import handler

        yield handler
    finally:
        sys.path.remove(handler_path)
        sys.modules.pop("handler", None)


@pytest.fixture(scope="module")
def planned_configmaps(
    handler_module, tmp_path_factory: pytest.TempPathFactory
) -> list[dict[str, Any]]:
    """The dashboard ConfigMaps as the applier's own planner sees them."""
    workdir = tmp_path_factory.mktemp("grafana-manifests")
    for name in DASHBOARD_MANIFESTS:
        shutil.copy(MANIFESTS_DIR / name, workdir / name)

    plan = handler_module.plan_manifests(str(workdir), REPLACEMENTS)

    # With both feature placeholders resolved, nothing may be gated out —
    # a skip here means a dashboard silently vanished from the deployment.
    assert plan["skipped"]["post-helm"] == []
    assert plan["featureGates"]["post-helm"] == []
    documents = [entry["document"] for entry in plan["phases"]["post-helm"]]
    assert documents, "the planner returned no dashboard ConfigMaps"
    return documents


@pytest.fixture(scope="module")
def dashboards(planned_configmaps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{data key: parsed dashboard}`` across every planned ConfigMap."""
    import json

    parsed: dict[str, dict[str, Any]] = {}
    for configmap in planned_configmaps:
        for key, payload in (configmap.get("data") or {}).items():
            parsed[key] = json.loads(payload)
    return parsed


# ---------------------------------------------------------------------------
# ConfigMap envelope
# ---------------------------------------------------------------------------


def test_every_configmap_carries_the_sidecar_import_contract(
    planned_configmaps: list[dict[str, Any]],
) -> None:
    for configmap in planned_configmaps:
        metadata = configmap["metadata"]
        labels = metadata.get("labels") or {}
        assert labels.get("grafana_dashboard") == "1", metadata["name"]
        assert metadata.get("namespace") == "monitoring", metadata["name"]
        data = configmap.get("data") or {}
        assert data, f"{metadata['name']} carries no dashboard payloads"
        for key in data:
            assert key.endswith(".json"), f"{metadata['name']} data key {key}"


def test_legend_template_tokens_survive_the_applier_substitution(
    dashboards: dict[str, dict[str, Any]],
) -> None:
    """Grafana's lowercase legend tokens must pass through planning intact.

    The applier resolves only ``{{UPPER_SNAKE}}`` feature placeholders; a
    regression that widens that substitution would blank every legend in the
    GPU dashboard.
    """
    gpu = dashboards["gco-gpu-dcgm.json"]
    legends = [
        target.get("legendFormat", "")
        for panel in gpu["panels"]
        for target in panel.get("targets", [])
    ]
    assert any("{{Hostname}}" in legend for legend in legends)
    assert any("{{gpu}}" in legend for legend in legends)

    cost = dashboards["gco-cost.json"]
    cost_legends = [
        target.get("legendFormat", "")
        for panel in cost["panels"]
        for target in panel.get("targets", [])
    ]
    assert any("{{namespace}}" in legend for legend in cost_legends)


# ---------------------------------------------------------------------------
# Dashboard payloads
# ---------------------------------------------------------------------------


def test_every_payload_parses_and_carries_identity(
    dashboards: dict[str, dict[str, Any]],
) -> None:
    assert len(dashboards) >= 5
    for key, dashboard in dashboards.items():
        assert isinstance(dashboard.get("title"), str) and dashboard["title"], key
        assert isinstance(dashboard.get("uid"), str) and dashboard["uid"], key
        assert isinstance(dashboard.get("schemaVersion"), int), key


def test_uids_are_unique_and_grafana_acceptable(dashboards: dict[str, dict[str, Any]]) -> None:
    uids = [dashboard["uid"] for dashboard in dashboards.values()]
    assert len(uids) == len(set(uids)), f"duplicate dashboard uids: {uids}"
    for uid in uids:
        assert len(uid) <= UID_MAX_LENGTH, uid
        assert UID_PATTERN.match(uid), uid


def test_titles_are_unique(dashboards: dict[str, dict[str, Any]]) -> None:
    titles = [dashboard["title"] for dashboard in dashboards.values()]
    assert len(titles) == len(set(titles)), f"duplicate dashboard titles: {titles}"


def test_panels_have_types_unique_ids_and_grid_positions(
    dashboards: dict[str, dict[str, Any]],
) -> None:
    for key, dashboard in dashboards.items():
        panels = dashboard.get("panels")
        assert isinstance(panels, list) and panels, f"{key} has no panels"
        seen_ids: set[int] = set()
        for panel in panels:
            where = f"{key} panel {panel.get('id')!r}"
            assert isinstance(panel.get("id"), int), where
            assert panel["id"] not in seen_ids, f"{where}: duplicate panel id"
            seen_ids.add(panel["id"])
            assert isinstance(panel.get("type"), str) and panel["type"], where
            assert isinstance(panel.get("title"), str) and panel["title"], where
            grid = panel.get("gridPos")
            assert isinstance(grid, dict), f"{where}: missing gridPos"
            for field in ("h", "w", "x", "y"):
                assert isinstance(grid.get(field), int), f"{where}: gridPos.{field}"
            assert grid["h"] >= 1 and grid["w"] >= 1, where
            assert grid["x"] >= 0 and grid["y"] >= 0, where
            assert grid["x"] + grid["w"] <= GRID_COLUMNS, (
                f"{where}: panel overflows the {GRID_COLUMNS}-column grid"
            )


def test_every_target_carries_a_promql_expression(
    dashboards: dict[str, dict[str, Any]],
) -> None:
    for key, dashboard in dashboards.items():
        for panel in dashboard["panels"]:
            targets = panel.get("targets")
            assert isinstance(targets, list) and targets, f"{key} panel {panel['id']}"
            for target in targets:
                expr = target.get("expr")
                assert isinstance(expr, str) and expr.strip(), (
                    f"{key} panel {panel['id']}: target without a PromQL expr"
                )


# ---------------------------------------------------------------------------
# Extraction-script lockstep
# ---------------------------------------------------------------------------


def test_extraction_script_agrees_with_the_applier_path(
    dashboards: dict[str, dict[str, Any]],
) -> None:
    """The CI extraction must see exactly the dashboards the applier plans."""
    extracted = script.extract_dashboards([MANIFESTS_DIR / name for name in DASHBOARD_MANIFESTS])
    assert set(extracted) == {dashboard["uid"] for dashboard in dashboards.values()}
    for uid, dashboard in extracted.items():
        assert dashboard["title"], uid


def test_chart_pin_is_readable_for_the_ci_image_resolution() -> None:
    pin = script.read_chart_pin(PROJECT_ROOT / "lambda" / "helm-installer" / "charts.yaml")
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin["version"]), pin
    assert pin["repo_url"].startswith("https://"), pin


def test_extraction_rejects_a_malformed_payload(tmp_path: Path) -> None:
    """The whole point: a stray comma must fail loudly, not ship silently."""
    manifest = tmp_path / "post-helm-grafana-broken.yaml"
    manifest.write_text(
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: gco-dashboard-broken\n"
        "  namespace: monitoring\n"
        "  labels:\n"
        '    grafana_dashboard: "1"\n'
        "data:\n"
        "  broken.json: |\n"
        '    {"title": "Broken", "uid": "broken",}\n',
        encoding="utf-8",
    )

    with pytest.raises(script.ValidationError, match="invalid JSON"):
        script.extract_dashboards([manifest])
