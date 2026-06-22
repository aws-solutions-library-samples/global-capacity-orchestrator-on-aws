"""API client example resources (clients:// scheme) for the GCO MCP server."""

from pathlib import Path

from server import mcp

PROJECT_ROOT = Path(__file__).parent.parent.parent
CLIENT_EXAMPLES_DIR = PROJECT_ROOT / "docs" / "client-examples"
_CLIENT_EXTENSIONS = {".py", ".sh", ".md"}


@mcp.resource("clients://gco/index")
def clients_index() -> str:
    """List API client examples for interacting with the GCO API Gateway."""
    lines = ["# API Client Examples\n"]
    lines.append("- `clients://gco/README` — Overview, setup, and API reference\n")
    for f in sorted(CLIENT_EXAMPLES_DIR.iterdir()):
        if f.is_file() and f.suffix in _CLIENT_EXTENSIONS and f.name != "README.md":
            desc = f.stem.replace("_", " ").title()
            lines.append(f"- `clients://gco/{f.name}` — {desc}")
    return "\n".join(lines)


@mcp.resource("clients://gco/{filename}")
def client_example_resource(filename: str) -> str:
    """Read an API client example file."""
    path = CLIENT_EXAMPLES_DIR / filename
    if not path.is_file():
        path = CLIENT_EXAMPLES_DIR / f"{filename}.md"
    if not path.is_file():
        available = sorted(f.name for f in CLIENT_EXAMPLES_DIR.iterdir() if f.is_file())
        return f"Client example '{filename}' not found. Available:\n" + "\n".join(available)
    if path.suffix not in _CLIENT_EXTENSIONS:
        return f"File type '{path.suffix}' not served."
    return path.read_text()
