"""Shared gco-jobs resource-governance defaults and quantity parsing.

Three enforcement layers govern job resources and must tell one story: the
manifest/queue processors cap what a single submitted manifest may total,
the gco-jobs ``LimitRange`` caps each container, and the namespace
``ResourceQuota`` caps the aggregate. The values below are the single source
of truth for all three, shared by:

- the regional stack (substitutes them into ``04-resource-quotas.yaml`` and
  validates cdk.json overrides against the layering invariant at synth),
- the manifest and queue processors (their built-in runtime defaults), and
- the example-job validation static checks (prove every shipped example
  fits these defaults offline).

This module lives at the top of the ``gco`` package — NOT under
``gco.stacks`` — because the service container images deliberately exclude
``gco/stacks/**`` from their build context (synth-only code must never
rebuild service images; see ``_SERVICE_IMAGE_COMMON_EXCLUDES`` in the
regional stack). A runtime import from ``gco.stacks`` fails the distroless
runtime smoke at image build. ``gco.stacks.constants`` re-exports these
names for synth-side callers.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

DEFAULT_RESOURCE_QUOTA: Mapping[str, str] = MappingProxyType(
    {
        # Namespace-wide aggregate ceilings (ResourceQuota on requests.*):
        # sized to hold two full accelerator-node training pods side by side
        # with CPU headroom for the surrounding jobs.
        "max_cpu": "400",
        "max_memory": "4096Gi",
        "max_gpu": "32",
        "max_pods": "50",
        # Per-container ceilings (LimitRange max): one full accelerator-node
        # slice — p5.48xlarge / trn2.48xlarge expose 192 vCPUs, 2 TiB memory,
        # and 8 GPUs, and one-pod-per-node is the standard unit of
        # distributed training. Anything smaller rejects the platform's own
        # EFA training example at pod admission (observed live: example-job
        # validation run ex241-df723811, where the previous 10-CPU/64Gi/4-GPU
        # caps left the Job permanently podless with only namespace events
        # explaining why).
        "container_max_cpu": "192",
        "container_max_memory": "2048Gi",
        "container_max_gpu": "8",
    }
)
"""Default ``resource_quota`` context for the gco-jobs namespace."""


def parse_k8s_quantity(value: object) -> float:
    """Parse a Kubernetes resource quantity into a float of base units.

    Supports the quantity forms GCO's manifests actually use: bare integers
    and decimals (``8``, ``0.5``), CPU millicores (``250m``), and the binary
    and decimal suffixes (``Ki Mi Gi Ti Pi`` / ``k M G T P``).

    Raises:
        ValueError: If the value is not a parseable quantity.
    """
    text = str(value).strip()
    if not text:
        raise ValueError("empty resource quantity")
    binary = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50}
    decimal = {"k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15}
    for suffix, factor in binary.items():
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    if text.endswith("m"):
        return float(text[:-1]) / 1000.0
    for suffix, factor in decimal.items():
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    return float(text)


DEFAULT_MANIFEST_RESOURCE_CAPS: Mapping[str, object] = MappingProxyType(
    {
        # Front-door budget for one API/SQS-submitted manifest, enforced by
        # the manifest and queue processors before anything reaches the
        # cluster: two full accelerator-node slices, i.e. the canonical
        # two-node distributed-training manifest
        # (examples/efa-distributed-training.yaml). Layering invariant,
        # validated at synth: container_max_* (LimitRange) <= per-manifest
        # cap <= max_* (namespace ResourceQuota) on every dimension — the
        # front door must never reject a manifest whose pods the namespace
        # would admit, and must never accept one it cannot possibly run.
        "max_cpu_per_manifest": "384",
        "max_memory_per_manifest": "4096Gi",
        "max_gpu_per_manifest": 16,
    }
)
"""Default ``job_validation_policy.resource_quotas`` for submitted manifests."""
