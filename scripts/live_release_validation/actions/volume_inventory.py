"""volume-inventory: record the exact pre-destroy PVC/PV/EBS inventory.

This is the evidence every later volume assertion is measured against, so it
runs while the cluster still exists — after ``topology`` has proved the stack
and cluster are healthy, and long before anything is destroyed. The record it
persists (per-PVC identity and requested size, bound PV identity/CSI
driver/handle, normalized EBS identity/Region/AZ/size/state/attachments/exact
cluster tag, and an explicit reason for every non-participating PVC) is what the
destroy-time targets and the post-destroy retain/delete assertions read.

Safety shape: the action is a no-op unless an exact volume-scenario case is
selected; every Region passes the scenario authorization gate (checkpoint
identity, account, branch, and CloudFormation stack ownership) before any EKS or
EC2 request; and each Region's evidence is persisted atomically as it is
observed, so an interrupted run resumes with everything it had already proved.
"""

from __future__ import annotations

from typing import Any

from cli.volume_cleanup import (
    RegionalVolumeTarget,
    TargetResolutionKind,
    resolve_regional_volume_target,
)

from ..checks.volumes import (
    cluster_kubectl,
    describe_recorded_volumes,
    observability_size_assertions,
    pvc_records,
    read_volume_objects,
)
from ..models import RunContext, utc_now
from ..ownership.stacks import _owned_stack_record
from ..ownership.volumes import (
    PRE_DESTROY_INVENTORY_KEY,
    _authorize_volume_scenario,
    _volume_scenario_state,
)

# ``PRE_DESTROY_INVENTORY_KEY`` is owned by ``ownership/volumes.py`` alongside
# the rest of the scenario state section, so the destroy-time target capture can
# read this inventory without importing the action that writes it.


def _observability_enabled(ctx: RunContext) -> tuple[bool, str]:
    """Return whether the checked-in config deploys PVC-backed observability."""
    observability = ctx.cdk_context.get("cluster_observability")
    if not isinstance(observability, dict) or not observability.get("enabled", True):
        return False, "cdk.json cluster_observability.enabled is false"
    return True, "cdk.json cluster_observability.enabled is true"


def _resolved_target(ctx: RunContext, *, region: str) -> RegionalVolumeTarget:
    """Resolve one exact regional volume target from checkpointed ownership."""
    stack_name = f"{ctx.config.project_name}-{region}"
    record = _owned_stack_record(ctx, region, stack_name)
    stack_id = str((record or {}).get("stack_id") or "")
    if not stack_id:
        raise RuntimeError(
            f"No checkpointed CloudFormation identity exists for {region}:{stack_name}; "
            "refusing to record volume identities from a name alone"
        )
    resolution = resolve_regional_volume_target(
        project_name=ctx.config.project_name,
        stack_name=stack_name,
        configured_regions=ctx.deployment_regions,
        stack_id=stack_id,
    )
    if resolution.kind is not TargetResolutionKind.TARGET or resolution.target is None:
        raise RuntimeError(
            f"Cannot record volume identities for {region}:{stack_name}: "
            f"{resolution.kind.value} ({resolution.reason_code}) {resolution.reason}"
        )
    return resolution.target


def _record_region_inventory(ctx: RunContext, *, region: str) -> dict[str, Any]:
    """Record one Region's PVC/PV/EBS inventory behind the authorization gate."""
    target = _resolved_target(ctx, region=region)
    _authorize_volume_scenario(
        ctx,
        action=f"pre-destroy-inventory:{region}",
        stack_name=target.stack_name,
        region=target.region,
        stack_id=target.stack_id,
    )

    with cluster_kubectl(target.cluster_name, target.region) as kubectl:
        claims, volumes = read_volume_objects(kubectl)

    records = pvc_records(claims, volumes)
    participating = [record for record in records if record["participating"]]
    observations = describe_recorded_volumes(
        ctx.session,
        target=target,
        volume_ids=[str(record["volume_id"]) for record in participating],
    )
    for record in participating:
        observation = observations.get(str(record["volume_id"]), {})
        if not observation.get("observed"):
            record["participating"] = False
            record["reason_code"] = observation.get("reason_code", "ebs-volume-unobserved")
            record["reason"] = observation.get(
                "reason", f"EBS volume {record['volume_id']} could not be observed"
            )

    observability_enabled, observability_source = _observability_enabled(ctx)
    if observability_enabled:
        observability = observability_size_assertions(
            records,
            observations,
            cdk_context=ctx.cdk_context,
        )
    else:
        observability = {}

    recorded_volumes = [
        observation
        for volume_id, observation in sorted(observations.items())
        if observation.get("observed")
    ]
    return {
        "region": target.region,
        "stack_name": target.stack_name,
        "stack_id": target.stack_id,
        "cluster_name": target.cluster_name,
        "cluster_tag_key": target.cluster_tag_key,
        "recorded_at": utc_now(),
        "pvcs": records,
        "non_participating": [
            {
                "namespace": record["namespace"],
                "name": record["name"],
                "uid": record["uid"],
                "reason_code": record["reason_code"],
                "reason": record["reason"],
            }
            for record in records
            if not record["participating"]
        ],
        "volumes": recorded_volumes,
        "volume_ids": [str(observation["volume_id"]) for observation in recorded_volumes],
        "observations": observations,
        "observability": {
            "enabled": observability_enabled,
            "source": observability_source,
            "components": observability,
        },
        "result": "recorded",
    }


def _observability_failures(inventory: dict[str, Any]) -> list[str]:
    """Collect every failed observability size assertion across Regions."""
    failures: list[str] = []
    for region, evidence in sorted(inventory["regions"].items()):
        components = (evidence.get("observability") or {}).get("components") or {}
        for component, assertion in sorted(components.items()):
            for failure in assertion.get("failures") or []:
                failures.append(f"{region} {component}: {failure}")
    return failures


def action_volume_inventory(ctx: RunContext) -> dict[str, Any]:
    """Record every PVC, bound PV, and PVC-derived EBS volume before destruction.

    Runs only for an explicitly selected volume-scenario case. For each exact
    regional target it authorizes the scenario boundary first, then records PVC
    identity and requested size, bound PV identity/CSI driver/volumeHandle,
    normalized EBS identity/Region/AZ/size/state/attachments/exact cluster tag,
    and a machine-readable reason for every PVC that produced no EBS volume.
    Prometheus and Alertmanager PVCs are discovered from their live component
    labels and their observed sizes asserted separately, so a size change fails
    the run instead of quietly redefining which volume was recorded.
    """
    case = ctx.settings.volume_scenario_case
    if case == "disabled":
        return {
            "status": "skipped",
            "case": case,
            "reason": (
                "No E2E volume scenario is selected; pass --volume-scenario "
                "retain-override|delete|both to record volume identities"
            ),
        }

    state = _volume_scenario_state(ctx)
    inventory: dict[str, Any] = {
        "status": "running",
        "case": case,
        "started_at": utc_now(),
        "regions": {},
    }
    with ctx.state_lock:
        state[PRE_DESTROY_INVENTORY_KEY] = inventory
        ctx.persist_callback(ctx.checkpoint)

    for region in ctx.deployment_regions:
        try:
            evidence = _record_region_inventory(ctx, region=region)
        except Exception as exc:
            with ctx.state_lock:
                inventory["regions"][region] = {
                    "region": region,
                    "result": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                inventory["status"] = "failed"
                inventory["completed_at"] = utc_now()
                ctx.persist_callback(ctx.checkpoint)
            raise
        with ctx.state_lock:
            inventory["regions"][region] = evidence
            ctx.persist_callback(ctx.checkpoint)

    failures = _observability_failures(inventory)
    with ctx.state_lock:
        inventory["status"] = "failed" if failures else "recorded"
        inventory["observability_failures"] = failures
        inventory["completed_at"] = utc_now()
        ctx.persist_callback(ctx.checkpoint)
    if failures:
        raise RuntimeError(
            "Observability PVC-backed EBS volumes do not match their configured sizes:\n  "
            + "\n  ".join(failures)
        )
    return inventory
