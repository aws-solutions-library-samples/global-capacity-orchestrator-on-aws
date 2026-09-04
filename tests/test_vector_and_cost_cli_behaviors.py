"""Output and failure-path tests for vector and cost CLI commands."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from click.testing import CliRunner

from cli.commands import vector_cmd
from cli.commands.costs_cmd import costs
from cli.commands.vector_cmd import vector
from cli.config import GCOConfig
from cli.vector_store import VectorStoreUnavailableError


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _config(*, output_format: str = "table") -> GCOConfig:
    return GCOConfig(
        project_name="test-gco",
        default_region="us-east-1",
        output_format=output_format,
    )


def _invoke_costs(
    runner: CliRunner,
    args: list[str],
    *,
    output_format: str = "table",
    input_text: str | None = None,
):
    kwargs: dict[str, object] = {"obj": _config(output_format=output_format)}
    if input_text is not None:
        kwargs["input"] = input_text
    return runner.invoke(costs, args, **kwargs)


def test_vector_error_envelope_omits_absent_details(monkeypatch: pytest.MonkeyPatch) -> None:
    emit = Mock()
    monkeypatch.setattr(vector_cmd, "_emit_json", emit)

    vector_cmd._emit_error("failed")

    emit.assert_called_once_with({"code": "failed"}, err=True)


@pytest.mark.parametrize(
    ("index_status", "replicas", "expects_note"),
    [
        ("ACTIVE", [], False),
        ("BUILDING", [{"region": "us-west-2", "status": "ACTIVE"}], True),
    ],
)
def test_vector_status_table_renders_replicas_and_index_note(
    runner: CliRunner,
    index_status: str,
    replicas: list[dict[str, str]],
    expects_note: bool,
) -> None:
    client = MagicMock()
    client.status.return_value = {
        "table_name": "vectors",
        "table_status": "ACTIVE",
        "index_name": "embedding-index",
        "index_status": index_status,
        "region": "us-east-1",
        "item_count": 12,
        "replicas": replicas,
    }

    with patch.object(vector_cmd, "_build_client", return_value=client):
        result = runner.invoke(vector, ["status", "--region", "us-east-1", "--output", "table"])

    assert result.exit_code == 0, result.output
    assert "table:        vectors (ACTIVE)" in result.output
    assert "index:        embedding-index" in result.output
    assert "items:        12" in result.output
    assert ("replica:      us-west-2 (ACTIVE)" in result.output) is bool(replicas)
    assert ("searches answer ValidationException" in result.output) is expects_note


@pytest.mark.parametrize(
    ("error", "code", "hint"),
    [
        (VectorStoreUnavailableError("not deployed"), "vector_store_unavailable", True),
        (RuntimeError("broken"), "vector_status_failed", False),
    ],
)
def test_vector_status_errors_are_structured(
    runner: CliRunner,
    error: Exception,
    code: str,
    hint: bool,
) -> None:
    client = MagicMock()
    client.status.side_effect = error

    with patch.object(vector_cmd, "_build_client", return_value=client):
        result = runner.invoke(vector, ["status"])

    assert result.exit_code == 1
    assert code in result.output
    assert ("vector store is not available" in result.output.lower()) is hint


def test_vector_ingest_table_renders_chunk_counts_and_async_note(runner: CliRunner) -> None:
    client = MagicMock()
    client.ingest.return_value = {
        "bucket": "corpus-bucket",
        "uploaded": ["corpus/a.md", "corpus/b.md"],
        "chunks_by_source": {"corpus/a.md": 0},
        "timed_out": False,
    }

    with (
        patch.object(vector_cmd, "_build_client", return_value=client),
        patch("cli.vector_store.demo_corpus_paths", return_value=[SimpleNamespace(name="a.md")]),
    ):
        result = runner.invoke(vector, ["ingest", "--demo", "--output", "table"])

    assert result.exit_code == 0, result.output
    assert "bucket:   corpus-bucket" in result.output
    assert "uploaded: corpus/a.md  (0 chunks)" in result.output
    assert "uploaded: corpus/b.md" in result.output
    assert "ingestion continues asynchronously" in result.output
    client.ingest.assert_called_once()
    assert client.ingest.call_args.kwargs["wait_timeout_seconds"] == 0


@pytest.mark.parametrize(
    ("wait", "timed_out", "expected_exit", "note"),
    [
        (True, True, 1, "wait timed out"),
        (True, False, 0, None),
    ],
)
def test_vector_ingest_wait_output_paths(
    runner: CliRunner,
    tmp_path,
    wait: bool,
    timed_out: bool,
    expected_exit: int,
    note: str | None,
) -> None:
    document = tmp_path / "doc.md"
    document.write_text("hello", encoding="utf-8")
    client = MagicMock()
    client.ingest.return_value = {
        "bucket": "corpus-bucket",
        "uploaded": [],
        "chunks_by_source": {},
        "timed_out": timed_out,
    }
    args = ["ingest", str(document), "--output", "table"]
    if wait:
        args.append("--wait")

    with patch.object(vector_cmd, "_build_client", return_value=client):
        result = runner.invoke(vector, args)

    assert result.exit_code == expected_exit
    if note is None:
        assert "note:" not in result.output
    else:
        assert note in result.output
    assert client.ingest.call_args.kwargs["wait_timeout_seconds"] == 300


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (VectorStoreUnavailableError("not deployed"), "vector_store_unavailable"),
        (RuntimeError("upload failed"), "vector_ingest_failed"),
    ],
)
def test_vector_ingest_errors_are_structured(
    runner: CliRunner,
    tmp_path,
    error: Exception,
    code: str,
) -> None:
    document = tmp_path / "doc.md"
    document.write_text("hello", encoding="utf-8")
    client = MagicMock()
    client.ingest.side_effect = error

    with patch.object(vector_cmd, "_build_client", return_value=client):
        result = runner.invoke(vector, ["ingest", str(document)])

    assert result.exit_code == 1
    assert code in result.output


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (VectorStoreUnavailableError("not deployed"), "vector_store_unavailable"),
        (RuntimeError("query failed"), "vector_search_failed"),
    ],
)
def test_vector_search_errors_are_structured(
    runner: CliRunner,
    error: Exception,
    code: str,
) -> None:
    client = MagicMock()
    client.search.side_effect = error

    with patch.object(vector_cmd, "_build_client", return_value=client):
        result = runner.invoke(vector, ["search", "gpu capacity"])

    assert result.exit_code == 1
    assert code in result.output


def test_vector_search_table_handles_sparse_nonnumeric_results(runner: CliRunner) -> None:
    client = MagicMock()
    client.search.return_value = [
        {
            "source": "s" * 60,
            "text": "  a   whitespace-rich  result  " + "x" * 140,
        }
    ]

    with patch.object(vector_cmd, "_build_client", return_value=client):
        result = runner.invoke(vector, ["search", "query", "--output", "table"])

    assert result.exit_code == 0, result.output
    assert "SCORE" in result.output
    assert "     -" in result.output
    assert "a whitespace-rich result" in result.output


def test_cost_workload_failure_is_reported(runner: CliRunner) -> None:
    tracker = MagicMock()
    tracker.estimate_running_workloads.side_effect = RuntimeError("pricing unavailable")

    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        result = _invoke_costs(runner, ["workloads", "--region", "us-east-1"])

    assert result.exit_code == 1
    assert "Failed to estimate workload costs: pricing unavailable" in result.output


def test_cost_forecast_json_emits_raw_result(runner: CliRunner) -> None:
    tracker = MagicMock()
    forecast = {
        "period_start": "2026-09-01",
        "period_end": "2026-09-30",
        "forecast_total": 42.5,
    }
    tracker.get_forecast.return_value = forecast

    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        result = _invoke_costs(runner, ["forecast", "--days", "30"], output_format="json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == forecast


def test_allocation_status_reports_advisory_history_failure(runner: CliRunner) -> None:
    tracker = MagicMock()
    tracker.get_cost_allocation_tag_status.return_value = [
        {"tag_key": "Project", "type": "UserDefined", "status": "Active", "last_used": ""}
    ]
    tracker.get_cost_allocation_backfill_history.side_effect = RuntimeError("billing denied")

    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        result = _invoke_costs(runner, ["allocation", "status"])

    assert result.exit_code == 0, result.output
    assert "backfill history unavailable: billing denied" in result.output
    assert "NotFound:" not in result.output
    assert "Inactive:" not in result.output


def test_allocation_status_reports_inactive_tag_and_latest_backfill(runner: CliRunner) -> None:
    tracker = MagicMock()
    tracker.get_cost_allocation_tag_status.return_value = [
        {
            "tag_key": "Project",
            "type": "UserDefined",
            "status": "Inactive",
            "last_used": "2026-09-03T11:22:33Z",
        }
    ]
    tracker.get_cost_allocation_backfill_history.return_value = [
        {
            "backfill_from": "2026-01-01T00:00:00Z",
            "requested_at": "2026-09-03T12:34:56Z",
            "status": "SUCCEEDED",
        }
    ]

    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        result = _invoke_costs(runner, ["allocation", "status"])

    assert result.exit_code == 0, result.output
    assert "Inactive: run `gco costs allocation activate`" in result.output
    assert "Latest backfill: from 2026-01-01 requested 2026-09-03T12:34:56" in result.output


def test_allocation_status_primary_failure_exits(runner: CliRunner) -> None:
    tracker = MagicMock()
    tracker.get_cost_allocation_tag_status.side_effect = RuntimeError("payer unavailable")

    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        result = _invoke_costs(runner, ["allocation", "status"])

    assert result.exit_code == 1
    assert "Failed to get cost allocation tag status: payer unavailable" in result.output


@pytest.mark.parametrize(
    "errors", [[], [{"tag_key": "Project", "code": "Denied", "message": "no"}]]
)
def test_allocation_activate_json_propagates_errors_and_exit(
    runner: CliRunner,
    errors: list[dict[str, str]],
) -> None:
    tracker = MagicMock()
    tracker.activate_cost_allocation_tags.return_value = {
        "activated": ["Project"] if not errors else [],
        "errors": errors,
    }

    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        result = _invoke_costs(
            runner,
            ["allocation", "activate", "--yes"],
            output_format="json",
        )

    assert result.exit_code == (1 if errors else 0)
    payload = json.loads(result.output)
    assert payload["errors"] == errors
    assert payload["backfill"] is None


def test_allocation_activate_table_skips_backfill_without_activated_keys(
    runner: CliRunner,
) -> None:
    tracker = MagicMock()
    tracker.activate_cost_allocation_tags.return_value = {
        "activated": [],
        "errors": [{"tag_key": "Owner", "code": "AccessDenied", "message": "no"}],
    }

    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        result = _invoke_costs(
            runner,
            ["allocation", "activate", "--yes", "--backfill-from", "2026-01-01"],
        )

    assert result.exit_code == 1
    assert "Owner: AccessDenied" in result.output
    assert "Backfill skipped: no tag keys were activated" in result.output
    tracker.start_cost_allocation_tag_backfill.assert_not_called()


def test_allocation_activate_failure_exits(runner: CliRunner) -> None:
    tracker = MagicMock()
    tracker.activate_cost_allocation_tags.side_effect = RuntimeError("not payer")

    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        result = _invoke_costs(runner, ["allocation", "activate", "--yes"])

    assert result.exit_code == 1
    assert "Failed to activate cost allocation tags: not payer" in result.output


@pytest.mark.parametrize(
    ("args", "method", "message"),
    [
        (["k8s", "regions"], "cost_by_region", "Failed to query regional costs"),
        (["k8s", "trend"], "cost_over_time", "Failed to query cost trend"),
        (["k8s", "top"], "top_spenders", "Failed to query top spenders"),
    ],
)
def test_kubernetes_cost_query_failures_exit(
    runner: CliRunner,
    args: list[str],
    method: str,
    message: str,
) -> None:
    analytics = MagicMock()
    getattr(analytics, method).side_effect = RuntimeError("athena unavailable")

    with patch("cli.cost_analytics.get_cost_analytics", return_value=analytics):
        result = _invoke_costs(runner, args)

    assert result.exit_code == 1
    assert f"{message}: athena unavailable" in result.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["report", "generate"], "Failed to generate cost report"),
        (["report", "list"], "Failed to list cost reports"),
    ],
)
def test_cost_report_api_failures_exit(
    runner: CliRunner,
    args: list[str],
    message: str,
) -> None:
    client = MagicMock()
    client.call_api.side_effect = RuntimeError("regional API unavailable")

    with patch("cli.aws_client.get_aws_client", return_value=client):
        result = _invoke_costs(runner, args)

    assert result.exit_code == 1
    assert f"{message}: regional API unavailable" in result.output
