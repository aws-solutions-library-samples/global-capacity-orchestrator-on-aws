"""Idempotently insert ``# Flowchart:`` markers into source files.

The marker sits right under the module docstring and points readers to
the generated flowchart artifacts. Re-running the generator is safe:
existing marker blocks (identified by a sentinel) are replaced in place
rather than duplicated.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import warnings
from collections import defaultdict
from pathlib import Path

from diagrams.code_diagrams._renderer import RenderedTarget

SENTINEL = "pyflowchart-code-diagram"
_BLOCK_RE = re.compile(
    rf"(?s)# <{re.escape(SENTINEL)}> BEGIN.*?# <{re.escape(SENTINEL)}> END\n?",
)


def upsert_markers(
    results: list[RenderedTarget],
    *,
    project_root: Path,
) -> None:
    """Add or refresh a pointer comment in every source file we charted.

    Multiple targets from the same source file collapse into a single
    comment block that lists every flowchart produced for that file.
    After writing the marker, each touched file is normalised with
    ``ruff format`` so the resulting layout is formatter-stable —
    otherwise the marker's leading/trailing blank lines can compose
    with the source file's existing PEP 8 spacing into three-blank-line
    runs that break ``ruff format --check``.
    """
    by_source: dict[Path, list[RenderedTarget]] = defaultdict(list)
    for result in results:
        by_source[project_root / result.target.source].append(result)

    touched: list[Path] = []
    for source_path, source_results in by_source.items():
        if _update_file(
            source_path=source_path,
            results=source_results,
            project_root=project_root,
        ):
            touched.append(source_path)

    if touched:
        _ruff_format(touched, project_root=project_root)


def _ruff_format(paths: list[Path], *, project_root: Path) -> None:
    """Run ``ruff format`` on ``paths`` so the marker insertion is formatter-stable.

    Uses ``python -m ruff`` from the current interpreter so the check
    works whether ``ruff`` is on PATH or only importable. Silently
    no-ops if ``ruff`` isn't importable at all — the generator still
    works, the contributor just has to run ``ruff format`` themselves later.
    """
    try:
        import ruff  # noqa: F401
    except ImportError:
        warnings.warn(
            "ruff is not installed — skipping post-marker normalisation. "
            "Install with ``pip install -e '.[diagrams]'``; the marker block "
            "may still land in a shape ruff later reformats.",
            stacklevel=2,
        )
        return

    rels = [str(p.relative_to(project_root)) for p in paths]
    # Invoke ruff directly so we inherit its exit code + stdout.
    subprocess.run(  # noqa: S603 — args are fully-known paths we just generated
        [sys.executable, "-m", "ruff", "format", "--quiet", *rels],
        cwd=str(project_root),
        check=False,
    )


def _update_file(
    *,
    source_path: Path,
    results: list[RenderedTarget],
    project_root: Path,
) -> bool:
    """Insert or replace the marker block in ``source_path``.

    Returns ``True`` iff the file was actually modified. Implementation
    note: we always *strip* any existing marker first, then re-insert
    at the current ``_insertion_point`` offset. Doing an in-place
    ``re.sub`` when the block is present is correct for the idempotent
    case, but fails silently when the placement rules change (e.g.
    when we moved the block from "after ``from __future__ import ...``"
    to "after all imports"). A strip-then-insert pipeline also makes
    ``--skip-marker=False`` + an upstream schema change land the marker
    in the right spot without the user having to run a separate
    cleanup pass.

    We don't try to normalise whitespace here — :func:`upsert_markers`
    runs ``ruff format`` on every touched file after all insertions
    complete, which handles the PEP 8 blank-line spacing consistently.
    """
    original = source_path.read_text(encoding="utf-8")
    stripped = strip_markers_from(original)
    block = _format_block(results=results, project_root=project_root)

    insertion_point = _insertion_point(stripped)
    updated = stripped[:insertion_point] + block + stripped[insertion_point:]

    if updated != original:
        source_path.write_text(updated, encoding="utf-8")
        print(f"   🖋  marker inserted/refreshed in {source_path.relative_to(project_root)}")
        return True
    return False


def strip_markers_from(source: str) -> str:
    """Return ``source`` with any existing marker block removed.

    Kept as a public helper so the CLI ``--strip-markers`` flag can
    reuse the exact same regex. If no marker block is present the
    source is returned unchanged. When a marker block is removed we
    collapse the resulting *four*-or-more consecutive newlines down
    to a single three-newline run (i.e. two blank lines) — that
    preserves the PEP 8 ``two-blank-lines-between-top-level-defs``
    requirement ruff format enforces, which would otherwise be broken
    when the marker block lived between two top-level defs and removing
    it fused their trailing and leading blank-line padding into a
    four-newline run. Files with legitimate triple-blank-line runs
    unrelated to a marker are left unchanged.
    """
    if SENTINEL not in source:
        return source
    without_block = _BLOCK_RE.sub("", source)
    return re.sub(r"\n{4,}", "\n\n\n", without_block)


def strip_all_markers(project_root: Path) -> int:
    """Remove every marker block under ``project_root``.

    Walks the standard source roots — ``app.py``, ``cli/``, ``gco/``,
    ``gco_mcp/``, and ``lambda/`` (excluding the kubectl-applier-simple-build
    and helm-installer-build packaged bundles) — and rewrites any file
    that actually contains a marker. Files without the sentinel are
    left untouched (even if they have triple-blank-line runs
    unrelated to this feature). Returns the number of files modified.
    """
    modified = 0
    search_roots: list[Path] = [
        project_root / "app.py",
        *(project_root / "cli").rglob("*.py"),
        *(project_root / "gco").rglob("*.py"),
        *(project_root / "gco_mcp").rglob("*.py"),
        *(project_root / "lambda").rglob("*.py"),
    ]
    skip_fragments = ("kubectl-applier-simple-build", "helm-installer-build")
    for source_path in search_roots:
        if not source_path.is_file():
            continue
        if any(frag in str(source_path) for frag in skip_fragments):
            continue
        original = source_path.read_text(encoding="utf-8")
        if SENTINEL not in original:
            continue
        stripped = strip_markers_from(original)
        if stripped != original:
            source_path.write_text(stripped, encoding="utf-8")
            print(f"   🧹 stripped marker from {source_path.relative_to(project_root)}")
            modified += 1
    return modified


def _format_block(
    *,
    results: list[RenderedTarget],
    project_root: Path,
) -> str:
    """Build the comment block that points at the generated artifacts.

    The block is preceded *and* followed by a blank line so it cleanly
    separates from the surrounding statements — otherwise ruff's
    ``I001`` rule treats the comment as part of the import block above,
    and PEP 8 enforcement complains about the single blank line between
    the marker and a class/def below. Two blank-line separators work in
    every context we insert into (after docstring, after ``from __future__``
    imports, after a regular import block, before a class/def/module-level
    statement).
    """
    lines = ["", f"# <{SENTINEL}> BEGIN - auto-inserted, do not edit"]
    lines.append("# Flowchart(s) generated from this file:")
    for result in results:
        html_rel = result.html_path.relative_to(project_root)
        lines.append(f"#   * ``{result.target.function}`` -> ``{html_rel}``")
        if result.png_path is not None:
            png_rel = result.png_path.relative_to(project_root)
            lines.append(f"#     (PNG: ``{png_rel}``)")
    lines.append(
        "# Regenerate with ``python diagrams/code_diagrams/generate.py``.",
    )
    lines.append(f"# <{SENTINEL}> END")
    # Trailing "" plus the final "\n" from ``join`` ensures the block
    # ends with an empty line — combined with whatever line-terminator
    # is already present in the source at the insertion point, this
    # gives us the two-blank-line separator ruff format expects before
    # the next class or def.
    lines.append("")
    return "\n".join(lines) + "\n"


def _insertion_point(source: str) -> int:
    """Return the character offset where the marker block should go.

    Places the block immediately after the module docstring and the
    full block of top-level imports (``import``, ``from`` — including
    ``from __future__ import …``), but before the first real
    statement. This placement keeps ruff's import sorter happy: it
    groups consecutive imports, and a comment block slotted in the
    middle of that group would be treated as a section boundary that
    forces reordering.

    Falls back to offset 0 if the file has no docstring and no imports.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - defensive
        return 0

    last_prelude_end_line = 0

    # Module docstring: the first statement is a bare string expression.
    body_iter = iter(tree.body)
    first = next(body_iter, None)
    if (
        first is not None
        and isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        last_prelude_end_line = first.end_lineno or 0
    else:
        # No docstring — the first statement (if any) is already
        # imports / code, so reset the iterator to include it.
        body_iter = iter(tree.body)

    # Walk every subsequent top-level ``import`` / ``from ... import ...``
    # statement. The first non-import node terminates the prelude.
    for node in body_iter:
        if isinstance(node, ast.Import | ast.ImportFrom):
            last_prelude_end_line = max(last_prelude_end_line, node.end_lineno or 0)
        else:
            break

    if last_prelude_end_line == 0:
        return 0

    # Convert line number (1-indexed, inclusive) -> char offset after
    # the newline that terminates that line.
    offset = 0
    for _ in range(last_prelude_end_line):
        newline = source.find("\n", offset)
        if newline == -1:
            return len(source)
        offset = newline + 1
    return offset
