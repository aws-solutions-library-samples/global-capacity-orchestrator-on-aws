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
- [Code Organization](#code-organization)
  - [Directory Structure](#directory-structure)
  - [Adding New Features](#adding-new-features)
- [Testing](#testing)
  - [Running Tests Locally](#running-tests-locally)
  - [Pre-Pull-Request Verification](#pre-pull-request-verification)
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

GCO uses exact-pinned dependencies in `pyproject.toml` and a committed lockfile (`requirements-lock.txt`) for reproducible builds.

#### Dependency Groups

| Group | Install command | What it includes |
|-------|----------------|------------------|
| Core | `pip install -e .` | CLI runtime deps (boto3, click, requests, etc.) |
| CDK | `pip install -e ".[cdk]"` | AWS CDK, cdk-nag, constructs (for stack synthesis) |
| Dev | `pip install -e ".[dev]"` | Everything: CDK + lint + typecheck + test + security |
| MCP | `pip install -e ".[mcp]"` | FastMCP server |

CDK dependencies are in a separate `[cdk]` extras group so operators who only use the CLI don't need to install the full CDK toolchain.

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
  pip-compile --no-emit-index-url --strip-extras --all-extras \
    -o requirements-lock.txt pyproject.toml &&
  sed -i "/^gco-cli @ file:/,+1d" requirements-lock.txt
'
```

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

The in-cluster services use token-based authentication via AWS Secrets Manager. The auth middleware (`gco/services/auth_middleware.py`) validates an `X-GCO-Auth-Token` header on every request (except health checks).

**Important:** The middleware is fail-closed by default. If `AUTH_SECRET_ARN` is not set and `GCO_DEV_MODE` is not enabled, all authenticated requests return 503. To run services locally without Secrets Manager:

```bash
export GCO_DEV_MODE=true
```

This is intentional — a missing `AUTH_SECRET_ARN` in production should fail loudly rather than silently allowing unauthenticated access.

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
├── api-gateway-proxy/       # API Gateway proxy Lambda
├── ga-registration/         # Global Accelerator registration
├── secret-rotation/         # Secret rotation Lambda
└── alb-header-validator/    # ALB header validation

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

Before you open a pull request, run the same three checks CI runs — the test
suite (`pytest`), the linter/formatter (`ruff`), and the type checker
(`mypy`) — so your change is verified locally before it ever reaches the
pipeline. These are the exact tools invoked by the `unit:pytest:core`,
`lint:ruff:python`, and `lint:mypy:strict` jobs described under
[CI/CD Pipeline](#cicd-pipeline), so a clean local run means a clean CI run.

Run all three from your dev-container shell (the recommended path — see
[Using the Dev Container (Recommended)](#using-the-dev-container-recommended)):

```bash
ruff format --check gco/ cli/ gco_mcp/ tests/ lambda/ scripts/ diagrams/
ruff check gco/ cli/ gco_mcp/ tests/ lambda/ scripts/ diagrams/
mypy gco/ cli/ gco_mcp/ scripts/ --exclude 'gco/stacks/'
pytest tests/ --cov=gco --cov=cli --cov=gco_mcp --cov-fail-under=90
```

**Success indicator:** all four commands complete with no reported failures —
`ruff` reports no formatting diffs and no lint findings, `mypy` reports no type
errors, and `pytest` reports all tests passing with coverage at or above the
threshold. When every command exits cleanly, your change is verified and ready
for a pull request. If any command reports a failure, fix it and re-run the
full sequence before submitting.

For what to update alongside code changes, see the [Testing](#testing) and
[Documentation](#documentation) guidance below and the
[Contributing section of the README](README.md#contributing).

### CI/CD Pipeline

The project uses GitHub Actions for automated testing. Every push and pull request runs five primary workflows in parallel, plus three satellites on schedule or manual trigger.

#### Primary workflows (run on every push + PR)

| Workflow file | README row | Purpose |
|---------------|------------|---------|
| `.github/workflows/unit-tests.yml` | Unit Tests | pytest with coverage, BATS, CLI smoke, CDK synth + config matrix, lockfile freshness, fresh install, workload import checks |
| `.github/workflows/integration-tests.yml` | Integration Tests | Per-Dockerfile build + healthcheck, kind cluster E2E (with Calico for NetworkPolicy enforcement), K8s manifest schema, Lambda import validation, cross-module pytest, MCP server pytest |
| `.github/workflows/security.yml` | Security | bandit, pip-audit, trivy (filesystem + per-image), trufflehog, gitleaks, semgrep, checkov, KICS |
| `.github/workflows/lint.yml` | Linting | actionlint, hadolint, markdownlint, mypy (strict/stacks/lambda), ruff (format + check, imports included), shellcheck, yamllint |
| `.github/workflows/mooncake-image.yml` | — (no badge) | Mooncake vLLM image contract: runs the real upstream image GCO defaults to (`cli/images.py::_DISAGGREGATED_DEFAULT_IMAGE`) and asserts the PD proxy starts under `python3` + serves `/healthz`, the rendered store config is accepted by the image's loader, and the connector names GCO emits are registered — so an image-version bump is validated by CI |

Each workflow file has a comment header documenting triggers and per-job purpose — that is the single source of truth. Every job uses `category:tool:test_name` display names (e.g., `unit:pytest:core`, `security:trivy:container-scan`) and `category-tool-test_name` job IDs.

#### Satellite workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `.github/workflows/release.yml` | Manual (`workflow_dispatch`) | Bump version, tag, and create a GitHub Release with auto-generated notes |
| `.github/workflows/deps-scan.yml` | `cron: 0 9 1 * *` (monthly) | Check Python/Docker/Helm/EKS-addon versions; open an issue if drift detected |
| `.github/workflows/cve-scan.yml` | `cron: 0 9 * * 1` (weekly) | Re-run Trivy against current CVE databases |

#### Auto-generated badges

Three README badges update automatically from `push: main` runs:

- `unit:pytest:core` test count
- `unit:bats:count`
- `unit:coverage` percentage

Values are published to the orphan `badges` branch as shields.io endpoint JSON and consumed via `img.shields.io/endpoint?url=…`. Fork PRs cannot write to this branch — the publish step is gated on `push: main`.

#### Running the pipeline locally

You can simulate the CI pipeline locally:

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run linters (matches lint.yml jobs)
ruff format --check gco/ cli/ gco_mcp/ tests/ lambda/ scripts/ diagrams/
ruff check gco/ cli/ gco_mcp/ tests/ lambda/ scripts/ diagrams/
yamllint --strict .

# Run markdownlint (requires Node; no Python install needed).
# Config lives in .markdownlint-cli2.yaml at the repo root.
npx markdownlint-cli2

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

# Run cdk-nag compliance matrix (matches unit:cdk:nag-compliance)
pytest tests/test_nag_compliance.py -n auto

# Run CDK config matrix (matches unit:cdk:config-matrix)
pytest tests/test_cdk_synthesis_matrix.py -n auto

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

`.gitlab-ci.yml` is kept as a frozen reference for anyone forking to GitLab. It is NOT maintained and may drift as tools evolve. GitHub Actions is authoritative.

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

- `README.md`: Overview and quick start
- `QUICKSTART.md`: Step-by-step setup guide
- `docs/ARCHITECTURE.md`: Technical architecture
- `docs/CLI.md`: CLI reference
- `docs/API.md`: REST API reference
- `docs/CONCEPTS.md`: Core concepts for new users
- `docs/CUSTOMIZATION.md`: How to customize
- `docs/TROUBLESHOOTING.md`: Common issues
- `docs/RUNBOOKS.md`: Operational runbooks for incident response
- `CONTRIBUTING.md`: This file

### Documentation Style

- Use clear, concise language
- Include code examples
- Add diagrams where helpful — GCO has two auto-generated diagram
  catalogues you can lean on or extend:
  - `diagrams/infra_diagrams/` — per-stack and full-architecture
    views synthesized from the CDK app via AWS PDK cdk-graph. Run
    `python diagrams/infra_diagrams/generate.py` to refresh.
  - `diagrams/code_diagrams/` — per-function control-flow charts
    (Lambda handlers, CLI entry points) rendered with pyflowchart +
    Playwright. Run `python diagrams/code_diagrams/generate.py` to
    refresh; the script auto-inserts a `# Flowchart:` pointer
    comment at the top of every source file it charts so readers
    can navigate from code to diagram without guessing. Add new
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

No long-lived tokens are required for the GitHub Actions pipeline. Both the release and dependency-scan workflows use the built-in `GITHUB_TOKEN`:

- `release.yml` needs `contents: write` to push the version commit, tag, and create the GitHub Release. The workflow declares this at the top of the file.
- `deps-scan.yml` needs `issues: write` to open a dependency-drift issue. Also declared at the top of the file.

If you fork and run your own copy, no setup is needed — the tokens are generated per-run by GitHub.

### Creating a Release

Releases are triggered from the Actions tab:

1. Go to the repository on GitHub → Actions → Release.
2. Click "Run workflow".
3. Pick the bump type (`patch`, `minor`, or `major`) and click "Run workflow".

The workflow will:

- Run `scripts/bump_version.py` with the chosen bump type.
- Commit the version change to `main` (as `github-actions[bot]`).
- Create and push a `v<new-version>` git tag.
- Create a GitHub Release with auto-generated notes (categorized per `.github/release.yml`).

#### Manual Release (Alternative)

If you need to release manually:

```bash
# Bump version
python scripts/bump_version.py patch  # or minor/major

# Commit and tag
git add VERSION gco/_version.py cli/__init__.py
git commit -m "Release v1.2.3"
git tag -a v1.2.3 -m "Release v1.2.3"
git push origin main
git push origin v1.2.3

# Create the GitHub Release with generated notes
gh release create v1.2.3 --generate-notes
```

After releasing, update CHANGELOG.md and deploy to production environments.

### Dependency Updates

Dependency drift is tracked through three layers:

1. **Dependabot (weekly PRs)** — GitHub Actions and Docker only. See `.github/dependabot.yml`. Python packages are intentionally excluded because `requirements-lock.txt` is managed through `pip-compile` and bumped intentionally.
2. **`deps-scan` workflow (monthly issue)** — runs on the 1st of each month at 09:00 UTC. Checks Python packages, Docker images, Helm charts, EKS add-on versions, Aurora PostgreSQL engine versions, and pre-commit hook revisions. If anything is out of date, it opens a GitHub issue labeled `dependencies, automated`. The scan logic lives in [`.github/scripts/dependency-scan.sh`](.github/scripts/dependency-scan.sh) — see [`.github/CI.md`](.github/CI.md#dependency-scan-script) for the full reference (surfaces checked, inputs, outputs, extension points, failure modes). Pinned versions are centralised in [`gco/stacks/constants.py`](gco/stacks/constants.py).
3. **`cve-scan` workflow (weekly job)** — runs Mondays at 09:00 UTC. Re-runs Trivy against the latest CVE databases. A red run is the signal; the per-push `security.yml` workflow will catch the same issue on the next PR.

#### What Gets Checked by `deps-scan`

- **Python Packages**: all packages resolved from `pyproject.toml` are checked against PyPI for newer versions
- **Docker Images**: semver-tagged images referenced in `.github/workflows/*.yml`, K8s manifests, examples, and Helm chart values
- **Helm Charts**: from `lambda/helm-installer/charts.yaml`
- **EKS Add-ons**: extracted from `gco/stacks/regional_stack.py` (requires AWS credentials via OIDC; falls back gracefully otherwise)
- **Pre-commit Hooks**: `rev:` pins in `.pre-commit-config.yaml` are compared against the latest tag published by the upstream GitHub repo

#### Running the Dependency Check Manually

The monthly scan is also wired to `workflow_dispatch`:

1. Go to Actions → "Deps scan" → "Run workflow".
2. Pick the `main` branch and click Run.
3. On completion, either a new issue appears (if drift was found) or the workflow just turns green.

#### Checking EKS Addon Versions

EKS addon versions are checked by `deps-scan` when AWS credentials are configured. Without credentials, the addon section is skipped silently. To check manually at any time:

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

Current addon versions are defined in `gco/stacks/regional_stack.py`. To update:

1. Run the command above to get latest versions
2. Update the `addon_version` parameter for each addon in `regional_stack.py`
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

# Check Lambda logs
aws logs tail /aws/lambda/gco-us-east-1-KubectlApplier* \
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

**Questions?** Open an issue on the [GCO GitHub repository](https://github.com/awslabs/global-capacity-orchestrator-on-aws/issues).
