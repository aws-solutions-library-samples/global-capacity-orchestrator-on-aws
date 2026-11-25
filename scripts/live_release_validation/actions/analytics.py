"""analytics: prove the analytics environment every release run deploys."""

from __future__ import annotations

from typing import Any

from ..checks.analytics import effective_analytics_enablement, probe_analytics
from ..models import RunContext


def action_analytics(ctx: RunContext) -> dict[str, Any]:
    """Prove Studio sign-in, profile ownership, the EFS home and user removal, or record the skip.

    With the analytics environment on (every release run turns it on through
    the ``analytics_environment_overrides`` CDK context), the probe in
    ``checks/analytics.py`` drives the real ``gco analytics`` commands and reads
    SageMaker, Cognito, IAM and S3 for the evidence, and leaves one user's
    profile, space and running app for the teardown. With it off, the action
    records the configuration source and skips.
    """
    state = effective_analytics_enablement(ctx)
    if state["enabled"]:
        result: dict[str, Any] = {
            "status": "validated",
            "source": state["source"],
            **probe_analytics(ctx),
        }
    else:
        result = {
            "status": "skipped",
            "reason": (
                f"Disabled by {state['source']}; a release run turns the analytics "
                "environment on through the analytics_environment_overrides CDK context"
            ),
        }
    evidence = {"enablement": state, "analytics": result}
    ctx.checkpoint.state["analytics_validation"] = evidence
    ctx.persist()
    return evidence
