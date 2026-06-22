"""Configuration resources (config:// scheme) for the GCO MCP server.

Exposes the CDK configuration schema, feature toggles, environment variables,
and the cdk.json configuration matrix used by tests.
"""

import json
from pathlib import Path

from server import mcp

PROJECT_ROOT = Path(__file__).parent.parent.parent


@mcp.resource("config://gco/index")
def config_index() -> str:
    """List configuration resources — CDK schema, feature toggles, env vars."""
    lines = ["# GCO Configuration\n"]
    lines.append("## CDK Configuration")
    lines.append("- `config://gco/cdk.json` — Current CDK deployment configuration")
    lines.append("- `config://gco/feature-toggles` — All feature toggles and their defaults")
    lines.append(
        "- `config://gco/env-vars` — Environment variables used by the MCP server and services\n"
    )
    lines.append("## Related")
    lines.append("- `source://gco/config/pyproject.toml` — Python project metadata")
    lines.append("- `source://gco/config/app.py` — CDK app entry point")
    lines.append("- `docs://gco/docs/CUSTOMIZATION` — Full customization guide")
    return "\n".join(lines)


@mcp.resource("config://gco/cdk.json")
def cdk_json_resource() -> str:
    """Read the current CDK deployment configuration."""
    path = PROJECT_ROOT / "cdk.json"
    if not path.is_file():
        return "cdk.json not found."
    return path.read_text()


@mcp.resource("config://gco/feature-toggles")
def feature_toggles_resource() -> str:
    """List all feature toggles available in cdk.json with their defaults.

    This resource parses the current cdk.json and documents every
    configurable feature toggle so the LLM can help users enable/disable
    features.
    """
    path = PROJECT_ROOT / "cdk.json"
    if not path.is_file():
        return "cdk.json not found."

    try:
        config = json.loads(path.read_text())
        context = config.get("context", {})
    except json.JSONDecodeError, KeyError:
        return "Could not parse cdk.json."

    lines = ["# GCO Feature Toggles\n"]
    lines.append("These are the configurable options in `cdk.json` under the `context` key.\n")

    # Extract known toggle categories
    if "regions" in context:
        lines.append(f"## Regions\n`regions`: {json.dumps(context['regions'])}\n")

    helm = context.get("helm", {})
    if helm:
        lines.append("## Helm Charts (Schedulers & Operators)\n")
        for chart, cfg in sorted(helm.items()):
            enabled = cfg.get("enabled", False) if isinstance(cfg, dict) else cfg
            lines.append(f"- `helm.{chart}.enabled`: {enabled}")
        lines.append("")

    # Storage toggles
    for key in ("fsx", "valkey", "aurora_pgvector"):
        val = context.get(key, {})
        if isinstance(val, dict):
            enabled = val.get("enabled", False)
            lines.append(f"## {key}\n- `{key}.enabled`: {enabled}")
            for k, v in sorted(val.items()):
                if k != "enabled":
                    lines.append(f"- `{key}.{k}`: {v}")
            lines.append("")

    # Resource quotas
    for key in ("manifest_processor", "queue_processor"):
        val = context.get(key, {})
        if val:
            lines.append(f"## {key}\n")
            for k, v in sorted(val.items()):
                if isinstance(v, dict):
                    for k2, v2 in sorted(v.items()):
                        lines.append(f"- `{key}.{k}.{k2}`: {v2}")
                else:
                    lines.append(f"- `{key}.{k}`: {v}")
            lines.append("")

    return "\n".join(lines)


@mcp.resource("config://gco/env-vars")
def env_vars_resource() -> str:
    """List environment variables used by the GCO MCP server and services."""
    return """# GCO Environment Variables

## MCP Server

| Variable | Default | Description |
|----------|---------|-------------|
| `GCO_MCP_ROLE_ARN` | (unset) | IAM role ARN to assume at startup. When set, the MCP server uses STS AssumeRole for least-privilege access. |
| `GCO_MCP_ROLE_SESSION_NAME` | `gco-mcp-server` | Session name for the assumed role. |
| `GCO_MCP_ROLE_DURATION_SECONDS` | `3600` | Duration in seconds for the assumed role credentials. |
| `GCO_ENABLE_CAPACITY_PURCHASE` | `false` | Set to `true` to enable the `reserve_capacity` tool (can incur AWS charges). |

## CLI

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_REGION` | (from config) | Default AWS region for CLI commands. |
| `AWS_PROFILE` | (default) | AWS CLI profile to use. |
| `GCO_CONFIG_PATH` | `cdk.json` | Path to the CDK configuration file. |

## Services (Kubernetes)

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_SECRET_ARN` | (from stack) | Secrets Manager ARN for the authentication secret. |
| `CLUSTER_NAME` | (from stack) | EKS cluster name. |
| `JOB_QUEUE_URL` | (from stack) | SQS queue URL for job submission. |
| `DLQ_URL` | (from stack) | Dead letter queue URL. |
| `DYNAMODB_TABLE` | (from stack) | DynamoDB table name for job/template/webhook storage. |
"""
