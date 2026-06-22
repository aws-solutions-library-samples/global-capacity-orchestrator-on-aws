# GCO Code Flowcharts

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
python diagrams/code_diagrams/generate.py \
    --target lambda/analytics-presigned-url/handler.py:lambda_handler

# HTML only (skip the Playwright PNG step)
python diagrams/code_diagrams/generate.py --skip-png

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

Without Playwright's browser, the generator still writes HTML and skips
the PNG step with a warning.

## Flowchart index

Entries below are grouped by top-level directory and listed in source
order. Each source file may contribute more than one flowchart if it
has multiple charted entry points.

### `gco_mcp/`

- **`gco_mcp/mission/`**
  - Mission engine factory (live vs stub dispatcher, sampling, sandbox wiring) &mdash; `gco_mcp/mission/_engine_factory.py::build_engine_dependencies` &mdash; [HTML](./gco_mcp/mission/_engine_factory.build_engine_dependencies.html) · [PNG](./gco_mcp/mission/_engine_factory.build_engine_dependencies.png)
