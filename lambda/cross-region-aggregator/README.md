# Cross-Region Aggregator

Aggregates jobs, health, and status from every required GCO workload region. The centralized [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) is not VPC-attached; it reaches each private regional backend through an always-deployed, IAM-authenticated regional [API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/welcome.html) bridge.

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
2. Describes the deterministic `<project>-regional-api-<region>` [CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) stack in each region.
3. Extracts and strictly validates the `RegionalApiEndpoint` output as that region's AWS `execute-api` HTTPS `/prod` URL under `AWS_URL_SUFFIX`.
4. Uses the Lambda execution-role credentials to SigV4-sign each request for `execute-api` in the target region.
5. Queries regions in parallel with a bounded endpoint-discovery cache. Each regional request is sent once (urllib3's own retries are off) and must finish two seconds before the invocation's remaining time runs out (`context.get_remaining_time_in_millis()`; 27 seconds without a context), so a slow region is reported as an error and the aggregate still answers within the function's 29-second timeout.
6. Merges and sorts successful results while returning bounded per-region errors.

Discovery fails closed if any required bridge is absent or invalid. A previously complete endpoint map may be reused only within the bounded stale-cache window after a transient CloudFormation failure.

## Transport and Trust Boundaries

```text
Global API Gateway → aggregator Lambda
  → regional API Gateway (AWS-managed TLS + SigV4)
  → regional VPC proxy (HMAC signing)
  → internal ALB (deployment-local private-root TLS)
  → pod TLS proxy (re-encrypted HTTPS)
  → Kubernetes application (pod-loopback HTTP)
```

The aggregator trusts the AWS-managed API Gateway certificate chain. It does **not** read the backend HMAC secret, the ALB-hostname [SSM](https://docs.aws.amazon.com/systems-manager/latest/userguide/what-is-systems-manager.html) registry, the private-root public trust bundle, the root private-key secret, or the root [KMS](https://docs.aws.amazon.com/kms/latest/developerguide/overview.html) key. The regional [VPC](https://docs.aws.amazon.com/vpc/latest/userguide/what-is-amazon-vpc.html) proxy owns [ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html) resolution, HMAC signing, and the private-root TLS hop.

## Input

An API Gateway proxy event containing the path, method, query parameters, and optional JSON body.

## Output

An API Gateway proxy response containing merged data, region summaries, and redacted per-region errors. Transport details, API identifiers, credentials, and certificate state are never returned to callers.

## Environment Variables

| Variable | Required | Description |
|---|---:|---|
| `PROJECT_NAME` | No | Prefix used in deterministic regional stack names; defaults to `gco` |
| `TARGET_REGIONS` | Yes | JSON array of required workload regions |
| `AWS_URL_SUFFIX` | Yes | CDK-provided DNS suffix for the active AWS partition; endpoint validation requires `execute-api.<region>.<suffix>` |

## IAM Permissions

The execution role receives only:

- `cloudformation:DescribeStacks` on the exact project-scoped regional API stack ARN in each configured region;
- `execute-api:Invoke` in the deployment account, constrained to exactly `GET /api/v1/jobs`, `GET /api/v1/health`, `GET /api/v1/status`, and `DELETE /api/v1/jobs`; and
- standard Lambda logging and [X-Ray](https://docs.aws.amazon.com/xray/latest/devguide/aws-xray.html) permissions.

## Failure Behavior

- A `limit` that is not an integer from 1 to 1000 (the regional jobs API's own ceiling), or a `DELETE` body that is not a JSON object, is a `400` and no region is queried. The per-region request asks for twice the `limit`, capped at 1000.
- Missing/invalid configuration or incomplete bridge discovery fails the aggregate request closed.
- Regional non-2xx responses become bounded `HTTP <status>` errors.
- Credential, TLS, JSON, and transport failures become `Authenticated regional API request failed` without leaking internal details.
- A regional health endpoint may return authenticated JSON with HTTP 503; that degraded payload remains useful and is included.
- A region the request budget no longer covers is reported as `No time left to query the region` without a request.

## Dependencies

- `boto3==1.43.93`
- `urllib3==2.8.0`

See `requirements.txt` for the deployable package pins.
