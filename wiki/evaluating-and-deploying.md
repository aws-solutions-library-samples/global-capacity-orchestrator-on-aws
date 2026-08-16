# Evaluating & deploying

The
[Quick Start Guide](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/QUICKSTART.md)
is the authoritative walkthrough — it aims to take you from a clean machine
to a running deployment in under 60 minutes, and it marks the exact point
where AWS charges begin. This page summarizes the journey so you know what
you are signing up for.

## What you need

- **Recommended path:** a container runtime (Docker, Finch, or Colima) and
  AWS credentials. The dev container ships everything else — Python, Node.js,
  CDK, kubectl, and the AWS CLI at pinned versions — so you skip dependency
  resolution entirely.
- **Host installs are the advanced path.** GCO pins exact versions of many
  Python packages; the README's
  [Prerequisites](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/README.md#prerequisites)
  section covers the clean-virtualenv route and its known caveats.

## The journey

1. **Clone and build the dev container** — a setup script builds the image
   and wires a `gco` shell function so commands run from your normal shell.
2. **First success milestone** — the CLI runs locally; no AWS charges yet.
3. **Deploy** — one command stands up the global control plane and every
   region configured in `cdk.json`. CDK bootstrap happens automatically.
   *This is the point where billable AWS resources exist.* Helm charts
   converge asynchronously afterwards and can take 10–30+ minutes.
4. **Submit a test job and (optionally) an inference endpoint** — the
   repository ships ready-to-submit
   [example manifests](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/examples/README.md).
5. **Tear down** — one command destroys the stacks with best-effort cleanup
   of known resources.

Prefer to let an agent drive? `gco autopilot` launches a configured Claude
Code session on Amazon Bedrock with the GCO MCP server wired in — deploying,
checking capacity, and submitting jobs conversationally. See
[docs/AUTOPILOT.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/AUTOPILOT.md).

## What it costs

The README's
[sample cost table](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/README.md#sample-cost-table)
breaks down a single-region deployment with default settings:

- **~$210/month fixed platform cost** — the largest items are the EKS
  cluster (~$73), two NAT gateways (~$65), the internal ALB (~$22), Global
  Accelerator (~$18 + transfer), and CloudWatch (~$15).
- **GPU instances dominate** and scale with usage — the table's example
  g5.xlarge runs ~$734/month on-demand or ~$250/month on spot (us-east-1,
  June 2025 pricing).
- Optional services (FSx, Valkey, Aurora, the analytics environment) add
  cost only when enabled. Multi-region deployments scale linearly.

## What you can customize

Deployment configuration is a single file: `cdk.json` defines the regions,
features, and thresholds. The
[Customization Guide](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/CUSTOMIZATION.md)
is the reference for all of it — deployment regions (any SDK-known region in
one partition, no count limit), endpoint access modes, GPU NodePool instance
types and spot preferences, security policy toggles, Helm chart
configuration, and the optional storage and data services. Most optional
features follow the same pattern: off by default, enabled with one toggle,
zero cost until enabled.

## Where to go next

- [QUICKSTART.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/QUICKSTART.md)
  — the full step-by-step walkthrough
- [docs/CUSTOMIZATION.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/CUSTOMIZATION.md)
  — every knob, from regions to NodePools to feature toggles
- [docs/CLI.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/CLI.md)
  — the complete command reference
- [What you can run](what-you-can-run.md) — the workload catalog this
  platform serves
