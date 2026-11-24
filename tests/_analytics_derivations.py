"""Template-to-toggle derivation helpers for the round-trip property test.

:func:`derive_enabled_from_templates` recovers ``analytics_environment.enabled``
from the synthesized CloudFormation templates: True iff any template contains
an ``AWS::SageMaker::Domain`` resource. When the toggle is off the analytics
stack is never instantiated, so no Studio domain is emitted.

``analytics_environment.hyperpod.enabled`` has no deriver: it is deprecated
and changes no template (the round-trip test asserts exactly that).
:func:`sagemaker_role_actions` lists the SageMaker execution role's actions
across the templates, for the checks that the role grants no HyperPod-only
statement.
"""

from __future__ import annotations

from typing import Any

from tests._analytics_template_inspectors import get_sagemaker_role_actions

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def derive_enabled_from_templates(
    templates: dict[str, dict[str, Any]],
) -> bool:
    """Return True iff any template contains an ``AWS::SageMaker::Domain``.

    The Studio domain is created only by
    ``GCOAnalyticsStack._create_studio_domain`` and the analytics stack
    is only instantiated when ``analytics_environment.enabled=true``, so
    its presence is a reliable proxy for the toggle.

    Args:
        templates: ``{stack_name: template_dict}`` as returned by
            :func:`tests._analytics_cdk_overlays.synth_all_stacks`.

    Returns:
        ``True`` if at least one template in the dict has a resource of
        type ``AWS::SageMaker::Domain``; ``False`` otherwise.
    """
    for template in templates.values():
        resources = template.get("Resources", {}) or {}
        for res in resources.values():
            if isinstance(res, dict) and res.get("Type") == "AWS::SageMaker::Domain":
                return True
    return False


def sagemaker_role_actions(templates: dict[str, dict[str, Any]]) -> set[str]:
    """Every IAM action the SageMaker execution role is granted, across ``templates``.

    Empty when the analytics stack is absent (no matching role).
    """
    actions: set[str] = set()
    for template in templates.values():
        actions.update(get_sagemaker_role_actions(template))
    return actions
