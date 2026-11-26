"""Shape API records into the tables the dashboard draws.

Pure functions from the dicts and dataclasses :mod:`cli.dashboard.data`
returns to ``pandas`` frames: no I/O and no Streamlit, so each is tested with
plain fixtures. Times are timezone-aware UTC throughout.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from cli.dashboard.data import RegionRead, Topology
from cli.dashboard.regions import region_info
from cli.jobs import JobInfo

#: Label the central job queue stamps on the Kubernetes Jobs it creates.
MANAGED_BY_LABEL = "gco.io/managed-by"

JOB_COLUMNS = [
    "name",
    "namespace",
    "region",
    "status",
    "created",
    "started",
    "finished",
    "runtime_s",
    "active",
    "succeeded",
    "failed",
    "completions",
    "progress",
    "image",
    "team",
    "labels",
    "source",
    "instance_type",
    "capacity_type",
    "uid",
]


def as_dict(value: object) -> dict[str, Any]:
    """``value`` when it is a JSON object, else an empty dict (API fields can be null)."""
    return dict(value) if isinstance(value, Mapping) else {}


def target_regions(endpoint: Mapping[str, Any]) -> list[str]:
    """An inference endpoint's ``target_regions`` as Region names.

    The registry sends a list. Anything else (null, a bare string, a number)
    reads as no Regions rather than as one Region per character or a crash.
    """
    value = endpoint.get("target_regions")
    return [str(region) for region in value] if isinstance(value, list | tuple) else []


def _timestamp(value: object) -> datetime | None:
    """An ISO timestamp, datetime or epoch as an aware UTC datetime, or None if unreadable."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except ValueError, OverflowError, OSError:
            # NaN (how pandas fills a row that lacks the field) or an epoch
            # the platform cannot represent, such as epoch milliseconds.
            return None
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _utc_series(values: Sequence[datetime | None]) -> Any:
    """A datetime column that stays ``datetime64[ns, UTC]`` even when empty."""
    return pd.to_datetime(pd.Series(list(values), dtype="object"), utc=True).dt.as_unit("ns")


def jobs_frame(jobs: Iterable[JobInfo], *, now: datetime | None = None) -> Any:
    """One row per Kubernetes Job, newest first."""
    current = now or datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for job in jobs:
        # A finished Job with no recorded end (a bridge that predates
        # reporting it) has an unknown runtime, not one that grows forever.
        live = job.status not in ("succeeded", "failed")
        end = job.completion_time or (current if job.start_time and live else None)
        runtime = (end - job.start_time).total_seconds() if job.start_time and end else None
        completions = max(int(job.completions or 1), 1)
        rows.append(
            {
                "name": job.name,
                "namespace": job.namespace,
                "region": job.region,
                "status": job.status,
                "created": job.created_time,
                "started": job.start_time,
                "finished": job.completion_time,
                "runtime_s": runtime,
                "active": int(job.active_pods or 0),
                "succeeded": int(job.succeeded_pods or 0),
                "failed": int(job.failed_pods or 0),
                "completions": completions,
                "progress": min(int(job.succeeded_pods or 0) / completions, 1.0),
                "image": job.image_refs[0] if job.image_refs else "",
                "team": str(job.labels.get("team", "")),
                "labels": [f"{key}={value}" for key, value in sorted(job.labels.items())],
                "source": (
                    "central queue"
                    if job.labels.get(MANAGED_BY_LABEL) == "central-queue"
                    else "direct"
                ),
                "instance_type": job.node_instance_type or "",
                "capacity_type": job.node_capacity_type or "",
                "uid": job.uid or "",
            }
        )
    frame = pd.DataFrame(rows, columns=JOB_COLUMNS)
    frame["runtime_s"] = pd.to_numeric(frame["runtime_s"], errors="coerce").astype("float64")
    for column in ("created", "started", "finished"):
        frame[column] = _utc_series(list(frame[column]))
    return frame.sort_values("created", ascending=False, na_position="last").reset_index(drop=True)


def filter_jobs(
    frame: Any,
    *,
    regions: Sequence[str] = (),
    statuses: Sequence[str] = (),
    namespaces: Sequence[str] = (),
    text: str = "",
) -> Any:
    """Rows matching every given filter; an empty filter matches everything."""
    mask = pd.Series(True, index=frame.index)
    if regions:
        mask &= frame["region"].isin(list(regions))
    if statuses:
        mask &= frame["status"].isin(list(statuses))
    if namespaces:
        mask &= frame["namespace"].isin(list(namespaces))
    needle = text.strip().lower()
    if needle:
        haystack = frame["name"].str.lower() + " " + frame["labels"].map(" ".join).str.lower()
        mask &= haystack.str.contains(needle, regex=False)
    return frame[mask].reset_index(drop=True)


def filter_pairs(frame: Any, pairs: Sequence[Mapping[str, Any]]) -> Any:
    """Rows whose ``(region, status)`` is one the status chart's selection holds."""
    wanted = {(str(p.get("region")), str(p.get("status"))) for p in pairs if isinstance(p, Mapping)}
    if not wanted:
        return frame
    mask = [
        (region, status) in wanted
        for region, status in zip(frame["region"], frame["status"], strict=True)
    ]
    return frame[pd.Series(mask, index=frame.index, dtype=bool)].reset_index(drop=True)


def status_counts(frame: Any) -> Any:
    """Jobs per Region and status, for the stacked status chart."""
    if frame.empty:
        return pd.DataFrame(columns=["region", "status", "jobs"])
    counts = frame.groupby(["region", "status"]).size().reset_index(name="jobs")
    return counts.sort_values(["region", "status"]).reset_index(drop=True)


def queue_frame(records: Iterable[Mapping[str, Any]]) -> Any:
    """One row per central-queue record, newest submission first."""
    rows = []
    for job in records:
        gate = ""
        if job.get("spot_max_price"):
            gate = f"{job.get('spot_instance_type')} <= ${job.get('spot_max_price')}/h"
        rows.append(
            {
                "job_id": str(job.get("job_id", "")),
                "name": str(job.get("job_name", "")),
                "region": str(job.get("target_region", "")),
                "namespace": str(job.get("namespace", "")),
                "status": str(job.get("status", "")),
                "priority": int(job.get("priority", 0) or 0),
                "submitted": _timestamp(job.get("submitted_at")),
                "updated": _timestamp(job.get("updated_at")),
                "claimed_by": str(job.get("claimed_by") or ""),
                "k8s_job": str(job.get("k8s_job_name") or ""),
                "spot_gate": gate,
                "error": str(job.get("error_message") or ""),
            }
        )
    columns = [
        "job_id",
        "name",
        "region",
        "namespace",
        "status",
        "priority",
        "submitted",
        "updated",
        "claimed_by",
        "k8s_job",
        "spot_gate",
        "error",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    for column in ("submitted", "updated"):
        frame[column] = _utc_series(list(frame[column]))
    return frame.sort_values("submitted", ascending=False, na_position="last").reset_index(
        drop=True
    )


def queue_stats_frame(stats: Mapping[str, Any]) -> Any:
    """``by_region`` of ``/api/v1/queue/stats`` as Region x status counts."""
    rows = [
        {"region": region, "status": status, "jobs": int(count)}
        for region, by_status in (stats.get("by_region") or {}).items()
        if isinstance(by_status, Mapping)
        for status, count in by_status.items()
    ]
    return pd.DataFrame(rows, columns=["region", "status", "jobs"])


def status_history_frame(job: Mapping[str, Any]) -> Any:
    """A queue record's status history, oldest first, with time spent in each state."""
    entries = [e for e in job.get("status_history") or [] if isinstance(e, Mapping)]
    rows = [
        {
            "status": str(e.get("status", "")),
            "timestamp": _timestamp(e.get("timestamp")),
            "message": str(e.get("message") or ""),
        }
        for e in entries
    ]
    frame = pd.DataFrame(rows, columns=["status", "timestamp", "message"])
    frame["timestamp"] = _utc_series(list(frame["timestamp"]))
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    frame["until"] = frame["timestamp"].shift(-1)
    # A terminal state is a moment, not a span: it ends where it starts.
    # A live last state keeps an open end, which the chart draws up to now.
    if not frame.empty and frame.iloc[-1]["status"] in TERMINAL_QUEUE_STATUSES:
        frame.loc[frame.index[-1], "until"] = frame.iloc[-1]["timestamp"]
    frame["seconds"] = (frame["until"] - frame["timestamp"]).dt.total_seconds()
    return frame


#: Central-queue statuses a record never leaves.
TERMINAL_QUEUE_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def claimed_by(job: Mapping[str, Any]) -> str | None:
    """Which Region's queue worker claimed a record, even after it released the claim.

    The API clears ``claimed_by`` when a record finishes, but its status
    history keeps the ``Claimed by <region>/<worker>/<lease>`` entry.
    """
    owner = job.get("claimed_by")
    if not owner:
        for entry in job.get("status_history") or []:
            message = str(entry.get("message") or "") if isinstance(entry, Mapping) else ""
            if (
                isinstance(entry, Mapping)
                and entry.get("status") == "claimed"
                and message.startswith("Claimed by ")
            ):
                owner = message.removeprefix("Claimed by ")
    return str(owner).split("/", 1)[0] if owner else None


def pods_frame(payload: Mapping[str, Any]) -> Any:
    """``GET .../pods``: one row per pod with its phase, node and restarts."""
    rows = []
    for pod in payload.get("pods") or []:
        if not isinstance(pod, Mapping):
            continue
        metadata = pod.get("metadata") or {}
        status = pod.get("status") or {}
        node = pod.get("node") or {}
        containers = [c for c in status.get("containerStatuses") or [] if isinstance(c, Mapping)]
        rows.append(
            {
                "pod": str(metadata.get("name", "")),
                "phase": str(status.get("phase", "")),
                "node": str(node.get("name") or (pod.get("spec") or {}).get("nodeName") or ""),
                "instance_type": str(node.get("instance_type") or ""),
                "capacity_type": str(node.get("capacity_type") or ""),
                "zone": str((node.get("labels") or {}).get("topology.kubernetes.io/zone", "")),
                "started": _timestamp(status.get("startTime")),
                "restarts": sum(int(c.get("restartCount", 0) or 0) for c in containers),
                "ready": all(bool(c.get("ready")) for c in containers) if containers else False,
                "containers": [str(c.get("name", "")) for c in containers],
            }
        )
    columns = [
        "pod",
        "phase",
        "node",
        "instance_type",
        "capacity_type",
        "zone",
        "started",
        "restarts",
        "ready",
        "containers",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    frame["started"] = _utc_series(list(frame["started"]))
    return frame


def events_frame(payload: Mapping[str, Any]) -> Any:
    """``GET .../events``: newest first, with the object each event is about."""
    rows = []
    for event in payload.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        involved = event.get("involvedObject") or {}
        source = event.get("source") or {}
        rows.append(
            {
                "time": _timestamp(event.get("lastTimestamp") or event.get("eventTime")),
                "type": str(event.get("type", "")),
                "reason": str(event.get("reason", "")),
                "object": f"{involved.get('kind', '')}/{involved.get('name', '')}",
                "message": str(event.get("message", "")),
                "count": int(event.get("count", 1) or 1),
                "source": str(source.get("component") or ""),
            }
        )
    frame = pd.DataFrame(
        rows, columns=["time", "type", "reason", "object", "message", "count", "source"]
    )
    frame["time"] = _utc_series(list(frame["time"]))
    return frame.sort_values("time", ascending=False, na_position="last").reset_index(drop=True)


def container_metrics_frame(payload: Mapping[str, Any]) -> Any:
    """``GET .../metrics``: CPU and memory per container (metrics-server)."""
    rows = [
        {
            "pod": str(pod.get("pod_name", "")),
            "container": str(container.get("name", "")),
            "cpu_millicores": float(container.get("cpu_millicores", 0) or 0),
            "memory_mib": float(container.get("memory_mib", 0) or 0),
        }
        for pod in payload.get("pods") or []
        if isinstance(pod, Mapping)
        for container in pod.get("containers") or []
        if isinstance(container, Mapping)
    ]
    return pd.DataFrame(rows, columns=["pod", "container", "cpu_millicores", "memory_mib"])


def _health_of(stack_health: str | None, metrics: RegionRead | None) -> str:
    """One Region's verdict from its stack and its health monitor.

    A stack ``gco status`` could not read has health ``unknown``: nothing is
    known about it, so it is no more degraded than one that is not deployed.
    """
    if stack_health == "unhealthy" or (
        metrics is not None and metrics.ok and metrics.data.get("status") == "unhealthy"
    ):
        return "unhealthy"
    if stack_health == "healthy" and metrics is not None and metrics.ok:
        return "healthy"
    if stack_health in (None, "not-deployed", "unknown"):
        return "unknown"
    return "degraded"


def _utilization(reading: RegionRead | None) -> dict[str, Any]:
    """A health monitor reading's ``resource_utilization``; empty when there is none."""
    return as_dict(reading.data.get("resource_utilization")) if reading and reading.ok else {}


def _gpu_counts(utilization: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """``(allocatable, requested)`` GPUs a health monitor reported, else ``(None, None)``.

    Its ``gpu_percent`` is 0 both without GPU nodes and with idle GPUs; the
    counts tell the two apart. A monitor deployed before they existed leaves
    them out, and one without a cluster reading sends nulls.
    """
    counts = (utilization.get("gpu_allocatable"), utilization.get("gpu_requested"))
    if all(isinstance(c, int) and not isinstance(c, bool) and c >= 0 for c in counts):
        return counts
    return None, None


def _stack_status(entry: Mapping[str, Any]) -> str:
    """A stack's CloudFormation status as the tables show it.

    The status document gives no status for a stack that is not deployed or
    for one it could not read (health ``unknown``); only the first is known
    to be absent.
    """
    if entry.get("status"):
        return str(entry["status"])
    return "unknown" if entry.get("health") == "unknown" else "not deployed"


def regions_frame(
    topology: Topology,
    fleet: Mapping[str, Any] | None,
    metrics: Sequence[RegionRead],
    *,
    jobs: Any = None,
    endpoints: Sequence[Mapping[str, Any]] = (),
) -> Any:
    """One row per workload Region: where it is, how it is, and what it runs.

    ``gpu_allocatable`` and ``gpu_requested`` are empty where the Region's
    health monitor sent no GPU counts, leaving only its ``gpu`` percentage.
    """
    sections = (fleet or {}).get("sections") or {}
    # Each Region's health is its regional stack's (``<project>-<region>``),
    # not the persistent or API-bridge stacks that share the Region.
    stacks = {
        entry.get("region"): entry
        for entry in (sections.get("stacks", {}).get("data", {}).get("expected") or [])
        if isinstance(entry, Mapping)
        and entry.get("name") == f"{topology.project}-{entry.get('region')}"
    }
    queue = sections.get("queue", {}).get("data", {}).get("by_region") or {}
    by_metrics = {read.region: read for read in metrics}
    rows = []
    for region in topology.workload_regions:
        info = region_info(region)
        stack = stacks.get(region) or {}
        reading = by_metrics.get(region)
        utilization = _utilization(reading)
        gpu_allocatable, gpu_requested = _gpu_counts(utilization)
        region_jobs = (
            jobs[jobs["region"] == region] if jobs is not None and not jobs.empty else None
        )
        serving = [
            e
            for e in endpoints
            if region in target_regions(e) and e.get("desired_state") != "deleted"
        ]
        rows.append(
            {
                "region": region,
                "city": info.city,
                "name": info.name,
                "latitude": info.latitude,
                "longitude": info.longitude,
                "health": _health_of(stack.get("health"), reading),
                "stack_status": _stack_status(stack),
                "cpu": utilization.get("cpu_percent"),
                "memory": utilization.get("memory_percent"),
                "gpu": utilization.get("gpu_percent"),
                "gpu_allocatable": gpu_allocatable,
                "gpu_requested": gpu_requested,
                "active_jobs": int(reading.data.get("active_jobs", 0) or 0)
                if reading and reading.ok
                else 0,
                "running": int((region_jobs["status"] == "running").sum())
                if region_jobs is not None
                else 0,
                "failed": int((region_jobs["status"] == "failed").sum())
                if region_jobs is not None
                else 0,
                "queued": int((queue.get(region) or {}).get("available", 0) or 0),
                "dlq": int((queue.get(region) or {}).get("dlq", 0) or 0),
                "endpoints": len(serving),
                "error": reading.error if reading and not reading.ok else "",
            }
        )
    columns = [
        "region",
        "city",
        "name",
        "latitude",
        "longitude",
        "health",
        "stack_status",
        "cpu",
        "memory",
        "gpu",
        "gpu_allocatable",
        "gpu_requested",
        "active_jobs",
        "running",
        "failed",
        "queued",
        "dlq",
        "endpoints",
        "error",
    ]
    return pd.DataFrame(rows, columns=columns)


def stacks_frame(fleet: Mapping[str, Any] | None) -> Any:
    """Every expected and optional CloudFormation stack the status document read."""
    data = ((fleet or {}).get("sections") or {}).get("stacks", {}).get("data", {})
    rows = []
    for role, entries in (("expected", data.get("expected")), ("optional", data.get("optional"))):
        for entry in entries or []:
            if isinstance(entry, Mapping):
                rows.append(
                    {
                        "stack": str(entry.get("name", "")),
                        "region": str(entry.get("region", "")),
                        "status": _stack_status(entry),
                        "health": str(entry.get("health", "")),
                        "updated": _timestamp(entry.get("updated_time")),
                        "role": role,
                    }
                )
    frame = pd.DataFrame(rows, columns=["stack", "region", "status", "health", "updated", "role"])
    frame["updated"] = _utc_series(list(frame["updated"]))
    return frame


#: The status document's add-on health as the verdicts the health columns colour:
#: an add-on teardown in progress is degraded, any status it does not know unknown.
_ADDON_VERDICTS = {"healthy": "healthy", "unhealthy": "unhealthy", "in-progress": "degraded"}
#: The order the add-on table lists verdicts in: failures first.
_VERDICT_ORDER = {"unhealthy": 0, "degraded": 1, "unknown": 2, "healthy": 3}


def addons_frame(fleet: Mapping[str, Any] | None) -> Any:
    """Each Region's add-on status records from the status document, failures first.

    One row per convergence phase or Helm chart a Region recorded. Rows with
    the same verdict keep the document's order: by Region, then by name.
    """
    data = ((fleet or {}).get("sections") or {}).get("addons", {}).get("data", {})
    rows = [
        {
            "region": str(region),
            "addon": str(record.get("name", "")),
            "status": str(record.get("status", "")),
            "health": _ADDON_VERDICTS.get(str(record.get("health")), "unknown"),
            "message": str(record.get("message") or ""),
            "updated": _timestamp(record.get("updated_at")),
        }
        for region, entry in (data.get("by_region") or {}).items()
        for record in as_dict(entry).get("addons") or []
        if isinstance(record, Mapping)
    ]
    frame = pd.DataFrame(
        rows, columns=["region", "addon", "status", "health", "message", "updated"]
    )
    frame["updated"] = _utc_series(list(frame["updated"]))
    return (
        frame.assign(_order=frame["health"].map(_VERDICT_ORDER))
        .sort_values("_order", kind="stable")
        .drop(columns="_order")
        .reset_index(drop=True)
    )


def findings_frame(fleet: Mapping[str, Any] | None) -> Any:
    """The status document's findings, errors first."""
    rows = [
        {
            "severity": str(f.get("severity", "")),
            "section": str(f.get("section", "")),
            "message": str(f.get("message", "")),
        }
        for f in (fleet or {}).get("findings") or []
        if isinstance(f, Mapping)
    ]
    frame = pd.DataFrame(rows, columns=["severity", "section", "message"])
    order = {"error": 0, "warn": 1}
    return (
        frame.assign(_order=frame["severity"].map(lambda s: order.get(s, 2)))
        .sort_values(["_order", "section"])
        .drop(columns="_order")
        .reset_index(drop=True)
    )


def utilization_frame(rows: Iterable[Mapping[str, Any]]) -> Any:
    """CloudWatch utilization samples as a long ``region/metric/timestamp/value`` frame."""
    frame = pd.DataFrame(list(rows), columns=["region", "metric", "timestamp", "value"])
    frame["timestamp"] = _utc_series([_timestamp(v) for v in frame["timestamp"]])
    return frame.sort_values(["metric", "region", "timestamp"]).reset_index(drop=True)


def spot_frame(rows: Iterable[Mapping[str, Any]]) -> Any:
    """Spot price changes, one row each, oldest first."""
    frame = pd.DataFrame(list(rows), columns=["region", "availability_zone", "timestamp", "price"])
    frame["timestamp"] = _utc_series([_timestamp(v) for v in frame["timestamp"]])
    return frame.sort_values(["availability_zone", "timestamp"]).reset_index(drop=True)


def instance_report_frame(reads: Sequence[RegionRead]) -> Any:
    """Per-Region offering, hardware and prices for one instance type."""
    rows = []
    for read in reads:
        data = read.data
        on_demand = data.get("on_demand_price")
        spot = data.get("spot_price")
        savings = (
            (1 - spot / on_demand) if isinstance(on_demand, float) and on_demand and spot else None
        )
        rows.append(
            {
                "region": read.region,
                "city": region_info(read.region).city,
                "offered": bool(data.get("offered")) if read.ok else None,
                "vcpus": data.get("vcpus"),
                "memory_gib": data.get("memory_gib"),
                "gpus": data.get("gpu_count"),
                "gpu_type": data.get("gpu_type") or "",
                "on_demand": on_demand,
                "spot": spot,
                "savings": savings,
                "stability": data.get("spot_stability"),
                "error": read.error or "",
            }
        )
    columns = [
        "region",
        "city",
        "offered",
        "vcpus",
        "memory_gib",
        "gpus",
        "gpu_type",
        "on_demand",
        "spot",
        "savings",
        "stability",
        "error",
    ]
    return pd.DataFrame(rows, columns=columns)


def endpoints_frame(endpoints: Iterable[Mapping[str, Any]]) -> Any:
    """One row per inference endpoint, with replicas ready across its Regions."""
    rows = []
    for endpoint in endpoints:
        spec = as_dict(endpoint.get("spec"))
        status = as_dict(endpoint.get("region_status"))
        ready = sum(int(as_dict(s).get("replicas_ready", 0) or 0) for s in status.values())
        desired = sum(int(as_dict(s).get("replicas_desired", 0) or 0) for s in status.values())
        env = as_dict(spec.get("env"))
        rows.append(
            {
                "endpoint": str(endpoint.get("endpoint_name", "")),
                "state": str(endpoint.get("desired_state", "")),
                "regions": target_regions(endpoint),
                "ready": ready,
                "desired": desired,
                "availability": (ready / desired) if desired else 0.0,
                "model": str(env.get("MODEL") or ""),
                "image": str(spec.get("image", "")),
                "gpus": int(spec.get("gpu_count", 0) or 0),
                "replicas": int(spec.get("replicas", 1) or 1),
                "created": _timestamp(endpoint.get("created_at")),
            }
        )
    columns = [
        "endpoint",
        "state",
        "regions",
        "ready",
        "desired",
        "availability",
        "model",
        "image",
        "gpus",
        "replicas",
        "created",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    frame["created"] = _utc_series(list(frame["created"]))
    return frame


def endpoint_regions_frame(endpoint: Mapping[str, Any]) -> Any:
    """An endpoint's per-Region state as the inference monitors last synced it."""
    status = as_dict(endpoint.get("region_status"))
    rows = []
    for region in target_regions(endpoint):
        entry = as_dict(status.get(region))
        rows.append(
            {
                "region": region,
                "state": str(entry.get("state") or "waiting"),
                "ready": int(entry.get("replicas_ready", 0) or 0),
                "desired": int(entry.get("replicas_desired", 0) or 0),
                "last_sync": _timestamp(entry.get("last_sync")),
                "error": str(entry.get("error") or ""),
            }
        )
    frame = pd.DataFrame(
        rows, columns=["region", "state", "ready", "desired", "last_sync", "error"]
    )
    frame["last_sync"] = _utc_series(list(frame["last_sync"]))
    return frame


def cost_frames(report: Mapping[str, Any]) -> dict[str, Any]:
    """Cost Explorer results as ``daily``, ``services`` and ``regions`` frames."""
    daily = pd.DataFrame(report.get("daily") or [], columns=["date", "amount"])
    daily["date"] = pd.to_datetime(daily["date"], utc=True)
    services = pd.DataFrame(report.get("by_service") or [], columns=["service", "amount"])
    regions = pd.DataFrame(
        [{"region": k, "amount": v} for k, v in (report.get("by_region") or {}).items()],
        columns=["region", "amount"],
    )
    return {"daily": daily, "services": services, "regions": regions}


def _number(value: object) -> float | None:
    """A float from a JSON number or numeric string (Athena returns strings)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def allocation_frames(report: Mapping[str, Any]) -> dict[str, Any]:
    """OpenCost allocation query rows as ``namespaces``, ``regions`` and ``trend`` frames.

    ``namespaces`` and ``regions`` are long frames (one row per cost component)
    so a stacked bar can show where the money goes; ``trend`` has one row per
    period.
    """
    components = ("cpu_cost", "ram_cost", "gpu_cost", "pv_cost")
    labels = {"cpu_cost": "CPU", "ram_cost": "Memory", "gpu_cost": "GPU", "pv_cost": "Storage"}

    def long(rows: Any, key: str) -> Any:
        records = [
            {
                key: str(row.get(key, "")),
                "component": labels[c],
                "amount": _number(row.get(c)) or 0.0,
            }
            for row in rows or []
            if isinstance(row, Mapping)
            for c in components
            if c in row
        ]
        return pd.DataFrame(records, columns=[key, "component", "amount"])

    totals = {
        name: pd.DataFrame(
            [
                {key: str(row.get(key, "")), "amount": _number(row.get("total_cost")) or 0.0}
                for row in report.get(name) or []
                if isinstance(row, Mapping)
            ],
            columns=[key, "amount"],
        )
        for name, key in (("namespaces", "namespace"), ("regions", "region"))
    }
    trend = pd.DataFrame(
        [
            # Daily periods are dates, hourly ones "2026-11-25T16:00Z".
            {
                "period": _timestamp(row.get("period")),
                "amount": _number(row.get("total_cost")) or 0.0,
            }
            for row in report.get("trend") or []
            if isinstance(row, Mapping)
        ],
        columns=["period", "amount"],
    )
    trend["period"] = _utc_series(list(trend["period"]))
    return {
        "namespaces": long(report.get("namespaces"), "namespace"),
        "regions": long(report.get("regions"), "region"),
        "namespace_totals": totals["namespaces"],
        "region_totals": totals["regions"],
        "trend": trend.dropna(subset=["period"]).reset_index(drop=True),
    }


def recommendation_frame(result: Mapping[str, Any]) -> Any:
    """A placement recommendation's ranking of every Region, best first.

    Lower scores rank higher. Weighted recommendations (an instance type was
    given) also carry the Spot signals they were scored on.
    """
    rows = []
    for rank, entry in enumerate(result.get("all_regions") or [], start=1):
        if not isinstance(entry, Mapping):
            continue
        rows.append(
            {
                "rank": rank,
                "region": str(entry.get("region", "")),
                "score": _number(entry.get("score")),
                "queue_depth": _number(entry.get("queue_depth")),
                "gpu_utilization": _number(entry.get("gpu_utilization")),
                "running_jobs": _number(entry.get("running_jobs")),
                "spot_placement": _number(entry.get("spot_placement_score")),
                "spot_price_ratio": _number(entry.get("spot_price_ratio")),
                "telemetry": str(entry.get("telemetry_status") or ""),
                "recommended": str(entry.get("region", "")) == result.get("region"),
            }
        )
    columns = [
        "rank",
        "region",
        "score",
        "queue_depth",
        "gpu_utilization",
        "running_jobs",
        "spot_placement",
        "spot_price_ratio",
        "telemetry",
        "recommended",
    ]
    return pd.DataFrame(rows, columns=columns)


def sqs_frame(fleet: Mapping[str, Any] | None) -> Any:
    """Each Region's SQS job queue as ``gco status`` read it (waiting, in flight, DLQ)."""
    by_region = ((fleet or {}).get("sections") or {}).get("queue", {}).get("data", {}).get(
        "by_region"
    ) or {}
    rows = [
        {
            "region": str(region),
            "available": int(counts.get("available", 0) or 0),
            "in_flight": int(counts.get("in_flight", 0) or 0),
            "delayed": int(counts.get("delayed", 0) or 0),
            "dlq": int(counts.get("dlq", 0) or 0),
        }
        for region, counts in by_region.items()
        if isinstance(counts, Mapping)
    ]
    return pd.DataFrame(rows, columns=["region", "available", "in_flight", "delayed", "dlq"])


def _container_gpu_cap(enforcement: object) -> str:
    """The per-container GPU cap a Region's admission policies report, or "".

    ``cluster_enforcement`` of ``GET /api/v1/policy``: the GPU cap policy on
    gco-jobs reports ``container_max``. More than one value, from more than
    one namespace, is listed in order.
    """
    caps: set[str] = set()
    for layer in as_dict(enforcement).values():
        for policy in as_dict(as_dict(layer).get("admission_policies")).values():
            cap = as_dict(as_dict(policy).get("container_max")).get("nvidia.com/gpu")
            if cap is not None:
                caps.add(str(cap))
    if not caps:
        # A Region deployed before the cap moved to an admission policy still
        # caps GPUs with its LimitRange's Container max.
        for layer in as_dict(enforcement).values():
            for limits in as_dict(as_dict(layer).get("limit_ranges")).values():
                for limit in limits if isinstance(limits, list) else []:
                    cap = as_dict(as_dict(limit).get("max")).get("nvidia.com/gpu")
                    if cap is not None and as_dict(limit).get("type") == "Container":
                        caps.add(str(cap))
    return ", ".join(sorted(caps))


def policy_frame(reads: Sequence[RegionRead]) -> Any:
    """What each Region's manifest processor admits, side by side."""
    rows = []
    for read in reads:
        policy = as_dict(read.data.get("policy")) if read.ok else {}
        caps = as_dict(policy.get("manifest_caps"))
        configured = as_dict(caps.get("configured"))
        security = as_dict(policy.get("manifest_security_policy"))
        rows.append(
            {
                "region": read.region,
                "validation": policy.get("validation_enabled"),
                "namespaces": [str(n) for n in policy.get("allowed_namespaces") or []],
                "kinds": [str(k) for k in policy.get("allowed_kinds") or []],
                "max_cpu": str(configured.get("max_cpu_per_manifest") or ""),
                "max_memory": str(configured.get("max_memory_per_manifest") or ""),
                "max_gpus": caps.get("max_gpu_count"),
                "container_gpus": _container_gpu_cap(
                    read.data.get("cluster_enforcement") if read.ok else None
                ),
                "registries": len(policy.get("trusted_registries") or []),
                "dockerhub_orgs": [str(o) for o in policy.get("trusted_dockerhub_orgs") or []],
                "blocked": [
                    key.removeprefix("block_").replace("_", " ")
                    for key, value in sorted(security.items())
                    if key.startswith("block_") and value is True
                ],
                "error": read.error or "",
            }
        )
    columns = [
        "region",
        "validation",
        "namespaces",
        "kinds",
        "max_cpu",
        "max_memory",
        "max_gpus",
        "container_gpus",
        "registries",
        "dockerhub_orgs",
        "blocked",
        "error",
    ]
    return pd.DataFrame(rows, columns=columns)


def services_frame(health: Mapping[str, Any] | None, statuses: Sequence[RegionRead]) -> Any:
    """Each Region's API health and manifest processor, including its queue worker."""
    verdicts = {
        str(entry.get("region")): entry
        for entry in (health or {}).get("regions") or []
        if isinstance(entry, Mapping)
    }
    by_region = {read.region: read for read in statuses}
    rows = []
    for region in sorted(set(verdicts) | set(by_region)):
        verdict = verdicts.get(region) or {}
        read = by_region.get(region)
        status = read.data if read is not None and read.ok else {}
        worker = status.get("central_queue_worker")
        worker = worker if isinstance(worker, Mapping) else {}
        rows.append(
            {
                "region": region,
                "health": str(verdict.get("status") or "unknown"),
                "cluster": str(verdict.get("cluster_id") or status.get("cluster_id") or ""),
                "processor": bool(status.get("manifest_processor_initialized")),
                "version": str(status.get("version") or ""),
                "queue_worker": (
                    "running"
                    if worker.get("running")
                    else ("stopped" if worker.get("enabled") else "disabled")
                )
                if worker
                else "",
                "last_pass": _timestamp(worker.get("last_successful_pass_at")),
                "worker_error": str(worker.get("last_error") or ""),
                "templates": int(status.get("templates_count", 0) or 0),
                "webhooks": int(status.get("webhooks_count", 0) or 0),
                "error": (read.error if read is not None and not read.ok else "") or "",
            }
        )
    columns = [
        "region",
        "health",
        "cluster",
        "processor",
        "version",
        "queue_worker",
        "last_pass",
        "worker_error",
        "templates",
        "webhooks",
        "error",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    frame["last_pass"] = _utc_series(list(frame["last_pass"]))
    return frame


def cost_monitor_frame(reads: Sequence[RegionRead]) -> Any:
    """Each Region's cost monitor: OpenCost health and its latest scheduled report."""
    rows = []
    for read in reads:
        data = read.data if read.ok else {}
        last = data.get("last_scheduled_report")
        last = last if isinstance(last, Mapping) else {}
        rows.append(
            {
                "region": read.region,
                "opencost": bool(data.get("opencost_healthy")) if read.ok else None,
                "returning_data": bool(data.get("opencost_returning_data")) if read.ok else None,
                "interval_min": data.get("report_interval_minutes"),
                "last_window_end": _timestamp(last.get("window_end")),
                "last_cost": _number(last.get("total_cost")),
                "namespaces": len(data.get("allocation_names") or []),
                "error": read.error or str(data.get("last_error") or ""),
            }
        )
    columns = [
        "region",
        "opencost",
        "returning_data",
        "interval_min",
        "last_window_end",
        "last_cost",
        "namespaces",
        "error",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    frame["last_window_end"] = _utc_series(list(frame["last_window_end"]))
    return frame


def health_checks_frame(checks: Iterable[Mapping[str, Any]]) -> Any:
    """Endpoint health probes, one row per Region probed."""
    frame = pd.DataFrame(
        [
            {
                "region": str(c.get("region", "")),
                "healthy": bool(c.get("healthy")),
                "http_status": c.get("http_status"),
                "latency_ms": _number(c.get("latency_ms")),
                "error": str(c.get("error") or ""),
            }
            for c in checks
        ],
        columns=["region", "healthy", "http_status", "latency_ms", "error"],
    )
    return frame


def records_table(payload: object) -> Any:
    """The largest list of objects in an API answer, flattened into a table.

    The console shows every answer as JSON; this is its table view. Nested
    objects become dotted columns; an answer with no list of objects yields
    a one-row table of its scalar fields.
    """
    best: list[Mapping[str, Any]] = []

    def visit(value: object) -> None:
        nonlocal best
        if isinstance(value, list):
            objects = [item for item in value if isinstance(item, Mapping)]
            if len(objects) > len(best):
                best = objects
            for item in value:
                visit(item)
        elif isinstance(value, Mapping):
            for item in value.values():
                visit(item)

    visit(payload)
    if best:
        return pd.json_normalize([dict(item) for item in best], sep=".")
    if isinstance(payload, Mapping):
        return pd.json_normalize([dict(payload)], sep=".")
    return pd.DataFrame()


def capacity_frame(fleet: Mapping[str, Any] | None, metrics: Sequence[RegionRead] = ()) -> Any:
    """The status document's per-Region capacity signals (what placement scores on).

    ``metrics``, the health monitors' readings, add each Region's GPU counts.
    A Region they show has no GPUs has no GPU use to report, so its ``gpu``
    is left empty instead of reading 0%.
    """
    by_region = ((fleet or {}).get("sections") or {}).get("capacity", {}).get("data", {}).get(
        "by_region"
    ) or {}
    counts = {read.region: _gpu_counts(_utilization(read)) for read in metrics}
    rows = []
    for region, entry in by_region.items():
        if not isinstance(entry, Mapping):
            continue
        allocatable, requested = counts.get(str(region), (None, None))
        rows.append(
            {
                "region": str(region),
                "gpu": None
                if allocatable == requested == 0
                else _number(entry.get("gpu_utilization")),
                "gpu_allocatable": allocatable,
                "gpu_requested": requested,
                "cpu": _number(entry.get("cpu_utilization")),
                "queue_depth": int(entry.get("queue_depth", 0) or 0),
                "running_jobs": int(entry.get("running_jobs", 0) or 0),
                "telemetry": str(entry.get("telemetry_status") or ""),
                "missing": [str(s) for s in entry.get("unavailable_signals") or []],
            }
        )
    columns = [
        "region",
        "gpu",
        "gpu_allocatable",
        "gpu_requested",
        "cpu",
        "queue_depth",
        "running_jobs",
        "telemetry",
        "missing",
    ]
    return pd.DataFrame(rows, columns=columns)
