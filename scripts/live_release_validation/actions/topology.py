"""topology: verify stacks, EKS, API endpoints, queues, and DynamoDB."""

from __future__ import annotations

import json
from typing import Any

from ..checks.topology import (
    _bounded_topology_evidence,
    _converge_region_addons,
    _health_stability_samples,
    _queue_counts,
)
from ..constants import (
    _HEALTHY_STACK_STATUSES,
)
from ..context import (
    _direct_regional_access_enabled,
)
from ..inventory import (
    describe_stack,
)
from ..models import RunContext, utc_now
from ..ownership.stacks import (
    _reconcile_stack_ownership,
    _record_stack_identity,
)


def action_topology(ctx: RunContext) -> dict[str, Any]:
    """Verify deterministic add-on convergence before stable API and data-plane health."""
    _reconcile_stack_ownership(ctx)
    stack_details: dict[str, Any] = {}
    target_stack_regions = ctx.checkpoint.state["target_stack_regions"]
    for stack_name, region in target_stack_regions.items():
        stack = describe_stack(ctx.session, str(region), stack_name)
        if stack is None:
            raise RuntimeError(f"Expected deployed stack is absent: {stack_name} ({region})")
        _record_stack_identity(ctx, stack_name, str(region), stack)
        if stack["status"] not in _HEALTHY_STACK_STATUSES:
            raise RuntimeError(
                f"Stack {stack_name} is {stack['status']}, expected one of "
                f"{sorted(_HEALTHY_STACK_STATUSES)}"
            )
        stack_details[stack_name] = stack

    convergence: dict[str, Any] = {
        "started_at": utc_now(),
        "status": "running",
        "regions": {},
    }
    ctx.checkpoint.state["topology_convergence"] = convergence
    ctx.persist()
    for region in ctx.deployment_regions:
        stack_name = f"{ctx.config.project_name}-{region}"
        evidence: dict[str, Any] = {
            "region": region,
            "stack_name": stack_name,
            "result": "pending",
            "observations": [],
        }
        convergence["regions"][region] = evidence
        try:
            if target_stack_regions.get(stack_name) != region:
                raise RuntimeError(
                    f"Checkpoint does not bind exact regional stack {stack_name} to {region}"
                )
            stack = stack_details.get(stack_name)
            if not isinstance(stack, dict):
                raise RuntimeError(f"Exact regional stack was not described: {region}:{stack_name}")
            _converge_region_addons(
                ctx,
                region=region,
                stack_name=stack_name,
                stack=stack,
                evidence=evidence,
            )
        except Exception as exc:
            evidence["result"] = "failed"
            evidence["completed_at"] = utc_now()
            evidence["error"] = _bounded_topology_evidence(f"{type(exc).__name__}: {exc}")
            convergence["status"] = "failed"
            convergence["completed_at"] = utc_now()
            ctx.persist()
            raise
        evidence["result"] = "succeeded"
        evidence["completed_at"] = utc_now()
        ctx.persist()
    convergence["status"] = "succeeded"
    convergence["completed_at"] = utc_now()
    ctx.persist()

    clusters: dict[str, Any] = {}
    for region in ctx.deployment_regions:
        name = f"{ctx.config.project_name}-{region}"
        cluster = ctx.session.client("eks", region_name=region).describe_cluster(name=name)[
            "cluster"
        ]
        if cluster.get("status") != "ACTIVE":
            raise RuntimeError(f"EKS cluster {name} is not ACTIVE: {cluster.get('status')}")
        clusters[region] = {
            "name": name,
            "arn": cluster.get("arn"),
            "status": cluster.get("status"),
            "version": cluster.get("version"),
            "endpoint_public_access": (cluster.get("resourcesVpcConfig") or {}).get(
                "endpointPublicAccess"
            ),
            "endpoint_private_access": (cluster.get("resourcesVpcConfig") or {}).get(
                "endpointPrivateAccess"
            ),
        }

    global_endpoint = ctx.aws_client.get_api_endpoint(force_refresh=True)
    global_url = str(getattr(global_endpoint, "url", "") or "")
    if not global_url:
        raise RuntimeError("Global API endpoint has no URL")
    direct_regional_access = _direct_regional_access_enabled(ctx)
    regional_urls: dict[str, str] = {}
    if direct_regional_access:
        for region in ctx.deployment_regions:
            endpoint = ctx.aws_client.get_regional_api_endpoint(region, force_refresh=True)
            endpoint_url = str(getattr(endpoint, "url", "") or "")
            if not endpoint_url:
                raise RuntimeError(f"Direct regional API endpoint is absent in {region}")
            regional_urls[region] = endpoint_url

    health_samples = _health_stability_samples(
        ctx,
        global_url=global_url,
        regional_urls=regional_urls,
    )
    global_samples = [sample for sample in health_samples if sample["scope"] == "global"]
    global_api = {
        "url": global_url,
        "health": global_samples[-1]["payload"],
        "samples": global_samples,
    }
    if direct_regional_access:
        regional_endpoints = {
            region: {
                "url": regional_urls[region],
                "health": next(
                    sample["payload"]
                    for sample in reversed(health_samples)
                    if sample["region"] == region
                ),
                "samples": [sample for sample in health_samples if sample["region"] == region],
            }
            for region in ctx.deployment_regions
        }
    else:
        regional_endpoints = {
            region: {
                "skipped": True,
                "reason": "direct caller access is disabled by cdk.json",
                "samples": [],
            }
            for region in ctx.deployment_regions
        }

    queue_baseline: dict[str, Any] = {}
    for region in ctx.deployment_regions:
        status = ctx.job_manager.get_queue_status(region)
        counts = _queue_counts(status)
        if any(counts.values()):
            raise RuntimeError(
                f"Fresh queue in {region} is not empty: {json.dumps(counts, sort_keys=True)}"
            )
        queue_baseline[region] = status

    table_name = f"{ctx.config.project_name}-jobs"
    table = ctx.session.client("dynamodb", region_name=ctx.config.global_region).describe_table(
        TableName=table_name
    )["Table"]
    if table.get("TableStatus") != "ACTIVE":
        raise RuntimeError(f"DynamoDB table {table_name} is not ACTIVE")

    ctx.checkpoint.state["queue_baseline"] = queue_baseline
    ctx.persist()
    return {
        "stacks": stack_details,
        "clusters": clusters,
        "convergence": convergence,
        "health_samples": health_samples,
        "global_api": global_api,
        "regional_apis": regional_endpoints,
        "queue_baseline": queue_baseline,
        "jobs_table": {
            "name": table_name,
            "arn": table.get("TableArn"),
            "status": table.get("TableStatus"),
        },
    }
