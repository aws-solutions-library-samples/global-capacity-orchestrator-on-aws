"""Tests for the Global Accelerator traffic-dial controller Lambda.

Covers the pure decision helpers (target mapping, step limiting, the
last-healthy-region guard), the AWS-facing helpers (endpoint-group listing,
override reading, health-signal aggregation), and full ``lambda_handler``
cycles across monitor/enforce modes, overrides, missing telemetry, a
mid-deployment accelerator, and update failures. The load-bearing invariant —
``UpdateEndpointGroup`` carries *only* the dial, never
``EndpointConfigurations`` — is asserted on every enforcement path.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from tests._lambda_imports import load_lambda_module

LISTENER_ARN = "arn:aws:globalaccelerator::123:accelerator/abc/listener/def"
ACCELERATOR_ARN = "arn:aws:globalaccelerator::123:accelerator/abc"
GROUP_EAST = f"{LISTENER_ARN}/endpoint-group/east"
GROUP_WEST = f"{LISTENER_ARN}/endpoint-group/west"


@pytest.fixture
def dial_module():
    """Load traffic-dial-controller with AWS constructors isolated."""
    with patch("boto3.client") as mock_boto_client, patch("boto3.Session"):
        handler = load_lambda_module("traffic-dial-controller")
        yield handler, mock_boto_client


@pytest.fixture
def dial_env(monkeypatch):
    """Baseline controller environment for two regions in enforce mode."""
    monkeypatch.setenv("LISTENER_ARN", LISTENER_ARN)
    monkeypatch.setenv("PROJECT_NAME", "gco")
    monkeypatch.setenv("REGIONS", "us-east-1,us-west-2")
    monkeypatch.setenv("MODE", "enforce")
    monkeypatch.setenv("LOOKBACK_MINUTES", "15")
    monkeypatch.setenv("MIN_DIAL_PERCENTAGE", "10")
    monkeypatch.setenv("MAX_STEP_PERCENTAGE", "20")
    monkeypatch.setenv("FULL_HEALTH_PERCENTAGE", "95")
    return monkeypatch


def _decision(region, *, new_dial, healthy=None, reason="degraded", current=100):
    return {
        "region": region,
        "endpoint_group_arn": f"{LISTENER_ARN}/endpoint-group/{region}",
        "current_dial": current,
        "healthy_percent": healthy,
        "target_dial": new_dial,
        "new_dial": new_dial,
        "reason": reason,
        "applied": False,
    }


def _route_clients(mock_boto_client, *, ga, ssm, cloudwatch, regional_cloudwatch=None):
    """Route boto3.client(service) calls to per-service mocks."""
    regional = regional_cloudwatch or {}

    def route(service, **kwargs):
        if service == "globalaccelerator":
            assert kwargs.get("region_name") == "us-west-2"
            return ga
        if service == "ssm":
            return ssm
        assert service == "cloudwatch"
        return regional.get(kwargs.get("region_name"), cloudwatch)

    mock_boto_client.side_effect = route


def _ga_stub(east_dial=100.0, west_dial=100.0, status="DEPLOYED"):
    ga = MagicMock()
    ga.describe_accelerator.return_value = {"Accelerator": {"Status": status}}
    ga.list_endpoint_groups.return_value = {
        "EndpointGroups": [
            {
                "EndpointGroupArn": GROUP_EAST,
                "EndpointGroupRegion": "us-east-1",
                "TrafficDialPercentage": east_dial,
            },
            {
                "EndpointGroupArn": GROUP_WEST,
                "EndpointGroupRegion": "us-west-2",
                "TrafficDialPercentage": west_dial,
            },
        ]
    }
    return ga


def _cloudwatch_stub(values):
    cloudwatch = MagicMock()
    cloudwatch.get_metric_data.return_value = {"MetricDataResults": [{"Values": values}]}
    return cloudwatch


def _ssm_stub(parameters=None):
    ssm = MagicMock()
    ssm.get_parameters_by_path.return_value = {"Parameters": parameters or []}
    return ssm


class TestDecisionMath:
    def test_full_health_maps_to_one_hundred(self, dial_module):
        handler, _ = dial_module
        assert handler.target_dial(100.0, 10, 95) == 100
        assert handler.target_dial(95.0, 10, 95) == 100

    def test_degraded_health_maps_linearly_with_floor(self, dial_module):
        handler, _ = dial_module
        assert handler.target_dial(50.0, 10, 95) == 50
        assert handler.target_dial(3.0, 10, 95) == 10
        assert handler.target_dial(0.0, 10, 95) == 10
        assert handler.target_dial(0.0, 0, 95) == 0

    def test_step_limit_bounds_both_directions(self, dial_module):
        handler, _ = dial_module
        assert handler.step_limit(100, 10, 20) == 80
        assert handler.step_limit(80, 10, 20) == 60
        assert handler.step_limit(30, 100, 20) == 50
        assert handler.step_limit(90, 100, 20) == 100
        assert handler.step_limit(50, 50, 20) == 50


class TestLastHealthyRegionGuard:
    def test_forces_best_region_to_full_dial(self, dial_module):
        handler, _ = dial_module
        decisions = [
            _decision("us-east-1", new_dial=80, healthy=40.0),
            _decision("us-west-2", new_dial=60, healthy=20.0),
        ]
        assert handler.apply_last_healthy_region_guard(decisions) == "us-east-1"
        assert decisions[0]["new_dial"] == 100
        assert decisions[0]["reason"] == "guard-last-healthy-region"
        assert decisions[1]["new_dial"] == 60

    def test_abstains_when_any_region_is_fully_dialed(self, dial_module):
        handler, _ = dial_module
        decisions = [
            _decision("us-east-1", new_dial=100, healthy=99.0, reason="healthy"),
            _decision("us-west-2", new_dial=60, healthy=20.0),
        ]
        assert handler.apply_last_healthy_region_guard(decisions) is None

    def test_abstains_when_an_override_holds_full_dial(self, dial_module):
        handler, _ = dial_module
        decisions = [
            _decision("us-east-1", new_dial=100, reason="override", current=100),
            _decision("us-west-2", new_dial=60, healthy=20.0),
        ]
        assert handler.apply_last_healthy_region_guard(decisions) is None
        assert decisions[1]["new_dial"] == 60

    def test_abstains_when_everything_is_overridden(self, dial_module):
        handler, _ = dial_module
        decisions = [
            _decision("us-east-1", new_dial=50, reason="override", current=50),
            _decision("us-west-2", new_dial=60, reason="override", current=60),
        ]
        assert handler.apply_last_healthy_region_guard(decisions) is None

    def test_missing_health_ranks_below_any_signal(self, dial_module):
        handler, _ = dial_module
        decisions = [
            _decision("us-east-1", new_dial=90, healthy=None, reason="no-health-data"),
            _decision("us-west-2", new_dial=60, healthy=5.0),
        ]
        assert handler.apply_last_healthy_region_guard(decisions) == "us-west-2"


class TestAwsHelpers:
    def test_list_endpoint_groups_paginates_and_rounds(self, dial_module):
        handler, _ = dial_module
        ga = MagicMock()
        ga.list_endpoint_groups.side_effect = [
            {
                "EndpointGroups": [
                    {
                        "EndpointGroupArn": GROUP_EAST,
                        "EndpointGroupRegion": "us-east-1",
                        "TrafficDialPercentage": 75.0,
                    }
                ],
                "NextToken": "page2",
            },
            {
                "EndpointGroups": [
                    {
                        "EndpointGroupArn": GROUP_WEST,
                        "EndpointGroupRegion": "us-west-2",
                    }
                ]
            },
        ]

        groups = handler.list_endpoint_groups(ga, LISTENER_ARN)

        assert groups == {
            "us-east-1": {"arn": GROUP_EAST, "traffic_dial": 75},
            "us-west-2": {"arn": GROUP_WEST, "traffic_dial": 100},
        }
        assert ga.list_endpoint_groups.call_args_list[1].kwargs["NextToken"] == "page2"

    def test_read_overrides_parses_only_override_parameters(self, dial_module):
        handler, _ = dial_module
        ssm = _ssm_stub(
            [
                {"Name": "/gco/traffic-dial/state", "Value": "{}"},
                {"Name": "/gco/traffic-dial/override-us-west-2", "Value": "20"},
            ]
        )

        assert handler.read_overrides(ssm, "gco") == {"us-west-2": "20"}
        assert ssm.get_parameters_by_path.call_args.kwargs["Path"] == "/gco/traffic-dial/"

    def test_healthy_percent_averages_the_window(self, dial_module):
        handler, _ = dial_module
        cloudwatch = _cloudwatch_stub([1.0, 1.0, 0.5, 0.5])

        value = handler.healthy_percent(
            "us-east-1", "gco-us-east-1", 15, cloudwatch_client=cloudwatch
        )

        assert value == 75.0
        query = cloudwatch.get_metric_data.call_args.kwargs["MetricDataQueries"][0]
        metric = query["MetricStat"]["Metric"]
        assert metric["Namespace"] == "GCO/HealthMonitor"
        assert metric["MetricName"] == "ClusterHealthy"
        assert {"Name": "ClusterName", "Value": "gco-us-east-1"} in metric["Dimensions"]
        assert {"Name": "Region", "Value": "us-east-1"} in metric["Dimensions"]

    def test_healthy_percent_returns_none_without_datapoints(self, dial_module):
        handler, _ = dial_module
        assert (
            handler.healthy_percent(
                "us-east-1", "gco-us-east-1", 15, cloudwatch_client=_cloudwatch_stub([])
            )
            is None
        )

    def test_healthy_percent_returns_none_on_error(self, dial_module):
        handler, _ = dial_module
        cloudwatch = MagicMock()
        cloudwatch.get_metric_data.side_effect = ClientError(
            {"Error": {"Code": "Throttling", "Message": "slow down"}}, "GetMetricData"
        )
        assert (
            handler.healthy_percent(
                "us-east-1", "gco-us-east-1", 15, cloudwatch_client=cloudwatch
            )
            is None
        )


class TestLambdaHandler:
    def test_missing_required_environment_raises(self, dial_module, monkeypatch):
        handler, _ = dial_module
        monkeypatch.delenv("LISTENER_ARN", raising=False)
        monkeypatch.delenv("PROJECT_NAME", raising=False)
        with pytest.raises(ValueError, match="LISTENER_ARN and PROJECT_NAME"):
            handler.lambda_handler({}, MagicMock())

    def test_skips_cycle_while_accelerator_deploys(self, dial_module, dial_env):
        handler, mock_boto_client = dial_module
        ga = _ga_stub(status="IN_PROGRESS")
        _route_clients(mock_boto_client, ga=ga, ssm=_ssm_stub(), cloudwatch=MagicMock())

        summary = handler.lambda_handler({}, MagicMock())

        assert summary["skipped"] == "accelerator-not-deployed"
        ga.list_endpoint_groups.assert_not_called()
        ga.update_endpoint_group.assert_not_called()

    def test_enforce_dials_degraded_region_with_dial_only_update(
        self, dial_module, dial_env
    ):
        handler, mock_boto_client = dial_module
        ga = _ga_stub()
        ssm = _ssm_stub()
        east_cloudwatch = _cloudwatch_stub([1.0] * 15)
        west_cloudwatch = _cloudwatch_stub([0.0] * 15)
        publish_cloudwatch = MagicMock()
        _route_clients(
            mock_boto_client,
            ga=ga,
            ssm=ssm,
            cloudwatch=publish_cloudwatch,
            regional_cloudwatch={
                "us-east-1": east_cloudwatch,
                "us-west-2": west_cloudwatch,
            },
        )

        summary = handler.lambda_handler({}, MagicMock())

        assert summary["updates_applied"] == 1
        assert summary["errors"] == 0
        ga.update_endpoint_group.assert_called_once()
        call = ga.update_endpoint_group.call_args
        # The load-bearing invariant: the dial-only update must never carry
        # EndpointConfigurations (an empty list would detach the ALB).
        assert set(call.kwargs) == {"EndpointGroupArn", "TrafficDialPercentage"}
        assert call.kwargs["EndpointGroupArn"] == GROUP_WEST
        assert call.kwargs["TrafficDialPercentage"] == 80.0  # 100 - max_step 20

        by_region = {d["region"]: d for d in summary["decisions"]}
        assert by_region["us-east-1"]["reason"] == "healthy"
        assert by_region["us-east-1"]["applied"] is False
        assert by_region["us-west-2"]["reason"] == "degraded"
        assert by_region["us-west-2"]["applied"] is True

        state_call = ssm.put_parameter.call_args
        assert state_call.kwargs["Name"] == "/gco/traffic-dial/state"
        stored = json.loads(state_call.kwargs["Value"])
        assert stored["updates_applied"] == 1
        publish_call = publish_cloudwatch.put_metric_data.call_args
        assert publish_call.kwargs["Namespace"] == "GCO/TrafficDial"

    def test_monitor_mode_never_writes(self, dial_module, dial_env):
        handler, mock_boto_client = dial_module
        dial_env.setenv("MODE", "monitor")
        ga = _ga_stub()
        ssm = _ssm_stub()
        _route_clients(
            mock_boto_client,
            ga=ga,
            ssm=ssm,
            cloudwatch=MagicMock(),
            regional_cloudwatch={
                "us-east-1": _cloudwatch_stub([1.0]),
                "us-west-2": _cloudwatch_stub([0.0]),
            },
        )

        summary = handler.lambda_handler({}, MagicMock())

        ga.update_endpoint_group.assert_not_called()
        assert summary["updates_applied"] == 0
        by_region = {d["region"]: d for d in summary["decisions"]}
        # The decision is still computed and published for observability.
        assert by_region["us-west-2"]["new_dial"] == 80
        assert by_region["us-west-2"]["applied"] is False
        ssm.put_parameter.assert_called_once()

    def test_override_region_is_left_alone(self, dial_module, dial_env):
        handler, mock_boto_client = dial_module
        ga = _ga_stub(west_dial=25.0)
        ssm = _ssm_stub(
            [{"Name": "/gco/traffic-dial/override-us-west-2", "Value": "25"}]
        )
        west_cloudwatch = MagicMock()
        _route_clients(
            mock_boto_client,
            ga=ga,
            ssm=ssm,
            cloudwatch=MagicMock(),
            regional_cloudwatch={
                "us-east-1": _cloudwatch_stub([1.0]),
                "us-west-2": west_cloudwatch,
            },
        )

        summary = handler.lambda_handler({}, MagicMock())

        by_region = {d["region"]: d for d in summary["decisions"]}
        assert by_region["us-west-2"]["reason"] == "override"
        assert by_region["us-west-2"]["new_dial"] == 25
        # An overridden region's health is not even queried.
        west_cloudwatch.get_metric_data.assert_not_called()
        ga.update_endpoint_group.assert_not_called()

    def test_missing_telemetry_holds_the_current_dial(self, dial_module, dial_env):
        handler, mock_boto_client = dial_module
        ga = _ga_stub(west_dial=70.0)
        _route_clients(
            mock_boto_client,
            ga=ga,
            ssm=_ssm_stub(),
            cloudwatch=MagicMock(),
            regional_cloudwatch={
                "us-east-1": _cloudwatch_stub([1.0]),
                "us-west-2": _cloudwatch_stub([]),
            },
        )

        summary = handler.lambda_handler({}, MagicMock())

        by_region = {d["region"]: d for d in summary["decisions"]}
        assert by_region["us-west-2"]["reason"] == "no-health-data"
        assert by_region["us-west-2"]["new_dial"] == 70
        ga.update_endpoint_group.assert_not_called()

    def test_region_without_endpoint_group_is_reported(self, dial_module, dial_env):
        handler, mock_boto_client = dial_module
        ga = _ga_stub()
        ga.list_endpoint_groups.return_value = {
            "EndpointGroups": [
                {
                    "EndpointGroupArn": GROUP_EAST,
                    "EndpointGroupRegion": "us-east-1",
                    "TrafficDialPercentage": 100.0,
                }
            ]
        }
        _route_clients(
            mock_boto_client,
            ga=ga,
            ssm=_ssm_stub(),
            cloudwatch=MagicMock(),
            regional_cloudwatch={"us-east-1": _cloudwatch_stub([1.0])},
        )

        summary = handler.lambda_handler({}, MagicMock())

        by_region = {d["region"]: d for d in summary["decisions"]}
        assert by_region["us-west-2"]["reason"] == "no-endpoint-group"
        assert by_region["us-west-2"]["new_dial"] is None
        ga.update_endpoint_group.assert_not_called()

    def test_update_failure_is_counted_not_raised(self, dial_module, dial_env):
        handler, mock_boto_client = dial_module
        ga = _ga_stub()
        ga.update_endpoint_group.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "UpdateEndpointGroup",
        )
        _route_clients(
            mock_boto_client,
            ga=ga,
            ssm=_ssm_stub(),
            cloudwatch=MagicMock(),
            regional_cloudwatch={
                "us-east-1": _cloudwatch_stub([1.0]),
                "us-west-2": _cloudwatch_stub([0.0]),
            },
        )

        summary = handler.lambda_handler({}, MagicMock())

        assert summary["errors"] == 1
        assert summary["updates_applied"] == 0
        by_region = {d["region"]: d for d in summary["decisions"]}
        assert by_region["us-west-2"]["applied"] is False
        assert "AccessDenied" in by_region["us-west-2"]["error"]

    def test_all_regions_degraded_keeps_one_fully_dialed(self, dial_module, dial_env):
        handler, mock_boto_client = dial_module
        ga = _ga_stub()
        _route_clients(
            mock_boto_client,
            ga=ga,
            ssm=_ssm_stub(),
            cloudwatch=MagicMock(),
            regional_cloudwatch={
                "us-east-1": _cloudwatch_stub([0.5]),
                "us-west-2": _cloudwatch_stub([0.0]),
            },
        )

        summary = handler.lambda_handler({}, MagicMock())

        by_region = {d["region"]: d for d in summary["decisions"]}
        # The healthier region (50%) is guarded back to 100; the other drains.
        assert by_region["us-east-1"]["reason"] == "guard-last-healthy-region"
        assert by_region["us-east-1"]["new_dial"] == 100
        assert by_region["us-west-2"]["new_dial"] == 80
        ga.update_endpoint_group.assert_called_once()
        assert (
            ga.update_endpoint_group.call_args.kwargs["EndpointGroupArn"] == GROUP_WEST
        )
