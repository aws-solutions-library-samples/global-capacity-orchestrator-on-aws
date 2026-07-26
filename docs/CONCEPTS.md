# Core Concepts

This guide explains the fundamental concepts behind Global Capacity Orchestrator (GCO). Read this before diving into the technical documentation.

## Table of Contents

- [What is GCO?](#what-is-gco)
- [The Problem It Solves](#the-problem-it-solves)
- [Key Concepts](#key-concepts)
  - [Multi-Region Architecture](#multi-region-architecture)
  - [EKS Auto Mode](#eks-auto-mode)
  - [Nodepools](#nodepools)
  - [Manifest Submission](#manifest-submission)
  - [Global Routing](#global-routing)
- [Inference Serving](#inference-serving)
  - [How Inference Works](#how-inference-works)
  - [Supported Frameworks](#supported-frameworks)
- [Storage Options](#storage-options)
  - [EFS (Elastic File System)](#efs-elastic-file-system)
  - [FSx for Lustre](#fsx-for-lustre)
- [Security Model](#security-model)
- [API Access Modes](#api-access-modes)
  - [Global API (Default)](#global-api-default)
  - [Regional API Bridge and Direct Access](#regional-api-bridge-and-direct-access)
- [How Components Work Together](#how-components-work-together)
- [Common Workflows](#common-workflows)

## What is GCO?

GCO is a **multi-region Kubernetes platform** built on AWS EKS Auto Mode, designed specifically for AI/ML workloads that need GPU compute. It provides:

- In commercial `aws`, one IAM-authenticated workload API with health-based regional failover; in other partitions, IAM-authenticated regional workload APIs
- Capacity-aware CLI and queue workflows for selecting a target region
- Automatic GPU node provisioning through EKS Auto Mode and purpose-built NodePools
- Inference endpoint management across regions with a single command
- Shared storage for job outputs that persists after pods terminate
- Production-ready security with IAM authentication

Think of it as a GPU workload platform: you submit a Kubernetes manifest, and GCO validates it, provisions matching nodes, runs it, and can persist outputs when the workload mounts shared storage. Capacity-aware CLI/queue workflows can choose a Region. In commercial `aws`, the global workload API routes by network health and latency rather than inspecting GPU inventory; other partitions use explicitly selected regional workload APIs. For inference, GCO reconciles long-running endpoints across selected Regions and, in `aws`, provides health-based global failover.

## The Problem It Solves

Running GPU workloads at scale on Kubernetes is hard:

| Challenge | Without GCO | With GCO |
|-----------|-----------------|--------------|
| GPU availability | Manually check each region | Capacity tools and auto-region workflows compare configured regions |
| Node provisioning | Pre-provision or wait for scaling | EKS Auto Mode provisions on-demand |
| Multi-region | Manage multiple clusters separately | One platform across unlimited SDK-known Regions in one partition |
| Authentication | Configure per-cluster access | IAM-based, works with existing AWS credentials |
| Job outputs | Lost unless persisted | EFS/FSx available for workloads that mount persistent storage |
| Inference serving | Deploy and manage per-region | Deploy once across selected Regions; global failover in `aws` |
| Failover | Manual intervention | Automatic via Global Accelerator in `aws`; explicit regional selection elsewhere |

## Key Concepts

### Multi-Region Architecture

GCO deploys identical infrastructure to multiple AWS Regions in one AWS partition. In the commercial `aws` partition, workload traffic can use this global topology:

```text
                    ┌─────────────────────┐
                    │   Global Endpoint   │
                    │  (API Gateway +     │
                    │  Global Accelerator)│
                    └──────────┬──────────┘
                               │
           ┌───────────────────┼───────────────────┐
           │                   │                   │
    ┌──────▼──────┐     ┌──────▼──────┐     ┌──────▼──────┐
    │  us-east-1  │     │  us-west-2  │     │  eu-west-1  │
    │  EKS + ALB  │     │  EKS + ALB  │     │  EKS + ALB  │
    └─────────────┘     └─────────────┘     └─────────────┘
```

Other AWS partitions omit Global Accelerator and its workload proxy routes. Their global API remains available for aggregate operations, while callers use each selected Region's IAM-authenticated API bridge for control-plane and inference traffic.

Each Region is independent. In `aws`, Global Accelerator routes new requests away from unhealthy registered Regions; elsewhere, clients select another healthy regional endpoint.

### EKS Auto Mode

[EKS Auto Mode](https://docs.aws.amazon.com/eks/latest/userguide/automode.html) is AWS's fully managed Kubernetes compute. Unlike traditional EKS where you manage node groups, Auto Mode:

- **Automatically provisions nodes** when pods are pending
- **Scales workload capacity down** when demand disappears, subject to NodePool and system-workload constraints
- **Handles node updates** and security patches
- **Supports GPU instances** via nodepools

You don't manage EC2 instances directly - you define what you need (CPU, memory, GPU), and EKS Auto Mode handles the rest.

### Nodepools

Nodepools define what types of nodes can be provisioned. GCO creates several:

| NodePool | Purpose | Typical constraints |
|----------|---------|---------------------|
| `system` | EKS Auto Mode system components | AWS-managed built-in |
| `general-purpose` | Standard workloads | AWS-managed built-in |
| `gpu-x86-pool` | NVIDIA x86 GPU workloads | g4dn/g5 and configured GPU families |
| `gpu-arm-pool` | NVIDIA ARM64 GPU workloads | g5g |
| `gpu-inference-pool` | Long-running inference endpoints | conservative disruption/consolidation |
| `gpu-efa-pool` | Distributed GPU workloads | EFA-capable GPU families |
| `mooncake-efa-pool` | Disaggregated Mooncake inference | EFA/RDMA placement |
| `neuron-pool` | Inferentia and Trainium workloads | AWS Neuron devices |
| `cpu-general-pool` | Project-scoped CPU workloads | configured CPU families and limits |

When you submit a job requesting a GPU, EKS Auto Mode finds the right nodepool and provisions an appropriate instance.

### Manifest Submission

A "manifest" is a Kubernetes YAML file describing your workload. GCO accepts manifests via:

1. **SQS Queue** (recommended for production) - Reliable, region-targeted submission
2. **API Gateway** - IAM-authenticated REST APIs: a global workload path in `aws`, aggregate-only globally plus regional workload bridges elsewhere
3. **DynamoDB Job Queue** - Centralized global queue with priority, status tracking, and audit trail
4. **Direct kubectl** - Requires cluster access, good for development

Example manifest:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: my-training-job
  namespace: gco-jobs
spec:
  template:
    spec:
      containers:
      - name: trainer
        image: my-training-image:v1.0.0
        resources:
          limits:
            nvidia.com/gpu: 1
      restartPolicy: Never
```

### Global Routing

[AWS Global Accelerator](https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html) is the backend regional-routing layer behind the global workload API in the commercial `aws` partition:

1. A user sends a SigV4-signed request to the global API Gateway
2. The proxy Lambda adds a request-bound HMAC envelope
3. Global Accelerator evaluates registered internal ALBs using health, endpoint weights, and network proximity
4. If an endpoint is unhealthy, it routes new requests to another healthy registered region

This routing is transparent to the client, but it is not a GPU-capacity scheduler. Use `gco capacity` and auto-region queue/CLI workflows when placement must consider available accelerator capacity. Outside `aws`, these accelerator resources and workload routes are omitted; clients call an authorized regional bridge and choose failover explicitly.

## Inference Serving

Beyond batch GPU jobs, GCO supports long-running inference endpoints — deploy a model once and serve it globally across regions. The platform handles reconciliation, model weight syncing, routing, and scaling.

### How Inference Works

Inference serving uses a reconciliation pattern similar to Kubernetes controllers:

1. You run `gco inference deploy my-llm -i vllm/vllm-openai:v0.25.1 --gpu-count 1`
2. The CLI writes the endpoint spec to a DynamoDB table (desired state)
3. An `inference_monitor` service running in each target region polls the table
4. The monitor creates Kubernetes Deployments, Services, scaling objects, and supporting configuration to match the desired state
5. The shared Gateway API `HTTPRoute` sends authenticated `/inference/*` traffic to the dedicated inference proxy, which validates the route and streams from the endpoint Service; per-endpoint public routes are not created
6. If anything drifts (pod deleted, resource missing), the monitor self-heals by recreating it
7. In `aws`, Global Accelerator routes proxy-signed requests to a healthy Region; elsewhere, callers invoke the selected regional API

```text
Control plane:
  gco inference deploy → DynamoDB desired state
                              │
                              ├─→ us-east-1 monitor → Deployment + Service
                              └─→ eu-west-1 monitor → Deployment + Service

Request path in commercial `aws`:
  Client → API Gateway streaming Lambda → Global Accelerator → Internal ALB
                                                   │
                                                   ▼
                                      Authenticated inference proxy
                                                   │
                                                   ▼
                                        Endpoint Service → model pods

Request path in other partitions:
  Client → Regional API Gateway → streaming VPC Lambda → Internal ALB
```

All inference endpoints share the same ALB as the main GCO services—one ALB per Region and cost-efficient. In `aws`, that ALB is registered with Global Accelerator; in other partitions it is reached by the regional VPC proxies.

Key capabilities:

- Deploy to one or all regions with a single command
- Rolling updates, canary deployments (A/B testing), stop/start
- Automatic model weight sync from S3 via init containers
- Autoscaling via HPA (CPU/memory metrics)
- Spot instance support for significant cost savings

### Supported Frameworks

GCO works with any containerized inference server. These have example manifests in `examples/`:

| Framework | Use Case | Example |
|-----------|----------|---------|
| vLLM | OpenAI-compatible LLM serving | `examples/inference-vllm.yaml` |
| SGLang | High-throughput serving with RadixAttention | `examples/inference-sglang.yaml` |
| TGI | HuggingFace optimized inference | `examples/inference-tgi.yaml` |
| Triton | Multi-framework model serving | `examples/inference-triton.yaml` |
| TorchServe | PyTorch native serving | `examples/inference-torchserve.yaml` |

See [Inference Guide](INFERENCE.md) for the full deep dive including model weight management, canary deployments, and production EFA setup.

## Storage Options

### EFS (Elastic File System)

EFS is a shared file system accessible by all pods in a cluster. Use it for:

- Job outputs that need to persist after pod termination
- Sharing data between pods
- Checkpoint storage for training jobs

```yaml
volumes:
- name: shared-storage
  persistentVolumeClaim:
    claimName: gco-shared-storage
```

**Characteristics:**

- Elastic (grows/shrinks automatically)
- Lower throughput than FSx
- Pay only for what you use
- Good for general-purpose storage

### FSx for Lustre

FSx for Lustre is a high-performance parallel file system. Use it for:

- Large dataset training (high throughput needed)
- Distributed training across multiple nodes
- Workloads with heavy I/O requirements

```yaml
volumes:
- name: fsx-storage
  persistentVolumeClaim:
    claimName: gco-fsx-storage
```

**Characteristics:**

- Very high throughput (hundreds of GB/s possible)
- Fixed capacity (must pre-provision)
- Higher cost than EFS
- Best for ML training workloads

**When to use which:**

| Use Case | Recommended |
|----------|-------------|
| Job logs and small outputs | EFS |
| Model checkpoints | EFS |
| Large dataset training | FSx for Lustre |
| Distributed training | FSx for Lustre |
| Cost-sensitive workloads | EFS |

## Security Model

GCO uses multiple security layers:

```text
┌─────────────────────────────────────────────────────────┐
│ Layer 1: IAM Authentication                             │
│ - API Gateway validates AWS credentials (SigV4)         │
│ - Users need execute-api:Invoke permission              │
└─────────────────────────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────┐
│ Layer 2: Request-Bound HMAC                             │
│ - Lambda signs method, target, timestamp, nonce, body   │
│ - Rotating key remains in trusted components            │
└─────────────────────────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────┐
│ Layer 3: Authenticated Backend TLS                      │
│ - Private-root TLS with explicit SNI/hostname checks    │
│ - Global Accelerator passes TCP/443 in `aws`; regional  │
│   VPC proxies connect directly in other partitions      │
└─────────────────────────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────┐
│ Layer 4: Network Isolation                              │
│ - Regional platform ALBs are internal                   │
│ - EKS nodes and service endpoints use private subnets   │
└─────────────────────────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────┐
│ Layer 5: Kubernetes RBAC                                │
│ - Service accounts with least-privilege                 │
│ - Namespace isolation for user jobs                     │
└─────────────────────────────────────────────────────────┘
```

**Key points:**

- All public requests must be signed with AWS credentials
- Backend TLS clients trust only the deployment's public root bundle and assert `backend.<project>.gco.internal`
- The root private key remains in a customer-managed-KMS-encrypted secret readable only by the certificate manager
- Backend requests must carry a fresh, integrity-bound HMAC envelope; HMAC is not encryption
- Reused nonces, stale timestamps, body changes, and target changes are rejected
- Jobs run in the isolated `gco-jobs` namespace
- Platform services run in `gco-system` namespace

## API Access Modes

GCO supports two API access modes:

### Global API (Default)

In the commercial `aws` partition, the default workload mode routes requests
through the edge-optimized global API Gateway and Global Accelerator to an
internal ALB. AWS-managed TLS protects the client hop; the proxy then uses
deployment-local private-root TLS through the Layer 4 accelerator to the ALB:

```text
User → Global API Gateway → HMAC-signing Lambda → Global Accelerator (TCP/443)
  → Internal Regional ALB (private-root TLS) → EKS pod (HTTP)
```

Outside `aws`, this global workload route is not created; the global API is regional and aggregate-only, and workload traffic uses the regional mode below.

**Pros in `aws`:**

- Single IAM-authenticated endpoint
- Health-based failover between registered regions
- AWS-managed edge termination and network acceleration (dynamic API responses are not CloudFront-cached)
- No public ALB exposure

**Cons:**

- One shared API Gateway stage throttle and proxy hop
- Global Accelerator selects by health/traffic policy, not GPU inventory

### Regional API Bridge and Direct Access

Every region has a regional API Gateway bridge so the centralized aggregator
can reach its private ALB. Aggregator fan-out uses AWS-managed TLS and SigV4:

```text
Aggregator → Regional API Gateway → HMAC-signing VPC Lambda
  → Internal Regional ALB (private-root TLS) → EKS pod (HTTP)
```

The bridge resource policy always admits the exact aggregator role. In the
commercial `aws` partition, `regional_api_enabled=true` additionally allows
IAM-authorized principals from the deployment account to invoke the
region-pinned endpoint directly. In other partitions that same-account policy
is enabled automatically because Global Accelerator is omitted:

```text
User (optional in `aws`; required ingress elsewhere) → Regional API Gateway → HMAC-signing VPC Lambda
  → Internal Regional ALB (private-root TLS) → EKS pod (HTTP)
```

**Pros:**

- Direct, explicitly selected regional route
- Same IAM and request-bound HMAC model as the global path
- Useful for deterministic regional operations

**Cons:**

- Separate endpoint per region
- No automatic cross-region failover on a directly invoked endpoint
- Direct callers require the explicit resource-policy opt-in in `aws`; other partitions enable it automatically

**Enable Direct Regional Access in `aws`:**

```json
// cdk.json
{
  "api_gateway": {
    "regional_api_enabled": true
  }
}
```

**Use Regional APIs:**

```bash
# CLI flag
gco --regional-api jobs list --region us-east-1

# Or environment variable
export GCO_REGIONAL_API=true
gco jobs list --region us-east-1
```

## How Components Work Together

Here's what happens when you submit a job:

```text
1. You run: gco jobs submit my-job.yaml

2. CLI signs request with your AWS credentials (SigV4)
   └─► API Gateway validates your IAM permissions

3. Lambda proxy signs the exact backend request
   └─► Binds timestamp, nonce, method, target, and body digest without sending the key

4. In `aws`, private-root TLS traverses Global Accelerator to a healthy regional ALB; elsewhere, a regional VPC proxy connects directly
   └─► Global Accelerator forwards TCP/443 without termination when present

5. The ALB terminates TLS and forwards HTTP to the service
   └─► Backend middleware verifies freshness, integrity, and nonce replay

6. Manifest Processor pod processes the job
   └─► Validates YAML, applies to Kubernetes

7. Kubernetes scheduler sees pending pod
   └─► Finds appropriate nodepool

8. EKS Auto Mode provisions node (if needed)
   └─► Launches EC2 instance matching requirements

9. Pod runs on provisioned node
   └─► Your job executes

10. Job completes, outputs saved to EFS/FSx
    └─► Data persists after pod terminates
```

## Common Workflows

### Submit a Simple Job

```bash
# Check what regions are available
gco capacity status

# Submit to a specific region
gco jobs submit-sqs my-job.yaml --region us-east-1

# Or let GCO pick the best region
gco jobs submit-sqs my-job.yaml --auto-region

# Check job status
gco jobs list --all-regions

# Get logs
gco jobs logs my-job -n gco-jobs -r us-east-1
```

### Run a GPU Training Job

```bash
# Check GPU capacity
gco capacity check --instance-type g5.xlarge --region us-east-1

# Submit GPU job
gco jobs submit-sqs examples/gpu-job.yaml --region us-east-1

# Monitor
gco jobs list -r us-east-1 -n gco-jobs
```

### Save Job Outputs

```bash
# Submit job that writes to EFS
gco jobs submit-direct examples/efs-output-job.yaml -r us-east-1

# Wait for completion
gco jobs list -r us-east-1 -n gco-jobs

# Download outputs (works even after pod is deleted)
gco files download my-job-outputs ./local-dir -r us-east-1
```

### Submit via Global Job Queue

Use the DynamoDB-backed queue when you need centralized tracking and status
history. Replace `<JOB_ID>` with a queue job ID from `gco queue list`:

```bash
# Submit to queue targeting a region
gco queue submit my-job.yaml --region us-east-1

# Track status
gco queue list --status running
gco queue get <JOB_ID>

# View queue statistics
gco queue stats
```

### Deploy an Inference Endpoint

GCO supports long-running inference endpoints across regions with automatic reconciliation:

```bash
# Deploy a vLLM endpoint
gco inference deploy my-llm -i vllm/vllm-openai:v0.25.1 --gpu-count 1

# Check status
gco inference status my-llm

# Send a prompt
gco inference invoke my-llm -p "Hello, world"
```

See [Inference Guide](INFERENCE.md) for the full guide including model weight management and canary deployments.

---

**Next Steps:**

- [Learning Path](LEARNING_PATH.md) - Follow the guided, staged path from here to productive
- [Quick Start Guide](../QUICKSTART.md) - Get running in under 60 minutes
- [Architecture Details](ARCHITECTURE.md) - Deep dive into the system
- [CLI Reference](CLI.md) - Complete command documentation
