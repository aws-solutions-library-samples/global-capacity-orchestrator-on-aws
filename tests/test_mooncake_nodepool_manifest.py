"""Dedicated Mooncake EFA NodePool: only GPUs that can serve KV-transfer.

Mooncake role pods (prefill / decode / store) move KV cache over RoCE and must
land on EFA-enabled nodes whose GPUs are large and modern enough to hold a model
plus its KV cache and to run FP8 KV-cache configs. The shared training EFA pool
(``43-nodepool-efa.yaml``) intentionally also offers ``p4d`` — 8x A100 40GB,
Ampere, no hardware FP8 — which is fine for training but too small / too old for
much disaggregated and store-mode serving. When Karpenter is free to pick the
cheapest ``efa=true`` node it can choose ``p4d``, which is the intermittent
"worked on p6, failed on p4d" placement.

``46-nodepool-mooncake-efa.yaml`` removes that failure mode: a separate pool
restricted to >=80GB Hopper/Blackwell families, carrying an extra
``mooncake-efa=true`` label that only it has. ``apply_efa_scheduling`` selects
that label, so role pods can only land here and never on ``p4d``.

These checks lock in the curated instance list, the labels/taints the placement
code relies on, and the agreement between the manifest label and the selector
constants in :mod:`gco.services.inference_monitor`, so the pool and the code
that targets it cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from gco.services.inference_monitor import (
    EFA_NODE_SELECTOR_KEY,
    EFA_NODE_SELECTOR_VALUE,
    EFA_RESOURCE_NAME,
    MOONCAKE_EFA_NODE_SELECTOR_KEY,
    MOONCAKE_EFA_NODE_SELECTOR_VALUE,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MANIFESTS = _REPO_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"
_MOONCAKE_POOL = _MANIFESTS / "46-nodepool-mooncake-efa.yaml"
_TRAINING_EFA_POOL = _MANIFESTS / "43-nodepool-efa.yaml"

# The only families allowed for Mooncake serving: every one is >=80GB of GPU
# memory and FP8-capable (Hopper or Blackwell). p4d (A100 40GB, Ampere) is
# deliberately absent.
_EXPECTED_FAMILIES = {"p5", "p5e", "p5en", "p6-b200", "p6-b300", "p6e-gb200"}
_EXCLUDED_FAMILY = "p4d"


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _requirement(pool: dict[str, Any], key: str) -> dict[str, Any]:
    """Return the single NodePool requirement entry for ``key``."""
    reqs = pool["spec"]["template"]["spec"]["requirements"]
    matches = [r for r in reqs if r["key"] == key]
    assert len(matches) == 1, f"expected exactly one requirement for {key}, got {len(matches)}"
    return matches[0]


def test_pool_is_a_named_nodepool() -> None:
    """The manifest is a Karpenter NodePool named ``mooncake-efa-pool``."""
    pool = _load(_MOONCAKE_POOL)
    assert pool["kind"] == "NodePool"
    assert pool["apiVersion"] == "karpenter.sh/v1"
    assert pool["metadata"]["name"] == "mooncake-efa-pool"


def test_instance_families_are_curated_and_exclude_p4d() -> None:
    """Only the >=80GB FP8-capable families are offered; p4d is excluded."""
    pool = _load(_MOONCAKE_POOL)
    families = set(_requirement(pool, "eks.amazonaws.com/instance-family")["values"])
    assert families == _EXPECTED_FAMILIES
    assert _EXCLUDED_FAMILY not in families


def test_pool_requires_nvidia_gpus() -> None:
    """Karpenter only launches NVIDIA-GPU nodes for this pool."""
    pool = _load(_MOONCAKE_POOL)
    manufacturer = _requirement(pool, "eks.amazonaws.com/instance-gpu-manufacturer")
    assert manufacturer["values"] == ["nvidia"]


def test_pool_carries_efa_and_mooncake_labels_matching_the_selector_constants() -> None:
    """The labels the placement code selects on are present with matching values.

    ``apply_efa_scheduling`` adds ``efa=<value>`` and ``mooncake-efa=<value>``
    node selectors using the constants below; the pool must advertise the exact
    same key/value pairs or pods would never schedule onto it.
    """
    pool = _load(_MOONCAKE_POOL)
    labels = pool["spec"]["template"]["metadata"]["labels"]
    assert labels[EFA_NODE_SELECTOR_KEY] == EFA_NODE_SELECTOR_VALUE
    assert labels[MOONCAKE_EFA_NODE_SELECTOR_KEY] == MOONCAKE_EFA_NODE_SELECTOR_VALUE


def test_pool_taints_gate_gpu_and_efa() -> None:
    """Both the GPU and EFA taints are present so only tolerating pods land here."""
    pool = _load(_MOONCAKE_POOL)
    taint_keys = {t["key"] for t in pool["spec"]["template"]["spec"]["taints"]}
    assert "nvidia.com/gpu" in taint_keys
    assert EFA_RESOURCE_NAME in taint_keys


def test_consolidation_does_not_disrupt_in_flight_serving() -> None:
    """Nodes are only consolidated once empty, matching the inference pools."""
    pool = _load(_MOONCAKE_POOL)
    assert pool["spec"]["disruption"]["consolidationPolicy"] == "WhenEmpty"


def test_training_efa_pool_still_offers_p4d_without_the_mooncake_label() -> None:
    """The split is intentional and the mooncake label stays unique to pool 46.

    Curating the mooncake pool must not remove p4d from distributed training,
    where 40GB A100s are a legitimate, cheaper EFA option. And the training pool
    must NOT carry ``mooncake-efa``: that label is what keeps role pods on pool
    46 only — if pool 43 grew it, pods could land on its p4d nodes again.
    """
    training = _load(_TRAINING_EFA_POOL)
    families = set(_requirement(training, "eks.amazonaws.com/instance-family")["values"])
    assert _EXCLUDED_FAMILY in families

    training_labels = training["spec"]["template"]["metadata"].get("labels", {})
    assert MOONCAKE_EFA_NODE_SELECTOR_KEY not in training_labels
