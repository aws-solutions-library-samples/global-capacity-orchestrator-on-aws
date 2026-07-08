"""
Tests for cli/nodepools.py — Karpenter NodePool management utilities.

Covers the NodePoolInfo dataclass, ODCR NodePool manifest generation
(instance types, capacity reservation wiring, vCPU lookup via EC2 API
with fallback to DEFAULT_VCPUS_PER_NODE on error), CPU limit calculation,
EKS token generation for kubectl auth, Kubernetes client configuration,
and list/describe operations. Uses boto3 mocks plus hand-rolled YAML
assertions to verify the rendered manifests match spec.
"""

import base64
from unittest.mock import MagicMock, patch

import pytest
import yaml

from cli.nodepools import (
    DEFAULT_VCPUS_PER_NODE,
    NodePoolInfo,
    calculate_cpu_limit,
    describe_cluster_nodepool,
    generate_capacity_block_nodepool_manifest,
    generate_odcr_nodepool_manifest,
    get_eks_token,
    get_k8s_client,
    get_vcpus_for_instance_type,
    list_cluster_nodepools,
)


class TestNodePoolInfo:
    """Tests for NodePoolInfo dataclass."""

    def test_nodepool_info_creation(self):
        """Test creating NodePoolInfo with all fields."""
        info = NodePoolInfo(
            name="test-pool",
            capacity_type="reserved",
            instance_types=["p4d.24xlarge"],
            max_nodes=10,
            status="Ready",
            node_count=5,
            capacity_reservation_id="cr-12345",
        )

        assert info.name == "test-pool"
        assert info.capacity_type == "reserved"
        assert info.instance_types == ["p4d.24xlarge"]
        assert info.max_nodes == 10
        assert info.status == "Ready"
        assert info.node_count == 5
        assert info.capacity_reservation_id == "cr-12345"

    def test_nodepool_info_without_optional_fields(self):
        """Test creating NodePoolInfo without optional fields."""
        info = NodePoolInfo(
            name="test-pool",
            capacity_type="on-demand",
            instance_types=["g4dn.xlarge"],
            max_nodes=None,
            status="NotReady",
            node_count=0,
        )

        assert info.name == "test-pool"
        assert info.capacity_reservation_id is None


class TestInstanceVcpuLookup:
    """Tests for instance vCPU lookup and calculation functions."""

    @patch("cli.nodepools.boto3.client")
    def test_get_vcpus_for_instance_type_success(self, mock_boto_client):
        """Test getting vCPUs for an instance type via API."""
        mock_ec2 = MagicMock()
        mock_ec2.describe_instance_types.return_value = {
            "InstanceTypes": [{"VCpuInfo": {"DefaultVCpus": 96}}]
        }
        mock_boto_client.return_value = mock_ec2

        result = get_vcpus_for_instance_type("p4d.24xlarge", "us-east-1")

        assert result == 96
        mock_ec2.describe_instance_types.assert_called_once_with(InstanceTypes=["p4d.24xlarge"])

    @patch("cli.nodepools.boto3.client")
    def test_get_vcpus_for_instance_type_api_failure(self, mock_boto_client):
        """Test getting vCPUs when API fails returns default."""
        mock_ec2 = MagicMock()
        mock_ec2.describe_instance_types.side_effect = Exception("API error")
        mock_boto_client.return_value = mock_ec2

        result = get_vcpus_for_instance_type("unknown.xlarge", "us-east-1")

        assert result == DEFAULT_VCPUS_PER_NODE

    @patch("cli.nodepools.boto3.client")
    def test_get_vcpus_for_instance_type_empty_response(self, mock_boto_client):
        """Test getting vCPUs when API returns empty response."""
        mock_ec2 = MagicMock()
        mock_ec2.describe_instance_types.return_value = {"InstanceTypes": []}
        mock_boto_client.return_value = mock_ec2

        result = get_vcpus_for_instance_type("invalid.type", "us-east-1")

        assert result == DEFAULT_VCPUS_PER_NODE

    @patch("cli.nodepools.get_vcpus_for_instance_type")
    def test_calculate_cpu_limit_no_instance_types(self, mock_get_vcpus):
        """Test CPU limit calculation with no instance types specified."""
        result = calculate_cpu_limit(None, 10)
        assert result == 10 * DEFAULT_VCPUS_PER_NODE
        mock_get_vcpus.assert_not_called()

    @patch("cli.nodepools.get_vcpus_for_instance_type")
    def test_calculate_cpu_limit_single_instance_type(self, mock_get_vcpus):
        """Test CPU limit calculation with single instance type."""
        mock_get_vcpus.return_value = 4
        result = calculate_cpu_limit(["g4dn.xlarge"], 10, "us-east-1")
        assert result == 10 * 4
        mock_get_vcpus.assert_called_once_with("g4dn.xlarge", "us-east-1")

    @patch("cli.nodepools.get_vcpus_for_instance_type")
    def test_calculate_cpu_limit_multiple_instance_types(self, mock_get_vcpus):
        """Test CPU limit calculation uses max vCPUs from multiple types."""
        mock_get_vcpus.side_effect = [4, 192]  # g4dn.xlarge, p5.48xlarge
        result = calculate_cpu_limit(["g4dn.xlarge", "p5.48xlarge"], 10, "us-east-1")
        # Should use max (192)
        assert result == 10 * 192

    @patch("cli.nodepools.get_vcpus_for_instance_type")
    def test_calculate_cpu_limit_empty_list(self, mock_get_vcpus):
        """Test CPU limit calculation with empty instance types list."""
        result = calculate_cpu_limit([], 10)
        assert result == 10 * DEFAULT_VCPUS_PER_NODE
        mock_get_vcpus.assert_not_called()


class TestGenerateOdcrNodepoolManifest:
    """Tests for generate_odcr_nodepool_manifest function."""

    def test_basic_manifest_generation(self):
        """Test generating a basic ODCR NodePool manifest."""
        manifest = generate_odcr_nodepool_manifest(
            name="test-odcr",
            region="us-east-1",
            capacity_reservation_id="cr-12345678901234567",
        )

        assert "# ODCR-backed NodePool for GCO" in manifest
        assert "cr-12345678901234567" in manifest
        assert "us-east-1" in manifest
        assert "test-odcr" in manifest

        # Parse YAML documents
        docs = list(yaml.safe_load_all(manifest))
        assert len(docs) == 2

        # Check EC2NodeClass
        ec2_node_class = docs[0]
        assert ec2_node_class["kind"] == "EC2NodeClass"
        assert ec2_node_class["metadata"]["name"] == "test-odcr-nodeclass"
        assert (
            ec2_node_class["spec"]["capacityReservationSelectorTerms"][0]["id"]
            == "cr-12345678901234567"
        )

        # Check NodePool
        nodepool = docs[1]
        assert nodepool["kind"] == "NodePool"
        assert nodepool["metadata"]["name"] == "test-odcr"

    def test_manifest_with_instance_types(self):
        """Test generating manifest with specific instance types."""
        manifest = generate_odcr_nodepool_manifest(
            name="gpu-pool",
            region="us-west-2",
            capacity_reservation_id="cr-abcdef",
            instance_types=["p4d.24xlarge", "p5.48xlarge"],
        )

        docs = list(yaml.safe_load_all(manifest))
        nodepool = docs[1]

        # Find instance type requirement
        requirements = nodepool["spec"]["template"]["spec"]["requirements"]
        instance_type_req = next(
            (r for r in requirements if r["key"] == "node.kubernetes.io/instance-type"),
            None,
        )

        assert instance_type_req is not None
        assert "p4d.24xlarge" in instance_type_req["values"]
        assert "p5.48xlarge" in instance_type_req["values"]

    def test_manifest_with_gpu_taints(self):
        """Test that GPU instance types get GPU taints."""
        manifest = generate_odcr_nodepool_manifest(
            name="gpu-pool",
            region="us-east-1",
            capacity_reservation_id="cr-gpu",
            instance_types=["g4dn.xlarge"],
        )

        docs = list(yaml.safe_load_all(manifest))
        nodepool = docs[1]

        taints = nodepool["spec"]["template"]["spec"].get("taints", [])
        assert len(taints) == 1
        assert taints[0]["key"] == "nvidia.com/gpu"
        assert taints[0]["effect"] == "NoSchedule"

    def test_manifest_without_gpu_taints(self):
        """Test that non-GPU instance types don't get GPU taints."""
        manifest = generate_odcr_nodepool_manifest(
            name="cpu-pool",
            region="us-east-1",
            capacity_reservation_id="cr-cpu",
            instance_types=["c5.xlarge"],
        )

        docs = list(yaml.safe_load_all(manifest))
        nodepool = docs[1]

        taints = nodepool["spec"]["template"]["spec"].get("taints", [])
        assert len(taints) == 0

    def test_manifest_with_fallback_on_demand(self):
        """Test generating manifest with on-demand fallback."""
        manifest = generate_odcr_nodepool_manifest(
            name="fallback-pool",
            region="us-east-1",
            capacity_reservation_id="cr-fallback",
            fallback_on_demand=True,
        )

        assert "Fallback: on-demand" in manifest

        docs = list(yaml.safe_load_all(manifest))
        nodepool = docs[1]

        requirements = nodepool["spec"]["template"]["spec"]["requirements"]
        capacity_type_req = next(
            (r for r in requirements if r["key"] == "karpenter.sh/capacity-type"),
            None,
        )

        assert capacity_type_req is not None
        assert "reserved" in capacity_type_req["values"]
        assert "on-demand" in capacity_type_req["values"]

    def test_manifest_without_fallback(self):
        """Test generating manifest without on-demand fallback."""
        manifest = generate_odcr_nodepool_manifest(
            name="reserved-only",
            region="us-east-1",
            capacity_reservation_id="cr-reserved",
            fallback_on_demand=False,
        )

        docs = list(yaml.safe_load_all(manifest))
        nodepool = docs[1]

        requirements = nodepool["spec"]["template"]["spec"]["requirements"]
        capacity_type_req = next(
            (r for r in requirements if r["key"] == "karpenter.sh/capacity-type"),
            None,
        )

        assert capacity_type_req is not None
        assert capacity_type_req["values"] == ["reserved"]

    def test_manifest_with_custom_max_nodes(self):
        """Test generating manifest with custom max nodes."""
        manifest = generate_odcr_nodepool_manifest(
            name="limited-pool",
            region="us-east-1",
            capacity_reservation_id="cr-limited",
            max_nodes=50,
        )

        docs = list(yaml.safe_load_all(manifest))
        nodepool = docs[1]

        # Without instance types, uses default 96 vCPUs per node
        assert nodepool["spec"]["limits"]["cpu"] == str(50 * 96)

    @patch("cli.nodepools.get_vcpus_for_instance_type")
    def test_manifest_cpu_limit_with_instance_types(self, mock_get_vcpus):
        """Test that CPU limit is calculated based on instance type vCPUs."""
        # Test with p4d.24xlarge (96 vCPUs)
        mock_get_vcpus.return_value = 96
        manifest = generate_odcr_nodepool_manifest(
            name="p4d-pool",
            region="us-east-1",
            capacity_reservation_id="cr-p4d",
            instance_types=["p4d.24xlarge"],
            max_nodes=10,
        )

        docs = list(yaml.safe_load_all(manifest))
        nodepool = docs[1]
        assert nodepool["spec"]["limits"]["cpu"] == str(10 * 96)

    @patch("cli.nodepools.get_vcpus_for_instance_type")
    def test_manifest_cpu_limit_with_small_instance(self, mock_get_vcpus):
        """Test CPU limit with smaller instance type."""
        mock_get_vcpus.return_value = 4
        manifest = generate_odcr_nodepool_manifest(
            name="g4dn-pool",
            region="us-east-1",
            capacity_reservation_id="cr-g4dn",
            instance_types=["g4dn.xlarge"],
            max_nodes=10,
        )

        docs = list(yaml.safe_load_all(manifest))
        nodepool = docs[1]
        # g4dn.xlarge has 4 vCPUs
        assert nodepool["spec"]["limits"]["cpu"] == str(10 * 4)

    @patch("cli.nodepools.get_vcpus_for_instance_type")
    def test_manifest_cpu_limit_with_multiple_instance_types(self, mock_get_vcpus):
        """Test that CPU limit uses max vCPUs when multiple instance types specified."""
        mock_get_vcpus.side_effect = [4, 192]  # g4dn.xlarge, p5.48xlarge
        manifest = generate_odcr_nodepool_manifest(
            name="mixed-pool",
            region="us-east-1",
            capacity_reservation_id="cr-mixed",
            instance_types=["g4dn.xlarge", "p5.48xlarge"],
            max_nodes=10,
        )

        docs = list(yaml.safe_load_all(manifest))
        nodepool = docs[1]
        # Should use max (192 vCPUs from p5.48xlarge)
        assert nodepool["spec"]["limits"]["cpu"] == str(10 * 192)

    def test_manifest_labels_and_tags(self):
        """Test that manifest includes proper labels and tags."""
        manifest = generate_odcr_nodepool_manifest(
            name="labeled-pool",
            region="us-east-1",
            capacity_reservation_id="cr-labeled",
        )

        docs = list(yaml.safe_load_all(manifest))
        ec2_node_class = docs[0]
        nodepool = docs[1]

        # Check EC2NodeClass labels
        assert ec2_node_class["metadata"]["labels"]["app.kubernetes.io/part-of"] == "gco"
        assert ec2_node_class["metadata"]["labels"]["gco.io/nodepool"] == "labeled-pool"

        # Check EC2NodeClass tags
        assert ec2_node_class["spec"]["tags"]["gco.io/nodepool"] == "labeled-pool"

        # Check NodePool labels
        assert nodepool["metadata"]["labels"]["app.kubernetes.io/part-of"] == "gco"


class TestGetEksToken:
    """Tests for get_eks_token function."""

    @patch("cli.nodepools.boto3.Session")
    @patch("botocore.signers.RequestSigner")
    def test_get_eks_token(self, mock_signer_class, mock_session_class):
        """Test generating EKS authentication token."""
        mock_session = MagicMock()
        mock_session_class.return_value = mock_session

        mock_sts = MagicMock()
        mock_sts.meta.service_model.service_id = "STS"
        mock_session.client.return_value = mock_sts

        mock_credentials = MagicMock()
        mock_session.get_credentials.return_value = mock_credentials
        mock_session.events = MagicMock()

        mock_signer = MagicMock()
        mock_signer.generate_presigned_url.return_value = (
            "https://sts.us-east-1.amazonaws.com/?signed=true"
        )
        mock_signer_class.return_value = mock_signer

        token = get_eks_token("test-cluster", "us-east-1")

        assert token.startswith("k8s-aws-v1.")
        mock_signer.generate_presigned_url.assert_called_once()


class TestGetK8sClient:
    """Tests for get_k8s_client function."""

    @patch("cli.nodepools.boto3.client")
    @patch("cli.nodepools.get_eks_token")
    @patch("kubernetes.client.Configuration")
    @patch("kubernetes.client.ApiClient")
    @patch("kubernetes.client.CustomObjectsApi")
    def test_get_k8s_client(
        self, mock_custom_api, mock_api_client, mock_config_class, mock_get_token, mock_boto_client
    ):
        """Test getting configured Kubernetes client."""
        mock_eks = MagicMock()
        mock_eks.describe_cluster.return_value = {
            "cluster": {
                "endpoint": "https://test-cluster.eks.amazonaws.com",
                "certificateAuthority": {
                    "data": base64.b64encode(b"test-ca-cert").decode(),
                },
            }
        }
        mock_boto_client.return_value = mock_eks
        mock_get_token.return_value = "k8s-aws-v1.test-token"

        # Create a mock configuration with the attributes the kubernetes client expects
        mock_config = MagicMock()
        # These attributes are accessed by kubernetes.client.ApiClient
        mock_config.assert_hostname = None
        mock_config.ssl_ca_cert = None
        mock_config.cert_file = None
        mock_config.key_file = None
        mock_config.verify_ssl = True
        mock_config.proxy = None
        mock_config.proxy_headers = None
        mock_config_class.return_value = mock_config

        result = get_k8s_client("test-cluster", "us-east-1")

        mock_eks.describe_cluster.assert_called_once_with(name="test-cluster")
        mock_get_token.assert_called_once_with("test-cluster", "us-east-1")
        assert result is not None


class TestListClusterNodepools:
    """Tests for list_cluster_nodepools function."""

    @patch("cli.nodepools.get_k8s_client")
    def test_list_nodepools_success(self, mock_get_client):
        """Test listing NodePools successfully."""
        mock_api = MagicMock()
        mock_get_client.return_value = mock_api

        mock_api.list_cluster_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "default-pool"},
                    "spec": {
                        "template": {
                            "spec": {
                                "requirements": [
                                    {"key": "karpenter.sh/capacity-type", "values": ["on-demand"]},
                                    {
                                        "key": "node.kubernetes.io/instance-type",
                                        "values": ["g4dn.xlarge", "g5.xlarge"],
                                    },
                                ]
                            }
                        },
                        "limits": {"cpu": "1000"},
                    },
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                },
                {
                    "metadata": {"name": "reserved-pool"},
                    "spec": {
                        "template": {
                            "spec": {
                                "requirements": [
                                    {"key": "karpenter.sh/capacity-type", "values": ["reserved"]},
                                ]
                            }
                        },
                        "limits": {},
                    },
                    "status": {"conditions": [{"type": "Ready", "status": "False"}]},
                },
            ]
        }

        result = list_cluster_nodepools("test-cluster", "us-east-1")

        assert len(result) == 2
        assert result[0]["name"] == "default-pool"
        assert result[0]["capacity_types"] == "on-demand"
        assert result[0]["status"] == "Ready"
        assert result[1]["name"] == "reserved-pool"
        assert result[1]["status"] == "NotReady"

    @patch("cli.nodepools.get_k8s_client")
    def test_list_nodepools_empty(self, mock_get_client):
        """Test listing NodePools when none exist."""
        mock_api = MagicMock()
        mock_get_client.return_value = mock_api
        mock_api.list_cluster_custom_object.return_value = {"items": []}

        result = list_cluster_nodepools("test-cluster", "us-east-1")

        assert len(result) == 0

    @patch("cli.nodepools.get_k8s_client")
    def test_list_nodepools_error(self, mock_get_client):
        """Test listing NodePools with error."""
        mock_api = MagicMock()
        mock_get_client.return_value = mock_api
        mock_api.list_cluster_custom_object.side_effect = Exception("API error")

        with pytest.raises(RuntimeError, match="Failed to list NodePools"):
            list_cluster_nodepools("test-cluster", "us-east-1")


class TestDescribeClusterNodepool:
    """Tests for describe_cluster_nodepool function."""

    @patch("cli.nodepools.get_k8s_client")
    def test_describe_nodepool_success(self, mock_get_client):
        """Test describing a NodePool successfully."""
        mock_api = MagicMock()
        mock_get_client.return_value = mock_api

        expected_nodepool = {
            "metadata": {"name": "test-pool"},
            "spec": {
                "template": {
                    "spec": {
                        "requirements": [
                            {"key": "karpenter.sh/capacity-type", "values": ["reserved"]},
                        ]
                    }
                }
            },
        }
        mock_api.get_cluster_custom_object.return_value = expected_nodepool

        result = describe_cluster_nodepool("test-cluster", "us-east-1", "test-pool")

        assert result == expected_nodepool
        mock_api.get_cluster_custom_object.assert_called_once_with(
            group="karpenter.sh",
            version="v1",
            plural="nodepools",
            name="test-pool",
        )

    @patch("cli.nodepools.get_k8s_client")
    def test_describe_nodepool_not_found(self, mock_get_client):
        """Test describing a NodePool that doesn't exist."""
        mock_api = MagicMock()
        mock_get_client.return_value = mock_api
        mock_api.get_cluster_custom_object.side_effect = Exception("404 Not Found")

        result = describe_cluster_nodepool("test-cluster", "us-east-1", "nonexistent")

        assert result is None

    @patch("cli.nodepools.get_k8s_client")
    def test_describe_nodepool_error(self, mock_get_client):
        """Test describing a NodePool with error."""
        mock_api = MagicMock()
        mock_get_client.return_value = mock_api
        mock_api.get_cluster_custom_object.side_effect = Exception("API error")

        with pytest.raises(RuntimeError, match="Failed to describe NodePool"):
            describe_cluster_nodepool("test-cluster", "us-east-1", "test-pool")


class TestGenerateCapacityBlockNodepoolManifest:
    """Tests for generate_capacity_block_nodepool_manifest function."""

    def test_basic_manifest_generation(self):
        """Test generating a basic Capacity Block NodePool manifest."""
        manifest = generate_capacity_block_nodepool_manifest(
            name="cb-pool",
            region="us-east-1",
            capacity_reservation_id="cr-cb12345678901234",
        )

        assert "# Capacity Block-backed NodePool for GCO" in manifest
        assert "cr-cb12345678901234" in manifest
        assert "us-east-1" in manifest

        docs = list(yaml.safe_load_all(manifest))
        assert len(docs) == 2

        ec2_node_class = docs[0]
        assert ec2_node_class["kind"] == "EC2NodeClass"
        assert ec2_node_class["metadata"]["name"] == "cb-pool-nodeclass"
        assert (
            ec2_node_class["spec"]["capacityReservationSelectorTerms"][0]["id"]
            == "cr-cb12345678901234"
        )
        assert ec2_node_class["spec"]["tags"]["gco.io/capacity-block"] == "cr-cb12345678901234"

        nodepool = docs[1]
        assert nodepool["kind"] == "NodePool"
        assert nodepool["metadata"]["name"] == "cb-pool"
        labels = nodepool["spec"]["template"]["metadata"]["labels"]
        assert labels["gco.io/capacity-type"] == "capacity-block"
        # A prepaid block should be held, not aggressively consolidated.
        assert nodepool["spec"]["disruption"]["consolidationPolicy"] == "WhenEmpty"

    def test_reserved_only_by_default(self):
        """Without fallback, only the reserved capacity type is requested."""
        manifest = generate_capacity_block_nodepool_manifest(
            name="cb-pool",
            region="us-east-1",
            capacity_reservation_id="cr-reserved",
        )
        nodepool = list(yaml.safe_load_all(manifest))[1]
        requirements = nodepool["spec"]["template"]["spec"]["requirements"]
        capacity_type_req = next(
            r for r in requirements if r["key"] == "karpenter.sh/capacity-type"
        )
        assert capacity_type_req["values"] == ["reserved"]

    def test_fallback_on_demand(self):
        """With fallback, both reserved and on-demand are requested."""
        manifest = generate_capacity_block_nodepool_manifest(
            name="cb-pool",
            region="us-east-1",
            capacity_reservation_id="cr-fallback",
            fallback_on_demand=True,
        )
        assert "Fallback: on-demand" in manifest
        nodepool = list(yaml.safe_load_all(manifest))[1]
        requirements = nodepool["spec"]["template"]["spec"]["requirements"]
        capacity_type_req = next(
            r for r in requirements if r["key"] == "karpenter.sh/capacity-type"
        )
        assert "reserved" in capacity_type_req["values"]
        assert "on-demand" in capacity_type_req["values"]

    def test_gpu_taints_for_p6(self):
        """P6 (Blackwell) instances get GPU taints."""
        manifest = generate_capacity_block_nodepool_manifest(
            name="cb-pool",
            region="us-east-1",
            capacity_reservation_id="cr-gpu",
            instance_types=["p6-b200.48xlarge"],
        )
        nodepool = list(yaml.safe_load_all(manifest))[1]
        taints = nodepool["spec"]["template"]["spec"].get("taints", [])
        assert len(taints) == 1
        assert taints[0]["key"] == "nvidia.com/gpu"

    def test_efa_adds_taint_and_labels(self):
        """EFA adds an EFA taint plus efa/gpu-efa labels."""
        manifest = generate_capacity_block_nodepool_manifest(
            name="cb-pool",
            region="us-east-1",
            capacity_reservation_id="cr-efa",
            instance_types=["p5.48xlarge"],
            efa=True,
        )
        nodepool = list(yaml.safe_load_all(manifest))[1]
        taints = nodepool["spec"]["template"]["spec"]["taints"]
        assert any(t["key"] == "vpc.amazonaws.com/efa" for t in taints)
        labels = nodepool["spec"]["template"]["metadata"]["labels"]
        assert labels["efa"] == "true"

    def test_no_gpu_taints_for_cpu(self):
        """Non-GPU instance types don't get GPU taints."""
        manifest = generate_capacity_block_nodepool_manifest(
            name="cb-pool",
            region="us-east-1",
            capacity_reservation_id="cr-cpu",
            instance_types=["c5.xlarge"],
        )
        nodepool = list(yaml.safe_load_all(manifest))[1]
        assert nodepool["spec"]["template"]["spec"].get("taints", []) == []
