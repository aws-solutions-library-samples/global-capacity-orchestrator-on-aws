"""
Extended tests for cli/nodepools.py.

Covers EFA-specific NodePool manifest generation: WhenEmpty
consolidation (vs WhenEmptyOrUnderutilized for non-EFA pools), the
efa=true label and workload-type=gpu-efa marker, dual taints for
nvidia.com/gpu plus vpc.amazonaws.com/efa. Also exercises non-GPU
instance types (no GPU taints emitted), the Auto Mode NodeClass and its
cluster lookup, NodePoolInfo construction, and calculate_cpu_limit edge
cases. EC2 is the record table in tests/_ec2_instance_types.py behind a
fake boto3 client.
"""

from unittest.mock import patch

import pytest
import yaml

from cli.nodepools import (
    FACTS_SOURCE_EC2,
    AutoModeNodeClassInputs,
    InstanceTypeFacts,
    NodePoolInfo,
    calculate_cpu_limit,
    generate_odcr_nodepool_manifest,
    resolve_auto_mode_node_class_inputs,
)
from tests._ec2_instance_types import fake_ec2_client

#: What resolve_auto_mode_node_class_inputs returns for a GCO cluster.
NODE_CLASS = AutoModeNodeClassInputs(
    role="gco-us-east-1-GCOEksClusternodePoolRole-ABC123",
    subnet_ids=("subnet-0aaa", "subnet-0bbb"),
    security_group_ids=("sg-0cluster",),
)


@pytest.fixture(autouse=True)
def _ec2_answers_from_the_table():
    """Every ``boto3.client`` in the module is the fake EC2 unless a test patches its own."""
    with patch("cli.nodepools.boto3.client", side_effect=fake_ec2_client):
        yield


class TestGenerateOdcrManifestEfa:
    """Tests for generate_odcr_nodepool_manifest with EFA."""

    def test_efa_sets_when_empty_consolidation(self):
        """EFA nodepools should use WhenEmpty consolidation for long-running jobs."""
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="efa-pool",
            region="us-east-1",
            capacity_reservation_id="cr-123",
            instance_types=["p4d.24xlarge"],
            efa=True,
        )
        docs = list(yaml.safe_load_all(manifest))
        nodepool = next(d for d in docs if d and d.get("kind") == "NodePool")
        assert nodepool["spec"]["disruption"]["consolidationPolicy"] == "WhenEmpty"
        assert nodepool["spec"]["disruption"]["consolidateAfter"] == "300s"

    def test_efa_adds_efa_label(self):
        """EFA nodepools should have efa=true label."""
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="efa-pool",
            region="us-east-1",
            capacity_reservation_id="cr-123",
            instance_types=["p4d.24xlarge"],
            efa=True,
        )
        docs = list(yaml.safe_load_all(manifest))
        nodepool = next(d for d in docs if d and d.get("kind") == "NodePool")
        labels = nodepool["spec"]["template"]["metadata"]["labels"]
        assert labels["efa"] == "true"
        assert labels["workload-type"] == "gpu-efa"

    def test_efa_adds_efa_taint(self):
        """EFA nodepools with GPU instances should have both GPU and EFA taints."""
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="efa-pool",
            region="us-east-1",
            capacity_reservation_id="cr-123",
            instance_types=["p4d.24xlarge"],
            efa=True,
        )
        docs = list(yaml.safe_load_all(manifest))
        nodepool = next(d for d in docs if d and d.get("kind") == "NodePool")
        taints = nodepool["spec"]["template"]["spec"]["taints"]
        taint_keys = [t["key"] for t in taints]
        assert "nvidia.com/gpu" in taint_keys
        assert "vpc.amazonaws.com/efa" in taint_keys

    def test_efa_on_a_cpu_type_labels_without_tainting(self):
        """EFA alone adds no taint; the EFA taint only accompanies an
        accelerator (GPU or Neuron) taint."""
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="efa-cpu",
            region="us-east-1",
            capacity_reservation_id="cr-123",
            instance_types=["c5n.18xlarge"],
            efa=True,
        )
        docs = list(yaml.safe_load_all(manifest))
        nodepool = next(d for d in docs if d and d.get("kind") == "NodePool")
        assert nodepool["spec"]["template"]["metadata"]["labels"]["efa"] == "true"
        assert "taints" not in nodepool["spec"]["template"]["spec"]

    def test_non_efa_uses_underutilized_consolidation(self):
        """Non-EFA nodepools should use WhenEmptyOrUnderutilized."""
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="gpu-pool",
            region="us-east-1",
            capacity_reservation_id="cr-123",
            instance_types=["p4d.24xlarge"],
            efa=False,
        )
        docs = list(yaml.safe_load_all(manifest))
        nodepool = next(d for d in docs if d and d.get("kind") == "NodePool")
        assert nodepool["spec"]["disruption"]["consolidationPolicy"] == "WhenEmptyOrUnderutilized"


class TestGenerateOdcrManifestNonGpu:
    """Tests for non-GPU instance types."""

    def test_non_gpu_instance_no_taints(self):
        """Non-GPU instances should not have GPU taints."""
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="cpu-pool",
            region="us-east-1",
            capacity_reservation_id="cr-456",
            instance_types=["m5.2xlarge"],
        )
        docs = list(yaml.safe_load_all(manifest))
        nodepool = next(d for d in docs if d and d.get("kind") == "NodePool")
        assert "taints" not in nodepool["spec"]["template"]["spec"]

    def test_non_gpu_instance_has_reserved_label(self):
        """Non-GPU instances should still have reserved-capacity label."""
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="cpu-pool",
            region="us-east-1",
            capacity_reservation_id="cr-456",
            instance_types=["m5.2xlarge"],
        )
        docs = list(yaml.safe_load_all(manifest))
        nodepool = next(d for d in docs if d and d.get("kind") == "NodePool")
        labels = nodepool["spec"]["template"]["metadata"]["labels"]
        assert labels["workload-type"] == "reserved-capacity"


class TestGenerateOdcrManifestNodeClass:
    """The Auto Mode NodeClass in generated manifests."""

    def test_node_class_binds_the_capacity_reservation(self):
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="test-pool",
            region="us-west-2",
            capacity_reservation_id="cr-abc123",
        )
        docs = list(yaml.safe_load_all(manifest))
        nodeclass = next(d for d in docs if d and d.get("kind") == "NodeClass")
        assert nodeclass["spec"]["capacityReservationSelectorTerms"] == [{"id": "cr-abc123"}]

    def test_node_class_reuses_the_clusters_role_subnets_and_security_group(self):
        """The built-in NodeClass's role already has the EC2 access entry nodes join with.

        Selecting by ID (not by a ``karpenter.sh/discovery`` tag nothing sets)
        keeps the nodes in the cluster's own private subnets and security group.
        """
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="test-pool",
            region="eu-west-1",
            capacity_reservation_id="cr-123",
        )
        nodeclass = next(
            d for d in yaml.safe_load_all(manifest) if d and d.get("kind") == "NodeClass"
        )
        spec = nodeclass["spec"]
        assert spec["role"] == NODE_CLASS.role
        assert spec["subnetSelectorTerms"] == [{"id": "subnet-0aaa"}, {"id": "subnet-0bbb"}]
        assert spec["securityGroupSelectorTerms"] == [{"id": "sg-0cluster"}]
        assert "karpenter.sh/discovery" not in manifest
        assert "KarpenterNodeRole" not in manifest

    @pytest.mark.parametrize(
        ("instance_type", "taint"),
        [
            ("g7e.2xlarge", "nvidia.com/gpu"),
            ("gr6.4xlarge", "nvidia.com/gpu"),
            ("trn2.3xlarge", "aws.amazon.com/neuron"),
            ("inf2.xlarge", "aws.amazon.com/neuron"),
        ],
    )
    def test_accelerator_reservations_carry_the_shipped_pools_taint(self, instance_type, taint):
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="acc-pool",
            region="us-east-1",
            capacity_reservation_id="cr-acc",
            instance_types=[instance_type],
        )
        nodepool = list(yaml.safe_load_all(manifest))[1]
        taints = nodepool["spec"]["template"]["spec"]["taints"]
        assert [t["key"] for t in taints] == [taint]

    def test_instance_types_pin_the_architecture_so_arm_reservations_work(self):
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="arm-pool",
            region="us-east-1",
            capacity_reservation_id="cr-arm",
            instance_types=["g5g.xlarge"],
        )
        requirements = list(yaml.safe_load_all(manifest))[1]["spec"]["template"]["spec"][
            "requirements"
        ]
        assert "kubernetes.io/arch" not in {r["key"] for r in requirements}

    def test_without_instance_types_the_pool_stays_on_amd64(self):
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="any-pool",
            region="us-east-1",
            capacity_reservation_id="cr-any",
        )
        requirements = list(yaml.safe_load_all(manifest))[1]["spec"]["template"]["spec"][
            "requirements"
        ]
        assert {"key": "kubernetes.io/arch", "operator": "In", "values": ["amd64"]} in requirements


class TestResolveAutoModeNodeClassInputs:
    """resolve_auto_mode_node_class_inputs reads what the built-in NodeClass uses."""

    @staticmethod
    def _cluster(**overrides):
        cluster = {
            "computeConfig": {
                "enabled": True,
                "nodeRoleArn": "arn:aws:iam::111122223333:role/gco-us-east-1-nodePoolRole-X1",
            },
            "resourcesVpcConfig": {
                "subnetIds": ["subnet-0aaa", "subnet-0bbb"],
                "clusterSecurityGroupId": "sg-0cluster",
            },
        }
        cluster.update(overrides)
        return {"cluster": cluster}

    @patch("cli.nodepools.boto3.client")
    def test_auto_mode_cluster(self, mock_client):
        mock_client.return_value.describe_cluster.return_value = self._cluster()
        inputs = resolve_auto_mode_node_class_inputs("gco-us-east-1", "us-east-1")
        mock_client.assert_called_once_with("eks", region_name="us-east-1")
        mock_client.return_value.describe_cluster.assert_called_once_with(name="gco-us-east-1")
        assert inputs == AutoModeNodeClassInputs(
            role="gco-us-east-1-nodePoolRole-X1",
            subnet_ids=("subnet-0aaa", "subnet-0bbb"),
            security_group_ids=("sg-0cluster",),
        )

    @patch("cli.nodepools.boto3.client")
    def test_role_name_is_the_last_arn_segment_even_with_a_path(self, mock_client):
        mock_client.return_value.describe_cluster.return_value = self._cluster(
            computeConfig={
                "enabled": True,
                "nodeRoleArn": "arn:aws:iam::111122223333:role/eks/nodes/NodeRole",
            }
        )
        assert resolve_auto_mode_node_class_inputs("c", "us-east-1").role == "NodeRole"

    @pytest.mark.parametrize(
        "compute",
        [None, {"enabled": False, "nodeRoleArn": "arn:aws:iam::1:role/r"}, {"enabled": True}],
        ids=["no-compute-config", "auto-mode-off", "no-node-role"],
    )
    @patch("cli.nodepools.boto3.client")
    def test_a_cluster_without_auto_mode_is_refused(self, mock_client, compute):
        mock_client.return_value.describe_cluster.return_value = self._cluster(
            computeConfig=compute
        )
        with pytest.raises(ValueError, match="does not run EKS Auto Mode"):
            resolve_auto_mode_node_class_inputs("c", "us-east-1")

    @pytest.mark.parametrize(
        "vpc_config",
        [
            {"subnetIds": [], "clusterSecurityGroupId": "sg-1"},
            {"subnetIds": ["subnet-1"]},
            None,
        ],
        ids=["no-subnets", "no-security-group", "no-vpc-config"],
    )
    @patch("cli.nodepools.boto3.client")
    def test_missing_subnets_or_security_group_is_refused(self, mock_client, vpc_config):
        mock_client.return_value.describe_cluster.return_value = self._cluster(
            resourcesVpcConfig=vpc_config
        )
        with pytest.raises(ValueError, match="no subnets or cluster security group"):
            resolve_auto_mode_node_class_inputs("c", "us-east-1")


class TestGenerateOdcrManifestComments:
    """Tests for manifest comment headers."""

    def test_manifest_has_comment_header(self):
        """Generated manifest should have descriptive comments."""
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="test",
            region="us-east-1",
            capacity_reservation_id="cr-123",
        )
        assert "# ODCR-backed NodePool" in manifest
        assert "cr-123" in manifest
        assert "us-east-1" in manifest

    def test_fallback_comment_present(self):
        """Fallback comment should appear when fallback_on_demand is True."""
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="test",
            region="us-east-1",
            capacity_reservation_id="cr-123",
            fallback_on_demand=True,
        )
        assert "Fallback: on-demand" in manifest

    def test_no_fallback_comment_when_disabled(self):
        """Fallback comment should not appear when fallback_on_demand is False."""
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
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


def _facts(instance_type: str, vcpus: int) -> InstanceTypeFacts:
    return InstanceTypeFacts(instance_type, vcpus, None, FACTS_SOURCE_EC2)


class TestCalculateCpuLimitEdgeCases:
    """Tests for calculate_cpu_limit edge cases."""

    def test_single_large_instance(self):
        assert calculate_cpu_limit([_facts("p4d.24xlarge", 96)], max_nodes=10) == 960

    def test_uses_max_vcpus_across_types(self):
        """Should use the maximum vCPU count when multiple types given."""
        facts = [_facts("m5.2xlarge", 8), _facts("p4d.24xlarge", 96), _facts("g5.8xlarge", 32)]
        assert calculate_cpu_limit(facts, max_nodes=5) == 480  # 5 * 96

    def test_no_instance_types_uses_default(self):
        """Without instance types the default vCPU count stands in for the largest type."""
        assert calculate_cpu_limit((), max_nodes=10) == 960  # DEFAULT_VCPUS_PER_NODE = 96

    def test_max_nodes_one(self):
        """Single node should return vCPUs for one instance."""
        assert calculate_cpu_limit((), max_nodes=1) == 96
