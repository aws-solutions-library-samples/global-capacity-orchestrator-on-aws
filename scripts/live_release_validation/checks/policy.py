"""Deployed job-validation policy readback checks.

``GET /api/v1/policy`` exists so a caller can ask "will this cluster admit the
job I am about to pay to run" before submitting. It answers in three layers: the
front-door per-manifest caps the manifest processor applies itself, the
per-container ``LimitRange``, and the namespace aggregate ``ResourceQuota``.

The two cluster-read layers are deliberately fail-soft -- a Kubernetes read
failure degrades that namespace to ``{"status": "unavailable", "reason": ...}``
rather than failing the whole response. That is the right behavior and it is also
why this check has to exist: **the degraded response is an HTTP 200**, so
``response.ok`` proves nothing and every transport-level check in this harness
passes while the endpoint reports nothing useful.

That is not hypothetical. The 2026-08-26 run was green across all ten actions
while ``cluster_enforcement."gco-jobs"`` was ``{"status": "unavailable",
"reason": "403 Forbidden"}`` -- the manifest-processor Role had no grant on
``resourcequotas``/``limitranges``. A caller reading only the caps would be told
a manifest is admissible that pod creation then rejects, possibly after they
provisioned a region on the strength of that answer.

So the assertions here are all on the response *body*.
"""

from __future__ import annotations

import re
from typing import Any

from ..checks.jobs import _response_json
from ..context import _job_transport_region
from ..models import RunContext

#: An AWS ECR registry hostname, e.g. 123456789012.dkr.ecr.us-east-2.amazonaws.com
_ECR_HOSTNAME = re.compile(r"^(\d{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$")

#: Quota keys the deployed ResourceQuota is expected to carry. Sourced from
#: 04-resource-quotas.yaml, which is substituted from cdk.json at deploy time.
_EXPECTED_QUOTA_HINTS = ("cpu", "memory")


def _get_policy(ctx: RunContext, region: str) -> dict[str, Any]:
    """Fetch one Region's /api/v1/policy through its authorized transport."""
    response = ctx.aws_client.make_authenticated_request(
        method="GET",
        path="/api/v1/policy",
        target_region=_job_transport_region(ctx, region),
    )
    if not response.ok:
        raise RuntimeError(
            f"Policy readback for {region} failed: {response.status_code} {response.text}"
        )
    return _response_json(response, f"Policy readback for {region}")


def _validate_identity(ctx: RunContext, region: str, payload: dict[str, Any]) -> None:
    """Require the response to come from the Region we addressed.

    A transport that silently answered from the wrong Region would make every
    other assertion here describe a cluster the caller did not ask about.
    """
    observed_region = str(payload.get("region") or "")
    if observed_region != region:
        raise RuntimeError(
            f"Policy readback transport returned Region {observed_region!r}; expected {region!r}"
        )
    expected_cluster = f"{ctx.config.project_name}-{region}"
    observed_cluster = str(payload.get("cluster_id") or "")
    if observed_cluster != expected_cluster:
        raise RuntimeError(
            f"Policy readback for {region} reported cluster_id {observed_cluster!r}; "
            f"expected {expected_cluster!r}"
        )
    source = str(payload.get("source") or "")
    if source != "deployed-cluster-runtime":
        raise RuntimeError(
            f"Policy readback for {region} reported source {source!r}; the endpoint must "
            "name the deployed runtime as its origin so a caller cannot mistake it for "
            "a config-file read"
        )


def _validate_front_door(region: str, policy: dict[str, Any]) -> dict[str, Any]:
    """Require the layer-1 caps and allowlists to be present and non-degenerate."""
    if not policy:
        raise RuntimeError(f"Policy readback for {region} carried no policy object")

    caps = policy.get("manifest_caps")
    if not isinstance(caps, dict):
        raise RuntimeError(f"Policy readback for {region} omitted manifest_caps")
    for key in ("max_cpu_millicores", "max_memory_bytes", "max_gpu_count"):
        value = caps.get(key)
        if not isinstance(value, int) or value <= 0:
            raise RuntimeError(
                f"Policy readback for {region} reported {key}={value!r}; a non-positive "
                "cap would reject every job that requests that resource"
            )

    namespaces = policy.get("allowed_namespaces")
    if not isinstance(namespaces, list) or not namespaces:
        raise RuntimeError(
            f"Policy readback for {region} reported no allowed_namespaces; nothing "
            "could ever be submitted"
        )
    kinds = policy.get("allowed_kinds")
    if not isinstance(kinds, list) or not kinds:
        raise RuntimeError(f"Policy readback for {region} reported no allowed_kinds")

    return {
        "max_cpu_millicores": caps["max_cpu_millicores"],
        "max_memory_bytes": caps["max_memory_bytes"],
        "max_gpu_count": caps["max_gpu_count"],
        "allowed_namespaces": sorted(str(item) for item in namespaces),
        "allowed_kinds": sorted(str(item) for item in kinds),
        "require_accelerator_toleration": bool(policy.get("require_accelerator_toleration")),
        "validation_enabled": bool(policy.get("validation_enabled")),
    }


def _validate_synth_time_ecr_augmentation(
    ctx: RunContext, region: str, policy: dict[str, Any]
) -> list[str]:
    """Require the project's own ECR hostnames to be in the trusted allowlist.

    CDK appends them at synth time (``_augment_trusted_registries_with_project
    _ecr``), which is the concrete reason a locally-computed policy is not
    authoritative: the deployed allowlist is strictly larger than the configured
    one. If this augmentation silently stopped happening, every job pulling from
    the project's own registry would be rejected as an untrusted image source --
    and no offline check would predict it, because the offline view never had
    those hostnames.
    """
    registries = policy.get("trusted_registries")
    if not isinstance(registries, list) or not registries:
        raise RuntimeError(f"Policy readback for {region} reported no trusted_registries")

    account = str(ctx.settings.expected_account or "")
    augmentation: list[str] = []
    for entry in registries:
        match = _ECR_HOSTNAME.match(str(entry))
        if match and (not account or match.group(1) == account):
            augmentation.append(str(entry))

    if not augmentation:
        raise RuntimeError(
            f"Policy readback for {region} shows no project ECR registry in "
            f"trusted_registries {sorted(map(str, registries))!r}. CDK is expected to "
            "append the project's own ECR hostnames at synth time; without them every "
            "job pulling a project-built image is rejected as an untrusted source"
        )
    return sorted(augmentation)


def _validate_cluster_enforcement(
    region: str, enforcement: Any, allowed_namespaces: list[str]
) -> dict[str, Any]:
    """Require layers 2 and 3 to be readable for every allowed namespace.

    This is the assertion the 2026-08-26 regression needed. The endpoint returns
    200 with a per-namespace ``status`` field, so the failure is only visible
    here -- in the body, per namespace.
    """
    if not isinstance(enforcement, dict) or not enforcement:
        raise RuntimeError(
            f"Policy readback for {region} carried no cluster_enforcement object; "
            "layers 2 and 3 (LimitRange, ResourceQuota) would be unreportable"
        )

    missing = sorted(set(allowed_namespaces) - set(enforcement))
    if missing:
        raise RuntimeError(
            f"Policy readback for {region} reports no cluster_enforcement for allowed "
            f"namespace(s) {missing}; a caller cannot tell whether a job would clear "
            "the namespace ceilings"
        )

    degraded: list[str] = []
    summary: dict[str, Any] = {}
    for namespace in sorted(allowed_namespaces):
        layer = enforcement.get(namespace)
        if not isinstance(layer, dict):
            degraded.append(f"{namespace}: not an object")
            continue
        status = str(layer.get("status") or "unknown")
        if status != "ok":
            degraded.append(f"{namespace}: {status} — {layer.get('reason', 'no reason given')}")
            continue

        quotas = layer.get("resource_quotas")
        limits = layer.get("limit_ranges")
        if not isinstance(quotas, dict) or not quotas:
            raise RuntimeError(
                f"Policy readback for {region} namespace {namespace} reports status ok "
                "but no ResourceQuota. 04-resource-quotas.yaml is expected to deploy "
                "one, so an empty result means the aggregate ceiling is unenforced"
            )
        if not isinstance(limits, dict) or not limits:
            raise RuntimeError(
                f"Policy readback for {region} namespace {namespace} reports status ok "
                "but no LimitRange; the per-container ceiling is unenforced"
            )
        for name, hard in quotas.items():
            if not isinstance(hard, dict) or not any(
                any(hint in str(key) for hint in _EXPECTED_QUOTA_HINTS) for key in hard
            ):
                raise RuntimeError(
                    f"Policy readback for {region} ResourceQuota/{name} in {namespace} "
                    f"carries no cpu/memory ceiling: {hard!r}"
                )
        summary[namespace] = {
            "status": status,
            "resource_quotas": sorted(quotas),
            "limit_ranges": sorted(limits),
        }

    if degraded:
        raise RuntimeError(
            f"Policy readback for {region} could not read the live ResourceQuota / "
            f"LimitRange: {'; '.join(degraded)}. The response degrades to HTTP 200, so "
            "this is invisible to a transport-level check — a caller is told a manifest "
            "is admissible that pod creation may still reject. Check the "
            "gco-manifest-processor-role grant on resourcequotas and limitranges."
        )
    return summary


def _validate_region_policy(ctx: RunContext, region: str) -> dict[str, Any]:
    """Assert one Region's full three-layer policy readback."""
    payload = _get_policy(ctx, region)
    _validate_identity(ctx, region, payload)

    policy = payload.get("policy")
    policy = policy if isinstance(policy, dict) else {}
    front_door = _validate_front_door(region, policy)
    augmentation = _validate_synth_time_ecr_augmentation(ctx, region, policy)
    enforcement = _validate_cluster_enforcement(
        region, payload.get("cluster_enforcement"), front_door["allowed_namespaces"]
    )

    return {
        "region": region,
        "cluster_id": payload.get("cluster_id"),
        "source": payload.get("source"),
        "policy": front_door,
        "synth_time_ecr_registries": augmentation,
        "cluster_enforcement": enforcement,
    }
