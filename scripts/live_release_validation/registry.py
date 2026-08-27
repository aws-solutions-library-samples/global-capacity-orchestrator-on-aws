"""The ordered live-validation action registry.

This is the single source of truth for which actions exist, what each one
promises, the order they run in, and which actions must precede them. The
runner derives selection, dependency expansion, and ``--actions all`` from
this mapping alone, and ``tests/test_live_release_validation_structure.py``
holds it in lockstep with the contract table in
``docs/LIVE_RELEASE_VALIDATION.md``.

Registering a new action is one entry here plus one module under
``actions/``; see ``scripts/live_release_validation/README.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .actions import (
    action_api_lifecycle,
    action_baseline,
    action_central_queue_lifecycle,
    action_convergence,
    action_deploy,
    action_destroy,
    action_final_inventory,
    action_opencost,
    action_policy,
    action_preflight,
    action_schedulers,
    action_sqs_lifecycle,
    action_topology,
)
from .models import RunContext

ActionHandler = Callable[[RunContext], dict[str, Any]]


@dataclass(frozen=True)
class ActionDefinition:
    """One selectable action and its safety dependencies."""

    name: str
    description: str
    dependencies: tuple[str, ...]
    handler: ActionHandler


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
            "baseline",
            "Capture protected CloudFormation and ECR baselines",
            ("preflight",),
            action_baseline,
        ),
        ActionDefinition(
            "deploy",
            "Deploy the configured GCO topology",
            ("baseline",),
            action_deploy,
        ),
        ActionDefinition(
            "topology",
            "Verify stacks, EKS, API endpoints, queues, and DynamoDB",
            ("deploy",),
            action_topology,
        ),
        ActionDefinition(
            "policy",
            "Require all three admission layers to be readable per Region",
            ("topology",),
            action_policy,
        ),
        ActionDefinition(
            "api",
            "Run the authenticated API Job lifecycle",
            ("topology",),
            action_api_lifecycle,
        ),
        ActionDefinition(
            "sqs",
            "Run the direct regional SQS Job lifecycle",
            ("topology",),
            action_sqs_lifecycle,
        ),
        ActionDefinition(
            "central-queue",
            "Run the idempotent DynamoDB-backed queue lifecycle",
            ("topology",),
            action_central_queue_lifecycle,
        ),
        ActionDefinition(
            "schedulers",
            "Prove every enabled batch scheduler against a scheduling-gated workload",
            ("topology",),
            action_schedulers,
        ),
        ActionDefinition(
            "opencost",
            "Require healthy, data-returning OpenCost and a working cost report pipeline",
            ("topology",),
            action_opencost,
        ),
        ActionDefinition(
            "convergence",
            "Require stable SQS/DLQ and DynamoDB convergence",
            ("topology",),
            action_convergence,
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
