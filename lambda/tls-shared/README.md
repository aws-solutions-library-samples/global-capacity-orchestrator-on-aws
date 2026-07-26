# Backend TLS Shared Transport

Canonical strict-TLS transport used by GCO [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) callers that connect to [Global Accelerator](https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html) or regional internal Application Load Balancers. This directory is a shared source library, not a standalone Lambda deployment package.

## Table of Contents

- [Contents](#contents)
- [Security Guarantees](#security-guarantees)
- [How It Works](#how-it-works)
- [Trust Refresh and Caching](#trust-refresh-and-caching)
- [Environment Variables](#environment-variables)
- [Consumers and Packaging](#consumers-and-packaging)
- [IAM Permissions](#iam-permissions)
- [Failure Behavior](#failure-behavior)

## Contents

- `backend_tls.py` — Loads the public root bundle from [SSM](https://docs.aws.amazon.com/systems-manager/latest/userguide/what-is-systems-manager.html), builds a private-root-only `ssl.SSLContext`, and returns a reusable `urllib3.PoolManager` with explicit SNI and hostname assertion.

## Security Guarantees

The transport enforces all of the following:

- TLS is mandatory; callers construct only `https://` backend URLs.
- TLS 1.2 is the minimum protocol version.
- Certificate verification is required and hostname checking remains enabled.
- SNI and hostname verification use the stable deployment-local identity from `BACKEND_TLS_SERVER_NAME`, not the dynamic connection hostname.
- The SSL context trusts only certificates in the project SSM trust bundle.
- Trust material containing a private-key PEM marker is rejected.
- Empty or malformed trust bundles fail closed.
- Clients never receive permission to read the root secret or root [KMS](https://docs.aws.amazon.com/kms/latest/developerguide/overview.html) key.
- Native urllib3 retries are disabled so each caller's bounded retry policy remains authoritative.

## How It Works

Backend leaves use a stable private identity such as:

```text
backend.gco.gco.internal
```

The network connection still targets a Global Accelerator DNS name or an internal [ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html) DNS name. `urllib3` receives the private identity through both `server_hostname` and `assert_hostname`, causing the TLS ClientHello to carry the expected SNI name and certificate verification to assert the same name. This allows authenticated TLS without registering a public domain or coupling certificates to dynamic ALB hostnames.

`get_backend_http_pool()` performs these steps:

1. Validate the server identity and SSM trust configuration.
2. Read the public PEM bundle from the configured SSM region.
3. Reject private or malformed material.
4. Create a client-only SSL context with required verification and TLS 1.2 minimum.
5. Create and cache a small urllib3 connection pool with explicit SNI and hostname assertion.

## Trust Refresh and Caching

The pool is refreshed after `BACKEND_TLS_CA_CACHE_TTL_SECONDS`. If SSM is temporarily unavailable, a previously verified pool may be reused only until `BACKEND_TLS_CA_MAX_STALE_SECONDS`; refresh attempts are throttled by `BACKEND_TLS_CA_RETRY_SECONDS`.

This bounded stale window maintains availability during short SSM disruptions without allowing trust to remain stale indefinitely. The certificate manager publishes pending roots before promotion and retains previous public roots during overlap, so routine refresh timing does not create a cutover gap.

`reset_backend_tls_cache()` clears process-local state for deterministic CI checks and cold-start simulation.

## Environment Variables

| Variable | Required | Description |
|---|---:|---|
| `BACKEND_TLS_SERVER_NAME` | Yes | Stable private certificate identity sent as SNI and asserted during verification |
| `BACKEND_TLS_ROOT_CA_PARAMETER` | Yes | Absolute SSM parameter name containing public root certificates only |
| `BACKEND_TLS_ROOT_CA_REGION` | Yes | Region containing the trust parameter |
| `BACKEND_TLS_CA_CACHE_TTL_SECONDS` | No | Normal trust refresh interval; default `300`, bounded to 1–3600 seconds |
| `BACKEND_TLS_CA_MAX_STALE_SECONDS` | No | Maximum use of previously verified trust after refresh failure; default `3600`, bounded to 1–86400 seconds and never below the TTL |
| `BACKEND_TLS_CA_RETRY_SECONDS` | No | Minimum interval between failed refresh attempts; default `5`, bounded to 0.1–60 seconds |

## Consumers and Packaging

`cli/stacks.py::_sync_lambda_sources` copies this canonical module into:

- `lambda/proxy-shared/backend_tls.py`
- `lambda/api-gateway-proxy/backend_tls.py`
- `lambda/regional-api-proxy/backend_tls.py`

Checked-in copies keep direct `cdk synth` deterministic, while deploy-time synchronization prevents stale assets. The two API proxy packages also receive the canonical `proxy_utils.py` implementation from `lambda/proxy-shared/`. The aggregator intentionally does not receive this module because it calls regional [API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/welcome.html) over the AWS-managed TLS chain rather than connecting to an ALB.

## IAM Permissions

Each proxy consumer needs `ssm:GetParameter` on the exact public trust parameter. No consumer requires:

- `secretsmanager:GetSecretValue` on the root secret,
- KMS decrypt permission for the root key,
- ACM private-key access, or
- permission to modify trust state.

Application HMAC-signing roles retain their separate read grant to the HMAC secret.

## Failure Behavior

Missing configuration, invalid server names, unavailable trust beyond the bounded stale window, private-key content, malformed PEM, certificate-chain failure, or hostname mismatch all fail closed. Proxy callers convert trust unavailability to a bounded HTTP 503 and certificate verification failures to a bounded HTTP 502 without exposing certificate or SSM details to clients.
