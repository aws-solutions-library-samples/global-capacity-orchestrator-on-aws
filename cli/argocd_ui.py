"""Open and screenshot the hosted Argo CD UI of the Argo CD EKS Capability.

Backs ``gco stacks capabilities argocd open`` / ``screenshot`` and the
``argocd_ui_url`` MCP tool. The AWS-managed Argo CD runs outside the cluster
and serves its UI at a server URL EKS reports on the capability
(``DescribeCapability`` -> ``configuration.argoCd.serverUrl``); sign-in is IAM
Identity Center, so there is nothing to port-forward and no credential GCO
holds — the browser carries the session.

Two operations:

* ``resolve_argocd_server_url`` — the URL, from the EKS API. A capability that
  is not attached or not yet ``ACTIVE`` is a ``RuntimeError`` naming the
  remedy (``gco stacks capabilities status``).
* ``capture_argocd_screenshot`` — a full-page PNG of the Applications view via
  Playwright's Chromium, the same rendering dependency
  ``scripts/capture_monitoring_screenshots.py`` and the code-diagram generator
  use (``pip install 'gco[diagrams]'`` then ``playwright install chromium``).
  The browser profile persists under ``~/.gco/argocd-browser/<region>`` so the
  Identity Center session survives between runs: the first capture is headed
  and waits for the operator to sign in; later captures can be ``--headless``.
  Playwright is imported lazily so the module (and the CLI) load without it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .eks_capabilities import ACTIVE_STATUS, capability_name, cluster_name_for

#: Default screenshot filename; ``docs/EKS_CAPABILITIES.md`` embeds
#: ``images/argocd-ui.png`` captured with ``--output images/argocd-ui.png``.
DEFAULT_SCREENSHOT_FILENAME = "argocd-ui.png"

#: How long a headed capture waits for the operator's Identity Center sign-in
#: to land back on the Applications view.
DEFAULT_LOGIN_TIMEOUT_SECONDS = 300

#: The Argo CD UI route the capture waits for and screenshots.
APPLICATIONS_PATH = "/applications"

#: Viewport for the capture (matches the monitoring screenshots). Passed to
#: Playwright as its ``ViewportSize`` TypedDict at call time.
VIEWPORT_WIDTH = 1600
VIEWPORT_HEIGHT = 900

#: Time for the SPA to fetch and render the application tiles after the route
#: settles. Argo CD's list view polls, so "network idle" never comes; a fixed
#: wait after ``load`` is the same approach the Grafana captures use.
_RENDER_WAIT_MS = 5000

#: Argo CD's login page labels its SSO entry "LOG IN VIA <provider>". Clicking
#: it for the operator turns the headed flow into "just sign in".
_SSO_BUTTON_PATTERN = re.compile(r"log\s*in\s*via", re.IGNORECASE)

_INSTALL_HINT = (
    "Playwright is not installed. Install the rendering extra and its browser: "
    "pip install 'gco[diagrams]' && playwright install chromium"
)


def default_profile_dir(region: str) -> Path:
    """Per-region persistent browser profile holding the Identity Center session."""
    base = os.environ.get("GCO_ARGOCD_BROWSER_PROFILE_DIR")
    root = Path(base).expanduser() if base else Path.home() / ".gco" / "argocd-browser"
    return root / region


def applications_url(server_url: str) -> str:
    """The Applications view under the hosted server URL."""
    return server_url.rstrip("/") + APPLICATIONS_PATH


def is_applications_url(url: str, server_url: str) -> bool:
    """True when ``url`` is the Applications view of ``server_url`` (post sign-in)."""
    target = urlsplit(server_url)
    current = urlsplit(url)
    if current.netloc.lower() != target.netloc.lower():
        return False
    path = current.path.rstrip("/") or "/"
    return path == APPLICATIONS_PATH or path.startswith(APPLICATIONS_PATH + "/")


def resolve_argocd_server_url(
    region: str,
    project_name: str,
    *,
    eks_client: Any | None = None,
) -> str:
    """The hosted Argo CD server URL for this region's cluster.

    Reads ``DescribeCapability`` for ``<project>-argocd`` on ``<project>-<region>``.
    Raises ``RuntimeError`` (with the remedy) when the cluster or capability is
    absent, the capability is not ``ACTIVE`` yet, or EKS has not published a
    server URL.
    """
    from botocore.exceptions import ClientError

    cluster = cluster_name_for(project_name, region)
    name = capability_name(project_name, "argocd")
    if eks_client is None:
        import boto3

        eks_client = boto3.client("eks", region_name=region)
    try:
        response = eks_client.describe_capability(clusterName=cluster, capabilityName=name)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ResourceNotFoundException":
            raise RuntimeError(
                f"No Argo CD capability {name!r} is attached to cluster {cluster!r} in "
                f"{region}. Enable eks_capabilities.argocd in cdk.json and deploy "
                f"'gco stacks deploy {cluster} -y'; check with 'gco stacks capabilities status'."
            ) from exc
        raise
    detail = response.get("capability") if isinstance(response, dict) else None
    if not isinstance(detail, dict):
        raise RuntimeError(f"EKS returned no detail for capability {name!r} on {cluster!r}")
    status = str(detail.get("status") or "UNKNOWN")
    if status != ACTIVE_STATUS:
        raise RuntimeError(
            f"Argo CD capability {name!r} on {cluster!r} is {status}, not {ACTIVE_STATUS}; "
            "wait for it to finish, or inspect 'gco stacks capabilities status'."
        )
    configuration = detail.get("configuration")
    argo = configuration.get("argoCd") if isinstance(configuration, dict) else None
    url = argo.get("serverUrl") if isinstance(argo, dict) else None
    if not isinstance(url, str) or not url.strip():
        raise RuntimeError(
            f"Argo CD capability {name!r} on {cluster!r} is {ACTIVE_STATUS} but EKS reports "
            "no server URL yet; retry shortly."
        )
    return url.strip()


def _click_sso_button_if_present(page: Any) -> bool:
    """Press Argo CD's "LOG IN VIA ..." button when the login page is showing."""
    try:
        button = page.get_by_role("button", name=_SSO_BUTTON_PATTERN)
        if button.count() == 0:
            return False
        button.first.click(timeout=5000)
        return True
    except Exception:  # login page absent (already signed in) or a different layout
        return False


def capture_argocd_screenshot(
    server_url: str,
    output: Path,
    *,
    profile_dir: Path,
    headless: bool = False,
    login_timeout_seconds: int = DEFAULT_LOGIN_TIMEOUT_SECONDS,
) -> Path:
    """Screenshot the Applications view of the hosted Argo CD UI. Returns the path.

    Launches Chromium on the persistent ``profile_dir`` (created on demand),
    navigates to ``<server_url>/applications``, presses the SSO button if the
    login page appears, and waits up to ``login_timeout_seconds`` for the
    browser to land back on the Applications route — instant when the profile
    already holds an Identity Center session, otherwise the operator signs in
    in the headed window. A headless run without a session times out with a
    message saying so. The page then gets a fixed render wait and a full-page
    PNG is written to ``output`` (parent directories created).
    """
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(_INSTALL_HINT) from exc

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    target = applications_url(server_url)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(target, wait_until="load")
            _click_sso_button_if_present(page)
            try:
                page.wait_for_url(
                    lambda url: is_applications_url(url, server_url),
                    timeout=login_timeout_seconds * 1000,
                )
            except PlaywrightTimeout as exc:
                how = (
                    "no saved Identity Center session in the browser profile; rerun without "
                    "--headless and sign in once"
                    if headless
                    else "sign-in did not complete in time"
                )
                raise RuntimeError(
                    f"Timed out after {login_timeout_seconds}s waiting for the Argo CD "
                    f"Applications view at {target}: {how} (profile: {profile_dir})."
                ) from exc
            page.wait_for_timeout(_RENDER_WAIT_MS)
            page.screenshot(path=str(output), full_page=True)
        finally:
            context.close()
    return output
