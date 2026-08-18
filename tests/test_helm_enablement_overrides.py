"""The helm_enabled_overrides context: parsing, resolution, and applier gates.

`--context helm_enabled_overrides=yunikorn,slurm` force-enables optional Helm
charts for one deploy without editing cdk.json. The live release validation
harness threads it through every CDK invocation of a run, so these helpers are
the single source of truth for which charts install AND which gated applier
manifests (Kueue default queues, Slurm NetworkPolicies) apply.
"""

from __future__ import annotations

import pytest

from gco.stacks.regional_stack import (
    _HELM_CHART_CONFIG_KEYS,
    _MANDATORY_CHART_KEYS,
    _compute_kubectl_scheduler_replacements,
    _helm_chart_enabled,
    _parse_helm_enabled_overrides,
)


class TestParseHelmEnabledOverrides:
    def test_none_means_no_overrides(self) -> None:
        assert _parse_helm_enabled_overrides(None) == frozenset()

    def test_comma_separated_string_is_the_cdk_cli_shape(self) -> None:
        assert _parse_helm_enabled_overrides("yunikorn,slurm") == frozenset({"yunikorn", "slurm"})

    def test_whitespace_and_empty_segments_are_tolerated(self) -> None:
        assert _parse_helm_enabled_overrides(" yunikorn , ,slurm ") == frozenset(
            {"yunikorn", "slurm"}
        )

    def test_string_list_is_the_cdk_json_shape(self) -> None:
        assert _parse_helm_enabled_overrides(["slurm"]) == frozenset({"slurm"})

    def test_unknown_names_fail_the_synth_loudly(self) -> None:
        with pytest.raises(ValueError, match="Unknown helm_enabled_overrides"):
            _parse_helm_enabled_overrides("yunikorn,slurrm")

    def test_non_string_shapes_are_refused(self) -> None:
        with pytest.raises(ValueError, match="comma-separated string or string list"):
            _parse_helm_enabled_overrides({"yunikorn": True})

    def test_every_valid_name_is_a_chart_map_key(self) -> None:
        for name in sorted(_HELM_CHART_CONFIG_KEYS):
            assert _parse_helm_enabled_overrides(name) == frozenset({name})


class TestHelmChartEnabled:
    def test_mandatory_charts_ignore_the_toggle(self) -> None:
        for key in _MANDATORY_CHART_KEYS:
            assert _helm_chart_enabled({key: {"enabled": False}}, frozenset(), key)

    def test_override_forces_a_disabled_chart_on(self) -> None:
        helm = {"slurm": {"enabled": False}}
        assert not _helm_chart_enabled(helm, frozenset(), "slurm")
        assert _helm_chart_enabled(helm, frozenset({"slurm"}), "slurm")

    def test_missing_key_defaults_to_enabled(self) -> None:
        # Historical chart_map behavior: an absent cdk.json block enables.
        assert _helm_chart_enabled({}, frozenset(), "volcano")

    def test_configured_toggle_decides_otherwise(self) -> None:
        assert _helm_chart_enabled({"yunikorn": {"enabled": True}}, frozenset(), "yunikorn")
        assert not _helm_chart_enabled({"yunikorn": {"enabled": False}}, frozenset(), "yunikorn")

    def test_malformed_block_defaults_to_enabled(self) -> None:
        assert _helm_chart_enabled({"volcano": "yes"}, frozenset(), "volcano")


class TestSchedulerGateReplacements:
    def test_enabled_schedulers_resolve_their_gates(self) -> None:
        assert _compute_kubectl_scheduler_replacements(kueue_enabled=True, slurm_enabled=True) == {
            "{{KUEUE_ENABLED}}": "true",
            "{{SLURM_ENABLED}}": "true",
        }

    def test_disabled_schedulers_leave_their_gates_unresolved(self) -> None:
        # An absent key leaves the {{...}} token in the manifest, which the
        # applier treats as "skip this file" (and prunes on later disable).
        assert (
            _compute_kubectl_scheduler_replacements(kueue_enabled=False, slurm_enabled=False) == {}
        )
        assert _compute_kubectl_scheduler_replacements(kueue_enabled=True, slurm_enabled=False) == {
            "{{KUEUE_ENABLED}}": "true"
        }
