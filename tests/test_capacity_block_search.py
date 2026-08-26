"""
Tests for the Capacity Block search expansion: date-range, duration-range,
multi-region fan-out, instance-type validation, pagination, and the
consolidated ``find_capacity_blocks`` report (CLI + MCP).

Covers:
* ``cli/capacity/blocks.py`` pure helpers — the AWS duration ladder (1-day
  increments to 14 days, then 7-day increments to 182), duration snapping and
  range resolution, hours/days coercion, ISO/date parsing, friendly instance
  normalization (p6-b200 -> p6-b200.48xlarge, p6-b300 -> p6-b300.48xlarge) and
  the UltraServer-only note for the Grace-Blackwell gb200/gb300 superchips,
  upfront-fee parsing, per-hour / per-GPU-hour pricing, and the
  offering de-dup / sort / rank / longest helpers.
* ``CapacityChecker.validate_instance_type`` — known offline spec, alias
  expansion, UltraServer-only families, EC2 InvalidInstanceType vs transient
  API error, and the live GPU-count lookup.
* ``CapacityChecker.list_capacity_block_offerings`` — StartDateRange/EndDateRange
  wiring, NextToken pagination, pricing enrichment, and graceful handling of the
  unsupported-region (ClientError / BotoCoreError) paths.
* ``CapacityChecker.find_capacity_blocks`` — the region x duration sweep, probe
  de-duplication, ranking, longest-block selection, invalid/UltraServer short
  circuits, and the end-to-end acceptance scenario (1x p6-b200.48xlarge across
  four regions for 1-63 days).
* ``CapacityChecker.check_reservation_availability`` — the new explicit regions
  list, parallel fan-out, date window, and day-based duration.
* The ``gco capacity find-blocks`` CLI command and the enhanced
  ``reservation-check`` options, plus the ``find_capacity_blocks`` /
  ``reservation_check`` MCP argv translation.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError
from click.testing import CliRunner

from cli.capacity import blocks
from cli.config import GCOConfig


def _make_checker():
    from cli.capacity import CapacityChecker

    with patch("cli.capacity.checker.get_config") as mock_config:
        mock_config.return_value = MagicMock(spec=GCOConfig, default_region="us-east-1")
        return CapacityChecker()


# =============================================================================
# blocks.py — duration math
# =============================================================================


class TestDurationLadder:
    def test_days_ladder_shape(self):
        days = blocks.capacity_block_duration_days()
        assert days[0] == 1
        assert 14 in days
        assert 21 in days  # first weekly step after the daily range
        assert days[-1] == 182
        # 1..14 contiguous, then 7-day steps.
        assert list(days[:14]) == list(range(1, 15))
        assert all((d - 14) % 7 == 0 for d in days[14:])

    def test_hours_ladder_mirrors_days(self):
        hours = blocks.capacity_block_duration_hours()
        assert hours[0] == 24
        assert hours[-1] == 182 * 24
        assert 63 * 24 in hours  # 63 days = 9 weeks is valid

    def test_is_valid_duration_hours(self):
        assert blocks.is_valid_duration_hours(24)
        assert blocks.is_valid_duration_hours(63 * 24)
        assert not blocks.is_valid_duration_hours(30)
        assert not blocks.is_valid_duration_hours(0)

    def test_snap_below_range_clamps_to_min(self):
        assert blocks.snap_duration_hours(1) == 24

    def test_snap_above_range_clamps_to_max(self):
        assert blocks.snap_duration_hours(10_000) == 182 * 24

    def test_snap_nearest_ties_round_up(self):
        # 36h is exactly between 24h and 48h -> rounds up to 48.
        assert blocks.snap_duration_hours(36) == 48

    def test_snap_picks_closest_valid(self):
        assert blocks.snap_duration_hours(50) == 48
        assert blocks.snap_duration_hours(70) == 72

    def test_coerce_hours_prefers_days(self):
        assert blocks.coerce_hours(24, 3) == 72
        assert blocks.coerce_hours(48, None) == 48
        assert blocks.coerce_hours(None, None) is None

    def test_hours_to_days(self):
        assert blocks.hours_to_days(48) == 2.0
        assert blocks.hours_to_days(36) == 1.5
        assert blocks.hours_to_days(None) is None


class TestResolveSearchDurations:
    def test_default_is_24h(self):
        assert blocks.resolve_search_durations() == [24]

    def test_single_duration_snaps(self):
        assert blocks.resolve_search_durations(duration_hours=50) == [48]

    def test_range_expands_to_valid_values(self):
        result = blocks.resolve_search_durations(min_duration_hours=24, max_duration_hours=72)
        assert result == [24, 48, 72]

    def test_open_ended_min_only(self):
        result = blocks.resolve_search_durations(min_duration_hours=180 * 24)
        assert result == [182 * 24]

    def test_open_ended_max_only(self):
        result = blocks.resolve_search_durations(max_duration_hours=48)
        assert result == [24, 48]

    def test_inverted_range_is_normalized(self):
        result = blocks.resolve_search_durations(min_duration_hours=72, max_duration_hours=24)
        assert result == [24, 48, 72]

    def test_tight_range_with_no_valid_value_snaps(self):
        # 30h..40h straddles no valid duration; falls back to a single snap.
        result = blocks.resolve_search_durations(min_duration_hours=30, max_duration_hours=40)
        assert len(result) == 1
        assert blocks.is_valid_duration_hours(result[0])

    def test_find_longest_sweeps_full_ladder(self):
        result = blocks.resolve_search_durations(find_longest=True)
        assert result == list(blocks.capacity_block_duration_hours())

    def test_find_longest_respects_range(self):
        result = blocks.resolve_search_durations(
            find_longest=True, min_duration_hours=24, max_duration_hours=72
        )
        assert result == [24, 48, 72]

    def test_acceptance_window_1_to_63_days(self):
        result = blocks.resolve_search_durations(min_duration_hours=24, max_duration_hours=63 * 24)
        # 1..14 days plus 21,28,...,63 days.
        assert result[0] == 24
        assert result[-1] == 63 * 24
        assert 14 * 24 in result
        assert 63 * 24 in result


# =============================================================================
# blocks.py — date parsing
# =============================================================================


class TestParseDateInput:
    def test_none_passthrough(self):
        assert blocks.parse_date_input(None) is None

    def test_empty_string(self):
        assert blocks.parse_date_input("   ") is None

    def test_date_only(self):
        dt = blocks.parse_date_input("2026-07-01")
        assert dt == datetime(2026, 7, 1, tzinfo=UTC)

    def test_iso_with_z(self):
        dt = blocks.parse_date_input("2026-07-01T11:30:00Z")
        assert dt == datetime(2026, 7, 1, 11, 30, tzinfo=UTC)

    def test_naive_datetime_gets_utc(self):
        dt = blocks.parse_date_input(datetime(2026, 7, 1, 9, 0))
        assert dt.tzinfo is UTC

    def test_aware_datetime_passthrough(self):
        src = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
        assert blocks.parse_date_input(src) is src

    def test_bad_value_raises(self):
        with pytest.raises(ValueError, match="Invalid date"):
            blocks.parse_date_input("not-a-date")


# =============================================================================
# blocks.py — instance normalization
# =============================================================================


class TestNormalizeInstanceType:
    def test_canonical_unchanged(self):
        assert blocks.normalize_instance_type("p5.48xlarge") == ("p5.48xlarge", None)

    def test_alias_expands(self):
        canonical, note = blocks.normalize_instance_type("p6-b200")
        assert canonical == "p6-b200.48xlarge"
        assert note and "p6-b200.48xlarge" in note

    def test_alias_case_insensitive(self):
        canonical, _ = blocks.normalize_instance_type("P6-B200")
        assert canonical == "p6-b200.48xlarge"

    def test_b300_alias_expands_to_standalone(self):
        # B300 is a standalone EC2 type (p6-b300.48xlarge), like B200 — not
        # UltraServer-only.
        canonical, note = blocks.normalize_instance_type("p6-b300")
        assert canonical == "p6-b300.48xlarge"
        assert note and "p6-b300.48xlarge" in note

    def test_ultraserver_only_gb300(self):
        # The Grace-Blackwell GB300 superchip ships only as P6e-GB300 UltraServers.
        canonical, note = blocks.normalize_instance_type("gb300")
        assert canonical == "gb300"
        assert note and "UltraServer" in note

    def test_ultraserver_only_p6e_gb300(self):
        canonical, note = blocks.normalize_instance_type("p6e-gb300")
        assert canonical == "p6e-gb300"
        assert note and "UltraServer" in note

    def test_unknown_passthrough(self):
        assert blocks.normalize_instance_type("zz9.fake") == ("zz9.fake", None)


# =============================================================================
# blocks.py — pricing
# =============================================================================


class TestPricing:
    def test_parse_upfront_fee_string(self):
        assert blocks.parse_upfront_fee("4500.00") == 4500.0

    def test_parse_upfront_fee_number(self):
        assert blocks.parse_upfront_fee(4500) == 4500.0

    def test_parse_upfront_fee_bad(self):
        assert blocks.parse_upfront_fee("free") is None
        assert blocks.parse_upfront_fee(None) is None
        assert blocks.parse_upfront_fee(True) is None

    def test_compute_pricing_full(self):
        result = blocks.compute_offering_pricing("4800.00", 24, 1, 8)
        assert result["upfront_fee_usd"] == 4800.0
        assert result["price_per_hour"] == 200.0
        assert result["price_per_instance_hour"] == 200.0
        assert result["price_per_gpu_hour"] == 25.0

    def test_compute_pricing_multi_instance(self):
        result = blocks.compute_offering_pricing("9600.00", 24, 2, 8)
        assert result["price_per_hour"] == 400.0  # whole block (2 instances) / hour
        assert result["price_per_instance_hour"] == 200.0
        assert result["price_per_gpu_hour"] == 25.0

    def test_compute_pricing_no_gpu_count(self):
        result = blocks.compute_offering_pricing("4800.00", 24, 1, None)
        assert result["price_per_instance_hour"] == 200.0
        assert result["price_per_gpu_hour"] is None

    def test_compute_pricing_missing_fee(self):
        result = blocks.compute_offering_pricing(None, 24, 1, 8)
        assert result["upfront_fee_usd"] is None
        assert result["price_per_hour"] is None

    def test_compute_pricing_zero_duration(self):
        result = blocks.compute_offering_pricing("100", 0, 1, 8)
        assert result["price_per_hour"] is None


# =============================================================================
# blocks.py — dedupe / sort / rank
# =============================================================================


def _off(region, az, oid, dur_h=24, start="2026-07-01T11:30:00", gpu_hr=None, fee=1000.0):
    return {
        "type": "capacity_block",
        "offering_id": oid,
        "instance_type": "p5.48xlarge",
        "availability_zone": az,
        "region": region,
        "instance_count": 1,
        "duration_hours": dur_h,
        "duration_days": round(dur_h / 24, 2),
        "start_date": start,
        "upfront_fee": str(fee),
        "upfront_fee_usd": fee,
        "price_per_hour": round(fee / dur_h, 4),
        "price_per_instance_hour": round(fee / dur_h, 4),
        "price_per_gpu_hour": gpu_hr,
        "gpus_per_instance": 8,
    }


class TestDedupeSortRank:
    def test_offering_identity_prefers_id(self):
        assert blocks.offering_identity({"offering_id": "cbo-1"}) == ("id", "cbo-1")

    def test_offering_identity_fallback_tuple(self):
        ident = blocks.offering_identity(
            {
                "region": "us-east-1",
                "availability_zone": "a",
                "start_date": "x",
                "duration_hours": 24,
            }
        )
        assert ident[0] == "tuple"

    def test_dedupe_by_offering_id(self):
        offerings = [_off("us-east-1", "a", "cbo-1"), _off("us-east-1", "a", "cbo-1")]
        assert len(blocks.dedupe_offerings(offerings)) == 1

    def test_dedupe_keeps_distinct(self):
        offerings = [_off("us-east-1", "a", "cbo-1"), _off("us-east-1", "a", "cbo-2")]
        assert len(blocks.dedupe_offerings(offerings)) == 2

    def test_sort_by_region_az_start(self):
        offerings = [
            _off("us-west-2", "us-west-2a", "cbo-3"),
            _off("us-east-1", "us-east-1b", "cbo-2"),
            _off("us-east-1", "us-east-1a", "cbo-1"),
        ]
        ordered = blocks.sort_offerings(offerings)
        assert [o["offering_id"] for o in ordered] == ["cbo-1", "cbo-2", "cbo-3"]

    def test_rank_cheapest_gpu_hour_first(self):
        offerings = [
            _off("us-east-1", "a", "cbo-expensive", gpu_hr=30.0),
            _off("us-east-1", "a", "cbo-cheap", gpu_hr=10.0),
            _off("us-east-1", "a", "cbo-none", gpu_hr=None),
        ]
        ranked = blocks.rank_offerings(offerings)
        assert ranked[0]["offering_id"] == "cbo-cheap"
        assert ranked[-1]["offering_id"] == "cbo-none"  # missing price sorts last

    def test_longest_offering(self):
        offerings = [
            _off("us-east-1", "a", "cbo-short", dur_h=24),
            _off("us-east-1", "a", "cbo-long", dur_h=63 * 24),
        ]
        longest = blocks.longest_offering(offerings)
        assert longest["offering_id"] == "cbo-long"

    def test_longest_offering_empty(self):
        assert blocks.longest_offering([]) is None


# =============================================================================
# CapacityChecker.validate_instance_type
# =============================================================================


class TestValidateInstanceType:
    @staticmethod
    def _checker_describing_8_gpus():
        """A checker whose EC2 client reports an 8-GPU type.

        These cases used to need no mock because a checked-in offline catalog
        answered them without touching AWS. That catalog is gone, so the EC2 call
        must be stubbed — otherwise the test passes only on a machine that
        happens to have credentials and silently makes a live API call in CI.
        """
        checker = _make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.describe_instance_types.return_value = {
            "InstanceTypes": [
                {
                    "GpuInfo": {
                        "Gpus": [{"Count": 8, "Name": "H100", "Manufacturer": "NVIDIA"}],
                    }
                }
            ]
        }
        checker._session.client = MagicMock(return_value=mock_ec2)
        return checker

    def test_confirmed_type_is_valid_and_known(self):
        checker = self._checker_describing_8_gpus()
        result = checker.validate_instance_type("p5.48xlarge")
        assert result["valid"] is True
        assert result["known"] is True
        assert result["gpu_count"] == 8
        assert result["instance_type"] == "p5.48xlarge"

    def test_alias_expands_and_is_known(self):
        checker = self._checker_describing_8_gpus()
        result = checker.validate_instance_type("p6-b200")
        assert result["instance_type"] == "p6-b200.48xlarge"
        assert result["valid"] is True
        assert result["known"] is True
        assert result["gpu_count"] == 8
        assert "p6-b200.48xlarge" in result["note"]
        # The canonical name is what reaches EC2, not the friendly alias.
        checker._session.client.return_value.describe_instance_types.assert_called_once_with(
            InstanceTypes=["p6-b200.48xlarge"]
        )

    def test_b300_is_valid_standalone(self):
        checker = self._checker_describing_8_gpus()
        result = checker.validate_instance_type("p6-b300")
        assert result["instance_type"] == "p6-b300.48xlarge"
        assert result["valid"] is True
        assert result["known"] is True
        assert result["gpu_count"] == 8

    def test_ultraserver_only_is_invalid(self):
        checker = _make_checker()
        result = checker.validate_instance_type("gb300")
        assert result["valid"] is False
        assert "UltraServer" in result["note"]

    def test_invalid_type_via_ec2(self):
        checker = _make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.describe_instance_types.side_effect = ClientError(
            {"Error": {"Code": "InvalidInstanceType", "Message": "bad"}},
            "DescribeInstanceTypes",
        )
        checker._session.client = MagicMock(return_value=mock_ec2)
        result = checker.validate_instance_type("zz9.fake")
        assert result["valid"] is False

    def test_ec2_confirmation_marks_the_type_known(self):
        """``known`` now means "EC2 described it", not "it is in our catalog".

        The hardcoded GPU catalog that used to define ``known`` was removed, so
        the flag reflects live confirmation. It is reported for information only
        and gates nothing.
        """
        checker = _make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.describe_instance_types.return_value = {
            "InstanceTypes": [{"GpuInfo": {"Gpus": [{"Count": 4}]}}]
        }
        checker._session.client = MagicMock(return_value=mock_ec2)
        result = checker.validate_instance_type("g6.48xlarge")
        assert result["valid"] is True
        assert result["known"] is True
        assert result["gpu_count"] == 4

    def test_gpu_count_sums_across_accelerator_models(self):
        """A heterogeneous Gpus[] list must not be read as Gpus[0]."""
        checker = _make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.describe_instance_types.return_value = {
            "InstanceTypes": [{"GpuInfo": {"Gpus": [{"Count": 4}, {"Count": 2}]}}]
        }
        checker._session.client = MagicMock(return_value=mock_ec2)
        assert checker.validate_instance_type("g6.48xlarge")["gpu_count"] == 6

    def test_empty_instance_types_is_valid_but_not_offered_here(self):
        """An empty result means "not offered in this region", not "bogus type".

        EC2 raises InvalidInstanceType for a type that does not exist; it returns
        an empty list for a real type the consulted region does not offer.
        Collapsing the two would reject a p5 in a region that merely lacks it —
        which the removed hardcoded catalog used to paper over by short-circuiting
        the lookup entirely.
        """
        checker = _make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.describe_instance_types.return_value = {"InstanceTypes": []}
        checker._session.client = MagicMock(return_value=mock_ec2)
        result = checker.validate_instance_type("p5.48xlarge")
        assert result["valid"] is True
        assert result["known"] is False
        assert "not offered" in (result["note"] or "")

    def test_transient_api_error_leaves_unverified(self):
        checker = _make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.describe_instance_types.side_effect = ClientError(
            {"Error": {"Code": "Throttling", "Message": "slow"}}, "DescribeInstanceTypes"
        )
        checker._session.client = MagicMock(return_value=mock_ec2)
        result = checker.validate_instance_type("g6.unknown")
        assert result["valid"] is True
        assert "Could not verify" in result["note"]

    def test_generic_exception_leaves_unverified(self):
        checker = _make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.describe_instance_types.side_effect = RuntimeError("boom")
        checker._session.client = MagicMock(return_value=mock_ec2)
        result = checker.validate_instance_type("g6.unknown")
        assert result["valid"] is True
        assert "Could not verify" in result["note"]


# =============================================================================
# CapacityChecker.list_capacity_block_offerings — date range + pagination
# =============================================================================


class TestListCapacityBlockOfferingsEnhanced:
    def _ec2_with(self, *responses):
        mock_ec2 = MagicMock()
        mock_ec2.describe_capacity_block_offerings.side_effect = list(responses)
        return mock_ec2

    def test_date_range_threaded_to_api(self):
        checker = _make_checker()
        mock_ec2 = self._ec2_with({"CapacityBlockOfferings": []})
        checker._session.client = MagicMock(return_value=mock_ec2)
        checker.list_capacity_block_offerings(
            "us-east-1",
            "p5.48xlarge",
            earliest_start=datetime(2026, 7, 1, tzinfo=UTC),
            latest_start=datetime(2026, 7, 10, tzinfo=UTC),
        )
        kwargs = mock_ec2.describe_capacity_block_offerings.call_args[1]
        assert kwargs["StartDateRange"] == datetime(2026, 7, 1, tzinfo=UTC)
        assert kwargs["EndDateRange"] == datetime(2026, 7, 10, tzinfo=UTC)

    def test_pagination_follows_next_token(self):
        checker = _make_checker()
        page1 = {
            "CapacityBlockOfferings": [
                {
                    "CapacityBlockOfferingId": "cbo-1",
                    "InstanceType": "p5.48xlarge",
                    "AvailabilityZone": "us-east-1a",
                    "InstanceCount": 1,
                    "CapacityBlockDurationHours": 24,
                    "StartDate": datetime(2026, 7, 1, 11, 30, tzinfo=UTC),
                    "UpfrontFee": "4800.00",
                }
            ],
            "NextToken": "page2",
        }
        page2 = {
            "CapacityBlockOfferings": [
                {
                    "CapacityBlockOfferingId": "cbo-2",
                    "InstanceType": "p5.48xlarge",
                    "AvailabilityZone": "us-east-1b",
                    "InstanceCount": 1,
                    "CapacityBlockDurationHours": 24,
                    "StartDate": datetime(2026, 7, 2, 11, 30, tzinfo=UTC),
                    "UpfrontFee": "4900.00",
                }
            ]
        }
        mock_ec2 = self._ec2_with(page1, page2)
        checker._session.client = MagicMock(return_value=mock_ec2)
        offerings = checker.list_capacity_block_offerings("us-east-1", "p5.48xlarge")
        assert len(offerings) == 2
        assert mock_ec2.describe_capacity_block_offerings.call_count == 2
        # Second call carried the NextToken.
        second_kwargs = mock_ec2.describe_capacity_block_offerings.call_args_list[1][1]
        assert second_kwargs["NextToken"] == "page2"

    def test_pricing_enrichment(self):
        checker = _make_checker()
        mock_ec2 = self._ec2_with(
            {
                "CapacityBlockOfferings": [
                    {
                        "CapacityBlockOfferingId": "cbo-1",
                        "InstanceType": "p5.48xlarge",
                        "AvailabilityZone": "us-east-1a",
                        "InstanceCount": 1,
                        "CapacityBlockDurationHours": 24,
                        "StartDate": datetime(2026, 7, 1, 11, 30, tzinfo=UTC),
                        "UpfrontFee": "4800.00",
                    }
                ]
            }
        )
        # GPUs per instance is resolved live now that the offline catalog is gone,
        # so the same mock client answers DescribeInstanceTypes too.
        mock_ec2.describe_instance_types.return_value = {
            "InstanceTypes": [
                {
                    "InstanceType": "p5.48xlarge",
                    "VCpuInfo": {"DefaultVCpus": 192},
                    "MemoryInfo": {"SizeInMiB": 2097152},
                    "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
                    "GpuInfo": {
                        "Gpus": [
                            {
                                "Name": "H100",
                                "Manufacturer": "NVIDIA",
                                "Count": 8,
                                "MemoryInfo": {"SizeInMiB": 81920},
                            }
                        ],
                        "TotalGpuMemoryInMiB": 655360,
                    },
                }
            ]
        }
        checker._session.client = MagicMock(return_value=mock_ec2)
        offerings = checker.list_capacity_block_offerings("us-east-1", "p5.48xlarge")
        o = offerings[0]
        # p5.48xlarge = 8 GPUs, per the live description above.
        assert o["upfront_fee"] == "4800.00"  # raw preserved
        assert o["upfront_fee_usd"] == 4800.0
        assert o["price_per_hour"] == 200.0
        assert o["price_per_gpu_hour"] == 25.0
        assert o["duration_days"] == 1.0
        assert o["gpus_per_instance"] == 8

    def test_duration_minutes_fallback(self):
        checker = _make_checker()
        mock_ec2 = self._ec2_with(
            {
                "CapacityBlockOfferings": [
                    {
                        "CapacityBlockOfferingId": "cbo-1",
                        "InstanceType": "p5.48xlarge",
                        "AvailabilityZone": "us-east-1a",
                        "InstanceCount": 1,
                        "CapacityBlockDurationMinutes": 1440,
                        "StartDate": datetime(2026, 7, 1, 11, 30, tzinfo=UTC),
                        "UpfrontFee": "4800.00",
                    }
                ]
            }
        )
        checker._session.client = MagicMock(return_value=mock_ec2)
        offerings = checker.list_capacity_block_offerings("us-east-1", "p5.48xlarge")
        assert offerings[0]["duration_hours"] == 24

    def test_explicit_gpus_per_instance_used(self):
        checker = _make_checker()
        mock_ec2 = self._ec2_with(
            {
                "CapacityBlockOfferings": [
                    {
                        "CapacityBlockOfferingId": "cbo-1",
                        "InstanceType": "unknown.type",
                        "AvailabilityZone": "us-east-1a",
                        "InstanceCount": 1,
                        "CapacityBlockDurationHours": 24,
                        "StartDate": datetime(2026, 7, 1, 11, 30, tzinfo=UTC),
                        "UpfrontFee": "2400.00",
                    }
                ]
            }
        )
        checker._session.client = MagicMock(return_value=mock_ec2)
        offerings = checker.list_capacity_block_offerings(
            "us-east-1", "unknown.type", gpus_per_instance=4
        )
        assert offerings[0]["price_per_gpu_hour"] == 25.0

    def test_unsupported_region_clienterror_returns_empty(self):
        checker = _make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.describe_capacity_block_offerings.side_effect = ClientError(
            {"Error": {"Code": "Unsupported", "Message": "no"}},
            "DescribeCapacityBlockOfferings",
        )
        checker._session.client = MagicMock(return_value=mock_ec2)
        assert checker.list_capacity_block_offerings("ap-south-1", "p5.48xlarge") == []

    def test_unsupported_region_invalidaction_returns_empty(self):
        # eu-west-1 returns InvalidAction (the Capacity Block API isn't available
        # there); treated as an expected unsupported-region signal, so it returns
        # [] quietly rather than raising or logging a warning.
        checker = _make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.describe_capacity_block_offerings.side_effect = ClientError(
            {"Error": {"Code": "InvalidAction", "Message": "not valid for this web service"}},
            "DescribeCapacityBlockOfferings",
        )
        checker._session.client = MagicMock(return_value=mock_ec2)
        assert checker.list_capacity_block_offerings("eu-west-1", "p5.48xlarge") == []

    def test_adaptive_retry_config_applied(self):
        # The capacity-block client uses adaptive retries so the parallel sweep
        # degrades gracefully under RequestLimitExceeded instead of failing fast.
        checker = _make_checker()
        mock_ec2 = self._ec2_with({"CapacityBlockOfferings": []})
        checker._session.client = MagicMock(return_value=mock_ec2)
        checker.list_capacity_block_offerings("us-east-1", "p5.48xlarge")
        # Resolving GPUs per instance opens its own plain client, so pick the
        # capacity-block call by the kwarg under test rather than trusting the
        # call order.
        cfg = next(
            call.kwargs["config"]
            for call in checker._session.client.call_args_list
            if "config" in call.kwargs
        )
        assert cfg.retries["mode"] == "adaptive"
        assert cfg.retries["max_attempts"] == 10

    def test_botocore_error_returns_empty(self):
        checker = _make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.describe_capacity_block_offerings.side_effect = BotoCoreError()
        checker._session.client = MagicMock(return_value=mock_ec2)
        assert checker.list_capacity_block_offerings("cn-north-1", "p5.48xlarge") == []

    def test_unexpected_clienterror_logged_returns_empty(self):
        checker = _make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.describe_capacity_block_offerings.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "boom"}},
            "DescribeCapacityBlockOfferings",
        )
        checker._session.client = MagicMock(return_value=mock_ec2)
        assert checker.list_capacity_block_offerings("us-east-1", "p5.48xlarge") == []


# =============================================================================
# CapacityChecker.find_capacity_blocks
# =============================================================================


def _enriched(region, az, oid, dur_h, start, fee_usd, gpu_hr, count=1):
    return {
        "type": "capacity_block",
        "offering_id": oid,
        "instance_type": "p6-b200.48xlarge",
        "availability_zone": az,
        "region": region,
        "instance_count": count,
        "duration_hours": dur_h,
        "duration_days": round(dur_h / 24, 2),
        "start_date": start,
        "end_date": None,
        "upfront_fee": str(fee_usd),
        "upfront_fee_usd": float(fee_usd),
        "price_per_hour": round(fee_usd / dur_h, 4),
        "price_per_instance_hour": round(fee_usd / dur_h / count, 4),
        "price_per_gpu_hour": gpu_hr,
        "gpus_per_instance": 8,
        "currency": "USD",
        "tenancy": "default",
    }


class TestFindCapacityBlocks:
    def test_invalid_instance_type_short_circuits(self):
        checker = _make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.describe_instance_types.side_effect = ClientError(
            {"Error": {"Code": "InvalidInstanceType", "Message": "bad"}},
            "DescribeInstanceTypes",
        )
        checker._session.client = MagicMock(return_value=mock_ec2)
        with patch.object(checker, "list_capacity_block_offerings") as mock_list:
            report = checker.find_capacity_blocks("zz9.fake", regions=["us-east-1"])
        assert report["valid_instance_type"] is False
        assert report["offerings_found"] == 0
        mock_list.assert_not_called()

    def test_ultraserver_only_short_circuits_with_note(self):
        checker = _make_checker()
        with patch.object(checker, "list_capacity_block_offerings") as mock_list:
            report = checker.find_capacity_blocks("gb300", regions=["us-east-1"])
        assert report["valid_instance_type"] is False
        assert "UltraServer" in report["recommendation"]
        mock_list.assert_not_called()

    def test_multi_region_multi_duration_dedupes_and_ranks(self):
        checker = _make_checker()

        def fake_list(region, instance_type, *, instance_count, duration_hours, **kwargs):
            # Same offering id returned across two adjacent duration probes in
            # us-east-1; a different, cheaper one in us-west-2.
            if region == "us-east-1":
                return [
                    _enriched(
                        "us-east-1",
                        "us-east-1a",
                        "cbo-east",
                        duration_hours,
                        "2026-07-01T11:30:00",
                        fee_usd=duration_hours * 100,
                        gpu_hr=30.0,
                    )
                ]
            if region == "us-west-2":
                return [
                    _enriched(
                        "us-west-2",
                        "us-west-2a",
                        "cbo-west",
                        duration_hours,
                        "2026-07-02T11:30:00",
                        fee_usd=duration_hours * 50,
                        gpu_hr=10.0,
                    )
                ]
            return []

        with patch.object(checker, "list_capacity_block_offerings", side_effect=fake_list):
            report = checker.find_capacity_blocks(
                "p5.48xlarge",
                regions=["us-east-1", "us-west-2", "eu-west-1"],
                min_duration_hours=24,
                max_duration_hours=48,
            )
        # cbo-east and cbo-west each appear once despite two duration probes.
        assert report["offerings_found"] == 2
        assert report["regions_with_offerings"] == ["us-east-1", "us-west-2"]
        assert report["best"]["offering_id"] == "cbo-west"  # cheaper per GPU-hr
        assert report["valid_instance_type"] is True
        assert "Found 2 Capacity Block offering(s)" in report["recommendation"]

    def test_longest_block_surfaced(self):
        checker = _make_checker()

        def fake_list(region, instance_type, *, instance_count, duration_hours, **kwargs):
            return [
                _enriched(
                    "us-east-1",
                    "us-east-1a",
                    f"cbo-{duration_hours}",
                    duration_hours,
                    "2026-07-01T11:30:00",
                    fee_usd=duration_hours * 100,
                    gpu_hr=20.0,
                )
            ]

        with patch.object(checker, "list_capacity_block_offerings", side_effect=fake_list):
            report = checker.find_capacity_blocks(
                "p5.48xlarge",
                regions=["us-east-1"],
                min_duration_hours=24,
                max_duration_hours=72,
                find_longest=True,
            )
        assert report["longest"]["duration_hours"] == 72

    def test_probe_exception_does_not_abort_sweep(self):
        checker = _make_checker()

        def fake_list(region, instance_type, *, instance_count, duration_hours, **kwargs):
            if region == "us-east-1":
                raise RuntimeError("transient")
            return [
                _enriched(
                    "us-west-2",
                    "us-west-2a",
                    "cbo-west",
                    duration_hours,
                    "2026-07-02T11:30:00",
                    fee_usd=2400,
                    gpu_hr=12.5,
                )
            ]

        with patch.object(checker, "list_capacity_block_offerings", side_effect=fake_list):
            report = checker.find_capacity_blocks("p5.48xlarge", regions=["us-east-1", "us-west-2"])
        assert report["offerings_found"] == 1
        assert report["regions_with_offerings"] == ["us-west-2"]

    def test_no_offerings_recommendation(self):
        checker = _make_checker()
        with patch.object(checker, "list_capacity_block_offerings", return_value=[]):
            report = checker.find_capacity_blocks("p5.48xlarge", regions=["us-east-1"])
        assert report["offerings_found"] == 0
        assert "No Capacity Block offerings" in report["recommendation"]

    def test_default_regions_discovered_when_omitted(self):
        checker = _make_checker()
        with (
            patch("cli.aws_client.get_aws_client") as mock_get,
            patch.object(checker, "list_capacity_block_offerings", return_value=[]),
        ):
            mock_get.return_value.discover_regional_stacks.return_value = {"us-east-1": MagicMock()}
            report = checker.find_capacity_blocks("p5.48xlarge")
        assert report["regions_checked"] == ["us-east-1"]

    def test_acceptance_scenario_p6_b200_four_regions(self):
        """One call answers the motivating scenario end-to-end."""
        checker = _make_checker()

        def fake_list(region, instance_type, *, instance_count, duration_hours, **kwargs):
            assert instance_type == "p6-b200.48xlarge"  # alias normalized
            # Only us-east-2 has a 63-day block in the window.
            if region == "us-east-2" and duration_hours == 63 * 24:
                return [
                    _enriched(
                        "us-east-2",
                        "us-east-2a",
                        "cbo-b200",
                        63 * 24,
                        "2026-07-03T11:30:00",
                        fee_usd=63 * 24 * 200,
                        gpu_hr=25.0,
                    )
                ]
            return []

        with patch.object(checker, "list_capacity_block_offerings", side_effect=fake_list):
            report = checker.find_capacity_blocks(
                "p6-b200",
                regions=["us-east-1", "us-east-2", "us-west-2", "eu-west-1"],
                instance_count=1,
                min_duration_days=1,
                max_duration_days=63,
                earliest_start="2026-07-01",
                latest_start="2026-07-10",
            )
        assert report["instance_type"] == "p6-b200.48xlarge"
        assert report["valid_instance_type"] is True
        assert report["offerings_found"] == 1
        assert report["best"]["region"] == "us-east-2"
        assert report["best"]["price_per_gpu_hour"] == 25.0
        assert report["date_window"]["earliest_start"].startswith("2026-07-01")
        assert report["date_window"]["latest_start"].startswith("2026-07-10")
        # The 63-day duration was among those probed.
        assert 63 * 24 in report["durations_probed_hours"]


# =============================================================================
# CapacityChecker.check_reservation_availability — multi-region + date window
# =============================================================================


class TestCheckReservationAvailabilityEnhanced:
    def test_explicit_regions_list_queried_in_parallel(self):
        checker = _make_checker()

        def fake_res(region, instance_type=None):
            if region == "us-west-2":
                return [{"available_instances": 1, "total_instances": 2}]
            return []

        checker.list_capacity_reservations = MagicMock(side_effect=fake_res)
        checker.list_capacity_block_offerings = MagicMock(return_value=[])

        result = checker.check_reservation_availability(
            "p5.48xlarge",
            regions=["us-east-1", "us-west-2", "eu-west-1"],
            include_capacity_blocks=False,
        )
        assert result["regions_checked"] == ["us-east-1", "us-west-2", "eu-west-1"]
        assert result["odcr"]["total_available_instances"] == 1
        # One ODCR query per region.
        assert checker.list_capacity_reservations.call_count == 3

    def test_block_duration_days_overrides_hours(self):
        checker = _make_checker()
        checker.list_capacity_reservations = MagicMock(return_value=[])
        captured = {}

        def fake_blocks(region, instance_type, instance_count, duration_hours, **kwargs):
            captured["duration_hours"] = duration_hours
            captured["earliest_start"] = kwargs.get("earliest_start")
            return []

        checker.list_capacity_block_offerings = MagicMock(side_effect=fake_blocks)
        result = checker.check_reservation_availability(
            "p5.48xlarge",
            regions=["us-east-1"],
            block_duration_days=14,
            earliest_start="2026-07-01",
        )
        assert captured["duration_hours"] == 14 * 24
        assert captured["earliest_start"] == datetime(2026, 7, 1, tzinfo=UTC)
        assert result["capacity_blocks"]["duration_hours"] == 14 * 24
        assert result["capacity_blocks"]["date_window"]["earliest_start"].startswith("2026-07-01")

    def test_cheapest_block_uses_parsed_fee(self):
        checker = _make_checker()
        checker.list_capacity_reservations = MagicMock(return_value=[])
        checker.list_capacity_block_offerings = MagicMock(
            return_value=[
                {
                    "offering_id": "cbo-1",
                    "availability_zone": "us-east-1a",
                    "upfront_fee": "5000.00",
                    "upfront_fee_usd": 5000.0,
                    "region": "us-east-1",
                },
                {
                    "offering_id": "cbo-2",
                    "availability_zone": "us-east-1b",
                    "upfront_fee": "3000.00",
                    "upfront_fee_usd": 3000.0,
                    "region": "us-east-1",
                },
            ]
        )
        result = checker.check_reservation_availability("p5.48xlarge", regions=["us-east-1"])
        assert result["capacity_blocks"]["has_offerings"] is True
        assert "$3000.0" in result["recommendation"]


# =============================================================================
# CLI — gco capacity find-blocks + reservation-check options
# =============================================================================


class TestFindBlocksCLI:
    def _report(self, **overrides):
        report = {
            "instance_type": "p6-b200.48xlarge",
            "requested_instance_type": "p6-b200",
            "valid_instance_type": True,
            "known_instance_type": True,
            "note": "Interpreted 'p6-b200' as 'p6-b200.48xlarge'.",
            "instance_count": 1,
            "regions_checked": ["us-east-1", "us-west-2"],
            "durations_probed_hours": [24, 48, 63 * 24],
            "durations_probed_days": [1.0, 2.0, 63.0],
            "date_window": {
                "earliest_start": "2026-07-01T00:00:00+00:00",
                "latest_start": "2026-07-10T00:00:00+00:00",
            },
            "offerings_found": 1,
            "offerings": [
                _enriched(
                    "us-east-1",
                    "us-east-1a",
                    "cbo-1",
                    63 * 24,
                    "2026-07-03T11:30:00",
                    302400.0,
                    25.0,
                )
            ],
            "ranked": [],
            "best": _enriched(
                "us-east-1",
                "us-east-1a",
                "cbo-1",
                63 * 24,
                "2026-07-03T11:30:00",
                302400.0,
                25.0,
            ),
            "longest": None,
            "regions_with_offerings": ["us-east-1"],
            "recommendation": "Found 1 Capacity Block offering(s) for p6-b200.48xlarge.",
        }
        report.update(overrides)
        return report

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_find_blocks_table(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.find_capacity_blocks.return_value = self._report()

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "capacity",
                "find-blocks",
                "-i",
                "p6-b200",
                "-r",
                "us-east-1",
                "-r",
                "us-west-2",
                "--min-duration-days",
                "1",
                "--max-duration-days",
                "63",
                "--earliest-start",
                "2026-07-01",
                "--latest-start",
                "2026-07-10",
            ],
        )
        assert result.exit_code == 0
        assert "us-east-1a" in result.output
        assert "$/GPU-hr" in result.output
        # Verify the checker received the parsed kwargs.
        call = mock_checker_fn.return_value.find_capacity_blocks.call_args
        assert call.kwargs["regions"] == ["us-east-1", "us-west-2"]
        assert call.kwargs["min_duration_days"] == 1
        assert call.kwargs["max_duration_days"] == 63
        assert call.kwargs["earliest_start"] == "2026-07-01"

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_find_blocks_invalid_type(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.find_capacity_blocks.return_value = self._report(
            valid_instance_type=False,
            offerings=[],
            best=None,
            recommendation="gb300 is not a standalone EC2 instance type.",
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "find-blocks", "-i", "gb300"])
        assert result.exit_code == 0
        assert "not a standalone EC2 instance type" in result.output

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_find_blocks_no_offerings(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.find_capacity_blocks.return_value = self._report(
            offerings_found=0,
            offerings=[],
            best=None,
            regions_with_offerings=[],
            recommendation="No Capacity Block offerings for p6-b200.48xlarge ...",
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "find-blocks", "-i", "p6-b200", "-r", "us-east-1"])
        assert result.exit_code == 0
        assert "No Capacity Block offerings" in result.output

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_find_blocks_json(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        fmt = MagicMock()
        mock_fmt_fn.return_value = fmt
        report = self._report()
        mock_checker_fn.return_value.find_capacity_blocks.return_value = report

        runner = CliRunner()
        result = runner.invoke(
            cli, ["--output", "json", "capacity", "find-blocks", "-i", "p6-b200", "-r", "us-east-1"]
        )
        assert result.exit_code == 0
        fmt.print.assert_called_once_with(report)

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_find_blocks_error(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.find_capacity_blocks.side_effect = RuntimeError("boom")
        runner = CliRunner()
        result = runner.invoke(cli, ["capacity", "find-blocks", "-i", "p6-b200"])
        assert result.exit_code == 1

    @patch("cli.commands.capacity_cmd.get_capacity_checker")
    @patch("cli.commands.capacity_cmd.get_output_formatter")
    def test_reservation_check_multi_region_and_dates(self, mock_fmt_fn, mock_checker_fn):
        from cli.main import cli

        mock_fmt_fn.return_value = MagicMock()
        mock_checker_fn.return_value.check_reservation_availability.return_value = {
            "instance_type": "p5.48xlarge",
            "min_count_requested": 1,
            "regions_checked": ["us-east-1", "us-west-2"],
            "odcr": {
                "total_reserved_instances": 0,
                "total_available_instances": 0,
                "has_availability": False,
                "reservations": [],
            },
            "capacity_blocks": {
                "offerings_found": 0,
                "has_offerings": False,
                "duration_hours": 336,
                "date_window": {"earliest_start": None, "latest_start": None},
                "offerings": [],
            },
            "recommendation": "No reserved capacity",
        }
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "capacity",
                "reservation-check",
                "-i",
                "p5.48xlarge",
                "-r",
                "us-east-1",
                "-r",
                "us-west-2",
                "--block-duration-days",
                "14",
                "--earliest-start",
                "2026-07-01",
            ],
        )
        assert result.exit_code == 0
        call = mock_checker_fn.return_value.check_reservation_availability.call_args
        assert call.kwargs["regions"] == ["us-east-1", "us-west-2"]
        assert call.kwargs["block_duration_days"] == 14
        assert call.kwargs["earliest_start"] == "2026-07-01"


# =============================================================================
# MCP — find_capacity_blocks + reservation_check argv translation
# =============================================================================

sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))
import run_mcp  # noqa: E402


class TestMCPCapacitySweepTools:
    def test_find_capacity_blocks_argv(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.find_capacity_blocks(
                "p6-b200",
                regions=["us-east-1", "us-east-2", "us-west-2", "eu-west-1"],
                min_duration_days=1,
                max_duration_days=63,
                earliest_start="2026-07-01",
                latest_start="2026-07-10",
            )
            cmd = mock.call_args[0][0]
            assert "find-blocks" in cmd
            assert "p6-b200" in cmd
            assert cmd.count("-r") == 4
            assert "eu-west-1" in cmd
            assert "--min-duration-days" in cmd
            assert "63" in cmd
            assert "--earliest-start" in cmd
            assert "2026-07-01" in cmd

    def test_find_capacity_blocks_find_longest(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.find_capacity_blocks("p5.48xlarge", regions=["us-east-1"], find_longest=True)
            cmd = mock.call_args[0][0]
            assert "--find-longest" in cmd

    def test_find_capacity_blocks_count_and_hours(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.find_capacity_blocks(
                "p5.48xlarge",
                count=2,
                duration_hours=48,
                min_duration_hours=24,
                max_duration_hours=72,
            )
            cmd = mock.call_args[0][0]
            assert cmd[cmd.index("-c") : cmd.index("-c") + 2] == ["-c", "2"]
            assert "--duration-hours" in cmd
            assert "--min-duration-hours" in cmd
            assert "--max-duration-hours" in cmd

    def test_reservation_check_regions_list_and_dates(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.reservation_check(
                "p5.48xlarge",
                regions=["us-east-1", "us-west-2"],
                block_duration_days=14,
                earliest_start="2026-07-01",
                latest_start="2026-07-10",
            )
            cmd = mock.call_args[0][0]
            assert "reservation-check" in cmd
            assert cmd.count("-r") == 2
            assert "--block-duration-days" in cmd
            assert "--earliest-start" in cmd
            assert "--latest-start" in cmd

    def test_reservation_check_single_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.reservation_check("p4d.24xlarge", regions=["us-east-1"], block_duration=48)
            cmd = mock.call_args[0][0]
            assert cmd.count("-r") == 1
            assert "us-east-1" in cmd
            assert "--block-duration" in cmd
            assert "48" in cmd
