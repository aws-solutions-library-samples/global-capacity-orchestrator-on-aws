"""Accepted-residue accounting for DynamoDB streams of deleted tables.

Deleting a DynamoDB table does not delete its stream: the stream object stays
readable (``DISABLED``) for roughly 24 hours so consumers can drain it, there
is no delete API for it, and the Resource Groups Tagging API keeps returning
its ARN for as long as the index remembers it — with or without tags. The
project scanners flag such an ARN by name alone (``.../table/gco-.../stream/...``),
so a run that correctly destroyed its vector-store table still presents a
"project resource" that nothing can remove. Observed live on 2026-08-20: the
previous run's two ``gco-vector-store`` stream ARNs failed the next run's
``baseline`` gate 4.5 hours after their tables were destroyed, with the
streams still describable and ``DISABLED``.

``_strip_expired_table_streams`` accepts exactly that shape and nothing else:
a ``tagged_resources`` entry whose ARN parses as a table stream in the same
region and expected account is stripped **only after** DynamoDB itself
confirms the parent table does not exist. A live table keeps its stream entry
in the inventory (genuine residue — and the table scanner reports the table
itself too). Every accepted entry is returned as evidence (region, table,
stream status, the check performed) so ``baseline`` and ``final-inventory``
disclose what they tolerated rather than silently ignoring it, mirroring the
pending-deletion KMS precedent in ``ownership/kms.py``.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from botocore.exceptions import ClientError

from ..models import RunContext

_TABLE_STREAM_ARN = re.compile(
    r"^arn:[^:]+:dynamodb:(?P<region>[a-z0-9-]+):(?P<account>\d{12})"
    r":table/(?P<table>[^/]+)/stream/(?P<label>.+)$"
)


def _stream_status(ctx: RunContext, region: str, stream_arn: str) -> str:
    """Return the live stream status, or ``ABSENT`` once fully expired."""
    streams = ctx.session.client("dynamodbstreams", region_name=region)
    try:
        description = streams.describe_stream(StreamArn=stream_arn).get("StreamDescription", {})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        return "ABSENT"
    return str(description.get("StreamStatus") or "UNKNOWN")


def _strip_expired_table_streams(
    ctx: RunContext,
    project_inventory: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Strip tagged stream ARNs whose parent table DynamoDB proves absent."""
    inventory = copy.deepcopy(project_inventory)
    accepted: list[dict[str, Any]] = []
    for region, resources in list(inventory.get("regional", {}).items()):
        kept: list[dict[str, Any]] = []
        for entry in resources.get("tagged_resources", []):
            arn = str(entry.get("arn") or "")
            match = _TABLE_STREAM_ARN.match(arn)
            if (
                match is None
                or match.group("region") != region
                or match.group("account") != ctx.settings.expected_account
            ):
                kept.append(entry)
                continue
            table_name = match.group("table")
            dynamodb = ctx.session.client("dynamodb", region_name=region)
            try:
                dynamodb.describe_table(TableName=table_name)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                    raise
            else:
                # The table is live, so the stream is real project residue —
                # keep it (the table scanner reports the table itself too).
                kept.append(entry)
                continue
            accepted.append(
                {
                    "region": region,
                    "arn": arn,
                    "table_name": table_name,
                    "table_absent": True,
                    "authority": "dynamodb:DescribeTable ResourceNotFoundException",
                    "stream_status": _stream_status(ctx, region, arn),
                    "tags": dict(entry.get("tags") or {}),
                    "note": (
                        "streams of deleted tables have no delete API and "
                        "expire on their own roughly 24h after table deletion"
                    ),
                }
            )
        resources["tagged_resources"] = kept
        if not any(resources.values()):
            inventory["regional"].pop(region)
    return inventory, accepted
