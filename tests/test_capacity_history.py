# Tests for cli/capacity/history.py -- Historical Capacity Surface storage layer.
# Pure helpers plus CapacityHistoryStore DynamoDB methods against a MagicMock table.

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from cli.capacity import history as hist

SAMPLE_CAPACITY_DATA = {
    "timestamp": "2025-06-23T14:00:00+00:00",
    "spot_data": {
        "g5.xlarge": {
            "us-east-1": {
                "placement_scores": {"regional": 8},
                "prices": [
                    {"az": "a", "current": 1.0},
                    {"az": "b", "current": 2.0},
                ],
            }
        }
    },
    "cluster_metrics": [{"region": "us-east-1", "queue_depth": 5}],
    "capacity_blocks": {"g5.xlarge": {"us-east-1": [{"az": "a"}, {"az": "b"}]}},
}


@pytest.fixture
def mock_table():
    table = MagicMock()
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = table
        yield table


@pytest.fixture
def store(mock_table):
    return hist.CapacityHistoryStore(table_name="test-table", region="us-east-1", retention_days=90)


class TestPercentile:
    def test_p25(self):
        assert hist._percentile([3.0, 5.0, 7.0, 9.0], 25) == 4.5

    def test_p50(self):
        assert hist._percentile([3.0, 5.0, 7.0, 9.0], 50) == 6.0

    def test_p75(self):
        assert hist._percentile([3.0, 5.0, 7.0, 9.0], 75) == 7.5

    def test_single_element(self):
        assert hist._percentile([42.0], 50) == 42.0


class TestDynamoConversion:
    def test_to_dynamo_float_becomes_decimal(self):
        assert hist._to_dynamo(1.5) == Decimal("1.5")
        assert isinstance(hist._to_dynamo(1.5), Decimal)

    def test_to_dynamo_bool_passes_through(self):
        assert hist._to_dynamo(True) is True

    def test_from_dynamo_integral_is_int(self):
        result = hist._from_dynamo(Decimal("7"))
        assert result == 7
        assert isinstance(result, int)

    def test_from_dynamo_fractional_is_float(self):
        assert hist._from_dynamo(Decimal("7.5")) == 7.5


class TestMakePk:
    def test_make_pk(self):
        assert hist.make_pk("g5.xlarge", "us-east-1") == "g5.xlarge#us-east-1"


class TestFlattenCapacityData:
    def test_flattens_single_pair(self):
        records = hist.flatten_capacity_data(SAMPLE_CAPACITY_DATA)
        assert len(records) == 1
        rec = records[0]
        assert rec["instance_type"] == "g5.xlarge"
        assert rec["region"] == "us-east-1"
        assert rec["spot_score"] == 8
        assert rec["spot_price"] == 1.5
        assert rec["az_count"] == 2
        assert rec["queue_depth"] == 5
        assert rec["capacity_blocks_available"] == 2
        assert rec["capacity_blocks_total"] == 2


class TestPutSnapshot:
    def test_put_snapshot_writes_item(self, store, mock_table):
        now = datetime(2025, 6, 23, 14, 0, 0, tzinfo=UTC)
        store.put_snapshot(
            "g5.xlarge",
            "us-east-1",
            {"spot_score": 8, "spot_price": 1.5, "queue_depth": None},
            now=now,
        )
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert item["pk"] == "g5.xlarge#us-east-1"
        assert isinstance(item["ttl"], int)
        assert item["spot_price"] == Decimal("1.5")
        assert isinstance(item["spot_price"], Decimal)
        assert item["spot_score"] == 8
        assert "queue_depth" not in item


class TestRecord:
    def test_record_writes_one_item(self, store, mock_table):
        now = datetime(2025, 6, 23, 14, 0, 0, tzinfo=UTC)
        written = store.record(SAMPLE_CAPACITY_DATA, now=now)
        assert written == 1
        mock_table.put_item.assert_called_once()


class TestGetTrendPagination:
    def test_get_trend_paginates(self, store, mock_table):
        mock_table.query.side_effect = [
            {"Items": [{"sk": "t1"}], "LastEvaluatedKey": {"pk": "x"}},
            {"Items": [{"sk": "t2"}]},
        ]
        items = store.get_trend("g5.xlarge", "us-east-1")
        assert len(items) == 2
        assert mock_table.query.call_count == 2


class TestGetStatistics:
    def test_statistics_over_fixed_trend(self, store, monkeypatch):
        trend = [{"spot_score": 3}, {"spot_score": 5}, {"spot_score": 7}, {"spot_score": 9}]
        monkeypatch.setattr(store, "get_trend", lambda *a, **k: trend)
        stats = store.get_statistics("g5.xlarge", "us-east-1")
        assert stats["sample_count"] == 4
        metric = stats["metrics"]["spot_score"]
        assert metric["count"] == 4
        assert metric["min"] == 3
        assert metric["max"] == 9
        assert metric["mean"] == 6.0
        assert metric["p25"] == 4.5
        assert metric["p50"] == 6.0
        assert metric["p75"] == 7.5

    def test_statistics_empty_trend(self, store, monkeypatch):
        monkeypatch.setattr(store, "get_trend", lambda *a, **k: [])
        stats = store.get_statistics("g5.xlarge", "us-east-1")
        assert stats["sample_count"] == 0
        assert stats["metrics"] == {}


class TestTemporalPatterns:
    def test_patterns_and_best_windows(self, store, monkeypatch):
        trend = [
            {"timestamp": "2025-06-23T14:00:00+00:00", "spot_score": 8},
            {"timestamp": "2025-06-23T14:30:00+00:00", "spot_score": 9},
            {"timestamp": "2025-06-24T09:00:00+00:00", "spot_score": 2},
        ]
        monkeypatch.setattr(store, "get_trend", lambda *a, **k: trend)
        result = store.get_temporal_patterns("g5.xlarge", "us-east-1")
        assert result["metric"] == "spot_score"
        monday = result["patterns"]["Monday"]
        assert monday[14]["avg"] == 8.5
        assert monday[14]["count"] == 2
        best = result["best_windows"]
        assert best[0]["day"] == "Monday"
        assert best[0]["hour"] == 14
        assert best[0]["avg"] == 8.5
        assert best[0]["avg"] >= best[-1]["avg"]


class TestFactory:
    def test_factory_returns_store(self, mock_table):
        result = hist.get_capacity_history_store(table_name="t", region="us-east-1")
        assert isinstance(result, hist.CapacityHistoryStore)
        assert result.table_name == "t"


class TestGetRegionsWithData:
    def test_distinct_sorted(self, store, mock_table):
        mock_table.query.return_value = {
            "Items": [
                {"region": "us-west-2"},
                {"region": "us-east-1"},
                {"region": "us-west-2"},
            ]
        }
        assert store.get_regions_with_data("g5.xlarge") == ["us-east-1", "us-west-2"]

    def test_empty(self, store, mock_table):
        mock_table.query.return_value = {"Items": []}
        assert store.get_regions_with_data("g5.xlarge") == []

    def test_paginates(self, store, mock_table):
        mock_table.query.side_effect = [
            {"Items": [{"region": "us-east-1"}], "LastEvaluatedKey": {"k": 1}},
            {"Items": [{"region": "eu-west-1"}]},
        ]
        assert store.get_regions_with_data("g5.xlarge") == ["eu-west-1", "us-east-1"]


class TestRegionResolution:
    def test_explicit_region_wins(self, mock_table):
        store = hist.CapacityHistoryStore(table_name="t", region="eu-west-1")
        assert store._region == "eu-west-1"

    def test_env_overrides_config(self, mock_table, monkeypatch):
        monkeypatch.setenv("DYNAMODB_REGION", "ap-south-1")
        store = hist.CapacityHistoryStore(table_name="t")
        assert store._region == "ap-south-1"

    def test_resolves_global_region_from_config(self, mock_table, monkeypatch):
        monkeypatch.delenv("DYNAMODB_REGION", raising=False)
        monkeypatch.delenv("REGION", raising=False)
        with patch("cli.config.get_config") as mock_get_config:
            mock_get_config.return_value.global_region = "us-east-2"
            store = hist.CapacityHistoryStore(table_name="t")
        assert store._region == "us-east-2"

    def test_falls_back_to_us_east_1_when_config_unavailable(self, mock_table, monkeypatch):
        monkeypatch.delenv("DYNAMODB_REGION", raising=False)
        monkeypatch.delenv("REGION", raising=False)
        with patch("cli.config.get_config", side_effect=RuntimeError("boom")):
            store = hist.CapacityHistoryStore(table_name="t")
        assert store._region == "us-east-1"
