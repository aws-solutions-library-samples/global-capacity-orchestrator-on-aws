"""Modular live-validation actions and registry."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit

from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from cli.jobs import resolve_submission_identity

from .inventory import (
    capture_baseline,
    collect_ecr_inventory,
    collect_project_resources,
    collect_project_stacks,
    compare_baseline,
    describe_ecr_image_by_tag,
    describe_stack,
    discover_enabled_regions,
    project_resources_are_absent,
    summarize_project_resources,
)
from .models import RunContext, utc_now

ActionHandler = Callable[[RunContext], dict[str, Any]]
_TERMINAL_QUEUE_STATUSES = {"succeeded", "failed", "cancelled"}
_HEALTHY_STACK_STATUSES = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
_RUN_STACK_TAG = "GcoLiveValidationRun"
_RUN_JOB_LABEL = "gco.aws/validation-run"
_PATH_JOB_LABEL = "gco.aws/validation-path"
_EKS_KEY_LOGICAL_ID_FRAGMENT = "EksSecretsEncryptionKey"
_KMS_PENDING_WINDOW_DAYS = 7
_CENTRAL_QUEUE_IDEMPOTENCY_NAMESPACE = uuid.UUID("88284d12-1e04-47d5-8871-607a9e4dac09")


@dataclass(frozen=True)
class ActionDefinition:
    """One selectable action and its safety dependencies."""

    name: str
    description: str
    dependencies: tuple[str, ...]
    handler: ActionHandler


def _run_git(repo_root: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise RuntimeError(f"git {' '.join(arguments)} failed: {message}")
    return result.stdout.strip()


def _resolve_branch(repo_root: Path) -> str:
    branch = _run_git(repo_root, "symbolic-ref", "--short", "HEAD", check=False)
    if branch:
        return branch
    raise RuntimeError("HEAD is detached; local live validation requires a checked-out branch")


def _validate_profile(ctx: RunContext) -> None:
    count = len(ctx.deployment_regions)
    profile = ctx.settings.profile
    if profile == "single-region" and count != 1:
        raise RuntimeError(
            f"single-region profile requires exactly one regional Region; cdk.json has {count}"
        )
    if profile == "multi-region" and count < 2:
        raise RuntimeError(
            f"multi-region profile requires at least two regional Regions; cdk.json has {count}"
        )
    if profile not in {"configured", "single-region", "multi-region"}:
        raise RuntimeError(f"Unknown validation profile: {profile}")


def _topology_regions(ctx: RunContext) -> tuple[str, ...]:
    regions = ctx.cdk_context["deployment_regions"]
    return tuple(
        dict.fromkeys(
            (
                str(regions["global"]),
                str(regions["api_gateway"]),
                str(regions["monitoring"]),
                *ctx.deployment_regions,
            )
        )
    )


def _direct_regional_access_enabled(ctx: RunContext) -> bool:
    partition = ctx.session.get_partition_for_region(ctx.config.global_region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for {ctx.config.global_region}")
    configured = bool((ctx.cdk_context.get("api_gateway") or {}).get("regional_api_enabled", False))
    return partition != "aws" or configured


def _job_transport_region(ctx: RunContext, execution_region: str) -> str | None:
    """Choose authorized transport without probing a denied regional bridge."""
    if _direct_regional_access_enabled(ctx):
        return execution_region
    if len(ctx.deployment_regions) == 1 and execution_region == ctx.deployment_regions[0]:
        return None
    raise RuntimeError(
        "Multi-Region workload validation requires api_gateway.regional_api_enabled=true "
        "so each Job can be observed and deleted in its exact execution Region"
    )


def _project_ecr_name(name: str, project_name: str) -> bool:
    return name == project_name or name.startswith((f"{project_name}/", f"{project_name}-"))


_PROTECTED_REGIONAL_RESOURCE_CATEGORIES = {
    "AWS::ApiGateway::RestApi": "api_gateway_v1_apis",
    "AWS::ApiGatewayV2::Api": "api_gateway_v2_apis",
    "AWS::Backup::BackupPlan": "backup_plans",
    "AWS::Backup::BackupSelection": "backup_selections",
    "AWS::Backup::BackupVault": "backup_vaults",
    "AWS::DynamoDB::Table": "dynamodb_tables",
    "AWS::EC2::EIP": "elastic_ips",
    "AWS::EC2::Instance": "instances",
    "AWS::EC2::NetworkInterface": "network_interfaces",
    "AWS::EC2::SecurityGroup": "security_groups",
    "AWS::EC2::Subnet": "subnets",
    "AWS::EC2::VPC": "vpcs",
    "AWS::ECR::Repository": "ecr_repositories",
    "AWS::EKS::Cluster": "eks_clusters",
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "load_balancers",
    "AWS::KMS::Key": "kms_keys",
    "AWS::Lambda::Function": "lambda_functions",
    "AWS::Logs::LogGroup": "cloudwatch_log_groups",
    "AWS::SQS::Queue": "sqs_queues",
    "AWS::SecretsManager::Secret": "secrets",
}
_PROTECTED_GLOBAL_RESOURCE_CATEGORIES = {
    "AWS::GlobalAccelerator::Accelerator": "global_accelerators",
    "AWS::IAM::Group": "iam_groups",
    "AWS::IAM::InstanceProfile": "iam_instance_profiles",
    "AWS::IAM::ManagedPolicy": "iam_policies",
    "AWS::IAM::Role": "iam_roles",
    "AWS::IAM::User": "iam_users",
    "AWS::S3::Bucket": "s3_buckets",
}
_IAM_ARN_RESOURCE_KINDS = {
    "AWS::IAM::Group": "group",
    "AWS::IAM::InstanceProfile": "instance-profile",
    "AWS::IAM::ManagedPolicy": "policy",
    "AWS::IAM::Role": "role",
    "AWS::IAM::User": "user",
}
_BACKUP_ARN_RESOURCE_PREFIXES = {
    "AWS::Backup::BackupPlan": "backup-plan:",
    "AWS::Backup::BackupVault": "backup-vault:",
}
_CLOUDFORMATION_STACK_ID_TAG = "aws:cloudformation:stack-id"
_SQS_DNS_SUFFIXES = {
    "aws": "amazonaws.com",
    "aws-cn": "amazonaws.com.cn",
    "aws-us-gov": "amazonaws.com",
}


def _baseline_protected_identities(
    baseline: dict[str, Any],
) -> tuple[dict[str, set[str]], dict[str, dict[str, set[str]]]]:
    """Validate and index exact protected stack and physical-resource identities."""
    protected_stacks = baseline.get("protected_stacks")
    if protected_stacks is None:
        protected_stacks = {}
    elif not isinstance(protected_stacks, dict):
        raise RuntimeError("Baseline protected_stacks must be an object")

    stack_ids: dict[str, set[str]] = {}
    resource_ids: dict[str, dict[str, set[str]]] = {}
    for raw_region, stacks in protected_stacks.items():
        region = str(raw_region)
        if not isinstance(stacks, list):
            raise RuntimeError(f"Protected stack baseline for {region} must be a list")
        for stack in stacks:
            if not isinstance(stack, dict):
                raise RuntimeError(f"Protected stack baseline for {region} is malformed")
            stack_id = str(stack.get("stack_id") or "")
            if not stack_id:
                raise RuntimeError(f"Protected stack baseline for {region} omitted its stack ID")
            stack_ids.setdefault(region, set()).add(stack_id)
            resources = stack.get("physical_resources")
            if not isinstance(resources, list):
                raise RuntimeError(
                    f"Protected stack baseline {region}:{stack_id} omitted physical resources"
                )
            for resource in resources:
                if not isinstance(resource, dict):
                    raise RuntimeError(
                        f"Protected stack baseline {region}:{stack_id} has a malformed resource"
                    )
                resource_type = str(resource.get("resource_type") or "")
                physical_id = str(resource.get("physical_id") or "")
                logical_id = str(resource.get("logical_id") or "")
                if not resource_type or not physical_id or not logical_id:
                    raise RuntimeError(
                        f"Protected stack baseline {region}:{stack_id} has an incomplete resource"
                    )
                resource_ids.setdefault(region, {}).setdefault(resource_type, set()).add(
                    physical_id
                )
    return stack_ids, resource_ids


def _iam_arn_name(arn: str, resource_kind: str) -> str | None:
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn" or parts[2] != "iam":
        return None
    prefix = f"{resource_kind}/"
    resource = parts[5]
    if not resource.startswith(prefix):
        return None
    name = resource[len(prefix) :].rsplit("/", 1)[-1]
    return name or None


def _lambda_arn_name(arn: str) -> str | None:
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn" or parts[2] != "lambda":
        return None
    resource = parts[5]
    if not resource.startswith("function:"):
        return None
    name = resource.removeprefix("function:")
    return name if name and ":" not in name else None


def _backup_arn_physical_id(arn: str, resource_prefix: str) -> str | None:
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn" or parts[2] != "backup":
        return None
    resource = parts[5]
    if not resource.startswith(resource_prefix):
        return None
    physical_id = resource.removeprefix(resource_prefix)
    return physical_id or None


def _sqs_queue_name_from_physical_id(
    physical_id: str,
    *,
    partition: str,
    region: str,
    account_id: str,
) -> str | None:
    """Normalize an exact SQS queue name or CloudFormation queue URL."""
    if "://" not in physical_id:
        return physical_id or None

    dns_suffix = _SQS_DNS_SUFFIXES.get(partition)
    parsed = urlsplit(physical_id)
    if (
        dns_suffix is None
        or parsed.scheme != "https"
        or parsed.hostname != f"sqs.{region}.{dns_suffix}"
        or parsed.query
        or parsed.fragment
    ):
        return None
    path_parts = parsed.path.strip("/").split("/")
    if len(path_parts) != 2 or path_parts[0] != account_id:
        return None
    return path_parts[1] or None


def _tagged_arn_matches_protected_physical_id(
    resource_type: str,
    arn: str,
    physical_id: str,
) -> bool:
    """Match a Tagging API ARN to one exact CloudFormation physical ID."""
    if arn == physical_id:
        return True

    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn":
        return False
    partition, service, region, account_id, resource = (
        parts[1],
        parts[2],
        parts[3],
        parts[4],
        parts[5],
    )

    if resource_type == "AWS::Lambda::Function":
        return _lambda_arn_name(arn) == physical_id
    if resource_type == "AWS::DynamoDB::Table":
        return (
            service == "dynamodb"
            and resource.startswith("table/")
            and "/" not in resource.removeprefix("table/")
            and resource.removeprefix("table/") == physical_id
        )
    if resource_type == "AWS::S3::Bucket":
        return (
            service == "s3" and bool(resource) and "/" not in resource and resource == physical_id
        )
    if resource_type == "AWS::SQS::Queue":
        if service != "sqs" or not region or not account_id or not resource:
            return False
        queue_name = _sqs_queue_name_from_physical_id(
            physical_id,
            partition=partition,
            region=region,
            account_id=account_id,
        )
        return queue_name == resource
    if resource_type == "AWS::KMS::Key":
        return (
            service == "kms"
            and resource.startswith("key/")
            and "/" not in resource.removeprefix("key/")
            and resource.removeprefix("key/") == physical_id
        )
    return False


def _tagged_resource_is_protected(
    record: Any,
    *,
    protected_stack_ids: set[str],
    protected_resource_ids: dict[str, set[str]],
    exact_arns: set[str],
) -> bool:
    """Return whether a tagged record has one exact protected identity."""
    if not isinstance(record, Mapping):
        return False
    arn = str(record.get("arn") or "")
    if not arn:
        return False
    if arn in exact_arns:
        return True

    tags = record.get("tags")
    if isinstance(tags, Mapping):
        stack_id = str(tags.get(_CLOUDFORMATION_STACK_ID_TAG) or "")
        if stack_id in protected_stack_ids:
            return True

    return any(
        _tagged_arn_matches_protected_physical_id(resource_type, arn, physical_id)
        for resource_type, physical_ids in protected_resource_ids.items()
        for physical_id in physical_ids
    )


def _matches_protected_physical_identity(
    resource_type: str,
    category: str,
    candidate: Any,
    physical_id: str,
    *,
    protected_backup_plan_ids: set[str] | None = None,
) -> bool:
    """Match one inventory record without any prefix or ownership-name fallback."""
    if category == "kms_keys":
        if not isinstance(candidate, dict):
            return False
        return physical_id in {
            str(candidate.get("key_id") or ""),
            str(candidate.get("arn") or ""),
        }
    if not isinstance(candidate, str):
        return False
    if resource_type == "AWS::Backup::BackupSelection":
        plan_id, separator, selection_id = candidate.partition(":")
        return bool(
            separator
            and selection_id == physical_id
            and plan_id in (protected_backup_plan_ids or set())
        )
    if candidate == physical_id:
        return True
    backup_prefix = _BACKUP_ARN_RESOURCE_PREFIXES.get(resource_type)
    if backup_prefix is not None:
        return _backup_arn_physical_id(candidate, backup_prefix) == physical_id
    iam_kind = _IAM_ARN_RESOURCE_KINDS.get(resource_type)
    if iam_kind is not None:
        return _iam_arn_name(candidate, iam_kind) == physical_id
    if resource_type == "AWS::Lambda::Function":
        return _lambda_arn_name(candidate) == physical_id
    return False


def _eks_pod_parent_cluster(arn: str, expected_region: str) -> str | None:
    """Parse only complete regional EKS pod ARNs; malformed records stay visible."""
    parts = arn.split(":", 5)
    if (
        len(parts) != 6
        or parts[0] != "arn"
        or not parts[1]
        or parts[2] != "eks"
        or parts[3] != expected_region
        or not parts[4]
    ):
        return None
    resource_parts = parts[5].split("/")
    if len(resource_parts) != 3 or resource_parts[0] != "podidentityassociation":
        return None
    if any(not component for component in resource_parts):
        return None
    return resource_parts[1]


def _strip_baseline_ecr(
    project_inventory: dict[str, Any], baseline: dict[str, Any]
) -> dict[str, Any]:
    """Strip only exact protected identities and provably stale EKS pod records."""
    protected_stack_ids, protected_resource_ids = _baseline_protected_identities(baseline)
    baseline_ecr_names: dict[str, set[str]] = {}
    baseline_ecr_arns: dict[str, set[str]] = {}
    for raw_region, repositories in (baseline.get("ecr_repositories") or {}).items():
        region = str(raw_region)
        for repository in repositories:
            name = str(repository.get("name") or "")
            arn = str(repository.get("arn") or "")
            if name:
                baseline_ecr_names.setdefault(region, set()).add(name)
            if arn:
                baseline_ecr_arns.setdefault(region, set()).add(arn)

    inventory = copy.deepcopy(project_inventory)
    stacks_by_region = inventory.get("cloudformation_stacks")
    if isinstance(stacks_by_region, dict):
        for region, stacks in list(stacks_by_region.items()):
            exact_stack_ids = protected_stack_ids.get(str(region), set())
            remaining = [
                stack for stack in stacks if str(stack.get("stack_id") or "") not in exact_stack_ids
            ]
            if remaining:
                stacks_by_region[region] = remaining
            else:
                stacks_by_region.pop(region)

    authoritative_clusters = inventory.get("authoritative_eks_clusters")
    for region, resources in list(inventory.get("regional", {}).items()):
        region_key = str(region)
        region_stack_ids = protected_stack_ids.get(region_key, set())
        region_resource_ids = protected_resource_ids.get(region_key, {})
        exact_tagged_arns = {
            physical_id
            for physical_ids in region_resource_ids.values()
            for physical_id in physical_ids
            if physical_id.startswith("arn:")
        }
        exact_tagged_arns.update(region_stack_ids)
        exact_tagged_arns.update(baseline_ecr_arns.get(region_key, set()))
        if "tagged_resources" in resources:
            resources["tagged_resources"] = [
                record
                for record in resources.get("tagged_resources", [])
                if not _tagged_resource_is_protected(
                    record,
                    protected_stack_ids=region_stack_ids,
                    protected_resource_ids=region_resource_ids,
                    exact_arns=exact_tagged_arns,
                )
            ]

        protected_backup_plan_ids = region_resource_ids.get(
            "AWS::Backup::BackupPlan",
            set(),
        )
        for resource_type, category in _PROTECTED_REGIONAL_RESOURCE_CATEGORIES.items():
            if category not in resources:
                continue
            physical_ids = region_resource_ids.get(resource_type, set())
            if physical_ids:
                resources[category] = [
                    candidate
                    for candidate in resources.get(category, [])
                    if not any(
                        _matches_protected_physical_identity(
                            resource_type,
                            category,
                            candidate,
                            physical_id,
                            protected_backup_plan_ids=protected_backup_plan_ids,
                        )
                        for physical_id in physical_ids
                    )
                ]

        if "ecr_repositories" in resources:
            resources["ecr_repositories"] = [
                name
                for name in resources.get("ecr_repositories", [])
                if name not in baseline_ecr_names.get(str(region), set())
            ]

        if (
            "tagged_resources" in resources
            and isinstance(authoritative_clusters, dict)
            and region in authoritative_clusters
            and isinstance(authoritative_clusters[region], list)
        ):
            existing_clusters = {str(name) for name in authoritative_clusters[region]}
            resources["tagged_resources"] = [
                record
                for record in resources["tagged_resources"]
                if (
                    (parent := _eks_pod_parent_cluster(str(record.get("arn") or ""), region))
                    is None
                    or parent in existing_clusters
                )
            ]

        if not any(resources.values()):
            inventory["regional"].pop(region)

    for resource_type, category in _PROTECTED_GLOBAL_RESOURCE_CATEGORIES.items():
        if category not in inventory:
            continue
        physical_ids = {
            physical_id
            for resources_by_type in protected_resource_ids.values()
            for physical_id in resources_by_type.get(resource_type, set())
        }
        if physical_ids:
            inventory[category] = [
                candidate
                for candidate in inventory.get(category, [])
                if not any(
                    _matches_protected_physical_identity(
                        resource_type,
                        category,
                        candidate,
                        physical_id,
                    )
                    for physical_id in physical_ids
                )
            ]
    return inventory


def _strip_expected_retained_ecr(
    ctx: RunContext,
    final_baseline: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Remove only exact checkpointed ECR residuals from comparison inventory."""
    sanitized = copy.deepcopy(final_baseline)
    repositories_by_region = sanitized.setdefault("ecr_repositories", {})
    baseline_repositories = {
        (str(region), str(repository["name"])): repository
        for region, repositories in (ctx.checkpoint.baseline or {})
        .get("ecr_repositories", {})
        .items()
        for repository in repositories
    }
    accepted: dict[str, list[dict[str, Any]]] = {
        "repositories": [],
        "image_deltas": [],
    }
    created_keys: set[tuple[str, str]] = set()

    for record in ctx.checkpoint.state.get("created_ecr_repositories", []):
        region = str(record["region"])
        name = str(record["name"])
        repository_key = (region, name)
        if repository_key in created_keys or repository_key in baseline_repositories:
            raise RuntimeError(f"Invalid retained ECR repository authority for {region}:{name}")
        created_keys.add(repository_key)
        repositories = repositories_by_region.get(region, [])
        matches = [repository for repository in repositories if repository.get("name") == name]
        if len(matches) > 1:
            raise RuntimeError(f"Final ECR inventory duplicated {region}:{name}")
        if not matches:
            accepted["repositories"].append(
                {"region": region, "name": name, "arn": record["arn"], "already_absent": True}
            )
            continue
        repository = matches[0]
        if _ecr_creation_identity(repository) != record.get("creation_identity"):
            raise RuntimeError(f"Retained ECR repository identity changed for {region}:{name}")
        if (repository.get("tags") or {}).get(_RUN_STACK_TAG) != record.get("run_tag"):
            raise RuntimeError(f"Retained ECR repository run tag changed for {record['arn']}")
        repositories.remove(repository)
        accepted["repositories"].append(
            {
                "region": region,
                "name": name,
                "arn": record["arn"],
                "retained": True,
                "inventory": repository,
            }
        )

    observed_delta_keys: set[tuple[str, str, str]] = set()
    for record in ctx.checkpoint.state.get("retained_ecr_image_deltas", []):
        region = str(record["region"])
        name = str(record["repository"])
        tag = str(record["tag"])
        image_key = (region, name, tag)
        if image_key in observed_delta_keys or (region, name) in created_keys:
            raise RuntimeError(f"Invalid retained ECR image-delta authority for {image_key}")
        observed_delta_keys.add(image_key)
        baseline_repository = baseline_repositories.get((region, name))
        if baseline_repository is None or _image_with_tag(baseline_repository, tag) is not None:
            raise RuntimeError(
                f"Retained ECR image delta is not absent from the baseline: {image_key}"
            )
        repositories = repositories_by_region.get(region, [])
        matches = [repository for repository in repositories if repository.get("name") == name]
        if len(matches) != 1:
            raise RuntimeError(
                f"Baseline ECR repository changed before final comparison: {image_key}"
            )
        repository = matches[0]
        images = repository.get("images", [])
        tagged_images = [image for image in images if tag in image.get("tags", [])]
        if len(tagged_images) > 1:
            raise RuntimeError(f"Retained ECR tag resolves to multiple images: {image_key}")
        if not tagged_images:
            accepted["image_deltas"].append(
                {"region": region, "repository": name, "tag": tag, "already_absent": True}
            )
            continue
        image = tagged_images[0]
        if _ecr_image_identity(image) != record.get("identity"):
            raise RuntimeError(f"Retained ECR image identity changed for {image_key}")
        remaining_tags = sorted(value for value in image.get("tags", []) if value != tag)
        baseline_digest_matches = [
            baseline_image
            for baseline_image in baseline_repository.get("images", [])
            if baseline_image.get("digest") == image.get("digest")
        ]
        if len(baseline_digest_matches) > 1:
            raise RuntimeError(f"Baseline ECR digest is ambiguous for {image_key}")
        if remaining_tags or baseline_digest_matches:
            image["tags"] = remaining_tags
        else:
            images.remove(image)
        accepted["image_deltas"].append(
            {
                "region": region,
                "repository": name,
                "tag": tag,
                "digest": record["identity"]["digest"],
                "retained": True,
            }
        )

    return sanitized, accepted


def _strip_accepted_retained_ecr(
    project_inventory: dict[str, Any],
    accepted: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Exclude exact retained repositories after final identity revalidation."""
    allowed = {
        (str(item["region"]), str(item["name"]))
        for item in accepted.get("repositories", [])
        if item.get("retained")
    }
    inventory = copy.deepcopy(project_inventory)
    for region, resources in list(inventory.get("regional", {}).items()):
        resources["ecr_repositories"] = [
            name
            for name in resources.get("ecr_repositories", [])
            if (str(region), str(name)) not in allowed
        ]
        if not any(resources.values()):
            inventory["regional"].pop(region)
    return inventory


def _owned_stacks(ctx: RunContext) -> dict[str, dict[str, dict[str, Any]]]:
    """Return region-qualified stack ownership records for this schema."""
    owned = ctx.checkpoint.state.setdefault("owned_stacks", {})
    if not isinstance(owned, dict):
        raise RuntimeError("Checkpoint owned_stacks must be an object")
    for region, records in owned.items():
        if not isinstance(records, dict):
            raise RuntimeError(f"Checkpoint stack ownership for {region} is malformed")
        for stack_name, record in records.items():
            if not isinstance(record, dict):
                raise RuntimeError(
                    f"Checkpoint stack ownership for {region}:{stack_name} is malformed"
                )
    return owned


def _owned_stack_record(
    ctx: RunContext,
    region: str,
    stack_name: str,
) -> dict[str, Any] | None:
    return _owned_stacks(ctx).get(region, {}).get(stack_name)


def _require_prepared_stack_authority(
    record: dict[str, Any],
    *,
    region: str,
    stack_name: str,
) -> None:
    if (
        record.get("authority") != "prepared-change-set"
        or not record.get("change_set_id")
        or record.get("change_set_type") not in {"CREATE", "UPDATE"}
    ):
        raise RuntimeError(
            f"Stack {region}:{stack_name} lacks persisted prepared-change-set authority"
        )


def _prepared_change_set_authority(
    ctx: RunContext,
) -> dict[str, dict[str, dict[str, str]]]:
    """Return validated per-target preparation history, including legacy checkpoints."""
    target_regions = ctx.checkpoint.state.get("target_stack_regions")
    if not isinstance(target_regions, dict):
        raise RuntimeError("Checkpoint target_stack_regions must be an object")

    authority: dict[str, dict[str, dict[str, str]]] = {}
    for stack_name, region_value in target_regions.items():
        region = str(region_value)
        record = _owned_stack_record(ctx, region, stack_name)
        prepared_records: dict[str, dict[str, str]] = {}
        if record is not None:
            _require_prepared_stack_authority(
                record,
                region=region,
                stack_name=stack_name,
            )
            raw_records = record.get("prepared_change_sets", {})
            if not isinstance(raw_records, dict):
                raise RuntimeError(
                    f"Prepared change-set history for {region}:{stack_name} is malformed"
                )
            for change_set_id, raw_prepared in raw_records.items():
                if not isinstance(change_set_id, str) or not isinstance(raw_prepared, dict):
                    raise RuntimeError(
                        f"Prepared change-set history for {region}:{stack_name} is malformed"
                    )
                prepared = {
                    "change_set_id": str(raw_prepared.get("change_set_id") or ""),
                    "stack_id": str(raw_prepared.get("stack_id") or ""),
                    "change_set_type": str(raw_prepared.get("change_set_type") or ""),
                }
                if (
                    prepared["change_set_id"] != change_set_id
                    or prepared["stack_id"] != str(record.get("stack_id") or "")
                    or prepared["change_set_type"] not in {"CREATE", "UPDATE"}
                ):
                    raise RuntimeError(
                        f"Prepared change-set history for {region}:{stack_name} is inconsistent"
                    )
                prepared_records[change_set_id] = prepared

            # Checkpoints written before per-change-set history retained only
            # the latest preparation. Preserve that exact authority on resume.
            legacy_change_set_id = str(record.get("change_set_id") or "")
            if legacy_change_set_id and legacy_change_set_id not in prepared_records:
                prepared_records[legacy_change_set_id] = {
                    "change_set_id": legacy_change_set_id,
                    "stack_id": str(record.get("stack_id") or ""),
                    "change_set_type": str(record.get("change_set_type") or ""),
                }
        authority[stack_name] = prepared_records
    return authority


def _record_prepared_stack_identity(
    ctx: RunContext,
    stack_name: str,
    region: str,
    stack_id: str,
    change_set_id: str,
    change_set_type: str,
) -> None:
    """Persist causal change-set authority before CloudFormation execution."""
    if not stack_id or not change_set_id or change_set_type not in {"CREATE", "UPDATE"}:
        raise RuntimeError(f"Invalid prepared change-set identity for {region}:{stack_name}")
    with ctx.state_lock:
        records = _owned_stacks(ctx).setdefault(region, {})
        previous = records.get(stack_name)
        core = {"name": stack_name, "region": region, "stack_id": stack_id}
        if previous is not None:
            _require_prepared_stack_authority(
                previous,
                region=region,
                stack_name=stack_name,
            )
            if any(previous.get(key) != value for key, value in core.items()):
                raise RuntimeError(
                    f"Prepared stack identity changed for {region}:{stack_name}; refusing adoption"
                )
        previous_prepared = (previous or {}).get("prepared_change_sets", {})
        if not isinstance(previous_prepared, dict):
            raise RuntimeError(
                f"Prepared change-set history for {region}:{stack_name} is malformed"
            )
        prepared_records = copy.deepcopy(previous_prepared)
        if previous is not None:
            legacy_change_set_id = str(previous.get("change_set_id") or "")
            legacy_record = {
                "change_set_id": legacy_change_set_id,
                "stack_id": stack_id,
                "change_set_type": str(previous.get("change_set_type") or ""),
            }
            persisted_legacy = prepared_records.get(legacy_change_set_id)
            if persisted_legacy is not None and persisted_legacy != legacy_record:
                raise RuntimeError(
                    f"Prepared change-set history for {region}:{stack_name} is inconsistent"
                )
            prepared_records[legacy_change_set_id] = legacy_record
        prepared_record = {
            "change_set_id": change_set_id,
            "stack_id": stack_id,
            "change_set_type": change_set_type,
        }
        existing_prepared = prepared_records.get(change_set_id)
        if existing_prepared is not None and existing_prepared != prepared_record:
            raise RuntimeError(
                f"Prepared change-set identity changed for {region}:{stack_name}; refusing adoption"
            )
        prepared_records[change_set_id] = prepared_record
        records[stack_name] = {
            **(previous or {}),
            **core,
            "run_tag": ctx.settings.run_id,
            "authority": "prepared-change-set",
            "change_set_id": change_set_id,
            "change_set_type": change_set_type,
            "prepared_change_sets": prepared_records,
        }
        ctx.persist_callback(ctx.checkpoint)


def _record_stack_identity(
    ctx: RunContext,
    stack_name: str,
    region: str,
    stack: dict[str, Any],
) -> dict[str, Any]:
    stack_id = str(stack.get("stack_id") or "")
    run_tag = str((stack.get("tags") or {}).get(_RUN_STACK_TAG) or "")
    if stack.get("name") != stack_name or not stack_id:
        raise RuntimeError(f"CloudFormation returned an invalid identity for {region}:{stack_name}")
    if run_tag != ctx.settings.run_id:
        raise RuntimeError(
            f"Stack {region}:{stack_name} is not tagged for run {ctx.settings.run_id!r}"
        )

    with ctx.state_lock:
        records = _owned_stacks(ctx).get(region)
        if records is None:
            raise RuntimeError(
                f"Stack {region}:{stack_name} was observed without prepared-change-set authority"
            )
        previous = records.get(stack_name)
        if previous is None:
            raise RuntimeError(
                f"Stack {region}:{stack_name} was observed without prepared-change-set authority"
            )
        _require_prepared_stack_authority(
            previous,
            region=region,
            stack_name=stack_name,
        )
        core = {
            "name": stack_name,
            "region": region,
            "stack_id": stack_id,
            "run_tag": run_tag,
        }
        if any(previous.get(key) != value for key, value in core.items()):
            raise RuntimeError(
                f"Stack identity changed for {region}:{stack_name}; refusing name-based adoption"
            )
        candidate = {**previous, **core}
        records[stack_name] = candidate
        ctx.persist_callback(ctx.checkpoint)
    return candidate


def _reconcile_stack_ownership(ctx: RunContext) -> dict[str, Any]:
    """Verify every live project stack by ARN and exact run tag."""
    target_regions = ctx.checkpoint.state.get("target_stack_regions") or {}
    enabled_regions = ctx.checkpoint.state.get("enabled_regions") or []
    if not target_regions or not enabled_regions:
        raise RuntimeError("Checkpoint lacks target stack Regions or enabled Regions")

    project_stacks = collect_project_stacks(
        ctx.session,
        enabled_regions,
        ctx.config.project_name,
    )
    expected_targets = {
        (str(region), str(stack_name)) for stack_name, region in target_regions.items()
    }
    unexpected = {
        region: [
            item for item in stacks if (str(region), str(item["name"])) not in expected_targets
        ]
        for region, stacks in project_stacks.items()
        if any((str(region), str(item["name"])) not in expected_targets for item in stacks)
    }
    if unexpected:
        raise RuntimeError(
            "Project stacks outside the checkpoint target set were found: "
            + json.dumps(unexpected, sort_keys=True)
        )

    present: dict[str, dict[str, Any]] = {}
    for stack_name, expected_region in target_regions.items():
        region = str(expected_region)
        stack = describe_stack(ctx.session, region, stack_name)
        if stack is None or stack.get("status") == "DELETE_COMPLETE":
            continue
        present.setdefault(region, {})[stack_name] = _record_stack_identity(
            ctx, stack_name, region, stack
        )

    checkpointed = _owned_stacks(ctx)
    for region, records in checkpointed.items():
        for stack_name, record in records.items():
            if target_regions.get(stack_name) != region:
                raise RuntimeError(
                    f"Checkpoint owns unexpected stack identity {region}:{stack_name}"
                )
            if str(record.get("region")) != region:
                raise RuntimeError(f"Checkpoint Region changed for stack {region}:{stack_name}")
    return present


def _authorize_owned_stack(
    ctx: RunContext,
    stack_name: str,
    region: str,
    stack_id: str,
) -> None:
    """Revalidate checkpoint ARN and run tag at a destructive boundary."""
    record = _owned_stack_record(ctx, region, stack_name)
    if record is None:
        raise RuntimeError(f"No checkpointed ownership exists for {region}:{stack_name}")
    _require_prepared_stack_authority(
        record,
        region=region,
        stack_name=stack_name,
    )
    if str(record.get("region")) != region or str(record.get("stack_id")) != stack_id:
        raise RuntimeError(f"Checkpoint identity changed for {region}:{stack_name}")
    live = describe_stack(ctx.session, region, stack_id)
    if live is None:
        raise RuntimeError(f"Checkpointed stack disappeared before authorization: {stack_id}")
    if live.get("name") != stack_name or live.get("stack_id") != stack_id:
        raise RuntimeError(f"CloudFormation identity changed for {region}:{stack_name}")
    if (live.get("tags") or {}).get(_RUN_STACK_TAG) != ctx.settings.run_id:
        raise RuntimeError(f"Run ownership changed for {region}:{stack_name}")


def _resolve_target_stack(
    ctx: RunContext,
    *,
    region: str,
    stack_name: str,
    expected_stack_id: str,
) -> dict[str, Any]:
    """Resolve live/absent/tombstone/replacement state for one exact target."""
    exact = describe_stack(ctx.session, region, expected_stack_id) if expected_stack_id else None
    if exact is not None and exact.get("status") != "DELETE_COMPLETE":
        if exact.get("name") != stack_name or exact.get("stack_id") != expected_stack_id:
            raise RuntimeError(f"Exact stack identity changed for {region}:{stack_name}")
        return {"state": "live", "stack": exact}

    by_name = describe_stack(ctx.session, region, stack_name)
    if by_name is None or by_name.get("status") == "DELETE_COMPLETE":
        return {
            "state": "absent",
            "tombstone": exact if exact and exact.get("status") == "DELETE_COMPLETE" else None,
        }
    actual_id = str(by_name.get("stack_id") or "")
    if expected_stack_id and actual_id != expected_stack_id:
        return {"state": "replacement", "stack": by_name}
    if not expected_stack_id:
        return {"state": "uncheckpointed", "stack": by_name}
    return {"state": "live", "stack": by_name}


def _verify_target_stack_absence(ctx: RunContext) -> dict[str, Any]:
    """Prove every target is absent while surfacing same-name replacements."""
    targets = ctx.checkpoint.state.get("target_stack_regions") or {}
    if not targets:
        raise RuntimeError("Checkpoint lacks target stack Regions for absence verification")
    residual: list[dict[str, Any]] = []
    absent: list[dict[str, str]] = []
    for stack_name, raw_region in targets.items():
        region = str(raw_region)
        record = _owned_stack_record(ctx, region, stack_name)
        expected_id = str((record or {}).get("stack_id") or "")
        resolution = _resolve_target_stack(
            ctx,
            region=region,
            stack_name=stack_name,
            expected_stack_id=expected_id,
        )
        if resolution["state"] == "absent":
            absent.append({"name": stack_name, "region": region, "stack_id": expected_id})
            continue
        stack = resolution["stack"]
        residual.append(
            {
                "name": stack_name,
                "region": region,
                "expected_stack_id": expected_id or None,
                "actual_stack_id": stack.get("stack_id"),
                "status": stack.get("status"),
                "kind": resolution["state"],
            }
        )
    return {"all_absent": not residual, "absent": absent, "residual": residual}


def _merge_expected_ecr_target(
    targets: dict[tuple[str, str, str], dict[str, Any]],
    *,
    region: str,
    repository: str,
    tag: str,
    source: dict[str, str],
) -> None:
    if not region or not repository or not tag:
        raise RuntimeError(f"Invalid expected ECR target: {region}:{repository}:{tag}")
    key = (region, repository, tag)
    target = targets.setdefault(
        key,
        {"region": region, "repository": repository, "tag": tag, "sources": []},
    )
    if source not in target["sources"]:
        target["sources"].append(source)


def _expected_ecr_images(ctx: RunContext, stack_names: list[str]) -> list[dict[str, Any]]:
    """Derive exact CDK-asset and configured mirror tags without AWS writes."""
    targets: dict[tuple[str, str, str], dict[str, Any]] = {}
    assembly = ctx.settings.repo_root / "cdk.out"
    for stack_name in stack_names:
        path = assembly / f"{stack_name}.assets.json"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read cloud assembly assets {path}: {exc}") from exc
        docker_images = document.get("dockerImages") if isinstance(document, dict) else None
        if not isinstance(docker_images, dict):
            raise RuntimeError(f"Cloud assembly {path} omitted dockerImages")
        for asset_id, asset in docker_images.items():
            destinations = asset.get("destinations") if isinstance(asset, dict) else None
            if not isinstance(destinations, dict):
                raise RuntimeError(f"Docker asset {stack_name}:{asset_id} has no destinations")
            for destination in destinations.values():
                if not isinstance(destination, dict):
                    raise RuntimeError(f"Docker asset {stack_name}:{asset_id} is malformed")
                _merge_expected_ecr_target(
                    targets,
                    region=str(destination.get("region") or ""),
                    repository=str(destination.get("repositoryName") or ""),
                    tag=str(destination.get("imageTag") or ""),
                    source={"kind": "cdk-asset", "stack": stack_name, "asset_id": str(asset_id)},
                )

    from cli import _image_mirror

    mirror_config = _image_mirror.read_mirror_config(ctx.settings.repo_root / "cdk.json")
    if mirror_config["enabled"]:
        source_refs = _image_mirror.collect_source_refs()
        for region in ctx.deployment_regions:
            plan = _image_mirror.plan_from_sources(
                source_refs,
                f"validation.invalid.{region}",
                mirror_config["ecr_namespace"],
            )
            for item in plan:
                _merge_expected_ecr_target(
                    targets,
                    region=region,
                    repository=item.dest_repo,
                    tag=item.tag,
                    source={"kind": "configured-mirror", "source_ref": item.source_ref},
                )

    return [
        {
            **target,
            "sources": sorted(target["sources"], key=lambda item: json.dumps(item, sort_keys=True)),
        }
        for _key, target in sorted(targets.items())
    ]


def _ecr_creation_identity(repository: dict[str, Any]) -> dict[str, Any]:
    """Return immutable-enough ECR creation fields for delete authorization."""
    return {
        "name": str(repository.get("name") or ""),
        "arn": str(repository.get("arn") or ""),
        "registry_id": str(repository.get("registry_id") or ""),
        "created_at": repository.get("created_at"),
    }


def _record_ecr_repository_creation(
    ctx: RunContext,
    region: str,
    repository: Mapping[str, Any],
) -> None:
    """Persist the synchronous create_repository acknowledgement before any copy."""
    name = str(repository.get("repositoryName") or "")
    arn = str(repository.get("repositoryArn") or "")
    registry_id = str(repository.get("registryId") or "")
    created_at_raw = repository.get("createdAt")
    if created_at_raw is None:
        created_at = ""
    elif hasattr(created_at_raw, "isoformat"):
        created_at = str(created_at_raw.isoformat())
    else:
        created_at = str(created_at_raw)
    expected = {
        (str(item["region"]), str(item["repository"]))
        for item in ctx.checkpoint.state.get("expected_ecr_images", [])
    }
    baseline_names = {
        (str(baseline_region), str(item["name"]))
        for baseline_region, repositories in (ctx.checkpoint.baseline or {})
        .get("ecr_repositories", {})
        .items()
        for item in repositories
    }
    key = (region, name)
    if key not in expected or key in baseline_names:
        raise RuntimeError(f"Unexpected ECR repository creation acknowledgement: {region}:{name}")
    if not arn or not registry_id or not created_at:
        raise RuntimeError(f"ECR creation acknowledgement is incomplete for {region}:{name}")
    creation_identity = {
        "name": name,
        "arn": arn,
        "registry_id": registry_id,
        "created_at": created_at,
    }
    with ctx.state_lock:
        records = ctx.checkpoint.state.setdefault("created_ecr_repositories", [])
        matches = [
            item for item in records if item.get("region") == region and item.get("name") == name
        ]
        if len(matches) > 1:
            raise RuntimeError(f"Duplicate ECR creation acknowledgements for {region}:{name}")
        candidate = {
            "region": region,
            "name": name,
            "arn": arn,
            "creation_identity": creation_identity,
            "run_tag": ctx.settings.run_id,
            "cleanup_policy": "retain-no-conditional-delete",
        }
        if matches and matches[0] != candidate:
            raise RuntimeError(f"ECR creation acknowledgement changed for {region}:{name}")
        if not matches:
            records.append(candidate)
        ctx.persist_callback(ctx.checkpoint)


def _checkpoint_new_ecr_repositories(ctx: RunContext) -> list[dict[str, Any]]:
    """Reconcile only repositories backed by persisted create acknowledgements."""
    records = ctx.checkpoint.state.get("created_ecr_repositories", [])
    if not records:
        return []
    current = collect_ecr_inventory(
        ctx.session,
        {str(item["region"]) for item in records},
    )
    current_by_key = {
        (region, str(repository["name"])): repository
        for region, repositories in current.items()
        for repository in repositories
    }
    for record in records:
        key = (str(record["region"]), str(record["name"]))
        repository = current_by_key.get(key)
        if repository is None:
            record["observed_absent"] = True
            continue
        if _ecr_creation_identity(repository) != record.get("creation_identity"):
            raise RuntimeError(f"ECR repository identity changed for {key[0]}:{key[1]}")
        if (repository.get("tags") or {}).get(_RUN_STACK_TAG) != record.get("run_tag"):
            raise RuntimeError(f"ECR repository run tag changed for {record['arn']}")
        record["last_observed"] = _ecr_creation_identity(repository)
    ctx.persist()
    return copy.deepcopy(records)


def _ecr_image_identity(image: dict[str, Any]) -> dict[str, Any]:
    return {
        "digest": str(image.get("digest") or ""),
        "manifest_media_type": str(image.get("manifest_media_type") or ""),
        "artifact_media_type": str(image.get("artifact_media_type") or ""),
        "manifest": image.get("manifest"),
    }


def _image_with_tag(repository: dict[str, Any], tag: str) -> dict[str, Any] | None:
    matches = [image for image in repository.get("images", []) if tag in image.get("tags", [])]
    if len(matches) > 1:
        raise RuntimeError(f"ECR tag {repository.get('name')}:{tag} resolved to multiple digests")
    return matches[0] if matches else None


def _checkpoint_new_ecr_images(ctx: RunContext) -> list[dict[str, Any]]:
    """Observe ECR deltas without converting mutable tags into delete authority."""
    baseline = ctx.checkpoint.baseline
    if baseline is None:
        raise RuntimeError("Cannot reconcile ECR images without a baseline")
    baseline_repositories = {
        (region, str(repository["name"])): repository
        for region, repositories in baseline.get("ecr_repositories", {}).items()
        for repository in repositories
    }
    current = collect_ecr_inventory(
        ctx.session,
        baseline.get("ecr_regions") or _topology_regions(ctx),
    )
    current_repositories = {
        (region, str(repository["name"])): repository
        for region, repositories in current.items()
        for repository in repositories
    }

    deltas: list[dict[str, Any]] = []
    for expected in ctx.checkpoint.state.get("expected_ecr_images", []):
        key = (
            str(expected["region"]),
            str(expected["repository"]),
            str(expected["tag"]),
        )
        baseline_repository = baseline_repositories.get(key[:2])
        if baseline_repository is None:
            continue
        current_repository = current_repositories.get(key[:2])
        if current_repository is None:
            raise RuntimeError(f"Baseline ECR repository disappeared: {key[0]}:{key[1]}")
        before = _image_with_tag(baseline_repository, key[2])
        now = _image_with_tag(current_repository, key[2])
        if before is not None:
            if now is None or _ecr_image_identity(now) != _ecr_image_identity(before):
                raise RuntimeError(f"Baseline ECR tag changed during validation: {key}")
            continue
        if now is None:
            continue
        identity = _ecr_image_identity(now)
        if not identity["digest"]:
            raise RuntimeError(f"Expected ECR tag lacks a digest: {key}")
        deltas.append(
            {
                "region": key[0],
                "repository": key[1],
                "tag": key[2],
                "identity": identity,
                "sources": expected.get("sources", []),
                "cleanup_policy": "retain-no-conditional-delete",
            }
        )
    with ctx.state_lock:
        previous = ctx.checkpoint.state.get("retained_ecr_image_deltas")
        if previous is not None and previous != deltas:
            raise RuntimeError("Observed ECR image deltas changed during validation")
        ctx.checkpoint.state["retained_ecr_image_deltas"] = deltas
        ctx.checkpoint.state["owned_ecr_images"] = []
        ctx.persist_callback(ctx.checkpoint)
    return copy.deepcopy(deltas)


def _kms_tags(client: Any, key_id: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    marker: str | None = None
    while True:
        kwargs = {"KeyId": key_id}
        if marker:
            kwargs["Marker"] = marker
        response = client.list_resource_tags(**kwargs)
        tags.update(
            {
                str(tag["TagKey"]): str(tag.get("TagValue") or "")
                for tag in response.get("Tags", [])
                if tag.get("TagKey") is not None
            }
        )
        marker = response.get("NextMarker") if response.get("Truncated") else None
        if not marker:
            return tags


def _checkpoint_retained_kms_keys(ctx: RunContext) -> list[dict[str, Any]]:
    """Capture exact retained EKS keys while their owning stacks still exist."""
    owned_stacks = _owned_stacks(ctx)
    with ctx.state_lock:
        records = ctx.checkpoint.state.setdefault("owned_kms_keys", [])
        by_arn = {str(item["arn"]): item for item in records}

        for region in ctx.deployment_regions:
            stack_name = f"{ctx.config.project_name}-{region}"
            stack_record = owned_stacks.get(region, {}).get(stack_name)
            if stack_record is None:
                continue
            live_stack = describe_stack(ctx.session, region, stack_record["stack_id"])
            if live_stack is None:
                continue
            cfn = ctx.session.client("cloudformation", region_name=region)
            matching_resources: list[dict[str, Any]] = []
            for page in cfn.get_paginator("list_stack_resources").paginate(
                StackName=stack_record["stack_id"]
            ):
                matching_resources.extend(
                    item
                    for item in page.get("StackResourceSummaries", [])
                    if item.get("ResourceType") == "AWS::KMS::Key"
                    and _EKS_KEY_LOGICAL_ID_FRAGMENT in str(item.get("LogicalResourceId") or "")
                    and item.get("PhysicalResourceId")
                )
            if live_stack.get("status") in _HEALTHY_STACK_STATUSES and len(matching_resources) != 1:
                raise RuntimeError(
                    f"Expected one retained EKS KMS key in {stack_name}; found "
                    f"{len(matching_resources)}"
                )

            for resource in matching_resources:
                key_id = str(resource["PhysicalResourceId"])
                kms = ctx.session.client("kms", region_name=region)
                metadata = kms.describe_key(KeyId=key_id).get("KeyMetadata", {})
                arn = str(metadata.get("Arn") or "")
                tags = _kms_tags(kms, key_id)
                if tags.get(_RUN_STACK_TAG) != ctx.settings.run_id:
                    raise RuntimeError(
                        f"KMS key {arn or key_id} lacks the exact live-validation run tag"
                    )
                if tags.get("aws:cloudformation:stack-id") != stack_record["stack_id"]:
                    raise RuntimeError(
                        f"KMS key {arn or key_id} lacks exact CloudFormation stack ownership"
                    )
                candidate = {
                    "region": region,
                    "key_id": key_id,
                    "arn": arn,
                    "stack_name": stack_name,
                    "stack_id": stack_record["stack_id"],
                    "logical_id": str(resource.get("LogicalResourceId") or ""),
                    "run_tag": ctx.settings.run_id,
                    "scheduled": False,
                    "deletion_date": None,
                }
                previous = by_arn.get(arn)
                if previous is not None:
                    for key in (
                        "region",
                        "key_id",
                        "arn",
                        "stack_name",
                        "stack_id",
                        "logical_id",
                        "run_tag",
                    ):
                        if previous.get(key) != candidate[key]:
                            raise RuntimeError(f"KMS ownership changed for {arn}: {key}")
                    continue
                if not arn:
                    raise RuntimeError(f"KMS key {key_id} omitted its ARN")
                records.append(candidate)
                by_arn[arn] = candidate
        ctx.persist_callback(ctx.checkpoint)
        return copy.deepcopy(records)


def action_preflight(ctx: RunContext) -> dict[str, Any]:
    """Validate exact git/AWS/config identity and prove project ownership."""
    settings = ctx.settings
    head = _run_git(settings.repo_root, "rev-parse", "HEAD")
    if head != settings.expected_sha:
        raise RuntimeError(f"HEAD {head} does not match expected SHA {settings.expected_sha}")

    branch = _resolve_branch(settings.repo_root)
    if branch != settings.expected_branch:
        raise RuntimeError(
            f"Current branch {branch!r} does not match expected branch {settings.expected_branch!r}"
        )

    dirty = _run_git(
        settings.repo_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if dirty:
        raise RuntimeError(
            "Live validation requires a clean worktree; commit or remove these paths:\n" + dirty
        )

    identity = ctx.session.client("sts", region_name=ctx.config.global_region).get_caller_identity()
    account = str(identity.get("Account") or "")
    if account != settings.expected_account:
        raise RuntimeError(
            f"AWS caller account {account or 'unknown'} does not match expected "
            f"account {settings.expected_account}"
        )

    _validate_profile(ctx)
    selected = set(ctx.report.selected_actions)
    if "deploy" in selected and not settings.confirm_kms_key_deletion:
        raise RuntimeError(
            "Deployment creates retained EKS encryption keys. Pass "
            "--confirm-kms-key-deletion to explicitly authorize scheduling only "
            "this run's exact keys for deletion during cleanup."
        )
    direct_regional_access = _direct_regional_access_enabled(ctx)
    if (
        len(ctx.deployment_regions) > 1
        and selected.intersection({"api", "sqs", "central-queue"})
        and not direct_regional_access
    ):
        raise RuntimeError(
            "Multi-Region Job actions require api_gateway.regional_api_enabled=true; "
            "the global API cannot prove which same-named regional Job it observed"
        )

    enabled_regions = discover_enabled_regions(ctx.session, ctx.config.global_region)
    target_stacks = ctx.stack_manager.list_stacks()
    if not target_stacks:
        raise RuntimeError("CDK returned no target stacks")
    expected_ecr_images = _expected_ecr_images(ctx, target_stacks)
    unexpected_names = [
        name
        for name in target_stacks
        if not (name == ctx.config.project_name or name.startswith(f"{ctx.config.project_name}-"))
    ]
    if unexpected_names:
        raise RuntimeError(
            "Refusing to own non-project CDK stacks: " + ", ".join(sorted(unexpected_names))
        )

    target_stack_regions = {
        stack_name: ctx.stack_manager._get_destroy_region(stack_name)
        for stack_name in target_stacks
    }
    if any(not region for region in target_stack_regions.values()):
        raise RuntimeError(
            "Could not resolve target stack Regions: "
            + json.dumps(target_stack_regions, sort_keys=True)
        )
    target_region_set = {str(region) for region in target_stack_regions.values()}
    unavailable_targets = sorted(target_region_set - set(enabled_regions))
    if unavailable_targets:
        raise RuntimeError(
            "Target Regions are not enabled for this account: " + ", ".join(unavailable_targets)
        )

    bootstrap_stacks: dict[str, Any] = {}
    for region in sorted(target_region_set):
        bootstrap = describe_stack(ctx.session, region, "CDKToolkit")
        if bootstrap is None or bootstrap.get("status") not in _HEALTHY_STACK_STATUSES:
            status = bootstrap.get("status") if bootstrap else "absent"
            raise RuntimeError(
                f"Region {region} must already contain a healthy CDKToolkit stack; found {status}. "
                "Live validation never auto-bootstraps or mutates the protected baseline."
            )
        bootstrap_stacks[region] = {
            "stack_id": bootstrap["stack_id"],
            "status": bootstrap["status"],
        }

    previous_bootstrap = ctx.checkpoint.state.get("bootstrap_stacks")
    if previous_bootstrap is not None and previous_bootstrap != bootstrap_stacks:
        raise RuntimeError(
            "Checkpointed CDKToolkit ARN/status changed; refusing bootstrap adoption"
        )
    previous_ecr_targets = ctx.checkpoint.state.get("expected_ecr_images")
    if previous_ecr_targets is not None and previous_ecr_targets != expected_ecr_images:
        raise RuntimeError("Cloud-assembly ECR image targets changed since checkpoint creation")

    existing = collect_project_stacks(
        ctx.session,
        enabled_regions,
        ctx.config.project_name,
    )
    if not ctx.checkpoint.deployment_attempted and existing:
        raise RuntimeError(
            "Fresh runs refuse pre-existing project stacks because ownership is unproven: "
            + json.dumps(existing, sort_keys=True)
        )

    previous_targets = ctx.checkpoint.state.get("target_stack_regions")
    if previous_targets is not None and previous_targets != target_stack_regions:
        raise RuntimeError(
            "CDK target stacks changed since the checkpoint was created; refusing resume"
        )

    ctx.checkpoint.state.update(
        {
            "account_arn": str(identity.get("Arn") or ""),
            "enabled_regions": enabled_regions,
            "target_stack_regions": target_stack_regions,
            "topology_regions": list(_topology_regions(ctx)),
            "bootstrap_stacks": bootstrap_stacks,
            "expected_ecr_images": expected_ecr_images,
            "direct_regional_access": direct_regional_access,
            "preexisting_project_stacks": existing
            if not ctx.checkpoint.deployment_attempted
            else ctx.checkpoint.state.get("preexisting_project_stacks", {}),
        }
    )
    ctx.persist()
    if ctx.checkpoint.deployment_attempted:
        _reconcile_stack_ownership(ctx)

    return {
        "account": account,
        "caller_arn": identity.get("Arn"),
        "sha": head,
        "branch": branch,
        "profile": settings.profile,
        "deployment_regions": list(ctx.deployment_regions),
        "topology_regions": list(_topology_regions(ctx)),
        "enabled_regions": enabled_regions,
        "target_stack_regions": target_stack_regions,
        "bootstrap_stacks": bootstrap_stacks,
        "expected_ecr_images": expected_ecr_images,
        "direct_regional_access": direct_regional_access,
        "kms_key_deletion_confirmed": settings.confirm_kms_key_deletion,
        "resume": settings.resume,
    }


def action_baseline(ctx: RunContext) -> dict[str, Any]:
    """Capture protected stacks/ECR and reject non-stack project leftovers."""
    if ctx.checkpoint.baseline is not None:
        return {"reused_checkpoint_baseline": True, **ctx.checkpoint.baseline}

    enabled_regions = ctx.checkpoint.state.get("enabled_regions")
    if not enabled_regions:
        raise RuntimeError("Preflight did not record enabled AWS Regions")
    baseline = capture_baseline(
        ctx.session,
        enabled_regions=enabled_regions,
        ecr_regions=_topology_regions(ctx),
        protected_stack_names=ctx.settings.protected_stack_names,
    )

    project_inventory = collect_project_resources(
        ctx.session,
        enabled_regions=enabled_regions,
        project_name=ctx.config.project_name,
        seed_region=ctx.config.global_region,
    )
    disallowed_inventory = _strip_baseline_ecr(project_inventory, baseline)
    if not project_resources_are_absent(disallowed_inventory):
        raise RuntimeError(
            "Fresh baseline contains project resources not owned by this run: "
            + json.dumps(disallowed_inventory, sort_keys=True)
        )

    ctx.checkpoint.baseline = baseline
    ctx.persist()
    return baseline


def action_deploy(ctx: RunContext) -> dict[str, Any]:
    """Deploy the exact checked-out CDK graph and checkpoint every AWS identity."""
    if ctx.checkpoint.baseline is None:
        raise RuntimeError("A protected-resource baseline is required before deployment")
    ctx.checkpoint.deployment_attempted = True
    ctx.checkpoint.destroyed = False
    ctx.persist()

    events: list[dict[str, Any]] = list(ctx.checkpoint.state.get("deploy_events", []))

    def on_start(stack_name: str) -> None:
        with ctx.state_lock:
            events.append({"stack": stack_name, "event": "started", "at": time.time()})
            ctx.checkpoint.state["deploy_events"] = events
            ctx.persist_callback(ctx.checkpoint)

    def on_complete(stack_name: str, success: bool) -> None:
        with ctx.state_lock:
            events.append(
                {
                    "stack": stack_name,
                    "event": "completed",
                    "success": success,
                    "at": time.time(),
                }
            )
            ctx.checkpoint.state["deploy_events"] = events
            region = str(ctx.checkpoint.state["target_stack_regions"][stack_name])
            stack = describe_stack(ctx.session, region, stack_name)
            if stack is None and success:
                raise RuntimeError(f"CDK reported success but {region}:{stack_name} is absent")
            if stack is not None:
                _record_stack_identity(ctx, stack_name, region, stack)
            ctx.persist_callback(ctx.checkpoint)

    def on_prepared(
        stack_name: str,
        region: str,
        stack_id: str,
        change_set_id: str,
        change_set_type: str,
    ) -> None:
        _record_prepared_stack_identity(
            ctx,
            stack_name,
            region,
            stack_id,
            change_set_id,
            change_set_type,
        )
        prepared_change_sets.setdefault(stack_name, {})[change_set_id] = {
            "change_set_id": change_set_id,
            "stack_id": stack_id,
            "change_set_type": change_set_type,
        }
        expected_stack_ids[stack_name] = stack_id

    def on_repository_created(region: str, repository: Mapping[str, Any]) -> None:
        _record_ecr_repository_creation(ctx, region, repository)

    expected_stack_ids = {
        name: (
            str(record["stack_id"])
            if (record := _owned_stack_record(ctx, str(region), name)) is not None
            else None
        )
        for name, region in ctx.checkpoint.state["target_stack_regions"].items()
    }
    prepared_change_sets = _prepared_change_set_authority(ctx)

    try:
        overall, successful, failed = ctx.stack_manager.deploy_orchestrated(
            require_approval=False,
            tags={_RUN_STACK_TAG: ctx.settings.run_id},
            progress="events",
            on_stack_start=on_start,
            on_stack_complete=on_complete,
            parallel=False,
            max_workers=1,
            allow_bootstrap=False,
            bootstrap_stacks=ctx.checkpoint.state["bootstrap_stacks"],
            expected_stack_ids=expected_stack_ids,
            prepared_change_sets=prepared_change_sets,
            authorize_stack=lambda name, region, stack_id: _authorize_owned_stack(
                ctx,
                name,
                region,
                stack_id,
            ),
            strict_deployment_token=ctx.settings.run_id,
            on_change_set_prepared=on_prepared,
            on_ecr_repository_created=on_repository_created,
        )
    finally:
        _reconcile_stack_ownership(ctx)
        _checkpoint_new_ecr_repositories(ctx)
        _checkpoint_new_ecr_images(ctx)
        _checkpoint_retained_kms_keys(ctx)

    result = {
        "overall_success": overall,
        "successful_stacks": successful,
        "failed_stacks": failed,
        "events": events,
        "owned_stacks": ctx.checkpoint.state.get("owned_stacks", {}),
        "owned_ecr_repositories": ctx.checkpoint.state.get("created_ecr_repositories", []),
        "owned_ecr_images": [],
        "retained_ecr_image_deltas": ctx.checkpoint.state.get("retained_ecr_image_deltas", []),
        "owned_kms_keys": ctx.checkpoint.state.get("owned_kms_keys", []),
    }
    ctx.checkpoint.state["deploy_result"] = result
    ctx.persist()
    if not overall:
        raise RuntimeError(f"Orchestrated deployment failed for: {', '.join(failed) or 'unknown'}")
    return result


def _queue_counts(status: dict[str, Any]) -> dict[str, int]:
    return {
        "available": int(status.get("messages_available", 0)),
        "in_flight": int(status.get("messages_in_flight", 0)),
        "delayed": int(status.get("messages_delayed", 0)),
        "dlq": int(status.get("dlq_messages", 0)),
    }


def action_topology(ctx: RunContext) -> dict[str, Any]:
    """Verify deployed stacks, EKS, endpoints, table, and empty regional queues."""
    _reconcile_stack_ownership(ctx)
    stack_details: dict[str, Any] = {}
    for stack_name, region in ctx.checkpoint.state["target_stack_regions"].items():
        stack = describe_stack(ctx.session, str(region), stack_name)
        if stack is None:
            raise RuntimeError(f"Expected deployed stack is absent: {stack_name} ({region})")
        _record_stack_identity(ctx, stack_name, str(region), stack)
        if stack["status"] not in _HEALTHY_STACK_STATUSES:
            raise RuntimeError(
                f"Stack {stack_name} is {stack['status']}, expected one of "
                f"{sorted(_HEALTHY_STACK_STATUSES)}"
            )
        stack_details[stack_name] = stack

    clusters: dict[str, Any] = {}
    for region in ctx.deployment_regions:
        name = f"{ctx.config.project_name}-{region}"
        cluster = ctx.session.client("eks", region_name=region).describe_cluster(name=name)[
            "cluster"
        ]
        if cluster.get("status") != "ACTIVE":
            raise RuntimeError(f"EKS cluster {name} is not ACTIVE: {cluster.get('status')}")
        clusters[region] = {
            "name": name,
            "arn": cluster.get("arn"),
            "status": cluster.get("status"),
            "version": cluster.get("version"),
            "endpoint_public_access": (cluster.get("resourcesVpcConfig") or {}).get(
                "endpointPublicAccess"
            ),
            "endpoint_private_access": (cluster.get("resourcesVpcConfig") or {}).get(
                "endpointPrivateAccess"
            ),
        }

    global_endpoint = ctx.aws_client.get_api_endpoint(force_refresh=True)
    global_health = ctx.aws_client.call_api(method="GET", path="/api/v1/health")
    regional_endpoints: dict[str, Any] = {}
    if _direct_regional_access_enabled(ctx):
        for region in ctx.deployment_regions:
            endpoint = ctx.aws_client.get_regional_api_endpoint(region, force_refresh=True)
            if endpoint is None:
                raise RuntimeError(f"Direct regional API endpoint is absent in {region}")
            regional_endpoints[region] = {
                "url": endpoint.url,
                "health": ctx.aws_client.call_api(
                    method="GET",
                    path="/api/v1/health",
                    region=region,
                ),
            }
    else:
        regional_endpoints = {
            region: {
                "skipped": True,
                "reason": "direct caller access is disabled by cdk.json",
            }
            for region in ctx.deployment_regions
        }

    queue_baseline: dict[str, Any] = {}
    for region in ctx.deployment_regions:
        status = ctx.job_manager.get_queue_status(region)
        counts = _queue_counts(status)
        if any(counts.values()):
            raise RuntimeError(
                f"Fresh queue in {region} is not empty: {json.dumps(counts, sort_keys=True)}"
            )
        queue_baseline[region] = status

    table_name = f"{ctx.config.project_name}-jobs"
    table = ctx.session.client("dynamodb", region_name=ctx.config.global_region).describe_table(
        TableName=table_name
    )["Table"]
    if table.get("TableStatus") != "ACTIVE":
        raise RuntimeError(f"DynamoDB table {table_name} is not ACTIVE")

    ctx.checkpoint.state["queue_baseline"] = queue_baseline
    ctx.persist()
    return {
        "stacks": stack_details,
        "clusters": clusters,
        "global_api": {"url": global_endpoint.url, "health": global_health},
        "regional_apis": regional_endpoints,
        "queue_baseline": queue_baseline,
        "jobs_table": {
            "name": table_name,
            "arn": table.get("TableArn"),
            "status": table.get("TableStatus"),
        },
    }


def _run_token(run_id: str) -> str:
    token = re.sub(r"[^a-z0-9-]+", "-", run_id.lower()).strip("-")
    token = re.sub(r"-+", "-", token)[:24].rstrip("-")
    if not token:
        raise RuntimeError("run_id does not contain a Kubernetes-safe token")
    return token


def _replace_token(value: Any, token: str) -> Any:
    if isinstance(value, str):
        return value.replace("__RUN_TOKEN__", token)
    if isinstance(value, list):
        return [_replace_token(item, token) for item in value]
    if isinstance(value, dict):
        return {key: _replace_token(item, token) for key, item in value.items()}
    return value


def _load_manifest(ctx: RunContext, filename: str) -> tuple[list[dict[str, Any]], str, str]:
    path = Path(__file__).with_name("manifests") / filename
    manifests = ctx.job_manager.load_manifests(str(path))
    manifests = _replace_token(manifests, _run_token(ctx.settings.run_id))
    job = next(item for item in manifests if item.get("kind") == "Job")
    name = str(job["metadata"]["name"])
    namespace = str(job["metadata"]["namespace"])
    return manifests, name, namespace


def _job_api_path(record: dict[str, Any], suffix: str = "") -> str:
    namespace = quote(str(record["namespace"]), safe="")
    name = quote(str(record["name"]), safe="")
    return f"/api/v1/jobs/{namespace}/{name}{suffix}"


def _response_json(response: Any, operation: str) -> dict[str, Any]:
    try:
        value = response.json()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{operation} returned invalid JSON: {response.text}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{operation} returned a non-object JSON response")
    return value


def _verify_response_region(data: dict[str, Any], expected_region: str, operation: str) -> None:
    actual_region = str(data.get("region") or "")
    if actual_region != expected_region:
        raise RuntimeError(
            f"{operation} came from Region {actual_region or 'unknown'}, expected {expected_region}"
        )


def _get_owned_job(ctx: RunContext, record: dict[str, Any]) -> dict[str, Any] | None:
    """Return a Job only after authoritative HTTP and UID/label verification."""
    response = ctx.aws_client.make_authenticated_request(
        method="GET",
        path=_job_api_path(record),
        target_region=record.get("transport_region"),
    )
    if response.status_code == 404:
        return None
    if not response.ok:
        raise RuntimeError(
            f"Job lookup failed for {record['region']}:{record['namespace']}/{record['name']}: "
            f"{response.status_code} {response.text}"
        )
    data = _response_json(response, "Job lookup")
    _verify_response_region(data, str(record["region"]), "Job lookup")
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        raise RuntimeError("Job lookup omitted metadata")
    if metadata.get("name") != record["name"] or metadata.get("namespace") != record["namespace"]:
        raise RuntimeError("Job lookup returned a different Kubernetes identity")
    labels = metadata.get("labels")
    if not isinstance(labels, dict):
        raise RuntimeError("Job lookup omitted ownership labels")
    if labels.get(_RUN_JOB_LABEL) != record["run_label"]:
        raise RuntimeError("Job run label does not match the checkpoint")
    if labels.get(_PATH_JOB_LABEL) != record["path"]:
        raise RuntimeError("Job validation-path label does not match the checkpoint")
    uid = str(metadata.get("uid") or "")
    if not uid:
        raise RuntimeError("Job lookup omitted metadata.uid")
    ctx.record_job_uid(record, uid)
    return data


def _reactivate_deleted_job_record(ctx: RunContext, record: dict[str, Any]) -> None:
    """Permit a crash-window replay only after authoritative prior absence."""
    if not record.get("deleted"):
        return
    if _get_owned_job(ctx, record) is not None:
        raise RuntimeError("Checkpoint marks a Job deleted but the exact UID still exists")
    with ctx.state_lock:
        previous_uid = record.get("uid")
        if previous_uid:
            record.setdefault("previous_uids", []).append(previous_uid)
        record["uid"] = None
        record["deleted"] = False
        record["submission_state"] = "registered"
        for key in (
            "submission_started_at",
            "submission_reconcile_deadline",
            "submission_acknowledged_at",
            "appearance_deadline",
            "submission",
            "submission_envelope",
            "submission_resumable",
            "submission_blocked_reason",
            "submission_blocked_at",
            "not_submitted_at",
            "validation_evidence",
            "deleted_at",
        ):
            record.pop(key, None)
        ctx.persist_callback(ctx.checkpoint)


def _job_appearance_timeout(ctx: RunContext) -> int:
    return min(ctx.settings.job_timeout_seconds, ctx.settings.queue_timeout_seconds)


def _wait_for_owned_job_appearance(
    ctx: RunContext,
    record: dict[str, Any],
    *,
    raise_on_timeout: bool = True,
) -> dict[str, Any] | None:
    raw_deadline = record.get("appearance_deadline")
    if raw_deadline is None:
        deadline = time.time() + _job_appearance_timeout(ctx)
        with ctx.state_lock:
            record["appearance_deadline"] = deadline
            ctx.persist_callback(ctx.checkpoint)
    else:
        deadline = float(raw_deadline)
    while True:
        job = _get_owned_job(ctx, record)
        if job is not None:
            return job
        if time.time() >= deadline:
            if raise_on_timeout:
                raise TimeoutError(
                    f"Job {record['region']}:{record['namespace']}/{record['name']} "
                    "did not appear before the bounded submission deadline"
                )
            return None
        time.sleep(ctx.settings.poll_interval_seconds)


def _wait_for_ambiguous_job_reconciliation(
    ctx: RunContext,
    record: dict[str, Any],
) -> dict[str, Any] | None:
    """Observe a non-replayable escaped submission until its distinct deadline."""
    raw_deadline = record.get("submission_reconcile_deadline")
    if raw_deadline is None:
        raise RuntimeError("Ambiguous Job submission has no reconciliation deadline")
    deadline = float(raw_deadline)
    while True:
        job = _get_owned_job(ctx, record)
        if job is not None:
            return job
        if time.time() >= deadline:
            return None
        time.sleep(ctx.settings.poll_interval_seconds)


def _job_status(job: dict[str, Any]) -> str:
    status = job.get("status") or {}
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Complete" and condition.get("status") == "True":
            return "succeeded"
        if condition.get("type") == "Failed" and condition.get("status") == "True":
            return "failed"
    return "running" if int(status.get("active") or 0) > 0 else "pending"


def _wait_for_owned_job_terminal(
    ctx: RunContext, record: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + ctx.settings.job_timeout_seconds
    history: list[dict[str, Any]] = []
    while True:
        job = _get_owned_job(ctx, record)
        if job is None:
            raise RuntimeError("An owned Job disappeared before reaching a terminal state")
        status = _job_status(job)
        history.append({"at": time.time(), "status": status})
        if status in {"succeeded", "failed"}:
            return job, history
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Job {record['region']}:{record['namespace']}/{record['name']} "
                f"did not complete within {ctx.settings.job_timeout_seconds}s"
            )
        time.sleep(ctx.settings.poll_interval_seconds)


def _owned_job_logs(ctx: RunContext, record: dict[str, Any], tail: int = 200) -> str:
    if _get_owned_job(ctx, record) is None:
        raise RuntimeError("Owned Job disappeared before its logs were read")
    response = ctx.aws_client.make_authenticated_request(
        method="GET",
        path=_job_api_path(record, f"/logs?tail={tail}"),
        target_region=record.get("transport_region"),
    )
    if not response.ok:
        raise RuntimeError(f"Job log lookup failed: {response.status_code} {response.text}")
    data = _response_json(response, "Job log lookup")
    _verify_response_region(data, str(record["region"]), "Job log lookup")
    if data.get("job_name") != record["name"] or data.get("namespace") != record["namespace"]:
        raise RuntimeError("Job log lookup returned a different Job identity")
    return str(data.get("logs") or "")


def _wait_for_owned_job_absence(ctx: RunContext, record: dict[str, Any]) -> None:
    consecutive_absent = 0
    deadline = time.monotonic() + 180
    while True:
        current = _get_owned_job(ctx, record)
        if current is None:
            consecutive_absent += 1
            if consecutive_absent >= 3:
                return
        else:
            consecutive_absent = 0
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Job {record['region']}:{record['namespace']}/{record['name']} remained visible"
            )
        time.sleep(min(5, ctx.settings.poll_interval_seconds))


def _delete_owned_job(ctx: RunContext, record: dict[str, Any]) -> dict[str, Any]:
    current = _get_owned_job(ctx, record)
    state = str(record.get("submission_state") or "registered")
    if current is None and state == "submitting" and not record.get("uid"):
        current = _wait_for_ambiguous_job_reconciliation(ctx, record)
    elif current is None and state == "submitted" and not record.get("uid"):
        current = _wait_for_owned_job_appearance(ctx, record, raise_on_timeout=False)
    if current is None:
        if record.get("uid"):
            _wait_for_owned_job_absence(ctx, record)
            ctx.mark_job_deleted(record)
            return {"authoritative_absence_after_uid_observation": True}
        if state in {"registered", "prepared", "not_submitted"}:
            ctx.mark_job_not_submitted(record)
            ctx.mark_job_deleted(record)
            return {"not_submitted": True, "already_absent": True}
        raise RuntimeError(
            "Job submission may have escaped but no immutable Kubernetes UID was observed; "
            "cleanup remains unresolved"
        )

    expected_uid = str(record.get("uid") or "")
    if not expected_uid:
        raise RuntimeError("Owned Job has no checkpointed UID at deletion time")
    separator = "&" if "?" in _job_api_path(record) else "?"
    response = ctx.aws_client.make_authenticated_request(
        method="DELETE",
        path=(f"{_job_api_path(record)}{separator}expected_uid={quote(expected_uid, safe='')}"),
        target_region=record.get("transport_region"),
    )
    if response.status_code == 404:
        deletion: dict[str, Any] = {"authoritative_404_after_uid_observation": True}
    elif response.status_code == 409:
        raise RuntimeError("Job UID changed before deletion; Kubernetes precondition rejected it")
    elif response.ok:
        deletion = _response_json(response, "Job deletion")
        _verify_response_region(deletion, str(record["region"]), "Job deletion")
        response_uid = deletion.get("uid")
        if response_uid is not None and str(response_uid) != expected_uid:
            raise RuntimeError("Job deletion response UID did not match the checkpoint")
    else:
        raise RuntimeError(f"Job deletion failed: {response.status_code} {response.text}")
    _wait_for_owned_job_absence(ctx, record)
    ctx.mark_job_deleted(record)
    return deletion


def _complete_job_lifecycle(
    ctx: RunContext,
    *,
    record: dict[str, Any],
    marker: str,
) -> dict[str, Any]:
    appeared = _wait_for_owned_job_appearance(ctx, record)
    if appeared is None:
        raise RuntimeError(
            f"Job {record['namespace']}/{record['name']} never appeared in {record['region']}"
        )
    final, history = _wait_for_owned_job_terminal(ctx, record)
    status = _job_status(final)
    if status != "succeeded":
        raise RuntimeError(
            f"Job {record['namespace']}/{record['name']} in {record['region']} "
            f"finished with status {status}"
        )
    logs = _owned_job_logs(ctx, record)
    if marker not in logs:
        raise RuntimeError(f"Job logs did not contain expected marker {marker!r}")
    evidence = {
        "name": record["name"],
        "namespace": record["namespace"],
        "region": record["region"],
        "transport_region": record.get("transport_region"),
        "uid": record.get("uid"),
        "status": status,
        "status_history": history,
        "marker": marker,
        "appearance": {
            "region": appeared.get("region"),
            "uid": (appeared.get("metadata") or {}).get("uid"),
        },
    }
    with ctx.state_lock:
        record["validation_evidence"] = copy.deepcopy(evidence)
        ctx.persist_callback(ctx.checkpoint)
    deletion = _delete_owned_job(ctx, record)
    return {**evidence, "deletion": deletion}


def _register_job(
    ctx: RunContext,
    *,
    name: str,
    namespace: str,
    execution_region: str,
    path: str,
    reactivate_deleted: bool = True,
) -> dict[str, Any]:
    record = ctx.register_job(
        name=name,
        namespace=namespace,
        region=execution_region,
        path=path,
        run_label=_run_token(ctx.settings.run_id),
        transport_region=_job_transport_region(ctx, execution_region),
    )
    if reactivate_deleted:
        _reactivate_deleted_job_record(ctx, record)
    return record


def action_api_lifecycle(ctx: RunContext) -> dict[str, Any]:
    """Submit, observe, read logs, and delete an authenticated API Job."""
    manifests, name, namespace = _load_manifest(ctx, "api-smoke-job.yaml")
    token = _run_token(ctx.settings.run_id)
    marker = f"GCO_LIVE_API_{token}"
    execution_region = ctx.deployment_regions[0]
    record = _register_job(
        ctx,
        name=name,
        namespace=namespace,
        execution_region=execution_region,
        path="api",
    )
    envelope = {
        "transport": "api",
        "manifests": manifests,
        "namespace": namespace,
        "execution_region": execution_region,
        "transport_region": record.get("transport_region"),
        "labels": {_RUN_JOB_LABEL: token},
    }
    ctx.prepare_job_submission(record, envelope=envelope, resumable=False)

    existing = _get_owned_job(ctx, record)
    submission: dict[str, Any] | None = None
    state = str(record.get("submission_state") or "")
    if existing is None and state == "submitting":
        existing = _wait_for_ambiguous_job_reconciliation(ctx, record)
        if existing is None:
            reason = (
                "API submission crossed a non-idempotent boundary but no Job appeared; "
                "automatic replay is forbidden"
            )
            ctx.block_job_submission(record, reason)
            raise RuntimeError(reason)
    elif existing is None and state == "submitted":
        existing = _wait_for_owned_job_appearance(ctx, record)
    elif existing is None and state == "blocked":
        raise RuntimeError(str(record.get("submission_blocked_reason") or "API submission blocked"))

    if existing is None:
        if state != "prepared":
            raise RuntimeError(f"Cannot submit API Job from state {state!r}")
        ctx.begin_job_submission(
            record,
            reconciliation_timeout_seconds=_job_appearance_timeout(ctx),
        )
        submission = ctx.job_manager.submit_job(
            manifests,
            namespace=namespace,
            target_region=record.get("transport_region"),
            labels={_RUN_JOB_LABEL: token},
        )
        submitted_name, submitted_namespace = resolve_submission_identity(
            submission,
            fallback_name=name,
            fallback_namespace=namespace,
        )
        if submitted_name != name or submitted_namespace != namespace:
            raise RuntimeError(
                "API submission identity mismatch: "
                f"expected {namespace}/{name}, got {submitted_namespace}/{submitted_name}"
            )
        response_region = submission.get("region")
        if response_region is not None and str(response_region) != execution_region:
            raise RuntimeError(
                f"API submission executed in {response_region}, expected {execution_region}"
            )
        ctx.finish_job_submission(
            record,
            submission,
            appearance_timeout_seconds=_job_appearance_timeout(ctx),
        )

    lifecycle = _complete_job_lifecycle(ctx, record=record, marker=marker)
    lifecycle["submission"] = submission or {"reconciled_existing_job": True}
    return lifecycle


def action_sqs_lifecycle(ctx: RunContext) -> dict[str, Any]:
    """Submit, observe, read logs, and delete a direct regional-SQS Job."""
    manifests, name, namespace = _load_manifest(ctx, "sqs-smoke-job.yaml")
    token = _run_token(ctx.settings.run_id)
    marker = f"GCO_LIVE_SQS_{token}"
    region = ctx.deployment_regions[0]
    record = _register_job(
        ctx,
        name=name,
        namespace=namespace,
        execution_region=region,
        path="sqs",
    )
    envelope = {
        "transport": "direct-sqs",
        "manifests": manifests,
        "region": region,
        "namespace": namespace,
        "labels": {_RUN_JOB_LABEL: token},
        "priority": 100,
    }
    ctx.prepare_job_submission(record, envelope=envelope, resumable=False)

    existing = _get_owned_job(ctx, record)
    state = str(record.get("submission_state") or "")
    if existing is None and state == "submitting":
        existing = _wait_for_ambiguous_job_reconciliation(ctx, record)
        if existing is None:
            reason = (
                "Direct SQS submission crossed a non-idempotent boundary but no Job appeared; "
                "automatic replay is forbidden"
            )
            ctx.block_job_submission(record, reason)
            raise RuntimeError(reason)
    elif existing is None and state == "submitted":
        existing = _wait_for_owned_job_appearance(ctx, record)
    elif existing is None and state == "blocked":
        raise RuntimeError(str(record.get("submission_blocked_reason") or "SQS submission blocked"))

    if existing is None:
        if state != "prepared":
            raise RuntimeError(f"Cannot submit SQS Job from state {state!r}")
        ctx.begin_job_submission(
            record,
            reconciliation_timeout_seconds=_job_appearance_timeout(ctx),
        )
        submission = ctx.job_manager.submit_job_sqs(
            manifests,
            region=region,
            namespace=namespace,
            labels={_RUN_JOB_LABEL: token},
            priority=100,
        )
        if submission.get("job_name") != name:
            raise RuntimeError(
                f"SQS submission returned unexpected job name: {submission.get('job_name')}"
            )
        ctx.finish_job_submission(
            record,
            submission,
            appearance_timeout_seconds=_job_appearance_timeout(ctx),
        )
        ctx.checkpoint.state["sqs_submission"] = submission
        ctx.persist()
    else:
        submission = {"reconciled_existing_job": True}

    lifecycle = _complete_job_lifecycle(ctx, record=record, marker=marker)
    lifecycle["submission"] = submission
    return lifecycle


def _central_manifest(ctx: RunContext) -> tuple[dict[str, Any], str, str, str]:
    manifests, _name, namespace = _load_manifest(ctx, "api-smoke-job.yaml")
    manifest = copy.deepcopy(manifests[0])
    token = _run_token(ctx.settings.run_id)
    name = f"gco-live-ddb-{token}"[:63].rstrip("-")
    marker = f"GCO_LIVE_DDB_{token}"
    manifest["metadata"]["name"] = name
    manifest["metadata"]["labels"][_PATH_JOB_LABEL] = "dynamodb"
    manifest["spec"]["template"]["metadata"]["labels"][_PATH_JOB_LABEL] = "dynamodb"
    manifest["spec"]["template"]["spec"]["containers"][0]["command"] = [
        "sh",
        "-c",
        f"echo {marker}",
    ]
    return manifest, name, namespace, marker


def _deserialize_item(item: dict[str, Any]) -> dict[str, Any]:
    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in item.items()}


def _read_central_job_item(ctx: RunContext, job_id: str) -> dict[str, Any]:
    table_name = f"{ctx.config.project_name}-jobs"
    response = ctx.session.client("dynamodb", region_name=ctx.config.global_region).get_item(
        TableName=table_name,
        Key={"job_id": {"S": job_id}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not item:
        raise RuntimeError(f"DynamoDB item {job_id} was not found in {table_name}")
    return _deserialize_item(item)


def _central_queue_job_id(idempotency_key: str) -> str:
    return str(uuid.uuid5(_CENTRAL_QUEUE_IDEMPOTENCY_NAMESPACE, idempotency_key))


def _register_central_job(
    ctx: RunContext,
    *,
    job_id: str,
    idempotency_key: str,
    record: dict[str, Any],
    marker: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    candidate = {
        "job_id": job_id,
        "idempotency_key": idempotency_key,
        "job_name": record["name"],
        "namespace": record["namespace"],
        "target_region": record["region"],
        "transport_region": record.get("transport_region"),
        "marker": marker,
        "body": copy.deepcopy(body),
    }
    with ctx.state_lock:
        raw_central_jobs = ctx.checkpoint.state.setdefault("central_jobs", [])
        if not isinstance(raw_central_jobs, list) or any(
            not isinstance(item, dict) for item in raw_central_jobs
        ):
            raise RuntimeError("Checkpoint central_jobs must be a list of objects")
        central_jobs = cast(list[dict[str, Any]], raw_central_jobs)
        matches = [
            item
            for item in central_jobs
            if item.get("job_id") == job_id or item.get("idempotency_key") == idempotency_key
        ]
        if len(matches) > 1:
            raise RuntimeError(f"Checkpoint contains duplicate central Job records for {job_id}")
        if matches:
            central_record = matches[0]
            for key, value in candidate.items():
                if central_record.get(key) != value:
                    raise RuntimeError(f"Central Job identity changed for {job_id}: {key}")
        else:
            central_record = {
                **candidate,
                "submission_state": str(record.get("submission_state") or "prepared"),
                "appearance_deadline": record.get("appearance_deadline"),
                "cleanup_complete": False,
            }
            central_jobs.append(central_record)
        if central_record.get("appearance_deadline") is None:
            central_record["appearance_deadline"] = record.get("appearance_deadline")
        ctx.persist_callback(ctx.checkpoint)
        return central_record


def _central_workload_record(
    ctx: RunContext,
    central_record: dict[str, Any],
) -> dict[str, Any]:
    raw_jobs = ctx.checkpoint.state.get("jobs", [])
    if not isinstance(raw_jobs, list) or any(not isinstance(item, dict) for item in raw_jobs):
        raise RuntimeError("Checkpoint jobs must be a list of objects")
    jobs = cast(list[dict[str, Any]], raw_jobs)
    matches = [
        record
        for record in jobs
        if record.get("path") == "dynamodb"
        and record.get("name") == central_record.get("job_name")
        and record.get("namespace") == central_record.get("namespace")
        and record.get("region") == central_record.get("target_region")
        and record.get("transport_region") == central_record.get("transport_region")
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Central queue record does not resolve to exactly one checkpointed workload: "
            f"{central_record.get('job_id')}"
        )
    return matches[0]


def _validate_central_job_identity(central_record: dict[str, Any], job: dict[str, Any]) -> None:
    expected = {
        "job_id": central_record["job_id"],
        "job_name": central_record["job_name"],
        "namespace": central_record["namespace"],
        "target_region": central_record["target_region"],
    }
    for key, value in expected.items():
        if str(job.get(key) or "") != str(value):
            raise RuntimeError(f"Central queue returned a different {key} for {value!r}")
    observed_key = job.get("idempotency_key")
    if observed_key is not None and observed_key != central_record["idempotency_key"]:
        raise RuntimeError("Central queue idempotency key changed")


def _get_central_queue_job(
    ctx: RunContext, central_record: dict[str, Any]
) -> dict[str, Any] | None:
    response = ctx.aws_client.make_authenticated_request(
        method="GET",
        path=f"/api/v1/queue/jobs/{quote(str(central_record['job_id']), safe='')}",
        target_region=central_record.get("transport_region"),
    )
    if response.status_code == 404:
        return None
    if not response.ok:
        raise RuntimeError(f"Central queue lookup failed: {response.status_code} {response.text}")
    data = _response_json(response, "Central queue lookup")
    job = data.get("job")
    if not isinstance(job, dict):
        raise RuntimeError("Central queue lookup omitted job")
    _validate_central_job_identity(central_record, job)
    return job


def _wait_for_central_queue_appearance(
    ctx: RunContext,
    central_record: dict[str, Any],
    *,
    raise_on_timeout: bool = True,
) -> dict[str, Any] | None:
    raw_deadline = central_record.get("appearance_deadline")
    if raw_deadline is None:
        deadline = time.time() + _job_appearance_timeout(ctx)
        central_record["appearance_deadline"] = deadline
        ctx.persist()
    else:
        deadline = float(raw_deadline)
    while True:
        job = _get_central_queue_job(ctx, central_record)
        if job is not None:
            return job
        if time.time() >= deadline:
            if raise_on_timeout:
                raise TimeoutError(
                    f"Central queue job {central_record['job_id']} did not appear before "
                    "the bounded submission deadline"
                )
            return None
        time.sleep(ctx.settings.poll_interval_seconds)


def _wait_for_central_queue_terminal(
    ctx: RunContext,
    central_record: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + ctx.settings.job_timeout_seconds
    history: list[dict[str, Any]] = []
    while True:
        job = _get_central_queue_job(ctx, central_record)
        if job is None:
            raise RuntimeError(
                f"Central queue job {central_record['job_id']} disappeared after observation"
            )
        status = str(job.get("status") or "unknown")
        history.append({"status": status, "at": time.time()})
        if status in _TERMINAL_QUEUE_STATUSES:
            return job, history
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Central queue job {central_record['job_id']} did not reach a terminal status"
            )
        time.sleep(ctx.settings.poll_interval_seconds)


def action_central_queue_lifecycle(ctx: RunContext) -> dict[str, Any]:
    """Exercise the idempotent DynamoDB queue and require terminal persistence."""
    manifest, name, namespace, marker = _central_manifest(ctx)
    target_region = ctx.deployment_regions[0]
    record = _register_job(
        ctx,
        name=name,
        namespace=namespace,
        execution_region=target_region,
        path="dynamodb",
        reactivate_deleted=False,
    )
    transport_region = record.get("transport_region")
    idempotency_key = f"gco-live-validation:{ctx.settings.run_id}:central"
    job_id = _central_queue_job_id(idempotency_key)
    body = {
        "manifest": manifest,
        "target_region": target_region,
        "namespace": namespace,
        "priority": 100,
        "labels": {_RUN_JOB_LABEL: _run_token(ctx.settings.run_id)},
    }

    envelope = {
        "transport": "central-queue",
        "body": body,
        "idempotency_key": idempotency_key,
        "job_id": job_id,
        "transport_region": transport_region,
    }
    ctx.prepare_job_submission(record, envelope=envelope, resumable=True)

    central_record = _register_central_job(
        ctx,
        job_id=job_id,
        idempotency_key=idempotency_key,
        record=record,
        marker=marker,
        body=body,
    )
    initial_state = str(record.get("submission_state") or "")
    queue_job = _get_central_queue_job(ctx, central_record)
    submission: dict[str, Any]
    if queue_job is None and initial_state in {"prepared", "submitting", "submitted"}:
        if initial_state in {"prepared", "submitting"}:
            ctx.begin_job_submission(
                record,
                reconciliation_timeout_seconds=_job_appearance_timeout(ctx),
            )
        persisted_envelope = record.get("submission_envelope")
        if not isinstance(persisted_envelope, dict) or persisted_envelope != envelope:
            raise RuntimeError("Central queue replay envelope changed")
        response = ctx.aws_client.make_authenticated_request(
            method="POST",
            path="/api/v1/queue/jobs",
            body=copy.deepcopy(persisted_envelope["body"]),
            headers={"Idempotency-Key": str(persisted_envelope["idempotency_key"])},
            target_region=persisted_envelope.get("transport_region"),
        )
        if response.status_code == 409:
            raise RuntimeError(
                "Central queue rejected the exact idempotent replay because request drift was detected"
            )
        if response.status_code not in {200, 201}:
            raise RuntimeError(
                f"Central queue submission failed: {response.status_code} {response.text}"
            )
        submission = _response_json(response, "Central queue submission")
        queued_job = submission.get("job")
        if not isinstance(queued_job, dict):
            raise RuntimeError("Central queue response omitted job")
        _validate_central_job_identity(central_record, queued_job)
        ctx.finish_job_submission(
            record,
            submission,
            appearance_timeout_seconds=_job_appearance_timeout(ctx),
        )
        central_record["submission"] = submission
        central_record["submission_state"] = "submitted"
        central_record["appearance_deadline"] = record["appearance_deadline"]
        ctx.persist()
    elif queue_job is not None:
        submission = (
            record.get("submission")
            or central_record.get("submission")
            or {"reconciled_existing_job": True, "job": queue_job}
        )
        if not isinstance(submission, dict):
            raise RuntimeError("Checkpointed central queue submission is malformed")
        submitted_job = submission.get("job")
        if isinstance(submitted_job, dict):
            _validate_central_job_identity(central_record, submitted_job)
        if initial_state in {"submitting", "submitted", "appeared"}:
            ctx.finish_job_submission(
                record,
                submission,
                appearance_timeout_seconds=_job_appearance_timeout(ctx),
            )
        central_record["submission"] = submission
        central_record["submission_state"] = "reconciled"
        central_record["appearance_deadline"] = record.get("appearance_deadline")
        ctx.persist()
    else:
        raise RuntimeError(
            f"Central queue record is absent and state {initial_state!r} is not replayable"
        )

    _wait_for_central_queue_appearance(ctx, central_record)
    final_job, history = _wait_for_central_queue_terminal(ctx, central_record)
    central_record["status"] = str(final_job.get("status") or "unknown")
    central_record["status_history"] = history
    ctx.persist()
    if final_job.get("status") != "succeeded":
        raise RuntimeError(
            f"Central queue job {job_id} finished as {final_job.get('status')}: "
            f"{final_job.get('error_message') or 'no error message'}"
        )

    item = _read_central_job_item(ctx, job_id)
    if item.get("status") != "succeeded":
        raise RuntimeError(f"DynamoDB record {job_id} is {item.get('status')}, expected succeeded")
    if record.get("deleted"):
        evidence = record.get("validation_evidence")
        if not isinstance(evidence, dict) or evidence.get("marker") != marker:
            raise RuntimeError(
                "Central Job was deleted without checkpointed live-validation evidence; "
                "refusing an idempotency replay"
            )
        workload_lifecycle = {
            **copy.deepcopy(evidence),
            "deletion": {"reconciled_checkpointed_deletion": True},
        }
    else:
        workload_lifecycle = _complete_job_lifecycle(ctx, record=record, marker=marker)

    central_record["status"] = "succeeded"
    central_record["cleanup_complete"] = True
    central_record["workload_lifecycle"] = workload_lifecycle
    ctx.persist()
    return {
        "submission": submission,
        "job_id": job_id,
        "job_name": name,
        "namespace": namespace,
        "target_region": target_region,
        "status_history": history,
        "final_job": final_job,
        "dynamodb_item": item,
        "workload_lifecycle": workload_lifecycle,
    }


def action_convergence(ctx: RunContext) -> dict[str, Any]:
    """Require stable empty SQS/DLQ counters and terminal DynamoDB records."""
    baseline = ctx.checkpoint.state.get("queue_baseline")
    if not baseline:
        raise RuntimeError("Topology action did not record queue baselines")

    deadline = time.monotonic() + ctx.settings.queue_timeout_seconds
    stable_observations = 0
    samples: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        sample = {
            region: ctx.job_manager.get_queue_status(region) for region in ctx.deployment_regions
        }
        counts = {region: _queue_counts(status) for region, status in sample.items()}
        samples.append({"at": time.time(), "counts": counts})
        expected_dlq = {region: _queue_counts(status)["dlq"] for region, status in baseline.items()}
        converged = all(
            values["available"] == 0
            and values["in_flight"] == 0
            and values["delayed"] == 0
            and values["dlq"] == expected_dlq.get(region, 0)
            for region, values in counts.items()
        )
        stable_observations = stable_observations + 1 if converged else 0
        if stable_observations >= 3:
            break
        time.sleep(ctx.settings.poll_interval_seconds)
    if stable_observations < 3:
        raise TimeoutError(
            "Regional SQS/DLQ counters did not converge for three observations: "
            + json.dumps(samples[-5:], sort_keys=True)
        )

    dynamodb_records: dict[str, Any] = {}
    for central_job in ctx.checkpoint.state.get("central_jobs", []):
        job_id = str(central_job["job_id"])
        item = _read_central_job_item(ctx, job_id)
        if item.get("status") != "succeeded":
            raise RuntimeError(f"DynamoDB record {job_id} regressed to {item.get('status')}")
        dynamodb_records[job_id] = item
    return {
        "stable_observations": stable_observations,
        "queue_samples": samples[-10:],
        "dynamodb_records": dynamodb_records,
    }


def _cleanup_central_job(ctx: RunContext, central_job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(central_job["job_id"])
    current = _wait_for_central_queue_appearance(
        ctx,
        central_job,
        raise_on_timeout=False,
    )
    if current is None:
        outcome = {
            "job_id": job_id,
            "complete": False,
            "unresolved": "no consistently readable queue record before the bounded deadline",
        }
        central_job["cleanup_complete"] = False
        central_job["cleanup_result"] = outcome
        ctx.persist()
        raise RuntimeError(
            f"Central queue job {job_id} was not observed; non-observation is not terminal proof"
        )

    status = str(current.get("status") or "unknown")
    previous_cancellation = central_job.get("cancellation")
    cancellation: dict[str, Any] = (
        copy.deepcopy(previous_cancellation)
        if isinstance(previous_cancellation, dict)
        else {"not_required": status in _TERMINAL_QUEUE_STATUSES}
    )
    if status not in _TERMINAL_QUEUE_STATUSES:
        reason = quote("live release validation cleanup", safe="")
        response = ctx.aws_client.make_authenticated_request(
            method="DELETE",
            path=f"/api/v1/queue/jobs/{quote(job_id, safe='')}?reason={reason}",
            target_region=central_job.get("transport_region"),
        )
        if response.status_code == 404:
            raise RuntimeError(f"Central queue job {job_id} disappeared during cancellation")
        if response.status_code == 409:
            cancellation = {
                "not_cancellable": True,
                "status_code": 409,
                "detail": response.text,
            }
        elif response.ok:
            cancellation = {
                "accepted_before_claim": True,
                "response": _response_json(response, "Central queue cancellation"),
            }
        else:
            raise RuntimeError(f"{response.status_code} {response.text}")
        central_job["cancel_attempted"] = True
        central_job["cancellation"] = cancellation
        ctx.persist()
        current, history = _wait_for_central_queue_terminal(ctx, central_job)
    else:
        history = [{"status": status, "at": time.time()}]

    terminal_status = str(current.get("status") or "unknown")
    if terminal_status not in _TERMINAL_QUEUE_STATUSES:
        raise RuntimeError(f"Central queue job {job_id} did not become terminal")
    persisted = _read_central_job_item(ctx, job_id)
    _validate_central_job_identity(central_job, persisted)
    persisted_status = str(persisted.get("status") or "unknown")
    if persisted_status != terminal_status or persisted_status not in _TERMINAL_QUEUE_STATUSES:
        raise RuntimeError(
            f"Central queue job {job_id} lacks consistent terminal DynamoDB evidence"
        )

    workload_not_submitted = False
    # The queue state machine permits ``cancelled`` only from ``queued``;
    # consistent terminal DynamoDB evidence therefore proves no worker ever
    # claimed the manifest even if the cancellation HTTP response was lost.
    if terminal_status == "cancelled":
        workload_record = _central_workload_record(ctx, central_job)
        if not workload_record.get("uid"):
            ctx.mark_central_job_cancelled_before_claim(
                workload_record,
                job_id=job_id,
            )
            workload_not_submitted = True

    outcome = {
        "job_id": job_id,
        "complete": True,
        "cancellation": cancellation,
        "terminal_status": terminal_status,
        "status_history": history,
        "consistent_record": persisted,
        "workload_not_submitted": workload_not_submitted,
    }
    central_job["status"] = terminal_status
    central_job["cleanup_complete"] = True
    central_job["cleanup_result"] = outcome
    ctx.persist()
    return outcome


def cleanup_workloads(ctx: RunContext) -> dict[str, Any]:
    """Reconcile every workload and return an explicit teardown barrier result."""
    result: dict[str, Any] = {
        "started_at": utc_now(),
        "complete": False,
        "jobs": [],
        "central_jobs": [],
        "errors": [],
        "unresolved": [],
    }
    for central_job in ctx.checkpoint.state.get("central_jobs", []):
        if central_job.get("cleanup_complete"):
            result["central_jobs"].append(copy.deepcopy(central_job.get("cleanup_result") or {}))
            continue
        job_id = str(central_job["job_id"])
        try:
            result["central_jobs"].append(_cleanup_central_job(ctx, central_job))
        except Exception as exc:  # noqa: BLE001 - preserve every unresolved resource
            error = f"{type(exc).__name__}: {exc}"
            result["errors"].append({"resource": f"central:{job_id}", "error": error})
            result["unresolved"].append({"resource": f"central:{job_id}", "reason": error})

    for record in ctx.checkpoint.state.get("jobs", []):
        if record.get("deleted"):
            continue
        reference = f"{record['region']}:{record['namespace']}/{record['name']}"
        try:
            deletion = _delete_owned_job(ctx, record)
            result["jobs"].append(
                {
                    "region": record["region"],
                    "namespace": record["namespace"],
                    "name": record["name"],
                    "uid": record.get("uid"),
                    "deletion": deletion,
                }
            )
        except Exception as exc:  # noqa: BLE001 - preserve every unresolved resource
            error = f"{type(exc).__name__}: {exc}"
            result["errors"].append({"resource": reference, "error": error})
            result["unresolved"].append({"resource": reference, "reason": error})

    for central_job in ctx.checkpoint.state.get("central_jobs", []):
        if not central_job.get("cleanup_complete"):
            reference = f"central:{central_job['job_id']}"
            if not any(item["resource"] == reference for item in result["unresolved"]):
                result["unresolved"].append(
                    {"resource": reference, "reason": "terminal queue evidence is incomplete"}
                )
    for record in ctx.checkpoint.state.get("jobs", []):
        if not record.get("deleted"):
            reference = f"{record['region']}:{record['namespace']}/{record['name']}"
            if not any(item["resource"] == reference for item in result["unresolved"]):
                result["unresolved"].append(
                    {"resource": reference, "reason": "UID-bound Job absence is incomplete"}
                )

    result["complete"] = not result["errors"] and not result["unresolved"]
    result["ended_at"] = utc_now()
    ctx.checkpoint.state.setdefault("workload_cleanup_attempts", []).append(result)
    ctx.persist()
    return result


def _cleanup_new_ecr_images(ctx: RunContext) -> dict[str, Any]:
    """Retain mutable baseline-repository tag deltas that cannot be conditionally deleted."""
    retained: list[dict[str, Any]] = []
    for record in ctx.checkpoint.state.get("retained_ecr_image_deltas", []):
        reference = f"{record['region']}:{record['repository']}:{record['tag']}"
        current = describe_ecr_image_by_tag(
            ctx.session,
            region=str(record["region"]),
            repository_name=str(record["repository"]),
            tag=str(record["tag"]),
        )
        if current is None:
            retained.append({"image": reference, "already_absent": True})
            continue
        if _ecr_image_identity(current) != record.get("identity"):
            raise RuntimeError(f"Observed ECR image identity changed for {reference}")
        retained.append(
            {
                "image": reference,
                "digest": current["digest"],
                "retained": True,
                "reason": "ECR has no conditional tag deletion primitive",
            }
        )
    return {"images": retained, "automatic_deletion": False}


def _cleanup_new_ecr_repositories(ctx: RunContext) -> dict[str, Any]:
    """Retain acknowledged repositories because ECR deletion is not conditional."""
    results: list[dict[str, Any]] = []
    records = ctx.checkpoint.state.get("created_ecr_repositories", [])
    current_by_region = (
        collect_ecr_inventory(ctx.session, {str(item["region"]) for item in records})
        if records
        else {}
    )
    for record in records:
        region = str(record["region"])
        current = next(
            (
                item
                for item in current_by_region.get(region, [])
                if item.get("name") == record["name"]
            ),
            None,
        )
        if current is None:
            results.append({"arn": record["arn"], "already_absent": True})
            continue
        if _ecr_creation_identity(current) != record.get("creation_identity"):
            raise RuntimeError(
                f"ECR repository creation identity changed for {region}:{record['name']}"
            )
        if (current.get("tags") or {}).get(_RUN_STACK_TAG) != record.get("run_tag"):
            raise RuntimeError(f"ECR run ownership changed for {record['arn']}")
        results.append(
            {
                "arn": record["arn"],
                "retained": True,
                "reason": "ECR has no conditional repository deletion primitive",
            }
        )
    return {"repositories": results, "automatic_deletion": False}


def _schedule_retained_kms_keys(ctx: RunContext) -> dict[str, Any]:
    records = ctx.checkpoint.state.get("owned_kms_keys", [])
    if records and not ctx.settings.confirm_kms_key_deletion:
        raise RuntimeError("Retained KMS keys exist but this identity did not confirm key deletion")
    results: list[dict[str, Any]] = []
    for record in records:
        region = str(record["region"])
        kms = ctx.session.client("kms", region_name=region)
        try:
            metadata = kms.describe_key(KeyId=record["key_id"]).get("KeyMetadata", {})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "NotFoundException":
                record["scheduled"] = True
                record["deleted"] = True
                results.append({"arn": record["arn"], "already_absent": True})
                ctx.persist()
                continue
            raise
        if metadata.get("Arn") != record["arn"]:
            raise RuntimeError(f"KMS key ARN changed for {record['key_id']}")
        tags = _kms_tags(kms, str(record["key_id"]))
        if tags.get(_RUN_STACK_TAG) != record["run_tag"]:
            raise RuntimeError(f"KMS run ownership changed for {record['arn']}")
        if tags.get("aws:cloudformation:stack-id") != record["stack_id"]:
            raise RuntimeError(f"KMS stack ownership changed for {record['arn']}")

        state = str(metadata.get("KeyState") or "")
        if state != "PendingDeletion":
            if state not in {"Enabled", "Disabled"}:
                raise RuntimeError(
                    f"KMS key {record['arn']} is {state}; refusing to schedule deletion"
                )
            kms.schedule_key_deletion(
                KeyId=record["key_id"],
                PendingWindowInDays=_KMS_PENDING_WINDOW_DAYS,
            )
            metadata = kms.describe_key(KeyId=record["key_id"]).get("KeyMetadata", {})
            state = str(metadata.get("KeyState") or "")
        if state != "PendingDeletion":
            raise RuntimeError(f"KMS key {record['arn']} did not enter PendingDeletion")
        deletion_date = metadata.get("DeletionDate")
        record["scheduled"] = True
        record["deletion_date"] = deletion_date.isoformat() if deletion_date is not None else None
        results.append(
            {
                "arn": record["arn"],
                "state": state,
                "deletion_date": record["deletion_date"],
            }
        )
        ctx.persist()
    return {"keys": results, "pending_window_days": _KMS_PENDING_WINDOW_DAYS}


def _retained_resource_cleanup(ctx: RunContext) -> dict[str, Any]:
    result: dict[str, Any] = {"started_at": utc_now(), "errors": []}
    try:
        result["ecr_images"] = _cleanup_new_ecr_images(ctx)
    except Exception as exc:  # noqa: BLE001 - preserve partial evidence
        result["errors"].append({"phase": "ecr-images", "error": f"{type(exc).__name__}: {exc}"})
    try:
        result["ecr_repositories"] = _cleanup_new_ecr_repositories(ctx)
    except Exception as exc:  # noqa: BLE001 - preserve partial evidence
        result["errors"].append(
            {"phase": "ecr-repositories", "error": f"{type(exc).__name__}: {exc}"}
        )
    try:
        result["kms"] = _schedule_retained_kms_keys(ctx)
    except Exception as exc:  # noqa: BLE001 - preserve partial evidence
        result["errors"].append({"phase": "kms", "error": f"{type(exc).__name__}: {exc}"})
    result["ended_at"] = utc_now()
    ctx.checkpoint.state.setdefault("retained_cleanup_attempts", []).append(result)
    ctx.persist()
    if result["errors"]:
        raise RuntimeError(
            "Retained resource cleanup failed: " + json.dumps(result["errors"], sort_keys=True)
        )
    return result


def destroy_deployment(ctx: RunContext) -> dict[str, Any]:
    """Retry exact-owned teardown, preserving every structured attempt."""
    if not ctx.checkpoint.deployment_attempted:
        return {"needed": False, "attempts": []}

    initial_absence = _verify_target_stack_absence(ctx)
    if ctx.checkpoint.destroyed and initial_absence["all_absent"]:
        return {
            "needed": True,
            "already_destroyed": True,
            "stack_absence": initial_absence,
            "attempts": ctx.checkpoint.state.get("destroy_attempts", []),
            "workload_cleanup_attempts": ctx.checkpoint.state.get("workload_cleanup_attempts", []),
            "retained_cleanup_attempts": ctx.checkpoint.state.get("retained_cleanup_attempts", []),
        }
    if ctx.checkpoint.destroyed:
        ctx.checkpoint.destroyed = False
        for action_name in ("destroy", "final-inventory"):
            if action_name in ctx.checkpoint.completed_actions:
                ctx.checkpoint.completed_actions.remove(action_name)
        ctx.checkpoint.state.setdefault("stale_destroyed_reconciliations", []).append(
            {"at": utc_now(), "stack_absence": initial_absence}
        )
        ctx.persist()

    workload_cleanup = cleanup_workloads(ctx)
    if not workload_cleanup.get("complete"):
        raise RuntimeError(
            "Workload cleanup is an unresolved teardown barrier: "
            + json.dumps(
                {
                    "errors": workload_cleanup.get("errors", []),
                    "unresolved": workload_cleanup.get("unresolved", []),
                },
                sort_keys=True,
            )
        )
    _reconcile_stack_ownership(ctx)
    _checkpoint_new_ecr_repositories(ctx)
    _checkpoint_new_ecr_images(ctx)
    _checkpoint_retained_kms_keys(ctx)

    attempts = ctx.checkpoint.state.setdefault("destroy_attempts", [])
    for invocation_attempt in range(1, ctx.settings.destroy_attempts + 1):
        sequence = len(attempts) + 1
        started_at = utc_now()
        helper_outcomes: list[dict[str, Any]] = []

        def on_cleanup_complete(
            name: str,
            details: dict[str, Any],
            destroy_sequence: int = sequence,
            outcomes: list[dict[str, Any]] = helper_outcomes,
        ) -> None:
            outcome = {
                "destroy_sequence": destroy_sequence,
                "name": name,
                "at": utc_now(),
                "details": copy.deepcopy(details),
            }
            outcomes.append(outcome)
            ctx.checkpoint.state.setdefault("destroy_helper_outcomes", []).append(outcome)
            ctx.persist()

        try:
            _reconcile_stack_ownership(ctx)
            expected_stack_ids = {
                name: (
                    str(record["stack_id"])
                    if (record := _owned_stack_record(ctx, str(region), name)) is not None
                    else None
                )
                for name, region in ctx.checkpoint.state["target_stack_regions"].items()
            }
            prepared_change_sets = _prepared_change_set_authority(ctx)

            def on_prepared(
                stack_name: str,
                region: str,
                stack_id: str,
                change_set_id: str,
                change_set_type: str,
                target_ids: dict[str, str | None] = expected_stack_ids,
                change_sets: dict[str, dict[str, dict[str, str]]] = prepared_change_sets,
            ) -> None:
                _record_prepared_stack_identity(
                    ctx,
                    stack_name,
                    region,
                    stack_id,
                    change_set_id,
                    change_set_type,
                )
                target_ids[stack_name] = stack_id
                change_sets.setdefault(stack_name, {})[change_set_id] = {
                    "change_set_id": change_set_id,
                    "stack_id": stack_id,
                    "change_set_type": change_set_type,
                }

            overall, successful, failed = ctx.stack_manager.destroy_orchestrated(
                force=True,
                parallel=False,
                max_workers=1,
                expected_stack_ids=expected_stack_ids,
                prepared_change_sets=prepared_change_sets,
                authorize_stack=lambda name, region, stack_id: _authorize_owned_stack(
                    ctx,
                    name,
                    region,
                    stack_id,
                ),
                allow_bootstrap=False,
                bootstrap_stacks=ctx.checkpoint.state["bootstrap_stacks"],
                on_cleanup_complete=on_cleanup_complete,
                strict_deployment_token=f"{ctx.settings.run_id}-teardown",
                on_change_set_prepared=on_prepared,
                on_ecr_repository_created=lambda region, repository: (
                    _record_ecr_repository_creation(ctx, region, repository)
                ),
            )
            attempt: dict[str, Any] = {
                "sequence": sequence,
                "invocation_attempt": invocation_attempt,
                "started_at": started_at,
                "overall_success": overall,
                "successful_stacks": successful,
                "failed_stacks": failed,
                "helper_outcomes": helper_outcomes,
            }
            if overall:
                absence_before_cleanup = _verify_target_stack_absence(ctx)
                attempt["stack_absence_before_retained_cleanup"] = absence_before_cleanup
                if not absence_before_cleanup["all_absent"]:
                    raise RuntimeError(
                        "Target stack absence was not proved after destroy: "
                        + json.dumps(absence_before_cleanup["residual"], sort_keys=True)
                    )
                attempt["retained_cleanup"] = _retained_resource_cleanup(ctx)
                absence_before_completion = _verify_target_stack_absence(ctx)
                attempt["stack_absence_before_completion"] = absence_before_completion
                if not absence_before_completion["all_absent"]:
                    raise RuntimeError(
                        "A target stack reappeared during retained cleanup: "
                        + json.dumps(absence_before_completion["residual"], sort_keys=True)
                    )
        except Exception as exc:  # noqa: BLE001 - retry and preserve teardown evidence
            overall = False
            if "attempt" not in locals() or attempt.get("sequence") != sequence:
                attempt = {
                    "sequence": sequence,
                    "invocation_attempt": invocation_attempt,
                    "started_at": started_at,
                    "successful_stacks": [],
                    "failed_stacks": [],
                    "helper_outcomes": helper_outcomes,
                }
            attempt["overall_success"] = False
            attempt["error"] = f"{type(exc).__name__}: {exc}"
        attempt["ended_at"] = utc_now()
        attempts.append(attempt)
        ctx.persist()
        if overall:
            ctx.checkpoint.destroyed = True
            ctx.persist()
            return {
                "needed": True,
                "workload_cleanup": workload_cleanup,
                "workload_cleanup_attempts": ctx.checkpoint.state.get(
                    "workload_cleanup_attempts", []
                ),
                "attempts": attempts,
                "retained_cleanup_attempts": ctx.checkpoint.state.get(
                    "retained_cleanup_attempts", []
                ),
                "stack_absence": attempt["stack_absence_before_completion"],
            }
        if invocation_attempt < ctx.settings.destroy_attempts:
            time.sleep(ctx.settings.destroy_retry_delay_seconds)

    last_attempt = attempts[-1]
    last_failure = last_attempt.get("error") or ", ".join(last_attempt.get("failed_stacks", []))
    raise RuntimeError(
        "Orchestrated teardown did not succeed after "
        f"{ctx.settings.destroy_attempts} invocation attempts; last failure: "
        f"{last_failure or 'unknown'}"
    )


def action_destroy(ctx: RunContext) -> dict[str, Any]:
    """Destroy all run-owned stacks and retained resources."""
    return destroy_deployment(ctx)


def _strip_expected_pending_kms(
    ctx: RunContext,
    project_inventory: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    inventory = copy.deepcopy(project_inventory)
    expected = {
        (str(item["region"]), str(item["arn"])): item
        for item in ctx.checkpoint.state.get("owned_kms_keys", [])
        if item.get("scheduled")
    }
    accepted: list[dict[str, Any]] = []
    for region, resources in list(inventory.get("regional", {}).items()):
        remaining = []
        for key in resources.get("kms_keys", []):
            record = expected.get((region, str(key.get("arn") or "")))
            tags = key.get("tags") or {}
            if (
                record is not None
                and key.get("state") == "PendingDeletion"
                and tags.get(_RUN_STACK_TAG) == record["run_tag"]
                and tags.get("aws:cloudformation:stack-id") == record["stack_id"]
            ):
                accepted.append(key)
            else:
                remaining.append(key)
        resources["kms_keys"] = remaining
        if not any(resources.values()):
            inventory["regional"].pop(region)
    return inventory, accepted


def action_final_inventory(ctx: RunContext) -> dict[str, Any]:
    """Prove cleanup and exact protected-stack/ECR baseline preservation."""
    if ctx.checkpoint.baseline is None:
        raise RuntimeError("Final inventory cannot compare without a baseline")
    enabled_regions = ctx.checkpoint.state.get("enabled_regions")
    if not enabled_regions:
        raise RuntimeError("Checkpoint omitted enabled Regions")

    stack_absence = _verify_target_stack_absence(ctx)
    final_baseline = capture_baseline(
        ctx.session,
        enabled_regions=enabled_regions,
        ecr_regions=ctx.checkpoint.baseline.get("ecr_regions") or _topology_regions(ctx),
        protected_stack_names=ctx.settings.protected_stack_names,
    )
    comparison_baseline, accepted_retained_ecr = _strip_expected_retained_ecr(
        ctx,
        final_baseline,
    )
    differences = compare_baseline(ctx.checkpoint.baseline, comparison_baseline)
    project_inventory = collect_project_resources(
        ctx.session,
        enabled_regions=enabled_regions,
        project_name=ctx.config.project_name,
        seed_region=ctx.config.global_region,
    )
    residual_inventory = _strip_baseline_ecr(
        project_inventory,
        ctx.checkpoint.baseline,
    )
    residual_inventory = _strip_accepted_retained_ecr(
        residual_inventory,
        accepted_retained_ecr,
    )
    residual_inventory, accepted_pending_kms = _strip_expected_pending_kms(
        ctx,
        residual_inventory,
    )
    summary = summarize_project_resources(residual_inventory)
    result = {
        "summary": summary,
        "stack_absence": stack_absence,
        "baseline_differences": differences,
        "protected_and_ecr_inventory": final_baseline,
        "comparison_inventory": comparison_baseline,
        "accepted_retained_ecr": accepted_retained_ecr,
        "project_resources": project_inventory,
        "accepted_pending_kms_keys": accepted_pending_kms,
        "residual_project_resources": residual_inventory,
    }
    ctx.report.final_inventory = result
    ctx.checkpoint.state["final_inventory"] = copy.deepcopy(result)
    if not stack_absence["all_absent"] and ctx.checkpoint.destroyed:
        ctx.checkpoint.destroyed = False
        for action_name in ("destroy", "final-inventory"):
            if action_name in ctx.checkpoint.completed_actions:
                ctx.checkpoint.completed_actions.remove(action_name)
        ctx.checkpoint.state.setdefault("stale_destroyed_reconciliations", []).append(
            {"at": utc_now(), "stack_absence": stack_absence, "source": "final-inventory"}
        )
    ctx.persist()
    if not stack_absence["all_absent"]:
        raise RuntimeError(
            "Target stacks remain after teardown: "
            + json.dumps(stack_absence["residual"], sort_keys=True)
        )
    if differences:
        raise RuntimeError(
            "Protected stack/ECR baseline changed: " + json.dumps(differences, sort_keys=True)
        )
    if not project_resources_are_absent(residual_inventory):
        raise RuntimeError(
            "Project resources remain after teardown: "
            + json.dumps(residual_inventory, sort_keys=True)
        )
    return result


def build_action_registry() -> dict[str, ActionDefinition]:
    """Return actions in dependency-safe execution order."""
    definitions = (
        ActionDefinition(
            "preflight",
            "Verify exact git, account, configuration, and ownership identity",
            (),
            action_preflight,
        ),
        ActionDefinition(
            "baseline",
            "Capture protected CloudFormation and ECR baselines",
            ("preflight",),
            action_baseline,
        ),
        ActionDefinition(
            "deploy",
            "Deploy the configured GCO topology",
            ("baseline",),
            action_deploy,
        ),
        ActionDefinition(
            "topology",
            "Verify stacks, EKS, API endpoints, queues, and DynamoDB",
            ("deploy",),
            action_topology,
        ),
        ActionDefinition(
            "api",
            "Run the authenticated API Job lifecycle",
            ("topology",),
            action_api_lifecycle,
        ),
        ActionDefinition(
            "sqs",
            "Run the direct regional SQS Job lifecycle",
            ("topology",),
            action_sqs_lifecycle,
        ),
        ActionDefinition(
            "central-queue",
            "Run the idempotent DynamoDB-backed queue lifecycle",
            ("topology",),
            action_central_queue_lifecycle,
        ),
        ActionDefinition(
            "convergence",
            "Require stable SQS/DLQ and DynamoDB convergence",
            ("topology",),
            action_convergence,
        ),
        ActionDefinition(
            "destroy",
            "Destroy all run-owned infrastructure in dependency order",
            ("deploy",),
            action_destroy,
        ),
        ActionDefinition(
            "final-inventory",
            "Verify zero residual resources and exact baseline preservation",
            ("destroy",),
            action_final_inventory,
        ),
    )
    return {definition.name: definition for definition in definitions}
