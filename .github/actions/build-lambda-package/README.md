# Build Lambda Package Action

This composite action stages the checked-in [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) build trees required by [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html)
synthesis and CI validation.

## Table of Contents

- [Behavior](#behavior)
- [Prerequisites](#prerequisites)
- [Usage](#usage)
- [Maintenance](#maintenance)

## Behavior

After installing the Lambda manifest's exact npm release, the action invokes
the build-only `cli.stacks.prepare_cdk_assets()` entry point. Raw/in-process CDK
and CLI synthesis callers instead use `cli.stacks.cdk_asset_consumer()` so a
shared lock keeps each published tree immutable through app construction and
synthesis. Each generated asset is built under a per-asset interprocess lock in
a unique sibling staging directory. A completion manifest records both the
canonical source digest and the full installed build-tree digest; only a
verified complete staging tree is renamed into place, with the prior final tree
kept as rollback until publication succeeds.

This prepares `lambda/kubectl-applier-simple-build/` with exact Python
requirements, `lambda/inference-streaming-proxy-build/` with production AWS SDK
clients from the committed npm lockfile and disabled lifecycle scripts, and
`lambda/helm-installer-build/` from its complete canonical Docker context.

## Prerequisites

The caller must check out the repository, configure Python 3.14 plus Node.js
from `.nvmrc`, and install the project's Python package before invoking the
action. The shared entry point imports `cli.stacks`, and package installation
also requires network access. The action reads the Lambda manifest's exact
`packageManager` pin, installs that npm release when needed, verifies it, and
then performs locked production installs.

## Usage

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
- uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020  # v7.0.0
  with:
    node-version-file: ".nvmrc"
- uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97  # v7.0.0
  with:
    python-version: "3.14"
- run: pip install -e ".[cdk]"
- uses: ./.github/actions/build-lambda-package
```

## Maintenance

[`action.yml`](action.yml) is authoritative. Keep it as a thin wrapper around
`prepare_cdk_assets()`; packaging inputs, locks, completion manifests, and
atomic publication belong in `cli/stacks.py`, not duplicated shell logic.
