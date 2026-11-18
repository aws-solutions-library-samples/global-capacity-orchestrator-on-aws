"""
IAM role assumption for the GCO MCP server.

When ``GCO_MCP_ROLE_ARN`` is set, :func:`assume_mcp_role` assumes the
dedicated MCP IAM role at startup, and the role applies both inside the
server and to every process the server starts:

* The default boto3 session (``boto3.client(...)``, ``boto3.resource(...)``)
  gets refreshable role credentials: botocore's assume-role fetcher calls
  ``sts:AssumeRole`` again before the current credentials expire, so the
  server keeps working past ``GCO_MCP_ROLE_DURATION_SECONDS``.
* Child processes (``gco``, kubectl, and what those start: the AWS CLI, CDK)
  get the role through the AWS SDKs' container credentials provider. The
  server serves the role's current credentials on a loopback HTTP endpoint
  that requires a random authorization token, and :func:`child_environment`
  sets ``AWS_CONTAINER_CREDENTIALS_FULL_URI`` and
  ``AWS_CONTAINER_AUTHORIZATION_TOKEN`` for the child. Every SDK fetches
  container credentials again before they expire, so a deploy that outlasts
  one role session keeps working. The child environment also drops the
  server's own credential variables and points the AWS config and
  credentials files at private copies with every credential setting
  removed, so no other credential source can take precedence over the role.

Code that builds its own ``boto3.Session()`` in the server process (Mission's
Bedrock sampling and embeddings, the capacity checks behind Mission's
environment snapshot) still resolves the server's ambient credentials.

Credentials and the authorization token are never logged or put on a
command line.
"""

import atexit
import hmac
import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

audit_logger = logging.getLogger("gco.mcp.audit")

#: STS Region per partition when no Region is configured. The global STS
#: endpoint a client without a Region uses only serves the ``aws`` partition.
_PARTITION_STS_REGIONS = {
    "aws": "us-east-1",
    "aws-cn": "cn-north-1",
    "aws-us-gov": "us-gov-west-1",
}

#: Variables a child must not inherit: an SDK would read credentials from
#: them before it reaches the container credentials provider.
CHILD_CREDENTIAL_VARIABLES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CREDENTIAL_EXPIRATION",
    "AWS_ACCESS_KEY",
    "AWS_SECRET_KEY",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_CREDENTIAL_FILE",
)

#: Profile settings the children's private AWS config keeps. Everything else
#: (keys, role_arn/source_profile, credential_process, sso_*, web identity,
#: login sessions, and any setting added later) is dropped, so a profile a
#: child selects (AWS_PROFILE, a kubeconfig exec env) never supplies
#: credentials of its own.
_CHILD_PROFILE_SETTINGS = frozenset(
    {
        "account_id_endpoint_mode",
        "api_versions",
        "auth_scheme_preference",
        "ca_bundle",
        "cli_auto_prompt",
        "cli_binary_format",
        "cli_follow_urlparam",
        "cli_history",
        "cli_pager",
        "cli_timestamp_format",
        "defaults_mode",
        "disable_host_prefix_injection",
        "disable_request_compression",
        "endpoint_url",
        "ignore_configured_endpoint_urls",
        "max_attempts",
        "output",
        "parameter_validation",
        "region",
        "request_checksum_calculation",
        "request_min_compression_size_bytes",
        "response_checksum_validation",
        "retry_mode",
        "s3",
        "services",
        "sigv4a_signing_region_set",
        "sts_regional_endpoints",
        "tcp_keepalive",
        "use_dualstack_endpoint",
        "use_fips_endpoint",
    }
)


def _audit_error(role_arn: str, error: str) -> None:
    audit_logger.error(
        json.dumps(
            {
                "event": "mcp.server.role_assumption.error",
                "role_arn": role_arn,
                "error": error[:200],
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
    )


def _utc_timestamp(value: Any) -> str:
    """Render an ISO 8601 expiry as ``YYYY-MM-DDTHH:MM:SSZ`` for container clients."""
    return datetime.fromisoformat(str(value)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RoleCredentials:
    """Refreshable credentials for the MCP role.

    ``fetch`` returns botocore's credential metadata (``access_key``,
    ``secret_key``, ``token``, ``expiry_time``). Each fetch is kept whole, so
    :meth:`container_document` always serves a key and the expiry that
    belongs to it, even while another thread refreshes.
    """

    def __init__(self, fetch: Callable[[], dict[str, Any]]) -> None:
        from botocore.credentials import DeferredRefreshableCredentials

        self._fetch = fetch
        self._latest: dict[str, Any] = {}
        self.credentials = DeferredRefreshableCredentials(
            refresh_using=self._refresh, method="assume-role"
        )

    def _refresh(self) -> dict[str, Any]:
        metadata = self._fetch()
        self._latest = metadata
        return metadata

    def container_document(self) -> dict[str, str]:
        """Return current credentials in the container credentials format."""
        # Refreshes first when the credentials are close to expiring.
        self.credentials.get_frozen_credentials()
        latest = self._latest
        document = {
            "AccessKeyId": latest["access_key"],
            "SecretAccessKey": latest["secret_key"],
            "Token": latest["token"],
            "Expiration": _utc_timestamp(latest["expiry_time"]),
        }
        if latest.get("account_id"):
            document["AccountId"] = latest["account_id"]
        return document


class CredentialEndpoint:
    """Serve one set of credentials to child processes on a loopback port.

    Requests must use the random path and send the random token as their
    ``Authorization`` header, the way the SDKs' container credentials
    provider does. Nothing is logged: the request line carries the path.
    """

    def __init__(self, document: Callable[[], dict[str, str]]) -> None:
        self._document = document
        self.token = secrets.token_urlsafe(32)
        self.path = "/" + secrets.token_urlsafe(16)
        endpoint = self

        class _Handler(BaseHTTPRequestHandler):
            timeout = 10

            def do_GET(self) -> None:
                endpoint._answer(self)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}{self.path}"
        threading.Thread(
            target=self._server.serve_forever, name="gco-mcp-role-credentials", daemon=True
        ).start()

    def _answer(self, request: BaseHTTPRequestHandler) -> None:
        supplied = request.headers.get("Authorization", "").encode("latin-1", "replace")
        if request.path != self.path or not hmac.compare_digest(supplied, self.token.encode()):
            self._reply(request, 403, {"message": "forbidden"})
            return
        try:
            document = self._document()
        except Exception as exc:
            _audit_error(os.environ.get("GCO_MCP_ROLE_ARN", ""), f"refresh failed: {exc}")
            self._reply(request, 500, {"message": "the MCP server could not refresh its role"})
            return
        self._reply(request, 200, document)

    @staticmethod
    def _reply(request: BaseHTTPRequestHandler, status: int, body: Mapping[str, str]) -> None:
        payload = json.dumps(body).encode()
        request.send_response(status)
        request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(payload)))
        request.send_header("Cache-Control", "no-store")
        request.end_headers()
        request.wfile.write(payload)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _config_value_lines(key: str, value: Any) -> list[str]:
    if isinstance(value, dict):
        return [f"{key} ="] + [f"    {sub} = {item}" for sub, item in value.items()]
    return [f"{key} = {value}"]


def child_aws_config(full_config: Mapping[str, Any]) -> str:
    """Return an AWS config file with every profile and no credential settings.

    ``full_config`` is botocore's merged view of the config and credentials
    files. Every profile keeps its name and the settings in
    ``_CHILD_PROFILE_SETTINGS``, so a child that selects a profile by name
    still finds it (with its Region), and ``services`` sections are copied
    because they only hold endpoint settings. SSO sessions are dropped.
    """
    lines: list[str] = []
    profiles: Mapping[str, Mapping[str, Any]] = full_config.get("profiles", {})
    for name, settings in profiles.items():
        lines.append("[default]" if name == "default" else f"[profile {name}]")
        for key, value in settings.items():
            if key in _CHILD_PROFILE_SETTINGS:
                lines.extend(_config_value_lines(key, value))
        lines.append("")
    services: Mapping[str, Mapping[str, Any]] = full_config.get("services", {})
    for name, settings in services.items():
        lines.append(f"[services {name}]")
        for key, value in settings.items():
            lines.extend(_config_value_lines(key, value))
        lines.append("")
    return "\n".join(lines)


class ChildCredentials:
    """The role endpoint plus the private files and variables children get."""

    def __init__(self, role: RoleCredentials, full_config: Mapping[str, Any]) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="gco-mcp-aws-"))
        try:
            config = self.directory / "config"
            credentials = self.directory / "credentials"
            boto = self.directory / "boto"
            for path, text in (
                (config, child_aws_config(full_config)),
                (credentials, ""),
                (boto, ""),
            ):
                path.write_text(text, encoding="utf-8")
                path.chmod(0o600)
            self.endpoint = CredentialEndpoint(role.container_document)
        except BaseException:
            shutil.rmtree(self.directory, ignore_errors=True)
            raise
        self.overlay = {
            "AWS_CONTAINER_CREDENTIALS_FULL_URI": self.endpoint.url,
            "AWS_CONTAINER_AUTHORIZATION_TOKEN": self.endpoint.token,
            "AWS_CONFIG_FILE": str(config),
            "AWS_SHARED_CREDENTIALS_FILE": str(credentials),
            "BOTO_CONFIG": str(boto),
        }

    def close(self) -> None:
        self.endpoint.close()
        shutil.rmtree(self.directory, ignore_errors=True)


_child_credentials: ChildCredentials | None = None


def _install_child_credentials(child_credentials: ChildCredentials | None) -> None:
    global _child_credentials
    previous, _child_credentials = _child_credentials, child_credentials
    if previous is not None:
        previous.close()


atexit.register(_install_child_credentials, None)


def child_environment(extra: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """Return the environment for a child process, or ``None`` to inherit the server's.

    Without an assumed role and without ``extra`` the child inherits the
    server's environment unchanged. With a role, the child gets the
    server's environment without :data:`CHILD_CREDENTIAL_VARIABLES`, plus
    the variables that point it at the role endpoint and at the private AWS
    config files. ``extra`` (a tool's secrets, such as
    ``GCO_STUDIO_PASSWORD``) is added last.
    """
    active = _child_credentials
    if active is None and not extra:
        return None
    environment = dict(os.environ)
    if active is not None:
        for name in CHILD_CREDENTIAL_VARIABLES:
            environment.pop(name, None)
        environment.update(active.overlay)
    if extra:
        environment.update(extra)
    return environment


def assume_mcp_role() -> None:
    """Assume the dedicated MCP IAM role if ``GCO_MCP_ROLE_ARN`` is set.

    When the environment variable is set, this function:

    1. Uses the ambient credentials (a transient ``boto3.Session``) as the
       source for ``sts:AssumeRole`` with the configured role ARN, through a
       Regional STS endpoint (the configured Region, else the partition's
       default Region taken from the role ARN).
    2. Installs a default ``boto3`` session whose credentials refresh by
       assuming the role again before they expire.
    3. Starts the loopback endpoint and writes the private AWS config files
       that :func:`child_environment` gives every child process.
    4. Logs a sanitized audit entry (role ARN + expiration). **Credentials
       themselves are never logged.**

    The first ``sts:AssumeRole`` call happens here, so a role the ambient
    credentials cannot assume stops the server at startup instead of
    letting it run with ambient credentials. When the environment variable
    is not set, a debug-level message is logged and the process continues
    with ambient credentials.
    """
    role_arn = os.environ.get("GCO_MCP_ROLE_ARN", "").strip()
    if not role_arn:
        audit_logger.debug(
            json.dumps(
                {
                    "event": "mcp.server.role_assumption.skipped",
                    "reason": "GCO_MCP_ROLE_ARN not set; using ambient credentials",
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )
        )
        return

    try:
        import boto3
    except ImportError:
        _audit_error(role_arn, "boto3 is not installed; cannot assume role")
        raise

    session_name = os.environ.get("GCO_MCP_ROLE_SESSION_NAME", "gco-mcp-server")
    raw_duration = os.environ.get("GCO_MCP_ROLE_DURATION_SECONDS", "3600").strip()
    try:
        duration_seconds = int(raw_duration)
    except ValueError:
        message = (
            "GCO_MCP_ROLE_DURATION_SECONDS must be a whole number of seconds, "
            f"not {raw_duration[:32]!r}"
        )
        _audit_error(role_arn, message)
        raise ValueError(message) from None

    try:
        import botocore.session
        from botocore.credentials import AssumeRoleCredentialFetcher, CredentialProvider

        ambient_botocore = botocore.session.Session()
        ambient_session = boto3.Session(botocore_session=ambient_botocore)
        source_credentials = ambient_session.get_credentials()
        if source_credentials is None:
            raise RuntimeError("no ambient AWS credentials to assume the role with")
        arn_parts = role_arn.split(":")
        partition = arn_parts[1] if len(arn_parts) > 1 else ""
        sts_region = ambient_session.region_name or _PARTITION_STS_REGIONS.get(partition)

        def sts_client(service_name: str, **kwargs: Any) -> Any:
            return ambient_session.client(service_name, region_name=sts_region, **kwargs)

        fetcher = AssumeRoleCredentialFetcher(
            client_creator=sts_client,
            source_credentials=source_credentials,
            role_arn=role_arn,
            extra_args={"RoleSessionName": session_name, "DurationSeconds": duration_seconds},
        )
        role = RoleCredentials(fetcher.fetch_credentials)
        expiration_iso = role.container_document()["Expiration"]

        class _RoleProvider(CredentialProvider):  # type: ignore[misc]
            METHOD = "gco-mcp-role"
            CANONICAL_NAME = "customGcoMcpRole"

            def load(self) -> Any:
                return role.credentials

        default_session = botocore.session.Session()
        resolver = default_session.get_component("credential_provider")
        resolver.insert_before("env", _RoleProvider())
        _install_child_credentials(ChildCredentials(role, ambient_botocore.full_config))
    except Exception as e:
        _audit_error(role_arn, str(e))
        raise
    boto3.setup_default_session(botocore_session=default_session)

    audit_logger.info(
        json.dumps(
            {
                "event": "mcp.server.role_assumption.success",
                "role_arn": role_arn,
                "session_name": session_name,
                "expiration": expiration_iso,
                "children": "container credentials endpoint on 127.0.0.1",
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
    )
