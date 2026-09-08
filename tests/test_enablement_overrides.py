"""The run-scoped enablement-override contract shared by the CLI and recorders.

``gco/enablement_overrides.py`` lets one run force optional add-ons on without
rewriting ``cdk.json``, which is what makes a full-topology demo recording
possible while the shipped defaults stay off (every one of these features bills
continuously). It deliberately re-declares the two canonical name sets instead
of importing them, because the authoritative parsers live in modules that
import ``aws_cdk`` and the CLI must validate ``--enable`` before the CDK
toolchain is known to be importable.

That duplication is only safe if it cannot drift, so the lockstep tests here
are the load-bearing ones: they import the heavyweight authorities and assert
set equality both ways.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.commands.stacks_cmd import stacks
from cli.config import GCOConfig
from gco.enablement_overrides import (
    FEATURE_OVERRIDE_CONTEXT_KEY,
    FEATURE_OVERRIDE_KEYS,
    HELM_CHART_CONFIG_KEYS,
    HELM_OVERRIDE_CONTEXT_KEY,
    EnablementOverrideError,
    route_enablement_overrides,
    split_override_names,
)


class TestLockstepWithTheAuthoritativeParsers:
    """The duplicated name sets must equal the sets CDK synth actually enforces."""

    def test_feature_keys_match_the_config_loader(self) -> None:
        from gco.config.config_loader import (
            FEATURE_OVERRIDE_CONTEXT_KEY as loader_key,
        )
        from gco.config.config_loader import (
            FEATURE_OVERRIDE_KEYS as loader_keys,
        )

        assert loader_keys == FEATURE_OVERRIDE_KEYS
        assert loader_key == FEATURE_OVERRIDE_CONTEXT_KEY

    def test_chart_keys_match_the_regional_stack(self) -> None:
        from gco.stacks.regional_stack import (
            _HELM_CHART_CONFIG_KEYS as stack_keys,
        )
        from gco.stacks.regional_stack import (
            _HELM_OVERRIDE_CONTEXT_KEY as stack_key,
        )

        assert stack_keys == HELM_CHART_CONFIG_KEYS
        assert stack_key == HELM_OVERRIDE_CONTEXT_KEY

    def test_every_routed_name_is_accepted_by_its_own_parser(self) -> None:
        """Routing is only useful if the receiving parser also accepts the name."""
        from gco.config.config_loader import parse_feature_enabled_overrides
        from gco.stacks.regional_stack import _parse_helm_enabled_overrides

        for name in sorted(FEATURE_OVERRIDE_KEYS):
            routed = route_enablement_overrides([name])
            assert parse_feature_enabled_overrides(
                routed[FEATURE_OVERRIDE_CONTEXT_KEY]
            ) == frozenset({name})
        for name in sorted(HELM_CHART_CONFIG_KEYS):
            routed = route_enablement_overrides([name])
            assert _parse_helm_enabled_overrides(routed[HELM_OVERRIDE_CONTEXT_KEY]) == frozenset(
                {name}
            )

    def test_the_two_namespaces_are_disjoint(self) -> None:
        """Disjointness is what lets a single --enable flag route unambiguously."""
        assert not FEATURE_OVERRIDE_KEYS & HELM_CHART_CONFIG_KEYS


class TestSplitOverrideNames:
    def test_repeated_and_comma_joined_shapes_are_equivalent(self) -> None:
        assert split_override_names(["valkey", "slurm"]) == ["valkey", "slurm"]
        assert split_override_names(["valkey,slurm"]) == ["valkey", "slurm"]

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert split_override_names([" valkey , slurm "]) == ["valkey", "slurm"]

    def test_empty_segments_are_dropped_so_a_trailing_comma_is_not_an_error(self) -> None:
        assert split_override_names(["valkey,"]) == ["valkey"]
        assert split_override_names([" , "]) == []

    def test_no_arguments_yields_nothing(self) -> None:
        assert split_override_names([]) == []

    def test_duplicates_survive_splitting(self) -> None:
        """De-duplication is route_enablement_overrides' job, not the splitter's."""
        assert split_override_names(["valkey,valkey"]) == ["valkey", "valkey"]


class TestRouteEnablementOverrides:
    def test_nothing_requested_yields_an_empty_context(self) -> None:
        assert route_enablement_overrides([]) == {}

    def test_features_route_to_the_feature_context_key(self) -> None:
        assert route_enablement_overrides(["valkey", "fsx_lustre"]) == {
            FEATURE_OVERRIDE_CONTEXT_KEY: "fsx_lustre,valkey"
        }

    def test_charts_route_to_the_helm_context_key(self) -> None:
        assert route_enablement_overrides(["yunikorn", "slurm"]) == {
            HELM_OVERRIDE_CONTEXT_KEY: "slurm,yunikorn"
        }

    def test_a_mixed_request_populates_both_keys(self) -> None:
        assert route_enablement_overrides(["fsx_lustre,valkey,aurora_pgvector,slurm,yunikorn"]) == {
            FEATURE_OVERRIDE_CONTEXT_KEY: "aurora_pgvector,fsx_lustre,valkey",
            HELM_OVERRIDE_CONTEXT_KEY: "slurm,yunikorn",
        }

    def test_ordering_and_duplicates_do_not_change_the_context(self) -> None:
        """Byte-stable output keeps a re-recorded or resumed run's argv identical."""
        assert route_enablement_overrides(["yunikorn,valkey"]) == route_enablement_overrides(
            ["valkey", "yunikorn", "valkey"]
        )

    def test_unknown_name_raises_with_the_full_valid_list(self) -> None:
        with pytest.raises(EnablementOverrideError) as excinfo:
            route_enablement_overrides(["valkey,bogus"])
        message = str(excinfo.value)
        assert "bogus" in message
        # Both namespaces are offered, because the caller cannot be expected to
        # know which of the two a name was supposed to belong to.
        assert "valkey" in message
        assert "yunikorn" in message

    def test_every_unknown_name_is_reported_not_just_the_first(self) -> None:
        with pytest.raises(EnablementOverrideError, match="alpha, zeta"):
            route_enablement_overrides(["zeta", "alpha"])

    def test_the_error_is_a_value_error(self) -> None:
        """Click's BadParameter wrapping and generic callers both rely on this."""
        assert issubclass(EnablementOverrideError, ValueError)


class TestStacksEnableOption:
    """``--enable`` on the four stack lifecycle commands.

    Every AWS boundary is mocked; these assert only that the flag reaches
    ``set_extra_cdk_context``, which is what makes the override ride every CDK
    invocation of the run.
    """

    @staticmethod
    def _config() -> GCOConfig:
        return GCOConfig(
            project_name="test-gco",
            default_region="us-east-1",
            output_format="table",
            verbose=False,
        )

    def _invoke(self, args: list[str], manager: MagicMock) -> Any:
        with patch("cli.stacks.get_stack_manager", return_value=manager):
            return CliRunner().invoke(stacks, args, obj=self._config())

    @staticmethod
    def _manager() -> MagicMock:
        manager = MagicMock()
        manager.list_stacks.return_value = ["test-gco-global"]
        manager.deploy.return_value = True
        manager.destroy.return_value = True
        manager.deploy_orchestrated.return_value = (True, ["test-gco-global"], [])
        manager.destroy_orchestrated.return_value = (True, ["test-gco-global"], [])
        return manager

    @pytest.mark.parametrize(
        ("argv", "expected_context"),
        [
            (
                ["deploy-all", "-y", "--enable", "fsx_lustre,valkey,aurora_pgvector"],
                {FEATURE_OVERRIDE_CONTEXT_KEY: "aurora_pgvector,fsx_lustre,valkey"},
            ),
            (
                ["deploy-all", "-y", "--enable", "slurm", "--enable", "yunikorn"],
                {HELM_OVERRIDE_CONTEXT_KEY: "slurm,yunikorn"},
            ),
            (
                ["deploy", "test-gco-global", "-y", "--enable", "valkey,slurm"],
                {
                    FEATURE_OVERRIDE_CONTEXT_KEY: "valkey",
                    HELM_OVERRIDE_CONTEXT_KEY: "slurm",
                },
            ),
            (
                ["destroy", "test-gco-global", "-y", "--enable", "fsx_lustre"],
                {FEATURE_OVERRIDE_CONTEXT_KEY: "fsx_lustre"},
            ),
        ],
    )
    def test_requested_names_reach_the_cdk_context(
        self, argv: list[str], expected_context: dict[str, str]
    ) -> None:
        manager = self._manager()
        result = self._invoke(argv, manager)
        assert result.exit_code == 0, result.output
        manager.set_extra_cdk_context.assert_called_once_with(expected_context)

    def test_destroy_all_carries_the_overrides_so_forced_on_resources_are_removed(self) -> None:
        """A destroy without the override would synthesize a graph missing the
        forced-on FSx/Aurora/Valkey resources and silently leave them billing."""
        manager = self._manager()
        result = self._invoke(
            ["destroy-all", "-y", "--enable", "fsx_lustre,valkey,aurora_pgvector,slurm,yunikorn"],
            manager,
        )
        assert result.exit_code == 0, result.output
        manager.set_extra_cdk_context.assert_called_once_with(
            {
                FEATURE_OVERRIDE_CONTEXT_KEY: "aurora_pgvector,fsx_lustre,valkey",
                HELM_OVERRIDE_CONTEXT_KEY: "slurm,yunikorn",
            }
        )

    def test_the_applied_context_is_disclosed_in_the_output(self) -> None:
        """The recorded GIF must show what the run forced on."""
        manager = self._manager()
        result = self._invoke(["deploy-all", "-y", "--enable", "valkey,slurm"], manager)
        assert result.exit_code == 0, result.output
        assert f"Run-scoped override: {FEATURE_OVERRIDE_CONTEXT_KEY}=valkey" in result.output
        assert f"Run-scoped override: {HELM_OVERRIDE_CONTEXT_KEY}=slurm" in result.output

    @pytest.mark.parametrize(
        "argv",
        [
            ["deploy-all", "-y"],
            ["deploy", "test-gco-global", "-y"],
            ["destroy", "test-gco-global", "-y"],
            ["destroy-all", "-y"],
        ],
    )
    def test_omitting_the_flag_registers_no_context_at_all(self, argv: list[str]) -> None:
        """The default path must stay byte-identical to before the flag existed."""
        manager = self._manager()
        result = self._invoke(argv, manager)
        assert result.exit_code == 0, result.output
        manager.set_extra_cdk_context.assert_not_called()
        assert "Run-scoped override" not in result.output

    @pytest.mark.parametrize(
        "argv",
        [
            ["deploy-all", "-y", "--enable", "bogus"],
            ["deploy", "test-gco-global", "-y", "--enable", "bogus"],
            ["destroy", "test-gco-global", "-y", "--enable", "bogus"],
            ["destroy-all", "-y", "--enable", "bogus"],
        ],
    )
    def test_an_unknown_name_fails_at_parse_time_before_touching_aws(self, argv: list[str]) -> None:
        """Click's usage error (exit 2), not a mid-deploy failure."""
        manager = self._manager()
        result = self._invoke(argv, manager)
        assert result.exit_code == 2
        assert "Unknown --enable name(s): bogus" in result.output
        manager.set_extra_cdk_context.assert_not_called()
        manager.deploy_orchestrated.assert_not_called()
        manager.destroy_orchestrated.assert_not_called()
        manager.deploy.assert_not_called()
        manager.destroy.assert_not_called()

    def test_the_flag_is_advertised_on_every_lifecycle_command(self) -> None:
        runner = CliRunner()
        for command in ("deploy", "deploy-all", "destroy", "destroy-all"):
            result = runner.invoke(stacks, [command, "--help"], obj=self._config())
            assert result.exit_code == 0, result.output
            assert "--enable" in result.output, command
