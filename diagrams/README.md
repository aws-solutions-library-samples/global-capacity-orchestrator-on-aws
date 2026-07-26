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
| [`infra_diagrams/`](infra_diagrams/README.md) | Per-stack and whole-architecture [CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) topologies synthesised from the [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) app ([cdk-dia](https://github.com/pistazie/cdk-dia)). PNG outputs for embedding in READMEs. | `python diagrams/infra_diagrams/generate.py` |
| [`code_diagrams/`](code_diagrams/README.md) | Per-function control-flow charts for [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) handlers, CLI entry points, and CDK stack constructors (pyflowchart + Playwright). Interactive HTML + rasterised PNG. | `python diagrams/code_diagrams/generate.py` |

Infrastructure diagrams derive from the synthesized source tree. Code diagrams also
derive from source, but a normal run intentionally refreshes their wall-clock
``Generated at`` metadata even when no code changed. Set ``SOURCE_DATE_EPOCH``
to a fixed integer Unix timestamp when byte-reproducible code-diagram output is
required. Output files are committed alongside their generators so GitHub's
Markdown renderer can embed the PNGs inline in docs and pull requests (the
interactive HTML is intended for local browsing since GitHub doesn't execute
JavaScript from repo files).

## Quick reference

```bash
# Refresh infrastructure architecture diagrams
python diagrams/infra_diagrams/generate.py

# Refresh code flowcharts (HTML + PNG), their UTC generation metadata,
# and the source-file marker comments that point to each diagram
python diagrams/code_diagrams/generate.py

# Make code-diagram HTML, PNG, README, and source comments byte-reproducible
# by fixing their generation timestamp
SOURCE_DATE_EPOCH=1784203200 python diagrams/code_diagrams/generate.py

# HTML-only — skip Playwright and remove older PNGs for selected targets
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
bash .github/scripts/use-pinned-npm.sh package.json
npm ci --ignore-scripts --no-audit --no-fund  # locked cdk-dia + CDK CLI
pip install -e '.[cdk]'    # CDK libs used to synthesize the app in-process
brew install graphviz      # or: apt-get install graphviz  (provides `dot`)
```

**Code flowcharts** (`pyflowchart` + `playwright` + Chromium):

```bash
pip install -e '.[diagrams]'
playwright install chromium
```

The code-flowchart generator stamps its committed outputs with a UTC generation
time so readers can judge artifact age, and places the same value in each
generated source comment block. Without ``SOURCE_DATE_EPOCH``, every normal run
intentionally records its wall-clock invocation time and can create
metadata-only changes. Set ``SOURCE_DATE_EPOCH`` to fixed integer Unix seconds
when byte-reproducible output is required.

See each catalogue's own README for the full reference, including
the list of stacks / targets each one chart.
