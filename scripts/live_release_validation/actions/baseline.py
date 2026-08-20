"""baseline: capture protected CloudFormation and ECR baselines."""

from __future__ import annotations

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


def action_baseline(ctx: RunContext) -> dict[str, Any]:
    """Capture protected stacks/ECR and reject non-stack project leftovers."""
    if ctx.checkpoint.baseline is not None:
        return {"reused_checkpoint_baseline": True, **ctx.checkpoint.baseline}

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
    disallowed_inventory, accepted_expired_streams = _strip_expired_table_streams(
        ctx,
        disallowed_inventory,
    )
    if not project_resources_are_absent(disallowed_inventory):
        raise RuntimeError(
            "Fresh baseline contains project resources not owned by this run: "
            + json.dumps(disallowed_inventory, sort_keys=True)
        )

    ctx.checkpoint.baseline = baseline
    ctx.persist()
    # The accepted-stream evidence rides the action result (report) only; the
    # persisted checkpoint baseline stays exactly the protected-stack/ECR
    # capture that final-inventory's compare_baseline expects.
    return {**baseline, "accepted_expired_dynamodb_streams": accepted_expired_streams}
