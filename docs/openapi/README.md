# Generated OpenAPI Documents

One OpenAPI 3.1 document per GCO HTTP service, generated from the running
FastAPI applications rather than written by hand.

| Document | Service | Surface |
|----------|---------|---------|
| [`manifest-processor.json`](manifest-processor.json) | `manifest-processor` | The control plane: manifests, jobs, the global job queue, templates, webhooks, cost reporting |
| [`inference-proxy.json`](inference-proxy.json) | `inference-proxy` | `/inference/*` proxying to deployed model endpoints |
| [`health-monitor.json`](health-monitor.json) | `health-monitor` | Cluster health, metrics, and status |
| [`cost-monitor.json`](cost-monitor.json) | `cost-monitor` | Cluster-internal `/internal/*` cost reporting |

These are committed so the API surface is reviewable in a diff: a pull request
that changes a route, a parameter, or a request model shows the schema change
alongside the code.

## Regenerating

```bash
python scripts/generate_openapi.py           # rewrite the documents
python scripts/generate_openapi.py --check   # fail if any document is stale
```

`--check` runs as part of the test suite via
[`tests/test_api_docs_coverage.py`](../../tests/test_api_docs_coverage.py), which
also asserts that every live route is described in [`../API.md`](../API.md). A
route added without regenerating these documents fails there.

## Scope

FastAPI's own `/docs`, `/redoc`, and `/openapi.json` routes are excluded — they
are interactive documentation, not part of the API contract, and no API Gateway
forwards them. The Prometheus `/metrics` route is also absent because it is
mounted as a plain Starlette route and carries no OpenAPI schema; it is
documented in [`../API.md`](../API.md) instead.

Not every documented endpoint appears here. The cross-region aggregation routes
(`/api/v1/global/*`) are served by a Lambda at the global API Gateway, and the
Mooncake prefill/decode proxy runs from a ConfigMap-hosted application — neither
is a FastAPI app in this repository. Both are documented in
[`../API.md`](../API.md).

## Using them

Point any OpenAPI-aware tool at a document to generate a client. The container
image needs nothing on your host beyond the runtime GCO already requires
(Docker, [Finch](https://runfinch.com/), or [Podman](https://podman.io/docs)):

```bash
mkdir -p ~/gco-client

docker run --rm \
  -v "$PWD/docs/openapi:/spec:ro" \
  -v "$HOME/gco-client:/out" \
  openapitools/openapi-generator-cli:v7.24.0 \
  generate -i /spec/manifest-processor.json -g python -o /out
```

That produces one API class per tag group — jobs, job queue, manifests,
templates, webhooks, cost, health — plus a model per request schema.

Two things to know before changing the command:

- **The output path must be inside a directory your runtime shares with the
  container.** Finch and Podman run a VM, so a bind mount of `/tmp` silently
  writes inside the VM and leaves the host directory empty. Paths under `$HOME`
  are shared by default, which is why the example writes there.
- **`npx @openapitools/openapi-generator-cli` needs a Java runtime.** The npm
  package is a wrapper that downloads a JAR and shells out to `java`, so without
  a JRE on your `PATH` it fails with "Unable to locate a Java Runtime". Use the
  container above, or install a JRE first.

Generated clients handle request shapes only. Every request still needs AWS IAM
SigV4 signing against the API Gateway host; see
[Authentication](../API.md#authentication) and the
[client examples](../client-examples/README.md).
