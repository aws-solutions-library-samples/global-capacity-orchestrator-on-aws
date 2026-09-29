# GCO Services

Runtime services and shared support modules used by the in-cluster GCO control plane. The HTTP applications are `health_api.py`, `manifest_api.py`, `inference_api.py`, and `cost_api.py`; loop-based workers and shared modules are listed alongside them so this file remains the authoritative package inventory.

## Module Inventory

| File | Description |
|------|-------------|
| `__init__.py` | Package marker for the service modules; runtime entry points import concrete modules directly. |
| `api_shared.py` | Shared Pydantic response models, pagination helpers, and API error utilities. |
| `auth_middleware.py` | Validates short-lived HMAC request envelopes, including timestamp, nonce, method, target, and body digest; a signature or digest header that is not 64 lowercase hex characters is a 403 before any comparison. |
| `aws_ssm.py` | Shared required/optional read, existence-check, and write helpers for SSM Parameter Store. |
| `central_queue_worker.py` | Lease-fenced worker that claims global queue records and adopts or creates deterministic Kubernetes Jobs. |
| `cost_api.py` | Internal FastAPI surface and scheduled reporting loop for the cost-monitor Deployment; traced as `cost-monitor`, and its lifespan stops the scheduled reporter and flushes spans at shutdown. |
| `cost_monitor.py` | Queries OpenCost over verified HTTPS (`opencost-tls` Service, internal CA) through a new `httpx2` client per call (internal CA looked up per call, so a rotated CA needs no restart), normalizes report windows, and writes deterministic Parquet reports. |
| `grafana_rotator.py` | Rotates the in-cluster Grafana administrator credential through the Grafana API over verified HTTPS (`grafana-tls` Service, internal CA; fails closed before reading a credential when the CA is missing) and patches the Kubernetes Secret. |
| `health_api.py` | Health-monitor FastAPI app exposing `/`, `/healthz`, `/readyz`, `/api/v1/health`, `/api/v1/metrics`, `/api/v1/status`, and Prometheus `/metrics`; traced as `health-monitor`. |
| `health_monitor.py` | Collects cluster and workload health, resource utilization, and self-healing observations. |
| `inference_api.py` | Dedicated authenticated inference-proxy FastAPI app exposing `/`, `/healthz`, `/readyz`, `/inference/*`, and Prometheus `/metrics`; traced as `inference-proxy`, and its lifespan flushes spans at shutdown. |
| `inference_monitor.py` | Reconciles inference endpoint desired state from DynamoDB with Kubernetes Deployments and Services; gives every managed model pod an `endpoint-tls-proxy` sidecar (the `tls_proxy.py` source from a per-endpoint ConfigMap on a pinned multi-arch Python image) and publishes model Services on 8443 only. |
| `inference_store.py` | DynamoDB-backed persistence for inference endpoint specifications and per-region status. |
| `internal_tls.py` | Trust for GCO's in-cluster HTTPS clients: an `ssl.SSLContext` that trusts only the GCO internal CA (`GCO_INTERNAL_CA_FILE`, default `/var/run/gco/ca/ca.crt`), verifies the host name, requires TLS 1.2+, is cached per CA file identity so a rotated bundle is picked up, and raises `InternalTLSError` instead of falling back to another trust store. |
| `leader_lease.py` | Kubernetes `coordination.k8s.io` Lease acquisition shared by the health monitor's ALB sync and webhook delivery leaders. |
| `manifest_api.py` | Authenticated control-plane FastAPI app for manifests, jobs, policy, queues, templates, webhooks, and costs; traced as `manifest-processor`. |
| `manifest_processor.py` | Validates and applies Kubernetes manifests with namespace, resource, placement, and security policy enforcement. |
| `metrics_publisher.py` | Publishes GCO health and workload metrics to CloudWatch. |
| `mooncake_pd_proxy.py` | Standalone Mooncake prefill/decode proxy mounted into disaggregated inference endpoint pods; binds `PD_PROXY_HOST` (loopback behind the pod's TLS sidecar), dials prefill/decode over HTTPS trusting only `PD_PROXY_CA_FILE` through a new client per call (the CA re-read when the file changes, so a rotated CA needs no restart; keep-alive off), relays header values byte for byte, and uses `httpx2` when the vLLM image has it, else the image's `httpx`; it must parse as Python 3.12. |
| `queue_processor.py` | SQS consumer that validates regional job submissions and applies them through Kubernetes. |
| `request_context.py` | Binds server-generated request IDs to responses, generic errors, and correlated log records. |
| `request_size_middleware.py` | Enforces request-body limits before authentication while replaying the exact bytes downstream. |
| `service_metrics.py` | Prometheus request instrumentation and collectors shared by HTTP and loop-based services; `start_metrics_server(..., host=)` lets the inference monitor bind its plaintext listener to loopback behind a TLS sidecar. |
| `spot_price_gate.py` | TTL-cached spot-price lookup and per-job dispatch decisions for price-capped central queue records. |
| `structured_logging.py` | JSON logging and safe operational-context formatting for service processes; adds `trace_id`, `span_id`, and `trace_sampled` to lines logged inside a traced request. |
| `template_store.py` | DynamoDB-backed job templates, webhook registrations, and central queue lifecycle records. |
| `tls_proxy.py` | Stdlib-only TLS sidecar proxy (uvloop when present) that terminates pod-facing HTTPS, forwards over loopback, and activates rotated cert-manager leaves in place without rebinding its listener; `TLS_PROXY_KEYPAIR_WAIT_SECONDS` lets chart-pod sidecars wait for a Secret that does not exist yet. Also runs as a plain script in model pods. |
| `tracing.py` | OpenTelemetry tracing for the four API services, exported directly to the AWS X-Ray OTLP endpoint (gzip protobuf, SigV4, no collector); inert unless `GCO_TRACING_ENABLED=true`, imports no OpenTelemetry until configured, and never raises into the service. |
| `uvicorn_runtime.py` | Reports which event loop (uvloop) and HTTP parser (httptools) uvicorn's `auto` selection resolved, for the FastAPI services' startup log line. |
| `webhook_dispatcher.py` | Delivers HMAC-signed job lifecycle notifications with bounded retry behavior over public-trust HTTPS; never traced, because webhook URLs carry credentials. |

## API Routes

The `api_routes/` package splits the manifest and inference applications into focused routers:

| File | Description |
|------|-------------|
| `cost.py` | Authenticated `/api/v1/cost/*` proxy to the internal cost-monitor service at `https://cost-monitor.gco-system.svc.cluster.local:8443`, verified against the internal CA; an unreachable service or a missing CA answers 503. |
| `inference_proxy.py` | Authenticated, allowlisted streaming reverse proxy for managed inference serving paths, dialling `https://<service>.<namespace>.svc.cluster.local:8443` through a new `httpx2` client per request (internal CA looked up per request, so a rotated CA needs no restart; keep-alive off; only the caller's `Accept-Encoding`), released when its stream ends or fails; header values cross it byte for byte. |
| `jobs.py` | Job listing, status, logs, events, pods, metrics, retry, and deletion. |
| `manifests.py` | Manifest submission and validation. |
| `queue.py` | Idempotent global queue submission, listing, cancellation, pagination, and status polling. |
| `templates.py` | Reusable template CRUD. |
| `webhooks.py` | Webhook registration, deletion, and delivery testing. |

## HTTP Clients, TLS, and Tracing

- Outbound HTTP uses [`httpx2`](https://pypi.org/project/httpx2/), the maintained successor fork of `httpx` with the same API (Starlette's `TestClient` prefers it). The PD proxy is the one exception: it runs on the upstream vLLM image and falls back to that image's `httpx`.
- In-cluster HTTPS clients take `verify=` from `internal_tls` and set it on the transport (`httpx2.AsyncHTTPTransport` / `HTTPTransport`), because a client given `transport=` ignores its own TLS settings. Only `https` URLs need the CA bundle; an `http://` override for kind or local runs does not.
- The two relaying proxies (inference proxy, PD proxy) remove the client's default `Accept-Encoding`, so only an encoding the caller asked for reaches the model and the bytes they relay are exactly what the caller negotiated.
- The four FastAPI apps call `tracing.configure_tracing("<service>")` and `tracing.instrument_fastapi_app(app)` where the app is built and `tracing.shutdown_tracing()` in their lifespan; in-cluster clients wrap their transport with `tracing.wrap_async_transport` / `wrap_sync_transport`. See [`docs/MONITORING.md`](../../docs/MONITORING.md#distributed-tracing).

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
