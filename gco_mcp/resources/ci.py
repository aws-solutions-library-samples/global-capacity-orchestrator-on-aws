"""CI/CD resources (ci:// scheme) for the GCO MCP server."""

from pathlib import Path

from server import mcp

PROJECT_ROOT = Path(__file__).parent.parent.parent
GITHUB_DIR = PROJECT_ROOT / ".github"
GITHUB_WORKFLOWS_DIR = GITHUB_DIR / "workflows"
GITHUB_ACTIONS_DIR = GITHUB_DIR / "actions"
GITHUB_SCRIPTS_DIR = GITHUB_DIR / "scripts"
GITHUB_ISSUE_TEMPLATE_DIR = GITHUB_DIR / "ISSUE_TEMPLATE"
GITHUB_KIND_DIR = GITHUB_DIR / "kind"
GITHUB_CODEQL_DIR = GITHUB_DIR / "codeql"

_CI_EXTENSIONS = {".yml", ".yaml", ".md", ".sh", ".json", ".toml"}
_CI_CONFIG_FILES = {
    "CI.md",
    "CODEOWNERS",
    "SECURITY.md",
    "dependabot.yml",
    "release.yml",
    "pull_request_template.md",
}


def _ci_read(path: Path, kind: str, available_root: Path | None = None) -> str:
    """Read a file from the .github/ tree with consistent error handling."""
    if path.is_file():
        if path.suffix and path.suffix not in _CI_EXTENSIONS and path.name not in _CI_CONFIG_FILES:
            return f"File type '{path.suffix}' not served. Allowed: {', '.join(sorted(_CI_EXTENSIONS))}"
        return path.read_text()
    if available_root is not None and available_root.is_dir():
        available = sorted(f.name for f in available_root.iterdir() if f.is_file())
        return f"{kind} '{path.name}' not found. Available:\n" + "\n".join(available)
    return f"{kind} '{path.name}' not found."


@mcp.resource("ci://gco/index")
def ci_index() -> str:
    """List CI/CD artefacts under .github/."""
    lines = ["# GitHub Actions & CI Configuration\n"]

    lines.append("## Documentation & Policy")
    for name in ("CI.md", "SECURITY.md", "CODEOWNERS"):
        if (GITHUB_DIR / name).is_file():
            lines.append(f"- `ci://gco/config/{name}`")
    lines.append("")

    if GITHUB_WORKFLOWS_DIR.is_dir():
        workflow_files = sorted(
            f
            for f in GITHUB_WORKFLOWS_DIR.iterdir()
            if f.is_file() and f.suffix in {".yml", ".yaml"}
        )
        if workflow_files:
            lines.append(f"## Workflows ({len(workflow_files)} files)")
            lines.append("Run on every push and PR unless otherwise noted.")
            for f in workflow_files:
                lines.append(f"- `ci://gco/workflows/{f.name}` — {f.stem}")
            lines.append("")

    if GITHUB_ACTIONS_DIR.is_dir():
        action_names = sorted(d.name for d in GITHUB_ACTIONS_DIR.iterdir() if d.is_dir())
        if action_names:
            lines.append(f"## Composite Actions ({len(action_names)})")
            lines.append("Reusable action definitions referenced by the workflows above.")
            for name in action_names:
                lines.append(f"- `ci://gco/actions/{name}` — {name}")
            lines.append("")

    if GITHUB_SCRIPTS_DIR.is_dir():
        script_files = sorted(
            f for f in GITHUB_SCRIPTS_DIR.iterdir() if f.is_file() and f.suffix in {".sh", ".py"}
        )
        if script_files:
            lines.append(f"## Scripts ({len(script_files)})")
            lines.append("Helper scripts invoked from the workflows.")
            for f in script_files:
                lines.append(f"- `ci://gco/scripts/{f.name}` — {f.stem}")
            lines.append("")

    template_entries: list[str] = []
    if GITHUB_ISSUE_TEMPLATE_DIR.is_dir():
        for f in sorted(GITHUB_ISSUE_TEMPLATE_DIR.iterdir()):
            if f.is_file() and f.suffix in {".md", ".yml", ".yaml"}:
                template_entries.append(f"- `ci://gco/templates/{f.name}` — {f.stem}")
    if (GITHUB_DIR / "pull_request_template.md").is_file():
        template_entries.append(
            "- `ci://gco/templates/pull_request_template.md` — Pull request template"
        )
    if template_entries:
        lines.append(f"## Issue & PR Templates ({len(template_entries)})")
        lines.extend(template_entries)
        lines.append("")

    if GITHUB_CODEQL_DIR.is_dir():
        codeql_files = sorted(f for f in GITHUB_CODEQL_DIR.iterdir() if f.is_file())
        if codeql_files:
            lines.append("## CodeQL Configuration")
            for f in codeql_files:
                lines.append(f"- `ci://gco/codeql/{f.name}` — {f.stem}")
            lines.append("")

    if GITHUB_KIND_DIR.is_dir():
        kind_files = sorted(f for f in GITHUB_KIND_DIR.iterdir() if f.is_file())
        if kind_files:
            lines.append("## Kind Cluster Configuration")
            for f in kind_files:
                lines.append(f"- `ci://gco/kind/{f.name}` — {f.stem}")
            lines.append("")

    automation_entries: list[str] = []
    if (GITHUB_DIR / "dependabot.yml").is_file():
        automation_entries.append("- `ci://gco/config/dependabot.yml` — Dependabot config")
    if (GITHUB_DIR / "release.yml").is_file():
        automation_entries.append(
            "- `ci://gco/config/release.yml` — Release notes auto-categorisation"
        )
    if automation_entries:
        lines.append("## Repo Automation")
        lines.extend(automation_entries)
        lines.append("")

    lines.append("## Related Resources")
    lines.append("- `infra://gco/index` — Dockerfiles and Helm charts")
    lines.append("- `scripts://gco/index` — Utility scripts outside CI")
    lines.append("- `source://gco/index` — Full source code browser")
    return "\n".join(lines)


@mcp.resource("ci://gco/workflows/{filename}")
def ci_workflow_resource(filename: str) -> str:
    """Read a GitHub Actions workflow YAML file."""
    return _ci_read(GITHUB_WORKFLOWS_DIR / filename, "Workflow", GITHUB_WORKFLOWS_DIR)


@mcp.resource("ci://gco/actions/{action_name}")
def ci_action_resource(action_name: str) -> str:
    """Read the action.yml for a composite action under .github/actions/."""
    action_name = action_name.removesuffix("/action.yml").strip("/")
    action_path = GITHUB_ACTIONS_DIR / action_name / "action.yml"
    if action_path.is_file():
        return action_path.read_text()
    action_path_alt = GITHUB_ACTIONS_DIR / action_name / "action.yaml"
    if action_path_alt.is_file():
        return action_path_alt.read_text()
    if GITHUB_ACTIONS_DIR.is_dir():
        available = sorted(d.name for d in GITHUB_ACTIONS_DIR.iterdir() if d.is_dir())
        return f"Composite action '{action_name}' not found. Available:\n" + "\n".join(available)
    return f"Composite action '{action_name}' not found."


@mcp.resource("ci://gco/scripts/{filename}")
def ci_script_resource(filename: str) -> str:
    """Read a helper script from .github/scripts/."""
    return _ci_read(GITHUB_SCRIPTS_DIR / filename, "Script", GITHUB_SCRIPTS_DIR)


@mcp.resource("ci://gco/templates/{filename}")
def ci_template_resource(filename: str) -> str:
    """Read an issue or pull-request template."""
    issue_path = GITHUB_ISSUE_TEMPLATE_DIR / filename
    if issue_path.is_file():
        return issue_path.read_text()
    pr_path = GITHUB_DIR / filename
    if pr_path.is_file() and filename.startswith("pull_request_template"):
        return pr_path.read_text()
    available: list[str] = []
    if GITHUB_ISSUE_TEMPLATE_DIR.is_dir():
        available.extend(f.name for f in GITHUB_ISSUE_TEMPLATE_DIR.iterdir() if f.is_file())
    if (GITHUB_DIR / "pull_request_template.md").is_file():
        available.append("pull_request_template.md")
    available.sort()
    return f"Template '{filename}' not found. Available:\n" + "\n".join(available)


@mcp.resource("ci://gco/codeql/{filename}")
def ci_codeql_resource(filename: str) -> str:
    """Read a CodeQL configuration file."""
    return _ci_read(GITHUB_CODEQL_DIR / filename, "CodeQL config", GITHUB_CODEQL_DIR)


@mcp.resource("ci://gco/kind/{filename}")
def ci_kind_resource(filename: str) -> str:
    """Read a kind (Kubernetes-in-Docker) config file."""
    return _ci_read(GITHUB_KIND_DIR / filename, "Kind config", GITHUB_KIND_DIR)


@mcp.resource("ci://gco/config/{filename}")
def ci_config_resource(filename: str) -> str:
    """Read a repo-level CI config file (CI.md, CODEOWNERS, etc.)."""
    if filename not in _CI_CONFIG_FILES:
        return (
            f"Config file '{filename}' is not in the served allowlist. "
            f"Allowed: {', '.join(sorted(_CI_CONFIG_FILES))}"
        )
    path = GITHUB_DIR / filename
    if not path.is_file():
        return f"Config file '{filename}' not found."
    return path.read_text()
