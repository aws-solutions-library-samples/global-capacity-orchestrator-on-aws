"""Utility script resources (scripts:// scheme) for the GCO MCP server."""

from pathlib import Path

from server import mcp

PROJECT_ROOT = Path(__file__).parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
_SCRIPT_EXTENSIONS = {".py", ".sh"}


@mcp.resource("scripts://gco/index")
def scripts_index() -> str:
    """List utility scripts for cluster access, versioning, testing, and operations."""
    lines = ["# Utility Scripts\n"]
    readme = SCRIPTS_DIR / "README.md"
    if readme.is_file():
        lines.append("- `scripts://gco/README` — Scripts overview and usage\n")
    for f in sorted(SCRIPTS_DIR.iterdir()):
        if f.is_file() and f.suffix in _SCRIPT_EXTENSIONS:
            desc = f.stem.replace("_", " ").replace("-", " ").title()
            lines.append(f"- `scripts://gco/{f.name}` — {desc}")
    return "\n".join(lines)


@mcp.resource("scripts://gco/{filename}")
def script_resource(filename: str) -> str:
    """Read a utility script."""
    path = SCRIPTS_DIR / filename
    if not path.is_file():
        path = SCRIPTS_DIR / f"{filename}.md"
    if not path.is_file():
        available = sorted(
            f.name
            for f in SCRIPTS_DIR.iterdir()
            if f.is_file() and f.suffix in (_SCRIPT_EXTENSIONS | {".md"})
        )
        return f"Script '{filename}' not found. Available:\n" + "\n".join(available)
    if path.suffix not in (_SCRIPT_EXTENSIONS | {".md"}):
        return f"File type '{path.suffix}' not served."
    return path.read_text()
