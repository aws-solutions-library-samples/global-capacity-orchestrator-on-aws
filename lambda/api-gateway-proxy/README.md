# API Gateway Proxy

Proxies IAM-authenticated requests from the global API Gateway through Global
Accelerator to regional ALBs. Each exact backend request carries a short-lived
HMAC envelope; the reusable Secrets Manager signing key is never transmitted.

## Table of Contents

- [Trigger](#trigger)
- [How It Works](#how-it-works)
- [Environment Variables](#environment-variables)
- [IAM Permissions](#iam-permissions)
- [Dependencies](#dependencies)

## Trigger

API Gateway (proxy integration) — all routes are forwarded through this Lambda.

## How It Works

1. API Gateway validates the caller's IAM credentials (SigV4)
2. This Lambda retrieves the HMAC signing key from Secrets Manager through a
   bounded TTL/stale cache
3. It allowlists supported end-to-end request headers, then signs the version,
   timestamp, random nonce, method, exact path/query, and body digest
4. It obtains a strict private-root connection pool, presenting and verifying
   `backend.<project>.gco.internal` through explicit SNI/hostname assertion
5. It forwards the signed request over HTTPS/443 through Global Accelerator;
   the accelerator is Layer 4 and does not terminate TLS
6. The regional ALB terminates TLS and forwards HTTP to the Kubernetes target
7. Backend middleware validates freshness, integrity, and process-local nonce
   replay before serving the request
8. The Lambda returns the buffered upstream response to the caller

Only safe read-only methods (`GET`, `HEAD`, `OPTIONS`) use bounded exponential
backoff for 429/502/503/504 or transport timeouts. Mutating methods are attempted
once so the proxy cannot duplicate a successful write whose response was lost.

## Input

API Gateway proxy event (httpMethod, path, queryStringParameters, headers, body).

## Output

API Gateway proxy response (statusCode, headers, body).

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GLOBAL_ACCELERATOR_ENDPOINT` | Yes | DNS name of the Global Accelerator |
| `SECRET_ARN` | Yes | ARN of the Secrets Manager secret containing the backend HMAC signing key |
| `PROXY_MAX_RETRIES` | No | Max attempts for safe read-only methods (default: 3) |
| `PROXY_RETRY_BACKOFF_BASE` | No | Base backoff in seconds (default: 0.3) |
| `SECRET_CACHE_TTL_SECONDS` | No | Normal signing-key cache TTL in seconds (default: 300) |
| `SECRET_CACHE_MAX_STALE_SECONDS` | No | Maximum bounded stale-key age during refresh failures (default: 900) |
| `SECRET_CACHE_RETRY_SECONDS` | No | Minimum delay between failed refresh attempts (default: 5) |
| `BACKEND_TLS_SERVER_NAME` | Yes | Stable private certificate identity sent through SNI and asserted during verification |
| `BACKEND_TLS_ROOT_CA_PARAMETER` | Yes | SSM parameter containing public CA roots only |
| `BACKEND_TLS_ROOT_CA_REGION` | Yes | Region containing the public trust parameter |
| `BACKEND_TLS_CA_CACHE_TTL_SECONDS` | No | Normal public-trust refresh interval |
| `BACKEND_TLS_CA_MAX_STALE_SECONDS` | No | Maximum bounded stale-trust interval |

## IAM Permissions

- `secretsmanager:GetSecretValue` on the HMAC secret ARN
- `ssm:GetParameter` on the exact public root trust parameter

The role cannot read the root private-key secret or use its KMS key.

## Dependencies

- `urllib3`, `boto3` (see `requirements.txt`)
- `proxy_utils.py` — shared secret caching, HMAC signing, and HTTPS forwarding utilities
- `backend_tls.py` — synchronized from `lambda/tls-shared/`; strict public-trust loading, TLS 1.2+, SNI, and hostname verification
