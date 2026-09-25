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
| `autopilot.py` | Autopilot launch-plan logic: per-engine Bedrock model resolution, session MCP config generation (Claude Code JSON, Codex TOML, OpenCode JSON), pinned [Claude Code](https://code.claude.com/docs/en/overview) / Codex / OpenCode installs ([docs](../docs/AUTOPILOT.md)) |
| `aws_client.py` | AWS SDK client wrapper with region discovery and credential handling |
| `config.py` | CLI configuration loader (cdk.json, env vars, user config) |
| `output.py` | Output formatting (table, JSON, YAML) |
| `jobs.py` | Job submission, listing, logs, and lifecycle management |
| `inference.py` | Inference endpoint deployment, scaling, canary, and invocation |
| `models.py` | Model weight upload, listing, and [S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html) URI management |
| `storage.py` | Human-friendly GCO S3 bucket discovery and incremental download/upload sync |
| `stacks.py` | [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) stack deployment, destruction, and status |
| `status.py` | Fleet-wide status document assembly: independent section gathers, findings, degradation model |
| `costs.py` | Cost tracking via AWS [Cost Explorer](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html) |
| `dag.py` | DAG pipeline execution with dependency ordering |
| `files.py` | EFS/FSx file listing and download |
| `nodepools.py` | Nodepool inspection and management |
| `kubectl_helpers.py` | kubectl command wrappers for direct cluster access |
| `images.py` | Container image registry management (`gco images`): repositories, tags, lifecycle, replication |
| `job_policy.py` | Reads the deployed job-validation policy (`GET /api/v1/policy`) and judges manifests against it |
| `managed_config.py` | Managed deployment-config engine: validated, atomic, audited `cdk.json` edits behind `gco stacks regions/bedrock/eks` |
| `upgrade.py` | Whole-deployment upgrade engine behind `gco upgrade`: release-tag resolution, `cdk.json`-preserving checkout, local install and dev-image refresh, and the scale-to-zero / deploy-all stack cycle |
| `cost_analytics.py` | Athena-backed Kubernetes cost analytics (`gco costs k8s ...`) |
| `vector_store.py` | Operator client for the vector store (`gco vector`) |
| `cluster_tunnel.py` | Shared helpers for reaching a possibly-private EKS API endpoint (`gco cluster tunnel`) |
| `cluster_doctor.py` | Diagnosis of the three layers of cluster access: reachability, authentication, authorization (`gco cluster doctor`) |
| `eks_capabilities.py` | Configured-vs-live view of the [EKS Capabilities](../docs/EKS_CAPABILITIES.md) (AWS-managed ACK and kro) on a regional cluster: cdk.json intent merged with `ListCapabilities`/`DescribeCapability`, drift sentences, unmanaged capabilities (`gco stacks capabilities status`) |
| `gitops.py` | The self-managed [Argo CD](../docs/GITOPS.md) behind `gco gitops`: the status document (validated `helm.argocd`, chart pin, fence, per-Region GitOps paths, repo-server scaling), the generated admin password, and the API login + session-cookie screenshot of the Applications view |
| `crossplane.py` | The self-managed [Crossplane](../docs/CROSSPLANE.md) behind `gco crossplane`: the status document (toggle, chart pins, shipped composition functions) and the Crossview dashboard screenshot |
| `cluster_ui.py` | Shared plumbing for the in-cluster web UIs reached through kubectl: cdk.json and charts.yaml reads, Secret-key reads, background port-forwards, headless Playwright captures, the shared tunnel options |
| `ssm_tunnel.py` | SSM Session Manager tunnel helpers for private EKS endpoints |
| `ephemeral_bastion.py` | Ephemeral SSM bastion lifecycle (`--via-ssm auto`) |
| `analytics_user_mgmt.py` | Cognito user management and Studio login for the analytics environment |
| `monitoring_user_mgmt.py` | Grafana user management over the admin HTTP API (`gco monitoring users`) |
| `_container_runtime.py` | Container runtime detection (Docker, Finch, Podman) shared by image builds and mirroring |
| `_image_mirror.py` | Shared core that mirrors third-party images into the project ECR (`gco images mirror`, deploy-time auto-mirror, MCP tools) |
| `_image_reference.py` | Linear-time validation of immutable container image references |
| `_image_uri.py` | ECR image URI helpers backed by local AWS partition metadata |

### commands/

Click command definitions that wire CLI flags to the business logic above.

| File | Commands |
|------|----------|
| `analytics_cmd.py` | `gco analytics ...` |
| `autopilot_cmd.py` | `gco autopilot` |
| `capacity_cmd.py` | `gco capacity ...` |
| `cluster_cmd.py` | `gco cluster ...` |
| `config_cmd.py` | `gco config-cmd init`, `show`, `get` |
| `costs_cmd.py` | `gco costs ...` |
| `crossplane_cmd.py` | `gco crossplane status`, `open`, `screenshot` |
| `dag_cmd.py` | `gco dag ...` |
| `deps_cmd.py` | `gco deps scan` |
| `examples_cmd.py` | `gco examples ...` |
| `files_cmd.py` | `gco files ...` |
| `gitops_cmd.py` | `gco gitops status`, `open`, `password`, `screenshot` |
| `images_cmd.py` | `gco images ...` |
| `inference_cmd.py` | `gco inference ...` |
| `jobs_cmd.py` | `gco jobs ...` |
| `mission_cmd.py` | `gco mission ...` |
| `models_cmd.py` | `gco models ...` |
| `monitoring_cmd.py` | `gco monitoring ...` |
| `nodepools_cmd.py` | `gco nodepools ...` |
| `queue_cmd.py` | `gco queue ...` |
| `release_cmd.py` | `gco release ...` |
| `stacks_cmd.py` | `gco stacks ...` |
| `status_cmd.py` | `gco status` |
| `storage_cmd.py` | `gco storage ...` |
| `swarm_cmd.py` | `gco swarm ...` |
| `tasks_cmd.py` | `gco tasks ...` |
| `templates_cmd.py` | `gco templates ...` |
| `upgrade_cmd.py` | `gco upgrade` |
| `vector_cmd.py` | `gco vector ...` |
| `webhooks_cmd.py` | `gco webhooks ...` |

### capacity/

GPU capacity checking, region recommendation, and AI-powered advisory.

| File | Description |
|------|-------------|
| `checker.py` | Spot placement scores, pricing, and availability checks |
| `advisor.py` | AI-powered capacity recommendations via Amazon [Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html) |
| `blocks.py` | Capacity Block search primitives: duration math, instance-type normalization, offering pricing, de-dup and sort helpers |
| `history.py` | DynamoDB-backed time-series store behind `gco capacity history` |
| `models.py` | Data models for capacity responses |
| `multi_region.py` | Cross-region capacity aggregation and comparison |
| `traffic_dial.py` | Manual Global Accelerator traffic-dial controls (`gco capacity traffic-dial show`, `set`, `clear`) |

## Installation

```bash
pip install -e .        # Development (editable)
pipx install -e .       # CLI-only usage
```

## Reference

See [CLI Reference](../docs/CLI.md) for the full command documentation.

## Control-Flow Diagrams

Auto-generated flowcharts for the most branchy CLI entry points (job
submission, inference deploys, orchestrated stack deploy/destroy, image
build/push/mirror, Studio login) live under `diagrams/code_diagrams/cli/`.
The generated [flowchart index](../diagrams/code_diagrams/README.md#cli) is
the complete, always-current list; this README deliberately does not
repeat it.

Regenerate through the
[canonical two-commit diagram workflow](../diagrams/README.md#quick-reference)
after editing any charted control flow.
