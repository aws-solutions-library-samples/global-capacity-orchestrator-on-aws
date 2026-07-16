# Kubernetes Manifest Examples

This directory contains example Kubernetes manifests you can use with GCO (Global Capacity Orchestrator on AWS). Each example is self-contained and ready to submit.

> **New to GCO?** Follow the [Learning Path](../docs/LEARNING_PATH.md) — it sequences these examples into a staged, hands-on onboarding curriculum.

## Table of Contents

- [Quick Reference](#quick-reference)
- [Examples](#examples)
  - [Analytics Database Export Job](#analytics-database-export-job)
  - [Analytics S3 Upload Job](#analytics-s3-upload-job)
  - [Aurora pgvector Job](#aurora-pgvector-job)
  - [Cluster Shared Bucket Upload Job](#cluster-shared-bucket-upload-job)
  - [DAG Pipeline](#dag-pipeline)
  - [EFA Distributed Training](#efa-distributed-training)
  - [EFS Output Job](#efs-output-job)
  - [FSx for Lustre Job](#fsx-for-lustre-job)
  - [GPU Job](#gpu-job)
  - [GPU Time-Slicing Job](#gpu-time-slicing-job)
  - [Inference Frameworks](#inference-frameworks)
  - [Inferentia Job](#inferentia-job)
  - [KEDA Autoscaled Job](#keda-autoscaled-job)
  - [Kueue Job Queueing](#kueue-job-queueing)
  - [MegaTrain SFT Job](#megatrain-sft-job)
  - [Mission Semantic-Progress Judge](#mission-semantic-progress-judge)
  - [Mission Training-Loss Observation](#mission-training-loss-observation)
  - [Model Download Job](#model-download-job)
  - [Multi-GPU Distributed Training](#multi-gpu-distributed-training)
  - [Ray Cluster](#ray-cluster)
  - [Simple Job](#simple-job)
  - [Slurm Cluster Job](#slurm-cluster-job)
  - [SQS Job Submission](#sqs-job-submission)
  - [Trainium Job](#trainium-job)
  - [Valkey Cache Job](#valkey-cache-job)
  - [Volcano Gang Scheduling](#volcano-gang-scheduling)
  - [YuniKorn Hierarchical Queues](#yunikorn-hierarchical-queues)
- [Customizing Examples](#customizing-examples)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)
- [Testing Your Manifests](#testing-your-manifests)
- [Cleaning Up](#cleaning-up)
- [Additional Resources](#additional-resources)

## Quick Reference

| Example | File | Category | GPU | Opt-in |
|---------|------|----------|-----|--------|
| [Analytics Database Export](#analytics-database-export-job) | `analytics-database-export-job.yaml` | Analytics | — | Aurora, Analytics |
| [Analytics S3 Upload](#analytics-s3-upload-job) | `analytics-s3-upload-job.yaml` | Analytics | — | Analytics |
| [Aurora pgvector](#aurora-pgvector-job) | `aurora-pgvector-job.yaml` | Database | — | Aurora |
| [Cluster Shared Bucket Upload](#cluster-shared-bucket-upload-job) | `cluster-shared-bucket-upload-job.yaml` | Storage | — | — |
| [DAG Preprocess](#dag-pipeline) | `dag-step-preprocess.yaml` | Pipeline | — | — |
| [DAG Train](#dag-pipeline) | `dag-step-train.yaml` | Pipeline | — | — |
| [EFA Training](#efa-distributed-training) | `efa-distributed-training.yaml` | Jobs | ✅ | — |
| [EFS Output](#efs-output-job) | `efs-output-job.yaml` | Storage | — | — |
| [FSx Lustre](#fsx-for-lustre-job) | `fsx-lustre-job.yaml` | Storage | — | FSx |
| [GPU Job](#gpu-job) | `gpu-job.yaml` | Jobs | ✅ | — |
| [GPU Time-Slicing](#gpu-time-slicing-job) | `gpu-timeslicing-job.yaml` | Jobs | ✅ | ConfigMap |
| [Inferentia](#inferentia-job) | `inferentia-job.yaml` | Accelerator | Inferentia | — |
| [SGLang](#inference-frameworks) | `inference-sglang.yaml` | Inference | ✅ | — |
| [TGI](#inference-frameworks) | `inference-tgi.yaml` | Inference | ✅ | — |
| [TorchServe](#inference-frameworks) | `inference-torchserve.yaml` | Inference | ✅ | — |
| [Triton](#inference-frameworks) | `inference-triton.yaml` | Inference | ✅ | — |
| [vLLM](#inference-frameworks) | `inference-vllm.yaml` | Inference | ✅ | — |
| [KEDA Scaled](#keda-autoscaled-job) | `keda-scaled-job.yaml` | Scheduler | — | — |
| [Kueue](#kueue-job-queueing) | `kueue-job.yaml` | Scheduler | Optional | — |
| [MegaTrain SFT](#megatrain-sft-job) | `megatrain-sft-job.yaml` | Jobs | ✅ | — |
| [Mission Semantic-Progress](#mission-semantic-progress-judge) | `mission-semantic-progress-criteria.json` | Mission | — | Mission, Semantic-Progress |
| [Mission Training-Loss](#mission-training-loss-observation) | `mission-training-loss-criteria.json`, `megatrain-trainer-state.json` | Mission | — | Mission |
| [Model Download](#model-download-job) | `model-download-job.yaml` | Jobs | — | — |
| [Multi-GPU Training](#multi-gpu-distributed-training) | `multi-gpu-training.yaml` | Jobs | ✅ | — |
| [Pipeline DAG](#dag-pipeline) | `pipeline-dag.yaml` | Pipeline | — | — |
| [Ray Cluster](#ray-cluster) | `ray-cluster.yaml` | Distributed | — | — |
| [Simple Job](#simple-job) | `simple-job.yaml` | Jobs | — | — |
| [Slurm](#slurm-cluster-job) | `slurm-cluster-job.yaml` | Scheduler | — | Slurm |
| [SQS Submission](#sqs-job-submission) | `sqs-job-submission.yaml` | Jobs | Optional | — |
| [Trainium](#trainium-job) | `trainium-job.yaml` | Accelerator | Trainium | — |
| [Valkey Cache](#valkey-cache-job) | `valkey-cache-job.yaml` | Caching | — | Valkey |
| [Volcano Gang](#volcano-gang-scheduling) | `volcano-gang-job.yaml` | Scheduler | — | — |
| [YuniKorn](#yunikorn-hierarchical-queues) | `yunikorn-job.yaml` | Scheduler | — | YuniKorn |

**Legend:** GPU = requires GPU/accelerator nodes. Opt-in = requires enabling a feature in `cdk.json` before deploying.

---

## Examples

### Analytics Database Export Job

**File:** `analytics-database-export-job.yaml`

Exports rows from the regional Aurora Serverless v2 PostgreSQL cluster (pgvector) to the always-on `Cluster_Shared_Bucket` as CSV, so a SageMaker Studio notebook can analyse the export without direct database credentials. An init container checks for the `gco-aurora-pgvector` ConfigMap and exits 0 when Aurora is not deployed, making the job a safe no-op on clusters without Aurora.

**Prerequisites:**

- `aurora_pgvector.enabled = true` in `cdk.json`, then `gco stacks deploy gco-us-east-1`
- Optional (for the notebook reader): `gco analytics enable` and `gco stacks deploy gco-analytics`

**Usage:**

```bash
gco jobs submit-direct examples/analytics-database-export-job.yaml -r us-east-1
```

**When to use:** Moving database snapshots to S3 for notebook-side analysis, decoupling notebooks from direct database connections, building RAG/pgvector export pipelines.

---

### Analytics S3 Upload Job

**File:** `analytics-s3-upload-job.yaml`

Publishes a small dataset snapshot (CSV plus a JSON schema manifest) to the always-on `Cluster_Shared_Bucket` under the `analytics-data/` prefix. A SageMaker Studio notebook running under the analytics execution role can then read it with a short `boto3 + pandas` snippet — printed at the end of the job.

**Prerequisites:**

- `gco analytics enable` — sets `analytics_environment.enabled = true` in `cdk.json`
- `gco stacks deploy gco-analytics` — provisions the Studio domain, Cognito user pool, and the execution role that can read the shared bucket
- `gco analytics users add --username <USERNAME>` — creates a Cognito user and Studio user profile (substitute `<USERNAME>` with the username to create)

**Usage:**

```bash
gco jobs submit-direct examples/analytics-s3-upload-job.yaml -r us-east-1
```

**When to use:** Handing off cluster-side artifacts to Studio notebooks, publishing dataset snapshots for exploratory analysis, wiring up the analytics end-to-end demo.

---

### Aurora pgvector Job

**File:** `aurora-pgvector-job.yaml`

Connects to the regional Aurora Serverless v2 PostgreSQL cluster with pgvector for vector similarity search (RAG, semantic search). Credentials are injected automatically via the `gco-aurora-pgvector` ConfigMap and AWS Secrets Manager.

**Prerequisites:** Enable Aurora in `cdk.json`: `"aurora_pgvector": { "enabled": true }` and redeploy.

**Usage:**

```bash
gco jobs submit-direct examples/aurora-pgvector-job.yaml -r us-east-1
```

**Demonstrates:** pgvector extension setup, HNSW index creation, vector similarity search, embedding storage and retrieval.

**When to use:** RAG applications, semantic search, vector similarity workloads, storing and querying embeddings.

---

### Cluster Shared Bucket Upload Job

**File:** `cluster-shared-bucket-upload-job.yaml`

Writes a small JSON artifact to the always-on `Cluster_Shared_Bucket` using `envFrom` against the `gco-cluster-shared-bucket` ConfigMap. The bucket's name, ARN, and region are exposed directly as env vars (`sharedBucketName`, `sharedBucketArn`, `sharedBucketRegion`) — no hardcoded bucket names. Works regardless of whether the analytics environment is enabled: the shared bucket is always on.

**Cross-region caveat:** the shared bucket lives in the global region (default `us-east-2`) where `GCOGlobalStack` is deployed, not the regional cluster's own region. The boto3 client in the example is pointed at `sharedBucketRegion` so requests go to the bucket's home region. Expect small cross-region egress charges on large uploads.

**Prerequisites:** none — `Cluster_Shared_Bucket` is always provisioned with `gco stacks deploy gco-global`.

**Usage:**

```bash
gco jobs submit-direct examples/cluster-shared-bucket-upload-job.yaml -r us-east-1
```

**When to use:** Any cluster job that needs a project-wide artifact store — ETL outputs, batch training checkpoints, cron dumps, hand-off to downstream analytics.

---

### DAG Pipeline

**Files:** `pipeline-dag.yaml`, `dag-step-preprocess.yaml`, `dag-step-train.yaml`

A multi-step pipeline with dependency ordering. The preprocess step generates training data on shared EFS, then the train step reads it and produces model artifacts. Steps only run after their dependencies complete successfully.

**Usage:**

```bash
# Validate the DAG
gco dag validate examples/pipeline-dag.yaml

# Dry run (shows execution plan without running)
gco dag run examples/pipeline-dag.yaml --dry-run

# Run the pipeline
gco dag run examples/pipeline-dag.yaml -r us-east-1
```

**When to use:** Multi-step ML pipelines, data processing workflows with dependencies, any workload where steps must execute in order.

---

### EFA Distributed Training

**File:** `efa-distributed-training.yaml`

Uses Elastic Fabric Adapter (EFA) for high-bandwidth inter-node communication — up to 3.2 Tbps on P5/Trn2 instances and up to 28.8 Tbps on P6e-GB200 UltraServers. Critical for large-scale distributed training.

**Usage:**

```bash
gco jobs submit-direct examples/efa-distributed-training.yaml -r us-east-1
```

**Requirements:** EFA-capable instances (p4d.24xlarge, p5.48xlarge, p5e.48xlarge, p6-b200.48xlarge, p6-b300.48xlarge, p6e-gb200.*, trn1.32xlarge, trn2.48xlarge). EFA is enabled by default via the AWS EFA Kubernetes device plugin.

**When to use:** Large-scale distributed training requiring high inter-node bandwidth, NCCL-based multi-node GPU communication.

---

### EFS Output Job

**File:** `efs-output-job.yaml`

Writes output to shared EFS storage, demonstrating how to persist job results that survive pod termination.

**Usage:**

```bash
gco jobs submit-direct examples/efs-output-job.yaml --region us-east-1 -n gco-jobs
gco files ls -r us-east-1
gco files download efs-output-example ./efs-results -r us-east-1
cat ./efs-results/results.json
```

**Features:** Mounts shared EFS at `/outputs`, creates output directory using job name, results persist after job completion, other pods can read the outputs.

**When to use:** ML training with checkpoint saving, data processing pipelines, sharing data between jobs.

---

### FSx for Lustre Job

**File:** `fsx-lustre-job.yaml`

Uses FSx for Lustre high-performance parallel storage for I/O-intensive workloads.

**Prerequisites:**

```bash
gco stacks fsx enable -y
gco stacks deploy gco-us-east-1 -y
```

**Usage:**

```bash
gco jobs submit-direct examples/fsx-lustre-job.yaml --region us-east-1 -n gco-jobs
gco files download fsx-lustre-example ./fsx-results -r us-east-1 -t fsx
```

**FSx vs EFS:**

| Feature | EFS | FSx for Lustre |
|---------|-----|----------------|
| Throughput | Up to 10 GB/s | Up to 1000+ GB/s |
| Latency | ~1-3ms | ~sub-ms |
| Cost | Pay per GB stored | Pay per GB provisioned |
| Best for | General purpose | HPC, ML training |

**When to use:** Large-scale ML training with big datasets, HPC workloads, jobs requiring high I/O throughput, checkpoint/restart for long-running jobs.

---

### GPU Job

**File:** `gpu-job.yaml`

A job that requests GPU resources and runs on GPU-enabled nodes.

**Usage:**

```bash
gco jobs submit-sqs examples/gpu-job.yaml --region us-east-1
kubectl logs job/gpu-test-job
```

**Requirements:** GPU nodepools (included by default), NVIDIA device plugin (included by default).

**When to use:** ML model training, GPU-accelerated workloads, testing GPU availability.

---

### GPU Time-Slicing Job

**File:** `gpu-timeslicing-job.yaml`

Uses a fractional GPU via NVIDIA time-slicing. Multiple pods share a single physical GPU by taking turns, letting you run lightweight GPU workloads without dedicating a full GPU to each pod.

**Usage:**

```bash
kubectl apply -f examples/gpu-timeslicing-job.yaml
kubectl logs job/gpu-timeslice-job -n gco-jobs
```

**Requirements:** GPU nodepools (default), NVIDIA device plugin with time-slicing ConfigMap applied (not enabled by default — see manifest comments for setup).

**When to use:** Inference workloads that don't need a full GPU, dev/test GPU workloads, reducing GPU costs by sharing hardware.

---

### Inference Frameworks

GCO includes example manifests for multiple inference frameworks. Each creates a Deployment and Service in the `gco-inference` namespace.

| Example | Framework | Description | Docs |
|---------|-----------|-------------|------|
| `inference-sglang.yaml` | SGLang | High-throughput serving with RadixAttention | [Inference Guide](../docs/INFERENCE.md) |
| `inference-tgi.yaml` | TGI | HuggingFace optimized inference | [Inference Guide](../docs/INFERENCE.md) |
| `inference-torchserve.yaml` | TorchServe | PyTorch model serving | [Inference Guide](../docs/INFERENCE.md) |
| `inference-triton.yaml` | Triton | NVIDIA multi-framework serving (PyTorch, TensorFlow, ONNX) | [Inference Guide](../docs/INFERENCE.md) |
| `inference-vllm.yaml` | vLLM | OpenAI-compatible LLM serving with PagedAttention | [Inference Guide](../docs/INFERENCE.md) |

**Deploy via CLI (recommended for multi-region):**

```bash
gco inference deploy my-llm -i vllm/vllm-openai:v0.24.0 --gpu-count 1
```

**Deploy a manifest directly (single region):**

```bash
gco jobs submit-direct examples/inference-vllm.yaml -r us-east-1
```

See the [Inference Guide](../docs/INFERENCE.md) for canary deployments, scaling, health checks, and multi-region routing.

---

### Inferentia Job

**File:** `inferentia-job.yaml`

Runs on an AWS Inferentia2 instance using the Neuron SDK. Inferentia is optimized for low-cost, high-throughput inference.

**Usage:**

```bash
gco jobs submit examples/inferentia-job.yaml --region us-east-1
gco jobs logs inferentia-test -r us-east-1
```

**Requirements:** Neuron device plugin (installed by default), Neuron nodepool (applied by default), container images built with the Neuron SDK (not CUDA).

**When to use:** Cost-optimized inference serving, high-throughput batch inference, deploying Neuron-compiled models.

---

### KEDA Autoscaled Job

**File:** `keda-scaled-job.yaml`

A safe, non-consuming SQS scaling demonstration using KEDA. It scales credentialless observer Jobs from a **disposable demo queue's** depth, but the Jobs never call `ReceiveMessage` or `DeleteMessage`. GCO ships a separate built-in consumer for real manifest submissions.

**Usage:**

```bash
# Replace the explicit disposable demo queue URL/region, then:
kubectl apply -f examples/keda-scaled-job.yaml
```

**Safety:** Do not point this demonstration at GCO's `JobQueueUrl`; because it intentionally never drains messages, real submissions would repeatedly trigger observer Jobs.

**Requirements:** KEDA (enabled by default), a disposable demo queue, and read-only KEDA metric access to that queue. See [KEDA docs](../docs/KEDA.md).

**When to use:** Learning how SQS queue depth maps to KEDA Jobs. Use the built-in queue processor—or a fully implemented custom consumer—for actual message processing.

---

### Kueue Job Queueing

**File:** `kueue-job.yaml`

Demonstrates job queueing with resource quotas and fair-sharing. Creates ClusterQueue, LocalQueue, ResourceFlavors, and sample CPU + GPU jobs. Jobs are queued and scheduled based on available cluster resources.

**Usage:**

```bash
kubectl apply -f examples/kueue-job.yaml
kubectl get clusterqueue
kubectl get localqueue -n gco-jobs
kubectl get workloads -n gco-jobs
```

**Requirements:** Kueue (enabled by default). See [Kueue docs](../docs/KUEUE.md).

**When to use:** Multi-tenant clusters with resource quotas, fair-sharing between teams, priority-based job scheduling.

---

### MegaTrain SFT Job

**File:** `megatrain-sft-job.yaml`

Runs SFT fine-tuning of Qwen2.5-1.5B on a single GPU using [MegaTrain](https://github.com/DLYuanGod/MegaTrain). An init container downloads model weights to shared EFS (skipped if already cached), then the main container trains on the built-in alpaca demo dataset. Change the `MODEL_NAME` env var to target a different HuggingFace model.

**Usage:**

```bash
gco jobs submit-direct examples/megatrain-sft-job.yaml -r us-east-1
gco jobs logs megatrain-sft -r us-east-1
```

**Requirements:** GPU node with large CPU RAM, shared EFS storage.

**When to use:** SFT fine-tuning of large HuggingFace models, full-precision training on a single GPU.

---

### Mission Semantic-Progress Judge

**File:** `mission-semantic-progress-criteria.json`

Drives a [Mission](../docs/MISSION.md) with an **LLM-as-judge** progress signal instead of a metric scraped from a file. The read-only `metrics_semantic_progress` tool scores how close a session is to satisfying its directive against a fixed, versioned rubric, then emits that score in the canonical `{"metrics": {"progress_score": <number>}}` shape Mission's Observe phase merges. A plain `metric_threshold` criterion then reads `metrics.progress_score` by dot-path with no special handling — the comparison is byte-for-byte the same one the engine performs on any numeric metric.

The score is a finite float in the closed range `[0.0, 1.0]` (`0.0` = no progress toward the directive, `1.0` = directive fully satisfied). All non-determinism is confined to the single LLM call inside the tool — exactly like a CloudWatch read varying between calls — while the criterion comparison over the score stays fully deterministic.

- `mission-semantic-progress-criteria.json` is a one-criterion [Criteria File](../docs/MISSION.md#criteria-file-schema) asserting `metrics.progress_score >= 0.8`.

**Prerequisites:**

- `GCO_ENABLE_SEMANTIC_PROGRESS=true` (or the umbrella `GCO_ENABLE_ALL_TOOLS=true`) — the judge tool is **gated default-off** because each invocation incurs an LLM call (a Bedrock cost on the CLI path), so it only appears once an operator deliberately opts in.
- `GCO_ENABLE_MISSION=true` (or the umbrella `GCO_ENABLE_ALL_TOOLS=true`) to run any Mission CLI subcommand.

**Usage:**

```bash
export GCO_ENABLE_MISSION=true
export GCO_ENABLE_SEMANTIC_PROGRESS=true

# Drive a Mission whose completion is judged semantically against the directive.
gco mission start --run \
  --directive "Stand up a healthy vLLM inference endpoint serving opt-125m." \
  --criteria-file examples/mission-semantic-progress-criteria.json \
  --max-iterations 10 --max-wall-clock 3600 \
  --tool-allowlist metrics_semantic_progress
```

The Mission loop calls `metrics_semantic_progress` each iteration with these arguments:

```json
{
  "directive": "Stand up a healthy vLLM inference endpoint serving opt-125m.",
  "recent_context": "Iteration 3: deployment created; 1/1 replicas ready; /health returns 200."
}
```

`recent_context` is optional — when omitted the judge scores from the directive alone. Pass a `metric_trend` criterion on the same `metrics.progress_score` dot-path instead to require the score to climb across iterations rather than clear a fixed bar.

**When to use:** Goal-directed runs where "done" is a semantic judgement of the directive rather than a single number a tool can scrape — provisioning workflows, multi-step setups, or any objective where progress is easier to describe than to measure directly.

---

### Mission Training-Loss Observation

**Files:** `mission-training-loss-criteria.json`, `megatrain-trainer-state.json`

Ties the [MegaTrain SFT Job](#megatrain-sft-job) to a [Mission](../docs/MISSION.md) `metric_threshold` criterion so the goal-directed loop can watch training loss fall without any scripting. The MegaTrain trainer writes a Hugging Face `trainer_state.json` to shared EFS under its `OUTPUT_DIR` (`/mnt/gco/megatrain-sft/checkpoints`); its `log_history` is a list of per-step records carrying `loss`, `eval_loss`, `step`, and `epoch`. The read-only `metrics_from_shared_storage_file` tool reads that file with `format=hf_trainer_state`, collects the `loss` field across every `log_history` entry, and reduces the sequence to a single number via an aggregation mode — `min` for "best loss so far", or `last` for "current loss". The result lands in the canonical `{"metrics": {"loss": <number>}}` shape that Mission's Observe phase merges, so the `metrics.loss` dot-path in the criterion resolves directly.

- `megatrain-trainer-state.json` is a small representative sample of the artifact MegaTrain produces (five training steps plus one eval record) so you can try the reader against a concrete file.
- `mission-training-loss-criteria.json` is a one-criterion [Criteria File](../docs/MISSION.md#criteria-file-schema) asserting `metrics.loss <= 1.5`.

**Prerequisites:** `GCO_ENABLE_MISSION=true` (or the umbrella `GCO_ENABLE_ALL_TOOLS=true`). The MegaTrain job must have run and written its `trainer_state.json` to shared storage.

**Usage:**

```bash
export GCO_ENABLE_MISSION=true

# Run the training job that produces trainer_state.json on shared EFS.
gco jobs submit-direct examples/megatrain-sft-job.yaml -r us-east-1

# Drive a Mission that observes the best training loss so far.
gco mission start --run \
  --directive "Drive MegaTrain SFT training loss to 1.5 or below." \
  --criteria-file examples/mission-training-loss-criteria.json \
  --max-iterations 10 --max-wall-clock 3600 \
  --tool-allowlist metrics_from_shared_storage_file
```

The Mission loop calls `metrics_from_shared_storage_file` each iteration with these arguments:

```json
{
  "path": "/mnt/gco/megatrain-sft/checkpoints/trainer_state.json",
  "region": "us-east-1",
  "field": "loss",
  "format": "hf_trainer_state",
  "aggregation": "min"
}
```

Switch `aggregation` to `last` to track the current step's loss instead of the best-so-far. To observe the held-out metric instead, set `field` to `eval_loss`.

**When to use:** Goal-directed training runs where a `metric_threshold` criterion should observe a metric a HF Trainer persisted (loss, eval loss, perplexity) without writing a custom log-scraping tool.

---

### Model Download Job

**File:** `model-download-job.yaml`

Pre-downloads model weights from HuggingFace to shared EFS so inference endpoints can mount them instantly. Downloads `facebook/opt-125m` by default — change the `MODEL_ID` env var for other models. For gated models (Llama, Mistral), uncomment the `HF_TOKEN` env var.

**Usage:**

```bash
kubectl apply -f examples/model-download-job.yaml
# After completion, deploy inference with the cached model:
gco inference deploy my-model -i vllm/vllm-openai:v0.24.0 --model-path /models/opt-125m
```

**When to use:** Pre-caching model weights before inference deployment, avoiding repeated downloads across pods.

---

### Multi-GPU Distributed Training

**File:** `multi-gpu-training.yaml`

Distributed training across multiple GPUs using PyTorch DistributedDataParallel (DDP). Creates indexed pods with a headless service for DNS-based peer discovery.

**Usage:**

```bash
kubectl apply -f examples/multi-gpu-training.yaml
kubectl get pods -n gco-jobs -l job-name=pytorch-ddp-training
kubectl logs -f job/pytorch-ddp-training -n gco-jobs
```

**Requirements:** GPU nodes available, NVIDIA device plugin (installed by default).

**When to use:** Multi-node distributed training, PyTorch DDP workloads, scaling training across GPUs.

---

### Ray Cluster

**File:** `ray-cluster.yaml`

Creates a Ray cluster for distributed computing — training, hyperparameter tuning, and model serving. Includes a head node and auto-scaling CPU worker group (1–5 replicas).

**Usage:**

```bash
kubectl apply -f examples/ray-cluster.yaml
kubectl get raycluster -n gco-jobs
# Port-forward to Ray dashboard:
kubectl port-forward svc/ray-cluster-head-svc 8265:8265 -n gco-jobs
# Submit a Ray job:
ray job submit --address http://localhost:8265 -- python -c "import ray; ray.init(); print(ray.cluster_resources())"
```

**Requirements:** KubeRay operator (enabled by default). See [KubeRay docs](../docs/KUBERAY.md).

**When to use:** Distributed training, hyperparameter tuning, Ray Serve for model serving, any workload using the Ray framework.

---

### Simple Job

**File:** `simple-job.yaml`

A basic Kubernetes Job that runs a simple command and completes. Start here to verify your cluster is working.

**Usage:**

```bash
gco jobs submit-sqs examples/simple-job.yaml --region us-east-1
# or directly:
kubectl apply -f examples/simple-job.yaml
kubectl logs job/hello-gco
```

**When to use:** Testing cluster connectivity, running one-off tasks, batch processing.

---

### Slurm Cluster Job

**File:** `slurm-cluster-job.yaml`

A self-contained Kubernetes Job that writes a Slurm batch script into the login pod, submits it via `sbatch`, waits for completion, and prints the output.

**Usage:**

```bash
kubectl apply -f examples/slurm-cluster-job.yaml
kubectl logs job/slurm-test -n gco-jobs -f
```

**Requirements:** Slinky Slurm Operator + Slurm cluster (opt-in — enable in `cdk.json`: `"helm": { "slurm": { "enabled": true } }`), cert-manager (installed by default). See [Slurm docs](../docs/SLURM_OPERATOR.md).

**When to use:** HPC workloads needing Slurm's deterministic scheduling, teams migrating from on-premises Slurm clusters.

---

### SQS Job Submission

**File:** `sqs-job-submission.yaml`

Demonstrates the recommended SQS-based submission path. Contains two example jobs (CPU and GPU) that you submit via the SQS queue. The built-in KEDA consumer automatically picks up messages and applies manifests to the cluster.

**Usage:**

```bash
gco jobs submit-sqs examples/sqs-job-submission.yaml --region us-east-1
gco jobs submit-sqs examples/sqs-job-submission.yaml --auto-region
gco jobs submit-sqs examples/sqs-job-submission.yaml --priority 10
```

**Why SQS:** Decoupled (returns immediately), fault-tolerant (DLQ after 3 retries), auto-scaling (KEDA scales consumers based on queue depth), priority support.

---

### Trainium Job

**File:** `trainium-job.yaml`

Runs on an AWS Trainium instance using the Neuron SDK. Trainium is a purpose-built ML accelerator designed by AWS, offering lower cost than GPU instances for training workloads.

**Usage:**

```bash
gco jobs submit examples/trainium-job.yaml --region us-east-1
gco jobs logs trainium-test -r us-east-1
```

**Requirements:** Neuron device plugin (installed by default), Neuron nodepool (applied by default), container images built with the Neuron SDK (not CUDA).

**When to use:** Cost-optimized model training, distributed training with EFA on trn1.32xlarge or trn2.48xlarge.

---

### Valkey Cache Job

**File:** `valkey-cache-job.yaml`

Connects to the regional Valkey Serverless cache for K/V caching, session state, or feature stores. The Valkey endpoint is injected automatically via the `gco-valkey` ConfigMap — the same manifest works in any region.

**Prerequisites:** Enable Valkey in `cdk.json`: `"valkey": { "enabled": true }` and redeploy.

**Usage:**

```bash
gco jobs submit-direct examples/valkey-cache-job.yaml -r us-east-1
```

**Demonstrates:** Prompt caching, feature embedding storage, session state management, TTL-based expiry.

**When to use:** LLM prompt caching, feature stores, session state, any workload needing low-latency key-value access.

---

### Volcano Gang Scheduling

**File:** `volcano-gang-job.yaml`

Demonstrates gang scheduling for distributed training — all pods must be scheduled together or none at all. Creates a master + 2 workers with automatic restart policies.

**Usage:**

```bash
kubectl apply -f examples/volcano-gang-job.yaml
kubectl get vcjob -n gco-jobs
kubectl get pods -n gco-jobs -l volcano.sh/job-name=distributed-training
```

**Requirements:** Volcano (enabled by default). See [Volcano docs](../docs/VOLCANO.md).

**When to use:** Distributed training requiring all workers to start simultaneously, jobs with master-worker topology.

---

### YuniKorn Hierarchical Queues

**File:** `yunikorn-job.yaml`

Demonstrates Apache YuniKorn's app-aware scheduling with hierarchical queues, gang scheduling, and GPU queue placement.

**Usage:**

```bash
kubectl apply -f examples/yunikorn-job.yaml
kubectl get pods -n gco-jobs -l app=yunikorn-demo -w
# Access the YuniKorn web UI:
kubectl port-forward svc/yunikorn-service -n yunikorn 9889:9889
```

**Requirements:** YuniKorn (opt-in — enable in `cdk.json`: `"helm": { "yunikorn": { "enabled": true } }`). See [YuniKorn docs](../docs/YUNIKORN.md).

**When to use:** Multi-tenant clusters with competing teams, hierarchical resource quota management, fair-sharing with preemption.

For a detailed comparison of all schedulers, see the [Schedulers Overview](../docs/SCHEDULERS.md).

---

## Customizing Examples

### Changing Resource Requests/Limits

Edit the `resources` section:

```yaml
resources:
  requests:
    cpu: "500m"      # Increase CPU request
    memory: "256Mi"  # Increase memory request
  limits:
    cpu: "1000m"     # Increase CPU limit
    memory: "1Gi"    # Increase memory limit
```

### Changing Replica Count

Edit the `replicas` field in Deployments:

```yaml
spec:
  replicas: 5  # Increase from 3 to 5
```

### Adding Environment Variables

Add to the container spec:

```yaml
containers:
- name: my-app
  image: my-image
  env:
  - name: MY_VAR
    value: "my-value"
  - name: CLUSTER_NAME
    value: "gco-us-east-1"
```

### Using ConfigMaps and Secrets

```yaml
# Create ConfigMap
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
data:
  config.json: |
    {"key": "value"}

---
# Use in Pod
containers:
- name: my-app
  volumeMounts:
  - name: config
    mountPath: /etc/config
volumes:
- name: config
  configMap:
    name: app-config
```

## Best Practices

### 1. Always Set Resource Limits

```yaml
resources:
  requests:
    cpu: "100m"
    memory: "128Mi"
  limits:
    cpu: "500m"
    memory: "512Mi"
```

This helps Kubernetes schedule pods efficiently and prevents resource exhaustion.

### 2. Use Security Contexts

```yaml
securityContext:
  allowPrivilegeEscalation: false
  runAsNonRoot: true
  runAsUser: 1000
  capabilities:
    drop: ["ALL"]
```

This follows security best practices and reduces attack surface.

### 3. Add Labels

```yaml
metadata:
  labels:
    app: my-app
    version: v1.0
    environment: production
```

Labels help with organization, selection, and monitoring.

### 4. Use Namespaces

```yaml
metadata:
  namespace: my-namespace
```

Namespaces provide isolation and organization.

### 5. Set Restart Policies

For Jobs:

```yaml
spec:
  template:
    spec:
      restartPolicy: Never  # or OnFailure
```

For Deployments:

```yaml
spec:
  template:
    spec:
      restartPolicy: Always  # default
```

### 6. Prevent Node Consolidation for Long-Running Jobs

EKS Auto Mode (Karpenter) may consolidate underutilized nodes, which evicts running pods. For training jobs or other long-running workloads that should not be interrupted, add the `karpenter.sh/do-not-disrupt` annotation to the pod template:

```yaml
spec:
  template:
    metadata:
      annotations:
        karpenter.sh/do-not-disrupt: "true"
```

This tells the autoscaler to leave the node alone until the pod completes. Remove the annotation or omit it for short-lived jobs where eviction and retry is acceptable.

## Troubleshooting

### "Resource limits exceed maximum allowed values"

The GCO API validates manifest resource requests against configurable limits. If your job needs more CPU, memory, or GPUs than the defaults allow, update the limits in `cdk.json` and redeploy:

```json
{
  "job_validation_policy": {
    "resource_quotas": {
      "max_cpu_per_manifest": "96",
      "max_memory_per_manifest": "192Gi",
      "max_gpu_per_manifest": 8
    }
  }
}
```

Then redeploy: `gco stacks deploy-all -y`

`job_validation_policy` is authoritative for both submission paths: the REST manifest processor and SQS queue processor enforce the same limits, namespace/kind allowlists, image policy, and security controls.

## Testing Your Manifests

### Dry Run

Test without actually creating resources:

```bash
kubectl apply -f <MANIFEST_FILE> --dry-run=client
```

### Validate

Check for syntax errors:

```bash
kubectl apply -f <MANIFEST_FILE> --validate=true --dry-run=server
```

Replace `<MANIFEST_FILE>` with the path to the manifest you want to test (for example `examples/simple-job.yaml`).

### Check Status

After applying:

```bash
# Check pods
kubectl get pods -w

# Check events
kubectl get events --sort-by='.lastTimestamp'

# Describe resource
kubectl describe pod <POD_NAME>
```

Replace `<POD_NAME>` with the name of the pod to inspect (from `kubectl get pods`).

## Cleaning Up

Delete resources created by examples:

```bash
# Delete specific resource
kubectl delete -f examples/simple-job.yaml

# Delete all jobs
kubectl delete jobs --all

# Delete all resources in a namespace
kubectl delete all --all -n default
```

## Additional Resources

- [GCO Documentation](../docs/README.md) — Full documentation index
- [Inference Serving Guide](../docs/INFERENCE.md) — Deploy and manage inference endpoints
- [Schedulers Overview](../docs/SCHEDULERS.md) — Compare all supported schedulers
- [Customization Guide](../docs/CUSTOMIZATION.md) — Enable features like FSx, Valkey, Aurora
- [CLI Reference](../docs/CLI.md) — Complete `gco` command reference
- [Troubleshooting Guide](../docs/TROUBLESHOOTING.md) — Common issues and solutions
- [Kubernetes Documentation](https://kubernetes.io/docs/)

---

**Need help?** Check the [Troubleshooting Guide](../docs/TROUBLESHOOTING.md) or connect the [MCP server](../gco_mcp/) to your IDE and ask in natural language.
