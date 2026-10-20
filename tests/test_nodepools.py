"""
Tests for cli/nodepools.py — Karpenter NodePool management utilities.

Covers the NodePoolInfo dataclass, ODCR NodePool manifest generation
(instance types, capacity reservation wiring, the per-type vCPU and
accelerator facts read from one EC2 DescribeInstanceTypes call, with the
reviewed accelerator catalog and DEFAULT_VCPUS_PER_NODE as the fallbacks
when that call fails), CPU limit calculation, EKS token generation for
kubectl auth, Kubernetes client configuration, and list/describe
operations. EC2 is a table of realistic records (tests/_ec2_instance_types.py)
behind a fake boto3 client, plus hand-rolled YAML assertions to verify the
rendered manifests match spec.
"""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest
import yaml
from botocore.exceptions import NoCredentialsError

from cli import nodepools
from cli.nodepools import (
    DEFAULT_VCPUS_PER_NODE,
    FACTS_SOURCE_CATALOG,
    FACTS_SOURCE_EC2,
    FACTS_SOURCE_UNKNOWN,
    AutoModeNodeClassInputs,
    InstanceTypeFacts,
    NodePoolInfo,
    accelerator_from_catalog,
    accelerator_from_ec2_record,
    calculate_cpu_limit,
    describe_cluster_nodepool,
    describe_instance_type,
    generate_capacity_block_nodepool_manifest,
    generate_odcr_nodepool_manifest,
    get_eks_token,
    get_k8s_client,
    list_cluster_nodepools,
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


@pytest.fixture
def _no_ec2():
    """The EC2 lookup fails the way it does without credentials."""
    with patch("cli.nodepools.boto3.client", side_effect=NoCredentialsError()):
        yield


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


def _facts(instance_type: str, vcpus: int, accelerator: str | None = None) -> InstanceTypeFacts:
    return InstanceTypeFacts(instance_type, vcpus, accelerator, FACTS_SOURCE_EC2)


class TestDescribeInstanceType:
    """One DescribeInstanceTypes call answers vCPUs and the accelerator class."""

    @pytest.mark.parametrize(
        ("instance_type", "vcpus", "accelerator"),
        [
            ("p4d.24xlarge", 96, "nvidia"),
            ("g4dn.xlarge", 4, "nvidia"),
            ("g5g.xlarge", 4, "nvidia"),
            ("inf2.xlarge", 4, "neuron"),
            ("trn2.3xlarge", 16, "neuron"),
            ("m5.2xlarge", 8, None),
            # An AMD GPU is a GPU, but not one the nvidia.com/gpu taint is for.
            ("g4ad.xlarge", 4, None),
        ],
    )
    def test_reads_both_facts_from_ec2(self, instance_type, vcpus, accelerator):
        facts = describe_instance_type(instance_type, "us-east-1")
        assert facts == InstanceTypeFacts(instance_type, vcpus, accelerator, FACTS_SOURCE_EC2)
        assert facts.family == instance_type.split(".")[0]

    def test_asks_ec2_once_in_the_requested_region(self):
        client = fake_ec2_client()
        with patch("cli.nodepools.boto3.client", return_value=client) as boto_client:
            describe_instance_type("p4d.24xlarge", "eu-west-1")
        boto_client.assert_called_once_with("ec2", region_name="eu-west-1")
        client.describe_instance_types.assert_called_once_with(InstanceTypes=["p4d.24xlarge"])

    @pytest.mark.usefixtures("_no_ec2")
    @pytest.mark.parametrize(
        ("instance_type", "accelerator", "source"),
        [
            # The reviewed catalog knows every GPU and Neuron family the
            # shipped NodePools use, so a failed lookup still taints correctly.
            ("p4d.24xlarge", "nvidia", FACTS_SOURCE_CATALOG),
            ("p6e-gb200.36xlarge", "nvidia", FACTS_SOURCE_CATALOG),
            ("inf2.xlarge", "neuron", FACTS_SOURCE_CATALOG),
            # A family the catalog doesn't review gets no taint, and says so.
            ("m5.2xlarge", None, FACTS_SOURCE_UNKNOWN),
            ("g4ad.xlarge", None, FACTS_SOURCE_UNKNOWN),
        ],
    )
    def test_failed_lookup_falls_back_to_the_reviewed_catalog(
        self, instance_type, accelerator, source
    ):
        facts = describe_instance_type(instance_type, "us-east-1")
        assert facts == InstanceTypeFacts(
            instance_type, DEFAULT_VCPUS_PER_NODE, accelerator, source
        )

    def test_a_type_ec2_rejects_falls_back_too(self):
        """EC2 answers a type it doesn't serve in the Region with a ClientError."""
        facts = describe_instance_type("zz9.48xlarge", "us-east-1")
        assert facts == InstanceTypeFacts(
            "zz9.48xlarge", DEFAULT_VCPUS_PER_NODE, None, FACTS_SOURCE_UNKNOWN
        )
        # The family is reviewed, so the catalog still classifies it.
        facts = describe_instance_type("p5.notasize", "us-east-1")
        assert facts == InstanceTypeFacts(
            "p5.notasize", DEFAULT_VCPUS_PER_NODE, "nvidia", FACTS_SOURCE_CATALOG
        )

    def test_an_empty_response_falls_back_too(self):
        client = MagicMock()
        client.describe_instance_types.return_value = {"InstanceTypes": []}
        with patch("cli.nodepools.boto3.client", return_value=client):
            facts = describe_instance_type("g4dn.xlarge", "us-east-1")
        assert facts == InstanceTypeFacts(
            "g4dn.xlarge", DEFAULT_VCPUS_PER_NODE, "nvidia", FACTS_SOURCE_CATALOG
        )

    @pytest.mark.parametrize(
        ("facts", "expected"),
        [
            (
                _facts("p4d.24xlarge", 96, "nvidia"),
                "# p4d.24xlarge: NVIDIA GPU, 96 vCPUs (EC2 DescribeInstanceTypes)",
            ),
            (
                InstanceTypeFacts("inf2.xlarge", 96, "neuron", FACTS_SOURCE_CATALOG),
                "# inf2.xlarge: AWS Neuron, 96 vCPUs (reviewed catalog; EC2 lookup failed)",
            ),
            (
                _facts("m5.2xlarge", 8),
                "# m5.2xlarge: no accelerator, 8 vCPUs (EC2 DescribeInstanceTypes)",
            ),
            (
                InstanceTypeFacts("zz9.xlarge", 96, None, FACTS_SOURCE_UNKNOWN),
                "# zz9.xlarge: accelerator unknown, 96 vCPUs (EC2 lookup failed and the "
                "family is not in the reviewed catalog; no accelerator taint)",
            ),
        ],
    )
    def test_describe_names_the_class_and_its_provenance(self, facts, expected):
        assert facts.describe() == expected


class TestAcceleratorClassification:
    """How an EC2 record and the reviewed catalog become an accelerator class."""

    @pytest.mark.parametrize(
        ("record", "expected"),
        [
            ({"GpuInfo": {"Gpus": [{"Manufacturer": "NVIDIA", "Name": "L4"}]}}, "nvidia"),
            ({"GpuInfo": {"Gpus": [{"Manufacturer": "nvidia"}]}}, "nvidia"),
            ({"GpuInfo": {"Gpus": [{"Manufacturer": "AMD", "Name": "Radeon Pro V520"}]}}, None),
            ({"GpuInfo": {"Gpus": ["bogus", {"Manufacturer": 7}, {}]}}, None),
            ({"GpuInfo": {"Gpus": "nope"}}, None),
            ({"GpuInfo": "nope"}, None),
            ({"NeuronInfo": {"NeuronDevices": [{"Name": "Inferentia2", "Count": 1}]}}, "neuron"),
            ({"NeuronInfo": {"NeuronDevices": []}}, None),
            ({"NeuronInfo": "x"}, None),
            (
                {
                    "GpuInfo": {"Gpus": [{"Manufacturer": "NVIDIA"}]},
                    "NeuronInfo": {"NeuronDevices": [{"Name": "Trainium"}]},
                },
                "nvidia",
            ),
            ({"InstanceType": "m5.large"}, None),
        ],
    )
    def test_ec2_record_classification_matches_the_catalog_scanner(self, record, expected):
        assert accelerator_from_ec2_record(record) == expected

    def test_catalog_classification_covers_every_reviewed_family(self):
        """The fallback is the catalog itself, so a new reviewed family needs no code change."""
        catalog = json.loads(
            (
                nodepools.importlib.resources.files("gco.config") / "accelerator_catalog.json"
            ).read_text(encoding="utf-8")
        )
        families = catalog["families"]
        assert len(families) >= 20
        for family, policy in families.items():
            assert accelerator_from_catalog(f"{family}.24xlarge") == policy["accelerator"], family
        assert accelerator_from_catalog("m5.large") is None

    def test_every_shipped_nodepool_family_resolves_to_its_pools_taint(self):
        """The shipped GPU and Neuron NodePools are the taint contract the catalog has to match."""
        from scripts.accelerator_catalog import load_nodepools

        taint_for = {"nvidia.com/gpu": "nvidia", "aws.amazon.com/neuron": "neuron"}
        pools = load_nodepools()
        assert pools, "no shipped NodePool declares instance families"
        for pool in pools:
            documents = yaml.safe_load_all(pool.path.read_text(encoding="utf-8"))
            nodepool = next(d for d in documents if d and d.get("kind") == "NodePool")
            taints = nodepool["spec"]["template"]["spec"].get("taints") or []
            expected = {taint_for[t["key"]] for t in taints if t["key"] in taint_for}
            for family in pool.families:
                assert {accelerator_from_catalog(f"{family}.xlarge")} == expected, (
                    f"{pool.location}: {family}"
                )

    @pytest.mark.parametrize(
        "broken",
        [
            OSError("no such resource"),
            ValueError("not json"),
            TypeError("not a traversable"),
        ],
    )
    def test_an_unreadable_catalog_classifies_nothing(self, broken):
        nodepools._catalog_family_accelerators.cache_clear()
        try:
            with patch("cli.nodepools.importlib.resources.files", side_effect=broken):
                assert nodepools._catalog_family_accelerators() == {}
                assert accelerator_from_catalog("p4d.24xlarge") is None
        finally:
            nodepools._catalog_family_accelerators.cache_clear()

    @pytest.mark.parametrize("payload", ['["not", "a", "mapping"]', '{"families": "none"}'])
    def test_a_malformed_catalog_classifies_nothing(self, payload):
        resource = MagicMock()
        resource.read_text.return_value = payload
        files = MagicMock()
        files.__truediv__.return_value = resource
        nodepools._catalog_family_accelerators.cache_clear()
        try:
            with patch("cli.nodepools.importlib.resources.files", return_value=files):
                assert nodepools._catalog_family_accelerators() == {}
        finally:
            nodepools._catalog_family_accelerators.cache_clear()

    def test_only_reviewed_accelerator_classes_are_kept(self):
        resource = MagicMock()
        resource.read_text.return_value = json.dumps(
            {
                "families": {
                    "p5": {"accelerator": "nvidia"},
                    "inf2": {"accelerator": "neuron"},
                    "x9": {"accelerator": "fpga"},
                    "y9": "not a policy",
                }
            }
        )
        files = MagicMock()
        files.__truediv__.return_value = resource
        nodepools._catalog_family_accelerators.cache_clear()
        try:
            with patch("cli.nodepools.importlib.resources.files", return_value=files):
                assert nodepools._catalog_family_accelerators() == {
                    "p5": "nvidia",
                    "inf2": "neuron",
                }
        finally:
            nodepools._catalog_family_accelerators.cache_clear()


class TestCalculateCpuLimit:
    """The NodePool CPU limit follows the largest instance type."""

    def test_no_instance_types_uses_the_default(self):
        assert calculate_cpu_limit([], 10) == 10 * DEFAULT_VCPUS_PER_NODE

    def test_single_instance_type(self):
        assert calculate_cpu_limit([_facts("g4dn.xlarge", 4, "nvidia")], 10) == 10 * 4

    def test_multiple_instance_types_use_the_largest(self):
        facts = [_facts("g4dn.xlarge", 4, "nvidia"), _facts("p5.48xlarge", 192, "nvidia")]
        assert calculate_cpu_limit(facts, 10) == 10 * 192


class TestGenerateOdcrNodepoolManifest:
    """Tests for generate_odcr_nodepool_manifest function."""

    def test_basic_manifest_generation(self):
        """Test generating a basic ODCR NodePool manifest."""
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
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

        # The EKS Auto Mode NodeClass: the only node class API a GCO cluster serves.
        node_class = docs[0]
        assert node_class["apiVersion"] == "eks.amazonaws.com/v1"
        assert node_class["kind"] == "NodeClass"
        assert node_class["metadata"]["name"] == "test-odcr-nodeclass"
        assert node_class["spec"] == {
            "role": NODE_CLASS.role,
            "subnetSelectorTerms": [{"id": "subnet-0aaa"}, {"id": "subnet-0bbb"}],
            "securityGroupSelectorTerms": [{"id": "sg-0cluster"}],
            "capacityReservationSelectorTerms": [{"id": "cr-12345678901234567"}],
        }

        # Check NodePool
        nodepool = docs[1]
        assert nodepool["kind"] == "NodePool"
        assert nodepool["metadata"]["name"] == "test-odcr"
        assert nodepool["spec"]["template"]["spec"]["nodeClassRef"] == {
            "group": "eks.amazonaws.com",
            "kind": "NodeClass",
            "name": "test-odcr-nodeclass",
        }
        # The header says how to apply it, target it, and what it changes.
        assert "karpenter.sh/nodepool: test-odcr" in manifest
        assert "stops" in manifest and "open ODCRs" in manifest

    def test_manifest_with_instance_types(self):
        """Test generating manifest with specific instance types."""
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
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
            node_class=NODE_CLASS,
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
            node_class=NODE_CLASS,
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
            node_class=NODE_CLASS,
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
            node_class=NODE_CLASS,
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
            node_class=NODE_CLASS,
            name="limited-pool",
            region="us-east-1",
            capacity_reservation_id="cr-limited",
            max_nodes=50,
        )

        docs = list(yaml.safe_load_all(manifest))
        nodepool = docs[1]

        # Without instance types, uses default 96 vCPUs per node
        assert nodepool["spec"]["limits"]["cpu"] == str(50 * 96)

    def test_manifest_cpu_limit_with_instance_types(self):
        """Test that CPU limit is calculated based on instance type vCPUs."""
        # p4d.24xlarge has 96 vCPUs
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="p4d-pool",
            region="us-east-1",
            capacity_reservation_id="cr-p4d",
            instance_types=["p4d.24xlarge"],
            max_nodes=10,
        )

        docs = list(yaml.safe_load_all(manifest))
        nodepool = docs[1]
        assert nodepool["spec"]["limits"]["cpu"] == str(10 * 96)

    def test_manifest_cpu_limit_with_small_instance(self):
        """Test CPU limit with smaller instance type."""
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
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

    def test_manifest_cpu_limit_with_multiple_instance_types(self):
        """Test that CPU limit uses max vCPUs when multiple instance types specified."""
        # g4dn.xlarge has 4 vCPUs, p5.48xlarge 192
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
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
            node_class=NODE_CLASS,
            name="labeled-pool",
            region="us-east-1",
            capacity_reservation_id="cr-labeled",
        )

        docs = list(yaml.safe_load_all(manifest))
        node_class = docs[0]
        nodepool = docs[1]

        # NodeClass labels identify the pool and the reservation it binds.
        labels = node_class["metadata"]["labels"]
        assert labels["app.kubernetes.io/part-of"] == "gco"
        assert labels["gco.io/nodepool"] == "labeled-pool"
        assert labels["gco.io/capacity-reservation"] == "cr-labeled"

        # No EC2 tags: propagating custom tags needs IAM the cluster role lacks.
        assert "tags" not in node_class["spec"]

        # Check NodePool labels
        assert nodepool["metadata"]["labels"]["app.kubernetes.io/part-of"] == "gco"


class TestAcceleratorTaintsFollowTheInstanceTypes:
    """The taint comes from what EC2 says the type carries, not from a family list."""

    @staticmethod
    def _taint_keys(manifest: str) -> list[str]:
        nodepool = list(yaml.safe_load_all(manifest))[1]
        return [t["key"] for t in nodepool["spec"]["template"]["spec"].get("taints", [])]

    @pytest.mark.parametrize(
        ("instance_types", "expected"),
        [
            (["g4dn.xlarge"], ["nvidia.com/gpu"]),
            (["g5g.xlarge"], ["nvidia.com/gpu"]),
            (["inf2.xlarge"], ["aws.amazon.com/neuron"]),
            (["trn2.3xlarge"], ["aws.amazon.com/neuron"]),
            # One pool for a mixed reservation set carries both taints.
            (["p5.48xlarge", "trn2.3xlarge"], ["nvidia.com/gpu", "aws.amazon.com/neuron"]),
            # g4* is not "NVIDIA": the AMD g4ad gets no GPU taint.
            (["g4ad.xlarge"], []),
            (["m6i.large", "c5n.18xlarge"], []),
        ],
    )
    def test_taints_match_ec2_hardware(self, instance_types, expected):
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="pool",
            region="us-east-1",
            capacity_reservation_id="cr-1",
            instance_types=instance_types,
        )
        assert self._taint_keys(manifest) == expected

    def test_header_lists_each_type_with_its_class_and_provenance(self):
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="pool",
            region="us-east-1",
            capacity_reservation_id="cr-1",
            instance_types=["p5.48xlarge", "m6i.large"],
        )
        assert "# Instance types:" in manifest
        assert "# p5.48xlarge: NVIDIA GPU, 192 vCPUs (EC2 DescribeInstanceTypes)" in manifest
        assert "# m6i.large: no accelerator, 2 vCPUs (EC2 DescribeInstanceTypes)" in manifest
        assert "9600" not in manifest  # the limit follows EC2's 192, not the default 96

    def test_without_instance_types_the_header_lists_none(self):
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS, name="pool", region="us-east-1", capacity_reservation_id="cr-1"
        )
        assert "# Instance types:" not in manifest

    @pytest.mark.usefixtures("_no_ec2")
    def test_without_ec2_the_reviewed_catalog_decides(self):
        """No credentials: the catalog the monthly scan maintains still taints GPU families."""
        manifest = generate_capacity_block_nodepool_manifest(
            node_class=NODE_CLASS,
            name="pool",
            region="us-east-1",
            capacity_reservation_id="cr-1",
            instance_types=["p6-b200.48xlarge", "trn2.3xlarge"],
        )
        assert self._taint_keys(manifest) == ["nvidia.com/gpu", "aws.amazon.com/neuron"]
        assert "(reviewed catalog; EC2 lookup failed)" in manifest
        nodepool = list(yaml.safe_load_all(manifest))[1]
        assert nodepool["spec"]["limits"]["cpu"] == str(100 * DEFAULT_VCPUS_PER_NODE)

    @pytest.mark.usefixtures("_no_ec2")
    def test_an_unreviewed_family_without_ec2_gets_no_taint_and_says_so(self):
        manifest = generate_odcr_nodepool_manifest(
            node_class=NODE_CLASS,
            name="pool",
            region="us-east-1",
            capacity_reservation_id="cr-1",
            instance_types=["zz9.48xlarge"],
        )
        assert self._taint_keys(manifest) == []
        assert "family is not in the reviewed catalog; no accelerator taint" in manifest


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
            node_class=NODE_CLASS,
            name="cb-pool",
            region="us-east-1",
            capacity_reservation_id="cr-cb12345678901234",
        )

        assert "# Capacity Block-backed NodePool for GCO" in manifest
        assert "cr-cb12345678901234" in manifest
        assert "us-east-1" in manifest

        docs = list(yaml.safe_load_all(manifest))
        assert len(docs) == 2

        node_class = docs[0]
        assert node_class["kind"] == "NodeClass"
        assert node_class["apiVersion"] == "eks.amazonaws.com/v1"
        assert node_class["metadata"]["name"] == "cb-pool-nodeclass"
        assert node_class["spec"]["capacityReservationSelectorTerms"] == [
            {"id": "cr-cb12345678901234"}
        ]
        assert node_class["spec"]["role"] == NODE_CLASS.role
        assert node_class["metadata"]["labels"]["gco.io/capacity-block"] == "cr-cb12345678901234"

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
            node_class=NODE_CLASS,
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
            node_class=NODE_CLASS,
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
            node_class=NODE_CLASS,
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
            node_class=NODE_CLASS,
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
            node_class=NODE_CLASS,
            name="cb-pool",
            region="us-east-1",
            capacity_reservation_id="cr-cpu",
            instance_types=["c5.xlarge"],
        )
        nodepool = list(yaml.safe_load_all(manifest))[1]
        assert nodepool["spec"]["template"]["spec"].get("taints", []) == []
