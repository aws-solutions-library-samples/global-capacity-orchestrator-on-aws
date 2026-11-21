"""The platform ALB health check: cdk.json gateway_health_check and alb_config.

``gateway_health_check`` sets the target-group health check of the internal
platform ALB (the default ``TargetGroupConfiguration`` in
``post-helm-gateway.yaml``). It defaults to the probe the manifest always
shipped. The older ``alb_config`` block was required and validated but never
read, and its 30-second interval was not what ran, so it is deprecated: a
cdk.json that still carries it validates with a warning and its values have
no effect.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from gco.config.config_loader import ConfigLoader, ConfigValidationError
from gco.gateway_health_check_config import (
    GATEWAY_HEALTH_CHECK_DEFAULTS,
    GATEWAY_HEALTH_CHECK_RANGES,
    GATEWAY_HEALTH_CHECK_TOKENS,
    GatewayHealthCheckConfigError,
    compute_gateway_health_check_replacements,
    render_gateway_health_check_defaults,
    validate_gateway_health_check_config,
)

ROOT = Path(__file__).resolve().parent.parent
GATEWAY_MANIFEST = (
    ROOT / "lambda" / "kubectl-applier-simple" / "manifests" / "post-helm-gateway.yaml"
)
SHIPPED_CDK_JSON = ROOT / "cdk.json"


class _Node:
    def __init__(self, context: dict[str, Any]) -> None:
        self._context = context

    def try_get_context(self, key: str) -> Any:
        return self._context.get(key)


class _App:
    def __init__(self, context: dict[str, Any]) -> None:
        self.node = _Node(context)


def _context(**overrides: Any) -> dict[str, Any]:
    context = json.loads(SHIPPED_CDK_JSON.read_text(encoding="utf-8"))["context"]
    context.update(overrides)
    return context


def _default_target_group(text: str) -> dict[str, Any]:
    return next(
        doc
        for doc in yaml.safe_load_all(text)
        if doc
        and doc["kind"] == "TargetGroupConfiguration"
        and doc["metadata"]["name"] == "gco-default-target-group"
    )


class TestDefaults:
    def test_defaults_are_the_probe_the_manifest_always_shipped(self) -> None:
        assert GATEWAY_HEALTH_CHECK_DEFAULTS == {
            "interval_seconds": 15,
            "timeout_seconds": 5,
            "healthy_threshold": 2,
            "unhealthy_threshold": 2,
        }

    def test_the_rendered_manifest_matches_the_documented_gateway_contract(self) -> None:
        """docs/openapi/cluster-gateway.json records the health check a stock deploy applies."""
        rendered = render_gateway_health_check_defaults(
            GATEWAY_MANIFEST.read_text(encoding="utf-8")
        )
        health_check = _default_target_group(rendered)["spec"]["defaultConfiguration"][
            "healthCheckConfig"
        ]
        document = json.loads(
            (ROOT / "docs" / "openapi" / "cluster-gateway.json").read_text(encoding="utf-8")
        )
        assert document["x-gco-gateway"]["targetGroup"]["healthCheck"] == health_check
        assert health_check["healthCheckInterval"] == 15
        assert type(health_check["healthCheckInterval"]) is int

    def test_the_manifest_carries_every_token_once(self) -> None:
        text = GATEWAY_MANIFEST.read_text(encoding="utf-8")
        for token in GATEWAY_HEALTH_CHECK_TOKENS.values():
            assert text.count(token) == 1, token

    def test_the_shipped_cdk_json_spells_out_the_defaults(self) -> None:
        context = json.loads(SHIPPED_CDK_JSON.read_text(encoding="utf-8"))["context"]
        assert context["gateway_health_check"] == GATEWAY_HEALTH_CHECK_DEFAULTS
        assert "alb_config" not in context


class TestValidation:
    def test_absent_block_is_the_defaults(self) -> None:
        assert validate_gateway_health_check_config(None) == GATEWAY_HEALTH_CHECK_DEFAULTS

    def test_partial_block_keeps_the_other_defaults(self) -> None:
        merged = validate_gateway_health_check_config({"interval_seconds": 30})
        assert merged == {**GATEWAY_HEALTH_CHECK_DEFAULTS, "interval_seconds": 30}

    def test_comment_keys_are_documentation(self) -> None:
        merged = validate_gateway_health_check_config({"_comment_interval": "seconds"})
        assert merged == GATEWAY_HEALTH_CHECK_DEFAULTS

    def test_the_result_is_a_copy(self) -> None:
        merged = validate_gateway_health_check_config(None)
        merged["interval_seconds"] = 99
        assert GATEWAY_HEALTH_CHECK_DEFAULTS["interval_seconds"] == 15

    @pytest.mark.parametrize("raw", ["15", 15, ["interval_seconds"], True])
    def test_non_object_block_is_rejected(self, raw: object) -> None:
        with pytest.raises(
            GatewayHealthCheckConfigError, match="gateway_health_check must be an object"
        ):
            validate_gateway_health_check_config(raw)

    def test_unknown_key_is_named_with_its_path(self) -> None:
        with pytest.raises(
            GatewayHealthCheckConfigError,
            match=r"unknown key\(s\): gateway_health_check\.health_check_interval;",
        ):
            validate_gateway_health_check_config({"health_check_interval": 30})

    @pytest.mark.parametrize("name", sorted(GATEWAY_HEALTH_CHECK_RANGES))
    def test_each_range_is_inclusive(self, name: str) -> None:
        minimum, maximum = GATEWAY_HEALTH_CHECK_RANGES[name]
        for value in (minimum, maximum):
            # Keep the timeout below the interval while probing either end.
            raw = {name: value}
            if name == "timeout_seconds":
                raw["interval_seconds"] = 300
            if name == "interval_seconds":
                raw["timeout_seconds"] = 2
            assert validate_gateway_health_check_config(raw)[name] == value

    @pytest.mark.parametrize("name", sorted(GATEWAY_HEALTH_CHECK_RANGES))
    def test_values_outside_the_range_are_rejected(self, name: str) -> None:
        minimum, maximum = GATEWAY_HEALTH_CHECK_RANGES[name]
        for value in (minimum - 1, maximum + 1):
            with pytest.raises(
                GatewayHealthCheckConfigError,
                match=rf"gateway_health_check\.{name} must be an integer between "
                rf"{minimum} and {maximum}",
            ):
                validate_gateway_health_check_config({name: value})

    @pytest.mark.parametrize("value", [True, 15.0, "15", None])
    def test_only_integers_are_accepted(self, value: object) -> None:
        with pytest.raises(GatewayHealthCheckConfigError, match="must be an integer"):
            validate_gateway_health_check_config({"interval_seconds": value})

    @pytest.mark.parametrize(("interval", "timeout"), [(10, 10), (10, 11)])
    def test_timeout_must_be_shorter_than_the_interval(self, interval: int, timeout: int) -> None:
        with pytest.raises(GatewayHealthCheckConfigError, match="must be less than"):
            validate_gateway_health_check_config(
                {"interval_seconds": interval, "timeout_seconds": timeout}
            )


class TestRendering:
    def test_replacements_are_plain_decimal_strings(self) -> None:
        replacements = compute_gateway_health_check_replacements(
            {
                "interval_seconds": 30,
                "timeout_seconds": 10,
                "healthy_threshold": 3,
                "unhealthy_threshold": 4,
            }
        )
        assert replacements == {
            "{{GATEWAY_HEALTH_CHECK_INTERVAL_SECONDS}}": "30",
            "{{GATEWAY_HEALTH_CHECK_TIMEOUT_SECONDS}}": "10",
            "{{GATEWAY_HEALTHY_THRESHOLD_COUNT}}": "3",
            "{{GATEWAY_UNHEALTHY_THRESHOLD_COUNT}}": "4",
        }

    def test_text_without_tokens_is_unchanged(self) -> None:
        assert render_gateway_health_check_defaults("kind: Gateway\n") == "kind: Gateway\n"


class TestConfigLoader:
    def test_the_shipped_cdk_json_resolves_the_defaults_without_warnings(self) -> None:
        config = ConfigLoader(_App(_context()))
        assert config.get_gateway_health_check_config() == GATEWAY_HEALTH_CHECK_DEFAULTS
        assert config.warnings == []

    def test_configured_values_are_returned(self) -> None:
        config = ConfigLoader(
            _App(_context(gateway_health_check={"interval_seconds": 60, "timeout_seconds": 20}))
        )
        assert config.get_gateway_health_check_config() == {
            **GATEWAY_HEALTH_CHECK_DEFAULTS,
            "interval_seconds": 60,
            "timeout_seconds": 20,
        }

    def test_an_invalid_block_fails_validation(self) -> None:
        with pytest.raises(ConfigValidationError, match=r"gateway_health_check\.healthy_threshold"):
            ConfigLoader(_App(_context(gateway_health_check={"healthy_threshold": 1})))

    @pytest.mark.parametrize(
        "alb_config",
        [
            # What v8.23.0 shipped, and values the old validator rejected:
            # neither changes anything any more.
            {
                "health_check_interval": 30,
                "health_check_timeout": 5,
                "healthy_threshold": 2,
                "unhealthy_threshold": 2,
            },
            {"health_check_timeout": 0},
            {},
        ],
    )
    def test_alb_config_is_deprecated_and_ignored(self, alb_config: dict[str, Any]) -> None:
        config = ConfigLoader(_App(_context(alb_config=alb_config)))
        assert config.get_gateway_health_check_config() == GATEWAY_HEALTH_CHECK_DEFAULTS
        (warning,) = config.warnings
        assert warning.startswith("alb_config is deprecated and ignored:")
        assert "gateway_health_check" in warning

    def test_alb_config_is_no_longer_required(self) -> None:
        context = _context()
        context.pop("alb_config", None)
        assert ConfigLoader(_App(context)).warnings == []

    def test_each_warning_is_recorded_and_logged_once(self, caplog) -> None:
        config = ConfigLoader(_App(_context(alb_config={})))
        config._warn_deprecated_blocks()
        assert len(config.warnings) == 1
        assert [record.getMessage() for record in caplog.records].count(config.warnings[0]) == 1


class TestRegionalStackAccessor:
    """``GCORegionalStack._gateway_health_check_config`` with real and double configs."""

    @staticmethod
    def _resolve(config: object, context: dict[str, Any]) -> dict[str, int]:
        from gco.stacks.regional_stack import GCORegionalStack

        stub = types.SimpleNamespace(config=config, node=_Node(context))
        return GCORegionalStack._gateway_health_check_config(stub)  # type: ignore[arg-type]

    def test_a_loader_answers_through_its_getter(self) -> None:
        config = ConfigLoader(_App(_context(gateway_health_check={"interval_seconds": 45})))
        assert self._resolve(config, {})["interval_seconds"] == 45

    def test_a_double_without_the_getter_reads_the_stack_context(self) -> None:
        resolved = self._resolve(object(), {"gateway_health_check": {"unhealthy_threshold": 5}})
        assert resolved == {**GATEWAY_HEALTH_CHECK_DEFAULTS, "unhealthy_threshold": 5}

    def test_a_double_without_the_getter_and_without_context_gets_the_defaults(self) -> None:
        assert self._resolve(object(), {}) == GATEWAY_HEALTH_CHECK_DEFAULTS
