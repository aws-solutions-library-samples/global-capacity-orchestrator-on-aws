"""eks-capabilities: the opt-in AWS-managed Argo CD / ACK / kro are attached and working."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..checks.cluster import cluster_kubectl
from ..checks.eks_capabilities import (
    GITOPS_FIXTURE_PATH,
    effective_eks_capabilities_config,
    enabled_types_by_region,
    gitops_expectations,
    push_gitops_fixture,
    verify_argocd_cluster_access,
    verify_capabilities_attached,
    verify_gitops_fixture,
    wait_for_gitops_sync,
)
from ..models import RunContext


def action_eks_capabilities(ctx: RunContext) -> dict[str, Any]:
    """Require every configured EKS Capability to be attached, ACTIVE, and — for Argo CD — wired and syncing.

    Resolves the block the run deployed with (cdk.json ``eks_capabilities``
    merged with the run's ``--eks-capabilities`` inputs). When no type is
    enabled for any deployed Region the action passes with a note: the shipped
    default is every capability off, and that default is what most runs
    validate. Otherwise, for every Region with an enabled type:

    * the EKS API must report each enabled capability attached to the Region's
      cluster and ``ACTIVE`` with no drift from the configuration, judged by
      the same merge ``gco stacks capabilities status`` prints;
    * when Argo CD is enabled, EKS must publish its server URL, and through the
      tunnelled kubectl session the ``local-cluster`` Secret must register the
      cluster by ARN and the RBAC must bind the capability role's access-entry
      group;
    * when the GitOps hand-off is enabled, the fenced ``AppProject`` must allow
      the run's repository, the root ``Application`` must reach ``Synced`` /
      ``Healthy`` at the expected commit within a bounded wait, and the fixture
      ConfigMap under ``examples/gitops/tenant-smoke`` must exist in
      ``gco-jobs`` carrying Argo CD's tracking label — the hosted Argo CD
      really pulled the repository and wrote into the tenant namespace. With
      the default ``source: codecommit`` the action first pushes the fixture
      directory into the GCO-managed repository the deploy created (the same
      code path as ``gco stacks capabilities gitops push``) and expects the
      resulting commit; with ``source: git`` it expects the run's
      ``--argocd-gitops-revision`` in the operator repository.

    The Argo CD server URL is recorded in the evidence so the operator can
    open it (``gco stacks capabilities argocd open``) or capture the docs
    screenshot (``gco stacks capabilities argocd screenshot``) while the
    cluster is still up.
    """
    config = effective_eks_capabilities_config(ctx)
    by_region = enabled_types_by_region(config, ctx.deployment_regions)
    if not by_region:
        evidence: dict[str, Any] = {
            "enabled": False,
            "detail": (
                "No EKS capability is enabled for any deployed Region (the shipped default); "
                "pass --eks-capabilities with the Argo CD Identity Center inputs to prove them"
            ),
        }
        ctx.checkpoint.state["eks_capabilities_validation"] = evidence
        ctx.persist()
        return evidence

    poll_interval = max(10.0, float(ctx.settings.poll_interval_seconds))
    command_timeout = float(getattr(ctx.settings, "command_timeout_seconds", 300))
    regions: dict[str, Any] = {}
    for region, enabled_types in by_region.items():
        record: dict[str, Any] = {"enabled_types": enabled_types}
        regions[region] = record
        ctx.checkpoint.state["eks_capabilities_validation"] = {"enabled": True, "regions": regions}
        ctx.persist()

        status = verify_capabilities_attached(ctx, region, config)
        record["capabilities"] = [
            {
                key: row.get(key)
                for key in ("type", "capability_name", "status", "version", "arn", "role_arn")
            }
            for row in status["capabilities"]
            if row["configured"]
        ]
        argo = status["capabilities"][0]
        if "argocd" not in enabled_types:
            ctx.persist()
            continue

        record["argocd_server_url"] = argo["argocd_server_url"]
        expectations = gitops_expectations(config, region, project_name=ctx.config.project_name)
        expected_revision = expectations["revision"] if expectations else None
        if expectations is not None and expectations["source"] == "codecommit":
            # Seed the GCO-managed repository with the fixture; the synced
            # revision must then be exactly the commit this push produced.
            fixture_dir = Path(ctx.settings.repo_root) / getattr(
                ctx.settings, "argocd_gitops_fixture_path", GITOPS_FIXTURE_PATH
            )
            pushed = push_gitops_fixture(ctx, region, config, fixture_dir=fixture_dir)
            record["gitops_push"] = pushed.to_dict()
            ctx.persist()
            expected_revision = pushed.head_commit_id
        with cluster_kubectl(ctx, region) as kubectl:
            record["cluster_access"] = verify_argocd_cluster_access(
                kubectl,
                record,
                cluster_arn=status["cluster_arn"],
                role_arn=str(argo["role_arn"]),
                timeout=command_timeout,
            )
            if expectations is None or expected_revision is None:
                record["gitops"] = {"enabled": False}
            else:
                synced = wait_for_gitops_sync(
                    kubectl,
                    record,
                    expected_revision=expected_revision,
                    expected_repo_url=expectations["repo_url"],
                    poll_interval=poll_interval,
                    timeout=command_timeout,
                )
                fixture = verify_gitops_fixture(kubectl, record, timeout=command_timeout)
                record["gitops"] = {
                    "enabled": True,
                    "source": expectations["source"],
                    "repo_url": expectations["repo_url"],
                    "revision": expected_revision,
                    "path": expectations["path"],
                    "application": synced,
                    "fixture": fixture,
                }
        ctx.persist()

    evidence = {"enabled": True, "regions": regions}
    ctx.checkpoint.state["eks_capabilities_validation"] = evidence
    ctx.persist()
    return evidence
