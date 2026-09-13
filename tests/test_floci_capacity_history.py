"""Floci layer: ``CapacityHistoryStore`` over the production-shaped history table.

``cli/capacity/history.py`` is the read side of the capacity time series the
``capacity-poller`` Lambda writes (its writes already run on the emulator in
``test_floci_lambda_regional.py``). The store's unit tests drive it against a
MagicMock table, which cannot tell a valid ``KeyConditionExpression`` from a
mistyped one, does not know the ``by-timestamp`` GSI's key schema, and never
sees the ``Decimal`` values the resource API returns.

Here the table is created exactly as ``gco/stacks/global_stack.py`` declares
it (``pk``/``sk`` keys, ``by-timestamp`` GSI on ``instance_type`` + ``sk``,
TTL attribute) and the store runs unmodified: snapshots with float metrics go
in through ``Decimal``, the time-window queries use real sort-key comparisons,
the statistics and temporal patterns compute over what DynamoDB returns, and
the cross-Region discovery goes through the GSI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest

from tests._floci import floci_test_markers, unique_name

pytestmark = floci_test_markers()

REGION = "us-east-1"


@pytest.fixture(scope="module")
def history_table(verified_floci_endpoint):
    from cli.capacity.history import GSI_BY_TIMESTAMP

    table_name = unique_name("gcotest-capacity-history")
    dynamodb = boto3.client("dynamodb", region_name=REGION)
    dynamodb.create_table(
        TableName=table_name,
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "instance_type", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": GSI_BY_TIMESTAMP,
                "KeySchema": [
                    {"AttributeName": "instance_type", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.get_waiter("table_exists").wait(TableName=table_name)
    dynamodb.update_time_to_live(
        TableName=table_name,
        TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
    )
    yield table_name
    dynamodb.delete_table(TableName=table_name)


@pytest.fixture()
def store(history_table):
    from cli.capacity.history import CapacityHistoryStore

    return CapacityHistoryStore(table_name=history_table, region=REGION, retention_days=30)


def test_the_global_stack_declares_the_shape_this_module_provisions():
    """Guard the fixture against drifting from the CDK table definition."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "gco" / "stacks" / "global_stack.py"
    ).read_text()
    table = source[source.index('"CapacityHistoryTable"') :]
    assert 'partition_key=dynamodb.Attribute(name="pk"' in table
    assert 'sort_key=dynamodb.Attribute(name="sk"' in table
    assert 'index_name="by-timestamp"' in table
    assert 'time_to_live_attribute="ttl"' in table


class TestSnapshotsOverTheWire:
    def test_a_snapshot_round_trips_with_plain_numbers(self, store):
        instance_type = unique_name("g5.xlarge")
        now = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

        stored = store.put_snapshot(
            instance_type,
            REGION,
            {"spot_score": 7.5, "spot_price": 0.4321, "on_demand_available": None},
            spot_pool="gpu-pool",
            now=now,
        )
        (item,) = store.get_trend(instance_type, REGION, hours_back=24 * 365 * 5)

        assert stored["spot_score"] == 7.5 and type(stored["spot_score"]) is float
        assert item["spot_score"] == 7.5 and type(item["spot_score"]) is float
        assert item["spot_price"] == 0.4321
        assert "on_demand_available" not in item, "an absent metric must never read as zero"
        assert item["spot_pool"] == "gpu-pool"
        assert item["pk"] == f"{instance_type}#{REGION}"
        assert item["sk"] == item["timestamp"] == now.isoformat()
        assert item["ttl"] == int((now + timedelta(days=30)).timestamp())
        assert type(item["ttl"]) is int


class TestWindowedQueries:
    @pytest.fixture()
    def series(self, store):
        """Six hourly snapshots ending now, plus one far outside any window."""
        instance_type = unique_name("p5.48xlarge")
        now = datetime.now(UTC).replace(microsecond=0)
        scores = [3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        for offset, score in enumerate(reversed(scores)):
            moment = now - timedelta(hours=offset)
            store.put_snapshot(
                instance_type,
                REGION,
                {"spot_score": score, "spot_price": 1.0 + score / 10},
                timestamp=moment.isoformat(),
                now=moment,
            )
        # An old snapshot by sort key, written now. Written with ``now`` in
        # the past its TTL would already have expired and the emulator drops
        # expired items (as DynamoDB eventually does); keeping the TTL in the
        # future leaves the window comparison as the only thing excluding it.
        stale = now - timedelta(days=400)
        store.put_snapshot(
            instance_type,
            REGION,
            {"spot_score": 1.0},
            timestamp=stale.isoformat(),
            now=now,
        )
        return {"instance_type": instance_type, "now": now, "scores": scores}

    def test_the_trend_is_oldest_first_and_bounded_by_the_window(self, store, series):
        trend = store.get_trend(series["instance_type"], REGION, hours_back=6)

        assert [item["spot_score"] for item in trend] == series["scores"], (
            "sort-key comparison and ScanIndexForward must give the window oldest first"
        )
        assert all(
            datetime.fromisoformat(item["timestamp"]) >= series["now"] - timedelta(hours=6)
            for item in trend
        )
        wider = store.get_trend(series["instance_type"], REGION, hours_back=24 * 365 * 2)
        assert [item["spot_score"] for item in wider] == [1.0, *series["scores"]]

    def test_statistics_compute_over_what_dynamodb_returned(self, store, series):
        stats = store.get_statistics(series["instance_type"], REGION, hours_back=6)

        assert stats["sample_count"] == 6
        spot = stats["metrics"]["spot_score"]
        assert spot == {
            "count": 6,
            "min": 3.0,
            "max": 8.0,
            "mean": 5.5,
            "p25": 4.25,
            "p50": 5.5,
            "p75": 6.75,
            "stddev": pytest.approx(1.870829, abs=1e-6),
        }
        assert "on_demand_available" not in stats["metrics"]

    def test_temporal_patterns_bucket_by_weekday_and_hour(self, store, series):
        patterns = store.get_temporal_patterns(series["instance_type"], REGION, hours_back=6)

        assert patterns["metric"] == "spot_score"
        assert sum(len(hours) for hours in patterns["patterns"].values()) == 6
        assert patterns["best_windows"][0]["avg"] == 8.0
        assert [window["avg"] for window in patterns["best_windows"]] == sorted(
            [window["avg"] for window in patterns["best_windows"]], reverse=True
        )


class TestCrossRegionDiscovery:
    def test_regions_with_data_come_from_the_timestamp_index(self, store):
        instance_type = unique_name("trn1.32xlarge")
        now = datetime.now(UTC).replace(microsecond=0)
        for region in ("us-west-2", "eu-west-1", "us-west-2"):
            store.put_snapshot(instance_type, region, {"spot_score": 5.0}, now=now)
        long_ago = now - timedelta(days=300)
        store.put_snapshot(
            instance_type,
            "ap-south-1",
            {"spot_score": 5.0},
            timestamp=long_ago.isoformat(),
            now=now,
        )

        assert store.get_regions_with_data(instance_type, hours_back=24) == [
            "eu-west-1",
            "us-west-2",
        ]
        assert store.get_regions_with_data(instance_type, hours_back=24 * 365) == [
            "ap-south-1",
            "eu-west-1",
            "us-west-2",
        ]
        assert store.get_regions_with_data(unique_name("never-polled"), hours_back=24) == []
