"""
IAM role assumption for the GCO MCP server.

When ``GCO_MCP_ROLE_ARN`` is set, assumes the dedicated MCP IAM role at
startup so every boto3 client downstream uses reduced-scope credentials.
"""

import json
import logging
import os
from datetime import UTC, datetime

audit_logger = logging.getLogger("gco.mcp.audit")


def assume_mcp_role() -> None:
    """Assume the dedicated MCP IAM role if ``GCO_MCP_ROLE_ARN`` is set.

    When the environment variable is set, this function:

    1. Uses ambient credentials (via a transient ``boto3.Session``) to call
       ``sts:AssumeRole`` with the configured role ARN.
    2. Builds a new ``boto3.Session`` from the temporary credentials and
       installs it as the default session (``boto3.setup_default_session``)
       so that every subsequent boto3 client in this process uses
       the least-privilege role automatically.
    3. Logs a sanitized audit entry (role ARN + expiration) via the audit
       logger. **Credentials themselves are never logged.**

    When the environment variable is not set, a debug-level message is
    logged and the process continues with ambient credentials.
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
        audit_logger.error(
            json.dumps(
                {
                    "event": "mcp.server.role_assumption.error",
                    "role_arn": role_arn,
                    "error": "boto3 is not installed; cannot assume role",
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )
        )
        raise

    session_name = os.environ.get("GCO_MCP_ROLE_SESSION_NAME", "gco-mcp-server")
    duration_seconds = int(os.environ.get("GCO_MCP_ROLE_DURATION_SECONDS", "3600"))

    try:
        ambient_session = boto3.Session()
        sts = ambient_session.client("sts")
        response = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            DurationSeconds=duration_seconds,
        )
    except Exception as e:
        audit_logger.error(
            json.dumps(
                {
                    "event": "mcp.server.role_assumption.error",
                    "role_arn": role_arn,
                    "error": str(e)[:200],
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )
        )
        raise

    credentials = response["Credentials"]
    expiration = credentials["Expiration"]
    expiration_iso = expiration.isoformat() if hasattr(expiration, "isoformat") else str(expiration)

    boto3.setup_default_session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
    )

    audit_logger.info(
        json.dumps(
            {
                "event": "mcp.server.role_assumption.success",
                "role_arn": role_arn,
                "session_name": session_name,
                "expiration": expiration_iso,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
    )
