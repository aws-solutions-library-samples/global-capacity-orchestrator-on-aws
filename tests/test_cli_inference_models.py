"""
Tests for the inference and models CLI subgroups in cli/main.py.

Drives `gco inference deploy` with regions, env vars, labels, and
autoscaling flags, plus the models commands, through CliRunner
against a mocked InferenceManager and AWS client. An autouse fixture
patches cli.main.get_config so nothing tries to read a real cdk.json
during tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.main import cli


@pytest.fixture
def runner():
    return CliRunner()


# Patch config loading for all tests
@pytest.fixture(autouse=True)
def mock_config():
    mock_cfg = MagicMock()
    mock_cfg.output_format = "table"
    mock_cfg.global_region = "us-east-2"
    mock_cfg.project_name = "gco"
    with patch("cli.main.get_config", return_value=mock_cfg):
        yield mock_cfg


# =============================================================================
# inference deploy
# =============================================================================


class TestInferenceDeploy:
    def test_deploy_basic(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.deploy.return_value = {
            "endpoint_name": "my-ep",
            "target_regions": ["us-east-1"],
            "ingress_path": "/inference/my-ep",
        }
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "deploy", "my-ep", "-i", "vllm/vllm:v1"])
        assert result.exit_code == 0
        assert "registered for deployment" in result.output

    def test_deploy_with_regions(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.deploy.return_value = {
            "endpoint_name": "ep",
            "target_regions": ["us-east-1", "eu-west-1"],
            "ingress_path": "/inference/ep",
        }
        mock_client = MagicMock()
        mock_client.discover_regional_stacks.return_value = {
            "us-east-1": {},
            "eu-west-1": {},
        }
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(
                cli,
                ["inference", "deploy", "ep", "-i", "img:v1", "-r", "us-east-1", "-r", "eu-west-1"],
            )
        assert result.exit_code == 0

    def test_deploy_with_env_and_labels(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.deploy.return_value = {
            "endpoint_name": "ep",
            "target_regions": ["us-east-1"],
            "ingress_path": "/inference/ep",
        }
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                [
                    "inference",
                    "deploy",
                    "ep",
                    "-i",
                    "img:v1",
                    "-e",
                    "KEY=VAL",
                    "-l",
                    "team=ml",
                    "--min-replicas",
                    "1",
                    "--max-replicas",
                    "10",
                    "--autoscale-metric",
                    "cpu:70",
                ],
            )
        assert result.exit_code == 0
        call_kwargs = mock_mgr.deploy.call_args.kwargs
        assert call_kwargs["env"] == {"KEY": "VAL"}
        assert call_kwargs["labels"] == {"team": "ml"}
        assert call_kwargs["autoscaling"]["enabled"] is True

    def test_deploy_autoscale_metric_no_target(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.deploy.return_value = {
            "endpoint_name": "ep",
            "target_regions": ["us-east-1"],
            "ingress_path": "/inference/ep",
        }
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                ["inference", "deploy", "ep", "-i", "img:v1", "--autoscale-metric", "memory"],
            )
        assert result.exit_code == 0
        call_kwargs = mock_mgr.deploy.call_args.kwargs
        assert call_kwargs["autoscaling"]["metrics"][0]["target"] == 70

    def test_deploy_warns_subset_regions(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.deploy.return_value = {
            "endpoint_name": "ep",
            "target_regions": ["us-east-1"],
            "ingress_path": "/inference/ep",
        }
        mock_client = MagicMock()
        mock_client.discover_regional_stacks.return_value = {
            "us-east-1": {},
            "eu-west-1": {},
        }
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(
                cli, ["inference", "deploy", "ep", "-i", "img:v1", "-r", "us-east-1"]
            )
        assert result.exit_code == 0
        assert "NOT deployed to" in result.output or "eu-west-1" in result.output

    def test_deploy_value_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.deploy.side_effect = ValueError("No deployed regions")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "deploy", "ep", "-i", "img:v1"])
        assert result.exit_code != 0

    def test_deploy_generic_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.deploy.side_effect = RuntimeError("boom")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "deploy", "ep", "-i", "img:v1"])
        assert result.exit_code != 0

    def test_deploy_with_extra_args(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.deploy.return_value = {
            "endpoint_name": "ep",
            "target_regions": ["us-east-1"],
            "ingress_path": "/inference/ep",
        }
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                [
                    "inference",
                    "deploy",
                    "ep",
                    "-i",
                    "vllm/vllm:v1",
                    "--extra-args",
                    "--kv-transfer-config",
                    "--extra-args",
                    '{"kv_connector":"P2pNcclConnector"}',
                ],
            )
        assert result.exit_code == 0
        call_kwargs = mock_mgr.deploy.call_args.kwargs
        assert call_kwargs["extra_args"] == [
            "--kv-transfer-config",
            '{"kv_connector":"P2pNcclConnector"}',
        ]

    def test_deploy_without_extra_args(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.deploy.return_value = {
            "endpoint_name": "ep",
            "target_regions": ["us-east-1"],
            "ingress_path": "/inference/ep",
        }
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "deploy", "ep", "-i", "img:v1"])
        assert result.exit_code == 0
        call_kwargs = mock_mgr.deploy.call_args.kwargs
        assert call_kwargs["extra_args"] is None


# =============================================================================
# inference list
# =============================================================================


class TestInferenceList:
    def test_list_empty(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.list_endpoints.return_value = []
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "list"])
        assert result.exit_code == 0
        assert "No inference endpoints" in result.output

    def test_list_with_results(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.list_endpoints.return_value = [
            {
                "endpoint_name": "ep1",
                "desired_state": "running",
                "target_regions": ["us-east-1"],
                "spec": {"image": "img:v1", "replicas": 2},
            }
        ]
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "list"])
        assert result.exit_code == 0
        assert "ep1" in result.output
        assert "running" in result.output

    def test_list_with_filters(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.list_endpoints.return_value = []
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli, ["inference", "list", "--state", "running", "-r", "us-east-1"]
            )
        assert result.exit_code == 0

    def test_list_json_output(self, runner, mock_config):
        mock_mgr = MagicMock()
        mock_mgr.list_endpoints.return_value = [{"endpoint_name": "ep"}]
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["-o", "json", "inference", "list"])
        assert result.exit_code == 0

    def test_list_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.list_endpoints.side_effect = RuntimeError("fail")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "list"])
        assert result.exit_code != 0


# =============================================================================
# inference status
# =============================================================================


class TestInferenceStatus:
    def test_status_found(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "desired_state": "running",
            "spec": {"image": "img:v1", "replicas": 2, "gpu_count": 1, "port": 8000},
            "ingress_path": "/inference/ep",
            "namespace": "gco-inference",
            "created_at": "2025-01-01",
            "region_status": {
                "us-east-1": {
                    "state": "running",
                    "replicas_ready": 2,
                    "replicas_desired": 2,
                    "last_sync": "2025-01-01T00:00:00.000000+00:00",
                }
            },
        }
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "status", "ep"])
        assert result.exit_code == 0
        assert "running" in result.output
        assert "us-east-1" in result.output

    def test_status_no_region_status(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "desired_state": "deploying",
            "spec": {"image": "img:v1"},
            "target_regions": ["us-east-1"],
            "region_status": {},
        }
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "status", "ep"])
        assert result.exit_code == 0
        assert "Waiting for inference_monitor" in result.output

    def test_status_not_found(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = None
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "status", "ghost"])
        assert result.exit_code != 0

    def test_status_json_output(self, runner, mock_config):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = {"endpoint_name": "ep", "spec": {}}
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["-o", "json", "inference", "status", "ep"])
        assert result.exit_code == 0

    def test_status_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.side_effect = RuntimeError("fail")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "status", "ep"])
        assert result.exit_code != 0


# =============================================================================
# inference scale
# =============================================================================


class TestInferenceScale:
    def test_scale_success(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.scale.return_value = {"endpoint_name": "ep"}
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "scale", "ep", "-r", "4"])
        assert result.exit_code == 0
        assert "scaled to 4" in result.output

    def test_scale_not_found(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.scale.return_value = None
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "scale", "ep", "-r", "4"])
        assert result.exit_code != 0

    def test_scale_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.scale.side_effect = RuntimeError("fail")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "scale", "ep", "-r", "4"])
        assert result.exit_code != 0


# =============================================================================
# inference stop
# =============================================================================


class TestInferenceStop:
    def test_stop_success(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.stop.return_value = {"desired_state": "stopped"}
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "stop", "ep", "-y"])
        assert result.exit_code == 0
        assert "marked for stop" in result.output

    def test_stop_not_found(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.stop.return_value = None
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "stop", "ep", "-y"])
        assert result.exit_code != 0

    def test_stop_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.stop.side_effect = RuntimeError("fail")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "stop", "ep", "-y"])
        assert result.exit_code != 0


# =============================================================================
# inference start
# =============================================================================


class TestInferenceStart:
    def test_start_success(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.start.return_value = {"desired_state": "running"}
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "start", "ep"])
        assert result.exit_code == 0
        assert "marked for start" in result.output

    def test_start_not_found(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.start.return_value = None
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "start", "ghost"])
        assert result.exit_code != 0

    def test_start_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.start.side_effect = RuntimeError("fail")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "start", "ep"])
        assert result.exit_code != 0


# =============================================================================
# inference delete
# =============================================================================


class TestInferenceDelete:
    def test_delete_success(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.delete.return_value = {"desired_state": "deleted"}
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "delete", "ep", "-y"])
        assert result.exit_code == 0
        assert "marked for deletion" in result.output

    def test_delete_not_found(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.delete.return_value = None
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "delete", "ep", "-y"])
        assert result.exit_code != 0

    def test_delete_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.delete.side_effect = RuntimeError("fail")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "delete", "ep", "-y"])
        assert result.exit_code != 0


# =============================================================================
# inference update-image
# =============================================================================


class TestInferenceUpdateImage:
    def test_update_image_success(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.update_image.return_value = {"endpoint_name": "ep"}
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "update-image", "ep", "-i", "new:v2"])
        assert result.exit_code == 0
        assert "image updated" in result.output

    def test_update_image_not_found(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.update_image.return_value = None
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "update-image", "ep", "-i", "new:v2"])
        assert result.exit_code != 0

    def test_update_image_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.update_image.side_effect = RuntimeError("fail")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "update-image", "ep", "-i", "new:v2"])
        assert result.exit_code != 0


# =============================================================================
# models upload
# =============================================================================


class TestModelsUpload:
    def test_upload_success(self, runner, tmp_path):
        f = tmp_path / "model.bin"
        f.write_text("data")
        mock_mgr = MagicMock()
        mock_mgr.upload.return_value = {
            "files_uploaded": 1,
            "s3_uri": "s3://bucket/models/m",
        }
        with patch("cli.models.get_model_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["models", "upload", str(f), "-n", "my-model"])
        assert result.exit_code == 0
        assert "Uploaded 1 file" in result.output

    def test_upload_error(self, runner, tmp_path):
        f = tmp_path / "model.bin"
        f.write_text("data")
        mock_mgr = MagicMock()
        mock_mgr.upload.side_effect = RuntimeError("S3 error")
        with patch("cli.models.get_model_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["models", "upload", str(f), "-n", "my-model"])
        assert result.exit_code != 0


# =============================================================================
# models list
# =============================================================================


class TestModelsList:
    def test_list_empty(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.list_models.return_value = []
        with patch("cli.models.get_model_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["models", "list"])
        assert result.exit_code == 0
        assert "No models found" in result.output

    def test_list_with_results(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.list_models.return_value = [
            {
                "model_name": "llama3",
                "files": 5,
                "total_size_gb": 14.5,
                "s3_uri": "s3://bucket/models/llama3",
            }
        ]
        with patch("cli.models.get_model_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["models", "list"])
        assert result.exit_code == 0
        assert "llama3" in result.output

    def test_list_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.list_models.side_effect = RuntimeError("fail")
        with patch("cli.models.get_model_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["models", "list"])
        assert result.exit_code != 0


# =============================================================================
# models delete
# =============================================================================


class TestModelsDelete:
    def test_delete_success(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.delete_model.return_value = 5
        with patch("cli.models.get_model_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["models", "delete", "llama3", "-y"])
        assert result.exit_code == 0
        assert "Deleted 5 file" in result.output

    def test_delete_no_files(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.delete_model.return_value = 0
        with patch("cli.models.get_model_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["models", "delete", "empty", "-y"])
        assert result.exit_code == 0
        assert "No files found" in result.output

    def test_delete_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.delete_model.side_effect = RuntimeError("fail")
        with patch("cli.models.get_model_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["models", "delete", "m", "-y"])
        assert result.exit_code != 0


# =============================================================================
# models uri
# =============================================================================


class TestModelsUri:
    def test_uri_success(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_model_uri.return_value = "s3://bucket/models/llama3"
        with patch("cli.models.get_model_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["models", "uri", "llama3"])
        assert result.exit_code == 0
        assert "s3://bucket/models/llama3" in result.output

    def test_uri_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_model_uri.side_effect = RuntimeError("fail")
        with patch("cli.models.get_model_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["models", "uri", "m"])
        assert result.exit_code != 0


# =============================================================================
# inference invoke
# =============================================================================


class TestInferenceInvoke:
    def _mock_endpoint(self, image="vllm/vllm-openai:v0.8.0", env=None):
        return {
            "endpoint_name": "ep",
            "ingress_path": "/inference/ep",
            "spec": {"image": image, "env": env or {}},
        }

    def test_invoke_with_prompt_vllm(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"choices": [{"text": "GPU orchestration is cool."}]}
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "invoke", "ep", "-p", "What is GPU?"])
        assert result.exit_code == 0
        assert "GPU orchestration is cool" in result.output

    def test_invoke_with_prompt_tgi(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint(
            image="ghcr.io/huggingface/text-generation-inference:3.2"
        )
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = [{"generated_text": "TGI response"}]
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "invoke", "ep", "-p", "Hello"])
        assert result.exit_code == 0
        assert "TGI response" in result.output

    def test_invoke_with_raw_data(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"choices": [{"text": "raw response"}]}
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(
                cli,
                [
                    "inference",
                    "invoke",
                    "ep",
                    "-d",
                    '{"prompt": "Hi", "max_tokens": 10}',
                ],
            )
        assert result.exit_code == 0

    def test_invoke_with_explicit_path(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"result": "ok"}
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(
                cli,
                [
                    "inference",
                    "invoke",
                    "ep",
                    "-p",
                    "test",
                    "--path",
                    "/v1/chat/completions",
                ],
            )
        assert result.exit_code == 0
        call_args = mock_client.make_authenticated_request.call_args
        assert "/v1/chat/completions" in call_args.kwargs["path"]

    def test_invoke_no_prompt_or_data(self, runner):
        result = runner.invoke(cli, ["inference", "invoke", "ep"])
        assert result.exit_code != 0
        assert "Provide --prompt" in result.output

    def test_invoke_stream_flag_is_rejected_before_endpoint_lookup(self, runner):
        mock_mgr_factory = MagicMock()
        with patch("cli.inference.get_inference_manager", mock_mgr_factory):
            result = runner.invoke(
                cli,
                ["inference", "invoke", "ep", "-p", "hello", "--stream"],
            )

        assert result.exit_code != 0
        assert (
            "--stream is not supported: API Gateway and Lambda buffer inference responses"
            in result.output
        )
        mock_mgr_factory.assert_not_called()

    def test_invoke_raw_stream_true_is_rejected_before_request(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(
                cli,
                [
                    "inference",
                    "invoke",
                    "ep",
                    "-d",
                    '{"prompt": "hello", "stream": true}',
                ],
            )

        assert result.exit_code != 0
        assert (
            "Streaming requests are not supported: API Gateway and Lambda buffer responses"
            in result.output
        )
        mock_client.make_authenticated_request.assert_not_called()

    def test_invoke_endpoint_not_found(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = None
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "invoke", "ghost", "-p", "hi"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_invoke_http_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 502
        mock_resp.text = "Bad Gateway"
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "invoke", "ep", "-p", "hi"])
        assert result.exit_code != 0
        assert "502" in result.output

    def test_invoke_non_json_response(self, runner):
        import json

        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.side_effect = json.JSONDecodeError("not json", "", 0)
        mock_resp.text = "plain text response"
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "invoke", "ep", "-p", "hi"])
        assert result.exit_code == 0
        assert "plain text response" in result.output

    def test_invoke_triton_auto_path(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint(
            image="nvcr.io/nvidia/tritonserver:25.01-py3"
        )
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"models": []}
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "invoke", "ep", "-p", "test"])
        assert result.exit_code == 0
        call_args = mock_client.make_authenticated_request.call_args
        assert "/v2/models" in call_args.kwargs["path"]

    def test_invoke_openai_message_format(self, runner):
        """Test extraction of chat completion message format."""
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"choices": [{"message": {"content": "Chat response here"}}]}
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "invoke", "ep", "-p", "hi"])
        assert result.exit_code == 0
        assert "Chat response here" in result.output

    def test_invoke_exception(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.side_effect = RuntimeError("boom")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "invoke", "ep", "-p", "hi"])
        assert result.exit_code != 0
        assert "Failed to invoke" in result.output


class TestInferenceHealth:
    def _mock_endpoint(self, health_path="/health"):
        return {
            "endpoint_name": "ep",
            "ingress_path": "/inference/ep",
            "spec": {
                "image": "vllm/vllm-openai:v0.8.0",
                "health_check_path": health_path,
            },
        }

    def test_health_healthy(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = None
        mock_resp.text = ""
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "health", "ep"])
        assert result.exit_code == 0
        assert "healthy" in result.output

    def test_health_unhealthy(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 503
        mock_resp.json.side_effect = Exception("no json")
        mock_resp.text = "Service Unavailable"
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "health", "ep"])
        assert result.exit_code == 0
        assert "unhealthy" in result.output

    def test_health_endpoint_not_found(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = None
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "health", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_health_with_region(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = None
        mock_resp.text = ""
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "health", "ep", "-r", "us-east-1"])
        assert result.exit_code == 0
        call_kwargs = mock_client.make_authenticated_request.call_args.kwargs
        assert call_kwargs["target_region"] == "us-east-1"

    def test_health_custom_health_path(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint(health_path="/v2/health/ready")
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = None
        mock_resp.text = ""
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "health", "ep"])
        assert result.exit_code == 0
        call_kwargs = mock_client.make_authenticated_request.call_args.kwargs
        assert "/v2/health/ready" in call_kwargs["path"]

    def test_health_exception(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.side_effect = RuntimeError("boom")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "health", "ep"])
        assert result.exit_code != 0
        assert "Health check failed" in result.output

    def test_health_json_output(self, runner, mock_config):
        mock_config.output_format = "json"
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = None
        mock_resp.text = ""
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["-o", "json", "inference", "health", "ep"])
        assert result.exit_code == 0
        # Table format shows status icon and latency (autouse mock_config forces table)
        assert "healthy" in result.output
        assert "HTTP 200" in result.output


class TestInferenceModels:
    def _mock_endpoint(self):
        return {
            "endpoint_name": "ep",
            "ingress_path": "/inference/ep",
            "spec": {"image": "vllm/vllm-openai:v0.8.0"},
        }

    def test_models_success(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "object": "list",
            "data": [{"id": "facebook/opt-125m", "object": "model"}],
        }
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "models", "ep"])
        assert result.exit_code == 0
        assert "facebook/opt-125m" in result.output
        call_kwargs = mock_client.make_authenticated_request.call_args.kwargs
        assert "/v1/models" in call_kwargs["path"]

    def test_models_endpoint_not_found(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = None
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "models", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_models_http_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 404
        mock_resp.text = "Not Found"
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "models", "ep"])
        assert result.exit_code != 0
        assert "HTTP 404" in result.output

    def test_models_with_region(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"object": "list", "data": []}
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "models", "ep", "-r", "eu-west-1"])
        assert result.exit_code == 0
        call_kwargs = mock_client.make_authenticated_request.call_args.kwargs
        assert call_kwargs["target_region"] == "eu-west-1"

    def test_models_non_json_response(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.side_effect = __import__("json").JSONDecodeError("not json", "", 0)
        mock_resp.text = "plain text response"
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "models", "ep"])
        assert result.exit_code == 0
        assert "plain text response" in result.output

    def test_models_exception(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.side_effect = RuntimeError("boom")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "models", "ep"])
        assert result.exit_code != 0
        assert "Failed to list models" in result.output
