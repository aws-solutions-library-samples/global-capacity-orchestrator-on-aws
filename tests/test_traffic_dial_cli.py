"""Tests for the traffic-dial CLI: manager logic and click commands.

``TrafficDialManager`` (cli/capacity/traffic_dial.py) is exercised with
mocked boto3 clients: endpoint-group discovery through the SSM registry,
status assembly from controller state plus overrides, manual dial
application (asserting the dial-only ``UpdateEndpointGroup`` shape), and
override lifecycle. The ``gco capacity traffic-dial`` commands are driven
through Click's ``CliRunner`` with the manager factory patched.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner

from cli.capacity.traffic_dial import (
    RegionDialStatus,
    TrafficDialError,
    TrafficDialManager,
)
from cli.config import GCOConfig
from cli.main import cli

EAST_ARN = "arn:aws:globalaccelerator::123:accelerator/a/listener/l/endpoint-group/east"
WEST_ARN = "arn:aws:globalaccelerator::123:accelerator/a/listener/l/endpoint-group/west"


def _client_error(code: str, operation: str = "Operation") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


@pytest.fixture
def manager():
    """A TrafficDialManager whose session yields per-service mocks."""
    config = GCOConfig()
    ssm = MagicMock()
    ga = MagicMock()
    with patch("cli.capacity.traffic_dial.boto3.Session") as session_cls:
        session_cls.return_value.client.side_effect = lambda service, region_name=None: (
            ssm if service == "ssm" else ga
        )
        yield TrafficDialManager(config), ssm, ga


def _registry_pages(ssm, *, groups=True, overrides=(), extra_page=False):
    """Wire get_parameters_by_path for the registry and dial trees."""
    registry = []
    if groups:
        registry = [
            {"Name": "/gco/endpoint-group-us-east-1-arn", "Value": EAST_ARN},
            {"Name": "/gco/alb-hostname-us-east-1", "Value": "ignored.example"},
        ]
    second_page = [{"Name": "/gco/endpoint-group-us-west-2-arn", "Value": WEST_ARN}]

    def by_path(Path, Recursive, NextToken=None):  # noqa: N803 - boto3 kwargs
        if Path == "/gco":
            if extra_page and NextToken is None:
                return {"Parameters": registry, "NextToken": "page2"}
            if extra_page and NextToken == "page2":
                return {"Parameters": second_page}
            return {"Parameters": registry + (second_page if groups else [])}
        assert Path == "/gco/traffic-dial/"
        return {
            "Parameters": [
                {"Name": f"/gco/traffic-dial/override-{region}", "Value": value}
                for region, value in overrides
            ]
        }

    ssm.get_parameters_by_path.side_effect = by_path


def _describe_groups(ga, dials, health_state="HEALTHY"):
    def describe(EndpointGroupArn):  # noqa: N803 - boto3 kwargs
        return {
            "EndpointGroup": {
                "TrafficDialPercentage": dials[EndpointGroupArn],
                "EndpointDescriptions": [{"HealthState": health_state}],
            }
        }

    ga.describe_endpoint_group.side_effect = describe


class TestDiscovery:
    def test_discovers_groups_across_pages_ignoring_other_parameters(self, manager):
        dial_manager, ssm, _ = manager
        _registry_pages(ssm, extra_page=True)

        assert dial_manager.discover_endpoint_groups() == {
            "us-east-1": EAST_ARN,
            "us-west-2": WEST_ARN,
        }

    def test_empty_registry_is_a_clear_error(self, manager):
        dial_manager, ssm, _ = manager
        ssm.get_parameters_by_path.side_effect = None
        ssm.get_parameters_by_path.return_value = {"Parameters": []}

        with pytest.raises(TrafficDialError, match="No Global Accelerator endpoint groups"):
            dial_manager.discover_endpoint_groups()

    def test_read_overrides_parses_only_override_parameters(self, manager):
        dial_manager, ssm, _ = manager
        _registry_pages(ssm, overrides=[("us-west-2", "20")])

        assert dial_manager.read_overrides() == {"us-west-2": "20"}


class TestControllerState:
    def test_reads_valid_state(self, manager):
        dial_manager, ssm, _ = manager
        ssm.get_parameter.return_value = {
            "Parameter": {"Value": json.dumps({"mode": "monitor", "decisions": []})}
        }
        assert dial_manager.read_controller_state() == {"mode": "monitor", "decisions": []}
        assert ssm.get_parameter.call_args.kwargs["Name"] == "/gco/traffic-dial/state"

    def test_absent_state_is_none(self, manager):
        dial_manager, ssm, _ = manager
        ssm.get_parameter.side_effect = _client_error("ParameterNotFound", "GetParameter")
        assert dial_manager.read_controller_state() is None

    @pytest.mark.parametrize("raw", ["not json", json.dumps([1, 2])])
    def test_invalid_state_payloads_are_none(self, manager, raw):
        dial_manager, ssm, _ = manager
        ssm.get_parameter.return_value = {"Parameter": {"Value": raw}}
        assert dial_manager.read_controller_state() is None

    def test_other_ssm_errors_propagate(self, manager):
        dial_manager, ssm, _ = manager
        ssm.get_parameter.side_effect = _client_error("AccessDeniedException", "GetParameter")
        with pytest.raises(ClientError):
            dial_manager.read_controller_state()


class TestGetStatus:
    def test_assembles_dial_health_override_and_decision(self, manager):
        dial_manager, ssm, ga = manager
        _registry_pages(ssm, overrides=[("us-west-2", "20")])
        ssm.get_parameter.return_value = {
            "Parameter": {
                "Value": json.dumps(
                    {
                        "decisions": [
                            {
                                "region": "us-east-1",
                                "reason": "degraded",
                                "healthy_percent": 42.5,
                            }
                        ]
                    }
                )
            }
        }
        _describe_groups(ga, {EAST_ARN: 80.0, WEST_ARN: 20.0})

        statuses = {status.region: status for status in dial_manager.get_status()}

        east = statuses["us-east-1"]
        assert east.traffic_dial == 80
        assert east.endpoint_health == "1/1 healthy"
        assert east.controller_reason == "degraded"
        assert east.healthy_percent == 42.5
        assert east.override is None
        west = statuses["us-west-2"]
        assert west.traffic_dial == 20
        assert west.override == "20"
        assert west.controller_reason is None

    def test_describe_failure_is_a_traffic_dial_error(self, manager):
        dial_manager, ssm, ga = manager
        _registry_pages(ssm)
        ssm.get_parameter.side_effect = _client_error("ParameterNotFound", "GetParameter")
        ga.describe_endpoint_group.side_effect = _client_error(
            "AccessDeniedException", "DescribeEndpointGroup"
        )

        with pytest.raises(TrafficDialError, match="Failed to describe"):
            dial_manager.get_status()

    def test_no_endpoints_summary(self, manager):
        dial_manager, _, _ = manager
        assert dial_manager._summarize_endpoint_health({}) == "no endpoints"
        assert (
            dial_manager._summarize_endpoint_health(
                {"EndpointDescriptions": [{"HealthState": "UNHEALTHY"}]}
            )
            == "0/1 healthy"
        )


class TestSetDial:
    @pytest.mark.parametrize("percentage", [True, "50", 3.5])
    def test_non_integer_percentages_are_rejected(self, manager, percentage):
        dial_manager, _, _ = manager
        with pytest.raises(TrafficDialError, match="must be an integer"):
            dial_manager.set_dial("us-east-1", percentage)

    @pytest.mark.parametrize("percentage", [-1, 101])
    def test_out_of_range_percentages_are_rejected(self, manager, percentage):
        dial_manager, _, _ = manager
        with pytest.raises(TrafficDialError, match="between 0 and 100"):
            dial_manager.set_dial("us-east-1", percentage)

    def test_unknown_region_lists_the_known_ones(self, manager):
        dial_manager, ssm, _ = manager
        _registry_pages(ssm)
        with pytest.raises(TrafficDialError, match="us-east-1, us-west-2"):
            dial_manager.set_dial("eu-central-1", 50)

    def test_applies_dial_only_update_and_records_override(self, manager):
        dial_manager, ssm, ga = manager
        _registry_pages(ssm)
        _describe_groups(ga, {EAST_ARN: 100.0, WEST_ARN: 100.0})
        ga.update_endpoint_group.return_value = {
            "EndpointGroup": {
                "TrafficDialPercentage": 25.0,
                "EndpointDescriptions": [{"HealthState": "HEALTHY"}],
            }
        }

        status = dial_manager.set_dial("us-west-2", 25)

        call = ga.update_endpoint_group.call_args
        # Dial-only update: EndpointConfigurations must be absent entirely.
        assert set(call.kwargs) == {"EndpointGroupArn", "TrafficDialPercentage"}
        assert call.kwargs == {
            "EndpointGroupArn": WEST_ARN,
            "TrafficDialPercentage": 25.0,
        }
        override_call = ssm.put_parameter.call_args
        assert override_call.kwargs["Name"] == "/gco/traffic-dial/override-us-west-2"
        assert override_call.kwargs["Value"] == "25"
        assert status.traffic_dial == 25
        assert status.override == "25"
        # The other region is still fully dialed, so no warning applies.
        assert status.warnings == []

    def test_warns_when_no_other_region_remains_fully_dialed(self, manager):
        dial_manager, ssm, ga = manager
        _registry_pages(ssm)
        _describe_groups(ga, {EAST_ARN: 80.0, WEST_ARN: 100.0})
        ga.update_endpoint_group.return_value = {"EndpointGroup": {}}

        status = dial_manager.set_dial("us-west-2", 50)

        assert len(status.warnings) == 1
        assert "no fully dialed region" in status.warnings[0]

    def test_full_dial_skips_the_spillover_check(self, manager):
        dial_manager, ssm, ga = manager
        _registry_pages(ssm)
        ga.update_endpoint_group.return_value = {"EndpointGroup": {}}

        status = dial_manager.set_dial("us-west-2", 100)

        ga.describe_endpoint_group.assert_not_called()
        assert status.warnings == []

    def test_update_failure_is_a_traffic_dial_error(self, manager):
        dial_manager, ssm, ga = manager
        _registry_pages(ssm)
        _describe_groups(ga, {EAST_ARN: 100.0, WEST_ARN: 100.0})
        ga.update_endpoint_group.side_effect = _client_error(
            "AccessDeniedException", "UpdateEndpointGroup"
        )

        with pytest.raises(TrafficDialError, match="Failed to update"):
            dial_manager.set_dial("us-west-2", 25)


class TestClearOverride:
    def test_returns_true_when_an_override_existed(self, manager):
        dial_manager, ssm, _ = manager
        assert dial_manager.clear_override("us-west-2") is True
        assert (
            ssm.delete_parameter.call_args.kwargs["Name"] == "/gco/traffic-dial/override-us-west-2"
        )

    def test_returns_false_when_no_override_existed(self, manager):
        dial_manager, ssm, _ = manager
        ssm.delete_parameter.side_effect = _client_error("ParameterNotFound", "DeleteParameter")
        assert dial_manager.clear_override("us-west-2") is False

    def test_other_errors_propagate(self, manager):
        dial_manager, ssm, _ = manager
        ssm.delete_parameter.side_effect = _client_error("AccessDeniedException", "DeleteParameter")
        with pytest.raises(ClientError):
            dial_manager.clear_override("us-west-2")


class TestPurgeRuntimeParameters:
    """Full-teardown purge of the runtime dial tree (state + overrides)."""

    def test_purges_across_pages_in_delete_batches_of_ten(self, manager):
        dial_manager, ssm, _ = manager
        first_page = [{"Name": f"/gco/traffic-dial/override-region-{i}"} for i in range(8)]
        second_page = [
            {"Name": "/gco/traffic-dial/override-region-8"},
            {"Name": "/gco/traffic-dial/override-region-9"},
            {"Name": "/gco/traffic-dial/override-region-10"},
            {"Name": "/gco/traffic-dial/state"},
        ]

        def by_path(Path, Recursive, NextToken=None):  # noqa: N803 - boto3 kwargs
            assert Path == "/gco/traffic-dial"
            assert Recursive is True
            if NextToken is None:
                return {"Parameters": first_page, "NextToken": "page2"}
            assert NextToken == "page2"
            return {"Parameters": second_page}

        ssm.get_parameters_by_path.side_effect = by_path
        ssm.delete_parameters.side_effect = lambda Names: {  # noqa: N803 - boto3 kwargs
            "DeletedParameters": Names
        }

        deleted = dial_manager.purge_runtime_parameters()

        batches = [call.kwargs["Names"] for call in ssm.delete_parameters.call_args_list]
        assert [len(batch) for batch in batches] == [10, 2]
        assert deleted == sorted(
            [param["Name"] for param in first_page] + [param["Name"] for param in second_page]
        )

    def test_empty_tree_deletes_nothing(self, manager):
        dial_manager, ssm, _ = manager
        ssm.get_parameters_by_path.side_effect = None
        ssm.get_parameters_by_path.return_value = {"Parameters": []}

        assert dial_manager.purge_runtime_parameters() == []
        ssm.delete_parameters.assert_not_called()

    def test_reports_only_what_ssm_confirmed_deleted(self, manager):
        dial_manager, ssm, _ = manager
        ssm.get_parameters_by_path.side_effect = None
        ssm.get_parameters_by_path.return_value = {
            "Parameters": [
                {"Name": "/gco/traffic-dial/state"},
                {"Name": "/gco/traffic-dial/override-us-east-1"},
            ]
        }
        ssm.delete_parameters.return_value = {
            "DeletedParameters": ["/gco/traffic-dial/state"],
            "InvalidParameters": ["/gco/traffic-dial/override-us-east-1"],
        }

        assert dial_manager.purge_runtime_parameters() == ["/gco/traffic-dial/state"]


class TestCommands:
    @pytest.fixture
    def mock_manager(self):
        with patch("cli.capacity.traffic_dial.get_traffic_dial_manager") as factory:
            yield factory.return_value

    def test_show_renders_status_and_controller_line(self, mock_manager):
        mock_manager.get_status.return_value = [
            RegionDialStatus(
                region="us-east-1",
                traffic_dial=80,
                endpoint_health="1/1 healthy",
                controller_reason="degraded",
                healthy_percent=42.5,
            )
        ]
        mock_manager.read_controller_state.return_value = {
            "mode": "monitor",
            "timestamp": "2026-08-19T00:00:00+00:00",
        }

        result = CliRunner().invoke(cli, ["capacity", "traffic-dial", "show"])

        assert result.exit_code == 0, result.output
        assert "monitor mode" in result.output
        assert "us-east-1" in result.output
        assert "degraded" in result.output

    def test_show_surfaces_traffic_dial_errors(self, mock_manager):
        mock_manager.get_status.side_effect = TrafficDialError("no endpoint groups")
        result = CliRunner().invoke(cli, ["capacity", "traffic-dial", "show"])
        assert result.exit_code == 1

    def test_show_surfaces_unexpected_errors(self, mock_manager):
        mock_manager.get_status.side_effect = RuntimeError("boom")
        result = CliRunner().invoke(cli, ["capacity", "traffic-dial", "show"])
        assert result.exit_code == 1

    def test_set_with_yes_applies_and_prints_warnings(self, mock_manager):
        mock_manager.set_dial.return_value = RegionDialStatus(
            region="us-west-2",
            traffic_dial=20,
            endpoint_health="1/1 healthy",
            override="20",
            warnings=["no fully dialed region remains"],
        )

        result = CliRunner().invoke(
            cli, ["capacity", "traffic-dial", "set", "us-west-2", "20", "--yes"]
        )

        assert result.exit_code == 0, result.output
        mock_manager.set_dial.assert_called_once_with("us-west-2", 20)
        assert "Dialed us-west-2 to 20%" in result.output
        assert "no fully dialed region remains" in result.output

    def test_set_without_confirmation_aborts(self, mock_manager):
        result = CliRunner().invoke(
            cli, ["capacity", "traffic-dial", "set", "us-west-2", "20"], input="n\n"
        )
        assert result.exit_code == 0
        assert "Aborted" in result.output
        mock_manager.set_dial.assert_not_called()

    def test_set_surfaces_traffic_dial_errors(self, mock_manager):
        mock_manager.set_dial.side_effect = TrafficDialError("unknown region")
        result = CliRunner().invoke(
            cli, ["capacity", "traffic-dial", "set", "nowhere-1", "20", "--yes"]
        )
        assert result.exit_code == 1

    def test_set_rejects_out_of_range_percentage_at_the_click_layer(self, mock_manager):
        result = CliRunner().invoke(
            cli, ["capacity", "traffic-dial", "set", "us-west-2", "150", "--yes"]
        )
        assert result.exit_code == 2
        mock_manager.set_dial.assert_not_called()

    def test_clear_reports_both_outcomes(self, mock_manager):
        mock_manager.clear_override.return_value = True
        cleared = CliRunner().invoke(cli, ["capacity", "traffic-dial", "clear", "us-west-2"])
        assert cleared.exit_code == 0
        assert "Cleared" in cleared.output

        mock_manager.clear_override.return_value = False
        absent = CliRunner().invoke(cli, ["capacity", "traffic-dial", "clear", "us-west-2"])
        assert absent.exit_code == 0
        assert "No traffic-dial override" in absent.output

    def test_clear_surfaces_errors(self, mock_manager):
        mock_manager.clear_override.side_effect = TrafficDialError("nope")
        result = CliRunner().invoke(cli, ["capacity", "traffic-dial", "clear", "us-west-2"])
        assert result.exit_code == 1
