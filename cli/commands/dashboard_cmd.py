"""The ``gco dashboard`` commands: a local web dashboard for the fleet.

``start`` runs the dashboard in a container of its own (built from
``Dockerfile.dashboard`` in the checkout) and leaves it running. ``serve``
runs it in this process. Inside the ``gco-dev`` container both serve it in
the foreground, on the port the ``gco`` shell function publishes. Both
print a link carrying a fresh access token. ``open``, ``status``, ``logs``
and ``stop`` find the running dashboard through ``~/.gco/dashboard.json``;
in the dev container, where they cannot reach a dashboard another
container serves, they say where it runs instead.
See docs/DASHBOARD.md.
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

import click

from ..config import GCOConfig
from ..dashboard import container
from ..dashboard.server import (
    DEFAULT_PORT,
    LOOPBACK,
    DashboardUnavailableError,
    healthy,
    new_token,
    require_streamlit,
    serve,
)
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)

#: Inside the dev container: the host port the ``gco`` function published.
HOST_PORT_ENV = "GCO_DASHBOARD_PORT"

#: Where the server listens in the dev container: every interface of the
#: container, whose port the ``gco`` function publishes on the host's loopback only.
_DEV_CONTAINER_ADDRESS = "0.0.0.0"  # nosec B104  # published on the host's 127.0.0.1 only

#: Where a dashboard served in the dev container runs, for the commands that
#: cannot reach it (see :func:`_out_of_reach`).
_IN_ITS_TERMINAL = "The dashboard runs in the terminal it was started from"

_PORT = click.IntRange(1, 65535)


def _port_option(function: Any) -> Any:
    return click.option(
        "--port",
        "-p",
        type=_PORT,
        default=DEFAULT_PORT,
        show_default=True,
        help="Port on this machine's loopback address",
    )(function)


def _token_option(function: Any) -> Any:
    return click.option(
        "--no-token",
        is_flag=True,
        help="Admit any browser session without the link's access token (not recommended)",
    )(function)


def _open_option(function: Any) -> Any:
    return click.option("--no-open", is_flag=True, help="Do not open a browser")(function)


def _project_option(function: Any) -> Any:
    return click.option(
        "--project-dir",
        type=click.Path(exists=True, file_okay=False, path_type=Path),
        default=None,
        help="GCO checkout whose cdk.json names the deployment (default: this directory)",
    )(function)


def _open_when_ready(port: int, url: str) -> None:
    """Open the browser once the server answers (it starts after this thread)."""
    if container.wait_until_healthy(port, timeout=60):
        webbrowser.open(url)


def _serve_here(
    config: Any,
    *,
    port: int,
    host_port: int,
    address: str,
    token: str | None,
    project_dir: Path,
    open_browser: bool,
) -> None:
    """Serve in this process until interrupted, recording where for ``open``/``status``.

    The server listens on ``port``; browsers reach it on ``host_port``, which
    the link and Streamlit's own start-up banner both name (in the dev
    container that is the host port the ``gco`` function publishes). Streamlit
    is checked first, so without it nothing is recorded and no link is printed.
    """
    formatter = get_output_formatter(config)
    try:
        require_streamlit()
    except DashboardUnavailableError as exc:
        formatter.print_error(str(exc))
        sys.exit(1)
    state = container.DashboardState(
        mode="serve",
        port=host_port,
        token=token,
        project_dir=str(project_dir),
        started_at=container.now(),
        # ``stop`` checks this process to tell whether the dashboard still runs.
        # A dev container's PIDs mean nothing on the host or in the other dev
        # containers, so none is kept there.
        pid=None if container.in_dev_container() else os.getpid(),
    )
    container.save_state(state)
    formatter.print_success(f"Dashboard starting at {state.url}")
    formatter.print_info("Press Ctrl-C to stop it.")
    if open_browser:
        threading.Thread(target=_open_when_ready, args=(port, state.url), daemon=True).start()
    try:
        serve(
            port=port,
            address=address,
            token=token,
            project_dir=project_dir,
            browser_port=host_port,
        )
    finally:
        # Only this server's own record: another dashboard may have replaced it.
        if container.load_state() == state:
            container.clear_state()


def _serve_in_dev_container(
    config: Any, *, address: str, token: str | None, project_dir: Path
) -> None:
    """Serve in the foreground in the gco-dev container, on the port the gco function publishes.

    The ``gco`` shell function (``scripts/setup-dev-alias.sh``) publishes the
    container's port 8501 on the host's 127.0.0.1, at ``GCO_DASHBOARD_PORT``
    (default 8501), for ``gco dashboard start`` and ``serve``. So the server
    listens on that container port whatever ``--port`` says, the link and
    Streamlit's banner name the host's port, and no browser is opened (the
    container has none).
    """
    _serve_here(
        config,
        port=DEFAULT_PORT,
        host_port=_published_port(config),
        address=address,
        token=token,
        project_dir=project_dir,
        open_browser=False,
    )


def _published_port(config: Any) -> int:
    """The host port the gco function publishes (``GCO_DASHBOARD_PORT``, default 8501)."""
    raw = os.environ.get(HOST_PORT_ENV) or str(DEFAULT_PORT)
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        get_output_formatter(config).print_error(
            f"{HOST_PORT_ENV}={raw!r} is not a port number (1-65535)."
        )
        sys.exit(2)
    return port


def _refuse_tokenless_beyond_loopback(config: Any, address: str, no_token: bool) -> None:
    """``--no-token`` only where the server listens on loopback alone."""
    if no_token and address not in (LOOPBACK, "localhost", "::1"):
        get_output_formatter(config).print_error(
            "A dashboard listening beyond loopback needs its access token: drop --no-token."
        )
        sys.exit(2)


@click.group()
@pass_config
def dashboard(config: Any) -> None:
    """Run the GCO dashboard, a local web UI for the whole fleet.

    The dashboard maps every Region with its health and load, lists and acts
    on Jobs, the central queue and inference endpoints (with a chat
    playground), compares capacity and prices across Regions, charts costs,
    and reads any API route. It runs on this machine and calls AWS with your
    credentials, exactly as the matching gco commands do.

    \b
    Examples:
        gco dashboard start           # its own container; opens the browser
        gco dashboard serve           # this process (pip install 'gco-cli[dashboard]')
        gco dashboard status
        gco dashboard stop
    """


@dashboard.command("start")
@_port_option
@_token_option
@_open_option
@_project_option
@click.option("--rebuild", is_flag=True, help="Rebuild the image even if its sources are unchanged")
@pass_config
def dashboard_start(
    config: Any,
    port: int,
    no_token: bool,
    no_open: bool,
    project_dir: Path | None,
    rebuild: bool,
) -> None:
    """Start the dashboard in its own container and open it.

    The image is built from Dockerfile.dashboard in this GCO checkout the
    first time, and again only when its sources change. The container
    listens on 127.0.0.1 only, mounts ~/.aws and the checkout read-only, and
    keeps running until 'gco dashboard stop'.

    In the gco-dev container (scripts/setup-dev-alias.sh), the dashboard is
    served in the foreground on the port the gco function publishes
    (GCO_DASHBOARD_PORT, default 8501); press Ctrl-C to stop it. It listens
    on every interface of the dev container there, so it always needs its
    access token.
    """
    formatter = get_output_formatter(config)
    project = (project_dir or Path.cwd()).resolve()
    if container.in_dev_container():
        _refuse_tokenless_beyond_loopback(config, _DEV_CONTAINER_ADDRESS, no_token)
        _serve_in_dev_container(
            config, address=_DEV_CONTAINER_ADDRESS, token=new_token(), project_dir=project
        )
        return
    token = None if no_token else new_token()

    from .._container_runtime import container_runtime_error_message, detect_container_runtime

    runtime = detect_container_runtime()
    if runtime is None:
        formatter.print_error(container_runtime_error_message())
        formatter.print_info("Or run the dashboard in this process: gco dashboard serve")
        sys.exit(1)

    current = container.load_state()
    try:
        status = container.container_status(runtime)
    except container.ContainerError as exc:
        formatter.print_error(str(exc))
        sys.exit(1)
    if status == "running" and current is not None and current.mode == "container":
        formatter.print_info(f"The dashboard is already running: {current.url}")
        if not no_open:
            webbrowser.open(current.url)
        return

    context = container.find_build_context(*container.default_build_contexts())
    if context is None:
        formatter.print_error(
            f"{container.DOCKERFILE} was not found: run this from a GCO checkout, or run "
            "the dashboard in this process with 'gco dashboard serve'."
        )
        sys.exit(1)
    try:
        fingerprint = container.source_fingerprint(context)
        if rebuild or container.image_fingerprint(runtime) != fingerprint:
            formatter.print_info(
                f"Building the dashboard image with {runtime} (the first build takes a few minutes)..."
            )
            container.build_image(runtime, context, fingerprint)
        container.start_container(
            runtime, port=port, token=token, project_dir=project, aws_dir=Path.home() / ".aws"
        )
    except container.ContainerError as exc:
        formatter.print_error(str(exc))
        sys.exit(1)
    if not container.wait_until_healthy(port):
        formatter.print_error(
            "The dashboard container started but does not answer; see 'gco dashboard logs'."
        )
        sys.exit(1)
    state = container.DashboardState(
        mode="container",
        port=port,
        token=token,
        project_dir=str(project),
        started_at=container.now(),
        runtime=runtime,
        container=container.NAME,
        image=container.image_ref(runtime),
    )
    container.save_state(state)
    formatter.print_success(f"Dashboard running at {state.url}")
    formatter.print_info("Stop it with 'gco dashboard stop'.")
    if not no_open:
        webbrowser.open(state.url)


def _default_address() -> str:
    """Loopback, or in the dev container the address ``start`` listens on there."""
    return _DEV_CONTAINER_ADDRESS if container.in_dev_container() else LOOPBACK


@dashboard.command("serve")
@_port_option
@click.option(
    "--address",
    default=_default_address,
    show_default=f"{LOOPBACK}; every interface in the gco-dev container",
    help="Address to listen on; anything but loopback exposes the dashboard to the network",
)
@_token_option
@_open_option
@_project_option
@pass_config
def dashboard_serve(
    config: Any,
    port: int,
    address: str,
    no_token: bool,
    no_open: bool,
    project_dir: Path | None,
) -> None:
    """Run the dashboard in this process until Ctrl-C.

    Needs Streamlit, which the 'dashboard' extra installs
    (pip install 'gco-cli[dashboard]'; the dev image has it).

    In the gco-dev container (scripts/setup-dev-alias.sh), it serves as
    'gco dashboard start' does there: on every interface unless --address
    says otherwise, on the port the gco function publishes on the host's
    127.0.0.1 (GCO_DASHBOARD_PORT, default 8501).
    """
    _refuse_tokenless_beyond_loopback(config, address, no_token)
    token = None if no_token else new_token()
    project = (project_dir or Path.cwd()).resolve()
    if container.in_dev_container():
        _serve_in_dev_container(config, address=address, token=token, project_dir=project)
        return
    _serve_here(
        config,
        port=port,
        host_port=port,
        address=address,
        token=token,
        project_dir=project,
        open_browser=not no_open,
    )


def _runtime_for(state: container.DashboardState | None) -> str | None:
    if state is not None and state.runtime:
        return state.runtime
    from .._container_runtime import detect_container_runtime

    return detect_container_runtime()


def _out_of_reach(state: container.DashboardState) -> bool:
    """Whether ``state`` is a dashboard served in the dev container, out of this command's reach.

    The ``gco`` shell function (``scripts/setup-dev-alias.sh``) runs every
    command in a dev container of its own and publishes the dashboard's port
    for ``gco dashboard start`` and ``serve`` alone. The dashboard those
    commands serve never answers on another container's loopback address
    and stops only with Ctrl-C in its terminal, so a silent health check
    here means "cannot tell", not "not running".
    """
    return state.mode == "serve" and container.in_dev_container()


def _still_serving(state: container.DashboardState) -> bool:
    """Whether the process serving ``state`` (a ``serve`` dashboard on this machine) still runs.

    Its PID tells at once, also while the server is starting and does not
    answer yet. A record without one to check (written in the dev container)
    or a system that cannot check it (Windows) falls back to whether the
    dashboard answers on its port.
    """
    alive = container.process_alive(state.pid)
    return healthy(state.port) if alive is None else alive


@dashboard.command("stop")
@pass_config
def dashboard_stop(config: Any) -> None:
    """Stop the dashboard container."""
    formatter = get_output_formatter(config)
    state = container.load_state()
    if state is not None and state.mode == "serve":
        if _out_of_reach(state):
            formatter.print_info(f"{_IN_ITS_TERMINAL}: press Ctrl-C there to stop it.")
        elif _still_serving(state):
            formatter.print_info(
                "The dashboard runs in a terminal ('gco dashboard serve'); press Ctrl-C there."
            )
        else:
            # The server died without clearing its record: closing its terminal
            # sends SIGHUP, which Streamlit does not handle.
            container.clear_state()
            formatter.print_info("The dashboard is no longer running.")
        return
    runtime = _runtime_for(state)
    try:
        removed = runtime is not None and container.remove_container(runtime)
    except container.ContainerError as exc:
        formatter.print_error(str(exc))
        sys.exit(1)
    container.clear_state()
    if removed:
        formatter.print_success("Dashboard stopped.")
    else:
        formatter.print_info("No dashboard container is running.")


@dashboard.command("status")
@pass_config
def dashboard_status(config: Any) -> None:
    """Show whether the dashboard runs, where, and its link."""
    formatter = get_output_formatter(config)
    state = container.load_state()
    port = state.port if state is not None else DEFAULT_PORT
    answering = healthy(port)
    # Whether a dashboard served from another dev container still runs cannot
    # be told from this one: unknown, not "not running".
    unknown = state is not None and not answering and _out_of_reach(state)
    runtime = _runtime_for(state) if state is None or state.mode == "container" else None
    try:
        status = container.container_status(runtime) if runtime else None
    except container.ContainerError as exc:
        formatter.print_error(str(exc))
        sys.exit(1)
    document = {
        "running": None if unknown else answering,
        "mode": state.mode if state is not None else None,
        "url": state.url if state is not None and (answering or unknown) else None,
        "port": port,
        "container": status,
        "started_at": state.started_at if state is not None else None,
        "project_dir": state.project_dir if state is not None else None,
    }
    if config.output_format != "table":
        formatter.print(document)
        return
    if answering and state is not None:
        formatter.print_success(f"Dashboard running ({state.mode}) at {state.url}")
        formatter.print_info(f"Started {state.started_at} for {state.project_dir}")
    elif answering:
        formatter.print_success(f"A dashboard answers on port {port} (not started by gco).")
    elif unknown and state is not None:
        formatter.print_info(f"{_IN_ITS_TERMINAL}, at {state.url}")
        formatter.print_info(
            "Each gco command runs in a dev container of its own, so this one cannot "
            "check that it still answers."
        )
    elif status:
        formatter.print_warning(f"The dashboard container is {status} and not answering.")
    else:
        formatter.print_info("The dashboard is not running. Start it with 'gco dashboard start'.")


@dashboard.command("logs")
@click.option("--follow", "-f", is_flag=True, help="Keep printing new lines")
@click.option("--tail", "-n", type=click.IntRange(min=0), default=200, show_default=True)
@pass_config
def dashboard_logs(config: Any, follow: bool, tail: int) -> None:
    """Print the dashboard container's logs."""
    formatter = get_output_formatter(config)
    state = container.load_state()
    if state is not None and _out_of_reach(state):
        formatter.print_info(f"{_IN_ITS_TERMINAL}, and prints its log there.")
        return
    runtime = _runtime_for(state)
    try:
        status = container.container_status(runtime) if runtime else None
    except container.ContainerError as exc:
        formatter.print_error(str(exc))
        sys.exit(1)
    if runtime is None or status is None:
        formatter.print_info("No dashboard container exists.")
        return
    sys.exit(container.stream_logs(runtime, follow=follow, tail=tail))


@dashboard.command("open")
@pass_config
def dashboard_open(config: Any) -> None:
    """Open the running dashboard in a browser (its link carries the access token)."""
    formatter = get_output_formatter(config)
    state = container.load_state()
    if state is not None and _out_of_reach(state):
        formatter.print_info(state.url)
        formatter.print_info(f"Open it in a browser on this machine. {_IN_ITS_TERMINAL}.")
        return
    if state is None or not healthy(state.port):
        formatter.print_error("The dashboard is not running. Start it with 'gco dashboard start'.")
        sys.exit(1)
    formatter.print_info(state.url)
    webbrowser.open(state.url)
