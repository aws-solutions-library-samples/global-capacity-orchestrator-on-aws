"""Capacity CLI output-mode, fallback, and error-boundary tests."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest
import yaml
from botocore.exceptions import ClientError
from click.testing import CliRunner

from cli.capacity.advisor import (
    AI_RECOMMENDATION_DISCLAIMER,
    BedrockCapacityRecommendation,
    CapacityPredictionResult,
)
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


_RESERVATION_FAILURE = {
    "success": False,
    "dry_run": False,
    "region": "us-east-1",
    "error_code": "InsufficientInstanceCapacity",
    "error": "sold out",
}


@pytest.mark.parametrize("output_format", ["json", "yaml"])
@pytest.mark.parametrize(
    ("checker_method", "arguments"),
    [
        (
            "purchase_capacity_block",
            ["reserve", "-o", "cb-0123456789abcdef0", "-r", "us-east-1", "-y"],
        ),
        (
            "create_capacity_reservation",
            [
                "create-reservation",
                "-i",
                "p5.48xlarge",
                "-r",
                "us-east-1",
                "-z",
                "us-east-1a",
                "-y",
            ],
        ),
        (
            "cancel_capacity_reservation",
            ["cancel-reservation", "-o", "cr-0123456789abcdef0", "-r", "us-east-1", "-y"],
        ),
    ],
)
def test_reservation_commands_exit_nonzero_on_failure_in_machine_modes(
    checker_method: str, arguments: list[str], output_format: str
) -> None:
    """A failed purchase/create/cancel used to print its document and exit 0
    in JSON/YAML mode, so a script checking $? saw a success."""
    from cli.main import cli

    checker = MagicMock()
    getattr(checker, checker_method).return_value = dict(_RESERVATION_FAILURE)
    with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
        result = CliRunner().invoke(cli, ["-o", output_format, "capacity", *arguments])

    assert result.exit_code == 1, result.output
    document = (json.loads if output_format == "json" else yaml.safe_load)(result.stdout)
    assert document == _RESERVATION_FAILURE
    assert "Failed: InsufficientInstanceCapacity: sold out" in result.stderr


_CHARGING_COMMANDS = [
    pytest.param(
        "purchase_capacity_block",
        ["reserve", "-o", "cb-0123456789abcdef0", "-r", "us-east-1"],
        id="reserve",
    ),
    pytest.param(
        "create_capacity_reservation",
        ["create-reservation", "-i", "p5.48xlarge", "-r", "us-east-1", "-z", "us-east-1a"],
        id="create-reservation",
    ),
]
_CHARGED = {"success": True, "dry_run": False, "reservation_id": "cr-0123456789abcdef0"}


@pytest.mark.parametrize(("checker_method", "arguments"), _CHARGING_COMMANDS)
def test_charging_commands_refuse_to_run_unconfirmed_without_a_terminal(
    checker_method: str, arguments: list[str]
) -> None:
    """Both commands used to spend money with no confirmation at all. Without
    a terminal nobody can answer a prompt (and one reading an open pipe would
    wait forever), so the command stops before calling EC2."""
    from cli.main import cli

    checker = MagicMock()
    with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
        result = CliRunner().invoke(cli, ["capacity", *arguments])

    assert result.exit_code == 2, result.output
    assert "pass --yes" in result.stderr
    getattr(checker, checker_method).assert_not_called()


@pytest.mark.parametrize(("checker_method", "arguments"), _CHARGING_COMMANDS)
def test_charging_commands_ask_first_and_a_declined_prompt_exits_1(
    checker_method: str, arguments: list[str]
) -> None:
    from cli.main import cli

    checker = MagicMock()
    with (
        patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker),
        patch("cli.commands.capacity_cmd._stdin_is_interactive", return_value=True),
    ):
        result = CliRunner().invoke(cli, ["capacity", *arguments], input="n\n")

    assert result.exit_code == 1, result.output
    assert "[y/N]" in result.output
    assert "Aborted" in result.stderr
    getattr(checker, checker_method).assert_not_called()


@pytest.mark.parametrize(("checker_method", "arguments"), _CHARGING_COMMANDS)
def test_charging_commands_proceed_after_an_accepted_prompt(
    checker_method: str, arguments: list[str]
) -> None:
    """In JSON mode the prompt goes to stderr, so stdout stays one document."""
    from cli.main import cli

    checker = MagicMock()
    getattr(checker, checker_method).return_value = dict(_CHARGED)
    with (
        patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker),
        patch("cli.commands.capacity_cmd._stdin_is_interactive", return_value=True),
    ):
        result = CliRunner().invoke(cli, ["-o", "json", "capacity", *arguments], input="y\n")

    assert result.exit_code == 0, result.output
    assert "[y/N]" in result.stderr
    assert json.loads(result.stdout) == _CHARGED
    getattr(checker, checker_method).assert_called_once()


@pytest.mark.parametrize("skip_flag", ["--yes", "-y", "--dry-run"])
@pytest.mark.parametrize(("checker_method", "arguments"), _CHARGING_COMMANDS)
def test_charging_commands_skip_the_prompt_for_yes_and_dry_run(
    checker_method: str, arguments: list[str], skip_flag: str
) -> None:
    """--yes answers in advance and a dry run costs nothing, so neither asks,
    even without a terminal."""
    from cli.main import cli

    checker = MagicMock()
    getattr(checker, checker_method).return_value = dict(_CHARGED)
    with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
        result = CliRunner().invoke(cli, ["-o", "json", "capacity", *arguments, skip_flag])

    assert result.exit_code == 0, result.output
    assert "[y/N]" not in result.output
    getattr(checker, checker_method).assert_called_once()
    assert getattr(checker, checker_method).call_args.kwargs["dry_run"] is (
        skip_flag == "--dry-run"
    )


_CANCEL_ARGUMENTS = ["cancel-reservation", "-o", "cr-0123456789abcdef0", "-r", "us-east-1"]


def test_cancel_reservation_refuses_to_run_unconfirmed_without_a_terminal() -> None:
    """cancel-reservation read its confirmation from stdin, so a script that
    left stdin on an open pipe hung. Without a terminal and without --yes it
    now stops before calling EC2, as reserve and create-reservation do."""
    from cli.main import cli

    checker = MagicMock()
    with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
        result = CliRunner().invoke(cli, ["capacity", *_CANCEL_ARGUMENTS])

    assert result.exit_code == 2, result.output
    assert "stdin is not a terminal" in result.stderr
    assert "pass --yes" in result.stderr
    assert "incurs charges" not in result.stderr
    checker.cancel_capacity_reservation.assert_not_called()


@pytest.mark.parametrize(("answer", "exit_code"), [("n\n", 1), ("y\n", 0)])
def test_cancel_reservation_asks_on_a_terminal(answer: str, exit_code: int) -> None:
    from cli.main import cli

    checker = MagicMock()
    checker.cancel_capacity_reservation.return_value = {
        "success": True,
        "dry_run": False,
        "message": "Capacity reservation cr-0123456789abcdef0 cancelled.",
    }
    with (
        patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker),
        patch("cli.commands.capacity_cmd._stdin_is_interactive", return_value=True),
    ):
        result = CliRunner().invoke(cli, ["capacity", *_CANCEL_ARGUMENTS], input=answer)

    assert result.exit_code == exit_code, result.output
    assert "Cancel capacity reservation 'cr-0123456789abcdef0' in us-east-1?" in result.output
    assert checker.cancel_capacity_reservation.called is (exit_code == 0)


@pytest.mark.parametrize("skip_flag", ["--yes", "-y", "--dry-run"])
def test_cancel_reservation_needs_no_terminal_with_yes_or_dry_run(skip_flag: str) -> None:
    from cli.main import cli

    checker = MagicMock()
    checker.cancel_capacity_reservation.return_value = {"success": True, "dry_run": True}
    with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
        result = CliRunner().invoke(cli, ["capacity", *_CANCEL_ARGUMENTS, skip_flag])

    assert result.exit_code == 0, result.output
    assert "[y/N]" not in result.output
    checker.cancel_capacity_reservation.assert_called_once()


def test_stdin_is_interactive_needs_a_terminal_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import capacity_cmd

    monkeypatch.setattr(sys, "stdin", None)
    assert capacity_cmd._stdin_is_interactive() is False

    terminal = MagicMock()
    terminal.isatty.return_value = True
    monkeypatch.setattr(sys, "stdin", terminal)
    assert capacity_cmd._stdin_is_interactive() is True

    terminal.isatty.return_value = False
    assert capacity_cmd._stdin_is_interactive() is False


_HISTORICAL_STATS = {
    "instance_type": "g5.xlarge",
    "region": "us-east-1",
    "hours_back": 168,
    "sample_count": 5,
    "metrics": {"spot_score": {"p25": 5, "p50": 6, "p75": 7, "min": 3, "max": 9}},
}
_ESTIMATE = {"instance_type": "g5.xlarge", "region": "us-east-1", "capacity_type": "spot"}


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_check_enrich_historical_prints_one_machine_document(output_format: str) -> None:
    """The historical lines used to be printed after the estimates, so the root
    output transaction wrapped all of stdout as {"status": "ok", "output": ...}."""
    from cli.main import cli

    checker = MagicMock()
    checker.estimate_capacity.return_value = [dict(_ESTIMATE)]
    store = MagicMock()
    store.get_statistics.return_value = dict(_HISTORICAL_STATS)
    with (
        patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker),
        patch("cli.capacity.history.get_capacity_history_store", return_value=store),
    ):
        result = CliRunner().invoke(
            cli,
            [
                "-o",
                output_format,
                "capacity",
                "check",
                "-i",
                "g5.xlarge",
                "-r",
                "us-east-1",
                "--enrich-historical",
            ],
        )

    assert result.exit_code == 0, result.output
    document = (json.loads if output_format == "json" else yaml.safe_load)(result.stdout)
    assert document == {"estimates": [_ESTIMATE], "historical": _HISTORICAL_STATS}


def test_check_enrich_historical_machine_document_has_null_history_when_unreadable() -> None:
    from cli.main import cli

    checker = MagicMock()
    checker.estimate_capacity.return_value = [dict(_ESTIMATE)]
    store = MagicMock()
    store.get_statistics.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}}, "Query"
    )
    with (
        patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker),
        patch("cli.capacity.history.get_capacity_history_store", return_value=store),
    ):
        result = CliRunner().invoke(
            cli,
            [
                "-o",
                "json",
                "capacity",
                "check",
                "-i",
                "g5.xlarge",
                "-r",
                "us-east-1",
                "--enrich-historical",
            ],
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"estimates": [_ESTIMATE], "historical": None}
    assert "optional add-on" in result.stderr


def _ai_recommendation() -> BedrockCapacityRecommendation:
    return BedrockCapacityRecommendation(
        recommended_region="us-west-2",
        recommended_instance_type="g5.xlarge",
        recommended_capacity_type="spot",
        reasoning="High spot score. Low queue depth.",
        confidence="high",
        cost_estimate="$0.40/hr",
        alternative_options=[
            {"region": "us-east-1", "instance_type": "g5.xlarge", "capacity_type": "on-demand"}
        ],
        warnings=["Spot can be interrupted"],
        raw_response='{"recommended_region": "us-west-2"}',
    )


_AI_DOCUMENT = {
    "recommended_region": "us-west-2",
    "recommended_instance_type": "g5.xlarge",
    "recommended_capacity_type": "spot",
    "instance_count": None,
    "confidence": "high",
    "cost_estimate": "$0.40/hr",
    "reasoning": "High spot score. Low queue depth.",
    "reservation_advice": None,
    "alternative_options": [
        {"region": "us-east-1", "instance_type": "g5.xlarge", "capacity_type": "on-demand"}
    ],
    "warnings": ["Spot can be interrupted"],
    "validation_warnings": [],
    "model_id": None,
    "bedrock_region": None,
    "data_collected_at": None,
    "regions_analyzed": [],
    "instance_types_analyzed": [],
    "data_gaps": [],
    "usage": {},
    "latency_ms": None,
    # stdout is all the MCP tool reads, so the disclaimer is in the document too.
    "disclaimer": AI_RECOMMENDATION_DISCLAIMER,
}


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_ai_recommend_prints_one_machine_document(output_format: str) -> None:
    """The disclaimer and the recommendation used to be printed as text in
    every mode, so JSON/YAML callers (the MCP tool included) got the text
    envelope instead of the recommendation."""
    from cli.main import cli

    advisor = MagicMock()
    advisor.model_id = "example.model-v1"
    advisor.get_recommendation.return_value = _ai_recommendation()
    with patch("cli.capacity.get_bedrock_capacity_advisor", return_value=advisor):
        result = CliRunner().invoke(
            cli, ["-o", output_format, "capacity", "ai-recommend", "-w", "training"]
        )

    assert result.exit_code == 0, result.output
    document = (json.loads if output_format == "json" else yaml.safe_load)(result.stdout)
    assert document == _AI_DOCUMENT
    assert "AI-POWERED RECOMMENDATION DISCLAIMER" in result.stderr


def test_ai_recommend_machine_document_carries_the_raw_response_only_with_raw() -> None:
    from cli.main import cli

    advisor = MagicMock()
    advisor.model_id = "example.model-v1"
    advisor.get_recommendation.return_value = _ai_recommendation()
    with patch("cli.capacity.get_bedrock_capacity_advisor", return_value=advisor):
        result = CliRunner().invoke(
            cli, ["-o", "json", "capacity", "ai-recommend", "-w", "training", "--raw"]
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        **_AI_DOCUMENT,
        "raw_response": '{"recommended_region": "us-west-2"}',
    }


def test_ai_recommend_table_mode_keeps_the_disclaimer_on_stdout() -> None:
    from cli.main import cli

    advisor = MagicMock()
    advisor.model_id = "example.model-v1"
    advisor.get_recommendation.return_value = _ai_recommendation()
    with patch("cli.capacity.get_bedrock_capacity_advisor", return_value=advisor):
        result = CliRunner().invoke(cli, ["capacity", "ai-recommend", "-w", "training"])

    assert result.exit_code == 0, result.output
    assert "AI-POWERED RECOMMENDATION DISCLAIMER" in result.stdout
    assert "Region:        us-west-2" in result.stdout


@pytest.mark.parametrize(
    ("output_format", "parse"), [("json", json.loads), ("yaml", yaml.safe_load)]
)
def test_spot_prices_empty_result_keeps_the_list_shape(output_format: str, parse) -> None:
    """An empty history used to return after the warning, so the root output
    transaction printed {"status": "ok"} instead of an empty list."""
    from cli.main import cli

    checker = MagicMock()
    checker.get_spot_price_history.return_value = []
    with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
        result = CliRunner().invoke(
            cli,
            ["-o", output_format, "capacity", "spot-prices", "-i", "g5.xlarge", "-r", "us-east-1"],
        )

    assert result.exit_code == 0, result.output
    assert parse(result.stdout) == []
    assert "No spot price data for g5.xlarge in us-east-1" in result.stderr


def test_spot_prices_empty_result_in_table_mode_prints_only_the_warning() -> None:
    from cli.main import cli

    checker = MagicMock()
    checker.get_spot_price_history.return_value = []
    with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
        result = CliRunner().invoke(
            cli, ["capacity", "spot-prices", "-i", "g5.xlarge", "-r", "us-east-1"]
        )

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "No spot price data" in result.stderr


# ---------------------------------------------------------------------------
# Instance-type aliases reach AWS expanded
# ---------------------------------------------------------------------------


def test_instance_type_alias_callback_expands_single_repeated_and_absent_values() -> None:
    from cli.commands.capacity_cmd import _canonical_instance_types

    assert _canonical_instance_types(None, None, "p5") == "p5.48xlarge"  # type: ignore[arg-type]
    assert _canonical_instance_types(None, None, "g5.xlarge") == "g5.xlarge"  # type: ignore[arg-type]
    assert _canonical_instance_types(None, None, ("b200", "g5.xlarge")) == (  # type: ignore[arg-type]
        "p6-b200.48xlarge",
        "g5.xlarge",
    )
    assert _canonical_instance_types(None, None, None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("arguments", "method", "expected"),
    [
        (["check", "-i", "p5", "-r", "us-east-1"], "estimate_capacity", ("p5.48xlarge",)),
        (
            ["spot-prices", "-i", "p5", "-r", "us-east-1"],
            "get_spot_price_history",
            ("p5.48xlarge",),
        ),
        (["recommend", "-i", "p5", "-r", "us-east-1"], "recommend_capacity_type", ("p5.48xlarge",)),
    ],
)
def test_commands_that_call_ec2_directly_expand_the_alias(
    arguments: list[str], method: str, expected: tuple[str, ...]
) -> None:
    """``gco capacity check -i p5`` used to report that "p5" is not offered."""
    checker = MagicMock()
    checker.estimate_capacity.return_value = []
    checker.get_spot_price_history.return_value = []
    checker.recommend_capacity_type.return_value = ("spot", "ok")
    with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
        result = CliRunner().invoke(capacity, arguments, obj=_config("json"))
    assert result.exit_code == 0, result.output
    assert getattr(checker, method).call_args.args[: len(expected)] == expected


# ---------------------------------------------------------------------------
# JSON/YAML stay one document when there is nothing to report
# ---------------------------------------------------------------------------


def _resource_not_found() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}}, "Query"
    )


@pytest.mark.parametrize(
    ("command", "store_method", "empty", "document"),
    [
        ("show", "get_trend", [], []),
        (
            "stats",
            "get_statistics",
            {"sample_count": 0, "metrics": {}},
            {"sample_count": 0, "metrics": {}},
        ),
        (
            "patterns",
            "get_temporal_patterns",
            {"patterns": {}, "best_windows": []},
            {"patterns": {}, "best_windows": []},
        ),
    ],
)
def test_history_without_samples_prints_an_empty_document(
    command: str, store_method: str, empty: object, document: object
) -> None:
    from cli.main import cli

    store = MagicMock()
    getattr(store, store_method).return_value = empty
    with patch("cli.capacity.history.get_capacity_history_store", return_value=store):
        result = CliRunner().invoke(
            cli, ["-o", "json", "capacity", "history", command, "-i", "p5", "-r", "us-east-1"]
        )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == document
    assert "No historical samples for p5.48xlarge in us-east-1" in result.stderr


@pytest.mark.parametrize("command", ["show", "stats", "patterns"])
def test_history_not_deployed_says_so_in_machine_output(command: str) -> None:
    from cli.main import cli

    store = MagicMock()
    for method in ("get_trend", "get_statistics", "get_temporal_patterns"):
        getattr(store, method).side_effect = _resource_not_found()
    with patch("cli.capacity.history.get_capacity_history_store", return_value=store):
        result = CliRunner().invoke(
            cli,
            ["-o", "json", "capacity", "history", command, "-i", "g5.xlarge", "-r", "us-east-1"],
        )
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["historical_enabled"] is False
    assert "historical.enabled" in document["message"]
    assert "historical.enabled" in result.stderr


def test_predict_without_samples_prints_a_document_naming_the_reason() -> None:
    from cli.main import cli

    advisor = MagicMock()
    advisor.predict_capacity_window.side_effect = ValueError("No historical capacity samples yet.")
    advisor.predict_capacity_windows_all_regions.side_effect = ValueError("none anywhere")
    with patch("cli.capacity.get_bedrock_capacity_advisor", return_value=advisor):
        single = CliRunner().invoke(
            cli, ["-o", "json", "capacity", "predict", "-i", "g5.xlarge", "-r", "us-east-1"]
        )
        every = CliRunner().invoke(
            cli, ["-o", "json", "capacity", "predict", "-i", "g5.xlarge", "--all-regions"]
        )
    assert single.exit_code == 0, single.output
    assert json.loads(single.stdout) == {
        "instance_type": "g5.xlarge",
        "region": "us-east-1",
        "message": "No historical capacity samples yet.",
    }
    assert every.exit_code == 0, every.output
    assert json.loads(every.stdout) == []
    assert "none anywhere" in every.stderr


def test_predict_with_no_usable_region_and_without_history_prints_documents() -> None:
    from cli.main import cli

    advisor = MagicMock()
    advisor.predict_capacity_windows_all_regions.return_value = []
    advisor.predict_capacity_window.side_effect = _resource_not_found()
    with patch("cli.capacity.get_bedrock_capacity_advisor", return_value=advisor):
        empty = CliRunner().invoke(
            cli, ["-o", "json", "capacity", "predict", "-i", "g5.xlarge", "--all-regions"]
        )
        disabled = CliRunner().invoke(
            cli, ["-o", "json", "capacity", "predict", "-i", "g5.xlarge", "-r", "us-east-1"]
        )
    assert empty.exit_code == 0, empty.output
    assert json.loads(empty.stdout) == []
    assert "No usable historical samples for g5.xlarge" in empty.stderr
    assert disabled.exit_code == 0, disabled.output
    assert json.loads(disabled.stdout)["historical_enabled"] is False


@pytest.mark.parametrize("existed", [True, False])
def test_traffic_dial_clear_prints_a_machine_document(existed: bool) -> None:
    manager = MagicMock()
    manager.clear_override.return_value = existed
    with patch("cli.capacity.traffic_dial.get_traffic_dial_manager", return_value=manager):
        result = CliRunner().invoke(
            capacity, ["traffic-dial", "clear", "us-west-2"], obj=_config("json")
        )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"region": "us-west-2", "override_cleared": existed}


def test_reservation_check_table_names_regions_whose_block_search_failed() -> None:
    checker = MagicMock()
    checker.check_reservation_availability.return_value = {
        "odcr": {"reservations": [], "total_available_instances": 0, "total_reserved_instances": 0},
        "capacity_blocks": {
            "duration_hours": 24,
            "offerings": [],
            "errors": [
                {
                    "region": "eu-west-1",
                    "duration_hours": 24,
                    "error_code": "UnauthorizedOperation",
                    "error": "not authorized",
                }
            ],
        },
        "recommendation": "No reserved capacity or block offerings found.",
    }
    with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
        result = CliRunner().invoke(capacity, ["reservation-check", "-i", "p5"], obj=_config())
    assert result.exit_code == 0, result.output
    assert "The Capacity Block search failed in 1 Region(s)" in result.output
    assert "eu-west-1: UnauthorizedOperation: not authorized" in result.output
    assert checker.check_reservation_availability.call_args.kwargs["instance_type"] == "p5.48xlarge"
