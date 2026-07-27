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
| `setup-cluster-access.sh` | Configures kubectl access to a GCO [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) cluster. Adds your [IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction.html) principal to the cluster's access entries and verifies connectivity. |
| `setup-dev-alias.sh` | Builds (or refreshes) the `gco-dev` image from `Dockerfile.dev`, then installs a `gco` shell function (between `# >>> gco >>>` markers) into your shell rc file so the CLI runs inside the dev container against your current directory. Auto-detects the container runtime (Docker/Finch/Podman), picks the matching socket mount, and is safe to re-run (`--no-build` skips the rebuild). |
| `bump_version.py` | Bumps the project version across all locations (pyproject.toml, CLI, docs). Supports major, minor, and patch increments. |
| `accelerator_catalog.py` | Validates the checked-in NVIDIA GPU/AWS Neuron catalog against Karpenter NodePools and both capacity-history watch lists; can capture, compare, and safely refresh the catalog from enabled-Region [EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/concepts.html) discovery. Successful refreshes embed a UTC `last_refreshed_at` timestamp. Offline `validate` is deterministic; online commands are sequential, paginated, and use adaptive retries. |
| `dump_nag_findings.py` | Dev-only debugging helper: runs the `tests/test_nag_compliance.py` harness and prints every cdk-nag finding grouped by rule + resource path + config. Use this when the compliance test gate fails in CI and you want a compact per-finding view instead of pytest's `AssertionError` repr. |
| `test_webhook_delivery.py` | Tests the webhook dispatcher by sending sample events and verifying delivery, HMAC signatures, and retry behavior. |
| `capture_scaffold_fixtures.py` | Captures raw model output for the Mission scaffolder prompt across a curated cross-family [Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html) model set. Writes one JSON file per model under `tests/fixtures/scaffold_responses/` for the replay test (`tests/test_scaffold_fixture_replay.py`) to drive on every CI run. See the [fixture-replay runbook](../tests/fixtures/scaffold_responses/README.md) for the full lifecycle. |
| `mcp_install_smoke.py` | Smoke-tests that a packaged GCO install exposes a working, self-contained `gco-mcp` server: the package imports from site-packages (not a checkout), the PyPI `mcp` SDK is not shadowed by the in-tree `gco_mcp` package, `main()` is callable, and the server resolves its own bundled `gco` CLI. Drives the `unit:mcp:install` CI job. |

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

### Bump Version

```bash
python3 scripts/bump_version.py patch   # 1.0.0 → 1.0.1
python3 scripts/bump_version.py minor   # 1.0.0 → 1.1.0
python3 scripts/bump_version.py major   # 1.0.0 → 2.0.0
```

### Test CDK Synthesis

The [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) configuration matrix is now exercised via pytest (runs in parallel under
`pytest-xdist`). Invoke it the same way CI does:

```bash
pytest tests/test_cdk_synthesis_matrix.py -n auto
```

### Dump cdk-nag Findings

Reach for this when the `unit:cdk:nag-compliance` CI job fails. It synthesizes every config in `tests/_cdk_config_matrix.py` with the full cdk-nag rule pack lineup attached and prints a compact, grouped summary of every unsuppressed finding. Exits 0 if clean, 1 otherwise.

```bash
python3 scripts/dump_nag_findings.py
```

Once you've scoped the relevant `acknowledge_nag_findings` entries, re-run to verify, then run the pytest gate to confirm:

```bash
pytest tests/test_nag_compliance.py -n auto -q
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
consumes `cdk.json` `context.bedrock.thinking`; the stock `high` effort can
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
