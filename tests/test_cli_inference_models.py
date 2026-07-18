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

from cli.config import GCOConfig
from cli.main import cli


@pytest.fixture
def runner():
    return CliRunner()


# Patch config loading for all tests with the concrete type Click passes to subcommands.
@pytest.fixture(autouse=True)
def mock_config():
    config = GCOConfig()
    with patch("cli.main.get_config", return_value=config):
        yield config


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

    def test_invoke_stream_flag_streams_openai_response(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock(ok=True, status_code=200, encoding="utf-8")
        mock_resp.iter_content.return_value = [b'data: {"token":"hel', b'lo"}\n\n']
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(
                cli,
                ["inference", "invoke", "ep", "-p", "hello", "--stream"],
            )

        assert result.exit_code == 0
        assert 'data: {"token":"hello"}' in result.output
        call_kwargs = mock_client.make_authenticated_request.call_args.kwargs
        assert call_kwargs["stream"] is True
        assert call_kwargs["body"]["stream"] is True
        mock_resp.close.assert_called_once_with()

    def test_invoke_raw_stream_true_auto_enables_transport(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock(ok=True, status_code=200, encoding="utf-8")
        mock_resp.iter_content.return_value = [b"data: first\n\n", b"data: second\n\n"]
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
                    '{"prompt": "hello", "stream": true}',
                ],
            )

        assert result.exit_code == 0
        assert "data: first" in result.output
        call_kwargs = mock_client.make_authenticated_request.call_args.kwargs
        assert call_kwargs["stream"] is True
        assert call_kwargs["body"]["stream"] is True
        mock_resp.close.assert_called_once_with()

    def test_invoke_no_stream_forces_raw_body_false(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint()
        mock_client = MagicMock()
        mock_resp = MagicMock(ok=True, status_code=200)
        mock_resp.json.return_value = {"choices": [{"text": "buffered"}]}
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
                    '{"prompt": "hello", "stream": true}',
                    "--no-stream",
                ],
            )

        assert result.exit_code == 0
        call_kwargs = mock_client.make_authenticated_request.call_args.kwargs
        assert call_kwargs["stream"] is False
        assert call_kwargs["body"]["stream"] is False

    def test_invoke_tgi_stream_uses_generate_stream_path(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._mock_endpoint(
            image="ghcr.io/huggingface/text-generation-inference:3.2"
        )
        mock_client = MagicMock()
        mock_resp = MagicMock(ok=True, status_code=200, encoding="utf-8")
        mock_resp.iter_content.return_value = [b'{"token":{"text":"hello"}}\n']
        mock_client.make_authenticated_request.return_value = mock_resp
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(
                cli,
                ["inference", "invoke", "ep", "-p", "hello", "--stream"],
            )

        assert result.exit_code == 0
        call_kwargs = mock_client.make_authenticated_request.call_args.kwargs
        assert call_kwargs["path"].endswith("/generate_stream")
        assert call_kwargs["stream"] is True

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
        assert '"status": "healthy"' in result.output
        assert '"http_status": 200' in result.output


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


# =============================================================================
# Remaining inference command behavior
# =============================================================================


class TestInferenceInvokeRemaining:
    @staticmethod
    def _endpoint(image="vllm/vllm-openai:v0.8.0", **spec_updates):
        spec = {"image": image, "env": {}}
        spec.update(spec_updates)
        return {
            "endpoint_name": "ep",
            "ingress_path": "/inference/ep",
            "spec": spec,
        }

    @staticmethod
    def _buffered_response(payload):
        response = MagicMock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = payload
        return response

    @staticmethod
    def _stream_response(chunks, content_type="text/event-stream; charset=utf-8"):
        response = MagicMock()
        response.ok = True
        response.status_code = 200
        response.headers = {"content-type": content_type}
        response.iter_content.return_value = chunks
        return response

    def _invoke_stream(self, runner, response):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint(env={"MODEL": "org/model"})
        mock_client = MagicMock()
        mock_client.make_authenticated_request.return_value = response
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(
                cli,
                ["inference", "invoke", "ep", "--prompt", "hello", "--stream"],
            )
        return result, mock_client

    @pytest.mark.parametrize(
        ("raw_data", "expected_error"),
        [
            ("{not-json", "Failed to invoke endpoint"),
            ('["not", "an", "object"]', "--data must contain a JSON object"),
        ],
    )
    def test_invoke_rejects_invalid_raw_json(self, runner, raw_data, expected_error):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint()
        mock_client = MagicMock()
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(
                cli,
                ["inference", "invoke", "ep", "--data", raw_data],
            )

        assert result.exit_code != 0
        assert expected_error in result.output
        mock_client.make_authenticated_request.assert_not_called()

    @pytest.mark.parametrize(
        ("spec_updates", "expected_model"),
        [
            ({"env": {"MODEL": "org/env-model"}}, "org/env-model"),
            (
                {"args": ["--dtype", "auto", "--model", "org/argument-model"]},
                "org/argument-model",
            ),
        ],
    )
    def test_invoke_selects_model_from_stored_spec(self, runner, spec_updates, expected_model):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint(**spec_updates)
        mock_client = MagicMock()
        mock_client.make_authenticated_request.return_value = self._buffered_response(
            {"choices": [{"text": "done"}]}
        )
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "invoke", "ep", "-p", "hello"])

        assert result.exit_code == 0
        assert mock_client.make_authenticated_request.call_count == 1
        request = mock_client.make_authenticated_request.call_args.kwargs
        assert request["body"]["model"] == expected_model

    @pytest.mark.parametrize(
        ("discovery_outcome", "expected_model"),
        [
            ("success", "org/discovered-model"),
            ("empty", "ep"),
            ("malformed", "ep"),
            ("failed", "ep"),
        ],
    )
    def test_invoke_vllm_model_discovery_falls_back_safely(
        self, runner, discovery_outcome, expected_model
    ):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint()

        discovery_response = MagicMock()
        discovery_response.ok = discovery_outcome != "failed"
        discovery_response.status_code = 503 if discovery_outcome == "failed" else 200
        if discovery_outcome == "success":
            discovery_response.json.return_value = {"data": [{"id": "org/discovered-model"}]}
        elif discovery_outcome == "empty":
            discovery_response.json.return_value = {"data": []}
        elif discovery_outcome == "malformed":
            discovery_response.json.side_effect = __import__("json").JSONDecodeError(
                "malformed models response", "", 0
            )

        invoke_response = self._buffered_response({"choices": [{"text": "done"}]})
        mock_client = MagicMock()
        mock_client.make_authenticated_request.side_effect = [
            discovery_response,
            invoke_response,
        ]
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "invoke", "ep", "-p", "hello"])

        assert result.exit_code == 0
        discovery_request, invoke_request = mock_client.make_authenticated_request.call_args_list
        assert discovery_request.kwargs["method"] == "GET"
        assert discovery_request.kwargs["path"] == "/inference/ep/v1/models"
        assert invoke_request.kwargs["body"]["model"] == expected_model

    def test_invoke_unknown_image_uses_openai_fallback(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint(image="example/custom-server:v1")
        mock_client = MagicMock()
        mock_client.make_authenticated_request.return_value = self._buffered_response(
            {"choices": [{"text": "fallback"}]}
        )
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "invoke", "ep", "-p", "hello"])

        assert result.exit_code == 0
        assert mock_client.make_authenticated_request.call_count == 1
        request = mock_client.make_authenticated_request.call_args.kwargs
        assert request["path"] == "/inference/ep/v1/completions"
        assert request["body"]["model"] == "ep"

    @pytest.mark.parametrize(
        ("payload", "expected_output"),
        [
            ({"generated_text": "dict result"}, "dict result"),
            ([{"generated_text": "list result"}], "list result"),
            ({"generated_text": ""}, '"generated_text": ""'),
        ],
    )
    def test_invoke_buffered_generated_text_shapes(self, runner, payload, expected_output):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint(image="tgi:latest")
        mock_client = MagicMock()
        mock_client.make_authenticated_request.return_value = self._buffered_response(payload)
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_client),
        ):
            result = runner.invoke(cli, ["inference", "invoke", "ep", "-p", "hello"])

        assert result.exit_code == 0
        assert expected_output in result.output

    def test_invoke_stream_http_error_closes_response(self, runner):
        response = MagicMock()
        response.ok = False
        response.status_code = 503
        response.text = "service unavailable"
        response.headers = {}

        result, _ = self._invoke_stream(runner, response)

        assert result.exit_code != 0
        assert "HTTP 503" in result.output
        response.close.assert_called_once_with()

    def test_invoke_stream_honors_explicit_charset(self, runner):
        response = self._stream_response(
            ["snowman: ☃".encode("utf-16-le")],
            content_type="text/event-stream; charset=utf-16-le",
        )

        result, _ = self._invoke_stream(runner, response)

        assert result.exit_code == 0
        assert "snowman: ☃" in result.output
        response.close.assert_called_once_with()

    def test_invoke_stream_invalid_charset_falls_back_to_utf8(self, runner):
        response = self._stream_response(
            ["café".encode()],
            content_type="text/event-stream; charset=not-a-real-charset",
        )

        result, _ = self._invoke_stream(runner, response)

        assert result.exit_code == 0
        assert "café" in result.output
        response.close.assert_called_once_with()

    def test_invoke_stream_accepts_str_and_skips_empty_chunks(self, runner):
        response = self._stream_response([b"", "", "first", b" second"])

        result, _ = self._invoke_stream(runner, response)

        assert result.exit_code == 0
        assert "first second" in result.output
        response.close.assert_called_once_with()

    def test_invoke_stream_decodes_split_multibyte_and_flushes_remainder(self, runner):
        response = self._stream_response([b"caf\xc3", b"\xa9 tail", b"\xe2\x82"])

        result, _ = self._invoke_stream(runner, response)

        assert result.exit_code == 0
        assert "café tail�" in result.output
        response.close.assert_called_once_with()

    def test_invoke_stream_iteration_error_closes_response(self, runner):
        response = self._stream_response([])
        response.iter_content.side_effect = RuntimeError("stream interrupted")

        result, _ = self._invoke_stream(runner, response)

        assert result.exit_code != 0
        assert "Failed to invoke endpoint: stream interrupted" in result.output
        response.close.assert_called_once_with()


class TestInferenceCanaryTransitions:
    CASES = [
        (
            "promote",
            "promote_canary",
            "Promoted: all traffic now serving canary:v2",
            "Promotion failed",
            "Failed to promote canary",
        ),
        (
            "rollback",
            "rollback_canary",
            "Rolled back: all traffic now serving primary:v1",
            "Rollback failed",
            "Failed to rollback canary",
        ),
    ]

    @staticmethod
    def _endpoint():
        return {
            "endpoint_name": "ep",
            "spec": {
                "image": "primary:v1",
                "canary": {"image": "canary:v2", "weight": 15},
            },
        }

    @pytest.mark.parametrize(
        ("command", "manager_method", "success_output", "false_output", "error_output"),
        CASES,
    )
    def test_transition_missing_endpoint(
        self,
        runner,
        command,
        manager_method,
        success_output,
        false_output,
        error_output,
    ):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = None
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", command, "ep", "--yes"])

        assert result.exit_code != 0
        assert "Endpoint 'ep' not found" in result.output
        getattr(mock_mgr, manager_method).assert_not_called()

    @pytest.mark.parametrize(
        ("command", "manager_method", "success_output", "false_output", "error_output"),
        CASES,
    )
    def test_transition_without_canary(
        self,
        runner,
        command,
        manager_method,
        success_output,
        false_output,
        error_output,
    ):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "spec": {"image": "primary:v1"},
        }
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", command, "ep", "--yes"])

        assert result.exit_code != 0
        assert "has no active canary" in result.output
        getattr(mock_mgr, manager_method).assert_not_called()

    @pytest.mark.parametrize(
        ("command", "manager_method", "success_output", "false_output", "error_output"),
        CASES,
    )
    def test_transition_confirmation_cancelled(
        self,
        runner,
        command,
        manager_method,
        success_output,
        false_output,
        error_output,
    ):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint()
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                ["inference", command, "ep"],
                input="n\n",
            )

        assert result.exit_code == 0
        assert "Cancelled" in result.output
        getattr(mock_mgr, manager_method).assert_not_called()

    @pytest.mark.parametrize(
        ("command", "manager_method", "success_output", "false_output", "error_output"),
        CASES,
    )
    def test_transition_success(
        self,
        runner,
        command,
        manager_method,
        success_output,
        false_output,
        error_output,
    ):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint()
        getattr(mock_mgr, manager_method).return_value = {
            "spec": {"image": "canary:v2" if command == "promote" else "primary:v1"}
        }
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", command, "ep", "--yes"])

        assert result.exit_code == 0
        assert success_output in result.output
        getattr(mock_mgr, manager_method).assert_called_once_with("ep")

    @pytest.mark.parametrize(
        ("command", "manager_method", "success_output", "false_output", "error_output"),
        CASES,
    )
    def test_transition_false_result(
        self,
        runner,
        command,
        manager_method,
        success_output,
        false_output,
        error_output,
    ):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint()
        getattr(mock_mgr, manager_method).return_value = None
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", command, "ep", "--yes"])

        assert result.exit_code != 0
        assert false_output in result.output

    @pytest.mark.parametrize(
        ("command", "manager_method", "success_output", "false_output", "error_output"),
        CASES,
    )
    def test_transition_value_error(
        self,
        runner,
        command,
        manager_method,
        success_output,
        false_output,
        error_output,
    ):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint()
        getattr(mock_mgr, manager_method).side_effect = ValueError("invalid canary state")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", command, "ep", "--yes"])

        assert result.exit_code != 0
        assert "invalid canary state" in result.output
        assert error_output not in result.output

    @pytest.mark.parametrize(
        ("command", "manager_method", "success_output", "false_output", "error_output"),
        CASES,
    )
    def test_transition_generic_error(
        self,
        runner,
        command,
        manager_method,
        success_output,
        false_output,
        error_output,
    ):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint()
        getattr(mock_mgr, manager_method).side_effect = RuntimeError("backend unavailable")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", command, "ep", "--yes"])

        assert result.exit_code != 0
        assert f"{error_output}: backend unavailable" in result.output


class TestInferenceSetTopology:
    def test_set_topology_success(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.set_topology.return_value = {
            "endpoint_name": "ep",
            "spec": {"mooncake": {"prefill_replicas": 3, "decode_replicas": 2}},
        }
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                ["inference", "set-topology", "ep", "--prefill", "3", "--decode", "2"],
            )

        assert result.exit_code == 0
        assert "topology set to 3p2d" in result.output
        mock_mgr.set_topology.assert_called_once_with("ep", 3, 2)

    def test_set_topology_non_table_prints_result(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.set_topology.return_value = {
            "endpoint_name": "ep",
            "prefill_replicas": 4,
            "decode_replicas": 5,
        }
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                [
                    "--output",
                    "json",
                    "inference",
                    "set-topology",
                    "ep",
                    "--prefill",
                    "4",
                    "--decode",
                    "5",
                ],
            )

        assert result.exit_code == 0
        assert '"prefill_replicas": 4' in result.output
        assert '"decode_replicas": 5' in result.output

    def test_set_topology_not_found(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.set_topology.return_value = None
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                ["inference", "set-topology", "ep", "--prefill", "1", "--decode", "1"],
            )

        assert result.exit_code != 0
        assert "Endpoint 'ep' not found" in result.output

    def test_set_topology_value_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.set_topology.side_effect = ValueError("prefill must be positive")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                ["inference", "set-topology", "ep", "--prefill", "1", "--decode", "1"],
            )

        assert result.exit_code != 0
        assert "prefill must be positive" in result.output
        assert "Failed to set topology" not in result.output

    def test_set_topology_generic_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.set_topology.side_effect = RuntimeError("write failed")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                ["inference", "set-topology", "ep", "--prefill", "1", "--decode", "1"],
            )

        assert result.exit_code != 0
        assert "Failed to set topology: write failed" in result.output


class TestInferenceConfigureStore:
    @staticmethod
    def _endpoint(store=None):
        return {
            "endpoint_name": "ep",
            "spec": {"mooncake": {"store": store or {}}},
        }

    @pytest.mark.parametrize(
        ("option_args", "expected_update"),
        [
            (["--enable-store"], {"enabled": True}),
            (["--disable-store"], {"enabled": False}),
            (["--no-cold-tier"], {"cold_tier_enabled": False}),
            (["--offload", "cpu"], {"offload": "cpu"}),
            (["--global-segment-size", "4096"], {"global_segment_size": 4096}),
            (["--local-buffer-size", "2048"], {"local_buffer_size": 2048}),
        ],
    )
    def test_configure_store_merges_each_option(self, runner, option_args, expected_update):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint({"preserved": "value"})
        mock_mgr.configure_store.return_value = {"endpoint_name": "ep"}
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                ["inference", "configure-store", "ep", *option_args],
            )

        assert result.exit_code == 0
        store_config = mock_mgr.configure_store.call_args.args[1]
        assert store_config["preserved"] == "value"
        for key, value in expected_update.items():
            assert store_config[key] == value

    def test_configure_store_cold_tier_enables_store(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint({"enabled": False, "offload": "disk"})
        mock_mgr.configure_store.return_value = {"endpoint_name": "ep"}
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                ["inference", "configure-store", "ep", "--cold-tier"],
            )

        assert result.exit_code == 0
        store_config = mock_mgr.configure_store.call_args.args[1]
        assert store_config == {
            "enabled": True,
            "offload": "disk",
            "cold_tier_enabled": True,
        }

    def test_configure_store_rejects_no_settings(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint()
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "configure-store", "ep"])

        assert result.exit_code != 0
        assert "No store settings given" in result.output
        mock_mgr.configure_store.assert_not_called()

    def test_configure_store_tolerates_malformed_stored_spec(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "spec": ["not", "a", "mapping"],
        }
        mock_mgr.configure_store.return_value = {"endpoint_name": "ep"}
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                ["inference", "configure-store", "ep", "--offload", "disk"],
            )

        assert result.exit_code == 0
        mock_mgr.configure_store.assert_called_once_with("ep", {"offload": "disk"})

    def test_configure_store_missing_endpoint(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = None
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                ["inference", "configure-store", "ep", "--enable-store"],
            )

        assert result.exit_code != 0
        assert "Endpoint 'ep' not found" in result.output
        mock_mgr.configure_store.assert_not_called()

    def test_configure_store_false_result(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint()
        mock_mgr.configure_store.return_value = None
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                ["inference", "configure-store", "ep", "--enable-store"],
            )

        assert result.exit_code != 0
        assert "Endpoint 'ep' not found" in result.output

    def test_configure_store_value_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint()
        mock_mgr.configure_store.side_effect = ValueError("invalid store size")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                ["inference", "configure-store", "ep", "--enable-store"],
            )

        assert result.exit_code != 0
        assert "invalid store size" in result.output
        assert "Failed to configure store" not in result.output

    def test_configure_store_generic_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = self._endpoint()
        mock_mgr.configure_store.side_effect = RuntimeError("database unavailable")
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                ["inference", "configure-store", "ep", "--enable-store"],
            )

        assert result.exit_code != 0
        assert "Failed to configure store: database unavailable" in result.output


class TestInferencePopulateKv:
    def test_populate_kv_non_table_prints_result(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.populate_kv_cache.return_value = {
            "files_uploaded": 3,
            "s3_uri": "s3://region-bucket/mooncake-kv/ep/",
        }
        with patch("cli.models.get_regional_bucket_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                [
                    "--output",
                    "json",
                    "inference",
                    "populate-kv",
                    "ep",
                    "./warm-cache",
                    "--region",
                    "us-west-2",
                ],
            )

        assert result.exit_code == 0
        assert '"files_uploaded": 3' in result.output
        assert "s3://region-bucket/mooncake-kv/ep/" in result.output
        mock_mgr.populate_kv_cache.assert_called_once_with("./warm-cache", "us-west-2", "ep")

    def test_populate_kv_error(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.populate_kv_cache.side_effect = RuntimeError("upload failed")
        with patch("cli.models.get_regional_bucket_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                [
                    "inference",
                    "populate-kv",
                    "ep",
                    "./warm-cache",
                    "--region",
                    "us-west-2",
                ],
            )

        assert result.exit_code != 0
        assert "Failed to populate KV cache: upload failed" in result.output
