"""Shared plumbing for the in-cluster web UIs GCO reaches through kubectl.

``gco gitops`` (Argo CD) and ``gco crossplane`` (the Crossview dashboard) sit
on the same three steps ``gco monitoring open`` takes for Grafana — reach the
(private by default) EKS API through :func:`cli.cluster_tunnel.open_api_server_tunnel`,
``kubectl port-forward`` a ClusterIP Service to localhost, and point a browser
or a headless Playwright page at it — plus two reads they need around it: a
key out of a Kubernetes Secret (the Argo CD admin password) and the pinned
chart entry from ``lambda/helm-installer/charts.yaml`` (for ``status``).

Everything here is list-form subprocess calls and pure helpers, so the command
modules stay thin and the tests can drive them with fakes. Playwright is
imported lazily so the CLI loads without the rendering extra installed.
"""

from __future__ import annotations

import base64
import json
import re
import socket
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import click
import yaml

#: Viewport for UI captures (matches the monitoring screenshots).
VIEWPORT_WIDTH = 1600
VIEWPORT_HEIGHT = 900

#: How long a capture waits after navigation for single-page apps that poll
#: (network-idle never comes), mirroring the Grafana captures.
RENDER_WAIT_MS = 5000

#: How long a background port-forward may take to accept connections.
PORT_FORWARD_READY_TIMEOUT_SECONDS = 30.0

#: Default self-terminate backstop for an ``--via-ssm auto`` bastion (mirrors
#: ``cli.ephemeral_bastion.DEFAULT_TTL_MINUTES``; kept literal so importing
#: this module stays cheap).
DEFAULT_BASTION_TTL_MINUTES = 120

_INSTALL_HINT = (
    "Playwright is not installed. Install the rendering extra and its browser: "
    "pip install 'gco[diagrams]' && playwright install chromium"
)

_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")

#: charts.yaml as shipped next to this package in a source checkout.
CHARTS_YAML = Path(__file__).resolve().parents[1] / "lambda" / "helm-installer" / "charts.yaml"

#: The kubectl-applier manifest directory in a source checkout.
MANIFESTS_DIR = (
    Path(__file__).resolve().parents[1] / "lambda" / "kubectl-applier-simple" / "manifests"
)


def load_cdk_context(cdk_json_path: Path | None = None) -> dict[str, Any]:
    """The ``context`` object of cdk.json (the working directory's by default).

    Raises ``RuntimeError`` when cdk.json is missing or is not JSON; a
    document without a ``context`` object reads as ``{}``.
    """
    path = cdk_json_path or Path.cwd() / "cdk.json"
    if not path.exists():
        raise RuntimeError(f"cdk.json not found at {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"cdk.json at {path} is not valid JSON: {exc}") from exc
    context = document.get("context") if isinstance(document, dict) else None
    return context if isinstance(context, dict) else {}


def regional_regions(context: Mapping[str, Any]) -> list[str]:
    """``deployment_regions.regional`` from a cdk.json context (``[]`` when absent)."""
    regions = context.get("deployment_regions")
    regional = regions.get("regional") if isinstance(regions, Mapping) else None
    return [str(region) for region in regional] if isinstance(regional, list) else []


def chart_entry(name: str, charts_yaml: Path | None = None) -> dict[str, Any] | None:
    """The pinned ``charts.yaml`` entry (chart, version, namespace), or ``None``.

    ``None`` when the file is not there (an installed CLI without the source
    tree) or has no such chart; status commands then just omit the pin.
    """
    path = charts_yaml or CHARTS_YAML
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError, yaml.YAMLError:
        return None
    charts = document.get("charts") if isinstance(document, dict) else None
    entry = charts.get(name) if isinstance(charts, dict) else None
    if not isinstance(entry, dict):
        return None
    return {
        "name": name,
        "chart": str(entry.get("chart") or name),
        "version": str(entry.get("version") or ""),
        "namespace": str(entry.get("namespace") or ""),
        "repo_url": str(entry.get("repo_url") or ""),
    }


def manifest_documents(filename: str, manifests_dir: Path | None = None) -> list[dict[str, Any]]:
    """Parse one shipped kubectl-applier manifest (``[]`` when it is not there).

    ``{{TOKEN}}`` placeholders are left in place; callers read only fields
    that never carry one (kinds, names, pinned package references).
    """
    path = (manifests_dir or MANIFESTS_DIR) / filename
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    # Structural tokens are bare (not quoted) and are not YAML on their own.
    text = re.sub(r"(?<![\"'])\{\{[A-Z0-9_]+\}\}", "null", text)
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


def kubectl_server_flags(server: str | None, tls_server_name: str | None) -> list[str]:
    """``--server`` / ``--tls-server-name`` for a tunnelled API endpoint (or nothing)."""
    flags: list[str] = []
    if server is not None:
        if not server.startswith("https://"):
            raise ValueError(f"Invalid --server {server!r}: must start with https://")
        flags += ["--server", server]
    if tls_server_name is not None:
        if not re.fullmatch(r"[a-zA-Z0-9.\-]{1,255}", tls_server_name):
            raise ValueError(f"Invalid --tls-server-name {tls_server_name!r}")
        flags += ["--tls-server-name", tls_server_name]
    return flags


def read_secret_key(
    namespace: str,
    name: str,
    key: str,
    *,
    server: str | None = None,
    tls_server_name: str | None = None,
) -> str:
    """Decode one key of a Kubernetes Secret through kubectl.

    Raises ``RuntimeError`` naming the Secret when kubectl is missing, the
    read fails, or the key is absent (the message never carries the value).
    """
    for label, value in (("namespace", namespace), ("Secret name", name)):
        if not _NAME_RE.match(value):
            raise ValueError(f"Invalid {label} {value!r}")
    cmd = [
        "kubectl",
        "get",
        "secret",
        name,
        "-n",
        namespace,
        "-o",
        "json",
        *kubectl_server_flags(server, tls_server_name),
    ]
    try:
        result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - validated argv, list form, no shell=True
            cmd, capture_output=True, text=True, check=False, timeout=60
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "kubectl not found. Install kubectl and ensure it's on your PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Timed out reading Secret {namespace}/{name}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise RuntimeError(f"Failed to read Secret {namespace}/{name}: {detail}")
    data = (json.loads(result.stdout or "{}").get("data")) or {}
    if key not in data:
        raise RuntimeError(f"Secret {namespace}/{name} has no {key!r} key")
    return base64.b64decode(data[key]).decode("utf-8")


def wait_for_local_port(
    port: int,
    process: subprocess.Popen[bytes] | None = None,
    *,
    timeout: float = PORT_FORWARD_READY_TIMEOUT_SECONDS,
) -> None:
    """Block until 127.0.0.1:``port`` accepts a TCP connection.

    Raises ``RuntimeError`` when ``process`` (the port-forward) exits first or
    the deadline passes.
    """
    deadline = time.monotonic() + timeout
    while True:
        if process is not None and process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
            raise RuntimeError(
                f"kubectl port-forward exited before localhost:{port} was ready: "
                f"{stderr.strip() or f'exit code {process.returncode}'}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"localhost:{port} did not accept connections within {int(timeout)}s"
                ) from None
            time.sleep(0.5)


@contextmanager
def background_port_forward(
    cmd: Sequence[str],
    local_port: int,
    *,
    timeout: float = PORT_FORWARD_READY_TIMEOUT_SECONDS,
) -> Iterator[subprocess.Popen[bytes]]:
    """Run a validated ``kubectl port-forward`` argv in the background until exit.

    Yields once the local port accepts connections; always terminates the
    process (kill after a short grace period) on the way out.
    """
    process = subprocess.Popen(  # nosemgrep: dangerous-subprocess-use-audit - argv built by build_port_forward_command; list form, no shell=True
        list(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        wait_for_local_port(local_port, process, timeout=timeout)
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def capture_page(
    url: str,
    output: Path,
    *,
    cookies: Sequence[Mapping[str, Any]] = (),
    headless: bool = True,
    render_wait_ms: int = RENDER_WAIT_MS,
) -> Path:
    """Screenshot ``url`` into ``output`` (full page) with Playwright's Chromium.

    ``cookies`` are added to the browser context before navigation (the Argo
    CD session token rides this way). Parent directories are created.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(_INSTALL_HINT) from exc

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            context = browser.new_context(
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}
            )
            if cookies:
                # Playwright types the list as its SetCookieParam TypedDict; the
                # callers pass plain name/value/url mappings of that shape.
                context.add_cookies(cast(Any, [dict(cookie) for cookie in cookies]))
            page = context.new_page()
            page.goto(url, wait_until="load")
            page.wait_for_timeout(render_wait_ms)
            page.screenshot(path=str(output), full_page=True)
        finally:
            browser.close()
    return output


def tunnel_options(func: Any) -> Any:
    """The --region / --via-ssm / --bastion-ttl-minutes / --yes options UI commands share."""
    func = click.option(
        "--yes",
        "-y",
        "assume_yes",
        is_flag=True,
        help="Skip the confirmation prompt when provisioning an `--via-ssm auto` bastion.",
    )(func)
    func = click.option(
        "--bastion-ttl-minutes",
        type=int,
        default=DEFAULT_BASTION_TTL_MINUTES,
        show_default=True,
        help="Self-terminate backstop (minutes) for an `--via-ssm auto` bastion.",
    )(func)
    func = click.option(
        "--via-ssm",
        "via_ssm",
        metavar="INSTANCE_ID|auto",
        help=(
            "Tunnel to the private API endpoint through an SSM-managed instance. "
            "Pass an instance id to use an existing one, or 'auto' to provision a "
            "self-terminating ephemeral bastion and tear it down afterwards."
        ),
    )(func)
    func = click.option(
        "--region", "-r", help="Cluster region (defaults to the first cdk.json regional entry)."
    )(func)
    return func


def exec_port_forward(cmd: Sequence[str]) -> None:
    """Run the (validated) kubectl port-forward argv in the foreground."""
    subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - argv built by build_port_forward_command; list form, no shell=True
        list(cmd), check=False
    )
