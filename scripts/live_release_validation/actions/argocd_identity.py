"""argocd-identity: provision the Identity Center inputs the Argo CD capability needs."""

from __future__ import annotations

from typing import Any

from gco.eks_capabilities_config import parse_eks_capabilities_overrides

from ..checks.eks_capabilities import (
    apply_effective_cdk_context,
    bootstrap_validation_identity,
    identity_state,
    needs_identity_bootstrap,
)
from ..models import RunContext


def action_argocd_identity(ctx: RunContext) -> dict[str, Any]:
    """Make the run self-contained on the identity side, before ``deploy`` synthesizes.

    The hosted Argo CD signs users in only through IAM Identity Center, so a
    deploy that enables it needs an instance ARN and at least one mapped
    identity — inputs a validation account has no reason to hold in advance.
    When the run requested Argo CD (``--eks-capabilities argocd``) without
    supplying them, this action discovers the instance visible from the
    account (any Identity Center Region), creates an *account instance* named
    ``<project>-live-validation`` when there is none, ensures the group
    ``<project>-live-validation-argocd`` and maps it to ``ADMIN``, records all
    of it in the checkpoint, and re-registers the CDK context so every later
    synthesis (deploy, destroy) carries the complete Argo CD block. Teardown
    deletes the group and, when this run created it (or it carries the
    harness's name), the instance.

    Passes with a note when Argo CD is not requested or the operator supplied
    the inputs. Idempotent under ``--resume``: the stored record is reused.
    """
    overrides = parse_eks_capabilities_overrides(
        getattr(ctx.settings, "eks_capabilities_overrides_json", "") or None
    )
    if not needs_identity_bootstrap(overrides):
        detail = (
            "The argocd capability is not requested for this run"
            if overrides.get("argocd", {}).get("enabled") is not True
            else "Identity Center inputs were supplied on the command line"
        )
        return {"bootstrapped": False, "detail": detail}

    already = identity_state(ctx.checkpoint) is not None
    record = bootstrap_validation_identity(ctx)
    context = apply_effective_cdk_context(ctx)
    return {
        "bootstrapped": True,
        "resumed": already,
        "instance_arn": record["instance_arn"],
        "idc_region": record["idc_region"],
        "instance_created": record["instance_created"],
        "instance_harness_owned": record["instance_harness_owned"],
        "group_id": record["group_id"],
        "group_name": record["group_name"],
        "group_created": record["group_created"],
        "cdk_context_keys": sorted(context),
    }
