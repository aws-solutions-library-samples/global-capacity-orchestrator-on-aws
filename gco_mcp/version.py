"""
Version management for the GCO MCP server.

Keeps the MCP server version locked to the project-wide ``VERSION`` file
via ``gco/_version.py``.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def get_project_version() -> str:
    """Return the project version string, falling back to 'unknown'."""
    try:
        from gco._version import __version__

        return __version__
    except Exception:  # pragma: no cover
        return "unknown"
