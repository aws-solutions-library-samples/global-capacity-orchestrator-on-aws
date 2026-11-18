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


@pytest.mark.parametrize("bad_label", ["missing-separator", "=value"])
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
    assert "labels must use key=value with a non-empty key" in result.output
    get_client.assert_not_called()


def test_submit_accepts_an_empty_label_value_like_the_other_submit_commands(tmp_path) -> None:
    manifest = _manifest(tmp_path)
    aws_client = MagicMock()
    aws_client.call_api.return_value = {"job": {"job_id": "job-1"}}
    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        result = CliRunner().invoke(
            queue,
            ["submit", str(manifest), "--region", "us-east-1", "--label", "urgent="],
            obj=_config(),
        )

    assert result.exit_code == 0, result.output
    assert aws_client.call_api.call_args.kwargs["body"]["labels"] == {"urgent": ""}


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


# ---------------------------------------------------------------------------
# One implementation behind `gco queue submit` and `gco jobs submit-queue`
# ---------------------------------------------------------------------------

_SUBMIT_COMMANDS = [["queue", "submit"], ["jobs", "submit-queue"]]


def _write_manifest(tmp_path, namespace: str | None = None):
    metadata = "metadata:\n  name: queued-job\n"
    if namespace is not None:
        metadata += f"  namespace: {namespace}\n"
    path = tmp_path / "job.yaml"
    path.write_text(
        "apiVersion: batch/v1\nkind: Job\n" + metadata + "spec:\n  template:\n"
        "    spec:\n      containers:\n      - name: main\n        image: busybox\n"
    )
    return str(path)


def _submit(args, env=None):
    """Invoke the root CLI and return (result, the mocked AWS client)."""
    from cli.main import cli

    aws_client = MagicMock()
    aws_client.call_api.return_value = {"job": {"job_id": "job-1"}}
    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        result = CliRunner().invoke(cli, args, env=env or {})
    return result, aws_client


def test_jobs_submit_queue_is_the_queue_submit_command() -> None:
    from cli.commands.jobs_cmd import jobs
    from cli.commands.queue_cmd import queue_submit

    assert jobs.commands["submit-queue"] is queue_submit
    assert queue.commands["submit"] is queue_submit


@pytest.mark.parametrize(
    ("mode", "expected_region"),
    [([], None), (["--regional-api"], "us-west-2")],
)
def test_both_commands_send_the_same_request(tmp_path, mode, expected_region) -> None:
    manifest = _write_manifest(tmp_path)
    options = [
        "-r",
        "us-west-2",
        "--priority",
        "60",
        "-l",
        "team=ml",
        "--sandbox",
        "isolated",
        "--max-spot-price",
        "0.5",
        "--spot-instance-type",
        "g5.xlarge",
    ]
    calls = []
    for command in _SUBMIT_COMMANDS:
        result, aws_client = _submit([*mode, *command, manifest, *options])
        assert result.exit_code == 0, result.output
        calls.append(aws_client.call_api.call_args)

    assert calls[0] == calls[1]
    kwargs = calls[0].kwargs
    assert kwargs["region"] == expected_region
    body = kwargs["body"]
    assert body["priority"] == 60
    assert body["labels"] == {"team": "ml"}
    assert (body["max_spot_price"], body["spot_instance_type"]) == (0.5, "g5.xlarge")
    assert body["manifest"]["spec"]["template"]["spec"]["runtimeClassName"] == ("isolated-sandbox")


@pytest.mark.parametrize("command", _SUBMIT_COMMANDS)
@pytest.mark.parametrize("priority", ["-1", "101"])
def test_both_commands_refuse_a_priority_the_api_rejects(tmp_path, command, priority) -> None:
    result, aws_client = _submit(
        [*command, _write_manifest(tmp_path), "-r", "us-east-1", "--priority", priority]
    )

    assert result.exit_code == 2
    assert "0<=x<=100" in result.output
    aws_client.call_api.assert_not_called()


@pytest.mark.parametrize("command", _SUBMIT_COMMANDS)
def test_the_manifest_namespace_is_used_when_it_declares_one(tmp_path, command) -> None:
    result, aws_client = _submit(
        [*command, _write_manifest(tmp_path, "ml-jobs"), "-r", "us-east-1"],
        env={"GCO_DEFAULT_NAMESPACE": "team-a"},
    )

    assert result.exit_code == 0, result.output
    body = aws_client.call_api.call_args.kwargs["body"]
    assert body["namespace"] == "ml-jobs"
    assert body["manifest"]["metadata"]["namespace"] == "ml-jobs"


@pytest.mark.parametrize("command", _SUBMIT_COMMANDS)
def test_the_default_namespace_applies_only_without_a_declared_one(tmp_path, command) -> None:
    manifest = _write_manifest(tmp_path)

    result, aws_client = _submit(
        [*command, manifest, "-r", "us-east-1"], env={"GCO_DEFAULT_NAMESPACE": "team-a"}
    )
    assert result.exit_code == 0, result.output
    assert aws_client.call_api.call_args.kwargs["body"]["namespace"] == "team-a"

    result, aws_client = _submit([*command, manifest, "-r", "us-east-1", "-n", "ml-jobs"])
    assert result.exit_code == 0, result.output
    assert aws_client.call_api.call_args.kwargs["body"]["namespace"] == "ml-jobs"


@pytest.mark.parametrize("command", _SUBMIT_COMMANDS)
def test_a_namespace_option_matching_the_manifest_is_accepted(tmp_path, command) -> None:
    result, aws_client = _submit(
        [*command, _write_manifest(tmp_path, "ml-jobs"), "-r", "us-east-1", "-n", "ml-jobs"]
    )

    assert result.exit_code == 0, result.output
    assert aws_client.call_api.call_args.kwargs["body"]["namespace"] == "ml-jobs"


@pytest.mark.parametrize("command", _SUBMIT_COMMANDS)
def test_a_conflicting_namespace_option_fails_before_any_aws_call(tmp_path, command) -> None:
    result, aws_client = _submit(
        [*command, _write_manifest(tmp_path, "ml-jobs"), "-r", "us-east-1", "-n", "gco-jobs"]
    )

    assert result.exit_code == 1
    assert "declares namespace 'ml-jobs' but --namespace is 'gco-jobs'" in result.stderr
    aws_client.call_api.assert_not_called()


@pytest.mark.parametrize("command", _SUBMIT_COMMANDS)
def test_a_manifest_that_is_not_one_mapping_is_refused(tmp_path, command) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- kind: Job\n")

    result, aws_client = _submit([*command, str(path), "-r", "us-east-1"])

    assert result.exit_code == 1
    assert "single YAML mapping" in result.stderr
    aws_client.call_api.assert_not_called()


# ---------------------------------------------------------------------------
# `gco queue list` paging
# ---------------------------------------------------------------------------


def _list(args, page, output_format="table"):
    aws_client = MagicMock()
    aws_client.call_api.return_value = page
    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        result = CliRunner().invoke(queue, ["list", *args], obj=_config(output_format))
    return result, aws_client


_PAGE = {
    "count": 1,
    "jobs": [
        {"job_id": "job-1", "job_name": "first", "target_region": "us-east-1", "status": "queued"}
    ],
    "next_cursor": "eyJ2ZXJzaW9uIjoxfQ",
    "partial": False,
}


def test_list_prints_the_command_for_the_next_page() -> None:
    result, aws_client = _list(["-s", "queued", "-r", "us-east-1", "--limit", "1"], _PAGE)

    assert result.exit_code == 0, result.output
    assert "Queued Jobs (1 in this page)" in result.output
    assert (
        "Next page: gco queue list --region us-east-1 --status queued --limit 1 "
        "--cursor eyJ2ZXJzaW9uIjoxfQ"
    ) in result.output
    assert "cursor" not in aws_client.call_api.call_args.kwargs["params"]


def test_list_sends_the_cursor_back_with_the_filters() -> None:
    last_page = {**_PAGE, "next_cursor": None}

    result, aws_client = _list(["-s", "queued", "--cursor", "eyJ2ZXJzaW9uIjoxfQ"], last_page)

    assert result.exit_code == 0, result.output
    assert aws_client.call_api.call_args.kwargs["params"] == {
        "limit": 50,
        "status": "queued",
        "cursor": "eyJ2ZXJzaW9uIjoxfQ",
    }
    assert "Next page" not in result.output


def test_an_empty_page_with_a_cursor_is_not_reported_as_no_jobs() -> None:
    result, _ = _list([], {"count": 0, "jobs": [], "next_cursor": "abc", "partial": True})

    assert result.exit_code == 0, result.output
    assert "No jobs in this page" in result.output
    assert "No jobs found" not in result.output
    assert "--cursor abc" in result.output


def test_json_output_keeps_next_cursor() -> None:
    import json

    result, _ = _list([], _PAGE, output_format="json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["next_cursor"] == "eyJ2ZXJzaW9uIjoxfQ"


@pytest.mark.parametrize("limit", ["0", "1001"])
def test_list_limit_outside_the_api_range_is_a_usage_error(limit) -> None:
    result, aws_client = _list(["--limit", limit], _PAGE)

    assert result.exit_code == 2
    aws_client.call_api.assert_not_called()
