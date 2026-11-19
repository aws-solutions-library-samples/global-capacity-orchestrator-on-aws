# Shared linter & scanner configuration

Configuration for the linters and security scanners that run in CI
(`.github/workflows/`), in pre-commit (`.pre-commit-config.yaml`), and locally.

These files used to sit at the repository root. They were moved here to keep the
root tree small. The tools no longer discover them automatically at the root, so
every consumer now points at them explicitly — each section below says where.

## Table of Contents

- [`.actionlint.yaml`](#actionlintyaml)
- [`.checkov.yaml`](#checkovyaml)
- [`.gitleaks.toml`](#gitleakstoml)
- [`.kics.yaml`](#kicsyaml)
- [`.markdownlint-cli2.yaml`](#markdownlint-cli2yaml)
- [`.npm-audit-ignore`](#npm-audit-ignore)
- [`.pip-audit-ignore`](#pip-audit-ignore)
- [`.trivyignore`](#trivyignore)
- [`.yamllint.yml`](#yamllintyml)
- [`lambda-python-runtime.txt`](#lambda-python-runtimetxt)
- [`semgrep-excluded-rules.txt`](#semgrep-excluded-rulestxt)
- [Why `.semgrepignore` is not here](#why-semgrepignore-is-not-here)

## `.actionlint.yaml`

actionlint settings. It declares the `ubuntu-26.04` and `ubuntu-26.04-arm`
runner labels, which the pinned actionlint release predates. Remove the entries
once a release knows them.

- **Used by:** `lint:actionlint:workflows` in `.github/workflows/lint.yml`
  via `actionlint -config-file .github/config/.actionlint.yaml`.

## `.checkov.yaml`

Checkov IaC scan settings — frameworks to scan and globally skipped checks.

- **Used by:** `security:checkov:iac` in `.github/workflows/security.yml`
  via `checkov --config-file .github/config/.checkov.yaml`.

## `.gitleaks.toml`

Gitleaks secret-scanning config — allowlists and path exclusions.

- **Used by:** `security:gitleaks:secrets` in `.github/workflows/security.yml`
  via `gitleaks detect --config .github/config/.gitleaks.toml`.

## `.kics.yaml`

KICS IaC scan settings — severity threshold, excluded query IDs, and output.

- **Used by:** `security:kics:iac` in `.github/workflows/security.yml`
  via `kics scan --config .github/config/.kics.yaml`.

## `.markdownlint-cli2.yaml`

markdownlint-cli2 rule set, globs, and ignores for Markdown linting.

- **Used by:** `lint:markdownlint:md` in `.github/workflows/lint.yml`
  (the action's `config:` input) and the `markdownlint-cli2` pre-commit hook
  (`--config`). The VS Code markdownlint extension can be pointed at this path
  too.

## `.npm-audit-ignore`

Exact, dated npm advisory suppressions scoped to one package graph, package,
advisory ID, and installed node path. The file documents each risk decision and
its upstream remediation tracker; entries expire inclusively and cannot hide
additional findings.

- **Used by:** `security:npm-audit:all-packages` in
  `.github/workflows/security.yml`. `.github/scripts/check_npm_audit.py`
  validates npm's JSON report, rejects expired or stale entries, and fails on
  every unmatched vulnerability of moderate severity or above. The
  "depends on vulnerable versions of X" records npm derives when X has no
  fixed version in range take no entry of their own: they clear once X is
  suppressed, and are named in the job log when they do.

## `.pip-audit-ignore`

Dated, justified pip-audit CVE suppressions. pip-audit has no native ignore
file, so this is a project convention expanded into `--ignore-vuln` flags.

- **Used by:** `security:pip-audit:deps` in `.github/workflows/security.yml`.
  `.github/scripts/check_pip_audit_ignore.py` validates that every entry carries
  an unexpired `exp:YYYY-MM-DD` marker.

## `.trivyignore`

Dated, justified Trivy CVE suppressions (one ID per line).

- **Used by:** the `security:trivy:*` jobs in `.github/workflows/security.yml`
  and the weekly `.github/workflows/cve-scan.yml`, via
  `trivy ... --ignorefile .github/config/.trivyignore`.

## `.yamllint.yml`

yamllint rule overrides — line length, indentation, and ignored paths.

- **Used by:** `lint:yamllint:yaml` in `.github/workflows/lint.yml` via
  `yamllint -c .github/config/.yamllint.yml`, and the `yamllint` pre-commit hook.

## `lambda-python-runtime.txt`

The packages the AWS Lambda Python runtime gives handler code (boto3, botocore
and the packages they depend on), pinned at the versions the runtime ships.
The header records where they were read (the x86_64 Lambda base image, as AWS
publishes it to `aws/aws-lambda-base-images`), when, and how to refresh them;
its `# lambda-runtime:` line names the runtime.

- **Used by:** `integration:lambda:imports` in
  `.github/workflows/integration-tests.yml`, through
  `.github/scripts/verify_lambda_imports.py`, which installs exactly this set as
  the base of every handler's import environment. A package the runtime does not
  ship belongs in the Lambda's own deployment, not here:
  `tests/test_ci_runtime_verifiers.py` holds the package names to the runtime's
  set and the runtime line to `LAMBDA_PYTHON_RUNTIME`.

## `semgrep-excluded-rules.txt`

Repo-wide semgrep rule suppressions — one rule ID per line, with rationale.
**Currently empty:** the only entry was `github-actions-mutable-action-tag`,
retired once every third-party action was pinned to a commit SHA (see
[Action pinning](../CI.md#action-pinning)). The file stays in place because
`run-semgrep.sh` degrades to "no excludes" when it is absent, and an
empty-but-present list is the reviewable home for a future suppression.

- **Used by:** `security:semgrep:sast` in `.github/workflows/security.yml`
  through `.github/scripts/run-semgrep.sh`, which expands each line into a
  `--exclude-rule` flag. Keeping the IDs in a data file (like `.trivyignore`)
  keeps suppressions reviewable instead of buried in pipeline YAML.

## Why `.semgrepignore` is not here

`.semgrepignore` stays at the repository root on purpose. Semgrep discovers it
only from the scan root and dropped the override that used to point it elsewhere
(`SEMGREP_R2C_INTERNAL_EXPLICIT_SEMGREPIGNORE`, removed in v1.111.0). Moving it
would silently disable its path exclusions, so it stays at the root next to
`.gitignore`.
