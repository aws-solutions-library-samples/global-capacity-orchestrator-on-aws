"""Confine a caller-supplied path to an allowlisted root directory.

The local-file metric reader is the only reader that accepts a path into the
host filesystem, so it needs to prove — before opening anything — that the
path stays inside a single allowlisted root. This module owns that proof and
nothing else.

:func:`resolve_within_root` is a pure function of ``(supplied_path, root)``:
it resolves the path with realpath semantics (collapsing ``..`` segments and
following every symlink), then checks the result against the resolved root.
A single containment test catches both attack classes — a path that walks out
with ``..`` segments and a path that stays lexically inside but points at a
symlink whose target lives elsewhere. The helper never opens or reads any
file, and never consults the environment; the caller reads the configured
root once and passes it in as ``root``.
"""

from __future__ import annotations

import os
from pathlib import Path

from .shape import ErrorCode, MetricReaderError


def resolve_within_root(supplied_path: str, root: str) -> Path:
    """Resolve ``supplied_path`` and prove it stays within ``root``.

    The returned path is the fully resolved location to read from, guaranteed
    to live inside the resolved ``root``. Resolution uses realpath semantics:
    ``..`` segments are collapsed and every symlink in the path is followed,
    so a path escapes by either route is caught by the same containment check.

    ``root`` must be a non-empty directory path; the caller is responsible for
    reading it (for example from an environment variable) before calling here.
    An unset or empty ``root`` raises a not-configured error so the reader can
    refuse rather than fall back to some implicit location.

    Raises:
        MetricReaderError: with code ``LOCAL_ROOT_NOT_CONFIGURED`` when
            ``root`` is empty; ``PATH_TRAVERSAL_ESCAPE`` when the supplied
            path's ``..`` segments alone walk outside the root; or
            ``SYMLINK_ESCAPE`` when the path stays lexically inside the root
            but a symlink target points outside it. The supplied path is
            included in the error details for the two escape cases.
    """
    if not root:
        raise MetricReaderError(ErrorCode.LOCAL_ROOT_NOT_CONFIGURED)

    # Absolute, real root: collapses ``..`` and follows any symlinks so the
    # containment comparison below is between two fully-resolved paths.
    real_root = Path(root).resolve()

    supplied = Path(supplied_path)
    # An absolute supplied path is used as-is; a relative one is joined under
    # the root before resolution.
    candidate = supplied if supplied.is_absolute() else (real_root / supplied)
    resolved = candidate.resolve()

    if not resolved.is_relative_to(real_root):
        # The path escaped. Decide which distinct code to surface by asking
        # whether ``..`` segments alone — collapsed lexically, without
        # following any symlink — would already leave the root. If they would,
        # this is a traversal escape; otherwise the lexical path stayed inside
        # and only a symlink target jumped out.
        if _lexically_escapes(supplied, real_root):
            raise MetricReaderError(
                ErrorCode.PATH_TRAVERSAL_ESCAPE,
                {"supplied_path": supplied_path},
            )
        raise MetricReaderError(
            ErrorCode.SYMLINK_ESCAPE,
            {"supplied_path": supplied_path},
        )

    return resolved


def _lexically_escapes(supplied: Path, real_root: Path) -> bool:
    """Return True when collapsing ``..`` alone walks ``supplied`` out of root.

    This mirrors the realpath collapse but stops short of touching the
    filesystem: ``os.path.normpath`` folds ``..`` segments purely lexically
    (it never follows symlinks). The result tells the caller whether an escape
    was caused by the path text itself rather than by a symlink target.
    """
    if supplied.is_absolute():
        lexical = Path(os.path.normpath(supplied))
    else:
        lexical = Path(os.path.normpath(real_root / supplied))
    return not lexical.is_relative_to(real_root)
