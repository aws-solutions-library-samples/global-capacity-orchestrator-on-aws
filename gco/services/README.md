# GCO Services

Runtime services and shared support modules used by the in-cluster GCO control plane. The HTTP applications are `health_api.py`, `manifest_api.py`, `inference_api.py`, and `cost_api.py`; loop-based workers and shared modules are listed alongside them so this file remains the authoritative package inventory.

## Module Inventory

| File | Description |
|------|-------------|
| `__init__.py` | Package marker for the service modules; runtime entry points import concrete modules directly. |
| `api_shared.py` | Shared Pydantic response models, pagination helpers, and API error utilities. |
| `auth_middleware.py` | Validates short-lived HMAC request envelopes, including timestamp, nonce, method, target, and body digest. |
| `aws_ssm.py` | Shared required/optional read, existence-check, and write helpers for SSM Parameter Store. |
| `central_queue_worker.py` | Lease-fenced worker that claims global queue records and adopts or creates deterministic Kubernetes Jobs. |
| `cost_api.py` | Internal FastAPI surface and scheduled reporting loop for the cost-monitor Deployment. |
| `cost_monitor.py` | Queries OpenCost, normalizes report windows, and writes deterministic Parquet reports. |
| `grafana_rotator.py` | Rotates the in-cluster Grafana administrator credential through the Grafana API and Kubernetes Secret. |
| `health_api.py` | Health-monitor FastAPI app exposing `/`, `/healthz`, `/readyz`, `/api/v1/health`, `/api/v1/metrics`, `/api/v1/status`, and Prometheus `/metrics`. |
| `health_monitor.py` | Collects cluster and workload health, resource utilization, and self-healing observations. |
| `inference_api.py` | Dedicated authenticated inference-proxy FastAPI app exposing `/`, `/healthz`, `/readyz`, `/inference/*`, and Prometheus `/metrics`. |
| `inference_monitor.py` | Reconciles inference endpoint desired state from DynamoDB with Kubernetes Deployments and Services. |
| `inference_store.py` | DynamoDB-backed persistence for inference endpoint specifications and per-region status. |
| `leader_lease.py` | Kubernetes `coordination.k8s.io` Lease acquisition shared by the health monitor's ALB sync and webhook delivery leaders. |
| `manifest_api.py` | Authenticated control-plane FastAPI app for manifests, jobs, policy, queues, templates, webhooks, and costs. |
| `manifest_processor.py` | Validates and applies Kubernetes manifests with namespace, resource, placement, and security policy enforcement. |
| `metrics_publisher.py` | Publishes GCO health and workload metrics to CloudWatch. |
| `mooncake_pd_proxy.py` | Standalone Mooncake prefill/decode proxy mounted into disaggregated inference endpoint pods. |
| `queue_processor.py` | SQS consumer that validates regional job submissions and applies them through Kubernetes. |
| `request_context.py` | Binds server-generated request IDs to responses, generic errors, and correlated log records. |
| `request_size_middleware.py` | Enforces request-body limits before authentication while replaying the exact bytes downstream. |
| `service_metrics.py` | Prometheus request instrumentation and collectors shared by HTTP and loop-based services. |
| `spot_price_gate.py` | TTL-cached spot-price lookup and per-job dispatch decisions for price-capped central queue records. |
| `structured_logging.py` | JSON logging and safe operational-context formatting for service processes. |
| `template_store.py` | DynamoDB-backed job templates, webhook registrations, and central queue lifecycle records. |
| `tls_proxy.py` | Hot-reloading TLS sidecar proxy that terminates pod-facing HTTPS and forwards over loopback. |
| `webhook_dispatcher.py` | Delivers HMAC-signed job lifecycle notifications with bounded retry behavior. |

## API Routes

The `api_routes/` package splits the manifest and inference applications into focused routers:

| File | Description |
|------|-------------|
| `inference_proxy.py` | Authenticated, allowlisted streaming reverse proxy for managed inference serving paths. |
| `jobs.py` | Job listing, status, logs, events, pods, metrics, retry, and deletion. |
| `queue.py` | Idempotent global queue submission, listing, cancellation, pagination, and status polling. |
| `cost.py` | Authenticated `/api/v1/cost/*` proxy to the internal cost-monitor service. |
| `manifests.py` | Manifest submission and validation. |
| `templates.py` | Reusable template CRUD. |
| `webhooks.py` | Webhook registration, deletion, and delivery testing. |

## How Services Are Deployed

1. CDK builds the six service images from `dockerfiles/` and publishes them to ECR.
2. The kubectl-applier Lambda applies manifests from `lambda/kubectl-applier-simple/manifests/`.
3. Deployment-time placeholders bind project-specific image URIs, resources, identities, and endpoints.
4. Long-running services run in `gco-system`; workload resources are confined to their documented namespaces.

See [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) for request flows and trust boundaries, and [`docs/API.md`](../../docs/API.md) for the public and internal HTTP contracts.

## Adding a Service Module

1. Add the module here and a focused test under `tests/`.
2. For a new process, add its Dockerfile, dependency group, Kubernetes manifest, and CDK image wiring.
3. Add the module to the inventory above; `tests/test_documentation_consistency.py` enforces exact coverage.
4. Update the architecture and API documentation when the runtime or route surface changes.
