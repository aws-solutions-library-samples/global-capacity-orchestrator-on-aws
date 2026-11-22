"""Record how long each keep-warm GPU node sat idle, on the Job that left it idle.

Runs in the health monitor, on every health loop, in the replica holding the
``gco-health-monitor-keep-warm`` Lease (pre-created by ``02-rbac.yaml``). One
pass lists the ``gco.aws/keep-warm`` nodes, the pods bound to each, and the
Jobs that still have an open interval, then:

* rebuilds each node's idle gaps from the pods it still lists (DaemonSet and
  mirror pods aside): a gap starts when the pod that finished last finished
  and ends when the next pod was bound, so a gap shorter than one loop is
  found as well as one that is still going on;
* records each gap not recorded yet on the Job of the pod that started it;
* closes an open one when the next pod is bound to the node
  (``ended_by: reused``, ending at that pod's scheduling time) or when the
  node is gone (``ended_by: reclaimed``, ending at the pass that found it
  gone, so at most one loop interval after the instance was terminated).

The intervals live in the Job's ``gco.aws/keep-warm-idle`` annotation
(:mod:`gco.keep_warm`), which Get Job reports, and the Job carries the
``gco.aws/keep-warm-idle-open`` label while one is open so a new leader
finds them again. Writes are JSON merge patches carrying the Job's
``resourceVersion``, so a stale former leader's write fails with 409 instead
of overwriting a newer record. A Job outside the namespaces the health monitor
may patch, a bare Pod, and a Job deleted before its interval closed get no
record.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gco.keep_warm import (
    ENDED_BY_RECLAIMED,
    ENDED_BY_REUSED,
    IDLE_ANNOTATION,
    IDLE_OPEN_LABEL,
    KEEP_WARM_LABEL,
    KEEP_WARM_VALUE,
    dump_idle_records,
    format_time,
    load_idle_records,
    parse_time,
)
from gco.services.leader_lease import LeaseElector
from gco.services.structured_logging import sanitize_log_value

logger = logging.getLogger(__name__)

_INSTANCE_TYPE_LABEL = "node.kubernetes.io/instance-type"
_CAPACITY_TYPE_LABEL = "karpenter.sh/capacity-type"
_MIRROR_POD_ANNOTATION = "kubernetes.io/config.mirror"
_TERMINAL_PHASES = ("Succeeded", "Failed")


@dataclass(frozen=True)
class PodFacts:
    """What the recorder needs to know about one workload pod on a keep-warm node."""

    namespace: str
    name: str
    #: The owning Job's name and UID, or ``None`` for a pod no Job owns.
    job: str | None
    job_uid: str | None
    #: When the pod was bound to the node.
    started_at: datetime | None
    #: When its last container finished; ``None`` while it is not terminal.
    finished_at: datetime | None


@dataclass(frozen=True)
class IdleGap:
    """A stretch with no workload pod on a node, after one had run there."""

    #: The pod that finished last before the gap, which left the node idle.
    predecessor: PodFacts
    #: The predecessor's finish.
    since: datetime
    #: When the next pod was bound to the node; ``None`` while it is still idle.
    until: datetime | None


def _finish_order(pod: PodFacts) -> tuple[bool, datetime, str, str]:
    """Sort key by finish, a pod still running last; ties broken by name."""
    return (
        pod.finished_at is None,
        pod.finished_at or datetime.min.replace(tzinfo=UTC),
        pod.namespace,
        pod.name,
    )


@dataclass(frozen=True)
class NodeObservation:
    """One keep-warm node as one pass saw it."""

    name: str
    instance_type: str | None
    capacity_type: str | None
    pods: tuple[PodFacts, ...]

    @property
    def busy(self) -> bool:
        """A pod other than a DaemonSet or mirror pod is still running on it."""
        return any(pod.finished_at is None for pod in self.pods)

    def idle_gaps(self) -> list[IdleGap]:
        """Every gap between the workload pods the node still lists, oldest first.

        The node is busy from each pod's binding to its finish, for good while
        the pod runs. A gap starts when the pod that finished last finished and
        ends when the next pod was bound; the last gap stays open while the
        node is idle. A pod deleted since it ran is not listed, so a gap is
        only found while the pods around it last.
        """
        gaps: list[IdleGap] = []
        holder: PodFacts | None = None
        bound = sorted(
            ((pod.started_at, pod) for pod in self.pods if pod.started_at is not None),
            key=lambda item: (item[0], item[1].namespace, item[1].name),
        )
        for started_at, pod in bound:
            if (
                holder is not None
                and holder.finished_at is not None
                and started_at > holder.finished_at
            ):
                gaps.append(IdleGap(holder, holder.finished_at, started_at))
            if holder is None or _finish_order(pod) > _finish_order(holder):
                holder = pod
        if holder is not None and holder.finished_at is not None and not self.busy:
            gaps.append(IdleGap(holder, holder.finished_at, None))
        return gaps

    def first_start_after(self, since: datetime, *, excluding: str | None) -> datetime | None:
        """When the first pod bound at or after ``since`` (not ``excluding``) took the node."""
        starts = [
            pod.started_at
            for pod in self.pods
            if pod.started_at is not None
            and pod.started_at >= since
            and f"{pod.namespace}/{pod.name}" != excluding
        ]
        return min(starts) if starts else None


@dataclass
class RecorderPass:
    """What one pass did, for the log line and the tests."""

    held: bool = False
    nodes: int = 0
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _labels(obj: Any) -> dict[str, str]:
    metadata = getattr(obj, "metadata", None)
    labels = getattr(metadata, "labels", None) if metadata is not None else None
    return (
        {k: v for k, v in labels.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(labels, dict)
        else {}
    )


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _pod_condition_time(
    status: Any, condition_type: str, *, condition_status: str | None = "True"
) -> datetime | None:
    """When a pod condition last changed; ``condition_status=None`` takes any status."""
    for condition in getattr(status, "conditions", None) or []:
        if getattr(condition, "type", None) == condition_type and (
            condition_status is None or getattr(condition, "status", None) == condition_status
        ):
            return _as_utc(getattr(condition, "last_transition_time", None))
    return None


def pod_facts(pod: Any) -> PodFacts | None:
    """The facts of a workload pod; ``None`` for DaemonSet and mirror pods."""
    metadata = getattr(pod, "metadata", None)
    if metadata is None:
        return None
    annotations = getattr(metadata, "annotations", None) or {}
    if isinstance(annotations, dict) and _MIRROR_POD_ANNOTATION in annotations:
        return None
    job: str | None = None
    job_uid: str | None = None
    for owner in getattr(metadata, "owner_references", None) or []:
        kind = getattr(owner, "kind", None)
        if kind == "DaemonSet":
            return None
        if kind == "Job" and job is None:
            job = getattr(owner, "name", None)
            job_uid = getattr(owner, "uid", None)
    status = getattr(pod, "status", None)
    started_at = (
        _pod_condition_time(status, "PodScheduled")
        or _as_utc(getattr(status, "start_time", None))
        or _as_utc(getattr(metadata, "creation_timestamp", None))
    )
    finished_at: datetime | None = None
    if getattr(status, "phase", None) in _TERMINAL_PHASES:
        finishes = [
            _as_utc(
                getattr(getattr(getattr(c, "state", None), "terminated", None), "finished_at", None)
            )
            for c in getattr(status, "container_statuses", None) or []
        ]
        known = [moment for moment in finishes if moment is not None]
        # A pod that failed before any container ran has no container finish
        # time; it stopped being Ready (or never was) when it went terminal.
        finished_at = (
            max(known)
            if known
            else _pod_condition_time(status, "Ready", condition_status=None) or started_at
        )
    return PodFacts(
        namespace=str(getattr(metadata, "namespace", "") or ""),
        name=str(getattr(metadata, "name", "") or ""),
        job=job if isinstance(job, str) and job else None,
        job_uid=job_uid if isinstance(job_uid, str) and job_uid else None,
        started_at=started_at,
        finished_at=finished_at,
    )


def observe_node(node: Any, pods: Iterable[Any]) -> NodeObservation:
    """A keep-warm node and the workload pods bound to it."""
    labels = _labels(node)
    facts = tuple(fact for fact in (pod_facts(pod) for pod in pods) if fact is not None)
    return NodeObservation(
        name=str(getattr(getattr(node, "metadata", None), "name", "") or ""),
        instance_type=labels.get(_INSTANCE_TYPE_LABEL),
        capacity_type=labels.get(_CAPACITY_TYPE_LABEL),
        pods=facts,
    )


def open_record(
    observation: NodeObservation, gap: IdleGap, *, window_seconds: int
) -> dict[str, Any]:
    """The record of one idle gap, open until :func:`close_records` ends it."""
    return {
        "node": observation.name,
        "instance_type": observation.instance_type,
        "capacity_type": observation.capacity_type,
        "pod": gap.predecessor.name,
        "since": format_time(gap.since),
        "window_seconds": window_seconds,
    }


def close_records(
    records: list[dict[str, Any]],
    observations: dict[str, NodeObservation],
    *,
    now: datetime,
    namespace: str,
) -> list[str]:
    """Close every open record whose node was reused or is gone; return their nodes."""
    closed: list[str] = []
    for entry in records:
        if entry.get("until"):
            continue
        since = parse_time(entry.get("since"))
        if since is None:  # pragma: no cover - load_idle_records drops these
            continue
        observation = observations.get(str(entry["node"]))
        if observation is None:
            until, ended_by = now, ENDED_BY_RECLAIMED
        else:
            pod = entry.get("pod")
            start = observation.first_start_after(
                since, excluding=f"{namespace}/{pod}" if isinstance(pod, str) else None
            )
            if start is None:
                continue
            until, ended_by = start, ENDED_BY_REUSED
        until = max(until, since)
        entry["until"] = format_time(until)
        entry["seconds"] = int((until - since).total_seconds())
        entry["ended_by"] = ended_by
        closed.append(str(entry["node"]))
    return closed


class KeepWarmRecorder:
    """The elected writer of keep-warm idle intervals onto Jobs."""

    def __init__(
        self,
        core_v1: Any,
        batch_v1: Any,
        coordination_v1: Any,
        *,
        elector: LeaseElector,
        window_seconds: int,
        job_namespaces: Collection[str],
        request_timeout: int = 30,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.core_v1 = core_v1
        self.batch_v1 = batch_v1
        self.coordination_v1 = coordination_v1
        self.elector = elector
        self.window_seconds = window_seconds
        self.job_namespaces = frozenset(job_namespaces)
        self.request_timeout = request_timeout
        self._clock = clock
        # Namespaces whose Jobs this service account may not read or patch,
        # logged once.
        self._refused_namespaces: set[str] = set()
        # The gaps already on their Jobs, or settled without a record (the Job
        # is gone, replaced or not ours to read), as (node, pod, since), so a
        # pass reads a Job only for a gap it has not seen. Each pass keeps only
        # the gaps it still found, so the set stays as small as the pod lists.
        self._settled: set[tuple[str, str, str]] = set()

    def run_once(self) -> RecorderPass:
        """One pass; writes only while this replica holds the Lease. Never raises."""
        result = RecorderPass()
        try:
            result.held = self.elector.try_acquire(self.coordination_v1).held
            if result.held:
                self._record(result)
        except Exception as exc:
            result.errors.append(f"{type(exc).__name__}: {exc}")
            logger.warning(
                "Keep-warm idle recording failed (non-fatal): %s", sanitize_log_value(exc)
            )
        if result.opened or result.closed:
            logger.info(
                "Keep-warm idle intervals: opened on %s, closed on %s",
                ", ".join(result.opened) or "none",
                ", ".join(result.closed) or "none",
            )
        return result

    def release(self) -> bool:
        """Give the Lease up on shutdown so the other replica takes over at once."""
        try:
            return self.elector.release(self.coordination_v1)
        except Exception as exc:
            logger.warning("Could not release the keep-warm Lease: %s", sanitize_log_value(exc))
            return False

    def _record(self, result: RecorderPass) -> None:
        now = self._clock()
        nodes = self.core_v1.list_node(
            label_selector=f"{KEEP_WARM_LABEL}={KEEP_WARM_VALUE}",
            _request_timeout=self.request_timeout,
        ).items
        observations: dict[str, NodeObservation] = {}
        for node in nodes:
            name = getattr(getattr(node, "metadata", None), "name", None)
            if not isinstance(name, str) or not name:
                continue
            pods = self.core_v1.list_pod_for_all_namespaces(
                field_selector=f"spec.nodeName={name}",
                _request_timeout=self.request_timeout,
            ).items
            observations[name] = observe_node(node, pods)
        result.nodes = len(observations)

        # Jobs that still have an open interval, then the Jobs of the pods
        # that started a gap not recorded yet.
        jobs: dict[tuple[str, str], Any] = {}
        for job in self.batch_v1.list_job_for_all_namespaces(
            label_selector=f"{IDLE_OPEN_LABEL}=true",
            _request_timeout=self.request_timeout,
        ).items:
            metadata = job.metadata
            if metadata.namespace in self.job_namespaces:
                jobs[(metadata.namespace, metadata.name)] = job
        records: dict[tuple[str, str], list[dict[str, Any]]] = {
            key: load_idle_records(job.metadata.annotations) for key, job in jobs.items()
        }
        changed: set[tuple[str, str]] = set()
        # The gaps each Job's write records, settled once the write lands.
        pending: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
        found: set[tuple[str, str, str]] = set()
        # This pass's reads of Jobs without an open interval, one per Job.
        reads: dict[tuple[str, str], tuple[Any | None, bool]] = {}
        for observation in observations.values():
            for gap in observation.idle_gaps():
                predecessor = gap.predecessor
                if predecessor.job is None or predecessor.namespace not in self.job_namespaces:
                    continue
                since = format_time(gap.since)
                seen = (observation.name, f"{predecessor.namespace}/{predecessor.name}", since)
                found.add(seen)
                if seen in self._settled:
                    continue
                key = (predecessor.namespace, predecessor.job)
                job = jobs.get(key)
                if job is None:
                    if key not in reads:
                        reads[key] = self._read_job(*key, result=result)
                    job, final = reads[key]
                    if job is None:
                        if final:
                            self._settled.add(seen)
                        continue
                if job.metadata.uid != predecessor.job_uid:
                    # The pod belonged to an earlier Job of the same name.
                    self._settled.add(seen)
                    continue
                if key not in jobs:
                    jobs[key] = job
                    records[key] = load_idle_records(job.metadata.annotations)
                if any(
                    existing["node"] == observation.name and existing["since"] == since
                    for existing in records[key]
                ):
                    self._settled.add(seen)
                    continue
                records[key].append(
                    open_record(observation, gap, window_seconds=self.window_seconds)
                )
                changed.add(key)
                pending.setdefault(key, []).append(seen)
                result.opened.append(f"{observation.name} ({key[0]}/{key[1]})")

        for key, entries in records.items():
            closed = close_records(entries, observations, now=now, namespace=key[0])
            if closed:
                changed.add(key)
                result.closed.extend(f"{node} ({key[0]}/{key[1]})" for node in closed)

        for key in sorted(changed):
            if self._write(jobs[key], records[key], result):
                self._settled.update(pending.get(key, ()))
        self._settled &= found

    def _note_refusal(self, namespace: str) -> None:
        """Log once per namespace that the Role there is missing."""
        if namespace in self._refused_namespaces:
            return
        self._refused_namespaces.add(namespace)
        logger.warning(
            "The health monitor may not read or patch Jobs in %s, so their keep-warm idle "
            "intervals are not recorded; bind gco-health-monitor-keep-warm-records there "
            "(docs/GPU_KEEP_WARM.md)",
            sanitize_log_value(namespace),
        )

    def _read_job(
        self, namespace: str, name: str, *, result: RecorderPass
    ) -> tuple[Any | None, bool]:
        """The Job, and whether a failed read is final (404 or 403) rather than worth a retry."""
        try:
            job = self.batch_v1.read_namespaced_job(
                name, namespace, _request_timeout=self.request_timeout
            )
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 403:
                self._note_refusal(namespace)
            if status != 404:
                result.errors.append(f"{namespace}/{name}: {status or type(exc).__name__}")
            return None, status in (403, 404)
        return job, True

    def _write(self, job: Any, entries: list[dict[str, Any]], result: RecorderPass) -> bool:
        """Patch the records onto the Job; whether that settles them.

        A write that landed settles them, and so does one refused for good: a
        403 (no Role in that namespace) or a 404 (the Job is gone). A 409 (the
        Job changed since it was read) or any other failure is retried on the
        next pass from a fresh read.
        """
        metadata = job.metadata
        still_open = any(not entry.get("until") for entry in entries)
        body = {
            "metadata": {
                "resourceVersion": metadata.resource_version,
                "annotations": {IDLE_ANNOTATION: dump_idle_records(entries)},
                "labels": {IDLE_OPEN_LABEL: "true" if still_open else None},
            }
        }
        try:
            self.batch_v1.patch_namespaced_job(
                metadata.name,
                metadata.namespace,
                body=body,
                _content_type="application/merge-patch+json",
                _request_timeout=self.request_timeout,
            )
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 403:
                self._note_refusal(metadata.namespace)
            result.errors.append(
                f"{metadata.namespace}/{metadata.name}: {status or type(exc).__name__}"
            )
            return status in (403, 404)
        return True
