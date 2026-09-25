"""Ownership-matching primitives and category lists shared by the scanners.

A resource counts as project-owned only through an explicit signal: its
CloudFormation stack-name tag, a ``gco:project`` tag, or a name/ARN under
the project prefix. Everything here is deliberately conservative, because
a false positive would let teardown consider a pre-existing account
resource in scope."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

_ECR_MANIFEST_MEDIA_TYPES = (
    "application/vnd.docker.distribution.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
)


_GLOBAL_ACCELERATOR_CONTROL_REGIONS = {"aws": "us-west-2"}


_REGIONAL_PROJECT_RESOURCE_CATEGORIES = (
    "tagged_resources",
    "eks_clusters",
    "sqs_queues",
    "dynamodb_tables",
    "load_balancers",
    "target_groups",
    "instances",
    "cluster_volumes",
    "vpcs",
    "subnets",
    "nat_gateways",
    "flow_logs",
    "network_interfaces",
    "security_groups",
    "elastic_ips",
    "ecr_repositories",
    "kms_keys",
    "lambda_functions",
    "api_gateway_v1_apis",
    "api_gateway_v2_apis",
    "cloudwatch_log_groups",
    "secrets",
    "backup_vaults",
    "backup_plans",
    "backup_selections",
    "backup_recovery_points",
)


_GLOBAL_PROJECT_RESOURCE_CATEGORIES = (
    "global_accelerators",
    "s3_buckets",
    "iam_roles",
    "iam_policies",
    "iam_instance_profiles",
    "iam_users",
    "iam_groups",
)


_PROJECT_RESOURCE_CATEGORIES = (
    "cloudformation_stacks",
    *_REGIONAL_PROJECT_RESOURCE_CATEGORIES,
    *_GLOBAL_PROJECT_RESOURCE_CATEGORIES,
)


_PROJECT_RESOURCE_SCANNERS = (
    "cloudformation_stacks",
    "resource_groups_tagging_api",
    "eks_clusters",
    "sqs_queues",
    "dynamodb_tables",
    "load_balancers",
    "target_groups",
    "ec2_instances",
    "ec2_networking",
    "ecr_repositories",
    "kms_keys",
    "lambda_functions",
    "api_gateway_v1_apis",
    "api_gateway_v2_apis",
    "cloudwatch_log_groups",
    "secrets_manager",
    "cluster_volumes",
    "aws_backup",
    "s3_buckets",
    "iam",
    "global_accelerators",
)


def _project_owned_name(name: str, project_name: str) -> bool:
    return name == project_name or name.startswith((f"{project_name}-", f"{project_name}/"))


def _tags_to_dict(tags: Iterable[dict[str, Any]]) -> dict[str, str]:
    return {
        str(tag.get("Key")): str(tag.get("Value")) for tag in tags if tag.get("Key") is not None
    }


def _tags_are_project_owned(tags: dict[str, str], project_name: str) -> bool:
    stack_name = tags.get("aws:cloudformation:stack-name", "")
    explicit_project = tags.get("gco:project", "")
    return _project_owned_name(stack_name, project_name) or explicit_project == project_name


def _normalize_json_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _mapping_tags(tags: Any) -> dict[str, str]:
    if tags is None:
        return {}
    if not isinstance(tags, dict):
        raise RuntimeError("AWS returned tags in an unexpected format")
    return {str(key): str(value) for key, value in tags.items()}


def _name_or_path_is_project_owned(value: str, project_name: str) -> bool:
    if _project_owned_name(value, project_name):
        return True
    components = [component for component in value.replace(":", "/").split("/") if component]
    return any(_project_owned_name(component, project_name) for component in components)


def _arn_is_project_owned(arn: str, project_name: str) -> bool:
    parts = arn.split(":", 5)
    if len(parts) != 6:
        return False
    components = [component for component in parts[5].replace(":", "/").split("/") if component]
    if len(components) > 1:
        components = components[1:]
    return any(_project_owned_name(component, project_name) for component in components)


def _ec2_resource_is_project_owned(resource: dict[str, Any], project_name: str) -> bool:
    tags = _tags_to_dict(resource.get("Tags", []))
    return _tags_are_project_owned(tags, project_name) or _project_owned_name(
        tags.get("Name", ""), project_name
    )


def _iam_resource_is_project_owned(
    name: str,
    path: str,
    tags: dict[str, str],
    project_name: str,
) -> bool:
    return (
        _project_owned_name(name, project_name)
        or _name_or_path_is_project_owned(path, project_name)
        or _tags_are_project_owned(tags, project_name)
    )
