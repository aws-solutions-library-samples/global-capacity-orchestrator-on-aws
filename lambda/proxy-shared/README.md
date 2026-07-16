# Proxy Shared

Shared utility library used by the `api-gateway-proxy` and `regional-api-proxy` Lambda functions. Not deployed as a standalone Lambda.

## Table of Contents

- [Contents](#contents)
- [Provided Utilities](#provided-utilities)
- [Usage](#usage)

## Contents

- `proxy_utils.py` — Bounded signing-key caching, request-header allowlisting,
  HMAC envelope construction, and deadline-aware HTTPS forwarding
- `backend_tls.py` — Synchronized strict private-root TLS transport from
  `lambda/tls-shared/`

## Provided Utilities

### `get_secret_token()`

Retrieves the backend HMAC signing key from Secrets Manager through a
thread-safe cache. Normal entries refresh after five minutes; refresh failures
may use a bounded stale key for at most fifteen minutes, with retry throttling.
The key is used only to sign requests and is never transmitted.

### `sanitize_request_headers(headers)`

Applies a case-insensitive allowlist at the API Gateway trust boundary. Caller
supplied authorization, forwarding, hop-by-hop, and internal signature headers
are not forwarded.

### `build_signed_headers(signing_key, http_method, target_url, body)`

Creates the short-lived HMAC envelope over version, timestamp, random nonce,
method, exact path/query, and SHA-256 body digest.

### `forward_request(target_url, http_method, headers, body, timeout)`

Forwards an HTTPS request within one deadline using the strict private-root
connection pool returned by `backend_tls.get_backend_http_pool()`. Only safe
read-only methods (`GET`, `HEAD`, `OPTIONS`) retry 429/502/503/504 or transport
timeouts with bounded exponential backoff; mutating methods are attempted
exactly once. TLS failures return bounded errors without exposing trust state.

### `build_target_url(endpoint, path, query_params)`

Constructs the target URL from endpoint, path, and query parameters.

## Usage

The `proxy_utils.py` and `backend_tls.py` files are synchronized into each proxy Lambda deployment package by `cli/stacks.py`. The proxy Lambdas import them as local modules; this directory is not deployed independently.
