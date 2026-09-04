# GitHub Actions Workflows

CI/CD workflow definitions that run on every push, non-draft pull request, or on a schedule.

## Table of Contents

- [Primary Workflows](#primary-workflows)
- [Satellite Workflows](#satellite-workflows)
- [Naming Conventions](#naming-conventions)
- [Adding a New Workflow](#adding-a-new-workflow)

## Primary Workflows

Six primary workflows run on every push to `main` and every pull request that is not a draft. Draft PRs skip every job until they are marked ready for review — see [Draft pull requests](../CI.md#draft-pull-requests).

| File | Badge | Description |
|------|-------|-------------|
| `unit-tests.yml` | Unit Tests | three dynamically balanced pytest shards with one stable combined-coverage gate (exact 100% line + branch floor), explicit offline accelerator catalog/NodePool/watch-list validation, BATS shell tests, [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) synth, config matrix, cdk-nag compliance, locked Linux installs, lockfile freshness, CLI smoke, and autopilot smoke |
| `inference-streaming-proxy.yml` | — | Native Node.js 24 tests for the inference streaming [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) with exact 100% line/function/branch gates |
| `floci-tests.yml` | Floci Tests | Emulated-AWS layer ([Floci](https://github.com/floci-io/floci), digest-pinned service container, zero AWS credentials): production stores/queues/secrets/S3/CloudFormation code issuing real SDK requests against the emulator, plus the live-validation harness E2E through `gco release validate --emulator-endpoint` (verified emulator opt-in, full preflight incl. `cdk list` over the real cloud assembly, baseline capture, negative account-mismatch proof). See `docs/FLOCI_TESTING.md` |
| `integration-tests.yml` | Integration Tests | Dockerfile builds + functional container tests (pod-equivalent boot, HTTP probe/auth contracts, SIGTERM shutdown, moto-SQS queue-processor exit codes), dev-container smoke (pinned toolchain incl. uv/uvx + both engine plans/configs + persisted Codex lazy install), kind cluster E2E with Calico and Metrics Server (5 service deployments, inference-proxy HPA `ScalingActive`, RBAC enforcement, NetworkPolicy blocking, ResourceQuota, PDB validation), K8s manifest validation, Lambda import checks, MCP server tests |
| `security.yml` | Security | bandit, pip-audit, npm audit across every owned graph, trivy (filesystem + container), trufflehog, gitleaks, semgrep, checkov, KICS, CodeQL (Python + JavaScript) |
| `lint.yml` | Linting | actionlint, hadolint, markdownlint, mypy (strict + stacks + lambda), ruff (format + check, imports included), strict ShellCheck at `style` severity over every tracked `*.sh` path, yamllint |

## Satellite Workflows

Eight workflows sit outside the four badged gates above. Most are schedule- or dispatch-driven; `mooncake-image.yml` also runs on push and PR but is a narrow, feature-specific contract test rather than a headline gate.

| File | Trigger | Description |
|------|---------|-------------|
| `release.yml` | `workflow_dispatch` | Stage 1 of the release: bump version files on a `release/vX.Y.Z` branch, open the release PR, and dispatch the PR-gating CI workflows against the branch (pushes/PRs made with `GITHUB_TOKEN` don't trigger `push:`/`pull_request:` runs, so required checks are attached via `workflow_dispatch` — the documented exception). Never writes to `main` |
| `release-publish.yml` | `push`: `main` (`VERSION` only), manual | Stage 2 of the release: on the squash-merged release PR, create the annotated `vX.Y.Z` tag and the GitHub Release with auto-generated notes. Idempotent (safe to re-dispatch after a partial failure), refuses to move an existing tag, and skips VERSION changes whose commit subject isn't `Release vX.Y.Z` |
| `deps-scan.yml` | Monthly cron + manual | Check pinned dependencies (including both Autopilot agent CLI pins and companion MCP server liveness), offline accelerator/NodePool policy, and online [EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/concepts.html) accelerator-catalog drift; update one rolling issue when findings exist |
| `cve-scan.yml` | Weekly cron + manual | Re-run trivy against current CVE databases |
| `pages.yml` | `workflow_run` after Unit Tests on `main` | Publish the project site to GitHub Pages: the MkDocs wiki (`wiki/`) at the root, the HTML coverage report at `/coverage/`, and the shields.io badge JSON at the site root. Split out of Unit Tests so a Pages outage (or wiki build failure) can't fail the test gate; `lint:mkdocs:strict` runs the same build on PRs |
| `mooncake-image.yml` | `push`: `main`, PR, manual | Contract-test the real upstream Mooncake vLLM image GCO defaults to — proxy `/healthz`, store-config loader, KV-connector names. Not CVE-scanned (upstream image); version drift is caught by `deps-scan` |
| `pr-type-label.yml` | `pull_request` (`opened`, `edited`, `reopened`, `ready_for_review`) | Sync a pull request's type label to the "Type of change" box its author ticked, so `.github/release.yml` can group the generated notes without a second step anyone has to remember — v6.5.0 and v6.5.1 both shipped with every entry in "Other changes" for want of a label. Only the nine type labels are touched; ticking nothing is a no-op rather than a strip. Uses `pull_request`, never `pull_request_target`, and the body is fetched by the script through `gh` rather than interpolated into a `run:` script, so fork PRs get a read-only token and the job is skipped for them — fork contributions need the label applied at review time |
| `grafana-dashboards.yml` | `push`: `main`, PR (paths-filtered), manual | Prove the exact Grafana image the pinned kube-prometheus-stack chart ships accepts the curated dashboard ConfigMaps: extract the payloads, resolve the image via `helm template` at the `charts.yaml` pin, boot it with sidecar-shaped file provisioning, and require every uid to answer with `meta.provisioned=true` and an error-free provisioning log. Runs only when the dashboards, the chart pin, or the check itself change |

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
4. For a PR workflow, add `types: [opened, synchronize, reopened, ready_for_review]` and gate **every** job on `github.event_name != 'pull_request' || github.event.pull_request.draft == false`, then add the file to `tests/test_workflow_draft_pr_gating_contract.py`
5. Set `timeout-minutes` on every job
6. Document the workflow in `../CI.md`
