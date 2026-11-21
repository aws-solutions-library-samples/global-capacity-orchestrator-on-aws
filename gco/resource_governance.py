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
from decimal import ROUND_CEILING
from types import MappingProxyType

from gco.k8s_quantity import parse_quantity

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

    Any quantity the API server accepts, string or number (``8``, ``0.5``,
    ``250m``, ``32Gi``, ``1e9``), through :func:`gco.k8s_quantity.parse_quantity`.
    The sign is kept, so a caller can name a negative configuration value in
    its own error.

    Raises:
        ValueError: If the value is not a parseable quantity.
    """
    return float(parse_quantity(value))


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

#: The most EFA interfaces an instance type pairs with each accelerator: a
#: p5.48xlarge attaches 32 to its 8 GPUs, a trn1n.32xlarge or trn2.48xlarge 16
#: to its 16 Neuron devices.
EFA_INTERFACES_PER_GPU = 4
EFA_INTERFACES_PER_NEURON_DEVICE = 1


def kueue_efa_quota(accelerator_quota: object) -> str:
    """The ``vpc.amazonaws.com/efa`` nominal quota of a shipped Kueue ClusterQueue.

    Kueue never admits a workload that requests a resource its ClusterQueue
    does not cover, so the shipped queues cover EFA next to the accelerators,
    and give ``nvidia.com/gpu`` and ``aws.amazon.com/neuron`` the same nominal
    quota, ``accelerator_quota``. EFA interfaces are a node's network devices,
    not a budget of their own: the quota is the most interfaces the instance
    types pair with that many GPUs plus that many Neuron devices, so the
    accelerator quotas are always reached first.

    Raises:
        ValueError: If ``accelerator_quota`` is not a quantity.
    """
    per_accelerator = EFA_INTERFACES_PER_GPU + EFA_INTERFACES_PER_NEURON_DEVICE
    interfaces = parse_quantity(accelerator_quota) * per_accelerator
    return str(int(interfaces.to_integral_value(rounding=ROUND_CEILING)))


#: (container ceiling, namespace ceiling) pairs the resource-quota invariant
#: compares; every dimension a container can request must fit the namespace.
RESOURCE_QUOTA_INVARIANTS = (
    ("container_max_cpu", "max_cpu"),
    ("container_max_memory", "max_memory"),
    ("container_max_gpu", "max_gpu"),
)


def validated_resource_quota(raw: object) -> dict[str, str]:
    """Merge the ``resource_quota`` context over defaults and validate it.

    The values are substituted verbatim into the gco-jobs ResourceQuota and
    LimitRange manifests, where a typo or an incoherent pair (a per-container
    ceiling that exceeds the namespace ceiling) previously deployed silently
    and only surfaced as pods being forbidden at admission — with the reason
    visible in namespace events alone. Fail the synth instead.

    The one merge both readers share: the regional stack renders the
    result into the manifests, and ``ConfigLoader`` validates it and checks
    the Slurm partitions against it, so an absent ``resource_quota`` is
    checked against the defaults that deploy. ``_comment*`` keys are
    documentation and are skipped.

    Raises:
        ValueError: If the context is not a mapping, carries an unknown key,
            a value that does not parse as a Kubernetes quantity, or a
            per-container ceiling exceeding its namespace ceiling.
    """
    if not isinstance(raw, dict):
        raise ValueError(
            f"cdk.json context 'resource_quota' must be an object, got {type(raw).__name__}"
        )
    unknown = sorted(
        str(key)
        for key in raw
        if key not in DEFAULT_RESOURCE_QUOTA and not str(key).startswith("_comment")
    )
    if unknown:
        allowed = ", ".join(sorted(DEFAULT_RESOURCE_QUOTA))
        raise ValueError(
            f"cdk.json context 'resource_quota' has unknown key(s) {unknown}; allowed: {allowed}"
        )
    merged = {key: str(raw.get(key, default)) for key, default in DEFAULT_RESOURCE_QUOTA.items()}
    parsed: dict[str, float] = {}
    for key, value in merged.items():
        try:
            parsed[key] = parse_k8s_quantity(value)
        except ValueError as exc:
            raise ValueError(
                f"resource_quota.{key}={value!r} is not a valid Kubernetes quantity"
            ) from exc
        if parsed[key] < 0:
            raise ValueError(f"resource_quota.{key}={value!r} must not be negative")
    for container_key, namespace_key in RESOURCE_QUOTA_INVARIANTS:
        if parsed[container_key] > parsed[namespace_key]:
            raise ValueError(
                f"resource_quota.{container_key}={merged[container_key]!r} exceeds "
                f"resource_quota.{namespace_key}={merged[namespace_key]!r}: a container "
                "that passes the LimitRange could never be admitted by the namespace "
                "ResourceQuota"
            )
    return merged
