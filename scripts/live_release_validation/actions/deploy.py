"""deploy: deploy the configured GCO topology."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from ..constants import (
    _RUN_STACK_TAG,
)
from ..inventory import (
    describe_stack,
)
from ..models import RunContext
from ..ownership.ecr import (
    _checkpoint_new_ecr_images,
    _checkpoint_new_ecr_repositories,
    _record_ecr_repository_creation,
)
from ..ownership.kms import (
    _checkpoint_retained_kms_keys,
)
from ..ownership.stacks import (
    _authorize_owned_stack,
    _owned_stack_record,
    _prepared_change_set_authority,
    _reconcile_stack_ownership,
    _record_prepared_stack_identity,
    _record_stack_identity,
)


def action_deploy(ctx: RunContext) -> dict[str, Any]:
    """Deploy the exact checked-out CDK graph and checkpoint every AWS identity."""
    if ctx.checkpoint.baseline is None:
        raise RuntimeError("A protected-resource baseline is required before deployment")
    ctx.checkpoint.deployment_attempted = True
    ctx.checkpoint.destroyed = False
    ctx.persist()

    events: list[dict[str, Any]] = list(ctx.checkpoint.state.get("deploy_events", []))

    def on_start(stack_name: str) -> None:
        with ctx.state_lock:
            events.append({"stack": stack_name, "event": "started", "at": time.time()})
            ctx.checkpoint.state["deploy_events"] = events
            ctx.persist_callback(ctx.checkpoint)

    def on_complete(stack_name: str, success: bool) -> None:
        with ctx.state_lock:
            events.append(
                {
                    "stack": stack_name,
                    "event": "completed",
                    "success": success,
                    "at": time.time(),
                }
            )
            ctx.checkpoint.state["deploy_events"] = events
            region = str(ctx.checkpoint.state["target_stack_regions"][stack_name])
            stack = describe_stack(ctx.session, region, stack_name)
            if stack is None and success:
                raise RuntimeError(f"CDK reported success but {region}:{stack_name} is absent")
            if stack is not None:
                _record_stack_identity(ctx, stack_name, region, stack)
            ctx.persist_callback(ctx.checkpoint)

    def on_prepared(
        stack_name: str,
        region: str,
        stack_id: str,
        change_set_id: str,
        change_set_type: str,
    ) -> None:
        _record_prepared_stack_identity(
            ctx,
            stack_name,
            region,
            stack_id,
            change_set_id,
            change_set_type,
        )
        prepared_change_sets.setdefault(stack_name, {})[change_set_id] = {
            "change_set_id": change_set_id,
            "stack_id": stack_id,
            "change_set_type": change_set_type,
        }
        expected_stack_ids[stack_name] = stack_id

    def on_repository_created(region: str, repository: Mapping[str, Any]) -> None:
        _record_ecr_repository_creation(ctx, region, repository)

    expected_stack_ids = {
        name: (
            str(record["stack_id"])
            if (record := _owned_stack_record(ctx, str(region), name)) is not None
            else None
        )
        for name, region in ctx.checkpoint.state["target_stack_regions"].items()
    }
    prepared_change_sets = _prepared_change_set_authority(ctx)

    try:
        overall, successful, failed = ctx.stack_manager.deploy_orchestrated(
            require_approval=False,
            tags={_RUN_STACK_TAG: ctx.settings.run_id},
            progress="events",
            on_stack_start=on_start,
            on_stack_complete=on_complete,
            parallel=False,
            max_workers=1,
            allow_bootstrap=False,
            bootstrap_stacks=ctx.checkpoint.state["bootstrap_stacks"],
            expected_stack_ids=expected_stack_ids,
            prepared_change_sets=prepared_change_sets,
            authorize_stack=lambda name, region, stack_id: _authorize_owned_stack(
                ctx,
                name,
                region,
                stack_id,
            ),
            strict_deployment_token=ctx.settings.run_id,
            on_change_set_prepared=on_prepared,
            on_ecr_repository_created=on_repository_created,
        )
    finally:
        _reconcile_stack_ownership(ctx)
        _checkpoint_new_ecr_repositories(ctx)
        _checkpoint_new_ecr_images(ctx)
        _checkpoint_retained_kms_keys(ctx)

    result = {
        "overall_success": overall,
        "successful_stacks": successful,
        "failed_stacks": failed,
        "events": events,
        "owned_stacks": ctx.checkpoint.state.get("owned_stacks", {}),
        "owned_ecr_repositories": ctx.checkpoint.state.get("created_ecr_repositories", []),
        "owned_ecr_images": [],
        "retained_ecr_image_deltas": ctx.checkpoint.state.get("retained_ecr_image_deltas", []),
        "owned_kms_keys": ctx.checkpoint.state.get("owned_kms_keys", []),
    }
    ctx.checkpoint.state["deploy_result"] = result
    ctx.persist()
    if not overall:
        raise RuntimeError(f"Orchestrated deployment failed for: {', '.join(failed) or 'unknown'}")
    return result
