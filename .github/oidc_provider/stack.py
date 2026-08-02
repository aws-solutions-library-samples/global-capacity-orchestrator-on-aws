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

Trust Policy:
    The role's trust policy restricts assumption to GitHub Actions workflows
    running in a specific repository (and optionally a specific branch).
    The OIDC subject claim format is:
        repo:<owner>/<repo>:ref:refs/heads/<branch>   (branch push)
        repo:<owner>/<repo>:pull_request               (PR)
        repo:<owner>/<repo>:ref:refs/tags/<tag>        (tag push)

    ``github_branch`` defaults to ``"main"`` and uses ``StringEquals`` for
    that exact branch. ``"*"`` is an explicit opt-in that uses ``StringLike``
    with ``repo:<owner>/<repo>:*`` to allow any ref.
"""

import json
from pathlib import Path

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

GITHUB_OIDC_ISSUER = "token.actions.githubusercontent.com"
GITHUB_OIDC_AUDIENCE = "sts.amazonaws.com"
GITHUB_OIDC_THUMBPRINT = "6938fd4d98bab03faadb97b34396831e3780aea1"
GITHUB_OIDC_BACKUP_THUMBPRINT = "1c58a3a8518e8759bf075b76b750d4f2df264fcd"


class GCOGitHubOIDCStack(Stack):
    """Standalone stack that creates a GitHub OIDC provider and CI role.

    Parameters:
        github_repo: GitHub repository in ``owner/repo`` format.
            Default: ``awslabs/global-capacity-orchestrator-on-aws``.
        github_branch: Exact branch restriction. Defaults to ``"main"``.
            Set this to the repository's actual default branch when it differs;
            use ``"*"`` only as an explicit opt-in to any branch or tag.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        github_repo: str = "awslabs/global-capacity-orchestrator-on-aws",
        github_branch: str = "main",
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------------------
        # OIDC Provider
        # ---------------------------------------------------------------------
        provider = iam.OpenIdConnectProvider(
            self,
            "GitHubOIDCProvider",
            url=f"https://{GITHUB_OIDC_ISSUER}",
            client_ids=[GITHUB_OIDC_AUDIENCE],
            thumbprints=[GITHUB_OIDC_THUMBPRINT, GITHUB_OIDC_BACKUP_THUMBPRINT],
        )

        # ---------------------------------------------------------------------
        # Trust policy — restrict to the specified GitHub repo (and branch)
        # ---------------------------------------------------------------------
        if github_branch == "*":
            subject_claim = f"repo:{github_repo}:*"
            condition = {"StringLike": {"token.actions.githubusercontent.com:sub": subject_claim}}
        else:
            subject_claim = f"repo:{github_repo}:ref:refs/heads/{github_branch}"
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
