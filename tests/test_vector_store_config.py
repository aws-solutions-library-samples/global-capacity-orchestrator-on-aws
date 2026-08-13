# Tests for gco/config/config_loader.py vector_store config block.
# Covers defaults (feature off), partial override merging, the effective
# replica-region derivation, and validation errors for the optional cdk.json
# "vector_store" block.

import aws_cdk as cdk
import pytest

from gco.config.config_loader import ConfigLoader, ConfigValidationError


def _loader(valid_cdk_context, vector_store=None, deployment_regions=None):
    context = dict(valid_cdk_context)
    if vector_store is not None:
        context["vector_store"] = vector_store
    if deployment_regions is not None:
        context["deployment_regions"] = deployment_regions
    return ConfigLoader(cdk.App(context=context))


class TestDefaults:
    def test_disabled_by_default(self, valid_cdk_context):
        loader = _loader(valid_cdk_context)
        assert loader.get_vector_store_enabled() is False
        config = loader.get_vector_store_config()
        assert config["enabled"] is False
        assert config["dimensions"] == 1024
        assert config["distance_function"] == "COSINE"
        assert config["embedding_model_id"] == "amazon.titan-embed-text-v2:0"
        assert config["replica_regions"] == []
        assert config["corpus_prefix"] == "vector-corpus/"

    def test_absent_block_applies_defaults_without_validation_error(self, valid_cdk_context):
        # The block is optional; a config that never mentions vector_store
        # must construct cleanly and stay disabled.
        loader = _loader(valid_cdk_context)
        assert loader.get_vector_store_config()["enabled"] is False

    def test_embedding_model_is_independent_of_the_mission_memory_knob(self, valid_cdk_context):
        # vector_store.embedding_model_id deliberately does not read
        # bedrock.embedding_model_id: the two corpora may use different models.
        context = dict(valid_cdk_context)
        context["bedrock"] = {"embedding_model_id": "amazon.titan-embed-text-v1"}
        loader = ConfigLoader(cdk.App(context=context))
        assert (
            loader.get_vector_store_config()["embedding_model_id"] == "amazon.titan-embed-text-v2:0"
        )


class TestOverrides:
    def test_partial_override_merges_defaults(self, valid_cdk_context):
        loader = _loader(valid_cdk_context, {"enabled": True, "dimensions": 256})
        assert loader.get_vector_store_enabled() is True
        config = loader.get_vector_store_config()
        assert config["dimensions"] == 256
        assert config["distance_function"] == "COSINE"
        assert config["corpus_prefix"] == "vector-corpus/"

    def test_custom_model_and_prefix(self, valid_cdk_context):
        loader = _loader(
            valid_cdk_context,
            {
                "embedding_model_id": "amazon.titan-embed-text-v1",
                "corpus_prefix": "rag/corpus/",
            },
        )
        config = loader.get_vector_store_config()
        assert config["embedding_model_id"] == "amazon.titan-embed-text-v1"
        assert config["corpus_prefix"] == "rag/corpus/"


class TestReplicaRegionDerivation:
    def test_unset_follows_regional_deployment_regions(self, valid_cdk_context):
        loader = _loader(
            valid_cdk_context,
            vector_store={"enabled": True},
            deployment_regions={
                "global": "us-east-2",
                "api_gateway": "us-east-2",
                "monitoring": "us-east-2",
                "regional": ["us-east-1", "eu-west-1"],
            },
        )
        assert loader.get_vector_store_replica_regions() == ["us-east-1", "eu-west-1"]

    def test_follow_mode_excludes_the_global_region(self, valid_cdk_context):
        # A deployment whose global region also hosts a cluster must not try
        # to replicate the table into its own primary region.
        loader = _loader(
            valid_cdk_context,
            vector_store={"enabled": True},
            deployment_regions={
                "global": "us-east-2",
                "api_gateway": "us-east-2",
                "monitoring": "us-east-2",
                "regional": ["us-east-2", "us-east-1"],
            },
        )
        assert loader.get_vector_store_replica_regions() == ["us-east-1"]

    def test_explicit_list_wins_over_regional(self, valid_cdk_context):
        loader = _loader(
            valid_cdk_context,
            vector_store={"enabled": True, "replica_regions": ["eu-central-1"]},
            deployment_regions={
                "global": "us-east-2",
                "api_gateway": "us-east-2",
                "monitoring": "us-east-2",
                "regional": ["us-east-1"],
            },
        )
        assert loader.get_vector_store_replica_regions() == ["eu-central-1"]

    def test_single_region_deployment_yields_no_replicas(self, valid_cdk_context):
        # regional == global -> empty replica set: a single-region global
        # table is valid and expected.
        loader = _loader(
            valid_cdk_context,
            vector_store={"enabled": True},
            deployment_regions={
                "global": "us-east-2",
                "api_gateway": "us-east-2",
                "monitoring": "us-east-2",
                "regional": ["us-east-2"],
            },
        )
        assert loader.get_vector_store_replica_regions() == []


class TestValidation:
    @pytest.mark.parametrize(
        "vector_store",
        [
            {"enabled": "yes"},
            {"enabled": 1},
            {"dimensions": 0},
            {"dimensions": -1},
            {"dimensions": 4097},
            {"dimensions": True},
            {"dimensions": "1024"},
            {"distance_function": "MANHATTAN"},
            {"distance_function": 3},
            {"embedding_model_id": ""},
            {"embedding_model_id": "   "},
            {"embedding_model_id": 7},
            {"replica_regions": "us-east-1"},
            {"replica_regions": [1, 2]},
            {"replica_regions": ["not-a-region"]},
            {"replica_regions": ["us-east-1", "us-east-1"]},
            {"corpus_prefix": ""},
            {"corpus_prefix": "no-trailing-slash"},
            {"corpus_prefix": "/leading/"},
            {"corpus_prefix": 5},
        ],
    )
    def test_invalid_vector_store_raises(self, valid_cdk_context, vector_store):
        with pytest.raises(ConfigValidationError):
            _loader(valid_cdk_context, vector_store)

    def test_replica_in_the_global_region_is_rejected_with_reason(self, valid_cdk_context):
        with pytest.raises(ConfigValidationError) as excinfo:
            _loader(
                valid_cdk_context,
                vector_store={"replica_regions": ["us-east-2"]},
                deployment_regions={
                    "global": "us-east-2",
                    "api_gateway": "us-east-2",
                    "monitoring": "us-east-2",
                    "regional": ["us-east-1"],
                },
            )
        message = str(excinfo.value)
        assert "us-east-2" in message
        assert "primary" in message

    def test_one_way_door_errors_say_so(self, valid_cdk_context):
        with pytest.raises(ConfigValidationError, match="one-way door"):
            _loader(valid_cdk_context, {"dimensions": 9999})
        with pytest.raises(ConfigValidationError, match="immutable after index creation"):
            _loader(valid_cdk_context, {"distance_function": "L2"})

    def test_valid_full_block_passes(self, valid_cdk_context):
        loader = _loader(
            valid_cdk_context,
            {
                "enabled": True,
                "dimensions": 512,
                "distance_function": "EUCLIDEAN",
                "embedding_model_id": "amazon.titan-embed-text-v2:0",
                "replica_regions": ["us-east-1"],
                "corpus_prefix": "corpus/",
            },
        )
        assert loader.get_vector_store_config()["distance_function"] == "EUCLIDEAN"
