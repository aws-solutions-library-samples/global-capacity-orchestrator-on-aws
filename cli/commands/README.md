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

| File | Commands | Description |
|------|----------|-------------|
| `autopilot_cmd.py` | `gco autopilot` | Launch a fully configured [Claude Code](https://code.claude.com/docs/en/overview) session on [Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html) with the GCO MCP server + companion MCPs ([docs](../../docs/AUTOPILOT.md)) |
| `jobs_cmd.py` | `gco jobs submit`, `submit-sqs`, `submit-direct`, `list`, `get`, `logs`, `events`, `delete`, `health`, `queue-status` | Job submission and lifecycle |
| `inference_cmd.py` | `gco inference deploy`, `list`, `status`, `scale`, `update-image`, `stop`, `start`, `delete`, `canary`, `promote`, `rollback`, `invoke`, `chat`, `health`, `models` | Inference endpoint management |
| `stacks_cmd.py` | `gco stacks deploy`, `deploy-all`, `destroy`, `destroy-all`, `list`, `status`, `access`, `bootstrap`, `fsx`, `valkey`, `aurora` | [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) stack deployment and management |
| `status_cmd.py` | `gco status` | Fleet-wide status: verdict, findings, and per-section state across regions |
| `capacity_cmd.py` | `gco capacity check`, `status`, `recommend-region`, `spot-prices`, `ai-recommend`, `reservations`, `reservation-check`, `reserve` | GPU capacity and recommendations |
| `queue_cmd.py` | `gco queue submit`, `list`, `get`, `stats` | Global [DynamoDB](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Introduction.html) job queue |
| `costs_cmd.py` | `gco costs summary`, `regions`, `trend`, `workloads`, `forecast` | Cost tracking via [Cost Explorer](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html) |
| `templates_cmd.py` | `gco templates list`, `get`, `create`, `delete` | Reusable job template management |
| `files_cmd.py` | `gco files ls`, `download` | EFS/FSx file operations |
| `webhooks_cmd.py` | `gco webhooks list`, `create`, `delete`, `test` | Webhook registration |
| `nodepools_cmd.py` | `gco nodepools list`, `describe` | Nodepool inspection |
| `models_cmd.py` | `gco models upload`, `list`, `uri`, `delete` | Model weight management |
| `storage_cmd.py` | `gco storage list`, `sync` | Human-friendly [S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html) bucket discovery and explicit download/upload sync |
| `dag_cmd.py` | `gco dag run`, `validate`, `status` | DAG pipeline execution |
| `config_cmd.py` | `gco config show`, `set` | CLI configuration |
| `__init__.py` | — | Registers all command groups on the root CLI |

## Adding a New Command

1. Create a new file (e.g. `my_cmd.py`) with a `@click.group()` or `@click.command()`
2. Add the business logic in `cli/my_module.py`
3. Register the group in `__init__.py`
4. Add tests in `tests/test_cli_*.py`
