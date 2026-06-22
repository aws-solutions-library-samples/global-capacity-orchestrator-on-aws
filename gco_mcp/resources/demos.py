"""Demo and walkthrough resources (demos:// scheme) for the GCO MCP server."""

from pathlib import Path

from server import mcp

PROJECT_ROOT = Path(__file__).parent.parent.parent
DEMO_DIR = PROJECT_ROOT / "demo"
_DEMO_EXTENSIONS = {".md", ".sh", ".py"}


@mcp.resource("demos://gco/index")
def demos_index() -> str:
    """List demo walkthroughs, live demo scripts, and presentation materials."""
    lines = ["# Demo & Walkthrough Resources\n"]
    lines.append("## Walkthroughs")
    for name in ("DEMO_WALKTHROUGH", "INFERENCE_WALKTHROUGH", "LIVE_DEMO"):
        path = DEMO_DIR / f"{name}.md"
        if path.is_file():
            lines.append(f"- `demos://gco/{name}` — {name.replace('_', ' ').title()}")
    lines.append("\n- `demos://gco/README` — Demo starter kit overview")
    lines.append("\n## Live Demo Scripts")
    for name in (
        "live_demo.sh",
        "lib_demo.sh",
        "record_demo.sh",
        "record_deploy.sh",
        "record_destroy.sh",
    ):
        path = DEMO_DIR / name
        if path.is_file():
            lines.append(f"- `demos://gco/{name}` — {name}")
    lines.append("\n## Utilities")
    path = DEMO_DIR / "md_to_pdf.py"
    if path.is_file():
        lines.append("- `demos://gco/md_to_pdf.py` — Markdown to PDF converter")
    return "\n".join(lines)


@mcp.resource("demos://gco/{filename}")
def demo_resource(filename: str) -> str:
    """Read a demo walkthrough, script, or utility file."""
    path = DEMO_DIR / filename
    if not path.is_file():
        path = DEMO_DIR / f"{filename}.md"
    if not path.is_file():
        available = sorted(
            f.name for f in DEMO_DIR.iterdir() if f.is_file() and f.suffix in _DEMO_EXTENSIONS
        )
        return f"Demo file '{filename}' not found. Available:\n" + "\n".join(available)
    if path.suffix not in _DEMO_EXTENSIONS:
        return f"File type '{path.suffix}' not served. Allowed: {', '.join(_DEMO_EXTENSIONS)}"
    return path.read_text()
