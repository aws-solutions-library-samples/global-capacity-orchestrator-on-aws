"""Start the GCO dashboard's Streamlit server with locked-down settings.

``gco dashboard serve`` and the dashboard image's ``CMD`` both end up in
:func:`serve`, so the app runs the same way from a checkout, in the dev
container and in its own image. Streamlit runs in this process through its
own command line (``streamlit run``). The security-relevant settings are
passed as flags, which outrank any ``.streamlit/config.toml`` in the
directory the dashboard is started from, so a config file in that project
cannot loosen them:

* the server listens on the loopback address unless told otherwise (the
  image listens on every interface inside the container and its port is
  published on the host's loopback only);
* WebSocket connections are accepted only for the ``localhost`` and
  ``127.0.0.1`` host names, which defeats DNS rebinding, and cross-origin
  WebSockets are refused (CORS and XSRF protection stay on);
* Streamlit's agent API, static file serving, file watcher and usage
  statistics are off;
* with a token, a browser session is admitted only with ``?token=`` matching
  it (see :func:`cli.dashboard.ui.require_access`).

This module imports nothing from Streamlit at import time: the ``gco`` CLI
loads it to start the server, and Streamlit is an optional extra.
"""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import os
import secrets
import urllib.request
from collections.abc import Sequence
from pathlib import Path

#: The Streamlit entrypoint, next to this module.
APP = Path(__file__).with_name("app.py")

#: Streamlit's own default port.
DEFAULT_PORT = 8501

#: Where the server listens unless told otherwise.
LOOPBACK = "127.0.0.1"

#: Host names a browser may use to reach the dashboard (DNS-rebinding defence).
ALLOWED_HOSTS = ("localhost", "127.0.0.1")

#: Environment variable carrying the access token into the Streamlit script.
TOKEN_ENV = "GCO_DASHBOARD_TOKEN"  # nosec B105  # an environment variable name, not a secret

#: Environment variable naming the port browsers use when the server's port is
#: published on another (``gco dashboard start`` sets it for the container).
BROWSER_PORT_ENV = "GCO_DASHBOARD_BROWSER_PORT"

#: Largest manifest the submit dialog accepts, in MB.
MAX_UPLOAD_MB = 5

#: Streamlit settings every start passes as flags, in ``streamlit run`` form.
SETTINGS: tuple[tuple[str, str], ...] = (
    ("server.headless", "true"),
    ("server.runOnSave", "false"),
    ("server.fileWatcherType", "none"),
    ("server.enableCORS", "true"),
    ("server.enableXsrfProtection", "true"),
    ("server.enableStaticServing", "false"),
    ("server.enableAgentApi", "false"),
    ("server.showEmailPrompt", "false"),
    ("server.maxUploadSize", str(MAX_UPLOAD_MB)),
    ("browser.gatherUsageStats", "false"),
    # gco prints the dashboard's link with its access token; Streamlit's own
    # banner names the same address without it, which only reaches the gate.
    ("logger.hideWelcomeMessage", "true"),
    ("browser.serverAddress", "localhost"),
    ("global.showWarningOnDirectExecution", "false"),
)


class DashboardUnavailableError(RuntimeError):
    """Streamlit is not installed, so the dashboard cannot start here."""


def streamlit_installed() -> bool:
    """Whether the optional ``dashboard`` extra (Streamlit) is importable."""
    return importlib.util.find_spec("streamlit") is not None


def require_streamlit() -> None:
    """Raise :class:`DashboardUnavailableError`, naming the extra, unless Streamlit is installed."""
    if not streamlit_installed():
        raise DashboardUnavailableError(
            "The dashboard needs Streamlit, which ships in the 'dashboard' extra: "
            "pip install 'gco-cli[dashboard]' (the dev image already has it), or run "
            "it in its own container with 'gco dashboard start'."
        )


def new_token() -> str:
    """A fresh access token for one dashboard start."""
    return secrets.token_urlsafe(24)


def dashboard_url(port: int, token: str | None = None) -> str:
    """The address a browser on this machine opens, with the token if there is one."""
    base = f"http://localhost:{port}/"
    return f"{base}?token={token}" if token else base


def streamlit_args(*, port: int, address: str, browser_port: int | None = None) -> list[str]:
    """The ``streamlit run`` arguments for the dashboard.

    ``browser_port`` is the port browsers use when it is not ``port``: a
    container's port published on another host port. Streamlit puts it in the
    address it prints at start-up and in the origin its upload routes allow;
    without it, both name ``port``.
    """
    args = ["run", str(APP), "--server.port", str(port), "--server.address", address]
    if browser_port is not None:
        args += ["--browser.serverPort", str(browser_port)]
    for key, value in SETTINGS:
        args += [f"--{key}", value]
    for host in ALLOWED_HOSTS:
        args += ["--server.allowedHosts", host]
    return args


def serve(
    *,
    port: int = DEFAULT_PORT,
    address: str = LOOPBACK,
    token: str | None = None,
    project_dir: Path | None = None,
    browser_port: int | None = None,
) -> None:
    """Run the dashboard in this process until it is interrupted.

    ``project_dir`` is the GCO checkout whose ``cdk.json`` names the
    deployment (the working directory by default, as for every ``gco``
    command). ``browser_port`` is the port browsers use when ``port`` is
    published on another (see :func:`streamlit_args`). Raises
    :class:`DashboardUnavailableError` when Streamlit is not installed.
    """
    require_streamlit()
    from streamlit.web import cli as streamlit_cli

    from cli.config import set_project_root

    if project_dir is not None:
        set_project_root(project_dir.resolve())
    if token:
        os.environ[TOKEN_ENV] = token
    else:
        os.environ.pop(TOKEN_ENV, None)
    streamlit_cli.main(
        args=streamlit_args(port=port, address=address, browser_port=browser_port),
        prog_name="streamlit",
        standalone_mode=True,
    )


def healthy(port: int = DEFAULT_PORT, *, host: str = LOOPBACK, timeout: float = 3.0) -> bool:
    """Whether a dashboard answers Streamlit's health check on ``host:port``."""
    url = f"http://{host}:{port}/_stcore/health"
    try:
        # The URL is built here from a fixed scheme, a loopback host and an
        # integer port; nothing in it comes from a request.
        with urllib.request.urlopen(url, timeout=timeout) as response:  # nosec B310  # nosemgrep: dynamic-urllib-use-detected - a fixed http:// URL on a loopback host and an integer port
            return bool(response.status == 200 and response.read(16).strip() == b"ok")
    except OSError, http.client.HTTPException:
        return False


def _port_number(text: str | None) -> int | None:
    """``text`` as a port number (1-65535), or None when it is unset, empty or not one."""
    try:
        port = int(text or "")
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m cli.dashboard.server``: the dashboard image's entrypoint."""
    parser = argparse.ArgumentParser(
        prog="python -m cli.dashboard.server", description="Run the GCO dashboard."
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--address", default=LOOPBACK)
    parser.add_argument(
        "--browser-port",
        type=int,
        # A value that is not a port number is ignored: Streamlit then names --port.
        default=_port_number(os.environ.get(BROWSER_PORT_ENV)),
        help=(
            f"Port browsers use when --port is published on another (default: ${BROWSER_PORT_ENV})"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 0 when a dashboard answers on --port, 1 otherwise (the image's HEALTHCHECK)",
    )
    options = parser.parse_args(argv)
    if options.check:
        return 0 if healthy(options.port) else 1
    serve(
        port=options.port,
        address=options.address,
        token=os.environ.get(TOKEN_ENV) or None,
        browser_port=options.browser_port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
