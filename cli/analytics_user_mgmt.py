"""
User management helpers for the GCO analytics environment.

This module holds the pieces of the ``gco analytics`` CLI that are worth
exercising in isolation from Click:

* :func:`discover_cognito_pool_id` / :func:`discover_cognito_client_id`
  / :func:`discover_api_endpoint` — single-stack CloudFormation output
  lookups used by every sub-command to avoid forcing operators to hand a
  pool id / api url on the command line.
* :func:`srp_authenticate` — Cognito SRP authentication via the
  ``pycognito`` library, used by ``gco analytics studio login``.
"""

from __future__ import annotations

import logging
from typing import Any

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-01T14:42:56Z
# Generated from Git commit: 89b000378ed5a912a38c06f4feab2b029936ebcc
# Flowchart(s) generated from this file:
#   * ``srp_authenticate`` -> ``diagrams/code_diagrams/cli/analytics_user_mgmt.srp_authenticate.html``
#     (PNG: ``diagrams/code_diagrams/cli/analytics_user_mgmt.srp_authenticate.png``)
#   * ``fetch_studio_url`` -> ``diagrams/code_diagrams/cli/analytics_user_mgmt.fetch_studio_url.html``
#     (PNG: ``diagrams/code_diagrams/cli/analytics_user_mgmt.fetch_studio_url.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CloudFormation output discovery
# ---------------------------------------------------------------------------


def _describe_stack_outputs(region: str, stack_name: str) -> list[dict[str, str]] | None:
    """Return the ``Outputs`` list for ``stack_name`` in ``region``.

    Returns ``None`` if the stack does not exist or the call fails.
    Any non-transient error surfaces as ``None`` — callers raise the
    user-facing error message themselves so the error copy can mention
    ``gco analytics enable`` / ``gco stacks deploy gco-analytics``.
    """
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        cfn = boto3.client("cloudformation", region_name=region)
        response = cfn.describe_stacks(StackName=stack_name)
    except (ClientError, BotoCoreError) as exc:
        logger.debug("describe_stacks(%s) in %s failed: %s", stack_name, region, exc)
        return None

    stacks = response.get("Stacks", [])
    if not stacks:
        return None
    outputs = stacks[0].get("Outputs", [])
    return list(outputs) if isinstance(outputs, list) else []


def _find_output(outputs: list[dict[str, str]], key: str) -> str | None:
    """Return the ``OutputValue`` for ``key`` in a CloudFormation outputs list."""
    for output in outputs:
        if output.get("OutputKey") == key:
            value = output.get("OutputValue")
            return value if isinstance(value, str) else None
    return None


def discover_cognito_pool_id(region: str, project_name: str = "gco") -> str | None:
    """Return the Cognito user pool id published by ``gco-analytics``.

    Returns ``None`` when the ``gco-analytics`` stack does not exist or
    when the stack exists but the ``CognitoUserPoolId`` output is
    missing. The CLI callers translate ``None`` into the documented
    "gco-analytics stack not deployed" error message.
    """
    stack_name = f"{project_name}-analytics"
    outputs = _describe_stack_outputs(region, stack_name)
    if outputs is None:
        return None
    return _find_output(outputs, "CognitoUserPoolId")


def discover_cognito_client_id(region: str, project_name: str = "gco") -> str | None:
    """Return the Cognito SRP client id published by ``gco-analytics``.

    Looked up on the same stack as :func:`discover_cognito_pool_id`.
    Returns ``None`` when the stack or output is missing.
    """
    stack_name = f"{project_name}-analytics"
    outputs = _describe_stack_outputs(region, stack_name)
    if outputs is None:
        return None
    return _find_output(outputs, "CognitoUserPoolClientId")


def discover_api_endpoint(region: str, project_name: str = "gco") -> str | None:
    """Return the API Gateway base URL published by ``gco-api-gateway``.

    The returned value is the ``ApiEndpoint`` CloudFormation output,
    typically of the form ``https://<id>.execute-api.<region>.amazonaws.com/prod/``.
    Returns ``None`` when the stack or output is missing.
    """
    stack_name = f"{project_name}-api-gateway"
    outputs = _describe_stack_outputs(region, stack_name)
    if outputs is None:
        return None
    return _find_output(outputs, "ApiEndpoint")


# ---------------------------------------------------------------------------
# Cognito authentication
# ---------------------------------------------------------------------------


def srp_authenticate(
    pool_id: str,
    client_id: str,
    username: str,
    password: str,
    region: str,
) -> dict[str, str]:
    """Authenticate a Cognito user via the ADMIN_USER_PASSWORD_AUTH flow.

    Uses ``admin_initiate_auth`` which sends the password over TLS
    directly (no client-side SRP math). This requires the user pool
    client to have ``ALLOW_ADMIN_USER_PASSWORD_AUTH`` enabled and the
    caller to have ``cognito-idp:AdminInitiateAuth`` permission.

    Returns a dict with ``IdToken``, ``AccessToken``, and
    ``RefreshToken`` on success. Raises ``botocore.exceptions.ClientError``
    for Cognito-side failures (``NotAuthorizedException``,
    ``UserNotFoundException``, etc.).
    """
    import boto3

    cognito = boto3.client("cognito-idp", region_name=region)
    response = cognito.admin_initiate_auth(
        UserPoolId=pool_id,
        ClientId=client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": username,
            "PASSWORD": password,
        },
    )
    tokens = response.get("AuthenticationResult") or {}
    return {
        "IdToken": str(tokens.get("IdToken", "")),
        "AccessToken": str(tokens.get("AccessToken", "")),
        "RefreshToken": str(tokens.get("RefreshToken", "")),
    }


__all__ = [
    "admin_create_user",
    "admin_delete_user",
    "admin_set_user_password",
    "check_ssm_parameter",
    "check_stack_complete",
    "discover_api_endpoint",
    "discover_cognito_client_id",
    "discover_cognito_pool_id",
    "fetch_studio_url",
    "generate_strong_password",
    "list_users",
    "scan_orphan_analytics_resources",
    "srp_authenticate",
]

# ---------------------------------------------------------------------------
# Cognito user management helpers
# ---------------------------------------------------------------------------


def admin_create_user(
    pool_id: str,
    region: str,
    username: str,
    email: str | None = None,
    suppress_email: bool = False,
) -> tuple[dict[str, Any], str | None]:
    """Create a Cognito user via AdminCreateUser.

    Returns ``(response, temporary_password)``. The temporary password
    is only set when Cognito echoes it in the response (it does this
    on some versions of the API when ``MessageAction=SUPPRESS``); when
    absent the caller should direct the operator to
    ``admin-set-user-password`` out-of-band.
    """
    import boto3

    user_attributes: list[dict[str, str]] = []
    if email:
        user_attributes.append({"Name": "email", "Value": email})
        user_attributes.append({"Name": "email_verified", "Value": "true"})

    kwargs: dict[str, Any] = {
        "UserPoolId": pool_id,
        "Username": username,
        "UserAttributes": user_attributes,
    }
    if suppress_email:
        kwargs["MessageAction"] = "SUPPRESS"

    cognito = boto3.client("cognito-idp", region_name=region)
    response = cognito.admin_create_user(**kwargs)

    temporary_password: str | None = None
    user = response.get("User", {})
    for attr in user.get("Attributes", []) or []:
        if attr.get("Name") == "temporary_password":
            temporary_password = attr.get("Value")
            break
    if temporary_password is None:
        temporary_password = response.get("TemporaryPassword")

    return response, temporary_password


def admin_set_user_password(
    pool_id: str,
    region: str,
    username: str,
    password: str,
    permanent: bool = True,
) -> None:
    """Set a Cognito user's password via AdminSetUserPassword.

    ``permanent=True`` (the default) marks the password as already
    satisfying Cognito's ``NEW_PASSWORD_REQUIRED`` challenge so the
    user can sign in without a forced reset — matching what you'd
    get from ``aws cognito-idp admin-set-user-password --permanent``.
    Pass ``permanent=False`` to require the user to pick their own
    password on first login.
    """
    import boto3

    cognito = boto3.client("cognito-idp", region_name=region)
    cognito.admin_set_user_password(
        UserPoolId=pool_id,
        Username=username,
        Password=password,
        Permanent=permanent,
    )


def generate_strong_password(length: int = 20) -> str:
    """Return a random password that satisfies Cognito's default policy.

    Cognito's default password policy requires at least one uppercase
    letter, one lowercase letter, one digit, and one symbol, plus the
    length minimum (8). The generated password is sampled from
    :func:`secrets.choice` — cryptographically strong by construction —
    and guaranteed to contain one character from each required class,
    with the remaining characters drawn from the union.
    """
    import secrets
    import string

    if length < 8:
        raise ValueError(f"length must be >= 8 to satisfy Cognito policy; got {length}")

    lowers = string.ascii_lowercase
    uppers = string.ascii_uppercase
    digits = string.digits
    # Cognito's allowed symbol set per AWS docs. Notably excludes space
    # and tab — Cognito rejects whitespace with InvalidParameterException.
    symbols = "^$*.[]{}()?-\"!@#%&/\\,><':;|_~`+="

    required = [
        secrets.choice(lowers),
        secrets.choice(uppers),
        secrets.choice(digits),
        secrets.choice(symbols),
    ]
    alphabet = lowers + uppers + digits + symbols
    remaining = [secrets.choice(alphabet) for _ in range(length - len(required))]

    # Shuffle so the required-class characters aren't always at the start.
    chars = required + remaining
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]

    return "".join(chars)


def list_users(pool_id: str, region: str) -> list[dict[str, str]]:
    """Return a flat row-per-user list suitable for tabular output."""
    import boto3

    cognito = boto3.client("cognito-idp", region_name=region)
    response = cognito.list_users(UserPoolId=pool_id)

    rows: list[dict[str, str]] = []
    for user in response.get("Users", []) or []:
        row: dict[str, str] = {
            "username": user.get("Username", ""),
            "status": user.get("UserStatus", ""),
            "enabled": str(user.get("Enabled", "")),
        }
        for attr in user.get("Attributes", []) or []:
            if attr.get("Name") == "email":
                row["email"] = attr.get("Value", "")
        rows.append(row)
    return rows


def admin_delete_user(pool_id: str, region: str, username: str) -> None:
    """Delete a Cognito user via AdminDeleteUser."""
    import boto3

    cognito = boto3.client("cognito-idp", region_name=region)
    cognito.admin_delete_user(UserPoolId=pool_id, Username=username)


# ---------------------------------------------------------------------------
# HTTP helper for /studio/login
# ---------------------------------------------------------------------------


def fetch_studio_url(api_base: str, id_token: str) -> tuple[str, int, str]:
    """GET ``{api_base}/studio/login`` with the Cognito ID token.

    Returns ``(url, expires_in, correlation_id)`` on success. Raises
    :class:`urllib.error.HTTPError` / :class:`urllib.error.URLError`
    on transport or HTTP failure; raises ``ValueError`` on malformed
    response bodies (unexpected JSON shape / missing ``url`` key), or
    on non-``https://`` ``api_base`` values (guards urllib's
    ``file://`` / ``ftp://`` scheme support).
    """
    import email.message
    import json as _json
    import urllib.error
    import urllib.parse
    import urllib.request

    # Scheme allow-list — urllib happily dereferences ``file://`` and
    # ``ftp://`` URLs, which is the shape of the semgrep
    # ``dynamic-urllib-use-detected`` finding. We only ever call this with
    # the API Gateway endpoint (HTTPS by construction), so reject anything
    # else before the urlopen call.
    parsed = urllib.parse.urlparse(api_base)
    if parsed.scheme != "https":
        raise ValueError(
            f"api_base must use https:// scheme (got {parsed.scheme!r}). "
            "This guard rejects file:// / ftp:// schemes that urllib would "
            "otherwise follow."
        )
    if not parsed.netloc:
        raise ValueError(f"api_base is missing a hostname: {api_base!r}")

    login_url = api_base.rstrip("/") + "/studio/login"
    # Justification for the ``dynamic-urllib-use-detected`` / ``B310``
    # suppressions below: ``login_url`` is built from ``api_base`` + a
    # static ``/studio/login`` suffix. The scheme allow-list near the top
    # of this function rejects any ``api_base`` that isn't ``https://``
    # before we reach these lines, which closes the ``file://`` /
    # ``ftp://`` / ``custom`` scheme hole the rules are written to catch.
    # ``# fmt: off`` pins the block so the formatter can't re-wrap the
    # urlopen call — wrapping moves the suppression comments to the
    # wrong line and bandit / semgrep attach findings to the first
    # line of the call.
    # fmt: off
    request = urllib.request.Request(  # nosec B310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected  # noqa: S310
        login_url,
        headers={"Authorization": id_token, "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected  # noqa: S310
        status = int(response.status)
        body = response.read().decode("utf-8")
        correlation_id = response.headers.get("x-amzn-RequestId") or "N/A"
    # fmt: on

    if status == 202:
        # Profile is still provisioning -- return empty URL so the caller
        # can poll.  The body is ``{"status": "provisioning"}``.
        return "", 0, correlation_id

    if status != 200:
        # HTTPError requires a Message (email.message.Message) as its
        # headers argument; build an empty one for determinism.
        headers_msg: email.message.Message = email.message.Message()
        headers_msg["x-amzn-RequestId"] = correlation_id
        raise urllib.error.HTTPError(
            login_url,
            status,
            f"Studio login returned HTTP {status}",
            headers_msg,
            None,
        )

    try:
        payload = _json.loads(body)
        url = str(payload["url"])
        expires_in = int(payload.get("expires_in", 0))
    except (ValueError, KeyError) as exc:
        raise ValueError(f"malformed /studio/login response: {exc!r}") from exc

    return url, expires_in, correlation_id


# ---------------------------------------------------------------------------
# Doctor helpers
# ---------------------------------------------------------------------------


def check_stack_complete(region: str, stack_name: str) -> tuple[bool, str]:
    """Return ``(True, "")`` iff ``stack_name`` is in a healthy state.

    Healthy states are ``CREATE_COMPLETE`` / ``UPDATE_COMPLETE`` /
    ``IMPORT_COMPLETE``. Any other status (or missing stack) returns
    ``(False, remediation_hint)``.
    """
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        cfn = boto3.client("cloudformation", region_name=region)
        resp = cfn.describe_stacks(StackName=stack_name)
    except (ClientError, BotoCoreError) as exc:
        return False, f"describe_stacks failed in {region}: {exc!s}"
    stacks = resp.get("Stacks", [])
    if not stacks:
        return False, f"{stack_name} not found in {region}"
    status = stacks[0].get("StackStatus", "")
    if status in ("CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"):
        return True, ""
    return False, f"{stack_name} in {region} has status {status}"


def check_ssm_parameter(region: str, param_name: str) -> tuple[bool, str]:
    """Return ``(True, "")`` iff the SSM parameter exists in ``region``.

    Thin alias over :func:`gco.services.aws_ssm.check_ssm_parameter`
    that preserves the historical positional ``(region, param_name)``
    argument order. Kept as a re-export so existing callers and the
    public ``__all__`` surface stay stable; new code should reach for
    the keyword-style helper directly.
    """
    from gco.services.aws_ssm import check_ssm_parameter as _check

    return _check(param_name, region=region)


def scan_orphan_analytics_resources(region: str) -> list[str]:
    """Return a list of copy-paste ``aws`` commands for retained resources.

    Scans EFS and Cognito for resources tagged
    ``gco:analytics:managed=true``. An empty list means no orphans
    were found.
    """
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    remediation: list[str] = []
    try:
        efs = boto3.client("efs", region_name=region)
        for fs in efs.describe_file_systems().get("FileSystems", []) or []:
            fs_id = fs.get("FileSystemId", "")
            if not fs_id:
                continue
            tag_resp = efs.list_tags_for_resource(ResourceId=fs_id)
            tags = {t.get("Key"): t.get("Value") for t in tag_resp.get("Tags", []) or []}
            if tags.get("gco:analytics:managed") == "true":
                remediation.append(f"aws efs delete-file-system --file-system-id {fs_id}")
    except (ClientError, BotoCoreError) as exc:
        remediation.append(f"(EFS orphan scan failed: {exc!s})")

    try:
        cognito = boto3.client("cognito-idp", region_name=region)
        pools = cognito.list_user_pools(MaxResults=60)
        for pool in pools.get("UserPools", []) or []:
            pool_id = pool.get("Id")
            if not pool_id:
                continue
            describe = cognito.describe_user_pool(UserPoolId=pool_id)
            tags = describe.get("UserPool", {}).get("UserPoolTags", {}) or {}
            if tags.get("gco:analytics:managed") == "true":
                remediation.append(f"aws cognito-idp delete-user-pool --user-pool-id {pool_id}")
    except (ClientError, BotoCoreError) as exc:
        remediation.append(f"(Cognito orphan scan failed: {exc!s})")

    return remediation
