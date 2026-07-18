"""Branch-coverage tests for ``cli/commands/analytics_cmd.py``.

The happy-path flows are covered by ``tests/test_analytics_cmd.py``; this
module fills in the error/recovery branches (ClientError paths, missing
confirmation prompts, HTTP/URL errors in studio login, missing lifecycle
script) to exercise the remaining branches and protect the project-wide 93%
coverage floor.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner
from moto import mock_aws


@pytest.fixture
def tmp_cdk_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cdk.json"
        path.write_text(json.dumps({"context": {}}))
        yield path


def _seed_gco_analytics_stack(region: str, pool_id: str, client_id: str) -> None:
    cfn = boto3.client("cloudformation", region_name=region)
    template = json.dumps(
        {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Resources": {"Topic": {"Type": "AWS::SNS::Topic"}},
            "Outputs": {
                "CognitoUserPoolId": {"Value": pool_id},
                "CognitoUserPoolClientId": {"Value": client_id},
            },
        }
    )
    cfn.create_stack(StackName="gco-analytics", TemplateBody=template)


@pytest.fixture
def aws_creds_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    yield


# ---------------------------------------------------------------------------
# Status / enable / disable — exception branches
# ---------------------------------------------------------------------------


class TestToggleExceptions:
    def test_status_surfaces_loader_exception(self, tmp_cdk_json):
        """When get_analytics_config raises, the CLI exits non-zero."""
        from cli.main import cli

        runner = CliRunner()
        with (
            patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
            patch(
                "cli.commands.analytics_cmd.get_output_formatter",
            ) as mock_formatter,
            patch(
                "cli.stacks.get_analytics_config",
                side_effect=RuntimeError("loader explosion"),
            ),
        ):
            # get_output_formatter returns a real formatter when not mocked;
            # wire a mock so we can inspect the error message.
            mock_formatter.return_value = MagicMock()
            result = runner.invoke(cli, ["analytics", "status"])
        assert result.exit_code == 1

    def test_enable_surfaces_update_exception(self, tmp_cdk_json):
        from cli.main import cli

        runner = CliRunner()
        with (
            patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
            patch(
                "cli.stacks.update_analytics_config",
                side_effect=OSError("disk full"),
            ),
        ):
            result = runner.invoke(cli, ["analytics", "enable", "-y"])
        assert result.exit_code == 1
        assert "Failed to enable" in result.output

    def test_disable_surfaces_update_exception(self, tmp_cdk_json):
        from cli.main import cli

        runner = CliRunner()
        with (
            patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
            patch(
                "cli.stacks.update_analytics_config",
                side_effect=OSError("disk full"),
            ),
        ):
            result = runner.invoke(cli, ["analytics", "disable", "-y"])
        assert result.exit_code == 1
        assert "Failed to disable" in result.output

    def test_enable_confirmation_prompt_when_no_yes_flag(self, tmp_cdk_json):
        """Without -y, click.confirm reads from stdin — 'n' aborts."""
        from cli.main import cli

        runner = CliRunner()
        with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
            result = runner.invoke(cli, ["analytics", "enable", "--hyperpod"], input="n\n")
        # click aborts with exit code 1 on user-declined confirm.
        assert result.exit_code != 0

    def test_disable_confirmation_prompt_when_no_yes_flag(self, tmp_cdk_json):
        from cli.main import cli

        runner = CliRunner()
        with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
            result = runner.invoke(cli, ["analytics", "disable"], input="n\n")
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Users subgroup — ClientError branches
# ---------------------------------------------------------------------------


class TestUsersClientErrorBranches:
    def test_users_add_surfaces_client_error(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")

            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch(
                    "cli.analytics_user_mgmt.admin_create_user",
                    side_effect=ClientError(
                        {"Error": {"Code": "UsernameExistsException", "Message": "x"}},
                        "AdminCreateUser",
                    ),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    cli,
                    ["analytics", "users", "add", "--username", "bob", "--no-email"],
                )

        assert result.exit_code == 1
        assert "UsernameExistsException" in result.output

    def test_users_list_surfaces_client_error(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")

            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch(
                    "cli.analytics_user_mgmt.list_users",
                    side_effect=ClientError(
                        {"Error": {"Code": "AccessDeniedException", "Message": "x"}},
                        "ListUsers",
                    ),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(cli, ["analytics", "users", "list"])

        assert result.exit_code == 1
        assert "AccessDeniedException" in result.output

    def test_users_list_renders_table_by_default(self, aws_creds_env, tmp_cdk_json):
        """Default (non-JSON) path exercises formatter.print."""
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            cognito.admin_create_user(
                UserPoolId=pool_id, Username="alice", MessageAction="SUPPRESS"
            )
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")

            with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
                runner = CliRunner()
                result = runner.invoke(cli, ["analytics", "users", "list"])
        assert result.exit_code == 0
        assert "alice" in result.output

    def test_users_remove_surfaces_client_error(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")

            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch(
                    "cli.analytics_user_mgmt.admin_delete_user",
                    side_effect=ClientError(
                        {"Error": {"Code": "UserNotFoundException", "Message": "x"}},
                        "AdminDeleteUser",
                    ),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    cli,
                    ["analytics", "users", "remove", "--username", "ghost", "--yes"],
                )

        assert result.exit_code == 1
        assert "UserNotFoundException" in result.output

    def test_users_add_no_temp_password_prints_guidance(self, aws_creds_env, tmp_cdk_json):
        """When Cognito returns no TemporaryPassword, the CLI prints the
        admin-set-user-password hint."""
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")

            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch(
                    "cli.analytics_user_mgmt.admin_create_user",
                    return_value=({"User": {"Username": "alice"}}, None),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    cli,
                    ["analytics", "users", "add", "--username", "alice", "--no-email"],
                )

        assert result.exit_code == 0
        assert "admin-set-user-password" in result.output


# ---------------------------------------------------------------------------
# studio login — error branches
# ---------------------------------------------------------------------------


def _seed_api_gateway_stack(region: str, endpoint: str) -> None:
    cfn = boto3.client("cloudformation", region_name=region)
    cfn.create_stack(
        StackName="gco-api-gateway",
        TemplateBody=json.dumps(
            {
                "AWSTemplateFormatVersion": "2010-09-09",
                "Resources": {"Topic": {"Type": "AWS::SNS::Topic"}},
                "Outputs": {"ApiEndpoint": {"Value": endpoint}},
            }
        ),
    )


class TestStudioLoginBranches:
    def test_login_fails_when_no_api_endpoint(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")
            # Intentionally no gco-api-gateway stack, no --api-url, no env var.

            with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
                runner = CliRunner()
                result = runner.invoke(
                    cli,
                    [
                        "analytics",
                        "studio",
                        "login",
                        "--username",
                        "alice",
                        "--password",
                        "hunter2",
                    ],
                )

        assert result.exit_code == 1
        assert "API Gateway" in result.output

    def test_login_fails_on_client_error(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")
            _seed_api_gateway_stack("us-east-2", "https://api/prod")

            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch(
                    "cli.analytics_user_mgmt.srp_authenticate",
                    side_effect=ClientError(
                        {"Error": {"Code": "NotAuthorizedException", "Message": "x"}},
                        "RespondToAuthChallenge",
                    ),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    cli,
                    [
                        "analytics",
                        "studio",
                        "login",
                        "--username",
                        "alice",
                        "--password",
                        "hunter2",
                    ],
                )

        assert result.exit_code == 1
        assert "NotAuthorizedException" in result.output

    def test_login_fails_when_no_id_token(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")
            _seed_api_gateway_stack("us-east-2", "https://api/prod")

            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch(
                    "cli.analytics_user_mgmt.srp_authenticate",
                    return_value={"IdToken": "", "AccessToken": "", "RefreshToken": ""},
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    cli,
                    [
                        "analytics",
                        "studio",
                        "login",
                        "--username",
                        "alice",
                        "--password",
                        "hunter2",
                    ],
                )

        assert result.exit_code == 1
        assert "no IdToken" in result.output

    def test_login_fails_on_httperror(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")
            _seed_api_gateway_stack("us-east-2", "https://api/prod")

            import email.message
            import urllib.error

            headers = email.message.Message()
            headers["x-amzn-RequestId"] = "req-boom"

            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch(
                    "cli.analytics_user_mgmt.srp_authenticate",
                    return_value={
                        "IdToken": "t",
                        "AccessToken": "a",
                        "RefreshToken": "r",
                    },
                ),
                patch(
                    "cli.analytics_user_mgmt.fetch_studio_url",
                    side_effect=urllib.error.HTTPError(
                        "https://api/prod/studio/login",
                        500,
                        "boom",
                        headers,
                        None,
                    ),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    cli,
                    [
                        "analytics",
                        "studio",
                        "login",
                        "--username",
                        "alice",
                        "--password",
                        "hunter2",
                    ],
                )

        assert result.exit_code == 2
        assert "HTTP 500" in result.output
        assert "req-boom" in result.output

    def test_login_fails_on_urlerror(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")
            _seed_api_gateway_stack("us-east-2", "https://api/prod")

            import urllib.error

            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch(
                    "cli.analytics_user_mgmt.srp_authenticate",
                    return_value={
                        "IdToken": "t",
                        "AccessToken": "a",
                        "RefreshToken": "r",
                    },
                ),
                patch(
                    "cli.analytics_user_mgmt.fetch_studio_url",
                    side_effect=urllib.error.URLError("connection refused"),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    cli,
                    [
                        "analytics",
                        "studio",
                        "login",
                        "--username",
                        "alice",
                        "--password",
                        "hunter2",
                    ],
                )

        assert result.exit_code == 2
        assert "network error" in result.output

    def test_login_fails_on_valueerror(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")
            _seed_api_gateway_stack("us-east-2", "https://api/prod")

            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch(
                    "cli.analytics_user_mgmt.srp_authenticate",
                    return_value={
                        "IdToken": "t",
                        "AccessToken": "a",
                        "RefreshToken": "r",
                    },
                ),
                patch(
                    "cli.analytics_user_mgmt.fetch_studio_url",
                    side_effect=ValueError("malformed"),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    cli,
                    [
                        "analytics",
                        "studio",
                        "login",
                        "--username",
                        "alice",
                        "--password",
                        "hunter2",
                    ],
                )

        assert result.exit_code == 2
        assert "malformed" in result.output

    def test_login_prompts_for_password_when_absent(self, aws_creds_env, tmp_cdk_json):
        """Without --password / env var, click.prompt reads from stdin."""
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")
            _seed_api_gateway_stack("us-east-2", "https://api/prod")

            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch(
                    "cli.analytics_user_mgmt.srp_authenticate",
                    return_value={
                        "IdToken": "t",
                        "AccessToken": "a",
                        "RefreshToken": "r",
                    },
                ),
                patch(
                    "cli.analytics_user_mgmt.fetch_studio_url",
                    return_value=("https://studio.aws/x", 180, "req-1"),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    cli,
                    ["analytics", "studio", "login", "--username", "alice"],
                    input="typed-password\n",
                )

        assert result.exit_code == 0
        assert "https://studio.aws/x" in result.output

    def test_login_uses_api_url_override_when_provided(self, aws_creds_env, tmp_cdk_json):
        """Passing --api-url short-circuits the discover_api_endpoint lookup."""
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")
            # No gco-api-gateway stack — but we pass --api-url, so we're fine.

            fetched_url: dict[str, str] = {}

            def _fake_fetch(api_base, id_token):
                fetched_url["api_base"] = api_base
                return ("https://studio.aws/x", 180, "req-1")

            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch(
                    "cli.analytics_user_mgmt.srp_authenticate",
                    return_value={
                        "IdToken": "t",
                        "AccessToken": "a",
                        "RefreshToken": "r",
                    },
                ),
                patch(
                    "cli.analytics_user_mgmt.fetch_studio_url",
                    side_effect=_fake_fetch,
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    cli,
                    [
                        "analytics",
                        "studio",
                        "login",
                        "--username",
                        "alice",
                        "--password",
                        "hunter2",
                        "--api-url",
                        "https://override/prod",
                    ],
                )

        assert result.exit_code == 0
        assert fetched_url["api_base"] == "https://override/prod"


# ---------------------------------------------------------------------------
# Doctor — cdk.json branches
# ---------------------------------------------------------------------------


class TestDoctorBranches:
    def test_doctor_reports_missing_cdk_json(self, aws_creds_env, tmp_cdk_json):
        """When cdk.json is absent, doctor prints the fix-it hint."""
        from cli.main import cli

        with mock_aws():
            runner = CliRunner()
            with patch("cli.stacks._find_cdk_json", return_value=None):
                result = runner.invoke(cli, ["analytics", "doctor"])
        assert result.exit_code == 1
        assert "cdk.json" in result.output

    def test_doctor_reports_malformed_cdk_json(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        tmp_cdk_json.write_text("{not valid json")

        with mock_aws():
            runner = CliRunner()
            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch("cli.config.Path.cwd", return_value=tmp_cdk_json.parent),
            ):
                result = runner.invoke(cli, ["analytics", "doctor"])
        assert result.exit_code == 1
        assert "cdk.json" in result.output
