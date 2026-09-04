"""Queue CLI boundary tests for label validation, spot gating, and rendering."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.commands.queue_cmd import queue
from cli.config import GCOConfig


def _config(output_format: str = "table") -> GCOConfig:
    return GCOConfig(
        project_name="test-gco",
        default_region="us-east-1",
        output_format=output_format,
    )


def _manifest(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: queued-job\n")
    return path


@pytest.mark.parametrize("bad_label", ["missing-separator", "=value", "key="])
def test_submit_rejects_malformed_labels_before_constructing_an_aws_client(
    tmp_path, bad_label
) -> None:
    manifest = _manifest(tmp_path)
    with patch("cli.aws_client.get_aws_client") as get_client:
        result = CliRunner().invoke(
            queue,
            ["submit", str(manifest), "--region", "us-east-1", "--label", bad_label],
            obj=_config(),
        )

    assert result.exit_code == 2
    assert "KEY=VALUE" in result.output
    get_client.assert_not_called()


def test_submit_preserves_equals_characters_inside_a_label_value(tmp_path) -> None:
    manifest = _manifest(tmp_path)
    aws_client = MagicMock()
    aws_client.call_api.return_value = {"job": {"job_id": "job-1"}}
    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        result = CliRunner().invoke(
            queue,
            [
                "submit",
                str(manifest),
                "--region",
                "us-east-1",
                "--label",
                "query=a=b",
            ],
            obj=_config(),
        )

    assert result.exit_code == 0, result.output
    assert aws_client.call_api.call_args.kwargs["body"]["labels"] == {"query": "a=b"}


def test_submit_rejects_an_incomplete_spot_gate_without_calling_aws(tmp_path) -> None:
    manifest = _manifest(tmp_path)
    with patch("cli.aws_client.get_aws_client") as get_client:
        result = CliRunner().invoke(
            queue,
            ["submit", str(manifest), "-r", "us-east-1", "--max-spot-price", "0.5"],
            obj=_config(),
        )

    assert result.exit_code == 1
    assert "spot_instance_type" in result.output.lower()
    get_client.assert_not_called()


def test_submit_adds_and_reports_a_complete_spot_gate(tmp_path) -> None:
    manifest = _manifest(tmp_path)
    aws_client = MagicMock()
    aws_client.call_api.return_value = {"job": {"job_id": "job-1"}}
    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        result = CliRunner().invoke(
            queue,
            [
                "submit",
                str(manifest),
                "-r",
                "us-west-2",
                "--max-spot-price",
                "0.5",
                "--spot-instance-type",
                "g5.xlarge",
            ],
            obj=_config(),
        )

    assert result.exit_code == 0, result.output
    body = aws_client.call_api.call_args.kwargs["body"]
    assert body["max_spot_price"] == 0.5
    assert body["spot_instance_type"] == "g5.xlarge"
    assert "Spot price gate" in result.output


@pytest.mark.parametrize(
    ("command", "response"),
    [
        (["list"], {"count": 0, "jobs": []}),
        (["get", "job-1"], {"job": {"job_id": "job-1"}}),
        (["stats"], {"summary": {}, "by_region": {}}),
    ],
)
def test_machine_output_paths_forward_the_complete_api_payload(command, response) -> None:
    aws_client = MagicMock()
    aws_client.call_api.return_value = response
    formatter = MagicMock()
    with (
        patch("cli.aws_client.get_aws_client", return_value=aws_client),
        patch("cli.commands.queue_cmd.get_output_formatter", return_value=formatter),
    ):
        result = CliRunner().invoke(queue, command, obj=_config("json"))

    assert result.exit_code == 0, result.output
    formatter.print.assert_called_once_with(response)


def test_get_table_renders_all_optional_spot_and_lifecycle_fields() -> None:
    aws_client = MagicMock()
    aws_client.call_api.return_value = {
        "job": {
            "job_id": "job-1",
            "job_name": "training",
            "target_region": "us-west-2",
            "namespace": "gco-jobs",
            "status": "failed",
            "priority": 50,
            "submitted_at": "2026-01-01T00:00:00Z",
            "spot_max_price": 0.5,
            "spot_instance_type": "g5.xlarge",
            "spot_gate_observed_price": 0.7,
            "spot_gate_checked_at": "2026-01-01T00:01:00Z",
            "claimed_by": "worker-west",
            "completed_at": "2026-01-01T00:02:00Z",
            "error_message": "price gate expired",
            "status_history": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "status": "queued",
                    "message": "waiting for spot price",
                }
            ],
        }
    }
    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        result = CliRunner().invoke(queue, ["get", "job-1"], obj=_config())

    assert result.exit_code == 0, result.output
    for expected in (
        "Spot Gate",
        "Last Price",
        "Claimed By",
        "Completed",
        "price gate expired",
        "Status History",
    ):
        assert expected in result.output


def test_cancel_confirmation_path_submits_after_yes() -> None:
    aws_client = MagicMock()
    aws_client.call_api.return_value = {"message": "cancelled"}
    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        result = CliRunner().invoke(
            queue,
            ["cancel", "job-1", "--region", "us-west-2"],
            input="y\n",
            obj=_config(),
        )

    assert result.exit_code == 0, result.output
    assert "Cancel job job-1?" in result.output
    aws_client.call_api.assert_called_once()


def test_stats_table_without_region_breakdown_finishes_after_summary() -> None:
    aws_client = MagicMock()
    aws_client.call_api.return_value = {
        "summary": {"total_jobs": 3, "total_queued": 2, "total_running": 1},
        "by_region": {},
    }
    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        result = CliRunner().invoke(queue, ["stats"], obj=_config())

    assert result.exit_code == 0, result.output
    assert "Total Jobs:   3" in result.output
    assert "By Region" not in result.output


def test_get_table_omits_absent_optional_fields_and_empty_history() -> None:
    aws_client = MagicMock()
    aws_client.call_api.return_value = {
        "job": {
            "job_id": "job-minimal",
            "job_name": "minimal",
            "target_region": "us-east-1",
            "namespace": "default",
            "status": "queued",
            "priority": 0,
            "submitted_at": "2026-01-01T00:00:00Z",
            "status_history": [],
        }
    }
    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        result = CliRunner().invoke(queue, ["get", "job-minimal"], obj=_config())

    assert result.exit_code == 0, result.output
    assert "job-minimal" in result.output
    assert "Spot Gate" not in result.output
    assert "Status History" not in result.output


def test_get_table_spot_gate_without_an_observed_price_skips_last_price() -> None:
    aws_client = MagicMock()
    aws_client.call_api.return_value = {
        "job": {
            "job_id": "job-waiting",
            "spot_max_price": 0.5,
            "spot_instance_type": "g5.xlarge",
            "status_history": [],
        }
    }
    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        result = CliRunner().invoke(queue, ["get", "job-waiting"], obj=_config())

    assert result.exit_code == 0, result.output
    assert "Spot Gate" in result.output
    assert "Last Price" not in result.output
