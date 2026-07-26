# Client Examples for the GCO API Gateway

These examples call GCO through an IAM-authenticated [API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/welcome.html) endpoint. They target the global API by default and use the AWS Signature Version 4 ([SigV4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html)) credential chain; no backend signing key is exposed to clients.

## Table of Contents

- [Examples](#examples)
- [Prerequisites](#prerequisites)
- [Discover an endpoint](#discover-an-endpoint)
  - [Global API (default)](#global-api-default)
  - [Regional bridge and direct access](#regional-bridge-and-direct-access)
- [Run the examples](#run-the-examples)
- [Current request shapes](#current-request-shapes)
- [Authentication failures](#authentication-failures)
- [Troubleshooting](#troubleshooting)
- [Security guidance](#security-guidance)
- [References](#references)

## Examples

| Example | Purpose | Credential behavior |
|---|---|---|
| [`python_boto3_example.py`](python_boto3_example.py) | Submit and dry-run one or more manifests with Python | Uses boto3's full provider chain, including profiles, SSO, web identity, and [IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction.html) roles |
| [`aws_cli_examples.sh`](aws_cli_examples.sh) | Call the API with curl's built-in SigV4 support | Resolves active credentials with AWS CLI v2 and preserves the session token |
| [`curl_sigv4_proxy_example.sh`](curl_sigv4_proxy_example.sh) | Use ordinary curl requests through `aws-sigv4-proxy` | The proxy uses the normal AWS credential chain |

The shell examples read `project_name` and the API Gateway region from the repository's `cdk.json`. Override discovery with `PROJECT_NAME`, `API_REGION`, or `STACK_NAME` when needed.

## Prerequisites

1. Configure a valid AWS identity and verify it:

   ```bash
   aws sts get-caller-identity
   ```

2. Ensure that identity is in the deployment account and can invoke the API (`execute-api:Invoke`). The API resource policy is account-scoped, and each API method uses IAM authorization.
3. Install the dependencies for the example you plan to run:

   ```bash
   # Python example
   python3 -m pip install boto3 requests aws-requests-auth

   # Shell examples on macOS
   brew install awscli jq
   # curl must support --aws-sigv4; the proxy example also needs aws-sigv4-proxy.
   ```

Temporary credentials are supported. Do not copy access keys into these scripts or omit `X-Amz-Security-Token` when signing manually.

## Discover an endpoint

### Global API (default)

For the stock `project_name: gco`, the stack is `gco-api-gateway` and its output is `ApiEndpoint`:

```bash
API_REGION=us-east-2 # use context.deployment_regions.api_gateway from cdk.json
aws cloudformation describe-stacks \
  --stack-name gco-api-gateway \
  --region "$API_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiEndpoint`].OutputValue' \
  --output text
```

In the commercial `aws` partition, the global workload path is:

```text
Client → edge-optimized API Gateway (AWS-managed TLS + SigV4)
  → proxy Lambda (request-bound HMAC)
  → Global Accelerator (TCP/443 pass-through)
  → internal regional ALB (private-root TLS) → EKS service (HTTP)
```

[Global Accelerator](https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html) chooses a healthy registered backend. The global proxy
rejects `X-GCO-Target-Region`; callers that require an exact Region must use an
authorized regional API endpoint. Outside `aws`, the global API is regional and
aggregate-only, so workload control and inference always use a regional bridge.

### Regional bridge and direct access

Every workload region has a stack named `<project>-regional-api-<region>` with a
`RegionalApiEndpoint` output because cross-region aggregation depends on it. In
`aws`, `api_gateway.regional_api_enabled=true` additionally permits
IAM-authorized same-account callers to invoke that endpoint directly. Other
partitions enable that policy automatically because this endpoint is the
supported workload ingress without Global Accelerator:

```bash
aws cloudformation describe-stacks \
  --stack-name gco-regional-api-us-east-1 \
  --region us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`RegionalApiEndpoint`].OutputValue' \
  --output text
```

The direct path is:

```text
Client → regional API Gateway (AWS-managed TLS + SigV4)
  → VPC Lambda (request-bound HMAC)
  → internal regional ALB (private-root TLS) → EKS service (HTTP)
```

Sign requests in the region that owns the selected API Gateway endpoint. The
bridge always exists for aggregator fan-out. In `aws`, direct invocation
requires the resource-policy opt-in; outside `aws`, the deployment enables that
same-account policy automatically. The aggregator itself uses AWS-managed TLS
and SigV4 to the bridge; it does not read [ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html) [SSM](https://docs.aws.amazon.com/systems-manager/latest/userguide/what-is-systems-manager.html) state, the HMAC secret, or
private-root trust.

## Run the examples

From the repository root:

```bash
python3 docs/client-examples/python_boto3_example.py
bash docs/client-examples/aws_cli_examples.sh
bash docs/client-examples/curl_sigv4_proxy_example.sh
```

The proxy example starts a local process, preserves the API Gateway stage path (normally `/prod`), and stops the process on exit. Set `PROXY_PORT` if port 8080 is unavailable.

## Current request shapes

### Submit manifests

`POST /api/v1/manifests` requires a `manifests` array. A singular `manifest` field is not accepted.

```json
{
  "manifests": [
    {
      "apiVersion": "batch/v1",
      "kind": "Job",
      "metadata": {
        "name": "my-job",
        "namespace": "gco-jobs"
      },
      "spec": {
        "template": {
          "spec": {
            "containers": [
              {
                "name": "worker",
                "image": "busybox:1.38.0",
                "command": ["echo", "hello"]
              }
            ],
            "restartPolicy": "Never"
          }
        }
      }
    }
  ],
  "dry_run": false,
  "validate": true
}
```

A successful response contains `success`, `cluster_id`, `region`, `summary`, and one entry per submitted object in `resources`.

### Validate without applying

Either send the same payload to `POST /api/v1/manifests/validate`, or set `"dry_run": true` on `POST /api/v1/manifests`.

### Inspect or delete a generic resource

```text
GET    /api/v1/manifests/{namespace}/{name}?api_version=batch%2Fv1&kind=Job
DELETE /api/v1/manifests/{namespace}/{name}?api_version=batch%2Fv1&kind=Job
```

The generic resource routes default to `apps/v1` and `Deployment`, so callers must provide `api_version` and `kind` for a Job. There is no `GET /api/v1/manifests` collection route.

### Work with Jobs

```text
GET    /api/v1/jobs?namespace=gco-jobs&limit=20
GET    /api/v1/jobs/gco-jobs/my-job
GET    /api/v1/jobs/gco-jobs/my-job/logs?tail=100
DELETE /api/v1/jobs/gco-jobs/my-job
```

Use the `/api/v1/jobs` routes for Job-specific status, logs, events, pods, metrics, retry, and deletion.

## Authentication failures

- **403 Missing Authentication Token**: the request is unsigned, the stage/path is wrong, or the method is not deployed. Use the full output URL, including `/prod`.
- **403 Invalid signature**: sign for service `execute-api` in the endpoint's region, preserve the request path/query exactly, include the session token for temporary credentials, and verify the system clock.
- **403 Not authorized**: verify `execute-api:Invoke`, the active account, and the API resource ARN. The deployment resource policy rejects principals from other accounts even if they otherwise have an allow policy.

Useful checks:

```bash
aws sts get-caller-identity
aws cloudformation describe-stacks --stack-name gco-api-gateway --region "$API_REGION"
```

## Troubleshooting

### `ApiEndpoint` or `RegionalApiEndpoint` is missing

Confirm the exact stack and list all outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name gco-api-gateway \
  --region "$API_REGION" \
  --query 'Stacks[0].Outputs'
```

The global output is `ApiEndpoint`; the optional regional output is `RegionalApiEndpoint`.

### Proxy requests time out or return 502/503

1. Inspect API Gateway access logs and the proxy [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) log group listed in the stack resources; generated Lambda log-group names are not fixed.
2. For the global path, check Global Accelerator endpoint health.
3. For either path, verify `/<project>/alb-hostname-<region>` in the global-region SSM registry.
4. Confirm the registered load balancer is an **internal application ALB** in the expected account/region and carries the GCO [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) cluster and `gco.aws/gateway` ownership tags.
5. Check the manifest-processor service, pods, and `/api/v1/health` response in the target cluster.

There is no [VPC](https://docs.aws.amazon.com/vpc/latest/userguide/what-is-amazon-vpc.html) Link or internal [NLB](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/introduction.html) in either API path.

### Python cannot import `aws_requests_auth`

```bash
python3 -m pip install aws-requests-auth
```

## Security guidance

- Prefer short-lived credentials from SSO, [STS](https://docs.aws.amazon.com/STS/latest/APIReference/), workload identity, or IAM roles.
- Never send the backend HMAC signing key; trusted proxy Lambdas retrieve it and sign each exact backend request with a timestamp and nonce.
- Retry safe reads with bounded exponential backoff. Do not automatically replay mutating requests unless the operation is explicitly idempotent.
- Keep request bodies below the configured API limit (1 MiB by default).

## References

- [AWS Signature Version 4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html)
- [Control access to API Gateway with IAM](https://docs.aws.amazon.com/apigateway/latest/developerguide/permissions.html)
- [aws-sigv4-proxy](https://github.com/awslabs/aws-sigv4-proxy)
- [IAM policy examples](../iam-policies/README.md)
