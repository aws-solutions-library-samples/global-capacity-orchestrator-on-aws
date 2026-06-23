# Tests for cli/capacity/advisor.py historical enrichment.
# Covers _build_prompt interpretation bands plus the Historical Context block, and
# _gather_historical_context (store-backed build path and graceful empty-on-error).

from unittest.mock import MagicMock, patch

import pytest

from cli.capacity.advisor import BedrockCapacityAdvisor

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
