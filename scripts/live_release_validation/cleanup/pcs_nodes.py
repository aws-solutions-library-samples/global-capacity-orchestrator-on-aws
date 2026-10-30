"""Wait for the EC2 instances AWS PCS launched to finish terminating after teardown.

CloudFormation deletes a managed PCS cluster's compute node groups before the
cluster, and PCS terminates each group's instances as part of that; the
stack is gone the moment PCS reports the group deleted, while EC2 still
shows the instances as ``shutting-down`` for a minute or two. The final
inventory counts ``shutting-down`` instances as live (an instance that is
still releasing its ENIs is not gone), so without this wait a clean
teardown could read as residue. The instances are found by the ownership
tag the launch templates stamp (``gco:project``) together with the tag PCS
itself puts on every node (``aws:pcs:compute-node-group-id``).

The nodes' root volumes lag further: the launch templates tag them like
the instances (``gco:project``, ``gco:pcs-node-group``), EBS deletes a
``DeleteOnTermination`` volume only after its instance is ``terminated``,
and the tagging API still lists the volume while it is ``deleting``. The
first full run on this harness ended with exactly one residual resource,
the login node's root volume, gone a minute later. So the wait covers the
volumes too; anything still present at the deadline is reported, and the
final inventory then decides.
"""

from __future__ import annotations

import time
from typing import Any

from gco.pcs_config import PCS_NODE_GROUP_INSTANCE_TAG

from ..models import RunContext, utc_now

#: How long PCS-launched instances may take to leave ``shutting-down``.
PCS_NODE_TERMINATION_WAIT_SECONDS = 600
PCS_NODE_TERMINATION_POLL_SECONDS = 15
_LIVE_STATES = ("pending", "running", "stopping", "stopped", "shutting-down")
#: Every volume status short of gone (``deleted`` volumes are not listed at all).
_VOLUME_STATES = ("creating", "available", "in-use", "deleting", "error")
#: The tag GCO's launch templates stamp on the nodes' volumes (``gco/stacks/pcs_cluster.py``).
PCS_NODE_GROUP_VOLUME_TAG = "gco:pcs-node-group"


def _pcs_instances(session: Any, region: str, project_name: str) -> list[dict[str, str]]:
    client = session.client("ec2", region_name=region)
    found: list[dict[str, str]] = []
    paginator = client.get_paginator("describe_instances")
    for page in paginator.paginate(
        Filters=[
            {"Name": "tag:gco:project", "Values": [project_name]},
            {"Name": "tag-key", "Values": [PCS_NODE_GROUP_INSTANCE_TAG]},
            {"Name": "instance-state-name", "Values": list(_LIVE_STATES)},
        ]
    ):
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = str(instance.get("InstanceId") or "")
                if not instance_id:
                    raise RuntimeError(f"EC2 returned an instance without an ID in {region}")
                found.append(
                    {
                        "instance_id": instance_id,
                        "state": str((instance.get("State") or {}).get("Name") or ""),
                    }
                )
    return sorted(found, key=lambda item: item["instance_id"])


def _pcs_volumes(session: Any, region: str, project_name: str) -> list[dict[str, str]]:
    client = session.client("ec2", region_name=region)
    found: list[dict[str, str]] = []
    paginator = client.get_paginator("describe_volumes")
    for page in paginator.paginate(
        Filters=[
            {"Name": "tag:gco:project", "Values": [project_name]},
            {"Name": "tag-key", "Values": [PCS_NODE_GROUP_VOLUME_TAG]},
            {"Name": "status", "Values": list(_VOLUME_STATES)},
        ]
    ):
        for volume in page.get("Volumes", []):
            volume_id = str(volume.get("VolumeId") or "")
            if not volume_id:
                raise RuntimeError(f"EC2 returned a volume without an ID in {region}")
            found.append({"volume_id": volume_id, "state": str(volume.get("State") or "")})
    return sorted(found, key=lambda item: item["volume_id"])


def _pcs_remnants(session: Any, region: str, project_name: str) -> dict[str, list[dict[str, str]]]:
    """The PCS-launched instances still live and their volumes still present."""
    return {
        "instances": _pcs_instances(session, region, project_name),
        "volumes": _pcs_volumes(session, region, project_name),
    }


def _wait_for_pcs_nodes_terminated(
    ctx: RunContext,
    *,
    timeout_seconds: float = PCS_NODE_TERMINATION_WAIT_SECONDS,
    poll_seconds: float = PCS_NODE_TERMINATION_POLL_SECONDS,
) -> dict[str, Any]:
    """Per deployment Region, wait until no project-owned PCS node or node volume remains.

    Returns the evidence (what was seen, how long it took); raises when a
    Region still has live PCS nodes or their volumes at the deadline, naming
    them.
    """
    result: dict[str, Any] = {"started_at": utc_now(), "regions": {}}
    deadline = time.monotonic() + timeout_seconds
    remaining: dict[str, dict[str, list[dict[str, str]]]] = {}
    for region in ctx.deployment_regions:
        polls = 0
        seen = _pcs_remnants(ctx.session, region, ctx.config.project_name)
        first_seen = {kind: list(items) for kind, items in seen.items()}
        while any(seen.values()) and time.monotonic() < deadline:
            time.sleep(poll_seconds)
            polls += 1
            seen = _pcs_remnants(ctx.session, region, ctx.config.project_name)
        result["regions"][region] = {
            "initial_instances": first_seen["instances"],
            "remaining_instances": seen["instances"],
            "initial_volumes": first_seen["volumes"],
            "remaining_volumes": seen["volumes"],
            "polls": polls,
            "terminated": not any(seen.values()),
        }
        if any(seen.values()):
            remaining[region] = seen
    result["ended_at"] = utc_now()
    if remaining:
        raise RuntimeError(
            "AWS PCS compute nodes or their volumes were still present after "
            f"{int(timeout_seconds)}s: "
            + "; ".join(
                f"{region}: "
                + ", ".join(
                    f"{item.get('instance_id') or item.get('volume_id')} ({item['state']})"
                    for kind in ("instances", "volumes")
                    for item in seen[kind]
                )
                for region, seen in sorted(remaining.items())
            )
        )
    return result
