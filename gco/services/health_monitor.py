"""
Health Monitor Service for GCO (Global Capacity Orchestrator on AWS).

This service monitors Kubernetes cluster resource utilization and reports
health status for load balancer health checks and monitoring dashboards.

Key Features:
- Collects CPU, memory, and GPU utilization metrics from Kubernetes Metrics Server
- Compares utilization against configurable thresholds
- Reports health status (healthy/unhealthy) based on threshold violations
- Caches metrics to reduce API calls to Kubernetes

Environment Variables:
    CLUSTER_NAME: Name of the EKS cluster being monitored
    REGION: AWS region of the cluster
    CPU_THRESHOLD: CPU utilization threshold percentage (default: 80, -1 to disable)
    MEMORY_THRESHOLD: Memory utilization threshold percentage (default: 80, -1 to disable)
    GPU_THRESHOLD: GPU utilization threshold percentage (default: 60, -1 to disable)

Usage:
    health_monitor = create_health_monitor_from_env()
    status = await health_monitor.get_health_status()
"""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Literal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from gco.job_admission import parse_cpu_millicores, parse_gpu_count, parse_memory_bytes
from gco.k8s_api_versions import METRICS_API_GROUP, METRICS_API_VERSION
from gco.k8s_objects import json_object, present
from gco.k8s_quantity import parse_quantity
from gco.keep_warm import validate_keep_warm_config
from gco.models import HealthStatus, RequestedResources, ResourceThresholds, ResourceUtilization
from gco.services.keep_warm_recorder import KeepWarmRecorder, RecorderPass
from gco.services.leader_lease import (
    LEASE_MIN_DURATION_SECONDS,
    LEASE_REQUEST_TIMEOUT,
    LeaseElector,
    LeaseIdentity,
    lease_duration_from_env,
)
from gco.services.structured_logging import configure_structured_logging

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-11-26T07:28:09Z
# Generated from Git commit: 30e51b52aa66343af9e39715ecf06f6ef43d9100
# Flowchart(s) generated from this file:
#   * ``HealthMonitor.get_health_status`` -> ``diagrams/code_diagrams/gco/services/health_monitor.HealthMonitor_get_health_status.html``
#     (PNG: ``diagrams/code_diagrams/gco/services/health_monitor.HealthMonitor_get_health_status.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Kept as module names for callers/tests that import them; the values live in
# gco.services.leader_lease so every election shares one floor and timeout.
_ALB_SYNC_LEASE_MIN_SECONDS = LEASE_MIN_DURATION_SECONDS
_ALB_SYNC_K8S_TIMEOUT = LEASE_REQUEST_TIMEOUT
_ALB_SYNC_SSM_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"total_max_attempts": 2, "mode": "standard"},
)


#: Namespaces whose pods never count toward pending pods or active Jobs.
_SYSTEM_NAMESPACES = frozenset({"kube-system", "kube-public", "kube-node-lease"})

#: How long the last complete reading stands in when a read fails. After this
#: the cluster is reported unhealthy, naming the failed read.
LAST_GOOD_READING_MAX_AGE_SECONDS = 60.0

#: One complete reading: (utilization, active_jobs, pending_pods, pending_requested).
ClusterReading = tuple[ResourceUtilization, int, int, RequestedResources]


class ClusterReadError(RuntimeError):
    """A read the health status depends on failed.

    ``str()`` names the read only ("listing pods failed"): it ends up in the
    health status, which ``/api/v1/health`` returns without authentication.
    ``detail`` adds the underlying error for the log.
    """

    def __init__(self, what: str, cause: Exception) -> None:
        super().__init__(f"{what} failed")
        self.what = what
        self.cause = cause

    @property
    def detail(self) -> str:
        return f"{self} ({type(self.cause).__name__}: {self.cause})"


async def _read[T](what: str, read: Awaitable[T]) -> T:
    """Await ``read``, turning any failure into a :class:`ClusterReadError` named ``what``."""
    try:
        return await read
    except Exception as exc:
        raise ClusterReadError(what, exc) from exc


def _job_namespaces_from_env() -> frozenset[str]:
    """The job namespaces, as the manifest processor's ``ALLOWED_NAMESPACES`` lists them."""
    raw = os.environ.get("ALLOWED_NAMESPACES", "gco-jobs,default")
    return frozenset(ns.strip() for ns in raw.split(",") if ns.strip())


def _gpu_percent(allocatable: int, requested: int) -> float:
    """``requested`` GPUs as a percentage of ``allocatable`` ones; ``0.0`` when there are none."""
    if allocatable > 0:
        return (requested / allocatable) * 100
    return 0.0


class HealthMonitor:
    """
    Monitors Kubernetes cluster resource utilization and determines health status

    Every Kubernetes read goes through ``asyncio.to_thread``: the official
    client is synchronous, and a blocking call inside the poll task stalls the
    whole event loop — the ``/healthz`` and ``/readyz`` the kubelet probes, and
    the socket bind uvicorn performs right after the lifespan yields. With an
    unreachable API server the client's connect timeout and retries add up to
    minutes, which is exactly the startup-probe budget: the pod was killed for
    "not starting" while it was really waiting on the API server (seen on kind,
    where NetworkPolicy blocked the :6443 endpoint). Off the loop, the service
    keeps serving its probes and its cached status regardless of how the
    cluster reads are doing.
    """

    def __init__(
        self,
        cluster_id: str,
        region: str,
        thresholds: ResourceThresholds,
        *,
        job_namespaces: Iterable[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cluster_id = cluster_id
        self.region = region
        self.thresholds = thresholds
        # Where active Jobs are counted (ALLOWED_NAMESPACES by default).
        self.job_namespaces = (
            frozenset(job_namespaces) if job_namespaces is not None else _job_namespaces_from_env()
        )

        # Initialize Kubernetes clients
        try:
            # Try to load in-cluster config first (when running in pod)
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes configuration")
        except config.ConfigException:
            try:
                # Fall back to local kubeconfig (for development)
                config.load_kube_config()
                logger.info("Loaded local Kubernetes configuration")
            except config.ConfigException as e:
                logger.error(f"Failed to load Kubernetes configuration: {e}")
                raise

        self.core_v1 = client.CoreV1Api()
        self.networking_v1 = client.NetworkingV1Api()
        self.coordination_v1 = client.CoordinationV1Api()
        self.metrics_v1beta1 = client.CustomObjectsApi()
        self.batch_v1 = client.BatchV1Api()

        # Timeout for Kubernetes API calls (seconds)
        self._k8s_timeout = int(os.environ.get("K8S_API_TIMEOUT", "30"))

        # Cache for metrics
        self._last_metrics_time: datetime | None = None
        self._cached_metrics: dict[str, Any] | None = None
        self._cache_duration = 30  # seconds

        # The last complete reading and when it was taken (on ``clock``), and
        # the refresh in flight that concurrent callers share.
        self._clock = clock
        self._last_good_reading: tuple[float, ClusterReading] | None = None
        self._refresh: asyncio.Task[HealthStatus] | None = None

        # ALB hostname sync. Every replica keeps serving health endpoints, but
        # only the holder of this Kubernetes Lease may perform the mutating SSM
        # reconciliation. The Lease is pre-created by 02-rbac.yaml so RBAC can
        # grant update on one exact resource instead of create on every Lease in
        # gco-system.
        self._last_alb_sync: datetime | None = None
        self._alb_sync_interval = 300  # 5 minutes
        self._alb_sync_lease_name = os.environ.get(
            "ALB_SYNC_LEASE_NAME", "gco-health-monitor-alb-sync"
        )
        self._alb_sync_lease_namespace = os.environ.get("POD_NAMESPACE", "gco-system")
        configured_lease_duration = int(os.environ.get("ALB_SYNC_LEASE_DURATION", "90"))
        self._alb_sync_lease_duration = max(configured_lease_duration, _ALB_SYNC_LEASE_MIN_SECONDS)
        if configured_lease_duration < _ALB_SYNC_LEASE_MIN_SECONDS:
            logger.warning(
                "ALB_SYNC_LEASE_DURATION=%s is too short; enforcing %s seconds",
                configured_lease_duration,
                _ALB_SYNC_LEASE_MIN_SECONDS,
            )
        self._alb_sync_holder = (
            os.environ.get("POD_NAME")
            or os.environ.get("HOSTNAME")
            or f"health-monitor-{os.getpid()}"
        )
        self._alb_sync_elector = LeaseElector(
            LeaseIdentity(
                name=self._alb_sync_lease_name,
                namespace=self._alb_sync_lease_namespace,
                holder=self._alb_sync_holder,
                duration_seconds=self._alb_sync_lease_duration,
            ),
            label="ALB-sync",
            request_timeout=_ALB_SYNC_K8S_TIMEOUT,
        )

        # Keep-warm idle intervals (gco.services.keep_warm_recorder): one more
        # elected duty with its own pre-created Lease, recorded on Jobs.
        self.keep_warm_recorder = KeepWarmRecorder(
            self.core_v1,
            self.batch_v1,
            self.coordination_v1,
            elector=LeaseElector(
                LeaseIdentity(
                    name=os.environ.get("KEEP_WARM_LEASE_NAME", "gco-health-monitor-keep-warm"),
                    namespace=self._alb_sync_lease_namespace,
                    holder=self._alb_sync_holder,
                    duration_seconds=lease_duration_from_env(
                        os.environ.get("KEEP_WARM_LEASE_DURATION"),
                        label="KEEP_WARM_LEASE_DURATION",
                    ),
                ),
                label="keep-warm",
                request_timeout=_ALB_SYNC_K8S_TIMEOUT,
            ),
            window_seconds=_keep_warm_window_seconds_from_env(),
            job_namespaces=self.job_namespaces,
            request_timeout=self._k8s_timeout,
        )

    async def record_keep_warm_idle(self) -> RecorderPass:
        """Record keep-warm idle intervals on Jobs, off the event loop (elected)."""
        return await asyncio.to_thread(self.keep_warm_recorder.run_once)

    async def get_cluster_metrics(self) -> tuple[ResourceUtilization, int, int, RequestedResources]:
        """Take one complete reading of the cluster.

        One metrics-server read (cached for ``_cache_duration`` seconds), one
        node list, and one pod list; every figure is computed from those.
        Returns ``(utilization, active_jobs, pending_pods, pending_requested)``.

        Raises:
            ClusterReadError: naming the read that failed. A failed read is
                never turned into zeros, which would look like an idle,
                healthy cluster.
        """
        node_metrics = await _read(
            "reading node metrics from metrics-server", self._get_node_metrics()
        )
        node_list = await _read(
            "listing nodes",
            asyncio.to_thread(self.core_v1.list_node, _request_timeout=self._k8s_timeout),
        )
        pod_list = await _read(
            "listing pods",
            asyncio.to_thread(
                self.core_v1.list_pod_for_all_namespaces, _request_timeout=self._k8s_timeout
            ),
        )
        try:
            nodes = list(node_list.items)
            pods = list(pod_list.items)
            # The counts travel with the percentage: it reads 0.0 both for a
            # cluster without GPU nodes and for idle GPUs.
            gpu_allocatable, gpu_requested = self._gpu_counts(nodes, pods)
            resource_utilization = ResourceUtilization(
                cpu=self._calculate_cpu_utilization(node_metrics, nodes),
                memory=self._calculate_memory_utilization(node_metrics, nodes),
                gpu=_gpu_percent(gpu_allocatable, gpu_requested),
                gpu_allocatable=gpu_allocatable,
                gpu_requested=gpu_requested,
            )
            active_jobs, pending_pods = self._get_pod_counts(pods)
            pending_requested = self._calculate_pending_requested_resources(pods)
        except Exception as exc:
            raise ClusterReadError("reading node and pod resources", exc) from exc

        logger.info(
            f"Cluster metrics - CPU: {resource_utilization.cpu:.1f}%, "
            f"Memory: {resource_utilization.memory:.1f}%, GPU: {resource_utilization.gpu:.1f}%, "
            f"Active Jobs: {active_jobs}, Pending Pods: {pending_pods}, "
            f"Pending Requested CPU: {pending_requested.cpu_vcpus:.1f} vCPUs, "
            f"Pending Requested Memory: {pending_requested.memory_gb:.1f} GB"
        )
        return resource_utilization, active_jobs, pending_pods, pending_requested

    async def _get_node_metrics(self) -> dict[str, Any]:
        """Get node metrics from Kubernetes metrics server"""
        try:
            # Check cache first
            now = datetime.now(UTC)
            if (
                self._cached_metrics
                and self._last_metrics_time
                # total_seconds(): timedelta.seconds is only the seconds field, so a
                # snapshot a day old read as fresh. Bounded below so a wall clock
                # stepped backwards refetches instead of serving the snapshot.
                and 0 <= (now - self._last_metrics_time).total_seconds() < self._cache_duration
            ):
                return self._cached_metrics

            # Fetch fresh metrics
            node_metrics = json_object(
                await asyncio.to_thread(
                    self.metrics_v1beta1.list_cluster_custom_object,
                    group=METRICS_API_GROUP,
                    version=METRICS_API_VERSION,
                    plural="nodes",
                    _request_timeout=self._k8s_timeout,
                ),
                "the node metrics",
            )

            # Update cache
            self._cached_metrics = node_metrics
            self._last_metrics_time = now

            return node_metrics

        except ApiException as e:
            logger.error(f"Failed to get node metrics: {e}")
            # Invalidate cache so stale data isn't used on next call
            self._cached_metrics = None
            self._last_metrics_time = None
            # Re-raise so the reading fails and the health status says why.
            raise

    def _calculate_cpu_utilization(self, node_metrics: dict[str, Any], nodes: list[Any]) -> float:
        """Cluster-wide CPU usage as a percentage of allocatable CPU.

        Can exceed 100: usage includes system-reserved CPU that allocatable
        leaves out. ``0.0`` when there are no nodes.
        """
        total_cpu_capacity = 0.0
        for node in nodes:
            allocatable = present(node.status, "node.status").allocatable or {}
            total_cpu_capacity += parse_cpu_millicores(allocatable.get("cpu"))

        total_cpu_usage = 0.0
        for item in node_metrics.get("items", []):
            # Exact, unrounded: metrics-server reports usage in nano- or
            # microcores, and rounding each node up would bias the sum.
            total_cpu_usage += float(parse_quantity(item["usage"]["cpu"]) * 1000)

        if total_cpu_capacity > 0:
            return (total_cpu_usage / total_cpu_capacity) * 100
        return 0.0

    def _calculate_memory_utilization(
        self, node_metrics: dict[str, Any], nodes: list[Any]
    ) -> float:
        """Cluster-wide memory usage as a percentage of allocatable memory.

        Can exceed 100 for the same reason as CPU. ``0.0`` when there are no nodes.
        """
        total_memory_capacity = 0
        for node in nodes:
            allocatable = present(node.status, "node.status").allocatable or {}
            total_memory_capacity += self._parse_memory_string(allocatable.get("memory", "0"))

        total_memory_usage = 0
        for item in node_metrics.get("items", []):
            total_memory_usage += self._parse_memory_string(item["usage"]["memory"])

        if total_memory_capacity > 0:
            return (total_memory_usage / total_memory_capacity) * 100
        return 0.0

    def _parse_memory_string(self, memory_str: object) -> int:
        """A memory quantity in bytes (any form the API server accepts); empty is 0."""
        return parse_memory_bytes(memory_str)

    def _gpu_counts(self, nodes: list[Any], pods: list[Any]) -> tuple[int, int]:
        """``(allocatable, requested)``: GPUs the nodes offer and GPUs running pods request."""
        total_gpu_capacity = 0
        for node in nodes:
            allocatable = present(node.status, "node.status").allocatable or {}
            total_gpu_capacity += parse_gpu_count(allocatable.get("nvidia.com/gpu"))

        total_gpu_requested = 0
        for pod in pods:
            if present(pod.status, "pod.status").phase == "Running":
                for container in present(pod.spec, "pod.spec").containers:
                    if container.resources and container.resources.requests:
                        total_gpu_requested += parse_gpu_count(
                            container.resources.requests.get("nvidia.com/gpu")
                        )

        return total_gpu_capacity, total_gpu_requested

    def _calculate_gpu_utilization(self, nodes: list[Any], pods: list[Any]) -> float:
        """GPUs requested by running pods as a percentage of allocatable GPUs.

        Can exceed 100, for example while a device plugin restarts and nodes
        briefly report fewer allocatable GPUs than running pods hold. ``0.0``
        when no node has GPUs; :meth:`_gpu_counts` tells that apart from idle GPUs.
        """
        return _gpu_percent(*self._gpu_counts(nodes, pods))

    def _get_pod_counts(self, pods: list[Any]) -> tuple[int, int]:
        """``(active_jobs, pending_pods)`` from one pod list.

        ``active_jobs`` counts Jobs, not pods: the distinct Jobs in the job
        namespaces (``ALLOWED_NAMESPACES``) that own at least one pending or
        running pod, which is what a Job's own ``status.active`` tracks.
        ``pending_pods`` counts every pending pod outside the system
        namespaces.
        """
        active_job_uids: set[str] = set()
        pending_pods = 0

        for pod in pods:
            metadata = present(pod.metadata, "pod.metadata")
            if metadata.namespace in _SYSTEM_NAMESPACES:
                continue
            phase = present(pod.status, "pod.status").phase
            if phase == "Pending":
                pending_pods += 1
            if phase in ("Pending", "Running") and metadata.namespace in self.job_namespaces:
                for owner in metadata.owner_references or []:
                    if owner.kind == "Job":
                        active_job_uids.add(owner.uid)

        return len(active_job_uids), pending_pods

    def _calculate_pending_requested_resources(self, pods: list[Any]) -> RequestedResources:
        """Total resources requested by pending pods outside the system namespaces."""
        total_cpu_millicores = 0.0
        total_memory_bytes = 0
        total_gpus = 0

        for pod in pods:
            if present(pod.metadata, "pod.metadata").namespace in _SYSTEM_NAMESPACES:
                continue
            # Only count pending pods
            if present(pod.status, "pod.status").phase != "Pending":
                continue
            for container in present(pod.spec, "pod.spec").containers:
                if container.resources and container.resources.requests:
                    requests = container.resources.requests
                    total_cpu_millicores += parse_cpu_millicores(requests.get("cpu"))
                    total_memory_bytes += self._parse_memory_string(requests.get("memory", "0"))
                    total_gpus += parse_gpu_count(requests.get("nvidia.com/gpu"))

        # Convert to vCPUs and GB
        cpu_vcpus = total_cpu_millicores / 1000
        memory_gb = total_memory_bytes / (1024 * 1024 * 1024)

        return RequestedResources(cpu_vcpus=cpu_vcpus, memory_gb=memory_gb, gpus=total_gpus)

    async def get_health_status(self) -> HealthStatus:
        """Get current health status of the cluster.

        Single-flight: a caller that arrives while a refresh runs (the
        background loop and an API request, say) shares that refresh instead
        of starting its own set of reads. A cancelled caller does not cancel
        the shared refresh.
        """
        refresh = self._refresh
        if refresh is None or refresh.done():
            refresh = asyncio.create_task(self._refresh_health_status())
            self._refresh = refresh
        return await asyncio.shield(refresh)

    async def _refresh_health_status(self) -> HealthStatus:
        """Read the cluster and judge it against the thresholds.

        When a read fails, the last complete reading stands in for up to
        ``LAST_GOOD_READING_MAX_AGE_SECONDS`` (the message says which read
        failed and how old the reading is). After that the cluster is
        reported unhealthy, with the failed read named in the message: a
        failed read is never reported as healthy.
        """
        stale_note: str | None = None
        try:
            reading = await self.get_cluster_metrics()
        except Exception as e:
            # The status message (served without authentication) names the
            # failed read; the error itself goes to the log only.
            reason = str(e) if isinstance(e, ClusterReadError) else "reading the cluster failed"
            detail = e.detail if isinstance(e, ClusterReadError) else f"{type(e).__name__}: {e}"
            now = self._clock()
            last_good = self._last_good_reading
            if last_good is None or now - last_good[0] > LAST_GOOD_READING_MAX_AGE_SECONDS:
                logger.error(f"Error getting health status: {detail}")
                return self._unhealthy(f"Health check error: {reason}")
            age = now - last_good[0]
            logger.warning(f"Cluster read failed; using the reading from {age:.0f}s ago: {detail}")
            reading = last_good[1]
            stale_note = f"Using the reading from {age:.0f}s ago ({reason})"
        else:
            self._last_good_reading = (self._clock(), reading)

        try:
            return self._evaluate(*reading, stale_note=stale_note)
        except Exception as e:
            logger.error(f"Error getting health status: {type(e).__name__}: {e}")
            return self._unhealthy("Health check error: the health status could not be computed")

    def _unhealthy(self, message: str) -> HealthStatus:
        return HealthStatus(
            cluster_id=self.cluster_id,
            region=self.region,
            timestamp=datetime.now(UTC),
            status="unhealthy",
            resource_utilization=ResourceUtilization(cpu=0.0, memory=0.0, gpu=0.0),
            thresholds=self.thresholds,
            active_jobs=0,
            pending_pods=0,
            pending_requested=RequestedResources(cpu_vcpus=0.0, memory_gb=0.0, gpus=0),
            message=message,
        )

    def _evaluate(
        self,
        resource_utilization: ResourceUtilization,
        active_jobs: int,
        pending_pods: int,
        pending_requested: RequestedResources,
        *,
        stale_note: str | None = None,
    ) -> HealthStatus:
        """Judge one reading against the thresholds (-1 disables a check)."""
        # Determine health status based on thresholds
        # A threshold of -1 means that check is disabled
        is_healthy = True
        if not self.thresholds.is_disabled("cpu_threshold"):
            is_healthy = is_healthy and resource_utilization.cpu <= self.thresholds.cpu_threshold
        if not self.thresholds.is_disabled("memory_threshold"):
            is_healthy = (
                is_healthy and resource_utilization.memory <= self.thresholds.memory_threshold
            )
        if not self.thresholds.is_disabled("gpu_threshold"):
            is_healthy = is_healthy and resource_utilization.gpu <= self.thresholds.gpu_threshold
        if not self.thresholds.is_disabled("pending_pods_threshold"):
            is_healthy = is_healthy and pending_pods <= self.thresholds.pending_pods_threshold
        if not self.thresholds.is_disabled("pending_requested_cpu_vcpus"):
            is_healthy = (
                is_healthy
                and pending_requested.cpu_vcpus <= self.thresholds.pending_requested_cpu_vcpus
            )
        if not self.thresholds.is_disabled("pending_requested_memory_gb"):
            is_healthy = (
                is_healthy
                and pending_requested.memory_gb <= self.thresholds.pending_requested_memory_gb
            )
        if not self.thresholds.is_disabled("pending_requested_gpus"):
            is_healthy = (
                is_healthy and pending_requested.gpus <= self.thresholds.pending_requested_gpus
            )

        status: Literal["healthy", "unhealthy"] = "healthy" if is_healthy else "unhealthy"

        # Generate status message
        message = None
        if not is_healthy:
            violations = []
            if (
                not self.thresholds.is_disabled("cpu_threshold")
                and resource_utilization.cpu > self.thresholds.cpu_threshold
            ):
                violations.append(
                    f"CPU: {resource_utilization.cpu:.1f}% > {self.thresholds.cpu_threshold}%"
                )
            if (
                not self.thresholds.is_disabled("memory_threshold")
                and resource_utilization.memory > self.thresholds.memory_threshold
            ):
                violations.append(
                    f"Memory: {resource_utilization.memory:.1f}% > {self.thresholds.memory_threshold}%"
                )
            if (
                not self.thresholds.is_disabled("gpu_threshold")
                and resource_utilization.gpu > self.thresholds.gpu_threshold
            ):
                violations.append(
                    f"GPU: {resource_utilization.gpu:.1f}% > {self.thresholds.gpu_threshold}%"
                )
            if (
                not self.thresholds.is_disabled("pending_pods_threshold")
                and pending_pods > self.thresholds.pending_pods_threshold
            ):
                violations.append(
                    f"Pending Pods: {pending_pods} > {self.thresholds.pending_pods_threshold}"
                )
            if (
                not self.thresholds.is_disabled("pending_requested_cpu_vcpus")
                and pending_requested.cpu_vcpus > self.thresholds.pending_requested_cpu_vcpus
            ):
                violations.append(
                    f"Pending CPU: {pending_requested.cpu_vcpus:.1f} vCPUs > {self.thresholds.pending_requested_cpu_vcpus} vCPUs"
                )
            if (
                not self.thresholds.is_disabled("pending_requested_memory_gb")
                and pending_requested.memory_gb > self.thresholds.pending_requested_memory_gb
            ):
                violations.append(
                    f"Pending Memory: {pending_requested.memory_gb:.1f} GB > {self.thresholds.pending_requested_memory_gb} GB"
                )
            if (
                not self.thresholds.is_disabled("pending_requested_gpus")
                and pending_requested.gpus > self.thresholds.pending_requested_gpus
            ):
                violations.append(
                    f"Pending GPUs: {pending_requested.gpus} > {self.thresholds.pending_requested_gpus}"
                )
            message = f"Threshold violations: {', '.join(violations)}"

        if stale_note is not None:
            message = f"{message}; {stale_note}" if message else stale_note

        health_status = HealthStatus(
            cluster_id=self.cluster_id,
            region=self.region,
            timestamp=datetime.now(UTC),
            status=status,
            resource_utilization=resource_utilization,
            thresholds=self.thresholds,
            active_jobs=active_jobs,
            pending_pods=pending_pods,
            pending_requested=pending_requested,
            message=message,
        )

        logger.info(f"Health status: {status} - {message or 'All thresholds within limits'}")
        return health_status

    def _try_acquire_alb_sync_lease(self) -> bool:
        """Acquire or renew the single-writer Lease for ALB self-healing.

        Shared election rules live in :mod:`gco.services.leader_lease`:
        optimistic ``replace`` (a racing writer gets HTTP 409), a holder whose
        record this replica has seen unchanged for the lease duration (or
        that never recorded a renewal) may be replaced, and every API or RBAC
        failure returns ``False`` — losing self-healing is safer than allowing
        two replicas to mutate the cross-region SSM parameter.
        """
        return self._alb_sync_elector.try_acquire(self.coordination_v1).held

    async def sync_alb_registration(self) -> None:
        """Run ALB self-healing without blocking FastAPI's event loop."""
        await asyncio.to_thread(self._sync_alb_registration)

    def _sync_alb_registration(self) -> None:
        """Ensure the SSM hostname matches the platform Gateway address.

        Every replica renews or checks the leader Lease on each health loop;
        only the leader performs reconciliation, at most once every 5 minutes.
        A second optimistic Lease renewal immediately before ``PutParameter``
        prevents a stale former leader from writing. The SSM client's bounded
        retry/timeouts keep that write comfortably inside the Lease duration.
        """
        if not self._try_acquire_alb_sync_lease():
            return

        now = datetime.now(UTC)
        if (
            self._last_alb_sync
            and (now - self._last_alb_sync).total_seconds() < self._alb_sync_interval
        ):
            return

        self._last_alb_sync = now

        try:
            gateway = json_object(
                self.metrics_v1beta1.get_namespaced_custom_object(
                    group="gateway.networking.k8s.io",
                    version="v1",
                    namespace="gco-system",
                    plural="gateways",
                    name="gco-gateway",
                    _request_timeout=self._k8s_timeout,
                ),
                "the Gateway",
            )
            addresses = gateway.get("status", {}).get("addresses", [])
            current_hostname = next(
                (
                    str(address.get("value", "")).strip()
                    for address in addresses
                    if isinstance(address, dict)
                    and address.get("type", "Hostname") == "Hostname"
                    and str(address.get("value", "")).strip()
                ),
                None,
            )
            if not current_hostname:
                return

            global_region = os.environ.get("GLOBAL_REGION", "us-east-2")
            project_name = os.environ.get("PROJECT_NAME", "gco")
            param_name = f"/{project_name}/alb-hostname-{self.region}"
            ssm_client = boto3.client(
                "ssm",
                region_name=global_region,
                config=_ALB_SYNC_SSM_CONFIG,
            )
            try:
                response = ssm_client.get_parameter(Name=param_name)
                stored_hostname = str(response["Parameter"]["Value"])
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ParameterNotFound":
                    raise
                stored_hostname = None

            if stored_hostname != current_hostname:
                logger.warning(
                    "ALB hostname mismatch: SSM=%s, Gateway=%s. Updating SSM.",
                    stored_hostname,
                    current_hostname,
                )
                # Re-read and optimistically replace the Lease immediately
                # before the only mutating AWS call. A 409 or a new live holder
                # makes this replica fail closed.
                if not self._try_acquire_alb_sync_lease():
                    logger.info("Lost ALB-sync Lease before SSM update; skipping mutation")
                    return
                ssm_client.put_parameter(
                    Name=param_name,
                    Value=current_hostname,
                    Type="String",
                    Overwrite=True,
                )
                logger.info("Updated SSM parameter %s to %s", param_name, current_hostname)

        except Exception as exc:
            logger.warning("Gateway ALB sync check failed (non-fatal): %s", exc)


def _keep_warm_window_seconds_from_env() -> int:
    """The keep-warm pool's window from ``GPU_KEEP_WARM_WINDOW_SECONDS``.

    The manifest renders it from the validated cdk.json block; a missing or
    unreadable value falls back to the default window with a warning rather
    than stopping the health monitor over a number it only reports.
    """
    default = int(validate_keep_warm_config(None)["window_seconds"])
    raw = os.environ.get("GPU_KEEP_WARM_WINDOW_SECONDS")
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        logger.warning("GPU_KEEP_WARM_WINDOW_SECONDS=%r is not an integer; using %s", raw, default)
        return default
    return value if value > 0 else default


def thresholds_from_env() -> ResourceThresholds:
    """The thresholds the health monitor applies, from its environment.

    The one place their defaults live: ``/api/v1/status`` reports these same
    values. The manifest sets every variable from ``cdk.json``; the defaults
    apply only to a run without them.
    """
    cpu_threshold = int(os.getenv("CPU_THRESHOLD", "80"))
    memory_threshold = int(os.getenv("MEMORY_THRESHOLD", "80"))
    gpu_threshold = int(os.getenv("GPU_THRESHOLD", "60"))
    pending_pods_threshold = int(os.getenv("PENDING_PODS_THRESHOLD", "10"))
    pending_requested_cpu_vcpus = int(os.getenv("PENDING_REQUESTED_CPU_VCPUS", "100"))
    pending_requested_memory_gb = int(os.getenv("PENDING_REQUESTED_MEMORY_GB", "200"))
    pending_requested_gpus = int(os.getenv("PENDING_REQUESTED_GPUS", "8"))

    return ResourceThresholds(
        cpu_threshold=cpu_threshold,
        memory_threshold=memory_threshold,
        gpu_threshold=gpu_threshold,
        pending_pods_threshold=pending_pods_threshold,
        pending_requested_cpu_vcpus=pending_requested_cpu_vcpus,
        pending_requested_memory_gb=pending_requested_memory_gb,
        pending_requested_gpus=pending_requested_gpus,
    )


def create_health_monitor_from_env() -> HealthMonitor:
    """
    Create HealthMonitor instance from environment variables
    """
    cluster_id = os.getenv("CLUSTER_NAME", "unknown-cluster")
    region = os.getenv("REGION", "unknown-region")

    return HealthMonitor(cluster_id, region, thresholds_from_env())


async def main() -> None:
    """
    Main function for running the health monitor with webhook dispatcher.

    This runs both the health monitoring loop and the webhook dispatcher
    as concurrent tasks.
    """
    from gco.services.webhook_dispatcher import create_webhook_dispatcher_from_env

    health_monitor = create_health_monitor_from_env()

    # Enable structured JSON logging for CloudWatch Insights
    configure_structured_logging(
        service_name="health-monitor",
        cluster_id=health_monitor.cluster_id,
        region=health_monitor.region,
    )

    webhook_dispatcher = create_webhook_dispatcher_from_env()

    # Start webhook dispatcher
    await webhook_dispatcher.start()
    logger.info("Webhook dispatcher started")

    try:
        while True:
            try:
                health_status = await health_monitor.get_health_status()
                print(f"Health Status: {health_status.status}")
                print(f"CPU: {health_status.resource_utilization.cpu:.1f}%")
                print(f"Memory: {health_status.resource_utilization.memory:.1f}%")
                print(f"GPU: {health_status.resource_utilization.gpu:.1f}%")
                print(f"Active Jobs: {health_status.active_jobs}")
                print(f"Pending Pods: {health_status.pending_pods}")
                if health_status.pending_requested:
                    print(
                        f"Pending Requested CPU: {health_status.pending_requested.cpu_vcpus:.1f} vCPUs"
                    )
                    print(
                        f"Pending Requested Memory: {health_status.pending_requested.memory_gb:.1f} GB"
                    )
                if health_status.message:
                    print(f"Message: {health_status.message}")

                # Print webhook dispatcher metrics
                webhook_metrics = webhook_dispatcher.get_metrics()
                print(
                    f"Webhook Deliveries: {webhook_metrics['deliveries_total']} "
                    f"(success={webhook_metrics['deliveries_success']}, "
                    f"failed={webhook_metrics['deliveries_failed']})"
                )
                print("-" * 50)

                await asyncio.sleep(30)  # Check every 30 seconds

            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(10)

    except KeyboardInterrupt:
        logger.info("Health monitor stopped by user")
    finally:
        await webhook_dispatcher.stop()
        logger.info("Webhook dispatcher stopped")


if __name__ == "__main__":
    asyncio.run(main())
