#!/usr/bin/env python3
"""
GCO MCP Server — Exposes the GCO CLI as MCP tools for LLM interaction.

Run with:
    python gco_mcp/run_mcp.py

Add to Kiro MCP config (.kiro/settings/mcp.json):
    {
        "mcpServers": {
            "gco": {
                "command": "python3",
                "args": ["gco_mcp/run_mcp.py"],
                "cwd": "/path/to/GCO"
            }
        }
    }

This file is a thin entrypoint. The actual implementation lives in the
``gco_mcp/`` directory:

    gco_mcp/
    ├── server.py           — FastMCP instance and instructions
    ├── audit.py            — Audit logging, sanitization, decorator
    ├── iam.py              — IAM role assumption
    ├── cli_runner.py       — _run_cli() subprocess wrapper
    ├── version.py          — Project version management
    ├── tools/              — MCP tool definitions (one file per domain)
    │   ├── jobs.py
    │   ├── capacity.py
    │   ├── inference.py
    │   ├── costs.py
    │   ├── stacks.py
    │   ├── storage.py
    │   └── models.py
    └── resources/          — MCP resource definitions (one file per scheme)
        ├── docs.py         — docs:// (documentation + examples with metadata)
        ├── source.py       — source:// (full source code browser)
        ├── k8s.py          — k8s:// (cluster manifests)
        ├── iam_policies.py — iam:// (IAM policy templates)
        ├── infra.py        — infra:// (Dockerfiles, Helm, CI/CD)
        ├── ci.py           — ci:// (GitHub Actions, workflows)
        ├── demos.py        — demos:// (walkthroughs, scripts)
        ├── clients.py      — clients:// (API client examples)
        ├── scripts.py      — scripts:// (utility scripts)
        ├── tests.py        — tests:// (test suite docs and patterns)
        └── config.py       — config:// (CDK config, feature toggles, env vars)
"""

import sys
from pathlib import Path

# Ensure the project root is on the path so CLI modules can be imported
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Ensure the gco_mcp/ directory is on the path so internal modules can import
# each other without a package prefix (avoids shadowing the ``mcp`` PyPI
# package that fastmcp depends on).
MCP_DIR = Path(__file__).parent
sys.path.insert(0, str(MCP_DIR))

# --- Re-export everything the existing tests expect on ``run_mcp.*`` ---

from audit import (  # noqa: E402, F401
    _MCP_SERVER_VERSION,
    _sanitize_arguments,
    audit_logged,
    audit_logger,
    emit_startup_log,
)
from iam import assume_mcp_role  # noqa: E402, F401
from server import mcp  # noqa: E402, F401
from version import get_project_version  # noqa: E402, F401

# Re-export the project version for tests that check run_mcp._PROJECT_VERSION
_PROJECT_VERSION = get_project_version()

# --- Register all tools and resources ---

from resources import register_all_resources  # noqa: E402
from tools import register_all_tools  # noqa: E402

register_all_tools()
register_all_resources()

# --- Re-export tool functions for backward compat with existing tests ---
# Tests call e.g. run_mcp.list_jobs(), so we import them into this namespace.

# Conditionally re-export reserve_capacity if it was registered.
# contextlib.suppress is the idiomatic "swallow this exception" form.
import contextlib as _contextlib  # noqa: E402
import importlib as _importlib  # noqa: E402
import os as _os  # noqa: E402

from tools.analytics import (  # noqa: E402, F401
    analytics_doctor,
    analytics_login_url,
    analytics_user_add,
    analytics_users_list,
    disable_analytics,
    enable_analytics,
)
from tools.capacity import (  # noqa: E402, F401
    ai_recommend,
    capacity_status,
    check_capacity,
    find_capacity_blocks,
    list_reservations,
    recommend_region,
    reservation_check,
    spot_prices,
)
from tools.config import config_get  # noqa: E402, F401
from tools.costs import cost_by_region, cost_forecast, cost_summary, cost_trend  # noqa: E402, F401
from tools.dag import dag_run, dag_validate  # noqa: E402, F401
from tools.docs import find_docs  # noqa: E402, F401
from tools.examples import find_examples  # noqa: E402, F401
from tools.images import (  # noqa: E402, F401
    images_describe,
    images_init,
    images_lifecycle_get,
    images_lifecycle_set,
    images_list,
    images_mirror_plan,
    images_mirror_status,
    images_orphans,
    images_replication_get,
    images_replication_status,
    images_replication_sync,
    images_tags,
    images_uri,
)
from tools.inference import (  # noqa: E402, F401
    canary_deploy,
    chat_inference,
    deploy_inference,
    inference_health,
    inference_status,
    invoke_inference,
    list_endpoint_models,
    list_inference_endpoints,
    promote_canary,
    rollback_canary,
    scale_inference,
    start_inference,
    stop_inference,
    update_inference_image,
)
from tools.jobs import (  # noqa: E402, F401
    cluster_health,
    get_job,
    get_job_events,
    get_job_logs,
    list_jobs,
    queue_status,
    submit_job_api,
    submit_job_sqs,
)
from tools.models import get_model_uri, list_models  # noqa: E402, F401
from tools.nodepools import (  # noqa: E402, F401
    nodepools_create_odcr,
    nodepools_describe,
    nodepools_list,
)
from tools.queue import queue_get, queue_list, queue_stats, queue_submit  # noqa: E402, F401
from tools.stacks import (  # noqa: E402, F401
    aurora_status,
    disable_aurora,
    disable_fsx,
    disable_valkey,
    enable_aurora,
    enable_fsx,
    enable_valkey,
    fsx_status,
    list_stacks,
    setup_cluster_access,
    stack_diff,
    stack_outputs,
    stack_status,
    stack_synth,
    valkey_status,
)
from tools.storage import (  # noqa: E402, F401
    files_access_points,
    files_get,
    list_file_systems,
    list_storage_contents,
)
from tools.tasks import task_status, task_tail  # noqa: E402, F401
from tools.templates import (  # noqa: E402, F401
    templates_create,
    templates_get,
    templates_list,
    templates_run,
)
from tools.webhooks import webhooks_create, webhooks_get, webhooks_list  # noqa: E402, F401

with _contextlib.suppress(ImportError):
    from tools.capacity import reserve_capacity  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.images import images_build, images_mirror, images_push  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.images import (  # noqa: F401
        images_cleanup,
        images_delete_repo,
        images_delete_tag,
        images_prune,
    )

with _contextlib.suppress(ImportError):
    from tools.stacks import bootstrap_cdk, deploy_all, deploy_stack  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.stacks import destroy_all, destroy_stack  # noqa: F401

# Destructive-operations gated tools — present only when
# GCO_ENABLE_DESTRUCTIVE_OPERATIONS (or GCO_ENABLE_ALL_TOOLS) is set.
with _contextlib.suppress(ImportError):
    from tools.jobs import delete_job  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.inference import delete_inference  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.templates import delete_template  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.webhooks import delete_webhook  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.models import delete_model  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.nodepools import delete_nodepool  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.analytics import analytics_user_remove  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.queue import cancel_queue_job  # noqa: F401

# Model-upload gated tool — present only when GCO_ENABLE_MODEL_UPLOAD
# (or GCO_ENABLE_ALL_TOOLS) is set.
with _contextlib.suppress(ImportError):
    from tools.models import models_upload  # noqa: F401

# Also make reserve_capacity available after module reload (tests use
# importlib.reload with GCO_ENABLE_CAPACITY_PURCHASE=true). The umbrella
# flag GCO_ENABLE_ALL_TOOLS is also honoured here so the per-flag and the
# umbrella both yield the same module-level rebinds.
if (
    _os.environ.get("GCO_ENABLE_CAPACITY_PURCHASE", "").lower() == "true"
    or _os.environ.get("GCO_ENABLE_ALL_TOOLS", "").lower() == "true"
):
    from tools import capacity as _cap_mod  # noqa: E402

    _importlib.reload(_cap_mod)
    if hasattr(_cap_mod, "reserve_capacity"):
        reserve_capacity = _cap_mod.reserve_capacity  # noqa: F811

# Reload tools.images when image-publish or destructive flags are set so
# the gated build/push/delete tools are present after a test
# ``importlib.reload(run_mcp)`` cycle. Mirrors the reserve_capacity pattern.
if (
    _os.environ.get("GCO_ENABLE_IMAGE_PUBLISH", "").lower() == "true"
    or _os.environ.get("GCO_ENABLE_DESTRUCTIVE_OPERATIONS", "").lower() == "true"
    or _os.environ.get("GCO_ENABLE_ALL_TOOLS", "").lower() == "true"
):
    from tools import images as _img_mod  # noqa: E402

    _importlib.reload(_img_mod)
    for _name in (
        "images_build",
        "images_push",
        "images_mirror",
        "images_cleanup",
        "images_prune",
        "images_delete_tag",
        "images_delete_repo",
    ):
        if hasattr(_img_mod, _name):
            globals()[_name] = getattr(_img_mod, _name)

# Reload tools.stacks when either infrastructure flag is set so the
# gated deploy/destroy/bootstrap tools are present after a test
# ``importlib.reload(run_mcp)`` cycle. Mirrors the reserve_capacity pattern.
if (
    _os.environ.get("GCO_ENABLE_INFRASTRUCTURE_DEPLOY", "").lower() == "true"
    or _os.environ.get("GCO_ENABLE_INFRASTRUCTURE_DESTROY", "").lower() == "true"
    or _os.environ.get("GCO_ENABLE_ALL_TOOLS", "").lower() == "true"
):
    from tools import stacks as _stacks_mod  # noqa: E402

    _importlib.reload(_stacks_mod)
    for _name in (
        "deploy_stack",
        "deploy_all",
        "bootstrap_cdk",
        "destroy_stack",
        "destroy_all",
    ):
        if hasattr(_stacks_mod, _name):
            globals()[_name] = getattr(_stacks_mod, _name)

# Destructive-operations and model-upload gated reload blocks — mirror the
# reserve_capacity pattern so flag-driven tests can do ``importlib.reload(
# run_mcp)`` and have the gated names appear as module-level attributes.
_DESTRUCTIVE_FLAG_ON = (
    _os.environ.get("GCO_ENABLE_DESTRUCTIVE_OPERATIONS", "").lower() == "true"
    or _os.environ.get("GCO_ENABLE_ALL_TOOLS", "").lower() == "true"
)
_MODEL_UPLOAD_FLAG_ON = (
    _os.environ.get("GCO_ENABLE_MODEL_UPLOAD", "").lower() == "true"
    or _os.environ.get("GCO_ENABLE_ALL_TOOLS", "").lower() == "true"
)

if _DESTRUCTIVE_FLAG_ON:
    from tools import jobs as _jobs_mod  # noqa: E402

    _importlib.reload(_jobs_mod)
    if hasattr(_jobs_mod, "delete_job"):
        delete_job = _jobs_mod.delete_job  # noqa: F811

    from tools import inference as _inf_mod  # noqa: E402

    _importlib.reload(_inf_mod)
    if hasattr(_inf_mod, "delete_inference"):
        delete_inference = _inf_mod.delete_inference  # noqa: F811

    from tools import templates as _tpl_mod  # noqa: E402

    _importlib.reload(_tpl_mod)
    if hasattr(_tpl_mod, "delete_template"):
        globals()["delete_template"] = _tpl_mod.delete_template

    from tools import webhooks as _wh_mod  # noqa: E402

    _importlib.reload(_wh_mod)
    if hasattr(_wh_mod, "delete_webhook"):
        globals()["delete_webhook"] = _wh_mod.delete_webhook

    from tools import nodepools as _np_mod  # noqa: E402

    _importlib.reload(_np_mod)
    if hasattr(_np_mod, "delete_nodepool"):
        globals()["delete_nodepool"] = _np_mod.delete_nodepool

    from tools import analytics as _an_mod  # noqa: E402

    _importlib.reload(_an_mod)
    if hasattr(_an_mod, "analytics_user_remove"):
        globals()["analytics_user_remove"] = _an_mod.analytics_user_remove

    from tools import queue as _q_mod  # noqa: E402

    _importlib.reload(_q_mod)
    if hasattr(_q_mod, "cancel_queue_job"):
        globals()["cancel_queue_job"] = _q_mod.cancel_queue_job

# tools.models is reloaded if either the destructive flag (delete_model)
# or the model-upload flag (models_upload) is set, so do it once here
# regardless of which (or both) flipped.
if _DESTRUCTIVE_FLAG_ON or _MODEL_UPLOAD_FLAG_ON:
    from tools import models as _models_mod  # noqa: E402

    _importlib.reload(_models_mod)
    for _name in ("delete_model", "models_upload"):
        if hasattr(_models_mod, _name):
            globals()[_name] = getattr(_models_mod, _name)

# --- Re-export resource directory constants for tests ---
from resources.ci import (  # noqa: E402, F401
    GITHUB_ACTIONS_DIR,
    GITHUB_CODEQL_DIR,
    GITHUB_DIR,
    GITHUB_ISSUE_TEMPLATE_DIR,
    GITHUB_KIND_DIR,
    GITHUB_SCRIPTS_DIR,
    GITHUB_WORKFLOWS_DIR,
)
from resources.docs import DOCS_DIR, EXAMPLES_DIR  # noqa: E402, F401
from resources.infra import DOCKERFILES_DIR, HELM_CHARTS_FILE  # noqa: E402, F401
from resources.k8s import MANIFESTS_DIR  # noqa: E402, F401

# Declare every name that is intentionally re-exported for tests and
# downstream consumers. This silences unused-import warnings from static
# analyzers that don't recognise the per-line ruff markers above.
__all__ = [
    "DOCKERFILES_DIR",
    "DOCS_DIR",
    "EXAMPLES_DIR",
    "GITHUB_ACTIONS_DIR",
    "GITHUB_CODEQL_DIR",
    "GITHUB_DIR",
    "GITHUB_ISSUE_TEMPLATE_DIR",
    "GITHUB_KIND_DIR",
    "GITHUB_SCRIPTS_DIR",
    "GITHUB_WORKFLOWS_DIR",
    "HELM_CHARTS_FILE",
    "MANIFESTS_DIR",
    "_MCP_SERVER_VERSION",
    "_PROJECT_VERSION",
    "_sanitize_arguments",
    "ai_recommend",
    "analytics_doctor",
    "analytics_login_url",
    "analytics_user_add",
    "analytics_user_remove",
    "analytics_users_list",
    "assume_mcp_role",
    "audit_logged",
    "audit_logger",
    "aurora_status",
    "bootstrap_cdk",
    "canary_deploy",
    "cancel_queue_job",
    "capacity_status",
    "chat_inference",
    "check_capacity",
    "cluster_health",
    "config_get",
    "cost_by_region",
    "cost_forecast",
    "cost_summary",
    "cost_trend",
    "dag_run",
    "dag_validate",
    "delete_inference",
    "delete_job",
    "delete_model",
    "delete_nodepool",
    "delete_template",
    "delete_webhook",
    "deploy_all",
    "deploy_inference",
    "deploy_stack",
    "destroy_all",
    "destroy_stack",
    "disable_analytics",
    "disable_aurora",
    "disable_fsx",
    "disable_valkey",
    "emit_startup_log",
    "enable_analytics",
    "enable_aurora",
    "enable_fsx",
    "enable_valkey",
    "files_access_points",
    "files_get",
    "find_capacity_blocks",
    "find_docs",
    "find_examples",
    "fsx_status",
    "get_job",
    "get_job_events",
    "get_job_logs",
    "get_model_uri",
    "get_project_version",
    "images_build",
    "images_cleanup",
    "images_delete_repo",
    "images_delete_tag",
    "images_describe",
    "images_init",
    "images_lifecycle_get",
    "images_lifecycle_set",
    "images_list",
    "images_mirror",
    "images_mirror_plan",
    "images_mirror_status",
    "images_orphans",
    "images_prune",
    "images_push",
    "images_replication_get",
    "images_replication_status",
    "images_replication_sync",
    "images_tags",
    "images_uri",
    "inference_health",
    "inference_status",
    "invoke_inference",
    "list_endpoint_models",
    "list_file_systems",
    "list_inference_endpoints",
    "list_jobs",
    "list_models",
    "list_reservations",
    "list_stacks",
    "list_storage_contents",
    "mcp",
    "models_upload",
    "nodepools_create_odcr",
    "nodepools_describe",
    "nodepools_list",
    "promote_canary",
    "queue_get",
    "queue_list",
    "queue_stats",
    "queue_status",
    "queue_submit",
    "recommend_region",
    "reservation_check",
    "rollback_canary",
    "scale_inference",
    "setup_cluster_access",
    "spot_prices",
    "stack_diff",
    "stack_outputs",
    "stack_status",
    "stack_synth",
    "start_inference",
    "stop_inference",
    "submit_job_api",
    "submit_job_sqs",
    "task_status",
    "task_tail",
    "templates_create",
    "templates_get",
    "templates_list",
    "templates_run",
    "update_inference_image",
    "valkey_status",
    "webhooks_create",
    "webhooks_get",
    "webhooks_list",
]

# --- Startup ---

emit_startup_log()

try:
    assume_mcp_role()
except Exception:
    raise

# =============================================================================
# ENTRYPOINT
# =============================================================================


def main() -> None:
    """Entry point for the gco-mcp console script: start the MCP server loop."""
    mcp.run()


if __name__ == "__main__":
    main()
