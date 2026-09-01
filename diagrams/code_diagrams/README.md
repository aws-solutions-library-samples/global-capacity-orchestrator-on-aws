# GCO Code Flowcharts

<!-- Generated at (UTC): 2026-09-01T13:22:56Z -->
<!-- Generated from Git commit: ed395032d46063f44b638deb85ae2a6dbf98e7f4 -->
*Generated at (UTC): `2026-09-01T13:22:56Z`.*
*Generated from Git commit: `ed395032d46063f44b638deb85ae2a6dbf98e7f4`.*

This directory holds auto-generated control-flow diagrams for the
Python source files listed below. Each target produces an interactive
[flowchart.js](https://github.com/adrai/flowchart.js) HTML page and (if
Playwright is available) a rendered PNG.

> Interactive HTML is the primary artifact — open it in any browser to
> pan, zoom, and export SVG/PNG directly. The PNGs are included for
> embedding in READMEs and pull requests where JS can't run.

## Table of Contents

- [Regeneration](#regeneration)
- [Prerequisites](#prerequisites)
- [Flowchart index](#flowchart-index)

## Regeneration

Use the aggregate driver for canonical committed output:

```bash
# Full code + infrastructure catalogues at the reviewed timestamp
SOURCE_DATE_EPOCH=1788091200 GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py

# Read-only artifact/index/marker/PNG contract
python diagrams/generate.py --check

# One catalogue only
SOURCE_DATE_EPOCH=1788091200 GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only
python diagrams/generate.py --infra-only

# A single target for local diagnosis
GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> \
python diagrams/code_diagrams/generate.py \
    --target lambda/analytics-presigned-url/handler.py:lambda_handler

# HTML only (skip Playwright and remove older PNGs for selected targets)
GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> \
python diagrams/code_diagrams/generate.py --skip-png

# Don't insert/refresh the ``# Flowchart:`` markers in source files
GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> \
python diagrams/code_diagrams/generate.py --skip-marker

# Remove every existing marker from the source tree and exit
# (useful when tearing the feature down or before a big refactor
# of placement rules)
python diagrams/code_diagrams/generate.py --strip-markers
```

See the [Prerequisites](#prerequisites) section below for one-time
browser install steps.

## Prerequisites

Install the project's ``diagrams`` extra, which pins ``pyflowchart`` and
``playwright`` to known-good versions:

```bash
pip install -e '.[diagrams]'
playwright install chromium
```

Without Playwright's browser, direct code-generator runs still write HTML and
remove any older PNG for the selected targets so mixed generation times are
impossible. Canonical aggregate generation requires ``SOURCE_DATE_EPOCH`` and
``GCO_DIAGRAM_SOURCE_COMMIT``. Commit substantive source changes first, then
supply that clean source commit while generating and commit the derived
artifacts separately; this avoids an impossible self-referential commit SHA.
The generator verifies every marker-stripped charted source against the supplied
commit. It records one UTC timestamp and source commit in HTML, PNG pixels, the
catalogue, and source markers. Each HTML/PNG pair also displays a deterministic
digest of the pre-annotation flow HTML, so source-flow changes remain visible
even when flowchart.js collapses them into the same SVG shape. Fixing the
timestamp prevents metadata-only churn; none of these mechanisms promises
byte-identical Chromium or Graphviz rasterization across toolchain versions or
platforms.
``python diagrams/generate.py --check`` enforces structural
contracts; ``tests/test_diagram_artifact_contract.py`` also verifies every PNG
with Pillow.

## Flowchart index

Entries below are grouped by top-level directory and listed in source
order. Each source file may contribute more than one flowchart if it
has multiple charted entry points.

### `app.py/`

- **`./`**
  - CDK app entry point (app.py::main) &mdash; `app.py::main` &mdash; [HTML](./app.main.html) · [PNG](./app.main.png)

### `cli/`

- **`cli/`**
  - gco jobs submit — direct kubectl apply path &mdash; `cli/jobs.py::JobManager.submit_job` &mdash; [HTML](./cli/jobs.JobManager_submit_job.html) · [PNG](./cli/jobs.JobManager_submit_job.png)
  - gco jobs submit-sqs — SQS-backed submission path &mdash; `cli/jobs.py::JobManager.submit_job_sqs` &mdash; [HTML](./cli/jobs.JobManager_submit_job_sqs.html) · [PNG](./cli/jobs.JobManager_submit_job_sqs.png)
  - Cognito SRP authentication (gco analytics studio login) &mdash; `cli/analytics_user_mgmt.py::srp_authenticate` &mdash; [HTML](./cli/analytics_user_mgmt.srp_authenticate.html) · [PNG](./cli/analytics_user_mgmt.srp_authenticate.png)
  - Studio presigned-URL fetch (gco analytics studio login) &mdash; `cli/analytics_user_mgmt.py::fetch_studio_url` &mdash; [HTML](./cli/analytics_user_mgmt.fetch_studio_url.html) · [PNG](./cli/analytics_user_mgmt.fetch_studio_url.png)
  - gco stacks deploy-all — orchestrated multi-stack deploy &mdash; `cli/stacks.py::StackManager.deploy_orchestrated` &mdash; [HTML](./cli/stacks.StackManager_deploy_orchestrated.html) · [PNG](./cli/stacks.StackManager_deploy_orchestrated.png)
  - gco stacks destroy-all — orchestrated multi-stack destroy &mdash; `cli/stacks.py::StackManager.destroy_orchestrated` &mdash; [HTML](./cli/stacks.StackManager_destroy_orchestrated.html) · [PNG](./cli/stacks.StackManager_destroy_orchestrated.png)
  - gco inference deploy — multi-region endpoint deploy &mdash; `cli/inference.py::InferenceManager.deploy` &mdash; [HTML](./cli/inference.InferenceManager_deploy.html) · [PNG](./cli/inference.InferenceManager_deploy.png)
  - gco inference canary — weighted canary rollout &mdash; `cli/inference.py::InferenceManager.canary_deploy` &mdash; [HTML](./cli/inference.InferenceManager_canary_deploy.html) · [PNG](./cli/inference.InferenceManager_canary_deploy.png)
  - Container runtime detection (docker > finch > podman) &mdash; `cli/_container_runtime.py::detect_container_runtime` &mdash; [HTML](./cli/_container_runtime.detect_container_runtime.html) · [PNG](./cli/_container_runtime.detect_container_runtime.png)
  - gco images build — context validation, login, build, push &mdash; `cli/images.py::ImageManager.build` &mdash; [HTML](./cli/images.ImageManager_build.html) · [PNG](./cli/images.ImageManager_build.png)
  - gco images push — auth + push existing local image &mdash; `cli/images.py::ImageManager.push` &mdash; [HTML](./cli/images.ImageManager_push.html) · [PNG](./cli/images.ImageManager_push.png)
  - gco images cleanup — bulk tag delete with filter branches &mdash; `cli/images.py::ImageManager.cleanup` &mdash; [HTML](./cli/images.ImageManager_cleanup.html) · [PNG](./cli/images.ImageManager_cleanup.png)
  - Volcano image-mirror config read (project-scoped ECR namespace default, #139) &mdash; `cli/_image_mirror.py::read_mirror_config` &mdash; [HTML](./cli/_image_mirror.read_mirror_config.html) · [PNG](./cli/_image_mirror.read_mirror_config.png)
  - Image mirror into project-scoped ECR (plan, strategy, auth, per-image copy, #139) &mdash; `cli/_image_mirror.py::mirror_images` &mdash; [HTML](./cli/_image_mirror.mirror_images.html) · [PNG](./cli/_image_mirror.mirror_images.png)
  - gco stacks deploy — pre-deploy image mirror gate (regional-only, #139) &mdash; `cli/stacks.py::StackManager._mirror_images_if_enabled` &mdash; [HTML](./cli/stacks.StackManager__mirror_images_if_enabled.html) · [PNG](./cli/stacks.StackManager__mirror_images_if_enabled.png)
  - gco jobs logs — TrainJob rank resolution and CloudWatch fallback chain &mdash; `cli/jobs.py::JobManager.get_job_logs` &mdash; [HTML](./cli/jobs.JobManager_get_job_logs.html) · [PNG](./cli/jobs.JobManager_get_job_logs.png)

- **`cli/commands/`**
  - Autopilot launch planner (engine, model, MCP, imports, resume) &mdash; `cli/commands/autopilot_cmd.py::_plan` &mdash; [HTML](./cli/commands/autopilot_cmd._plan.html) · [PNG](./cli/commands/autopilot_cmd._plan.png)

### `gco/`

- **`gco/services/`**
  - Backend authentication gate (health bypass, HMAC validation, fail-closed paths) &mdash; `gco/services/auth_middleware.py::AuthenticationMiddleware.dispatch` &mdash; [HTML](./gco/services/auth_middleware.AuthenticationMiddleware_dispatch.html) · [PNG](./gco/services/auth_middleware.AuthenticationMiddleware_dispatch.png)
  - Manifest API lifecycle (stores + optional central queue worker) &mdash; `gco/services/manifest_api.py::lifespan` &mdash; [HTML](./gco/services/manifest_api.lifespan.html) · [PNG](./gco/services/manifest_api.lifespan.png)
  - Central queue activation pass (migration, fenced claim, heartbeat, deterministic apply) &mdash; `gco/services/central_queue_worker.py::process_queued_jobs_once` &mdash; [HTML](./gco/services/central_queue_worker.process_queued_jobs_once.html) · [PNG](./gco/services/central_queue_worker.process_queued_jobs_once.png)
  - Central queue status reconciliation (Kubernetes UID fencing + terminal transitions) &mdash; `gco/services/central_queue_worker.py::reconcile_active_jobs_once` &mdash; [HTML](./gco/services/central_queue_worker.reconcile_active_jobs_once.html) · [PNG](./gco/services/central_queue_worker.reconcile_active_jobs_once.png)
  - Global queue fenced claim (conditional write + monotonic generation) &mdash; `gco/services/template_store.py::JobStore.claim_job` &mdash; [HTML](./gco/services/template_store.JobStore_claim_job.html) · [PNG](./gco/services/template_store.JobStore_claim_job.png)
  - Global queue lifecycle transition (lease, status, and Kubernetes UID fencing) &mdash; `gco/services/template_store.py::JobStore.transition_job` &mdash; [HTML](./gco/services/template_store.JobStore_transition_job.html) · [PNG](./gco/services/template_store.JobStore_transition_job.png)
  - Deterministic queued Job create-or-adopt path &mdash; `gco/services/manifest_processor.py::ManifestProcessor.apply_queued_job` &mdash; [HTML](./gco/services/manifest_processor.ManifestProcessor_apply_queued_job.html) · [PNG](./gco/services/manifest_processor.ManifestProcessor_apply_queued_job.png)
  - Inference endpoint authorized desired-state reconciliation &mdash; `gco/services/inference_monitor.py::InferenceMonitor._reconcile_endpoint_authorized` &mdash; [HTML](./gco/services/inference_monitor.InferenceMonitor__reconcile_endpoint_authorized.html) · [PNG](./gco/services/inference_monitor.InferenceMonitor__reconcile_endpoint_authorized.png)
  - SQS job prevalidation (kinds, TrainJob decomposition, security, weighted caps) &mdash; `gco/services/queue_processor.py::validate_manifest` &mdash; [HTML](./gco/services/queue_processor.validate_manifest.html) · [PNG](./gco/services/queue_processor.validate_manifest.png)
  - REST manifest validation pipeline (structure, kinds, limits, tolerations, images) &mdash; `gco/services/manifest_processor.py::ManifestProcessor.validate_manifest` &mdash; [HTML](./gco/services/manifest_processor.ManifestProcessor_validate_manifest.html) · [PNG](./gco/services/manifest_processor.ManifestProcessor_validate_manifest.png)
  - Spot price gate (unknown/malformed/above-cap dispatch policy) &mdash; `gco/services/spot_price_gate.py::SpotPriceGate.evaluate` &mdash; [HTML](./gco/services/spot_price_gate.SpotPriceGate_evaluate.html) · [PNG](./gco/services/spot_price_gate.SpotPriceGate_evaluate.png)
  - Request-size trust boundary (declared/streamed limits and exact replay) &mdash; `gco/services/request_size_middleware.py::RequestSizeLimitMiddleware.__call__` &mdash; [HTML](./gco/services/request_size_middleware.RequestSizeLimitMiddleware___call__.html) · [PNG](./gco/services/request_size_middleware.RequestSizeLimitMiddleware___call__.png)
  - Webhook delivery boundary (DNS pinning, HMAC, retries, redacted accounting) &mdash; `gco/services/webhook_dispatcher.py::WebhookDispatcher._deliver_webhook` &mdash; [HTML](./gco/services/webhook_dispatcher.WebhookDispatcher__deliver_webhook.html) · [PNG](./gco/services/webhook_dispatcher.WebhookDispatcher__deliver_webhook.png)
  - Mooncake prefill/decode dispatch (admin gate, KV handoff, streaming decode) &mdash; `gco/services/mooncake_pd_proxy.py::_dispatch` &mdash; [HTML](./gco/services/mooncake_pd_proxy._dispatch.html) · [PNG](./gco/services/mooncake_pd_proxy._dispatch.png)
  - Health status policy (thresholds, violations, collection failure) &mdash; `gco/services/health_monitor.py::HealthMonitor.get_health_status` &mdash; [HTML](./gco/services/health_monitor.HealthMonitor_get_health_status.html) · [PNG](./gco/services/health_monitor.HealthMonitor_get_health_status.png)

- **`gco/services/api_routes/`**
  - Authenticated inference target resolution (region, readiness, namespace, canary) &mdash; `gco/services/api_routes/inference_proxy.py::_resolve_upstream` &mdash; [HTML](./gco/services/api_routes/inference_proxy._resolve_upstream.html) · [PNG](./gco/services/api_routes/inference_proxy._resolve_upstream.png)
  - Managed inference reverse proxy (path allowlist, bounded I/O, streaming cleanup) &mdash; `gco/services/api_routes/inference_proxy.py::_proxy` &mdash; [HTML](./gco/services/api_routes/inference_proxy._proxy.html) · [PNG](./gco/services/api_routes/inference_proxy._proxy.png)

- **`gco/stacks/`**
  - Global stack constructor (Global Accelerator, SSM, DynamoDB) &mdash; `gco/stacks/global_stack.py::GCOGlobalStack.__init__` &mdash; [HTML](./gco/stacks/global_stack.GCOGlobalStack___init__.html) · [PNG](./gco/stacks/global_stack.GCOGlobalStack___init__.png)
  - API Gateway stack constructor (REST API + IAM + WAF) &mdash; `gco/stacks/api_gateway_global_stack.py::GCOApiGatewayGlobalStack.__init__` &mdash; [HTML](./gco/stacks/api_gateway_global_stack.GCOApiGatewayGlobalStack___init__.html) · [PNG](./gco/stacks/api_gateway_global_stack.GCOApiGatewayGlobalStack___init__.png)
  - Regional stack constructor (VPC, EKS, ALB, SQS, EFS) &mdash; `gco/stacks/regional_stack.py::GCORegionalStack.__init__` &mdash; [HTML](./gco/stacks/regional_stack.GCORegionalStack___init__.html) · [PNG](./gco/stacks/regional_stack.GCORegionalStack___init__.png)
  - Regional API Gateway stack constructor (private access) &mdash; `gco/stacks/regional_api_gateway_stack.py::GCORegionalApiGatewayStack.__init__` &mdash; [HTML](./gco/stacks/regional_api_gateway_stack.GCORegionalApiGatewayStack___init__.html) · [PNG](./gco/stacks/regional_api_gateway_stack.GCORegionalApiGatewayStack___init__.png)
  - Monitoring stack constructor (CloudWatch + alarms + SNS) &mdash; `gco/stacks/monitoring_stack.py::GCOMonitoringStack.__init__` &mdash; [HTML](./gco/stacks/monitoring_stack.GCOMonitoringStack___init__.html) · [PNG](./gco/stacks/monitoring_stack.GCOMonitoringStack___init__.png)
  - Analytics stack constructor (KMS, VPC, EFS, Studio, EMR, Cognito) &mdash; `gco/stacks/analytics_stack.py::GCOAnalyticsStack.__init__` &mdash; [HTML](./gco/stacks/analytics_stack.GCOAnalyticsStack___init__.html) · [PNG](./gco/stacks/analytics_stack.GCOAnalyticsStack___init__.png)
  - Analytics stack SageMaker execution role (hyperpod/canvas branches) &mdash; `gco/stacks/analytics_stack.py::GCOAnalyticsStack._create_execution_role_and_grants` &mdash; [HTML](./gco/stacks/analytics_stack.GCOAnalyticsStack__create_execution_role_and_grants.html) · [PNG](./gco/stacks/analytics_stack.GCOAnalyticsStack__create_execution_role_and_grants.png)
  - Analytics stack Studio domain (Canvas override branch) &mdash; `gco/stacks/analytics_stack.py::GCOAnalyticsStack._create_studio_domain` &mdash; [HTML](./gco/stacks/analytics_stack.GCOAnalyticsStack__create_studio_domain.html) · [PNG](./gco/stacks/analytics_stack.GCOAnalyticsStack__create_studio_domain.png)
  - Regional volcano image-mirror config (project-prefix + ECR-path validation, #139) &mdash; `gco/stacks/regional_stack.py::GCORegionalStack._get_volcano_image_mirror_config` &mdash; [HTML](./gco/stacks/regional_stack.GCORegionalStack__get_volcano_image_mirror_config.html) · [PNG](./gco/stacks/regional_stack.GCORegionalStack__get_volcano_image_mirror_config.png)
  - Global ECR replication rule (project-scoped PREFIX_MATCH filter, #139) &mdash; `gco/stacks/global_stack.py::GCOGlobalStack._create_image_replication_rule` &mdash; [HTML](./gco/stacks/global_stack.GCOGlobalStack__create_image_replication_rule.html) · [PNG](./gco/stacks/global_stack.GCOGlobalStack__create_image_replication_rule.png)

### `gco_mcp/`

- **`gco_mcp/`**
  - MCP audit_logged decorator (sync + async dispatch, Context capture) &mdash; `gco_mcp/audit.py::audit_logged` &mdash; [HTML](./gco_mcp/audit.audit_logged.html) · [PNG](./gco_mcp/audit.audit_logged.png)

- **`gco_mcp/mission/`**
  - Mission iteration loop (propose -> execute -> observe -> evaluate -> decide) &mdash; `gco_mcp/mission/engine.py::MissionEngine.run_iteration` &mdash; [HTML](./gco_mcp/mission/engine.MissionEngine_run_iteration.html) · [PNG](./gco_mcp/mission/engine.MissionEngine_run_iteration.png)
  - Mission verdict cascade (budget caps, completion, cadence-skip, heuristic) &mdash; `gco_mcp/mission/decide.py::decide_verdict` &mdash; [HTML](./gco_mcp/mission/decide.decide_verdict.html) · [PNG](./gco_mcp/mission/decide.decide_verdict.png)
  - Mission strategy-revision sampling (orchestrator + deterministic fallback) &mdash; `gco_mcp/mission/sampling.py::maybe_sample_strategy_revision` &mdash; [HTML](./gco_mcp/mission/sampling.maybe_sample_strategy_revision.html) · [PNG](./gco_mcp/mission/sampling.maybe_sample_strategy_revision.png)
  - Mission script AST validator (parse-time allowlist enforcement) &mdash; `gco_mcp/mission/sandbox.py::validate_script_ast` &mdash; [HTML](./gco_mcp/mission/sandbox.validate_script_ast.html) · [PNG](./gco_mcp/mission/sandbox.validate_script_ast.png)
  - Mission criteria scaffolder (Bedrock sampling + retry + autofix pipeline) &mdash; `gco_mcp/mission/criteria_scaffold.py::generate_sampled_criteria` &mdash; [HTML](./gco_mcp/mission/criteria_scaffold.generate_sampled_criteria.html) · [PNG](./gco_mcp/mission/criteria_scaffold.generate_sampled_criteria.png)
  - Mission engine factory (live vs stub dispatcher, sampling, sandbox wiring) &mdash; `gco_mcp/mission/_engine_factory.py::build_engine_dependencies` &mdash; [HTML](./gco_mcp/mission/_engine_factory.build_engine_dependencies.html) · [PNG](./gco_mcp/mission/_engine_factory.build_engine_dependencies.png)
  - Swarm runner lifecycle (fleet guard, respawn, settlement, cascade shutdown) &mdash; `gco_mcp/mission/swarm_runner.py::SwarmRunner.run_to_completion` &mdash; [HTML](./gco_mcp/mission/swarm_runner.SwarmRunner_run_to_completion.html) · [PNG](./gco_mcp/mission/swarm_runner.SwarmRunner_run_to_completion.png)

- **`gco_mcp/tools/`**
  - MCP long-task runner (drain, progress, cancel + SIGTERM/SIGKILL) &mdash; `gco_mcp/tools/_long_task.py::_run_long_task` &mdash; [HTML](./gco_mcp/tools/_long_task._run_long_task.html) · [PNG](./gco_mcp/tools/_long_task._run_long_task.png)

### `lambda/`

- **`lambda/analytics-cleanup/`**
  - Analytics Cleanup Lambda (stack-delete drain) &mdash; `lambda/analytics-cleanup/handler.py::handler` &mdash; [HTML](./lambda/analytics-cleanup/handler.handler.html) · [PNG](./lambda/analytics-cleanup/handler.handler.png)

- **`lambda/analytics-presigned-url/`**
  - Analytics Presigned-URL Lambda (SageMaker Studio login) &mdash; `lambda/analytics-presigned-url/handler.py::lambda_handler` &mdash; [HTML](./lambda/analytics-presigned-url/handler.lambda_handler.html) · [PNG](./lambda/analytics-presigned-url/handler.lambda_handler.png)

- **`lambda/api-gateway-proxy/`**
  - API Gateway Proxy Lambda &mdash; `lambda/api-gateway-proxy/handler.py::lambda_handler` &mdash; [HTML](./lambda/api-gateway-proxy/handler.lambda_handler.html) · [PNG](./lambda/api-gateway-proxy/handler.lambda_handler.png)

- **`lambda/capacity-poller/`**
  - Capacity snapshot poller (Region truth, pooled scores, bounded retries, writes) &mdash; `lambda/capacity-poller/handler.py::lambda_handler` &mdash; [HTML](./lambda/capacity-poller/handler.lambda_handler.html) · [PNG](./lambda/capacity-poller/handler.lambda_handler.png)

- **`lambda/cross-region-aggregator/`**
  - Cross-Region Aggregator Lambda &mdash; `lambda/cross-region-aggregator/handler.py::lambda_handler` &mdash; [HTML](./lambda/cross-region-aggregator/handler.lambda_handler.html) · [PNG](./lambda/cross-region-aggregator/handler.lambda_handler.png)

- **`lambda/drift-detection/`**
  - CloudFormation Drift Detection Lambda &mdash; `lambda/drift-detection/handler.py::lambda_handler` &mdash; [HTML](./lambda/drift-detection/handler.lambda_handler.html) · [PNG](./lambda/drift-detection/handler.lambda_handler.png)

- **`lambda/ga-registration/`**
  - Global Accelerator Endpoint Registration Lambda &mdash; `lambda/ga-registration/handler.py::lambda_handler` &mdash; [HTML](./lambda/ga-registration/handler.lambda_handler.html) · [PNG](./lambda/ga-registration/handler.lambda_handler.png)

- **`lambda/helm-installer/`**
  - Helm Installer Lambda (CFN custom resource) &mdash; `lambda/helm-installer/handler.py::lambda_handler` &mdash; [HTML](./lambda/helm-installer/handler.lambda_handler.html) · [PNG](./lambda/helm-installer/handler.lambda_handler.png)
  - Helm teardown provider event path (install drain + idempotent execution start) &mdash; `lambda/helm-installer/teardown_provider.py::on_event` &mdash; [HTML](./lambda/helm-installer/teardown_provider.on_event.html) · [PNG](./lambda/helm-installer/teardown_provider.on_event.png)
  - Helm teardown completion poll (continued fencing + terminal status) &mdash; `lambda/helm-installer/teardown_provider.py::is_complete` &mdash; [HTML](./lambda/helm-installer/teardown_provider.is_complete.html) · [PNG](./lambda/helm-installer/teardown_provider.is_complete.png)
  - Helm convergence per-chart decision (EnabledCharts authority: install vs uninstall) &mdash; `lambda/helm-installer/handler.py::handle_task` &mdash; [HTML](./lambda/helm-installer/handler.handle_task.html) · [PNG](./lambda/helm-installer/handler.handle_task.png)
  - Helm release-set validation (charts.yaml expected set, deployed vs absent) &mdash; `lambda/helm-installer/handler.py::validate_releases` &mdash; [HTML](./lambda/helm-installer/handler.validate_releases.html) · [PNG](./lambda/helm-installer/handler.validate_releases.png)

- **`lambda/helm-orchestrator/`**
  - Helm convergence orchestrator (start/adopt, replay identity, rollback fencing) &mdash; `lambda/helm-orchestrator/handler.py::on_event` &mdash; [HTML](./lambda/helm-orchestrator/handler.on_event.html) · [PNG](./lambda/helm-orchestrator/handler.on_event.png)

- **`lambda/image-lookup/`**
  - Image-lookup-or-create custom resource Lambda &mdash; `lambda/image-lookup/handler.py::lambda_handler` &mdash; [HTML](./lambda/image-lookup/handler.lambda_handler.html) · [PNG](./lambda/image-lookup/handler.lambda_handler.png)

- **`lambda/kubectl-applier-simple/`**
  - Kubectl Applier Lambda (CFN custom resource) &mdash; `lambda/kubectl-applier-simple/handler.py::lambda_handler` &mdash; [HTML](./lambda/kubectl-applier-simple/handler.lambda_handler.html) · [PNG](./lambda/kubectl-applier-simple/handler.lambda_handler.png)

- **`lambda/proxy-shared/`**
  - Proxy request-bound HMAC envelope construction &mdash; `lambda/proxy-shared/proxy_utils.py::build_signed_headers` &mdash; [HTML](./lambda/proxy-shared/proxy_utils.build_signed_headers.html) · [PNG](./lambda/proxy-shared/proxy_utils.build_signed_headers.png)

- **`lambda/regional-api-proxy/`**
  - Regional API Gateway Proxy Lambda &mdash; `lambda/regional-api-proxy/handler.py::lambda_handler` &mdash; [HTML](./lambda/regional-api-proxy/handler.lambda_handler.html) · [PNG](./lambda/regional-api-proxy/handler.lambda_handler.png)

- **`lambda/secret-rotation/`**
  - Secrets Manager Rotation Lambda &mdash; `lambda/secret-rotation/handler.py::lambda_handler` &mdash; [HTML](./lambda/secret-rotation/handler.lambda_handler.html) · [PNG](./lambda/secret-rotation/handler.lambda_handler.png)

- **`lambda/tls-certificate-manager/`**
  - Backend TLS Certificate Manager Lambda &mdash; `lambda/tls-certificate-manager/handler.py::lambda_handler` &mdash; [HTML](./lambda/tls-certificate-manager/handler.lambda_handler.html) · [PNG](./lambda/tls-certificate-manager/handler.lambda_handler.png)

- **`lambda/tls-shared/`**
  - Private-root backend TLS trust refresh and verified connection pool &mdash; `lambda/tls-shared/backend_tls.py::get_backend_http_pool` &mdash; [HTML](./lambda/tls-shared/backend_tls.get_backend_http_pool.html) · [PNG](./lambda/tls-shared/backend_tls.get_backend_http_pool.png)

- **`lambda/traffic-dial-controller/`**
  - Traffic dial controller (health evidence, step limits, last-Region safety) &mdash; `lambda/traffic-dial-controller/handler.py::lambda_handler` &mdash; [HTML](./lambda/traffic-dial-controller/handler.lambda_handler.html) · [PNG](./lambda/traffic-dial-controller/handler.lambda_handler.png)

- **`lambda/vector-ingest/`**
  - Vector-store corpus ingest (S3 notification -> chunk, embed, write items) &mdash; `lambda/vector-ingest/handler.py::lambda_handler` &mdash; [HTML](./lambda/vector-ingest/handler.lambda_handler.html) · [PNG](./lambda/vector-ingest/handler.lambda_handler.png)
