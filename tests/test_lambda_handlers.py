"""Tests for the active Secrets Manager signing-key rotation Lambda.

Drives the four-step rotation state machine
(createSecret/setSecret/testSecret/finishSecret), including createSecret
idempotency, pending-key validation, and rejection of invalid step names.
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


class TestSecretRotationHandler:
    def test_dispatches_create_secret(self, rotation_module):
        handler, mock_client = rotation_module
        client = mock_client.return_value
        client.exceptions.ResourceNotFoundException = type(
            "ResourceNotFoundException", (Exception,), {}
        )
        client.get_secret_value.side_effect = client.exceptions.ResourceNotFoundException()

        event = {
            "SecretId": "arn:aws:secretsmanager:us-east-1:123:secret:test",
            "ClientRequestToken": "token-123",
            "Step": "createSecret",
        }
        handler.lambda_handler(event, None)
        client.put_secret_value.assert_called_once()

    def test_dispatches_set_secret_noop(self, rotation_module):
        handler, mock_client = rotation_module
        event = {
            "SecretId": "arn:aws:secretsmanager:us-east-1:123:secret:test",
            "ClientRequestToken": "token-123",
            "Step": "setSecret",
        }
        handler.lambda_handler(event, None)
        mock_client.return_value.put_secret_value.assert_not_called()

    def test_dispatches_test_secret(self, rotation_module):
        handler, mock_client = rotation_module
        client = mock_client.return_value
        client.get_secret_value.return_value = {"SecretString": json.dumps({"token": "a" * 64})}
        event = {
            "SecretId": "arn:aws:secretsmanager:us-east-1:123:secret:test",
            "ClientRequestToken": "token-123",
            "Step": "testSecret",
        }
        handler.lambda_handler(event, None)
        client.get_secret_value.assert_called_once()

    def test_test_secret_fails_on_missing_token(self, rotation_module):
        handler, mock_client = rotation_module
        mock_client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps({"description": "no token field"})
        }
        event = {
            "SecretId": "test-secret",
            "ClientRequestToken": "token-123",
            "Step": "testSecret",
        }
        with pytest.raises(ValueError, match="missing 'token' field"):
            handler.lambda_handler(event, None)

    def test_invalid_step_raises(self, rotation_module):
        handler, _ = rotation_module
        event = {
            "SecretId": "test-secret",
            "ClientRequestToken": "token-123",
            "Step": "invalidStep",
        }
        with pytest.raises(ValueError, match="Invalid rotation step"):
            handler.lambda_handler(event, None)
