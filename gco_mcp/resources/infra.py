"""Infrastructure config resources (infra:// scheme) for the GCO MCP server."""

from pathlib import Path

from server import mcp

PROJECT_ROOT = Path(__file__).parent.parent.parent
DOCKERFILES_DIR = PROJECT_ROOT / "dockerfiles"
HELM_CHARTS_FILE = PROJECT_ROOT / "lambda" / "helm-installer" / "charts.yaml"


@mcp.resource("infra://gco/index")
def infra_index() -> str:
    """List infrastructure configuration files — Dockerfiles, Helm charts, CI/CD."""
    lines = ["# Infrastructure Configuration\n"]
    lines.append("## Dockerfiles")
    readme = DOCKERFILES_DIR / "README.md"
    if readme.is_file():
        lines.append("- `infra://gco/dockerfiles/README.md` — Dockerfiles overview")
    for f in sorted(DOCKERFILES_DIR.iterdir()):
        if f.is_file() and not f.name.startswith(".") and f.name != "README.md":
            lines.append(f"- `infra://gco/dockerfiles/{f.name}`")
    lines.append("\n## Helm Charts")
    if HELM_CHARTS_FILE.is_file():
        lines.append("- `infra://gco/helm/charts.yaml` — Helm chart versions and config")
    lines.append("\n## CI/CD")
    lines.append("- `ci://gco/index` — GitHub Actions workflows, composite actions, scripts")
    lines.append("- `source://gco/config/.gitlab-ci.yml` — GitLab CI pipeline (frozen reference)")
    lines.append("- `source://gco/config/.pre-commit-config.yaml` — Pre-commit hooks")
    lines.append("\n## Security & Linting")
    for name in (
        ".checkov.yaml",
        ".kics.yaml",
        ".gitleaks.toml",
        ".semgrepignore",
        ".yamllint.yml",
    ):
        lines.append(f"- `source://gco/config/{name}` — {name}")
    lines.append("\n## CDK Configuration")
    lines.append("- `source://gco/config/cdk.json` — CDK deployment configuration")
    lines.append("- `source://gco/config/app.py` — CDK app entry point")
    lines.append(
        "- `source://gco/config/pyproject.toml` — Python project metadata and dependencies"
    )
    lines.append("\n## Related Resources")
    lines.append("- `scripts://gco/index` — Utility scripts for operations")
    lines.append("- `demos://gco/index` — Demo walkthroughs and scripts")
    return "\n".join(lines)


@mcp.resource("infra://gco/dockerfiles/{filename}")
def dockerfile_resource(filename: str) -> str:
    """Read a Dockerfile, requirements file, or README for a GCO service."""
    path = DOCKERFILES_DIR / filename
    if not path.is_file():
        available = sorted(f.name for f in DOCKERFILES_DIR.iterdir() if f.is_file())
        return f"File '{filename}' not found. Available:\n" + "\n".join(available)
    return path.read_text()


@mcp.resource("infra://gco/helm/charts.yaml")
def helm_charts_resource() -> str:
    """Read the Helm charts configuration (chart names, versions, values)."""
    if not HELM_CHARTS_FILE.is_file():
        return "charts.yaml not found."
    return HELM_CHARTS_FILE.read_text()
