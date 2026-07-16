# Install Trivy Action

This composite action installs and verifies a caller-selected Trivy release
through the pinned `aquasecurity/setup-trivy` installer.

## Table of Contents

- [Inputs](#inputs)
- [Behavior](#behavior)
- [Usage](#usage)
- [Version Updates](#version-updates)

## Inputs

| Input | Required | Default | Purpose |
|---|---:|---|---|
| `version` | Yes | — | Trivy release tag to install |
| `github-token` | No | `""` | Token used by the upstream installer |

## Behavior

The upstream installer restores or downloads the pinned binary, adds it to
`PATH`, and this action runs `trivy --version` as a fail-fast verification.
Scanning remains in the calling workflow so filesystem and image scans can use
their distinct policies.

## Usage

```yaml
- uses: ./.github/actions/install-trivy
  with:
    version: "${{ env.TRIVY_VERSION }}"
    github-token: "${{ github.token }}"
```

## Version Updates

Keep the Trivy release synchronized across all workflow callers. Review and pin
any update to `aquasecurity/setup-trivy` in [`action.yml`](action.yml).
