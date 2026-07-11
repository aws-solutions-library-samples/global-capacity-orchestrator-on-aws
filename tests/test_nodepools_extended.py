"""
Extended tests for cli/nodepools.py.

Covers EFA-specific NodePool manifest generation: WhenEmpty
consolidation (vs WhenEmptyOrUnderutilized for non-EFA pools), the
efa=true label and workload-type=gpu-efa marker, dual taints for
nvidia.com/gpu plus vpc.amazonaws.com/efa. Also exercises non-GPU
instance types (no GPU taints emitted), NodePoolInfo construction,
and calculate_cpu_limit edge cases.
"""

from unittest.mock import patch

import yaml

from cli.nodepools import (
    NodePoolInfo,
    calculate_cpu_limit,
    generate_odcr_nodepool_manifest,
)


class TestGenerateOdcrManifestEfa:
    """Tests for generate_odcr_nodepool_manifest with EFA."""

    @patch("cli.nodepools.get_vcpus_for_instance_type", return_value=96)
    def test_efa_sets_when_empty_consolidation(self, mock_vcpus):
        """EFA nodepools should use WhenEmpty consolidation for long-running jobs."""
        manifest = generate_odcr_nodepool_manifest(
            name="efa-pool",
            region="us-east-1",
            capacity_reservation_id="cr-123",
            instance_types=["p4d.24xlarge"],
            efa=True,
        )
        docs = list(yaml.safe_load_all(manifest))
        nodepool = [d for d in docs if d and d.get("kind") == "NodePool"][0]
        assert nodepool["spec"]["disruption"]["consolidationPolicy"] == "WhenEmpty"
        assert nodepool["spec"]["disruption"]["consolidateAfter"] == "300s"

    @patch("cli.nodepools.get_vcpus_for_instance_type", return_value=96)
    def test_efa_adds_efa_label(self, mock_vcpus):
        """EFA nodepools should have efa=true label."""
        manifest = generate_odcr_nodepool_manifest(
            name="efa-pool",
            region="us-east-1",
            capacity_reservation_id="cr-123",
            instance_types=["p4d.24xlarge"],
            efa=True,
        )
        docs = list(yaml.safe_load_all(manifest))
        nodepool = [d for d in docs if d and d.get("kind") == "NodePool"][0]
        labels = nodepool["spec"]["template"]["metadata"]["labels"]
        assert labels["efa"] == "true"
        assert labels["workload-type"] == "gpu-efa"

    @patch("cli.nodepools.get_vcpus_for_instance_type", return_value=96)
    def test_efa_adds_efa_taint(self, mock_vcpus):
        """EFA nodepools with GPU instances should have both GPU and EFA taints."""
        manifest = generate_odcr_nodepool_manifest(
            name="efa-pool",
            region="us-east-1",
            capacity_reservation_id="cr-123",
            instance_types=["p4d.24xlarge"],
            efa=True,
        )
        docs = list(yaml.safe_load_all(manifest))
        nodepool = [d for d in docs if d and d.get("kind") == "NodePool"][0]
        taints = nodepool["spec"]["template"]["spec"]["taints"]
        taint_keys = [t["key"] for t in taints]
        assert "nvidia.com/gpu" in taint_keys
        assert "vpc.amazonaws.com/efa" in taint_keys

    @patch("cli.nodepools.get_vcpus_for_instance_type", return_value=96)
    def test_non_efa_uses_underutilized_consolidation(self, mock_vcpus):
        """Non-EFA nodepools should use WhenEmptyOrUnderutilized."""
        manifest = generate_odcr_nodepool_manifest(
            name="gpu-pool",
            region="us-east-1",
            capacity_reservation_id="cr-123",
            instance_types=["p4d.24xlarge"],
            efa=False,
        )
        docs = list(yaml.safe_load_all(manifest))
        nodepool = [d for d in docs if d and d.get("kind") == "NodePool"][0]
        assert nodepool["spec"]["disruption"]["consolidationPolicy"] == "WhenEmptyOrUnderutilized"


class TestGenerateOdcrManifestNonGpu:
    """Tests for non-GPU instance types."""

    @patch("cli.nodepools.get_vcpus_for_instance_type", return_value=8)
    def test_non_gpu_instance_no_taints(self, mock_vcpus):
        """Non-GPU instances should not have GPU taints."""
        manifest = generate_odcr_nodepool_manifest(
            name="cpu-pool",
            region="us-east-1",
            capacity_reservation_id="cr-456",
            instance_types=["m5.2xlarge"],
        )
        docs = list(yaml.safe_load_all(manifest))
        nodepool = [d for d in docs if d and d.get("kind") == "NodePool"][0]
        assert "taints" not in nodepool["spec"]["template"]["spec"]

    @patch("cli.nodepools.get_vcpus_for_instance_type", return_value=8)
    def test_non_gpu_instance_has_reserved_label(self, mock_vcpus):
        """Non-GPU instances should still have reserved-capacity label."""
        manifest = generate_odcr_nodepool_manifest(
            name="cpu-pool",
            region="us-east-1",
            capacity_reservation_id="cr-456",
            instance_types=["m5.2xlarge"],
        )
        docs = list(yaml.safe_load_all(manifest))
        nodepool = [d for d in docs if d and d.get("kind") == "NodePool"][0]
        labels = nodepool["spec"]["template"]["metadata"]["labels"]
        assert labels["workload-type"] == "reserved-capacity"


class TestGenerateOdcrManifestEc2NodeClass:
    """Tests for EC2NodeClass in generated manifests."""

    @patch("cli.nodepools.get_vcpus_for_instance_type", return_value=96)
    def test_ec2_nodeclass_has_capacity_reservation(self, mock_vcpus):
        """EC2NodeClass should reference the capacity reservation."""
        manifest = generate_odcr_nodepool_manifest(
            name="test-pool",
            region="us-west-2",
            capacity_reservation_id="cr-abc123",
        )
        docs = list(yaml.safe_load_all(manifest))
        nodeclass = [d for d in docs if d and d.get("kind") == "EC2NodeClass"][0]
        assert nodeclass["spec"]["capacityReservationSelectorTerms"] == [{"id": "cr-abc123"}]

    @patch("cli.nodepools.get_vcpus_for_instance_type", return_value=96)
    def test_ec2_nodeclass_has_correct_discovery_tags(self, mock_vcpus):
        """EC2NodeClass should use region-based discovery tags."""
        manifest = generate_odcr_nodepool_manifest(
            name="test-pool",
            region="eu-west-1",
            capacity_reservation_id="cr-123",
        )
        docs = list(yaml.safe_load_all(manifest))
        nodeclass = [d for d in docs if d and d.get("kind") == "EC2NodeClass"][0]
        subnet_tags = nodeclass["spec"]["subnetSelectorTerms"][0]["tags"]
        assert subnet_tags["karpenter.sh/discovery"] == "gco-eu-west-1"

    @patch("cli.nodepools.get_vcpus_for_instance_type", return_value=96)
    def test_ec2_nodeclass_discovery_tags_project_scoped(self, mock_vcpus):
        """#139: discovery tags, node role, and Name tag derive from project_name.

        A non-``gco`` deployment's NodeClass must select subnets/SGs tagged for
        its own cluster (``<project>-<region>``); the default ``gco`` renders
        byte-identical to the pre-#139 output (see the test above).
        """
        manifest = generate_odcr_nodepool_manifest(
            name="test-pool",
            region="eu-west-1",
            capacity_reservation_id="cr-123",
            project_name="acme",
        )
        docs = list(yaml.safe_load_all(manifest))
        nodeclass = [d for d in docs if d and d.get("kind") == "EC2NodeClass"][0]
        spec = nodeclass["spec"]
        assert spec["subnetSelectorTerms"][0]["tags"]["karpenter.sh/discovery"] == "acme-eu-west-1"
        assert (
            spec["securityGroupSelectorTerms"][0]["tags"]["karpenter.sh/discovery"]
            == "acme-eu-west-1"
        )
        assert spec["role"] == "KarpenterNodeRole-acme"
        assert spec["tags"]["Name"] == "acme-test-pool"


class TestGenerateOdcrManifestComments:
    """Tests for manifest comment headers."""

    @patch("cli.nodepools.get_vcpus_for_instance_type", return_value=96)
    def test_manifest_has_comment_header(self, mock_vcpus):
        """Generated manifest should have descriptive comments."""
        manifest = generate_odcr_nodepool_manifest(
            name="test",
            region="us-east-1",
            capacity_reservation_id="cr-123",
        )
        assert "# ODCR-backed NodePool" in manifest
        assert "cr-123" in manifest
        assert "us-east-1" in manifest

    @patch("cli.nodepools.get_vcpus_for_instance_type", return_value=96)
    def test_fallback_comment_present(self, mock_vcpus):
        """Fallback comment should appear when fallback_on_demand is True."""
        manifest = generate_odcr_nodepool_manifest(
            name="test",
            region="us-east-1",
            capacity_reservation_id="cr-123",
            fallback_on_demand=True,
        )
        assert "Fallback: on-demand" in manifest

    @patch("cli.nodepools.get_vcpus_for_instance_type", return_value=96)
    def test_no_fallback_comment_when_disabled(self, mock_vcpus):
        """Fallback comment should not appear when fallback_on_demand is False."""
        manifest = generate_odcr_nodepool_manifest(
            name="test",
            region="us-east-1",
            capacity_reservation_id="cr-123",
            fallback_on_demand=False,
        )
        assert "Fallback" not in manifest


class TestNodePoolInfoDataclass:
    """Tests for NodePoolInfo dataclass."""

    def test_creation_with_all_fields(self):
        info = NodePoolInfo(
            name="gpu-pool",
            capacity_type="on-demand",
            instance_types=["p4d.24xlarge"],
            max_nodes=10,
            status="Ready",
            node_count=3,
            capacity_reservation_id="cr-123",
        )
        assert info.name == "gpu-pool"
        assert info.capacity_type == "on-demand"
        assert info.instance_types == ["p4d.24xlarge"]
        assert info.max_nodes == 10
        assert info.node_count == 3
        assert info.capacity_reservation_id == "cr-123"

    def test_creation_without_optional_fields(self):
        info = NodePoolInfo(
            name="basic",
            capacity_type="spot",
            instance_types=["m5.xlarge"],
            max_nodes=None,
            status="Ready",
            node_count=0,
        )
        assert info.name == "basic"
        assert info.capacity_reservation_id is None
        assert info.max_nodes is None


class TestCalculateCpuLimitEdgeCases:
    """Tests for calculate_cpu_limit edge cases."""

    @patch("cli.nodepools.get_vcpus_for_instance_type", return_value=96)
    def test_single_large_instance(self, mock_vcpus):
        result = calculate_cpu_limit(["p4d.24xlarge"], max_nodes=10)
        assert result == 960

    @patch("cli.nodepools.get_vcpus_for_instance_type")
    def test_uses_max_vcpus_across_types(self, mock_vcpus):
        """Should use the maximum vCPU count when multiple types given."""
        mock_vcpus.side_effect = [8, 96, 32]
        result = calculate_cpu_limit(["m5.2xlarge", "p4d.24xlarge", "g5.8xlarge"], max_nodes=5)
        assert result == 480  # 5 * 96

    def test_none_instance_types_uses_default(self):
        """None instance_types should use default vCPU count."""
        result = calculate_cpu_limit(None, max_nodes=10)
        # DEFAULT_VCPUS_PER_NODE = 96
        assert result == 960

    def test_max_nodes_one(self):
        """Single node should return vCPUs for one instance."""
        result = calculate_cpu_limit(None, max_nodes=1)
        assert result == 96
