"""Extended tests for the active Secrets Manager signing-key rotation Lambda.

Covers finishSecret promotion/no-op behavior, pending-key length validation,
and createSecret idempotency and key generation.
"""

import json
from unittest.mock import patch

import pytest

from tests._lambda_imports import load_lambda_module


@pytest.fixture
def rotation_module():
    """Import the secret-rotation handler with a mocked boto3 client."""
    with patch("boto3.client") as mock_client:
        handler = load_lambda_module("secret-rotation")
        yield handler, mock_client


class TestFinishSecret:
    """Tests for finish_secret function."""

    def test_finish_secret_moves_pending_to_current(self, rotation_module):
        handler, mock_client = rotation_module
        client = mock_client.return_value
        client.describe_secret.return_value = {
            "VersionIdsToStages": {
                "old-version": ["AWSCURRENT"],
                "new-version": ["AWSPENDING"],
            }
        }

        handler.finish_secret(client, "test-secret", "new-version")

        client.update_secret_version_stage.assert_called_once_with(
            SecretId="test-secret",
            VersionStage="AWSCURRENT",
            MoveToVersionId="new-version",
            RemoveFromVersionId="old-version",
        )

    def test_finish_secret_already_current(self, rotation_module):
        handler, mock_client = rotation_module
        client = mock_client.return_value
        client.describe_secret.return_value = {"VersionIdsToStages": {"token-123": ["AWSCURRENT"]}}

        handler.finish_secret(client, "test-secret", "token-123")

        client.update_secret_version_stage.assert_not_called()


class TestTestSecretValidation:
    """Tests for test_secret validation."""

    def test_test_secret_wrong_token_length(self, rotation_module):
        handler, mock_client = rotation_module
        client = mock_client.return_value
        client.get_secret_value.return_value = {"SecretString": json.dumps({"token": "short"})}

        with pytest.raises(ValueError, match="Token length mismatch"):
            handler.test_secret(client, "test-secret", "token-123")

    def test_test_secret_valid_token(self, rotation_module):
        handler, mock_client = rotation_module
        client = mock_client.return_value
        client.get_secret_value.return_value = {"SecretString": json.dumps({"token": "a" * 64})}

        handler.test_secret(client, "test-secret", "token-123")

        client.get_secret_value.assert_called_once_with(
            SecretId="test-secret",
            VersionId="token-123",
            VersionStage="AWSPENDING",
        )


class TestCreateSecretIdempotency:
    """Tests for create_secret idempotency."""

    def test_create_secret_skips_if_already_exists(self, rotation_module):
        handler, mock_client = rotation_module
        client = mock_client.return_value
        client.get_secret_value.return_value = {"SecretString": json.dumps({"token": "existing"})}
        client.exceptions.ResourceNotFoundException = type(
            "ResourceNotFoundException", (Exception,), {}
        )

        handler.create_secret(client, "test-secret", "token-123")

        client.put_secret_value.assert_not_called()

    def test_create_secret_generates_correct_length_token(self, rotation_module):
        handler, mock_client = rotation_module
        client = mock_client.return_value
        client.exceptions.ResourceNotFoundException = type(
            "ResourceNotFoundException", (Exception,), {}
        )
        client.get_secret_value.side_effect = client.exceptions.ResourceNotFoundException()

        handler.create_secret(client, "test-secret", "token-123")

        call_args = client.put_secret_value.call_args
        secret_value = json.loads(call_args[1]["SecretString"])
        assert len(secret_value["token"]) == handler.TOKEN_LENGTH
        assert call_args[1]["VersionStages"] == ["AWSPENDING"]
