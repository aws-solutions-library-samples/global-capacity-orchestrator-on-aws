"""Where Mission's global-stack resources live: the project name and global Region.

The global stack creates the ``<project>-missions`` state table, the
mission-memory table and vector index, and the SSM parameters that name them
(``/<project>/missions-table-name``, ``/<project>/mission-memory-*``), all in
``deployment_regions.global``. Mission code that reaches them must use the
same project name and Region as the deployment, not ``gco`` and whatever
Region the caller's AWS configuration defaults to.

Resolution, highest precedence first:

1. A value passed by the caller (``DynamoDBBackend(project_name=...)``,
   ``MissionMemoryStore(region=...)``).
2. :func:`configure`: the ``gco mission`` and ``gco swarm`` command groups pass
   the CLI's merged configuration (cdk.json, config file, ``GCO_*``
   variables), so CLI and Mission agree.
3. ``GCO_PROJECT_NAME`` / ``GCO_GLOBAL_REGION``.
4. cdk.json ``context.project_name`` / ``context.deployment_regions.global``
   in the project root: the directory set with :func:`configure` (the MCP
   server sets the root its ``gco`` children run from), else the working
   directory.
5. ``gco``, and no Region (the AWS SDK's default chain).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_configured_project_name: str | None = None
_configured_global_region: str | None = None
_configured_project_root: Path | None = None


def configure(
    *,
    project_name: str | None = None,
    global_region: str | None = None,
    project_root: Path | None = None,
) -> None:
    """Set the process-wide project name, global Region and cdk.json root.

    ``None`` clears a value, so the next source in the precedence applies.
    """
    global _configured_project_name, _configured_global_region, _configured_project_root
    _configured_project_name = project_name or None
    _configured_global_region = global_region or None
    _configured_project_root = project_root


def _cdk_context() -> dict[str, Any]:
    """Return cdk.json ``context`` from the project root, or ``{}``."""
    root = _configured_project_root if _configured_project_root is not None else Path.cwd()
    try:
        data = json.loads((root / "cdk.json").read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    context = data.get("context") if isinstance(data, dict) else None
    return context if isinstance(context, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def project_name(explicit: str | None = None) -> str:
    """Return the project name Mission's global-stack resources are named after."""
    return (
        _text(explicit)
        or _configured_project_name
        or _text(os.environ.get("GCO_PROJECT_NAME"))
        or _text(_cdk_context().get("project_name"))
        or "gco"
    )


def global_region(explicit: str | None = None) -> str | None:
    """Return the global stack's Region, or ``None`` for the SDK's default chain."""
    found = (
        _text(explicit) or _configured_global_region or _text(os.environ.get("GCO_GLOBAL_REGION"))
    )
    if found:
        return found
    regions = _cdk_context().get("deployment_regions")
    return _text(regions.get("global")) if isinstance(regions, dict) else None
