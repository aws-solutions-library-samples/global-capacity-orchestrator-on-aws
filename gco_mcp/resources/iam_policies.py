"""IAM policy resources (iam:// scheme) for the GCO MCP server."""

from pathlib import Path

from server import mcp

PROJECT_ROOT = Path(__file__).parent.parent.parent
IAM_POLICIES_DIR = PROJECT_ROOT / "docs" / "iam-policies"


@mcp.resource("iam://gco/policies/index")
def iam_policies_index() -> str:
    """List available IAM policy templates for GCO access control."""
    lines = ["# IAM Policy Templates\n"]
    for f in sorted(IAM_POLICIES_DIR.glob("*.json")):
        lines.append(f"- `iam://gco/policies/{f.name}` — {f.stem}")
    readme = IAM_POLICIES_DIR / "README.md"
    if readme.is_file():
        lines.append("\n- `iam://gco/policies/README.md` — policy documentation")
    return "\n".join(lines)


@mcp.resource("iam://gco/policies/{filename}")
def iam_policy_resource(filename: str) -> str:
    """Read an IAM policy template."""
    path = IAM_POLICIES_DIR / filename
    if not path.is_file():
        available = sorted(f.name for f in IAM_POLICIES_DIR.glob("*") if f.is_file())
        return f"Policy '{filename}' not found. Available:\n" + "\n".join(available)
    return path.read_text()
