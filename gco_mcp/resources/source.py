"""Source code resources (source:// scheme) for the GCO MCP server."""

from pathlib import Path

from cli_runner import PROJECT_ROOT  # runtime-resolved checkout root (uvx-safe)
from server import mcp

_SOURCE_DIRS = {
    "gco": PROJECT_ROOT / "gco",
    "cli": PROJECT_ROOT / "cli",
    "lambda": PROJECT_ROOT / "lambda",
    "gco_mcp": PROJECT_ROOT / "gco_mcp",
    "scripts": PROJECT_ROOT / "scripts",
    "demo": PROJECT_ROOT / "demo",
    "dockerfiles": PROJECT_ROOT / "dockerfiles",
}
_SKIP_DIRS = {
    "__pycache__",
    ".git",
    "cdk.out",
    "node_modules",
    "kubectl-applier-simple-build",
    "helm-installer-build",
}
_SOURCE_EXTENSIONS = {".py", ".yaml", ".yml", ".json", ".txt", ".toml", ".cfg", ".sh", ".md"}

# Config files exposed via the source://gco/config/<name> URI. The logical name
# (the key) is kept stable even though several files now live under .github/, so
# existing references to these URIs keep resolving.
_GITHUB_CONFIG_DIR = PROJECT_ROOT / ".github" / "config"
_CONFIG_FILES = {
    "pyproject.toml": PROJECT_ROOT / "pyproject.toml",
    "cdk.json": PROJECT_ROOT / "cdk.json",
    "app.py": PROJECT_ROOT / "app.py",
    "Dockerfile.dev": PROJECT_ROOT / "Dockerfile.dev",
    ".pre-commit-config.yaml": PROJECT_ROOT / ".pre-commit-config.yaml",
    ".dockerignore": PROJECT_ROOT / ".dockerignore",
    ".gitignore": PROJECT_ROOT / ".gitignore",
    ".semgrepignore": PROJECT_ROOT / ".semgrepignore",
    ".yamllint.yml": _GITHUB_CONFIG_DIR / ".yamllint.yml",
    ".checkov.yaml": _GITHUB_CONFIG_DIR / ".checkov.yaml",
    ".kics.yaml": _GITHUB_CONFIG_DIR / ".kics.yaml",
    ".gitleaks.toml": _GITHUB_CONFIG_DIR / ".gitleaks.toml",
}


def _list_source_files(base: Path) -> list[Path]:
    """Walk a directory and return all source files, skipping noise."""
    files = []
    for p in sorted(base.rglob("*")):
        if any(skip in p.parts for skip in _SKIP_DIRS):
            continue
        if p.is_file() and p.suffix in _SOURCE_EXTENSIONS:
            files.append(p)
    return files


@mcp.resource("source://gco/index")
def source_index() -> str:
    """List all source code files available for reading, grouped by package."""
    sections = ["# GCO Source Code Index\n"]
    sections.append("## Project Config")
    for name, path in sorted(_CONFIG_FILES.items()):
        if path.is_file():
            sections.append(f"- `source://gco/config/{name}`")
    for pkg, base in _SOURCE_DIRS.items():
        if not base.is_dir():
            continue
        files = _list_source_files(base)
        if not files:
            continue
        sections.append(f"\n## {pkg}/ ({len(files)} files)")
        for f in files:
            rel = f.relative_to(PROJECT_ROOT)
            sections.append(f"- `source://gco/file/{rel}`")
    return "\n".join(sections)


@mcp.resource("source://gco/config/{filename}")
def config_file_resource(filename: str) -> str:
    """Read a top-level project config file (pyproject.toml, cdk.json, etc.)."""
    if filename not in _CONFIG_FILES:
        return f"Not available. Allowed: {', '.join(sorted(_CONFIG_FILES))}"
    path = _CONFIG_FILES[filename]
    if not path.is_file():
        return f"File '{filename}' not found."
    return path.read_text()


@mcp.resource("source://gco/file/{filepath*}")
def source_file_resource(filepath: str) -> str:
    """Read a source file confined beneath the project root."""
    root = PROJECT_ROOT.resolve()
    path = (root / filepath).resolve()
    if not path.is_relative_to(root):
        return "Access denied: path is outside the project."
    if any(skip in path.parts for skip in _SKIP_DIRS):
        return "Access denied: path is in a skipped directory."
    if not path.is_file():
        return f"File '{filepath}' not found."
    if path.suffix not in _SOURCE_EXTENSIONS:
        return f"File type '{path.suffix}' not served. Allowed: {', '.join(_SOURCE_EXTENSIONS)}"
    return path.read_text()
