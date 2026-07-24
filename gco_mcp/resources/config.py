"""Current configuration resources for the GCO MCP server."""

from cli_runner import PROJECT_ROOT  # runtime-resolved checkout root (uvx-safe)
from feature_flags import ALL_FLAGS, FLAG_ALL_TOOLS
from server import mcp

_FLAG_DESCRIPTIONS = {
    "GCO_ENABLE_CAPACITY_PURCHASE": "Enable cost-incurring capacity reservation tools.",
    "GCO_ENABLE_MODEL_UPLOAD": "Enable local model-data uploads to central or regional S3.",
    "GCO_ENABLE_IMAGE_PUBLISH": "Enable image build, push, and mirror operations.",
    "GCO_ENABLE_INFRASTRUCTURE_DEPLOY": "Enable infrastructure deployment tools.",
    "GCO_ENABLE_INFRASTRUCTURE_DESTROY": "Enable infrastructure destroy tools.",
    "GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "Enable irreversible deletion/cancellation tools.",
    "GCO_ENABLE_MISSION": "Enable autonomous Mission tools.",
    "GCO_ENABLE_LOCAL_METRICS": "Enable reads beneath GCO_METRICS_LOCAL_ROOT.",
    "GCO_ENABLE_LOCAL_STORAGE_SYNC": "Enable local storage sync operations.",
    "GCO_ENABLE_SEMANTIC_PROGRESS": "Enable LLM-based semantic progress scoring.",
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
    """List MCP feature flags and operational environment variables."""
    lines = [
        "# GCO Environment Variables\n",
        "## MCP Server\n",
        "| Variable | Default | Description |",
        "|----------|---------|-------------|",
        "| `GCO_MCP_ROLE_ARN` | (unset) | IAM role ARN assumed when the server starts. |",
        "| `GCO_MCP_ROLE_SESSION_NAME` | `gco-mcp-server` | Assumed-role session name. |",
        "| `GCO_MCP_ROLE_DURATION_SECONDS` | `3600` | Assumed-role credential duration. |",
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
            "| `GCO_STORAGE_LOCAL_ROOT` | (unset) | Required root for model uploads and storage sync; relative paths resolve beneath it and short uploads use a descriptor-backed same-filesystem snapshot. |",
            "| `GCO_METRICS_LOCAL_ROOT` | (unset) | Required root for local metric-file reads. |",
            "| `GCO_TASK_STATUS_DIR` | `~/.gco/tasks` | Private bounded task status/log directory. |",
            "| `GCO_DISABLE_TASK_STATUS` | `false` | Disable disk-backed task status emission. |",
            "| `FASTMCP_DOCKET_URL` | `memory://` | FastMCP background-task store. |",
            "\n## CLI\n",
            "| Variable | Default | Description |",
            "|----------|---------|-------------|",
            "| `AWS_REGION` | (from config) | Default AWS region for CLI commands. |",
            "| `AWS_PROFILE` | (default) | AWS CLI profile to use. |",
            "| `GCO_CONFIG_PATH` | `cdk.json` | Path to the CDK configuration file. |",
            "\n## Services (Kubernetes)\n",
            "| Variable | Default | Description |",
            "|----------|---------|-------------|",
            "| `AUTH_SECRET_ARN` | (from stack) | Secrets Manager ARN for authentication. |",
            "| `CLUSTER_NAME` | (from stack) | EKS cluster name. |",
            "| `JOB_QUEUE_URL` | (from stack) | SQS job queue URL. |",
            "| `DLQ_URL` | (from stack) | Dead-letter queue URL. |",
            "| `DYNAMODB_TABLE` | (from stack) | Job/template/webhook table name. |",
        ]
    )
    return "\n".join(lines)
