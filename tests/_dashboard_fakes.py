"""An in-memory stand-in for ``cli.dashboard.data.FleetData`` and the AppTest harness.

The dashboard's pages reach AWS only through ``ui.fleet()``, so the view tests
swap that for :class:`FakeFleet`: every read answers from the canned fleet
below (three workload Regions, a dozen Jobs, central-queue records, one
inference endpoint, costs), every write is recorded in ``calls`` and answers
like the API does, and any method can be made to fail through ``fail``.

Nothing here holds a real account, ARN or host name.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cli.config import GCOConfig
from cli.dashboard.data import (
    DashboardError,
    ExampleManifest,
    JobsSnapshot,
    RegionRead,
    Topology,
    load_manifest_text,
)
from cli.jobs import JobInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
APP = REPO_ROOT / "cli" / "dashboard" / "app.py"
REGIONS = ("us-east-1", "us-west-2", "eu-west-1")
NOW = datetime(2026, 11, 25, 18, 0, tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def topology(**overrides: Any) -> Topology:
    values: dict[str, Any] = {
        "project": "gco",
        "global_region": "us-east-2",
        "api_gateway_region": "us-east-2",
        "monitoring_region": "us-east-2",
        "workload_regions": REGIONS,
        "configured": True,
        "regional_api": False,
    }
    values.update(overrides)
    return Topology(**values)


def job_info(
    name: str,
    region: str,
    status: str,
    *,
    minutes_ago: int = 90,
    runtime_minutes: int | None = 10,
    labels: Mapping[str, str] | None = None,
    uid: str | None = None,
) -> JobInfo:
    created = NOW - timedelta(minutes=minutes_ago)
    started = created + timedelta(seconds=5)
    finished = (
        started + timedelta(minutes=runtime_minutes)
        if status in ("succeeded", "failed") and runtime_minutes is not None
        else None
    )
    return JobInfo(
        name=name,
        namespace="gco-jobs",
        region=region,
        status=status,
        created_time=created,
        start_time=started if status != "pending" else None,
        completion_time=finished,
        active_pods=1 if status == "running" else 0,
        succeeded_pods=1 if status == "succeeded" else 0,
        failed_pods=1 if status == "failed" else 0,
        labels=dict(labels or {"app": name.split("-")[0], "team": "ml"}),
        image_refs=["busybox:1.38.0"],
        uid=uid or f"uid-{name}",
    )


def sample_jobs() -> list[JobInfo]:
    return [
        job_info("llm-finetune-a", "us-east-1", "running", minutes_ago=30),
        job_info("eval-suite-1", "us-east-1", "succeeded"),
        job_info("data-validate", "us-east-1", "failed"),
        job_info("vision-pretrain", "us-west-2", "running", minutes_ago=50),
        job_info("feature-store-sync", "us-west-2", "succeeded"),
        job_info("speech-asr-train", "eu-west-1", "pending", minutes_ago=2),
        job_info(
            "queued-sweep-lr-1e4-abc",
            "us-east-1",
            "succeeded",
            labels={"gco.io/managed-by": "central-queue", "team": "ml"},
        ),
    ]


def fleet_status_document(*, findings: Sequence[Mapping[str, str]] = ()) -> dict[str, Any]:
    expected = [
        {
            "name": "gco-global",
            "region": "us-east-2",
            "status": "CREATE_COMPLETE",
            "health": "healthy",
            "updated_time": _iso(NOW - timedelta(hours=4)),
        },
        *[
            {
                "name": f"gco-{region}",
                "region": region,
                "status": "UPDATE_COMPLETE",
                "health": "healthy",
                "updated_time": _iso(NOW - timedelta(hours=3)),
            }
            for region in REGIONS
        ],
        {
            "name": "gco-us-east-1-persistent",
            "region": "us-east-1",
            "status": "CREATE_COMPLETE",
            "health": "healthy",
            "updated_time": _iso(NOW - timedelta(hours=4)),
        },
    ]
    optional = [
        {
            "name": f"gco-regional-api-{region}",
            "region": region,
            "status": "CREATE_COMPLETE",
            "health": "healthy",
            "updated_time": _iso(NOW - timedelta(hours=3)),
        }
        for region in REGIONS
    ] + [{"name": "gco-analytics", "region": "us-east-2", "status": None, "health": "not-deployed"}]
    queue_by_region = {
        "us-east-1": {"available": 2, "in_flight": 1, "delayed": 0, "dlq": 0},
        "us-west-2": {"available": 0, "in_flight": 0, "delayed": 0, "dlq": 1},
        "eu-west-1": {"available": 0, "in_flight": 0, "delayed": 1, "dlq": 0},
    }
    # Every Region's add-ons converged after the last deploy.
    addons_by_region = {
        region: {
            "readable": True,
            "addons": [
                {
                    "name": "base-manifests",
                    "status": "applied",
                    "health": "healthy",
                    "message": "applied=41 expected=41 skipped=0",
                    "updated_at": _iso(NOW - timedelta(hours=3)),
                },
                {
                    "name": "keda",
                    "status": "installed",
                    "health": "healthy",
                    "message": "keda installed",
                    "updated_at": _iso(NOW - timedelta(hours=3)),
                },
            ],
        }
        for region in REGIONS
    }
    return {
        "generated_at": _iso(NOW),
        "project_name": "gco",
        "overall": "ok" if not findings else "degraded",
        "degraded": bool(findings),
        "findings": [dict(f) for f in findings],
        "sections": {
            "stacks": {"status": "ok", "data": {"expected": expected, "optional": optional}},
            "addons": {"status": "ok", "data": {"by_region": addons_by_region}},
            "queue": {
                "status": "ok",
                "data": {"by_region": queue_by_region, "totals": {"available": 2, "dlq": 1}},
            },
            "capacity": {
                "status": "ok",
                "data": {
                    "by_region": {
                        "us-east-1": {
                            "queue_depth": 2,
                            "running_jobs": 2,
                            "gpu_utilization": 40.0,
                            "cpu_utilization": 6.5,
                            "telemetry_status": "complete",
                            "unavailable_signals": [],
                        },
                        "us-west-2": {
                            "queue_depth": 0,
                            "running_jobs": 1,
                            "gpu_utilization": 0.0,
                            "cpu_utilization": 7.0,
                            "telemetry_status": "partial",
                            "unavailable_signals": ["gpu"],
                        },
                    }
                },
            },
            "policy": {
                "status": "ok",
                "data": {
                    "compared": list(REGIONS),
                    "unreadable": {},
                    "agree": True,
                    "drift": [],
                    "enforcement_gaps": {},
                },
            },
        },
    }


def metrics_read(
    region: str,
    *,
    gpu: float = 50.0,
    gpu_counts: tuple[int, int] | None = (2, 1),
    violations: Sequence[str] = (),
) -> RegionRead:
    """A health monitor's ``GET /api/v1/metrics`` answer.

    ``gpu_counts`` is ``(allocatable, requested)`` GPUs, sent beside the
    ``gpu`` percentage; None leaves them out, as a monitor deployed before
    they existed does.
    """
    utilization: dict[str, Any] = {"cpu_percent": 6.0, "memory_percent": 40.0, "gpu_percent": gpu}
    if gpu_counts is not None:
        utilization["gpu_allocatable"], utilization["gpu_requested"] = gpu_counts
    return RegionRead(
        region=region,
        data={
            "status": "healthy",
            "resource_utilization": utilization,
            "thresholds": {"cpu_threshold": 80, "memory_threshold": 80, "gpu_threshold": -1},
            "active_jobs": 2,
            "threshold_violations": list(violations),
        },
    )


def queue_record(job_id: str, name: str, region: str, status: str, **extra: Any) -> dict[str, Any]:
    submitted = NOW - timedelta(minutes=30)
    history = [
        {"status": "queued", "timestamp": _iso(submitted), "message": "Job submitted"},
    ]
    if status != "queued":
        history += [
            {
                "status": "claimed",
                "timestamp": _iso(submitted + timedelta(seconds=5)),
                "message": f"Claimed by {region}/worker-1/lease-1",
            },
            {
                "status": status,
                "timestamp": _iso(submitted + timedelta(minutes=2)),
                "message": f"Kubernetes Job is {status}",
            },
        ]
    record = {
        "job_id": job_id,
        "job_name": name,
        "target_region": region,
        "namespace": "gco-jobs",
        "status": status,
        "priority": 50,
        "submitted_at": _iso(submitted),
        "updated_at": _iso(submitted + timedelta(minutes=2)),
        "claimed_by": None,
        "k8s_job_name": f"{name}-0123" if status != "queued" else None,
        "error_message": None,
        "status_history": history,
        "manifest": {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name}},
    }
    record.update(extra)
    return record


def sample_endpoint(**overrides: Any) -> dict[str, Any]:
    endpoint: dict[str, Any] = {
        "endpoint_name": "qwen-chat",
        "desired_state": "running",
        "target_regions": ["us-east-1", "us-west-2"],
        "ingress_path": "/inference/qwen-chat",
        "created_at": _iso(NOW - timedelta(hours=3)),
        "region_status": {
            "us-east-1": {
                "state": "running",
                "replicas_ready": 1,
                "replicas_desired": 1,
                "last_sync": _iso(NOW),
            },
            "us-west-2": {
                "state": "running",
                "replicas_ready": 1,
                "replicas_desired": 1,
                "last_sync": _iso(NOW),
            },
        },
        "spec": {
            "image": "vllm/vllm-openai:v0.32.0",
            "port": 8000,
            "gpu_count": 1,
            "replicas": 1,
            "health_check_path": "/health",
            "env": {"MODEL": "Qwen/Qwen2.5-1.5B-Instruct"},
            "framework": "vllm",
        },
    }
    endpoint.update(overrides)
    return endpoint


class FakeFleet:
    """Answers every ``FleetData`` call from canned data and records the writes.

    ``fail[name] = exc`` makes the method ``name`` raise ``exc``; ``overrides``
    replaces a read's answer; ``calls`` lists ``(name, args, kwargs)`` for
    every method called, reads and writes alike.
    """

    def __init__(self) -> None:
        self.config = GCOConfig()
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.fail: dict[str, BaseException] = {}
        self.overrides: dict[str, Any] = {}
        self.chat_chunks: list[str] = ["Several ", "Regions ", "keep it up."]

    # -- plumbing --------------------------------------------------------
    def _call(self, name: str, default: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args, kwargs))
        if name in self.fail:
            raise self.fail[name]
        value = self.overrides.get(name, default)
        return copy.deepcopy(value) if not callable(value) else value(*args, **kwargs)

    def called(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(args, kwargs) for called, args, kwargs in self.calls if called == name]

    # -- topology and status ----------------------------------------------
    def topology(self) -> Topology:
        return self._call("topology", topology())

    def identity(self) -> dict[str, str]:
        return self._call(
            "identity", {"arn": "user/tester", "account": "test-account", "principal": "tester"}
        )

    def fleet_status(
        self, *, with_costs: bool = False, with_policy: bool = False
    ) -> dict[str, Any]:
        return self._call(
            "fleet_status", fleet_status_document(), with_costs=with_costs, with_policy=with_policy
        )

    def cluster_metrics(self, regions: Sequence[str]) -> list[RegionRead]:
        return self._call("cluster_metrics", [metrics_read(r) for r in regions], tuple(regions))

    def utilization_history(
        self, regions: Sequence[str], *, hours: int = 3, period: int = 60
    ) -> list[dict[str, Any]]:
        rows = [
            {
                "region": region,
                "metric": metric,
                "timestamp": NOW - timedelta(minutes=minute),
                "value": float(10 * index + minute % 7),
            }
            for index, region in enumerate(regions)
            for metric in ("cpu", "memory", "gpu", "active_jobs")
            for minute in range(0, 30, 5)
        ]
        return self._call("utilization_history", rows, tuple(regions), hours=hours, period=period)

    # -- jobs ---------------------------------------------------------------
    def jobs(self, regions: Sequence[str], *, limit: int = 200) -> JobsSnapshot:
        jobs = [j for j in sample_jobs() if j.region in regions]
        return self._call("jobs", JobsSnapshot(jobs, {}, len(regions), len(jobs), "global"))

    def job_detail(self, name: str, namespace: str, region: str) -> dict[str, Any]:
        detail = {
            "metadata": {"name": name, "namespace": namespace, "labels": {"team": "ml"}},
            "spec": {"template": {"spec": {"containers": [{"name": "main", "image": "busybox"}]}}},
            "status": {
                "active": 0,
                "succeeded": 0,
                "failed": 1,
                "startTime": _iso(NOW - timedelta(minutes=20)),
                "conditions": [
                    {
                        "type": "Failed",
                        "status": "True",
                        "reason": "BackoffLimitExceeded",
                        "message": "Job has reached the specified backoff limit",
                        "lastTransitionTime": _iso(NOW - timedelta(minutes=10)),
                    },
                    {"type": "FailureTarget", "status": "True", "reason": "BackoffLimitExceeded"},
                ],
            },
            "scheduling": {
                "node_instance_type": "m5a.large",
                "node_capacity_type": "on-demand",
                "node_labels": {
                    "topology.kubernetes.io/zone": "us-east-1c",
                    "karpenter.sh/nodepool": "general-purpose",
                },
                "unscheduled": [],
                "node_lookup_error": None,
            },
        }
        return self._call("job_detail", detail, name, namespace, region)

    def job_pods(self, name: str, namespace: str, region: str) -> dict[str, Any]:
        pods = {
            "pods": [
                {
                    "metadata": {"name": f"{name}-x1"},
                    "status": {
                        "phase": "Failed",
                        "startTime": _iso(NOW - timedelta(minutes=20)),
                        "containerStatuses": [
                            {"name": "main", "ready": False, "restartCount": 0},
                            {"name": "sidecar", "ready": False, "restartCount": 1},
                        ],
                    },
                    "node": {"name": "node-1", "instance_type": "m5a.large", "labels": {}},
                }
            ]
        }
        return self._call("job_pods", pods, name, namespace, region)

    def job_events(self, name: str, namespace: str, region: str) -> dict[str, Any]:
        events = {
            "events": [
                {
                    "type": "Warning",
                    "reason": "BackoffLimitExceeded",
                    "message": "Job has reached the specified backoff limit",
                    "lastTimestamp": _iso(NOW - timedelta(minutes=10)),
                    "count": 1,
                    "involvedObject": {"kind": "Job", "name": name},
                    "source": {"component": "job-controller"},
                },
                {
                    "type": "Normal",
                    "reason": "Scheduled",
                    "message": "Successfully assigned",
                    "lastTimestamp": _iso(NOW - timedelta(minutes=20)),
                    "count": 1,
                    "involvedObject": {"kind": "Pod", "name": f"{name}-x1"},
                    "source": {"component": "default-scheduler"},
                },
            ]
        }
        return self._call("job_events", events, name, namespace, region)

    def job_metrics(self, name: str, namespace: str, region: str) -> dict[str, Any]:
        metrics = {
            "summary": {"total_cpu_millicores": 5, "total_memory_mib": 12.5, "pod_count": 1},
            "pods": [
                {
                    "pod_name": f"{name}-x1",
                    "containers": [{"name": "main", "cpu_millicores": 5, "memory_mib": 12.5}],
                }
            ],
        }
        return self._call("job_metrics", metrics, name, namespace, region)

    def job_logs(
        self,
        name: str,
        namespace: str,
        region: str,
        *,
        tail: int = 200,
        container: str | None = None,
    ) -> str:
        return self._call(
            "job_logs", "step 1\nstep 2\n", name, namespace, region, tail=tail, container=container
        )

    def submit_job(self, manifests: Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
        return self._call(
            "submit_job", {"success": True, "resources": [], "errors": None}, manifests, **kwargs
        )

    def submit_job_sqs(
        self, manifests: Sequence[Mapping[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        return self._call("submit_job_sqs", {"message_id": "m-1"}, manifests, **kwargs)

    def recommend_region(
        self, manifests: Sequence[Mapping[str, Any]] = (), **kwargs: Any
    ) -> dict[str, Any]:
        answer = {
            "region": "us-west-2",
            "reason": "empty queue, 100% GPU available",
            "score": 0.12,
            "all_regions": [
                {"region": "us-west-2", "score": 0.12, "queue_depth": 0, "gpu_utilization": 0.0},
                {"region": "us-east-1", "score": 0.4, "queue_depth": 2, "gpu_utilization": 40.0},
            ],
        }
        return self._call("recommend_region", answer, manifests, **kwargs)

    def delete_job(
        self, name: str, namespace: str, region: str, *, uid: str | None = None
    ) -> dict[str, Any]:
        return self._call("delete_job", {"deleted": True}, name, namespace, region, uid=uid)

    def retry_job(self, name: str, namespace: str, region: str) -> dict[str, Any]:
        return self._call("retry_job", {"new_job": f"{name}-retry-1"}, name, namespace, region)

    def cleanup_jobs(self, **kwargs: Any) -> dict[str, Any]:
        return self._call(
            "cleanup_jobs",
            {
                "dry_run": kwargs.get("dry_run"),
                "total_matched": 2,
                "total_deleted": 0,
                "errors": None,
            },
            **kwargs,
        )

    # -- central queue ------------------------------------------------------
    def queue_jobs(self, **kwargs: Any) -> dict[str, Any]:
        records = [
            queue_record("q-1", "queued-sweep", "us-east-1", "queued"),
            queue_record("q-2", "queued-dedup", "eu-west-1", "succeeded"),
            queue_record(
                "q-3",
                "gated-train",
                "us-west-2",
                "failed",
                error_message="image pull failed",
                spot_max_price=0.5,
                spot_instance_type="g5.xlarge",
            ),
        ]
        return self._call("queue_jobs", {"jobs": records, "next_cursor": None}, **kwargs)

    def queue_job(self, job_id: str) -> dict[str, Any]:
        records = {r["job_id"]: r for r in self.queue_jobs()["jobs"]}
        return self._call("queue_job", records.get(job_id, {}), job_id)

    def queue_stats(self) -> dict[str, Any]:
        stats = {
            "summary": {"total_jobs": 3, "total_queued": 1},
            "by_region": {
                "us-east-1": {"queued": 1},
                "eu-west-1": {"succeeded": 1},
                "us-west-2": {"failed": 1},
            },
        }
        return self._call("queue_stats", stats)

    def queue_submit(self, manifest: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self._call("queue_submit", {"job_id": "q-9"}, manifest, **kwargs)

    def queue_cancel(self, job_id: str, *, reason: str | None = None) -> dict[str, Any]:
        return self._call("queue_cancel", {"message": "cancelled"}, job_id, reason=reason)

    # -- capacity -------------------------------------------------------------
    def spot_price_history(
        self, instance_type: str, region: str, *, days: int = 7
    ) -> list[dict[str, Any]]:
        rows = [
            {
                "region": region,
                "availability_zone": f"{region}{zone}",
                "timestamp": NOW - timedelta(hours=hour),
                "price": 0.5 + 0.01 * hour,
            }
            for zone in ("a", "b")
            for hour in (1, 12, 48)
        ]
        return self._call("spot_price_history", rows, instance_type, region, days=days)

    def instance_report(self, instance_type: str, regions: Sequence[str]) -> list[RegionRead]:
        reads = [
            RegionRead(
                region=region,
                data={
                    "offered": region != "eu-west-1",
                    "vcpus": 8,
                    "memory_gib": 32.0,
                    "gpu_count": 1,
                    "gpu_type": "L4",
                    "on_demand_price": 2.5,
                    "spot_price": 0.5 if region == "us-east-1" else 0.75,
                    "spot_stability": 0.9,
                },
            )
            for region in regions
        ]
        return self._call("instance_report", reads, instance_type, tuple(regions))

    def placement_score(self, instance_type: str, region: str, *, capacity: int = 1) -> int | None:
        return self._call("placement_score", 7, instance_type, region, capacity=capacity)

    # -- inference ------------------------------------------------------------
    def endpoints(self) -> list[dict[str, Any]]:
        return self._call("endpoints", [sample_endpoint()])

    def endpoint(self, name: str) -> dict[str, Any] | None:
        return self._call("endpoint", sample_endpoint(endpoint_name=name), name)

    def served_model(self, name: str, region: str | None = None) -> str:
        return self._call("served_model", "Qwen/Qwen2.5-1.5B-Instruct", name, region)

    def chat(
        self, name: str, messages: Sequence[Mapping[str, str]], **kwargs: Any
    ) -> Iterator[str]:
        self.calls.append(("chat", (name, messages), kwargs))
        if "chat" in self.fail:
            raise self.fail["chat"]
        yield from self.chat_chunks

    def endpoint_health_all(self, name: str, regions: Sequence[str]) -> list[dict[str, Any]]:
        checks = [{"region": "global", "healthy": True, "http_status": 200, "latency_ms": 250}] + [
            {"region": region, "healthy": True, "http_status": 200, "latency_ms": 900}
            for region in regions
        ]
        return self._call("endpoint_health_all", checks, name, tuple(regions))

    def deploy_endpoint(self, **kwargs: Any) -> dict[str, Any]:
        return self._call(
            "deploy_endpoint", {"target_regions": kwargs.get("target_regions")}, **kwargs
        )

    def _endpoint_write(self, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call(name, {"endpoint_name": args[0] if args else None}, *args, **kwargs)

    def scale_endpoint(self, name: str, replicas: int) -> dict[str, Any]:
        return self._endpoint_write("scale_endpoint", name, replicas)

    def stop_endpoint(self, name: str) -> dict[str, Any]:
        return self._endpoint_write("stop_endpoint", name)

    def start_endpoint(self, name: str) -> dict[str, Any]:
        return self._endpoint_write("start_endpoint", name)

    def delete_endpoint(self, name: str) -> dict[str, Any]:
        return self._endpoint_write("delete_endpoint", name)

    def update_endpoint_image(self, name: str, image: str) -> dict[str, Any]:
        return self._endpoint_write("update_endpoint_image", name, image)

    def add_endpoint_region(self, name: str, region: str) -> dict[str, Any]:
        return self._endpoint_write("add_endpoint_region", name, region)

    def remove_endpoint_region(self, name: str, region: str) -> dict[str, Any]:
        return self._endpoint_write("remove_endpoint_region", name, region)

    def start_canary(
        self, name: str, image: str, *, weight: int, replicas: int = 1
    ) -> dict[str, Any]:
        return self._endpoint_write("start_canary", name, image, weight=weight, replicas=replicas)

    def promote_canary(self, name: str) -> dict[str, Any]:
        return self._endpoint_write("promote_canary", name)

    def rollback_canary(self, name: str) -> dict[str, Any]:
        return self._endpoint_write("rollback_canary", name)

    # -- costs ------------------------------------------------------------------
    def costs(self, *, days: int = 30) -> dict[str, Any]:
        report = {
            "total": 42.5,
            "currency": "USD",
            "by_service": [
                {"service": "Amazon Elastic Compute Cloud - Compute", "amount": 30.0},
                {"service": "Amazon Elastic Kubernetes Service", "amount": 12.5},
            ],
            "by_region": {"us-east-1": 30.0, "us-west-2": 12.5},
            "daily": [
                {"date": (NOW - timedelta(days=d)).date().isoformat(), "amount": 1.0 + d}
                for d in range(5)
            ],
            "forecast": {"forecast_total": 50.0},
            "allocation_tags": [
                {"tag_key": "Project", "type": "UserDefined", "status": "Inactive"}
            ],
            "errors": [],
        }
        return self._call("costs", report, days=days)

    def allocation_costs(self, *, days: int = 7, granularity: str = "daily") -> dict[str, Any]:
        report = {
            "namespaces": [
                {
                    "namespace": "gco-inference",
                    "total_cost": "4.62",
                    "cpu_cost": "0.1",
                    "ram_cost": "0.02",
                    "gpu_cost": "4.5",
                    "pv_cost": "0.0",
                },
                {
                    "namespace": "gco-jobs",
                    "total_cost": "0.5",
                    "cpu_cost": "0.4",
                    "ram_cost": "0.1",
                    "gpu_cost": "0.0",
                    "pv_cost": "0.0",
                },
            ],
            "regions": [
                {
                    "region": "us-east-1",
                    "total_cost": "4.59",
                    "cpu_cost": "0.6",
                    "ram_cost": "0.1",
                    "gpu_cost": "3.89",
                },
                {
                    "region": "us-west-2",
                    "total_cost": "0.53",
                    "cpu_cost": "0.4",
                    "ram_cost": "0.13",
                    "gpu_cost": "0.0",
                },
            ],
            "trend": [
                {"period": (NOW - timedelta(days=1)).date().isoformat(), "total_cost": "2.0"},
                {"period": NOW.date().isoformat(), "total_cost": "3.1"},
            ],
            "errors": [],
        }
        return self._call("allocation_costs", report, days=days, granularity=granularity)

    def cost_monitors(self, regions: Sequence[str]) -> list[RegionRead]:
        reads = [
            RegionRead(
                region=region,
                data={
                    "opencost_healthy": True,
                    "opencost_returning_data": True,
                    "report_interval_minutes": 60,
                    "allocation_names": ["gco-jobs", "gco-inference"],
                    "last_scheduled_report": {"window_end": _iso(NOW), "total_cost": 0.2},
                    "last_error": None,
                },
            )
            for region in regions
        ]
        return self._call("cost_monitors", reads, tuple(regions))

    # -- platform ---------------------------------------------------------------
    def global_health(self) -> dict[str, Any]:
        health = {
            "overall_status": "healthy",
            "healthy_regions": 3,
            "total_regions": 3,
            "regions": [
                {"region": r, "status": "healthy", "cluster_id": f"gco-{r}"} for r in REGIONS
            ],
        }
        return self._call("global_health", health)

    def regional_status(self, regions: Sequence[str]) -> list[RegionRead]:
        reads = [
            RegionRead(
                region=region,
                data={
                    "version": "2.0.0",
                    "cluster_id": f"gco-{region}",
                    "manifest_processor_initialized": True,
                    "templates_count": 1,
                    "webhooks_count": 0,
                    "central_queue_worker": {
                        "enabled": True,
                        "running": True,
                        "last_successful_pass_at": _iso(NOW),
                        "last_error": None,
                    },
                },
            )
            for region in regions
        ]
        return self._call("regional_status", reads, tuple(regions))

    def regional_policy(self, regions: Sequence[str]) -> list[RegionRead]:
        reads = [
            RegionRead(
                region=region,
                data={
                    "policy": {
                        "validation_enabled": True,
                        "allowed_namespaces": ["gco-jobs"],
                        "allowed_kinds": ["Job", "Pod"],
                        "manifest_caps": {
                            "max_gpu_count": 16,
                            "configured": {
                                "max_cpu_per_manifest": "384",
                                "max_memory_per_manifest": "4096Gi",
                            },
                        },
                        "trusted_registries": ["docker.io", "public.ecr.aws"],
                        "trusted_dockerhub_orgs": ["vllm"],
                        "manifest_security_policy": {
                            "block_privileged": True,
                            "block_run_as_root": False,
                        },
                    }
                },
            )
            for region in regions
        ]
        return self._call("regional_policy", reads, tuple(regions))

    def templates(self) -> list[dict[str, Any]]:
        templates = [
            {
                "name": "gpu-training",
                "description": "GPU training",
                "manifest": {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "{{name}}"},
                },
                "parameters": {"image": "pytorch/pytorch:latest"},
            }
        ]
        return self._call("templates", templates)

    def webhooks(self) -> list[dict[str, Any]]:
        hooks = [
            {
                "id": "ab12cd34",
                "url": "https://hooks.example.com/gco",
                "events": ["job.completed"],
                "namespace": "gco-jobs",
            }
        ]
        return self._call("webhooks", hooks)

    def create_webhook(self, url: str, events: Sequence[str], **kwargs: Any) -> dict[str, Any]:
        return self._call("create_webhook", {"webhook": {"id": "new"}}, url, list(events), **kwargs)

    def delete_webhook(self, webhook_id: str) -> dict[str, Any]:
        return self._call("delete_webhook", {"message": "deleted"}, webhook_id)

    def create_template(
        self, name: str, manifest: Mapping[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        return self._call("create_template", {"template": {"name": name}}, name, manifest, **kwargs)

    def delete_template(self, name: str) -> dict[str, Any]:
        return self._call("delete_template", {"message": "deleted"}, name)

    def run_template(self, name: str, **kwargs: Any) -> dict[str, Any]:
        return self._call("run_template", {"success": True}, name, **kwargs)

    def api_get(
        self, path: str, *, region: str | None = None, params: Mapping[str, str] | None = None
    ) -> Any:
        answer = {"regions": [{"region": r, "status": "healthy"} for r in REGIONS], "count": 3}
        return self._call("api_get", answer, path, region=region, params=params)

    # -- examples ---------------------------------------------------------------
    def examples(self) -> list[ExampleManifest]:
        example = REPO_ROOT / "examples" / "simple-job.yaml"
        gpu = REPO_ROOT / "examples" / "gpu-job.yaml"
        found = [
            ExampleManifest(
                name="simple-job",
                path=str(example),
                kinds=("Job",),
                gpu=False,
                title="Simple Kubernetes Job Example",
                summary="A minimal job.",
                prerequisites="None",
            ),
            ExampleManifest(
                name="gpu-job",
                path=str(gpu),
                kinds=("Job",),
                gpu=True,
                title="GPU Job Example",
                summary="Runs nvidia-smi.",
                prerequisites="GPU nodepool (enabled by default)",
            ),
        ]
        return self._call("examples", found)


def simple_manifest() -> dict[str, Any]:
    """The checkout's simple-job example, parsed."""
    return load_manifest_text((REPO_ROOT / "examples" / "simple-job.yaml").read_text())[0]


@pytest.fixture(name="fake_fleet")
def fake_fleet_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeFleet]:
    """Point ``ui.fleet`` at a fresh :class:`FakeFleet` with empty Streamlit caches.

    Import ``fake_fleet_fixture`` into a test module (``# noqa: F401``) and
    ask for ``fake_fleet``; the two names differ so the import does not
    shadow the test's parameter.
    """
    import streamlit as st

    from cli.dashboard import ui

    fake = FakeFleet()
    st.cache_data.clear()
    st.cache_resource.clear()
    monkeypatch.setattr(ui, "fleet", lambda: fake)
    monkeypatch.delenv(ui.TOKEN_ENV, raising=False)
    yield fake
    st.cache_data.clear()
    st.cache_resource.clear()


__all__ = [
    "APP",
    "NOW",
    "REGIONS",
    "DashboardError",
    "FakeFleet",
    "fake_fleet_fixture",
    "fleet_status_document",
    "job_info",
    "metrics_read",
    "queue_record",
    "sample_endpoint",
    "sample_jobs",
    "simple_manifest",
    "topology",
]
