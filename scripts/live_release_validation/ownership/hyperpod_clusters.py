"""Accept Tagging-API records of SageMaker HyperPod clusters the service proves gone.

The regional stack tags its ``AWS::SageMaker::Cluster`` with the project's
ownership tags, so the Resource Groups Tagging API lists the cluster under
the project. Deleting a HyperPod cluster takes the service the better part of
an hour (it terminates the instances and frees their network interfaces
first), and the Tagging API index lags that deletion the way it lags a VPC
endpoint's or a volume's: the fourth #502 release attempt's teardown ended
with ``sagemaker:…:cluster/<id>`` still listed — stack tags intact — while
``DescribeCluster`` already answered ``ResourceNotFound`` and the ``hyperpod``
scanner, which asks the service, listed nothing. The same stale entry would
fail the next run's clean-account gate.

Acceptance is conditional, the way the deleted-VPC-endpoint and volume strips
are, and narrower: only a cluster the service no longer knows is stripped. A
cluster still ``Deleting`` is residue in flight — the one HyperPod deletion
this harness has watched stalled for forty minutes on a permissions gap — and
keeps its entry, as does any other live status.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from botocore.exceptions import ClientError

from ..models import RunContext

_HYPERPOD_CLUSTER_ARN = re.compile(
    r"^arn:[^:]+:sagemaker:(?P<region>[a-z0-9-]+):(?P<account>\d{12})"
    r":cluster/(?P<cluster_id>[a-z0-9]+)$"
)
_NOT_FOUND_CODES = frozenset({"ResourceNotFound", "ResourceNotFoundException"})


def _cluster_status(ctx: RunContext, region: str, cluster_arn: str) -> str:
    """Return the live ``ClusterStatus``, or ``ABSENT`` once SageMaker no longer knows it."""
    sagemaker = ctx.session.client("sagemaker", region_name=region)
    try:
        response = sagemaker.describe_cluster(ClusterName=cluster_arn)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in _NOT_FOUND_CODES:
            raise
        return "ABSENT"
    return str(response.get("ClusterStatus") or "UNKNOWN")


def _strip_deleted_hyperpod_clusters(
    ctx: RunContext,
    project_inventory: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Strip tagged HyperPod cluster ARNs whose cluster SageMaker proves gone."""
    inventory = copy.deepcopy(project_inventory)
    accepted: list[dict[str, Any]] = []
    for region, resources in list(inventory.get("regional", {}).items()):
        kept: list[dict[str, Any]] = []
        for entry in resources.get("tagged_resources", []):
            arn = str(entry.get("arn") or "")
            match = _HYPERPOD_CLUSTER_ARN.match(arn)
            if (
                match is None
                or match.group("region") != region
                or match.group("account") != ctx.settings.expected_account
            ):
                kept.append(entry)
                continue
            status = _cluster_status(ctx, region, arn)
            if status != "ABSENT":
                # SageMaker still has it, deleting or not: genuine residue.
                kept.append(entry)
                continue
            accepted.append(
                {
                    "region": region,
                    "arn": arn,
                    "cluster_id": match.group("cluster_id"),
                    "cluster_status": status,
                    "authority": "sagemaker:DescribeCluster",
                    "tags": dict(entry.get("tags") or {}),
                    "note": (
                        "the Resource Groups Tagging API index lags cluster deletion; "
                        "SageMaker is the authority for existence"
                    ),
                }
            )
        resources["tagged_resources"] = kept
        if not any(resources.values()):
            inventory["regional"].pop(region)
    return inventory, accepted
