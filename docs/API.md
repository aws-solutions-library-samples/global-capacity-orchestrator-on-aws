# GCO Manifest Processor API

This document describes the REST API for the GCO Manifest Processor service.

## Table of Contents

- [Base URL](#base-url)
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
- [Error Responses](#error-responses)
- [Examples](#examples)

---

## Base URL

The API is available at the API Gateway endpoint configured during deployment,
where `<API_GATEWAY_ENDPOINT>` is the host from the `ApiGatewayUrl`
CloudFormation output:

```http
https://<API_GATEWAY_ENDPOINT>/api/v1
```

## Authentication

All API requests are authenticated using AWS IAM Signature Version 4 (SigV4)
at the API Gateway level. API Gateway validates the caller's AWS credentials;
the proxy then allowlists supported headers and signs the exact backend request
with a short-lived HMAC envelope. The envelope binds a timestamp, nonce, method,
path/query, and body digest, so the reusable Secrets Manager signing key is never
sent to the cluster. Backend middleware rejects stale, tampered, or process-local
replayed envelopes.

**Important:** Clients provide only SigV4 authentication. Do not set any
`X-GCO-Signature-*`, `X-GCO-Timestamp`, `X-GCO-Nonce`, or
`X-GCO-Content-SHA256` headers; the proxy strips caller-supplied internal headers
and creates its own envelope after IAM authentication. The global endpoint also
rejects `X-GCO-Target-Region`; select an authorized regional API endpoint for
explicit region pinning. The GCO CLI does this automatically whenever an API
operation carries an exact transport target region.

## Transport Security

GCO has two explicit TLS trust domains:

1. Clients and the centralized aggregator use AWS-managed TLS to API Gateway.
   Aggregator fan-out additionally uses SigV4 with its execution-role
   credentials to each deterministic regional API bridge.
2. Trusted proxy Lambdas use the deployment-local private root for the backend
   ALB hop. The normal global path is proxy → Global Accelerator → ALB, while a
   regional bridge uses VPC proxy → ALB. Global Accelerator forwards TCP/443 at
   Layer 4 and does not terminate TLS.

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

request = AWSRequest(method='GET', url='https://<API_GATEWAY_ENDPOINT>/api/v1/jobs')
SigV4Auth(credentials, 'execute-api', 'us-east-1').add_auth(request)

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

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/healthz` | Kubernetes liveness probe |
| GET | `/readyz` | Kubernetes readiness probe |
| GET | `/api/v1/health` | Detailed health check |
| GET | `/api/v1/status` | Service status and configuration |

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
  "labels": {"team": "ml"}
}
```

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
    expected = hmac.new(
        secret.encode('utf-8'),
        payload,
        hashlib.sha256
    ).hexdigest()
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
