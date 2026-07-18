# 0003. Dedicated inference response-streaming path

- **Status:** Accepted
- **Date:** 2026-07-17
- **Deciders:** GCO maintainers
- **Supersedes:** none
- **Superseded by:** none

## Context

GCO's existing `/api/v1` data plane uses Python Lambda proxy handlers and
buffered HTTP clients. That contract is appropriate for control-plane requests,
but buffering an inference response until completion prevents clients from
observing tokens as a model produces them and makes long generations appear
idle.

API Gateway REST APIs can stream an AWS proxy integration response, but the
Lambda integration must opt into response-transfer mode and the handler must
write the Lambda streaming wire format. AWS Lambda's managed response-streaming
API is available directly to Node.js handlers. API Gateway still buffers the
request body, so this capability solves response streaming only.

The new path must retain GCO's existing boundaries: IAM authorization at API
Gateway, exact request-target and body binding in the HMAC envelope, backend TLS,
the Global Accelerator source-IP restriction on the shared ALB, project/Region
resource ownership, and dynamic inference-endpoint routing. It must not weaken
or destabilize the established buffered control plane.

## Decision

We will add a dedicated inference-only response-streaming path while leaving
`/api/v1` on the existing buffered Python implementation.

1. The global and regional REST APIs expose inference methods whose AWS proxy
   integrations set response transfer mode to `STREAM`.
2. Dedicated Node.js 24 Lambda functions handle only those inference methods.
   They emit the Lambda response-streaming prelude and delimiter, forward body
   chunks incrementally, preserve REST API `multiValueHeaders` as repeated
   outbound values where the request allowlist permits them, stop upstream work
   when the downstream client disconnects, and keep the existing HMAC, TLS,
   timeout, retry, and endpoint-cache controls.
3. The regional Lambda sends traffic to a separate in-cluster
   `inference-proxy` Deployment and Service. That FastAPI application resolves
   only valid inference endpoint names and streams the selected endpoint's
   response; it does not expose the broader manifest/control-plane routes.
4. The CLI offers incremental response consumption as an explicit inference
   behavior. Existing callers and all `/api/v1` operations retain their
   buffered response contract.
5. The production Lambda owns an isolated npm graph with exact direct pins and
   a committed lockfile. Root CDK/diagram/documentation tooling remains in the
   separate root graph and is never bundled into the Lambda asset.

Request streaming is out of scope because API Gateway does not provide it for
this integration. This decision also does not move the Python control plane to
Node.js.

## Consequences

### Positive

- Inference clients receive tokens or other chunks as soon as the model emits
  them instead of waiting for the complete response.
- Streaming failures, disconnects, and idle behavior are isolated from the
  mature buffered control plane.
- A dedicated in-cluster service and IAM role keep the inference route's
  permissions and reachable paths narrow.
- The deployable JavaScript dependency graph can be audited, scanned, and
  updated independently from repository development tooling.

### Negative

- GCO now maintains two Lambda languages and two response contracts.
- The streaming Lambda must implement API Gateway's response-streaming framing
  and careful backpressure/disconnect handling.
- The additional Lambda, IAM role, Kubernetes Deployment, and Service increase
  deployment and operational surface area.

### Neutral

- API Gateway request bodies remain buffered.
- Regional streams are bounded by API Gateway and ALB idle/runtime limits, so
  services must emit data or heartbeats within those limits.
- Buffered inference remains available for compatibility and comparison.

## Alternatives considered

### Convert the existing Python proxy to streaming

- **Summary:** replace the buffered `/api/v1` Lambda and route all requests
  through one streaming implementation.
- **Why not:** it would couple control-plane reliability to inference framing,
  require a broader migration, and expand the blast radius without improving
  non-inference operations.

### Buffer inference responses in the existing path

- **Summary:** keep one Python path and return only after the upstream model
  finishes.
- **Why not:** clients cannot distinguish active generation from a stalled
  request, receive no incremental tokens, and pay the full latency before the
  first byte.

### Put root development tooling in the Lambda package

- **Summary:** use one npm manifest for CDK, diagrams, linting, and runtime AWS
  SDK clients.
- **Why not:** it would enlarge the production asset and dependency attack
  surface, while allowing unrelated tooling updates to perturb runtime code.

## References

- PR #161
- [`../INFERENCE.md`](../INFERENCE.md)
- [`../../lambda/inference-streaming-proxy/README.md`](../../lambda/inference-streaming-proxy/README.md)
- [`../../gco/services/api_routes/inference_proxy.py`](../../gco/services/api_routes/inference_proxy.py)
- [`../../gco/stacks/api_gateway_global_stack.py`](../../gco/stacks/api_gateway_global_stack.py)
- [`../../gco/stacks/regional_api_gateway_stack.py`](../../gco/stacks/regional_api_gateway_stack.py)
