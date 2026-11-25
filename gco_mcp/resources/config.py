"""Current configuration resources for the GCO MCP server."""

from cli_runner import PROJECT_ROOT  # runtime-resolved checkout root (uvx-safe)
from feature_flags import ALL_FLAGS, FLAG_ALL_TOOLS
from server import mcp

_FLAG_DESCRIPTIONS = {
    "GCO_ENABLE_CAPACITY_PURCHASE": "Enable cost-incurring capacity reservation tools.",
    "GCO_ENABLE_MODEL_UPLOAD": "Enable local model-data and KV-cache uploads to central or regional S3.",
    "GCO_ENABLE_IMAGE_PUBLISH": "Enable image build, push, and mirror operations.",
    "GCO_ENABLE_INFRASTRUCTURE_DEPLOY": "Enable infrastructure deployment tools.",
    "GCO_ENABLE_INFRASTRUCTURE_DESTROY": "Enable infrastructure destroy tools.",
    "GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "Enable irreversible deletion/cancellation tools.",
    "GCO_ENABLE_MISSION": "Enable autonomous Mission tools.",
    "GCO_ENABLE_LOCAL_METRICS": "Enable reads beneath GCO_METRICS_LOCAL_ROOT.",
    "GCO_ENABLE_LOCAL_STORAGE_SYNC": "Enable local storage sync operations.",
    "GCO_ENABLE_SEMANTIC_PROGRESS": "Enable LLM-based semantic progress scoring.",
    "GCO_ENABLE_RUNAI_CAPACITY": "Enable the read-only NVIDIA Run:ai capacity reader (calls a non-AWS control plane).",
}


@mcp.resource("config://gco/index")
def config_index() -> str:
    """List authoritative CDK and MCP configuration resources."""
    return "\n".join(
        [
            "# GCO Configuration\n",
            "## Authoritative Configuration",
            "- `config://gco/cdk.json` — Current CDK deployment configuration",
            "- `mcp://gco/feature-flags` — Live MCP feature flags and gated tools",
            "- `config://gco/env-vars` — MCP, CLI, and service environment variables\n",
            "## Related",
            "- `source://gco/config/pyproject.toml` — Python project metadata",
            "- `source://gco/config/app.py` — CDK app entry point",
            "- `docs://gco/docs/CUSTOMIZATION` — Full customization guide",
        ]
    )


@mcp.resource("config://gco/cdk.json")
def cdk_json_resource() -> str:
    """Read the authoritative CDK deployment configuration without projection."""
    path = PROJECT_ROOT / "cdk.json"
    if not path.is_file():
        return "cdk.json not found."
    return path.read_text()


@mcp.resource("config://gco/env-vars")
def env_vars_resource() -> str:
    """List the MCP feature flags and the environment variables the code reads.

    Every variable here is read by the MCP server, the ``gco`` CLI or the
    in-cluster services; keep the table in step with the code when adding or
    removing one.
    """
    lines = [
        "# GCO Environment Variables\n",
        "## MCP Server\n",
        "| Variable | Default | Description |",
        "|----------|---------|-------------|",
        "| `GCO_PROJECT_ROOT` | (checkout, else nearest `cdk.json` above the working directory) | Project every `gco` child runs from; in-process tools and resources read its `cdk.json` too. |",
        "| `GCO_MCP_AUDIT_LOG` | (stderr) | File the audit records (one JSON line per tool call, resource read, startup and role assumption) are appended to; created with mode 0600. |",
        "| `GCO_MCP_AUDIT_LOG_LEVEL` | `INFO` | Lowest level written to the audit output; unknown names keep INFO. |",
        "| `GCO_MCP_ROLE_ARN` | (unset) | IAM role ARN assumed when the server starts; applies to the default boto3 session and to every `gco`/kubectl child (container credentials endpoint on 127.0.0.1), and refreshes before it expires. |",
        "| `GCO_MCP_ROLE_SESSION_NAME` | `gco-mcp-server` | Assumed-role session name. |",
        "| `GCO_MCP_ROLE_DURATION_SECONDS` | `3600` | Lifetime of each role session; the role is assumed again before it ends. |",
        "| `GCO_MCP_TOOL_SEARCH` | `bm25` | Tool catalog mode: bm25, regex, code_mode, or off. |",
        "| `GCO_MCP_CODE_MODE_MAX_DURATION_SECS` | `30` | Code Mode execution time limit. |",
        "| `GCO_MCP_CODE_MODE_MAX_MEMORY` | `200000000` | Code Mode memory limit in bytes. |",
        f"| `{FLAG_ALL_TOOLS}` | `false` | Enable every per-tool feature gate. |",
    ]
    for flag in ALL_FLAGS:
        description = _FLAG_DESCRIPTIONS.get(flag, "Enable the corresponding gated MCP tools.")
        lines.append(f"| `{flag}` | `false` | {description} |")
    lines.extend(
        [
            "| `GCO_STORAGE_LOCAL_ROOT` | (unset) | Required root for model uploads and storage sync; relative paths resolve beneath it and short uploads use a private same-filesystem snapshot. |",
            "| `GCO_METRICS_LOCAL_ROOT` | (unset) | Required root for local metric-file reads. |",
            "| `GCO_TASK_STATUS_DIR` | `~/.gco/tasks` | Private bounded task status/log directory. |",
            "| `GCO_DISABLE_TASK_STATUS` | `false` | Disable disk-backed task status emission. |",
            "| `FASTMCP_DOCKET_URL` | `memory://` | FastMCP background-task store. |",
            "\n## Mission\n",
            "| Variable | Default | Description |",
            "|----------|---------|-------------|",
            "| `GCO_MISSION_STATE_BACKEND` | `filesystem` | `filesystem` (`~/.gco/missions/`) or `dynamodb` (the `<project>-missions` table in the global Region). |",
            "| `GCO_MISSION_BEDROCK_MODEL_ID` | (cdk.json Mission default) | Bedrock model for the CLI sampling path. |",
            "| `GCO_MISSION_BEDROCK_REGION` | `us-east-1` | Region for those Bedrock calls. |",
            "\n## CLI (and the MCP server's `gco` children)\n",
            "| Variable | Default | Description |",
            "|----------|---------|-------------|",
            "| `GCO_PROJECT_NAME` | cdk.json `project_name`, else `gco` | Project the stacks, clusters, tables and ECR repositories are named after. |",
            "| `GCO_GLOBAL_REGION` | cdk.json `deployment_regions.global` | Region of the global stack. |",
            "| `GCO_DEFAULT_REGION` | first cdk.json regional Region | Region for commands that take an optional `--region`. |",
            "| `GCO_API_GATEWAY_REGION` | cdk.json `deployment_regions.api_gateway` | Region of the API Gateway stack. |",
            "| `GCO_MONITORING_REGION` | cdk.json `deployment_regions.monitoring` | Region of the monitoring stack. |",
            "| `GCO_DEFAULT_NAMESPACE` | `gco-jobs` | Kubernetes namespace for namespaced workloads. |",
            "| `GCO_OUTPUT_FORMAT` | `table` | `table`, `json` or `yaml` (the MCP server always passes `--output json`). |",
            "| `GCO_VERBOSE` | `false` | Verbose CLI output. |",
            "| `GCO_CACHE_DIR` | `~/.gco/cache` | CLI cache directory. |",
            "| `GCO_REGIONAL_API` | `false` | Call the regional APIs instead of API Gateway (private access). |",
            "| `GCO_STUDIO_PASSWORD` | (unset) | Password for `gco analytics studio login`; `analytics_login_url` passes it to the child this way, never on the command line. |",
            "| `GCO_STUDIO_NEW_PASSWORD` | (unset) | The new password `gco analytics studio login` sets at a first sign-in with a temporary password; `analytics_login_url` passes its `new_password` this way. |",
            "| `GCO_GRAFANA_USER_PASSWORD` | (unset) | Password for `gco monitoring users add`; `monitoring_user_add` passes it this way. |",
            "| `GCO_WEBHOOK_URL` / `GCO_WEBHOOK_SECRET` | (unset) | URL and HMAC secret for `gco webhooks create`; `webhooks_create` passes them this way. |",
            "| `AWS_REGION` / `AWS_DEFAULT_REGION` | (AWS SDK default) | Region for AWS SDK calls that have no Region from the GCO configuration; not the CLI's default Region (that is `GCO_DEFAULT_REGION`). |",
            "| `AWS_PROFILE` | (default) | AWS CLI profile to use. |",
            "| `GCO_RUNAI_URL` | (unset) | NVIDIA Run:ai control plane `gco capacity runai` reads (`https://host[:port]`). |",
            "| `GCO_RUNAI_CLIENT_ID` | (unset) | Run:ai service account or user access key ID. |",
            "| `GCO_RUNAI_CLIENT_SECRET` | (unset) | Its secret; read from the environment only, never a flag. |",
            "| `GCO_RUNAI_CA_FILE` | (unset) | PEM bundle for a control plane certificate signed by a private CA; the only trust anchor when set. |",
            "\n## Services (Kubernetes)\n",
            "| Variable | Default | Description |",
            "|----------|---------|-------------|",
            "| `AUTH_SECRET_ARN` | (from stack) | Secrets Manager ARN for authentication. |",
            "| `CLUSTER_NAME` | (from stack) | EKS cluster name. |",
            "| `JOB_QUEUE_URL` | (from stack) | SQS job queue URL. |",
        ]
    )
    return "\n".join(lines)
