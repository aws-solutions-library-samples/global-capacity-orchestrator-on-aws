# Lambda Functions

[AWS Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) functions that power GCO's infrastructure layer. These are deployed as part of the [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) stacks and handle cluster operations, API routing, security, and cross-region coordination.

## Table of Contents

- [Contents](#contents)
- [Build](#build)
- [Architecture](#architecture)
- [Control-Flow Diagrams](#control-flow-diagrams)

## Contents

| Directory | Description |
|-----------|-------------|
| `analytics-cleanup/` | CloudFormation deletion custom resource that drains SageMaker Studio apps, spaces, profiles, and EFS access points before analytics teardown. |
| `analytics-presigned-url/` | Exchanges a Cognito-authorized request for a short-lived SageMaker Studio URL, lazily provisioning the user's profile and EFS access point. |
| `api-gateway-proxy/` | Proxies IAM-authenticated global requests through [Global Accelerator](https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html) using request-bound HMAC and strict private-root TLS. |
| `capacity-poller/` | Scheduled read-only EC2 capacity snapshotter for the DynamoDB historical-capacity surface. |
| `cross-region-aggregator/` | Discovers regional API Gateway bridges and aggregates their AWS-TLS, SigV4-authenticated responses. |
| `drift-detection/` | Scheduled CloudFormation drift detection and SNS notification handler. |
| `ga-registration/` | Registers verified regional ALB endpoints with Global Accelerator during deployment. |
| `helm-installer/` | Per-chart Helm installation task Lambda used by the deployment Step Functions state machine. |
| `helm-orchestrator/` | Async [CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) custom-resource provider that starts and polls the Helm-install state machine. |
| `image-lookup/` | Adopts or creates retained `gco/<name>` [ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html) repositories without same-name deployment failures. |
| `inference-streaming-proxy/` | Node.js 24 response-streaming proxy used by global and regional `/inference/*` [API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/welcome.html) integrations. |
| `kubectl-applier-simple/` | Applies Kubernetes manifests to [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) clusters during CDK deployment. |
| `proxy-shared/` | Shared request signing, header sanitization, URL, timeout, and retry utilities for API proxy Lambdas. |
| `regional-api-proxy/` | [VPC](https://docs.aws.amazon.com/vpc/latest/userguide/what-is-amazon-vpc.html) proxy behind each regional aggregation bridge; verifies the internal ALB and uses HMAC plus private-root TLS. |
| `secret-rotation/` | Daily overlap-safe rotation of the backend HMAC key in [Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html). |
| `tls-certificate-manager/` | Bootstraps the encrypted private root, rotates regional ACM leaves, publishes trust, and manages staged root rollover. |
| `tls-shared/` | Canonical strict private-root TLS and SNI client shared by backend proxy packages. |
| `traffic-dial-controller/` | Scheduled, health-driven Global Accelerator traffic-dial convergence with manual overrides and last-healthy-region protection. |
| `vector-ingest/` | S3-triggered chunking and Bedrock embedding pipeline for the optional DynamoDB global vector store. |

## Build

The `kubectl-applier-simple` Lambda requires a build step to package dependencies.
`gco stacks deploy` runs it automatically (`StackManager._build_kubectl_lambda`
in `cli/stacks.py`); this is the equivalent by hand, pinned by the package's
`requirements.txt` and built for the Lambda platform rather than your laptop's:

```bash
rm -rf lambda/kubectl-applier-simple-build
mkdir -p lambda/kubectl-applier-simple-build
cp lambda/kubectl-applier-simple/handler.py lambda/kubectl-applier-simple/requirements.txt lambda/kubectl-applier-simple-build/
cp -r lambda/kubectl-applier-simple/manifests lambda/kubectl-applier-simple-build/
python3 -m pip install -r lambda/kubectl-applier-simple/requirements.txt \
  -t lambda/kubectl-applier-simple-build/ --upgrade \
  --platform manylinux2014_x86_64 --only-binary=:all:
```

The inference streaming proxy has a separate production npm graph. CI and CDK staging install it from its committed lockfile with lifecycle scripts disabled:

```bash
npm ci --prefix lambda/inference-streaming-proxy --omit=dev --ignore-scripts --no-audit --no-fund
```

Do not install the root CDK/diagram/markdown packages into this directory; keeping the deployable graph isolated prevents development tooling from entering the Lambda asset.

## Architecture

```text
API Gateway → api-gateway-proxy (HMAC) → Global Accelerator (TCP/443 pass-through)
  → regional ALB (private-root TLS) → EKS pod (re-encrypted HTTPS)
                                      ↓
                         AuthenticationMiddleware
                         (validates exact request)

API Gateway → inference-streaming-proxy (HMAC, Node.js 24 response stream)
  → Global Accelerator or regional ALB (private-root TLS)
  → inference-proxy Service → model endpoint Service → streamed response

Global API → cross-region-aggregator → regional API (AWS TLS + SigV4)
  → regional-api-proxy (HMAC) → regional ALB (private-root TLS) → EKS pod

CDK Deploy → kubectl-applier-simple → EKS (applies manifests)
           → helm-orchestrator → Step Functions → helm-installer → EKS (installs Helm charts)
           → ga-registration → Global Accelerator (registers endpoints)

Scheduled → secret-rotation → HMAC secret
          → tls-certificate-manager → stable regional ACM certificate ARNs
          → capacity-poller → DynamoDB capacity history
          → traffic-dial-controller → Global Accelerator endpoint-group dials
          → drift-detection → CloudFormation drift status + SNS

S3 ObjectCreated → vector-ingest → Bedrock embeddings → DynamoDB global table
Cognito API request → analytics-presigned-url → SageMaker Studio session URL
Analytics stack delete → analytics-cleanup → Studio + EFS dependency drain
```

## Control-Flow Diagrams

Auto-generated flowcharts for each handler live under
`diagrams/code_diagrams/lambda/`. The generated
[flowchart index](../diagrams/code_diagrams/README.md#lambda) lists every
charted handler; this README deliberately does not repeat it. Open the
interactive HTML pages for pan/zoom/SVG export; the PNGs are static snapshots
for GitHub's web viewer where JavaScript can't run.

Regenerate through the
[canonical two-commit diagram workflow](../diagrams/README.md#quick-reference)
after editing a handler's control flow.
