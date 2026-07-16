# Free Disk Space Action

This composite action reclaims storage on GitHub-hosted Ubuntu runners before
large dependency installs, coverage collection, and CDK synthesis.

## Table of Contents

- [Behavior](#behavior)
- [Safety Boundaries](#safety-boundaries)
- [Usage](#usage)

## Behavior

The action reports root-volume usage, removes preinstalled toolchains unused by
this project, prunes cached Docker images, and reports the resulting free space.
Every cleanup command is best-effort so runner-image changes do not fail CI.

## Safety Boundaries

`/opt/hostedtoolcache` is intentionally preserved because `actions/setup-python`
uses it. Run this action only on disposable GitHub-hosted CI runners, never on a
persistent developer or production host.

## Usage

```yaml
- uses: actions/checkout@v6
- uses: ./.github/actions/free-disk-space
- uses: actions/setup-python@v6
  with:
    python-version: "3.14"
```

See [`action.yml`](action.yml) for the exact cleanup list.
