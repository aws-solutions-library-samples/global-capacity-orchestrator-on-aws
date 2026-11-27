"""
CloudWatch Metrics Publisher for GCO (Global Capacity Orchestrator on AWS).

This module provides classes for publishing custom metrics to CloudWatch
for monitoring and alerting. It supports both single metric puts and
batch operations for efficiency.

Metric Namespaces:
- GCO/HealthMonitor: Cluster health and resource utilization metrics
- GCO/ManifestProcessor: Manifest submission and processing metrics

Common Dimensions:
- ClusterName: EKS cluster identifier
- Region: AWS region

Usage:
    # Health monitor metrics
    metrics = create_health_monitor_metrics()
    metrics.publish_resource_utilization(cpu=45.2, memory=62.1, gpu=0.0, active_jobs=5)

    # Manifest processor metrics
    metrics = create_manifest_processor_metrics()
    metrics.publish_submission_metrics(total=10, successful=9, failed=1, validation_failures=0)
"""

import logging
import os
from datetime import datetime
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class MetricsPublisher:
    """
    Publishes custom metrics to CloudWatch
    """

    def __init__(self, namespace: str, cluster_name: str, region: str):
        self.namespace = namespace
        self.cluster_name = cluster_name
        self.region = region

        # Initialize CloudWatch client. Publishing runs beside the services'
        # request handling, so a call gives up in seconds rather than after
        # botocore's 60 s timeouts and legacy retries.
        try:
            self.cloudwatch = boto3.client(
                "cloudwatch",
                region_name=region,
                config=Config(
                    connect_timeout=3,
                    read_timeout=10,
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        except Exception as e:
            logger.error(f"Failed to initialize CloudWatch client: {e}")
            raise

    def put_metric(
        self,
        metric_name: str,
        value: float,
        unit: str = "None",
        dimensions: dict[str, str] | None = None,
        timestamp: datetime | None = None,
    ) -> bool:
        """
        Put a single metric to CloudWatch

        Args:
            metric_name: Name of the metric
            value: Metric value
            unit: Metric unit (Count, Percent, Seconds, etc.)
            dimensions: Additional dimensions for the metric
            timestamp: Timestamp for the metric (defaults to now)

        Returns:
            True if successful, False otherwise
        """
        try:
            # Prepare dimensions
            metric_dimensions = [
                {"Name": "ClusterName", "Value": self.cluster_name},
                {"Name": "Region", "Value": self.region},
            ]

            if dimensions:
                for key, dim_value in dimensions.items():
                    metric_dimensions.append({"Name": key, "Value": dim_value})

            # Prepare metric data
            metric_data = {
                "MetricName": metric_name,
                "Value": value,
                "Unit": unit,
                "Dimensions": metric_dimensions,
            }

            if timestamp:
                metric_data["Timestamp"] = timestamp

            # Put metric to CloudWatch
            self.cloudwatch.put_metric_data(Namespace=self.namespace, MetricData=[metric_data])

            logger.debug(f"Published metric {metric_name}={value} to {self.namespace}")
            return True

        except ClientError as e:
            logger.error(f"Failed to put metric {metric_name}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error putting metric {metric_name}: {e}")
            return False

    def put_metrics_batch(self, metrics: list[dict[str, Any]]) -> bool:
        """
        Put multiple metrics to CloudWatch in a batch

        Args:
            metrics: List of metric dictionaries with keys: name, value, unit, dimensions, timestamp

        Returns:
            True if successful, False otherwise
        """
        try:
            metric_data = []

            for metric in metrics:
                # Prepare dimensions
                metric_dimensions = [
                    {"Name": "ClusterName", "Value": self.cluster_name},
                    {"Name": "Region", "Value": self.region},
                ]

                if metric.get("dimensions"):
                    for key, value in metric["dimensions"].items():
                        metric_dimensions.append({"Name": key, "Value": value})

                # Prepare metric data
                metric_item = {
                    "MetricName": metric["name"],
                    "Value": metric["value"],
                    "Unit": metric.get("unit", "None"),
                    "Dimensions": metric_dimensions,
                }

                if metric.get("timestamp"):
                    metric_item["Timestamp"] = metric["timestamp"]

                metric_data.append(metric_item)

            # CloudWatch allows max 20 metrics per batch
            batch_size = 20
            for i in range(0, len(metric_data), batch_size):
                batch = metric_data[i : i + batch_size]
                self.cloudwatch.put_metric_data(Namespace=self.namespace, MetricData=batch)

            logger.debug(f"Published {len(metrics)} metrics to {self.namespace}")
            return True

        except ClientError as e:
            logger.error(f"Failed to put metrics batch: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error putting metrics batch: {e}")
            return False


class HealthMonitorMetrics(MetricsPublisher):
    """
    Metrics publisher for Health Monitor service
    """

    def __init__(self, cluster_name: str, region: str):
        super().__init__("GCO/HealthMonitor", cluster_name, region)

    def publish_resource_utilization(
        self,
        cpu_percent: float,
        memory_percent: float,
        gpu_percent: float,
        active_jobs: int,
        gpu_allocatable: int | None = None,
        gpu_requested: int | None = None,
    ) -> bool:
        """
        Publish resource utilization metrics.

        ``ClusterGpuUtilization`` is 0 both without GPU nodes and with idle
        GPUs. The counts behind it, ``ClusterGpuAllocatable`` and
        ``ClusterGpuRequested``, tell the two apart and are published when
        both were counted.
        """
        metrics: list[dict[str, Any]] = [
            {"name": "ClusterCpuUtilization", "value": cpu_percent, "unit": "Percent"},
            {"name": "ClusterMemoryUtilization", "value": memory_percent, "unit": "Percent"},
            {"name": "ClusterGpuUtilization", "value": gpu_percent, "unit": "Percent"},
            {"name": "ActiveJobs", "value": active_jobs, "unit": "Count"},
        ]
        if gpu_allocatable is not None and gpu_requested is not None:
            metrics += [
                {"name": "ClusterGpuAllocatable", "value": gpu_allocatable, "unit": "Count"},
                {"name": "ClusterGpuRequested", "value": gpu_requested, "unit": "Count"},
            ]

        return self.put_metrics_batch(metrics)

    def publish_health_status(self, is_healthy: bool, threshold_violations: list[str]) -> bool:
        """
        Publish health status metrics
        """
        metrics = [
            {"name": "ClusterHealthy", "value": 1.0 if is_healthy else 0.0, "unit": "None"},
            {"name": "ThresholdViolations", "value": len(threshold_violations), "unit": "Count"},
        ]

        return self.put_metrics_batch(metrics)


class ManifestProcessorMetrics(MetricsPublisher):
    """
    Metrics publisher for Manifest Processor service
    """

    def __init__(self, cluster_name: str, region: str):
        super().__init__("GCO/ManifestProcessor", cluster_name, region)

    def publish_submission_metrics(
        self,
        total_submissions: int,
        successful_submissions: int,
        failed_submissions: int,
        validation_failures: int,
    ) -> bool:
        """
        Publish manifest submission metrics
        """
        metrics = [
            {"name": "ManifestSubmissions", "value": total_submissions, "unit": "Count"},
            {"name": "ManifestSuccesses", "value": successful_submissions, "unit": "Count"},
            {"name": "ManifestFailures", "value": failed_submissions, "unit": "Count"},
            {"name": "ValidationFailures", "value": validation_failures, "unit": "Count"},
        ]

        if total_submissions > 0:
            success_rate = (successful_submissions / total_submissions) * 100
            metrics.append(
                {"name": "ManifestSuccessRate", "value": success_rate, "unit": "Percent"}
            )

        return self.put_metrics_batch(metrics)

    def publish_resource_metrics(
        self, resources_created: int, resources_updated: int, resources_deleted: int
    ) -> bool:
        """
        Publish resource management metrics
        """
        metrics = [
            {"name": "ResourcesCreated", "value": resources_created, "unit": "Count"},
            {"name": "ResourcesUpdated", "value": resources_updated, "unit": "Count"},
            {"name": "ResourcesDeleted", "value": resources_deleted, "unit": "Count"},
        ]

        return self.put_metrics_batch(metrics)

    def publish_performance_metrics(self, avg_processing_time: float, queue_size: int) -> bool:
        """
        Publish performance metrics
        """
        metrics = [
            {"name": "AvgProcessingTime", "value": avg_processing_time, "unit": "Seconds"},
            {"name": "QueueSize", "value": queue_size, "unit": "Count"},
        ]

        return self.put_metrics_batch(metrics)


def create_health_monitor_metrics() -> HealthMonitorMetrics:
    """
    Create HealthMonitorMetrics instance from environment variables
    """
    cluster_name = os.getenv("CLUSTER_NAME", "unknown-cluster")
    region = os.getenv("REGION", "unknown-region")

    return HealthMonitorMetrics(cluster_name, region)


def create_manifest_processor_metrics() -> ManifestProcessorMetrics:
    """
    Create ManifestProcessorMetrics instance from environment variables
    """
    cluster_name = os.getenv("CLUSTER_NAME", "unknown-cluster")
    region = os.getenv("REGION", "unknown-region")

    return ManifestProcessorMetrics(cluster_name, region)
