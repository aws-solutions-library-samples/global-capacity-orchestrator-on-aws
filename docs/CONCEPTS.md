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
- [How Components Work Together](#how-components-work-together)
- [Common Workflows](#common-workflows)

## What is GCO?

GCO is a **multi-region Kubernetes platform** built on AWS EKS Auto Mode, designed specifically for AI/ML workloads that need GPU compute. It provides:

- A single API endpoint that routes jobs to the best available region
- Automatic GPU node provisioning (no manual scaling)
- Inference endpoint management across regions with a single command
- Shared storage for job outputs that persists after pods terminate
- Production-ready security with IAM authentication

Think of it as a "GPU job submission service" — you submit a Kubernetes manifest, and GCO handles finding capacity, provisioning nodes, running your job, and storing outputs. For inference, you deploy an endpoint once and GCO serves it globally with automatic failover.

## The Problem It Solves

Running GPU workloads at scale on Kubernetes is hard:

| Challenge | Without GCO | With GCO |
|-----------|-----------------|--------------|
| GPU availability | Manually check each region | Auto-routes to available capacity |
| Node provisioning | Pre-provision or wait for scaling | EKS Auto Mode provisions on-demand |
| Multi-region | Manage multiple clusters separately | Single API, automatic routing |
| Authentication | Configure per-cluster access | IAM-based, works with existing AWS credentials |
| Job outputs | Lost when pods terminate | Persisted to EFS/FSx storage |
| Inference serving | Deploy and manage per-region | Deploy once, serve globally with auto-failover |
| Failover | Manual intervention | Automatic via Global Accelerator |

## Key Concepts

### Multi-Region Architecture

GCO deploys identical infrastructure to multiple AWS regions:

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

Each region is independent - if one region has issues, traffic automatically routes to healthy regions.

### EKS Auto Mode

EKS Auto Mode is AWS's fully managed Kubernetes compute. Unlike traditional EKS where you manage node groups, Auto Mode:

- **Automatically provisions nodes** when pods are pending
- **Scales to zero** when no workloads are running (cost savings)
- **Handles node updates** and security patches
- **Supports GPU instances** via nodepools

You don't manage EC2 instances directly - you define what you need (CPU, memory, GPU), and EKS Auto Mode handles the rest.

### Nodepools

Nodepools define what types of nodes can be provisioned. GCO creates several:

| nodepool | Purpose | Instance Types |
|----------|---------|----------------|
| `system` | Kubernetes system components | Managed by EKS |
| `general-purpose` | Standard workloads | Various CPU instances |
| `gpu-x86` | NVIDIA GPU workloads | g4dn, g5 (T4, A10G GPUs) |
| `gpu-arm` | ARM64 GPU workloads | g5g (T4g GPUs) |
| `inference` | Long-running inference endpoints | Same as gpu-x86, WhenEmpty consolidation |
| `gpu-efa-pool` | Distributed training and high-performance inference | p4d, p5 (A100, H100 with EFA) |

When you submit a job requesting a GPU, EKS Auto Mode finds the right nodepool and provisions an appropriate instance.

### Manifest Submission

A "manifest" is a Kubernetes YAML file describing your workload. GCO accepts manifests via:

1. **SQS Queue** (recommended for production) - Reliable, region-targeted submission
2. **API Gateway** - IAM-authenticated REST API with global routing
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
        image: my-training-image:latest
        resources:
          limits:
            nvidia.com/gpu: 1
      restartPolicy: Never
```

### Global Routing

AWS Global Accelerator provides a single endpoint that routes to the nearest healthy region:

1. User submits job to global endpoint
2. Global Accelerator checks health of each region
3. Routes to nearest healthy region (lowest latency)
4. If a region fails, automatically routes to next-best region

This happens transparently - you always use the same endpoint.

## Inference Serving

Beyond batch GPU jobs, GCO supports long-running inference endpoints — deploy a model once and serve it globally across regions. The platform handles reconciliation, model weight syncing, routing, and scaling.

### How Inference Works

Inference serving uses a reconciliation pattern similar to Kubernetes controllers:

1. You run `gco inference deploy my-llm -i vllm/vllm-openai:v0.22.0 --gpu-count 1`
2. The CLI writes the endpoint spec to a DynamoDB table (desired state)
3. An `inference_monitor` service running in each target region polls the table
4. The monitor creates Kubernetes Deployments, Services, and Ingress rules to match the desired state
5. If anything drifts (pod deleted, resource missing), the monitor self-heals by recreating it
6. Global Accelerator routes user requests to the nearest healthy region

```text
gco inference deploy
        │
        ▼
  DynamoDB (desired state)
        │
        ▼ (each region polls)
  ┌──────────┐    ┌──────────┐
  │us-east-1 │    │eu-west-1 │
  │Deployment│    │Deployment│
  │Service   │    │Service   │
  │Ingress   │    │Ingress   │
  └────┬─────┘    └────┬─────┘
       └──────┬────────┘
              ▼
     Global Accelerator
              │
         End Users
```

All inference endpoints share the same ALB as the main GCO services — one ALB per region, cost-efficient, and already registered with Global Accelerator.

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
│ Layer 2: Secret Header                                  │
│ - Lambda adds secret token from Secrets Manager         │
│ - Token rotates daily (zero-downtime)                   │
└─────────────────────────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────┐
│ Layer 3: Network Isolation                              │
│ - ALBs only accept Global Accelerator IPs               │
│ - EKS runs in private subnets                           │
└─────────────────────────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────┐
│ Layer 4: Kubernetes RBAC                                │
│ - Service accounts with least-privilege                 │
│ - Namespace isolation for user jobs                     │
└─────────────────────────────────────────────────────────┘
```

**Key points:**

- All requests must be signed with AWS credentials
- No anonymous access
- Jobs run in isolated `gco-jobs` namespace
- Platform services run in `gco-system` namespace

## API Access Modes

GCO supports two API access modes:

### Global API (Default)

The default mode routes all requests through a global API Gateway and Global Accelerator:

```text
User → Global API Gateway → Global Accelerator → Regional ALB → EKS
```

**Pros:**

- Single global endpoint
- Automatic failover between regions
- Edge caching via CloudFront

**Cons:**

- Requires public ALB exposure
- Traffic routes through Global Accelerator

### Regional API (Private Access)

When public access is disabled, regional API Gateways provide direct access via VPC Lambdas:

```text
User → Regional API Gateway → VPC Lambda → Internal ALB → EKS
```

**Pros:**

- No public ALB exposure
- Direct regional access
- Maximum security posture

**Cons:**

- Must specify target region
- No automatic cross-region failover

**Enable Regional APIs:**

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

3. Lambda proxy adds secret header
   └─► Ensures request came through authenticated path

4. Global Accelerator routes to nearest healthy region
   └─► Checks ALB health in each region

5. Regional ALB receives request
   └─► Validates it came from Global Accelerator

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
gco inference deploy my-llm -i vllm/vllm-openai:v0.22.0 --gpu-count 1

# Check status
gco inference status my-llm

# Send a prompt
gco inference invoke my-llm -p "Hello, world"
```

See [Inference Guide](INFERENCE.md) for the full guide including model weight management and canary deployments.

---

**Next Steps:**

- [Quick Start Guide](../QUICKSTART.md) - Get running in under 60 minutes
- [Architecture Details](ARCHITECTURE.md) - Deep dive into the system
- [CLI Reference](CLI.md) - Complete command documentation
