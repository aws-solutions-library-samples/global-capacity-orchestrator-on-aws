# Diagrams

Auto-generated diagrams for the GCO project. Split into two catalogues
so infrastructure views and code control-flow views stay out of each
other's way:

## Table of Contents

- [Catalogues](#catalogues)
- [Quick reference](#quick-reference)
- [Prerequisites](#prerequisites)

## Catalogues

| Catalogue | What it shows | Generator |
|-----------|---------------|-----------|
| [`infra_diagrams/`](infra_diagrams/README.md) | Per-stack and whole-architecture CloudFormation topologies synthesised from the CDK app ([cdk-dia](https://github.com/pistazie/cdk-dia)). PNG outputs for embedding in READMEs. | `python diagrams/infra_diagrams/generate.py` |
| [`code_diagrams/`](code_diagrams/README.md) | Per-function control-flow charts for Lambda handlers, CLI entry points, and CDK stack constructors (pyflowchart + Playwright). Interactive HTML + rasterised PNG. | `python diagrams/code_diagrams/generate.py` |

Both catalogues regenerate deterministically from the source tree —
no drift possible without a code change. Output files are committed
alongside their generators so GitHub's Markdown renderer can embed
the PNGs inline in docs and pull requests (the interactive HTML is
intended for local browsing since GitHub doesn't execute
JavaScript from repo files).

## Quick reference

```bash
# Refresh infrastructure architecture diagrams
python diagrams/infra_diagrams/generate.py

# Refresh code flowcharts (HTML + PNG) and the source-file markers
# that point from each charted function to its diagram
python diagrams/code_diagrams/generate.py

# HTML-only — skip Playwright if you don't have Chromium installed
python diagrams/code_diagrams/generate.py --skip-png

# Wipe every ``# <pyflowchart-code-diagram>`` marker from the source
# tree (useful when tearing the feature down or before a placement
# refactor)
python diagrams/code_diagrams/generate.py --strip-markers
```

## Prerequisites

The two generators have independent dependency chains — only install
what you need.

**Infrastructure diagrams** ([cdk-dia](https://github.com/pistazie/cdk-dia) + Graphviz + Node):

```bash
pip install -e '.[cdk]'    # CDK libs used to synthesize the app in-process
brew install graphviz      # or: apt-get install graphviz  (provides `dot`)
# cdk-dia itself is fetched on demand via npx (Node.js required); no global
# install needed.
```

**Code flowcharts** (`pyflowchart` + `playwright` + Chromium):

```bash
pip install -e '.[diagrams]'
playwright install chromium
```

See each catalogue's own README for the full reference, including
the list of stacks / targets each one chart.
