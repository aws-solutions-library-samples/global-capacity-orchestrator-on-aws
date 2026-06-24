# Tests for cli/capacity/advisor.py historical enrichment.
# Covers _build_prompt interpretation bands plus the Historical Context block, and
# _gather_historical_context (store-backed build path and graceful empty-on-error).

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from cli.capacity.advisor import BedrockCapacityAdvisor, CapacityPredictionResult

BASE_DATA = {"timestamp": "t", "regions_analyzed": [], "instance_types_analyzed": []}


def _advisor():
    return BedrockCapacityAdvisor.__new__(BedrockCapacityAdvisor)


def _hist(current):
    return {
        "g5.xlarge#us-east-1": {
            "instance_type": "g5.xlarge",
            "region": "us-east-1",
            "current_spot_score": current,
            "p25": 5,
            "p50": 6,
            "p75": 7,
            "best_windows": [{"day": "Monday", "hour": 14, "avg": 8.1, "count": 3}],
        }
    }


class TestBuildPromptHistorical:
    @pytest.mark.parametrize(
        ("current", "phrase"),
        [
            (3, "likely transient contention"),
            (6, "within normal range"),
            (9, "unusually favorable"),
        ],
    )
    def test_interpretation_bands(self, current, phrase):
        prompt = _advisor()._build_prompt(BASE_DATA, None, None, _hist(current))
        assert "## Historical Context (last 7 days)" in prompt
        assert phrase in prompt
        assert "Monday 14:00 (avg 8.1)" in prompt

    def test_no_historical_section_when_none(self):
        prompt = _advisor()._build_prompt(BASE_DATA, None, None, None)
        assert "Historical Context" not in prompt


class TestGatherHistoricalContext:
    @patch("cli.capacity.history.get_capacity_history_store")
    def test_builds_context(self, mock_get_store):
        store = MagicMock()
        store.get_statistics.return_value = {
            "metrics": {"spot_score": {"p25": 5, "p50": 6, "p75": 7}}
        }
        store.get_temporal_patterns.return_value = {
            "best_windows": [{"day": "Monday", "hour": 14, "avg": 8.1, "count": 3}]
        }
        mock_get_store.return_value = store
        capacity_data = {
            "spot_data": {"g5.xlarge": {"us-east-1": {"placement_scores": {"regional": 3}}}}
        }
        result = _advisor()._gather_historical_context(capacity_data)
        assert "g5.xlarge#us-east-1" in result
        ctx = result["g5.xlarge#us-east-1"]
        assert ctx["current_spot_score"] == 3
        assert ctx["p25"] == 5
        assert len(ctx["best_windows"]) == 1

    @patch("cli.capacity.history.get_capacity_history_store", side_effect=Exception("boom"))
    def test_returns_empty_on_error(self, mock_get_store):
        capacity_data = {
            "spot_data": {"g5.xlarge": {"us-east-1": {"placement_scores": {"regional": 3}}}}
        }
        assert _advisor()._gather_historical_context(capacity_data) == {}


def _predict_advisor(client):
    adv = BedrockCapacityAdvisor.__new__(BedrockCapacityAdvisor)
    adv.model_id = "test-model"
    adv._get_bedrock_client = MagicMock(return_value=client)
    return adv


def _converse(text):
    return {"output": {"message": {"content": [{"text": text}]}}}


_PREDICT_STATS = {
    "instance_type": "g5.xlarge",
    "region": "us-east-1",
    "hours_back": 168,
    "sample_count": 12,
    "metrics": {
        "spot_score": {"p25": 5, "p50": 6, "p75": 7, "min": 3, "max": 9},
        "spot_price": {"p25": 1.1, "p50": 1.2, "p75": 1.3},
    },
}
_PREDICT_PATTERNS = {"best_windows": [{"day": "Monday", "hour": 14, "avg": 8.1, "count": 3}]}


class TestBuildPredictPrompt:
    def test_prompt_includes_metrics_and_windows(self):
        prompt = _advisor()._build_predict_prompt(
            "g5.xlarge", "us-east-1", _PREDICT_STATS, _PREDICT_PATTERNS
        )
        assert "g5.xlarge" in prompt
        assert "us-east-1" in prompt
        assert "p25=5" in prompt
        assert "Monday 14:00 UTC" in prompt
        assert '"best_windows"' in prompt

    def test_prompt_handles_missing_metrics(self):
        prompt = _advisor()._build_predict_prompt("g5.xlarge", "us-east-1", {}, {})
        assert "g5.xlarge" in prompt
        assert '"confidence": "high|medium|low"' in prompt


class TestPredictCapacityWindow:
    @patch("cli.capacity.history.get_capacity_history_store")
    def test_parses_json_response(self, mock_get_store):
        store = MagicMock()
        store.get_statistics.return_value = _PREDICT_STATS
        store.get_temporal_patterns.return_value = _PREDICT_PATTERNS
        mock_get_store.return_value = store
        client = MagicMock()
        client.converse.return_value = _converse(
            '{"best_windows": [{"day": "Monday", "hour_range": "13:00-16:00 UTC", '
            '"why": "peak availability"}], "avoid_windows": [{"day": "Friday", '
            '"hour_range": "18:00-22:00 UTC", "why": "contention"}], '
            '"reasoning": "Mondays score highest.", "confidence": "high"}'
        )
        result = _predict_advisor(client).predict_capacity_window("g5.xlarge", "us-east-1")
        assert isinstance(result, CapacityPredictionResult)
        assert result.confidence == "high"
        assert result.best_windows[0]["day"] == "Monday"
        assert result.avoid_windows[0]["day"] == "Friday"
        assert "Mondays" in result.reasoning

    @patch("cli.capacity.history.get_capacity_history_store")
    def test_raises_when_no_samples(self, mock_get_store):
        store = MagicMock()
        store.get_statistics.return_value = {"sample_count": 0, "metrics": {}}
        mock_get_store.return_value = store
        with pytest.raises(ValueError, match="No historical capacity samples"):
            _predict_advisor(MagicMock()).predict_capacity_window("g5.xlarge", "us-east-1")

    @patch("cli.capacity.history.get_capacity_history_store")
    def test_propagates_table_missing(self, mock_get_store):
        store = MagicMock()
        store.get_statistics.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}},
            "GetItem",
        )
        mock_get_store.return_value = store
        with pytest.raises(ClientError):
            _predict_advisor(MagicMock()).predict_capacity_window("g5.xlarge", "us-east-1")

    @patch("cli.capacity.history.get_capacity_history_store")
    def test_tolerates_non_json_response(self, mock_get_store):
        store = MagicMock()
        store.get_statistics.return_value = _PREDICT_STATS
        store.get_temporal_patterns.return_value = _PREDICT_PATTERNS
        mock_get_store.return_value = store
        client = MagicMock()
        client.converse.return_value = _converse("the model rambled without json")
        result = _predict_advisor(client).predict_capacity_window("g5.xlarge", "us-east-1")
        assert result.best_windows == []
        assert result.confidence == "low"
        assert "rambled" in result.raw_response


class TestPredictAllRegions:
    @patch("cli.capacity.history.get_capacity_history_store")
    def test_predicts_each_region(self, mock_get_store):
        store = MagicMock()
        store.get_regions_with_data.return_value = ["us-east-1", "us-west-2"]
        store.get_statistics.return_value = _PREDICT_STATS
        store.get_temporal_patterns.return_value = _PREDICT_PATTERNS
        mock_get_store.return_value = store
        client = MagicMock()
        client.converse.return_value = _converse(
            '{"best_windows": [], "avoid_windows": [], "reasoning": "x", "confidence": "medium"}'
        )
        results = _predict_advisor(client).predict_capacity_windows_all_regions("g5.xlarge")
        assert [r.region for r in results] == ["us-east-1", "us-west-2"]
        assert all(r.confidence == "medium" for r in results)

    @patch("cli.capacity.history.get_capacity_history_store")
    def test_raises_when_no_regions(self, mock_get_store):
        store = MagicMock()
        store.get_regions_with_data.return_value = []
        mock_get_store.return_value = store
        with pytest.raises(ValueError, match="any region"):
            _predict_advisor(MagicMock()).predict_capacity_windows_all_regions("g5.xlarge")

    @patch("cli.capacity.history.get_capacity_history_store")
    def test_skips_region_with_no_samples(self, mock_get_store):
        store = MagicMock()
        store.get_regions_with_data.return_value = ["us-east-1", "us-west-2"]
        store.get_statistics.side_effect = [
            {"sample_count": 0, "metrics": {}},
            _PREDICT_STATS,
        ]
        store.get_temporal_patterns.return_value = _PREDICT_PATTERNS
        mock_get_store.return_value = store
        client = MagicMock()
        client.converse.return_value = _converse('{"confidence": "low"}')
        results = _predict_advisor(client).predict_capacity_windows_all_regions("g5.xlarge")
        assert [r.region for r in results] == ["us-west-2"]
