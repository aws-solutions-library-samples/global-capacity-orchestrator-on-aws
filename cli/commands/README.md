# CLI Commands

Click command definitions that wire CLI flags and arguments to the business logic in the parent `cli/` modules. Each file defines a Click group or set of commands for one domain.

## Table of Contents

- [Architecture](#architecture)
- [Files](#files)
- [Adding a New Command](#adding-a-new-command)

## Architecture

Commands follow a two-layer pattern:

1. **Command layer** (this directory) — Click decorators, argument parsing, output formatting
2. **Business logic layer** (`cli/*.py`) — AWS API calls, data processing, error handling

This separation keeps the Click wiring thin and the business logic testable without Click.

## Files

One module per top-level command group. The command list itself lives in the
[CLI Reference](../../docs/CLI.md), which the test suite keeps aligned with the
registered groups; this table only says what each module owns.

| File | Group | Description | Reference |
|------|-------|-------------|-----------|
| `analytics_cmd.py` | `gco analytics ...` | Manage the GCO analytics (SageMaker Studio + EMR) environment. | [reference](../../docs/CLI.md#analytics-commands) |
| `autopilot_cmd.py` | `gco autopilot` | Launch a fully configured Claude Code or Codex session for GCO. | [reference](../../docs/CLI.md#autopilot-command) |
| `capacity_cmd.py` | `gco capacity ...` | Check EC2 capacity availability. | [reference](../../docs/CLI.md#capacity-commands) |
| `cluster_cmd.py` | `gco cluster ...` | Cluster connectivity helpers (SSM tunnel to the private EKS API). | [reference](../../docs/CLI.md#cluster-commands) |
| `config_cmd.py` | `gco config-cmd ...` | Manage CLI configuration. | [reference](../../docs/CLI.md#config-cmd-commands) |
| `costs_cmd.py` | `gco costs ...` | View cost breakdowns and estimates for GCO resources. | [reference](../../docs/CLI.md#costs-commands) |
| `dag_cmd.py` | `gco dag ...` | Run multi-step job pipelines with dependencies. | [reference](../../docs/CLI.md#dag-commands) |
| `deps_cmd.py` | `gco deps ...` | Dependency maintenance (update scans, NodePool registry freshness). | [reference](../../docs/CLI.md#deps-commands) |
| `examples_cmd.py` | `gco examples ...` | Validate the shipped example manifests. | [reference](../../docs/CLI.md#examples-commands) |
| `files_cmd.py` | `gco files ...` | Manage file systems (EFS/FSx). | [reference](../../docs/CLI.md#files-commands) |
| `images_cmd.py` | `gco images ...` | Manage container images in the project ECR registry (gco/* repos). | [reference](../../docs/CLI.md#images-commands) |
| `inference_cmd.py` | `gco inference ...` | Manage multi-region inference endpoints. | [reference](../../docs/CLI.md#inference-commands) |
| `jobs_cmd.py` | `gco jobs ...` | Manage jobs across GCO clusters. | [reference](../../docs/CLI.md#jobs-commands) |
| `mission_cmd.py` | `gco mission ...` | Mission goal-directed iteration loop commands. | [reference](../../docs/CLI.md#mission-commands) |
| `models_cmd.py` | `gco models ...` | Manage model weights in the central S3 bucket. | [reference](../../docs/CLI.md#models-commands) |
| `monitoring_cmd.py` | `gco monitoring ...` | Manage in-cluster observability (Prometheus + Grafana + Alertmanager). | [reference](../../docs/CLI.md#monitoring-commands) |
| `nodepools_cmd.py` | `gco nodepools ...` | Manage Karpenter NodePools with ODCR/Capacity Reservation support. | [reference](../../docs/CLI.md#nodepools-commands) |
| `queue_cmd.py` | `gco queue ...` | Manage the global job queue (DynamoDB-backed). | [reference](../../docs/CLI.md#queue-commands) |
| `release_cmd.py` | `gco release ...` | Release validation lifecycle. | [reference](../../docs/CLI.md#release-commands) |
| `stacks_cmd.py` | `gco stacks ...` | Deploy and manage GCO CDK stacks. | [reference](../../docs/CLI.md#stacks-commands) |
| `status_cmd.py` | `gco status` | Show fleet-wide deployment status across configured regions. | [reference](../../docs/CLI.md#status-commands) |
| `storage_cmd.py` | `gco storage ...` | Discover and sync user-facing GCO S3 buckets. | [reference](../../docs/CLI.md#storage-commands) |
| `swarm_cmd.py` | `gco swarm ...` | Swarm supervision: one orchestrator Mission driving child Missions. | [reference](../../docs/CLI.md#swarm-commands) |
| `tasks_cmd.py` | `gco tasks ...` | Inspect long-running MCP / CLI task status. | [reference](../../docs/CLI.md#tasks-commands) |
| `templates_cmd.py` | `gco templates ...` | Manage job templates. | [reference](../../docs/CLI.md#templates-commands) |
| `vector_cmd.py` | `gco vector ...` | Semantic search over an S3-ingested document corpus. | [reference](../../docs/CLI.md#vector-commands) |
| `webhooks_cmd.py` | `gco webhooks ...` | Manage webhooks for job event notifications. | [reference](../../docs/CLI.md#webhooks-commands) |
| `__init__.py` | — | Imports every group so `cli/main.py` can register them with `cli.add_command()` | — |

## Adding a New Command

1. Create a new file (e.g. `my_cmd.py`) with a `@click.group()` or `@click.command()`
2. Add the business logic in `cli/my_module.py`
3. Import the group in `__init__.py` and register it with `cli.add_command()` in `cli/main.py`
4. Document it: a `### My Commands` section plus a TOC entry in `docs/CLI.md`, a row in
   `cli/README.md` under `### commands/`, and a row in the table above
   (`tests/test_documentation_consistency.py` fails the build until the first two match the registered groups)
5. Add tests in `tests/test_cli_*.py`
