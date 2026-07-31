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
| `unit-tests.yml` | Unit Tests | pytest with coverage (90% enforced floor; ~92% Python target), explicit offline accelerator catalog/NodePool/watch-list validation, BATS shell tests, [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) synth, config matrix, cdk-nag compliance, lockfile freshness, CLI smoke |
| `inference-streaming-proxy.yml` | — | Native Node.js 24 tests for the inference streaming [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) with 93% line/function/branch gates |
| `integration-tests.yml` | Integration Tests | Dockerfile builds, kind cluster E2E with Calico and Metrics Server (4 service deployments, inference-proxy HPA `ScalingActive`, RBAC enforcement, NetworkPolicy blocking, ResourceQuota, PDB validation), K8s manifest validation, Lambda import checks, MCP server tests |
| `security.yml` | Security | bandit, pip-audit, npm audit across every owned graph, trivy (filesystem + container), trufflehog, gitleaks, semgrep, checkov, KICS, CodeQL (Python + JavaScript) |
| `lint.yml` | Linting | actionlint, hadolint, markdownlint, mypy (strict + stacks + lambda), ruff (format + check, imports included), shellcheck, yamllint |

## Satellite Workflows

Workflows outside the four badged gates above. Most are schedule- or dispatch-driven; `mooncake-image.yml` also runs on push and PR (path-filtered) but is a narrow, feature-specific contract test rather than a headline gate.

| File | Trigger | Description |
|------|---------|-------------|
| `release.yml` | `workflow_dispatch` | Bump version, tag, create GitHub Release with auto-generated notes |
| `deps-scan.yml` | Monthly cron + manual | Check pinned dependencies, offline accelerator/NodePool policy, and online [EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/concepts.html) accelerator-catalog drift; update one rolling issue when findings exist |
| `cve-scan.yml` | Weekly cron + manual | Re-run trivy against current CVE databases |
| `pages.yml` | `workflow_run` after Unit Tests on `main` | Publish the HTML coverage report + shields.io badge JSON to GitHub Pages. Split out of Unit Tests so a Pages outage can't fail the test gate |
| `mooncake-image.yml` | `push`: `main` + PR (path-filtered to the contract's inputs), weekly cron, manual | Contract-test the real upstream Mooncake vLLM image GCO defaults to — proxy `/healthz`, store-config loader, KV-connector names. Path-filtered because every run pulls the ~9GB image; the weekly run catches upstream tag re-pushes. Not CVE-scanned (upstream image); version drift is caught by `deps-scan` |

The accelerator check deliberately has two tiers: `unit-tests.yml` runs only the
checked-in deterministic validator, while `deps-scan.yml` adds sequential,
paginated EC2 discovery when its read-only OIDC credentials are available.
Routine drift is reported through the rolling dependency issue rather than
failing the scheduled workflow.

## Naming Conventions

- **Display names:** `category:tool:test_name` (e.g. `unit:pytest:core`, `security:trivy:container-scan`)
- **Job IDs:** hyphen-delimited (e.g. `unit-pytest-core`)

## Adding a New Workflow

1. Create a new `.yml` file in this directory
2. Set `permissions:` to the minimum required (default: `contents: read`)
3. Add `concurrency` with `cancel-in-progress: true` for PR workflows
4. Set `timeout-minutes` on every job
5. Document the workflow in `../.github/CI.md`
