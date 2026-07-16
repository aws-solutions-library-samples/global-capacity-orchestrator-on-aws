"""Unit tests for :mod:`tests._lambda_imports`.

The helper is infrastructure that several other test files lean on.
Breaking it silently would break fixture isolation across Lambda
handler tests. These tests pin the contract.

Covered:
- Loads a Lambda module under a unique ``sys.modules`` name.
- Different ``lambda_dir`` values produce different module objects.
- Every call produces a freshly-exec'd module (no session-wide cache).
- ``shared_dirs`` resolves cross-module imports (handler.py importing
  ``proxy_utils`` from a sibling lambda dir).
- Collateral modules imported via ``shared_dirs`` are removed from
  ``sys.modules`` after the load so they don't leak into later loads.
- Standalone loads (no ``shared_dirs``) don't touch unrelated
  ``sys.modules`` entries — third-party globals like ``boto3`` stay
  where they are.
- Path traversal in ``lambda_dir`` or ``shared_dirs`` is rejected.
- Missing target file raises a clean ``ValueError``.
"""

from __future__ import annotations

import sys

import pytest

from tests._lambda_imports import load_lambda_module


class TestUniqueModuleNaming:
    """Loaded modules are registered under a namespace-safe unique name."""

    def test_unique_name_based_on_lambda_dir_and_module(self):
        module = load_lambda_module("secret-rotation")
        assert module.__name__ == "_gco_lambda_secret_rotation_handler"
        assert "_gco_lambda_secret_rotation_handler" in sys.modules

    def test_unique_name_for_non_handler_module(self):
        module = load_lambda_module("proxy-shared", "proxy_utils", shared_dirs=["tls-shared"])
        assert module.__name__ == "_gco_lambda_proxy_shared_proxy_utils"
        assert "_gco_lambda_proxy_shared_proxy_utils" in sys.modules

    def test_different_lambda_dirs_produce_different_modules(self):
        a = load_lambda_module("secret-rotation")
        b = load_lambda_module("proxy-shared", "proxy_utils", shared_dirs=["tls-shared"])
        assert a is not b
        assert a.__name__ != b.__name__


class TestFreshLoadSemantics:
    """Every call performs a fresh module exec so mocks can be reapplied."""

    def test_consecutive_calls_return_different_module_objects(self):
        first = load_lambda_module("secret-rotation")
        second = load_lambda_module("secret-rotation")
        # Two separate module objects — NOT the same cached instance.
        # Fixtures rely on this so their ``patch(...)`` contexts apply
        # to a freshly-exec'd module body on each invocation.
        assert first is not second


class TestSharedDirsResolution:
    """``shared_dirs`` makes cross-module imports resolvable at load time."""

    def test_handler_importing_proxy_utils_loads_successfully(self):
        handler = load_lambda_module("api-gateway-proxy", shared_dirs=["proxy-shared"])
        # handler does ``from proxy_utils import ...`` at module body;
        # if the load succeeded, proxy_utils resolved correctly.
        assert hasattr(handler, "lambda_handler")

    def test_shared_dirs_accepts_list_or_tuple(self):
        # Both container types should work — the helper calls
        # ``tuple(shared_dirs)`` internally so iterables are fine too.
        a = load_lambda_module("api-gateway-proxy", shared_dirs=("proxy-shared",))
        b = load_lambda_module("api-gateway-proxy", shared_dirs=["proxy-shared"])
        assert hasattr(a, "lambda_handler")
        assert hasattr(b, "lambda_handler")


class TestCollateralCleanup:
    """Cross-load isolation — no stale modules in ``sys.modules``."""

    def test_collateral_proxy_utils_is_removed_after_shared_load(self):
        """After loading api-gateway-proxy with shared_dirs, the bare
        ``proxy_utils`` entry Python cached during the load must be
        cleaned up — otherwise the next fixture's load finds it and
        reuses it under stale mocks.
        """
        # Make sure we start clean (not required for correctness, but
        # the assertion is more meaningful).
        sys.modules.pop("proxy_utils", None)

        load_lambda_module("api-gateway-proxy", shared_dirs=["proxy-shared"])

        assert "proxy_utils" not in sys.modules, (
            "Collateral ``proxy_utils`` entry leaked into sys.modules; "
            "later Lambda handler fixtures will see stale first-fixture "
            "state instead of re-importing under their own mocks."
        )

    def test_standalone_load_does_not_pop_unrelated_modules(self):
        """When shared_dirs is empty, the helper must not touch
        ``sys.modules`` entries that were already present.

        The standalone loads hit this path: ``test_cross_region_aggregator``
        loads its handler at module-collection time, and earlier tests
        may have already imported ``boto3``, ``urllib3``, ``botocore``,
        and friends. Aggressive cleanup would break them.
        """
        # Pick a well-known third-party module that every prior test
        # has already imported.
        before = "boto3" in sys.modules

        load_lambda_module("secret-rotation")

        after = "boto3" in sys.modules
        assert before == after, (
            "Standalone load (no shared_dirs) should leave unrelated "
            "sys.modules entries exactly as it found them."
        )


class TestInputValidation:
    """Reject inputs that could traverse outside ``lambda/``."""

    @pytest.mark.parametrize(
        "bad_dir",
        [
            "..",
            "../etc",
            "foo/bar",
            "foo\\bar",
            "secret-rotation/..",
            "..secret-rotation",  # legitimate: edge case, still a single name
        ],
    )
    def test_rejects_path_traversal_in_lambda_dir(self, bad_dir):
        # The ``..secret-rotation`` case is intentionally the edge — the
        # string has no ``/`` and no ``..`` component, so the validator
        # lets it through but the file-exists check rejects it.
        with pytest.raises(ValueError):
            load_lambda_module(bad_dir)

    def test_rejects_path_traversal_in_shared_dirs(self):
        with pytest.raises(ValueError):
            load_lambda_module(
                "api-gateway-proxy",
                shared_dirs=["../other-lambda"],
            )

    def test_missing_file_raises_clean_error(self):
        with pytest.raises(ValueError, match="No file at"):
            load_lambda_module("secret-rotation", module_name="does_not_exist")
