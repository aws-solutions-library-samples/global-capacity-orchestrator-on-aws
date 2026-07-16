"""Tests that exercise the Python 3.14+ minimum-version commitment.

The config resource import is kept as a direct compatibility smoke check, and
the interpreter floor is asserted explicitly. Documentation is scanned for
legacy supported-version claims.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Mirror tests/test_mcp_audit.py's path-prep pattern: gco_mcp/ is a sibling package
# whose modules use bare imports (``from server import mcp``). Putting the
# directory on sys.path makes those imports resolve when this test runs.
_MCP_DIR = str(PROJECT_ROOT / "gco_mcp")
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)


def test_config_resource_imports_on_supported_python() -> None:
    """The current config resource imports and serves raw CDK configuration."""
    assert sys.version_info >= (3, 14)
    from resources import config as config_resource

    result = config_resource.cdk_json_resource()
    assert isinstance(result, str)
    assert result.strip(), "cdk_json_resource() returned empty content"


# Doc files whose Python-version language was bumped to 3.14+.
DOC_FILES = (
    "README.md",
    "gco_mcp/README.md",
    "CONTRIBUTING.md",
    "QUICKSTART.md",
    ".github/oidc_provider/README.md",
    "docs/TROUBLESHOOTING.md",
    "demo/DEMO_WALKTHROUGH.md",
)

# Match "Python 3.10" through "Python 3.13", optionally followed by a "+".
# A negative-lookahead block on the digit prevents matching "Python 3.14",
# "Python 3.140", etc. The range is constrained to the four versions the
# project intentionally dropped.
_LEGACY_VERSION_RE = re.compile(r"\bPython\s+3\.(?:1[0-3])(?!\d)\+?")


def test_no_legacy_python_versions_in_docs() -> None:
    """Fails if any tracked doc file still names Python 3.10 / 3.11 / 3.12 / 3.13."""
    failures: list[str] = []
    for rel in DOC_FILES:
        path = PROJECT_ROOT / rel
        assert path.is_file(), f"expected doc file missing: {rel}"
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if _LEGACY_VERSION_RE.search(line):
                failures.append(f"{rel}:{line_no}: {line.rstrip()}")
    assert not failures, "Legacy Python version references found in docs:\n  " + "\n  ".join(
        failures
    )
