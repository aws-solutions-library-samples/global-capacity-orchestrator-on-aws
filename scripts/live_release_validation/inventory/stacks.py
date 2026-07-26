"""CloudFormation stack discovery, description, and fingerprinting."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from botocore.exceptions import ClientError

from ._shared import (
    _normalize_json_text,
    _project_owned_name,
    _tags_to_dict,
)


def discover_enabled_regions(session: Any, seed_region: str) -> list[str]:
    """Return every enabled Region in the caller's partition that supports CFN."""
    partition = session.get_partition_for_region(seed_region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for {seed_region}")
    available = set(session.get_available_regions("cloudformation", partition_name=partition))
    ec2 = session.client("ec2", region_name=seed_region)
    response = ec2.describe_regions(AllRegions=False)
    enabled = {
        str(item["RegionName"])
        for item in response.get("Regions", [])
        if item.get("RegionName")
        and item.get("OptInStatus") in {None, "opt-in-not-required", "opted-in"}
    }
    regions = sorted(enabled & available)
    if not regions:
        raise RuntimeError(
            f"No enabled CloudFormation Regions were discovered in partition {partition}"
        )
    return regions


def list_active_stacks(session: Any, region: str) -> list[dict[str, str]]:
    """List every non-deleted CloudFormation stack in one Region."""
    client = session.client("cloudformation", region_name=region)
    stacks: list[dict[str, str]] = []
    for page in client.get_paginator("list_stacks").paginate():
        for summary in page.get("StackSummaries", []):
            status = str(summary.get("StackStatus") or "")
            if status == "DELETE_COMPLETE":
                continue
            name = str(summary.get("StackName") or "")
            stack_id = str(summary.get("StackId") or "")
            if name:
                stacks.append({"name": name, "stack_id": stack_id, "status": status})
    return sorted(stacks, key=lambda item: (item["name"], item["stack_id"]))


def describe_stack(session: Any, region: str, stack_name: str) -> dict[str, Any] | None:
    """Describe one stack; return None only for authoritative nonexistence."""
    client = session.client("cloudformation", region_name=region)
    try:
        response = client.describe_stacks(StackName=stack_name)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        if (
            error.get("Code") == "ValidationError"
            and "does not exist" in str(error.get("Message", "")).lower()
        ):
            return None
        raise
    stacks = response.get("Stacks", [])
    if not stacks:
        return None
    stack = stacks[0]
    return {
        "name": str(stack.get("StackName") or stack_name),
        "stack_id": str(stack.get("StackId") or ""),
        "status": str(stack.get("StackStatus") or ""),
        "parameters": sorted(
            (
                {
                    "key": str(item["ParameterKey"]),
                    "value": str(item.get("ParameterValue") or ""),
                    "resolved_value": str(item.get("ResolvedValue") or ""),
                }
                for item in stack.get("Parameters", [])
                if item.get("ParameterKey") is not None
            ),
            key=lambda item: item["key"],
        ),
        "outputs": {
            str(item["OutputKey"]): str(item["OutputValue"])
            for item in stack.get("Outputs", [])
            if item.get("OutputKey") is not None and item.get("OutputValue") is not None
        },
        "tags": _tags_to_dict(stack.get("Tags", [])),
        "termination_protection": bool(stack.get("EnableTerminationProtection", False)),
    }


def _list_stack_resource_identities(client: Any, stack_id: str) -> list[dict[str, str]]:
    """Return every stack resource's exact physical identity in stable order."""
    identities: list[dict[str, str]] = []
    logical_ids: set[str] = set()
    for page in client.get_paginator("list_stack_resources").paginate(StackName=stack_id):
        for resource in page.get("StackResourceSummaries", []):
            logical_id = str(resource.get("LogicalResourceId") or "")
            resource_type = str(resource.get("ResourceType") or "")
            physical_id = str(resource.get("PhysicalResourceId") or "")
            if not logical_id or not resource_type or not physical_id:
                raise RuntimeError(
                    "CloudFormation omitted a protected stack resource identity for "
                    f"{stack_id}: {json.dumps(resource, sort_keys=True, default=str)}"
                )
            if logical_id in logical_ids:
                raise RuntimeError(
                    f"CloudFormation duplicated protected stack resource {stack_id}:{logical_id}"
                )
            logical_ids.add(logical_id)
            identities.append(
                {
                    "logical_id": logical_id,
                    "resource_type": resource_type,
                    "physical_id": physical_id,
                }
            )
    return sorted(
        identities,
        key=lambda item: (item["logical_id"], item["resource_type"], item["physical_id"]),
    )


def describe_stack_fingerprint(
    session: Any,
    region: str,
    stack_name: str,
) -> dict[str, Any] | None:
    """Return template, policy, and exact physical-resource fingerprints."""
    stack = describe_stack(session, region, stack_name)
    if stack is None:
        return None
    client = session.client("cloudformation", region_name=region)
    template = _normalize_json_text(
        client.get_template(
            StackName=stack["stack_id"],
            TemplateStage="Original",
        ).get("TemplateBody", "")
    )
    if isinstance(template, (dict, list)):
        template_bytes = json.dumps(
            template,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    else:
        template_bytes = str(template).encode("utf-8")

    try:
        stack_policy = _normalize_json_text(
            client.get_stack_policy(StackName=stack["stack_id"]).get("StackPolicyBody")
        )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        message = str(error.get("Message", "")).lower()
        if error.get("Code") == "ValidationError" and "stack policy" in message:
            stack_policy = None
        else:
            raise
    return {
        **stack,
        "template_sha256": hashlib.sha256(template_bytes).hexdigest(),
        "stack_policy": stack_policy,
        "physical_resources": _list_stack_resource_identities(client, stack["stack_id"]),
    }


def collect_stack_inventory(
    session: Any, regions: Iterable[str]
) -> dict[str, list[dict[str, str]]]:
    """Collect active stacks across Regions with deterministic ordering."""
    return {region: list_active_stacks(session, region) for region in sorted(set(regions))}


def collect_project_stacks(
    session: Any,
    regions: Iterable[str],
    project_name: str,
) -> dict[str, list[dict[str, str]]]:
    """Collect all project-prefixed stacks, including unexpected/orphan stacks."""
    inventory = collect_stack_inventory(session, regions)
    return {
        region: [stack for stack in stacks if _project_owned_name(stack["name"], project_name)]
        for region, stacks in inventory.items()
        if any(_project_owned_name(stack["name"], project_name) for stack in stacks)
    }
