# CodeQL Configuration

Configuration for CodeQL Code Scanning. Loaded by both the
`security:codeql:python-code-analysis` and
`security:codeql:javascript-code-analysis` jobs in
[`workflows/security.yml`](../workflows/security.yml) through the
`github/codeql-action/init` action's `config-file:` input (Advanced Setup),
so the Python and JavaScript scans share one reviewable scope and one set
of query filters.

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

Only hand-authored runtime code — Python for the `gco/`, `cli/`, `gco_mcp/`
and `scripts/` packages, and both Python and JavaScript under `lambda/` (the
Node.js inference streaming proxy lives there):

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

Each exclusion is documented inline in `codeql-config.yml` with the
specific call sites and rationale.

## Modifying the Config

- To scan additional directories: add them to the `paths:` list (directories only — single files crash the Python autobuilder)
- To exclude a new rule: add an entry to `query-filters:` with `exclude: id:` and document which call sites are covered and why
- To add a query pack: add it to the `queries:` list
- To swap these jobs for GitHub's Default Setup: remove the two `security:codeql:*` jobs from `workflows/security.yml` and re-enable Default Setup in repo Settings → Code security → CodeQL. The config file has no effect under Default Setup.
