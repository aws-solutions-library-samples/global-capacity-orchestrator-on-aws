# Scripts

Utility scripts for development, testing, and operations.

## Table of Contents

- [Contents](#contents)
- [Usage](#usage)
  - [Setup Cluster Access](#setup-cluster-access)
  - [Setup Dev Alias](#setup-dev-alias)
  - [Bump Version](#bump-version)
  - [Test CDK Synthesis](#test-cdk-synthesis)
  - [Dump cdk-nag Findings](#dump-cdk-nag-findings)
  - [Test Webhook Delivery](#test-webhook-delivery)
  - [Capture Mission Scaffolder Fixtures](#capture-mission-scaffolder-fixtures)
  - [Maintain the Accelerator Catalog](#maintain-the-accelerator-catalog)
  - [MCP Install Smoke Test](#mcp-install-smoke-test)

## Contents

| Script | Description |
|--------|-------------|
| `accelerator_catalog.py` | Validates and refreshes the reviewed NVIDIA GPU/AWS Neuron catalog, NodePool families, capacity-history watch lists, and capacity pools. |
| `bump_version.py` | Bumps `VERSION` and every maintained version mirror used by packages and release documentation. |
| `capture_monitoring_screenshots.py` | Uses Playwright against a live Grafana/OpenCost port-forward to refresh monitoring screenshots under `images/`. |
| `capture_scaffold_fixtures.py` | Captures model output for the Mission scaffolder fixture-replay corpus. |
| `dump_nag_findings.py` | Runs the cdk-nag harness and prints findings grouped by rule and resource path. |
| `generate_openapi.py` | Writes or checks the committed OpenAPI documents for all four GCO HTTP services. |
| `mcp_install_smoke.py` | Verifies a packaged install exposes a self-contained, version-matched `gco-mcp` server and bundled `gco` CLI. |
| `migrate_fork.py` | Safely rewrites references owned by this repository when adopting GCO into a fork. See [`docs/FORKING.md`](../docs/FORKING.md). |
| `mkdocs_hooks.py` | Injects tracked `images/` assets into the MkDocs wiki build without duplicating binaries. |
| `preview_wiki.sh` | Runs the strict MkDocs build and optional local live-reload server used for wiki development. |
| `setup-cluster-access.sh` | Configures kubectl access to a GCO [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) cluster. |
| `setup-dev-alias.sh` | Builds the dev image and installs the shell function that runs `gco` through the containerized toolchain. |
| `split_tests.py` | Collects and deterministically balances the core pytest suite across CI shards. |
| `test_webhook_delivery.py` | Sends sample lifecycle events and verifies webhook delivery, signatures, and retries. |
| `example_job_validation/` | Static and authorized live validation harness for every shipped example manifest. |
| `live_release_validation/` | Checkpointed, explicitly authorized live deployment/recovery validation and sanitized reporting harness. |

> CI-only scripts live under [`.github/scripts/`](../.github/scripts/). In particular, [`.github/scripts/dependency-scan.sh`](../.github/scripts/dependency-scan.sh) powers the monthly `deps-scan` workflow and invokes `accelerator_catalog.py` for the offline and online accelerator maintenance tiers — see [`.github/CI.md`](../.github/CI.md#dependency-scan-script) for its full reference.

Each script has corresponding tests under `tests/` (Python) or `tests/BATS/` (shell). The matrix is documented in [`tests/README.md`](../tests/README.md) — add an entry there whenever you land a new script.

## Usage

### Setup Cluster Access

```bash
# Configure kubectl for a specific cluster and region
./scripts/setup-cluster-access.sh gco-us-east-1 us-east-1
```

Requires `PUBLIC_AND_PRIVATE` endpoint access mode in `cdk.json`. See [Customization Guide](../docs/CUSTOMIZATION.md#endpoint-access-modes) for details.

### Setup Dev Alias

```bash
# Build the gco-dev image and install the `gco` shell function
./scripts/setup-dev-alias.sh
source ~/.zshrc   # or ~/.bashrc — the script prints which file it updated

# Preview the generated function without building or writing anything
./scripts/setup-dev-alias.sh --print

# Reuse an existing image (skip the Dockerfile.dev build)
./scripts/setup-dev-alias.sh --no-build
```

Builds (or refreshes) the `gco-dev` image from `Dockerfile.dev`, then makes `gco` run inside the dev container against your current directory — no interactive session needed, and no separate build step. Re-running rebuilds the image so a stale one is refreshed automatically (`--no-build` skips it). Auto-detects Docker, Finch, or Podman (override with `--runtime`) and writes an idempotent block to your shell profile (`--rc` to target a specific file). This is the onboarding path recommended in the [main README](../README.md).

The build starts by resolving the `Dockerfile.dev` base image from Docker Hub, so a transient registry timeout there would otherwise fail an otherwise-healthy build. It is retried three times with a 15-second backoff; tune or disable that with `GCO_DEV_IMAGE_BUILD_ATTEMPTS` (set to `1` for no retries) and `GCO_DEV_IMAGE_BUILD_RETRY_DELAY`.

### Bump Version

```bash
python3 scripts/bump_version.py patch   # 1.0.0 → 1.0.1
python3 scripts/bump_version.py minor   # 1.0.0 → 1.1.0
python3 scripts/bump_version.py major   # 1.0.0 → 2.0.0
```

### Test CDK Synthesis

The [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) configuration matrix is exercised via pytest. Run it serially, as CI does, because concurrent in-process synths can race while staging shared CDK assets:

```bash
pytest tests/test_cdk_synthesis_matrix.py
```

### Dump cdk-nag Findings

Reach for this when the `unit:cdk:nag-compliance` CI job fails. It synthesizes every config in `tests/_cdk_config_matrix.py` with the full cdk-nag rule pack lineup attached and prints a compact, grouped summary of every unsuppressed finding. Exits 0 if clean, 1 otherwise.

```bash
python3 scripts/dump_nag_findings.py
```

Once you've scoped the relevant `acknowledge_nag_findings` entries, re-run to verify, then run the pytest gate to confirm:

```bash
pytest tests/test_nag_compliance.py -q
```

### Test Webhook Delivery

```bash
python3 scripts/test_webhook_delivery.py
```

### Capture Mission Scaffolder Fixtures

Captures raw Bedrock model output for the Mission scaffolder prompt across a curated cross-family model set (Anthropic Claude, [Amazon Nova](https://docs.aws.amazon.com/nova/latest/userguide/what-is-nova.html), Meta Llama, Mistral, DeepSeek). Each captured response is checked into `tests/fixtures/scaffold_responses/` and replayed by `tests/test_scaffold_fixture_replay.py` on every CI run, so a regression that breaks one model is caught against every captured model on the next push.

```bash
# Capture every default model against the canonical directive set
# (writes one JSON file per model). Per-model failures (denied
# access, transient errors) are reported and never abort the run.
python3 scripts/capture_scaffold_fixtures.py

# Capture the canonical global Claude Opus 5 fixture. This makes three
# sequential paid Converse calls and applies the stock high reasoning effort.
python3 scripts/capture_scaffold_fixtures.py \
  --model global.anthropic.claude-opus-5 \
  --region us-east-1

# Capture a different single model.
python3 scripts/capture_scaffold_fixtures.py \
  --model us.anthropic.claude-haiku-4-5-20251001-v1:0

# Use a different region.
python3 scripts/capture_scaffold_fixtures.py --region us-west-2
```

Requires AWS credentials with `bedrock:InvokeModel` access to the listed
models, plus the one-time
[Anthropic first-time-use form](../docs/CUSTOMIZATION.md#accepting-the-anthropic-first-time-use-form)
for Anthropic models such as the stock default. The configured default also
consumes `cdk.json` `context.bedrock.generation_reasoning`; the stock `high` effort can
materially increase billed output tokens and latency, and omits
`temperature`, `topP`, and `topK`, which Claude no longer supports. Schedule capture as a
quarterly canary if you want fresh data; otherwise the existing fixtures
continue to protect the validator surface. The full lifecycle (adding a new
model, what to do when the replay test fires red) is documented in
[`tests/fixtures/scaffold_responses/README.md`](../tests/fixtures/scaffold_responses/README.md).

### Maintain the Accelerator Catalog

The authoritative accelerator inventory and reviewed family policy live in
`gco/config/accelerator_catalog.json`. Normal development and CI use the offline
command, which needs no AWS credentials:

```bash
python scripts/accelerator_catalog.py validate
python -m pytest tests/test_accelerator_catalog.py -q
```

The monthly dependency workflow adds live EC2 discovery. Maintainers can run the
same online paths manually:

```bash
# Print the discovered enabled-Region NVIDIA GPU / AWS Neuron union
python scripts/accelerator_catalog.py capture

# Compare it with the checked-in catalog (0=current, 1=drift, 2=tool failure)
python scripts/accelerator_catalog.py check-online --json-summary

# After reviewing family lifecycle/generation/architecture policy, stage a refresh
python scripts/accelerator_catalog.py refresh \
  --output /tmp/accelerator_catalog.json
```

`refresh` refuses an unreviewed family or EC2 metadata that conflicts with
checked-in policy. It updates the catalog type list and embeds a UTC
`last_refreshed_at` timestamp; read-only commands never rewrite that timestamp.
The maintainer must review NodePool scheduling eligibility and synchronize `cdk.json`
`historical.watch_instance_types` plus the `ConfigLoader` fallback. Follow the
complete [maintenance runbook](../docs/MAINTENANCE.md#adding-a-new-instance-type-or-family).

### MCP Install Smoke Test

Verifies that a packaged install of GCO (via `uv` or `pip`) exposes a working, self-contained MCP server. These are the same checks the `unit:mcp:install` CI job runs. Invoke it with the target environment interpreter so it exercises the installed package, not the working tree:

```bash
# After installing into an isolated environment, e.g.:
# uv venv /tmp/gco && uv pip install --python /tmp/gco .
/tmp/gco/bin/python scripts/mcp_install_smoke.py
```

It asserts the package imports from site-packages, the PyPI `mcp` SDK is not shadowed by the in-tree `gco_mcp` package, `gco_mcp.run_mcp.main` is callable, and the server resolves its own bundled, version-matched `gco` CLI. It exits non-zero with the failing checks listed if any invariant breaks.
