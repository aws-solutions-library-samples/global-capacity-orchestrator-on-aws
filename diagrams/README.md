# Diagrams

Auto-generated diagrams for the GCO project. Split into two catalogues
so infrastructure views and code control-flow views stay out of each
other's way:

## Table of Contents

- [Catalogues](#catalogues)
- [Quick reference](#quick-reference)
- [Prerequisites](#prerequisites)

## Catalogues

| Catalogue | What it shows | Canonical generator |
|-----------|---------------|---------------------|
| [`infra_diagrams/`](infra_diagrams/README.md) | Per-stack and whole-architecture [CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) topologies synthesised from the [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) app ([cdk-dia](https://github.com/pistazie/cdk-dia)). PNG outputs for embedding in READMEs. | `python diagrams/generate.py --infra-only` |
| [`code_diagrams/`](code_diagrams/README.md) | Per-function control-flow charts for [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) handlers, CLI entry points, and CDK stack constructors (pyflowchart + Playwright). Interactive HTML + rasterised PNG. | `SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only` |

Use `SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py` to reconcile
both catalogues in one run, or `python diagrams/generate.py --check` for the
read-only artifact, index, marker, timestamp, and source-commit contract.
Canonical code generation requires a fixed integer timestamp and an explicit
clean source commit. Commit substantive source changes first, generate from
that SHA, then commit derived artifacts separately; embedding the SHA of the
same commit that contains an artifact would be self-referential. The generator
compares every marker-stripped charted source with the supplied commit before
rendering. Code artifacts also display a source-flow digest, forcing paired PNG
freshness even when the renderer collapses a changed flow to the same SVG. A
fixed timestamp does not imply byte-identical Chromium or Graphviz
Graphviz rasterization across platforms; the contract is structural,
and `tests/test_diagram_artifact_contract.py` uses Pillow to verify that each
committed PNG is valid with nonzero dimensions. Output files are committed so
GitHub's Markdown renderer can embed them in docs and pull requests. Interactive
HTML is intended for local browsing because GitHub does not execute JavaScript
from repository files.

## Quick reference

```bash
# Canonical regeneration from a clean, committed source revision.
# The code catalogue is INCREMENTAL: only charted sources whose bytes
# changed are re-rendered and restamped, so the diff stays proportional
# to the code you touched. A run with nothing stale is a no-op.
SOURCE_DATE_EPOCH=1788091200 \
GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> \
python diagrams/generate.py

# Read-only committed-tree contract
python diagrams/generate.py --check

# Reconcile just one catalogue
SOURCE_DATE_EPOCH=1788091200 \
GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> \
python diagrams/generate.py --code-only
python diagrams/generate.py --infra-only

# Force a full restamp of every code target (rarely needed — reach for
# this only after changing the generator's own rendering or marker
# format, which invalidates every artifact rather than one source).
SOURCE_DATE_EPOCH=1788091200 \
GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> \
python diagrams/code_diagrams/generate.py --all

# Direct code-generator maintenance operations
GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> \
python diagrams/code_diagrams/generate.py --skip-png
python diagrams/code_diagrams/generate.py --strip-markers
```

Freshness is verified against `code_diagrams/provenance.json`, which records
each charted source's marker-stripped SHA-256 digest alongside the timestamp
and commit that produced its artifacts. Two consequences worth knowing:

- The contract never resolves a recorded commit through Git, so a
  squash-merged (and deleted) branch commit stays a valid provenance label.
- The catalogue is legitimately a mix of vintages. Each source's marker and
  artifacts must match *that source's* recorded stamp; the index header
  reports the most recent generation.

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

The aggregate driver requires one UTC timestamp and one exact source commit for
canonical code outputs and places both values in HTML, PNG pixels, the generated
index, and source markers. Keep the reviewed epoch and source SHA stable when
source has not changed, and always run the read-only contract after generation.

See each catalogue's own README for the full reference, including
the list of stacks / targets each one chart.
