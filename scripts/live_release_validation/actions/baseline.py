"""baseline: capture protected CloudFormation and ECR baselines."""

from __future__ import annotations

import copy
import json
from typing import Any

from ..context import (
    _topology_regions,
)
from ..inventory import (
    capture_baseline,
    collect_project_resources,
    project_resources_are_absent,
)
from ..models import RunContext
from ..ownership.dynamodb_streams import (
    _strip_expired_table_streams,
)
from ..ownership.ecr import (
    _strip_baseline_ecr,
)
from ..ownership.efs_automatic_backups import (
    _strip_accepted_efs_automatic_backup_recovery_points,
)
from ..ownership.transaction_search import (
    TRANSACTION_SEARCH_BASELINE_STATE_KEY,
    _capture_transaction_search_baseline,
    _validated_transaction_search_baseline,
)
from ..ownership.vpc_endpoints import (
    _strip_deleted_vpc_endpoints,
)

_BASELINE_EFS_ACCEPTANCE_STATE_KEY = "baseline_accepted_efs_automatic_backup_recovery_points"


def action_baseline(ctx: RunContext) -> dict[str, Any]:
    """Capture protected stacks/ECR and Transaction Search state; reject project leftovers.

    The Transaction Search record (every regional Region's X-Ray trace segment
    destination, GCO resource policy, and span log groups) is persisted with
    the baseline in the same checkpoint write, because teardown restores
    exactly what it says and a resume must never re-capture state the run
    already changed.
    """
    if ctx.checkpoint.baseline is not None:
        accepted_efs_backups = ctx.checkpoint.state.get(
            _BASELINE_EFS_ACCEPTANCE_STATE_KEY,
            [],
        )
        if not isinstance(accepted_efs_backups, list):
            raise RuntimeError("Checkpoint baseline EFS acceptance evidence must be a list")
        transaction_search = _validated_transaction_search_baseline(ctx)
        return {
            "reused_checkpoint_baseline": True,
            **ctx.checkpoint.baseline,
            "accepted_efs_automatic_backup_recovery_points": copy.deepcopy(accepted_efs_backups),
            "transaction_search": copy.deepcopy(transaction_search),
        }

    enabled_regions = ctx.checkpoint.state.get("enabled_regions")
    if not enabled_regions:
        raise RuntimeError("Preflight did not record enabled AWS Regions")
    baseline = capture_baseline(
        ctx.session,
        enabled_regions=enabled_regions,
        ecr_regions=_topology_regions(ctx),
        protected_stack_names=ctx.settings.protected_stack_names,
    )

    project_inventory = collect_project_resources(
        ctx.session,
        enabled_regions=enabled_regions,
        expected_account=ctx.settings.expected_account,
        project_name=ctx.config.project_name,
        seed_region=ctx.config.global_region,
        validation_run_id=ctx.settings.run_id,
    )
    disallowed_inventory = _strip_baseline_ecr(project_inventory, baseline)
    disallowed_inventory, accepted_efs_backups = (
        _strip_accepted_efs_automatic_backup_recovery_points(
            ctx,
            disallowed_inventory,
        )
    )
    disallowed_inventory, accepted_expired_streams = _strip_expired_table_streams(
        ctx,
        disallowed_inventory,
    )
    disallowed_inventory, accepted_deleted_vpc_endpoints = _strip_deleted_vpc_endpoints(
        ctx,
        disallowed_inventory,
    )
    if not project_resources_are_absent(disallowed_inventory):
        raise RuntimeError(
            "Fresh baseline contains project resources not owned by this run: "
            + json.dumps(disallowed_inventory, sort_keys=True)
        )

    transaction_search = _capture_transaction_search_baseline(ctx)

    ctx.checkpoint.baseline = baseline
    ctx.checkpoint.state[_BASELINE_EFS_ACCEPTANCE_STATE_KEY] = copy.deepcopy(accepted_efs_backups)
    ctx.checkpoint.state[TRANSACTION_SEARCH_BASELINE_STATE_KEY] = copy.deepcopy(transaction_search)
    ctx.persist()
    # The accepted-stream evidence rides the action result (report) only; the
    # persisted checkpoint baseline stays exactly the protected-stack/ECR
    # capture that final-inventory's compare_baseline expects. The Transaction
    # Search record lives in checkpoint state for the same reason.
    return {
        **baseline,
        "accepted_efs_automatic_backup_recovery_points": accepted_efs_backups,
        "accepted_expired_dynamodb_streams": accepted_expired_streams,
        "accepted_deleted_vpc_endpoints": accepted_deleted_vpc_endpoints,
        "transaction_search": transaction_search,
    }
