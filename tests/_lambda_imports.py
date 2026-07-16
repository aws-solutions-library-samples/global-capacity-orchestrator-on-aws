"""
Shared helper for loading Lambda handler modules in tests.

Background
----------
Each ``lambda/<name>/`` directory ships a ``handler.py`` (and sometimes
helper modules like ``proxy-shared/proxy_utils.py``) that isn't on the
regular ``sys.path``. Early tests worked around this with the pattern:

    sys.path.insert(0, "lambda/foo")
    sys.modules.pop("handler", None)
    import handler

That works in isolation but leaks across tests. Pytest runs modules in
the same Python process, so the first test to ``import handler`` wins
``sys.modules['handler']``. Any later test that forgets to pop — or
pops in a different order — silently gets the wrong Lambda's handler
loaded under the name ``handler``. We hit this on the v0.1.0 launch
when a new test file's module-level ``sys.path.insert`` collided with
``test_bug_fixes.py``'s inline import and broke CI.

This module provides :func:`load_lambda_module`, which loads the
target module under a unique name via
``importlib.util.spec_from_file_location``. The loaded module is
registered in ``sys.modules`` under a key like
``_gco_lambda_cross_region_aggregator_handler`` — impossible to
collide with anything and immune to the order tests run in.

Each call performs a fresh module load. This matches the semantics of
the ``sys.modules.pop('handler') + import handler`` pattern that
several fixtures rely on: handlers such as
``secret-rotation/handler.py`` create
``boto3.client("secretsmanager")`` at module import time, and fixtures
that wrap the load in ``patch("boto3.client")`` expect the mock to
be applied on every fixture invocation. Caching the module object
would capture the first fixture's mock and reuse it across tests.

Usage
-----

Simple case (no shared dependencies)::

    from tests._lambda_imports import load_lambda_module

    handler = load_lambda_module("cross-region-aggregator")

Handler that imports a shared module (e.g. proxy-shared/proxy_utils.py)::

    handler = load_lambda_module(
        "api-gateway-proxy",
        shared_dirs=["proxy-shared"],
    )

Loading a non-``handler`` module directly::

    proxy_utils = load_lambda_module("proxy-shared", module_name="proxy_utils")
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LAMBDA_ROOT = _PROJECT_ROOT / "lambda"


def load_lambda_module(
    lambda_dir: str,
    module_name: str = "handler",
    *,
    shared_dirs: Iterable[str] = (),
) -> ModuleType:
    """Load a Lambda module by file path under a unique ``sys.modules`` name.

    Replaces the ``sys.path.insert('lambda/foo') + import handler``
    pattern, which pollutes ``sys.modules['handler']`` and causes
    cross-test name collisions.

    Args:
        lambda_dir: Directory under ``lambda/``, e.g. ``"secret-rotation"``
            or ``"proxy-shared"``. Must not contain path separators or
            ``..`` segments — the directory name only.
        module_name: Python module filename without the ``.py``
            extension. Defaults to ``"handler"``. Pass e.g.
            ``"proxy_utils"`` to load a non-handler module.
        shared_dirs: Additional ``lambda/`` subdirectories that the
            target module imports from via ``sys.path`` at its own
            module load time. For example, ``lambda/api-gateway-proxy/
            handler.py`` starts with
            ``from proxy_utils import build_target_url``; to load it
            you must pass ``shared_dirs=["proxy-shared"]`` so the
            ``proxy_utils`` import resolves during ``exec_module``.
            These directories are pushed onto ``sys.path`` for the
            duration of the load only, then removed.

    Returns:
        A freshly-loaded module. Each invocation runs the target's
        module body again; the module is registered in ``sys.modules``
        under a unique name like ``_gco_lambda_secret_rotation_handler``
        so it cannot collide with other Lambda handler modules.

    Raises:
        ValueError: If ``lambda_dir`` contains a path separator or
            ``..``, or if the target file does not exist.
    """
    # Validate input — reject anything that could traverse outside
    # lambda/.
    if "/" in lambda_dir or "\\" in lambda_dir or ".." in lambda_dir.split("/"):
        raise ValueError(
            f"lambda_dir must be a single directory name under lambda/, got {lambda_dir!r}"
        )

    module_path = _LAMBDA_ROOT / lambda_dir / f"{module_name}.py"
    if not module_path.is_file():
        raise ValueError(
            f"No file at {module_path}; checked under {_LAMBDA_ROOT}. "
            f"(lambda_dir={lambda_dir!r}, module_name={module_name!r})"
        )

    unique_name = f"_gco_lambda_{lambda_dir.replace('-', '_')}_{module_name}"

    # Push shared_dirs onto sys.path only for the duration of the load.
    # They need to be on the path when the target does
    # ``from proxy_utils import ...`` inside its module body, because
    # Python resolves that with the regular import machinery.
    #
    # When shared_dirs is non-empty, the load may register collateral
    # modules in ``sys.modules`` (e.g. a bare ``proxy_utils`` entry
    # when ``handler.py`` does ``from proxy_utils import ...``). We
    # snapshot ``sys.modules`` and remove those collateral entries
    # afterward so the next test's fixture gets a fresh re-import
    # under its own mocks — otherwise the stale module with
    # first-test-fixture state leaks forward.
    #
    # We intentionally do NOT clean up collateral modules when
    # shared_dirs is empty: plain ``import boto3`` / ``import urllib3``
    # calls inside a standalone handler shouldn't be disturbed, since
    # those are well-known third-party modules loaded globally by the
    # pytest process. Aggressively popping them breaks unrelated tests
    # that rely on their module state.
    pushed: list[str] = []
    use_collateral_cleanup = bool(tuple(shared_dirs))
    # Re-materialize shared_dirs since the bool() check above may have
    # consumed a one-shot iterable.
    shared_dirs = tuple(shared_dirs) if not isinstance(shared_dirs, (list, tuple)) else shared_dirs
    sys_modules_snapshot = set(sys.modules) if use_collateral_cleanup else set()
    try:
        for shared in shared_dirs:
            if "/" in shared or "\\" in shared or ".." in shared.split("/"):
                raise ValueError(
                    f"shared_dirs entries must be single directory names "
                    f"under lambda/, got {shared!r}"
                )
            shared_path = str(_LAMBDA_ROOT / shared)
            sys.path.insert(0, shared_path)
            pushed.append(shared_path)

        spec = importlib.util.spec_from_file_location(unique_name, module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(
                f"Failed to build module spec for {module_path} under name {unique_name!r}"
            )
        module = importlib.util.module_from_spec(spec)
        # Register BEFORE exec_module so relative imports inside the
        # target can find the module if they happen to re-enter.
        sys.modules[unique_name] = module
        spec.loader.exec_module(module)
    finally:
        # Unwind sys.path insertions in reverse order so we leave the
        # path exactly as we found it.
        for shared_path in reversed(pushed):
            with contextlib.suppress(ValueError):
                # Already removed by something else — harmless.
                sys.path.remove(shared_path)
        # Remove any collateral modules imported during the load. Only
        # runs when shared_dirs is non-empty so we don't disturb
        # third-party globals for standalone handler loads.
        if use_collateral_cleanup:
            for name in list(sys.modules):
                if name != unique_name and name not in sys_modules_snapshot:
                    del sys.modules[name]

    return module
