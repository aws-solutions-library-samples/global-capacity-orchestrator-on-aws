# Kubernetes Services

Python microservices that run inside the [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) clusters as Kubernetes Deployments. These handle the runtime workload — manifest processing, health monitoring, inference reconciliation, queue consumption, and API serving.

## Table of Contents

- [Overview](#overview)
- [Services](#services)
- [API Routes](#api-routes)
- [Shared Utilities](#shared-utilities)
- [How Services Are Deployed](#how-services-are-deployed)
- [Adding a New Service](#adding-a-new-service)

## Overview

Each service runs as a container built from a Dockerfile in `dockerfiles/`. The Kubernetes manifests in `lambda/kubectl-applier-simple/manifests/` define the Deployments, Services, and PodDisruptionBudgets. [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) builds the container images, pushes them to [ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html), and the kubectl-applier [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) applies the manifests at deploy time.

## Services

| File | Description |
|------|-------------|
| `manifest_processor.py` | Validates and applies Kubernetes manifests submitted via the API. Enforces namespace restrictions, resource limits, security contexts, and image allowlists. |
| `inference_monitor.py` | GitOps-style reconciliation controller. Polls [DynamoDB](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Introduction.html) for desired inference endpoint state and creates/updates/deletes K8s Deployments, Services, and Ingress rules. |
| `health_monitor.py` | Collects CPU, memory, and GPU utilization from the Kubernetes Metrics Server. Reports health status for [ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html) health checks and monitoring dashboards. |
| `health_api.py` | FastAPI app exposing health check endpoints (`/health`, `/ready`, `/metrics`). |
| `manifest_api.py` | FastAPI app for manifest submission, job listing, templates, webhooks, and queue management. Routes are split into `api_routes/`. |
| `queue_processor.py` | [SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html) consumer that reads job manifests from the regional queue, validates them, and applies to the cluster. Runs as a KEDA ScaledJob. |
| `template_store.py` | DynamoDB-backed CRUD for reusable job templates and webhook registrations. |
| `webhook_dispatcher.py` | Dispatches webhook notifications (HMAC-signed) on job lifecycle events (submitted, running, completed, failed). |
| `inference_store.py` | DynamoDB-backed store for inference endpoint specs and per-region status. |
| `metrics_publisher.py` | Publishes custom [CloudWatch](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/WhatIsCloudWatch.html) metrics (job counts, latency, queue depth). |
| `central_queue_worker.py` | Fenced, renewable-lease worker that adopts or creates deterministic Kubernetes Jobs from the global DynamoDB queue. Consults the spot price gate before claiming price-capped jobs. |
| `spot_price_gate.py` | Spot price gating for the central queue — TTL-cached minimum-across-AZ [spot price](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-spot-instances.html) lookups plus per-job gate decisions, so price-capped jobs dispatch only when the market clears their cap. |
| `cost_monitor.py` | Cost-monitor service core — queries the in-cluster [OpenCost](https://opencost.io/) allocation API, normalizes windows into stable report rows, and writes deterministic Parquet reports to the central cost report bucket. |
| `cost_api.py` | FastAPI app for the cost-monitor Deployment: probes, `/internal/status`, report listing, ad-hoc generation, and the scheduled interval-report loop. |
| `auth_middleware.py` | Validates short-lived HMAC request envelopes (timestamp, nonce, method, target, and body digest) from trusted [API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/welcome.html) proxies. |
| `structured_logging.py` | JSON structured logging configuration for all services. |
| `api_shared.py` | Shared Pydantic models and helper functions used by all API routes. |

## API Routes

The `api_routes/` subdirectory splits the FastAPI routes into focused modules:

| File | Description |
|------|-------------|
| `inference_proxy.py` | Authenticated, allowlisted reverse proxy for managed inference serving paths (`GET`, `HEAD`, and `POST` only). |
| `jobs.py` | Job listing, status, logs, events, pods, metrics, retry, and deletion |
| `queue.py` | Idempotent global queue submission (including the optional spot price gate fields), opaque pagination, bounded stats, queued-only cancellation, and operator polling |
| `cost.py` | Authenticated `/api/v1/cost/*` surface — proxies status, report listing, and ad-hoc report generation to the internal cost-monitor service |
| `manifests.py` | Manifest submission and validation |
| `templates.py` | Template CRUD |
| `webhooks.py` | Webhook registration and testing |

## Shared Utilities

| File | Description |
|------|-------------|
| `api_shared.py` | Pydantic response models, error helpers, pagination |
| `structured_logging.py` | JSON log formatter, correlation ID injection |
| `__init__.py` | Package-level imports and service factory functions |

## How Services Are Deployed

1. CDK builds Docker images from `dockerfiles/` and pushes to ECR
2. The kubectl-applier Lambda applies manifests from `lambda/kubectl-applier-simple/manifests/`
3. Manifests reference the ECR image URIs via `{{PLACEHOLDER}}` variables replaced at deploy time
4. Services run as Deployments with PodDisruptionBudgets in the `gco-system` namespace

## Adding a New Service

1. Create the service module in this directory
2. Add a Dockerfile in `dockerfiles/`
3. Add a Kubernetes manifest in `lambda/kubectl-applier-simple/manifests/` (use the `30-39` range for system services)
4. Wire the ECR image build into `gco/stacks/regional_stack.py`
5. Add tests in `tests/`
