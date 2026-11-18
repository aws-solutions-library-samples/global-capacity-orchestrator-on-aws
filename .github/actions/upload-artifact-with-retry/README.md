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

The second and third attempts pass `overwrite: true` whatever the caller set.
The artifact service can fail the finalize call after it has recorded the
artifact, and `upload-artifact` v4 and later refuse a second upload under an
existing name (`409 Conflict: an artifact with this name already exists`), so
a retry that does not overwrite could never succeed. The only artifact a retry
can replace is the first attempt's, because every caller's artifact name is
unique within its workflow run; `tests/test_supply_chain_integrity.py` keeps
that true.

## Inputs

| Input | Required | Default |
|---|---:|---|
| `name` | Yes | — |
| `path` | Yes | — |
| `retention-days` | No | `""` |
| `if-no-files-found` | No | `warn` |
| `overwrite` | No | `false` (first attempt only) |
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
