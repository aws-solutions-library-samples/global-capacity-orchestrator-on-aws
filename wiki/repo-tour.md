# Repo tour

Every top-level directory carries its own README — this page tells you which
door to open. Descriptions below are the packages' own words, condensed.

## The deployable platform

- [`gco/`](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/gco/README.md)
  — CDK infrastructure stacks, the Kubernetes services that run inside the
  EKS clusters, data models, and configuration. `gco/stacks/` defines the
  cloud infrastructure; `gco/services/` holds the in-cluster microservices
  (health monitor, manifest processor, queue processor, inference monitor,
  inference proxy).
- [`lambda/`](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/lambda/README.md)
  — AWS Lambda functions powering the infrastructure layer: cluster
  operations, API routing, security, and cross-region coordination. Deployed
  by the CDK stacks.
- [`dockerfiles/`](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/dockerfiles/README.md)
  — the six Kubernetes service images (two-stage distroless builds).
- `app.py` and `cdk.json` — the CDK app entry point and the deployment
  configuration (regions, features, thresholds).

## The interfaces

- [`cli/`](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/cli/README.md)
  — the `gco` command-line interface for managing infrastructure, jobs,
  inference endpoints, and operations. Reference:
  [docs/CLI.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/CLI.md).
- [`gco_mcp/`](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/gco_mcp/README.md)
  — an MCP server exposing the CLI and the whole project (docs, examples,
  source, manifests) as tools for LLM interaction. Some tool groups are
  gated behind opt-in feature flags.

![Using the MCP server to write a PI calculation manifest, run it on available capacity, and print the logs](assets/images/gco_mcp_calculating_pi.png)

*The MCP server end to end: writing a manifest, running it on available
capacity, and pulling the logs — without leaving the editor.*

## The material you'll use daily

- [`examples/`](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/examples/README.md)
  — self-contained, ready-to-submit Kubernetes manifests for every workload
  category: GPU jobs, TrainJobs, gang scheduling, Ray, Slurm, KEDA, storage,
  inference servers, DAG pipelines, and more.
- [`docs/`](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/README.md)
  — the guide corpus: concepts, architecture, CLI and API references,
  customization, per-scheduler guides, runbooks, and maintenance. Its README
  is the index with suggested reading orders.
- [`images/`](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/images/README.md)
  — screenshots and visual assets (this wiki serves them too).

## The engineering scaffolding

- [`tests/`](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/tests/README.md)
  — the pytest + BATS suite, organized by component; every test module is
  registered and described in its README.
- [`scripts/`](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/scripts/README.md)
  — utility scripts for development, testing, and operations (CI-only
  scripts live under `.github/scripts/`).
- [`.github/`](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/.github/CI.md)
  — workflows, composite actions, scanner configs, and templates, all
  documented in `.github/CI.md` (see
  [How we build & test](build-and-test.md)).
- [`diagrams/`](https://github.com/awslabs/global-capacity-orchestrator-on-aws/tree/main/diagrams)
  — auto-generated architecture diagrams and code flowcharts, regenerated
  from the CDK app and source so they track reality.

## Suggested first hour

1. Skim the
   [README](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/README.md)
   top-to-bottom — it is the project's front page for a reason.
2. Read
   [docs/CONCEPTS.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/CONCEPTS.md)
   for the mental model, then browse
   [examples/](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/examples/README.md)
   to see the workload surface.
3. Open the package README closest to what you want to change — each one
   orients you locally before you read code.
