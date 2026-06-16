# MCP Tools

MCP tool definitions — one file per domain. Each module registers tools against the shared FastMCP server instance via `@mcp.tool()` decorators.

## Table of Contents

- [Files](#files)
- [How Tools Work](#how-tools-work)
- [Adding a New Tool](#adding-a-new-tool)

## Files

Counts are tools registered per module; tools gated behind a feature flag only
appear when that flag (or the umbrella `GCO_ENABLE_ALL_TOOLS`) is set. At
default registration the server exposes 98 tools; with every flag enabled the
ceiling is 130. See [Feature Flags](../README.md#feature-flags) for the
flag-to-tool mapping.

| File | Tools | Description |
|------|-------|-------------|
| `jobs.py` | 9 | `list_jobs`, `submit_job_sqs`, `submit_job_api`, `get_job`, `get_job_logs`, `get_job_events`, `delete_job` (gated), `cluster_health`, `queue_status` |
| `capacity.py` | 8 | `check_capacity`, `capacity_status`, `recommend_region`, `spot_prices`, `ai_recommend`, `list_reservations`, `reservation_check`, `reserve_capacity` (gated) |
| `inference.py` | 18 | `deploy_inference`, `list_inference_endpoints`, `inference_status`, `scale_inference`, `update_inference_image`, `stop_inference`, `start_inference`, `delete_inference` (gated), `canary_deploy`, `promote_canary`, `rollback_canary`, `invoke_inference`, `chat_inference`, `inference_health`, `list_endpoint_models`, `deploy_disaggregated_inference`, `set_mooncake_topology`, `mooncake_topology_status` |
| `costs.py` | 4 | `cost_summary`, `cost_by_region`, `cost_trend`, `cost_forecast` |
| `stacks.py` | 20 | `list_stacks`, `stack_status`, `setup_cluster_access`, `fsx_status`, `stack_diff`, `stack_outputs`, `stack_synth`, `valkey_status`, `aurora_status`, `enable_fsx`, `disable_fsx`, `enable_valkey`, `disable_valkey`, `enable_aurora`, `disable_aurora`, `deploy_stack` (gated), `deploy_all` (gated), `bootstrap_cdk` (gated), `destroy_stack` (gated), `destroy_all` (gated) |
| `storage.py` | 4 | `list_storage_contents`, `list_file_systems`, `files_get`, `files_access_points` |
| `models.py` | 4 | `list_models`, `get_model_uri`, `models_upload` (gated), `delete_model` (gated) |
| `nodepools.py` | 4 | `nodepools_list`, `nodepools_describe`, `nodepools_create_odcr`, `delete_nodepool` (gated) |
| `analytics.py` | 7 | `analytics_doctor`, `analytics_login_url`, `analytics_users_list`, `enable_analytics`, `disable_analytics`, `analytics_user_add`, `analytics_user_remove` (gated) |
| `templates.py` | 5 | `templates_list`, `templates_get`, `templates_create`, `templates_run`, `delete_template` (gated) |
| `webhooks.py` | 4 | `webhooks_list`, `webhooks_get`, `webhooks_create`, `delete_webhook` (gated) |
| `queue.py` | 5 | `queue_list`, `queue_get`, `queue_stats`, `queue_submit`, `cancel_queue_job` (gated) |
| `images.py` | 17 | `images_list`, `images_tags`, `images_describe`, `images_uri`, `images_replication_get`, `images_replication_status`, `images_orphans`, `images_init`, `images_lifecycle_get`, `images_lifecycle_set`, `images_replication_sync`, `images_build` (gated), `images_push` (gated), `images_delete_tag` (gated), `images_delete_repo` (gated), `images_cleanup` (gated), `images_prune` (gated) |
| `dag.py` | 2 | `dag_validate`, `dag_run` |
| `config.py` | 1 | `config_get` |
| `metrics.py` | 4 | `metrics_cloudwatch_get`, `metrics_from_job_logs`, `metrics_from_shared_storage_file` (default-on); `metrics_from_local_file` (gated by `GCO_ENABLE_LOCAL_METRICS`, default-off) — all `safe` |
| `semantic_progress.py` | 1 | `metrics_semantic_progress` (gated by `GCO_ENABLE_SEMANTIC_PROGRESS`, default-off) — `safe` LLM-as-judge progress score |
| `mission.py` | 9 | `mission_start`, `mission_status`, `mission_iterate`, `mission_checkpoint`, `mission_complete`, `mission_abort`, `mission_resume`, `mission_history`, `mission_list` — all gated by `GCO_ENABLE_MISSION` |
| `docs.py` | 1 | `find_docs` (documentation discovery) |
| `examples.py` | 1 | `find_examples` (example-manifest discovery) |
| `tasks.py` | 2 | `task_status`, `task_tail` (read-only observability for long-running tools) |

## How Tools Work

Every tool follows the same pattern:

1. Decorated with `@mcp.tool()` (registers with FastMCP) and `@audit_logged` (structured audit logging)
2. Builds a CLI argument list from the tool's parameters
3. Calls `cli_runner._run_cli(*args)` which shells out to `gco --output json ...`
4. Returns the JSON string result to the LLM

## Adding a New Tool

1. Add the function to the appropriate domain file (or create a new one)
2. Decorate with `@mcp.tool()` and `@audit_logged`
3. Call `cli_runner._run_cli(...)` with the correct CLI arguments
4. Register the module in `tools/__init__.py` if it's a new file
5. Add tests in `tests/test_mcp_server.py` and `tests/test_mcp_integration.py`
