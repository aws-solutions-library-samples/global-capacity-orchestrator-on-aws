# Global Capacity Orchestrator on AWS

*One API. Every Accelerator. Any Region.*

**Global Capacity Orchestrator (GCO)** is multi-region accelerated-compute
orchestration for AWS — NVIDIA GPUs, AWS Trainium, AWS Inferentia, and CPU
(amd64 + arm64/Graviton) — with capacity-aware placement workflows, spot
fallback, and autoscaling inference endpoints. You submit a Kubernetes
manifest; GCO validates it, provisions matching nodes through EKS Auto Mode,
runs it, and can persist outputs to shared storage after pods terminate.

This wiki is an **orientation layer**: each page summarizes one facet of the
project and routes you to the authoritative documentation on GitHub. The deep
reference material lives in the repository — start with the
[README](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/README.md)
and the
[documentation index](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/README.md).

## Is GCO for you?

GCO is built for teams running accelerated workloads — LLM training and
inference, batch ML, HPC, and general CPU jobs — that need multi-region
redundancy, capacity discovery, and IAM-based access without per-cluster
kubeconfig distribution. It fits when:

- You run GPU workloads (training, inference, batch processing) and want
  capacity-aware region selection instead of manually checking each region.
- You want inference endpoints deployed across multiple regions with a single
  command, with automatic failover in the commercial `aws` partition.
- You prefer IAM (SigV4) authentication over kubeconfig management.
- You need job outputs to persist after pods terminate (EFS/FSx).

The full problem/solution comparison lives in the README's
[Why GCO?](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/README.md#why-gco)
section, and
[Core Concepts](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/CONCEPTS.md)
explains the ideas behind it.

## What you get

- **One coherent API** across regions: a global, IAM-authenticated workload
  API with health-based failover in commercial `aws`; IAM-authenticated
  regional workload APIs in `aws-cn` and `aws-us-gov` — all through the same
  CLI and MCP server.
- **Capacity intelligence**: spot placement scores, spot price history,
  capacity reservations, and auto-region workflows behind `gco capacity` —
  network routing never substitutes for live GPU-capacity placement.

![Checking GPU capacity for g5.xlarge in us-east-1 through the GCO MCP server](assets/images/gco_mcp_check_capacity.png)

*Capacity discovery the conversational way: the GCO MCP server answering a
GPU capacity question in an AI-powered IDE.*

- **Automatic GPU node provisioning** through EKS Auto Mode and purpose-built
  NodePools (GPU x86/ARM, inference, EFA, Neuron, CPU).
- **Multi-region inference endpoint management** (vLLM, TGI, Triton,
  TorchServe, SGLang) with rolling updates, scaling, and canary deployments.
- **An agent-first front door**: `gco autopilot` turns a terminal into a
  configured Claude Code session on Amazon Bedrock, grounded by the project's
  own MCP server — see
  [docs/AUTOPILOT.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/AUTOPILOT.md).

## Guided by ordered tenets

Every trade-off in GCO is resolved against ten prioritized
[project tenets](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/TENETS.md),
beginning with *Protect Workloads, Data, and Accounts* and *Tell the Truth
About State and Capacity*. Earlier tenets outrank later ones, and durable
exceptions require an Architecture Decision Record.

## What it costs

The README's
[sample cost table](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/README.md#sample-cost-table)
estimates a single-region deployment at roughly **$210/month of fixed platform
cost** (EKS cluster, NAT gateways, ALB, monitoring, and friends) — GPU
instances dominate real spend and scale with usage. Multi-region deployments
scale linearly.

## Where to go next

| You are… | Start with |
| --- | --- |
| Evaluating GCO or deploying it for the first time | [Evaluating & deploying](evaluating-and-deploying.md) |
| Wondering what workloads it supports | [What you can run](what-you-can-run.md) |
| Trying to understand the architecture | [How it works](how-it-works.md) |
| A developer exploring the codebase | [Repo tour](repo-tour.md) and [How we build & test](build-and-test.md) |
| Ready to contribute or fork | [Contributing](contributing.md) |

New to Kubernetes itself? The repository ships a staged
[Learning Path](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/LEARNING_PATH.md)
with a primer and role-based tracks.
