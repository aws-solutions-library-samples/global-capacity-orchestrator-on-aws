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

This module holds the shared names, what a keep-warm node offers a pod's node
selector and node affinity, the window validation and the idle-record format
without importing AWS CDK or the Kubernetes client, so the stacks,
``ConfigLoader``, the CLI, admission, the health monitor and the API share one
source.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, NamedTuple

from gco.hyperpod_config import CAPACITY_PROVIDER_LABEL

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
#: The node labels that name an instance type or its family, the two whose
#: values the pool limits to :data:`KEEP_WARM_INSTANCE_FAMILIES`.
KEEP_WARM_INSTANCE_LABELS = frozenset({_INSTANCE_TYPE_LABEL, "eks.amazonaws.com/instance-family"})

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


#: The operators of a node selector requirement (a ``matchExpressions`` entry).
_NODE_SELECTOR_OPERATORS = ("In", "NotIn", "Exists", "DoesNotExist", "Gt", "Lt")
#: A ``Gt`` or ``Lt`` operand as Kubernetes parses one (Go's ``strconv.ParseInt``
#: in base 10): an optional sign and ASCII digits, within 64 bits.
_INTEGER_RE = re.compile(r"[+-]?[0-9]+")
_INT64_LIMIT = 2**63


class NodeSelectorRequirement(NamedTuple):
    """One well-formed ``matchExpressions`` entry of a node selector term."""

    key: str
    operator: str
    #: Empty for ``Exists`` and ``DoesNotExist``, which take no values.
    values: tuple[str, ...]


def keep_warm_affinity_conflicts(affinity: object) -> list[list[NodeSelectorRequirement]]:
    """The terms of a pod's required node affinity that no keep-warm node satisfies.

    *affinity* is a pod spec's ``affinity``. Kubernetes ORs the
    ``nodeSelectorTerms`` of ``nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution``
    and ANDs the ``matchExpressions`` within a term, so a keep-warm node can
    take the pod when one term holds there. When no term does, the result has
    one entry per term: its expressions no keep-warm node satisfies, or ``[]``
    for a term with neither ``matchExpressions`` nor ``matchFields``, which
    matches no node. Otherwise the result is ``[]``, as it is for a pod that
    requires no node affinity.

    Each expression is judged on its own label, as
    :func:`keep_warm_selector_conflicts` judges a nodeSelector entry: a label in
    :data:`KEEP_WARM_NODE_LABELS` always has one of the values listed there,
    the instance type belongs to one of :data:`KEEP_WARM_INSTANCE_FAMILIES`, and
    SageMaker HyperPod's capacity-provider label, which only HyperPod nodes
    carry, is absent. Expressions on other labels pass, and so do
    ``matchFields`` and the preferred terms, which are not read. So does an
    expression the API server refuses for its shape (not a mapping, no key, an
    unknown operator, values that are not a list of strings where the operator
    reads them): admission leaves it to the API. Expressions are judged one at
    a time, so two that contradict each other on one label are not caught.
    """
    unsatisfiable = []
    for term in _required_node_selector_terms(affinity):
        conflicts = _term_conflicts(term)
        if conflicts is None:
            return []
        unsatisfiable.append(conflicts)
    return unsatisfiable


def affinity_selects_keep_warm(affinity: object) -> bool:
    """Whether a pod's required node affinity lets it run on keep-warm nodes only.

    True when every term requires :data:`KEEP_WARM_LABEL`, by ``In`` with
    :data:`KEEP_WARM_VALUE` among its values or by ``Exists``: no other node
    carries the label.
    """
    terms = _required_node_selector_terms(affinity)
    return bool(terms) and all(_term_requires_keep_warm(term) for term in terms)


def _required_node_selector_terms(affinity: object) -> list[Any]:
    """A pod's required node-affinity terms; ``[]`` when it requires none or the shape is wrong."""
    node_affinity = affinity.get("nodeAffinity") if isinstance(affinity, Mapping) else None
    required = (
        node_affinity.get("requiredDuringSchedulingIgnoredDuringExecution")
        if isinstance(node_affinity, Mapping)
        else None
    )
    terms = required.get("nodeSelectorTerms") if isinstance(required, Mapping) else None
    return terms if isinstance(terms, list) else []


def _node_selector_requirement(expression: object) -> NodeSelectorRequirement | None:
    """A ``matchExpressions`` entry, or ``None`` when the API server would refuse its shape."""
    if not isinstance(expression, Mapping):
        return None
    key = expression.get("key")
    operator = expression.get("operator")
    values = expression.get("values")
    if not isinstance(key, str) or operator not in _NODE_SELECTOR_OPERATORS:
        return None
    if operator in ("Exists", "DoesNotExist"):
        return NodeSelectorRequirement(key, operator, ())
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return None
    return NodeSelectorRequirement(key, operator, tuple(values))


def _term_conflicts(term: object) -> list[NodeSelectorRequirement] | None:
    """A term's expressions no keep-warm node satisfies; ``None`` when one may satisfy the term.

    A term that is not a mapping, or whose expression lists are not lists, has
    a shape the API server refuses, so it is not judged and counts as one a
    keep-warm node may satisfy.
    """
    if not isinstance(term, Mapping):
        return None
    expressions = term.get("matchExpressions")
    fields = term.get("matchFields")
    if any(part is not None and not isinstance(part, list) for part in (expressions, fields)):
        return None
    if not expressions and not fields:
        return []
    conflicts = [
        requirement
        for requirement in map(_node_selector_requirement, expressions or [])
        if requirement is not None and not _keep_warm_node_satisfies(requirement)
    ]
    return conflicts or None


def _term_requires_keep_warm(term: object) -> bool:
    """Whether a term requires :data:`KEEP_WARM_LABEL` as :func:`affinity_selects_keep_warm` says."""
    expressions = term.get("matchExpressions") if isinstance(term, Mapping) else None
    return isinstance(expressions, list) and any(
        requirement is not None
        and requirement.key == KEEP_WARM_LABEL
        and (
            requirement.operator == "Exists"
            or (requirement.operator == "In" and KEEP_WARM_VALUE in requirement.values)
        )
        for requirement in map(_node_selector_requirement, expressions)
    )


def _keep_warm_node_satisfies(requirement: NodeSelectorRequirement) -> bool:
    """Whether some keep-warm node satisfies one expression, judged on its label alone."""
    key, operator, values = requirement
    if key == CAPACITY_PROVIDER_LABEL:
        # Only SageMaker HyperPod nodes carry it.
        return operator in ("NotIn", "DoesNotExist")
    if key == _INSTANCE_TYPE_LABEL:
        # Every keep-warm node has an instance type in one of the pool's
        # families, which hold dozens of types, so a NotIn list leaves one. No
        # instance type is a number, so Gt and Lt never hold.
        if operator == "In":
            return any(value.split(".", 1)[0] in KEEP_WARM_INSTANCE_FAMILIES for value in values)
        return operator in ("NotIn", "Exists")
    allowed = KEEP_WARM_NODE_LABELS.get(key)
    if allowed is None:
        return True
    if operator == "In":
        return not allowed.isdisjoint(values)
    if operator == "NotIn":
        return not allowed.issubset(values)
    if operator in ("Exists", "DoesNotExist"):
        return operator == "Exists"
    # Gt or Lt: Kubernetes compares exactly one integer operand with the
    # label's value read as an integer, and any other operand matches no node.
    bound = _label_integer(values[0]) if len(values) == 1 else None
    if bound is None:
        return False
    numbers = [number for number in map(_label_integer, allowed) if number is not None]
    return any(number > bound if operator == "Gt" else number < bound for number in numbers)


def _label_integer(value: str) -> int | None:
    """*value* read as Kubernetes reads a ``Gt`` or ``Lt`` operand or label value, else ``None``."""
    if not _INTEGER_RE.fullmatch(value):
        return None
    number = int(value)
    return number if -_INT64_LIMIT <= number < _INT64_LIMIT else None


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
