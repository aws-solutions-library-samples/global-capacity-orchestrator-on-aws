# GitHub Actions Workflows

CI/CD workflow definitions that run on every push, pull request, or on a schedule.

## Table of Contents

- [Primary Workflows](#primary-workflows)
- [Satellite Workflows](#satellite-workflows)
- [Naming Conventions](#naming-conventions)
- [Adding a New Workflow](#adding-a-new-workflow)

## Primary Workflows

Run on every push to `main` and every pull request.

| File | Badge | Description |
|------|-------|-------------|
| `unit-tests.yml` | Unit Tests | pytest with coverage (85% gate), BATS shell tests, CDK synth, config matrix, cdk-nag compliance, lockfile freshness, CLI smoke |
| `integration-tests.yml` | Integration Tests | Dockerfile builds, kind cluster E2E with Calico (3 service deployments, RBAC enforcement, NetworkPolicy blocking, ResourceQuota, PDB validation), K8s manifest validation, Lambda import checks, MCP server tests |
| `security.yml` | Security | bandit, pip-audit, trivy (filesystem + container), trufflehog, gitleaks, semgrep, checkov, KICS |
| `lint.yml` | Linting | actionlint, hadolint, markdownlint, mypy (strict + stacks + lambda), ruff (format + check, imports included), shellcheck, yamllint |

## Satellite Workflows

| File | Trigger | Description |
|------|---------|-------------|
| `release.yml` | `workflow_dispatch` | Bump version, tag, create GitHub Release with auto-generated notes |
| `deps-scan.yml` | Monthly cron + manual | Check Python, Docker, Helm, EKS-addon versions; open issue if drift found |
| `cve-scan.yml` | Weekly cron + manual | Re-run trivy against current CVE databases |
| `pages.yml` | `workflow_run` after Unit Tests on `main` | Publish the HTML coverage report + shields.io badge JSON to GitHub Pages. Split out of Unit Tests so a Pages outage can't fail the test gate |

## Naming Conventions

- **Display names:** `category:tool:test_name` (e.g. `unit:pytest:core`, `security:trivy:container-scan`)
- **Job IDs:** hyphen-delimited (e.g. `unit-pytest-core`)

## Adding a New Workflow

1. Create a new `.yml` file in this directory
2. Set `permissions:` to the minimum required (default: `contents: read`)
3. Add `concurrency` with `cancel-in-progress: true` for PR workflows
4. Set `timeout-minutes` on every job
5. Document the workflow in `../.github/CI.md`
