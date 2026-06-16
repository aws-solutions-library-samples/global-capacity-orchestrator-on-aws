# Lambda Functions

AWS Lambda functions that power GCO's infrastructure layer. These are deployed as part of the CDK stacks and handle cluster operations, API routing, security, and cross-region coordination.

## Table of Contents

- [Contents](#contents)
- [Build](#build)
- [Architecture](#architecture)
- [Control-Flow Diagrams](#control-flow-diagrams)

## Contents

| Directory | Description |
|-----------|-------------|
| `kubectl-applier-simple/` | Applies Kubernetes manifests to EKS clusters during CDK deployment. Contains the nodepool, RBAC, service, and storage manifests in `manifests/`. |
| `helm-installer/` | Installs Helm charts (KEDA, Volcano, KubeRay, Kueue, DRA driver) into EKS clusters during deployment. |
| `helm-orchestrator/` | CloudFormation custom-resource provider (async `cr.Provider`) that starts and polls the Helm-install Step Functions state machine. Does no Helm/Kubernetes work itself — the per-chart tasks run in `helm-installer`. |
| `image-lookup/` | CloudFormation custom resource that adopts-or-creates `gco/<name>` ECR repositories so retained repos from a prior deploy are rebound rather than failing the stack with `RepositoryAlreadyExistsException`. Honors `gco:retain=true` on Delete. |
| `api-gateway-proxy/` | Proxies requests from the global API Gateway through Global Accelerator to regional ALBs. Injects the secret authentication header. |
| `regional-api-proxy/` | Proxies requests from regional API Gateways directly to the internal NLB via VPC Link. Used for private cluster access. |
| `cross-region-aggregator/` | Aggregates job status, health, and inference data across all deployed regions into a single response. Powers the global API endpoints. |
| `secret-rotation/` | Rotates the authentication secret in AWS Secrets Manager on a daily schedule. Ensures zero-downtime rotation. |
| `ga-registration/` | Registers regional ALB endpoints with AWS Global Accelerator during stack deployment. |
| `alb-header-validator/` | ALB Lambda target that validates the secret authentication header on incoming requests. |
| `proxy-shared/` | Shared utilities used by the API Gateway and regional proxy Lambda functions. |

## Build

The `kubectl-applier-simple` Lambda requires a build step to package dependencies:

```bash
rm -rf lambda/kubectl-applier-simple-build
mkdir -p lambda/kubectl-applier-simple-build
cp lambda/kubectl-applier-simple/handler.py lambda/kubectl-applier-simple-build/
cp -r lambda/kubectl-applier-simple/manifests lambda/kubectl-applier-simple-build/
pip3 install kubernetes pyyaml urllib3 -t lambda/kubectl-applier-simple-build/
```

The GCO CLI handles this automatically during `gco stacks deploy`.

## Architecture

```text
API Gateway → api-gateway-proxy → Global Accelerator → ALB → EKS
                                                         ↑
                                              alb-header-validator
                                              (validates secret header)

Regional API → regional-api-proxy → Internal NLB → EKS

CDK Deploy → kubectl-applier-simple → EKS (applies manifests)
           → helm-orchestrator → Step Functions → helm-installer → EKS (installs Helm charts)
           → ga-registration → Global Accelerator (registers endpoints)

Scheduled → secret-rotation → Secrets Manager (daily rotation)

Global API → cross-region-aggregator → DynamoDB/SSM (aggregates all regions)
```

## Control-Flow Diagrams

Auto-generated flowcharts for each handler live under
[`diagrams/code_diagrams/lambda/`](../diagrams/code_diagrams/README.md).
Open the interactive HTML pages for pan/zoom/SVG export; the PNGs
below are static snapshots embedded for GitHub's web viewer where
JavaScript can't run.

| Handler | Flowchart |
|---------|-----------|
| `analytics-presigned-url` | [HTML](../diagrams/code_diagrams/lambda/analytics-presigned-url/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/analytics-presigned-url/handler.lambda_handler.png) |
| `analytics-cleanup` | [HTML](../diagrams/code_diagrams/lambda/analytics-cleanup/handler.handler.html) · [PNG](../diagrams/code_diagrams/lambda/analytics-cleanup/handler.handler.png) |
| `api-gateway-proxy` | [HTML](../diagrams/code_diagrams/lambda/api-gateway-proxy/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/api-gateway-proxy/handler.lambda_handler.png) |
| `regional-api-proxy` | [HTML](../diagrams/code_diagrams/lambda/regional-api-proxy/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/regional-api-proxy/handler.lambda_handler.png) |
| `cross-region-aggregator` | [HTML](../diagrams/code_diagrams/lambda/cross-region-aggregator/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/cross-region-aggregator/handler.lambda_handler.png) |
| `alb-header-validator` | [HTML](../diagrams/code_diagrams/lambda/alb-header-validator/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/alb-header-validator/handler.lambda_handler.png) |
| `drift-detection` | [HTML](../diagrams/code_diagrams/lambda/drift-detection/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/drift-detection/handler.lambda_handler.png) |
| `ga-registration` | [HTML](../diagrams/code_diagrams/lambda/ga-registration/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/ga-registration/handler.lambda_handler.png) |
| `helm-installer` | [HTML](../diagrams/code_diagrams/lambda/helm-installer/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/helm-installer/handler.lambda_handler.png) |
| `image-lookup` | [HTML](../diagrams/code_diagrams/lambda/image-lookup/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/image-lookup/handler.lambda_handler.png) |
| `kubectl-applier-simple` | [HTML](../diagrams/code_diagrams/lambda/kubectl-applier-simple/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/kubectl-applier-simple/handler.lambda_handler.png) |
| `secret-rotation` | [HTML](../diagrams/code_diagrams/lambda/secret-rotation/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/secret-rotation/handler.lambda_handler.png) |

Regenerate with `python diagrams/code_diagrams/generate.py` after
editing a handler's control flow.
