"""Explicit EKS context resolution shared by live MCP resources."""

from __future__ import annotations

import re

import boto3

from cli.config import get_config

_REGION_RE = re.compile(r"^[a-z]{2,4}(?:-[a-z0-9]+)+-[0-9]+$")
_PARTITION_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_PROJECT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_ACCOUNT_ID_RE = re.compile(r"^[0-9]{12}$")
_EKS_ARN_RE = re.compile(
    r"^arn:([a-z][a-z0-9-]*):eks:([a-z0-9-]+):([0-9]{12}):"
    r"cluster/([a-z][a-z0-9-]{1,99})$"
)


def is_valid_region(region: str) -> bool:
    """Return whether ``region`` has the bounded AWS region shape GCO accepts."""
    return _REGION_RE.fullmatch(region) is not None


def eks_context_for_region(region: str, project_name: str | None = None) -> str:
    """Return an account-qualified kubectl context ARN for one GCO EKS cluster.

    The cluster prefix follows the same merged CLI configuration as every other
    project-scoped command (cdk.json, config file, then ``GCO_PROJECT_NAME``).
    An explicit ``project_name`` is accepted for deterministic callers/tests.
    """
    if not is_valid_region(region):
        raise ValueError(f"invalid AWS region: {region}")

    project = project_name if project_name is not None else get_config().project_name
    if not isinstance(project, str) or _PROJECT_NAME_RE.fullmatch(project) is None:
        raise ValueError("invalid GCO project name")

    account = str(boto3.client("sts", region_name=region).get_caller_identity().get("Account", ""))
    if _ACCOUNT_ID_RE.fullmatch(account) is None:
        raise ValueError("STS returned an invalid AWS account ID")

    session = boto3.session.Session()
    partition = session.get_partition_for_region(region)
    if not isinstance(partition, str) or _PARTITION_RE.fullmatch(partition) is None:
        raise ValueError(f"AWS SDK returned an invalid partition for region {region}")

    cluster_name = f"{project}-{region}"
    arn = f"arn:{partition}:eks:{region}:{account}:cluster/{cluster_name}"
    match = _EKS_ARN_RE.fullmatch(arn)
    if (
        match is None
        or match.group(1) != partition
        or match.group(2) != region
        or match.group(4) != cluster_name
    ):
        raise ValueError("failed to construct a valid EKS cluster ARN")
    return arn
