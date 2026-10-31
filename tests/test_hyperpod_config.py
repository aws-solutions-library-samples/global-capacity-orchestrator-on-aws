"""The ``hyperpod`` cdk.json block: shape, defaults, overrides, derived views, tokens.

Every rule :mod:`gco.hyperpod_config` enforces is pinned here, plus the
loader wiring (``hyperpod_regions`` overlays, ``hyperpod_overrides`` and the
``feature_enabled_overrides=hyperpod`` force-on) through a real
``ConfigLoader`` built over the shipped cdk.json context.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest

from gco import hyperpod_config as hp
from gco.config.config_loader import ConfigLoader, ConfigValidationError
from gco.enablement_overrides import FEATURE_OVERRIDE_KEYS, route_enablement_overrides

_REPO = Path(__file__).resolve().parents[1]


def _validate(raw: object) -> dict[str, Any]:
    return hp.validate_hyperpod_config(raw)


@pytest.fixture
def valid_cdk_context() -> dict[str, Any]:
    document = json.loads((_REPO / "cdk.json").read_text(encoding="utf-8"))
    context = document["context"]
    assert context["hyperpod"] == {"enabled": False}, "the shipped block must stay off"
    return copy.deepcopy(context)


def _loader(base: dict[str, Any], **context: Any) -> ConfigLoader:
    merged = {**base, **context}
    return ConfigLoader(cdk.App(context=merged))


class TestDefaults:
    def test_absent_block_is_the_defaults_and_off(self) -> None:
        config = _validate(None)
        assert config == hp.HYPERPOD_DEFAULTS
        assert config is not hp.HYPERPOD_DEFAULTS
        assert hp.hyperpod_enabled(config) is False
        assert config["instance_groups"][0]["instance_type"] == "ml.g5.8xlarge"

    def test_shipped_cdk_json_block_validates(self, valid_cdk_context: dict[str, Any]) -> None:
        config = _validate(valid_cdk_context["hyperpod"])
        assert config["enabled"] is False
        assert config["node_recovery"] == "Automatic"

    def test_enabled_block_keeps_defaults_for_omitted_keys(self) -> None:
        config = _validate({"enabled": True, "instance_groups": [{"name": "gpu"}]})
        group = config["instance_groups"][0]
        assert group["instance_type"] == "ml.g5.8xlarge"
        # Opt in: SageMaker holds a new instance for about two hours while they
        # run and accepts InstanceConnectivity only on multi-node-GPU families
        # (it refused ml.g4dn.xlarge live), so a default that turns them on
        # would make the block valid only for some instance types.
        assert group["deep_health_checks"] == []
        assert group["root_volume_size_gb"] == 100
        assert config["queue"] == {"max_cpu": "1000", "max_memory": "4Ti", "max_gpu": "64"}

    def test_null_over_a_nested_block_keeps_the_defaults(self) -> None:
        config = _validate({"enabled": True, "queue": None})
        assert config["queue"] == hp.HYPERPOD_DEFAULTS["queue"]

    def test_defaults_are_not_shared_between_calls(self) -> None:
        first = _validate({"enabled": True})
        first["instance_groups"][0]["name"] = "mutated"
        assert _validate({"enabled": True})["instance_groups"][0]["name"] == "accelerated"


class TestValidation:
    @pytest.mark.parametrize(
        ("raw", "match"),
        [
            ("yes", r"hyperpod must be an object"),
            ({"bogus": 1}, r"unknown key\(s\): bogus"),
            ({"enabled": "true"}, r"hyperpod\.enabled must be a boolean"),
            ({"cluster_name": "-bad"}, r"hyperpod\.cluster_name must be 1-63"),
            ({"cluster_name": "a" * 64}, r"hyperpod\.cluster_name"),
            (
                {"node_recovery": "Sometimes"},
                r"hyperpod\.node_recovery must be one of Automatic, None",
            ),
            (
                {"availability_zone": "us-east-1"},
                r"hyperpod\.availability_zone must be an Availability Zone",
            ),
            ({"instance_groups": {}}, r"hyperpod\.instance_groups must be a list"),
            ({"instance_groups": ["gpu"]}, r"hyperpod\.instance_groups\[0\] must be an object"),
            (
                {"instance_groups": [{"name": "gpu", "size": 1}]},
                r"instance_groups\[0\] contains unknown key\(s\): size",
            ),
            (
                {"instance_groups": [{"name": "gpu group"}]},
                r"instance_groups\[0\]\.name must be 1-63",
            ),
            (
                {"instance_groups": [{"instance_type": "g5.8xlarge"}]},
                r"instance_type must be a SageMaker HyperPod instance type",
            ),
            (
                {"instance_groups": [{"instance_count": True}]},
                r"instance_count must be an integer between 0 and 10000",
            ),
            (
                {"instance_groups": [{"threads_per_core": 3}]},
                r"threads_per_core must be an integer between 1 and 2",
            ),
            (
                {"instance_groups": [{"deep_health_checks": ["Smoke"]}]},
                r"deep_health_checks must be a list drawn from",
            ),
            (
                {"instance_groups": [{"deep_health_checks": "InstanceStress"}]},
                r"deep_health_checks must be a list",
            ),
            (
                {"instance_groups": [{"training_plan_arn": "tp-123"}]},
                r"training_plan_arn must be a SageMaker training-plan ARN",
            ),
            (
                {"instance_groups": [{"root_volume_size_gb": 0}]},
                r"root_volume_size_gb must be an integer between 1 and 16384",
            ),
            (
                {"instance_groups": [{"name": "a"}, {"name": "a"}]},
                r"names must be unique; duplicated: a",
            ),
            ({"enabled": True, "instance_groups": []}, r"must name at least one instance group"),
            ({"queue": []}, r"hyperpod\.queue must be an object"),
            ({"queue": {"max_pods": "1"}}, r"hyperpod\.queue contains unknown key\(s\): max_pods"),
            ({"queue": {"max_gpu": 64}}, r"hyperpod\.queue\.max_gpu must be a Kubernetes quantity"),
            (
                {"queue": {"max_memory": "lots"}},
                r"hyperpod\.queue\.max_memory must be a Kubernetes quantity",
            ),
            (
                {"removal_policy": "keep"},
                r"hyperpod\.removal_policy must be one of destroy, retain",
            ),
        ],
    )
    def test_malformed_blocks_fail_by_name(self, raw: object, match: str) -> None:
        with pytest.raises(hp.HyperpodConfigError, match=match):
            _validate(raw)

    def test_disabled_block_may_have_no_instance_groups(self) -> None:
        assert _validate({"instance_groups": []})["instance_groups"] == []

    def test_deep_health_checks_are_deduplicated_in_order(self) -> None:
        config = _validate(
            {
                "instance_groups": [
                    {
                        "deep_health_checks": [
                            "InstanceConnectivity",
                            "InstanceStress",
                            "InstanceConnectivity",
                        ]
                    }
                ]
            }
        )
        assert config["instance_groups"][0]["deep_health_checks"] == [
            "InstanceConnectivity",
            "InstanceStress",
        ]

    def test_full_block_round_trips(self) -> None:
        arn = "arn:aws:sagemaker:us-east-1:123456789012:training-plan/p5-week"
        config = _validate(
            {
                "enabled": True,
                "cluster_name": "research-hyperpod",
                "node_recovery": "None",
                "availability_zone": "us-east-1b",
                "instance_groups": [
                    {
                        "name": "p5",
                        "instance_type": "ml.p5.48xlarge",
                        "instance_count": 4,
                        "threads_per_core": 2,
                        "deep_health_checks": [],
                        "training_plan_arn": arn,
                        "root_volume_size_gb": 500,
                    },
                    {"name": "cpu", "instance_type": "ml.c5.xlarge", "instance_count": 0},
                ],
                "queue": {"max_cpu": "2000", "max_memory": "8Ti", "max_gpu": "32"},
                "removal_policy": "retain",
            }
        )
        assert config["cluster_name"] == "research-hyperpod"
        assert config["instance_groups"][0]["training_plan_arn"] == arn
        assert config["instance_groups"][1]["instance_count"] == 0
        assert hp.instance_group_names(config) == ["p5", "cpu"]
        assert hp.cluster_name(config, "gco") == "research-hyperpod"
        assert hp.cluster_name(_validate(None), "gco") == "gco-hyperpod"


class TestOverrides:
    def test_json_string_and_mapping_overrides(self) -> None:
        assert hp.parse_hyperpod_overrides(None) == {}
        assert hp.parse_hyperpod_overrides("  ") == {}
        assert hp.parse_hyperpod_overrides('{"enabled": true}') == {"enabled": True}
        assert hp.parse_hyperpod_overrides({"enabled": True}) == {"enabled": True}

    @pytest.mark.parametrize(
        ("raw", "match"),
        [
            ("{not json", r"must be a JSON object:"),
            ("[1]", r"must be a JSON object, got list"),
            (7, r"got int"),
        ],
    )
    def test_malformed_overrides_fail(self, raw: object, match: str) -> None:
        with pytest.raises(hp.HyperpodConfigError, match=match):
            hp.parse_hyperpod_overrides(raw)

    def test_apply_merges_over_the_block_and_leaves_odd_shapes_alone(self) -> None:
        assert hp.apply_hyperpod_overrides(None, None) is None
        assert hp.apply_hyperpod_overrides({"enabled": False}, "") == {"enabled": False}
        assert hp.apply_hyperpod_overrides(None, '{"enabled": true}') == {"enabled": True}
        merged = hp.apply_hyperpod_overrides(
            {"queue": {"max_gpu": "8"}}, {"enabled": True, "queue": {"max_cpu": "10"}}
        )
        assert merged == {"enabled": True, "queue": {"max_gpu": "8", "max_cpu": "10"}}
        # A non-object block is handed back for the validator to name.
        assert hp.apply_hyperpod_overrides("nope", {"enabled": True}) == "nope"


class TestDerivedViews:
    def test_health_monitoring_agent_image_per_region(self) -> None:
        image = hp.health_monitoring_agent_image("us-east-1")
        assert image == (
            "767398015722.dkr.ecr.us-east-1.amazonaws.com/hyperpod-health-monitoring-agent:"
            + hp.HEALTH_MONITORING_AGENT_TAG
        )
        assert hp.health_monitoring_agent_image("eu-west-2").startswith(
            "011528288831.dkr.ecr.eu-west-2."
        )
        with pytest.raises(
            hp.HyperpodConfigError, match=r"no health-monitoring agent for ca-central-1"
        ):
            hp.health_monitoring_agent_image("ca-central-1")

    def test_node_labels_taints_and_toleration_agree(self) -> None:
        labels = hp.node_labels("gpu")
        assert labels == {
            "gco.aws/capacity-provider": "hyperpod",
            "gco.aws/hyperpod-instance-group": "gpu",
        }
        (taint,) = hp.node_taints()
        toleration = hp.hyperpod_toleration()
        assert taint == {"key": "gco.aws/hyperpod", "value": "true", "effect": "NoSchedule"}
        assert (toleration["key"], toleration["value"], toleration["effect"]) == (
            taint["key"],
            taint["value"],
            taint["effect"],
        )
        assert toleration["operator"] == "Equal"


class TestReplacements:
    def test_disabled_emits_nothing_so_every_gated_token_stays_unreplaced(self) -> None:
        assert (
            hp.compute_hyperpod_replacements(_validate(None), enabled=False, region="us-east-1")
            == {}
        )

    def test_enabled_emits_every_gated_token(self) -> None:
        config = _validate({"enabled": True, "queue": {"max_gpu": "16"}})
        replacements = hp.compute_hyperpod_replacements(config, enabled=True, region="us-west-2")
        assert set(replacements) == set(hp.HYPERPOD_GATED_TOKENS)
        assert replacements["{{HYPERPOD_ENABLED}}"] == "true"
        assert replacements["{{HYPERPOD_HMA_IMAGE}}"] == hp.health_monitoring_agent_image(
            "us-west-2"
        )
        assert replacements["{{HYPERPOD_QUEUE_MAX_GPU}}"] == "16"
        assert replacements["{{HYPERPOD_QUEUE_MAX_MEMORY}}"] == "4Ti"

    def test_unsupported_region_fails_the_synth_instead_of_deploying_without_the_agent(
        self,
    ) -> None:
        with pytest.raises(hp.HyperpodConfigError, match="ca-central-1"):
            hp.compute_hyperpod_replacements(
                _validate({"enabled": True}), enabled=True, region="ca-central-1"
            )


class TestConfigLoaderWiring:
    def test_absent_block_reads_as_off(self, valid_cdk_context: dict[str, Any]) -> None:
        context = {k: v for k, v in valid_cdk_context.items() if k != "hyperpod"}
        assert _loader(context).get_hyperpod_config() == _validate(None)

    def test_valid_block_is_returned_with_defaults(self, valid_cdk_context: dict[str, Any]) -> None:
        loader = _loader(valid_cdk_context, hyperpod={"enabled": True, "node_recovery": "None"})
        config = loader.get_hyperpod_config("us-east-1")
        assert config["enabled"] is True
        assert config["node_recovery"] == "None"
        assert config["instance_groups"][0]["name"] == "accelerated"

    def test_malformed_block_fails_at_construction(self, valid_cdk_context: dict[str, Any]) -> None:
        with pytest.raises(ConfigValidationError, match=r"hyperpod\.node_recovery"):
            _loader(valid_cdk_context, hyperpod={"node_recovery": "Maybe"})

    def test_region_overlay_patches_the_global_block(
        self, valid_cdk_context: dict[str, Any]
    ) -> None:
        loader = _loader(
            valid_cdk_context,
            hyperpod={"queue": {"max_gpu": "8"}},
            hyperpod_regions={"us-east-1": {"enabled": True, "queue": {"max_cpu": "500"}}},
        )
        assert loader.get_hyperpod_config()["enabled"] is False
        regional = loader.get_hyperpod_config("us-east-1")
        assert regional["enabled"] is True
        assert regional["queue"] == {"max_cpu": "500", "max_memory": "4Ti", "max_gpu": "8"}
        assert loader.get_hyperpod_config("eu-west-1") == loader.get_hyperpod_config()

    def test_region_overlay_errors_name_the_region(self, valid_cdk_context: dict[str, Any]) -> None:
        with pytest.raises(
            ConfigValidationError, match=r"hyperpod_regions\.us-east-1 must be an object"
        ):
            _loader(valid_cdk_context, hyperpod_regions={"us-east-1": "on"})
        with pytest.raises(
            ConfigValidationError, match=r"hyperpod_regions must be an object keyed by region"
        ):
            _loader(valid_cdk_context, hyperpod_regions=["us-east-1"])
        with pytest.raises(ConfigValidationError, match=r"hyperpod\.removal_policy"):
            _loader(valid_cdk_context, hyperpod_regions={"us-east-1": {"removal_policy": "keep"}})

    def test_run_scoped_overrides_win_over_block_and_overlay(
        self, valid_cdk_context: dict[str, Any]
    ) -> None:
        loader = _loader(
            valid_cdk_context,
            hyperpod={"enabled": False},
            hyperpod_regions={"us-east-1": {"queue": {"max_gpu": "8"}}},
            hyperpod_overrides='{"enabled": true, "queue": {"max_gpu": "2"}}',
        )
        regional = loader.get_hyperpod_config("us-east-1")
        assert regional["enabled"] is True
        assert regional["queue"]["max_gpu"] == "2"
        with pytest.raises(ConfigValidationError, match="hyperpod_overrides must be a JSON object"):
            _loader(valid_cdk_context, hyperpod_overrides="{oops")

    def test_getter_reports_a_block_that_went_bad_after_construction(
        self, valid_cdk_context: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gco.config import config_loader as loader_module

        loader = _loader(valid_cdk_context)

        def _broken(raw: object) -> dict[str, Any]:
            raise hp.HyperpodConfigError("hyperpod.enabled must be a boolean")

        monkeypatch.setattr(loader_module, "validate_hyperpod_config", _broken)
        with pytest.raises(ConfigValidationError, match=r"hyperpod\.enabled"):
            loader.get_hyperpod_config()

    def test_feature_override_forces_enabled(self, valid_cdk_context: dict[str, Any]) -> None:
        loader = _loader(valid_cdk_context, feature_enabled_overrides="hyperpod")
        assert loader.get_hyperpod_config()["enabled"] is True
        assert loader.get_hyperpod_config("us-east-1")["enabled"] is True

    def test_enable_flag_routes_hyperpod_to_the_feature_override_context(self) -> None:
        assert "hyperpod" in FEATURE_OVERRIDE_KEYS
        assert route_enablement_overrides(("hyperpod",)) == {
            "feature_enabled_overrides": "hyperpod"
        }
