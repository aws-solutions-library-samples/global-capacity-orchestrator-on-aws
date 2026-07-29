"""
Tests for A/B (canary) inference endpoint deployments.

Covers the canary lifecycle on InferenceManager — canary_deploy with
weight/replicas validation and stopped-endpoint guard, promote_canary
promoting the canary image to primary, and rollback_canary discarding
it — plus the CLI wrappers in `gco inference canary|promote|rollback`
and the canary field on the InferenceEndpointSpec model. Also includes
baseline coverage for InferenceManager deploy/list/scale/stop/start/
delete/update_image/add_region/remove_region so canary changes don't
regress the surrounding surface.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.inference import InferenceManager

# =============================================================================
# InferenceManager Canary Tests
# =============================================================================


class TestCanaryDeploy:
    """Tests for canary_deploy method."""

    @patch("cli.inference.get_aws_client")
    def test_canary_deploy_success(self, mock_aws):
        original_spec = {
            "image": "vllm/vllm-openai:v0.8.0",
            "replicas": 2,
            "env": {"MODEL_REVISION": "stable"},
        }
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-llm",
            "desired_state": "running",
            "spec": original_spec,
        }
        mock_store.update_spec.return_value = {
            "endpoint_name": "my-llm",
            "spec": {
                "image": "vllm/vllm-openai:v0.8.0",
                "canary": {"image": "vllm/vllm-openai:v0.9.0", "weight": 10, "replicas": 1},
            },
        }

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.canary_deploy("my-llm", " vllm/vllm-openai:v0.9.0 ", weight=10)

        assert result is not None
        assert result["spec"]["canary"]["image"] == "vllm/vllm-openai:v0.9.0"
        submitted_spec = mock_store.update_spec.call_args.args[1]
        assert submitted_spec["canary"] == {
            "image": "vllm/vllm-openai:v0.9.0",
            "weight": 10,
            "replicas": 1,
        }
        assert submitted_spec is not original_spec
        assert submitted_spec["env"] is not original_spec["env"]
        assert "canary" not in original_spec

    @patch("cli.inference.get_aws_client")
    def test_canary_deploy_not_found(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = None

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.canary_deploy("nonexistent", "image:v2")

        assert result is None

    @pytest.mark.parametrize("weight", [0, 100, -1, True, 10.5, "10"])
    def test_canary_deploy_invalid_weight(self, weight):
        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = MagicMock()

        with pytest.raises(
            ValueError,
            match="Canary weight must be an integer between 1 and 99",
        ):
            manager.canary_deploy("my-llm", "image:v2", weight=weight)

    @pytest.mark.parametrize("image", [None, "", "   ", 123])
    def test_canary_deploy_invalid_image(self, image):
        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = MagicMock()

        with pytest.raises(ValueError, match="Canary image must be a non-empty string"):
            manager.canary_deploy("my-llm", image)

    @pytest.mark.parametrize("replicas", [0, -1, True, 1.5, "1"])
    def test_canary_deploy_invalid_replicas(self, replicas):
        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = MagicMock()

        with pytest.raises(ValueError, match="Canary replicas must be a positive integer"):
            manager.canary_deploy("my-llm", "image:v2", replicas=replicas)

    @patch("cli.inference.get_aws_client")
    def test_canary_deploy_stopped_endpoint(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-llm",
            "desired_state": "stopped",
            "spec": {"image": "old:v1"},
        }

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with (
            patch.object(manager, "_get_store", return_value=mock_store),
            pytest.raises(ValueError, match="Cannot canary"),
        ):
            manager.canary_deploy("my-llm", "new:v2")

    @patch("cli.inference.get_aws_client")
    def test_canary_deploy_custom_replicas(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-llm",
            "desired_state": "running",
            "spec": {"image": "old:v1"},
        }
        mock_store.update_spec.return_value = {"endpoint_name": "my-llm"}

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            manager.canary_deploy("my-llm", "new:v2", weight=25, replicas=3)

        call_args = mock_store.update_spec.call_args[0]
        spec = call_args[1]
        assert spec["canary"]["weight"] == 25
        assert spec["canary"]["replicas"] == 3

    @patch("cli.inference.get_aws_client")
    def test_canary_deploy_rejects_mooncake_endpoint(self, mock_aws):
        original_spec = {
            "image": "old:v1",
            "mooncake": {"mode": "store", "store": {"enabled": True}},
        }
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-llm",
            "desired_state": "running",
            "spec": original_spec,
        }

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with (
            patch.object(manager, "_get_store", return_value=mock_store),
            pytest.raises(
                ValueError,
                match="Canary deployments are not supported for Mooncake endpoints",
            ),
        ):
            manager.canary_deploy("my-llm", "new:v2")

        mock_store.update_spec.assert_not_called()
        assert "canary" not in original_spec


class TestPromoteCanary:
    """Tests for promote_canary method."""

    @patch("cli.inference.get_aws_client")
    def test_promote_success(self, mock_aws):
        original_spec = {
            "image": "old:v1",
            "region_image_uris": {"us-east-1": "regional-old:v1"},
            "env": {"MODEL_REVISION": "stable"},
            "canary": {"image": " new:v2 ", "weight": 10, "replicas": 1},
        }
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-llm",
            "spec": original_spec,
        }
        mock_store.update_spec.return_value = {
            "endpoint_name": "my-llm",
            "spec": {"image": "new:v2"},
        }

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.promote_canary("my-llm")

        assert result is not None
        submitted_spec = mock_store.update_spec.call_args.args[1]
        assert submitted_spec["image"] == "new:v2"
        assert "canary" not in submitted_spec
        assert "region_image_uris" not in submitted_spec
        assert submitted_spec is not original_spec
        assert submitted_spec["env"] is not original_spec["env"]
        assert original_spec["image"] == "old:v1"
        assert "canary" in original_spec
        assert "region_image_uris" in original_spec

    @patch("cli.inference.get_aws_client")
    def test_promote_not_found(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = None

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.promote_canary("nonexistent")

        assert result is None

    @patch("cli.inference.get_aws_client")
    def test_promote_no_canary(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-llm",
            "spec": {"image": "old:v1"},
        }

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with (
            patch.object(manager, "_get_store", return_value=mock_store),
            pytest.raises(ValueError, match="no active canary"),
        ):
            manager.promote_canary("my-llm")

    @patch("cli.inference.get_aws_client")
    def test_promote_rejects_mooncake_endpoint(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-llm",
            "spec": {
                "image": "old:v1",
                "mooncake": {"mode": "store"},
                "canary": {"image": "new:v2"},
            },
        }

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with (
            patch.object(manager, "_get_store", return_value=mock_store),
            pytest.raises(
                ValueError,
                match="Canary promotion is not supported for Mooncake endpoints",
            ),
        ):
            manager.promote_canary("my-llm")

        mock_store.update_spec.assert_not_called()

    @patch("cli.inference.get_aws_client")
    def test_promote_rejects_invalid_canary_image(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-llm",
            "spec": {"image": "old:v1", "canary": {"image": "   "}},
        }

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with (
            patch.object(manager, "_get_store", return_value=mock_store),
            pytest.raises(ValueError, match="has an invalid 'image' field"),
        ):
            manager.promote_canary("my-llm")

        mock_store.update_spec.assert_not_called()


class TestRollbackCanary:
    """Tests for rollback_canary method."""

    @patch("cli.inference.get_aws_client")
    def test_rollback_repairs_legacy_mooncake_canary_record(self, mock_aws):
        original_spec = {
            "image": "old:v1",
            "mooncake": {"mode": "store", "store": {"enabled": True}},
            "canary": {"image": "new:v2", "weight": 10},
        }
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-llm",
            "spec": original_spec,
        }
        mock_store.update_spec.return_value = {
            "endpoint_name": "my-llm",
            "spec": {"image": "old:v1", "mooncake": original_spec["mooncake"]},
        }

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.rollback_canary("my-llm")

        assert result is not None
        submitted_spec = mock_store.update_spec.call_args.args[1]
        assert submitted_spec["image"] == "old:v1"
        assert submitted_spec["mooncake"] == original_spec["mooncake"]
        assert "canary" not in submitted_spec
        assert submitted_spec is not original_spec
        assert submitted_spec["mooncake"] is not original_spec["mooncake"]
        assert "canary" in original_spec

    @patch("cli.inference.get_aws_client")
    def test_rollback_not_found(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = None

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.rollback_canary("nonexistent")

        assert result is None

    @patch("cli.inference.get_aws_client")
    def test_rollback_no_canary(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-llm",
            "spec": {"image": "old:v1"},
        }

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with (
            patch.object(manager, "_get_store", return_value=mock_store),
            pytest.raises(ValueError, match="no active canary"),
        ):
            manager.rollback_canary("my-llm")


# =============================================================================
# Authenticated Inference Proxy Canary Routing Tests
# =============================================================================


class TestInferenceProxyCanaryRouting:
    """Canary traffic is gated on observed, matching local readiness."""

    @staticmethod
    def _endpoint(canary_status):
        return {
            "spec": {
                "image": "old:v1",
                "canary": {"image": "new:v2", "weight": 10, "replicas": 2},
            },
            "region_status": {"us-east-1": {"state": "running", "canary": canary_status}},
        }

    def test_routes_to_fully_ready_matching_canary(self):
        from gco.services.api_routes.inference_proxy import _target_service

        endpoint = self._endpoint(
            {
                "state": "running",
                "image": "new:v2",
                "weight": 10,
                "replicas_ready": 2,
                "replicas_desired": 2,
            }
        )
        with (
            patch.dict("os.environ", {"REGION": "us-east-1"}),
            patch("gco.services.api_routes.inference_proxy.secrets.randbelow", return_value=0),
        ):
            assert _target_service(endpoint, "my-llm") == "my-llm-canary"

    @pytest.mark.parametrize(
        "status_override",
        [
            {"state": "creating"},
            {"image": "different:v2"},
            {"replicas_ready": 1},
            {"replicas_desired": 0, "replicas_ready": 0},
        ],
    )
    def test_routes_to_primary_until_matching_canary_is_fully_ready(self, status_override):
        from gco.services.api_routes.inference_proxy import _target_service

        canary_status = {
            "state": "running",
            "image": "new:v2",
            "weight": 10,
            "replicas_ready": 2,
            "replicas_desired": 2,
        }
        canary_status.update(status_override)
        endpoint = self._endpoint(canary_status)
        with (
            patch.dict("os.environ", {"REGION": "us-east-1"}),
            patch("gco.services.api_routes.inference_proxy.secrets.randbelow", return_value=0),
        ):
            assert _target_service(endpoint, "my-llm") == "my-llm"

    def test_mooncake_split_topology_routes_to_proxy_service(self):
        from gco.services.api_routes.inference_proxy import _target_service

        endpoint = {
            "spec": {
                "image": "image:v1",
                "mooncake": {"mode": "disaggregated"},
            }
        }
        assert _target_service(endpoint, "my-llm") == "my-llm-proxy"

    def test_proxy_routes_expose_only_supported_methods(self):
        """Only GET, HEAD, and POST reach either proxy path.

        Methods are unioned per path because each verb is registered as its own
        route: FastAPI derives an operation's ``operationId`` from
        ``generate_unique_id``, which is evaluated once per route, so a single
        route serving all three verbs would emit three OpenAPI operations
        sharing one ID. See
        ``tests/test_inference_proxy.py::test_router_operation_ids_are_unique_per_method``.
        """
        from gco.services.api_routes.inference_proxy import router

        methods_by_path: dict[str, set[str]] = {}
        for route in router.routes:
            methods_by_path.setdefault(route.path, set()).update(route.methods)

        assert methods_by_path == {
            "/inference/{endpoint_name}": {"GET", "HEAD", "POST"},
            "/inference/{endpoint_name}/{upstream_path:path}": {"GET", "HEAD", "POST"},
        }


# =============================================================================
# CLI Command Tests
# =============================================================================


class TestCanaryCLI:
    """Tests for canary CLI commands."""

    def test_canary_help(self):
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["inference", "canary", "--help"])
        assert result.exit_code == 0
        assert "--image" in result.output
        assert "--weight" in result.output
        assert "--replicas" in result.output

    def test_promote_help(self):
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["inference", "promote", "--help"])
        assert result.exit_code == 0
        assert "ENDPOINT_NAME" in result.output

    def test_rollback_help(self):
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["inference", "rollback", "--help"])
        assert result.exit_code == 0
        assert "ENDPOINT_NAME" in result.output

    @patch("cli.inference.get_aws_client")
    @patch("cli.inference.InferenceManager.canary_deploy")
    def test_canary_command_success(self, mock_canary, mock_aws):
        from cli.main import cli

        mock_canary.return_value = {
            "endpoint_name": "my-llm",
            "spec": {"canary": {"image": "new:v2", "weight": 10}},
        }

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "inference",
                "canary",
                "my-llm",
                "--image",
                "new:v2",
                "--weight",
                "10",
            ],
        )
        assert result.exit_code == 0
        assert "Canary started" in result.output
        assert "10%" in result.output

    @patch("cli.inference.get_aws_client")
    @patch("cli.inference.InferenceManager.canary_deploy")
    def test_canary_command_not_found(self, mock_canary, mock_aws):
        from cli.main import cli

        mock_canary.return_value = None

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "inference",
                "canary",
                "nonexistent",
                "--image",
                "new:v2",
            ],
        )
        assert result.exit_code == 1
        assert "not found" in result.output

    @patch("cli.inference.get_aws_client")
    @patch("cli.inference.InferenceManager.get_endpoint")
    @patch("cli.inference.InferenceManager.promote_canary")
    def test_promote_command_success(self, mock_promote, mock_get, mock_aws):
        from cli.main import cli

        mock_get.return_value = {
            "endpoint_name": "my-llm",
            "spec": {"image": "old:v1", "canary": {"image": "new:v2", "weight": 10}},
        }
        mock_promote.return_value = {
            "endpoint_name": "my-llm",
            "spec": {"image": "new:v2"},
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["inference", "promote", "my-llm", "-y"])
        assert result.exit_code == 0
        assert "Promoted" in result.output

    @patch("cli.inference.get_aws_client")
    @patch("cli.inference.InferenceManager.get_endpoint")
    @patch("cli.inference.InferenceManager.rollback_canary")
    def test_rollback_command_success(self, mock_rollback, mock_get, mock_aws):
        from cli.main import cli

        mock_get.return_value = {
            "endpoint_name": "my-llm",
            "spec": {"image": "old:v1", "canary": {"image": "new:v2", "weight": 10}},
        }
        mock_rollback.return_value = {
            "endpoint_name": "my-llm",
            "spec": {"image": "old:v1"},
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["inference", "rollback", "my-llm", "-y"])
        assert result.exit_code == 0
        assert "Rolled back" in result.output

    @patch("cli.inference.get_aws_client")
    @patch("cli.inference.InferenceManager.get_endpoint")
    def test_promote_no_canary(self, mock_get, mock_aws):
        from cli.main import cli

        mock_get.return_value = {
            "endpoint_name": "my-llm",
            "spec": {"image": "old:v1"},
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["inference", "promote", "my-llm", "-y"])
        assert result.exit_code == 1
        assert "no active canary" in result.output

    @patch("cli.inference.get_aws_client")
    @patch("cli.inference.InferenceManager.get_endpoint")
    def test_rollback_no_canary(self, mock_get, mock_aws):
        from cli.main import cli

        mock_get.return_value = {
            "endpoint_name": "my-llm",
            "spec": {"image": "old:v1"},
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["inference", "rollback", "my-llm", "-y"])
        assert result.exit_code == 1
        assert "no active canary" in result.output

    @patch("cli.inference.get_aws_client")
    @patch("cli.inference.InferenceManager.get_endpoint")
    def test_promote_endpoint_not_found(self, mock_get, mock_aws):
        from cli.main import cli

        mock_get.return_value = None

        runner = CliRunner()
        result = runner.invoke(cli, ["inference", "promote", "nonexistent", "-y"])
        assert result.exit_code == 1
        assert "not found" in result.output


# =============================================================================
# Model Tests
# =============================================================================


class TestInferenceEndpointSpecCanary:
    """Tests for canary field in InferenceEndpointSpec."""

    def test_spec_with_canary(self):
        from gco.models.inference_models import InferenceEndpointSpec

        spec = InferenceEndpointSpec(
            image="old:v1",
            canary={"image": "new:v2", "weight": 10, "replicas": 1},
        )
        d = spec.to_dict()
        assert d["canary"]["image"] == "new:v2"
        assert d["canary"]["weight"] == 10

    def test_spec_without_canary(self):
        from gco.models.inference_models import InferenceEndpointSpec

        spec = InferenceEndpointSpec(image="old:v1")
        d = spec.to_dict()
        assert "canary" not in d

    def test_spec_from_dict_with_canary(self):
        from gco.models.inference_models import InferenceEndpointSpec

        spec = InferenceEndpointSpec.from_dict(
            {
                "image": "old:v1",
                "canary": {"image": "new:v2", "weight": 25, "replicas": 2},
            }
        )
        assert spec.canary is not None
        assert spec.canary["image"] == "new:v2"
        assert spec.canary["weight"] == 25

    def test_spec_from_dict_without_canary(self):
        from gco.models.inference_models import InferenceEndpointSpec

        spec = InferenceEndpointSpec.from_dict({"image": "old:v1"})
        assert spec.canary is None


# =============================================================================
# InferenceManager Core Method Tests
# =============================================================================


class TestInferenceManagerDeploy:
    """Tests for the deploy method."""

    @patch("cli.inference.get_aws_client")
    def test_deploy_basic(self, mock_aws):
        mock_store = MagicMock()
        mock_store.create_endpoint.return_value = {
            "endpoint_name": "test-ep",
            "target_regions": ["us-east-1"],
            "ingress_path": "/inference/test-ep",
        }
        mock_aws.return_value.discover_regional_stacks.return_value = {"us-east-1": {}}

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.deploy("test-ep", "image:v1")

        assert result["endpoint_name"] == "test-ep"
        mock_store.create_endpoint.assert_called_once()

    @patch("cli.inference.get_aws_client")
    def test_deploy_with_capacity_type(self, mock_aws):
        mock_store = MagicMock()
        mock_store.create_endpoint.return_value = {"endpoint_name": "test-ep"}
        mock_aws.return_value.discover_regional_stacks.return_value = {"us-east-1": {}}

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            manager.deploy("test-ep", "image:v1", capacity_type="spot")

        call_args = mock_store.create_endpoint.call_args
        spec = call_args[1]["spec"]
        assert spec["capacity_type"] == "spot"

    @patch("cli.inference.get_aws_client")
    def test_deploy_no_regions_raises(self, mock_aws):
        mock_aws.return_value.discover_regional_stacks.return_value = {}

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with pytest.raises(ValueError, match="No deployed regions"):
            manager.deploy("test-ep", "image:v1")


class TestInferenceManagerList:
    """Tests for list_endpoints method."""

    @patch("cli.inference.get_aws_client")
    def test_list_endpoints(self, mock_aws):
        mock_store = MagicMock()
        mock_store.list_endpoints.return_value = [
            {"endpoint_name": "ep1"},
            {"endpoint_name": "ep2"},
        ]

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.list_endpoints()

        assert len(result) == 2


class TestInferenceManagerScale:
    """Tests for scale method."""

    @patch("cli.inference.get_aws_client")
    def test_scale(self, mock_aws):
        mock_store = MagicMock()
        mock_store.scale_endpoint.return_value = {"endpoint_name": "ep1", "spec": {"replicas": 3}}

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.scale("ep1", 3)

        assert result is not None
        mock_store.scale_endpoint.assert_called_once_with("ep1", 3)


class TestInferenceManagerStopStart:
    """Tests for stop/start methods."""

    @patch("cli.inference.get_aws_client")
    def test_stop(self, mock_aws):
        mock_store = MagicMock()
        mock_store.update_desired_state.return_value = {"desired_state": "stopped"}

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.stop("ep1")

        mock_store.update_desired_state.assert_called_once_with("ep1", "stopped")
        assert result is not None

    @patch("cli.inference.get_aws_client")
    def test_start(self, mock_aws):
        mock_store = MagicMock()
        mock_store.update_desired_state.return_value = {"desired_state": "running"}

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            manager.start("ep1")

        mock_store.update_desired_state.assert_called_once_with("ep1", "running")

    @patch("cli.inference.get_aws_client")
    def test_delete(self, mock_aws):
        mock_store = MagicMock()
        mock_store.update_desired_state.return_value = {"desired_state": "deleted"}

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            manager.delete("ep1")

        mock_store.update_desired_state.assert_called_once_with("ep1", "deleted")


class TestInferenceManagerUpdateImage:
    """Tests for update_image method."""

    @patch("cli.inference.get_aws_client")
    def test_update_image(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "ep1",
            "spec": {"image": "old:v1"},
        }
        mock_store.update_spec.return_value = {"spec": {"image": "new:v2"}}

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.update_image("ep1", "new:v2")

        assert result is not None

    @patch("cli.inference.get_aws_client")
    def test_update_image_not_found(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = None

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.update_image("nonexistent", "new:v2")

        assert result is None


class TestInferenceManagerGetEndpoint:
    """Tests for get_endpoint method."""

    @patch("cli.inference.get_aws_client")
    def test_get_endpoint(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {"endpoint_name": "ep1", "spec": {"image": "img"}}

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.get_endpoint("ep1")

        assert result is not None
        assert result["endpoint_name"] == "ep1"

    @patch("cli.inference.get_aws_client")
    def test_get_endpoint_not_found(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = None

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.get_endpoint("nonexistent")

        assert result is None


class TestInferenceManagerDeployOptions:
    """Tests for deploy with various options."""

    @patch("cli.inference.get_aws_client")
    def test_deploy_with_all_options(self, mock_aws):
        mock_store = MagicMock()
        mock_store.create_endpoint.return_value = {"endpoint_name": "ep1"}
        mock_aws.return_value.discover_regional_stacks.return_value = {"us-east-1": {}}

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            manager.deploy(
                "ep1",
                "image:v1",
                gpu_type="g5.xlarge",
                model_path="/models/llama",
                model_source="s3://bucket/model",
                env={"KEY": "val"},
                autoscaling={"enabled": True},
                capacity_type="spot",
            )

        call_args = mock_store.create_endpoint.call_args[1]
        spec = call_args["spec"]
        assert spec["gpu_type"] == "g5.xlarge"
        assert spec["model_path"] == "/models/llama"
        assert spec["model_source"] == "s3://bucket/model"
        assert spec["env"] == {"KEY": "val"}
        assert spec["autoscaling"] == {"enabled": True}
        assert spec["capacity_type"] == "spot"

    @patch("cli.inference.get_aws_client")
    def test_deploy_with_explicit_regions(self, mock_aws):
        mock_store = MagicMock()
        mock_store.create_endpoint.return_value = {"endpoint_name": "ep1"}

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            manager.deploy("ep1", "image:v1", target_regions=["us-east-1", "us-west-2"])

        call_args = mock_store.create_endpoint.call_args[1]
        assert call_args["target_regions"] == ["us-east-1", "us-west-2"]


class TestInferenceManagerRegions:
    """Tests for add_region/remove_region methods."""

    @patch("cli.inference.get_aws_client")
    def test_add_region(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "ep1",
            "target_regions": ["us-east-1"],
        }
        mock_store._table.update_item.return_value = {
            "Attributes": {"target_regions": ["us-east-1", "us-west-2"]}
        }

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.add_region("ep1", "us-west-2")

        assert result is not None

    @patch("cli.inference.get_aws_client")
    def test_add_region_not_found(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = None

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.add_region("nonexistent", "us-west-2")

        assert result is None

    @patch("cli.inference.get_aws_client")
    def test_remove_region(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "ep1",
            "target_regions": ["us-east-1", "us-west-2"],
        }
        mock_store._table.update_item.return_value = {
            "Attributes": {"target_regions": ["us-east-1"]}
        }

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.remove_region("ep1", "us-west-2")

        assert result is not None

    @patch("cli.inference.get_aws_client")
    def test_remove_region_not_found(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = None

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.remove_region("nonexistent", "us-west-2")

        assert result is None

    @patch("cli.inference.get_aws_client")
    def test_remove_region_error(self, mock_aws):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "ep1",
            "target_regions": ["us-east-1"],
        }
        mock_store._table.update_item.side_effect = Exception("DynamoDB error")

        manager = InferenceManager.__new__(InferenceManager)
        manager.config = MagicMock()
        manager._aws_client = mock_aws.return_value

        with patch.object(manager, "_get_store", return_value=mock_store):
            result = manager.remove_region("ep1", "us-east-1")

        assert result is None
