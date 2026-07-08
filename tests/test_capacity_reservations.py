"""
Tests for On-Demand Capacity Reservations and EC2 Capacity Blocks.

Covers CapacityChecker.list_capacity_reservations (active ODCRs with
utilization math and instance-type filter), list_capacity_block_offerings
(with graceful handling of unsupported instance types), list_all_reservations
aggregation across regions, and check_reservation_availability which
merges both signals into a single recommendation. Also exercises
purchase_capacity_block dry-run and real-purchase paths plus a couple
of cli.jobs helpers (_format_duration, wait_for_job progress callback
and timeout message) that share the same test file.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from cli.config import GCOConfig


class TestListCapacityReservations:
    """Tests for CapacityChecker.list_capacity_reservations."""

    def _make_checker(self):
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock(spec=GCOConfig)
            return CapacityChecker()

    def test_list_reservations_returns_active(self):
        checker = self._make_checker()
        mock_ec2 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "CapacityReservations": [
                    {
                        "CapacityReservationId": "cr-abc123",
                        "InstanceType": "p5.48xlarge",
                        "AvailabilityZone": "us-east-1a",
                        "State": "active",
                        "TotalInstanceCount": 4,
                        "AvailableInstanceCount": 2,
                        "InstancePlatform": "Linux/UNIX",
                        "Tenancy": "default",
                        "InstanceMatchCriteria": "open",
                        "EndDateType": "unlimited",
                        "Tags": [{"Key": "Project", "Value": "GCO"}],
                    }
                ]
            }
        ]
        mock_ec2.get_paginator.return_value = mock_paginator
        checker._session.client = MagicMock(return_value=mock_ec2)

        result = checker.list_capacity_reservations("us-east-1")

        assert len(result) == 1
        r = result[0]
        assert r["type"] == "odcr"
        assert r["reservation_id"] == "cr-abc123"
        assert r["instance_type"] == "p5.48xlarge"
        assert r["total_instances"] == 4
        assert r["available_instances"] == 2
        assert r["used_instances"] == 2
        assert r["utilization_pct"] == 50.0
        assert r["tags"] == {"Project": "GCO"}

    def test_list_reservations_with_instance_type_filter(self):
        checker = self._make_checker()
        mock_ec2 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"CapacityReservations": []}]
        mock_ec2.get_paginator.return_value = mock_paginator
        checker._session.client = MagicMock(return_value=mock_ec2)

        checker.list_capacity_reservations("us-east-1", instance_type="g5.xlarge")

        call_kwargs = mock_paginator.paginate.call_args[1]
        filters = call_kwargs.get("Filters", [])
        instance_filter = [f for f in filters if f["Name"] == "instance-type"]
        assert len(instance_filter) == 1
        assert instance_filter[0]["Values"] == ["g5.xlarge"]

    def test_list_reservations_empty(self):
        checker = self._make_checker()
        mock_ec2 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"CapacityReservations": []}]
        mock_ec2.get_paginator.return_value = mock_paginator
        checker._session.client = MagicMock(return_value=mock_ec2)

        result = checker.list_capacity_reservations("us-west-2")
        assert result == []

    def test_list_reservations_client_error(self):
        checker = self._make_checker()
        mock_ec2 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = ClientError(
            {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}},
            "DescribeCapacityReservations",
        )
        mock_ec2.get_paginator.return_value = mock_paginator
        checker._session.client = MagicMock(return_value=mock_ec2)

        result = checker.list_capacity_reservations("us-east-1")
        assert result == []

    def test_list_reservations_utilization_zero_total(self):
        """Edge case: total_instances is 0 should not divide by zero."""
        checker = self._make_checker()
        mock_ec2 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "CapacityReservations": [
                    {
                        "CapacityReservationId": "cr-zero",
                        "InstanceType": "g5.xlarge",
                        "AvailabilityZone": "us-east-1b",
                        "State": "active",
                        "TotalInstanceCount": 0,
                        "AvailableInstanceCount": 0,
                        "Tags": [],
                    }
                ]
            }
        ]
        mock_ec2.get_paginator.return_value = mock_paginator
        checker._session.client = MagicMock(return_value=mock_ec2)

        result = checker.list_capacity_reservations("us-east-1")
        assert result[0]["utilization_pct"] == 0


class TestListCapacityBlockOfferings:
    """Tests for CapacityChecker.list_capacity_block_offerings."""

    def _make_checker(self):
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock(spec=GCOConfig)
            return CapacityChecker()

    def test_list_block_offerings_success(self):
        checker = self._make_checker()
        mock_ec2 = MagicMock()
        start = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)
        mock_ec2.describe_capacity_block_offerings.return_value = {
            "CapacityBlockOfferings": [
                {
                    "CapacityBlockOfferingId": "cbo-abc123",
                    "InstanceType": "p5.48xlarge",
                    "AvailabilityZone": "us-east-1a",
                    "InstanceCount": 1,
                    "CapacityBlockDurationHours": 24,
                    "StartDate": start,
                    "EndDate": end,
                    "UpfrontFee": "4500.00",
                    "CurrencyCode": "USD",
                    "Tenancy": "default",
                }
            ]
        }
        checker._session.client = MagicMock(return_value=mock_ec2)

        result = checker.list_capacity_block_offerings(
            "us-east-1", "p5.48xlarge", instance_count=1, duration_hours=24
        )

        assert len(result) == 1
        b = result[0]
        assert b["type"] == "capacity_block"
        assert b["offering_id"] == "cbo-abc123"
        assert b["instance_type"] == "p5.48xlarge"
        assert b["duration_hours"] == 24
        assert b["upfront_fee"] == "4500.00"

    def test_list_block_offerings_empty(self):
        checker = self._make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.describe_capacity_block_offerings.return_value = {"CapacityBlockOfferings": []}
        checker._session.client = MagicMock(return_value=mock_ec2)

        result = checker.list_capacity_block_offerings("us-east-1", "g5.xlarge")
        assert result == []

    def test_list_block_offerings_unsupported(self):
        """Capacity Blocks not supported for this instance type."""
        checker = self._make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.describe_capacity_block_offerings.side_effect = ClientError(
            {"Error": {"Code": "Unsupported", "Message": "not supported"}},
            "DescribeCapacityBlockOfferings",
        )
        checker._session.client = MagicMock(return_value=mock_ec2)

        result = checker.list_capacity_block_offerings("us-east-1", "g5.xlarge")
        assert result == []

    def test_list_block_offerings_other_error(self):
        checker = self._make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.describe_capacity_block_offerings.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "oops"}},
            "DescribeCapacityBlockOfferings",
        )
        checker._session.client = MagicMock(return_value=mock_ec2)

        result = checker.list_capacity_block_offerings("us-east-1", "p5.48xlarge")
        assert result == []


class TestListAllReservations:
    """Tests for CapacityChecker.list_all_reservations."""

    def _make_checker(self):
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock(spec=GCOConfig)
            return CapacityChecker()

    def test_list_all_reservations_aggregates_regions(self):
        checker = self._make_checker()

        def mock_list(region, instance_type=None):
            if region == "us-east-1":
                return [{"total_instances": 4, "available_instances": 2, "region": "us-east-1"}]
            elif region == "us-west-2":
                return [{"total_instances": 2, "available_instances": 0, "region": "us-west-2"}]
            return []

        checker.list_capacity_reservations = mock_list

        result = checker.list_all_reservations(regions=["us-east-1", "us-west-2"])

        assert result["total_reservations"] == 2
        assert result["total_reserved_instances"] == 6
        assert result["total_available_instances"] == 2
        assert len(result["reservations"]) == 2

    def test_list_all_reservations_with_filter(self):
        checker = self._make_checker()
        checker.list_capacity_reservations = MagicMock(return_value=[])

        checker.list_all_reservations(instance_type="p5.48xlarge", regions=["us-east-1"])

        checker.list_capacity_reservations.assert_called_once_with(
            "us-east-1", instance_type="p5.48xlarge"
        )


class TestCheckReservationAvailability:
    """Tests for CapacityChecker.check_reservation_availability."""

    def _make_checker(self):
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock(spec=GCOConfig)
            return CapacityChecker()

    def test_has_odcr_availability(self):
        checker = self._make_checker()
        checker.list_capacity_reservations = MagicMock(
            return_value=[
                {
                    "total_instances": 4,
                    "available_instances": 3,
                    "reservation_id": "cr-123",
                    "availability_zone": "us-east-1a",
                }
            ]
        )
        checker.list_capacity_block_offerings = MagicMock(return_value=[])

        result = checker.check_reservation_availability(
            "p5.48xlarge", regions=["us-east-1"], min_count=2
        )

        assert result["odcr"]["has_availability"] is True
        assert result["odcr"]["total_available_instances"] == 3
        assert "ODCR capacity available" in result["recommendation"]

    def test_no_odcr_but_has_blocks(self):
        checker = self._make_checker()
        checker.list_capacity_reservations = MagicMock(return_value=[])
        checker.list_capacity_block_offerings = MagicMock(
            return_value=[
                {
                    "offering_id": "cbo-123",
                    "upfront_fee": "4500.00",
                    "availability_zone": "us-east-1a",
                }
            ]
        )

        result = checker.check_reservation_availability("p5.48xlarge", regions=["us-east-1"])

        assert result["odcr"]["has_availability"] is False
        assert result["capacity_blocks"]["has_offerings"] is True
        assert "Capacity Block" in result["recommendation"]

    def test_no_availability_at_all(self):
        checker = self._make_checker()
        checker.list_capacity_reservations = MagicMock(return_value=[])
        checker.list_capacity_block_offerings = MagicMock(return_value=[])

        result = checker.check_reservation_availability("p5.48xlarge", regions=["us-east-1"])

        assert result["odcr"]["has_availability"] is False
        assert result["capacity_blocks"]["has_offerings"] is False
        assert "No reserved capacity" in result["recommendation"]

    def test_skip_capacity_blocks(self):
        checker = self._make_checker()
        checker.list_capacity_reservations = MagicMock(return_value=[])
        checker.list_capacity_block_offerings = MagicMock(return_value=[])

        result = checker.check_reservation_availability(
            "g5.xlarge", regions=["us-east-1"], include_capacity_blocks=False
        )

        checker.list_capacity_block_offerings.assert_not_called()
        assert result["capacity_blocks"]["offerings"] == []

    def test_multi_region_check(self):
        checker = self._make_checker()

        def mock_list(region, instance_type=None):
            if region == "us-west-2":
                return [{"total_instances": 2, "available_instances": 1}]
            return []

        checker.list_capacity_reservations = mock_list
        checker.list_capacity_block_offerings = MagicMock(return_value=[])

        # Pass explicit regions to avoid discover_regional_stacks (needs AWS creds)
        result = checker.check_reservation_availability(
            "p5.48xlarge",
            regions=["us-west-2"],
            min_count=1,
            include_capacity_blocks=False,
        )
        assert result["odcr"]["has_availability"] is True
        assert result["odcr"]["total_available_instances"] == 1


class TestFormatDuration:
    """Tests for the _format_duration helper in jobs.py."""

    def test_seconds_only(self):
        from cli.jobs import _format_duration

        assert _format_duration(0) == "0s"
        assert _format_duration(45) == "45s"
        assert _format_duration(59) == "59s"

    def test_minutes_and_seconds(self):
        from cli.jobs import _format_duration

        assert _format_duration(60) == "1m00s"
        assert _format_duration(90) == "1m30s"
        assert _format_duration(3599) == "59m59s"

    def test_hours(self):
        from cli.jobs import _format_duration

        assert _format_duration(3600) == "1h00m00s"
        assert _format_duration(3661) == "1h01m01s"
        assert _format_duration(7200) == "2h00m00s"


class TestWaitForJobProgress:
    """Tests for wait_for_job progress reporting."""

    def test_wait_for_job_calls_progress_callback(self):
        from unittest.mock import MagicMock

        from cli.jobs import JobInfo, JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()
                manager = JobManager()

        # First call: running, second call: succeeded
        running_job = JobInfo(
            name="test",
            namespace="default",
            region="us-east-1",
            status="running",
            active_pods=1,
            succeeded_pods=0,
            completions=1,
        )
        done_job = JobInfo(
            name="test",
            namespace="default",
            region="us-east-1",
            status="succeeded",
            active_pods=0,
            succeeded_pods=1,
            completions=1,
        )
        manager.get_job = MagicMock(side_effect=[running_job, done_job])

        callback = MagicMock()
        with patch("time.sleep"):
            result = manager.wait_for_job(
                "test",
                "default",
                "us-east-1",
                poll_interval=1,
                progress_callback=callback,
            )

        assert result.status == "succeeded"
        callback.assert_called_once()
        # Callback receives (JobInfo, elapsed_seconds)
        call_args = callback.call_args[0]
        assert call_args[0].status == "running"

    def test_wait_for_job_timeout_includes_status(self):
        from unittest.mock import MagicMock

        from cli.jobs import JobInfo, JobManager

        with patch("cli.jobs.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            with patch("cli.jobs.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()
                manager = JobManager()

        running_job = JobInfo(
            name="test",
            namespace="default",
            region="us-east-1",
            status="running",
            active_pods=2,
            succeeded_pods=0,
            completions=3,
        )
        manager.get_job = MagicMock(return_value=running_job)

        # time.time() returns increasing values; second call exceeds timeout
        with (
            patch("time.sleep"),
            patch("time.time", side_effect=[0, 0, 100]),
            pytest.raises(TimeoutError, match="last status: running"),
        ):
            manager.wait_for_job(
                "test",
                "default",
                "us-east-1",
                timeout_seconds=5,
                poll_interval=1,
                progress_callback=lambda *a: None,
            )


class TestPurchaseCapacityBlock:
    """Tests for purchasing Capacity Block offerings."""

    @patch("cli.capacity.checker.get_config")
    def test_purchase_dry_run_success(self, mock_config):
        """Dry run should succeed when DryRunOperation is returned."""
        from cli.capacity import CapacityChecker

        mock_config.return_value = MagicMock(default_region="us-east-1")

        with patch("boto3.Session") as mock_session:
            mock_ec2 = MagicMock()
            mock_session.return_value.client.return_value = mock_ec2

            # DryRunOperation means the request would have succeeded
            mock_ec2.purchase_capacity_block.side_effect = ClientError(
                {"Error": {"Code": "DryRunOperation", "Message": "Request would have succeeded"}},
                "PurchaseCapacityBlock",
            )

            checker = CapacityChecker()
            result = checker.purchase_capacity_block(
                offering_id="cb-0123456789abcdef0",
                region="us-east-1",
                dry_run=True,
            )

            assert result["success"] is True
            assert result["dry_run"] is True
            assert result["offering_id"] == "cb-0123456789abcdef0"

    @patch("cli.capacity.checker.get_config")
    def test_purchase_dry_run_failure(self, mock_config):
        """Dry run should fail when offering is invalid."""
        from cli.capacity import CapacityChecker

        mock_config.return_value = MagicMock(default_region="us-east-1")

        with patch("boto3.Session") as mock_session:
            mock_ec2 = MagicMock()
            mock_session.return_value.client.return_value = mock_ec2

            mock_ec2.purchase_capacity_block.side_effect = ClientError(
                {"Error": {"Code": "InvalidParameterValue", "Message": "Invalid offering ID"}},
                "PurchaseCapacityBlock",
            )

            checker = CapacityChecker()
            result = checker.purchase_capacity_block(
                offering_id="cb-invalid",
                region="us-east-1",
                dry_run=True,
            )

            assert result["success"] is False
            assert result["dry_run"] is True
            assert "Invalid" in result["error"]

    @patch("cli.capacity.checker.get_config")
    def test_purchase_real_success(self, mock_config):
        """Real purchase should return reservation details."""
        from cli.capacity import CapacityChecker

        mock_config.return_value = MagicMock(default_region="us-east-1")

        with patch("boto3.Session") as mock_session:
            mock_ec2 = MagicMock()
            mock_session.return_value.client.return_value = mock_ec2

            mock_ec2.purchase_capacity_block.return_value = {
                "CapacityReservation": {
                    "CapacityReservationId": "cr-09876543210fedcba",
                    "InstanceType": "p4d.24xlarge",
                    "AvailabilityZone": "us-east-1a",
                    "TotalInstanceCount": 1,
                    "StartDate": datetime(2026, 3, 25, 11, 30, tzinfo=UTC),
                    "EndDate": datetime(2026, 3, 26, 11, 30, tzinfo=UTC),
                    "State": "payment-pending",
                }
            }

            checker = CapacityChecker()
            result = checker.purchase_capacity_block(
                offering_id="cb-0123456789abcdef0",
                region="us-east-1",
                dry_run=False,
            )

            assert result["success"] is True
            assert result["dry_run"] is False
            assert result["reservation_id"] == "cr-09876543210fedcba"
            assert result["instance_type"] == "p4d.24xlarge"
            assert result["total_instances"] == 1

    @patch("cli.capacity.checker.get_config")
    def test_purchase_real_failure(self, mock_config):
        """Real purchase should handle errors gracefully."""
        from cli.capacity import CapacityChecker

        mock_config.return_value = MagicMock(default_region="us-east-1")

        with patch("boto3.Session") as mock_session:
            mock_ec2 = MagicMock()
            mock_session.return_value.client.return_value = mock_ec2

            mock_ec2.purchase_capacity_block.side_effect = ClientError(
                {"Error": {"Code": "InsufficientCapacity", "Message": "No capacity available"}},
                "PurchaseCapacityBlock",
            )

            checker = CapacityChecker()
            result = checker.purchase_capacity_block(
                offering_id="cb-0123456789abcdef0",
                region="us-east-1",
                dry_run=False,
            )

            assert result["success"] is False
            assert result["dry_run"] is False
            assert result["error_code"] == "InsufficientCapacity"


class TestReservationPricingHelpers:
    """Pure ODCR pricing / ranking helpers in cli/capacity/blocks.py."""

    def test_compute_reservation_pricing_full(self):
        from cli.capacity import blocks

        pricing = blocks.compute_reservation_pricing(30.0, 4, 8)
        assert pricing["price_per_instance_hour"] == 30.0
        assert pricing["price_per_hour"] == 120.0  # 30 * 4 instances
        assert pricing["price_per_gpu_hour"] == 3.75  # 30 / 8 gpus

    def test_compute_reservation_pricing_missing_rate(self):
        from cli.capacity import blocks

        pricing = blocks.compute_reservation_pricing(None, 4, 8)
        assert pricing == {
            "price_per_instance_hour": None,
            "price_per_hour": None,
            "price_per_gpu_hour": None,
        }

    def test_compute_reservation_pricing_no_gpu_count(self):
        from cli.capacity import blocks

        pricing = blocks.compute_reservation_pricing(10.0, 2, None)
        assert pricing["price_per_instance_hour"] == 10.0
        assert pricing["price_per_hour"] == 20.0
        assert pricing["price_per_gpu_hour"] is None

    def test_rank_reservations_most_available_then_cheapest(self):
        from cli.capacity import blocks

        reservations = [
            {"region": "us-east-1", "available_instances": 1, "price_per_gpu_hour": 3.0},
            {"region": "us-west-2", "available_instances": 5, "price_per_gpu_hour": 4.0},
            {"region": "eu-west-1", "available_instances": 5, "price_per_gpu_hour": 2.0},
        ]
        ranked = blocks.rank_reservations(reservations)
        # Most available first; ties broken by cheaper per-GPU-hour.
        assert [r["region"] for r in ranked] == ["eu-west-1", "us-west-2", "us-east-1"]

    def test_sort_reservations_region_az_type(self):
        from cli.capacity import blocks

        reservations = [
            {
                "region": "us-west-2",
                "availability_zone": "us-west-2a",
                "instance_type": "p5.48xlarge",
            },
            {
                "region": "us-east-1",
                "availability_zone": "us-east-1b",
                "instance_type": "p4d.24xlarge",
            },
        ]
        ordered = blocks.sort_reservations(reservations)
        assert [r["region"] for r in ordered] == ["us-east-1", "us-west-2"]


class TestListReservationsPricing:
    """include_pricing enrichment on CapacityChecker.list_capacity_reservations."""

    def _make_checker(self):
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock(spec=GCOConfig)
            return CapacityChecker()

    def _paginator_with_one(self):
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "CapacityReservations": [
                    {
                        "CapacityReservationId": "cr-price",
                        "InstanceType": "p5.48xlarge",
                        "AvailabilityZone": "us-east-1a",
                        "State": "active",
                        "TotalInstanceCount": 4,
                        "AvailableInstanceCount": 2,
                        "Tags": [],
                    }
                ]
            }
        ]
        return mock_paginator

    def test_include_pricing_enriches_entry(self):
        checker = self._make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.get_paginator.return_value = self._paginator_with_one()
        checker._session.client = MagicMock(return_value=mock_ec2)
        checker.get_on_demand_price = MagicMock(return_value=30.0)

        result = checker.list_capacity_reservations("us-east-1", include_pricing=True)

        r = result[0]
        assert r["on_demand_price_per_hour"] == 30.0
        assert r["gpus_per_instance"] == 8
        assert r["price_per_instance_hour"] == 30.0
        assert r["price_per_hour"] == 120.0  # 30 * 4 total instances
        assert r["price_per_gpu_hour"] == 3.75  # 30 / 8 gpus

    def test_pricing_absent_by_default(self):
        checker = self._make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.get_paginator.return_value = self._paginator_with_one()
        checker._session.client = MagicMock(return_value=mock_ec2)
        checker.get_on_demand_price = MagicMock(return_value=30.0)

        result = checker.list_capacity_reservations("us-east-1")

        assert "price_per_hour" not in result[0]
        checker.get_on_demand_price.assert_not_called()

    def test_pricing_survives_lookup_failure(self):
        checker = self._make_checker()
        mock_ec2 = MagicMock()
        mock_ec2.get_paginator.return_value = self._paginator_with_one()
        checker._session.client = MagicMock(return_value=mock_ec2)
        checker.get_on_demand_price = MagicMock(side_effect=RuntimeError("pricing down"))

        result = checker.list_capacity_reservations("us-east-1", include_pricing=True)

        r = result[0]
        assert r["on_demand_price_per_hour"] is None
        assert r["price_per_hour"] is None


class TestCreateCapacityReservation:
    """Tests for CapacityChecker.create_capacity_reservation."""

    @patch("cli.capacity.checker.get_config")
    def test_create_dry_run_success(self, mock_config):
        from cli.capacity import CapacityChecker

        mock_config.return_value = MagicMock(default_region="us-east-1")
        with patch("boto3.Session") as mock_session:
            mock_ec2 = MagicMock()
            mock_session.return_value.client.return_value = mock_ec2
            mock_ec2.create_capacity_reservation.side_effect = ClientError(
                {"Error": {"Code": "DryRunOperation", "Message": "would have succeeded"}},
                "CreateCapacityReservation",
            )

            checker = CapacityChecker()
            result = checker.create_capacity_reservation(
                instance_type="p5.48xlarge",
                region="us-east-1",
                availability_zone="us-east-1a",
                instance_count=2,
                dry_run=True,
            )

            assert result["success"] is True
            assert result["dry_run"] is True
            assert result["instance_type"] == "p5.48xlarge"
            assert result["instance_count"] == 2

    @patch("cli.capacity.checker.get_config")
    def test_create_real_success(self, mock_config):
        from cli.capacity import CapacityChecker

        mock_config.return_value = MagicMock(default_region="us-east-1")
        with patch("boto3.Session") as mock_session:
            mock_ec2 = MagicMock()
            mock_session.return_value.client.return_value = mock_ec2
            mock_ec2.create_capacity_reservation.return_value = {
                "CapacityReservation": {
                    "CapacityReservationId": "cr-newodcr",
                    "InstanceType": "p5.48xlarge",
                    "AvailabilityZone": "us-east-1a",
                    "TotalInstanceCount": 2,
                    "AvailableInstanceCount": 2,
                    "State": "active",
                    "Tenancy": "default",
                    "InstanceMatchCriteria": "open",
                    "StartDate": datetime(2026, 7, 8, tzinfo=UTC),
                    "EndDateType": "unlimited",
                }
            }

            checker = CapacityChecker()
            result = checker.create_capacity_reservation(
                instance_type="p5.48xlarge",
                region="us-east-1",
                availability_zone="us-east-1a",
                instance_count=2,
            )

            assert result["success"] is True
            assert result["dry_run"] is False
            assert result["reservation_id"] == "cr-newodcr"
            assert result["total_instances"] == 2
            assert result["end_date_type"] == "unlimited"

    @patch("cli.capacity.checker.get_config")
    def test_create_alias_normalized(self, mock_config):
        from cli.capacity import CapacityChecker

        mock_config.return_value = MagicMock(default_region="us-east-1")
        with patch("boto3.Session") as mock_session:
            mock_ec2 = MagicMock()
            mock_session.return_value.client.return_value = mock_ec2
            mock_ec2.create_capacity_reservation.return_value = {
                "CapacityReservation": {"CapacityReservationId": "cr-x", "State": "active"}
            }

            checker = CapacityChecker()
            result = checker.create_capacity_reservation(
                instance_type="p6-b200",  # alias
                region="us-east-1",
                availability_zone="us-east-1a",
            )

            assert result["success"] is True
            kwargs = mock_ec2.create_capacity_reservation.call_args.kwargs
            assert kwargs["InstanceType"] == "p6-b200.48xlarge"

    @patch("cli.capacity.checker.get_config")
    def test_create_with_end_date_is_limited(self, mock_config):
        from cli.capacity import CapacityChecker

        mock_config.return_value = MagicMock(default_region="us-east-1")
        with patch("boto3.Session") as mock_session:
            mock_ec2 = MagicMock()
            mock_session.return_value.client.return_value = mock_ec2
            mock_ec2.create_capacity_reservation.return_value = {
                "CapacityReservation": {"CapacityReservationId": "cr-x", "State": "active"}
            }

            checker = CapacityChecker()
            checker.create_capacity_reservation(
                instance_type="p5.48xlarge",
                region="us-east-1",
                availability_zone="us-east-1a",
                end_date="2026-08-01",
            )

            kwargs = mock_ec2.create_capacity_reservation.call_args.kwargs
            assert kwargs["EndDateType"] == "limited"
            assert "EndDate" in kwargs

    @patch("cli.capacity.checker.get_config")
    def test_create_invalid_instance_type(self, mock_config):
        from cli.capacity import CapacityChecker

        mock_config.return_value = MagicMock(default_region="us-east-1")
        with patch("boto3.Session"):
            checker = CapacityChecker()
            checker.validate_instance_type = MagicMock(
                return_value={
                    "instance_type": "bogus.type",
                    "valid": False,
                    "known": False,
                    "note": "not a valid type",
                    "gpu_count": None,
                }
            )

            result = checker.create_capacity_reservation(
                instance_type="bogus.type",
                region="us-east-1",
                availability_zone="us-east-1a",
            )

            assert result["success"] is False
            assert result["error_code"] == "InvalidInstanceType"

    @patch("cli.capacity.checker.get_config")
    def test_create_real_failure(self, mock_config):
        from cli.capacity import CapacityChecker

        mock_config.return_value = MagicMock(default_region="us-east-1")
        with patch("boto3.Session") as mock_session:
            mock_ec2 = MagicMock()
            mock_session.return_value.client.return_value = mock_ec2
            mock_ec2.create_capacity_reservation.side_effect = ClientError(
                {"Error": {"Code": "InsufficientInstanceCapacity", "Message": "no capacity"}},
                "CreateCapacityReservation",
            )

            checker = CapacityChecker()
            result = checker.create_capacity_reservation(
                instance_type="p5.48xlarge",
                region="us-east-1",
                availability_zone="us-east-1a",
            )

            assert result["success"] is False
            assert result["error_code"] == "InsufficientInstanceCapacity"


class TestCancelCapacityReservation:
    """Tests for CapacityChecker.cancel_capacity_reservation."""

    @patch("cli.capacity.checker.get_config")
    def test_cancel_dry_run_success(self, mock_config):
        from cli.capacity import CapacityChecker

        mock_config.return_value = MagicMock(default_region="us-east-1")
        with patch("boto3.Session") as mock_session:
            mock_ec2 = MagicMock()
            mock_session.return_value.client.return_value = mock_ec2
            mock_ec2.cancel_capacity_reservation.side_effect = ClientError(
                {"Error": {"Code": "DryRunOperation", "Message": "would have succeeded"}},
                "CancelCapacityReservation",
            )

            checker = CapacityChecker()
            result = checker.cancel_capacity_reservation("cr-abc", "us-east-1", dry_run=True)

            assert result["success"] is True
            assert result["dry_run"] is True

    @patch("cli.capacity.checker.get_config")
    def test_cancel_real_success(self, mock_config):
        from cli.capacity import CapacityChecker

        mock_config.return_value = MagicMock(default_region="us-east-1")
        with patch("boto3.Session") as mock_session:
            mock_ec2 = MagicMock()
            mock_session.return_value.client.return_value = mock_ec2
            mock_ec2.cancel_capacity_reservation.return_value = {"Return": True}

            checker = CapacityChecker()
            result = checker.cancel_capacity_reservation("cr-abc", "us-east-1")

            assert result["success"] is True
            assert result["dry_run"] is False
            assert "cr-abc" in result["message"]

    @patch("cli.capacity.checker.get_config")
    def test_cancel_real_failure(self, mock_config):
        from cli.capacity import CapacityChecker

        mock_config.return_value = MagicMock(default_region="us-east-1")
        with patch("boto3.Session") as mock_session:
            mock_ec2 = MagicMock()
            mock_session.return_value.client.return_value = mock_ec2
            mock_ec2.cancel_capacity_reservation.side_effect = ClientError(
                {"Error": {"Code": "InvalidCapacityReservationId.NotFound", "Message": "nope"}},
                "CancelCapacityReservation",
            )

            checker = CapacityChecker()
            result = checker.cancel_capacity_reservation("cr-missing", "us-east-1")

            assert result["success"] is False
            assert result["error_code"] == "InvalidCapacityReservationId.NotFound"


class TestFindCapacityReservations:
    """Tests for CapacityChecker.find_capacity_reservations (parallel search)."""

    def _make_checker(self):
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock(spec=GCOConfig)
            return CapacityChecker()

    def test_aggregates_and_ranks_across_regions(self):
        checker = self._make_checker()

        def mock_list(region, instance_type=None, state="active", include_pricing=False):
            if region == "us-east-1":
                return [
                    {
                        "region": "us-east-1",
                        "availability_zone": "us-east-1a",
                        "instance_type": "p5.48xlarge",
                        "total_instances": 4,
                        "available_instances": 1,
                        "reservation_id": "cr-e1",
                    }
                ]
            if region == "us-west-2":
                return [
                    {
                        "region": "us-west-2",
                        "availability_zone": "us-west-2b",
                        "instance_type": "p5.48xlarge",
                        "total_instances": 8,
                        "available_instances": 6,
                        "reservation_id": "cr-w2",
                    }
                ]
            return []

        checker.list_capacity_reservations = mock_list

        result = checker.find_capacity_reservations(
            instance_type="p5.48xlarge",
            regions=["us-east-1", "us-west-2"],
            include_pricing=False,
        )

        assert result["reservations_found"] == 2
        assert result["total_reserved_instances"] == 12
        assert result["total_available_instances"] == 7
        # Most available first.
        assert result["best"]["reservation_id"] == "cr-w2"
        assert set(result["regions_with_reservations"]) == {"us-east-1", "us-west-2"}

    def test_normalizes_alias(self):
        checker = self._make_checker()
        captured = {}

        def mock_list(region, instance_type=None, state="active", include_pricing=False):
            captured["instance_type"] = instance_type
            return []

        checker.list_capacity_reservations = mock_list

        result = checker.find_capacity_reservations(
            instance_type="p6-b200", regions=["us-east-1"], include_pricing=False
        )

        assert captured["instance_type"] == "p6-b200.48xlarge"
        assert result["instance_type"] == "p6-b200.48xlarge"

    def test_invalid_instance_type_short_circuits(self):
        checker = self._make_checker()
        checker.validate_instance_type = MagicMock(
            return_value={
                "instance_type": "bogus.type",
                "valid": False,
                "known": False,
                "note": "not a valid type",
                "gpu_count": None,
            }
        )
        checker.list_capacity_reservations = MagicMock()

        result = checker.find_capacity_reservations(
            instance_type="bogus.type", regions=["us-east-1"]
        )

        assert result["valid_instance_type"] is False
        assert result["reservations_found"] == 0
        checker.list_capacity_reservations.assert_not_called()

    def test_empty_recommends_creation(self):
        checker = self._make_checker()
        checker.list_capacity_reservations = MagicMock(return_value=[])

        result = checker.find_capacity_reservations(
            instance_type="p5.48xlarge", regions=["us-east-1"], include_pricing=False
        )

        assert result["reservations_found"] == 0
        assert "create-reservation" in result["recommendation"]

    def test_no_type_filter_lists_all(self):
        checker = self._make_checker()
        checker.list_capacity_reservations = MagicMock(
            return_value=[
                {
                    "region": "us-east-1",
                    "availability_zone": "us-east-1a",
                    "instance_type": "g5.xlarge",
                    "total_instances": 2,
                    "available_instances": 2,
                    "reservation_id": "cr-any",
                }
            ]
        )

        result = checker.find_capacity_reservations(regions=["us-east-1"], include_pricing=False)

        assert result["instance_type"] is None
        assert result["reservations_found"] == 1
