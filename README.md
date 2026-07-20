<div align="center">

<h1>Automated Deployment of EKS AutoMode Clusters with Global Capacity Orchestrator (GCO) on AWS</h1>

<p><b><i>One API. Every Accelerator. Any Region.</i></b></p>

<p>Multi-region accelerated-compute orchestration for AWS — NVIDIA GPUs, AWS Trainium, AWS Inferentia, and CPU (amd64 + arm64 / Graviton) — with capacity-aware placement workflows, spot fallback, and autoscaling inference endpoints. Commercial <code>aws</code> deployments add automatic failover and latency-aware routing through one workload API; other partitions use IAM-authenticated regional workload APIs, all through the same CLI.</p>

<!-- BEGIN BADGE TABLE -->
<p>
  <a href="https://github.com/awslabs/global-capacity-orchestrator-on-aws/actions/workflows/unit-tests.yml"><img src="https://github.com/awslabs/global-capacity-orchestrator-on-aws/actions/workflows/unit-tests.yml/badge.svg?branch=main" alt="Unit Tests"></a>
  <a href="https://github.com/awslabs/global-capacity-orchestrator-on-aws/actions/workflows/integration-tests.yml"><img src="https://github.com/awslabs/global-capacity-orchestrator-on-aws/actions/workflows/integration-tests.yml/badge.svg?branch=main" alt="Integration Tests"></a>
  <a href="https://github.com/awslabs/global-capacity-orchestrator-on-aws/actions/workflows/security.yml"><img src="https://github.com/awslabs/global-capacity-orchestrator-on-aws/actions/workflows/security.yml/badge.svg?branch=main" alt="Security"></a>
  <a href="https://github.com/awslabs/global-capacity-orchestrator-on-aws/actions/workflows/lint.yml"><img src="https://github.com/awslabs/global-capacity-orchestrator-on-aws/actions/workflows/lint.yml/badge.svg?branch=main" alt="Linting"></a>
  <a href="https://awslabs.github.io/global-capacity-orchestrator-on-aws/"><img src="https://img.shields.io/endpoint?url=https%3A%2F%2Fawslabs.github.io%2Fglobal-capacity-orchestrator-on-aws%2Fcoverage-badge.json" alt="Coverage"></a>
</p>
<!-- END BADGE TABLE -->

<details>
<summary>🎬 Live demo recording</summary>

![GCO Live Demo](demo/live_demo.gif)

*`gco` CLI demo: capacity discovery, cost visibility, 5 schedulers (Volcano, Kueue, YuniKorn, Slurm, KEDA), FSx, Valkey, live LLM inference, and EFS — all against one already-deployed cluster. ([source](demo/live_demo.sh) · [re-record](demo/record_demo.sh))*

</details>

<details>
<summary>📦 Deploy recording</summary>

![GCO Deploy](demo/deploy.gif)

*Fresh `gco stacks deploy-all -y` from a clean account ([re-record](demo/record_deploy.sh))*

</details>

<details>
<summary>🗑️ Destroy recording</summary>

![GCO Destroy](demo/destroy.gif)

*Full teardown with `gco stacks destroy-all -y` ([re-record](demo/record_destroy.sh))*

</details>

</div>

**What it does.** Spins up [EKS Auto Mode](docs/CONCEPTS.md#eks-auto-mode) clusters across any number of SDK-known CloudFormation Regions in one AWS partition. In commercial `aws`, [Global Accelerator](docs/CONCEPTS.md#global-routing) provides latency-aware anycast routing and automatic failover behind the global workload API; other partitions use IAM-authenticated regional workload APIs while retaining the aggregate global API. Capacity tools and auto-region queue/CLI workflows can select a target Region, EKS Auto Mode provisions matching nodes, and shared storage can persist workload outputs. Network routing never substitutes for live GPU-capacity placement.

**Who it's for.** Teams running accelerated workloads — LLM training and inference, batch ML, HPC, and general CPU jobs — that need multi-region redundancy, capacity discovery, and IAM-based access without per-cluster kubeconfig distribution. GCO includes the EKS Auto Mode `system` and `general-purpose` NodePools plus project-managed GPU x86, GPU ARM, inference, EFA, Mooncake EFA, Neuron, and CPU NodePools.

**Why it's different.** Capacity-aware placement tools and auto-region workflows, partition-aware authenticated routing, full-stack observability (CloudWatch dashboards, alarms, SNS), and a CDK app validated across 20+ config matrix combinations in CI.

---

**Deploy everything and tear it all down with one command each:**

```bash
gco stacks deploy-all -y      # stand up every region defined in cdk.json
gco stacks destroy-all -y     # destroy every stack across every region — no orphaned resources
```

**Recommended: run everything from the dev container.** GCO pins exact versions of a lot of Python packages (CDK, AWS SDKs, FastAPI, mypy, Ruff, etc.), and installing them on top of an existing Python environment is the most common source of "it doesn't install" reports. The dev container ships a fully resolved environment (Python 3.14, Node.js 24, CDK, kubectl, AWS CLI, Docker CLI + Buildx, all Python deps) so you skip the whole problem.

**Let the setup script do it all — build the image and wire up a `gco` command for you.** You shouldn't hand-write `docker run …` invocations or live inside an interactive container shell — `scripts/setup-dev-alias.sh` builds the `gco-dev` image and installs the wiring, so `gco` runs straight from your normal shell:

```bash
git clone git@github.com:awslabs/global-capacity-orchestrator-on-aws.git
cd global-capacity-orchestrator-on-aws

./scripts/setup-dev-alias.sh   # builds gco-dev from Dockerfile.dev + installs the `gco` shell function
source ~/.zshrc                # or ~/.bashrc — the script prints which file it updated
```

`setup-dev-alias.sh` detects your container runtime (Docker, Finch, or Podman), builds the `gco-dev` image from `Dockerfile.dev` (re-running rebuilds it, so a stale image is refreshed automatically — pass `--no-build` to skip), wires up the correct socket pass-through, and installs an idempotent `gco` shell *function* (not a bare alias, so arguments and pipes forward correctly and a TTY is attached only when one is present) into your profile. From then on, run GCO straight from your repo checkout — each command executes inside the dev container against your current directory:

```bash
gco --help                # explore every command
gco stacks deploy-all -y  # stand up every region defined in cdk.json
gco stacks destroy-all -y # tear it all down
```

Re-run the script whenever you switch runtimes, or use `--print` to preview the function, `--runtime <name>` to force one, and `--rc <path>` to target a specific profile.

The function shares your host Docker socket with every `gco` call, and `gco stacks deploy-all` uses it for much more than bundling Lambda assets: through your host Docker daemon it builds and bundles the Lambda function assets (as `linux/amd64`, cross-built via Buildx so this works on Apple Silicon / arm64) and — when the Volcano image mirror is enabled in `cdk.json` — mirrors third-party images such as the Volcano scheduler from Docker Hub into your ECR before the Helm install runs. The same socket also backs `gco images build` / `push`. See [Prerequisites](#prerequisites) for Colima/Finch socket paths and the security note about host-socket pass-through. *Finch users:* Finch runs in its own VM with no host Docker socket to share, so the function omits the socket mount — everyday commands work as-is, while build-heavy ones like `deploy-all` run on the host with Finch as the CDK builder.

<details>
<summary>Prefer an interactive container shell instead?</summary>

You can also build the image yourself and drop straight into the dev container, running `gco` from inside it — handy for ad-hoc tools and exploration. The `-v /var/run/docker.sock:/var/run/docker.sock` mount gives the container's Docker CLI access to your host daemon for the asset builds, image mirroring, and bundling described above:

```bash
docker build -f Dockerfile.dev -t gco-dev .
docker run -it --rm \
  -v ~/.aws:/root/.aws:ro \
  -v $(pwd):/workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -w /workspace \
  gco-dev
```

</details>

<details>
<summary>Prefer to install on your host? (advanced — the dev container is recommended)</summary>

Host installs are the advanced, non-recommended path. GCO pins exact versions of many Python packages, so installing on top of an existing Python environment frequently fails with dependency-resolver errors (`ResolutionImpossible`). The dev container shown above is the recommended path — it ships every dependency at the pinned versions — and the [Quick Start Guide](QUICKSTART.md) walks through it end to end. If you still want a host install, use a clean virtual environment or pipx.

```bash
git clone git@github.com:awslabs/global-capacity-orchestrator-on-aws.git
cd global-capacity-orchestrator-on-aws && pipx install -e .
```

</details>

See the [Quick Start](#quick-start) for the full install + first-job walkthrough, or [`docs/CLI.md`](docs/CLI.md) for every CLI command.

> **💡 New to the codebase?** GCO ships with the **GCO MCP server** — an [MCP server](gco_mcp/) exposing 125 tools by default (up to 165 with feature flags) that index the whole project: docs, examples, source code, K8s manifests, and scripts. Connect it to an AI-powered IDE with MCP support (like [Kiro](https://kiro.dev)) and explore GCO conversationally — ask questions about the codebase instead of reading repository files directly: *"How does region recommendation work?"*, *"Walk me through the inference deployment flow"*. See [gco_mcp/README.md](gco_mcp/README.md).

<details>
<summary><b>Table of Contents</b></summary>

- [Project Tenets](#project-tenets)
- [Why GCO?](#why-gco)
- [Quick Start](#quick-start)
- [Architecture Overview](#architecture-overview)
- [AWS Services in this Guidance](#aws-services-in-this-guidance)
- [Sample Cost Table](#sample-cost-table)
- [Supported AWS Regions](#supported-aws-regions)
- [Key Features](#key-features)
- [Documentation](#documentation)
- [Project Structure](#project-structure)
- [Contributing](#contributing)
- [License](#license)
- [Support](#support)
- [Security](#security)

</details>

## Project Tenets

GCO is guided by the prioritized [project tenets](TENETS.md), beginning with workload,
data, and account safety and anchored by the north star **One API. Every Accelerator.
Any Region.** The tenets define how the project resolves trade-offs across truthful
state, security, regional behavior, accelerator policy, automation, recovery,
operations, cost, and maintainability. Earlier tenets outrank later ones; durable
exceptions require an [Architecture Decision Record](docs/adr/README.md).

## Why GCO?

Running GPU workloads at scale is hard. You need to find regions with available capacity, provision clusters, handle authentication, deal with failover, and persist outputs after pods terminate. GCO solves all of this with a single deployable platform.

| Challenge | Traditional Approach | With GCO |
|-----------|---------------------|--------------|
| GPU availability | Manually check each region | Capacity tools and auto-region workflows compare configured regions |
| Node provisioning | Pre-provision or wait for scaling | EKS Auto Mode provisions on-demand |
| Multi-region ops | Manage clusters separately | One platform across unlimited SDK-known Regions in one partition |
| Authentication | Configure per-cluster access | IAM-based, uses existing AWS credentials |
| Job outputs | Lost when pods terminate | Persisted to EFS/FSx storage |
| Inference serving | Deploy and manage per-region | Deploy once across selected Regions |
| Failover | Manual intervention required | Automatic via Global Accelerator in `aws`; explicit regional selection elsewhere |

**When to use GCO:**

- You need to run GPU workloads (training, inference, batch processing)
- You want to deploy inference endpoints across multiple regions with a single command
- You want multi-region redundancy without managing multiple clusters
- You prefer IAM authentication over kubeconfig management
- You need job outputs to persist after completion

## Quick Start

### Install and Deploy

The fastest, most reliable path is the dev container — it sidesteps the dependency-conflict issues that come with installing GCO's pinned Python packages on top of your existing Python environment.

Run the setup script — it builds the `gco-dev` image (Python, Node.js, CDK, kubectl, and the AWS CLI all pinned and pre-installed) and wires up `gco` so it works straight from your shell, with no hand-written `docker run …` and no interactive container:

```bash
./scripts/setup-dev-alias.sh   # builds gco-dev from Dockerfile.dev + installs the `gco` shell function
source ~/.zshrc                # or ~/.bashrc — the script prints which file it updated
```

Then deploy everything — CDK bootstrap runs automatically for every region defined in `cdk.json`:

```bash
gco stacks deploy-all -y
```

Prefer an interactive container shell, or want to install on your host instead? See the [Prerequisites](#prerequisites) and [QUICKSTART.md](QUICKSTART.md) for those options and known caveats.

> **Heads up — Helm charts finish installing in the background.** When `deploy-all` reports the cluster `CREATE_COMPLETE`, the scheduler/operator Helm charts (KEDA, Volcano, KubeRay, cert-manager, Kueue, …) have only been *kicked off*; they converge asynchronously and can take **10–30+ minutes** to all become ready. This is intentional — a slow chart never rolls back the cluster. Track progress with `gco stacks addons status -r <region>` and re-converge any failures with `gco stacks addons install -r <region>`. See [docs/CUSTOMIZATION.md](docs/CUSTOMIZATION.md#helm-chart-configuration).

<!-- -->

> **Optional:** configure kubectl access (requires `PUBLIC_AND_PRIVATE` endpoint mode). The default endpoint mode is `PRIVATE` — see [docs/CUSTOMIZATION.md](docs/CUSTOMIZATION.md) for details. Most users don't need this; submit jobs via SQS or API Gateway instead.

### Submit Your First Job

Check GPU capacity in a region before you submit:

```bash
gco capacity check --instance-type g4dn.xlarge --region us-east-1
```

Submit a job using whichever path fits your setup — via SQS (recommended), via the global DynamoDB queue, via API Gateway, or directly through kubectl:

```bash
gco jobs submit-sqs examples/simple-job.yaml --region us-east-1
gco queue submit examples/simple-job.yaml --region us-east-1
gco jobs submit examples/simple-job.yaml -n gco-jobs
gco jobs submit-direct examples/simple-job.yaml -r us-east-1
```

Check status and pull logs:

```bash
gco jobs list --all-regions
gco jobs logs hello-gco -n gco-jobs -r us-east-1
```

### Deploy an Inference Endpoint

```bash
gco inference deploy my-llm -i vllm/vllm-openai:v0.24.0 --gpu-count 1
gco inference status my-llm
gco inference scale my-llm --replicas 3
```

See the [Quick Start Guide](QUICKSTART.md) for the full step-by-step walkthrough, or the [CLI Reference](docs/CLI.md) for all available commands.

## Architecture Overview

![Generated GCO infrastructure architecture](diagrams/infra_diagrams/full-architecture.png)

*Figure 1: Generated CDK architecture for the global control plane and regional EKS data planes*

### Reference Architecture Diagrams

These curated views complement the generated CDK diagram with the multi-region platform, regional EKS data plane, and security/request flow.

<p align="center">
  <a href="images/gco_ref_architecture_part1.png"><img src="images/gco_ref_architecture_part1.png" alt="GCO multi-region reference architecture" width="32%"></a>
  <a href="images/gco_ref_architecture_part2.png"><img src="images/gco_ref_architecture_part2.png" alt="GCO regional EKS reference architecture" width="32%"></a>
  <a href="images/gco_ref_architecture_part3.png"><img src="images/gco_ref_architecture_part3.png" alt="GCO security controls and request flow" width="32%"></a>
</p>

### Multi-Region Reference Architecture workflow

The generated reference architecture shows the commercial `aws` workload path. Other partitions retain the global aggregate API but route workload control and inference through each Region's IAM-authenticated bridge.

1. **DevOps / Platform engineers** own the deployment. They configure the platform through `cdk.json` and drive everything from the `gco` CLI.
2. The **AWS CDK app** synthesises and deploys the GCO stacks with a single `gco stacks deploy-all`, provisioning the global control plane and one regional stack per target region.
3. **Users** submit jobs and inference requests through the `gco` CLI, which signs every call with **AWS SigV4** credentials.
4. In commercial `aws`, **Amazon API Gateway** is edge-optimized and is the global workload and aggregate entry point. In other partitions it is regional and aggregate-only. Every exposed method enforces **IAM (SigV4) authentication** before integration.
5. In `aws`, route-specific **AWS Lambda proxies** sign workload requests with a short-lived HMAC envelope derived from a rotating **AWS Secrets Manager** key; `/api/v1/*` stays buffered while `/inference/*` streams. Other partitions omit these global workload proxies and use equivalent VPC proxies behind the regional APIs.
6. In `aws`, **AWS Global Accelerator** routes workload requests over the AWS backbone to a healthy registered Region. Other partitions create no accelerator resources.
7. A regional internal **AWS Application Load Balancer** terminates deployment-local private-root TLS from either Global Accelerator (`aws`) or the regional VPC proxy, then sends HTTP to shared platform Ingress targets.
8. Each region runs an **Amazon EKS Auto Mode cluster** with built-in `system` and `general-purpose` NodePools plus project-managed GPU, inference, EFA, Mooncake EFA, Neuron, and CPU NodePools. Platform services include the Health Monitor, Manifest Processor, Queue Processor, Inference Monitor, and dedicated Inference Proxy.

Below is the per-region workflow for a single regional stack.

### Regional Architecture workflow

1. An internal **Application Load Balancer** created from the shared platform Ingress accepts only HTTPS/443 with a rotating regional ACM leaf, then forwards HTTP to cluster services after TLS termination.
2. The **Amazon EKS Auto Mode cluster** is the heart of the regional stack, hosting platform services and user workloads with a private API endpoint by default.
3. **NodePools** provision capacity on demand: built-in `system` and `general-purpose`, plus `gpu-x86-pool`, `gpu-arm-pool`, `gpu-inference-pool`, `gpu-efa-pool`, `mooncake-efa-pool`, `neuron-pool`, and `cpu-general-pool`.
4. **Workloads and platform services** run across namespaces: `gco-system` (Health Monitor, Manifest Processor, Queue Processor, Inference Monitor, Inference Proxy) and `gco-jobs` / `gco-inference` (training and batch jobs, inference endpoints, and job DAG pipelines).
5. **Storage and data services** back workloads: Amazon EFS, optional FSx for Lustre, optional Valkey, optional Aurora pgvector, and Amazon S3 for KMS-encrypted model weights.
6. An always-deployed **Regional API Gateway bridge** gives the aggregator a SigV4-authenticated path to the VPC Lambda and internal ALB. Direct same-account access is optional through `regional_api_enabled` in `aws` and enabled automatically as the required workload ingress elsewhere.
7. **Regional AWS services** complete the stack: Amazon SQS for job ingestion, DynamoDB-backed state where applicable, and Amazon CloudWatch metrics and logs.

<details>
<summary>Infrastructure diagram generation details</summary>

Regenerate the full architecture and every per-stack view with `python diagrams/infra_diagrams/generate.py`. The generator synthesizes the current CDK app through cdk-dia so the committed diagrams track the deployed resource graph. See [`diagrams/infra_diagrams/README.md`](diagrams/infra_diagrams/README.md) for per-stack flags (`--stack global|api-gateway|regional|regional-api|monitoring|analytics|all`).

</details>

Flowcharts of Lambda handlers, CLI commands, stack constructors, and MCP control paths live under [`diagrams/code_diagrams/`](diagrams/code_diagrams/README.md).

> A regional stack can be deployed to any CloudFormation Region known to the installed AWS SDK. Add or remove Regions in `deployment_regions.regional`; all configured Regions must belong to one AWS partition, and GCO imposes no count limit.

### Security Model

Six complementary controls protect backend requests:

1. **IAM authentication** — API Gateway validates AWS credentials with SigV4.
2. **TLS trust separation** — API Gateway uses AWS-managed TLS; proxy-to-ALB traffic uses a deployment-local private root and explicit `backend.<project>.gco.internal` SNI/hostname verification.
3. **Request-bound HMAC** — a trusted Lambda signs the version, timestamp, nonce, method, exact target, and body digest with a rotating key that is never transmitted. HMAC provides integrity/freshness/replay defense, not encryption.
4. **Private backend exposure** — regional ALBs are internal and the EKS API endpoint is private by default.
5. **Freshness and integrity validation** — backend middleware rejects stale, altered, or replayed envelopes.
6. **IRSA / EKS Pod Identity** — pods receive scoped AWS permissions without static workload credentials.

```text
Commercial `aws` request flow:
User → API Gateway (AWS TLS + SigV4) → Lambda (HMAC)
  → Global Accelerator (TCP/443 pass-through) → internal ALB (private-root TLS)
  → AuthenticationMiddleware (HTTP after ALB termination)

Other partitions:
User → Regional API Gateway (AWS TLS + SigV4) → VPC Lambda (HMAC)
  → internal ALB (private-root TLS) → AuthenticationMiddleware
```

Every Region's API bridge is required for aggregator fan-out. [Direct regional
access](docs/CUSTOMIZATION.md#regional-api-gateway-aggregation-bridge-and-direct-regional-access)
is optional for same-account callers in `aws` and enabled automatically as the
supported workload ingress in other partitions.

See [Architecture Details](docs/ARCHITECTURE.md) for the full deep dive.

## AWS Services in this Guidance

| AWS Service | Usage |
|-------------|-------|
| [Amazon EKS](https://aws.amazon.com/eks/) | Kubernetes control plane and Auto Mode compute (GPU, Trainium, Inferentia, CPU nodepools) |
| [AWS Global Accelerator](https://aws.amazon.com/global-accelerator/) | Anycast endpoint with health-based cross-region routing and automatic failover |
| [Amazon API Gateway](https://aws.amazon.com/api-gateway/) | IAM-authenticated (SigV4) REST entry point for job submission and inference |
| [AWS Lambda](https://aws.amazon.com/lambda/) | HMAC-signing proxy functions, Global Accelerator registration, manifest application, and Helm chart installation orchestration |
| [AWS Step Functions](https://aws.amazon.com/step-functions/) | Orchestrates Helm chart installs — one state per chart with per-chart retry and backoff |
| [Amazon DynamoDB](https://aws.amazon.com/dynamodb/) | Inference endpoint desired-state store, job queue state, and template storage |
| [Amazon SQS](https://aws.amazon.com/sqs/) | Regional job ingestion queue with dead-letter queue and KEDA-driven scale-to-zero consumer |
| [Amazon S3](https://aws.amazon.com/s3/) | Model weight storage (KMS-encrypted), cluster shared bucket, CDK asset staging |
| [Amazon EFS](https://aws.amazon.com/efs/) | Shared elastic storage for job outputs, model weights, and inter-pod data sharing |
| [Amazon FSx for Lustre](https://aws.amazon.com/fsx/lustre/) | Optional high-performance parallel file system for ML training workloads |
| [Amazon ElastiCache (Valkey)](https://aws.amazon.com/elasticache/) | Optional serverless key-value cache for prompt caching and session state |
| [Amazon Aurora](https://aws.amazon.com/rds/aurora/) | Optional Serverless v2 PostgreSQL with pgvector for RAG and semantic search |
| [Amazon ECR](https://aws.amazon.com/ecr/) | Container image registry with cross-region replication for platform and user images |
| [Amazon CloudWatch](https://aws.amazon.com/cloudwatch/) | Metrics, logs, alarms, dashboards, and Container Insights for GPU utilization |
| [Amazon SNS](https://aws.amazon.com/sns/) | Alert notifications for drift detection, health issues, and capacity events |
| [AWS Secrets Manager](https://aws.amazon.com/secrets-manager/) | Rotating HMAC signing key plus the KMS-encrypted deployment-local TLS root state |
| [AWS KMS](https://aws.amazon.com/kms/) | Encryption keys for S3 model buckets, EFS, application secrets, and the backend TLS root secret |
| [AWS IAM](https://aws.amazon.com/iam/) | IRSA roles for pod-level AWS access, service roles, and SigV4 authentication |
| [AWS CDK](https://aws.amazon.com/cdk/) | Infrastructure as code — synthesizes, validates (cdk-nag), and deploys all stacks |
| [Amazon VPC](https://aws.amazon.com/vpc/) | Network isolation with public/private subnets, NAT Gateways, and VPC endpoints |
| [AWS Cost Explorer](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/) | Cost tracking by service, region, and workload via the `gco costs` commands |

## Sample Cost Table

The following estimates are for a single-region deployment with default settings. Multi-region deployments scale linearly. Costs vary by region, instance type, and utilization.

| Resource | Configuration | Estimated Monthly Cost (USD) |
|----------|--------------|------------------------------|
| EKS cluster | 1 cluster (Auto Mode) | ~$73 |
| NAT Gateways | 2 (high availability) | ~$65 |
| Application Load Balancer | 1 (shared by all services) | ~$22 |
| Global Accelerator | 1 accelerator + data transfer | ~$18 + transfer |
| Lambda functions | ~8 functions, minimal invocations | < $1 (often $0 within free tier) |
| Step Functions | ~10 state transitions per deploy | < $1 |
| DynamoDB | On-demand, low throughput | ~$5 |
| SQS | Standard queue, low message volume | < $1 |
| S3 | Model storage (varies with model size) | ~$2 (10 GB + API requests) |
| EFS | Elastic storage (varies with usage) | ~$3 (10 GB stored) |
| CloudWatch | Logs, metrics, Container Insights | ~$15 |
| ECR | Image storage + replication | ~$5 |
| Secrets Manager | 2 secrets with managed lifecycle (HMAC key and backend TLS root) | < $2 |
| **Subtotal (platform, no GPU workloads)** | | **~$210/month** |
| GPU instances (example) | 1× g5.xlarge on-demand, 24/7 (us-east-1) | ~$734 |
| GPU instances (spot) | 1× g5.xlarge spot, 24/7 (us-east-1) | ~$250 |

**Notes:**

- Platform costs (~$210/month) are fixed regardless of workload volume.
- GPU costs dominate and scale with the number of instances and hours run. Use `gco costs summary` to track actual spend.
- GPU estimates assume an on-demand g5.xlarge in us-east-1 at ~$1.006/hr (~$734/month over 730 hours); rates vary by region and instance type.
- Optional services (FSx, Valkey, Aurora) add additional cost depending on configuration.
- The cost table above uses US East (N. Virginia) pricing as of June 2025.

## Supported AWS Regions

GCO can be deployed to any AWS region that supports Amazon EKS, AWS Global Accelerator, and the other services listed above. The deployment regions are configured in `cdk.json` under `deployment_regions.regional`.

**Tested and validated regions:**

| Region | Location |
|--------|----------|
| us-east-1 | N. Virginia |
| us-east-2 | Ohio |
| us-west-2 | Oregon |
| eu-west-1 | Ireland |
| eu-west-2 | London |
| eu-central-1 | Frankfurt |
| ap-southeast-1 | Singapore |
| ap-northeast-1 | Tokyo |

**Adding a new region:**

```json
// cdk.json
{
  "context": {
    "deployment_regions": {
      "regional": ["us-east-1", "eu-west-1", "ap-northeast-1"]
    }
  }
}
```

Then redeploy: `gco stacks deploy-all -y`. CDK bootstrap runs automatically for new regions.

GPU instance availability varies by region. Use `gco capacity check -i <instance-type> -r <region>` or `gco capacity recommend-region --gpu` to find regions with available GPU capacity before deploying workloads.

## Key Features

### Compute & Orchestration

- **EKS Auto Mode** with automatic node provisioning — no pre-scaling needed
- **GPU and accelerator support** through `gpu-x86-pool`, `gpu-arm-pool`, `gpu-inference-pool`, `gpu-efa-pool`, `mooncake-efa-pool`, and `neuron-pool`, plus built-in and project-scoped CPU pools
- **Multiple submission methods**: API Gateway, SQS queues, DynamoDB job queue, or direct kubectl
- **Job pipelines (DAGs)**: Multi-step ML pipelines with dependency ordering and failure handling
- **Helm-managed ecosystem**: mandatory KEDA; EFA and Neuron device plugins; Volcano, KubeRay, cert-manager, optional kube-prometheus-stack, and Kueue; opt-in Slurm/Slinky and YuniKorn

### Inference Serving

- **Multi-region inference**: Deploy endpoints (vLLM, TGI, Triton, TorchServe, SGLang) across regions with a single command
- **Canary deployments**: A/B test new model versions with weighted traffic routing
- **Model weight management**: Central S3 bucket with KMS encryption, automatic sync to each region
- **Spot instance support**: Run inference on spot GPUs for significant cost savings
- **Autoscaling**: HPA-based scaling with CPU/memory metrics

### Networking & Security

- **Global Accelerator**: Single anycast endpoint with automatic failover
- **IAM authentication**: SigV4 at the API Gateway — no kubeconfig distribution
- **Infrastructure policy validation**: cdk-nag v3 rule packs for AWS Solutions, HIPAA, NIST 800-53, PCI DSS, and Serverless findings (these checks are not certifications)
- **Network policies**: Default-deny with explicit allow rules for all service communication
- **EFA support**: Optional Elastic Fabric Adapter for high-bandwidth distributed training and NIXL-based inference (toggle on/off)

### Storage & Data

- **EFS**: Shared elastic storage for job outputs that persist after pod termination
- **FSx for Lustre**: Optional high-performance parallel file system for ML training (toggle on/off)
- **Valkey cache**: Optional serverless key-value cache for prompt caching and session state
- **Aurora pgvector**: Optional serverless vector database for RAG, semantic search, and embedding storage

### Operations

- **Cost visibility**: Track spend by service, region, and workload via Cost Explorer integration
- **Auto-bootstrap**: CDK bootstrap runs automatically for new regions during deploy
- **Multi-region monitoring**: CloudWatch dashboards, alarms, and SNS alerts across all regions

### ML & Analytics Environment

- **ML & Analytics Environment**: Optional SageMaker Studio domain + EMR Serverless + Cognito user pool for interactive notebook analytics, with an always-on `Cluster_Shared_Bucket` that all cluster jobs can read and write. Off by default — enable with `gco analytics enable`. See [Analytics Guide](docs/ANALYTICS.md).

### Mission

Goal-directed iteration loop for orchestrated workflows. The operator declares a natural-language directive plus machine-checkable success criteria, a tool allowlist, and a budget; Mission runs five-phase iterations (propose → execute → observe → evaluate → decide) until a verdict is reached. Off by default — enable with `GCO_ENABLE_MISSION=true`. See [Mission Guide](docs/MISSION.md).

- **Deterministic verdict cascade** with optional advisory LLM sampling (MCP host or Amazon Bedrock). Sampling shapes only the next strategy; it never moves the verdict.
- **Budget caps** on iterations and wall clock — the engine terminates cleanly when any cap fires. Cost guardrails live out-of-band via AWS Budgets and Cost Anomaly Detection at the account level.
- **Scripted strategies** opt-in: an AST-validated Python sandbox with bounded duration and memory limits.
- **CLI + MCP surface**: ten `gco mission` subcommands (including the chained `gco mission run` that scaffolds criteria and drives a session to completion in one call) and matching MCP tools, plus three `mission://sessions/{id}` resource templates.

## Documentation

**New to GCO?** Start here:

| Your Goal | Read This |
|-----------|-----------|
| Understand the project's north star and decision priorities | [Project Tenets](TENETS.md) |
| Understand what GCO does | [Core Concepts](docs/CONCEPTS.md) |
| Follow a guided learning path (new to GCO or Kubernetes) | [Learning Path](docs/LEARNING_PATH.md) |
| Get running in under 60 minutes | [Quick Start Guide](QUICKSTART.md) |
| Learn the architecture | [Architecture Details](docs/ARCHITECTURE.md) |
| Browse every guide in one place | [Documentation Index](docs/README.md) |

**Day-to-day operations:**

| Your Goal | Read This |
|-----------|-----------|
| CLI commands and usage | [CLI Reference](docs/CLI.md) |
| Deploy inference endpoints | [Inference Guide](docs/INFERENCE.md) |
| Use the REST API directly | [API Reference](docs/API.md) |
| Fix issues | [Troubleshooting](docs/TROUBLESHOOTING.md) |
| Respond to incidents | [Operational Runbooks](docs/RUNBOOKS.md) |
| Run interactive notebook analytics | [Analytics Guide](docs/ANALYTICS.md) |
| Drive a goal-directed iteration loop | [Mission Guide](docs/MISSION.md) |
| Perform routine maintenance & upgrades | [Maintenance Guide](docs/MAINTENANCE.md) |

**Customization and development:**

| Your Goal | Read This |
|-----------|-----------|
| Add regions, tune nodepools, enable FSx | [Customization Guide](docs/CUSTOMIZATION.md) |
| Choose a scheduler for your workload | [Schedulers & Orchestrators](docs/SCHEDULERS.md) |
| Mirror Docker Hub images into ECR (Volcano) | [Image Mirror](docs/IMAGE_MIRROR.md) |
| Configure the SQS queue processor | [Queue Processor Config](docs/CUSTOMIZATION.md#queue-processor-sqs-consumer) |
| Contribute to the project | [Contributing](CONTRIBUTING.md) |
| API client examples (Python, curl, AWS CLI) | [Client Examples](docs/client-examples/README.md) |
| IAM policy templates | [IAM Policies](docs/iam-policies/README.md) |
| Presentation slides and demo scripts | [Demo Starter Kit](demo/README.md) |

### Prerequisites

**Recommended path — dev container only:**

- AWS CLI configured with appropriate credentials (or `~/.aws` to mount in)
- Docker (or Finch / Colima) — that's it. The container ships Python 3.14, Node.js 24, CDK, kubectl, AWS CLI, and Docker CLI + Buildx at pinned versions.

```bash
docker build -f Dockerfile.dev -t gco-dev .
docker run -it --rm -v ~/.aws:/root/.aws:ro -v $(pwd):/workspace -w /workspace gco-dev
```

For `gco stacks deploy-all`, the CLI and `cdk deploy` drive Docker for several steps — building and bundling the Lambda function assets (as `linux/amd64`, via Buildx on Apple Silicon), and, when the Volcano image mirror is enabled, mirroring third-party images from Docker Hub into ECR before the Helm install. `setup-dev-alias.sh` already wires the host Docker socket into the `gco` function; if you instead run the container by hand, mount the socket so the container's CLI talks to your host daemon (works with Docker Desktop on macOS/Windows, with Docker on Linux, and with Colima on macOS — see `Dockerfile.dev` for Colima-specific socket paths):

```bash
docker run --rm -it \
  -v ~/.aws:/root/.aws:ro \
  -v $(pwd):/workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -w /workspace \
  gco-dev gco stacks deploy-all -y
```

This is host-socket pass-through, not true Docker-in-Docker. Anyone with access to the container has root-equivalent access to the host Docker daemon, so keep the container on a trusted host.

**Host install path (advanced):**

- AWS CLI configured with appropriate credentials
- Python 3.14+ and Node.js 24 (use `.nvmrc`)
- npm 11.18.0 and the repository's locked tooling graph: run `npm ci --ignore-scripts --no-audit --no-fund` at the repository root; `gco` prefers its local `node_modules/.bin/cdk` over a global CLI
- Docker or Finch (for building container images)
- A **clean** Python virtual environment or pipx — GCO pins exact versions of many packages, so installing it into an existing environment will commonly fail with dependency-resolver errors. If you hit `ResolutionImpossible`, switch to the dev container instead of debugging your local env.

## Project Structure

```text
.
├── app.py                               # CDK app entry point
├── TENETS.md                            # Prioritized project principles and north-star guidance
├── cdk.json                             # CDK configuration (regions, features, thresholds)
├── pyproject.toml                       # Project metadata, dependencies, and CLI installation
│
├── cli/                                 # GCO CLI (jobs, stacks, capacity, inference, costs, DAGs)
├── diagrams/                            # Auto-generated architecture diagrams (infra_diagrams/) and code flowcharts (code_diagrams/)
├── docs/                                # Documentation (architecture, CLI, API, inference, customization, analytics)
├── examples/                            # Example manifests (jobs, inference, Ray, Volcano, Kueue, Slurm, YuniKorn)
├── gco/
│   ├── config/                          # Configuration loader with validation
│   ├── models/                          # Data models for k8s clusters, health monitor, inference monitor and manifest processor
│   ├── services/                        # K8s services (health/inference monitors, inference proxy, manifest/queue processors)
│   └── stacks/                          # CDK stacks (global, API gateway, regional, regional API gateway, monitoring, analytics)
│       └── constants.py                 # Pinned versions: EKS addons, Lambda runtime, Aurora engine
│
├── lambda/                              # Lambda functions
│   ├── analytics-cleanup/               # Custom resource that deletes Studio user profiles + EFS access points on stack destroy
│   ├── analytics-presigned-url/         # Generates presigned SageMaker Studio URLs for Cognito-authenticated users
│   ├── api-gateway-proxy/               # API Gateway → Global Accelerator proxy
│   ├── cross-region-aggregator/         # Cross-region job/health aggregation
│   ├── drift-detection/                 # Scheduled drift checks against deployed CDK stacks
│   ├── ga-registration/                 # Global Accelerator endpoint registration
│   ├── helm-installer/                  # Installs Helm charts (schedulers, cert-manager)
│   │   └── charts.yaml                  # Helm chart configuration (schedulers, cert-manager)
│   ├── image-lookup/                    # Adopt-or-create custom resource for the project's gco/* ECR repositories
│   ├── inference-streaming-proxy/       # Node.js response-streaming proxy for global and regional inference routes
│   ├── kubectl-applier-simple/          # Applies K8s manifests during deployment
│   │   └── manifests/                   # Kubernetes manifests (nodepools, RBAC, services, storage)
│   ├── proxy-shared/                    # Shared utilities for proxy Lambdas
│   ├── regional-api-proxy/              # Regional API Gateway → internal ALB proxy
│   └── secret-rotation/                 # Daily secret rotation
│
├── gco_mcp/                             # MCP server for LLM interaction (125 tools default, up to 165 with feature flags)
├── scripts/                             # Utility scripts (version bump, cluster access setup)
└── tests/                               # PyTest + BATS test suites (counts tracked via badges)
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, the GitHub Actions CI/CD layout, release process, and dependency scanning schedules.

Quick start for contributors (dev container — recommended):

```bash
docker build -f Dockerfile.dev -t gco-dev .
docker run --rm -v $(pwd):/workspace -w /workspace gco-dev pytest tests/ -v --cov=gco --cov=cli --cov=gco_mcp
```

Or, in a clean virtual environment on your host:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v --cov=gco --cov=cli --cov=gco_mcp
```

> If `pip install -e ".[dev]"` fails with dependency-resolver errors, that's the pinned-versions issue mentioned in [Prerequisites](#prerequisites). Use the dev container instead — it ships everything at the exact versions CI uses.

## License

See the [LICENSE](LICENSE) file for details.

## Support

- Check [Troubleshooting](docs/TROUBLESHOOTING.md) for common issues
- Review CloudWatch logs for Lambda and EKS errors
- Open an issue on [GitHub](https://github.com/awslabs/global-capacity-orchestrator-on-aws/issues)

## Security

GCO implements defense-in-depth across five layers (see [Security Model](#security-model) above for the diagram):

**Authentication and Authorization:**

- All API requests require AWS IAM (SigV4) authentication at the API Gateway
- The trusted proxy Lambda adds a request-bound HMAC envelope using a rotating Secrets Manager key; the reusable key is never sent downstream
- IRSA (IAM Roles for Service Accounts) provides pod-level AWS access with no static credentials
- EKS access entries with explicit policy bindings (no aws-auth ConfigMap)

**Network Security:**

- Regional platform ALBs are internal; the EKS API endpoint defaults to `PRIVATE`
- EKS clusters run in private subnets with configurable endpoint access (PRIVATE or PUBLIC_AND_PRIVATE)
- VPC endpoints eliminate traffic traversal over the public internet for ECR, S3, STS, SSM, and CloudWatch
- VPC Flow Logs (30-day retention) capture all network traffic for audit
- Kubernetes Network Policies enforce default-deny with explicit allow rules

**Encryption:**

- Data at rest: S3 (KMS), EFS (KMS), EBS (KMS), DynamoDB (AWS-managed), and Secrets Manager (KMS)
- Client-to-global/regional API traffic uses AWS-managed TLS and IAM SigV4. Cross-region aggregation also uses AWS-managed TLS plus SigV4 from the aggregator to each regional API bridge.
- The normal global proxy → Global Accelerator → ALB path and each regional VPC proxy → ALB path use authenticated private-root TLS on TCP/HTTPS 443. Global Accelerator is a Layer 4 pass-through and does not terminate TLS.
- Every ALB leaf is issued for `backend.<project>.gco.internal`; clients send that identity through SNI and assert it while connecting to dynamic accelerator or ALB DNS names. ALB → Kubernetes pod traffic remains HTTP after ALB termination.
- The root private key exists only in a customer-managed-KMS-encrypted Secrets Manager secret readable by the certificate-manager role. Proxy roles read only the public SSM trust bundle; rotating leaves are reimported into stable regional ACM ARNs.
- The request-bound HMAC envelope adds integrity, freshness, and replay defense; it is not encryption and is independent of TLS confidentiality and server authentication.
- EFS mount encryption in transit is enabled by the deployed storage configuration.
- Kubernetes secrets are encrypted in etcd (EKS-managed encryption).

**Infrastructure Policy Validation:**

- Five cdk-nag v3 rule packs run during CDK policy validation:
  - AWS Solutions best practices
  - HIPAA Security Rule mappings
  - NIST 800-53 Rev 5 mappings
  - PCI DSS 3.2.1 mappings
  - Serverless best practices
- Findings are either fixed or explicitly acknowledged with justification in `gco/stacks/nag_suppressions.py`.
- These automated checks are not certifications and do not by themselves establish compliance.

**Supply Chain Security:**

- Container images scanned with Trivy on every push (CVE detection)
- Python dependencies audited with pip-audit (GHSA/CVE detection)
- Both repository-owned npm graphs are exact-pinned with committed lockfiles, audited on every PR, and updated by Dependabot
- Production JavaScript is scanned by CodeQL and Semgrep; the inference-streaming Lambda has a separate Node.js 24 test workflow with 93% line/function/branch gates
- Dependency versions pinned with exact hashes in `requirements-lock.txt`
- Dependabot and CodeQL enabled for automated vulnerability alerts
- SBOM generation via Trivy for all container images

**Vulnerability Disclosure:**
For security issues, **do not open a public GitHub issue.** See [`.github/SECURITY.md`](.github/SECURITY.md) for the responsible disclosure process.

---
