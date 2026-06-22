"""
FastMCP server instance and instructions for the GCO MCP server.

This module creates the shared ``mcp`` FastMCP instance that all tool and
resource modules register against. Import ``mcp`` from here — never create
a second instance.
"""

import os

from fastmcp import FastMCP

# Code Mode lives under fastmcp.experimental — the import path itself signals
# the API can move between minor versions. The fastmcp pin in pyproject.toml is
# intentionally an `==` to keep that surface stable for a release.
from fastmcp.experimental.transforms.code_mode import (
    CodeMode,
    GetSchemas,
    GetTags,
    MontySandboxProvider,
    Search,
)
from fastmcp.server.transforms import ResourcesAsTools
from fastmcp.server.transforms.search import BM25SearchTransform, RegexSearchTransform

mcp = FastMCP(
    "GCO",
    instructions=(
        "Multi-region EKS Auto Mode platform for AI/ML workload orchestration. "
        "Submit jobs, manage inference endpoints, check capacity, track costs, "
        "and manage infrastructure across AWS regions.\n\n"
        "Resources available:\n"
        "- docs:// — Documentation, architecture guides, and example job/inference manifests\n"
        "- k8s:// — Kubernetes manifests deployed to the cluster (RBAC, deployments, NodePools, etc.)\n"
        "- iam:// — IAM policy templates for access control\n"
        "- infra:// — Dockerfiles, Helm charts, CI/CD config\n"
        "- ci:// — GitHub Actions workflows, composite actions, scripts, issue/PR templates\n"
        "- source:// — Full source code of the platform\n"
        "- demos:// — Demo walkthroughs, live demo scripts, and presentation materials\n"
        "- clients:// — API client examples (Python, curl, AWS CLI)\n"
        "- scripts:// — Utility scripts for cluster access, versioning, testing\n"
        "- tests:// — Test suite documentation, patterns, and configuration\n"
        "- config:// — CDK configuration schema, feature toggles, and environment variables\n\n"
        "Start with docs://gco/index or k8s://gco/manifests/index to explore."
    ),
    # NOTE on background-task support: ``tasks=True`` is intentionally NOT set
    # here. FastMCP's ``tasks=True`` at the server level applies a default
    # ``TaskConfig(mode="optional")`` to every tool, which requires every tool
    # function to be async (FastMCP raises ValueError at registration time
    # otherwise). The async migration of existing sync tools lands in a
    # later phase; until then, the long-running tools that genuinely need
    # background-task support set ``task=TaskConfig(mode=...)`` on their
    # individual ``@mcp.tool(...)`` decorators rather than relying on the
    # server-wide default. The ``fastmcp[tasks]`` extra is still pulled in
    # via ``pyproject.toml`` so pydocket is available when those per-tool
    # decorators run.
)

# Always-on: tool-only clients (Cursor) get list_resources/read_resource synthetic tools.
# Registered AFTER the catalog-replacement transform below so the synthetic
# resource tools survive even when BM25/Regex/Code Mode replace the catalog.


def _int_env(name: str, default: int) -> int:
    """Parse an integer env var; fall back to default on missing/empty/non-numeric."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    """Parse a float env var; fall back to default on missing/empty/non-numeric."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Catalog-replacement transform. Mutually exclusive between the four values.
# Default is "bm25" so a brand-new install gets relevance-ranked tool search
# without any extra configuration. An unknown value (typo, etc.) also falls
# back to "bm25" so a misconfigured client doesn't accidentally drop into the
# full-catalog listing.
_TOOL_SEARCH = os.environ.get("GCO_MCP_TOOL_SEARCH", "bm25").strip().lower()
_ALWAYS_VISIBLE = [
    "find_examples",
    "find_docs",
    "list_jobs",
    "submit_job_sqs",
    "list_inference_endpoints",
    "task_status",
]
if _TOOL_SEARCH == "bm25":
    mcp.add_transform(BM25SearchTransform(always_visible=_ALWAYS_VISIBLE))
elif _TOOL_SEARCH == "regex":
    mcp.add_transform(RegexSearchTransform(always_visible=_ALWAYS_VISIBLE))
elif _TOOL_SEARCH == "code_mode":
    # Four-stage discovery: GetTags → Search → GetSchemas → execute. Tags are
    # mandatory on every tool, so GetTags as the first stage gives the LLM
    # cheap browse-by-category before searching.
    mcp.add_transform(
        CodeMode(
            discovery_tools=[GetTags(), Search(), GetSchemas()],
            sandbox_provider=MontySandboxProvider(
                limits={
                    "max_duration_secs": _float_env("GCO_MCP_CODE_MODE_MAX_DURATION_SECS", 30.0),
                    "max_memory": _int_env("GCO_MCP_CODE_MODE_MAX_MEMORY", 200_000_000),
                },
            ),
        )
    )
elif _TOOL_SEARCH == "off":
    pass  # legacy: list_tools returns the full catalog
else:
    # Unknown value → behave as the default (bm25).
    mcp.add_transform(BM25SearchTransform(always_visible=_ALWAYS_VISIBLE))


# Resources As Tools is added AFTER the catalog-replacement transform so its
# synthetic ``list_resources`` / ``read_resource`` tools are appended to the
# catalog the search transform produced. Tool-only clients (Cursor, etc.)
# always see the resource surface even under search-mode.
mcp.add_transform(ResourcesAsTools(mcp))


# Audit-capture middleware. Installs once after the transforms so every
# tool invocation gets fresh per-call buffers for ctx.warning/info/error
# and ctx.elicit. The patched Context methods are a no-op outside an
# active middleware scope, so this has no effect on non-MCP callers.
from audit_middleware import AuditCaptureMiddleware  # noqa: E402

mcp.add_middleware(AuditCaptureMiddleware())
