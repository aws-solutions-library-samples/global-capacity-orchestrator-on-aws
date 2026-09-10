"""Tests for the analytics presigned-URL Lambda handler.

Test strategy:

* The handler creates module-level boto3 clients at import time
  (``sagemaker = boto3.client("sagemaker")`` and similarly for ``efs``).
  We use ``unittest.mock.patch("boto3.client")`` wrapped around the
  :func:`tests._lambda_imports.load_lambda_module` call so the clients
  are replaced with ``MagicMock`` instances before any environment
  variables are read.
* Each test re-imports the handler via the fixture, gets a fresh pair
  of client mocks, and drives the handler with a synthetic API Gateway
  proxy event shaped like what Cognito would send.
* The Hypothesis property only exercises the happy path under stubbed
  boto3 clients, so per-example runtime is tiny.
"""

from __future__ import annotations

import json
import os
import string
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests._lambda_imports import load_lambda_module

# ---------------------------------------------------------------------------
# Constants that must match the handler's env-var defaults / error tokens.
# ---------------------------------------------------------------------------

_STUDIO_DOMAIN_ID = "d-abc123xyz"
_STUDIO_EFS_ID = "fs-0123456789abcdef0"
_SAGEMAKER_EXECUTION_ROLE_ARN = (
    "arn:aws:iam::123456789012:role/AmazonSageMaker-gco-analytics-exec-us-east-2"
)
_URL_EXPIRES_SECONDS = "300"
_SESSION_EXPIRES_SECONDS = "43200"

_HANDLER_ENV = {
    "STUDIO_DOMAIN_ID": _STUDIO_DOMAIN_ID,
    "STUDIO_EFS_ID": _STUDIO_EFS_ID,
    "SAGEMAKER_EXECUTION_ROLE_ARN": _SAGEMAKER_EXECUTION_ROLE_ARN,
    "URL_EXPIRES_SECONDS": _URL_EXPIRES_SECONDS,
    "SESSION_EXPIRES_SECONDS": _SESSION_EXPIRES_SECONDS,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_event(username: str | None = "alice") -> dict[str, Any]:
    """Build a minimal API Gateway proxy event with a Cognito authorizer.

    When ``username`` is ``None``, the ``claims`` dict omits both
    ``cognito:username`` and ``username`` so the handler's 401 path fires.
    """
    claims: dict[str, Any] = {}
    if username is not None:
        claims["cognito:username"] = username
    return {
        "requestContext": {
            "authorizer": {
                "claims": claims,
            }
        }
    }


@pytest.fixture
def handler_module(monkeypatch: pytest.MonkeyPatch):
    """Import the Lambda handler under patched ``boto3.client`` + env vars.

    Yields ``(handler, sagemaker_client, efs_client)`` tuples. The
    sagemaker/efs client mocks are distinct ``MagicMock`` instances so
    tests can assert call sequences on one without interference from the
    other.
    """
    for key, value in _HANDLER_ENV.items():
        monkeypatch.setenv(key, value)

    sagemaker_mock = MagicMock(name="sagemaker_client")
    efs_mock = MagicMock(name="efs_client")

    def _client_factory(service_name: str, *args: Any, **kwargs: Any) -> MagicMock:
        if service_name == "sagemaker":
            return sagemaker_mock
        if service_name == "efs":
            return efs_mock
        raise AssertionError(f"Unexpected boto3 client: {service_name!r}")

    with patch("boto3.client", side_effect=_client_factory):
        handler = load_lambda_module("analytics-presigned-url")

    # Defaults for the "everything works" case. Individual tests override
    # these as needed.
    sagemaker_mock.list_domains.return_value = {
        "Domains": [
            {"DomainName": "gco-analytics-us-east-2", "DomainId": _STUDIO_DOMAIN_ID},
        ],
    }
    sagemaker_mock.describe_user_profile.return_value = {
        "UserProfileName": "alice",
        "DomainId": _STUDIO_DOMAIN_ID,
        "Status": "InService",
    }
    sagemaker_mock.create_presigned_domain_url.return_value = {
        "AuthorizedUrl": "https://d-abc123xyz.studio.us-east-2.sagemaker.aws/auth?token=xyz",
    }
    efs_mock.describe_access_points.return_value = {
        "AccessPoints": [
            {
                "AccessPointArn": (
                    "arn:aws:elasticfilesystem:us-east-2:123456789012:"
                    "access-point/fsap-0123456789abcdef0"
                ),
                "RootDirectory": {"Path": "/home/alice"},
            }
        ]
    }

    yield handler, sagemaker_mock, efs_mock


# ---------------------------------------------------------------------------
# Happy path + error-path unit tests
# ---------------------------------------------------------------------------


class TestPresignedUrlHappyPath:
    """Happy-path invariants for the Lambda handler."""

    def test_happy_path_returns_200_with_url(self, handler_module) -> None:
        handler, sagemaker_mock, _ = handler_module

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 200
        assert response["headers"]["Content-Type"] == "application/json"
        body = json.loads(response["body"])
        assert isinstance(body["url"], str)
        assert body["url"].startswith("https://")
        assert isinstance(body["expires_in"], int)
        assert body["expires_in"] > 0
        # Session-expiration + URL-expiration kwargs are passed through.
        sagemaker_mock.create_presigned_domain_url.assert_called_once_with(
            DomainId=_STUDIO_DOMAIN_ID,
            UserProfileName="alice",
            SessionExpirationDurationInSeconds=int(_SESSION_EXPIRES_SECONDS),
            ExpiresInSeconds=int(_URL_EXPIRES_SECONDS),
        )

    def test_falls_back_to_plain_username_claim(self, handler_module) -> None:
        """If the token lacks ``cognito:username``, plain ``username`` is used."""
        handler, sagemaker_mock, _ = handler_module

        event = {"requestContext": {"authorizer": {"claims": {"username": "bob"}}}}
        response = handler.lambda_handler(event, None)

        assert response["statusCode"] == 200
        call_kwargs = sagemaker_mock.create_presigned_domain_url.call_args.kwargs
        assert call_kwargs["UserProfileName"] == "bob"


class TestPresignedUrlCreatesResources:
    """Lazy-provisioning branches for user profile + EFS access point."""

    def test_creates_user_profile_if_missing_returns_202(self, handler_module) -> None:
        handler, sagemaker_mock, _ = handler_module

        # Profile doesn't exist -- triggers creation, returns 202.
        sagemaker_mock.describe_user_profile.side_effect = ClientError(
            error_response={
                "Error": {
                    "Code": "ValidationException",
                    "Message": "User profile not found",
                }
            },
            operation_name="DescribeUserProfile",
        )

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body == {"status": "provisioning"}
        sagemaker_mock.create_user_profile.assert_called_once()
        call_kwargs = sagemaker_mock.create_user_profile.call_args.kwargs
        assert call_kwargs["DomainId"] == _STUDIO_DOMAIN_ID
        assert call_kwargs["UserProfileName"] == "alice"
        user_settings = call_kwargs["UserSettings"]
        assert user_settings["ExecutionRole"] == _SAGEMAKER_EXECUTION_ROLE_ARN
        efs_configs = user_settings["CustomFileSystemConfigs"]
        assert efs_configs[0]["EFSFileSystemConfig"]["FileSystemId"] == _STUDIO_EFS_ID
        assert efs_configs[0]["EFSFileSystemConfig"]["FileSystemPath"] == "/home/alice"
        # No presigned URL minted on a 202.
        sagemaker_mock.create_presigned_domain_url.assert_not_called()

    def test_creates_user_profile_on_resource_not_found(self, handler_module) -> None:
        """``ResourceNotFound`` is also treated as 'profile missing'."""
        handler, sagemaker_mock, _ = handler_module

        sagemaker_mock.describe_user_profile.side_effect = ClientError(
            error_response={"Error": {"Code": "ResourceNotFound", "Message": "not found"}},
            operation_name="DescribeUserProfile",
        )

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 202
        sagemaker_mock.create_user_profile.assert_called_once()

    def test_pending_profile_returns_202(self, handler_module) -> None:
        """If the profile exists but is Pending, return 202 without creating."""
        handler, sagemaker_mock, _ = handler_module

        sagemaker_mock.describe_user_profile.return_value = {
            "UserProfileName": "alice",
            "DomainId": _STUDIO_DOMAIN_ID,
            "Status": "Pending",
        }

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body == {"status": "provisioning"}
        sagemaker_mock.create_user_profile.assert_not_called()

    def test_failed_profile_deletes_and_recreates(self, handler_module) -> None:
        """If the profile is Failed, delete it, recreate, and return 202."""
        handler, sagemaker_mock, _ = handler_module

        sagemaker_mock.describe_user_profile.return_value = {
            "UserProfileName": "alice",
            "DomainId": _STUDIO_DOMAIN_ID,
            "Status": "Failed",
            "FailureReason": "Permission error",
        }

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 202
        sagemaker_mock.delete_user_profile.assert_called_once_with(
            DomainId=_STUDIO_DOMAIN_ID,
            UserProfileName="alice",
        )
        sagemaker_mock.create_user_profile.assert_called_once()

    def test_creates_access_point_if_missing(self, handler_module) -> None:
        handler, _, efs_mock = handler_module

        # No access points yet for this EFS; handler should create one.
        efs_mock.describe_access_points.return_value = {"AccessPoints": []}
        efs_mock.create_access_point.return_value = {
            "AccessPointArn": (
                "arn:aws:elasticfilesystem:us-east-2:123456789012:access-point/fsap-newabc"
            )
        }

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 200
        efs_mock.create_access_point.assert_called_once()
        call_kwargs = efs_mock.create_access_point.call_args.kwargs
        assert call_kwargs["FileSystemId"] == _STUDIO_EFS_ID
        # The derived uid/gid pair must be equal and >= 100000.
        uid = call_kwargs["PosixUser"]["Uid"]
        gid = call_kwargs["PosixUser"]["Gid"]
        assert uid == gid
        assert uid >= 100000
        # Match what _derive_posix_ids returns for the same username.
        assert (uid, gid) == handler._derive_posix_ids("alice")
        # Root-dir creation info matches the POSIX owner.
        root_dir = call_kwargs["RootDirectory"]
        assert root_dir["Path"] == "/home/alice"
        assert root_dir["CreationInfo"]["OwnerUid"] == uid
        assert root_dir["CreationInfo"]["Permissions"] == "0700"


class TestPresignedUrlErrorPaths:
    """Error-path assertions — auth failures, missing domain, boto3 errors."""

    def test_missing_cognito_claim_returns_401(self, handler_module) -> None:
        handler, sagemaker_mock, efs_mock = handler_module

        response = handler.lambda_handler(_make_event(username=None), None)

        assert response["statusCode"] == 401
        body = json.loads(response["body"])
        assert body == {"error": "MissingCognitoClaim"}
        # No SageMaker or EFS calls for an unauthenticated request.
        sagemaker_mock.list_domains.assert_not_called()
        sagemaker_mock.create_presigned_domain_url.assert_not_called()
        efs_mock.describe_access_points.assert_not_called()

    def test_missing_authorizer_block_returns_401(self, handler_module) -> None:
        """A bare event with no requestContext still fails cleanly."""
        handler, sagemaker_mock, _ = handler_module

        response = handler.lambda_handler({}, None)

        assert response["statusCode"] == 401
        body = json.loads(response["body"])
        assert body == {"error": "MissingCognitoClaim"}
        sagemaker_mock.list_domains.assert_not_called()

    def test_domain_not_found_returns_404(self, handler_module) -> None:
        handler, sagemaker_mock, _ = handler_module

        # Override the module-level STUDIO_DOMAIN_ID to empty so the handler
        # falls back to _resolve_domain_id, which returns None when no
        # matching domain is found.
        handler.STUDIO_DOMAIN_ID = ""
        sagemaker_mock.list_domains.return_value = {
            "Domains": [
                {"DomainName": "some-other-domain", "DomainId": "d-xxxxxxxx"},
            ],
        }

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body == {"error": "SagemakerDomainNotFound"}
        sagemaker_mock.create_presigned_domain_url.assert_not_called()

    def test_create_presigned_url_raise_returns_500_with_opaque_token(self, handler_module) -> None:
        """500 body contains only the opaque token, never the message."""
        handler, sagemaker_mock, _ = handler_module

        secret_message = "internal-detail-do-not-leak-123-accountid-credentials-leak"
        sagemaker_mock.create_presigned_domain_url.side_effect = ClientError(
            error_response={
                "Error": {
                    "Code": "InternalServerError",
                    "Message": secret_message,
                }
            },
            operation_name="CreatePresignedDomainUrl",
        )

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body == {"error": "PresignedUrlGenerationFailed"}
        # The raw exception text must not appear anywhere in the
        # response payload — not the body, not the headers.
        assert secret_message not in response["body"]
        assert secret_message not in json.dumps(response["headers"])


# ---------------------------------------------------------------------------
# Pure-helper coverage
# ---------------------------------------------------------------------------


class TestDerivePosixIds:
    """Unit tests for the pure POSIX-id derivation helper."""

    def test_derive_posix_ids_returns_in_valid_range(self, handler_module) -> None:
        handler, _, _ = handler_module

        uid, gid = handler._derive_posix_ids("alice")

        assert uid == gid
        assert uid >= 100000
        # 32-bit signed positive int ceiling.
        assert uid < 2**31

    def test_derive_posix_ids_is_deterministic(self, handler_module) -> None:
        """Same username always produces the same (uid, gid)."""
        handler, _, _ = handler_module

        for _ in range(3):
            assert handler._derive_posix_ids("alice") == handler._derive_posix_ids("alice")

    def test_derive_posix_ids_differs_across_usernames(self, handler_module) -> None:
        """Different usernames produce different uids (not a contract, but a
        reasonable collision-resistance smoke test)."""
        handler, _, _ = handler_module

        uid_alice, _ = handler._derive_posix_ids("alice")
        uid_bob, _ = handler._derive_posix_ids("bob")
        assert uid_alice != uid_bob


# ---------------------------------------------------------------------------
# Hypothesis response-shape property
# ---------------------------------------------------------------------------


_USERNAME_ALPHABET = string.ascii_letters + string.digits + "_-"

_username_strategy = st.text(
    min_size=1,
    max_size=64,
    alphabet=_USERNAME_ALPHABET,
)

_claim_strategy: st.SearchStrategy[dict[str, Any]] = st.fixed_dictionaries(
    {"cognito:username": _username_strategy},
    optional={
        "cognito:groups": st.lists(
            st.text(min_size=1, max_size=16, alphabet=string.ascii_letters),
            max_size=4,
        ),
        "email": st.one_of(
            st.none(),
            st.emails(),
        ),
        "sub": st.uuids().map(str),
    },
)


class TestPresignedUrlResponseShapeProperty:
    """Hypothesis property for the Lambda response shape.

    For any valid Cognito claim payload, the Lambda's happy-path response
    body SHALL be valid JSON with keys ``url`` (string) and
    ``expires_in`` (positive int).
    """

    @settings(
        max_examples=30,
        deadline=5000,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
    )
    @given(claims=_claim_strategy)
    def test_response_body_is_valid_json_with_url_and_positive_expires(
        self, handler_module, claims: dict[str, Any]
    ) -> None:
        handler, sagemaker_mock, efs_mock = handler_module

        # Stub the domain + profile + access-point flow so every example
        # takes the happy path. The per-username access-point path
        # uses string interpolation on ``claims["cognito:username"]``
        # so we synthesize the matching AP record.
        username = claims["cognito:username"]
        sagemaker_mock.list_domains.return_value = {
            "Domains": [
                {"DomainName": "gco-analytics-us-east-2", "DomainId": _STUDIO_DOMAIN_ID},
            ],
        }
        sagemaker_mock.describe_user_profile.return_value = {
            "UserProfileName": username,
            "DomainId": _STUDIO_DOMAIN_ID,
            "Status": "InService",
        }
        sagemaker_mock.create_presigned_domain_url.return_value = {
            "AuthorizedUrl": (
                f"https://d-propcheck.studio.us-east-2.sagemaker.aws/auth?user={username}"
            ),
        }
        efs_mock.describe_access_points.return_value = {
            "AccessPoints": [
                {
                    "AccessPointArn": (
                        "arn:aws:elasticfilesystem:us-east-2:123456789012:"
                        "access-point/fsap-propcheck"
                    ),
                    "RootDirectory": {"Path": f"/home/{username}"},
                }
            ]
        }

        event = {"requestContext": {"authorizer": {"claims": claims}}}
        response = handler.lambda_handler(event, None)

        assert response["statusCode"] == 200
        # Body is valid JSON.
        body = json.loads(response["body"])
        assert isinstance(body, dict)
        # Key shape.
        assert set(body.keys()) == {"url", "expires_in"}
        # Types.
        assert isinstance(body["url"], str)
        assert body["url"]  # non-empty
        assert isinstance(body["expires_in"], int)
        # bool is a subclass of int in Python; explicitly reject it.
        assert not isinstance(body["expires_in"], bool)
        assert body["expires_in"] > 0


# ---------------------------------------------------------------------------
# Module-scope sanity check
# ---------------------------------------------------------------------------


def test_handler_module_exposes_pure_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_parse_claims``, ``_derive_posix_ids``, ``_format_success``, and
    ``_format_error`` are factored out as pure helpers.
    """
    for key, value in _HANDLER_ENV.items():
        monkeypatch.setenv(key, value)

    with patch("boto3.client", return_value=MagicMock()):
        handler = load_lambda_module("analytics-presigned-url")

    assert callable(handler._parse_claims)
    assert callable(handler._derive_posix_ids)
    assert callable(handler._format_success)
    assert callable(handler._format_provisioning)
    assert callable(handler._format_error)

    # _format_success returns the documented proxy-response shape.
    ok = handler._format_success("https://example.com", 42)
    assert ok["statusCode"] == 200
    assert ok["headers"]["Content-Type"] == "application/json"
    assert json.loads(ok["body"]) == {"url": "https://example.com", "expires_in": 42}

    # _format_error returns the documented error shape.
    err = handler._format_error(401, "MissingCognitoClaim")
    assert err["statusCode"] == 401
    assert json.loads(err["body"]) == {"error": "MissingCognitoClaim"}

    # _parse_claims handles malformed events without raising.
    assert handler._parse_claims({}) == {}
    assert handler._parse_claims({"requestContext": None}) == {}
    assert handler._parse_claims(
        {"requestContext": {"authorizer": {"claims": {"cognito:username": "x"}}}}
    ) == {"cognito:username": "x"}

    # Env vars were read at import time.
    assert os.environ["STUDIO_DOMAIN_ID"] == _STUDIO_DOMAIN_ID


# ---------------------------------------------------------------------------
# Remaining branches: malformed events, pagination, and boto3 error codes
# ---------------------------------------------------------------------------


class TestParseClaimsShapes:
    @pytest.mark.parametrize(
        "event",
        [
            "not an event",
            {"requestContext": {"authorizer": "not a dict"}},
            {"requestContext": {"authorizer": {"claims": ["not", "a", "dict"]}}},
        ],
        ids=["non-dict-event", "non-dict-authorizer", "non-dict-claims"],
    )
    def test_every_malformed_layer_yields_no_claims(self, handler_module, event) -> None:
        handler, _, _ = handler_module
        assert handler._parse_claims(event) == {}


class TestResolveDomainId:
    def test_follows_pagination_to_the_matching_domain(self, handler_module) -> None:
        handler, sagemaker_mock, _ = handler_module
        sagemaker_mock.list_domains.side_effect = [
            {"Domains": [{"DomainName": "other", "DomainId": "d-other"}], "NextToken": "p2"},
            {"Domains": [{"DomainName": "gco-analytics", "DomainId": "d-target"}]},
        ]

        assert handler._resolve_domain_id("gco-analytics") == "d-target"
        assert sagemaker_mock.list_domains.call_args_list[1].kwargs == {"NextToken": "p2"}

    def test_matching_domain_without_an_id_is_treated_as_missing(self, handler_module) -> None:
        handler, sagemaker_mock, _ = handler_module
        sagemaker_mock.list_domains.return_value = {"Domains": [{"DomainName": "gco-analytics"}]}

        assert handler._resolve_domain_id("gco-analytics") is None


class TestEnsureUserProfileErrorCodes:
    def test_unexpected_describe_error_propagates(self, handler_module) -> None:
        handler, sagemaker_mock, _ = handler_module
        sagemaker_mock.describe_user_profile.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "DescribeUserProfile"
        )

        with pytest.raises(ClientError, match="AccessDeniedException"):
            handler._ensure_user_profile(_STUDIO_DOMAIN_ID, "alice", _STUDIO_EFS_ID)
        sagemaker_mock.create_user_profile.assert_not_called()

    def test_failed_profile_that_cannot_be_deleted_is_still_recreated(self, handler_module) -> None:
        handler, sagemaker_mock, _ = handler_module
        sagemaker_mock.describe_user_profile.return_value = {
            "Status": "Failed",
            "FailureReason": "quota",
        }
        sagemaker_mock.delete_user_profile.side_effect = ClientError(
            {"Error": {"Code": "ResourceInUse", "Message": "busy"}}, "DeleteUserProfile"
        )

        status = handler._ensure_user_profile(_STUDIO_DOMAIN_ID, "alice", _STUDIO_EFS_ID)

        assert status == "Provisioning"
        sagemaker_mock.create_user_profile.assert_called_once()

    def test_concurrent_creation_is_tolerated(self, handler_module) -> None:
        handler, sagemaker_mock, _ = handler_module
        sagemaker_mock.create_user_profile.side_effect = ClientError(
            {"Error": {"Code": "ResourceInUse", "Message": "exists"}}, "CreateUserProfile"
        )

        handler._create_user_profile(_STUDIO_DOMAIN_ID, "alice", _STUDIO_EFS_ID)

    def test_other_creation_failures_propagate(self, handler_module) -> None:
        handler, sagemaker_mock, _ = handler_module
        sagemaker_mock.create_user_profile.side_effect = ClientError(
            {"Error": {"Code": "ResourceLimitExceeded", "Message": "quota"}}, "CreateUserProfile"
        )

        with pytest.raises(ClientError, match="ResourceLimitExceeded"):
            handler._create_user_profile(_STUDIO_DOMAIN_ID, "alice", _STUDIO_EFS_ID)


class TestEnsureAccessPointPagination:
    def test_existing_access_point_on_a_later_page_is_reused(self, handler_module) -> None:
        handler, _, efs_mock = handler_module
        arn = "arn:aws:elasticfilesystem:us-east-2:123456789012:access-point/fsap-alice"
        efs_mock.describe_access_points.side_effect = [
            {"AccessPoints": [{"RootDirectory": {"Path": "/home/bob"}}], "NextToken": "p2"},
            {"AccessPoints": [{"AccessPointArn": arn, "RootDirectory": {"Path": "/home/alice"}}]},
        ]

        assert handler._ensure_access_point("alice", _STUDIO_EFS_ID) == arn
        assert efs_mock.describe_access_points.call_args_list[1].kwargs["NextToken"] == "p2"
        efs_mock.create_access_point.assert_not_called()

    def test_exhausted_pages_lead_to_creation(self, handler_module) -> None:
        handler, _, efs_mock = handler_module
        efs_mock.describe_access_points.side_effect = [
            {"AccessPoints": [{"RootDirectory": {"Path": "/home/bob"}}], "NextToken": "p2"},
            {"AccessPoints": []},
        ]
        efs_mock.create_access_point.return_value = {"AccessPointArn": "arn:new"}

        assert handler._ensure_access_point("alice", _STUDIO_EFS_ID) == "arn:new"
        uid, gid = handler._derive_posix_ids("alice")
        assert efs_mock.create_access_point.call_args.kwargs["PosixUser"] == {
            "Uid": uid,
            "Gid": gid,
        }
