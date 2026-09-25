"""Tests for ``gco crossplane`` — cli/crossplane.py and cli/commands/crossplane_cmd.py.

The status document is pure (cdk.json, charts.yaml and the shipped
post-Helm manifest), so it is checked against temporary files and the real
shipped ones. The dashboard commands run through click's CliRunner with the
EKS tunnel, kubeconfig update, kubectl and Playwright replaced by fakes.
"""

from __future__ import annotations

import json
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cli import cluster_ui, crossplane
from cli.main import cli

_SERVER = "https://127.0.0.1:8443"
_SNI = "abc.gr7.us-east-1.eks.amazonaws.com"


def _write_cdk(tmp_path: Path, helm: Any = None) -> Path:
    context: dict[str, Any] = {"project_name": "gco"}
    if helm is not None:
        context["helm"] = helm
    path = tmp_path / "cdk.json"
    path.write_text(json.dumps({"context": context}))
    return path


class TestToggle:
    @pytest.mark.parametrize(
        ("context", "expected"),
        [
            ({}, False),
            ({"helm": []}, False),
            ({"helm": {}}, False),
            ({"helm": {"crossplane": {}}}, False),
            ({"helm": {"crossplane": {"enabled": False}}}, False),
            ({"helm": {"crossplane": {"enabled": True}}}, True),
        ],
    )
    def test_absent_means_off(self, context: dict[str, Any], expected: bool) -> None:
        assert crossplane.crossplane_enabled(context) is expected

    @pytest.mark.parametrize("block", [True, "on", {"enabled": "true"}, {"enabled": 1}])
    def test_malformed_blocks_are_refused(self, block: Any) -> None:
        with pytest.raises(ValueError, match="boolean enabled"):
            crossplane.crossplane_enabled({"helm": {"crossplane": block}})


class TestStatusDocument:
    def test_shipped_function_is_listed(self) -> None:
        functions = crossplane.shipped_functions()
        assert functions == [
            {
                "name": "crossplane-contrib-function-go-templating",
                "package": functions[0]["package"],
            }
        ]
        assert functions[0]["package"].startswith(
            "xpkg.crossplane.io/crossplane-contrib/function-go-templating:v"
        )

    def test_functions_tolerate_sparse_documents(self, tmp_path: Path) -> None:
        (tmp_path / crossplane.CROSSPLANE_MANIFEST).write_text(
            "---\nkind: Function\n---\nkind: Function\nmetadata: {name: f}\nspec: {package: p}\n"
            "---\nkind: ClusterRole\nmetadata: {name: r}\n"
        )
        assert crossplane.shipped_functions(tmp_path) == [
            {"name": "", "package": ""},
            {"name": "f", "package": "p"},
        ]

    def test_status_over_the_shipped_files(self, tmp_path: Path) -> None:
        status = crossplane.crossplane_status(
            cdk_json_path=_write_cdk(tmp_path, helm={"crossplane": {"enabled": True}})
        )
        assert status["enabled"] is True
        assert [chart["name"] for chart in status["charts"]] == ["crossplane", "crossview"]
        assert [chart["chart"] for chart in status["charts"]] == ["crossplane", "crossview"]
        assert all(chart["namespace"] == "crossplane-system" for chart in status["charts"])
        assert status["namespace"] == "crossplane-system"
        assert status["functions"][0]["name"] == "crossplane-contrib-function-go-templating"
        assert status["dashboard"] == {
            "name": "Crossview",
            "service": "svc/crossview-service",
            "open": "gco crossplane open",
            "auth": "none (reachable only through the kubectl port-forward)",
        }

    def test_missing_pins_are_omitted(self, tmp_path: Path) -> None:
        charts = tmp_path / "charts.yaml"
        charts.write_text("charts:\n  crossplane: {chart: crossplane, version: '9.9.9'}\n")
        status = crossplane.crossplane_status(
            cdk_json_path=_write_cdk(tmp_path),
            charts_yaml=charts,
            manifests_dir=tmp_path,
        )
        assert status["enabled"] is False
        assert [chart["version"] for chart in status["charts"]] == ["9.9.9"]
        assert status["functions"] == []


def test_capture_lands_on_the_dashboard_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def fake_capture(url: str, output: Path, **kwargs: Any) -> Path:
        captured.update(url=url, output=output, **kwargs)
        return output

    monkeypatch.setattr(cluster_ui, "capture_page", fake_capture)
    crossplane.capture_dashboard_screenshot("http://localhost:3001/", tmp_path / "c.png")
    assert captured == {
        "url": "http://localhost:3001/",
        "output": tmp_path / "c.png",
        "headless": True,
    }


def test_port_forward_target() -> None:
    assert crossplane.PORT_FORWARD_TARGET == ("crossplane-system", "svc/crossview-service", 80)
    assert crossplane.DEFAULT_LOCAL_PORT == 3001


# ─── gco crossplane (CliRunner) ──────────────────────────────────────────────


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"tunnels": [], "kubeconfig": [], "session": (_SERVER, _SNI)}

    @contextmanager
    def fake_tunnel(formatter: Any, **kwargs: Any) -> Iterator[Any]:
        state["tunnels"].append(kwargs)
        server, sni = state["session"]
        yield types.SimpleNamespace(server=server, tls_server_name=sni)

    monkeypatch.setattr("cli.cluster_tunnel.open_api_server_tunnel", fake_tunnel)
    monkeypatch.setattr(
        "cli.kubectl_helpers.update_kubeconfig",
        lambda cluster, region: state["kubeconfig"].append((cluster, region)),
    )
    return state


class TestStatusCommand:
    def test_json_status(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_cdk(tmp_path, helm={"crossplane": {"enabled": True}})
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["--output", "json", "crossplane", "status"])
        assert result.exit_code == 0, result.output
        document = json.loads(result.output)
        assert document["enabled"] is True
        assert document["dashboard"]["service"] == "svc/crossview-service"

    def test_disabled_says_how_to_enable(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_cdk(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["crossplane", "status"])
        assert result.exit_code == 0, result.output
        assert "Crossplane is off" in result.output
        assert "--enable crossplane" in result.output

    @pytest.mark.parametrize("helm", [None, {"crossplane": {"enabled": "yes"}}])
    def test_unreadable_config_exits_1(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helm: Any
    ) -> None:
        if helm is not None:
            _write_cdk(tmp_path, helm=helm)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["crossplane", "status"])
        assert result.exit_code == 1
        assert "Failed to read helm.crossplane from cdk.json" in result.output


class TestOpenCommand:
    def test_forwards_the_dashboard_through_the_tunnel(
        self, runner: CliRunner, cluster: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        forwarded: list[list[str]] = []
        monkeypatch.setattr(cluster_ui, "exec_port_forward", forwarded.append)
        result = runner.invoke(
            cli,
            [
                "crossplane",
                "open",
                "-r",
                "us-west-2",
                "--via-ssm",
                "i-0123456789abcdef0",
                "--local-port",
                "13001",
            ],
        )
        assert result.exit_code == 0, result.output
        assert cluster["kubeconfig"] == [("gco-us-west-2", "us-west-2")]
        assert cluster["tunnels"][0]["via_ssm"] == "i-0123456789abcdef0"
        assert cluster["tunnels"][0]["assume_yes"] is False
        assert forwarded == [
            [
                "kubectl",
                "port-forward",
                "-n",
                "crossplane-system",
                "svc/crossview-service",
                "13001:80",
                "--server",
                _SERVER,
                "--tls-server-name",
                _SNI,
            ]
        ]
        assert "http://localhost:13001" in result.output

    def test_default_port(
        self, runner: CliRunner, cluster: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster["session"] = (None, None)
        forwarded: list[list[str]] = []
        monkeypatch.setattr(cluster_ui, "exec_port_forward", forwarded.append)
        result = runner.invoke(cli, ["crossplane", "open", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert forwarded[0][-1] == "3001:80"

    def test_failures_exit_1(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(cluster: str, region: str) -> None:
            raise RuntimeError("no such cluster")

        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", fail)
        result = runner.invoke(cli, ["crossplane", "open", "-r", "us-east-1"])
        assert result.exit_code == 1
        assert "no such cluster" in result.output


class TestScreenshotCommand:
    @pytest.fixture
    def capture(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        state: dict[str, Any] = {"forwards": [], "captures": []}

        @contextmanager
        def fake_forward(cmd: list[str], port: int) -> Iterator[None]:
            state["forwards"].append((cmd, port))
            yield None

        def fake_capture(base_url: str, output: Path, *, headless: bool) -> Path:
            state["captures"].append((base_url, output, headless))
            return output

        monkeypatch.setattr(cluster_ui, "background_port_forward", fake_forward)
        monkeypatch.setattr(crossplane, "capture_dashboard_screenshot", fake_capture)
        return state

    def test_captures_the_dashboard(
        self,
        runner: CliRunner,
        cluster: dict[str, Any],
        capture: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["crossplane", "screenshot", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert capture["captures"] == [
            ("http://localhost:3001", tmp_path / "crossview-dashboard.png", True)
        ]
        (cmd, port) = capture["forwards"][0]
        assert port == 3001 and cmd[3:6] == [
            "crossplane-system",
            "svc/crossview-service",
            "3001:80",
        ]
        assert "crossview-dashboard.png" in result.output

    def test_output_port_and_headed(
        self, runner: CliRunner, cluster: dict[str, Any], capture: dict[str, Any], tmp_path: Path
    ) -> None:
        target = tmp_path / "docs" / "c.png"
        result = runner.invoke(
            cli,
            [
                "crossplane",
                "screenshot",
                "-r",
                "us-east-1",
                "-o",
                str(target),
                "--local-port",
                "13001",
                "--headed",
            ],
        )
        assert result.exit_code == 0, result.output
        assert capture["captures"] == [("http://localhost:13001", target, False)]

    def test_failures_exit_1(
        self,
        runner: CliRunner,
        cluster: dict[str, Any],
        capture: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        def fail(base_url: str, output: Path, *, headless: bool) -> Path:
            raise RuntimeError("Playwright is not installed")

        monkeypatch.setattr(crossplane, "capture_dashboard_screenshot", fail)
        result = runner.invoke(
            cli, ["crossplane", "screenshot", "-r", "us-east-1", "-o", str(tmp_path / "x.png")]
        )
        assert result.exit_code == 1
        assert "Playwright is not installed" in result.output


def test_group_help_lists_every_command(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["crossplane", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("status", "open", "screenshot"):
        assert command in result.output
