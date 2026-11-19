"""
Tests for the cost-visibility feature in cli/costs.py.

Exercises CostTracker and its factory, the get_cost_summary path
against a mocked Cost Explorer client (happy path with multiple
services plus zero-cost filtering), and the supporting CostSummary
/ResourceCost/WorkloadCost dataclasses. Also hits the `gco costs`
CLI subgroup via CliRunner to confirm the command wiring surfaces
summaries, workloads, and forecasts as expected.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.config import GCOConfig
from cli.costs import CostSummary, CostTracker, ResourceCost, WorkloadCost, get_cost_tracker

# =============================================================================
# Unit Tests - CostTracker
# =============================================================================


class TestCostTracker:
    """Tests for CostTracker class."""

    def test_init_default(self):
        tracker = CostTracker()
        assert tracker._config is None
        assert tracker._pricing_cache == {}

    def test_init_with_config(self):
        config = MagicMock(spec=GCOConfig)
        tracker = CostTracker(config=config)
        assert tracker._config is config

    def test_factory_function(self):
        tracker = get_cost_tracker()
        assert isinstance(tracker, CostTracker)

    def test_factory_function_with_config(self):
        config = MagicMock(spec=GCOConfig)
        tracker = get_cost_tracker(config)
        assert tracker._config is config


class TestGetCostSummary:
    """Tests for get_cost_summary method."""

    @patch("cli.costs.boto3.Session")
    def test_returns_summary_with_services(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "Groups": [
                        {
                            "Keys": ["Amazon Elastic Compute Cloud - Compute"],
                            "Metrics": {"UnblendedCost": {"Amount": "150.50"}},
                        },
                        {
                            "Keys": ["Amazon Elastic Kubernetes Service"],
                            "Metrics": {"UnblendedCost": {"Amount": "25.00"}},
                        },
                        {
                            "Keys": ["Amazon Simple Storage Service"],
                            "Metrics": {"UnblendedCost": {"Amount": "3.75"}},
                        },
                    ]
                }
            ]
        }

        tracker = CostTracker()
        summary = tracker.get_cost_summary(days=30)

        assert isinstance(summary, CostSummary)
        assert summary.total == pytest.approx(179.25)
        assert summary.currency == "USD"
        assert len(summary.by_service) == 3
        # Should be sorted descending
        assert summary.by_service[0].amount == pytest.approx(150.50)
        assert summary.by_service[0].service == "Amazon Elastic Compute Cloud - Compute"

    @patch("cli.costs.boto3.Session")
    def test_filters_zero_cost_services(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "Groups": [
                        {
                            "Keys": ["EC2"],
                            "Metrics": {"UnblendedCost": {"Amount": "10.00"}},
                        },
                        {
                            "Keys": ["CloudWatch"],
                            "Metrics": {"UnblendedCost": {"Amount": "0.0001"}},
                        },
                    ]
                }
            ]
        }

        tracker = CostTracker()
        summary = tracker.get_cost_summary(days=7)

        assert len(summary.by_service) == 1
        assert summary.by_service[0].service == "EC2"

    @patch("cli.costs.boto3.Session")
    def test_empty_results(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

        tracker = CostTracker()
        summary = tracker.get_cost_summary(days=30)

        assert summary.total == 0.0
        assert summary.by_service == []

    @patch("cli.costs.boto3.Session")
    def test_api_error_raises(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.side_effect = Exception("AccessDenied")

        tracker = CostTracker()
        with pytest.raises(RuntimeError, match="Cost Explorer query failed"):
            tracker.get_cost_summary()

    @patch("cli.costs.boto3.Session")
    def test_custom_days_parameter(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

        tracker = CostTracker()
        tracker.get_cost_summary(days=7)

        call_args = mock_ce.get_cost_and_usage.call_args
        call_args[1]["TimePeriod"] if "TimePeriod" in call_args[1] else call_args[0][0]
        # Verify the filter uses the GCO tag
        filter_arg = call_args[1].get("Filter", {})
        assert filter_arg["Tags"]["Key"] == "Project"
        assert filter_arg["Tags"]["Values"] == ["GCO"]

    @patch("cli.costs.boto3.Session")
    def test_multiple_time_periods(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "Groups": [
                        {"Keys": ["EC2"], "Metrics": {"UnblendedCost": {"Amount": "50.00"}}},
                    ]
                },
                {
                    "Groups": [
                        {"Keys": ["EC2"], "Metrics": {"UnblendedCost": {"Amount": "75.00"}}},
                    ]
                },
            ]
        }

        tracker = CostTracker()
        summary = tracker.get_cost_summary(days=60, granularity="MONTHLY")

        # Both periods should be summed
        assert summary.total == pytest.approx(125.00)
        assert [(s.service, s.amount) for s in summary.by_service] == [
            ("EC2", pytest.approx(125.00))
        ]


def _two_month_service_groups() -> dict:
    """GetCostAndUsage answer for a 30-day window that crosses a month boundary."""
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-10-20", "End": "2026-11-01"},
                "Groups": [
                    {
                        "Keys": ["Amazon Elastic Compute Cloud - Compute"],
                        "Metrics": {"UnblendedCost": {"Amount": "200.00"}},
                    },
                    {
                        "Keys": ["Amazon Simple Storage Service"],
                        "Metrics": {"UnblendedCost": {"Amount": "6.00"}},
                    },
                    {
                        "Keys": ["AWS Key Management Service"],
                        "Metrics": {"UnblendedCost": {"Amount": "0.0006"}},
                    },
                ],
            },
            {
                "TimePeriod": {"Start": "2026-11-01", "End": "2026-11-19"},
                "Groups": [
                    {
                        "Keys": ["Amazon Simple Storage Service"],
                        "Metrics": {"UnblendedCost": {"Amount": "4.00"}},
                    },
                    {
                        "Keys": ["Amazon Elastic Compute Cloud - Compute"],
                        "Metrics": {"UnblendedCost": {"Amount": "120.00"}},
                    },
                    {
                        "Keys": ["AWS Key Management Service"],
                        "Metrics": {"UnblendedCost": {"Amount": "0.0006"}},
                    },
                ],
            },
        ]
    }


class TestCostSummaryAcrossMonths:
    """A MONTHLY query returns one ResultsByTime entry per calendar month in the
    window. The summary listed each service once per month with partial amounts
    (N35); it must list each service once, with its total for the window."""

    @patch("cli.costs.boto3.Session")
    def test_each_service_is_listed_once_with_its_window_total(self, mock_session_cls):
        mock_ce = mock_session_cls.return_value.client.return_value
        mock_ce.get_cost_and_usage.return_value = _two_month_service_groups()

        summary = CostTracker().get_cost_summary(days=30)

        assert [(s.service, s.amount) for s in summary.by_service] == [
            ("Amazon Elastic Compute Cloud - Compute", pytest.approx(320.00)),
            ("Amazon Simple Storage Service", pytest.approx(10.00)),
            # 0.0006 a month is below the display floor; the window total is not.
            ("AWS Key Management Service", pytest.approx(0.0012)),
        ]
        assert summary.total == pytest.approx(330.0012)

    @patch("cli.costs.boto3.Session")
    def test_json_by_service_has_one_row_per_service(self, mock_session_cls):
        from cli.main import cli

        mock_ce = mock_session_cls.return_value.client.return_value
        mock_ce.get_cost_and_usage.return_value = _two_month_service_groups()

        result = CliRunner().invoke(cli, ["--output", "json", "costs", "summary"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        services = [row["service"] for row in data["by_service"]]
        assert services == sorted(set(services), key=services.index)
        assert len(services) == 3
        assert data["by_service"][0]["amount"] == pytest.approx(320.00)

    @patch("cli.costs.boto3.Session")
    def test_table_lists_each_service_once(self, mock_session_cls):
        from cli.main import cli

        mock_ce = mock_session_cls.return_value.client.return_value
        mock_ce.get_cost_and_usage.return_value = _two_month_service_groups()

        result = CliRunner().invoke(cli, ["costs", "summary"])

        assert result.exit_code == 0, result.output
        assert result.output.count("Amazon Elastic Compute Cloud - Compute") == 1
        assert "320.00" in result.output
        assert result.output.count("Amazon Simple Storage Service") == 1


class TestGetCostByRegion:
    """Tests for get_cost_by_region method."""

    @patch("cli.costs.boto3.Session")
    def test_returns_region_breakdown(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "Groups": [
                        {"Keys": ["us-east-1"], "Metrics": {"UnblendedCost": {"Amount": "100.00"}}},
                        {"Keys": ["us-east-2"], "Metrics": {"UnblendedCost": {"Amount": "50.00"}}},
                        {"Keys": ["eu-west-1"], "Metrics": {"UnblendedCost": {"Amount": "25.00"}}},
                    ]
                }
            ]
        }

        tracker = CostTracker()
        by_region = tracker.get_cost_by_region(days=30)

        assert by_region["us-east-1"] == pytest.approx(100.00)
        assert by_region["us-east-2"] == pytest.approx(50.00)
        # Should be sorted descending
        regions = list(by_region.keys())
        assert regions[0] == "us-east-1"

    @patch("cli.costs.boto3.Session")
    def test_filters_zero_cost_regions(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "Groups": [
                        {"Keys": ["us-east-1"], "Metrics": {"UnblendedCost": {"Amount": "10.00"}}},
                        {"Keys": ["us-west-2"], "Metrics": {"UnblendedCost": {"Amount": "0.0001"}}},
                    ]
                }
            ]
        }

        tracker = CostTracker()
        by_region = tracker.get_cost_by_region()

        assert len(by_region) == 1
        assert "us-east-1" in by_region

    @patch("cli.costs.boto3.Session")
    def test_api_error_raises(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.side_effect = Exception("Forbidden")

        tracker = CostTracker()
        with pytest.raises(RuntimeError, match="Cost Explorer query failed"):
            tracker.get_cost_by_region()


class TestGetDailyTrend:
    """Tests for get_daily_trend method."""

    @patch("cli.costs.boto3.Session")
    def test_returns_daily_data(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-03-01"},
                    "Total": {"UnblendedCost": {"Amount": "12.50"}},
                },
                {
                    "TimePeriod": {"Start": "2026-03-02"},
                    "Total": {"UnblendedCost": {"Amount": "15.75"}},
                },
            ]
        }

        tracker = CostTracker()
        trend = tracker.get_daily_trend(days=14)

        assert len(trend) == 2
        assert trend[0]["date"] == "2026-03-01"
        assert trend[0]["amount"] == pytest.approx(12.50)
        assert trend[1]["date"] == "2026-03-02"
        assert trend[1]["amount"] == pytest.approx(15.75)

    @patch("cli.costs.boto3.Session")
    def test_empty_trend(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

        tracker = CostTracker()
        trend = tracker.get_daily_trend()

        assert trend == []

    @patch("cli.costs.boto3.Session")
    def test_api_error_raises(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.side_effect = Exception("Timeout")

        tracker = CostTracker()
        with pytest.raises(RuntimeError):
            tracker.get_daily_trend()


class TestGetForecast:
    """Tests for get_forecast method."""

    @patch("cli.costs.boto3.Session")
    def test_returns_forecast(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_forecast.return_value = {
            "Total": {"Amount": "450.00"},
        }

        tracker = CostTracker()
        forecast = tracker.get_forecast(days_ahead=30)

        assert forecast["forecast_total"] == pytest.approx(450.00)
        assert "period_start" in forecast
        assert "period_end" in forecast

    @patch("cli.costs.boto3.Session")
    def test_forecast_error_returns_error_dict(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_forecast.side_effect = Exception("Not enough data")

        tracker = CostTracker()
        with pytest.raises(RuntimeError, match="Cost Explorer forecast failed: Not enough data"):
            tracker.get_forecast()


def _ce_client_error(code: str, message: str, operation: str = "GetCostForecast"):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


class TestForecastFailures:
    """`gco costs forecast` printed the error, a "needs 14+ days" hint for any
    failure, and exited 0; in JSON mode stdout was {"status": "ok"} (N36). A
    failure now exits 1 in every output mode, and the hint is shown only when
    Cost Explorer says it lacks the data."""

    @patch("cli.costs.boto3.Session")
    def test_get_forecast_raises_on_access_denied(self, mock_session_cls):
        from cli.costs import CostDataUnavailableError

        mock_ce = mock_session_cls.return_value.client.return_value
        mock_ce.get_cost_forecast.side_effect = _ce_client_error(
            "AccessDeniedException", "not authorized to perform ce:GetCostForecast"
        )

        with pytest.raises(RuntimeError, match="AccessDeniedException") as raised:
            CostTracker().get_forecast(days_ahead=30)
        assert not isinstance(raised.value, CostDataUnavailableError)

    @patch("cli.costs.boto3.Session")
    def test_get_forecast_names_missing_history(self, mock_session_cls):
        from cli.costs import CostDataUnavailableError

        mock_ce = mock_session_cls.return_value.client.return_value
        mock_ce.get_cost_forecast.side_effect = _ce_client_error(
            "DataUnavailableException", "Insufficient amount of historical data"
        )

        with pytest.raises(CostDataUnavailableError, match="Insufficient amount"):
            CostTracker().get_forecast(days_ahead=30)

    @pytest.mark.parametrize("output_format", ["table", "json", "yaml"])
    @patch("cli.costs.boto3.Session")
    def test_access_denied_exits_1_in_every_output_mode(self, mock_session_cls, output_format):
        from cli.main import cli

        mock_ce = mock_session_cls.return_value.client.return_value
        mock_ce.get_cost_forecast.side_effect = _ce_client_error(
            "AccessDeniedException", "not authorized to perform ce:GetCostForecast"
        )

        result = CliRunner().invoke(cli, ["--output", output_format, "costs", "forecast"])

        assert result.exit_code == 1, result.output
        assert "AccessDeniedException" in result.stderr
        assert "status" not in result.stdout
        # The missing-history hint is not offered for an unrelated failure.
        assert "14+ days" not in result.output

    @patch("cli.costs.boto3.Session")
    def test_missing_history_exits_1_with_the_hint(self, mock_session_cls):
        from cli.main import cli

        mock_ce = mock_session_cls.return_value.client.return_value
        mock_ce.get_cost_forecast.side_effect = _ce_client_error(
            "DataUnavailableException", "Insufficient amount of historical data"
        )

        result = CliRunner().invoke(cli, ["costs", "forecast"])

        assert result.exit_code == 1, result.output
        assert "Forecast unavailable" in result.stderr
        assert "Cost Explorer needs 14+ days" in result.stdout

    @pytest.mark.parametrize("command", ["summary", "regions", "trend", "forecast"])
    @pytest.mark.parametrize("days", ["0", "-3"])
    @patch("cli.costs.boto3.Session")
    def test_a_window_below_one_day_is_a_usage_error(self, mock_session_cls, command, days):
        from cli.main import cli

        result = CliRunner().invoke(cli, ["costs", command, "--days", days])

        assert result.exit_code == 2, result.output
        assert "--days" in result.stderr
        mock_session_cls.return_value.client.assert_not_called()

    @pytest.mark.parametrize("command", ["namespaces", "regions", "trend", "top"])
    def test_k8s_window_below_one_day_is_a_usage_error(self, command):
        from cli.main import cli

        with patch("cli.cost_analytics.get_cost_analytics") as factory:
            result = CliRunner().invoke(cli, ["costs", "k8s", command, "--days", "0"])

        assert result.exit_code == 2, result.output
        factory.assert_not_called()


class TestEstimateRunningWorkloads:
    """Tests for estimate_running_workloads method."""

    @patch("cli.costs.boto3.Session")
    def test_returns_empty_on_no_cluster(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        with patch("cli.costs.CostTracker.estimate_running_workloads", return_value=[]):
            tracker = CostTracker()
            workloads = tracker.estimate_running_workloads("us-east-1")

        assert workloads == []

    @patch("cli.costs.boto3.Session")
    def test_raises_on_kubectl_error(self, mock_session_cls):
        from cli.costs import WorkloadReadError

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        with patch("cli.kubectl_helpers.update_kubeconfig", side_effect=Exception("No cluster")):
            tracker = CostTracker()
            with pytest.raises(WorkloadReadError, match="No cluster") as raised:
                tracker.estimate_running_workloads("us-east-1")

        assert raised.value.workloads == []


# =============================================================================
# Data Model Tests
# =============================================================================


class TestResourceCost:
    """Tests for ResourceCost dataclass."""

    def test_defaults(self):
        rc = ResourceCost(service="EC2", amount=100.0)
        assert rc.currency == "USD"
        assert rc.region is None
        assert rc.detail is None

    def test_all_fields(self):
        rc = ResourceCost(
            service="EKS", amount=25.0, currency="USD", region="us-east-1", detail="Cluster hours"
        )
        assert rc.service == "EKS"
        assert rc.amount == 25.0
        assert rc.region == "us-east-1"


class TestCostSummary:
    """Tests for CostSummary dataclass."""

    def test_defaults(self):
        cs = CostSummary(total=0.0)
        assert cs.currency == "USD"
        assert cs.by_service == []
        assert cs.by_region == {}

    def test_with_services(self):
        cs = CostSummary(
            total=100.0,
            period_start="2026-01-01",
            period_end="2026-01-31",
            by_service=[ResourceCost(service="EC2", amount=100.0)],
        )
        assert len(cs.by_service) == 1


class TestWorkloadCost:
    """Tests for WorkloadCost dataclass."""

    def test_all_fields(self):
        wc = WorkloadCost(
            name="vllm-demo-abc123",
            workload_type="inference",
            instance_type="g4dn.xlarge",
            gpu_count=1,
            hourly_rate=0.526,
            runtime_hours=24.5,
            estimated_cost=12.887,
            region="us-east-1",
            status="Running",
        )
        assert wc.name == "vllm-demo-abc123"
        assert wc.workload_type == "inference"
        assert wc.gpu_count == 1
        assert wc.hourly_rate == pytest.approx(0.526)


# =============================================================================
# CLI Command Tests
# =============================================================================


class TestCostsCLI:
    """Tests for the costs CLI commands."""

    def test_costs_help(self):
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "--help"])
        assert result.exit_code == 0
        assert "summary" in result.output
        assert "regions" in result.output
        assert "trend" in result.output
        assert "workloads" in result.output
        assert "forecast" in result.output

    @patch("cli.costs.boto3.Session")
    def test_costs_summary_table(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "Groups": [
                        {"Keys": ["EC2"], "Metrics": {"UnblendedCost": {"Amount": "100.00"}}},
                        {"Keys": ["EKS"], "Metrics": {"UnblendedCost": {"Amount": "25.00"}}},
                    ]
                }
            ]
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "summary", "--days", "7"])
        assert result.exit_code == 0
        assert "EC2" in result.output
        assert "EKS" in result.output
        assert "TOTAL" in result.output
        assert "$" in result.output

    @patch("cli.costs.boto3.Session")
    def test_costs_summary_empty(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "summary"])
        assert result.exit_code == 0
        assert "0.00" in result.output

    @patch("cli.costs.boto3.Session")
    def test_costs_regions_table(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "Groups": [
                        {"Keys": ["us-east-1"], "Metrics": {"UnblendedCost": {"Amount": "80.00"}}},
                        {"Keys": ["us-east-2"], "Metrics": {"UnblendedCost": {"Amount": "20.00"}}},
                    ]
                }
            ]
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "regions"])
        assert result.exit_code == 0
        assert "us-east-1" in result.output
        assert "us-east-2" in result.output
        assert "80%" in result.output or "80.00" in result.output

    @patch("cli.costs.boto3.Session")
    def test_costs_trend_table(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-03-01"},
                    "Total": {"UnblendedCost": {"Amount": "10.00"}},
                },
                {
                    "TimePeriod": {"Start": "2026-03-02"},
                    "Total": {"UnblendedCost": {"Amount": "20.00"}},
                },
            ]
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "trend", "--days", "7"])
        assert result.exit_code == 0
        assert "2026-03-01" in result.output
        assert "2026-03-02" in result.output
        assert "█" in result.output
        assert "Avg/day" in result.output

    @patch("cli.costs.boto3.Session")
    def test_costs_forecast_success(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_forecast.return_value = {
            "Total": {"Amount": "300.00"},
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "forecast"])
        assert result.exit_code == 0
        assert "300.00" in result.output
        assert "Projected spend" in result.output

    @patch("cli.costs.boto3.Session")
    def test_costs_forecast_not_enough_data(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_forecast.side_effect = Exception("Not enough data points")

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "forecast"])
        assert result.exit_code == 1
        assert "Failed to get forecast" in result.stderr
        assert "not enough data points" in result.output.lower()

    @patch("cli.costs.CostTracker.estimate_running_workloads", return_value=[])
    @patch("cli.costs.boto3.Session")
    def test_costs_workloads_empty(self, mock_session_cls, mock_estimate):
        from cli.main import cli

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "workloads", "-r", "us-east-1"])
        assert result.exit_code == 0
        assert "No running workloads" in result.output

    @patch("cli.costs.boto3.Session")
    def test_costs_summary_error_handling(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.side_effect = Exception("AccessDenied")

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "summary"])
        assert result.exit_code == 1
        assert "Failed" in result.output


class TestCostsJSONOutput:
    """Tests for JSON output format."""

    @patch("cli.costs.boto3.Session")
    def test_summary_json(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "Groups": [
                        {"Keys": ["EC2"], "Metrics": {"UnblendedCost": {"Amount": "50.00"}}},
                    ]
                }
            ]
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["--output", "json", "costs", "summary"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total"] == pytest.approx(50.00)
        assert len(data["by_service"]) == 1

    @patch("cli.costs.boto3.Session")
    def test_regions_json(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "Groups": [
                        {"Keys": ["us-east-1"], "Metrics": {"UnblendedCost": {"Amount": "75.00"}}},
                    ]
                }
            ]
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["--output", "json", "costs", "regions"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["us-east-1"] == pytest.approx(75.00)

    @patch("cli.costs.boto3.Session")
    def test_trend_json(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-03-01"},
                    "Total": {"UnblendedCost": {"Amount": "10.00"}},
                },
            ]
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["--output", "json", "costs", "trend"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["date"] == "2026-03-01"


# =============================================================================
# Extended Unit Tests - Edge Cases and Coverage
# =============================================================================


class TestCostSummaryGranularity:
    """Tests for different granularity options."""

    @patch("cli.costs.boto3.Session")
    def test_daily_granularity(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

        tracker = CostTracker()
        tracker.get_cost_summary(days=7, granularity="DAILY")

        call_args = mock_ce.get_cost_and_usage.call_args
        assert call_args[1]["Granularity"] == "DAILY"

    @patch("cli.costs.boto3.Session")
    def test_monthly_granularity(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

        tracker = CostTracker()
        tracker.get_cost_summary(days=90, granularity="MONTHLY")

        call_args = mock_ce.get_cost_and_usage.call_args
        assert call_args[1]["Granularity"] == "MONTHLY"


class TestCostSummaryPeriod:
    """Tests for time period calculation."""

    @patch("cli.costs.boto3.Session")
    def test_period_dates_are_set(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

        tracker = CostTracker()
        summary = tracker.get_cost_summary(days=30)

        assert summary.period_start != ""
        assert summary.period_end != ""
        # Start should be before end
        assert summary.period_start < summary.period_end

    @patch("cli.costs.boto3.Session")
    def test_one_day_period(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

        tracker = CostTracker()
        summary = tracker.get_cost_summary(days=1)

        assert summary.period_start != summary.period_end


class TestCostByRegionAggregation:
    """Tests for region cost aggregation across time periods."""

    @patch("cli.costs.boto3.Session")
    def test_aggregates_across_periods(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "Groups": [
                        {"Keys": ["us-east-1"], "Metrics": {"UnblendedCost": {"Amount": "50.00"}}},
                    ]
                },
                {
                    "Groups": [
                        {"Keys": ["us-east-1"], "Metrics": {"UnblendedCost": {"Amount": "30.00"}}},
                    ]
                },
            ]
        }

        tracker = CostTracker()
        by_region = tracker.get_cost_by_region(days=60)

        assert by_region["us-east-1"] == pytest.approx(80.00)

    @patch("cli.costs.boto3.Session")
    def test_empty_groups(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": [{"Groups": []}]}

        tracker = CostTracker()
        by_region = tracker.get_cost_by_region()

        assert by_region == {}


class TestDailyTrendEdgeCases:
    """Edge case tests for daily trend."""

    @patch("cli.costs.boto3.Session")
    def test_single_day(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-03-09"},
                    "Total": {"UnblendedCost": {"Amount": "5.00"}},
                },
            ]
        }

        tracker = CostTracker()
        trend = tracker.get_daily_trend(days=1)

        assert len(trend) == 1
        assert trend[0]["amount"] == pytest.approx(5.00)

    @patch("cli.costs.boto3.Session")
    def test_zero_cost_days_included(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-03-01"},
                    "Total": {"UnblendedCost": {"Amount": "0.00"}},
                },
                {
                    "TimePeriod": {"Start": "2026-03-02"},
                    "Total": {"UnblendedCost": {"Amount": "10.00"}},
                },
            ]
        }

        tracker = CostTracker()
        trend = tracker.get_daily_trend(days=2)

        assert len(trend) == 2
        assert trend[0]["amount"] == pytest.approx(0.00)


class TestForecastEdgeCases:
    """Edge case tests for forecast."""

    @patch("cli.costs.boto3.Session")
    def test_forecast_custom_days(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_forecast.return_value = {
            "Total": {"Amount": "900.00"},
        }

        tracker = CostTracker()
        forecast = tracker.get_forecast(days_ahead=60)

        assert forecast["forecast_total"] == pytest.approx(900.00)

    @patch("cli.costs.boto3.Session")
    def test_forecast_missing_total(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_forecast.return_value = {}

        tracker = CostTracker()
        forecast = tracker.get_forecast()

        assert forecast["forecast_total"] == pytest.approx(0.0)


class TestWorkloadCostEstimation:
    """Tests for workload cost estimation edge cases."""

    def test_workload_cost_zero_runtime(self):
        wc = WorkloadCost(
            name="test-pod",
            workload_type="job",
            instance_type="m5.xlarge",
            gpu_count=0,
            hourly_rate=0.192,
            runtime_hours=0.0,
            estimated_cost=0.0,
            region="us-east-1",
            status="Pending",
        )
        assert wc.estimated_cost == 0.0

    def test_workload_cost_long_running(self):
        wc = WorkloadCost(
            name="inference-endpoint",
            workload_type="inference",
            instance_type="g4dn.xlarge",
            gpu_count=1,
            hourly_rate=0.526,
            runtime_hours=720.0,  # 30 days
            estimated_cost=378.72,
            region="us-east-1",
            status="Running",
        )
        assert wc.estimated_cost == pytest.approx(378.72)
        assert wc.runtime_hours == 720.0

    def test_workload_cost_multi_gpu(self):
        wc = WorkloadCost(
            name="training-job",
            workload_type="job",
            instance_type="p3.8xlarge",
            gpu_count=4,
            hourly_rate=12.24,
            runtime_hours=8.5,
            estimated_cost=104.04,
            region="us-west-2",
            status="Running",
        )
        assert wc.gpu_count == 4


class TestCLIEdgeCases:
    """Additional CLI edge case tests."""

    @patch("cli.costs.boto3.Session")
    def test_costs_trend_all_zero(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-03-01"},
                    "Total": {"UnblendedCost": {"Amount": "0.00"}},
                },
                {
                    "TimePeriod": {"Start": "2026-03-02"},
                    "Total": {"UnblendedCost": {"Amount": "0.00"}},
                },
            ]
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "trend"])
        assert result.exit_code == 0
        assert "0.00" in result.output

    @patch("cli.costs.boto3.Session")
    def test_costs_regions_error(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.side_effect = Exception("Throttled")

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "regions"])
        assert result.exit_code == 1
        assert "Failed" in result.output

    @patch("cli.costs.boto3.Session")
    def test_costs_trend_error(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.side_effect = Exception("ServiceUnavailable")

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "trend"])
        assert result.exit_code == 1
        assert "Failed" in result.output

    @patch("cli.costs.boto3.Session")
    def test_costs_summary_large_dataset(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        # Simulate many services
        groups = [
            {"Keys": [f"Service-{i}"], "Metrics": {"UnblendedCost": {"Amount": str(i * 10.5)}}}
            for i in range(1, 21)
        ]
        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": [{"Groups": groups}]}

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "summary"])
        assert result.exit_code == 0
        assert "TOTAL" in result.output
        # First service in sorted order should be the most expensive
        assert "Service-20" in result.output

    @patch("cli.costs.boto3.Session")
    def test_costs_forecast_custom_days(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_forecast.return_value = {
            "Total": {"Amount": "600.00"},
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "forecast", "--days", "60"])
        assert result.exit_code == 0
        assert "600.00" in result.output
        assert "Daily average" in result.output

    @patch("cli.costs.boto3.Session")
    def test_costs_regions_percentage_calculation(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "Groups": [
                        {"Keys": ["us-east-1"], "Metrics": {"UnblendedCost": {"Amount": "75.00"}}},
                        {"Keys": ["us-west-2"], "Metrics": {"UnblendedCost": {"Amount": "25.00"}}},
                    ]
                }
            ]
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "regions"])
        assert result.exit_code == 0
        assert "75%" in result.output
        assert "25%" in result.output

    @patch("cli.costs.CostTracker.estimate_running_workloads", return_value=[])
    @patch("cli.costs.boto3.Session")
    def test_costs_workloads_json_output(self, mock_session_cls, mock_estimate):
        from cli.main import cli

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        runner = CliRunner()
        result = runner.invoke(cli, ["--output", "json", "costs", "workloads", "-r", "us-east-1"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 0  # No workloads running


class TestCostTrackerTagFilter:
    """Tests to verify the GCO tag filter is always applied."""

    @patch("cli.costs.boto3.Session")
    def test_summary_uses_project_tag(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

        tracker = CostTracker()
        tracker.get_cost_summary()

        call_args = mock_ce.get_cost_and_usage.call_args[1]
        assert call_args["Filter"]["Tags"]["Key"] == "Project"
        assert call_args["Filter"]["Tags"]["Values"] == ["GCO"]

    @patch("cli.costs.boto3.Session")
    def test_regions_uses_project_tag(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

        tracker = CostTracker()
        tracker.get_cost_by_region()

        call_args = mock_ce.get_cost_and_usage.call_args[1]
        assert call_args["Filter"]["Tags"]["Key"] == "Project"
        assert call_args["Filter"]["Tags"]["Values"] == ["GCO"]

    @patch("cli.costs.boto3.Session")
    def test_trend_uses_project_tag(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

        tracker = CostTracker()
        tracker.get_daily_trend()

        call_args = mock_ce.get_cost_and_usage.call_args[1]
        assert call_args["Filter"]["Tags"]["Key"] == "Project"
        assert call_args["Filter"]["Tags"]["Values"] == ["GCO"]

    @patch("cli.costs.boto3.Session")
    def test_forecast_uses_project_tag(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_forecast.return_value = {"Total": {"Amount": "0"}}

        tracker = CostTracker()
        tracker.get_forecast()

        call_args = mock_ce.get_cost_forecast.call_args[1]
        assert call_args["Filter"]["Tags"]["Key"] == "Project"
        assert call_args["Filter"]["Tags"]["Values"] == ["GCO"]

    @patch("cli.costs.boto3.Session")
    def test_ce_client_uses_us_east_1(self, mock_session_cls):
        """Cost Explorer API is only available in us-east-1."""
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

        tracker = CostTracker()
        tracker.get_cost_summary()

        mock_session.client.assert_called_with("ce", region_name="us-east-1")


def _write_cdk_json(directory, context: dict) -> None:
    (directory / "cdk.json").write_text(json.dumps({"app": "python3 app.py", "context": context}))


def _tag_filters_of_every_query(mock_ce) -> list:
    tracker = CostTracker()
    mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}
    mock_ce.get_cost_forecast.return_value = {"Total": {"Amount": "0"}}
    tracker.get_cost_summary()
    tracker.get_cost_by_region()
    tracker.get_daily_trend()
    tracker.get_forecast()
    calls = mock_ce.get_cost_and_usage.call_args_list + mock_ce.get_cost_forecast.call_args_list
    return [call.kwargs["Filter"]["Tags"] for call in calls]


def _group(key: str, amount: str) -> dict:
    return {"Keys": [key], "Metrics": {"UnblendedCost": {"Amount": amount}}}


def _day(date: str, amount: str) -> dict:
    return {"TimePeriod": {"Start": date}, "Total": {"UnblendedCost": {"Amount": amount}}}


class TestCostExplorerPagination:
    """GetCostAndUsage returns NextPageToken when a result exceeds one page;
    the summary, region and trend reads used only the first page (CC23)."""

    @staticmethod
    def _pages(mock_session_cls, *pages):
        mock_ce = mock_session_cls.return_value.client.return_value
        mock_ce.get_cost_and_usage.side_effect = list(pages)
        return mock_ce

    @staticmethod
    def _tokens(mock_ce) -> list:
        return [
            call.kwargs.get("NextPageToken") for call in mock_ce.get_cost_and_usage.call_args_list
        ]

    @patch("cli.costs.boto3.Session")
    def test_summary_reads_every_page(self, mock_session_cls):
        period = {"Start": "2026-11-01", "End": "2026-11-19"}
        mock_ce = self._pages(
            mock_session_cls,
            {
                "ResultsByTime": [{"TimePeriod": period, "Groups": [_group("EC2", "100.00")]}],
                "NextPageToken": "page-2",
            },
            {
                "ResultsByTime": [
                    {
                        "TimePeriod": period,
                        "Groups": [_group("EC2", "50.00"), _group("S3", "5.00")],
                    }
                ]
            },
        )

        summary = CostTracker().get_cost_summary(days=18)

        assert [(s.service, s.amount) for s in summary.by_service] == [
            ("EC2", pytest.approx(150.0)),
            ("S3", pytest.approx(5.0)),
        ]
        assert summary.total == pytest.approx(155.0)
        assert self._tokens(mock_ce) == [None, "page-2"]
        first, second = mock_ce.get_cost_and_usage.call_args_list
        assert {k: v for k, v in second.kwargs.items() if k != "NextPageToken"} == first.kwargs

    @patch("cli.costs.boto3.Session")
    def test_regions_read_every_page(self, mock_session_cls):
        mock_ce = self._pages(
            mock_session_cls,
            {"ResultsByTime": [{"Groups": [_group("us-east-1", "10.00")]}], "NextPageToken": "a"},
            {"ResultsByTime": [{"Groups": [_group("us-west-2", "30.00")]}], "NextPageToken": "b"},
            {"ResultsByTime": [{"Groups": [_group("us-east-1", "5.00")]}]},
        )

        by_region = CostTracker().get_cost_by_region(days=30)

        assert by_region == {"us-west-2": pytest.approx(30.0), "us-east-1": pytest.approx(15.0)}
        assert self._tokens(mock_ce) == [None, "a", "b"]

    @patch("cli.costs.boto3.Session")
    def test_trend_reads_every_page(self, mock_session_cls):
        mock_ce = self._pages(
            mock_session_cls,
            {
                "ResultsByTime": [_day("2026-11-17", "1.00"), _day("2026-11-18", "2.00")],
                "NextPageToken": "next",
            },
            {"ResultsByTime": [_day("2026-11-19", "3.00")]},
        )

        trend = CostTracker().get_daily_trend(days=3)

        assert [day["date"] for day in trend] == ["2026-11-17", "2026-11-18", "2026-11-19"]
        assert self._tokens(mock_ce) == [None, "next"]

    @patch("cli.costs.boto3.Session")
    def test_a_failed_later_page_fails_the_query(self, mock_session_cls):
        self._pages(
            mock_session_cls,
            {"ResultsByTime": [{"Groups": [_group("EC2", "1.00")]}], "NextPageToken": "t"},
            _ce_client_error("InvalidNextTokenException", "token expired", "GetCostAndUsage"),
        )

        with pytest.raises(RuntimeError, match="InvalidNextTokenException"):
            CostTracker().get_cost_summary()


class TestProjectTagValueFromCdkJson:
    """Every Cost Explorer query filtered on the literal Project=GCO, while the
    stacks are tagged from cdk.json context.tags (app.py). A deployment whose
    Project tag differs got $0 from summary/regions/trend/forecast (N44)."""

    @patch("cli.costs.boto3.Session")
    def test_queries_use_the_value_cdk_json_tags_the_stacks_with(
        self, mock_session_cls, tmp_path, monkeypatch
    ):
        _write_cdk_json(
            tmp_path,
            {"project_name": "gco-staging", "tags": {"Project": "GCO-staging", "Owner": "ml"}},
        )
        monkeypatch.chdir(tmp_path)

        filters = _tag_filters_of_every_query(mock_session_cls.return_value.client.return_value)

        assert filters == [{"Key": "Project", "Values": ["GCO-staging"]}] * 4

    @patch("cli.costs.boto3.Session")
    def test_mcp_project_root_is_honoured(self, mock_session_cls, tmp_path):
        from cli.config import set_project_root

        _write_cdk_json(tmp_path, {"tags": {"Project": "Team-B"}})
        set_project_root(tmp_path)
        try:
            filters = _tag_filters_of_every_query(mock_session_cls.return_value.client.return_value)
        finally:
            set_project_root(None)

        assert {tag["Values"][0] for tag in filters} == {"Team-B"}

    @pytest.mark.parametrize(
        "context",
        [None, {}, {"tags": {}}, {"tags": {"Project": ""}}, {"tags": ["Project", "x"]}],
        ids=["no-cdk-json", "no-tags", "empty-tags", "empty-value", "malformed-tags"],
    )
    @patch("cli.costs.boto3.Session")
    def test_falls_back_to_the_committed_default(
        self, mock_session_cls, context, tmp_path, monkeypatch
    ):
        if context is not None:
            _write_cdk_json(tmp_path, context)
        monkeypatch.chdir(tmp_path)

        filters = _tag_filters_of_every_query(mock_session_cls.return_value.client.return_value)

        assert filters == [{"Key": "Project", "Values": ["GCO"]}] * 4

    @patch("cli.costs.boto3.Session")
    def test_unreadable_cdk_json_falls_back(self, mock_session_cls, tmp_path, monkeypatch):
        (tmp_path / "cdk.json").write_text("{not json")
        monkeypatch.chdir(tmp_path)

        filters = _tag_filters_of_every_query(mock_session_cls.return_value.client.return_value)

        assert {tag["Values"][0] for tag in filters} == {"GCO"}


# =============================================================================
# Unfiltered (--all) Mode Tests
# =============================================================================


class TestUnfilteredMode:
    """Tests for the unfiltered (--all) query mode."""

    @patch("cli.costs.boto3.Session")
    def test_summary_unfiltered_no_tag_filter(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

        tracker = CostTracker()
        tracker.get_cost_summary(unfiltered=True)

        call_args = mock_ce.get_cost_and_usage.call_args[1]
        assert "Filter" not in call_args

    @patch("cli.costs.boto3.Session")
    def test_summary_filtered_has_tag_filter(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

        tracker = CostTracker()
        tracker.get_cost_summary(unfiltered=False)

        call_args = mock_ce.get_cost_and_usage.call_args[1]
        assert "Filter" in call_args

    @patch("cli.costs.boto3.Session")
    def test_trend_unfiltered_no_tag_filter(self, mock_session_cls):
        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}

        tracker = CostTracker()
        tracker.get_daily_trend(unfiltered=True)

        call_args = mock_ce.get_cost_and_usage.call_args[1]
        assert "Filter" not in call_args

    @patch("cli.costs.boto3.Session")
    def test_summary_cli_all_flag(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "Groups": [
                        {
                            "Keys": ["EC2"],
                            "Metrics": {"UnblendedCost": {"Amount": "100.00"}},
                        },
                    ]
                }
            ]
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "summary", "--all"])
        assert result.exit_code == 0
        assert "Account" in result.output

    @patch("cli.costs.boto3.Session")
    def test_trend_cli_all_flag(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-03-01"},
                    "Total": {"UnblendedCost": {"Amount": "50.00"}},
                },
            ]
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "trend", "--all"])
        assert result.exit_code == 0
        assert "Account" in result.output


# =============================================================================
# Forecast CLI Tests
# =============================================================================


class TestForecastCLIExtended:
    """Extended forecast CLI tests."""

    @patch("cli.costs.boto3.Session")
    def test_forecast_error_handling(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        mock_ce.get_cost_forecast.side_effect = Exception("API error")

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "forecast"])
        assert result.exit_code == 1
        assert "API error" in result.stderr


# ============================================================================
# estimate_running_workloads — detailed k8s pod listing and cost calculation
# ============================================================================


def _make_cost_tracker_mocked():
    """Create a CostTracker with mocked boto3 session."""
    from cli.costs import CostTracker

    with patch("cli.costs.boto3.Session"):
        tracker = CostTracker()
    return tracker


class TestEstimateRunningWorkloadsDetailed:
    """Cover the k8s pod listing and cost calculation logic."""

    def test_full_path_with_running_pods(self):
        tracker = _make_cost_tracker_mocked()

        mock_pod = MagicMock()
        mock_pod.status.phase = "Running"
        mock_pod.metadata.name = "vllm-demo-abc123"
        mock_pod.spec.node_name = "ip-10-0-1-1.ec2.internal"
        mock_pod.status.start_time = datetime.now(UTC) - timedelta(hours=2)
        mock_container = MagicMock()
        mock_container.resources.requests = {"nvidia.com/gpu": "1"}
        mock_pod.spec.containers = [mock_container]

        mock_node = MagicMock()
        mock_node.metadata.labels = {"node.kubernetes.io/instance-type": "g4dn.xlarge"}

        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[mock_pod])
        mock_v1.read_node.return_value = mock_node

        with (
            patch("cli.kubectl_helpers.update_kubeconfig"),
            patch("kubernetes.client.CoreV1Api", return_value=mock_v1),
            patch("kubernetes.config.load_kube_config"),
            patch("cli.capacity.checker.CapacityChecker") as mock_cc,
        ):
            mock_cc.return_value.get_on_demand_price.return_value = 0.526
            workloads = tracker.estimate_running_workloads("us-east-1")

        found = [w for w in workloads if w.name == "vllm-demo-abc123"]
        assert len(found) >= 1
        assert found[0].gpu_count == 1
        assert found[0].instance_type == "g4dn.xlarge"
        assert found[0].hourly_rate == pytest.approx(0.526)

    @pytest.mark.parametrize(
        "context", ["arn:aws:eks:us-east-1:123456789012:cluster/gco-us-east-1", None]
    )
    def test_loads_the_context_update_kubeconfig_returns(self, context):
        """COSTS-CONTEXT: load_kube_config() read the current context, which can
        name another cluster; it now loads the one update_kubeconfig reports
        (None: the current context it just set)."""
        tracker = _make_cost_tracker_mocked()
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[])

        with (
            patch("cli.kubectl_helpers.update_kubeconfig", return_value=context) as update,
            patch("kubernetes.client.CoreV1Api", return_value=mock_v1),
            patch("kubernetes.config.load_kube_config") as load_kube_config,
            patch("cli.capacity.checker.CapacityChecker"),
        ):
            assert tracker.estimate_running_workloads("us-east-1") == []

        update.assert_called_once_with("gco-us-east-1", "us-east-1")
        load_kube_config.assert_called_once_with(context=context)

    def test_pod_no_node_name(self):
        tracker = _make_cost_tracker_mocked()

        mock_pod = MagicMock()
        mock_pod.status.phase = "Pending"
        mock_pod.metadata.name = "pending-job"
        mock_pod.spec.node_name = None
        mock_pod.status.start_time = None
        mock_pod.spec.containers = [MagicMock(resources=MagicMock(requests={}))]

        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[mock_pod])

        with (
            patch("cli.kubectl_helpers.update_kubeconfig"),
            patch("kubernetes.client.CoreV1Api", return_value=mock_v1),
            patch("kubernetes.config.load_kube_config"),
            patch("cli.capacity.checker.CapacityChecker") as mock_cc,
        ):
            mock_cc.return_value.get_on_demand_price.return_value = 0.0
            workloads = tracker.estimate_running_workloads("us-east-1")

        pending = [w for w in workloads if w.name == "pending-job"]
        assert len(pending) >= 1
        assert pending[0].instance_type == "unknown"
        assert pending[0].runtime_hours == 0.0

    def test_node_read_exception(self):
        tracker = _make_cost_tracker_mocked()

        mock_pod = MagicMock()
        mock_pod.status.phase = "Running"
        mock_pod.metadata.name = "gpu-job"
        mock_pod.spec.node_name = "some-node"
        mock_pod.status.start_time = datetime.now(UTC) - timedelta(hours=1)
        mock_pod.spec.containers = [
            MagicMock(resources=MagicMock(requests={"nvidia.com/gpu": "2"}))
        ]

        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[mock_pod])
        mock_v1.read_node.side_effect = RuntimeError("node not found")

        with (
            patch("cli.kubectl_helpers.update_kubeconfig"),
            patch("kubernetes.client.CoreV1Api", return_value=mock_v1),
            patch("kubernetes.config.load_kube_config"),
            patch("cli.capacity.checker.CapacityChecker") as mock_cc,
        ):
            mock_cc.return_value.get_on_demand_price.return_value = 0.0
            workloads = tracker.estimate_running_workloads("us-east-1")

        found = [w for w in workloads if w.name == "gpu-job"]
        assert len(found) >= 1
        assert found[0].instance_type == "unknown"
        assert found[0].gpu_count == 2

    def test_namespace_list_exception(self):
        from cli.costs import WorkloadReadError

        tracker = _make_cost_tracker_mocked()

        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.side_effect = RuntimeError("connection reset")

        with (
            patch("cli.kubectl_helpers.update_kubeconfig"),
            patch("kubernetes.client.CoreV1Api", return_value=mock_v1),
            patch("kubernetes.config.load_kube_config"),
            patch("cli.capacity.checker.CapacityChecker"),
            pytest.raises(WorkloadReadError, match="connection reset") as raised,
        ):
            tracker.estimate_running_workloads("us-east-1")

        assert raised.value.workloads == []

    def test_skips_non_running_pods(self):
        tracker = _make_cost_tracker_mocked()

        mock_pod = MagicMock()
        mock_pod.status.phase = "Succeeded"
        mock_pod.metadata.name = "done-job"

        mock_v1 = MagicMock()
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[mock_pod])

        with (
            patch("cli.kubectl_helpers.update_kubeconfig"),
            patch("kubernetes.client.CoreV1Api", return_value=mock_v1),
            patch("kubernetes.config.load_kube_config"),
            patch("cli.capacity.checker.CapacityChecker"),
        ):
            workloads = tracker.estimate_running_workloads("us-east-1")

        assert all(w.name != "done-job" for w in workloads)


def _wire_pod(name: str, namespace: str, *, gpus: str = "1", node: str | None = "node-a"):
    from kubernetes.client import V1Pod

    spec: dict = {
        "containers": [
            {
                "name": "main",
                "image": "example.com/train:1",
                "resources": {"requests": {"nvidia.com/gpu": gpus}},
            }
        ]
    }
    if node:
        spec["nodeName"] = node
    return V1Pod.from_dict(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace},
            "spec": spec,
            "status": {"phase": "Running", "startTime": "2026-11-19T00:00:00Z"},
        }
    )


def _wire_pod_list(*pods):
    from kubernetes.client import V1PodList

    return V1PodList(api_version="v1", kind="PodList", items=list(pods))


def _wire_node(instance_type: str):
    from kubernetes.client import V1Node

    return V1Node.from_dict(
        {
            "apiVersion": "v1",
            "kind": "Node",
            "metadata": {
                "name": "node-a",
                "labels": {"node.kubernetes.io/instance-type": instance_type},
            },
        }
    )


class TestWorkloadReadFailures:
    """`gco costs workloads` swallowed every Kubernetes failure (kubeconfig,
    pod listing) and printed "No running workloads found" with exit 0, and its
    requests carried no timeout (N37). Failures are now reported per region and
    the command exits 1; requests go through BoundedApiClient."""

    @staticmethod
    def _estimate(v1, region="us-east-1", **patches):
        from cli.costs import CostTracker

        with patch("cli.costs.boto3.Session"):
            tracker = CostTracker(config=GCOConfig(project_name="gco"))
        with (
            patch("cli.kubectl_helpers.update_kubeconfig", **patches),
            patch("kubernetes.config.load_kube_config"),
            patch("kubernetes.client.CoreV1Api", return_value=v1) as core_api,
            patch("cli.capacity.checker.CapacityChecker") as checker,
        ):
            checker.return_value.get_on_demand_price.return_value = 2.0
            try:
                return tracker.estimate_running_workloads(region), core_api
            except Exception as error:
                error.core_api = core_api
                raise

    def test_requests_are_bounded_by_the_shared_client(self, monkeypatch):
        from gco.services.k8s_client import BoundedApiClient

        monkeypatch.setenv("K8S_API_TIMEOUT", "12")
        v1 = MagicMock()
        v1.list_namespaced_pod.return_value = _wire_pod_list()

        workloads, core_api = self._estimate(v1)

        assert workloads == []
        (api_client,) = core_api.call_args.args
        assert isinstance(api_client, BoundedApiClient)
        assert api_client.default_request_timeout == 12

    def test_kubeconfig_failure_is_an_error_not_an_empty_region(self):
        from cli.costs import WorkloadReadError

        with pytest.raises(WorkloadReadError) as raised:
            self._estimate(MagicMock(), side_effect=RuntimeError("AccessDeniedException"))

        assert raised.value.region == "us-east-1"
        assert raised.value.workloads == []
        assert "gco-us-east-1" in str(raised.value)
        assert "AccessDeniedException" in str(raised.value)

    def test_an_unreachable_api_server_stops_after_the_first_namespace(self):
        from urllib3.exceptions import ConnectTimeoutError, MaxRetryError

        from cli.costs import WorkloadReadError

        v1 = MagicMock()
        v1.list_namespaced_pod.side_effect = MaxRetryError(
            None,
            "/api/v1/namespaces/gco-inference/pods",
            reason=ConnectTimeoutError("connect timed out"),
        )

        with pytest.raises(WorkloadReadError) as raised:
            self._estimate(v1)

        assert "cannot reach the API server of cluster gco-us-east-1" in str(raised.value)
        # The second namespace would wait out the same timeouts.
        assert v1.list_namespaced_pod.call_count == 1

    def test_a_refused_namespace_keeps_what_the_other_one_returned(self):
        from kubernetes.client.exceptions import ApiException

        from cli.costs import WorkloadReadError

        v1 = MagicMock()
        v1.list_namespaced_pod.side_effect = [
            ApiException(status=403, reason="Forbidden"),
            _wire_pod_list(_wire_pod("train-0", "gco-jobs", gpus="8")),
        ]
        v1.read_node.return_value = _wire_node("p5.48xlarge")

        with pytest.raises(WorkloadReadError) as raised:
            self._estimate(v1)

        assert raised.value.errors == [
            "cannot list pods in gco-inference on cluster gco-us-east-1: HTTP 403 Forbidden"
        ]
        (workload,) = raised.value.workloads
        assert (workload.name, workload.workload_type, workload.gpu_count) == ("train-0", "job", 8)
        assert workload.instance_type == "p5.48xlarge"

    def _invoke(self, args, outcomes):
        """Run `gco costs workloads` with each region's estimate scripted."""
        from cli.main import cli

        def estimate(region):
            outcome = outcomes[region]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        tracker = MagicMock()
        tracker.estimate_running_workloads.side_effect = estimate
        with (
            patch("cli.costs.get_cost_tracker", return_value=tracker),
            patch(
                "cli.commands.costs_cmd._get_deployment_regions",
                return_value=list(outcomes),
            ),
        ):
            return CliRunner().invoke(cli, args)

    @staticmethod
    def _unreachable(region):
        from cli.costs import WorkloadReadError

        return WorkloadReadError(
            region, [f"cannot reach the API server of cluster gco-{region}: timed out"], []
        )

    @pytest.mark.parametrize("output_format", ["table", "json"])
    def test_an_unreachable_cluster_is_not_reported_as_no_workloads(self, output_format):
        result = self._invoke(
            ["--output", output_format, "costs", "workloads"],
            {"us-east-1": self._unreachable("us-east-1")},
        )

        assert result.exit_code == 1, result.output
        assert "No running workloads found" not in result.output
        assert "us-east-1: cannot reach the API server of cluster gco-us-east-1" in result.stderr
        if output_format == "json":
            assert json.loads(result.stdout) == []

    def test_partial_failure_prints_the_readable_regions_and_exits_1(self):
        job = WorkloadCost(
            name="train-0",
            workload_type="job",
            instance_type="p5.48xlarge",
            gpu_count=8,
            hourly_rate=2.0,
            runtime_hours=1.0,
            estimated_cost=2.0,
            region="us-west-2",
            status="Running",
        )
        outcomes = {"us-east-1": self._unreachable("us-east-1"), "us-west-2": [job]}

        table = self._invoke(["costs", "workloads"], outcomes)
        machine = self._invoke(["--output", "json", "costs", "workloads"], outcomes)

        assert table.exit_code == 1, table.output
        assert "train-0" in table.stdout
        assert "us-east-1: cannot reach" in table.stderr
        assert machine.exit_code == 1, machine.output
        assert [row["region"] for row in json.loads(machine.stdout)] == ["us-west-2"]

    def test_nothing_running_in_the_readable_regions_names_them(self):
        outcomes = {"us-east-1": self._unreachable("us-east-1"), "us-west-2": []}

        result = self._invoke(["costs", "workloads"], outcomes)

        assert result.exit_code == 1, result.output
        assert "No running workloads found in us-west-2" in result.stdout

    def test_a_readable_empty_fleet_still_exits_0(self):
        result = self._invoke(["costs", "workloads"], {"us-east-1": [], "us-west-2": []})

        assert result.exit_code == 0, result.output
        assert "No running workloads found" in result.stdout
        assert result.stderr == ""


# =============================================================================
# Unit Tests - Cost allocation tags
# =============================================================================


def _tracker_with_ce():
    """Return (tracker, mock_ce) with the boto3 session already stubbed."""
    tracker = CostTracker()
    mock_ce = MagicMock()
    mock_session = MagicMock()
    mock_session.client.return_value = mock_ce
    tracker._session = mock_session
    return tracker, mock_ce


class TestCostAllocationTagStatus:
    """get_cost_allocation_tag_status reports Active/Inactive/NotFound."""

    def test_defaults_cover_project_and_eks_cluster_name(self):
        from cli.costs import DEFAULT_COST_ALLOCATION_TAG_KEYS

        assert DEFAULT_COST_ALLOCATION_TAG_KEYS == ("Project", "aws:eks:cluster-name")

    def test_reports_found_and_synthesizes_not_found(self):
        """A key Billing has never seen is absent from the response — it must
        surface as NotFound instead of silently disappearing."""
        tracker, mock_ce = _tracker_with_ce()
        mock_ce.list_cost_allocation_tags.return_value = {
            "CostAllocationTags": [
                {
                    "TagKey": "Project",
                    "Type": "UserDefined",
                    "Status": "Active",
                    "LastUpdatedDate": "2026-07-01T00:00:00Z",
                    "LastUsedDate": "2026-07-28T00:00:00Z",
                }
            ]
        }

        statuses = tracker.get_cost_allocation_tag_status()

        assert [s["tag_key"] for s in statuses] == ["Project", "aws:eks:cluster-name"]
        assert statuses[0]["status"] == "Active"
        assert statuses[0]["type"] == "UserDefined"
        assert statuses[1]["status"] == "NotFound"
        kwargs = mock_ce.list_cost_allocation_tags.call_args.kwargs
        assert kwargs["TagKeys"] == ["Project", "aws:eks:cluster-name"]

    def test_follows_pagination(self):
        tracker, mock_ce = _tracker_with_ce()
        mock_ce.list_cost_allocation_tags.side_effect = [
            {
                "CostAllocationTags": [
                    {"TagKey": "Project", "Type": "UserDefined", "Status": "Inactive"}
                ],
                "NextToken": "page-2",
            },
            {
                "CostAllocationTags": [
                    {"TagKey": "aws:eks:cluster-name", "Type": "AWSGenerated", "Status": "Active"}
                ]
            },
        ]

        statuses = tracker.get_cost_allocation_tag_status()

        assert mock_ce.list_cost_allocation_tags.call_count == 2
        second_kwargs = mock_ce.list_cost_allocation_tags.call_args_list[1].kwargs
        assert second_kwargs["NextToken"] == "page-2"
        assert statuses[0]["status"] == "Inactive"
        assert statuses[1]["status"] == "Active"

    def test_batches_more_than_twenty_keys(self):
        """The TagKeys filter caps at 20 entries; larger requests must chunk."""
        tracker, mock_ce = _tracker_with_ce()
        mock_ce.list_cost_allocation_tags.return_value = {"CostAllocationTags": []}
        keys = [f"key-{i}" for i in range(25)]

        statuses = tracker.get_cost_allocation_tag_status(keys)

        assert mock_ce.list_cost_allocation_tags.call_count == 2
        first = mock_ce.list_cost_allocation_tags.call_args_list[0].kwargs["TagKeys"]
        second = mock_ce.list_cost_allocation_tags.call_args_list[1].kwargs["TagKeys"]
        assert len(first) == 20 and len(second) == 5
        assert len(statuses) == 25
        assert all(s["status"] == "NotFound" for s in statuses)


class TestActivateCostAllocationTags:
    """activate_cost_allocation_tags splits per-key successes and errors."""

    def test_all_keys_activate(self):
        tracker, mock_ce = _tracker_with_ce()
        mock_ce.update_cost_allocation_tags_status.return_value = {"Errors": []}

        result = tracker.activate_cost_allocation_tags()

        assert result == {"activated": ["Project", "aws:eks:cluster-name"], "errors": []}
        entries = mock_ce.update_cost_allocation_tags_status.call_args.kwargs[
            "CostAllocationTagsStatus"
        ]
        assert entries == [
            {"TagKey": "Project", "Status": "Active"},
            {"TagKey": "aws:eks:cluster-name", "Status": "Active"},
        ]

    def test_per_key_error_does_not_mask_other_successes(self):
        """One not-yet-discovered key must not hide that the other activated."""
        tracker, mock_ce = _tracker_with_ce()
        mock_ce.update_cost_allocation_tags_status.return_value = {
            "Errors": [
                {
                    "TagKey": "aws:eks:cluster-name",
                    "Code": "TagKeysNotFoundException",
                    "Message": "Failed to update Cost Allocation Tag: aws:eks:cluster-name",
                }
            ]
        }

        result = tracker.activate_cost_allocation_tags()

        assert result["activated"] == ["Project"]
        assert result["errors"] == [
            {
                "tag_key": "aws:eks:cluster-name",
                "code": "TagKeysNotFoundException",
                "message": "Failed to update Cost Allocation Tag: aws:eks:cluster-name",
            }
        ]

    def test_batches_more_than_twenty_keys(self):
        """UpdateCostAllocationTagsStatus caps at 20 entries per call."""
        tracker, mock_ce = _tracker_with_ce()
        mock_ce.update_cost_allocation_tags_status.return_value = {"Errors": []}
        keys = [f"key-{i}" for i in range(25)]

        result = tracker.activate_cost_allocation_tags(keys)

        assert mock_ce.update_cost_allocation_tags_status.call_count == 2
        first = mock_ce.update_cost_allocation_tags_status.call_args_list[0].kwargs[
            "CostAllocationTagsStatus"
        ]
        second = mock_ce.update_cost_allocation_tags_status.call_args_list[1].kwargs[
            "CostAllocationTagsStatus"
        ]
        assert len(first) == 20 and len(second) == 5
        assert result["activated"] == keys


class TestCostAllocationBackfill:
    """Backfill start + history wrappers."""

    def test_start_returns_normalized_request(self):
        tracker, mock_ce = _tracker_with_ce()
        mock_ce.start_cost_allocation_tag_backfill.return_value = {
            "BackfillRequest": {
                "BackfillFrom": "2026-01-01T00:00:00Z",
                "RequestedAt": "2026-07-28T12:00:00Z",
                "BackfillStatus": "PROCESSING",
            }
        }

        result = tracker.start_cost_allocation_tag_backfill("2026-01-01T00:00:00Z")

        assert result["backfill_from"] == "2026-01-01T00:00:00Z"
        assert result["status"] == "PROCESSING"
        assert result["completed_at"] == ""
        mock_ce.start_cost_allocation_tag_backfill.assert_called_once_with(
            BackfillFrom="2026-01-01T00:00:00Z"
        )

    def test_history_follows_pagination(self):
        tracker, mock_ce = _tracker_with_ce()
        mock_ce.list_cost_allocation_tag_backfill_history.side_effect = [
            {
                "BackfillRequests": [
                    {
                        "BackfillFrom": "2026-01-01T00:00:00Z",
                        "RequestedAt": "2026-07-28T12:00:00Z",
                        "CompletedAt": "2026-07-28T13:00:00Z",
                        "BackfillStatus": "SUCCEEDED",
                    }
                ],
                "NextToken": "more",
            },
            {
                "BackfillRequests": [
                    {
                        "BackfillFrom": "2025-10-01T00:00:00Z",
                        "RequestedAt": "2026-01-02T00:00:00Z",
                        "BackfillStatus": "FAILED",
                    }
                ]
            },
        ]

        history = tracker.get_cost_allocation_backfill_history()

        assert len(history) == 2
        assert history[0]["status"] == "SUCCEEDED"
        assert history[1]["status"] == "FAILED"
        assert mock_ce.list_cost_allocation_tag_backfill_history.call_count == 2


class TestCostsAllocationCLI:
    """CLI wiring for `gco costs allocation status|activate`."""

    @patch("cli.costs.boto3.Session")
    def test_status_table_flags_not_found_and_split_cost_guidance(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session
        mock_ce.list_cost_allocation_tags.return_value = {
            "CostAllocationTags": [{"TagKey": "Project", "Type": "UserDefined", "Status": "Active"}]
        }
        mock_ce.list_cost_allocation_tag_backfill_history.return_value = {"BackfillRequests": []}

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "allocation", "status"])

        assert result.exit_code == 0
        assert "Project" in result.output
        assert "aws:eks:cluster-name" in result.output
        assert "NotFound" in result.output
        assert "24 hours" in result.output
        assert "Split cost allocation data" in result.output
        assert "console-only" in result.output

    @patch("cli.costs.boto3.Session")
    def test_status_extra_tags_are_queried(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session
        mock_ce.list_cost_allocation_tags.return_value = {"CostAllocationTags": []}
        mock_ce.list_cost_allocation_tag_backfill_history.return_value = {"BackfillRequests": []}

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "allocation", "status", "-t", "Environment"])

        assert result.exit_code == 0
        assert "Environment" in result.output
        kwargs = mock_ce.list_cost_allocation_tags.call_args.kwargs
        assert kwargs["TagKeys"] == ["Project", "aws:eks:cluster-name", "Environment"]

    @patch("cli.costs.boto3.Session")
    def test_activate_requires_confirmation(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "allocation", "activate"], input="n\n")

        assert result.exit_code != 0
        mock_ce.update_cost_allocation_tags_status.assert_not_called()

    @patch("cli.costs.boto3.Session")
    def test_activate_yes_activates_defaults(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session
        mock_ce.update_cost_allocation_tags_status.return_value = {"Errors": []}

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "allocation", "activate", "-y"])

        assert result.exit_code == 0
        assert "Project: active" in result.output
        assert "aws:eks:cluster-name: active" in result.output
        assert "24 hours" in result.output
        mock_ce.start_cost_allocation_tag_backfill.assert_not_called()

    @patch("cli.costs.boto3.Session")
    def test_activate_surfaces_per_key_error_and_exits_nonzero(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session
        mock_ce.update_cost_allocation_tags_status.return_value = {
            "Errors": [
                {
                    "TagKey": "aws:eks:cluster-name",
                    "Code": "TagKeysNotFoundException",
                    "Message": "Failed to update Cost Allocation Tag: aws:eks:cluster-name",
                }
            ]
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["costs", "allocation", "activate", "-y"])

        assert result.exit_code == 1
        assert "Project: active" in result.output
        assert "TagKeysNotFoundException" in result.output
        assert "deploy first, then retry" in result.output

    @patch("cli.costs.boto3.Session")
    def test_activate_backfill_date_is_normalized_to_timestamp(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session
        mock_ce.update_cost_allocation_tags_status.return_value = {"Errors": []}
        mock_ce.start_cost_allocation_tag_backfill.return_value = {
            "BackfillRequest": {
                "BackfillFrom": "2026-01-01T00:00:00Z",
                "RequestedAt": "2026-07-28T12:00:00Z",
                "BackfillStatus": "PROCESSING",
            }
        }

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["costs", "allocation", "activate", "-y", "--backfill-from", "2026-01-01"],
        )

        assert result.exit_code == 0
        mock_ce.start_cost_allocation_tag_backfill.assert_called_once_with(
            BackfillFrom="2026-01-01T00:00:00Z"
        )
        assert "Backfill from 2026-01-01" in result.output
        assert "PROCESSING" in result.output

    @patch("cli.costs.boto3.Session")
    def test_status_json_output(self, mock_session_cls):
        from cli.main import cli

        mock_ce = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ce
        mock_session_cls.return_value = mock_session
        mock_ce.list_cost_allocation_tags.return_value = {"CostAllocationTags": []}
        mock_ce.list_cost_allocation_tag_backfill_history.return_value = {"BackfillRequests": []}

        runner = CliRunner()
        result = runner.invoke(cli, ["-o", "json", "costs", "allocation", "status"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert [t["tag_key"] for t in payload["tags"]] == ["Project", "aws:eks:cluster-name"]
        assert "split_cost_allocation_data" in payload
