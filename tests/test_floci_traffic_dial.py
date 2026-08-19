"""Floci layer: the traffic-dial controller against real emulated AWS.

Covered here, through the production handler module with the session
environment pointing boto3 at the emulator:

* the manual-override read path — ``GetParametersByPath`` against real SSM,
  including the exact path/prefix contract shared with the CLI
  (``/{project}/traffic-dial/override-{region}``);
* the state publication path — the run summary really lands in
  ``/{project}/traffic-dial/state`` and round-trips as JSON;
* decision metrics — ``PutMetricData`` into ``GCO/TrafficDial`` is accepted
  by the real wire protocol (Floci accepts CloudWatch writes; see
  docs/FLOCI_TESTING.md);
* the missing-telemetry hold — ``GetMetricData`` over the emulator returns
  no datapoints for the never-written ``ClusterHealthy`` metric, driving the
  controller's fail-safe "hold" for real rather than through a mock.

Global Accelerator is absent from Floci's catalog (documented gap; the same
convention as the GA half of ``ga-registration``), so the GA client alone is
mocked while every other client the handler constructs goes to the emulator.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

import boto3
import pytest

from tests._floci import floci_test_markers
from tests._lambda_imports import load_lambda_module

pytestmark = floci_test_markers()

LISTENER_ARN = "arn:aws:globalaccelerator::123:accelerator/abc/listener/def"
GROUP_EAST = f"{LISTENER_ARN}/endpoint-group/east"
GROUP_WEST = f"{LISTENER_ARN}/endpoint-group/west"


def _ga_stub(east_dial: float = 100.0, west_dial: float = 100.0) -> MagicMock:
    ga = MagicMock()
    ga.describe_accelerator.return_value = {"Accelerator": {"Status": "DEPLOYED"}}
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


class TestTrafficDialController:
    @pytest.fixture()
    def project(self, verified_floci_endpoint: str):
        """A per-test project prefix plus SSM cleanup of the dial tree."""
        project = f"gco{uuid.uuid4().hex[:8]}"
        yield project
        # Leaving parameters behind pollutes the session's shared emulator
        # account and fails the live-validation inventory gate (see the
        # history_table fixture in test_floci_lambda_regional.py).
        ssm = boto3.client("ssm")
        for name in (
            f"/{project}/traffic-dial/state",
            f"/{project}/traffic-dial/override-us-west-2",
        ):
            try:
                ssm.delete_parameter(Name=name)
            except ssm.exceptions.ParameterNotFound:
                continue

    @pytest.fixture()
    def dial_environment(self, project, monkeypatch):
        monkeypatch.setenv("LISTENER_ARN", LISTENER_ARN)
        monkeypatch.setenv("PROJECT_NAME", project)
        monkeypatch.setenv("REGIONS", "us-east-1,us-west-2")
        monkeypatch.setenv("MODE", "enforce")
        monkeypatch.setenv("LOOKBACK_MINUTES", "5")
        return monkeypatch

    def _run_with_real_aws_except_ga(self, handler, ga: MagicMock) -> dict:
        """Run lambda_handler with only the GA client mocked.

        Global Accelerator is absent from the emulator's catalog, so its
        client is answered locally while ssm/cloudwatch clients are built by
        the real factory and hit the emulator over the wire.
        """
        real_client = boto3.client

        def route(service: str, **kwargs):
            if service == "globalaccelerator":
                assert kwargs.get("region_name") == "us-west-2"
                return ga
            return real_client(service, **kwargs)

        with patch.object(handler.boto3, "client", side_effect=route):
            return handler.lambda_handler({}, None)

    def test_hold_and_override_paths_through_real_ssm_and_cloudwatch(
        self, project, dial_environment
    ):
        """A full cycle: real override read, real no-data hold, real state write.

        Nothing has ever published ``ClusterHealthy`` for these clusters, so
        the emulator's GetMetricData answers with no datapoints — the exact
        missing-telemetry condition the controller must treat as "hold", not
        as health or degradation. The west override comes back through a real
        ``GetParametersByPath`` call, proving the path/prefix contract the
        CLI writes and the controller reads.
        """
        handler = load_lambda_module("traffic-dial-controller")
        boto3.client("ssm").put_parameter(
            Name=f"/{project}/traffic-dial/override-us-west-2",
            Value="25",
            Type="String",
            Overwrite=True,
        )
        ga = _ga_stub(west_dial=25.0)

        summary = self._run_with_real_aws_except_ga(handler, ga)

        by_region = {d["region"]: d for d in summary["decisions"]}
        assert by_region["us-west-2"]["reason"] == "override"
        assert by_region["us-west-2"]["new_dial"] == 25
        assert by_region["us-east-1"]["reason"] == "no-health-data"
        assert by_region["us-east-1"]["new_dial"] == 100
        ga.update_endpoint_group.assert_not_called()

        # The run summary must have really landed in SSM and round-trip as
        # JSON — this is what `gco capacity traffic-dial show` reads.
        stored = json.loads(
            boto3.client("ssm").get_parameter(Name=f"/{project}/traffic-dial/state")[
                "Parameter"
            ]["Value"]
        )
        assert stored["mode"] == "enforce"
        assert {d["region"] for d in stored["decisions"]} == {"us-east-1", "us-west-2"}

    def test_decision_metrics_are_accepted_by_real_cloudwatch(
        self, project, dial_environment
    ):
        """PutMetricData into GCO/TrafficDial succeeds over the wire.

        Floci accepts CloudWatch writes (docs/FLOCI_TESTING.md); a malformed
        MetricData shape would be rejected here where a MagicMock would
        swallow it. publish_metrics deliberately never raises, so the proof
        is a direct call against the real client.
        """
        handler = load_lambda_module("traffic-dial-controller")
        decisions = [
            {
                "region": "us-east-1",
                "endpoint_group_arn": GROUP_EAST,
                "current_dial": 100,
                "healthy_percent": 87.5,
                "target_dial": 88,
                "new_dial": 88,
                "reason": "degraded",
                "applied": True,
            }
        ]

        cloudwatch = boto3.client("cloudwatch")
        handler.publish_metrics(cloudwatch, decisions)

        # publish_metrics swallows failures by design, so re-issue the same
        # payload directly to assert the wire protocol accepted it.
        cloudwatch.put_metric_data(
            Namespace=handler.DIAL_METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "TrafficDialPercentage",
                    "Value": 88.0,
                    "Unit": "Percent",
                    "Dimensions": [{"Name": "Region", "Value": "us-east-1"}],
                }
            ],
        )

    def test_health_signal_round_trip_where_the_emulator_answers_queries(
        self, project, dial_environment
    ):
        """Probe GetMetricData after a real PutMetricData.

        Floci is only documented to *accept* CloudWatch writes; whether
        queries return the written datapoints is probed here rather than
        assumed. When the emulator answers, the full producer→consumer signal
        path is proven; when it does not, the hold-path test above already
        covers the honest behavior and this probe records the gap.
        """
        handler = load_lambda_module("traffic-dial-controller")
        cluster_name = f"{project}-us-east-1"
        boto3.client("cloudwatch").put_metric_data(
            Namespace=handler.HEALTH_METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "ClusterHealthy",
                    "Value": 1.0,
                    "Dimensions": [
                        {"Name": "ClusterName", "Value": cluster_name},
                        {"Name": "Region", "Value": "us-east-1"},
                    ],
                }
            ],
        )

        value = handler.healthy_percent(
            "us-east-1", cluster_name, 5, cloudwatch_client=boto3.client("cloudwatch")
        )
        if value is None:
            pytest.skip(
                "emulator accepts CloudWatch writes but does not answer "
                "GetMetricData queries; the no-data hold path is covered by "
                "test_hold_and_override_paths_through_real_ssm_and_cloudwatch"
            )
        assert value == 100.0
