"""Secrets the MCP tools hand to the ``gco`` CLI stay off its command line and out of logs.

A child process's argv is visible to every local user through ``ps``. The
tools that take a password, a webhook signing secret, or a webhook URL (which
often embeds a token) therefore pass the value to the CLI through an
environment variable the CLI option also reads:

* ``analytics_login_url`` → ``GCO_STUDIO_PASSWORD``
* ``monitoring_user_add`` → ``GCO_GRAFANA_USER_PASSWORD``
* ``webhooks_create`` → ``GCO_WEBHOOK_URL`` and ``GCO_WEBHOOK_SECRET``

``cli_runner._run_cli(..., env=...)`` adds those variables to the child's
environment only. The audit log already redacts password and secret
arguments; a URL argument is now logged as its scheme and host only.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gco_mcp"))

import audit
import cli_runner
import run_mcp

from cli.main import cli

# ---------------------------------------------------------------------------
# cli_runner._run_cli(env=...)
# ---------------------------------------------------------------------------


def _completed() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout='{"ok": true}', stderr="")


@pytest.mark.parametrize("pass_fds", [(), (7,)], ids=["plain", "pass-fds"])
def test_run_cli_adds_env_to_the_child_only(
    monkeypatch: pytest.MonkeyPatch, pass_fds: tuple[int, ...]
) -> None:
    monkeypatch.setenv("GCO_INHERITED_FOR_TEST", "kept")
    with patch.object(cli_runner.subprocess, "run", return_value=_completed()) as run:
        result = cli_runner._run_cli("status", pass_fds=pass_fds, env={"GCO_X": "s3cret"})
    assert json.loads(result) == {"ok": True}
    child_env = run.call_args.kwargs["env"]
    assert child_env["GCO_X"] == "s3cret"
    assert child_env["GCO_INHERITED_FOR_TEST"] == "kept"
    assert "s3cret" not in run.call_args.args[0]
    assert "GCO_X" not in os.environ  # the server's own environment is untouched


def test_run_cli_without_env_inherits_the_environment() -> None:
    with patch.object(cli_runner.subprocess, "run", return_value=_completed()) as run:
        cli_runner._run_cli("status")
    assert run.call_args.kwargs["env"] is None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.args: tuple[str, ...] = ()
        self.env: dict[str, str] | None = None

    def __call__(self, *args: str, env: dict[str, str] | None = None, **_kw: Any) -> str:
        self.args = args
        self.env = env
        return '{"ok": true}'


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(cli_runner, "_run_cli", rec)
    return rec


def test_analytics_login_password_goes_through_the_environment(recorder: _Recorder) -> None:
    asyncio.run(run_mcp.analytics_login_url(username="alice", password="Pa55/word"))
    assert recorder.args == ("analytics", "studio", "login", "--username", "alice")
    assert recorder.env == {"GCO_STUDIO_PASSWORD": "Pa55/word"}


def test_analytics_login_without_password_adds_nothing(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The child inherits the server's GCO_STUDIO_PASSWORD; without either the
    # tool refuses before running the CLI (tests/test_mcp_child_stdin.py).
    monkeypatch.setenv("GCO_STUDIO_PASSWORD", "from-the-server-env")
    asyncio.run(run_mcp.analytics_login_url(username="alice"))
    assert recorder.args == ("analytics", "studio", "login", "--username", "alice")
    assert recorder.env is None


@pytest.mark.parametrize("email", [None, "alice@example.com"])
def test_monitoring_user_password_goes_through_the_environment(
    recorder: _Recorder, email: str | None
) -> None:
    asyncio.run(run_mcp.monitoring_user_add(username="alice", password="Pa55word", email=email))
    expected = ("monitoring", "users", "add", "--username", "alice")
    if email:
        expected += ("--email", email)
    assert recorder.args == expected
    assert recorder.env == {"GCO_GRAFANA_USER_PASSWORD": "Pa55word"}


@pytest.mark.parametrize(
    ("secret", "expected_env"),
    [
        (None, {"GCO_WEBHOOK_URL": "https://hooks.example.com/services/T0/B0/token"}),
        (
            "hmac-key",
            {
                "GCO_WEBHOOK_URL": "https://hooks.example.com/services/T0/B0/token",
                "GCO_WEBHOOK_SECRET": "hmac-key",
            },
        ),
    ],
    ids=["no-secret", "secret"],
)
def test_webhook_url_and_secret_go_through_the_environment(
    recorder: _Recorder, secret: str | None, expected_env: dict[str, str]
) -> None:
    asyncio.run(
        run_mcp.webhooks_create(
            url="https://hooks.example.com/services/T0/B0/token",
            events=["job.failed"],
            region="us-east-1",
            secret=secret,
        )
    )
    assert recorder.args == ("webhooks", "create", "--event", "job.failed", "-r", "us-east-1")
    assert recorder.env == expected_env


# ---------------------------------------------------------------------------
# CLI options read the same variables
# ---------------------------------------------------------------------------


def test_cli_monitoring_users_add_reads_the_password_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _create(base_url: str, auth: Any, *, login: str, password: str, **_kw: Any) -> int:
        captured["password"] = password
        return 5

    monkeypatch.setattr("cli.monitoring_user_mgmt.create_user", _create)
    result = CliRunner().invoke(
        cli,
        ["monitoring", "users", "add", "--username", "alice", "--admin-password", "adminpw"],
        env={"GCO_GRAFANA_USER_PASSWORD": "from-env"},
    )
    assert result.exit_code == 0, result.output
    assert captured["password"] == "from-env"


def test_cli_monitoring_users_add_names_the_variable_when_no_password() -> None:
    result = CliRunner().invoke(
        cli,
        ["monitoring", "users", "add", "--username", "alice"],
        env={"GCO_GRAFANA_USER_PASSWORD": None},
    )
    assert result.exit_code == 1
    assert "GCO_GRAFANA_USER_PASSWORD" in result.output


def test_cli_webhooks_create_reads_url_and_secret_variables() -> None:
    aws_client = MagicMock()
    aws_client.call_api.return_value = {"id": "hook-1"}
    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        result = CliRunner().invoke(
            cli,
            ["webhooks", "create", "--event", "job.failed", "-r", "us-east-1"],
            env={
                "GCO_WEBHOOK_URL": "https://hooks.example.com/services/T0/B0/token",
                "GCO_WEBHOOK_SECRET": "hmac-key",
            },
        )
    assert result.exit_code == 0, result.output
    body = aws_client.call_api.call_args.kwargs["body"]
    assert body["url"] == "https://hooks.example.com/services/T0/B0/token"
    assert body["secret"] == "hmac-key"


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "logged"),
    [
        ("https://hooks.example.com/services/T0/B0/token", "https://hooks.example.com"),
        ("https://user:pw@example.com:8443/hook?sig=abc#frag", "https://example.com:8443"),
        ("http://[2001:db8::1]:8080/x", "http://[2001:db8::1]:8080"),
        ("https://example.com", "https://example.com"),
        ("not a url", "[REDACTED]"),
        ("https://example.com:notaport/x", "[REDACTED]"),
    ],
)
def test_url_arguments_are_logged_as_scheme_and_host(value: str, logged: str) -> None:
    sanitized = audit._sanitize_arguments({"url": value, "events": ["job.failed"]})
    assert sanitized == {"url": logged, "events": ["job.failed"]}


def test_nested_and_suffixed_url_keys_are_reduced_too() -> None:
    sanitized = audit._sanitize_arguments(
        {
            "callback_url": "https://example.com/cb?token=t",
            "spec": {"endpoint_url": "https://example.com/a/b", "urls": ["kept"]},
            "url": 42,
        }
    )
    assert sanitized == {
        "callback_url": "https://example.com",
        "spec": {"endpoint_url": "https://example.com", "urls": ["kept"]},
        "url": 42,
    }


def test_webhooks_create_audit_entry_carries_no_url_path_or_secret(
    recorder: _Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO", logger=audit.audit_logger.name):
        asyncio.run(
            run_mcp.webhooks_create(
                url="https://hooks.example.com/services/T0/B0/token",
                events=["job.failed"],
                secret="hmac-key",
            )
        )
    entries = [
        json.loads(r.getMessage()) for r in caplog.records if "webhooks_create" in r.getMessage()
    ]
    assert entries, caplog.text
    assert entries[-1]["arguments"]["url"] == "https://hooks.example.com"
    assert entries[-1]["arguments"]["secret"] == "[REDACTED]"
    assert "T0/B0/token" not in caplog.text
    assert "hmac-key" not in caplog.text
