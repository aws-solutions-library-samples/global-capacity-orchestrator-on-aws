"""final-inventory: verify zero residual resources and baseline preservation."""

from __future__ import annotations

import copy
import json
from typing import Any

from ..checks.volume_outcomes import (
    accepted_pending_volume_deletions,
    volume_residual_inventory,
)
from ..context import (
    _topology_regions,
)
from ..inventory import (
    capture_baseline,
    collect_project_resources,
    compare_baseline,
    project_resources_are_absent,
    summarize_project_resources,
)
from ..models import RunContext, utc_now
from ..ownership.dynamodb_streams import (
    _strip_expired_table_streams,
)
from ..ownership.ecr import (
    _strip_accepted_retained_ecr,
    _strip_baseline_ecr,
    _strip_expected_retained_ecr,
)
from ..ownership.kms import (
    _strip_expected_pending_kms,
)
from ..ownership.stacks import (
    _verify_target_stack_absence,
)


def action_final_inventory(ctx: RunContext) -> dict[str, Any]:
    """Prove cleanup and exact protected-stack/ECR baseline preservation."""
    if ctx.checkpoint.baseline is None:
        raise RuntimeError("Final inventory cannot compare without a baseline")
    enabled_regions = ctx.checkpoint.state.get("enabled_regions")
    if not enabled_regions:
        raise RuntimeError("Checkpoint omitted enabled Regions")

    stack_absence = _verify_target_stack_absence(ctx)
    final_baseline = capture_baseline(
        ctx.session,
        enabled_regions=enabled_regions,
        ecr_regions=ctx.checkpoint.baseline.get("ecr_regions") or _topology_regions(ctx),
        protected_stack_names=ctx.settings.protected_stack_names,
    )
    comparison_baseline, accepted_retained_ecr = _strip_expected_retained_ecr(
        ctx,
        final_baseline,
    )
    differences = compare_baseline(ctx.checkpoint.baseline, comparison_baseline)
    project_inventory = collect_project_resources(
        ctx.session,
        enabled_regions=enabled_regions,
        expected_account=ctx.settings.expected_account,
        project_name=ctx.config.project_name,
        seed_region=ctx.config.global_region,
        validation_run_id=ctx.settings.run_id,
    )
    residual_inventory = _strip_baseline_ecr(
        project_inventory,
        ctx.checkpoint.baseline,
    )
    residual_inventory = _strip_accepted_retained_ecr(
        residual_inventory,
        accepted_retained_ecr,
    )
    residual_inventory, accepted_pending_kms = _strip_expected_pending_kms(
        ctx,
        residual_inventory,
    )
    residual_inventory, accepted_expired_streams = _strip_expired_table_streams(
        ctx,
        residual_inventory,
    )
    summary = summarize_project_resources(residual_inventory)
    # PVC-provisioned EBS volumes are not CloudFormation resources, so the project
    # scanners cannot see them. Accounting for this run's recorded volume IDs here
    # is what makes an unresolved validation-fixture cleanup fail the run instead
    # of leaving billable storage behind quietly.
    ebs_volume_residuals = volume_residual_inventory(ctx)
    summary["ebs_fixture_volumes"] = len(ebs_volume_residuals["residual_volume_ids"])
    # A volume EC2 has begun releasing is tolerated rather than failed below, so
    # it is disclosed here in the same shape as the other accepted residues
    # instead of staying buried in the per-Region accounting.
    accepted_pending_volumes = accepted_pending_volume_deletions(ebs_volume_residuals)
    result = {
        "summary": summary,
        "stack_absence": stack_absence,
        "ebs_volume_residuals": ebs_volume_residuals,
        "baseline_differences": differences,
        "protected_and_ecr_inventory": final_baseline,
        "comparison_inventory": comparison_baseline,
        "accepted_retained_ecr": accepted_retained_ecr,
        "project_resources": project_inventory,
        "accepted_pending_kms_keys": accepted_pending_kms,
        "accepted_expired_dynamodb_streams": accepted_expired_streams,
        "accepted_pending_deletion_volumes": accepted_pending_volumes,
        "residual_project_resources": residual_inventory,
    }
    ctx.report.final_inventory = result
    ctx.checkpoint.state["final_inventory"] = copy.deepcopy(result)
    if not stack_absence["all_absent"] and ctx.checkpoint.destroyed:
        ctx.checkpoint.destroyed = False
        for action_name in ("destroy", "final-inventory"):
            if action_name in ctx.checkpoint.completed_actions:
                ctx.checkpoint.completed_actions.remove(action_name)
        ctx.checkpoint.state.setdefault("stale_destroyed_reconciliations", []).append(
            {"at": utc_now(), "stack_absence": stack_absence, "source": "final-inventory"}
        )
    ctx.persist()
    if not stack_absence["all_absent"]:
        raise RuntimeError(
            "Target stacks remain after teardown: "
            + json.dumps(stack_absence["residual"], sort_keys=True)
        )
    if differences:
        raise RuntimeError(
            "Protected stack/ECR baseline changed: " + json.dumps(differences, sort_keys=True)
        )
    if not project_resources_are_absent(residual_inventory):
        raise RuntimeError(
            "Project resources remain after teardown: "
            + json.dumps(residual_inventory, sort_keys=True)
        )
    if ebs_volume_residuals["residual_volume_ids"]:
        raise RuntimeError(
            "Recorded EBS volumes remain after teardown: "
            + json.dumps(
                {
                    "residual_volume_ids": ebs_volume_residuals["residual_volume_ids"],
                    "fixture_cleanup_status": ebs_volume_residuals["fixture_cleanup_status"],
                    "follow_up": ebs_volume_residuals["follow_up"],
                },
                sort_keys=True,
            )
        )
    return result
