# Inference Serving Guide

Deploy and manage multi-region GPU inference endpoints with GCO (Global Capacity Orchestrator on AWS).

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Model Weight Management](#model-weight-management)
- [Deploying Inference Endpoints](#deploying-inference-endpoints)
- [Supported Frameworks](#supported-frameworks)
- [Disaggregated Inference (Mooncake)](#disaggregated-inference-mooncake)
- [Managing Endpoints](#managing-endpoints)
- [Invoking Endpoints](#invoking-endpoints)
- [Multi-Region Deployment](#multi-region-deployment)
- [Monitoring Endpoint Status](#monitoring-endpoint-status)
- [Example Workflows](#example-workflows)

## Overview

GCO's inference serving extends the platform beyond batch GPU jobs to support long-running inference endpoints. You define an endpoint once, and GCO deploys it across your target regions with automatic reconciliation, model weight syncing, and Global Accelerator routing.

Key capabilities:

- Deploy inference endpoints to one or more regions with a single command
- Automatic model weight sync from S3 to each region via init containers
- DynamoDB-backed desired state with continuous reconciliation
- Rolling updates, scaling, stop/start without losing configuration
- Global Accelerator routing to the nearest healthy region
- Support for vLLM, TGI, Triton, TorchServe, and SGLang out of the box

## Architecture

Inference serving uses a reconciliation pattern similar to Kubernetes controllers:

```text
User → gco inference deploy
         │
         ▼
    DynamoDB (desired state)
         │
         ▼ (each region's inference_monitor polls)
    ┌────────────────┐    ┌────────────────┐
    │  us-east-1     │    │  eu-west-1     │
    │  ┌──────────┐  │    │  ┌──────────┐  │
    │  │ init:    │  │    │  │ init:    │  │
    │  │ S3 sync  │  │    │  │ S3 sync  │  │
    │  └────┬─────┘  │    │  └────┬─────┘  │
    │  ┌────▼─────┐  │    │  ┌────▼─────┐  │
    │  │ Inference│  │    │  │ Inference│  │
    │  │ (GPU)    │  │    │  │ (GPU)    │  │
    │  └────┬─────┘  │    │  └────┬─────┘  │
    │  ┌────▼─────┐  │    │  ┌────▼─────┐  │
    │  │ Service  │  │    │  │ Service  │  │
    │  └────┬─────┘  │    │  └────┬─────┘  │
    │  ┌────▼─────┐  │    │  ┌────▼─────┐  │
    │  │ Ingress  │  │    │  │ Ingress  │  │
    │  │ (ALB)    │  │    │  │ (ALB)    │  │
    │  └────┬─────┘  │    │  └────┬─────┘  │
    └───────┼────────┘    └───────┼────────┘
            │                     │
            └──────┬──────────────┘
                   ▼
          Global Accelerator
          (anycast IPs, health routing)
                   │
                   ▼
              End Users
         (nearest healthy region)
```

### How It Works

1. `gco inference deploy` writes the endpoint spec to a DynamoDB table (`gco-inference-endpoints`)
2. The `inference_monitor` service running in each target region polls the table every 15 seconds
3. For each endpoint targeting its region, the monitor reconciles desired state with actual K8s resources:
   - Creates/updates Deployments, Services, and Ingress rules
   - Recreates any missing resources (self-healing)
   - Purges fully-deleted endpoints from DynamoDB automatically

### Shared ALB

All inference endpoints share the same ALB as the main GCO services via EKS Auto Mode's `IngressClassParams` with `group.name: gco`. This means:

- One ALB per region (cost-efficient, registered with Global Accelerator)
- Inference requests route through the same ALB as job management APIs
- URL rewrite transforms strip the `/inference/{name}` prefix before forwarding to the pod
- The inference_monitor creates an ExternalName proxy Service in `gco-system` so the Ingress can reference the inference pod Service across namespaces

### Self-Healing

The health monitor periodically verifies the ALB hostname stored in SSM matches the actual ALB from the Kubernetes Ingress status. If the ALB changes (e.g., due to cluster recreation or IngressClassParams updates), SSM is updated automatically so the cross-region aggregator and API Gateway proxy continue routing correctly.

- If `model_source` is set, adds an init container that syncs model weights from S3 to local EFS
- Reports per-region status (replicas ready, errors) back to DynamoDB

1. State transitions (`deploying` → `running` → `stopped` → `deleted`) are driven by the CLI and reconciled by the monitor

### Inference-Optimized nodepool

Inference workloads use a Karpenter nodepool with `WhenEmpty` consolidation policy. Unlike batch job nodepools that aggressively consolidate underutilized nodes, inference nodes are only removed when completely empty. This prevents disruption to long-running serving pods.

## Model Weight Management

GCO provides a central S3 bucket (KMS-encrypted) for storing model weights. Models uploaded here are automatically available to inference endpoints across all regions.

### Upload Model Weights

```bash
# Upload a directory of model files
gco models upload ./my-model-weights/ --name llama3-8b

# Upload a single file
gco models upload ./weights.safetensors --name my-model
```

### List Models

```bash
gco models list
```

Output:

```text
  Models (2 found)
  ----------------------------------------------------------------------
  NAME                      FILES  SIZE (GB) S3 URI
  ----------------------------------------------------------------------
  llama3-8b                    12      14.96 s3://gco-models-xxx/models/llama3-8b
  my-model                      1       0.50 s3://gco-models-xxx/models/my-model
```

### Get Model URI

```bash
# Get the S3 URI for use with --model-source
gco models uri llama3-8b
# Output: s3://gco-models-xxx/models/llama3-8b
```

### Delete a Model

```bash
gco models delete llama3-8b -y
```

### How Model Sync Works

When you deploy an endpoint with `--model-source`, the inference_monitor adds an init container to the Deployment that:

1. Runs before the inference container starts
2. Uses `aws s3 sync` to download model weights from S3 to a shared EFS volume
3. Mounts the EFS volume at the model path inside the inference container

This happens automatically in every target region, so model weights are always local to the cluster.

## Deploying Inference Endpoints

### Basic Deployment

```bash
# Deploy vLLM serving a model (downloads from HuggingFace at startup)
gco inference deploy my-llm \
  -i vllm/vllm-openai:v0.22.0 \
  --gpu-count 1 \
  -e MODEL=meta-llama/Llama-3.1-8B-Instruct
```

### Deployment with S3 Model Weights

```bash
# Upload weights first
gco models upload ./llama3-weights/ --name llama3-8b

# Deploy with model sync from S3
gco inference deploy my-llm \
  -i vllm/vllm-openai:v0.22.0 \
  --gpu-count 1 \
  --model-source $(gco models uri llama3-8b) \
  -e MODEL=/models/my-llm
```

### Full Options

```bash
gco inference deploy ENDPOINT_NAME \
  --image IMAGE                    # Container image (required)
  --region REGION                  # Target region(s), repeatable (default: all)
  --replicas N                     # Replicas per region (default: 1)
  --gpu-count N                    # GPUs per replica (default: 1)
  --gpu-type TYPE                  # GPU instance type hint (e.g. g5.xlarge)
  --port PORT                      # Container port (default: 8000)
  --model-path PATH                # EFS path for model weights
  --model-source S3_URI            # S3 URI for auto-sync via init container
  --health-path PATH               # Health check endpoint (default: /health)
  --env KEY=VALUE                  # Environment variable, repeatable
  --namespace NS                   # K8s namespace (default: gco-inference)
  --label KEY=VALUE                # Label, repeatable
```

### What Gets Created

For each target region, the inference_monitor creates:

- **Deployment** — Runs the inference container with GPU resource requests, optional init container for S3 model sync
- **Service** — ClusterIP service exposing the container port
- **Ingress rule** — ALB path at `/inference/<endpoint-name>` for external access via Global Accelerator

## Supported Frameworks

GCO works with any containerized inference server. These frameworks have example manifests in `examples/`:

| Framework | Image Example | Default Port | Health Path | Use Case |
|-----------|--------------|-------------|-------------|----------|
| vLLM | `vllm/vllm-openai:v0.22.0` | 8000 | `/health` | OpenAI-compatible LLM serving |
| TGI | `ghcr.io/huggingface/text-generation-inference:3.3.7` | 8080 | `/health` | HuggingFace model serving |
| Triton | `nvcr.io/nvidia/tritonserver:24.01-py3` | 8000 | `/v2/health/ready` | Multi-framework model serving |
| TorchServe | `pytorch/torchserve:latest-gpu` | 8080 | `/ping` | PyTorch model serving |
| SGLang | `lmsysorg/sglang:v0.5.12` | 30000 | `/health` | High-throughput LLM serving with RadixAttention |

### vLLM Example

```bash
gco inference deploy vllm-llama3 \
  -i vllm/vllm-openai:v0.22.0 \
  --gpu-count 1 \
  -e MODEL=meta-llama/Llama-3.1-8B-Instruct \
  -e MAX_MODEL_LEN=4096
```

### TGI Example

```bash
gco inference deploy tgi-mistral \
  -i ghcr.io/huggingface/text-generation-inference:3.3.7 \
  --port 8080 \
  --health-path /health \
  --gpu-count 1 \
  -e MODEL_ID=mistralai/Mistral-7B-Instruct-v0.2
```

### Triton Example

```bash
gco inference deploy triton-models \
  -i nvcr.io/nvidia/tritonserver:24.01-py3 \
  --port 8000 \
  --health-path /v2/health/ready \
  --gpu-count 1 \
  --model-source s3://your-bucket/models/triton-repo
```

### TorchServe Example

```bash
gco inference deploy torchserve-resnet \
  -i pytorch/torchserve:latest-gpu \
  --port 8080 \
  --health-path /ping \
  --gpu-count 1 \
  --model-source s3://your-bucket/models/torchserve-mar
```

## Disaggregated Inference (Mooncake)

GCO supports Mooncake disaggregated serving, which splits inference into separate prefill and decode roles. This architecture enables independent scaling of compute-bound prefill and memory-bound decode workloads — prefill nodes can saturate GPU compute filling the KV cache while decode nodes stream tokens at lower utilization.

### Deploy a Disaggregated Endpoint

```bash
gco inference deploy my-llm \
  --mooncake-mode disaggregated \
  --gpu-count 1 \
  --prefill-replicas 2 \
  --decode-replicas 4 \
  -e MODEL=meta-llama/Llama-3.1-8B-Instruct
```

When `--mooncake-mode disaggregated` is set:

- The inference monitor creates separate Deployments for each role (`{name}-prefill` and `{name}-decode`), fronted by a `{name}-proxy` Deployment and Service
- A shared Mooncake transfer engine enables zero-copy KV cache transfer between roles via RDMA/TCP
- The `--image` flag is optional; when omitted, the upstream `vllm/vllm-openai` image (which bundles the Mooncake transfer engine) is used by default. The PD proxy defaults to that same image — override it with `--mooncake-proxy-image`
- `--prefill-replicas` and `--decode-replicas` set the initial replica count for each role (both default to 1)
- The public Ingress publishes only the endpoint's `/inference/{name}/v1` serving paths; the proxy's `/instances/add` admin path is never exposed externally

#### Proxy admin key

The PD proxy guards a privileged admin path with an `ADMIN_API_KEY`. By default you don't manage it: each region's monitor auto-provisions a `{name}-admin` Kubernetes Secret with a generated key and wires it into the proxy by reference (the key never appears in the endpoint spec, a CLI argument, or logs).

To bring your own key instead, create the Secret up front and name it with `--mooncake-admin-key-secret`:

```bash
kubectl create secret generic my-llm-admin \
  --from-literal=ADMIN_API_KEY="$(openssl rand -hex 32)" \
  -n gco-inference

gco inference deploy my-llm --mooncake-mode disaggregated \
  --mooncake-admin-key-secret my-llm-admin
```

A Secret named with `--mooncake-admin-key-secret` must already exist and carry a non-empty `ADMIN_API_KEY`, or the deployment is rejected (so a typo fails fast). A key is generated for you only when you name no Secret.

### Per-Role Autoscaling

Each role can autoscale independently based on its own metrics:

```bash
gco inference deploy my-llm \
  --mooncake-mode disaggregated \
  --gpu-count 1 \
  --prefill-replicas 2 --decode-replicas 4 \
  --mooncake-autoscale prefill:1:8:gpu:70 \
  --mooncake-autoscale decode:2:16:cpu:60:gpu:50
```

The `--mooncake-autoscale` format is `ROLE:MIN:MAX[:METRIC:TARGET...]`:

- `ROLE` — `prefill` or `decode`
- `MIN:MAX` — replica bounds for that role
- `METRIC:TARGET` pairs (optional, repeatable) — scaling signals (`cpu`, `memory`, `gpu`, `gpu_memory`)

Each role gets its own KEDA ScaledObject (for GPU metrics) or HPA (for CPU/memory only), targeting just that role's Deployment.

### Resize Topology

Change the prefill/decode replica counts on a running disaggregated endpoint:

```bash
gco inference set-topology my-llm --prefill 3 --decode 6
```

### Mooncake Modes

| Mode | Description |
|------|-------------|
| `disaggregated` | Separate prefill and decode Deployments with RDMA KV cache transfer between them |
| `store` | Shared per-region KV-cache store: a single `mooncake-master` per region (built-in HTTP metadata server, no etcd) backing hash-based prefix caching |
| `both` | Disaggregated prefill/decode roles plus the shared store (KV is transferred between roles *and* cached for reuse) |

The `store` and `both` modes enable the shared KV-cache store automatically — there is no separate flag to turn it on. See [Shared KV-Cache Store](#shared-kv-cache-store) for how it is wired and extended with a cold tier.

### Architecture

```text
                 ┌──────────────────────────────────────┐
                 │        Mooncake Transfer Engine      │
                 │   (zero-copy KV cache via RDMA/TCP)  │
                 └────────────┬─────────────┬───────────┘
                              │             │
              ┌───────────────▼──┐    ┌─────▼──────────────┐
              │  Prefill Pods    │    │   Decode Pods      │
              │  (compute-bound) │    │   (memory-bound)   │
              │  {name}-prefill  │    │   {name}-decode    │
              │  GPU-saturated   │    │   Streaming tokens │
              └──────────────────┘    └────────────────────┘
```

### Shared KV-Cache Store

The `store` and `both` modes add a shared Mooncake KV-cache store. The monitor maintains exactly one `mooncake-master` StatefulSet **per region** (not per endpoint), running the master daemon with its built-in HTTP metadata server — RPC on port 50051 and the metadata endpoint on port 8080. There is no etcd dependency. Every vLLM pod in the region reaches this one master, so cached KV blocks are shared across instances through hash-based prefix caching.

The store is enabled automatically for `store` and `both` modes. To extend it with an asynchronous, per-region S3 cold tier, add `--mooncake-cold-tier` at deploy time, or update a running endpoint:

```bash
# Enable the cold tier on an existing store/both endpoint
gco inference configure-store my-llm --cold-tier

# Tune the offload tier and buffer sizes (bytes)
gco inference configure-store my-llm --offload cpu --local-buffer-size 2147483648
```

`configure-store` merges onto the endpoint's existing store settings and re-triggers reconciliation, so changing one field leaves the others intact.

### Populating the KV Cache (Cold Tier)

The cold tier auto-targets the always-on general-purpose regional bucket (`gco-regional-shared-<account>-<region>`) and reads/writes under the `mooncake-kv/<endpoint>/` key prefix. You can pre-warm it — upload KV blocks or reusable prompt data your workloads will hit — with a single command:

```bash
# Upload a local file or directory into an endpoint's KV-cache cold tier
gco inference populate-kv my-llm ./kv-warm-set/ --region us-east-1
```

This writes the objects to exactly the prefix the endpoint's pods read from, so a `store`/`both` endpoint deployed with the cold tier enabled warm-starts its prefix cache from them. The endpoint must have the cold tier enabled (`--mooncake-cold-tier` at deploy, or `configure-store --cold-tier`) for its pods to consume the data. AI agents can do the same through the audited `populate_kv_cache` MCP tool.

The cold tier is per-region: run `populate-kv` once for each region you want warmed (it resolves and writes to that region's own bucket). Writes never cross a region boundary, and cold-tier reads/writes are asynchronous — they never sit on the RDMA hot path.

### How Workloads Access the KV Cache

Each role pod (prefill, decode, or the single store instance) reaches the KV cache without any application changes — the monitor wires it up declaratively:

- The monitor renders a per-endpoint `mooncake.json` into a ConfigMap and mounts it at `/etc/mooncake/mooncake.json`, pointing the connector at it through the `MOONCAKE_CONFIG_PATH` environment variable. That file carries the metadata-server address, the transport (`rdma`/`tcp`) and device, and — when the store is enabled — the `mooncake-master` address plus (when the cold tier is on) the cold-tier S3 URI.
- vLLM is launched with a `--kv-transfer-config` selecting the right connector for the role: `MooncakeConnector` for `disaggregated` (RDMA transfer of freshly computed KV from prefill to decode), `MooncakeStoreConnector` for `store` (read/write of the shared, hash-keyed prefix cache), and a `MultiConnector` chaining both for `both`.
- Prefill and decode share freshly computed KV directly over RDMA/RoCE on the EFA fabric (intra-region only). The shared store additionally lets any instance reuse a previously cached prefix, and the optional cold tier extends that cache to S3 for warm starts.
- The PD proxy checks whether a prompt's KV blocks already reside in the store before dispatching to a prefill pod, so a cache hit skips recomputation.

No client-side wiring is needed to read the cache — workloads simply serve from the role pods, which the connectors back automatically. Clients invoke the endpoint exactly as any other GCO endpoint (see [Invoking Endpoints](#invoking-endpoints)): requests reach the nearest region through Global Accelerator and are routed to the PD proxy at `/inference/{name}/v1/...`.

### GPU Placement for RDMA Role Pods

RDMA KV transfer only runs on EFA-enabled GPU nodes, and not every EFA-capable instance is a good fit for serving. When the transfer protocol is `rdma` (the default), the monitor pins each role pod to a dedicated EFA nodepool, `mooncake-efa-pool` (`46-nodepool-mooncake-efa.yaml`), by adding both an `efa=true` and a `mooncake-efa=true` node selector plus the EFA toleration and a `vpc.amazonaws.com/efa` device request.

That pool is curated to instance families with at least 80GB of GPU memory and FP8-capable Hopper/Blackwell GPUs — `p5`/`p5e`/`p5en` (H100/H200) and `p6-b200`/`p6-b300`/`p6e-gb200` (B200/B300/GB200). It deliberately excludes `p4d` (8× A100 40GB, Ampere): a model plus its KV cache that fits comfortably on an 80GB+ GPU can run out of memory on a 40GB A100, and FP8 KV-cache and quantized configurations do not run on Ampere at all. The shared training EFA pool (`43-nodepool-efa.yaml`) keeps `p4d` for distributed training, where 40GB A100s are a legitimate, cheaper option; the `mooncake-efa=true` label is unique to the inference pool, so serving pods never land on `p4d`.

## Managing Endpoints

### List Endpoints

```bash
# List all endpoints
gco inference list

# Filter by state
gco inference list --state running

# Filter by region
gco inference list -r us-east-1
```

### Scale

```bash
# Scale to 4 replicas (applied across all target regions)
gco inference scale my-llm --replicas 4
```

### Autoscaling (HPA)

Inference endpoints support automatic scaling based on resource utilization. When autoscaling is enabled, the inference_monitor creates a Kubernetes Horizontal Pod Autoscaler (HPA) alongside the Deployment. GPU-based scaling is handled through KEDA instead (see below), since GPU utilization is not a native HPA resource metric.

```bash
# Deploy with autoscaling enabled
gco inference deploy my-llm \
  -i vllm/vllm-openai:v0.22.0 \
  --replicas 2 --gpu-count 1 \
  --min-replicas 1 --max-replicas 8 \
  --autoscale-metric cpu:70 --autoscale-metric memory:80

# Scale on GPU utilization (routed through KEDA + CloudWatch)
gco inference deploy my-llm \
  -i vllm/vllm-openai:v0.22.0 \
  --replicas 2 --gpu-count 1 \
  --min-replicas 1 --max-replicas 8 \
  --autoscale-metric gpu:60
```

**Supported metrics:**

| Metric | Description | Example | Backend |
|--------|-------------|---------|---------|
| `cpu` | CPU utilization percentage | `cpu:70` (scale at 70% CPU) | Native HPA |
| `memory` | Memory utilization percentage | `memory:80` (scale at 80% memory) | Native HPA |
| `gpu` | GPU utilization percentage | `gpu:60` (scale at 60% GPU) | KEDA + CloudWatch |
| `gpu_memory` | GPU memory utilization percentage | `gpu_memory:80` | KEDA + CloudWatch |

The `--autoscale-metric` flag is repeatable — you can combine multiple metrics. The format is `type:target` where `target` is the utilization percentage threshold. If no target is specified, it defaults to 70%.

The HPA respects `--min-replicas` (default: 1) and `--max-replicas` (default: 10) bounds. The `--replicas` flag sets the initial replica count before the HPA takes over.

**GPU autoscaling.** GPU utilization cannot be read by a native HPA (Kubernetes Resource metrics are limited to CPU and memory). When any `--autoscale-metric gpu:...` (or `gpu_memory:...`) is requested, the endpoint is materialized as a KEDA `ScaledObject` with an `aws-cloudwatch` trigger that reads the `ContainerInsights` per-pod GPU metric (`pod_gpu_utilization` / `pod_gpu_memory_utilization`) for the Deployment. CPU/memory targets supplied alongside a GPU metric ride on the same `ScaledObject` as native KEDA triggers. KEDA is a mandatory cluster component, and the KEDA operator's IAM role is granted read-only CloudWatch access (`GetMetricData`, `GetMetricStatistics`, `ListMetrics`) for this purpose.

### Update Image (Rolling Update)

```bash
# Triggers a rolling update in all target regions
gco inference update-image my-llm -i vllm/vllm-openai:v0.22.0
```

### Stop and Start

```bash
# Stop (scales to zero, keeps configuration)
gco inference stop my-llm -y

# Start (restores previous replica count)
gco inference start my-llm
```

### Delete

```bash
# Mark for deletion — inference_monitor cleans up K8s resources in each region
gco inference delete my-llm -y
```

### Canary Deployments (A/B Testing)

Canary deployments let you test a new model version with a percentage of traffic before fully rolling it out. The primary deployment continues serving most traffic while the canary receives a configurable slice.

```bash
# Start a canary: 10% traffic to v0.9.0, 90% stays on current primary
gco inference canary my-llm -i vllm/vllm-openai:v0.22.0 --weight 10

# Increase canary traffic to 25%
gco inference canary my-llm -i vllm/vllm-openai:v0.22.0 --weight 25

# Happy with the canary? Promote it to primary (100% traffic)
gco inference promote my-llm -y

# Something wrong? Roll back (removes canary, 100% to primary)
gco inference rollback my-llm -y
```

How it works:

- `canary` stores the canary config (image, weight, replicas) in the endpoint spec in DynamoDB
- The inference_monitor creates a second deployment (`{name}-canary`) and service in each target region
- The ingress is updated with ALB weighted routing annotations to split traffic
- `promote` swaps the primary image to the canary image and removes the canary
- `rollback` removes the canary deployment and restores 100% traffic to the primary

### Spot Instances for Inference

Use spot instances to reduce inference serving costs. Spot GPU instances can be significantly cheaper than on-demand but can be interrupted with 2 minutes notice.

```bash
# Deploy on spot instances
gco inference deploy my-llm -i vllm/vllm-openai:v0.22.0 --gpu-count 1 --capacity-type spot

# Deploy on on-demand (default, guaranteed availability)
gco inference deploy my-llm -i vllm/vllm-openai:v0.22.0 --gpu-count 1 --capacity-type on-demand
```

When `--capacity-type spot` is set, the inference_monitor adds a `karpenter.sh/capacity-type: spot` node selector to the deployment. Karpenter then provisions spot GPU instances for those pods.

When to use spot for inference:

- Development and testing environments
- Non-critical inference endpoints with multiple replicas (if one gets interrupted, others continue serving)
- Cost-sensitive workloads where occasional brief interruptions are acceptable

When to use on-demand (default):

- Production inference endpoints requiring high availability
- Single-replica deployments where interruption means downtime

### Deploy on AWS Trainium or Inferentia

Use `--accelerator neuron` to deploy inference on AWS Trainium or Inferentia instances instead of NVIDIA GPUs. This uses `aws.amazon.com/neuron` resources and schedules on the Neuron nodepool.

```bash
# Deploy on Trainium or Inferentia
gco inference deploy my-model \
  -i public.ecr.aws/neuron/your-neuron-image:latest \
  --gpu-count 1 --accelerator neuron
```

To target a specific instance family (e.g., Inferentia only), add `--node-selector`:

```bash
gco inference deploy my-model \
  -i public.ecr.aws/neuron/your-neuron-image:latest \
  --gpu-count 1 --accelerator neuron \
  --node-selector eks.amazonaws.com/instance-family=inf2
```

Container images must include the Neuron runtime. Use images from `public.ecr.aws/neuron/` or build your own with the Neuron SDK.

## Invoking Endpoints

Once an endpoint is running, you can send prompts and chat conversations directly from the CLI or via MCP tools.

### Single-Turn Completions

```bash
# Simple prompt (auto-detects framework from container image)
gco inference invoke my-llm -p "What is GPU orchestration?"

# With max tokens
gco inference invoke my-llm -p "Explain Kubernetes" --max-tokens 200

# Explicit API path
gco inference invoke my-llm -p "Hello" --path /v1/completions

# Raw JSON body for full control
gco inference invoke my-llm -d '{"model": "meta-llama/Llama-3.1-8B-Instruct", "prompt": "Hello", "max_tokens": 50}'
```

The CLI auto-detects the serving framework from the container image and builds the appropriate request body:

- **vLLM** → `/v1/completions` (OpenAI-compatible)
- **TGI** → `/generate` (HuggingFace format)
- **Triton** → `/v2/models` (Triton HTTP API)

### Chat Conversations

For multi-turn conversations with chat models, use the `/v1/chat/completions` path:

```bash
# Chat-style request via raw JSON
gco inference invoke my-llm \
  -d '{"messages": [{"role": "user", "content": "What is Kubernetes?"}], "max_tokens": 256}' \
  --path /v1/chat/completions
```

The MCP server exposes a dedicated `chat_inference` tool that accepts a messages array directly, making it easy for AI agents to have multi-turn conversations with your endpoints.

### Health Checks

Verify an endpoint is ready before sending requests:

```bash
# Check health (routes via Global Accelerator to nearest region)
gco inference health my-llm

# Check a specific region
gco inference health my-llm -r us-east-1
```

Returns HTTP status and round-trip latency in milliseconds.

### Model Introspection

Query which models are loaded on an endpoint (OpenAI-compatible servers):

```bash
gco inference models my-llm
```

Returns the `/v1/models` response including model IDs, context lengths, and metadata.

### MCP Tools for AI Agents

The MCP server exposes four inference interaction tools so AI agents can use your endpoints programmatically:

| Tool | Description |
|------|-------------|
| `invoke_inference` | Single-turn text completion with auto framework detection |
| `chat_inference` | Multi-turn chat with OpenAI-compatible messages format |
| `inference_health` | Health check with latency reporting |
| `list_endpoint_models` | Discover loaded models via `/v1/models` |

Both `invoke_inference` and `chat_inference` support a `stream` parameter. When enabled, the request is sent with streaming mode, which reduces time-to-first-token for long generations.

## Valkey K/V Cache

Each regional stack can include a Valkey Serverless cache for microsecond-latency key-value storage. Common inference use cases:

- Prompt caching (avoid re-computing identical prompts)
- Session state for multi-turn conversations
- Feature stores for real-time model inputs
- Rate limiting and request deduplication

Enable in `cdk.json`:

```json
"valkey": {
  "enabled": true,
  "max_data_storage_gb": 5,
  "max_ecpu_per_second": 5000
}
```

The endpoint is discoverable via SSM parameter `/{project}/valkey-endpoint-{region}`. See [Customization Guide](CUSTOMIZATION.md#configure-valkey-cache) for full configuration options and `examples/valkey-cache-job.yaml` for a working example.

## RAG Patterns

GCO's inference endpoints pair well with Retrieval-Augmented Generation (RAG) workflows. Here's how the components fit together:

### Semantic Caching with Valkey

Use the Valkey cache to avoid redundant inference calls for semantically similar prompts.

```python
import hashlib
import json
import boto3
import valkey

# Connect to Valkey and Bedrock
cache = valkey.Valkey(host="VALKEY_ENDPOINT", port=6379, ssl=True)
bedrock = boto3.client("bedrock-runtime")

def get_embedding(text):
    """Get embedding from Amazon Bedrock."""
    response = bedrock.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": text}),
    )
    return json.loads(response["body"].read())["embedding"]

def cached_inference(prompt, inference_fn):
    """Check cache before calling inference."""
    cache_key = f"prompt:{hashlib.sha256(prompt.encode()).hexdigest()}"
    cached = cache.get(cache_key)
    if cached:
        return json.loads(cached)

    result = inference_fn(prompt)
    cache.setex(cache_key, 3600, json.dumps(result))  # cache 1 hour
    return result
```

### Vector Store Options

For the retrieval component of RAG, GCO doesn't include a built-in vector database — this is intentional to avoid being opinionated about a rapidly evolving space. Recommended options:

| Option | Best For | Managed |
|--------|----------|---------|
| Amazon OpenSearch Serverless | Production RAG with full-text + vector search | Yes |
| Amazon Bedrock Knowledge Bases | Fully managed RAG with zero infrastructure | Yes |
| pgvector on Amazon RDS | Teams already using PostgreSQL | Yes |
| ElastiCache Valkey 8.2 (node-based) | Microsecond-latency vector search at scale | Yes |
| ChromaDB / Qdrant on EKS | Self-hosted, full control | No |

A typical RAG flow with GCO:

```text
User query
    → Valkey cache check (semantic cache hit?)
    → If miss: embed query (Bedrock Titan)
    → Vector search (OpenSearch / Bedrock KB / pgvector)
    → Augment prompt with retrieved context
    → Inference endpoint (gco inference invoke)
    → Cache result in Valkey
    → Return to user
```

See `examples/valkey-cache-job.yaml` for a working Valkey caching example.

## Multi-Region Deployment

By default, `gco inference deploy` targets all deployed regions. This is the recommended approach because Global Accelerator routes users to the nearest healthy region — if an endpoint only exists in some regions, users routed to a region without it will get a 404.

```bash
# Deploy to all regions (recommended — ensures consistent global routing)
gco inference deploy my-llm \
  -i vllm/vllm-openai:v0.22.0

# Deploy to specific regions (use with caution — see note below)
gco inference deploy my-llm \
  -i vllm/vllm-openai:v0.22.0 \
  -r us-east-1 -r eu-west-1
```

> **Routing caveat:** If you deploy to a subset of regions, Global Accelerator may route users to a region where the endpoint doesn't exist. The CLI warns you about this. For production inference, deploy to all regions or ensure your users only connect from regions where the endpoint is available.

### Global Accelerator Routing

Once deployed, inference endpoints are accessible through Global Accelerator at:

```text
https://<GA_ENDPOINT>/inference/<endpoint-name>/
```

Global Accelerator automatically routes requests to the nearest healthy region. If a region becomes unhealthy, traffic fails over to the next closest region.

### Per-Region Status

Each region independently reconciles and reports its status:

```bash
gco inference status my-llm
```

```text
  Endpoint: my-llm
  ------------------------------------------------------------
  State:     running
  Image:     vllm/vllm-openai:v0.22.0
  Replicas:  2
  GPUs:      1
  Port:      8000
  Path:      /inference/my-llm
  Namespace: gco-inference
  Created:   2025-01-15T10:30:00+00:00

  Region Status:
  REGION             STATE        READY DESIRED LAST SYNC
  -----------------------------------------------------------------
  us-east-1          running          2       2 2025-01-15T10:35:00
  eu-west-1          running          2       2 2025-01-15T10:35:12
```

## Monitoring Endpoint Status

### CLI Status Check

```bash
# Detailed status with per-region breakdown
gco inference status my-llm

# Quick list of all endpoints
gco inference list
```

### kubectl Inspection

```bash
# Check pods
kubectl get pods -n gco-inference --context arn:aws:eks:us-east-1:ACCOUNT:cluster/gco-us-east-1

# Check deployment rollout
kubectl rollout status deployment/my-llm -n gco-inference

# View logs
kubectl logs -n gco-inference deployment/my-llm
```

### Endpoint States

| State | Description |
|-------|-------------|
| `deploying` | Endpoint registered, waiting for inference_monitor to create resources |
| `running` | All target regions have healthy replicas |
| `stopped` | Scaled to zero, configuration preserved |
| `deleted` | Marked for deletion, inference_monitor cleaning up resources |

## Example Workflows

### End-to-End: Deploy a vLLM Endpoint

```bash
# 1. Check GPU capacity
gco capacity check -i g5.xlarge -r us-east-1

# 2. Upload model weights (optional — vLLM can download from HuggingFace)
gco models upload ./llama3-weights/ --name llama3-8b

# 3. Deploy the endpoint
gco inference deploy vllm-llama3 \
  -i vllm/vllm-openai:v0.22.0 \
  --gpu-count 1 \
  --model-source $(gco models uri llama3-8b) \
  -e MODEL=/models/vllm-llama3 \
  -r us-east-1

# 4. Monitor deployment
gco inference status vllm-llama3

# 5. Test the endpoint
curl https://GA_ENDPOINT/inference/vllm-llama3/v1/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "/models/vllm-llama3", "prompt": "Hello", "max_tokens": 50}'

# 6. Scale up for production
gco inference scale vllm-llama3 --replicas 3

# 7. Update to a new version
gco inference update-image vllm-llama3 -i vllm/vllm-openai:v0.22.0

# 8. Clean up
gco inference delete vllm-llama3 -y
```

### Quick Single-Region Test with Example Manifests

For development or quick testing, you can apply example manifests directly:

```bash
# Apply a vLLM example manifest directly
gco jobs submit-direct examples/inference-vllm.yaml -r us-east-1

# Other available examples:
# examples/inference-tgi.yaml
# examples/inference-triton.yaml
# examples/inference-torchserve.yaml
# examples/inference-sglang.yaml
# examples/model-download-job.yaml
```

Note: Direct manifest submission creates resources in a single region only. For multi-region production deployments, use `gco inference deploy`.

---

**Related documentation:**

- [CLI Reference](CLI.md) — Full command reference for `inference` and `models` commands
- [Architecture Details](ARCHITECTURE.md) — Infrastructure deep dive
- [Quick Start Guide](../QUICKSTART.md) — Get running in under 60 minutes
