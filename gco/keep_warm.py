"""Keep-warm GPU nodes: the pool, its window, and the idle records on Jobs.

A Job opts in with ``gco jobs submit --keep-warm`` (or by carrying the
toleration and node selector below itself). Its pods then land on the
``gpu-keep-warm-pool`` NodePool
(``lambda/kubectl-applier-simple/manifests/47-nodepool-gpu-keep-warm.yaml``),
whose empty nodes Karpenter keeps for the cdk.json ``gpu_keep_warm.window``
after their last pod instead of the 30 seconds of ``gpu-x86-pool``. The next
opted-in pod that fits is scheduled onto the waiting node, so it needs no new
NodeClaim and finds its image cached.

The health monitor writes each idle interval onto the Job whose pod finished
last on the node (:data:`IDLE_ANNOTATION`), and Get Job reports it, so a
caller can account for the idle node time its Jobs caused.

This module holds the shared names, the window validation and the idle-record
format without importing AWS CDK or the Kubernetes client, so the stacks,
``ConfigLoader``, the CLI, admission, the health monitor and the API share one
source.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

#: Node label and taint key of the keep-warm pool, and the value both carry.
KEEP_WARM_LABEL = "gco.aws/keep-warm"
KEEP_WARM_VALUE = "true"
#: The NodePool's name.
KEEP_WARM_NODEPOOL = "gpu-keep-warm-pool"
#: The toleration an opted-in pod carries for the pool's own taint.
KEEP_WARM_TOLERATION: dict[str, str] = {
    "key": KEEP_WARM_LABEL,
    "operator": "Equal",
    "value": KEEP_WARM_VALUE,
    "effect": "NoSchedule",
}
#: The pool's nodes run amd64 NVIDIA GPU instances only.
KEEP_WARM_ARCHITECTURE = "amd64"
KEEP_WARM_GPU_RESOURCE = "nvidia.com/gpu"
#: The pool's instance families, the ones gpu-x86-pool offers.
KEEP_WARM_INSTANCE_FAMILIES = frozenset(
    {"g4dn", "g5", "g6", "g6e", "g6f", "gr6", "gr6f", "g7", "g7e"}
)
#: Every label a keep-warm node carries with a value the pool fixes, mapped to
#: the values it can have: the NodePool's template labels and requirements and
#: the name Karpenter labels its nodes with. A test holds this equal to
#: ``47-nodepool-gpu-keep-warm.yaml``.
KEEP_WARM_NODE_LABELS: dict[str, frozenset[str]] = {
    KEEP_WARM_LABEL: frozenset({KEEP_WARM_VALUE}),
    "karpenter.sh/nodepool": frozenset({KEEP_WARM_NODEPOOL}),
    "workload-type": frozenset({"gpu"}),
    "arch": frozenset({"x86_64"}),
    "project": frozenset({"gco"}),
    "kubernetes.io/arch": frozenset({KEEP_WARM_ARCHITECTURE}),
    "eks.amazonaws.com/instance-gpu-manufacturer": frozenset({"nvidia"}),
    "eks.amazonaws.com/instance-family": KEEP_WARM_INSTANCE_FAMILIES,
    "karpenter.sh/capacity-type": frozenset({"on-demand", "spot"}),
}
_INSTANCE_TYPE_LABEL = "node.kubernetes.io/instance-type"

#: cdk.json context key of the block, and its one setting.
KEEP_WARM_CONTEXT_KEY = "gpu_keep_warm"
KEEP_WARM_DEFAULTS: dict[str, str] = {"window": "10m"}
#: Bounds on the window. Below 30 seconds the pool reclaims faster than
#: gpu-x86-pool; above a day an idle GPU node is a cost nobody meant to keep.
KEEP_WARM_MIN_WINDOW_SECONDS = 30
KEEP_WARM_MAX_WINDOW_SECONDS = 24 * 60 * 60
#: The manifest placeholders the window renders into: the NodePool's
#: ``consolidateAfter`` and the health monitor's environment.
KEEP_WARM_WINDOW_PLACEHOLDER = "{{GPU_KEEP_WARM_WINDOW}}"
KEEP_WARM_WINDOW_SECONDS_PLACEHOLDER = "{{GPU_KEEP_WARM_WINDOW_SECONDS}}"

#: Annotation on a Job: a JSON list of the idle intervals its pods left.
IDLE_ANNOTATION = "gco.aws/keep-warm-idle"
#: Label on a Job while one of its idle intervals is still open, so the
#: health monitor finds them again after a restart.
IDLE_OPEN_LABEL = "gco.aws/keep-warm-idle-open"
#: How an interval ended: another pod took the node, or the node went away.
ENDED_BY_REUSED = "reused"
ENDED_BY_RECLAIMED = "reclaimed"
#: Most intervals kept on one Job; the oldest closed ones are dropped first.
MAX_IDLE_RECORDS = 50

# Karpenter's duration syntax for consolidateAfter, without "Never": a bounded
# window is the point of the pool.
_WINDOW_RE = re.compile(r"(?:[0-9]+[hms])+")
_WINDOW_PART_RE = re.compile(r"([0-9]+)([hms])")
_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}


class KeepWarmConfigError(ValueError):
    """Raised when the ``gpu_keep_warm`` block is invalid."""


def window_seconds(window: str) -> int:
    """Seconds in a duration such as ``10m`` or ``1h30m`` (hours, minutes, seconds)."""
    if not _WINDOW_RE.fullmatch(window):
        raise KeepWarmConfigError(
            f"{KEEP_WARM_CONTEXT_KEY}.window must be a duration of hours, minutes and "
            f"seconds such as '10m' or '1h30m', got {window!r}"
        )
    return sum(
        int(amount) * _UNIT_SECONDS[unit] for amount, unit in _WINDOW_PART_RE.findall(window)
    )


def validate_keep_warm_config(raw: object) -> dict[str, Any]:
    """Return ``{"window": str, "window_seconds": int}`` from the cdk.json block.

    ``None`` (the block is absent) means the defaults. Keys starting with
    ``_comment`` are documentation and are skipped.

    Raises:
        KeepWarmConfigError: The block is not an object, names an unknown key,
            or sets a window that is not a string duration between
            :data:`KEEP_WARM_MIN_WINDOW_SECONDS` and
            :data:`KEEP_WARM_MAX_WINDOW_SECONDS`.
    """
    key = KEEP_WARM_CONTEXT_KEY
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise KeepWarmConfigError(f"{key} must be an object, got {type(raw).__name__}: {raw!r}")
    settings = {
        str(name): value for name, value in raw.items() if not str(name).startswith("_comment")
    }
    unknown = sorted(name for name in settings if name not in KEEP_WARM_DEFAULTS)
    if unknown:
        raise KeepWarmConfigError(
            f"{key} contains unknown key(s): "
            + ", ".join(f"{key}.{name}" for name in unknown)
            + f"; allowed keys: {key}.window"
        )
    window = settings.get("window", KEEP_WARM_DEFAULTS["window"])
    if not isinstance(window, str):
        raise KeepWarmConfigError(
            f"{key}.window must be a string duration such as '10m', got {window!r}"
        )
    seconds = window_seconds(window)
    if not KEEP_WARM_MIN_WINDOW_SECONDS <= seconds <= KEEP_WARM_MAX_WINDOW_SECONDS:
        raise KeepWarmConfigError(
            f"{key}.window must be between {KEEP_WARM_MIN_WINDOW_SECONDS} seconds and "
            f"{KEEP_WARM_MAX_WINDOW_SECONDS // 3600} hours, got {window!r} ({seconds} seconds)"
        )
    return {"window": window, "window_seconds": seconds}


def compute_keep_warm_replacements(config: Mapping[str, Any]) -> dict[str, str]:
    """Render a validated block as the manifests' ``{{TOKEN}}`` replacements."""
    return {
        KEEP_WARM_WINDOW_PLACEHOLDER: str(config["window"]),
        KEEP_WARM_WINDOW_SECONDS_PLACEHOLDER: str(config["window_seconds"]),
    }


def render_keep_warm_defaults(text: str) -> str:
    """Substitute the default window into manifest text, for readers other than the applier."""
    for token, value in compute_keep_warm_replacements(validate_keep_warm_config(None)).items():
        text = text.replace(token, value)
    return text


def keep_warm_selector_conflicts(selector: Mapping[str, Any]) -> list[str]:
    """The ``key=value`` entries of a nodeSelector that no keep-warm node matches.

    A label in :data:`KEEP_WARM_NODE_LABELS` with a value the pool never
    gives it, or an instance type outside :data:`KEEP_WARM_INSTANCE_FAMILIES`.
    Labels the pool does not fix, such as the zone, are not judged.
    """
    conflicts = []
    for key, value in sorted(selector.items(), key=lambda item: str(item[0])):
        allowed = KEEP_WARM_NODE_LABELS.get(str(key))
        if (allowed is not None and str(value) not in allowed) or (
            key == _INSTANCE_TYPE_LABEL
            and str(value).split(".", 1)[0] not in KEEP_WARM_INSTANCE_FAMILIES
        ):
            conflicts.append(f"{key}={value}")
    return conflicts


# ── Idle records ────────────────────────────────────────────────────


def format_time(moment: datetime) -> str:
    """RFC 3339 in UTC with a ``Z`` suffix, second precision, as Kubernetes writes times."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: object) -> datetime | None:
    """A time written by :func:`format_time` (or any ISO 8601 string), else ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def load_idle_records(annotations: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The idle intervals in a Job's :data:`IDLE_ANNOTATION`; ``[]`` when absent or unreadable.

    Only well-formed entries survive: a mapping with a ``node`` string and a
    readable ``since``. A hand-edited or truncated annotation never turns a
    job read into an error.
    """
    raw = (annotations or {}).get(IDLE_ANNOTATION)
    if not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [
        dict(entry)
        for entry in parsed
        if isinstance(entry, dict)
        and isinstance(entry.get("node"), str)
        and parse_time(entry.get("since")) is not None
    ]


def dump_idle_records(records: list[dict[str, Any]]) -> str:
    """Serialize records for the annotation, keeping at most :data:`MAX_IDLE_RECORDS`.

    Open records are always kept; the oldest closed ones go first.
    """
    kept = list(records)
    while len(kept) > MAX_IDLE_RECORDS:
        closed = [index for index, entry in enumerate(kept) if entry.get("until")]
        if not closed:
            break
        del kept[closed[0]]
    return json.dumps(kept, sort_keys=True, separators=(",", ":"))


def keep_warm_summary(annotations: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What Get Job reports: the intervals, their closed total, and whether one is open.

    ``None`` when the Job left no idle interval on a keep-warm node.
    """
    records = load_idle_records(annotations)
    if not records:
        return None
    closed = [entry for entry in records if entry.get("until")]
    return {
        "idle": records,
        "idle_seconds": sum(int(entry.get("seconds") or 0) for entry in closed),
        "open": len(closed) < len(records),
    }
