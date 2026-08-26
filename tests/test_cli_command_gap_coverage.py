"""Behavior-focused coverage for CLI command branches left open by the CI report.

Every command is driven through :class:`click.testing.CliRunner`. Manager,
AWS, image-mirror, and timing boundaries are mocked so this module performs no
real network, subprocess, container-runtime, or infrastructure operations.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from click.testing import CliRunner

from cli.capacity.multi_region import RegionCapacity
from cli.commands.capacity_cmd import capacity
from cli.commands.config_cmd import config_cmd
from cli.commands.costs_cmd import costs
from cli.commands.images_cmd import images
from cli.commands.models_cmd import models
from cli.commands.stacks_cmd import stacks
from cli.commands.webhooks_cmd import webhooks
from cli.config import GCOConfig


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _config(
    *,
    output_format: str = "table",
    verbose: bool = False,
    use_regional_api: bool = False,
) -> GCOConfig:
    return GCOConfig(
        project_name="test-gco",
        default_region="us-east-1",
        output_format=output_format,
        verbose=verbose,
        use_regional_api=use_regional_api,
    )


def _invoke(
    runner: CliRunner,
    group: Any,
    args: list[str],
    *,
    config: GCOConfig | None = None,
    input_text: str | None = None,
) -> Any:
    kwargs: dict[str, Any] = {"obj": config or _config()}
    if input_text is not None:
        kwargs["input"] = input_text
    return runner.invoke(group, args, **kwargs)


# ---------------------------------------------------------------------------
# config_cmd.py
# ---------------------------------------------------------------------------


def test_config_get_reports_the_full_missing_dotted_key(runner: CliRunner) -> None:
    result = _invoke(runner, config_cmd, ["get", "default_region.zone"])

    assert result.exit_code == 1
    assert "Config key not found: default_region.zone" in result.output


# ---------------------------------------------------------------------------
# costs_cmd.py
# ---------------------------------------------------------------------------


def test_cost_workloads_table_sorts_rows_and_totals_all_regions(runner: CliRunner) -> None:
    tracker = MagicMock()
    cheap = SimpleNamespace(
        name="small-inference",
        workload_type="inference",
        instance_type="g5.xlarge",
        gpu_count=1,
        hourly_rate=0.5,
        runtime_hours=3.0,
        estimated_cost=1.5,
        region="us-east-1",
    )
    expensive = SimpleNamespace(
        name="training-job-with-a-name-that-is-truncated",
        workload_type="job",
        instance_type="p5.48xlarge",
        gpu_count=8,
        hourly_rate=3.0,
        runtime_hours=2.0,
        estimated_cost=6.0,
        region="us-west-2",
    )
    tracker.estimate_running_workloads.side_effect = [[cheap], [expensive]]

    with (
        patch("cli.costs.get_cost_tracker", return_value=tracker),
        patch(
            "cli.commands.costs_cmd._get_deployment_regions",
            return_value=["us-east-1", "us-west-2"],
        ),
    ):
        result = _invoke(runner, costs, ["workloads"])

    assert result.exit_code == 0, result.output
    assert tracker.estimate_running_workloads.call_args_list == [
        call("us-east-1"),
        call("us-west-2"),
    ]
    assert result.output.index("training-job-with-a-name-that") < result.output.index(
        "small-inference"
    )
    assert "3.500" in result.output
    assert "7.5000" in result.output
    assert "training-job-with-a-name-that-is" not in result.output


def test_cost_workloads_json_preserves_region_and_workload_fields(runner: CliRunner) -> None:
    tracker = MagicMock()
    tracker.estimate_running_workloads.return_value = [
        SimpleNamespace(
            name="endpoint-a",
            workload_type="inference",
            instance_type="g6.xlarge",
            gpu_count=1,
            hourly_rate=1.25,
            runtime_hours=4.0,
            estimated_cost=5.0,
            region="eu-west-1",
        )
    ]

    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        result = _invoke(
            runner,
            costs,
            ["workloads", "--region", "eu-west-1"],
            config=_config(output_format="json"),
        )

    assert result.exit_code == 0, result.output
    assert '"name": "endpoint-a"' in result.output
    assert '"type": "inference"' in result.output
    assert '"region": "eu-west-1"' in result.output
    tracker.estimate_running_workloads.assert_called_once_with("eu-west-1")


def test_cost_forecast_explains_an_unavailable_forecast(runner: CliRunner) -> None:
    tracker = MagicMock()
    tracker.get_forecast.return_value = {"error": "not enough historical data"}

    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        result = _invoke(runner, costs, ["forecast", "--days", "45"])

    assert result.exit_code == 0, result.output
    assert "Forecast unavailable: not enough historical data" in result.output
    assert "Cost Explorer needs 14+ days" in result.output
    tracker.get_forecast.assert_called_once_with(days_ahead=45)


def test_cost_forecast_surfaces_tracker_failures(runner: CliRunner) -> None:
    tracker = MagicMock()
    tracker.get_forecast.side_effect = RuntimeError("cost explorer denied")

    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        result = _invoke(runner, costs, ["forecast"])

    assert result.exit_code == 1
    assert "Failed to get forecast: cost explorer denied" in result.output


# ---------------------------------------------------------------------------
# images_cmd.py
# ---------------------------------------------------------------------------


def test_images_init_json_reports_idempotent_result(runner: CliRunner) -> None:
    manager = MagicMock()
    manager.init.return_value = {"name": "gco/service", "created": False, "retain": True}

    with patch("cli.images.get_image_manager", return_value=manager):
        result = _invoke(
            runner,
            images,
            ["init", "service", "--retain"],
            config=_config(output_format="json"),
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "name": "gco/service",
        "created": False,
        "retain": True,
    }
    manager.init.assert_called_once_with("service", retain=True)


def test_images_build_json_accepts_equals_in_values_and_optional_digest(
    runner: CliRunner,
) -> None:
    manager = MagicMock()
    manager.build.return_value = {
        "image_uri": "registry.example/gco/service:v2",
        "repository": "gco/service",
        "tag": "v2",
    }

    with patch("cli.images.get_image_manager", return_value=manager):
        result = _invoke(
            runner,
            images,
            [
                "build",
                "./context",
                "--name",
                "service",
                "--tag",
                "v2",
                "--dockerfile",
                "Containerfile",
                "--build-arg",
                "MODE=release",
                "--build-arg",
                "TOKEN=left=right",
                "--platform",
                "linux/arm64",
            ],
            config=_config(output_format="json"),
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "image_uri": "registry.example/gco/service:v2",
        "repository": "gco/service",
        "tag": "v2",
    }
    manager.build.assert_called_once_with(
        context="./context",
        name="service",
        tag="v2",
        dockerfile="Containerfile",
        build_args={"MODE": "release", "TOKEN": "left=right"},
        platform="linux/arm64",
        retain=False,
        quiet=True,
    )


def test_images_push_json_handles_a_result_without_digest(runner: CliRunner) -> None:
    manager = MagicMock()
    manager.push.return_value = {"image_uri": "registry.example/gco/service:v2"}

    with patch("cli.images.get_image_manager", return_value=manager):
        result = _invoke(
            runner,
            images,
            [
                "push",
                "service",
                "--tag",
                "v2",
                "--local-image",
                "local/service:ready",
                "--retain",
            ],
            config=_config(output_format="json"),
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"image_uri": "registry.example/gco/service:v2"}
    manager.push.assert_called_once_with(
        name="service",
        tag="v2",
        local_image="local/service:ready",
        retain=True,
        quiet=True,
    )


def test_images_mirror_dry_run_renders_plan_without_aws(runner: CliRunner) -> None:
    sources = ["docker.io/example/controller:v1"]
    plan = [
        SimpleNamespace(
            source_ref="docker.io/example/controller:v1",
            dest_ref=(
                "<account>.dkr.ecr.us-west-2.amazonaws.com/gco/dockerhub/example/controller:v1"
            ),
        )
    ]

    with (
        patch("cli._image_mirror.cdk_default_namespace", return_value="/gco/dockerhub/"),
        patch(
            "cli._image_mirror._registry_host",
            return_value="<account>.dkr.ecr.us-west-2.amazonaws.com",
        ) as registry_host,
        patch("cli._image_mirror.collect_source_refs", return_value=sources),
        patch("cli._image_mirror.plan_from_sources", return_value=plan) as plan_from_sources,
        patch("cli._image_mirror.mirror_images") as mirror_images,
    ):
        result = _invoke(
            runner,
            images,
            ["mirror", "--region", "us-west-2", "--dry-run"],
            config=_config(output_format="json"),
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "region": "us-west-2",
        "ecr_namespace": "gco/dockerhub",
        "images": [
            {
                "source_ref": "docker.io/example/controller:v1",
                "dest_ref": (
                    "<account>.dkr.ecr.us-west-2.amazonaws.com/gco/dockerhub/example/controller:v1"
                ),
            }
        ],
    }
    registry_host.assert_called_once_with("<account>", "us-west-2")
    plan_from_sources.assert_called_once_with(
        sources,
        "<account>.dkr.ecr.us-west-2.amazonaws.com",
        "gco/dockerhub",
    )
    mirror_images.assert_not_called()


def test_images_mirror_live_honors_namespace_and_no_skip_existing(
    runner: CliRunner,
) -> None:
    mirrored = ["gco/custom/controller:v1"]
    skipped = ["gco/custom/scheduler:v1"]
    mirror_result = {
        "mirrored": mirrored,
        "skipped": skipped,
        "registry": "123456789012.dkr.ecr.eu-west-1.amazonaws.com",
        "strategy": "crane",
    }

    with (
        patch("cli._image_mirror.cdk_default_namespace") as default_namespace,
        patch("cli._image_mirror.mirror_images", return_value=mirror_result) as mirror_images,
    ):
        result = _invoke(
            runner,
            images,
            [
                "mirror",
                "--region",
                "eu-west-1",
                "--ecr-namespace",
                "/gco/custom/",
                "--no-skip-existing",
            ],
            config=_config(output_format="json"),
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == mirror_result
    default_namespace.assert_not_called()
    mirror_images.assert_called_once()
    assert mirror_images.call_args.args == ("eu-west-1",)
    assert mirror_images.call_args.kwargs["ecr_namespace"] == "gco/custom"
    assert mirror_images.call_args.kwargs["skip_existing"] is False
    quiet_log = mirror_images.call_args.kwargs["log"]
    assert callable(quiet_log)
    assert quiet_log("must not reach structured output") is None


def test_images_mirror_surfaces_copy_failures(runner: CliRunner) -> None:
    with (
        patch("cli._image_mirror.cdk_default_namespace", return_value="gco/dockerhub"),
        patch("cli._image_mirror.mirror_images", side_effect=RuntimeError("crane unavailable")),
    ):
        result = _invoke(runner, images, ["mirror", "--region", "us-east-1"])

    assert result.exit_code == 1
    assert "Failed to mirror images: crane unavailable" in result.output


# ---------------------------------------------------------------------------
# models_cmd.py
# ---------------------------------------------------------------------------


def test_models_upload_json_prints_machine_readable_result(runner: CliRunner) -> None:
    manager = MagicMock()
    manager.upload.return_value = {
        "files_uploaded": 2,
        "s3_uri": "s3://model-bucket/models/llama",
        "model_name": "llama",
    }

    with patch("cli.models.get_model_manager", return_value=manager):
        result = _invoke(
            runner,
            models,
            ["upload", "./weights", "--name", "llama"],
            config=_config(output_format="json"),
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "files_uploaded": 2,
        "s3_uri": "s3://model-bucket/models/llama",
        "model_name": "llama",
    }
    manager.upload.assert_called_once_with("./weights", "llama")


def test_models_upload_regional_surfaces_targeted_failure(runner: CliRunner) -> None:
    manager = MagicMock()
    manager.upload.side_effect = RuntimeError("regional bucket parameter missing")

    with patch("cli.models.get_regional_bucket_manager", return_value=manager):
        result = _invoke(
            runner,
            models,
            [
                "upload-regional",
                "./dataset",
                "--region",
                "ap-southeast-2",
                "--prefix",
                "training/input",
            ],
        )

    assert result.exit_code == 1
    assert "Failed to upload to regional bucket: regional bucket parameter missing" in result.output
    manager.upload.assert_called_once_with(
        "./dataset",
        "ap-southeast-2",
        prefix="training/input",
    )


def test_models_list_surfaces_manager_failure(runner: CliRunner) -> None:
    manager = MagicMock()
    manager.list_models.side_effect = RuntimeError("s3 denied")

    with patch("cli.models.get_model_manager", return_value=manager):
        result = _invoke(runner, models, ["list"])

    assert result.exit_code == 1
    assert "Failed to list models: s3 denied" in result.output


# ---------------------------------------------------------------------------
# webhooks_cmd.py
# ---------------------------------------------------------------------------


def test_webhooks_list_passes_filters_and_surfaces_api_failure(runner: CliRunner) -> None:
    aws_client = MagicMock()
    aws_client.call_api.side_effect = RuntimeError("regional API unavailable")

    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        result = _invoke(
            runner,
            webhooks,
            ["list", "--namespace", "research"],
            config=_config(use_regional_api=True),
        )

    assert result.exit_code == 1
    assert "Failed to list webhooks: regional API unavailable" in result.output
    aws_client.call_api.assert_called_once_with(
        method="GET",
        path="/api/v1/webhooks",
        region="us-east-1",
        params={"namespace": "research"},
    )


def test_webhooks_get_not_found_keeps_specific_error(runner: CliRunner) -> None:
    aws_client = MagicMock()
    aws_client.call_api.return_value = {
        "webhooks": [{"id": "other", "url": "https://example.test/hook"}]
    }

    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        result = _invoke(
            runner,
            webhooks,
            ["get", "missing", "--region", "eu-central-1"],
        )

    assert result.exit_code == 1
    assert "Webhook 'missing' not found" in result.output
    assert "Failed to get webhook" not in result.output
    aws_client.call_api.assert_called_once_with(
        method="GET",
        path="/api/v1/webhooks",
        region="eu-central-1",
    )


def test_webhooks_get_wraps_unexpected_api_errors(runner: CliRunner) -> None:
    aws_client = MagicMock()
    aws_client.call_api.side_effect = ValueError("malformed gateway response")

    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        result = _invoke(runner, webhooks, ["get", "hook-123"])

    assert result.exit_code == 1
    assert "Failed to get webhook: malformed gateway response" in result.output


def test_webhooks_create_forwards_repeatable_events_and_secret(runner: CliRunner) -> None:
    aws_client = MagicMock()
    aws_client.call_api.return_value = {"id": "hook-123", "status": "created"}

    with patch("cli.aws_client.get_aws_client", return_value=aws_client):
        result = _invoke(
            runner,
            webhooks,
            [
                "create",
                "--url",
                "https://example.test/hook",
                "--event",
                "job.completed",
                "--event",
                "job.failed",
                "--namespace",
                "research",
                "--secret",
                "unit-test-secret",
                "--region",
                "us-west-2",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "Webhook registered successfully" in result.output
    assert "hook-123" in result.output
    aws_client.call_api.assert_called_once_with(
        method="POST",
        path="/api/v1/webhooks",
        region="us-west-2",
        body={
            "url": "https://example.test/hook",
            "events": ["job.completed", "job.failed"],
            "namespace": "research",
            "secret": "unit-test-secret",
        },
    )


# ---------------------------------------------------------------------------
# stacks_cmd.py -- low-risk wrappers only; no CDK, AWS, or subprocess calls
# ---------------------------------------------------------------------------


def test_stacks_list_refresh_explains_compatibility_before_failure(
    runner: CliRunner,
) -> None:
    manager = MagicMock()
    manager.list_stacks.side_effect = RuntimeError("synthesis failed")

    with patch("cli.stacks.get_stack_manager", return_value=manager):
        result = _invoke(runner, stacks, ["list", "--refresh"])

    assert result.exit_code == 1
    assert "--refresh is retained for compatibility" in result.output
    assert "Failed to list stacks: synthesis failed" in result.output


def test_stacks_synth_accepts_empty_tool_output_as_success(runner: CliRunner) -> None:
    manager = MagicMock()
    manager.synth.return_value = ""

    with patch("cli.stacks.get_stack_manager", return_value=manager):
        result = _invoke(runner, stacks, ["synth", "test-gco-global"])

    assert result.exit_code == 0, result.output
    assert "CDK synthesis completed" in result.output
    manager.synth.assert_called_once_with("test-gco-global", quiet=True)


def test_stacks_diff_reports_no_changes_for_empty_output(runner: CliRunner) -> None:
    manager = MagicMock()
    manager.diff.return_value = None

    with patch("cli.stacks.get_stack_manager", return_value=manager):
        result = _invoke(runner, stacks, ["diff", "test-gco-global"])

    assert result.exit_code == 0, result.output
    assert "No differences found" in result.output
    manager.diff.assert_called_once_with("test-gco-global")


def test_stacks_deploy_rejects_manager_failure_and_ignores_malformed_tags(
    runner: CliRunner,
) -> None:
    manager = MagicMock()
    manager.deploy.return_value = False

    with patch("cli.stacks.get_stack_manager", return_value=manager):
        result = _invoke(
            runner,
            stacks,
            [
                "deploy",
                "test-gco-global",
                "--yes",
                "--outputs-file",
                "outputs.json",
                "--tag",
                "Environment=test",
                "--tag",
                "ignored",
                "--tag",
                "Token=left=right",
            ],
        )

    assert result.exit_code == 1
    assert "Deployment failed" in result.output
    manager.deploy.assert_called_once_with(
        stack_name="test-gco-global",
        require_approval=False,
        outputs_file="outputs.json",
        tags={"Environment": "test", "Token": "left=right"},
    )


def test_stacks_destroy_confirmation_preserves_force_false(runner: CliRunner) -> None:
    manager = MagicMock()
    manager.destroy.return_value = True

    with patch("cli.stacks.get_stack_manager", return_value=manager):
        result = _invoke(
            runner,
            stacks,
            ["destroy", "test-gco-us-east-1"],
            input_text="y\n",
        )

    assert result.exit_code == 0, result.output
    assert "Are you sure you want to destroy test-gco-us-east-1?" in result.output
    assert "destroyed successfully" in result.output
    manager.destroy.assert_called_once_with(
        stack_name="test-gco-us-east-1",
        force=False,
    )


def test_stacks_deploy_all_reports_parallel_callbacks_and_failed_stack(
    runner: CliRunner,
) -> None:
    manager = MagicMock()
    manager.list_stacks.return_value = ["test-gco-global", "test-gco-us-east-1"]

    def deploy_orchestrated(**kwargs: Any) -> tuple[bool, list[str], list[str]]:
        kwargs["on_stack_start"]("test-gco-global")
        kwargs["on_stack_complete"]("test-gco-global", True)
        kwargs["on_stack_start"]("test-gco-us-east-1")
        kwargs["on_stack_complete"]("test-gco-us-east-1", False)
        return False, ["test-gco-global"], ["test-gco-us-east-1"]

    manager.deploy_orchestrated.side_effect = deploy_orchestrated

    with patch("cli.stacks.get_stack_manager", return_value=manager):
        result = _invoke(
            runner,
            stacks,
            [
                "deploy-all",
                "--yes",
                "--parallel",
                "--max-workers",
                "2",
                "--outputs-file",
                "outputs.json",
                "--tag",
                "Environment=test",
                "--tag",
                "ignored",
            ],
        )

    assert result.exit_code == 1
    assert "Parallel mode enabled (max workers: 2)" in result.output
    assert "test-gco-global deployed" in result.output
    assert "test-gco-us-east-1 failed" in result.output
    assert "Deployed: 1/2 stacks" in result.output
    kwargs = manager.deploy_orchestrated.call_args.kwargs
    assert kwargs["require_approval"] is False
    assert kwargs["outputs_file"] == "outputs.json"
    assert kwargs["tags"] == {"Environment": "test"}
    assert kwargs["parallel"] is True
    assert kwargs["max_workers"] == 2


def test_stacks_destroy_all_confirms_retries_cleanup_and_then_succeeds(
    runner: CliRunner,
) -> None:
    manager = MagicMock()
    stack_names = ["test-gco-global", "test-gco-us-east-1"]
    manager.list_stacks.return_value = stack_names

    def destroy_orchestrated(**kwargs: Any) -> tuple[bool, list[str], list[str]]:
        if manager.destroy_orchestrated.call_count == 1:
            kwargs["on_stack_start"]("test-gco-us-east-1")
            kwargs["on_stack_complete"]("test-gco-us-east-1", False)
            return False, ["test-gco-global"], ["test-gco-us-east-1"]
        kwargs["on_stack_start"]("test-gco-us-east-1")
        kwargs["on_stack_complete"]("test-gco-us-east-1", True)
        return True, stack_names, []

    manager.destroy_orchestrated.side_effect = destroy_orchestrated

    with (
        patch("cli.stacks.get_stack_manager", return_value=manager),
        patch(
            "cli.stacks.get_stack_destroy_order",
            return_value=["test-gco-us-east-1", "test-gco-global"],
        ) as destroy_order,
        patch("time.sleep") as sleep,
    ):
        result = _invoke(
            runner,
            stacks,
            ["destroy-all", "--parallel", "--max-workers", "3"],
            input_text="y\n",
        )

    assert result.exit_code == 0, result.output
    assert "This will destroy ALL GCO stacks" in result.output
    assert "1 stack(s) failed: test-gco-us-east-1" in result.output
    assert "Attempt 2/3" in result.output
    assert "test-gco-us-east-1 failed" in result.output
    assert "test-gco-us-east-1 destroyed" in result.output
    assert "Destroyed: 2/2 stacks" in result.output
    destroy_order.assert_called_once_with(stack_names, project_name="test-gco")
    manager.cleanup_orphaned_network_interfaces.assert_called_once_with()
    sleep.assert_called_once_with(30)
    assert manager.destroy_orchestrated.call_count == 2
    assert manager.destroy_orchestrated.call_args.kwargs["parallel"] is True
    assert manager.destroy_orchestrated.call_args.kwargs["max_workers"] == 3


def test_stacks_bootstrap_false_result_is_nonzero(runner: CliRunner) -> None:
    manager = MagicMock()
    manager.bootstrap.return_value = False

    with patch("cli.stacks.get_stack_manager", return_value=manager):
        result = _invoke(
            runner,
            stacks,
            ["bootstrap", "--account", "123456789012", "--region", "eu-west-1"],
        )

    assert result.exit_code == 1
    assert "Bootstrap failed" in result.output
    manager.bootstrap.assert_called_once_with(account="123456789012", region="eu-west-1")


def test_stacks_status_reports_missing_stack(runner: CliRunner) -> None:
    manager = MagicMock()
    manager.get_stack_status.return_value = None

    with patch("cli.stacks.get_stack_manager", return_value=manager):
        result = _invoke(
            runner,
            stacks,
            ["status", "missing", "--region", "us-east-1"],
        )

    assert result.exit_code == 1
    assert "Stack missing not found in us-east-1" in result.output


def test_stacks_outputs_warns_when_stack_has_no_outputs(runner: CliRunner) -> None:
    manager = MagicMock()
    manager.get_outputs.return_value = {}

    with patch("cli.stacks.get_stack_manager", return_value=manager):
        result = _invoke(
            runner,
            stacks,
            ["outputs", "test-gco-us-east-1", "--region", "us-east-1"],
        )

    assert result.exit_code == 0, result.output
    assert "No outputs found for test-gco-us-east-1" in result.output


def test_fsx_enable_confirmation_displays_and_persists_all_options(
    runner: CliRunner,
) -> None:
    with patch("cli.stacks.update_fsx_config") as update:
        result = _invoke(
            runner,
            stacks,
            [
                "fsx",
                "enable",
                "--region",
                "us-west-2",
                "--storage-capacity",
                "2400",
                "--deployment-type",
                "PERSISTENT_2",
                "--throughput",
                "500",
                "--compression",
                "NONE",
                "--import-path",
                "s3://input/data",
                "--export-path",
                "s3://output/data",
            ],
            input_text="y\n",
        )

    assert result.exit_code == 0, result.output
    assert "FSx for Lustre configuration for region us-west-2" in result.output
    assert "Import Path: s3://input/data" in result.output
    assert "Export Path: s3://output/data" in result.output
    update.assert_called_once_with(
        {
            "enabled": True,
            "storage_capacity_gib": 2400,
            "deployment_type": "PERSISTENT_2",
            "per_unit_storage_throughput": 500,
            "data_compression_type": "NONE",
            "import_path": "s3://input/data",
            "export_path": "s3://output/data",
            "auto_import_policy": "NEW_CHANGED_DELETED",
        },
        "us-west-2",
    )


def test_valkey_enable_confirmation_displays_requested_limits(runner: CliRunner) -> None:
    with patch("cli.stacks.update_valkey_config") as update:
        result = _invoke(
            runner,
            stacks,
            [
                "valkey",
                "enable",
                "--max-storage",
                "12",
                "--max-ecpu",
                "9000",
                "--snapshot-retention",
                "5",
            ],
            input_text="y\n",
        )

    assert result.exit_code == 0, result.output
    assert "Max Data Storage: 12 GB" in result.output
    assert "Max eCPU/second: 9000" in result.output
    assert "Snapshot Retention: 5 days" in result.output
    update.assert_called_once_with(
        {
            "enabled": True,
            "max_data_storage_gb": 12,
            "max_ecpu_per_second": 9000,
            "snapshot_retention_limit": 5,
        }
    )


def test_aurora_enable_confirmation_explains_scale_to_zero(runner: CliRunner) -> None:
    with patch("cli.stacks.update_aurora_config") as update:
        result = _invoke(
            runner,
            stacks,
            [
                "aurora",
                "enable",
                "--min-acu",
                "0",
                "--max-acu",
                "8",
                "--backup-retention",
                "14",
                "--deletion-protection",
            ],
            input_text="y\n",
        )

    assert result.exit_code == 0, result.output
    assert "Min ACU: 0 (scale to zero)" in result.output
    assert "Deletion Protection: True" in result.output
    update.assert_called_once_with(
        {
            "enabled": True,
            "min_acu": 0,
            "max_acu": 8,
            "backup_retention_days": 14,
            "deletion_protection": True,
        }
    )


@pytest.mark.parametrize(
    ("args", "patch_target", "warning_text", "expected_args"),
    [
        (
            ["fsx", "disable"],
            "cli.stacks.update_fsx_config",
            "Existing FSx file systems will be deleted",
            ({"enabled": False}, None),
        ),
        (
            ["valkey", "disable"],
            "cli.stacks.update_valkey_config",
            "Existing Valkey caches will be deleted",
            ({"enabled": False},),
        ),
        (
            ["aurora", "disable"],
            "cli.stacks.update_aurora_config",
            "Existing Aurora clusters will be deleted",
            ({"enabled": False},),
        ),
    ],
)
def test_stack_feature_disable_confirmations_are_explicit(
    runner: CliRunner,
    args: list[str],
    patch_target: str,
    warning_text: str,
    expected_args: tuple[Any, ...],
) -> None:
    with patch(patch_target) as update:
        result = _invoke(runner, stacks, args, input_text="y\n")

    assert result.exit_code == 0, result.output
    assert warning_text in result.output
    assert "Are you sure?" in result.output
    assert update.call_args.args == expected_args


# ---------------------------------------------------------------------------
# capacity_cmd.py -- renderers and option forwarding with mocked advisors
# ---------------------------------------------------------------------------


def test_capacity_check_json_forwards_capacity_type(runner: CliRunner) -> None:
    checker = MagicMock()
    checker.estimate_capacity.return_value = [
        {
            "instance_type": "g6.xlarge",
            "region": "us-west-2",
            "capacity_type": "spot",
            "availability": "high",
        }
    ]

    with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
        result = _invoke(
            runner,
            capacity,
            [
                "check",
                "--instance-type",
                "g6.xlarge",
                "--region",
                "us-west-2",
                "--type",
                "spot",
            ],
            config=_config(output_format="json"),
        )

    assert result.exit_code == 0, result.output
    assert '"availability": "high"' in result.output
    checker.estimate_capacity.assert_called_once_with("g6.xlarge", "us-west-2", "spot")


def test_capacity_instance_info_missing_is_a_specific_error(runner: CliRunner) -> None:
    checker = MagicMock()
    checker.get_instance_info.return_value = None

    with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
        result = _invoke(runner, capacity, ["instance-info", "unknown.large"])

    assert result.exit_code == 1
    # The message names the type AND the region, because a missing description
    # usually means "not offered there" rather than "no such type".
    assert "unknown.large" in result.output
    assert "not be offered in that region" in result.output
    assert "Failed to get instance info" not in result.output


def test_capacity_status_sorts_regions_and_recommends_lowest_score(
    runner: CliRunner,
) -> None:
    checker = MagicMock()
    checker.get_all_regions_capacity.return_value = [
        SimpleNamespace(
            region="us-west-2",
            queue_depth=9,
            running_jobs=3,
            gpu_utilization=70.0,
            cpu_utilization=60.0,
            recommendation_score=85.0,
        ),
        SimpleNamespace(
            region="us-east-1",
            queue_depth=1,
            running_jobs=1,
            gpu_utilization=20.0,
            cpu_utilization=30.0,
            recommendation_score=15.0,
        ),
    ]

    with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
        result = _invoke(runner, capacity, ["status"])

    assert result.exit_code == 0, result.output
    assert result.output.index("us-east-1") < result.output.index("us-west-2")
    assert "Recommended region: us-east-1" in result.output


def test_capacity_status_json_emits_empty_array_when_no_stacks(runner: CliRunner) -> None:
    checker = MagicMock()
    checker.get_all_regions_capacity.return_value = []

    with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
        result = _invoke(
            runner,
            capacity,
            ["status"],
            config=_config(output_format="json"),
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert "No GCO stacks found" in result.stderr


def test_capacity_status_json_emits_region_objects(runner: CliRunner) -> None:
    checker = MagicMock()
    checker.get_all_regions_capacity.return_value = [
        RegionCapacity(
            region="us-east-1",
            queue_depth=2,
            running_jobs=1,
            recommendation_score=12.5,
        )
    ]

    with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
        result = _invoke(
            runner,
            capacity,
            ["status"],
            config=_config(output_format="json"),
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload[0]["region"] == "us-east-1"
    assert payload[0]["recommendation_score"] == 12.5
    assert "REGION" not in result.stdout
    assert "ℹ" not in result.stdout


def test_capacity_recommend_json_emits_structured_result(runner: CliRunner) -> None:
    checker = MagicMock()
    checker.recommend_capacity_type.return_value = (
        "on-demand",
        "stable capacity for this workload",
    )

    with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
        result = _invoke(
            runner,
            capacity,
            ["recommend", "-i", "g5.2xlarge", "-r", "us-east-1"],
            config=_config(output_format="json"),
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "capacity_type": "on-demand",
        "explanation": "stable capacity for this workload",
    }
    assert "ℹ" not in result.stdout


def test_capacity_recommend_table_preserves_human_output(runner: CliRunner) -> None:
    checker = MagicMock()
    checker.recommend_capacity_type.return_value = ("spot", "lowest cost")

    with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
        result = _invoke(
            runner,
            capacity,
            ["recommend", "-i", "g5.2xlarge", "-r", "us-east-1"],
        )

    assert result.exit_code == 0, result.output
    assert "Recommended: SPOT" in result.output
    assert "Reason: lowest cost" in result.output


def test_capacity_recommend_region_json_ignores_verbose_human_output(
    runner: CliRunner,
) -> None:
    checker = MagicMock()
    recommendation = {
        "region": "eu-west-1",
        "reason": "lowest weighted score",
        "all_regions": [
            {
                "region": "eu-west-1",
                "score": 0.125,
                "queue_depth": 1,
                "gpu_utilization": 25.0,
            }
        ],
    }
    checker.recommend_region_for_job.return_value = recommendation

    with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
        result = _invoke(
            runner,
            capacity,
            ["recommend-region", "--gpu", "-i", "p5.48xlarge"],
            config=_config(output_format="json", verbose=True),
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == recommendation
    assert "All regions ranked" not in result.stdout
    assert "✓" not in result.stdout
    assert "ℹ" not in result.stdout


def test_capacity_recommend_region_verbose_prints_ranked_signals(
    runner: CliRunner,
) -> None:
    checker = MagicMock()
    checker.recommend_region_for_job.return_value = {
        "region": "eu-west-1",
        "reason": "lowest weighted score",
        "all_regions": [
            {
                "region": "eu-west-1",
                "score": 0.125,
                "queue_depth": 1,
                "gpu_utilization": 25.0,
            },
            {
                "region": "us-east-1",
                "score": 0.75,
                "queue_depth": 8,
                "gpu_utilization": 80.0,
            },
        ],
    }

    with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
        result = _invoke(
            runner,
            capacity,
            [
                "recommend-region",
                "--gpu",
                "--min-gpus",
                "4",
                "--instance-type",
                "p5.48xlarge",
                "--gpu-count",
                "8",
            ],
            config=_config(verbose=True),
        )

    assert result.exit_code == 0, result.output
    assert "All regions ranked" in result.output
    assert "eu-west-1: score=0.1250, queue=1, gpu=25%" in result.output
    checker.recommend_region_for_job.assert_called_once_with(
        gpu_required=True,
        min_gpus=4,
        instance_type="p5.48xlarge",
        gpu_count=8,
    )


def test_capacity_ai_recommend_forwards_constraints_and_limits_alternatives(
    runner: CliRunner,
) -> None:
    advisor = MagicMock()
    advisor.model_id = "test-model"
    advisor.get_recommendation.return_value = SimpleNamespace(
        recommended_region="us-west-2",
        recommended_instance_type="g6.xlarge",
        recommended_capacity_type="spot",
        confidence="medium",
        cost_estimate=None,
        reasoning=". Capacity is healthy. ",
        alternative_options=[
            {},
            {
                "region": "eu-west-1",
                "instance_type": "g6.xlarge",
                "capacity_type": "on-demand",
                "reason": "stable fallback",
            },
            {
                "region": "us-east-1",
                "instance_type": "g5.xlarge",
                "capacity_type": "spot",
            },
            {
                "region": "ap-south-1",
                "instance_type": "g5.xlarge",
                "capacity_type": "spot",
            },
        ],
        warnings=["Capacity can change", "Validate pricing"],
        raw_response="unused",
    )

    with patch("cli.capacity.get_bedrock_capacity_advisor", return_value=advisor) as factory:
        result = _invoke(
            runner,
            capacity,
            [
                "ai-recommend",
                "--workload",
                "batch inference",
                "--instance-type",
                "g6.xlarge",
                "--region",
                "us-west-2",
                "--min-gpus",
                "2",
                "--min-memory-gb",
                "64",
                "--fault-tolerance",
                "high",
                "--max-cost",
                "4.5",
                "--model",
                "test-model",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "Capacity is healthy." in result.output
    assert "stable fallback" in result.output
    assert "Capacity can change" in result.output
    assert "ap-south-1" not in result.output
    assert "Est. Cost" not in result.output
    factory.assert_called_once_with(factory.call_args.args[0], model_id="test-model")
    advisor.get_recommendation.assert_called_once_with(
        workload_description="batch inference",
        instance_types=["g6.xlarge"],
        regions=["us-west-2"],
        requirements={
            "gpu_required": False,
            "min_gpus": 2,
            "min_memory_gb": 64,
            "fault_tolerance": "high",
            "max_cost_per_hour": 4.5,
        },
    )


def test_capacity_reservations_json_aggregates_explicit_region(runner: CliRunner) -> None:
    checker = MagicMock()
    checker.list_capacity_reservations.return_value = [
        {"total_instances": 3, "available_instances": 1},
        {"total_instances": 2, "available_instances": 2},
    ]

    with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
        result = _invoke(
            runner,
            capacity,
            ["reservations", "--region", "us-east-1", "--instance-type", "p5.48xlarge"],
            config=_config(output_format="json"),
        )

    assert result.exit_code == 0, result.output
    assert '"total_reservations": 2' in result.output
    assert '"total_reserved_instances": 5' in result.output
    assert '"total_available_instances": 3' in result.output
    checker.list_capacity_reservations.assert_called_once_with(
        "us-east-1",
        instance_type="p5.48xlarge",
    )


def test_capacity_reservation_check_renders_empty_blocks_and_forwards_window(
    runner: CliRunner,
) -> None:
    checker = MagicMock()
    checker.check_reservation_availability.return_value = {
        "odcr": {
            "reservations": [],
            "total_available_instances": 0,
            "total_reserved_instances": 0,
        },
        "capacity_blocks": {"duration_hours": 336, "offerings": []},
        "recommendation": "Try another start window",
    }

    with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
        result = _invoke(
            runner,
            capacity,
            [
                "reservation-check",
                "--instance-type",
                "p5.48xlarge",
                "--region",
                "us-east-1",
                "--region",
                "us-west-2",
                "--count",
                "2",
                "--block-duration-days",
                "14",
                "--earliest-start",
                "2026-08-01",
                "--latest-start",
                "2026-08-10",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "min 2 instances" in result.output
    assert "No active ODCRs found" in result.output
    assert "No Capacity Block offerings available" in result.output
    assert "Try another start window" in result.output
    checker.check_reservation_availability.assert_called_once_with(
        instance_type="p5.48xlarge",
        regions=["us-east-1", "us-west-2"],
        min_count=2,
        include_capacity_blocks=True,
        block_duration_hours=24,
        block_duration_days=14,
        earliest_start="2026-08-01",
        latest_start="2026-08-10",
    )
