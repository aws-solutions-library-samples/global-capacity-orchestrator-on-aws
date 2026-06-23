# Tests for the gco capacity history CLI commands in cli/commands/capacity_cmd.py.
# Drives capacity history show/stats/patterns and capacity check --enrich-historical
# with CliRunner, patching the history store (and checker) so no AWS is touched.

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from click.testing import CliRunner

from cli.capacity.advisor import CapacityPredictionResult
from cli.main import cli


class TestHistoryShow:
    @patch("cli.capacity.history.get_capacity_history_store")
    def test_show_renders_trend(self, mock_get_store):
        store = MagicMock()
        store.get_trend.return_value = [
            {"timestamp": "2025-06-23T14:00:00+00:00", "spot_score": 8, "spot_price": 1.5},
            {"timestamp": "2025-06-23T15:00:00+00:00", "spot_score": 7, "spot_price": 1.6},
        ]
        mock_get_store.return_value = store
        result = CliRunner().invoke(
            cli, ["capacity", "history", "show", "-i", "g5.xlarge", "-r", "us-east-1"]
        )
        assert result.exit_code == 0
        assert "2025-06-23" in result.output

    @patch("cli.capacity.history.get_capacity_history_store")
    def test_show_empty_warns(self, mock_get_store):
        store = MagicMock()
        store.get_trend.return_value = []
        mock_get_store.return_value = store
        result = CliRunner().invoke(
            cli, ["capacity", "history", "show", "-i", "g5.xlarge", "-r", "us-east-1"]
        )
        assert result.exit_code == 0
        assert "No historical samples" in result.output


class TestHistoryStats:
    @patch("cli.capacity.history.get_capacity_history_store")
    def test_stats_renders(self, mock_get_store):
        store = MagicMock()
        store.get_statistics.return_value = {
            "instance_type": "g5.xlarge",
            "region": "us-east-1",
            "hours_back": 168,
            "sample_count": 4,
            "metrics": {
                "spot_score": {
                    "count": 4,
                    "min": 3.0,
                    "max": 9.0,
                    "mean": 6.0,
                    "p25": 4.5,
                    "p50": 6.0,
                    "p75": 7.5,
                    "stddev": 2.58,
                }
            },
        }
        mock_get_store.return_value = store
        result = CliRunner().invoke(
            cli, ["capacity", "history", "stats", "-i", "g5.xlarge", "-r", "us-east-1"]
        )
        assert result.exit_code == 0
        assert "spot_score" in result.output

    @patch("cli.capacity.history.get_capacity_history_store")
    def test_stats_empty_warns(self, mock_get_store):
        store = MagicMock()
        store.get_statistics.return_value = {"sample_count": 0, "metrics": {}}
        mock_get_store.return_value = store
        result = CliRunner().invoke(
            cli, ["capacity", "history", "stats", "-i", "g5.xlarge", "-r", "us-east-1"]
        )
        assert result.exit_code == 0
        assert "No historical samples" in result.output


class TestHistoryPatterns:
    @patch("cli.capacity.history.get_capacity_history_store")
    def test_patterns_renders(self, mock_get_store):
        store = MagicMock()
        store.get_temporal_patterns.return_value = {
            "instance_type": "g5.xlarge",
            "region": "us-east-1",
            "metric": "spot_score",
            "patterns": {"Monday": {14: {"avg": 8.1, "count": 3}}},
            "best_windows": [{"day": "Monday", "hour": 14, "avg": 8.1, "count": 3}],
        }
        mock_get_store.return_value = store
        result = CliRunner().invoke(
            cli, ["capacity", "history", "patterns", "-i", "g5.xlarge", "-r", "us-east-1"]
        )
        assert result.exit_code == 0
        assert "Monday" in result.output or "Best windows" in result.output


class TestCheckEnrichHistorical:
    @patch("cli.capacity.history.get_capacity_history_store")
    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    def test_check_appends_historical_context(self, mock_get_checker, mock_get_store):
        checker = MagicMock()
        checker.estimate_capacity.return_value = []
        mock_get_checker.return_value = checker
        store = MagicMock()
        store.get_statistics.return_value = {
            "sample_count": 5,
            "metrics": {"spot_score": {"p25": 5, "p50": 6, "p75": 7, "min": 3, "max": 9}},
        }
        mock_get_store.return_value = store
        result = CliRunner().invoke(
            cli,
            ["capacity", "check", "-i", "g5.xlarge", "-r", "us-east-1", "--enrich-historical"],
        )
        assert result.exit_code == 0
        assert "Historical context" in result.output


def _rnf(op="GetItem"):
    return ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}}, op)


class TestPredict:
    @patch("cli.capacity.get_bedrock_capacity_advisor")
    def test_predict_renders_table(self, mock_get_advisor):
        advisor = MagicMock()
        advisor.predict_capacity_window.return_value = CapacityPredictionResult(
            instance_type="g5.xlarge",
            region="us-east-1",
            best_windows=[{"day": "Monday", "hour_range": "13:00-16:00 UTC", "why": "peak"}],
            avoid_windows=[{"day": "Friday", "hour_range": "18:00-22:00 UTC", "why": "busy"}],
            reasoning="Mondays score highest.",
            confidence="high",
        )
        mock_get_advisor.return_value = advisor
        result = CliRunner().invoke(
            cli, ["capacity", "predict", "-i", "g5.xlarge", "-r", "us-east-1"]
        )
        assert result.exit_code == 0
        assert "Best time to acquire" in result.output
        assert "Monday" in result.output

    @patch("cli.capacity.get_bedrock_capacity_advisor")
    def test_predict_no_samples_warns(self, mock_get_advisor):
        advisor = MagicMock()
        advisor.predict_capacity_window.side_effect = ValueError(
            "No historical capacity samples for g5.xlarge in us-east-1 yet."
        )
        mock_get_advisor.return_value = advisor
        result = CliRunner().invoke(
            cli, ["capacity", "predict", "-i", "g5.xlarge", "-r", "us-east-1"]
        )
        assert result.exit_code == 0
        assert "No historical capacity samples" in result.output

    @patch("cli.capacity.get_bedrock_capacity_advisor")
    def test_predict_table_missing_hints(self, mock_get_advisor):
        advisor = MagicMock()
        advisor.predict_capacity_window.side_effect = _rnf("GetStatistics")
        mock_get_advisor.return_value = advisor
        result = CliRunner().invoke(
            cli, ["capacity", "predict", "-i", "g5.xlarge", "-r", "us-east-1"]
        )
        assert result.exit_code == 0
        assert "optional add-on" in result.output


class TestHistoryFriendlyErrors:
    @patch("cli.capacity.history.get_capacity_history_store")
    def test_show_table_missing_hints(self, mock_get_store):
        store = MagicMock()
        store.get_trend.side_effect = _rnf()
        mock_get_store.return_value = store
        result = CliRunner().invoke(
            cli, ["capacity", "history", "show", "-i", "g5.xlarge", "-r", "us-east-1"]
        )
        assert result.exit_code == 0
        assert "optional add-on" in result.output

    @patch("cli.capacity.history.get_capacity_history_store")
    def test_stats_table_missing_hints(self, mock_get_store):
        store = MagicMock()
        store.get_statistics.side_effect = _rnf()
        mock_get_store.return_value = store
        result = CliRunner().invoke(
            cli, ["capacity", "history", "stats", "-i", "g5.xlarge", "-r", "us-east-1"]
        )
        assert result.exit_code == 0
        assert "optional add-on" in result.output

    @patch("cli.capacity.history.get_capacity_history_store")
    def test_patterns_table_missing_hints(self, mock_get_store):
        store = MagicMock()
        store.get_temporal_patterns.side_effect = _rnf()
        mock_get_store.return_value = store
        result = CliRunner().invoke(
            cli, ["capacity", "history", "patterns", "-i", "g5.xlarge", "-r", "us-east-1"]
        )
        assert result.exit_code == 0
        assert "optional add-on" in result.output

    @patch("cli.capacity.history.get_capacity_history_store")
    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    def test_check_enrich_table_missing_hints(self, mock_get_checker, mock_get_store):
        checker = MagicMock()
        checker.estimate_capacity.return_value = []
        mock_get_checker.return_value = checker
        store = MagicMock()
        store.get_statistics.side_effect = _rnf()
        mock_get_store.return_value = store
        result = CliRunner().invoke(
            cli,
            ["capacity", "check", "-i", "g5.xlarge", "-r", "us-east-1", "--enrich-historical"],
        )
        assert result.exit_code == 0
        assert "optional add-on" in result.output
