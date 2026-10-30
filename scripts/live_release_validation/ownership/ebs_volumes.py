"""Accept Tagging-API records of EBS volumes EC2 proves gone.

The launch templates of a managed AWS PCS cluster tag every node's root
volume with the project's ownership tags (``gco:project``,
``gco:pcs-node-group``), so the Resource Groups Tagging API lists those
volumes under the project. EBS deletes a ``DeleteOnTermination`` volume
after its instance reaches ``terminated``, and the Tagging API index lags
that deletion: the first full run on the PCS branch ended with exactly one
residual resource, the login node's root volume, which
``ec2:DescribeVolumes`` already answered ``InvalidVolume.NotFound`` for, and
the next run's baseline saw the same ghost ninety minutes later. The volume
``cleanup/pcs_nodes.py`` waits for is the live one; this strip is for the
index entry that outlives it.

Acceptance is conditional, the way the deleted-VPC-endpoint strip is: a
volume EC2 still describes in a non-terminal state is genuine residue and
keeps its entry.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from botocore.exceptions import ClientError

from ..models import RunContext

_VOLUME_ARN = re.compile(
    r"^arn:[^:]+:ec2:(?P<region>[a-z0-9-]+):(?P<account>\d{12}):volume/(?P<volume_id>vol-[0-9a-f]+)$"
)
#: EBS states in which the volume is already being removed and cannot be
#: residue; anything else (``available``, ``in-use``, ``creating``, ``error``) is.
_TERMINAL_VOLUME_STATES = frozenset({"deleting", "deleted"})
_NOT_FOUND_CODES = frozenset({"InvalidVolume.NotFound"})


def _volume_state(ctx: RunContext, region: str, volume_id: str) -> str:
    """Return the live volume state, or ``ABSENT`` once EC2 no longer knows it."""
    ec2 = ctx.session.client("ec2", region_name=region)
    try:
        response = ec2.describe_volumes(VolumeIds=[volume_id])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in _NOT_FOUND_CODES:
            raise
        return "ABSENT"
    volumes = response.get("Volumes") or []
    if not volumes:
        return "ABSENT"
    return str(volumes[0].get("State") or "UNKNOWN")


def _strip_deleted_volumes(
    ctx: RunContext,
    project_inventory: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Strip tagged EBS volume ARNs whose volume EC2 proves gone."""
    inventory = copy.deepcopy(project_inventory)
    accepted: list[dict[str, Any]] = []
    for region, resources in list(inventory.get("regional", {}).items()):
        kept: list[dict[str, Any]] = []
        for entry in resources.get("tagged_resources", []):
            arn = str(entry.get("arn") or "")
            match = _VOLUME_ARN.match(arn)
            if (
                match is None
                or match.group("region") != region
                or match.group("account") != ctx.settings.expected_account
            ):
                kept.append(entry)
                continue
            volume_id = match.group("volume_id")
            state = _volume_state(ctx, region, volume_id)
            if state != "ABSENT" and state not in _TERMINAL_VOLUME_STATES:
                # EC2 still has it: genuine residue, keep it visible.
                kept.append(entry)
                continue
            accepted.append(
                {
                    "region": region,
                    "arn": arn,
                    "volume_id": volume_id,
                    "volume_state": state,
                    "authority": "ec2:DescribeVolumes",
                    "tags": dict(entry.get("tags") or {}),
                    "note": (
                        "the Resource Groups Tagging API index lags volume deletion; "
                        "EC2 is the authority for existence"
                    ),
                }
            )
        resources["tagged_resources"] = kept
        if not any(resources.values()):
            inventory["regional"].pop(region)
    return inventory, accepted
