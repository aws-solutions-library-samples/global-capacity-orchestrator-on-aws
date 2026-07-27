"""
Tests for cli/cost_analytics.py and the new gco costs command surface.

Covers the Athena client (query execution polling, failure/timeout paths,
header-row handling, parameterized namespace filters, identifier lockstep
with the stack constants), each canned aggregation's SQL shape, and the
Click commands: costs k8s namespaces/regions/trend/top rendering Athena
rows, costs report generate/list/status calling the region-pinned GCO API,
and the transport-region resolution helper.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.config import GCOConfig
from cli.cost_analytics import (
    AthenaQueryError,
    CostAnalytics,
    QueryResult,
    _database_name,
    _workgroup_name,
)


def _athena_result_pages(columns, rows):
    """Shape one get_query_results page including the repeated header row."""
    header = {"Data": [{"VarCharValue": column} for column in columns]}
    data_rows = [{"Data": [{"VarCharValue": str(value)} for value in row]} for row in rows]
    return [
        {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Name": column} for column in columns]},
                "Rows": [header, *data_rows],
            }
        }
    ]


def _analytics(state="SUCCEEDED", pages=None, states=None):
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "qid-1"}
    if states is None:
        states = [state]
    athena.get_query_execution.side_effect = [
        {"QueryExecution": {"Status": {"State": value, "StateChangeReason": "why"}}}
        for value in states
    ]
    paginator = MagicMock()
    paginator.paginate.return_value = pages or _athena_result_pages(
        ["namespace", "total_cost"], [["gco-jobs", "1.25"]]
    )
    athena.get_paginator.return_value = paginator
    config = GCOConfig(project_name="gco")
    with patch("cli.cost_analytics._load_cdk_json", create=True):
        analytics = CostAnalytics(
            config=config,
            athena_client=athena,
            region="us-east-2",
            poll_seconds=0.0,
            timeout_seconds=5.0,
        )
    return analytics, athena


class TestIdentifierLockstep:
    def test_database_and_workgroup_mirror_the_stack_constants(self):
        from gco.stacks.constants import (
            cost_athena_workgroup_name,
            cost_glue_database_name,
        )

        for project in ("gco", "my-project"):
            assert _database_name(project) == cost_glue_database_name(project)
            assert _workgroup_name(project) == cost_athena_workgroup_name(project)


class TestRunQuery:
    def test_successful_query_decodes_rows_and_skips_the_header(self):
        analytics, athena = _analytics()
        result = analytics.run_query("SELECT 1")
        assert isinstance(result, QueryResult)
        assert result.columns == ["namespace", "total_cost"]
        assert result.rows == [{"namespace": "gco-jobs", "total_cost": "1.25"}]
        start_kwargs = athena.start_query_execution.call_args.kwargs
        assert start_kwargs["WorkGroup"] == "gco-cost"
        assert start_kwargs["QueryExecutionContext"] == {"Database": "gco_cost"}
        assert "ExecutionParameters" not in start_kwargs

    def test_parameters_flow_through_execution_parameters(self):
        analytics, athena = _analytics()
        analytics.run_query("SELECT 1 WHERE x = ?", ["gco-jobs"])
        start_kwargs = athena.start_query_execution.call_args.kwargs
        assert start_kwargs["ExecutionParameters"] == ["gco-jobs"]

    def test_polls_through_transient_states(self):
        analytics, athena = _analytics(states=["QUEUED", "RUNNING", "SUCCEEDED"])
        result = analytics.run_query("SELECT 1")
        assert result.rows
        assert athena.get_query_execution.call_count == 3

    def test_failed_query_raises_with_the_reason(self):
        analytics, _ = _analytics(states=["FAILED"])
        with pytest.raises(AthenaQueryError, match="failed: why"):
            analytics.run_query("SELECT 1")

    def test_cancelled_query_raises(self):
        analytics, _ = _analytics(states=["CANCELLED"])
        with pytest.raises(AthenaQueryError, match="cancelled"):
            analytics.run_query("SELECT 1")

    def test_timeout_stops_the_query_and_raises(self):
        analytics, athena = _analytics(states=["RUNNING"] * 50)
        analytics.timeout_seconds = 0.0
        with pytest.raises(AthenaQueryError, match="timed out"):
            analytics.run_query("SELECT 1")
        athena.stop_query_execution.assert_called_once_with(QueryExecutionId="qid-1")

    def test_start_failure_names_workgroup_and_database(self):
        analytics, athena = _analytics()
        athena.start_query_execution.side_effect = RuntimeError("no workgroup")
        with pytest.raises(AthenaQueryError, match="gco-cost"):
            analytics.run_query("SELECT 1")


class TestCannedQueries:
    def test_cost_by_namespace_groups_and_orders(self):
        analytics, athena = _analytics()
        analytics.cost_by_namespace(days=7)
        sql = athena.start_query_execution.call_args.kwargs["QueryString"]
        assert 'FROM "gco_cost"."allocation_reports"' in sql
        assert "GROUP BY namespace" in sql
        assert "ORDER BY total_cost DESC" in sql
        assert "date >= date_format(date_add('day', -7" in sql

    def test_cost_by_namespace_region_filter_is_parameterized(self):
        analytics, athena = _analytics()
        analytics.cost_by_namespace(days=7, region="us-east-1")
        kwargs = athena.start_query_execution.call_args.kwargs
        assert "region = ?" in kwargs["QueryString"]
        assert kwargs["ExecutionParameters"] == ["us-east-1"]

    def test_cost_by_region_groups_by_region(self):
        analytics, athena = _analytics()
        analytics.cost_by_region(days=30)
        sql = athena.start_query_execution.call_args.kwargs["QueryString"]
        assert "GROUP BY region" in sql

    def test_cost_over_time_daily_and_hourly_buckets(self):
        analytics, athena = _analytics()
        analytics.cost_over_time(days=14, granularity="daily")
        assert (
            "SELECT date AS period"
            in (athena.start_query_execution.call_args.kwargs["QueryString"])
        )
        analytics_h, athena_h = _analytics()
        analytics_h.cost_over_time(days=2, granularity="hourly")
        assert (
            "date_format(window_start"
            in (athena_h.start_query_execution.call_args.kwargs["QueryString"])
        )

    def test_cost_over_time_namespace_filter_is_parameterized(self):
        analytics, athena = _analytics()
        analytics.cost_over_time(days=14, namespace="gco-jobs'; DROP TABLE x")
        kwargs = athena.start_query_execution.call_args.kwargs
        assert "namespace = ?" in kwargs["QueryString"]
        assert kwargs["ExecutionParameters"] == ["gco-jobs'; DROP TABLE x"]
        assert "DROP TABLE" not in kwargs["QueryString"]

    def test_cost_over_time_rejects_unknown_granularity(self):
        analytics, _ = _analytics()
        with pytest.raises(AthenaQueryError, match="granularity"):
            analytics.cost_over_time(granularity="weekly")

    def test_top_spenders_bounds_n_and_validates_grouping(self):
        analytics, athena = _analytics()
        analytics.top_spenders(n=5_000, by="region")
        sql = athena.start_query_execution.call_args.kwargs["QueryString"]
        assert "LIMIT 100" in sql
        assert "GROUP BY region" in sql
        with pytest.raises(AthenaQueryError, match="by must be"):
            analytics.top_spenders(by="pod'; DROP")

    def test_days_clause_is_bounded(self):
        analytics, athena = _analytics()
        analytics.cost_by_region(days=1_000_000)
        sql = athena.start_query_execution.call_args.kwargs["QueryString"]
        assert "-3650" in sql


class TestCostsK8sCommands:
    def _invoke(self, args, result=None):
        from cli.commands.costs_cmd import costs

        analytics = MagicMock()
        query_result = result or QueryResult(
            columns=["namespace", "total_cost"],
            rows=[{"namespace": "gco-jobs", "total_cost": "1.25"}],
            query_execution_id="qid-1",
        )
        analytics.cost_by_namespace.return_value = query_result
        analytics.cost_by_region.return_value = query_result
        analytics.cost_over_time.return_value = query_result
        analytics.top_spenders.return_value = query_result
        runner = CliRunner()
        with patch("cli.cost_analytics.get_cost_analytics", return_value=analytics):
            outcome = runner.invoke(costs, args)
        return outcome, analytics

    def test_namespaces_renders_the_table(self):
        outcome, analytics = self._invoke(["k8s", "namespaces", "--days", "3"])
        assert outcome.exit_code == 0
        assert "gco-jobs" in outcome.output
        analytics.cost_by_namespace.assert_called_once_with(days=3, region=None)

    def test_namespaces_passes_the_region_filter(self):
        outcome, analytics = self._invoke(["k8s", "namespaces", "--region", "us-east-1"])
        assert outcome.exit_code == 0
        analytics.cost_by_namespace.assert_called_once_with(days=7, region="us-east-1")

    def test_regions_command(self):
        outcome, analytics = self._invoke(["k8s", "regions", "--days", "30"])
        assert outcome.exit_code == 0
        analytics.cost_by_region.assert_called_once_with(days=30)

    def test_trend_command(self):
        outcome, analytics = self._invoke(
            ["k8s", "trend", "--granularity", "hourly", "--namespace", "gco-jobs"]
        )
        assert outcome.exit_code == 0
        analytics.cost_over_time.assert_called_once_with(
            days=14, granularity="hourly", namespace="gco-jobs"
        )

    def test_top_command(self):
        outcome, analytics = self._invoke(["k8s", "top", "-n", "5", "--by", "region"])
        assert outcome.exit_code == 0
        analytics.top_spenders.assert_called_once_with(n=5, by="region", days=7)

    def test_empty_result_prints_guidance(self):
        empty = QueryResult(columns=["namespace"], rows=[], query_execution_id="qid")
        outcome, _ = self._invoke(["k8s", "namespaces"], result=empty)
        assert outcome.exit_code == 0
        assert "No cost data" in outcome.output

    def test_query_failure_exits_nonzero(self):
        from cli.commands.costs_cmd import costs

        analytics = MagicMock()
        analytics.cost_by_namespace.side_effect = AthenaQueryError("no table")
        runner = CliRunner()
        with patch("cli.cost_analytics.get_cost_analytics", return_value=analytics):
            outcome = runner.invoke(costs, ["k8s", "namespaces"])
        assert outcome.exit_code == 1
        assert "no table" in outcome.output


class TestCostsReportCommands:
    def _invoke(self, args, api_result=None, api_error=None):
        from cli.commands.costs_cmd import costs

        aws_client = MagicMock()
        if api_error is not None:
            aws_client.call_api.side_effect = api_error
        else:
            aws_client.call_api.return_value = api_result or {}
        runner = CliRunner()
        with patch("cli.aws_client.get_aws_client", return_value=aws_client):
            outcome = runner.invoke(costs, args)
        return outcome, aws_client

    def test_generate_posts_the_window(self):
        outcome, aws_client = self._invoke(
            ["report", "generate", "--region", "us-east-1", "--window-hours", "48"],
            api_result={
                "bucket": "bucket-x",
                "region": "us-east-1",
                "report": {"s3_key": "adhoc/x.parquet", "row_count": 3},
            },
        )
        assert outcome.exit_code == 0
        assert "adhoc/x.parquet" in outcome.output
        kwargs = aws_client.call_api.call_args.kwargs
        assert kwargs["method"] == "POST"
        assert kwargs["path"] == "/api/v1/cost/reports"
        assert kwargs["region"] == "us-east-1"
        assert kwargs["body"] == {"window_hours": 48, "include_rows": False}

    def test_list_renders_report_rows(self):
        outcome, aws_client = self._invoke(
            ["report", "list", "-r", "us-east-1"],
            api_result={
                "region": "us-east-1",
                "count": 1,
                "reports": [
                    {
                        "key": "reports/region=us-east-1/date=2026-07-26/a.parquet",
                        "size_bytes": 1234,
                        "last_modified": "2026-07-26T10:00:00+00:00",
                    }
                ],
            },
        )
        assert outcome.exit_code == 0
        assert "a.parquet" in outcome.output
        kwargs = aws_client.call_api.call_args.kwargs
        assert kwargs["params"] == {"adhoc": "false", "limit": "20"}

    def test_status_renders_health_fields(self):
        outcome, _ = self._invoke(
            ["report", "status"],
            api_result={
                "region": "us-east-1",
                "opencost_healthy": True,
                "opencost_returning_data": True,
                "bucket": "bucket-x",
                "report_interval_minutes": 60,
                "last_scheduled_report": {
                    "s3_key": "reports/x.parquet",
                    "row_count": 4,
                    "total_cost": 1.5,
                },
            },
        )
        assert outcome.exit_code == 0
        assert "OpenCost healthy" in outcome.output
        assert "reports/x.parquet" in outcome.output

    def test_api_failure_exits_nonzero(self):
        outcome, _ = self._invoke(
            ["report", "status"], api_error=RuntimeError("API request failed: 503")
        )
        assert outcome.exit_code == 1
        assert "503" in outcome.output


class TestCostApiRegionResolution:
    def test_explicit_region_pins(self):
        from cli.commands.costs_cmd import _cost_api_region

        config = GCOConfig()
        assert _cost_api_region(config, "us-west-2") == "us-west-2"

    def test_global_transport_when_unpinned(self):
        from cli.commands.costs_cmd import _cost_api_region

        config = GCOConfig()
        config.use_regional_api = False
        assert _cost_api_region(config, None) is None

    def test_regional_mode_defaults_to_the_configured_region(self):
        from cli.commands.costs_cmd import _cost_api_region

        config = GCOConfig(default_region="eu-west-1")
        config.use_regional_api = True
        assert _cost_api_region(config, None) == "eu-west-1"


class TestCostsDashboardCommand:
    def _invoke(self, args, monkeypatch, *, public=True, captured=None):
        from cli.commands.costs_cmd import costs

        captured = captured if captured is not None else {}
        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", lambda c, r: None)
        monkeypatch.setattr(
            "cli.kubectl_helpers.describe_cluster_access",
            lambda c, r: {"public": public, "endpoint": "https://x.eks.amazonaws.com"},
        )
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, check=False: captured.__setitem__("cmd", cmd),
        )
        runner = CliRunner()
        outcome = runner.invoke(costs, args)
        return outcome, captured

    def test_grafana_dashboard_forwards_and_prints_the_direct_url(self, monkeypatch):
        outcome, captured = self._invoke(["dashboard", "--region", "us-east-1"], monkeypatch)
        assert outcome.exit_code == 0, outcome.output
        assert "/d/gco-cost/" in outcome.output
        assert "svc/kube-prometheus-stack-grafana" in captured["cmd"]
        assert "3000:80" in " ".join(captured["cmd"])

    def test_opencost_service_forwards_the_ui_port(self, monkeypatch):
        outcome, captured = self._invoke(
            ["dashboard", "--service", "opencost", "--region", "us-east-1"], monkeypatch
        )
        assert outcome.exit_code == 0, outcome.output
        assert "OpenCost UI" in outcome.output
        assert "svc/opencost" in captured["cmd"]
        # UI remote port 9090 binds a local default of 9091 (no Prometheus clash).
        assert "9091:9090" in " ".join(captured["cmd"])

    def test_local_port_override_is_honored(self, monkeypatch):
        outcome, captured = self._invoke(
            ["dashboard", "--region", "us-east-1", "--local-port", "4000"], monkeypatch
        )
        assert outcome.exit_code == 0, outcome.output
        assert "4000:80" in " ".join(captured["cmd"])

    def test_kubeconfig_failure_exits_nonzero(self, monkeypatch):
        from cli.commands.costs_cmd import costs

        def _boom(cluster, region):
            raise RuntimeError("no aws cli")

        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", _boom)
        runner = CliRunner()
        outcome = runner.invoke(costs, ["dashboard", "--region", "us-east-1"])
        assert outcome.exit_code == 1
        assert "no aws cli" in outcome.output

    def test_tunnel_failure_exits_nonzero(self, monkeypatch):
        from cli.commands.costs_cmd import costs

        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", lambda c, r: None)

        def _boom(*args, **kwargs):
            raise RuntimeError("tunnel exploded")

        monkeypatch.setattr("cli.cluster_tunnel.open_api_server_tunnel", _boom)
        runner = CliRunner()
        outcome = runner.invoke(costs, ["dashboard", "--region", "us-east-1"])
        assert outcome.exit_code == 1
        assert "tunnel exploded" in outcome.output


class TestJsonOutputModes:
    def test_k8s_namespaces_json_output_emits_columns_and_rows(self, monkeypatch):
        from cli.main import cli

        analytics = MagicMock()
        analytics.cost_by_namespace.return_value = QueryResult(
            columns=["namespace", "total_cost"],
            rows=[{"namespace": "gco-jobs", "total_cost": "1.25"}],
            query_execution_id="qid-1",
        )
        runner = CliRunner()
        with patch("cli.cost_analytics.get_cost_analytics", return_value=analytics):
            outcome = runner.invoke(cli, ["--output", "json", "costs", "k8s", "namespaces"])
        assert outcome.exit_code == 0, outcome.output
        assert '"columns"' in outcome.output
        assert '"gco-jobs"' in outcome.output

    def test_report_list_json_output_passes_through(self, monkeypatch):
        from cli.main import cli

        aws_client = MagicMock()
        aws_client.call_api.return_value = {"region": "us-east-1", "reports": []}
        runner = CliRunner()
        with patch("cli.aws_client.get_aws_client", return_value=aws_client):
            outcome = runner.invoke(cli, ["--output", "json", "costs", "report", "list"])
        assert outcome.exit_code == 0, outcome.output
        assert '"us-east-1"' in outcome.output

    def test_report_status_json_output_passes_through(self, monkeypatch):
        from cli.main import cli

        aws_client = MagicMock()
        aws_client.call_api.return_value = {"region": "us-east-1", "opencost_healthy": True}
        runner = CliRunner()
        with patch("cli.aws_client.get_aws_client", return_value=aws_client):
            outcome = runner.invoke(cli, ["--output", "json", "costs", "report", "status"])
        assert outcome.exit_code == 0, outcome.output
        assert '"opencost_healthy"' in outcome.output


class TestTableEdgeBranches:
    def test_report_list_empty_prints_guidance(self):
        from cli.commands.costs_cmd import costs

        aws_client = MagicMock()
        aws_client.call_api.return_value = {"region": "us-east-1", "reports": []}
        runner = CliRunner()
        with patch("cli.aws_client.get_aws_client", return_value=aws_client):
            outcome = runner.invoke(costs, ["report", "list"])
        assert outcome.exit_code == 0
        assert "No reports found yet" in outcome.output

    def test_report_status_renders_last_error(self):
        from cli.commands.costs_cmd import costs

        aws_client = MagicMock()
        aws_client.call_api.return_value = {
            "region": "us-east-1",
            "opencost_healthy": False,
            "opencost_returning_data": False,
            "bucket": "bucket-x",
            "report_interval_minutes": 60,
            "last_scheduled_report": None,
            "last_error": "OpenCost request failed",
        }
        runner = CliRunner()
        with patch("cli.aws_client.get_aws_client", return_value=aws_client):
            outcome = runner.invoke(costs, ["report", "status"])
        assert outcome.exit_code == 0
        assert "OpenCost request failed" in outcome.output


class TestMonitoringRegionResolution:
    def test_reads_the_monitoring_region_from_cdk_json(self):
        with (
            patch(
                "cli.config._load_cdk_json",
                return_value={"monitoring": "eu-central-1", "regional": ["us-east-1"]},
            ),
            patch("cli.cost_analytics.boto3.client") as mock_client,
        ):
            analytics = CostAnalytics(config=GCOConfig(project_name="gco"))
        assert analytics.region == "eu-central-1"
        mock_client.assert_called_once_with("athena", region_name="eu-central-1")

    def test_falls_back_to_the_default_region(self):
        with (
            patch("cli.config._load_cdk_json", return_value={}),
            patch("cli.cost_analytics.boto3.client"),
        ):
            analytics = CostAnalytics(config=GCOConfig(default_region="ap-southeast-2"))
        assert analytics.region == "ap-southeast-2"
