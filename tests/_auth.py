"""Authentication helpers for tests that exercise non-authentication behavior."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

_TEST_SIGNING_KEY = "test-backend-signing-key"  # nosec B105 - test fixture


@contextmanager
def bypass_backend_auth() -> Iterator[None]:
    """Keep auth configured while bypassing signature checks in unrelated tests.

    AuthenticationMiddleware still executes and sees a healthy signing-key
    cache. Only its cryptographic verifier is replaced, so route and middleware
    ordering remain realistic without forcing business-logic tests to duplicate
    HMAC request construction. The dedicated auth suites exercise real signing.
    """
    import gco.services.auth_middleware as auth_module

    original_tokens = set(auth_module._cached_tokens)
    original_timestamp = auth_module._cache_timestamp
    original_successful_refresh = auth_module._last_successful_refresh
    original_refresh_attempt = auth_module._last_refresh_attempt
    original_nonces = dict(auth_module._seen_nonces)
    now = time.monotonic()
    auth_module._cached_tokens = {_TEST_SIGNING_KEY}
    auth_module._cache_timestamp = now
    auth_module._last_successful_refresh = now
    auth_module._last_refresh_attempt = now
    auth_module._seen_nonces.clear()

    try:
        with patch.object(
            auth_module,
            "_has_valid_signature",
            new=AsyncMock(return_value=True),
        ):
            yield
    finally:
        auth_module._cached_tokens = original_tokens
        auth_module._cache_timestamp = original_timestamp
        auth_module._last_successful_refresh = original_successful_refresh
        auth_module._last_refresh_attempt = original_refresh_attempt
        auth_module._seen_nonces.clear()
        auth_module._seen_nonces.update(original_nonces)
