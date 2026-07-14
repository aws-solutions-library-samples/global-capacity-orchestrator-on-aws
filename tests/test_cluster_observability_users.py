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
from unittest.mock import MagicMock

import pytest
import requests
from click.testing import CliRunner

from cli import monitoring_user_mgmt as mum
from cli.main import cli

BASE = "http://localhost:3000"
AUTH = ("admin", "s3cret")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


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
        with pytest.raises(ValueError):
            mum.read_grafana_admin_credentials(namespace="Bad NS")


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
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
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
        )
        assert result.exit_code == 1
        assert "Pass --password or --generate-password" in result.output

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
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No --admin-password => reads Secret; make that fail.
        def _boom(*a: object, **k: object) -> tuple[str, str]:
            raise RuntimeError("kubectl not found")

        monkeypatch.setattr("cli.monitoring_user_mgmt.read_grafana_admin_credentials", _boom)
        result = runner.invoke(cli, ["monitoring", "users", "list"])
        assert result.exit_code == 1
        assert "Failed to list Grafana users" in result.output
