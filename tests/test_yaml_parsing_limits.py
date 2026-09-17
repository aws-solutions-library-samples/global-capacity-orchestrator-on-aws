"""
Tests for YAML parsing limits on the manifest processor.

Drives ``ManifestProcessor._check_yaml_depth`` against hand-built
dict/list trees at, below, and above the configured depth threshold,
then confirms ``validate_manifest`` refuses structures that blow past
the limit with a clear error (so billion-laughs-style bombs can't
pin the event loop). Also covers the ``NoAliasSafeLoader`` plus the
``safe_load_yaml`` / ``safe_load_all_yaml`` utilities: anchor / alias
syntax is rejected by default (alias-expansion DoS), permitted when
``allow_aliases=True``, and the ``yaml_max_depth`` and
``yaml_allow_aliases`` knobs in ``cdk.json`` flow through to the
runtime loader via the manifest processor config.
"""

from unittest.mock import patch

import pytest
import yaml
from kubernetes import config as k8s_config

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_k8s_config():
    """Mock Kubernetes configuration loading."""
    with patch("gco.services.manifest_processor.config") as mock_config:
        mock_config.ConfigException = k8s_config.ConfigException
        mock_config.load_incluster_config.side_effect = k8s_config.ConfigException("Not in cluster")
        mock_config.load_kube_config.return_value = None
        yield mock_config


@pytest.fixture
def processor(mock_k8s_config):
    """Create ManifestProcessor with default yaml_max_depth=50."""
    from gco.services.manifest_processor import ManifestProcessor

    with patch("gco.services.manifest_processor.client"):
        return ManifestProcessor(
            cluster_id="test-cluster",
            region="us-east-1",
            config_dict={
                "max_cpu_per_manifest": "10",
                "max_memory_per_manifest": "32Gi",
                "max_gpu_per_manifest": 4,
                "allowed_namespaces": ["default", "gco-jobs"],
                "validation_enabled": True,
                "yaml_max_depth": 50,
            },
        )


@pytest.fixture
def shallow_processor(mock_k8s_config):
    """Create ManifestProcessor with yaml_max_depth=5 for easier testing."""
    from gco.services.manifest_processor import ManifestProcessor

    with patch("gco.services.manifest_processor.client"):
        return ManifestProcessor(
            cluster_id="test-cluster",
            region="us-east-1",
            config_dict={
                "max_cpu_per_manifest": "10",
                "max_memory_per_manifest": "32Gi",
                "max_gpu_per_manifest": 4,
                "allowed_namespaces": ["default", "gco-jobs"],
                "validation_enabled": True,
                "yaml_max_depth": 5,
            },
        )


# ---------------------------------------------------------------------------
# Helper: build nested structures
# ---------------------------------------------------------------------------


def _nested_dict(depth: int) -> dict:
    """Build a dict nested to the given depth."""
    obj: dict = {"leaf": "value"}
    for _ in range(depth):
        obj = {"nested": obj}
    return obj


def _nested_list(depth: int) -> list:
    """Build a list nested to the given depth."""
    obj: list = ["leaf"]
    for _ in range(depth):
        obj = [obj]
    return obj


def _build_job_manifest(extra_spec: dict | None = None) -> dict:
    """Build a minimal valid Job manifest."""
    manifest = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "test-job", "namespace": "default"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "worker",
                            "image": "python:3.14",
                        }
                    ],
                    "restartPolicy": "Never",
                }
            }
        },
    }
    if extra_spec:
        manifest["spec"]["template"]["spec"].update(extra_spec)
    return manifest


# =============================================================================
# Tests: _check_yaml_depth
# =============================================================================


class TestCheckYamlDepth:
    """Unit tests for _check_yaml_depth method."""

    def test_scalar_always_passes(self, processor):
        assert processor._check_yaml_depth("hello") is True
        assert processor._check_yaml_depth(42) is True
        assert processor._check_yaml_depth(None) is True
        assert processor._check_yaml_depth(True) is True

    def test_empty_dict_passes(self, processor):
        assert processor._check_yaml_depth({}) is True

    def test_empty_list_passes(self, processor):
        assert processor._check_yaml_depth([]) is True

    def test_flat_dict_passes(self, processor):
        assert processor._check_yaml_depth({"a": 1, "b": 2, "c": 3}) is True

    def test_flat_list_passes(self, processor):
        assert processor._check_yaml_depth([1, 2, 3]) is True

    def test_nested_dict_within_limit(self, shallow_processor):
        """Depth 4 nested dict should pass with max_depth=5."""
        obj = _nested_dict(4)
        assert shallow_processor._check_yaml_depth(obj) is True

    def test_nested_dict_exceeds_limit(self, shallow_processor):
        """Depth 7 nested dict should fail with max_depth=5."""
        obj = _nested_dict(7)
        assert shallow_processor._check_yaml_depth(obj) is False

    def test_nested_list_within_limit(self, shallow_processor):
        """Depth 4 nested list should pass with max_depth=5."""
        obj = _nested_list(4)
        assert shallow_processor._check_yaml_depth(obj) is True

    def test_nested_list_exceeds_limit(self, shallow_processor):
        """Depth 7 nested list should fail with max_depth=5."""
        obj = _nested_list(7)
        assert shallow_processor._check_yaml_depth(obj) is False

    def test_mixed_nesting_within_limit(self, shallow_processor):
        """Mixed dict/list nesting within limit passes."""
        obj = {"a": [{"b": [{"c": "leaf"}]}]}  # depth ~5
        assert shallow_processor._check_yaml_depth(obj) is True

    def test_mixed_nesting_exceeds_limit(self, shallow_processor):
        """Mixed dict/list nesting exceeding limit fails."""
        obj = {"a": [{"b": [{"c": [{"d": [{"e": [{"f": "deep"}]}]}]}]}]}  # depth ~11
        assert shallow_processor._check_yaml_depth(obj) is False

    def test_default_depth_50_accepts_normal_manifests(self, processor):
        """A typical K8s manifest should be well within depth 50."""
        manifest = _build_job_manifest()
        assert processor._check_yaml_depth(manifest) is True

    def test_depth_exactly_at_limit(self, shallow_processor):
        """Depth exactly at the limit should pass (boundary test).

        _nested_dict(n) creates n+1 dict levels. With max_depth=5,
        _nested_dict(4) creates 5 dict levels; the scalar leaf is
        checked at depth 5 which equals max_depth → passes.
        """
        obj = _nested_dict(4)
        assert shallow_processor._check_yaml_depth(obj) is True

    def test_depth_one_over_limit(self, shallow_processor):
        """Depth one over the limit should fail (boundary test).

        _nested_dict(5) creates 6 dict levels; the scalar leaf is
        checked at depth 6 which exceeds max_depth=5 → fails.
        """
        obj = _nested_dict(5)
        assert shallow_processor._check_yaml_depth(obj) is False


# =============================================================================
# Tests: validate_manifest depth integration
# =============================================================================


class TestValidateManifestDepth:
    """Tests that validate_manifest rejects deeply nested manifests."""

    def test_normal_manifest_passes(self, processor):
        manifest = _build_job_manifest()
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True
        assert error is None

    def test_deeply_nested_manifest_rejected(self, shallow_processor):
        """A manifest with excessive nesting is rejected with HTTP 400 message."""
        manifest = _build_job_manifest()
        # Inject a deeply nested annotation value
        manifest["metadata"]["annotations"] = _nested_dict(10)

        is_valid, error = shallow_processor.validate_manifest(manifest)
        assert is_valid is False
        assert "nesting depth" in error
        assert "5" in error  # should mention the configured limit

    def test_depth_check_runs_before_other_validations(self, shallow_processor):
        """Depth check should run before structure validation.

        Even a manifest missing required fields should be rejected for depth
        first if it exceeds the limit.
        """
        # Missing 'kind' and 'metadata' but deeply nested
        manifest = {"deeply": _nested_dict(10)}

        is_valid, error = shallow_processor.validate_manifest(manifest)
        assert is_valid is False
        assert "nesting depth" in error

    def test_validation_disabled_skips_depth_check(self, mock_k8s_config):
        """When validation is disabled, depth check is skipped."""
        from gco.services.manifest_processor import ManifestProcessor

        with patch("gco.services.manifest_processor.client"):
            proc = ManifestProcessor(
                cluster_id="test",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": False,
                    "yaml_max_depth": 5,
                },
            )

        manifest = {"deeply": _nested_dict(10)}
        is_valid, _error = proc.validate_manifest(manifest)
        assert is_valid is True


# =============================================================================
# Tests: NoAliasSafeLoader and safe_load_yaml utilities
# =============================================================================


class TestNoAliasSafeLoader:
    """Tests for the alias-rejecting YAML loader."""

    def test_simple_yaml_loads_fine(self):
        from gco.services.manifest_processor import safe_load_yaml

        result = safe_load_yaml("key: value\nlist:\n  - item1\n  - item2")
        assert result == {"key": "value", "list": ["item1", "item2"]}

    def test_yaml_with_alias_rejected(self):
        from gco.services.manifest_processor import safe_load_yaml

        yaml_with_alias = "anchor: &anchor_name\n  key: value\nalias: *anchor_name"
        with pytest.raises(yaml.YAMLError, match="aliases are not allowed"):
            safe_load_yaml(yaml_with_alias, allow_aliases=False)

    def test_yaml_with_alias_allowed_when_configured(self):
        from gco.services.manifest_processor import safe_load_yaml

        yaml_with_alias = "anchor: &anchor_name\n  key: value\nalias: *anchor_name"
        result = safe_load_yaml(yaml_with_alias, allow_aliases=True)
        assert result["alias"] == {"key": "value"}

    def test_safe_load_all_yaml_no_aliases(self):
        from gco.services.manifest_processor import safe_load_all_yaml

        multi_doc = "---\nfoo: bar\n---\nbaz: qux\n"
        result = safe_load_all_yaml(multi_doc, allow_aliases=False)
        assert len(result) == 2
        assert result[0] == {"foo": "bar"}
        assert result[1] == {"baz": "qux"}

    def test_safe_load_all_yaml_rejects_alias(self):
        from gco.services.manifest_processor import safe_load_all_yaml

        # Both anchor and alias must be in the same document for the alias to resolve
        multi_doc = "---\nfoo: bar\n---\nanchor: &a\n  x: 1\nalias: *a\n"
        with pytest.raises(yaml.YAMLError, match="aliases are not allowed"):
            safe_load_all_yaml(multi_doc, allow_aliases=False)

    def test_safe_load_all_yaml_skips_none_documents(self):
        from gco.services.manifest_processor import safe_load_all_yaml

        # Empty documents between separators produce None
        multi_doc = "---\nfoo: bar\n---\n---\nbaz: qux\n"
        result = safe_load_all_yaml(multi_doc, allow_aliases=False)
        assert len(result) == 2

    def test_billion_laughs_attack_rejected(self):
        """The classic billion-laughs YAML bomb should be rejected."""
        from gco.services.manifest_processor import safe_load_yaml

        # Simplified billion-laughs pattern: anchor defined, then alias used
        bomb = "a: &a\n  - lol\n  - lol\nb:\n  - *a\n  - *a\n"
        with pytest.raises(yaml.YAMLError, match="aliases are not allowed"):
            safe_load_yaml(bomb, allow_aliases=False)


# =============================================================================
# Tests: yaml_max_depth config from cdk.json
# =============================================================================


class TestYamlMaxDepthConfig:
    """Tests for yaml_max_depth configuration."""

    def test_default_depth_is_50(self, mock_k8s_config):
        """When yaml_max_depth is not specified, default is 50."""
        from gco.services.manifest_processor import ManifestProcessor

        with patch("gco.services.manifest_processor.client"):
            proc = ManifestProcessor(
                cluster_id="test",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                },
            )
        assert proc.yaml_max_depth == 50

    def test_custom_depth_from_config(self, mock_k8s_config):
        """yaml_max_depth can be set via config_dict."""
        from gco.services.manifest_processor import ManifestProcessor

        with patch("gco.services.manifest_processor.client"):
            proc = ManifestProcessor(
                cluster_id="test",
                region="us-east-1",
                config_dict={
                    "allowed_namespaces": ["default"],
                    "validation_enabled": True,
                    "yaml_max_depth": 10,
                },
            )
        assert proc.yaml_max_depth == 10
