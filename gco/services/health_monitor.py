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
    MEMORY_THRESHOLD: Memory utilization threshold percentage (default: 85, -1 to disable)
    GPU_THRESHOLD: GPU utilization threshold percentage (default: 90, -1 to disable)

Usage:
    health_monitor = create_health_monitor_from_env()
    status = await health_monitor.get_health_status()
"""

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any, Literal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from gco.models import HealthStatus, RequestedResources, ResourceThresholds, ResourceUtilization
from gco.services.structured_logging import configure_structured_logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

_ALB_SYNC_LEASE_MIN_SECONDS = 60
_ALB_SYNC_K8S_TIMEOUT = (3, 10)
_ALB_SYNC_SSM_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"total_max_attempts": 2, "mode": "standard"},
)


class HealthMonitor:
    """
    Monitors Kubernetes cluster resource utilization and determines health status
    """

    def __init__(self, cluster_id: str, region: str, thresholds: ResourceThresholds):
        self.cluster_id = cluster_id
        self.region = region
        self.thresholds = thresholds

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

        # Timeout for Kubernetes API calls (seconds)
        self._k8s_timeout = int(os.environ.get("K8S_API_TIMEOUT", "30"))

        # Cache for metrics
        self._last_metrics_time: datetime | None = None
        self._cached_metrics: dict[str, Any] | None = None
        self._cache_duration = 30  # seconds

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

    async def get_cluster_metrics(self) -> tuple[ResourceUtilization, int, int, RequestedResources]:
        """
        Get current cluster resource utilization metrics
        Returns: (ResourceUtilization, active_jobs_count, pending_pods_count, pending_requested_resources)
        """
        try:
            # Get node metrics from metrics server
            node_metrics = await self._get_node_metrics()

            # Get pod metrics for active jobs count and pending pods
            active_jobs, pending_pods = await self._get_pod_counts()

            # Calculate cluster-wide utilization
            cpu_utilization = self._calculate_cpu_utilization(node_metrics)
            memory_utilization = self._calculate_memory_utilization(node_metrics)
            gpu_utilization = await self._calculate_gpu_utilization()

            # Calculate resources requested by pending pods
            pending_requested = await self._calculate_pending_requested_resources()

            resource_utilization = ResourceUtilization(
                cpu=cpu_utilization, memory=memory_utilization, gpu=gpu_utilization
            )

            logger.info(
                f"Cluster metrics - CPU: {cpu_utilization:.1f}%, "
                f"Memory: {memory_utilization:.1f}%, GPU: {gpu_utilization:.1f}%, "
                f"Active Jobs: {active_jobs}, Pending Pods: {pending_pods}, "
                f"Pending Requested CPU: {pending_requested.cpu_vcpus:.1f} vCPUs, "
                f"Pending Requested Memory: {pending_requested.memory_gb:.1f} GB"
            )

            return resource_utilization, active_jobs, pending_pods, pending_requested

        except Exception as e:
            logger.error(f"Failed to get cluster metrics: {e}")
            # Re-raise so get_health_status returns "unhealthy" instead of
            # silently reporting 0% utilization (which looks healthy to GA).
            raise

    async def _get_node_metrics(self) -> dict[str, Any]:
        """Get node metrics from Kubernetes metrics server"""
        try:
            # Check cache first
            now = datetime.now()
            if (
                self._cached_metrics
                and self._last_metrics_time
                and (now - self._last_metrics_time).seconds < self._cache_duration
            ):
                return self._cached_metrics

            # Fetch fresh metrics
            node_metrics: dict[str, Any] = self.metrics_v1beta1.list_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="nodes",
                _request_timeout=self._k8s_timeout,
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
            # Re-raise so get_cluster_metrics propagates the failure
            # to get_health_status, which returns "unhealthy"
            raise

    def _calculate_cpu_utilization(self, node_metrics: dict[str, Any]) -> float:
        """Calculate cluster-wide CPU utilization percentage"""
        total_cpu_usage = 0.0
        total_cpu_capacity = 0.0

        try:
            # Get node list for capacity information
            nodes = self.core_v1.list_node(_request_timeout=self._k8s_timeout)
            node_capacities = {}

            for node in nodes.items:
                node_name = node.metadata.name
                cpu_capacity = node.status.allocatable.get("cpu", "0")
                # Convert CPU capacity to millicores
                if cpu_capacity.endswith("m"):
                    cpu_capacity_millicores = int(cpu_capacity[:-1])
                else:
                    cpu_capacity_millicores = int(cpu_capacity) * 1000
                node_capacities[node_name] = cpu_capacity_millicores
                total_cpu_capacity += cpu_capacity_millicores

            # Calculate usage from metrics
            for item in node_metrics.get("items", []):
                cpu_usage = item["usage"]["cpu"]

                # Convert CPU usage to millicores
                if cpu_usage.endswith("n"):
                    cpu_usage_millicores = int(cpu_usage[:-1]) / 1_000_000
                elif cpu_usage.endswith("u"):
                    cpu_usage_millicores = int(cpu_usage[:-1]) / 1_000
                elif cpu_usage.endswith("m"):
                    cpu_usage_millicores = int(cpu_usage[:-1])
                else:
                    cpu_usage_millicores = int(cpu_usage) * 1000

                total_cpu_usage += cpu_usage_millicores

            if total_cpu_capacity > 0:
                return (total_cpu_usage / total_cpu_capacity) * 100

        except Exception as e:
            logger.error(f"Error calculating CPU utilization: {e}")

        return 0.0

    def _calculate_memory_utilization(self, node_metrics: dict[str, Any]) -> float:
        """Calculate cluster-wide memory utilization percentage"""
        total_memory_usage = 0
        total_memory_capacity = 0

        try:
            # Get node list for capacity information
            nodes = self.core_v1.list_node(_request_timeout=self._k8s_timeout)

            for node in nodes.items:
                memory_capacity = node.status.allocatable.get("memory", "0")
                # Convert memory capacity to bytes
                memory_capacity_bytes = self._parse_memory_string(memory_capacity)
                total_memory_capacity += memory_capacity_bytes

            # Calculate usage from metrics
            for item in node_metrics.get("items", []):
                memory_usage = item["usage"]["memory"]
                memory_usage_bytes = self._parse_memory_string(memory_usage)
                total_memory_usage += memory_usage_bytes

            if total_memory_capacity > 0:
                return (total_memory_usage / total_memory_capacity) * 100

        except Exception as e:
            logger.error(f"Error calculating memory utilization: {e}")

        return 0.0

    def _parse_memory_string(self, memory_str: str) -> int:
        """Parse Kubernetes memory string to bytes"""
        if not memory_str:
            return 0

        memory_str = memory_str.strip()

        # Handle different units
        if memory_str.endswith("Ki"):
            return int(memory_str[:-2]) * 1024
        if memory_str.endswith("Mi"):
            return int(memory_str[:-2]) * 1024 * 1024
        if memory_str.endswith("Gi"):
            return int(memory_str[:-2]) * 1024 * 1024 * 1024
        if memory_str.endswith("Ti"):
            return int(memory_str[:-2]) * 1024 * 1024 * 1024 * 1024
        if memory_str.endswith("k"):
            return int(memory_str[:-1]) * 1000
        if memory_str.endswith("M"):
            return int(memory_str[:-1]) * 1000 * 1000
        if memory_str.endswith("G"):
            return int(memory_str[:-1]) * 1000 * 1000 * 1000
        return int(memory_str)

    async def _calculate_gpu_utilization(self) -> float:
        """Calculate cluster-wide GPU utilization percentage"""
        try:
            # Get pods with GPU requests
            pods = self.core_v1.list_pod_for_all_namespaces(
                _request_timeout=self._k8s_timeout,
            )

            total_gpu_requested = 0
            total_gpu_capacity = 0

            # Get node GPU capacity
            nodes = self.core_v1.list_node(_request_timeout=self._k8s_timeout)
            for node in nodes.items:
                gpu_capacity = node.status.allocatable.get("nvidia.com/gpu", "0")
                total_gpu_capacity += int(gpu_capacity)

            # Calculate GPU requests from running pods
            for pod in pods.items:
                if pod.status.phase == "Running":
                    for container in pod.spec.containers:
                        if container.resources and container.resources.requests:
                            gpu_request = container.resources.requests.get("nvidia.com/gpu", "0")
                            total_gpu_requested += int(gpu_request)

            if total_gpu_capacity > 0:
                return (total_gpu_requested / total_gpu_capacity) * 100

        except Exception as e:
            logger.error(f"Error calculating GPU utilization: {e}")

        return 0.0

    async def _get_active_jobs_count(self) -> int:
        """Get count of active jobs in the cluster"""
        try:
            # Count running pods (excluding system pods)
            pods = self.core_v1.list_pod_for_all_namespaces(
                _request_timeout=self._k8s_timeout,
            )
            active_jobs = 0

            for pod in pods.items:
                # Skip system namespaces
                if pod.metadata.namespace in ["kube-system", "kube-public", "kube-node-lease"]:
                    continue

                # Count running pods as active jobs
                if pod.status.phase == "Running":
                    active_jobs += 1

            return active_jobs

        except Exception as e:
            logger.error(f"Error getting active jobs count: {e}")
            return 0

    async def _get_pod_counts(self) -> tuple[int, int]:
        """Get count of active jobs and pending pods in the cluster"""
        try:
            pods = self.core_v1.list_pod_for_all_namespaces(
                _request_timeout=self._k8s_timeout,
            )
            active_jobs = 0
            pending_pods = 0

            for pod in pods.items:
                # Skip system namespaces
                if pod.metadata.namespace in ["kube-system", "kube-public", "kube-node-lease"]:
                    continue

                if pod.status.phase == "Running":
                    active_jobs += 1
                elif pod.status.phase == "Pending":
                    pending_pods += 1

            return active_jobs, pending_pods

        except Exception as e:
            logger.error(f"Error getting pod counts: {e}")
            return 0, 0

    async def _calculate_pending_requested_resources(self) -> RequestedResources:
        """Calculate total resources requested by pending pods"""
        try:
            pods = self.core_v1.list_pod_for_all_namespaces(
                _request_timeout=self._k8s_timeout,
            )
            total_cpu_millicores = 0.0
            total_memory_bytes = 0
            total_gpus = 0

            for pod in pods.items:
                # Skip system namespaces
                if pod.metadata.namespace in ["kube-system", "kube-public", "kube-node-lease"]:
                    continue

                # Only count pending pods
                if pod.status.phase != "Pending":
                    continue

                for container in pod.spec.containers:
                    if container.resources and container.resources.requests:
                        # CPU
                        cpu_request = container.resources.requests.get("cpu", "0")
                        if cpu_request.endswith("m"):
                            total_cpu_millicores += int(cpu_request[:-1])
                        elif cpu_request.endswith("n"):
                            total_cpu_millicores += int(cpu_request[:-1]) / 1_000_000
                        else:
                            total_cpu_millicores += float(cpu_request) * 1000

                        # Memory
                        memory_request = container.resources.requests.get("memory", "0")
                        total_memory_bytes += self._parse_memory_string(memory_request)

                        # GPUs
                        gpu_request = container.resources.requests.get("nvidia.com/gpu", "0")
                        total_gpus += int(gpu_request)

            # Convert to vCPUs and GB
            cpu_vcpus = total_cpu_millicores / 1000
            memory_gb = total_memory_bytes / (1024 * 1024 * 1024)

            return RequestedResources(cpu_vcpus=cpu_vcpus, memory_gb=memory_gb, gpus=total_gpus)

        except Exception as e:
            logger.error(f"Error calculating pending requested resources: {e}")
            return RequestedResources(cpu_vcpus=0.0, memory_gb=0.0, gpus=0)

    async def get_health_status(self) -> HealthStatus:
        """
        Get current health status of the cluster
        """
        try:
            # Get current metrics
            (
                resource_utilization,
                active_jobs,
                pending_pods,
                pending_requested,
            ) = await self.get_cluster_metrics()

            # Determine health status based on thresholds
            # A threshold of -1 means that check is disabled
            is_healthy = True
            if not self.thresholds.is_disabled("cpu_threshold"):
                is_healthy = (
                    is_healthy and resource_utilization.cpu <= self.thresholds.cpu_threshold
                )
            if not self.thresholds.is_disabled("memory_threshold"):
                is_healthy = (
                    is_healthy and resource_utilization.memory <= self.thresholds.memory_threshold
                )
            if not self.thresholds.is_disabled("gpu_threshold"):
                is_healthy = (
                    is_healthy and resource_utilization.gpu <= self.thresholds.gpu_threshold
                )
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

            health_status = HealthStatus(
                cluster_id=self.cluster_id,
                region=self.region,
                timestamp=datetime.now(),
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

        except Exception as e:
            logger.error(f"Error getting health status: {e}")
            # Return unhealthy status on error
            return HealthStatus(
                cluster_id=self.cluster_id,
                region=self.region,
                timestamp=datetime.now(),
                status="unhealthy",
                resource_utilization=ResourceUtilization(cpu=0.0, memory=0.0, gpu=0.0),
                thresholds=self.thresholds,
                active_jobs=0,
                pending_pods=0,
                pending_requested=RequestedResources(cpu_vcpus=0.0, memory_gb=0.0, gpus=0),
                message=f"Health check error: {e!s}",
            )

    def _try_acquire_alb_sync_lease(self) -> bool:
        """Acquire or renew the single-writer Lease for ALB self-healing.

        ``replace_namespaced_lease`` carries the resourceVersion returned by
        the read, so Kubernetes rejects a racing writer with HTTP 409. API or
        RBAC failures return ``False``: losing self-healing is safer than
        allowing two replicas to mutate the cross-region SSM parameter.
        """
        observed_at = datetime.now(UTC)

        try:
            lease = self.coordination_v1.read_namespaced_lease(
                self._alb_sync_lease_name,
                self._alb_sync_lease_namespace,
                _request_timeout=_ALB_SYNC_K8S_TIMEOUT,
            )
            spec = lease.spec
            current_holder = spec.holder_identity
            renew_time = spec.renew_time
            lease_duration = spec.lease_duration_seconds or self._alb_sync_lease_duration

            expired = False
            if current_holder:
                if renew_time is None:
                    # A holder without a renewal timestamp cannot prove it still
                    # owns the lease. Treat it as expired so the named Lease
                    # cannot remain wedged indefinitely.
                    expired = True
                else:
                    if renew_time.tzinfo is None:
                        renew_time = renew_time.replace(tzinfo=UTC)
                    expired = (observed_at - renew_time).total_seconds() >= lease_duration

            if current_holder not in (None, "", self._alb_sync_holder) and not expired:
                return False

            acquiring = current_holder != self._alb_sync_holder
            renewed_at = datetime.now(UTC)
            if acquiring:
                spec.holder_identity = self._alb_sync_holder
                spec.acquire_time = renewed_at
                spec.lease_transitions = (spec.lease_transitions or 0) + 1
            spec.lease_duration_seconds = self._alb_sync_lease_duration
            spec.renew_time = renewed_at

            try:
                self.coordination_v1.replace_namespaced_lease(
                    self._alb_sync_lease_name,
                    self._alb_sync_lease_namespace,
                    lease,
                    _request_timeout=_ALB_SYNC_K8S_TIMEOUT,
                )
            except ApiException as exc:
                if exc.status == 409:
                    logger.debug("Lost ALB-sync Lease race to another health-monitor replica")
                    return False
                raise

            if acquiring:
                logger.info("Acquired ALB-sync leader Lease as %s", self._alb_sync_holder)
            return True

        except ApiException as exc:
            if exc.status == 404:
                logger.warning(
                    "ALB-sync Lease %s/%s is missing; self-healing is disabled until it is restored",
                    self._alb_sync_lease_namespace,
                    self._alb_sync_lease_name,
                )
            else:
                logger.warning("ALB-sync Lease check failed (non-fatal): %s", exc)
            return False
        except Exception as exc:
            logger.warning("ALB-sync Lease check failed (non-fatal): %s", exc)
            return False

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

        now = datetime.now()
        if (
            self._last_alb_sync
            and (now - self._last_alb_sync).total_seconds() < self._alb_sync_interval
        ):
            return

        self._last_alb_sync = now

        try:
            gateway = self.metrics_v1beta1.get_namespaced_custom_object(
                group="gateway.networking.k8s.io",
                version="v1",
                namespace="gco-system",
                plural="gateways",
                name="gco-gateway",
                _request_timeout=self._k8s_timeout,
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


def create_health_monitor_from_env() -> HealthMonitor:
    """
    Create HealthMonitor instance from environment variables
    """
    cluster_id = os.getenv("CLUSTER_NAME", "unknown-cluster")
    region = os.getenv("REGION", "unknown-region")

    # Load thresholds from environment (defaults match cdk.json)
    cpu_threshold = int(os.getenv("CPU_THRESHOLD", "60"))
    memory_threshold = int(os.getenv("MEMORY_THRESHOLD", "60"))
    gpu_threshold = int(os.getenv("GPU_THRESHOLD", "60"))
    pending_pods_threshold = int(os.getenv("PENDING_PODS_THRESHOLD", "10"))
    pending_requested_cpu_vcpus = int(os.getenv("PENDING_REQUESTED_CPU_VCPUS", "100"))
    pending_requested_memory_gb = int(os.getenv("PENDING_REQUESTED_MEMORY_GB", "200"))
    pending_requested_gpus = int(os.getenv("PENDING_REQUESTED_GPUS", "8"))

    thresholds = ResourceThresholds(
        cpu_threshold=cpu_threshold,
        memory_threshold=memory_threshold,
        gpu_threshold=gpu_threshold,
        pending_pods_threshold=pending_pods_threshold,
        pending_requested_cpu_vcpus=pending_requested_cpu_vcpus,
        pending_requested_memory_gb=pending_requested_memory_gb,
        pending_requested_gpus=pending_requested_gpus,
    )

    return HealthMonitor(cluster_id, region, thresholds)


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
