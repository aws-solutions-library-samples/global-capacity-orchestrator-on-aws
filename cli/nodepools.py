"""
NodePool management utilities for GCO CLI.

Provides functionality to create and manage Karpenter NodePools with
support for On-Demand Capacity Reservations (ODCRs) and Capacity Blocks.

Key Features:
- Generate NodePool manifests for ODCR-backed capacity
- List and describe NodePools in EKS clusters
- Support for fallback to on-demand when ODCR is exhausted

See: https://karpenter.sh/docs/tasks/odcrs/
"""

import base64
import logging
from dataclasses import dataclass
from typing import Any

import boto3
import yaml
from kubernetes.client import CustomObjectsApi

logger = logging.getLogger(__name__)

# Default vCPU count when instance type lookup fails (conservative estimate)
DEFAULT_VCPUS_PER_NODE = 96


def get_vcpus_for_instance_type(instance_type: str, region: str = "us-east-1") -> int:
    """
    Get the vCPU count for an instance type from EC2 API.

    Args:
        instance_type: EC2 instance type (e.g., "p4d.24xlarge")
        region: AWS region for API calls

    Returns:
        Number of vCPUs for the instance type, or DEFAULT_VCPUS_PER_NODE if lookup fails
    """
    try:
        ec2 = boto3.client("ec2", region_name=region)
        response = ec2.describe_instance_types(InstanceTypes=[instance_type])
        if response["InstanceTypes"]:
            return int(response["InstanceTypes"][0]["VCpuInfo"]["DefaultVCpus"])
    except Exception as e:
        logger.debug("Failed to get vCPU count for %s: %s", instance_type, e)

    return DEFAULT_VCPUS_PER_NODE


def calculate_cpu_limit(
    instance_types: list[str] | None, max_nodes: int, region: str = "us-east-1"
) -> int:
    """
    Calculate the CPU limit for a NodePool based on instance types.

    If multiple instance types are specified, uses the maximum vCPU count
    to ensure the limit can accommodate the largest instances.

    Args:
        instance_types: List of instance types (None means any)
        max_nodes: Maximum number of nodes in the pool
        region: AWS region for API calls

    Returns:
        Total CPU limit (max_nodes * max_vcpus_per_instance)
    """
    if not instance_types:
        # No specific instance types - use conservative default
        return max_nodes * DEFAULT_VCPUS_PER_NODE

    # Get vCPU count for each instance type and use the maximum
    vcpu_counts = [get_vcpus_for_instance_type(it, region) for it in instance_types]
    max_vcpus = max(vcpu_counts) if vcpu_counts else DEFAULT_VCPUS_PER_NODE

    return max_nodes * max_vcpus


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


def generate_odcr_nodepool_manifest(
    name: str,
    region: str,
    capacity_reservation_id: str,
    instance_types: list[str] | None = None,
    max_nodes: int = 100,
    fallback_on_demand: bool = False,
    efa: bool = False,
) -> str:
    """
    Generate a Karpenter NodePool manifest for ODCR-backed capacity.

    Args:
        name: Name for the NodePool
        region: AWS region
        capacity_reservation_id: EC2 Capacity Reservation ID (cr-xxx) or ODCR group ARN
        instance_types: List of instance types (if None, uses ODCR's instance type)
        max_nodes: Maximum number of nodes
        fallback_on_demand: Whether to fall back to on-demand if ODCR exhausted
        efa: Whether to enable EFA support (adds EFA taint and labels)

    Returns:
        YAML manifest string for the NodePool and EC2NodeClass
    """
    # Determine capacity types based on fallback setting
    capacity_types = ["reserved", "on-demand"] if fallback_on_demand else ["reserved"]

    # Build the EC2NodeClass with capacity reservation selector
    ec2_node_class = {
        "apiVersion": "karpenter.k8s.aws/v1",
        "kind": "EC2NodeClass",
        "metadata": {
            "name": f"{name}-nodeclass",
            "labels": {
                "app.kubernetes.io/part-of": "gco",
                "gco.io/nodepool": name,
            },
        },
        "spec": {
            "role": "KarpenterNodeRole-gco",
            "subnetSelectorTerms": [{"tags": {"karpenter.sh/discovery": f"gco-{region}"}}],
            "securityGroupSelectorTerms": [{"tags": {"karpenter.sh/discovery": f"gco-{region}"}}],
            "capacityReservationSelectorTerms": [{"id": capacity_reservation_id}],
            "tags": {
                "Name": f"gco-{name}",
                "gco.io/nodepool": name,
                "gco.io/capacity-reservation": capacity_reservation_id,
            },
        },
    }

    # Build requirements list
    requirements: list[dict[str, Any]] = [
        {
            "key": "karpenter.sh/capacity-type",
            "operator": "In",
            "values": capacity_types,
        },
        {
            "key": "kubernetes.io/arch",
            "operator": "In",
            "values": ["amd64"],
        },
    ]

    # Build the NodePool
    nodepool: dict[str, Any] = {
        "apiVersion": "karpenter.sh/v1",
        "kind": "NodePool",
        "metadata": {
            "name": name,
            "labels": {
                "app.kubernetes.io/part-of": "gco",
            },
        },
        "spec": {
            "template": {
                "metadata": {
                    "labels": {
                        "workload-type": "reserved-capacity",
                        "project": "gco",
                        "gco.io/capacity-reservation": capacity_reservation_id,
                    },
                },
                "spec": {
                    "nodeClassRef": {
                        "group": "karpenter.k8s.aws",
                        "kind": "EC2NodeClass",
                        "name": f"{name}-nodeclass",
                    },
                    "requirements": requirements,
                },
            },
            "limits": {
                "cpu": str(calculate_cpu_limit(instance_types, max_nodes, region)),
            },
            "disruption": {
                "consolidationPolicy": "WhenEmptyOrUnderutilized",
                "consolidateAfter": "30s",
                "budgets": [{"nodes": "10%"}],
            },
        },
    }

    # Add instance type requirements if specified
    if instance_types:
        requirements.append(
            {
                "key": "node.kubernetes.io/instance-type",
                "operator": "In",
                "values": instance_types,
            }
        )

    # Add GPU taints for GPU instances
    gpu_families = ["p3", "p4", "p5", "p6", "g4", "g5", "g6"]
    if instance_types and any(
        any(it.startswith(fam) for fam in gpu_families) for it in instance_types
    ):
        taints = [
            {
                "key": "nvidia.com/gpu",
                "value": "true",
                "effect": "NoSchedule",
            }
        ]
        if efa:
            taints.append(
                {
                    "key": "vpc.amazonaws.com/efa",
                    "value": "true",
                    "effect": "NoSchedule",
                }
            )
        nodepool["spec"]["template"]["spec"]["taints"] = taints

    # Add EFA labels
    if efa:
        nodepool["spec"]["template"]["metadata"]["labels"]["efa"] = "true"
        nodepool["spec"]["template"]["metadata"]["labels"]["workload-type"] = "gpu-efa"
        # Use WhenEmpty consolidation for EFA workloads to avoid disrupting
        # long-running distributed training jobs
        nodepool["spec"]["disruption"] = {
            "consolidationPolicy": "WhenEmpty",
            "consolidateAfter": "300s",
            "budgets": [{"nodes": "10%"}],
        }

    # Generate YAML output
    output = []
    output.append("# ODCR-backed NodePool for GCO")
    output.append(f"# Capacity Reservation: {capacity_reservation_id}")
    output.append(f"# Region: {region}")
    if fallback_on_demand:
        output.append("# Fallback: on-demand (when ODCR exhausted)")
    output.append("#")
    output.append("# Apply with: kubectl apply -f <this-file>.yaml")
    output.append("# See: https://karpenter.sh/docs/tasks/odcrs/")
    output.append("---")
    output.append(yaml.dump(ec2_node_class, default_flow_style=False, sort_keys=False))
    output.append("---")
    output.append(yaml.dump(nodepool, default_flow_style=False, sort_keys=False))

    return "\n".join(output)


def generate_capacity_block_nodepool_manifest(
    name: str,
    region: str,
    capacity_reservation_id: str,
    instance_types: list[str] | None = None,
    max_nodes: int = 100,
    fallback_on_demand: bool = False,
    efa: bool = False,
) -> str:
    """
    Generate a Karpenter NodePool manifest for Capacity Block-backed capacity.

    The Capacity Block counterpart to :func:`generate_odcr_nodepool_manifest`.
    Purchasing a Capacity Block ("gco capacity reserve") yields a normal EC2
    Capacity Reservation id (``cr-...``), which Karpenter consumes through the
    same ``capacityReservationSelectorTerms`` / ``reserved`` capacity type as an
    ODCR. The difference is intent: a Capacity Block is prepaid for a fixed term,
    so the generated NodePool defaults to holding that capacity (``WhenEmpty``
    consolidation with a long delay) rather than consolidating aggressively — you
    have already paid for the whole block and want it available for its duration.

    Args:
        name: Name for the NodePool.
        region: AWS region.
        capacity_reservation_id: Capacity Reservation ID (cr-xxx) of the purchased
            Capacity Block (from ``gco capacity reserve`` / reservation-check).
        instance_types: List of instance types (if None, uses the reservation's type).
        max_nodes: Maximum number of nodes.
        fallback_on_demand: Whether to fall back to on-demand when the block is
            exhausted or after it expires. Off by default — a Capacity Block is
            fixed-term guaranteed capacity, so silent on-demand fallback can
            surprise-bill; opt in explicitly.
        efa: Whether to enable EFA support (adds EFA taint and labels).

    Returns:
        YAML manifest string for the NodePool and EC2NodeClass.
    """
    capacity_types = ["reserved", "on-demand"] if fallback_on_demand else ["reserved"]

    ec2_node_class = {
        "apiVersion": "karpenter.k8s.aws/v1",
        "kind": "EC2NodeClass",
        "metadata": {
            "name": f"{name}-nodeclass",
            "labels": {
                "app.kubernetes.io/part-of": "gco",
                "gco.io/nodepool": name,
            },
        },
        "spec": {
            "role": "KarpenterNodeRole-gco",
            "subnetSelectorTerms": [{"tags": {"karpenter.sh/discovery": f"gco-{region}"}}],
            "securityGroupSelectorTerms": [{"tags": {"karpenter.sh/discovery": f"gco-{region}"}}],
            "capacityReservationSelectorTerms": [{"id": capacity_reservation_id}],
            "tags": {
                "Name": f"gco-{name}",
                "gco.io/nodepool": name,
                "gco.io/capacity-block": capacity_reservation_id,
            },
        },
    }

    requirements: list[dict[str, Any]] = [
        {
            "key": "karpenter.sh/capacity-type",
            "operator": "In",
            "values": capacity_types,
        },
        {
            "key": "kubernetes.io/arch",
            "operator": "In",
            "values": ["amd64"],
        },
    ]

    nodepool: dict[str, Any] = {
        "apiVersion": "karpenter.sh/v1",
        "kind": "NodePool",
        "metadata": {
            "name": name,
            "labels": {
                "app.kubernetes.io/part-of": "gco",
            },
        },
        "spec": {
            "template": {
                "metadata": {
                    "labels": {
                        "workload-type": "capacity-block",
                        "project": "gco",
                        "gco.io/capacity-type": "capacity-block",
                        "gco.io/capacity-block": capacity_reservation_id,
                    },
                },
                "spec": {
                    "nodeClassRef": {
                        "group": "karpenter.k8s.aws",
                        "kind": "EC2NodeClass",
                        "name": f"{name}-nodeclass",
                    },
                    "requirements": requirements,
                },
            },
            "limits": {
                "cpu": str(calculate_cpu_limit(instance_types, max_nodes, region)),
            },
            # A Capacity Block is prepaid for a fixed term — hold the nodes rather
            # than consolidating them away, so the paid capacity stays available
            # for the whole block.
            "disruption": {
                "consolidationPolicy": "WhenEmpty",
                "consolidateAfter": "600s",
                "budgets": [{"nodes": "10%"}],
            },
        },
    }

    if instance_types:
        requirements.append(
            {
                "key": "node.kubernetes.io/instance-type",
                "operator": "In",
                "values": instance_types,
            }
        )

    gpu_families = ["p3", "p4", "p5", "p6", "g4", "g5", "g6"]
    if instance_types and any(
        any(it.startswith(fam) for fam in gpu_families) for it in instance_types
    ):
        taints = [
            {
                "key": "nvidia.com/gpu",
                "value": "true",
                "effect": "NoSchedule",
            }
        ]
        if efa:
            taints.append(
                {
                    "key": "vpc.amazonaws.com/efa",
                    "value": "true",
                    "effect": "NoSchedule",
                }
            )
        nodepool["spec"]["template"]["spec"]["taints"] = taints

    if efa:
        nodepool["spec"]["template"]["metadata"]["labels"]["efa"] = "true"
        nodepool["spec"]["template"]["metadata"]["labels"]["workload-type"] = "gpu-efa"

    output = []
    output.append("# Capacity Block-backed NodePool for GCO")
    output.append(f"# Capacity Reservation (from Capacity Block): {capacity_reservation_id}")
    output.append(f"# Region: {region}")
    if fallback_on_demand:
        output.append("# Fallback: on-demand (when Capacity Block exhausted/expired)")
    output.append("#")
    output.append("# Apply with: kubectl apply -f <this-file>.yaml")
    output.append(
        "# See: https://karpenter.sh/docs/concepts/nodeclasses/#speccapacityreservationselectorterms"
    )
    output.append("---")
    output.append(yaml.dump(ec2_node_class, default_flow_style=False, sort_keys=False))
    output.append("---")
    output.append(yaml.dump(nodepool, default_flow_style=False, sort_keys=False))

    return "\n".join(output)


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


def get_k8s_client(cluster_name: str, region: str) -> CustomObjectsApi:
    """Get configured Kubernetes client for EKS cluster."""
    from kubernetes import client

    eks = boto3.client("eks", region_name=region)
    cluster_info = eks.describe_cluster(name=cluster_name)
    cluster = cluster_info["cluster"]

    configuration = client.Configuration()
    configuration.host = cluster["endpoint"]
    configuration.verify_ssl = True

    # Decode CA certificate using secure tempfile method
    ca_cert = base64.b64decode(cluster["certificateAuthority"]["data"])
    import os
    import tempfile

    fd, ca_cert_path = tempfile.mkstemp(suffix=".crt")
    try:
        with os.fdopen(fd, "wb") as ca_file:
            ca_file.write(ca_cert)
            ca_file.flush()
        configuration.ssl_ca_cert = ca_cert_path
    except Exception:
        os.close(fd)
        raise

    # Generate EKS token
    eks_token = get_eks_token(cluster_name, region)
    configuration.api_key = {"authorization": f"Bearer {eks_token}"}

    # Create API client with the configuration explicitly
    api_client = client.ApiClient(configuration)
    return client.CustomObjectsApi(api_client)


def list_cluster_nodepools(cluster_name: str, region: str) -> list[dict[str, Any]]:
    """
    List NodePools in an EKS cluster.

    Args:
        cluster_name: EKS cluster name
        region: AWS region

    Returns:
        List of NodePool information dictionaries
    """
    try:
        custom_api = get_k8s_client(cluster_name, region)

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
    cluster_name: str, region: str, nodepool_name: str
) -> dict[str, Any] | None:
    """
    Describe a specific NodePool in an EKS cluster.

    Args:
        cluster_name: EKS cluster name
        region: AWS region
        nodepool_name: Name of the NodePool

    Returns:
        NodePool details or None if not found
    """
    try:
        custom_api = get_k8s_client(cluster_name, region)

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
