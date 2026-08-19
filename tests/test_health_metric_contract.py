"""Producer/consumer contract for the ClusterHealthy traffic-dial signal.

The health-monitor service publishes ``ClusterHealthy`` to CloudWatch
(``gco/services/metrics_publisher.py``), and the traffic-dial controller
(``lambda/traffic-dial-controller/handler.py``) queries it to decide each
region's dial. The two sides never import each other, so nothing structural
stops the namespace, metric name, or dimension set from drifting apart — and
GetMetricData matches a metric only on an *exact* dimension set, so any
drift silently degrades the controller into a permanent "no data" hold.

These tests capture the publisher's real ``PutMetricData`` payload and the
controller's real ``GetMetricData`` query and assert they agree, then pin
the cluster-name derivation both sides depend on (the health monitor's
``CLUSTER_NAME`` env is ``ConfigLoader.get_cluster_config``'s
``{project}-{region}``; the controller reconstructs the same string).
"""

from unittest.mock import MagicMock, patch

import pytest

from tests._lambda_imports import load_lambda_module

PROJECT = "gco"
REGION = "us-east-1"
CLUSTER = f"{PROJECT}-{REGION}"


@pytest.fixture
def dial_handler():
    with patch("boto3.client"), patch("boto3.Session"):
        yield load_lambda_module("traffic-dial-controller")


def _published_health_payload(is_healthy: bool) -> dict:
    """The exact PutMetricData kwargs the health monitor emits."""
    with patch("gco.services.metrics_publisher.boto3.client") as client_factory:
        cloudwatch = client_factory.return_value
        from gco.services.metrics_publisher import HealthMonitorMetrics

        publisher = HealthMonitorMetrics(cluster_name=CLUSTER, region=REGION)
        assert publisher.publish_health_status(is_healthy, []) is True
        return cloudwatch.put_metric_data.call_args.kwargs


@pytest.mark.parametrize(
    ("is_healthy", "expected_percent"), [(True, 100.0), (False, 0.0)]
)
def test_controller_query_matches_publisher_payload(
    dial_handler, is_healthy, expected_percent
):
    """The controller's query must select exactly what the monitor publishes.

    The published ClusterHealthy datapoint is fed back as the query result,
    proving namespace, metric name, the exact dimension set, and the value
    semantics (1.0/0.0 averaging to a 0-100 percent) in one pass.
    """
    published = _published_health_payload(is_healthy)
    cluster_healthy = next(
        metric
        for metric in published["MetricData"]
        if metric["MetricName"] == "ClusterHealthy"
    )

    cloudwatch = MagicMock()
    cloudwatch.get_metric_data.return_value = {
        "MetricDataResults": [{"Values": [cluster_healthy["Value"]]}]
    }
    value = dial_handler.healthy_percent(
        REGION, CLUSTER, 15, cloudwatch_client=cloudwatch
    )
    assert value == expected_percent

    queried = cloudwatch.get_metric_data.call_args.kwargs["MetricDataQueries"][0][
        "MetricStat"
    ]["Metric"]
    assert queried["Namespace"] == published["Namespace"]
    assert queried["MetricName"] == cluster_healthy["MetricName"]
    # GetMetricData matches only on the exact dimension set: name-value pairs
    # must agree precisely, with nothing extra on either side.
    assert {
        (dimension["Name"], dimension["Value"]) for dimension in queried["Dimensions"]
    } == {
        (dimension["Name"], dimension["Value"])
        for dimension in cluster_healthy["Dimensions"]
    }


def test_controller_namespace_constant_matches_publisher(dial_handler):
    """The handler's constant and the publisher subclass name one namespace."""
    published = _published_health_payload(True)
    assert published["Namespace"] == dial_handler.HEALTH_METRIC_NAMESPACE


def test_cluster_name_convention_matches_controller_derivation():
    """Both sides derive the same ClusterName dimension value.

    The health monitor's CLUSTER_NAME env comes from
    ``ConfigLoader.get_cluster_config`` at synth time; the controller
    reconstructs it as ``f"{project}-{region}"`` from its own env. If the
    naming convention ever changes, this pins the two derivations together.
    """
    import aws_cdk as cdk

    from gco.config.config_loader import ConfigLoader

    config = ConfigLoader(cdk.App())  # no context: validation skips, defaults apply
    cluster_config = config.get_cluster_config(REGION)
    assert cluster_config.cluster_name == f"{config.get_project_name()}-{REGION}"
    assert cluster_config.cluster_name == CLUSTER
