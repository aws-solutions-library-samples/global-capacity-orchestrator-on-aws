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
*Generated at (UTC): `__GENERATED_AT__`.*

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

```bash
# All targets
python diagrams/code_diagrams/generate.py

# A single target
python diagrams/code_diagrams/generate.py \\
    --target lambda/analytics-presigned-url/handler.py:lambda_handler

# HTML only (skip Playwright and remove older PNGs for selected targets)
python diagrams/code_diagrams/generate.py --skip-png

# Make every HTML + PNG artifact byte-reproducible by fixing its timestamp
SOURCE_DATE_EPOCH=1784203200 python diagrams/code_diagrams/generate.py

# Don't insert/refresh the ``# Flowchart:`` markers in source files
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

Without Playwright's browser, the generator still writes HTML and removes any
older PNG for the selected targets so mixed generation times are impossible.
Every full run records one UTC timestamp in the HTML, PNG, catalogue, and source
marker. Without ``SOURCE_DATE_EPOCH``, a normal run intentionally records its
wall-clock invocation time and can change committed metadata even when source
code is unchanged. Set ``SOURCE_DATE_EPOCH`` to fixed integer Unix seconds when
byte-reproducible output is required.

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
    generated_at_values = {result.generated_at for result in results}
    if len(generated_at_values) > 1:
        raise ValueError("README results must share one generation timestamp")
    generated_at = next(iter(generated_at_values), "unknown")
    lines = [_HEADER.replace("__GENERATED_AT__", generated_at).rstrip()]
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
