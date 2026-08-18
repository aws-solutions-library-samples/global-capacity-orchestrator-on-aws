# Composite Actions

Reusable GitHub Actions composite actions shared across multiple CI workflows. Invoked with `uses: ./.github/actions/<name>`.

## Table of Contents

- [Actions](#actions)
  - [`apt-install-with-retry`](#apt-install-with-retry)
  - [`build-image-with-retry`](#build-image-with-retry)
  - [`build-lambda-package`](#build-lambda-package)
  - [`docker-build-with-retry`](#docker-build-with-retry)
  - [`docker-pull-with-retry`](#docker-pull-with-retry)
  - [`free-disk-space`](#free-disk-space)
  - [`install-trivy`](#install-trivy)
  - [`setup-buildx-with-retry`](#setup-buildx-with-retry)
  - [`upload-artifact-with-retry`](#upload-artifact-with-retry)
- [Adding a New Action](#adding-a-new-action)

## Actions

### `apt-install-with-retry`

Runs `apt-get update` + `apt-get install` with ordered fallback mirrors, bounded network timeouts, and a retry loop. GitHub-hosted runners resolve `archive.ubuntu.com` through an ordered mirror list (`/etc/apt/apt-mirrors.txt`); when the first mirror (`azure.archive.ubuntu.com`) is unreachable-but-hanging, stock apt waits out a timeout for **every** index fetch before falling back — observed in CI as ten-plus minutes of `Ign:` lines on a plain `sudo apt-get update` while the job's actual tests never started. Bounded timeouts alone don't fix that mode: 15 s × 3 retries × ~20 index files still serializes into minutes per attempt.

Three layers, none of which mask real failures: each configured mirror is health-probed once with a 5-second cap and the reachable ones are written to the runner's mirror list in order (apt then falls back across healthy mirrors natively, per fetch; if nothing answers, the list is left untouched); every fetch is capped (`Acquire::http::Timeout=15`, `Acquire::Retries=3`) so a mirror that degrades mid-run costs seconds; and the update+install pair retries as one unit with backoff, because a failed install often means a stale index. A genuinely missing package or dependency conflict still fails on the final attempt with the real apt error. Steps that add a third-party APT repo keep that setup in their own step — the mirror list only governs the Ubuntu archive, and other sources are never touched.

**Inputs:**

| Name | Default | Description |
|------|---------|-------------|
| `packages` | (required) | Whitespace- or newline-separated package names to install. |
| `mirrors` | Azure mirror, then `archive.ubuntu.com` | Ordered Ubuntu archive mirror base URLs. First healthy mirror is primary; later entries are apt's native per-fetch fallbacks. Add regional mirrors here for more fallback depth. |
| `no-install-recommends` | `false` | Pass `"true"` to install with `--no-install-recommends`. |
| `attempts` | `3` | Total update+install attempts. Set to `1` to disable retries. |
| `delay` | `15` | Seconds to sleep between attempts. |

**Used by:** `unit-tests.yml` (`unit:bats:shell`), `deps-scan.yml` (system tooling), and `integration-tests.yml` (`integration:dev-alias:podman`, `integration:dev-alias:finch`) — every job that installs Ubuntu packages on a hosted runner. Steps that add a third-party APT repo first (the Finch job) keep that setup in their own step; this action's `update` then indexes the new source before installing from it.

**Usage:**

```yaml
- name: Install bats + deps (with retry)
  uses: ./.github/actions/apt-install-with-retry
  with:
    packages: bats jq python3-yaml
```

### `build-image-with-retry`

Wraps `docker/build-push-action@v7.3.0` with retry-on-failure. Every build starts by resolving base-image metadata against Docker Hub, whose registry and token endpoints intermittently time out on GitHub runners (`failed to resolve source metadata for docker.io/library/python:3.14.6-slim ... dial tcp ...:443: i/o timeout`) — a network fault that failed an integration job before a single layer was built. The build-push action has no retry of its own, so one blip fails the job.

Retrying the whole action keeps the GHA layer-cache semantics identical on every attempt, and layers completed by a failed attempt are reused by the next. Genuine build failures are deterministic and still fail on the final attempt with the real error. Behaviour matches `docker/build-push-action@v7.3.0` on every successful path. Three attempts with a 15 s / 45 s backoff, matching [`setup-buildx-with-retry`](#setup-buildx-with-retry) (typically paired immediately before this action).

**Inputs (passed straight through to `build-push-action`):**

| Name | Default | Description |
|------|---------|-------------|
| `context` | `.` | Build context |
| `file` | (required) | Dockerfile path |
| `tags` | (required) | Image tag(s) |
| `load` | `true` | Load the result into the local image store |
| `cache-from` | `""` | Cache sources |
| `cache-to` | `""` | Cache destinations |

**Used by:** every `integration:docker:*` image job and the kind E2E builds in `integration-tests.yml`, and `security:trivy:container-scan` in `security.yml`. Drop-in replacement for `docker/build-push-action@v7.3.0` for the input surface above.

**Usage:**

```yaml
- uses: ./.github/actions/setup-buildx-with-retry
- name: Build image
  uses: ./.github/actions/build-image-with-retry
  with:
    context: .
    file: dockerfiles/cost-monitor-dockerfile
    tags: cost-monitor:ci
    load: true
    cache-from: type=gha,scope=cost-monitor
    cache-to: type=gha,mode=max,scope=cost-monitor,ignore-error=true
```

### `build-lambda-package`

Stages all three generated [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) assets through the build-only
`prepare_cdk_assets()` entry point. [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) synthesis callers use
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
  - uses: actions/checkout@v7
  - uses: actions/setup-node@v7.0.0
    with:
      node-version-file: ".nvmrc"
  - uses: actions/setup-python@v7.0.0
    with:
      python-version: "3.14"
  - run: pip install -e ".[cdk]"
  - uses: ./.github/actions/build-lambda-package
```

### `docker-build-with-retry`

Runs a plain `docker build` (the daemon's default builder — no Buildx) with a retry loop, for jobs that deliberately want the image built exactly as a contributor's `docker build` would produce it. Same failure class as [`build-image-with-retry`](#build-image-with-retry): Docker Hub registry timeouts while resolving base-image metadata. Layers completed by a failed attempt stay in the daemon cache, so retries resume rather than rebuild. Genuine build failures still fail on the final attempt with the real error.

**Inputs:**

| Name | Default | Description |
|------|---------|-------------|
| `dockerfile` | `""` | Dockerfile path passed to `-f` (empty = the context default) |
| `tag` | (required) | Image tag passed to `-t` |
| `context` | `.` | Build context directory |
| `attempts` | `3` | Total attempts. Set to `1` to disable retries. |
| `delay` | `15` | Seconds between attempts, doubled after each failure |

**Used by:** `integration:docker:dev-container` (`integration-tests.yml`) — the native-arch matrix that intentionally avoids Buildx and the GHA cache.

**Usage:**

```yaml
- name: Build dev container
  uses: ./.github/actions/docker-build-with-retry
  with:
    dockerfile: Dockerfile.dev
    tag: gco-dev
```

### `docker-pull-with-retry`

Pulls one or more pinned container images with a retry loop, so a following `docker run` finds them in the local cache. Docker Hub's registry and token endpoints intermittently time out on GitHub runners (`auth.docker.io ... Client.Timeout exceeded while awaiting headers`), and because a plain `docker run` pulls implicitly with no retry, a single blip failed a lint job whose checks never ran. Retrying with backoff makes that class self-healing; a genuinely missing or unauthorised image still fails on the final attempt with the real registry error.

**Inputs:**

| Name | Default | Description |
|------|---------|-------------|
| `images` | (required) | Whitespace- or newline-separated image references. Always pin a tag or digest. |
| `attempts` | `3` | Total attempts per image. Set to `1` to disable retries. |
| `delay` | `15` | Seconds to sleep between attempts. |

**Used by:** `lint.yml` (`lint:hadolint:dockerfile`, `lint:shellcheck:shell`), `security.yml` (`security:trufflehog:secrets`), `integration-tests.yml` (`integration:docker:dev-container` — DinD probe base image), and `mooncake-image.yml` — every job that runs or builds from a Docker Hub image it did not itself build.

**Usage:**

```yaml
- name: Pre-pull shellcheck image
  uses: ./.github/actions/docker-pull-with-retry
  with:
    images: koalaman/shellcheck-alpine:v0.11.0
- name: Run shellcheck
  run: docker run --rm -v "$PWD":/repo -w /repo koalaman/shellcheck-alpine:v0.11.0 ...
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
  - uses: actions/checkout@v7
  - uses: ./.github/actions/free-disk-space
  - uses: actions/setup-python@v7.0.0
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
| `version` | `v0.73.0` | Trivy version tag. **The default is THE Trivy pin for this repository** — callers pass no version, so bumping the default bumps every workflow at once. Tracked for drift by dependency-scan.sh via `extract_install_trivy_pin`. |
| `github-token` | `""` | Token forwarded to `setup-trivy` for the install-script checkout (authenticated API limit vs anonymous). Pass `${{ github.token }}`. |

**Used by:** `security:trivy:filesystem`, `security:trivy:container-scan` (`security.yml`), and `cve-scan.yml` — all inherit the pin from the `version` default. `setup-trivy` is pinned to its `v0.2.6` release tag in `action.yml`; bump that tag there after reviewing a newer release.

**Usage:**

```yaml
- uses: actions/checkout@v7
- name: Install pinned Trivy
  uses: ./.github/actions/install-trivy
  with:
    github-token: "${{ github.token }}"
- run: trivy fs --severity HIGH,CRITICAL .
```

### `setup-buildx-with-retry`

Wraps `docker/setup-buildx-action@v4.2.0` with retry-on-failure. Creating a `docker-container` builder pulls the BuildKit image from Docker Hub, so the same token-endpoint timeouts described above can fail a job before anything is built — one such blip failed a Trivy container scan before any image existed to scan. The action has no retry of its own. Retrying the whole action rather than pre-pulling a hard-coded BuildKit tag keeps this correct if the pinned buildx version changes the image it resolves.

Behaviour matches `docker/setup-buildx-action@v4.2.0` for every successful path; the only observable difference is on transient failures. Three attempts with a 15 s / 45 s backoff.

**Inputs:**

| Name | Default | Description |
|------|---------|-------------|
| `driver-opts` | `""` | Builder driver options, passed straight through. |

**Used by:** `security.yml` (`security:trivy:container-scan`) and every `integration:docker:*` / kind E2E job in `integration-tests.yml`. Drop-in replacement for `docker/setup-buildx-action@v4.2.0`.

**Usage:**

```yaml
- uses: ./.github/actions/setup-buildx-with-retry
- uses: ./.github/actions/build-image-with-retry
  with:
    file: dockerfiles/my-image-dockerfile
    tags: my-image:ci
    cache-from: type=gha,scope=my-image
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
