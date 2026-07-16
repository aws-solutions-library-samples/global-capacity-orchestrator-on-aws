"""Live job state resources for explicitly selected regional clusters."""

from __future__ import annotations

import json
import re
from typing import Any

import cli_runner

from resources._eks import eks_context_for_region, is_valid_region

# RFC 1123 label format. Job names live in the same namespace as pod
# names, so the same rule applies. Bounded length stops accidental
# command-line stuffing through a malformed URI template expansion.
_JOB_NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?$")
_DEFAULT_NAMESPACE = "gco-jobs"
_KUBECTL_TIMEOUT_SECONDS = 30


def _job_resource(job_name: str) -> str:
    """Fail closed for the legacy URI that did not identify a cluster."""
    return json.dumps(
        {
            "error": "explicit region required",
            "code": "eks_region_required",
            "use": f"gco://jobs/{{region}}/{job_name}",
        }
    )


def _job_resource_for_region(region: str, job_name: str) -> str:
    """Return live YAML for ``job_name`` from one explicit regional cluster."""
    if not is_valid_region(region):
        return json.dumps({"error": "invalid region", "value": region})
    if not _JOB_NAME_RE.fullmatch(job_name):
        return json.dumps(
            {
                "error": "invalid job_name",
                "detail": "must match ^[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?$",
                "value": job_name,
            }
        )
    try:
        context_arn = eks_context_for_region(region)
    except Exception as exc:  # AWS credential/session failures become resource errors
        return json.dumps({"error": "unable to resolve EKS context", "detail": str(exc)[:200]})
    try:
        result = cli_runner.subprocess.run(  # type: ignore[attr-defined] # nosemgrep: dangerous-subprocess-use-audit - shell=False; caller input is validated and the context ARN is constructed internally
            [
                "kubectl",
                "get",
                "job",
                job_name,
                "-n",
                _DEFAULT_NAMESPACE,
                "-o",
                "yaml",
                "--context",
                context_arn,
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
    """Register regional live job-state resources and the fail-closed legacy URI."""
    mcp_instance.resource("gco://jobs/{job_name}")(_job_resource)
    mcp_instance.resource("gco://jobs/{region}/{job_name}")(_job_resource_for_region)
