"""Property-based test for the analytics toggle round-trip.

*For any* ``(enabled, hyperpod_enabled) ∈ {true, false}²``, synthesizing
the full CDK app with that toggle pair SHALL:

* recover ``enabled`` exactly from the templates
  (:func:`~tests._analytics_derivations.derive_enabled_from_templates`
  returns True iff any template contains an ``AWS::SageMaker::Domain``), and
* produce the same templates whatever ``hyperpod_enabled`` is.

``analytics_environment.hyperpod.enabled`` is deprecated and ignored. It
used to add CreateTrainingJob, DescribeTrainingJob, StopTrainingJob,
DescribeClusterNode and ListClusterNodes to the SageMaker execution role,
which ``AmazonSageMakerFullAccess`` already allows, plus
``sagemaker:ClusterInstance`` and ``sagemaker:ClusterInstanceGroup``, which
are not IAM actions. The key is still accepted so existing cdk.json files
synth.

Cardinality of the input space is 4, so ``max_examples=4`` is already
exhaustive. Hypothesis is used for its boilerplate-free replay /
shrinking machinery, not for input coverage.
"""

from __future__ import annotations

import functools
import json
from typing import Any

from hypothesis import HealthCheck, given, settings

from tests._analytics_cdk_overlays import build_overlay, synth_all_stacks
from tests._analytics_derivations import derive_enabled_from_templates, sagemaker_role_actions
from tests._analytics_strategies import roundtrip_fixtures

# ---------------------------------------------------------------------------
# Cached synth — one entry per unique (enabled, hyperpod) tuple.
# ---------------------------------------------------------------------------

_REGIONS_FIXED: list[str] = ["us-east-1"]


@functools.cache
def _cached_templates_frozen(enabled: bool, hyperpod_enabled: bool) -> tuple[tuple[str, str], ...]:
    """Return every (stack_name, JSON-serialized template) pair for the
    given toggle tuple as a hashable structure.

    Serializing the template dicts via ``json.dumps`` + a sorted-key
    ordering makes the cache entry hashable; callers materialize it
    back into a ``{stack_name: template_dict}`` dict on every use so
    mutations don't bleed between examples.
    """
    overlay = build_overlay(enabled, hyperpod_enabled, _REGIONS_FIXED)
    templates = synth_all_stacks(overlay)
    return tuple(
        (name, json.dumps(template, sort_keys=True)) for name, template in templates.items()
    )


def _unfreeze(
    frozen: tuple[tuple[str, str], ...],
) -> dict[str, dict[str, Any]]:
    """Rebuild a ``{stack_name: template_dict}`` from the cache entry."""
    return {name: json.loads(blob) for name, blob in frozen}


def _analytics_template(frozen: tuple[tuple[str, str], ...]) -> str | None:
    """The serialized analytics stack template, or None when there is none."""
    return next((blob for name, blob in frozen if name.endswith("-analytics")), None)


# ---------------------------------------------------------------------------
# Round-trip property.
# ---------------------------------------------------------------------------


class TestToggleRoundTrip:
    """The ``(enabled, hyperpod_enabled)`` tuple round-trips through
    full-app synth and template inspection.
    """

    @classmethod
    def setup_class(cls) -> None:
        """Pre-warm the cache for all 4 points of the input space.

        This is exhaustive: ``_cached_templates_frozen`` is deterministic
        in its two boolean arguments, so 2×2=4 cache entries cover
        every possible Hypothesis draw.
        """
        for enabled in (False, True):
            for hyperpod in (False, True):
                _cached_templates_frozen(enabled, hyperpod)

    @settings(
        max_examples=4,
        deadline=20000,
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.function_scoped_fixture,
            HealthCheck.data_too_large,
        ],
    )
    @given(fixture=roundtrip_fixtures)
    def test_round_trip_recovers_enabled_and_ignores_hyperpod(
        self, fixture: tuple[bool, bool]
    ) -> None:
        """``enabled`` round-trips; ``hyperpod_enabled`` changes nothing."""
        enabled, hyperpod_enabled = fixture
        frozen = _cached_templates_frozen(enabled, hyperpod_enabled)

        enabled_derived = derive_enabled_from_templates(_unfreeze(frozen))
        assert enabled_derived == enabled, (
            f"Round-trip failed on ``enabled``: expected "
            f"{enabled!r} (input), got {enabled_derived!r} (derived). "
            f"Input tuple was (enabled={enabled}, hyperpod={hyperpod_enabled}). "
            f"The deriver looks for AWS::SageMaker::Domain — check "
            f"whether the analytics stack was (not) instantiated as "
            f"expected in app.py / synth_all_stacks."
        )
        # The analytics stack is the only one that reads the toggle. (The
        # whole app is not compared: the regional stacks carry a per-synth
        # add-on deployment timestamp.)
        other = _cached_templates_frozen(enabled, not hyperpod_enabled)
        assert _analytics_template(frozen) == _analytics_template(other), (
            "analytics_environment.hyperpod.enabled is deprecated and must not "
            f"change the analytics stack's template (enabled={enabled})."
        )


# ---------------------------------------------------------------------------
# Smoke tests on each input in isolation — catches template-shape
# regressions without waiting on a full Hypothesis run.
# ---------------------------------------------------------------------------


class TestDerivers:
    """Pinned to the four concrete input tuples — fails fast if template
    shape drifts away from what the deriver expects.
    """

    def test_enabled_false_hyperpod_false(self) -> None:
        templates = _unfreeze(_cached_templates_frozen(False, False))
        assert derive_enabled_from_templates(templates) is False
        assert sagemaker_role_actions(templates) == set()

    def test_enabled_true_hyperpod_false(self) -> None:
        templates = _unfreeze(_cached_templates_frozen(True, False))
        assert derive_enabled_from_templates(templates) is True
        assert "sts:GetCallerIdentity" in sagemaker_role_actions(templates)

    def test_enabled_true_hyperpod_true_grants_no_hyperpod_statement(self) -> None:
        """The removed statement's two made-up actions stay gone."""
        templates = _unfreeze(_cached_templates_frozen(True, True))
        actions = sagemaker_role_actions(templates)
        assert derive_enabled_from_templates(templates) is True
        assert not {a for a in actions if a.startswith("sagemaker:ClusterInstance")}
        assert "sagemaker:CreateTrainingJob" not in actions

    def test_enabled_false_hyperpod_true_has_no_analytics_effect(self) -> None:
        templates = _unfreeze(_cached_templates_frozen(False, True))
        assert derive_enabled_from_templates(templates) is False
        assert sagemaker_role_actions(templates) == set()
