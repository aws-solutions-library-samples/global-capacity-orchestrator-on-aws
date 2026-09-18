# Get started

This is the fast path: a clean machine to a running job in well under an
hour, with the point where AWS starts billing you marked clearly. Every
step is a summary of the
[Quick Start Guide](https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/blob/main/QUICKSTART.md),
which has the full explanations, the host-install alternative, and the
troubleshooting entries.

## What you need

- AWS credentials (`aws sts get-caller-identity` works) with permission to
  create the platform — EKS, VPC, Lambda, API Gateway, DynamoDB, S3 and
  friends.
- Git and a running container runtime: Docker, Finch, or Podman (Colima
  works too).

That is the whole host-side list. Python, Node.js, the CDK CLI, kubectl, and
the AWS CLI all ship inside the dev container at the exact versions CI uses,
so there is no dependency resolution to fight.

## 1. Clone and build the dev container

```bash
git clone https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws.git
cd global-capacity-orchestrator-on-aws
./scripts/setup-dev-alias.sh   # builds gco-dev from Dockerfile.dev + installs the `gco` shell function
source ~/.zshrc                # or ~/.bashrc — the script prints which file it updated
```

The script detects your runtime, builds the multi-arch `gco-dev` image (a
couple of minutes the first time, cached afterwards), and installs a `gco`
shell function that runs the CLI inside the container with your current
directory mounted at `/workspace` and your AWS credentials passed through.

## 2. First success — no AWS charges yet

```bash
gco --version
```

A `gco, version …` line means the environment is complete. Nothing has been
created in AWS. If the command fails, the
[Common Issues](https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/blob/main/QUICKSTART.md#common-issues)
section of the Quick Start covers every failure mode seen so far; you never
need to read source code to get past this step.

## 3. Deploy the platform

!!! warning "Billing starts here"
    `deploy-all` creates real, paid infrastructure: an EKS Auto Mode cluster,
    VPC and NAT gateways, an internal ALB, API Gateway, Lambda functions, and
    the global control plane. It stays billable until you
    [tear down](#6-tear-down). Fixed platform cost is roughly $210/month for
    one Region before any GPU instance runs — see
    [Evaluating & deploying](evaluating-and-deploying.md#what-it-costs).

```bash
gco stacks deploy-all -y   # CDK bootstrap runs automatically for every Region in cdk.json
```

By default this deploys the global control plane in `us-east-2` and one
workload Region, `us-east-1`; edit `deployment_regions` in `cdk.json` (or run
`gco stacks regions add <region>`) before deploying if you want a different
topology. Expect 30–45 minutes for the EKS cluster.

When `deploy-all` reports success, the scheduler and operator Helm charts
(KEDA, Volcano, KubeRay, cert-manager, Kueue, and any opt-ins) are still
converging in the background — that can take another 10–30 minutes and is by
design, so a slow chart never rolls back the cluster. Watch them with:

```bash
gco stacks addons status -r us-east-1
```

## 4. Run your first job

Submit through SQS: it works with the default private EKS endpoint, needs no
kubectl, and the KEDA-scaled queue processor picks the job up.

```bash
gco jobs submit-sqs examples/simple-job.yaml --region us-east-1   # submit
gco jobs list --all-regions                                       # watch it move to succeeded
gco jobs logs hello-gco -n gco-jobs -r us-east-1                  # read its output
gco jobs delete hello-gco -n gco-jobs -r us-east-1 -y             # clean up
```

The same manifest also goes through the IAM-authenticated REST API
(`gco jobs submit`) or the global DynamoDB queue (`gco queue submit`);
[Core Concepts](https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/blob/main/docs/CONCEPTS.md#manifest-submission)
explains when to use which. A few things worth trying next:

```bash
gco capacity check --instance-type g4dn.xlarge --region us-east-1   # is there GPU capacity right now?
gco capacity recommend-region --gpu                                 # which Region should a GPU job go to?
gco status                                                          # fleet-wide health in one screen
gco dag run examples/pipeline-dag.yaml --region us-east-1           # a multi-step pipeline
```

Every workload category — GPU jobs, Kubeflow `TrainJob`s, gang scheduling,
Ray, Slurm, KEDA-scaled jobs, inference servers, storage patterns — ships as a
ready-to-submit manifest in
[examples/](https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/blob/main/examples/README.md).

## 5. Deploy an inference endpoint (optional)

```bash
gco inference deploy my-llm \
  -i vllm/vllm-openai:v0.29.0 \
  --gpu-count 1 \
  -e MODEL=meta-llama/Llama-3.1-8B-Instruct \
  -r us-east-1

gco inference status my-llm      # per-Region sync state
gco inference list
gco inference delete my-llm -y   # when you are done
```

One command per endpoint, any number of Regions (`-r` is repeatable). The
regional inference monitor creates the Deployment, Service, and autoscaling
objects for you; canaries, scaling, and model-weight sync from S3 are in the
[Inference Guide](https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/blob/main/docs/INFERENCE.md).

## Prefer to talk to it?

```bash
gco autopilot                  # Claude Code on Amazon Bedrock, GCO MCP server preconfigured
gco autopilot --engine codex   # the same session with OpenAI Codex
```

Autopilot installs nothing without asking, wires the project's MCP server and
recommended companions into the agent, and hands you a session that can
deploy, check capacity, and submit jobs conversationally. To use the MCP
server from your own IDE instead (Cursor, Kiro, Claude Desktop), the
[MCP server README](https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/blob/main/gco_mcp/README.md)
has the copy-paste configuration for each client.

## 6. Tear down

```bash
gco stacks destroy-all -y
```

Removes every stack in dependency order with best-effort cleanup of the
resources CloudFormation leaves behind (orphaned volumes, log groups, empty
security groups). Run it whenever you are finished experimenting — the fixed
platform cost accrues while the stacks exist.

## When something goes wrong

- `gco cluster doctor --region us-east-1` diagnoses EKS access one layer at
  a time (reachability, authentication, authorization) and names the fix.
- `gco status` shows which stack, queue, or Region is unhealthy.
- The Quick Start's
  [Common Issues](https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/blob/main/QUICKSTART.md#common-issues)
  and the
  [Troubleshooting Guide](https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/blob/main/docs/TROUBLESHOOTING.md)
  cover deployment, networking, pod, and storage problems, and the
  [Runbooks](https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/blob/main/docs/RUNBOOKS.md)
  handle incidents step by step.

## Where to go next

- [Evaluating & deploying](evaluating-and-deploying.md) — what it costs,
  what you can customize, and how upgrades work
- [What you can run](what-you-can-run.md) — the workload catalog
- [docs/CLI.md](https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/blob/main/docs/CLI.md)
  — every `gco` command, alphabetically
