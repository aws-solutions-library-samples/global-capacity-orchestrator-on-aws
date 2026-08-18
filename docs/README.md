# Documentation

Comprehensive guides for understanding, deploying, operating, and customizing **Global Capacity Orchestrator (GCO)** — *One API. Every Accelerator. Any Region.*

> **💡 Tip:** Connect the [MCP server](../gco_mcp/) to an agent and explore the codebase through conversation. Ask things like *"What [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) stacks does GCO create?"* or *"How does the manifest processor validate jobs?"* — the agent reads the source code and docs to answer. See [gco_mcp/README.md](../gco_mcp/README.md) for setup.

## Table of Contents

- [Guides](#guides)
- [Schedulers & Orchestrators](#schedulers--orchestrators)
- [Supplementary](#supplementary)
- [Reading Order](#reading-order)

## Guides

| Document | Audience | Description |
|----------|----------|-------------|
| [Project Tenets](../TENETS.md) | Everyone | Normative north star and prioritized decision guidance for safety, truth, security, accelerator policy, operations, and maintainability |
| [Core Concepts](CONCEPTS.md) | New users | What GCO is, the problems it solves, and how the key components work together |
| [Learning Path](LEARNING_PATH.md) | New users / new to Kubernetes | A staged, hands-on path from zero to productive, with a Kubernetes primer and role-based tracks |
| [Architecture](ARCHITECTURE.md) | Engineers | Deep dive into the multi-region infrastructure, security layers, data flow, and scale characteristics |
| [Quick Start](../QUICKSTART.md) | New users | Get running in under 60 minutes — install, deploy, submit your first job |
| [CLI Reference](CLI.md) | Operators | Complete command reference for all `gco` CLI commands |
| [Autopilot](AUTOPILOT.md) | Everyone | `gco autopilot` — one command to a fully configured [Claude Code](https://code.claude.com/docs/en/overview) session on [Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html) with the GCO MCP server and companion MCPs wired in |
| [Inference Guide](INFERENCE.md) | ML engineers | Deploy and manage multi-region GPU inference endpoints ([vLLM](https://docs.vllm.ai/en/latest/), TGI, Triton, [SGLang](https://docs.sglang.ai/), [TorchServe](https://pytorch.org/serve/)) |
| [Distributed Training](DISTRIBUTED_TRAINING.md) | ML engineers | Multi-node training through the [Kubeflow Trainer v2](https://github.com/kubeflow/trainer) `TrainJob` API (on by default): runtimes, validation semantics, GPU variants, Kueue gang scheduling, and Spot guidance |
| [API Reference](API.md) | Developers | Every HTTP surface: the control plane (manifests, jobs, queue, templates, webhooks, cost), cross-region aggregation, inference, and health/observability |
| [Customization](CUSTOMIZATION.md) | Platform teams | Add regions, tune nodepools, enable FSx/[Valkey](https://valkey.io/)/EFA, configure queue processor |
| [Analytics Environment](ANALYTICS.md) | Data scientists / ML engineers | Optional [SageMaker](https://docs.aws.amazon.com/sagemaker/latest/dg/whatis.html) Studio + [EMR Serverless](https://docs.aws.amazon.com/emr/latest/EMR-Serverless-UserGuide/emr-serverless.html) environment for interactive analysis of cluster data |
| [Cluster Observability](MONITORING.md) | Operators | Self-hosted per-cluster [Prometheus](https://prometheus.io/docs/introduction/overview/) + [Grafana](https://grafana.com/docs/grafana/latest/) + Alertmanager (on by default), private port-forward access, and the `gco monitoring` CLI |
| [Cost Monitoring](COST_MONITORING.md) | Operators, FinOps | Per-cluster [OpenCost](https://opencost.io/) + Grafana cost dashboard (on by default), scheduled [Parquet](https://parquet.apache.org/docs/) cost reports to S3, cross-region [Athena](https://docs.aws.amazon.com/athena/latest/ug/what-is.html) analytics via `gco costs k8s`, ad-hoc reports via `/api/v1/cost/*`, and spot price-aware central-queue scheduling |
| [Mission](MISSION.md) | Operators | GCO's goal-directed iteration loop that runs five-phase iterations against machine-checkable success criteria until a verdict is reached |
| [Cluster Shared Bucket](CLUSTER_SHARED_BUCKET.md) | Operators | Always-on cross-region [S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html) bucket shared across all regional clusters and the analytics environment |
| [Troubleshooting](TROUBLESHOOTING.md) | Operators | Common issues and solutions for deployment, networking, pods, and storage |
| [Operational Runbooks](RUNBOOKS.md) | Operators | Step-by-step incident response procedures for common failure scenarios |
| [Maintenance](MAINTENANCE.md) | Maintainers | Routine upkeep: adding instance types, [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) version upgrades, base-image and CVE-suppression refreshes, dependency bumps |
| [Live Release Validation](LIVE_RELEASE_VALIDATION.md) | Maintainers / operators | Run the local deploy-test-destroy harness and post its sanitized summary in a pull request comment; full reports stay local |
| [Floci Testing](FLOCI_TESTING.md) | Contributors / maintainers | The emulated-AWS test layer between in-process mocks and real-account validation: how CI runs it, what it proves, known emulator gaps, and how `gco release validate --emulator-endpoint` rehearses the harness |
| [Image Mirror](IMAGE_MIRROR.md) | Platform teams | Mirror Docker Hub add-on images into project-scoped [ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html) repositories with multi-architecture preservation |
| [Forking](FORKING.md) | Anyone running their own copy | Take GCO into your own repository: repoint badges, clone URLs, the Pages site, and the OIDC trust-policy subject with `scripts/migrate_fork.py`, plus the manual follow-ups it cannot decide |

## Schedulers & Orchestrators

| Document | Audience | Description |
|----------|----------|-------------|
| [Schedulers Overview](SCHEDULERS.md) | ML/HPC engineers | Comparison, decision guide, and how the scheduling tools combine |
| [Volcano](VOLCANO.md) | ML/HPC engineers | Gang scheduling and batch job management for distributed training (enabled by default) |
| [Kueue](KUEUE.md) | ML/HPC engineers | Job queueing with resource quotas, fair sharing, and priority (enabled by default) |
| [KubeRay](KUBERAY.md) | ML engineers | Ray distributed computing for training, tuning, and serving (enabled by default) |
| [KEDA](KEDA.md) | Platform teams | Event-driven autoscaling from [SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html), Prometheus, [CloudWatch](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/WhatIsCloudWatch.html), and 60+ sources (enabled by default) |
| [Slurm (Slinky)](SLURM_OPERATOR.md) | HPC engineers | HPC-style scheduling with sbatch/srun on Kubernetes (opt-in) |
| [YuniKorn](YUNIKORN.md) | Platform teams | App-aware scheduler with hierarchical queues and multi-tenant fair sharing (opt-in) |

## Supplementary

| Directory | Description |
|-----------|-------------|
| [adr/](adr/) | Architecture Decision Records — the append-only log of significant architectural decisions and the *why* behind them |
| [client-examples/](client-examples/) | API client examples in Python, curl, and AWS CLI |
| [openapi/](openapi/) | Generated OpenAPI documents, one per HTTP service, regenerated by `scripts/generate_openapi.py` |
| [iam-policies/](iam-policies/) | [IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction.html) policy templates for different access levels |

## Reading Order

If you're new to GCO:

1. [Project Tenets](../TENETS.md) — understand the north star and how trade-offs are resolved
2. [Learning Path](LEARNING_PATH.md) — the guided route through the docs below, with a Kubernetes primer for newcomers
3. [Core Concepts](CONCEPTS.md) — understand what it does
4. [Quick Start](../QUICKSTART.md) — get it running
5. [CLI Reference](CLI.md) — learn the commands
6. [Inference Guide](INFERENCE.md) — deploy inference endpoints
7. [Schedulers Overview](SCHEDULERS.md) — pick the right scheduler for your workload

If you're customizing or operating:

1. [Project Tenets](../TENETS.md) — apply the project's prioritized decision framework
2. [Architecture](ARCHITECTURE.md) — understand the infrastructure
3. [Customization](CUSTOMIZATION.md) — tune for your needs
4. [Cluster Shared Bucket](CLUSTER_SHARED_BUCKET.md) — the always-on shared storage layer
5. [Analytics Environment](ANALYTICS.md) — optional Studio + EMR for interactive analysis
6. [Mission](MISSION.md) — run goal-directed iteration loops
7. [Schedulers Overview](SCHEDULERS.md) — configure scheduling tools
8. [Troubleshooting](TROUBLESHOOTING.md) — fix issues
9. [Operational Runbooks](RUNBOOKS.md) — incident response procedures
10. [Maintenance](MAINTENANCE.md) — keep the accelerator catalog, EKS version, and pinned tooling current
