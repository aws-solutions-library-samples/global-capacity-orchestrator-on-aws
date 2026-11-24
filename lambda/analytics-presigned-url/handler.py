"""Presigned-URL Lambda for SageMaker Studio (analytics environment).

Exchanges a Cognito-authorized API Gateway event for a time-limited
``sagemaker:CreatePresignedDomainUrl`` link.

Flow (happy path):

1. Extract ``claims = event["requestContext"]["authorizer"]["claims"]`` and
   read ``cognito:username`` (an ID token's name for it) or ``username``
   (an access token's, which is what the route requires since it lists a
   scope) and ``sub``, which both tokens carry. A username
   SageMaker cannot use as a ``UserProfileName`` (``STUDIO_USERNAME_PATTERN``,
   63 characters at most) is an HTTP 400 ``UnsupportedUsername``.
2. Read the Studio ``DomainId`` from the ``STUDIO_DOMAIN_ID`` env var,
   which the stack always sets; without it the request is an HTTP 404.
3. ``sagemaker:DescribeUserProfile`` -- if the profile doesn't exist yet
   (``ValidationException`` / ``ResourceNotFound``), create it via
   ``sagemaker:CreateUserProfile``, tagged ``gco:analytics:cognito-sub``
   with the caller's ``sub``. SageMaker matches profile names without
   regard to case, so ``Alice`` and ``alice`` reach the same profile: an
   existing profile whose tag names another ``sub`` is an HTTP 403
   ``UserProfileOwnedByAnotherUser`` before anything else is done with it,
   and an untagged one (created before the tag, or outside this Lambda)
   is tagged for the caller once it is ``InService``. If the profile is
   still provisioning (``Pending`` / ``Updating`` / ``Deleting``), return
   HTTP 202 so the CLI can poll. If the profile is ``Failed``, delete it
   and recreate. A profile this Lambda cannot recover (``Update_Failed``,
   ``Delete_Failed``, or a ``Failed`` one whose delete is refused) is an
   HTTP 409 ``UserProfileFailed`` instead of a 202 the CLI would poll
   until it gives up.
4. ``sagemaker:CreatePresignedDomainUrl`` and return the URL (HTTP 200).

The profile reaches ``/home/<username>`` on the Studio EFS through its
``EFSFileSystemConfig``, which takes only a file system ID and a path, with
Studio's own POSIX identity. Earlier releases also created a per-user EFS
access point at that path on every login; nothing mounted through it, so
it never applied (EFS creates an access point's root directory only when a
client mounts through the access point), and it is no longer created. The
stack's cleanup still deletes the ones earlier releases left.

The Lambda never blocks waiting for profile provisioning.  Instead it
returns HTTP 202 ``{"status": "provisioning"}`` and the CLI retries
every few seconds until it receives HTTP 200 with the presigned URL.
This avoids hitting the API Gateway 29-second integration timeout.

All failures funnel through the outer ``try/except`` in
:func:`lambda_handler`; the response body is always JSON and never
leaks an exception string.

Environment variables (set by ``GCOAnalyticsStack._create_presigned_url_lambda``):

- ``STUDIO_DOMAIN_ID`` -- the Studio domain ID (e.g. ``d-abc123xyz``).
- ``SAGEMAKER_EXECUTION_ROLE_ARN`` -- passed on ``CreateUserProfile``.
- ``STUDIO_EFS_ID`` -- the file system each profile's home directory is on.
- ``URL_EXPIRES_SECONDS`` -- default ``300`` (5 minutes).
- ``SESSION_EXPIRES_SECONDS`` -- default ``43200`` (12 hours).

The module-level ``sagemaker`` boto3 client is created once at cold start
so repeat invocations inside a warm container reuse its HTTP connection
pool.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import boto3
from botocore.exceptions import ClientError

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-11-24T19:00:27Z
# Generated from Git commit: 21c18ee43b7d5814a559de944c67a84889dff8d5
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/analytics-presigned-url/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/analytics-presigned-url/handler.lambda_handler.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


# ---------------------------------------------------------------------------
# Module-level logger + boto3 clients
# ---------------------------------------------------------------------------
# Created once per cold start. boto3 clients are thread-safe for the
# method calls this Lambda makes (list/describe/create presigned URL).

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

sagemaker = boto3.client("sagemaker")

# ---------------------------------------------------------------------------
# Environment variables (read at module import, i.e. cold start)
# ---------------------------------------------------------------------------

STUDIO_DOMAIN_ID = os.environ.get("STUDIO_DOMAIN_ID", "")
SAGEMAKER_EXECUTION_ROLE_ARN = os.environ.get("SAGEMAKER_EXECUTION_ROLE_ARN", "")
STUDIO_EFS_ID = os.environ.get("STUDIO_EFS_ID", "")
URL_EXPIRES_SECONDS = int(os.environ.get("URL_EXPIRES_SECONDS", "300"))
SESSION_EXPIRES_SECONDS = int(os.environ.get("SESSION_EXPIRES_SECONDS", "43200"))

# ---------------------------------------------------------------------------
# Error tokens -- opaque strings returned in the ``error`` body key.
# ---------------------------------------------------------------------------
# Keep these short, stable, and free of implementation details so clients
# can switch on them without parsing exception messages.

_ERR_MISSING_CLAIM = "MissingCognitoClaim"
_ERR_UNSUPPORTED_USERNAME = "UnsupportedUsername"
_ERR_DOMAIN_NOT_FOUND = "SagemakerDomainNotFound"
_ERR_PROFILE_NOT_OWNED = "UserProfileOwnedByAnotherUser"
_ERR_PROFILE_FAILED = "UserProfileFailed"
_ERR_GENERIC = "PresignedUrlGenerationFailed"

# ---------------------------------------------------------------------------
# User profile names and ownership
# ---------------------------------------------------------------------------

#: SageMaker's ``UserProfileName`` rule (CreateUserProfile): a letter or digit,
#: then letters and digits with hyphens only between them. SageMaker also caps
#: the name at :data:`STUDIO_USERNAME_MAX_LENGTH` characters and compares names
#: without regard to case. ``gco analytics users add`` applies the same rule
#: (``cli.analytics_user_mgmt.STUDIO_USERNAME_PATTERN``) before it creates a
#: Cognito user, so the two must stay equal.
STUDIO_USERNAME_PATTERN = r"[a-zA-Z0-9](-*[a-zA-Z0-9]){0,62}"
STUDIO_USERNAME_MAX_LENGTH = 63
_STUDIO_USERNAME_RE = re.compile(STUDIO_USERNAME_PATTERN)

#: Tag recording the Cognito ``sub`` a user profile belongs to.
PROFILE_OWNER_TAG_KEY = "gco:analytics:cognito-sub"

#: Profile states a retry cannot fix: an update or a delete of the profile
#: failed, so it is neither usable nor safe to recreate. They need an operator.
_UNRECOVERABLE_PROFILE_STATUSES = frozenset({"Update_Failed", "Delete_Failed"})
#: DescribeUserProfile / DeleteUserProfile error codes for a missing profile.
_PROFILE_MISSING_CODES = frozenset({"ValidationException", "ResourceNotFound"})

# ==========================================================================
# Pure helpers (unit-testable without mocking)
# ==========================================================================


def _parse_claims(event: object) -> dict[str, Any]:
    """Extract the Cognito claims dict from an API Gateway proxy event.

    Returns an empty dict if ``event["requestContext"]["authorizer"]["claims"]``
    is not present or not a dict. The caller decides whether an empty
    result warrants a 401 -- see :func:`lambda_handler`.

    ``event`` is typed ``object`` because every layer of the path is
    shape-checked here, the event itself included; the helper makes no
    assumption about what API Gateway (or a test) hands it.
    """
    if not isinstance(event, dict):
        return {}
    request_context = event.get("requestContext")
    if not isinstance(request_context, dict):
        return {}
    authorizer = request_context.get("authorizer")
    if not isinstance(authorizer, dict):
        return {}
    claims = authorizer.get("claims")
    if not isinstance(claims, dict):
        return {}
    return claims


def _is_studio_username(username: str) -> bool:
    """Return True when SageMaker accepts ``username`` as a ``UserProfileName``.

    The length is checked first, so the pattern never sees more than
    :data:`STUDIO_USERNAME_MAX_LENGTH` characters. ``fullmatch`` rather than
    ``^...$``: ``$`` also matches before a trailing newline.
    """
    return (
        len(username) <= STUDIO_USERNAME_MAX_LENGTH
        and _STUDIO_USERNAME_RE.fullmatch(username) is not None
    )


def _format_success(url: str, expires: int) -> dict[str, Any]:
    """Format an HTTP 200 API Gateway proxy response with the presigned URL."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"url": url, "expires_in": expires}),
    }


def _format_provisioning() -> dict[str, Any]:
    """Format an HTTP 202 response indicating the profile is still provisioning.

    The CLI polls on this status code until the profile reaches InService
    and the Lambda returns HTTP 200 with the presigned URL.
    """
    return {
        "statusCode": 202,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"status": "provisioning"}),
    }


def _format_error(status: int, token: str) -> dict[str, Any]:
    """Format an HTTP error API Gateway proxy response.

    ``token`` is one of the module-level ``_ERR_*`` constants; ``status``
    is the HTTP status code. The body never contains an exception
    message -- callers log the underlying exception via :data:`logger`
    before returning.
    """
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": token}),
    }


# ==========================================================================
# Effectful helpers (wrap boto3 calls)
# ==========================================================================


def _profile_owner(profile_arn: str) -> str | None:
    """Return the Cognito ``sub`` the profile's owner tag holds, or None if untagged."""
    kwargs: dict[str, Any] = {"ResourceArn": profile_arn}
    while True:
        response = sagemaker.list_tags(**kwargs)
        for tag in response.get("Tags", []):
            if tag.get("Key") == PROFILE_OWNER_TAG_KEY:
                return str(tag.get("Value", ""))
        next_token = response.get("NextToken")
        if not next_token:
            return None
        kwargs["NextToken"] = next_token


def _ensure_user_profile(domain_id: str, username: str, sub: str, efs_id: str) -> str:
    """Ensure a ``sagemaker:UserProfile`` owned by ``sub`` exists for ``username``.

    Returns the profile status:

    * ``"InService"`` -- profile is ready; caller can mint a presigned URL.
    * ``"Provisioning"`` -- profile was just created or is still starting;
      caller should return HTTP 202 so the CLI can poll.
    * ``"NotOwned"`` -- the profile's owner tag names another Cognito
      user; caller should return HTTP 403. SageMaker matches profile names
      without regard to case, so this is how ``alice`` is kept out of
      ``Alice``'s profile (and out of a deleted user's, when a new Cognito
      user gets the old name).
    * ``"Failed"`` -- profile is stuck in a state no poll will change
      (``_UNRECOVERABLE_PROFILE_STATUSES``, or ``Failed`` with its delete
      refused); caller should return an error.

    Ownership is checked before anything else is done with an existing
    profile, the delete of a ``Failed`` one included. An untagged profile
    is tagged for ``sub`` once it is ``InService``; until then a profile
    another user claims first is theirs.

    If the profile is in ``Failed`` state, it is deleted and recreated
    so the next poll attempt finds a fresh ``Pending`` profile. While the
    profile still exists a create cannot succeed, so a refused delete is
    reported rather than polled on.
    """
    try:
        resp = sagemaker.describe_user_profile(
            DomainId=domain_id,
            UserProfileName=username,
        )
        profile_arn = resp["UserProfileArn"]
        owner = _profile_owner(profile_arn)
        if owner is not None and owner != sub:
            logger.warning("User profile %s belongs to another Cognito user", username)
            return "NotOwned"

        status = resp.get("Status", "")

        if status == "InService":
            if owner is None:
                sagemaker.add_tags(
                    ResourceArn=profile_arn,
                    Tags=[{"Key": PROFILE_OWNER_TAG_KEY, "Value": sub}],
                )
                logger.info("Tagged untagged user profile %s with its owner", username)
            return "InService"

        if status in _UNRECOVERABLE_PROFILE_STATUSES:
            logger.error(
                "User profile %s is %s (%s); it needs an operator",
                username,
                status,
                resp.get("FailureReason", "unknown"),
            )
            return "Failed"

        if status == "Failed":
            logger.warning(
                "User profile %s is Failed (%s) -- deleting and recreating",
                username,
                resp.get("FailureReason", "unknown"),
            )
            try:
                sagemaker.delete_user_profile(
                    DomainId=domain_id,
                    UserProfileName=username,
                )
            except ClientError as del_exc:
                code = del_exc.response.get("Error", {}).get("Code", "")
                if code not in _PROFILE_MISSING_CODES:
                    logger.error("Could not delete failed profile %s: %s", username, del_exc)
                    return "Failed"
            # Create a fresh profile (may race with the delete; ResourceInUse
            # is caught below).
            _create_user_profile(domain_id, username, sub, efs_id)
            return "Provisioning"

        # Pending / Updating / Deleting: still converging.
        logger.info("User profile %s status=%s, still provisioning", username, status)
        return "Provisioning"

    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in _PROFILE_MISSING_CODES:
            raise

    # Profile doesn't exist -- create it.
    _create_user_profile(domain_id, username, sub, efs_id)
    return "Provisioning"


def _create_user_profile(domain_id: str, username: str, sub: str, efs_id: str) -> None:
    """Create a SageMaker user profile owned by ``sub``. Silently ignores ResourceInUse.

    A concurrent create by another user wins the name; the next poll's
    ownership check answers this caller with ``NotOwned``.
    """
    try:
        sagemaker.create_user_profile(
            DomainId=domain_id,
            UserProfileName=username,
            UserSettings={
                "ExecutionRole": SAGEMAKER_EXECUTION_ROLE_ARN,
                "CustomFileSystemConfigs": [
                    {
                        "EFSFileSystemConfig": {
                            "FileSystemId": efs_id,
                            "FileSystemPath": f"/home/{username}",
                        }
                    }
                ],
            },
            Tags=[{"Key": PROFILE_OWNER_TAG_KEY, "Value": sub}],
        )
        logger.info("Created user profile %s in domain %s", username, domain_id)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "ResourceInUse":
            raise
        logger.info("User profile %s already exists (ResourceInUse)", username)


# ==========================================================================
# Entry point
# ==========================================================================


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Exchange a Cognito-authorized event for a presigned Studio URL.

    Returns:

    * **HTTP 200** with ``{"url": "...", "expires_in": N}`` when the
      user profile is ``InService`` and the presigned URL is ready.
    * **HTTP 202** with ``{"status": "provisioning"}`` when the user
      profile is still being created.  The CLI should retry after a
      few seconds.
    * **HTTP 400** with ``{"error": "UnsupportedUsername"}`` when the
      Cognito username cannot name a SageMaker user profile.
    * **HTTP 403** with ``{"error": "UserProfileOwnedByAnotherUser"}`` when
      the user profile of that name belongs to another Cognito user.
    * **HTTP 409** with ``{"error": "UserProfileFailed"}`` when the user
      profile is stuck in a failed state no retry will change.
    * **HTTP 4xx / 5xx** with ``{"error": "<token>"}`` on failure.

    The Lambda never blocks waiting for profile provisioning.  The CLI
    is responsible for polling until it receives HTTP 200.
    """
    try:
        # Step 1: extract + validate the Cognito username and sub claims.
        claims = _parse_claims(event)
        username = claims.get("cognito:username") or claims.get("username")
        sub = claims.get("sub")
        if not isinstance(username, str) or not username or not isinstance(sub, str) or not sub:
            logger.warning("Request missing the Cognito username or sub claim")
            return _format_error(401, _ERR_MISSING_CLAIM)
        if not _is_studio_username(username):
            logger.warning("Username %r cannot name a SageMaker user profile", username)
            return _format_error(400, _ERR_UNSUPPORTED_USERNAME)

        # Step 2: the Studio DomainId, which the stack always sets.
        domain_id = STUDIO_DOMAIN_ID
        if not domain_id:
            logger.error("SageMaker domain not configured (STUDIO_DOMAIN_ID is empty)")
            return _format_error(404, _ERR_DOMAIN_NOT_FOUND)

        # Step 3: describe-or-create the user profile, owned by ``sub``.
        profile_status = _ensure_user_profile(domain_id, username, sub, STUDIO_EFS_ID)
        if profile_status == "NotOwned":
            return _format_error(403, _ERR_PROFILE_NOT_OWNED)
        if profile_status == "Failed":
            return _format_error(409, _ERR_PROFILE_FAILED)
        if profile_status != "InService":
            return _format_provisioning()

        # Step 4: mint the presigned URL.
        response = sagemaker.create_presigned_domain_url(
            DomainId=domain_id,
            UserProfileName=username,
            SessionExpirationDurationInSeconds=SESSION_EXPIRES_SECONDS,
            ExpiresInSeconds=URL_EXPIRES_SECONDS,
        )
        url = response.get("AuthorizedUrl", "")
        return _format_success(url, URL_EXPIRES_SECONDS)

    except Exception as exc:  # outer catch-all so every failure returns an opaque error token
        # Log with exception info so CloudWatch captures the stack trace,
        # but never leak the message to the HTTP response body.
        logger.error("Presigned URL generation failed: %s", exc, exc_info=True)
        return _format_error(500, _ERR_GENERIC)
