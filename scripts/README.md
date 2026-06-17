# Scripts

Utility scripts for development, testing, and operations.

## Table of Contents

- [Contents](#contents)
- [Usage](#usage)
  - [Setup Cluster Access](#setup-cluster-access)
  - [Bump Version](#bump-version)
  - [Test CDK Synthesis](#test-cdk-synthesis)
  - [Dump cdk-nag Findings](#dump-cdk-nag-findings)
  - [Test Webhook Delivery](#test-webhook-delivery)
  - [Capture Mission Scaffolder Fixtures](#capture-mission-scaffolder-fixtures)
  - [Mirror Images](#mirror-images)

## Contents

| Script | Description |
|--------|-------------|
| `setup-cluster-access.sh` | Configures kubectl access to a GCO EKS cluster. Adds your IAM principal to the cluster's access entries and verifies connectivity. |
| `bump_version.py` | Bumps the project version across all locations (pyproject.toml, CLI, docs). Supports major, minor, and patch increments. |
| `dump_nag_findings.py` | Dev-only debugging helper: runs the `tests/test_nag_compliance.py` harness and prints every cdk-nag finding grouped by rule + resource path + config. Use this when the compliance test gate fails in CI and you want a compact per-finding view instead of pytest's `AssertionError` repr. |
| `test_webhook_delivery.py` | Tests the webhook dispatcher by sending sample events and verifying delivery, HMAC signatures, and retry behavior. |
| `capture_scaffold_fixtures.py` | Captures raw model output for the Mission scaffolder prompt across a curated cross-family Bedrock model set. Writes one JSON file per model under `tests/fixtures/scaffold_responses/` for the replay test (`tests/test_scaffold_fixture_replay.py`) to drive on every CI run. See the [fixture-replay runbook](../tests/fixtures/scaffold_responses/README.md) for the full lifecycle. |
| `mirror_images.py` | Mirrors the project's third-party images (currently Volcano's docker.io `volcanosh/vc-*`) into the project's `gco/*` ECR, preserving the full multi-arch manifest list (Docker Buildx / Finch `--all-platforms` / skopeo, whichever the runtime supports), so the cluster pulls them from same-account ECR instead of rate-limited Docker Hub. Thin wrapper over `cli/_image_mirror.py` (the same core `gco stacks deploy` runs automatically). Pairs with the `volcano_image_mirror` cdk.json toggle. See [Customization Guide](../docs/CUSTOMIZATION.md#get-volcanos-dockerio-images-off-the-rate-limited-path-ecr-mirror). |

> CI-only scripts live under [`.github/scripts/`](../.github/scripts/). In particular, [`.github/scripts/dependency-scan.sh`](../.github/scripts/dependency-scan.sh) powers the monthly `deps-scan` workflow — see [`.github/CI.md`](../.github/CI.md#dependency-scan-script) for its full reference.

Each script has corresponding tests under `tests/` (Python) or `tests/BATS/` (shell). The matrix is documented in [`tests/README.md`](../tests/README.md) — add an entry there whenever you land a new script.

## Usage

### Setup Cluster Access

```bash
# Configure kubectl for a specific cluster and region
./scripts/setup-cluster-access.sh gco-us-east-1 us-east-1
```

Requires `PUBLIC_AND_PRIVATE` endpoint access mode in `cdk.json`. See [Customization Guide](../docs/CUSTOMIZATION.md#endpoint-access-modes) for details.

### Bump Version

```bash
python3 scripts/bump_version.py patch   # 1.0.0 → 1.0.1
python3 scripts/bump_version.py minor   # 1.0.0 → 1.1.0
python3 scripts/bump_version.py major   # 1.0.0 → 2.0.0
```

### Test CDK Synthesis

The CDK configuration matrix is now exercised via pytest (runs in parallel under
`pytest-xdist`). Invoke it the same way CI does:

```bash
pytest tests/test_cdk_synthesis_matrix.py -n auto
```

### Dump cdk-nag Findings

Reach for this when the `unit:cdk:nag-compliance` CI job fails. It synthesizes every config in `tests/_cdk_config_matrix.py` with the full cdk-nag rule pack lineup attached and prints a compact, grouped summary of every unsuppressed finding. Exits 0 if clean, 1 otherwise.

```bash
python3 scripts/dump_nag_findings.py
```

Once you've scoped the relevant `NagSuppressions` entries, re-run to verify, then run the pytest gate to confirm:

```bash
pytest tests/test_nag_compliance.py -n auto -q
```

### Test Webhook Delivery

```bash
python3 scripts/test_webhook_delivery.py
```

### Capture Mission Scaffolder Fixtures

Captures raw Bedrock model output for the Mission scaffolder prompt across a curated cross-family model set (Anthropic Claude, Amazon Nova, Meta Llama, Mistral, DeepSeek). Each captured response is checked into `tests/fixtures/scaffold_responses/` and replayed by `tests/test_scaffold_fixture_replay.py` on every CI run, so a regression that breaks one model is caught against every captured model on the next push.

```bash
# Capture every default model against the canonical directive set
# (writes one JSON file per model). Per-model failures (denied
# access, transient errors) are reported and never abort the run.
python3 scripts/capture_scaffold_fixtures.py

# Capture a single model.
python3 scripts/capture_scaffold_fixtures.py \
  --model us.anthropic.claude-haiku-4-5-20251001-v1:0

# Use a different region.
python3 scripts/capture_scaffold_fixtures.py --region us-west-2
```

Requires AWS credentials with `bedrock:InvokeModel` access to the listed models. Schedule it as a quarterly canary if you want fresh data; otherwise the existing fixtures continue to protect the validator surface. The full lifecycle (adding a new model, what to do when the replay test fires red) is documented in [`tests/fixtures/scaffold_responses/README.md`](../tests/fixtures/scaffold_responses/README.md).

### Mirror Images

Mirrors the project's third-party images (currently Volcano's docker.io `volcanosh/vc-*`) into the project's `gco/*` ECR so the cluster pulls them from same-account ECR instead of rate-limited Docker Hub. Thin wrapper over `cli/_image_mirror.py` — the same core `gco stacks deploy` runs automatically (per region) when `volcano_image_mirror.enabled` is set, so this script is mainly for pre-seeding a region or re-mirroring after a version bump. The image set and pinned tag come from `lambda/helm-installer/charts.yaml`, so the mirror never drifts from the deployed chart version.

```bash
# Preview the copy plan (no AWS calls, no copies)
python3 scripts/mirror_images.py --region us-east-1 --dry-run

# Mirror into <account>.dkr.ecr.us-east-1.amazonaws.com/gco/dockerhub/...
python3 scripts/mirror_images.py --region us-east-1
```

Requires a container runtime with a multi-arch copy path (Docker Buildx, Finch/nerdctl, or skopeo — the copy preserves the full manifest list so amd64 and arm64 nodes both match) and AWS credentials with ECR create/push permissions for the target account and region. The source pull from Docker Hub is anonymous. To mirror an additional image down the road, see "HOW TO ADD AN IMAGE TO THE MIRROR" in [`cli/_image_mirror.py`](../cli/_image_mirror.py). See the [Customization Guide](../docs/CUSTOMIZATION.md#get-volcanos-dockerio-images-off-the-rate-limited-path-ecr-mirror) for the full flow.
