"""Unit tests for ``cli/commands/dashboard_cmd.py``, the ``gco dashboard`` command group.

Every subcommand runs through the root ``gco`` group with ``CliRunner``. The
runtime-facing half of ``cli.dashboard.container`` (container status, build,
start, removal, logs and the health wait) is replaced by an in-memory fake,
and so are the in-process server, the health probe, runtime detection, the
browser and the browser-opener thread: nothing starts a container, a server
or a browser. The state file stays real, under ``tmp_path``, so the
commands' bookkeeping runs as written, and so does the check of the PID it
records (this test process, or one that has exited). A few tests drive the commands
through the real container module against a fake runtime at its
``subprocess.run`` boundary (one that keeps images and containers, and one
that hangs), and the last group checks that ``gco`` loads and renders its
help without Streamlit, which is an optional extra. ``GCO_DEV_CONTAINER=1``
stands for the ``gco-dev`` image, where each ``gco`` command runs in a
container of its own.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from click.testing import CliRunner, Result

from cli import _container_runtime
from cli import config as cli_config
from cli.commands import dashboard_cmd
from cli.dashboard import container, server
from cli.dashboard.container import ContainerError, DashboardState
from cli.dashboard.server import TOKEN_ENV
from cli.main import cli

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "fresh-test-token"
STARTED = "2026-11-25T18:00:00+00:00"
NO_RUNTIME = "No container runtime found. Install Docker, Finch, or Podman."
#: What a runtime stopped at its timeout raises (``cli.dashboard.container._run``).
HUNG = (
    "'docker inspect --format' did not finish within 120s and was stopped; "
    "check that docker is responding"
)
SUBCOMMANDS = ("logs", "open", "serve", "start", "status", "stop")


def gco(*args: str) -> Result:
    """Run ``gco <args>`` through the root group, as a user would."""
    return CliRunner().invoke(cli, list(args))


def _state(**overrides: Any) -> DashboardState:
    """A dashboard started earlier, in its container unless overridden."""
    values: dict[str, Any] = {
        "mode": "container",
        "port": 9100,
        "token": "earlier-token",
        "project_dir": "/home/tester/gco",
        "started_at": "2026-11-24T09:30:00+00:00",
        "runtime": "docker",
        "container": "gco-dashboard",
        "image": "gco-dashboard:local",
    }
    values.update(overrides)
    return DashboardState(**values)


def _served_state(**overrides: Any) -> DashboardState:
    """A dashboard ``gco dashboard serve`` started earlier, with no PID to check unless given."""
    return _state(mode="serve", runtime=None, container=None, image=None, **overrides)


@pytest.fixture(scope="module")
def exited_pid() -> int:
    """The PID of a process that has exited and been reaped."""
    child = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return int(child.stdout)


def _record_saves(monkeypatch: pytest.MonkeyPatch) -> list[DashboardState]:
    """Every state the commands save, in order (each is still written)."""
    saved: list[DashboardState] = []
    save = container.save_state

    def recording(state: DashboardState) -> None:
        saved.append(state)
        save(state)

    monkeypatch.setattr(container, "save_state", recording)
    return saved


EARLIER_URL = "http://localhost:9100/?token=earlier-token"
#: What ``stop``, ``status``, ``logs`` and ``open`` say in the dev container about a
#: dashboard another dev container serves.
IN_ITS_TERMINAL = "The dashboard runs in the terminal it was started from"


# ---------------------------------------------------------------------------
# Fakes. All but ``fake`` are autouse: no test can reach a runtime, a server,
# the network or a browser by forgetting to ask for one.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A clean workstation under ``tmp_path``: HOME, the state file and the working directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(container.STATE_ENV, str(tmp_path / "state" / "dashboard.json"))
    for name in ("GCO_DEV_CONTAINER", dashboard_cmd.HOST_PORT_ENV, "GCO_OUTPUT_FORMAT", TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dashboard_cmd, "new_token", lambda: TOKEN)
    monkeypatch.setattr(container, "now", lambda: STARTED)
    return home


@pytest.fixture(autouse=True)
def browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The URLs the commands open in a browser."""
    opened: list[str] = []
    monkeypatch.setattr(dashboard_cmd, "webbrowser", SimpleNamespace(open=opened.append))
    return opened


@pytest.fixture(autouse=True)
def threads(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Threads the commands start (the browser opener), recorded instead of run."""
    started: list[dict[str, Any]] = []

    class _Thread:
        def __init__(
            self, *, target: Callable[..., None], args: tuple[Any, ...], daemon: bool
        ) -> None:
            self._spec = {"target": target, "args": args, "daemon": daemon}

        def start(self) -> None:
            started.append(self._spec)

    monkeypatch.setattr(dashboard_cmd, "threading", SimpleNamespace(Thread=_Thread))
    return started


class FakeServer:
    """The in-process Streamlit server: records each call with the state file as it was then."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.during: Callable[[], None] = lambda: None

    def serve(self, **kwargs: Any) -> None:
        self.calls.append({**kwargs, "state": container.load_state()})
        self.during()


@pytest.fixture(autouse=True)
def served(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    fake = FakeServer()
    monkeypatch.setattr(dashboard_cmd, "serve", fake.serve)
    return fake


class Probe:
    """Streamlit's health check, for the commands and for the container module's wait."""

    def __init__(self) -> None:
        self.answering = False
        self.ports: list[int] = []

    def healthy(self, port: int) -> bool:
        self.ports.append(port)
        return self.answering


@pytest.fixture(autouse=True)
def probe(monkeypatch: pytest.MonkeyPatch) -> Probe:
    fake = Probe()
    monkeypatch.setattr(dashboard_cmd, "healthy", fake.healthy)
    monkeypatch.setattr(container, "healthy", fake.healthy)
    return fake


class Detection:
    """Container-runtime detection (the real one runs ``<runtime> info``)."""

    def __init__(self) -> None:
        self.runtime: str | None = "docker"
        self.calls = 0

    def detect(self) -> str | None:
        self.calls += 1
        return self.runtime


@pytest.fixture(autouse=True)
def detection(monkeypatch: pytest.MonkeyPatch) -> Detection:
    fake = Detection()
    monkeypatch.setattr(_container_runtime, "detect_container_runtime", fake.detect)
    monkeypatch.setattr(
        _container_runtime, "container_runtime_error_message", lambda **kwargs: NO_RUNTIME
    )
    return fake


def _fake_subprocess(monkeypatch: pytest.MonkeyPatch, run: Callable[..., Any]) -> None:
    """Replace the container module's ``subprocess`` (it catches the real ``TimeoutExpired``)."""
    monkeypatch.setattr(
        container,
        "subprocess",
        SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired),
    )


@pytest.fixture(autouse=True)
def _no_runtime_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """The container module's subprocess refuses every call unless a test fakes a runtime."""

    def refuse(argv: list[str], **kwargs: Any) -> None:
        raise AssertionError(f"unexpected runtime command: {argv}")

    _fake_subprocess(monkeypatch, refuse)


class FakeContainers:
    """The runtime-facing half of ``cli.dashboard.container``, answered in memory.

    Each attribute is what the matching function answers, ``errors`` makes a
    function raise instead, and every call lands in ``calls``.
    """

    NAMES = (
        "container_status",
        "find_build_context",
        "source_fingerprint",
        "image_fingerprint",
        "build_image",
        "start_container",
        "wait_until_healthy",
        "remove_container",
        "stream_logs",
    )

    def __init__(self, checkout: Path) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.errors: dict[str, BaseException] = {}
        self.status: str | None = None
        self.context: Path | None = checkout
        self.source = "fingerprint-of-the-checkout"
        self.label: str | None = self.source
        self.ready = True
        self.removed = True
        self.logs_exit = 0

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if name in self.errors:
            raise self.errors[name]

    def called(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(args, kwargs) for call, args, kwargs in self.calls if call == name]

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def container_status(self, runtime: str) -> str | None:
        self._record("container_status", runtime)
        return self.status

    def find_build_context(self, *candidates: Path) -> Path | None:
        self._record("find_build_context", *candidates)
        return self.context

    def source_fingerprint(self, root: Path) -> str:
        self._record("source_fingerprint", root)
        return self.source

    def image_fingerprint(self, runtime: str) -> str | None:
        self._record("image_fingerprint", runtime)
        return self.label

    def build_image(self, runtime: str, context: Path, fingerprint: str) -> None:
        self._record("build_image", runtime, context, fingerprint)

    def start_container(self, runtime: str, **kwargs: Any) -> None:
        self._record("start_container", runtime, **kwargs)

    def wait_until_healthy(self, port: int, **kwargs: Any) -> bool:
        self._record("wait_until_healthy", port, **kwargs)
        return self.ready

    def remove_container(self, runtime: str) -> bool:
        self._record("remove_container", runtime)
        return self.removed

    def stream_logs(self, runtime: str, **kwargs: Any) -> int:
        self._record("stream_logs", runtime, **kwargs)
        return self.logs_exit


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeContainers:
    fake = FakeContainers(tmp_path / "checkout")
    for name in FakeContainers.NAMES:
        monkeypatch.setattr(container, name, getattr(fake, name))
    return fake


# ---------------------------------------------------------------------------
# start: its own container
# ---------------------------------------------------------------------------


class TestStart:
    def test_builds_a_stale_image_runs_it_and_opens_the_link(
        self, fake: FakeContainers, browser: list[str], home: Path, tmp_path: Path
    ) -> None:
        fake.label = "fingerprint-of-an-older-checkout"

        result = gco("dashboard", "start")

        url = f"http://localhost:8501/?token={TOKEN}"
        assert result.exit_code == 0, result.output
        assert fake.names() == [
            "container_status",
            "find_build_context",
            "source_fingerprint",
            "image_fingerprint",
            "build_image",
            "start_container",
            "wait_until_healthy",
        ]
        assert fake.called("build_image") == [(("docker", fake.context, fake.source), {})]
        assert fake.called("start_container") == [
            (
                ("docker",),
                {
                    "port": 8501,
                    "token": TOKEN,
                    "project_dir": tmp_path.resolve(),
                    "aws_dir": home / ".aws",
                },
            )
        ]
        assert fake.called("wait_until_healthy") == [((8501,), {})]
        assert container.load_state() == DashboardState(
            mode="container",
            port=8501,
            token=TOKEN,
            project_dir=str(tmp_path.resolve()),
            started_at=STARTED,
            runtime="docker",
            container="gco-dashboard",
            image="gco-dashboard:local",
        )
        assert "Building the dashboard image with docker" in result.stdout
        assert f"Dashboard running at {url}" in result.stdout
        assert "Stop it with 'gco dashboard stop'." in result.stdout
        assert browser == [url]

    def test_an_image_built_from_the_same_sources_is_reused(self, fake: FakeContainers) -> None:
        result = gco("dashboard", "start", "--no-open")

        assert result.exit_code == 0, result.output
        assert fake.called("image_fingerprint") == [(("docker",), {})]
        assert fake.called("build_image") == []
        assert "Building" not in result.stdout
        assert len(fake.called("start_container")) == 1

    def test_rebuild_builds_without_reading_the_image_label(self, fake: FakeContainers) -> None:
        result = gco("dashboard", "start", "--rebuild", "--no-open")

        assert result.exit_code == 0, result.output
        assert fake.called("image_fingerprint") == []
        assert fake.called("build_image") == [(("docker", fake.context, fake.source), {})]

    def test_port_token_browser_and_project_options(
        self, fake: FakeContainers, browser: list[str], home: Path, tmp_path: Path
    ) -> None:
        project = tmp_path / "elsewhere"
        project.mkdir()

        result = gco(
            "dashboard",
            "start",
            "--port",
            "9000",
            "--no-token",
            "--no-open",
            "--project-dir",
            str(project),
        )

        assert result.exit_code == 0, result.output
        assert fake.called("start_container") == [
            (
                ("docker",),
                {
                    "port": 9000,
                    "token": None,
                    "project_dir": project.resolve(),
                    "aws_dir": home / ".aws",
                },
            )
        ]
        assert fake.called("wait_until_healthy") == [((9000,), {})]
        state = container.load_state()
        assert state is not None
        assert (state.port, state.token, state.project_dir) == (9000, None, str(project.resolve()))
        assert "Dashboard running at http://localhost:9000/\n" in result.stdout
        assert browser == []

    @pytest.mark.parametrize("runtime", ["finch", "podman"])
    def test_the_detected_runtime_runs_it_and_is_recorded(
        self, fake: FakeContainers, detection: Detection, runtime: str
    ) -> None:
        detection.runtime = runtime

        result = gco("dashboard", "start", "--no-open")

        assert result.exit_code == 0, result.output
        assert fake.called("start_container")[0][0] == (runtime,)
        state = container.load_state()
        assert state is not None
        assert (state.runtime, state.image) == (runtime, container.image_ref(runtime))

    @pytest.mark.parametrize("no_open", [False, True])
    def test_a_running_dashboard_is_reused_with_its_link(
        self, fake: FakeContainers, browser: list[str], no_open: bool
    ) -> None:
        container.save_state(_state())
        fake.status = "running"

        result = gco("dashboard", "start", *(["--no-open"] if no_open else []))

        assert result.exit_code == 0, result.output
        assert f"The dashboard is already running: {EARLIER_URL}" in result.stdout
        assert browser == ([] if no_open else [EARLIER_URL])
        assert fake.names() == ["container_status"]
        assert container.load_state() == _state()

    @pytest.mark.parametrize(
        ("status", "state"),
        [
            ("running", None),
            ("running", _served_state()),
            ("exited", _state()),
            (None, _state()),
        ],
        ids=["running-without-state", "running-beside-a-serve", "exited", "removed"],
    )
    def test_otherwise_a_new_container_is_started(
        self, fake: FakeContainers, status: str | None, state: DashboardState | None
    ) -> None:
        if state is not None:
            container.save_state(state)
        fake.status = status

        result = gco("dashboard", "start", "--no-open")

        assert result.exit_code == 0, result.output
        assert len(fake.called("start_container")) == 1
        current = container.load_state()
        assert current is not None
        assert (current.mode, current.token) == ("container", TOKEN)

    def test_without_a_runtime_it_points_at_serve(
        self, fake: FakeContainers, detection: Detection
    ) -> None:
        detection.runtime = None

        result = gco("dashboard", "start")

        assert result.exit_code == 1
        assert f"✗ {NO_RUNTIME}" in result.stderr
        assert "Or run the dashboard in this process: gco dashboard serve" in result.stdout
        assert fake.calls == []
        assert container.load_state() is None

    def test_outside_a_checkout_it_points_at_serve(self, fake: FakeContainers) -> None:
        fake.context = None

        result = gco("dashboard", "start")

        assert result.exit_code == 1
        assert (
            "Dockerfile.dashboard was not found: run this from a GCO checkout, or run the "
            "dashboard in this process with 'gco dashboard serve'."
        ) in result.stderr
        assert fake.called("find_build_context") == [(container.default_build_contexts(), {})]
        assert fake.called("build_image") == []
        assert fake.called("start_container") == []

    @pytest.mark.parametrize("step", ["build_image", "start_container"])
    def test_a_runtime_failure_is_reported_and_nothing_is_recorded(
        self, fake: FakeContainers, browser: list[str], step: str
    ) -> None:
        fake.label = None
        fake.errors[step] = ContainerError("'docker run --detach' failed (exit 125): port in use")

        result = gco("dashboard", "start")

        assert result.exit_code == 1
        assert "✗ 'docker run --detach' failed (exit 125): port in use" in result.stderr
        assert fake.called("wait_until_healthy") == []
        assert container.load_state() is None
        assert browser == []

    def test_a_runtime_that_fails_before_anything_starts_is_reported(
        self, fake: FakeContainers, browser: list[str]
    ) -> None:
        fake.errors["container_status"] = ContainerError(HUNG)

        result = gco("dashboard", "start")

        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert result.stderr == f"✗ {HUNG}\n"
        assert fake.names() == ["container_status"]
        assert container.load_state() is None
        assert browser == []

    def test_a_container_that_never_answers_points_at_its_logs(
        self, fake: FakeContainers, browser: list[str]
    ) -> None:
        fake.ready = False

        result = gco("dashboard", "start")

        assert result.exit_code == 1
        assert (
            "The dashboard container started but does not answer; see 'gco dashboard logs'."
        ) in result.stderr
        assert container.load_state() is None
        assert browser == []

    @pytest.mark.parametrize(
        "args",
        [["--port", "0"], ["--port", "65536"], ["--project-dir", "missing"]],
        ids=["port-0", "port-too-high", "missing-project"],
    )
    def test_bad_options_are_usage_errors(self, fake: FakeContainers, args: list[str]) -> None:
        result = gco("dashboard", "start", *args)

        assert result.exit_code == 2
        assert fake.calls == []


class TestStartAndServeInTheDevContainer:
    """The ``gco`` function publishes container port 8501 on the host's loopback for both."""

    @pytest.fixture(autouse=True)
    def _dev_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GCO_DEV_CONTAINER", "1")

    @pytest.mark.parametrize("command", ["start", "serve"])
    @pytest.mark.parametrize(
        ("published", "host_port"),
        [(None, 8501), ("", 8501), ("9123", 9123)],
        ids=["default", "empty", "published"],
    )
    def test_serves_in_the_foreground_on_the_published_port(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeContainers,
        served: FakeServer,
        detection: Detection,
        threads: list[dict[str, Any]],
        browser: list[str],
        tmp_path: Path,
        command: str,
        published: str | None,
        host_port: int,
    ) -> None:
        if published is not None:
            monkeypatch.setenv(dashboard_cmd.HOST_PORT_ENV, published)

        result = gco("dashboard", command)

        url = f"http://localhost:{host_port}/?token={TOKEN}"
        assert result.exit_code == 0, result.output
        # The container's port is published on the host's loopback only, so the
        # server listens on every interface inside it, on Streamlit's port, and
        # Streamlit's own banner names the published port, as the link does. Its
        # PID would mean nothing to the other dev containers, so none is kept.
        assert served.calls == [
            {
                "port": 8501,
                "address": "0.0.0.0",
                "token": TOKEN,
                "project_dir": tmp_path.resolve(),
                "browser_port": host_port,
                "state": DashboardState(
                    mode="serve",
                    port=host_port,
                    token=TOKEN,
                    project_dir=str(tmp_path.resolve()),
                    started_at=STARTED,
                    pid=None,
                ),
            }
        ]
        assert f"Dashboard starting at {url}" in result.stdout
        assert "Press Ctrl-C to stop it." in result.stdout
        assert fake.calls == []
        assert detection.calls == 0
        assert threads == []
        assert browser == []
        assert container.load_state() is None

    @pytest.mark.parametrize("command", ["start", "serve"])
    def test_streamlit_is_told_the_published_port_the_link_names(
        self, monkeypatch: pytest.MonkeyPatch, command: str
    ) -> None:
        """Streamlit's own banner, printed under the link, names port 8502 too, not 8501.

        The real ``serve`` runs and Streamlit's command line is recorded.
        ``serve`` sets the project root and the token variable; both are
        restored after the test.
        """
        from streamlit.web import cli as streamlit_cli

        argvs: list[list[str]] = []
        monkeypatch.setattr(streamlit_cli, "main", lambda *, args, **kwargs: argvs.append(args))
        monkeypatch.setattr(dashboard_cmd, "serve", server.serve)
        monkeypatch.setattr(cli_config, "_project_root", None)
        monkeypatch.setenv(TOKEN_ENV, "")
        monkeypatch.setenv(dashboard_cmd.HOST_PORT_ENV, "8502")

        result = gco("dashboard", command)

        assert result.exit_code == 0, result.output
        assert f"Dashboard starting at http://localhost:8502/?token={TOKEN}" in result.stdout
        assert argvs == [server.streamlit_args(port=8501, address="0.0.0.0", browser_port=8502)]

    def test_serve_ignores_the_port_and_opens_no_browser_there(
        self, served: FakeServer, threads: list[dict[str, Any]], browser: list[str]
    ) -> None:
        """The function publishes container port 8501 at GCO_DASHBOARD_PORT, whatever --port says."""
        result = gco("dashboard", "serve", "--port", "9000")

        assert result.exit_code == 0, result.output
        assert (served.calls[0]["port"], served.calls[0]["address"]) == (8501, "0.0.0.0")
        assert (served.calls[0]["state"].port, served.calls[0]["browser_port"]) == (8501, 8501)
        assert f"Dashboard starting at http://localhost:8501/?token={TOKEN}" in result.stdout
        assert threads == []
        assert browser == []

    def test_serve_listens_on_an_address_given_explicitly(self, served: FakeServer) -> None:
        result = gco("dashboard", "serve", "--address", "127.0.0.1", "--no-token")

        assert result.exit_code == 0, result.output
        assert (served.calls[0]["address"], served.calls[0]["token"]) == ("127.0.0.1", None)

    @pytest.mark.parametrize("command", ["start", "serve"])
    def test_keeps_its_token_on_every_interface(self, served: FakeServer, command: str) -> None:
        """Both listen on every interface of the dev container, which runs other tools too."""
        result = gco("dashboard", command, "--no-token")

        assert result.exit_code == 2
        assert (
            "A dashboard listening beyond loopback needs its access token: drop --no-token."
        ) in result.stderr
        assert served.calls == []
        assert container.load_state() is None

    @pytest.mark.parametrize("command", ["start", "serve"])
    @pytest.mark.parametrize("published", ["web", "0", "70000", "-1"])
    def test_a_published_port_that_is_not_a_port_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        served: FakeServer,
        command: str,
        published: str,
    ) -> None:
        monkeypatch.setenv(dashboard_cmd.HOST_PORT_ENV, published)

        result = gco("dashboard", command)

        assert result.exit_code == 2
        assert (
            f"GCO_DASHBOARD_PORT={published!r} is not a port number (1-65535)."
        ) in result.stderr
        assert served.calls == []
        assert container.load_state() is None

    def test_serve_help_names_both_default_addresses(self) -> None:
        result = gco("dashboard", "serve", "--help")

        assert result.exit_code == 0, result.output
        text = " ".join(result.stdout.split())
        assert "[default: (127.0.0.1; every interface in the gco-dev container)]" in text
        assert "on every interface unless --address says otherwise" in text


# ---------------------------------------------------------------------------
# serve: this process
# ---------------------------------------------------------------------------


def _exit_like_streamlit() -> None:
    """Streamlit's command line ends with ``sys.exit(0)`` in standalone mode."""
    sys.exit(0)


def _refuse(**kwargs: Any) -> None:
    raise AssertionError("Streamlit must not start")


def _block_streamlit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make Streamlit unimportable, as without the extra (already-imported submodules too)."""
    names = [name for name in sys.modules if name == "streamlit" or name.startswith("streamlit.")]
    for name in names:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "streamlit", None)


class TestServe:
    def test_serves_here_with_a_fresh_token_and_opens_the_browser_once_ready(
        self, served: FakeServer, threads: list[dict[str, Any]], tmp_path: Path
    ) -> None:
        result = gco("dashboard", "serve", "--port", "9000")

        url = f"http://localhost:9000/?token={TOKEN}"
        assert result.exit_code == 0, result.output
        # The record names this process (the server runs in it), for ``stop``.
        # Browsers reach it on the port it listens on.
        assert served.calls == [
            {
                "port": 9000,
                "address": "127.0.0.1",
                "token": TOKEN,
                "project_dir": tmp_path.resolve(),
                "browser_port": 9000,
                "state": DashboardState(
                    mode="serve",
                    port=9000,
                    token=TOKEN,
                    project_dir=str(tmp_path.resolve()),
                    started_at=STARTED,
                    pid=os.getpid(),
                ),
            }
        ]
        assert threads == [
            {"target": dashboard_cmd._open_when_ready, "args": (9000, url), "daemon": True}
        ]
        assert f"Dashboard starting at {url}" in result.stdout
        assert container.load_state() is None

    def test_no_open_starts_no_browser_thread(
        self, served: FakeServer, threads: list[dict[str, Any]]
    ) -> None:
        result = gco("dashboard", "serve", "--no-open")

        assert result.exit_code == 0, result.output
        assert threads == []
        assert served.calls[0]["port"] == 8501

    def test_the_project_dir_is_resolved(self, served: FakeServer, tmp_path: Path) -> None:
        (tmp_path / "checkout").mkdir()

        result = gco("dashboard", "serve", "--no-open", "--project-dir", "checkout")

        assert result.exit_code == 0, result.output
        assert served.calls[0]["project_dir"] == (tmp_path / "checkout").resolve()

    def test_a_non_loopback_address_keeps_the_token(self, served: FakeServer) -> None:
        result = gco("dashboard", "serve", "--no-open", "--address", "0.0.0.0")

        assert result.exit_code == 0, result.output
        assert (served.calls[0]["address"], served.calls[0]["token"]) == ("0.0.0.0", TOKEN)

    @pytest.mark.parametrize("address", ["0.0.0.0", "::", "192.0.2.10"])
    def test_no_token_beyond_loopback_is_refused(self, served: FakeServer, address: str) -> None:
        result = gco("dashboard", "serve", "--no-token", "--address", address)

        assert result.exit_code == 2
        assert (
            "A dashboard listening beyond loopback needs its access token: drop --no-token."
        ) in result.stderr
        assert served.calls == []
        assert container.load_state() is None

    @pytest.mark.parametrize("address", ["127.0.0.1", "localhost", "::1"])
    def test_no_token_is_allowed_on_loopback(
        self, served: FakeServer, tmp_path: Path, address: str
    ) -> None:
        result = gco("dashboard", "serve", "--no-open", "--no-token", "--address", address)

        assert result.exit_code == 0, result.output
        assert (served.calls[0]["address"], served.calls[0]["token"]) == (address, None)
        assert served.calls[0]["state"] == DashboardState(
            mode="serve",
            port=8501,
            token=None,
            project_dir=str(tmp_path.resolve()),
            started_at=STARTED,
            pid=os.getpid(),
        )
        assert "Dashboard starting at http://localhost:8501/\n" in result.stdout

    @pytest.mark.parametrize(
        ("command", "dev_container"),
        [("serve", False), ("start", True), ("serve", True)],
        ids=["serve", "dev-container-start", "dev-container-serve"],
    )
    def test_without_streamlit_it_names_the_extra_before_printing_or_recording_a_link(
        self,
        monkeypatch: pytest.MonkeyPatch,
        threads: list[dict[str, Any]],
        detection: Detection,
        command: str,
        dev_container: bool,
    ) -> None:
        """The real ``serve`` runs (it would refuse too); the command checks before it."""
        from streamlit.web import cli as streamlit_cli

        saved = _record_saves(monkeypatch)
        monkeypatch.setattr(streamlit_cli, "main", _refuse)
        monkeypatch.setattr(dashboard_cmd, "serve", server.serve)
        _block_streamlit(monkeypatch)
        if dev_container:
            monkeypatch.setenv("GCO_DEV_CONTAINER", "1")

        result = gco("dashboard", command)

        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert "✗ The dashboard needs Streamlit" in result.stderr
        assert "pip install 'gco-cli[dashboard]'" in result.stderr
        assert "Dashboard starting" not in result.output
        assert "Ctrl-C" not in result.output
        assert saved == []
        assert threads == []
        assert detection.calls == 0
        assert container.load_state() is None
        assert TOKEN_ENV not in os.environ

    @pytest.mark.parametrize("ending", [None, _exit_like_streamlit], ids=["return", "exit"])
    def test_the_state_is_cleared_when_the_server_stops(
        self, served: FakeServer, ending: Callable[[], None] | None
    ) -> None:
        if ending is not None:
            served.during = ending

        result = gco("dashboard", "serve", "--no-open")

        assert result.exit_code == 0, result.output
        assert len(served.calls) == 1
        assert container.load_state() is None

    @pytest.mark.parametrize(
        "replacement",
        [None, _state(), _served_state(token="other-token")],
        ids=["removed", "container-started-since", "another-serve"],
    )
    def test_a_state_that_is_no_longer_this_servers_is_left_alone(
        self, served: FakeServer, replacement: DashboardState | None
    ) -> None:
        def replace() -> None:
            if replacement is None:
                container.clear_state()
            else:
                container.save_state(replacement)

        served.during = replace

        result = gco("dashboard", "serve", "--no-open")

        assert result.exit_code == 0, result.output
        assert container.load_state() == replacement

    def test_another_tokenless_servers_record_is_left_alone(self, served: FakeServer) -> None:
        """Two dashboards without a token: the first to stop keeps the other's record."""
        other = _served_state(token=None)
        served.during = lambda: container.save_state(other)

        result = gco("dashboard", "serve", "--no-open", "--no-token")

        assert result.exit_code == 0, result.output
        assert container.load_state() == other


@pytest.mark.parametrize(("ready", "opened"), [(True, True), (False, False)])
def test_the_opener_waits_for_the_server_before_opening_the_browser(
    fake: FakeContainers, browser: list[str], ready: bool, opened: bool
) -> None:
    fake.ready = ready
    url = "http://localhost:9000/?token=abc"

    dashboard_cmd._open_when_ready(9000, url)

    assert fake.called("wait_until_healthy") == [((9000,), {"timeout": 60})]
    assert browser == ([url] if opened else [])


# ---------------------------------------------------------------------------
# stop, status, logs, open
# ---------------------------------------------------------------------------


class TestStop:
    @pytest.mark.parametrize("answering", [False, True], ids=["starting", "answering"])
    def test_a_served_dashboard_whose_process_runs_is_stopped_where_it_runs(
        self, fake: FakeContainers, detection: Detection, probe: Probe, answering: bool
    ) -> None:
        """A server still starting does not answer yet; its live PID keeps its record."""
        state = _served_state(pid=os.getpid())
        container.save_state(state)
        probe.answering = answering

        result = gco("dashboard", "stop")

        assert result.exit_code == 0, result.output
        assert result.stdout == (
            "ℹ The dashboard runs in a terminal ('gco dashboard serve'); press Ctrl-C there.\n"
        )
        assert probe.ports == []
        assert fake.calls == []
        assert detection.calls == 0
        assert container.load_state() == state

    @pytest.mark.parametrize("answering", [False, True], ids=["silent", "port-reused"])
    def test_a_served_dashboard_whose_process_is_gone_is_forgotten(
        self,
        fake: FakeContainers,
        detection: Detection,
        probe: Probe,
        exited_pid: int,
        answering: bool,
    ) -> None:
        """Closing the serving terminal kills it with SIGHUP, before it can clear its record."""
        container.save_state(_served_state(pid=exited_pid))
        probe.answering = answering

        result = gco("dashboard", "stop")

        assert result.exit_code == 0, result.output
        assert result.stdout == "ℹ The dashboard is no longer running.\n"
        assert probe.ports == []
        assert container.load_state() is None
        assert fake.calls == []
        assert detection.calls == 0

    def test_without_a_pid_a_served_dashboard_that_answers_is_stopped_where_it_runs(
        self, fake: FakeContainers, detection: Detection, probe: Probe
    ) -> None:
        """A record written in the dev container has no PID: its port is the next best sign."""
        container.save_state(_served_state())
        probe.answering = True

        result = gco("dashboard", "stop")

        assert result.exit_code == 0, result.output
        assert (
            "The dashboard runs in a terminal ('gco dashboard serve'); press Ctrl-C there."
        ) in result.stdout
        assert probe.ports == [9100]
        assert fake.calls == []
        assert detection.calls == 0
        assert container.load_state() == _served_state()

    def test_without_a_pid_a_served_dashboard_that_no_longer_answers_is_forgotten(
        self, fake: FakeContainers, detection: Detection, probe: Probe
    ) -> None:
        container.save_state(_served_state())

        result = gco("dashboard", "stop")

        assert result.exit_code == 0, result.output
        assert result.stdout == "ℹ The dashboard is no longer running.\n"
        assert probe.ports == [9100]
        assert container.load_state() is None
        assert fake.calls == []
        assert detection.calls == 0

    @pytest.mark.parametrize("answering", [False, True])
    @pytest.mark.parametrize("host_pid", [False, True], ids=["no-pid", "host-pid"])
    def test_in_the_dev_container_a_served_dashboard_is_left_to_its_terminal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeContainers,
        detection: Detection,
        probe: Probe,
        exited_pid: int,
        answering: bool,
        host_pid: bool,
    ) -> None:
        """Another dev container cannot reach it, and a PID the host wrote means nothing here."""
        monkeypatch.setenv("GCO_DEV_CONTAINER", "1")
        state = _served_state(pid=exited_pid if host_pid else None)
        container.save_state(state)
        probe.answering = answering

        result = gco("dashboard", "stop")

        assert result.exit_code == 0, result.output
        assert result.stdout == f"ℹ {IN_ITS_TERMINAL}: press Ctrl-C there to stop it.\n"
        assert probe.ports == []
        assert container.load_state() == state
        assert fake.calls == []
        assert detection.calls == 0

    def test_removes_the_container_with_the_runtime_that_started_it(
        self, fake: FakeContainers, detection: Detection
    ) -> None:
        container.save_state(_state(runtime="podman"))

        result = gco("dashboard", "stop")

        assert result.exit_code == 0, result.output
        assert fake.called("remove_container") == [(("podman",), {})]
        assert detection.calls == 0
        assert "Dashboard stopped." in result.stdout
        assert container.load_state() is None

    @pytest.mark.parametrize(
        "state", [None, _state(runtime=None)], ids=["no-state", "no-recorded-runtime"]
    )
    def test_otherwise_the_detected_runtime_is_asked(
        self, fake: FakeContainers, detection: Detection, state: DashboardState | None
    ) -> None:
        if state is not None:
            container.save_state(state)
        fake.removed = False

        result = gco("dashboard", "stop")

        assert result.exit_code == 0, result.output
        assert fake.called("remove_container") == [(("docker",), {})]
        assert detection.calls == 1
        assert "No dashboard container is running." in result.stdout
        assert container.load_state() is None

    def test_without_a_runtime_there_is_nothing_to_stop(
        self, fake: FakeContainers, detection: Detection
    ) -> None:
        detection.runtime = None
        container.save_state(_state(runtime=None))

        result = gco("dashboard", "stop")

        assert result.exit_code == 0, result.output
        assert fake.calls == []
        assert "No dashboard container is running." in result.stdout
        assert container.load_state() is None

    def test_a_failed_removal_is_reported_and_the_state_kept(self, fake: FakeContainers) -> None:
        container.save_state(_state())
        fake.errors["remove_container"] = ContainerError("'docker rm -f' failed (exit 1): busy")

        result = gco("dashboard", "stop")

        assert result.exit_code == 1
        assert "✗ 'docker rm -f' failed (exit 1): busy" in result.stderr
        assert container.load_state() == _state()


class TestStatus:
    def test_nothing_running(self, fake: FakeContainers, probe: Probe) -> None:
        result = gco("dashboard", "status")

        assert result.exit_code == 0, result.output
        assert (
            "The dashboard is not running. Start it with 'gco dashboard start'."
        ) in result.stdout
        assert probe.ports == [8501]
        assert fake.called("container_status") == [(("docker",), {})]

    def test_a_dashboard_gco_did_not_start(self, fake: FakeContainers, probe: Probe) -> None:
        probe.answering = True

        result = gco("dashboard", "status")

        assert result.exit_code == 0, result.output
        assert "A dashboard answers on port 8501 (not started by gco)." in result.stdout

    @pytest.mark.parametrize("status", ["exited", "running"])
    def test_a_container_that_does_not_answer(self, fake: FakeContainers, status: str) -> None:
        container.save_state(_state())
        fake.status = status

        result = gco("dashboard", "status")

        assert result.exit_code == 0, result.output
        assert f"The dashboard container is {status} and not answering." in result.stderr

    def test_a_running_container(self, fake: FakeContainers, probe: Probe) -> None:
        container.save_state(_state())
        fake.status = "running"
        probe.answering = True

        result = gco("dashboard", "status")

        assert result.exit_code == 0, result.output
        assert f"Dashboard running (container) at {EARLIER_URL}" in result.stdout
        assert "Started 2026-11-24T09:30:00+00:00 for /home/tester/gco" in result.stdout
        assert probe.ports == [9100]

    @pytest.mark.parametrize("answering", [True, False])
    def test_a_served_dashboard_is_not_looked_up_in_a_runtime(
        self, fake: FakeContainers, probe: Probe, detection: Detection, answering: bool
    ) -> None:
        container.save_state(_served_state())
        probe.answering = answering

        result = gco("dashboard", "status")

        assert result.exit_code == 0, result.output
        expected = (
            f"Dashboard running (serve) at {EARLIER_URL}"
            if answering
            else "The dashboard is not running."
        )
        assert expected in result.stdout
        assert fake.calls == []
        assert detection.calls == 0

    def test_without_a_runtime_there_is_no_container_to_report(
        self, fake: FakeContainers, detection: Detection
    ) -> None:
        detection.runtime = None

        result = gco("dashboard", "status")

        assert result.exit_code == 0, result.output
        assert fake.calls == []
        assert "The dashboard is not running." in result.stdout

    def test_a_runtime_that_fails_is_reported(self, fake: FakeContainers) -> None:
        container.save_state(_state())
        fake.errors["container_status"] = ContainerError(HUNG)

        result = gco("dashboard", "status")

        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert result.stderr == f"✗ {HUNG}\n"
        assert result.stdout == ""


class TestStatusInTheDevContainer:
    """Each ``gco`` runs in a dev container of its own, so a served dashboard is out of reach."""

    @pytest.fixture(autouse=True)
    def _dev_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GCO_DEV_CONTAINER", "1")

    def test_a_served_dashboard_it_cannot_reach_is_reported_with_its_link(
        self, fake: FakeContainers, probe: Probe, detection: Detection
    ) -> None:
        container.save_state(_served_state())

        result = gco("dashboard", "status")

        assert result.exit_code == 0, result.output
        assert result.stdout == (
            f"ℹ {IN_ITS_TERMINAL}, at {EARLIER_URL}\n"
            "ℹ Each gco command runs in a dev container of its own, so this one cannot "
            "check that it still answers.\n"
        )
        assert probe.ports == [9100]
        assert fake.calls == []
        assert detection.calls == 0

    def test_machine_output_says_running_is_unknown(self, fake: FakeContainers) -> None:
        container.save_state(_served_state())

        result = gco("--output", "json", "dashboard", "status")

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {
            "running": None,
            "mode": "serve",
            "url": EARLIER_URL,
            "port": 9100,
            "container": None,
            "started_at": "2026-11-24T09:30:00+00:00",
            "project_dir": "/home/tester/gco",
        }

    def test_a_dashboard_served_in_this_container_answers_as_usual(
        self, fake: FakeContainers, probe: Probe
    ) -> None:
        container.save_state(_served_state())
        probe.answering = True

        result = gco("dashboard", "status")

        assert result.exit_code == 0, result.output
        assert f"Dashboard running (serve) at {EARLIER_URL}" in result.stdout

    @pytest.mark.parametrize(
        ("state", "status", "expected"),
        [
            (None, None, "The dashboard is not running. Start it with 'gco dashboard start'."),
            (_state(), "exited", "The dashboard container is exited and not answering."),
        ],
        ids=["nothing-started", "container"],
    )
    def test_anything_else_is_reported_as_on_the_host(
        self,
        fake: FakeContainers,
        state: DashboardState | None,
        status: str | None,
        expected: str,
    ) -> None:
        if state is not None:
            container.save_state(state)
        fake.status = status

        result = gco("dashboard", "status")

        assert result.exit_code == 0, result.output
        assert expected in result.output
        assert len(fake.called("container_status")) == 1

    @pytest.mark.parametrize("output", ["json", "yaml"])
    def test_machine_output_is_one_document(
        self, fake: FakeContainers, probe: Probe, output: str
    ) -> None:
        container.save_state(_state())
        fake.status = "running"
        probe.answering = True

        result = gco("--output", output, "dashboard", "status")

        assert result.exit_code == 0, result.output
        parse = json.loads if output == "json" else yaml.safe_load
        assert parse(result.stdout) == {
            "running": True,
            "mode": "container",
            "url": EARLIER_URL,
            "port": 9100,
            "container": "running",
            "started_at": "2026-11-24T09:30:00+00:00",
            "project_dir": "/home/tester/gco",
        }

    def test_machine_output_without_a_dashboard(self, fake: FakeContainers) -> None:
        result = gco("--output", "json", "dashboard", "status")

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {
            "running": False,
            "mode": None,
            "url": None,
            "port": 8501,
            "container": None,
            "started_at": None,
            "project_dir": None,
        }

    def test_the_link_is_withheld_while_nothing_answers(self, fake: FakeContainers) -> None:
        container.save_state(_state())
        fake.status = "exited"

        result = gco("--output", "json", "dashboard", "status")

        document = json.loads(result.stdout)
        assert (document["running"], document["url"], document["container"]) == (
            False,
            None,
            "exited",
        )
        assert "earlier-token" not in result.output


class TestLogs:
    def test_prints_the_last_200_lines_by_default(self, fake: FakeContainers) -> None:
        fake.status = "running"

        result = gco("dashboard", "logs")

        assert result.exit_code == 0, result.output
        assert fake.called("stream_logs") == [(("docker",), {"follow": False, "tail": 200})]

    def test_follows_from_the_requested_tail_with_the_recorded_runtime(
        self, fake: FakeContainers, detection: Detection
    ) -> None:
        container.save_state(_state(runtime="podman"))
        fake.status = "exited"

        result = gco("dashboard", "logs", "-f", "-n", "50")

        assert result.exit_code == 0, result.output
        assert fake.called("stream_logs") == [(("podman",), {"follow": True, "tail": 50})]
        assert detection.calls == 0

    def test_the_runtimes_exit_code_is_passed_on(self, fake: FakeContainers) -> None:
        fake.status = "running"
        fake.logs_exit = 3

        assert gco("dashboard", "logs").exit_code == 3

    def test_without_a_container_there_are_no_logs(self, fake: FakeContainers) -> None:
        result = gco("dashboard", "logs")

        assert result.exit_code == 0, result.output
        assert "No dashboard container exists." in result.stdout
        assert fake.called("stream_logs") == []

    def test_without_a_runtime_there_are_no_logs(
        self, fake: FakeContainers, detection: Detection
    ) -> None:
        detection.runtime = None

        result = gco("dashboard", "logs")

        assert result.exit_code == 0, result.output
        assert "No dashboard container exists." in result.stdout
        assert fake.calls == []

    def test_a_negative_tail_is_a_usage_error(self, fake: FakeContainers) -> None:
        assert gco("dashboard", "logs", "--tail", "-1").exit_code == 2
        assert fake.calls == []

    def test_a_runtime_that_fails_is_reported(self, fake: FakeContainers) -> None:
        fake.errors["container_status"] = ContainerError(HUNG)

        result = gco("dashboard", "logs")

        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert result.stderr == f"✗ {HUNG}\n"
        assert fake.called("stream_logs") == []

    def test_in_the_dev_container_a_served_dashboard_logs_in_its_terminal(
        self, monkeypatch: pytest.MonkeyPatch, fake: FakeContainers, detection: Detection
    ) -> None:
        monkeypatch.setenv("GCO_DEV_CONTAINER", "1")
        container.save_state(_served_state())

        result = gco("dashboard", "logs")

        assert result.exit_code == 0, result.output
        assert result.stdout == f"ℹ {IN_ITS_TERMINAL}, and prints its log there.\n"
        assert fake.calls == []
        assert detection.calls == 0

    def test_in_the_dev_container_a_container_still_has_its_logs(
        self, monkeypatch: pytest.MonkeyPatch, fake: FakeContainers
    ) -> None:
        monkeypatch.setenv("GCO_DEV_CONTAINER", "1")
        container.save_state(_state())
        fake.status = "running"

        result = gco("dashboard", "logs")

        assert result.exit_code == 0, result.output
        assert fake.called("stream_logs") == [(("docker",), {"follow": False, "tail": 200})]


class TestOpen:
    def test_opens_the_running_dashboards_link(self, probe: Probe, browser: list[str]) -> None:
        container.save_state(_state())
        probe.answering = True

        result = gco("dashboard", "open")

        assert result.exit_code == 0, result.output
        assert EARLIER_URL in result.stdout
        assert browser == [EARLIER_URL]
        assert probe.ports == [9100]

    @pytest.mark.parametrize(
        ("state", "probed"), [(None, []), (_state(), [9100])], ids=["no-state", "not-answering"]
    )
    def test_refuses_when_nothing_answers(
        self, probe: Probe, browser: list[str], state: DashboardState | None, probed: list[int]
    ) -> None:
        if state is not None:
            container.save_state(state)

        result = gco("dashboard", "open")

        assert result.exit_code == 1
        assert (
            "The dashboard is not running. Start it with 'gco dashboard start'."
        ) in result.stderr
        assert probe.ports == probed
        assert browser == []

    def test_in_the_dev_container_a_served_dashboard_link_is_printed_for_the_host(
        self, monkeypatch: pytest.MonkeyPatch, probe: Probe, browser: list[str]
    ) -> None:
        """The dev container has no browser, and cannot reach a dashboard another one serves."""
        monkeypatch.setenv("GCO_DEV_CONTAINER", "1")
        container.save_state(_served_state())

        result = gco("dashboard", "open")

        assert result.exit_code == 0, result.output
        assert result.stdout == (
            f"ℹ {EARLIER_URL}\nℹ Open it in a browser on this machine. {IN_ITS_TERMINAL}.\n"
        )
        assert probe.ports == []
        assert browser == []

    @pytest.mark.parametrize("state", [None, _state()], ids=["nothing-started", "container"])
    def test_in_the_dev_container_anything_else_is_looked_up_as_on_the_host(
        self,
        monkeypatch: pytest.MonkeyPatch,
        probe: Probe,
        browser: list[str],
        state: DashboardState | None,
    ) -> None:
        monkeypatch.setenv("GCO_DEV_CONTAINER", "1")
        if state is not None:
            container.save_state(state)

        result = gco("dashboard", "open")

        assert result.exit_code == 1
        assert "The dashboard is not running." in result.stderr
        assert browser == []


# ---------------------------------------------------------------------------
# The real container module against a fake runtime
# ---------------------------------------------------------------------------


class InMemoryRuntime:
    """A container runtime at the ``subprocess.run`` boundary: labelled images and containers."""

    def __init__(self) -> None:
        self.labels: dict[str, str] = {}
        self.containers: dict[str, str] = {}
        self.commands: list[list[str]] = []
        self.run_env: list[dict[str, str]] = []

    def verbs(self) -> list[str]:
        verbs = [argv[1] for argv in self.commands]
        self.commands.clear()
        return verbs

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(argv))
        verb, rest = argv[1], argv[2:]
        stdout, code = "", 0
        if verb == "image":  # image inspect --format <label template> <ref>
            code = 0 if rest[-1] in self.labels else 1
            stdout = self.labels.get(rest[-1], "")
        elif verb == "build":
            tag = rest[rest.index("-t") + 1]
            self.labels[tag] = rest[rest.index("--label") + 1].partition("=")[2]
        elif verb == "inspect":
            code = 0 if rest[-1] in self.containers else 1
            stdout = self.containers.get(rest[-1], "")
        elif verb == "rm":
            del self.containers[rest[-1]]
        elif verb == "run":
            self.run_env.append(dict(kwargs["env"]))
            self.containers[rest[rest.index("--name") + 1]] = "running"
        else:
            raise AssertionError(f"unexpected runtime command: {argv}")
        return subprocess.CompletedProcess(argv, code, f"{stdout}\n", "")


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> InMemoryRuntime:
    """The real container module, a checkout under ``tmp_path`` and an in-memory Docker."""
    checkout = tmp_path / "checkout"
    (checkout / "cli").mkdir(parents=True)
    (checkout / "Dockerfile.dashboard").write_text("FROM scratch\n", encoding="utf-8")
    (checkout / "cli" / "main.py").write_text("VERSION = 1\n", encoding="utf-8")
    fake = InMemoryRuntime()
    _fake_subprocess(monkeypatch, fake)
    monkeypatch.setattr(container, "default_build_contexts", lambda: (checkout,))

    def answering(port: int) -> bool:
        return fake.containers.get(container.NAME) == "running"

    monkeypatch.setattr(container, "healthy", answering)
    monkeypatch.setattr(dashboard_cmd, "healthy", answering)
    return fake


def test_start_reuse_status_and_stop_against_a_runtime(runtime: InMemoryRuntime) -> None:
    started = gco("dashboard", "start", "--no-open")

    assert started.exit_code == 0, started.output
    assert runtime.verbs() == ["inspect", "image", "build", "inspect", "run"]
    assert runtime.run_env[-1][TOKEN_ENV] == TOKEN
    assert container.load_state() is not None

    again = gco("dashboard", "start", "--no-open")

    assert again.exit_code == 0, again.output
    assert f"The dashboard is already running: http://localhost:8501/?token={TOKEN}" in (
        again.stdout
    )
    assert runtime.verbs() == ["inspect"]

    status = gco("--output", "json", "dashboard", "status")

    assert status.exit_code == 0, status.output
    document = json.loads(status.stdout)
    assert (document["running"], document["container"]) == (True, "running")
    assert runtime.verbs() == ["inspect"]

    stopped = gco("dashboard", "stop")

    assert stopped.exit_code == 0, stopped.output
    assert "Dashboard stopped." in stopped.stdout
    assert runtime.verbs() == ["inspect", "rm"]
    assert runtime.containers == {}
    assert container.load_state() is None


def test_start_on_another_port_tells_the_container_that_port(runtime: InMemoryRuntime) -> None:
    """The server listens on 8501 inside; the address it logs should name the host's port."""
    result = gco("dashboard", "start", "--port", "9000", "--no-open")

    assert result.exit_code == 0, result.output
    (argv,) = [command for command in runtime.commands if command[1] == "run"]
    pairs = list(itertools.pairwise(argv))
    assert ("--publish", "127.0.0.1:9000:8501") in pairs
    assert ("--env", "GCO_DASHBOARD_BROWSER_PORT=9000") in pairs
    assert f"Dashboard running at http://localhost:9000/?token={TOKEN}" in result.stdout


def _hung(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """A runtime that never answers: ``subprocess.run`` stops it at its timeout."""
    raise subprocess.TimeoutExpired(argv, kwargs["timeout"])


@pytest.mark.parametrize("command", ["start", "stop", "status", "logs"])
def test_a_hung_runtime_ends_each_command_with_its_message(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """The real container module, its runtime hanging: a message and exit 1, no traceback."""
    _fake_subprocess(monkeypatch, _hung)
    container.save_state(_state())

    result = gco("dashboard", command)

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.stderr == f"✗ {HUNG}\n"
    assert result.stdout == ""
    assert container.load_state() == _state()


def test_the_image_is_rebuilt_only_when_its_sources_change(
    runtime: InMemoryRuntime, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"

    def restart() -> list[str]:
        assert gco("dashboard", "stop").exit_code == 0
        runtime.verbs()
        result = gco("dashboard", "start", "--no-open")
        assert result.exit_code == 0, result.output
        return runtime.verbs()

    assert gco("dashboard", "start", "--no-open").exit_code == 0
    assert "build" in runtime.verbs()
    assert runtime.labels == {"gco-dashboard:local": container.source_fingerprint(checkout)}

    assert restart() == ["inspect", "image", "inspect", "run"]

    (checkout / "cli" / "main.py").write_text("VERSION = 2\n", encoding="utf-8")

    assert restart() == ["inspect", "image", "build", "inspect", "run"]
    assert runtime.labels == {"gco-dashboard:local": container.source_fingerprint(checkout)}
    assert restart() == ["inspect", "image", "inspect", "run"]


# ---------------------------------------------------------------------------
# Without Streamlit (the optional extra)
# ---------------------------------------------------------------------------


class TestWithoutStreamlit:
    @pytest.mark.parametrize(
        "args",
        [
            ["--help"],
            ["dashboard", "--help"],
            *(["dashboard", name, "--help"] for name in SUBCOMMANDS),
        ],
        ids=["gco", "dashboard", *SUBCOMMANDS],
    )
    def test_help_renders(self, monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
        _block_streamlit(monkeypatch)

        result = gco(*args)

        assert result.exit_code == 0, result.output
        assert result.stdout.startswith("Usage:")

    def test_help_lists_the_dashboard_and_its_commands(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _block_streamlit(monkeypatch)

        root = gco("--help").stdout
        group = gco("dashboard", "--help").stdout

        assert re.search(r"^\s+dashboard\s+Run the GCO dashboard", root, re.MULTILINE)
        for name in SUBCOMMANDS:
            assert re.search(rf"^\s+{name}\s", group, re.MULTILINE), name

    def test_a_fresh_interpreter_loads_the_cli_without_importing_streamlit(
        self, tmp_path: Path
    ) -> None:
        script = r"""
import json
import sys

from click.testing import CliRunner

from cli.main import cli

codes = [
    CliRunner().invoke(cli, args).exit_code
    for args in (["--help"], ["dashboard", "--help"], ["dashboard", "start", "--help"])
]
loaded = sorted(name for name in sys.modules if name.split(".")[0] == "streamlit")
print(json.dumps({"exit_codes": codes, "streamlit_modules": loaded}))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert json.loads(completed.stdout.splitlines()[-1]) == {
            "exit_codes": [0, 0, 0],
            "streamlit_modules": [],
        }
