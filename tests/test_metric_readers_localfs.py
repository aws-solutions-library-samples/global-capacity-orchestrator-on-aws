"""Tests for confining a supplied path to an allowlisted root directory.

The local-file metric reader accepts a path into the host filesystem, so
before it ever opens anything it must prove the path stays inside a single
allowlisted root. ``resolve_within_root`` owns that proof. The contract it
must uphold is a strict safety property: for any input, it either returns a
fully resolved path that lives *inside* the resolved root, or it refuses with
a containment error — it must never hand back a path that escaped the root.

The property test below drives the helper with a wide range of relative and
absolute paths built from ordinary names, ``.`` and ``..`` segments, and a
symlink that points outside the root, then asserts that every outcome is
either an in-root path or one of the three containment errors. A second
property pins down that an unset (empty) root is always refused outright,
without regard to the path supplied.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import importlib
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``mcp/run_mcp.py`` puts ``mcp/`` on ``sys.path`` at runtime; mirror that
# here so the pure ``metric_readers`` package imports the same way it does
# in production, matching the convention used by the sibling tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from metric_readers.localfs import resolve_within_root  # noqa: E402
from metric_readers.shape import ErrorCode, MetricReaderError  # noqa: E402

# The stable codes that signal a path was refused for containment reasons.
# Any one of them is an acceptable, safe outcome; a returned out-of-root path
# is never acceptable.
_CONTAINMENT_CODES = frozenset(
    {
        ErrorCode.PATH_TRAVERSAL_ESCAPE,
        ErrorCode.SYMLINK_ESCAPE,
        ErrorCode.LOCAL_ROOT_NOT_CONFIGURED,
    }
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# An ordinary path-segment name: 1..8 ASCII letters/digits, with no path
# separator and no "." so it cannot itself encode traversal.
_normal_segment = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=127),
    min_size=1,
    max_size=8,
)

# A single path segment drawn from ordinary names plus the three interesting
# special tokens: ".." (lexical traversal), "." (no-op), and "link" — the name
# of a symlink each example creates inside the root that points *outside* it,
# so a path routed through it escapes via the link target rather than via text.
_segment = st.one_of(
    _normal_segment,
    st.just(".."),
    st.just("."),
    st.just("link"),
)

# A path is 0..8 segments joined with "/"; ``absolute`` decides whether it is
# anchored at "/" (almost always escaping a temp-dir root) or treated relative
# to the root.
_segments = st.lists(_segment, min_size=0, max_size=8)


def _build_path(segments: list[str], absolute: bool) -> str:
    """Join drawn segments into a single supplied-path string."""
    joined = "/".join(segments)
    return "/" + joined if absolute else joined


# ---------------------------------------------------------------------------
# Property: a returned path is always inside the resolved root
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(segments=_segments, absolute=st.booleans())
def test_resolve_within_root_returns_in_root_path_or_containment_error(
    segments: list[str],
    absolute: bool,
) -> None:
    """Every outcome is an in-root path or a containment error — never an escape.

    For an arbitrary relative or absolute path (including ``..`` sequences and
    a symlink that points outside the root), ``resolve_within_root`` against a
    valid root directory must either:

    * return a ``Path`` that is contained within the resolved root, or
    * raise ``MetricReaderError`` with a containment code (path-traversal
      escape, symlink escape, or root-not-configured).

    It must never return a path that resolves outside the root.
    """
    with (
        tempfile.TemporaryDirectory() as root_dir,
        tempfile.TemporaryDirectory() as outside_dir,
    ):
        root = Path(root_dir)
        real_root = root.resolve()

        # A symlink inside the root whose target lives outside it, so a path
        # that walks through "link" escapes by following the link rather than
        # by lexical "..". On platforms without symlink support, drop the token.
        link = root / "link"
        try:
            link.symlink_to(outside_dir)
        except OSError, NotImplementedError:
            segments = [s for s in segments if s != "link"]

        supplied = _build_path(segments, absolute)

        try:
            result = resolve_within_root(supplied, str(root))
        except MetricReaderError as exc:
            assert exc.code in _CONTAINMENT_CODES
        else:
            # A successful return must be a path genuinely inside the root.
            assert isinstance(result, Path)
            assert result.is_relative_to(real_root)


# ---------------------------------------------------------------------------
# Property: an unset/empty root is always refused
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(segments=_segments, absolute=st.booleans())
def test_empty_root_always_reports_not_configured(
    segments: list[str],
    absolute: bool,
) -> None:
    """An empty root is refused with the not-configured code for any path.

    When no allowlisted root is configured, the helper must never attempt to
    resolve or return a path; it always raises the root-not-configured error
    regardless of what path was supplied.
    """
    supplied = _build_path(segments, absolute)

    with pytest.raises(MetricReaderError) as excinfo:
        resolve_within_root(supplied, "")

    assert excinfo.value.code == ErrorCode.LOCAL_ROOT_NOT_CONFIGURED


# ===========================================================================
# Local-file confinement integration tests (task 8.7)
# ===========================================================================
#
# The property tests above pin down the *pure* helper. These integration tests
# drive confinement through the gated reader tool itself —
# ``mcp/tools/metrics.py::metrics_from_local_file`` — which reads
# ``GCO_METRICS_LOCAL_ROOT`` from ``os.environ`` once at the boundary and calls
# ``localfs.resolve_within_root``. Because the tool is defined inside an
# ``if is_enabled("GCO_ENABLE_LOCAL_METRICS")`` block, it only exists after the
# flag is set and the module is (re)imported. The three required cases assert
# both the correct Tool_Error_Envelope code AND that **no file outside the
# Local_Root is ever opened** (Requirements 18.8, 18.9, 18.10).


@contextlib.contextmanager
def _local_file_tool(local_root: str | None) -> Iterator:
    """Yield the gated ``tools.metrics`` module loaded under the flag.

    Sets ``GCO_ENABLE_LOCAL_METRICS`` so the decorator inside the
    ``if is_enabled(...)`` block fires, points ``GCO_METRICS_LOCAL_ROOT`` at
    ``local_root`` (or removes it when ``None``, to model an unset root), then
    re-imports ``tools.metrics`` so the gated ``metrics_from_local_file`` tool
    is bound. The module is yielded (not just the tool) so a test can spy on
    the ``_read_local_file`` reader seam to prove no read is attempted. On exit
    the tool is force-unregistered from the module-level FastMCP singleton —
    mirroring the cleanup the destructive-gating tests use — so this gated
    registration never leaks into a sibling test's tool-registry snapshot.
    """
    # ``tools.metrics`` injects ``mcp/`` onto sys.path at import time, but the
    # property tests above already put it there; importing run-time deps below
    # resolves against that same entry.
    import os

    prev_flag = os.environ.get("GCO_ENABLE_LOCAL_METRICS")
    prev_root = os.environ.get("GCO_METRICS_LOCAL_ROOT")
    os.environ["GCO_ENABLE_LOCAL_METRICS"] = "true"
    if local_root is None:
        os.environ.pop("GCO_METRICS_LOCAL_ROOT", None)
    else:
        os.environ["GCO_METRICS_LOCAL_ROOT"] = local_root

    # Re-import under the flag so the gated decorator runs and binds the tool.
    if "tools.metrics" in sys.modules:
        del sys.modules["tools.metrics"]
    metrics_module = importlib.import_module("tools.metrics")

    try:
        yield metrics_module
    finally:
        # Drop the gated registration off the shared FastMCP singleton so the
        # canonical tool-name/tool-count snapshots in sibling tests stay clean.
        with contextlib.suppress(Exception):
            import server

            server.mcp.local_provider.remove_tool("metrics_from_local_file")
        # Restore the environment we mutated.
        for key, prev in (
            ("GCO_ENABLE_LOCAL_METRICS", prev_flag),
            ("GCO_METRICS_LOCAL_ROOT", prev_root),
        ):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev


@contextlib.contextmanager
def _guarded_reader(metrics_module) -> Iterator[tuple[list[Path], list[str]]]:
    """Trip-wire any read of a confined artifact, recording paths opened.

    Confinement must reject before the reader ever touches the filesystem, so
    two seams are watched at once:

    * ``tools.metrics._read_local_file`` — the only function that opens a
      confined artifact. It is replaced with a recorder that captures the
      resolved path it was handed and then delegates to the real reader, so a
      test can assert it was **never reached** in an escape/unconfigured case.
    * ``builtins.open`` — captures the raw path of every file actually opened
      while active, so a test can assert a specific out-of-root target was
      never opened.

    Yields ``(read_calls, opened)``: the resolved paths handed to the reader
    seam, and the raw paths passed to ``open``.
    """
    read_calls: list[Path] = []
    opened: list[str] = []

    real_reader = metrics_module._read_local_file

    def _recording_reader(resolved_path, path, max_bytes):  # type: ignore[no-untyped-def]
        read_calls.append(resolved_path)
        return real_reader(resolved_path, path, max_bytes)

    real_open = builtins.open

    def _recording_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    metrics_module._read_local_file = _recording_reader
    builtins.open = _recording_open
    try:
        yield read_calls, opened
    finally:
        builtins.open = real_open
        metrics_module._read_local_file = real_reader


def _opened_resolved(opened: list[str]) -> set[Path]:
    """Resolve every recorded ``open`` path for membership comparisons."""
    resolved: set[Path] = set()
    for raw in opened:
        with contextlib.suppress(OSError, ValueError):
            resolved.add(Path(raw).resolve())
    return resolved


def test_traversal_escape_returns_envelope_and_reads_nothing(tmp_path: Path) -> None:
    """A ``..``-traversal path escaping the root yields ``path_traversal_escape``.

    Using ``tmp_path`` as the Local_Root, a supplied path whose ``..`` segments
    climb out of the root must be refused with the path-traversal-escape code,
    carry the supplied path in the envelope details, return **no** canonical
    ``metrics`` shape, and read nothing out of root (Requirement 18.8). The
    out-of-root target the path resolves to is proven never opened, and the
    reader seam is proven never reached.
    """
    supplied = "../../etc/passwd"
    # The path the supplied traversal resolves to, lexically, from the root.
    escape_target = (tmp_path / supplied).resolve()

    with (
        _local_file_tool(str(tmp_path)) as metrics_module,
        _guarded_reader(metrics_module) as (read_calls, opened),
    ):
        result = asyncio.run(
            metrics_module.metrics_from_local_file(path=supplied, field="loss", format="json")
        )

    assert result["code"] == ErrorCode.PATH_TRAVERSAL_ESCAPE
    assert result["details"]["supplied_path"] == supplied
    # An escape never produces the Canonical_Metrics_Shape.
    assert "metrics" not in result
    # The reader seam was never reached, so no confined artifact was read.
    assert read_calls == []
    # The out-of-root target the traversal pointed at was never opened.
    assert escape_target not in _opened_resolved(opened)


def test_symlink_escape_returns_envelope_and_never_reads_target(tmp_path: Path) -> None:
    """A symlink inside the root pointing outside yields ``symlink_escape``.

    A real symlink is created *inside* the Local_Root whose target lives
    *outside* it; the in-root symlink path is supplied. The reader must refuse
    with the symlink-escape code, carry the supplied path in the details,
    return **no** canonical ``metrics`` shape, and — critically — never open
    the link target file nor reach the reader seam (Requirement 18.9).
    """
    # The escape target lives outside the root, in a sibling temp dir that the
    # in-root symlink points at. Writing a real file there lets the spy prove
    # it is never opened.
    with tempfile.TemporaryDirectory() as outside_dir:
        outside = Path(outside_dir)
        secret = outside / "secret.json"
        secret.write_text('{"loss": 0.5}')

        link = tmp_path / "link"
        try:
            link.symlink_to(outside)
        except OSError, NotImplementedError:
            pytest.skip("platform does not support symlinks")

        supplied = "link/secret.json"

        with (
            _local_file_tool(str(tmp_path)) as metrics_module,
            _guarded_reader(metrics_module) as (read_calls, opened),
        ):
            result = asyncio.run(
                metrics_module.metrics_from_local_file(path=supplied, field="loss", format="json")
            )

        assert result["code"] == ErrorCode.SYMLINK_ESCAPE
        assert result["details"]["supplied_path"] == supplied
        assert "metrics" not in result
        # The reader seam was never reached.
        assert read_calls == []
        # The link target must never have been opened, by either name.
        opened_resolved = _opened_resolved(opened)
        assert str(secret) not in opened
        assert secret.resolve() not in opened_resolved


def test_unconfigured_root_returns_envelope_and_reads_nothing(tmp_path: Path) -> None:
    """An unset/empty Local_Root yields ``local_root_not_configured``.

    With the gate enabled but ``GCO_METRICS_LOCAL_ROOT`` unset, the reader must
    refuse with the not-configured code, return **no** canonical ``metrics``
    shape, and read no file at all (Requirement 18.10). ``tmp_path`` holds a
    real metrics file the reader would have read had it not refused first;
    proving that file is never opened and the reader seam is never reached
    shows the refusal happens before any read.
    """
    present = tmp_path / "metrics.json"
    present.write_text('{"loss": 0.25}')

    # local_root=None models the unset env var while the gate stays on.
    with (
        _local_file_tool(None) as metrics_module,
        _guarded_reader(metrics_module) as (read_calls, opened),
    ):
        result = asyncio.run(
            metrics_module.metrics_from_local_file(path=str(present), field="loss", format="json")
        )

    assert result["code"] == ErrorCode.LOCAL_ROOT_NOT_CONFIGURED
    assert "metrics" not in result
    # The reader seam was never reached, so nothing was read.
    assert read_calls == []
    # The file that exists under tmp_path was never opened.
    assert str(present) not in opened
    assert present.resolve() not in _opened_resolved(opened)
