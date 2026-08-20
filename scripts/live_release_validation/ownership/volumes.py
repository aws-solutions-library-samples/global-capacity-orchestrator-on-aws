"""Durable identity fencing for this run's EBS volume-scenario evidence.

Every scenario step that records, destroys, or asserts EBS state passes through
:func:`_authorize_volume_scenario` first, so a resumed or re-pointed invocation
cannot inherit another run's volumes. The gate re-verifies the exact checkpoint
identity (including the scenario case), the account preflight recorded, the
live branch, the explicit retained-fixture authorization, and — when a target
is supplied — CloudFormation stack ownership, before any EKS or EC2 request.
"""

from __future__ import annotations

from typing import Any

from ..context import _resolve_branch
from ..models import RunContext, utc_now
from ..volume_scenario import validated_volume_scenario_case
from .stacks import _authorize_owned_stack

#: Checkpoint state section owned by the volume scenario.
_VOLUME_SCENARIO_STATE_KEY = "volume_scenario"

#: Key inside that section holding the pre-destroy PVC/PV/EBS inventory. Owned
#: here with the section itself so the destroy-time target capture can read it
#: without importing the action that writes it.
PRE_DESTROY_INVENTORY_KEY = "pre_destroy_inventory"

#: Key inside that section holding the retained-fixture cleanup evidence. Owned
#: here for the same reason: final-inventory residual accounting reports the
#: fixture-cleanup status without importing the module that deletes fixtures.
FIXTURE_CLEANUP_KEY = "fixture_cleanup"


def _volume_scenario_state(ctx: RunContext) -> dict[str, Any]:
    """Return this run's scenario section, fenced to one exact case."""
    case = validated_volume_scenario_case(ctx.settings.volume_scenario_case)
    if case == "disabled":
        raise RuntimeError(
            "The E2E volume scenario is disabled for this run; select retain-override "
            "or delete before recording or asserting volume evidence"
        )
    with ctx.state_lock:
        state = ctx.checkpoint.state.setdefault(_VOLUME_SCENARIO_STATE_KEY, {})
        if not isinstance(state, dict):
            raise RuntimeError("Checkpoint volume_scenario state must be an object")
        recorded = state.setdefault("case", case)
        if recorded != case:
            raise RuntimeError(
                f"Checkpoint volume scenario case changed from {recorded!r} to {case!r}; "
                "each case runs as its own isolated lifecycle"
            )
        state["fixture_cleanup_authorized"] = bool(ctx.settings.confirm_ebs_fixture_cleanup)
        ctx.persist_callback(ctx.checkpoint)
        return state


def _checkpointed_account(ctx: RunContext) -> str:
    """Return the account preflight proved, refusing unverified invocations."""
    caller_arn = str(ctx.checkpoint.state.get("account_arn") or "")
    parts = caller_arn.split(":")
    account = parts[4] if len(parts) >= 5 else ""
    if not account:
        raise RuntimeError(
            "Volume scenario work requires a checkpointed caller identity; run preflight first"
        )
    if account != ctx.settings.expected_account:
        raise RuntimeError(
            f"Checkpointed caller account {account} does not match expected "
            f"account {ctx.settings.expected_account}"
        )
    return account


def _authorize_volume_scenario(
    ctx: RunContext,
    *,
    action: str,
    stack_name: str | None = None,
    region: str | None = None,
    stack_id: str | None = None,
    fixture_cleanup: bool = False,
) -> dict[str, Any]:
    """Authorize one scenario boundary and persist its exact authorization evidence."""
    settings = ctx.settings
    state = _volume_scenario_state(ctx)
    case = validated_volume_scenario_case(settings.volume_scenario_case)

    if ctx.checkpoint.identity != settings.identity():
        raise RuntimeError(
            f"Checkpoint identity does not match this invocation; refusing volume scenario "
            f"action {action!r}. Account, SHA, branch, profile, actions, run ID, scenario "
            "case, and fixture-cleanup authorization must remain exact."
        )
    account = _checkpointed_account(ctx)
    branch = _resolve_branch(settings.repo_root)
    if branch != settings.expected_branch:
        raise RuntimeError(
            f"Current branch {branch!r} does not match expected branch "
            f"{settings.expected_branch!r}; refusing volume scenario action {action!r}"
        )

    if fixture_cleanup:
        if not settings.confirm_ebs_fixture_cleanup:
            raise RuntimeError(
                "Deleting this run's retained validation volumes requires the explicit "
                "--confirm-ebs-fixture-cleanup authorization"
            )
        if case != "retain-override":
            raise RuntimeError(
                f"Retained-fixture volume cleanup belongs to the retain-override case, not {case!r}"
            )

    target: dict[str, str] | None = None
    if stack_name is not None or region is not None or stack_id is not None:
        if not stack_name or not region or not stack_id:
            raise RuntimeError(
                f"Refusing volume scenario action {action!r} without an exact stack name, "
                "Region, and stack ID"
            )
        _authorize_owned_stack(ctx, stack_name, region, stack_id)
        target = {"stack_name": stack_name, "region": region, "stack_id": stack_id}

    authorization = {
        "action": action,
        "case": case,
        "run_id": settings.run_id,
        "account": account,
        "branch": branch,
        "target": target,
        "fixture_cleanup": fixture_cleanup,
        "authorized_at": utc_now(),
    }
    with ctx.state_lock:
        authorizations = state.setdefault("authorizations", {})
        if not isinstance(authorizations, dict):
            raise RuntimeError("Checkpoint volume_scenario authorizations must be an object")
        authorizations[action] = authorization
        ctx.persist_callback(ctx.checkpoint)
    return authorization
