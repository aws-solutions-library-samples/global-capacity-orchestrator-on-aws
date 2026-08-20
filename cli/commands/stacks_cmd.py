"""Stack deployment and management commands."""

import sys
from collections.abc import Mapping, Sequence
from typing import Any

import click

from ..config import GCOConfig, _load_cdk_json
from ..output import get_output_formatter
from ..volume_cleanup import (
    DestroyCommandKind,
    VolumeCleanupRequest,
    VolumePolicyConflictError,
    resolve_volume_cleanup_request,
)
from ..volume_cleanup_reporting import (
    EBS_VOLUME_CLEANUP_NAME,
    EXIT_SUCCESS,
    CleanupFormatter,
    VolumeCleanupCommandResult,
    VolumeCleanupExitReason,
    VolumeCleanupTargetStatus,
    destroy_command_exit_code,
    evaluate_volume_cleanup_result,
    publish_volume_cleanup_outcome,
    render_volume_cleanup_command_result,
    render_volume_cleanup_publication,
    volume_cleanup_publication_from_details,
)

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)

#: Actionable follow-up when the cleanup helper itself could not produce an
#: outcome. The stack result is reported separately and is never rewritten here.
_CLEANUP_UNAVAILABLE_FOLLOW_UP = (
    "The stack result above is unchanged. Re-run the destroy command to retry "
    "EBS volume cleanup; discovery and deletion re-verify every safety condition."
)


def _resolve_destroy_volume_policy(
    *,
    command: DestroyCommandKind,
    retain_volumes: bool,
    delete_volumes: bool,
    yes: bool,
) -> VolumeCleanupRequest:
    """Resolve volume policy and obtain any required irreversible confirmation."""
    try:
        decision = resolve_volume_cleanup_request(
            command=command,
            retain_volumes=retain_volumes,
            delete_volumes=delete_volumes,
            yes=yes,
        )
    except VolumePolicyConflictError as error:
        raise click.UsageError(str(error)) from error

    if decision.requires_volume_confirmation:
        click.confirm(
            "Permanently delete eligible dynamically provisioned EBS volumes? "
            "This is irreversible and cannot be undone.",
            abort=True,
        )
        return decision.confirm_volume_deletion()
    return decision.request


def _cleanup_single_stack_volumes(
    *,
    manager: Any,
    formatter: CleanupFormatter,
    stack_name: str,
    request: VolumeCleanupRequest,
) -> VolumeCleanupCommandResult:
    """Dispose of one destroyed regional stack's volumes and report the result.

    This runs only after ``destroy()`` reported success, which for a single named
    stack means CloudFormation reconciliation already established absence; a retry
    against an already-absent stack reaches the same point and is handled the same
    way. The shared helper resolves the exact regional target, so a non-regional
    stack returns ``None`` and produces no cleanup outcome, no AWS call, and no
    change to the existing exit status.

    The stack result stays separate from the cleanup result: cleanup renders and
    aggregates its own status, and a failure inside cleanup never rewrites the
    stack outcome the operator was already shown.
    """
    try:
        outcome = manager.cleanup_regional_volumes_after_destroy(
            stack_name=stack_name,
            stack_deleted=True,
            request=request,
        )
    except Exception as error:  # noqa: BLE001 - cleanup must not mask the stack result
        formatter.print_error(f"EBS volume cleanup could not complete for {stack_name}: {error}")
        formatter.print_warning(_CLEANUP_UNAVAILABLE_FOLLOW_UP)
        return VolumeCleanupCommandResult(
            targets=(
                VolumeCleanupTargetStatus(
                    stack_name=stack_name,
                    cleanup_successful=False,
                    reporting_successful=False,
                    reasons=(
                        VolumeCleanupExitReason.CLEANUP_FAILED,
                        VolumeCleanupExitReason.REPORTING_INCOMPLETE,
                    ),
                ),
            )
        )

    if outcome is None:
        return VolumeCleanupCommandResult()

    publication = publish_volume_cleanup_outcome(outcome)
    render_volume_cleanup_publication(formatter, publication)
    result = evaluate_volume_cleanup_result([publication])
    render_volume_cleanup_command_result(formatter, result)
    return result


def _report_orchestrated_volume_cleanup(
    *,
    formatter: CleanupFormatter,
    published: Sequence[Mapping[str, Any]],
) -> VolumeCleanupCommandResult:
    """Render and aggregate the outcomes orchestration published for this attempt.

    Orchestrated destruction publishes one complete ``ebs-volumes`` outcome per
    exact regional target through the existing cleanup channel. The command reads
    that published evidence back, so both destroy paths render the same fields and
    derive the same command-level status from the same records.
    """
    publications = [volume_cleanup_publication_from_details(details) for details in published]
    for publication in publications:
        render_volume_cleanup_publication(formatter, publication)
    result = evaluate_volume_cleanup_result(publications)
    render_volume_cleanup_command_result(formatter, result)
    return result


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
@click.option(
    "--retain-volumes",
    is_flag=True,
    help="Retain the cluster's dynamically provisioned EBS volumes after destruction",
)
@click.option(
    "--delete-volumes",
    is_flag=True,
    help="Delete eligible detached, owned EBS volumes after the cluster is gone "
    "(prompts for irreversible confirmation unless -y is given)",
)
@pass_config
def destroy_stack(
    config: Any,
    stack_name: Any,
    yes: Any,
    retain_volumes: Any,
    delete_volumes: Any,
) -> None:
    """Destroy a single CDK stack.

    For destroying all stacks in the correct order, use 'destroy-all'.

    For a regional stack (<project>-<region>), EBS volumes that its EKS cluster
    dynamically provisioned (for example the Prometheus and Alertmanager PVCs)
    can outlive CloudFormation teardown. After the stack is deleted and the
    cluster is confirmed gone, this command discovers those volumes in the
    stack's Region by the exact cluster tag and, by the selected policy, either
    retains or deletes them. Non-regional stacks perform no EBS work.

    Volume policy for single-stack destroy defaults to RETAIN. Deletion is never
    implicit here: pass --delete-volumes to delete eligible volumes (owned,
    available, and detached; anything else is preserved and reported), which
    prompts for an irreversible-data confirmation unless -y is also given.
    --retain-volumes is the explicit non-destructive opposite. Passing both
    --retain-volumes and --delete-volumes is rejected before any action.

    Retained volumes continue to incur EBS storage cost; the command prints a
    warning identifying them and the policy that preserved them.

    Examples:
        gco stacks destroy gco-us-east-1                     # retain volumes
        gco stacks destroy gco-us-east-1 -y                  # retain volumes
        gco stacks destroy gco-us-east-1 --delete-volumes    # prompt, then delete
        gco stacks destroy gco-us-east-1 --delete-volumes -y # delete, no prompt
        gco stacks destroy gco-us-east-1 --retain-volumes -y # explicit retain
    """
    volume_cleanup_request = _resolve_destroy_volume_policy(
        command=DestroyCommandKind.SINGLE,
        retain_volumes=bool(retain_volumes),
        delete_volumes=bool(delete_volumes),
        yes=bool(yes),
    )
    # Cleanup only runs for a reconciled successful deletion below, so a failed
    # stack keeps its existing exit status and performs no EBS work at all.
    volume_cleanup_result = VolumeCleanupCommandResult()

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
            volume_cleanup_result = _cleanup_single_stack_volumes(
                manager=manager,
                formatter=formatter,
                stack_name=str(stack_name),
                request=volume_cleanup_request,
            )
        else:
            formatter.print_error("Destroy failed")

        # Stack failure keeps its existing exit status; volume cleanup can only
        # add an unsuccessful exit for a stack that otherwise succeeded.
        exit_code = destroy_command_exit_code(
            stack_successful=bool(success),
            cleanup=volume_cleanup_result,
        )
        if exit_code != EXIT_SUCCESS:
            sys.exit(exit_code)

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
@click.option(
    "--retain-volumes",
    is_flag=True,
    help="Retain dynamically provisioned EBS volumes; overrides the implicit "
    "delete authorized by 'destroy-all -y'",
)
@click.option(
    "--delete-volumes",
    is_flag=True,
    help="Delete eligible EBS volumes; redundant with 'destroy-all -y', which "
    "already authorizes deletion unless --retain-volumes is given",
)
@click.option("--parallel", "-p", is_flag=True, help="Destroy regional stacks in parallel")
@click.option("--max-workers", "-w", default=4, help="Max parallel destructions (default: 4)")
@pass_config
def destroy_all_orchestrated(
    config: Any,
    yes: Any,
    retain_volumes: Any,
    delete_volumes: Any,
    parallel: Any,
    max_workers: Any,
) -> None:
    """Destroy all stacks in the correct order.

    Destroys in four dependency phases:
    1. Monitoring stack (<project>-monitoring)
    2. Regional API bridges (<project>-regional-api-<region>)
    3. Base regional stacks (<project>-<region>) - can be parallelized
    4. Global stacks (<project>-api-gateway, <project>-global)

    Automatically retries up to 3 times (with 30s waits) if any stacks fail,
    which handles transient issues like orphaned resources during teardown.

    After a fully successful teardown this also purges the runtime
    /{project}/traffic-dial SSM parameters (controller state and manual
    overrides), which are written outside CloudFormation.

    Use --parallel to destroy regional stacks concurrently, which can
    significantly reduce total teardown time when destroying multiple
    regional stacks.

    EBS volume policy: 'gco stacks destroy-all -y' implicitly AUTHORIZES DELETION
    of eligible dynamically provisioned EBS volumes (owned, available, and
    detached) for every regional cluster it destroys, unless --retain-volumes is
    supplied. --delete-volumes is not required and there is no separate volume
    prompt on this path. Pass --retain-volumes to keep the volumes instead; that
    explicit retention overrides the implicit delete. An interactive destroy-all
    (without -y) defaults to retain after the existing stack confirmation.
    Passing both --retain-volumes and --delete-volumes is rejected before any
    action. Only owned, available, detached volumes are deleted; attached,
    non-available, or non-owned tagged volumes are always preserved and reported,
    and retained volumes trigger a continuing-storage-cost warning.

    Examples:
        gco stacks destroy-all -y                       # deletes eligible volumes
        gco stacks destroy-all -y --retain-volumes      # keeps all volumes
        gco stacks destroy-all -y --parallel
        gco stacks destroy-all -y -p --max-workers 8
    """
    volume_cleanup_request = _resolve_destroy_volume_policy(
        command=DestroyCommandKind.ALL,
        retain_volumes=bool(retain_volumes),
        delete_volumes=bool(delete_volumes),
        yes=bool(yes),
    )
    # One resolved policy authorizes every exact regional target of this
    # operation; the barrier inside orchestration publishes one outcome per target.
    volume_cleanup_result = VolumeCleanupCommandResult()

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
        # Each attempt republishes a complete set of target outcomes, so only the
        # outcomes of the attempt that ran last determine the exit status.
        published_volume_cleanups: list[Mapping[str, Any]] = []

        def collect_volume_cleanup(name: str, details: dict[str, Any]) -> None:
            if name == EBS_VOLUME_CLEANUP_NAME:
                published_volume_cleanups.append(details)

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

            published_volume_cleanups.clear()
            success, successful, failed = manager.destroy_orchestrated(
                force=True,
                on_stack_start=on_start,
                on_stack_complete=on_complete,
                parallel=parallel,
                max_workers=max_workers,
                on_cleanup_complete=collect_volume_cleanup,
                volume_cleanup_request=volume_cleanup_request,
            )
            volume_cleanup_result = _report_orchestrated_volume_cleanup(
                formatter=formatter,
                published=published_volume_cleanups,
            )

            if success:
                break

            if attempt < max_attempts:
                if failed:
                    formatter.print_warning(f"{len(failed)} stack(s) failed: {', '.join(failed)}")
                else:
                    formatter.print_warning(
                        "All stacks were deleted but EBS volume cleanup was unsuccessful"
                    )

        formatter.print_info("")
        formatter.print_info(f"Destroyed: {total_stacks - len(failed)}/{total_stacks} stacks")

        if success:
            formatter.print_success("All stacks destroyed successfully")
        elif failed:
            formatter.print_error(f"Some stacks failed to destroy: {', '.join(failed)}")
        else:
            formatter.print_error(
                "All stacks were destroyed but EBS volume cleanup was unsuccessful"
            )

        # Retry semantics above are unchanged; only the final exit status also
        # accounts for the volume-cleanup result of an otherwise successful run.
        exit_code = destroy_command_exit_code(
            stack_successful=bool(success),
            cleanup=volume_cleanup_result,
        )
        if exit_code != EXIT_SUCCESS:
            sys.exit(exit_code)

    except click.Abort:
        # A declined stack confirmation aborts exactly like the single-stack
        # destroy path: let Click emit "Aborted!" and exit non-zero instead of
        # reporting it as a destroy failure.
        raise
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


# =============================================================================
# Deployment-region commands (managed-config engine veneers)
# =============================================================================


@stacks.group("regions")
@pass_config
def regions_cmd(config: Any) -> None:
    """Manage workload deployment Regions in cdk.json.

    These commands edit context.deployment_regions.regional through the
    managed-config engine: validated against the same rules CDK synth
    enforces, atomic, idempotent, and audited. They never deploy — run
    'gco stacks deploy' afterwards to apply the change.
    """
    pass


@regions_cmd.command("list")
@click.option("--config-path", help="Explicit cdk.json to use (default: nearest in cwd/parents)")
@pass_config
def regions_list(config: Any, config_path: Any) -> None:
    """Show the configured deployment-region topology.

    Reports the global/api_gateway/monitoring Regions, the workload Region
    list, the resolved AWS partition, and the cdk.json path backing the
    answer. On a broken configuration, partition_error explains what CDK
    synth would reject.
    """
    from ..managed_config import ManagedConfigError, get_deployment_regions_status

    formatter = get_output_formatter(config)

    try:
        status = get_deployment_regions_status(config_path=config_path)
    except ManagedConfigError as e:
        formatter.print_error(str(e))
        sys.exit(1)
    if config.output_format == "table":
        # The table cell renderer collapses lists to "[N items]"; join for
        # humans. JSON/YAML (the MCP path) keep the real list.
        status["regional"] = ", ".join(status["regional"])
    formatter.print(status)


@regions_cmd.command("add")
@click.argument("region")
@click.option("--config-path", help="Explicit cdk.json to use (default: nearest in cwd/parents)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def regions_add(config: Any, region: Any, config_path: Any, yes: Any) -> None:
    """Add a workload Region to deployment_regions.regional.

    The Region must expose CloudFormation in the AWS SDK's endpoint data and
    belong to the same AWS partition as the already-configured Regions.
    Re-adding a present Region is a reported no-op.

    Examples:
        gco stacks regions add us-west-2
        gco stacks regions add eu-west-1 -y
    """
    from ..managed_config import ManagedConfigError, add_deployment_region

    formatter = get_output_formatter(config)

    if not yes:
        click.confirm(f"Add {region} to deployment_regions.regional in cdk.json?", abort=True)

    try:
        report = add_deployment_region(region, config_path=config_path)
    except ManagedConfigError as e:
        formatter.print_error(str(e))
        sys.exit(1)

    if report.changed:
        formatter.print_success(report.summary())
        formatter.print_info(
            "Config only — no stacks were deployed. "
            f"Run 'gco stacks deploy {config.project_name}-{region}' (or 'gco stacks deploy-all') to apply"
        )
    else:
        formatter.print_info(report.summary())


@regions_cmd.command("remove")
@click.argument("region")
@click.option("--config-path", help="Explicit cdk.json to use (default: nearest in cwd/parents)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def regions_remove(config: Any, region: Any, config_path: Any, yes: Any) -> None:
    """Remove a workload Region from deployment_regions.regional.

    The resulting list must stay valid (at least one Region). Removing an
    absent Region is a reported no-op. Removing an unknown/typo'd entry from
    a hand-edited config is allowed — validation applies to the result, so
    this is also the repair path.

    Examples:
        gco stacks regions remove us-west-2
        gco stacks regions remove xx-typo-1 -y
    """
    from ..managed_config import ManagedConfigError, remove_deployment_region

    formatter = get_output_formatter(config)

    if not yes:
        formatter.print_warning(
            f"This only edits cdk.json — a deployed {config.project_name}-{region} "
            "stack is NOT destroyed by this change."
        )
        click.confirm(f"Remove {region} from deployment_regions.regional in cdk.json?", abort=True)

    try:
        report = remove_deployment_region(region, config_path=config_path)
    except ManagedConfigError as e:
        formatter.print_error(str(e))
        sys.exit(1)

    if report.changed:
        formatter.print_success(report.summary())
        formatter.print_info(
            f"Config only — if {config.project_name}-{region} is deployed, destroy it "
            f"explicitly with 'gco stacks destroy {config.project_name}-{region}'"
        )
    else:
        formatter.print_info(report.summary())


@regions_cmd.command("set")
@click.argument("role", type=click.Choice(["global", "api_gateway", "monitoring"]))
@click.argument("region")
@click.option("--config-path", help="Explicit cdk.json to use (default: nearest in cwd/parents)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def regions_set(config: Any, role: Any, region: Any, config_path: Any, yes: Any) -> None:
    """Set a control-plane Region scalar (global/api_gateway/monitoring).

    The Region must be SDK-known and keep the whole topology (all three
    scalars plus the workload list) in one AWS partition. Setting the
    current value is a reported no-op.

    Examples:
        gco stacks regions set monitoring us-west-2
        gco stacks regions set global us-east-2 -y
    """
    from ..managed_config import ManagedConfigError, set_deployment_region_role

    formatter = get_output_formatter(config)

    if not yes:
        formatter.print_warning(
            "This only edits cdk.json — already-deployed stacks are not moved "
            "or destroyed; the next deploy creates the stack in the new Region."
        )
        click.confirm(f"Set deployment_regions.{role} to {region} in cdk.json?", abort=True)

    try:
        report = set_deployment_region_role(role, region, config_path=config_path)
    except ManagedConfigError as e:
        formatter.print_error(str(e))
        sys.exit(1)

    if report.changed:
        formatter.print_success(report.summary())
        formatter.print_info(
            "Config only — no stacks were deployed. Run 'gco stacks deploy-all' to apply, "
            "and clean up the stack in the previous Region yourself if it was deployed"
        )
    else:
        formatter.print_info(report.summary())


# =============================================================================
# Bedrock model default (managed-config engine veneer)
# =============================================================================


@stacks.group("bedrock")
@pass_config
def bedrock_cmd(config: Any) -> None:
    """Manage the Bedrock model defaults in cdk.json.

    Three independent keys live under context.bedrock, one per consumer:
    mission_default_model_id (Mission sampling), capacity_advisor_default_model_id
    (`gco capacity advise`), and claude_code_default_model_id (the session
    model `gco autopilot` hands to Claude Code). Edits go through the
    managed-config engine: validated, atomic, idempotent, and audited.
    """
    pass


@bedrock_cmd.command("show")
@click.option("--config-path", help="Explicit cdk.json to use (default: nearest in cwd/parents)")
@pass_config
def bedrock_show(config: Any, config_path: Any) -> None:
    """Show every configured Bedrock model default and its backing path."""
    from ..managed_config import ManagedConfigError, get_bedrock_model_status

    formatter = get_output_formatter(config)

    try:
        status = get_bedrock_model_status(config_path=config_path)
    except ManagedConfigError as e:
        formatter.print_error(str(e))
        sys.exit(1)
    formatter.print(status)


@bedrock_cmd.command("set-mission-model")
@click.argument("model_id")
@click.option("--config-path", help="Explicit cdk.json to use (default: nearest in cwd/parents)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def bedrock_set_mission_model(config: Any, model_id: Any, config_path: Any, yes: Any) -> None:
    """Set context.bedrock.mission_default_model_id (Mission sampling).

    This is the default Mission sampling uses; the capacity advisor and
    `gco autopilot` have their own keys (see set-capacity-advisor-model and
    set-claude-code-model). Model and inference-profile IDs are free-form
    (custom profiles, marketplace models), so validation mirrors the runtime
    reader: a non-empty string without surrounding whitespace. Sibling
    settings (bedrock.thinking, the other model keys) are preserved.

    Examples:
        gco stacks bedrock set-mission-model us.amazon.nova-pro-v1:0
        gco stacks bedrock set-mission-model us.amazon.nova-2-lite-v1:0 -y
    """
    from ..managed_config import ManagedConfigError, set_mission_default_model

    formatter = get_output_formatter(config)

    if not yes:
        click.confirm(
            f"Set bedrock.mission_default_model_id to {model_id} in cdk.json?", abort=True
        )

    try:
        report = set_mission_default_model(model_id, config_path=config_path)
    except ManagedConfigError as e:
        formatter.print_error(str(e))
        sys.exit(1)

    if report.changed:
        formatter.print_success(report.summary())
        formatter.print_info(
            "Mission sampling picks this up on its next run; explicit "
            "--bedrock-model-id/env overrides still take precedence"
        )
    else:
        formatter.print_info(report.summary())


@bedrock_cmd.command("set-capacity-advisor-model")
@click.argument("model_id")
@click.option("--config-path", help="Explicit cdk.json to use (default: nearest in cwd/parents)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def bedrock_set_capacity_advisor_model(
    config: Any, model_id: Any, config_path: Any, yes: Any
) -> None:
    """Set context.bedrock.capacity_advisor_default_model_id.

    This is the default `gco capacity advise` (and its historical variant)
    uses; Mission sampling and `gco autopilot` have their own keys (see
    set-mission-model and set-claude-code-model). Model and inference-profile
    IDs are free-form (custom profiles, marketplace models), so validation
    mirrors the runtime reader: a non-empty string without surrounding
    whitespace. Sibling settings (bedrock.thinking, the other model keys)
    are preserved.

    Examples:
        gco stacks bedrock set-capacity-advisor-model us.amazon.nova-pro-v1:0
        gco stacks bedrock set-capacity-advisor-model us.amazon.nova-2-lite-v1:0 -y
    """
    from ..managed_config import ManagedConfigError, set_capacity_advisor_default_model

    formatter = get_output_formatter(config)

    if not yes:
        click.confirm(
            f"Set bedrock.capacity_advisor_default_model_id to {model_id} in cdk.json?",
            abort=True,
        )

    try:
        report = set_capacity_advisor_default_model(model_id, config_path=config_path)
    except ManagedConfigError as e:
        formatter.print_error(str(e))
        sys.exit(1)

    if report.changed:
        formatter.print_success(report.summary())
        formatter.print_info(
            "The capacity advisor picks this up on its next run; explicit "
            "--model overrides still take precedence"
        )
    else:
        formatter.print_info(report.summary())


@bedrock_cmd.command("set-claude-code-model")
@click.argument("model_id")
@click.option("--config-path", help="Explicit cdk.json to use (default: nearest in cwd/parents)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def bedrock_set_claude_code_model(config: Any, model_id: Any, config_path: Any, yes: Any) -> None:
    """Set context.bedrock.claude_code_default_model_id.

    This is the session model `gco autopilot` hands to Claude Code, kept
    separate from the generation defaults (see set-mission-model and
    set-capacity-advisor-model) so repointing the interactive agent never
    repoints Mission sampling or the capacity advisor. Validation mirrors the runtime reader: a non-empty string
    without surrounding whitespace. Sibling settings are preserved.

    Examples:
        gco stacks bedrock set-claude-code-model us.anthropic.claude-sonnet-4-6
        gco stacks bedrock set-claude-code-model us.anthropic.claude-opus-4-7 -y
    """
    from ..managed_config import ManagedConfigError, set_claude_code_default_model

    formatter = get_output_formatter(config)

    if not yes:
        click.confirm(
            f"Set bedrock.claude_code_default_model_id to {model_id} in cdk.json?",
            abort=True,
        )

    try:
        report = set_claude_code_default_model(model_id, config_path=config_path)
    except ManagedConfigError as e:
        formatter.print_error(str(e))
        sys.exit(1)

    if report.changed:
        formatter.print_success(report.summary())
        formatter.print_info(
            "New autopilot sessions pick this up at launch; explicit "
            "--model/GCO_AUTOPILOT_MODEL overrides still take precedence"
        )
    else:
        formatter.print_info(report.summary())


# =============================================================================
# FSx commands
# =============================================================================


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
