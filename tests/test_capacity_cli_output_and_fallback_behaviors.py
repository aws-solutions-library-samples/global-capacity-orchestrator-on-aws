"""Capacity CLI output-mode, fallback, and error-boundary tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from click.testing import CliRunner

from cli.capacity.advisor import CapacityPredictionResult
from cli.capacity.traffic_dial import RegionDialStatus
from cli.commands.capacity_cmd import (
    _format_patterns_grid,
    _print_find_blocks_report,
    _print_find_reservations_report,
    _print_historical_enrichment,
    _print_prediction,
    capacity,
)
from cli.config import GCOConfig
from gco.bedrock import BEDROCK_FTU_FORM_ERROR_CODE, BEDROCK_FTU_REMEDIATION


def _ftu_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": BEDROCK_FTU_FORM_ERROR_CODE, "Message": "form required"}},
        "Converse",
    )


def _config(output_format: str = "table") -> GCOConfig:
    return GCOConfig(
        project_name="test-gco",
        default_region="us-east-1",
        output_format=output_format,
    )


def test_ai_recommend_renders_first_time_use_remediation() -> None:
    advisor = MagicMock()
    advisor.get_recommendation.side_effect = _ftu_error()
    formatter = MagicMock()
    with (
        patch("cli.capacity.get_bedrock_capacity_advisor", return_value=advisor),
        patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
    ):
        result = CliRunner().invoke(capacity, ["ai-recommend"], obj=_config())

    assert result.exit_code == 1
    formatter.print_error.assert_called_once_with(BEDROCK_FTU_REMEDIATION)


def test_reservation_check_machine_output_forwards_the_complete_report() -> None:
    report = {"odcr": {}, "capacity_blocks": {}, "recommendation": "use spot"}
    checker = MagicMock()
    checker.check_reservation_availability.return_value = report
    formatter = MagicMock()
    with (
        patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker),
        patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
    ):
        result = CliRunner().invoke(
            capacity,
            ["reservation-check", "--instance-type", "g5.xlarge"],
            obj=_config("json"),
        )

    assert result.exit_code == 0, result.output
    formatter.print.assert_called_once_with(report)


def test_find_blocks_report_handles_canonical_name_and_empty_duration_set(capsys) -> None:
    _print_find_blocks_report(
        {
            "instance_type": "p5.48xlarge",
            "requested_instance_type": "p5.48xlarge",
            "regions_checked": ["us-east-1"],
            "durations_probed_days": [],
            "date_window": {},
            "offerings": [],
            "recommendation": "No offerings found.",
        }
    )

    output = capsys.readouterr().out
    assert "Capacity Block search for p5.48xlarge" in output
    assert "->" not in output
    assert "Durations probed: n/a" in output


def test_reserve_machine_output_forwards_the_checker_payload() -> None:
    payload = {"success": True, "dry_run": True, "offering_id": "cbo-1"}
    checker = MagicMock()
    checker.purchase_capacity_block.return_value = payload
    formatter = MagicMock()
    with (
        patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker),
        patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
    ):
        result = CliRunner().invoke(
            capacity,
            ["reserve", "--offering-id", "cbo-1", "--region", "us-east-1", "--dry-run"],
            obj=_config("json"),
        )

    assert result.exit_code == 0, result.output
    formatter.print.assert_called_once_with(payload)


def test_find_reservations_report_renders_alias_and_invalid_type_warning(capsys) -> None:
    _print_find_reservations_report(
        {
            "instance_type": "p6-b200.48xlarge",
            "requested_instance_type": "p6-b200",
            "valid_instance_type": False,
            "regions_checked": ["us-east-1"],
            "recommendation": "not a standalone instance type",
        }
    )

    output = capsys.readouterr().out
    assert "p6-b200 -> p6-b200.48xlarge" in output
    assert "not a standalone instance type" in output


def test_find_reservations_machine_output_forwards_the_report() -> None:
    report = {"instance_type": None, "reservations": [], "regions_checked": []}
    checker = MagicMock()
    checker.find_capacity_reservations.return_value = report
    formatter = MagicMock()
    with (
        patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker),
        patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
    ):
        result = CliRunner().invoke(
            capacity, ["find-reservations", "--state", "all"], obj=_config("json")
        )

    assert result.exit_code == 0, result.output
    assert checker.find_capacity_reservations.call_args.kwargs["state"] is None
    formatter.print.assert_called_once_with(report)


def test_create_reservation_machine_output_and_unexpected_failure_paths() -> None:
    payload = {"success": True, "dry_run": True}
    checker = MagicMock()
    checker.create_capacity_reservation.return_value = payload
    formatter = MagicMock()
    arguments = [
        "create-reservation",
        "--instance-type",
        "p5.48xlarge",
        "--region",
        "us-east-1",
        "--availability-zone",
        "us-east-1a",
        "--dry-run",
    ]
    with (
        patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker),
        patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
    ):
        success = CliRunner().invoke(capacity, arguments, obj=_config("json"))
    assert success.exit_code == 0, success.output
    formatter.print.assert_called_once_with(payload)

    checker.create_capacity_reservation.side_effect = RuntimeError("unexpected create failure")
    formatter.reset_mock()
    with (
        patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker),
        patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
    ):
        failed = CliRunner().invoke(capacity, arguments, obj=_config())
    assert failed.exit_code == 1
    assert "unexpected create failure" in formatter.print_error.call_args.args[0]


def test_cancel_reservation_machine_output_and_unexpected_failure_paths() -> None:
    payload = {"success": True, "dry_run": True}
    checker = MagicMock()
    checker.cancel_capacity_reservation.return_value = payload
    formatter = MagicMock()
    arguments = [
        "cancel-reservation",
        "--reservation-id",
        "cr-1",
        "--region",
        "us-east-1",
        "--dry-run",
    ]
    with (
        patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker),
        patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
    ):
        success = CliRunner().invoke(capacity, arguments, obj=_config("json"))
    assert success.exit_code == 0, success.output
    formatter.print.assert_called_once_with(payload)

    checker.cancel_capacity_reservation.side_effect = RuntimeError("unexpected cancel failure")
    formatter.reset_mock()
    with (
        patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker),
        patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
    ):
        failed = CliRunner().invoke(capacity, arguments, obj=_config())
    assert failed.exit_code == 1
    assert "unexpected cancel failure" in formatter.print_error.call_args.args[0]


def test_historical_enrichment_handles_generic_error_and_zero_samples() -> None:
    formatter = MagicMock()
    store = MagicMock()
    store.get_statistics.side_effect = RuntimeError("DynamoDB throttled")
    with patch("cli.capacity.history.get_capacity_history_store", return_value=store):
        _print_historical_enrichment(formatter, "g5.xlarge", "us-east-1")
    assert "Historical enrichment unavailable" in formatter.print_warning.call_args.args[0]

    formatter.reset_mock()
    store.get_statistics.side_effect = None
    store.get_statistics.return_value = {"sample_count": 0, "metrics": {}}
    with patch("cli.capacity.history.get_capacity_history_store", return_value=store):
        _print_historical_enrichment(formatter, "g5.xlarge", "us-east-1")
    assert "No historical samples" in formatter.print_warning.call_args.args[0]


def test_historical_enrichment_renders_price_without_spot_score(capsys) -> None:
    formatter = MagicMock()
    store = MagicMock()
    store.get_statistics.return_value = {
        "sample_count": 3,
        "metrics": {
            "spot_price": {"p25": 1.0, "p50": 1.1, "p75": 1.2},
        },
    }
    with patch("cli.capacity.history.get_capacity_history_store", return_value=store):
        _print_historical_enrichment(formatter, "g5.xlarge", "us-east-1")

    assert "spot_price p25/p50/p75: 1.0/1.1/1.2" in capsys.readouterr().out


def test_patterns_grid_without_best_windows_has_no_summary_section() -> None:
    rendered = _format_patterns_grid(
        {"metric": "spot_score", "patterns": {"Monday": {0: {"avg": 5.0}}}}
    )
    assert "Monday" in rendered
    assert "Best windows:" not in rendered


def test_history_show_generic_failure_is_an_error() -> None:
    store = MagicMock()
    store.get_trend.side_effect = RuntimeError("query failed")
    formatter = MagicMock()
    with (
        patch("cli.capacity.history.get_capacity_history_store", return_value=store),
        patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
    ):
        result = CliRunner().invoke(
            capacity,
            ["history", "show", "-i", "g5.xlarge", "-r", "us-east-1"],
            obj=_config(),
        )

    assert result.exit_code == 1
    assert "query failed" in formatter.print_error.call_args.args[0]


def test_history_stats_machine_output_forwards_statistics() -> None:
    stats = {"sample_count": 2, "metrics": {"spot_score": {"mean": 7.0}}}
    store = MagicMock()
    store.get_statistics.return_value = stats
    formatter = MagicMock()
    with (
        patch("cli.capacity.history.get_capacity_history_store", return_value=store),
        patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
    ):
        result = CliRunner().invoke(
            capacity,
            ["history", "stats", "-i", "g5.xlarge", "-r", "us-east-1"],
            obj=_config("json"),
        )

    assert result.exit_code == 0, result.output
    formatter.print.assert_called_once_with(stats)


def test_prediction_renderer_prints_each_reasoning_sentence(capsys) -> None:
    prediction = CapacityPredictionResult(
        instance_type="g5.xlarge",
        region="us-east-1",
        best_windows=[],
        avoid_windows=[],
        reasoning="First signal is strong. . Second signal confirms it",
        confidence="medium",
    )
    _print_prediction(prediction, raw=False)
    output = capsys.readouterr().out
    assert "First signal is strong" in output
    assert "Second signal confirms it" in output


def test_predict_capacity_renders_first_time_use_remediation() -> None:
    advisor = MagicMock()
    advisor.predict_capacity_window.side_effect = _ftu_error()
    formatter = MagicMock()
    with (
        patch("cli.capacity.get_bedrock_capacity_advisor", return_value=advisor),
        patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
    ):
        result = CliRunner().invoke(
            capacity,
            ["predict", "-i", "g5.xlarge", "-r", "us-east-1"],
            obj=_config(),
        )

    assert result.exit_code == 1
    formatter.print_error.assert_called_once_with(BEDROCK_FTU_REMEDIATION)


def test_traffic_dial_show_without_controller_state_skips_the_preface() -> None:
    manager = MagicMock()
    manager.get_status.return_value = [
        RegionDialStatus(region="us-east-1", traffic_dial=100, endpoint_health="1/1 healthy")
    ]
    manager.read_controller_state.return_value = None
    with patch("cli.capacity.traffic_dial.get_traffic_dial_manager", return_value=manager):
        result = CliRunner().invoke(capacity, ["traffic-dial", "show"], obj=_config())

    assert result.exit_code == 0, result.output
    assert "Controller:" not in result.output


def test_traffic_dial_set_handles_generic_failure_and_machine_success() -> None:
    manager = MagicMock()
    manager.set_dial.side_effect = RuntimeError("Global Accelerator unavailable")
    with patch("cli.capacity.traffic_dial.get_traffic_dial_manager", return_value=manager):
        failed = CliRunner().invoke(
            capacity,
            ["traffic-dial", "set", "us-west-2", "25", "--yes"],
            obj=_config(),
        )
    assert failed.exit_code == 1
    assert "Failed to set traffic dial" in failed.output

    status = RegionDialStatus(region="us-west-2", traffic_dial=25, endpoint_health="1/1 healthy")
    manager.set_dial.side_effect = None
    manager.set_dial.return_value = status
    formatter = MagicMock()
    with (
        patch("cli.capacity.traffic_dial.get_traffic_dial_manager", return_value=manager),
        patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
    ):
        success = CliRunner().invoke(
            capacity,
            ["traffic-dial", "set", "us-west-2", "25", "--yes"],
            obj=_config("json"),
        )
    assert success.exit_code == 0, success.output
    formatter.print.assert_called_once_with(status)


def test_traffic_dial_clear_handles_generic_failure() -> None:
    manager = MagicMock()
    manager.clear_override.side_effect = RuntimeError("SSM unavailable")
    with patch("cli.capacity.traffic_dial.get_traffic_dial_manager", return_value=manager):
        result = CliRunner().invoke(capacity, ["traffic-dial", "clear", "us-west-2"], obj=_config())

    assert result.exit_code == 1
    assert "Failed to clear traffic-dial override" in result.output
