"""NodePool commands."""

import sys
from typing import Any

import click

from ..config import GCOConfig
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


@click.group()
@pass_config
def nodepools(config: Any) -> None:
    """Manage Karpenter NodePools with ODCR/Capacity Reservation support."""
    pass


@nodepools.command("create-odcr")
@click.option("--name", "-n", required=True, help="NodePool name")
@click.option("--region", "-r", required=True, help="AWS region")
@click.option(
    "--capacity-reservation-id",
    "-c",
    required=True,
    help="EC2 Capacity Reservation ID (cr-xxx) or ODCR group ARN",
)
@click.option(
    "--instance-type",
    "-i",
    multiple=True,
    help="Instance types (can specify multiple)",
)
@click.option("--max-nodes", default=100, help="Maximum nodes in pool")
@click.option("--fallback-on-demand", is_flag=True, help="Fall back to on-demand if ODCR exhausted")
@click.option(
    "--efa",
    is_flag=True,
    help="Enable EFA support (adds EFA taint and labels for p4d/p5 instances)",
)
@click.option("--output-file", "-o", help="Output manifest to file instead of applying")
@pass_config
def create_odcr_nodepool(
    config: Any,
    name: Any,
    region: Any,
    capacity_reservation_id: Any,
    instance_type: Any,
    max_nodes: Any,
    fallback_on_demand: Any,
    efa: Any,
    output_file: Any,
) -> None:
    """Create a NodePool backed by an On-Demand Capacity Reservation (ODCR).

    This creates a Karpenter NodePool and EC2NodeClass configured to use
    a specific capacity reservation for guaranteed capacity.

    Examples:
        gco nodepools create-odcr -n gpu-reserved -r us-east-1 \\
            -c cr-0123456789abcdef0 -i p4d.24xlarge

        gco nodepools create-odcr -n ml-training -r us-west-2 \\
            -c cr-0123456789abcdef0 -i p5.48xlarge --fallback-on-demand

        gco nodepools create-odcr -n efa-training -r us-east-1 \\
            -c cr-0123456789abcdef0 -i p4d.24xlarge --efa

    See: https://karpenter.sh/docs/tasks/odcrs/
    """
    from ..nodepools import generate_odcr_nodepool_manifest

    formatter = get_output_formatter(config)

    try:
        manifest = generate_odcr_nodepool_manifest(
            name=name,
            region=region,
            capacity_reservation_id=capacity_reservation_id,
            instance_types=list(instance_type) if instance_type else None,
            max_nodes=max_nodes,
            fallback_on_demand=fallback_on_demand,
            efa=efa,
        )

        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(manifest)
            formatter.print_success(f"NodePool manifest written to {output_file}")
            formatter.print_info(f"Apply with: kubectl apply -f {output_file}")
        else:
            # Print the manifest for review
            print(manifest)
            formatter.print_info("\nTo apply this manifest, save it to a file and run:")
            formatter.print_info("  kubectl apply -f <filename>.yaml")

    except Exception as e:
        formatter.print_error(f"Failed to create ODCR NodePool: {e}")
        sys.exit(1)


@nodepools.command("create-capacity-block")
@click.option("--name", "-n", required=True, help="NodePool name")
@click.option("--region", "-r", required=True, help="AWS region")
@click.option(
    "--capacity-reservation-id",
    "-c",
    required=True,
    help="Capacity Reservation ID (cr-xxx) of the purchased Capacity Block",
)
@click.option(
    "--instance-type",
    "-i",
    multiple=True,
    help="Instance types (can specify multiple)",
)
@click.option("--max-nodes", default=100, help="Maximum nodes in pool")
@click.option(
    "--fallback-on-demand",
    is_flag=True,
    help="Fall back to on-demand when the block is exhausted/expired",
)
@click.option(
    "--efa",
    is_flag=True,
    help="Enable EFA support (adds EFA taint and labels for p4d/p5 instances)",
)
@click.option("--output-file", "-o", help="Output manifest to file instead of applying")
@pass_config
def create_capacity_block_nodepool(
    config: Any,
    name: Any,
    region: Any,
    capacity_reservation_id: Any,
    instance_type: Any,
    max_nodes: Any,
    fallback_on_demand: Any,
    efa: Any,
    output_file: Any,
) -> None:
    """Create a NodePool backed by a purchased Capacity Block.

    Purchasing a Capacity Block ('gco capacity reserve') yields a normal EC2
    Capacity Reservation ID (cr-xxx). This generates a Karpenter NodePool and
    EC2NodeClass that consume it via capacityReservationSelectorTerms. Because a
    Capacity Block is prepaid for a fixed term, the NodePool defaults to holding
    that capacity rather than consolidating it away.

    Examples:
        gco nodepools create-capacity-block -n cb-train -r us-east-1 \\
            -c cr-0123456789abcdef0 -i p5.48xlarge

        gco nodepools create-capacity-block -n cb-efa -r us-east-1 \\
            -c cr-0123456789abcdef0 -i p4d.24xlarge --efa

    See: https://karpenter.sh/docs/concepts/nodeclasses/
    """
    from ..nodepools import generate_capacity_block_nodepool_manifest

    formatter = get_output_formatter(config)

    try:
        manifest = generate_capacity_block_nodepool_manifest(
            name=name,
            region=region,
            capacity_reservation_id=capacity_reservation_id,
            instance_types=list(instance_type) if instance_type else None,
            max_nodes=max_nodes,
            fallback_on_demand=fallback_on_demand,
            efa=efa,
        )

        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(manifest)
            formatter.print_success(f"NodePool manifest written to {output_file}")
            formatter.print_info(f"Apply with: kubectl apply -f {output_file}")
        else:
            print(manifest)
            formatter.print_info("\nTo apply this manifest, save it to a file and run:")
            formatter.print_info("  kubectl apply -f <filename>.yaml")

    except Exception as e:
        formatter.print_error(f"Failed to create Capacity Block NodePool: {e}")
        sys.exit(1)


@nodepools.command("list")
@click.option("--region", "-r", help="Filter by region")
@click.option("--cluster", help="EKS cluster name (defaults to gco-<region>)")
@pass_config
def list_nodepools(config: Any, region: Any, cluster: Any) -> None:
    """List NodePools in the cluster.

    Examples:
        gco nodepools list --region us-east-1
        gco nodepools list --cluster my-cluster
    """
    from ..nodepools import list_cluster_nodepools

    formatter = get_output_formatter(config)

    try:
        if not region and not cluster:
            formatter.print_error("Either --region or --cluster is required")
            sys.exit(1)

        cluster_name = cluster or f"gco-{region}"
        nodepools_list = list_cluster_nodepools(cluster_name, region or config.default_region)

        if not nodepools_list:
            formatter.print_info("No NodePools found")
            return

        formatter.print(nodepools_list)

    except Exception as e:
        formatter.print_error(f"Failed to list NodePools: {e}")
        sys.exit(1)


@nodepools.command("describe")
@click.argument("nodepool_name")
@click.option("--region", "-r", required=True, help="AWS region")
@click.option("--cluster", help="EKS cluster name (defaults to gco-<region>)")
@pass_config
def describe_nodepool(config: Any, nodepool_name: Any, region: Any, cluster: Any) -> None:
    """Describe a NodePool.

    Examples:
        gco nodepools describe gpu-x86-pool --region us-east-1
    """
    from ..nodepools import describe_cluster_nodepool

    formatter = get_output_formatter(config)

    try:
        cluster_name = cluster or f"gco-{region}"
        nodepool = describe_cluster_nodepool(cluster_name, region, nodepool_name)

        if not nodepool:
            formatter.print_error(f"NodePool {nodepool_name} not found")
            sys.exit(1)

        formatter.print(nodepool)

    except Exception as e:
        formatter.print_error(f"Failed to describe NodePool: {e}")
        sys.exit(1)
