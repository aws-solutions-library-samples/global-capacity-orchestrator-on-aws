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
| `helm-installer/` | Installs Helm charts (KEDA, Volcano, KubeRay, Kueue) into EKS clusters during deployment. |
| `helm-orchestrator/` | CloudFormation custom-resource provider (async `cr.Provider`) that starts and polls the Helm-install Step Functions state machine. Does no Helm/Kubernetes work itself — the per-chart tasks run in `helm-installer`. |
| `image-lookup/` | CloudFormation custom resource that adopts-or-creates `gco/<name>` ECR repositories so retained repos from a prior deploy are rebound rather than failing the stack with `RepositoryAlreadyExistsException`. Honors `gco:retain=true` on Delete. |
| `api-gateway-proxy/` | Proxies IAM-authenticated global requests through Global Accelerator to regional ALBs using request-bound HMAC plus strict deployment-local private-root TLS. |
| `regional-api-proxy/` | VPC proxy behind every regional aggregation bridge; resolves/verifies its internal ALB and uses HMAC plus private-root TLS. Direct user invocation is optional. |
| `cross-region-aggregator/` | Discovers deterministic regional API Gateway stacks and aggregates their SigV4-authenticated AWS-TLS responses; it never connects directly to ALBs. |
| `secret-rotation/` | Rotates the backend HMAC key in AWS Secrets Manager on a daily schedule with overlap-safe validation. |
| `tls-certificate-manager/` | Bootstraps the KMS-encrypted deployment-local root, rotates short-lived regional ACM leaves in place, publishes public trust, and manages staged root rollover. |
| `tls-shared/` | Canonical strict private-root TLS/SNI client used by backend proxy packages. |
| `ga-registration/` | Registers regional ALB endpoints with AWS Global Accelerator during stack deployment. |
| `proxy-shared/` | Shared request-signing, header-sanitization, URL-building, timeout, and retry utilities used by both API Gateway proxy Lambda functions. |

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
API Gateway → api-gateway-proxy (HMAC) → Global Accelerator (TCP/443 pass-through)
  → regional ALB (private-root TLS) → EKS pod (HTTP)
                                      ↓
                         AuthenticationMiddleware
                         (validates exact request)

Global API → cross-region-aggregator → regional API (AWS TLS + SigV4)
  → regional-api-proxy (HMAC) → regional ALB (private-root TLS) → EKS pod

CDK Deploy → kubectl-applier-simple → EKS (applies manifests)
           → helm-orchestrator → Step Functions → helm-installer → EKS (installs Helm charts)
           → ga-registration → Global Accelerator (registers endpoints)

Scheduled → secret-rotation → HMAC secret
          → tls-certificate-manager → stable regional ACM certificate ARNs
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
| `drift-detection` | [HTML](../diagrams/code_diagrams/lambda/drift-detection/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/drift-detection/handler.lambda_handler.png) |
| `ga-registration` | [HTML](../diagrams/code_diagrams/lambda/ga-registration/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/ga-registration/handler.lambda_handler.png) |
| `helm-installer` | [HTML](../diagrams/code_diagrams/lambda/helm-installer/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/helm-installer/handler.lambda_handler.png) |
| `image-lookup` | [HTML](../diagrams/code_diagrams/lambda/image-lookup/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/image-lookup/handler.lambda_handler.png) |
| `kubectl-applier-simple` | [HTML](../diagrams/code_diagrams/lambda/kubectl-applier-simple/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/kubectl-applier-simple/handler.lambda_handler.png) |
| `secret-rotation` | [HTML](../diagrams/code_diagrams/lambda/secret-rotation/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/secret-rotation/handler.lambda_handler.png) |
| `tls-certificate-manager` | [HTML](../diagrams/code_diagrams/lambda/tls-certificate-manager/handler.lambda_handler.html) · [PNG](../diagrams/code_diagrams/lambda/tls-certificate-manager/handler.lambda_handler.png) |
| `tls-shared.get_backend_http_pool` | [HTML](../diagrams/code_diagrams/lambda/tls-shared/backend_tls.get_backend_http_pool.html) · [PNG](../diagrams/code_diagrams/lambda/tls-shared/backend_tls.get_backend_http_pool.png) |

Regenerate with `python diagrams/code_diagrams/generate.py` after
editing a handler's control flow.
