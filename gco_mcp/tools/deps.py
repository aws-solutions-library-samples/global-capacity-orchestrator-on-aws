"""Dependency-maintenance tools (update scan + NodePool registry freshness)."""

import json

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "observability"})
@audit_logged
async def deps_scan(nodepools_only: bool = False) -> str:
    """Generate the dependency update list the monthly deps-scan produces.

    Runs `gco deps scan` — the same scanner behind the rolling
    "[Automated] Dependency updates available" GitHub issue — and returns
    a JSON envelope with `has_drift`, `scan_complete`, and the full
    Markdown report under `report_markdown`. Surfaces that need AWS
    credentials or missing host tools are skipped and flagged as
    incomplete rather than failing.

    The full scan reaches out to PyPI, npm, container registries, GitHub,
    and (with credentials) AWS, and typically takes several minutes. Its
    Python surface pip-installs the project's extras into the server's
    active environment, mirroring how the CI scan runs. Requires a GCO
    checkout (the scanner lives under .github/scripts/).

    Args:
        nodepools_only: Run only the accelerator-catalog / Karpenter
            NodePool freshness check (offline policy validation always;
            live EC2 catalog comparison when AWS credentials resolve).
            Fast, and the only network it may touch is EC2.
    """
    args = ["deps", "scan"]
    if nodepools_only:
        args.append("--nodepools-only")
    # A full scan sweeps several registries and installs the Python extras;
    # give it far more headroom than the default two minutes.
    timeout = 300 if nodepools_only else 1800
    result = await cli_runner._run_cli_async(*args, timeout_seconds=timeout)
    # _run_cli_async returns either the command's stdout (already a JSON
    # envelope — the CLI honors the global --output json flag) or its own
    # JSON error envelope; both are valid JSON strings. Guard anyway so a
    # partial read never surfaces as an unparseable blob.
    try:
        json.loads(result)
    except TypeError, ValueError:
        return json.dumps({"error": "deps scan produced unparseable output", "raw": result[:2000]})
    return result
