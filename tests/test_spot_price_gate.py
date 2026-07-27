"""
Tests for gco/services/spot_price_gate.py and its central-queue integration.

Covers submission-time field validation, the TTL price cache, minimum-across-
AZ price resolution, gate decisions (open, closed, unknown price, malformed
record fields), observation-write throttling, the JobStore spot fields
(submit_job storage, record_spot_gate_observation conditions,
_parse_job_item exposure), and process_queued_jobs_once deferring gated jobs
without consuming the apply budget while ungated jobs continue to dispatch.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gco.services.central_queue_worker as worker_module
from gco.services.central_queue_worker import process_queued_jobs_once
from gco.services.spot_price_gate import (
    INSTANCE_TYPE_PATTERN,
    SpotPriceGate,
    should_persist_observation,
    validate_spot_gate_fields,
)


def _price_response(prices: dict[str, list[str]]) -> dict:
    history = []
    for az, az_prices in prices.items():
        for price in az_prices:
            history.append({"AvailabilityZone": az, "SpotPrice": price})
    return {"SpotPriceHistory": history}


class TestValidateSpotGateFields:
    def test_both_absent_is_valid(self):
        assert validate_spot_gate_fields(None, None) is None

    def test_price_without_type_is_rejected(self):
        assert "together" in validate_spot_gate_fields(0.5, None)

    def test_type_without_price_is_rejected(self):
        assert "together" in validate_spot_gate_fields(None, "g5.xlarge")

    def test_valid_pair_passes(self):
        assert validate_spot_gate_fields(0.5, "g5.xlarge") is None

    @pytest.mark.parametrize("price", [0.0, -1.0, 1_000_000.0])
    def test_out_of_range_price_is_rejected(self, price):
        assert "between" in validate_spot_gate_fields(price, "g5.xlarge")

    @pytest.mark.parametrize(
        "instance_type",
        ["", "no-dot", "UPPER.case", "g5.", ".xlarge", "g5.xlarge; DROP TABLE"],
    )
    def test_malformed_instance_types_are_rejected(self, instance_type):
        assert "not a valid" in validate_spot_gate_fields(0.5, instance_type)

    @pytest.mark.parametrize(
        "instance_type",
        ["g5.xlarge", "p6-b200.48xlarge", "trn2.3xlarge", "g4dn.metal"],
    )
    def test_real_instance_type_shapes_match(self, instance_type):
        assert INSTANCE_TYPE_PATTERN.fullmatch(instance_type)


class TestSpotPriceLookup:
    def test_takes_latest_price_per_az_then_min_across_azs(self):
        ec2 = MagicMock()
        # DescribeSpotPriceHistory returns newest-first per AZ.
        ec2.describe_spot_price_history.return_value = _price_response(
            {"us-east-1a": ["0.50", "0.90"], "us-east-1b": ["0.35", "0.30"]}
        )
        gate = SpotPriceGate("us-east-1", ec2_client=ec2)
        assert gate.current_min_spot_price("g5.xlarge") == pytest.approx(0.35)

    def test_cache_prevents_repeat_lookups_within_ttl(self):
        ec2 = MagicMock()
        ec2.describe_spot_price_history.return_value = _price_response({"us-east-1a": ["0.50"]})
        gate = SpotPriceGate("us-east-1", ec2_client=ec2, cache_ttl_seconds=60)
        assert gate.current_min_spot_price("g5.xlarge") == pytest.approx(0.5)
        assert gate.current_min_spot_price("g5.xlarge") == pytest.approx(0.5)
        assert ec2.describe_spot_price_history.call_count == 1

    def test_cache_expires_after_ttl(self):
        ec2 = MagicMock()
        ec2.describe_spot_price_history.return_value = _price_response({"us-east-1a": ["0.50"]})
        gate = SpotPriceGate("us-east-1", ec2_client=ec2, cache_ttl_seconds=0.0)
        gate.current_min_spot_price("g5.xlarge")
        gate.current_min_spot_price("g5.xlarge")
        assert ec2.describe_spot_price_history.call_count == 2

    def test_lookup_failure_returns_none(self):
        ec2 = MagicMock()
        ec2.describe_spot_price_history.side_effect = RuntimeError("throttled")
        gate = SpotPriceGate("us-east-1", ec2_client=ec2)
        assert gate.current_min_spot_price("g5.xlarge") is None

    def test_empty_history_returns_none(self):
        ec2 = MagicMock()
        ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        gate = SpotPriceGate("us-east-1", ec2_client=ec2)
        assert gate.current_min_spot_price("g5.xlarge") is None

    def test_malformed_entries_are_skipped(self):
        ec2 = MagicMock()
        ec2.describe_spot_price_history.return_value = {
            "SpotPriceHistory": [
                {"AvailabilityZone": "", "SpotPrice": "0.10"},
                {"AvailabilityZone": "us-east-1a", "SpotPrice": "bogus"},
                {"AvailabilityZone": "us-east-1b", "SpotPrice": "0.42"},
            ]
        }
        gate = SpotPriceGate("us-east-1", ec2_client=ec2)
        assert gate.current_min_spot_price("g5.xlarge") == pytest.approx(0.42)


class TestGateEvaluation:
    def _gate_with_price(self, price):
        gate = SpotPriceGate("us-east-1", ec2_client=MagicMock())
        gate.current_min_spot_price = MagicMock(return_value=price)
        return gate

    def test_ungated_job_returns_none(self):
        gate = self._gate_with_price(0.10)
        assert gate.evaluate({"job_id": "x"}) is None
        gate.current_min_spot_price.assert_not_called()

    def test_open_gate_when_price_clears_cap(self):
        gate = self._gate_with_price(0.40)
        decision = gate.evaluate({"spot_max_price": "0.50", "spot_instance_type": "g5.xlarge"})
        assert decision is not None and decision.gated is False
        assert decision.observed_price == pytest.approx(0.40)
        assert "clears" in decision.reason

    def test_closed_gate_when_price_above_cap(self):
        gate = self._gate_with_price(0.90)
        decision = gate.evaluate({"spot_max_price": "0.50", "spot_instance_type": "g5.xlarge"})
        assert decision.gated is True
        assert "above" in decision.reason

    def test_exact_cap_dispatches(self):
        gate = self._gate_with_price(0.50)
        decision = gate.evaluate({"spot_max_price": "0.50", "spot_instance_type": "g5.xlarge"})
        assert decision.gated is False

    def test_unknown_price_defers(self):
        gate = self._gate_with_price(None)
        decision = gate.evaluate({"spot_max_price": "0.50", "spot_instance_type": "g5.xlarge"})
        assert decision.gated is True
        assert "unavailable" in decision.reason

    @pytest.mark.parametrize(
        "record",
        [
            {"spot_max_price": "not-a-price", "spot_instance_type": "g5.xlarge"},
            {"spot_max_price": "0.50", "spot_instance_type": None},
            {"spot_max_price": "0.50", "spot_instance_type": ""},
        ],
    )
    def test_malformed_record_fields_gate_closed(self, record):
        gate = self._gate_with_price(0.10)
        decision = gate.evaluate(record)
        assert decision.gated is True
        assert "malformed" in decision.reason


class TestObservationThrottle:
    def test_missing_observation_writes(self):
        assert should_persist_observation({}) is True

    def test_recent_observation_skips(self):
        now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
        job = {"spot_gate_checked_at": (now - timedelta(seconds=10)).isoformat()}
        assert should_persist_observation(job, now=now) is False

    def test_stale_observation_writes(self):
        now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
        job = {"spot_gate_checked_at": (now - timedelta(seconds=120)).isoformat()}
        assert should_persist_observation(job, now=now) is True

    def test_unparseable_timestamp_writes(self):
        assert should_persist_observation({"spot_gate_checked_at": "garbage"}) is True


class TestJobStoreSpotFields:
    def _store(self):
        from gco.services.template_store import JobStore

        with patch("gco.services.template_store.boto3.resource") as mock_resource:
            table = MagicMock()
            mock_resource.return_value.Table.return_value = table
            store = JobStore(table_name="jobs", region="us-east-2")
        return store, table

    def test_submit_job_persists_gate_pair(self):
        store, table = self._store()
        store.submit_job(
            job_id="job-1",
            manifest={"metadata": {"name": "training"}},
            target_region="us-east-1",
            spot_max_price="0.500000",
            spot_instance_type="g5.xlarge",
        )
        item = table.put_item.call_args.kwargs["Item"]
        assert item["spot_max_price"] == "0.500000"
        assert item["spot_instance_type"] == "g5.xlarge"

    def test_submit_job_omits_gate_fields_when_absent(self):
        store, table = self._store()
        store.submit_job(
            job_id="job-1",
            manifest={"metadata": {"name": "training"}},
            target_region="us-east-1",
        )
        item = table.put_item.call_args.kwargs["Item"]
        assert "spot_max_price" not in item
        assert "spot_instance_type" not in item

    def test_parse_job_item_exposes_gate_fields_only_when_present(self):
        store, _ = self._store()
        gated = store._parse_job_item(
            {
                "job_id": "job-1",
                "spot_max_price": "0.500000",
                "spot_instance_type": "g5.xlarge",
                "spot_gate_checked_at": "2026-07-26T12:00:00+00:00",
                "spot_gate_observed_price": "0.610000",
            }
        )
        assert gated["spot_max_price"] == "0.500000"
        assert gated["spot_gate_observed_price"] == "0.610000"
        ungated = store._parse_job_item({"job_id": "job-2"})
        assert "spot_max_price" not in ungated

    def test_record_spot_gate_observation_conditional_on_queued(self):
        from botocore.exceptions import ClientError

        store, table = self._store()
        assert store.record_spot_gate_observation("job-1", observed_price="0.610000") is True
        kwargs = table.update_item.call_args.kwargs
        assert "updated_at" not in kwargs["UpdateExpression"]
        assert "#status = :queued" in kwargs["ConditionExpression"]

        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
        )
        assert store.record_spot_gate_observation("job-1", observed_price="0.610000") is False

    def test_record_spot_gate_observation_raises_on_other_errors(self):
        from botocore.exceptions import ClientError

        store, table = self._store()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
        )
        with pytest.raises(ClientError):
            store.record_spot_gate_observation("job-1", observed_price="0.610000")


def _worker_store(queued_jobs):
    store = MagicMock()
    store.claim_lease_seconds = 300
    store.migrate_legacy_records_for_region.return_value = {
        "evaluated": 0,
        "migrated": 0,
        "failed": 0,
        "complete": True,
    }
    store.get_queued_jobs_for_region.return_value = queued_jobs
    return store


def _open_gate(price=0.10):
    """Gate double whose observed market price is fixed at ``price``."""
    gate = MagicMock()
    gate.evaluate.side_effect = lambda job: _decision_for(job, price)
    return gate


def _decision_for(job, price):
    from gco.services.spot_price_gate import SpotGateDecision

    raw = job.get("spot_max_price")
    if raw is None and job.get("spot_instance_type") is None:
        return None
    max_price = float(raw)
    gated = price > max_price
    return SpotGateDecision(
        gated=gated,
        instance_type=str(job.get("spot_instance_type")),
        max_price=max_price,
        observed_price=price,
        reason="above the cap" if gated else "clears the cap",
    )


class TestWorkerSpotGateIntegration:
    @pytest.mark.asyncio
    async def test_gated_job_is_deferred_without_claiming(self):
        processor = MagicMock()
        processor.region = "us-east-1"
        store = _worker_store(
            [
                {
                    "job_id": "gated-1",
                    "spot_max_price": "0.50",
                    "spot_instance_type": "g5.xlarge",
                }
            ]
        )
        gate = _open_gate(price=0.90)

        polled, processed = await process_queued_jobs_once(
            processor, store, limit=5, spot_gate=gate
        )

        assert polled == 1
        assert processed == [
            {
                "job_id": "gated-1",
                "status": "price_gated",
                "instance_type": "g5.xlarge",
                "max_spot_price": 0.5,
                "observed_spot_price": 0.9,
                "reason": "above the cap",
            }
        ]
        store.claim_job.assert_not_called()
        store.record_spot_gate_observation.assert_called_once_with(
            "gated-1", observed_price="0.900000"
        )

    @pytest.mark.asyncio
    async def test_gated_jobs_do_not_starve_dispatchable_work(self):
        processor = MagicMock()
        processor.region = "us-east-1"
        resource = MagicMock()
        resource.name = "run-me"
        resource.namespace = "gco-jobs"
        resource.uid = "uid-1"
        processor.apply_queued_job.return_value = resource

        store = _worker_store(
            [
                {
                    "job_id": "gated-1",
                    "spot_max_price": "0.50",
                    "spot_instance_type": "g5.xlarge",
                },
                {"job_id": "run-1"},
            ]
        )
        store.claim_job.return_value = {
            "claim_token": "token",
            "claim_generation": 1,
            "manifest": {"metadata": {"name": "run-me"}},
            "namespace": "gco-jobs",
        }
        store.transition_job.side_effect = [
            {"status": "applying"},
            {"status": "pending"},
        ]
        gate = _open_gate(price=0.90)

        with patch.object(worker_module, "_lease_heartbeat", AsyncMock()):
            polled, processed = await process_queued_jobs_once(
                processor, store, limit=1, spot_gate=gate
            )

        # The wider candidate fetch lets run-1 dispatch even though the
        # apply budget is 1 and the gated job sits ahead of it in priority.
        fetch_limit = store.get_queued_jobs_for_region.call_args.args[1]
        assert fetch_limit == 20
        statuses = {entry["job_id"]: entry["status"] for entry in processed}
        assert statuses == {"gated-1": "price_gated", "run-1": "applied"}
        store.claim_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_budget_still_bounds_dispatch(self):
        processor = MagicMock()
        processor.region = "us-east-1"
        resource = MagicMock()
        resource.name = "job"
        resource.namespace = "gco-jobs"
        resource.uid = "uid"
        processor.apply_queued_job.return_value = resource

        store = _worker_store([{"job_id": f"run-{i}"} for i in range(5)])
        store.claim_job.return_value = {
            "claim_token": "token",
            "claim_generation": 1,
            "manifest": {"metadata": {"name": "job"}},
            "namespace": "gco-jobs",
        }
        store.transition_job.side_effect = [
            {"status": "applying"},
            {"status": "pending"},
        ] * 2
        gate = _open_gate(price=0.10)

        with patch.object(worker_module, "_lease_heartbeat", AsyncMock()):
            _, processed = await process_queued_jobs_once(processor, store, limit=2, spot_gate=gate)

        assert len(processed) == 2
        assert store.claim_job.call_count == 2

    @pytest.mark.asyncio
    async def test_open_gate_dispatches_normally(self):
        processor = MagicMock()
        processor.region = "us-east-1"
        resource = MagicMock()
        resource.name = "priced"
        resource.namespace = "gco-jobs"
        resource.uid = "uid"
        processor.apply_queued_job.return_value = resource

        store = _worker_store(
            [
                {
                    "job_id": "priced-1",
                    "spot_max_price": "0.50",
                    "spot_instance_type": "g5.xlarge",
                }
            ]
        )
        store.claim_job.return_value = {
            "claim_token": "token",
            "claim_generation": 1,
            "manifest": {"metadata": {"name": "priced"}},
            "namespace": "gco-jobs",
        }
        store.transition_job.side_effect = [
            {"status": "applying"},
            {"status": "pending"},
        ]
        gate = _open_gate(price=0.20)

        with patch.object(worker_module, "_lease_heartbeat", AsyncMock()):
            _, processed = await process_queued_jobs_once(processor, store, limit=5, spot_gate=gate)

        assert processed[0]["status"] == "applied"
        store.record_spot_gate_observation.assert_not_called()

    @pytest.mark.asyncio
    async def test_observation_write_is_throttled_and_failures_tolerated(self):
        processor = MagicMock()
        processor.region = "us-east-1"
        recent = datetime.now(UTC).isoformat()
        store = _worker_store(
            [
                {
                    "job_id": "recently-checked",
                    "spot_max_price": "0.50",
                    "spot_instance_type": "g5.xlarge",
                    "spot_gate_checked_at": recent,
                },
                {
                    "job_id": "never-checked",
                    "spot_max_price": "0.50",
                    "spot_instance_type": "g5.xlarge",
                },
            ]
        )
        store.record_spot_gate_observation.side_effect = RuntimeError("dynamodb down")
        gate = _open_gate(price=0.90)

        _, processed = await process_queued_jobs_once(processor, store, limit=5, spot_gate=gate)

        # Both jobs defer; only the unchecked one attempts a persisted
        # observation, and its write failure does not fail the pass.
        assert [entry["status"] for entry in processed] == ["price_gated", "price_gated"]
        store.record_spot_gate_observation.assert_called_once()
        assert store.record_spot_gate_observation.call_args.args[0] == "never-checked"

    @pytest.mark.asyncio
    async def test_stop_event_halts_gate_processing(self):
        processor = MagicMock()
        processor.region = "us-east-1"
        store = _worker_store(
            [
                {
                    "job_id": "gated-1",
                    "spot_max_price": "0.50",
                    "spot_instance_type": "g5.xlarge",
                }
            ]
        )
        stop = asyncio.Event()
        stop.set()
        gate = _open_gate(price=0.90)

        polled, processed = await process_queued_jobs_once(
            processor, store, limit=5, stop_event=stop, spot_gate=gate
        )

        assert processed == []
