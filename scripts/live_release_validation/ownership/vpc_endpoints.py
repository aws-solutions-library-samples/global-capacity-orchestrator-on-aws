"""Accepted-residue accounting for VPC endpoints EC2 has already deleted.

The Resource Groups Tagging API is an index, not the source of truth, and it
lags resource deletion by minutes. Gateway VPC endpoints (the S3 and DynamoDB
endpoints the regional stack creates) are deleted synchronously with their
stack, yet the index kept returning both ARNs — still carrying their
``aws:cloudformation:stack-id`` tags — when ``final-inventory`` ran seconds
after the stack was gone. Observed live on 2026-09-12: two
``vpc-endpoint/vpce-…`` ARNs failed the all-zero teardown gate while
``DescribeVpcEndpoints`` already answered ``InvalidVpcEndpointId.NotFound`` for
both. The same stale entries would fail the next run's clean-account gate.

``_strip_deleted_vpc_endpoints`` accepts exactly that shape and nothing else: a
``tagged_resources`` entry whose ARN parses as a VPC endpoint in the same
region and expected account is stripped **only after** EC2 itself confirms the
endpoint does not exist or is in a terminal ``deleting``/``deleted`` state. A
live endpoint keeps its entry as genuine residue. Every acceptance is returned
as evidence (region, endpoint id, the EC2 check performed, tags) so ``baseline``
and ``final-inventory`` disclose what they tolerated rather than silently
ignoring it — the same posture as ``ownership/dynamodb_streams.py``.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from botocore.exceptions import ClientError

from ..models import RunContext

_VPC_ENDPOINT_ARN = re.compile(
    r"^arn:[^:]+:ec2:(?P<region>[a-z0-9-]+):(?P<account>\d{12})"
    r":vpc-endpoint/(?P<endpoint_id>vpce-[0-9a-f]+)$"
)
#: EC2 states in which the endpoint is already being removed and cannot be
#: residue; anything else (``available``, ``pending``, ``failed``, ...) is.
_TERMINAL_ENDPOINT_STATES = frozenset({"deleting", "deleted"})
_NOT_FOUND_CODES = frozenset({"InvalidVpcEndpointId.NotFound", "InvalidVpcEndpoint.NotFound"})


def _endpoint_state(ctx: RunContext, region: str, endpoint_id: str) -> str:
    """Return the live endpoint state, or ``ABSENT`` once EC2 no longer knows it."""
    ec2 = ctx.session.client("ec2", region_name=region)
    try:
        response = ec2.describe_vpc_endpoints(VpcEndpointIds=[endpoint_id])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in _NOT_FOUND_CODES:
            raise
        return "ABSENT"
    endpoints = response.get("VpcEndpoints") or []
    if not endpoints:
        return "ABSENT"
    return str(endpoints[0].get("State") or "UNKNOWN")


def _strip_deleted_vpc_endpoints(
    ctx: RunContext,
    project_inventory: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Strip tagged VPC endpoint ARNs whose endpoint EC2 proves gone."""
    inventory = copy.deepcopy(project_inventory)
    accepted: list[dict[str, Any]] = []
    for region, resources in list(inventory.get("regional", {}).items()):
        kept: list[dict[str, Any]] = []
        for entry in resources.get("tagged_resources", []):
            arn = str(entry.get("arn") or "")
            match = _VPC_ENDPOINT_ARN.match(arn)
            if (
                match is None
                or match.group("region") != region
                or match.group("account") != ctx.settings.expected_account
            ):
                kept.append(entry)
                continue
            endpoint_id = match.group("endpoint_id")
            state = _endpoint_state(ctx, region, endpoint_id)
            if state != "ABSENT" and state not in _TERMINAL_ENDPOINT_STATES:
                # EC2 still has it: genuine residue, keep it visible.
                kept.append(entry)
                continue
            accepted.append(
                {
                    "region": region,
                    "arn": arn,
                    "endpoint_id": endpoint_id,
                    "endpoint_state": state,
                    "authority": "ec2:DescribeVpcEndpoints",
                    "tags": dict(entry.get("tags") or {}),
                    "note": (
                        "the Resource Groups Tagging API index lags endpoint deletion; "
                        "EC2 is the authority for existence"
                    ),
                }
            )
        resources["tagged_resources"] = kept
        if not any(resources.values()):
            inventory["regional"].pop(region)
    return inventory, accepted
