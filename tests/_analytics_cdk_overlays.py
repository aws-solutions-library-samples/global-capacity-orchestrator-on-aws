"""Shared overlay and synthesis helpers for the analytics-environment
property tests and the project-name scoping tests.

Two public entry points:

* :func:`build_overlay` — shape a ``cdk.json`` context dict from the
  ``(enabled, hyperpod_enabled, regions)`` tuple the Hypothesis strategies
  in :mod:`tests._analytics_strategies` produce.

* :func:`synth_all_stacks` — synthesize the full CDK app with
  :mod:`tests._cdk_app_builder`, the builder the cdk-nag gate and the
  synthesis matrix use, and return every stack's CloudFormation template as a
  plain ``dict``. The templates are the ones a ``cdk deploy --all`` would see,
  regional API bridge stacks and Helm installer included.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk

from tests._cdk_app_builder import load_context, synthesize_app

# ---------------------------------------------------------------------------
# build_overlay — shape a ``cdk.json`` context dict from a toggle tuple.
# ---------------------------------------------------------------------------


def build_overlay(
    enabled: bool,
    hyperpod_enabled: bool,
    regions: list[str],
    *,
    canvas_enabled: bool = False,
) -> dict[str, Any]:
    """Return a cdk.json context overlay dict for the given toggle state.

    The returned dict is suitable for passing as ``context_overrides`` to
    :func:`synth_all_stacks`. Only the keys this feature cares about are
    set — the baseline cdk.json context supplies everything else.

    Args:
        enabled: Value for ``analytics_environment.enabled``.
        hyperpod_enabled: Value for ``analytics_environment.hyperpod.enabled``.
        regions: Regional regions for ``deployment_regions.regional``. The
            global / api-gateway / monitoring regions default to
            ``us-east-2`` to match the baseline cdk.json. If the caller
            needs different non-regional regions they can layer another
            overlay on top of the returned dict.
        canvas_enabled: Value for ``analytics_environment.canvas.enabled``.
            Defaults to ``False`` because most existing callers only
            care about the ``enabled`` × ``hyperpod_enabled`` matrix.

    Returns:
        A shallow-merge-compatible dict shaped like the relevant slice of
        ``cdk.json.context``.
    """
    return {
        "analytics_environment": {
            "enabled": enabled,
            "hyperpod": {"enabled": hyperpod_enabled},
            "canvas": {"enabled": canvas_enabled},
            "cognito": {"domain_prefix": None, "removal_policy": "destroy"},
            "efs": {"removal_policy": "destroy"},
            "studio": {"user_profile_name_prefix": None},
        },
        "deployment_regions": {
            "global": "us-east-2",
            "api_gateway": "us-east-2",
            "monitoring": "us-east-2",
            "regional": list(regions),
        },
    }


# ---------------------------------------------------------------------------
# synth_all_stacks — full-app synth returning {stack_name: template_dict}.
# ---------------------------------------------------------------------------


def synth_all_stacks(overlay: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Synthesize the full CDK app with the given context overlay.

    Merges ``overlay`` over the baseline cdk.json context, builds every stack
    ``app.py`` builds (the optional ``GCOAnalyticsStack`` only when the
    analytics toggle is on), synthesizes offline, and returns a
    ``{stack_name: CloudFormation_template_dict}`` dict.

    Args:
        overlay: cdk.json context overlay, typically produced by
            :func:`build_overlay`. Merged over the baseline by
            :func:`tests._cdk_app_builder.load_context`.

    Returns:
        ``{stack_name: template_dict}`` — every stack the app produced,
        keyed by its construct id (e.g. ``gco-global``,
        ``gco-api-gateway``, ``gco-us-east-1``,
        ``gco-regional-api-us-east-1``, ``gco-monitoring``,
        ``gco-analytics``).
    """
    assembly = synthesize_app(cdk.App(context=load_context(overlay)))
    return {artifact.stack_name: dict(artifact.template) for artifact in assembly.stacks}
