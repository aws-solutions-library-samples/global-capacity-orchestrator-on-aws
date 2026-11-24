"""Tests for the analytics presigned-URL Lambda handler.

Test strategy:

* The handler creates module-level boto3 clients at import time
  (``sagemaker = boto3.client("sagemaker")``, the only client it makes).
  We use ``unittest.mock.patch("boto3.client")`` wrapped around the
  :func:`tests._lambda_imports.load_lambda_module` call so the clients
  are replaced with ``MagicMock`` instances before any environment
  variables are read.
* Each test re-imports the handler via the fixture, gets a fresh pair
  of client mocks, and drives the handler with a synthetic API Gateway
  proxy event shaped like what Cognito would send.
* The Hypothesis properties exercise the happy path and the
  unsupported-username answer under stubbed boto3 clients, so
  per-example runtime is tiny.
"""

from __future__ import annotations

import json
import os
import re
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

# The caller's Cognito ``sub`` and another user's, plus the owner tag the
# handler writes on the profiles it creates.
_SUB = "11111111-2222-4333-8444-555555555555"
_OTHER_SUB = "99999999-8888-4777-8666-555555555555"
_OWNER_TAG_KEY = "gco:analytics:cognito-sub"
_PROFILE_ARN = "arn:aws:sagemaker:us-east-2:123456789012:user-profile/d-abc123xyz/alice"


def _owner_tags(sub: str = _SUB) -> dict[str, Any]:
    """A ``ListTags`` page holding the owner tag for ``sub``."""
    return {"Tags": [{"Key": _OWNER_TAG_KEY, "Value": sub}]}


def _profile(status: str, **extra: Any) -> dict[str, Any]:
    """A ``DescribeUserProfile`` answer for alice's profile in ``status``."""
    return {
        "UserProfileName": "alice",
        "UserProfileArn": _PROFILE_ARN,
        "DomainId": _STUDIO_DOMAIN_ID,
        "Status": status,
        **extra,
    }


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


def _make_event(username: str | None = "alice", sub: str | None = _SUB) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event with a Cognito authorizer.

    When ``username`` is ``None``, the ``claims`` dict omits both
    ``cognito:username`` and ``username`` so the handler's 401 path fires;
    ``sub=None`` omits the ``sub`` claim.
    """
    claims: dict[str, Any] = {}
    if username is not None:
        claims["cognito:username"] = username
    if sub is not None:
        claims["sub"] = sub
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

    Yields ``(handler, sagemaker_client)``. The handler may create only a
    SageMaker client: the per-user EFS access points earlier releases made
    at every login are gone, so an EFS client would be a regression.
    """
    for key, value in _HANDLER_ENV.items():
        monkeypatch.setenv(key, value)

    sagemaker_mock = MagicMock(name="sagemaker_client")

    def _client_factory(service_name: str, *args: Any, **kwargs: Any) -> MagicMock:
        if service_name == "sagemaker":
            return sagemaker_mock
        raise AssertionError(f"Unexpected boto3 client: {service_name!r}")

    with patch("boto3.client", side_effect=_client_factory):
        handler = load_lambda_module("analytics-presigned-url")

    # Defaults for the "everything works" case. Individual tests override
    # these as needed.
    sagemaker_mock.describe_user_profile.return_value = _profile("InService")
    sagemaker_mock.list_tags.return_value = _owner_tags()
    sagemaker_mock.create_presigned_domain_url.return_value = {
        "AuthorizedUrl": "https://d-abc123xyz.studio.us-east-2.sagemaker.aws/auth?token=xyz",
    }

    return handler, sagemaker_mock


# ---------------------------------------------------------------------------
# Happy path + error-path unit tests
# ---------------------------------------------------------------------------


class TestPresignedUrlHappyPath:
    """Happy-path invariants for the Lambda handler."""

    def test_happy_path_returns_200_with_url(self, handler_module) -> None:
        handler, sagemaker_mock = handler_module

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
        handler, sagemaker_mock = handler_module

        event = {"requestContext": {"authorizer": {"claims": {"username": "bob", "sub": _SUB}}}}
        response = handler.lambda_handler(event, None)

        assert response["statusCode"] == 200
        call_kwargs = sagemaker_mock.create_presigned_domain_url.call_args.kwargs
        assert call_kwargs["UserProfileName"] == "bob"


class TestPresignedUrlCreatesResources:
    """Lazy-provisioning branches for user profile + EFS access point."""

    def test_creates_user_profile_if_missing_returns_202(self, handler_module) -> None:
        handler, sagemaker_mock = handler_module

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
        # The new profile records who owns it.
        assert call_kwargs["Tags"] == [{"Key": _OWNER_TAG_KEY, "Value": _SUB}]
        # No presigned URL minted on a 202.
        sagemaker_mock.create_presigned_domain_url.assert_not_called()

    def test_creates_user_profile_on_resource_not_found(self, handler_module) -> None:
        """``ResourceNotFound`` is also treated as 'profile missing'."""
        handler, sagemaker_mock = handler_module

        sagemaker_mock.describe_user_profile.side_effect = ClientError(
            error_response={"Error": {"Code": "ResourceNotFound", "Message": "not found"}},
            operation_name="DescribeUserProfile",
        )

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 202
        sagemaker_mock.create_user_profile.assert_called_once()

    def test_pending_profile_returns_202(self, handler_module) -> None:
        """If the profile exists but is Pending, return 202 without creating."""
        handler, sagemaker_mock = handler_module

        sagemaker_mock.describe_user_profile.return_value = _profile("Pending")

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body == {"status": "provisioning"}
        sagemaker_mock.create_user_profile.assert_not_called()

    def test_failed_profile_deletes_and_recreates(self, handler_module) -> None:
        """If the profile is Failed, delete it, recreate, and return 202."""
        handler, sagemaker_mock = handler_module

        sagemaker_mock.describe_user_profile.return_value = _profile(
            "Failed", FailureReason="Permission error"
        )

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 202
        sagemaker_mock.delete_user_profile.assert_called_once_with(
            DomainId=_STUDIO_DOMAIN_ID,
            UserProfileName="alice",
        )
        sagemaker_mock.create_user_profile.assert_called_once()
        assert sagemaker_mock.create_user_profile.call_args.kwargs["Tags"] == [
            {"Key": _OWNER_TAG_KEY, "Value": _SUB}
        ]


class TestPresignedUrlErrorPaths:
    """Error-path assertions — auth failures, missing domain, boto3 errors."""

    def test_missing_cognito_claim_returns_401(self, handler_module) -> None:
        handler, sagemaker_mock = handler_module

        response = handler.lambda_handler(_make_event(username=None), None)

        assert response["statusCode"] == 401
        body = json.loads(response["body"])
        assert body == {"error": "MissingCognitoClaim"}
        # No SageMaker calls for an unauthenticated request.
        sagemaker_mock.list_domains.assert_not_called()
        sagemaker_mock.create_presigned_domain_url.assert_not_called()

    def test_missing_sub_claim_returns_401(self, handler_module) -> None:
        """Without ``sub`` the caller's ownership of a profile cannot be checked."""
        handler, sagemaker_mock = handler_module

        response = handler.lambda_handler(_make_event(sub=None), None)

        assert response["statusCode"] == 401
        assert json.loads(response["body"]) == {"error": "MissingCognitoClaim"}
        sagemaker_mock.describe_user_profile.assert_not_called()

    def test_missing_authorizer_block_returns_401(self, handler_module) -> None:
        """A bare event with no requestContext still fails cleanly."""
        handler, sagemaker_mock = handler_module

        response = handler.lambda_handler({}, None)

        assert response["statusCode"] == 401
        body = json.loads(response["body"])
        assert body == {"error": "MissingCognitoClaim"}
        sagemaker_mock.list_domains.assert_not_called()

    def test_domain_not_found_returns_404(self, handler_module) -> None:
        """Without STUDIO_DOMAIN_ID the request is a 404 with no SageMaker call.

        The old ListDomains fallback looked for a domain named "", so it
        could never find one; the stack always sets the ID.
        """
        handler, sagemaker_mock = handler_module

        handler.STUDIO_DOMAIN_ID = ""

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body == {"error": "SagemakerDomainNotFound"}
        sagemaker_mock.list_domains.assert_not_called()
        sagemaker_mock.describe_user_profile.assert_not_called()
        sagemaker_mock.create_presigned_domain_url.assert_not_called()

    def test_create_presigned_url_raise_returns_500_with_opaque_token(self, handler_module) -> None:
        """500 body contains only the opaque token, never the message."""
        handler, sagemaker_mock = handler_module

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
# Hypothesis response-shape property
# ---------------------------------------------------------------------------


# Names SageMaker accepts as a UserProfileName: letters and digits with
# hyphens only between them, 63 characters at most.
_username_strategy = st.from_regex(r"[a-zA-Z0-9](-{0,3}[a-zA-Z0-9]){0,20}", fullmatch=True).filter(
    lambda name: len(name) <= 63
)

# Cognito usernames SageMaker rejects: a character outside letters, digits
# and hyphens, a leading or trailing hyphen, or more than 63 characters.
_unsupported_username_strategy = st.one_of(
    st.text(alphabet=string.ascii_letters + string.digits + "-._@+ \n", min_size=1, max_size=70),
    st.text(alphabet=string.ascii_lowercase, min_size=64, max_size=80),
).filter(
    lambda name: re.fullmatch(r"[a-zA-Z0-9](-*[a-zA-Z0-9]){0,62}", name) is None or len(name) > 63
)

_claim_strategy: st.SearchStrategy[dict[str, Any]] = st.fixed_dictionaries(
    {"cognito:username": _username_strategy, "sub": st.uuids().map(str)},
    optional={
        "cognito:groups": st.lists(
            st.text(min_size=1, max_size=16, alphabet=string.ascii_letters),
            max_size=4,
        ),
        "email": st.one_of(
            st.none(),
            st.emails(),
        ),
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
        handler, sagemaker_mock = handler_module

        # Stub the domain + profile + access-point flow so every example
        # takes the happy path. The per-username access-point path
        # uses string interpolation on ``claims["cognito:username"]``
        # so we synthesize the matching AP record.
        username = claims["cognito:username"]
        sagemaker_mock.describe_user_profile.return_value = {
            "UserProfileName": username,
            "UserProfileArn": f"{_PROFILE_ARN.rsplit('/', 1)[0]}/{username}",
            "DomainId": _STUDIO_DOMAIN_ID,
            "Status": "InService",
        }
        sagemaker_mock.list_tags.return_value = _owner_tags(claims["sub"])
        sagemaker_mock.create_presigned_domain_url.return_value = {
            "AuthorizedUrl": (
                f"https://d-propcheck.studio.us-east-2.sagemaker.aws/auth?user={username}"
            ),
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

    @settings(
        max_examples=60,
        deadline=5000,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
            HealthCheck.filter_too_much,
        ],
    )
    @given(username=_unsupported_username_strategy)
    def test_unsupported_username_is_a_400_before_any_aws_call(
        self, handler_module, username: str
    ) -> None:
        """A name SageMaker would reject never reaches SageMaker or EFS.

        Before, ``alice.smith`` reached ``DescribeUserProfile`` and came back
        as a generic 500 (and as an EFS path component).
        """
        handler, sagemaker_mock = handler_module
        sagemaker_mock.reset_mock()

        response = handler.lambda_handler(_make_event(username=username), None)

        assert response["statusCode"] == 400
        assert json.loads(response["body"]) == {"error": "UnsupportedUsername"}
        assert sagemaker_mock.mock_calls == []


# ---------------------------------------------------------------------------
# Module-scope sanity check
# ---------------------------------------------------------------------------


def test_handler_module_exposes_pure_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_parse_claims``, ``_format_success``, and ``_format_error`` are
    factored out as pure helpers.
    """
    for key, value in _HANDLER_ENV.items():
        monkeypatch.setenv(key, value)

    with patch("boto3.client", return_value=MagicMock()):
        handler = load_lambda_module("analytics-presigned-url")

    assert callable(handler._parse_claims)
    assert callable(handler._is_studio_username)
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
        handler, _ = handler_module
        assert handler._parse_claims(event) == {}


class TestEnsureUserProfileErrorCodes:
    def test_unexpected_describe_error_propagates(self, handler_module) -> None:
        handler, sagemaker_mock = handler_module
        sagemaker_mock.describe_user_profile.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "DescribeUserProfile"
        )

        with pytest.raises(ClientError, match="AccessDeniedException"):
            handler._ensure_user_profile(_STUDIO_DOMAIN_ID, "alice", _SUB, _STUDIO_EFS_ID)
        sagemaker_mock.create_user_profile.assert_not_called()

    @pytest.mark.parametrize("code", ["ResourceInUse", "AccessDeniedException"])
    def test_failed_profile_that_cannot_be_deleted_is_reported(self, handler_module, code) -> None:
        """A create cannot succeed while the profile exists (NEW-2).

        Before, the refused delete was logged and the create's ResourceInUse
        ignored, so the CLI was told "provisioning" until it gave up.
        """
        handler, sagemaker_mock = handler_module
        sagemaker_mock.describe_user_profile.return_value = _profile(
            "Failed", FailureReason="quota"
        )
        sagemaker_mock.delete_user_profile.side_effect = ClientError(
            {"Error": {"Code": code, "Message": "no"}}, "DeleteUserProfile"
        )

        status = handler._ensure_user_profile(_STUDIO_DOMAIN_ID, "alice", _SUB, _STUDIO_EFS_ID)

        assert status == "Failed"
        sagemaker_mock.create_user_profile.assert_not_called()

    def test_failed_profile_already_gone_is_recreated(self, handler_module) -> None:
        handler, sagemaker_mock = handler_module
        sagemaker_mock.describe_user_profile.return_value = _profile("Failed")
        sagemaker_mock.delete_user_profile.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFound", "Message": "gone"}}, "DeleteUserProfile"
        )

        status = handler._ensure_user_profile(_STUDIO_DOMAIN_ID, "alice", _SUB, _STUDIO_EFS_ID)

        assert status == "Provisioning"
        sagemaker_mock.create_user_profile.assert_called_once()

    @pytest.mark.parametrize("status", ["Update_Failed", "Delete_Failed"])
    def test_unrecoverable_profile_is_a_409_not_a_202(self, handler_module, status) -> None:
        """Before: only ``Failed`` was handled, so these returned 202 on every
        poll and the CLI timed out after 120 s with "Try again in a minute"."""
        handler, sagemaker_mock = handler_module
        sagemaker_mock.describe_user_profile.return_value = _profile(
            status, FailureReason="VPC quota"
        )

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 409
        assert json.loads(response["body"]) == {"error": "UserProfileFailed"}
        sagemaker_mock.delete_user_profile.assert_not_called()
        sagemaker_mock.create_user_profile.assert_not_called()
        sagemaker_mock.create_presigned_domain_url.assert_not_called()

    @pytest.mark.parametrize("status", ["Pending", "Updating", "Deleting"])
    def test_converging_profile_keeps_polling(self, handler_module, status) -> None:
        handler, sagemaker_mock = handler_module
        sagemaker_mock.describe_user_profile.return_value = _profile(status)

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 202
        assert json.loads(response["body"]) == {"status": "provisioning"}
        sagemaker_mock.delete_user_profile.assert_not_called()
        sagemaker_mock.create_user_profile.assert_not_called()

    def test_concurrent_creation_is_tolerated(self, handler_module) -> None:
        handler, sagemaker_mock = handler_module
        sagemaker_mock.create_user_profile.side_effect = ClientError(
            {"Error": {"Code": "ResourceInUse", "Message": "exists"}}, "CreateUserProfile"
        )

        handler._create_user_profile(_STUDIO_DOMAIN_ID, "alice", _SUB, _STUDIO_EFS_ID)

    def test_other_creation_failures_propagate(self, handler_module) -> None:
        handler, sagemaker_mock = handler_module
        sagemaker_mock.create_user_profile.side_effect = ClientError(
            {"Error": {"Code": "ResourceLimitExceeded", "Message": "quota"}}, "CreateUserProfile"
        )

        with pytest.raises(ClientError, match="ResourceLimitExceeded"):
            handler._create_user_profile(_STUDIO_DOMAIN_ID, "alice", _SUB, _STUDIO_EFS_ID)


class TestStudioUsernames:
    """``_is_studio_username`` is SageMaker's ``UserProfileName`` rule."""

    @pytest.mark.parametrize(
        "username",
        ["alice", "Alice", "a", "7", "alice-smith", "a--b", "a" * 63, "a" + "-b" * 31],
    )
    def test_names_sagemaker_accepts(self, handler_module, username: str) -> None:
        handler, _ = handler_module
        assert handler._is_studio_username(username)

    @pytest.mark.parametrize(
        "username",
        [
            "alice.smith",
            "alice_smith",
            "alice@example.com",
            "-alice",
            "alice-",
            "a b",
            "alice\n",
            "\u00e5lice",
            "a" * 64,
            # The pattern alone allows hyphen runs past 63 characters.
            "a" + "--b" * 21,
        ],
    )
    def test_names_sagemaker_rejects(self, handler_module, username: str) -> None:
        handler, _ = handler_module
        assert not handler._is_studio_username(username)


class TestProfileOwnership:
    """A profile answers only to the Cognito ``sub`` its owner tag names.

    SageMaker matches profile names without regard to case, so before the
    tag, ``alice`` logging in after ``Alice`` was handed ``Alice``'s
    profile (ResourceInUse read as "already exists") and a presigned URL
    into it.
    """

    @pytest.mark.parametrize("status", ["InService", "Pending", "Updating", "Deleting"])
    def test_another_users_profile_is_a_403(self, handler_module, status: str) -> None:
        handler, sagemaker_mock = handler_module
        sagemaker_mock.describe_user_profile.return_value = _profile(status)
        sagemaker_mock.list_tags.return_value = _owner_tags(_OTHER_SUB)

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 403
        assert json.loads(response["body"]) == {"error": "UserProfileOwnedByAnotherUser"}
        sagemaker_mock.list_tags.assert_called_once_with(ResourceArn=_PROFILE_ARN)
        sagemaker_mock.add_tags.assert_not_called()
        sagemaker_mock.create_presigned_domain_url.assert_not_called()

    @pytest.mark.parametrize("status", ["Failed", "Update_Failed", "Delete_Failed"])
    def test_another_users_failed_profile_is_left_alone(self, handler_module, status) -> None:
        """Ownership comes before the delete-and-recreate of a Failed profile."""
        handler, sagemaker_mock = handler_module
        sagemaker_mock.describe_user_profile.return_value = _profile(status)
        sagemaker_mock.list_tags.return_value = _owner_tags(_OTHER_SUB)

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 403
        sagemaker_mock.delete_user_profile.assert_not_called()
        sagemaker_mock.create_user_profile.assert_not_called()

    def test_untagged_profile_in_service_is_claimed_by_the_caller(self, handler_module) -> None:
        """A profile made before the tag (or outside the Lambda) gets one."""
        handler, sagemaker_mock = handler_module
        sagemaker_mock.list_tags.return_value = {"Tags": [{"Key": "Project", "Value": "GCO"}]}

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 200
        sagemaker_mock.add_tags.assert_called_once_with(
            ResourceArn=_PROFILE_ARN, Tags=[{"Key": _OWNER_TAG_KEY, "Value": _SUB}]
        )

    def test_untagged_profile_still_provisioning_is_not_claimed_yet(self, handler_module) -> None:
        handler, sagemaker_mock = handler_module
        sagemaker_mock.describe_user_profile.return_value = _profile("Pending")
        sagemaker_mock.list_tags.return_value = {"Tags": []}

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 202
        sagemaker_mock.add_tags.assert_not_called()

    def test_own_profile_is_not_retagged(self, handler_module) -> None:
        handler, sagemaker_mock = handler_module

        response = handler.lambda_handler(_make_event(), None)

        assert response["statusCode"] == 200
        sagemaker_mock.add_tags.assert_not_called()

    def test_owner_tag_on_a_later_page_is_found(self, handler_module) -> None:
        handler, sagemaker_mock = handler_module
        sagemaker_mock.list_tags.side_effect = [
            {"Tags": [{"Key": "Project", "Value": "GCO"}], "NextToken": "page-2"},
            _owner_tags(_OTHER_SUB),
        ]

        assert handler._profile_owner(_PROFILE_ARN) == _OTHER_SUB
        assert sagemaker_mock.list_tags.call_args_list[1].kwargs == {
            "ResourceArn": _PROFILE_ARN,
            "NextToken": "page-2",
        }

    def test_failed_untagged_profile_is_recreated_for_the_caller(self, handler_module) -> None:
        handler, sagemaker_mock = handler_module
        sagemaker_mock.describe_user_profile.return_value = _profile("Failed")
        sagemaker_mock.list_tags.return_value = {"Tags": []}

        status = handler._ensure_user_profile(_STUDIO_DOMAIN_ID, "alice", _SUB, _STUDIO_EFS_ID)

        assert status == "Provisioning"
        sagemaker_mock.delete_user_profile.assert_called_once()
        assert sagemaker_mock.create_user_profile.call_args.kwargs["Tags"] == [
            {"Key": _OWNER_TAG_KEY, "Value": _SUB}
        ]

    def test_concurrent_create_by_another_user_is_refused_on_the_next_poll(
        self, handler_module
    ) -> None:
        """``alice``'s create loses to ``Alice``'s; the next poll answers 403."""
        handler, sagemaker_mock = handler_module
        missing = ClientError(
            {"Error": {"Code": "ResourceNotFound", "Message": "not found"}}, "DescribeUserProfile"
        )
        sagemaker_mock.describe_user_profile.side_effect = [missing, _profile("Pending")]
        sagemaker_mock.create_user_profile.side_effect = ClientError(
            {"Error": {"Code": "ResourceInUse", "Message": "exists"}}, "CreateUserProfile"
        )
        sagemaker_mock.list_tags.return_value = _owner_tags(_OTHER_SUB)

        first = handler.lambda_handler(_make_event(), None)
        second = handler.lambda_handler(_make_event(), None)

        assert first["statusCode"] == 202
        assert second["statusCode"] == 403
