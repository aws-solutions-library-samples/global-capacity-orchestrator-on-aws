"""Targets for :mod:`diagrams.code_diagrams.generate`.

Each :class:`Target` names a source file and a top-level
function/method to flowchart. Add new entries here to extend the
catalogue — the generator and README pick them up automatically.

Path conventions:

* ``source`` is relative to the project root (the directory that owns
  ``cdk.json``).
* ``function`` is the name as ``pyflowchart`` would resolve it via
  ``--field``. Use dotted form (``Class.method``) for methods.
* ``inner`` controls whether to parse the *body* of the function
  (``True``) or the function definition itself (``False``). Body-level
  charts read far better for control-flow-heavy functions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    """A single function or method to flowchart."""

    source: str
    """Path to the source file, relative to project root."""

    function: str
    """Name of the function (or ``Class.method``) inside ``source``."""

    inner: bool = True
    """If ``True``, chart the body of the function (preferred)."""

    title: str | None = None
    """Optional human-readable title for the HTML page and README."""

    def slug(self) -> str:
        """File-safe slug for the function component of output names."""
        return self.function.replace(".", "_")


# Order matters only for the progress output; README groups by source
# directory regardless. New targets go at the end of the appropriate
# section so review diffs stay local.
TARGETS: list[Target] = [
    # --- Top-level CDK app entry point -----------------------------------
    # ``app.py::main`` has real control flow (per-region loop, analytics
    # sub-toggle gating) so its flowchart is informative. CDK Stack
    # ``__init__`` methods are mostly linear wiring sequences and we
    # chart only the ones that carry real branches (e.g. the
    # ``_create_execution_role_and_grants`` helper on the analytics
    # stack, which has hyperpod/canvas branches).
    Target(
        source="app.py",
        function="main",
        title="CDK app entry point (app.py::main)",
    ),
    # --- Lambda handlers -------------------------------------------------
    Target(
        source="lambda/analytics-presigned-url/handler.py",
        function="lambda_handler",
        title="Analytics Presigned-URL Lambda (SageMaker Studio login)",
    ),
    Target(
        source="lambda/analytics-cleanup/handler.py",
        function="handler",
        title="Analytics Cleanup Lambda (stack-delete drain)",
    ),
    Target(
        source="lambda/api-gateway-proxy/handler.py",
        function="lambda_handler",
        title="API Gateway Proxy Lambda",
    ),
    Target(
        source="lambda/regional-api-proxy/handler.py",
        function="lambda_handler",
        title="Regional API Gateway Proxy Lambda",
    ),
    Target(
        source="lambda/cross-region-aggregator/handler.py",
        function="lambda_handler",
        title="Cross-Region Aggregator Lambda",
    ),
    Target(
        source="lambda/drift-detection/handler.py",
        function="lambda_handler",
        title="CloudFormation Drift Detection Lambda",
    ),
    Target(
        source="lambda/ga-registration/handler.py",
        function="lambda_handler",
        title="Global Accelerator Endpoint Registration Lambda",
    ),
    Target(
        source="lambda/helm-installer/handler.py",
        function="lambda_handler",
        title="Helm Installer Lambda (CFN custom resource)",
    ),
    Target(
        source="lambda/kubectl-applier-simple/handler.py",
        function="lambda_handler",
        title="Kubectl Applier Lambda (CFN custom resource)",
    ),
    Target(
        source="lambda/secret-rotation/handler.py",
        function="lambda_handler",
        title="Secrets Manager Rotation Lambda",
    ),
    Target(
        source="lambda/tls-certificate-manager/handler.py",
        function="lambda_handler",
        title="Backend TLS Certificate Manager Lambda",
    ),
    # --- CLI entry points ------------------------------------------------
    Target(
        source="cli/jobs.py",
        function="JobManager.submit_job",
        title="gco jobs submit — direct kubectl apply path",
    ),
    Target(
        source="cli/jobs.py",
        function="JobManager.submit_job_sqs",
        title="gco jobs submit-sqs — SQS-backed submission path",
    ),
    Target(
        source="cli/analytics_user_mgmt.py",
        function="srp_authenticate",
        title="Cognito SRP authentication (gco analytics studio login)",
    ),
    Target(
        source="cli/analytics_user_mgmt.py",
        function="fetch_studio_url",
        title="Studio presigned-URL fetch (gco analytics studio login)",
    ),
    # --- Additional CLI branchy paths ------------------------------------
    Target(
        source="cli/stacks.py",
        function="StackManager.deploy_orchestrated",
        title="gco stacks deploy-all — orchestrated multi-stack deploy",
    ),
    Target(
        source="cli/stacks.py",
        function="StackManager.destroy_orchestrated",
        title="gco stacks destroy-all — orchestrated multi-stack destroy",
    ),
    Target(
        source="cli/inference.py",
        function="InferenceManager.deploy",
        title="gco inference deploy — multi-region endpoint deploy",
    ),
    Target(
        source="cli/inference.py",
        function="InferenceManager.canary_deploy",
        title="gco inference canary — weighted canary rollout",
    ),
    # --- CDK stack constructors ------------------------------------------
    # Each ``__init__`` is a mostly-linear wiring sequence (create KMS
    # key → create VPC → create role → …). We chart them anyway because
    # they're the single most useful map for readers learning the code:
    # "given this stack, which helpers run in what order, and what
    # objects do they produce?".
    Target(
        source="gco/stacks/global_stack.py",
        function="GCOGlobalStack.__init__",
        title="Global stack constructor (Global Accelerator, SSM, DynamoDB)",
    ),
    Target(
        source="gco/stacks/api_gateway_global_stack.py",
        function="GCOApiGatewayGlobalStack.__init__",
        title="API Gateway stack constructor (REST API + IAM + WAF)",
    ),
    Target(
        source="gco/stacks/regional_stack.py",
        function="GCORegionalStack.__init__",
        title="Regional stack constructor (VPC, EKS, ALB, SQS, EFS)",
    ),
    Target(
        source="gco/stacks/regional_api_gateway_stack.py",
        function="GCORegionalApiGatewayStack.__init__",
        title="Regional API Gateway stack constructor (private access)",
    ),
    Target(
        source="gco/stacks/monitoring_stack.py",
        function="GCOMonitoringStack.__init__",
        title="Monitoring stack constructor (CloudWatch + alarms + SNS)",
    ),
    Target(
        source="gco/stacks/analytics_stack.py",
        function="GCOAnalyticsStack.__init__",
        title="Analytics stack constructor (KMS, VPC, EFS, Studio, EMR, Cognito)",
    ),
    # --- CDK stack helpers with real branches ----------------------------
    # Most CDK ``__init__`` methods are linear wiring sequences (create
    # KMS key, create VPC, create role, ...). These helpers are the
    # exception — they carry real conditional branches tied to
    # sub-toggles (hyperpod, canvas, fsx, valkey, aurora) and feature
    # flags, so a flowchart of them is genuinely informative.
    Target(
        source="gco/stacks/analytics_stack.py",
        function="GCOAnalyticsStack._create_execution_role_and_grants",
        title="Analytics stack SageMaker execution role (hyperpod/canvas branches)",
    ),
    Target(
        source="gco/stacks/analytics_stack.py",
        function="GCOAnalyticsStack._create_studio_domain",
        title="Analytics stack Studio domain (Canvas override branch)",
    ),
    # --- Runtime service security and reconciliation paths ---------------
    Target(
        source="gco/services/auth_middleware.py",
        function="AuthenticationMiddleware.dispatch",
        title="Backend authentication gate (health bypass, HMAC validation, fail-closed paths)",
    ),
    Target(
        source="lambda/proxy-shared/proxy_utils.py",
        function="build_signed_headers",
        title="Proxy request-bound HMAC envelope construction",
    ),
    Target(
        source="lambda/tls-shared/backend_tls.py",
        function="get_backend_http_pool",
        title="Private-root backend TLS trust refresh and verified connection pool",
    ),
    Target(
        source="gco/services/manifest_api.py",
        function="lifespan",
        title="Manifest API lifecycle (stores + optional central queue worker)",
    ),
    Target(
        source="gco/services/central_queue_worker.py",
        function="process_queued_jobs_once",
        title="Central queue activation pass (migration, fenced claim, heartbeat, deterministic apply)",
    ),
    Target(
        source="gco/services/central_queue_worker.py",
        function="reconcile_active_jobs_once",
        title="Central queue status reconciliation (Kubernetes UID fencing + terminal transitions)",
    ),
    Target(
        source="gco/services/template_store.py",
        function="JobStore.claim_job",
        title="Global queue fenced claim (conditional write + monotonic generation)",
    ),
    Target(
        source="gco/services/template_store.py",
        function="JobStore.transition_job",
        title="Global queue lifecycle transition (lease, status, and Kubernetes UID fencing)",
    ),
    Target(
        source="gco/services/manifest_processor.py",
        function="ManifestProcessor.apply_queued_job",
        title="Deterministic queued Job create-or-adopt path",
    ),
    Target(
        source="gco/services/api_routes/inference_proxy.py",
        function="_resolve_upstream",
        title="Authenticated inference target resolution (region, readiness, namespace, canary)",
    ),
    Target(
        source="gco/services/api_routes/inference_proxy.py",
        function="_proxy",
        title="Managed inference reverse proxy (path allowlist, bounded I/O, streaming cleanup)",
    ),
    Target(
        source="gco/services/inference_monitor.py",
        function="InferenceMonitor._reconcile_endpoint",
        title="Inference endpoint desired-state reconciliation",
    ),
    Target(
        source="lambda/helm-installer/teardown_provider.py",
        function="on_event",
        title="Helm teardown provider event path (install drain + idempotent execution start)",
    ),
    Target(
        source="lambda/helm-installer/teardown_provider.py",
        function="is_complete",
        title="Helm teardown completion poll (continued fencing + terminal status)",
    ),
    # --- MCP server branchy modules --------------------------------------
    # New code-diagram targets for the branchy MCP modules introduced by
    # this work. Each one carries real control flow tied to feature
    # flags, FastMCP Tasks cancellation, image-registry replication,
    # or audit-log enrichment.
    Target(
        source="cli/_container_runtime.py",
        function="detect_container_runtime",
        title="Container runtime detection (docker > finch > podman)",
    ),
    Target(
        source="cli/images.py",
        function="ImageManager.build",
        title="gco images build — context validation, login, build, push",
    ),
    Target(
        source="cli/images.py",
        function="ImageManager.push",
        title="gco images push — auth + push existing local image",
    ),
    Target(
        source="cli/images.py",
        function="ImageManager.cleanup",
        title="gco images cleanup — bulk tag delete with filter branches",
    ),
    Target(
        source="gco_mcp/audit.py",
        function="audit_logged",
        title="MCP audit_logged decorator (sync + async dispatch, Context capture)",
    ),
    Target(
        source="gco_mcp/tools/_long_task.py",
        function="_run_long_task",
        title="MCP long-task runner (drain, progress, cancel + SIGTERM/SIGKILL)",
    ),
    Target(
        source="lambda/image-lookup/handler.py",
        function="lambda_handler",
        title="Image-lookup-or-create custom resource Lambda",
    ),
    # --- Mission goal-directed iteration loop ----------------------------
    Target(
        source="gco_mcp/mission/engine.py",
        function="MissionEngine.run_iteration",
        title="Mission iteration loop (propose -> execute -> observe -> evaluate -> decide)",
    ),
    Target(
        source="gco_mcp/mission/decide.py",
        function="decide_verdict",
        title="Mission verdict cascade (budget caps, completion, cadence-skip, heuristic)",
    ),
    Target(
        source="gco_mcp/mission/sampling.py",
        function="maybe_sample_strategy_revision",
        title="Mission strategy-revision sampling (orchestrator + deterministic fallback)",
    ),
    Target(
        source="gco_mcp/mission/sandbox.py",
        function="validate_script_ast",
        title="Mission script AST validator (parse-time allowlist enforcement)",
    ),
    Target(
        source="gco_mcp/mission/criteria_scaffold.py",
        function="generate_sampled_criteria",
        title="Mission criteria scaffolder (Bedrock sampling + retry + autofix pipeline)",
    ),
    Target(
        source="gco_mcp/mission/_engine_factory.py",
        function="build_engine_dependencies",
        title="Mission engine factory (live vs stub dispatcher, sampling, sandbox wiring)",
    ),
    # --- project_name / ECR image-namespace scoping (#139) ---------------
    # These paths make every ECR image namespace derive from
    # ``project_name`` so multiple GCO deployments can co-exist in one
    # account/region without colliding. Each carries real control flow
    # (enable toggles, project-prefix + regex validation, per-region
    # mirror loop, replication-rule guards), so a flowchart is genuinely
    # informative for readers auditing the multi-deployment story.
    Target(
        source="cli/_image_mirror.py",
        function="read_mirror_config",
        title="Volcano image-mirror config read (project-scoped ECR namespace default, #139)",
    ),
    Target(
        source="cli/_image_mirror.py",
        function="mirror_images",
        title="Image mirror into project-scoped ECR (plan, strategy, auth, per-image copy, #139)",
    ),
    Target(
        source="cli/stacks.py",
        function="StackManager._mirror_images_if_enabled",
        title="gco stacks deploy — pre-deploy image mirror gate (regional-only, #139)",
    ),
    Target(
        source="gco/stacks/regional_stack.py",
        function="GCORegionalStack._get_volcano_image_mirror_config",
        title="Regional volcano image-mirror config (project-prefix + ECR-path validation, #139)",
    ),
    Target(
        source="gco/stacks/global_stack.py",
        function="GCOGlobalStack._create_image_replication_rule",
        title="Global ECR replication rule (project-scoped PREFIX_MATCH filter, #139)",
    ),
    # --- 6.0: trainer, MLflow, vector store (#252) ------------------------
    # The validation pipeline gained a workload kind without a single pod
    # spec (TrainJob decomposes into weighted views), the helm installer
    # now converges two more charts, and the CLI job lifecycle grew
    # TrainJob-aware fallback chains. Each target below carries the real
    # branch structure a reader needs to audit those flows.
    Target(
        source="gco/services/queue_processor.py",
        function="validate_manifest",
        title="SQS job prevalidation (kinds, TrainJob decomposition, security, weighted caps)",
    ),
    Target(
        source="gco/services/manifest_processor.py",
        function="ManifestProcessor.validate_manifest",
        title="REST manifest validation pipeline (structure, kinds, limits, tolerations, images)",
    ),
    Target(
        source="lambda/helm-installer/handler.py",
        function="handle_task",
        title="Helm convergence per-chart decision (EnabledCharts authority: install vs uninstall)",
    ),
    Target(
        source="lambda/helm-installer/handler.py",
        function="validate_releases",
        title="Helm release-set validation (charts.yaml expected set, deployed vs absent)",
    ),
    Target(
        source="cli/jobs.py",
        function="JobManager.get_job_logs",
        title="gco jobs logs — TrainJob rank resolution and CloudWatch fallback chain",
    ),
    Target(
        source="lambda/vector-ingest/handler.py",
        function="lambda_handler",
        title="Vector-store corpus ingest (S3 notification -> chunk, embed, write items)",
    ),
]
