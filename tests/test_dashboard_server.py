"""Unit tests for ``cli/dashboard/server.py``, the dashboard's Streamlit launcher.

``serve`` hands Streamlit its own command line in this process, so the tests
replace ``streamlit.web.cli.main`` with a recorder and never start a server.
The health probe's ``urllib.request.urlopen`` is faked too, so nothing opens a
socket. The settings tests pin the security-relevant flags (loopback bind,
CORS and XSRF protection on, usage statistics, static serving and the agent
API off, the allowed host names), and one parses the exact argument list with
Streamlit's own ``run`` command, so an option Streamlit renames or retypes in
an upgrade fails here instead of at ``gco dashboard start``. The port browsers
use, when the server's port is published on another, is followed from the
image entrypoint's flag and environment variable to the start-up banner,
which a fresh interpreter prints through Streamlit's own ``run`` command with
the server start replaced.
"""

from __future__ import annotations

import http.client
import os
import re
import runpy
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

import cli.config
from cli.dashboard import server
from cli.dashboard.server import (
    ALLOWED_HOSTS,
    APP,
    BROWSER_PORT_ENV,
    DEFAULT_PORT,
    LOOPBACK,
    MAX_UPLOAD_MB,
    SETTINGS,
    TOKEN_ENV,
    DashboardUnavailableError,
    dashboard_url,
    healthy,
    main,
    new_token,
    require_streamlit,
    serve,
    streamlit_args,
    streamlit_installed,
)

HEALTH_URL = "http://127.0.0.1:{port}/_stcore/health"
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _process_state_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    """``serve`` writes the token into ``os.environ`` and may set the project root; undo both.

    ``setenv`` before ``delenv`` makes monkeypatch record the variable's
    original state, so whatever ``serve`` leaves behind is rolled back. A
    browser port set in the calling shell is dropped too, so ``main`` reads
    only what a test sets.
    """
    monkeypatch.setenv(TOKEN_ENV, "set-by-fixture")
    monkeypatch.delenv(TOKEN_ENV)
    monkeypatch.delenv(BROWSER_PORT_ENV, raising=False)
    monkeypatch.setattr(cli.config, "_project_root", None)


@pytest.fixture
def streamlit_main(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace Streamlit's command line with a recorder of each call and the token it saw."""
    from streamlit.web import cli as streamlit_cli

    calls: list[dict[str, Any]] = []

    def fake_main(**kwargs: Any) -> None:
        calls.append({**kwargs, "token_env": os.environ.get(TOKEN_ENV)})

    monkeypatch.setattr(streamlit_cli, "main", fake_main)
    return calls


class _Response:
    """What ``urlopen`` returns: a context manager with a status and a body."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        self.reads: list[int] = []

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self, amount: int) -> bytes:
        self.reads.append(amount)
        return self.body[:amount]


class _Probe:
    """Answers ``urlopen`` with ``answer`` (a response, or an exception to raise)."""

    def __init__(self) -> None:
        self.answer: _Response | BaseException = _Response(200, b"ok")
        self.requests: list[tuple[str, float]] = []

    def urlopen(self, url: str, *, timeout: float) -> _Response:
        self.requests.append((url, timeout))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> _Probe:
    fake = _Probe()
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_the_server_listens_on_loopback_at_streamlits_port(self) -> None:
        assert (DEFAULT_PORT, LOOPBACK) == (8501, "127.0.0.1")
        assert TOKEN_ENV == "GCO_DASHBOARD_TOKEN"
        assert BROWSER_PORT_ENV == "GCO_DASHBOARD_BROWSER_PORT"

    def test_the_app_is_the_entrypoint_next_to_the_module(self) -> None:
        assert Path(server.__file__).with_name("app.py") == APP
        assert APP.is_file()

    def test_browsers_may_reach_it_only_by_loopback_host_names(self) -> None:
        assert ALLOWED_HOSTS == ("localhost", "127.0.0.1")

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("server.headless", "true"),
            ("server.runOnSave", "false"),
            ("server.fileWatcherType", "none"),
            ("server.enableCORS", "true"),
            ("server.enableXsrfProtection", "true"),
            ("server.enableStaticServing", "false"),
            ("server.enableAgentApi", "false"),
            ("server.showEmailPrompt", "false"),
            ("server.maxUploadSize", "5"),
            ("browser.gatherUsageStats", "false"),
            ("browser.serverAddress", "localhost"),
            ("global.showWarningOnDirectExecution", "false"),
        ],
    )
    def test_locked_down_setting(self, key: str, value: str) -> None:
        assert dict(SETTINGS)[key] == value

    def test_each_setting_is_given_once_and_the_ports_and_address_are_not_fixed(self) -> None:
        keys = [key for key, _ in SETTINGS]
        assert len(keys) == len(set(keys))
        assert not {
            "server.port",
            "server.address",
            "server.allowedHosts",
            "browser.serverPort",
        } & set(keys)

    def test_the_upload_limit_matches_the_setting(self) -> None:
        assert dict(SETTINGS)["server.maxUploadSize"] == str(MAX_UPLOAD_MB)


class TestStreamlitArgs:
    def test_run_the_app_with_port_address_settings_then_allowed_hosts(self) -> None:
        settings = [item for key, value in SETTINGS for item in (f"--{key}", value)]

        assert streamlit_args(port=9000, address="0.0.0.0") == [
            "run",
            str(APP),
            "--server.port",
            "9000",
            "--server.address",
            "0.0.0.0",
            *settings,
            "--server.allowedHosts",
            "localhost",
            "--server.allowedHosts",
            "127.0.0.1",
        ]

    @pytest.mark.parametrize("browser_port", [8502, 8501, 1, 65535])
    def test_a_browser_port_follows_the_address_when_given(self, browser_port: int) -> None:
        plain = streamlit_args(port=8501, address="0.0.0.0")

        published = streamlit_args(port=8501, address="0.0.0.0", browser_port=browser_port)

        assert published == [*plain[:6], "--browser.serverPort", str(browser_port), *plain[6:]]

    def test_without_a_browser_port_streamlit_names_the_server_port(self) -> None:
        """Unset, Streamlit's ``browser.serverPort`` defaults to ``server.port``."""
        from streamlit.web import cli as streamlit_cli

        args = streamlit_args(port=9000, address="127.0.0.1")
        run = streamlit_cli.main.commands["run"]

        assert streamlit_args(port=9000, address="127.0.0.1", browser_port=None) == args
        assert "--browser.serverPort" not in args
        assert run.make_context("run", args[1:]).params["browser_serverPort"] is None

    def test_streamlits_own_run_command_parses_every_flag_as_meant(self) -> None:
        """Drift guard: the installed Streamlit knows each option and keeps both hosts."""
        from streamlit.web import cli as streamlit_cli

        args = streamlit_args(port=9000, address="127.0.0.1", browser_port=9001)
        run = streamlit_cli.main.commands["run"]
        params = run.make_context("run", args[1:]).params

        expected = {
            "target": str(APP),
            "args": (),
            "server_port": 9000,
            "server_address": "127.0.0.1",
            "browser_serverPort": 9001,
            "server_allowedHosts": ("localhost", "127.0.0.1"),
            "server_headless": True,
            "server_runOnSave": False,
            "server_fileWatcherType": "none",
            "server_enableCORS": True,
            "server_enableXsrfProtection": True,
            "server_enableStaticServing": False,
            "server_enableAgentApi": False,
            "server_showEmailPrompt": False,
            "server_maxUploadSize": MAX_UPLOAD_MB,
            "browser_gatherUsageStats": False,
            "logger_hideWelcomeMessage": True,
            "browser_serverAddress": "localhost",
            "global_showWarningOnDirectExecution": False,
        }
        assert {key: params[key] for key in expected} == expected


#: Prints what ``streamlit run`` prints once its server listens, and the
#: port it takes browsers to use, for the arguments ``streamlit_args`` builds
#: from ``argv[1:]`` (port, address and browser port, ``-`` for none).
#: Streamlit's own ``run`` command loads them; only the server start is
#: replaced, and every address lookup is refused.
_BANNER_SCRIPT = r"""
import sys

from streamlit import config, net_util
from streamlit.web import bootstrap
from streamlit.web import cli as streamlit_cli

from cli.dashboard.server import streamlit_args


def refuse_lookup():
    raise AssertionError("the banner must not look up an address")


def print_banner(main_script_path, is_hello, args, flag_options):
    bootstrap._print_url(is_hello)
    print(f"browser port {config.get_option('browser.serverPort')}")


net_util.get_internal_ip = net_util.get_external_ip = refuse_lookup
bootstrap.run = print_banner
port, address, browser_port = sys.argv[1:]
streamlit_cli.main(
    args=streamlit_args(
        port=int(port),
        address=address,
        browser_port=None if browser_port == "-" else int(browser_port),
    ),
    prog_name="streamlit",
    standalone_mode=False,
)
"""


@pytest.mark.parametrize(
    ("browser_port", "configured"),
    [("8502", "8502"), ("-", "8501")],
    ids=["published-on-another-port", "no-browser-port"],
)
def test_streamlit_prints_no_link_of_its_own_and_knows_the_port_browsers_use(
    tmp_path: Path, browser_port: str, configured: str
) -> None:
    """gco prints the dashboard's link with its token; Streamlit's banner would omit it.

    Streamlit still takes the port the browser reaches (a published host
    port) for its own checks.

    A fresh interpreter runs it, because Streamlit's configuration is
    process-wide. Its home and working directory are empty, so no other
    Streamlit configuration applies, and nothing listens.
    """
    environ = {key: value for key, value in os.environ.items() if not key.startswith("STREAMLIT_")}

    completed = subprocess.run(
        [sys.executable, "-c", _BANNER_SCRIPT, "8501", "0.0.0.0", browser_port],
        cwd=tmp_path,
        env={**environ, "HOME": str(tmp_path), "PYTHONPATH": str(REPO_ROOT)},
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )

    lines = [line.strip() for line in completed.stdout.splitlines()]
    assert "You can now view your Streamlit app in your browser." not in lines
    assert [line for line in lines if "://" in line] == []
    assert f"browser port {configured}" in lines


# ---------------------------------------------------------------------------
# Tokens and links
# ---------------------------------------------------------------------------


def test_new_token_is_fresh_url_safe_and_carries_24_random_bytes() -> None:
    tokens = {new_token() for _ in range(5)}

    assert len(tokens) == 5
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{32}", token) for token in tokens)


@pytest.mark.parametrize(
    ("port", "token", "url"),
    [
        (8501, None, "http://localhost:8501/"),
        (8501, "", "http://localhost:8501/"),
        (9000, "abc_DEF-123", "http://localhost:9000/?token=abc_DEF-123"),
    ],
)
def test_dashboard_url_uses_localhost_and_appends_the_token(
    port: int, token: str | None, url: str
) -> None:
    assert dashboard_url(port, token) == url


def test_dashboard_url_without_a_token_argument() -> None:
    assert dashboard_url(8501) == "http://localhost:8501/"


# ---------------------------------------------------------------------------
# serve()
# ---------------------------------------------------------------------------


def test_streamlit_installed_reports_the_optional_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    assert streamlit_installed() is True  # the test environment installs the dashboard extra

    monkeypatch.setitem(sys.modules, "streamlit", None)

    assert streamlit_installed() is False


def test_require_streamlit_passes_with_the_extra_and_names_it_without(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check ``gco dashboard serve`` and ``start`` run before printing or recording a link."""
    require_streamlit()

    monkeypatch.setitem(sys.modules, "streamlit", None)

    with pytest.raises(DashboardUnavailableError) as excinfo:
        require_streamlit()

    assert str(excinfo.value) == (
        "The dashboard needs Streamlit, which ships in the 'dashboard' extra: "
        "pip install 'gco-cli[dashboard]' (the dev image already has it), or run "
        "it in its own container with 'gco dashboard start'."
    )


class TestServe:
    def test_runs_streamlit_in_this_process_with_the_locked_down_arguments(
        self, streamlit_main: list[dict[str, Any]]
    ) -> None:
        serve(port=9000, address="127.0.0.1", token="tok")

        assert streamlit_main == [
            {
                "args": streamlit_args(port=9000, address="127.0.0.1"),
                "prog_name": "streamlit",
                "standalone_mode": True,
                "token_env": "tok",
            }
        ]

    def test_defaults_to_loopback_on_streamlits_port_without_a_token(
        self, streamlit_main: list[dict[str, Any]]
    ) -> None:
        serve()

        (call,) = streamlit_main
        assert call["args"] == streamlit_args(port=8501, address="127.0.0.1")
        assert "--browser.serverPort" not in call["args"]
        assert call["token_env"] is None

    def test_a_browser_port_reaches_streamlit(self, streamlit_main: list[dict[str, Any]]) -> None:
        """The dev container serves on 8501 and the gco function publishes it on another port."""
        serve(port=8501, address="0.0.0.0", token="tok", browser_port=8502)

        (call,) = streamlit_main
        assert call["args"] == streamlit_args(port=8501, address="0.0.0.0", browser_port=8502)
        assert call["args"][6:8] == ["--browser.serverPort", "8502"]

    @pytest.mark.parametrize("token", [None, ""])
    def test_without_a_token_a_stale_one_is_removed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        streamlit_main: list[dict[str, Any]],
        token: str | None,
    ) -> None:
        monkeypatch.setenv(TOKEN_ENV, "left-over")

        serve(token=token)

        assert streamlit_main[0]["token_env"] is None
        assert TOKEN_ENV not in os.environ

    def test_project_dir_becomes_the_resolved_project_root(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        streamlit_main: list[dict[str, Any]],
    ) -> None:
        (tmp_path / "checkout").mkdir()
        monkeypatch.chdir(tmp_path)

        serve(project_dir=Path("checkout"))

        assert cli.config._project_root == (tmp_path / "checkout").resolve()
        assert len(streamlit_main) == 1

    def test_without_project_dir_the_working_directory_stays_the_project(
        self, streamlit_main: list[dict[str, Any]]
    ) -> None:
        serve()

        assert cli.config._project_root is None

    def test_without_streamlit_it_raises_before_changing_anything(
        self, monkeypatch: pytest.MonkeyPatch, streamlit_main: list[dict[str, Any]]
    ) -> None:
        monkeypatch.setitem(sys.modules, "streamlit", None)

        with pytest.raises(DashboardUnavailableError, match=r"pip install 'gco-cli\[dashboard\]'"):
            serve(token="tok", project_dir=Path("/home/tester/gco"))

        assert streamlit_main == []
        assert TOKEN_ENV not in os.environ
        assert cli.config._project_root is None

    def test_dashboard_unavailable_is_a_runtime_error(self) -> None:
        assert issubclass(DashboardUnavailableError, RuntimeError)


# ---------------------------------------------------------------------------
# healthy()
# ---------------------------------------------------------------------------


class TestHealthy:
    def test_asks_streamlits_health_endpoint_on_loopback(self, probe: _Probe) -> None:
        assert healthy() is True
        assert probe.requests == [(HEALTH_URL.format(port=8501), 3.0)]

    def test_honours_port_host_and_timeout(self, probe: _Probe) -> None:
        healthy(9000, host="localhost", timeout=0.5)

        assert probe.requests == [("http://localhost:9000/_stcore/health", 0.5)]

    @pytest.mark.parametrize(
        ("status", "body", "expected"),
        [
            (200, b"ok", True),
            (200, b"ok\n", True),
            (200, b"okay", False),
            (200, b"", False),
            (204, b"ok", False),
        ],
    )
    def test_only_a_200_answering_ok_counts(
        self, probe: _Probe, status: int, body: bytes, expected: bool
    ) -> None:
        probe.answer = _Response(status, body)

        assert healthy() is expected

    def test_reads_no_more_than_a_short_prefix_of_the_body(self, probe: _Probe) -> None:
        response = _Response(200, b"ok" + b" " * 100)
        probe.answer = response

        assert healthy() is True
        assert response.reads == [16]

    @pytest.mark.parametrize(
        "error",
        [
            urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
            urllib.error.HTTPError(
                HEALTH_URL.format(port=8501), 503, "Service Unavailable", hdrs=None, fp=None
            ),
            TimeoutError("timed out"),
            ConnectionResetError(54, "Connection reset by peer"),
            http.client.RemoteDisconnected("Remote end closed connection without response"),
            # Something that does not speak HTTP holds the port.
            http.client.BadStatusLine("SSH-2.0-OpenSSH_9.6"),
            http.client.IncompleteRead(b"o", 1),
        ],
        ids=[
            "refused",
            "http-error",
            "timeout",
            "reset",
            "disconnected",
            "not-http",
            "truncated",
        ],
    )
    def test_an_unreachable_or_foreign_listener_is_not_healthy(
        self, probe: _Probe, error: BaseException
    ) -> None:
        probe.answer = error

        assert healthy() is False


# ---------------------------------------------------------------------------
# main(): the dashboard image's entrypoint and HEALTHCHECK
# ---------------------------------------------------------------------------


def _unexpected(**kwargs: Any) -> None:
    raise AssertionError(f"serve() must not run: {kwargs}")


class TestMain:
    @pytest.mark.parametrize(("answering", "code"), [(True, 0), (False, 1)])
    def test_check_turns_the_health_probe_into_the_exit_code(
        self, monkeypatch: pytest.MonkeyPatch, answering: bool, code: int
    ) -> None:
        ports: list[int] = []

        def fake_healthy(port: int) -> bool:
            ports.append(port)
            return answering

        monkeypatch.setattr(server, "healthy", fake_healthy)
        monkeypatch.setattr(server, "serve", _unexpected)

        assert main(["--check", "--port", "9100"]) == code
        assert ports == [9100]

    def test_check_defaults_to_streamlits_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ports: list[int] = []
        monkeypatch.setattr(server, "healthy", lambda port: ports.append(port) or True)

        assert main(["--check"]) == 0
        assert ports == [8501]

    def test_serves_on_the_given_port_and_address_with_the_environment_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(server, "serve", lambda **kwargs: calls.append(kwargs))
        monkeypatch.setenv(TOKEN_ENV, "tok")

        assert main(["--port", "9000", "--address", "0.0.0.0"]) == 0
        assert calls == [{"port": 9000, "address": "0.0.0.0", "token": "tok", "browser_port": None}]

    @pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
    def test_serves_on_loopback_without_a_token_by_default(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ) -> None:
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(server, "serve", lambda **kwargs: calls.append(kwargs))
        if value is not None:
            monkeypatch.setenv(TOKEN_ENV, value)

        assert main([]) == 0
        assert calls == [
            {"port": 8501, "address": "127.0.0.1", "token": None, "browser_port": None}
        ]

    def test_rejects_a_port_that_is_not_a_number(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(server, "serve", _unexpected)

        with pytest.raises(SystemExit) as excinfo:
            main(["--port", "http"])

        assert excinfo.value.code == 2
        assert "invalid int value: 'http'" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("answer", "code"),
        [(_Response(200, b"ok"), 0), (ConnectionRefusedError(61, "Connection refused"), 1)],
        ids=["healthy", "down"],
    )
    def test_python_dash_m_check_exits_with_the_health_status(
        self,
        monkeypatch: pytest.MonkeyPatch,
        probe: _Probe,
        answer: _Response | BaseException,
        code: int,
    ) -> None:
        """``python -m cli.dashboard.server --check`` is the image's HEALTHCHECK command."""
        probe.answer = answer
        monkeypatch.delitem(sys.modules, "cli.dashboard.server")
        monkeypatch.setattr(sys, "argv", ["server", "--check", "--port", "9100"])

        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("cli.dashboard.server", run_name="__main__")

        assert excinfo.value.code == code
        assert probe.requests == [(HEALTH_URL.format(port=9100), 3.0)]


def _browser_port_served(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int | None:
    """The ``browser_port`` that ``main(argv)`` serves with."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(server, "serve", lambda **kwargs: calls.append(kwargs))

    assert main(argv) == 0
    (call,) = calls
    return call["browser_port"]


class TestMainBrowserPort:
    """``gco dashboard start`` tells the container the host port its port is published on."""

    @pytest.mark.parametrize(
        ("value", "port"), [("8502", 8502), ("1", 1), ("65535", 65535), (" 9000\n", 9000)]
    )
    def test_comes_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, value: str, port: int
    ) -> None:
        monkeypatch.setenv(BROWSER_PORT_ENV, value)

        assert _browser_port_served(monkeypatch, ["--address", "0.0.0.0"]) == port

    @pytest.mark.parametrize("value", [None, "8502"], ids=["unset", "set"])
    def test_the_flag_wins_over_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ) -> None:
        if value is not None:
            monkeypatch.setenv(BROWSER_PORT_ENV, value)

        assert _browser_port_served(monkeypatch, ["--browser-port", "9000"]) == 9000

    @pytest.mark.parametrize("value", ["", "web", "0", "-1", "65536", "8502.0", "0x2016"])
    def test_a_variable_that_is_not_a_port_number_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Streamlit then names the port the server listens on, as without the variable."""
        monkeypatch.setenv(BROWSER_PORT_ENV, value)

        assert _browser_port_served(monkeypatch, []) is None

    def test_a_flag_that_is_not_a_number_is_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(server, "serve", _unexpected)

        with pytest.raises(SystemExit) as excinfo:
            main(["--browser-port", "web"])

        assert excinfo.value.code == 2
        assert "argument --browser-port: invalid int value: 'web'" in capsys.readouterr().err

    def test_the_health_check_probes_the_servers_own_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The image's HEALTHCHECK runs inside the container, where the server listens on 8501."""
        ports: list[int] = []
        monkeypatch.setattr(server, "healthy", lambda port: ports.append(port) or True)
        monkeypatch.setattr(server, "serve", _unexpected)
        monkeypatch.setenv(BROWSER_PORT_ENV, "9000")

        assert main(["--check"]) == 0
        assert ports == [8501]

    def test_help_names_the_variable(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--help"])

        assert excinfo.value.code == 0
        help_text = " ".join(capsys.readouterr().out.split())
        assert "--browser-port BROWSER_PORT" in help_text
        assert f"(default: ${BROWSER_PORT_ENV})" in help_text
