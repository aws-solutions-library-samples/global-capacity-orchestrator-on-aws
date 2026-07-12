# `.github/` — GitHub-native configuration

Everything GitHub reads from this folder: CI/CD workflows, issue and PR templates, Dependabot config, CODEOWNERS, composite actions used by the workflows, and helper scripts.

For contributor-facing docs (how to run tests locally, release process, dependency updates), see [`CONTRIBUTING.md`](../CONTRIBUTING.md).

## Table of contents

- [Layout](#layout)
- [Workflows](#workflows)
  - [Primary (run on every push + PR)](#primary-run-on-every-push--pr)
  - [Satellites](#satellites)
  - [Naming conventions](#naming-conventions)
  - [Cross-cutting defaults](#cross-cutting-defaults)
- [Composite actions](#composite-actions)
- [CodeQL config](#codeql-config)
- [README badges](#readme-badges)
- [Issue & PR templates](#issue--pr-templates)
- [CODEOWNERS](#codeowners)
- [Dependabot](#dependabot)
- [Helper scripts](#helper-scripts)
  - [Dependency-scan script](#dependency-scan-script)
  - [pip-audit-ignore validator](#pip-audit-ignore-validator)
- [Kind config](#kind-config)
- [Markdownlint config](#markdownlint-config)
- [Running checks locally](#running-checks-locally)

## Layout

```text
.github/
├── actions/
│   └── build-lambda-package/       # Composite action: stage Lambda build dirs
├── codeql/
│   └── codeql-config.yml           # Paths + query-filters for Code Scanning
├── ISSUE_TEMPLATE/
│   ├── bug_report.md
│   ├── config.yml                  # Blank-issue + contact links config
│   └── feature_request.md
├── kind/
│   └── kind-calico.yaml            # Kind cluster config for integration:kind:cluster-e2e
├── scripts/
│   └── dependency-scan.sh          # Monthly dependency-drift scanner
├── workflows/
│   ├── unit-tests.yml              # Unit Tests workflow
│   ├── integration-tests.yml       # Integration Tests workflow
│   ├── security.yml                # Security workflow
│   ├── lint.yml                    # Linting workflow
│   ├── mooncake-image.yml          # Mooncake vLLM image contract test (push/PR)
│   ├── release.yml                 # Manual workflow_dispatch release
│   ├── deps-scan.yml               # Monthly dependency scan
│   ├── cve-scan.yml                # Weekly CVE scan
│   └── pages.yml                   # Publish coverage report to GitHub Pages (workflow_run)
├── CODEOWNERS
├── dependabot.yml
├── pull_request_template.md
├── release.yml                     # GitHub Release notes categorization
└── CI.md                           # You are here (reference for everything in this folder)
```

## Workflows

### Primary (run on every push + PR)

Each file maps to one row in the README badge table.

| File | README row | What it covers |
|------|------------|----------------|
| `workflows/unit-tests.yml` | Unit Tests | pytest with coverage (fail under 90%), BATS, CLI smoke, CDK synth + config matrix, lockfile freshness, fresh install, MCP install + launch smoke, workload import checks |
| `workflows/integration-tests.yml` | Integration Tests | Per-Dockerfile build + module-import smoke, dev-container smoke, kind E2E with Calico (NetworkPolicy enforcement, RBAC verification, ResourceQuota/LimitRange, PDB validation, cross-namespace traffic blocking, all 3 service deployments), K8s manifest validation, Lambda import validation, cross-module pytest, MCP server pytest |
| `workflows/security.yml` | Security | bandit, pip-audit, trivy (filesystem + per-image matrix), trufflehog, gitleaks, semgrep, checkov, KICS, CodeQL (Python) |
| `workflows/lint.yml` | Linting | actionlint, hadolint, markdownlint, mypy (strict / stacks / lambda), ruff (format + check, imports included), shellcheck, yamllint |

### Satellites

Workflows outside the four badged gates. Most are schedule- or dispatch-driven; `mooncake-image.yml` also runs on push and PR but is a narrow, feature-specific contract test rather than a headline gate.

| File | Trigger | Purpose |
|------|---------|---------|
| `workflows/release.yml` | `workflow_dispatch` | Bump version, tag, create a GitHub Release with auto-generated notes. Uses the built-in `GITHUB_TOKEN` — no PAT required |
| `workflows/deps-scan.yml` | `cron: 0 9 1 * *` (monthly, UTC) + manual | Check Python / Docker / Helm / EKS-addon / Bedrock-model versions; open a GitHub issue if drift is found |
| `workflows/cve-scan.yml` | `cron: 0 9 * * 1` (Mondays, UTC) + manual | Re-run trivy against current CVE databases |
| `workflows/pages.yml` | `workflow_run` after **Unit Tests** completes on `main` | Download the `pytest-coverage` artifact from the triggering run, regenerate the shields.io coverage badge, and deploy `htmlcov/` to GitHub Pages via `actions/deploy-pages`. Split out of `unit-tests.yml` so a GitHub Pages backend stall surfaces here instead of failing the test gate |
| `workflows/mooncake-image.yml` | `push`: `main`, PR, manual | Pull the upstream Mooncake vLLM image pinned in `cli/images.py` (`_DISAGGREGATED_DEFAULT_IMAGE`) and run `tests/test_mooncake_image_contract.py`: prefill-decode proxy health under the image's `python3`, `MooncakeStoreConfig` acceptance of the rendered store config, and KV-connector name registration. Deliberately not Trivy/CVE-scanned — the image is upstream and unpatchable; version drift is surfaced by `deps-scan` |

### Naming conventions

- **Display names:** colon-delimited `category:tool:test_name`, for example `unit:pytest:core`, `security:trivy:container-scan`, `lint:mypy:stacks`.
- **Job IDs:** hyphen-delimited (GitHub Actions requires `[A-Za-z0-9_-]`), for example `unit-pytest-core`, `security-trivy-container-scan`.
- **Click target for every badge:** the workflow file on the Actions tab, not a per-job deep link. GitHub's per-job URL scheme is inconsistent; the Actions tab surfaces every job of a workflow in one view.

### Cross-cutting defaults

All CI workflows share the same safety defaults:

- `concurrency.group: ${{ github.workflow }}-${{ github.ref }}` with `cancel-in-progress: true` so rapid pushes on the same branch supersede in-flight runs. Explicitly **off** on `release.yml` — a half-run release is worse than a slow one. `pages.yml` is the other exception: it uses a dedicated `concurrency.group: pages` with `cancel-in-progress: false` so a real Pages deployment is never cancelled mid-flight.
- `timeout-minutes` on every job (10 min for lint, 15 for unit, 20–30 for integration).
- `permissions:` scoped narrowly. All CI workflows run with `contents: read`; `release.yml` upgrades to `contents: write` so the version-bump job can push a tag and create a GitHub Release. `pages.yml`'s deploy job grants `pages: write` + `id-token: write` (to publish to Pages) and `actions: read` (to pull the `pytest-coverage` artifact from the triggering Unit Tests run).
- Caching: `actions/setup-python@v6` with `cache: pip` and `cache-dependency-path: requirements-lock.txt`. Mypy jobs add an explicit `actions/cache@v5` on `.mypy_cache/`.
- AWS auth (when a future test needs it) uses OIDC via `aws-actions/configure-aws-credentials@v4` — not long-lived access keys.

## Composite actions

Shared logic used by multiple jobs. Invoked with `uses: ./.github/actions/<name>`.

- **`actions/build-lambda-package`** — stages `lambda/kubectl-applier-simple-build/` and `lambda/helm-installer-build/` that CDK synth, pytest, and KICS scans all expect. Used by `unit:cdk:synth`, `unit:cdk:config-matrix`, `unit:cdk:nag-compliance`, `unit:pytest:core`, and `security:kics:iac`.

## CodeQL config

[`codeql/codeql-config.yml`](codeql/codeql-config.yml) is read by the Advanced Setup CodeQL job (`security:codeql:python-code-analysis`) in [`workflows/security.yml`](workflows/security.yml), via the `config-file:` input on `github/codeql-action/init@v3`. It does three things:

- **Scopes the scan** to hand-authored Python runtime code (`gco/`, `cli/`, `gco_mcp/`, `lambda/`, `scripts/`). Generated output (`cdk.out/`, `lambda/*-build/`), virtualenvs, caches, tests, and the demo folder are excluded. `app.py` (the CDK app entry point) is not in scope — it's composition-only glue with no runtime/security surface.
- **Pins the query pack** to `security-and-quality` so the additional maintainability queries still surface alongside the default security suite.
- **Filters three rules** that have been reviewed and classified as false positives against this codebase: `py/clear-text-logging-sensitive-data` (we log operational identifiers like ARNs and registry hostnames, not credential values), `py/incomplete-url-substring-sanitization` (only ever hit by test-file assertions, not access-control code paths), and `py/weak-sensitive-data-hashing` (SRP protocol digest in `cli/analytics_user_mgmt.py`, not a password storage hash — RFC 5054 mandates SHA-256 for the protocol primitive). Each exclusion carries an inline comment in the config naming the exact call sites and the reason — audit them when the codebase shape changes.

The scan runs as an Advanced Setup workflow rather than Default Setup so the filters and paths are pinned in git instead of hidden in repo Settings. To swap back to Default Setup: comment out the `security-codeql-python-code-analysis` job in `workflows/security.yml` and re-enable Default Setup in repo Settings → Code security → CodeQL. The config file has no effect under Default Setup.

## README badges

The README's badge row has two parts:

1. **Four workflow-status badges** (`Unit Tests`, `Integration Tests`, `Security`, `Linting`) from GitHub's native `badge.svg` endpoint.
2. **Eight stack/tech badges** (Python, CDK, EKS Auto Mode, Kubernetes, CDK-Nag, etc.) rendered by shields.io from hardcoded values, each linking to the authoritative source (pyproject.toml, cdk.json, upstream docs, etc.).

There are no auto-generated test-count or coverage badges — those were removed before the first release because they depended on an orphan `badges` branch and a shields.io endpoint that didn't resolve reliably against a private repo. Room to add them back once the repo goes public; for now the workflow status itself carries the signal.

### "repo or workflow not found" on fresh or private repositories

The four workflow-status badges at the top of the README come from GitHub's native `badge.svg` endpoint and render a placeholder image when the repo is unreachable. All other shields.io URLs (`img.shields.io/badge/...`) are static and always render.

If a stale run ever shows a `img.shields.io/github/actions/workflow/status/...` URL rendering as **"repo or workflow not found"**, the usual cause is the repo being private (shields.io hits the public GitHub REST API and gets a 404). Making the repo public resolves it; there's no code change needed.

## Issue & PR templates

- `ISSUE_TEMPLATE/bug_report.md` — structured bug report with environment, repro steps, expected vs. actual.
- `ISSUE_TEMPLATE/feature_request.md` — problem/solution/alternatives framing.
- `ISSUE_TEMPLATE/config.yml` — links out to the docs (TROUBLESHOOTING.md, QUICKSTART.md) so users who arrive here with a support question are routed there first.
- `pull_request_template.md` — summary, type-of-change checkboxes (the leading token `feat:`, `fix:`, etc. is what `release.yml` uses to categorize auto-generated release notes), testing checklist.

## CODEOWNERS

[`CODEOWNERS`](CODEOWNERS) lists path-based review owners. Reviews are requested automatically when matched paths change. Make it mandatory by enabling "Require review from Code Owners" in branch protection.

## Dependabot

[`dependabot.yml`](dependabot.yml) covers **GitHub Actions and Docker only**, not Python.

Rationale: Python deps are pinned through `requirements-lock.txt` with `pip-compile` and reviewed intentionally; Dependabot would fight that workflow. CVE-driven Python bumps are caught by the weekly `cve-scan` workflow (Trivy) and the monthly `deps-scan` workflow.

Ecosystems tracked:

- GitHub Actions (`uses:` versions across all workflows)
- Docker (`dockerfiles/`, `lambda/helm-installer/`, `Dockerfile.dev` at repo root)

## Helper scripts

- **`scripts/dependency-scan.sh`** — backs the `deps-scan` workflow. See [below](#dependency-scan-script) for the full reference.
- **`scripts/check_pip_audit_ignore.py`** — backs the `security:pip-audit:deps` job. See [below](#pip-audit-ignore-validator) for the full reference.

### Dependency-scan script

`scripts/dependency-scan.sh` is the engine behind the monthly `deps-scan` workflow. It detects drift across every dependency surface the project controls and, when run from CI, writes a Markdown report that the workflow turns into a GitHub issue.

#### What it checks

| Surface | Source | Notes |
|---------|--------|-------|
| Python packages | `pip list --outdated` against the editable install of the current repo, filtered to packages we pin *directly* in `pyproject.toml` | Transitive-only drift is excluded because those versions are controlled by upstream pins (`jsii`, `aws-cdk-lib`, `botocore`, `fastmcp`, …) and bumping them ourselves either no-ops or breaks the resolver. The filter is driven by `extract_direct_python_deps` in `lib_dependency_scan.sh`. |
| Docker image tags | `image: …:<tag>` references in `.github/workflows/*.yml`, `lambda/kubectl-applier-simple/manifests/`, `examples/`, and `lambda/helm-installer/charts.yaml`, plus the Mooncake default image pinned as `_DISAGGREGATED_DEFAULT_IMAGE` in `cli/images.py` (via `extract_mooncake_default_image`) | Queries the original registry (Docker Hub, Quay, GHCR, GCR, ECR Public, registry.k8s.io) via `skopeo`; only semver tags. The Mooncake image is a Python constant — not a Dockerfile `FROM` or a manifest — so Dependabot doesn't see it; surfacing its drift here is the cue to validate and bump the pin (the `mooncake-image` workflow re-runs the image contract tests against the new tag). |
| Helm charts | `lambda/helm-installer/charts.yaml` | Uses `helm show chart` for OCI charts and `helm search repo` for traditional repos |
| EKS add-ons | `addon_name`/`addon_version` pairs extracted from `gco/stacks/constants.py` | Requires AWS credentials (via OIDC). The script pre-flights `sts get-caller-identity`; without valid creds the add-on section is explicitly **skipped** and the report notes why — everything else still runs |
| EKS Kubernetes version | `kubernetes_version` in `cdk.json` | Requires AWS credentials (via OIDC). Compares against the newest minor still in EKS **standard support** (`eks describe-cluster-versions`) and reports the standard-support end date so upgrade urgency is visible. See [Maintenance](../docs/MAINTENANCE.md#upgrading-the-eks-kubernetes-version) for the upgrade steps |
| Aurora PostgreSQL engine | `AURORA_POSTGRES_VERSION_DISPLAY` from `gco/stacks/constants.py` | Requires AWS credentials (via OIDC). Queries `rds describe-db-engine-versions` for the latest minor release within the same major line |
| EMR Serverless | `EMR_SERVERLESS_RELEASE_LABEL` from `gco/stacks/constants.py` | Requires AWS credentials (via OIDC). Lists release labels (`emr list-release-labels`) and reports a newer release in the same major line, or a new major line when one exists |
| Bedrock default model | `DEFAULT_BEDROCK_MODEL_ID` from `gco_mcp/mission/sampling.py` (mirrored by `cli/capacity/advisor.py`) | Requires AWS credentials (via OIDC). Lists system-defined inference profiles (`bedrock list-inference-profiles`, pinned to us-east-1) and reports drift when a newer release exists in the *same model family*. The id is a Python constant, so Dependabot never sees it |
| Dockerfile.dev pins | `ARG` pins in `Dockerfile.dev` (Node LTS major, npm, CDK CLI, kubectl, AWS CLI v2, Docker CLI, Buildx) | Public endpoints, no AWS creds. Each ARG resolves against its own upstream (`nodejs/Release`, the npm/CDK registries, `dl.k8s.io`, `aws/aws-cli` tags, `moby/moby`, `docker/buildx`) |
| Pre-commit hooks | `repo:` / `rev:` blocks in `.pre-commit-config.yaml` | Calls `GET /repos/{owner}/{repo}/tags` on GitHub for each hook and reports drift when our pinned `rev:` is older than the highest semver-shaped tag. Unauthenticated; SHA pins and non-GitHub repos are skipped silently |
| CDK enum constants | `LAMBDA_PYTHON_RUNTIME` and `AURORA_POSTGRES_VERSION` from `gco/stacks/constants.py` | Introspects the installed `aws-cdk-lib` (the `deps-scan` workflow installs the latest) for `aws_lambda.Runtime.PYTHON_X_Y` and `aws_rds.AuroraPostgresEngineVersion.VER_X_Y` and reports drift when our pinned enum is older than the highest member exposed by the library. Skipped with a note when `aws-cdk-lib` isn't importable |
| Python release | `LAMBDA_PYTHON_RUNTIME` (the major Python version we standardise on across Lambdas) | Queries `https://endoflife.date/api/python.json` for the highest currently-supported stable cycle and reports drift compared to the `LAMBDA_PYTHON_RUNTIME` constant. Public endpoint, no AWS creds |
| CI tooling | `TRIVY_VERSION` (`cve-scan.yml` / `security.yml`), `HELM_VERSION` and `KUBECTL_VERSION` (`deps-scan.yml`), and the kind binary + node image (`integration-tests.yml`) | Public endpoints, no AWS creds. Compares each hand-installed CI tool against its upstream (GitHub Releases for Trivy/Helm/kind, `dl.k8s.io` for kubectl, registry tags within the pinned minor for the kind node image). These are plain env / `with:` pins Dependabot doesn't watch — a stale Trivy silently weakens the CVE scan |
| Version consistency | ruff (pyproject / pre-commit / `lint.yml`), `python-version` across workflows vs the project runtime, and each `*_VERSION` env pin across workflow files | No network. Reports when copies of a pin that must move together disagree |
| Base-image security epochs | `APT_SECURITY_EPOCH` / `DNF_SECURITY_EPOCH` ARGs in `Dockerfile.dev`, `dockerfiles/*`, and `lambda/helm-installer/Dockerfile` | No network. Flags an epoch older than `SECURITY_EPOCH_STALE_DAYS` (default 45) so a stale cache-bust date masking new OS patches gets bumped |
| Suppression expiries | `exp:` markers in `.github/config/.trivyignore` and `.pip-audit-ignore` | No network. Surfaces entries expiring within `SUPPRESSION_EXPIRY_WARN_DAYS` (default 30) so they're renewed before the CI validator hard-fails a build on the expiry date |
| Lockfile freshness | `pyproject.toml` direct deps vs `requirements-lock.txt` | No network. Reports direct dependencies missing from the lock — the sign of a stale `pip-compile` |

Images matching `gco/*` are skipped (we build those). Non-semver tags (`latest`, branch names, SHAs) are ignored. Acting on a drift report is documented in the [Maintenance guide](../docs/MAINTENANCE.md).

#### Inputs

Set via environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `WORKFLOWS_DIR` | `.github/workflows` | Directory scanned for Docker image references in workflow files. Lets forks that vendor workflows elsewhere still use the script. |

#### Outputs

The script writes a Markdown report to a temp file and, when invoked from a workflow, emits two keys on `$GITHUB_OUTPUT` for the caller:

| Output | Value |
|--------|-------|
| `has_drift` | `true` when any scanned surface reported drift, else `false` |
| `report_path` | Path to the Markdown report (only set when `has_drift=true`) |

The report opens with a summary table (every surface, its status, and an urgency hint) linking to per-surface detail sections; skipped checks collapse into a single `<details>` block. When run in CI the script also mirrors the report — or an "up to date" line — into `$GITHUB_STEP_SUMMARY`, so results show on the workflow run page even when no issue is opened.

Exit code is `0` in both cases — drift is a signal, not a failure. When `has_drift=true` the `deps-scan` workflow opens **or refreshes** a single rolling GitHub issue labeled `dependencies, automated`; a stable, date-free title means the same issue is updated each month rather than a new one piling up. See the [Maintenance guide](../docs/MAINTENANCE.md) for how to act on a report.

#### Running it locally

```bash
# Requires: python3, pip, jq, skopeo, helm, kubectl, awscli
# (install or skip individual tools — the script handles missing awscli gracefully)

bash .github/scripts/dependency-scan.sh
```

The console output shows each surface's drift inline. To trigger the exact workflow path from GitHub, go to Actions → "Deps scan" → "Run workflow" and pick `main`.

#### Extending it

- **New Docker image source** — add a `grep … >> "$ALL_IMAGES"` block alongside the existing ones. Anything with a semver tag is picked up automatically.
- **New Helm chart** — nothing to change; the script walks every entry in `lambda/helm-installer/charts.yaml`.
- **New EKS add-on** — add the constant in `gco/stacks/constants.py` and reference it in `regional_stack.py`. The scanner imports from the constants module.
- **New Aurora engine version** — update `AURORA_POSTGRES_VERSION` and `AURORA_POSTGRES_VERSION_DISPLAY` in `gco/stacks/constants.py`.
- **New pre-commit hook** — nothing to change; `extract_precommit_hooks` walks every `repo:` block in `.pre-commit-config.yaml` and the GitHub-tags lookup picks up the hook automatically (as long as the upstream lives on GitHub and tags semver-shaped releases).
- **New CDK enum constant** — add the constant in `gco/stacks/constants.py`, then add a comparison block in `dependency-scan.sh`'s "Checking CDK enum constants" section that calls a new `get_latest_<name>` helper from `lib_dependency_scan.sh`. Pattern-match the existing `LAMBDA_PYTHON_RUNTIME` and `AURORA_POSTGRES_VERSION` blocks.
- **New default Bedrock model** — bump `DEFAULT_BEDROCK_MODEL_ID` in `gco_mcp/mission/sampling.py` and `BedrockCapacityAdvisor.DEFAULT_MODEL` in `cli/capacity/advisor.py` (kept identical by `tests/test_default_bedrock_model_consistency.py`); the "Checking Bedrock default model" section then tracks the new model family automatically. If the new model has no captured scaffold fixture yet, run `python scripts/capture_scaffold_fixtures.py --model <id>`.
- **New CI tool pin** — add a `check_github_tool <name> <pin> <owner/repo> <url>` call in the "Checking CI tooling pins" section (or a `dl.k8s.io` / registry lookup for non-GitHub tools), reading the current pin via `extract_workflow_env_pin` or `extract_kind_pins` from `lib_dependency_scan.sh`.
- **New consistency check** — add an extractor to `lib_dependency_scan.sh` and a comparison block in the "Checking version consistency" section that records disagreeing copies to `CONSISTENCY_RESULTS`.
- **New recurring-hygiene check** (suppression file, base-image epoch, lockfile, …) — add a parser to `lib_dependency_scan.sh` and a section that filters by the shared thresholds (`SUPPRESSION_EXPIRY_WARN_DAYS`, `SECURITY_EPOCH_STALE_DAYS`). Remember to wire the new `*_COUNT` into the summary, the all-zero `has_drift` gate, and both `rm -f` cleanup lines.

#### Failure modes & debugging

| Symptom | Likely cause |
|---------|--------------|
| `has_drift=false` but you expected drift | The latest-tag query returned empty (rate-limited Docker Hub, private registry). Run with `skopeo` directly to confirm |
| EKS add-on section explicitly skipped | No AWS credentials. Either expected (private repo without OIDC yet) or an OIDC misconfiguration. See [Enabling the EKS add-on check](#enabling-the-eks-add-on-check) |
| Helm chart resolution silently skipped | `helm repo add` failed. The script runs with `\|\| true` for these to avoid aborting on a single flaky repo; check the console log |

#### Enabling the EKS add-on check

The add-on-version section is the only surface that needs AWS credentials — there's no client-side catalog of supported EKS add-on versions (CDK doesn't ship one and neither does any public mirror; the authoritative answer only exists in the EKS API itself). Without creds the scan logs a one-line skip note and moves on, so the Python / Docker / Helm checks still report drift normally.

To turn the check on without introducing long-lived access keys, configure a GitHub OIDC trust to a read-only IAM role:

1. **Create the OIDC identity provider in the target AWS account** (one-time, skip if already present):

   ```text
   URL:      https://token.actions.githubusercontent.com
   Audience: sts.amazonaws.com
   Thumbprint: (auto-fetched by AWS; no manual step)
   ```

2. **Create a role** `GCODependencyScanRole` with a trust policy scoped to this repo's main branch:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Principal": { "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com" },
       "Action": "sts:AssumeRoleWithWebIdentity",
       "Condition": {
         "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
         "StringLike":   { "token.actions.githubusercontent.com:sub": "repo:awslabs/global-capacity-orchestrator-on-aws:ref:refs/heads/main" }
       }
     }]
   }
   ```

3. **Attach a least-privilege inline policy** listing only the read-only actions the scan needs. Keep this in sync with `.github/oidc_provider/policy.json` when you add new checks:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect":   "Allow",
       "Action": [
         "bedrock:ListFoundationModels",
         "bedrock:GetFoundationModel",
         "bedrock:ListInferenceProfiles",
         "bedrock:GetInferenceProfile",
         "eks:DescribeAddonVersions",
         "eks:DescribeClusterVersions",
         "elasticmapreduce:ListReleaseLabels",
         "rds:DescribeDBEngineVersions",
         "sts:GetCallerIdentity"
       ],
       "Resource": "*"
     }]
   }
   ```

4. **Add the OIDC step to `deps-scan.yml`** just above the "Run dependency scan" step:

   ```yaml
   permissions:
     id-token: write     # required to mint the OIDC JWT
     contents: read
     issues: write
   steps:
     # ...existing checkout + tooling install steps...
     - uses: aws-actions/configure-aws-credentials@v4
       with:
         role-to-assume: arn:aws:iam::<ACCOUNT_ID>:role/GCODependencyScanRole
         aws-region: us-east-1
     - name: Run dependency scan
       # ...
   ```

The script self-detects the credentials via `aws sts get-caller-identity`. No script changes are needed when you flip this on.

### pip-audit-ignore validator

`scripts/check_pip_audit_ignore.py` gates the `security:pip-audit:deps` job in `workflows/security.yml`. It validates the project-local `.pip-audit-ignore` file before pip-audit itself runs, so a stale CVE suppression can't quietly outlive its expiration date and hide a finding forever.

#### What it checks

Each non-comment, non-blank line in `.pip-audit-ignore` must:

- start with the vulnerability ID (e.g. `PYSEC-2025-183`, `CVE-2025-45768`, `GHSA-xxxx-xxxx-xxxx`); and
- carry an `exp:YYYY-MM-DD` marker somewhere on the same line.

The script fails the workflow when:

- any entry's `exp:` date is on-or-before today (inclusive — the listed date is itself expired, no bonus day); or
- any entry is missing the `exp:` marker entirely or has a malformed date (e.g. `exp:2026-13-40`).

Comment lines (`#…`) and blank lines are skipped. A missing `.pip-audit-ignore` file is treated as clean, not as an error — the suppression file is opt-in.

#### How it's wired

The `security:pip-audit:deps` job runs the validator as a dedicated step before the actual `pip-audit` invocation:

```yaml
- name: Validate .pip-audit-ignore expirations
  run: python3 .github/scripts/check_pip_audit_ignore.py .github/config/.pip-audit-ignore

- name: Run pip-audit
  # ... reads .pip-audit-ignore and converts each ID into --ignore-vuln <ID>
```

Splitting validation into its own step makes the failure surface clearly in the GitHub Actions UI when a suppression expires — the step name itself tells the operator what's wrong.

#### Running it locally

```bash
# Pass current date (default)
python3 .github/scripts/check_pip_audit_ignore.py .github/config/.pip-audit-ignore

# Pin "today" to a specific date — useful for previewing what will fail
# on or after that date
python3 .github/scripts/check_pip_audit_ignore.py .github/config/.pip-audit-ignore --today 2026-09-01
```

Exit codes: `0` (clean), `1` (one or more entries failed), `2` (argparse / I/O error).

#### Tests

Validator coverage lives in `tests/test_pip_audit_ignore_validator.py` (19 tests). It covers happy paths, expired-date detection (boundary tests for ±1 day and equal-to-today), missing or malformed markers, `main()` exit codes / stdout, and a live-file check that runs the committed `.pip-audit-ignore` against the validator with today's date. The live-file check is what catches drift between the suppression file and the validator's own rules.

#### Adding a suppression

Append a single line to `.pip-audit-ignore` with rationale and an expiration date:

```text
# CVE-2026-12345 — Brief one-line description.
#
# Why we're suppressing it (disputed, no upstream fix, not on a code path
# we exercise, etc.). Link to the upstream advisory and any tracking issue
# so the next reviewer can verify the rationale still holds.
#
# CVE record: https://www.cve.org/CVERecord?id=CVE-2026-12345
# OSV record: https://github.com/pypa/advisory-database/blob/main/vulns/<package>/PYSEC-XXXX-XXX.yaml
PYSEC-XXXX-XXX exp:2026-09-30
```

Pick an `exp:` date that gives upstream a reasonable window to ship a fix or have the advisory withdrawn (90 days is the typical default). When the date arrives, the validator step fails and forces a re-evaluation — extend with fresh rationale or remove the entry once the underlying CVE is fixed.

## Kind config

- **`kind/kind-calico.yaml`** — kind cluster config with `disableDefaultCNI: true` so Calico can be installed on top and actually enforce the `NetworkPolicy` resources from `lambda/kubectl-applier-simple/manifests/03-network-policies.yaml`. The default kindnet CNI does not enforce NetworkPolicy. Used exclusively by `integration:kind:cluster-e2e`.

## Markdownlint config

Configuration for the `lint:markdownlint:md` job lives in **`.github/config/.markdownlint-cli2.yaml`**. A single file covers three surfaces:

- The **GitHub Actions job** (`lint-markdownlint-md` in `workflows/lint.yml`) via `DavidAnson/markdownlint-cli2-action`.
- The **pre-commit hook** (`markdownlint-cli2` in `.pre-commit-config.yaml`).
- The **vscode-markdownlint** editor extension, which reads the same file so contributors see the same warnings as CI while they type.

The config does two things worth calling out:

1. **Rules** — starts from the markdownlint defaults and disables a few that fire a lot of aesthetic noise against this repo's style (`MD013` line-length, `MD033` inline HTML, `MD036` emphasis-as-heading, `MD041` first-line heading, `MD060` table column style). Every override is commented inline so future maintainers can audit the reason. `MD044` (proper-names) is intentionally left unconfigured: it does a case-insensitive substring match and mangles legitimate lowercase identifiers that share letters with product names (`cdk.json` becomes `cdk.JSON`, `kubernetes-sigs/karpenter` becomes `Kubernetes-sigs/...`, and so on).
2. **Globs** — the `globs` list targets `**/*.md`; the `ignores` list excludes `cdk.out/`, `build/`, `node_modules/`, Lambda build-staging directories, every tool cache, and `.kiro/` (IDE-local workspace content). `gitignore: true` additionally pulls in everything the repo's `.gitignore` already excludes.

To add a new exclusion (e.g. a generated-docs folder), extend the `ignores` list. To loosen or tighten a rule, adjust the `config:` block — see the [markdownlint rule reference](https://github.com/DavidAnson/markdownlint/blob/main/doc/Rules.md) for the full catalog.

## Running checks locally

Most jobs map to a single command you can run locally. Quick reference:

```bash
# Lint (matches jobs in workflows/lint.yml)
ruff format --check gco/ cli/ gco_mcp/ tests/ lambda/ scripts/ diagrams/
ruff check gco/ cli/ gco_mcp/ tests/ lambda/ scripts/ diagrams/
yamllint -c .github/config/.yamllint.yml --strict .
npx markdownlint-cli2 --config .github/config/.markdownlint-cli2.yaml

# Type check (matches lint:mypy:strict and lint:mypy:stacks)
mypy gco/ cli/ gco_mcp/ scripts/ --exclude 'gco/stacks/'
mypy gco/stacks/ app.py          # requires ".[cdk,typecheck]"

# Unit tests (matches unit:pytest:core)
pytest tests/ --cov=gco --cov=cli --cov=gco_mcp --cov-fail-under=90 \
    --ignore=tests/test_integration.py \
    --ignore=tests/test_nag_compliance.py

# cdk-nag compliance matrix (matches unit:cdk:nag-compliance)
pytest tests/test_nag_compliance.py -n auto

# CDK synth / config matrix (matches unit:cdk:synth and unit:cdk:config-matrix)
cdk synth --quiet
pytest tests/test_cdk_synthesis_matrix.py -n auto

# Security (matches security:bandit:sast)
bandit -r gco/ cli/ -c pyproject.toml --severity-level medium

# Validate workflow files (matches lint:actionlint:workflows)
actionlint
```

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for the full contributor setup and dependency management workflow.
