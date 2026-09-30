"""The upgrade-validation action registry.

Reuses the live-release-validation handlers verbatim for ``baseline``,
``topology``, ``destroy`` and ``final-inventory``; the other actions live in
``actions.py``. The runner requires the names ``deploy``, ``destroy`` and
``final-inventory``. ``tests/test_upgrade_validation.py`` holds this registry
in lockstep with the contract table in ``docs/UPGRADE_VALIDATION.md``.
"""

from __future__ import annotations

from scripts.live_release_validation.actions import (
    action_baseline,
    action_destroy,
    action_final_inventory,
    action_topology,
)
from scripts.live_release_validation.registry import ActionDefinition

from .actions import (
    action_base_deploy,
    action_prepare,
    action_sentinels,
    action_upgrade,
    action_upgrade_preflight,
    action_verify_upgrade,
)


def build_action_registry() -> dict[str, ActionDefinition]:
    """Return actions in dependency-safe execution order."""
    definitions = (
        ActionDefinition(
            "preflight",
            "Verify the release preflight, then pin the base release, tools, and workspace",
            (),
            action_upgrade_preflight,
        ),
        ActionDefinition(
            "baseline",
            "Capture protected CloudFormation and ECR baselines",
            ("preflight",),
            action_baseline,
        ),
        ActionDefinition(
            "prepare",
            "Clone the base release privately, install its gco, and synthesize its app",
            ("preflight",),
            action_prepare,
        ),
        ActionDefinition(
            "deploy",
            "Deploy the base release with its own gco and adopt its stacks by run tag",
            ("baseline", "prepare"),
            action_base_deploy,
        ),
        ActionDefinition(
            "sentinels",
            "Write a job template through the base release's API for the upgrade to keep",
            ("deploy",),
            action_sentinels,
        ),
        ActionDefinition(
            "upgrade",
            "Run the base release's gco upgrade to this commit and adopt the recreated stacks",
            ("sentinels",),
            action_upgrade,
        ),
        ActionDefinition(
            "topology",
            "Verify the upgraded stacks, EKS, HTTPS ALB targets, APIs, queues, and DynamoDB",
            ("upgrade",),
            action_topology,
        ),
        ActionDefinition(
            "verify-upgrade",
            "Verify the upgraded checkout, the stack generations, and the sentinel",
            ("topology",),
            action_verify_upgrade,
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
