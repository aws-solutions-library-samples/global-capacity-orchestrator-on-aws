"""Wait for the EC2 instances AWS PCS launched to finish terminating after teardown.

CloudFormation deletes a managed PCS cluster's compute node groups before the
cluster, and PCS terminates each group's instances as part of that; the
stack is gone the moment PCS reports the group deleted, while EC2 still
shows the instances as ``shutting-down`` for a minute or two. The final
inventory counts ``shutting-down`` instances as live (an instance that is
still releasing its ENIs is not gone), so without this wait a clean
teardown could read as residue. The instances are found by the ownership
tag the launch templates stamp (``gco:project``) together with the tag PCS
itself puts on every node (``aws:pcs:compute-node-group-id``); anything
that is still not terminated at the deadline is reported, and the final
inventory then decides.
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


def _wait_for_pcs_nodes_terminated(
    ctx: RunContext,
    *,
    timeout_seconds: float = PCS_NODE_TERMINATION_WAIT_SECONDS,
    poll_seconds: float = PCS_NODE_TERMINATION_POLL_SECONDS,
) -> dict[str, Any]:
    """Per deployment Region, wait until no project-owned PCS node is still live.

    Returns the evidence (what was seen, how long it took); raises when a
    Region still has live PCS nodes at the deadline, naming them.
    """
    result: dict[str, Any] = {"started_at": utc_now(), "regions": {}}
    deadline = time.monotonic() + timeout_seconds
    remaining: dict[str, list[dict[str, str]]] = {}
    for region in ctx.deployment_regions:
        polls = 0
        instances = _pcs_instances(ctx.session, region, ctx.config.project_name)
        first_seen = list(instances)
        while instances and time.monotonic() < deadline:
            time.sleep(poll_seconds)
            polls += 1
            instances = _pcs_instances(ctx.session, region, ctx.config.project_name)
        result["regions"][region] = {
            "initial_instances": first_seen,
            "remaining_instances": instances,
            "polls": polls,
            "terminated": not instances,
        }
        if instances:
            remaining[region] = instances
    result["ended_at"] = utc_now()
    if remaining:
        raise RuntimeError(
            "AWS PCS compute nodes were still live after "
            f"{int(timeout_seconds)}s: "
            + "; ".join(
                f"{region}: "
                + ", ".join(f"{item['instance_id']} ({item['state']})" for item in items)
                for region, items in sorted(remaining.items())
            )
        )
    return result
