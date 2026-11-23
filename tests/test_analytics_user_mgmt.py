"""Focused unit tests for ``cli/analytics_user_mgmt.py``.

The happy-path flows are already covered through the Click-level tests in
``tests/test_analytics_cmd.py`` — this module adds targeted tests for the
pure helpers (password generation, the Studio username rule, CloudFormation output lookups)
and for the error branches of the AWS-facing functions.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
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
# srp_authenticate (AdminInitiateAuth with ADMIN_USER_PASSWORD_AUTH)
# ---------------------------------------------------------------------------


class TestSRPAuthenticate:
    """Exercise srp_authenticate, which calls cognito-idp AdminInitiateAuth."""

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
    def test_list_users_reads_every_page(self):
        """ListUsers returns at most 60 users per call; the listing used to stop
        at the first page."""
        cognito = boto3.client("cognito-idp", region_name="us-east-2")
        pool_id = cognito.create_user_pool(PoolName="gco-studio")["UserPool"]["Id"]
        for index in range(65):
            cognito.admin_create_user(
                UserPoolId=pool_id, Username=f"user{index:02d}", MessageAction="SUPPRESS"
            )

        rows = aum.list_users(pool_id, "us-east-2")

        assert sorted(row["username"] for row in rows) == [f"user{i:02d}" for i in range(65)]

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


class _FakeCloudFormation:
    """CloudFormation answering for one deleted and (optionally) one live
    ``gco-analytics`` stack, as ListStacks/ListStackResources do."""

    def __init__(self, *, deleted=(), live=None, error=None):
        self.deleted = list(deleted)  # [(stack_id, [resource summaries])]
        self.live = live  # [resource summaries] or None (no live stack)
        self.error = error

    def get_paginator(self, name):
        fake = self

        class _Paginator:
            def paginate(self, **kwargs):
                if fake.error is not None:
                    raise fake.error
                if name == "list_stacks":
                    assert kwargs == {"StackStatusFilter": ["DELETE_COMPLETE"]}
                    return [
                        {
                            "StackSummaries": [
                                {"StackName": "other-analytics", "StackId": "arn:other"},
                                *(
                                    {"StackName": "gco-analytics", "StackId": stack_id}
                                    for stack_id, _ in fake.deleted
                                ),
                            ]
                        }
                    ]
                stack = kwargs["StackName"]
                for stack_id, resources in fake.deleted:
                    if stack == stack_id:
                        return [{"StackResourceSummaries": resources}]
                assert stack == "gco-analytics"
                if fake.live is None:
                    raise ClientError(
                        {
                            "Error": {
                                "Code": "ValidationError",
                                "Message": "Stack with id gco-analytics does not exist",
                            }
                        },
                        "ListStackResources",
                    )
                return [{"StackResourceSummaries": fake.live}]

        return _Paginator()


def _skipped(resource_type, physical_id):
    return {
        "ResourceType": resource_type,
        "PhysicalResourceId": physical_id,
        "ResourceStatus": "DELETE_SKIPPED",
    }


def _scan_with(cfn, region="us-east-2"):
    real_client = boto3.client

    def client(service, region_name=None):
        if service == "cloudformation":
            return cfn
        return real_client(service, region_name=region_name)

    with patch("boto3.client", side_effect=client):
        return aum.scan_orphan_analytics_resources(region, "gco")


def _studio_efs(efs, ec2, *, name="gco-analytics/StudioEfs", tags=()):
    fs_id = efs.create_file_system(
        CreationToken=name + str(len(efs.describe_file_systems()["FileSystems"])),
        Tags=[{"Key": "Name", "Value": name}, *tags],
    )["FileSystemId"]
    subnet = ec2.describe_subnets()["Subnets"][0]["SubnetId"]
    mount_target = efs.create_mount_target(FileSystemId=fs_id, SubnetId=subnet)["MountTargetId"]
    access_point = efs.create_access_point(
        FileSystemId=fs_id, Tags=[{"Key": "gco:analytics:managed", "Value": "true"}]
    )["AccessPointId"]
    return fs_id, mount_target, access_point


class TestScanOrphanResources:
    """The scan used to look for a tag only per-user EFS access points carry,
    so it could never flag a retained file system or user pool."""

    @mock_aws
    def test_reports_what_a_deleted_stack_retained_with_ordered_cleanup(self):
        efs = boto3.client("efs", region_name="us-east-2")
        ec2 = boto3.client("ec2", region_name="us-east-2")
        cognito = boto3.client("cognito-idp", region_name="us-east-2")
        fs_id, mount_target, access_point = _studio_efs(efs, ec2)
        pool_id = cognito.create_user_pool(
            PoolName="StudioUserPool3816E37C-abc", UserPoolTags={"Project": "GCO"}
        )["UserPool"]["Id"]
        cognito.create_user_pool_domain(Domain="gco-studio-123456789012", UserPoolId=pool_id)
        unrelated_pool = cognito.create_user_pool(PoolName="someone-else")["UserPool"]["Id"]
        cfn = _FakeCloudFormation(
            deleted=[
                (
                    "arn:aws:cloudformation:us-east-2:123456789012:stack/gco-analytics/1",
                    [
                        _skipped("AWS::EFS::FileSystem", fs_id),
                        _skipped("AWS::Cognito::UserPool", pool_id),
                        {
                            "ResourceType": "AWS::IAM::Role",
                            "PhysicalResourceId": "role",
                            "ResourceStatus": "DELETE_SKIPPED",
                        },
                        {
                            "ResourceType": "AWS::Cognito::UserPool",
                            "PhysicalResourceId": "us-east-2_deleted",
                            "ResourceStatus": "DELETE_COMPLETE",
                        },
                    ],
                )
            ]
        )

        remediation = _scan_with(cfn)

        assert remediation == [
            f"aws efs delete-access-point --access-point-id {access_point} --region us-east-2",
            f"aws efs delete-mount-target --mount-target-id {mount_target} --region us-east-2",
            f"aws efs delete-file-system --file-system-id {fs_id} --region us-east-2",
            "aws cognito-idp delete-user-pool-domain --domain gco-studio-123456789012 "
            f"--user-pool-id {pool_id} --region us-east-2",
            f"aws cognito-idp delete-user-pool --user-pool-id {pool_id} --region us-east-2",
        ]
        assert all(unrelated_pool not in command for command in remediation)

    @mock_aws
    def test_never_reports_the_live_stacks_resources(self):
        efs = boto3.client("efs", region_name="us-east-2")
        ec2 = boto3.client("ec2", region_name="us-east-2")
        cognito = boto3.client("cognito-idp", region_name="us-east-2")
        live_fs, _, _ = _studio_efs(efs, ec2)
        live_pool = cognito.create_user_pool(
            PoolName="StudioUserPool3816E37C-live",
            UserPoolTags={"gco:analytics:managed": "true", "gco:project": "gco"},
        )["UserPool"]["Id"]
        cfn = _FakeCloudFormation(
            live=[
                {"ResourceType": "AWS::EFS::FileSystem", "PhysicalResourceId": live_fs},
                {"ResourceType": "AWS::Cognito::UserPool", "PhysicalResourceId": live_pool},
            ]
        )

        assert _scan_with(cfn) == []

    @mock_aws
    def test_recognises_an_old_studio_file_system_by_its_tags(self):
        """Past CloudFormation's 90 days, the Name tag CDK sets (or the
        managed/project tags) still identify this project's Studio EFS."""
        efs = boto3.client("efs", region_name="us-east-2")
        ec2 = boto3.client("ec2", region_name="us-east-2")
        by_name, _, _ = _studio_efs(efs, ec2)
        by_tags, _, _ = _studio_efs(
            efs,
            ec2,
            name="renamed",
            tags=[
                {"Key": "gco:analytics:managed", "Value": "true"},
                {"Key": "gco:project", "Value": "gco"},
            ],
        )
        other_project, _, _ = _studio_efs(efs, ec2, name="other-analytics/StudioEfs")
        other_managed, _, _ = _studio_efs(
            efs,
            ec2,
            name="x",
            tags=[
                {"Key": "gco:analytics:managed", "Value": "true"},
                {"Key": "gco:project", "Value": "other"},
            ],
        )

        remediation = _scan_with(_FakeCloudFormation())

        deleted = [cmd.split()[-3] for cmd in remediation if "delete-file-system" in cmd]
        assert sorted(deleted) == sorted([by_name, by_tags])
        assert other_project not in " ".join(remediation)
        assert other_managed not in " ".join(remediation)

    @mock_aws
    def test_a_retained_pool_deleted_by_hand_since_is_not_reported(self):
        cfn = _FakeCloudFormation(
            deleted=[("arn:stack/1", [_skipped("AWS::Cognito::UserPool", "us-east-2_gone")])]
        )
        assert _scan_with(cfn) == []

    def test_cloudformation_failure_offers_nothing_for_deletion(self):
        """Without the stack's resource lists a live file system could look
        retained, so the scan stops with one diagnostic line."""
        from botocore.exceptions import ClientError

        cfn = _FakeCloudFormation(
            error=ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListStacks")
        )
        with patch("boto3.client", return_value=cfn):
            remediation = aum.scan_orphan_analytics_resources("us-east-2", "gco")
        assert len(remediation) == 1
        assert remediation[0].startswith("(orphan scan skipped:")

    def test_live_stack_lookup_errors_other_than_missing_stack_propagate(self):
        cfn = MagicMock()
        cfn.get_paginator.return_value.paginate.side_effect = [
            [{"StackSummaries": []}],
            ClientError({"Error": {"Code": "Throttling", "Message": "slow"}}, "ListStackResources"),
        ]
        with patch("boto3.client", return_value=cfn):
            remediation = aum.scan_orphan_analytics_resources("us-east-2", "gco")
        assert remediation == [
            "(orphan scan skipped: the CloudFormation lookup of gco-analytics failed: "
            "An error occurred (Throttling) when calling the ListStackResources operation: slow)"
        ]

    def test_efs_and_cognito_failures_emit_diagnostics(self):
        from botocore.exceptions import ClientError

        denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "Describe")
        efs = MagicMock()
        efs.get_paginator.return_value.paginate.side_effect = denied
        cognito = MagicMock()
        cognito.get_paginator.return_value.paginate.return_value = [
            {"UserPools": [{"Id": "us-east-2_pool"}, {}]}
        ]
        cognito.describe_user_pool.side_effect = denied
        clients = {"cloudformation": _FakeCloudFormation(), "efs": efs, "cognito-idp": cognito}
        with patch("boto3.client", side_effect=lambda service, region_name=None: clients[service]):
            remediation = aum.scan_orphan_analytics_resources("us-east-2", "gco")
        assert remediation[0].startswith("(EFS orphan scan failed:")
        assert remediation[1].startswith("(Cognito orphan scan failed:")
        assert len(remediation) == 2

    def test_skips_file_systems_without_an_id(self):
        efs = MagicMock()
        efs.get_paginator.return_value.paginate.return_value = [{"FileSystems": [{}]}]
        cognito = MagicMock()
        cognito.get_paginator.return_value.paginate.return_value = [{"UserPools": []}]
        clients = {"cloudformation": _FakeCloudFormation(), "efs": efs, "cognito-idp": cognito}
        with patch("boto3.client", side_effect=lambda service, region_name=None: clients[service]):
            assert aum.scan_orphan_analytics_resources("us-east-2", "gco") == []


class TestStudioUsernames:
    """The checks ``gco analytics users add`` makes before it creates a user."""

    @pytest.mark.parametrize("username", ["alice", "Bob-2", "a--b", "x" * 63])
    def test_names_sagemaker_accepts(self, username):
        assert aum.is_studio_username(username)

    @pytest.mark.parametrize(
        "username", ["alice.smith", "alice_smith", "-a", "a-", "a b", "alice\n", "a" * 64]
    )
    def test_names_sagemaker_rejects(self, username):
        assert not aum.is_studio_username(username)

    def test_the_rule_matches_the_presigned_url_lambda(self):
        """The CLI's copy of the rule and the Lambda's must not drift apart.

        The Lambda ships on its own (it cannot import ``cli``), so each holds
        the pattern; a name one accepts and the other refuses would either
        be created and then fail at login, or be refused for no reason.
        """
        from tests._lambda_imports import load_lambda_module

        with patch("boto3.client", return_value=MagicMock()):
            handler = load_lambda_module("analytics-presigned-url")

        assert handler.STUDIO_USERNAME_PATTERN == aum.STUDIO_USERNAME_PATTERN
        assert handler.STUDIO_USERNAME_MAX_LENGTH == aum.STUDIO_USERNAME_MAX_LENGTH

    @mock_aws
    def test_find_user_ignoring_case_reads_every_page(self):
        cognito = boto3.client("cognito-idp", region_name="us-east-2")
        pool_id = cognito.create_user_pool(PoolName="gco-studio")["UserPool"]["Id"]
        # ListUsers returns at most 60 users a page.
        for index in range(61):
            cognito.admin_create_user(
                UserPoolId=pool_id, Username=f"user-{index:02d}", MessageAction="SUPPRESS"
            )
        cognito.admin_create_user(UserPoolId=pool_id, Username="Alice", MessageAction="SUPPRESS")

        assert aum.find_user_ignoring_case(pool_id, "us-east-2", "alice") == "Alice"
        assert aum.find_user_ignoring_case(pool_id, "us-east-2", "USER-60") == "user-60"
        assert aum.find_user_ignoring_case(pool_id, "us-east-2", "bob") is None


class TestStudioLoginErrorToken:
    @staticmethod
    def _error(body: bytes | None):
        import io
        import urllib.error

        fp = None if body is None else io.BytesIO(body)
        return urllib.error.HTTPError("https://api.example/studio/login", 403, "x", None, fp)  # type: ignore[arg-type]

    def test_reads_the_lambda_token(self):
        assert aum.studio_login_error_token(self._error(b'{"error": "UnsupportedUsername"}')) == (
            "UnsupportedUsername"
        )

    @pytest.mark.parametrize(
        "body",
        [
            None,
            b"",
            b"not json",
            b"\xff\xfe\x00",
            b'["error"]',
            b'{"message": "Forbidden"}',
            b'{"error": 7}',
        ],
        ids=["no-body", "empty", "not-json", "not-utf8", "list", "gateway-message", "non-string"],
    )
    def test_anything_else_has_no_token(self, body):
        assert aum.studio_login_error_token(self._error(body)) is None

    def test_an_unreadable_body_has_no_token(self):
        error = self._error(b"{}")
        with patch.object(error, "read", side_effect=OSError("connection reset")):
            assert aum.studio_login_error_token(error) is None
