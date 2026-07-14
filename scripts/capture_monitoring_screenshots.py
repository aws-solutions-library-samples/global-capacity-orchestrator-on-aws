"""Capture Grafana dashboard screenshots for the monitoring docs.

Renders each curated GCO dashboard to the repo's ``images/`` directory using a
headless Chromium via Playwright (the same rendering dependency the code-diagram
generator uses). It drives a live Grafana reached through a
``gco monitoring open`` port-forward, so regenerating the doc assets after a
dashboard change is a two-step, on-demand flow:

    # 1. In one shell, port-forward Grafana. The cluster's API endpoint is
    #    private, so tunnel through SSM; ``--via-ssm auto`` provisions a
    #    self-terminating ephemeral bastion and tears it down on exit (or pass
    #    an existing instance with ``--via-ssm <instance-id>``):
    gco monitoring open --region us-east-1 --via-ssm auto

    # 2. In another, capture the dashboards (Chromium fetched once with
    #    ``playwright install chromium``):
    python scripts/capture_monitoring_screenshots.py \
        --username admin --password "$GCO_GRAFANA_ADMIN_PASSWORD"

The set of dashboards captured here is kept in lockstep with the dashboard
ConfigMaps in
``lambda/kubectl-applier-simple/manifests/post-helm-grafana-dashboards.yaml`` by
``tests/test_cluster_observability_screenshots.py`` — add a dashboard there and
the test fails until it is added to ``SCREENSHOTS`` below.
"""

from __future__ import annotations

import argparse
import base64
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMAGES_DIR = PROJECT_ROOT / "images"
DEFAULT_GRAFANA_URL = "http://localhost:3000"

# How long to let a dashboard's panels finish rendering before the screenshot.
# Generous because the SPA has to initialise, fetch the dashboard, and run its
# panel queries — each a round trip that, over an SSM tunnel, carries latency.
_PANEL_RENDER_WAIT_MS = 4000


@dataclass(frozen=True)
class Screenshot:
    """One dashboard capture: its Grafana ``uid`` and the output filename."""

    dashboard_uid: str
    filename: str
    title: str


# One entry per curated dashboard. The ``dashboard_uid`` values must match the
# ``uid`` in each dashboard JSON under post-helm-grafana-dashboards.yaml.
SCREENSHOTS: tuple[Screenshot, ...] = (
    Screenshot("gco-gpu-dcgm", "grafana-gpu-dcgm.png", "GCO GPU (DCGM)"),
    Screenshot("gco-schedulers", "grafana-schedulers.png", "GCO Schedulers & Queues"),
    Screenshot("gco-keda", "grafana-keda.png", "GCO KEDA Autoscaling"),
    Screenshot("gco-services", "grafana-services.png", "GCO Services"),
)


def expected_output_paths(images_dir: Path = IMAGES_DIR) -> list[Path]:
    """Return the image paths this script writes, under ``images_dir``."""
    return [images_dir / shot.filename for shot in SCREENSHOTS]


def capture(grafana_url: str, username: str, password: str, output_dir: Path) -> list[Path]:
    """Authenticate to Grafana and screenshot each dashboard. Returns written paths.

    Authentication sends an HTTP basic-auth ``Authorization`` header on every
    request (Grafana's ``auth.basic`` is enabled by default). The header is set
    proactively rather than via Playwright's ``http_credentials`` because Grafana
    redirects an unauthenticated browser navigation to ``/login`` (a 302) instead
    of issuing a 401 challenge — so ``http_credentials``, which only answers a
    401, would never send the header and the capture would screenshot the login
    page. Setting the header outright mirrors ``curl -u`` and keeps the SPA
    authenticated, so no login form is driven.

    Playwright is imported lazily so this module can be imported (and its
    metadata inspected by tests) without the browser being installed.
    """
    from playwright.sync_api import sync_playwright

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    base = grafana_url.rstrip("/")
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 1600, "height": 900},
                extra_http_headers={"Authorization": f"Basic {token}"},
            )
            page = context.new_page()
            for shot in SCREENSHOTS:
                # ``kiosk`` hides Grafana chrome so the screenshot is just panels.
                # Wait for ``load`` (not ``networkidle``) — a live dashboard's
                # periodic queries can keep the network busy indefinitely — then
                # give the panels a fixed moment to finish rendering.
                page.goto(f"{base}/d/{shot.dashboard_uid}?kiosk", wait_until="load")
                page.wait_for_timeout(_PANEL_RENDER_WAIT_MS)
                out = output_dir / shot.filename
                page.screenshot(path=str(out), full_page=True)
                written.append(out)
        finally:
            browser.close()
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture GCO Grafana dashboard screenshots for the monitoring docs."
    )
    parser.add_argument("--grafana-url", default=DEFAULT_GRAFANA_URL)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--output-dir", type=Path, default=IMAGES_DIR)
    args = parser.parse_args(argv)

    try:
        written = capture(args.grafana_url, args.username, args.password, args.output_dir)
    except Exception as exc:  # noqa: BLE001 — surface any Playwright/login failure
        print(f"screenshot capture failed: {exc}", file=sys.stderr)
        return 1

    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
