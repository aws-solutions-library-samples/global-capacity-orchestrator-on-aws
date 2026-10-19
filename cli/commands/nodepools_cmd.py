"""NodePool commands."""

import sys
from typing import Any

import click

from ..config import GCOConfig
from ..output import confirm, get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


@click.group()
@pass_config
def nodepools(config: Any) -> None:
    """Manage Karpenter NodePools with ODCR/Capacity Reservation support."""


_CLUSTER_HELP = "EKS cluster name (defaults to <project_name>-<region>)"
_OUTPUT_FILE_HELP = "Write the manifest to this file instead of printing it"


def _write_or_print_manifest(formatter: Any, manifest: str, output_file: str | None) -> None:
    """Print the generated manifest, or write it to ``output_file``, with the apply hint."""
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


@nodepools.command("create-odcr")
@click.option("--name", "-n", required=True, help="NodePool name")
@click.option("--region", "-r", required=True, help="AWS region")
@click.option(
    "--capacity-reservation-id",
    "-c",
    required=True,
    help="EC2 Capacity Reservation ID (cr-xxx)",
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
@click.option("--cluster", help=_CLUSTER_HELP)
@click.option("--output-file", "-o", help=_OUTPUT_FILE_HELP)
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
    cluster: Any,
    output_file: Any,
) -> None:
    """Generate a NodePool backed by an On-Demand Capacity Reservation (ODCR).

    Prints (or writes) an EKS Auto Mode NodeClass that binds the reservation
    through capacityReservationSelectorTerms, using the cluster's own node
    role, subnets and security group, plus a NodePool that launches reserved
    capacity through it. Apply it with kubectl; pods select the pool with
    nodeSelector karpenter.sh/nodepool: <name>.

    Once any NodeClass selects capacity reservations, EKS Auto Mode stops
    launching into open ODCRs on its own for every NodeClass in the cluster.

    Examples:
        gco nodepools create-odcr -n gpu-reserved -r us-east-1 \\
            -c cr-0123456789abcdef0 -i p4d.24xlarge

        gco nodepools create-odcr -n ml-training -r us-west-2 \\
            -c cr-0123456789abcdef0 -i p5.48xlarge --fallback-on-demand

        gco nodepools create-odcr -n efa-training -r us-east-1 \\
            -c cr-0123456789abcdef0 -i p4d.24xlarge --efa

    See: https://docs.aws.amazon.com/eks/latest/userguide/auto-odcr.html
    """
    from ..nodepools import generate_odcr_nodepool_manifest, resolve_auto_mode_node_class_inputs

    formatter = get_output_formatter(config)

    try:
        node_class = resolve_auto_mode_node_class_inputs(
            cluster or f"{config.project_name}-{region}", region
        )
        manifest = generate_odcr_nodepool_manifest(
            name=name,
            region=region,
            capacity_reservation_id=capacity_reservation_id,
            node_class=node_class,
            instance_types=list(instance_type) if instance_type else None,
            max_nodes=max_nodes,
            fallback_on_demand=fallback_on_demand,
            efa=efa,
        )
        _write_or_print_manifest(formatter, manifest, output_file)

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
@click.option("--cluster", help=_CLUSTER_HELP)
@click.option("--output-file", "-o", help=_OUTPUT_FILE_HELP)
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
    cluster: Any,
    output_file: Any,
) -> None:
    """Generate a NodePool backed by a purchased Capacity Block.

    Purchasing a Capacity Block ('gco capacity reserve') yields a normal EC2
    Capacity Reservation ID (cr-xxx). This generates an EKS Auto Mode
    NodeClass that binds it through capacityReservationSelectorTerms and a
    NodePool that launches reserved capacity through it; Auto Mode never uses
    a Capacity Block without such a NodeClass. Because a Capacity Block is
    prepaid for a fixed term, the NodePool defaults to holding that capacity
    rather than consolidating it away.

    Examples:
        gco nodepools create-capacity-block -n cb-train -r us-east-1 \\
            -c cr-0123456789abcdef0 -i p5.48xlarge

        gco nodepools create-capacity-block -n cb-efa -r us-east-1 \\
            -c cr-0123456789abcdef0 -i p4d.24xlarge --efa

    See: https://docs.aws.amazon.com/eks/latest/userguide/auto-odcr.html
    """
    from ..nodepools import (
        generate_capacity_block_nodepool_manifest,
        resolve_auto_mode_node_class_inputs,
    )

    formatter = get_output_formatter(config)

    try:
        node_class = resolve_auto_mode_node_class_inputs(
            cluster or f"{config.project_name}-{region}", region
        )
        manifest = generate_capacity_block_nodepool_manifest(
            name=name,
            region=region,
            capacity_reservation_id=capacity_reservation_id,
            node_class=node_class,
            instance_types=list(instance_type) if instance_type else None,
            max_nodes=max_nodes,
            fallback_on_demand=fallback_on_demand,
            efa=efa,
        )
        _write_or_print_manifest(formatter, manifest, output_file)

    except Exception as e:
        formatter.print_error(f"Failed to create Capacity Block NodePool: {e}")
        sys.exit(1)


@nodepools.command("delete")
@click.argument("nodepool_name")
@click.option("--region", "-r", required=True, help="AWS region")
@click.option("--cluster", help="EKS cluster name (defaults to <project_name>-<region>)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def delete_nodepool(config: Any, nodepool_name: Any, region: Any, cluster: Any, yes: Any) -> None:
    """Delete a NodePool and its paired NodeClass.

    Removes the NodePool and the ``<name>-nodeclass`` NodeClass created with
    it. EKS Auto Mode drains and terminates any nodes the NodePool
    provisioned. Cannot be undone.

    Examples:
        gco nodepools delete gpu-reserved -r us-east-1
        gco nodepools delete gpu-reserved -r us-east-1 -y
    """
    from ..nodepools import delete_cluster_nodepool

    formatter = get_output_formatter(config)

    if not yes:
        confirm(f"Delete NodePool '{nodepool_name}' in {region}?", abort=True)

    try:
        cluster_name = cluster or f"{config.project_name}-{region}"
        deleted = delete_cluster_nodepool(cluster_name, region, nodepool_name)
        formatter.print_success(f"Deleted NodePool {nodepool_name}")
        if deleted.get("nodeclass"):
            formatter.print_info(f"Deleted NodeClass {deleted['nodeclass']}")
    except Exception as e:
        formatter.print_error(f"Failed to delete NodePool: {e}")
        sys.exit(1)


@nodepools.command("list")
@click.option("--region", "-r", help="Filter by region")
@click.option("--cluster", help="EKS cluster name (defaults to <project_name>-<region>)")
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

        cluster_name = cluster or f"{config.project_name}-{region}"
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
@click.option("--cluster", help="EKS cluster name (defaults to <project_name>-<region>)")
@pass_config
def describe_nodepool(config: Any, nodepool_name: Any, region: Any, cluster: Any) -> None:
    """Describe a NodePool.

    Examples:
        gco nodepools describe gpu-x86-pool --region us-east-1
    """
    from ..nodepools import describe_cluster_nodepool

    formatter = get_output_formatter(config)

    try:
        cluster_name = cluster or f"{config.project_name}-{region}"
        nodepool = describe_cluster_nodepool(cluster_name, region, nodepool_name)

        if not nodepool:
            formatter.print_error(f"NodePool {nodepool_name} not found")
            sys.exit(1)

        formatter.print(nodepool)

    except Exception as e:
        formatter.print_error(f"Failed to describe NodePool: {e}")
        sys.exit(1)
