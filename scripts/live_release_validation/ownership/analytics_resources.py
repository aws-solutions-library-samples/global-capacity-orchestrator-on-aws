"""Accept Tagging-API records of analytics resources their services prove gone.

The analytics stack's Cognito user pool and EMR Serverless application carry
the stack's ownership tags, so the Resource Groups Tagging API lists them under
the project, and its index keeps both for hours after the stack deletes them.
A release run's baseline, nearly four hours after a test deployment's
teardown, still found the user pool, which ``DescribeUserPool`` answered with
``ResourceNotFoundException``, and the application, which ``GetApplication``
answered as ``TERMINATED``, the state EMR Serverless keeps a deleted
application in. Both entries had their stack tags intact. A run's own teardown
would fail its final inventory the same way, and the next run's clean-account
gate after it.

Acceptance is conditional, as for the HyperPod clusters: a user pool is
stripped only once Cognito no longer knows it, and an application only once
EMR Serverless reports it ``TERMINATED`` or no longer knows it. Any other
answer keeps the entry as residue.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

from ..models import RunContext

_ABSENT = "ABSENT"
_NOT_FOUND = "ResourceNotFoundException"


def _user_pool_state(ctx: RunContext, region: str, pool_id: str) -> str:
    """``EXISTS``, or ``ABSENT`` once Cognito no longer knows the user pool."""
    cognito = ctx.session.client("cognito-idp", region_name=region)
    try:
        cognito.describe_user_pool(UserPoolId=pool_id)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != _NOT_FOUND:
            raise
        return _ABSENT
    return "EXISTS"


def _application_state(ctx: RunContext, region: str, application_id: str) -> str:
    """The application's state, or ``ABSENT`` once EMR Serverless no longer knows it."""
    emr = ctx.session.client("emr-serverless", region_name=region)
    try:
        response = emr.get_application(applicationId=application_id)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != _NOT_FOUND:
            raise
        return _ABSENT
    return str((response.get("application") or {}).get("state") or "UNKNOWN")


@dataclass(frozen=True)
class _Kind:
    name: str
    arn: re.Pattern[str]
    authority: str
    state: Callable[[RunContext, str, str], str]
    gone: frozenset[str]


_KINDS = (
    _Kind(
        "cognito-user-pool",
        re.compile(
            r"^arn:[^:]+:cognito-idp:(?P<region>[a-z0-9-]+):(?P<account>\d{12})"
            r":userpool/(?P<resource_id>[A-Za-z0-9_-]+)$"
        ),
        "cognito-idp:DescribeUserPool",
        _user_pool_state,
        frozenset({_ABSENT}),
    ),
    _Kind(
        "emr-serverless-application",
        re.compile(
            r"^arn:[^:]+:emr-serverless:(?P<region>[a-z0-9-]+):(?P<account>\d{12})"
            r":/applications/(?P<resource_id>[a-z0-9]+)$"
        ),
        "emr-serverless:GetApplication",
        _application_state,
        frozenset({_ABSENT, "TERMINATED"}),
    ),
)


def _kind_of(arn: str) -> tuple[_Kind, re.Match[str]] | None:
    for kind in _KINDS:
        match = kind.arn.fullmatch(arn)
        if match is not None:
            return kind, match
    return None


def _strip_deleted_analytics_resources(
    ctx: RunContext,
    project_inventory: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Strip tagged user-pool and EMR Serverless ARNs whose service proves them gone."""
    inventory = copy.deepcopy(project_inventory)
    accepted: list[dict[str, Any]] = []
    for region, resources in list(inventory.get("regional", {}).items()):
        kept: list[dict[str, Any]] = []
        for entry in resources.get("tagged_resources", []):
            arn = str(entry.get("arn") or "")
            found = _kind_of(arn)
            if (
                found is None
                or found[1].group("region") != region
                or found[1].group("account") != ctx.settings.expected_account
            ):
                kept.append(entry)
                continue
            kind, match = found
            state = kind.state(ctx, region, match.group("resource_id"))
            if state not in kind.gone:
                # The service still has it: genuine residue.
                kept.append(entry)
                continue
            accepted.append(
                {
                    "region": region,
                    "arn": arn,
                    "kind": kind.name,
                    "state": state,
                    "authority": kind.authority,
                    "tags": dict(entry.get("tags") or {}),
                    "note": (
                        "the Resource Groups Tagging API index lags this deletion by hours; "
                        "the service is the authority for existence"
                    ),
                }
            )
        resources["tagged_resources"] = kept
        if not any(resources.values()):
            inventory["regional"].pop(region)
    return inventory, accepted
