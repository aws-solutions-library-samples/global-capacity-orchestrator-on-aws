"""
Tests for the monitoring dashboard screenshot tooling
(scripts/capture_monitoring_screenshots.py + the repo ``images/`` directory).

Real Grafana screenshots need a live cluster, so instead of asserting binary
images exist, this pins the invariants that keep the doc assets honest:

- the capture script's target dashboards stay in lockstep with the dashboard
  ConfigMaps actually shipped (add a dashboard and this fails until the script
  is updated),
- every output path is a ``.png`` under the repo ``images/`` directory,
- the docs reference both the capture script and the captured images.

``main()`` is exercised with the Playwright-driven ``capture`` mocked out, so
the CLI wiring is covered without a browser.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "capture_monitoring_screenshots.py"
_MANIFESTS_DIR = PROJECT_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"
# Every manifest that ships sidecar-imported Grafana dashboard ConfigMaps.
DASHBOARD_MANIFESTS = (
    _MANIFESTS_DIR / "post-helm-grafana-dashboards.yaml",
    _MANIFESTS_DIR / "post-helm-grafana-cost-dashboard.yaml",
)
IMAGES_DIR = PROJECT_ROOT / "images"
MONITORING_DOC = PROJECT_ROOT / "docs" / "MONITORING.md"


def _load_script():
    spec = importlib.util.spec_from_file_location("capture_monitoring_screenshots", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec so the module's @dataclass can resolve its own module
    # via sys.modules (Python 3.14 dataclass introspection requires this).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script()


def _dashboard_uids_from_manifest() -> set[str]:
    uids: set[str] = set()
    for manifest in DASHBOARD_MANIFESTS:
        for doc in yaml.safe_load_all(manifest.read_text()):
            if not doc:
                continue
            for blob in doc.get("data", {}).values():
                uids.add(json.loads(blob)["uid"])
    return uids


def test_screenshots_cover_exactly_the_shipped_dashboards(script) -> None:
    """The capture list must match the dashboards actually shipped — no more, no
    less — so a new dashboard cannot silently ship without a screenshot target."""
    captured_uids = {shot.dashboard_uid for shot in script.SCREENSHOTS}
    assert captured_uids == _dashboard_uids_from_manifest()


def test_output_paths_are_pngs_under_images_dir(script) -> None:
    paths = script.expected_output_paths()
    assert len(paths) == len(script.SCREENSHOTS)
    for path in paths:
        assert path.suffix == ".png"
        assert path.parent == IMAGES_DIR


def test_screenshot_filenames_are_unique(script) -> None:
    filenames = [shot.filename for shot in script.SCREENSHOTS]
    assert len(filenames) == len(set(filenames))


def test_docs_reference_the_capture_script() -> None:
    text = MONITORING_DOC.read_text()
    assert "scripts/capture_monitoring_screenshots.py" in text
    assert "images/grafana-gpu-dcgm.png" in text


def test_opencost_ui_target_is_a_distinct_png(script) -> None:
    """The native OpenCost UI capture lands beside the Grafana PNGs without
    colliding with any dashboard filename."""
    assert script.OPENCOST_UI_FILENAME.endswith(".png")
    assert script.OPENCOST_UI_FILENAME not in {shot.filename for shot in script.SCREENSHOTS}


def test_cost_docs_embed_both_cost_screenshots(script) -> None:
    """COST_MONITORING.md must embed the Grafana cost dashboard and the native
    OpenCost UI screenshots — the docs-integration the images exist for."""
    text = (PROJECT_ROOT / "docs" / "COST_MONITORING.md").read_text()
    assert "images/grafana-cost.png" in text
    assert f"images/{script.OPENCOST_UI_FILENAME}" in text
    assert "scripts/capture_monitoring_screenshots.py" in text


def test_main_captures_opencost_ui_when_url_given(script, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(script, "capture", lambda *a, **k: [IMAGES_DIR / "grafana-cost.png"])

    def _fake_opencost(url: str, output_dir: Path) -> Path:
        calls["url"] = url
        return IMAGES_DIR / script.OPENCOST_UI_FILENAME

    monkeypatch.setattr(script, "capture_opencost_ui", _fake_opencost)
    rc = script.main(["--password", "secret", "--opencost-url", "http://localhost:9091"])
    assert rc == 0
    assert calls["url"] == "http://localhost:9091"


def test_main_skips_opencost_ui_by_default(script, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(script, "capture", lambda *a, **k: [])

    def _boom(url: str, output_dir: Path) -> Path:
        raise AssertionError("opencost capture must not run without --opencost-url")

    monkeypatch.setattr(script, "capture_opencost_ui", _boom)
    assert script.main(["--password", "secret"]) == 0


def test_main_returns_zero_on_success(script, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(script, "capture", lambda *a, **k: [IMAGES_DIR / "grafana-gpu-dcgm.png"])
    rc = script.main(["--password", "secret", "--grafana-url", "http://localhost:3000"])
    assert rc == 0


def test_main_returns_one_on_capture_failure(script, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **k: object) -> list[Path]:
        raise RuntimeError("no browser")

    monkeypatch.setattr(script, "capture", _boom)
    rc = script.main(["--password", "secret"])
    assert rc == 1
