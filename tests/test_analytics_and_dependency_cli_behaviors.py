"""Behavior tests for analytics workflows and dependency-scan helpers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import click
import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner

from cli.commands import analytics_cmd, deps_cmd
from cli.commands.analytics_cmd import analytics
from cli.commands.deps_cmd import deps
from cli.config import GCOConfig


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _config(*, output_format: str = "table") -> GCOConfig:
    return GCOConfig(
        project_name="test-gco",
        default_region="us-east-1",
        global_region="us-east-1",
        api_gateway_region="us-east-1",
        output_format=output_format,
    )


def _invoke_analytics(
    runner: CliRunner,
    args: list[str],
    *,
    input_text: str | None = None,
):
    kwargs: dict[str, object] = {"obj": _config()}
    if input_text is not None:
        kwargs["input"] = input_text
    return runner.invoke(analytics, args, **kwargs)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Operation")


def test_analytics_enable_canvas_confirmation_describes_only_selected_toggle(
    runner: CliRunner,
) -> None:
    update = Mock()
    with (
        patch(
            "cli.stacks.get_analytics_config",
            return_value={"hyperpod": {"instance_type": "ml.p5.48xlarge"}, "canvas": {}},
        ),
        patch("cli.stacks.update_analytics_config", update),
    ):
        result = _invoke_analytics(runner, ["enable", "--canvas"], input_text="y\n")

    assert result.exit_code == 0, result.output
    assert "Canvas sub-toggle will also be enabled" in result.output
    assert "Hyperpod sub-toggle" not in result.output
    update.assert_called_once_with(
        {
            "enabled": True,
            "hyperpod": {"instance_type": "ml.p5.48xlarge", "enabled": False},
            "canvas": {"enabled": True},
        }
    )


def test_analytics_user_creation_reports_permanent_password_failure(runner: CliRunner) -> None:
    with (
        patch.object(analytics_cmd, "_require_cognito_pool_id", return_value=("pool", "us-east-1")),
        patch("cli.analytics_user_mgmt.admin_create_user", return_value=("user", "Temporary!1")),
        patch(
            "cli.analytics_user_mgmt.admin_set_user_password",
            side_effect=_client_error("InvalidPasswordException"),
        ),
    ):
        result = _invoke_analytics(
            runner,
            ["users", "add", "--username", "alice", "--password", "Permanent!1"],
        )

    assert result.exit_code == 1
    assert "created, but setting the password failed: InvalidPasswordException" in result.output
    assert "admin-set-user-password --permanent" in result.output


def test_analytics_user_creation_prints_temporary_password_once(runner: CliRunner) -> None:
    with (
        patch.object(analytics_cmd, "_require_cognito_pool_id", return_value=("pool", "us-east-1")),
        patch("cli.analytics_user_mgmt.admin_create_user", return_value=("user", "Temporary!1")),
    ):
        result = _invoke_analytics(runner, ["users", "add", "--username", "alice"])

    assert result.exit_code == 0, result.output
    assert result.output.count("Temporary!1") == 1
    assert "Temporary password (printed exactly once)" in result.output


@pytest.mark.parametrize(
    ("temporary", "qualifier", "permanent"),
    [(False, "permanent", True), (True, "temporary", False)],
)
def test_set_password_prompts_and_confirms_selected_lifetime(
    runner: CliRunner,
    temporary: bool,
    qualifier: str,
    permanent: bool,
) -> None:
    set_password = Mock()
    args = ["users", "set-password", "--username", "alice"]
    if temporary:
        args.append("--temporary")

    with (
        patch.object(analytics_cmd, "_require_cognito_pool_id", return_value=("pool", "us-east-1")),
        patch.object(analytics_cmd.click, "prompt", return_value="Prompted!1") as prompt,
        patch.object(analytics_cmd.click, "confirm", return_value=True) as confirm,
        patch("cli.analytics_user_mgmt.admin_set_user_password", set_password),
    ):
        result = _invoke_analytics(runner, args)

    assert result.exit_code == 0, result.output
    prompt.assert_called_once_with("New password", hide_input=True, confirmation_prompt=True)
    assert qualifier in confirm.call_args.args[0]
    set_password.assert_called_once_with(
        pool_id="pool",
        region="us-east-1",
        username="alice",
        password="Prompted!1",
        permanent=permanent,
    )
    assert f"Password set ({qualifier})" in result.output


@pytest.mark.parametrize(("pool_id", "client_id"), [(None, "client"), ("pool", None)])
def test_studio_login_requires_both_cognito_identifiers(
    runner: CliRunner,
    pool_id: str | None,
    client_id: str | None,
) -> None:
    with (
        patch("cli.analytics_user_mgmt.discover_cognito_pool_id", return_value=pool_id),
        patch("cli.analytics_user_mgmt.discover_cognito_client_id", return_value=client_id),
        patch("cli.analytics_user_mgmt.discover_api_endpoint") as endpoint,
    ):
        result = _invoke_analytics(
            runner,
            ["studio", "login", "--username", "alice", "--password", "Secret!1"],
        )

    assert result.exit_code == 1
    assert "analytics stack" in result.output.lower()
    endpoint.assert_not_called()


def _studio_patches(fetch: Mock):
    return (
        patch("cli.analytics_user_mgmt.discover_cognito_pool_id", return_value="pool"),
        patch("cli.analytics_user_mgmt.discover_cognito_client_id", return_value="client"),
        patch("cli.analytics_user_mgmt.discover_api_endpoint", return_value="https://api.example"),
        patch("cli.analytics_user_mgmt.srp_authenticate", return_value={"IdToken": "token"}),
        patch("cli.analytics_user_mgmt.fetch_studio_url", fetch),
        patch("time.sleep"),
    )


def test_studio_login_polls_until_profile_is_ready(runner: CliRunner) -> None:
    fetch = Mock(
        side_effect=[
            ("", 0, "request-1"),
            ("", 0, "request-2"),
            ("https://studio.example/session", 900, "request-3"),
        ]
    )

    with (
        _studio_patches(fetch)[0],
        _studio_patches(fetch)[1],
        _studio_patches(fetch)[2],
        _studio_patches(fetch)[3],
        _studio_patches(fetch)[4],
        _studio_patches(fetch)[5],
    ):
        result = _invoke_analytics(
            runner,
            ["studio", "login", "--username", "alice", "--password", "Secret!1"],
        )

    assert result.exit_code == 0, result.output
    assert "Waiting for user profile to provision..." in result.output
    assert ".. ready" in result.output
    assert "https://studio.example/session" in result.output
    assert fetch.call_count == 3


def test_studio_login_times_out_after_bounded_polling(runner: CliRunner) -> None:
    fetch = Mock(return_value=("", 0, "request"))
    patches = _studio_patches(fetch)

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = _invoke_analytics(
            runner,
            ["studio", "login", "--username", "alice", "--password", "Secret!1"],
        )

    assert result.exit_code == 2
    assert "did not become ready within 120s" in result.output
    assert fetch.call_count == 24


def test_studio_login_can_open_the_ready_url(runner: CliRunner) -> None:
    fetch = Mock(return_value=("https://studio.example/session", 900, "request"))
    patches = _studio_patches(fetch)

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch.object(analytics_cmd.click, "launch") as launch,
    ):
        result = _invoke_analytics(
            runner,
            [
                "studio",
                "login",
                "--username",
                "alice",
                "--password",
                "Secret!1",
                "--open",
            ],
        )

    assert result.exit_code == 0, result.output
    launch.assert_called_once_with("https://studio.example/session")


def test_analytics_doctor_reports_all_failed_checks_and_remediation(runner: CliRunner) -> None:
    with (
        patch("cli.stacks._find_cdk_json", return_value=None),
        patch("cli.config._load_cdk_json", return_value={"regional": []}),
        patch(
            "cli.analytics_user_mgmt.check_stack_complete",
            return_value=(False, "deploy prerequisite"),
        ),
        patch(
            "cli.analytics_user_mgmt.check_ssm_parameter",
            side_effect=[
                (False, ""),
                (False, "parameter missing"),
                (False, "parameter missing"),
            ],
        ),
        patch(
            "cli.analytics_user_mgmt.scan_orphan_analytics_resources",
            return_value=["delete orphan"],
        ),
    ):
        result = _invoke_analytics(runner, ["doctor"])

    assert result.exit_code == 1
    assert "cdk.json present" in result.output
    assert "deploy prerequisite" in result.output
    assert "deploy test-gco-global first" in result.output
    assert "delete orphan" in result.output
    assert "Doctor checks failed" in result.output


def test_dependency_repo_root_uses_git_checkout_when_scanner_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deps_cmd.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="/repo\n"),
    )
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda self: str(self).endswith(str(deps_cmd._SCAN_SCRIPT)),
    )

    assert deps_cmd._repo_root() == Path("/repo")


def test_dependency_repo_root_rejects_checkout_without_scanner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deps_cmd.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="/repo\n"),
    )
    monkeypatch.setattr(Path, "is_file", lambda _self: False)

    with pytest.raises(click.ClickException, match="not a GCO checkout"):
        deps_cmd._repo_root()


def test_parse_github_output_ignores_malformed_rows(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.write_text(" good = value \nno-separator\nempty=\n", encoding="utf-8")

    assert deps_cmd._parse_github_output(output) == {"good": "value", "empty": ""}


def test_parse_github_output_degrades_on_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "output"
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=OSError("denied")))

    assert deps_cmd._parse_github_output(output) == {}


@pytest.mark.parametrize(
    ("which", "returncode", "expected"),
    [(None, 0, False), ("/usr/bin/aws", 0, True), ("/usr/bin/aws", 1, False)],
)
def test_sts_identity_probe_reports_cli_and_identity_availability(
    monkeypatch: pytest.MonkeyPatch,
    which: str | None,
    returncode: int,
    expected: bool,
) -> None:
    run = Mock(return_value=SimpleNamespace(returncode=returncode))
    monkeypatch.setattr(deps_cmd.shutil, "which", lambda _name: which)
    monkeypatch.setattr(deps_cmd.subprocess, "run", run)

    assert deps_cmd._sts_identity_available() is expected
    if which is None:
        run.assert_not_called()
    else:
        run.assert_called_once_with(
            ["aws", "sts", "get-caller-identity"],
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.mark.parametrize("stderr", ["catalog failed", ""])
def test_nodepool_online_operational_failure_is_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
) -> None:
    runs = iter(
        [
            SimpleNamespace(returncode=0, stdout="# Offline\n", stderr=""),
            SimpleNamespace(returncode=2, stdout="", stderr=stderr),
        ]
    )
    monkeypatch.setattr(deps_cmd, "_sts_identity_available", lambda: True)
    monkeypatch.setattr(deps_cmd.subprocess, "run", lambda *_args, **_kwargs: next(runs))

    with pytest.raises(click.ClickException, match=stderr or "exit 2"):
        deps_cmd._run_nodepools_check(tmp_path)


def test_nodepool_online_malformed_json_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = iter(
        [
            SimpleNamespace(returncode=0, stdout="# Offline\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="not json", stderr=""),
        ]
    )
    monkeypatch.setattr(deps_cmd, "_sts_identity_available", lambda: True)
    monkeypatch.setattr(deps_cmd.subprocess, "run", lambda *_args, **_kwargs: next(runs))

    with pytest.raises(click.ClickException, match="malformed JSON summary"):
        deps_cmd._run_nodepools_check(tmp_path)


def test_nodepool_online_report_is_optional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = iter(
        [
            SimpleNamespace(returncode=0, stdout="# Offline\n", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"status": "pass", "drift_count": 0, "regions_checked": 2}),
                stderr="",
            ),
        ]
    )
    monkeypatch.setattr(deps_cmd, "_sts_identity_available", lambda: True)
    monkeypatch.setattr(deps_cmd.subprocess, "run", lambda *_args, **_kwargs: next(runs))

    result = deps_cmd._run_nodepools_check(tmp_path)

    assert result["online"] == {
        "status": "pass",
        "drift_count": 0,
        "regions_checked": 2,
    }


def test_full_dependency_scan_rejects_unreadable_drift_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps_cmd.shutil, "which", lambda _tool: "/usr/bin/tool")

    def run(_command: list[str], **kwargs: object) -> SimpleNamespace:
        output = Path(kwargs["env"]["GITHUB_OUTPUT"])
        output.write_text(
            "has_drift=true\nscan_complete=true\nreport_path=/missing/report.md\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(deps_cmd.subprocess, "run", run)

    with pytest.raises(click.ClickException, match="report file is unreadable"):
        deps_cmd._run_full_scan(tmp_path, stream=False)


@pytest.mark.parametrize("online_report", [None, "# Online\n"])
def test_nodepool_scan_table_renders_offline_and_optional_online_reports(
    runner: CliRunner,
    online_report: str | None,
) -> None:
    online: dict[str, object] = {"status": "pass"}
    if online_report is not None:
        online["report_markdown"] = online_report
    envelope = {
        "nodepools_only": True,
        "has_drift": False,
        "scan_complete": True,
        "offline": {"status": "pass", "report_markdown": "# Offline\n"},
        "online": online,
    }

    with (
        patch.object(deps_cmd, "_repo_root", return_value=Path("/repo")),
        patch.object(deps_cmd, "_run_nodepools_check", return_value=envelope),
    ):
        result = runner.invoke(deps, ["scan", "--nodepools-only"], obj=_config())

    assert result.exit_code == 0, result.output
    assert "offline policy check: pass" in result.output
    assert "online EC2 catalog:   pass" in result.output
    assert "# Offline" in result.output
    assert ("# Online" in result.output) is (online_report is not None)


def test_set_password_reports_cognito_failure(runner: CliRunner) -> None:
    with (
        patch.object(analytics_cmd, "_require_cognito_pool_id", return_value=("pool", "us-east-1")),
        patch(
            "cli.analytics_user_mgmt.admin_set_user_password",
            side_effect=_client_error("InvalidPasswordException"),
        ),
    ):
        result = _invoke_analytics(
            runner,
            [
                "users",
                "set-password",
                "--username",
                "alice",
                "--password",
                "Bad!1",
                "--yes",
            ],
        )

    assert result.exit_code == 1
    assert "Failed to set password for alice: InvalidPasswordException" in result.output


def test_analytics_doctor_successfully_emits_checks_without_remediation(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    cdk_json = tmp_path / "cdk.json"
    cdk_json.write_text("{}", encoding="utf-8")
    with (
        patch("cli.stacks._find_cdk_json", return_value=cdk_json),
        patch("cli.config._load_cdk_json", return_value={"regional": []}),
        patch("cli.analytics_user_mgmt.check_stack_complete", return_value=(True, "")),
        patch("cli.analytics_user_mgmt.check_ssm_parameter", return_value=(True, "")),
        patch("cli.analytics_user_mgmt.scan_orphan_analytics_resources", return_value=[]),
    ):
        result = _invoke_analytics(runner, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "✓ cdk.json parses as JSON" in result.output
    assert "→" not in result.output
    assert "All pre-flight checks passed" in result.output
