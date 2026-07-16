# Build Lambda Package Action

This composite action stages the checked-in Lambda build trees required by CDK
synthesis and CI validation.

## Table of Contents

- [Behavior](#behavior)
- [Prerequisites](#prerequisites)
- [Usage](#usage)
- [Maintenance](#maintenance)

## Behavior

The action recreates `lambda/kubectl-applier-simple-build/`, copies its handler
and manifests, installs its Python dependencies, and recreates
`lambda/helm-installer-build/` from the canonical source directory.

## Prerequisites

The caller must check out the repository and configure Python before invoking
the action. Package installation requires network access.

## Usage

```yaml
- uses: actions/checkout@v6
- uses: actions/setup-python@v6
  with:
    python-version: "3.14"
- uses: ./.github/actions/build-lambda-package
```

## Maintenance

[`action.yml`](action.yml) is authoritative. Keep its copied paths and installed
packages aligned with `cli/stacks.py` and the Lambda source requirements.
