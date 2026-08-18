# Upload Artifact with Retry Action

This composite action wraps `actions/upload-artifact` with bounded retry
handling for transient artifact-service failures.

## Table of Contents

- [Behavior](#behavior)
- [Inputs](#inputs)
- [Usage](#usage)

## Behavior

The action attempts an upload up to three times. Failed first and second
attempts wait 30 and 60 seconds respectively; the final attempt is allowed to
fail the job. Successful behavior matches the underlying upload action.

## Inputs

| Input | Required | Default |
|---|---:|---|
| `name` | Yes | — |
| `path` | Yes | — |
| `retention-days` | No | `""` |
| `if-no-files-found` | No | `warn` |
| `overwrite` | No | `false` |
| `include-hidden-files` | No | `false` |

## Usage

```yaml
- uses: ./.github/actions/upload-artifact-with-retry
  with:
    name: validation-results
    path: reports/
    retention-days: 7
```

[`action.yml`](action.yml) is the authoritative input and retry contract.
