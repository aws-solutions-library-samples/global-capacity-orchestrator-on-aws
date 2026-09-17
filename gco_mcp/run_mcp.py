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

This file is a thin entrypoint. The implementation lives under
``gco_mcp/``; the two package registries are the authoritative module lists:

    gco_mcp/
    ├── server.py           — FastMCP singleton, transforms, and middleware
    ├── feature_flags.py    — Environment-driven tool gates
    ├── audit.py            — Structured tool/resource/startup audit logging
    ├── audit_middleware.py — Per-request message and elicitation capture
    ├── iam.py              — Optional startup role assumption
    ├── cli_runner.py       — Bounded gco CLI subprocess wrapper
    ├── local_data.py       — Confined local-path and snapshot helpers
    ├── mission/            — Goal-directed Mission engine
    ├── metric_readers/     — Canonical metric-source adapters
    ├── mission_judge/      — Semantic-progress scorer
    ├── tools/
    │   ├── __init__.py     — Registers all tool-domain modules
    │   └── README.md       — Complete per-module and per-tool catalog
    └── resources/
        ├── __init__.py     — Registers every static/live resource module
        └── README.md       — Complete URI-family and module catalog
"""

import sys
from pathlib import Path

# The file is supported both as ``python gco_mcp/run_mcp.py`` and as
# ``import gco_mcp.run_mcp``. Alias the two names before importing the shared
# server so either route observes one module and one FastMCP singleton.
_THIS_MODULE = sys.modules[__name__]
if __name__ in {"run_mcp", "__main__"}:
    sys.modules.setdefault("gco_mcp.run_mcp", _THIS_MODULE)
if __name__ in {"gco_mcp.run_mcp", "__main__"}:
    sys.modules.setdefault("run_mcp", _THIS_MODULE)

# ``importlib.reload`` retains a module's globals. Record whether this is an
# explicit same-process reload so compatibility rebinds do not re-register
# every flagged tool during a normal, clean server startup.
_IS_RELOAD = bool(getattr(_THIS_MODULE, "_RUN_MCP_IMPORT_COMPLETE", False))

# Direct script execution starts with only gco_mcp/ on sys.path. Add each
# required root at most once; package imports need no mutation.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = Path(__file__).resolve().parent
for _path in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

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
if _IS_RELOAD:
    # Static resources already live on the shared FastMCP singleton. Mission
    # resources are the sole flag-gated family and may need to appear after a
    # test or embedding process deliberately changes flags and reloads us.
    from resources import mission as _mission_resources

    _mission_resources.register(mcp)
else:
    register_all_resources()

# Argument completion (FastMCP 4): answers ``completion/complete`` for the
# static registry-backed resource templates. Registered after the resource
# modules so the handler's providers read fully-populated registries;
# re-registration on reload just replaces the handler.
from completions import register_completions  # noqa: E402

register_completions(mcp)

# --- Re-export tool functions for backward compat with existing tests ---
# Tests call e.g. run_mcp.list_jobs(), so we import them into this namespace.

# Conditionally re-export reserve_capacity if it was registered.
# contextlib.suppress is the idiomatic "swallow this exception" form.
import contextlib as _contextlib  # noqa: E402
import importlib as _importlib  # noqa: E402

import feature_flags as _feature_flags  # noqa: E402
from tools.analytics import (  # noqa: E402, F401
    analytics_doctor,
    analytics_login_url,
    analytics_status,
    analytics_user_add,
    analytics_users_list,
    disable_analytics,
    enable_analytics,
)
from tools.capacity import (  # noqa: E402, F401
    ai_recommend,
    capacity_history_patterns,
    capacity_history_show,
    capacity_history_stats,
    capacity_predict,
    capacity_status,
    check_capacity,
    find_capacity_blocks,
    find_capacity_reservations,
    instance_info,
    list_reservations,
    recommend_capacity,
    recommend_region,
    reservation_check,
    spot_prices,
)
from tools.cluster import cluster_tunnel_command  # noqa: E402, F401
from tools.config import config_get  # noqa: E402, F401
from tools.costs import (  # noqa: E402, F401
    cost_allocation_activate,
    cost_allocation_status,
    cost_by_region,
    cost_forecast,
    cost_k8s_namespaces,
    cost_k8s_regions,
    cost_k8s_top,
    cost_k8s_trend,
    cost_report_generate,
    cost_report_list,
    cost_report_status,
    cost_summary,
    cost_trend,
    cost_workloads,
)
from tools.dag import dag_run, dag_validate  # noqa: E402, F401
from tools.deps import deps_scan  # noqa: E402, F401
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
    configure_mooncake_store,
    deploy_disaggregated_inference,
    deploy_inference,
    inference_health,
    inference_status,
    invoke_inference,
    list_endpoint_models,
    list_inference_endpoints,
    mooncake_topology_status,
    populate_kv_cache,
    promote_canary,
    rollback_canary,
    scale_inference,
    set_mooncake_topology,
    start_inference,
    stop_inference,
    update_inference_image,
)
from tools.jobs import (  # noqa: E402, F401
    check_job_policy,
    cluster_health,
    get_job,
    get_job_events,
    get_job_logs,
    get_job_metrics,
    get_job_pods,
    get_job_validation_policy,
    get_pod_logs,
    list_jobs,
    queue_status,
    retry_job,
    submit_job_api,
    submit_job_sqs,
)
from tools.metrics import (  # noqa: E402, F401
    metrics_cloudwatch_get,
    metrics_from_job_logs,
    metrics_from_shared_storage_file,
)
from tools.models import get_model_uri, list_models  # noqa: E402, F401
from tools.monitoring import (  # noqa: E402, F401
    disable_monitoring,
    enable_monitoring,
    monitoring_status,
    monitoring_user_add,
    monitoring_users_list,
)
from tools.nodepools import (  # noqa: E402, F401
    nodepools_create_capacity_block,
    nodepools_create_odcr,
    nodepools_describe,
    nodepools_list,
)
from tools.queue import queue_get, queue_list, queue_stats, queue_submit  # noqa: E402, F401
from tools.stacks import (  # noqa: E402, F401
    addons_status,
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
from tools.status import fleet_status  # noqa: E402, F401
from tools.storage import (  # noqa: E402, F401
    files_access_points,
    files_get,
    list_file_systems,
    list_storage_buckets,
    list_storage_contents,
    s3_inventory,
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
    from tools.capacity import create_reservation, reserve_capacity  # noqa: F401

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
    from tools.stacks import addons_install, bootstrap_cdk, deploy_all, deploy_stack  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.stacks import destroy_all, destroy_stack  # noqa: F401

# Config-management gated tools — present only when
# GCO_ENABLE_CONFIG_MANAGEMENT (or GCO_ENABLE_ALL_TOOLS) is set.
with _contextlib.suppress(ImportError):
    from tools.stacks import (  # noqa: F401
        add_deployment_region,
        list_deployment_regions,
        remove_deployment_region,
        set_capacity_advisor_default_model,
        set_claude_code_default_model,
        set_codex_default_model,
        set_codex_reasoning_effort,
        set_deployment_region,
        set_eks_endpoint_access,
        set_mission_default_model,
    )

# Destructive-operations gated tools — present only when
# GCO_ENABLE_DESTRUCTIVE_OPERATIONS (or GCO_ENABLE_ALL_TOOLS) is set.
with _contextlib.suppress(ImportError):
    from tools.capacity import cancel_reservation  # noqa: F401

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
    from tools.monitoring import monitoring_user_remove  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.queue import cancel_queue_job  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.tasks import task_prune  # noqa: F401

# Model-upload gated tool — present only when GCO_ENABLE_MODEL_UPLOAD
# (or GCO_ENABLE_ALL_TOOLS) is set.
with _contextlib.suppress(ImportError):
    from tools.models import models_upload  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.storage import upload_to_regional_bucket  # noqa: F401

# Local-metrics, local-storage, semantic-progress, and Mission tools also use
# import-time gates. The imports are no-ops when disabled; the explicit reload
# compatibility blocks below rebind them only during ``importlib.reload``.
with _contextlib.suppress(ImportError):
    from tools.metrics import metrics_from_local_file  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.storage import sync_storage_bucket  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.semantic_progress import metrics_semantic_progress  # noqa: F401

with _contextlib.suppress(ImportError):
    from tools.mission import (  # noqa: F401
        mission_abort,
        mission_checkpoint,
        mission_complete,
        mission_history,
        mission_iterate,
        mission_list,
        mission_memory_search,
        mission_resume,
        mission_start,
        mission_status,
    )

with _contextlib.suppress(ImportError):
    from tools.swarm import (  # noqa: F401
        swarm_abort,
        swarm_iterate,
        swarm_list,
        swarm_plan,
        swarm_start,
        swarm_status,
    )

# Explicit reload compatibility for the two gated families in capacity.py.
# A clean startup has just imported the module under the final environment and
# must not reload it: doing so used to emit duplicate-component warnings for
# every unconditional capacity tool.
if _IS_RELOAD and (
    _feature_flags.is_enabled(_feature_flags.FLAG_CAPACITY_PURCHASE)
    or _feature_flags.is_enabled(_feature_flags.FLAG_DESTRUCTIVE_OPERATIONS)
):
    from tools import capacity as _cap_mod  # noqa: E402

    _importlib.reload(_cap_mod)
    for _name in ("reserve_capacity", "create_reservation", "cancel_reservation"):
        if hasattr(_cap_mod, _name):
            globals()[_name] = getattr(_cap_mod, _name)

# Reload tools.images when image-publish or destructive flags are set so
# the gated build/push/delete tools are present after a test
# ``importlib.reload(run_mcp)`` cycle. Mirrors the reserve_capacity pattern.
if _IS_RELOAD and (
    _feature_flags.is_enabled(_feature_flags.FLAG_IMAGE_PUBLISH)
    or _feature_flags.is_enabled(_feature_flags.FLAG_DESTRUCTIVE_OPERATIONS)
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

# Reload tools.stacks when an infrastructure flag or the managed-config
# flag is set so the gated deploy/destroy/bootstrap and deployment-region
# tools are present after a test ``importlib.reload(run_mcp)`` cycle.
# Mirrors the reserve_capacity pattern.
if _IS_RELOAD and (
    _feature_flags.is_enabled(_feature_flags.FLAG_INFRASTRUCTURE_DEPLOY)
    or _feature_flags.is_enabled(_feature_flags.FLAG_INFRASTRUCTURE_DESTROY)
    or _feature_flags.is_enabled(_feature_flags.FLAG_CONFIG_MANAGEMENT)
):
    from tools import stacks as _stacks_mod  # noqa: E402

    _importlib.reload(_stacks_mod)
    for _name in (
        "deploy_stack",
        "deploy_all",
        "bootstrap_cdk",
        "addons_install",
        "destroy_stack",
        "destroy_all",
        "list_deployment_regions",
        "add_deployment_region",
        "remove_deployment_region",
        "set_deployment_region",
        "set_eks_endpoint_access",
        "set_mission_default_model",
        "set_capacity_advisor_default_model",
        "set_claude_code_default_model",
        "set_codex_default_model",
        "set_codex_reasoning_effort",
    ):
        if hasattr(_stacks_mod, _name):
            globals()[_name] = getattr(_stacks_mod, _name)

# Destructive-operations and model-upload gated reload blocks — mirror the
# reserve_capacity pattern so flag-driven tests can do ``importlib.reload(
# run_mcp)`` and have the gated names appear as module-level attributes.
_DESTRUCTIVE_FLAG_ON = _feature_flags.is_enabled(_feature_flags.FLAG_DESTRUCTIVE_OPERATIONS)
_MODEL_UPLOAD_FLAG_ON = _feature_flags.is_enabled(_feature_flags.FLAG_MODEL_UPLOAD)

if _IS_RELOAD and _DESTRUCTIVE_FLAG_ON:
    from tools import jobs as _jobs_mod  # noqa: E402

    _importlib.reload(_jobs_mod)
    delete_job = _jobs_mod.delete_job  # noqa: F811

    from tools import inference as _inf_mod  # noqa: E402

    _importlib.reload(_inf_mod)
    delete_inference = _inf_mod.delete_inference  # noqa: F811

    from tools import templates as _tpl_mod  # noqa: E402

    _importlib.reload(_tpl_mod)
    globals()["delete_template"] = _tpl_mod.delete_template

    from tools import webhooks as _wh_mod  # noqa: E402

    _importlib.reload(_wh_mod)
    globals()["delete_webhook"] = _wh_mod.delete_webhook

    from tools import nodepools as _np_mod  # noqa: E402

    _importlib.reload(_np_mod)
    globals()["delete_nodepool"] = _np_mod.delete_nodepool

    from tools import analytics as _an_mod  # noqa: E402

    _importlib.reload(_an_mod)
    globals()["analytics_user_remove"] = _an_mod.analytics_user_remove

    from tools import queue as _q_mod  # noqa: E402

    _importlib.reload(_q_mod)
    globals()["cancel_queue_job"] = _q_mod.cancel_queue_job

    from tools import monitoring as _mon_mod  # noqa: E402

    _importlib.reload(_mon_mod)
    globals()["monitoring_user_remove"] = _mon_mod.monitoring_user_remove

    from tools import tasks as _tasks_mod  # noqa: E402

    _importlib.reload(_tasks_mod)
    globals()["task_prune"] = _tasks_mod.task_prune

# tools.models is reloaded if either the destructive flag (delete_model)
# or the model-upload flag (models_upload) is set, so do it once here
# regardless of which (or both) flipped.
if _IS_RELOAD and (_DESTRUCTIVE_FLAG_ON or _MODEL_UPLOAD_FLAG_ON):
    from tools import models as _models_mod  # noqa: E402

    _importlib.reload(_models_mod)
    for _name in ("delete_model", "models_upload"):
        if hasattr(_models_mod, _name):
            globals()[_name] = getattr(_models_mod, _name)

if _IS_RELOAD and (
    _MODEL_UPLOAD_FLAG_ON or _feature_flags.is_enabled(_feature_flags.FLAG_LOCAL_STORAGE_SYNC)
):
    from tools import storage as _storage_mod  # noqa: E402

    _importlib.reload(_storage_mod)
    for _name in ("upload_to_regional_bucket", "sync_storage_bucket"):
        if hasattr(_storage_mod, _name):
            globals()[_name] = getattr(_storage_mod, _name)

if _IS_RELOAD and _feature_flags.is_enabled(_feature_flags.FLAG_LOCAL_METRICS):
    from tools import metrics as _metrics_mod  # noqa: E402

    _importlib.reload(_metrics_mod)
    metrics_from_local_file = _metrics_mod.metrics_from_local_file

if _IS_RELOAD and _feature_flags.is_enabled(_feature_flags.FLAG_SEMANTIC_PROGRESS):
    from tools import semantic_progress as _semantic_progress_mod  # noqa: E402

    _importlib.reload(_semantic_progress_mod)
    metrics_semantic_progress = _semantic_progress_mod.metrics_semantic_progress

if _IS_RELOAD and _feature_flags.is_enabled(_feature_flags.FLAG_MISSION):
    from tools import mission as _mission_tools_mod  # noqa: E402

    _importlib.reload(_mission_tools_mod)
    # Unlike the images/models/storage reload blocks above, every name here is
    # defined under the exact same `is_enabled(FLAG_MISSION)` gate this block
    # is itself conditioned on, so a `hasattr` guard would never see a miss —
    # it is a straight rebind.
    for _name in (
        "mission_start",
        "mission_status",
        "mission_iterate",
        "mission_checkpoint",
        "mission_complete",
        "mission_abort",
        "mission_resume",
        "mission_history",
        "mission_list",
        "mission_memory_search",
    ):
        globals()[_name] = getattr(_mission_tools_mod, _name)

if _IS_RELOAD and _feature_flags.is_enabled(_feature_flags.FLAG_SWARM):
    from tools import swarm as _swarm_tools_mod  # noqa: E402

    _importlib.reload(_swarm_tools_mod)
    # Same reasoning as the mission block above: every name is defined under
    # this same `is_enabled(FLAG_SWARM)` gate, so `hasattr` cannot miss.
    for _name in (
        "swarm_start",
        "swarm_iterate",
        "swarm_status",
        "swarm_abort",
        "swarm_list",
        "swarm_plan",
    ):
        globals()[_name] = getattr(_swarm_tools_mod, _name)

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
from resources.self import _TOOL_GATING_TABLE  # noqa: E402

# Declare every candidate name that is intentionally re-exported for tests and
# downstream consumers. The final ``__all__`` below filters gated names against
# both the current environment and actual module globals, so ``from run_mcp
# import *`` never advertises an unavailable attribute.
_PUBLIC_EXPORTS = [
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
    "add_deployment_region",
    "addons_install",
    "addons_status",
    "ai_recommend",
    "analytics_doctor",
    "analytics_login_url",
    "analytics_status",
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
    "cancel_reservation",
    "capacity_history_patterns",
    "capacity_history_show",
    "capacity_history_stats",
    "capacity_predict",
    "capacity_status",
    "chat_inference",
    "check_capacity",
    "check_job_policy",
    "cluster_health",
    "cluster_tunnel_command",
    "config_get",
    "configure_mooncake_store",
    "cost_allocation_activate",
    "cost_allocation_status",
    "cost_by_region",
    "cost_forecast",
    "cost_k8s_namespaces",
    "cost_k8s_regions",
    "cost_k8s_top",
    "cost_k8s_trend",
    "cost_report_generate",
    "cost_report_list",
    "cost_report_status",
    "cost_summary",
    "cost_trend",
    "cost_workloads",
    "create_reservation",
    "dag_run",
    "dag_validate",
    "delete_inference",
    "delete_job",
    "delete_model",
    "delete_nodepool",
    "delete_template",
    "delete_webhook",
    "deploy_all",
    "deploy_disaggregated_inference",
    "deploy_inference",
    "deploy_stack",
    "deps_scan",
    "destroy_all",
    "destroy_stack",
    "disable_analytics",
    "disable_aurora",
    "disable_fsx",
    "disable_monitoring",
    "disable_valkey",
    "emit_startup_log",
    "enable_analytics",
    "enable_aurora",
    "enable_fsx",
    "enable_monitoring",
    "enable_valkey",
    "files_access_points",
    "files_get",
    "find_capacity_blocks",
    "find_capacity_reservations",
    "find_docs",
    "find_examples",
    "fleet_status",
    "fsx_status",
    "get_job",
    "get_job_events",
    "get_job_logs",
    "get_job_metrics",
    "get_job_pods",
    "get_job_validation_policy",
    "get_model_uri",
    "get_pod_logs",
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
    "instance_info",
    "invoke_inference",
    "list_deployment_regions",
    "list_endpoint_models",
    "list_file_systems",
    "list_inference_endpoints",
    "list_jobs",
    "list_models",
    "list_reservations",
    "list_stacks",
    "list_storage_buckets",
    "list_storage_contents",
    "mcp",
    "metrics_cloudwatch_get",
    "metrics_from_job_logs",
    "metrics_from_local_file",
    "metrics_from_shared_storage_file",
    "metrics_semantic_progress",
    "mission_abort",
    "mission_checkpoint",
    "mission_complete",
    "mission_history",
    "mission_iterate",
    "mission_list",
    "mission_memory_search",
    "mission_resume",
    "mission_start",
    "mission_status",
    "models_upload",
    "monitoring_status",
    "monitoring_user_add",
    "monitoring_user_remove",
    "monitoring_users_list",
    "mooncake_topology_status",
    "nodepools_create_capacity_block",
    "nodepools_create_odcr",
    "nodepools_describe",
    "nodepools_list",
    "populate_kv_cache",
    "promote_canary",
    "queue_get",
    "queue_list",
    "queue_stats",
    "queue_status",
    "queue_submit",
    "recommend_capacity",
    "recommend_region",
    "remove_deployment_region",
    "reservation_check",
    "reserve_capacity",
    "retry_job",
    "rollback_canary",
    "s3_inventory",
    "scale_inference",
    "set_capacity_advisor_default_model",
    "set_claude_code_default_model",
    "set_codex_default_model",
    "set_codex_reasoning_effort",
    "set_deployment_region",
    "set_eks_endpoint_access",
    "set_mission_default_model",
    "set_mooncake_topology",
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
    "swarm_abort",
    "swarm_iterate",
    "swarm_list",
    "swarm_plan",
    "swarm_start",
    "swarm_status",
    "sync_storage_bucket",
    "task_prune",
    "task_status",
    "task_tail",
    "templates_create",
    "templates_get",
    "templates_list",
    "templates_run",
    "update_inference_image",
    "upload_to_regional_bucket",
    "valkey_status",
    "webhooks_create",
    "webhooks_get",
    "webhooks_list",
]

__all__ = [
    name
    for name in _PUBLIC_EXPORTS
    if name in globals()
    and (name not in _TOOL_GATING_TABLE or _feature_flags.is_enabled(_TOOL_GATING_TABLE[name]))
]

# =============================================================================
# ENTRYPOINT
# =============================================================================


def _initialize_runtime() -> None:
    """Perform external startup effects exactly once per process invocation.

    Importing ``run_mcp`` is now safe for documentation tooling and tests: it
    does not emit a startup audit record or mutate the ambient boto3 session by
    assuming a role. Those effects happen only when the server is actually run.
    """
    emit_startup_log()
    assume_mcp_role()


def main() -> None:
    """Start the MCP server after applying runtime identity and audit setup."""
    _initialize_runtime()
    mcp.run()


# Set only after registration/re-export initialization has completed. Python's
# reload machinery preserves this sentinel in the module dictionary.
_RUN_MCP_IMPORT_COMPLETE = True


if __name__ == "__main__":
    main()
