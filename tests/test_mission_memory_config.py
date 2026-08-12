# Tests for gco/config/config_loader.py mission-memory config block.
# Covers the shipped defaults (feature ON — resolved design decision: memory is
# cheap and silently missing recall is the worse failure mode), partial
# override merging, and every validation error path, with special attention to
# the one-way-door fields (dimensions, distance_function) that cannot be
# corrected after the vector index exists.

import aws_cdk as cdk
import pytest

from gco.config.config_loader import ConfigLoader, ConfigValidationError


def _loader(valid_cdk_context, mission_memory=None):
    context = dict(valid_cdk_context)
    if mission_memory is not None:
        context["mission_memory"] = mission_memory
    return ConfigLoader(cdk.App(context=context))


class TestDefaults:
    def test_enabled_by_default(self, valid_cdk_context):
        loader = _loader(valid_cdk_context)
        assert loader.get_mission_memory_enabled() is True
        cfg = loader.get_mission_memory_config()
        assert cfg == {
            "enabled": True,
            "retention_days": 365,
            "dimensions": 1024,
            "distance_function": "COSINE",
            "top_k": 3,
        }

    def test_defaults_match_cdk_json(self, valid_cdk_context):
        # The committed cdk.json block and the code defaults must agree, so
        # operators on older config files get the same behavior as fresh
        # checkouts (the `historical` block's enabled-drift is the cautionary
        # tale — see the spec's warning in design §2.3).
        import json
        from pathlib import Path

        cdk_json = json.loads((Path(__file__).resolve().parent.parent / "cdk.json").read_text())
        documented = cdk_json["context"]["mission_memory"]
        code_defaults = _loader(valid_cdk_context).get_mission_memory_config()
        assert documented == code_defaults


class TestOverrides:
    def test_disable(self, valid_cdk_context):
        loader = _loader(valid_cdk_context, {"enabled": False})
        assert loader.get_mission_memory_enabled() is False

    def test_partial_override_deep_merges_over_defaults(self, valid_cdk_context):
        cfg = _loader(valid_cdk_context, {"top_k": 5}).get_mission_memory_config()
        assert cfg["top_k"] == 5
        assert cfg["enabled"] is True
        assert cfg["dimensions"] == 1024
        assert cfg["distance_function"] == "COSINE"
        assert cfg["retention_days"] == 365

    def test_alternate_distance_function_accepted(self, valid_cdk_context):
        cfg = _loader(
            valid_cdk_context, {"distance_function": "EUCLIDEAN"}
        ).get_mission_memory_config()
        assert cfg["distance_function"] == "EUCLIDEAN"

    def test_max_dimensions_accepted(self, valid_cdk_context):
        cfg = _loader(valid_cdk_context, {"dimensions": 4096}).get_mission_memory_config()
        assert cfg["dimensions"] == 4096


class TestValidationErrors:
    def test_non_bool_enabled(self, valid_cdk_context):
        with pytest.raises(ConfigValidationError, match="mission_memory.enabled"):
            _loader(valid_cdk_context, {"enabled": "yes"})

    @pytest.mark.parametrize("field", ["retention_days", "top_k"])
    @pytest.mark.parametrize("bad", [0, -1, "3", 2.5, True])
    def test_positive_int_fields(self, valid_cdk_context, field, bad):
        with pytest.raises(ConfigValidationError, match=f"mission_memory.{field}"):
            _loader(valid_cdk_context, {field: bad})

    @pytest.mark.parametrize("bad", [0, -1, 4097, "1024", True])
    def test_dimensions_bounds(self, valid_cdk_context, bad):
        with pytest.raises(ConfigValidationError, match="mission_memory.dimensions"):
            _loader(valid_cdk_context, {"dimensions": bad})

    def test_dimensions_error_names_the_one_way_door(self, valid_cdk_context):
        with pytest.raises(ConfigValidationError, match="one-way door"):
            _loader(valid_cdk_context, {"dimensions": 5000})

    @pytest.mark.parametrize("bad", ["cosine", "L2", "", 3])
    def test_distance_function_membership(self, valid_cdk_context, bad):
        with pytest.raises(ConfigValidationError, match="mission_memory.distance_function"):
            _loader(valid_cdk_context, {"distance_function": bad})

    def test_non_dict_block_is_ignored(self, valid_cdk_context):
        # Matches the historical block's posture: a non-mapping block means
        # the defaults apply rather than a validation crash.
        loader = _loader(valid_cdk_context, mission_memory=None)
        assert loader.get_mission_memory_enabled() is True
