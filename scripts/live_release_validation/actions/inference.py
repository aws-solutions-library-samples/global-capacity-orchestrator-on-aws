"""First-class managed inference validation action."""

from __future__ import annotations

import sys
from typing import Any

from scripts.example_job_validation import kube

from ..checks.inference import (
    ManagedInferenceLifecycle,
    ManagedInferenceValidationError,
    initialize_run_state,
)
from ..models import RunContext


def action_inference(ctx: RunContext) -> dict[str, Any]:
    """Run every runtime scenario strictly sequentially and prove stable absence."""
    settings = ctx.settings
    if not settings.inference_enabled:
        raise ManagedInferenceValidationError(
            "inference action requires the main RunSettings inference contract"
        )
    plans, state = initialize_run_state(ctx, settings)
    if settings.selected_region not in ctx.deployment_regions:
        state["session_error"] = "selected region is not in the deployed regional topology"
        ctx.persist()
        raise ManagedInferenceValidationError(
            "inference selected region is not part of this deployment"
        )

    kubeconfig_path = settings.kubeconfig_path
    if kubeconfig_path.parent != settings.report_dir:
        raise ManagedInferenceValidationError("isolated kubeconfig escaped the private report dir")
    cluster_name = f"{ctx.config.project_name}-{settings.selected_region}"

    try:
        with kube.cluster_session(
            settings.repo_root,
            cluster_name,
            settings.selected_region,
            kubeconfig_path=kubeconfig_path,
            gco_command=(sys.executable, "-m", "cli.main"),
        ) as kubectl:
            lifecycle = ManagedInferenceLifecycle(
                ctx=ctx,
                settings=settings,
                plans=plans,
                state=state,
                kubectl=kubectl,
                kubeconfig_path=kubeconfig_path,
            )
            lifecycle.verify_shared_proxy_autoscaling(state)
            return lifecycle.execute()
    except ManagedInferenceValidationError:
        raise
    except (Exception, KeyboardInterrupt) as exc:
        state["session_error"] = f"{type(exc).__name__}: {exc}"
        ctx.persist()
        if not isinstance(exc, Exception):
            raise
        raise ManagedInferenceValidationError(
            "inference cluster session failed; inspect the private checkpoint"
        ) from None
