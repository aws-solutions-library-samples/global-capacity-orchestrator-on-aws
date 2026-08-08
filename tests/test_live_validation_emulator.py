"""Unit tests for the harness's verified emulator opt-in.

``scripts/live_release_validation/emulator.py`` is the single seam through
which the local-only harness may run in CI — and only against a proven AWS
emulator. Every rejection branch here is a safety property: if any of them
regressed, a misconfigured CI job could aim the harness at real AWS. The
happy path (a genuine Floci endpoint) is exercised for real by
tests/test_floci_live_validation_e2e.py; these tests pin the fail-closed
logic with mocked STS so they run in the ordinary credential-free suite.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scripts.live_release_validation.emulator import (
    EMULATOR_ENDPOINT_ENV,
    emulator_endpoint_requested,
    verify_emulator_endpoint,
)
from scripts.live_release_validation.runner import require_local_execution


@pytest.fixture()
def emulator_env(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:4566")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "911111111111")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    return "http://127.0.0.1:4566"


def _sts_answering(account: str):
    client = MagicMock()
    client.get_caller_identity.return_value = {"Account": account}
    boto3_module = MagicMock()
    boto3_module.client.return_value = client
    return boto3_module


class TestVerifyEmulatorEndpoint:
    def test_https_endpoint_is_refused(self, emulator_env, monkeypatch):
        monkeypatch.setenv("AWS_ENDPOINT_URL", "https://sts.amazonaws.com")
        with pytest.raises(RuntimeError, match="plain http"):
            verify_emulator_endpoint("https://sts.amazonaws.com")

    def test_non_allowlisted_host_is_refused(self, emulator_env, monkeypatch):
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://internal.example:4566")
        with pytest.raises(RuntimeError, match="not an allowed emulator host"):
            verify_emulator_endpoint("http://internal.example:4566")

    def test_split_endpoint_is_refused(self, emulator_env, monkeypatch):
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:9999")
        with pytest.raises(RuntimeError, match="split-endpoint"):
            verify_emulator_endpoint("http://127.0.0.1:4566")

    def test_realistic_credentials_are_refused(self, emulator_env, monkeypatch):
        # An AKIA-style key id could belong to a real principal; emulator
        # runs must use a fabricated 12-digit account id instead.
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
        with pytest.raises(RuntimeError, match="12-digit"):
            verify_emulator_endpoint("http://127.0.0.1:4566")

    def test_identity_echo_mismatch_is_refused(self, emulator_env):
        with (
            patch.dict("sys.modules", {"boto3": _sts_answering("999999999999")}),
            pytest.raises(RuntimeError, match="does not behave like an emulator"),
        ):
            verify_emulator_endpoint("http://127.0.0.1:4566")

    def test_identity_echo_match_passes(self, emulator_env):
        with patch.dict("sys.modules", {"boto3": _sts_answering("911111111111")}):
            verify_emulator_endpoint("http://127.0.0.1:4566")

    def test_trailing_slash_is_normalized(self, emulator_env):
        with patch.dict("sys.modules", {"boto3": _sts_answering("911111111111")}):
            verify_emulator_endpoint("http://127.0.0.1:4566/")


class TestEndpointRequest:
    def test_unset_and_blank_mean_a_real_run(self, monkeypatch):
        monkeypatch.delenv(EMULATOR_ENDPOINT_ENV, raising=False)
        assert emulator_endpoint_requested() is None
        monkeypatch.setenv(EMULATOR_ENDPOINT_ENV, "   ")
        assert emulator_endpoint_requested() is None

    def test_set_value_is_returned(self, monkeypatch):
        monkeypatch.setenv(EMULATOR_ENDPOINT_ENV, "http://127.0.0.1:4566")
        assert emulator_endpoint_requested() == "http://127.0.0.1:4566"


class TestRequireLocalExecution:
    def test_outside_ci_is_a_noop(self, monkeypatch):
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        require_local_execution()

    def test_ci_without_emulator_is_refused(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv(EMULATOR_ENDPOINT_ENV, raising=False)
        with pytest.raises(RuntimeError, match="local-only"):
            require_local_execution()

    def test_ci_with_emulator_runs_the_verification(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv(EMULATOR_ENDPOINT_ENV, "http://127.0.0.1:4566")
        verified = []
        monkeypatch.setattr(
            "scripts.live_release_validation.emulator.verify_emulator_endpoint",
            lambda endpoint: verified.append(endpoint),
        )
        require_local_execution()
        assert verified == ["http://127.0.0.1:4566"], (
            "CI execution must be permitted only through the emulator verification"
        )

    def test_ci_with_failing_verification_propagates(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv(EMULATOR_ENDPOINT_ENV, "http://127.0.0.1:4566")

        def explode(endpoint):
            raise RuntimeError("endpoint refused")

        monkeypatch.setattr(
            "scripts.live_release_validation.emulator.verify_emulator_endpoint", explode
        )
        with pytest.raises(RuntimeError, match="endpoint refused"):
            require_local_execution()
