# Regional API Proxy

Proxies IAM-authenticated requests from each always-deployed regional API bridge to the regional internal [ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html) through a [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) function in the workload VPC. The centralized aggregator always uses this path. In the commercial `aws` partition, same-account callers may opt in with `api_gateway.regional_api_enabled=true`; in other AWS partitions this is the required supported workload ingress and same-account direct access is enabled automatically because [Global Accelerator](https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html) is omitted.

## Table of Contents

- [Trigger](#trigger)
- [How It Works](#how-it-works)
- [Backend Discovery and Verification](#backend-discovery-and-verification)
- [Environment Variables](#environment-variables)
- [Failure Behavior](#failure-behavior)
- [IAM Permissions](#iam-permissions)
- [Dependencies](#dependencies)

## Trigger

Regional [API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/welcome.html)'s buffered `/api/v1/{proxy+}` integration. The bridge also exposes `/inference/{proxy+}` through the separate Node.js response-streaming proxy. Every method requires [IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction.html) authorization ([SigV4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html)).

## How It Works

1. Regional API Gateway validates the caller's IAM credentials.
2. The Lambda retrieves the HMAC signing key from [Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html) through a bounded TTL/stale cache.
3. It resolves and verifies the current regional platform ALB.
4. It allowlists supported end-to-end request headers, then signs the version, timestamp, random nonce, method, exact path/query, and body digest.
5. It reads the public root bundle, sends/asserts
   `backend.<project>.gco.internal`, and forwards to the internal ALB over
   private-root HTTPS/443.
6. The ALB terminates TLS and forwards HTTP to the Kubernetes target.
7. Backend middleware validates freshness, integrity, and process-local nonce replay before serving the request.
8. The Lambda returns the buffered upstream response to the caller.

Only safe read-only methods (`GET`, `HEAD`, and `OPTIONS`) use bounded exponential backoff for 429/502/503/504 responses or transport timeouts. Mutating methods are attempted once so the proxy cannot duplicate a successful write whose response was lost.

## Backend Discovery and Verification

Production wiring omits `ALB_ENDPOINT`. At request time the Lambda reads `/<project>/alb-hostname-<target-region>` from [SSM](https://docs.aws.amazon.com/systems-manager/latest/userguide/what-is-systems-manager.html) in `REGISTRY_REGION`, validates that the value is an ELB DNS name under the CDK-provided `AWS_URL_SUFFIX`, and verifies with Elastic Load Balancing APIs that it is:

- an internal application load balancer;
- owned by `AWS_ACCOUNT_ID` in `TARGET_REGION`;
- tagged for the exact `<project>-<region>` [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) cluster; and
- tagged with the exact `gco.aws/gateway: gco-system/gco-gateway` ownership marker.

Verified endpoints are cached for 60 seconds by default. `REGIONAL_ENDPOINT_CACHE_TTL_SECONDS=0` disables the cache; accepted values are 0–300 seconds. Resolution or ownership failures are never cached.

`ALB_ENDPOINT` is an optional compatibility override for isolated stack synthesis or controlled tests. When supplied, the Lambda validates its ELB DNS shape but intentionally skips SSM lookup and ELB ownership/tag verification. Production deployments should use the registry path.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SECRET_ARN` | Yes | Secrets Manager ARN containing the backend HMAC signing key |
| `REGISTRY_REGION` | Registry mode | AWS Region containing the project ALB-hostname SSM registry |
| `TARGET_REGION` | Registry mode | Workload Region served by this regional API |
| `PROJECT_NAME` | Registry mode | Deployment prefix used in the SSM path, EKS cluster name, and ownership checks |
| `AWS_ACCOUNT_ID` | Registry mode | AWS account that must own the resolved ALB |
| `AWS_URL_SUFFIX` | Registry mode | CDK-provided DNS suffix for the active AWS partition; the ALB hostname must end in the exact regional ELB suffix |
| `ALB_ENDPOINT` | No | Literal ELB DNS override for compatibility/isolated use; bypasses registry ownership checks |
| `REGIONAL_ENDPOINT_CACHE_TTL_SECONDS` | No | Verified endpoint cache TTL, 0–300 seconds (default: 60; `0` disables caching) |
| `PROXY_MAX_RETRIES` | No | Maximum attempts for safe read-only methods (default: 3) |
| `PROXY_RETRY_BACKOFF_BASE` | No | Base retry backoff in seconds (default: 0.3) |
| `SECRET_CACHE_TTL_SECONDS` | No | Normal signing-key cache TTL in seconds (default: 300) |
| `SECRET_CACHE_MAX_STALE_SECONDS` | No | Maximum bounded stale-key age during refresh failures (default: 900) |
| `SECRET_CACHE_RETRY_SECONDS` | No | Minimum delay between failed secret-refresh attempts (default: 5) |
| `BACKEND_TLS_SERVER_NAME` | Yes | Stable private certificate identity sent through SNI and asserted during verification |
| `BACKEND_TLS_ROOT_CA_PARAMETER` | Yes | SSM parameter containing public CA roots only |
| `BACKEND_TLS_ROOT_CA_REGION` | Yes | Region containing the public trust parameter |
| `BACKEND_TLS_CA_CACHE_TTL_SECONDS` | No | Normal public-trust refresh interval |
| `BACKEND_TLS_CA_MAX_STALE_SECONDS` | No | Maximum bounded stale-trust interval |

“Registry mode” variables are required when `ALB_ENDPOINT` is absent, which is the normal application wiring.

## Failure Behavior

- Signing-key retrieval failure returns `503 Backend authentication is temporarily unavailable`.
- Registry lookup, malformed endpoint, ELB API, or ownership verification failure returns `502 Regional backend is temporarily unavailable`.
- Base64-encoded request bodies return `415`; the proxy does not reinterpret binary payloads.
- Upstream status codes and bounded transport failures are returned by the shared forwarding utility.

## IAM Permissions

The regional proxy role needs:

- `secretsmanager:GetSecretValue` and `secretsmanager:DescribeSecret` on the signing secret;
- `ssm:GetParameter` on the exact `/<project>/alb-hostname-<region>` endpoint parameter and public backend-root trust parameter;
- `elasticloadbalancing:DescribeLoadBalancers` and `elasticloadbalancing:DescribeTags` for ownership verification (these Describe APIs do not support resource-level scoping); and
- Lambda [VPC](https://docs.aws.amazon.com/vpc/latest/userguide/what-is-amazon-vpc.html) ENI permissions through `AWSLambdaVPCAccessExecutionRole` so it can reach the internal ALB.

## Dependencies

- `proxy_utils.py` — shared secret caching, request signing, header sanitization, URL construction, retry, and HTTPS forwarding utilities copied from `lambda/proxy-shared/`
- `backend_tls.py` — strict public-trust loading, TLS 1.2+, SNI, and hostname verification copied from `lambda/tls-shared/`
