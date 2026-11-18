"""
GitHub OIDC Provider CDK Stack.

Creates an IAM OIDC identity provider for GitHub Actions and an IAM role
that GitHub workflows can assume via ``aws-actions/configure-aws-credentials``.

This stack is standalone — it does not depend on or import from the main
GCO CDK stacks. Deploy it independently in any AWS account:

    cd .github/oidc_provider
    cdk deploy GCOGitHubOIDCStack

The IAM policy attached to the role is loaded from ``policy.json`` in this
directory. Edit that file to grant additional permissions for your CI needs.

OIDC Provider:
    An account has at most one IAM OIDC provider per issuer URL, and roles
    of other repositories may trust it. The provider this stack creates is
    retained when the stack is deleted. When the account already has a
    GitHub provider, pass its ARN as ``existing_provider_arn`` (context
    ``github_oidc_provider_arn``) and the stack trusts it without creating
    or ever deleting one.

Trust Policy:
    The role's trust policy restricts assumption to GitHub Actions workflows
    running in a specific repository (and optionally a specific branch).
    Legacy repositories use ``repo:<owner>/<repo>``. Repositories created or
    transferred after July 15, 2026 use the immutable
    ``repo:<owner>@<owner-id>/<repo>@<repo-id>`` prefix. The remainder is:
        :ref:refs/heads/<branch>   (branch push)
        :pull_request              (PR)
        :ref:refs/tags/<tag>       (tag push)

    ``github_branch`` defaults to ``"main"`` and uses ``StringEquals`` for
    that exact branch. ``"*"`` is an explicit opt-in that uses ``StringLike``
    with the configured repository subject prefix plus ``:*``.
"""

import json
import re
from pathlib import Path
from typing import Any

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

GITHUB_OIDC_ISSUER = "token.actions.githubusercontent.com"
GITHUB_OIDC_AUDIENCE = "sts.amazonaws.com"
GITHUB_OIDC_THUMBPRINT = "6938fd4d98bab03faadb97b34396831e3780aea1"
GITHUB_OIDC_BACKUP_THUMBPRINT = "1c58a3a8518e8759bf075b76b750d4f2df264fcd"
_PROVIDER_ARN_RE = re.compile(
    rf"^arn:aws(?:-[a-z]+)*:iam::\d{{12}}:oidc-provider/{re.escape(GITHUB_OIDC_ISSUER)}$"
)
_IMMUTABLE_SUBJECT_PREFIX_RE = re.compile(
    r"^repo:(?P<owner>[^/@:]+)@[1-9]\d*/(?P<repo>[^/@:]+)@[1-9]\d*$"
)


def _validated_subject_prefix(github_repo: str, configured: str | None) -> str:
    """Return a mutable or immutable GitHub repository subject prefix.

    GitHub repositories created or transferred after July 15, 2026 use
    ``repo:OWNER@OWNER_ID/REPO@REPO_ID``. The explicit prefix is validated
    against ``github_repo`` so a copied ID pair cannot silently trust a
    different named repository.
    """
    try:
        owner, repository = github_repo.split("/", 1)
    except ValueError as exc:
        raise ValueError("github_repo must use owner/repo format") from exc
    if not owner or not repository or "/" in repository:
        raise ValueError("github_repo must use owner/repo format")

    mutable_prefix = f"repo:{github_repo}"
    if configured is None:
        return mutable_prefix
    if not isinstance(configured, str):
        raise ValueError("github_subject_prefix must be a string")
    if configured != configured.strip() or not configured:
        raise ValueError("github_subject_prefix must be a non-empty trimmed string")
    if configured == mutable_prefix:
        return configured

    match = _IMMUTABLE_SUBJECT_PREFIX_RE.fullmatch(configured)
    if match is None:
        raise ValueError(
            "github_subject_prefix must be repo:owner/repo or repo:owner@OWNER_ID/repo@REPO_ID"
        )
    if (match.group("owner"), match.group("repo")) != (owner, repository):
        raise ValueError("github_subject_prefix names must match github_repo")
    return configured


def _validated_provider_arn(configured: str) -> str:
    """Return an existing GitHub OIDC provider ARN, or raise ``ValueError``."""
    if not isinstance(configured, str) or _PROVIDER_ARN_RE.fullmatch(configured) is None:
        raise ValueError(
            "github_oidc_provider_arn must be the ARN of the account's "
            f"{GITHUB_OIDC_ISSUER} OIDC provider "
            f"(arn:aws:iam::123456789012:oidc-provider/{GITHUB_OIDC_ISSUER})"
        )
    return configured


class GCOGitHubOIDCStack(Stack):
    """Standalone stack that creates a GitHub OIDC provider and CI role.

    Parameters:
        github_repo: GitHub repository in ``owner/repo`` format.
            Default: ``aws-solutions-library-samples/global-capacity-orchestrator-on-aws``.
        github_subject_prefix: Exact repository prefix GitHub reports for the
            OIDC ``sub`` claim. Omit for the legacy ``repo:owner/repo`` format;
            immutable repositories use ``repo:owner@OWNER_ID/repo@REPO_ID``.
        github_branch: Exact branch restriction. Defaults to ``"main"``.
            Set this to the repository's actual default branch when it differs;
            use ``"*"`` only as an explicit opt-in to any branch or tag.
        existing_provider_arn: ARN of a GitHub OIDC provider that already
            exists in the account. When set, the stack trusts that provider
            and creates none; otherwise it creates one and retains it on
            delete.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        github_repo: str = "aws-solutions-library-samples/global-capacity-orchestrator-on-aws",
        github_subject_prefix: str | None = None,
        github_branch: str = "main",
        existing_provider_arn: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------------------
        # OIDC Provider
        # ---------------------------------------------------------------------
        # One provider per issuer URL per account, possibly trusted by other
        # repositories' roles: import an existing one, or create one that a
        # stack delete leaves in place. Keep the construct id: the provider a
        # stack created before it was retained is deleted if this resource is
        # removed or replaced.
        provider: iam.IOpenIdConnectProvider
        if existing_provider_arn is not None:
            provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
                self,
                "GitHubOIDCProvider",
                _validated_provider_arn(existing_provider_arn),
            )
        else:
            provider = iam.OpenIdConnectProvider(
                self,
                "GitHubOIDCProvider",
                url=f"https://{GITHUB_OIDC_ISSUER}",
                client_ids=[GITHUB_OIDC_AUDIENCE],
                thumbprints=[GITHUB_OIDC_THUMBPRINT, GITHUB_OIDC_BACKUP_THUMBPRINT],
                removal_policy=RemovalPolicy.RETAIN,
            )

        # ---------------------------------------------------------------------
        # Trust policy — restrict to the specified GitHub repo (and branch)
        # ---------------------------------------------------------------------
        subject_prefix = _validated_subject_prefix(github_repo, github_subject_prefix)
        if github_branch == "*":
            subject_claim = f"{subject_prefix}:*"
            condition = {"StringLike": {"token.actions.githubusercontent.com:sub": subject_claim}}
        else:
            subject_claim = f"{subject_prefix}:ref:refs/heads/{github_branch}"
            condition = {"StringEquals": {"token.actions.githubusercontent.com:sub": subject_claim}}

        # Also require the audience claim to match
        condition.setdefault("StringEquals", {})
        condition["StringEquals"]["token.actions.githubusercontent.com:aud"] = GITHUB_OIDC_AUDIENCE

        principal = iam.OpenIdConnectPrincipal(provider, conditions=condition)

        # ---------------------------------------------------------------------
        # IAM Role
        # ---------------------------------------------------------------------
        role = iam.Role(
            self,
            "GitHubActionsRole",
            assumed_by=principal,
            role_name=f"gco-github-actions-{self.region}",
            description=(
                f"GitHub Actions OIDC role for {github_repo}. "
                "Assumed by CI workflows via aws-actions/configure-aws-credentials."
            ),
            max_session_duration=None,  # default 1 hour
        )

        # ---------------------------------------------------------------------
        # IAM Policy (loaded from policy.json)
        # ---------------------------------------------------------------------
        policy_path = Path(__file__).parent / "policy.json"
        policy_doc = json.loads(policy_path.read_text())

        role.attach_inline_policy(
            iam.Policy(
                self,
                "CIPolicy",
                document=iam.PolicyDocument.from_json(policy_doc),
            )
        )

        # ---------------------------------------------------------------------
        # Outputs
        # ---------------------------------------------------------------------
        CfnOutput(
            self,
            "RoleArn",
            value=role.role_arn,
            description="IAM role ARN for GitHub Actions. Add as GCO_CI_ROLE_ARN secret.",
        )
        CfnOutput(
            self,
            "OIDCProviderArn",
            value=provider.open_id_connect_provider_arn,
            description="OIDC provider ARN.",
        )
