"""Source code resources (source:// scheme) for the GCO MCP server."""

from pathlib import Path

from server import mcp

PROJECT_ROOT = Path(__file__).parent.parent.parent

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

_CONFIG_FILES = {
    "pyproject.toml",
    "cdk.json",
    "app.py",
    "Dockerfile.dev",
    ".gitlab-ci.yml",
    ".pre-commit-config.yaml",
    ".yamllint.yml",
    ".checkov.yaml",
    ".kics.yaml",
    ".gitleaks.toml",
    ".semgrepignore",
    ".dockerignore",
    ".gitignore",
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
    for name in sorted(_CONFIG_FILES):
        if (PROJECT_ROOT / name).is_file():
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
    path = PROJECT_ROOT / filename
    if not path.is_file():
        return f"File '{filename}' not found."
    return path.read_text()


@mcp.resource("source://gco/file/{filepath*}")
def source_file_resource(filepath: str) -> str:
    """Read any source file by its path relative to the project root."""
    path = (PROJECT_ROOT / filepath).resolve()
    if not str(path).startswith(str(PROJECT_ROOT.resolve())):
        return "Access denied: path is outside the project."
    if any(skip in path.parts for skip in _SKIP_DIRS):
        return "Access denied: path is in a skipped directory."
    if not path.is_file():
        return f"File '{filepath}' not found."
    if path.suffix not in _SOURCE_EXTENSIONS:
        return f"File type '{path.suffix}' not served. Allowed: {', '.join(_SOURCE_EXTENSIONS)}"
    return path.read_text()
