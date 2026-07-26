# CodeQL Configuration

Configuration for CodeQL Code Scanning. Run via the
`security:codeql:python-code-analysis` job in
[`workflows/security.yml`](../workflows/security.yml), which uses the
`github/codeql-action/init@v3` action's `config-file:` input to load this
config from disk (Advanced Setup).

## Table of Contents

- [Files](#files)
- [What Gets Scanned](#what-gets-scanned)
- [Query Packs](#query-packs)
- [Excluded Rules](#excluded-rules)
- [Modifying the Config](#modifying-the-config)

## Files

| File | Description |
|------|-------------|
| `codeql-config.yml` | Paths to scan, paths to skip, query packs, and rule exclusions |

## What Gets Scanned

Only hand-authored Python runtime code:

- `gco/`, `cli/`, `gco_mcp/`, `lambda/`, `scripts/`

Excluded: `cdk.out/`, `lambda/*-build/` staging dirs, caches, tests, demo
scripts. The top-level `app.py` ([CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) composition entry point) is out of
scope — it has no runtime/security surface and the CodeQL Python
autobuilder raises `NotADirectoryError` on single-file `paths:` entries.

## Query Packs

- `security-and-quality` — includes both security rules and maintainability queries

## Excluded Rules

| Rule | Reason |
|------|--------|
| `py/clear-text-logging-sensitive-data` | False positives on logging registry names, secret ARNs, and one-shot [Cognito](https://docs.aws.amazon.com/cognito/latest/developerguide/what-is-amazon-cognito.html) temp passwords (not secret values) |
| `py/incomplete-url-substring-sanitization` | URL access control is handled by [API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/welcome.html) [IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction.html) and [ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html) allowlists, not substring checks |
| `py/weak-sensitive-data-hashing` | SRP protocol message digest in `cli/analytics_user_mgmt.py::_hash_sha256` — RFC 5054 mandates SHA-256 as the primitive; not a password storage hash (Cognito holds the SRP verifier server-side) |

Each exclusion is documented inline in `codeql-config.yml` with the
specific call sites and rationale.

## Modifying the Config

- To scan additional directories: add them to the `paths:` list (directories only — single files crash the Python autobuilder)
- To exclude a new rule: add an entry to `query-filters:` with `exclude: id:` and document which call sites are covered and why
- To add a query pack: add it to the `queries:` list
- To swap this job for GitHub's Default Setup: comment out `security-codeql-python-code-analysis` in `workflows/security.yml` and re-enable Default Setup in repo Settings → Code security → CodeQL. The config file has no effect under Default Setup.
