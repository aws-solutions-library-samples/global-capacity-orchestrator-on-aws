# Maintenance Guide

Routine upkeep for GCO: adding new instance types, upgrading the EKS
Kubernetes version, refreshing base-image security patches, renewing CVE
suppressions, and acting on the monthly dependency scan. It also covers the
engineering-process areas that keep the project healthy over time — the
dependency-pinning policy, testing and CI hygiene, the release and deployment
flow, monitoring and alerting, day-to-day code health, and how a new maintainer
gets oriented.

This guide is the *how*. The monthly [`deps-scan` workflow](../.github/CI.md#dependency-scan-script)
is the *when* — it opens or refreshes one issue when a pinned version or the EC2
accelerator catalog drifts. Accelerator discovery is automated, but lifecycle,
architecture, replacement, and NodePool scheduling policy remain explicit human
review decisions.

## Table of contents

- [Maintenance at a glance](#maintenance-at-a-glance)
- [Adding a new instance type or family](#adding-a-new-instance-type-or-family)
- [Upgrading the EKS Kubernetes version](#upgrading-the-eks-kubernetes-version)
- [Refreshing base-image security patches](#refreshing-base-image-security-patches)
- [Renewing CVE suppressions](#renewing-cve-suppressions)
- [Routine dependency bumps](#routine-dependency-bumps)
- [Refreshing the Bedrock default model](#refreshing-the-bedrock-default-model)
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
| Monthly | Review the dependency-scan issue, including accelerator catalog and NodePool findings | Automated `deps-scan` issue |
| When EC2 accelerator drift appears | Review family policy, refresh the catalog, and update eligible NodePools/watch lists together | `deps-scan` **Accelerator Catalog and NodePools** row or AWS launch announcement |
| When the scan flags EKS standard-support ending (or ~yearly) | Upgrade the EKS Kubernetes minor | `deps-scan` **EKS Kubernetes Version** row |
| When the scan flags an epoch older than 45 days (or [Trivy](https://trivy.dev/) finds an OS CVE) | Bump the base-image security epoch | `deps-scan` **Base-image Security Epochs** row |
| ~30 days before a suppression `exp:` date | Renew or drop the CVE suppression | `deps-scan` **Suppression Expiries** row |
| When the scan flags a newer same-family model | Bump the Bedrock default model pin | `deps-scan` **Bedrock default model** row |
| Weekly | Check the `cve-scan` result and act on new findings | Monday `cve-scan` run |
| Every PR | Keep measured Python coverage ~92% and label the PR so release notes categorize | Opening a pull request |
| Every release | Bump the version and confirm the generated GitHub Release notes | Cutting a version |
| On alarm | Follow the matching runbook in `docs/RUNBOOKS.md` | CloudWatch alarm via the SNS alert topic |

## Adding a new instance type or family

GCO separates three concerns that must not be conflated:

1. **Discovery** — which NVIDIA GPU and AWS Neuron instance types EC2 currently
   advertises in any enabled commercial Region.
2. **Observation** — which types the capacity-history poller watches, including
   types retained for historical visibility.
3. **Scheduling policy** — which reviewed families each [Karpenter](https://karpenter.sh/) NodePool may
   select for new workloads.

The checked-in catalog makes the first two concerns complete and deterministic;
NodePool family lists keep the third concern deliberate.

### Sources of truth

| Purpose | Authoritative file(s) | Contract |
|---------|-----------------------|----------|
| EC2 accelerator inventory | `gco/config/accelerator_catalog.json` → `instance_types` | Sorted union of instance types with an NVIDIA GPU or AWS Neuron device across enabled commercial Regions |
| Reviewed family policy | `gco/config/accelerator_catalog.json` → `families` | Accelerator, architecture, track, generation, lifecycle, scheduling eligibility, reason, and replacements |
| Capacity-history observation | `cdk.json` → `historical.watch_instance_types`; fallback in `gco/config/config_loader.py` | Both copies must exactly equal the catalog's `instance_types` list |
| Karpenter scheduling | `lambda/kubectl-applier-simple/manifests/40-*.yaml` through `46-*.yaml` | Explicit `eks.amazonaws.com/instance-family` policy per workload class |
| Rich CLI hardware/pricing metadata | `cli/capacity/models.py` and the curated defaults in `cli/capacity/advisor.py` | Add only when the CLI needs local vCPU, memory, accelerator, or advisor metadata |
| Pinned examples and prose | `examples/*.yaml`, `gco/stacks/regional_stack.py`, `README.md`, `docs/CUSTOMIZATION.md` | Keep selectors and human guidance aligned with reviewed scheduling support |

`45-nodepool-cpu-general.yaml` selects by category and generation, so accelerator
catalog maintenance does not require per-type CPU edits.

> **Instance-family gotcha:** EKS Auto Mode labels a node with the exact family
> segment AWS uses in its catalog. `p5`, `p5e`, and `p5en` are separate families;
> `p6-b200`, `p6-b300`, and `p6e-gb200` are separate as well. Catalog presence
> never makes a family schedulable automatically. For example, `p3dn.24xlarge`
> remains observable while the deprecated `p3dn` family is prohibited from new
> NodePools.

### Deterministic offline validation

Run this before and after every accelerator or NodePool change:

```bash
python scripts/accelerator_catalog.py validate
python -m pytest tests/test_accelerator_catalog.py -q
```

The validator needs no AWS credentials and fails with actionable guidance when:

- a NodePool references a deprecated or end-of-life family, naming the exact
  manifest and reviewed replacements;
- a newer active generation in the same scheduling track is absent from every
  eligible NodePool, naming the pools to review;
- `cdk.json` or the `ConfigLoader` fallback omits or adds a watched type; or
- the catalog, family metadata, architecture, lifecycle, or manifest policy is
  malformed or contradictory.

Normal pull-request CI runs both commands. Do not replace this deterministic gate
with live EC2 calls.

### Monthly online drift

The monthly dependency scan always runs offline validation first. With valid OIDC
credentials it then calls `DescribeRegions` and paginated
`DescribeInstanceTypes` sequentially, using adaptive retries, and compares the
live enabled-Region union with the checked-in catalog. Ordinary drift updates the
same rolling dependency issue and does not fail the scheduled workflow; an API,
credential, or parser failure becomes one explicit operational finding rather
than a false “current” result.

Run the same comparison manually with:

```bash
python scripts/accelerator_catalog.py check-online --json-summary

# Optional human-readable report for review
python scripts/accelerator_catalog.py check-online \
  --report /tmp/accelerator-catalog-drift.md --json-summary
```

Exit code `0` means current, `1` means reviewed action is needed for catalog
drift, and `2` means the online check itself failed.

### Reviewing and refreshing catalog drift

1. Read every added, removed, and family-metadata change in the report. Confirm
   it against the AWS launch or lifecycle information; catalog output is
   untrusted discovery data, not policy.
2. For a new family, add explicit `accelerator`, `architectures`, `track`,
   `generation`, and `lifecycle` metadata first. Add `manifest_allowed`,
   `reason`, and `replacements` when the default active/allowed policy is not
   correct.
3. Decide which NodePools, if any, should schedule the family. Check CPU
   architecture, accelerator class, [EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)/RDMA requirements, memory and FP8
   capability, workload fit, and regional support. Deprecated and end-of-life
   families must not enter new scheduling.
4. Refresh to a review file first. The command refuses unknown families and EC2
   metadata that disagrees with reviewed family policy:

   ```bash
   python scripts/accelerator_catalog.py refresh \
     --output /tmp/accelerator_catalog.json
   ```

   Successful refresh output embeds `last_refreshed_at` as a UTC ISO-8601
   timestamp. Read-only `validate`, `capture`, and `check-online` runs never
   rewrite that timestamp.

5. Review the diff, then replace the catalog's `instance_types` with the approved
   output. Synchronize `historical.watch_instance_types` in `cdk.json` and the
   fallback in `gco/config/config_loader.py`; offline validation reports every
   missing or extra type if either copy is incomplete.
6. Update NodePools, CLI hardware metadata/advisor defaults, examples, and prose
   only where the reviewed support decision requires it.
7. Run the focused suite:

   ```bash
   python scripts/accelerator_catalog.py validate
   pytest tests/test_accelerator_catalog.py \
     tests/test_capacity_history_config.py \
     tests/test_mooncake_nodepool_manifest.py \
     tests/test_cli.py tests/test_inference.py tests/test_integration.py
   ```

For an existing family with a newly released size, no NodePool family edit may
be necessary, but the catalog and both observation lists still move together.
For a new generation, the validator intentionally remains advisory until a
maintainer records the scheduling decision.

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
6. Helm pins — if a new Helm is needed for the minor, bump the `HELM_VERSION`
   env in both `.github/workflows/deps-scan.yml` and
   `.github/workflows/integration-tests.yml` (the `integration:helm:charts-valid`
   job) and the `get.helm.sh` URL in `lambda/helm-installer/Dockerfile`. The
   `deps-scan` **Version Consistency** check flags the two workflow env pins if
   they drift apart; the Dockerfile copy is a hardcoded `RUN` line it can't see,
   so keep that one in lockstep by hand.
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

- `APT_SECURITY_EPOCH` (Debian images): `Dockerfile.dev` and the six
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
every surface that has drifted (Python packages, npm graphs, Docker images,
Helm charts, EKS add-ons, accelerator catalog/NodePool policy, CI tooling, and
more), grouped with an urgency hint and per-row links to the upstream source.
To act on it:

1. Follow the report's **Ref** links to review changelogs for breaking changes.
2. Update the exact version in `pyproject.toml`, the relevant `package.json`, a
   manifest, `charts.yaml`, or the pinned `*_VERSION` env/ARG value.
3. Regenerate the lock that belongs to the changed graph:
   - Python: regenerate `requirements-lock.txt` with the supported container
     workflow documented below.
   - Root npm tooling: run
     `npm install --save-dev --save-exact --ignore-scripts --no-audit --no-fund <package>@<version>`.
   - Streaming Lambda: run
     `npm --prefix lambda/inference-streaming-proxy install --save-exact --ignore-scripts --no-audit --no-fund <package>@<version>`.
4. Reconcile any **Version Consistency** rows so every copy of a pin agrees.
5. Run the affected checks and open a PR; CI independently audits both npm
   graphs and rejects lock, runtime, or dependency-management drift.

Python dependencies are intentionally not tracked by [Dependabot](https://docs.github.com/en/code-security/dependabot) — they are
pinned through `requirements-lock.txt` with `pip-compile` and reviewed
deliberately. GitHub Actions, Docker base images, and both repository-owned npm
graphs *are* tracked by Dependabot; see
[Dependabot](../.github/CI.md#dependabot) for the split.

## Refreshing the Bedrock default model

GCO's two optional, advisory Bedrock features — Mission sampling (`gco mission
...`) and the capacity advisor (`gco capacity ai-recommend` / `predict` and the
`ai_recommend` MCP tool) — default to **Anthropic Claude Opus 5** through its
global cross-Region inference profile (`global.anthropic.claude-opus-5`). The
model id and reasoning preference have one checked-in source: `cdk.json`
`context.bedrock`, whose stock `thinking.effort` is `high` (Claude's default
adaptive-thinking level). Mission sampling and the capacity advisor resolve both
values through the lightweight `gco.bedrock` module; the same file is shipped
as package data for installed CLI/MCP use. The consistency test guards the
compatibility aliases, reasoning translation, packaging, inference-profile
shape, and captured default-model fixture.

Because it is an Anthropic model, the default additionally requires the one-time
[Anthropic first-time-use form](CUSTOMIZATION.md#accepting-the-anthropic-first-time-use-form)
on the account. Bedrock answers `FTUFormNotFilled` until it is submitted. GCO
raises `BedrockFTUFormNotAcceptedError` for that code rather than degrading to a
deterministic fallback, so a missing form surfaces as an actionable error on
every path that reaches Bedrock.

`gco.bedrock` translates the canonical effort into whichever reasoning dialect
the default model speaks:

| Default model family | Converse fields | Inference controls dropped |
|----------------------|-----------------|-----------------------------|
| Claude adaptive thinking (Opus 4.6+, Sonnet 4.6, Mythos/Fable) | `thinking.type=adaptive` + `output_config.effort` | `temperature`, `topP`, `topK` (removed from Opus 4.7 onward) |
| Nova 2 | `reasoningConfig.maxReasoningEffort` | `maxTokens`, `temperature`, `topP` — at `high` effort only |

The adaptive-thinking model list is enumerated in `gco/bedrock.py` rather than
pattern-matched, because pre-4.6 Claude models reject `adaptive` and need the
legacy `enabled` + `budget_tokens` form; an unlisted default therefore receives
no reasoning fields rather than a guessed request shape. Reasoning tokens are
billed as output tokens and high effort can materially increase cost and
latency. Explicit model overrides keep their existing inference controls and do
not inherit the default's reasoning fields.

Because it is a deployment configuration value — not a `pyproject.toml` entry,
a Dockerfile `FROM`, or a manifest image — Dependabot never sees it. The monthly
[`deps-scan`](../.github/CI.md#dependency-scan-script) closes that gap: its
**Bedrock default model** check reads the `cdk.json` context value, lists the
system-defined inference profiles in `us-east-1`, and flags a newer release **in
the same model family** — a future global Claude Opus release, never a jump to
a different scope, tier, or provider (that is a choice, not drift). Family
derivation tolerates all three revision shapes Bedrock ships
(`-vMAJOR:MINOR`, a bare `-vMAJOR`, and no suffix at all), so one model line
stays one family. The check needs AWS
credentials via OIDC; without them the scan skips it with a noted reason, so a
credential-less run is not a false "up to date".

When the scan flags a newer same-family model (or you decide to move the default
deliberately):

1. Change `cdk.json` `context.bedrock.default_model_id` to the new id and set
   `context.bedrock.thinking.effort` to a level the model supports. The stock
   value is a system-defined **global inference profile**; global profiles can
   route worldwide and are unsuitable when a geography boundary is required.
   Use an appropriate geography-scoped profile (`us.` / `eu.` / `jp.` / etc.)
   where data residency requires it. If the new model speaks a reasoning
   dialect GCO does not yet translate, add it to the dialect dispatch in
   `gco/bedrock.py` — otherwise the configured effort is silently inert. Update
   the intentionally independent `_EXPECTED_DEFAULT_MODEL_ID`,
   `_EXPECTED_FIXTURE_NAME`, and thinking review
   pins in `tests/test_default_bedrock_model_consistency.py`; those assertions
   are not runtime defaults, but they make model, fixture, and reasoning changes
   explicit in review.
2. Capture a genuine fixture for the exact profile id:
   `python3 scripts/capture_scaffold_fixtures.py --model <id> --region us-east-1`.
   The canonical directive set makes three paid calls; high reasoning can make
   the run substantially slower and more expensive.
3. Run the Mission and capacity suites, then open a PR. The consistency guard
   proves both runtime aliases and the dependency scanner still resolve the
   same `cdk.json` value.

Picking a *different* model — for regulatory, data-residency, model-governance,
or cost reasons, or to avoid the Anthropic FTU form — rather than tracking
Claude Opus releases is an operator choice,
not routine maintenance; the override paths (per-call flag,
`GCO_MISSION_BEDROCK_MODEL_ID`, or changing the default) live in
[Bedrock Model Selection](CUSTOMIZATION.md#bedrock-model-selection). Both
features are advisory and degrade gracefully: when no model is reachable Mission
falls back to deterministic templates and the advisor surfaces a clear error, so
a stale pin never blocks core orchestration.

## Maintaining the MCP server

The in-tree MCP server (`gco_mcp/`) exposes the docs, example manifests, and
`gco` operations to agents. A few catalogs must stay in sync with the rest of
the repo or CI fails — most of this is "when you add X, register it in Y":

| When you… | Update | Guard |
|-----------|--------|-------|
| Add a `docs/*.md` guide | `DOC_METADATA` in `gco_mcp/resources/docs.py` (keep `topics` from the existing small vocabulary; every `related` entry must reference a real key) | `tests/test_mcp_docs_index.py` |
| Add an `examples/*.yaml` manifest | `EXAMPLE_METADATA` in `gco_mcp/resources/docs.py` | `find_examples` discovery |
| Add a package README meant for agents | `PACKAGE_DOC_METADATA` in `gco_mcp/resources/docs.py` | `tests/test_mcp_docs_index.py` |
| Add a normative root document | The root Markdown file, `ROOT_DOC_METADATA`, a static `docs://gco/{name}` resource, and `docs_index()` in `gco_mcp/resources/docs.py` | `tests/test_mcp_docs_index.py`, `tests/test_mcp_server.py`, `tests/test_mcp_integration.py` |
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
- The Python build backend — an exact `==` pin in `pyproject.toml`
  `[build-system] requires` (setuptools). pip resolves it inside build
  isolation, so it never appears in the runtime environment or the lock;
  the monthly scan compares the pin against PyPI and flags any entry that
  is not an exact pin.
- The full Python transitive closure — `requirements-lock.txt`, generated by
  `pip-compile`.
- Repository development tooling — exact `devDependencies` in the root
  `package.json` with the full graph in the adjacent `package-lock.json`.
- The deployable response-streaming Lambda — exact `dependencies` in
  `lambda/inference-streaming-proxy/package.json` with a separate adjacent
  lockfile, so development tools cannot enter the production asset.
- Both npm manifests pin `engines.node` and `packageManager`; the monthly scan
  keeps those values aligned with `.nvmrc`, `Dockerfile.dev`, and
  `LAMBDA_NODEJS_RUNTIME` in `gco/stacks/constants.py`.
- Versions that live outside `pyproject.toml` — workflow `*_VERSION` env pins,
  Dockerfile `ARG`s, `lambda/helm-installer/charts.yaml`,
  `gco/stacks/constants.py`, the Python-constant Mooncake default image in
  `cli/images.py`, and the Bedrock model at
  `cdk.json` `context.bedrock.default_model_id` (see
  [Refreshing the Bedrock default model](#refreshing-the-bedrock-default-model)).
  These are tracked by the monthly scan rather than Dependabot.

No open ranges. Every direct Python and npm dependency uses an exact version,
and each graph commits its resolved lock. If a local install needs a range to
resolve, the environment is dirty — fix the environment, don't loosen the pin.

### Updating a dependency

1. Change the exact pin (`pyproject.toml`, the appropriate `package.json`, a
   manifest, `charts.yaml`, or a `*_VERSION`), reviewing the upstream changelog
   for breaking changes.
2. For an npm dependency, update only its owning graph and adjacent lockfile:

   ```bash
   # Install and verify npm from the exact packageManager pin first.
   bash .github/scripts/use-pinned-npm.sh package.json

   # Root development tooling
   npm install --save-dev --save-exact --ignore-scripts --no-audit --no-fund \
     <package>@<version>

   # Production streaming-Lambda dependencies
   npm --prefix lambda/inference-streaming-proxy install \
     --save-exact --ignore-scripts --no-audit --no-fund <package>@<version>
   ```

   Never copy root tooling into the Lambda graph. Commit both the changed
   manifest and its `package-lock.json`.
3. For a Python dependency, regenerate the lockfile through the container — the
   only supported path, so the result matches CI's Linux resolution:

   ```bash
   docker build -f Dockerfile.dev -t gco-dev .
   docker run --rm -v "$(pwd):/workspace" -w /workspace gco-dev bash -c '
     pip-compile --no-emit-index-url --strip-extras --all-extras \
       -o requirements-lock.txt pyproject.toml &&
     sed -i "/^gco-cli @ file:/,+1d" requirements-lock.txt
   '
   ```

4. Run the affected checks, then open a PR. CI rejects stale Python or npm
   lockfiles, unmanaged npm graphs, and inconsistent Node/npm/CDK pins.

See [Regenerating the Lockfile](../CONTRIBUTING.md#regenerating-the-lockfile)
for the full rationale, and [Routine dependency bumps](#routine-dependency-bumps)
for acting on a monthly drift report.

### Checking for vulnerabilities

| Layer | What runs | Cadence |
|-------|-----------|---------|
| `security.yml` | bandit, pip-audit, npm audit (every owned graph), Trivy (filesystem + per-image), semgrep, checkov, KICS, trufflehog, gitleaks, [CodeQL](https://codeql.github.com/docs/) (Python + JavaScript) | Every push + PR |
| `cve-scan.yml` | Trivy re-run against fresh CVE databases | Weekly (Mon 09:00 UTC) |
| `deps-scan.yml` | Version drift across every pinned surface | Monthly |

When a scanner flags a CVE with no upstream fix yet, suppress it with an
expiring entry — see [Renewing CVE suppressions](#renewing-cve-suppressions).

**Automated updates.** Dependabot is scoped to **GitHub Actions, Docker, and
both repository-owned npm graphs** (`.github/dependabot.yml`); Python stays on
the deliberate `pip-compile` path above. The security workflow runs
`npm audit` independently in every discovered graph, and Advanced Setup CodeQL
analyzes both Python and JavaScript. See
[Dependabot](../.github/CI.md#dependabot) for the rationale.

## Testing and CI hygiene

### Coverage expectation

Python line + branch coverage has an enforced floor of **90%** (`fail_under = 90`
in `pyproject.toml` `[tool.coverage.report]`), applied by `unit:pytest:core`
with `--cov-fail-under=90` over `gco`, `cli`, and `gco_mcp`. The project still
targets **~92% measured coverage** for pull requests and releases; review the
CI artifact against that target without raising the global failure floor. The
dedicated `unit:node:inference-streaming-proxy` job separately requires at
least 93% lines, functions, and branches from Node.js 24's built-in V8
coverage. The Python HTML report is published to GitHub Pages after each
`main` run by `pages.yml`. Ship new code with tests that hold the ~92% target
rather than lowering the 90% floor.

### Test layout

- `tests/` — the pytest suite. Markers are declared in `pyproject.toml`
  `[tool.pytest.ini_options]` (`slow`, `integration`, `unit`, `mission_e2e`,
  `mooncake_image`, `helm_online`, `asyncio`), and `addopts` includes
  `--strict-markers` so a typo'd marker fails instead of silently skipping.
- Heavy tests are opt-in via env vars so the default run stays fast:
  `GCO_MOONCAKE_IMAGE_TEST=1` (pulls the ~9 GB [vLLM](https://docs.vllm.ai/en/latest/) image) and
  `GCO_HELM_CHART_VALIDATION=1` (needs `helm` + network).
- `tests/BATS/` — Bash tests for the shell scripts (`dependency-scan.sh`, the
  demo recorders, cluster-access setup), run by the `unit:bats:*` jobs.
- `tests/inference-streaming-proxy/` — native `node:test` coverage for the
  production response-streaming Lambda. Its isolated Node.js 24 workflow runs
  `npm ci` in the Lambda's dependency graph before enforcing 93%
  line/function/branch coverage.
- Many tests are **guard tests** that pin an invariant so a partial change
  fails loudly — version-skew guards, manifest-shape guards, the docs index,
  the pip-audit-ignore validator, and accelerator catalog/NodePool/watch-list
  synchronization. A guard failure means "you changed things that must move
  together," not "delete the assertion."

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
  FSx/[Valkey](https://valkey.io/)/Aurora services, and custom application metrics.
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
  `/metrics` ([Prometheus](https://prometheus.io/docs/introduction/overview/)). These four are the only paths the auth middleware
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
rot. One code-diagram generation uses one UTC timestamp across HTML metadata
and visible content, PNG pixels, the generated README, and every source marker.
Normal runs intentionally record their wall-clock invocation time and can
produce metadata-only changes even when source code is unchanged. Set a fixed
integer `SOURCE_DATE_EPOCH` when byte-reproducible output is required. Never
hand-edit an individual artifact's timestamp — regenerate the complete target
catalogue.

## Onboarding for maintainers

A new maintainer should become productive from the docs alone. Read in this
order:

1. **Orientation** — [`TENETS.md`](../TENETS.md), [`README.md`](../README.md),
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
