# MCP Tools

MCP tool definitions — one file per domain. Each module registers tools against the shared FastMCP server instance via `@mcp.tool()` decorators.

## Table of Contents

- [Files](#files)
- [How Tools Work](#how-tools-work)
- [Adding a New Tool](#adding-a-new-tool)

## Files

| File | Tools | Description |
|------|-------|-------------|
| `jobs.py` | 9 | `list_jobs`, `submit_job_sqs`, `submit_job_api`, `get_job`, `get_job_logs`, `get_job_events`, `delete_job`, `cluster_health`, `queue_status` |
| `capacity.py` | 8 | `check_capacity`, `capacity_status`, `recommend_region`, `spot_prices`, `ai_recommend`, `list_reservations`, `reservation_check`, `reserve_capacity` (conditional) |
| `inference.py` | 15 | `deploy_inference`, `list_inference_endpoints`, `inference_status`, `scale_inference`, `update_inference_image`, `stop_inference`, `start_inference`, `delete_inference`, `canary_deploy`, `promote_canary`, `rollback_canary`, `invoke_inference`, `chat_inference`, `inference_health`, `list_endpoint_models` |
| `costs.py` | 4 | `cost_summary`, `cost_by_region`, `cost_trend`, `cost_forecast` |
| `stacks.py` | 4 | `list_stacks`, `stack_status`, `setup_cluster_access`, `fsx_status` |
| `storage.py` | 2 | `list_storage_contents`, `list_file_systems` |
| `models.py` | 2 | `list_models`, `get_model_uri` |
| `metrics.py` | 5 (3 default-on, 2 gated) | All `safe`: `metrics_cloudwatch_get`, `metrics_from_job_logs`, `metrics_from_shared_storage_file` (default-on); `metrics_from_local_file` (gated by `GCO_ENABLE_LOCAL_METRICS`, default-off); `metrics_semantic_progress` (gated by `GCO_ENABLE_SEMANTIC_PROGRESS`, default-off) |
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
