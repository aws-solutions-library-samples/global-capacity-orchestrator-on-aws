"""Tests for the ``feature_enabled_overrides`` CDK context.

The override force-enables optional infrastructure features (Aurora
pgvector, Valkey, FSx for Lustre, the vector store) for a single deploy
without editing cdk.json — the infrastructure sibling of
``helm_enabled_overrides``. Validation harnesses rely on it because their
preflight requires a clean worktree, so cdk.json can never be rewritten
mid-run.
"""

from __future__ import annotations

import pytest
from aws_cdk import App

from gco.config.config_loader import (
    FEATURE_OVERRIDE_CONTEXT_KEY,
    FEATURE_OVERRIDE_KEYS,
    ConfigLoader,
    ConfigValidationError,
    parse_feature_enabled_overrides,
)


def _repo_context() -> dict[str, object]:
    """The repository's real cdk.json context (features all default-disabled)."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    context = json.loads((root / "cdk.json").read_text(encoding="utf-8"))["context"]
    assert isinstance(context, dict)
    return context


def _loader(extra_context: dict[str, object] | None = None) -> ConfigLoader:
    context: dict[str, object] = dict(_repo_context())
    if extra_context:
        context.update(extra_context)
    return ConfigLoader(App(context=context))


class TestParseFeatureEnabledOverrides:
    def test_none_is_empty(self) -> None:
        assert parse_feature_enabled_overrides(None) == frozenset()

    def test_comma_string_shape(self) -> None:
        assert parse_feature_enabled_overrides("valkey, aurora_pgvector") == frozenset(
            {"valkey", "aurora_pgvector"}
        )

    def test_list_shape(self) -> None:
        assert parse_feature_enabled_overrides(["fsx_lustre"]) == frozenset({"fsx_lustre"})

    def test_empty_string_is_empty(self) -> None:
        assert parse_feature_enabled_overrides(" , ") == frozenset()

    def test_unknown_name_raises_with_valid_list(self) -> None:
        with pytest.raises(ConfigValidationError, match="Unknown feature_enabled_overrides"):
            parse_feature_enabled_overrides("valkey,bogus")

    def test_non_string_shape_rejected(self) -> None:
        with pytest.raises(ConfigValidationError, match="comma-separated string"):
            parse_feature_enabled_overrides(42)

    def test_override_keys_are_the_documented_four(self) -> None:
        assert (
            frozenset({"aurora_pgvector", "valkey", "fsx_lustre", "vector_store"})
            == FEATURE_OVERRIDE_KEYS
        )


class TestOverrideForcesEnablement:
    def test_defaults_stay_disabled_without_override(self) -> None:
        loader = _loader()
        assert loader.get_valkey_config()["enabled"] is False
        assert loader.get_aurora_pgvector_config()["enabled"] is False
        assert loader.get_fsx_lustre_config("us-east-1")["enabled"] is False
        assert loader.get_vector_store_enabled() is False

    def test_each_feature_can_be_forced_on(self) -> None:
        loader = _loader(
            {FEATURE_OVERRIDE_CONTEXT_KEY: "aurora_pgvector,valkey,fsx_lustre,vector_store"}
        )
        assert loader.get_valkey_config()["enabled"] is True
        assert loader.get_aurora_pgvector_config()["enabled"] is True
        assert loader.get_fsx_lustre_config("us-east-1")["enabled"] is True
        assert loader.get_vector_store_enabled() is True

    def test_override_is_selective(self) -> None:
        loader = _loader({FEATURE_OVERRIDE_CONTEXT_KEY: "valkey"})
        assert loader.get_valkey_config()["enabled"] is True
        assert loader.get_aurora_pgvector_config()["enabled"] is False
        assert loader.get_fsx_lustre_config("us-east-1")["enabled"] is False
        assert loader.get_vector_store_enabled() is False

    def test_vector_store_override_does_not_clobber_other_settings(self) -> None:
        loader = _loader(
            {
                FEATURE_OVERRIDE_CONTEXT_KEY: "vector_store",
                "vector_store": {"enabled": False, "dimensions": 256},
            }
        )
        merged = loader.get_vector_store_config()
        assert merged["enabled"] is True
        assert merged["dimensions"] == 256
        # The one-way-door settings keep their defaults when not configured.
        assert merged["embedding_model_id"] == "amazon.titan-embed-text-v2:0"

    def test_override_does_not_clobber_other_settings(self) -> None:
        loader = _loader(
            {
                FEATURE_OVERRIDE_CONTEXT_KEY: "valkey",
                "valkey": {"enabled": False, "max_data_storage_gb": 9},
            }
        )
        merged = loader.get_valkey_config()
        assert merged["enabled"] is True
        assert merged["max_data_storage_gb"] == 9

    def test_explicitly_enabled_feature_unaffected(self) -> None:
        loader = _loader({"valkey": {"enabled": True}})
        assert loader.get_valkey_config()["enabled"] is True

    def test_unknown_override_fails_at_config_read(self) -> None:
        loader = _loader({FEATURE_OVERRIDE_CONTEXT_KEY: "slurm"})
        with pytest.raises(ConfigValidationError, match="Unknown feature_enabled_overrides"):
            loader.get_valkey_config()
