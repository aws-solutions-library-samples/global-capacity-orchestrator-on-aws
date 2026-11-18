"""The enabled flags the regional stack reads from the raw cdk.json context.

``queue_processor.enabled``, ``drift_detection.enabled``,
``mcp_server.enabled``, ``volcano_image_mirror.enabled`` and the optional
``helm.<chart>.enabled`` toggles were read by Python truthiness, so
``"enabled": "false"`` deployed the feature. ConfigLoader now validates them
and the stack reads them with the same parser
(``gco.config.config_loader.read_block_flag`` over ``parse_block_flag``): a
JSON boolean as it is, the string ``"true"`` as true with a deprecation
warning, and every other string fails with what to write instead, because
reading ``"false"`` as off would switch off a feature that has been running.
The top-level ``gco_live_validation_disable_efs_automatic_backups`` context,
which ``cdk --context`` passes as a string, goes through ``parse_bool_flag``.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest

from gco.config import config_loader
from gco.config.config_loader import (
    STACK_BLOCK_FLAGS,
    ConfigLoader,
    ConfigValidationError,
    read_block_flag,
)
from gco.enablement_overrides import HELM_CHART_CONFIG_KEYS
from gco.stacks import regional_stack as rs
from gco.stacks.regional_stack import GCORegionalStack
from tests._minimal_cdk_context import minimal_context
from tests.test_regional_stack import MockConfigLoader, TestRegionalStackSynthesis

#: Every flag ConfigLoader validates itself (helm.argocd and helm.slurm are
#: left to their blocks' own validators).
_LOADER_VALIDATED = sorted(set(STACK_BLOCK_FLAGS) - {"helm.argocd.enabled", "helm.slurm.enabled"})


def _context_with_flag(path: str, value: object) -> dict[str, Any]:
    """The minimal context with the flag at ``path`` set to ``value``."""
    context = minimal_context()
    *parents, key = path.split(".")
    block = context
    for part in parents:
        block = block.setdefault(part, {})
    block[key] = value
    return context


def _loader(context: dict[str, Any]) -> ConfigLoader:
    return ConfigLoader(cdk.App(context=context))


class _Stop(Exception):
    """Raised by a patched call to stop a stack method right after the flag read."""


class TestReadBlockFlag:
    def test_an_absent_block_or_key_reads_as_the_default(self) -> None:
        assert read_block_flag(None, "queue_processor.enabled", default=True) is True
        assert read_block_flag({}, "volcano_image_mirror.enabled", default=False) is False
        assert read_block_flag({"polling_interval": 5}, "queue_processor.enabled", default=True)

    @pytest.mark.parametrize("value", [True, False])
    def test_json_booleans_read_as_they_are(self, value: bool) -> None:
        warn = MagicMock()
        read = read_block_flag(
            {"enabled": value}, "mcp_server.enabled", default=not value, warn=warn
        )
        assert read is value
        warn.assert_not_called()

    @pytest.mark.parametrize("value", ["true", "True", "TRUE"])
    def test_the_string_true_reads_as_true_with_a_deprecation_warning(self, value: str) -> None:
        warn = MagicMock()
        assert read_block_flag(
            {"enabled": value}, "drift_detection.enabled", default=False, warn=warn
        )
        (message,) = warn.call_args.args
        assert message.startswith("drift_detection.enabled is the string")
        assert "deprecated" in message

    def test_the_default_warn_drops_the_repeat_the_loader_already_reported(self) -> None:
        assert read_block_flag({"enabled": "true"}, "mcp_server.enabled", default=False) is True

    @pytest.mark.parametrize("value", ["false", "False", "0", "no", "off"])
    def test_any_other_string_fails_because_earlier_releases_read_it_as_on(
        self, value: str
    ) -> None:
        with pytest.raises(ConfigValidationError) as raised:
            read_block_flag({"enabled": value}, "queue_processor.enabled", default=True)
        message = str(raised.value)
        assert message.startswith(
            f"queue_processor.enabled must be true or false (a JSON boolean), not the string "
            f"{value!r}."
        )
        assert "this setting has been on" in message
        assert "Write true to keep it on, or false to turn it off." in message

    def test_an_empty_string_fails_because_earlier_releases_read_it_as_off(self) -> None:
        with pytest.raises(ConfigValidationError, match="this setting has been off"):
            read_block_flag({"enabled": ""}, "drift_detection.enabled", default=True)

    @pytest.mark.parametrize("value", [0, 1, None, [], {}])
    def test_other_types_fail(self, value: object) -> None:
        with pytest.raises(
            ConfigValidationError,
            match=r"^mcp_server\.enabled must be true or false \(a JSON boolean\), got ",
        ):
            read_block_flag({"enabled": value}, "mcp_server.enabled", default=True)

    def test_a_scalar_block_fails_instead_of_reading_as_absent(self) -> None:
        with pytest.raises(ConfigValidationError, match=r"^queue_processor must be an object"):
            read_block_flag(False, "queue_processor.enabled", default=True)

    @pytest.mark.parametrize(
        ("chart", "deletes"),
        [
            ("kuberay", "the kuberay Helm release"),
            ("kueue", "the kueue Helm release and every Kueue queue and workload object"),
            (
                "crossplane",
                "the crossplane Helm release and every Crossplane composite resource with the "
                "resources it composed",
            ),
        ],
    )
    def test_the_message_says_what_switching_a_chart_off_deletes(
        self, chart: str, deletes: str
    ) -> None:
        with pytest.raises(ConfigValidationError) as raised:
            read_block_flag({"enabled": "false"}, f"helm.{chart}.enabled", default=True)
        assert str(raised.value).endswith(f"or false to turn it off, which deletes {deletes}.")

    @pytest.mark.parametrize("path", ["helm.argocd.enabled", "helm.slurm.enabled"])
    def test_flags_that_never_took_a_string_make_no_claim_about_the_past(self, path: str) -> None:
        with pytest.raises(ConfigValidationError) as raised:
            read_block_flag({"enabled": "false"}, path, default=False)
        assert str(raised.value) == (
            f"{path} must be true or false (a JSON boolean), not the string 'false'."
        )

    def test_the_table_covers_every_optional_chart_and_no_mandatory_one(self) -> None:
        charts = {path.split(".")[1] for path in STACK_BLOCK_FLAGS if path.startswith("helm.")}
        assert charts == HELM_CHART_CONFIG_KEYS - rs._MANDATORY_CHART_KEYS
        # Lockstep: the loader skips exactly the toggles the stack ignores.
        assert config_loader._MANDATORY_HELM_CHARTS == rs._MANDATORY_CHART_KEYS


class TestConfigLoaderValidatesTheStackFlags:
    @pytest.mark.parametrize("path", _LOADER_VALIDATED)
    @pytest.mark.parametrize("value", ["false", "False", "0", ""])
    def test_a_string_other_than_true_fails_validation(self, path: str, value: str) -> None:
        with pytest.raises(ConfigValidationError, match=f"^{re.escape(path)} must be"):
            _loader(_context_with_flag(path, value))

    @pytest.mark.parametrize("path", _LOADER_VALIDATED)
    def test_the_string_true_validates_with_a_deprecation_warning(self, path: str) -> None:
        loader = _loader(_context_with_flag(path, "True"))
        assert any(
            warning.startswith(f"{path} is the string 'True', read as true")
            for warning in loader.warnings
        )

    @pytest.mark.parametrize("path", _LOADER_VALIDATED)
    @pytest.mark.parametrize("value", [True, False])
    def test_json_booleans_validate_without_a_warning(self, path: str, value: bool) -> None:
        assert not any(
            path in warning for warning in _loader(_context_with_flag(path, value)).warnings
        )

    def test_a_number_fails_validation(self) -> None:
        with pytest.raises(ConfigValidationError, match=r"^helm\.volcano\.enabled must be"):
            _loader(_context_with_flag("helm.volcano.enabled", 0))

    @pytest.mark.parametrize("chart", ["aws_load_balancer_controller", "keda"])
    def test_a_mandatory_charts_toggle_is_not_read(self, chart: str) -> None:
        # The stack installs these charts whatever the toggle says.
        _loader(_context_with_flag(f"helm.{chart}.enabled", "false"))

    def test_argocd_keeps_its_own_validator_which_takes_no_string(self) -> None:
        with pytest.raises(ConfigValidationError, match=r"helm\.argocd\.enabled must be a boolean"):
            _loader(_context_with_flag("helm.argocd.enabled", "true"))


class TestExplicitContextBool:
    @pytest.mark.parametrize(
        ("raw", "expected"), [(" TRUE ", True), ("false\n", False), ("False", False)]
    )
    def test_strings_are_parsed_by_parse_bool_flag(self, raw: str, expected: bool) -> None:
        assert rs._explicit_context_bool(raw, key="flag") is expected

    @pytest.mark.parametrize("raw", [0, 1, "yes", "", []])
    def test_anything_else_is_a_value_error_naming_the_key_and_value(self, raw: object) -> None:
        with pytest.raises(ValueError, match=r"^flag must be true or false, got ") as raised:
            rs._explicit_context_bool(raw, key="flag")
        assert isinstance(raised.value.__cause__, ConfigValidationError)


class TestHelmChartEnabled:
    def test_the_string_true_enables(self) -> None:
        assert rs._helm_chart_enabled({"yunikorn": {"enabled": "True"}}, frozenset(), "yunikorn")

    @pytest.mark.parametrize("value", ["false", "", 0, None])
    def test_other_values_fail_instead_of_reading_by_truthiness(self, value: object) -> None:
        with pytest.raises(ConfigValidationError, match=r"^helm\.kuberay\.enabled must be"):
            rs._helm_chart_enabled({"kuberay": {"enabled": value}}, frozenset(), "kuberay")

    def test_an_absent_toggle_keeps_the_charts_default(self) -> None:
        assert rs._helm_chart_enabled({"kuberay": {}}, frozenset(), "kuberay") is True
        assert rs._helm_chart_enabled({"crossplane": {}}, frozenset(), "crossplane") is False


def _stub_stack(context: dict[str, Any], **attributes: Any) -> SimpleNamespace:
    """An object with the stack's ``node.try_get_context`` over ``context``."""
    return SimpleNamespace(node=SimpleNamespace(try_get_context=context.get), **attributes)


class TestStackSites:
    def test_volcano_image_mirror(self) -> None:
        config = SimpleNamespace(get_project_name=lambda: "gco")
        read = GCORegionalStack._get_volcano_image_mirror_config
        on = _stub_stack({"volcano_image_mirror": {"enabled": "TRUE"}}, config=config)
        assert read(on) == {"enabled": True, "ecr_namespace": "gco/dockerhub"}
        off = _stub_stack({"volcano_image_mirror": {"enabled": "false"}}, config=config)
        with pytest.raises(ConfigValidationError, match=r"^volcano_image_mirror\.enabled must"):
            read(off)

    def test_drift_detection(self) -> None:
        create = GCORegionalStack._create_drift_detection
        assert create(_stub_stack({"drift_detection": {"enabled": False}})) is None
        with pytest.raises(ConfigValidationError, match=r"^drift_detection\.enabled must"):
            create(_stub_stack({"drift_detection": {"enabled": "false"}}))
        with (
            patch("gco.stacks.regional_stack.kms.Key", side_effect=_Stop) as key,
            pytest.raises(_Stop),
        ):
            create(_stub_stack({"drift_detection": {"enabled": "true"}}))
        assert key.call_args.args[1] == "DriftDetectionTopicKey"

    def test_mcp_server(self) -> None:
        create = GCORegionalStack._create_mcp_role
        config = MagicMock(get_project_name=MagicMock(side_effect=_Stop))
        assert create(_stub_stack({"mcp_server": {"enabled": False}}, config=config)) is None
        with pytest.raises(ConfigValidationError, match=r"^mcp_server\.enabled must"):
            create(_stub_stack({"mcp_server": {"enabled": "false"}}, config=config))
        with pytest.raises(_Stop):
            create(_stub_stack({"mcp_server": {"enabled": "true"}}, config=config))

    @staticmethod
    def _construct(context: dict[str, Any], construct_id: str) -> None:
        """Construct a regional stack whose queue processor image stops the build."""

        def service_image_asset(stack: Any, asset_id: str, _service: str) -> Any:
            if asset_id == "QueueProcessorImage":
                raise _Stop
            return MagicMock(image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/test:x")

        app = cdk.App(context=context)
        with (
            patch.object(GCORegionalStack, "_service_image_asset", service_image_asset),
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackSynthesis._mock_helm_installer,
            ),
        ):
            GCORegionalStack(
                app,
                construct_id,
                config=MockConfigLoader(app),
                region="us-east-1",
                auth_secret_arn=(
                    "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret"  # nosec B106  # test fixture ARN with a fake account ID
                ),
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

    def test_queue_processor(self) -> None:
        with pytest.raises(ConfigValidationError, match=r"^queue_processor\.enabled must"):
            self._construct({"queue_processor": {"enabled": "false"}}, "qp-string-false")
        with pytest.raises(_Stop):
            self._construct({"queue_processor": {"enabled": "TRUE"}}, "qp-string-true")
