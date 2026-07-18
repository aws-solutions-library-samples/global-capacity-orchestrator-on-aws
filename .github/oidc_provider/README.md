# GitHub OIDC Provider for CI/CD

A standalone CDK stack that creates an IAM OIDC identity provider and role for GitHub Actions. This lets CI workflows authenticate to AWS without long-lived access keys.

## Table of Contents

- [Overview](#overview)
- [What Gets Created](#what-gets-created)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Customizing the IAM Policy](#customizing-the-iam-policy)
- [Using with a Fork or Different Repo](#using-with-a-fork-or-different-repo)
- [Using the Role in Workflows](#using-the-role-in-workflows)
- [Files](#files)
- [Testing](#testing)
- [Tearing Down](#tearing-down)

## Overview

GitHub Actions can assume an AWS IAM role via OpenID Connect (OIDC) instead of storing AWS access keys as repository secrets. This is more secure because:

- No long-lived credentials to rotate or leak
- Permissions are scoped to the specific repository and branch
- AWS CloudTrail logs show the GitHub repo and workflow that assumed the role

This stack is **standalone** — it does not depend on or affect the main GCO infrastructure stacks. You can deploy it independently in any AWS account.

## What Gets Created

1. **IAM OIDC Identity Provider** — trusts `token.actions.githubusercontent.com` (the GitHub OIDC issuer). Skipped if one already exists in the account.
2. **IAM Role** — assumable only by GitHub Actions workflows from your repository. The trust policy restricts access to a specific GitHub org/repo.
3. **IAM Policy** — attached to the role. By default grants read-only permissions needed for the dependency scan workflow (`eks:DescribeAddonVersions`, `eks:DescribeClusterVersions`, `elasticmapreduce:ListReleaseLabels`, `rds:DescribeDBEngineVersions`). You can expand this for your own needs.

## Prerequisites

- Python 3.14+
- AWS CDK CLI available as `cdk` on `PATH`
- AWS credentials configured for the target account
- CDK bootstrapped in the target region: `cdk bootstrap aws://ACCOUNT_ID/REGION`

## Quick Start

```bash
# From the repository root
cd .github/oidc_provider

# Install dependencies
pip install -e "../../[cdk]"

# Deploy (uses your default AWS credentials and region)
cdk deploy GCOGitHubOIDCStack

# Note the role ARN from the stack output
# Add it as a GitHub Actions secret: GCO_CI_ROLE_ARN
```

## Customizing the IAM Policy

The default policy in `policy.json` grants minimal read-only permissions for the dependency scan workflow:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "eks:DescribeAddonVersions",
        "eks:DescribeClusterVersions",
        "elasticmapreduce:ListReleaseLabels",
        "rds:DescribeDBEngineVersions",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    }
  ]
}
```

To add permissions (for example, to allow CI to deploy stacks or run integration tests against live infrastructure):

1. Edit `policy.json` to add the IAM actions you need
2. Redeploy from `.github/oidc_provider`: `cdk deploy GCOGitHubOIDCStack` from an environment with AWS credentials for the target account

Common additions:

| Use Case | Actions to Add |
|----------|---------------|
| CDK deploy from CI | `cloudformation:*`, `iam:*`, `eks:*`, `ec2:*`, `s3:*`, `lambda:*`, `logs:*`, `sqs:*`, `dynamodb:*`, `elasticloadbalancing:*`, `globalaccelerator:*`, `secretsmanager:*`, `ssm:*`, `ecr:*`, `efs:*` |
| ECR image push | `ecr:GetAuthorizationToken`, `ecr:BatchCheckLayerAvailability`, `ecr:PutImage`, `ecr:InitiateLayerUpload`, `ecr:UploadLayerPart`, `ecr:CompleteLayerUpload` |
| S3 artifact upload | `s3:PutObject`, `s3:GetObject`, `s3:ListBucket` |

## Using with a Fork or Different Repo

The trust policy restricts which GitHub repository can assume the role. By default it's set to `awslabs/global-capacity-orchestrator-on-aws`.

To use with your fork, edit `cdk.json`:

```json
{
  "context": {
    "github_repo": "your-org/your-repo",
    "github_branch": "*"
  }
}
```

Then redeploy from `.github/oidc_provider`: `cdk deploy GCOGitHubOIDCStack`

Setting `github_branch` to `"main"` restricts the role so only the main branch can assume it. `"*"` (the default) allows any branch or tag.

## Using the Role in Workflows

After deploying, add the role ARN as a GitHub Actions secret (`GCO_CI_ROLE_ARN`), then use it in your workflow:

```yaml
permissions:
  id-token: write   # Required for OIDC
  contents: read

steps:
  - uses: aws-actions/configure-aws-credentials@v4
    with:
      role-to-assume: ${{ secrets.GCO_CI_ROLE_ARN }}
      aws-region: us-east-1
```

## Files

| File | Description |
|------|-------------|
| `app.py` | CDK app entry point — instantiates the OIDC stack |
| `stack.py` | CDK stack definition — OIDC provider, IAM role, IAM policy |
| `policy.json` | IAM policy document attached to the role (edit this to change permissions) |
| `cdk.json` | CDK configuration — edit `github_repo` and `github_branch` here |
| `README.md` | This file |

## Testing

From the repository root:

```bash
python -m pytest tests/test_oidc_stack.py -v
```

The tests verify:

- Stack synthesizes without errors
- OIDC provider is created with the correct issuer URL
- IAM role trust policy restricts to the correct GitHub repo
- IAM policy contains the expected actions
- Branch restriction works when specified
- Custom repo names are reflected in the trust policy

## Tearing Down

```bash
cd .github/oidc_provider
cdk destroy GCOGitHubOIDCStack
```

This removes the IAM role and policy. The OIDC provider is retained if other roles in the account depend on it.
