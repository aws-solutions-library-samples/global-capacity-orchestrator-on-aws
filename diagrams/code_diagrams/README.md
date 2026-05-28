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

### `mcp/`

- **`mcp/mission/`**
  - Mission iteration loop (propose -> execute -> observe -> evaluate -> decide) &mdash; `mcp/mission/engine.py::MissionEngine.run_iteration` &mdash; [HTML](./mcp/mission/engine.MissionEngine_run_iteration.html) · [PNG](./mcp/mission/engine.MissionEngine_run_iteration.png)
  - Mission verdict cascade (budget caps, completion, cadence-skip, heuristic) &mdash; `mcp/mission/decide.py::decide_verdict` &mdash; [HTML](./mcp/mission/decide.decide_verdict.html) · [PNG](./mcp/mission/decide.decide_verdict.png)
  - Mission strategy-revision sampling (orchestrator + deterministic fallback) &mdash; `mcp/mission/sampling.py::maybe_sample_strategy_revision` &mdash; [HTML](./mcp/mission/sampling.maybe_sample_strategy_revision.html) · [PNG](./mcp/mission/sampling.maybe_sample_strategy_revision.png)
  - Mission script AST validator (parse-time allowlist enforcement) &mdash; `mcp/mission/sandbox.py::validate_script_ast` &mdash; [HTML](./mcp/mission/sandbox.validate_script_ast.html) · [PNG](./mcp/mission/sandbox.validate_script_ast.png)
