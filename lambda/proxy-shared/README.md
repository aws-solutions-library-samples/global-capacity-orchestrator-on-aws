# Proxy Shared

Shared utility library used by the `api-gateway-proxy` and `regional-api-proxy` [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) functions. Not deployed as a standalone Lambda.

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

Retrieves the backend HMAC signing key from [Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html) through a
thread-safe cache. Normal entries refresh after five minutes; refresh failures
may use a bounded stale key for at most fifteen minutes, with retry throttling.
The key is used only to sign requests and is never transmitted.

### `sanitize_request_headers(headers)`

Applies a case-insensitive allowlist at the [API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/welcome.html) trust boundary. Caller
supplied authorization, forwarding, hop-by-hop, and internal signature headers
are not forwarded.

### `caller_identity_headers(event)`

Returns the `x-gco-caller` header carrying the IAM user or STS assumed-role
ARN API Gateway resolved from the request's SigV4 signature
(`requestContext.identity.userArn`), or nothing when the context holds no
well-formed IAM/STS ARN. Only the proxy sets it: a caller-supplied
`x-gco-caller` is dropped by `sanitize_request_headers`, and the cluster API
reads it only from requests whose HMAC envelope verifies. The manifest
processor's `/api/v1/slurm` routes record it in each Slurm job's comment for
accounting attribution.

### `build_signed_headers(signing_key, http_method, target_url, body)`

Creates the short-lived HMAC envelope over version, timestamp, random nonce,
method, exact path/query, and SHA-256 body digest.

### `forward_request(target_url, http_method, headers, body, timeout, resign_headers=None)`

Forwards an HTTPS request within one deadline using the strict private-root
connection pool returned by `backend_tls.get_backend_http_pool()`. Only safe
read-only methods (`GET`, `HEAD`, `OPTIONS`) retry 429/502/503/504 or transport
failures with bounded exponential backoff; mutating methods are attempted
exactly once, except after the ALB's own `503 Service Temporarily Unavailable`
page (`_is_load_balancer_503`: exact body, `Server` header absent or
`awselb/2.0`), which the load balancer serves before selecting a target and
therefore proves the write never reached the backend. Each read-only retry
carries the fresh HMAC envelope `resign_headers()` returns (the proxies pass
`build_signed_headers` bound to the request): the backend accepts a nonce once
and refuses a replayed envelope with 403. The mutating replay after the ALB's
503 is sent byte-identical, since no target saw the first envelope. The
pool runs with urllib3's retries off, so the failures arrive unwrapped: a
connection that cannot be opened (refused, unreachable, unresolvable) or that
is reset, closed or cut short ends in `503 Service unavailable`, and a connect
or read timeout in `504 Gateway timeout`, the same mapping as the Node
streaming proxy. TLS failures return bounded errors without exposing trust
state.

urllib3 tries each address the target resolves to in turn and gives each the
whole connect timeout, so each attempt's per-address connect limit is the
smaller of `PROXY_CONNECT_TIMEOUT_SECONDS` (default 5, bounds 0.5–28) and the
attempt's remaining budget divided by the number of addresses
(`_connect_timeout`). Global Accelerator answers with two addresses, and in
the minutes after a Region's load balancer first joins the accelerator both
can leave the connection unanswered; without the split, one attempt outlasted
the Lambda's timeout and API Gateway answered `Endpoint request timed out`
with nothing logged.

### `has_dot_segment(path)`

Whether the request path has a `.` or `..` segment, literal or
percent-encoded. urllib3 removes such segments from the URL it sends, so
`/api/v1/jobs/../../../inference/x` would reach `/inference/x`, a route other
than the one API Gateway authorized. Both proxies answer such a path with
`400` before forwarding; no GCO route has a dot segment.

### `build_target_url(endpoint, path, query_params)`

Constructs the target URL from endpoint, path, and query parameters.

## Usage

The `proxy_utils.py` and `backend_tls.py` files are synchronized into each proxy Lambda deployment package by `cli/stacks.py`. The proxy Lambdas import them as local modules; this directory is not deployed independently.
