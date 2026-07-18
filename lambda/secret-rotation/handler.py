"""
Secrets Manager rotation Lambda for the GCO backend HMAC signing key.

This Lambda handles the four-step rotation protocol for the API Gateway
proxy-to-backend signing key:
1. createSecret - Generate a new random key and store it as AWSPENDING
2. setSecret - No-op (no external system to update)
3. testSecret - Validate the pending key's structure
4. finishSecret - Move AWSPENDING to AWSCURRENT

The key is a cryptographically random string used only to compute and verify
request-bound HMAC envelopes; it is never transmitted to the backend.
Multi-region replication distributes new versions automatically. Proxies and
services accept both AWSCURRENT and AWSPENDING during the rotation window.
"""

import json
import logging
import secrets
import string
from typing import Any

import boto3

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-07-18T01:03:40Z
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/secret-rotation/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/secret-rotation/handler.lambda_handler.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Signing-key configuration
TOKEN_LENGTH = 64
# Alphanumeric output keeps the existing secret JSON schema and avoids escaping.
TOKEN_ALPHABET = string.ascii_letters + string.digits


def lambda_handler(event: dict[str, Any], context: Any) -> None:
    """
    Handle Secrets Manager rotation request.

    Args:
        event: Rotation event with Step, SecretId, ClientRequestToken
        context: Lambda context (unused)

    Raises:
        ValueError: If the rotation step is invalid
    """
    secret_id = event["SecretId"]
    token = event["ClientRequestToken"]
    step = event["Step"]

    logger.info(f"Rotation step '{step}' for secret {secret_id}")

    client = boto3.client("secretsmanager")

    if step == "createSecret":
        create_secret(client, secret_id, token)
    elif step == "setSecret":
        set_secret(client, secret_id, token)
    elif step == "testSecret":
        test_secret(client, secret_id, token)
    elif step == "finishSecret":
        finish_secret(client, secret_id, token)
    else:
        raise ValueError(f"Invalid rotation step: {step}")


def create_secret(client: Any, secret_id: str, token: str) -> None:
    """
    Create a new secret version with AWSPENDING staging label.

    Generates a cryptographically secure random token and stores it
    as the pending version of the secret.

    Args:
        client: Secrets Manager boto3 client
        secret_id: ARN or name of the secret
        token: Client request token for idempotency
    """
    # Check if this version already exists (idempotency)
    try:
        client.get_secret_value(SecretId=secret_id, VersionId=token, VersionStage="AWSPENDING")
        logger.info(f"Secret version {token} already exists as AWSPENDING")
        return
    except client.exceptions.ResourceNotFoundException:
        pass  # Expected - version doesn't exist yet

    # Generate new secure random token
    new_token = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))

    # Create the secret value structure (matching original format)
    secret_value = json.dumps(
        {
            "description": "GCO backend HMAC signing key",
            "token": new_token,
        }
    )

    # Store as AWSPENDING
    client.put_secret_value(
        SecretId=secret_id,
        ClientRequestToken=token,
        SecretString=secret_value,
        VersionStages=["AWSPENDING"],
    )

    logger.info(f"Created new secret version {token} as AWSPENDING")


def set_secret(client: Any, secret_id: str, token: str) -> None:
    """
    Set the secret in the target system.

    For GCO's HMAC signing key, there is no external system to update.
    Proxies and services read staged versions directly from Secrets Manager.

    Args:
        client: Secrets Manager boto3 client
        secret_id: ARN or name of the secret
        token: Client request token for idempotency
    """
    # No-op: No external system to update
    # Services read directly from Secrets Manager and validate both versions
    logger.info("setSecret: No external system to update")


def test_secret(client: Any, secret_id: str, token: str) -> None:
    """
    Test that the pending secret is valid.

    For GCO's signing key, verify that the staged value can be retrieved
    and has the expected structure and length.

    Args:
        client: Secrets Manager boto3 client
        secret_id: ARN or name of the secret
        token: Client request token for idempotency
    """
    # Verify the pending secret can be retrieved and parsed
    response = client.get_secret_value(
        SecretId=secret_id,
        VersionId=token,
        VersionStage="AWSPENDING",
    )

    secret_data = json.loads(response["SecretString"])

    if "token" not in secret_data:
        raise ValueError("Pending secret missing 'token' field")

    if len(secret_data["token"]) != TOKEN_LENGTH:
        raise ValueError(f"Token length mismatch: expected {TOKEN_LENGTH}")

    logger.info("testSecret: Pending secret validated successfully")


def finish_secret(client: Any, secret_id: str, token: str) -> None:
    """
    Finish the rotation by moving AWSPENDING to AWSCURRENT.

    This atomically updates the staging labels so that:
    - The new version becomes AWSCURRENT
    - The old version loses AWSCURRENT (but may retain AWSPREVIOUS)

    Args:
        client: Secrets Manager boto3 client
        secret_id: ARN or name of the secret
        token: Client request token for idempotency
    """
    # Get current version info
    metadata = client.describe_secret(SecretId=secret_id)

    # Find the current version
    current_version = None
    for version_id, stages in metadata.get("VersionIdsToStages", {}).items():
        if "AWSCURRENT" in stages:
            if version_id == token:
                # Already current - rotation already completed
                logger.info(f"Version {token} is already AWSCURRENT")
                return
            current_version = version_id
            break

    # Move AWSPENDING to AWSCURRENT
    client.update_secret_version_stage(
        SecretId=secret_id,
        VersionStage="AWSCURRENT",
        MoveToVersionId=token,
        RemoveFromVersionId=current_version,
    )

    logger.info(f"Rotation complete: {token} is now AWSCURRENT (was {current_version})")
