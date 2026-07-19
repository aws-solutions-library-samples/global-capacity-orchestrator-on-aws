"""
Tests for the capacity CLI subcommands in cli/commands/capacity_cmd.py.

Drives `gco capacity ai-recommend` (Bedrock-backed GPU recommendation
with alternatives, warnings, and --raw passthrough), `reservations`
(list ODCRs with optional region filter), `reservation-check` (query
ODCR + Capacity Block availability with --no-blocks), and `reserve`
(purchase a Capacity Block offering with dry-run and real-purchase
paths, including failure handling). Uses CliRunner plus MagicMock
patches of the capacity checker and Bedrock advisor — no AWS calls.
"""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from tests._scaffold_replay import DEFAULT_FIXTURE, DEFAULT_MODEL_ID


def _default_advisor_mock() -> MagicMock:
    """Return an advisor mock tied to the validated default-model fixture."""
    advisor = MagicMock()
    advisor.model_id = DEFAULT_FIXTURE.model_id
    return advisor


class TestCapacityCmdAiRecommend:
    """Cover the ai_recommend CLI command."""

    @patch("cli.capacity.get_bedrock_capacity_advisor")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_ai_recommend_success(self, mock_fmt_fn, mock_advisor_fn):
        from cli.capacity.advisor import BedrockCapacityRecommendation
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_advisor = _default_advisor_mock()
        mock_advisor.get_recommendation.return_value = BedrockCapacityRecommendation(
            recommended_region="us-east-1",
            recommended_instance_type="g5.xlarge",
            recommended_capacity_type="spot",
            reasoning="Best availability. Low cost.",
            confidence="high",
            cost_estimate="$0.50/hr",
            alternative_options=[
                {
                    "region": "us-west-2",
                    "instance_type": "g5.xlarge",
                    "capacity_type": "on-demand",
                    "reason": "Backup",
                }
            ],
            warnings=["Spot may be interrupted"],
            raw_response='{"test": true}',
        )
        mock_advisor_fn.return_value = mock_advisor

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "capacity",
                "ai-recommend",
                "-w",
                "ML training",
                "--gpu",
                "--raw",
            ],
        )
        assert result.exit_code == 0
        assert "RECOMMENDATION" in result.output
        assert "ALTERNATIVE" in result.output
        assert "WARNING" in result.output
        assert "RAW AI RESPONSE" in result.output
        mock_advisor_fn.assert_called_once()
        assert mock_advisor_fn.call_args.kwargs["model_id"] is None
        assert mock_advisor.model_id == DEFAULT_MODEL_ID

    @patch("cli.capacity.get_bedrock_capacity_advisor")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_ai_recommend_failure(self, mock_fmt_fn, mock_advisor_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_advisor = _default_advisor_mock()
        mock_advisor.get_recommendation.side_effect = RuntimeError("fail")
        mock_advisor_fn.return_value = mock_advisor

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "ai-recommend", "-w", "test"])
        assert result.exit_code == 1

    @patch("cli.capacity.get_bedrock_capacity_advisor")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_ai_recommend_no_extras(self, mock_fmt_fn, mock_advisor_fn):
        from cli.capacity.advisor import BedrockCapacityRecommendation
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_advisor = _default_advisor_mock()
        mock_advisor.get_recommendation.return_value = BedrockCapacityRecommendation(
            recommended_region="us-west-2",
            recommended_instance_type="g4dn.xlarge",
            recommended_capacity_type="on-demand",
            reasoning="Only option.",
            confidence="medium",
        )
        mock_advisor_fn.return_value = mock_advisor

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "ai-recommend"])
        assert result.exit_code == 0
        assert "us-west-2" in result.output


class TestCapacityCmdListReservations:
    """Cover the list_reservations CLI command."""

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_with_region_table(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.list_capacity_reservations.return_value = [
            {
                "instance_type": "p5.48xlarge",
                "region": "us-east-1",
                "availability_zone": "us-east-1a",
                "total_instances": 4,
                "available_instances": 2,
                "utilization_pct": 50.0,
                "instance_match_criteria": "open",
            }
        ]

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "reservations", "-r", "us-east-1"])
        assert result.exit_code == 0
        assert "p5.48xlarge" in result.output

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_no_results(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.list_all_reservations.return_value = {
            "regions_checked": ["us-east-1"],
            "total_reservations": 0,
            "total_reserved_instances": 0,
            "total_available_instances": 0,
            "reservations": [],
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "reservations"])
        assert result.exit_code == 0

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_error(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.list_all_reservations.side_effect = RuntimeError("fail")

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "reservations"])
        assert result.exit_code == 1


class TestCapacityCmdReservationCheck:
    """Cover the reservation_check CLI command."""

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_with_blocks(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.check_reservation_availability.return_value = {
            "instance_type": "p4d.24xlarge",
            "min_count_requested": 1,
            "regions_checked": ["us-east-1"],
            "odcr": {
                "total_reserved_instances": 2,
                "total_available_instances": 1,
                "has_availability": True,
                "reservations": [
                    {
                        "availability_zone": "us-east-1a",
                        "available_instances": 1,
                        "total_instances": 2,
                        "reservation_id": "cr-123",
                    }
                ],
            },
            "capacity_blocks": {
                "offerings_found": 1,
                "has_offerings": True,
                "duration_hours": 24,
                "offerings": [
                    {
                        "availability_zone": "us-east-1b",
                        "instance_count": 1,
                        "duration_hours": 24,
                        "start_date": "2025-01-15T00:00:00",
                        "upfront_fee": 500.0,
                    }
                ],
            },
            "recommendation": "ODCR capacity available",
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "reservation-check", "-i", "p4d.24xlarge"])
        assert result.exit_code == 0

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_no_blocks(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.check_reservation_availability.return_value = {
            "instance_type": "g5.xlarge",
            "min_count_requested": 1,
            "regions_checked": ["us-east-1"],
            "odcr": {
                "total_reserved_instances": 0,
                "total_available_instances": 0,
                "has_availability": False,
                "reservations": [],
            },
            "capacity_blocks": {
                "offerings_found": 0,
                "has_offerings": False,
                "duration_hours": 24,
                "offerings": [],
            },
            "recommendation": "No reserved capacity",
        }

        runner = CliRunner()
        result = runner.invoke(
            cli, ["capacity", "reservation-check", "-i", "g5.xlarge", "--no-blocks"]
        )
        assert result.exit_code == 0

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_error(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.check_reservation_availability.side_effect = RuntimeError("x")

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "reservation-check", "-i", "g5.xlarge"])
        assert result.exit_code == 1


class TestCapacityCmdReserve:
    """Cover the reserve_capacity CLI command."""

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_dry_run_success(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.purchase_capacity_block.return_value = {
            "success": True,
            "dry_run": True,
            "offering_id": "cb-123",
            "region": "us-east-1",
        }

        runner = CliRunner()
        result = runner.invoke(
            cli, ["capacity", "reserve", "-o", "cb-123", "-r", "us-east-1", "--dry-run"]
        )
        assert result.exit_code == 0
        assert "Dry run passed" in result.output

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_purchase_success(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.purchase_capacity_block.return_value = {
            "success": True,
            "dry_run": False,
            "reservation_id": "cr-456",
            "offering_id": "cb-123",
            "instance_type": "p4d.24xlarge",
            "availability_zone": "us-east-1a",
            "region": "us-east-1",
            "total_instances": 1,
            "start_date": "2025-01-15",
            "end_date": "2025-01-16",
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "reserve", "-o", "cb-123", "-r", "us-east-1"])
        assert result.exit_code == 0
        assert "purchased successfully" in result.output

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_purchase_failure(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.purchase_capacity_block.return_value = {
            "success": False,
            "dry_run": False,
            "error_code": "InvalidParameterValue",
            "error": "Expired",
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "reserve", "-o", "cb-x", "-r", "us-east-1"])
        assert result.exit_code == 1

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_exception(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.purchase_capacity_block.side_effect = RuntimeError("err")

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "reserve", "-o", "cb-x", "-r", "us-east-1"])
        assert result.exit_code == 1


class TestCapacityCmdFindReservations:
    """Cover the find_reservations CLI command."""

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_find_reservations_success(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.find_capacity_reservations.return_value = {
            "instance_type": "p5.48xlarge",
            "requested_instance_type": "p5.48xlarge",
            "valid_instance_type": True,
            "regions_checked": ["us-east-1"],
            "reservations_found": 1,
            "reservations": [
                {
                    "instance_type": "p5.48xlarge",
                    "region": "us-east-1",
                    "availability_zone": "us-east-1a",
                    "available_instances": 2,
                    "total_instances": 4,
                    "price_per_gpu_hour": 3.75,
                }
            ],
            "recommendation": "Found 1 reservation(s).",
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "find-reservations", "-i", "p5.48xlarge"])
        assert result.exit_code == 0
        assert "ODCR search" in result.output

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_find_reservations_empty(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.find_capacity_reservations.return_value = {
            "instance_type": "p5.48xlarge",
            "requested_instance_type": "p5.48xlarge",
            "valid_instance_type": True,
            "regions_checked": ["us-east-1"],
            "reservations_found": 0,
            "reservations": [],
            "recommendation": "No active ODCRs. Create one with 'gco capacity create-reservation'.",
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "find-reservations", "-i", "p5.48xlarge"])
        assert result.exit_code == 0
        assert "create-reservation" in result.output

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_find_reservations_exception(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.find_capacity_reservations.side_effect = RuntimeError("boom")

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "find-reservations", "-i", "p5.48xlarge"])
        assert result.exit_code == 1


class TestCapacityCmdCreateReservation:
    """Cover the create_reservation CLI command."""

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_create_dry_run_success(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.create_capacity_reservation.return_value = {
            "success": True,
            "dry_run": True,
            "instance_type": "p5.48xlarge",
            "availability_zone": "us-east-1a",
            "instance_count": 2,
        }

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "capacity",
                "create-reservation",
                "-i",
                "p5.48xlarge",
                "-r",
                "us-east-1",
                "-z",
                "us-east-1a",
                "-c",
                "2",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0
        assert "Dry run passed" in result.output

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_create_success(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.create_capacity_reservation.return_value = {
            "success": True,
            "dry_run": False,
            "reservation_id": "cr-new",
            "instance_type": "p5.48xlarge",
            "availability_zone": "us-east-1a",
            "total_instances": 2,
            "state": "active",
            "end_date": None,
        }

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "capacity",
                "create-reservation",
                "-i",
                "p5.48xlarge",
                "-r",
                "us-east-1",
                "-z",
                "us-east-1a",
            ],
        )
        assert result.exit_code == 0
        assert "created successfully" in result.output

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_create_failure(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.create_capacity_reservation.return_value = {
            "success": False,
            "dry_run": False,
            "error_code": "InsufficientInstanceCapacity",
            "error": "no capacity",
        }

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "capacity",
                "create-reservation",
                "-i",
                "p5.48xlarge",
                "-r",
                "us-east-1",
                "-z",
                "us-east-1a",
            ],
        )
        assert result.exit_code == 1


class TestCapacityCmdCancelReservation:
    """Cover the cancel_reservation CLI command."""

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_cancel_dry_run(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.cancel_capacity_reservation.return_value = {
            "success": True,
            "dry_run": True,
            "reservation_id": "cr-abc",
            "region": "us-east-1",
        }

        runner = CliRunner()
        result = runner.invoke(
            cli, ["capacity", "cancel-reservation", "-o", "cr-abc", "-r", "us-east-1", "--dry-run"]
        )
        assert result.exit_code == 0
        assert "can be cancelled" in result.output

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_cancel_with_yes(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.cancel_capacity_reservation.return_value = {
            "success": True,
            "dry_run": False,
            "reservation_id": "cr-abc",
            "region": "us-east-1",
            "message": "Capacity reservation cr-abc cancelled.",
        }

        runner = CliRunner()
        result = runner.invoke(
            cli, ["capacity", "cancel-reservation", "-o", "cr-abc", "-r", "us-east-1", "-y"]
        )
        assert result.exit_code == 0
        assert "cancelled" in result.output

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_cancel_aborts_without_confirmation(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["capacity", "cancel-reservation", "-o", "cr-abc", "-r", "us-east-1"],
            input="n\n",
        )
        assert result.exit_code != 0
        mock_checker_fn.return_value.cancel_capacity_reservation.assert_not_called()

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_cancel_failure(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.cancel_capacity_reservation.return_value = {
            "success": False,
            "dry_run": False,
            "error_code": "InvalidCapacityReservationId.NotFound",
            "error": "not found",
        }

        runner = CliRunner()
        result = runner.invoke(
            cli, ["capacity", "cancel-reservation", "-o", "cr-x", "-r", "us-east-1", "-y"]
        )
        assert result.exit_code == 1


class TestCapacityCheckError:
    """A CapacityCheckError from the checker must become a non-zero CLI exit
    with a detailed error message (which cli_runner.py turns into an MCP
    ``{"error": ...}`` response) rather than a silent success."""

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_check_exits_nonzero_on_capacity_check_error(self, mock_fmt_fn, mock_checker_fn):
        from cli.capacity import CapacityCheckError
        from cli.main import cli

        formatter = MagicMock()
        mock_fmt_fn.return_value = formatter
        mock_checker_fn.return_value.estimate_capacity.side_effect = CapacityCheckError(
            "Could not check instance availability in eu-west-1: RequestLimitExceeded"
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "check", "-i", "g5.xlarge", "-r", "eu-west-1"])

        assert result.exit_code == 1
        # The detailed underlying cause is surfaced to the user.
        (msg,), _ = formatter.print_error.call_args
        assert "eu-west-1" in msg
        assert "RequestLimitExceeded" in msg
