# GCO API Reference

GCO's HTTP surface is served by four in-cluster services behind one or two API
Gateways. This document covers all of it:

- the **control-plane API** — manifests, jobs, the global job queue, templates,
  webhooks, and cost reporting, served by the manifest processor;
- the **global aggregation API** — cross-region fan-out served by a Lambda at
  the global API Gateway, not by a cluster service;
- the **inference API** — authenticated proxying to deployed model endpoints,
  served by the inference proxy;
- the **health, readiness, and observability** endpoints, served by the health
  monitor and by each service's own probe routes.

Surfaces that are deliberately unreachable from outside the VPC are documented
under [Cluster-Internal Surfaces](#cluster-internal-surfaces) rather than
omitted, so operators know they exist and why a request from outside cannot
reach them.

Start with [API Surface at a Glance](#api-surface-at-a-glance): it maps every
path prefix to the service that answers it, whether the global or regional API
Gateway exposes it, and which authentication applies.

> **Generated inventory.** The endpoint tables in this document are checked
> against the running applications by
> [`tests/test_api_docs_coverage.py`](../tests/test_api_docs_coverage.py), which
> fails if a route is added, removed, or renamed without updating this file.
> Machine-readable OpenAPI documents live in [`docs/openapi/`](openapi/).

## Table of Contents

- [API Surface at a Glance](#api-surface-at-a-glance)
- [Base URLs](#base-urls)
- [Authentication](#authentication)
- [Transport Security](#transport-security)
- [CLI Quick Reference](#cli-quick-reference)
- [API Endpoints](#api-endpoints)
  - [Health & Status](#health--status)
  - [Global Aggregation (Cross-Region)](#global-aggregation-cross-region)
  - [Manifest Operations](#manifest-operations)
  - [Job Operations](#job-operations)
  - [Job Queue (Global)](#job-queue-global)
  - [Job Templates](#job-templates)
  - [Webhooks](#webhooks)
  - [Cost Reporting (Per Region)](#cost-reporting-per-region)
  - [Inference](#inference)
- [Detailed Endpoint Documentation](#detailed-endpoint-documentation)
  - [Global Jobs List](#global-jobs-list)
  - [Global Health Status](#global-health-status)
  - [Global Bulk Delete](#global-bulk-delete)
  - [List Jobs](#list-jobs)
  - [Get Job Logs](#get-job-logs)
  - [Get Job Events](#get-job-events)
  - [Get Job Pods](#get-job-pods)
  - [Get Job Metrics](#get-job-metrics)
  - [Bulk Delete Jobs](#bulk-delete-jobs)
  - [Retry Job](#retry-job)
- [Job Queue (DynamoDB-backed)](#job-queue-dynamodb-backed)
  - [Submit to Queue](#submit-to-queue)
  - [List Queued Jobs](#list-queued-jobs)
  - [Get Queued Job](#get-queued-job)
  - [Cancel Queued Job](#cancel-queued-job)
  - [Queue Statistics](#queue-statistics)
- [Job Templates](#job-templates-1)
  - [Create Template](#create-template)
  - [Create Job from Template](#create-job-from-template)
- [Webhooks](#webhooks-1)
  - [Register Webhook](#register-webhook)
- [Inference API](#inference-api)
  - [Invoking an Endpoint](#invoking-an-endpoint)
  - [Allowed Upstream Paths](#allowed-upstream-paths)
  - [Streaming, Timeouts, and Failure Codes](#streaming-timeouts-and-failure-codes)
  - [Canary and Disaggregated Routing](#canary-and-disaggregated-routing)
- [Health, Readiness, and Observability](#health-readiness-and-observability)
- [Cluster-Internal Surfaces](#cluster-internal-surfaces)
- [Error Responses](#error-responses)
- [Examples](#examples)

---

## API Surface at a Glance

Two things determine whether a request succeeds: whether an API Gateway exposes
the path at all, and which in-cluster service the ALB routes it to.

**What each API Gateway exposes.** The gateways forward only these prefixes;
anything else returns a gateway-level error regardless of what the cluster
services implement.

| Path prefix | Global API Gateway | Regional API Gateway | Handled by |
|-------------|--------------------|----------------------|------------|
| `/api/v1/global/*` | Yes (IAM) | No | Cross-region aggregator Lambda |
| `/api/v1/*` | Yes (IAM) | Yes (IAM) | Forwarded to the regional ALB |
| `/inference/*` | Yes (IAM, response streaming) | Yes (IAM, response streaming) | Forwarded to the regional ALB |
| `/studio/login`, `/studio/callback` | Only when the analytics stack is enabled (Cognito) | No | Presigned SageMaker Studio Lambda |

In the commercial `aws` partition the global API Gateway is the workload entry
point and reaches regions over Global Accelerator. In every other partition the
global API is aggregate-only and workload traffic uses each region's IAM
authenticated bridge. See [Base URLs](#base-urls).

**What the ALB routes to which service.** Inside a region, the shared Gateway
API `HTTPRoute` (`gco-system/gco-routes`) matches on path prefix, longest prefix
first:

| Path prefix | Service | Notes |
|-------------|---------|-------|
| `/api/v1/health` | `health-monitor` | Cluster health; used for Global Accelerator health checks |
| `/api/v1/metrics` | `health-monitor` | Cluster resource utilization |
| `/api/v1/manifests` | `manifest-processor` | |
| `/inference` | `inference-proxy` | |
| `/healthz` | `health-monitor` | |
| `/` (catch-all) | `manifest-processor` | Every other `/api/v1/*` path lands here |

`/api/v1/status` is the one path both the health monitor and the manifest
processor implement, and a path prefix cannot be split between two Services. The
catch-all resolves it to the **manifest processor**, which is the response with
external consumers: the cross-region aggregator's `/api/v1/global/status` fans
out to this path and reads `templates_count`, `webhooks_count`,
`resource_limits`, and `allowed_namespaces` from it. The health monitor's own
`/api/v1/status` is therefore in-cluster only, reached by addressing the
`health-monitor` Service directly. See
[Cluster-Internal Surfaces](#cluster-internal-surfaces).

## Base URLs

Every path in this document is relative to one of two hosts.

**Global API Gateway** — the `ApiGatewayUrl` / `ApiEndpoint` CloudFormation
output of the API Gateway stack. Use it for cross-region aggregation, and for
workload traffic in the commercial `aws` partition:

```http
https://<API_GATEWAY_ENDPOINT>/api/v1
```

**Regional API Gateway** — the `RegionalApiEndpoint` output of a
`<project>-regional-api-<region>` stack. Use it to pin a request to one region,
and for all workload traffic outside the commercial `aws` partition:

```http
https://<REGIONAL_API_ENDPOINT>/api/v1
```

Both accept `/inference/*` as well as `/api/v1/*`. The `gco` CLI resolves these
hosts for you and selects the regional endpoint automatically whenever an
operation carries an exact target Region.

## Authentication

All API requests are authenticated using AWS IAM Signature Version 4 ([SigV4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html))
at the API Gateway level. API Gateway validates the caller's AWS credentials;
the proxy then allowlists supported headers and signs the exact backend request
with a short-lived HMAC envelope. The envelope binds a timestamp, nonce, method,
path/query, and body digest, so the reusable [Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html) signing key is never
sent to the cluster. Backend middleware rejects stale, tampered, or process-local
replayed envelopes.

**Important:** Clients provide only SigV4 authentication. Do not set any
`X-GCO-Signature-*`, `X-GCO-Timestamp`, `X-GCO-Nonce`, or
`X-GCO-Content-SHA256` headers; the proxy strips caller-supplied internal headers
and creates its own envelope after IAM authentication. In the commercial `aws`
partition, the global workload endpoint also rejects `X-GCO-Target-Region`;
select an authorized regional API endpoint for explicit region pinning. The GCO
CLI does this automatically whenever an API operation carries an exact transport
target Region. Outside `aws`, workload control and inference use those regional
IAM endpoints because the global API is aggregate-only.

Four paths are exempt from the HMAC envelope so load balancers, Global
Accelerator, and the cluster's Prometheus can reach them without credentials:
`/healthz`, `/readyz`, `/metrics`, and `/api/v1/health`. Of those, only
`/api/v1/health` is exposed through an API Gateway, and reaching it there still
requires IAM SigV4 at the edge. Every other path is rejected with `403` unless it
carries a valid envelope, and with `503` if the service cannot load its signing
key at all.

## Transport Security

GCO has two explicit TLS trust domains:

1. Clients and the centralized aggregator use AWS-managed TLS to API Gateway.
   Aggregator fan-out additionally uses SigV4 with its execution-role
   credentials to each deterministic regional API bridge.
2. Trusted proxy Lambdas use the deployment-local private root for the backend
   ALB hop. In commercial `aws`, the global path is proxy → [Global Accelerator](https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html)
   → ALB; a regional bridge uses VPC proxy → ALB in every partition. Global
   Accelerator forwards TCP/443 at Layer 4 and does not terminate TLS. Outside
   `aws`, accelerator-backed workload routes are omitted and callers use the
   regional bridge path directly.

Every regional ACM leaf represents `backend.<project>.gco.internal`. Backend
clients connect to dynamic accelerator or ALB DNS names but explicitly send
that identity with SNI and assert it during certificate verification. ALBs
terminate TLS and forward HTTP to Kubernetes pods.

The root private key exists only in a customer-managed-KMS-encrypted Secrets
Manager secret readable by the certificate-manager role. Proxy roles read only
the public SSM trust bundle. The HMAC envelope is complementary: it provides
application integrity, freshness, and replay defense, not encryption.

### Using AWS CLI with SigV4

Set `API_GATEWAY_ENDPOINT` to your API Gateway host, then reuse it in each
request. Replace the `<API_GATEWAY_ENDPOINT>` placeholder with the host from
the `ApiGatewayUrl` CloudFormation output (for example
`abc123.execute-api.us-east-1.amazonaws.com`):

```bash
export API_GATEWAY_ENDPOINT=<API_GATEWAY_ENDPOINT>

# Using awscurl (recommended)
pip install awscurl

awscurl --service execute-api \
  --region us-east-1 \
  "https://$API_GATEWAY_ENDPOINT/api/v1/jobs"

# Or using curl with AWS credentials
curl "https://$API_GATEWAY_ENDPOINT/api/v1/jobs" \
  --aws-sigv4 "aws:amz:us-east-1:execute-api" \
  --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY"
```

### Using Python with boto3

```python
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import boto3

session = boto3.Session()
credentials = session.get_credentials()

request = AWSRequest(method="GET", url="https://<API_GATEWAY_ENDPOINT>/api/v1/jobs")
SigV4Auth(credentials, "execute-api", "us-east-1").add_auth(request)

response = requests.get(request.url, headers=dict(request.headers))
```

See [Client Examples](client-examples/README.md) for more detailed examples.

## CLI Quick Reference

The `gco` CLI is the recommended way to interact with the API. Install it with:

```bash
pip install -e .
```

Common commands (replace `<JOB_ID>` with a queue job ID from `gco queue list`
and `<WEBHOOK_ID>` with a webhook ID from `gco webhooks list`):

```bash
# Job management
gco jobs submit job.yaml --region us-east-1
gco jobs list --region us-east-1
gco jobs list --all-regions
gco jobs get my-job --region us-east-1
gco jobs logs my-job --region us-east-1
gco jobs delete my-job --region us-east-1

# Global job queue (DynamoDB-backed)
gco queue submit job.yaml --region us-east-1
gco queue list --status queued
gco queue get <JOB_ID>
gco queue cancel <JOB_ID>
gco queue stats

# Templates
gco templates list
gco templates create job.yaml --name my-template
gco templates run my-template --name my-job --region us-east-1

# Webhooks
gco webhooks list
gco webhooks create --url https://example.com/hook -e job.completed
gco webhooks delete <WEBHOOK_ID>
```

## API Endpoints

### Health & Status

`/api/v1/health` is the only one of these reachable through an API Gateway; it
requires no HMAC envelope so Global Accelerator can health-check it. The probe
routes exist on all three request-serving services and are reached in-cluster.
Full detail, including response shapes, is in
[Health, Readiness, and Observability](#health-readiness-and-observability).

| Method | Endpoint | Answered by | Reachable via gateway | Description |
|--------|----------|-------------|-----------------------|-------------|
| GET | `/api/v1/health` | `health-monitor` | Yes | Cluster health; `200` healthy, `503` unhealthy |
| GET | `/api/v1/status` | `manifest-processor` | Yes | Service status, resource limits, queue-worker state |
| GET | `/api/v1/policy` | `manifest-processor` | Yes | The region's effective job validation policy plus live namespace ResourceQuota / LimitRange ceilings |
| GET | `/healthz` | each service | No | Liveness probe |
| GET | `/readyz` | each service | No | Readiness probe |
| GET | `/metrics` | each service | No | Prometheus exposition |

### Global Aggregation (Cross-Region)

These endpoints query all required regions in parallel and return aggregated
results. The aggregator discovers each `<project>-regional-api-<region>` stack
through CloudFormation, validates its `RegionalApiEndpoint`, and calls it over
AWS-managed TLS with SigV4. The regional VPC proxy then performs the
HMAC-signed, private-root-TLS hop to the internal ALB. Discovery fails closed if
a required bridge is unavailable.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/global/jobs` | List jobs across all regions |
| DELETE | `/api/v1/global/jobs` | Bulk delete jobs across all regions |
| GET | `/api/v1/global/health` | Health status across all regions |
| GET | `/api/v1/global/status` | Cluster status across all regions |

### Manifest Operations

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/manifests` | Submit manifests for processing |
| POST | `/api/v1/manifests/validate` | Validate manifests without applying |
| GET | `/api/v1/manifests/{ns}/{name}` | Get resource status |
| DELETE | `/api/v1/manifests/{ns}/{name}` | Delete a resource |

### Job Operations

| Method | Endpoint | Description | CLI Command |
|--------|----------|-------------|-------------|
| GET | `/api/v1/jobs` | List jobs with pagination | `gco jobs list -r REGION` |
| GET | `/api/v1/jobs/{ns}/{name}` | Get job details | `gco jobs get NAME -r REGION` |
| GET | `/api/v1/jobs/{ns}/{name}/logs` | Get job logs | `gco jobs logs NAME -r REGION` |
| GET | `/api/v1/jobs/{ns}/{name}/events` | Get job events | `gco jobs events NAME -r REGION` |
| GET | `/api/v1/jobs/{ns}/{name}/pods` | Get job pods | `gco jobs pods NAME -r REGION` |
| GET | `/api/v1/jobs/{ns}/{name}/pods/{pod}/logs` | Get specific pod logs | `gco jobs pod-logs NAME POD -r REGION` |
| GET | `/api/v1/jobs/{ns}/{name}/metrics` | Get job resource metrics | `gco jobs metrics NAME -r REGION` |
| DELETE | `/api/v1/jobs/{ns}/{name}` | Delete a job | `gco jobs delete NAME -r REGION` |
| DELETE | `/api/v1/jobs` | Bulk delete jobs | `gco jobs bulk-delete -r REGION` |
| POST | `/api/v1/jobs/{ns}/{name}/retry` | Retry a failed job | `gco jobs retry NAME -r REGION` |

### Job Queue (Global)

The job queue provides centralized job submission with region targeting via DynamoDB.

| Method | Endpoint | Description | CLI Command |
|--------|----------|-------------|-------------|
| POST | `/api/v1/queue/jobs` | Submit job to global queue | `gco queue submit FILE -r REGION` |
| GET | `/api/v1/queue/jobs` | List queued jobs | `gco queue list` |
| GET | `/api/v1/queue/jobs/{id}` | Get queued job details | `gco queue get ID` |
| DELETE | `/api/v1/queue/jobs/{id}` | Cancel a queued job | `gco queue cancel ID` |
| GET | `/api/v1/queue/stats` | Queue statistics | `gco queue stats` |
| POST | `/api/v1/queue/poll` | Poll and process jobs (internal) | - |

### Job Templates

| Method | Endpoint | Description | CLI Command |
|--------|----------|-------------|-------------|
| GET | `/api/v1/templates` | List job templates | `gco templates list` |
| POST | `/api/v1/templates` | Create a job template | `gco templates create FILE -n NAME` |
| GET | `/api/v1/templates/{name}` | Get a template | `gco templates get NAME` |
| DELETE | `/api/v1/templates/{name}` | Delete a template | `gco templates delete NAME` |
| POST | `/api/v1/jobs/from-template/{name}` | Create job from template | `gco templates run NAME -n JOB -r REGION` |

### Webhooks

| Method | Endpoint | Description | CLI Command |
|--------|----------|-------------|-------------|
| GET | `/api/v1/webhooks` | List webhooks | `gco webhooks list` |
| POST | `/api/v1/webhooks` | Register a webhook | `gco webhooks create -u URL -e EVENT` |
| DELETE | `/api/v1/webhooks/{id}` | Delete a webhook | `gco webhooks delete ID` |

### Cost Reporting (Per Region)

Available when `cost_monitoring.enabled` is on (the default). Each region's manifest API proxies these to its in-cluster cost-monitor service; see [COST_MONITORING.md](COST_MONITORING.md).

| Method | Endpoint | Description | CLI Command |
|--------|----------|-------------|-------------|
| GET | `/api/v1/cost/status` | Cost monitoring + OpenCost health | `gco costs report status` |
| GET | `/api/v1/cost/reports` | List recent report objects | `gco costs report list` |
| POST | `/api/v1/cost/reports` | Generate an ad-hoc cost report | `gco costs report generate` |

### Inference

Requests are proxied to a deployed endpoint's in-cluster Service. `GET`, `HEAD`,
and `POST` are the only accepted methods, and `{upstream_path}` must match the
allowlist in [Allowed Upstream Paths](#allowed-upstream-paths). Full detail is in
[Inference API](#inference-api).

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET, HEAD, POST | `/inference/{endpoint_name}` | Proxy to the endpoint root |
| GET, HEAD, POST | `/inference/{endpoint_name}/{upstream_path}` | Proxy to an allowlisted sub-path, e.g. `v1/chat/completions` |

---

## Detailed Endpoint Documentation

### Global Jobs List

```http
GET /api/v1/global/jobs
```

List jobs across ALL regional clusters. This endpoint queries all regional ALBs in parallel and aggregates the results.

**CLI:**

```bash
gco jobs list --all-regions
gco jobs list -a --namespace gco-jobs --status running
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `namespace` | string | - | Filter by namespace |
| `status` | string | - | Filter by status |
| `limit` | int | 50 | Maximum jobs to return |

**Response:**

```json
{
  "total": 150,
  "count": 50,
  "limit": 50,
  "regions_queried": 3,
  "regions_successful": 3,
  "region_summaries": [
    {"region": "us-east-1", "count": 25, "total": 80},
    {"region": "us-west-2", "count": 15, "total": 45},
    {"region": "eu-west-1", "count": 10, "total": 25}
  ],
  "jobs": [
    {
      "metadata": {"name": "job-1", "namespace": "gco-jobs"},
      "_source_region": "us-east-1",
      "computed_status": "running"
    }
  ],
  "errors": null
}
```

### Global Health Status

```http
GET /api/v1/global/health
```

Get health status across all regional clusters.

**CLI:**

```bash
gco jobs health --all-regions
```

**Response:**

```json
{
  "overall_status": "healthy",
  "healthy_regions": 3,
  "total_regions": 3,
  "regions": [
    {
      "region": "us-east-1",
      "status": "healthy",
      "cluster_id": "gco-us-east-1"
    },
    {
      "region": "us-west-2",
      "status": "healthy",
      "cluster_id": "gco-us-west-2"
    }
  ]
}
```

### Global Bulk Delete

```http
DELETE /api/v1/global/jobs
```

Bulk delete jobs across all regional clusters.

**CLI:**

```bash
gco jobs bulk-delete --all-regions --status failed --older-than-days 30 --execute
```

**Request Body:**

```json
{
  "namespace": "gco-jobs",
  "status": "completed",
  "older_than_days": 7,
  "dry_run": true
}
```

**Response:**

```json
{
  "dry_run": false,
  "total_matched": 25,
  "total_deleted": 25,
  "regions_queried": 3,
  "region_results": [
    {"region": "us-east-1", "matched": 15, "deleted": 15, "failed": 0},
    {"region": "us-west-2", "matched": 10, "deleted": 10, "failed": 0}
  ],
  "errors": null
}
```

### List Jobs

```http
GET /api/v1/jobs
```

List Kubernetes Jobs with pagination and filtering.

**CLI:**

```bash
# List jobs in a specific region
gco jobs list --region us-east-1

# List jobs across all regions
gco jobs list --all-regions

# Filter by namespace and status
gco jobs list -r us-west-2 -n gco-jobs --status running

# Limit results
gco jobs list -r us-east-1 --limit 10
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `namespace` | string | - | Filter by namespace |
| `status` | string | - | Filter by status (pending, running, completed, succeeded, failed) |
| `limit` | int | 50 | Maximum jobs to return (1-1000) |
| `offset` | int | 0 | Number of jobs to skip |
| `sort` | string | createdAt:desc | Sort field and order (field:asc\|desc) |
| `label_selector` | string | - | Kubernetes label selector (e.g., app=test) |

**Response:**

```json
{
  "cluster_id": "gco-cluster",
  "region": "us-east-1",
  "timestamp": "2024-01-15T10:30:00Z",
  "total": 100,
  "limit": 50,
  "offset": 0,
  "has_more": true,
  "count": 50,
  "jobs": [
    {
      "metadata": {
        "name": "training-job-001",
        "namespace": "gco-jobs",
        "creationTimestamp": "2024-01-15T10:00:00Z",
        "labels": {"app": "ml-training"},
        "uid": "abc123"
      },
      "spec": {
        "parallelism": 1,
        "completions": 1,
        "backoffLimit": 6
      },
      "status": {
        "active": 1,
        "succeeded": 0,
        "failed": 0,
        "startTime": "2024-01-15T10:00:05Z",
        "completionTime": null,
        "conditions": []
      },
      "computed_status": "running"
    }
  ]
}
```

### Get Job Logs

```http
GET /api/v1/jobs/{namespace}/{name}/logs
```

Get logs from a Job's pods with multi-container support.

**CLI:**

```bash
# Get logs from a job
gco jobs logs my-job --region us-east-1

# Get more lines
gco jobs logs training-job -r us-west-2 -n ml-jobs --tail 500

# Get logs from specific container
gco jobs logs multi-container-job -r us-east-1 --container sidecar
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `container` | string | - | Container name (for multi-container pods) |
| `tail` | int | 100 | Number of lines from the end (1-10000) |
| `previous` | bool | false | Get logs from previous terminated container |
| `since_seconds` | int | - | Only return logs newer than N seconds |
| `timestamps` | bool | false | Include timestamps in log lines |

**Response:**

```json
{
  "cluster_id": "gco-cluster",
  "region": "us-east-1",
  "timestamp": "2024-01-15T10:30:00Z",
  "job_name": "training-job-001",
  "namespace": "gco-jobs",
  "pod_name": "training-job-001-abc123",
  "container": "main",
  "available_containers": ["main", "sidecar"],
  "init_containers": ["init-data"],
  "previous": false,
  "tail_lines": 100,
  "logs": "2024-01-15 10:00:05 Starting training...\n2024-01-15 10:00:10 Epoch 1/10..."
}
```

### Get Job Events

```http
GET /api/v1/jobs/{namespace}/{name}/events
```

Get Kubernetes events related to a Job and its pods.

**CLI:**

```bash
gco jobs events my-job --region us-east-1
gco jobs events training-job -n ml-jobs -r us-west-2
```

**Response:**

```json
{
  "cluster_id": "gco-cluster",
  "region": "us-east-1",
  "timestamp": "2024-01-15T10:30:00Z",
  "job_name": "training-job-001",
  "namespace": "gco-jobs",
  "count": 3,
  "events": [
    {
      "type": "Normal",
      "reason": "SuccessfulCreate",
      "message": "Created pod: training-job-001-abc123",
      "count": 1,
      "firstTimestamp": "2024-01-15T10:00:00Z",
      "lastTimestamp": "2024-01-15T10:00:00Z",
      "source": {
        "component": "job-controller",
        "host": null
      },
      "involvedObject": {
        "kind": "Job",
        "name": "training-job-001",
        "namespace": "gco-jobs"
      }
    }
  ]
}
```

### Get Job Pods

```http
GET /api/v1/jobs/{namespace}/{name}/pods
```

Get detailed information about all pods created by a Job.

**CLI:**

```bash
gco jobs pods my-job -r us-east-1
gco jobs pods training-job -n ml-jobs -r us-west-2
```

**Response:**

```json
{
  "cluster_id": "gco-cluster",
  "region": "us-east-1",
  "timestamp": "2024-01-15T10:30:00Z",
  "job_name": "training-job-001",
  "namespace": "gco-jobs",
  "count": 1,
  "pods": [
    {
      "metadata": {
        "name": "training-job-001-abc123",
        "namespace": "gco-jobs",
        "creationTimestamp": "2024-01-15T10:00:00Z",
        "labels": {"job-name": "training-job-001"},
        "uid": "pod-uid-123"
      },
      "spec": {
        "nodeName": "ip-10-0-1-100.ec2.internal",
        "containers": [{"name": "main", "image": "pytorch:latest"}],
        "initContainers": []
      },
      "status": {
        "phase": "Running",
        "hostIP": "10.0.1.100",
        "podIP": "10.0.2.50",
        "startTime": "2024-01-15T10:00:05Z",
        "containerStatuses": [
          {
            "name": "main",
            "ready": true,
            "restartCount": 0,
            "image": "pytorch:latest",
            "state": "running",
            "startedAt": "2024-01-15T10:00:10Z"
          }
        ],
        "initContainerStatuses": []
      }
    }
  ]
}
```

### Get Job Metrics

```http
GET /api/v1/jobs/{namespace}/{name}/metrics
```

Get resource usage metrics for a Job's pods (requires metrics-server).

**CLI:**

```bash
gco jobs metrics my-job --region us-east-1
gco jobs metrics training-job -n ml-jobs -r us-west-2
```

**Response:**

```json
{
  "cluster_id": "gco-cluster",
  "region": "us-east-1",
  "timestamp": "2024-01-15T10:30:00Z",
  "job_name": "training-job-001",
  "namespace": "gco-jobs",
  "summary": {
    "total_cpu_millicores": 2500,
    "total_memory_bytes": 4294967296,
    "total_memory_mib": 4096.0,
    "pod_count": 1
  },
  "pods": [
    {
      "pod_name": "training-job-001-abc123",
      "containers": [
        {
          "name": "main",
          "cpu_millicores": 2500,
          "memory_bytes": 4294967296,
          "memory_mib": 4096.0
        }
      ]
    }
  ]
}
```

### Bulk Delete Jobs

```http
DELETE /api/v1/jobs
```

Bulk delete jobs based on filters.

**CLI:**

```bash
# Dry run (preview what would be deleted)
gco jobs bulk-delete --region us-east-1 --status completed --older-than-days 7

# Actually delete (use --execute)
gco jobs bulk-delete -r us-west-2 -n gco-jobs -s failed --execute -y

# Delete across all regions
gco jobs bulk-delete --all-regions --status failed --older-than-days 30 --execute
```

**Request Body:**

```json
{
  "namespace": "gco-jobs",
  "status": "completed",
  "older_than_days": 7,
  "label_selector": "app=test",
  "dry_run": true
}
```

| Field | Type | Description |
|-------|------|-------------|
| `namespace` | string | Filter by namespace |
| `status` | string | Filter by status |
| `older_than_days` | int | Delete jobs older than N days (1-365) |
| `label_selector` | string | Kubernetes label selector |
| `dry_run` | bool | If true, only return what would be deleted |

**Response:**

```json
{
  "cluster_id": "gco-cluster",
  "region": "us-east-1",
  "timestamp": "2024-01-15T10:30:00Z",
  "dry_run": false,
  "total_matched": 5,
  "deleted_count": 5,
  "failed_count": 0,
  "jobs": [
    {"name": "old-job-1", "namespace": "gco-jobs"},
    {"name": "old-job-2", "namespace": "gco-jobs"}
  ],
  "failed": null
}
```

### Retry Job

```http
POST /api/v1/jobs/{namespace}/{name}/retry
```

Retry a failed job by creating a new job from its spec.

**CLI:**

```bash
gco jobs retry failed-job --region us-east-1
gco jobs retry training-job -n ml-jobs -r us-west-2 -y
```

**Response:**

```json
{
  "cluster_id": "gco-cluster",
  "region": "us-east-1",
  "timestamp": "2024-01-15T10:30:00Z",
  "original_job": "failed-job-001",
  "new_job": "failed-job-001-retry-20240115103000",
  "namespace": "gco-jobs",
  "success": true,
  "message": "Job retry created successfully",
  "errors": []
}
```

---

## Job Queue (DynamoDB-backed)

The job queue provides centralized job submission with region targeting. Jobs are stored in DynamoDB and picked up by regional manifest processors, enabling:

- Global job submission from any region
- Centralized job tracking and status updates
- Priority-based job scheduling
- Full job history and audit trail

### Submit to Queue

```http
POST /api/v1/queue/jobs
```

Submit a job to the global queue for regional pickup.

**CLI:**

```bash
# Submit job targeting us-east-1
gco queue submit job.yaml --region us-east-1

# Submit with priority
gco queue submit job.yaml -r us-west-2 --priority 50

# Submit with labels
gco queue submit job.yaml -r us-east-1 -l team=ml -l project=training

# Submit with a spot price gate (dispatch only at or below $0.50/hour)
gco queue submit job.yaml -r us-east-1 --max-spot-price 0.50 --spot-instance-type g5.xlarge
```

**Request Body:**

```json
{
  "manifest": {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {"name": "my-training-job"},
    "spec": {
      "template": {
        "spec": {
          "containers": [{"name": "train", "image": "pytorch:latest"}],
          "restartPolicy": "Never"
        }
      }
    }
  },
  "target_region": "us-east-1",
  "namespace": "gco-jobs",
  "priority": 10,
  "labels": {"team": "ml"},
  "max_spot_price": 0.5,
  "spot_instance_type": "g5.xlarge"
}
```

`max_spot_price` (USD/hour) and `spot_instance_type` are optional and must be provided together. When set, the target region's queue worker holds the job in `queued` until the instance type's lowest current spot price across the region's Availability Zones drops to or below the cap; the job record then carries `spot_gate_checked_at` / `spot_gate_observed_price` so `GET /api/v1/queue/jobs/{job_id}` shows why a gated job is waiting. Gated jobs wait indefinitely (cancel with `DELETE /api/v1/queue/jobs/{job_id}`) and never block other queued work. See [COST_MONITORING.md](COST_MONITORING.md#spot-price-aware-scheduling).

**Response:**

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "message": "Job queued successfully",
  "job": {
    "job_id": "abc123-def456-ghi789",
    "job_name": "my-training-job",
    "target_region": "us-east-1",
    "namespace": "gco-jobs",
    "status": "queued",
    "priority": 10,
    "submitted_at": "2024-01-15T10:30:00Z"
  }
}
```

### List Queued Jobs

```http
GET /api/v1/queue/jobs
```

List jobs in the global queue with optional filters.

**CLI:**

```bash
# List all queued jobs
gco queue list

# Filter by region and status
gco queue list --region us-east-1 --status queued

# Filter by status only
gco queue list -s running
```

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `target_region` | string | Filter by target region |
| `status` | string | Filter by status (queued, claimed, running, succeeded, failed, cancelled) |
| `namespace` | string | Filter by namespace |
| `limit` | int | Maximum results (default: 100) |

**Response:**

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "count": 5,
  "jobs": [
    {
      "job_id": "abc123-def456",
      "job_name": "training-job-1",
      "target_region": "us-east-1",
      "namespace": "gco-jobs",
      "status": "queued",
      "priority": 10,
      "submitted_at": "2024-01-15T10:00:00Z"
    }
  ]
}
```

### Get Queued Job

```http
GET /api/v1/queue/jobs/{job_id}
```

Get details of a specific queued job including full status history.

**CLI:**

```bash
gco queue get abc123-def456
gco queue get abc123-def456 --region us-east-1
```

**Response:**

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "job": {
    "job_id": "abc123-def456",
    "job_name": "training-job-1",
    "target_region": "us-east-1",
    "namespace": "gco-jobs",
    "status": "running",
    "priority": 10,
    "manifest": {"apiVersion": "batch/v1", "...": "..."},
    "labels": {"team": "ml"},
    "submitted_at": "2024-01-15T10:00:00Z",
    "claimed_by": "us-east-1",
    "claimed_at": "2024-01-15T10:00:05Z",
    "k8s_job_uid": "k8s-uid-123",
    "status_history": [
      {"status": "queued", "timestamp": "2024-01-15T10:00:00Z", "message": "Job submitted"},
      {"status": "claimed", "timestamp": "2024-01-15T10:00:05Z"},
      {"status": "applying", "timestamp": "2024-01-15T10:00:06Z"},
      {"status": "running", "timestamp": "2024-01-15T10:00:10Z"}
    ]
  }
}
```

### Cancel Queued Job

```http
DELETE /api/v1/queue/jobs/{job_id}
```

Cancel a queued job. Only works for jobs in `queued` or `claimed` status.

**CLI:**

```bash
gco queue cancel abc123-def456
gco queue cancel abc123-def456 --reason "No longer needed"
```

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `reason` | string | Optional cancellation reason |

**Response:**

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "message": "Job 'abc123-def456' cancelled successfully"
}
```

### Queue Statistics

```http
GET /api/v1/queue/stats
```

Get job queue statistics grouped by region and status.

**CLI:**

```bash
gco queue stats
```

**Response:**

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "summary": {
    "total_jobs": 150,
    "total_queued": 10,
    "total_running": 25
  },
  "by_region": {
    "us-east-1": {
      "queued": 5,
      "running": 15,
      "succeeded": 50,
      "failed": 3
    },
    "us-west-2": {
      "queued": 5,
      "running": 10,
      "succeeded": 40,
      "failed": 2
    }
  }
}
```

---

## Cost Reporting

Region-scoped cost reporting backed by [OpenCost](https://opencost.io/) and the central cost report bucket. Each region's manifest API answers for its own cluster, so pin the request to a region (`gco costs report ... --region REGION` uses the regional API bridge) or accept the nearest healthy region through the global API — the response names the region that answered. Cross-region aggregation lives in [Athena](https://docs.aws.amazon.com/athena/latest/ug/what-is.html) via `gco costs k8s`; see [COST_MONITORING.md](COST_MONITORING.md).

### Cost Monitoring Status

```http
GET /api/v1/cost/status
```

**CLI:**

```bash
gco costs report status -r us-east-1
```

**Response:**

```json
{
  "service": "cost-monitor",
  "region": "us-east-1",
  "cluster": "gco-us-east-1",
  "bucket": "gco-cost-reports-123456789012-us-east-2",
  "report_interval_minutes": 60,
  "opencost_healthy": true,
  "opencost_returning_data": true,
  "allocation_names": ["__idle__", "gco-jobs", "gco-system", "monitoring"],
  "last_scheduled_report": {
    "s3_key": "reports/region=us-east-1/date=2026-07-26/allocation-20260726T090000Z-20260726T100000Z.parquet",
    "row_count": 9,
    "total_cost": 1.4137,
    "window_start": "2026-07-26T09:00:00+00:00",
    "window_end": "2026-07-26T10:00:00+00:00"
  },
  "last_error": null,
  "timestamp": "2026-07-26T10:42:00+00:00"
}
```

`opencost_returning_data` performs a live allocation probe — release validation gates on it, so a healthy-but-empty OpenCost (for example, a broken [Prometheus](https://prometheus.io/docs/introduction/overview/) scrape) is surfaced here rather than silently producing empty reports.

### List Cost Reports

```http
GET /api/v1/cost/reports?adhoc=false&limit=50
```

List this region's most recent report objects, newest first. `adhoc=true` lists user-requested reports instead of the scheduled ones.

**CLI:**

```bash
gco costs report list -r us-east-1 --limit 50
```

**Response:**

```json
{
  "timestamp": "2026-07-26T10:42:00+00:00",
  "region": "us-east-1",
  "bucket": "gco-cost-reports-123456789012-us-east-2",
  "count": 1,
  "reports": [
    {
      "key": "reports/region=us-east-1/date=2026-07-26/allocation-20260726T090000Z-20260726T100000Z.parquet",
      "size_bytes": 6212,
      "last_modified": "2026-07-26T10:00:04+00:00"
    }
  ]
}
```

### Generate Ad-hoc Cost Report

```http
POST /api/v1/cost/reports
```

Generate one allocation report for the trailing window now. Ad-hoc reports land under the `adhoc/` prefix — deliberately outside the scheduled `reports/` prefix Athena aggregates, so an overlapping window can never double-count.

**CLI:**

```bash
gco costs report generate -r us-east-1 --window-hours 48
```

**Request Body:**

```json
{
  "window_hours": 24,
  "include_rows": false
}
```

**Response (201):**

```json
{
  "timestamp": "2026-07-26T10:42:05+00:00",
  "region": "us-east-1",
  "bucket": "gco-cost-reports-123456789012-us-east-2",
  "report": {
    "s3_key": "adhoc/region=us-east-1/date=2026-07-26/allocation-20260725T104200Z-20260726T104200Z-1a2b3c4d.parquet",
    "row_count": 9,
    "total_cost": 33.92,
    "window_start": "2026-07-25T10:42:00+00:00",
    "window_end": "2026-07-26T10:42:00+00:00"
  }
}
```

With `"include_rows": true` the response also carries the normalized per-namespace allocation rows. Errors map cleanly: `503` when OpenCost is unreachable (or cost monitoring is disabled in this region), `502` when the S3 write fails, `422` for an invalid window.

---

## Job Templates

Templates allow you to define reusable job configurations with parameter substitution.

### List Templates

```http
GET /api/v1/templates
```

**CLI:**

```bash
gco templates list
```

### Create Template

```http
POST /api/v1/templates
```

**CLI:**

```bash
# Create from manifest file
gco templates create job.yaml --name gpu-training-template -d "GPU training template"

# With default parameters
gco templates create job.yaml -n my-template -p image=pytorch:latest -p gpus=4
```

**Request Body:**

```json
{
  "name": "gpu-training-template",
  "description": "Template for GPU training jobs",
  "manifest": {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {"name": "{{name}}"},
    "spec": {
      "template": {
        "spec": {
          "containers": [{
            "name": "train",
            "image": "{{image}}",
            "resources": {
              "limits": {"nvidia.com/gpu": "{{gpu_count}}"}
            }
          }],
          "restartPolicy": "Never"
        }
      }
    }
  },
  "parameters": {
    "image": "pytorch/pytorch:latest",
    "gpu_count": "1"
  }
}
```

### Create Job from Template

```http
POST /api/v1/jobs/from-template/{name}
```

**CLI:**

```bash
# Create job from template
gco templates run gpu-training-template --name my-job --region us-east-1

# With parameter overrides
gco templates run gpu-template -n my-job -r us-east-1 -p image=custom:v1 -p gpus=8
```

**Request Body:**

```json
{
  "name": "my-training-job",
  "namespace": "gco-jobs",
  "parameters": {
    "image": "my-custom-image:v1",
    "gpu_count": "4"
  }
}
```

**Response:**

```json
{
  "cluster_id": "gco-cluster",
  "region": "us-east-1",
  "timestamp": "2024-01-15T10:30:00Z",
  "template": "gpu-training-template",
  "job_name": "my-training-job",
  "namespace": "gco-jobs",
  "success": true,
  "parameters_applied": {
    "name": "my-training-job",
    "image": "my-custom-image:v1",
    "gpu_count": "4"
  },
  "errors": []
}
```

---

## Webhooks

Webhooks allow you to receive notifications when job events occur. The webhook dispatcher monitors Kubernetes jobs for status changes and sends HTTP POST requests to registered webhook endpoints.

### Webhook Delivery

When a job event occurs (started, completed, or failed), the webhook dispatcher:

1. Detects the job status transition
2. Queries matching webhooks from DynamoDB based on event type and namespace
3. Sends HTTP POST requests to all matching webhook URLs
4. Retries failed deliveries with exponential backoff (up to 3 attempts)

### Webhook Payload Format

All webhook deliveries use the following JSON payload format:

```json
{
  "event": "job.completed",
  "timestamp": "2024-01-15T12:00:00Z",
  "cluster_id": "gco-cluster-us-east-1",
  "region": "us-east-1",
  "job": {
    "name": "my-training-job",
    "namespace": "gco-jobs",
    "uid": "abc-123-def-456",
    "labels": {"app": "ml-training", "team": "data-science"},
    "status": "succeeded",
    "start_time": "2024-01-15T11:55:00Z",
    "completion_time": "2024-01-15T12:00:00Z",
    "active": 0,
    "succeeded": 1,
    "failed": 0
  }
}
```

### Webhook Headers

Each webhook request includes the following headers:

| Header | Description |
|--------|-------------|
| `Content-Type` | `application/json` |
| `User-Agent` | `GCO-Webhook/<cluster-id>` |
| `X-GCO-Event` | Event type (e.g., `job.completed`) |
| `X-GCO-Cluster` | Cluster ID |
| `X-GCO-Region` | AWS region |
| `X-GCO-Signature` | HMAC-SHA256 signature (if secret configured) |

### HMAC Signature Verification

When a webhook has a secret configured, the payload is signed using HMAC-SHA256. The signature is included in the `X-GCO-Signature` header as `sha256=<hex_digest>`.

To verify the signature in your webhook handler:

```python
import hmac
import hashlib


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)
```

### Retry Behavior

- **5xx errors**: Retried up to 3 times with exponential backoff (5s, 10s, 20s)
- **4xx errors**: Not retried (client error)
- **Timeouts**: Retried up to 3 times (default timeout: 30 seconds)
- **Connection errors**: Retried up to 3 times

### List Webhooks

```http
GET /api/v1/webhooks
```

**CLI:**

```bash
gco webhooks list
gco webhooks list --namespace gco-jobs
```

### Register Webhook

```http
POST /api/v1/webhooks
```

**CLI:**

```bash
# Register webhook for job events
gco webhooks create --url https://example.com/webhook -e job.completed -e job.failed

# Filter by namespace
gco webhooks create -u https://slack.com/webhook -e job.failed -n gco-jobs

# With HMAC secret for signature verification
gco webhooks create -u https://example.com/webhook -e job.completed --secret my-secret-key
```

**Request Body:**

```json
{
  "url": "https://example.com/webhook",
  "events": ["job.completed", "job.failed", "job.started"],
  "namespace": "gco-jobs",
  "secret": "optional-hmac-secret"
}
```

**Available Events:**

- `job.started` - Job started running (transitioned from pending to running)
- `job.completed` - Job completed successfully
- `job.failed` - Job failed

**Response:**

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "message": "Webhook registered successfully",
  "webhook": {
    "id": "abc12345",
    "url": "https://example.com/webhook",
    "events": ["job.completed", "job.failed"],
    "namespace": "gco-jobs"
  }
}
```

### Delete Webhook

```http
DELETE /api/v1/webhooks/{id}
```

**CLI:**

```bash
gco webhooks delete abc12345
gco webhooks delete abc12345 -y  # Skip confirmation
```

---

## Inference API

The inference proxy sits in front of every endpoint deployed by
`gco inference deploy`. It exists so callers reach models through the same
IAM-authenticated edge as the control plane: the proxy resolves the endpoint
name against the DynamoDB endpoint store, then forwards the request to that
endpoint's in-cluster Service. Callers never address a pod, Service, or model
container directly, and cannot supply an upstream host.

For deploying and managing endpoints, see the
[Inference Guide](INFERENCE.md); this section covers only the request surface.

### Invoking an Endpoint

```http
GET|HEAD|POST /inference/{endpoint_name}
GET|HEAD|POST /inference/{endpoint_name}/{upstream_path}
```

`{endpoint_name}` must be a valid Kubernetes DNS label; anything else returns
`404`. Because most model servers expose an OpenAI-compatible surface, the
common call is a `POST` to `v1/chat/completions`:

```bash
export API_GATEWAY_ENDPOINT=<API_GATEWAY_ENDPOINT>

awscurl --service execute-api --region us-east-1 \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{
        "model": "my-llm",
        "messages": [{"role": "user", "content": "Hello"}]
      }' \
  "https://$API_GATEWAY_ENDPOINT/inference/my-llm/v1/chat/completions"
```

List the models a running endpoint serves:

```bash
awscurl --service execute-api --region us-east-1 \
  "https://$API_GATEWAY_ENDPOINT/inference/my-llm/v1/models"
```

Only `Accept`, `Accept-Encoding`, `Cache-Control`, `Content-Encoding`,
`Content-Type`, `Idempotency-Key`, `If-Match`, `If-None-Match`, `Prefer`,
`Range`, `User-Agent`, and `X-Request-Id` are forwarded upstream. Hop-by-hop
headers and anything else a caller sends are dropped, so a model server never
sees caller-supplied auth or routing headers.

### Allowed Upstream Paths

`{upstream_path}` is matched against an allowlist rather than passed through, so
a caller cannot reach a model server's administrative surface. Requests outside
the allowlist are rejected before any upstream connection is made.

| Pattern | Examples |
|---------|----------|
| `v1/models` and `v1/models/{model}` | `v1/models`, `v1/models/my-llm` |
| `v1/chat/completions`, `v1/completions`, `v1/embeddings`, `v1/responses` | OpenAI-compatible generation calls |
| `v2/models/...` with optional `config`, `infer`, `ready`, `stats` | `v2/models/my-llm/infer` (Triton) |
| The endpoint's own configured health path | as recorded on the endpoint |

The path segments `admin`, `debug`, `docs`, `instances`, `metrics`, and
`openapi.json` are rejected outright, as are `.` and `..` segments. A request to
an unlisted path returns `404`.

### Streaming, Timeouts, and Failure Codes

Responses stream back to the caller unbuffered, so server-sent-event token
streams work end to end. At the edge this is why `/inference/*` is served by a
response-streaming Lambda rather than the buffered proxy used for `/api/v1/*`,
and why the WAF body-size limit that applies elsewhere is not applied to
`/inference/*`.

Upstream timeouts are bounded and configurable through environment variables on
the inference-proxy deployment:

| Setting | Default | Bounds |
|---------|---------|--------|
| `INFERENCE_PROXY_CONNECT_TIMEOUT_SECONDS` | 5 | 0.1–30 |
| `INFERENCE_PROXY_READ_TIMEOUT_SECONDS` | 300 | 1–900 |
| `INFERENCE_PROXY_WRITE_TIMEOUT_SECONDS` | 30 | 1–300 |
| `INFERENCE_PROXY_POOL_TIMEOUT_SECONDS` | 5 | 0.1–30 |

| Status | Meaning |
|--------|---------|
| `404` | Unknown endpoint name, invalid DNS label, or a path outside the allowlist |
| `400` | Path contained `.` or `..` segments |
| `502` | The upstream endpoint could not be reached |
| `503` | The endpoint record exists but its spec is unusable |
| `504` | The upstream endpoint exceeded the read timeout |

Any other status is the model server's own response, passed through unmodified.

### Canary and Disaggregated Routing

The target Service is always derived from the stored endpoint record, never from
the request:

- **Plain endpoints** resolve to the `{endpoint_name}` Service.
- **Canary deployments** send a cryptographically unbiased sample of requests to
  `{endpoint_name}-canary`, matching the endpoint's configured canary weight. A
  caller cannot select the canary explicitly.
- **Mooncake disaggregated endpoints** (`mode: disaggregated` or `both`) resolve
  to the reconciled `{endpoint_name}-proxy` Service, which performs
  prefill/decode dispatch internally.

## Health, Readiness, and Observability

Three services answer probe traffic. `/api/v1/health` and `/api/v1/metrics` are
exposed through an API Gateway; the rest are in-cluster, reached by addressing a
Service directly or scraped by the cluster's Prometheus.

| Endpoint | Service | Auth | Purpose |
|----------|---------|------|---------|
| `GET /api/v1/health` | `health-monitor` | None | `200` when the cluster is healthy, `503` otherwise. Global Accelerator health check target |
| `GET /api/v1/metrics` | `health-monitor` | HMAC envelope | CPU / memory / GPU utilization, configured thresholds, active job count, threshold violations |
| `GET /api/v1/status` | `health-monitor` | HMAC envelope | Monitor initialization, background task state, webhook dispatcher counters |
| `GET /api/v1/status` | `manifest-processor` | HMAC envelope | Resource limits, allowed namespaces, template/webhook counts, central queue worker health |
| `GET /api/v1/policy` | `manifest-processor` | HMAC envelope | The effective job validation policy this region enforces — per-manifest cpu/memory/gpu caps, `allowed_namespaces`, `allowed_kinds`, `allowed_api_versions`, `trusted_registries`, `trusted_dockerhub_orgs`, `require_accelerator_toleration`, `yaml_max_depth`, the eight `manifest_security_policy.block_*` flags, and each allowed namespace's live ResourceQuota / LimitRange ceilings. `503` until the processor is initialized |
| `GET /healthz` | all three | None | Liveness. Always `200` when the process is up |
| `GET /readyz` | all three | None | Readiness. `503` until dependencies are initialized; the manifest processor also reports `503` if its central queue worker has stopped |
| `GET /metrics` | all three | None | Prometheus exposition for the in-cluster scrape |

`/healthz`, `/readyz`, `/metrics`, and `/api/v1/health` are the only paths that
bypass the HMAC envelope; see [Authentication](#authentication).

The health monitor also publishes resource utilization and health status to
CloudWatch every 30 seconds independently of these endpoints, which is how
dashboards and alarms are fed. See the [Monitoring Guide](MONITORING.md).

> **Which `/api/v1/status` you get.** As described in
> [API Surface at a Glance](#api-surface-at-a-glance), the ALB resolves
> `/api/v1/status` to the manifest processor, because a path prefix cannot be
> split across two Services and that is the response the cross-region
> aggregator consumes. The health monitor's `/api/v1/status` row above describes
> its in-cluster response; through either API Gateway, `/api/v1/status` returns
> the manifest processor's. The health monitor's `/api/v1/metrics` has its own
> route and is reachable through the gateway.

### Reading the deployed validation policy

`GET /api/v1/policy` answers one question: **will this cluster admit the job I
am about to submit?** Ask it before submission and a policy conflict surfaces
at plan time instead of after a region has been provisioned and billed.

```bash
gco jobs policy --region us-east-1
gco jobs policy --region us-east-1 -o json | jq '.policy.trusted_registries'
```

The response reads the live `ManifestProcessor` instance, so it reflects what
that region enforces right now. **A local `cdk.json` is not a substitute.** It
is the input to a deploy, not the state of one, and it diverges from the
deployed policy for two independent reasons:

- The cluster may have been deployed from a different checkout than the one you
  are reading.
- CDK augments `trusted_registries` with the project's own ECR registry
  hostnames at synth time, so the effective allowlist is always strictly larger
  than the configured one.

Because both the REST manifest processor and the SQS queue processor read the
same environment variables, this endpoint describes both submission paths —
neither is a bypass of the other.

Three layers govern admission, and the response reports all three:

| Layer | Where it comes from | Response key |
|-------|--------------------|--------------|
| Front-door policy — per-manifest caps, namespace/kind/registry allowlists, pod-security flags | `cdk.json::job_validation_policy`, baked into the service's container env at deploy | `policy` |
| Per-container ceilings | the namespace's `LimitRange` | `cluster_enforcement.<ns>.limit_ranges` |
| Aggregate ceilings | the namespace's `ResourceQuota` | `cluster_enforcement.<ns>.resource_quotas` |

A manifest must clear all three. Reporting only the first would be misleading:
a job can pass the front-door cap and still be rejected by the `LimitRange`.
Layers 2 and 3 are read live from the Kubernetes API and degrade to
`{"status": "unavailable", "reason": "..."}` per namespace rather than failing
the whole response, so a missing layer is always explicit rather than inferred
from an absent key.

Caps appear in the units the validator actually compares in
(`max_cpu_millicores`, `max_memory_bytes`, `max_gpu_count`) alongside the raw
configured strings under `manifest_caps.configured`, because `"384"` vCPU and
`384000` millicores are the same cap and a caller doing a local pre-check needs
to know which one it is holding.

Note that `gco jobs submit --dry-run` is **not** an admission preview — it runs
`kubectl --dry-run=client`, a client-side parse that never consults this policy.

## Cluster-Internal Surfaces

These exist in running deployments but are not exposed by any API Gateway. They
are documented so operators recognize them in logs, network policies, and
port-forward sessions.

### Cost Monitor

The cost-monitor service is reachable only from the manifest processor, enforced
by a Kubernetes NetworkPolicy, and runs no authentication middleware of its own.
The manifest processor's `/api/v1/cost/*` routes are the authenticated front for
these:

| Endpoint | Description |
|----------|-------------|
| `GET /internal/status` | Cost monitoring configuration and OpenCost health |
| `GET /internal/reports` | Recent report objects. Query: `adhoc` (default `false`), `limit` (1–1000, default 50) |
| `POST /internal/reports` | Generate an ad-hoc report; `201` on success. Body: `window_hours` (1–168, default 24), `include_rows` (default `false`) |

### Mooncake Prefill/Decode Proxy

Deployed only for endpoints using Mooncake disaggregation, as the
`{endpoint_name}-proxy` Service:

| Endpoint | Description |
|----------|-------------|
| `GET /health`, `GET /healthz` | Liveness for the proxy pod |
| `POST /instances/add` | Register a prefill or decode instance. Requires the `ADMIN_API_KEY` shared secret via `x-admin-api-key` or `Authorization: Bearer`; `403` otherwise. Never exposed through an Ingress |
| `POST /{path}` | Disaggregation dispatch for serving paths. `503` when no decode backend is Ready |
| `GET /{path}` | Catch-all `200` so ALB health checks succeed |

The inference proxy's allowlist blocks the `instances` path segment, so
`/instances/add` cannot be reached through `/inference/{endpoint_name}/...`.

### Service Descriptors

Each request-serving application answers its own root path with a small
descriptor: service name, version, running status, and an index of the routes it
implements. No API Gateway forwards `/`, so these are in-cluster only. They are
useful when port-forwarding to confirm which service a pod is actually running.

| Endpoint | Service | Returns |
|----------|---------|---------|
| `GET /` | `manifest-processor` | Name, version, cluster ID, region, and an index of every `/api/v1` route group |
| `GET /` | `health-monitor` | Name, version, and its three `/api/v1` endpoints |
| `GET /` | `inference-proxy` | Name, version, and proxy status |

### Interactive API Documentation

Each FastAPI service serves `/docs` (Swagger UI), `/redoc`, and `/openapi.json`.
No API Gateway forwards these prefixes, so they are in-cluster only. For a local
copy of the same schemas, see the committed documents in
[`docs/openapi/`](openapi/) or regenerate them with
[`scripts/generate_openapi.py`](../scripts/generate_openapi.py).

---

## Error Responses

All error responses follow this format:

```json
{
  "error": "Error type",
  "detail": "Detailed error message",
  "timestamp": "2024-01-15T10:30:00Z"
}
```

**Common HTTP Status Codes:**

| Code | Description |
|------|-------------|
| 200 | Success |
| 201 | Created |
| 400 | Bad Request - Invalid input |
| 403 | Forbidden - Namespace not allowed |
| 404 | Not Found - Resource doesn't exist |
| 409 | Conflict - Resource already exists |
| 500 | Internal Server Error |
| 503 | Service Unavailable - Processor not ready |

---

## Examples

### Using the CLI (Recommended)

The `gco` CLI handles authentication automatically using your AWS credentials.

```bash
# Submit a job
gco jobs submit job.yaml --region us-east-1

# Submit to global queue (DynamoDB-backed)
gco queue submit job.yaml --region us-east-1 --priority 10

# List jobs across all regions
gco jobs list --all-regions

# Get job logs
gco jobs logs my-job --region us-east-1 --tail 500

# Create and use a template
gco templates create job.yaml --name gpu-template -d "GPU training"
gco templates run gpu-template --name my-job --region us-east-1

# Register a webhook
gco webhooks create --url https://example.com/hook -e job.completed -e job.failed

# Check queue statistics
gco queue stats

# Bulk delete old completed jobs
gco jobs bulk-delete --all-regions --status completed --older-than-days 7 --execute
```

### Using awscurl (Direct API Access)

All examples below use `awscurl` for SigV4 authentication. Install with `pip install awscurl`.

Set `API_GATEWAY_ENDPOINT` once and reuse it in every request. Replace the
`<API_GATEWAY_ENDPOINT>` placeholder with the host from the `ApiGatewayUrl`
CloudFormation output (for example `abc123.execute-api.us-east-1.amazonaws.com`):

```bash
export API_GATEWAY_ENDPOINT=<API_GATEWAY_ENDPOINT>
```

### Submit a Job

```bash
awscurl --service execute-api --region us-east-1 \
  -X POST "https://$API_GATEWAY_ENDPOINT/api/v1/manifests" \
  -H "Content-Type: application/json" \
  -d '{
    "manifests": [{
      "apiVersion": "batch/v1",
      "kind": "Job",
      "metadata": {
        "name": "my-job",
        "namespace": "gco-jobs"
      },
      "spec": {
        "template": {
          "spec": {
            "containers": [{
              "name": "main",
              "image": "python:3.11",
              "command": ["python", "-c", "print(\"Hello World\")"]
            }],
            "restartPolicy": "Never"
          }
        }
      }
    }]
  }'
```

### List Jobs with Pagination

```bash
awscurl --service execute-api --region us-east-1 \
  "https://$API_GATEWAY_ENDPOINT/api/v1/jobs?namespace=gco-jobs&limit=10&offset=0&status=running"
```

### Get Job Logs with Container Selection

```bash
awscurl --service execute-api --region us-east-1 \
  "https://$API_GATEWAY_ENDPOINT/api/v1/jobs/gco-jobs/my-job/logs?container=main&tail=500&timestamps=true"
```

### Bulk Delete Old Completed Jobs

```bash
awscurl --service execute-api --region us-east-1 \
  -X DELETE "https://$API_GATEWAY_ENDPOINT/api/v1/jobs" \
  -H "Content-Type: application/json" \
  -d '{
    "namespace": "gco-jobs",
    "status": "completed",
    "older_than_days": 7,
    "dry_run": false
  }'
```

### Create and Use a Template

```bash
# Create template
awscurl --service execute-api --region us-east-1 \
  -X POST "https://$API_GATEWAY_ENDPOINT/api/v1/templates" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "python-job",
    "manifest": {
      "apiVersion": "batch/v1",
      "kind": "Job",
      "metadata": {"name": "{{name}}"},
      "spec": {
        "template": {
          "spec": {
            "containers": [{"name": "main", "image": "{{image}}", "command": {{command}}}],
            "restartPolicy": "Never"
          }
        }
      }
    },
    "parameters": {"image": "python:3.11"}
  }'

# Create job from template
awscurl --service execute-api --region us-east-1 \
  -X POST "https://$API_GATEWAY_ENDPOINT/api/v1/jobs/from-template/python-job" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-python-job",
    "namespace": "gco-jobs",
    "parameters": {"command": "[\"python\", \"-c\", \"print(1+1)\"]"}
  }'
```
