# Contributing to GCO (Global Capacity Orchestrator on AWS)

Thank you for contributing to GCO (Global Capacity Orchestrator on AWS)! This guide will help you get started.

## Table of Contents

- [Development Setup](#development-setup)
  - [Prerequisites](#prerequisites)
  - [Using the Dev Container (Recommended)](#using-the-dev-container-recommended)
  - [Local Development Environment (Advanced)](#local-development-environment-advanced)
- [Development Workflow](#development-workflow)
  - [Dependency Management](#dependency-management)
  - [Type Checking](#type-checking)
  - [Authentication](#authentication)
  - [Tenet-Driven Decisions](#tenet-driven-decisions)
- [Code Organization](#code-organization)
  - [Directory Structure](#directory-structure)
  - [Adding New Features](#adding-new-features)
- [Testing](#testing)
  - [Running Tests Locally](#running-tests-locally)
  - [Pre-Pull-Request Verification](#pre-pull-request-verification)
  - [Live Release Validation Applicability](#live-release-validation-applicability)
  - [CI/CD Pipeline](#cicd-pipeline)
  - [Integration Tests](#integration-tests)
- [Documentation](#documentation)
- [Code Review Guidelines](#code-review-guidelines)
- [Release Process](#release-process)
- [Best Practices](#best-practices)
- [Common Tasks](#common-tasks)
- [Getting Help](#getting-help)
- [Code of Conduct](#code-of-conduct)

## Development Setup

### Prerequisites

**Recommended path — the dev container only needs:**

- AWS account with appropriate permissions
- Docker (or Finch / Colima) and Git

The container itself ships Python 3.14, Node.js 24, CDK, kubectl, AWS CLI, and every Python dependency at the exact versions CI uses, so you don't need any of them on your host.

**Host development path additionally needs:**

- Python 3.14+ (required for the un-parenthesized except-tuple syntax in `gco_mcp/resources/config.py`)
- Node.js 24+ (for CDK)
- kubectl
- A clean virtualenv (or pipx) for the GCO Python deps — see the warning under [Local Development Environment (Advanced)](#local-development-environment-advanced).

> **Strong recommendation:** use the dev container. GCO pins many exact package versions (FastAPI, mypy, Ruff, AWS SDKs, CDK, etc.) so CI is reproducible. Installing those on top of an existing Python environment frequently triggers `ResolutionImpossible` / resolver errors. The dev container sidesteps this entirely and matches CI bit-for-bit.

### Using the Dev Container (Recommended)

The dev container includes all dependencies pre-installed (Python 3.14, Node.js 24, CDK, kubectl, AWS CLI). This avoids "works on my machine" issues and is the supported path for everything from running tests to deploying stacks.

The image is **multi-arch** — Apple Silicon (`linux/arm64`), Intel/x86_64 hosts, and CI all build natively from the same `Dockerfile.dev` because every baked-in binary (kubectl, AWS CLI v2, Docker static client) is selected by `$TARGETARCH`. Native builds on Apple Silicon take ~2 min; emulated cross-builds (e.g. `--platform linux/amd64` on an arm64 host) take ~7-8 min and are only needed when you specifically want to test the amd64 image.

```bash
# Build the container (cached on subsequent runs; ~2 min the first time)
docker build -f Dockerfile.dev -t gco-dev .

# Run an interactive shell
docker run -it --rm \
  -v ~/.aws:/root/.aws:ro \
  -v $(pwd):/workspace \
  -w /workspace \
  gco-dev

# Or run a single command
docker run --rm \
  -v ~/.aws:/root/.aws:ro \
  -v $(pwd):/workspace \
  -w /workspace \
  gco-dev gco stacks list

# Run CDK commands
docker run --rm \
  -v ~/.aws:/root/.aws:ro \
  -v $(pwd):/workspace \
  -w /workspace \
  -e CDK_DOCKER=docker \
  gco-dev cdk synth

# Run tests
docker run --rm \
  -v $(pwd):/workspace \
  -w /workspace \
  gco-dev pytest tests/ -v
```

**Running `gco stacks deploy-all` from the container.** `cdk deploy`
invokes Docker to bundle Lambda assets. The dev container ships only the
Docker CLI (no daemon), so mount the host Docker socket to give it a
transport to your host daemon:

```bash
docker run --rm -it \
  -v ~/.aws:/root/.aws:ro \
  -v $(pwd):/workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -w /workspace \
  gco-dev gco stacks deploy-all -y
```

This pattern works on Linux, Docker Desktop for macOS and Windows, and
Colima for macOS. On Colima the host socket lives at
`~/.colima/default/docker.sock` (older Colima) or `~/.colima/docker.sock`
(newer Colima) — adjust the left side of the `-v` flag accordingly or
symlink the Colima socket to `/var/run/docker.sock`. See
<https://github.com/abiosoft/colima> for the current default. This is
host-socket pass-through, not true Docker-in-Docker — do not add
`--privileged`. The trade-off is that anyone inside the container has
root-equivalent access to the host Docker daemon through the mounted
socket, so only use this on trusted hosts.

**Tip**: Create a shell function for convenience. Using a function (rather than an alias that hardcodes `$(pwd)`) means it auto-resolves your GCO clone via `git rev-parse`, so `gco stacks *` and other source-tree-dependent commands work regardless of which subdirectory you call it from. Set `GCO_HOME` in your shell to use it from anywhere on disk:

```bash
gco-dev() {
    local project_root="${GCO_HOME:-$(git rev-parse --show-toplevel 2>/dev/null)}"
    # Check for both Dockerfile.dev *and* the gco/ namespace package
    # so we don't accidentally bind-mount an unrelated repo that
    # happens to have a Dockerfile.dev at its root.
    if [[ -z "$project_root" \
        || ! -f "$project_root/Dockerfile.dev" \
        || ! -d "$project_root/gco" ]]; then
        echo "gco-dev: not inside the GCO repo. cd into your clone, or set GCO_HOME." >&2
        return 1
    fi
    docker run --rm \
        -v ~/.aws:/root/.aws:ro \
        -v "$project_root:/workspace" \
        -w /workspace \
        gco-dev "$@"
}
# Then use: gco-dev gco stacks list
```

### Local Development Environment (Advanced)

Use this path only if you specifically want to develop on your host (e.g., editor integrations like the Pyright/mypy LSP). It is not the supported path for one-off deploys or running tests — those should go through the dev container above.

```bash
# Clone repository
git clone <repository-url>
cd GCO

# Create a *fresh* virtual environment — do not reuse one that already has
# AWS CDK, FastAPI, mypy, or other commonly-pinned packages installed.
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"
```

If `pip install` fails with `ResolutionImpossible` or "the conflict is caused by..." messages, your venv is not actually clean (or you're on a Python version older than 3.14). Recreate the venv from scratch or switch to the dev container — please don't loosen the pins in `pyproject.toml` or `requirements-lock.txt` to make local install work, since CI will reject the lockfile drift.

## Development Workflow

### Dependency Management

GCO uses exact-pinned Python dependencies in `pyproject.toml` with a committed transitive lock (`requirements-lock.txt`). It also owns two isolated npm graphs, each with exact direct pins, a committed `package-lock.json`, an npm `packageManager` pin, Dependabot coverage, and CI audit enforcement.

#### Dependency Groups

| Group | Consumer / install command | What it includes |
|-------|----------------------------|------------------|
| Core | `pip install -e .` | CLI runtime deps (boto3, click, requests, etc.) |
| CDK | `pip install -e ".[cdk]"` | AWS CDK, cdk-nag, constructs (for stack synthesis) |
| Dev | `pip install -e ".[dev]"` | Everything: CDK + lint + typecheck + test + security |
| MCP | `pip install -e ".[mcp]"` | FastMCP server |
| Image: health monitor | Docker reads `[image-health-monitor]` | Direct runtime roots for `gco.services.health_api` |
| Image: manifest processor | Docker reads `[image-manifest-processor]` | Direct runtime roots for the manifest API and Grafana rotator |
| Image: inference proxy | Docker reads `[image-inference-proxy]` | Direct runtime roots for `gco.services.inference_api` |
| Image: inference monitor | Docker reads `[image-inference-monitor]` | Direct runtime roots for the inference reconciler |
| Image: queue processor | Docker reads `[image-queue-processor]` | Direct runtime roots for the SQS worker |
| Image: cost monitor | Docker reads `[image-cost-monitor]` | Direct runtime roots for the cost API and report pipeline |

CDK dependencies are in a separate `[cdk]` extras group so operators who only use the CLI don't need to install the full CDK toolchain. The six `image-*` groups are build metadata and the single source of direct dependency pins for production service images: each Dockerfile extracts only its own group with `tomllib`, constrains it with `requirements-lock.txt`, and deletes the generated requirements file in the same layer. Do not add per-image requirements files or install `.[image-*]` inside production images, because either approach introduces extra dependencies or another synchronization surface.

#### Node.js Dependency Graphs

| Graph | Purpose | Install command |
|-------|---------|-----------------|
| Repository root | Locked AWS CDK CLI, cdk-dia, and markdownlint tooling | `npm ci --ignore-scripts --no-audit --no-fund` |
| `lambda/inference-streaming-proxy/` | Deployable AWS SDK clients; its package script runs the native tests in `tests/inference-streaming-proxy/` | `npm ci --prefix lambda/inference-streaming-proxy --ignore-scripts --no-audit --no-fund` |

Before installing either graph, select Node from `.nvmrc` and install/verify the
exact npm release declared by `packageManager`:

```bash
bash .github/scripts/use-pinned-npm.sh package.json
```

Keep these graphs separate: root development tools must never enter the deployable Lambda bundle. Direct versions must be exact, lockfiles must be committed, and every new repository-owned `package.json` must add a matching npm entry in `.github/dependabot.yml`. CI's `check_npm_package_management` guard fails on an unlocked, ranged, unpinned, or unmanaged graph. Node 24, npm 12.0.2, and the root CDK CLI pin are also checked against `.nvmrc`, `Dockerfile.dev`, `gco/stacks/constants.py`, and both manifests by the monthly dependency scan.

#### Regenerating the Lockfile

After updating any dependency version in `pyproject.toml`, regenerate the
lockfile using `Dockerfile.dev`. This is the only supported workflow — it
produces a deterministic, Linux-targeted lockfile that matches CI, avoids
host-specific path leakage, and doesn't require `pip-tools` on your machine.

```bash
# Build the dev image once (cached between runs, ~2 minutes the first time)
docker build -f Dockerfile.dev -t gco-dev .

# Regenerate the lockfile and strip the project self-reference
docker run --rm -v "$(pwd):/workspace" -w /workspace gco-dev bash -c '
  pip install --quiet "pip==25.0.1" &&
  pip-compile --no-emit-index-url --strip-extras --all-extras \
    -o requirements-lock.txt pyproject.toml &&
  sed -i "/^gco-cli @ file:/,+1d" requirements-lock.txt
'
```

The `pip install "pip==25.0.1"` step works around `pip-tools==7.6.1` importing
pip internals (`pip._internal.utils.compat.stdlib_pkgs`) that newer pip — as
shipped in the current `python:3.14-slim` base image — has removed. The
downgrade lives only inside the throwaway container; upgrading `pip-tools`
past 7.6 would remove the need for it, but that is a dependency change made on
its own PR, not silently alongside a lockfile regeneration.

The `sed` step removes the `gco-cli @ file:///workspace` self-reference that
`pip-compile` always emits (two lines — the `file://` URI and its `# via`
continuation). CI installs the project separately with `pip install --no-deps`,
and the staleness check strips `^gco-cli @ file` anyway, but we keep it out of
the committed file for readability.

Running on Linux directly (native or WSL) matches the container's environment
— macOS-only resolutions will produce a different lockfile that CI rejects,
which is why the Docker path is the only supported workflow.

Commit the updated `requirements-lock.txt` alongside your `pyproject.toml`
changes. The lockfile pins all transitive dependencies to ensure reproducible
builds across environments.

#### Installing from the Lockfile

For reproducible installs (CI, production containers):

```bash
pip install -r requirements-lock.txt
pip install -e . --no-deps
```

### Type Checking

mypy runs across the entire codebase with `--check-untyped-defs` enabled. The CI pipeline has two type-checking jobs:

1. **`lint:typecheck`** — Checks `gco/config/`, `gco/models/`, `gco/services/`, and `cli/`. Installs only mypy + type stubs (fast, no CDK needed).
2. **`lint:typecheck-stacks`** — Checks `gco/stacks/`. Installs CDK dependencies since stack code uses CDK types.

To run locally:

```bash
# Check everything except stacks (fast, no CDK needed)
mypy gco/config/ gco/models/ gco/services/ cli/ --ignore-missing-imports --check-untyped-defs

# Check stacks (requires CDK: pip install -e ".[cdk,typecheck]")
mypy gco/stacks/ --ignore-missing-imports --check-untyped-defs

# Check everything at once (requires CDK installed)
mypy gco/ cli/ --ignore-missing-imports --check-untyped-defs
```

### Authentication

The in-cluster services validate short-lived HMAC request envelopes produced by
the IAM-protected API Gateway proxies. The middleware
(`gco/services/auth_middleware.py`) binds each signature to its timestamp, nonce,
HTTP method, exact path/query, and body digest, accepts current and pending
Secrets Manager keys during rotation, and rejects stale or replayed envelopes.
The reusable signing key is never sent to the cluster as a request header.

**Important:** The middleware is fail-closed by default. If `AUTH_SECRET_ARN` is not set and `GCO_DEV_MODE` is not enabled, all authenticated requests return 503. To run services locally without Secrets Manager:

```bash
export GCO_DEV_MODE=true
```

This is intentional — a missing `AUTH_SECRET_ARN` in production should fail loudly rather than silently allowing unauthenticated access.

### Tenet-Driven Decisions

Read [TENETS.md](TENETS.md) before making a change that affects architecture,
security, accelerator support, destructive operations, regional behavior,
recovery, or long-term maintenance. The tenets are prioritized: when two
principles genuinely conflict, the earlier one wins. Reviews should name the
relevant tenet and the evidence supporting the choice rather than treating the
document as a generic checklist.

Record an [Architecture Decision Record](docs/adr/README.md) when a decision is
expensive to reverse, changes a trust boundary, resolves a real tenet conflict,
or creates a durable exception. A bounded exception should identify its owner,
scope, compensating controls, and revisit trigger.

### 1. Create a Feature Branch

```bash
git checkout -b feature/your-feature-name
```

### 2. Make Changes

Follow these guidelines:

- **Code Style**: Follow PEP 8 for Python, use type hints
- **Documentation**: Update docs for any user-facing changes
- **Tests**: Add tests for new functionality
- **Commits**: Use clear, descriptive commit messages

### 3. Test Locally

```bash
# Synthesize CDK
cdk synth

# Deploy to dev account
export AWS_PROFILE=dev
gco stacks deploy-all -y

# Run tests
pytest tests/

# Verify deployment
kubectl get pods -n gco-system
gco jobs list -r us-east-1
```

### 4. Submit Changes

```bash
# Commit changes
git add .
git commit -m "feat: add new feature"

# Push to remote
git push origin feature/your-feature-name

# Create pull request
# Follow your organization's PR process
```

## Code Organization

### Directory Structure

```text
gco/
├── stacks/                  # CDK stack definitions
├── services/                # Kubernetes services (Python/FastAPI)
├── models/                  # Data models
└── config/                  # Configuration management

cli/                         # CLI commands and utilities
  ├── commands/              # Per-group command modules (jobs, capacity, stacks, …)
  ├── main.py                # Root CLI group and entry point
  ├── kubectl_helpers.py     # Shared kubeconfig utilities

lambda/
├── kubectl-applier-simple/  # Lambda for kubectl operations
├── helm-installer/          # Lambda for Helm chart installation
├── api-gateway-proxy/       # Global API Gateway proxy and HMAC signer
├── regional-api-proxy/      # Region-pinned API Gateway proxy and HMAC signer
├── ga-registration/         # Global Accelerator registration
└── secret-rotation/         # Backend HMAC signing-key rotation Lambda

dockerfiles/         # Dockerfiles for K8s services
docs/                # Documentation
examples/            # Example job manifests
tests/               # Test suites
scripts/             # Utility scripts
```

### Adding New Features

#### New CDK Stack

1. Create file in `gco/stacks/`
2. Import in `app.py`
3. Add to deployment workflow
4. Document in `docs/ARCHITECTURE.md`

#### New Kubernetes Service

1. Create service code in `gco/services/`
2. Create Dockerfile in `dockerfiles/`
3. Add manifest to `lambda/kubectl-applier-simple/manifests/`
4. Update `regional_stack.py` to build image
5. Document in README

#### New Region Support

1. Update `cdk.json` context
2. Test deployment
3. Update documentation
4. Verify Global Accelerator integration

## Testing

### Running Tests Locally

```bash
# Run all tests
pytest tests/

# Run specific test file
pytest tests/test_integration.py

# Run with coverage
pytest --cov=gco --cov=cli --cov=gco_mcp tests/

# Run with verbose output
pytest tests/ -v

# Run only unit tests
pytest tests/ -m unit

# Run only integration tests
pytest tests/ -m integration
```

### Pre-Pull-Request Verification

Before you open a pull request, run the core test, lint/format, and type-check
commands plus the deterministic accelerator maintenance guard. These are the
same surfaces invoked by `unit:pytest:core`, `lint:ruff:python`,
`lint:mypy:strict`, and the explicit accelerator validation steps described
under [CI/CD Pipeline](#cicd-pipeline).

Run them from your dev-container shell (the recommended path — see
[Using the Dev Container (Recommended)](#using-the-dev-container-recommended)):

```bash
ruff format --check gco/ cli/ gco_mcp/ tests/ lambda/ scripts/ diagrams/
ruff check gco/ cli/ gco_mcp/ tests/ lambda/ scripts/ diagrams/
mypy gco/ cli/ gco_mcp/ scripts/ --exclude 'gco/stacks/'
python scripts/accelerator_catalog.py validate
pytest tests/test_accelerator_catalog.py -q
pytest tests/ --cov=gco --cov=cli --cov=gco_mcp --cov-fail-under=90
```

**Success indicator:** all six commands complete with no reported failures —
Ruff reports no formatting diffs or lint findings, mypy reports no type errors,
the accelerator validator and focused tests report a current internally
consistent catalog, and the aggregate pytest run passes at or above the coverage
threshold. Fix any failure and re-run the full sequence before submitting.

For what to update alongside code changes, see the [Testing](#testing) and
[Documentation](#documentation) guidance below and the
[Contributing section of the README](README.md#contributing).

### Live Release Validation Applicability

Live release validation is a separate, explicitly authorized local operator process for behavior that mocked/offline CI cannot prove. Use the highest-risk row that applies to the pull request.

| Decision | Typical changes |
|---|---|
| **Required** | CDK or CloudFormation topology and lifecycle changes; deploy/destroy or retained-resource cleanup changes; IAM, networking, regional routing, EKS/Kubernetes runtime wiring; and deployed service or Lambda behavior that depends on real AWS integration |
| **Usually not required** | Isolated CLI changes that fast mocked/offline tests completely validate; CI/workflow/test-tooling-only changes; routine dependency bumps with no deployed runtime or infrastructure effect; docs-only or test-only changes; and behavior-preserving refactors |

These are risk categories, not blanket path exemptions. A CLI command that mutates live AWS resources still requires validation, while a dependency bump may require it if it changes a deployed image, AWS SDK behavior, CDK output, or runtime integration. Explain the decision in the pull request; maintainers may require validation when impact is uncertain.

When required, obtain explicit account and KMS-deletion authorization, run `python -m scripts.live_release_validation --actions all` only on a developer's local machine, and post a sanitized summary comment (run ID, exact SHA, overall status, per-action statuses) on the pull request. The full reports enumerate the validation account's ID, ARNs, and endpoint URLs: keep them local alongside `checkpoint.json`, and share a full report only through a private maintainer channel. Never invoke the harness from GitHub Actions. See the [Live Release Validation runbook](docs/LIVE_RELEASE_VALIDATION.md) for the safety gates and complete command.

### Example Manifest Validation

Changes under `examples/` carry their own validation bar, enforced by the same risk framing:

- **Any change** (including comment/doc edits): `gco examples validate --static-only` must pass. CI runs the identical checks (`tests/test_example_job_validation.py`), covering YAML validity, transport-gate acceptance for the documented submission path, workload namespaces, and three-way symmetry between `examples/`, the spec registry (`scripts/example_job_validation/specs.py`), and the `gco_mcp` example catalog.
- **Behavior changes** (image, command, resources, scheduler, target namespace, new or removed example): additionally run a live validation scoped to the affected examples — `gco examples validate --examples <name>` with the same account/consent/KMS flags as live release validation — and post a sanitized per-example summary on the pull request. The same report-privacy rules apply.

See the [Example Job Validation guide](docs/EXAMPLE_VALIDATION.md) for the full pipeline, per-example criteria, and scoping flags.

### CI/CD Pipeline

The project uses GitHub Actions for automated testing. Every push and every non-draft pull request runs six primary workflows in parallel. Eight satellite workflows cover release publication, scheduled scans, Pages, and feature-specific contracts; some satellites also run on push or pull request.

Open a PR as a draft while you iterate and CI stays idle: every job in a PR-triggered workflow skips on drafts, and the full suite fires when you mark the PR ready for review. See [Draft pull requests](.github/CI.md#draft-pull-requests) for why the gate is per job and what `ready_for_review` guarantees.

#### Primary workflows (run on every push + PR)

| Workflow file | README row | Purpose |
|---------------|------------|---------|
| `.github/workflows/unit-tests.yml` | Unit Tests | Three dynamically balanced pytest shards with combined coverage, accelerator policy, BATS, CLI/autopilot smoke, CDK synth/config/nag, lockfile freshness, and workload imports |
| `.github/workflows/floci-tests.yml` | Floci Tests | Credential-free wire-level and live-validation harness contracts against the pinned Floci emulator |
| `.github/workflows/integration-tests.yml` | Integration Tests | Container, dev-image, kind, manifest, Lambda, and MCP integration contracts |
| `.github/workflows/security.yml` | Security | Python/npm/container/IaC/secret scans and Python+JavaScript CodeQL |
| `.github/workflows/inference-streaming-proxy.yml` | — (no badge) | Native Node.js 24 streaming-Lambda tests with 93% line/function/branch gates |
| `.github/workflows/lint.yml` | Linting | actionlint, hadolint, markdownlint, strict MkDocs, mypy, Ruff, ShellCheck, and yamllint |

Each workflow file has a comment header documenting triggers and per-job purpose. Every job uses `category:tool:test_name` display names (for example, `unit:pytest:core`) and `category-tool-test_name` job IDs.

#### Satellite workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `.github/workflows/release.yml` | Manual | Stage 1: open the version-bump PR and dispatch its gating workflows |
| `.github/workflows/release-publish.yml` | `main` VERSION push + manual | Stage 2: verify the merged release commit, then create the immutable tag and GitHub Release |
| `.github/workflows/deps-scan.yml` | Monthly + manual | Check pinned versions and live accelerator-catalog drift; maintain one rolling issue |
| `.github/workflows/cve-scan.yml` | Weekly + manual | Re-run Trivy against current CVE databases |
| `.github/workflows/pages.yml` | Successful Unit Tests run on `main` | Publish the strict MkDocs site, coverage report, and badge JSON |
| `.github/workflows/mooncake-image.yml` | `main`, PR, manual | Validate the pinned upstream Mooncake image contract |
| `.github/workflows/pr-type-label.yml` | PR opened/edited/reopened/ready | Sync the declared type-of-change checkbox to its release-note label |
| `.github/workflows/grafana-dashboards.yml` | Paths-filtered `main`/PR + manual | Provision curated dashboards into the real Grafana image resolved from the pinned chart |

#### Published coverage report and badge

After a successful `Unit Tests` push run on the repository's default branch, `pages.yml` downloads that exact run's `pytest-coverage` artifact. It publishes `htmlcov/` and generates `coverage-badge.json` in the same GitHub Pages site. The README's custom shields.io endpoint reads that Pages JSON; test-count and BATS-count endpoint badges are not generated. Pull requests cannot deploy Pages because the workflow requires a successful same-repository default-branch push.

#### Running the pipeline locally

You can simulate the CI pipeline locally:

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run linters (matches lint.yml jobs)
ruff format --check gco/ cli/ gco_mcp/ tests/ lambda/ scripts/ diagrams/
ruff check gco/ cli/ gco_mcp/ tests/ lambda/ scripts/ diagrams/
yamllint -c .github/config/.yamllint.yml --strict .

# Install locked tooling and the isolated production-Lambda graph.
bash .github/scripts/use-pinned-npm.sh package.json
npm ci --ignore-scripts --no-audit --no-fund
npm run lint:markdown
npm ci --prefix lambda/inference-streaming-proxy --ignore-scripts --no-audit --no-fund
npm --prefix lambda/inference-streaming-proxy test

# Run type checks (everything except stacks — fast, no CDK needed)
mypy gco/ cli/ gco_mcp/ scripts/ --exclude 'gco/stacks/'

# Run type checks on stacks (requires CDK)
pip install -e ".[cdk,typecheck]"
mypy gco/stacks/ app.py

# Run security scans
bandit -r gco/ cli/ -c pyproject.toml --severity-level medium

# Run tests with coverage (matches unit:pytest:core)
pytest tests/ --cov=gco --cov=cli --cov=gco_mcp --cov-report=html --cov-fail-under=90 \
    --ignore=tests/test_nag_compliance.py

# Run cdk-nag compliance matrix serially (matches unit:cdk:nag-compliance)
pytest tests/test_nag_compliance.py

# Run CDK config matrix serially (matches unit:cdk:config-matrix)
pytest tests/test_cdk_synthesis_matrix.py

# Regenerate the lockfile (after dependency changes — use the Docker workflow
# documented in Dependency Management above; pip-compile on the host produces
# a macOS-resolved lockfile that CI rejects)
docker run --rm -v "$(pwd):/workspace" -w /workspace gco-dev bash -c '
  pip-compile --no-emit-index-url --strip-extras --all-extras \
    -o requirements-lock.txt pyproject.toml &&
  sed -i "/^gco-cli @ file:/,+1d" requirements-lock.txt
'
```

#### Debugging a failing check

The README badge label tells you the workflow and job. For example, `unit:pytest:core` maps to:

- Workflow file: `.github/workflows/unit-tests.yml`
- Job ID: `unit-pytest-core`
- Actions UI: repo → Actions → "Unit Tests" → latest run → `unit:pytest:core`

Click any badge to land on the workflow page; the Actions UI lists every job.

#### Frozen GitLab pipeline

`.github/legacy/.gitlab-ci.yml` is kept as a frozen reference for anyone forking to GitLab. It is NOT maintained and may drift as tools evolve. GitHub Actions is authoritative. See `.github/legacy/README.md`.

### Integration Tests

```bash
# Deploy to test environment
export AWS_PROFILE=test
gco stacks deploy-all -y

# Run tests against deployed environment
pytest tests/ -v

# Clean up
gco stacks destroy-all -y
```

## Documentation

### When to Update Docs

- New features or capabilities
- Changes to deployment process
- New configuration options
- Breaking changes
- Bug fixes that affect users

### Documentation Files

- `TENETS.md`: Normative north star and prioritized project decision guidance
- `README.md`: Overview and quick start
- `QUICKSTART.md`: Step-by-step setup guide
- `docs/README.md`: Comprehensive top-level guide index
- `docs/ARCHITECTURE.md`: Technical architecture
- `docs/CLI.md`: CLI reference
- `docs/API.md`: REST API reference
- `docs/CONCEPTS.md`: Core concepts for new users
- `docs/CUSTOMIZATION.md`: How to customize
- `docs/TROUBLESHOOTING.md`: Common issues
- `docs/RUNBOOKS.md`: Operational runbooks for incident response
- `docs/adr/`: Architecture Decision Records — the append-only log of significant architectural decisions
- `wiki/` + `mkdocs.yml`: The orientation wiki published to GitHub Pages (see
  [Developing the wiki](#developing-the-wiki))
- `CONTRIBUTING.md`: This file

Repository inventories move in pairs and are guarded by tests. When adding or
removing a top-level `docs/*.md` guide, CLI command module, workflow, production
`image-*` dependency group/Dockerfile, or count-bearing MCP document, update its
human index in the same change. Run `tests/test_documentation_consistency.py`,
`tests/test_mcp_tool_count_docs.py`, and `tests/test_mcp_gating_consistency.py`
before pushing. Generated diagrams use the canonical aggregate command:

```bash
SOURCE_DATE_EPOCH=1788091200 \
GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> \
python diagrams/generate.py
python diagrams/generate.py --check
```

Commit substantive charted-source changes first, use that clean commit for
`GCO_DIAGRAM_SOURCE_COMMIT`, and commit the generated artifacts separately.
The generator fails if marker-stripped target source differs from that commit.

### Developing the wiki

The [project wiki](https://aws-solutions-library-samples.github.io/global-capacity-orchestrator-on-aws/)
is a small MkDocs site built from `wiki/*.md` and `mkdocs.yml`, published to
GitHub Pages by `pages.yml` with the coverage report embedded at `/coverage/`.
It is an orientation layer: pages **summarize and link** to the authoritative
docs on GitHub — they must not restate reference detail (flags, config keys,
procedures), which would rot. Deep-doc links use full
`https://github.com/.../blob/main/...` URLs (the docs are not part of the
built site), and images are referenced as `assets/images/<name>` — a build
hook serves the tracked `images/` directory, so never commit image copies.

To preview changes locally with live reload:

```bash
pip install -e ".[docs]"        # once, in your venv (or use the dev container)
./scripts/preview_wiki.sh       # strict build + live server on :8000
./scripts/preview_wiki.sh --build-only   # just the CI-equivalent strict build
```

The script's first phase runs `mkdocs build --strict` — the exact command the
`lint:mkdocs:strict` PR gate and the Pages deploy run — so a broken link or a
nav entry without a file fails locally before CI sees it. From the dev
container, forward the port yourself
(`docker run -p 8000:8000 ... ./scripts/preview_wiki.sh`); the `gco` shell
function does not forward ports. The locally served `/coverage/` path 404s by
design — the coverage report is merged in at deploy time, not built by MkDocs.

Before pushing wiki changes, also run the wiki's guard tests and markdownlint:

```bash
pytest tests/test_wiki.py -q
npm run lint:markdown
```

`tests/test_wiki.py` enforces the structural invariants (every page in the
nav, every repo link and image resolving, no external image hosts); keep
`mkdocs.yml` free of custom YAML tags so those guards can keep parsing it.

### Architecture Decision Records

Record architecturally significant decisions — ones that are expensive to reverse,
resolve a real conflict between the prioritized [project tenets](TENETS.md), or
shape the system in ways future contributors must understand — as an
[Architecture Decision Record](docs/adr/README.md). Copy `docs/adr/template.md`
to the next `docs/adr/NNNN-title.md`, fill in the context, decision, and
consequences, then add a row to the ADR index. See
[docs/adr/README.md](docs/adr/README.md) for when to write one and the full
process.

### Documentation Style

- Use clear, concise language
- Include code examples
- Add diagrams where helpful — GCO has two auto-generated diagram
  catalogues you can lean on or extend:
  - `diagrams/infra_diagrams/` — per-stack and full-architecture
    views synthesized from the CDK app via cdk-dia. Run
    `python diagrams/infra_diagrams/generate.py` to refresh.
  - `diagrams/code_diagrams/` — per-function control-flow charts
    (Lambda handlers, CLI entry points) rendered with pyflowchart +
    Playwright. Use the [canonical two-commit workflow](diagrams/README.md#quick-reference):
    commit charted source first, regenerate with a fixed `SOURCE_DATE_EPOCH`
    and exact `GCO_DIAGRAM_SOURCE_COMMIT`, then commit derived artifacts.
    Regeneration is incremental — only the charted sources you actually
    changed are re-rendered and restamped, so expect a handful of touched
    files rather than the whole catalogue. Generated marker blocks, HTML/PNG,
    and the index carry the stamp plus a flow-content digest, and
    `code_diagrams/provenance.json` records each source's digest so the
    freshness contract holds without resolving commits through Git. Add new
    targets by editing `diagrams/code_diagrams/_targets.py`.
- Keep it up-to-date with code changes

## Code Review Guidelines

### For Authors

- Keep PRs focused and reasonably sized
- Write clear PR descriptions
- Include tests
- Update documentation
- Respond to feedback promptly

### For Reviewers

- Be constructive and respectful
- Focus on code quality and maintainability
- Check the change against the prioritized [project tenets](TENETS.md), starting with the earliest affected tenet
- Check for security issues
- Verify documentation is updated
- Test changes if possible

## Release Process

### Versioning

We use semantic versioning (MAJOR.MINOR.PATCH):

- MAJOR: Breaking changes
- MINOR: New features (backward compatible)
- PATCH: Bug fixes

### CI/CD Token Setup

No long-lived tokens are required for the GitHub Actions pipeline. The release and dependency-scan workflows use the built-in `GITHUB_TOKEN`:

- `release.yml` (stage 1) needs `contents: write` to push the `release/vX.Y.Z` branch, `pull-requests: write` to open the release PR, and `actions: write` to dispatch the PR-gating CI workflows against that branch. It also requires the repository Actions setting "Allow GitHub Actions to create and approve pull requests" to be enabled.
- `release-publish.yml` (stage 2) needs `contents: write` to push the release tag and create the GitHub Release. Each workflow declares its permissions at the top of the file.
- `deps-scan.yml` needs `issues: write` to open a dependency-drift issue. Also declared at the top of the file.

If you fork and run your own copy, no PAT setup is needed — the tokens are generated per-run by GitHub. Do enable the Actions PR-creation setting above, and keep a `v*` tag ruleset blocking update/deletion/force-push (no bypass actors) so released tags stay immutable; tag creation stays open for the publish workflow, whose existing-tag check refuses to re-point a released version.

### Creating a Release

Releases run in two stages so `main` stays fully branch-protected — nothing,
including the release automation, pushes to it directly.

1. Go to the repository on GitHub → Actions → Release.
2. Click "Run workflow" on `main`, pick the bump type (`patch`, `minor`, or
   `major`), and run it.
3. Stage 1 (`release.yml`) bumps the version files on a `release/vX.Y.Z`
   branch, opens a pull request titled `Release vX.Y.Z`, and dispatches the
   PR-gating CI workflows against that branch (required checks appear on the
   PR as those runs finish).
4. Review, approve, and **squash-merge** the release PR. Squash keeps the
   merge commit subject `Release vX.Y.Z (#N)`, which stage 2 verifies before
   tagging.
5. Stage 2 (`release-publish.yml`) fires on the merge, creates the annotated
   `vX.Y.Z` tag on the merge commit, and creates the GitHub Release with
   auto-generated notes (categorized per `.github/release.yml`).

If stage 2 fails partway (for example, the tag pushed but the Release wasn't
created), re-run it from Actions → Release Publish → "Run workflow" on
`main`; every step is idempotent and completes only what's missing. A
VERSION change that reaches `main` without a `Release vX.Y.Z` commit subject
is deliberately not auto-tagged — publish it explicitly the same way after
review.

#### Manual Release (Alternative)

If you need to release without stage 1, open the version-bump PR by hand;
merging it still triggers stage 2:

```bash
# Bump version on a release branch
git checkout -b release/v1.2.3 main
python scripts/bump_version.py patch  # or minor/major

# Commit and open the PR (title must stay "Release v1.2.3")
git add VERSION gco/_version.py cli/__init__.py
git commit -m "Release v1.2.3"
git push -u origin release/v1.2.3
gh pr create --base main --title "Release v1.2.3" \
  --body "Manual version bump. release-publish.yml tags on merge."

# After approval, squash-merge; release-publish.yml tags the merge commit
# and creates the GitHub Release with generated notes.
```

Direct pushes of release commits to `main` are blocked by branch
protection, and existing `v*` tags cannot be moved or deleted (tag
ruleset with no bypass actors). Publishing through the workflow is the
supported path; its guards are what keep manual mistakes out of the tag
namespace.

When a new required status check is introduced (for example a new
cdk-nag matrix entry), add it to the branch protection required-checks
list on `main` as part of the same PR review; a check that reports on
every PR but isn't required is a silent gap in the merge gate.

After releasing, confirm the auto-generated GitHub Release notes read well (they are categorized by PR label per `.github/release.yml`), then deploy to production environments. The GitHub Releases page is GCO's changelog — there is no separate `CHANGELOG.md` to maintain.

### Dependency Updates

Dependency drift is tracked through three layers:

1. **Dependabot (weekly PRs)** — GitHub Actions, Docker, the locked root npm tooling graph, and the deployable inference-streaming Lambda graph. See `.github/dependabot.yml`. Python packages are intentionally excluded because `requirements-lock.txt` is managed through `pip-compile` and bumped intentionally.
2. **`deps-scan` workflow (monthly issue)** — runs on the 1st of each month at 09:00 UTC. Checks Python packages, Docker images, Helm charts, EKS add-on versions, Aurora PostgreSQL engine versions, pre-commit hook revisions, and accelerator catalog/NodePool/watch-list policy. Deterministic accelerator validation always runs offline; with OIDC credentials the scan also compares the checked-in catalog with the live enabled-Region EC2 union. If anything is out of date or an online check cannot run correctly, it updates one GitHub issue labeled `dependencies, automated`. The scan logic lives in [`.github/scripts/dependency-scan.sh`](.github/scripts/dependency-scan.sh) — see [`.github/CI.md`](.github/CI.md#dependency-scan-script) for the full reference (surfaces checked, inputs, outputs, extension points, failure modes). Pinned versions are centralised in [`gco/stacks/constants.py`](gco/stacks/constants.py).
3. **`cve-scan` workflow (weekly job)** — runs Mondays at 09:00 UTC. Re-runs Trivy against the latest CVE databases. A red run is the signal; the per-push `security.yml` workflow will catch the same issue on the next PR.

#### What Gets Checked by `deps-scan`

- **Python Packages**: direct dependencies resolved from `pyproject.toml` are checked against PyPI for newer versions; transitive-only drift is left to the direct package that owns it
- **Node/npm**: Node 24, npm, the CDK CLI, exact package pins, lockfile presence, and Dependabot coverage across every repository-owned `package.json`
- **Docker Images**: semver-tagged images referenced in `.github/workflows/*.yml`, K8s manifests, examples, and Helm chart values
- **Helm Charts**: from `lambda/helm-installer/charts.yaml`
- **EKS Add-ons**: extracted from `gco/stacks/constants.py` (requires AWS credentials via OIDC; records an explicit skip otherwise)
- **Accelerator Catalog and NodePools**: always runs `python scripts/accelerator_catalog.py validate` offline to reject deprecated scheduling, surface newer unreferenced generations, and require exact catalog/`watch_instance_types`/`ConfigLoader` synchronization; with AWS credentials, compares the catalog with NVIDIA GPU and AWS Neuron instance types returned by EC2 across enabled commercial Regions
- **Pre-commit Hooks**: `rev:` pins in `.pre-commit-config.yaml` are compared against the latest tag published by the upstream GitHub repo

#### Running the Dependency Check Manually

The monthly scan is also wired to `workflow_dispatch`:

1. Go to Actions → "Deps scan" → "Run workflow".
2. Pick the `main` branch and click Run.
3. On completion, either a new issue appears (if drift was found) or the workflow just turns green.

#### Checking EKS Addon Versions

The EKS add-on check is one of several AWS-backed dependency surfaces. EKS
cluster-version, Aurora, EMR, Bedrock, and online EC2 accelerator discovery also
use the OIDC role. Without credentials, each online surface is explicitly marked
skipped rather than reported current; deterministic accelerator/NodePool
validation still runs offline. To inspect EKS add-ons manually:

```bash
# Check latest versions for all addons used by GCO
K8S_VERSION="1.36"  # Match your configured Kubernetes version

for addon in metrics-server aws-efs-csi-driver amazon-cloudwatch-observability aws-fsx-csi-driver; do
  echo "=== $addon ==="
  aws eks describe-addon-versions \
    --addon-name "$addon" \
    --kubernetes-version "$K8S_VERSION" \
    --query 'addons[0].addonVersions[0].addonVersion' \
    --output text
done
```

Current add-on versions are defined in `gco/stacks/constants.py` and consumed by
`gco/stacks/regional_stack.py`. To update:

1. Run the command above to get latest versions
2. Update the matching `EKS_ADDON_*` constants in `gco/stacks/constants.py`
3. Test the deployment in a non-production environment first
4. Review the [EKS addon release notes](https://docs.aws.amazon.com/eks/latest/userguide/eks-add-ons.html) for breaking changes

## Best Practices

### Security

- Never commit secrets or credentials
- Use IAM roles, not access keys
- Follow least-privilege principle
- Encrypt sensitive data
- Review security groups and network ACLs

### Performance

- Optimize Docker images (use slim base images)
- Set appropriate resource limits
- Use caching where possible
- Monitor and profile performance

### Cost Optimization

- Use Spot instances for fault-tolerant workloads
- Right-size resources
- Clean up unused resources
- Set up cost alerts

### Reliability

- Implement health checks
- Use multiple replicas
- Test failure scenarios
- Monitor and alert on issues

## Common Tasks

### Adding a New Kubernetes Manifest

```bash
# 1. Create manifest file
cat > lambda/kubectl-applier-simple/manifests/33-my-service.yaml << 'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-service
  namespace: gco-system
spec:
  replicas: 2
  selector:
    matchLabels:
      app: my-service
  template:
    metadata:
      labels:
        app: my-service
    spec:
      containers:
        - name: my-service
          image: {{MY_SERVICE_IMAGE}}
          ports:
            - containerPort: 8080
EOF

# 2. Update CDK stack to build image (if needed)
# Edit gco/stacks/regional_stack.py

# 3. Deploy
gco stacks deploy-all -y
```

### Updating Service Code

```bash
# 1. Make changes to service
vim gco/services/health_monitor.py

# 2. Test locally (if possible)
python gco/services/health_monitor.py

# 3. Rebuild and deploy
gco stacks deploy-all -y

# 4. Verify deployment
kubectl get pods -n gco-system
gco jobs list -r us-east-1
```

### Debugging Issues

```bash
# Check CloudFormation events
aws cloudformation describe-stack-events \
  --stack-name gco-us-east-1 \
  --region us-east-1 \
  --max-items 20

# Resolve generated Lambda and log-group physical names
aws cloudformation list-stack-resources \
  --stack-name gco-us-east-1 \
  --region us-east-1 \
  --query "StackResourceSummaries[?ResourceType=='AWS::Lambda::Function' || ResourceType=='AWS::Logs::LogGroup'].{Type:ResourceType,Logical:LogicalResourceId,Physical:PhysicalResourceId}"

# Tail the exact log group selected from the output above
aws logs tail <EXACT_LOG_GROUP_NAME> \
  --region us-east-1 \
  --since 30m

# Check pod logs (requires kubectl for detailed pod inspection)
kubectl logs -n gco-system deployment/health-monitor --tail=100

# Describe pod for events (requires kubectl)
kubectl describe pod POD-NAME -n gco-system

# Check job logs via CLI
gco jobs logs JOB-NAME -n gco-jobs -r us-east-1
```

## Getting Help

- Check existing documentation
- Search for similar issues
- Open a GitHub issue

## Code of Conduct

- Be respectful and professional
- Welcome newcomers
- Focus on constructive feedback
- Collaborate openly

---

**Questions?** Open an issue on the [GCO GitHub repository](https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/issues).
