"""Test suite resources (tests:// scheme) for the GCO MCP server.

Exposes the test README, test configuration, and individual test files
so the LLM can understand testing patterns, fixtures, and coverage.
"""

from pathlib import Path

from server import mcp

PROJECT_ROOT = Path(__file__).parent.parent.parent
TESTS_DIR = PROJECT_ROOT / "tests"
_TEST_EXTENSIONS = {".py", ".md"}


@mcp.resource("tests://gco/index")
def tests_index() -> str:
    """List test suite documentation, configuration, and test files."""
    lines = ["# GCO Test Suite\n"]
    readme = TESTS_DIR / "README.md"
    if readme.is_file():
        lines.append("- `tests://gco/README` — Test suite overview, patterns, and mocking guide\n")

    # Categorize test files
    test_files = sorted(f for f in TESTS_DIR.glob("test_*.py"))
    helper_files = sorted(f for f in TESTS_DIR.glob("_*.py"))
    config_files = sorted(f for f in TESTS_DIR.iterdir() if f.name in ("conftest.py", "pytest.ini"))

    if config_files or helper_files:
        lines.append("## Test Infrastructure")
        for f in config_files:
            lines.append(f"- `tests://gco/{f.name}` — {f.stem}")
        for f in helper_files:
            lines.append(f"- `tests://gco/{f.name}` — {f.stem}")
        lines.append("")

    if test_files:
        lines.append(f"## Test Files ({len(test_files)} files)")
        for f in test_files:
            lines.append(f"- `tests://gco/{f.name}` — {f.stem}")
        lines.append("")

    # BATS tests
    bats_dir = TESTS_DIR / "BATS"
    if bats_dir.is_dir():
        bats_files = sorted(bats_dir.glob("*.bats"))
        bats_readme = bats_dir / "README.md"
        if bats_files or bats_readme.is_file():
            lines.append("## BATS Shell Tests")
            if bats_readme.is_file():
                lines.append("- `tests://gco/BATS/README.md` — BATS test overview")
            for f in bats_files:
                lines.append(f"- `tests://gco/BATS/{f.name}` — {f.stem}")

    return "\n".join(lines)


@mcp.resource("tests://gco/{filepath*}")
def test_file_resource(filepath: str) -> str:
    """Read a test file, helper, or configuration file.

    Args:
        filepath: Path relative to tests/ (e.g. test_mcp_server.py, conftest.py, BATS/README.md).
    """
    path = TESTS_DIR / filepath
    if not path.is_file():
        # Try with .md extension for README
        path = TESTS_DIR / f"{filepath}.md"
    if not path.is_file():
        return f"Test file '{filepath}' not found."
    if path.suffix not in (_TEST_EXTENSIONS | {".bats"}):
        return f"File type '{path.suffix}' not served."
    return path.read_text()
