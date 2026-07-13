# Maintenance Guide

Routine upkeep for GCO: adding new instance types, upgrading the EKS
Kubernetes version, refreshing base-image security patches, renewing CVE
suppressions, and acting on the monthly dependency scan. It also covers the
engineering-process areas that keep the project healthy over time — the
dependency-pinning policy, testing and CI hygiene, the release and deployment
flow, monitoring and alerting, day-to-day code health, and how a new maintainer
gets oriented.

This guide is the *how*. The monthly [`deps-scan` workflow](../.github/CI.md#dependency-scan-script)
is the *when* — it opens an issue when a pinned version falls behind upstream.
Some lists here (instance families, for example) are deliberately not
auto-bumped: adding hardware is a human decision, so the scan leaves them to
this runbook.

## Table of contents

- [Maintenance at a glance](#maintenance-at-a-glance)
- [Adding a new instance type or family](#adding-a-new-instance-type-or-family)
- [Upgrading the EKS Kubernetes version](#upgrading-the-eks-kubernetes-version)
- [Refreshing base-image security patches](#refreshing-base-image-security-patches)
- [Renewing CVE suppressions](#renewing-cve-suppressions)
- [Routine dependency bumps](#routine-dependency-bumps)
- [Maintaining the MCP server](#maintaining-the-mcp-server)
- [Dependency management policy](#dependency-management-policy)
- [Testing and CI hygiene](#testing-and-ci-hygiene)
- [Release and deployment](#release-and-deployment)
- [Monitoring and alerting](#monitoring-and-alerting)
- [Code health](#code-health)
- [Onboarding for maintainers](#onboarding-for-maintainers)

## Maintenance at a glance

| Cadence | Task | Trigger |
|---------|------|---------|
| Monthly | Review the dependency-scan issue and bump flagged pins | Automated `deps-scan` issue |
| When AWS ships new hardware | Add instance types / families to the lists below | AWS launch announcement |
| When the scan flags EKS standard-support ending (or ~yearly) | Upgrade the EKS Kubernetes minor | `deps-scan` **EKS Kubernetes Version** row |
| When the scan flags an epoch older than 45 days (or Trivy finds an OS CVE) | Bump the base-image security epoch | `deps-scan` **Base-image Security Epochs** row |
| ~30 days before a suppression `exp:` date | Renew or drop the CVE suppression | `deps-scan` **Suppression Expiries** row |
| Weekly | Check the `cve-scan` result and act on new findings | Monday `cve-scan` run |
| Every PR | Keep coverage ≥ 90% and label the PR so release notes categorize | Opening a pull request |
| Every release | Bump the version and confirm the generated GitHub Release notes | Cutting a version |
| On alarm | Follow the matching runbook in `docs/RUNBOOKS.md` | CloudWatch alarm via the SNS alert topic |

## Adding a new instance type or family

There is no single list of instance types. Which files you touch depends on
*why* you are adding the hardware — CLI capacity analysis, a training pool, a
serving pool, and so on. Use the table, then follow the matching recipe.

### Where instance types live

| Purpose | File(s) to edit | Format |
|---------|-----------------|--------|
| CLI capacity/spot analysis, `gco capacity` | `cli/capacity/models.py` → `GPU_INSTANCE_SPECS` | Python dict: exact type → `InstanceTypeInfo(vcpus, mem, gpu_count, gpu_type, gpu_mem, arch)` |
| Default set the capacity advisor probes | `cli/capacity/advisor.py` → default `instance_types` list | Python list of exact types |
| x86 general GPU pool | `lambda/kubectl-applier-simple/manifests/40-nodepool-gpu-x86.yaml` | Karpenter `instance-family` values |
| ARM64 GPU pool | `.../41-nodepool-gpu-arm.yaml` | Karpenter `instance-family` values |
| Inference-optimized GPU pool | `.../42-nodepool-inference.yaml` | Karpenter `instance-family` values |
| EFA distributed-training pool | `.../43-nodepool-efa.yaml` | Karpenter `instance-family` values |
| Neuron / Trainium / Inferentia pool | `.../44-nodepool-neuron.yaml` | Karpenter `instance-family` values |
| Curated ≥80 GB FP8 serving pool (Mooncake) | `.../46-nodepool-mooncake-efa.yaml` | Karpenter `instance-family` values |
| Example jobs that pin a family | `examples/*.yaml` (e.g. `trainium-job.yaml`, `inferentia-job.yaml`, `megatrain-sft-job.yaml`, `inference-sglang.yaml`) | `nodeSelector` / affinity |
| Human-facing lists (keep accurate, not load-bearing) | `gco/stacks/regional_stack.py` (nodepool comment block), `README.md`, `docs/CUSTOMIZATION.md` | Prose / comments |

`45-nodepool-cpu-general.yaml` selects by category and generation rather than a
family list, so it does **not** need per-hardware edits.

> **Instance-family gotcha:** EKS Auto Mode labels a node with the exact family
> segment AWS uses in its catalog, and a bare entry only matches that one
> family. `p5`, `p5e`, and `p5en` are three separate families; `p6-b200`,
> `p6-b300`, and `p6e-gb200` are three more. Enumerate every generation you
> want — see the header note in `43-nodepool-efa.yaml`.

### Recipe: a new GPU instance type visible to the CLI

1. Add an entry to `GPU_INSTANCE_SPECS` in `cli/capacity/models.py`. The
   `gpu_type` field (for example `H100`, `B200`) is what ties the accelerator
   to its family, so fill it in accurately.
2. If the type belongs in the advisor's default probe set, add it to the
   `instance_types` list in `cli/capacity/advisor.py`.
3. Update the tests that assert on the specs (see [below](#tests-that-must-change-together)).

### Recipe: a new accelerator family for a node pool

1. Add the family string to the `values` list under the
   `eks.amazonaws.com/instance-family` requirement in the matching NodePool
   manifest from the table above.
2. If the family is a new GPU generation, confirm the pool's other
   requirements still apply (GPU manufacturer, architecture, EFA taint).
3. For a serving-tier GPU, add it to `46-nodepool-mooncake-efa.yaml` **only** if
   it is a ≥80 GB FP8-capable part, and keep `p4d` out of that curated pool.
4. Refresh the human-facing lists (the nodepool comment block in
   `regional_stack.py`, the `README.md` nodepool summary, and
   `docs/CUSTOMIZATION.md`) so they stay accurate.

### Tests that must change together

These tests assert on the instance lists and will fail if you edit a list
without updating them:

- `tests/test_cli.py` — expects `GPU_INSTANCE_SPECS` to contain the baseline
  types (`g4dn.xlarge`, `g5.xlarge`, `p3.2xlarge`, `p4d.24xlarge`).
- `tests/test_capacity_history_config.py` — derives the family set from the
  spec keys and asserts a baseline set is present.
- `tests/test_mooncake_nodepool_manifest.py` — pins the pool 46 family set
  **exactly** (`_EXPECTED_FAMILIES`) and requires `p4d` to stay in pool 43.
  Edit this whenever you change either pool's families.
- `tests/test_inference.py` — asserts an example's `nodeSelector` family
  (for example `inf2`); update if you change the referenced example.
- `tests/test_integration.py` — validates the shape of every manifest under
  `lambda/kubectl-applier-simple/`, so a new pool must keep the NodePool schema.

Run the focused set after editing:

```bash
pytest tests/test_cli.py tests/test_capacity_history_config.py \
  tests/test_mooncake_nodepool_manifest.py tests/test_inference.py \
  tests/test_integration.py
```

## Upgrading the EKS Kubernetes version

The version lives in one place — `cdk.json` `context.kubernetes_version` (for
example `1.36`) — and flows through `gco/config/config_loader.py`
(`get_kubernetes_version()`) into `GCORegionalStack`, which resolves it to
`eks.KubernetesVersion.V1_<minor>` (falling back to `.of()` if the installed
`aws-cdk-lib` does not yet expose that enum). Several pinned tools track the
same minor and are guarded by tests, so a partial bump fails CI rather than
shipping a skew.

### Files to change, in order

1. `cdk.json` — set `context.kubernetes_version` to the new minor.
2. `pyproject.toml` — bump `kubernetes==<minor>.x` so the Python client's major
   equals the cluster minor (for example `kubernetes==37.*` for EKS `1.37`),
   then regenerate the lock:

   ```bash
   pip-compile --all-extras --strip-extras -o requirements-lock.txt pyproject.toml
   ```

3. `gco/stacks/constants.py` — update the five `EKS_ADDON_*` constants to builds
   published for the new minor (see [validating add-ons](#validating-add-on-versions)).
4. Confirm the pinned `aws-cdk-lib` exposes `eks.KubernetesVersion.V1_<minor>`.
   If it does not, bump `aws-cdk-lib` in `pyproject.toml` and re-lock; otherwise
   the stack silently uses the `.of()` fallback.
5. kubectl pins — bump to a patch of the new minor in all three spots, staying
   within one minor of the cluster:
   - `Dockerfile.dev` (`ARG KUBECTL_VERSION`)
   - `lambda/helm-installer/Dockerfile` (the `dl.k8s.io/release/...` URL)
   - `.github/workflows/deps-scan.yml` (`KUBECTL_VERSION` env)
6. Helm pins — bump `HELM_VERSION` / the `get.helm.sh` URL in the same
   `deps-scan.yml` and `lambda/helm-installer/Dockerfile` if a new Helm is
   needed for the minor.
7. `.github/workflows/integration-tests.yml` — bump the kind `node_image`
   (`kindest/node:v<minor>.<patch>`) so CI exercises the new control plane.
8. `.github/config/.trivyignore` — revisit any suppressions tied to the old
   kubectl/helm binaries; several entries clear once the pins move.
9. `tests/test_config_loader.py` and `tests/test_config_loader_validation.py` —
   update the hardcoded default minor.

### Validating add-on versions

The `EKS_ADDON_*` builds are `-eksbuild.N` releases tied to a specific minor.
Query the ones supported for the new minor (recipe also in
[CONTRIBUTING.md](../CONTRIBUTING.md)):

```bash
K8S_VERSION="1.37"  # the minor you are moving to
for addon in eks-pod-identity-agent metrics-server aws-efs-csi-driver \
             amazon-cloudwatch-observability aws-fsx-csi-driver; do
  echo "=== $addon ==="
  aws eks describe-addon-versions \
    --addon-name "$addon" \
    --kubernetes-version "$K8S_VERSION" \
    --query 'addons[0].addonVersions[0].addonVersion' \
    --output text
done
```

### Version-skew rules

- **kubectl** must stay within ±1 minor of the cluster (the standard Kubernetes
  skew policy — see the comment on `ARG KUBECTL_VERSION` in `Dockerfile.dev`).
- **kubernetes Python client**: its major must equal the cluster minor
  (`kubernetes==36.x` ↔ EKS `1.36`). Enforced by
  `tests/test_integration.py::test_kubernetes_python_client_matches_eks_version`.
- **kubectl in the helm-installer image** must match `cdk.json`. Enforced by
  `tests/test_integration.py::test_kubectl_version_matches_eks_version`.

### Deploy and verify

1. Run the guard tests first — they catch a partial bump immediately:

   ```bash
   pytest tests/test_integration.py tests/test_config_loader.py \
     tests/test_config_loader_validation.py
   ```

2. Deploy to a non-production account/region and confirm the cluster reaches
   the new version and every add-on reconciles.
3. Watch the first NodePool scale-up so a new node image / add-on combination
   does not regress GPU or EFA scheduling.
4. Roll out to remaining regions once non-prod is healthy.

## Refreshing base-image security patches

The container images pull OS security patches at build time behind a
hand-bumped epoch ARG that busts the CI layer cache. Bump the date to force a
rebuild that pulls the latest fixes. The dependency scan flags an epoch older
than 45 days; Trivy's container scan is the backstop.

- `APT_SECURITY_EPOCH` (Debian images): `Dockerfile.dev` and the four
  `dockerfiles/*-dockerfile` service images.
- `DNF_SECURITY_EPOCH` (Amazon Linux 2023): `lambda/helm-installer/Dockerfile`.

Set the ARG default to today's date (`YYYY-MM-DD`), rebuild, and re-run the
container scan to confirm the previously flagged packages are gone.

## Renewing CVE suppressions

`.github/config/.trivyignore` and `.github/config/.pip-audit-ignore` entries
each carry an `exp:YYYY-MM-DD` marker and a justification. The rules:

- The dependency scan surfaces entries expiring within 30 days so you can act
  early; the CI validator hard-fails a build on the expiry date itself.
- When an entry is due, re-check upstream. Drop it if the fix has shipped
  (and bump whatever pin carries the fix); otherwise extend the date with a
  fresh justification — never extend blindly.
- Keep both files short. A growing ignore file is a smell.

## Routine dependency bumps

The monthly [`deps-scan`](../.github/CI.md#dependency-scan-script) issue lists
every surface that has drifted (Python packages, Docker images, Helm charts,
EKS add-ons, CI tooling, and more), grouped with an urgency hint and per-row
links to the upstream changelog. To act on it:

1. Follow the report's **Ref** links to review changelogs for breaking changes.
2. Update the version in `pyproject.toml`, the manifest, `charts.yaml`, or the
   pinned `*_VERSION` env/ARG value.
3. Regenerate `requirements-lock.txt` if Python dependencies changed.
4. Reconcile any **Version Consistency** rows so every copy of a pin agrees.
5. Run the test suite locally, then open a PR.

Python dependencies are intentionally not tracked by Dependabot — they are
pinned through `requirements-lock.txt` with `pip-compile` and reviewed
deliberately. GitHub Actions and Docker base images *are* tracked by Dependabot;
see [Dependabot](../.github/CI.md#dependabot) for the split.

## Maintaining the MCP server

The in-tree MCP server (`gco_mcp/`) exposes the docs, example manifests, and
`gco` operations to agents. A few catalogs must stay in sync with the rest of
the repo or CI fails — most of this is "when you add X, register it in Y":

| When you… | Update | Guard |
|-----------|--------|-------|
| Add a `docs/*.md` guide | `DOC_METADATA` in `gco_mcp/resources/docs.py` (keep `topics` from the existing small vocabulary; every `related` entry must reference a real key) | `tests/test_mcp_docs_index.py` |
| Add an `examples/*.yaml` manifest | `EXAMPLE_METADATA` in `gco_mcp/resources/docs.py` | `find_examples` discovery |
| Add a package README meant for agents | `PACKAGE_DOC_METADATA` in `gco_mcp/resources/docs.py` | `tests/test_mcp_docs_index.py` |
| Add or rename an MCP tool | the Tool Reference table (and the per-module count) in `gco_mcp/tools/README.md` | `tests/test_docs_coverage.py` |
| Gate a tool behind a feature flag | `gco_mcp/feature_flags.py`, and document the flag in `gco_mcp/README.md` | — |

Notes:

- **Version** — the MCP server version tracks the project `VERSION` through
  `gco_mcp/version.py`; there is no separate MCP version to bump.
- **Dependency** — `fastmcp` is pinned in `pyproject.toml` and covered by the
  monthly dependency scan (Python packages) and the weekly CVE scan. After
  bumping it, re-run the MCP install smoke (`unit:mcp:install`) to confirm the
  server still launches and registers its tools.
- This guide is itself registered in `DOC_METADATA`, so `find_docs` surfaces it
  to agents — the same step every new guide needs.

## Dependency management policy

GCO pins **every** direct dependency to an exact version and commits a fully
resolved lockfile, so a clean checkout installs the same graph CI ran.

### What is pinned, and where

- Direct Python deps and their extras (`cdk`, `diagrams`, `inference-monitor`,
  `mcp`, `lint`, `typecheck`, `test`, `security`, `dev`) — exact `==` pins in
  `pyproject.toml`.
- The full transitive closure — `requirements-lock.txt`, generated by
  `pip-compile`.
- Versions that live outside `pyproject.toml` — workflow `*_VERSION` env pins,
  Dockerfile `ARG`s, `lambda/helm-installer/charts.yaml`,
  `gco/stacks/constants.py`, and the Python-constant image/model pins. These
  are tracked by the monthly scan rather than Dependabot.

No open ranges. If a local `pip install` needs a range to resolve, the venv is
dirty — fix the environment, don't loosen the pin.

### Updating a dependency

1. Change the pin (`pyproject.toml`, a manifest, `charts.yaml`, or a
   `*_VERSION`), reviewing the upstream changelog for breaking changes.
2. Regenerate the lockfile through the container — the only supported path, so
   the result matches CI's Linux resolution:

   ```bash
   docker build -f Dockerfile.dev -t gco-dev .
   docker run --rm -v "$(pwd):/workspace" -w /workspace gco-dev bash -c '
     pip-compile --no-emit-index-url --strip-extras --all-extras \
       -o requirements-lock.txt pyproject.toml &&
     sed -i "/^gco-cli @ file:/,+1d" requirements-lock.txt
   '
   ```

3. Run the suite locally, then open a PR. The lockfile-freshness check in
   `unit-tests.yml` fails if `requirements-lock.txt` drifts from
   `pyproject.toml`.

See [Regenerating the Lockfile](../CONTRIBUTING.md#regenerating-the-lockfile)
for the full rationale, and [Routine dependency bumps](#routine-dependency-bumps)
for acting on a monthly drift report.

### Checking for vulnerabilities

| Layer | What runs | Cadence |
|-------|-----------|---------|
| `security.yml` | bandit, pip-audit, Trivy (filesystem + per-image), semgrep, checkov, KICS, trufflehog, gitleaks, CodeQL | Every push + PR |
| `cve-scan.yml` | Trivy re-run against fresh CVE databases | Weekly (Mon 09:00 UTC) |
| `deps-scan.yml` | Version drift across every pinned surface | Monthly |

When a scanner flags a CVE with no upstream fix yet, suppress it with an
expiring entry — see [Renewing CVE suppressions](#renewing-cve-suppressions).

**Automated updates.** Dependabot is scoped to **GitHub Actions and Docker
only** (`.github/dependabot.yml`); Python stays on the deliberate `pip-compile`
path above. See [Dependabot](../.github/CI.md#dependabot) for the rationale.

## Testing and CI hygiene

### Coverage expectation

Line + branch coverage must stay **≥ 90%** (`fail_under = 90` in
`pyproject.toml` `[tool.coverage.report]`), enforced by the `unit:pytest:core`
job with `--cov-fail-under=90` over `gco`, `cli`, and `gco_mcp`. The HTML report
is published to GitHub Pages after each `main` run by `pages.yml`. Ship new code
with tests that hold the line rather than lowering the threshold.

### Test layout

- `tests/` — the pytest suite. Markers are declared in `pyproject.toml`
  `[tool.pytest.ini_options]` (`slow`, `integration`, `unit`, `mission_e2e`,
  `mooncake_image`, `helm_online`, `asyncio`), and `addopts` includes
  `--strict-markers` so a typo'd marker fails instead of silently skipping.
- Heavy tests are opt-in via env vars so the default run stays fast:
  `GCO_MOONCAKE_IMAGE_TEST=1` (pulls the ~9 GB vLLM image) and
  `GCO_HELM_CHART_VALIDATION=1` (needs `helm` + network).
- `tests/BATS/` — Bash tests for the shell scripts (`dependency-scan.sh`, the
  demo recorders, cluster-access setup), run by the `unit:bats:*` jobs.
- Many tests are **guard tests** that pin an invariant so a partial change
  fails loudly — version-skew guards, manifest-shape guards, the docs index,
  the pip-audit-ignore validator. A guard failure means "you changed two things
  that must move together," not "delete the assertion."

### Flaky-test triage

There is no auto-retry wrapper — a flake is treated as a bug, not hidden.

1. Reproduce in isolation: `pytest tests/test_x.py::test_y -x`.
2. If it only fails under the parallel runner, re-run serially to confirm
   ordering/shared-state coupling: `pytest -p no:xdist tests/test_x.py` (the
   heavier jobs use `pytest-xdist -n auto`).
3. Fix the root cause (shared state, wall-clock assumptions, test ordering). If
   a flake can't be fixed immediately, quarantine it with
   `@pytest.mark.xfail(strict=False, reason="<tracking issue>")` rather than
   leaving it to fail intermittently and erode trust in the gate.

### CI pipeline maintenance

- The four badged gates and the satellites are documented in
  [`.github/CI.md`](../.github/CI.md) — that file is the single source of truth
  for what each workflow covers.
- **Caches:** `actions/setup-python` keys the pip cache on
  `requirements-lock.txt`; the mypy jobs key `.mypy_cache` on
  `hashFiles('pyproject.toml', 'requirements-lock.txt')`. Both invalidate
  automatically when dependencies change — don't hand-clear them.
- **Runners and tools:** jobs run on `ubuntu-latest` with actions pinned by
  major version (bumped by Dependabot). Hand-installed CI tools
  (`TRIVY_VERSION`, `HELM_VERSION`, `KUBECTL_VERSION`, the kind node image) are
  tracked by the scan's **CI tooling** and **Version consistency** rows, so a
  pin that must move in lockstep across files is caught there.
- On an EKS bump the kind `node_image` moves too — see
  [Upgrading the EKS Kubernetes version](#upgrading-the-eks-kubernetes-version).

Pytest configuration lives in **one** place — `pyproject.toml`
`[tool.pytest.ini_options]`. (A top-level `pytest.ini` used to shadow it and
silently take precedence; it was removed and its settings — `asyncio_mode`, the
marker list, `--strict-markers` — merged in. Don't reintroduce a second config
file.)

## Release and deployment

### Versioning

Semantic Versioning (`MAJOR.MINOR.PATCH`). `VERSION` is the source of truth;
`scripts/bump_version.py` mirrors it into `gco/_version.py` and
`cli/__init__.py`, and `pyproject.toml` reads it dynamically
(`version = {attr = "gco._version.__version__"}`). `gco_mcp` reports the same
version, so there is no second number to bump.

### Cadence

Releases are cut **on demand**, not on a calendar — ship when a change set
warrants it (the tag history is mostly frequent patch releases). There is no
release train to keep.

### Cutting a release

Actions → **Release** → *Run workflow* → pick `patch` / `minor` / `major`.
`release.yml` then bumps the version, commits, pushes a `v<x.y.z>` tag, and
creates a GitHub Release. Full steps (and the manual fallback) are in
[Creating a Release](../CONTRIBUTING.md#creating-a-release).

### Changelog

The changelog is the **auto-generated GitHub Release notes**, categorized by PR
label via `.github/release.yml` (Breaking / Features / Bug fixes / Documentation
/ Dependencies / Other). Every PR must carry the right label — the leading
`feat:` / `fix:` / `docs:` token from `.github/pull_request_template.md` is what
drives categorization. There is no separate `CHANGELOG.md` — the Releases page
is the changelog.

### Deploying

The CDK app (`app.py`) synthesizes stacks in dependency order: `GCOGlobalStack`
→ `GCOApiGatewayGlobalStack` → per-region `GCORegionalStack` →
`GCOMonitoringStack` → optional `GCOAnalyticsStack`. Deploy with the CLI:

```bash
gco stacks deploy-all -y            # every stack, in dependency order
gco stacks deploy gco-us-east-1 -y  # a single stack
```

Roll out to a non-production account/region first, watch the first NodePool
scale-up, then promote to the remaining regions — the same staged pattern as an
EKS upgrade.

### Rollback

There is no one-click release rollback; infrastructure is declarative, so you
roll *forward to the previous known-good tag*:

1. A failed `cdk deploy` auto-rolls-back that stack via CloudFormation — the
   prior template stays live.
2. To undo a shipped release, check out the previous tag and redeploy:

   ```bash
   git checkout v<previous>
   gco stacks deploy-all -y
   ```

3. Stateful resources such as the ECR image repositories default to *retain*
   (`gco/stacks/global_stack.py`), so rolling back compute never silently drops
   them. For incident recovery of a specific subsystem, use the matching
   [runbook](RUNBOOKS.md).

## Monitoring and alerting

### Where it lives

`gco/stacks/monitoring_stack.py` (`GCOMonitoringStack`, deployed to the
monitoring region resolved in `app.py`) creates the observability surface:

- **Dashboard** — one CloudWatch dashboard with per-region widgets for Global
  Accelerator, API Gateway, Lambda, SQS, DynamoDB, EKS, ALBs, the optional
  FSx/Valkey/Aurora services, and custom application metrics.
- **Alarms** — metric and composite alarms for EKS CPU/memory, ALB unhealthy
  hosts, response time, manifest-processing failures, Lambda errors/throttles,
  SQS message age (stuck jobs), DynamoDB throttling, API Gateway 5XX, and
  secret-rotation failure.
- **Alert delivery** — a single SNS topic (`alert_topic`) fed by the composite
  alarms. **Subscribe your on-call channel to it** — an alarm with no
  subscriber is silent (see the secret-rotation runbook).

Shape is guarded by `tests/test_monitoring_stack.py` and
`tests/test_regional_stack.py`.

### Custom metrics and health checks

- **Metrics:** `gco/services/metrics_publisher.py` publishes to the
  `GCO/HealthMonitor` and `GCO/ManifestProcessor` namespaces (dimensions
  `ClusterName`, `Region`) from `health_monitor.py` and `manifest_processor.py`.
- **Health endpoints:** `gco/services/health_api.py` exposes `/healthz`
  (liveness), `/readyz` (readiness), `/api/v1/health` (detailed, 200/503), and
  `/metrics` (Prometheus). These four are the only paths the auth middleware
  leaves unauthenticated (`gco/services/auth_middleware.py`), so ALB and Global
  Accelerator health checks reach them without a token.

### Logs and rotation

Services emit structured JSON (`gco/services/structured_logging.py`, tunable via
`LOG_FORMAT` / `LOG_LEVEL`) for CloudWatch Logs Insights. Retention is bounded,
not open-ended:

- Monitoring log groups use `RetentionDays.ONE_MONTH` (`monitoring_stack.py`).
- S3 access logs expire per `s3_access_logs.retention_days` in `cdk.json`
  (default 90 days).
- Capacity-history rows carry a DynamoDB TTL (default 90 days).

### On-call

`docs/RUNBOOKS.md` holds nine step-by-step incident procedures (region
unhealthy, DLQ filling, secret-rotation failure, cost spike, and more), and the
alarms above page through the SNS alert topic. Keep one runbook per alarm so a
page maps to an action.

## Code health

### Lint and format — enforced, not suggested

Ruff owns both formatting and linting (`pyproject.toml` `[tool.ruff]`: line
length 100, target `py314`, rules `E,W,F,I,B,C4,UP,ARG,SIM`). mypy runs in a
`--strict`-equivalent mode (`[tool.mypy]`). Both run three ways so nothing slips:

- **Pre-commit** (`.pre-commit-config.yaml`): `ruff-format`, `ruff --fix`,
  `mypy` (on `gco/config|models|services` + `cli/`), `yamllint`,
  `markdownlint-cli2`. Install once with `pre-commit install`.
- **CI** (`lint.yml`): `ruff`, `mypy` (`strict` / `stacks` / `lambda`), plus
  `actionlint`, `hadolint`, `shellcheck`, `yamllint`, `markdownlint`.
- **Locally** before a PR: the exact trio in
  [Pre-Pull-Request Verification](../CONTRIBUTING.md#pre-pull-request-verification).

Keep the Ruff pin identical in `pyproject.toml`, `.pre-commit-config.yaml`, and
`lint.yml`; the scan's **Version consistency** row fails if they drift.

### Dead code

Ruff removes unused imports (`F401`) and unused variables (`F841`) on every run,
in both pre-commit and CI — that is the dead-code coverage the toolchain
enforces.

### Tech-debt tracking

Two mechanisms track debt today: CVE suppressions carry an `exp:` date that CI
enforces (so a suppression can't outlive its justification — see [Renewing CVE
suppressions](#renewing-cve-suppressions)), and the monthly dependency scan
surfaces version drift as a single rolling issue.

### Keep the diagrams current

`diagrams/infra_diagrams/` (per-stack CDK views) and `diagrams/code_diagrams/`
(per-function flowcharts) regenerate via their `generate.py` scripts. Refresh
them when architecture or a charted handler changes so the visual docs don't
rot.

## Onboarding for maintainers

A new maintainer should become productive from the docs alone. Read in this
order:

1. **Orientation** — [`README.md`](../README.md),
   [`QUICKSTART.md`](../QUICKSTART.md), and the [docs index](README.md) (which
   carries a full reading order for users and operators).
2. **How it is built** — [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) and
   [`docs/CONCEPTS.md`](CONCEPTS.md), backed by the generated `diagrams/`.
3. **How to work on it** — [`CONTRIBUTING.md`](../CONTRIBUTING.md). The dev
   container is the supported environment; it matches CI bit-for-bit and avoids
   dependency-resolution pain.

### Package-level READMEs (the close-to-the-code context)

| Area | README |
|------|--------|
| CDK stacks | `gco/stacks/README.md` |
| In-cluster services | `gco/services/README.md` |
| CLI | `docs/CLI.md` |
| MCP server + tools | `gco_mcp/README.md`, `gco_mcp/tools/README.md` |
| Lambdas | `lambda/*/README.md` |
| CI / GitHub config | `.github/CI.md` |
| Tests | `tests/README.md` |
| Scripts / examples / diagrams | `scripts/README.md`, `examples/README.md`, `diagrams/README.md` |

### Where decisions and tribal knowledge live

Architecturally significant decisions are recorded as
[Architecture Decision Records](adr/README.md) under `docs/adr/` — an
append-only log of the context, decision, and consequences behind each choice.
Record a new one (copy `docs/adr/template.md`) whenever you make a decision that
is expensive to reverse or that shapes the system in a way future maintainers
must understand; see [`docs/adr/README.md`](adr/README.md) for when and how.

Rationale that does not rise to the level of an ADR still lives close to the
code: PR history and the categorized GitHub Release notes, and unusually
thorough inline comments — [`.github/CI.md`](../.github/CI.md), the
justification strings in `gco/stacks/nag_suppressions.py`, and the comment
headers on the workflows and `pyproject.toml`. When you make a non-obvious
change, add the "why" in those same places.

### Explore by asking

GCO ships an MCP server (`gco_mcp/`); connect it to an agent and ask questions
like "what stacks does the app deploy?" — it reads the source and docs to
answer. Setup is in [`gco_mcp/README.md`](../gco_mcp/README.md).
