# Inference Streaming Proxy

Node.js 24 Lambda response-streaming proxy for the authenticated `/inference/...` surface. Set `ROUTING_MODE=global` to use Global Accelerator or `ROUTING_MODE=regional` to discover and verify the regional internal ALB. The deployable package owns a separate exact-pinned npm graph containing only the three AWS SDK clients it imports.

## Environment

| Variable | Mode | Description |
|---|---|---|
| `ROUTING_MODE` | Both | Required: `global` or `regional`. |
| `SECRET_ARN` | Both | Secrets Manager ARN whose JSON `token` signs backend requests. |
| `BACKEND_TLS_SERVER_NAME` | Both | Private certificate DNS identity used for both SNI and hostname verification. |
| `BACKEND_TLS_ROOT_CA_PARAMETER` | Both | SSM parameter containing only the public private-root CA bundle. |
| `BACKEND_TLS_ROOT_CA_REGION` | Both | Region containing the trust parameter. |
| `GLOBAL_ACCELERATOR_ENDPOINT` | Global | Global Accelerator DNS endpoint; HTTPS/443 only. |
| `REGISTRY_REGION` | Regional | Region containing `/<project>/alb-hostname-<target-region>`. |
| `TARGET_REGION` | Regional | Workload region whose ALB is resolved and verified. |
| `PROJECT_NAME` | Regional | Prefix used by the registry path and expected EKS cluster tag. |
| `AWS_ACCOUNT_ID` | Regional | Account that must own the resolved internal application ALB. |
| `AWS_URL_SUFFIX` | Regional | CDK-provided DNS suffix for the active AWS partition; regional ALB validation requires the exact ELB suffix. |
| `MAX_REQUEST_BODY_BYTES` | Both | UTF-8 request-body cap enforced before authentication, discovery, or TLS work; bounded to 1–10 MiB (default `1048576`). |
| `REGIONAL_ENDPOINT_CACHE_TTL_SECONDS` | Regional | Verified route TTL, bounded to 0–300 seconds (default `60`; `0` disables reads from cache). |
| `PROXY_MAX_RETRIES` | Both | Attempts for `GET`/`HEAD`, bounded to 1–5 (default `3`). |
| `PROXY_RETRY_BACKOFF_BASE` | Both | Exponential-backoff base seconds, bounded to 0–5 (default `0.3`). |
| `SECRET_CACHE_TTL_SECONDS` / `SECRET_CACHE_MAX_STALE_SECONDS` / `SECRET_CACHE_RETRY_SECONDS` | Both | Signing-key cache controls (defaults `300` / `900` / `5`). |
| `BACKEND_TLS_CA_CACHE_TTL_SECONDS` / `BACKEND_TLS_CA_MAX_STALE_SECONDS` / `BACKEND_TLS_CA_RETRY_SECONDS` | Both | Public-trust cache controls (defaults `300` / `3600` / `5`). |

The execution role needs `secretsmanager:GetSecretValue` and trust-parameter `ssm:GetParameter`. Regional mode also needs registry-parameter `ssm:GetParameter`, `elasticloadbalancing:DescribeLoadBalancers`, and `elasticloadbalancing:DescribeTags`.

## Dependencies and CI

`package.json` and `package-lock.json` are the deployment graph and pin Node 24, npm 11.18.0, and each direct AWS SDK client exactly. The root tooling graph is intentionally separate so CDK, diagram, and markdown tooling cannot enter the Lambda bundle. Install this graph with lifecycle scripts disabled:

```bash
npm ci --prefix lambda/inference-streaming-proxy --ignore-scripts --no-audit --no-fund
```

The native `node:test` suite lives in `tests/inference-streaming-proxy/`; the
package's `npm test` script invokes it while keeping dependency resolution
anchored to this production graph. The dedicated `Inference Streaming Proxy`
workflow runs it on Node 24 and enforces at least 93% lines, functions, and
branches. `security:npm-audit:all-packages`, JavaScript CodeQL, Semgrep, Trivy,
Dependabot, and the monthly dependency-consistency scan cover this graph. The
shared Lambda packaging action stages production dependencies with
`npm ci --omit=dev --ignore-scripts`; it never resolves packages on demand.

## Behavior

The handler accepts only `GET`, `HEAD`, and `POST` under `/inference/...`, rejects base64 request bodies, and applies `MAX_REQUEST_BODY_BYTES` to the UTF-8 byte length for every supported method before any AWS, routing, or TLS dependency runs. It forwards only the existing request-header allowlist, strips hop-by-hop response headers, and drops upstream `Set-Cookie` rather than corrupting repeated cookie values in Lambda's single-value streaming metadata. It signs the exact outbound method, encoded path/query (including duplicate query values), UTF-8 body digest, timestamp, and nonce with the Python-compatible `v1` HMAC envelope.

Both modes require HTTPS/443 with a public root bundle loaded from SSM, TLS 1.2 or newer, and explicit `BACKEND_TLS_SERVER_NAME` SNI/hostname verification. Regional mode accepts only the SSM-registered ELB DNS name after ELB confirms the exact account/region, internal application type, GCO cluster tag, and `gco.aws/gateway` ownership tag. Secret and trust refresh failures may use only bounded stale cache entries; failed or expired route verification is never accepted.

Retries apply only to `GET` and `HEAD` for 429/502/503/504 or retryable transport failures. `POST` is attempted exactly once and is never replayed. Locally generated failures are bounded JSON messages and do not expose secret, certificate, registry, or backend details.

The selected upstream status and end-to-end headers are attached with `awslambda.HttpResponseStream.from`, then the upstream body is piped with backpressure. The Lambda runtime owns the streaming metadata prelude and transfer framing; the handler does not construct framing bytes. A downstream disconnect aborts and destroys the upstream request/response.

Both edge/global and regional modes can stream for the Lambda invocation's full remaining budget (up to the 15-minute Lambda/API Gateway integration limit, with one second reserved for response handling). Edge/global mode retains a 30-second **idle** timeout, while regional mode uses a 5-minute idle timeout; each timeout resets while response bytes continue to arrive.
