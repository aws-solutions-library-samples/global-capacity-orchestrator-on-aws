"""Tests for cli/cluster_ui.py — the plumbing behind ``gco gitops`` and ``gco crossplane``.

Everything here runs without a cluster: kubectl, the port-forward process and
Playwright are replaced by fakes, and the one real socket test binds an
ephemeral loopback port. The argv builders and the Secret read are the
security-relevant seams (list-form argv, validated names, the Secret value
never in an error message), so they are pinned exactly.
"""

from __future__ import annotations

import base64
import json
import socket
import subprocess
import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from cli import cluster_ui

# ─── cdk.json and charts.yaml reads ──────────────────────────────────────────


class TestLoadCdkContext:
    def test_reads_the_context_object(self, tmp_path: Path) -> None:
        path = tmp_path / "cdk.json"
        path.write_text(json.dumps({"app": "x", "context": {"project_name": "p"}}))
        assert cluster_ui.load_cdk_context(path) == {"project_name": "p"}

    def test_defaults_to_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "cdk.json").write_text(json.dumps({"context": {"helm": {}}}))
        monkeypatch.chdir(tmp_path)
        assert cluster_ui.load_cdk_context() == {"helm": {}}

    def test_missing_file_is_named(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match=r"cdk\.json not found"):
            cluster_ui.load_cdk_context(tmp_path / "cdk.json")

    def test_invalid_json_is_named(self, tmp_path: Path) -> None:
        path = tmp_path / "cdk.json"
        path.write_text("{not json")
        with pytest.raises(RuntimeError, match="is not valid JSON"):
            cluster_ui.load_cdk_context(path)

    @pytest.mark.parametrize("document", [[], {"context": []}, {"app": "x"}])
    def test_documents_without_a_context_object_read_empty(
        self, tmp_path: Path, document: object
    ) -> None:
        path = tmp_path / "cdk.json"
        path.write_text(json.dumps(document))
        assert cluster_ui.load_cdk_context(path) == {}


class TestRegionalRegions:
    def test_lists_the_regional_entries(self) -> None:
        context = {"deployment_regions": {"regional": ["us-east-1", "us-west-2"]}}
        assert cluster_ui.regional_regions(context) == ["us-east-1", "us-west-2"]

    @pytest.mark.parametrize(
        "context",
        [{}, {"deployment_regions": []}, {"deployment_regions": {"regional": "us-east-1"}}],
    )
    def test_odd_shapes_read_empty(self, context: dict[str, Any]) -> None:
        assert cluster_ui.regional_regions(context) == []


class TestChartEntry:
    def test_reads_the_shipped_pins(self) -> None:
        entry = cluster_ui.chart_entry("argocd")
        assert entry is not None
        assert entry["name"] == "argocd"
        assert entry["chart"] == "argo-cd"
        assert entry["namespace"] == "argocd"
        assert entry["repo_url"] == "https://argoproj.github.io/argo-helm"
        assert entry["version"]

    def test_missing_fields_fall_back(self, tmp_path: Path) -> None:
        path = tmp_path / "charts.yaml"
        path.write_text("charts:\n  bare: {}\n")
        assert cluster_ui.chart_entry("bare", path) == {
            "name": "bare",
            "chart": "bare",
            "version": "",
            "namespace": "",
            "repo_url": "",
        }

    @pytest.mark.parametrize(
        "text", ["", "charts: []\n", "charts:\n  other: {}\n", "charts:\n  x: 3\n"]
    )
    def test_absent_entries_read_none(self, tmp_path: Path, text: str) -> None:
        path = tmp_path / "charts.yaml"
        path.write_text(text)
        assert cluster_ui.chart_entry("x", path) is None

    def test_unreadable_or_invalid_files_read_none(self, tmp_path: Path) -> None:
        assert cluster_ui.chart_entry("x", tmp_path / "missing.yaml") is None
        bad = tmp_path / "bad.yaml"
        bad.write_text("charts: [unclosed\n")
        assert cluster_ui.chart_entry("x", bad) is None


class TestManifestDocuments:
    def test_structural_tokens_parse_and_quoted_tokens_stay(self, tmp_path: Path) -> None:
        (tmp_path / "m.yaml").write_text(
            "---\n"
            "kind: AppProject\n"
            "metadata:\n"
            '  labels: {enabled: "{{ARGOCD_ENABLED}}"}\n'
            "spec:\n"
            "  sourceRepos: {{ARGOCD_SOURCE_REPOS}}\n"
            "---\n"
            "# a comment-only document\n"
            "---\n"
            "- not-a-mapping\n"
        )
        documents = cluster_ui.manifest_documents("m.yaml", tmp_path)
        assert documents == [
            {
                "kind": "AppProject",
                "metadata": {"labels": {"enabled": "{{ARGOCD_ENABLED}}"}},
                "spec": {"sourceRepos": None},
            }
        ]

    def test_missing_manifest_reads_empty(self, tmp_path: Path) -> None:
        assert cluster_ui.manifest_documents("absent.yaml", tmp_path) == []

    def test_shipped_manifests_parse(self) -> None:
        kinds = {
            doc["kind"] for doc in cluster_ui.manifest_documents("post-helm-argocd-access.yaml")
        }
        assert kinds == {"Role", "RoleBinding", "AppProject"}


# ─── kubectl ─────────────────────────────────────────────────────────────────


class TestKubectlServerFlags:
    def test_no_tunnel_means_no_flags(self) -> None:
        assert cluster_ui.kubectl_server_flags(None, None) == []

    def test_tunnel_flags(self) -> None:
        assert cluster_ui.kubectl_server_flags(
            "https://127.0.0.1:8443", "abc.gr7.us-east-1.eks.amazonaws.com"
        ) == [
            "--server",
            "https://127.0.0.1:8443",
            "--tls-server-name",
            "abc.gr7.us-east-1.eks.amazonaws.com",
        ]

    def test_rejects_a_non_https_server(self) -> None:
        with pytest.raises(ValueError, match="must start with https://"):
            cluster_ui.kubectl_server_flags("http://127.0.0.1:8443", None)

    @pytest.mark.parametrize("name", ["evil host;rm", "abc.eks.amazonaws.com\n", "", "a" * 256])
    def test_rejects_an_odd_tls_server_name(self, name: str) -> None:
        with pytest.raises(ValueError, match="Invalid --tls-server-name"):
            cluster_ui.kubectl_server_flags(None, name)


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestReadSecretKey:
    @staticmethod
    def _secret(**data: str) -> str:
        return json.dumps(
            {
                "data": {
                    key: base64.b64encode(value.encode()).decode() for key, value in data.items()
                }
            }
        )

    def test_decodes_the_key_with_a_list_form_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> _Completed:
            calls.append(cmd)
            assert kwargs["check"] is False and kwargs["timeout"] == 60
            return _Completed(stdout=self._secret(password="s3cret"))

        monkeypatch.setattr(cluster_ui.subprocess, "run", fake_run)
        value = cluster_ui.read_secret_key(
            "argocd",
            "argocd-initial-admin-secret",
            "password",
            server="https://127.0.0.1:8443",
            tls_server_name="abc.eks.amazonaws.com",
        )
        assert value == "s3cret"
        assert calls == [
            [
                "kubectl",
                "get",
                "secret",
                "argocd-initial-admin-secret",
                "-n",
                "argocd",
                "-o",
                "json",
                "--server",
                "https://127.0.0.1:8443",
                "--tls-server-name",
                "abc.eks.amazonaws.com",
            ]
        ]

    @pytest.mark.parametrize(
        ("namespace", "name", "label"),
        [("Bad_NS", "ok", "namespace"), ("ok", "a;b", "Secret name")],
    )
    def test_rejects_invalid_names(self, namespace: str, name: str, label: str) -> None:
        with pytest.raises(ValueError, match=f"Invalid {label}"):
            cluster_ui.read_secret_key(namespace, name, "k")

    def test_missing_kubectl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*_a: Any, **_k: Any) -> _Completed:
            raise FileNotFoundError("kubectl")

        monkeypatch.setattr(cluster_ui.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="kubectl not found"):
            cluster_ui.read_secret_key("argocd", "s", "k")

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*_a: Any, **_k: Any) -> _Completed:
            raise subprocess.TimeoutExpired("kubectl", 60)

        monkeypatch.setattr(cluster_ui.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="Timed out reading Secret argocd/s"):
            cluster_ui.read_secret_key("argocd", "s", "k")

    def test_kubectl_failure_carries_stderr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cluster_ui.subprocess,
            "run",
            lambda *_a, **_k: _Completed(
                returncode=1, stderr='Error from server (NotFound): secrets "s" not found\n'
            ),
        )
        with pytest.raises(RuntimeError, match=r"Failed to read Secret argocd/s: .*\(NotFound\)"):
            cluster_ui.read_secret_key("argocd", "s", "k")

    def test_kubectl_failure_without_stderr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cluster_ui.subprocess, "run", lambda *_a, **_k: _Completed(returncode=1, stderr=None)
        )
        with pytest.raises(RuntimeError, match=r"Failed to read Secret argocd/s: $"):
            cluster_ui.read_secret_key("argocd", "s", "k")

    @pytest.mark.parametrize("stdout", ["", "{}", '{"data": null}', '{"data": {"other": "eA=="}}'])
    def test_missing_key_never_leaks_other_values(
        self, monkeypatch: pytest.MonkeyPatch, stdout: str
    ) -> None:
        monkeypatch.setattr(
            cluster_ui.subprocess, "run", lambda *_a, **_k: _Completed(stdout=stdout)
        )
        with pytest.raises(RuntimeError, match="has no 'password' key") as excinfo:
            cluster_ui.read_secret_key("argocd", "s", "password")
        assert "eA==" not in str(excinfo.value)


class TestExecPortForward:
    def test_runs_the_argv_in_the_foreground(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[list[str], dict[str, Any]]] = []
        monkeypatch.setattr(cluster_ui.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
        cluster_ui.exec_port_forward(("kubectl", "port-forward"))
        assert calls == [(["kubectl", "port-forward"], {"check": False})]


# ─── Port readiness and the background port-forward ──────────────────────────


class _FakeProcess:
    def __init__(
        self,
        *,
        exited: int | None = None,
        stderr: bytes | None = b"",
        hang_on_wait: bool = False,
    ) -> None:
        self.returncode = exited
        self.stderr = None if stderr is None else types.SimpleNamespace(read=lambda: stderr)
        self._hang = hang_on_wait
        self.events: list[str] = []

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.events.append("terminate")

    def kill(self) -> None:
        self.events.append("kill")
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.events.append(f"wait:{timeout}")
        if self._hang and "kill" not in self.events:
            raise subprocess.TimeoutExpired("kubectl", timeout or 0)
        self.returncode = self.returncode if self.returncode is not None else -15
        return self.returncode


class TestWaitForLocalPort:
    def test_returns_once_the_port_accepts(self) -> None:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            cluster_ui.wait_for_local_port(port, _FakeProcess(), timeout=5)
            cluster_ui.wait_for_local_port(port, None, timeout=5)

    def test_a_dead_port_forward_is_reported_with_its_stderr(self) -> None:
        process = _FakeProcess(exited=1, stderr=b"error: unable to forward port\n")
        with pytest.raises(RuntimeError, match="exited before localhost:1 was ready: error"):
            cluster_ui.wait_for_local_port(1, process)  # type: ignore[arg-type]

    @pytest.mark.parametrize("stderr", [None, b""])
    def test_a_silent_exit_reports_the_code(self, stderr: bytes | None) -> None:
        process = _FakeProcess(exited=3, stderr=stderr)
        with pytest.raises(RuntimeError, match="exit code 3"):
            cluster_ui.wait_for_local_port(1, process)  # type: ignore[arg-type]

    def test_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(*_a: Any, **_k: Any) -> None:
            raise ConnectionRefusedError

        clock = iter([0.0, 0.1, 99.0])
        sleeps: list[float] = []
        monkeypatch.setattr(cluster_ui.socket, "create_connection", refuse)
        monkeypatch.setattr(cluster_ui.time, "monotonic", lambda: next(clock))
        monkeypatch.setattr(cluster_ui.time, "sleep", sleeps.append)
        with pytest.raises(RuntimeError, match="did not accept connections within 2s"):
            cluster_ui.wait_for_local_port(8080, None, timeout=2)
        assert sleeps == [0.5]


class TestBackgroundPortForward:
    def _run(
        self, monkeypatch: pytest.MonkeyPatch, process: _FakeProcess, *, ready: bool = True
    ) -> list[Any]:
        launched: list[Any] = []

        def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeProcess:
            launched.append((cmd, kwargs))
            return process

        def fake_wait(port: int, proc: Any, *, timeout: float) -> None:
            launched.append(("wait", port, proc is process, timeout))
            if not ready:
                raise RuntimeError("never ready")

        monkeypatch.setattr(cluster_ui.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(cluster_ui, "wait_for_local_port", fake_wait)
        return launched

    def test_yields_when_ready_and_terminates_on_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        process = _FakeProcess()
        launched = self._run(monkeypatch, process)
        with cluster_ui.background_port_forward(("kubectl", "port-forward"), 8080) as proc:
            assert proc is process
        cmd, kwargs = launched[0]
        assert cmd == ["kubectl", "port-forward"]
        assert kwargs == {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
        assert launched[1] == ("wait", 8080, True, cluster_ui.PORT_FORWARD_READY_TIMEOUT_SECONDS)
        assert process.events == ["terminate", "wait:5"]

    def test_kills_a_process_that_ignores_terminate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        process = _FakeProcess(hang_on_wait=True)
        self._run(monkeypatch, process)
        with cluster_ui.background_port_forward(["kubectl"], 8080, timeout=3):
            pass
        assert process.events == ["terminate", "wait:5", "kill", "wait:5"]

    def test_an_exited_process_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        process = _FakeProcess(exited=0)
        self._run(monkeypatch, process)
        with cluster_ui.background_port_forward(["kubectl"], 8080):
            pass
        assert process.events == []

    def test_readiness_failure_still_cleans_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        process = _FakeProcess()
        self._run(monkeypatch, process, ready=False)
        with (
            pytest.raises(RuntimeError, match="never ready"),
            cluster_ui.background_port_forward(["kubectl"], 8080),
        ):
            pytest.fail("body must not run")  # pragma: no cover
        assert process.events == ["terminate", "wait:5"]


# ─── Playwright capture ──────────────────────────────────────────────────────


@dataclass
class _Browser:
    calls: list[tuple[Any, ...]] = field(default_factory=list)


class _FakePage:
    def __init__(self, session: _Browser) -> None:
        self._session = session

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self._session.calls.append(("goto", url, wait_until))

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self._session.calls.append(("wait_for_timeout", timeout_ms))

    def screenshot(self, path: str, full_page: bool = False) -> None:
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        self._session.calls.append(("screenshot", path, full_page))


class _FakeContext:
    def __init__(self, session: _Browser) -> None:
        self._session = session

    def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self._session.calls.append(("add_cookies", cookies))

    def new_page(self) -> _FakePage:
        self._session.calls.append(("new_page",))
        return _FakePage(self._session)


class _FakeBrowserHandle:
    def __init__(self, session: _Browser) -> None:
        self._session = session

    def new_context(self, **kwargs: Any) -> _FakeContext:
        self._session.calls.append(("new_context", kwargs))
        return _FakeContext(self._session)

    def close(self) -> None:
        self._session.calls.append(("close",))


class _FakeChromium:
    def __init__(self, session: _Browser) -> None:
        self._session = session

    def launch(self, headless: bool = True) -> _FakeBrowserHandle:
        self._session.calls.append(("launch", headless))
        return _FakeBrowserHandle(self._session)


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> _Browser:
    session = _Browser()

    @contextmanager
    def sync_playwright() -> Any:
        yield types.SimpleNamespace(chromium=_FakeChromium(session))

    module = types.ModuleType("playwright.sync_api")
    module.__dict__["sync_playwright"] = sync_playwright
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return session


class TestCapturePage:
    def test_captures_a_full_page_with_cookies(self, browser: _Browser, tmp_path: Path) -> None:
        output = tmp_path / "nested" / "shot.png"
        cookie = {"name": "argocd.token", "value": "t", "url": "http://localhost:8080"}
        written = cluster_ui.capture_page(
            "http://localhost:8080/applications",
            output,
            cookies=[cookie],
            headless=False,
            render_wait_ms=10,
        )
        assert written == output and output.read_bytes().startswith(b"\x89PNG")
        assert browser.calls == [
            ("launch", False),
            (
                "new_context",
                {
                    "viewport": {
                        "width": cluster_ui.VIEWPORT_WIDTH,
                        "height": cluster_ui.VIEWPORT_HEIGHT,
                    }
                },
            ),
            ("add_cookies", [cookie]),
            ("new_page",),
            ("goto", "http://localhost:8080/applications", "load"),
            ("wait_for_timeout", 10),
            ("screenshot", str(output), True),
            ("close",),
        ]

    def test_without_cookies_none_are_added(self, browser: _Browser, tmp_path: Path) -> None:
        cluster_ui.capture_page("http://localhost:3001/", tmp_path / "x.png")
        names = [call[0] for call in browser.calls]
        assert "add_cookies" not in names
        assert ("launch", True) in browser.calls
        assert ("wait_for_timeout", cluster_ui.RENDER_WAIT_MS) in browser.calls

    def test_the_browser_closes_when_the_capture_fails(
        self, browser: _Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(self: _FakePage, url: str, wait_until: str | None = None) -> None:
            raise RuntimeError("net::ERR_CONNECTION_REFUSED")

        monkeypatch.setattr(_FakePage, "goto", boom)
        with pytest.raises(RuntimeError, match="ERR_CONNECTION_REFUSED"):
            cluster_ui.capture_page("http://localhost:1/", tmp_path / "x.png")
        assert browser.calls[-1] == ("close",)

    def test_without_playwright_the_install_hint_is_raised(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        with pytest.raises(RuntimeError, match=r"pip install 'gco\[diagrams\]'"):
            cluster_ui.capture_page("http://localhost:1/", tmp_path / "x.png")


# ─── Shared click options ────────────────────────────────────────────────────


def test_tunnel_options_register_the_shared_flags() -> None:
    @click.command()
    @cluster_ui.tunnel_options
    def command(
        region: str | None, via_ssm: str | None, bastion_ttl_minutes: int, assume_yes: bool
    ) -> None:
        click.echo(json.dumps([region, via_ssm, bastion_ttl_minutes, assume_yes]))

    runner = CliRunner()
    defaults = runner.invoke(command, [])
    assert defaults.exit_code == 0, defaults.output
    assert json.loads(defaults.output) == [
        None,
        None,
        cluster_ui.DEFAULT_BASTION_TTL_MINUTES,
        False,
    ]
    given = runner.invoke(
        command, ["-r", "us-west-2", "--via-ssm", "auto", "--bastion-ttl-minutes", "30", "-y"]
    )
    assert json.loads(given.output) == ["us-west-2", "auto", 30, True]


def test_bastion_ttl_default_matches_the_ephemeral_bastion() -> None:
    from cli import ephemeral_bastion

    assert cluster_ui.DEFAULT_BASTION_TTL_MINUTES == ephemeral_bastion.DEFAULT_TTL_MINUTES
