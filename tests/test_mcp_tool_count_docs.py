"""Guard against MCP tool-count drift in human-facing docs.

The registered tool inventory is pinned by ``test_mcp_server.py`` (exact count +
name set) and every tool name is documented by ``test_docs_coverage.py``. But
the *human-readable* headline counts — "N tools by default (up to M with all
flags enabled)" — live in three prose files and had silently drifted (README
headlines said 98/130 while the server actually registered 109/144) because
nothing checked them against reality.

This computes the live counts by enumerating the FastMCP registry in a
subprocess (a clean environment for the default count, ``GCO_ENABLE_ALL_TOOLS``
for the ceiling — mirroring ``test_docs_coverage``'s subprocess enumeration so
import-time flag registration the other MCP tests rely on isn't perturbed) and
asserts every count quoted in the docs matches. Adding a tool now fails here
until the headline numbers are refreshed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Files that quote the tool count in prose, with the regexes that pull the
# "default" and "ceiling" numbers out of each. A file may quote the pair more
# than once (e.g. the top-level README mentions it in the intro and the repo
# tree); every occurrence must agree with the live count.
_DEFAULT_PATTERNS = (
    re.compile(r"(\d+)\s+tools?\s+by\s+default", re.IGNORECASE),
    re.compile(r"(\d+)\s+tools?\s+default\b", re.IGNORECASE),
    re.compile(r"exposes\s+(\d+)\s+tools?\b", re.IGNORECASE),
)
_CEILING_PATTERNS = (
    re.compile(r"up\s+to\s+(\d+)", re.IGNORECASE),
    re.compile(r"ceiling\s+is\s+(\d+)", re.IGNORECASE),
)

_DOC_FILES = (
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "gco_mcp" / "README.md",
    PROJECT_ROOT / "gco_mcp" / "tools" / "README.md",
)

_COUNT_SNIPPET = (
    'import asyncio, sys; sys.path.insert(0, "gco_mcp"); import run_mcp; '
    "print(len(asyncio.run(run_mcp.mcp._list_tools())))"
)


def _count(env_overrides: dict[str, str]) -> int:
    env = dict(os.environ)
    # Clear every per-tool + umbrella flag so the baseline is deterministic,
    # then apply the requested overrides.
    for key in list(env):
        if key.startswith("GCO_ENABLE_"):
            env.pop(key)
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, "-c", _COUNT_SNIPPET],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip().splitlines()[-1])


def _live_counts() -> tuple[int, int]:
    default = _count({})
    ceiling = _count({"GCO_ENABLE_ALL_TOOLS": "true"})
    return default, ceiling


def _scan(path: Path, patterns: tuple[re.Pattern[str], ...]) -> list[int]:
    text = path.read_text(encoding="utf-8")
    found: list[int] = []
    for pat in patterns:
        found.extend(int(m) for m in pat.findall(text))
    return found


def test_docs_tool_counts_match_registry() -> None:
    default, ceiling = _live_counts()
    # Sanity floor: the registry enumeration must actually return a plausible
    # catalog, so a broken subprocess can't make the whole guard vacuous.
    assert default >= 50, f"suspiciously low default tool count: {default}"
    assert ceiling > default, f"ceiling {ceiling} should exceed default {default}"

    default_hits: list[tuple[Path, int]] = []
    ceiling_hits: list[tuple[Path, int]] = []
    for path in _DOC_FILES:
        assert path.exists(), f"expected doc file missing: {path}"
        default_hits.extend((path, n) for n in _scan(path, _DEFAULT_PATTERNS))
        ceiling_hits.extend((path, n) for n in _scan(path, _CEILING_PATTERNS))

    # Floor: if a phrasing change stops the regexes from matching, fail loudly
    # rather than passing on zero comparisons.
    assert len(default_hits) >= 3, (
        f"expected to find the default count quoted in the docs, found {default_hits}"
    )
    assert len(ceiling_hits) >= 3, (
        f"expected to find the ceiling count quoted in the docs, found {ceiling_hits}"
    )

    wrong_default = [(str(p.relative_to(PROJECT_ROOT)), n) for p, n in default_hits if n != default]
    wrong_ceiling = [(str(p.relative_to(PROJECT_ROOT)), n) for p, n in ceiling_hits if n != ceiling]
    assert not wrong_default, (
        f"Docs quote a default MCP tool count that no longer matches the registry "
        f"({default}). Stale references: {wrong_default}"
    )
    assert not wrong_ceiling, (
        f"Docs quote an all-flags MCP tool ceiling that no longer matches the registry "
        f"({ceiling}). Stale references: {wrong_ceiling}"
    )


def test_live_counts_are_current_values() -> None:
    """Pin today's known-good numbers so an accidental registry change is loud.

    Not a substitute for the drift guard above — this simply documents the
    expected 134/182 and turns an unexpected count change into an obvious
    failure with context, alongside the exact-inventory guard in
    test_mcp_server.py.
    """
    default, ceiling = _live_counts()
    assert (default, ceiling) == (134, 182), (
        f"Registered MCP tool counts changed to {default}/{ceiling}. If intentional, "
        "update the docs, test_mcp_server.py, and this expectation together."
    )
