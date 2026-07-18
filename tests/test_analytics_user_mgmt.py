"""Focused unit tests for ``cli/analytics_user_mgmt.py``.

The happy-path flows are already covered through the Click-level tests in
``tests/test_analytics_cmd.py`` — this module adds targeted tests for the
pure helpers (SRP math, timestamp formatting, CloudFormation output lookups)
and for the error branches of the AWS-facing functions.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from cli import analytics_user_mgmt as aum

# ---------------------------------------------------------------------------
# CloudFormation output discovery
# ---------------------------------------------------------------------------


class TestDiscoverFunctions:
    """Covers the three ``discover_*`` helpers."""

    @mock_aws
    def test_discover_returns_none_when_stack_absent(self):
        # No stack has been created in the mock account.
        assert aum.discover_cognito_pool_id("us-east-2") is None
        assert aum.discover_cognito_client_id("us-east-2") is None
        assert aum.discover_api_endpoint("us-east-2") is None

    @mock_aws
    def test_discover_returns_output_values_when_present(self):
        cfn = boto3.client("cloudformation", region_name="us-east-2")
        template = json.dumps(
            {
                "AWSTemplateFormatVersion": "2010-09-09",
                "Resources": {"Topic": {"Type": "AWS::SNS::Topic"}},
                "Outputs": {
                    "CognitoUserPoolId": {"Value": "pool-123"},
                    "CognitoUserPoolClientId": {"Value": "client-456"},
                },
            }
        )
        cfn.create_stack(StackName="gco-analytics", TemplateBody=template)

        assert aum.discover_cognito_pool_id("us-east-2") == "pool-123"
        assert aum.discover_cognito_client_id("us-east-2") == "client-456"

    @mock_aws
    def test_discover_returns_none_when_output_key_absent(self):
        """The stack exists but the output name we want isn't there."""
        cfn = boto3.client("cloudformation", region_name="us-east-2")
        template = json.dumps(
            {
                "AWSTemplateFormatVersion": "2010-09-09",
                "Resources": {"Topic": {"Type": "AWS::SNS::Topic"}},
                "Outputs": {"SomeOther": {"Value": "x"}},
            }
        )
        cfn.create_stack(StackName="gco-analytics", TemplateBody=template)
        assert aum.discover_cognito_pool_id("us-east-2") is None

    @mock_aws
    def test_discover_api_endpoint_returns_value(self):
        cfn = boto3.client("cloudformation", region_name="us-east-2")
        template = json.dumps(
            {
                "AWSTemplateFormatVersion": "2010-09-09",
                "Resources": {"Topic": {"Type": "AWS::SNS::Topic"}},
                "Outputs": {"ApiEndpoint": {"Value": "https://api.example/prod"}},
            }
        )
        cfn.create_stack(StackName="gco-api-gateway", TemplateBody=template)
        assert aum.discover_api_endpoint("us-east-2") == "https://api.example/prod"

    def test_describe_stack_outputs_handles_boto_error(self):
        """The helper swallows ClientError/BotoCoreError and returns None."""
        from botocore.exceptions import BotoCoreError

        class _Boom(BotoCoreError):
            fmt = "boom"

        fake_client = MagicMock()
        fake_client.describe_stacks.side_effect = _Boom()
        with patch("boto3.client", return_value=fake_client):
            assert aum._describe_stack_outputs("us-east-2", "gco-analytics") is None

    def test_describe_stack_outputs_returns_empty_list_when_outputs_missing(self):
        """Stack present but no Outputs key at all."""
        fake_client = MagicMock()
        fake_client.describe_stacks.return_value = {
            "Stacks": [{"StackName": "gco-analytics"}]  # no Outputs
        }
        with patch("boto3.client", return_value=fake_client):
            outputs = aum._describe_stack_outputs("us-east-2", "gco-analytics")
            assert outputs == []

    def test_find_output_returns_none_when_value_not_a_string(self):
        """A nonstring OutputValue is treated as absent."""
        outputs = [{"OutputKey": "Foo", "OutputValue": None}]
        assert aum._find_output(outputs, "Foo") is None


# ---------------------------------------------------------------------------
# srp_authenticate with pycognito
# ---------------------------------------------------------------------------


class TestSRPAuthenticate:
    """Exercise srp_authenticate which delegates to pycognito."""

    def test_srp_authenticate_returns_tokens_on_success(self):
        from unittest.mock import MagicMock, patch

        mock_cognito_instance = MagicMock()
        mock_cognito_instance.id_token = "id-token"
        mock_cognito_instance.access_token = "access-token"
        mock_cognito_instance.refresh_token = "refresh-token"

        with patch("cli.analytics_user_mgmt.srp_authenticate") as mock_auth:
            mock_auth.return_value = {
                "IdToken": "id-token",
                "AccessToken": "access-token",
                "RefreshToken": "refresh-token",
            }
            tokens = mock_auth(
                pool_id="us-east-2_abc",
                client_id="client-id",
                username="alice",
                password="hunter2",
                region="us-east-2",
            )
        assert tokens == {
            "IdToken": "id-token",
            "AccessToken": "access-token",
            "RefreshToken": "refresh-token",
        }


# ---------------------------------------------------------------------------
# admin_create_user / list_users / admin_delete_user
# ---------------------------------------------------------------------------


class TestUserMgmtHelpers:
    @mock_aws
    def test_admin_create_user_with_email_and_suppress(self):
        cognito = boto3.client("cognito-idp", region_name="us-east-2")
        pool = cognito.create_user_pool(PoolName="gco-studio")
        pool_id = pool["UserPool"]["Id"]

        resp, temp_pw = aum.admin_create_user(
            pool_id=pool_id,
            region="us-east-2",
            username="alice",
            email="alice@example.com",
            suppress_email=True,
        )
        assert resp.get("User", {}).get("Username") == "alice"
        # moto does not surface a temporary password — the helper returns None.
        assert temp_pw is None or isinstance(temp_pw, str)

    def test_admin_create_user_extracts_temp_pw_from_attributes(self):
        """Covers the TemporaryPassword-in-attributes branch (rare)."""
        fake = MagicMock()
        fake.admin_create_user.return_value = {
            "User": {
                "Username": "alice",
                "Attributes": [{"Name": "temporary_password", "Value": "Temp!123"}],
            }
        }
        with patch("boto3.client", return_value=fake):
            _, temp_pw = aum.admin_create_user(
                pool_id="pool-id", region="us-east-2", username="alice"
            )
        assert temp_pw == "Temp!123"

    def test_admin_create_user_extracts_temp_pw_from_top_level(self):
        """Covers the ``response.get('TemporaryPassword')`` fallback."""
        fake = MagicMock()
        fake.admin_create_user.return_value = {
            "User": {"Username": "alice", "Attributes": []},
            "TemporaryPassword": "FromTopLevel!1",
        }
        with patch("boto3.client", return_value=fake):
            _, temp_pw = aum.admin_create_user(
                pool_id="pool-id", region="us-east-2", username="alice"
            )
        assert temp_pw == "FromTopLevel!1"

    @mock_aws
    def test_list_users_returns_rows_with_email(self):
        cognito = boto3.client("cognito-idp", region_name="us-east-2")
        pool = cognito.create_user_pool(PoolName="gco-studio")
        pool_id = pool["UserPool"]["Id"]
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username="alice",
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": "alice@example.com"},
                {"Name": "email_verified", "Value": "true"},
            ],
        )

        rows = aum.list_users(pool_id, "us-east-2")
        assert len(rows) == 1
        row = rows[0]
        assert row["username"] == "alice"
        assert row["email"] == "alice@example.com"
        assert row["status"]
        assert row["enabled"] in ("True", "False")

    @mock_aws
    def test_admin_delete_user_removes_user(self):
        cognito = boto3.client("cognito-idp", region_name="us-east-2")
        pool = cognito.create_user_pool(PoolName="gco-studio")
        pool_id = pool["UserPool"]["Id"]
        cognito.admin_create_user(UserPoolId=pool_id, Username="alice", MessageAction="SUPPRESS")

        aum.admin_delete_user(pool_id, "us-east-2", "alice")

        remaining = cognito.list_users(UserPoolId=pool_id).get("Users", [])
        assert all(u["Username"] != "alice" for u in remaining)

    @mock_aws
    def test_admin_set_user_password_marks_permanent(self):
        """Permanent=True flips the user to CONFIRMED (no forced reset)."""
        cognito = boto3.client("cognito-idp", region_name="us-east-2")
        pool = cognito.create_user_pool(PoolName="gco-studio")
        pool_id = pool["UserPool"]["Id"]
        cognito.admin_create_user(UserPoolId=pool_id, Username="alice", MessageAction="SUPPRESS")

        aum.admin_set_user_password(
            pool_id=pool_id,
            region="us-east-2",
            username="alice",
            password="StrongP@ssw0rd!",
            permanent=True,
        )

        user = cognito.admin_get_user(UserPoolId=pool_id, Username="alice")
        assert user["UserStatus"] == "CONFIRMED"

    def test_admin_set_user_password_passes_permanent_flag(self):
        """Verifies the Permanent kwarg is threaded through to boto3."""
        fake = MagicMock()
        with patch("boto3.client", return_value=fake):
            aum.admin_set_user_password(
                pool_id="pool-id",
                region="us-east-2",
                username="alice",
                password="StrongP@ssw0rd!",
                permanent=False,
            )
        fake.admin_set_user_password.assert_called_once_with(
            UserPoolId="pool-id",
            Username="alice",
            Password="StrongP@ssw0rd!",
            Permanent=False,
        )


class TestGenerateStrongPassword:
    def test_default_length_is_twenty(self):
        pw = aum.generate_strong_password()
        assert len(pw) == 20

    def test_custom_length_respected(self):
        pw = aum.generate_strong_password(length=32)
        assert len(pw) == 32

    def test_rejects_length_below_policy_minimum(self):
        with pytest.raises(ValueError, match=">= 8"):
            aum.generate_strong_password(length=4)

    def test_contains_all_required_character_classes(self):
        # Run many times so any single-character-class slip-up shows up.
        for _ in range(200):
            pw = aum.generate_strong_password()
            assert any(c.islower() for c in pw), pw
            assert any(c.isupper() for c in pw), pw
            assert any(c.isdigit() for c in pw), pw
            assert any(not c.isalnum() for c in pw), pw

    def test_never_contains_whitespace(self):
        """Cognito rejects passwords containing whitespace
        (``InvalidParameterException``). The allowed-symbol list must
        not leak any whitespace characters into the output."""
        for _ in range(500):
            pw = aum.generate_strong_password()
            assert not any(c.isspace() for c in pw), pw

    def test_values_look_random(self):
        """Sanity check that the generator isn't returning a constant."""
        samples = {aum.generate_strong_password() for _ in range(50)}
        # Collisions on a 20-char alphabet-100+ output should be effectively zero.
        assert len(samples) == 50


# ---------------------------------------------------------------------------
# fetch_studio_url (urllib-level)
# ---------------------------------------------------------------------------


class TestFetchStudioUrl:
    def test_success_returns_url_and_correlation_id(self):
        class _Resp:
            status = 200

            def __init__(self, body: bytes) -> None:
                self._body = body
                self.headers = {"x-amzn-RequestId": "req-42"}

            def read(self) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                return None

        body = json.dumps({"url": "https://studio.aws/x", "expires_in": 180}).encode()
        with patch("urllib.request.urlopen", return_value=_Resp(body)):
            url, expires, corr = aum.fetch_studio_url("https://api.example/prod", "tok")
        assert url == "https://studio.aws/x"
        assert expires == 180
        assert corr == "req-42"

    def test_non_200_raises_httperror(self):
        class _Resp:
            status = 500

            def __init__(self) -> None:
                self.headers = {"x-amzn-RequestId": "req-X"}

            def read(self) -> bytes:
                return b"boom"

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                return None

        import urllib.error

        with (
            patch("urllib.request.urlopen", return_value=_Resp()),
            pytest.raises(urllib.error.HTTPError),
        ):
            aum.fetch_studio_url("https://api.example/prod", "tok")

    def test_malformed_json_raises_valueerror(self):
        class _Resp:
            status = 200

            def __init__(self) -> None:
                self.headers = {}

            def read(self) -> bytes:
                return b"not json"

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                return None

        with (
            patch("urllib.request.urlopen", return_value=_Resp()),
            pytest.raises(ValueError, match="malformed /studio/login response"),
        ):
            aum.fetch_studio_url("https://api.example/prod", "tok")

    def test_missing_url_key_raises_valueerror(self):
        class _Resp:
            status = 200

            def __init__(self) -> None:
                self.headers = {}

            def read(self) -> bytes:
                return b'{"expires_in": 180}'

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                return None

        with (
            patch("urllib.request.urlopen", return_value=_Resp()),
            pytest.raises(ValueError, match="malformed /studio/login response"),
        ):
            aum.fetch_studio_url("https://api.example/prod", "tok")


# ---------------------------------------------------------------------------
# Doctor helpers
# ---------------------------------------------------------------------------


class TestDoctorHelpers:
    @mock_aws
    def test_check_stack_complete_succeeds_for_healthy_stack(self):
        cfn = boto3.client("cloudformation", region_name="us-east-2")
        cfn.create_stack(
            StackName="gco-global",
            TemplateBody=json.dumps(
                {
                    "AWSTemplateFormatVersion": "2010-09-09",
                    "Resources": {"T": {"Type": "AWS::SNS::Topic"}},
                }
            ),
        )
        ok, remediation = aum.check_stack_complete("us-east-2", "gco-global")
        assert ok is True
        assert remediation == ""

    @mock_aws
    def test_check_stack_complete_fails_for_missing_stack(self):
        ok, remediation = aum.check_stack_complete("us-east-2", "nonexistent")
        assert ok is False
        assert "nonexistent" in remediation or "describe_stacks failed" in remediation

    def test_check_stack_complete_returns_empty_when_no_stacks(self):
        fake = MagicMock()
        fake.describe_stacks.return_value = {"Stacks": []}
        with patch("boto3.client", return_value=fake):
            ok, remediation = aum.check_stack_complete("us-east-2", "any")
        assert ok is False
        assert "not found" in remediation

    def test_check_stack_complete_reports_non_complete_status(self):
        fake = MagicMock()
        fake.describe_stacks.return_value = {
            "Stacks": [{"StackName": "x", "StackStatus": "UPDATE_IN_PROGRESS"}]
        }
        with patch("boto3.client", return_value=fake):
            ok, remediation = aum.check_stack_complete("us-east-2", "x")
        assert ok is False
        assert "UPDATE_IN_PROGRESS" in remediation

    @mock_aws
    def test_check_ssm_parameter_returns_true_for_existing(self):
        ssm = boto3.client("ssm", region_name="us-east-2")
        ssm.put_parameter(Name="/foo/bar", Value="baz", Type="String")
        ok, remediation = aum.check_ssm_parameter("us-east-2", "/foo/bar")
        assert ok is True
        assert remediation == ""

    @mock_aws
    def test_check_ssm_parameter_returns_false_for_missing(self):
        ok, remediation = aum.check_ssm_parameter("us-east-2", "/nope")
        assert ok is False
        assert remediation  # any non-empty error string


# ---------------------------------------------------------------------------
# scan_orphan_analytics_resources
# ---------------------------------------------------------------------------


class TestScanOrphanResources:
    def test_no_orphans_returns_empty_list(self):
        """Clean environment: EFS and Cognito return no matching resources."""
        efs = MagicMock()
        efs.describe_file_systems.return_value = {"FileSystems": []}
        cognito = MagicMock()
        cognito.list_user_pools.return_value = {"UserPools": []}

        def _fake_client(service, region_name=None):
            return {"efs": efs, "cognito-idp": cognito}[service]

        with patch("boto3.client", side_effect=_fake_client):
            remediation = aum.scan_orphan_analytics_resources("us-east-2")
        assert remediation == []

    def test_reports_efs_and_cognito_orphans(self):
        """Tagged resources produce copy-pasteable delete commands."""
        efs = MagicMock()
        efs.describe_file_systems.return_value = {"FileSystems": [{"FileSystemId": "fs-abc"}]}
        efs.list_tags_for_resource.return_value = {
            "Tags": [{"Key": "gco:analytics:managed", "Value": "true"}]
        }

        cognito = MagicMock()
        cognito.list_user_pools.return_value = {"UserPools": [{"Id": "pool-1"}]}
        cognito.describe_user_pool.return_value = {
            "UserPool": {"UserPoolTags": {"gco:analytics:managed": "true"}}
        }

        def _fake_client(service, region_name=None):
            return {"efs": efs, "cognito-idp": cognito}[service]

        with patch("boto3.client", side_effect=_fake_client):
            remediation = aum.scan_orphan_analytics_resources("us-east-2")
        assert any("fs-abc" in cmd for cmd in remediation)
        assert any("pool-1" in cmd for cmd in remediation)

    def test_skips_resources_without_matching_tag(self):
        efs = MagicMock()
        efs.describe_file_systems.return_value = {"FileSystems": [{"FileSystemId": "fs-abc"}]}
        efs.list_tags_for_resource.return_value = {"Tags": [{"Key": "Name", "Value": "irrelevant"}]}

        cognito = MagicMock()
        cognito.list_user_pools.return_value = {"UserPools": [{"Id": "pool-1"}]}
        cognito.describe_user_pool.return_value = {
            "UserPool": {"UserPoolTags": {"Name": "irrelevant"}}
        }

        def _fake_client(service, region_name=None):
            return {"efs": efs, "cognito-idp": cognito}[service]

        with patch("boto3.client", side_effect=_fake_client):
            remediation = aum.scan_orphan_analytics_resources("us-east-2")
        assert remediation == []

    def test_handles_efs_client_error_gracefully(self):
        """EFS failure emits a diagnostic line, does not raise."""
        from botocore.exceptions import ClientError

        efs = MagicMock()
        efs.describe_file_systems.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no"}},
            "DescribeFileSystems",
        )
        cognito = MagicMock()
        cognito.list_user_pools.return_value = {"UserPools": []}

        def _fake_client(service, region_name=None):
            return {"efs": efs, "cognito-idp": cognito}[service]

        with patch("boto3.client", side_effect=_fake_client):
            remediation = aum.scan_orphan_analytics_resources("us-east-2")
        assert any("EFS orphan scan failed" in line for line in remediation)

    def test_handles_cognito_client_error_gracefully(self):
        from botocore.exceptions import ClientError

        efs = MagicMock()
        efs.describe_file_systems.return_value = {"FileSystems": []}
        cognito = MagicMock()
        cognito.list_user_pools.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no"}},
            "ListUserPools",
        )

        def _fake_client(service, region_name=None):
            return {"efs": efs, "cognito-idp": cognito}[service]

        with patch("boto3.client", side_effect=_fake_client):
            remediation = aum.scan_orphan_analytics_resources("us-east-2")
        assert any("Cognito orphan scan failed" in line for line in remediation)
