"""Stack deployment and management commands."""

import sys
from typing import Any

import click

from ..config import GCOConfig, _load_cdk_json
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


@click.group()
@pass_config
def stacks(config: Any) -> None:
    """Deploy and manage GCO CDK stacks."""
    pass


@stacks.command("list")
@click.option(
    "--refresh",
    is_flag=True,
    help="Compatibility flag; stack discovery already runs live",
)
@pass_config
def list_stacks(config: Any, refresh: Any) -> None:
    """List stacks synthesized by the local CDK app."""
    from ..stacks import get_stack_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_stack_manager(config)
        if refresh:
            formatter.print_info(
                "Stack discovery runs live on every invocation; --refresh is retained "
                "for compatibility."
            )
        local_stacks = manager.list_stacks()

        formatter.print_info("Available CDK stacks:")
        for stack in local_stacks:
            print(f"  - {stack}")

    except Exception as e:
        formatter.print_error(f"Failed to list stacks: {e}")
        sys.exit(1)


@stacks.command("synth")
@click.argument("stack_name", required=False)
@click.option("--quiet", "-q", is_flag=True, default=True, help="Quiet output")
@pass_config
def synth_stack(config: Any, stack_name: Any, quiet: Any) -> None:
    """Synthesize CloudFormation templates."""
    from ..stacks import get_stack_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_stack_manager(config)
        output = manager.synth(stack_name, quiet=quiet)
        if output:
            print(output)
        formatter.print_success("CDK synthesis completed")
    except Exception as e:
        formatter.print_error(f"CDK synth failed: {e}")
        sys.exit(1)


@stacks.command("diff")
@click.argument("stack_name", required=False)
@pass_config
def diff_stack(config: Any, stack_name: Any) -> None:
    """Show differences between deployed and local stacks."""
    from ..stacks import get_stack_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_stack_manager(config)
        diff_output = manager.diff(stack_name)
        if diff_output:
            print(diff_output)
        else:
            formatter.print_success("No differences found")
    except Exception as e:
        formatter.print_error(f"CDK diff failed: {e}")
        sys.exit(1)


@stacks.command("deploy")
@click.argument("stack_name")
@click.option("--yes", "-y", is_flag=True, help="Skip approval prompts")
@click.option("--outputs-file", "-o", help="Write outputs to file")
@click.option("--tag", "-t", multiple=True, help="Add tags (key=value)")
@pass_config
def deploy_stack(config: Any, stack_name: Any, yes: Any, outputs_file: Any, tag: Any) -> None:
    """Deploy a single CDK stack to AWS.

    For deploying all stacks in the correct order, use 'deploy-all'.

    Examples:
        gco stacks deploy gco-us-east-1
        gco stacks deploy gco-global -y
        gco stacks deploy gco-us-east-1 -t Environment=prod
    """
    from ..stacks import get_stack_manager

    formatter = get_output_formatter(config)

    # Parse tags
    tags = {}
    for t in tag:
        if "=" in t:
            k, v = t.split("=", 1)
            tags[k] = v

    try:
        manager = get_stack_manager(config)

        formatter.print_info(f"Deploying {stack_name}...")

        success = manager.deploy(
            stack_name=stack_name,
            require_approval=not yes,
            outputs_file=outputs_file,
            tags=tags if tags else None,
        )

        if success:
            formatter.print_success("Deployment completed successfully")
        else:
            formatter.print_error("Deployment failed")
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"Deployment failed: {e}")
        sys.exit(1)


@stacks.command("destroy")
@click.argument("stack_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def destroy_stack(config: Any, stack_name: Any, yes: Any) -> None:
    """Destroy a single CDK stack.

    For destroying all stacks in the correct order, use 'destroy-all'.

    Examples:
        gco stacks destroy gco-us-east-1
        gco stacks destroy gco-us-east-1 -y
    """
    from ..stacks import get_stack_manager

    formatter = get_output_formatter(config)

    if not yes:
        click.confirm(f"Are you sure you want to destroy {stack_name}?", abort=True)

    try:
        manager = get_stack_manager(config)

        formatter.print_info(f"Destroying {stack_name}...")

        success = manager.destroy(
            stack_name=stack_name,
            force=yes,
        )

        if success:
            formatter.print_success(f"Stack {stack_name} destroyed successfully")
        else:
            formatter.print_error("Destroy failed")
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"Destroy failed: {e}")
        sys.exit(1)


@stacks.command("deploy-all")
@click.option("--yes", "-y", is_flag=True, help="Skip approval prompts")
@click.option("--outputs-file", "-o", help="Write outputs to file")
@click.option("--tag", "-t", multiple=True, help="Add tags (key=value)")
@click.option("--parallel", "-p", is_flag=True, help="Deploy regional stacks in parallel")
@click.option("--max-workers", "-w", default=4, help="Max parallel deployments (default: 4)")
@pass_config
def deploy_all_orchestrated(
    config: Any, yes: Any, outputs_file: Any, tag: Any, parallel: Any, max_workers: Any
) -> None:
    """Deploy all stacks in the correct order.

    Deploys in three phases:
    1. Global stacks (gco-global, gco-api-gateway)
    2. Regional stacks (gco-us-east-1, etc.) - can be parallelized
    3. Monitoring stack (gco-monitoring) - depends on regional stacks

    Use --parallel to deploy regional stacks concurrently, which can
    significantly reduce total deployment time when deploying to
    multiple regions.

    Examples:
        gco stacks deploy-all -y
        gco stacks deploy-all -y --parallel
        gco stacks deploy-all -y -p --max-workers 8
        gco stacks deploy-all -y -t Environment=prod
    """
    from ..stacks import get_stack_manager

    formatter = get_output_formatter(config)

    # Parse tags
    tags = {}
    for t in tag:
        if "=" in t:
            k, v = t.split("=", 1)
            tags[k] = v

    try:
        manager = get_stack_manager(config)
        stacks = manager.list_stacks()

        formatter.print_info(f"Found {len(stacks)} stacks to deploy")
        if parallel:
            formatter.print_info(f"Parallel mode enabled (max workers: {max_workers})")

        def on_start(stack_name: str) -> None:
            formatter.print_info(f"Deploying {stack_name}...")

        def on_complete(stack_name: str, success: bool) -> None:
            if success:
                formatter.print_success(f"  ✓ {stack_name} deployed")
            else:
                formatter.print_error(f"  ✗ {stack_name} failed")

        success, successful, failed = manager.deploy_orchestrated(
            require_approval=not yes,
            outputs_file=outputs_file,
            tags=tags if tags else None,
            on_stack_start=on_start,
            on_stack_complete=on_complete,
            parallel=parallel,
            max_workers=max_workers,
        )

        formatter.print_info("")
        formatter.print_info(f"Deployed: {len(successful)}/{len(stacks)} stacks")

        if success:
            formatter.print_success("All stacks deployed successfully")
        else:
            formatter.print_error(f"Deployment failed. Failed stacks: {', '.join(failed)}")
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"Deployment failed: {e}")
        sys.exit(1)


@stacks.command("destroy-all")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.option("--parallel", "-p", is_flag=True, help="Destroy regional stacks in parallel")
@click.option("--max-workers", "-w", default=4, help="Max parallel destructions (default: 4)")
@pass_config
def destroy_all_orchestrated(config: Any, yes: Any, parallel: Any, max_workers: Any) -> None:
    """Destroy all stacks in the correct order.

    Destroys in four dependency phases:
    1. Monitoring stack (<project>-monitoring)
    2. Regional API bridges (<project>-regional-api-<region>)
    3. Base regional stacks (<project>-<region>) - can be parallelized
    4. Global stacks (<project>-api-gateway, <project>-global)

    Automatically retries up to 3 times (with 30s waits) if any stacks fail,
    which handles transient issues like orphaned resources during teardown.

    Use --parallel to destroy regional stacks concurrently, which can
    significantly reduce total teardown time when destroying multiple
    regional stacks.

    Examples:
        gco stacks destroy-all -y
        gco stacks destroy-all -y --parallel
        gco stacks destroy-all -y -p --max-workers 8
    """
    import time

    from ..stacks import get_stack_destroy_order, get_stack_manager

    formatter = get_output_formatter(config)
    # Retry up to 3 times total. CloudFormation stack deletions can fail
    # transiently — e.g., EKS leaves behind a cluster security group that
    # blocks VPC deletion, but it gets cleaned up async. A 30-second wait
    # between attempts is usually enough for the orphaned resources to clear.
    max_attempts = 3

    try:
        manager = get_stack_manager(config)
        stacks = manager.list_stacks()
        ordered = get_stack_destroy_order(
            stacks,
            project_name=config.project_name,
        )

        if not yes:
            formatter.print_warning("This will destroy ALL GCO stacks:")
            for stack in ordered:
                formatter.print_info(f"  - {stack}")
            click.confirm("\nAre you sure you want to destroy all stacks?", abort=True)

        total_stacks = len(stacks)

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                # Inspect each regional VPC for resources that block teardown
                # (the EKS cluster security group EKS leaves behind, plus any
                # lingering ENIs from ELB / Global Accelerator), clear what's
                # safe to remove, and report what the next attempt is waiting
                # on. The service-managed ENIs drain asynchronously, which is
                # what the 30s wait is for.
                formatter.print_info(
                    "Inspecting VPCs for resources that can block teardown "
                    "(orphaned ENIs, EKS security groups)..."
                )
                manager.cleanup_orphaned_network_interfaces()
                formatter.print_warning(
                    f"Attempt {attempt}/{max_attempts}: waiting 30 seconds before retrying..."
                )
                time.sleep(30)

            formatter.print_info(f"Destroying {len(stacks)} stacks...")
            if parallel:
                formatter.print_info(f"Parallel mode enabled (max workers: {max_workers})")

            def on_start(stack_name: str) -> None:
                formatter.print_info(f"Destroying {stack_name}...")

            def on_complete(stack_name: str, success: bool) -> None:
                if success:
                    formatter.print_success(f"  ✓ {stack_name} destroyed")
                else:
                    formatter.print_error(f"  ✗ {stack_name} failed")

            success, successful, failed = manager.destroy_orchestrated(
                force=True,
                on_stack_start=on_start,
                on_stack_complete=on_complete,
                parallel=parallel,
                max_workers=max_workers,
            )

            if success:
                break

            if attempt < max_attempts:
                formatter.print_warning(f"{len(failed)} stack(s) failed: {', '.join(failed)}")

        formatter.print_info("")
        formatter.print_info(f"Destroyed: {total_stacks - len(failed)}/{total_stacks} stacks")

        if success:
            formatter.print_success("All stacks destroyed successfully")
        else:
            formatter.print_error(f"Some stacks failed to destroy: {', '.join(failed)}")
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"Destroy failed: {e}")
        sys.exit(1)


@stacks.command("bootstrap")
@click.option("--account", "-a", help="AWS account ID")
@click.option("--region", "-r", required=True, help="AWS region")
@pass_config
def bootstrap_cdk(config: Any, account: Any, region: Any) -> None:
    """Bootstrap CDK in an AWS account/region.

    This is required before deploying stacks to a new account/region.

    Example:
        gco stacks bootstrap --region us-east-1
        gco stacks bootstrap -a 123456789012 -r eu-west-1
    """
    from ..stacks import get_stack_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_stack_manager(config)
        formatter.print_info(f"Bootstrapping CDK in {region}...")

        success = manager.bootstrap(account=account, region=region)

        if success:
            formatter.print_success(f"CDK bootstrapped in {region}")
        else:
            formatter.print_error("Bootstrap failed")
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"Bootstrap failed: {e}")
        sys.exit(1)


@stacks.command("status")
@click.argument("stack_name")
@click.option("--region", "-r", required=True, help="AWS region")
@pass_config
def stack_status(config: Any, stack_name: Any, region: Any) -> None:
    """Get detailed status of a deployed stack."""
    from ..stacks import get_stack_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_stack_manager(config)
        status = manager.get_stack_status(stack_name, region)

        if status:
            formatter.print(status.to_dict())
        else:
            formatter.print_error(f"Stack {stack_name} not found in {region}")
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"Failed to get stack status: {e}")
        sys.exit(1)


@stacks.command("outputs")
@click.argument("stack_name")
@click.option("--region", "-r", required=True, help="AWS region")
@pass_config
def stack_outputs(config: Any, stack_name: Any, region: Any) -> None:
    """Get outputs from a deployed stack."""
    from ..stacks import get_stack_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_stack_manager(config)
        outputs = manager.get_outputs(stack_name, region)

        if outputs:
            formatter.print(outputs)
        else:
            formatter.print_warning(f"No outputs found for {stack_name}")

    except Exception as e:
        formatter.print_error(f"Failed to get outputs: {e}")
        sys.exit(1)


@stacks.command("access")
@click.option("--cluster", "-c", help="Cluster name (default: <project_name>-<region>)")
@click.option("--region", "-r", help="AWS region (default: first deployment region)")
@pass_config
def setup_access(config: Any, cluster: Any, region: Any) -> None:
    """Configure kubectl access to a GCO EKS cluster.

    Updates kubeconfig, creates an EKS access entry for your IAM principal,
    and associates the cluster admin policy. Handles assumed roles automatically.

    Examples:
        gco stacks access
        gco stacks access -r us-west-2
        gco stacks access -c my-cluster -r eu-west-1
    """
    import subprocess

    from .._image_uri import aws_partition
    from ..config import _load_cdk_json

    formatter = get_output_formatter(config)

    # Determine region
    if not region:
        cdk_regions = _load_cdk_json()
        if cdk_regions and "regional" in cdk_regions:
            region = cdk_regions["regional"][0]
        else:
            region = config.default_region or "us-east-1"

    partition = aws_partition(str(region))

    # Determine cluster name
    if not cluster:
        cluster = f"{config.project_name}-{region}"

    formatter.print_info(f"Setting up access to cluster: {cluster} in region: {region}")

    # Cluster endpoint access mode — warn early if the API server is
    # private-only, since every kubectl call from outside the VPC will
    # fail. We still try every step so the access entry + policy
    # association land (those use the EKS control plane via boto3,
    # which doesn't go through the cluster endpoint), but the verify
    # step at the end will hit a connection timeout from the laptop.
    private_endpoint_only = False
    public_cidrs: list[str] = []
    try:
        endpoint_check = subprocess.run(
            [
                "aws",
                "eks",
                "describe-cluster",
                "--name",
                cluster,
                "--region",
                region,
                "--query",
                # Explicit ``+`` rather than implicit string concatenation
                # so static analysers don't flag the multi-line literal as
                # a possibly-missing comma between two list elements. The
                # value is one JMESPath expression passed as a single
                # ``--query`` argument.
                "cluster.resourcesVpcConfig.{public:endpointPublicAccess,"
                + "private:endpointPrivateAccess,publicCidrs:publicAccessCidrs}",
                "--output",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        import json

        endpoint_cfg = json.loads(endpoint_check.stdout or "{}")
        is_public = bool(endpoint_cfg.get("public"))
        public_cidrs = endpoint_cfg.get("publicCidrs") or []
        if not is_public:
            private_endpoint_only = True
            formatter.print_warning(
                f"Cluster {cluster!r} has endpointPublicAccess=false — kubectl from "
                "outside the VPC will not be able to reach the API server. The access "
                "entry and policy association below still apply, but the verify step "
                "at the end will time out from this host."
            )
            formatter.print_warning(
                "To enable kubectl from your laptop or CI runner, set "
                '``eks_cluster.endpoint_access`` to ``"PUBLIC_AND_PRIVATE"`` in '
                "``cdk.json`` and redeploy the regional stack: ``gco stacks deploy "
                f"{config.project_name}-{region} -y``."
            )
        elif public_cidrs:
            # Public access is on but restricted to a CIDR allowlist — the
            # caller's IP may or may not be in it.
            formatter.print_info(
                "Cluster API endpoint is public+private with a CIDR allowlist; "
                f"verify your egress IP is covered by one of: {', '.join(public_cidrs)}"
            )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        # Don't block setup if describe-cluster fails — the access steps
        # below may still succeed (e.g. for a brand new cluster the caller
        # already has permission to update).
        formatter.print_info(f"Could not determine endpoint access mode: {exc}")

    try:
        # Step 1: Update kubeconfig
        formatter.print_info("Updating kubeconfig...")
        subprocess.run(
            ["aws", "eks", "update-kubeconfig", "--name", cluster, "--region", region],
            check=True,
            capture_output=True,
            text=True,
        )

        # Step 2: Get IAM principal
        formatter.print_info("Getting your IAM principal...")
        result = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--query", "Arn", "--output", "text"],
            check=True,
            capture_output=True,
            text=True,
        )
        principal_arn = result.stdout.strip()
        formatter.print_info(f"Principal: {principal_arn}")

        # Handle assumed roles — extract the role ARN from the assumed-role ARN
        if ":assumed-role/" in principal_arn:
            import re

            role_name = re.search(r":assumed-role/([^/]+)/", principal_arn)
            if role_name:
                account_result = subprocess.run(
                    [
                        "aws",
                        "sts",
                        "get-caller-identity",
                        "--query",
                        "Account",
                        "--output",
                        "text",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                account_id = account_result.stdout.strip()
                principal_arn = f"arn:{partition}:iam::{account_id}:role/{role_name.group(1)}"
                formatter.print_info(f"Using role ARN: {principal_arn}")

        # Step 3: Create access entry
        formatter.print_info("Creating EKS access entry...")
        try:
            subprocess.run(
                [
                    "aws",
                    "eks",
                    "create-access-entry",
                    "--cluster-name",
                    cluster,
                    "--region",
                    region,
                    "--principal-arn",
                    principal_arn,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            formatter.print_info("Access entry may already exist")

        # Step 4: Associate admin policy
        formatter.print_info("Associating cluster admin policy...")
        try:
            subprocess.run(
                [
                    "aws",
                    "eks",
                    "associate-access-policy",
                    "--cluster-name",
                    cluster,
                    "--region",
                    region,
                    "--principal-arn",
                    principal_arn,
                    "--policy-arn",
                    f"arn:{partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
                    "--access-scope",
                    "type=cluster",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            formatter.print_info("Policy may already be associated")

        # Step 5: Verify access
        formatter.print_info("Waiting for permissions to propagate...")
        import time

        time.sleep(10)

        result = subprocess.run(
            ["kubectl", "get", "nodes", "--request-timeout=10s"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            node_count = len(
                [line for line in result.stdout.strip().split("\n")[1:] if line.strip()]
            )
            print(result.stdout)
            formatter.print_info(f"Access configured successfully. {node_count} node(s) ready.")
        elif private_endpoint_only:
            # Don't double-warn — we already explained this above. Just
            # restate the fix so the operator doesn't have to scroll up.
            formatter.print_warning(
                "kubectl could not reach the API server, as expected for a "
                "private-only cluster from outside the VPC. The IAM access entry "
                "and admin policy association above did succeed, so kubectl will "
                "work from inside the VPC (e.g. SSM Session Manager into a node) "
                "or after redeploying with endpoint_access=PUBLIC_AND_PRIVATE."
            )
        else:
            stderr = (result.stderr or "").strip()
            # When the laptop's egress IP isn't in the CIDR allowlist, AWS
            # returns the API server endpoint but kubectl times out at the
            # TLS handshake. Surface the same actionable hint as the
            # private-only case.
            looks_like_network_block = (
                "i/o timeout" in stderr
                or "no route to host" in stderr
                or "connection refused" in stderr
                or "dial tcp" in stderr
            )
            if looks_like_network_block:
                formatter.print_warning(
                    "kubectl could not reach the API server. If the cluster's "
                    "endpoint_access is restricted to a CIDR allowlist, confirm "
                    "your egress IP is covered, or set endpoint_access to "
                    '"PUBLIC_AND_PRIVATE" in cdk.json and run: gco stacks deploy '
                    f"{config.project_name}-{region} -y"
                )
            else:
                formatter.print_warning(
                    "kubectl connected but no nodes found (cluster may be scaling to zero)"
                )

    except subprocess.CalledProcessError as e:
        formatter.print_error(f"Command failed: {e.stderr or e.stdout or str(e)}")
        sys.exit(1)
    except FileNotFoundError as e:
        formatter.print_error(f"Required tool not found: {e}")
        sys.exit(1)
    except Exception as e:
        formatter.print_error(f"Failed to set up access: {e}")
        sys.exit(1)


@stacks.group("fsx")
@pass_config
def fsx_cmd(config: Any) -> None:
    """Manage FSx for Lustre configuration."""
    pass


@fsx_cmd.command("status")
@click.option("--region", "-r", help="Show config for specific region")
@pass_config
def fsx_status(config: Any, region: Any) -> None:
    """Show current FSx for Lustre configuration status."""
    from ..stacks import get_fsx_config

    formatter = get_output_formatter(config)

    try:
        fsx_config = get_fsx_config(region)
        if region:
            formatter.print_info(f"FSx config for region: {region}")
        else:
            formatter.print_info("Global FSx config:")
        formatter.print(fsx_config)
    except Exception as e:
        formatter.print_error(f"Failed to get FSx config: {e}")
        sys.exit(1)


@fsx_cmd.command("enable")
@click.option("--region", "-r", help="Enable FSx for specific region only")
@click.option("--storage-capacity", "-s", default=1200, help="Storage capacity in GiB (min 1200)")
@click.option(
    "--deployment-type",
    "-d",
    type=click.Choice(["SCRATCH_1", "SCRATCH_2", "PERSISTENT_1", "PERSISTENT_2"]),
    default="SCRATCH_2",
    help="FSx deployment type",
)
@click.option("--throughput", "-t", default=200, help="Per-unit storage throughput (MB/s)")
@click.option("--compression", "-c", type=click.Choice(["LZ4", "NONE"]), default="LZ4")
@click.option("--import-path", help="S3 path for data import (s3://bucket/prefix)")
@click.option("--export-path", help="S3 path for data export (s3://bucket/prefix)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def fsx_enable(
    config: Any,
    region: Any,
    storage_capacity: Any,
    deployment_type: Any,
    throughput: Any,
    compression: Any,
    import_path: Any,
    export_path: Any,
    yes: Any,
) -> None:
    """Enable FSx for Lustre in the stack configuration.

    FSx for Lustre provides high-performance parallel file system storage
    ideal for ML training workloads requiring high throughput and low latency.

    Examples:
        gco stacks fsx enable
        gco stacks fsx enable --region us-east-1
        gco stacks fsx enable --storage-capacity 2400 --deployment-type PERSISTENT_2
        gco stacks fsx enable -r us-west-2 --import-path s3://my-bucket/training-data
    """
    from ..stacks import update_fsx_config

    formatter = get_output_formatter(config)

    if storage_capacity < 1200:
        formatter.print_error("Storage capacity must be at least 1200 GiB")
        sys.exit(1)

    scope = f"region {region}" if region else "all regions (global)"

    if not yes:
        formatter.print_info(f"FSx for Lustre configuration for {scope}:")
        formatter.print_info(f"  Storage Capacity: {storage_capacity} GiB")
        formatter.print_info(f"  Deployment Type: {deployment_type}")
        formatter.print_info(f"  Throughput: {throughput} MB/s per TiB")
        formatter.print_info(f"  Compression: {compression}")
        if import_path:
            formatter.print_info(f"  Import Path: {import_path}")
        if export_path:
            formatter.print_info(f"  Export Path: {export_path}")
        click.confirm(f"\nEnable FSx for Lustre for {scope}?", abort=True)

    try:
        fsx_settings = {
            "enabled": True,
            "storage_capacity_gib": storage_capacity,
            "deployment_type": deployment_type,
            "per_unit_storage_throughput": throughput,
            "data_compression_type": compression,
            "import_path": import_path,
            "export_path": export_path,
            "auto_import_policy": "NEW_CHANGED_DELETED" if import_path else None,
        }

        update_fsx_config(fsx_settings, region)
        formatter.print_success(f"FSx for Lustre enabled in cdk.json for {scope}")
        if region:
            formatter.print_info(
                f"Run 'gco stacks deploy {config.project_name}-{region}' to apply changes"
            )
        else:
            formatter.print_info("Run 'gco stacks deploy' to apply changes")

    except Exception as e:
        formatter.print_error(f"Failed to enable FSx: {e}")
        sys.exit(1)


@fsx_cmd.command("disable")
@click.option("--region", "-r", help="Disable FSx for specific region only")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def fsx_disable(config: Any, region: Any, yes: Any) -> None:
    """Disable FSx for Lustre in the stack configuration.

    Note: This only updates the configuration. Run 'gco stacks deploy'
    to apply changes. Existing FSx file systems will be deleted.

    Examples:
        gco stacks fsx disable
        gco stacks fsx disable --region us-east-1
    """
    from ..stacks import update_fsx_config

    formatter = get_output_formatter(config)

    scope = f"region {region}" if region else "all regions (global)"

    if not yes:
        formatter.print_warning(f"This will disable FSx for Lustre for {scope}.")
        formatter.print_warning("Existing FSx file systems will be deleted on next deploy.")
        click.confirm("Are you sure?", abort=True)

    try:
        update_fsx_config({"enabled": False}, region)
        formatter.print_success(f"FSx for Lustre disabled in cdk.json for {scope}")
        if region:
            formatter.print_info(
                f"Run 'gco stacks deploy {config.project_name}-{region}' to apply changes"
            )
        else:
            formatter.print_info("Run 'gco stacks deploy' to apply changes")

    except Exception as e:
        formatter.print_error(f"Failed to disable FSx: {e}")
        sys.exit(1)


# =============================================================================
# Valkey commands
# =============================================================================


@stacks.group("valkey")
@pass_config
def valkey_cmd(config: Any) -> None:
    """Manage Valkey Serverless cache configuration."""
    pass


@valkey_cmd.command("status")
@pass_config
def valkey_status(config: Any) -> None:
    """Show current Valkey Serverless configuration status."""
    from ..stacks import get_valkey_config

    formatter = get_output_formatter(config)

    try:
        valkey_config = get_valkey_config()
        formatter.print_info("Valkey config:")
        formatter.print(valkey_config)
    except Exception as e:
        formatter.print_error(f"Failed to get Valkey config: {e}")
        sys.exit(1)


@valkey_cmd.command("enable")
@click.option("--max-storage", default=5, help="Max data storage in GB (default: 5)")
@click.option("--max-ecpu", default=5000, help="Max eCPU per second (default: 5000)")
@click.option("--snapshot-retention", default=1, help="Snapshot retention in days (default: 1)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def valkey_enable(
    config: Any,
    max_storage: Any,
    max_ecpu: Any,
    snapshot_retention: Any,
    yes: Any,
) -> None:
    """Enable Valkey Serverless cache in the stack configuration.

    Valkey provides a serverless key-value cache for prompt caching,
    feature stores, session state, and low-latency data access.

    Examples:
        gco stacks valkey enable
        gco stacks valkey enable --max-storage 10 --max-ecpu 10000
    """
    from ..stacks import update_valkey_config

    formatter = get_output_formatter(config)

    if not yes:
        formatter.print_info("Valkey Serverless configuration:")
        formatter.print_info(f"  Max Data Storage: {max_storage} GB")
        formatter.print_info(f"  Max eCPU/second: {max_ecpu}")
        formatter.print_info(f"  Snapshot Retention: {snapshot_retention} days")
        click.confirm("\nEnable Valkey Serverless?", abort=True)

    try:
        valkey_settings = {
            "enabled": True,
            "max_data_storage_gb": max_storage,
            "max_ecpu_per_second": max_ecpu,
            "snapshot_retention_limit": snapshot_retention,
        }

        update_valkey_config(valkey_settings)
        formatter.print_success("Valkey Serverless enabled in cdk.json")
        formatter.print_info("Run 'gco stacks deploy-all -y' to apply changes")

    except Exception as e:
        formatter.print_error(f"Failed to enable Valkey: {e}")
        sys.exit(1)


@valkey_cmd.command("disable")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def valkey_disable(config: Any, yes: Any) -> None:
    """Disable Valkey Serverless cache in the stack configuration.

    Note: This only updates the configuration. Run 'gco stacks deploy-all -y'
    to apply changes. Existing Valkey caches will be deleted.

    Examples:
        gco stacks valkey disable
    """
    from ..stacks import update_valkey_config

    formatter = get_output_formatter(config)

    if not yes:
        formatter.print_warning("This will disable Valkey Serverless.")
        formatter.print_warning("Existing Valkey caches will be deleted on next deploy.")
        click.confirm("Are you sure?", abort=True)

    try:
        update_valkey_config({"enabled": False})
        formatter.print_success("Valkey Serverless disabled in cdk.json")
        formatter.print_info("Run 'gco stacks deploy-all -y' to apply changes")

    except Exception as e:
        formatter.print_error(f"Failed to disable Valkey: {e}")
        sys.exit(1)


# =============================================================================
# Aurora pgvector commands
# =============================================================================


@stacks.group("aurora")
@pass_config
def aurora_cmd(config: Any) -> None:
    """Manage Aurora PostgreSQL (pgvector) configuration."""
    pass


@aurora_cmd.command("status")
@pass_config
def aurora_status(config: Any) -> None:
    """Show current Aurora PostgreSQL (pgvector) configuration status."""
    from ..stacks import get_aurora_config

    formatter = get_output_formatter(config)

    try:
        aurora_config = get_aurora_config()
        formatter.print_info("Aurora pgvector config:")
        formatter.print(aurora_config)
    except Exception as e:
        formatter.print_error(f"Failed to get Aurora config: {e}")
        sys.exit(1)


@aurora_cmd.command("enable")
@click.option("--min-acu", default=0, help="Minimum ACU (0 = scale to zero, default: 0)")
@click.option("--max-acu", default=16, help="Maximum ACU (default: 16)")
@click.option("--backup-retention", default=7, help="Backup retention in days (default: 7)")
@click.option(
    "--deletion-protection/--no-deletion-protection",
    default=False,
    help="Enable deletion protection",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def aurora_enable(
    config: Any,
    min_acu: Any,
    max_acu: Any,
    backup_retention: Any,
    deletion_protection: Any,
    yes: Any,
) -> None:
    """Enable Aurora PostgreSQL with pgvector in the stack configuration.

    Aurora Serverless v2 with pgvector provides vector similarity search
    for RAG applications, semantic search, and embedding storage.

    Examples:
        gco stacks aurora enable
        gco stacks aurora enable --min-acu 2 --max-acu 32 --deletion-protection
    """
    from ..stacks import update_aurora_config

    formatter = get_output_formatter(config)

    if min_acu < 0:
        formatter.print_error("Minimum ACU must be >= 0")
        sys.exit(1)
    if max_acu < 1:
        formatter.print_error("Maximum ACU must be >= 1")
        sys.exit(1)
    if max_acu < min_acu:
        formatter.print_error("Maximum ACU must be >= minimum ACU")
        sys.exit(1)

    if not yes:
        formatter.print_info("Aurora pgvector configuration:")
        formatter.print_info(f"  Min ACU: {min_acu} {'(scale to zero)' if min_acu == 0 else ''}")
        formatter.print_info(f"  Max ACU: {max_acu}")
        formatter.print_info(f"  Backup Retention: {backup_retention} days")
        formatter.print_info(f"  Deletion Protection: {deletion_protection}")
        click.confirm("\nEnable Aurora pgvector?", abort=True)

    try:
        aurora_settings = {
            "enabled": True,
            "min_acu": min_acu,
            "max_acu": max_acu,
            "backup_retention_days": backup_retention,
            "deletion_protection": deletion_protection,
        }

        update_aurora_config(aurora_settings)
        formatter.print_success("Aurora pgvector enabled in cdk.json")
        formatter.print_info("Run 'gco stacks deploy-all -y' to apply changes")

    except Exception as e:
        formatter.print_error(f"Failed to enable Aurora: {e}")
        sys.exit(1)


@aurora_cmd.command("disable")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def aurora_disable(config: Any, yes: Any) -> None:
    """Disable Aurora PostgreSQL (pgvector) in the stack configuration.

    Note: This only updates the configuration. Run 'gco stacks deploy-all -y'
    to apply changes. Existing Aurora clusters will be deleted unless
    deletion protection is enabled.

    Examples:
        gco stacks aurora disable
    """
    from ..stacks import update_aurora_config

    formatter = get_output_formatter(config)

    if not yes:
        formatter.print_warning("This will disable Aurora pgvector.")
        formatter.print_warning(
            "Existing Aurora clusters will be deleted on next deploy "
            "(unless deletion protection is enabled)."
        )
        click.confirm("Are you sure?", abort=True)

    try:
        update_aurora_config({"enabled": False})
        formatter.print_success("Aurora pgvector disabled in cdk.json")
        formatter.print_info("Run 'gco stacks deploy-all -y' to apply changes")

    except Exception as e:
        formatter.print_error(f"Failed to disable Aurora: {e}")
        sys.exit(1)


def _project_name() -> str:
    """Read project_name from cdk.json context (default 'gco')."""
    import json
    from pathlib import Path

    try:
        with open(Path.cwd() / "cdk.json", encoding="utf-8") as f:
            ctx = (json.load(f) or {}).get("context", {})
        return str(ctx.get("project_name") or "gco")
    except OSError, ValueError:
        return "gco"


def _target_regions(config: Any, region: Any, all_regions: bool) -> list[str]:
    """Resolve which regions a command acts on.

    ``--all-regions`` returns every configured regional deployment region;
    otherwise an explicit ``--region``, else the first regional region, else
    the configured default.
    """
    cdk_regions = _load_cdk_json()
    regional = (
        list(cdk_regions["regional"]) if (cdk_regions and cdk_regions.get("regional")) else []
    )

    if all_regions:
        return regional
    if region:
        return [str(region)]
    if regional:
        return [str(regional[0])]
    return [str(config.default_region or "us-east-1")]


@stacks.group("addons")
@pass_config
def addons_cmd(config: Any) -> None:
    """Inspect and re-converge cluster add-ons (Helm charts).

    Add-on installation is decoupled from the CloudFormation rollback path: a
    chart that fails to install never rolls back the cluster. Use these commands
    to see per-chart status and re-run the installer without a full redeploy.
    """
    pass


@addons_cmd.command("status")
@click.option("--region", "-r", help="AWS region (default: first deployment region)")
@click.option("--all-regions", "-A", is_flag=True, help="Show status across all deployment regions")
@pass_config
def addons_status(config: Any, region: Any, all_regions: bool) -> None:
    """Show per-chart add-on install status (from SSM).

    Examples:
        gco stacks addons status
        gco stacks addons status -r us-west-2
        gco stacks addons status --all-regions
    """
    formatter = get_output_formatter(config)
    project = _project_name()
    for target in _target_regions(config, region, all_regions):
        _addons_status_one(formatter, project, target)


def _addons_status_one(formatter: Any, project: str, region: str) -> None:
    """Print the add-on status table for a single region."""
    import json

    import boto3

    prefix = f"/{project}/addons/{region}/"

    try:
        ssm = boto3.client("ssm", region_name=region)
        params: list[dict[str, Any]] = []
        paginator = ssm.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=prefix, Recursive=False):
            params.extend(page.get("Parameters", []))
    except Exception as e:
        formatter.print_error(f"[{region}] Failed to read add-on status from SSM: {e}")
        return

    rows = []
    for p in params:
        name = p["Name"].rsplit("/", 1)[-1]
        if name == "_input":
            continue
        try:
            data = json.loads(p["Value"])
        except ValueError:
            data = {"status": "unknown", "message": p.get("Value", "")}
        rows.append((name, data.get("status", "unknown"), data.get("message", "")[:80]))

    if not rows:
        formatter.print_info(
            f"[{region}] No add-on status recorded under {prefix} yet. "
            "The installer writes status as charts are processed."
        )
        return

    rows.sort()
    formatter.print_info(f"Add-on status for {project} in {region}:")
    for name, status, message in rows:
        line = f"  {name:<28} {status:<12} {message}"
        if status in ("installed", "uninstalled", "absent", "applied"):
            formatter.print_success(line)
        else:
            formatter.print_error(line)


@addons_cmd.command("install")
@click.option("--region", "-r", help="AWS region (default: first deployment region)")
@click.option(
    "--all-regions", "-A", is_flag=True, help="Re-converge add-ons in all deployment regions"
)
@pass_config
def addons_install(config: Any, region: Any, all_regions: bool) -> None:
    """Re-run the Helm add-on installer (idempotent; never rolls back the cluster).

    Replays the last execution input persisted by the deploy, so chart config
    and IAM role wiring stay in one place. Use this to re-converge after a
    transient failure instead of a full stack redeploy.

    Examples:
        gco stacks addons install
        gco stacks addons install -r us-west-2
        gco stacks addons install --all-regions
    """
    formatter = get_output_formatter(config)
    project = _project_name()
    failures = 0
    for target in _target_regions(config, region, all_regions):
        if not _addons_install_one(formatter, project, target):
            failures += 1
    if failures:
        sys.exit(1)


def _decode_addon_replay_input(stored_value: str) -> str:
    """Reverse the helm orchestrator's zlib+base64 replay-input encoding.

    The orchestrator stores the execution input encoded because SSM rejects
    raw ``{{PLACEHOLDER}}`` tokens (see lambda/helm-orchestrator/handler.py).
    A leading ``{`` means a raw legacy JSON value; pass it through unchanged.
    """
    import base64
    import zlib

    if stored_value.lstrip().startswith("{"):
        return stored_value
    compressed = base64.b64decode(stored_value.encode("ascii"), validate=True)
    return zlib.decompress(compressed).decode("utf-8")


def _addons_install_one(formatter: Any, project: str, region: str) -> bool:
    """Start an add-on install for a single region. Returns True on success."""
    import boto3
    from botocore.exceptions import ClientError

    input_param = f"/{project}/addons/{region}/_input"
    fence_param = f"/{project}/addons/{region}/_teardown"

    try:
        ssm = boto3.client("ssm", region_name=region)
        try:
            ssm.get_parameter(Name=fence_param)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ParameterNotFound":
                raise
        else:
            formatter.print_error(
                f"[{region}] Add-on teardown is active ({fence_param}); refusing to start."
            )
            return False
        stored_input = ssm.get_parameter(Name=input_param)["Parameter"]["Value"]
        execution_input = _decode_addon_replay_input(stored_input)
    except Exception as e:
        formatter.print_error(
            f"[{region}] Could not read {input_param}: {e}. "
            f"Deploy the regional stack at least once first (gco stacks deploy {project}-{region} -y)."
        )
        return False

    try:
        sfn = boto3.client("stepfunctions", region_name=region)
        machines = sfn.list_state_machines(maxResults=1000)["stateMachines"]
        arn = next(
            (m["stateMachineArn"] for m in machines if "HelmInstall" in m["name"]),
            None,
        )
        if not arn:
            formatter.print_error(f"[{region}] No HelmInstall state machine found.")
            return False
        resp = sfn.start_execution(stateMachineArn=arn, input=execution_input)
    except Exception as e:
        formatter.print_error(f"[{region}] Failed to start add-on install: {e}")
        return False

    formatter.print_success(f"[{region}] Started add-on install (idempotent re-converge).")
    formatter.print_info(f"  execution: {resp['executionArn']}")
    formatter.print_info(f"  track status with: gco stacks addons status -r {region}")
    return True
