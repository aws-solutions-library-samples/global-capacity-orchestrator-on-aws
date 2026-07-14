"""
Tests for the monitoring dashboard screenshot tooling
(scripts/capture_monitoring_screenshots.py + docs/images/monitoring/).

Real Grafana screenshots need a live cluster, so instead of asserting binary
images exist, this pins the invariants that keep the doc assets honest:

- the capture script's target dashboards stay in lockstep with the dashboard
  ConfigMaps actually shipped (add a dashboard and this fails until the script
  is updated),
- every output path is a ``.png`` under ``docs/images/monitoring/``,
- the docs reference the script and the images directory exists with a README.

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
DASHBOARDS_MANIFEST = (
    PROJECT_ROOT
    / "lambda"
    / "kubectl-applier-simple"
    / "manifests"
    / "post-helm-grafana-dashboards.yaml"
)
IMAGES_DIR = PROJECT_ROOT / "docs" / "images" / "monitoring"
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
    for doc in yaml.safe_load_all(DASHBOARDS_MANIFEST.read_text()):
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


def test_images_dir_has_readme() -> None:
    assert (IMAGES_DIR / "README.md").is_file()


def test_docs_reference_the_capture_script() -> None:
    text = MONITORING_DOC.read_text()
    assert "scripts/capture_monitoring_screenshots.py" in text
    assert "images/monitoring" in text


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
