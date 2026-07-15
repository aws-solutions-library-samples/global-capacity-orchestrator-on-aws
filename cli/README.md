# CLI

The `gco` command-line interface for managing GCO infrastructure, jobs, inference endpoints, and operations.

## Table of Contents

- [Structure](#structure)
- [Installation](#installation)
- [Reference](#reference)

## Structure

| File | Description |
|------|-------------|
| `main.py` | CLI entry point and top-level command group registration |
| `aws_client.py` | AWS SDK client wrapper with region discovery and credential handling |
| `config.py` | CLI configuration loader (cdk.json, env vars, user config) |
| `output.py` | Output formatting (table, JSON, YAML) |
| `jobs.py` | Job submission, listing, logs, and lifecycle management |
| `inference.py` | Inference endpoint deployment, scaling, canary, and invocation |
| `models.py` | Model weight upload, listing, and S3 URI management |
| `storage.py` | Human-friendly GCO S3 bucket discovery and incremental download/upload sync |
| `stacks.py` | CDK stack deployment, destruction, and status |
| `costs.py` | Cost tracking via AWS Cost Explorer |
| `dag.py` | DAG pipeline execution with dependency ordering |
| `files.py` | EFS/FSx file listing and download |
| `nodepools.py` | Nodepool inspection and management |
| `kubectl_helpers.py` | kubectl command wrappers for direct cluster access |

### commands/

Click command definitions that wire CLI flags to the business logic above.

| File | Commands |
|------|----------|
| `capacity_cmd.py` | `gco capacity check`, `status`, `recommend`, `ai-recommend` |
| `config_cmd.py` | `gco config show`, `set` |
| `costs_cmd.py` | `gco costs summary`, `regions`, `trend`, `workloads`, `forecast` |
| `dag_cmd.py` | `gco dag run`, `status` |
| `files_cmd.py` | `gco files ls`, `download` |
| `inference_cmd.py` | `gco inference deploy`, `list`, `status`, `scale`, `invoke`, `canary`, etc. |
| `jobs_cmd.py` | `gco jobs submit`, `submit-sqs`, `submit-direct`, `list`, `logs`, `delete` |
| `models_cmd.py` | `gco models upload`, `list`, `uri`, `delete` |
| `storage_cmd.py` | `gco storage list`, `sync` |
| `nodepools_cmd.py` | `gco nodepools list`, `describe` |
| `queue_cmd.py` | `gco queue submit`, `list`, `get`, `stats` |
| `stacks_cmd.py` | `gco stacks deploy`, `deploy-all`, `destroy-all`, `list`, `bootstrap` |
| `templates_cmd.py` | `gco templates list`, `get`, `create`, `delete` |
| `webhooks_cmd.py` | `gco webhooks list`, `create`, `delete`, `test` |

### capacity/

GPU capacity checking, region recommendation, and AI-powered advisory.

| File | Description |
|------|-------------|
| `checker.py` | Spot placement scores, pricing, and availability checks |
| `advisor.py` | AI-powered capacity recommendations via Amazon Bedrock |
| `models.py` | Data models for capacity responses |
| `multi_region.py` | Cross-region capacity aggregation and comparison |

## Installation

```bash
pip install -e .        # Development (editable)
pipx install -e .       # CLI-only usage
```

## Reference

See [CLI Reference](../docs/CLI.md) for the full command documentation.

## Control-Flow Diagrams

Auto-generated flowcharts for the most branchy CLI entry points live
under [`diagrams/code_diagrams/cli/`](../diagrams/code_diagrams/README.md).

| Function | Flowchart |
|----------|-----------|
| `JobManager.submit_job` (direct `kubectl apply` path) | [HTML](../diagrams/code_diagrams/cli/jobs.JobManager_submit_job.html) · [PNG](../diagrams/code_diagrams/cli/jobs.JobManager_submit_job.png) |
| `JobManager.submit_job_sqs` (SQS-backed submission) | [HTML](../diagrams/code_diagrams/cli/jobs.JobManager_submit_job_sqs.html) · [PNG](../diagrams/code_diagrams/cli/jobs.JobManager_submit_job_sqs.png) |
| `srp_authenticate` (Cognito SRP auth for Studio login) | [HTML](../diagrams/code_diagrams/cli/analytics_user_mgmt.srp_authenticate.html) · [PNG](../diagrams/code_diagrams/cli/analytics_user_mgmt.srp_authenticate.png) |
| `fetch_studio_url` (`/studio/login` presigned-URL poll) | [HTML](../diagrams/code_diagrams/cli/analytics_user_mgmt.fetch_studio_url.html) · [PNG](../diagrams/code_diagrams/cli/analytics_user_mgmt.fetch_studio_url.png) |

Regenerate with `python diagrams/code_diagrams/generate.py` after
editing any of the charted functions' control flow.
