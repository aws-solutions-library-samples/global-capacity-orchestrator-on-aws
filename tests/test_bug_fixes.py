"""
Regression tests for a handful of bug fixes across the GCO codebase.

Pins behavior that previously regressed: add_region now writes an ISO
timestamp to updated_at (not the region name), promote_canary and
rollback_canary validate that a canary exists and has an image field,
canary_deploy rejects weights outside 1..99 and stopped endpoints, and
both the cross-region aggregator Lambda and the shared proxy_utils
URL-encode query parameters instead of concatenating them raw. Uses
sys.path and importlib to reach the lambda/ modules that aren't on the
normal import path.
"""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

# ============================================================================
# inference.py: add_region updated_at bug fix
# ============================================================================


class TestAddRegionTimestamp:
    """Verify add_region sets updated_at to an ISO timestamp, not a region name."""

    @pytest.fixture
    def manager(self):
        from cli.inference import InferenceManager

        mgr = InferenceManager.__new__(InferenceManager)
        mgr._config = MagicMock()
        mgr._aws_client = MagicMock()
        return mgr

    def test_add_region_sets_iso_timestamp(self, manager):
        """updated_at should be an ISO 8601 timestamp, not a region name."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-ep",
            "target_regions": ["us-east-1"],
        }
        mock_store._table.update_item.return_value = {"Attributes": {"endpoint_name": "my-ep"}}
        manager._get_store = MagicMock(return_value=mock_store)

        manager.add_region("my-ep", "eu-west-1")

        call_args = mock_store._table.update_item.call_args
        updated_at = call_args[1]["ExpressionAttributeValues"][":u"]

        # Should be a valid ISO timestamp, not a region name
        assert "T" in updated_at, f"Expected ISO timestamp, got: {updated_at}"
        # Should be parseable as a datetime
        datetime.fromisoformat(updated_at)
        # Should NOT be a region name
        assert updated_at not in (
            "us-east-1",
            "us-west-2",
            "eu-west-1",
            "ap-southeast-1",
        )

    def test_add_region_includes_new_region_in_list(self, manager):
        """The new region should be appended to the target_regions list."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-ep",
            "target_regions": ["us-east-1"],
        }
        mock_store._table.update_item.return_value = {"Attributes": {"endpoint_name": "my-ep"}}
        manager._get_store = MagicMock(return_value=mock_store)

        manager.add_region("my-ep", "eu-west-1")

        call_args = mock_store._table.update_item.call_args
        regions = call_args[1]["ExpressionAttributeValues"][":r"]
        assert "eu-west-1" in regions
        assert "us-east-1" in regions

    def test_add_region_does_not_duplicate(self, manager):
        """Adding an already-present region should not duplicate it."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-ep",
            "target_regions": ["us-east-1", "eu-west-1"],
        }
        mock_store._table.update_item.return_value = {"Attributes": {"endpoint_name": "my-ep"}}
        manager._get_store = MagicMock(return_value=mock_store)

        manager.add_region("my-ep", "eu-west-1")

        call_args = mock_store._table.update_item.call_args
        regions = call_args[1]["ExpressionAttributeValues"][":r"]
        assert regions.count("eu-west-1") == 1


# ============================================================================
# inference.py: promote_canary defensive validation
# ============================================================================


class TestPromoteCanaryValidation:
    """Verify promote_canary validates canary structure."""

    @pytest.fixture
    def manager(self):
        from cli.inference import InferenceManager

        mgr = InferenceManager.__new__(InferenceManager)
        mgr._config = MagicMock()
        mgr._aws_client = MagicMock()
        return mgr

    def test_promote_canary_missing_image_raises(self, manager):
        """promote_canary should raise if canary dict has no 'image' key."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-ep",
            "spec": {"image": "old:v1", "canary": {"weight": 10, "replicas": 1}},
        }
        manager._get_store = MagicMock(return_value=mock_store)

        with pytest.raises(ValueError, match="missing the 'image' field"):
            manager.promote_canary("my-ep")

    def test_promote_canary_no_canary_raises(self, manager):
        """promote_canary should raise if no canary deployment exists."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-ep",
            "spec": {"image": "old:v1"},
        }
        manager._get_store = MagicMock(return_value=mock_store)

        with pytest.raises(ValueError, match="no active canary"):
            manager.promote_canary("my-ep")

    def test_promote_canary_success(self, manager):
        """promote_canary should swap image and remove canary config."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-ep",
            "spec": {
                "image": "old:v1",
                "canary": {"image": "new:v2", "weight": 20, "replicas": 1},
            },
        }
        mock_store.update_spec.return_value = {"endpoint_name": "my-ep"}
        manager._get_store = MagicMock(return_value=mock_store)

        result = manager.promote_canary("my-ep")
        assert result is not None

        # Verify the spec passed to update_spec
        call_args = mock_store.update_spec.call_args
        updated_spec = call_args[0][1]
        assert updated_spec["image"] == "new:v2"
        assert "canary" not in updated_spec

    def test_promote_canary_not_found(self, manager):
        """promote_canary should return None if endpoint not found."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = None
        manager._get_store = MagicMock(return_value=mock_store)

        result = manager.promote_canary("ghost")
        assert result is None


# ============================================================================
# inference.py: rollback_canary edge cases
# ============================================================================


class TestRollbackCanary:
    """Tests for rollback_canary."""

    @pytest.fixture
    def manager(self):
        from cli.inference import InferenceManager

        mgr = InferenceManager.__new__(InferenceManager)
        mgr._config = MagicMock()
        mgr._aws_client = MagicMock()
        return mgr

    def test_rollback_removes_canary_keeps_primary(self, manager):
        """rollback should remove canary config but keep primary image."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-ep",
            "spec": {
                "image": "primary:v1",
                "canary": {"image": "canary:v2", "weight": 10},
            },
        }
        mock_store.update_spec.return_value = {"endpoint_name": "my-ep"}
        manager._get_store = MagicMock(return_value=mock_store)

        result = manager.rollback_canary("my-ep")
        assert result is not None

        call_args = mock_store.update_spec.call_args
        updated_spec = call_args[0][1]
        assert updated_spec["image"] == "primary:v1"
        assert "canary" not in updated_spec

    def test_rollback_no_canary_raises(self, manager):
        """rollback should raise if no canary exists."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "my-ep",
            "spec": {"image": "primary:v1"},
        }
        manager._get_store = MagicMock(return_value=mock_store)

        with pytest.raises(ValueError, match="no active canary"):
            manager.rollback_canary("my-ep")


# ============================================================================
# inference.py: canary_deploy validation
# ============================================================================


class TestCanaryDeployValidation:
    """Tests for canary_deploy input validation."""

    @pytest.fixture
    def manager(self):
        from cli.inference import InferenceManager

        mgr = InferenceManager.__new__(InferenceManager)
        mgr._config = MagicMock()
        mgr._aws_client = MagicMock()
        return mgr

    def test_canary_weight_zero_raises(self, manager):
        """Weight of 0 should raise ValueError."""
        with pytest.raises(ValueError, match="between 1 and 99"):
            manager.canary_deploy("ep", "img:v2", weight=0)

    def test_canary_weight_100_raises(self, manager):
        """Weight of 100 should raise ValueError."""
        with pytest.raises(ValueError, match="between 1 and 99"):
            manager.canary_deploy("ep", "img:v2", weight=100)

    def test_canary_on_stopped_endpoint_raises(self, manager):
        """Canary on a stopped endpoint should raise."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "desired_state": "stopped",
            "spec": {"image": "old:v1"},
        }
        manager._get_store = MagicMock(return_value=mock_store)

        with pytest.raises(ValueError, match="must be running"):
            manager.canary_deploy("ep", "new:v2", weight=10)

    def test_canary_not_found_returns_none(self, manager):
        """Canary on non-existent endpoint should return None."""
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = None
        manager._get_store = MagicMock(return_value=mock_store)

        result = manager.canary_deploy("ghost", "img:v2")
        assert result is None


# ============================================================================
# cross-region-aggregator: URL encoding
# ============================================================================


class TestQueryRegionUrlEncoding:
    """Verify query params are properly URL-encoded."""

    def test_special_chars_in_query_params(self):
        """Query params with special characters should be URL-encoded."""
        from tests._lambda_imports import load_lambda_module

        handler = load_lambda_module("cross-region-aggregator")

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps({"ok": True}).encode("utf-8")
        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with (
            patch.dict(handler.os.environ, {"AWS_URL_SUFFIX": "amazonaws.com"}),
            patch.object(handler, "http", mock_http),
            patch.object(handler, "_sigv4_headers", return_value={"Authorization": "test"}),
        ):
            handler.query_region(
                "us-east-1",
                "https://abc123.execute-api.us-east-1.amazonaws.com/prod",
                "/api/v1/jobs",
                "GET",
                query_params={"namespace": "my namespace", "label": "app=web&tier=front"},
            )

            call_args = mock_http.request.call_args
            url = call_args[0][1]
            # Should be URL-encoded, not raw
            assert "my+namespace" in url or "my%20namespace" in url
            assert "app%3Dweb%26tier%3Dfront" in url or "app%3Dweb%26" in url
            # Should NOT contain raw special chars in query string
            assert "my namespace" not in url.split("?")[1]


# ============================================================================
# proxy_utils.py: URL encoding
# ============================================================================


class TestBuildTargetUrlEncoding:
    """Verify build_target_url properly URL-encodes query params."""

    def test_special_chars_encoded(self):
        """Query params with special characters should be URL-encoded."""
        from tests._lambda_imports import load_lambda_module

        proxy_utils = load_lambda_module("proxy-shared", "proxy_utils", shared_dirs=["tls-shared"])

        url = proxy_utils.build_target_url(
            "alb.example.com",
            "/api/v1/jobs",
            {"namespace": "my namespace", "filter": "a=b&c=d"},
        )

        assert "my+namespace" in url or "my%20namespace" in url
        assert "a%3Db%26c%3Dd" in url or "a%3Db%26" in url

    def test_no_query_params(self):
        """URL without query params should have no question mark."""
        from tests._lambda_imports import load_lambda_module

        proxy_utils = load_lambda_module("proxy-shared", "proxy_utils", shared_dirs=["tls-shared"])

        url = proxy_utils.build_target_url("alb.example.com", "/api/v1/health", None)

        assert url == "https://alb.example.com/api/v1/health"
        assert "?" not in url

    def test_empty_query_params(self):
        """Empty query params dict should produce no query string."""
        from tests._lambda_imports import load_lambda_module

        proxy_utils = load_lambda_module("proxy-shared", "proxy_utils", shared_dirs=["tls-shared"])

        url = proxy_utils.build_target_url("alb.example.com", "/api/v1/health", {})

        # Empty dict is falsy, so no query string
        assert "?" not in url
