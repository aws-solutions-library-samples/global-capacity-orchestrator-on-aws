"""Patch a live-validation helper wherever the package binds its name.

The live-validation harness is split into focused modules
(``scripts/live_release_validation/{actions,checks,cleanup,ownership}``), and
each module imports the helpers it needs by name. Patching only the module
that *defines* a helper would therefore miss the binding the code under test
actually calls, so a test would silently exercise the real implementation.

``patch_live_validation_helper`` installs one shared mock into every module in
the package that binds the given name, which keeps a test honest no matter
which module currently consumes the helper. That means moving a helper between
modules stays a pure refactor: no test needs a new patch target.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from types import ModuleType
from typing import Any
from unittest.mock import DEFAULT, MagicMock, patch

import scripts.live_release_validation as _package

_UNSET = object()


def live_validation_modules() -> list[ModuleType]:
    """Import and return every module in the live-validation package.

    Discovery is dynamic on purpose: hard-coding the module list would drift
    the moment someone adds one, and a missed module means a patch silently
    fails to cover the binding the code under test actually calls. Every name
    comes from walking this one package's own ``__path__``, and the prefix
    assertion below rejects anything that is not a submodule of it, so no
    caller-supplied value can reach the import.
    """
    prefix = f"{_package.__name__}."
    modules: list[ModuleType] = [_package]
    for info in pkgutil.walk_packages(_package.__path__, prefix):
        if not info.name.startswith(prefix):
            raise AssertionError(
                f"Refusing to import {info.name!r}: not a submodule of {_package.__name__}"
            )
        # The name is produced by walk_packages over this package's own
        # __path__ and is prefix-asserted above; it is never user input.
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        modules.append(importlib.import_module(info.name))
    return modules


@contextmanager
def patch_live_validation_helper(
    name: str,
    new: Any = _UNSET,
    **mock_kwargs: Any,
) -> Iterator[Any]:
    """Bind one mock for ``name`` in every package module that references it.

    Args:
        name: The helper's attribute name, e.g. ``"_register_job"``.
        new: An explicit replacement object, accepted positionally to mirror
            ``unittest.mock.patch.object``. When omitted a ``MagicMock`` built
            from ``mock_kwargs`` (``return_value``, ``side_effect``, ...) is
            shared across every patched module, so call assertions see the
            complete set of calls.

    Yields:
        The single replacement object installed in every binding module.

    Raises:
        AssertionError: If no module in the package binds ``name``. This turns
            a stale patch target into an immediate, explicit failure instead
            of a test that quietly runs production code.
    """
    targets = [module for module in live_validation_modules() if hasattr(module, name)]
    if not targets:
        raise AssertionError(
            f"No live-validation module binds {name!r}; the patch target is stale. "
            "Check scripts/live_release_validation/ for the helper's current name."
        )

    replacement = MagicMock(**mock_kwargs) if new is _UNSET else new
    with ExitStack() as stack:
        for module in targets:
            stack.enter_context(patch.object(module, name, replacement))
        yield replacement


__all__ = ["DEFAULT", "live_validation_modules", "patch_live_validation_helper"]
