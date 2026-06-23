# Tests for lambda/capacity-poller/handler.py -- Historical Capacity Surface poller.
# Drives the EC2 summary helpers and lambda_handler (write path, env guard, isolation).

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from tests._lambda_imports import load_lambda_module


@pytest.fixture
def handler():
    return load_lambda_module("capacity-poller")


def _fake_ec2():
    ec2 = MagicMock()
    ec2.get_spot_placement_scores.return_value = {"SpotPlacementScores": [{"Score": 7}]}
    ec2.describe_spot_price_history.return_value = {
        "SpotPriceHistory": [
            {"AvailabilityZone": "a", "SpotPrice": "1.0"},
            {"AvailabilityZone": "b", "SpotPrice": "2.0"},
        ],
    }
    ec2.describe_capacity_block_offerings.return_value = {
        "CapacityBlockOfferings": [{"InstanceCount": 2}, {"InstanceCount": 3}],
    }
    return ec2


def _fake_boto3(mock_table, ec2):
    fake = MagicMock()
    fake.resource.return_value.Table.return_value = mock_table
    fake.client.return_value = ec2
    return fake


def _set_env(monkeypatch):
    monkeypatch.setenv("CAPACITY_HISTORY_TABLE_NAME", "gco-capacity-history")
    monkeypatch.setenv("WATCH_INSTANCE_TYPES", "g5.xlarge")
    monkeypatch.setenv("ENABLED_REGIONS", "us-east-1")
    monkeypatch.setenv("CAPACITY_HISTORY_RETENTION_DAYS", "90")


class TestSpotPriceSummary:
    def test_mean_and_az_count(self, handler):
        ec2 = _fake_ec2()
        price, az_count = handler._spot_price_summary(ec2, "g5.xlarge")
        assert price == 1.5
        assert az_count == 2


class TestCapacityBlockSummary:
    def test_offering_and_instance_counts(self, handler):
        ec2 = _fake_ec2()
        offerings, total = handler._capacity_block_summary(ec2, "g5.xlarge")
        assert offerings == 2
        assert total == 5


class TestLambdaHandler:
    def test_writes_one_item(self, handler, monkeypatch):
        _set_env(monkeypatch)
        mock_table = MagicMock()
        ec2 = _fake_ec2()
        with patch.object(handler, "boto3", _fake_boto3(mock_table, ec2)):
            result = handler.lambda_handler({}, None)
        assert result["written"] == 1
        assert result["errors"] == 0
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert item["pk"] == "g5.xlarge#us-east-1"
        assert item["spot_score"] == 7
        assert item["spot_price"] == Decimal("1.5")
        assert item["az_count"] == 2
        assert item["capacity_blocks_available"] == 2
        assert item["capacity_blocks_total"] == 5
        assert isinstance(item["ttl"], int)

    def test_missing_table_name_raises(self, handler, monkeypatch):
        monkeypatch.delenv("CAPACITY_HISTORY_TABLE_NAME", raising=False)
        monkeypatch.setenv("WATCH_INSTANCE_TYPES", "g5.xlarge")
        monkeypatch.setenv("ENABLED_REGIONS", "us-east-1")
        with pytest.raises(ValueError):
            handler.lambda_handler({}, None)

    def test_put_item_error_is_isolated(self, handler, monkeypatch):
        _set_env(monkeypatch)
        mock_table = MagicMock()
        mock_table.put_item.side_effect = Exception("boom")
        ec2 = _fake_ec2()
        with patch.object(handler, "boto3", _fake_boto3(mock_table, ec2)):
            result = handler.lambda_handler({}, None)
        assert result["errors"] == 1
        assert result["written"] == 0
