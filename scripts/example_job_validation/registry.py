"""The example-validation action registry.

Reuses the live-release-validation actions verbatim for everything except
the two new ones: ``static`` (offline example checks, no AWS) and
``examples`` (the live per-example lifecycle). Structure tests hold this
registry in lockstep with ``docs/EXAMPLE_VALIDATION.md``.
"""

from __future__ import annotations

from scripts.live_release_validation.actions import (
    action_baseline,
    action_deploy,
    action_destroy,
    action_final_inventory,
    action_preflight,
)
from scripts.live_release_validation.registry import ActionDefinition

from .actions import action_examples, action_static


def build_action_registry() -> dict[str, ActionDefinition]:
    """Return actions in dependency-safe execution order."""
    definitions = (
        ActionDefinition(
            "preflight",
            "Verify exact git, account, configuration, and ownership identity",
            (),
            action_preflight,
        ),
        ActionDefinition(
            "static",
            "Run the offline example checks (parse, transport gates, catalog symmetry)",
            (),
            action_static,
        ),
        ActionDefinition(
            "baseline",
            "Capture protected CloudFormation and ECR baselines",
            ("preflight",),
            action_baseline,
        ),
        ActionDefinition(
            "deploy",
            "Deploy the configured GCO topology plus example-required overrides",
            ("baseline", "static"),
            action_deploy,
        ),
        ActionDefinition(
            "examples",
            "Run every selected example through its documented submission path",
            ("deploy",),
            action_examples,
        ),
        ActionDefinition(
            "destroy",
            "Destroy all run-owned infrastructure in dependency order",
            ("deploy",),
            action_destroy,
        ),
        ActionDefinition(
            "final-inventory",
            "Verify zero residual resources and exact baseline preservation",
            ("destroy",),
            action_final_inventory,
        ),
    )
    return {definition.name: definition for definition in definitions}
