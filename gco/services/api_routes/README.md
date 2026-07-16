# API Routes

FastAPI route modules for the GCO manifest API. Each module declares its own complete path or router prefix and is mounted by `manifest_api.py` without an additional prefix.

## Table of Contents

- [Files](#files)
- [Route Prefixes](#route-prefixes)
- [Adding a New Route Module](#adding-a-new-route-module)

## Files

| File | Endpoints | Description |
|------|-----------|-------------|
| `manifests.py` | `POST /api/v1/manifests`, `POST /api/v1/manifests/validate`, `GET /api/v1/manifests/{namespace}/{name}`, `DELETE /api/v1/manifests/{namespace}/{name}` | Manifest submission, validation, status, and deletion |
| `jobs.py` | `GET /api/v1/jobs`, `DELETE /api/v1/jobs`, `GET /api/v1/jobs/{namespace}/{name}`, `DELETE /api/v1/jobs/{namespace}/{name}`, `GET /api/v1/jobs/{namespace}/{name}/logs`, `GET /api/v1/jobs/{namespace}/{name}/events`, `GET /api/v1/jobs/{namespace}/{name}/pods`, `GET /api/v1/jobs/{namespace}/{name}/pods/{pod_name}/logs`, `GET /api/v1/jobs/{namespace}/{name}/metrics`, `POST /api/v1/jobs/{namespace}/{name}/retry` | Job listing, status, logs, events, pods, metrics, deletion, and retry |
| `templates.py` | `GET /api/v1/templates`, `POST /api/v1/templates`, `GET /api/v1/templates/{name}`, `DELETE /api/v1/templates/{name}`, `POST /api/v1/jobs/from-template/{name}` | Reusable job template CRUD and job creation |
| `webhooks.py` | `GET /api/v1/webhooks`, `POST /api/v1/webhooks`, `DELETE /api/v1/webhooks/{webhook_id}` | Webhook registration and deletion |
| `queue.py` | `POST /api/v1/queue/jobs`, `GET /api/v1/queue/jobs`, `GET /api/v1/queue/jobs/{job_id}`, `DELETE /api/v1/queue/jobs/{job_id}`, `GET /api/v1/queue/stats`, `POST /api/v1/queue/poll` | Idempotent global-queue submission, status, cancellation, statistics, and operator-triggered polling |
| `inference_proxy.py` | `GET\|HEAD\|POST /inference/{endpoint_name}`, `GET\|HEAD\|POST /inference/{endpoint_name}/{upstream_path}` | Authenticated, allowlisted proxy to managed in-cluster inference services |

## Route Prefixes

The management APIs use `/api/v1`. Managed model-serving traffic uses `/inference/{endpoint_name}` so OpenAI-, Triton-, and native runtime paths can follow the endpoint name. Router modules own these prefixes; `manifest_api.py` mounts each router directly.

## Adding a New Route Module

1. Create a module with an `APIRouter` whose decorators include the complete public path or whose router declares a prefix.
2. Define the endpoints on that router.
3. Import and mount it in `manifest_api.py` with `app.include_router(router)`.
4. Document the exact methods and paths in this table.
5. Add or update the relevant existing tests under `tests/`.
