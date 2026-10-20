"""
NodePool management utilities for GCO CLI.

Provides functionality to create and manage EKS Auto Mode NodePools with
support for On-Demand Capacity Reservations (ODCRs) and Capacity Blocks.

Key Features:
- Generate NodeClass + NodePool manifests for ODCR- and Capacity Block-backed
  capacity, bound through the Auto Mode NodeClass's
  ``capacityReservationSelectorTerms``
- Taint reserved GPU and Neuron nodes the way the shipped NodePools do, with
  each instance type's accelerator class read from EC2 (``DescribeInstanceTypes``)
  and, when that lookup fails, from the reviewed accelerator catalog the
  monthly dependency scan keeps current
- List, describe and delete NodePools in EKS clusters
- Support for fallback to on-demand when the reservation is exhausted

See: https://docs.aws.amazon.com/eks/latest/userguide/auto-odcr.html
"""

import base64
import functools
import importlib.resources
import json
import logging
import os
import tempfile
import weakref
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import boto3
import yaml
from kubernetes.client import CustomObjectsApi

logger = logging.getLogger(__name__)

# Default vCPU count when instance type lookup fails (conservative estimate)
DEFAULT_VCPUS_PER_NODE = 96

#: Where an instance type's accelerator class came from (``InstanceTypeFacts.source``).
FACTS_SOURCE_EC2 = "ec2"
FACTS_SOURCE_CATALOG = "catalog"
FACTS_SOURCE_UNKNOWN = "unknown"

#: The reviewed accelerator catalog, shipped inside the ``gco.config`` package.
#: The monthly dependency scan compares its families with EC2 and the
#: maintainer refreshes it (docs/MAINTENANCE.md), so it is the offline answer
#: to "does this family carry an NVIDIA GPU or Neuron devices" and nothing in
#: this module keeps its own list of families.
_ACCELERATOR_CATALOG_RESOURCE = "accelerator_catalog.json"
_ACCELERATOR_CLASSES = frozenset({"nvidia", "neuron"})


@dataclass(frozen=True)
class InstanceTypeFacts:
    """What the NodePool generator needs to know about one EC2 instance type.

    ``accelerator`` is ``"nvidia"``, ``"neuron"`` or ``None``; ``source`` says
    whether it was read live from EC2, from the reviewed catalog because the
    EC2 lookup failed, or could not be determined at all.
    """

    instance_type: str
    vcpus: int
    accelerator: str | None
    source: str

    @property
    def family(self) -> str:
        return self.instance_type.partition(".")[0]

    def describe(self) -> str:
        """One manifest-comment line: the class, the vCPUs and where they came from."""
        if self.accelerator == "nvidia":
            hardware = "NVIDIA GPU"
        elif self.accelerator == "neuron":
            hardware = "AWS Neuron"
        elif self.source == FACTS_SOURCE_UNKNOWN:
            hardware = "accelerator unknown"
        else:
            hardware = "no accelerator"
        provenance = {
            FACTS_SOURCE_EC2: "EC2 DescribeInstanceTypes",
            FACTS_SOURCE_CATALOG: "reviewed catalog; EC2 lookup failed",
        }.get(
            self.source,
            "EC2 lookup failed and the family is not in the reviewed catalog; no accelerator taint",
        )
        return f"# {self.instance_type}: {hardware}, {self.vcpus} vCPUs ({provenance})"


def accelerator_from_ec2_record(record: Mapping[str, Any]) -> str | None:
    """Classify one ``DescribeInstanceTypes`` record the way the accelerator catalog does.

    An NVIDIA GPU (``GpuInfo.Gpus[].Manufacturer``) wins over Neuron devices;
    AMD GPUs, FPGAs and media accelerators are not accelerators GCO schedules.
    """
    gpu_info = record.get("GpuInfo")
    gpus = gpu_info.get("Gpus") if isinstance(gpu_info, Mapping) else None
    for gpu in gpus if isinstance(gpus, list) else []:
        manufacturer = gpu.get("Manufacturer") if isinstance(gpu, Mapping) else None
        if isinstance(manufacturer, str) and manufacturer.casefold() == "nvidia":
            return "nvidia"
    neuron_info = record.get("NeuronInfo")
    devices = neuron_info.get("NeuronDevices") if isinstance(neuron_info, Mapping) else None
    if isinstance(devices, list) and devices:
        return "neuron"
    return None


@functools.cache
def _catalog_family_accelerators() -> dict[str, str]:
    """``family -> accelerator`` from the reviewed catalog; empty when it can't be read."""
    try:
        resource = importlib.resources.files("gco.config") / _ACCELERATOR_CATALOG_RESOURCE
        parsed = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        logger.debug("Reviewed accelerator catalog unavailable: %s", exc)
        return {}
    families = parsed.get("families") if isinstance(parsed, dict) else None
    if not isinstance(families, dict):
        return {}
    return {
        str(name): str(policy["accelerator"])
        for name, policy in families.items()
        if isinstance(policy, dict) and policy.get("accelerator") in _ACCELERATOR_CLASSES
    }


def accelerator_from_catalog(instance_type: str) -> str | None:
    """The reviewed catalog's accelerator class for an instance type's family, if any."""
    return _catalog_family_accelerators().get(instance_type.partition(".")[0])


def describe_instance_type(instance_type: str, region: str = "us-east-1") -> InstanceTypeFacts:
    """Read an instance type's vCPUs and accelerator class from EC2, with fallbacks.

    One ``DescribeInstanceTypes`` call answers both. When it fails (no
    credentials, an unknown type in this Region, throttling) the vCPU count
    falls back to :data:`DEFAULT_VCPUS_PER_NODE` and the accelerator class to
    the reviewed catalog's entry for the family.
    """
    try:
        ec2 = boto3.client("ec2", region_name=region)
        response = ec2.describe_instance_types(InstanceTypes=[instance_type])
        records = response.get("InstanceTypes") or []
        if records:
            record = records[0]
            return InstanceTypeFacts(
                instance_type=instance_type,
                vcpus=int(record["VCpuInfo"]["DefaultVCpus"]),
                accelerator=accelerator_from_ec2_record(record),
                source=FACTS_SOURCE_EC2,
            )
        logger.debug("EC2 returned no record for instance type %s in %s", instance_type, region)
    except Exception as exc:
        logger.debug("Failed to describe instance type %s in %s: %s", instance_type, region, exc)
    accelerator = accelerator_from_catalog(instance_type)
    return InstanceTypeFacts(
        instance_type=instance_type,
        vcpus=DEFAULT_VCPUS_PER_NODE,
        accelerator=accelerator,
        source=FACTS_SOURCE_CATALOG if accelerator is not None else FACTS_SOURCE_UNKNOWN,
    )


def _unlink_temp_ca_cert(path: str) -> None:
    """Best-effort cleanup for a CA file owned by a Kubernetes API client."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        # Already gone (e.g. removed by another cleanup path) — the goal is an
        # absent file, so this is success, not a failure to log or retry.
        pass
    except OSError as exc:
        logger.debug("Failed to remove temporary Kubernetes CA certificate %s: %s", path, exc)


def calculate_cpu_limit(facts: Sequence[InstanceTypeFacts], max_nodes: int) -> int:
    """The NodePool CPU limit: ``max_nodes`` times the largest instance type's vCPUs.

    Without instance types (any type the reservation offers) the conservative
    :data:`DEFAULT_VCPUS_PER_NODE` stands in for the largest type.
    """
    if not facts:
        return max_nodes * DEFAULT_VCPUS_PER_NODE
    return max_nodes * max(fact.vcpus for fact in facts)


@dataclass
class NodePoolInfo:
    """Information about a Karpenter NodePool."""

    name: str
    capacity_type: str  # "on-demand", "spot", "reserved"
    instance_types: list[str]
    max_nodes: int | None
    status: str
    node_count: int
    capacity_reservation_id: str | None = None


#: EKS Auto Mode's node class API. GCO clusters run Auto Mode, which serves
#: ``NodeClass`` objects in this group; self-managed Karpenter's
#: ``karpenter.k8s.aws`` ``EC2NodeClass`` CRD does not exist on them.
AUTO_MODE_NODECLASS_GROUP = "eks.amazonaws.com"
AUTO_MODE_NODECLASS_VERSION = "v1"
AUTO_MODE_NODECLASS_KIND = "NodeClass"
AUTO_MODE_NODECLASS_PLURAL = "nodeclasses"

#: Header lines every generated reservation manifest carries.
_RESERVATION_MANIFEST_NOTES = (
    "#",
    "# Apply with: kubectl apply -f <this-file>.yaml. The EKS endpoint is private by",
    "# default: run `gco cluster tunnel --via-ssm auto` and add the kubectl flags it prints.",
    "# Target the pool from a pod with nodeSelector karpenter.sh/nodepool: {name},",
    "# plus a toleration for the pool's taint when it has one.",
    "#",
    "# Once any NodeClass selects capacity reservations, EKS Auto Mode stops",
    "# launching into open ODCRs on its own for every NodeClass in the cluster.",
    "# See: https://docs.aws.amazon.com/eks/latest/userguide/auto-odcr.html",
)


@dataclass(frozen=True)
class AutoModeNodeClassInputs:
    """What a custom Auto Mode NodeClass needs to launch nodes like the built-in one.

    ``role`` is the cluster's Auto Mode node role, which already has the EC2
    access entry nodes join with; ``subnet_ids`` and ``security_group_ids`` are
    the cluster's own subnets and cluster security group, which the built-in
    ``default`` NodeClass uses too.
    """

    role: str
    subnet_ids: tuple[str, ...]
    security_group_ids: tuple[str, ...]


def resolve_auto_mode_node_class_inputs(cluster_name: str, region: str) -> AutoModeNodeClassInputs:
    """Read the node role, subnets and security group of an Auto Mode cluster.

    The CDK app names none of these (CloudFormation generates the role name),
    so they come from ``eks:DescribeCluster`` at generation time.

    Raises:
        ValueError: The cluster does not run EKS Auto Mode, or reports no node
            role, subnet, or cluster security group.
    """
    eks = boto3.client("eks", region_name=region)
    cluster = eks.describe_cluster(name=cluster_name)["cluster"]
    compute = cluster.get("computeConfig") or {}
    role_arn = compute.get("nodeRoleArn")
    if not compute.get("enabled") or not isinstance(role_arn, str) or not role_arn:
        raise ValueError(
            f"EKS cluster {cluster_name} in {region} does not run EKS Auto Mode with a "
            "node role, so it has no NodeClass API to bind a reservation to"
        )
    vpc_config = cluster.get("resourcesVpcConfig") or {}
    subnet_ids = tuple(
        subnet for subnet in vpc_config.get("subnetIds") or () if isinstance(subnet, str) and subnet
    )
    security_group = vpc_config.get("clusterSecurityGroupId")
    if not subnet_ids or not isinstance(security_group, str) or not security_group:
        raise ValueError(
            f"EKS cluster {cluster_name} in {region} reports no subnets or cluster security group"
        )
    return AutoModeNodeClassInputs(
        role=role_arn.rsplit("/", 1)[-1],
        subnet_ids=subnet_ids,
        security_group_ids=(security_group,),
    )


def _reservation_node_class(
    name: str,
    capacity_reservation_id: str,
    node_class: AutoModeNodeClassInputs,
    reservation_label: str,
) -> dict[str, Any]:
    """The Auto Mode NodeClass that binds one capacity reservation."""
    return {
        "apiVersion": f"{AUTO_MODE_NODECLASS_GROUP}/{AUTO_MODE_NODECLASS_VERSION}",
        "kind": AUTO_MODE_NODECLASS_KIND,
        "metadata": {
            "name": f"{name}-nodeclass",
            "labels": {
                "app.kubernetes.io/part-of": "gco",
                "gco.io/nodepool": name,
                reservation_label: capacity_reservation_id,
            },
        },
        "spec": {
            "role": node_class.role,
            "subnetSelectorTerms": [{"id": subnet} for subnet in node_class.subnet_ids],
            "securityGroupSelectorTerms": [
                {"id": group} for group in node_class.security_group_ids
            ],
            "capacityReservationSelectorTerms": [{"id": capacity_reservation_id}],
        },
    }


def _reservation_nodepool(
    name: str,
    facts: Sequence[InstanceTypeFacts],
    max_nodes: int,
    fallback_on_demand: bool,
    efa: bool,
    template_labels: dict[str, str],
    disruption: dict[str, Any],
) -> dict[str, Any]:
    """The NodePool that launches reserved capacity through ``<name>-nodeclass``.

    Reserved nodes get the same NoSchedule taint the shipped GPU and Neuron
    NodePools carry, so only pods that tolerate it (the workloads the
    reservation was bought for) consume them. Which taint follows from each
    instance type's accelerator class in ``facts``, read from EC2 or the
    reviewed catalog; no family list is kept here.
    """
    capacity_types = ["reserved", "on-demand"] if fallback_on_demand else ["reserved"]
    requirements: list[dict[str, Any]] = [
        {"key": "karpenter.sh/capacity-type", "operator": "In", "values": capacity_types},
    ]
    if facts:
        # The reservation's instance types pin the architecture, Arm included.
        requirements.append(
            {
                "key": "node.kubernetes.io/instance-type",
                "operator": "In",
                "values": [fact.instance_type for fact in facts],
            }
        )
    else:
        requirements.append({"key": "kubernetes.io/arch", "operator": "In", "values": ["amd64"]})

    labels = dict(template_labels)
    taints: list[dict[str, str]] = []
    accelerators = {fact.accelerator for fact in facts}
    if "nvidia" in accelerators:
        taints.append({"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"})
    if "neuron" in accelerators:
        taints.append({"key": "aws.amazon.com/neuron", "value": "true", "effect": "NoSchedule"})
    if efa:
        labels["efa"] = "true"
        labels["workload-type"] = "gpu-efa"
        if taints:
            taints.append({"key": "vpc.amazonaws.com/efa", "value": "true", "effect": "NoSchedule"})

    template_spec: dict[str, Any] = {
        "nodeClassRef": {
            "group": AUTO_MODE_NODECLASS_GROUP,
            "kind": AUTO_MODE_NODECLASS_KIND,
            "name": f"{name}-nodeclass",
        },
        "requirements": requirements,
    }
    if taints:
        template_spec["taints"] = taints

    return {
        "apiVersion": "karpenter.sh/v1",
        "kind": "NodePool",
        "metadata": {"name": name, "labels": {"app.kubernetes.io/part-of": "gco"}},
        "spec": {
            "template": {"metadata": {"labels": labels}, "spec": template_spec},
            "limits": {"cpu": str(calculate_cpu_limit(facts, max_nodes))},
            "disruption": disruption,
        },
    }


def _render_reservation_manifest(
    header: list[str],
    name: str,
    facts: Sequence[InstanceTypeFacts],
    node_class: dict[str, Any],
    nodepool: dict[str, Any],
) -> str:
    notes = [line.format(name=name) for line in _RESERVATION_MANIFEST_NOTES]
    if facts:
        notes = ["# Instance types:", *(fact.describe() for fact in facts), *notes]
    return "\n".join(
        [
            *header,
            *notes,
            "---",
            yaml.dump(node_class, default_flow_style=False, sort_keys=False),
            "---",
            yaml.dump(nodepool, default_flow_style=False, sort_keys=False),
        ]
    )


def _instance_type_facts(
    instance_types: Sequence[str] | None, region: str
) -> tuple[InstanceTypeFacts, ...]:
    return tuple(describe_instance_type(item, region) for item in instance_types or ())


def generate_odcr_nodepool_manifest(
    name: str,
    region: str,
    capacity_reservation_id: str,
    *,
    node_class: AutoModeNodeClassInputs,
    instance_types: list[str] | None = None,
    max_nodes: int = 100,
    fallback_on_demand: bool = False,
    efa: bool = False,
) -> str:
    """
    Generate an EKS Auto Mode NodeClass and NodePool for ODCR-backed capacity.

    The NodeClass binds the reservation through
    ``capacityReservationSelectorTerms``; nodes launched into it carry
    ``karpenter.sh/capacity-type: reserved`` and the
    ``eks.amazonaws.com/capacity-reservation-id`` label.

    Args:
        name: Name for the NodePool (the NodeClass is ``<name>-nodeclass``)
        region: AWS region
        capacity_reservation_id: EC2 Capacity Reservation ID (cr-xxx)
        node_class: The cluster's node role, subnets and security group, from
            :func:`resolve_auto_mode_node_class_inputs`
        instance_types: List of instance types (if None, any amd64 type the
            reservation offers)
        max_nodes: Maximum number of nodes
        fallback_on_demand: Whether to fall back to on-demand if ODCR exhausted
        efa: Whether to enable EFA support (adds EFA taint and labels)

    Returns:
        YAML manifest string for the NodeClass and NodePool
    """
    disruption: dict[str, Any] = {
        "consolidationPolicy": "WhenEmptyOrUnderutilized",
        "consolidateAfter": "30s",
        "budgets": [{"nodes": "10%"}],
    }
    if efa:
        # WhenEmpty for EFA workloads: never disrupt a long-running
        # distributed training job to consolidate.
        disruption = {
            "consolidationPolicy": "WhenEmpty",
            "consolidateAfter": "300s",
            "budgets": [{"nodes": "10%"}],
        }
    facts = _instance_type_facts(instance_types, region)
    nodepool = _reservation_nodepool(
        name,
        facts,
        max_nodes,
        fallback_on_demand,
        efa,
        {
            "workload-type": "reserved-capacity",
            "project": "gco",
            "gco.io/capacity-reservation": capacity_reservation_id,
        },
        disruption,
    )
    header = [
        "# ODCR-backed NodePool for GCO (EKS Auto Mode)",
        f"# Capacity Reservation: {capacity_reservation_id}",
        f"# Region: {region}",
    ]
    if fallback_on_demand:
        header.append("# Fallback: on-demand (when ODCR exhausted)")
    return _render_reservation_manifest(
        header,
        name,
        facts,
        _reservation_node_class(
            name, capacity_reservation_id, node_class, "gco.io/capacity-reservation"
        ),
        nodepool,
    )


def generate_capacity_block_nodepool_manifest(
    name: str,
    region: str,
    capacity_reservation_id: str,
    *,
    node_class: AutoModeNodeClassInputs,
    instance_types: list[str] | None = None,
    max_nodes: int = 100,
    fallback_on_demand: bool = False,
    efa: bool = False,
) -> str:
    """
    Generate an EKS Auto Mode NodeClass and NodePool for a Capacity Block.

    The Capacity Block counterpart to :func:`generate_odcr_nodepool_manifest`.
    Purchasing a Capacity Block ("gco capacity reserve") yields a normal EC2
    Capacity Reservation id (``cr-...``), which Auto Mode consumes through the
    same ``capacityReservationSelectorTerms`` / ``reserved`` capacity type as an
    ODCR; Auto Mode never uses a Capacity Block without such a selector. The
    difference is intent: a Capacity Block is prepaid for a fixed term, so the
    generated NodePool defaults to holding that capacity (``WhenEmpty``
    consolidation with a long delay) rather than consolidating aggressively.

    Args:
        name: Name for the NodePool (the NodeClass is ``<name>-nodeclass``).
        region: AWS region.
        capacity_reservation_id: Capacity Reservation ID (cr-xxx) of the purchased
            Capacity Block (from ``gco capacity reserve`` / reservation-check).
        node_class: The cluster's node role, subnets and security group, from
            :func:`resolve_auto_mode_node_class_inputs`.
        instance_types: List of instance types (if None, any amd64 type).
        max_nodes: Maximum number of nodes.
        fallback_on_demand: Whether to fall back to on-demand when the block is
            exhausted or after it expires. Off by default — a Capacity Block is
            fixed-term guaranteed capacity, so silent on-demand fallback can
            surprise-bill; opt in explicitly.
        efa: Whether to enable EFA support (adds EFA taint and labels).

    Returns:
        YAML manifest string for the NodeClass and NodePool.
    """
    facts = _instance_type_facts(instance_types, region)
    nodepool = _reservation_nodepool(
        name,
        facts,
        max_nodes,
        fallback_on_demand,
        efa,
        {
            "workload-type": "capacity-block",
            "project": "gco",
            "gco.io/capacity-type": "capacity-block",
            "gco.io/capacity-block": capacity_reservation_id,
        },
        # A Capacity Block is prepaid for a fixed term — hold the nodes rather
        # than consolidating them away, so the paid capacity stays available
        # for the whole block.
        {
            "consolidationPolicy": "WhenEmpty",
            "consolidateAfter": "600s",
            "budgets": [{"nodes": "10%"}],
        },
    )
    header = [
        "# Capacity Block-backed NodePool for GCO (EKS Auto Mode)",
        f"# Capacity Reservation (from Capacity Block): {capacity_reservation_id}",
        f"# Region: {region}",
    ]
    if fallback_on_demand:
        header.append("# Fallback: on-demand (when Capacity Block exhausted/expired)")
    return _render_reservation_manifest(
        header,
        name,
        facts,
        _reservation_node_class(name, capacity_reservation_id, node_class, "gco.io/capacity-block"),
        nodepool,
    )


def get_eks_token(cluster_name: str, region: str) -> str:
    """Generate EKS authentication token using STS presigned URL."""
    from botocore.signers import RequestSigner

    session = boto3.Session()
    sts_client = session.client("sts", region_name=region)
    service_id = sts_client.meta.service_model.service_id

    signer = RequestSigner(
        service_id, region, "sts", "v4", session.get_credentials(), session.events
    )

    params = {
        "method": "GET",
        "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "body": {},
        "headers": {"x-k8s-aws-id": cluster_name},
        "context": {},
    }

    url = signer.generate_presigned_url(
        params, region_name=region, expires_in=60, operation_name=""
    )

    token_b64 = base64.urlsafe_b64encode(url.encode("utf-8")).decode("utf-8").rstrip("=")
    return f"k8s-aws-v1.{token_b64}"


def get_k8s_client(
    cluster_name: str,
    region: str,
    *,
    server: str | None = None,
    tls_server_name: str | None = None,
) -> CustomObjectsApi:
    """Get configured Kubernetes client for EKS cluster.

    ``server`` and ``tls_server_name`` point the client at an SSM tunnel to the
    cluster's private endpoint (``gco cluster tunnel``): requests go to the
    local tunnel address while TLS keeps the endpoint's own host name for SNI
    and certificate verification, so the cluster CA still validates.
    """
    from kubernetes import client

    eks = boto3.client("eks", region_name=region)
    cluster_info = eks.describe_cluster(name=cluster_name)
    cluster = cluster_info["cluster"]

    configuration = client.Configuration()
    configuration.host = server or cluster["endpoint"]
    if tls_server_name:
        configuration.tls_server_name = tls_server_name
    configuration.verify_ssl = True

    # The ApiClient reads this path throughout its lifetime. Keep the file until
    # that client is collected, while cleaning every partially constructed path.
    ca_cert = base64.b64decode(cluster["certificateAuthority"]["data"])
    fd, ca_cert_path = tempfile.mkstemp(suffix=".crt")
    fd_owned = True
    api_client = None
    try:
        ca_file = os.fdopen(fd, "wb")
        fd_owned = False  # os.fdopen transferred descriptor ownership to ca_file.
        with ca_file:
            ca_file.write(ca_cert)
            ca_file.flush()
        configuration.ssl_ca_cert = ca_cert_path

        # Generate EKS token
        eks_token = get_eks_token(cluster_name, region)
        configuration.api_key = {"authorization": f"Bearer {eks_token}"}

        # Create API client with the configuration explicitly
        api_client = client.ApiClient(configuration)
        custom_api = client.CustomObjectsApi(api_client)
    except BaseException:
        if fd_owned:
            with suppress(OSError):
                os.close(fd)
        if api_client is not None:
            with suppress(Exception):
                api_client.close()
        _unlink_temp_ca_cert(ca_cert_path)
        raise

    # CustomObjectsApi retains api_client, so the certificate remains available
    # for lazy TLS setup and is removed when the owning client is released.
    weakref.finalize(api_client, _unlink_temp_ca_cert, ca_cert_path)
    return custom_api


def list_cluster_nodepools(
    cluster_name: str,
    region: str,
    *,
    server: str | None = None,
    tls_server_name: str | None = None,
) -> list[dict[str, Any]]:
    """
    List NodePools in an EKS cluster.

    Args:
        cluster_name: EKS cluster name
        region: AWS region
        server: API server override (an SSM tunnel address); see :func:`get_k8s_client`
        tls_server_name: The endpoint host name TLS verifies against through the tunnel

    Returns:
        List of NodePool information dictionaries
    """
    try:
        custom_api = get_k8s_client(
            cluster_name, region, server=server, tls_server_name=tls_server_name
        )

        nodepools = custom_api.list_cluster_custom_object(
            group="karpenter.sh",
            version="v1",
            plural="nodepools",
        )

        result = []
        for np in nodepools.get("items", []):
            spec = np.get("spec", {})
            template = spec.get("template", {}).get("spec", {})
            requirements = template.get("requirements", [])

            # Extract capacity types
            capacity_types = []
            instance_types = []
            for req in requirements:
                if req.get("key") == "karpenter.sh/capacity-type":
                    capacity_types = req.get("values", [])
                elif req.get("key") == "node.kubernetes.io/instance-type":
                    instance_types = req.get("values", [])

            # Get status
            status = np.get("status", {})
            conditions = status.get("conditions", [])
            ready_condition: dict[str, Any] = next(
                (c for c in conditions if c.get("type") == "Ready"), {}
            )

            result.append(
                {
                    "name": np["metadata"]["name"],
                    "capacity_types": ", ".join(capacity_types) or "on-demand",
                    "instance_types": ", ".join(instance_types[:3])
                    + ("..." if len(instance_types) > 3 else "")
                    or "any",
                    "status": "Ready" if ready_condition.get("status") == "True" else "NotReady",
                    "limits": spec.get("limits", {}),
                }
            )

        return result

    except Exception as e:
        raise RuntimeError(f"Failed to list NodePools: {e}") from e


def describe_cluster_nodepool(
    cluster_name: str,
    region: str,
    nodepool_name: str,
    *,
    server: str | None = None,
    tls_server_name: str | None = None,
) -> dict[str, Any] | None:
    """
    Describe a specific NodePool in an EKS cluster.

    Args:
        cluster_name: EKS cluster name
        region: AWS region
        nodepool_name: Name of the NodePool
        server: API server override (an SSM tunnel address); see :func:`get_k8s_client`
        tls_server_name: The endpoint host name TLS verifies against through the tunnel

    Returns:
        NodePool details or None if not found
    """
    try:
        custom_api = get_k8s_client(
            cluster_name, region, server=server, tls_server_name=tls_server_name
        )

        nodepool = custom_api.get_cluster_custom_object(
            group="karpenter.sh",
            version="v1",
            plural="nodepools",
            name=nodepool_name,
        )

        if isinstance(nodepool, dict):
            return nodepool
        return None

    except Exception as e:
        if "404" in str(e):
            return None
        raise RuntimeError(f"Failed to describe NodePool: {e}") from e


def delete_cluster_nodepool(
    cluster_name: str,
    region: str,
    nodepool_name: str,
    *,
    server: str | None = None,
    tls_server_name: str | None = None,
) -> dict[str, Any]:
    """
    Delete a NodePool (and its paired NodeClass) from an EKS cluster.

    Deletes the NodePool ``nodepool_name`` and, when present, the Auto Mode
    NodeClass named ``<nodepool_name>-nodeclass`` that the GCO manifest
    generators create alongside it. EKS Auto Mode drains and terminates any
    nodes the NodePool provisioned once it is removed. A missing NodeClass
    (custom name, or already deleted) is not treated as an error.

    Args:
        cluster_name: EKS cluster name
        region: AWS region
        nodepool_name: Name of the NodePool to delete
        server: API server override (an SSM tunnel address); see :func:`get_k8s_client`
        tls_server_name: The endpoint host name TLS verifies against through the tunnel

    Returns:
        Dict describing what was deleted: ``{"nodepool": <name>,
        "nodeclass": <name-or-None>}``.
    """
    try:
        custom_api = get_k8s_client(
            cluster_name, region, server=server, tls_server_name=tls_server_name
        )

        custom_api.delete_cluster_custom_object(
            group="karpenter.sh",
            version="v1",
            plural="nodepools",
            name=nodepool_name,
        )
        deleted: dict[str, Any] = {"nodepool": nodepool_name, "nodeclass": None}

        nodeclass_name = f"{nodepool_name}-nodeclass"
        try:
            custom_api.delete_cluster_custom_object(
                group=AUTO_MODE_NODECLASS_GROUP,
                version=AUTO_MODE_NODECLASS_VERSION,
                plural=AUTO_MODE_NODECLASS_PLURAL,
                name=nodeclass_name,
            )
            deleted["nodeclass"] = nodeclass_name
        except Exception as e:  # best effort; the NodePool is the primary target
            if "404" not in str(e):
                logger.warning("Could not delete NodeClass %s: %s", nodeclass_name, e)

        return deleted

    except Exception as e:
        raise RuntimeError(f"Failed to delete NodePool: {e}") from e
