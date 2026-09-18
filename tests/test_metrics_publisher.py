"""
Tests for gco/services/metrics_publisher.MetricsPublisher.

Covers initialization with namespace/cluster_name/region, put_metric
happy path (correct PutMetricData call shape, True return), dimension
merging so per-call dimensions land in the MetricData entry alongside
cluster/region defaults, graceful False return when CloudWatch raises
ClientError, and put_metrics_batch batching. Uses boto3.client
patching so no real CloudWatch calls are made.
"""

from unittest.mock import MagicMock, patch

import pytest


class TestMetricsPublisher:
    """Tests for MetricsPublisher class."""

    def test_metrics_publisher_initialization(self):
        """Test MetricsPublisher initialization."""
        from gco.services.metrics_publisher import MetricsPublisher

        with patch("boto3.client") as mock_client:
            mock_client.return_value = MagicMock()
            publisher = MetricsPublisher(
                namespace="Test/Namespace",
                cluster_name="test-cluster",
                region="us-east-1",
            )

            assert publisher.namespace == "Test/Namespace"
            assert publisher.cluster_name == "test-cluster"
            assert publisher.region == "us-east-1"

    def test_put_metric_success(self):
        """Test successful metric put."""
        from gco.services.metrics_publisher import MetricsPublisher

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_client.return_value = mock_cw

            publisher = MetricsPublisher(
                namespace="Test/Namespace",
                cluster_name="test-cluster",
                region="us-east-1",
            )

            result = publisher.put_metric("TestMetric", 42.0, "Count")

            assert result is True
            mock_cw.put_metric_data.assert_called_once()

    def test_put_metric_with_dimensions(self):
        """Test metric put with additional dimensions."""
        from gco.services.metrics_publisher import MetricsPublisher

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_client.return_value = mock_cw

            publisher = MetricsPublisher(
                namespace="Test/Namespace",
                cluster_name="test-cluster",
                region="us-east-1",
            )

            result = publisher.put_metric(
                "TestMetric",
                42.0,
                "Count",
                dimensions={"Environment": "test"},
            )

            assert result is True
            call_args = mock_cw.put_metric_data.call_args
            metric_data = call_args[1]["MetricData"][0]
            dimension_names = [d["Name"] for d in metric_data["Dimensions"]]
            assert "Environment" in dimension_names

    def test_put_metric_failure(self):
        """Test metric put failure handling."""
        from botocore.exceptions import ClientError

        from gco.services.metrics_publisher import MetricsPublisher

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_cw.put_metric_data.side_effect = ClientError(
                {"Error": {"Code": "500", "Message": "Error"}}, "PutMetricData"
            )
            mock_client.return_value = mock_cw

            publisher = MetricsPublisher(
                namespace="Test/Namespace",
                cluster_name="test-cluster",
                region="us-east-1",
            )

            result = publisher.put_metric("TestMetric", 42.0)
            assert result is False

    def test_put_metrics_batch_success(self):
        """Test successful batch metric put."""
        from gco.services.metrics_publisher import MetricsPublisher

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_client.return_value = mock_cw

            publisher = MetricsPublisher(
                namespace="Test/Namespace",
                cluster_name="test-cluster",
                region="us-east-1",
            )

            metrics = [
                {"name": "Metric1", "value": 10.0, "unit": "Count"},
                {"name": "Metric2", "value": 20.0, "unit": "Percent"},
            ]

            result = publisher.put_metrics_batch(metrics)

            assert result is True
            mock_cw.put_metric_data.assert_called_once()

    def test_put_metrics_batch_large(self):
        """Test batch metric put with more than 20 metrics."""
        from gco.services.metrics_publisher import MetricsPublisher

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_client.return_value = mock_cw

            publisher = MetricsPublisher(
                namespace="Test/Namespace",
                cluster_name="test-cluster",
                region="us-east-1",
            )

            # Create 25 metrics (should be split into 2 batches)
            metrics = [{"name": f"Metric{i}", "value": float(i)} for i in range(25)]

            result = publisher.put_metrics_batch(metrics)

            assert result is True
            # Should be called twice (20 + 5)
            assert mock_cw.put_metric_data.call_count == 2

    def test_put_metrics_batch_failure(self):
        """Test batch metric put failure handling."""
        from botocore.exceptions import ClientError

        from gco.services.metrics_publisher import MetricsPublisher

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_cw.put_metric_data.side_effect = ClientError(
                {"Error": {"Code": "500", "Message": "Error"}}, "PutMetricData"
            )
            mock_client.return_value = mock_cw

            publisher = MetricsPublisher(
                namespace="Test/Namespace",
                cluster_name="test-cluster",
                region="us-east-1",
            )

            metrics = [{"name": "Metric1", "value": 10.0}]
            result = publisher.put_metrics_batch(metrics)

            assert result is False


class TestHealthMonitorMetrics:
    """Tests for HealthMonitorMetrics class."""

    def test_health_monitor_metrics_initialization(self):
        """Test HealthMonitorMetrics initialization."""
        from gco.services.metrics_publisher import HealthMonitorMetrics

        with patch("boto3.client") as mock_client:
            mock_client.return_value = MagicMock()
            metrics = HealthMonitorMetrics(
                cluster_name="test-cluster",
                region="us-east-1",
            )

            assert metrics.namespace == "GCO/HealthMonitor"

    def test_publish_resource_utilization(self):
        """Test publishing resource utilization metrics."""
        from gco.services.metrics_publisher import HealthMonitorMetrics

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_client.return_value = mock_cw

            metrics = HealthMonitorMetrics(
                cluster_name="test-cluster",
                region="us-east-1",
            )

            result = metrics.publish_resource_utilization(
                cpu_percent=45.0,
                memory_percent=60.0,
                gpu_percent=30.0,
                active_jobs=5,
            )

            assert result is True
            mock_cw.put_metric_data.assert_called_once()

    def test_publish_health_status(self):
        """Test publishing health status metrics."""
        from gco.services.metrics_publisher import HealthMonitorMetrics

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_client.return_value = mock_cw

            metrics = HealthMonitorMetrics(
                cluster_name="test-cluster",
                region="us-east-1",
            )

            result = metrics.publish_health_status(
                is_healthy=True,
                threshold_violations=[],
            )

            assert result is True


class TestManifestProcessorMetrics:
    """Tests for ManifestProcessorMetrics class."""

    def test_manifest_processor_metrics_initialization(self):
        """Test ManifestProcessorMetrics initialization."""
        from gco.services.metrics_publisher import ManifestProcessorMetrics

        with patch("boto3.client") as mock_client:
            mock_client.return_value = MagicMock()
            metrics = ManifestProcessorMetrics(
                cluster_name="test-cluster",
                region="us-east-1",
            )

            assert metrics.namespace == "GCO/ManifestProcessor"

    def test_publish_submission_metrics(self):
        """Test publishing submission metrics."""
        from gco.services.metrics_publisher import ManifestProcessorMetrics

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_client.return_value = mock_cw

            metrics = ManifestProcessorMetrics(
                cluster_name="test-cluster",
                region="us-east-1",
            )

            result = metrics.publish_submission_metrics(
                total_submissions=10,
                successful_submissions=9,
                failed_submissions=1,
                validation_failures=0,
            )

            assert result is True

    def test_publish_submission_metrics_with_success_rate(self):
        """Test that success rate is calculated when total > 0."""
        from gco.services.metrics_publisher import ManifestProcessorMetrics

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_client.return_value = mock_cw

            metrics = ManifestProcessorMetrics(
                cluster_name="test-cluster",
                region="us-east-1",
            )

            result = metrics.publish_submission_metrics(
                total_submissions=10,
                successful_submissions=8,
                failed_submissions=2,
                validation_failures=1,
            )

            assert result is True
            # Verify success rate metric was included
            call_args = mock_cw.put_metric_data.call_args
            metric_names = [m["MetricName"] for m in call_args[1]["MetricData"]]
            assert "ManifestSuccessRate" in metric_names

    def test_publish_resource_metrics(self):
        """Test publishing resource metrics."""
        from gco.services.metrics_publisher import ManifestProcessorMetrics

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_client.return_value = mock_cw

            metrics = ManifestProcessorMetrics(
                cluster_name="test-cluster",
                region="us-east-1",
            )

            result = metrics.publish_resource_metrics(
                resources_created=5,
                resources_updated=3,
                resources_deleted=1,
            )

            assert result is True

    def test_publish_performance_metrics(self):
        """Test publishing performance metrics."""
        from gco.services.metrics_publisher import ManifestProcessorMetrics

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_client.return_value = mock_cw

            metrics = ManifestProcessorMetrics(
                cluster_name="test-cluster",
                region="us-east-1",
            )

            result = metrics.publish_performance_metrics(
                avg_processing_time=1.5,
                queue_size=10,
            )

            assert result is True


class TestFactoryFunctions:
    """Tests for factory functions."""

    def test_create_health_monitor_metrics(self):
        """Test create_health_monitor_metrics factory."""
        from gco.services.metrics_publisher import (
            HealthMonitorMetrics,
            create_health_monitor_metrics,
        )

        with patch("boto3.client") as mock_client:
            mock_client.return_value = MagicMock()

            with patch.dict(
                "os.environ",
                {"CLUSTER_NAME": "test-cluster", "REGION": "us-west-2"},
            ):
                metrics = create_health_monitor_metrics()

                assert isinstance(metrics, HealthMonitorMetrics)
                assert metrics.cluster_name == "test-cluster"
                assert metrics.region == "us-west-2"

    def test_create_manifest_processor_metrics(self):
        """Test create_manifest_processor_metrics factory."""
        from gco.services.metrics_publisher import (
            ManifestProcessorMetrics,
            create_manifest_processor_metrics,
        )

        with patch("boto3.client") as mock_client:
            mock_client.return_value = MagicMock()

            with patch.dict(
                "os.environ",
                {"CLUSTER_NAME": "prod-cluster", "REGION": "eu-west-1"},
            ):
                metrics = create_manifest_processor_metrics()

                assert isinstance(metrics, ManifestProcessorMetrics)
                assert metrics.cluster_name == "prod-cluster"
                assert metrics.region == "eu-west-1"

    def test_create_metrics_with_defaults(self):
        """Test factory functions use defaults when env vars not set."""
        from gco.services.metrics_publisher import create_health_monitor_metrics

        with patch("boto3.client") as mock_client:
            mock_client.return_value = MagicMock()

            with patch.dict("os.environ", {}, clear=True):
                metrics = create_health_monitor_metrics()

                assert metrics.cluster_name == "unknown-cluster"
                assert metrics.region == "unknown-region"


class TestMetricsPublisherEdgeCases:
    """Tests for edge cases in MetricsPublisher."""

    def test_initialization_failure(self):
        """Test MetricsPublisher initialization failure."""
        from gco.services.metrics_publisher import MetricsPublisher

        with patch("boto3.client") as mock_client:
            mock_client.side_effect = Exception("AWS credentials not found")

            with pytest.raises(Exception, match="AWS credentials not found"):
                MetricsPublisher(
                    namespace="Test/Namespace",
                    cluster_name="test-cluster",
                    region="us-east-1",
                )

    def test_put_metric_with_timestamp(self):
        """Test metric put with custom timestamp."""
        from datetime import UTC, datetime

        from gco.services.metrics_publisher import MetricsPublisher

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_client.return_value = mock_cw

            publisher = MetricsPublisher(
                namespace="Test/Namespace",
                cluster_name="test-cluster",
                region="us-east-1",
            )

            custom_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
            result = publisher.put_metric("TestMetric", 42.0, "Count", timestamp=custom_time)

            assert result is True
            call_args = mock_cw.put_metric_data.call_args
            metric_data = call_args[1]["MetricData"][0]
            assert metric_data["Timestamp"] == custom_time

    def test_put_metric_unexpected_error(self):
        """Test metric put with unexpected error."""
        from gco.services.metrics_publisher import MetricsPublisher

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_cw.put_metric_data.side_effect = RuntimeError("Unexpected error")
            mock_client.return_value = mock_cw

            publisher = MetricsPublisher(
                namespace="Test/Namespace",
                cluster_name="test-cluster",
                region="us-east-1",
            )

            result = publisher.put_metric("TestMetric", 42.0)
            assert result is False

    def test_put_metrics_batch_with_timestamp(self):
        """Test batch metric put with timestamps."""
        from datetime import UTC, datetime

        from gco.services.metrics_publisher import MetricsPublisher

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_client.return_value = mock_cw

            publisher = MetricsPublisher(
                namespace="Test/Namespace",
                cluster_name="test-cluster",
                region="us-east-1",
            )

            custom_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
            metrics = [
                {"name": "Metric1", "value": 10.0, "unit": "Count", "timestamp": custom_time},
                {"name": "Metric2", "value": 20.0, "dimensions": {"Env": "test"}},
            ]

            result = publisher.put_metrics_batch(metrics)

            assert result is True

    def test_put_metrics_batch_unexpected_error(self):
        """Test batch metric put with unexpected error."""
        from gco.services.metrics_publisher import MetricsPublisher

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_cw.put_metric_data.side_effect = RuntimeError("Unexpected error")
            mock_client.return_value = mock_cw

            publisher = MetricsPublisher(
                namespace="Test/Namespace",
                cluster_name="test-cluster",
                region="us-east-1",
            )

            metrics = [{"name": "Metric1", "value": 10.0}]
            result = publisher.put_metrics_batch(metrics)

            assert result is False

    def test_publish_submission_metrics_zero_total(self):
        """Test submission metrics with zero total (no success rate)."""
        from gco.services.metrics_publisher import ManifestProcessorMetrics

        with patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_client.return_value = mock_cw

            metrics = ManifestProcessorMetrics(
                cluster_name="test-cluster",
                region="us-east-1",
            )

            result = metrics.publish_submission_metrics(
                total_submissions=0,
                successful_submissions=0,
                failed_submissions=0,
                validation_failures=0,
            )

            assert result is True
            # Verify success rate metric was NOT included
            call_args = mock_cw.put_metric_data.call_args
            metric_names = [m["MetricName"] for m in call_args[1]["MetricData"]]
            assert "ManifestSuccessRate" not in metric_names
