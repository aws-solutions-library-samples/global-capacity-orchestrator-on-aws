# Kubernetes Services

Python microservices that run inside the EKS clusters as Kubernetes Deployments. These handle the runtime workload — manifest processing, health monitoring, inference reconciliation, queue consumption, and API serving.

## Table of Contents

- [Overview](#overview)
- [Services](#services)
- [API Routes](#api-routes)
- [Shared Utilities](#shared-utilities)
- [How Services Are Deployed](#how-services-are-deployed)
- [Adding a New Service](#adding-a-new-service)

## Overview

Each service runs as a container built from a Dockerfile in `dockerfiles/`. The Kubernetes manifests in `lambda/kubectl-applier-simple/manifests/` define the Deployments, Services, and PodDisruptionBudgets. CDK builds the container images, pushes them to ECR, and the kubectl-applier Lambda applies the manifests at deploy time.

## Services

| File | Description |
|------|-------------|
| `manifest_processor.py` | Validates and applies Kubernetes manifests submitted via the API. Enforces namespace restrictions, resource limits, security contexts, and image allowlists. |
| `inference_monitor.py` | GitOps-style reconciliation controller. Polls DynamoDB for desired inference endpoint state and creates/updates/deletes K8s Deployments, Services, and Ingress rules. |
| `health_monitor.py` | Collects CPU, memory, and GPU utilization from the Kubernetes Metrics Server. Reports health status for ALB health checks and monitoring dashboards. |
| `health_api.py` | FastAPI app exposing health check endpoints (`/health`, `/ready`, `/metrics`). |
| `manifest_api.py` | FastAPI app for manifest submission, job listing, templates, webhooks, and queue management. Routes are split into `api_routes/`. |
| `queue_processor.py` | SQS consumer that reads job manifests from the regional queue, validates them, and applies to the cluster. Runs as a KEDA ScaledJob. |
| `template_store.py` | DynamoDB-backed CRUD for reusable job templates and webhook registrations. |
| `webhook_dispatcher.py` | Dispatches webhook notifications (HMAC-signed) on job lifecycle events (submitted, running, completed, failed). |
| `inference_store.py` | DynamoDB-backed store for inference endpoint specs and per-region status. |
| `metrics_publisher.py` | Publishes custom CloudWatch metrics (job counts, latency, queue depth). |
| `central_queue_worker.py` | Fenced, renewable-lease worker that adopts or creates deterministic Kubernetes Jobs from the global DynamoDB queue. |
| `auth_middleware.py` | Validates short-lived HMAC request envelopes (timestamp, nonce, method, target, and body digest) from trusted API Gateway proxies. |
| `structured_logging.py` | JSON structured logging configuration for all services. |
| `api_shared.py` | Shared Pydantic models and helper functions used by all API routes. |

## API Routes

The `api_routes/` subdirectory splits the FastAPI routes into focused modules:

| File | Description |
|------|-------------|
| `inference_proxy.py` | Authenticated, allowlisted reverse proxy for managed inference serving paths (`GET`, `HEAD`, and `POST` only). |
| `jobs.py` | Job listing, status, logs, events, pods, metrics, retry, and deletion |
| `queue.py` | Idempotent global queue submission, opaque pagination, bounded stats, queued-only cancellation, and operator polling |
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
