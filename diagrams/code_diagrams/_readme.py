"""Hierarchical ``code_diagrams/README.md`` renderer.

The README is regenerated on every run so the index never drifts
from what's actually on disk. It groups flowcharts by top-level
source directory (``lambda/``, ``cli/``, ``gco/``, ...) and then by
parent directory within that, mirroring the project layout.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from diagrams.code_diagrams._renderer import RenderedTarget

_HEADER = """\
# GCO Code Flowcharts

<!-- Generated at (UTC): __GENERATED_AT__ -->
<!-- Generated from Git commit: __SOURCE_COMMIT__ -->
*Generated at (UTC): `__GENERATED_AT__`.*
*Generated from Git commit: `__SOURCE_COMMIT__`.*

This directory holds auto-generated control-flow diagrams for the
Python source files listed below. Each target produces an interactive
[flowchart.js](https://github.com/adrai/flowchart.js) HTML page and (if
Playwright is available) a rendered PNG.

> Interactive HTML is the primary artifact — open it in any browser to
> pan, zoom, and export SVG/PNG directly. The PNGs are included for
> embedding in READMEs and pull requests where JS can't run.

## Table of Contents

- [Regeneration](#regeneration)
- [Prerequisites](#prerequisites)
- [Flowchart index](#flowchart-index)

## Regeneration

Use the aggregate driver for canonical committed output:

```bash
# Full code + infrastructure catalogues at the reviewed timestamp
SOURCE_DATE_EPOCH=1788091200 \
GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> \
python diagrams/generate.py

# Read-only artifact/index/marker/PNG contract
python diagrams/generate.py --check

# One catalogue only
SOURCE_DATE_EPOCH=1788091200 \
GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> \
python diagrams/generate.py --code-only
python diagrams/generate.py --infra-only

# A single target for local diagnosis
GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> \\
python diagrams/code_diagrams/generate.py \\
    --target lambda/analytics-presigned-url/handler.py:lambda_handler

# HTML only (skip Playwright and remove older PNGs for selected targets)
GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> \\
python diagrams/code_diagrams/generate.py --skip-png

# Don't insert/refresh the ``# Flowchart:`` markers in source files
GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> \\
python diagrams/code_diagrams/generate.py --skip-marker

# Remove every existing marker from the source tree and exit
# (useful when tearing the feature down or before a big refactor
# of placement rules)
python diagrams/code_diagrams/generate.py --strip-markers
```

See the [Prerequisites](#prerequisites) section below for one-time
browser install steps.

## Prerequisites

Install the project's ``diagrams`` extra, which pins ``pyflowchart`` and
``playwright`` to known-good versions:

```bash
pip install -e '.[diagrams]'
playwright install chromium
```

Without Playwright's browser, direct code-generator runs still write HTML and
remove any older PNG for the selected targets so mixed generation times are
impossible. Canonical aggregate generation requires ``SOURCE_DATE_EPOCH`` and
``GCO_DIAGRAM_SOURCE_COMMIT``. Commit substantive source changes first, then
supply that clean source commit while generating and commit the derived
artifacts separately; this avoids an impossible self-referential commit SHA.
The generator verifies every marker-stripped charted source against the supplied
commit. It records one UTC timestamp and source commit in HTML, PNG pixels, the
catalogue, and source markers. Each HTML/PNG pair also displays a deterministic
digest of the pre-annotation flow HTML, so source-flow changes remain visible
even when flowchart.js collapses them into the same SVG shape. Fixing the
timestamp prevents metadata-only churn; none of these mechanisms promises
byte-identical Chromium or Graphviz rasterization across toolchain versions or
platforms.
``python diagrams/generate.py --check`` enforces structural
contracts; ``tests/test_diagram_artifact_contract.py`` also verifies every PNG
with Pillow.

## Flowchart index

Entries below are grouped by top-level directory and listed in source
order. Each source file may contribute more than one flowchart if it
has multiple charted entry points.
"""


def render_readme(
    results: list[RenderedTarget],
    *,
    output_dir: Path,
) -> str:
    """Render the full README markdown body as a string."""
    sections = _group_by_toplevel(results)
    # Provenance is per source (see ``provenance.json``), so the catalogue is
    # legitimately a mix of vintages: incremental regeneration restamps only
    # the sources that changed. The index header therefore reports the most
    # recent generation; each source's own stamp lives in its marker block and
    # in the manifest.
    newest = max(results, key=lambda result: result.generated_at, default=None)
    generated_at = newest.generated_at if newest is not None else "unknown"
    source_commit = newest.source_commit if newest is not None else "unknown"
    header = _HEADER.replace("__GENERATED_AT__", generated_at).replace(
        "__SOURCE_COMMIT__", source_commit
    )
    lines = [header.rstrip()]
    for top, dir_groups in sections.items():
        lines.append("")
        lines.append(f"### `{top}/`")
        for dir_path, entries in dir_groups.items():
            lines.append("")
            display_dir = dir_path if dir_path else top
            lines.append(f"- **`{display_dir}/`**")
            for entry in entries:
                lines.append(_format_entry(entry, output_dir=output_dir))
    lines.append("")
    return "\n".join(lines)


def _group_by_toplevel(
    results: list[RenderedTarget],
) -> dict[str, dict[str, list[RenderedTarget]]]:
    """Group results into ``{top_level: {parent_dir: [results]}}``.

    ``dict`` preservation of insertion order keeps the README stable:
    sort top-level groups alphabetically, then sort inner directory
    groups alphabetically, then leave each directory's target list in
    its original :data:`TARGETS` order.
    """
    grouped: dict[str, dict[str, list[RenderedTarget]]] = defaultdict(
        lambda: defaultdict(list),
    )
    for result in results:
        src = Path(result.target.source)
        top = src.parts[0]
        parent = str(src.parent)
        grouped[top][parent].append(result)

    ordered: dict[str, dict[str, list[RenderedTarget]]] = {}
    for top in sorted(grouped):
        ordered[top] = {k: grouped[top][k] for k in sorted(grouped[top])}
    return ordered


def _format_entry(
    entry: RenderedTarget,
    *,
    output_dir: Path,
) -> str:
    """Render a single bullet for one flowchart target.

    Uses a 2-space indent on the nested bullet level so the output
    passes markdownlint's MD007/ul-indent rule (default expected
    indent = 2 spaces).
    """
    html_rel = entry.html_path.relative_to(output_dir)
    src = entry.target.source
    func = entry.target.function
    title = entry.target.title or f"`{func}`"
    line = f"  - {title} &mdash; `{src}::{func}` &mdash; [HTML](./{html_rel.as_posix()})"
    if entry.png_path is not None:
        png_rel = entry.png_path.relative_to(output_dir)
        line += f" · [PNG](./{png_rel.as_posix()})"
    return line
