"""Feature-flag evaluation for the GCO MCP server.

Convention: a flag is enabled if and only if (a) the umbrella flag
GCO_ENABLE_ALL_TOOLS is set to "true", OR (b) the case-insensitive,
whitespace-stripped value of os.environ.get(<flag>, "") equals the
literal "true". Anything else (unset, empty, "false", "0", "yes", "1",
"True\n" without strip, ...) is disabled.

The umbrella flag is mutually inclusive — setting GCO_ENABLE_ALL_TOOLS=true
overrides per-flag values, even per-flag values explicitly set to "false".
"""

import os

# Known flag constants. Tool modules import these by name.
FLAG_ALL_TOOLS = "GCO_ENABLE_ALL_TOOLS"
FLAG_CAPACITY_PURCHASE = "GCO_ENABLE_CAPACITY_PURCHASE"
FLAG_MODEL_UPLOAD = "GCO_ENABLE_MODEL_UPLOAD"
FLAG_IMAGE_PUBLISH = "GCO_ENABLE_IMAGE_PUBLISH"
FLAG_INFRASTRUCTURE_DEPLOY = "GCO_ENABLE_INFRASTRUCTURE_DEPLOY"
FLAG_INFRASTRUCTURE_DESTROY = "GCO_ENABLE_INFRASTRUCTURE_DESTROY"
FLAG_DESTRUCTIVE_OPERATIONS = "GCO_ENABLE_DESTRUCTIVE_OPERATIONS"
FLAG_MISSION = "GCO_ENABLE_MISSION"
# Read-only-but-sensitive metric readers: local-filesystem reads and per-call
# LLM scoring are default-off for security / cost reasons even though they
# never mutate AWS state. They are full members of the registry like any
# other per-tool flag.
FLAG_LOCAL_METRICS = "GCO_ENABLE_LOCAL_METRICS"
# Reads from or writes to the MCP host and can upload data to S3.
FLAG_LOCAL_STORAGE_SYNC = "GCO_ENABLE_LOCAL_STORAGE_SYNC"
FLAG_SEMANTIC_PROGRESS = "GCO_ENABLE_SEMANTIC_PROGRESS"
# Writes to the deployment configuration (cdk.json) on the MCP host —
# validated, atomic, and audited via the managed-config engine, but still a
# local-file mutation an agent could chain into a deploy, so default-off.
FLAG_CONFIG_MANAGEMENT = "GCO_ENABLE_CONFIG_MANAGEMENT"

# Per-tool flags. The umbrella is intentionally not in this tuple — callers
# iterating ALL_FLAGS for "what gates this tool?" lookups should not see
# the umbrella, only the per-tool flags.
ALL_FLAGS = (
    FLAG_CAPACITY_PURCHASE,
    FLAG_MODEL_UPLOAD,
    FLAG_IMAGE_PUBLISH,
    FLAG_INFRASTRUCTURE_DEPLOY,
    FLAG_INFRASTRUCTURE_DESTROY,
    FLAG_DESTRUCTIVE_OPERATIONS,
    FLAG_MISSION,
    FLAG_LOCAL_METRICS,
    FLAG_LOCAL_STORAGE_SYNC,
    FLAG_SEMANTIC_PROGRESS,
    FLAG_CONFIG_MANAGEMENT,
)


def _raw(flag_name: str) -> bool:
    """Return True iff the env var equals literal "true" (case-insensitive, stripped)."""
    return os.environ.get(flag_name, "").strip().lower() == "true"


def is_enabled(flag_name: str) -> bool:
    """Return True iff the umbrella flag is set OR the named flag is set."""
    return _raw(FLAG_ALL_TOOLS) or _raw(flag_name)


def all_tools_enabled() -> bool:
    """Return True iff GCO_ENABLE_ALL_TOOLS is set. Used by emit_startup_log."""
    return _raw(FLAG_ALL_TOOLS)
