# Cross-Region Aggregator

Aggregates jobs, health, and status from every required GCO workload region. The centralized Lambda is not VPC-attached; it reaches each private regional backend through an always-deployed, IAM-authenticated regional API Gateway bridge.

## Table of Contents

- [Trigger](#trigger)
- [Routes](#routes)
- [How It Works](#how-it-works)
- [Transport and Trust Boundaries](#transport-and-trust-boundaries)
- [Input](#input)
- [Output](#output)
- [Environment Variables](#environment-variables)
- [IAM Permissions](#iam-permissions)
- [Failure Behavior](#failure-behavior)
- [Dependencies](#dependencies)

## Trigger

The global API Gateway invokes this Lambda for `/api/v1/global/*` routes.

## Routes

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/global/jobs` | List jobs across all regions |
| `GET` | `/api/v1/global/health` | Health status across all regions |
| `GET` | `/api/v1/global/status` | Cluster status across all regions |
| `DELETE` | `/api/v1/global/jobs` | Bulk-delete jobs across all regions |

## How It Works

1. Reads the required workload regions from `TARGET_REGIONS`.
2. Describes the deterministic `<project>-regional-api-<region>` CloudFormation stack in each region.
3. Extracts and strictly validates the `RegionalApiEndpoint` output as that region's AWS `execute-api` HTTPS `/prod` URL.
4. Uses the Lambda execution-role credentials to SigV4-sign each request for `execute-api` in the target region.
5. Queries regions in parallel with a bounded endpoint-discovery cache.
6. Merges and sorts successful results while returning bounded per-region errors.

Discovery fails closed if any required bridge is absent or invalid. A previously complete endpoint map may be reused only within the bounded stale-cache window after a transient CloudFormation failure.

## Transport and Trust Boundaries

```text
Global API Gateway → aggregator Lambda
  → regional API Gateway (AWS-managed TLS + SigV4)
  → regional VPC proxy (HMAC signing)
  → internal ALB (deployment-local private-root TLS)
  → Kubernetes pod (HTTP after ALB termination)
```

The aggregator trusts the AWS-managed API Gateway certificate chain. It does **not** read the backend HMAC secret, the ALB-hostname SSM registry, the private-root public trust bundle, the root private-key secret, or the root KMS key. The regional VPC proxy owns ALB resolution, HMAC signing, and the private-root TLS hop.

## Input

An API Gateway proxy event containing the path, method, query parameters, and optional JSON body.

## Output

An API Gateway proxy response containing merged data, region summaries, and redacted per-region errors. Transport details, API identifiers, credentials, and certificate state are never returned to callers.

## Environment Variables

| Variable | Required | Description |
|---|---:|---|
| `PROJECT_NAME` | No | Prefix used in deterministic regional stack names; defaults to `gco` |
| `TARGET_REGIONS` | Yes | JSON array of required workload regions |

## IAM Permissions

The execution role receives only:

- `cloudformation:DescribeStacks` on the exact project-scoped regional API stack ARN in each configured region;
- `execute-api:Invoke` in the deployment account, constrained to the `/api/v1/*` route tree; and
- standard Lambda logging and X-Ray permissions.

## Failure Behavior

- Missing/invalid configuration or incomplete bridge discovery fails the aggregate request closed.
- Regional non-2xx responses become bounded `HTTP <status>` errors.
- Credential, TLS, JSON, and transport failures become `Authenticated regional API request failed` without leaking internal details.
- A regional health endpoint may return authenticated JSON with HTTP 503; that degraded payload remains useful and is included.

## Dependencies

- `boto3==1.43.38`
- `urllib3==2.7.0`

See `requirements.txt` for the deployable package pins.
