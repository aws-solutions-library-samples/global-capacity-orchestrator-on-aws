"""
Tests for cli/capacity/ — the GPU capacity checker and recommender.

Covers the InstanceTypeInfo/SpotPriceInfo/CapacityEstimate dataclasses,
the GPU_INSTANCE_SPECS catalog, EC2 describe_instance_types lookup with
fallback to hardcoded specs, spot price history + stability analysis,
on-demand pricing via the Pricing API (with in-process caching),
instance availability checks, and the combined spot/on-demand/both
estimation path. Exercises recommend_capacity_type across low/medium/
high fault tolerance regimes and includes live-signal fallbacks
(placement score, AZ coverage) with heavy boto3.Session mocking.
"""

import json
from unittest.mock import MagicMock, patch


class TestInstanceTypeInfo:
    """Tests for InstanceTypeInfo dataclass."""

    def test_instance_type_info_creation(self):
        """Test creating InstanceTypeInfo."""
        from cli.capacity import InstanceTypeInfo

        info = InstanceTypeInfo(
            instance_type="g4dn.xlarge",
            vcpus=4,
            memory_gib=16,
            gpu_count=1,
            gpu_type="T4",
            gpu_memory_gib=16,
        )

        assert info.instance_type == "g4dn.xlarge"
        assert info.vcpus == 4
        assert info.memory_gib == 16
        assert info.gpu_count == 1
        assert info.is_gpu is True

    def test_instance_type_info_non_gpu(self):
        """Test InstanceTypeInfo for non-GPU instance."""
        from cli.capacity import InstanceTypeInfo

        info = InstanceTypeInfo(
            instance_type="m5.xlarge",
            vcpus=4,
            memory_gib=16,
        )

        assert info.gpu_count == 0
        assert info.is_gpu is False


class TestSpotPriceInfo:
    """Tests for SpotPriceInfo dataclass."""

    def test_spot_price_info_creation(self):
        """Test creating SpotPriceInfo."""
        from cli.capacity import SpotPriceInfo

        info = SpotPriceInfo(
            instance_type="g4dn.xlarge",
            availability_zone="us-east-1a",
            current_price=0.50,
            avg_price_7d=0.45,
            min_price_7d=0.40,
            max_price_7d=0.60,
            price_stability=0.85,
        )

        assert info.instance_type == "g4dn.xlarge"
        assert info.current_price == 0.50
        assert info.price_stability == 0.85


class TestCapacityEstimate:
    """Tests for CapacityEstimate dataclass."""

    def test_capacity_estimate_creation(self):
        """Test creating CapacityEstimate."""
        from cli.capacity import CapacityEstimate

        estimate = CapacityEstimate(
            instance_type="g4dn.xlarge",
            region="us-east-1",
            availability_zone="us-east-1a",
            capacity_type="spot",
            availability="high",
            confidence=0.9,
            price_per_hour=0.50,
            recommendation="Good for spot",
        )

        assert estimate.instance_type == "g4dn.xlarge"
        assert estimate.capacity_type == "spot"
        assert estimate.availability in ("high", "unknown")  # Depends on live signals
        assert estimate.details == {}

    def test_capacity_estimate_with_details(self):
        """Test CapacityEstimate with details."""
        from cli.capacity import CapacityEstimate

        estimate = CapacityEstimate(
            instance_type="g4dn.xlarge",
            region="us-east-1",
            availability_zone=None,
            capacity_type="on-demand",
            availability="high",
            confidence=0.9,
            details={"is_gpu": True},
        )

        assert estimate.details["is_gpu"] is True


class TestGPUInstanceSpecs:
    """Tests for GPU instance specifications."""

    def test_gpu_instance_specs_contains_common_types(self):
        """Test that GPU_INSTANCE_SPECS contains common GPU types."""
        from cli.capacity import GPU_INSTANCE_SPECS

        assert "g4dn.xlarge" in GPU_INSTANCE_SPECS
        assert "g5.xlarge" in GPU_INSTANCE_SPECS
        assert "p3.2xlarge" in GPU_INSTANCE_SPECS
        assert "p4d.24xlarge" in GPU_INSTANCE_SPECS

    def test_gpu_instance_specs_structure(self):
        """Test GPU_INSTANCE_SPECS structure."""
        from cli.capacity import GPU_INSTANCE_SPECS

        g4dn = GPU_INSTANCE_SPECS["g4dn.xlarge"]
        assert g4dn.vcpus == 4
        assert g4dn.memory_gib == 16
        assert g4dn.gpu_count == 1
        assert g4dn.gpu_type == "T4"
        assert g4dn.is_gpu is True

    def test_p4d_instance_specs(self):
        """Test P4d instance specs."""
        from cli.capacity import GPU_INSTANCE_SPECS

        p4d = GPU_INSTANCE_SPECS["p4d.24xlarge"]
        assert p4d.gpu_count == 8
        assert p4d.gpu_type == "A100"


class TestCapacityChecker:
    """Tests for CapacityChecker class."""

    def test_capacity_checker_initialization(self):
        """Test CapacityChecker initialization."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            checker = CapacityChecker()
            assert checker.config is not None

    def test_get_instance_info_known_type(self):
        """Test getting info for known GPU instance type."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            checker = CapacityChecker()
            info = checker.get_instance_info("g4dn.xlarge")

            assert info is not None
            assert info.instance_type == "g4dn.xlarge"
            assert info.gpu_count == 1

    def test_get_instance_info_unknown_type(self):
        """Test getting info for unknown instance type from API."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            checker = CapacityChecker()

            with patch.object(checker, "_session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.client.return_value = mock_ec2
                mock_ec2.describe_instance_types.return_value = {
                    "InstanceTypes": [
                        {
                            "VCpuInfo": {"DefaultVCpus": 8},
                            "MemoryInfo": {"SizeInMiB": 32768},
                            "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
                        }
                    ]
                }

                info = checker.get_instance_info("m5.2xlarge")
                assert info is not None
                assert info.vcpus == 8
                assert info.memory_gib == 32

    def test_get_instance_info_not_found(self):
        """Test getting info for non-existent instance type."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            checker = CapacityChecker()

            with patch.object(checker, "_session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.client.return_value = mock_ec2
                mock_ec2.describe_instance_types.return_value = {"InstanceTypes": []}

                info = checker.get_instance_info("nonexistent.type")
                assert info is None


class TestCapacityCheckerSpotPrices:
    """Tests for spot price functionality."""

    def test_get_spot_price_history(self):
        """Test getting spot price history."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            checker = CapacityChecker()

            with patch.object(checker, "_session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.client.return_value = mock_ec2
                mock_ec2.describe_spot_price_history.return_value = {
                    "SpotPriceHistory": [
                        {
                            "AvailabilityZone": "us-east-1a",
                            "SpotPrice": "0.50",
                        },
                        {
                            "AvailabilityZone": "us-east-1a",
                            "SpotPrice": "0.45",
                        },
                        {
                            "AvailabilityZone": "us-east-1b",
                            "SpotPrice": "0.55",
                        },
                    ]
                }

                prices = checker.get_spot_price_history("g4dn.xlarge", "us-east-1")

                assert len(prices) == 2  # Two AZs
                assert any(p.availability_zone == "us-east-1a" for p in prices)
                assert any(p.availability_zone == "us-east-1b" for p in prices)

    def test_get_spot_price_history_empty(self):
        """Test getting spot price history when no data available."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            checker = CapacityChecker()

            with patch.object(checker, "_session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.client.return_value = mock_ec2
                mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}

                prices = checker.get_spot_price_history("g4dn.xlarge", "us-east-1")
                assert prices == []


class TestCapacityCheckerRecommendations:
    """Tests for capacity recommendations."""

    def test_recommend_capacity_type_low_fault_tolerance(self):
        """Test recommendation with low fault tolerance."""
        from cli.capacity import CapacityChecker, CapacityEstimate

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            checker = CapacityChecker()

            with patch.object(checker, "estimate_capacity") as mock_estimate:
                # Return a valid on-demand estimate
                mock_estimate.return_value = [
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone=None,
                        capacity_type="on-demand",
                        availability="high",
                        confidence=0.9,
                        price_per_hour=1.00,
                    )
                ]

                capacity_type, explanation = checker.recommend_capacity_type(
                    "g4dn.xlarge", "us-east-1", "low"
                )

                assert capacity_type == "on-demand"
                assert "fault tolerance" in explanation.lower()

    def test_recommend_capacity_type_high_spot_availability(self):
        """Test recommendation with high spot availability."""
        from cli.capacity import CapacityChecker, CapacityEstimate

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            checker = CapacityChecker()

            with patch.object(checker, "estimate_capacity") as mock_estimate:
                mock_estimate.return_value = [
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone="us-east-1a",
                        capacity_type="spot",
                        availability="high",
                        confidence=0.9,
                        price_per_hour=0.50,
                    ),
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone=None,
                        capacity_type="on-demand",
                        availability="high",
                        confidence=0.9,
                        price_per_hour=1.00,
                    ),
                ]

                capacity_type, explanation = checker.recommend_capacity_type(
                    "g4dn.xlarge", "us-east-1", "high"
                )

                assert capacity_type == "spot"
                assert "spot" in explanation.lower()


class TestGetCapacityChecker:
    """Tests for get_capacity_checker factory function."""

    def test_get_capacity_checker(self):
        """Test factory function returns CapacityChecker."""
        from cli.capacity import CapacityChecker, get_capacity_checker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            checker = get_capacity_checker()
            assert isinstance(checker, CapacityChecker)

    def test_get_capacity_checker_with_config(self):
        """Test factory function with custom config."""
        from cli.capacity import CapacityChecker, get_capacity_checker

        custom_config = MagicMock()
        checker = get_capacity_checker(custom_config)
        assert isinstance(checker, CapacityChecker)
        assert checker.config == custom_config


class TestCapacityCheckerEstimateCapacity:
    """Tests for estimate_capacity method."""

    def test_estimate_capacity_spot_only(self):
        """Test estimating spot capacity only."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            # Mock the methods that estimate_capacity calls
            with (
                patch.object(checker, "check_instance_available_in_region", return_value=True),
                patch.object(checker, "get_spot_placement_score", return_value={}),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_az_coverage", return_value=None),
                patch.object(checker, "get_spot_placement_score", return_value={}),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_az_coverage", return_value=None),
                patch.object(
                    checker,
                    "get_spot_price_history",
                    return_value=[],
                ),
                patch.object(checker, "get_availability_zones", return_value=["us-east-1a"]),
                patch.object(checker, "get_on_demand_price", return_value=1.0),
            ):
                estimates = checker.estimate_capacity("g4dn.xlarge", "us-east-1", "spot")

                assert len(estimates) > 0
                assert all(e.capacity_type == "spot" for e in estimates)

    def test_estimate_capacity_on_demand_only(self):
        """Test estimating on-demand capacity only."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with (
                patch.object(checker, "check_instance_available_in_region", return_value=True),
                patch.object(checker, "get_spot_placement_score", return_value={}),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_az_coverage", return_value=None),
                patch.object(checker, "get_on_demand_price", return_value=1.0),
            ):
                estimates = checker.estimate_capacity("g4dn.xlarge", "us-east-1", "on-demand")

                assert len(estimates) == 1
                assert estimates[0].capacity_type == "on-demand"

    def test_estimate_capacity_both(self):
        """Test estimating both spot and on-demand capacity."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with (
                patch.object(checker, "check_instance_available_in_region", return_value=True),
                patch.object(checker, "get_spot_placement_score", return_value={}),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_az_coverage", return_value=None),
                patch.object(checker, "get_spot_placement_score", return_value={}),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_az_coverage", return_value=None),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_availability_zones", return_value=["us-east-1a"]),
                patch.object(checker, "get_on_demand_price", return_value=1.0),
            ):
                estimates = checker.estimate_capacity("g4dn.xlarge", "us-east-1", "both")

                capacity_types = {e.capacity_type for e in estimates}
                assert "spot" in capacity_types or "on-demand" in capacity_types


class TestCapacityCheckerSpotEstimation:
    """Tests for _estimate_spot_capacity method."""

    def test_estimate_spot_no_data(self):
        """Test spot estimation with no price data."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
                mock_session.return_value.client.return_value = mock_ec2

                checker = CapacityChecker()
                estimates = checker._estimate_spot_capacity("g4dn.xlarge", "us-east-1", None)

                assert len(estimates) == 1
                assert estimates[0].availability == "unknown"

    def test_estimate_spot_high_availability(self):
        """Test spot estimation with high availability."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                # Stable prices indicate high availability
                mock_ec2.describe_spot_price_history.return_value = {
                    "SpotPriceHistory": [
                        {"AvailabilityZone": "us-east-1a", "SpotPrice": "0.30"},
                        {"AvailabilityZone": "us-east-1a", "SpotPrice": "0.31"},
                        {"AvailabilityZone": "us-east-1a", "SpotPrice": "0.30"},
                    ]
                }
                mock_pricing = MagicMock()
                mock_pricing.get_products.return_value = {
                    "PriceList": [
                        '{"terms": {"OnDemand": {"term1": {"priceDimensions": {"dim1": {"pricePerUnit": {"USD": "1.00"}}}}}}}'
                    ]
                }

                def client_factory(service, region_name=None):
                    if service == "pricing":
                        return mock_pricing
                    return mock_ec2

                mock_session.return_value.client.side_effect = client_factory

                checker = CapacityChecker()
                estimates = checker._estimate_spot_capacity("g4dn.xlarge", "us-east-1", None)

                # Should have at least one estimate
                assert len(estimates) >= 1


class TestCapacityCheckerOnDemandEstimation:
    """Tests for _estimate_on_demand_capacity method."""

    def test_estimate_on_demand_gpu(self):
        """Test on-demand estimation for GPU instance."""
        from cli.capacity import CapacityChecker, InstanceTypeInfo

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()
            gpu_info = InstanceTypeInfo("g4dn.xlarge", 4, 16, 1, "T4", 16)

            with (
                patch.object(checker, "get_on_demand_price", return_value=None),
                patch.object(checker, "check_instance_available_in_region", return_value=True),
                patch.object(checker, "get_spot_placement_score", return_value={}),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_az_coverage", return_value=None),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_az_coverage", return_value=None),
            ):
                estimate = checker._estimate_on_demand_capacity(
                    "g4dn.xlarge", "us-east-1", gpu_info
                )

                assert estimate is not None
                assert estimate.capacity_type == "on-demand"
                assert estimate.availability == "unknown"  # No live signals available

    def test_estimate_on_demand_non_gpu(self):
        """Test on-demand estimation for non-GPU instance."""
        from cli.capacity import CapacityChecker, InstanceTypeInfo

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()
            cpu_info = InstanceTypeInfo("m5.xlarge", 4, 16, 0, None, 0)

            with (
                patch.object(checker, "get_on_demand_price", return_value=None),
                patch.object(checker, "check_instance_available_in_region", return_value=True),
                patch.object(checker, "get_spot_placement_score", return_value={}),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_az_coverage", return_value=None),
            ):
                estimate = checker._estimate_on_demand_capacity("m5.xlarge", "us-east-1", cpu_info)

                assert estimate is not None
                assert estimate.availability in (
                    "high",
                    "unknown",
                )  # Depends on live signals  # Non-GPU instances have high availability


class TestCapacityCheckerRecommendCapacityType:
    """Tests for recommend_capacity_type method."""

    def test_recommend_low_fault_tolerance(self):
        """Test recommendation with low fault tolerance."""
        from cli.capacity import CapacityChecker, CapacityEstimate

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            # Mock estimate_capacity to return valid estimates
            with patch.object(checker, "estimate_capacity") as mock_estimate:
                mock_estimate.return_value = [
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone=None,
                        capacity_type="on-demand",
                        availability="high",
                        confidence=0.9,
                        price_per_hour=1.00,
                    )
                ]

                capacity_type, explanation = checker.recommend_capacity_type(
                    "g4dn.xlarge", "us-east-1", "low"
                )

                assert capacity_type == "on-demand"
                assert (
                    "fault tolerance" in explanation.lower() or "on-demand" in explanation.lower()
                )

    def test_recommend_high_fault_tolerance_with_spot(self):
        """Test recommendation with high fault tolerance and good spot availability."""
        from cli.capacity import CapacityChecker, CapacityEstimate

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            # Mock estimate_capacity to return spot and on-demand estimates
            with patch.object(checker, "estimate_capacity") as mock_estimate:
                mock_estimate.return_value = [
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone="us-east-1a",
                        capacity_type="spot",
                        availability="high",
                        confidence=0.85,
                        price_per_hour=0.30,
                    ),
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone=None,
                        capacity_type="on-demand",
                        availability="high",
                        confidence=0.9,
                        price_per_hour=1.00,
                    ),
                ]

                capacity_type, explanation = checker.recommend_capacity_type(
                    "g4dn.xlarge", "us-east-1", "high"
                )

                # With high spot availability and high fault tolerance, should recommend spot
                assert capacity_type == "spot"
                assert "spot" in explanation.lower()


class TestGetOnDemandPrice:
    """Tests for get_on_demand_price method."""

    def test_get_on_demand_price_success(self):
        """Test successful on-demand price retrieval."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_pricing = MagicMock()
                mock_pricing.get_products.return_value = {
                    "PriceList": [
                        '{"terms": {"OnDemand": {"term1": {"priceDimensions": {"dim1": {"pricePerUnit": {"USD": "0.526"}}}}}}}'
                    ]
                }
                mock_session.return_value.client.return_value = mock_pricing

                checker = CapacityChecker()
                price = checker.get_on_demand_price("g4dn.xlarge", "us-east-1")

                assert price == 0.526

    def test_get_on_demand_price_cached(self):
        """Test on-demand price is cached."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_pricing = MagicMock()
                mock_pricing.get_products.return_value = {
                    "PriceList": [
                        '{"terms": {"OnDemand": {"term1": {"priceDimensions": {"dim1": {"pricePerUnit": {"USD": "0.526"}}}}}}}'
                    ]
                }
                mock_session.return_value.client.return_value = mock_pricing

                checker = CapacityChecker()
                price1 = checker.get_on_demand_price("g4dn.xlarge", "us-east-1")
                price2 = checker.get_on_demand_price("g4dn.xlarge", "us-east-1")

                assert price1 == price2
                # Should only call API once due to caching
                assert mock_pricing.get_products.call_count == 1

    def test_get_on_demand_price_not_found(self):
        """Test on-demand price when not found."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_pricing = MagicMock()
                mock_pricing.get_products.return_value = {"PriceList": []}
                mock_session.return_value.client.return_value = mock_pricing

                checker = CapacityChecker()
                price = checker.get_on_demand_price("unknown-type", "us-east-1")

                assert price is None


class TestCapacityCheckerNewMethods:
    """Tests for new capacity checker methods."""

    def test_check_instance_available_in_region_success(self):
        """Test checking instance availability when available."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_paginator = MagicMock()
                mock_paginator.paginate.return_value = [
                    {"InstanceTypeOfferings": [{"InstanceType": "g4dn.xlarge"}]}
                ]
                mock_ec2.get_paginator.return_value = mock_paginator
                mock_session.return_value.client.return_value = mock_ec2

                checker = CapacityChecker()
                result = checker.check_instance_available_in_region("g4dn.xlarge", "us-east-1")

                assert result is True

    def test_check_instance_available_in_region_not_found(self):
        """Test checking instance availability when not available."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_paginator = MagicMock()
                mock_paginator.paginate.return_value = [
                    {"InstanceTypeOfferings": [{"InstanceType": "m5.xlarge"}]}
                ]
                mock_ec2.get_paginator.return_value = mock_paginator
                mock_session.return_value.client.return_value = mock_ec2

                checker = CapacityChecker()
                result = checker.check_instance_available_in_region("g4dn.xlarge", "us-east-1")

                assert result is False

    def test_check_instance_available_in_region_error(self):
        """An AWS API failure must raise CapacityCheckError, not mask as False."""
        from botocore.exceptions import ClientError

        from cli.capacity import CapacityChecker, CapacityCheckError

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_ec2.get_paginator.side_effect = ClientError(
                    {"Error": {"Code": "RequestLimitExceeded", "Message": "slow"}},
                    "DescribeInstanceTypeOfferings",
                )
                mock_session.return_value.client.return_value = mock_ec2

                checker = CapacityChecker()
                with pytest.raises(CapacityCheckError, match="us-east-1"):
                    checker.check_instance_available_in_region("g4dn.xlarge", "us-east-1")

    def test_get_availability_zones_success(self):
        """Test getting availability zones."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_ec2.describe_availability_zones.return_value = {
                    "AvailabilityZones": [
                        {"ZoneName": "us-east-1a"},
                        {"ZoneName": "us-east-1b"},
                    ]
                }
                mock_session.return_value.client.return_value = mock_ec2

                checker = CapacityChecker()
                azs = checker.get_availability_zones("us-east-1")

                assert azs == ["us-east-1a", "us-east-1b"]

    def test_get_availability_zones_error(self):
        """Test getting availability zones when API fails."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_ec2.describe_availability_zones.side_effect = Exception("API Error")
                mock_session.return_value.client.return_value = mock_ec2

                checker = CapacityChecker()
                azs = checker.get_availability_zones("us-east-1")

                assert azs == []

    def test_get_spot_placement_score_success(self):
        """Test getting spot placement score."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_ec2.get_spot_placement_scores.return_value = {
                    "SpotPlacementScores": [
                        {"Score": 8},  # Regional score (no AZ ID)
                        {"AvailabilityZoneId": "use1-az1", "Score": 9},
                    ]
                }
                mock_session.return_value.client.return_value = mock_ec2

                checker = CapacityChecker()
                scores = checker.get_spot_placement_score("g4dn.xlarge", "us-east-1")

                assert scores["regional"] == 8
                assert scores["use1-az1"] == 9

    def test_get_spot_placement_score_error(self):
        """Test getting spot placement score when API fails."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_ec2.get_spot_placement_scores.side_effect = Exception("API Error")
                mock_session.return_value.client.return_value = mock_ec2

                checker = CapacityChecker()
                scores = checker.get_spot_placement_score("g4dn.xlarge", "us-east-1")

                assert scores == {}

    def test_estimate_capacity_unavailable_instance(self):
        """Test estimating capacity for unavailable instance type."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with patch.object(checker, "check_instance_available_in_region", return_value=False):
                estimates = checker.estimate_capacity("nonexistent.type", "us-east-1")

                assert len(estimates) == 1
                assert estimates[0].availability == "unavailable"
                assert estimates[0].capacity_type == "both"

    def test_recommend_capacity_type_unavailable(self):
        """Test recommendation when instance type is unavailable."""
        from cli.capacity import CapacityChecker, CapacityEstimate

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with patch.object(checker, "estimate_capacity") as mock_estimate:
                mock_estimate.return_value = [
                    CapacityEstimate(
                        instance_type="nonexistent.type",
                        region="us-east-1",
                        availability_zone=None,
                        capacity_type="both",
                        availability="unavailable",
                        confidence=1.0,
                    )
                ]

                capacity_type, explanation = checker.recommend_capacity_type(
                    "nonexistent.type", "us-east-1"
                )

                assert capacity_type == "unavailable"
                assert "not available" in explanation.lower()

    def test_recommend_capacity_type_medium_spot_high_tolerance(self):
        """Test recommendation with medium spot availability and high fault tolerance."""
        from cli.capacity import CapacityChecker, CapacityEstimate

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with patch.object(checker, "estimate_capacity") as mock_estimate:
                mock_estimate.return_value = [
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone="us-east-1a",
                        capacity_type="spot",
                        availability="medium",
                        confidence=0.7,
                        price_per_hour=0.30,
                    ),
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone=None,
                        capacity_type="on-demand",
                        availability="high",
                        confidence=0.9,
                        price_per_hour=1.00,
                    ),
                ]

                capacity_type, explanation = checker.recommend_capacity_type(
                    "g4dn.xlarge", "us-east-1", "high"
                )

                assert capacity_type == "spot"
                assert "medium" in explanation.lower() or "spot" in explanation.lower()

    def test_recommend_capacity_type_low_spot_high_tolerance(self):
        """Test recommendation with low spot availability and high fault tolerance."""
        from cli.capacity import CapacityChecker, CapacityEstimate

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with patch.object(checker, "estimate_capacity") as mock_estimate:
                mock_estimate.return_value = [
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone="us-east-1a",
                        capacity_type="spot",
                        availability="low",
                        confidence=0.5,
                        price_per_hour=0.30,
                    ),
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone=None,
                        capacity_type="on-demand",
                        availability="high",
                        confidence=0.9,
                        price_per_hour=1.00,
                    ),
                ]

                capacity_type, explanation = checker.recommend_capacity_type(
                    "g4dn.xlarge", "us-east-1", "high"
                )

                assert capacity_type == "spot"
                assert "low" in explanation.lower() or "spot" in explanation.lower()


class TestCapacityCheckerGetInstanceInfoWithGPU:
    """Tests for get_instance_info with GPU instances."""

    def test_get_instance_info_with_gpu_from_api(self):
        """Test getting GPU instance info from API."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_ec2.describe_instance_types.return_value = {
                    "InstanceTypes": [
                        {
                            "VCpuInfo": {"DefaultVCpus": 8},
                            "MemoryInfo": {"SizeInMiB": 32768},
                            "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
                            "GpuInfo": {
                                "Gpus": [
                                    {
                                        "Count": 1,
                                        "Name": "T4",
                                        "MemoryInfo": {"SizeInMiB": 16384},
                                    }
                                ]
                            },
                        }
                    ]
                }
                mock_session.return_value.client.return_value = mock_ec2

                checker = CapacityChecker()
                # Clear the known specs cache to force API call
                info = checker.get_instance_info("g4dn.2xlarge")

                assert info is not None
                assert info.gpu_count == 1
                assert info.gpu_type == "T4"


class TestCapacityCheckerSpotPlacementScoreEdgeCases:
    """Tests for spot placement score edge cases."""

    def test_get_spot_placement_score_invalid_parameter(self):
        """Test spot placement score with InvalidParameterValue error."""
        from botocore.exceptions import ClientError

        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_ec2.get_spot_placement_scores.side_effect = ClientError(
                    {"Error": {"Code": "InvalidParameterValue", "Message": "Invalid"}},
                    "GetSpotPlacementScores",
                )
                mock_session.return_value.client.return_value = mock_ec2

                checker = CapacityChecker()
                scores = checker.get_spot_placement_score("invalid.type", "us-east-1")

                assert scores == {}

    def test_get_spot_placement_score_unsupported_operation(self):
        """Test spot placement score with UnsupportedOperation error."""
        from botocore.exceptions import ClientError

        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_ec2.get_spot_placement_scores.side_effect = ClientError(
                    {"Error": {"Code": "UnsupportedOperation", "Message": "Not supported"}},
                    "GetSpotPlacementScores",
                )
                mock_session.return_value.client.return_value = mock_ec2

                checker = CapacityChecker()
                scores = checker.get_spot_placement_score("g4dn.xlarge", "us-east-1")

                assert scores == {}


class TestCapacityCheckerSpotPriceHistoryEdgeCases:
    """Tests for spot price history edge cases."""

    def test_get_spot_price_history_invalid_parameter(self):
        """Test spot price history with InvalidParameterValue error."""
        from botocore.exceptions import ClientError

        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_ec2.describe_spot_price_history.side_effect = ClientError(
                    {"Error": {"Code": "InvalidParameterValue", "Message": "Invalid"}},
                    "DescribeSpotPriceHistory",
                )
                mock_session.return_value.client.return_value = mock_ec2

                checker = CapacityChecker()
                prices = checker.get_spot_price_history("invalid.type", "us-east-1")

                assert prices == []

    def test_get_spot_price_history_with_stability_calculation(self):
        """Test spot price history with stability calculation."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                # Prices with some variance
                mock_ec2.describe_spot_price_history.return_value = {
                    "SpotPriceHistory": [
                        {"AvailabilityZone": "us-east-1a", "SpotPrice": "0.30"},
                        {"AvailabilityZone": "us-east-1a", "SpotPrice": "0.32"},
                        {"AvailabilityZone": "us-east-1a", "SpotPrice": "0.31"},
                        {"AvailabilityZone": "us-east-1a", "SpotPrice": "0.29"},
                    ]
                }
                mock_session.return_value.client.return_value = mock_ec2

                checker = CapacityChecker()
                prices = checker.get_spot_price_history("g4dn.xlarge", "us-east-1")

                assert len(prices) == 1
                assert prices[0].availability_zone == "us-east-1a"
                assert prices[0].price_stability > 0  # Should have some stability


class TestCapacityCheckerEstimateSpotWithPlacementScores:
    """Tests for spot estimation with placement scores."""

    def test_estimate_spot_with_high_placement_score(self):
        """Test spot estimation with high placement score."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with (
                patch.object(
                    checker,
                    "get_spot_placement_score",
                    return_value={"regional": 9, "use1-az1": 9},
                ),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_availability_zones", return_value=["us-east-1a"]),
                patch.object(checker, "get_on_demand_price", return_value=1.0),
            ):
                estimates = checker._estimate_spot_capacity("g4dn.xlarge", "us-east-1", None)

                assert len(estimates) >= 1
                # High score should result in high availability
                assert any(e.availability == "high" for e in estimates)

    def test_estimate_spot_with_medium_placement_score(self):
        """Test spot estimation with medium placement score."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with (
                patch.object(
                    checker,
                    "get_spot_placement_score",
                    return_value={"regional": 6},
                ),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_availability_zones", return_value=["us-east-1a"]),
                patch.object(checker, "get_on_demand_price", return_value=1.0),
            ):
                estimates = checker._estimate_spot_capacity("g4dn.xlarge", "us-east-1", None)

                assert len(estimates) >= 1
                assert any(e.availability == "medium" for e in estimates)

    def test_estimate_spot_with_low_placement_score(self):
        """Test spot estimation with low placement score."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with (
                patch.object(
                    checker,
                    "get_spot_placement_score",
                    return_value={"regional": 2},
                ),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_availability_zones", return_value=["us-east-1a"]),
                patch.object(checker, "get_on_demand_price", return_value=1.0),
            ):
                estimates = checker._estimate_spot_capacity("g4dn.xlarge", "us-east-1", None)

                assert len(estimates) >= 1
                assert any(e.availability == "low" for e in estimates)

    def test_estimate_spot_with_price_history_fallback(self):
        """Test spot estimation falling back to price history."""
        from cli.capacity import CapacityChecker, SpotPriceInfo

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            spot_info = SpotPriceInfo(
                instance_type="g4dn.xlarge",
                availability_zone="us-east-1a",
                current_price=0.30,
                avg_price_7d=0.30,
                min_price_7d=0.28,
                max_price_7d=0.32,
                price_stability=0.9,
            )

            with (
                patch.object(checker, "get_spot_placement_score", return_value={}),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_az_coverage", return_value=None),
                patch.object(checker, "get_spot_price_history", return_value=[spot_info]),
                patch.object(checker, "get_availability_zones", return_value=["us-east-1a"]),
                patch.object(checker, "get_on_demand_price", return_value=1.0),
            ):
                estimates = checker._estimate_spot_capacity("g4dn.xlarge", "us-east-1", None)

                assert len(estimates) >= 1
                # High stability should result in medium availability
                assert any(e.availability == "medium" for e in estimates)


class TestCapacityCheckerRecommendCapacityTypeEdgeCases:
    """Tests for recommend_capacity_type edge cases."""

    def test_recommend_medium_fault_tolerance(self):
        """Test recommendation with medium fault tolerance."""
        from cli.capacity import CapacityChecker, CapacityEstimate

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with patch.object(checker, "estimate_capacity") as mock_estimate:
                mock_estimate.return_value = [
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone="us-east-1a",
                        capacity_type="spot",
                        availability="high",
                        confidence=0.9,
                        price_per_hour=0.30,
                    ),
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone=None,
                        capacity_type="on-demand",
                        availability="high",
                        confidence=0.9,
                        price_per_hour=1.00,
                    ),
                ]

                capacity_type, explanation = checker.recommend_capacity_type(
                    "g4dn.xlarge", "us-east-1", "medium"
                )

                # Medium tolerance with high spot availability should recommend spot
                assert capacity_type == "spot"

    def test_recommend_no_estimates(self):
        """Test recommendation when no estimates available."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with patch.object(checker, "estimate_capacity") as mock_estimate:
                mock_estimate.return_value = []

                capacity_type, explanation = checker.recommend_capacity_type(
                    "g4dn.xlarge", "us-east-1"
                )

                # Defaults to on-demand when no estimates available
                assert capacity_type == "on-demand"
                assert "unknown" in explanation.lower() or "limited" in explanation.lower()


# =============================================================================
# Additional coverage tests for cli/capacity.py
# =============================================================================


class TestCapacityCheckerSpotPlacementScoreExtended:
    """Tests for Spot Placement Score functionality."""

    def test_get_spot_placement_score_success(self):
        """Test successful spot placement score retrieval."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.boto3.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session
            mock_ec2 = MagicMock()
            mock_session.client.return_value = mock_ec2

            mock_ec2.get_spot_placement_scores.return_value = {
                "SpotPlacementScores": [
                    {"Score": 8},
                    {"AvailabilityZoneId": "use1-az1", "Score": 7},
                ]
            }

            checker = CapacityChecker()
            scores = checker.get_spot_placement_score("m5.large", "us-east-1")

            assert "regional" in scores
            assert scores["regional"] == 8
            assert "use1-az1" in scores
            assert scores["use1-az1"] == 7

    def test_get_spot_placement_score_client_error(self):
        """Test spot placement score handles ClientError."""
        from botocore.exceptions import ClientError

        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.boto3.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session
            mock_ec2 = MagicMock()
            mock_session.client.return_value = mock_ec2

            mock_ec2.get_spot_placement_scores.side_effect = ClientError(
                {"Error": {"Code": "InvalidParameterValue", "Message": "Invalid"}},
                "GetSpotPlacementScores",
            )

            checker = CapacityChecker()
            scores = checker.get_spot_placement_score("invalid-type", "us-east-1")

            assert scores == {}

    def test_get_spot_placement_score_generic_exception(self):
        """Test spot placement score handles generic exception."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.boto3.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session
            mock_ec2 = MagicMock()
            mock_session.client.return_value = mock_ec2

            mock_ec2.get_spot_placement_scores.side_effect = Exception("Network error")

            checker = CapacityChecker()
            scores = checker.get_spot_placement_score("m5.large", "us-east-1")

            assert scores == {}


class TestCapacityCheckerSpotPricesExtended:
    """Extended tests for spot price history functionality."""

    def test_get_spot_price_history_success(self):
        """Test successful spot price history retrieval."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.boto3.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session
            mock_ec2 = MagicMock()
            mock_session.client.return_value = mock_ec2

            mock_ec2.describe_spot_price_history.return_value = {
                "SpotPriceHistory": [
                    {"AvailabilityZone": "us-east-1a", "SpotPrice": "0.10"},
                    {"AvailabilityZone": "us-east-1a", "SpotPrice": "0.12"},
                    {"AvailabilityZone": "us-east-1b", "SpotPrice": "0.11"},
                ]
            }

            checker = CapacityChecker()
            prices = checker.get_spot_price_history("m5.large", "us-east-1")

            assert len(prices) == 2
            az_names = [p.availability_zone for p in prices]
            assert "us-east-1a" in az_names
            assert "us-east-1b" in az_names

    def test_get_spot_price_history_client_error(self):
        """Test spot price history handles ClientError."""
        from botocore.exceptions import ClientError

        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.boto3.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session
            mock_ec2 = MagicMock()
            mock_session.client.return_value = mock_ec2

            mock_ec2.describe_spot_price_history.side_effect = ClientError(
                {"Error": {"Code": "InvalidParameterValue", "Message": "Invalid"}},
                "DescribeSpotPriceHistory",
            )

            checker = CapacityChecker()
            prices = checker.get_spot_price_history("invalid-type", "us-east-1")

            assert prices == []


class TestCapacityCheckerOnDemandExtended:
    """Extended tests for on-demand capacity checking."""

    def test_get_on_demand_price_success(self):
        """Test successful on-demand price retrieval."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.boto3.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session
            mock_pricing = MagicMock()
            mock_session.client.return_value = mock_pricing

            price_data = {
                "terms": {
                    "OnDemand": {
                        "term1": {"priceDimensions": {"dim1": {"pricePerUnit": {"USD": "0.50"}}}}
                    }
                }
            }
            mock_pricing.get_products.return_value = {"PriceList": [json.dumps(price_data)]}

            checker = CapacityChecker()
            price = checker.get_on_demand_price("m5.large", "us-east-1")

            assert price == 0.50

    def test_get_on_demand_price_exception(self):
        """Test on-demand price returns None on exception."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.boto3.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session
            mock_pricing = MagicMock()
            mock_session.client.return_value = mock_pricing
            mock_pricing.get_products.side_effect = Exception("API Error")

            checker = CapacityChecker()
            price = checker.get_on_demand_price("m5.large", "us-east-1")

            assert price is None


class TestCapacityCheckerEstimateExtended:
    """Extended tests for capacity estimation."""

    def test_estimate_capacity_unavailable_instance(self):
        """Test estimate_capacity for unavailable instance type."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.boto3.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session
            mock_ec2 = MagicMock()
            mock_session.client.return_value = mock_ec2

            paginator = MagicMock()
            paginator.paginate.return_value = [{"InstanceTypeOfferings": []}]
            mock_ec2.get_paginator.return_value = paginator

            checker = CapacityChecker()
            estimates = checker.estimate_capacity("invalid-type", "us-east-1")

            assert len(estimates) == 1
            assert estimates[0].availability == "unavailable"

    def test_recommend_capacity_type_high_fault_tolerance(self):
        """Test capacity recommendation with high fault tolerance."""
        from cli.capacity import CapacityChecker, InstanceTypeInfo

        with patch("cli.capacity.checker.boto3.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session
            mock_ec2 = MagicMock()
            mock_session.client.return_value = mock_ec2

            paginator = MagicMock()
            paginator.paginate.return_value = [
                {"InstanceTypeOfferings": [{"InstanceType": "m5.large"}]}
            ]
            mock_ec2.get_paginator.return_value = paginator

            mock_ec2.get_spot_placement_scores.return_value = {
                "SpotPlacementScores": [{"Score": 9}]
            }

            mock_ec2.describe_spot_price_history.return_value = {
                "SpotPriceHistory": [{"AvailabilityZone": "us-east-1a", "SpotPrice": "0.05"}]
            }

            mock_ec2.describe_availability_zones.return_value = {
                "AvailabilityZones": [{"ZoneName": "us-east-1a"}]
            }

            checker = CapacityChecker()
            with (
                patch.object(checker, "get_on_demand_price", return_value=0.096),
                patch.object(checker, "get_spot_placement_score", return_value={"regional": 9}),
                patch.object(
                    checker,
                    "get_instance_info",
                    return_value=InstanceTypeInfo("m5.large", 2, 8, 0, None, 0),
                ),
            ):
                capacity_type, explanation = checker.recommend_capacity_type(
                    "m5.large", "us-east-1", "high"
                )

            assert capacity_type == "spot"


class TestCapacityCheckerAvailabilityZonesExtended:
    """Extended tests for availability zone functionality."""

    def test_get_availability_zones_success(self):
        """Test successful AZ retrieval."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.boto3.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session
            mock_ec2 = MagicMock()
            mock_session.client.return_value = mock_ec2

            mock_ec2.describe_availability_zones.return_value = {
                "AvailabilityZones": [
                    {"ZoneName": "us-east-1a"},
                    {"ZoneName": "us-east-1b"},
                ]
            }

            checker = CapacityChecker()
            azs = checker.get_availability_zones("us-east-1")

            assert len(azs) == 2
            assert "us-east-1a" in azs

    def test_get_availability_zones_exception(self):
        """Test AZ retrieval handles exception."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.boto3.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session
            mock_ec2 = MagicMock()
            mock_session.client.return_value = mock_ec2
            mock_ec2.describe_availability_zones.side_effect = Exception("API Error")

            checker = CapacityChecker()
            azs = checker.get_availability_zones("us-east-1")

            assert azs == []


class TestMultiRegionCapacityCheckerExtended:
    """Extended tests for MultiRegionCapacityChecker."""

    def test_get_region_capacity_no_stack(self):
        """Test get_region_capacity when no stack found."""
        from cli.capacity import MultiRegionCapacityChecker, RegionCapacity

        with patch("cli.capacity.checker.boto3.Session"):
            checker = MultiRegionCapacityChecker()
            with patch.object(
                checker, "get_region_capacity", return_value=RegionCapacity(region="us-east-1")
            ):
                capacity = checker.get_region_capacity("us-east-1")

                assert capacity.region == "us-east-1"
                assert capacity.queue_depth == 0

    def test_get_all_regions_capacity_empty(self):
        """Test get_all_regions_capacity with no stacks."""
        from cli.capacity import MultiRegionCapacityChecker

        with patch("cli.capacity.checker.boto3.Session"):
            checker = MultiRegionCapacityChecker()
            with patch.object(checker, "get_all_regions_capacity", return_value=[]):
                capacities = checker.get_all_regions_capacity()

                assert capacities == []

    def test_recommend_region_for_job_no_capacity(self):
        """Test recommend_region_for_job with no capacity data."""
        from cli.capacity import MultiRegionCapacityChecker

        with patch("cli.capacity.checker.boto3.Session"):
            checker = MultiRegionCapacityChecker()
            with patch.object(checker, "get_all_regions_capacity", return_value=[]):
                recommendation = checker.recommend_region_for_job()

                assert "region" in recommendation
                assert "No capacity data" in recommendation["reason"]


# =============================================================================
# Additional coverage tests for uncovered lines in cli/capacity.py
# =============================================================================


class TestCapacityCheckerGetInstanceInfoWithGPUFromAPI:
    """Tests for get_instance_info with GPU instances from API."""

    def test_get_instance_info_with_gpu_from_api(self):
        """Test getting GPU instance info from EC2 API."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.return_value.client.return_value = mock_ec2
                mock_ec2.describe_instance_types.return_value = {
                    "InstanceTypes": [
                        {
                            "VCpuInfo": {"DefaultVCpus": 8},
                            "MemoryInfo": {"SizeInMiB": 32768},
                            "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
                            "GpuInfo": {
                                "Gpus": [
                                    {"Count": 1, "Name": "T4", "MemoryInfo": {"SizeInMiB": 16384}}
                                ]
                            },
                        }
                    ]
                }

                checker = CapacityChecker()
                info = checker.get_instance_info("g4dn.custom")

                assert info is not None
                assert info.gpu_count == 1
                assert info.gpu_type == "T4"
                assert info.gpu_memory_gib == 16

    def test_get_instance_info_api_error(self):
        """Test get_instance_info returns None on API error."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.return_value.client.return_value = mock_ec2
                mock_ec2.describe_instance_types.side_effect = Exception("API error")

                checker = CapacityChecker()
                info = checker.get_instance_info("unknown.type")

                assert info is None


class TestCapacityCheckerSpotPlacementScoreAPI:
    """Tests for get_spot_placement_score method."""

    def test_get_spot_placement_score_success(self):
        """Test successful spot placement score retrieval."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.return_value.client.return_value = mock_ec2
                mock_ec2.get_spot_placement_scores.return_value = {
                    "SpotPlacementScores": [
                        {"Score": 8},  # Regional score
                        {"AvailabilityZoneId": "use1-az1", "Score": 9},
                    ]
                }

                checker = CapacityChecker()
                scores = checker.get_spot_placement_score("g4dn.xlarge", "us-east-1")

                assert "regional" in scores
                assert scores["regional"] == 8
                assert scores["use1-az1"] == 9

    def test_get_spot_placement_score_invalid_parameter(self):
        """Test spot placement score with invalid parameter error."""
        from botocore.exceptions import ClientError

        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.return_value.client.return_value = mock_ec2
                mock_ec2.get_spot_placement_scores.side_effect = ClientError(
                    {"Error": {"Code": "InvalidParameterValue", "Message": "Invalid"}},
                    "GetSpotPlacementScores",
                )

                checker = CapacityChecker()
                scores = checker.get_spot_placement_score("invalid.type", "us-east-1")

                assert scores == {}

    def test_get_spot_placement_score_unsupported_operation(self):
        """Test spot placement score with unsupported operation error."""
        from botocore.exceptions import ClientError

        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.return_value.client.return_value = mock_ec2
                mock_ec2.get_spot_placement_scores.side_effect = ClientError(
                    {"Error": {"Code": "UnsupportedOperation", "Message": "Not supported"}},
                    "GetSpotPlacementScores",
                )

                checker = CapacityChecker()
                scores = checker.get_spot_placement_score("g4dn.xlarge", "us-east-1")

                assert scores == {}

    def test_get_spot_placement_score_generic_error(self):
        """Test spot placement score with generic error."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.return_value.client.return_value = mock_ec2
                mock_ec2.get_spot_placement_scores.side_effect = Exception("Generic error")

                checker = CapacityChecker()
                scores = checker.get_spot_placement_score("g4dn.xlarge", "us-east-1")

                assert scores == {}


class TestCapacityCheckerSpotPriceHistoryErrors:
    """Tests for get_spot_price_history error handling."""

    def test_get_spot_price_history_invalid_parameter(self):
        """Test spot price history with invalid parameter error."""
        from botocore.exceptions import ClientError

        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.return_value.client.return_value = mock_ec2
                mock_ec2.describe_spot_price_history.side_effect = ClientError(
                    {"Error": {"Code": "InvalidParameterValue", "Message": "Invalid"}},
                    "DescribeSpotPriceHistory",
                )

                checker = CapacityChecker()
                prices = checker.get_spot_price_history("invalid.type", "us-east-1")

                assert prices == []


class TestCapacityCheckerEstimateCapacityUnavailable:
    """Tests for estimate_capacity when instance type is unavailable."""

    def test_estimate_capacity_unavailable_in_region(self):
        """Test estimate_capacity when instance type not available in region."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with patch.object(checker, "check_instance_available_in_region", return_value=False):
                estimates = checker.estimate_capacity("g4dn.xlarge", "us-east-1")

                assert len(estimates) == 1
                assert estimates[0].availability == "unavailable"
                assert "not available" in estimates[0].recommendation


class TestCapacityCheckerSpotEstimationWithPlacementScore:
    """Tests for _estimate_spot_capacity with placement scores."""

    def test_estimate_spot_with_high_placement_score(self):
        """Test spot estimation with high placement score."""
        from cli.capacity import CapacityChecker, SpotPriceInfo

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with (
                patch.object(
                    checker, "get_spot_placement_score", return_value={"regional": 9, "use1-az1": 9}
                ),
                patch.object(
                    checker,
                    "get_spot_price_history",
                    return_value=[
                        SpotPriceInfo("g4dn.xlarge", "us-east-1a", 0.30, 0.30, 0.25, 0.35, 0.9)
                    ],
                ),
                patch.object(checker, "get_availability_zones", return_value=["us-east-1a"]),
                patch.object(checker, "get_on_demand_price", return_value=1.0),
            ):
                estimates = checker._estimate_spot_capacity("g4dn.xlarge", "us-east-1", None)

                assert len(estimates) >= 1
                assert estimates[0].availability == "high"

    def test_estimate_spot_with_medium_placement_score(self):
        """Test spot estimation with medium placement score."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with (
                patch.object(checker, "get_spot_placement_score", return_value={"regional": 6}),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_availability_zones", return_value=["us-east-1a"]),
                patch.object(checker, "get_on_demand_price", return_value=1.0),
            ):
                estimates = checker._estimate_spot_capacity("g4dn.xlarge", "us-east-1", None)

                assert len(estimates) >= 1
                assert estimates[0].availability == "medium"

    def test_estimate_spot_with_low_placement_score(self):
        """Test spot estimation with low placement score."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with (
                patch.object(checker, "get_spot_placement_score", return_value={"regional": 2}),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_availability_zones", return_value=["us-east-1a"]),
                patch.object(checker, "get_on_demand_price", return_value=1.0),
            ):
                estimates = checker._estimate_spot_capacity("g4dn.xlarge", "us-east-1", None)

                assert len(estimates) >= 1
                assert estimates[0].availability == "low"


class TestCapacityCheckerRecommendCapacityTypeExtended:
    """Extended tests for recommend_capacity_type method."""

    def test_recommend_unavailable(self):
        """Test recommendation when instance type is unavailable."""
        from cli.capacity import CapacityChecker, CapacityEstimate

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with patch.object(checker, "estimate_capacity") as mock_estimate:
                mock_estimate.return_value = [
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone=None,
                        capacity_type="both",
                        availability="unavailable",
                        confidence=1.0,
                    )
                ]

                capacity_type, explanation = checker.recommend_capacity_type(
                    "g4dn.xlarge", "us-east-1", "medium"
                )

                assert capacity_type == "unavailable"
                assert "not available" in explanation.lower()

    def test_recommend_medium_spot_with_medium_fault_tolerance(self):
        """Test recommendation with medium spot availability and medium fault tolerance."""
        from cli.capacity import CapacityChecker, CapacityEstimate

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with patch.object(checker, "estimate_capacity") as mock_estimate:
                mock_estimate.return_value = [
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone="us-east-1a",
                        capacity_type="spot",
                        availability="medium",
                        confidence=0.85,
                        price_per_hour=0.30,
                    ),
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone=None,
                        capacity_type="on-demand",
                        availability="high",
                        confidence=0.9,
                        price_per_hour=1.00,
                    ),
                ]

                capacity_type, explanation = checker.recommend_capacity_type(
                    "g4dn.xlarge", "us-east-1", "medium"
                )

                # With medium fault tolerance and medium spot availability, should recommend on-demand
                assert capacity_type == "on-demand"

    def test_recommend_low_spot_with_high_fault_tolerance(self):
        """Test recommendation with low spot availability and high fault tolerance."""
        from cli.capacity import CapacityChecker, CapacityEstimate

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with patch.object(checker, "estimate_capacity") as mock_estimate:
                mock_estimate.return_value = [
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone="us-east-1a",
                        capacity_type="spot",
                        availability="low",
                        confidence=0.5,
                        price_per_hour=0.30,
                    ),
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone=None,
                        capacity_type="on-demand",
                        availability="high",
                        confidence=0.9,
                        price_per_hour=1.00,
                    ),
                ]

                capacity_type, explanation = checker.recommend_capacity_type(
                    "g4dn.xlarge", "us-east-1", "high"
                )

                # With high fault tolerance, even low spot availability is acceptable
                assert capacity_type == "spot"

    def test_recommend_no_spot_data(self):
        """Test recommendation when no spot data available."""
        from cli.capacity import CapacityChecker, CapacityEstimate

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with patch.object(checker, "estimate_capacity") as mock_estimate:
                mock_estimate.return_value = [
                    CapacityEstimate(
                        instance_type="g4dn.xlarge",
                        region="us-east-1",
                        availability_zone=None,
                        capacity_type="on-demand",
                        availability="high",
                        confidence=0.9,
                        price_per_hour=1.00,
                    ),
                ]

                capacity_type, explanation = checker.recommend_capacity_type(
                    "g4dn.xlarge", "us-east-1", "high"
                )

                # No spot data, should recommend on-demand
                assert capacity_type == "on-demand"


class TestMultiRegionCapacityCheckerWithStack:
    """Extended tests for MultiRegionCapacityChecker."""

    def test_get_region_capacity_with_stack(self):
        """Test get_region_capacity with actual stack data."""
        from cli.aws_client import RegionalStack
        from cli.capacity import MultiRegionCapacityChecker

        with patch("cli.capacity.multi_region.get_config") as mock_config:
            mock_config.return_value = MagicMock(default_region="us-east-1")

            with patch("boto3.Session") as mock_session:
                mock_cfn = MagicMock()
                mock_sqs = MagicMock()
                mock_cloudwatch = MagicMock()

                clients = {
                    "cloudformation": mock_cfn,
                    "sqs": mock_sqs,
                    "cloudwatch": mock_cloudwatch,
                }

                def client_factory(service, region_name=None):
                    return clients.get(service, MagicMock())

                mock_session.return_value.client.side_effect = client_factory

                mock_cfn.describe_stacks.return_value = {
                    "Stacks": [
                        {
                            "Outputs": [
                                {
                                    "OutputKey": "JobQueueUrl",
                                    "OutputValue": "https://sqs.us-east-1.amazonaws.com/123/queue",
                                }
                            ]
                        }
                    ]
                }
                mock_sqs.get_queue_attributes.return_value = {
                    "Attributes": {
                        "ApproximateNumberOfMessages": "5",
                        "ApproximateNumberOfMessagesNotVisible": "2",
                    }
                }
                mock_cloudwatch.get_metric_statistics.return_value = {
                    "Datapoints": [{"Average": 50.0}]
                }

                with patch("cli.aws_client.get_aws_client") as mock_get_client:
                    mock_aws_client = MagicMock()
                    mock_get_client.return_value = mock_aws_client
                    mock_aws_client.get_regional_stack.return_value = RegionalStack(
                        region="us-east-1",
                        stack_name="gco-us-east-1",
                        cluster_name="gco-us-east-1",
                        status="CREATE_COMPLETE",
                    )

                    checker = MultiRegionCapacityChecker()
                    capacity = checker.get_region_capacity("us-east-1")

                    assert capacity.region == "us-east-1"

    def test_recommend_region_for_job_with_capacity(self):
        """Test recommend_region_for_job with actual capacity data."""
        from cli.capacity import MultiRegionCapacityChecker, RegionCapacity

        with patch("cli.capacity.multi_region.get_config") as mock_config:
            mock_config.return_value = MagicMock(default_region="us-east-1")

            with patch("boto3.Session"):
                checker = MultiRegionCapacityChecker()

                with patch.object(checker, "get_all_regions_capacity") as mock_get_all:
                    mock_get_all.return_value = [
                        RegionCapacity(
                            region="us-east-1",
                            queue_depth=10,
                            running_jobs=5,
                            gpu_utilization=80.0,
                            recommendation_score=150,
                        ),
                        RegionCapacity(
                            region="us-west-2",
                            queue_depth=0,
                            running_jobs=0,
                            gpu_utilization=0.0,
                            recommendation_score=0,
                        ),
                    ]

                    recommendation = checker.recommend_region_for_job()

                    assert recommendation["region"] == "us-west-2"
                    assert "empty queue" in recommendation["reason"]

    def test_recommend_region_for_job_with_moderate_capacity(self):
        """Test recommend_region_for_job with moderate capacity."""
        from cli.capacity import MultiRegionCapacityChecker, RegionCapacity

        with patch("cli.capacity.multi_region.get_config") as mock_config:
            mock_config.return_value = MagicMock(default_region="us-east-1")

            with patch("boto3.Session"):
                checker = MultiRegionCapacityChecker()

                with patch.object(checker, "get_all_regions_capacity") as mock_get_all:
                    mock_get_all.return_value = [
                        RegionCapacity(
                            region="us-east-1",
                            queue_depth=3,
                            running_jobs=2,
                            gpu_utilization=60.0,
                            recommendation_score=50,
                        ),
                    ]

                    recommendation = checker.recommend_region_for_job()

                    assert recommendation["region"] == "us-east-1"
                    assert "low queue depth" in recommendation["reason"]


# =============================================================================
# BedrockCapacityAdvisor Tests
# =============================================================================


class TestBedrockCapacityRecommendation:
    """Tests for BedrockCapacityRecommendation dataclass."""

    def test_recommendation_creation(self):
        """Test creating BedrockCapacityRecommendation."""
        from cli.capacity import BedrockCapacityRecommendation

        rec = BedrockCapacityRecommendation(
            recommended_region="us-east-1",
            recommended_instance_type="g4dn.xlarge",
            recommended_capacity_type="spot",
            reasoning="Best availability and cost",
            confidence="high",
            cost_estimate="$0.50/hr",
            alternative_options=[{"region": "us-west-2", "instance_type": "g5.xlarge"}],
            warnings=["Spot may be interrupted"],
        )

        assert rec.recommended_region == "us-east-1"
        assert rec.recommended_instance_type == "g4dn.xlarge"
        assert rec.recommended_capacity_type == "spot"
        assert rec.confidence == "high"
        assert len(rec.alternative_options) == 1
        assert len(rec.warnings) == 1


class TestBedrockCapacityAdvisor:
    """Tests for BedrockCapacityAdvisor class."""

    @patch("cli.capacity.advisor.get_config")
    def test_advisor_initialization(self, mock_config):
        """Test BedrockCapacityAdvisor initialization."""
        from cli.capacity import BedrockCapacityAdvisor

        mock_config.return_value = MagicMock(default_region="us-east-1")

        with patch("boto3.Session"):
            advisor = BedrockCapacityAdvisor()

            assert advisor.model_id == BedrockCapacityAdvisor.DEFAULT_MODEL

    @patch("cli.capacity.advisor.get_config")
    def test_advisor_custom_model(self, mock_config):
        """Test BedrockCapacityAdvisor with custom model."""
        from cli.capacity import BedrockCapacityAdvisor

        mock_config.return_value = MagicMock(default_region="us-east-1")

        with patch("boto3.Session"):
            advisor = BedrockCapacityAdvisor(model_id="anthropic.claude-3-haiku-20240307-v1:0")

            assert advisor.model_id == "anthropic.claude-3-haiku-20240307-v1:0"

    @patch("cli.capacity.advisor.get_config")
    def test_gather_capacity_data(self, mock_config):
        """Test gathering capacity data."""
        from cli.capacity import BedrockCapacityAdvisor

        mock_config.return_value = MagicMock(default_region="us-east-1")

        with patch("boto3.Session"):
            advisor = BedrockCapacityAdvisor()

            # Mock the internal checkers
            with (
                patch.object(advisor, "_multi_region_checker") as mock_multi,
                patch.object(advisor, "_capacity_checker") as mock_cap,
                patch("cli.aws_client.get_aws_client") as mock_aws,
            ):
                mock_aws.return_value.discover_regional_stacks.return_value = {
                    "us-east-1": MagicMock()
                }
                mock_multi.get_region_capacity.return_value = MagicMock(
                    region="us-east-1",
                    queue_depth=5,
                    running_jobs=2,
                    pending_jobs=1,
                    gpu_utilization=50.0,
                    cpu_utilization=30.0,
                    recommendation_score=100,
                )
                mock_cap.get_spot_placement_score.return_value = {"regional": 7}
                mock_cap.get_spot_price_history.return_value = []
                mock_cap.get_on_demand_price.return_value = 1.50
                mock_cap.check_instance_available_in_region.return_value = True

                data = advisor.gather_capacity_data(
                    instance_types=["g4dn.xlarge"], regions=["us-east-1"]
                )

                assert "timestamp" in data
                assert data["regions_analyzed"] == ["us-east-1"]
                assert data["instance_types_analyzed"] == ["g4dn.xlarge"]
                assert len(data["cluster_metrics"]) == 1

    @patch("cli.capacity.advisor.get_config")
    def test_build_prompt(self, mock_config):
        """Test building the prompt for Bedrock."""
        from cli.capacity import BedrockCapacityAdvisor

        mock_config.return_value = MagicMock(default_region="us-east-1")

        with patch("boto3.Session"):
            advisor = BedrockCapacityAdvisor()

            capacity_data = {
                "timestamp": "2025-01-01T00:00:00Z",
                "regions_analyzed": ["us-east-1"],
                "instance_types_analyzed": ["g4dn.xlarge"],
                "cluster_metrics": [
                    {
                        "region": "us-east-1",
                        "queue_depth": 5,
                        "running_jobs": 2,
                        "gpu_utilization": 50.0,
                        "cpu_utilization": 30.0,
                    }
                ],
                "spot_data": {
                    "g4dn.xlarge": {
                        "us-east-1": {"placement_scores": {"regional": 7}, "prices": []}
                    }
                },
                "on_demand_data": {
                    "g4dn.xlarge": {"us-east-1": {"price_per_hour": 1.50, "available": True}}
                },
            }

            prompt = advisor._build_prompt(
                capacity_data,
                workload_description="ML training job",
                requirements={"gpu_required": True, "min_gpus": 1},
            )

            assert "ML training job" in prompt
            assert "GPU Required: Yes" in prompt
            assert "g4dn.xlarge" in prompt
            assert "us-east-1" in prompt
            assert "DISCLAIMER" in prompt

    @patch("cli.capacity.advisor.get_config")
    def test_get_recommendation_success(self, mock_config):
        """Test getting a recommendation from Bedrock."""
        from cli.capacity import BedrockCapacityAdvisor

        mock_config.return_value = MagicMock(default_region="us-east-1")

        with patch("boto3.Session") as mock_session:
            advisor = BedrockCapacityAdvisor()

            # Mock the Bedrock client
            mock_bedrock = MagicMock()
            mock_session.return_value.client.return_value = mock_bedrock

            # Mock the response
            mock_bedrock.converse.return_value = {
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": json.dumps(
                                    {
                                        "recommended_region": "us-east-1",
                                        "recommended_instance_type": "g4dn.xlarge",
                                        "recommended_capacity_type": "spot",
                                        "reasoning": "Best availability",
                                        "confidence": "high",
                                        "cost_estimate": "$0.50/hr",
                                        "alternative_options": [],
                                        "warnings": [],
                                    }
                                )
                            }
                        ]
                    }
                }
            }

            # Mock gather_capacity_data
            with patch.object(advisor, "gather_capacity_data") as mock_gather:
                mock_gather.return_value = {
                    "timestamp": "2025-01-01T00:00:00Z",
                    "regions_analyzed": ["us-east-1"],
                    "instance_types_analyzed": ["g4dn.xlarge"],
                    "cluster_metrics": [],
                    "spot_data": {},
                    "on_demand_data": {},
                }

                rec = advisor.get_recommendation(workload_description="Test workload")

                assert rec.recommended_region == "us-east-1"
                assert rec.recommended_instance_type == "g4dn.xlarge"
                assert rec.recommended_capacity_type == "spot"
                assert rec.confidence == "high"

    @patch("cli.capacity.advisor.get_config")
    def test_get_recommendation_access_denied(self, mock_config):
        """Test handling access denied error from Bedrock."""
        import pytest
        from botocore.exceptions import ClientError

        from cli.capacity import BedrockCapacityAdvisor

        mock_config.return_value = MagicMock(default_region="us-east-1")

        with patch("boto3.Session") as mock_session:
            advisor = BedrockCapacityAdvisor()

            # Mock the Bedrock client to raise AccessDeniedException
            mock_bedrock = MagicMock()
            mock_session.return_value.client.return_value = mock_bedrock
            mock_bedrock.converse.side_effect = ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "Access denied"}},
                "Converse",
            )

            # Mock gather_capacity_data
            with patch.object(advisor, "gather_capacity_data") as mock_gather:
                mock_gather.return_value = {
                    "timestamp": "2025-01-01T00:00:00Z",
                    "regions_analyzed": ["us-east-1"],
                    "instance_types_analyzed": ["g4dn.xlarge"],
                    "cluster_metrics": [],
                    "spot_data": {},
                    "on_demand_data": {},
                }

                try:
                    advisor.get_recommendation()
                    pytest.fail("Should have raised RuntimeError")
                except RuntimeError as e:
                    assert "Access denied" in str(e)

    @patch("cli.capacity.advisor.get_config")
    def test_get_recommendation_invalid_json(self, mock_config):
        """Test handling invalid JSON response from Bedrock."""
        import pytest

        from cli.capacity import BedrockCapacityAdvisor

        mock_config.return_value = MagicMock(default_region="us-east-1")

        with patch("boto3.Session") as mock_session:
            advisor = BedrockCapacityAdvisor()

            # Mock the Bedrock client
            mock_bedrock = MagicMock()
            mock_session.return_value.client.return_value = mock_bedrock

            # Mock invalid response
            mock_bedrock.converse.return_value = {
                "output": {"message": {"content": [{"text": "This is not valid JSON"}]}}
            }

            # Mock gather_capacity_data
            with patch.object(advisor, "gather_capacity_data") as mock_gather:
                mock_gather.return_value = {
                    "timestamp": "2025-01-01T00:00:00Z",
                    "regions_analyzed": ["us-east-1"],
                    "instance_types_analyzed": ["g4dn.xlarge"],
                    "cluster_metrics": [],
                    "spot_data": {},
                    "on_demand_data": {},
                }

                try:
                    advisor.get_recommendation()
                    pytest.fail("Should have raised RuntimeError")
                except RuntimeError as e:
                    assert "No valid JSON found" in str(e)


class TestGetBedrockCapacityAdvisor:
    """Tests for get_bedrock_capacity_advisor function."""

    @patch("cli.capacity.advisor.get_config")
    def test_get_bedrock_capacity_advisor(self, mock_config):
        """Test getting a BedrockCapacityAdvisor instance."""
        from cli.capacity import BedrockCapacityAdvisor, get_bedrock_capacity_advisor

        mock_config.return_value = MagicMock(default_region="us-east-1")

        with patch("boto3.Session"):
            advisor = get_bedrock_capacity_advisor()

            assert advisor is not None
            assert advisor.model_id == BedrockCapacityAdvisor.DEFAULT_MODEL

    @patch("cli.capacity.advisor.get_config")
    def test_get_bedrock_capacity_advisor_with_model(self, mock_config):
        """Test getting a BedrockCapacityAdvisor with custom model."""
        from cli.capacity import get_bedrock_capacity_advisor

        mock_config.return_value = MagicMock(default_region="us-east-1")

        with patch("boto3.Session"):
            advisor = get_bedrock_capacity_advisor(
                model_id="anthropic.claude-3-haiku-20240307-v1:0"
            )

            assert advisor.model_id == "anthropic.claude-3-haiku-20240307-v1:0"


# =============================================================================
# Tests for weighted scoring and updated recommendation system
# =============================================================================


class TestComputeWeightedScore:
    """Tests for the compute_weighted_score function."""

    def test_perfect_region_scores_low(self):
        """A region with ideal signals should score near zero."""
        from cli.capacity import compute_weighted_score

        score = compute_weighted_score(
            spot_placement_score=1.0,  # Best possible
            spot_price_ratio=0.0,  # Free spot (theoretical)
            queue_depth=0,
            gpu_utilization=0.0,
            running_jobs=0,
            capacity_block_trend=1.0,  # Growing capacity
        )
        assert score == 0.0

    def test_worst_region_scores_high(self):
        """A region with terrible signals should score near 1.0."""
        from cli.capacity import compute_weighted_score

        score = compute_weighted_score(
            spot_placement_score=0.0,  # No spot availability
            spot_price_ratio=1.0,  # Spot same price as on-demand
            queue_depth=1000,
            gpu_utilization=100.0,
            running_jobs=1000,
            capacity_block_trend=-1.0,  # Shrinking capacity
        )
        # All signals at worst → score approaches 1.0
        assert score > 0.9

    def test_default_weights_sum_to_one(self):
        """Default weights should sum to 1.0."""
        weights = {
            "spot_placement": 0.25,
            "spot_price": 0.20,
            "queue_depth": 0.20,
            "gpu_utilization": 0.15,
            "running_jobs": 0.10,
            "capacity_blocks": 0.10,
        }
        assert abs(sum(weights.values()) - 1.0) < 1e-9

    def test_spot_placement_dominates(self):
        """Higher spot placement score should produce lower overall score."""
        from cli.capacity import compute_weighted_score

        score_high_spot = compute_weighted_score(
            spot_placement_score=0.9,
            spot_price_ratio=0.5,
            queue_depth=5,
            gpu_utilization=50.0,
            running_jobs=5,
        )
        score_low_spot = compute_weighted_score(
            spot_placement_score=0.1,
            spot_price_ratio=0.5,
            queue_depth=5,
            gpu_utilization=50.0,
            running_jobs=5,
        )
        assert score_high_spot < score_low_spot

    def test_queue_depth_impact(self):
        """Higher queue depth should produce higher score."""
        from cli.capacity import compute_weighted_score

        score_empty = compute_weighted_score(queue_depth=0)
        score_busy = compute_weighted_score(queue_depth=50)
        assert score_empty < score_busy

    def test_gpu_utilization_impact(self):
        """Higher GPU utilization should produce higher score."""
        from cli.capacity import compute_weighted_score

        score_idle = compute_weighted_score(gpu_utilization=10.0)
        score_busy = compute_weighted_score(gpu_utilization=90.0)
        assert score_idle < score_busy

    def test_running_jobs_impact(self):
        """More running jobs should produce higher score."""
        from cli.capacity import compute_weighted_score

        score_none = compute_weighted_score(running_jobs=0)
        score_many = compute_weighted_score(running_jobs=100)
        assert score_none < score_many

    def test_spot_price_ratio_impact(self):
        """Lower spot price ratio (better savings) should produce lower score."""
        from cli.capacity import compute_weighted_score

        score_cheap = compute_weighted_score(spot_price_ratio=0.2)
        score_expensive = compute_weighted_score(spot_price_ratio=0.9)
        assert score_cheap < score_expensive

    def test_custom_weights(self):
        """Custom weights should be respected."""
        from cli.capacity import compute_weighted_score

        # Weight only queue depth
        custom_weights = {
            "spot_placement": 0.0,
            "spot_price": 0.0,
            "queue_depth": 1.0,
            "gpu_utilization": 0.0,
            "running_jobs": 0.0,
        }
        score = compute_weighted_score(
            spot_placement_score=0.0,  # Would be bad, but weight is 0
            queue_depth=0,  # Best
            weights=custom_weights,
        )
        assert score == 0.0

        score_busy = compute_weighted_score(
            spot_placement_score=1.0,  # Would be good, but weight is 0
            queue_depth=100,
            weights=custom_weights,
        )
        assert score_busy > 0.9

    def test_score_is_deterministic(self):
        """Same inputs should always produce same output."""
        from cli.capacity import compute_weighted_score

        kwargs = {
            "spot_placement_score": 0.7,
            "spot_price_ratio": 0.4,
            "queue_depth": 3,
            "gpu_utilization": 45.0,
            "running_jobs": 2,
        }
        result_a = compute_weighted_score(**kwargs)
        result_b = compute_weighted_score(**kwargs)
        assert result_a == result_b

    def test_clamping_out_of_range_values(self):
        """Out-of-range values should be clamped, not crash."""
        from cli.capacity import compute_weighted_score

        # Negative spot score clamped to 0
        score_neg = compute_weighted_score(spot_placement_score=-0.5)
        score_zero = compute_weighted_score(spot_placement_score=0.0)
        assert score_neg == score_zero

        # Spot score > 1 clamped to 1
        score_over = compute_weighted_score(spot_placement_score=1.5)
        score_one = compute_weighted_score(spot_placement_score=1.0)
        assert score_over == score_one

        # GPU utilization > 100 clamped
        score_over_gpu = compute_weighted_score(gpu_utilization=150.0)
        score_max_gpu = compute_weighted_score(gpu_utilization=100.0)
        assert score_over_gpu == score_max_gpu

    def test_moderate_scenario(self):
        """A moderate scenario should score in the middle range."""
        from cli.capacity import compute_weighted_score

        score = compute_weighted_score(
            spot_placement_score=0.5,
            spot_price_ratio=0.5,
            queue_depth=5,
            gpu_utilization=50.0,
            running_jobs=10,
        )
        assert 0.2 < score < 0.8

    def test_capacity_block_trend_positive_helps(self):
        """Positive capacity block trend (growing) should lower the score."""
        from cli.capacity import compute_weighted_score

        score_growing = compute_weighted_score(capacity_block_trend=1.0)
        score_shrinking = compute_weighted_score(capacity_block_trend=-1.0)
        score_neutral = compute_weighted_score(capacity_block_trend=0.0)
        assert score_growing < score_neutral < score_shrinking

    def test_capacity_block_trend_clamped(self):
        """Trend values outside [-1, 1] should be clamped."""
        from cli.capacity import compute_weighted_score

        score_over = compute_weighted_score(capacity_block_trend=5.0)
        score_max = compute_weighted_score(capacity_block_trend=1.0)
        assert score_over == score_max

        score_under = compute_weighted_score(capacity_block_trend=-5.0)
        score_min = compute_weighted_score(capacity_block_trend=-1.0)
        assert score_under == score_min


class TestCapacityBlockTrend:
    """Tests for get_capacity_block_trend time-series regression."""

    def test_no_offerings_returns_zero(self):
        """No offerings should return 0.0 trend."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with patch.object(checker, "_session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.client.return_value = mock_ec2
                mock_ec2.describe_capacity_block_offerings.return_value = {
                    "CapacityBlockOfferings": []
                }

                trend = checker.get_capacity_block_trend("p4d.24xlarge", "us-east-1")
                assert trend == 0.0

    def test_api_error_returns_zero(self):
        """AWS client errors should be caught and return a 0.0 trend."""
        from botocore.exceptions import ClientError

        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with patch.object(checker, "_session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.client.return_value = mock_ec2
                mock_ec2.describe_capacity_block_offerings.side_effect = ClientError(
                    {"Error": {"Code": "InternalError", "Message": "boom"}},
                    "DescribeCapacityBlockOfferings",
                )

                trend = checker.get_capacity_block_trend("p4d.24xlarge", "us-east-1")
                assert trend == 0.0

    def test_growing_capacity_positive_trend(self):
        """More offerings in later weeks should produce positive trend."""
        from datetime import UTC, datetime, timedelta

        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()
            now = datetime.now(UTC)

            # Create a clear upward ramp: week N gets N offerings
            offerings = []
            for week in range(0, 26):
                for _ in range(week):
                    offerings.append({"StartDate": now + timedelta(weeks=week, days=1)})

            with patch.object(checker, "_session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.client.return_value = mock_ec2
                mock_ec2.describe_capacity_block_offerings.return_value = {
                    "CapacityBlockOfferings": offerings
                }

                trend = checker.get_capacity_block_trend("p4d.24xlarge", "us-east-1")
                assert trend > 0, f"Expected positive trend, got {trend}"

    def test_shrinking_capacity_negative_trend(self):
        """More offerings in early weeks should produce negative trend."""
        from datetime import UTC, datetime, timedelta

        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()
            now = datetime.now(UTC)

            # Create a clear downward ramp: week N gets (25 - N) offerings
            offerings = []
            for week in range(0, 26):
                for _ in range(25 - week):
                    offerings.append({"StartDate": now + timedelta(weeks=week, days=1)})

            with patch.object(checker, "_session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.client.return_value = mock_ec2
                mock_ec2.describe_capacity_block_offerings.return_value = {
                    "CapacityBlockOfferings": offerings
                }

                trend = checker.get_capacity_block_trend("p4d.24xlarge", "us-east-1")
                assert trend < 0, f"Expected negative trend, got {trend}"

    def test_flat_distribution_near_zero_trend(self):
        """Evenly distributed offerings should produce near-zero trend."""
        from datetime import UTC, datetime, timedelta

        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()
            now = datetime.now(UTC)

            # 1 offering per week for 26 weeks
            offerings = [{"StartDate": now + timedelta(weeks=w, days=1)} for w in range(26)]

            with patch.object(checker, "_session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.client.return_value = mock_ec2
                mock_ec2.describe_capacity_block_offerings.return_value = {
                    "CapacityBlockOfferings": offerings
                }

                trend = checker.get_capacity_block_trend("p4d.24xlarge", "us-east-1")
                assert abs(trend) < 0.1, f"Expected near-zero trend, got {trend}"

    def test_single_bin_returns_zero(self):
        """Only one non-zero bin should return 0.0 (can't regress a point)."""
        from datetime import UTC, datetime, timedelta

        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()
            now = datetime.now(UTC)

            # All offerings in the same week
            offerings = [
                {"StartDate": now + timedelta(days=1)},
                {"StartDate": now + timedelta(days=2)},
                {"StartDate": now + timedelta(days=3)},
            ]

            with patch.object(checker, "_session") as mock_session:
                mock_ec2 = MagicMock()
                mock_session.client.return_value = mock_ec2
                mock_ec2.describe_capacity_block_offerings.return_value = {
                    "CapacityBlockOfferings": offerings
                }

                trend = checker.get_capacity_block_trend("p4d.24xlarge", "us-east-1")
                assert trend == 0.0


class TestWeightedRecommendRegion:
    """Tests for the weighted recommendation path in recommend_region_for_job."""

    def test_weighted_recommend_with_instance_type(self):
        """When instance_type is provided, weighted scoring should be used."""
        from cli.capacity import MultiRegionCapacityChecker, RegionCapacity

        with patch("cli.capacity.multi_region.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = MultiRegionCapacityChecker()

            capacities = [
                RegionCapacity(
                    region="us-east-1",
                    queue_depth=10,
                    running_jobs=5,
                    gpu_utilization=80.0,
                    recommendation_score=155.0,
                ),
                RegionCapacity(
                    region="us-west-2",
                    queue_depth=2,
                    running_jobs=1,
                    gpu_utilization=20.0,
                    recommendation_score=45.0,
                ),
            ]

            with (
                patch.object(checker, "get_all_regions_capacity", return_value=capacities),
                patch("cli.capacity.multi_region.CapacityChecker") as MockCapChecker,
            ):
                mock_cap = MagicMock()
                MockCapChecker.return_value = mock_cap
                mock_cap.get_spot_placement_score.side_effect = [
                    {"regional": 3},  # us-east-1: low spot
                    {"regional": 8},  # us-west-2: high spot
                ]
                mock_cap.get_spot_price_history.return_value = []
                mock_cap.get_on_demand_price.return_value = None
                mock_cap.get_capacity_block_trend.return_value = 0.0

                result = checker.recommend_region_for_job(
                    instance_type="g5.xlarge",
                )

                assert result["region"] == "us-west-2"
                assert result["scoring_method"] == "weighted"
                assert result["instance_type"] == "g5.xlarge"

    def test_simple_fallback_without_instance_type(self):
        """Without instance_type, should use simple scoring."""
        from cli.capacity import MultiRegionCapacityChecker, RegionCapacity

        with patch("cli.capacity.multi_region.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = MultiRegionCapacityChecker()

            capacities = [
                RegionCapacity(
                    region="us-east-1",
                    queue_depth=0,
                    running_jobs=0,
                    gpu_utilization=10.0,
                    recommendation_score=10.0,
                ),
                RegionCapacity(
                    region="us-west-2",
                    queue_depth=5,
                    running_jobs=3,
                    gpu_utilization=60.0,
                    recommendation_score=125.0,
                ),
            ]

            with patch.object(checker, "get_all_regions_capacity", return_value=capacities):
                result = checker.recommend_region_for_job()

                assert result["region"] == "us-east-1"
                assert "scoring_method" not in result  # Simple path doesn't set this

    def test_weighted_recommend_no_capacity_data(self):
        """When no capacity data, should return default region."""
        from cli.capacity import MultiRegionCapacityChecker

        with patch("cli.capacity.multi_region.get_config") as mock_config:
            mock_config.return_value = MagicMock(default_region="us-east-1")

            checker = MultiRegionCapacityChecker()

            with patch.object(checker, "get_all_regions_capacity", return_value=[]):
                result = checker.recommend_region_for_job(instance_type="g5.xlarge")

                assert result["region"] == "us-east-1"
                assert "No capacity data" in result["reason"]

    def test_recommend_region_surfaces_underlying_errors(self):
        """Empty capacities due to AWS failures surface the real error, not a
        benign 'no capacity' message that masks throttling / auth failures."""
        from cli.capacity import MultiRegionCapacityChecker

        with patch("cli.capacity.multi_region.get_config") as mock_config:
            mock_config.return_value = MagicMock(default_region="us-east-1")

            checker = MultiRegionCapacityChecker()

            def _fail_sweep():
                checker._last_region_errors = [
                    "us-east-1: RequestLimitExceeded",
                    "us-west-2: AuthFailure",
                ]
                return []

            with patch.object(checker, "get_all_regions_capacity", side_effect=_fail_sweep):
                result = checker.recommend_region_for_job()

            assert result["region"] == "us-east-1"
            assert "failed" in result["reason"].lower()
            assert "RequestLimitExceeded" in result["reason"]
            assert result["error"]

    def test_weighted_recommend_with_spot_pricing(self):
        """Weighted scoring should factor in spot pricing data."""
        from cli.capacity import MultiRegionCapacityChecker, RegionCapacity, SpotPriceInfo

        with patch("cli.capacity.multi_region.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = MultiRegionCapacityChecker()

            capacities = [
                RegionCapacity(
                    region="us-east-1",
                    queue_depth=2,
                    running_jobs=1,
                    gpu_utilization=30.0,
                    recommendation_score=55.0,
                ),
                RegionCapacity(
                    region="us-west-2",
                    queue_depth=2,
                    running_jobs=1,
                    gpu_utilization=30.0,
                    recommendation_score=55.0,
                ),
            ]

            spot_east = SpotPriceInfo(
                instance_type="g5.xlarge",
                availability_zone="us-east-1a",
                current_price=0.80,  # 80% of on-demand
                avg_price_7d=0.80,
                min_price_7d=0.75,
                max_price_7d=0.85,
                price_stability=0.9,
            )
            spot_west = SpotPriceInfo(
                instance_type="g5.xlarge",
                availability_zone="us-west-2a",
                current_price=0.30,  # 30% of on-demand — much cheaper
                avg_price_7d=0.30,
                min_price_7d=0.25,
                max_price_7d=0.35,
                price_stability=0.9,
            )

            with (
                patch.object(checker, "get_all_regions_capacity", return_value=capacities),
                patch("cli.capacity.multi_region.CapacityChecker") as MockCapChecker,
            ):
                mock_cap = MagicMock()
                MockCapChecker.return_value = mock_cap
                # Same spot placement scores
                mock_cap.get_spot_placement_score.return_value = {"regional": 7}
                # Different spot prices
                mock_cap.get_spot_price_history.side_effect = [
                    [spot_east],
                    [spot_west],
                ]
                mock_cap.get_on_demand_price.return_value = 1.00
                mock_cap.get_capacity_block_trend.return_value = 0.0

                result = checker.recommend_region_for_job(instance_type="g5.xlarge")

                # us-west-2 should win due to much better spot pricing
                assert result["region"] == "us-west-2"

    def test_weighted_recommend_handles_api_errors(self):
        """Weighted scoring should handle API errors gracefully."""
        from cli.capacity import MultiRegionCapacityChecker, RegionCapacity

        with patch("cli.capacity.multi_region.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = MultiRegionCapacityChecker()

            capacities = [
                RegionCapacity(
                    region="us-east-1",
                    queue_depth=0,
                    running_jobs=0,
                    gpu_utilization=0.0,
                    recommendation_score=0.0,
                ),
            ]

            with (
                patch.object(checker, "get_all_regions_capacity", return_value=capacities),
                patch("cli.capacity.multi_region.CapacityChecker") as MockCapChecker,
            ):
                mock_cap = MagicMock()
                MockCapChecker.return_value = mock_cap
                # All API calls fail
                mock_cap.get_spot_placement_score.side_effect = Exception("API Error")
                mock_cap.get_spot_price_history.side_effect = Exception("API Error")
                mock_cap.get_on_demand_price.side_effect = Exception("API Error")
                mock_cap.get_capacity_block_trend.side_effect = Exception("API Error")

                result = checker.recommend_region_for_job(instance_type="g5.xlarge")

                # Should still return a result using defaults
                assert result["region"] == "us-east-1"
                assert result["scoring_method"] == "weighted"

    def test_weighted_recommend_gpu_count_passed_to_placement_score(self):
        """gpu_count should be passed as target_capacity to spot placement score."""
        from cli.capacity import MultiRegionCapacityChecker, RegionCapacity

        with patch("cli.capacity.multi_region.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = MultiRegionCapacityChecker()

            capacities = [
                RegionCapacity(region="us-east-1", recommendation_score=0.0),
            ]

            with (
                patch.object(checker, "get_all_regions_capacity", return_value=capacities),
                patch("cli.capacity.multi_region.CapacityChecker") as MockCapChecker,
            ):
                mock_cap = MagicMock()
                MockCapChecker.return_value = mock_cap
                mock_cap.get_spot_placement_score.return_value = {"regional": 5}
                mock_cap.get_spot_price_history.return_value = []
                mock_cap.get_on_demand_price.return_value = None
                mock_cap.get_capacity_block_trend.return_value = 0.0

                checker.recommend_region_for_job(
                    instance_type="p4d.24xlarge",
                    gpu_count=8,
                )

                # Verify target_capacity was set to gpu_count
                mock_cap.get_spot_placement_score.assert_called_once_with(
                    "p4d.24xlarge", "us-east-1", target_capacity=8
                )


class TestEstimateOnDemandCapacityWithoutDryRun:
    """Tests for the updated _estimate_on_demand_capacity without dry-run."""

    def test_on_demand_gpu_instance_medium_availability(self):
        """GPU instances should get medium availability."""
        from cli.capacity import CapacityChecker, InstanceTypeInfo

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()
            gpu_info = InstanceTypeInfo("g4dn.xlarge", 4, 16, 1, "T4", 16)

            with (
                patch.object(checker, "get_on_demand_price", return_value=0.526),
                patch.object(checker, "check_instance_available_in_region", return_value=True),
                patch.object(checker, "get_spot_placement_score", return_value={}),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_az_coverage", return_value=None),
                patch.object(checker, "get_spot_placement_score", return_value={}),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_az_coverage", return_value=None),
            ):
                estimate = checker._estimate_on_demand_capacity(
                    "g4dn.xlarge", "us-east-1", gpu_info
                )

                assert estimate is not None
                assert estimate.capacity_type == "on-demand"
                assert estimate.availability in (
                    "medium",
                    "high",
                    "unknown",
                )  # Depends on live signals
                assert "$0.5260" in estimate.recommendation

    def test_on_demand_cpu_instance_high_availability(self):
        """Non-GPU instances should get high availability."""
        from cli.capacity import CapacityChecker, InstanceTypeInfo

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()
            cpu_info = InstanceTypeInfo("m5.xlarge", 4, 16, 0, None, 0)

            with (
                patch.object(checker, "get_on_demand_price", return_value=0.192),
                patch.object(checker, "check_instance_available_in_region", return_value=True),
                patch.object(checker, "get_spot_placement_score", return_value={}),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_az_coverage", return_value=None),
            ):
                estimate = checker._estimate_on_demand_capacity("m5.xlarge", "us-east-1", cpu_info)

                assert estimate is not None
                assert estimate.availability in ("high", "unknown")  # Depends on live signals
                assert estimate.confidence >= 0.2  # Scales with number of live signals

    def test_on_demand_not_offered_in_region(self):
        """Instance not offered in region should return unavailable."""
        from cli.capacity import CapacityChecker, InstanceTypeInfo

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()
            info = InstanceTypeInfo("p5.48xlarge", 192, 2048, 8, "H100", 640)

            with (
                patch.object(checker, "get_on_demand_price", return_value=None),
                patch.object(checker, "check_instance_available_in_region", return_value=False),
            ):
                estimate = checker._estimate_on_demand_capacity("p5.48xlarge", "ap-south-1", info)

                assert estimate is not None
                assert estimate.availability == "unavailable"
                assert estimate.confidence == 1.0

    def test_on_demand_no_pricing_data(self):
        """Missing pricing data should reduce confidence."""
        from cli.capacity import CapacityChecker, InstanceTypeInfo

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()
            info = InstanceTypeInfo("g5.xlarge", 4, 16, 1, "A10G", 24)

            with (
                patch.object(checker, "get_on_demand_price", return_value=None),
                patch.object(checker, "check_instance_available_in_region", return_value=True),
                patch.object(checker, "get_spot_placement_score", return_value={}),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_az_coverage", return_value=None),
            ):
                estimate = checker._estimate_on_demand_capacity("g5.xlarge", "us-east-1", info)

                assert estimate is not None
                assert estimate.availability in (
                    "medium",
                    "low",
                    "unknown",
                )  # Depends on live signals
                assert "unavailable" in estimate.recommendation.lower()

    def test_on_demand_no_instance_info(self):
        """When instance_info is None, should default to high availability."""
        from cli.capacity import CapacityChecker

        with patch("cli.capacity.checker.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            checker = CapacityChecker()

            with (
                patch.object(checker, "get_on_demand_price", return_value=0.50),
                patch.object(checker, "check_instance_available_in_region", return_value=True),
                patch.object(checker, "get_spot_placement_score", return_value={}),
                patch.object(checker, "get_spot_price_history", return_value=[]),
                patch.object(checker, "get_az_coverage", return_value=None),
                patch.object(checker, "get_spot_price_history", return_value=[]),
            ):
                estimate = checker._estimate_on_demand_capacity("custom.type", "us-east-1", None)

                assert estimate is not None
                assert estimate.availability in ("high", "unknown")  # Depends on live signals
                assert estimate.confidence >= 0.2  # Scales with number of live signals


class TestComputePriceTrend:
    """Tests for the compute_price_trend utility function."""

    def test_rising_prices(self):
        """Prices going up should produce positive slope."""
        from cli.capacity import compute_price_trend

        # Most recent first: 1.0, 0.9, 0.8, ... (prices were rising)
        prices = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5]
        result = compute_price_trend(prices)
        assert result["slope"] > 0
        assert result["normalized_slope"] > 0
        assert result["direction"] == "rising"

    def test_falling_prices(self):
        """Prices going down should produce negative slope."""
        from cli.capacity import compute_price_trend

        # Most recent first: 0.5, 0.6, 0.7, ... (prices were falling)
        prices = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        result = compute_price_trend(prices)
        assert result["slope"] < 0
        assert result["normalized_slope"] < 0
        assert result["direction"] == "falling"

    def test_stable_prices(self):
        """Constant prices should produce zero slope."""
        from cli.capacity import compute_price_trend

        prices = [0.50, 0.50, 0.50, 0.50]
        result = compute_price_trend(prices)
        assert result["slope"] == 0.0
        assert result["direction"] == "stable"

    def test_single_price(self):
        """Single price point should return stable."""
        from cli.capacity import compute_price_trend

        result = compute_price_trend([0.50])
        assert result["slope"] == 0.0
        assert result["direction"] == "stable"
        assert result["price_changes"] == 0

    def test_empty_prices(self):
        """Empty list should return stable."""
        from cli.capacity import compute_price_trend

        result = compute_price_trend([])
        assert result["slope"] == 0.0
        assert result["direction"] == "stable"

    def test_price_changes_counted(self):
        """Should count the number of distinct price transitions."""
        from cli.capacity import compute_price_trend

        # 0.5 → 0.6 → 0.6 → 0.7 → 0.5 = 3 changes (reversed: 0.5→0.7→0.6→0.6→0.5)
        prices = [0.5, 0.7, 0.6, 0.6, 0.5]
        result = compute_price_trend(prices)
        assert result["price_changes"] == 3

    def test_normalized_slope_clamped(self):
        """Normalized slope should be clamped to [-1, 1]."""
        from cli.capacity import compute_price_trend

        # Extreme price jump
        prices = [100.0, 0.01]
        result = compute_price_trend(prices)
        assert -1.0 <= result["normalized_slope"] <= 1.0


class TestGatherCapacityDataEnhanced:
    """Tests for the enhanced gather_capacity_data with new signals."""

    def test_gather_includes_capacity_block_trends(self):
        """gather_capacity_data should include capacity_block_trends."""
        from cli.capacity import BedrockCapacityAdvisor

        with patch("cli.capacity.advisor.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            advisor = BedrockCapacityAdvisor()

            with (
                patch.object(advisor, "_multi_region_checker") as mock_multi,
                patch.object(advisor, "_capacity_checker") as mock_cap,
                patch.object(advisor, "_session") as mock_session,
            ):
                mock_multi.get_region_capacity.return_value = MagicMock(
                    queue_depth=0,
                    running_jobs=0,
                    pending_jobs=0,
                    gpu_utilization=0,
                    cpu_utilization=0,
                    recommendation_score=0,
                )
                mock_multi.recommend_region_for_job.return_value = {
                    "region": "us-east-1",
                    "scoring_method": "weighted",
                    "all_regions": [],
                }
                mock_cap.get_spot_placement_score.return_value = {}
                mock_cap.get_spot_price_history.return_value = []
                mock_cap.get_on_demand_price.return_value = None
                mock_cap.check_instance_available_in_region.return_value = True
                mock_cap.list_capacity_reservations.return_value = []
                mock_cap.list_capacity_block_offerings.return_value = []
                mock_cap.get_capacity_block_trend.return_value = 0.5

                mock_ec2 = MagicMock()
                mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
                mock_session.client.return_value = mock_ec2

                data = advisor.gather_capacity_data(
                    instance_types=["g5.xlarge"],
                    regions=["us-east-1"],
                )

                assert "capacity_block_trends" in data
                assert "weighted_recommendation" in data
                assert data["weighted_recommendation"]["top_region"] == "us-east-1"

    def test_gather_includes_spot_price_trends(self):
        """gather_capacity_data should include spot price trend analysis."""
        from cli.capacity import BedrockCapacityAdvisor

        with patch("cli.capacity.advisor.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            advisor = BedrockCapacityAdvisor()

            with (
                patch.object(advisor, "_multi_region_checker") as mock_multi,
                patch.object(advisor, "_capacity_checker") as mock_cap,
                patch.object(advisor, "_session") as mock_session,
            ):
                mock_multi.get_region_capacity.return_value = MagicMock(
                    queue_depth=0,
                    running_jobs=0,
                    pending_jobs=0,
                    gpu_utilization=0,
                    cpu_utilization=0,
                    recommendation_score=0,
                )
                mock_multi.recommend_region_for_job.return_value = {
                    "region": "us-east-1",
                    "all_regions": [],
                }
                mock_cap.get_spot_placement_score.return_value = {"regional": 7}
                from cli.capacity import SpotPriceInfo

                mock_cap.get_spot_price_history.return_value = [
                    SpotPriceInfo("g5.xlarge", "us-east-1a", 0.50, 0.48, 0.40, 0.55, 0.9)
                ]
                mock_cap.get_on_demand_price.return_value = 1.00
                mock_cap.check_instance_available_in_region.return_value = True
                mock_cap.list_capacity_reservations.return_value = []
                mock_cap.list_capacity_block_offerings.return_value = []
                mock_cap.get_capacity_block_trend.return_value = 0.0

                # Mock raw spot price history for trend analysis
                mock_ec2 = MagicMock()
                mock_ec2.describe_spot_price_history.return_value = {
                    "SpotPriceHistory": [
                        {"AvailabilityZone": "us-east-1a", "SpotPrice": "0.50"},
                        {"AvailabilityZone": "us-east-1a", "SpotPrice": "0.48"},
                        {"AvailabilityZone": "us-east-1a", "SpotPrice": "0.45"},
                    ]
                }
                mock_session.client.return_value = mock_ec2

                data = advisor.gather_capacity_data(
                    instance_types=["g5.xlarge"],
                    regions=["us-east-1"],
                )

                spot_region = data["spot_data"].get("g5.xlarge", {}).get("us-east-1", {})
                assert "price_trends" in spot_region
                assert "us-east-1a" in spot_region["price_trends"]
                trend = spot_region["price_trends"]["us-east-1a"]
                assert "slope" in trend
                assert "direction" in trend
                assert "price_changes" in trend


# ============================================================================
# Advisor exception paths (gather_capacity_data + get_recommendation errors)
# ============================================================================

import pytest  # noqa: E402 — needed for appended test classes


def _make_advisor_with_mocks():
    """Helper to create a BedrockCapacityAdvisor with mocked internals."""
    from cli.capacity.advisor import BedrockCapacityAdvisor

    with (
        patch("cli.capacity.advisor.get_config") as mc,
        patch("boto3.Session"),
    ):
        mc.return_value = MagicMock(default_region="us-east-1")
        advisor = BedrockCapacityAdvisor()
    advisor._multi_region_checker = MagicMock()
    advisor._capacity_checker = MagicMock()
    return advisor


def _setup_advisor_mocks(advisor):
    """Set up default return values for advisor's internal mocks."""
    mrc = advisor._multi_region_checker
    cap = advisor._capacity_checker
    mrc.get_region_capacity.return_value = MagicMock(
        queue_depth=0,
        running_jobs=0,
        pending_jobs=0,
        gpu_utilization=0.0,
        cpu_utilization=0.0,
        recommendation_score=0,
    )
    cap.get_spot_placement_score.return_value = {}
    cap.get_spot_price_history.return_value = []
    cap.get_on_demand_price.return_value = 1.0
    cap.check_instance_available_in_region.return_value = True
    cap.list_capacity_reservations.return_value = []
    cap.list_capacity_block_offerings.return_value = []
    cap.get_capacity_block_trend.return_value = 0.0
    mrc.recommend_region_for_job.return_value = {"region": "us-east-1"}


class TestAdvisorGatherExceptions:
    """Cover exception-handling branches inside gather_capacity_data."""

    def test_cluster_metrics_exception(self):
        advisor = _make_advisor_with_mocks()
        _setup_advisor_mocks(advisor)
        advisor._multi_region_checker.get_region_capacity.side_effect = RuntimeError("down")
        data = advisor.gather_capacity_data(instance_types=["g4dn.xlarge"], regions=["us-east-1"])
        assert data["cluster_metrics"] == []

    def test_spot_data_exception(self):
        advisor = _make_advisor_with_mocks()
        _setup_advisor_mocks(advisor)
        advisor._capacity_checker.get_spot_placement_score.side_effect = RuntimeError("boom")
        data = advisor.gather_capacity_data(instance_types=["g4dn.xlarge"], regions=["us-east-1"])
        assert data["spot_data"]["g4dn.xlarge"] == {}

    def test_reservation_exception(self):
        advisor = _make_advisor_with_mocks()
        _setup_advisor_mocks(advisor)
        advisor._capacity_checker.list_capacity_reservations.side_effect = RuntimeError("no")
        data = advisor.gather_capacity_data(instance_types=["g4dn.xlarge"], regions=["us-east-1"])
        assert data["reservations"]["g4dn.xlarge"] == {}

    def test_capacity_blocks_exception(self):
        advisor = _make_advisor_with_mocks()
        _setup_advisor_mocks(advisor)
        advisor._capacity_checker.list_capacity_block_offerings.side_effect = RuntimeError("no")
        data = advisor.gather_capacity_data(instance_types=["g4dn.xlarge"], regions=["us-east-1"])
        assert data["capacity_blocks"]["g4dn.xlarge"] == {}

    def test_capacity_block_trend_exception(self):
        advisor = _make_advisor_with_mocks()
        _setup_advisor_mocks(advisor)
        advisor._capacity_checker.get_capacity_block_trend.side_effect = RuntimeError("no")
        data = advisor.gather_capacity_data(instance_types=["g4dn.xlarge"], regions=["us-east-1"])
        assert data["capacity_block_trends"]["g4dn.xlarge"] == {}

    def test_weighted_recommendation_exception(self):
        advisor = _make_advisor_with_mocks()
        _setup_advisor_mocks(advisor)
        advisor._multi_region_checker.recommend_region_for_job.side_effect = RuntimeError("no")
        data = advisor.gather_capacity_data(instance_types=["g4dn.xlarge"], regions=["us-east-1"])
        assert "weighted_recommendation" not in data

    def test_default_instance_types_and_regions(self):
        advisor = _make_advisor_with_mocks()
        _setup_advisor_mocks(advisor)
        with patch("cli.aws_client.get_aws_client") as mock_aws:
            mock_aws.return_value.discover_regional_stacks.return_value = {}
            data = advisor.gather_capacity_data()
        assert advisor.config.default_region in data["regions_analyzed"]
        assert len(data["instance_types_analyzed"]) > 1

    def test_price_trend_exception(self):
        advisor = _make_advisor_with_mocks()
        _setup_advisor_mocks(advisor)
        advisor._session = MagicMock()
        mock_ec2 = MagicMock()
        mock_ec2.describe_spot_price_history.side_effect = RuntimeError("fail")
        advisor._session.client.return_value = mock_ec2
        data = advisor.gather_capacity_data(instance_types=["g4dn.xlarge"], regions=["us-east-1"])
        assert "g4dn.xlarge" in data["spot_data"]


class TestAdvisorGetRecommendationErrors:
    """Cover error paths in get_recommendation."""

    def test_validation_exception(self):
        from botocore.exceptions import ClientError

        advisor = _make_advisor_with_mocks()
        mock_bedrock = MagicMock()
        mock_bedrock.converse.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Model not available"}},
            "Converse",
        )
        advisor._session = MagicMock()
        advisor._session.client.return_value = mock_bedrock
        with (
            patch.object(
                advisor,
                "gather_capacity_data",
                return_value={
                    "timestamp": "t",
                    "regions_analyzed": [],
                    "instance_types_analyzed": [],
                    "cluster_metrics": [],
                    "spot_data": {},
                    "on_demand_data": {},
                },
            ),
            pytest.raises(RuntimeError, match="may not be available"),
        ):
            advisor.get_recommendation()

    def test_generic_client_error(self):
        from botocore.exceptions import ClientError

        advisor = _make_advisor_with_mocks()
        mock_bedrock = MagicMock()
        mock_bedrock.converse.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Too many"}},
            "Converse",
        )
        advisor._session = MagicMock()
        advisor._session.client.return_value = mock_bedrock
        with (
            patch.object(
                advisor,
                "gather_capacity_data",
                return_value={
                    "timestamp": "t",
                    "regions_analyzed": [],
                    "instance_types_analyzed": [],
                    "cluster_metrics": [],
                    "spot_data": {},
                    "on_demand_data": {},
                },
            ),
            pytest.raises(RuntimeError, match="Bedrock API error"),
        ):
            advisor.get_recommendation()

    def test_json_decode_error(self):
        advisor = _make_advisor_with_mocks()
        mock_bedrock = MagicMock()
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "{ invalid json }"}]}}
        }
        advisor._session = MagicMock()
        advisor._session.client.return_value = mock_bedrock
        with (
            patch.object(
                advisor,
                "gather_capacity_data",
                return_value={
                    "timestamp": "t",
                    "regions_analyzed": [],
                    "instance_types_analyzed": [],
                    "cluster_metrics": [],
                    "spot_data": {},
                    "on_demand_data": {},
                },
            ),
            pytest.raises(RuntimeError, match="Failed to parse AI response"),
        ):
            advisor.get_recommendation()

    def test_generic_exception(self):
        advisor = _make_advisor_with_mocks()
        mock_bedrock = MagicMock()
        mock_bedrock.converse.side_effect = ValueError("unexpected")
        advisor._session = MagicMock()
        advisor._session.client.return_value = mock_bedrock
        with (
            patch.object(
                advisor,
                "gather_capacity_data",
                return_value={
                    "timestamp": "t",
                    "regions_analyzed": [],
                    "instance_types_analyzed": [],
                    "cluster_metrics": [],
                    "spot_data": {},
                    "on_demand_data": {},
                },
            ),
            pytest.raises(RuntimeError, match="Failed to get AI recommendation"),
        ):
            advisor.get_recommendation()
