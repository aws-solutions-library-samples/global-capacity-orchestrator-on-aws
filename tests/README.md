# GCO Test Suite

This directory contains the test suite for GCO (Global Capacity Orchestrator on AWS). The tests are organized by component and functionality.

## Table of Contents

- [Running Tests](#running-tests)
- [Mission End-to-End Tests](#mission-end-to-end-tests)
- [Test Organization](#test-organization)
- [Test Files by Category](#test-files-by-category)
- [cdk-nag Compliance Testing](#cdk-nag-compliance-testing)
- [Lambda Handler Import Helper](#lambda-handler-import-helper)
- [Writing New Tests](#writing-new-tests)
- [Mocking Patterns](#mocking-patterns)
- [Coverage Requirements](#coverage-requirements)
- [Type Checking](#type-checking)
- [Import Conventions After the Manifest API Refactor](#import-conventions-after-the-manifest-api-refactor)
- [Hermetic Environment Variables](#hermetic-environment-variables)
- [Common Issues](#common-issues)

## Running Tests

```bash
# Run all tests
python -m pytest

# Run with coverage report
python -m pytest --cov=gco --cov=cli --cov=gco_mcp --cov-report=term-missing

# Run specific test file
python -m pytest tests/test_manifest_api.py -v

# Run specific test class
python -m pytest tests/test_manifest_api.py::TestManifestSubmission -v

# Run specific test
python -m pytest tests/test_manifest_api.py::TestManifestSubmission::test_submit_valid_manifest -v

# Run tests matching a pattern
python -m pytest -k "health" -v
```

## Mission End-to-End Tests

The `mission_e2e` marker (registered under `[tool.pytest.ini_options]` in
`pyproject.toml`) tags 14 end-to-end tests across eight files that drive a
complete Mission session through `MissionEngine`. The covered files are
`test_metric_readers_observe.py`, `test_mission_e2e_train_to_loss.py`,
`test_mission_e2e_search.py`, `test_mission_e2e_converge.py`,
`test_mission_e2e_budget.py`, `test_mission_e2e_stagnation.py`,
`test_mission_no_aws.py`, and `test_semantic_progress_observe.py`. Per-file
descriptions live in the [Mission Tests](#mission-tests) section below.

```bash
# Run only the Mission e2e suite (about 11 seconds wall-clock on a laptop)
python -m pytest -m mission_e2e

# CI-friendly invocation — caps each test at 30 seconds. Requires
# pytest-timeout, which is not pinned by this project; install ad hoc
# (`pip install pytest-timeout`) for the local run if you want the
# per-test gate.
python -m pytest -m mission_e2e --timeout=30

# No-extra-deps alternative — wraps the whole invocation in coreutils
# `timeout` so the wall-clock gate still fires without pytest-timeout.
# (Linux ships `timeout` in coreutils; macOS users can `brew install
# coreutils` and substitute `gtimeout`.)
command timeout 30 python -m pytest -m mission_e2e
```

Every test wires a stub dispatcher and a `FilesystemBackend(root=tmp_path)` so the suite runs offline — no AWS credentials, no network, no real LLM. The full set completes in well under 30 seconds on a fresh checkout.

## Test Organization

Tests are organized by the component they test:

| Category | Files | Description |
|----------|-------|-------------|
| CLI | `test_cli*.py` | Command-line interface tests |
| API | `test_manifest_api*.py`, `test_health_api*.py` | REST API endpoint tests |
| Services | `test_manifest_processor*.py`, `test_health_monitor*.py` | Core service logic tests |
| Models | `test_models*.py` | Pydantic model validation tests |
| [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) Stacks | `test_*_stack*.py` | Infrastructure-as-code tests |
| Storage | `test_template_store.py` | [DynamoDB](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Introduction.html) storage layer tests |
| Integration | `test_integration.py`, `test_sqs_integration.py` | End-to-end integration tests |
| MCP Server | `test_mcp_server.py`, `test_mcp_audit.py`, `test_mcp_resources_new.py`, `test_mcp_integration.py` | MCP tools, resources, audit logging, protocol tests |
| Mission | `test_mission_*.py` | Mission goal-directed iteration loop — engine, state, validators, sampling, sandbox, audit, CLI, scaffolder, MCP gating, and end-to-end sessions |
| Infrastructure | `test_oidc_stack.py`, `test_feature_toggles.py` | OIDC provider stack, feature toggle helpers (Valkey, [Aurora](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/CHAP_AuroraOverview.html), [FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html)) |
| Node [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) | `inference-streaming-proxy/*.test.mjs` | Native Node.js 24 unit tests for response streaming, routing, signing, TLS, retries, cache behavior, and disconnect handling; separate CI workflow |

## Test Files by Category

### CLI Tests

| File | Description |
|------|-------------|
| `test_cli.py` | Foundational `cli/config.GCOConfig` round-trips — defaults, dict / YAML / JSON construction, `from_env` for the `GCO_*` variable set, and the `get_config` singleton merging file and env sources. The broad happy-path smoke test that sits alongside the more targeted suites. |
| `test_cli_main.py` | `cli/main.py` Click entry point — version / help, jobs list (with `--region`, `--all-regions`, and filters), jobs get (required `--region`, not-found handling), driven via `CliRunner` with `get_job_manager` patched. Focuses on argument validation and the `--all-regions` global aggregation path. |
| `test_cli_commands.py` | Top-level Click command surface — `gco --help`, `--version`, `--config`, plus the jobs subgroup (list with required `--region` / `--all-regions` guard, get by name, not-found handling) through `CliRunner` with the job manager and output formatter mocked out. Command-layer companion to `test_cli_main.py`. |
| `test_cli_autopilot.py` | `gco autopilot` behavior through `CliRunner` with the install/exec boundaries mocked: Bedrock model resolution precedence, generated MCP-config shape (companions, checkout vs pinned-uvx `gco` entry, read-only EKS default), GCO MCP feature-flag / `--mcp-env` injection and validation, session resumption (`--continue` / `--resume` / interactive prompt gating), the lazy pinned Claude Code install flow, and lockstep guards keeping `cli/autopilot.py`, the `gco_mcp/README.md` companion tables, and the deps-scan extraction regexes in agreement. |
| `test_cli_coverage.py` | Edge-case CLI branches the other suites don't reach — pod-logs success / empty / error paths with container selection, error handling in command handlers, formatter interaction matrix. Mocks `get_job_manager` and `get_output_formatter` from the command modules so Click wiring is also exercised. |
| `test_cli_command_gap_coverage.py` | Behavior-focused branch coverage for the config, costs, capacity, stacks, images, models, and webhooks Click commands. Every command runs through `CliRunner`, with AWS, managers, image mirroring, subprocesses, and timing boundaries mocked. |
| `test_cli_help.py` | Help-text smoke tests across every Click command tree node (top-level `gco` plus jobs / stacks / capacity / files / inference / queue / templates / webhooks / costs / models). Invokes `--help` on each and asserts exit code 0 — catches regressions where a command raises at import time or fails option validation before the help screen renders. |
| `test_cli_queue_templates_webhooks.py` | `gco queue / templates / webhooks` subgroups — `queue submit` with manifest files, priority, and labels (writing real temp YAML files and mocking `aws_client.call_api`) plus the templates and webhooks subgroups. Contract test between the Click commands and the `/queue` endpoints. |
| `test_cli_release.py` | `gco release validate` — the no-prompt wrapper around the live-validation harness. Every subprocess is faked (git derivations answer canned SHA/branch/toplevel; the harness launch is captured, never executed) so the suite is hermetic on detached-HEAD CI checkouts. Covers the consent gates (missing `--i-understand-this-deploys-and-destroys-infrastructure`, malformed account, deploy without `--confirm-kms-key-deletion`, `--resume` without run-id/report-dir, empty `--actions`), harness argv composition (derived SHA/branch/run-id, report dir placed outside the checkout, checkpoint path, forwarded profile/protected-stacks/resume), exit-code propagation, the `--emulator-endpoint` env pair (`GCO_LIVE_VALIDATION_EMULATOR` + `AWS_ENDPOINT_URL`, and its absence otherwise), and repo-root validation failure modes. |
| `test_cli_sqs_commands.py` | `gco jobs submit-sqs` (with labels, priority, auto-region discovery) and `queue-status`, using tempfile-backed YAML manifests and a mocked `JobManager.submit_job_sqs`. Targets the code paths in `cli/main.py` that talk to the [SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html) consumer pipeline rather than the REST manifest endpoint. |
| `test_cli_status.py` | The `gco status` command surface through `CliRunner` with the gatherer patched and hand-built `FleetStatus` documents: JSON/YAML purity (one parseable document, no table glyphs), table rendering of every section status including `skipped`/`unavailable` with reasons, findings above section detail with an explicit no-findings statement, table and JSON cross-asserted against the same document, flag forwarding, `--watch` refusals (sub-floor interval, JSON/YAML) and loop behavior with patched sleep (clear/redraw, 15-minute costs reuse with `as_of` preserved, expiry re-fetch), and `--fail-on-findings` exit codes (1 only on an `error`-severity finding, always after a full render). |
| `test_status_gatherer.py` | Fleet status document assembly in `cli/status.py` — region resolution from `cdk.json` (configured, flag-narrowed, missing → `unavailable` naming both remedies, and never the all-region stack scan), each section gatherer against its mocked manager factory using real `StackInfo`/`RegionCapacity`/`CostSummary` payloads (success, empty, partial, the specific `unavailable` classifications, unexpected-exception escape), the queue/capacity gates that keep discovery-based managers unreachable when no regional stack exists, opt-in costs/nodepools behavior including the private-endpoint branch asserting the NodePool list is never attempted, the section-boundary and per-section timeout (a stuck gatherer degrades without holding the document), the seven findings rules one test per rule, and the `overall`/`degraded` derivation table. |
| `test_storage.py` | Human-friendly [S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html) bucket discovery and safe explicit download/upload sync — authoritative SSM/CloudFormation alias resolution, pagination and incrementality, SHA-256 upload metadata and checksums, no-delete semantics, unsafe-key/collision rejection, source mutation detection, and descriptor-pinned POSIX confinement. Uses an in-memory S3 fake; no AWS calls. |
| `test_storage_cmd.py` | `gco storage list` and `gco storage sync` Click behavior — table/JSON output, download/upload and dry-run summaries, hidden MCP confinement contract propagation, errors, and cooperative SIGTERM handling. |

### API Tests

| File | Description |
|------|-------------|
| `test_split_tests.py` | Tests for `scripts/split_tests.py`, which shards the core pytest suite across parallel CI jobs. The headline case asserts that a partition never loses or duplicates a test file, for shard counts from 1 to 6 — a splitter that silently dropped files would leave every shard green while part of the suite stopped running. Also pins determinism (a rerun of a commit reproduces the same split), rough balance, that the excluded modules are exactly the ones owned by dedicated jobs, and the workflow contract that makes changing the shard count a one-line edit: `matrix.shard` must be a contiguous 1-based range, `--of` must come from `strategy.job-total` rather than a second literal, shards must pass `--cov-fail-under=0` (pytest-cov otherwise enforces pyproject's floor against a single shard's slice), and the combining job must depend on every shard and own the real floor. |
| `test_migrate_fork.py` | Tests for `scripts/migrate_fork.py`, the fork-migration assistant. The headline case walks every git-tracked file and asserts each occurrence of the upstream organization or repository name is claimed by exactly one classification rule, so a reference in an unanticipated shape (a `raw.githubusercontent.com` URL, a new package name) fails CI until a rule handles it deliberately. Also pins the preservation rules that keep links to other AWS Labs projects and the runtime-resolved `awslabs.*` MCP package names intact, the rewrite cases including the percent-encoded Pages URL in the coverage badge and the OIDC trust-policy subject, idempotency, `--repo-url` parsing and rejection of malformed or upstream targets, and that a dry run leaves the working tree untouched. |
| `test_manifest_api.py` | `gco/services/manifest_api.py` core route set — `/`, `/healthz`, `/readyz`, `/api/v1/health`, `/api/v1/status`, `/api/v1/manifests`, `/api/v1/manifests/validate`, plus `ManifestSubmissionAPIRequest` / `ResourceIdentifier` request shapes. An autouse fixture seeds the auth middleware token cache so the real `AuthenticationMiddleware` runs end-to-end against `TestClient` traffic — same code path as production. |
| `test_manifest_api_extended.py` | Async lifespan startup (wires a `ManifestProcessor` into the module global, propagates failures), the `submit_manifests` endpoint with a full `ResourceStatus` response, and other endpoint wiring driven by direct route-function calls after mutating module-level state so the handler logic is asserted in isolation. |
| `test_manifest_api_new_endpoints.py` | Endpoint functions split out into `gco/services/api_routes/` — pagination on `/api/v1/jobs`, per-job `/events`, `/pods`, `/metrics`, bulk delete, retry, plus the templates and webhooks surfaces. Driven via `TestClient` with a `mock_manifest_processor` that stubs every Kubernetes client used by the handlers. |
| `test_manifest_api_queue_endpoints.py` | Job-queue surface — `POST /api/v1/queue/jobs` (priority / labels / queued record return), listing, status retrieval, and the SQS consumer poll endpoint. Uses `mock_manifest_processor` plus a mocked `job_store` patched into the module global. |
| `test_manifest_api_coverage.py` | Edge-case branches the main suites don't hit — health endpoint returning 503 when `list_namespace` raises, job metrics when pod-metrics retrieval errors out, and similar error-path branches. Shares the autouse auth-cache seeding pattern with `test_manifest_api.py`. |
| `test_health_api.py` | `gco/services/health_api.py` — `create_app` factory, route registration for the full health surface (`/`, `/healthz`, `/readyz`, `/api/v1/health`, `/api/v1/metrics`, `/api/v1/status`), and endpoint handlers driven via `TestClient` against a mocked `HealthMonitor`. Auth middleware exercises real validation against a seeded token cache. |
| `test_health_api_extended.py` | Async lifespan context manager (successful startup and failure propagation), the stale-status refresh logic where the API reruns the health monitor when the cached `HealthStatus` is older than two minutes, and `cluster_id` / `region` attribute passthrough. |
| `test_inference_proxy.py` | Managed inference proxy security and streaming behavior — endpoint/region/namespace readiness checks, strict service-label and upstream-path validation, request/response header filtering, bounded HTTP timeouts, percent-encoded URL construction, transport-error mapping, and cancellation-safe upstream/client cleanup. |

### Service Tests

| File | Description |
|------|-------------|
| `test_manifest_processor.py` | `gco/services/manifest_processor.ManifestProcessor` core validation pipeline — structure checks, namespace allowlist, per-manifest CPU / memory / GPU caps, Pod Security Admission-style security context enforcement, image-registry allowlist, plus the apply / submission pipeline and CRUD helpers against the Kubernetes APIs. |
| `test_manifest_processor_extended.py` | Branches the base suite doesn't reach — CronJob container extraction with per-container validation (security context, image registry, GPU limits), manifest-level validation error wrapping into `ResourceStatus` entries, `list_jobs` namespace validation errors, and `_get_job_status` derivation for pending state. Hypothesis property tests for the registry-domain validator. |
| `test_manifest_security_validation.py` | Manifest security validation (hostNetwork, hostPID, hostIPC, hostPath, capabilities, init/ephemeral containers, kind allowlist, auth middleware) |
| `test_manifest_validation_preservation.py` | Validation preservation/regression tests — ensures existing behavior is not broken by security changes |
| `test_job_submission_validation.py` | Accelerator-toleration requirement (GPU/Neuron/EFA jobs must carry a matching toleration) — exercised against BOTH the REST `manifest_processor` and the SQS `queue_processor` so the SQS path is proven not to be a validation bypass |
| `test_security_policy_toggles.py` | Security policy toggle configuration tests — verifies each toggle can be individually enabled/disabled |
| `test_queue_processor.py` | SQS queue processor — manifest validation, security policy toggles (parity with `manifest_processor`), SA-token auto-mount injection, structural parity checks |
| `test_central_queue_worker.py` | Lease-fenced central queue worker behavior — claim processing and heartbeat renewal, stale owner/token rejection, retryable apply failures, terminal-state reconciliation, cancellation/deletion, and one-shot/continuous-loop orchestration against mocked job-store and Kubernetes clients. |
| `test_spot_price_gate.py` | The central-queue spot price gate (`gco/services/spot_price_gate.py`) — submission-time field validation, TTL-cached minimum-across-AZ price lookups, gate decisions (open/closed/unknown-price/malformed), observation-write throttling, the JobStore spot fields, and `process_queued_jobs_once` deferring gated jobs without consuming the apply budget or starving dispatchable work. |
| `test_queue_spot_gate_api.py` | The spot price gate on `POST /api/v1/queue/jobs` — canonical cap serialization to the store, 422 rejection of half-specified or malformed pairs before any write, gate fields folding into the idempotency request hash, and gate-free submissions hashing exactly as pre-gate deployments did. |
| `test_rbac_manifest.py` | RBAC manifest regression tests — verifies every runtime API path (pod logs, events, patch, metrics) has the Kubernetes RBAC grants the services need |
| `test_health_monitor.py` | `gco/services/health_monitor.HealthMonitor` core — construction against patched `kubernetes.config` (in-cluster preferred, kubeconfig fallback), the memory string parser (Ki/Mi/Gi/Ti), and the broader health-calculation surface. Uses a shared `mock_k8s_config` fixture so tests never touch a real cluster. |
| `test_health_monitor_extended.py` | Async internals the base suite doesn't reach — `_get_pod_counts` (active vs pending across namespaces, graceful degradation when the K8s API throws) and `_calculate_pending_requested_resources` summing CPU / memory / GPU requests from pending pods. Plus CPU / memory / GPU edge cases and node metrics caching. |
| `test_health_monitor_main.py` | The `main()` entry point's long-running loop — wakes on a fixed interval, calls `HealthMonitor.get_health_status`, logs a structured report, and feeds the webhook dispatcher. Each test runs a single iteration by making `asyncio.sleep` raise `KeyboardInterrupt` so both healthy and unhealthy paths can be covered. |
| `test_auth_middleware.py` | FastAPI backend HMAC middleware — unauthenticated-path allowlist (`/healthz`, `/readyz`, `/metrics`, `/api/v1/health`), exact method/target/body binding, timestamp freshness, nonce replay rejection, explicit `GCO_DEV_MODE` bypass, AWSCURRENT/AWSPENDING rotation, and bounded stale-key fallback. Autouse fixtures reset both key and replay caches. |
| `test_metrics_publisher.py` | `MetricsPublisher` — initialization with namespace / cluster_name / region, `put_metric` happy path (correct `PutMetricData` call shape), dimension merging so per-call dimensions land alongside cluster / region defaults, graceful `False` return when [CloudWatch](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/WhatIsCloudWatch.html) raises `ClientError`, and `put_metrics_batch` batching. |
| `test_template_store.py` | DynamoDB-backed stores in `gco/services/template_store.py` — `TemplateStore` (list / get / create / update / delete with pagination and duplicate-name guard), `WebhookStore` (namespace-scoped queries, event-filtered fanout, HMAC secret round-trip), and `JobStore` (submit, conditional claim, `update_job_status` with history append, priority-sorted queue retrieval, `ConditionalCheckFailedException` on cancel). |
| `test_helm_installer_handler.py` | `lambda/helm-installer/handler.py` — `run_helm` timeout handling, `_clear_stuck_release` preflight that recovers releases wedged in `pending-install` / `pending-upgrade` / `pending-rollback` state from a prior interrupted deploy, and `install_chart` integration against the preflight (never invokes the legacy `helm rollback --wait` path that hangs on stuck operators). Pre-uninstall custom-resource purges for finalizer-bearing charts (`CHART_CUSTOM_RESOURCE_API_GROUPS`: KEDA and Kueue): discovery-driven delete ordering (namespaced then cluster-scoped) while the finalizer-clearing controller is still live, purge-before-`helm uninstall` sequencing, and the self-healing path where a stalled `delete --wait` strips finalizers from survivors and retries once (the wedged-Terminating-CRD state that deadlocked live run sched241-350ffc7d's teardown) — including namespaced-instance patching and tolerance of resource types that vanish mid-strip. Mocks `subprocess.run` directly; does not invoke helm or kubectl. |
| `test_inference.py` | `InferenceManager` deploy / list / describe / scale / delete (CLI surface) and `InferenceMonitor` reconciliation loop (Deployment / internal Service lifecycle, complete classic and Mooncake teardown, status reporting, leader-election bootstrap). |
| `test_inference_extended.py` | `InferenceMonitor` long tail — `_try_acquire_lease` leader-election (renew, claim-when-empty, claim-when-None, not-leader), HPA creation, `_create_deployment` body matrix (model_source S3 sync init container, model_path volumes, env vars, command/args, zero-GPU, custom node selector / resource requests), the start/stop lifecycle, `main()` entry point, `InferenceManager.add_region` / `remove_region`, and `_resolve_image_for_region` per-region URI selection across happy / fallback / malformed-map / empty-value branches. |
| `test_inference_monitor_behavior.py` | Focused `InferenceMonitor` orchestration branches — endpoint reconciliation and stale cleanup, Mooncake service resolution, regional-scope and master-readiness gates, admin-key failures, materialization ordering, and region-status transitions with mocked Kubernetes clients and endpoint storage. |
| `test_inference_canary_monitor.py` | Canary deployment reconciliation — image change detection, replica scaling, readiness status used by authenticated proxy weighting, canary cleanup, plus the capacity-type node-selector matrix (spot / on-demand / unspecified) on `_create_deployment`. |
| `test_inference_health_watchdog.py` | Inference endpoint health watchdog — recovery-from-failure logic, status synchronisation between Kubernetes and DynamoDB, and restart-on-CrashLoopBackOff orchestration. |

### Model Tests

| File | Description |
|------|-------------|
| `test_models.py` | `gco/models` data classes — `ResourceThresholds` (boundary values, `-1` disable sentinel, out-of-range rejection), `ResourceUtilization`, `HealthStatus`, `KubernetesManifest`, `ManifestSubmissionRequest` / `Response`, and `ResourceStatus`. Pins the exact `__post_init__` error messages so callers can rely on them. |
| `test_models_extended.py` | Deeper validation paths — `RequestedResources` (rejects negative `cpu_vcpus` / `memory_gb` / `gpus` and non-numeric types, accepts zero), `ResourceUtilization` negative-gpu and over-100 rejection plus integer acceptance, and additional `KubernetesManifest` and `ResourceStatus` edge cases. |
| `test_config_loader.py` | `gco/config/config_loader.ConfigLoader` happy-path loading of every top-level field (`project_name`, `deployment_regions`, `kubernetes_version`, `resource_thresholds`, `global_accelerator`, `alb_config`, `manifest_processor`, `job_validation_policy`, `api_gateway`, `tags`) and `ConfigValidationError` on missing required fields. Drives a MockApp / MockNode pair surfacing a hand-crafted CDK context dict. |
| `test_config_loader_validation.py` | `ConfigLoader` validator defensive branches — no-op when no context is provided, missing required fields, empty regional list, too many regions, and other field-level constraints. Uses real `cdk.App` instances with `context=` dicts so the CDK Node wiring is part of the test rather than mocked out. |

### CDK Stack Tests

| File | Description |
|------|-------------|
| `test_cdk_stacks.py` | CDK stack synthesis smoke tests — synthesizes Global, [API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/welcome.html), Monitoring, and Regional stacks against a `MockConfigLoader` (no `cdk.json`, no boto3) and asserts the resulting [CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) templates contain the expected resources, outputs, and cross-stack dependencies. Catches construct-wiring breakage after refactors without needing a real AWS environment. |
| `test_regional_stack.py` | `gco/stacks/regional_stack.GCORegionalStack` — [VPC](https://docs.aws.amazon.com/vpc/latest/userguide/what-is-amazon-vpc.html), [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) cluster, [EFS](https://docs.aws.amazon.com/efs/latest/ug/whatisefs.html), optional FSx, kubectl-applier Lambda, helm-installer Lambda, MCP role, drift detection, and the NetworkPolicy / RBAC apply pipeline against a `MockConfigLoader`. Patches `DockerImageAsset` and the helm-installer builder so tests don't need a Docker daemon. The `MockConfigLoader` here is reused by sibling test files. |
| `test_regional_stack_feature_gap_coverage.py` | Feature-gap coverage for optional regional services and Helm workflows. It synthesizes real CDK constructs while mocking Docker assets and AWS lookups, then inspects their security, [IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction.html) scoping, convergence, and teardown lifecycle properties. |
| `test_monitoring_stack.py` | `gco/stacks/monitoring_stack.GCOMonitoringStack` — synthesizes the stack against `MockConfigLoader` plus mock Global, API Gateway, and regional stack objects. Asserts dashboard widgets, CloudWatch alarms (metric and composite), and [SNS](https://docs.aws.amazon.com/sns/latest/dg/welcome.html) topic shape on the resulting template — no AWS or Docker dependency. |
| `test_stacks.py` | `cli/stacks._detect_container_runtime` — `CDK_DOCKER` env override, docker selected when on PATH and `docker info` returns 0, finch fallback when docker isn't running, `None` when nothing is available, and `docker info` timeout handling. Autouse fixture resets the module-level runtime cache so tests run in any order. |
| `test_stacks_extended.py` | Extended `cli/stacks.StackManager` — `get_outputs` / `get_stack_status` against mocked boto3 CloudFormation (success, missing outputs, stack-not-found, `ClientError`), deploy / destroy argv shape with `--all` / `--outputs-file` / `--parameters` / `--tags` / `CDK_DOCKER`, `_get_deploy_region` mapping for `gco-global` / `gco-api-gateway` / `gco-monitoring` / regional stacks, and the `is_bootstrapped` + `ensure_bootstrapped` pair gating `cdk deploy` on a live `CDKToolkit` stack. |
| `test_stacks_extended_coverage.py` | Long tail of `cli/stacks.py` destroy-flow helpers — `_read_images_config`, `_build_image_registry_inventory`, `_image_registry_destroy_preflight` (every refusal / confirmation branch including TTY prompt, EOF, and `force=True`), `_stack_exists_in_cloudformation`, `_cloudformation_delete_stack`, `_get_destroy_region`, the analytics-toggle wrappers, `_api_gateway_imports_from_analytics`, `_cleanup_backup_vault`, `_cleanup_eks_security_groups`, and the `_start_eks_sg_watchdog` background thread. Plus the `gco stacks fsx / valkey / aurora` CLI subcommand handlers — happy paths, every validation rejection (FSx storage capacity floor, Aurora `min_acu < 0`, `max_acu < 1`, `max_acu < min_acu`), and `update_*_config` exception branches. Every AWS call is mocked. |
| `test_stacks_destroy_hardening.py` | Deploy/destroy hardening in `cli/stacks.py`. State-aware deploy verification: a cdk-success deploy is reconciled against the live CloudFormation status, so a silent `*_ROLLBACK_COMPLETE` reads as failure while `CREATE_COMPLETE` / `UPDATE_COMPLETE` pass, an unknown `None` leaves cdk's verdict intact, and `--all` skips the per-stack check. Orphaned-ENI sweep: `_classify_orphaned_eni` buckets by `InterfaceType` / description ([Global Accelerator](https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html) / ELB / EKS / other), `_summarize_orphaned_enis` counts per category and deletes only detached, non-service-managed interfaces (service-managed GA/ELB ENIs are reported, not fought), `_print_orphaned_eni_summary` renders the friendly report, and the public `cleanup_orphaned_network_interfaces` iterates regional stacks only. Every AWS call is mocked. |

### Diagram Generator Tests

| File | Description |
|------|-------------|
| `test_code_diagrams_generator.py` | `diagrams/code_diagrams/generate.py` — `Target` catalogue well-formedness (every source file + function resolves), `_output_stem_for` path math (including the dotted-function edge case that `Path.with_suffix` would mangle), idempotent insertion of the `# <pyflowchart-code-diagram>` marker block (handles module docstrings, `from __future__` imports, no-docstring files, multi-target collapse), and the grouped hierarchical rendering of `code_diagrams/README.md`. |

### Analytics Environment Tests

Tests for the optional `analytics_environment` ([SageMaker](https://docs.aws.amazon.com/sagemaker/latest/dg/whatis.html) Studio + EMR
Serverless + [Cognito](https://docs.aws.amazon.com/cognito/latest/developerguide/what-is-amazon-cognito.html)) and the always-on `Cluster_Shared_Bucket` in
`GCOGlobalStack`. The analytics stack is only synthesized when
`analytics_environment.enabled=true` in `cdk.json`; off-by-default
assertions live in `test_analytics_stack.py`.

| File | Description |
|------|-------------|
| `test_analytics_stack.py` | Core CDK template assertions for `GCOAnalyticsStack` — SageMaker Studio domain, [EMR Serverless](https://docs.aws.amazon.com/emr/latest/EMR-Serverless-UserGuide/emr-serverless.html) Spark application, Cognito user pool + client + hosted domain, `Analytics_KMS_Key`, private-isolated VPC + nine interface endpoints + S3 gateway endpoint, `Studio_EFS` + dedicated SG, `SageMaker_Execution_Role` grants (including the cross-region `Cluster_Shared_Bucket` policy resolved via `AwsCustomResource`), and IAM / cdk-nag compliance. Also asserts the `canvas` sub-toggle correctly attaches `AmazonSageMakerCanvasFullAccess` and injects a `DefaultUserSettings.CanvasAppSettings` block when on, omitting both when off. |
| `test_analytics_bucket_isolation_property.py` | Hypothesis property test: across randomized cdk.json overlays the regional job-pod role's S3 policy only references `arn:aws:s3:::gco-cluster-shared-*` ARNs and never touches `gco-analytics-studio-*` |
| `test_analytics_configmap_property.py` | Hypothesis property test for the biconditional between `analytics_environment.enabled` and the presence of the SageMaker execution role's RW grant on `Cluster_Shared_Bucket` — enabling the toggle must materialize the grant, disabling it must remove both the role and the grant |
| `test_analytics_roundtrip_property.py` | Hypothesis property test that the two-bit `(enabled, hyperpod_enabled)` toggle state can be recovered from the synthesized CloudFormation templates alone (derive the toggles back from resource presence/absence and assert equality with the input config) |
| `test_analytics_cluster_shared_configmap_property.py` | Hypothesis property test that the `gco-cluster-shared-bucket` ConfigMap is present in every regional cluster regardless of the `analytics_environment.enabled` toggle — the cluster-shared bucket is always-on |
| `test_analytics_cmd.py` | CLI tests for `gco analytics enable/disable/status/users/studio login/doctor` including the toggle round-trip Hypothesis property, the `--hyperpod` and `--canvas` sub-toggle flags (individually and combined), a `disable` test that proves `canvas.enabled=true` survives a disable/enable cycle, and a `cdk synth` integration test that exercises the full analytics pipeline from CLI toggle to template |
| `test_analytics_cmd_branches.py` | Edge-case coverage for the analytics CLI command branches (error paths, missing-config fallbacks, mixed-toggle scenarios) |
| `test_analytics_user_mgmt.py` | Tests for the stdlib SRP implementation and Cognito auto-discovery helpers in `cli/analytics_user_mgmt.py` (used by `gco analytics studio login`) |
| `test_analytics_examples_validation.py` | Validates the three new analytics example manifests (notebook-hosted SageMaker job, EMR Serverless Spark job, cluster-shared-bucket read/write job) pass `ManifestProcessor.validate_manifest` against the trusted-registry security config |
| `test_api_gateway_analytics_config.py` | Tests for the `AnalyticsApiConfig` mutator and the `/studio/*` route wiring it attaches to the existing API Gateway when analytics is enabled |
| `test_cluster_shared_bucket.py` | Tests for the always-on `Cluster_Shared_Bucket` (name, [KMS](https://docs.aws.amazon.com/kms/latest/developerguide/overview.html) encryption, versioning, public-access-block, `DenyInsecureTransport` policy) + its KMS key + the `/gco/cluster-shared-bucket/{name,arn,region}` [SSM](https://docs.aws.amazon.com/systems-manager/latest/userguide/what-is-systems-manager.html) parameters written by `GCOGlobalStack` |
| `test_presigned_url_lambda.py` | Tests for `lambda/analytics-presigned-url/handler.py` — happy path (`CreatePresignedDomainUrl` success), error-token mapping (auth, profile-missing, quota, throttle), and a Hypothesis property test for the response-shape invariants |

#### Analytics Test Helpers

The `tests/_analytics_*.py` modules are shared helpers, not tests. Pytest
does not pick them up as test files but the analytics tests above import
them for strategy construction, overlay generation, template parsing,
and inverse-derivation logic.

| Helper | Purpose |
|--------|---------|
| `_analytics_strategies.py` | Hypothesis strategies for randomized `analytics_environment` cdk.json overlays (enabled/disabled, hyperpod on/off, removal-policy choices, cognito prefix overrides) |
| `_analytics_cdk_overlays.py` | Materializes a strategy draw into a real cdk.json context dict that `ConfigLoader` can consume; kept separate from the strategies so the same overlay shape can be written as a fixture without running Hypothesis |
| `_analytics_template_inspectors.py` | Small library of template-walk helpers (`find_sagemaker_role`, `find_studio_bucket`, `collect_role_statements`, `extract_cluster_shared_grant`) used across the analytics stack assertions; consolidates the boilerplate that earlier iterations inlined into every test class |
| `_analytics_derivations.py` | The inverse-direction helpers — given a set of synthesized templates, derive back the `(enabled, hyperpod_enabled)` toggle pair used by `test_analytics_roundtrip_property.py` |

### CDK Configuration Matrix

The cdk.json configuration matrix — the set of overlays users can pick from (multi-region, FSx on/off, all feature toggles, resource threshold values, helm chart enable/disable, etc.) — lives in `tests/_cdk_config_matrix.CONFIGS` and is the single source of truth shared between two test surfaces:

1. **`tests/test_cdk_synthesis_matrix.py`** builds the full CDK app in-process against every entry in `CONFIGS` and runs `app.synth()` serially. Serial execution avoids shared CDK asset-staging races while catching synth-time breakage, hardcoded regions, missing conditional guards, and broken feature-flag interactions. Run locally or in CI:

    ```bash
    pytest tests/test_cdk_synthesis_matrix.py
    ```

2. **`tests/test_nag_compliance.py`** runs the full CDK app in-process against the IAM-relevant subset (`NAG_CONFIGS`) and asserts zero unsuppressed cdk-nag findings across five rule packs (AwsSolutions, HIPAA Security, NIST 800-53 R5, PCI DSS 3.2.1, Serverless). This is the gate that prevents a user from hitting a cdk-nag error at `cdk deploy` time on a config CI hasn't already validated. See [cdk-nag Compliance Testing](#cdk-nag-compliance-testing) below for details.

Sharing the matrix is deliberate — divergence between the two lists is how we ended up with an `AwsSolutions-IAM5` error on a user's `gco-us-east-1` deploy that neither tool had exercised. Adding a new cdk.json knob means adding one entry to `tests/_cdk_config_matrix.py` and both tests pick it up.

### cdk-nag Compliance Testing

The cdk-nag rule packs that block production deploys (AwsSolutions-IAM5 wildcards, Serverless-LambdaTracing, etc.) are enforced by `tests/test_nag_compliance.py` across every `cdk.json` configuration in the shared matrix. If the test is green, every config the user can pick has been verified to produce zero unsuppressed findings.

**Why this exists:** `cdk synth --quiet` exits 0 even when unsuppressed findings exist, and we shipped a regional-stack `AwsSolutions-IAM5` finding on the auth-secret ARN to v0.1.0 that only surfaced when a user ran `cdk deploy gco-us-east-1` for the first time. The CI matrix at that point only ran `cdk synth --quiet` and called exit 0 success — the finding slipped through.

**How it works:**

- cdk-nag v3 runs as a CDK **policy-validation plugin** (`IPolicyValidationPlugin`) rather than an `IAspect`. `app.synth()` writes every unacknowledged finding to `validation-report.json` in the cloud assembly directory (and does **not** raise on findings), so the test reads that report to assert on findings programmatically.
- `tests/test_nag_compliance.py` parameterizes over the IAM-relevant `NAG_CONFIGS` subset, builds the complete CDK app (Global, API Gateway, Regional, Monitoring) the same way `app.py` does, registers all five rule packs via `cdk.Validations.of(app).add_plugins(...)`, calls `app.synth()`, and asserts the report's finding list is empty.
- CI fans the IAM-relevant configs out through `unit:cdk:nag-compliance`, one GitHub Actions runner per `NAG_CONFIGS` entry; each pytest invocation runs serially.

**Scope discipline for new suppressions:**

Any `acknowledge_nag_findings` call this test forces you to add should:

- Scope via `applies_to` to the specific finding detail — an exact string such as a literal ARN or `Resource::<LogicalId.Arn>/*`. cdk-nag v3 matches details verbatim (there is no regex), so it must match exactly, including any synthesis-time logical-id hash. Never use `applies_to=["Resource::*"]` or `applies_to=["Action::*"]` unless the AWS API genuinely offers no resource-level scoping — blanket bypasses defeat the whole test.
- Include a `reason` string that explains WHY the wildcard is necessary (cross-stack token, AWS-managed policy, [Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html) suffix, etc.) and links to any relevant AWS documentation.
- Live as close to the construct that created the finding as possible. `acknowledge_nag_findings(construct, ...)` records the acknowledgment on that construct's node and cdk-nag walks the ancestor tree, so scope it to the smallest construct that owns the finding rather than the whole stack — that fails closed when the construct is renamed.

**Debugging findings locally:**

If the test fails, run `scripts/dump_nag_findings.py` for a compact, per-finding report grouped by rule + path + config name. It uses the same test harness and gives cleaner output than pytest's `AssertionError` repr.

```bash
python3 scripts/dump_nag_findings.py
```

### Fresh Install Verification

The `test:fresh-install` CI job does a clean `pip install -e .` and verifies all critical imports work — including `cdk-nag`, `aws_cdk.aws_eks_v2`, the CLI entry point, and the CDK stack classes. This catches missing or mismatched dependencies in `pyproject.toml`.

### Lambda Build Verification

The Lambda build directory (`lambda/kubectl-applier-simple-build/`) is auto-created by `StackManager` during deploy. In CI, this is validated at multiple levels:

- `integration:lambda` — verifies all Lambda handler modules import correctly
- `test:cdk-config-matrix` — builds the Lambda package in `before_script` and runs `cdk synth` against it (synth fails if the build dir is missing or incomplete)
- `test_stacks.py::TestStackManagerSyncLambdaSources` — unit tests that `_sync_lambda_sources` auto-creates the build directory when missing

### Lambda Handler Import Helper

Lambda handler modules live under `lambda/<name>/handler.py` and aren't on Python's normal `sys.path`. Early tests loaded them with the pattern:

```python
sys.path.insert(0, "lambda/foo")
sys.modules.pop("handler", None)
import handler
```

That works in isolation but leaks across tests. Pytest runs the whole suite in one Python process, so the first test to `import handler` wins `sys.modules['handler']`. Any later test that forgets to pop — or runs after a fixture that populated it with a different Lambda's module — silently gets the wrong handler. This collision broke CI on the v0.1.0 launch when two test files' `handler` imports collided.

**The helper:** `tests/_lambda_imports.py` exposes `load_lambda_module(lambda_dir, module_name="handler", *, shared_dirs=())`. It loads the target module under a unique, namespace-safe name (e.g. `_gco_lambda_secret_rotation_handler`) via `importlib.util.spec_from_file_location`, so registrations cannot collide across tests.

Features:

- **Unique `sys.modules` name** per `(lambda_dir, module_name)` — zero collision risk.
- **Fresh load on every call** — matches the semantics of the old `sys.modules.pop + import` pattern. Fixtures that wrap the load in `patch("boto3.client")` see the mock applied on every invocation, which is required by handlers like `secret-rotation/handler.py` that create AWS clients at module-import time.
- **`shared_dirs`** — for handlers that `import` from a sibling lambda dir (e.g. `lambda/api-gateway-proxy/handler.py` doing `from proxy_utils import ...`), `shared_dirs=["proxy-shared"]` pushes that dir onto `sys.path` for the duration of the load only.
- **Collateral cleanup** — when `shared_dirs` is non-empty, any new entries the load added to `sys.modules` (e.g. a bare `proxy_utils` entry) are removed afterward, so the next fixture gets a truly fresh re-import under its own mocks. Standalone loads (no `shared_dirs`) leave `sys.modules` untouched so third-party globals like `boto3` aren't disturbed.
- **Input validation** — rejects path traversal in `lambda_dir` and `shared_dirs`, raises a clean `ValueError` if the target file doesn't exist.

Typical usage in a fixture:

```python
from tests._lambda_imports import load_lambda_module


@pytest.fixture
def rotation_module():
    with patch("boto3.client") as mock_client:
        handler = load_lambda_module("secret-rotation")
        yield handler, mock_client
```

Handler that depends on a shared utility module:

```python
proxy_utils = load_lambda_module(
    "proxy-shared",
    "proxy_utils",
    shared_dirs=["tls-shared"],
)
proxy_utils._cached_secret = None
handler = load_lambda_module("api-gateway-proxy", shared_dirs=["proxy-shared"])
```

Every Lambda handler test in this repo now loads via this helper. The legacy `sys.path.insert + import handler` pattern is gone, and `tests/test_lambda_imports.py` pins the helper's contract against regression.

### Other Tests

| File | Description |
|------|-------------|
| `test_aws_client.py` | `cli/aws_client.GCOAWSClient` — `RegionalStack` and `ApiEndpoint` dataclasses, TTL-based endpoint and stack discovery (with force-refresh and invalidation), SigV4-signed request plumbing, and every higher-level helper: regional job CRUD (list / get / logs / events / pods / metrics / retry / delete / bulk-delete), global aggregation endpoints, [ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html) endpoint discovery, retry / backoff on transient errors, and the regional-vs-API-Gateway routing toggle. |
| `test_aws_ssm.py` | `gco/services/aws_ssm.py` — the four free functions (`get_ssm_parameter`, `get_ssm_parameter_optional`, `check_ssm_parameter`, `put_ssm_parameter`) the `cli/`, `gco_mcp/`, and `gco/services/` callsites all share. moto-backed happy paths plus the contract differences across the three "missing parameter" semantics: `get_ssm_parameter` propagates `ParameterNotFound` verbatim, `get_ssm_parameter_optional` returns `None` only on `ParameterNotFound` (every other `ClientError` propagates), and `check_ssm_parameter` flattens every failure including missing into `(False, str(exc))`. Also pins `put_ssm_parameter`'s `overwrite=False` rejection of existing names, the `parameter_type` thread-through to `ssm:PutParameter`, and the historical `(region, name)` positional argument order on the `cli.analytics_user_mgmt.check_ssm_parameter` alias so a future delegation refactor can't silently flip it. |
| `test_files.py` | `cli/files.py` baseline — `FileSystemInfo` and `FileInfo` dataclasses plus `FileSystemClient` initialization with `get_config` and `get_aws_client` patched out. The end-to-end EFS / FSx discovery and DataSync transfer paths live in `test_files_extended.py`. |
| `test_files_extended.py` | Extended `FileSystemClient` — `get_file_systems` against `RegionalStack` instances exposing both EFS and FSx file system IDs, plus error handling in `_get_efs_info` and `_get_fsx_info` when the AWS APIs raise `ClientError`. Pairs with `test_files.py` which covers the dataclass layer. |
| `test_jobs.py` | `cli/jobs.JobManager` — `JobInfo` dataclass (`is_complete` derivation across running / succeeded / failed / pending states, `duration_seconds` math with / without `start_time` and `completion_time`), manifest loading from files and directories, submission with namespace fallback and label injection, list / get / logs / delete, `wait_for_job`, and `_extract_image_refs` extraction from the parsed Job spec. |
| `test_nodepools.py` | `cli/nodepools.py` Karpenter NodePool utilities — `NodePoolInfo` dataclass, [ODCR](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-reservations.html) NodePool manifest generation (instance types, capacity reservation wiring, vCPU lookup via [EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/concepts.html) API with `DEFAULT_VCPUS_PER_NODE` fallback), CPU limit calculation, EKS token generation for kubectl auth, Kubernetes client configuration, and list / describe operations. boto3-mocked, no real AWS. |
| `test_output.py` | `cli/output.py` table / JSON / YAML formatter — `_serialize_value` helper (datetime, dataclass, dict, list, primitive passthrough), `OutputFormatter` initialization and format selection (`set_format` validation), and the JSON-specific paths. Extended table-rendering edge cases (price-column detection, string truncation, column filtering) live in `test_output_extended.py`. |
| `test_deployment_regions.py` | `deployment_regions` configuration block — `ConfigLoader` enforces it's required and that sub-fields (`regional`, `api_gateway`, `global`, `monitoring`) are loaded correctly from CDK context. Uses MockApp / MockNode stand-ins and a shared `base_context` fixture so only `deployment_regions` is exercised. |
| `test_cross_region_aggregator.py` | `lambda/cross-region-aggregator/handler.py` — deterministic project-scoped regional CloudFormation stack discovery, strict `RegionalApiEndpoint` validation, SigV4-signed AWS-managed HTTPS requests, bounded endpoint caching, and the `aggregate_*` helpers that merge job lists, health status, metrics, and bulk-delete results across every required region. Loaded via `tests._lambda_imports.load_lambda_module` for `sys.modules` isolation. |
| `test_integration.py` | Cross-cutting static-analysis-style checks — every Kubernetes manifest under `lambda/kubectl-applier-simple/` has the required shape for its kind, every example job under `examples/` pulls images only from trusted registries, every Lambda handler imports cleanly with a `handler(event, context)` signature, and CDK synthesis produces well-formed CloudFormation. Belt-and-braces smoke test for schema drift across manifests, examples, and stacks. |
| `test_cdk_config_consumption.py` | Guard against dead config in `cdk.json`. Auto-discovers every dict block under `context` and asserts each user-config key is referenced as a dict-literal, attribute access, or `.get()` call somewhere under `gco/` or `lambda/`. Documentation siblings (top-level and nested `_comment*` keys) and CDK feature flags (`@aws-cdk*`) are filtered out. Plus a `tags`-block guard that rejects any documentation-style key (`_comment` etc.) — the dict is iterated by `app.py` and would emit literal AWS tags. A discovery smoke test asserts the canonical block set is still picked up if `cdk.json` gets restructured. |
| `test_sqs_integration.py` | `JobManager.submit_job_sqs` end-to-end against mocked CloudFormation and SQS clients — looks up `JobQueueUrl` from the regional stack outputs, sends an SQS message with manifest payload and priority, and returns the queued-record dict. Covers missing-stack, missing-output, and `send_message` failure paths. |
| `test_lambda_imports.py` | Contract tests for the `tests/_lambda_imports.py` helper — unique module naming, fresh-load semantics, collateral module cleanup when `shared_dirs` is used, input validation against path traversal |
| `test_lambda_image_lookup.py` | `lambda/image-lookup/handler.py` [ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html) adopt-or-create custom resource — `_describe_repository` (typed `RepositoryNotFoundException`, generic `ClientError` translation, error propagation, empty-list short-circuit), `_create_repository` (project-standard `MUTABLE` + `scanOnPush=true`), lifecycle policy applier (no-op on empty / whitespace / `None`, JSON-validation failure), `_has_retain_tag`, `_delete_all_images` (paginated digest collection, 100-id chunked deletes, missing-digest skip, empty-repo no-op), every Create / Update / Delete `lambda_handler` branch, and dispatcher errors (unsupported / missing `RequestType`, `None` `ResourceProperties`). |
| `test_nag_compliance.py` | End-to-end cdk-nag regression — synthesizes the full CDK app (Global, API Gateway, Regional, Monitoring) against each entry in `tests/_cdk_config_matrix.NAG_CONFIGS` and asserts zero unsuppressed findings across all five rule packs (AwsSolutions, HIPAA Security, NIST 800-53 R5, PCI DSS 3.2.1, Serverless). See [cdk-nag Compliance Testing](#cdk-nag-compliance-testing). |
| `test_project_name_runtime_paths.py` | Issue #139 runtime + CLI project-scoping (no CDK synth). Asserts every runtime service/Lambda builds project-scoped discovery or storage names, and that the inference monitor's locally duplicated regional-shared-bucket namespace stays in lockstep with `gco.stacks.constants`: `inference_monitor` prefix, `ga-registration` `/<project>/alb-hostname-<region>` store/delete, cross-region aggregator discovery of `<project>-regional-api-<region>` and its `RegionalApiEndpoint` CloudFormation output, and kubectl-applier / helm-installer `/<project>/addons/<region>/…` status writers. Also asserts the ECR image namespace is project-scoped: `cli.images.ImageManager` repositories live under `<project>/`, and `cli._image_mirror.read_mirror_config` defaults to `<project>/dockerhub`. |
| `test_project_name_scoping.py` | Issue #139 project-name isolation. `ConfigLoader` `project_name` format validation; a backward-compat check that `project_name="gco"` still renders the pre-#139 physical names; a **classification guard** that fails if a full multi-region + analytics synth produces any named resource type not explicitly classified as collision-prone or documented-safe (so coverage can't silently rot); and parametrized full-app synths asserting every collision-prone physical name (S3 buckets, CFN exports, [WAF](https://docs.aws.amazon.com/waf/latest/developerguide/what-is-aws-waf.html), auth secret, log groups, SSM params, DynamoDB tables, Lambda functions, SQS queues, EKS clusters, KMS aliases, SNS topics, SageMaker domain, EMR app, Cognito domain, composite alarms, backup vaults, …) embeds the project name and that two deployments share **zero** collision-prone names — both in the **same** regions and in **different** regions, plus a hyphen-variant edge case and a single-deployment multi-region / co-located self-collision check. Runs in the dedicated `unit:cdk:project-name-scoping` CI job. |

### CI and Supply-Chain Tests

| File | Description |
|------|-------------|
| `test_ci_runtime_verifiers.py` | Behavioral contracts for `.github/scripts/verify_lambda_imports.py` and `verify_container_tool_versions.py`: discovers and imports all 14 tracked Python Lambda handlers in bounded isolated subprocesses, parses every reviewed Dockerfile tool pin, accepts exact runtime matches for the development and helm-installer images, and rejects valid-but-different versions. |
| `test_structured_logging_sanitizer.py` | `gco/services/structured_logging.py::sanitize_log_value` — the single log-injection (CWE-117) barrier for untrusted logged values. Pins the neutralization contract: CR/LF become literal `\r`/`\n` sequences, Unicode line separators (NEL, U+2028, U+2029) and all other control characters (ANSI escapes, NUL, DEL, TAB) become `?`, printable text including non-ASCII passes through unchanged, non-strings are coerced, and no output ever contains a raw control character. |
| `test_supply_chain_integrity.py` | Offline provenance and workflow-policy guards: binds downloaded releases to committed checksums, rejects mutable remote installers, requires authenticated kind/Finch inputs, keeps new pins in monthly drift inventory, enforces fail-closed dependency-scan reporting, pins the official AWS CLI image by digest, preserves atomic release publication and native amd64/arm64 image coverage, and verifies the runtime-verifier and bounded scanner-prepull wiring. |

### Script Tests

Tests for helper scripts under `scripts/`. All of them exercise their
target script's public helpers or CLI argparse dispatch — none of them
actually deploy anything, hit AWS, or spawn long-running subprocesses.

| File | Script under test | What it covers |
|------|-------------------|----------------|
| `test_bump_version.py` | `scripts/bump_version.py` | SemVer reads the source of truth from `VERSION` and keeps `gco/_version.py` and `cli/__init__.py` in sync — current-version reading, patch / minor / major bumps with correct field resets, dry-run mode, invalid-input error paths, and `main()` argparse dispatch. Uses a `tmp_path` fixture that patches the module's path constants so real repo files are never touched. |
| `test_webhook_delivery_script.py` | `scripts/test_webhook_delivery.py` | The script's own helpers and argparse `main()` without spinning up a real dispatcher or hitting the network — `WebhookHandler.do_POST` capture and 200 response, silenced `log_message`, `start_local_server` port binding + daemon thread + clean shutdown, `create_mock_job` fixture shape, and the local-server vs. external-URL `main()` branches. |
| `test_cdk_synthesis_matrix.py` | `tests/_cdk_config_matrix.CONFIGS` | Full-app `app.synth()` validation parameterised over every entry in the shared matrix, run serially to avoid shared CDK asset-staging races. Pairs with `test_nag_compliance.py` which fans the IAM-relevant subset across CI runners for cdk-nag. |
| `test_dump_nag_findings_script.py` | `scripts/dump_nag_findings.py` | `run_config` threads context overrides through to `_build_app_with_logger`, invokes `app.synth()` while the Docker-asset mock is live, returns `logger.findings` verbatim. `main()` aggregates by `(rule_id, resource_path, finding_id)`, deduplicates across configs, emits per-config and summary counts, exits 0 on clean and 1 otherwise. |
| `test_image_mirror.py` | `cli/_image_mirror.py` + `gco images mirror` (`cli/commands/images_cmd.py`) | The general image-mirror core and its CLI command without Docker or AWS — `_volcano_source_refs`/`collect_source_refs` read the default components + pinned tag from a charts.yaml-shaped config (tag-override precedence, error paths), `parse_source_ref`/`plan_from_sources` produce source/dest refs that line up with the consumer's `image_registry` override, `resolve_copy_strategy` prefers buildx then Finch `--all-platforms` then skopeo, `_copy_commands` shells out to a manifest-list-preserving copy (asserts it is *not* the arch-dropping pull/tag/push), `ensure_repository`/`tag_exists` cover idempotent create + skip-if-mirrored, `plan_mirror`/`mirror_status` resolve the destination registry/repos and per-image ECR presence read-only (no copy), and `gco images mirror --dry-run` makes no STS/ECR/docker calls. |
| `test_example_job_validation.py` | `scripts/example_job_validation/` | The example-manifest validation harness, offline half. Runs the harness's own static checks as CI tests — every file under `examples/` must parse, clear the exact transport gates its documented submission path enforces (kind/GVK allowlist, trusted image sources), target a provisioned workload namespace, and stay in three-way symmetry with the spec registry and the `gco_mcp` `EXAMPLE_METADATA` catalog — so a PR that breaks an example's documented contract fails in CI. Also pins the harness plumbing with no AWS access: spec enumerations, selection-derived helm/feature enablement in `ExampleRunSettings` (identity + extra CDK context), the action registry order, the runner's derived deploy-dependent guard (including the `opencost` gap the old hardcoded set missed), disclosed manifest mutations (gated-model substitution incl. `REMOVE_VALUE` semantics, results stay valid YAML), and the `--static-only`/selection argument surface. |
| `test_live_release_validation.py` | `scripts/live_release_validation/` | Offline contracts for the local-only release-validation harness: exact identity and resume checks, private checkpoint/report writes, action ordering and partial-report semantics, destructive-resource ownership gates, smoke-manifest security, contributor documentation, and the requirement to share reports through manual PR upload only. All AWS, Kubernetes, subprocess, and time-dependent boundaries are stubbed. Replaces harness helpers through `tests/_live_validation_patching.patch_live_validation_helper`, which binds one shared mock into every module that references the name so a stale patch target fails loudly instead of silently running production code. |
| `test_live_release_validation_structure.py` | `scripts/live_release_validation/` (architecture) | Architecture guards that keep the harness from collapsing back into one 6,900-line module: every registry action has exactly one owning module and is exported from `actions/__init__.py`; the registry agrees with the contract table in `docs/LIVE_RELEASE_VALIDATION.md` on action names, order, and dependencies; dependencies are declared in an executable order; layer imports flow one way (`actions` → `checks`/`cleanup`/`ownership` → root) so the graph cannot go cyclic and only `runner.py`/`__main__.py` import `registry`; no module exceeds the review-size ceiling; and every module, action handler, and README guidance section stays documented. |
| `test_live_validation_emulator.py` | `scripts/live_release_validation/emulator.py` + `runner.require_local_execution` | The single seam through which the local-only harness may run in CI, and only against a proven AWS emulator. Pins every fail-closed branch with mocked STS: https endpoints refused, non-allowlisted hosts refused, split `AWS_ENDPOINT_URL` refused, realistic (non-12-digit) credentials refused, identity-echo mismatch refused, echo match accepted; and `require_local_execution` staying a no-op outside CI, refusing CI without the emulator env, and routing CI-with-emulator through the verification. The happy path against a real emulator lives in `test_floci_live_validation_e2e.py`. |
| `test_live_validation_opencost.py` | `scripts/live_release_validation/actions/opencost.py` + `checks/opencost.py` | The cost monitoring validation action: the cdk.json configuration short-circuit, the bounded `/api/v1/cost/status` readiness poll (healthy-with-data proceeds; unhealthy or data-less OpenCost fails with a diagnostic), region-identity verification, the ad-hoc report requirement (S3 key present, non-zero rows), the S3 object existence/size proof, per-region evidence checkpointing, and registry wiring. |

### MCP Server Tests

The MCP server has a layered test surface — unit tests for individual modules, protocol-level integration tests that exercise the FastMCP Client, transform-behaviour tests for the catalog-replacement modes, and gating tests for every feature flag. Running the full set takes about a minute and gives end-to-end confidence in the tool surface without needing AWS credentials.

| File | Description |
|------|-------------|
| `test_mcp_server.py` | Core unit tests — `_run_cli` wrapper, tool registration, per-tool argv translation (every public tool), resource registration counts, resource content reading. The single largest MCP test file. |
| `test_mcp_audit.py` | Audit logging — argument sanitization (redaction, truncation), `@audit_logged` decorator (sync + async dispatch), startup log fields (`tool_search`, `code_mode_experimental`, `all_tools_enabled`), `request_id` / `client_id` / `task_id` capture from FastMCP Context, and `client_messages` / `elicitations` capture via the `AuditCaptureMiddleware`. Hypothesis property tests for sanitization completeness. |
| `test_mcp_resources_new.py` | Tests for `tests://`, `config://`, and `docs://gco/examples/guide` resource groups, enhanced example metadata, module structure verification. |
| `test_mcp_integration.py` | End-to-end MCP protocol tests via FastMCP `Client` — tool discovery, tool call round trips, resource reading, schema validation, stdio subprocess transport. The `test_list_tools_returns_all_registered_tools` test asserts against `mcp._list_tools()` (the underlying registry) rather than the public `client.list_tools()` so the BM25 catalog-replacement transform doesn't hide real tools from the assertion. |
| `test_mcp_transforms.py` | FastMCP transform behaviour — `ResourcesAsTools` round-trip, BM25 / Regex / Code Mode / `off` selection via `GCO_MCP_TOOL_SEARCH` (default + unknown-value fallback), always-visible entry-point set survives catalog replacement, Code Mode discovery-tool order (`[GetTags, Search, GetSchemas]`), `MontySandboxProvider` limits via the duration / memory env knobs (defaults + overrides + invalid-value fallback), and startup audit log carries `code_mode_experimental: true` under Code Mode. |
| `test_mcp_feature_flags.py` | Hypothesis truth-table tests for `gco_mcp/feature_flags.py::is_enabled` — every flag obeys the `"true"` (case-insensitive, stripped) rule, the umbrella `GCO_ENABLE_ALL_TOOLS` overrides per-flag values, `ALL_FLAGS` enumerates only per-tool flags (umbrella stays out so iterating doesn't accidentally re-enable everything). |
| `test_mcp_examples_index.py` | Example-manifest discovery — `EXAMPLE_METADATA` enrichment (`keywords` / `instance_types` / `use_cases` / `related`), `find_examples` tool ranking, `docs://gco/examples/by-category/{category}` and `docs://gco/examples/by-use-case/{use_case}` resource paths, Hypothesis property tests covering keyword-match recall and `related` reference closure (every name in any `related` list resolves to a valid example key). |
| `test_mcp_docs_index.py` | Documentation discovery across all three catalogs: `DOC_METADATA` for `docs/*.md`, `ROOT_DOC_METADATA` for registered project-root guidance such as `TENETS.md`, and `PACKAGE_DOC_METADATA` for package READMEs. Enforces file/catalog symmetry where applicable, pairwise-disjoint keys, related-reference closure, `find_docs` recall and exact resource URIs, root-document content/TOC registration, and topic/related resource paths. |
| `test_mcp_adr_resource.py` | Directory-driven Architecture Decision Record resources — the `docs://gco/adr/index` listing and the `docs://gco/adr/{id}` per-record resource (resolution by four-digit id / filename stem / the `README` and `template` guides, the not-found message, and path-traversal rejection), `_parse_adr` title-and-status extraction, the directory-driven invariant that any `NNNN-*.md` record appears in the index with no per-file registration, and the advertisement of the ADR resources from the top-level `docs://gco/index`. |
| `test_mcp_tasks.py` | FastMCP background-task tooling — `_run_long_task` lifecycle (drain stdout / stderr, increment progress on CFN `*_COMPLETE` lines), cancellation with `SIGTERM` → 10s grace → `SIGKILL`, partial-CloudFormation-state disclaimer in cancelled stack ops, and path-traversal rejection in argv. Plus the deploy / destroy gating tests (`deploy_stack` / `deploy_all` / `bootstrap_cdk` / `destroy_stack` / `destroy_all` absent without their feature flags), argv kick-off tests, and the audit-log task-id correlation. |
| `test_long_task_gap_coverage.py` | Deterministic branch coverage for `gco_mcp/tools/_long_task.py` stream framing, notification failures, subprocess cleanup, status writing, and runner lifecycle edges. Uses in-memory streams and fake processes, so it never spawns an OS process or waits on cancellation grace periods. |
| `test_mcp_destructive_gating.py` | Destructive-flag gating — `delete_job` / `delete_inference` / `delete_template` / `delete_webhook` / `delete_model` / `delete_nodepool` / `analytics_user_remove` / `cancel_queue_job` absent by default, present under `GCO_ENABLE_DESTRUCTIVE_OPERATIONS=true`. Plus `models_upload` under `GCO_ENABLE_MODEL_UPLOAD`. Confirms `GCO_ENABLE_ALL_TOOLS=true` registers every gated tool in one shot, and asserts each destructive tool builds the expected CLI invocation. |
| `test_mcp_images.py` | Image-publish gating (`images_build` / `images_push` under `GCO_ENABLE_IMAGE_PUBLISH`), destructive image tools (`images_cleanup` / `images_prune` / `images_delete_tag` / `images_delete_repo` under `GCO_ENABLE_DESTRUCTIVE_OPERATIONS`), `task=TaskConfig(mode="optional")` on `images_build`, `ctx.warning` capture on every destructive image tool via the audit middleware, and the image-mirror tools — `images_mirror_plan` / `images_mirror_status` (read-only, default-on) and `images_mirror` (gated by `GCO_ENABLE_IMAGE_PUBLISH`) — invoked through a FastMCP `Client` with `cli._image_mirror` patched. |
| `test_mcp_image_resources.py` | Image-registry resource paths — `images://gco/index`, `images://gco/{name}/tags`, `images://gco/{name}/{tag}`, `images://gco/replication/status`. Each test mocks the underlying `ImageManager` so the resource handlers never reach ECR. |
| `test_mcp_live_resources.py` | Live-state resource paths — explicit-region `gco://jobs/{region}/{job_name}` and `gco://k8s/{region}/{namespace}/{kind}/{name}` kubectl reads, account-qualified EKS context resolution, `gco://inference/{endpoint_name}`, `gco://cluster/{region}/topology`, `costs://gco/summary/{days_window}`, and `tasks://gco/{task_id}`. Regionless kubectl URIs fail closed instead of using the ambient context. |
| `test_mcp_python_version.py` | Asserts the Python 3.14+ floor explicitly, imports the current raw-CDK config resource, and scans version-bump docs for legacy `Python 3.10/3.11/3.12/3.13` support claims. |
| `test_mcp_extended_coverage.py` | Branch coverage across the long tail of MCP modules — `gco_mcp/iam.py` (env-unset no-op, role assumption, failure propagation, expiration fallback), `gco_mcp/resources/tasks.py` (task-id validation, the `get_task` / `_docket` / `fetch_task` accessor chain, `_coerce_to_dict` fall-throughs across dict / `model_dump` / `__dict__` / `str()` for slotted records, protocol-unavailable stub when `server` import fails), `gco_mcp/resources/docs.py` per-bucket resource handlers and metadata-header rendering, `gco_mcp/resources/cluster.py` and `k8s.py` (validation + kubectl branches), `gco_mcp/resources/iam_policies.py` and `ci.py`, `gco_mcp/tools/docs.py` (`find_docs` query / topic / no-match / `limit <= 0`), `gco_mcp/tools/images.py` plus the lazy `_get_manager`, `AuditCaptureMiddleware` ContextVar reset, and every error path in `gco_mcp/cli_runner._run_cli`. |
| `test_mcp_tool_wrapper_gap_coverage.py` | Deterministic behavior coverage for MCP CLI-wrapper branches. Tool modules are loaded under unique names with local stubs for registration, decorators, feature flags, CLI and long-task runners, context dependencies, and backend helpers, preventing network access and shared-registry mutation. |
| `test_storage_mcp.py` | S3 storage MCP wrappers and shared local-path confinement — model-upload versus sync gating, short-path resolution beneath `GCO_STORAGE_LOCAL_ROOT`, identity-bound sync contracts, traversal/symlink rejection, `--` argv boundaries, and async CLI cancellation handling. |
| `test_local_data.py` | POSIX descriptor-relative local-data confinement and lifecycle — secure root creation, traversal/symlink/special-file rejection, `/dev/fd` staging, immutable upload snapshots, source-mutation detection, and deterministic consume/abort/descriptor cleanup. |

### Mission Tests

The Mission goal-directed iteration loop has its own test surface that
exercises the engine, the validators, the predicate / script sandbox,
sampling, the filesystem state backend, the audit pipeline, the MCP
tool gating, and the CLI subcommand group. Eight end-to-end files carry
the `mission_e2e` marker (see [Mission End-to-End Tests](#mission-end-to-end-tests)
for the dedicated invocation knobs); the rest run in the default suite.

Every test runs offline against a `FilesystemBackend(root=tmp_path)`
with a stub dispatcher — no AWS credentials, no network, no real LLM.
The full Mission suite finishes in well under a minute on a fresh
checkout.

| File | Description |
|------|-------------|
| `test_mission_engine.py` | `MissionEngine.run_iteration` happy path and failure paths against a real `FilesystemBackend(tmp_path)` — one normal `continue` iteration with all five phases `succeeded`, completion-on-criteria-met (Final_Report written next to the session JSON), `max_iterations=1` terminating on the first call, an empty `tool_allowlist` failing Propose with `MissionEngineError("propose_no_tool_available")` and the engine refusing subsequent calls with `session_failed`, and the per-iteration five-call `mission_phase_event` audit cadence. |
| `test_mission_validation.py` | Property-based soundness tests for every validator in `gco_mcp/mission/validation.py`. Two complementary invariants per validator: synthesised malformed input always raises `MissionValidationError` with the expected `code` plus the `details["field"]` / `details["reason"]` markers, and well-formed input returns the normalised shape. Predicate criteria carry the cached AST under `_parsed_ast`. |
| `test_mission_state.py` | Round-trip and atomic-write tests for `FilesystemBackend` — `save_session` → `load_session` is byte-identical, schema-version mismatches log a single warning and return `None`, the `tempfile` → `flush` → `fsync` → `os.replace` write path leaves the prior on-disk version intact when `os.fsync` raises mid-write, POSIX 0o700/0o600 mode tightening, status-filtered listing, atomic deletion of the session and its sibling `.report.json`, plus a skip-marked DynamoDB placeholder so the deferred coverage shows in `pytest -v`. |
| `test_mission_types.py` | Hypothesis round-trip property test for the `SessionState` JSON serializability invariant — for every well-formed shape, `json.loads(json.dumps(s)) == s` must hold. Strategy uses `st.fixed_dictionaries` for every required key set defined in `gco_mcp/mission/types.py` and `st.sampled_from(get_args(...))` for every `Literal`-typed label. |
| `test_mission_decide_determinism.py` | Property tests for the verdict cascade in `gco_mcp/mission/decide.py`. Two invariants: control-path determinism (calling `decide_verdict` twice on the same `(session, iteration, now)` triple returns equal tuples — no clock reads, no globals, no random sources) and sampling cannot mutate the control path (cycling `iteration["sampling_output"]` through three values, then through five distinct sampler-mode profiles, leaves the verdict tuple unchanged). |
| `test_mission_predicate_security.py` | Hypothesis property tests for the predicate sandbox parser. Two invariants: forbidden source (e.g. `__import__("os")`, `().__class__`, `eval("1")`, `lambda x: x`, walrus assignments, attribute walks, subscript-then-call, `getattr(obs, ...)`) always raises `PredicateRejected` — the evaluator is monkey-patched to raise on entry as a tripwire — and well-formed expressions over `obs` plus the eight allowlisted callables (`len` / `min` / `max` / `sum` / `abs` / `any` / `all` / `sorted`) evaluate without raising. |
| `test_mission_script_sandbox.py` | Hypothesis property tests for the script-sandbox AST validator (`gco_mcp/mission/sandbox.py`). Same two invariants as the predicate tests but against multi-statement Python source — `import` / `__import__` / class-walk chains / `eval` / `exec` / `compile` / lambdas / async / walrus / decorators / `class` defs / bare `except` / `with` / `match` / `global` / `nonlocal` / `del` / `assert` / subscript-then-call all raise `ScriptRejected`, and a tripwire patched onto `mission.sandbox` confirms the runtime entry point is never reached for any rejected source. |
| `test_mission_sampling.py` | Property-based tests for the `SamplingPrompt` builder — `assemble()` is deterministic across runs (byte-identical), the rendered prompt is never larger than `PROMPT_BYTE_BUDGET`, an Observation field exactly `OBSERVATION_FIELD_BYTE_CAP` bytes long is not truncated (one byte more is, with `_original_bytes` recorded), oldest iterations drop first when the cap forces eviction, and final-lessons rendering uses a distinct schema. |
| `test_mission_audit.py` | End-to-end audit-log reconstruction. Drives a complete Mission session through several iterations to a terminal verdict, captures every `gco.mcp.audit` JSON line via `caplog`, filters by `event_type`, and rebuilds the iteration history (`iteration_index`, ordered `(phase, status)` pairs, terminal verdict / verdict_reason) from the audit stream alone. The rebuild must equal the persisted session's `iterations` list on those fields. Determinism enforced via stagnation-threshold-disabled, `max_iterations=4`, and a fixed-result dispatcher. |
| `test_mission_criteria_scaffold.py` | Three groups against `gco_mcp/mission/criteria_scaffold.py`. Deterministic generator — keyword-template lookups produce the expected criterion shape with the placeholder fallback when no keyword matches. Validator contract — every output of the deterministic generator is accepted by `validate_criteria` (so the file is immediately usable with `mission start --criteria-file`). Sampling path — `generate_sampled_criteria` walks the retry loop correctly: well-formed array succeeds in one attempt; garbage-then-valid succeeds on retry; garbage every attempt eventually raises `ScaffoldSamplingError`. Plus the metric-path normaliser, the predicate-attribute autofix, and the prompt-content assertions (rejected examples, `tool_call_succeeded` guidance, dict-method allowlist). |
| `test_scaffold_fixture_replay.py` | Cross-model regression net for the scaffolder pipeline. Replays every captured [Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html) model response under `tests/fixtures/scaffold_responses/*.json` through `_parse_response` -> `_normalize_metric_path` -> `_autofix_predicate` -> `validate_criteria` and asserts the round-trip succeeds. Each test id is `<model_slug>::<directive_slug>` so a regression points directly at the (model, directive) pair that broke. New fixtures captured via `scripts/capture_scaffold_fixtures.py` automatically join the parametrize matrix on the next run; see `tests/fixtures/scaffold_responses/README.md` for the runbook. |
| `test_mission_environment_context.py` | Tests the `gco_mcp/mission/_environment.py::gather_session_environment` helper and the optional `environment_context` field it feeds on `SamplingPrompt`. Three groups: gather behaviour (returns `None` when no checker is reachable, drives a stub checker to render per-region cluster metrics + counts-only reservation summary, returns `None` when the cluster-metrics probe raises, returns a zeroed-but-shape-stable skeleton when no regions are deployed, and a guard that no timestamp leaks into the output so determinism holds), the `_summarise_environment_context` byte-cap + key-sort guarantees (small input passes through with sorted top-level keys, oversize input drops the largest field first and records `_dropped_fields`, no `_dropped_fields` key appears when nothing was dropped), and the `SamplingPrompt` integration (section omitted when `environment_context=None`, section emitted under its header when set, byte-identical assemble across two prompts with the same env, byte-identical assemble when env keys are reordered, and an oversize env never pushes the prompt past `PROMPT_BYTE_BUDGET`). |
| `test_mission_cli.py` | CLI tests for the `gco mission` subcommand group through `CliRunner`. Each test points the module-level `_BACKEND_INSTANCE` at a per-test `tmp_path` so sessions don't leak across cases, and sets `GCO_ENABLE_MISSION` per-test through `monkeypatch` so the gate doesn't block the subcommand under test. Covers `start` (creates a session), `start --run` (runs to completion synchronously), `status` against an unknown id (exits 1 with a `session_not_found` envelope on stderr), `list --output table`, and the `--max-iterations -1` opt-out sentinel acceptance plus the rejection of `--max-iterations 0`. |
| `test_mission_memory_config.py` | Tests for the `mission_memory.*` config getters and validation in gco/config/config_loader.py — shipped defaults (feature ON), cdk.json/code default agreement, partial-override merging, and every validation error path including the one-way-door `dimensions` / `distance_function` fields. |
| `test_mission_memory_stack.py` | CDK synthesis tests for the mission-memory add-on in gco/stacks/global_stack.GCOGlobalStack (present when `mission_memory.enabled`, absent otherwise) — table shape (PAY_PER_REQUEST, PITR, TTL, AWS-managed encryption), the `AwsCustomResource` vector-index Create/Delete payloads (index name, embedding attribute, filter/projection schema resolved through nested `Fn::Join`s), the pre-created index role's exact-ARN scoping, SSM parameter names, backup-plan selection, and the disabled state contributing zero `MissionMemory` resources. Three live-deploy regression guards: `InstallLatestAwsSdk: true` is pinned (the Lambda runtime's bundled JS SDK predates vector indexes and silently drops `VectorIndexUpdates`, failing the create), the on-delete call's `ignoreErrorCodesMatching` is pinned so a failed create rolls back cleanly instead of wedging the stack in DELETE_FAILED, and the synthesized Create/Delete payloads are walked recursively against botocore's `UpdateTable` model (unknown members and missing required members both fail — the second live failure was a nested `VectorConfiguration` draft shape whose members serialized as nulls). |
| `test_mission_memory_runtime.py` | Unit tests for the mission-memory runtime modules (`gco_mcp/mission/embeddings.py`, `gco_mcp/mission/memory.py`) with mocked boto3 — no live AWS. Pins the two request-shape gotchas (`SearchVector` is a plain list of `{"N": ...}` values, never `L`-wrapped; the written `directive_embedding` attribute *is* the `L`-wrapped form) and the degradation taxonomy: absent table/index, backfilling index, missing SSM parameters, and missing credentials all raise `MissionMemoryUnavailableError`, while write-path `ValidationException` stays a hard `MissionMemoryError`. Also covers the Bedrock FTU escalation, malformed-embedding responses, SSM-lazy name caching, the `DYNAMODB_REGION` → `GLOBAL_REGION` → `AWS_REGION` resolution order, the runtime-defaults-mirror-cdk.json agreement test, and a skip-marked Floci placeholder (the emulator does not implement `SearchVectors`). |
| `test_mission_memory_engine.py` | Engine and prompt wiring tests for mission memory, all hermetic. Terminal-verdict write path: `MissionEngine._maybe_write_memory` fires after the Final_Report lands (ordering asserted via the report file existing at call time), reuses the sampled overlay without re-sampling, writes on both `complete` and `terminate`, and swallows every store failure — absent table, backfilling index, Bedrock down, and an unexpected bug all leave the mission completed with its report on disk. Prompt block: the optional `prior_missions` field renders under `=== Prior similar missions ===` with its own byte-cap domain (tail/least-similar dropped first, oversize `lessons` field-truncated), and its `None` default keeps the prompt byte-identical to the pre-memory shape. Factory retrieval: the sampler closure retrieves once per wiring (cached), passes results to `maybe_sample_strategy_revision`, degrades to `None` on empty/raising/absent stores, the `--dry-run` dependency set stays memory-free, and the suite-wide conftest neutraliser for the store-construction seam is itself regression-tested. |
| `test_mission_memory_cli.py` | CLI tests for the `gco mission memory` subcommand group (`search` / `list` / `backfill`) through `CliRunner`, with `MissionMemoryStore` patched at its import site so no AWS is reached. Pins the `GCO_ENABLE_MISSION` gate (exit 2 + hint), argument forwarding and JSON/table output shapes, the deployment hint + structured envelope + exit 1 on `MissionMemoryUnavailableError`, and the backfill contract: terminal reports written, non-terminal shapes skipped, unreadable files and per-report write failures isolated (run continues, exit flips to 1), and infrastructure absence stopping the run immediately. |
| `test_mission_mcp_tools.py` | Gating tests for the ten `mission_*` MCP tools plus functional `mission_memory_search` tests over the FastMCP Client with a stubbed store (results + argument forwarding, unavailable-infrastructure envelope, unexpected-failure envelope). Mirrors the precedent in `test_mcp_destructive_gating.py`: snapshots the registry via the async `mcp._list_tools()` and asserts the expected names appear or are absent depending on `GCO_ENABLE_MISSION` (or the umbrella `GCO_ENABLE_ALL_TOOLS`). The pre-test cleanup strips every `mission_*` registration off the live FastMCP singleton via `local_provider.remove_tool` and pops `tools.mission` from `sys.modules` plus the `tools` package attribute — required so `register_all_tools()` re-evaluates the gating flag on every reload rather than resolving a cached module reference. |
| `test_mission_coverage.py` | Focused unit tests that backfill coverage gaps the rest of the corpus misses. Each test targets a narrow line range from `--cov-report=term-missing`: `gco_mcp/mission/predicate.py` comprehension target shadows / dunder-name rejections / dict `**` unpacking / sliced-with-step rejections, `gco_mcp/mission/sandbox.py` `_rewrite_mission_helpers` and `validate_script_ast` rejection branches, `gco_mcp/mission/audit.py::replay_audit_entries` shape reconstruction, `gco_mcp/resources/mission.py` non-filesystem report fallback and `_make_not_found` exception chain, and `gco_mcp/tools/mission.py::_strip_private_fields` plus the iterations-shape variant. |
| `test_mission_e2e_train_to_loss.py` | End-to-end Mission session driven to completion by a metric_threshold criterion (`val_loss <= 0.5`). The dispatcher returns a `val_loss` metric that decreases each iteration via an `itertools.chain` closure, so the test is reproducible. Asserts four invariants: the cascade lands on `("complete", "criteria_met")` within `max_iterations=20`, the persisted Final_Report carries the directive verbatim (no rewriting / summarisation), the audit stream emits exactly five `mission_phase_event` records per iteration (the engine's per-phase try/finally emit contract), and the persisted `iterations` length matches the Final_Report's `iterations_run` field. The reference precedent the other e2e tests are shaped against. |
| `test_mission_e2e_search.py` | End-to-end Mission session driven to completion by a `predicate` criterion declaring success when an iteration's `tool_results` has at least five entries and any entry has `score > 0.9`. Two non-obvious structural overrides: `MissionEngine._build_strategy` is monkey-patched to return a five-call Strategy each iteration (the deterministic Propose fallback synthesises a single-call Strategy, which can never satisfy the five-entry predicate), and the dispatcher's `score` field grows monotonically `0.10, 0.15, 0.20, ...` *across* the entire session rather than resetting per iteration. |
| `test_mission_e2e_converge.py` | End-to-end Mission session driven to completion by *both* a `metric_threshold` (`metrics.current_loss <= 0.5`) and a `predicate` (the absolute delta of that metric across the last three iterations is at or below a small tolerance) holding simultaneously. Asserts the cascade lands on `("complete", "criteria_met")` only on the iteration where both Criteria are `met` — not earlier (when only the threshold holds but the values still vary) and not later (the cascade stops on the first satisfying iteration). |
| `test_mission_e2e_budget.py` | Three end-to-end budget-cap scenarios. `test_terminate_on_max_iterations` configures an unreachable Criterion and caps the run at three iterations — asserts the cascade returns `("terminate", "max_iterations")` on the iteration where the budget first flips. `test_uncapped_iterations_does_not_terminate_on_iteration_count` uses `max_iterations=-1` (the opt-out sentinel) and lets the heuristic terminate the session via `no_progress` instead. `test_uncapped_wall_clock_does_not_terminate_on_time` does the same with `max_wall_clock_seconds=-1`. |
| `test_mission_e2e_stagnation.py` | Two end-to-end no-progress scenarios under an unreachable Criterion. `test_adjust_fires_before_terminate` caps the run at `stagnation_threshold=4` and asserts the heuristic emits `("adjust", "heuristic_unproductive")` on an iteration *before* the cascade terminates — the brief calls out that adjust must surface at the half-threshold (`ceil(4/2)=2`) once three consecutive iterations share a tool-name sequence (`_strategy_unproductive` clause a). `test_terminate_on_no_progress` lets the loop run all the way to threshold and asserts the final verdict is `("terminate", "no_progress")`. |
| `test_mission_no_aws.py` | End-to-end Mission session that completes without any AWS access at all. Two safe-tier tools (`find_examples`, `find_docs`) in the allowlist, a documentation-search directive, and a single `predicate` Criterion that completes the session the moment any `tool_results` appear. To prove the no-AWS contract is *enforced* rather than incidentally held, `boto3.Session` is monkey-patched to raise on construction — any regression that started reaching for an AWS service from a safe-tier path would surface as a hard failure here rather than as a silent runtime cost on a credential-less host. |
| `test_mission_mcp_integration.py` | End-to-end Mission lifecycle through `Client(run_mcp.mcp)` over the FastMCP in-process protocol layer. Drives every Mission tool over real JSON-RPC: tool discovery (gated and ungated), `mission_start` (wired against the live FastMCP tool registry, dispatching real `find_examples` calls so the Observation carries production-shape `tool_results`), `mission_iterate`, `mission_status`, `mission_history`, `mission_checkpoint`, `mission_list`, plus the `mission_abort --pause` → `mission_iterate` refusal → `mission_resume` → `mission_complete` round-trip and the second-call idempotent rejection. Also exercises the `mission://sessions/{id}` resource (persisted state) and the `mission://sessions/{id}/audit-replay` resource (reconstructs the iteration history from the in-process audit collector). The `_reload_with_mission_flag` helper strips `tools.mission` from `sys.modules` AND removes the `mission` attribute from the `tools` package before reloading `run_mcp`, so the `if is_enabled(FLAG_MISSION):` gate at module-import time re-evaluates the env var. |
| `test_mlflow_charts.py` | MLflow chart wiring and gating — the pinned charts.yaml entry (official OCI chart + `-full` server image pins, `fullnameOverride: mlflow` naming contract, chart-managed SQLite claim on the observability gp3 class with no strategy pin so the chart auto-Recreates, ClusterIP/no-ingress/no-app-auth posture, telemetry env kill, chart-built pod NetworkPolicy, metrics + ServiceMonitor, deployment tokens deliberately absent from static values), the `cluster_observability.mlflow` conjunction in both directions plus the no-clobber sub-block merge, the value overrides carrying the S3 artifact destination, IRSA role annotation and claim size, the `{{MLFLOW_ENABLED}}`-gated client egress NetworkPolicy (port matched to the example's tracking URI, example carrying the client label), the prune inventory covering the manifest resources plus the chart-managed claim `helm uninstall` leaves behind, the tunnel service entry, and helm-installer `handle_task` convergence both ways. |

### Mission Semantic-Progress Judge Tests

The Semantic-Progress Judge (`gco_mcp/tools/semantic_progress.py` plus the pure
`gco_mcp/mission_judge/` package) is the LLM-as-judge tool that scores Mission
progress against a fixed, versioned rubric and emits the score in the canonical
`{"metrics": {"progress_score": <float>}}` shape the Observe phase merges. Its
test surface mirrors the metric-reader split: property/unit tests for the pure
package, mocked-backend tests for the tool wrapper, an integration test against
the unchanged engine merge contract, and a doc-hygiene guard. Every test runs
offline — the single non-deterministic `sample()` call is always stubbed, so no
live LLM or Bedrock call is ever made.

| File | Description |
|------|-------------|
| `test_semantic_progress_shape.py` | Property and unit tests for the pure `gco_mcp/mission_judge/shape.py` helpers — the canonical-shape builder (`metrics_result` maps exactly one output name to the finite score with every provenance field placed *outside* `metrics`), the finite-float Numeric_Value guard (`is_finite_float` is true iff a non-bool int or finite float), the Output_Name validator round-trip (1..128 chars, no `.` / whitespace, else `JudgeError(INVALID_OUTPUT_NAME)`), and the error-envelope builder (`error_envelope` never carries a top-level `metrics` key). Hypothesis-driven with ≥100 examples per property. |
| `test_semantic_progress_score.py` | Property and unit tests for `gco_mcp/mission_judge/score.py` — `clamp_score` folds any finite float onto the closed `[0.0, 1.0]` interval (below→`0.0`, above→`1.0`, in-range unchanged) and never raises, while `parse_score` accepts a JSON object with a finite, non-bool numeric `score` field and rejects the full family of invalid model output (non-JSON, non-object, missing field, bool / string / null / NaN / ±inf) with `JudgeError(INVALID_MODEL_SCORE)`. |
| `test_semantic_progress_prompt.py` | Property tests for the deterministic prompt builder in `gco_mcp/mission_judge/prompt.py` — two independent `build_prompt(...).assemble()` calls render byte-identical output, the folded-in context never exceeds `MAX_CONTEXT_CHARS`, oversized context is truncated keep-newest (tail retained behind `TRUNCATION_MARKER`, oldest head discarded), and an absent/empty context yields a directive-only prompt. |
| `test_semantic_progress_tool.py` | The tool-wrapper surface in `gco_mcp/tools/semantic_progress.py`, exercised against a stub sampling backend (no live LLM). Covers the success path (canonical shape with finite `progress_score`, provenance outside `metrics`, exactly-once `sample()`, `"<backend>:<model>"` source identifier preserving embedded colons, MCP vs Bedrock backend provenance, out-of-range clamp keeping the pre-clamp `raw_score`), one failure-class assertion per stable code (`invalid_output_name`, `missing_directive`, `no_sampling_backend`, `sampling_transport_error`, `invalid_model_score` — each returns an envelope and nothing escapes the tool), flag-gated registration (absent default-off, present under the per-tool flag or the `GCO_ENABLE_ALL_TOOLS` umbrella, gating-prefixed description and `{"safe","metrics"}` tags), and Hypothesis property tests for the determinism boundary (fixed inputs + fixed `sample()` → identical result) and tool-registry determinism (repeated `_list_tools()` is stable and gates the judge exactly). Module-scope helpers force-unregister the gated tool on teardown so registration never leaks into sibling tool-count snapshots. |
| `test_semantic_progress_observe.py` | Integration test that feeds a real judge success result and error envelope through the **unchanged** `MissionEngine._build_observation` merge and the `_evaluate_metric_threshold` / `_evaluate_metric_trend` / `_build_cumulative_observation` surfaces. Asserts `observation["metrics"]["progress_score"]` resolves to the emitted score and a `progress_score >= 0.8` criterion evaluates `met` / `unmet` deterministically, an error envelope leaves the criterion `inconclusive` (no metric merged), and a two-iteration cumulative view drives a `metric_trend` over the `progress_score` series. Also runs both paths end-to-end through `run_iteration` (marked `mission_e2e`). Confirms the merge contract without modifying the engine. |

The feature's spec breadcrumb guard is not a per-feature file; it is one row in the parametrized `test_doc_hygiene.py` (see [Codebase Guardrail Tests](#codebase-guardrail-tests)).

### Image Registry Tests (CLI + global stack)

Image-registry tests cover the CLI side (`cli/images.py::ImageManager`), the global-stack ECR replication and lookup-or-create custom resource, and the destroy-time inventory guard.

| File | Description |
|------|-------------|
| `test_container_runtime.py` | `cli/_container_runtime.py::detect_container_runtime` priority order (`docker` > `finch` > `podman`), `CDK_DOCKER` override, `None` fallback. Mocks `shutil.which` and `subprocess.run` so no real runtime is required. |
| `test_images_cli.py` | `ImageManager` validation, public methods, and CLI-surface argv translation — name/tag regex round-trip, ECR-URI rewrite identity for non-ECR refs, path-traversal rejection on the build context, idempotent `init`, default lifecycle policy shape (keep 20 tagged + expire untagged after 7d), build-runtime detection, immutable-tag rejection on a second build of the same tag. Hypothesis property tests for the regex round-trips and the URI-rewrite identity. |
| `test_images_cli_extended.py` | Extended `ImageManager` — `list_repos` / `list_tags` / `describe` / `replication_get` / `replication_status` / `lifecycle_get` / `lifecycle_set` / `replication_sync` / `delete_tag` / `delete_repo` / `cleanup` (100-id chunked) / `prune` (dry-run vs actual) / `orphans` (cross-referencing inference and recent-job image refs), the `_ecr_login` and `_check_tag_immutable_collision` pre-flight branches, `_isoformat` / `_parse_iso` / `get_image_manager`, plus `_collect_recent_job_image_refs` covering happy-path region union, threshold filtering, missing `created_time` treated as in-window, naive datetime normalisation, fail-soft on `JobManager` / `list_jobs` failures, and skipping non-string / empty image-ref entries. |
| `test_images_cmd.py` | `gco images` Click subgroup driven through `CliRunner` — every subcommand surface (`init`, `list`, `tags`, `describe`, `uri`, `build`, `push`, `delete-tag`, `delete-repo`, `cleanup`, `prune`, `orphans`, `lifecycle get/set`, `replication get/status/sync`) with success and error branches, the `--yes` confirmation gate on every destructive command, `--build-arg` parsing, and the `--no-dry-run` toggle on `prune`. Mocks the `ImageManager` factory so no AWS or runtime calls happen. |
| `test_image_lookup_handler.py` | The lookup-or-create custom resource Lambda (`lambda/image-lookup/handler.py`) — adopt-existing-repo path, create-on-missing path, and the `gco:retain=true` tag suppressing the Delete event even when `removal_policy: "destroy"` is set. |
| `test_global_stack_images_config.py` | `GCOGlobalStack`'s `_parse_images_config` accepts the documented `cdk.json` schema with default values, the ECR replication rule materialises every deployment region as a destination, and the `gco/*` repo prefix is enforced. Validation tests reject malformed `removal_policy` values. |
| `test_stacks_image_registry_destroy.py` | Pre-destroy inventory summary in `cli/stacks.py` (when `images.removal_policy: "destroy"` AND `images.empty_on_delete: true`, print repo / tag / GiB / referencing endpoint / recent-job reference counts; prompt on a TTY). Also the helpful-error path when `empty_on_delete: false` AND repos are non-empty — points the user at `gco images cleanup --all` or flipping the flag. |

### Codebase Guardrail Tests

Static analysis tests act as guardrails against regressions in specific drift directions: Python-3.15 deprecation surface re-appearing in production code, spec / planning-document references leaking into production code or human-facing docs, stale CVE suppressions in `.pip-audit-ignore` or `.trivyignore` outliving their expiration date, CI-config path references (CodeQL scan paths, coverage `--cov` targets, coverage source) outliving a package rename, MCP tools drifting out of sync with the CLI they wrap (invalid command paths / flags) or with the tool counts quoted in the docs, OS-base container images shipping without a build-time security upgrade, and Helm chart pins in `charts.yaml` that no longer resolve to an installable chart at the pinned version.

| File | Description |
|------|-------------|
| `test_accelerator_catalog.py` | Deterministic accelerator maintenance guard: real-repository success, deprecated/end-of-life NodePool rejection with exact replacements, newer unreferenced generation advice naming affected pools, and exact catalog/`cdk.json`/`ConfigLoader` watch-list synchronization. Online EC2 discovery is intentionally outside pytest. |
| `test_accelerator_pools.py` | Spot Placement Score instance-pool policy: shipped pools validate against the real repository, the three-distinct-type minimum names offending pools, members stay a subset of the watch list, overlap is permitted with deterministic first-pool attribution, every watched type has an explicit pooled-or-unpooled decision, and no pool mixes CPU architectures. |
| `test_dependency_pin_consistency.py` | Parses `[project].dependencies` and every `[project.optional-dependencies]` group in `pyproject.toml` with `packaging.requirements.Requirement`, canonicalizes package names, and fails when a package repeated across sections uses different version specifiers. Failure output names every conflicting specifier and declaration location, preventing production-image groups, core dependencies, and development extras from silently drifting to incompatible pins. |
| `test_no_python_315_deprecation_surface.py` | Walks the production tree (`gco_mcp/`, `cli/`, `gco/`, `lambda/`, `tests/`, `dockerfiles/`, project README) and fails if any pattern Python 3.15 soft-deprecates re-appears: `collections.abc.ByteString`, `typing.ByteString` / `no_type_check_decorator`, `cProfile` import, `glob.glob0` / `glob1`, `platform.java_ver`, `load_module` / `find_module` / `zipimporter`, `NamedTuple` keyword-argument syntax, zero-field `TypedDict("Name")`, and bare `re.match(` calls outside two intentional carve-outs. Failures are emitted as `path:line: [pattern] line-content`. A companion drift assertion requires the filename to target the minor immediately after `[project].requires-python`; raising the floor from 3.14 to 3.15 therefore fails until this suite is renamed and updated for Python 3.16 deprecations. |
| `test_no_spec_references.py` | Walks `gco_mcp/`, `cli/`, `gco/`, `lambda/`, `tests/`, `dockerfiles/`, `examples/`, `docs/`, `scripts/`, plus the project READMEs, and fails if any prohibited spec / planning prose substring appears — covers filenames (`requirements.md` / `design.md` / `tasks.md` / `bugfix.md`) plus prose phrases (`per the requirements`, `per the design`, `per the spec`, `as the spec says`, `see the {requirements,design,tasks} doc`). Self-excludes via `Path(__file__)` so its own literals don't trip the check. |
| `test_distroless_build_scripts.py` | Hermetic unit tests for the distroless image build scripts under `dockerfiles/` (loaded by file path — they are deliberately not a package). Covers `runtime_smoke.py` end to end via doctored manifests: usage errors, the all-green path, missing stdlib extensions and entry modules, wrong runtime user, the empty-manifest hard failure (a gutted manifest must never pass vacuously), failure aggregation, and monkeypatched zero-CA / broken-trust-store / missing-tzdata reporting. Covers `build_scratch_rootfs.py`'s pure path canonicalization (merged-/usr aliasing, rootfs staging), the stdlib import probe (importable/broken split against a fake `lib-dynload` tree, critical-floor enforcement, empty-enumeration abort, and that every floor anchor is a real importable extension), manifest content/placement, and the `ldd`/`dpkg -S` output parsers faked at the `subprocess.run` seam (per-seed attribution, builder-broken `_tkinter`-style skips whose resolvable libraries must not ride along, unresolved-dependency aborts, diversion-line and `/usr/local` tolerance). Per-Dockerfile drift guards assert each smoke `RUN`'s entry module matches its `CMD` and resolves to a real `gco/services` module, and that both build scripts are COPY'd together. |
| `test_doc_hygiene.py` | Per-feature companion to `test_no_spec_references.py` for the *aggressive* spec-breadcrumb patterns (requirement IDs like `R12.6`, bare `Property N`, `Requirement N`, `task N`, `Validates:`, planning-doc filenames, and "the design" / "the spec" prose) that would false-positive if run repo-wide. One parametrized case per spec-driven feature (currently `mission-metric-reader-tools`, `mission-allow-all-tools`, `mission-semantic-progress-judge`), each scanning only that feature's explicit source files and its own `tests/test_<feature>_*.py` modules; documentation, examples, and a feature's functional flag names / `[gated by ...]` prefixes are out of scope. Adding a feature means appending one `_Feature` row, not a new test file. |
| `test_pip_audit_ignore_validator.py` | Pins the contract of `.github/scripts/check_pip_audit_ignore.py`, which gates the pip-audit job in `.github/workflows/security.yml`. Every entry in `.pip-audit-ignore` must carry an `exp:YYYY-MM-DD` marker; the validator fails the workflow when any entry is on-or-before today (inclusive — no bonus day) or is missing the marker entirely. Tests cover happy paths (single, multi, blank-line / comment skipping, missing-file-is-clean), expired-date detection (past dates, equal-to-today, ±1 day boundary), missing or malformed markers, `main()` exit codes / stdout, and a live-file check that runs the committed suppression file through the validator with today's date. |
| `test_npm_audit_checker.py` | Pins the exact, expiring npm-audit suppression gate in `.github/scripts/check_npm_audit.py`: suppression-file parsing, inclusive expiration and duplicate rejection, advisory extraction, malformed and operational-error JSON, exact package-directory/package/advisory/node matching, compound-record fail-closed behavior, severity thresholds, stale entries, and `main()` exit codes. Uses synthetic reports and temporary files only; it never contacts npm or the network. |
| `test_ci_config_paths.py` | Guards CI config against stale path references left by a package rename (the `mcp` to `gco_mcp` move broke the CodeQL autobuilder with FileNotFoundError). Asserts every `paths:` entry in `.github/codeql/codeql-config.yml` is a real directory, every `--cov=<pkg>` target across `.github/workflows/*.yml` and `.github/legacy/.gitlab-ci.yml` resolves to an existing top-level directory, and every `[tool.coverage.run] source` dir in `pyproject.toml` exists. Catches the silent failure mode where a renamed package leaves coverage recording nothing and CodeQL crashing at scan time. |
| `test_docs_coverage.py` | Documentation-coverage guard with four cases: every `tests/test_*.py` module appears in `tests/README.md`; every `gco` Click command (the full command tree, walked recursively) is documented in `docs/CLI.md` (matched as a `gco <command>` entry); every registered MCP tool — enumerated in a subprocess with `GCO_ENABLE_ALL_TOOLS` so the full catalog is visible — appears in `gco_mcp/tools/README.md`; and every documented `uvx` / `uv tool install` snippet in `gco_mcp/README.md` pins `--python` to the minimum version from pyproject's `requires-python` (so installs cannot fail resolution on hosts whose default Python is older, and a future Python bump cannot leave the docs requesting a stale interpreter). Each case fails with the list of offending items so the fix is mechanical. |
| `test_mcp_cli_contract.py` | Contract guard: every MCP tool that shells out to the `gco` CLI must build an invocation the Click command tree actually accepts. A subprocess (with `GCO_ENABLE_ALL_TOOLS`) invokes each tool with dummy args — patching `cli_runner._run_cli` to capture the argv instead of running it, and only invoking tools whose body references `_run_cli` so non-CLI backends aren't executed — and the parent resolves each captured argv against the live tree, flagging unknown subcommands and unknown options. Catches the class of bug where a tool passes a flag/subcommand the CLI rejects (this guard found and drove the fix of ten such pre-existing mismatches, e.g. `nodepools_create_odcr` passing `--count`/`--cluster`, `enable_analytics` calling `stacks analytics enable`, `webhooks_create` passing `--secret-name`). The check is strict — any mismatch fails. |
| `test_mcp_tool_count_docs.py` | Drift guard for the human-readable MCP tool counts. Enumerates the live registry in two subprocesses (clean env for the default count, `GCO_ENABLE_ALL_TOOLS` for the ceiling) and asserts every "N tools by default (up to M with all flags enabled)" figure quoted in `README.md`, `gco_mcp/README.md`, and `gco_mcp/tools/README.md` matches — the headlines had silently drifted to 98/130 while the server registered 109/144. Includes a sanity floor so a phrasing change that breaks the regexes fails loudly instead of passing on zero comparisons. |
| `test_trivyignore_validator.py` | Applies the shared `.github/scripts/check_pip_audit_ignore.py` validator (its line format — `<ID> exp:YYYY-MM-DD` — is identical) to `.github/config/.trivyignore`: every suppression must carry a future `exp:` marker, and the committed file must validate clean against today so an expired Trivy suppression can't silently outlive its fix. Covers valid/expired/missing-marker fixtures plus a live-file check and a well-formed-token check. |
| `test_helm_charts_validation.py` | Pins the contract of `.github/scripts/validate_helm_charts.py`, which gates the `integration:helm:charts-valid` job in `.github/workflows/integration-tests.yml`. Every `(chart, version)` pinned in `lambda/helm-installer/charts.yaml` must be a real, installable Helm chart. The offline tier checks required fields, SemVer versions, and `oci://`/`use_oci` consistency, builds the same chart reference the installer Lambda uses, and runs the committed `charts.yaml` through `main()`; an opt-in online tier (gated behind `GCO_HELM_CHART_VALIDATION=1` plus a `helm` binary) resolves each chart at its exact version (`helm show chart`) and renders it (`helm template`) so a typo'd name or a version that never shipped fails in CI rather than mid-deploy. Covers structural happy/failure fixtures, classic-vs-OCI reference construction, `main()` exit codes (0/1/2), the live charts.yaml, and both lockstep contracts: the Gateway API CRD-bundle/chart pairing and the Kubeflow Trainer runtime lockstep — the always-on offline half proves the committed `torch-distributed` extraction, the TrainJob example image, and the `docs/DISTRIBUTED_TRAINING.md` snippet agree, while fixture-driven online tests (plus an opt-in live render) prove a chart bump without re-extraction, a non-image spec drift beyond the documented pod-template deviations (`automountServiceAccountToken: false`, NoNewPrivs on the `node` container), or a dropped `trainer.kubeflow.org/*` label fails with an actionable message. |
| `test_k8s_manifest_validation.py` | Pins the contract of `.github/scripts/validate_k8s_manifests.py`, which gates the `integration:k8s:manifest-schema` job in `.github/workflows/integration-tests.yml`. Schema-validates the kubectl-applier manifests and the `examples/` gallery with kubeconform (not just YAML syntax). The offline tier covers placeholder rendering (`{{...}}` tokens to schema-shaped stubs — generic string, bare-integer, and the structural VPC-CIDR ipBlock — asserting real templated manifests still parse), target-file selection (only `*.yaml`/`*.yml`, `pipeline-dag.yaml` and `*.json` fixtures excluded, `dag-step-*.yaml` included), `format_failures` parsing (`statusInvalid`/`statusError` fail; `statusValid`/`statusSkipped` don't), the binary-missing exit code, and the design constants (GVK-qualified Ray/Volcano skips, Datree CRD-catalog URL); an opt-in online tier (gated behind `GCO_KUBECONFORM_VALIDATION=1` plus a `kubeconform` binary) runs the committed manifests and confirms a schema violation is caught. |
| `test_dockerfile_os_patch.py` | Every tracked OS-base Dockerfile (final stage using a package manager) must run a build-time security upgrade (`apt-get`/`dnf`/`apk` upgrade), the convention the Debian service images already follow and the helm-installer image was missing when it shipped HIGH CVEs. `scratch`/distroless bases are skipped automatically; a Dockerfile may opt out with a `# os-patch-lint: skip - <reason>` comment. Includes a sanity floor on the number of Dockerfiles discovered. |
| `test_bats_readme_coverage.py` | Keeps the BATS suite index honest: every `tests/BATS/*.bats` file must have a row in the "What's Tested" table of `tests/BATS/README.md`, and the table must not carry rows for suites that no longer exist. Scoped to backtick-wrapped `test_*.bats` names on table-row lines, so the `test_my_script.bats` placeholder in the README's "Adding New Tests" prose neither satisfies the forward check nor trips the reverse one. |
| `test_ci_scripts_readme_coverage.py` | The same index guard for CI helpers: every `*.sh` / `*.py` in `.github/scripts` must have a row in the "Files" table of `.github/scripts/README.md`, and the table must not carry rows for scripts that no longer exist. Added after four helpers (`check_npm_audit.py`, `use-pinned-npm.sh`, `validate_k8s_manifests.py`, `verify_inference_streaming_bundle_freshness.py`) drifted out of that table — since it is the only index of what the pipeline shells out to, a missing row makes a script invisible to anyone auditing CI. Reads only the *first* cell of each row, because later cells legitimately reference files from other directories (the `dev_alias_live.sh` row names `scripts/setup-dev-alias.sh`), which would otherwise make the reverse check demand they live in `.github/scripts`. |

### Infrastructure Tests

| File | Description |
|------|-------------|
| `test_oidc_stack.py` | GitHub OIDC provider CDK stack — synthesis, OIDC provider config, trust policy (wildcard/branch/custom repo), IAM policy actions, role properties, `policy.json` validation |
| `test_feature_toggles.py` | Generic feature toggle helpers, Valkey config (get/update/enable/disable), Aurora config (get/update/enable/disable), FSx refactor regression |
| `test_managed_config.py` | Managed deployment-config engine (`cli/managed_config.py`) and its veneers: writable-config resolution (installed-mode refusal), result-only validation incl. the repair path, idempotent no-ops, atomic writes preserving comments/order/mode/trailing-newline, scalar keys (region roles + `bedrock.mission_default_model_id` / `bedrock.capacity_advisor_default_model_id` with sibling preservation), `gco.cli.managed_config` audit lines, `gco stacks regions list/add/remove/set` + `gco stacks bedrock show/set-mission-model/set-capacity-advisor-model` CliRunner coverage, and the `GCO_ENABLE_CONFIG_MANAGEMENT`-gated MCP tools (registration + argv). |

### Configuration Files

| File | Description |
|------|-------------|
| `conftest.py` | Shared pytest fixtures and configuration |
| `_lambda_imports.py` | `load_lambda_module()` helper for importing Lambda handler modules under unique `sys.modules` names. See the [Lambda Handler Import Helper](#lambda-handler-import-helper) section above. |
| `_auth.py` | Shared cache-state and signature-verifier isolation for API tests whose subject is not authentication. |
| `_cdk_config_matrix.py` | The canonical list of `cdk.json` configuration overlays (default, multi-region, feature toggles, thresholds, helm matrix, analytics fixtures). Imported by both `tests/test_cdk_synthesis_matrix.py` and `tests/test_nag_compliance.py` so the two iterate over the same set. See the [CDK Configuration Matrix](#cdk-stack-tests) section. |
| `__init__.py` | Package initialization |

> The category tables above are curated by area. The tables below complete the per-file index so that **every** test module in this directory is documented; new clusters get their own subsection and everything else is listed alphabetically under Additional Tests.

### Mooncake / Disaggregated Inference Tests

| File | Description |
|------|-------------|
| `test_mooncake_autoscale_cli.py` | The deploy command routes per-role autoscaling into the mooncake block. |
| `test_mooncake_autoscaling_bounds.py` | Per-role autoscaler bounds and the static replica fallback. |
| `test_mooncake_backward_compat.py` | Endpoints without a ``mooncake`` block keep their classic single-instance shape. |
| `test_mooncake_bootstrap_port.py` | Bootstrap-port assignment for KV-transfer workers is collision-free. |
| `test_mooncake_cli_mcp_surface.py` | Tests for the disaggregated-serving CLI and MCP surface. |
| `test_mooncake_config_rendering.py` | Connector chaining and runtime config rendering for mooncake endpoints. |
| `test_mooncake_connector_config.py` | KV connector configuration emitted for each worker role. |
| `test_mooncake_e2e.py` | End-to-end reconciliation of a distributed inference endpoint. |
| `test_mooncake_efa_scheduling.py` | [EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html) fabric placement for RDMA KV-transfer role pods. |
| `test_mooncake_image_contract.py` | Contract tests that exercise the *actual* Mooncake vLLM image. |
| `test_mooncake_kvcache_tooling.py` | KV-cache tooling and split-serving deployability for Mooncake endpoints. |
| `test_mooncake_master_idempotency.py` | Idempotent maintenance of the shared per-region Mooncake master. |
| `test_mooncake_master_image.py` | Default image wiring for the shared per-region Mooncake master. |
| `test_mooncake_master_readiness_gating.py` | Gating dependent role-pod creation on the shared master's readiness. |
| `test_mooncake_mode_role.py` | Which worker Deployments a reconcile materializes for each serving mode. |
| `test_mooncake_multi_region_scope.py` | Region-boundary checks for disaggregated mooncake topologies. |
| `test_mooncake_nodepool_manifest.py` | Dedicated Mooncake EFA NodePool: only GPUs that can serve KV-transfer. |
| `test_mooncake_pd_proxy_program.py` | Request-shaping contract of the Mooncake PD proxy program. |
| `test_mooncake_pd_proxy_routing.py` | Front-door routing for disaggregated prefill-decode endpoints. |
| `test_mooncake_pd_proxy_coverage.py` | Handler coverage for the Mooncake PD proxy program (`gco/services/mooncake_pd_proxy.py`): prefill priming, decode streaming, the admin path, request dispatch, and the health endpoints. |
| `test_mooncake_region_services.py` | In-region service resolution for mooncake endpoints. |
| `test_mooncake_regional_bucket_provisioning.py` | Property-based test — the general-purpose regional bucket is always-on. |
| `test_mooncake_regional_bucket_synthesis.py` | Synthesis checks for the always-on general-purpose regional bucket. |
| `test_mooncake_regional_bucket_targeting.py` | Property-based test — a regional upload only ever touches its own region. |
| `test_mooncake_regional_scope.py` | Regional confinement of disaggregated KV-transfer wiring. |
| `test_mooncake_regional_upload.py` | Tests for ``RegionalBucketManager`` bucket resolution and upload error paths. |
| `test_mooncake_security.py` | Access-control guarantees for disaggregated inference. |
| `test_mooncake_serialization.py` | Round-trip behavior of a ``mooncake`` endpoint-spec block through the store. |
| `test_mooncake_spec.py` | Tests for the Mooncake endpoint-spec shape, constants, and byte-size helper. |
| `test_mooncake_spec_validation.py` | Fail-fast validation of a ``mooncake`` endpoint-spec block. |
| `test_mooncake_topology_fidelity.py` | Materialized role replica counts mirror the requested topology. |

### Metric Reader Tests

| File | Description |
|------|-------------|
| `test_metric_readers_aggregate.py` | Tests for the sequence reducer that collapses history to one number. |
| `test_metric_readers_cloudwatch.py` | Tests for the CloudWatch datapoint reader. |
| `test_metric_readers_files.py` | Round-trip tests for the file-format metric reader. |
| `test_metric_readers_files_coverage.py` | Edge and error-path coverage for the file-format metric reader (`gco_mcp/metric_readers/files.py`): value description, non-numeric and malformed-file errors, JSONL skip rules, Hugging Face trainer-state paths, and the Parquet and tfevents handlers. |
| `test_metric_readers_localfs.py` | Tests for confining a supplied path to an allowlisted root directory. |
| `test_metric_readers_logs.py` | Tests for pulling a scalar out of a job's log lines. |
| `test_metric_readers_observe.py` | Observe_Phase merge-contract integration test. |
| `test_metric_readers_shape.py` | Tests for the canonical metric-result builder. |
| `test_metric_readers_tools.py` | Success-path tests for the metric-reader MCP tool wrappers. |
| `test_tool_metrics_coverage.py` | Validation and error-envelope coverage for the metric-reader MCP tools (`gco_mcp/tools/metrics.py`): invalid extraction and aggregation modes, bad regex, and the CloudWatch, job-log, and shared-storage failure paths. |

### Additional Mission Tests

| File | Description |
|------|-------------|
| `test_mission_allow_all_tools_cli.py` | CLI tests for the ``--allow-all-tools`` flag on ``gco mission``. |
| `test_mission_allow_all_tools_integration.py` | Integration checks that an all-tools-resolved session behaves like an explicit one. |
| `test_mission_allow_all_tools_mcp.py` | MCP-tool tests for the ``mission_start`` all-tools resolution path. |
| `test_mission_allow_all_tools_validation.py` | Property-based checks for the Mission all-tools allowlist resolver. |
| `test_mission_metric_trend.py` | Tests for the cumulative-metrics view and the ``metric_trend`` criterion. |

### Additional Tests

| File | Description |
|------|-------------|
| `test_addons_cli.py` | Tests for ``gco stacks addons`` (status / install) and the ``--all-regions`` flag. |
| `test_analytics_cleanup_lambda.py` | Tests for the analytics-cleanup Lambda (lambda/analytics-cleanup/handler.py). |
| `test_api_docs_coverage.py` | Documentation-coverage guard for the HTTP API surface: enumerates every route the four FastAPI applications serve (manifest processor, health monitor, inference proxy, cost monitor) and asserts `docs/API.md` covers each one, that each documented path lists every method served on it, and — in reverse — that every endpoint declared in a `docs/API.md` table is actually served by an application or listed in the explicit allowlist of non-FastAPI surfaces (the cross-region aggregator Lambda, the Mooncake prefill/decode proxy). Paths are compared with parameter names erased so the tables may keep readable shorthand like `/api/v1/jobs/{ns}/{name}`. Also asserts the committed OpenAPI documents in `docs/openapi/` match what the applications produce, so a route change cannot land with stale schemas. |
| `test_floci_dynamodb_stores.py` | Floci emulator layer (opt-in via `GCO_FLOCI_ENDPOINT`; see `docs/FLOCI_TESTING.md`): the production DynamoDB store classes (`TemplateStore`, `WebhookStore`, `JobStore`, `InferenceEndpointStore`) over the real wire protocol against tables shaped exactly like `gco/stacks/global_stack.py` provisions them — server-enforced conditional writes and duplicate rejection, idempotent job replay vs conflict, the region-status GSI the queue worker reads, exclusive fenced claims, namespace-GSI webhook queries, event routing, and multi-page scan pagination that follows real `LastEvaluatedKey` cursors. |
| `test_floci_harness_inventory.py` | Floci emulator layer: the live-validation harness's inventory machinery through its own `ThrottleResilientSession` — partition/EC2 cross-checked region discovery, the fail-closed project scanners observing then proving absence of created SQS/DynamoDB/CloudFormation resources (the exact final-inventory gate), and protected-baseline capture/comparison detecting protected-stack loss. Applies the two documented Floci-gap shims from `tests/_floci_gap_shims.py` (unparseable `GetStackPolicy`; Global Accelerator absent). |
| `test_floci_lambda_orchestration.py` | Floci emulator layer: control-plane Lambda handlers run unmodified against real emulated services — the `secret-rotation` four-step Secrets Manager protocol (AWSPENDING staging, per-token idempotency, promotion to AWSCURRENT, invalid-step rejection); the `helm-orchestrator` fire-and-forget provider (real Step Functions execution start, zlib+base64 SSM replay-input round trip with raw `{{PLACEHOLDER}}` tokens, execution-identity persistence with raw-JSON digest, retry adoption via `ExecutionAlreadyExists`, non-identical-input refusal, teardown-fence blocking and Create-time clearing); the `helm-installer` teardown provider (fence write, stop of running install executions, deterministic `helm-delete-*` execution reuse on provider retry, `is_complete` mapping of RUNNING/SUCCEEDED/FAILED with Fail-state cause, drain task); and `cross-region-aggregator` bridge discovery against real CloudFormation stacks (output validation, fail-closed missing/invalid bridges, bounded-stale cache surviving a discovery outage, 404/503 routing shields). |
| `test_grafana_dashboards.py` | Static validation of the curated Grafana dashboard ConfigMaps in `lambda/kubectl-applier-simple/manifests/` — parses every payload through the applier's real `plan_manifests` path (same substitution and feature gating production uses) and asserts: valid JSON with title/uid/schemaVersion, uids unique and within Grafana's import limits, panels carry types, unique ids, and 24-column-grid-safe positions, every target has a PromQL `expr`, lowercase legend tokens (`{{Hostname}}`, `{{gpu}}`, `{{namespace}}`) survive placeholder substitution, and the sidecar import contract (label, namespace, `.json` keys) holds. Also locks the `.github/scripts/validate_grafana_dashboards.py` extraction to the applier path (same uid set, readable chart pin, malformed payloads rejected loudly) — the companion `grafana-dashboards.yml` workflow boots the pinned chart's actual Grafana image against the same extraction. |
| `test_floci_lambda_regional.py` | Floci emulator layer: regional data-plane Lambda handlers — `capacity-poller` writing a real DynamoDB snapshot through the emulator's rejecting EC2 capacity APIs (spot fields absent rather than zeroed, long-tier omission when disabled, config validation); `image-lookup` ECR adopt-or-create/retain/destroy (runs fully on the CI emulator; local Finch hosts skip with the documented `CreateRepository` gap); `regional-api-proxy` registry-driven ALB resolution against a real internal ALB (SSM parameter → DNS shape → ELBv2 ownership → Gateway tag, with fail-closed rejections for missing parameters, foreign accounts, and untagged ALBs); and the `ga-registration` SSM registry round trip plus exact-ownership tag/hostname ALB discovery. |
| `test_floci_live_validation_e2e.py` | Floci emulator layer, deepest stage: runs the real `gco release validate --emulator-endpoint` command, which executes the unmodified live-validation harness as a subprocess against the emulator inside CI — the verified emulator opt-in in `require_local_execution`, full preflight (git identity pinning, STS account verification, EC2 region discovery, `cdk list` over the real cloud assembly, per-region CDKToolkit health, fresh-run refusal of pre-existing project stacks), baseline capture, report/checkpoint writing, and PARTIAL-status semantics for subset runs; plus the negative path proving an account mismatch fails the run and still writes its report. Requires the Node CDK toolchain. |
| `test_floci_mission_memory.py` | Floci emulator layer: the mission-memory store's plain-DynamoDB paths over the real wire protocol against a table shaped like `gco/stacks/global_stack.py` provisions it (minus the vector index, which the emulator cannot create; Bedrock embedding is stubbed). Validates what no client-side fake can — the hand-rolled typed-attribute serialisation in `write_memory` round-trips (L-of-N embedding vectors including scientific-notation number strings, TTL numbers, string lists), `PutItem` overwrite semantics back the backfill idempotency claim, `list_memories`' `ProjectionExpression` is valid server-side and actually suppresses the embedding vector, and the multi-page `Scan` loop follows real `LastEvaluatedKey` cursors. The final test pins the `SearchVectors` gap itself: `search_similar` against an environment without the API surfaces a typed `MissionMemoryError`, and fails loudly the day the emulator implements it. |
| `test_floci_secrets_and_cost.py` | Floci emulator layer: `auth_middleware` token loading against a real Secrets Manager secret (ARN region parsing, rotation overlap where AWSCURRENT and AWSPENDING both validate, fail-closed empty token set for an unfetchable secret) and the `CostMonitor` S3 pipeline (real Parquet upload, `list_reports` round trip, byte-level Parquet read-back, and per-window scheduled idempotency via `head_object`) with OpenCost stubbed by a local HTTP server. |
| `test_floci_sqs_job_path.py` | Floci emulator layer: `queue_processor.process_one_message` against a production-shaped main-queue + DLQ redrive pair — clean empty poll, full consume-and-delete with only the K8s apply boundary patched, and the security-critical retention path: a policy-rejected job is never acknowledged and, after `maxReceiveCount` receives, the server's redrive moves the exact payload to the DLQ while the main queue stops serving it. |
| `test_floci_vector_cli.py` | Floci emulator layer: the `gco vector` CLI core (`cli/vector_store.py`, only Bedrock stubbed) run exactly as an operator runs it — SSM name discovery under a per-test project namespace, corpus uploads landing in a real bucket, the `--wait` chunk count through a genuine server-side `FilterExpression`, `status` against a real table description (index degrades to NOT_VISIBLE, never a KeyError), the ParameterNotFound → unavailable mapping over the wire, and the `SearchVectors` gap surfacing as a typed error. |
| `test_floci_vector_ingest.py` | Floci emulator layer: the production `lambda/vector-ingest/handler.py` (loaded as Lambda loads it, only Bedrock stubbed) against real S3 `GetObject` and DynamoDB `PutItem` over the wire — markdown round-trip with typed L-of-N embedding (including a scientific-notation component), at-least-once redelivery overwriting via deterministic `doc_id`s, `.jsonl` multi-item ingestion, and a real `NoSuchKey` driving the per-object failure path. Two tests pin the emulator's vector-index gap exactly as probed on Floci 1.6.0: the stack's `UpdateTable` + `VectorIndexUpdates` call is accepted but silently dropped, and `SearchVectors` answers `UnknownOperationException` — either failing after a Floci bump is the signal to grow real index coverage. |
| `test_floci_stack_discovery.py` | Floci emulator layer: `GCOAWSClient` stack discovery against real CloudFormation stacks — the configured-region fast path reading regional stack outputs (cluster name, EFS id), the missing-stack `ClientError`-to-`None` path, global API endpoint resolution from stack outputs with api-id parsing, and TTL-cache behavior proven by deleting the backing stack. |
| `test_gateway_route_coverage.py` | Guards the shared ALB `HTTPRoute` (`gco-system/gco-routes` in `post-helm-gateway.yaml`) against sending traffic to a Service that does not serve it. Resolves every live path from `docs/openapi/` through the manifest's `PathPrefix` rules using Gateway API precedence (longest segment-wise match wins, independent of document order) and fails when the winning backend lacks the route — the bug that left the health monitor's `/api/v1/metrics` answering `404` through both API Gateways because the `/` catch-all claimed it. Also pins the intended winner for every path more than one application implements, each with its reason (notably `/api/v1/status` staying with the manifest processor, whose fields the cross-region aggregator consumes), fails when a *newly* shared path has no recorded decision, asserts the cost monitor is never an ALB backend and its `/internal/*` paths never become routable, and keeps the rule list ordered most-specific-first with a single trailing catch-all so the file reads the way traffic resolves. |
| `test_api_shared.py` | Tests for shared API helpers in gco/services/api_shared.py. |
| `test_bug_fixes.py` | Regression tests for a handful of bug fixes across the GCO codebase. |
| `test_canary.py` | Tests for A/B (canary) inference endpoint deployments. |
| `test_capacity.py` | Tests for cli/capacity/ — the GPU capacity checker and recommender. |
| `test_capacity_advisor_coverage.py` | Coverage for the capacity advisor prompt builder (`cli/capacity/advisor.py`) and the multi-region aggregator (`cli/capacity/multi_region.py`): recommendation tie-breaks and SQS and CloudWatch error handling. |
| `test_capacity_advisor_historical.py` | Tests for the Bedrock capacity-advisor historical-context enrichment in cli/capacity/advisor.py. |
| `test_capacity_block_search.py` | Tests for the [Capacity Block](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html) search expansion — `cli/capacity/blocks.py` duration/normalization/pricing helpers, `validate_instance_type`, date-range + pagination on `list_capacity_block_offerings`, the consolidated `find_capacity_blocks` region x duration sweep (de-dup, ranking, longest, the p6-b200 acceptance scenario), multi-region `check_reservation_availability`, and the `gco capacity find-blocks` CLI plus `find_capacity_blocks` / `reservation_check` MCP argv. |
| `test_capacity_checker_coverage.py` | Error, empty-result, and scarcity-assessment coverage for the capacity checker (`cli/capacity/checker.py`): boto3 ClientError paths, availability bands, and reservation and capacity-block discovery. |
| `test_capacity_cmd_coverage.py` | Tests for the capacity CLI subcommands in cli/commands/capacity_cmd.py. |
| `test_capacity_history.py` | Tests for cli/capacity/history.CapacityHistoryStore (time-series store, statistics, and temporal patterns). |
| `test_capacity_history_cli.py` | Tests for the `gco capacity history` show/stats/patterns subcommands and the `--enrich-historical` flag. |
| `test_capacity_history_config.py` | Tests for the `historical.*` config getters and validation in gco/config/config_loader.py. |
| `test_capacity_poller_handler.py` | Tests for the capacity-poller Lambda (lambda/capacity-poller/handler.py). |
| `test_capacity_poller_stack.py` | CDK synthesis tests for the capacity-poller add-on folded into gco/stacks/global_stack.GCOGlobalStack (present when historical.enabled, absent otherwise). |
| `test_capacity_reservations.py` | Tests for On-Demand Capacity Reservations and EC2 Capacity Blocks. |
| `test_cli_config.py` | Tests for cli/config.py. |
| `test_cli_inference_models.py` | Tests for the inference and models CLI subgroups in cli/main.py. |
| `test_cloudwatch_logs_fallback.py` | Tests for JobManager.get_job_logs [CloudWatch Logs](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/WhatIsCloudWatchLogs.html) fallback. |
| `test_cluster_observability_charts.py` | Regional-stack wiring for kube-prometheus-stack — chart-enable membership, value overrides, the gp3 StorageClass manifest, and the pure `_compute_kubectl_observability_replacements` gate helper. |
| `test_cluster_observability_cli.py` | The `gco monitoring` CLI (status/enable/disable/open) plus the validated `kubectl port-forward` and SSM remote-host tunnel argv builders, including private-endpoint detection and the `--via-ssm` path. |
| `test_cluster_observability_config.py` | `ConfigLoader.get_cluster_observability_config` defaults/merge and `_validate_cluster_observability_config` (enabled/persistence/retention/rotation-schedule validation), with a Hypothesis toggle round-trip. |
| `test_cluster_observability_dashboards.py` | The curated Grafana dashboard ConfigMaps — four dashboards, the `grafana_dashboard` sidecar label, the observability gate annotation, valid dashboard JSON, and the UPPER_SNAKE-only placeholder guard. |
| `test_cluster_observability_rotation.py` | The Grafana admin-password rotation module (`gco/services/grafana_rotator.py`) with mocked k8s client + HTTP, plus the gated rotation CronJob + least-privilege RBAC manifest assertions. |
| `test_cluster_observability_screenshots.py` | The dashboard screenshot tooling (`scripts/capture_monitoring_screenshots.py`) — the capture targets stay in lockstep with the shipped dashboard ConfigMaps, output paths are PNGs under the repo `images/` directory, and `main()` wiring (Playwright `capture` mocked). |
| `test_cluster_observability_service_metrics.py` | The GCO-service Prometheus instrumentation in `gco/services/service_metrics.py` (RED metrics + the scrape-time collector). |
| `test_cluster_observability_users.py` | Grafana user management (`cli/monitoring_user_mgmt.py`) over the admin HTTP API and the `gco monitoring users` subcommands, with mocked requests + kubectl. |
| `test_cluster_tunnel.py` | The shared cluster-tunnel core (`cli/cluster_tunnel.py`): `TunnelPlan` connection-plan builders, the `open_api_server_tunnel` lifecycle (all branches), the `gco cluster tunnel` command (interactive + `--print`), and the `gco monitoring open --via-ssm auto` bastion path. |
| `test_cost_api_routes.py` | The `/api/v1/cost/*` proxy router on the manifest API — happy relays to the internal cost-monitor service, error-status propagation, connection failures mapping to a clear 503, non-JSON/non-object bodies mapping to 502, the `COST_MONITOR_URL` override, and router registration. |
| `test_cost_api_service.py` | The cost-monitor HTTP service (`gco/services/cost_api.py`) — probes, `/internal/status`, report listing and ad-hoc generation with error mapping (OpenCost 503, S3 502, window 422), readiness coupling to the scheduled reporter task, and the scheduled loop's failure isolation. |
| `test_cost_monitor_service.py` | The cost-monitor core (`gco/services/cost_monitor.py`) — the OpenCost allocation client, row normalization, real pyarrow Parquet serialization, deterministic scheduled report keys, aligned window math, the CostMonitor orchestrator (generate/skip/list/status), and the environment factory. |
| `test_cost_monitoring_config.py` | `ConfigLoader.get_cost_monitoring_config` defaults/merge, `_validate_cost_monitoring_config` type/range and lifecycle-invariant validation, and the effective-enable conjunction with `cluster_observability`. |
| `test_cost_monitoring_stack.py` | The cost pipeline in the monitoring stack — the deterministic cost report bucket (KMS, deny-insecure-transport, cdk.json-driven lifecycle rules), the Glue database/table with partition projection matching the service's write layout, the enforced KMS-encrypted Athena workgroup, and full absence when disabled. |
| `test_cost_opencost_charts.py` | OpenCost chart wiring and cost manifests — the pinned charts.yaml entry (monitoring namespace, Prometheus wiring, ServiceMonitor, MCP off, non-blocking install), regional chart-enable/override behavior under the toggle conjunction, the gated cost-monitor and Grafana cost dashboard manifests, the applier prune inventory, and the tunnel service entries. |
| `test_costs.py` | Tests for the cost-visibility feature in cli/costs.py. |
| `test_costs_cli_analytics.py` | Athena-backed cost analytics (`cli/cost_analytics.py`) — query execution/polling/failure/timeout, parameterized filters, canned aggregation SQL shapes, identifier lockstep with the stack constants — plus the `gco costs k8s`/`report` command surfaces and the cost API transport-region resolution. |
| `test_costs_cmd_extended.py` | Extended tests for cli/commands/costs_cmd.py. |
| `test_cross_region_aggregator_extended.py` | Extended coverage tests for the cross-region aggregator Lambda. |
| `test_dag.py` | Tests for the job-DAG pipeline feature in cli/dag.py. |
| `test_default_bedrock_model_consistency.py` | Guards that each `cdk.json` `context.bedrock` model default (`mission_default_model_id`, `capacity_advisor_default_model_id`, `claude_code_default_model_id`, `embedding_model_id`) is its consumer's sole operational source through `gco.bedrock`, the retired pre-v6 `default_model_id` key and its module aliases stay removed (fail-closed rename error), the canonical file ships as package data, and the configured Mission profile has a complete scaffold replay fixture. |
| `test_drift_detection.py` | Tests for the CloudFormation drift-detection resources on the regional stack. |
| `test_ephemeral_bastion.py` | The ephemeral SSM bastion lifecycle (`cli/ephemeral_bastion.py`): validated `aws` CLI argv builders with orphan safeguards (IMDSv2, shutdown-terminate, self-terminate user-data, `gco:ephemeral` tags), network/AMI discovery, and the atomic create/destroy lifecycle, with the AWS CLI shell-out mocked. |
| `test_ga_registration.py` | Tests for the Global Accelerator registration Lambda (lambda/ga-registration/handler.py). |
| `test_health_check_coverage.py` | Consistency tests between ALB Ingress health-check paths and the auth middleware allowlist. |
| `test_helm_orchestrator_handler.py` | Unit tests for the helm-orchestrator custom-resource provider handler. |
| `test_helm_teardown_provider.py` | Delete-only Helm teardown provider contracts — Create/Update no-ops, deterministic and idempotent [Step Functions](https://docs.aws.amazon.com/step-functions/latest/dg/welcome.html) execution, in-flight installer cancellation and drain, completion polling, and failed-uninstall propagation. |
| `test_inference_gpu_autoscaling.py` | GPU-aware autoscaling routes through a KEDA ScaledObject. |
| `test_inference_manager_extended.py` | Extended tests for cli/inference.InferenceManager. |
| `test_inference_store.py` | Tests for gco/services/inference_store.InferenceEndpointStore. |
| `test_jobs_dag_extended.py` | Extended tests for cli/jobs.py and cli/dag.py. |
| `test_feature_enablement_overrides.py` | The `feature_enabled_overrides` CDK context (`gco/config/config_loader.py`): parsing/validation of the comma-or-list override value against the documented three feature keys (`aurora_pgvector`, `valkey`, `fsx_lustre`), each getter's forced enablement under override (selective, non-clobbering of other settings, never weakening an explicit cdk.json enablement), defaults staying disabled without it, and unknown names failing at config read with the valid list. Uses the repository's real cdk.json context so required-field drift can't silently skip validation. |
| `test_helm_enablement_overrides.py` | The `helm_enabled_overrides` CDK context: parsing/validation of the comma-separated override value (`gco.stacks.regional_stack._parse_helm_enabled_overrides`), the single enablement resolver shared by the chart map and the applier gates (`_helm_chart_enabled`: mandatory charts ignore toggles, overrides force on, missing keys default enabled), and the `{{KUEUE_ENABLED}}`/`{{SLURM_ENABLED}}` gate replacements that make the Kueue default queues and Slurm NetworkPolicies apply exactly when their scheduler does. |
| `test_kubectl_applier.py` | Tests for the kubectl-applier Lambda (lambda/kubectl-applier-simple/handler.py). |
| `test_kubeflow_trainer_charts.py` | Kubeflow Trainer chart wiring and TrainJob gating — the pinned charts.yaml entry (OCI source, chart version + controller image tag in lockstep, chart-shipped runtimes disabled, own namespace, bounded wait, before-kueue ordering), regional enablement in both directions plus the `helm_enabled_overrides` force-on path, the `{{KUBEFLOW_TRAINER_ENABLED}}` gate replacements, the extracted torch-distributed runtime manifest and its exact-GVK prune inventory, CRUD acceptance of the pinned TrainJob GVK only, helm-installer `handle_task` convergence both ways, and the deliberate absence of a finalizer pre-purge (verified against upstream v2.3.0). |
| `test_kubectl_helpers.py` | Tests for cli/kubectl_helpers.update_kubeconfig. |
| `test_kubectl_helpers_extended.py` | Extended tests for cli/kubectl_helpers.update_kubeconfig. |
| `test_lambda_handlers.py` | Tests for the active Secrets Manager HMAC signing-key rotation Lambda. |
| `test_lambda_handlers_extended.py` | Extended signing-key rotation tests covering promotion, validation, idempotency, and generation. |
| `test_lambda_proxy.py` | Tests for the Lambda proxy handlers and shared proxy_utils. |
| `test_manifest_property.py` | Property-based tests for manifest validation and YAML parsing. |
| `test_mcp_cluster_tool.py` | The `cluster_tunnel_command` MCP tool (`gco_mcp/tools/cluster.py`) — asserts the `gco cluster tunnel --print` argv it constructs, with the CLI subprocess mocked. |
| `test_mcp_iam_role.py` | CDK assertion tests for the dedicated MCP server IAM role on the regional stack. |
| `test_mcp_self_resources.py` | Tests for the self-indexing MCP resources (``mcp://gco/...``). |
| `test_mcp_task_tools.py` | Tests for the read-only MCP observability tools (``task_status`` and ``task_tail``) and the matching ``gco tasks`` CLI surface. |
| `test_model_bucket_access_logs.py` | Tests for S3 server access logging on the model weights bucket. |
| `test_models_cli.py` | Tests for cli/models.ModelManager — S3 model weight management. |
| `test_network_policies_manifest.py` | Tests for the NetworkPolicy manifest at lambda/kubectl-applier-simple/manifests/03-network-policies.yaml. |
| `test_nodepools_extended.py` | Extended tests for cli/nodepools.py. |
| `test_proxy_utils_extended.py` | Extended tests for lambda/proxy-shared/proxy_utils.py. |
| `test_python_base_image_consistency.py` | Python base-image pins stay consistent across services and CI. |
| `test_regional_api_gateway_stack.py` | Tests for gco/stacks/regional_api_gateway_stack.GCORegionalApiGatewayStack. |
| `test_request_size_limit.py` | Tests for the RequestSizeLimitMiddleware on the Manifest API. |
| `test_resource_quota_config.py` | End-to-end tests that cdk.json resource quota values flow into the Kubernetes manifests applied on the cluster. |
| `test_resource_quota_manifest.py` | Tests for the ResourceQuota + LimitRange manifest (lambda/kubectl-applier-simple/manifests/04-resource-quotas.yaml). |
| `test_stacks_access.py` | Tests for `gco stacks access` — the kubectl bootstrap command in cli/commands/stacks_cmd.py. |
| `test_stacks_ordering_fsx.py` | Tests for stack ordering helpers and FSx configuration in cli/stacks.py. |
| `test_task_status.py` | Tests for the disk-backed task status writer. |
| `test_task_status_coverage.py` | Branch coverage for the disk-backed task-status reader and writer (`gco_mcp/tools/_task_status.py`): orphaned-PID rewrite, prune edge cases, and malformed-record guards. |
| `test_tasks_cmd_coverage.py` | Coverage for the `gco tasks` command helpers and subcommands (`cli/commands/tasks_cmd.py`): PID liveness, state colorization, duration formatting, and the list, show, tail, and prune paths. |
| `test_tls_certificate_manager.py` | Tests for the TLS certificate manager Lambda: strict project ownership, ACM certificate discovery and reconciliation, SSM state recovery, cryptographic legacy-certificate migration, and safe cleanup of managed resources. |
| `test_trusted_registries_augmentation.py` | Tests for ``_augment_trusted_registries_with_project_ecr``. |
| `test_vector_cli.py` | Tests for the `gco vector` CLI: the `VectorStoreClient` core (`cli/vector_store.py`) with stubbed clients — SearchVectors request shape (plain N-attr `SearchVector`, `--source` inline condition) and response parse, the building-index/absent-table → unavailable taxonomy, status describe walk (defensive vector-index key scan), ingest upload/suffix-refusal/`--wait` polling with real pagination cursors, cached SSM resolution with the ParameterNotFound hint; the click veneer via CliRunner (option forwarding, envelopes, exit codes); the client-defaults-vs-`cdk.json` `vector_store` block agreement; and the cross-implementation Titan embedding contract pinning the CLI, the ingest Lambda, and mission memory's embedder to one request shape. |
| `test_vector_ingest_handler.py` | Tests for the S3-triggered vector-store ingest Lambda (`lambda/vector-ingest/handler.py`) via `load_lambda_module` with stubbed clients: the deterministic ~2000-char paragraph chunker (packing, hard-split, CRLF, title extraction), the pre-chunked `.jsonl` path and its per-line validation, the Titan request contract (`dimensions` key by model family) and vector-width fail-closed check, and the handler's event walk — URL-decoded keys, prefix guard, suffix routing, per-object isolation with batch-level re-raise, provenance-laden item shapes with deterministic `doc_id`s, and env fail-closed. |
| `test_vector_store_config.py` | Tests for the `vector_store.*` config getters and validation in gco/config/config_loader.py — shipped defaults (feature OFF), cdk.json/code default agreement, replica-region derivation from `deployment_regions`, partial-override merging, and every validation error path including the one-way-door `dimensions` / `distance_function` fields. |
| `test_vector_store_stack.py` | CDK synthesis tests for the vector-store add-on across both stacks (gated by `vector_store.enabled`, OFF by default). GCOGlobalStack: the `AWS::DynamoDB::GlobalTable` shape (PITR per replica, SSE, on-demand), replica derivation (regional deployments minus the global region; explicit `replica_regions` win and the primary is stripped), the index custom resource's live-earned payload pins (flat CreateVectorIndexAction + same-call AttributeDefinitions, InstallLatestAwsSdk, delete-path error swallowing, botocore model walk), table-scoped index role, SSM/backup wiring, and the ingest pipeline (prefix-filtered OBJECT_CREATED notification, 5min/512MB function with async DLQ and env contract, write-only role with no read-path actions). GCORegionalStack: the `{{VECTOR_STORE_*}}` ConfigMap replacements as synth-time literals and the workload read grant on LOCAL-region exact ARNs (`SearchVectors`/`GetItem`/`Query` + `bedrock:InvokeModel` on the embedding model). Disabled paths carry zero feature traces in either stack — no bucket notification, no replacements, no grants. |
| `test_waf_rate_limit.py` | Tests for the WAF PerIPRateLimit rule on GCOApiGatewayGlobalStack. |
| `test_webhook_dispatcher.py` | Tests for gco/services/webhook_dispatcher.WebhookDispatcher. |
| `test_yaml_parsing_limits.py` | Tests for YAML parsing limits on the manifest processor. |

## Writing New Tests

### General Guidelines

1. **Use descriptive test names**: Test names should describe what is being tested and the expected outcome.

   ```python
   def test_submit_manifest_with_invalid_namespace_returns_403(): ...
   ```

2. **One assertion per test when possible**: Makes failures easier to diagnose.

3. **Use fixtures for common setup**: Define reusable fixtures in `conftest.py` or at the module level.

4. **Test both success and failure paths**: Don't just test the happy path.

5. **Mock external dependencies**: Use `unittest.mock` to isolate tests from external services.

### Test Structure

```python
"""
Tests for [component name].

Brief description of what this test file covers.
"""

from unittest.mock import MagicMock, patch, AsyncMock
import pytest


@pytest.fixture
def mock_dependency():
    """Fixture description."""
    mock = MagicMock()
    mock.some_method.return_value = "expected_value"
    return mock


class TestFeatureName:
    """Tests for [feature name]."""

    def test_success_case(self, mock_dependency):
        """Test description."""
        # Arrange
        ...

        # Act
        result = function_under_test()

        # Assert
        assert result == expected

    def test_error_case(self, mock_dependency):
        """Test error handling."""
        mock_dependency.some_method.side_effect = Exception("Error")

        with pytest.raises(Exception):
            function_under_test()
```

## Mocking Patterns

### Mocking FastAPI Applications

When testing FastAPI endpoints, you need to mock both the factory functions AND the module-level variables:

```python
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi.testclient import TestClient


def test_api_endpoint(mock_manifest_processor):
    """Test an API endpoint with proper mocking."""
    mock_job_store = MagicMock()
    mock_job_store.list_jobs.return_value = [{"job_id": "abc123"}]

    with (
        patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ),
        patch("gco.services.manifest_api.get_template_store", return_value=MagicMock()),
        patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
        patch("gco.services.manifest_api.get_job_store", return_value=mock_job_store),
    ):
        # IMPORTANT: Also set the module-level variables directly
        import gco.services.manifest_api as api_module

        api_module.manifest_processor = mock_manifest_processor
        api_module.job_store = mock_job_store

        from gco.services.manifest_api import app

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/queue/jobs")
            assert response.status_code == 200
```

### Mocking Async Functions

Use `AsyncMock` for async functions:

```python
from unittest.mock import AsyncMock

mock_processor.process_manifest_submission = AsyncMock(return_value=mock_result)
```

### Mocking Kubernetes API

```python
@pytest.fixture
def mock_manifest_processor():
    """Fixture to mock the manifest processor."""
    mock_processor = MagicMock()
    mock_processor.cluster_id = "test-cluster"
    mock_processor.region = "us-east-1"
    mock_processor.core_v1 = MagicMock()
    mock_processor.batch_v1 = MagicMock()
    mock_processor.custom_objects = MagicMock()
    mock_processor.max_cpu_per_manifest = 10000
    mock_processor.max_memory_per_manifest = 34359738368
    mock_processor.max_gpu_per_manifest = 4
    mock_processor.allowed_namespaces = {"default", "gco-jobs"}
    mock_processor.validation_enabled = True
    return mock_processor
```

### Mocking DynamoDB

```python
from unittest.mock import MagicMock, patch


@pytest.fixture
def mock_dynamodb():
    """Mock DynamoDB table."""
    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": {"name": "test"}}
    mock_table.put_item.return_value = {}
    mock_table.scan.return_value = {"Items": []}
    return mock_table
```

### Providing Valid Kubernetes Manifests

When testing endpoints that process Kubernetes manifests, provide complete manifests:

```python
valid_job_manifest = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {"name": "test-job"},
    "spec": {
        "template": {
            "spec": {
                "containers": [{"name": "main", "image": "test:latest"}],
                "restartPolicy": "Never",
            }
        }
    },
}
```

## Coverage Requirements

The global Python line + branch coverage floor is 90% across `gco/`, `cli/`,
and `gco_mcp/` in `unit:pytest:core`; pull requests and releases target ~92%
measured coverage. The dedicated streaming-Lambda workflow independently
enforces at least 93% lines, functions, and branches with Node 24's built-in
V8 coverage. Current measured values come from CI artifacts; add tests to hold
the ~92% Python target rather than raising or lowering the global floor.

To check the Python report:

```bash
python -m pytest --cov=gco --cov=cli --cov=gco_mcp --cov-report=term-missing
```

The streaming-Lambda graph uses:

```bash
npm ci --prefix lambda/inference-streaming-proxy --ignore-scripts --no-audit --no-fund
npm --prefix lambda/inference-streaming-proxy test
```

To generate an HTML coverage report:

```bash
python -m pytest --cov=gco --cov=cli --cov=gco_mcp --cov-report=html
open htmlcov/index.html
```

### Areas Needing Coverage

When adding new features, ensure tests cover:

1. **Success paths**: Normal operation with valid inputs
2. **Error paths**: Invalid inputs, missing data, exceptions
3. **Edge cases**: Empty lists, None values, boundary conditions
4. **Authentication**: Both authenticated and unauthenticated requests
5. **Authorization**: Namespace restrictions, permission checks

## Type Checking

CI runs `mypy --strict` across three jobs:

- `lint:typecheck` — `gco/` (except stacks), `cli/`, `gco_mcp/`, `scripts/`, `app.py`
- `lint:typecheck-stacks` — `gco/stacks/` with `aws-cdk-lib` installed
- `lint:typecheck-lambda` — each `lambda/*/` directory individually

Strict flags enabled in `pyproject.toml` include `disallow_untyped_defs`,
`disallow_untyped_calls`, `disallow_any_generics`, `no_implicit_optional`,
`warn_return_any`, and `warn_unused_ignores`. Test files relax
`disallow_untyped_defs` so fixture and helper signatures can stay concise.

Prefer concrete types over `Any`. Runtime types from the installed packages
(boto3, Kubernetes, fastapi, click) are preferred over `Any` fallbacks —
the CI typecheck jobs install the full runtime (`pip install -e ".[typecheck,mcp]"`)
so stubs resolve properly.

Run locally with the same commands:

```bash
mypy gco/ cli/ gco_mcp/ scripts/ app.py --exclude 'gco/stacks/'
mypy gco/stacks/
for d in lambda/*/; do ls "$d"*.py >/dev/null 2>&1 && mypy "$d"; done
```

## Import Conventions After the Manifest API Refactor

The manifest API was split into several modules. When writing tests that
import endpoint functions or shared helpers, import from the module they
actually live in, not from `gco.services.manifest_api`:

| Symbol | Import from |
|--------|-------------|
| Endpoint functions (`submit_manifests`, `list_jobs`, `delete_job`, etc.) | `gco.services.api_routes.{manifests,jobs,queue,templates,webhooks}` |
| Pydantic models (`ManifestSubmissionAPIRequest`, `BulkDeleteRequest`, `ResourceIdentifier`, `JobStatus`, `WebhookEvent`, etc.) | `gco.services.api_shared` |
| Helper parsers (`_parse_job_to_dict`, `_parse_pod_to_dict`, `_parse_event_to_dict`, `_apply_template_parameters`, `_check_namespace`, `_check_processor`) | `gco.services.api_shared` |
| App itself, lifecycle, health probes (`app`, `lifespan`, `create_app`, `health_check`, `kubernetes_readiness_check`, `global_exception_handler`, `get_service_status`, `DEFAULT_MAX_REQUEST_BODY_BYTES`) | `gco.services.manifest_api` |

`manifest_api.py` no longer re-exports the moved symbols — importing from
the wrong module will now fail at collection time instead of silently
masking drift.

## Hermetic Environment Variables

Several services read configuration from `os.environ` at module import
time (`queue_processor.py`, `manifest_processor.py`). Tests that cover
these services must not leak env vars to later tests. Two patterns
handle this:

1. **Use `monkeypatch.setenv` / `monkeypatch.delenv`** — pytest cleans
   up automatically between tests.
2. **Autouse scrub fixture** — for files that reload modules via
   `importlib.reload`, declare a module-level autouse fixture that
   calls `monkeypatch.delenv(name, raising=False)` on every variable
   the module reads. See `tests/test_queue_processor.py::_scrub_qp_env`
   for the canonical pattern.

Never set env vars via `os.environ["X"] = "..."` directly in a test body
without a tear-down — it will leak into unrelated tests that run later
in the same session.

## Common Issues

### Import Errors

If you see import errors, ensure you're running tests from the project root:

```bash
python -m pytest tests/test_file.py
```

### Async Test Warnings

The project uses `pytest-asyncio`. Async tests are automatically detected.

### Module Caching

FastAPI apps can be cached between tests. Use fresh imports within test functions:

```python
def test_something():
    with patch(...):
        from gco.services.manifest_api import app

        with TestClient(app) as client:
            ...
```
