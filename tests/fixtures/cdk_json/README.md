# Released cdk.json Files

`gco upgrade` keeps an operator's `cdk.json` byte for byte, so a release must
accept the `cdk.json` that earlier releases shipped. Each file here is the
committed `cdk.json` of one release, used by `tests/test_cdk_json_compatibility.py`
to prove that the current validation still accepts it.

## Table of Contents

- [Files](#files)
- [Adding a Release](#adding-a-release)

## Files

| File | Provenance |
|---|---|
| `v8.23.0.json` | `git show v8.23.0:cdk.json`, unmodified |

## Adding a Release

Copy the release's file without editing it, so the fixture stays what operators
actually have:

```bash
git show v<version>:cdk.json > tests/fixtures/cdk_json/v<version>.json
```

Then add a row above. The compatibility test picks up every `*.json` file in
this directory.
