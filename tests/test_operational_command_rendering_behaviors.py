"""Behavior tests for operational CLI rendering, confirmation, and callback edges."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from click.testing import CliRunner

from cli.commands import autopilot_cmd, cluster_cmd, files_cmd, monitoring_cmd
from cli.commands.autopilot_cmd import _resolve_codex_resume_args, _validate_codex_engine_args
from cli.commands.config_cmd import config_cmd
from cli.commands.dag_cmd import dag
from cli.commands.files_cmd import files
from cli.commands.images_cmd import images
from cli.commands.models_cmd import models
from cli.commands.monitoring_cmd import monitoring
from cli.commands.release_cmd import CONSENT_FLAG, release
from cli.commands.storage_cmd import storage
from cli.commands.templates_cmd import templates
from cli.commands.webhooks_cmd import webhooks
from cli.config import GCOConfig


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _config(
    *,
    output_format: str = "table",
    use_regional_api: bool = False,
) -> GCOConfig:
    return GCOConfig(
        project_name="test-gco",
        default_region="us-east-1",
        global_region="us-east-1",
        api_gateway_region="us-east-1",
        output_format=output_format,
        use_regional_api=use_regional_api,
    )


def _invoke(
    runner: CliRunner,
    group,
    args: list[str],
    *,
    output_format: str = "table",
    input_text: str | None = None,
    use_regional_api: bool = False,
):
    kwargs: dict[str, object] = {
        "obj": _config(
            output_format=output_format,
            use_regional_api=use_regional_api,
        )
    }
    if input_text is not None:
        kwargs["input"] = input_text
    return runner.invoke(group, args, **kwargs)


@pytest.mark.parametrize(
    ("command", "args", "method", "success"),
    [
        (
            "build",
            ["build", ".", "--name", "service"],
            "build",
            "Built and pushed registry/gco/service:v1",
        ),
        (
            "push",
            ["push", "service", "--tag", "v1", "--local-image", "service:v1"],
            "push",
            "Pushed registry/gco/service:v1",
        ),
    ],
)
def test_image_build_and_push_table_results_may_omit_digest(
    runner: CliRunner,
    command: str,
    args: list[str],
    method: str,
    success: str,
) -> None:
    manager = MagicMock()
    getattr(manager, method).return_value = {"image_uri": "registry/gco/service:v1"}

    with patch("cli.images.get_image_manager", return_value=manager):
        result = _invoke(runner, images, args)

    assert result.exit_code == 0, result.output
    assert success in result.output
    assert "Digest:" not in result.output


def test_image_mirror_table_prints_live_summary(runner: CliRunner) -> None:
    mirror_result = {
        "mirrored": ["one", "two"],
        "skipped": ["three"],
        "registry": "123456789012.dkr.ecr.us-east-1.amazonaws.com",
        "strategy": "crane",
    }
    with (
        patch("cli._image_mirror.cdk_default_namespace", return_value="gco/dockerhub"),
        patch("cli._image_mirror.mirror_images", return_value=mirror_result),
    ):
        result = _invoke(runner, images, ["mirror", "--region", "us-east-1"])

    assert result.exit_code == 0, result.output
    assert "Mirrored 2, skipped 1" in result.output
    assert mirror_result["registry"] in result.output
    assert "strategy: crane" in result.output


@pytest.mark.parametrize(
    ("args", "method", "manager_result"),
    [
        (
            ["delete-tag", "service", "v1", "--yes"],
            "delete_tag",
            {"name": "gco/service", "deleted": [{"imageTag": "v1"}]},
        ),
        (
            ["delete-repo", "service", "--force", "--yes"],
            "delete_repo",
            {"name": "gco/service"},
        ),
        (
            ["cleanup", "--name", "service", "--yes"],
            "cleanup",
            {"repos_touched": 1, "tags_deleted": 2, "bytes_freed": 3},
        ),
        (
            ["prune", "--yes"],
            "prune",
            {"repos_touched": 1, "tags_deleted": 2, "bytes_freed": 3},
        ),
    ],
)
def test_destructive_image_commands_emit_structured_results(
    runner: CliRunner,
    args: list[str],
    method: str,
    manager_result: dict[str, object],
) -> None:
    manager = MagicMock()
    getattr(manager, method).return_value = manager_result

    with patch("cli.images.get_image_manager", return_value=manager):
        result = _invoke(runner, images, args, output_format="json")

    assert result.exit_code == 0, result.output
    payload_start = result.output.index("{")
    assert json.loads(result.output[payload_start:]) == manager_result


def test_lifecycle_set_json_emits_updated_policy(runner: CliRunner, tmp_path: Path) -> None:
    policy = {"rules": [{"rulePriority": 1}]}
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(policy), encoding="utf-8")
    manager = MagicMock()
    manager.lifecycle_set.return_value = {
        "name": "gco/service",
        "registry_id": "123456789012",
        "policy": policy,
    }

    with patch("cli.images.get_image_manager", return_value=manager):
        result = _invoke(
            runner,
            images,
            ["lifecycle", "set", "service", "--file", str(policy_file)],
            output_format="json",
        )

    assert result.exit_code == 0, result.output
    payload_start = result.output.index("{")
    assert json.loads(result.output[payload_start:])["policy"] == policy


def test_replication_get_emits_present_policy(runner: CliRunner) -> None:
    manager = MagicMock()
    manager.replication_get.return_value = {"rules": [{"destinations": ["us-west-2"]}]}

    with patch("cli.images.get_image_manager", return_value=manager):
        result = _invoke(runner, images, ["replication", "get"], output_format="json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == manager.replication_get.return_value


def test_codex_resume_token_starting_with_dash_uses_separator() -> None:
    assert _resolve_codex_resume_args(False, "-session-id") == (
        "resume",
        "--",
        "-session-id",
    )


def test_codex_validator_leaves_dangling_or_nonassignment_config_for_codex() -> None:
    _validate_codex_engine_args(("-c",))
    _validate_codex_engine_args(("-c", "not-an-assignment"))


@pytest.mark.parametrize(
    "assignment",
    [
        '"model=broken',
        "mcp_servers.gco.required=not-valid",
        'mcp_servers.gco.required=true\nmodel="other"',
        "mcp_servers.gco=1",
        "mcp_servers.gco.required=false",
        "mcp_servers.gco.enabled_tools=[]",
        'mcp_servers.gco.enabled_tools=["unknown"]',
        'mcp_servers.gco.tools.unknown={approval_mode="approve"}',
        'mcp_servers.gco.tools.find_docs={approval_mode="ask"}',
    ],
)
def test_codex_validator_rejects_malformed_or_broadened_gco_policy(assignment: str) -> None:
    with pytest.raises(ValueError, match="owned by Autopilot|cannot override"):
        _validate_codex_engine_args(("-c", assignment))


@pytest.mark.parametrize("pod_writable", [[], ["models-bucket"]])
def test_storage_inventory_table_renders_summary_and_optional_pod_buckets(
    runner: CliRunner,
    pod_writable: list[str],
) -> None:
    manager = MagicMock()
    manager.s3_inventory.return_value = {
        "project_name": "test-gco",
        "account": "123456789012",
        "summary": {
            "deployed": 1,
            "total": 2,
            "not_deployed": 1,
            "pod_writable": pod_writable,
        },
        "buckets": [
            {
                "id": "model-weights",
                "role": "primary",
                "region": "us-east-1",
                "bucket": "models-bucket",
                "pod_access": "read-write",
                "status": "deployed",
            }
        ],
    }

    with patch("cli.storage.get_storage_manager", return_value=manager):
        result = _invoke(runner, storage, ["s3-inventory"])

    assert result.exit_code == 0, result.output
    assert "S3 inventory — project test-gco, account 123456789012" in result.output
    assert "1/2 deployed" in result.output
    assert "models-bucket" in result.output
    assert ("Pod-writable buckets:" in result.output) is bool(pod_writable)


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_regional_model_upload_renders_selected_output(
    runner: CliRunner,
    output_format: str,
) -> None:
    manager = MagicMock()
    manager.upload.return_value = {
        "files_uploaded": 2,
        "s3_uri": "s3://regional/uploads",
    }

    with patch("cli.models.get_regional_bucket_manager", return_value=manager):
        result = _invoke(
            runner,
            models,
            ["upload-regional", "./data", "--region", "us-west-2"],
            output_format=output_format,
        )

    assert result.exit_code == 0, result.output
    if output_format == "table":
        assert "Uploading ./data to region 'us-west-2'" in result.output
        assert "Uploaded 2 file(s)" in result.output
    else:
        assert json.loads(result.output) == manager.upload.return_value


def test_models_list_json_emits_complete_list(runner: CliRunner) -> None:
    manager = MagicMock()
    models_result = [{"model_name": "llama", "files": 2}]
    manager.list_models.return_value = models_result

    with patch("cli.models.get_model_manager", return_value=manager):
        result = _invoke(runner, models, ["list"], output_format="json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == models_result


def test_models_delete_prompts_before_removing_history(runner: CliRunner) -> None:
    manager = MagicMock()
    manager.delete_model.return_value = 3

    with patch("cli.models.get_model_manager", return_value=manager):
        result = _invoke(
            runner,
            models,
            ["delete", "llama"],
            input_text="y\n",
        )

    assert result.exit_code == 0, result.output
    assert "including all current files and historical S3 versions" in result.output
    manager.delete_model.assert_called_once_with("llama")


def test_templates_list_and_get_emit_non_table_results(runner: CliRunner) -> None:
    client = MagicMock()
    client.call_api.side_effect = [
        {"templates": [{"name": "one"}], "count": 1},
        {"template": {"name": "one", "manifest": {"kind": "Job"}}},
    ]

    with patch("cli.aws_client.get_aws_client", return_value=client):
        listed = _invoke(runner, templates, ["list"], output_format="json")
        fetched = _invoke(runner, templates, ["get", "one"], output_format="json")

    assert listed.exit_code == 0, listed.output
    assert fetched.exit_code == 0, fetched.output
    assert json.loads(listed.output)["count"] == 1
    assert json.loads(fetched.output)["template"]["name"] == "one"


def test_template_get_table_without_parameters_still_prints_manifest(runner: CliRunner) -> None:
    client = MagicMock()
    client.call_api.return_value = {
        "template": {
            "name": "one",
            "description": None,
            "created_at": "2026-01-01",
            "updated_at": "2026-01-02",
            "parameters": {},
            "manifest": {"kind": "Job"},
        }
    }

    with patch("cli.aws_client.get_aws_client", return_value=client):
        result = _invoke(runner, templates, ["get", "one"])

    assert result.exit_code == 0, result.output
    assert "Default Parameters" not in result.output
    assert "Manifest:" in result.output
    assert '"kind": "Job"' in result.output


def test_template_create_ignores_malformed_parameter_and_keeps_valid_one(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "job.yaml"
    manifest.write_text("apiVersion: batch/v1\nkind: Job\n", encoding="utf-8")
    client = MagicMock()
    client.call_api.return_value = {"created": True}

    with patch("cli.aws_client.get_aws_client", return_value=client):
        result = _invoke(
            runner,
            templates,
            [
                "create",
                str(manifest),
                "--name",
                "one",
                "--param",
                "malformed",
                "--param",
                "image=repo:v1",
            ],
        )

    assert result.exit_code == 0, result.output
    assert client.call_api.call_args.kwargs["body"]["parameters"] == {"image": "repo:v1"}


def test_template_delete_prompts_and_template_run_ignores_malformed_override(
    runner: CliRunner,
) -> None:
    client = MagicMock()
    client.call_api.side_effect = [{"deleted": True}, {"success": True}]

    with patch("cli.aws_client.get_aws_client", return_value=client):
        deleted = _invoke(
            runner,
            templates,
            ["delete", "one"],
            input_text="y\n",
        )
        launched = _invoke(
            runner,
            templates,
            [
                "run",
                "one",
                "--name",
                "job-one",
                "--region",
                "us-east-1",
                "--param",
                "malformed",
            ],
        )

    assert deleted.exit_code == 0, deleted.output
    assert "Delete template 'one'?" in deleted.output
    assert launched.exit_code == 0, launched.output
    assert client.call_args_list if False else True
    assert client.call_api.call_args_list[1].kwargs["body"]["parameters"] is None


def test_monitoring_users_list_table_uses_formatter(runner: CliRunner) -> None:
    users = [{"id": 1, "login": "alice"}]
    with (
        patch.object(monitoring_cmd, "_resolve_grafana_auth", return_value=("admin", "secret")),
        patch("cli.monitoring_user_mgmt.list_users", return_value=users),
    ):
        result = _invoke(runner, monitoring, ["users", "list"])

    assert result.exit_code == 0, result.output
    assert "alice" in result.output


def test_monitoring_user_remove_prompts_before_success(runner: CliRunner) -> None:
    with (
        patch.object(monitoring_cmd, "_resolve_grafana_auth", return_value=("admin", "secret")),
        patch("cli.monitoring_user_mgmt.lookup_user_id", return_value=7),
        patch("cli.monitoring_user_mgmt.delete_user") as delete,
    ):
        result = _invoke(
            runner,
            monitoring,
            ["users", "remove", "--username", "alice"],
            input_text="y\n",
        )

    assert result.exit_code == 0, result.output
    assert "Delete Grafana user 'alice'?" in result.output
    delete.assert_called_once()


@pytest.mark.parametrize("error", [RuntimeError("auth"), ValueError("bad user")])
def test_monitoring_user_remove_errors_exit(runner: CliRunner, error: Exception) -> None:
    with (
        patch.object(monitoring_cmd, "_resolve_grafana_auth", side_effect=error),
        patch("cli.monitoring_user_mgmt.lookup_user_id") as lookup,
    ):
        result = _invoke(
            runner,
            monitoring,
            ["users", "remove", "--username", "alice", "--yes"],
        )

    assert result.exit_code == 1
    assert f"Failed to remove Grafana user 'alice': {error}" in result.output
    lookup.assert_not_called()


def test_config_get_without_key_emits_full_config(runner: CliRunner) -> None:
    result = _invoke(runner, config_cmd, ["get"], output_format="json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["project_name"] == "test-gco"
    assert payload["default_region"] == "us-east-1"


def test_config_init_prompts_before_overwriting_existing_file(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    config_path = tmp_path / ".gco" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("old", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    save = Mock()
    monkeypatch.setattr(config, "save", save)

    result = runner.invoke(config_cmd, ["init"], obj=config, input="y\n")

    assert result.exit_code == 0, result.output
    assert "Overwrite?" in result.output
    save.assert_called_once_with(str(config_path))


def _release_inference_args() -> list[str]:
    return [
        "--expected-account",
        "123456789012",
        CONSENT_FLAG,
        "--confirm-kms-key-deletion",
        "--inference-region",
        "us-east-1",
        "--inference-vllm-image",
        "registry.example/vllm@sha256:" + "a" * 64,
        "--inference-vllm-model-id",
        "test/vllm",
        "--inference-vllm-model-revision",
        "b" * 40,
        "--inference-tgi-image",
        "registry.example/tgi@sha256:" + "c" * 64,
        "--inference-tgi-model-id",
        "test/tgi",
        "--inference-tgi-model-revision",
        "d" * 40,
    ]


def test_release_inference_reports_all_missing_required_options(runner: CliRunner) -> None:
    args = _release_inference_args()
    option = args.index("--inference-tgi-model-revision")
    del args[option : option + 2]

    result = runner.invoke(release, ["validate", *args])

    assert result.exit_code == 1
    assert "inference action requires --inference-tgi-model-revision" in result.output


def test_release_inference_rejects_non_lowercase_full_revision(runner: CliRunner) -> None:
    args = _release_inference_args()
    option = args.index("--inference-vllm-model-revision")
    args[option + 1] = "B" * 40

    result = runner.invoke(release, ["validate", *args])

    assert result.exit_code == 1
    assert "--inference-vllm-model-revision must be a full lowercase 40-hex commit" in result.output


def test_files_list_json_emits_nonempty_file_systems(runner: CliRunner) -> None:
    client = MagicMock()
    client.get_file_systems.return_value = [
        {
            "file_system_id": "fs-123",
            "file_system_type": "efs",
            "region": "us-east-1",
        }
    ]

    with patch.object(files_cmd, "get_file_system_client", return_value=client):
        result = _invoke(
            runner,
            files,
            ["list", "--region", "us-east-1"],
            output_format="json",
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["file_system_id"] == "fs-123"
    client.get_file_systems.assert_called_once_with("us-east-1")


def test_dag_run_ignores_unknown_progress_and_returns_success(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    dag_file = tmp_path / "dag.yaml"
    dag_file.write_text("name: demo\nsteps: []\n", encoding="utf-8")
    dag_definition = SimpleNamespace(name="demo", steps=[], validate=lambda: [])
    completed = SimpleNamespace(has_failures=lambda: False)
    fake_runner = MagicMock()

    def run(_dag, **kwargs):
        kwargs["progress_callback"]("step", "unknown", "must not print")
        return completed

    fake_runner.run.side_effect = run
    with (
        patch("cli.dag.load_dag", return_value=dag_definition),
        patch("cli.dag.get_dag_runner", return_value=fake_runner),
    ):
        result = _invoke(runner, dag, ["run", str(dag_file)])

    assert result.exit_code == 0, result.output
    assert "must not print" not in result.output


def test_private_cluster_plan_without_note_renders_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "cluster": "test-gco-us-east-1",
        "region": "us-east-1",
        "reachable": "private",
        "update_kubeconfig": ["aws", "eks", "update-kubeconfig"],
        "ssm_command_str": "aws ssm start-session",
        "kubectl_example": "kubectl get pods",
        "note": "",
    }

    cluster_cmd._echo_plan_human(payload)

    output = capsys.readouterr().out
    assert "PRIVATE endpoint" in output
    assert "aws ssm start-session" in output
    assert "kubectl get pods" in output
    assert output.rstrip().endswith("kubectl get pods")


def test_webhooks_list_json_and_get_match_emit_api_results(runner: CliRunner) -> None:
    client = MagicMock()
    client.call_api.side_effect = [
        {"webhooks": [{"id": "hook-1"}], "count": 1},
        {"webhooks": [{"id": "hook-1", "url": "https://example.test"}]},
    ]

    with patch("cli.aws_client.get_aws_client", return_value=client):
        listed = _invoke(runner, webhooks, ["list"], output_format="json")
        fetched = _invoke(runner, webhooks, ["get", "hook-1"], output_format="json")

    assert listed.exit_code == 0, listed.output
    assert fetched.exit_code == 0, fetched.output
    assert json.loads(listed.output)["count"] == 1
    assert json.loads(fetched.output)["id"] == "hook-1"


def test_webhook_delete_prompts_before_api_call(runner: CliRunner) -> None:
    client = MagicMock()
    client.call_api.return_value = {"deleted": True}

    with patch("cli.aws_client.get_aws_client", return_value=client):
        result = _invoke(
            runner,
            webhooks,
            ["delete", "hook-1"],
            input_text="y\n",
        )

    assert result.exit_code == 0, result.output
    assert "Delete webhook 'hook-1'?" in result.output
    client.call_api.assert_called_once_with(
        method="DELETE",
        path="/api/v1/webhooks/hook-1",
        region=None,
    )


def test_replication_status_emits_present_rows(runner: CliRunner) -> None:
    manager = MagicMock()
    rows = [{"repository": "gco/service", "tag": "v1", "region": "us-west-2"}]
    manager.replication_status.return_value = rows

    with patch("cli.images.get_image_manager", return_value=manager):
        result = _invoke(runner, images, ["replication", "status"], output_format="json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == rows


def test_codex_launch_without_reasoning_omits_reasoning_detail(runner: CliRunner) -> None:
    plan = {
        "engine": "codex",
        "codex_binary": "/tmp/bin/codex",
        "codex_pin": "1.0.0",
        "install_command": "npm install codex",
        "codex_config": "model_provider='amazon-bedrock-runtime'\n",
        "region": "us-east-1",
        "model": "global.example.model",
        "reasoning_effort": None,
        "workspace": "/workspace",
    }

    with (
        patch.object(autopilot_cmd, "_plan", return_value=(plan, [])),
        patch.object(autopilot_cmd, "write_codex_config"),
        patch.object(autopilot_cmd, "stage_codex_skills"),
        patch.object(autopilot_cmd, "build_codex_env", return_value={}),
        patch.object(autopilot_cmd, "build_codex_owned_args", return_value=()),
        patch.object(
            autopilot_cmd,
            "build_codex_launch_argv",
            return_value=["/tmp/bin/codex"],
        ),
        patch.object(autopilot_cmd, "exec_codex", return_value=0) as execute,
    ):
        result = runner.invoke(
            autopilot_cmd.autopilot,
            ["--engine", "codex"],
            obj=_config(),
        )

    assert result.exit_code == 0, result.output
    assert "Launching Codex on Bedrock" in result.output
    assert "provider=amazon-bedrock-runtime" in result.output
    assert "reasoning=" not in result.output
    execute.assert_called_once_with(["/tmp/bin/codex"], {})


def test_replication_sync_emits_structured_result(runner: CliRunner) -> None:
    manager = MagicMock()
    result_payload = {"destinations": ["us-west-2"], "updated": True}
    manager.replication_sync.return_value = result_payload

    with patch("cli.images.get_image_manager", return_value=manager):
        result = _invoke(runner, images, ["replication", "sync"], output_format="json")

    assert result.exit_code == 0, result.output
    payload_start = result.output.index("{")
    assert json.loads(result.output[payload_start:]) == result_payload
