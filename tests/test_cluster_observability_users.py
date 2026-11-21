"""
Tests for Grafana user management (cli/monitoring_user_mgmt.py) and the
`gco monitoring users` subcommands.

``requests`` and ``kubectl`` are mocked so nothing hits a cluster. The HTTP
helpers assert the Grafana admin-API contract (method, path, auth, body); the
CLI tests assert the wiring, the credential-resolution precedence
(--admin-password vs. reading the Secret), and the error paths.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests
import yaml
from click.testing import CliRunner

from cli import monitoring_user_mgmt as mum
from cli.main import cli

BASE = "http://localhost:3000"
AUTH = ("admin", "s3cret")


_CONTEXT = "arn:aws:eks:us-west-2:123456789012:cluster/gco-us-west-2"
_SERVER = "https://127.0.0.1:18443"
_SNI = "abc.gr7.us-west-2.eks.amazonaws.com"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake kubeconfig refresh and tunnel; records what the Secret read asked for."""
    state: dict[str, Any] = {"kubeconfig": [], "tunnels": []}

    @contextmanager
    def fake_tunnel(formatter: Any, **kwargs: Any) -> Iterator[Any]:
        state["tunnels"].append(kwargs)
        formatter.print_info("Route: SSM tunnel via the test instance")
        formatter.print_success("Ephemeral bastion is online.")
        formatter.print_warning("Private endpoint only.")
        yield types.SimpleNamespace(server=_SERVER, tls_server_name=_SNI)

    def fake_update(cluster_name: str, region: str) -> str:
        state["kubeconfig"].append((cluster_name, region))
        return _CONTEXT

    monkeypatch.setattr("cli.cluster_tunnel.open_api_server_tunnel", fake_tunnel)
    monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", fake_update)
    return state


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class TestGrafanaApiHelpers:
    def test_create_user_posts_admin_users(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = MagicMock()
        resp.json.return_value = {"id": 42, "message": "User created"}
        posted: dict[str, object] = {}

        def _post(url, **kw):
            posted["url"] = url
            posted["auth"] = kw.get("auth")
            posted["json"] = kw.get("json")
            return resp

        monkeypatch.setattr(mum.requests, "post", _post)
        uid = mum.create_user(BASE, AUTH, login="alice", password="pw", email="a@example.com")
        assert uid == 42
        assert posted["url"] == f"{BASE}/api/admin/users"
        assert posted["auth"] == AUTH
        assert posted["json"]["login"] == "alice"
        assert posted["json"]["email"] == "a@example.com"
        assert posted["json"]["password"] == "pw"
        resp.raise_for_status.assert_called_once()

    def test_list_users_gets_org_users(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = MagicMock()
        resp.json.return_value = [{"login": "admin"}, {"login": "alice"}]
        monkeypatch.setattr(mum.requests, "get", lambda url, **kw: resp)
        users = mum.list_users(BASE, AUTH)
        assert [u["login"] for u in users] == ["admin", "alice"]

    def test_lookup_user_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = MagicMock()
        resp.json.return_value = {"id": 7, "login": "alice"}
        captured: dict[str, object] = {}

        def _get(url, **kw):
            captured["url"] = url
            captured["params"] = kw.get("params")
            return resp

        monkeypatch.setattr(mum.requests, "get", _get)
        assert mum.lookup_user_id(BASE, AUTH, "alice") == 7
        assert captured["url"] == f"{BASE}/api/users/lookup"
        assert captured["params"] == {"loginOrEmail": "alice"}

    def test_delete_user_deletes_by_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = MagicMock()
        captured: dict[str, object] = {}

        def _delete(url, **kw):
            captured["url"] = url
            return resp

        monkeypatch.setattr(mum.requests, "delete", _delete)
        mum.delete_user(BASE, AUTH, 7)
        assert captured["url"] == f"{BASE}/api/admin/users/7"
        resp.raise_for_status.assert_called_once()

    def test_generate_password_is_strong_unique(self) -> None:
        assert mum.generate_password() != mum.generate_password()
        assert len(mum.generate_password()) >= 24


class TestReadAdminCredentials:
    def test_reads_and_decodes_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {
            "data": {
                "admin-user": base64.b64encode(b"admin").decode(),
                "admin-password": base64.b64encode(b"topsecret").decode(),
            }
        }
        result = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        monkeypatch.setattr(mum.subprocess, "run", lambda *a, **k: result)
        user, password = mum.read_grafana_admin_credentials()
        assert (user, password) == ("admin", "topsecret")

    def test_raises_on_kubectl_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = MagicMock(returncode=1, stdout="", stderr="Forbidden")
        monkeypatch.setattr(mum.subprocess, "run", lambda *a, **k: result)
        with pytest.raises(RuntimeError, match="Failed to read Secret"):
            mum.read_grafana_admin_credentials()

    def test_raises_when_keys_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = MagicMock(returncode=0, stdout=json.dumps({"data": {}}), stderr="")
        monkeypatch.setattr(mum.subprocess, "run", lambda *a, **k: result)
        with pytest.raises(RuntimeError, match="admin-user/admin-password"):
            mum.read_grafana_admin_credentials()

    def test_rejects_bad_namespace(self) -> None:
        with pytest.raises(ValueError, match=re.escape("Invalid namespace 'Bad NS'")):
            mum.read_grafana_admin_credentials(namespace="Bad NS")

    def test_reads_through_the_context_and_tunnel_with_a_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "data": {
                "admin-user": base64.b64encode(b"admin").decode(),
                "admin-password": base64.b64encode(b"topsecret").decode(),
            }
        }
        calls: list[tuple[list[str], dict[str, Any]]] = []

        def run(cmd: list[str], **kwargs: Any) -> MagicMock:
            calls.append((cmd, kwargs))
            return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")

        monkeypatch.setattr(mum.subprocess, "run", run)
        assert mum.read_grafana_admin_credentials(
            server=_SERVER, tls_server_name=_SNI, context=_CONTEXT
        ) == ("admin", "topsecret")
        ((cmd, kwargs),) = calls
        assert cmd == [
            "kubectl",
            f"--context={_CONTEXT}",
            "get",
            "secret",
            "kube-prometheus-stack-grafana",
            "-n",
            "monitoring",
            "-o",
            "json",
            "--server",
            _SERVER,
            "--tls-server-name",
            _SNI,
        ]
        assert kwargs["timeout"] == 60

    def test_an_api_server_that_never_answers_times_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def run(cmd: list[str], **kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setattr(mum.subprocess, "run", run)
        with pytest.raises(RuntimeError, match=r"Timed out after 60s.*pass --via-ssm"):
            mum.read_grafana_admin_credentials()


# ---------------------------------------------------------------------------
# CLI: gco monitoring users
# ---------------------------------------------------------------------------


class TestUsersCli:
    def test_add_with_explicit_password_uses_flag_auth(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def _create(base_url, auth, *, login, password, email=None, name=None):
            captured["base_url"] = base_url
            captured["auth"] = auth
            captured["login"] = login
            return 5

        monkeypatch.setattr("cli.monitoring_user_mgmt.create_user", _create)
        result = runner.invoke(
            cli,
            [
                "monitoring",
                "users",
                "add",
                "--username",
                "alice",
                "--password",
                "pw",
                "--admin-password",
                "adminpw",
            ],
        )
        assert result.exit_code == 0, result.output
        # --admin-password provided => auth resolved from flags, Secret not read.
        assert captured["auth"] == ("admin", "adminpw")
        assert captured["login"] == "alice"

    def test_add_reads_secret_when_no_admin_password(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, cluster: dict[str, Any]
    ) -> None:
        monkeypatch.setattr(
            "cli.monitoring_user_mgmt.read_grafana_admin_credentials",
            lambda *a, **k: ("admin", "from-secret"),
        )
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            "cli.monitoring_user_mgmt.create_user",
            lambda base_url, auth, **kw: captured.setdefault("auth", auth) or 9,
        )
        result = runner.invoke(
            cli,
            ["monitoring", "users", "add", "--username", "bob", "--generate-password"],
        )
        assert result.exit_code == 0, result.output
        assert captured["auth"] == ("admin", "from-secret")
        # generated password is printed exactly once
        assert "Generated password" in result.output

    @pytest.mark.parametrize("output_format", ["json", "yaml"])
    def test_generated_password_survives_machine_output_once(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        output_format: str,
    ) -> None:
        secret = "Grafana!Password1"
        monkeypatch.setattr(
            "cli.monitoring_user_mgmt.generate_password",
            lambda: secret,
        )
        monkeypatch.setattr(
            "cli.monitoring_user_mgmt.create_user",
            lambda *_args, **_kwargs: 42,
        )

        result = runner.invoke(
            cli,
            [
                "--output",
                output_format,
                "monitoring",
                "users",
                "add",
                "--username",
                "bob",
                "--email",
                "bob@example.invalid",
                "--generate-password",
                "--admin-password",
                "adminpw",
            ],
        )

        assert result.exit_code == 0, result.output
        assert result.output.count(secret) == 1
        payload = (
            json.loads(result.stdout) if output_format == "json" else yaml.safe_load(result.stdout)
        )
        assert payload == {
            "created": True,
            "username": "bob",
            "user_id": 42,
            "email": "bob@example.invalid",
            "password_state": "set",
            "password_generated": True,
            "password_source": "generated",
            "password": secret,
        }

    def test_add_rejects_conflicting_password_flags(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = runner.invoke(
            cli,
            [
                "monitoring",
                "users",
                "add",
                "--username",
                "x",
                "--password",
                "p",
                "--generate-password",
                "--admin-password",
                "a",
            ],
        )
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_add_requires_a_password_choice(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = runner.invoke(
            cli,
            ["monitoring", "users", "add", "--username", "x", "--admin-password", "a"],
            env={"GCO_GRAFANA_USER_PASSWORD": None},
        )
        assert result.exit_code == 1
        assert (
            "Pass --password (or set $GCO_GRAFANA_USER_PASSWORD) or --generate-password"
            in result.output
        )

    def test_list_json(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cli.monitoring_user_mgmt.list_users",
            lambda base_url, auth: [{"login": "admin"}],
        )
        result = runner.invoke(
            cli,
            ["monitoring", "users", "list", "--admin-password", "a", "--as-json"],
        )
        assert result.exit_code == 0, result.output
        assert '"login": "admin"' in result.output

    def test_remove_looks_up_then_deletes(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cli.monitoring_user_mgmt.lookup_user_id", lambda b, a, u: 11)
        deleted: dict[str, object] = {}
        monkeypatch.setattr(
            "cli.monitoring_user_mgmt.delete_user",
            lambda b, a, uid: deleted.setdefault("uid", uid),
        )
        result = runner.invoke(
            cli,
            [
                "monitoring",
                "users",
                "remove",
                "--username",
                "alice",
                "--admin-password",
                "a",
                "--yes",
            ],
        )
        assert result.exit_code == 0, result.output
        assert deleted["uid"] == 11

    def test_add_surfaces_http_error(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*a: object, **k: object) -> int:
            raise requests.HTTPError("409 user exists")

        monkeypatch.setattr("cli.monitoring_user_mgmt.create_user", _boom)
        result = runner.invoke(
            cli,
            [
                "monitoring",
                "users",
                "add",
                "--username",
                "dup",
                "--password",
                "p",
                "--admin-password",
                "a",
            ],
        )
        assert result.exit_code == 1
        assert "Failed to create Grafana user" in result.output

    def test_list_surfaces_secret_read_error(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, cluster: dict[str, Any]
    ) -> None:
        # No --admin-password => reads Secret; make that fail.
        def _boom(*a: object, **k: object) -> tuple[str, str]:
            raise RuntimeError("kubectl not found")

        monkeypatch.setattr("cli.monitoring_user_mgmt.read_grafana_admin_credentials", _boom)
        result = runner.invoke(cli, ["monitoring", "users", "list"])
        assert result.exit_code == 1
        assert "Failed to list Grafana users" in result.output

    def test_list_reads_the_secret_on_the_regions_cluster_through_a_tunnel(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, cluster: dict[str, Any]
    ) -> None:
        """The documented laptop flow, `monitoring open --via-ssm` in one
        terminal and `monitoring users list` in another, needs the Secret read
        to reach the private endpoint itself, on the right cluster."""
        reads: list[dict[str, Any]] = []

        def read(**kwargs: Any) -> tuple[str, str]:
            reads.append(kwargs)
            return ("admin", "from-secret")

        monkeypatch.setattr("cli.monitoring_user_mgmt.read_grafana_admin_credentials", read)
        monkeypatch.setattr(
            "cli.monitoring_user_mgmt.list_users", lambda base_url, auth: [{"login": auth[0]}]
        )
        result = runner.invoke(
            cli,
            [
                "monitoring",
                "users",
                "list",
                "-r",
                "us-west-2",
                "--via-ssm",
                "auto",
                "-y",
                "--as-json",
            ],
        )
        assert result.exit_code == 0, result.output
        assert cluster["kubeconfig"] == [("gco-us-west-2", "us-west-2")]
        (tunnel,) = cluster["tunnels"]
        assert tunnel["cluster"] == "gco-us-west-2" and tunnel["region"] == "us-west-2"
        assert tunnel["via_ssm"] == "auto" and tunnel["assume_yes"] is True
        # Its own tunnel: `monitoring open` holds the default API port.
        assert tunnel["local_port"] != 8443
        assert reads == [{"server": _SERVER, "tls_server_name": _SNI, "context": _CONTEXT}]
        # The route line goes to stderr; stdout stays the JSON document.
        assert json.loads(result.stdout) == [{"login": "admin"}]
        assert "Route: SSM tunnel" in result.stderr
        assert "Ephemeral bastion is online." in result.stderr
        assert "Private endpoint only." in result.stderr

    def test_remove_yes_also_skips_the_bastion_prompt(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, cluster: dict[str, Any]
    ) -> None:
        monkeypatch.setattr(
            "cli.monitoring_user_mgmt.read_grafana_admin_credentials",
            lambda **_k: ("admin", "from-secret"),
        )
        monkeypatch.setattr("cli.monitoring_user_mgmt.lookup_user_id", lambda b, a, u: 3)
        monkeypatch.setattr("cli.monitoring_user_mgmt.delete_user", lambda b, a, uid: None)
        result = runner.invoke(
            cli,
            ["monitoring", "users", "remove", "--username", "a", "--via-ssm", "auto", "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert cluster["tunnels"][0]["assume_yes"] is True


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_grafana_explicit_password_machine_schema_omits_secret(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(
        "cli.monitoring_user_mgmt.create_user",
        lambda *_args, **_kwargs: 42,
    )

    result = runner.invoke(
        cli,
        [
            "--output",
            output_format,
            "monitoring",
            "users",
            "add",
            "--username",
            "bob",
            "--password",
            "Provided!Password1",
            "--admin-password",
            "adminpw",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = (
        json.loads(result.stdout) if output_format == "json" else yaml.safe_load(result.stdout)
    )
    assert payload["password_generated"] is False
    assert payload["password_source"] == "provided"
    assert "password" not in payload
    assert "Provided!Password1" not in result.output
