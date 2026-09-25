"""Tests for ``gco gitops`` — cli/gitops.py and cli/commands/gitops_cmd.py.

The status document is pure (cdk.json + charts.yaml), so it is checked against
temporary files. The cluster-facing commands run through click's CliRunner
with the EKS tunnel, kubeconfig update, kubectl and Playwright replaced by
fakes: what matters is the exact port-forward argv (namespace, target, ports,
the tunnel's --server / --tls-server-name), that the admin password comes
from the right Secret, and that every failure exits 1 with a message.
"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cli import cluster_ui, gitops
from cli.main import cli
from gco.argocd_config import ArgoCdConfigError

_SERVER = "https://127.0.0.1:8443"
_SNI = "abc.gr7.us-east-1.eks.amazonaws.com"


def _write_cdk(tmp_path: Path, helm: dict[str, Any] | None = None, **context: Any) -> Path:
    document = {
        "app": "python3 app.py",
        "context": {
            "project_name": "gco",
            "deployment_regions": {"regional": ["us-east-1", "eu-west-1"]},
            **({"helm": helm} if helm is not None else {}),
            **context,
        },
    }
    path = tmp_path / "cdk.json"
    path.write_text(json.dumps(document))
    return path


_GITOPS = {
    "enabled": True,
    "source_repos": ["https://github.com/example/*"],
    "gitops": {
        "repo_url": "https://github.com/example/tenants.git",
        "revision": "main",
        "path": "clusters/{cluster_name}",
        "sync_policy": "automated",
    },
}


# ─── cli/gitops.py ───────────────────────────────────────────────────────────


class TestStatusDocument:
    def test_load_argocd_config_validates_the_block(self, tmp_path: Path) -> None:
        path = _write_cdk(tmp_path, helm={"argocd": _GITOPS})
        assert gitops.load_argocd_config(path)["gitops"]["sync_policy"] == "automated"

    def test_absent_helm_block_is_the_default(self, tmp_path: Path) -> None:
        assert gitops.load_argocd_config(_write_cdk(tmp_path))["enabled"] is False
        assert gitops.load_argocd_config(_write_cdk(tmp_path, helm=[]))["enabled"] is False  # type: ignore[arg-type]

    def test_malformed_block_raises_the_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ArgoCdConfigError, match="unknown key"):
            gitops.load_argocd_config(_write_cdk(tmp_path, helm={"argocd": {"idc": 1}}))

    def test_gitops_paths_render_per_region(self, tmp_path: Path) -> None:
        charts = tmp_path / "charts.yaml"
        charts.write_text(
            "charts:\n  argocd:\n    chart: argo-cd\n    version: '1.2.3'\n    namespace: argocd\n"
        )
        status = gitops.gitops_status(
            regions=["us-east-1", "eu-west-1"],
            project_name="acme",
            cdk_json_path=_write_cdk(tmp_path, helm={"argocd": _GITOPS}),
            charts_yaml=charts,
        )
        assert status["enabled"] is True
        assert status["chart"]["version"] == "1.2.3"
        assert status["namespace"] == "argocd"
        assert status["project"] == "gco-tenants"
        assert status["destinations"] == ["gco-jobs", "gco-inference"]
        assert status["source_repos"] == ["https://github.com/example/*"]
        assert status["gitops"] == {
            "enabled": True,
            "application": "gco-gitops-root",
            "repo_url": "https://github.com/example/tenants.git",
            "revision": "main",
            "path": "clusters/{cluster_name}",
            "paths": {
                "us-east-1": "clusters/acme-us-east-1",
                "eu-west-1": "clusters/acme-eu-west-1",
            },
            "default_namespace": "gco-jobs",
            "sync_policy": "automated",
        }
        assert status["repo_server"] == {
            "deployment": "argocd-repo-server",
            "replicas": 1,
            "autoscaling": {"enabled": False},
        }
        assert status["ui"] == {
            "service": "svc/argocd-server",
            "open": "gco gitops open",
            "username": "admin",
            "password": "gco gitops password",
        }

    def test_disabled_and_no_chart(self) -> None:
        status = gitops.build_status(
            gitops.validate_argocd_config(None), regions=["us-east-1"], project_name="gco"
        )
        assert status["enabled"] is False
        assert status["chart"] is None
        assert status["gitops"] == {"enabled": False}
        assert status["source_repos"] == ["*"]

    def test_repo_server_autoscaler_is_reported(self) -> None:
        config = gitops.validate_argocd_config(
            {
                "enabled": True,
                "repo_server": {
                    "replicas": 2,
                    "autoscaling": {"enabled": True, "max_replicas": 8},
                },
            }
        )
        status = gitops.build_status(config, regions=["us-east-1"], project_name="gco")
        assert status["repo_server"] == {
            "deployment": "argocd-repo-server",
            "replicas": 2,
            "autoscaling": {
                "enabled": True,
                "min_replicas": 2,
                "max_replicas": 8,
                "cpu_target_utilization_percentage": 70,
            },
        }


class TestReadAdminPassword:
    def test_reads_the_initial_admin_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[Any, ...]] = []

        def fake_read(namespace: str, name: str, key: str, **kwargs: Any) -> str:
            calls.append((namespace, name, key, kwargs))
            return "generated"

        monkeypatch.setattr(cluster_ui, "read_secret_key", fake_read)
        assert gitops.read_admin_password(server=_SERVER, tls_server_name=_SNI) == "generated"
        assert calls == [
            (
                "argocd",
                "argocd-initial-admin-secret",
                "password",
                {"server": _SERVER, "tls_server_name": _SNI},
            )
        ]

    def test_a_deleted_secret_explains_the_rotation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_read(*_a: Any, **_k: Any) -> str:
            raise RuntimeError(
                "Failed to read Secret argocd/argocd-initial-admin-secret: "
                'Error from server (NotFound): secrets "argocd-initial-admin-secret" not found'
            )

        monkeypatch.setattr(cluster_ui, "read_secret_key", fake_read)
        with pytest.raises(RuntimeError, match="initial admin password was rotated"):
            gitops.read_admin_password()

    def test_other_failures_pass_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_read(*_a: Any, **_k: Any) -> str:
            raise RuntimeError("kubectl not found. Install kubectl and ensure it's on your PATH.")

        monkeypatch.setattr(cluster_ui, "read_secret_key", fake_read)
        with pytest.raises(RuntimeError, match="kubectl not found"):
            gitops.read_admin_password()


class _Response:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class TestSessionToken:
    @pytest.fixture
    def post(self, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        import requests

        calls: list[Any] = []
        responses: list[Any] = []

        def fake_post(url: str, **kwargs: Any) -> Any:
            calls.append((url, kwargs))
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(requests, "post", fake_post)
        calls.append(responses)
        return calls

    def test_logs_in_as_admin(self, post: list[Any]) -> None:
        post[0].append(_Response(200, {"token": "jwt"}))
        assert gitops.create_session_token("http://localhost:8080/", "pw", timeout=5) == "jwt"
        assert post[1] == (
            "http://localhost:8080/api/v1/session",
            {"json": {"username": "admin", "password": "pw"}, "timeout": 5},
        )

    def test_unreachable_api(self, post: list[Any]) -> None:
        import requests

        post[0].append(requests.ConnectionError("refused"))
        with pytest.raises(RuntimeError, match="Could not reach the Argo CD API"):
            gitops.create_session_token("http://localhost:8080", "pw")

    def test_refused_login(self, post: list[Any]) -> None:
        post[0].append(_Response(401, {"error": "Invalid username or password"}))
        with pytest.raises(RuntimeError, match=r"refused the admin login \(401\).*--password"):
            gitops.create_session_token("http://localhost:8080", "wrong")

    @pytest.mark.parametrize("payload", [None, {}, {"token": ""}, {"token": 3}])
    def test_no_token(self, post: list[Any], payload: Any) -> None:
        post[0].append(_Response(200, payload))
        with pytest.raises(RuntimeError, match="returned no session token"):
            gitops.create_session_token("http://localhost:8080", "pw")


def test_capture_rides_the_session_cookie(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_capture(url: str, output: Path, **kwargs: Any) -> Path:
        captured.update(url=url, output=output, **kwargs)
        return output

    monkeypatch.setattr(cluster_ui, "capture_page", fake_capture)
    out = gitops.capture_argocd_screenshot(
        "http://localhost:8080/", tmp_path / "a.png", token="jwt", headless=False
    )
    assert out == tmp_path / "a.png"
    assert captured == {
        "url": "http://localhost:8080/applications",
        "output": tmp_path / "a.png",
        "cookies": [{"name": "argocd.token", "value": "jwt", "url": "http://localhost:8080"}],
        "headless": False,
    }


def test_port_forward_target() -> None:
    assert gitops.PORT_FORWARD_TARGET == ("argocd", "svc/argocd-server", 80)
    assert gitops.DEFAULT_LOCAL_PORT == 8080


def test_cli_modules_import_without_aws_cdk() -> None:
    """``gco gitops`` / ``gco crossplane`` read cdk.json through CDK-free modules."""
    import subprocess

    snippet = (
        "import sys\n"
        "import cli.gitops, cli.crossplane, cli.cluster_ui, gco.argocd_config\n"
        "import cli.commands.gitops_cmd, cli.commands.crossplane_cmd\n"
        "assert 'aws_cdk' not in sys.modules, 'aws_cdk was imported'\n"
        "assert 'gco.config' not in sys.modules, 'gco.config was imported'\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


# ─── gco gitops (CliRunner) ──────────────────────────────────────────────────


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake tunnel + kubeconfig; records what the command asked for."""
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
        _write_cdk(tmp_path, helm={"argocd": _GITOPS})
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["--output", "json", "gitops", "status"])
        assert result.exit_code == 0, result.output
        document = json.loads(result.output)
        assert document["enabled"] is True
        assert document["chart"]["chart"] == "argo-cd"
        assert set(document["gitops"]["paths"]) == {"us-east-1", "eu-west-1"}

    def test_region_flag_renders_one_path(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_cdk(tmp_path, helm={"argocd": _GITOPS})
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["--output", "json", "gitops", "status", "-r", "ap-south-1"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["gitops"]["paths"] == {
            "ap-south-1": "clusters/gco-ap-south-1"
        }

    def test_disabled_argocd_says_how_to_enable(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_cdk(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["gitops", "status"])
        assert result.exit_code == 0, result.output
        assert "Argo CD is off" in result.output
        assert "--enable argocd" in result.output

    @pytest.mark.parametrize("helm", [None, {"argocd": {"enabled": "yes"}}])
    def test_unreadable_config_exits_1(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        helm: dict[str, Any] | None,
    ) -> None:
        if helm is not None:
            _write_cdk(tmp_path, helm=helm)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["gitops", "status"])
        assert result.exit_code == 1
        assert "Failed to read helm.argocd from cdk.json" in result.output


class TestOpenCommand:
    def test_forwards_the_ui_through_the_tunnel(
        self, runner: CliRunner, cluster: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        forwarded: list[list[str]] = []
        monkeypatch.setattr(cluster_ui, "exec_port_forward", forwarded.append)
        result = runner.invoke(
            cli,
            [
                "gitops",
                "open",
                "-r",
                "us-west-2",
                "--via-ssm",
                "auto",
                "-y",
                "--local-port",
                "18080",
            ],
        )
        assert result.exit_code == 0, result.output
        assert cluster["kubeconfig"] == [("gco-us-west-2", "us-west-2")]
        assert cluster["tunnels"] == [
            {
                "cluster": "gco-us-west-2",
                "region": "us-west-2",
                "via_ssm": "auto",
                "bastion_ttl_minutes": cluster_ui.DEFAULT_BASTION_TTL_MINUTES,
                "assume_yes": True,
            }
        ]
        assert forwarded == [
            [
                "kubectl",
                "port-forward",
                "-n",
                "argocd",
                "svc/argocd-server",
                "18080:80",
                "--server",
                _SERVER,
                "--tls-server-name",
                _SNI,
            ]
        ]
        assert "http://localhost:18080" in result.output
        assert "gco gitops password" in result.output

    def test_default_port_and_direct_endpoint(
        self, runner: CliRunner, cluster: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster["session"] = (None, None)
        forwarded: list[list[str]] = []
        monkeypatch.setattr(cluster_ui, "exec_port_forward", forwarded.append)
        result = runner.invoke(cli, ["gitops", "open", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert forwarded[0][-1] == "8080:80"
        assert "--server" not in forwarded[0]

    def test_failures_exit_1(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(cluster: str, region: str) -> None:
            raise RuntimeError("aws eks update-kubeconfig failed")

        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", fail)
        result = runner.invoke(cli, ["gitops", "open", "-r", "us-east-1"])
        assert result.exit_code == 1
        assert "update-kubeconfig failed" in result.output


class TestPasswordCommand:
    def test_prints_the_generated_password(
        self, runner: CliRunner, cluster: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, Any]] = []

        def fake_read(**kwargs: Any) -> str:
            seen.append(kwargs)
            return "generated-pw"

        monkeypatch.setattr(gitops, "read_admin_password", fake_read)
        result = runner.invoke(cli, ["--output", "json", "gitops", "password", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {
            "region": "us-east-1",
            "username": "admin",
            "password": "generated-pw",
        }
        assert seen == [{"server": _SERVER, "tls_server_name": _SNI}]

    def test_failures_exit_1(
        self, runner: CliRunner, cluster: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_read(**_kwargs: Any) -> str:
            raise RuntimeError("argocd/argocd-initial-admin-secret is gone")

        monkeypatch.setattr(gitops, "read_admin_password", fake_read)
        result = runner.invoke(cli, ["gitops", "password", "-r", "us-east-1"])
        assert result.exit_code == 1
        assert "is gone" in result.output


class TestScreenshotCommand:
    @pytest.fixture
    def capture(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        state: dict[str, Any] = {"forwards": [], "tokens": [], "captures": []}

        @contextmanager
        def fake_forward(cmd: list[str], port: int) -> Iterator[None]:
            state["forwards"].append((cmd, port))
            yield None

        def fake_token(base_url: str, password: str) -> str:
            state["tokens"].append((base_url, password))
            return "jwt"

        def fake_capture(base_url: str, output: Path, *, token: str, headless: bool) -> Path:
            state["captures"].append((base_url, output, token, headless))
            return output

        monkeypatch.setattr(cluster_ui, "background_port_forward", fake_forward)
        monkeypatch.setattr(gitops, "create_session_token", fake_token)
        monkeypatch.setattr(gitops, "capture_argocd_screenshot", fake_capture)
        monkeypatch.setattr(gitops, "read_admin_password", lambda **_k: "from-secret")
        return state

    def test_captures_with_the_secret_password(
        self,
        runner: CliRunner,
        cluster: dict[str, Any],
        capture: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCO_ARGOCD_ADMIN_PASSWORD", raising=False)
        result = runner.invoke(cli, ["--output", "json", "gitops", "screenshot", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert capture["tokens"] == [("http://localhost:8080", "from-secret")]
        (base_url, output, token, headless) = capture["captures"][0]
        assert (base_url, token, headless) == ("http://localhost:8080", "jwt", True)
        assert output == tmp_path / "argocd-ui.png"
        (cmd, port) = capture["forwards"][0]
        assert port == 8080 and cmd[3:6] == ["argocd", "svc/argocd-server", "8080:80"]
        assert str(output) in result.output

    def test_explicit_password_output_port_and_headed(
        self,
        runner: CliRunner,
        cluster: dict[str, Any],
        capture: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "docs" / "argocd.png"
        result = runner.invoke(
            cli,
            [
                "gitops",
                "screenshot",
                "-r",
                "us-east-1",
                "--password",
                "given",
                "--output",
                str(target),
                "--local-port",
                "18080",
                "--headed",
            ],
        )
        assert result.exit_code == 0, result.output
        assert capture["tokens"] == [("http://localhost:18080", "given")]
        assert capture["captures"] == [("http://localhost:18080", target, "jwt", False)]

    def test_password_from_the_environment(
        self,
        runner: CliRunner,
        cluster: dict[str, Any],
        capture: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            cli,
            ["gitops", "screenshot", "-r", "us-east-1", "-o", str(tmp_path / "x.png")],
            env={"GCO_ARGOCD_ADMIN_PASSWORD": "from-env"},
        )
        assert result.exit_code == 0, result.output
        assert capture["tokens"] == [("http://localhost:8080", "from-env")]

    def test_failures_exit_1(
        self,
        runner: CliRunner,
        cluster: dict[str, Any],
        capture: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        def refuse(base_url: str, password: str) -> str:
            raise RuntimeError("Argo CD refused the admin login (401)")

        monkeypatch.setattr(gitops, "create_session_token", refuse)
        result = runner.invoke(
            cli,
            ["gitops", "screenshot", "-r", "us-east-1", "-o", str(tmp_path / "x.png")],
            env={"GCO_ARGOCD_ADMIN_PASSWORD": "x"},
        )
        assert result.exit_code == 1
        assert "refused the admin login" in result.output
        assert capture["captures"] == []


def test_group_help_lists_every_command(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["gitops", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("status", "open", "password", "screenshot"):
        assert command in result.output
