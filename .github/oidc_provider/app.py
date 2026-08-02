#!/usr/bin/env python3
"""
CDK app entry point for the GitHub OIDC provider stack.

This is a standalone CDK app — it does not import or depend on the main
GCO stacks. Deploy it independently:

    cd .github/oidc_provider
    cdk deploy GCOGitHubOIDCStack

Configuration is read from ``cdk.json`` context values:

    github_repo    — GitHub repository in owner/repo format
                     (default: awslabs/global-capacity-orchestrator-on-aws)
    github_branch  — Branch restriction; "main" = main only (default),
                     "*" = explicit opt-in to any branch/tag
"""

import aws_cdk as cdk
from stack import GCOGitHubOIDCStack

app = cdk.App()

# Read configuration from cdk.json context — users edit cdk.json, not this file.
github_repo = (
    app.node.try_get_context("github_repo") or "awslabs/global-capacity-orchestrator-on-aws"
)
github_branch = app.node.try_get_context("github_branch") or "main"

GCOGitHubOIDCStack(
    app,
    "GCOGitHubOIDCStack",
    github_repo=github_repo,
    github_branch=github_branch,
    description="GitHub Actions OIDC provider and CI role for GCO",
)

app.synth()
