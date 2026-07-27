"""
Tests for the spot price gate fields on POST /api/v1/queue/jobs.

Exercises the QueuedJobRequest surface: a valid cap+type pair reaches the
job store as a canonically-serialized pair, half-specified or malformed
pairs are rejected with 422 before any store write, gate fields fold into
the idempotency request hash (so a replay with a different cap conflicts
rather than silently replaying), and gate-free submissions hash exactly as
they did before the fields existed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from unittest.mock import MagicMock, patch

import pytest

_TEST_SIGNING_KEY = "test-spot-gate-signing-key"  # nosec B105 - test-only key


def _sign_request(request) -> None:
    """Attach a unique HMAC envelope after TestClient serializes the request."""
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    raw_target = request.url.raw_path
    target = raw_target.decode("ascii") if isinstance(raw_target, bytes) else str(raw_target)
    body = bytes(request.content)
    content_hash = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(["v1", timestamp, nonce, request.method.upper(), target, content_hash])
    signature = hmac.new(_TEST_SIGNING_KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    request.headers.update(
        {
            "x-gco-signature-version": "v1",
            "x-gco-signature": signature,
            "x-gco-timestamp": timestamp,
            "x-gco-nonce": nonce,
            "x-gco-content-sha256": content_hash,
        }
    )


@pytest.fixture(autouse=True)
def _seed_auth_cache(monkeypatch):
    """Seed a fresh signing key and sign every serialized TestClient request."""
    from fastapi.testclient import TestClient

    import gco.services.auth_middleware as auth_module

    original_send = TestClient.send

    def send_with_signature(client, request, *args, **kwargs):
        _sign_request(request)
        return original_send(client, request, *args, **kwargs)

    auth_module.clear_token_cache()
    now = time.monotonic()
    auth_module._cached_tokens = {_TEST_SIGNING_KEY}
    auth_module._cache_timestamp = now
    auth_module._last_successful_refresh = now
    auth_module._last_refresh_attempt = now
    auth_module._secrets_client = None
    monkeypatch.setattr(TestClient, "send", send_with_signature)
    yield
    auth_module.clear_token_cache()
    auth_module._secrets_client = None


@pytest.fixture
def mock_manifest_processor():
    mock_processor = MagicMock()
    mock_processor.cluster_id = "test-cluster"
    mock_processor.region = "us-east-1"
    mock_processor.allowed_namespaces = {"gco-jobs"}
    mock_processor.validation_enabled = True
    return mock_processor


def _manifest() -> dict:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "priced-job"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": "main", "image": "test:latest"}],
                    "restartPolicy": "Never",
                }
            }
        },
    }


def _submit(mock_manifest_processor, body: dict, mock_job_store=None, headers=None):
    mock_job_store = mock_job_store or MagicMock()
    mock_job_store.submit_job.return_value = {"job_id": "j-1", "status": "queued"}
    with (
        patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ),
        patch("gco.services.manifest_api.get_template_store", return_value=MagicMock()),
        patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
        patch("gco.services.manifest_api.get_job_store", return_value=mock_job_store),
    ):
        import gco.services.manifest_api as api_module

        api_module.job_store = mock_job_store

        from fastapi.testclient import TestClient

        from gco.services.manifest_api import app

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/api/v1/queue/jobs", json=body, headers=headers or {})
    return response, mock_job_store


class TestSpotGateSubmission:
    def test_valid_gate_pair_reaches_the_store_canonically(self, mock_manifest_processor):
        body = {
            "manifest": _manifest(),
            "target_region": "us-east-1",
            "namespace": "gco-jobs",
            "max_spot_price": 0.5,
            "spot_instance_type": "g5.xlarge",
        }
        response, store = _submit(mock_manifest_processor, body)
        assert response.status_code == 201
        kwargs = store.submit_job.call_args.kwargs
        assert kwargs["spot_max_price"] == "0.500000"
        assert kwargs["spot_instance_type"] == "g5.xlarge"

    def test_gate_free_submission_passes_none_fields(self, mock_manifest_processor):
        body = {
            "manifest": _manifest(),
            "target_region": "us-east-1",
            "namespace": "gco-jobs",
        }
        response, store = _submit(mock_manifest_processor, body)
        assert response.status_code == 201
        kwargs = store.submit_job.call_args.kwargs
        assert kwargs["spot_max_price"] is None
        assert kwargs["spot_instance_type"] is None

    def test_price_without_type_is_422(self, mock_manifest_processor):
        body = {
            "manifest": _manifest(),
            "target_region": "us-east-1",
            "namespace": "gco-jobs",
            "max_spot_price": 0.5,
        }
        response, store = _submit(mock_manifest_processor, body)
        assert response.status_code == 422
        assert "together" in response.json()["detail"]
        store.submit_job.assert_not_called()

    def test_type_without_price_is_422(self, mock_manifest_processor):
        body = {
            "manifest": _manifest(),
            "target_region": "us-east-1",
            "namespace": "gco-jobs",
            "spot_instance_type": "g5.xlarge",
        }
        response, store = _submit(mock_manifest_processor, body)
        assert response.status_code == 422
        store.submit_job.assert_not_called()

    def test_malformed_instance_type_is_422(self, mock_manifest_processor):
        body = {
            "manifest": _manifest(),
            "target_region": "us-east-1",
            "namespace": "gco-jobs",
            "max_spot_price": 0.5,
            "spot_instance_type": "NOT_AN_INSTANCE",
        }
        response, store = _submit(mock_manifest_processor, body)
        assert response.status_code == 422
        store.submit_job.assert_not_called()

    def test_non_positive_price_is_rejected_by_the_model(self, mock_manifest_processor):
        body = {
            "manifest": _manifest(),
            "target_region": "us-east-1",
            "namespace": "gco-jobs",
            "max_spot_price": 0,
            "spot_instance_type": "g5.xlarge",
        }
        response, store = _submit(mock_manifest_processor, body)
        assert response.status_code == 422
        store.submit_job.assert_not_called()

    def test_absurd_price_is_422(self, mock_manifest_processor):
        body = {
            "manifest": _manifest(),
            "target_region": "us-east-1",
            "namespace": "gco-jobs",
            "max_spot_price": 5_000.0,
            "spot_instance_type": "g5.xlarge",
        }
        response, store = _submit(mock_manifest_processor, body)
        assert response.status_code == 422
        store.submit_job.assert_not_called()


class TestSpotGateIdempotencyHash:
    def test_gate_fields_change_the_request_hash(self, mock_manifest_processor):
        base = {
            "manifest": _manifest(),
            "target_region": "us-east-1",
            "namespace": "gco-jobs",
        }
        gated = {**base, "max_spot_price": 0.5, "spot_instance_type": "g5.xlarge"}
        headers = {"Idempotency-Key": "same-key"}

        _, store_plain = _submit(mock_manifest_processor, base, headers=headers)
        _, store_gated = _submit(mock_manifest_processor, gated, headers=headers)

        plain_hash = store_plain.submit_job.call_args.kwargs["request_hash"]
        gated_hash = store_gated.submit_job.call_args.kwargs["request_hash"]
        assert plain_hash != gated_hash

    def test_gate_free_hash_matches_the_historical_payload_shape(self, mock_manifest_processor):
        """Pre-gate deployments hashed exactly this payload; replay must hold."""
        body = {
            "manifest": _manifest(),
            "target_region": "us-east-1",
            "namespace": "gco-jobs",
            "priority": 0,
        }
        _, store = _submit(mock_manifest_processor, body, headers={"Idempotency-Key": "legacy-key"})
        observed = store.submit_job.call_args.kwargs["request_hash"]

        manifest = dict(_manifest())
        manifest["metadata"] = {**manifest["metadata"], "namespace": "gco-jobs"}
        legacy_payload = {
            "manifest": manifest,
            "target_region": "us-east-1",
            "namespace": "gco-jobs",
            "priority": 0,
            "labels": {},
        }
        expected = hashlib.sha256(
            json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert observed == expected
