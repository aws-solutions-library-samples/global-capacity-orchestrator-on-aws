# Learning Path

A staged, hands-on path for getting productive with **Global Capacity Orchestrator (GCO)** — *One API. Every Accelerator. Any Region.* It is written for two overlapping audiences: people new to GCO, and people new to Kubernetes. Each stage pairs a guide to read with something concrete to run and a checkpoint that tells you when you have understood it.

> **💡 Learn by asking.** GCO ships an [MCP server](../gco_mcp/README.md) you can connect to an AI-powered IDE. Instead of reading every file, ask questions like *"What happens between `gco jobs submit` and my pod running?"* and the agent answers from the source and docs. See [Learn by Asking](#learn-by-asking-the-mcp-server) below.

## Table of Contents

- [How to Use This Path](#how-to-use-this-path)
- [Track 0: Kubernetes Foundations](#track-0-kubernetes-foundations)
- [The Common Core](#the-common-core)
- [Role-Based Tracks](#role-based-tracks)
- [Advanced and Optional Modules](#advanced-and-optional-modules)
- [Learn by Asking: the MCP Server](#learn-by-asking-the-mcp-server)
- [At a Glance](#at-a-glance)

## How to Use This Path

GCO is "submit a Kubernetes manifest, get accelerated compute," so its whole vocabulary is Kubernetes. Route yourself based on what you already know:

- **New to Kubernetes?** Start with [Track 0](#track-0-kubernetes-foundations), then do [The Common Core](#the-common-core), then pick a role track.
- **New to GCO only?** Skip Track 0, go straight to [The Common Core](#the-common-core), then pick a role track.

The path is built on a few principles:

- **Progressive disclosure.** One new idea at a time. Nobody reads the [Architecture](ARCHITECTURE.md) deep dive on day one.
- **No-cost before billable.** The [Quick Start](../QUICKSTART.md) defines two checkpoints: the *First Success Milestone* (verify the CLI, zero AWS charges) and the *First Deploy Milestone* (provisions billable resources). Crossing into billable territory is a deliberate step.
- **Learn by doing.** Every stage runs a real manifest from the [`examples/`](../examples/README.md) directory.
- **Branch by role after a shared core.** An ML engineer, an operator, and a platform engineer need different things once they can run a job.

## Track 0: Kubernetes Foundations

*Skip this track if you already know Kubernetes.*

The goal here is narrow: understand the handful of Kubernetes objects GCO actually uses, not all of Kubernetes. You do not need to run your own control plane — EKS Auto Mode does that for you.

**Warm up (a few hours).** Work through the official [Kubernetes Basics tutorial](https://kubernetes.io/docs/tutorials/kubernetes-basics/) and skim the [Job concept page](https://kubernetes.io/docs/concepts/workloads/controllers/job/). Focus on Pods, Jobs, Deployments, Services, Namespaces, PersistentVolumeClaims, and resource requests and limits.

**Map each concept to where it shows up in GCO.** This table is the mental model that matters:

| Kubernetes concept | Where it appears in GCO |
|--------------------|-------------------------|
| Pod | The unit that runs your container on a node provisioned by Karpenter |
| Job (`batch/v1`) | What you submit for batch and training work; runs in the `gco-jobs` namespace |
| `resources.limits.nvidia.com/gpu: 1` | The line that makes GCO provision a GPU node for you |
| Namespace | `gco-system` (platform services), `gco-jobs` (your jobs), `gco-inference` (endpoints) |
| PersistentVolumeClaim | `gco-shared-storage` (EFS) or `gco-fsx-storage` (FSx) for outputs that survive pod termination |
| Deployment + Service | Created for you by the inference monitor when you deploy an endpoint; requests stay behind the shared authenticated Gateway API route |
| Nodepool / autoscaling | Abstracted by EKS Auto Mode and Karpenter — you never hand-edit node groups |

**Checkpoint.** You can read the sample Job manifest in [Core Concepts](CONCEPTS.md#manifest-submission), explain what each field does, and say which namespace it runs in and how it gets a GPU.

## The Common Core

Everyone does these four stages in order. By the end you can deploy GCO, submit jobs several ways, and persist outputs.

### Stage 1: Understand What GCO Is

- **Read:** [Core Concepts](CONCEPTS.md) — what GCO is, the problem it solves, multi-region architecture, EKS Auto Mode, nodepools, manifest submission, and global routing.
- **Do:** nothing to install yet. Skim the four submission methods and the security-layers diagram.
- **Go deeper (optional):** the AWS layer GCO builds on — [Amazon EKS Auto Mode](https://docs.aws.amazon.com/eks/latest/userguide/automode.html) for hands-off compute, and [Karpenter](https://karpenter.sh/docs/concepts/) for the node provisioning behind GCO's nodepools.
- **Checkpoint:** in one sentence each, explain "capacity-aware placement," "health-based global routing," "EKS Auto Mode," and why job outputs survive after a pod terminates.

### Stage 2: First Success Milestone

*This milestone incurs no AWS charges.*

- **Read:** the [Quick Start](../QUICKSTART.md) through the First Success Milestone section.
- **Do:** build the dev container and verify the CLI.

  ```bash
  ./scripts/setup-dev-alias.sh
  gco --version
  ```

- **Checkpoint:** you see a `gco, version ...` line and no error. If not, you have learned the pinned-dependency story and why the dev container is the recommended path.

### Stage 3: First Deploy and Your First Job

*This stage provisions billable AWS resources.*

- **Read:** the [Quick Start](../QUICKSTART.md) deploy and test-job steps. Note that scheduler and operator Helm charts converge asynchronously and can take 10–30 minutes to become ready after the cluster reports complete.
- **Do:** deploy, then submit the starter job and read its logs.

  ```bash
  gco stacks deploy-all -y
  gco jobs submit examples/simple-job.yaml -n gco-jobs
  gco jobs list --all-regions
  gco jobs logs hello-gco -n gco-jobs -r us-east-1
  ```

- **Checkpoint:** `gco jobs list --all-regions` shows the job completed and you retrieved its logs. You now understand the deploy → submit → status → logs loop.

### Stage 4: Core Job Workflows

- **Read:** the [CLI Reference](CLI.md) (jobs, queue, and capacity sections) and the storage section of [Core Concepts](CONCEPTS.md#storage-options).
- **Do:** run these in order, checking capacity first for the GPU job.

  ```bash
  gco capacity check --instance-type g4dn.xlarge --region us-east-1
  gco jobs submit-sqs examples/gpu-job.yaml --region us-east-1
  gco queue submit examples/simple-job.yaml --region us-east-1
  gco jobs submit-direct examples/efs-output-job.yaml -r us-east-1
  ```

- **Checkpoint:** you can pick the right submission method (SQS, API Gateway, the global DynamoDB queue, or direct kubectl) for a situation and explain when a GPU node gets created.

At this point you are productive. Now branch into one or more role tracks.

## Role-Based Tracks

Pick the track that matches your work. Each ends with a checkpoint that proves the skill.

### Track A: ML and Research Engineer

Train and serve models.

- **Inference:** read the [Inference Guide](INFERENCE.md), then deploy an endpoint.

  ```bash
  gco inference deploy my-llm -i vllm/vllm-openai:v0.26.0 --gpu-count 1
  gco inference status my-llm
  ```

  Then try the other frameworks with [`examples/inference-tgi.yaml`](../examples/inference-tgi.yaml), [`examples/inference-sglang.yaml`](../examples/inference-sglang.yaml), [`examples/inference-triton.yaml`](../examples/inference-triton.yaml), and [`examples/inference-torchserve.yaml`](../examples/inference-torchserve.yaml).
- **Distributed training:** work up through [`examples/multi-gpu-training.yaml`](../examples/multi-gpu-training.yaml) and [`examples/efa-distributed-training.yaml`](../examples/efa-distributed-training.yaml).
- **AWS accelerators:** try [`examples/trainium-job.yaml`](../examples/trainium-job.yaml) and [`examples/inferentia-job.yaml`](../examples/inferentia-job.yaml).
- **Schedulers:** read the [Schedulers Overview](SCHEDULERS.md), then [Volcano](VOLCANO.md) (gang scheduling) and [Kueue](KUEUE.md) (queueing and quotas) — both on by default.
- **Checkpoint:** you can deploy a [vLLM](https://docs.vllm.ai/en/latest/) endpoint, send it a prompt, and launch a multi-GPU training job.

### Track B: Operator and SRE

Run GCO in production.

- **Read:** the [CLI Reference](CLI.md) in full, then [Troubleshooting](TROUBLESHOOTING.md) and the [Operational Runbooks](RUNBOOKS.md).
- **Do:** practice capacity discovery and diagnosis.

  ```bash
  gco capacity recommend-region --gpu
  gco costs summary
  ```

- **Optional:** the [Mission](MISSION.md) goal-directed iteration loop.
- **Checkpoint:** you can diagnose a stuck job and an `ImagePullBackOff` using only the runbooks.

### Track C: Platform and Customization Engineer

Adapt GCO to your environment.

- **Read:** the [Architecture](ARCHITECTURE.md) deep dive, the [Customization Guide](CUSTOMIZATION.md), and the [Image Mirror](IMAGE_MIRROR.md) guide.
- **Do:** add a region to `cdk.json`, redeploy, and enable one optional service (FSx, [Valkey](https://valkey.io/), or Aurora [pgvector](https://github.com/pgvector/pgvector)).
- **Checkpoint:** you can explain the global-versus-regional stack split and add an instance type to a nodepool.

### Track D: Maintainer

Keep GCO healthy over time.

- **Read:** the [Maintenance Guide](MAINTENANCE.md) (EKS version upgrades, instance types, CVE-suppression refreshes, dependency scans) and the [Architecture Decision Records](adr/).
- **Checkpoint:** you can run the maintenance checklist and explain the reasoning behind a recorded architectural decision.

## Advanced and Optional Modules

Reach for these from any track once the core is solid:

- **More schedulers:** [KubeRay](KUBERAY.md), [KEDA](KEDA.md), [Slurm](SLURM_OPERATOR.md), and [YuniKorn](YUNIKORN.md).
- **Pipelines:** multi-step DAGs via [`examples/pipeline-dag.yaml`](../examples/pipeline-dag.yaml).
- **Databases and caching:** [`examples/aurora-pgvector-job.yaml`](../examples/aurora-pgvector-job.yaml) and [`examples/valkey-cache-job.yaml`](../examples/valkey-cache-job.yaml).
- **Analytics:** the [Analytics Environment](ANALYTICS.md) (SageMaker Studio and [EMR Serverless](https://docs.aws.amazon.com/emr/latest/EMR-Serverless-UserGuide/emr-serverless.html)).
- **Shared storage:** the always-on [Cluster Shared Bucket](CLUSTER_SHARED_BUCKET.md).
- **Direct REST access:** the [API Reference](API.md).

## Learn by Asking: the MCP Server

At every stage you can explore GCO conversationally instead of grep-ing source. Connect the [GCO MCP server](../gco_mcp/README.md) to an MCP-capable IDE and ask questions scoped to where you are:

- Stage 1: *"What CDK stacks does GCO create, and why is there a separate global one?"*
- Stage 3: *"Walk me through what happens between `gco jobs submit` and my pod running."*
- Track A: *"How does the inference monitor reconcile desired state?"*
- Track C: *"What changes in the regional stack when I enable FSx?"*

This doubles as onboarding to MCP itself.

## At a Glance

| Stage | Read | Do | Done when |
|-------|------|----|-----------|
| 0. Kubernetes foundations | Kubernetes Basics tutorial | — | You can read a Job manifest |
| 1. Understand GCO | [Core Concepts](CONCEPTS.md) | — | You can explain three core ideas |
| 2. First Success | [Quick Start](../QUICKSTART.md) | `gco --version` | Version prints, nothing billed |
| 3. First Deploy | [Quick Start](../QUICKSTART.md) | `examples/simple-job.yaml` | Job completes, logs pulled |
| 4. Core workflows | [CLI Reference](CLI.md) | GPU / EFS / SQS jobs | You pick the right submit path |
| A–D. Role tracks | Role guides | Role examples | Role-specific checkpoint |

---

**Next steps:** start with [Core Concepts](CONCEPTS.md), then the [Quick Start](../QUICKSTART.md). Browse every guide in the [Documentation Index](README.md).
