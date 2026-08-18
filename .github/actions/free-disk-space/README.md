# Free Disk Space Action

This composite action reclaims storage on GitHub-hosted Ubuntu runners before
large dependency installs, coverage collection, and [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) synthesis.

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
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
- uses: ./.github/actions/free-disk-space
- uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97  # v7.0.0
  with:
    python-version: "3.14"
```

See [`action.yml`](action.yml) for the exact cleanup list.
