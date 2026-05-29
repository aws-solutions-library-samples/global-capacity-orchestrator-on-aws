# Live Demo Script

Automated feature demonstration for **Global Capacity Orchestrator (GCO)** — *One API. Every Accelerator. Any Region.* Run `live_demo.sh` in a terminal during presentations to showcase the platform's capabilities in real time.

---

## Table of Contents

- [What It Does](#what-it-does)
- [Prerequisites](#prerequisites)
- [Running the Demo](#running-the-demo)
- [Recording the Demo](#recording-the-demo)
- [What the Script Covers](#what-the-script-covers)
- [Customization](#customization)
- [Maintenance Guide](#maintenance-guide)
- [Troubleshooting](#troubleshooting)

---

## What It Does

`live_demo.sh` is a single script designed to be run in a visible terminal during a live presentation. It walks through GCO's core capabilities automatically, with clear narration, pauses, and visually formatted output so the audience can follow along.

The script reads `cdk.json` to detect which optional features are enabled (schedulers, FSx, Valkey) and adapts its flow accordingly — it only demos what's actually deployed.

---

## Prerequisites

- GCO CLI installed (`gco --version`)
- Infrastructure deployed (`gco stacks deploy-all -y`)
- EKS endpoint set to `PUBLIC_AND_PRIVATE` (for kubectl access during demo)
- Cluster access configured (`./scripts/setup-cluster-access.sh`)
- `kubectl` working (`kubectl get nodes`)
- `jq` installed (`brew install jq` / `apt install jq`)
- Terminal with at least 120 columns and color support

---

## Running the Demo

```bash
# From the repo root:
bash demo/live_demo.sh

# Or with a specific region override:
GCO_DEMO_REGION=us-west-2 bash demo/live_demo.sh
```

The script pauses between sections. Press Enter to advance.

---

## Recording the Demo

`record_demo.sh` captures a non-interactive run of `live_demo.sh` as an animated GIF for embedding in READMEs and documentation.

### Recording Prerequisites

Everything needed for `live_demo.sh` (see [Prerequisites](#prerequisites) above), plus:

- `asciinema` — records the terminal session as a `.cast` file

  ```bash
  brew install asciinema     # macOS
  pip install asciinema      # pip
  apt install asciinema      # Debian/Ubuntu
  ```

- `agg` — converts `.cast` to animated GIF (optional — skipped gracefully if missing)

  ```bash
  brew install agg           # macOS
  cargo install agg          # Rust/cargo
  ```

### Recording

```bash
# Record and generate GIF (from repo root)
bash demo/record_demo.sh

# Record only the .cast file (skip GIF conversion)
SKIP_GIF=1 bash demo/record_demo.sh

# Custom dimensions and speed
DEMO_COLS=140 DEMO_ROWS=40 DEMO_SPEED=3 bash demo/record_demo.sh
```

The script runs its own preflight validation (tools, cluster access, disk space) before recording. Output files:

| File | Description |
|---|---|
| `demo/live_demo.cast` | Asciinema recording (JSON text, replayable with `asciinema play`) |
| `demo/live_demo.gif` | Animated GIF for embedding in READMEs |

### Re-recording After Changes

After editing `live_demo.sh` or `lib_demo.sh`, re-record:

```bash
bash demo/record_demo.sh
```

The GIF is embedded in both `demo/README.md` and the main `README.md`. Commit the updated `.gif` and `.cast` files.

### Replay Without Re-recording

```bash
asciinema play demo/live_demo.cast
```

---

## What the Script Covers

| Section | Feature | Condition |
|---|---|---|
| 1 | Cost visibility — summary, regional breakdown, daily trend | Always |
| 2 | Capacity discovery — GPU availability, region recommendation, auto-region SQS | Always |
| 3 | Volcano scheduler — gang scheduling example | `volcano.enabled = true` in cdk.json |
| 4 | Kueue scheduler — quota-based job queueing | `kueue.enabled = true` in cdk.json |
| 5 | YuniKorn scheduler — app-aware fair scheduling | `yunikorn.enabled = true` in cdk.json |
| 6 | Slurm operator — HPC batch scheduling | `slurm.enabled = true` in cdk.json |
| 7 | FSx for Lustre — high-performance scratch storage | `fsx_lustre.enabled = true` in cdk.json |
| 8 | Valkey cache — serverless K/V caching | `valkey.enabled = true` in cdk.json |
| 9 | Inference endpoint — deploy, invoke, and teardown | Always (skip with `SKIP_INFERENCE=1`) |
| 10 | EFS shared storage — persistent job outputs | Always |

---

## Customization

- **Skip sections:** Set environment variables to skip specific parts:

  ```bash
  SKIP_COSTS=1 bash demo/live_demo.sh         # Skip cost section
  SKIP_CAPACITY=1 bash demo/live_demo.sh      # Skip capacity section
  SKIP_SCHEDULERS=1 bash demo/live_demo.sh    # Skip all scheduler demos
  ```

- **Region:** Override the auto-detected region:

  ```bash
  GCO_DEMO_REGION=eu-west-1 bash demo/live_demo.sh
  ```

- **Speed:** Adjust the typing delay and pause duration:

  ```bash
  GCO_DEMO_FAST=1 bash demo/live_demo.sh      # Shorter pauses
  ```

---

## Maintenance Guide

This script depends on the example manifests in `examples/` and the `gco` CLI. When updating GCO, check the following:

1. **New schedulers or features added to cdk.json** — Add a detection block and demo section in `live_demo.sh`. Follow the pattern of existing scheduler sections.
2. **Example manifest names changed** — Update the corresponding `submit` commands in the script. The manifest filenames are referenced directly.
3. **CLI command changes** — If `gco` subcommands change syntax, update the commands in the script. The script calls `gco costs`, `gco jobs`, and `gco files` directly.
4. **New example jobs** — If a new example is added that showcases a feature worth demoing, add a section. Use the `section_header`, `narrate`, and `run_cmd` helper functions for consistent formatting.
5. **cdk.json schema changes** — The script parses `cdk.json` with `jq`. If the config structure changes (e.g., `helm.volcano.enabled` moves), update the `jq` queries.

### Testing Changes

After editing the script, do a dry run:

```bash
# Check syntax
bash -n demo/live_demo.sh

# Run with a quick pass (shorter pauses)
GCO_DEMO_FAST=1 bash demo/live_demo.sh
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `jq: command not found` | Install jq: `brew install jq` or `apt install jq` |
| `gco: command not found` | Install the GCO CLI. The recommended path is the [dev container](../QUICKSTART.md#step-1-clone-and-build-the-dev-container); a host install (`pipx install -e .` from the repo root) is the advanced, non-recommended path. |
| Jobs stuck in Pending | Check node provisioning: `kubectl get nodes -w` — GPU nodes take 60-90s |
| Script skips a scheduler you enabled | Re-run `gco stacks deploy-all -y` after changing cdk.json |
| Colors not rendering | Ensure your terminal supports ANSI colors. Try `TERM=xterm-256color` |
| FSx/Valkey section skipped | Verify `fsx_lustre.enabled` / `valkey.enabled` is `true` in cdk.json |

> **Installing the GCO CLI on your host?** GCO pins exact versions of many Python packages, so host installs frequently fail with `ResolutionImpossible` / dependency-resolver errors. Prefer the [dev container](../QUICKSTART.md#step-1-clone-and-build-the-dev-container) — it ships every dependency at the exact versions CI uses. See [Common Issues](../QUICKSTART.md#pip-install-fails-with-resolutionimpossible-or-dependency-conflicts) for the resolver-error fix.
