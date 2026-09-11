"""Tests for lambda/tls-shared/backend_tls.py — the strict private-root TLS transport.

The proxy Lambdas reach dynamic Global Accelerator / internal-ALB hostnames
while verifying one stable deployment identity against a public trust bundle
held in SSM. These tests pin the settings validation (fail closed on any
missing piece), the trust-bundle sanity checks (no private keys, real PEM),
the pool cache (TTL, bounded stale grace, retry throttling, double-checked
locking) and the reset hook. The canonical copy is the one measured;
``tests/test_lambda_shared_sources.py`` keeps the per-Lambda copies identical.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import urllib3
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from tests._lambda_imports import load_lambda_module

_SERVER_NAME = "gco-backend.internal"
_PARAMETER = "/gco/backend-tls/root-ca"
_REGION = "us-east-1"


def _self_signed_root_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GCO Backend Root")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


ROOT_PEM = _self_signed_root_pem()


@pytest.fixture
def backend_tls(monkeypatch):
    """A freshly loaded module with a complete, valid configuration."""
    monkeypatch.setenv("BACKEND_TLS_SERVER_NAME", _SERVER_NAME)
    monkeypatch.setenv("BACKEND_TLS_ROOT_CA_PARAMETER", _PARAMETER)
    monkeypatch.setenv("BACKEND_TLS_ROOT_CA_REGION", _REGION)
    for name in (
        "BACKEND_TLS_CA_CACHE_TTL_SECONDS",
        "BACKEND_TLS_CA_MAX_STALE_SECONDS",
        "BACKEND_TLS_CA_RETRY_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    return load_lambda_module("tls-shared", module_name="backend_tls")


@pytest.fixture
def ssm(backend_tls):
    """Route ``boto3.client`` to an SSM stub that serves the root bundle."""
    client = MagicMock()
    client.get_parameter.return_value = {"Parameter": {"Value": ROOT_PEM}}
    with patch.object(backend_tls.boto3, "client", return_value=client) as factory:
        yield client, factory


class TestSettings:
    def test_defaults(self, backend_tls):
        assert backend_tls._tls_settings() == (
            _SERVER_NAME,
            _PARAMETER,
            _REGION,
            300.0,
            3600.0,
            5.0,
        )

    def test_server_name_is_normalised(self, backend_tls, monkeypatch):
        monkeypatch.setenv("BACKEND_TLS_SERVER_NAME", f"  {_SERVER_NAME}. ")
        assert backend_tls._tls_settings()[0] == _SERVER_NAME

    @pytest.mark.parametrize("server_name", ["", "   ", "not a host name", "localhost"])
    def test_missing_or_malformed_identity_fails_closed(
        self, backend_tls, monkeypatch, server_name
    ):
        monkeypatch.setenv("BACKEND_TLS_SERVER_NAME", server_name)
        with pytest.raises(RuntimeError, match="server identity is not configured"):
            backend_tls._tls_settings()

    @pytest.mark.parametrize(
        ("parameter", "region"),
        [("", _REGION), ("relative/name", _REGION), (_PARAMETER, ""), (_PARAMETER, "  ")],
    )
    def test_missing_trust_parameter_fails_closed(
        self, backend_tls, monkeypatch, parameter, region
    ):
        monkeypatch.setenv("BACKEND_TLS_ROOT_CA_PARAMETER", parameter)
        monkeypatch.setenv("BACKEND_TLS_ROOT_CA_REGION", region)
        with pytest.raises(RuntimeError, match="trust parameter is not configured"):
            backend_tls._tls_settings()

    def test_tuning_is_bounded_and_stale_never_undercuts_ttl(self, backend_tls, monkeypatch):
        monkeypatch.setenv("BACKEND_TLS_CA_CACHE_TTL_SECONDS", "600")
        monkeypatch.setenv("BACKEND_TLS_CA_MAX_STALE_SECONDS", "10")  # below the TTL
        monkeypatch.setenv("BACKEND_TLS_CA_RETRY_SECONDS", "forever")  # not a number
        *_, ttl, max_stale, retry = backend_tls._tls_settings()
        assert (ttl, max_stale, retry) == (600.0, 600.0, 5.0)

    def test_out_of_range_tuning_falls_back_to_defaults(self, backend_tls, monkeypatch):
        monkeypatch.setenv("BACKEND_TLS_CA_CACHE_TTL_SECONDS", "0")
        monkeypatch.setenv("BACKEND_TLS_CA_MAX_STALE_SECONDS", "90000")
        monkeypatch.setenv("BACKEND_TLS_CA_RETRY_SECONDS", "61")
        *_, ttl, max_stale, retry = backend_tls._tls_settings()
        assert (ttl, max_stale, retry) == (300.0, 3600.0, 5.0)


class TestNewPool:
    def test_builds_a_strict_verifying_pool(self, backend_tls):
        pool = backend_tls._new_pool(_SERVER_NAME, ROOT_PEM)
        assert isinstance(pool, urllib3.PoolManager)
        context = pool.connection_pool_kw["ssl_context"]
        assert context.verify_mode == backend_tls.ssl.CERT_REQUIRED
        assert context.check_hostname is True
        assert context.minimum_version == backend_tls.ssl.TLSVersion.TLSv1_2
        assert pool.connection_pool_kw["server_hostname"] == _SERVER_NAME
        assert pool.connection_pool_kw["assert_hostname"] == _SERVER_NAME
        # urllib3 normalises ``retries=False`` into a disabled Retry object;
        # proxy_utils owns the retry budget, so the transport must not add one.
        assert pool.connection_pool_kw["retries"].total is False

    @pytest.mark.parametrize(
        "bundle",
        [
            "",
            "not a certificate",
            f"-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n{ROOT_PEM}",
        ],
        ids=["empty", "no-certificate", "private-key"],
    )
    def test_non_public_material_is_refused_before_parsing(self, backend_tls, bundle):
        with (
            pytest.raises(RuntimeError, match="invalid public material"),
            patch.object(backend_tls.ssl, "SSLContext") as context_cls,
        ):
            backend_tls._new_pool(_SERVER_NAME, bundle)
        context_cls.assert_not_called()

    def test_malformed_certificates_are_refused(self, backend_tls):
        garbage = "-----BEGIN CERTIFICATE-----\nnot base64 at all\n-----END CERTIFICATE-----\n"
        with pytest.raises(RuntimeError, match="malformed certificates"):
            backend_tls._new_pool(_SERVER_NAME, garbage)


class TestGetBackendHttpPool:
    def test_fetches_the_bundle_once_and_caches_within_ttl(self, backend_tls, ssm):
        client, factory = ssm

        first = backend_tls.get_backend_http_pool()
        second = backend_tls.get_backend_http_pool()

        assert first is second
        factory.assert_called_once_with("ssm", region_name=_REGION)
        client.get_parameter.assert_called_once_with(Name=_PARAMETER)

    def test_refreshes_after_ttl(self, backend_tls, ssm):
        client, _ = ssm
        first = backend_tls.get_backend_http_pool()
        backend_tls._last_successful_refresh = time.monotonic() - 301
        backend_tls._last_refresh_attempt = 0.0

        second = backend_tls.get_backend_http_pool()

        assert second is not first
        assert client.get_parameter.call_count == 2

    def test_refresh_failure_keeps_the_bounded_stale_pool(self, backend_tls, ssm, caplog):
        client, _ = ssm
        first = backend_tls.get_backend_http_pool()
        backend_tls._last_successful_refresh = time.monotonic() - 301
        backend_tls._last_refresh_attempt = 0.0
        client.get_parameter.side_effect = RuntimeError("ssm down")

        with caplog.at_level(logging.WARNING):
            assert backend_tls.get_backend_http_pool() is first
        assert "using bounded stale trust bundle" in caplog.text

    def test_stale_pool_older_than_the_ceiling_fails_closed(self, backend_tls, ssm):
        client, _ = ssm
        backend_tls.get_backend_http_pool()
        backend_tls._last_successful_refresh = time.monotonic() - 3601
        backend_tls._last_refresh_attempt = 0.0
        client.get_parameter.side_effect = RuntimeError("ssm down")

        with pytest.raises(RuntimeError, match="trust bundle is unavailable"):
            backend_tls.get_backend_http_pool()

    def test_no_pool_and_failed_fetch_fails_closed(self, backend_tls, ssm):
        client, _ = ssm
        client.get_parameter.side_effect = RuntimeError("ssm down")

        with pytest.raises(RuntimeError, match="trust bundle is unavailable"):
            backend_tls.get_backend_http_pool()

    def test_invalid_bundle_on_first_fetch_fails_closed(self, backend_tls, ssm):
        client, _ = ssm
        client.get_parameter.return_value = {"Parameter": {"Value": "not a certificate"}}

        with pytest.raises(RuntimeError, match="trust bundle is unavailable"):
            backend_tls.get_backend_http_pool()

    def test_retry_is_throttled_while_the_stale_pool_is_within_grace(self, backend_tls, ssm):
        client, _ = ssm
        first = backend_tls.get_backend_http_pool()
        backend_tls._last_successful_refresh = time.monotonic() - 301
        backend_tls._last_refresh_attempt = time.monotonic()  # a refresh was just attempted

        assert backend_tls.get_backend_http_pool() is first
        assert client.get_parameter.call_count == 1

    def test_settings_are_validated_before_the_cache_is_consulted(
        self, backend_tls, ssm, monkeypatch
    ):
        backend_tls.get_backend_http_pool()
        monkeypatch.setenv("BACKEND_TLS_SERVER_NAME", "")

        with pytest.raises(RuntimeError, match="server identity is not configured"):
            backend_tls.get_backend_http_pool()


class _LockThatLetsAnotherRefreshWin:
    """``_pool_lock`` stand-in: another invocation finishes its refresh first.

    Exercises the double-checked locking in ``get_backend_http_pool`` — the
    caller saw an expired pool, but by the time it holds the lock a concurrent
    caller has already refreshed (or just attempted to), so it must reuse that
    result rather than fetch the bundle again.
    """

    def __init__(self, module, on_acquire):
        self._module = module
        self._on_acquire = on_acquire

    def __enter__(self):
        self._on_acquire(self._module)

    def __exit__(self, *exc_info):
        return False


class TestDoubleCheckedLocking:
    def test_fresh_pool_installed_by_another_caller_is_reused(self, backend_tls, ssm):
        client, _ = ssm
        winner = backend_tls._new_pool(_SERVER_NAME, ROOT_PEM)
        backend_tls._cached_pool = backend_tls._new_pool(_SERVER_NAME, ROOT_PEM)
        backend_tls._last_successful_refresh = time.monotonic() - 301
        backend_tls._last_refresh_attempt = 0.0

        def other_caller_refreshed(module):
            module._cached_pool = winner
            module._last_successful_refresh = time.monotonic()

        backend_tls._pool_lock = _LockThatLetsAnotherRefreshWin(backend_tls, other_caller_refreshed)

        assert backend_tls.get_backend_http_pool() is winner
        client.get_parameter.assert_not_called()

    def test_recent_attempt_by_another_caller_keeps_the_stale_pool(self, backend_tls, ssm):
        client, _ = ssm
        stale = backend_tls._new_pool(_SERVER_NAME, ROOT_PEM)
        backend_tls._cached_pool = stale
        backend_tls._last_successful_refresh = time.monotonic() - 301
        backend_tls._last_refresh_attempt = 0.0

        def other_caller_just_tried(module):
            module._last_refresh_attempt = time.monotonic()

        backend_tls._pool_lock = _LockThatLetsAnotherRefreshWin(
            backend_tls, other_caller_just_tried
        )

        assert backend_tls.get_backend_http_pool() is stale
        client.get_parameter.assert_not_called()


class TestReset:
    def test_reset_forces_a_cold_start(self, backend_tls, ssm):
        client, _ = ssm
        first = backend_tls.get_backend_http_pool()

        backend_tls.reset_backend_tls_cache()

        assert backend_tls._cached_pool is None
        assert backend_tls._last_successful_refresh == 0.0
        assert backend_tls._last_refresh_attempt == 0.0
        assert backend_tls.get_backend_http_pool() is not first
        assert client.get_parameter.call_count == 2
