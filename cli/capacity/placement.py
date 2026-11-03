"""What a job manifest asks of a Region, read before ``--auto-region`` picks one.

``gco jobs submit-sqs --auto-region`` used to rank Regions without opening the
manifest, so a Job labelled for the ``gco-hyperpod`` Kueue queue could land in
a Region with no HyperPod cluster and stay Pending there. This reads the two
facts the picker needs: whether the manifest is routed to SageMaker HyperPod
(the one definition in :func:`gco.job_admission.hyperpod_routing`, shared with
the manifest processor and the admission checks) and how many GPUs its pod
specs request (the count the manifest caps use: every pod spec weighted by its
replicas, ``nvidia.com/gpu`` limits or else requests).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from gco.job_admission import hyperpod_routing, weighted_pod_specs

from .backends import BACKEND_EKS, BACKEND_HYPERPOD

__all__ = ["PlacementHints", "manifest_placement"]

_GPU_RESOURCE = "nvidia.com/gpu"


@dataclass(frozen=True)
class PlacementHints:
    """The backend a manifest is routed to and the GPUs it requests."""

    #: ``hyperpod`` when any manifest is routed to HyperPod, else ``eks``.
    backend: str
    #: ``nvidia.com/gpu`` across every pod spec, replicas weighted.
    gpu_count: int
    #: How the manifest asks for HyperPod (``queue`` or ``selector``), else ``None``.
    routing: str | None = None


def _gpu_quantity(value: Any) -> int:
    """A ``nvidia.com/gpu`` quantity as an integer; anything unparsable counts as 0."""
    try:
        return max(0, int(str(value)))
    except TypeError, ValueError:
        return 0


def _pod_spec_gpus(pod_spec: dict[str, Any]) -> int:
    total = 0
    for container in pod_spec.get("containers") or []:
        if not isinstance(container, dict):
            continue
        resources = container.get("resources") or {}
        limits = resources.get("limits") or {}
        requests = resources.get("requests") or {}
        total += _gpu_quantity(limits.get(_GPU_RESOURCE) or requests.get(_GPU_RESOURCE) or 0)
    return total


def manifest_placement(manifests: Iterable[Any]) -> PlacementHints:
    """Read the routing and GPU request of every manifest document.

    Non-mapping documents (an empty YAML document, a stray scalar) are
    skipped; the first HyperPod-routed manifest decides the backend.
    """
    backend = BACKEND_EKS
    routing: str | None = None
    gpu_count = 0
    for manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        pairs = weighted_pod_specs(manifest)
        route = hyperpod_routing(manifest, [spec for spec, _ in pairs])
        if route is not None and routing is None:
            routing = route
            backend = BACKEND_HYPERPOD
        gpu_count += sum(multiplier * _pod_spec_gpus(spec) for spec, multiplier in pairs)
    return PlacementHints(backend=backend, gpu_count=gpu_count, routing=routing)
