"""Transaction Search baseline: what the account looked like before the run touched it.

GCO's regional stack turns on CloudWatch Transaction Search so its services'
OpenTelemetry spans can be searched: a custom resource points the Region's
X-Ray trace segment destination at ``CloudWatchLogs`` and writes the
``gco-transaction-search-xray-access`` CloudWatch Logs resource policy, and
X-Ray then creates the ``aws/spans`` (and Application Signals the
``/aws/application-signals/data``) log group. All three are account- and
Region-scoped, and none of them is a CloudFormation resource: the enabler's
Delete is a deliberate no-op because other workloads in an account may depend
on Transaction Search. Stack teardown therefore leaves them behind, and the
harness has to put the account back itself.

This module is the authority for that restoration. Before anything deploys,
``baseline`` records per regional Region the destination and its status,
whether the GCO policy exists (only a SHA-256 of its document, never the
document: it names the account), and whether each span log group exists (its
creation time, so a later same-name generation is never mistaken for the
original). Nothing here mutates AWS state; ``cleanup/transaction_search.py``
acts on this record and ``final-inventory`` re-observes the account and
compares it with the record through :func:`_transaction_search_differences`.

A log group that did not exist at baseline is only ever the run's when the
Region's destination was ``XRay`` at baseline: had Transaction Search already
been on, every span producer in the account writes to the same group, so it
is retained and reported as accepted instead of deleted.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any, cast

from ..models import RunContext, utc_now
from ..ownership.log_groups import _observe_log_group_stability

#: The account-level CloudWatch Logs resource policy GCO's enabler writes.
TRANSACTION_SEARCH_POLICY_NAME = "gco-transaction-search-xray-access"
#: The span log groups Transaction Search (and Application Signals) write to.
TRANSACTION_SEARCH_LOG_GROUPS = ("aws/spans", "/aws/application-signals/data")
#: Checkpoint state key. Kept out of ``checkpoint.baseline`` on purpose: that
#: object stays exactly the protected-stack/ECR capture ``compare_baseline``
#: re-captures against after teardown.
TRANSACTION_SEARCH_BASELINE_STATE_KEY = "transaction_search_baseline"

_SCHEMA_VERSION = 1
_DESTINATIONS = frozenset({"XRay", "CloudWatchLogs"})
_STATUSES = frozenset({"PENDING", "ACTIVE"})
#: Consecutive agreeing reads before a log group's presence counts as settled.
_STABLE_OBSERVATIONS = 2
_XRAY_SERVICE_PRINCIPAL = "xray.amazonaws.com"


def _read_trace_segment_destination(xray: Any) -> dict[str, str]:
    """Return ``{"destination", "status"}``, failing closed on an unknown shape."""
    response = xray.get_trace_segment_destination()
    destination = str(response.get("Destination") or "")
    status = str(response.get("Status") or "")
    if destination not in _DESTINATIONS or status not in _STATUSES:
        raise RuntimeError(
            "X-Ray returned an unexpected trace segment destination "
            f"{destination!r} with status {status!r}"
        )
    return {"destination": destination, "status": status}


def _gco_resource_policy(logs: Any) -> dict[str, Any] | None:
    """Return the account-level GCO Transaction Search policy record, or ``None``."""
    found: dict[str, Any] | None = None
    kwargs: dict[str, Any] = {}
    while True:
        response = logs.describe_resource_policies(**kwargs)
        for raw in response.get("resourcePolicies") or []:
            if not isinstance(raw, Mapping):
                raise RuntimeError("CloudWatch Logs returned a non-object resource policy record")
            policy = cast(Mapping[str, Any], raw)
            if policy.get("policyName") != TRANSACTION_SEARCH_POLICY_NAME:
                continue
            if found is not None:
                raise RuntimeError(
                    f"CloudWatch Logs listed resource policy {TRANSACTION_SEARCH_POLICY_NAME} twice"
                )
            found = {str(key): value for key, value in policy.items()}
        token = response.get("nextToken")
        if not token:
            return found
        kwargs["nextToken"] = token


def _statement_list(document: Any) -> list[Mapping[str, Any]]:
    statements = document.get("Statement") if isinstance(document, Mapping) else None
    if isinstance(statements, Mapping):
        statements = [statements]
    if not isinstance(statements, list):
        return []
    return [item for item in statements if isinstance(item, Mapping)]


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _grants_xray_span_delivery(document_text: str) -> bool:
    """Whether a policy document lets X-Ray deliver spans (the enabler's shape)."""
    try:
        document = json.loads(document_text)
    except json.JSONDecodeError:
        return False
    for statement in _statement_list(document):
        principal = statement.get("Principal")
        services = principal.get("Service") if isinstance(principal, Mapping) else None
        if (
            statement.get("Effect") == "Allow"
            and _XRAY_SERVICE_PRINCIPAL in _as_list(services)
            and "logs:PutLogEvents" in _as_list(statement.get("Action"))
        ):
            return True
    return False


def _policy_summary(policy: Mapping[str, Any] | None) -> dict[str, Any]:
    """Sanitized evidence for the GCO policy: presence, document hash, write time."""
    if policy is None:
        return {
            "present": False,
            "document_sha256": None,
            "last_updated_time": None,
            "grants_xray_span_delivery": False,
        }
    document = str(policy.get("policyDocument") or "")
    last_updated = policy.get("lastUpdatedTime")
    return {
        "present": True,
        "document_sha256": hashlib.sha256(document.encode("utf-8")).hexdigest(),
        "last_updated_time": (
            last_updated
            if isinstance(last_updated, int) and not isinstance(last_updated, bool)
            else None
        ),
        "grants_xray_span_delivery": _grants_xray_span_delivery(document),
    }


def _log_group_presence(logs: Any, region: str, name: str) -> dict[str, Any]:
    """Settled presence of one span log group, identified by its creation time."""
    outcome = _observe_log_group_stability(
        logs,
        region,
        name,
        required_present=_STABLE_OBSERVATIONS,
        required_absent=_STABLE_OBSERVATIONS,
    )
    if outcome["status"] == "absent":
        return {"present": False, "creation_time": None}
    identity = outcome.get("identity")
    if outcome["status"] != "present" or not isinstance(identity, dict):
        raise RuntimeError(
            f"CloudWatch log group {region}:{name} did not settle: {outcome['status']}"
        )
    return {"present": True, "creation_time": int(identity["creation_time"])}


def _observe_transaction_search_state(ctx: RunContext, region: str) -> dict[str, Any]:
    """Read one Region's destination, GCO policy, and span log groups (read-only)."""
    xray = ctx.session.client("xray", region_name=region)
    logs = ctx.session.client("logs", region_name=region)
    return {
        "observed_at": utc_now(),
        **_read_trace_segment_destination(xray),
        "resource_policy": _policy_summary(_gco_resource_policy(logs)),
        "log_groups": {
            name: _log_group_presence(logs, region, name) for name in TRANSACTION_SEARCH_LOG_GROUPS
        },
    }


def _capture_transaction_search_baseline(ctx: RunContext) -> dict[str, Any]:
    """Observe every regional Region before the run changes anything."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "policy_name": TRANSACTION_SEARCH_POLICY_NAME,
        "regions": {
            region: _observe_transaction_search_state(ctx, region)
            for region in ctx.deployment_regions
        },
    }


def _validate_region_state(region: str, state: Any) -> None:
    """Fail closed on a malformed checkpointed Region record."""
    problem = f"Transaction Search baseline for {region} is malformed"
    if not isinstance(state, dict):
        raise RuntimeError(problem)
    if state.get("destination") not in _DESTINATIONS or state.get("status") not in _STATUSES:
        raise RuntimeError(f"{problem}: destination")
    policy = state.get("resource_policy")
    if (
        not isinstance(policy, dict)
        or not isinstance(policy.get("present"), bool)
        or isinstance(policy.get("document_sha256"), str) is not policy["present"]
    ):
        raise RuntimeError(f"{problem}: resource policy")
    groups = state.get("log_groups")
    if not isinstance(groups, dict) or set(groups) != set(TRANSACTION_SEARCH_LOG_GROUPS):
        raise RuntimeError(f"{problem}: log groups")
    for name, group in groups.items():
        creation_time = group.get("creation_time") if isinstance(group, dict) else None
        if (
            not isinstance(group, dict)
            or not isinstance(group.get("present"), bool)
            or (isinstance(creation_time, int) and not isinstance(creation_time, bool))
            is not group["present"]
        ):
            raise RuntimeError(f"{problem}: log group {name}")


def _validated_transaction_search_baseline(ctx: RunContext) -> dict[str, Any]:
    """Return the checkpointed baseline, requiring exactly the deployed Regions."""
    raw = ctx.checkpoint.state.get(TRANSACTION_SEARCH_BASELINE_STATE_KEY)
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
        raise RuntimeError(
            "Checkpoint has no Transaction Search baseline; the account's X-Ray trace "
            "segment destination cannot be proven or restored without it"
        )
    regions = raw.get("regions")
    if not isinstance(regions, dict) or set(regions) != set(ctx.deployment_regions):
        raise RuntimeError(
            "Transaction Search baseline does not cover exactly the deployed Regions"
        )
    for region, state in regions.items():
        _validate_region_state(str(region), state)
    return raw


def _transaction_search_differences(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
) -> tuple[list[str], list[dict[str, str]]]:
    """Compare one Region's current state with its baseline.

    Returns ``(differences, accepted)``. A span log group that appeared while
    Transaction Search was already enabled at baseline, and still is, is
    accepted rather than a difference: the run cannot tell its spans from
    anybody else's in a shared service log group.
    """
    differences: list[str] = []
    accepted: list[dict[str, str]] = []
    if current["destination"] != baseline["destination"]:
        differences.append(
            f"trace segment destination is {current['destination']}, "
            f"baseline {baseline['destination']}"
        )
    before = baseline["resource_policy"]
    after = current["resource_policy"]
    if before["present"] != after["present"]:
        differences.append(
            f"resource policy {TRANSACTION_SEARCH_POLICY_NAME} is "
            f"{'present' if after['present'] else 'absent'}, baseline "
            f"{'present' if before['present'] else 'absent'}"
        )
    elif before["present"] and before["document_sha256"] != after["document_sha256"]:
        differences.append(
            f"resource policy {TRANSACTION_SEARCH_POLICY_NAME} document changed since baseline"
        )
    shared = baseline["destination"] == "CloudWatchLogs" == current["destination"]
    for name in TRANSACTION_SEARCH_LOG_GROUPS:
        was = baseline["log_groups"][name]
        now = current["log_groups"][name]
        if was["present"]:
            if not now["present"]:
                differences.append(f"log group {name} existed at baseline and is gone")
            elif now["creation_time"] != was["creation_time"]:
                differences.append(f"log group {name} was replaced since baseline")
        elif now["present"]:
            if shared:
                accepted.append(
                    {
                        "log_group": name,
                        "reason": (
                            "Transaction Search was already enabled at baseline; the span "
                            "log group is shared by every span producer in the account"
                        ),
                    }
                )
            else:
                differences.append(f"log group {name} did not exist at baseline and remains")
    return differences, accepted


def _verify_transaction_search_restored(ctx: RunContext) -> dict[str, Any]:
    """Re-observe every regional Region and compare it with the baseline."""
    baseline = _validated_transaction_search_baseline(ctx)
    regions: dict[str, Any] = {}
    differences: list[str] = []
    for region in sorted(baseline["regions"]):
        before = baseline["regions"][region]
        current = _observe_transaction_search_state(ctx, region)
        region_differences, accepted = _transaction_search_differences(before, current)
        regions[region] = {
            "baseline": copy.deepcopy(before),
            "current": current,
            "differences": region_differences,
            "accepted_retained": accepted,
        }
        differences.extend(f"{region}: {item}" for item in region_differences)
    return {"regions": regions, "differences": differences}
