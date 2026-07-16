"""Explicit EKS context resolution shared by live MCP resources."""

from __future__ import annotations

import re

import boto3

_REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-[0-9]+$")
_ACCOUNT_ID_RE = re.compile(r"^[0-9]{12}$")
_EKS_ARN_RE = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):eks:([a-z0-9-]+):([0-9]{12}):cluster/(gco-[a-z0-9-]+)$"
)


def is_valid_region(region: str) -> bool:
    """Return whether ``region`` has the bounded AWS region shape GCO accepts."""
    return _REGION_RE.fullmatch(region) is not None


def eks_context_for_region(region: str) -> str:
    """Return an account-qualified kubectl context ARN for a GCO EKS cluster.

    GCO's ``setup-cluster-access`` command uses the EKS cluster ARN as the
    kubeconfig context name. Resolving the current caller's account and AWS
    partition makes that context explicit and avoids both the invalid historical
    ARN (which omitted account ID) and any reliance on kubectl's ambient current
    context.
    """
    if not is_valid_region(region):
        raise ValueError(f"invalid AWS region: {region}")

    account = str(boto3.client("sts", region_name=region).get_caller_identity().get("Account", ""))
    if _ACCOUNT_ID_RE.fullmatch(account) is None:
        raise ValueError("STS returned an invalid AWS account ID")

    partition = boto3.session.Session().get_partition_for_region(region)
    if partition not in {"aws", "aws-us-gov", "aws-cn"}:
        raise ValueError(f"unsupported AWS partition for region {region}")

    cluster_name = f"gco-{region}"
    arn = f"arn:{partition}:eks:{region}:{account}:cluster/{cluster_name}"
    match = _EKS_ARN_RE.fullmatch(arn)
    if match is None or match.group(2) != region or match.group(4) != cluster_name:
        raise ValueError("failed to construct a valid EKS cluster ARN")
    return arn
