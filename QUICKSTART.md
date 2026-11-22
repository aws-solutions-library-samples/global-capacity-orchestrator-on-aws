# Quick Start Guide

Get Global Capacity Orchestrator (GCO) running in under 60 minutes.

> **🐳 Use the dev container.** GCO pins exact versions of a lot of Python packages so CI is reproducible, which makes installing on top of an existing Python environment a frequent source of `ResolutionImpossible` errors. The recommended path — and the one this guide follows — is the dev container: [`scripts/setup-dev-alias.sh`](scripts/setup-dev-alias.sh) builds it and installs a `gco` shell function, so every command below runs inside the container without a hand-written `docker run …`. Host installs are an advanced path for contributors who develop on their host; see [Installing on your host instead](#installing-on-your-host-instead-advanced).
>
> **🤖 Or let an agent drive:** once `gco` is installed (Step 1), `gco autopilot` starts [Claude Code](https://code.claude.com/docs/en/overview), `gco autopilot --engine codex` starts OpenAI Codex, and `gco autopilot --engine opencode` starts [OpenCode](https://opencode.ai/docs/), all on Amazon Bedrock with the GCO MCP server and recommended companion MCPs wired in. See [docs/AUTOPILOT.md](docs/AUTOPILOT.md).
>
> **💡 Tip:** the same [MCP server](gco_mcp/) also plugs into your IDE for guided exploration — *"What do I need to deploy?"*, *"Explain the architecture"*. See [MCP Server](#mcp-server-for-cursor--kiro--llm-integration) below.

## Table of Contents

- [Prerequisites Check](#prerequisites-check)
- [Step 1: Clone and Build the Dev Container](#step-1-clone-and-build-the-dev-container)
- [Step 2: Run the GCO CLI](#step-2-run-the-gco-cli)
- [First Success Milestone](#first-success-milestone)
- [Step 3: Bootstrap CDK](#step-3-bootstrap-cdk-optional)
- [Step 4: Deploy Infrastructure](#step-4-deploy-infrastructure)
- [Step 5: Configure Cluster Access](#step-5-configure-cluster-access-optional)
- [Step 6: Run a Test Job](#step-6-run-a-test-job)
- [Step 7: Deploy an Inference Endpoint](#step-7-deploy-an-inference-endpoint-optional)
- [Next Steps](#next-steps)
- [MCP Server](#mcp-server-for-cursor--kiro--llm-integration)
- [Common Issues](#common-issues)
- [Clean Up](#clean-up)

## Prerequisites Check

The only host-side requirements for the recommended (container) path are AWS credentials, Git, and a container runtime:

```bash
# Verify AWS CLI is configured (or just have ~/.aws populated to mount in)
aws --version
aws sts get-caller-identity

# Verify your container runtime is running — Docker, Finch or Podman
# (Colima also works; see the header of Dockerfile.dev for its socket path)
docker --version    # or: finch version / podman version
docker info         # confirms the daemon is running
```

Your AWS identity needs permission to create the stacks; administrator access
is the simplest. The `gco` commands that call the GCO API (`gco jobs submit`,
`jobs list`, `jobs logs`, `jobs delete`, `queue submit`, `dag run`,
`inference invoke`) are authorized by that identity's own IAM policy, which
must allow `execute-api:Invoke` on the GCO APIs: the APIs' resource policies
admit no principal of the account by themselves. Administrator credentials
have the permission; [CLI.md → Regional API Mode](docs/CLI.md#regional-api-mode)
has a policy for a narrower identity.

Everything else — Python 3.14, Node.js 24, CDK, kubectl, the AWS CLI, Docker CLI + Buildx and every GCO Python dependency — ships inside the container at the exact versions CI uses.

## Step 1: Clone and Build the Dev Container

Clone the repository, then run the setup script. It detects your container runtime (Docker, Finch or Podman), builds the `gco-dev` image from `Dockerfile.dev` (cached on later runs; about two minutes the first time), wires the runtime's socket through, and installs a `gco` shell *function* into your shell profile:

```bash
git clone https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws.git
cd global-capacity-orchestrator-on-aws

./scripts/setup-dev-alias.sh   # builds gco-dev from Dockerfile.dev + installs the `gco` shell function
source ~/.zshrc                # or ~/.bashrc — the script prints which file it updated
```

The image is multi-arch — it builds natively on `linux/amd64` (Intel/x86_64 hosts and CI) and `linux/arm64` (Apple Silicon Macs, Graviton Linux) by selecting the right kubectl / AWS CLI / Docker CLI / Buildx binary via `$TARGETARCH`, with no `--platform` flag. Buildx ships in the image so the `linux/amd64` Lambda asset builds and the multi-arch image mirror that `gco stacks deploy-all` runs succeed on Apple Silicon as well as x86_64.

Why a function and not an alias: it forwards arguments and pipes correctly, attaches a TTY only when one is present, mounts your current directory at `/workspace`, and bakes in the correct socket for the runtime you actually have. Re-run the script whenever you switch runtimes; `--print` previews the function, `--runtime <name>` forces one, `--rc <path>` targets a specific profile, and `--no-build` skips the image build.

> **Security note:** the function shares your container runtime's Docker-compatible API socket with the container so `cdk deploy` can build Lambda assets and mirror images through your host daemon. That is host-socket pass-through, not Docker-in-Docker: anyone with access to the container has root-equivalent access to that daemon, so only use this on trusted hosts. The script finds the socket by running the dev image with each likely path mounted: `/var/run/docker.sock` (Docker Desktop, Colima, OrbStack and Linux Docker), the socket of `DOCKER_HOST` or the current Docker context (rootless Docker), the API service `podman info` reports, and finch-daemon's socket in Finch's VM (Finch v1.8.0 and later). For Podman and finch-daemon, which have no BuildKit API, the function also sets `DOCKER_BUILDKIT=0`, and where SELinux keeps containers off the socket (Podman machines, Fedora) it disables label separation for the container. `--socket PATH` names the socket yourself, and `--no-socket` shares none.

## Step 2: Run the GCO CLI

Every `gco` command now runs inside the dev container against your checkout:

```bash
gco --version
gco --help
```

<details>
<summary>Prefer an interactive shell inside the container?</summary>

```bash
docker run -it --rm \
  -v ~/.aws:/root/.aws:ro \
  -v $(pwd):/workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -w /workspace \
  gco-dev

# From inside the container
gco --version
```

Docker Desktop, Colima and OrbStack serve `/var/run/docker.sock` inside their VMs, so the command works as written. For rootless Docker, Podman and Finch, `./scripts/setup-dev-alias.sh --print` shows the socket mount and flags to use, and the header of [`Dockerfile.dev`](Dockerfile.dev) explains them.

</details>

<details>
<summary>Installing on your host instead (advanced)</summary>

### Installing on your host instead (advanced)

This path commonly fails with the pinned-version `ResolutionImpossible` / dependency-resolver errors described in [Common Issues](#pip-install-fails-with-resolutionimpossible-or-dependency-conflicts); it exists for contributors who develop on their host (editor integrations, the Pyright/mypy LSP). You additionally need:

```bash
# Python 3.14+ (3.14 used in CI)
python3 --version

# Node.js 24 and npm 12.2.1 (see .nvmrc and package.json)
node --version
npm --version

# Install and verify the repository's exact npm pin, then the locked CDK CLI
bash .github/scripts/use-pinned-npm.sh package.json
npm ci --ignore-scripts --no-audit --no-fund
npm exec -- cdk --version
```

Then install GCO into a **fresh** isolated environment — never into one that already has CDK, FastAPI, mypy or other commonly pinned packages:

```bash
# Option A: pipx (CLI only)
brew install pipx && pipx ensurepath  # macOS
pipx install -e .

# Option B: pip in a fresh virtualenv (development)
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

gco --version
```

If pip fails with `ResolutionImpossible` or similar resolver errors, start from a fresh venv or switch to the dev container — please don't try to relax the pins on your end.

</details>

## First Success Milestone

**This milestone incurs no AWS charges.** It runs entirely on your machine and confirms the `gco` CLI works before you deploy anything billable.

From a fresh clone, the path to this milestone is Step 1 and Step 2 above — clone, run the setup script, reload your shell, then:

```bash
gco --version
```

Success looks like the `gco` CLI printing its version and exiting without error:

```text
gco, version <current-version>
```

When you see a `gco, version …` line and no error, your environment is correctly set up and you have reached the First Success Milestone.

> **Verification failed?** If `gco --version` does not print a `gco, version …` line — for example you see `command not found` (reload your shell, or check the profile file the setup script named), a Python import error, or a `ResolutionImpossible` / dependency-resolver error from a host install — go to [Common Issues](#common-issues) for the fix. You never need to read source code to get past this step.

After this milestone, the next checkpoint is the **First Deploy Milestone** in [Step 4](#step-4-deploy-infrastructure). **That step provisions billable AWS resources**, unlike this milestone. Steps labeled *(Optional)* below are not required to reach the First Success Milestone.

## Step 3: Bootstrap CDK (Optional)

CDK bootstrap runs automatically during `deploy` and `deploy-all` if a region hasn't been bootstrapped yet. You can skip this step entirely.

If you prefer to bootstrap manually:

```bash
# Bootstrap CDK in your target region (optional — deploy will do this automatically)
gco stacks bootstrap -r us-east-1
```

## Step 4: Deploy Infrastructure

> **First Deploy Milestone — this step provisions billable AWS resources.** Unlike the [First Success Milestone](#first-success-milestone), deploying infrastructure creates AWS resources (EKS, VPC, load balancer, API Gateway, Lambda, and more) that incur charges until you [clean up](#clean-up).

Run this from your shell — the `gco` function from [Step 1](#step-1-clone-and-build-the-dev-container) executes it inside the dev container (or run it from an interactive container shell, see [Step 2](#step-2-run-the-gco-cli)):

```bash
# Start Finch VM (if using Finch on the host — Docker Desktop & Colima need no equivalent)
finch vm start

# Deploy all stacks
gco stacks deploy-all -y
```

> **Did the setup script say `Socket mount : none`?** The dev container builds the deployment's images through the runtime socket the `gco` function shares, so fix what the script's note names and re-run `./scripts/setup-dev-alias.sh`: start Docker or Podman's API service (on Linux, `systemctl --user enable --now podman.socket`), upgrade Finch to v1.8.0 or later, or pass `--socket PATH`. Or run this step from a [host install](#installing-on-your-host-instead-advanced) as `CDK_DOCKER=<runtime> gco stacks deploy-all -y`. The same applies to `gco stacks deploy`, `gco upgrade` and `gco images build|push`.

Or deploy a single region:

```bash
gco stacks deploy gco-us-east-1 -y
```

> **Note:** The CLI automatically detects Docker, Finch, or Podman (in that order). If you need to override, set `CDK_DOCKER=docker`, `CDK_DOCKER=finch`, or `CDK_DOCKER=podman`.

**What's being created:**

- VPC with public/private subnets
- EKS Auto Mode cluster
- Application Load Balancer
- API Gateway
- Lambda function for kubectl operations
- Health Monitor and Manifest Processor services

## Step 5: Configure Cluster Access (Optional)

> **Most users can skip this.** The EKS API endpoint is `PRIVATE` by default, and every job path below works without kubectl: SQS (`gco jobs submit-sqs`), the API Gateway (`gco jobs submit`), and the global queue (`gco queue submit`) all authenticate with your AWS credentials.

If you do want kubectl — for debugging or manual operations — you need two things, and `gco cluster doctor` tells you which is missing:

```bash
# 1. Authorization: an EKS access entry for your IAM principal (one-shot, after deploy)
gco stacks access -r us-east-1

# 2. Reachability: reach the private endpoint from your laptop over SSM
gco cluster tunnel --via-ssm auto --region us-east-1   # holds the tunnel open; prints the kubectl flags
```

The tunnel provisions a self-terminating bastion in the cluster VPC and tears it down on exit. The commands that run kubectl for you (`gco jobs submit-direct`, `gco files ls` and `gco files download`) take `--via-ssm auto` themselves, so they need only the access entry. If you would rather expose the endpoint, `gco stacks eks endpoint set PUBLIC_AND_PRIVATE --cidr <your-ip>/32` edits `cdk.json` and the next `gco stacks deploy` applies it; see [EKS Cluster Configuration](docs/CUSTOMIZATION.md#eks-cluster-configuration) for the trade-offs.

## Step 6: Run a Test Job

Submit through SQS — the recommended path: it works with the default `PRIVATE` endpoint, needs no kubectl, and the built-in KEDA-scaled queue processor picks the job up:

```bash
# Submit a job (uses your AWS credentials, no kubectl needed)
gco jobs submit-sqs examples/simple-job.yaml --region us-east-1

# Check job status
gco jobs list --all-regions

# View logs once the job completes
gco jobs logs hello-gco -n gco-jobs -r us-east-1

# Clean up
gco jobs delete hello-gco -n gco-jobs -r us-east-1 -y
```

`examples/simple-job.yaml` sets `ttlSecondsAfterFinished: 120`, so Kubernetes
deletes the Job two minutes after it finishes. After that, `gco jobs logs`
reads the output from CloudWatch Logs, and the clean-up step is not needed:
`gco jobs delete` then fails with the API's 404.

**Other submission methods:**

```bash
# Via the API Gateway (SigV4-authenticated REST, also kubectl-free)
gco jobs submit examples/simple-job.yaml -n gco-jobs

# Via the global DynamoDB queue (priority, status tracking, audit trail)
gco queue submit examples/simple-job.yaml --region us-east-1

# Via kubectl (requires cluster access — see Step 5)
kubectl apply -f examples/simple-job.yaml
```

See [Core Concepts — Manifest Submission](docs/CONCEPTS.md#manifest-submission) for how the paths differ.

**Your GCO cluster is ready.** 🎉 Some things to try next:

```bash
# Check GPU capacity before submitting GPU jobs
gco capacity check --instance-type g4dn.xlarge --region us-east-1

# Get a region recommendation for a GPU workload
gco capacity recommend-region --gpu

# View costs by region
gco costs summary

# Run a multi-step pipeline (DAG)
gco dag run examples/pipeline-dag.yaml --region us-east-1

# Queue depth, running jobs and GPU/CPU utilization per Region
gco capacity status
```

## Step 7: Deploy an Inference Endpoint (Optional)

GCO can also deploy long-running inference endpoints across regions. Here's a quick example:

```bash
# Deploy a vLLM inference endpoint (an ungated model, no Hugging Face token needed)
gco inference deploy my-llm \
  -i vllm/vllm-openai:v0.32.0 \
  --gpu-count 1 \
  -e MODEL=Qwen/Qwen2.5-1.5B-Instruct \
  -r us-east-1

# Check deployment progress
gco inference status my-llm

# Send it a prompt once it reports running
gco inference invoke my-llm -p "Hello"

# List all inference endpoints
gco inference list

# Clean up when done
gco inference delete my-llm -y
```

The `inference_monitor` in each target region automatically creates the Kubernetes Deployment, Service, autoscaling objects, and supporting configuration. Inference requests remain behind the shared authenticated Gateway API route; the monitor does not create a direct per-endpoint route. See [docs/INFERENCE.md](docs/INFERENCE.md) for the full inference guide including model weight management, multi-region deployment, and supported frameworks.

## Next Steps

- Follow the [Learning Path](docs/LEARNING_PATH.md) to go from this first job to GPU jobs, shared storage, pipelines and the role tracks
- Read [README.md](README.md) for full documentation
- See [docs/INFERENCE.md](docs/INFERENCE.md) for inference serving guide
- See [docs/CUSTOMIZATION.md](docs/CUSTOMIZATION.md) for customization options
- Review [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for architecture details
- When a new release ships, [docs/UPGRADING.md](docs/UPGRADING.md) explains `gco upgrade` — it recreates the regional stacks, so back regional data up to the cluster-shared bucket first
- For a Region-pinned path, call that Region's [API bridge](docs/CUSTOMIZATION.md#regional-api-gateway-aggregation-bridge-and-direct-regional-access) directly: every Region has one, and the same `execute-api:Invoke` permission covers it

### MCP Server (for Cursor / Kiro / LLM integration)

GCO includes an MCP server with 155 tools by default (up to 216 with all flags enabled) spanning the CLI and project-aware resources. The recommended install needs no clone: the one-click buttons in the [README](README.md#install-the-mcp-server) add it to Kiro, Cursor or VS Code pinned to the latest release, and [`gco_mcp/README.md`](gco_mcp/README.md#install-with-uv-recommended) has the equivalent `uvx` command for any other client.

To run it from this checkout instead — when developing GCO, or for the clone-only resources (`docs://`, `source://`, `k8s://`, `infra://`) and the stack lifecycle tools — the dev container already has the `[mcp]` extras installed, so all you need is the client-side config. The most portable form passes an absolute path in `args` (works in Cursor, Kiro, Claude Desktop, etc.):

```jsonc
// MCP client config file (for example, Cursor's ~/.cursor/mcp.json)
{
  "mcpServers": {
    "gco": {
      "command": "python3",
      "args": ["<ABSOLUTE_REPO_PATH>/gco_mcp/run_mcp.py"]
    }
  }
}
```

Replace `<ABSOLUTE_REPO_PATH>` with the absolute path to your local GCO clone — the `global-capacity-orchestrator-on-aws` directory created by `git clone` — so `args` resolves to that clone's `gco_mcp/run_mcp.py`. Save the snippet in your MCP client's own config file; its location varies by client (Cursor uses `~/.cursor/mcp.json`), so see [`gco_mcp/README.md`](gco_mcp/README.md) for each client's path, including a `cwd`-shorthand variant for Kiro.

After saving, reload the `gco` server in your MCP client's settings UI so the tool descriptors get picked up. If you're running outside the dev container, install the MCP extras into your venv first:

```bash
pip install -e ".[mcp]"
```

## Common Issues

### `pip install` fails with `ResolutionImpossible` or dependency conflicts

GCO pins exact versions of many Python packages (CDK, AWS SDKs, FastAPI, mypy, Ruff, etc.) so CI is reproducible. Installing on top of an existing Python environment frequently triggers resolver errors.

**Fix:** use the [dev container](#step-1-clone-and-build-the-dev-container) — it ships every dep at the correct version and has no overlap with your host Python. If you must install on the host, start from a brand-new virtual environment or use `pipx install -e .` (which gives the CLI its own isolated env).

### CDK CLI version mismatch

If you see `Cloud assembly schema version mismatch`, reinstall the exact repository-owned CDK graph rather than downloading a global latest release:

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm exec -- cdk --version
```

The CLI prefers `node_modules/.bin/cdk` from this lockfile. If the local graph is absent, `gco` can still use an already-installed CDK on `PATH`, but contributors and CI should use the locked copy.

### "Stack already exists"

If deployment fails partway through, destroy and redeploy:

```bash
gco stacks destroy-all -y
gco stacks deploy-all -y
```

### `403 Client Error: Forbidden` from `gco jobs` or `gco queue`

API Gateway refuses a caller whose IAM policy does not allow
`execute-api:Invoke` on the GCO APIs. Attach a policy that allows it
([CLI.md → Regional API Mode](docs/CLI.md#regional-api-mode)); administrator
credentials already have it.

### "Unauthorized" when using kubectl

`Unauthorized` is an authentication failure, not a reachability one: your IAM principal has no EKS access entry. Run `gco stacks access -r <region>` ([Step 5](#step-5-configure-cluster-access-optional)); with the default `PRIVATE` endpoint, reach the API through `gco cluster tunnel --via-ssm auto --region <region>` (or expose it with `gco stacks eks endpoint set PUBLIC_AND_PRIVATE --cidr <your-ip>/32` and redeploy). `gco cluster doctor --region <region>` reports which layer is failing.

### Pods not starting

Check pod events for details:

```bash
kubectl describe pods -n gco-system
```

## Clean Up

When you're done testing:

```bash
# Destroy all stacks
gco stacks destroy-all -y
```

---

**Need help?** Check [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
