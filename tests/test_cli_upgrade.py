"""Tests for ``gco upgrade`` (cli/commands/upgrade_cmd.py).

The engine (:mod:`cli.upgrade`) is faked at its module seams, so these tests
pin the command's contract: the plan it shows, the typed confirmation, which
engine steps run under which flags, how failures surface (exit code 1, stderr
text, nothing else changed), and the single JSON/YAML document a machine caller
receives.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from cli.config import GCOConfig
from cli.upgrade import (
    CheckoutState,
    InstallProbe,
    ReleaseTag,
    StackCycleResult,
    UpgradeError,
    UpgradePlan,
)

ROOT = Path("/repo")


def _config(output_format: str = "table") -> GCOConfig:
    return GCOConfig(project_name="gco", default_region="us-east-1", output_format=output_format)


def _plan(
    *,
    already_at_target: bool = False,
    editable: bool = True,
    node: bool = False,
    image_present: bool = True,
    workload: list[str] | None = None,
) -> UpgradePlan:
    target = ReleaseTag((8, 1, 0), "v8.1.0")
    return UpgradePlan(
        current_version="8.0.1",
        target=target,
        latest=target,
        checkout=CheckoutState(root=ROOT, head="abc1234", ref="main", version_file="8.0.1"),
        install=InstallProbe(
            cli_version="8.0.1",
            cli_path="/repo" if editable else "/opt/uv/tools/gco",
            cli_editable_from_checkout=editable,
            python="/usr/bin/python3",
            node_toolchain=node,
            container_runtime="docker",
            dev_image="gco-dev",
            dev_image_present=image_present,
        ),
        already_at_target=already_at_target,
        control_plane_stacks=["gco-global", "gco-api-gateway", "gco-monitoring"],
        workload_stacks=(
            ["gco-regional-api-us-east-1", "gco-us-east-1"] if workload is None else workload
        ),
        remote="origin",
    )


class _Engine:
    """Patch every engine seam the command touches and record the calls."""

    def __init__(self, plan: UpgradePlan, *, cycle: StackCycleResult | None = None) -> None:
        self.plan = plan
        self.cycle = cycle or StackCycleResult(
            destroyed=["gco-regional-api-us-east-1", "gco-us-east-1"],
            deployed=["gco-global", "gco-api-gateway", "gco-us-east-1"],
            teardown_attempts=1,
        )
        self.manager = MagicMock()
        self.manager.list_stacks.return_value = [
            "gco-global",
            "gco-api-gateway",
            "gco-us-east-1",
            "gco-regional-api-us-east-1",
            "gco-monitoring",
        ]
        self.patches: dict[str, MagicMock] = {}

    def __enter__(self) -> _Engine:
        specs: dict[str, Any] = {
            "find_checkout_root": {"return_value": ROOT},
            "build_plan": {"return_value": self.plan},
            "require_container_runtime": {"return_value": "docker"},
            "checkout_release": {"return_value": {"checked_out": "v8.1.0", "previous_ref": "main"}},
            "refresh_python_install": {"return_value": {"tool": "pip", "status": "ok"}},
            "refresh_node_toolchain": {"return_value": {"status": "ok"}},
            "rebuild_dev_image": {"return_value": {"status": "ok", "image": "gco-dev"}},
            "run_stack_cycle": {"return_value": self.cycle},
        }
        for name, spec in specs.items():
            patcher = patch(f"cli.commands.upgrade_cmd.{name}", **spec)
            self.patches[name] = patcher.start()
        self.patches["get_stack_manager"] = patch(
            "cli.stacks.get_stack_manager", return_value=self.manager
        ).start()
        return self

    def __exit__(self, *exc: object) -> None:
        patch.stopall()


def _invoke(
    args: list[str], *, config: GCOConfig | None = None, input_text: str | None = None
) -> Any:
    from cli.commands.upgrade_cmd import upgrade

    kwargs: dict[str, Any] = {"obj": config or _config()}
    if input_text is not None:
        kwargs["input"] = input_text
    return CliRunner().invoke(upgrade, args, **kwargs)


# ---------------------------------------------------------------------------
# Help, --check, and the up-to-date short circuit
# ---------------------------------------------------------------------------


def test_help_spells_out_the_destructive_step() -> None:
    result = _invoke(["--help"])
    assert result.exit_code == 0
    assert "DESTROYED" in result.output
    assert "docs/UPGRADING.md" in result.output
    assert "--skip-checkout" in result.output and "--force" in result.output


def test_check_prints_the_plan_and_changes_nothing() -> None:
    with _Engine(_plan(node=True)) as engine:
        result = _invoke(["--check"])

    assert result.exit_code == 0, result.output
    assert "8.0.1 → v8.1.0" in result.output
    assert "git checkout --detach v8.1.0" in result.output
    assert "pip install -e ." in result.output and "npm ci" in result.output
    assert "Rebuild the gco-dev container image with docker" in result.output
    assert "gco-regional-api-us-east-1" in result.output
    assert "gco-global, gco-api-gateway, gco-monitoring" in result.output
    assert "DESTROYED" in result.output
    assert "--check made no changes" in result.output
    engine.patches["build_plan"].assert_called_once()
    assert engine.patches["build_plan"].call_args.kwargs["skip_fetch"] is False
    for name in (
        "checkout_release",
        "refresh_python_install",
        "rebuild_dev_image",
        "run_stack_cycle",
    ):
        engine.patches[name].assert_not_called()


def test_check_when_already_current_says_so() -> None:
    with _Engine(_plan(already_at_target=True, image_present=False)):
        result = _invoke(["--check"])
    assert result.exit_code == 0, result.output
    assert "already at v8.1.0" in result.output
    assert "No local gco-dev image to rebuild" in result.output


def test_check_describes_a_non_editable_cli_and_skip_flags() -> None:
    with _Engine(_plan(editable=False)):
        result = _invoke(["--check", "--skip-checkout", "--skip-container"])
    assert result.exit_code == 0, result.output
    assert "--skip-checkout" in result.output
    assert "--skip-container" in result.output
    assert "pip install -e ." not in result.output


def test_check_names_the_non_editable_cli() -> None:
    with _Engine(_plan(editable=False)):
        result = _invoke(["--check"])
    assert result.exit_code == 0, result.output
    assert "is not the editable install of this checkout" in result.output


def test_check_with_zero_workload_regions() -> None:
    with _Engine(_plan(workload=[])):
        result = _invoke(["--check"])
    assert result.exit_code == 0, result.output
    assert "no workload Regions are configured" in result.output


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_check_emits_one_machine_document(output_format: str) -> None:
    from cli.main import cli

    with _Engine(_plan()) as engine:
        result = CliRunner().invoke(cli, ["--output", output_format, "upgrade", "--check"])

    assert result.exit_code == 0, result.output
    document = (
        json.loads(result.stdout) if output_format == "json" else yaml.safe_load(result.stdout)
    )
    assert document["status"] == "ok"
    assert document["up_to_date"] is False
    assert document["plan"]["target"] == "v8.1.0"
    assert document["plan"]["workload_stacks"] == ["gco-regional-api-us-east-1", "gco-us-east-1"]
    engine.patches["run_stack_cycle"].assert_not_called()


def test_already_current_without_force_is_a_no_op() -> None:
    with _Engine(_plan(already_at_target=True)) as engine:
        result = _invoke([])
    assert result.exit_code == 0, result.output
    assert "Already at v8.1.0" in result.output and "--force" in result.output
    engine.patches["run_stack_cycle"].assert_not_called()


def test_already_current_machine_mode_reports_up_to_date() -> None:
    from cli.main import cli

    with _Engine(_plan(already_at_target=True)):
        result = CliRunner().invoke(cli, ["--output", "json", "upgrade"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "up-to-date"


# ---------------------------------------------------------------------------
# Preparation failures
# ---------------------------------------------------------------------------


def test_upgrade_error_during_preparation_exits_1() -> None:
    with _Engine(_plan()) as engine:
        engine.patches["find_checkout_root"].side_effect = UpgradeError("not a checkout")
        result = _invoke(["-y"])
    assert result.exit_code == 1
    assert "not a checkout" in result.output
    engine.patches["run_stack_cycle"].assert_not_called()


def test_unexpected_error_during_preparation_exits_1() -> None:
    with _Engine(_plan()) as engine:
        engine.manager.list_stacks.side_effect = RuntimeError("cdk list exploded")
        result = _invoke(["-y"])
    assert result.exit_code == 1
    assert "Could not prepare the upgrade: cdk list exploded" in result.output


def test_enable_overrides_reach_both_managers() -> None:
    with _Engine(_plan()) as engine:
        result = _invoke(["-y", "--enable", "fsx_lustre"])
    assert result.exit_code == 0, result.output
    assert "Run-scoped override" in result.output
    # Once for the plan's manager, once for the fresh post-checkout manager.
    assert engine.manager.set_extra_cdk_context.call_count == 2
    for call in engine.manager.set_extra_cdk_context.call_args_list:
        assert call.args[0] == {"feature_enabled_overrides": "fsx_lustre"}


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def test_confirmation_requires_the_project_name() -> None:
    with _Engine(_plan()) as engine:
        result = _invoke([], input_text="nope\n")
    assert result.exit_code == 1
    assert "Type the project name (gco) to confirm" in result.output
    assert "Confirmation did not match" in result.output
    engine.patches["checkout_release"].assert_not_called()
    engine.patches["run_stack_cycle"].assert_not_called()


def test_confirmation_with_the_project_name_proceeds() -> None:
    with _Engine(_plan()) as engine:
        result = _invoke([], input_text="gco\n")
    assert result.exit_code == 0, result.output
    assert "DESTROYED" in result.output
    engine.patches["checkout_release"].assert_called_once()
    engine.patches["run_stack_cycle"].assert_called_once()


def test_confirmation_goes_to_stderr_in_machine_mode() -> None:
    from cli.main import cli

    with _Engine(_plan()):
        result = CliRunner().invoke(cli, ["--output", "json", "upgrade"], input="gco\n")
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["status"] == "ok"
    assert "Type the project name" in result.stderr
    assert "DESTROYED" in result.stderr


# ---------------------------------------------------------------------------
# The full run
# ---------------------------------------------------------------------------


def test_full_run_executes_every_step_in_order() -> None:
    with _Engine(_plan(node=True)) as engine:
        result = _invoke(["-y"])

    assert result.exit_code == 0, result.output
    p = engine.patches
    p["require_container_runtime"].assert_called_once_with()
    p["checkout_release"].assert_called_once()
    assert p["checkout_release"].call_args.args == (ROOT, engine.plan.target)
    p["refresh_python_install"].assert_called_once()
    p["refresh_node_toolchain"].assert_called_once()
    p["rebuild_dev_image"].assert_called_once()
    assert p["rebuild_dev_image"].call_args.kwargs["runtime"] == "docker"
    assert p["rebuild_dev_image"].call_args.kwargs["image"] == "gco-dev"
    p["run_stack_cycle"].assert_called_once()
    cycle_kwargs = p["run_stack_cycle"].call_args.kwargs
    assert cycle_kwargs["parallel"] is False and cycle_kwargs["max_workers"] == 4
    # Two managers: one for the plan, a fresh one after the checkout moved.
    assert p["get_stack_manager"].call_count == 2
    assert "Upgrade to v8.1.0 complete." in result.output
    assert "gco-regional-api-us-east-1, gco-us-east-1" in result.output
    assert "Restore any data you staged" in result.output


def test_full_run_forwards_parallel_and_workers() -> None:
    with _Engine(_plan()) as engine:
        result = _invoke(["-y", "--parallel", "--max-workers", "6"])
    assert result.exit_code == 0, result.output
    kwargs = engine.patches["run_stack_cycle"].call_args.kwargs
    assert kwargs["parallel"] is True and kwargs["max_workers"] == 6


def test_stack_callbacks_print_progress() -> None:
    with _Engine(_plan()) as engine:
        result = _invoke(["-y"])
        kwargs = engine.patches["run_stack_cycle"].call_args.kwargs
    assert result.exit_code == 0
    # The callbacks are plain closures over the formatter; drive them directly.
    kwargs["on_stack_start"]("gco-us-east-1")
    kwargs["on_stack_complete"]("gco-us-east-1", True)
    kwargs["on_stack_complete"]("gco-us-west-2", False)


def test_skip_checkout_and_skip_container_only_cycle_the_stacks() -> None:
    with _Engine(_plan(already_at_target=True)) as engine:
        result = _invoke(["-y", "--skip-checkout", "--skip-container"])

    assert result.exit_code == 0, result.output
    p = engine.patches
    assert p["build_plan"].call_args.kwargs["skip_fetch"] is True
    for name in (
        "checkout_release",
        "refresh_python_install",
        "refresh_node_toolchain",
        "rebuild_dev_image",
    ):
        p[name].assert_not_called()
    p["run_stack_cycle"].assert_called_once()


def test_force_when_already_current_skips_the_checkout_but_cycles() -> None:
    with _Engine(_plan(already_at_target=True)) as engine:
        result = _invoke(["-y", "--force"])
    assert result.exit_code == 0, result.output
    engine.patches["checkout_release"].assert_not_called()
    engine.patches["refresh_python_install"].assert_called_once()
    engine.patches["run_stack_cycle"].assert_called_once()


def test_non_editable_cli_is_warned_about_not_refreshed() -> None:
    with _Engine(_plan(editable=False)) as engine:
        result = _invoke(["-y"])
    assert result.exit_code == 0, result.output
    assert "reinstall it from v8.1.0 yourself" in result.output
    engine.patches["refresh_python_install"].assert_not_called()


def test_missing_dev_image_is_skipped() -> None:
    with _Engine(_plan(image_present=False)) as engine:
        result = _invoke(["-y", "--image", "other-dev"])
    assert result.exit_code == 0, result.output
    engine.patches["rebuild_dev_image"].assert_not_called()
    assert engine.patches["build_plan"].call_args.kwargs["image"] == "other-dev"


def test_warnings_from_node_and_container_steps_are_surfaced() -> None:
    with _Engine(_plan(node=True)) as engine:
        engine.patches["refresh_node_toolchain"].return_value = {
            "status": "warning",
            "message": "npm ci failed; run it by hand",
        }
        engine.patches["rebuild_dev_image"].return_value = {
            "status": "warning",
            "message": "docker could not rebuild gco-dev",
        }
        result = _invoke(["-y"])
    assert result.exit_code == 0, result.output
    assert "npm ci failed; run it by hand" in result.output
    assert "docker could not rebuild gco-dev" in result.output


def test_ref_and_remote_reach_the_plan() -> None:
    with _Engine(_plan()) as engine:
        result = _invoke(["--check", "--ref", "v8.1.0", "--remote", "upstream"])
    assert result.exit_code == 0, result.output
    kwargs = engine.patches["build_plan"].call_args.kwargs
    assert kwargs["ref"] == "v8.1.0" and kwargs["remote"] == "upstream"


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_successful_run_emits_one_machine_document(output_format: str) -> None:
    from cli.main import cli

    with _Engine(_plan(node=True)):
        result = CliRunner().invoke(cli, ["--output", output_format, "upgrade", "-y"])

    assert result.exit_code == 0, result.output
    document = (
        json.loads(result.stdout) if output_format == "json" else yaml.safe_load(result.stdout)
    )
    assert document["status"] == "ok"
    assert document["plan"]["target"] == "v8.1.0"
    steps = document["steps"]
    assert steps["checkout"]["checked_out"] == "v8.1.0"
    assert steps["python"]["tool"] == "pip"
    assert steps["node"]["status"] == "ok"
    assert steps["container"]["status"] == "ok"
    assert steps["stacks"]["ok"] is True
    assert steps["stacks"]["destroyed"] == ["gco-regional-api-us-east-1", "gco-us-east-1"]


def test_machine_document_records_skipped_steps() -> None:
    from cli.main import cli

    with _Engine(_plan(editable=False, image_present=False)):
        result = CliRunner().invoke(cli, ["--output", "json", "upgrade", "-y", "--skip-checkout"])
    assert result.exit_code == 0, result.output
    steps = json.loads(result.stdout)["steps"]
    assert steps["checkout"] == {"status": "skipped", "reason": "--skip-checkout"}
    assert "python" not in steps
    assert steps["container"]["status"] == "skipped"


def test_machine_document_records_already_at_target_checkout() -> None:
    from cli.main import cli

    with _Engine(_plan(already_at_target=True, editable=False)):
        result = CliRunner().invoke(
            cli, ["--output", "json", "upgrade", "-y", "--force", "--skip-container"]
        )
    assert result.exit_code == 0, result.output
    steps = json.loads(result.stdout)["steps"]
    assert steps["checkout"]["status"] == "already-at-target"
    assert steps["python"]["status"] == "skipped"
    assert steps["container"]["reason"] == "--skip-container"


# ---------------------------------------------------------------------------
# Execution failures
# ---------------------------------------------------------------------------


def test_engine_error_during_execution_exits_1() -> None:
    with _Engine(_plan()) as engine:
        engine.patches["refresh_python_install"].side_effect = UpgradeError("pip broke")
        result = _invoke(["-y"])
    assert result.exit_code == 1
    assert "pip broke" in result.output
    engine.patches["run_stack_cycle"].assert_not_called()


def test_unexpected_error_during_execution_exits_1() -> None:
    with _Engine(_plan()) as engine:
        engine.patches["run_stack_cycle"].side_effect = RuntimeError("boom")
        result = _invoke(["-y"])
    assert result.exit_code == 1
    assert "Upgrade failed: boom" in result.output


def test_missing_container_runtime_stops_before_the_checkout() -> None:
    with _Engine(_plan()) as engine:
        engine.patches["require_container_runtime"].side_effect = UpgradeError("no runtime")
        result = _invoke(["-y"])
    assert result.exit_code == 1
    assert "no runtime" in result.output
    engine.patches["checkout_release"].assert_not_called()


def test_failed_stack_cycle_reports_the_phase_and_the_resume_command() -> None:
    cycle = StackCycleResult(
        destroyed=["gco-regional-api-us-east-1"],
        failed=["gco-us-east-1"],
        teardown_attempts=3,
        phase_failed="teardown",
    )
    with _Engine(_plan(), cycle=cycle):
        result = _invoke(["-y"])
    assert result.exit_code == 1
    assert "stopped during the teardown phase" in result.output
    assert "gco-us-east-1" in result.output
    assert "gco upgrade --skip-checkout" in result.output


def test_failed_deploy_without_named_stacks() -> None:
    cycle = StackCycleResult(deployed=["gco-global"], phase_failed="deploy")
    with _Engine(_plan(), cycle=cycle):
        result = _invoke(["-y"])
    assert result.exit_code == 1
    assert "deploy phase" in result.output and "none reported" in result.output
