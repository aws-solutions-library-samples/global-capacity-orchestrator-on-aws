# Documentation

Comprehensive guides for understanding, deploying, operating, and customizing **Global Capacity Orchestrator (GCO)** — *One API. Every Accelerator. Any Region.*

> **💡 Tip:** Connect the [MCP server](../gco_mcp/) to an agent and explore the codebase through conversation. Ask things like *"What CDK stacks does GCO create?"* or *"How does the manifest processor validate jobs?"* — the agent reads the source code and docs to answer. See [gco_mcp/README.md](../gco_mcp/README.md) for setup.

## Table of Contents

- [Guides](#guides)
- [Schedulers & Orchestrators](#schedulers--orchestrators)
- [Supplementary](#supplementary)
- [Reading Order](#reading-order)

## Guides

| Document | Audience | Description |
|----------|----------|-------------|
| [Core Concepts](CONCEPTS.md) | New users | What GCO is, the problems it solves, and how the key components work together |
| [Architecture](ARCHITECTURE.md) | Engineers | Deep dive into the multi-region infrastructure, security layers, data flow, and scale characteristics |
| [Quick Start](../QUICKSTART.md) | New users | Get running in under 60 minutes — install, deploy, submit your first job |
| [CLI Reference](CLI.md) | Operators | Complete command reference for all `gco` CLI commands |
| [Inference Guide](INFERENCE.md) | ML engineers | Deploy and manage multi-region GPU inference endpoints (vLLM, TGI, Triton, SGLang, TorchServe) |
| [API Reference](API.md) | Developers | REST API documentation for manifest submission, job management, and webhooks |
| [Customization](CUSTOMIZATION.md) | Platform teams | Add regions, tune nodepools, enable FSx/Valkey/EFA, configure queue processor |
| [Analytics Environment](ANALYTICS.md) | Data scientists / ML engineers | Optional SageMaker Studio + EMR Serverless environment for interactive analysis of cluster data |
| [Mission](MISSION.md) | Operators | GCO's goal-directed iteration loop that runs five-phase iterations against machine-checkable success criteria until a verdict is reached |
| [Cluster Shared Bucket](CLUSTER_SHARED_BUCKET.md) | Operators | Always-on cross-region S3 bucket shared across all regional clusters and the analytics environment |
| [Troubleshooting](TROUBLESHOOTING.md) | Operators | Common issues and solutions for deployment, networking, pods, and storage |
| [Operational Runbooks](RUNBOOKS.md) | Operators | Step-by-step incident response procedures for common failure scenarios |
| [Maintenance](MAINTENANCE.md) | Maintainers | Routine upkeep: adding instance types, EKS version upgrades, base-image and CVE-suppression refreshes, dependency bumps |

## Schedulers & Orchestrators

| Document | Audience | Description |
|----------|----------|-------------|
| [Schedulers Overview](SCHEDULERS.md) | ML/HPC engineers | Comparison, decision guide, and how the scheduling tools combine |
| [Volcano](VOLCANO.md) | ML/HPC engineers | Gang scheduling and batch job management for distributed training (enabled by default) |
| [Kueue](KUEUE.md) | ML/HPC engineers | Job queueing with resource quotas, fair sharing, and priority (enabled by default) |
| [KubeRay](KUBERAY.md) | ML engineers | Ray distributed computing for training, tuning, and serving (enabled by default) |
| [KEDA](KEDA.md) | Platform teams | Event-driven autoscaling from SQS, Prometheus, CloudWatch, and 60+ sources (enabled by default) |
| [Slurm (Slinky)](SLURM_OPERATOR.md) | HPC engineers | HPC-style scheduling with sbatch/srun on Kubernetes (opt-in) |
| [YuniKorn](YUNIKORN.md) | Platform teams | App-aware scheduler with hierarchical queues and multi-tenant fair sharing (opt-in) |

## Supplementary

| Directory | Description |
|-----------|-------------|
| [adr/](adr/) | Architecture Decision Records — the append-only log of significant architectural decisions and the *why* behind them |
| [client-examples/](client-examples/) | API client examples in Python, curl, and AWS CLI |
| [iam-policies/](iam-policies/) | IAM policy templates for different access levels |

## Reading Order

If you're new to GCO:

1. [Core Concepts](CONCEPTS.md) — understand what it does
2. [Quick Start](../QUICKSTART.md) — get it running
3. [CLI Reference](CLI.md) — learn the commands
4. [Inference Guide](INFERENCE.md) — deploy inference endpoints
5. [Schedulers Overview](SCHEDULERS.md) — pick the right scheduler for your workload

If you're customizing or operating:

1. [Architecture](ARCHITECTURE.md) — understand the infrastructure
2. [Customization](CUSTOMIZATION.md) — tune for your needs
3. [Cluster Shared Bucket](CLUSTER_SHARED_BUCKET.md) — the always-on shared storage layer
4. [Analytics Environment](ANALYTICS.md) — optional Studio + EMR for interactive analysis
5. [Mission](MISSION.md) — run goal-directed iteration loops
6. [Schedulers Overview](SCHEDULERS.md) — configure scheduling tools
7. [Troubleshooting](TROUBLESHOOTING.md) — fix issues
8. [Operational Runbooks](RUNBOOKS.md) — incident response procedures
9. [Maintenance](MAINTENANCE.md) — keep instance types, the EKS version, and pinned tooling current
