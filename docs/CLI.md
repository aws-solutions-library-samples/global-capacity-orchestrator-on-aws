# GCO CLI Reference

Complete command-line interface documentation for GCO (Global Capacity Orchestrator on AWS).

## Table of Contents

- [Installation](#installation)
- [Global Options](#global-options)
- [Commands](#commands)
  - [jobs](#jobs-commands)
  - [queue](#queue-commands)
  - [templates](#templates-commands)
  - [webhooks](#webhooks-commands)
  - [stacks](#stacks-commands)
  - [dag](#dag-commands)
  - [costs](#costs-commands)
  - [capacity](#capacity-commands)
  - [inference](#inference-commands)
  - [models](#models-commands)
  - [images](#images-commands)
  - [files](#files-commands)
  - [nodepools](#nodepools-commands)
  - [analytics](#analytics-commands)
  - [config-cmd](#config-cmd-commands)
  - [tasks](#tasks-commands)
  - [mission](#mission-commands)
- [Configuration](#configuration)
- [Environment Variables](#environment-variables)
- [Examples](#examples)
- [Troubleshooting](#troubleshooting)

## Installation

### Using pipx (Recommended)

```bash
# Install pipx if not already installed
brew install pipx && pipx ensurepath  # macOS
# or
pip install pipx && pipx ensurepath   # Linux/Windows

# Install GCO CLI
pipx install -e .
```

### Using pip

```bash
pip install -e .
```

### Verify Installation

```bash
gco --version
gco --help
```

## Global Options

These options are available for all commands:

| Option | Short | Description |
|--------|-------|-------------|
| `--config` | `-c` | Path to config file |
| `--region` | `-r` | Default AWS region |
| `--output` | `-o` | Output format: `table`, `json`, `yaml` |
| `--verbose` | `-v` | Enable verbose output |
| `--regional-api` | | Use regional API endpoints (for private access) |
| `--help` | | Show help message |
| `--version` | | Show version |

### Regional API Mode

When `--regional-api` is enabled (or `GCO_REGIONAL_API=true` environment variable is set), the CLI routes requests through regional API Gateways instead of the global API Gateway. This is required when:

- The ALB is internal-only (no public exposure)
- Public access is disabled on the EKS cluster
- Maximum security posture is required

```bash
# Use regional API for a single command
gco --regional-api jobs list --region us-east-1

# Or set environment variable for all commands
export GCO_REGIONAL_API=true
gco jobs list --region us-east-1
```

## Commands

### Jobs Commands

Manage jobs across GCO clusters.

<details>
<summary>All <code>gco jobs</code> commands (16) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco jobs submit`](#gco-jobs-submit) | Submit a job via API Gateway (SigV4 authenticated). |
| [`gco jobs submit-sqs`](#gco-jobs-submit-sqs) | Submit a job via SQS queue (recommended for production). |
| [`gco jobs submit-direct`](#gco-jobs-submit-direct) | Submit a job directly via kubectl (requires EKS access). |
| [`gco jobs submit-queue`](#gco-jobs-submit-queue) | Submit a job to the global DynamoDB queue for regional pickup. |
| [`gco jobs list`](#gco-jobs-list) | List jobs in GCO clusters. |
| [`gco jobs get`](#gco-jobs-get) | Get details of a specific job. |
| [`gco jobs logs`](#gco-jobs-logs) | Get logs from a job. |
| [`gco jobs pod-logs`](#gco-jobs-pod-logs) | Get logs from a specific pod of a job. |
| [`gco jobs delete`](#gco-jobs-delete) | Delete a job. |
| [`gco jobs events`](#gco-jobs-events) | Get Kubernetes events for a job. |
| [`gco jobs pods`](#gco-jobs-pods) | Get pod details for a job. |
| [`gco jobs metrics`](#gco-jobs-metrics) | Get resource usage metrics for a job. |
| [`gco jobs retry`](#gco-jobs-retry) | Retry a failed job. |
| [`gco jobs bulk-delete`](#gco-jobs-bulk-delete) | Bulk delete jobs based on filters. |
| [`gco jobs health`](#gco-jobs-health) | Get health status of GCO clusters. |
| [`gco jobs queue-status`](#gco-jobs-queue-status) | View SQS queue status across regions. |

</details>

#### `gco jobs submit`

Submit a job via API Gateway (SigV4 authenticated).

```bash
gco jobs submit MANIFEST_PATH [OPTIONS]
```

**Arguments:**

- `MANIFEST_PATH` - Path to YAML manifest file

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--namespace` | `-n` | Fallback namespace for manifests that don't declare their own (manifest `metadata.namespace` takes precedence) |
| `--region` | `-r` | Target specific region |
| `--dry-run` | | Validate without applying |
| `--label` | `-l` | Add labels (key=value), can be repeated |
| `--wait` | `-w` | Wait for job completion |
| `--timeout` | | Wait timeout in seconds (default: 3600) |

**Example:**

```bash
gco jobs submit examples/simple-job.yaml -n gco-jobs
gco jobs submit job.yaml --dry-run
gco jobs submit job.yaml -l team=ml -l priority=high
```

#### `gco jobs submit-sqs`

Submit a job via SQS queue (recommended for production).

```bash
gco jobs submit-sqs MANIFEST_PATH [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Target region for SQS queue |
| `--auto-region` | | Auto-select optimal region based on capacity |
| `--priority` | `-p` | Job priority (0-100, higher = more important) |
| `--namespace` | `-n` | Fallback namespace for manifests that don't declare their own (manifest `metadata.namespace` takes precedence) |

**Example:**

```bash
gco jobs submit-sqs examples/simple-job.yaml --region us-east-1
gco jobs submit-sqs job.yaml --auto-region --priority 10
```

#### `gco jobs submit-direct`

Submit a job directly via kubectl (requires EKS access).

If a job with the same name already exists:

- Completed or failed jobs are silently deleted and replaced
- Active (running/pending) jobs are preserved, and the new submission is auto-renamed with a `-{5char}` suffix

```bash
gco jobs submit-direct MANIFEST_PATH [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Target region |
| `--namespace` | `-n` | Fallback namespace for manifests that don't declare their own (manifest `metadata.namespace` takes precedence) |

**Example:**

```bash
gco jobs submit-direct examples/simple-job.yaml --region us-east-1 -n gco-jobs
```

#### `gco jobs submit-queue`

Submit a job to the global DynamoDB queue for regional pickup.

```bash
gco jobs submit-queue MANIFEST_PATH [OPTIONS]
```

Jobs are stored in DynamoDB and picked up by the target region's manifest processor. This enables global job submission with centralized tracking and status history.

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Target region for job execution (required) |
| `--namespace` | `-n` | Kubernetes namespace |
| `--priority` | `-p` | Job priority (0-100, higher = more important) |
| `--label` | `-l` | Add labels (key=value), can be repeated |

**Example:**

```bash
gco jobs submit-queue examples/simple-job.yaml --region us-east-1
gco jobs submit-queue job.yaml -r us-west-2 --priority 50
gco jobs submit-queue job.yaml -r us-east-1 -l team=ml -l project=training
```

**Note:** Use `gco queue list` or `gco queue get <job_id>` to track job status.

#### `gco jobs list`

List jobs in GCO clusters.

```bash
gco jobs list [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Target region (required unless --all-regions) |
| `--all-regions` | `-a` | Query all regions via global API |
| `--namespace` | `-n` | Filter by namespace |
| `--status` | `-s` | Filter by status |
| `--limit` | `-l` | Maximum results (default: 50) |

**Example:**

```bash
gco jobs list --region us-east-1
gco jobs list --all-regions
gco jobs list -r us-west-2 -n gco-jobs --status running
```

#### `gco jobs get`

Get details of a specific job.

```bash
gco jobs get JOB_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Job region (required) |
| `--namespace` | `-n` | Job namespace |

**Example:**

```bash
gco jobs get my-job --region us-east-1
gco jobs get training-job -r us-west-2 -n ml-jobs
```

#### `gco jobs logs`

Get logs from a job.

```bash
gco jobs logs JOB_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Job region (required) |
| `--namespace` | `-n` | Job namespace |
| `--tail` | `-t` | Number of lines to show |
| `--container` | `-c` | Container name (for multi-container pods) |

**Example:**

```bash
gco jobs logs my-job --region us-east-1
gco jobs logs my-job -r us-east-1 --tail 500
gco jobs logs multi-container-job -r us-east-1 --container sidecar
```

#### `gco jobs pod-logs`

Get logs from a specific pod of a job. Use this when a Job creates
multiple pods (parallelism > 1) and you need logs from a particular
replica. Use `gco jobs pods` first to list available pods.

```bash
gco jobs pod-logs JOB_NAME POD_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Job region (required) |
| `--namespace` | `-n` | Job namespace |
| `--tail` | `-t` | Number of lines to show |
| `--container` | `-c` | Container name (for multi-container pods) |

**Example:**

```bash
gco jobs pod-logs training-job training-job-abc123 -r us-east-1
gco jobs pod-logs multi-job multi-job-pod1 -r us-east-1 --container sidecar
```

#### `gco jobs delete`

Delete a job.

```bash
gco jobs delete JOB_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Job region (required) |
| `--namespace` | `-n` | Job namespace |
| `--yes` | `-y` | Skip confirmation |

**Example:**

```bash
gco jobs delete my-job --region us-east-1
gco jobs delete old-job -r us-west-2 -n ml-jobs -y
```

#### `gco jobs events`

Get Kubernetes events for a job.

```bash
gco jobs events JOB_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Job region (required) |
| `--namespace` | `-n` | Job namespace |

**Example:**

```bash
gco jobs events my-job --region us-east-1
gco jobs events training-job -r us-west-2 -n ml-jobs
```

#### `gco jobs pods`

Get pod details for a job.

```bash
gco jobs pods JOB_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Job region (required) |
| `--namespace` | `-n` | Job namespace |

**Example:**

```bash
gco jobs pods my-job --region us-east-1
gco jobs pods training-job -r us-west-2 -n ml-jobs
```

#### `gco jobs metrics`

Get resource usage metrics for a job.

```bash
gco jobs metrics JOB_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Job region (required) |
| `--namespace` | `-n` | Job namespace |

**Example:**

```bash
gco jobs metrics my-job --region us-east-1
gco jobs metrics training-job -r us-west-2 -n ml-jobs
```

#### `gco jobs retry`

Retry a failed job.

```bash
gco jobs retry JOB_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Job region (required) |
| `--namespace` | `-n` | Job namespace |
| `--yes` | `-y` | Skip confirmation |

**Example:**

```bash
gco jobs retry failed-job --region us-east-1
gco jobs retry training-job -r us-west-2 -n ml-jobs -y
```

#### `gco jobs bulk-delete`

Bulk delete jobs based on filters.

```bash
gco jobs bulk-delete [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Target region (required unless --all-regions) |
| `--all-regions` | `-a` | Delete across all regions |
| `--namespace` | `-n` | Filter by namespace |
| `--status` | `-s` | Filter by status |
| `--older-than-days` | `-d` | Delete jobs older than N days |
| `--label-selector` | `-l` | Kubernetes label selector |
| `--dry-run` | | Only show what would be deleted (default) |
| `--execute` | | Actually delete (disables dry-run) |
| `--yes` | `-y` | Skip confirmation |

**Example:**

```bash
gco jobs bulk-delete --region us-east-1 --status completed --older-than-days 7
gco jobs bulk-delete -r us-west-2 -n gco-jobs -s failed --execute -y
gco jobs bulk-delete --all-regions --status failed --older-than-days 30 --execute
```

#### `gco jobs health`

Get health status of GCO clusters.

```bash
gco jobs health [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Target region (required unless --all-regions) |
| `--all-regions` | `-a` | Get health across all regions |

**Example:**

```bash
gco jobs health --region us-east-1
gco jobs health --all-regions
```

#### `gco jobs queue-status`

View SQS queue status across regions.

```bash
gco jobs queue-status [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Filter by region |
| `--all-regions` | | Show all regions |

**Example:**

```bash
gco jobs queue-status --all-regions
gco jobs queue-status -r us-east-1
```

---

### Queue Commands

Manage the global job queue (DynamoDB-backed). The job queue provides centralized job submission and tracking across all regions.

<details>
<summary>All <code>gco queue</code> commands (5) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco queue submit`](#gco-queue-submit) | Submit a job to the global queue for regional pickup. |
| [`gco queue list`](#gco-queue-list) | List jobs in the global queue. |
| [`gco queue get`](#gco-queue-get) | Get details of a queued job including status history. |
| [`gco queue cancel`](#gco-queue-cancel) | Cancel a queued job (only works for jobs not yet running). |
| [`gco queue stats`](#gco-queue-stats) | Get job queue statistics by region and status. |

</details>

#### `gco queue submit`

Submit a job to the global queue for regional pickup.

```bash
gco queue submit MANIFEST_PATH [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Target region for job execution (required) |
| `--namespace` | `-n` | Kubernetes namespace |
| `--priority` | `-p` | Job priority (0-100, higher = more important) |
| `--label` | `-l` | Add labels (key=value), can be repeated |

**Example:**

```bash
gco queue submit job.yaml --region us-east-1
gco queue submit job.yaml -r us-west-2 --priority 50
gco queue submit job.yaml -r us-east-1 -l team=ml -l project=training
```

#### `gco queue list`

List jobs in the global queue.

```bash
gco queue list [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Filter by target region |
| `--status` | `-s` | Filter by status (queued, claimed, running, succeeded, failed, cancelled) |
| `--namespace` | `-n` | Filter by namespace |
| `--limit` | `-l` | Maximum results (default: 50) |

**Example:**

```bash
gco queue list
gco queue list --region us-east-1 --status queued
gco queue list -s running
```

#### `gco queue get`

Get details of a queued job including status history.

```bash
gco queue get JOB_ID [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Region to query (any region works) |

**Example:**

```bash
gco queue get abc123-def456
gco queue get abc123-def456 --region us-east-1
```

#### `gco queue cancel`

Cancel a queued job (only works for jobs not yet running).

```bash
gco queue cancel JOB_ID [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--reason` | | Cancellation reason |
| `--region` | `-r` | Region to query (any region works) |
| `--yes` | `-y` | Skip confirmation |

**Example:**

```bash
gco queue cancel abc123-def456
gco queue cancel abc123-def456 --reason "No longer needed" -y
```

#### `gco queue stats`

Get job queue statistics by region and status.

```bash
gco queue stats [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Region to query (any region works) |

**Example:**

```bash
gco queue stats
```

---

### Templates Commands

Manage job templates. Templates are reusable job configurations stored in DynamoDB with parameter substitution support.

<details>
<summary>All <code>gco templates</code> commands (5) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco templates list`](#gco-templates-list) | List all job templates. |
| [`gco templates get`](#gco-templates-get) | Get details of a specific template. |
| [`gco templates create`](#gco-templates-create) | Create a new job template from a manifest file. |
| [`gco templates delete`](#gco-templates-delete) | Delete a job template. |
| [`gco templates run`](#gco-templates-run) | Create and run a job from a template. |

</details>

#### `gco templates list`

List all job templates.

```bash
gco templates list [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Region to query |

**Example:**

```bash
gco templates list
```

#### `gco templates get`

Get details of a specific template.

```bash
gco templates get TEMPLATE_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Region to query |

**Example:**

```bash
gco templates get gpu-training-template
```

#### `gco templates create`

Create a new job template from a manifest file.

```bash
gco templates create MANIFEST_PATH [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--name` | `-n` | Template name (required) |
| `--description` | `-d` | Template description |
| `--param` | `-p` | Default parameter (key=value), can be repeated |
| `--region` | `-r` | Region to create in |

**Example:**

```bash
gco templates create job.yaml --name gpu-training -d "GPU training template"
gco templates create job.yaml -n my-template -p image=pytorch:latest -p gpus=4
```

#### `gco templates delete`

Delete a job template.

```bash
gco templates delete TEMPLATE_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Region |
| `--yes` | `-y` | Skip confirmation |

**Example:**

```bash
gco templates delete old-template -y
```

#### `gco templates run`

Create and run a job from a template.

```bash
gco templates run TEMPLATE_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--name` | `-n` | Job name (required) |
| `--region` | `-r` | Target region (required) |
| `--namespace` | | Kubernetes namespace |
| `--param` | `-p` | Parameter override (key=value), can be repeated |

**Example:**

```bash
gco templates run gpu-training --name my-job --region us-east-1
gco templates run gpu-template -n my-job -r us-east-1 -p image=custom:v1 -p gpus=8
```

---

### Webhooks Commands

Manage webhooks for job event notifications. Webhooks receive HTTP POST notifications when job events occur.

<details>
<summary>All <code>gco webhooks</code> commands (3) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco webhooks list`](#gco-webhooks-list) | List all registered webhooks. |
| [`gco webhooks create`](#gco-webhooks-create) | Register a new webhook for job events. |
| [`gco webhooks delete`](#gco-webhooks-delete) | Delete a webhook. |

</details>

#### `gco webhooks list`

List all registered webhooks.

```bash
gco webhooks list [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--namespace` | `-n` | Filter by namespace |
| `--region` | `-r` | Region to query |

**Example:**

```bash
gco webhooks list
gco webhooks list --namespace gco-jobs
```

#### `gco webhooks create`

Register a new webhook for job events.

```bash
gco webhooks create [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--url` | `-u` | Webhook URL (required) |
| `--event` | `-e` | Event type (job.started, job.completed, job.failed), can be repeated |
| `--namespace` | `-n` | Filter events by namespace |
| `--secret` | `-s` | HMAC secret for signature verification |
| `--region` | `-r` | Region to create in |

**Example:**

```bash
gco webhooks create --url https://example.com/webhook -e job.completed -e job.failed
gco webhooks create -u https://slack.com/webhook -e job.failed -n gco-jobs
```

#### `gco webhooks delete`

Delete a webhook.

```bash
gco webhooks delete WEBHOOK_ID [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Region |
| `--yes` | `-y` | Skip confirmation |

**Example:**

```bash
gco webhooks delete abc12345 -y
```

---

### Stacks Commands

Manage CDK infrastructure stacks.

<details>
<summary>All <code>gco stacks</code> commands (14) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco stacks list`](#gco-stacks-list) | List all GCO stacks. |
| [`gco stacks status`](#gco-stacks-status) | Get detailed status of a stack. |
| [`gco stacks deploy`](#gco-stacks-deploy) | Deploy a single stack. |
| [`gco stacks deploy-all`](#gco-stacks-deploy-all) | Deploy all stacks in correct order. |
| [`gco stacks destroy`](#gco-stacks-destroy) | Destroy a single stack. |
| [`gco stacks destroy-all`](#gco-stacks-destroy-all) | Destroy all stacks in correct order. |
| [`gco stacks bootstrap`](#gco-stacks-bootstrap) | Bootstrap CDK in a region. |
| [`gco stacks access`](#gco-stacks-access) | Configure kubectl access to a GCO EKS cluster. |
| [`gco stacks fsx`](#gco-stacks-fsx) | Manage FSx for Lustre storage. |
| [`gco stacks valkey`](#gco-stacks-valkey) | Manage Valkey Serverless cache. |
| [`gco stacks aurora`](#gco-stacks-aurora) | Manage Aurora PostgreSQL (pgvector) database. |
| [`gco stacks synth`](#gco-stacks-synth) | Synthesize CloudFormation templates without deploying. |
| [`gco stacks diff`](#gco-stacks-diff) | Show differences between deployed and local stacks. |
| [`gco stacks outputs`](#gco-stacks-outputs) | Get CloudFormation outputs from a deployed stack (e.g. API URLs, ARNs, secret references that the stack exposes). |

</details>

#### `gco stacks list`

List all GCO stacks.

```bash
gco stacks list [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Filter by region |
| `--all-regions` | | List from all regions |

#### `gco stacks status`

Get detailed status of a stack.

```bash
gco stacks status STACK_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Stack region |

**Example:**

```bash
gco stacks status gco-us-east-1 --region us-east-1
```

#### `gco stacks deploy`

Deploy a single stack. Automatically bootstraps CDK in the target region if needed.

```bash
gco stacks deploy STACK_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Stack region |
| `--yes` | `-y` | Skip confirmation |

**Example:**

```bash
gco stacks deploy gco-us-east-1 -y
```

#### `gco stacks deploy-all`

Deploy all stacks in correct order. Automatically bootstraps CDK in any un-bootstrapped regions before deploying.

```bash
gco stacks deploy-all [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--yes` | `-y` | Skip confirmation |
| `--parallel` | `-p` | Deploy regional stacks in parallel |
| `--max-workers` | `-w` | Max parallel workers (default: 4) |

**Example:**

```bash
gco stacks deploy-all -y
gco stacks deploy-all -y --parallel --max-workers 8
```

#### `gco stacks destroy`

Destroy a single stack.

```bash
gco stacks destroy STACK_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Stack region |
| `--yes` | `-y` | Skip confirmation |

#### `gco stacks destroy-all`

Destroy all stacks in correct order.

```bash
gco stacks destroy-all [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--yes` | `-y` | Skip confirmation |
| `--parallel` | `-p` | Destroy regional stacks in parallel |
| `--max-workers` | `-w` | Max parallel workers (default: 4) |

#### `gco stacks bootstrap`

Bootstrap CDK in a region. This is run automatically by `deploy` and `deploy-all` when needed, so manual bootstrapping is optional.

```bash
gco stacks bootstrap [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Region to bootstrap |

#### `gco stacks access`

Configure kubectl access to a GCO EKS cluster. Updates kubeconfig, creates an EKS access entry for your IAM principal, and associates the cluster admin policy. Handles assumed roles automatically.

```bash
gco stacks access [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--cluster` | `-c` | Cluster name (default: gco-{region}) |
| `--region` | `-r` | AWS region (default: first deployment region) |

**Examples:**

```bash
gco stacks access                             # Auto-detect region from cdk.json
gco stacks access -r us-west-2                # Specific region
gco stacks access -c my-cluster -r eu-west-1  # Custom cluster name
```

#### `gco stacks fsx`

Manage FSx for Lustre storage.

```bash
gco stacks fsx COMMAND [OPTIONS]
```

**Subcommands:**

- `status` - Show FSx status
- `enable` - Enable FSx for Lustre
- `disable` - Disable FSx for Lustre

**Example:**

```bash
gco stacks fsx status
gco stacks fsx enable --storage-capacity 1200 -y
gco stacks fsx disable -y
```

#### `gco stacks valkey`

Manage Valkey Serverless cache.

```bash
gco stacks valkey COMMAND [OPTIONS]
```

**Subcommands:**

- `status` - Show Valkey configuration status
- `enable` - Enable Valkey Serverless cache
- `disable` - Disable Valkey Serverless cache

**Example:**

```bash
gco stacks valkey status
gco stacks valkey enable --max-storage 10 --max-ecpu 10000 -y
gco stacks valkey disable -y
```

#### `gco stacks aurora`

Manage Aurora PostgreSQL (pgvector) database.

```bash
gco stacks aurora COMMAND [OPTIONS]
```

**Subcommands:**

- `status` - Show Aurora pgvector configuration status
- `enable` - Enable Aurora Serverless v2 with pgvector
- `disable` - Disable Aurora pgvector

**Example:**

```bash
gco stacks aurora status
gco stacks aurora enable --min-acu 2 --max-acu 32 --deletion-protection -y
gco stacks aurora disable -y
```

#### `gco stacks addons`

Inspect and re-converge cluster add-ons (Helm charts). Add-on installation is decoupled from the CloudFormation rollback path, so a chart that fails to install never rolls back the cluster.

```bash
gco stacks addons COMMAND [OPTIONS]
```

**Subcommands:**

- `status` - Show per-chart add-on install status (read from SSM)
- `install` - Re-run the Helm add-on installer (idempotent; never rolls back the cluster)

**Options (both subcommands):**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | AWS region (default: first deployment region) |
| `--all-regions` | `-A` | Apply across all deployment regions |

**Example:**

```bash
gco stacks addons status
gco stacks addons status --all-regions
gco stacks addons install -r us-west-2
```

#### `gco stacks synth`

Synthesize CloudFormation templates without deploying.

```bash
gco stacks synth [STACK_NAME] [OPTIONS]
```

**Arguments:**

- `STACK_NAME` - Optional stack name; omit to synthesize all stacks

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--quiet` | `-q` | Suppress synthesis output |

**Example:**

```bash
gco stacks synth                           # synthesize all
gco stacks synth gco-us-east-1
gco stacks synth -q                        # quiet mode
```

#### `gco stacks diff`

Show differences between deployed and local stacks.

```bash
gco stacks diff [STACK_NAME]
```

**Arguments:**

- `STACK_NAME` - Optional stack name; omit to diff all stacks

**Example:**

```bash
gco stacks diff
gco stacks diff gco-us-east-1
```

#### `gco stacks outputs`

Get CloudFormation outputs from a deployed stack (e.g. API URLs, ARNs,
secret references that the stack exposes).

```bash
gco stacks outputs STACK_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | AWS region (required) |

**Example:**

```bash
gco stacks outputs gco-us-east-1 -r us-east-1
gco stacks outputs gco-global -r us-east-2
```

---

### DAG Commands

Run multi-step job pipelines with dependencies. Define a DAG in YAML, and GCO runs steps in dependency order, skipping downstream steps if a dependency fails.

<details>
<summary>All <code>gco dag</code> commands (2) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco dag run`](#gco-dag-run) | Execute a DAG pipeline. |
| [`gco dag validate`](#gco-dag-validate) | Validate a DAG definition without running it. |

</details>

#### `gco dag run`

Execute a DAG pipeline.

```bash
gco dag run DAG_FILE [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Region to run in (default: from DAG file or first deployed) |
| `--timeout` | `-t` | Timeout per step in seconds (default: 3600) |
| `--dry-run` | | Validate and show execution order without running |

**Examples:**

```bash
# Run a pipeline
gco dag run pipeline.yaml -r us-east-1

# Preview execution order
gco dag run pipeline.yaml --dry-run
```

#### `gco dag validate`

Validate a DAG definition without running it. Checks for cycles, missing dependencies, and missing manifest files.

```bash
gco dag validate DAG_FILE
```

**Example:**

```bash
gco dag validate examples/pipeline-dag.yaml
```

#### DAG File Format

```yaml
name: my-pipeline
region: us-east-1          # optional, auto-detects if omitted
namespace: gco-jobs    # optional, defaults to gco-jobs

steps:
  - name: preprocess
    manifest: examples/preprocess-job.yaml

  - name: train
    manifest: examples/train-job.yaml
    depends_on: [preprocess]

  - name: evaluate
    manifest: examples/evaluate-job.yaml
    depends_on: [train]
```

Steps without `depends_on` run first. Steps with dependencies wait until all dependencies succeed. If a step fails, all downstream steps are automatically skipped.

Use shared EFS storage (`/mnt/shared`) to pass data between steps.

---

### Costs Commands

View cost breakdowns and estimates for GCO resources. Uses AWS Cost Explorer filtered by the `Project: GCO` tag applied to all resources.

**Setup (one-time):** To filter costs by the `Project` tag, you must activate cost allocation tags in your AWS account:

1. Go to the [AWS Billing Console → Cost Allocation Tags](https://us-east-1.console.aws.amazon.com/billing/home#/tags)
2. Search for the `Project` tag under "User-defined cost allocation tags"
3. Select it and click "Activate"
4. Wait ~24 hours for tag data to appear in Cost Explorer

Until the tag is activated, use `--all` to see total account costs:

```bash
gco costs summary --all
```

You can also activate the `Environment` and `Owner` tags for more granular filtering in the AWS Cost Explorer console.

<details>
<summary>All <code>gco costs</code> commands (5) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco costs summary`](#gco-costs-summary) | Show total GCO spend broken down by AWS service. |
| [`gco costs regions`](#gco-costs-regions) | Show cost breakdown by AWS region. |
| [`gco costs trend`](#gco-costs-trend) | Show daily cost trend with a visual bar chart. |
| [`gco costs workloads`](#gco-costs-workloads) | Estimate costs for currently running workloads (jobs and inference endpoints) based on instance pricing and runtime. |
| [`gco costs forecast`](#gco-costs-forecast) | Forecast GCO costs for the next N days based on historical spending patterns. |

</details>

#### `gco costs summary`

Show total GCO spend broken down by AWS service.

```bash
gco costs summary [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--days` | `-d` | Number of days to look back (default: 30) |
| `--all` | | Show all account costs, not filtered by GCO tag |

**Examples:**

```bash
# Last 30 days (default)
gco costs summary

# Last 7 days
gco costs summary --days 7

# All account costs (before tags are activated)
gco costs summary --all

# JSON output
gco --output json costs summary
```

#### `gco costs regions`

Show cost breakdown by AWS region.

```bash
gco costs regions [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--days` | `-d` | Number of days to look back (default: 30) |

**Examples:**

```bash
gco costs regions
gco costs regions --days 7
```

#### `gco costs trend`

Show daily cost trend with a visual bar chart.

```bash
gco costs trend [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--days` | `-d` | Number of days to show (default: 14) |
| `--all` | | Show all account costs, not filtered by GCO tag |

**Examples:**

```bash
gco costs trend
gco costs trend --days 7
gco costs trend --all
```

#### `gco costs workloads`

Estimate costs for currently running workloads (jobs and inference endpoints) based on instance pricing and runtime.

```bash
gco costs workloads [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Region to check (default: all deployment regions) |

**Examples:**

```bash
# All regions
gco costs workloads

# Specific region
gco costs workloads -r us-east-1
```

#### `gco costs forecast`

Forecast GCO costs for the next N days based on historical spending patterns.

```bash
gco costs forecast [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--days` | `-d` | Days to forecast ahead (default: 30) |

**Examples:**

```bash
gco costs forecast
gco costs forecast --days 60
```

> **Note:** Cost Explorer needs at least 14 days of historical data to generate forecasts.

---

### Capacity Commands

Check and manage cluster capacity.

<details>
<summary>All <code>gco capacity</code> commands (16) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco capacity check`](#gco-capacity-check) | Check capacity for a specific instance type. |
| [`gco capacity status`](#gco-capacity-status) | View capacity status across regions. |
| [`gco capacity recommend`](#gco-capacity-recommend) | Get capacity recommendation for an instance type. |
| [`gco capacity recommend-region`](#gco-capacity-recommend-region) | Get optimal region recommendation. |
| [`gco capacity ai-recommend`](#gco-capacity-ai-recommend) | Get AI-powered capacity recommendation using Amazon Bedrock. |
| [`gco capacity reservations`](#gco-capacity-reservations) | List On-Demand Capacity Reservations (ODCRs) across deployed regions. |
| [`gco capacity reservation-check`](#gco-capacity-reservation-check) | Check reservation availability and Capacity Block offerings for ML workloads. |
| [`gco capacity find-blocks`](#gco-capacity-find-blocks) | Find Capacity Blocks across regions, durations, and a start-date window in one consolidated, ranked report. |
| [`gco capacity reserve`](#gco-capacity-reserve) | Purchase a Capacity Block offering by ID. |
| [`gco capacity instance-info`](#gco-capacity-instance-info) | Print AWS-published metadata for an instance type — vCPUs, memory, GPU count, network performance, and supported architectures. |
| [`gco capacity spot-prices`](#gco-capacity-spot-prices) | Get spot price history for an instance type in a region. |
| [`gco capacity history`](#gco-capacity-history) | Query the historical capacity surface (optional global-stack add-on, on by default). |
| [`gco capacity history show`](#gco-capacity-history-show) | Show the recorded capacity time-series for an instance type in a region. |
| [`gco capacity history stats`](#gco-capacity-history-stats) | Show p25/p50/p75/min/max/stddev per metric over a time window. |
| [`gco capacity history patterns`](#gco-capacity-history-patterns) | Show a day-of-week by hour heatmap of average spot scores. |
| [`gco capacity predict`](#gco-capacity-predict) | Predict the best time to acquire capacity from historical patterns (Bedrock). |

</details>

#### `gco capacity check`

Check capacity for a specific instance type.

```bash
gco capacity check [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--instance-type` | `-i` | Instance type to check |
| `--region` | `-r` | Region to check |
| `--type` | `-t` | Capacity type: `spot`, `on-demand`, or `both` |
| `--enrich-historical` | | Append historical capacity context to the output (requires historical.enabled) |

**Example:**

```bash
gco capacity check --instance-type g4dn.xlarge --region us-east-1
gco capacity check -i g5.xlarge -r us-west-2 -t spot
```

#### `gco capacity status`

View capacity status across regions.

```bash
gco capacity status [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Filter by region |

#### `gco capacity recommend`

Get capacity recommendation for an instance type.

```bash
gco capacity recommend [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--instance-type` | `-i` | Instance type |
| `--region` | `-r` | Region |

#### `gco capacity recommend-region`

Get optimal region recommendation.

```bash
gco capacity recommend-region [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--gpu` | | Recommend for GPU workloads |
| `--instance-type` | `-i` | Specific instance type (enables weighted scoring) |
| `--gpu-count` | | Number of GPUs required |
| `--min-gpus` | | Minimum GPUs required |

When `--instance-type` is provided, the recommendation uses weighted multi-signal
scoring that combines spot placement scores, spot-vs-on-demand pricing, queue depth,
GPU utilization, and running job counts. Without it, a simpler composite score is used.

**Example:**

```bash
gco capacity recommend-region --gpu
gco capacity recommend-region -i g5.xlarge
gco capacity recommend-region -i p4d.24xlarge --gpu-count 8
```

#### `gco capacity ai-recommend`

Get AI-powered capacity recommendation using Amazon Bedrock.

⚠️ **DISCLAIMER**: Recommendations are AI-generated and should be validated before making production decisions. Capacity availability and pricing can change rapidly.

```bash
gco capacity ai-recommend [OPTIONS]
```

This command gathers comprehensive capacity data including:

- Spot placement scores and pricing across regions
- On-demand availability and pricing
- Current cluster utilization (queue depth, GPU/CPU usage)
- Running and pending job counts

The data is analyzed by an LLM (Claude by default) to provide intelligent recommendations.

**Requirements:**

- AWS credentials with `bedrock:InvokeModel` permission
- The specified Bedrock model must be enabled in your account

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--workload` | `-w` | Description of your workload |
| `--instance-type` | `-i` | Instance types to consider (can specify multiple) |
| `--region` | `-r` | Regions to consider (can specify multiple) |
| `--gpu` | | Workload requires GPUs |
| `--min-gpus` | | Minimum GPUs required |
| `--min-memory-gb` | | Minimum memory in GB |
| `--fault-tolerance` | `-f` | Fault tolerance level: `high`, `medium`, `low` |
| `--max-cost` | | Maximum cost per hour in USD |
| `--model` | `-m` | Bedrock model ID to use |
| `--raw` | | Show raw AI response |

**Example:**

```bash
# Basic recommendation
gco capacity ai-recommend --workload "Training a large language model"

# GPU workload with specific requirements
gco capacity ai-recommend -w "Inference workload" --gpu --min-gpus 4

# Compare specific instance types and regions
gco capacity ai-recommend -i g5.xlarge -i g5.2xlarge -r us-east-1 -r us-west-2

# Cost-constrained recommendation
gco capacity ai-recommend --fault-tolerance high --max-cost 5.00

# Use a different model
gco capacity ai-recommend -w "ML training" --model us.anthropic.claude-3-haiku-20240307-v1:0
```

#### `gco capacity reservations`

List On-Demand Capacity Reservations (ODCRs) across deployed regions.

```bash
gco capacity reservations [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `-i, --instance-type` | Filter by instance type |
| `-r, --region` | Specific region (default: all deployed regions) |

```bash
# List all active reservations
gco capacity reservations

# Filter by instance type
gco capacity reservations -i p5.48xlarge

# Check a specific region
gco capacity reservations -r us-east-1
```

#### `gco capacity reservation-check`

Check reservation availability and Capacity Block offerings for ML workloads. Checks both existing ODCRs and purchasable Capacity Blocks (guaranteed GPU capacity for a fixed duration at a known price). Pass `--region` more than once to check several regions in parallel, and use `--earliest-start`/`--latest-start` to ask for blocks starting near a date. For a full duration-range sweep that returns one consolidated ranked report, use [`gco capacity find-blocks`](#gco-capacity-find-blocks).

```bash
gco capacity reservation-check [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `-i, --instance-type` | Instance type to check (required) |
| `-r, --region` | Region(s) to check; repeatable (default: all deployed regions) |
| `-c, --count` | Minimum instances needed (default: 1) |
| `--include-blocks/--no-blocks` | Include Capacity Block offerings (default: yes) |
| `--block-duration` | Capacity Block duration in hours (default: 24) |
| `--block-duration-days` | Capacity Block duration in days (overrides `--block-duration`) |
| `--earliest-start` | Earliest block start date (`YYYY-MM-DD` or ISO datetime) |
| `--latest-start` | Latest block start date (`YYYY-MM-DD` or ISO datetime) |

```bash
# Check for p5.48xlarge reservations and block offerings
gco capacity reservation-check -i p5.48xlarge

# Check with specific count and duration
gco capacity reservation-check -i p4d.24xlarge -c 2 --block-duration 48

# ODCRs only, no block offerings
gco capacity reservation-check -i g5.48xlarge -r us-east-1 --no-blocks

# Two regions, a 14-day block starting on/after a date
gco capacity reservation-check -i p5.48xlarge -r us-east-1 -r us-west-2 \
  --block-duration-days 14 --earliest-start 2026-07-01
```

#### `gco capacity find-blocks`

Find EC2 Capacity Blocks for ML across regions, durations, and a start-date window in a single call. One command fans out across every requested region and every valid Capacity Block duration in the range (in parallel), then returns one consolidated, de-duplicated, ranked report — cheapest per-GPU-hour first — with per-hour and per-GPU-hour pricing and the longest available block. This replaces the manual multi-call sweeping the older `reservation-check` required to bound "where and when can I get this GPU for N days?".

```bash
gco capacity find-blocks [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `-i, --instance-type` | GPU instance type or friendly alias, e.g. `p6-b200` (required) |
| `-r, --region` | Region(s) to search; repeatable (default: all deployed regions) |
| `-c, --count` | Instances per block (default: 1) |
| `--duration-days` | Single target duration in days |
| `--duration-hours` | Single target duration in hours |
| `--min-duration-days` / `--max-duration-days` | Duration range bounds, in days |
| `--min-duration-hours` / `--max-duration-hours` | Duration range bounds, in hours |
| `--earliest-start` | Earliest block start (`YYYY-MM-DD` or ISO datetime) |
| `--latest-start` | Latest block start (`YYYY-MM-DD` or ISO datetime) |
| `--find-longest` | Sweep the duration ladder and surface the longest available block |

**Allowed Capacity Block durations.** AWS accepts reservation durations in **1-day increments up to 14 days, then 7-day increments up to 182 days** (26 weeks). Because the `DescribeCapacityBlockOfferings` API requires an exact duration per query, a duration *range* is expanded to those discrete valid values automatically (e.g. `1`–`63` days probes 1…14, 21, 28, …, 63 days). All Capacity Blocks end at 11:30 UTC, so a returned block's actual duration is the closest valid match to your request and is reported per offering.

**Instance-type notes.** Friendly names are normalized (`p6-b200` → `p6-b200.48xlarge`, `p6-b300` → `p6-b300.48xlarge`). Both B200 and B300 (Blackwell Ultra) are standalone EC2 instance types and resolve normally. The Grace-Blackwell **GB200/GB300** NVL72 *superchips* ship only as **P6e-GB200 / P6e-GB300 UltraServers** (never as a standalone `InstanceType`), so those names are flagged with guidance toward the UltraServer flow rather than silently returning nothing. Unknown/typo'd types are reported as invalid (distinct from a valid type that simply has zero offerings).

```bash
# Bound the motivating scenario in a single call:
gco capacity find-blocks -i p6-b200.48xlarge \
  -r us-east-1 -r us-east-2 -r us-west-2 -r eu-west-1 \
  --min-duration-days 1 --max-duration-days 63 \
  --earliest-start 2026-07-01 --latest-start 2026-07-10

# A single 14-day block in one region
gco capacity find-blocks -i p5.48xlarge -r us-east-1 --duration-days 14

# Longest block available in the next window
gco capacity find-blocks -i p5.48xlarge -r us-east-1 --find-longest

# JSON for scripting (consolidated report with ranked offerings)
gco --output json capacity find-blocks -i p6-b200 -r us-east-1 --max-duration-days 7
```

#### `gco capacity reserve`

Purchase a Capacity Block offering by ID. Use `gco capacity reservation-check`
first to find available offerings, then pass the `cb-...` ID to this
command. Always validate with `--dry-run` before committing — Capacity
Block purchases incur charges.

```bash
gco capacity reserve [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--offering-id` | `-o` | Capacity Block offering ID (`cb-…`) (required) |
| `--region` | `-r` | AWS region where the offering exists (required) |
| `--dry-run` | | Validate the offering without purchasing |

**Example:**

```bash
# Find offerings, validate, then purchase
gco capacity reservation-check -i p4d.24xlarge -r us-east-1
gco capacity reserve -o cb-0123456789abcdef0 -r us-east-1 --dry-run
gco capacity reserve -o cb-0123456789abcdef0 -r us-east-1
```

#### `gco capacity instance-info`

Print AWS-published metadata for an instance type — vCPUs, memory,
GPU count, network performance, and supported architectures. Read-only
and does not call the EC2 RunInstances API.

```bash
gco capacity instance-info INSTANCE_TYPE
```

**Example:**

```bash
gco capacity instance-info g5.xlarge
gco capacity instance-info p5.48xlarge
```

#### `gco capacity spot-prices`

Get spot price history for an instance type in a region.

```bash
gco capacity spot-prices [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--instance-type` | `-i` | EC2 instance type (required) |
| `--region` | `-r` | AWS region (required) |
| `--days` | `-d` | Days of history to retrieve |

**Example:**

```bash
gco capacity spot-prices -i g5.xlarge -r us-east-1
gco capacity spot-prices -i p4d.24xlarge -r us-west-2 -d 30
```

#### `gco capacity history`

Query the historical capacity surface, an optional add-on to the global stack (not a separate stack) that is enabled by default. Set `historical.enabled` to `false` in cdk.json to opt out. The poller writes time-series snapshots to DynamoDB; when none are available yet the subcommands print a clear notice.

#### `gco capacity history show`

Show the recorded capacity time-series (spot score, spot price, AZ coverage, queue depth, capacity-block availability) for an instance type in a region.

```bash
gco capacity history show [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--instance-type` | `-i` | EC2 instance type (required) |
| `--region` | `-r` | AWS region (required) |
| `--hours` | `-H` | Hours of history to show (default 168 = 7 days) |

**Example:**

```bash
gco capacity history show -i g5.xlarge -r us-east-1
gco capacity history show -i p5.48xlarge -r us-east-1 -H 72
```

#### `gco capacity history stats`

Show a statistical summary (p25/p50/p75, min, max, mean, stddev) for each metric over the window.

```bash
gco capacity history stats [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--instance-type` | `-i` | EC2 instance type (required) |
| `--region` | `-r` | AWS region (required) |
| `--hours` | `-H` | Hours of history to summarize (default 168 = 7 days) |

**Example:**

```bash
gco capacity history stats -i g5.xlarge -r us-east-1
```

#### `gco capacity history patterns`

Show a day-of-week by hour heatmap grid of average spot placement scores, plus the best historical windows.

```bash
gco capacity history patterns [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--instance-type` | `-i` | EC2 instance type (required) |
| `--region` | `-r` | AWS region (required) |
| `--hours` | `-H` | Hours of history to analyze (default 168 = 7 days) |

**Example:**

```bash
gco capacity history patterns -i g5.xlarge -r us-east-1
```

#### `gco capacity predict`

Predict the best time to acquire capacity from historical patterns using Amazon
Bedrock. Combines the historical capacity surface (an optional add-on to the
global stack, enabled by default) with an LLM to recommend the day/hour windows
with the best spot availability and pricing, and which windows to avoid.
Requires collected history. Pass `--all-regions` to run the prediction for every
region that has data for the instance type instead of a single `--region`.

```bash
gco capacity predict [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--instance-type` | `-i` | EC2 instance type (required) |
| `--region` | `-r` | AWS region (omit when using `--all-regions`) |
| `--all-regions` | `-a` | Predict across every region that has historical data for the instance type |
| `--hours` | `-H` | Hours of history to analyze (default 168 = 7 days) |
| `--model` | `-m` | Bedrock model ID to use |
| `--raw` | | Show the raw AI response |

**Example:**

```bash
gco capacity predict -i p5.48xlarge -r us-east-1
gco capacity predict -i g5.xlarge -r us-west-2 -H 336
gco capacity predict -i g5.xlarge --all-regions
```

---

### Inference Commands

Manage multi-region inference endpoints. Endpoints are stored in DynamoDB and reconciled by the `inference_monitor` in each target region.

See [Inference Guide](INFERENCE.md) for architecture details and workflows.

<details>
<summary>All <code>gco inference</code> commands (17) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco inference deploy`](#gco-inference-deploy) | Deploy an inference endpoint to one or more regions. |
| [`gco inference list`](#gco-inference-list) | List inference endpoints. |
| [`gco inference status`](#gco-inference-status) | Show detailed status of an inference endpoint including per-region sync state. |
| [`gco inference scale`](#gco-inference-scale) | Scale an inference endpoint to a new replica count (applied across all target regions). |
| [`gco inference stop`](#gco-inference-stop) | Stop an inference endpoint (scales to zero, keeps configuration). |
| [`gco inference start`](#gco-inference-start) | Start a stopped inference endpoint. |
| [`gco inference delete`](#gco-inference-delete) | Delete an inference endpoint from all regions. |
| [`gco inference update-image`](#gco-inference-update-image) | Update the container image for an endpoint. |
| [`gco inference invoke`](#gco-inference-invoke) | Send a request to an inference endpoint via the API Gateway. |
| [`gco inference health`](#gco-inference-health) | Check if an inference endpoint is healthy and ready to serve requests. |
| [`gco inference models`](#gco-inference-models) | List models loaded on an inference endpoint. |
| [`gco inference canary`](#gco-inference-canary) | Start a canary deployment with a new image. |
| [`gco inference promote`](#gco-inference-promote) | Promote the canary to primary. |
| [`gco inference rollback`](#gco-inference-rollback) | Remove the canary deployment, keeping the primary unchanged. |
| [`gco inference set-topology`](#gco-inference-set-topology) | Resize a disaggregated endpoint's prefill/decode replica counts without redeploying. |
| [`gco inference configure-store`](#gco-inference-configure-store) | Update the shared KV-cache store on a Mooncake `store`/`both` endpoint. |
| [`gco inference populate-kv`](#gco-inference-populate-kv) | Upload data into an endpoint's Mooncake KV-cache cold tier. |

</details>

#### `gco inference deploy`

Deploy an inference endpoint to one or more regions.

```bash
gco inference deploy ENDPOINT_NAME [OPTIONS]
```

**Arguments:**

- `ENDPOINT_NAME` - Unique name for the endpoint

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--image` | `-i` | Container image (required) |
| `--region` | `-r` | Target region(s), repeatable (default: all deployed regions) |
| `--replicas` | | Replicas per region (default: 1) |
| `--gpu-count` | | GPUs per replica (default: 1) |
| `--gpu-type` | | GPU instance type hint (e.g. g5.xlarge) |
| `--port` | | Container port (default: 8000) |
| `--model-path` | | EFS path for model weights |
| `--model-source` | | S3 URI for model weights (auto-synced via init container) |
| `--health-path` | | Health check endpoint path (default: /health) |
| `--env` | `-e` | Environment variable (KEY=VALUE), repeatable |
| `--namespace` | `-n` | Kubernetes namespace (default: gco-inference) |
| `--label` | `-l` | Label (key=value), repeatable |
| `--min-replicas` | | Autoscaling: minimum replicas |
| `--max-replicas` | | Autoscaling: maximum replicas |
| `--autoscale-metric` | | Autoscaling metric (e.g. `cpu:70`, `memory:80`, `gpu:60`), repeatable. CPU/memory use the native HPA; gpu/gpu_memory scale via KEDA + CloudWatch. |
| `--capacity-type` | | Node capacity type: `on-demand` (default) or `spot` |
| `--accelerator` | `nvidia` | Accelerator type: `nvidia` for GPU instances, `neuron` for Trainium/Inferentia |
| `--node-selector` | | Node selector (key=value), repeatable. E.g. `eks.amazonaws.com/instance-family=inf2` |
| `--extra-args` | | Extra arguments passed to the container (e.g. `--kv-transfer-config {...}`). Repeatable |
| `--mooncake-mode` | | Mooncake serving mode: `disaggregated` (prefill/decode split), `store` (shared KV-cache), or `both` |
| `--prefill-replicas` | | Number of prefill replicas (default: 1). Used with `--mooncake-mode disaggregated\|both` |
| `--decode-replicas` | | Number of decode replicas (default: 1). Used with `--mooncake-mode disaggregated\|both` |
| `--mooncake-autoscale` | | Per-role autoscaling as `ROLE:MIN:MAX[:METRIC:TARGET...]`. Repeatable. E.g. `prefill:1:8:gpu:70` |
| `--mooncake-cold-tier` | | Enable the async per-region S3 cold tier for the shared KV-cache store. Requires `--mooncake-mode store\|both`. Pre-warm with `gco inference populate-kv` |
| `--mooncake-proxy-image` | | Container image for the prefill-decode proxy (disaggregated/both). Defaults to the endpoint image |
| `--mooncake-admin-key-secret` | | Name of an existing Kubernetes Secret holding the prefill-decode proxy `ADMIN_API_KEY`. Optional — when omitted, each region's monitor auto-provisions a `{name}-admin` Secret with a generated key |
| `--no-rewrite-image` | | Disable automatic image rewriting to the regional ECR mirror |

**Example:**

```bash
gco inference deploy my-llm -i vllm/vllm-openai:v0.22.0
gco inference deploy llama3-70b \
  -i vllm/vllm-openai:v0.22.0 \
  -r us-east-1 -r eu-west-1 \
  --replicas 2 --gpu-count 4 \
  --model-source s3://bucket/models/llama3-70b \
  -e MODEL=/models/llama3-70b

# Deploy with autoscaling (creates a Kubernetes HPA)
gco inference deploy my-llm \
  -i vllm/vllm-openai:v0.22.0 \
  --replicas 2 --gpu-count 1 \
  --min-replicas 1 --max-replicas 8 \
  --autoscale-metric cpu:70 --autoscale-metric memory:80
```

#### `gco inference list`

List inference endpoints.

```bash
gco inference list [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--state` | `-s` | Filter by state (deploying, running, stopped, deleted) |
| `--region` | `-r` | Filter by target region |

**Example:**

```bash
gco inference list
gco inference list --state running
gco inference list -r us-east-1
```

#### `gco inference status`

Show detailed status of an inference endpoint including per-region sync state.

```bash
gco inference status ENDPOINT_NAME
```

**Example:**

```bash
gco inference status my-llm
```

#### `gco inference scale`

Scale an inference endpoint to a new replica count (applied across all target regions).

```bash
gco inference scale ENDPOINT_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--replicas` | `-r` | New replica count (required) |

**Example:**

```bash
gco inference scale my-llm --replicas 4
```

#### `gco inference stop`

Stop an inference endpoint (scales to zero, keeps configuration).

```bash
gco inference stop ENDPOINT_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--yes` | `-y` | Skip confirmation |

**Example:**

```bash
gco inference stop my-llm -y
```

#### `gco inference start`

Start a stopped inference endpoint.

```bash
gco inference start ENDPOINT_NAME
```

**Example:**

```bash
gco inference start my-llm
```

#### `gco inference delete`

Delete an inference endpoint from all regions. The inference_monitor in each region cleans up K8s resources.

```bash
gco inference delete ENDPOINT_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--yes` | `-y` | Skip confirmation |

**Example:**

```bash
gco inference delete my-llm -y
```

#### `gco inference update-image`

Update the container image for an endpoint. Triggers a rolling update across all target regions.

```bash
gco inference update-image ENDPOINT_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--image` | `-i` | New container image (required) |

**Example:**

```bash
gco inference update-image my-llm -i vllm/vllm-openai:v0.22.0
```

#### `gco inference invoke`

Send a request to an inference endpoint via the API Gateway. Auto-detects the framework (vLLM, TGI, Triton) and builds the appropriate request body.

```bash
gco inference invoke ENDPOINT_NAME [OPTIONS]
```

**Arguments:**

- `ENDPOINT_NAME` - Name of the inference endpoint

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--prompt` | `-p` | Text prompt to send |
| `--data` | `-d` | Raw JSON body (overrides --prompt) |
| `--path` | | API sub-path (default: auto-detected from image) |
| `--region` | `-r` | Target region for the request |
| `--max-tokens` | | Max tokens to generate (default: 100) |
| `--stream/--no-stream` | | Stream the response |

**Example:**

```bash
# Simple prompt (auto-detects vLLM OpenAI-compatible format)
gco inference invoke my-llm -p "What is GPU orchestration?"

# With max tokens
gco inference invoke my-llm -p "Explain Kubernetes" --max-tokens 200

# Raw JSON body
gco inference invoke my-llm -d '{"prompt": "Hello", "max_tokens": 50}'

# Explicit API path
gco inference invoke my-llm -p "Hello" --path /v1/chat/completions
```

#### `gco inference health`

Check if an inference endpoint is healthy and ready to serve requests. Hits the endpoint's health check path and reports HTTP status and round-trip latency.

```bash
gco inference health ENDPOINT_NAME [OPTIONS]
```

**Arguments:**

- `ENDPOINT_NAME` - Name of the inference endpoint

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Target region to check |

**Example:**

```bash
# Check health (nearest region via Global Accelerator)
gco inference health my-llm

# Check health in a specific region
gco inference health my-llm -r us-east-1
```

#### `gco inference models`

List models loaded on an inference endpoint. Queries the `/v1/models` path (OpenAI-compatible) to discover which models are available.

```bash
gco inference models ENDPOINT_NAME [OPTIONS]
```

**Arguments:**

- `ENDPOINT_NAME` - Name of the inference endpoint

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Target region to query |

**Example:**

```bash
# List loaded models
gco inference models my-llm

# Query a specific region
gco inference models my-llm -r eu-west-1
```

#### `gco inference canary`

Start a canary deployment with a new image. Routes a percentage of traffic to the canary while the primary continues serving the rest.

```bash
gco inference canary ENDPOINT_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--image` | `-i` | New container image for canary (required) |
| `--weight` | `-w` | Percentage of traffic to canary, 1-99 (default: 10) |
| `--replicas` | `-r` | Number of canary replicas (default: 1) |

**Examples:**

```bash
# 10% traffic to new version
gco inference canary my-llm -i vllm/vllm-openai:v0.22.0

# 25% traffic with 2 canary replicas
gco inference canary my-llm -i vllm/vllm-openai:v0.22.0 -w 25 -r 2
```

#### `gco inference promote`

Promote the canary to primary. Replaces the primary image with the canary image and removes the canary deployment. All traffic goes to the new image.

```bash
gco inference promote ENDPOINT_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--yes` | `-y` | Skip confirmation |

**Example:**

```bash
gco inference promote my-llm -y
```

#### `gco inference rollback`

Remove the canary deployment, keeping the primary unchanged. All traffic returns to the primary.

```bash
gco inference rollback ENDPOINT_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--yes` | `-y` | Skip confirmation |

**Example:**

```bash
gco inference rollback my-llm -y
```

#### `gco inference set-topology`

Resize a disaggregated endpoint's prefill/decode replica counts without redeploying.

```bash
gco inference set-topology ENDPOINT_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--prefill` | | New prefill replica count (required) |
| `--decode` | | New decode replica count (required) |

**Example:**

```bash
gco inference set-topology my-llm --prefill 3 --decode 6
```

#### `gco inference configure-store`

Update the shared KV-cache store on a Mooncake `store`/`both` endpoint. Settings merge onto the endpoint's existing store block and re-trigger reconciliation, so changing one field leaves the others intact.

```bash
gco inference configure-store ENDPOINT_NAME [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--cold-tier` / `--no-cold-tier` | Opt the endpoint into (or out of) the async S3 cold tier. Enabling it also enables the shared store it extends |
| `--offload` | KV-store offload tier: `cpu`, `disk`, or `none` |
| `--global-segment-size` | Global segment size in bytes |
| `--local-buffer-size` | Local buffer size in bytes |
| `--enable-store` / `--disable-store` | Enable or disable the shared KV-cache store |

**Example:**

```bash
gco inference configure-store my-llm --cold-tier
gco inference configure-store my-llm --offload cpu --local-buffer-size 2147483648
```

#### `gco inference populate-kv`

Upload data into an endpoint's Mooncake KV-cache cold tier. Objects are written to the region's general-purpose bucket under the `mooncake-kv/<endpoint>/` prefix the endpoint reads from, pre-warming its prefix cache. The endpoint must have the cold tier enabled (`--mooncake-cold-tier` at deploy, or `configure-store --cold-tier`) for its pods to consume the data.

```bash
gco inference populate-kv ENDPOINT_NAME LOCAL_PATH --region REGION
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Region whose general-purpose bucket backs the endpoint's cold tier (required) |

**Example:**

```bash
gco inference populate-kv my-llm ./kv-warm-set/ --region us-east-1
```

---

### Models Commands

Manage model weights in the central S3 bucket. Models uploaded here are automatically available to inference endpoints across all regions via init container sync.

See [Inference Guide](INFERENCE.md) for details on model weight management.

<details>
<summary>All <code>gco models</code> commands (5) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco models upload`](#gco-models-upload) | Upload model weights to the central S3 bucket. |
| [`gco models upload-regional`](#gco-models-upload-regional) | Upload local files or a directory to a region's general-purpose regional bucket (`gco-regional-shared-<account>-<region>`), resolved from that region's own SSM parameter. |
| [`gco models list`](#gco-models-list) | List models in the central S3 bucket. |
| [`gco models delete`](#gco-models-delete) | Delete a model and all its files from the S3 bucket. |
| [`gco models uri`](#gco-models-uri) | Get the S3 URI for a model (for use with `--model-source` in inference deploy). |

</details>

#### `gco models upload`

Upload model weights to the central S3 bucket.

```bash
gco models upload LOCAL_PATH [OPTIONS]
```

**Arguments:**

- `LOCAL_PATH` - Local file or directory path

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--name` | `-n` | Model name in the registry (required) |

**Example:**

```bash
gco models upload ./my-model-weights/ --name llama3-8b
gco models upload ./weights.safetensors --name my-model
```

#### `gco models upload-regional`

Upload local files or a directory to a region's general-purpose regional bucket (`gco-regional-shared-<account>-<region>`), resolved from that region's own SSM parameter. The bucket is general purpose and usable by any in-region workload; it also backs the Mooncake cold tier. To warm an endpoint's KV cache specifically, prefer `gco inference populate-kv`, which targets the cold-tier key prefix.

```bash
gco models upload-regional LOCAL_PATH --region REGION [OPTIONS]
```

**Arguments:**

- `LOCAL_PATH` - Local file or directory path

**Options:**

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--region` | `-r` | | Target region whose regional bucket receives the objects (required) |
| `--prefix` | | `uploads` | S3 key prefix for uploaded objects |

**Example:**

```bash
gco models upload-regional ./data/ --region us-east-1
gco models upload-regional ./file.bin -r eu-west-1 --prefix datasets
```

#### `gco models list`

List models in the central S3 bucket.

```bash
gco models list
```

**Example:**

```bash
gco models list
```

#### `gco models delete`

Delete a model and all its files from the S3 bucket.

```bash
gco models delete MODEL_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--yes` | `-y` | Skip confirmation |

**Example:**

```bash
gco models delete llama3-8b -y
```

#### `gco models uri`

Get the S3 URI for a model (for use with `--model-source` in inference deploy).

```bash
gco models uri MODEL_NAME
```

**Example:**

```bash
gco models uri llama3-8b
# Output: s3://gco-models-xxx/models/llama3-8b
```

---

### Images Commands

Manage container images in the project ECR registry. The CLI talks to
the global registry (the regional ECR replicas are populated by ECR
replication automatically). Project repositories live under the
`gco/` prefix; non-`gco/*` repositories cannot be touched through the
CLI.

See [Customization Guide](CUSTOMIZATION.md) for the full registry
architecture and lifecycle policy options.

<details>
<summary>All <code>gco images</code> commands (14) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco images init`](#gco-images-init) | Create a project repository with the default lifecycle policy applied (keep 20 tagged images, expire untagged after 7 days). |
| [`gco images list`](#gco-images-list) | List every repository under the project's `gco/` prefix. |
| [`gco images tags`](#gco-images-tags) | List every tag in a repository (one row per tag, plus an "untagged" row aggregating untagged images). |
| [`gco images describe`](#gco-images-describe) | Print the full ECR details for a single image tag — digest, push time, size, scan findings. |
| [`gco images uri`](#gco-images-uri) | Print the registry URI for an image without making any AWS calls. |
| [`gco images build`](#gco-images-build) | Build a container image and push it to the project's ECR repo. |
| [`gco images push`](#gco-images-push) | Push an already-built local image to the project's ECR repo. |
| [`gco images delete-tag`](#gco-images-delete-tag) | Delete a single tag from a repository. |
| [`gco images delete-repo`](#gco-images-delete-repo) | Delete a whole repository. |
| [`gco images cleanup`](#gco-images-cleanup) | Remove untagged images across one or all project repos. |
| [`gco images prune`](#gco-images-prune) | Remove untagged images older than 30 days. |
| [`gco images orphans`](#gco-images-orphans) | List tags older than `threshold_days` that are not referenced by any deployed inference endpoint or recent job. |
| [`gco images lifecycle`](#gco-images-lifecycle) | Lifecycle policy management. |
| [`gco images replication`](#gco-images-replication) | Replication management for the project's ECR registry. |

</details>

#### `gco images init`

Create a project repository with the default lifecycle policy applied
(keep 20 tagged images, expire untagged after 7 days).

```bash
gco images init NAME [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--retain / --no-retain` | Apply `gco:retain=true` so the repo survives stack destroy |

**Example:**

```bash
gco images init my-app
gco images init prod-svc --retain
```

#### `gco images list`

List every repository under the project's `gco/` prefix.

```bash
gco images list
```

#### `gco images tags`

List every tag in a repository (one row per tag, plus an "untagged"
row aggregating untagged images).

```bash
gco images tags NAME
```

**Example:**

```bash
gco images tags my-app
```

#### `gco images describe`

Print the full ECR details for a single image tag — digest, push time,
size, scan findings.

```bash
gco images describe NAME TAG
```

**Example:**

```bash
gco images describe my-app v1.2.3
```

#### `gco images uri`

Print the registry URI for an image without making any AWS calls.
Useful in shell scripts that need the resolved URI to plug into a
manifest.

```bash
gco images uri NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--tag` | `-t` | Image tag (default: `latest`) |

**Example:**

```bash
gco images uri my-app -t v1.2.3
# Output: 760425982254.dkr.ecr.us-east-2.amazonaws.com/gco/my-app:v1.2.3
```

#### `gco images build`

Build a container image and push it to the project's ECR repo. Uses
the local container runtime (Docker / Finch / Podman), authenticates
to ECR via `aws ecr get-login-password`, then pushes the resulting
image.

```bash
gco images build CONTEXT [OPTIONS]
```

**Arguments:**

- `CONTEXT` - Build context directory (must contain a Dockerfile)

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--name` | `-n` | Image name (required) |
| `--tag` | `-t` | Image tag (default: git SHA or `latest`) |
| `--dockerfile` | `-f` | Path to Dockerfile within the context |
| `--build-arg` | | Build arg `KEY=VALUE`, repeatable |
| `--platform` | | Target platform (e.g. `linux/amd64`) |
| `--retain / --no-retain` | | Apply `gco:retain=true` |

**Example:**

```bash
gco images build ./my-app --name my-app --tag v1
gco images build ./svc --name svc --build-arg VERSION=1.2.3 --platform linux/amd64
```

#### `gco images push`

Push an already-built local image to the project's ECR repo. Use this
when the image was built outside `gco images build` (CI runner,
multi-stage local pipeline, etc.).

```bash
gco images push NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--tag` | `-t` | Image tag (required) |
| `--local-image` | | Existing local image reference (required) |
| `--retain / --no-retain` | | Apply `gco:retain=true` |

**Example:**

```bash
gco images push my-app -t v1 --local-image my-app:dev
```

#### `gco images delete-tag`

Delete a single tag from a repository. Irreversible — the manifest is
removed, and the underlying image becomes untagged (and eventually
expired by the lifecycle policy).

```bash
gco images delete-tag NAME TAG --yes
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--yes` | `-y` | Required confirmation |

**Example:**

```bash
gco images delete-tag my-app v0.1 -y
```

#### `gco images delete-repo`

Delete a whole repository. Irreversible. Refuses to delete a non-empty
repository unless `--force` is passed.

```bash
gco images delete-repo NAME --yes [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--yes / -y` | Required confirmation |
| `--force / --no-force` | Delete even if non-empty |

**Example:**

```bash
gco images delete-repo old-svc -y
gco images delete-repo orphaned-repo -y --force
```

#### `gco images cleanup`

Remove untagged images across one or all project repos. Useful after
a heavy push cycle has produced many orphaned manifest digests.

```bash
gco images cleanup --yes [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--name` | `-n` | Single repository to clean up |
| `--all` | | Clean up every project repo |
| `--yes` | `-y` | Required confirmation |

**Example:**

```bash
gco images cleanup --all -y
gco images cleanup -n my-app -y
```

#### `gco images prune`

Remove untagged images older than 30 days. Dry-run by default —
nothing is deleted unless you pass `--no-dry-run`.

```bash
gco images prune --yes [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--dry-run / --no-dry-run` | | Default: dry-run; pass `--no-dry-run` to delete |
| `--yes` | `-y` | Required confirmation |

**Example:**

```bash
gco images prune -y                    # dry-run only
gco images prune --no-dry-run -y       # actually delete
```

#### `gco images orphans`

List tags older than `threshold_days` that are not referenced by any
deployed inference endpoint or recent job. Cross-references against
both data sources before declaring a tag an orphan.

```bash
gco images orphans [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--threshold-days` | Only report tags older than this many days (default: 30) |

**Example:**

```bash
gco images orphans
gco images orphans --threshold-days 60
```

#### `gco images lifecycle`

Lifecycle policy management.

```bash
gco images lifecycle COMMAND [OPTIONS]
```

**Subcommands:**

- `get NAME` - Print the lifecycle policy on a repository
- `set NAME --file <json>` - Replace the lifecycle policy from a JSON file

**Example:**

```bash
gco images lifecycle get my-app
gco images lifecycle set my-app --file lifecycle.json
```

#### `gco images mirror`

Mirror third-party images (e.g. the Volcano `docker.io` images) into the project ECR. This is the same multi-arch copy `gco stacks deploy` runs automatically when `volcano_image_mirror.enabled` is set; run it directly to pre-seed a region before enabling the toggle, or to re-mirror after bumping a mirrored image version. Wraps the shared `cli._image_mirror` core (also used by the `images_mirror` MCP tool).

```bash
gco images mirror [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Target AWS region; must match the regional stack (required) |
| `--ecr-namespace` | | Destination ECR namespace (default: cdk.json `volcano_image_mirror.ecr_namespace`) |
| `--no-skip-existing` | | Re-copy images even if the tag already exists in ECR |
| `--dry-run` | | Print the copy plan without creating repos or copying images |

**Example:**

```bash
gco images mirror --region us-east-1
gco images mirror --region us-east-1 --dry-run
gco images mirror --region us-east-1 --ecr-namespace gco/dockerhub
```

#### `gco images replication`

Replication management for the project's ECR registry.

```bash
gco images replication COMMAND
```

**Subcommands:**

- `get` - Print the current ECR replication configuration
- `status` - Print per-image replication status across project repos
- `sync` - Apply the project's standard replication rule (`gco/*` to all deployed regions)

**Example:**

```bash
gco images replication get
gco images replication status
gco images replication sync
```

---

### Files Commands

Manage file systems and download job outputs.

<details>
<summary>All <code>gco files</code> commands (5) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco files list`](#gco-files-list) | List file systems (EFS/FSx) across GCO stacks. |
| [`gco files ls`](#gco-files-ls) | List the contents of EFS/FSx storage. |
| [`gco files download`](#gco-files-download) | Download files from shared storage. |
| [`gco files get`](#gco-files-get) | Get details for the file system in a region (file system ID, lifecycle state, throughput mode, encryption flags, mount targets). |
| [`gco files access-points`](#gco-files-access-points) | List EFS access points for a file system. |

</details>

#### `gco files list`

List file systems (EFS/FSx) across GCO stacks.

```bash
gco files list [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Filter by region |

**Example:**

```bash
gco files list
gco files list -r us-east-1
```

#### `gco files ls`

List the contents of EFS/FSx storage. `REMOTE_PATH` is relative to the storage
root and defaults to `/`.

```bash
gco files ls [REMOTE_PATH] [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | AWS region (required) |
| `--namespace` | `-n` | Kubernetes namespace |
| `--storage-type` | `-t` | Storage type: `efs` or `fsx` (default: `efs`) |

**Example:**

```bash
gco files ls -r us-east-1
gco files ls efs-output-example -r us-east-1
gco files ls -r us-west-2 -t fsx
```

#### `gco files download`

Download files from shared storage.

```bash
gco files download REMOTE_PATH LOCAL_PATH [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Region |
| `--type` | `-t` | Storage type: `efs` or `fsx` |

**Example:**

```bash
gco files download my-job/outputs ./results -r us-east-1
gco files download training-run ./checkpoints -r us-west-2 -t fsx
```

#### `gco files get`

Get details for the file system in a region (file system ID, lifecycle
state, throughput mode, encryption flags, mount targets).

```bash
gco files get REGION [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--type` | `-t` | File system type: `efs` or `fsx` (default: `efs`) |

**Example:**

```bash
gco files get us-east-1
gco files get us-west-2 -t fsx
```

#### `gco files access-points`

List EFS access points for a file system. Useful for inspecting which
namespaces have been provisioned and for confirming the POSIX UID/GID
each access point enforces.

```bash
gco files access-points FILE_SYSTEM_ID [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | AWS region (required) |

**Example:**

```bash
gco files access-points fs-0123456789abcdef0 -r us-east-1
```

---

### Nodepools Commands

Manage Karpenter NodePools.

<details>
<summary>All <code>gco nodepools</code> commands (3) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco nodepools list`](#gco-nodepools-list) | List NodePools in a cluster. |
| [`gco nodepools describe`](#gco-nodepools-describe) | Describe a specific NodePool. |
| [`gco nodepools create-odcr`](#gco-nodepools-create-odcr) | Generate nodepool manifest for ODCR (On-Demand Capacity Reservation). |

</details>

#### `gco nodepools list`

List NodePools in a cluster.

```bash
gco nodepools list [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Region |

#### `gco nodepools describe`

Describe a specific NodePool.

```bash
gco nodepools describe NODEPOOL_NAME [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--region` | `-r` | Region |

#### `gco nodepools create-odcr`

Generate nodepool manifest for ODCR (On-Demand Capacity Reservation).

```bash
gco nodepools create-odcr [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--name` | `-n` | NodePool name (required) |
| `--region` | `-r` | AWS region (required) |
| `--capacity-reservation-id` | `-c` | EC2 Capacity Reservation ID (`cr-xxx`) or ODCR group ARN (required) |
| `--instance-type` | `-i` | Instance type (repeatable) |
| `--output-file` | `-o` | Output manifest to file instead of applying |

**Example:**

```bash
gco nodepools create-odcr \
  --name gpu-reserved \
  --region us-east-1 \
  --capacity-reservation-id cr-0123456789abcdef0 \
  --instance-type p4d.24xlarge \
  --output-file nodepool.yaml
```

---

### Analytics Commands

Manage the optional GCO analytics environment (SageMaker Studio + EMR
Serverless + Cognito). The feature is **off by default**; enable it only
when you want interactive notebook analytics. See the
[Analytics Guide](ANALYTICS.md) for end-to-end workflows.

All `gco analytics *` commands auto-discover the Cognito user-pool ID
and API Gateway endpoint from the `gco-analytics` and `gco-api-gateway`
CloudFormation outputs, so no manual ID wiring is needed.

<details>
<summary>All <code>gco analytics</code> commands (9) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco analytics enable`](#gco-analytics-enable) | Flip `analytics_environment.enabled` to `true` in `cdk.json`. |
| [`gco analytics disable`](#gco-analytics-disable) | Flip `analytics_environment.enabled` to `false` in `cdk.json`. |
| [`gco analytics status`](#gco-analytics-status) | Show the current `analytics_environment.*` toggle state from `cdk.json` plus the deployment state of `gco-analytics`. |
| [`gco analytics users add`](#gco-analytics-users-add) | Create a Cognito user in the analytics user pool. |
| [`gco analytics users list`](#gco-analytics-users-list) | List Cognito users in the analytics user pool. |
| [`gco analytics users remove`](#gco-analytics-users-remove) | Delete a Cognito user from the analytics user pool. |
| [`gco analytics users set-password`](#gco-analytics-users-set-password) | Change a Cognito user's password via ``AdminSetUserPassword``. |
| [`gco analytics studio login`](#gco-analytics-studio-login) | Sign in to SageMaker Studio via Cognito SRP and print a presigned Studio URL on its own line on stdout (pipe-friendly). |
| [`gco analytics doctor`](#gco-analytics-doctor) | Run pre-flight checks before `gco stacks deploy gco-analytics`. |

</details>

#### `gco analytics enable`

Flip `analytics_environment.enabled` to `true` in `cdk.json`. Prints
the follow-up `gco stacks deploy gco-analytics` command — does not
deploy automatically.

```bash
gco analytics enable [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--hyperpod` | | Also set `analytics_environment.hyperpod.enabled=true` (adds HyperPod training-job permissions to the SageMaker execution role). |
| `--canvas` | | Also set `analytics_environment.canvas.enabled=true` (attaches `AmazonSageMakerCanvasFullAccess` to the SageMaker execution role and enables the Canvas app on the Studio domain; artifacts land under `Cluster_Shared_Bucket/analytics-canvas/`). |
| `--yes` | `-y` | Skip the confirmation prompt. |

**Example:**

```bash
gco analytics enable
gco analytics enable --hyperpod
gco analytics enable --canvas
gco analytics enable --hyperpod --canvas -y

# Follow-up to actually deploy the stack:
gco stacks deploy gco-analytics
```

#### `gco analytics disable`

Flip `analytics_environment.enabled` to `false` in `cdk.json`. Leaves
the `hyperpod`, `canvas`, `cognito`, and `efs` sub-blocks untouched so
a later `enable` preserves your preferences. Run `gco stacks destroy
gco-analytics` afterward to tear down the deployed resources.

```bash
gco analytics disable [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--yes` | `-y` | Skip the confirmation prompt. |

**Example:**

```bash
gco analytics disable
gco analytics disable -y
gco stacks destroy gco-analytics
```

#### `gco analytics status`

Show the current `analytics_environment.*` toggle state from `cdk.json`
plus the deployment state of `gco-analytics`.

```bash
gco analytics status
```

**Example:**

```bash
gco analytics status
```

#### `gco analytics users add`

Create a Cognito user in the analytics user pool. Calls
`cognito-idp:AdminCreateUser` and prints the temporary password to
stdout exactly once. Optionally sets a permanent password via
`cognito-idp:AdminSetUserPassword` so the user can sign in without the
`NEW_PASSWORD_REQUIRED` challenge on first login.

```bash
gco analytics users add [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--username` | Cognito username to create (required). |
| `--email` | Email address for the new user. |
| `--no-email` | Suppress the Cognito welcome email (`MessageAction=SUPPRESS`). |
| `--password` | Set a permanent password on the new user (also read from `$GCO_STUDIO_PASSWORD`). Mutually exclusive with `--generate-password`. |
| `--generate-password` | Generate a strong random password, set it permanent, and print it once. Mutually exclusive with `--password`. |

**Example:**

```bash
gco analytics users add --username alice --email alice@example.com
gco analytics users add --username bob --email bob@example.com --no-email

# Set a permanent password so first-time login doesn't hit NEW_PASSWORD_REQUIRED
gco analytics users add --username carol --no-email --generate-password
GCO_STUDIO_PASSWORD='StrongP@ssw0rd!' gco analytics users add --username dave --no-email --password "$GCO_STUDIO_PASSWORD"
```

#### `gco analytics users list`

List Cognito users in the analytics user pool. Default output is a
formatted table via the existing `OutputFormatter`.

```bash
gco analytics users list [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--as-json` | Emit JSON instead of a table. |

**Example:**

```bash
gco analytics users list
gco analytics users list --as-json
```

#### `gco analytics users remove`

Delete a Cognito user from the analytics user pool. Does not delete
the user's Studio user profile or EFS home folder — use
`aws sagemaker delete-user-profile` for that.

```bash
gco analytics users remove [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--username` | Cognito username to remove (required). |
| `--yes` | Skip the confirmation prompt. |

**Example:**

```bash
gco analytics users remove --username alice
gco analytics users remove --username alice --yes
```

#### `gco analytics users set-password`

Change a Cognito user's password via ``AdminSetUserPassword``. By
default the new password is marked permanent so the user can sign in
directly with ``gco analytics studio login`` without hitting the
``NEW_PASSWORD_REQUIRED`` challenge. Pass ``--temporary`` to require
the user to choose their own password on first sign-in.

```bash
gco analytics users set-password [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--username` | Cognito username whose password to change (required). |
| `--password` | New password (also read from `$GCO_STUDIO_PASSWORD`; prompted otherwise). Mutually exclusive with `--generate-password`. |
| `--generate-password` | Generate a strong random password, set it, and print it once. Mutually exclusive with `--password`. |
| `--temporary` | Set the password as temporary (`Permanent=false`). Default is permanent. |
| `--yes`, `-y` | Skip the confirmation prompt. |

**Examples:**

```bash
# Interactive — prompts twice for the new password
gco analytics users set-password --username alice

# Non-interactive via env var (won't leak into shell history)
GCO_STUDIO_PASSWORD='StrongP@ssw0rd!' \
  gco analytics users set-password --username alice --yes

# Generate and print a new password
gco analytics users set-password --username alice --generate-password --yes

# Force the user to reset on next login
gco analytics users set-password --username alice \
  --password 'Temp!Reset123$' --temporary --yes
```

#### `gco analytics studio login`

Sign in to SageMaker Studio via Cognito SRP and print a presigned
Studio URL on its own line on stdout (pipe-friendly). The password,
`IdToken`, and URL are never written to disk.

```bash
gco analytics studio login [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--username` | Cognito username (required). |
| `--password` | Password. Defaults to prompt (`click.prompt(..., hide_input=True)`). Also read from `$GCO_STUDIO_PASSWORD` if set. |
| `--api-url` | Override the API Gateway base URL (otherwise auto-discovered from CloudFormation). |
| `--open` | Launch the default browser on the presigned URL after printing it. |

**Example:**

```bash
# Interactive (prompts for password)
gco analytics studio login --username alice

# Non-interactive
export GCO_STUDIO_PASSWORD='...'
gco analytics studio login --username alice

# Open browser automatically
gco analytics studio login --username alice --open

# Custom API endpoint
gco analytics studio login \
  --username alice \
  --api-url https://abc123.execute-api.us-east-2.amazonaws.com
```

#### `gco analytics doctor`

Run pre-flight checks before `gco stacks deploy gco-analytics`. Each
check prints `✓`/`✗` plus a short remediation line. Exits `1` on any
failing check.

Checks performed:

- `cdk.json` is present and parses as JSON
- `gco-global`, `gco-api-gateway`, and every regional stack are
  `CREATE_COMPLETE`
- The three `/gco/cluster-shared-bucket/*` SSM parameters are
  present in the global region
- No orphaned retained analytics resources are left from a previous
  `retain`-policy destroy

```bash
gco analytics doctor
```

**Example:**

```bash
gco analytics doctor
```

---

### Config-Cmd Commands

Manage the local CLI configuration file at `~/.gco/config.yaml`. The
config file lets you set per-machine defaults (default region, output
format, verbose flag) so you don't have to repeat them on every
invocation.

<details>
<summary>All <code>gco config-cmd</code> commands (2) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco config-cmd init`](#gco-config-cmd-init) | Initialize the config file with a starter template. |
| [`gco config-cmd show`](#gco-config-cmd-show) | Print the current resolved configuration (file values plus any environment-variable overrides). |

</details>

#### `gco config-cmd init`

Initialize the config file with a starter template.

```bash
gco config-cmd init [OPTIONS]
```

**Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--force` | `-f` | Overwrite an existing config file |

**Example:**

```bash
gco config-cmd init
gco config-cmd init -f          # overwrite existing
```

#### `gco config-cmd show`

Print the current resolved configuration (file values plus any
environment-variable overrides).

```bash
gco config-cmd show
```

---

### Tasks Commands

Inspect long-running MCP tool invocations through their disk-backed
status surface. Every long-running tool (`deploy_all`, `destroy_all`,
`bootstrap_cdk`, `deploy_stack`, `destroy_stack`, `images_build`,
`images_push`) writes a JSON status file plus a raw output log under
`~/.gco/tasks/{task_id}.{json,log}` on every line, so an operator
with a terminal can see real-time progress even when the MCP client
drops or buries notifications.

The directory is configurable via `GCO_TASK_STATUS_DIR`. Set
`GCO_DISABLE_TASK_STATUS=1` to skip disk emission for sandboxed
environments where `~/.gco` isn't writable.

<details>
<summary>All <code>gco tasks</code> commands (4) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco tasks list`](#gco-tasks-list) | List recent tasks, newest first. |
| [`gco tasks show TASK_ID`](#gco-tasks-show-task_id) | Print the full JSON status record for a single task. |
| [`gco tasks tail TASK_ID`](#gco-tasks-tail-task_id) | Print the last N lines of a task's raw output log. |
| [`gco tasks prune`](#gco-tasks-prune) | Remove all but the most-recent N task files. |

</details>

#### `gco tasks list`

List recent tasks, newest first. Reports the state, elapsed time,
stack count, last stack name, and task ID for each. Tasks whose
recorded PID is no longer alive but whose state still says `running`
are reported as `orphaned` so callers see honest data even when the
original MCP wrapper exited unexpectedly.

```bash
gco tasks list [OPTIONS]
```

**Options:**

- `-n, --limit INTEGER` — Maximum tasks to show (default: 20)
- `--json` — Emit raw JSON instead of a table

```bash
gco tasks list
gco tasks list -n 5
gco tasks list --json | jq '.tasks[] | select(.state == "orphaned")'
```

#### `gco tasks show TASK_ID`

Print the full JSON status record for a single task. Includes the
original argv, the last 20 output lines, the recorded PID, and the
path to the full log file. Useful when `gco tasks list` shows a
state worth digging into (failed/orphaned/cancelled).

```bash
gco tasks show deploy_all-1747683123-42
```

#### `gco tasks tail TASK_ID`

Print the last N lines of a task's raw output log. Each line is
prefixed with `[stdout]` or `[stderr]` so streams stay distinguishable
in the interleaved order the subprocess produced them. The `--follow`
mode polls the log file like `tail -f`.

```bash
gco tasks tail TASK_ID [OPTIONS]
```

**Options:**

- `-n, --lines INTEGER` — Lines to show from the end of the log (default: 100)
- `-f, --follow` — Stream new lines as they're written
- `--interval FLOAT` — Polling interval in seconds when `--follow` is set (default: 1.0)

```bash
gco tasks tail deploy_all-1747683123-42
gco tasks tail deploy_all-1747683123-42 -n 500
gco tasks tail deploy_all-1747683123-42 -f
```

#### `gco tasks prune`

Remove all but the most-recent N task files. Pruning happens
automatically when new tasks start, but a manual sweep is sometimes
useful (e.g. after a flurry of failed retries cluttered the directory).

```bash
gco tasks prune [OPTIONS]
```

**Options:**

- `--keep INTEGER` — Number of most-recent tasks to keep (default: 50)

```bash
gco tasks prune --keep 10
```

---

### Mission Commands

Drive a goal-directed iteration loop with machine-checkable success
criteria and an optional advisory LLM. Mission is gated by the
`GCO_ENABLE_MISSION=true` environment variable (or the umbrella
`GCO_ENABLE_ALL_TOOLS=true`); without the flag every subcommand exits
2 with a one-line hint.

The full design lives in [Mission Guide](MISSION.md). This section
covers the CLI surface only.

Sessions persist as JSON under `~/.gco/missions/` (the filesystem
backend) or in the `<project>-missions` DynamoDB table when
`GCO_MISSION_STATE_BACKEND=dynamodb`. The sampling backend resolves
in this order:

1. Explicit `--use-sampling` / `--no-sampling` flag.
2. MCP host capability (only when running inside an MCP host).
3. Bedrock credential probe — when `boto3` resolves credentials, the
   Bedrock backend is selected with the model id from
   `GCO_MISSION_BEDROCK_MODEL_ID` (or the default).
4. Otherwise sampling is off and the loop runs deterministically.

<details>
<summary>All <code>gco mission</code> commands (11) — click to expand</summary>

| Command | Description |
| --- | --- |
| [`gco mission run`](#gco-mission-run) | Chained shorthand that scaffolds criteria from a directive, persists a new session, and drives it through iterations until a terminal verdict — three steps in one call. |
| [`gco mission start`](#gco-mission-start) | Validate inputs, resolve sampling, persist a new session. |
| [`gco mission scaffold-criteria`](#gco-mission-scaffold-criteria) | Draft a criteria file from a natural-language directive. |
| [`gco mission iterate SESSION_ID`](#gco-mission-iterate-session_id) | Drive one or more iterations of an existing session. |
| [`gco mission status SESSION_ID`](#gco-mission-status-session_id) | Print the full state of a session. |
| [`gco mission checkpoint SESSION_ID`](#gco-mission-checkpoint-session_id) | Re-run the verdict cascade on the latest iteration without producing a new one. |
| [`gco mission complete SESSION_ID`](#gco-mission-complete-session_id) | Force a session into `completed` and stamp the final verdict. |
| [`gco mission abort SESSION_ID`](#gco-mission-abort-session_id) | Pause or terminate a session. |
| [`gco mission resume SESSION_ID`](#gco-mission-resume-session_id) | Transition a paused session back to `running`. |
| [`gco mission history SESSION_ID`](#gco-mission-history-session_id) | Get the iteration history of a session. |
| [`gco mission list`](#gco-mission-list) | List Mission sessions across the configured backend. |

</details>

#### `gco mission run`

Chained shorthand that scaffolds criteria from a directive, persists
a new session, and drives it through iterations until a terminal
verdict — three steps in one call. The most common operator workflow
when you don't need a saved criteria file or per-step inspection.

```bash
gco mission run [OPTIONS]
```

**Options:**

- `--directive TEXT` — Required. Natural-language goal description.
- `--tool-allowlist NAME` — Required, repeatable. Tools the loop may invoke. The first allowlisted tool also seeds the deterministic strategy when sampling is off.
- `--max-iterations INTEGER` — Iteration cap (default: 5). Pass `-1` to opt out.
- `--max-wall-clock INTEGER` — Wall-clock cap in seconds (default: 300). Pass `-1` to opt out.
- `--max-criteria INTEGER` — Cap on the number of criterion entries the scaffolder emits (default: 5).
- `--retries INTEGER` — Sampling-path retry budget on validator rejection (default: 3). After exhaustion, falls back to deterministic templates.
- `--use-sampling` / `--no-sampling` — Force the sampling path on/off for both the scaffolder and the loop's Strategy_Revision sampler. Default auto-detects.
- `--bedrock-model-id MODEL_ID` — Override the Bedrock model id (CLI sampling backend only).
- `--allow-scripted-strategies` — Permit scripted strategies (the AST-validated Python sandbox).
- `--save-criteria PATH` — Also write the scaffolded criteria JSON to `PATH` for inspection or reuse.
- `--cadence` — Checkpoint cadence kind (default: `every_iteration`).
- `--stagnation-threshold INTEGER` — Iterations of no progress before terminate (default: 3).

A scaffold-summary JSON line lands on stderr before iteration starts; per-iteration verdicts stream to stderr as JSON lines; the Final_Report lands on stdout when a terminal verdict fires.

```bash
# No-AWS smoke test against in-memory tools.
export GCO_ENABLE_MISSION=true
gco mission run \
  --directive "Find documentation about inference endpoints." \
  --tool-allowlist find_examples --tool-allowlist find_docs \
  --max-iterations 1 --max-wall-clock 30 \
  --no-sampling

# Bedrock-backed run with a captured criteria file.
gco mission run \
  --directive "Drive validation loss below 0.1." \
  --tool-allowlist find_examples \
  --max-iterations 10 --max-wall-clock 1800 \
  --save-criteria /tmp/criteria.json
```

#### `gco mission start`

Validate inputs, resolve sampling, persist a new session. Use this
when you want to inspect or modify the criteria file by hand, or when
you want to drive iterations one at a time.

```bash
gco mission start [OPTIONS]
```

**Options:**

- `--directive TEXT` — Required. Natural-language goal description.
- `--criteria-file PATH` — Required (unless `--with-defaults` is set). JSON file containing the criteria list.
- `--with-defaults` — Use a basic placeholder predicate criterion when no `--criteria-file` is provided.
- `--max-iterations INTEGER` — Required. Hard cap on iterations. Pass `-1` to opt out (uncapped).
- `--max-wall-clock INTEGER` — Required. Hard cap on wall-clock seconds. Pass `-1` to opt out (uncapped).
- `--tool-allowlist NAME` — Required, repeatable. Tools the loop may invoke.
- `--cadence` — Checkpoint cadence kind (default: `every_iteration`).
- `--cadence-n INTEGER` — `n` parameter for `every_n_iterations`.
- `--cadence-t INTEGER` — `t` parameter (seconds) for `every_t_seconds`.
- `--cadence-event TEXT` — `event_name` parameter for `on_event`.
- `--stagnation-threshold INTEGER` — Iterations of no progress before terminate (default: 3).
- `--use-sampling` / `--no-sampling` — Force sampling on/off (default: auto-detect).
- `--bedrock-model-id MODEL_ID` — Override the Bedrock model id.
- `--allow-scripted-strategies` — Permit scripted strategies.
- `--run` — Iterate to completion synchronously after creating the session.
- `--output table|json` — Output format (default: `json`).

```bash
gco mission start \
  --directive "Drive validation loss below 0.1." \
  --criteria-file /tmp/criteria.json \
  --max-iterations 10 --max-wall-clock 3600 \
  --tool-allowlist find_examples
```

#### `gco mission scaffold-criteria`

Draft a criteria file from a natural-language directive. Output is
always validated through `validate_criteria` so the resulting file
is immediately usable with `gco mission start --criteria-file` or
`gco mission run --save-criteria` for inspection.

```bash
gco mission scaffold-criteria [OPTIONS]
```

**Options:**

- `--directive TEXT` — Required. Natural-language goal description.
- `--allowlist NAME` — Optional, repeatable. Tools that will be allowlisted on the resulting session. When the directive is search-flavoured *and* an allowlist is supplied, the deterministic generator emits one `tool_call_succeeded` criterion per allowlisted tool — server-evaluated, never goes through the predicate AST sandbox.
- `--use-sampling` / `--no-sampling` — Force sampling on/off (default: auto-detect).
- `--bedrock-model-id MODEL_ID` — Override the Bedrock model id.
- `--max-criteria INTEGER` — Cap on the number of criterion entries (default: 5).
- `--retries INTEGER` — Sampling-path retry budget (default: 3).
- `--output-file PATH` — Write the JSON to this file instead of stdout.
- `--output table|json` — Output format (default: `json`).

```bash
gco mission scaffold-criteria \
  --directive "Find documentation about inference endpoints." \
  --allowlist find_examples --allowlist find_docs \
  --output-file criteria.json
```

#### `gco mission iterate SESSION_ID`

Drive one or more iterations of an existing session. Stops early on a
terminal verdict. The CLI uses a stub tool dispatcher; real tool
execution requires the MCP server.

```bash
gco mission iterate SESSION_ID [OPTIONS]
```

**Options:**

- `--max-iterations INTEGER` — How many iterations to run in this call (default: 1). Distinct from the session-wide budget cap.
- `--output table|json` — Output format (default: `json`).

```bash
gco mission iterate mission-abc123 --max-iterations 3
```

#### `gco mission status SESSION_ID`

Print the full state of a session.

```bash
gco mission status SESSION_ID [--output table|json]
```

```bash
gco mission status mission-abc123 --output table
```

#### `gco mission checkpoint SESSION_ID`

Re-run the verdict cascade on the latest iteration without producing
a new one. Useful for inspecting how a paused or stalled session
would terminate if the cascade fired now.

```bash
gco mission checkpoint SESSION_ID [--output table|json]
```

#### `gco mission complete SESSION_ID`

Force a session into `completed` and stamp the final verdict.

```bash
gco mission complete SESSION_ID [--output table|json]
```

#### `gco mission abort SESSION_ID`

Pause or terminate a session. With `--pause` the session can be
resumed later; without `--pause` the session is terminated and the
final verdict is stamped.

```bash
gco mission abort SESSION_ID [--pause] [--output table|json]
```

```bash
gco mission abort mission-abc123 --pause
```

#### `gco mission resume SESSION_ID`

Transition a paused session back to `running`.

```bash
gco mission resume SESSION_ID [--output table|json]
```

#### `gco mission history SESSION_ID`

Get the iteration history of a session.

```bash
gco mission history SESSION_ID [OPTIONS]
```

**Options:**

- `--format full|summary` — Iteration history detail level (default: `summary`).
- `--output table|json` — Output format (default: `json`).

```bash
gco mission history mission-abc123 --format full
```

#### `gco mission list`

List Mission sessions across the configured backend.

```bash
gco mission list [OPTIONS]
```

**Options:**

- `--status STATUS` — Filter sessions by status (`pending`, `running`, `paused`, ...).
- `--output table|json` — Output format (default: `json`).

```bash
gco mission list --status running --output table
```

---

## Configuration

### Config File

Create `~/.gco/config.yaml`:

```yaml
default_region: us-east-1
output_format: table
verbose: false
regions:
  - us-east-1
  - us-west-2
  - eu-west-1
```

### cdk.json

Project configuration in `cdk.json`:

```json
{
  "context": {
    "project_name": "gco",
    "deployment_regions": {
      "global": "us-east-2",
      "api_gateway": "us-east-2",
      "monitoring": "us-east-2",
      "regional": ["us-east-1", "us-west-2"]
    },
    "resource_thresholds": {
      "cpu_threshold": 60,
      "memory_threshold": 60,
      "gpu_threshold": -1,
      "pending_pods_threshold": 10,
      "pending_requested_cpu_vcpus": 100,
      "pending_requested_memory_gb": 200,
      "pending_requested_gpus": -1
    },
    "fsx_lustre": {
      "enabled": false,
      "storage_capacity_gib": 1200
    }
  }
}
```

Set any threshold to `-1` to disable that health check. This is useful when running GPU inference endpoints that naturally saturate GPU resources.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `AWS_REGION` | Default AWS region |
| `AWS_PROFILE` | AWS credentials profile |
| `GCO_CONFIG` | Path to config file |
| `GCO_REGIONAL_API` | Use regional API endpoints (`true`/`false`) |
| `CDK_DOCKER` | Docker command (`docker` or `finch`) |
| `GCO_ENABLE_MISSION` | Gate the `gco mission` subcommand group (`true`/`false`). With the flag unset, every subcommand exits 2 with a hint. |
| `GCO_ENABLE_ALL_TOOLS` | Umbrella flag that satisfies every per-tool gate including `GCO_ENABLE_MISSION`. |
| `GCO_MISSION_STATE_BACKEND` | Persistence backend for sessions (`filesystem` or `dynamodb`). Unrecognised values fall back to filesystem with a one-line warning. |
| `GCO_MISSION_BEDROCK_MODEL_ID` | Override the default Bedrock model id (Amazon Nova Pro, `us.amazon.nova-pro-v1:0`) used by the CLI sampling backend. See [Customization → Bedrock Model Selection](CUSTOMIZATION.md#bedrock-model-selection). |
| `GCO_MISSION_BEDROCK_REGION` | Override the default Bedrock region (`us-east-1`). |

## Examples

### Complete Workflow

```bash
# 1. Deploy (bootstrap runs automatically if needed)
export CDK_DOCKER=finch
gco stacks deploy-all -y

# 2. Check capacity
gco capacity status
gco capacity recommend-region --gpu

# 3. Submit jobs
gco jobs submit-sqs examples/simple-job.yaml --region us-east-1
gco jobs queue-status --all-regions

# 4. Monitor jobs
gco jobs list --all-regions
gco jobs logs my-job -r us-east-1 -n gco-jobs

# 5. Download outputs
gco files ls -r us-east-1
gco files download my-job/outputs ./results -r us-east-1

# 6. Cleanup
gco stacks destroy-all -y
```

### Inference Endpoint Workflow

```bash
# 1. Upload model weights
gco models upload ./llama3-weights/ --name llama3-8b

# 2. Deploy inference endpoint
gco inference deploy my-llm \
  -i vllm/vllm-openai:v0.22.0 \
  --gpu-count 1 \
  --model-source $(gco models uri llama3-8b) \
  -e MODEL=/models/my-llm \
  -r us-east-1

# 3. Monitor deployment
gco inference status my-llm

# 4. Scale for production
gco inference scale my-llm --replicas 3

# Or enable autoscaling
gco inference deploy my-llm \
  -i vllm/vllm-openai:v0.22.0 \
  --replicas 2 --gpu-count 1 \
  --min-replicas 1 --max-replicas 8 \
  --autoscale-metric cpu:70

# 5. Rolling update
gco inference update-image my-llm -i vllm/vllm-openai:v0.22.0

# 6. Cleanup
gco inference delete my-llm -y
gco models delete llama3-8b -y
```

### GPU Job Submission

```bash
# Check GPU capacity
gco capacity check -i g5.xlarge -r us-east-1

# Submit GPU job
gco jobs submit-sqs examples/gpu-job.yaml --auto-region

# Monitor
gco jobs list --all-regions
gco jobs logs gpu-test-job -r us-east-1 -n gco-jobs
```

### Multi-Region Deployment

```bash
# Deploy to multiple regions
gco stacks deploy-all -y --parallel --max-workers 4

# Check status across regions
gco stacks list
gco capacity status
```

## Troubleshooting

### Common Issues

**"No credentials found"**

```bash
# Ensure AWS credentials are configured
aws sts get-caller-identity
```

**"Endpoint request timed out"**

- Wait 1-2 minutes after deployment for ALB targets to become healthy
- Use `submit-sqs` or `submit-direct` instead of `submit`

**"kubectl access denied"**

- Add your IAM principal to EKS access entries:

```bash
aws eks create-access-entry \
  --cluster-name gco-us-east-1 \
  --principal-arn arn:aws:iam::ACCOUNT:user/YOUR-USER \
  --region us-east-1

aws eks associate-access-policy \
  --cluster-name gco-us-east-1 \
  --principal-arn arn:aws:iam::ACCOUNT:user/YOUR-USER \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
  --access-scope type=cluster \
  --region us-east-1
```

**"CDK bootstrap required"**

This should resolve automatically — `deploy` and `deploy-all` auto-bootstrap un-bootstrapped regions. If it persists:

```bash
gco stacks bootstrap --region us-east-1
```

### Debug Mode

```bash
# Enable verbose output
gco -v jobs list --all-regions

# Check AWS configuration
aws sts get-caller-identity
aws eks list-clusters --region us-east-1
```

---

For more help, see:

- [Troubleshooting Guide](TROUBLESHOOTING.md)
- [Inference Guide](INFERENCE.md)
- [Architecture Documentation](ARCHITECTURE.md)
- [Customization Guide](CUSTOMIZATION.md)
