# CDK Stacks

AWS CDK stack definitions that create the GCO cloud infrastructure. Each stack is a self-contained unit that can be deployed independently (respecting dependency order).

## Table of Contents

- [Overview](#overview)
- [Stack Dependency Order](#stack-dependency-order)
- [Files](#files)
- [Deployment](#deployment)
- [Adding a New Stack](#adding-a-new-stack)

## Overview

GCO has six stack types: global state plus optional commercial-partition routing, the global API and backend PKI, per-region workload infrastructure, always-deployed per-region aggregation bridges, cross-region monitoring, and an optional analytics environment. Direct regional access is optional in `aws` and required elsewhere. Dependencies—not construction order in `app.py`—determine deployment sequencing. Each regional stack creates one VPC, EKS Auto Mode cluster, internal application load balancer, storage layer, Lambda functions, and platform images.

## Stack Dependency Order

```text
1. GCOGlobalStack
   ├─ Shared DynamoDB/S3/ECR/SSM resources in every partition
   ├─ Global Accelerator only in the commercial `aws` partition
   └─ prerequisite for all global/control-plane features
2. GCOAnalyticsStack (optional)
   └─ depends on Global; when enabled, the global API depends on its Studio integration
3. GCOApiGatewayGlobalStack
   ├─ depends on Global (and Analytics when enabled)
   └─ owns the HMAC secret, private-root certificate manager, and aggregator role
4. GCORegionalStack (×N)
   └─ depends on Global + Global API; resolves its stable regional ACM leaf ARN
5. GCORegionalApiGatewayStack (×N, always deployed)
   └─ each instance depends on its matching Regional stack; direct callers are optional in `aws` and required elsewhere
6. GCOMonitoringStack
   └─ depends on every Regional stack
```

## Files

| File | Description |
|------|-------------|
| `global_stack.py` | Partition-wide shared state plus Global Accelerator and endpoint groups in `aws` only |
| `api_gateway_global_stack.py` | Edge-optimized API Gateway with IAM auth, HMAC proxy, SigV4 regional aggregator, KMS-encrypted deployment root, scheduled TLS certificate manager, stable regional ACM leaves, WAF, logging, and alarms |
| `regional_stack.py` | Per-region VPC (spans all AZs in the region), EKS Auto Mode cluster, ALB, EFS/FSx storage, ECR images, Lambda functions (kubectl-applier, helm-installer, GA registration), IRSA roles |
| `regional_api_gateway_stack.py` | Always-deployed aggregation bridge with IAM auth and VPC Lambdas that resolve/verify the internal ALB and connect with HMAC plus private-root TLS; direct same-account callers are optional in `aws` and forced on elsewhere |
| `monitoring_stack.py` | Cross-region CloudWatch dashboard (GA, API Gateway, Lambda, SQS, DynamoDB, EKS, ALB widgets), SNS alerting, and CloudWatch alarms |
| `analytics_stack.py` | Optional SageMaker Studio, EMR Serverless, Cognito, and presigned-URL integration |
| `nag_suppressions.py` | cdk-nag v3 finding acknowledgments and five policy-validation rule-pack registrations |
| `constants.py` | Pinned versions for EKS addons, Lambda runtimes, Aurora engine, Helm charts |
| `__init__.py` | Package exports |

## Deployment

```bash
gco stacks deploy-all -y          # Deploy all stacks in dependency order
gco stacks deploy gco-us-east-1   # Deploy a single regional stack
gco stacks destroy-all -y         # Tear down everything
```

## Adding a New Stack

1. Create a new file in this directory (for example, `my_stack.py`).
2. Subclass `aws_cdk.Stack`.
3. Wire it into `app.py` with explicit dependencies.
4. Add narrowly scoped cdk-nag v3 acknowledgments in `nag_suppressions.py` when a finding is intentional.
5. Add the stack to `tests/_cdk_config_matrix.py` so CI covers synthesis and policy validation.

## Control-Flow Diagrams

Every stack constructor is auto-charted — the diagrams show the
wiring sequence (create KMS key → create VPC → create role → …)
and, where they exist, the real branches (sub-toggle gates inside
the analytics stack, feature flags inside the regional stack).

| Stack | Constructor | Helpers with branching logic |
|-------|-------------|------------------------------|
| `GCOGlobalStack` | [HTML](../../diagrams/code_diagrams/gco/stacks/global_stack.GCOGlobalStack___init__.html) · [PNG](../../diagrams/code_diagrams/gco/stacks/global_stack.GCOGlobalStack___init__.png) | — |
| `GCOApiGatewayGlobalStack` | [HTML](../../diagrams/code_diagrams/gco/stacks/api_gateway_global_stack.GCOApiGatewayGlobalStack___init__.html) · [PNG](../../diagrams/code_diagrams/gco/stacks/api_gateway_global_stack.GCOApiGatewayGlobalStack___init__.png) | — |
| `GCORegionalStack` | [HTML](../../diagrams/code_diagrams/gco/stacks/regional_stack.GCORegionalStack___init__.html) · [PNG](../../diagrams/code_diagrams/gco/stacks/regional_stack.GCORegionalStack___init__.png) | — |
| `GCORegionalApiGatewayStack` | [HTML](../../diagrams/code_diagrams/gco/stacks/regional_api_gateway_stack.GCORegionalApiGatewayStack___init__.html) · [PNG](../../diagrams/code_diagrams/gco/stacks/regional_api_gateway_stack.GCORegionalApiGatewayStack___init__.png) | — |
| `GCOMonitoringStack` | [HTML](../../diagrams/code_diagrams/gco/stacks/monitoring_stack.GCOMonitoringStack___init__.html) · [PNG](../../diagrams/code_diagrams/gco/stacks/monitoring_stack.GCOMonitoringStack___init__.png) | — |
| `GCOAnalyticsStack` | [HTML](../../diagrams/code_diagrams/gco/stacks/analytics_stack.GCOAnalyticsStack___init__.html) · [PNG](../../diagrams/code_diagrams/gco/stacks/analytics_stack.GCOAnalyticsStack___init__.png) | `_create_execution_role_and_grants` ([HTML](../../diagrams/code_diagrams/gco/stacks/analytics_stack.GCOAnalyticsStack__create_execution_role_and_grants.html) · [PNG](../../diagrams/code_diagrams/gco/stacks/analytics_stack.GCOAnalyticsStack__create_execution_role_and_grants.png)), `_create_studio_domain` ([HTML](../../diagrams/code_diagrams/gco/stacks/analytics_stack.GCOAnalyticsStack__create_studio_domain.html) · [PNG](../../diagrams/code_diagrams/gco/stacks/analytics_stack.GCOAnalyticsStack__create_studio_domain.png)) |

`app.py::main` wires these stacks together — the
[app.py flowchart](../../diagrams/code_diagrams/app.main.html)
shows the overall dependency order and the analytics sub-toggle
gate. Regenerate all of these with
`python diagrams/code_diagrams/generate.py`.
