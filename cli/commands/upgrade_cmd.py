"""``gco upgrade`` — move a whole GCO deployment to the latest tagged release.

Thin Click veneer over :mod:`cli.upgrade`. The command's job is to make the
blast radius unmistakable before anything happens: it prints the release it
will move to, what happens to the checkout and the local install, and — in
capitals — that every regional stack and the data inside it is destroyed and
recreated. The typed project-name confirmation (skipped by ``-y``) exists
because the command deletes data in every workload Region.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any

import click

from ..config import GCOConfig
from ..output import get_output_formatter, interactive_echo, prompt
from ..upgrade import (
    DEFAULT_DEV_IMAGE,
    DEFAULT_REMOTE,
    UpgradeError,
    UpgradePlan,
    build_plan,
    checkout_release,
    find_checkout_root,
    rebuild_dev_image,
    refresh_node_toolchain,
    refresh_python_install,
    require_container_runtime,
    run_stack_cycle,
)
from .stacks_cmd import _ENABLE_OPTION_HELP, _apply_enable_overrides, _validate_enable

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)

#: Reminder printed with the plan and again after a successful run.
_DATA_WARNING = (
    "Every regional stack is DESTROYED and recreated. EFS and FSx file systems, Valkey, "
    "Aurora, in-cluster volumes (Prometheus, Grafana, MLflow) and, unless retained, the "
    "regional-shared bucket in those Regions are deleted with them. Back that data up to "
    "the cluster-shared bucket first and restore it afterwards — see docs/UPGRADING.md."
)


def _describe_install_steps(
    plan: UpgradePlan, *, skip_checkout: bool, skip_container: bool
) -> list[str]:
    """Human lines for the local half of the plan."""
    lines: list[str] = []
    if skip_checkout:
        lines.append(
            f"Keep the current checkout ({plan.checkout.ref} at {plan.checkout.head}, "
            f"VERSION {plan.checkout.version_file or 'unknown'}) — --skip-checkout"
        )
    elif plan.already_at_target:
        lines.append(
            f"Checkout is already at {plan.target.name} ({plan.checkout.head}); nothing to fetch"
        )
    else:
        lines.append(
            f"git checkout --detach {plan.target.name} in {plan.checkout.root} "
            f"(currently {plan.checkout.ref} at {plan.checkout.head}); cdk.json is preserved byte-for-byte"
        )
        if plan.install.cli_editable_from_checkout:
            lines.append("Refresh the editable CLI install (pip install -e .)")
        else:
            lines.append(
                f"The running gco ({plan.current_version} from {plan.install.cli_path}) is not the "
                "editable install of this checkout — reinstall it yourself after the upgrade"
            )
        if plan.install.node_toolchain:
            lines.append("Refresh the checkout's CDK toolchain (npm ci)")
    if skip_container:
        lines.append("Leave the dev-container image alone — --skip-container")
    elif plan.install.dev_image_present:
        lines.append(
            f"Rebuild the {plan.install.dev_image} container image with {plan.install.container_runtime}"
        )
    else:
        lines.append(f"No local {plan.install.dev_image} image to rebuild")
    return lines


def _render_plan(
    plan: UpgradePlan,
    *,
    skip_checkout: bool,
    skip_container: bool,
    echo: Any,
) -> None:
    """Print the plan through ``echo`` (stdout in table mode, stderr in machine modes)."""
    echo(f"gco upgrade — {plan.current_version} → {plan.target.name} (latest: {plan.latest.name})")
    echo("")
    echo("Local install:")
    for line in _describe_install_steps(
        plan, skip_checkout=skip_checkout, skip_container=skip_container
    ):
        echo(f"  - {line}")
    echo("")
    echo("Stacks (in order):")
    echo("  1. DESTROY the workload tier (regional API bridges, then regional stacks):")
    if plan.workload_stacks:
        for stack in plan.workload_stacks:
            echo(f"       - {stack}")
    else:
        echo("       (no workload Regions are configured — nothing to destroy)")
    echo("  2. Update the control-plane stacks in place: " + ", ".join(plan.control_plane_stacks))
    echo("  3. Recreate the workload tier from cdk.json on the new release")
    echo("")
    echo(f"⚠ {_DATA_WARNING}")
    echo(
        "Expect 45-90 minutes for a single Region; the command is safe to rerun with --skip-checkout."
    )


@click.command("upgrade")
@click.option("--check", is_flag=True, help="Show the plan and the latest release; change nothing")
@click.option("--yes", "-y", is_flag=True, help="Skip the typed confirmation")
@click.option("--ref", metavar="vX.Y.Z", help="Upgrade to this release tag instead of the latest")
@click.option(
    "--remote",
    default=DEFAULT_REMOTE,
    show_default=True,
    help="Git remote whose tags define the releases",
)
@click.option(
    "--skip-checkout",
    is_flag=True,
    help="Keep the current checkout and toolchain; only cycle the stacks",
)
@click.option("--skip-container", is_flag=True, help="Do not rebuild the dev-container image")
@click.option(
    "--image",
    default=DEFAULT_DEV_IMAGE,
    show_default=True,
    help="Dev-container image to rebuild when it exists locally",
)
@click.option(
    "--force",
    is_flag=True,
    help="Cycle the stacks even when the checkout is already at the target release",
)
@click.option(
    "--parallel", "-p", is_flag=True, help="Destroy and recreate regional stacks in parallel"
)
@click.option("--max-workers", "-w", default=4, help="Max parallel stack operations (default: 4)")
@click.option(
    "--enable",
    "enable",
    multiple=True,
    metavar="NAME[,NAME...]",
    callback=_validate_enable,
    help=_ENABLE_OPTION_HELP,
)
@pass_config
def upgrade(
    config: Any,
    check: Any,
    yes: Any,
    ref: Any,
    remote: Any,
    skip_checkout: Any,
    skip_container: Any,
    image: Any,
    force: Any,
    parallel: Any,
    max_workers: Any,
    enable: Mapping[str, str],
) -> None:
    """Upgrade this GCO deployment to the latest tagged release.

    Runs inside a git checkout of the repository and, in one pass:

    \b
      1. fetches the release tags and checks out the latest vMAJOR.MINOR.PATCH
         (cdk.json — your deployment's configuration — is preserved as-is);
      2. refreshes the local install: pip install -e . for an editable CLI,
         npm ci for a checkout-local CDK toolchain, and a rebuild of the
         gco-dev container image when it exists;
      3. scales the workload tier to zero — the monitoring stack is updated
         in place so it stops referencing the regional stacks, then every
         regional API bridge and regional stack is DESTROYED;
      4. updates the global, API Gateway and monitoring stacks in place and
         recreates the regional stacks from cdk.json on the new release.

    Step 3 deletes the data inside the regional stacks (EFS and FSx file
    systems, Valkey, Aurora, in-cluster volumes, the regional-shared bucket
    unless retained). Back it up to the cluster-shared bucket first and
    restore it afterwards: docs/UPGRADING.md walks through the procedure.
    The shared state in the control plane — DynamoDB tables, the model and
    cluster-shared buckets, the image registry, EFS recovery points, cost
    reports — is untouched.

    --check prints the plan without changing anything. --skip-checkout reruns
    only the stack cycle (to finish an interrupted upgrade, or on a fork that
    merged the release itself); --force does the same when the checkout is
    already at the latest release.

    \b
    Examples:
        gco upgrade --check
        gco upgrade
        gco upgrade -y --parallel
        gco upgrade --ref v8.1.0
        gco upgrade --skip-checkout -y
    """
    from ..stacks import get_stack_manager

    formatter = get_output_formatter(config)
    machine = config.output_format != "table"

    try:
        root = find_checkout_root()
        manager = get_stack_manager(config)
        _apply_enable_overrides(formatter, manager, enable)
        stacks = manager.list_stacks()
        plan = build_plan(
            root,
            project_name=config.project_name,
            stacks=stacks,
            remote=remote,
            ref=ref,
            image=image,
            skip_fetch=skip_checkout,
        )
    except UpgradeError as e:
        formatter.print_error(str(e))
        sys.exit(1)
    except Exception as e:
        formatter.print_error(f"Could not prepare the upgrade: {e}")
        sys.exit(1)

    if check:
        if machine:
            formatter.print(
                {
                    "status": "ok",
                    "up_to_date": plan.already_at_target,
                    "plan": plan.to_dict(),
                }
            )
        else:
            _render_plan(
                plan,
                skip_checkout=skip_checkout,
                skip_container=skip_container,
                echo=formatter.print_info,
            )
            if plan.already_at_target:
                formatter.print_success(
                    f"Checkout is already at {plan.target.name}; --check made no changes"
                )
            else:
                formatter.print_info(
                    f"Run 'gco upgrade' to move to {plan.target.name}; --check made no changes"
                )
        return

    if plan.already_at_target and not force and not skip_checkout:
        if machine:
            formatter.print({"status": "up-to-date", "plan": plan.to_dict()})
        else:
            formatter.print_success(
                f"Already at {plan.target.name} — nothing to upgrade. Use --force to run the "
                "stack cycle anyway (for example to finish an interrupted upgrade)."
            )
        return

    if not yes:
        _render_plan(
            plan,
            skip_checkout=skip_checkout,
            skip_container=skip_container,
            echo=interactive_echo,
        )
        answer = prompt(
            f"\nType the project name ({config.project_name}) to confirm",
            default="",
            show_default=False,
        )
        if str(answer).strip() != config.project_name:
            formatter.print_error(
                "Confirmation did not match the project name; nothing was changed."
            )
            sys.exit(1)

    result: dict[str, Any] = {"status": "ok", "plan": plan.to_dict(), "steps": {}}
    steps = result["steps"]
    try:
        runtime = require_container_runtime()

        if skip_checkout:
            steps["checkout"] = {"status": "skipped", "reason": "--skip-checkout"}
        else:
            if plan.already_at_target:
                steps["checkout"] = {"status": "already-at-target", "checked_out": plan.target.name}
            else:
                steps["checkout"] = checkout_release(root, plan.target, log=formatter.print_info)
            if plan.install.cli_editable_from_checkout:
                steps["python"] = refresh_python_install(root, log=formatter.print_info)
            else:
                steps["python"] = {
                    "status": "skipped",
                    "reason": "the running gco is not the editable install of this checkout",
                }
                formatter.print_warning(
                    f"The running gco ({plan.current_version}) is installed from "
                    f"{plan.install.cli_path}, not from this checkout; reinstall it from "
                    f"{plan.target.name} yourself once the upgrade finishes."
                )
            if plan.install.node_toolchain:
                steps["node"] = refresh_node_toolchain(root, log=formatter.print_info)
                if steps["node"].get("status") == "warning":
                    formatter.print_warning(steps["node"]["message"])

        if skip_container:
            steps["container"] = {"status": "skipped", "reason": "--skip-container"}
        elif plan.install.dev_image_present:
            steps["container"] = rebuild_dev_image(
                root, runtime=runtime, image=image, log=formatter.print_info
            )
            if steps["container"].get("status") == "warning":
                formatter.print_warning(steps["container"]["message"])
        else:
            steps["container"] = {
                "status": "skipped",
                "reason": f"image {image} is not present locally",
            }

        # A fresh manager: the checkout may have moved, and the CDK app it
        # synthesizes is now the new release's. Run-scoped --enable overrides
        # ride along exactly as they did for the plan.
        manager = get_stack_manager(config)
        if enable:
            manager.set_extra_cdk_context(dict(enable))

        def on_start(stack_name: str) -> None:
            formatter.print_info(f"  {stack_name}...")

        def on_complete(stack_name: str, success: bool) -> None:
            if success:
                formatter.print_success(f"  ✓ {stack_name}")
            else:
                formatter.print_error(f"  ✗ {stack_name} failed")

        cycle = run_stack_cycle(
            manager,
            parallel=parallel,
            max_workers=max_workers,
            on_stack_start=on_start,
            on_stack_complete=on_complete,
            log=formatter.print_info,
        )
        steps["stacks"] = cycle.to_dict()
    except UpgradeError as e:
        formatter.print_error(str(e))
        sys.exit(1)
    except Exception as e:
        formatter.print_error(f"Upgrade failed: {e}")
        sys.exit(1)

    if not cycle.ok:
        formatter.print_error(
            f"Upgrade stopped during the {cycle.phase_failed} phase; failed stacks: "
            f"{', '.join(cycle.failed) or 'none reported'}. The checkout and local install are "
            f"already on {plan.target.name}; fix the cause and rerun 'gco upgrade --skip-checkout' "
            "to finish the stack cycle."
        )
        sys.exit(1)

    if machine:
        formatter.print(result)
        return

    formatter.print_success(f"Upgrade to {plan.target.name} complete.")
    formatter.print_info(f"  destroyed and recreated: {', '.join(cycle.destroyed) or 'none'}")
    formatter.print_info(f"  deployed: {', '.join(cycle.deployed) or 'none'}")
    formatter.print_info(
        "Restore any data you staged from the regional stacks now (see docs/UPGRADING.md). "
        "Open a new shell so `gco --version` reports the new release."
    )
