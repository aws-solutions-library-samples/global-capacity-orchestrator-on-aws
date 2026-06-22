"""Live job state resources (gco://jobs/...) for the GCO MCP server.

Each handler is a thin wrapper around ``kubectl get job`` so the
resource layer never re-implements Kubernetes plumbing. Handler
returns the raw YAML (or a structured error string) — pinning a job
URI is a cheap way for an LLM to keep the latest manifest in context
across turns.
"""

from __future__ import annotations

import json
import re
from typing import Any

import cli_runner

# RFC 1123 label format. Job names live in the same namespace as pod
# names, so the same rule applies. Bounded length stops accidental
# command-line stuffing through a malformed URI template expansion.
_JOB_NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?$")
_DEFAULT_NAMESPACE = "gco-jobs"
_KUBECTL_TIMEOUT_SECONDS = 30


def _job_resource(job_name: str) -> str:
    """Return the live YAML for ``job_name`` in the GCO jobs namespace."""
    if not _JOB_NAME_RE.match(job_name):
        return json.dumps(
            {
                "error": "invalid job_name",
                "detail": "must match ^[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?$",
                "value": job_name,
            }
        )
    try:
        result = cli_runner.subprocess.run(  # type: ignore[attr-defined] # nosemgrep: dangerous-subprocess-use-audit - shell=False; argv is a literal list with a validated job_name
            [
                "kubectl",
                "get",
                "job",
                job_name,
                "-n",
                _DEFAULT_NAMESPACE,
                "-o",
                "yaml",
            ],
            capture_output=True,
            text=True,
            timeout=_KUBECTL_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return json.dumps({"error": "kubectl not found"})
    except cli_runner.subprocess.TimeoutExpired:  # type: ignore[attr-defined]
        return json.dumps({"error": f"kubectl timed out after {_KUBECTL_TIMEOUT_SECONDS}s"})
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        return json.dumps(
            {"error": err or "kubectl command failed", "exit_code": result.returncode}
        )
    return str(result.stdout)


def register(mcp_instance: Any) -> None:
    """Register live job-state resources against the shared MCP server."""
    mcp_instance.resource("gco://jobs/{job_name}")(_job_resource)
