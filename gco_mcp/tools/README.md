# MCP Tools

MCP tool definitions — one file per domain. Each module registers tools against the shared FastMCP server instance via `@mcp.tool()` decorators.

## Table of Contents

- [Files](#files)
- [Tool Reference](#tool-reference)
- [How Tools Work](#how-tools-work)
- [Adding a New Tool](#adding-a-new-tool)

## Files

Counts are tools registered per module; tools gated behind a feature flag only
appear when that flag (or the umbrella `GCO_ENABLE_ALL_TOOLS`) is set. At
default registration the server exposes 134 tools; with every flag enabled the
ceiling is 182. See [Feature Flags](../README.md#feature-flags) for the
flag-to-tool mapping.

| File | Tools | Description |
|------|-------|-------------|
| `jobs.py` | 13 | `list_jobs`, `submit_job_sqs`, `submit_job_api`, `get_job`, `get_job_logs`, `get_job_events`, `get_job_pods`, `get_pod_logs`, `get_job_metrics`, `retry_job`, `delete_job` (gated), `cluster_health`, `queue_status` |
| `capacity.py` | 18 | `check_capacity`, `instance_info`, `recommend_capacity`, `capacity_status`, `recommend_region`, `spot_prices`, `ai_recommend`, `list_reservations`, `reservation_check`, `find_capacity_blocks`, `find_capacity_reservations`, `capacity_history_show`, `capacity_history_stats`, `capacity_history_patterns`, `capacity_predict`, `reserve_capacity` (gated), `create_reservation` (gated), `cancel_reservation` (gated) |
| `inference.py` | 20 | `deploy_inference`, `list_inference_endpoints`, `inference_status`, `scale_inference`, `update_inference_image`, `stop_inference`, `start_inference`, `delete_inference` (gated), `canary_deploy`, `promote_canary`, `rollback_canary`, `invoke_inference`, `chat_inference`, `inference_health`, `list_endpoint_models`, `deploy_disaggregated_inference`, `set_mooncake_topology`, `configure_mooncake_store`, `mooncake_topology_status`, `populate_kv_cache` |
| `costs.py` | 14 | `cost_summary`, `cost_by_region`, `cost_trend`, `cost_forecast`, `cost_workloads`, `cost_allocation_status`, `cost_allocation_activate`, `cost_k8s_namespaces`, `cost_k8s_regions`, `cost_k8s_trend`, `cost_k8s_top`, `cost_report_status`, `cost_report_list`, `cost_report_generate` |
| `stacks.py` | 29 | `list_stacks`, `stack_status`, `setup_cluster_access`, `fsx_status`, `stack_diff`, `stack_outputs`, `stack_synth`, `addons_status`, `valkey_status`, `aurora_status`, `enable_fsx`, `disable_fsx`, `enable_valkey`, `disable_valkey`, `enable_aurora`, `disable_aurora`, `addons_install` (gated), `deploy_stack` (gated), `deploy_all` (gated), `bootstrap_cdk` (gated), `destroy_stack` (gated), `destroy_all` (gated), `list_deployment_regions` (gated), `add_deployment_region` (gated), `remove_deployment_region` (gated), `set_deployment_region` (gated), `set_mission_default_model` (gated), `set_capacity_advisor_default_model` (gated), `set_claude_code_default_model` (gated) |
| `storage.py` | 7 | `list_storage_contents`, `list_file_systems`, `list_storage_buckets`, `files_get`, `files_access_points`, `upload_to_regional_bucket` (gated by `GCO_ENABLE_MODEL_UPLOAD`), `sync_storage_bucket` (gated by `GCO_ENABLE_LOCAL_STORAGE_SYNC`) |
| `models.py` | 4 | `list_models`, `get_model_uri`, `models_upload` (gated), `delete_model` (gated) |
| `nodepools.py` | 5 | `nodepools_list`, `nodepools_describe`, `nodepools_create_odcr`, `nodepools_create_capacity_block`, `delete_nodepool` (gated) |
| `analytics.py` | 8 | `analytics_doctor`, `analytics_status`, `analytics_login_url`, `analytics_users_list`, `enable_analytics`, `disable_analytics`, `analytics_user_add`, `analytics_user_remove` (gated) |
| `monitoring.py` | 6 | `monitoring_status`, `monitoring_users_list`, `enable_monitoring`, `disable_monitoring`, `monitoring_user_add`, `monitoring_user_remove` (gated) |
| `cluster.py` | 1 | `cluster_tunnel_command` |
| `templates.py` | 5 | `templates_list`, `templates_get`, `templates_create`, `templates_run`, `delete_template` (gated) |
| `webhooks.py` | 4 | `webhooks_list`, `webhooks_get`, `webhooks_create`, `delete_webhook` (gated) |
| `queue.py` | 5 | `queue_list`, `queue_get`, `queue_stats`, `queue_submit`, `cancel_queue_job` (gated) |
| `images.py` | 20 | `images_list`, `images_tags`, `images_describe`, `images_uri`, `images_replication_get`, `images_replication_status`, `images_orphans`, `images_mirror_plan`, `images_mirror_status`, `images_init`, `images_lifecycle_get`, `images_lifecycle_set`, `images_replication_sync`, `images_build` (gated), `images_push` (gated), `images_mirror` (gated), `images_delete_tag` (gated), `images_delete_repo` (gated), `images_cleanup` (gated), `images_prune` (gated) |
| `dag.py` | 2 | `dag_validate`, `dag_run` |
| `config.py` | 1 | `config_get` |
| `metrics.py` | 4 | `metrics_cloudwatch_get`, `metrics_from_job_logs`, `metrics_from_shared_storage_file` (default-on); `metrics_from_local_file` (gated by `GCO_ENABLE_LOCAL_METRICS`, default-off) — all `safe` |
| `semantic_progress.py` | 1 | `metrics_semantic_progress` (gated by `GCO_ENABLE_SEMANTIC_PROGRESS`, default-off) — `safe` LLM-as-judge progress score |
| `mission.py` | 10 | `mission_start`, `mission_status`, `mission_iterate`, `mission_checkpoint`, `mission_complete`, `mission_abort`, `mission_resume`, `mission_history`, `mission_list`, `mission_memory_search` — all gated by `GCO_ENABLE_MISSION` |
| `docs.py` | 1 | `find_docs` (documentation discovery) |
| `examples.py` | 1 | `find_examples` (example-manifest discovery) |
| `tasks.py` | 3 | `task_status`, `task_tail` (read-only observability), `task_prune` (gated local cleanup) |

## Tool Reference

Every registered MCP tool, grouped by module, with a one-line description from the tool docstring. Tools marked `(gated)` in the [Files](#files) table register only when their feature flag (or the umbrella `GCO_ENABLE_ALL_TOOLS`) is set; this reference lists the full set. `tests/test_docs_coverage.py` fails if a registered tool is missing from this section.

### `jobs.py`

| Tool | Description |
|------|-------------|
| `cluster_health` | Get health status of GCO clusters. |
| `delete_job` | Delete a job. |
| `get_job` | Get details of a specific job. |
| `get_job_events` | Get Kubernetes events for a job (useful for debugging). |
| `get_job_logs` | Get logs from a job. |
| `get_job_metrics` | Get CPU and memory usage for all pods in a job. |
| `get_job_pods` | Get pod details, placement, and container status for a job. |
| `get_pod_logs` | Get a bounded log tail from one specific pod belonging to a job. |
| `list_jobs` | List jobs across GCO clusters. |
| `queue_status` | View [SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html) queue status (pending, in-flight, DLQ counts). |
| `retry_job` | Retry a failed job by creating a new Job while preserving the original. |
| `submit_job_api` | Submit a job via the authenticated [API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/welcome.html) ([SigV4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html)). |
| `submit_job_sqs` | Submit a job via SQS queue (recommended for production). |

### `capacity.py`

| Tool | Description |
|------|-------------|
| `ai_recommend` | Get AI-powered capacity recommendation using Amazon Bedrock. |
| `cancel_reservation` | Cancel an [On-Demand Capacity Reservation](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-reservations.html), releasing its capacity (gated by `GCO_ENABLE_DESTRUCTIVE_OPERATIONS`). |
| `capacity_history_patterns` | Show a day-of-week by hour heatmap of average spot placement scores. |
| `capacity_history_show` | Show the recorded capacity time-series for an instance type in a region. |
| `capacity_history_stats` | Show p25/p50/p75/min/max/stddev per capacity metric over a time window. |
| `capacity_predict` | Predict the best time to acquire capacity from historical patterns ([Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html)). |
| `capacity_status` | View capacity status across all deployed regions. |
| `check_capacity` | Check spot and on-demand capacity for a specific instance type. |
| `create_reservation` | Create a new On-Demand Capacity Reservation (ODCR) (gated by `GCO_ENABLE_CAPACITY_PURCHASE`). |
| `find_capacity_blocks` | Find [EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/concepts.html) [Capacity Blocks](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html) across regions x durations x a start-date window in one consolidated, ranked, de-duplicated report. |
| `find_capacity_reservations` | Find existing ODCRs across regions in one parallel, ranked, priced report. |
| `instance_info` | Get hardware and pricing metadata for an EC2 instance type. |
| `list_reservations` | List On-Demand Capacity Reservations (ODCRs) across regions. |
| `recommend_capacity` | Recommend spot or on-demand capacity for a workload. |
| `recommend_region` | Get optimal region recommendation based on capacity. |
| `reservation_check` | Check ODCR and Capacity Block availability (multi-region, date-window, duration in hours or days). |
| `reserve_capacity` | Purchase a Capacity Block offering by its ID. |
| `spot_prices` | Get current spot prices for an instance type. |

### `inference.py`

| Tool | Description |
|------|-------------|
| `canary_deploy` | Start a canary deployment (A/B test a new image version). |
| `chat_inference` | Send a buffered multi-turn chat conversation to an inference endpoint. |
| `configure_mooncake_store` | Update a Mooncake endpoint's shared KV-cache store configuration. |
| `delete_inference` | Delete an inference endpoint. |
| `deploy_disaggregated_inference` | Deploy a split prefill/decode (XpYd) inference endpoint. |
| `deploy_inference` | Deploy an inference endpoint across regions. |
| `inference_health` | Check if an inference endpoint is healthy and ready to serve requests. |
| `inference_status` | Get detailed status of an inference endpoint including per-region breakdown. |
| `invoke_inference` | Send a prompt to an inference endpoint and return the buffered generated text. |
| `list_endpoint_models` | List models loaded on an inference endpoint. |
| `list_inference_endpoints` | List all inference endpoints. |
| `mooncake_topology_status` | Show a disaggregated endpoint's per-role topology status. |
| `populate_kv_cache` | `gco inference populate-kv` — upload data into an endpoint's KV-cache cold tier. |
| `promote_canary` | Promote canary to primary (100% traffic to new version). |
| `rollback_canary` | Rollback canary (remove canary, 100% traffic to primary). |
| `scale_inference` | Scale an inference endpoint. |
| `set_mooncake_topology` | Resize a disaggregated endpoint's prefill/decode topology. |
| `start_inference` | Start a stopped inference endpoint. |
| `stop_inference` | Stop an inference endpoint (scales to zero, keeps config). |
| `update_inference_image` | Rolling update of an inference endpoint's container image. |

### `costs.py`

| Tool | Description |
|------|-------------|
| `cost_allocation_activate` | `gco costs allocation activate` — activate cost allocation tag keys (reversible billing toggle). |
| `cost_allocation_status` | `gco costs allocation status` — activation status of GCO's cost allocation tag keys. |
| `cost_by_region` | Get cost breakdown by AWS region. |
| `cost_forecast` | Forecast GCO costs for the next N days. |
| `cost_k8s_namespaces` | `gco costs k8s namespaces` — Kubernetes cost by namespace across regions (Athena/OpenCost). |
| `cost_k8s_regions` | `gco costs k8s regions` — Kubernetes allocation cost by deployment region. |
| `cost_k8s_top` | `gco costs k8s top` — top-N Kubernetes spenders by namespace, region, or cluster. |
| `cost_k8s_trend` | `gco costs k8s trend` — Kubernetes cost over time (daily or hourly buckets). |
| `cost_report_generate` | `gco costs report generate` — generate an ad-hoc OpenCost allocation report. |
| `cost_report_list` | `gco costs report list` — list recent cost report objects in the report bucket. |
| `cost_report_status` | `gco costs report status` — cost monitoring health, including OpenCost status. |
| `cost_summary` | Get total GCO spend broken down by AWS service. |
| `cost_trend` | Get daily cost trend. |
| `cost_workloads` | Estimate accumulated and hourly cost for running workloads. |

### `stacks.py`

| Tool | Description |
|------|-------------|
| `add_deployment_region` | `gco stacks regions add` — add a workload Region to cdk.json `deployment_regions.regional`, validated and config-only (gated by `GCO_ENABLE_CONFIG_MANAGEMENT`). |
| `addons_install` | `gco stacks addons install` — start an idempotent Helm add-on re-convergence (gated by `GCO_ENABLE_INFRASTRUCTURE_DEPLOY`). |
| `addons_status` | `gco stacks addons status` — show per-chart Helm add-on status from SSM. |
| `aurora_status` | `gco stacks aurora status` — show [Aurora](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/CHAP_AuroraOverview.html) database stack status. |
| `bootstrap_cdk` | `gco stacks bootstrap` — bootstrap [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) in an AWS account/region. |
| `deploy_all` | `gco stacks deploy-all` — deploy every CDK stack in dependency order. |
| `deploy_stack` | `gco stacks deploy` — deploy a single CDK stack to AWS. |
| `destroy_all` | `gco stacks destroy-all` — destroy every CDK stack in reverse dependency order. |
| `destroy_stack` | `gco stacks destroy` — destroy a single CDK stack. |
| `disable_aurora` | `gco stacks aurora disable` — flip Aurora pgvector off in cdk.json. |
| `disable_fsx` | `gco stacks fsx disable` — flip [FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html) Lustre off in cdk.json. |
| `disable_valkey` | `gco stacks valkey disable` — flip Valkey Serverless off in cdk.json. |
| `enable_aurora` | `gco stacks aurora enable` — flip Aurora pgvector on in cdk.json. |
| `enable_fsx` | `gco stacks fsx enable` — flip FSx Lustre on in cdk.json. |
| `enable_valkey` | `gco stacks valkey enable` — flip Valkey Serverless on in cdk.json. |
| `fsx_status` | Check FSx for Lustre configuration status. |
| `list_deployment_regions` | `gco stacks regions list` — show the cdk.json deployment-region topology and its resolved partition (gated by `GCO_ENABLE_CONFIG_MANAGEMENT`). |
| `list_stacks` | List all GCO CDK stacks. |
| `remove_deployment_region` | `gco stacks regions remove` — remove a workload Region from cdk.json `deployment_regions.regional`; never destroys stacks (gated by `GCO_ENABLE_CONFIG_MANAGEMENT`). |
| `set_claude_code_default_model` | `gco stacks bedrock set-claude-code-model` — set cdk.json `bedrock.claude_code_default_model_id`, the session model `gco autopilot` hands to Claude Code (gated by `GCO_ENABLE_CONFIG_MANAGEMENT`). |
| `set_mission_default_model` | `gco stacks bedrock set-mission-model` — set cdk.json `bedrock.mission_default_model_id`, Mission sampling's model default (gated by `GCO_ENABLE_CONFIG_MANAGEMENT`). |
| `set_capacity_advisor_default_model` | `gco stacks bedrock set-capacity-advisor-model` — set cdk.json `bedrock.capacity_advisor_default_model_id`, the capacity advisor's model default (gated by `GCO_ENABLE_CONFIG_MANAGEMENT`). |
| `set_deployment_region` | `gco stacks regions set` — set a control-plane Region scalar (global/api_gateway/monitoring) in cdk.json (gated by `GCO_ENABLE_CONFIG_MANAGEMENT`). |
| `setup_cluster_access` | Configure kubectl access to a GCO [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) cluster. |
| `stack_diff` | `gco stacks diff` — show [CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) diff for a stack. |
| `stack_outputs` | `gco stacks outputs` — fetch CloudFormation outputs for a stack. |
| `stack_status` | Get detailed status of a CloudFormation stack. |
| `stack_synth` | `gco stacks synth` — synthesize CloudFormation templates from CDK. |
| `valkey_status` | `gco stacks valkey status` — show Valkey cache stack status. |

### `storage.py`

| Tool | Description |
|------|-------------|
| `files_access_points` | `gco files access-points` — list [EFS](https://docs.aws.amazon.com/efs/latest/ug/whatisefs.html) access points. |
| `files_get` | `gco files get` — get file system details for a region (EFS/FSx). |
| `list_file_systems` | List EFS and FSx file systems. |
| `list_storage_buckets` | List deployed GCO [S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html) buckets and their human-friendly aliases. |
| `list_storage_contents` | List contents of shared EFS storage. |
| `sync_storage_bucket` | Sync between a GCO S3 bucket and a confined local path using explicit `download` (default) or `upload` direction; neither direction deletes destination-only data (gated by `GCO_ENABLE_LOCAL_STORAGE_SYNC` and confined to `GCO_STORAGE_LOCAL_ROOT`). |
| `upload_to_regional_bucket` | `gco models upload-regional` — upload a descriptor-backed snapshot of a source confined beneath `GCO_STORAGE_LOCAL_ROOT` to a regional bucket (gated by `GCO_ENABLE_MODEL_UPLOAD`; links, special files, and filesystem crossings fail closed). |

### `models.py`

| Tool | Description |
|------|-------------|
| `delete_model` | `gco models delete` — delete a model from the central S3 bucket. |
| `get_model_uri` | Get the S3 URI for a model (for use with --model-source). |
| `list_models` | List all uploaded model weights in the S3 bucket. |
| `models_upload` | Upload a descriptor-backed snapshot of a source confined beneath `GCO_STORAGE_LOCAL_ROOT` to the central model bucket (gated by `GCO_ENABLE_MODEL_UPLOAD`; links, special files, and filesystem crossings fail closed). |

### `nodepools.py`

| Tool | Description |
|------|-------------|
| `delete_nodepool` | `gco nodepools delete` — delete a Karpenter NodePool. |
| `nodepools_create_capacity_block` | `gco nodepools create-capacity-block` — create a Karpenter NodePool for a purchased Capacity Block. |
| `nodepools_create_odcr` | `gco nodepools create-odcr` — create a Karpenter NodePool tied to an ODCR. |
| `nodepools_describe` | `gco nodepools describe` — describe a single NodePool. |
| `nodepools_list` | `gco nodepools list` — list Karpenter NodePools in a cluster. |

### `analytics.py`

| Tool | Description |
|------|-------------|
| `analytics_doctor` | `gco analytics doctor` — run analytics environment health checks. |
| `analytics_status` | `gco analytics status` — show the analytics environment configuration. |
| `analytics_login_url` | `gco analytics studio login` — get a [SageMaker](https://docs.aws.amazon.com/sagemaker/latest/dg/whatis.html) Studio presigned login URL. |
| `analytics_user_add` | `gco analytics users add` — create a [Cognito](https://docs.aws.amazon.com/cognito/latest/developerguide/what-is-amazon-cognito.html) user in the analytics pool. |
| `analytics_user_remove` | `gco analytics users remove` — delete a Cognito user from the analytics user pool. |
| `analytics_users_list` | `gco analytics users list` — list Cognito users in the analytics user pool. |
| `disable_analytics` | `gco analytics disable` — flip the analytics environment off in cdk.json. |
| `enable_analytics` | `gco analytics enable` — flip the analytics environment on in cdk.json. |

### `monitoring.py`

| Tool | Description |
|------|-------------|
| `monitoring_status` | `gco monitoring status` — show the cluster observability toggle + config. |
| `monitoring_users_list` | `gco monitoring users list` — list Grafana users via the admin API. |
| `enable_monitoring` | `gco monitoring enable` — flip cluster observability on in cdk.json. |
| `disable_monitoring` | `gco monitoring disable` — flip cluster observability off in cdk.json. |
| `monitoring_user_add` | `gco monitoring users add` — create a Grafana user via the admin API. |
| `monitoring_user_remove` | `gco monitoring users remove` — delete a Grafana user (gated). |

### `cluster.py`

| Tool | Description |
|------|-------------|
| `cluster_tunnel_command` | `gco cluster tunnel --print` — return the connection plan (the `aws ssm start-session` tunnel command + `kubectl` flags) for reaching a cluster's private EKS API endpoint. Read-only; does not open a tunnel. |

### `templates.py`

| Tool | Description |
|------|-------------|
| `delete_template` | `gco templates delete` — delete a job template. |
| `templates_create` | `gco templates create` — register a new job template from a manifest. |
| `templates_get` | `gco templates get` — fetch a single job template by name. |
| `templates_list` | `gco templates list` — list job templates. |
| `templates_run` | `gco templates run` — instantiate a job from a stored template. |

### `webhooks.py`

| Tool | Description |
|------|-------------|
| `delete_webhook` | `gco webhooks delete` — delete a webhook subscription. |
| `webhooks_create` | `gco webhooks create` — register a new webhook subscription. |
| `webhooks_get` | `gco webhooks get` — fetch a single webhook by name. |
| `webhooks_list` | `gco webhooks list` — list configured webhooks. |

### `queue.py`

| Tool | Description |
|------|-------------|
| `cancel_queue_job` | `gco queue cancel` — cancel a queued job (only works for jobs not yet running). |
| `queue_get` | `gco queue get` — fetch a single job from the global queue. |
| `queue_list` | `gco queue list` — list jobs in the global queue. |
| `queue_stats` | `gco queue stats` — show aggregate stats for the global queue. |
| `queue_submit` | `gco queue submit` — submit a job manifest to the global queue. |

### `images.py`

| Tool | Description |
|------|-------------|
| `images_build` | long-running, data-upload. |
| `images_cleanup` | `gco images cleanup` — remove every untagged image across one or all project repos. |
| `images_delete_repo` | `gco images delete-repo` — delete a whole repository. |
| `images_delete_tag` | `gco images delete-tag` — delete a single tag from a repository. |
| `images_describe` | `gco images describe` — full [ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html) details for a single image tag. |
| `images_init` | `gco images init` — create the project ECR repo idempotently with default lifecycle. |
| `images_lifecycle_get` | `gco images lifecycle get` — print the lifecycle policy on a repository. |
| `images_lifecycle_set` | `gco images lifecycle set` — replace the lifecycle policy on a repository. |
| `images_list` | `gco images list` — list every gco/* repository in ECR. |
| `images_mirror` | image-upload. |
| `images_mirror_plan` | Image mirror — show which third-party images would be mirrored into ECR. |
| `images_mirror_status` | Image mirror — report which managed images are already present in ECR. |
| `images_orphans` | `gco images orphans` — list gco/* tags older than ``threshold_days`` with no references. |
| `images_prune` | `gco images prune` — remove untagged images older than 30 days. |
| `images_push` | long-running, data-upload. |
| `images_replication_get` | `gco images replication get` — current ECR replication configuration. |
| `images_replication_status` | `gco images replication status` — per-image replication status across project repos. |
| `images_replication_sync` | `gco images replication sync` — apply the standard gco/* replication rule. |
| `images_tags` | `gco images tags` — list tags within a repository. |
| `images_uri` | `gco images uri` — return the registry URI for an image. |

### `dag.py`

| Tool | Description |
|------|-------------|
| `dag_run` | `gco dag run` — execute a DAG manifest end-to-end. |
| `dag_validate` | `gco dag validate` — statically validate a DAG manifest. |

### `config.py`

| Tool | Description |
|------|-------------|
| `config_get` | `gco config get` — read a CLI configuration value. |

### `metrics.py`

| Tool | Description |
|------|-------------|
| `metrics_cloudwatch_get` | Read one [CloudWatch](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/WhatIsCloudWatch.html) datapoint as a canonical metric. |
| `metrics_from_job_logs` | Extract a scalar from the tail of a job's logs. |
| `metrics_from_local_file` | Read a named field from a LOCAL metrics file. |
| `metrics_from_shared_storage_file` | Read a named field from a shared-storage metrics file. |

### `semantic_progress.py`

| Tool | Description |
|------|-------------|
| `metrics_semantic_progress` | Score Mission progress. |

### `mission.py`

| Tool | Description |
|------|-------------|
| `mission_abort` | Pause or terminate a Mission session. |
| `mission_checkpoint` | Re-run the verdict cascade on the latest iteration. |
| `mission_complete` | Force a Mission session into completed status. |
| `mission_history` | Get iteration history for a Mission session. |
| `mission_iterate` | Run iteration(s) on a Mission session. |
| `mission_list` | List Mission sessions. |
| `mission_memory_search` | Search mission memory for similar past missions by directive. |
| `mission_resume` | Resume a paused Mission session. |
| `mission_start` | Start a new Mission session. |
| `mission_status` | Get the full state of a Mission session. |

### `docs.py`

| Tool | Description |
|------|-------------|
| `find_docs` | `find_docs` — search the docs catalog by topic and free-text query. |

### `examples.py`

| Tool | Description |
|------|-------------|
| `find_examples` | `find_examples` — search the example-manifest catalog by keyword and filters. |

### `tasks.py`

| Tool | Description |
|------|-------------|
| `task_status` | Return live status of long-running tools. |
| `task_tail` | Return the last N lines of a long-running task's raw output log. |
| `task_prune` | Delete old local task records while retaining the newest N (gated by `GCO_ENABLE_DESTRUCTIVE_OPERATIONS`). |

## How Tools Work

Every tool follows the same pattern:

1. Decorated with `@mcp.tool()` (registers with FastMCP) and `@audit_logged` (structured audit logging)
2. Builds a CLI argument list from the tool's parameters
3. Calls `cli_runner._run_cli(*args)` which shells out to `gco --output json ...`
4. Returns the JSON string result to the LLM

Most tools follow this `_run_cli` shell-out pattern. A few domains — notably the
image-registry and image-mirror tools in `images.py` — instead wrap their CLI
manager/core directly via `asyncio.to_thread(...)` (e.g. `cli.images.ImageManager`,
`cli._image_mirror`), so the MCP layer never re-implements the underlying
ECR/runtime logic. They still carry `@mcp.tool()` + `@audit_logged` and return a
JSON string. The image-mirror tools (`images_mirror_plan`, `images_mirror_status`,
`images_mirror`) are documented in [Image Mirror](../../docs/IMAGE_MIRROR.md#mcp-tools).

## Adding a New Tool

1. Add the function to the appropriate domain file (or create a new one)
2. Decorate with `@mcp.tool()` and `@audit_logged`
3. Call `cli_runner._run_cli(...)` with the correct CLI arguments
4. Register the module in `tools/__init__.py` if it's a new file
5. Add tests in `tests/test_mcp_server.py` and `tests/test_mcp_integration.py`
