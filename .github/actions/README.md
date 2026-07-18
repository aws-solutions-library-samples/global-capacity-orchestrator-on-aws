# Composite Actions

Reusable GitHub Actions composite actions shared across multiple CI workflows. Invoked with `uses: ./.github/actions/<name>`.

## Table of Contents

- [Actions](#actions)
  - [`build-lambda-package`](#build-lambda-package)
  - [`free-disk-space`](#free-disk-space)
  - [`install-trivy`](#install-trivy)
  - [`upload-artifact-with-retry`](#upload-artifact-with-retry)
- [Adding a New Action](#adding-a-new-action)

## Actions

### `build-lambda-package`

Stages all three generated Lambda assets through the build-only
`prepare_cdk_assets()` entry point. CDK synthesis callers use
`cdk_asset_consumer()` to retain shared locks through app construction and
synthesis. Each asset uses an interprocess lock, a unique staging tree, a
source/full-build completion manifest, and rollback-safe rename publication:

- `lambda/kubectl-applier-simple-build/` — exact Python requirements, handler, and manifests
- `lambda/inference-streaming-proxy-build/` — Node.js 24 handler and production AWS SDK clients from the committed lockfile, with lifecycle scripts disabled
- `lambda/helm-installer-build/` — complete deployable helm-installer Docker context

**Used by:** `unit:cdk:synth`, `unit:cdk:config-matrix`, `unit:cdk:nag-compliance`, `unit:pytest:core`, `security:kics:iac`

**Prerequisite:** The calling job must set up Python (via
`actions/setup-python`), install this project's Python package, and set up
Node.js from `.nvmrc` (via `actions/setup-node`) before invoking this action.

**Usage:**

```yaml
steps:
  - uses: actions/checkout@v6
  - uses: actions/setup-node@v6.4.0
    with:
      node-version-file: ".nvmrc"
  - uses: actions/setup-python@v6
    with:
      python-version: "3.14"
  - run: pip install -e ".[cdk]"
  - uses: ./.github/actions/build-lambda-package
```

### `free-disk-space`

Reclaims disk on GitHub-hosted ubuntu runners before disk-heavy jobs. The default runner image ships with ~14 GB free on the root volume, and a full pip install (CDK + dev + mcp) plus the Lambda build trees, node, and the per-xdist-worker coverage SQLite files can exhaust it. When the disk fills mid-run, coverage.py's `.coverage` flush raises `sqlite3.OperationalError: database or disk is full`, which xdist surfaces as a pytest `INTERNALERROR` / `KeyError: <WorkerController gwN>` — failing the whole job with a false negative.

This action removes large preinstalled toolchains the build never uses (Android SDK, .NET, Haskell/GHC, Swift, PowerShell, Chromium) and prunes cached Docker images, typically reclaiming 20–30 GB. It prints `df -h /` before and after so the reclaimed space is visible in the job log.

**Safe by design:**

- Leaves `/opt/hostedtoolcache` untouched — `actions/setup-python` serves the job's interpreter from there (e.g. `/opt/hostedtoolcache/Python/3.14.x`), so removing it would break the run.
- Every removal is best-effort (`|| true`), so a missing directory on a future runner-image revision can never fail the job.

**Used by:** `unit:pytest:core`, `unit:cdk:config-matrix`, `unit:cdk:nag-compliance`. Add it as the first step after `actions/checkout` (before the Python/Node setup and install steps) so the headroom exists for the whole job.

**Usage:**

```yaml
steps:
  - uses: actions/checkout@v6
  - uses: ./.github/actions/free-disk-space
  - uses: actions/setup-python@v6
    with:
      python-version: "3.14"
```

### `install-trivy`

Installs a pinned Trivy binary by wrapping the official `aquasecurity/setup-trivy` installer. A thin shim so workflows reference one place and the installer version + pin live here, not duplicated across `security.yml` and `cve-scan.yml`.

**Why wrap `setup-trivy` instead of `curl ... contrib/install.sh | sh`:**

- **Supply chain** — `setup-trivy` SHA-pins the `aquasecurity/trivy` ref it pulls `install.sh` from, so the install script is itself pinned. A raw `curl | sh` from the upstream `main` branch runs whatever is there at runtime, unpinned.
- **Reliability** — the previous raw-curl step intermittently failed when the `raw.githubusercontent.com` fetch was throttled or dropped mid-download (a hard exit 1 right after the version resolved). `setup-trivy` installs via a pinned `actions/checkout` + release download and caches the binary, so most runs restore it without touching that path.

**Why `setup-trivy` (installer), not `trivy-action` (scanner):** our jobs run `trivy fs` / `trivy image` directly with a two-pass JSON+table pattern and flags (`--file-patterns`, dual `--exit-code`, `--skip-version-check`) that don't map onto `trivy-action`'s single-run model. This action only puts the pinned binary on `PATH`; the run steps stay in the workflows.

**Inputs:**

| Name | Default | Description |
|------|---------|-------------|
| `version` | (required) | Trivy version tag (e.g. `v0.70.0`). Pin in lockstep across all callers. |
| `github-token` | `""` | Token forwarded to `setup-trivy` for the install-script checkout (authenticated API limit vs anonymous). Pass `${{ github.token }}`. |

**Used by:** `security:trivy:filesystem`, `security:trivy:container-scan` (`security.yml`), and `cve-scan.yml`. Keep `TRIVY_VERSION` identical across all three. `setup-trivy` is pinned to its `v0.2.6` release tag in `action.yml`; bump that tag there after reviewing a newer release.

**Usage:**

```yaml
- uses: actions/checkout@v6
- name: Install pinned Trivy
  uses: ./.github/actions/install-trivy
  with:
    version: "${{ env.TRIVY_VERSION }}"
    github-token: "${{ github.token }}"
- run: trivy fs --severity HIGH,CRITICAL .
```

### `upload-artifact-with-retry`

Wraps `actions/upload-artifact@v7.0.1` with an inline retry loop. The GitHub Actions artifact backend occasionally returns 5xx or 403s during the finalize step (after the bytes are fully uploaded), and the default upload action surfaces those as a hard job failure. This composite retries the upload up to 3 times with a 30 s / 60 s backoff so a flaky finalize doesn't fail an otherwise-green CI run.

Behaviour matches `actions/upload-artifact@v7.0.1` for every successful path; the only observable difference is on transient failures.

**Inputs (passed straight through to `upload-artifact`):**

| Name | Default | Description |
|------|---------|-------------|
| `name` | (required) | Artifact name |
| `path` | (required) | File or directory path(s) to upload |
| `retention-days` | `""` | Retention period in days |
| `if-no-files-found` | `warn` | Behaviour when no matching files exist (`warn` / `error` / `ignore`) |
| `overwrite` | `false` | Whether to overwrite an existing artifact with the same name |
| `include-hidden-files` | `false` | Whether to include hidden files in the upload |

**Used by:** every workflow that uploads artifacts — `unit-tests.yml`, `integration-tests.yml`, `security.yml`, `cve-scan.yml`. Drop-in replacement for `actions/upload-artifact@v7.0.1`.

**Usage:**

```yaml
- name: Upload coverage artifacts
  if: always()
  uses: ./.github/actions/upload-artifact-with-retry
  with:
    name: pytest-coverage
    path: |
      htmlcov/
      coverage.xml
    retention-days: 7
```

## Adding a New Action

1. Create a new directory under `.github/actions/` (e.g. `my-action/`)
2. Add an `action.yml` with `runs: using: "composite"` and your steps
3. Reference it in workflows with `uses: ./.github/actions/my-action`
4. Document it under [Actions](#actions) above
5. Add an anchor link in the [Table of Contents](#table-of-contents) so the action is discoverable from the top of the file
