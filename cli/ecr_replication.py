"""Keep GCO's deploys and destroys from touching ECR replication rules it did not create.

A private registry has exactly one replication configuration per Region, and
the global stack manages it through ``AWS::ECR::ReplicationConfiguration``
when ``images.replication`` is on. That resource owns the whole
configuration: its create handler fails when the registry already has any
rule, its update handler writes GCO's rules over every rule in the registry,
and its delete handler clears all of them. CloudFormation applies the
``DeletionPolicy`` of the template a resource was last deployed with, so a
stack deployed by an earlier release (no policy) clears the registry when the
resource is removed or the stack is deleted.

From this release the resource is retained, and the CLI checks the registry
around every deploy and destroy of the global stack:

- deploy: refuse when rules GCO did not create exist and the deploy would
  create the resource, change GCO's rule (an update rewrites the whole
  configuration) or remove a resource that is not retained; also refuse when
  a GCO rule left by a stack deleted outside the CLI would make the create
  fail;
- destroy: refuse when the stack's resource is not retained and other rules
  exist; after the stack is gone, remove GCO's own rule and keep every other
  rule.

GCO's rule is the one the stack writes: a single ``PREFIX_MATCH`` filter on
``<project_name>/`` whose destinations are all in this account.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from botocore.exceptions import ClientError

#: CloudFormation type of the registry's replication configuration.
REPLICATION_RESOURCE_TYPE = "AWS::ECR::ReplicationConfiguration"

#: What the deployed global stack would do to the registry when the resource
#: leaves it: nothing (``absent``), keep the configuration (``retained``) or
#: clear every rule (``deletes``).
DeployedState = Literal["absent", "retained", "deletes"]

_DEFAULT_DESTINATIONS = "all_deployed_regions"


class ReplicationConflictError(RuntimeError):
    """A deploy would delete or overwrite ECR replication rules GCO did not create."""


def gco_repository_filter(project_name: str) -> dict[str, str]:
    """The repository filter of the rule the global stack writes."""
    return {"filter": f"{project_name}/", "filterType": "PREFIX_MATCH"}


def _normalized(rule: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "destinations": [
            {"region": d.get("region"), "registryId": d.get("registryId")}
            for d in rule.get("destinations") or []
        ],
        "repositoryFilters": [
            {"filter": f.get("filter"), "filterType": f.get("filterType")}
            for f in rule.get("repositoryFilters") or []
        ],
    }


def is_gco_rule(rule: Mapping[str, Any], *, project_name: str, account_id: str) -> bool:
    """Whether ``rule`` has the shape the global stack writes for ``project_name``."""
    normalized = _normalized(rule)
    destinations = normalized["destinations"]
    return (
        normalized["repositoryFilters"] == [gco_repository_filter(project_name)]
        and bool(destinations)
        and all(d["registryId"] == account_id for d in destinations)
    )


def split_rules(
    rules: Sequence[Mapping[str, Any]], *, project_name: str, account_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(gco_rules, other_rules)``, each in the registry's order."""
    gco: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for rule in rules:
        target = (
            gco if is_gco_rule(rule, project_name=project_name, account_id=account_id) else other
        )
        target.append(dict(rule))
    return gco, other


def desired_gco_rule(
    context: Mapping[str, Any], *, project_name: str, account_id: str
) -> dict[str, Any] | None:
    """The rule the global stack will write for this ``cdk.json`` context, or ``None``.

    Mirrors ``GCOGlobalStack._create_image_replication_rule``: replication on
    (the default), destinations ``all_deployed_regions`` (the regional
    Regions) or an explicit list, minus the global Region; no resource when
    nothing is left.
    """
    images = context.get("images") or {}
    if not isinstance(images, Mapping):
        return None
    replication = images.get("replication") or {}
    if not isinstance(replication, Mapping) or not bool(replication.get("enabled", True)):
        return None
    regions_config = context.get("deployment_regions") or {}
    global_region = regions_config.get("global")
    destinations = replication.get("destinations", _DEFAULT_DESTINATIONS)
    if isinstance(destinations, str):
        regional = regions_config.get("regional") or []
        candidates = [regional] if isinstance(regional, str) else list(regional)
    else:
        candidates = list(destinations)
    regions = [str(region) for region in candidates if region != global_region]
    if not regions:
        return None
    return {
        "destinations": [{"region": region, "registryId": account_id} for region in regions],
        "repositoryFilters": [gco_repository_filter(project_name)],
    }


def read_registry(ecr_client: Any) -> tuple[str, list[dict[str, Any]]]:
    """``(registry_id, rules)`` of the client's Region."""
    response = ecr_client.describe_registry()
    configuration = response.get("replicationConfiguration") or {}
    return str(response["registryId"]), list(configuration.get("rules") or [])


def deployed_state(cloudformation_client: Any, stack_name: str) -> DeployedState:
    """How the deployed ``stack_name`` holds its replication configuration.

    Raises:
        ClientError: for any failure other than CloudFormation reporting that
            the stack does not exist.
    """
    try:
        response = cloudformation_client.get_template(
            StackName=stack_name, TemplateStage="Original"
        )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        if error.get("Code") == "ValidationError" and "does not exist" in str(
            error.get("Message", "")
        ):
            return "absent"
        raise
    body = response.get("TemplateBody") or {}
    template = json.loads(body) if isinstance(body, str) else body
    resources = [
        resource
        for resource in (template.get("Resources") or {}).values()
        if isinstance(resource, Mapping) and resource.get("Type") == REPLICATION_RESOURCE_TYPE
    ]
    if not resources:
        return "absent"
    if all(resource.get("DeletionPolicy") == "Retain" for resource in resources):
        return "retained"
    return "deletes"


def _describe(rules: Sequence[Mapping[str, Any]]) -> str:
    parts = []
    for rule in rules:
        normalized = _normalized(rule)
        filters = ", ".join(str(f["filter"]) for f in normalized["repositoryFilters"]) or "*"
        targets = ", ".join(f"{d['registryId']}/{d['region']}" for d in normalized["destinations"])
        parts.append(f"[{filters} -> {targets}]")
    return " ".join(parts)


def _put_command(region: str, rules: Sequence[Mapping[str, Any]]) -> str:
    configuration = json.dumps({"rules": [_normalized(rule) for rule in rules]})
    return (
        f"aws ecr put-replication-configuration --region {region} "
        f"--replication-configuration '{configuration}'"
    )


def deploy_conflict(
    rules: Sequence[Mapping[str, Any]],
    *,
    desired: Mapping[str, Any] | None,
    state: DeployedState,
    project_name: str,
    account_id: str,
    region: str,
) -> str | None:
    """Why deploying the global stack would break the registry, or ``None``."""
    gco, other = split_rules(rules, project_name=project_name, account_id=account_id)
    stack = f"{project_name}-global"
    if not other:
        if desired is not None and state == "absent" and gco:
            return (
                f"The ECR registry in {region} still has the replication rule of a "
                f"{stack} stack that no longer exists, so CloudFormation cannot "
                f"create {stack}'s replication configuration. Remove the rule with:\n"
                f"  {_put_command(region, [])}"
            )
        return None
    listing = _describe(other)
    keep = (
        "Either set images.replication.enabled to false in cdk.json and manage the "
        "registry's replication yourself (add a rule for "
        f"'{project_name}/' if its images should be replicated), or remove those "
        "rules first."
    )
    if desired is not None:
        if state == "absent":
            return (
                f"The ECR registry in {region} has replication rules {project_name} did "
                f"not create: {listing}. CloudFormation creates {stack}'s replication "
                "configuration only in a registry without rules. " + keep
            )
        if [_normalized(rule) for rule in gco] != [_normalized(desired)]:
            return (
                f"The ECR registry in {region} has replication rules {project_name} did "
                f"not create: {listing}. This deploy changes {project_name}'s rule, and "
                "CloudFormation then rewrites the registry's whole replication "
                "configuration, which deletes those rules. " + keep
            )
        return None
    if state == "deletes":
        return (
            f"The ECR registry in {region} has replication rules {project_name} did not "
            f"create: {listing}. This deploy removes {stack}'s replication "
            "configuration, and the release that last deployed it does not retain it, "
            "so CloudFormation would delete every rule in the registry. Deploy once "
            "with images.replication.enabled still true (this release retains the "
            "configuration), then disable it."
        )
    return None


def destroy_conflict(
    rules: Sequence[Mapping[str, Any]],
    *,
    state: DeployedState,
    project_name: str,
    account_id: str,
    region: str,
) -> str | None:
    """Why destroying the global stack would break the registry, or ``None``."""
    _gco, other = split_rules(rules, project_name=project_name, account_id=account_id)
    if state != "deletes" or not other:
        return None
    stack = f"{project_name}-global"
    return (
        f"The ECR registry in {region} has replication rules {project_name} did not "
        f"create: {_describe(other)}. {stack} was last deployed by a release that does "
        "not retain its replication configuration, so deleting the stack would delete "
        f"every rule in the registry. Redeploy {stack} with this release first "
        f"('gco stacks deploy {stack}'), then destroy it."
    )


def remove_gco_rules(ecr_client: Any, *, project_name: str) -> int:
    """Remove ``project_name``'s rule from the registry and keep every other rule.

    ECR has no conditional write, so a rule another tool adds between the read
    and the write here is lost; this runs once, right after the global stack
    is deleted. Returns how many rules were removed.
    """
    account_id, rules = read_registry(ecr_client)
    gco, other = split_rules(rules, project_name=project_name, account_id=account_id)
    if not gco:
        return 0
    ecr_client.put_replication_configuration(replicationConfiguration={"rules": other})
    return len(gco)
