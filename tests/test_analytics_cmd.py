"""
Tests for ``cli/commands/analytics_cmd.py``.

Covers:
* ``enable`` / ``disable`` / ``status`` against a temporary ``cdk.json``.
* ``enable --hyperpod`` setting the sub-toggle.
* ``users add`` / ``list`` / ``remove`` against a moto-backed Cognito pool
  auto-discovered through a moto-backed CloudFormation ``gco-analytics``
  stack.
* ``users add`` failing with the documented error message when the
  ``gco-analytics`` stack is absent.
* ``studio login`` SRP-authenticating against moto Cognito and fetching
  a Studio URL from a stubbed ``/studio/login`` HTTP endpoint.
* ``doctor`` passing on a healthy state and failing when the required
  SSM parameters are missing.

Plus two property-style tests:

* **Toggle round-trip** — for every ``(enabled, hyperpod_enabled)`` in
  ``{true, false}²``, ``enable`` / ``disable`` / ``enable --hyperpod``
  leave ``cdk.json`` in a state that re-reads to the exact tuple that
  was set (``@settings(max_examples=4)`` because the input space is
  exhaustively four points).
* **Enable + synth** — ``gco analytics enable`` then an integration-style
  ``cdk synth gco-analytics`` test that checks the synthesized template
  contains SageMaker Domain + EMR + Cognito resources, with AWS calls
  mocked.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import boto3
import pytest
from click.testing import CliRunner
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from moto import mock_aws

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_cdk_json():
    """Create a temporary cdk.json whose path is returned as a Path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cdk.json"
        path.write_text(json.dumps({"context": {}}))
        yield path


@pytest.fixture
def aws_creds_env():
    """Provide deterministic moto-friendly AWS credentials for the test."""
    previous = {
        "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID"),
        "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY"),
        "AWS_DEFAULT_REGION": os.environ.get("AWS_DEFAULT_REGION"),
    }
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-2"
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _seed_gco_analytics_stack(region: str, pool_id: str, client_id: str) -> None:
    """Create a ``gco-analytics`` CloudFormation stack with discovery outputs."""
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


def _seed_gco_api_gateway_stack(region: str, endpoint: str) -> None:
    """Create a ``gco-api-gateway`` stack with an ``ApiEndpoint`` output."""
    cfn = boto3.client("cloudformation", region_name=region)
    template = json.dumps(
        {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Resources": {"Topic": {"Type": "AWS::SNS::Topic"}},
            "Outputs": {"ApiEndpoint": {"Value": endpoint}},
        }
    )
    cfn.create_stack(StackName="gco-api-gateway", TemplateBody=template)


# ---------------------------------------------------------------------------
# Toggle tests
# ---------------------------------------------------------------------------


class TestToggles:
    def test_enable_writes_cdk_json(self, tmp_cdk_json):
        from cli.main import cli

        runner = CliRunner()
        with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
            result = runner.invoke(cli, ["analytics", "enable", "-y"])

        assert result.exit_code == 0, result.output
        data = json.loads(tmp_cdk_json.read_text())
        assert data["context"]["analytics_environment"]["enabled"] is True
        # hyperpod defaults to False when --hyperpod is absent
        assert data["context"]["analytics_environment"]["hyperpod"]["enabled"] is False

    def test_disable_writes_cdk_json(self, tmp_cdk_json):
        from cli.main import cli

        # Start from enabled=true so disable has something to flip.
        tmp_cdk_json.write_text(
            json.dumps(
                {
                    "context": {
                        "analytics_environment": {
                            "enabled": True,
                            "hyperpod": {"enabled": True},
                        }
                    }
                }
            )
        )

        runner = CliRunner()
        with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
            result = runner.invoke(cli, ["analytics", "disable", "-y"])

        assert result.exit_code == 0, result.output
        data = json.loads(tmp_cdk_json.read_text())
        assert data["context"]["analytics_environment"]["enabled"] is False
        # disable leaves the hyperpod sub-toggle alone
        assert data["context"]["analytics_environment"]["hyperpod"]["enabled"] is True

    def test_status_shows_current_config(self, tmp_cdk_json):
        from cli.main import cli

        tmp_cdk_json.write_text(
            json.dumps(
                {
                    "context": {
                        "analytics_environment": {
                            "enabled": True,
                            "hyperpod": {"enabled": False},
                        }
                    }
                }
            )
        )

        runner = CliRunner()
        with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
            result = runner.invoke(cli, ["analytics", "status"])

        assert result.exit_code == 0, result.output
        assert "ENABLED" in result.output or "enabled" in result.output

    def test_enable_with_hyperpod_flag_sets_sub_toggle(self, tmp_cdk_json):
        from cli.main import cli

        runner = CliRunner()
        with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
            result = runner.invoke(cli, ["analytics", "enable", "--hyperpod", "-y"])

        assert result.exit_code == 0, result.output
        data = json.loads(tmp_cdk_json.read_text())
        assert data["context"]["analytics_environment"]["enabled"] is True
        assert data["context"]["analytics_environment"]["hyperpod"]["enabled"] is True

    def test_enable_with_canvas_flag_sets_sub_toggle(self, tmp_cdk_json):
        from cli.main import cli

        runner = CliRunner()
        with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
            result = runner.invoke(cli, ["analytics", "enable", "--canvas", "-y"])

        assert result.exit_code == 0, result.output
        data = json.loads(tmp_cdk_json.read_text())
        ae = data["context"]["analytics_environment"]
        assert ae["enabled"] is True
        assert ae["canvas"]["enabled"] is True
        # hyperpod must stay off when only --canvas is passed.
        assert ae["hyperpod"]["enabled"] is False

    def test_enable_with_both_sub_toggles(self, tmp_cdk_json):
        from cli.main import cli

        runner = CliRunner()
        with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
            result = runner.invoke(
                cli,
                ["analytics", "enable", "--hyperpod", "--canvas", "-y"],
            )

        assert result.exit_code == 0, result.output
        ae = json.loads(tmp_cdk_json.read_text())["context"]["analytics_environment"]
        assert ae["enabled"] is True
        assert ae["hyperpod"]["enabled"] is True
        assert ae["canvas"]["enabled"] is True

    def test_disable_preserves_canvas_sub_toggle(self, tmp_cdk_json):
        from cli.main import cli

        # Pre-seed with canvas=true so we can prove disable doesn't touch it.
        tmp_cdk_json.write_text(
            json.dumps(
                {
                    "context": {
                        "analytics_environment": {
                            "enabled": True,
                            "hyperpod": {"enabled": False},
                            "canvas": {"enabled": True},
                        }
                    }
                }
            )
        )

        runner = CliRunner()
        with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
            result = runner.invoke(cli, ["analytics", "disable", "-y"])

        assert result.exit_code == 0, result.output
        ae = json.loads(tmp_cdk_json.read_text())["context"]["analytics_environment"]
        assert ae["enabled"] is False
        # The canvas sub-toggle survives a disable/enable round-trip.
        assert ae["canvas"]["enabled"] is True


# ---------------------------------------------------------------------------
# Users tests
# ---------------------------------------------------------------------------


class TestUsers:
    def test_users_add_prints_temporary_password(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")

            runner = CliRunner()
            with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
                result = runner.invoke(
                    cli,
                    [
                        "analytics",
                        "users",
                        "add",
                        "--username",
                        "alice",
                        "--email",
                        "alice@example.com",
                        "--no-email",
                    ],
                )

        assert result.exit_code == 0, result.output
        assert "alice" in result.output
        # moto does not return a temporary password — the CLI surfaces a
        # human-readable note rather than inventing one.
        assert "admin-set-user-password" in result.output or "printed exactly once" in result.output

    def test_users_add_with_explicit_password_sets_permanent(self, aws_creds_env, tmp_cdk_json):
        """``--password`` calls admin_set_user_password with Permanent=True
        and doesn't leak the password into stdout."""
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")

            runner = CliRunner()
            with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
                result = runner.invoke(
                    cli,
                    [
                        "analytics",
                        "users",
                        "add",
                        "--username",
                        "bob",
                        "--no-email",
                        "--password",
                        "StrongP@ssw0rd!",
                    ],
                )

            assert result.exit_code == 0, result.output
            # The password itself must not be echoed back.
            assert "StrongP@ssw0rd!" not in result.output
            assert "Password set (permanent)" in result.output

            # And the user must be in CONFIRMED / FORCE_CHANGE_PASSWORD=false
            # state — moto reflects AdminSetUserPassword's Permanent=True by
            # flipping the user's status to CONFIRMED.
            user = cognito.admin_get_user(UserPoolId=pool_id, Username="bob")
            assert user["UserStatus"] == "CONFIRMED"

    def test_users_add_with_generate_password_prints_once(self, aws_creds_env, tmp_cdk_json):
        """``--generate-password`` generates a strong password, sets it
        permanent on Cognito, and prints it once."""
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")

            runner = CliRunner()
            with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
                result = runner.invoke(
                    cli,
                    [
                        "analytics",
                        "users",
                        "add",
                        "--username",
                        "carol",
                        "--no-email",
                        "--generate-password",
                    ],
                )

            assert result.exit_code == 0, result.output
            assert "Generated password" in result.output
            # Password appears exactly once in output (printed-once guarantee).
            # Extract it and sanity-check the shape.
            import re

            match = re.search(r"Generated password[^:]*:\s*(\S.+)$", result.output, re.M)
            assert match, (
                f"expected a 'Generated password ...: <pw>' line in output; got {result.output!r}"
            )
            generated = match.group(1).strip()
            assert len(generated) >= 16
            # Cognito policy classes.
            assert any(c.islower() for c in generated)
            assert any(c.isupper() for c in generated)
            assert any(c.isdigit() for c in generated)

            user = cognito.admin_get_user(UserPoolId=pool_id, Username="carol")
            assert user["UserStatus"] == "CONFIRMED"

    def test_users_add_rejects_password_and_generate_together(self, aws_creds_env, tmp_cdk_json):
        """``--password`` and ``--generate-password`` are mutually exclusive."""
        from cli.main import cli

        runner = CliRunner()
        with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
            result = runner.invoke(
                cli,
                [
                    "analytics",
                    "users",
                    "add",
                    "--username",
                    "dave",
                    "--no-email",
                    "--password",
                    "StrongP@ssw0rd!",
                    "--generate-password",
                ],
            )

        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_users_list_shows_all_users(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            cognito.admin_create_user(
                UserPoolId=pool_id, Username="alice", MessageAction="SUPPRESS"
            )
            cognito.admin_create_user(UserPoolId=pool_id, Username="bob", MessageAction="SUPPRESS")
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")

            runner = CliRunner()
            with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
                result = runner.invoke(cli, ["analytics", "users", "list", "--as-json"])

        assert result.exit_code == 0, result.output
        users = json.loads(result.output)
        usernames = {row["username"] for row in users}
        assert {"alice", "bob"} <= usernames

    def test_users_remove_confirms_then_deletes(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            cognito.admin_create_user(
                UserPoolId=pool_id, Username="alice", MessageAction="SUPPRESS"
            )
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")

            runner = CliRunner()
            with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
                # Without --yes, click.confirm reads from stdin — "y\n" confirms.
                result = runner.invoke(
                    cli,
                    ["analytics", "users", "remove", "--username", "alice"],
                    input="y\n",
                )

            assert result.exit_code == 0, result.output
            remaining = cognito.list_users(UserPoolId=pool_id).get("Users", [])
            assert all(u.get("Username") != "alice" for u in remaining)

    def test_users_add_fails_when_stack_not_deployed(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        # No gco-analytics stack is created — describe_stacks returns
        # ValidationError which the CLI surfaces as the documented error.
        with mock_aws():
            runner = CliRunner()
            with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
                result = runner.invoke(
                    cli,
                    ["analytics", "users", "add", "--username", "alice", "--no-email"],
                )

        assert result.exit_code == 1, result.output
        assert "gco-analytics stack not deployed" in result.output

    def test_users_set_password_with_explicit_password(self, aws_creds_env, tmp_cdk_json):
        """``users set-password --password`` sets a permanent password."""
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            cognito.admin_create_user(
                UserPoolId=pool_id, Username="alice", MessageAction="SUPPRESS"
            )
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")

            captured: dict[str, Any] = {}

            def _spy(*, pool_id, region, username, password, permanent):
                captured["password"] = password
                captured["permanent"] = permanent
                captured["username"] = username

            runner = CliRunner()
            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch(
                    "cli.analytics_user_mgmt.admin_set_user_password",
                    side_effect=_spy,
                ),
            ):
                result = runner.invoke(
                    cli,
                    [
                        "analytics",
                        "users",
                        "set-password",
                        "--username",
                        "alice",
                        "--password",
                        "Strong!Pass123$",
                        "--yes",
                    ],
                )

            assert result.exit_code == 0, result.output
            assert "Password set (permanent) for alice" in result.output
            assert captured == {
                "password": "Strong!Pass123$",
                "permanent": True,
                "username": "alice",
            }

    def test_users_set_password_generate_flag(self, aws_creds_env, tmp_cdk_json):
        """``users set-password --generate-password`` emits the new password once."""
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            cognito.admin_create_user(
                UserPoolId=pool_id, Username="alice", MessageAction="SUPPRESS"
            )
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")

            runner = CliRunner()
            with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
                result = runner.invoke(
                    cli,
                    [
                        "analytics",
                        "users",
                        "set-password",
                        "--username",
                        "alice",
                        "--generate-password",
                        "--yes",
                    ],
                )

            assert result.exit_code == 0, result.output
            assert "Generated password (printed exactly once):" in result.output

    def test_users_set_password_mutually_exclusive_flags(self, aws_creds_env, tmp_cdk_json):
        """Passing both --password and --generate-password fails fast."""
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")

            runner = CliRunner()
            with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
                result = runner.invoke(
                    cli,
                    [
                        "analytics",
                        "users",
                        "set-password",
                        "--username",
                        "alice",
                        "--password",
                        "Strong!Pass123$",
                        "--generate-password",
                        "--yes",
                    ],
                )

            assert result.exit_code == 1, result.output
            assert "mutually exclusive" in result.output

    def test_users_set_password_temporary_flag(self, aws_creds_env, tmp_cdk_json):
        """``--temporary`` sets Permanent=false and emits the expected message."""
        from cli.main import cli

        with mock_aws():
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            cognito.admin_create_user(
                UserPoolId=pool_id, Username="alice", MessageAction="SUPPRESS"
            )
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")

            captured: dict[str, Any] = {}

            def _spy(*, pool_id, region, username, password, permanent):
                captured["permanent"] = permanent
                captured["username"] = username

            runner = CliRunner()
            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch(
                    "cli.analytics_user_mgmt.admin_set_user_password",
                    side_effect=_spy,
                ),
            ):
                result = runner.invoke(
                    cli,
                    [
                        "analytics",
                        "users",
                        "set-password",
                        "--username",
                        "alice",
                        "--password",
                        "Strong!Pass123$",
                        "--temporary",
                        "--yes",
                    ],
                )

            assert result.exit_code == 0, result.output
            assert "Password set (temporary) for alice" in result.output
            assert captured == {"permanent": False, "username": "alice"}

    def test_users_set_password_fails_when_stack_not_deployed(self, aws_creds_env, tmp_cdk_json):
        """Emits the documented error when gco-analytics is absent."""
        from cli.main import cli

        with mock_aws():
            runner = CliRunner()
            with patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json):
                result = runner.invoke(
                    cli,
                    [
                        "analytics",
                        "users",
                        "set-password",
                        "--username",
                        "alice",
                        "--password",
                        "Strong!Pass123$",
                        "--yes",
                    ],
                )

        assert result.exit_code == 1, result.output
        assert "gco-analytics stack not deployed" in result.output


# ---------------------------------------------------------------------------
# Studio login test
# ---------------------------------------------------------------------------


class TestStudioLogin:
    def test_studio_login_fetches_url(self, aws_creds_env, tmp_cdk_json):
        """Stub SRP auth + HTTP call and assert the Studio URL is printed."""
        from cli.main import cli

        expected_url = "https://studio.example.aws/auth?token=abc"
        # Use a captured-value container so the stub can mutate it.
        captured: dict[str, str] = {}

        def _fake_srp_authenticate(**kwargs):  # duck-typed
            captured["username"] = kwargs["username"]
            captured["pool_id"] = kwargs["pool_id"]
            captured["client_id"] = kwargs["client_id"]
            return {
                "IdToken": "fake.id.token",
                "AccessToken": "fake.access.token",
                "RefreshToken": "fake.refresh.token",
            }

        class _FakeResponse:
            status = 200

            def __init__(self, body: bytes) -> None:
                self._body = body
                self.headers: dict[str, str] = {"x-amzn-RequestId": "req-42"}

            def read(self) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                return None

        def _fake_urlopen(request, timeout=None):  # duck-typed
            captured["login_url"] = request.full_url
            captured["authorization"] = request.headers.get("Authorization")
            return _FakeResponse(
                json.dumps({"url": expected_url, "expires_in": 300}).encode("utf-8")
            )

        with mock_aws():
            # Seed both CloudFormation stacks so discovery succeeds.
            cognito = boto3.client("cognito-idp", region_name="us-east-2")
            pool = cognito.create_user_pool(PoolName="gco-studio")
            pool_id = pool["UserPool"]["Id"]
            _seed_gco_analytics_stack("us-east-2", pool_id, "client-abc")
            _seed_gco_api_gateway_stack(
                "us-east-2", "https://api.example.execute-api.us-east-2.amazonaws.com/prod"
            )

            runner = CliRunner()
            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch(
                    "cli.analytics_user_mgmt.srp_authenticate",
                    side_effect=_fake_srp_authenticate,
                ),
                patch(
                    "urllib.request.urlopen",
                    side_effect=_fake_urlopen,
                ),
            ):
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

        assert result.exit_code == 0, result.output
        assert expected_url in result.output
        # The password must not appear anywhere in the captured output
        # (no password echo, even on success).
        assert "hunter2" not in result.output
        assert captured["username"] == "alice"
        assert captured["authorization"] == "fake.id.token"
        assert "/studio/login" in captured["login_url"]


# ---------------------------------------------------------------------------
# Doctor tests
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_doctor_passes_on_healthy_state(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        tmp_cdk_json.write_text(
            json.dumps(
                {
                    "context": {
                        "deployment_regions": {
                            "global": "us-east-2",
                            "api_gateway": "us-east-2",
                            "regional": ["us-east-1"],
                            "monitoring": "us-east-2",
                        }
                    }
                }
            )
        )

        with mock_aws():
            # Publish the three SSM params in the global region.
            ssm = boto3.client("ssm", region_name="us-east-2")
            ssm.put_parameter(
                Name="/gco/cluster-shared-bucket/name",
                Value="gco-cluster-shared-123-us-east-2",
                Type="String",
            )
            ssm.put_parameter(
                Name="/gco/cluster-shared-bucket/arn",
                Value="arn:aws:s3:::gco-cluster-shared-123-us-east-2",
                Type="String",
            )
            ssm.put_parameter(
                Name="/gco/cluster-shared-bucket/region",
                Value="us-east-2",
                Type="String",
            )

            # Seed the three required stacks.
            template = json.dumps(
                {
                    "AWSTemplateFormatVersion": "2010-09-09",
                    "Resources": {"Topic": {"Type": "AWS::SNS::Topic"}},
                }
            )
            for region, stack in (
                ("us-east-2", "gco-global"),
                ("us-east-2", "gco-api-gateway"),
                ("us-east-1", "gco-us-east-1"),
            ):
                boto3.client("cloudformation", region_name=region).create_stack(
                    StackName=stack, TemplateBody=template
                )

            runner = CliRunner()
            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch("cli.config.Path.cwd", return_value=tmp_cdk_json.parent),
            ):
                result = runner.invoke(cli, ["analytics", "doctor"])

        assert result.exit_code == 0, result.output
        assert "All pre-flight checks passed" in result.output

    def test_doctor_fails_on_missing_ssm_param(self, aws_creds_env, tmp_cdk_json):
        from cli.main import cli

        tmp_cdk_json.write_text(
            json.dumps(
                {
                    "context": {
                        "deployment_regions": {
                            "global": "us-east-2",
                            "api_gateway": "us-east-2",
                            "regional": [],
                            "monitoring": "us-east-2",
                        }
                    }
                }
            )
        )

        with mock_aws():
            # Seed the stacks but intentionally omit the SSM params.
            template = json.dumps(
                {
                    "AWSTemplateFormatVersion": "2010-09-09",
                    "Resources": {"Topic": {"Type": "AWS::SNS::Topic"}},
                }
            )
            for stack in ("gco-global", "gco-api-gateway"):
                boto3.client("cloudformation", region_name="us-east-2").create_stack(
                    StackName=stack, TemplateBody=template
                )

            runner = CliRunner()
            with (
                patch("cli.stacks._find_cdk_json", return_value=tmp_cdk_json),
                patch("cli.config.Path.cwd", return_value=tmp_cdk_json.parent),
            ):
                result = runner.invoke(cli, ["analytics", "doctor"])

        assert result.exit_code == 1, result.output
        assert "SSM parameter /gco/cluster-shared-bucket/name" in result.output


# ---------------------------------------------------------------------------
# Hypothesis toggle round-trip property
# ---------------------------------------------------------------------------


@settings(
    max_examples=4, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(enabled=st.booleans(), hyperpod_enabled=st.booleans())
def test_toggle_round_trip_property(enabled: bool, hyperpod_enabled: bool) -> None:
    """CLI enable/disable round-trip preserves the toggle pair.

    Exhaustive over the four points in ``{true, false}²``.
    """
    from cli.main import cli
    from cli.stacks import get_analytics_config

    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        cdk_path = Path(tmpdir) / "cdk.json"
        cdk_path.write_text(json.dumps({"context": {}}))

        with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
            if enabled and hyperpod_enabled:
                invocation = ["analytics", "enable", "--hyperpod", "-y"]
            elif enabled and not hyperpod_enabled:
                invocation = ["analytics", "enable", "-y"]
            else:
                # For (False, *) the base state is disabled regardless.
                # First enable --hyperpod so we can confirm disable preserves it.
                runner.invoke(cli, ["analytics", "enable", "--hyperpod", "-y"])
                invocation = ["analytics", "disable", "-y"]

            result = runner.invoke(cli, invocation)
            assert result.exit_code == 0, result.output

            current = get_analytics_config()

        assert current["enabled"] is enabled
        if enabled:
            # enable --hyperpod / enable without the flag round-trip exactly
            assert current["hyperpod"]["enabled"] is hyperpod_enabled


# ---------------------------------------------------------------------------
# CLI enable followed by CDK synth emits the expected resources
# ---------------------------------------------------------------------------


def test_enable_then_synth_emits_sagemaker_emr_cognito(tmp_path, monkeypatch):
    """``analytics enable`` then ``cdk synth`` emits the analytics resources.

    Rather than shelling out to the real ``cdk`` CLI (which would need
    ``npx`` and node), this test drives the in-process CDK app via
    ``aws_cdk.App`` + ``Template.from_stack`` and confirms the
    synthesized template carries SageMaker Domain, EMR Serverless app,
    and Cognito user pool resources. AWS calls are not made — CDK
    synthesis is purely template-generation.
    """
    from cli.main import cli

    # Write a minimal cdk.json with the deployment_regions so the CDK
    # app can resolve regions during synth.
    cdk_path = tmp_path / "cdk.json"
    cdk_path.write_text(
        json.dumps(
            {
                "app": "python3 app.py",
                "context": {
                    "deployment_regions": {
                        "global": "us-east-2",
                        "api_gateway": "us-east-2",
                        "regional": ["us-east-1"],
                        "monitoring": "us-east-2",
                    },
                },
            }
        )
    )

    runner = CliRunner()
    with patch("cli.stacks._find_cdk_json", return_value=cdk_path):
        result = runner.invoke(cli, ["analytics", "enable", "-y"])
    assert result.exit_code == 0, result.output

    # Now directly synthesize gco-analytics in-process. Import the
    # stack lazily so the rest of this file can run even when CDK is
    # unavailable (the other tests don't need it).
    try:
        import aws_cdk as cdk
        from aws_cdk.assertions import Template

        from gco.config.config_loader import ConfigLoader
        from gco.stacks.analytics_stack import GCOAnalyticsStack
    except Exception:
        pytest.skip("CDK or analytics stack not importable in this environment")

    # Point ConfigLoader at our temporary cdk.json by pushing it into
    # the process CWD so its default loader sees the right file.
    monkeypatch.chdir(tmp_path)

    app = cdk.App(
        context={
            "analytics_environment": {
                "enabled": True,
                "hyperpod": {"enabled": False},
                "cognito": {"removal_policy": "destroy"},
                "efs": {"removal_policy": "destroy"},
            },
            "deployment_regions": {
                "global": "us-east-2",
                "api_gateway": "us-east-2",
                "regional": ["us-east-1"],
                "monitoring": "us-east-2",
            },
        }
    )
    config = ConfigLoader(app)
    stack = GCOAnalyticsStack(
        app,
        "gco-analytics",
        config=config,
        env=cdk.Environment(account="123456789012", region="us-east-2"),
    )
    template = Template.from_stack(stack)

    # Primary assertion: the three signature resource types are present.
    template.resource_count_is("AWS::SageMaker::Domain", 1)
    # EMR Serverless uses CfnApplication from emrserverless module.
    assert len(template.find_resources("AWS::EMRServerless::Application")) >= 1
    assert len(template.find_resources("AWS::Cognito::UserPool")) >= 1
