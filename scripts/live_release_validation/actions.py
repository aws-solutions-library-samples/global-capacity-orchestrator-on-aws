"""Modular live-validation actions and registry."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import subprocess
import time
import uuid
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
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
from .models import RunContext, to_jsonable, utc_now

ActionHandler = Callable[[RunContext], dict[str, Any]]
_TERMINAL_QUEUE_STATUSES = {"succeeded", "failed", "cancelled"}
_HEALTHY_STACK_STATUSES = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
_RUN_STACK_TAG = "GcoLiveValidationRun"
_RUN_JOB_LABEL = "gco.aws/validation-run"
_PATH_JOB_LABEL = "gco.aws/validation-path"
_CENTRAL_MANAGED_BY_LABEL = "gco.io/managed-by"
_CENTRAL_QUEUE_KEY_LABEL = "gco.io/queue-job-key"
_CENTRAL_QUEUE_ID_ANNOTATION = "gco.io/queue-job-id"
_CENTRAL_ORIGINAL_NAME_ANNOTATION = "gco.io/original-job-name"
_EKS_KEY_LOGICAL_ID = "EksSecretsEncryptionKey74AFFE88"
_KMS_PENDING_WINDOW_DAYS = 7
_LOG_CLEANUP_TOKEN_TAG = "GcoLiveValidationCleanupToken"
_LOG_CLEANUP_HELPER_STACK_PREFIX = "LiveValidationLogCleanup"
_LOG_CLEANUP_HELPER_RUN_TAG = "LiveValidationHelperRun"
_LOG_CLEANUP_HELPER_TOKEN_TAG = "LiveValidationHelperToken"
_LOG_CLEANUP_ROLE_RUN_TAG = "LiveValidationCleanupRoleRun"
_LOG_CLEANUP_ROLE_TOKEN_TAG = "LiveValidationCleanupRoleToken"
_LOG_CLEANUP_ROLE_OUTPUT = "CleanupRoleArn"
_LOG_CLEANUP_ROLE_POLICY_NAME = "DeleteTaggedLogGroups"
_LOG_CLEANUP_SESSION_SECONDS = 900
_LOG_CLEANUP_STACK_POLL_ATTEMPTS = 120
_LOG_CLEANUP_STACK_POLL_SECONDS = 5
_LOG_GROUP_OBSERVATION_ATTEMPTS = 6
_LOG_GROUP_CLEANUP_MAX_PASSES = 3
_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS = 2
_LOG_GROUP_CLEANUP_STABLE_OBSERVATIONS = 2
_LOG_GROUP_ABSENCE_OBSERVATIONS = 3
_LOG_GROUP_OBSERVATION_POLL_SECONDS = 1
_LOG_GROUP_OBSERVATION_HISTORY_LIMIT = 40
_LOG_GROUP_RETRYABLE_OBSERVATION_CODES = frozenset(
    {
        "InternalFailure",
        "InternalServerError",
        "OperationAbortedException",
        "RequestLimitExceeded",
        "ServiceUnavailableException",
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
    }
)
_LOG_GROUP_SOURCE_TYPES = {
    "AWS::EKS::Cluster",
    "AWS::Lambda::Function",
    "AWS::Logs::LogGroup",
}
_EKS_LOG_GROUP_SUFFIXES = ("application", "dataplane", "host", "performance")
_CENTRAL_QUEUE_IDEMPOTENCY_NAMESPACE = uuid.UUID("88284d12-1e04-47d5-8871-607a9e4dac09")
_LOG_CLEANUP_HELPER_NAMESPACE = uuid.UUID("83af5e0b-f987-4ca6-8bb6-aa174c57096c")


@dataclass(frozen=True)
class ActionDefinition:
    """One selectable action and its safety dependencies."""

    name: str
    description: str
    dependencies: tuple[str, ...]
    handler: ActionHandler


class _LogGroupCleanupError(RuntimeError):
    """Retain structured cleanup evidence while propagating a failed phase."""

    def __init__(self, message: str, details: dict[str, Any]):
        super().__init__(message)
        self.details = copy.deepcopy(details)


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
    "AWS::EC2::FlowLog": "flow_logs",
    "AWS::EC2::Instance": "instances",
    "AWS::EC2::NatGateway": "nat_gateways",
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
_EC2_ID_SUFFIX = r"(?:[0-9a-f]{8}|[0-9a-f]{17})"
_EC2_TAGGED_RESOURCE_IDENTITIES: dict[str, tuple[str, re.Pattern[str]]] = {
    "elastic-ip": ("elastic_ips", re.compile(rf"eipalloc-{_EC2_ID_SUFFIX}")),
    "instance": ("instances", re.compile(rf"i-{_EC2_ID_SUFFIX}")),
    "natgateway": ("nat_gateways", re.compile(rf"nat-{_EC2_ID_SUFFIX}")),
    "network-interface": ("network_interfaces", re.compile(rf"eni-{_EC2_ID_SUFFIX}")),
    "security-group": ("security_groups", re.compile(rf"sg-{_EC2_ID_SUFFIX}")),
    "subnet": ("subnets", re.compile(rf"subnet-{_EC2_ID_SUFFIX}")),
    "vpc": ("vpcs", re.compile(rf"vpc-{_EC2_ID_SUFFIX}")),
    "vpc-flow-log": ("flow_logs", re.compile(rf"fl-{_EC2_ID_SUFFIX}")),
}
_EKS_CLUSTER_NAME = re.compile(r"[0-9A-Za-z][A-Za-z0-9_-]{0,99}")
_EKS_ASSOCIATION_ID = re.compile(r"a-[0-9a-z]{17}")
_KUBERNETES_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_KUBERNETES_POD_UID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
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
    *,
    expected_partition: str,
    expected_region: str,
    expected_account: str,
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
    expected_ec2_category = {
        "AWS::EC2::FlowLog": "flow_logs",
        "AWS::EC2::NatGateway": "nat_gateways",
    }.get(resource_type)
    if expected_ec2_category is not None:
        return bool(
            expected_partition
            and expected_region
            and re.fullmatch(r"\d{12}", expected_account)
            and _ec2_tagged_resource_identity(
                arn,
                expected_region,
                expected_partition,
                expected_account,
            )
            == (expected_ec2_category, physical_id)
        )
    return False


def _tagged_resource_is_protected(
    record: Any,
    *,
    protected_stack_ids: set[str],
    protected_resource_ids: dict[str, set[str]],
    exact_arns: set[str],
    expected_partition: str,
    expected_region: str,
    expected_account: str,
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
        _tagged_arn_matches_protected_physical_id(
            resource_type,
            arn,
            physical_id,
            expected_partition=expected_partition,
            expected_region=expected_region,
            expected_account=expected_account,
        )
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


def _valid_kubernetes_dns_subdomain(value: str) -> bool:
    return bool(
        value
        and len(value) <= 253
        and all(_KUBERNETES_DNS_LABEL.fullmatch(label) for label in value.split("."))
    )


def _eks_pod_parent_cluster(
    arn: str,
    expected_region: str,
    expected_partition: str,
    expected_account: str,
) -> str | None:
    """Parse canonical in-scope EKS pod identities; malformed records stay visible."""
    parts = arn.split(":", 5)
    if (
        len(parts) != 6
        or parts[0] != "arn"
        or parts[1] != expected_partition
        or parts[2] != "eks"
        or parts[3] != expected_region
        or parts[4] != expected_account
    ):
        return None
    resource_parts = parts[5].split("/")
    if any(not component for component in resource_parts):
        return None
    if resource_parts[0] == "pod":
        if len(resource_parts) != 5:
            return None
        _kind, cluster, namespace, pod_name, pod_uid = resource_parts
        if (
            not _EKS_CLUSTER_NAME.fullmatch(cluster)
            or not _KUBERNETES_DNS_LABEL.fullmatch(namespace)
            or not _valid_kubernetes_dns_subdomain(pod_name)
            or not _KUBERNETES_POD_UID.fullmatch(pod_uid)
        ):
            return None
        return cluster
    if resource_parts[0] == "podidentityassociation":
        if len(resource_parts) != 3:
            return None
        _kind, cluster, association_id = resource_parts
        if not _EKS_CLUSTER_NAME.fullmatch(cluster) or not _EKS_ASSOCIATION_ID.fullmatch(
            association_id
        ):
            return None
        return cluster
    return None


def _ec2_tagged_resource_identity(
    arn: str,
    expected_region: str,
    expected_partition: str,
    expected_account: str,
) -> tuple[str, str] | None:
    """Map only canonical in-scope EC2 ARNs to authoritative live identities."""
    parts = arn.split(":", 5)
    if (
        len(parts) != 6
        or parts[0] != "arn"
        or parts[1] != expected_partition
        or parts[2] != "ec2"
        or parts[3] != expected_region
        or parts[4] != expected_account
    ):
        return None
    resource_kind, separator, resource_id = parts[5].partition("/")
    identity = _EC2_TAGGED_RESOURCE_IDENTITIES.get(resource_kind)
    if not separator or not resource_id or "/" in resource_id or identity is None:
        return None
    category, resource_id_pattern = identity
    if not resource_id_pattern.fullmatch(resource_id):
        return None
    return category, resource_id


def _strip_baseline_ecr(
    project_inventory: dict[str, Any], baseline: dict[str, Any]
) -> dict[str, Any]:
    """Strip only exact protected identities and authoritatively absent tag records."""
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
    authoritative_ec2_resources = inventory.get("authoritative_ec2_resources")
    authority_scope = inventory.get("authority_scope")
    expected_partition = (
        str(authority_scope.get("partition") or "") if isinstance(authority_scope, Mapping) else ""
    )
    expected_account = (
        str(authority_scope.get("account") or "") if isinstance(authority_scope, Mapping) else ""
    )
    has_exact_authority_scope = bool(
        expected_partition and re.fullmatch(r"\d{12}", expected_account)
    )
    coverage = inventory.get("coverage")
    coverage_complete = isinstance(coverage, Mapping) and coverage.get("complete") is True
    completed_scanners = (
        {str(scanner) for scanner in coverage.get("completed_scanners", [])}
        if isinstance(coverage, Mapping) and isinstance(coverage.get("completed_scanners"), list)
        else set()
    )
    scanner_regions = coverage.get("scanner_regions") if isinstance(coverage, Mapping) else None
    eks_scanner_regions = (
        {str(region) for region in scanner_regions.get("eks_clusters", [])}
        if isinstance(scanner_regions, Mapping)
        and isinstance(scanner_regions.get("eks_clusters"), list)
        else set()
    )
    instance_scanner_regions = (
        {str(region) for region in scanner_regions.get("ec2_instances", [])}
        if isinstance(scanner_regions, Mapping)
        and isinstance(scanner_regions.get("ec2_instances"), list)
        else set()
    )
    network_scanner_regions = (
        {str(region) for region in scanner_regions.get("ec2_networking", [])}
        if isinstance(scanner_regions, Mapping)
        and isinstance(scanner_regions.get("ec2_networking"), list)
        else set()
    )
    has_complete_eks_authority = coverage_complete and "eks_clusters" in completed_scanners
    has_complete_ec2_authority = coverage_complete and {
        "ec2_instances",
        "ec2_networking",
    }.issubset(completed_scanners)
    ec2_scanner_regions = instance_scanner_regions & network_scanner_regions
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
                    expected_partition=expected_partition,
                    expected_region=region_key,
                    expected_account=expected_account,
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

        region_ec2_authority = (
            authoritative_ec2_resources.get(region_key)
            if isinstance(authoritative_ec2_resources, dict)
            else None
        )
        if (
            "tagged_resources" in resources
            and has_exact_authority_scope
            and has_complete_ec2_authority
            and region_key in ec2_scanner_regions
            and isinstance(region_ec2_authority, dict)
        ):
            authoritative_ec2_ids = {
                category: {str(candidate) for candidate in candidates}
                for category, candidates in region_ec2_authority.items()
                if category in {item[0] for item in _EC2_TAGGED_RESOURCE_IDENTITIES.values()}
                and isinstance(candidates, list)
            }
            resources["tagged_resources"] = [
                record
                for record in resources["tagged_resources"]
                if (
                    (
                        identity := _ec2_tagged_resource_identity(
                            str(record.get("arn") or ""),
                            region_key,
                            expected_partition,
                            expected_account,
                        )
                    )
                    is None
                    or identity[0] not in authoritative_ec2_ids
                    or identity[1] in authoritative_ec2_ids[identity[0]]
                )
            ]

        if (
            "tagged_resources" in resources
            and has_exact_authority_scope
            and has_complete_eks_authority
            and region_key in eks_scanner_regions
            and isinstance(authoritative_clusters, dict)
            and region in authoritative_clusters
            and isinstance(authoritative_clusters[region], list)
        ):
            existing_clusters = {str(name) for name in authoritative_clusters[region]}
            resources["tagged_resources"] = [
                record
                for record in resources["tagged_resources"]
                if (
                    (
                        parent := _eks_pod_parent_cluster(
                            str(record.get("arn") or ""),
                            region_key,
                            expected_partition,
                            expected_account,
                        )
                    )
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


def _validated_owned_kms_identity(
    ctx: RunContext,
    record: Mapping[str, Any],
) -> tuple[str, str, str, str]:
    """Validate immutable stack-resource authority for one run-owned KMS key."""
    region = str(record.get("region") or "")
    key_id = str(record.get("key_id") or "")
    arn = str(record.get("arn") or "")
    stack_name = str(record.get("stack_name") or "")
    stack_id = str(record.get("stack_id") or "")
    logical_id = str(record.get("logical_id") or "")
    target_regions = ctx.checkpoint.state.get("target_stack_regions")
    if not isinstance(target_regions, dict) or str(target_regions.get(stack_name) or "") != region:
        raise RuntimeError(f"KMS checkpoint target stack is invalid for {arn or key_id}")
    partition = ctx.session.get_partition_for_region(region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for KMS key in {region}")
    expected_arn = f"arn:{partition}:kms:{region}:{ctx.settings.expected_account}:key/{key_id}"
    expected_stack_prefix = (
        f"arn:{partition}:cloudformation:{region}:{ctx.settings.expected_account}:"
        f"stack/{stack_name}/"
    )
    owned_stack_record = _owned_stack_record(ctx, region, stack_name)
    expected_stack_id = str((owned_stack_record or {}).get("stack_id") or "")
    if not key_id or arn != expected_arn:
        raise RuntimeError(f"KMS checkpoint ARN is invalid for {arn or key_id}")
    if (
        not stack_name
        or not expected_stack_id.startswith(expected_stack_prefix)
        or stack_id != expected_stack_id
        or (owned_stack_record or {}).get("run_tag") != ctx.settings.run_id
    ):
        raise RuntimeError(f"KMS checkpoint stack identity is invalid for {arn}")
    if (
        record.get("ownership_authority") != "cloudformation-stack-resource"
        or not logical_id
        or record.get("run_tag") != ctx.settings.run_id
    ):
        raise RuntimeError(f"KMS checkpoint authority is incomplete for {arn}")

    retained_identity = (
        stack_name == f"{ctx.config.project_name}-{region}" and logical_id == _EKS_KEY_LOGICAL_ID
    )
    cleanup_policy = str(record.get("cleanup_policy") or "")
    if not cleanup_policy and retained_identity:
        cleanup_policy = "harness-schedule"
    if cleanup_policy == "harness-schedule":
        if not retained_identity:
            raise RuntimeError(f"Retained KMS checkpoint identity is invalid for {arn}")
    elif cleanup_policy == "cloudformation-delete":
        if retained_identity:
            raise RuntimeError(f"Retained EKS key cannot use CloudFormation cleanup: {arn}")
    else:
        raise RuntimeError(f"KMS checkpoint cleanup policy is invalid for {arn}")
    return region, key_id, arn, cleanup_policy


def _validated_retained_kms_identity(
    ctx: RunContext,
    record: Mapping[str, Any],
) -> tuple[str, str, str]:
    """Validate exact retained-EKS authority before harness-scheduled deletion."""
    region, key_id, arn, cleanup_policy = _validated_owned_kms_identity(ctx, record)
    if cleanup_policy != "harness-schedule":
        raise RuntimeError(f"KMS key is not harness-retained: {arn}")
    return region, key_id, arn


def _derived_log_group_names(resource_type: str, physical_id: str) -> tuple[str, ...]:
    if resource_type == "AWS::Logs::LogGroup":
        return (physical_id,)
    if resource_type == "AWS::Lambda::Function":
        return (f"/aws/lambda/{physical_id}",)
    if resource_type == "AWS::EKS::Cluster":
        return (
            f"/aws/eks/{physical_id}/cluster",
            *(
                f"/aws/containerinsights/{physical_id}/{suffix}"
                for suffix in _EKS_LOG_GROUP_SUFFIXES
            ),
        )
    return ()


def _live_eks_cluster_identity(
    ctx: RunContext,
    region: str,
    cluster_name: str,
) -> dict[str, str]:
    """Require the exact ACTIVE service-side cluster before deriving log authority."""
    partition = ctx.session.get_partition_for_region(region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for EKS cluster in {region}")
    expected_arn = (
        f"arn:{partition}:eks:{region}:{ctx.settings.expected_account}:cluster/{cluster_name}"
    )
    response = ctx.session.client("eks", region_name=region).describe_cluster(name=cluster_name)
    cluster = response.get("cluster")
    if not isinstance(cluster, dict):
        raise RuntimeError(f"EKS omitted cluster identity for {region}:{cluster_name}")
    identity = {
        "name": str(cluster.get("name") or ""),
        "arn": str(cluster.get("arn") or ""),
        "status": str(cluster.get("status") or ""),
    }
    if identity != {"name": cluster_name, "arn": expected_arn, "status": "ACTIVE"}:
        raise RuntimeError(
            f"EKS cluster identity is not exact and ACTIVE for {region}:{cluster_name}"
        )
    return identity


def _eks_cluster_log_authority_identity(
    ctx: RunContext,
    region: str,
    cluster_name: str,
    *,
    allow_deleted: bool,
) -> dict[str, str]:
    """Resolve EKS log authority, tolerating a rolled-back (deleted) cluster.

    A create rollback deletes the cluster itself while its control-plane and
    Container Insights log groups survive. The DELETED tombstone identity is
    only ever derived from this run's own stack resource record.
    """
    if not allow_deleted:
        return _live_eks_cluster_identity(ctx, region, cluster_name)
    partition = ctx.session.get_partition_for_region(region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for EKS cluster in {region}")
    expected_arn = (
        f"arn:{partition}:eks:{region}:{ctx.settings.expected_account}:cluster/{cluster_name}"
    )
    try:
        return _live_eks_cluster_identity(ctx, region, cluster_name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code", "") != "ResourceNotFoundException":
            raise
        return {"name": cluster_name, "arn": expected_arn, "status": "DELETED"}


def _validated_owned_log_group_identity(
    ctx: RunContext,
    record: Mapping[str, Any],
) -> tuple[str, str]:
    """Validate an exact log-group name derived from a checkpointed stack resource."""
    region = str(record.get("region") or "")
    name = str(record.get("name") or "")
    stack_name = str(record.get("stack_name") or "")
    stack_id = str(record.get("stack_id") or "")
    resource_type = str(record.get("source_resource_type") or "")
    logical_id = str(record.get("source_logical_id") or "")
    physical_id = str(record.get("source_physical_id") or "")
    target_regions = ctx.checkpoint.state.get("target_stack_regions")
    if not isinstance(target_regions, dict) or str(target_regions.get(stack_name) or "") != region:
        raise RuntimeError(f"Log-group checkpoint target stack is invalid for {region}:{name}")
    partition = ctx.session.get_partition_for_region(region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for log group in {region}")
    expected_stack_prefix = (
        f"arn:{partition}:cloudformation:{region}:{ctx.settings.expected_account}:"
        f"stack/{stack_name}/"
    )
    owned_stack_record = _owned_stack_record(ctx, region, stack_name)
    expected_stack_id = str((owned_stack_record or {}).get("stack_id") or "")
    if (
        not name
        or not logical_id
        or resource_type not in _LOG_GROUP_SOURCE_TYPES
        or name not in _derived_log_group_names(resource_type, physical_id)
    ):
        raise RuntimeError(f"Log-group checkpoint source is invalid for {region}:{name}")
    source_service_identity = record.get("source_service_identity")
    if resource_type == "AWS::EKS::Cluster":
        expected_arn = (
            f"arn:{partition}:eks:{region}:{ctx.settings.expected_account}:cluster/{physical_id}"
        )
        # ACTIVE is the pre-destroy authority; DELETED is the exact tombstone
        # recorded when a rolled-back create removed the cluster but left its
        # control-plane and Container Insights log groups behind.
        accepted_source_identities = tuple(
            {"name": physical_id, "arn": expected_arn, "status": status}
            for status in ("ACTIVE", "DELETED")
        )
        if source_service_identity not in accepted_source_identities:
            raise RuntimeError(
                f"Log-group checkpoint lacks exact live EKS identity for {region}:{name}"
            )
    elif source_service_identity not in (None, {}):
        raise RuntimeError(f"Unexpected service identity for log group {region}:{name}")
    cleanup_token = str(record.get("cleanup_token") or "")
    expected_cleanup_token = str(ctx.checkpoint.state.get("log_group_cleanup_token") or "")
    if (
        not expected_stack_id.startswith(expected_stack_prefix)
        or stack_id != expected_stack_id
        or (owned_stack_record or {}).get("run_tag") != ctx.settings.run_id
        or record.get("run_tag") != ctx.settings.run_id
        or record.get("ownership_authority") != "cloudformation-stack-resource-derived"
        or record.get("authority_phase") != "pre-destroy"
        or not cleanup_token
        or cleanup_token != expected_cleanup_token
    ):
        raise RuntimeError(f"Log-group checkpoint authority is invalid for {region}:{name}")
    return region, name


def _describe_exact_log_group(client: Any, name: str) -> dict[str, Any] | None:
    kwargs: dict[str, Any] = {"logGroupNamePrefix": name, "limit": 50}
    while True:
        response = client.describe_log_groups(**kwargs)
        for log_group in response.get("logGroups", []):
            if not isinstance(log_group, Mapping):
                raise RuntimeError("CloudWatch Logs returned a non-object log-group record")
            candidate = cast(Mapping[str, Any], log_group)
            if str(candidate.get("logGroupName") or "") == name:
                return {str(key): value for key, value in candidate.items()}
        token = response.get("nextToken")
        if not token:
            return None
        kwargs["nextToken"] = token


def _log_group_identity(client: Any, region: str, name: str) -> dict[str, Any] | None:
    log_group = _describe_exact_log_group(client, name)
    if log_group is None:
        return None
    arn = str(log_group.get("logGroupArn") or log_group.get("arn") or "").removesuffix(":*")
    creation_time = log_group.get("creationTime")
    if not arn or not isinstance(creation_time, int):
        raise RuntimeError(f"CloudWatch Logs omitted identity for {region}:{name}")
    tags = client.list_tags_for_resource(resourceArn=arn).get("tags") or {}
    return {
        "arn": arn,
        "creation_time": creation_time,
        "tags": {str(key): str(value) for key, value in tags.items()},
    }


def _log_group_generation(identity: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable fields that distinguish same-name log generations."""
    arn = str(identity.get("arn") or "")
    creation_time = identity.get("creation_time")
    if not arn or not isinstance(creation_time, int):
        raise RuntimeError("Checkpointed CloudWatch log-group identity is malformed")
    return {"arn": arn, "creation_time": creation_time}


def _observe_log_group_stability(
    client: Any,
    region: str,
    name: str,
    *,
    expected_identity: Mapping[str, Any] | None = None,
    expected_tags: Mapping[str, str] | None = None,
    required_present: int | None,
    required_absent: int | None,
    attempts: int = _LOG_GROUP_OBSERVATION_ATTEMPTS,
    poll_seconds: float = _LOG_GROUP_OBSERVATION_POLL_SECONDS,
) -> dict[str, Any]:
    """Bound identity reads until presence/absence is stable or a fence is crossed."""
    for label, value in (
        ("attempts", attempts),
        ("required_present", required_present),
        ("required_absent", required_absent),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(f"{label} must be a positive integer or None")
    if required_present is None and required_absent is None:
        raise ValueError("At least one stable log-group outcome must be requested")
    if poll_seconds < 0:
        raise ValueError("poll_seconds must be non-negative")

    expected_generation = (
        _log_group_generation(expected_identity) if expected_identity is not None else None
    )
    expected_authority_tags = {str(key): str(value) for key, value in (expected_tags or {}).items()}
    observations: list[dict[str, Any]] = []
    seen_generations: list[dict[str, Any]] = []
    present_streak = 0
    absent_streak = 0
    replacement_streak = 0
    replacement_generation: dict[str, Any] | None = None
    last_identity: dict[str, Any] | None = None

    def result(status: str, **extra: Any) -> dict[str, Any]:
        return {
            "status": status,
            "region": region,
            "name": name,
            "attempt_count": len(observations),
            "observations": observations,
            "identity": copy.deepcopy(last_identity),
            **extra,
        }

    for attempt in range(1, attempts + 1):
        observed_at = utc_now()
        try:
            identity = _log_group_identity(client, region, name)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "")
            if code not in _LOG_GROUP_RETRYABLE_OBSERVATION_CODES:
                raise
            observations.append(
                {
                    "attempt": attempt,
                    "observed_at": observed_at,
                    "status": "retryable-error",
                    "error_code": code,
                    "error": str(exc),
                }
            )
            present_streak = 0
            absent_streak = 0
            replacement_streak = 0
            replacement_generation = None
        else:
            last_identity = copy.deepcopy(identity)
            if identity is None:
                if expected_generation is None and seen_generations:
                    observations.append(
                        {
                            "attempt": attempt,
                            "observed_at": observed_at,
                            "status": "replacement",
                            "observed_generation": None,
                        }
                    )
                    return result(
                        "replacement",
                        expected_generation=seen_generations[-1],
                        observed_generation=None,
                    )
                replacement_streak = 0
                replacement_generation = None
                absent_streak += 1
                present_streak = 0
                observations.append(
                    {
                        "attempt": attempt,
                        "observed_at": observed_at,
                        "status": "absent",
                        "consecutive": absent_streak,
                    }
                )
                if required_absent is not None and absent_streak >= required_absent:
                    return result("absent", consecutive=absent_streak)
            else:
                generation = _log_group_generation(identity)
                if generation not in seen_generations:
                    seen_generations.append(generation)
                observations.append(
                    {
                        "attempt": attempt,
                        "observed_at": observed_at,
                        "status": "present",
                        "generation": copy.deepcopy(generation),
                    }
                )
                if expected_generation is not None and generation != expected_generation:
                    if generation == replacement_generation:
                        replacement_streak += 1
                    else:
                        replacement_generation = generation
                        replacement_streak = 1
                    observations[-1]["status"] = "replacement-candidate"
                    observations[-1]["consecutive"] = replacement_streak
                    present_streak = 0
                    absent_streak = 0
                    if replacement_streak >= _LOG_GROUP_CLEANUP_STABLE_OBSERVATIONS:
                        observations[-1]["status"] = "replacement"
                        return result(
                            "replacement",
                            expected_generation=expected_generation,
                            observed_generation=generation,
                            consecutive=replacement_streak,
                        )
                    if attempt < attempts:
                        time.sleep(poll_seconds)
                    continue
                replacement_streak = 0
                replacement_generation = None
                if expected_generation is None and len(seen_generations) > 1:
                    observations[-1]["status"] = "replacement"
                    return result(
                        "replacement",
                        expected_generation=seen_generations[0],
                        observed_generation=generation,
                    )
                tags = identity.get("tags") or {}
                tag_drift = {
                    key: {"expected": value, "observed": tags.get(key)}
                    for key, value in expected_authority_tags.items()
                    if tags.get(key) != value
                }
                if tag_drift:
                    observations[-1]["status"] = "tag-drift"
                    observations[-1]["tag_drift"] = copy.deepcopy(tag_drift)
                    return result("tag-drift", tag_drift=tag_drift)
                present_streak += 1
                absent_streak = 0
                observations[-1]["consecutive"] = present_streak
                if required_present is not None and present_streak >= required_present:
                    return result("present", consecutive=present_streak)
        if attempt < attempts:
            time.sleep(poll_seconds)

    return result(
        "unsettled",
        required_present=required_present,
        required_absent=required_absent,
        present_streak=present_streak,
        absent_streak=absent_streak,
    )


def _record_log_group_observation(
    ctx: RunContext,
    record: dict[str, Any],
    *,
    phase: str,
    outcome: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist bounded identity evidence and any confirmed replacement generation."""
    entry = {"phase": phase, "recorded_at": utc_now(), **copy.deepcopy(dict(outcome))}
    with ctx.state_lock:
        history = record.setdefault("identity_observation_history", [])
        if not isinstance(history, list):
            raise RuntimeError("Log-group identity_observation_history must be a list")
        history.append(entry)
        del history[:-_LOG_GROUP_OBSERVATION_HISTORY_LIMIT]
        if outcome.get("status") == "replacement":
            replacements = record.setdefault("replacement_evidence", [])
            if not isinstance(replacements, list):
                raise RuntimeError("Log-group replacement_evidence must be a list")
            replacements.append(copy.deepcopy(entry))
        ctx.persist_callback(ctx.checkpoint)
    return entry


def _record_log_group_checkpoint_incident(
    ctx: RunContext,
    candidate: Mapping[str, Any],
    *,
    phase: str,
    outcome: Mapping[str, Any],
) -> None:
    """Preserve failed pre-authority observations without adopting the generation."""
    with ctx.state_lock:
        incidents = ctx.checkpoint.state.setdefault("log_group_checkpoint_incidents", [])
        if not isinstance(incidents, list):
            raise RuntimeError("Checkpoint log_group_checkpoint_incidents must be a list")
        incidents.append(
            {
                "phase": phase,
                "recorded_at": utc_now(),
                "candidate": copy.deepcopy(dict(candidate)),
                "outcome": copy.deepcopy(dict(outcome)),
            }
        )
        ctx.persist_callback(ctx.checkpoint)


def _set_log_group_disposition(
    ctx: RunContext,
    record: dict[str, Any],
    *,
    status: str,
    phase: str,
    outcome: Mapping[str, Any],
) -> dict[str, Any]:
    disposition = {
        "status": status,
        "phase": phase,
        "recorded_at": utc_now(),
        "original_identity": copy.deepcopy(record.get("observed_identity")),
        "last_observation_status": str(outcome.get("status") or ""),
    }
    with ctx.state_lock:
        record["original_generation_disposition"] = disposition
        ctx.persist_callback(ctx.checkpoint)
    return disposition


def _canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _cleanup_principal_identity(ctx: RunContext, caller_arn: str) -> dict[str, str]:
    """Resolve a renewable caller session to one immutable IAM principal."""
    region = ctx.config.global_region
    partition = ctx.session.get_partition_for_region(region)
    account = ctx.settings.expected_account
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for cleanup authority in {region}")
    if not caller_arn or "*" in caller_arn:
        raise RuntimeError("Cleanup authority principal ARN is empty or contains a wildcard")

    iam = ctx.session.client("iam", region_name=region)
    iam_prefix = f"arn:{partition}:iam::{account}:"
    if caller_arn.startswith(f"{iam_prefix}user/"):
        user_name = caller_arn.rsplit("/", 1)[-1]
        user = iam.get_user(UserName=user_name).get("User")
        principal_arn = str((user or {}).get("Arn") or "")
        principal_id = str((user or {}).get("UserId") or "")
        if principal_arn != caller_arn or not principal_id:
            raise RuntimeError(f"IAM returned an invalid user identity for {caller_arn}")
        return {"arn": principal_arn, "principal_id": principal_id}

    if caller_arn.startswith(f"{iam_prefix}role/"):
        role_name = caller_arn.rsplit("/", 1)[-1]
        role = iam.get_role(RoleName=role_name).get("Role")
        principal_arn = str((role or {}).get("Arn") or "")
        principal_id = str((role or {}).get("RoleId") or "")
        if principal_arn != caller_arn or not principal_id:
            raise RuntimeError(f"IAM returned an invalid role identity for {caller_arn}")
        return {"arn": principal_arn, "principal_id": principal_id}

    assumed_prefix = f"arn:{partition}:sts::{account}:assumed-role/"
    if caller_arn.startswith(assumed_prefix):
        role_session = caller_arn.removeprefix(assumed_prefix)
        role_resource, separator, session_name = role_session.rpartition("/")
        role_name = role_resource.rsplit("/", 1)[-1]
        if not separator or not role_name or not session_name:
            raise RuntimeError(f"Malformed assumed-role caller ARN: {caller_arn}")
        role = iam.get_role(RoleName=role_name).get("Role")
        principal_arn = str((role or {}).get("Arn") or "")
        principal_id = str((role or {}).get("RoleId") or "")
        if (
            not principal_arn.startswith(f"{iam_prefix}role/")
            or principal_arn.rsplit("/", 1)[-1] != role_name
            or not principal_id
        ):
            raise RuntimeError(f"IAM returned an invalid underlying role identity for {caller_arn}")
        return {"arn": principal_arn, "principal_id": principal_id}
    raise RuntimeError(
        f"Log cleanup requires an exact IAM user or STS assumed-role caller; found {caller_arn}"
    )


def _log_cleanup_policy(
    ctx: RunContext,
    cleanup_token: str,
    records: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    partitions: set[str] = set()
    for record in records:
        region, _name = _validated_owned_log_group_identity(ctx, record)
        partition = ctx.session.get_partition_for_region(region)
        if not partition:
            raise RuntimeError(f"Could not resolve AWS partition for log cleanup in {region}")
        partitions.add(partition)
    if len(partitions) != 1:
        raise RuntimeError("Log cleanup requires all authorized groups to share one AWS partition")
    partition = next(iter(partitions))
    return (
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "logs:DeleteLogGroup",
                    "Resource": (
                        f"arn:{partition}:logs:*:{ctx.settings.expected_account}:log-group:*"
                    ),
                    "Condition": {
                        "StringEquals": {
                            f"aws:ResourceTag/{_RUN_STACK_TAG}": ctx.settings.run_id,
                            f"aws:ResourceTag/{_LOG_CLEANUP_TOKEN_TAG}": cleanup_token,
                        }
                    },
                }
            ],
        },
        partition,
    )


def _log_cleanup_helper_spec(ctx: RunContext) -> dict[str, Any] | None:
    records = ctx.checkpoint.state.get("owned_log_groups", [])
    if not isinstance(records, list):
        raise RuntimeError("Checkpoint owned_log_groups must be a list")
    if not records:
        return None
    cleanup_token = str(ctx.checkpoint.state.get("log_group_cleanup_token") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", cleanup_token):
        raise RuntimeError("Checkpoint log-group cleanup token is malformed")
    if any(not isinstance(record, dict) for record in records):
        raise RuntimeError("Checkpoint owned_log_groups must contain objects")
    policy, partition = _log_cleanup_policy(ctx, cleanup_token, records)
    helper_region = str(ctx.config.global_region)
    if ctx.session.get_partition_for_region(helper_region) != partition:
        raise RuntimeError("Cleanup helper Region is outside the log groups' AWS partition")

    existing_helper = ctx.checkpoint.state.get("log_cleanup_helper")
    if existing_helper is not None and not isinstance(existing_helper, dict):
        raise RuntimeError("Checkpoint log_cleanup_helper must be an object")
    if isinstance(existing_helper, dict):
        first_caller_arn = str(existing_helper.get("first_caller_arn") or "")
        trusted_principal_arn = str(existing_helper.get("trusted_principal_arn") or "")
        trusted_principal_id = str(existing_helper.get("trusted_principal_id") or "")
        if not first_caller_arn or not trusted_principal_arn or not trusted_principal_id:
            raise RuntimeError("Checkpoint cleanup helper lacks immutable caller identity")
    else:
        first_caller_arn = str(ctx.checkpoint.state.get("account_arn") or "")
        principal_identity = _cleanup_principal_identity(ctx, first_caller_arn)
        trusted_principal_arn = principal_identity["arn"]
        trusted_principal_id = principal_identity["principal_id"]
    expected_iam_prefix = f"arn:{partition}:iam::{ctx.settings.expected_account}:"
    if (
        not trusted_principal_arn.startswith(
            (f"{expected_iam_prefix}user/", f"{expected_iam_prefix}role/")
        )
        or "*" in trusted_principal_arn
        or not re.fullmatch(r"[A-Z0-9]+", trusted_principal_id)
    ):
        raise RuntimeError("Checkpoint cleanup helper canonical principal is invalid")
    stable_id = uuid.uuid5(
        _LOG_CLEANUP_HELPER_NAMESPACE,
        f"{partition}:{ctx.settings.expected_account}:{ctx.settings.run_id}:{cleanup_token}",
    ).hex[:20]
    stack_name = f"{_LOG_CLEANUP_HELPER_STACK_PREFIX}-{stable_id}"
    role_name = stack_name
    project_name = str(ctx.config.project_name)
    if any(
        name == project_name or name.startswith((f"{project_name}-", f"{project_name}/"))
        for name in (stack_name, role_name)
    ):
        raise RuntimeError("Cleanup helper identity overlaps project inventory naming")
    role_arn = f"arn:{partition}:iam::{ctx.settings.expected_account}:role/{role_name}"
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": trusted_principal_arn},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"sts:ExternalId": cleanup_token}},
            }
        ],
    }
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Temporary least-privilege role for live-validation log cleanup",
        "Resources": {
            "CleanupRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": role_name,
                    "MaxSessionDuration": 3600,
                    "AssumeRolePolicyDocument": trust_policy,
                    "Policies": [
                        {
                            "PolicyName": _LOG_CLEANUP_ROLE_POLICY_NAME,
                            "PolicyDocument": policy,
                        }
                    ],
                    "Tags": [
                        {"Key": _LOG_CLEANUP_ROLE_RUN_TAG, "Value": ctx.settings.run_id},
                        {"Key": _LOG_CLEANUP_ROLE_TOKEN_TAG, "Value": cleanup_token},
                    ],
                },
            }
        },
        "Outputs": {_LOG_CLEANUP_ROLE_OUTPUT: {"Value": {"Fn::GetAtt": ["CleanupRole", "Arn"]}}},
    }
    template_body = _canonical_json(template)
    return {
        "schema_version": 1,
        "region": helper_region,
        "stack_name": stack_name,
        "role_name": role_name,
        "role_arn": role_arn,
        "partition": partition,
        "run_id": ctx.settings.run_id,
        "cleanup_token": cleanup_token,
        "first_caller_arn": first_caller_arn,
        "trusted_principal_arn": trusted_principal_arn,
        "trusted_principal_id": trusted_principal_id,
        "role_policy": policy,
        "trust_policy": trust_policy,
        "template": template,
        "template_body": template_body,
        "template_sha256": hashlib.sha256(template_body.encode("utf-8")).hexdigest(),
    }


def _prepare_log_cleanup_helper_record(
    ctx: RunContext,
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    immutable_keys = (
        "schema_version",
        "region",
        "stack_name",
        "role_name",
        "role_arn",
        "partition",
        "run_id",
        "cleanup_token",
        "trusted_principal_arn",
        "trusted_principal_id",
        "template_sha256",
    )
    with ctx.state_lock:
        record = ctx.checkpoint.state.get("log_cleanup_helper")
        if record is None:
            record = {key: spec[key] for key in immutable_keys}
            record.update(
                {
                    "first_caller_arn": spec["first_caller_arn"],
                    "active_stack_id": None,
                    "lifecycle": "prepared",
                    "create_sequence": 0,
                    "stack_history": [],
                }
            )
            ctx.checkpoint.state["log_cleanup_helper"] = record
        elif not isinstance(record, dict):
            raise RuntimeError("Checkpoint log_cleanup_helper must be an object")
        elif any(record.get(key) != spec[key] for key in immutable_keys):
            raise RuntimeError("Checkpoint log cleanup helper identity changed")
        ctx.persist_callback(ctx.checkpoint)
        return record


def _helper_stack_id_prefix(ctx: RunContext, spec: Mapping[str, Any]) -> str:
    return (
        f"arn:{spec['partition']}:cloudformation:{spec['region']}:"
        f"{ctx.settings.expected_account}:stack/{spec['stack_name']}/"
    )


def _record_log_cleanup_helper_stack(
    ctx: RunContext,
    spec: Mapping[str, Any],
    stack_id: str,
    status: str,
) -> None:
    if not stack_id.startswith(_helper_stack_id_prefix(ctx, spec)):
        raise RuntimeError(f"Cleanup helper returned an invalid stack ID: {stack_id}")
    with ctx.state_lock:
        record = _prepare_log_cleanup_helper_record(ctx, spec)
        active_stack_id = str(record.get("active_stack_id") or "")
        if active_stack_id and active_stack_id != stack_id:
            raise RuntimeError("Cleanup helper stack generation changed without absence proof")
        history = record.setdefault("stack_history", [])
        if not isinstance(history, list):
            raise RuntimeError("Checkpoint cleanup helper stack_history must be a list")
        if not any(item.get("stack_id") == stack_id for item in history if isinstance(item, dict)):
            history.append({"stack_id": stack_id, "first_observed_at": utc_now()})
        record["active_stack_id"] = stack_id
        record["lifecycle"] = status
        record["last_observed_at"] = utc_now()
        ctx.persist_callback(ctx.checkpoint)


def _mark_log_cleanup_helper_absent(
    ctx: RunContext,
    stack_id: str | None,
) -> None:
    with ctx.state_lock:
        record = ctx.checkpoint.state.get("log_cleanup_helper")
        if not isinstance(record, dict):
            return
        active_stack_id = str(record.get("active_stack_id") or "")
        if stack_id and active_stack_id and active_stack_id != stack_id:
            raise RuntimeError("Cleanup helper absence proof refers to a different stack")
        record["active_stack_id"] = None
        record["lifecycle"] = "deleted"
        record["last_deleted_stack_id"] = stack_id
        record["deleted_at"] = utc_now()
        ctx.persist_callback(ctx.checkpoint)


def _template_document(template_body: Any) -> dict[str, Any]:
    if isinstance(template_body, str):
        try:
            template_body = json.loads(template_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Cleanup helper template is not canonical JSON") from exc
    if not isinstance(template_body, dict):
        raise RuntimeError("Cleanup helper template is not a JSON object")
    return template_body


def _validate_log_cleanup_helper_stack(
    ctx: RunContext,
    spec: Mapping[str, Any],
    stack: Mapping[str, Any],
) -> str:
    stack_id = str(stack.get("stack_id") or "")
    if (
        stack.get("name") != spec["stack_name"]
        or not stack_id.startswith(_helper_stack_id_prefix(ctx, spec))
        or stack.get("termination_protection")
    ):
        raise RuntimeError("Cleanup helper CloudFormation identity is invalid")
    tags = stack.get("tags") or {}
    if (
        tags.get(_LOG_CLEANUP_HELPER_RUN_TAG) != spec["run_id"]
        or tags.get(_LOG_CLEANUP_HELPER_TOKEN_TAG) != spec["cleanup_token"]
        or tags.get("gco:project") is not None
        or tags.get("Project") is not None
    ):
        raise RuntimeError("Cleanup helper CloudFormation tags are invalid")
    cfn = ctx.session.client("cloudformation", region_name=spec["region"])
    body = _template_document(
        cfn.get_template(StackName=stack_id, TemplateStage="Original").get("TemplateBody")
    )
    observed_hash = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
    if observed_hash != spec["template_sha256"]:
        raise RuntimeError("Cleanup helper CloudFormation template changed")
    return stack_id


def _validate_log_cleanup_helper_role(
    ctx: RunContext,
    spec: Mapping[str, Any],
    helper_record: dict[str, Any],
    stack_id: str,
) -> dict[str, str]:
    iam = ctx.session.client("iam", region_name=spec["region"])
    role = iam.get_role(RoleName=spec["role_name"]).get("Role")
    if not isinstance(role, dict):
        raise RuntimeError("IAM omitted the cleanup helper role")
    tags = {
        str(item.get("Key")): str(item.get("Value") or "")
        for item in role.get("Tags", [])
        if item.get("Key") is not None
    }
    if (
        str(role.get("RoleName") or "") != spec["role_name"]
        or str(role.get("Arn") or "") != spec["role_arn"]
        or str(role.get("Path") or "") != "/"
        or int(role.get("MaxSessionDuration") or 0) != 3600
        or role.get("AssumeRolePolicyDocument") != spec["trust_policy"]
        or tags.get(_LOG_CLEANUP_ROLE_RUN_TAG) != spec["run_id"]
        or tags.get(_LOG_CLEANUP_ROLE_TOKEN_TAG) != spec["cleanup_token"]
    ):
        raise RuntimeError("Cleanup helper IAM role identity changed")
    inline = iam.list_role_policies(RoleName=spec["role_name"])
    if inline.get("IsTruncated") or inline.get("PolicyNames") != [_LOG_CLEANUP_ROLE_POLICY_NAME]:
        raise RuntimeError("Cleanup helper IAM inline policies changed")
    role_policy = iam.get_role_policy(
        RoleName=spec["role_name"],
        PolicyName=_LOG_CLEANUP_ROLE_POLICY_NAME,
    ).get("PolicyDocument")
    if role_policy != spec["role_policy"]:
        raise RuntimeError("Cleanup helper IAM delete policy changed")
    attached = iam.list_attached_role_policies(RoleName=spec["role_name"])
    if attached.get("IsTruncated") or attached.get("AttachedPolicies"):
        raise RuntimeError("Cleanup helper IAM role gained a managed policy")
    created = role.get("CreateDate")
    identity = {
        "arn": str(role["Arn"]),
        "role_id": str(role.get("RoleId") or ""),
        "created_at": created.isoformat() if created is not None else "",
    }
    if not identity["role_id"] or not identity["created_at"]:
        raise RuntimeError("IAM omitted immutable cleanup role identity")
    history = helper_record.get("stack_history")
    if not isinstance(history, list):
        raise RuntimeError("Checkpoint cleanup helper stack_history must be a list")
    generation = next(
        (
            item
            for item in history
            if isinstance(item, dict) and str(item.get("stack_id") or "") == stack_id
        ),
        None,
    )
    if generation is None:
        raise RuntimeError("Cleanup role identity has no exact helper stack generation")
    observed = generation.get("observed_role_identity")
    if observed is None:
        generation["observed_role_identity"] = identity
        ctx.persist()
    elif observed != identity:
        raise RuntimeError("Cleanup helper IAM role generation changed within its stack")
    return identity


def _wait_for_log_cleanup_helper(
    ctx: RunContext,
    spec: Mapping[str, Any],
    stack_id: str,
    *,
    deleting: bool,
) -> dict[str, Any] | None:
    for _attempt in range(_LOG_CLEANUP_STACK_POLL_ATTEMPTS):
        stack = describe_stack(ctx.session, str(spec["region"]), stack_id)
        status = str((stack or {}).get("status") or "")
        if deleting and (stack is None or status == "DELETE_COMPLETE"):
            return None
        if not deleting and stack is not None and status == "CREATE_COMPLETE":
            return stack
        if status == "DELETE_FAILED":
            raise RuntimeError(f"Cleanup helper stack deletion failed: {stack_id}")
        if not deleting and stack is not None and not status.endswith("_IN_PROGRESS"):
            raise RuntimeError(f"Cleanup helper stack creation ended in {status}: {stack_id}")
        time.sleep(_LOG_CLEANUP_STACK_POLL_SECONDS)
    operation = "deletion" if deleting else "creation"
    raise RuntimeError(f"Cleanup helper stack {operation} timed out: {stack_id}")


def _current_cleanup_trusted_principal(ctx: RunContext) -> tuple[str, dict[str, str]]:
    identity = ctx.session.client("sts", region_name=ctx.config.global_region).get_caller_identity()
    account = str(identity.get("Account") or "")
    caller_arn = str(identity.get("Arn") or "")
    if account != ctx.settings.expected_account:
        raise RuntimeError("Cleanup helper caller account changed")
    return caller_arn, _cleanup_principal_identity(ctx, caller_arn)


def _ensure_log_cleanup_helper(ctx: RunContext) -> dict[str, Any]:
    records = ctx.checkpoint.state.get("owned_log_groups", [])
    if not isinstance(records, list):
        raise RuntimeError("Checkpoint owned_log_groups must be a list")
    if not records or all(bool(record.get("deleted")) for record in records):
        return {"needed": False}
    spec = _log_cleanup_helper_spec(ctx)
    if spec is None:
        return {"needed": False}
    helper_record = _prepare_log_cleanup_helper_record(ctx, spec)
    caller_arn, current_principal = _current_cleanup_trusted_principal(ctx)
    if (
        current_principal["arn"] != spec["trusted_principal_arn"]
        or current_principal["principal_id"] != spec["trusted_principal_id"]
    ):
        raise RuntimeError("Cleanup helper caller principal changed since authority creation")

    region = str(spec["region"])
    cfn = ctx.session.client("cloudformation", region_name=region)
    stack: dict[str, Any] | None = None
    active_stack_id = str(helper_record.get("active_stack_id") or "")
    if active_stack_id:
        stack = describe_stack(ctx.session, region, active_stack_id)
        if stack is not None and stack.get("status") == "DELETE_COMPLETE":
            _mark_log_cleanup_helper_absent(ctx, active_stack_id)
            active_stack_id = ""
            stack = None
    if stack is None:
        named_stack = describe_stack(ctx.session, region, str(spec["stack_name"]))
        if named_stack is not None and named_stack.get("status") != "DELETE_COMPLETE":
            named_stack_id = _validate_log_cleanup_helper_stack(ctx, spec, named_stack)
            if active_stack_id and named_stack_id != active_stack_id:
                raise RuntimeError("A different cleanup helper stack generation appeared")
            _record_log_cleanup_helper_stack(
                ctx,
                spec,
                named_stack_id,
                str(named_stack.get("status") or ""),
            )
            active_stack_id = named_stack_id
            stack = named_stack
    if stack is not None and stack.get("status") == "DELETE_IN_PROGRESS":
        _wait_for_log_cleanup_helper(ctx, spec, active_stack_id, deleting=True)
        _mark_log_cleanup_helper_absent(ctx, active_stack_id)
        active_stack_id = ""
        stack = None

    if stack is None:
        with ctx.state_lock:
            helper_record = _prepare_log_cleanup_helper_record(ctx, spec)
            helper_record["create_sequence"] = int(helper_record.get("create_sequence") or 0) + 1
            sequence = helper_record["create_sequence"]
            helper_record["lifecycle"] = "create-intent"
            helper_record["create_intent_at"] = utc_now()
            ctx.persist_callback(ctx.checkpoint)
        token = f"live-validation-{spec['stack_name']}-{sequence}"
        try:
            response = cfn.create_stack(
                StackName=spec["stack_name"],
                TemplateBody=spec["template_body"],
                Capabilities=["CAPABILITY_NAMED_IAM"],
                ClientRequestToken=token[:128],
                EnableTerminationProtection=False,
                OnFailure="ROLLBACK",
                TimeoutInMinutes=10,
                Tags=[
                    {"Key": _LOG_CLEANUP_HELPER_RUN_TAG, "Value": spec["run_id"]},
                    {"Key": _LOG_CLEANUP_HELPER_TOKEN_TAG, "Value": spec["cleanup_token"]},
                ],
            )
            active_stack_id = str(response.get("StackId") or "")
            _record_log_cleanup_helper_stack(ctx, spec, active_stack_id, "CREATE_IN_PROGRESS")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "AlreadyExistsException":
                raise
            stack = describe_stack(ctx.session, region, str(spec["stack_name"]))
            if stack is None:
                raise RuntimeError("Cleanup helper name exists but cannot be described") from exc
            active_stack_id = _validate_log_cleanup_helper_stack(ctx, spec, stack)
            _record_log_cleanup_helper_stack(
                ctx,
                spec,
                active_stack_id,
                str(stack.get("status") or ""),
            )

    stack = _wait_for_log_cleanup_helper(ctx, spec, active_stack_id, deleting=False)
    if stack is None:
        raise RuntimeError("Cleanup helper disappeared after creation")
    _validate_log_cleanup_helper_stack(ctx, spec, stack)
    outputs = stack.get("outputs") or {}
    if outputs.get(_LOG_CLEANUP_ROLE_OUTPUT) != spec["role_arn"]:
        raise RuntimeError("Cleanup helper role output changed")
    helper_record = _prepare_log_cleanup_helper_record(ctx, spec)
    _validate_log_cleanup_helper_role(ctx, spec, helper_record, active_stack_id)
    _record_log_cleanup_helper_stack(ctx, spec, active_stack_id, "CREATE_COMPLETE")
    return {
        "needed": True,
        "region": region,
        "stack_id": active_stack_id,
        "stack_name": spec["stack_name"],
        "role_arn": spec["role_arn"],
        "partition": spec["partition"],
        "caller_arn": caller_arn,
        "trusted_principal_arn": spec["trusted_principal_arn"],
        "session_policy": spec["role_policy"],
        "external_id": spec["cleanup_token"],
    }


def _delete_log_cleanup_helper(ctx: RunContext) -> dict[str, Any]:
    helper_record = ctx.checkpoint.state.get("log_cleanup_helper")
    if helper_record is None:
        return {"needed": False, "deleted": True}
    if not isinstance(helper_record, dict):
        raise RuntimeError("Checkpoint log_cleanup_helper must be an object")
    spec = _log_cleanup_helper_spec(ctx)
    if spec is None:
        raise RuntimeError("Cleanup helper exists without log-group authority records")
    helper_record = _prepare_log_cleanup_helper_record(ctx, spec)
    region = str(spec["region"])
    cfn = ctx.session.client("cloudformation", region_name=region)
    active_stack_id = str(helper_record.get("active_stack_id") or "")
    stack = describe_stack(ctx.session, region, active_stack_id) if active_stack_id else None
    if stack is not None and stack.get("status") == "DELETE_COMPLETE":
        stack = None
    if stack is None:
        named_stack = describe_stack(ctx.session, region, str(spec["stack_name"]))
        if named_stack is not None and named_stack.get("status") != "DELETE_COMPLETE":
            named_stack_id = _validate_log_cleanup_helper_stack(ctx, spec, named_stack)
            if active_stack_id and named_stack_id != active_stack_id:
                raise RuntimeError("Refusing to delete a replacement cleanup helper stack")
            active_stack_id = named_stack_id
            stack = named_stack
            _record_log_cleanup_helper_stack(
                ctx,
                spec,
                active_stack_id,
                str(stack.get("status") or ""),
            )
    if stack is None:
        _mark_log_cleanup_helper_absent(ctx, active_stack_id or None)
        return {
            "needed": bool(active_stack_id),
            "deleted": True,
            "already_absent": True,
            "stack_id": active_stack_id or None,
        }

    active_stack_id = _validate_log_cleanup_helper_stack(ctx, spec, stack)
    status = str(stack.get("status") or "")
    if status == "CREATE_COMPLETE":
        _validate_log_cleanup_helper_role(ctx, spec, helper_record, active_stack_id)
    if status != "DELETE_IN_PROGRESS":
        with ctx.state_lock:
            helper_record["lifecycle"] = "delete-intent"
            helper_record["delete_intent_at"] = utc_now()
            ctx.persist_callback(ctx.checkpoint)
        cfn.delete_stack(
            StackName=active_stack_id,
            ClientRequestToken=(
                f"delete-{spec['stack_name']}-{active_stack_id.rsplit('/', 1)[-1]}"[:128]
            ),
        )
        helper_record["lifecycle"] = "DELETE_IN_PROGRESS"
        ctx.persist()
    _wait_for_log_cleanup_helper(ctx, spec, active_stack_id, deleting=True)
    replacement = describe_stack(ctx.session, region, str(spec["stack_name"]))
    if replacement is not None and replacement.get("status") != "DELETE_COMPLETE":
        raise RuntimeError("Cleanup helper stack name was replaced during deletion")
    _mark_log_cleanup_helper_absent(ctx, active_stack_id)
    return {"needed": True, "deleted": True, "stack_id": active_stack_id}


def _checkpoint_owned_log_groups(ctx: RunContext) -> list[dict[str, Any]]:
    """Fence, tag, and checkpoint exact generations while source stacks are live."""
    target_regions = ctx.checkpoint.state.get("target_stack_regions")
    if not isinstance(target_regions, dict):
        raise RuntimeError("Checkpoint target_stack_regions must be an object")
    try:
        run_started_ms = int(datetime.fromisoformat(ctx.checkpoint.created_at).timestamp() * 1000)
    except ValueError as exc:
        raise RuntimeError("Checkpoint created_at is not a valid timestamp") from exc

    with ctx.state_lock:
        cleanup_token = str(ctx.checkpoint.state.get("log_group_cleanup_token") or "")
        if not cleanup_token:
            cleanup_token = uuid.uuid4().hex
            ctx.checkpoint.state["log_group_cleanup_token"] = cleanup_token
        if not re.fullmatch(r"[0-9a-f]{32}", cleanup_token):
            raise RuntimeError("Checkpoint log-group cleanup token is malformed")
        records = ctx.checkpoint.state.setdefault("owned_log_groups", [])
        if not isinstance(records, list):
            raise RuntimeError("Checkpoint owned_log_groups must be a list")
        by_identity = {
            (str(item.get("region") or ""), str(item.get("name") or "")): item
            for item in records
            if isinstance(item, dict)
        }
        authority_tags = {
            _RUN_STACK_TAG: ctx.settings.run_id,
            _LOG_CLEANUP_TOKEN_TAG: cleanup_token,
        }
        for stack_name, raw_region in sorted(target_regions.items()):
            region = str(raw_region)
            stack_record = _owned_stack_record(ctx, region, str(stack_name))
            if stack_record is None:
                continue
            live_stack = describe_stack(ctx.session, region, stack_record["stack_id"])
            if (
                live_stack is None
                or str(live_stack.get("status") or "").startswith("DELETE")
                or (live_stack.get("tags") or {}).get(_RUN_STACK_TAG) != ctx.settings.run_id
            ):
                # Destructive authority is never created or completed from a
                # deleted-stack tombstone. Existing pre-destroy records remain usable.
                continue
            # A rolled-back create leaves log groups behind: retained LogGroup
            # resources, Lambda-created default groups, and EKS control-plane
            # groups all survive resource deletion. Their stack resources read
            # DELETE_COMPLETE while the stack itself is still describable, so
            # rollback statuses widen the resource filter to those tombstones.
            rolled_back = str(live_stack.get("status") or "") in {
                "ROLLBACK_COMPLETE",
                "ROLLBACK_FAILED",
                "UPDATE_ROLLBACK_COMPLETE",
                "UPDATE_ROLLBACK_FAILED",
            }
            allowed_resource_statuses = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
            if rolled_back:
                allowed_resource_statuses |= {"DELETE_COMPLETE", "DELETE_FAILED", "DELETE_SKIPPED"}
            cfn = ctx.session.client("cloudformation", region_name=region)
            pages = cfn.get_paginator("list_stack_resources").paginate(
                StackName=stack_record["stack_id"]
            )
            resources = [
                item
                for page in pages
                for item in page.get("StackResourceSummaries", [])
                if str(item.get("ResourceType") or "") in _LOG_GROUP_SOURCE_TYPES
                and item.get("LogicalResourceId")
                and item.get("PhysicalResourceId")
                and str(item.get("ResourceStatus") or "") in allowed_resource_statuses
            ]
            logs = ctx.session.client("logs", region_name=region)
            lambda_client = ctx.session.client("lambda", region_name=region)
            for resource in resources:
                resource_type = str(resource["ResourceType"])
                physical_id = str(resource["PhysicalResourceId"])
                source_service_identity = None
                if resource_type == "AWS::EKS::Cluster":
                    source_service_identity = _eks_cluster_log_authority_identity(
                        ctx,
                        region,
                        physical_id,
                        allow_deleted=rolled_back,
                    )
                names = _derived_log_group_names(resource_type, physical_id)
                if resource_type == "AWS::Lambda::Function":
                    default_name = f"/aws/lambda/{physical_id}"
                    try:
                        function = lambda_client.get_function_configuration(
                            FunctionName=physical_id
                        )
                    except ClientError as exc:
                        error_code = exc.response.get("Error", {}).get("Code", "")
                        if not (rolled_back and error_code == "ResourceNotFoundException"):
                            raise
                        # The rolled-back function is gone; only its default
                        # log group can remain.
                        names = (default_name,)
                    else:
                        configured_name = str(
                            (function.get("LoggingConfig") or {}).get("LogGroup") or default_name
                        )
                        names = (default_name,) if configured_name == default_name else ()
                for name in names:
                    key = (region, name)
                    candidate = {
                        "region": region,
                        "name": name,
                        "stack_name": str(stack_name),
                        "stack_id": stack_record["stack_id"],
                        "source_resource_type": resource_type,
                        "source_logical_id": str(resource["LogicalResourceId"]),
                        "source_physical_id": physical_id,
                        "ownership_authority": "cloudformation-stack-resource-derived",
                        "authority_phase": "pre-destroy",
                        "run_tag": ctx.settings.run_id,
                        "cleanup_token": cleanup_token,
                    }
                    if source_service_identity is not None:
                        candidate["source_service_identity"] = source_service_identity
                    _validated_owned_log_group_identity(ctx, candidate)

                    previous = by_identity.get(key)
                    immutable = tuple(candidate)
                    expected_identity: Mapping[str, Any] | None = None
                    if previous is not None:
                        if any(previous.get(field) != candidate[field] for field in immutable):
                            raise RuntimeError(f"Log-group ownership changed for {region}:{name}")
                        observed = previous.get("observed_identity")
                        if not isinstance(observed, dict):
                            raise RuntimeError(
                                f"Log-group checkpoint identity is malformed for {region}:{name}"
                            )
                        expected_identity = observed

                    initial = _observe_log_group_stability(
                        logs,
                        region,
                        name,
                        expected_identity=expected_identity,
                        expected_tags=authority_tags if previous is not None else None,
                        required_present=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                        required_absent=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                    )
                    if previous is not None:
                        _record_log_group_observation(
                            ctx,
                            previous,
                            phase="checkpoint-revalidation",
                            outcome=initial,
                        )
                        if initial["status"] != "present":
                            status = (
                                "replacement-observed-during-checkpoint"
                                if initial["status"] == "replacement"
                                else "checkpoint-generation-not-stable"
                            )
                            _set_log_group_disposition(
                                ctx,
                                previous,
                                status=status,
                                phase="checkpoint-revalidation",
                                outcome=initial,
                            )
                            raise RuntimeError(
                                f"Log-group checkpoint generation is not stable for "
                                f"{region}:{name}: {initial['status']}"
                            )
                        continue

                    if initial["status"] == "absent":
                        if resource_type == "AWS::Logs::LogGroup":
                            _record_log_group_checkpoint_incident(
                                ctx,
                                candidate,
                                phase="checkpoint-explicit-group-absence",
                                outcome=initial,
                            )
                            raise RuntimeError(
                                f"CloudFormation log group is absent before teardown: "
                                f"{region}:{name}"
                            )
                        try:
                            logs.create_log_group(logGroupName=name, tags=authority_tags)
                        except ClientError as exc:
                            if (
                                exc.response.get("Error", {}).get("Code")
                                != "ResourceAlreadyExistsException"
                            ):
                                raise
                            raced = _observe_log_group_stability(
                                logs,
                                region,
                                name,
                                expected_identity=None,
                                expected_tags=None,
                                required_present=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                                required_absent=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                            )
                            _record_log_group_checkpoint_incident(
                                ctx,
                                candidate,
                                phase="checkpoint-create-race",
                                outcome=raced,
                            )
                            raise RuntimeError(
                                f"Log group appeared during checkpoint creation; refusing to "
                                f"adopt or tag it: {region}:{name}"
                            ) from exc
                        initial = _observe_log_group_stability(
                            logs,
                            region,
                            name,
                            expected_identity=None,
                            expected_tags=authority_tags,
                            required_present=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                            required_absent=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                        )
                    if initial["status"] != "present":
                        _record_log_group_checkpoint_incident(
                            ctx,
                            candidate,
                            phase="checkpoint-initial-stability",
                            outcome=initial,
                        )
                        raise RuntimeError(
                            f"Log group could not be stably checkpointed: "
                            f"{region}:{name}: {initial['status']}"
                        )

                    identity = initial.get("identity")
                    if not isinstance(identity, dict):
                        raise RuntimeError(f"Log group omitted identity: {region}:{name}")
                    if identity["creation_time"] < run_started_ms:
                        raise RuntimeError(
                            f"Log group predates this validation run: {region}:{name}"
                        )
                    tags = identity.get("tags") or {}
                    conflicting_tags = {
                        key: {"expected": value, "observed": tags.get(key)}
                        for key, value in authority_tags.items()
                        if key in tags and tags.get(key) != value
                    }
                    if conflicting_tags:
                        conflict = {
                            **copy.deepcopy(initial),
                            "status": "tag-drift",
                            "tag_drift": conflicting_tags,
                        }
                        _record_log_group_checkpoint_incident(
                            ctx,
                            candidate,
                            phase="checkpoint-authority-tag-conflict",
                            outcome=conflict,
                        )
                        raise RuntimeError(f"Log-group authority tags conflict for {region}:{name}")

                    # This read is intentionally adjacent to tag_resource. A generation
                    # change after the stable reads is fenced before any authority tags
                    # can be applied. The post-tag stable reads catch the unavoidable
                    # service-side TOCTOU without ever adopting that replacement.
                    pre_tag = _observe_log_group_stability(
                        logs,
                        region,
                        name,
                        expected_identity=identity,
                        expected_tags=None,
                        required_present=1,
                        required_absent=1,
                    )
                    if pre_tag["status"] != "present":
                        _record_log_group_checkpoint_incident(
                            ctx,
                            candidate,
                            phase="checkpoint-immediate-pre-tag",
                            outcome=pre_tag,
                        )
                        raise RuntimeError(
                            f"Log-group generation changed immediately before tagging: "
                            f"{region}:{name}: {pre_tag['status']}"
                        )
                    if any(tags.get(key) != value for key, value in authority_tags.items()):
                        logs.tag_resource(resourceArn=identity["arn"], tags=authority_tags)

                    post_tag = _observe_log_group_stability(
                        logs,
                        region,
                        name,
                        expected_identity=identity,
                        expected_tags=authority_tags,
                        required_present=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                        required_absent=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                    )
                    if post_tag["status"] != "present":
                        _record_log_group_checkpoint_incident(
                            ctx,
                            candidate,
                            phase="checkpoint-post-tag-stability",
                            outcome=post_tag,
                        )
                        raise RuntimeError(
                            f"Log-group generation or authority changed while checkpointing: "
                            f"{region}:{name}: {post_tag['status']}"
                        )
                    final_identity = post_tag.get("identity")
                    if not isinstance(final_identity, dict):
                        raise RuntimeError(
                            f"Log group omitted its post-tag identity: {region}:{name}"
                        )
                    candidate["observed_identity"] = final_identity
                    candidate["checkpoint_observations"] = {
                        "initial": initial,
                        "immediate_pre_tag": pre_tag,
                        "post_tag": post_tag,
                    }
                    candidate["original_generation_disposition"] = {
                        "status": "checkpointed-present",
                        "phase": "pre-destroy",
                        "recorded_at": utc_now(),
                        "original_identity": copy.deepcopy(final_identity),
                    }
                    records.append(candidate)
                    by_identity[key] = candidate
        ctx.persist_callback(ctx.checkpoint)
        return copy.deepcopy(records)


def _checkpoint_retained_kms_keys(ctx: RunContext) -> list[dict[str, Any]]:
    """Capture every exact stack-owned KMS key plus teardown log-group candidates."""
    _checkpoint_owned_log_groups(ctx)
    owned_stacks = _owned_stacks(ctx)
    target_regions = ctx.checkpoint.state.get("target_stack_regions")
    if not isinstance(target_regions, dict):
        raise RuntimeError("Checkpoint target_stack_regions must be an object")
    with ctx.state_lock:
        records = ctx.checkpoint.state.setdefault("owned_kms_keys", [])
        if not isinstance(records, list):
            raise RuntimeError("Checkpoint owned_kms_keys must be a list")
        by_arn = {str(item.get("arn") or ""): item for item in records if isinstance(item, dict)}

        for stack_name, raw_region in sorted(target_regions.items()):
            region = str(raw_region)
            stack_record = owned_stacks.get(region, {}).get(str(stack_name))
            if stack_record is None:
                continue
            live_stack = describe_stack(ctx.session, region, stack_record["stack_id"])
            live_source_authority = (
                live_stack is not None
                and not str(live_stack.get("status") or "").startswith("DELETE")
                and live_stack.get("stack_id") == stack_record["stack_id"]
                and (live_stack.get("tags") or {}).get(_RUN_STACK_TAG) == ctx.settings.run_id
            )
            cfn = ctx.session.client("cloudformation", region_name=region)
            try:
                pages = cfn.get_paginator("list_stack_resources").paginate(
                    StackName=stack_record["stack_id"]
                )
                matching_resources = [
                    item
                    for page in pages
                    for item in page.get("StackResourceSummaries", [])
                    if item.get("ResourceType") == "AWS::KMS::Key"
                    and item.get("LogicalResourceId")
                    and item.get("PhysicalResourceId")
                ]
            except ClientError as exc:
                if (
                    exc.response.get("Error", {}).get("Code") == "ValidationError"
                    and live_stack is None
                ):
                    continue
                raise
            retained_resources = [
                item
                for item in matching_resources
                if str(stack_name) == f"{ctx.config.project_name}-{region}"
                and str(item.get("LogicalResourceId") or "") == _EKS_KEY_LOGICAL_ID
            ]
            if (
                live_stack is not None
                and live_stack.get("status") in _HEALTHY_STACK_STATUSES
                and str(stack_name) == f"{ctx.config.project_name}-{region}"
                and len(retained_resources) != 1
            ):
                raise RuntimeError(
                    f"Expected one retained EKS KMS key in {stack_name}; found "
                    f"{len(retained_resources)}"
                )

            for resource in matching_resources:
                key_id = str(resource["PhysicalResourceId"])
                logical_id = str(resource["LogicalResourceId"])
                partition = ctx.session.get_partition_for_region(region)
                if not partition:
                    raise RuntimeError(f"Could not resolve AWS partition for KMS key in {region}")
                derived_arn = (
                    f"arn:{partition}:kms:{region}:{ctx.settings.expected_account}:key/{key_id}"
                )
                previous = by_arn.get(derived_arn)
                if previous is None and not live_source_authority:
                    # Deleted-stack tombstones may reconcile exact records that
                    # were persisted pre-destroy, but can never create authority.
                    continue
                kms = ctx.session.client("kms", region_name=region)
                try:
                    metadata = kms.describe_key(KeyId=key_id).get("KeyMetadata", {})
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "NotFoundException":
                        raise
                    if previous is not None:
                        previous["scheduled"] = True
                        previous["deleted"] = True
                    continue
                arn = str(metadata.get("Arn") or "")
                tags = _kms_tags(kms, key_id)
                if tags.get(_RUN_STACK_TAG) != ctx.settings.run_id:
                    raise RuntimeError(
                        f"KMS key {arn or key_id} lacks the exact live-validation run tag"
                    )
                cleanup_policy = (
                    "harness-schedule"
                    if str(stack_name) == f"{ctx.config.project_name}-{region}"
                    and logical_id == _EKS_KEY_LOGICAL_ID
                    else "cloudformation-delete"
                )
                deletion_date = metadata.get("DeletionDate")
                state = str(metadata.get("KeyState") or "")
                candidate = {
                    "region": region,
                    "key_id": key_id,
                    "arn": arn,
                    "stack_name": str(stack_name),
                    "stack_id": stack_record["stack_id"],
                    "logical_id": logical_id,
                    "ownership_authority": "cloudformation-stack-resource",
                    "cleanup_policy": cleanup_policy,
                    "run_tag": ctx.settings.run_id,
                    "scheduled": state == "PendingDeletion",
                    "deletion_date": (
                        deletion_date.isoformat() if deletion_date is not None else None
                    ),
                }
                _validated_owned_kms_identity(ctx, candidate)
                if arn != derived_arn:
                    raise RuntimeError(f"KMS returned an unexpected ARN for {key_id}: {arn}")
                previous = by_arn.get(arn)
                if previous is not None:
                    previous.setdefault("cleanup_policy", cleanup_policy)
                    for key in (
                        "region",
                        "key_id",
                        "arn",
                        "stack_name",
                        "stack_id",
                        "logical_id",
                        "ownership_authority",
                        "cleanup_policy",
                        "run_tag",
                    ):
                        if previous.get(key) != candidate[key]:
                            raise RuntimeError(f"KMS ownership changed for {arn}: {key}")
                    if candidate["scheduled"]:
                        previous["scheduled"] = True
                        previous["deletion_date"] = candidate["deletion_date"]
                    continue
                if not arn:
                    raise RuntimeError(f"KMS key {key_id} omitted its ARN")
                refreshed_stack = describe_stack(ctx.session, region, stack_record["stack_id"])
                if not (
                    refreshed_stack is not None
                    and not str(refreshed_stack.get("status") or "").startswith("DELETE")
                    and refreshed_stack.get("stack_id") == stack_record["stack_id"]
                    and (refreshed_stack.get("tags") or {}).get(_RUN_STACK_TAG)
                    == ctx.settings.run_id
                ):
                    continue
                records.append(candidate)
                by_arn[arn] = candidate
                ctx.persist_callback(ctx.checkpoint)
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
        expected_account=ctx.settings.expected_account,
        project_name=ctx.config.project_name,
        seed_region=ctx.config.global_region,
        validation_run_id=ctx.settings.run_id,
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


_ADDON_EXECUTION_FIELDS = frozenset(
    {
        "execution_arn",
        "state_machine_arn",
        "deployment_token",
        "cluster_name",
        "region",
        "input_sha256",
        "started_at",
    }
)
_ADDON_REQUIRED_INPUT_FIELDS = frozenset(
    {
        "ClusterName",
        "Region",
        "RegistryRegion",
        "ProjectName",
        "EnabledCharts",
        "Charts",
        "KedaOperatorRoleArn",
        "ImageReplacements",
        "DeploymentToken",
    }
)
_ADDON_OPTIONAL_INPUT_FIELDS = frozenset({"EndpointGroupArn"})
_ADDON_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"})
_ADDON_FAILURE_STATUSES = _ADDON_TERMINAL_STATUSES - {"SUCCEEDED"}
_ADDON_CONVERGENCE_TIMEOUT_SECONDS = 2 * 60 * 60
_HEALTH_STABILITY_ROUNDS = 3
_MAX_TOPOLOGY_EVIDENCE_CHARS = 2048


def _bounded_topology_evidence(
    value: Any,
    limit: int = _MAX_TOPOLOGY_EVIDENCE_CHARS,
) -> str:
    """Serialize diagnostic evidence without allowing an unbounded checkpoint."""
    if value is None:
        text = "<absent>"
    elif isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(to_jsonable(value), sort_keys=True)
        except TypeError, ValueError:
            text = str(value)
    suffix = "... [truncated]"
    if len(text) <= limit:
        return text
    return text[: limit - len(suffix)] + suffix


def _topology_json_object(
    value: Any,
    description: str,
    *,
    canonical: bool,
) -> dict[str, Any]:
    """Decode a JSON object, optionally requiring the provider's exact encoding."""
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{description} is not a non-empty JSON string")

    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-standard JSON constant {constant}")

    try:
        parsed = json.loads(value, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"{description} is invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{description} must be a JSON object")
    if canonical and json.dumps(parsed, sort_keys=True, separators=(",", ":")) != value:
        raise RuntimeError(f"{description} is not exact canonical JSON")
    return parsed


def _decode_replay_input_parameter(stored_value: str, description: str) -> str:
    """Reverse the helm orchestrator's zlib+base64 replay-input encoding.

    The orchestrator stores the convergence execution input encoded because
    SSM rejects raw ``{{PLACEHOLDER}}`` tokens. ``input_sha256`` in the
    companion ``_execution`` parameter is always computed over the decoded
    canonical JSON returned here.
    """
    try:
        compressed = base64.b64decode(stored_value.encode("ascii"), validate=True)
        return zlib.decompress(compressed).decode("utf-8")
    except (ValueError, zlib.error, UnicodeDecodeError) as exc:
        raise RuntimeError(f"{description} is not zlib+base64 replay input: {exc}") from exc


def _ssm_string_parameter(client: Any, name: str) -> str:
    """Read one exact String parameter and reject a malformed SDK response."""
    response = client.get_parameter(Name=name)
    parameter = response.get("Parameter") if isinstance(response, dict) else None
    if not isinstance(parameter, dict):
        raise RuntimeError(f"SSM parameter response is malformed for {name}")
    if parameter.get("Name") != name or parameter.get("Type") != "String":
        raise RuntimeError(f"SSM parameter identity/type is invalid for {name}")
    value = parameter.get("Value")
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"SSM parameter has no String value: {name}")
    return value


def _epoch_seconds(value: Any, description: str) -> int:
    """Normalize an SDK timestamp while rejecting booleans and invalid values."""
    if isinstance(value, datetime):
        raw_value: Any = value.timestamp()
    else:
        raw_value = value
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
        raise RuntimeError(f"{description} is not a timestamp")
    try:
        result = int(raw_value)
    except (OverflowError, ValueError) as exc:
        raise RuntimeError(f"{description} is not a finite timestamp") from exc
    if result <= 0:
        raise RuntimeError(f"{description} must be positive")
    return result


def _validate_addon_arns(
    ctx: RunContext,
    *,
    region: str,
    stack_name: str,
    stack_id: str,
    state_machine_arn: str,
    execution_arn: str,
) -> None:
    """Require exact account, partition, Region, and parent state-machine ARNs."""
    partition = ctx.session.get_partition_for_region(region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for {region}")
    escaped_partition = re.escape(str(partition))
    escaped_region = re.escape(region)
    escaped_account = re.escape(ctx.settings.expected_account)
    escaped_stack_name = re.escape(stack_name)

    stack_pattern = (
        rf"arn:{escaped_partition}:cloudformation:{escaped_region}:{escaped_account}:"
        rf"stack/{escaped_stack_name}/[^/:\s]+"
    )
    if re.fullmatch(stack_pattern, stack_id) is None:
        raise RuntimeError(f"Regional stack ID has the wrong ARN identity: {stack_id}")

    state_machine_pattern = (
        rf"arn:{escaped_partition}:states:{escaped_region}:{escaped_account}:"
        r"stateMachine:([^:\s]+)"
    )
    state_machine_match = re.fullmatch(state_machine_pattern, state_machine_arn)
    if state_machine_match is None:
        raise RuntimeError(
            f"Add-on state-machine ARN has the wrong account/partition/Region: {state_machine_arn}"
        )
    execution_pattern = (
        rf"arn:{escaped_partition}:states:{escaped_region}:{escaped_account}:"
        rf"execution:{re.escape(state_machine_match.group(1))}:[^:\s]+"
    )
    if re.fullmatch(execution_pattern, execution_arn) is None:
        raise RuntimeError(
            f"Add-on execution ARN is not an execution of {state_machine_arn}: {execution_arn}"
        )


def _state_machine_stack_resource(
    ctx: RunContext,
    *,
    region: str,
    stack_id: str,
    state_machine_arn: str,
) -> dict[str, str]:
    """Prove the physical state machine belongs to the exact regional stack ARN."""
    cloudformation = ctx.session.client("cloudformation", region_name=region)
    pages = cloudformation.get_paginator("list_stack_resources").paginate(StackName=stack_id)
    matches = [
        resource
        for page in pages
        for resource in page.get("StackResourceSummaries", [])
        if resource.get("ResourceType") == "AWS::StepFunctions::StateMachine"
        and resource.get("PhysicalResourceId") == state_machine_arn
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"State machine {state_machine_arn} is not exactly one "
            f"AWS::StepFunctions::StateMachine resource in stack {stack_id}"
        )
    resource = matches[0]
    logical_id = resource.get("LogicalResourceId")
    resource_status = resource.get("ResourceStatus")
    if not isinstance(logical_id, str) or not logical_id:
        raise RuntimeError(f"State-machine stack resource lacks a logical ID in {stack_id}")
    if resource_status not in _HEALTHY_STACK_STATUSES:
        raise RuntimeError(
            f"State-machine stack resource {logical_id} is not complete: {resource_status}"
        )
    return {
        "logical_id": logical_id,
        "physical_id": state_machine_arn,
        "resource_type": "AWS::StepFunctions::StateMachine",
        "status": str(resource_status),
    }


def _validate_terminal_validator(
    output: dict[str, Any],
    *,
    key: str,
    deployment_token: str,
    count_pairs: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    """Validate one terminal convergence payload and each expected/actual count pair."""
    validator = output.get(key)
    if not isinstance(validator, dict):
        raise RuntimeError(f"Step Functions output lacks object {key}")
    if validator.get("status") != "validated":
        raise RuntimeError(f"Step Functions output {key}.status is not exactly 'validated'")
    if validator.get("DeploymentToken") != deployment_token:
        raise RuntimeError(f"Step Functions output {key} has a stale deployment token")
    for expected_key, validated_key in count_pairs:
        expected = validator.get(expected_key)
        validated = validator.get(validated_key)
        if (
            isinstance(expected, bool)
            or isinstance(validated, bool)
            or not isinstance(expected, int)
            or not isinstance(validated, int)
            or expected < 0
            or validated < 0
        ):
            raise RuntimeError(
                f"Step Functions output {key} has invalid counts {expected_key}/{validated_key}"
            )
        if expected != validated:
            raise RuntimeError(
                f"Step Functions output {key} did not validate every item: "
                f"{expected_key}={expected}, {validated_key}={validated}"
            )
    return validator


def _validate_addon_execution_input(
    input_value: dict[str, Any],
    *,
    cluster_name: str,
    region: str,
    registry_region: str,
    project_name: str,
    deployment_token: str,
) -> None:
    """Require the exact current orchestrator input schema and regional identity."""
    fields = set(input_value)
    if not _ADDON_REQUIRED_INPUT_FIELDS.issubset(fields) or not fields.issubset(
        _ADDON_REQUIRED_INPUT_FIELDS | _ADDON_OPTIONAL_INPUT_FIELDS
    ):
        raise RuntimeError("Add-on execution input does not use the exact current schema")
    if input_value.get("ClusterName") != cluster_name:
        raise RuntimeError("Add-on execution input has a stale cluster name")
    if input_value.get("Region") != region:
        raise RuntimeError("Add-on execution input has a stale Region")
    if input_value.get("RegistryRegion") != registry_region:
        raise RuntimeError("Add-on execution input has a stale registry Region")
    if input_value.get("ProjectName") != project_name:
        raise RuntimeError("Add-on execution input has a stale project name")
    if input_value.get("DeploymentToken") != deployment_token:
        raise RuntimeError("Add-on execution input has a stale deployment token")
    enabled_charts = input_value.get("EnabledCharts")
    if not isinstance(enabled_charts, list) or not all(
        isinstance(item, str) and item for item in enabled_charts
    ):
        raise RuntimeError("Add-on execution input EnabledCharts must be a string list")
    if not isinstance(input_value.get("Charts"), dict):
        raise RuntimeError("Add-on execution input Charts must be an object")
    if not isinstance(input_value.get("ImageReplacements"), dict):
        raise RuntimeError("Add-on execution input ImageReplacements must be an object")
    keda_role_arn = input_value.get("KedaOperatorRoleArn")
    if not isinstance(keda_role_arn, str | type(None)):
        raise RuntimeError("Add-on execution input KedaOperatorRoleArn must be a string or null")
    if "EndpointGroupArn" in input_value:
        endpoint_group_arn = input_value["EndpointGroupArn"]
        if not isinstance(endpoint_group_arn, str) or not endpoint_group_arn:
            raise RuntimeError("Add-on execution input EndpointGroupArn must be non-empty")


def _poll_addon_execution(
    ctx: RunContext,
    *,
    region: str,
    execution: dict[str, Any],
    input_json: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Poll one exact execution to a bounded terminal result and validate its output."""
    execution_arn = str(execution["execution_arn"])
    state_machine_arn = str(execution["state_machine_arn"])
    deployment_token = str(execution["deployment_token"])
    poll_interval = max(0.0, float(ctx.settings.poll_interval_seconds))
    deadline = time.monotonic() + _ADDON_CONVERGENCE_TIMEOUT_SECONDS + poll_interval
    stepfunctions = ctx.session.client("stepfunctions", region_name=region)

    while True:
        response = stepfunctions.describe_execution(executionArn=execution_arn)
        if not isinstance(response, dict):
            raise RuntimeError(f"DescribeExecution returned a malformed response in {region}")
        if response.get("executionArn") != execution_arn:
            raise RuntimeError(f"DescribeExecution returned a different execution in {region}")
        if response.get("stateMachineArn") != state_machine_arn:
            raise RuntimeError(f"DescribeExecution returned a different state machine in {region}")
        if response.get("input") != input_json:
            raise RuntimeError(f"DescribeExecution returned stale execution input in {region}")
        if (
            _epoch_seconds(response.get("startDate"), "DescribeExecution startDate")
            != execution["started_at"]
        ):
            raise RuntimeError(f"DescribeExecution start time changed in {region}")

        status = response.get("status")
        if not isinstance(status, str):
            raise RuntimeError(f"DescribeExecution returned no status in {region}")
        observation = {
            "observed_at": utc_now(),
            "status": status,
            "execution_arn": execution_arn,
        }
        for field in ("error", "cause", "output"):
            if field in response:
                observation[field] = _bounded_topology_evidence(response.get(field))
        evidence.setdefault("observations", []).append(observation)
        ctx.persist()

        if status == "RUNNING":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"Add-on execution {execution_arn} did not finish within "
                    f"{_ADDON_CONVERGENCE_TIMEOUT_SECONDS + poll_interval:.1f} seconds"
                )
            time.sleep(min(poll_interval if poll_interval > 0 else 0.1, remaining))
            continue
        if status not in _ADDON_TERMINAL_STATUSES:
            raise RuntimeError(f"Add-on execution {execution_arn} has unknown status {status}")

        evidence["execution_status"] = status
        if status in _ADDON_FAILURE_STATUSES:
            terminal = {
                field: _bounded_topology_evidence(response.get(field))
                for field in ("error", "cause", "output")
            }
            evidence["terminal"] = {"status": status, **terminal}
            ctx.persist()
            raise RuntimeError(
                f"Add-on execution {execution_arn} ended {status}; "
                f"error={terminal['error']}; cause={terminal['cause']}; "
                f"output={terminal['output']}"
            )

        output = _topology_json_object(
            response.get("output"),
            f"Step Functions output for {region}",
            canonical=False,
        )
        manifest_validation = _validate_terminal_validator(
            output,
            key="manifestValidation",
            deployment_token=deployment_token,
            count_pairs=(("ExpectedCount", "ValidatedCount"),),
        )
        helm_validation = _validate_terminal_validator(
            output,
            key="helmValidation",
            deployment_token=deployment_token,
            count_pairs=(
                ("expected_release_count", "validated_release_count"),
                ("expected_resource_count", "validated_resource_count"),
            ),
        )
        terminal_evidence: dict[str, Any] = {
            "status": status,
            "manifestValidation": to_jsonable(manifest_validation),
            "helmValidation": to_jsonable(helm_validation),
        }
        evidence["terminal"] = terminal_evidence
        ctx.persist()
        return terminal_evidence


def _converge_region_addons(
    ctx: RunContext,
    *,
    region: str,
    stack_name: str,
    stack: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    """Validate persisted identity and wait for exact current add-on convergence."""
    stack_id = str(stack.get("stack_id") or "")
    outputs = stack.get("outputs")
    if not isinstance(outputs, dict):
        raise RuntimeError(f"Regional stack {stack_name} has malformed outputs")
    cluster_name = f"{ctx.config.project_name}-{region}"
    if outputs.get("ClusterName") != cluster_name:
        raise RuntimeError(f"Regional stack {stack_name} has a stale ClusterName output")
    deployment_token = outputs.get("AddonDeploymentToken")
    if not isinstance(deployment_token, str) or not deployment_token:
        raise RuntimeError(f"Regional stack {stack_name} has no AddonDeploymentToken output")

    parameter_root = f"/{ctx.config.project_name}/addons/{region}"
    execution_parameter = f"{parameter_root}/_execution"
    input_parameter = f"{parameter_root}/_input"
    ssm = ctx.session.client("ssm", region_name=region)
    execution_json = _ssm_string_parameter(ssm, execution_parameter)
    input_json = _decode_replay_input_parameter(
        _ssm_string_parameter(ssm, input_parameter),
        f"SSM parameter {input_parameter}",
    )
    execution = _topology_json_object(
        execution_json,
        f"SSM parameter {execution_parameter}",
        canonical=True,
    )
    input_value = _topology_json_object(
        input_json,
        f"SSM parameter {input_parameter}",
        canonical=True,
    )

    if set(execution) != _ADDON_EXECUTION_FIELDS:
        raise RuntimeError(f"SSM parameter {execution_parameter} has an unexpected schema")
    for field in (
        "execution_arn",
        "state_machine_arn",
        "deployment_token",
        "cluster_name",
        "region",
        "input_sha256",
    ):
        if not isinstance(execution.get(field), str) or not execution[field]:
            raise RuntimeError(f"SSM parameter {execution_parameter} has invalid {field}")
    started_at = execution.get("started_at")
    if isinstance(started_at, bool) or not isinstance(started_at, int) or started_at <= 0:
        raise RuntimeError(f"SSM parameter {execution_parameter} has invalid started_at")
    if execution["deployment_token"] != deployment_token:
        raise RuntimeError(f"SSM parameter {execution_parameter} has a stale deployment token")
    if execution["cluster_name"] != cluster_name or execution["region"] != region:
        raise RuntimeError(f"SSM parameter {execution_parameter} has stale regional identity")
    input_sha256 = hashlib.sha256(input_json.encode("utf-8")).hexdigest()
    if execution["input_sha256"] != input_sha256:
        raise RuntimeError(f"SSM parameter {execution_parameter} has a stale input SHA-256")
    _validate_addon_execution_input(
        input_value,
        cluster_name=cluster_name,
        region=region,
        registry_region=ctx.config.global_region,
        project_name=ctx.config.project_name,
        deployment_token=deployment_token,
    )
    _validate_addon_arns(
        ctx,
        region=region,
        stack_name=stack_name,
        stack_id=stack_id,
        state_machine_arn=execution["state_machine_arn"],
        execution_arn=execution["execution_arn"],
    )
    stack_resource = _state_machine_stack_resource(
        ctx,
        region=region,
        stack_id=stack_id,
        state_machine_arn=execution["state_machine_arn"],
    )

    evidence.update(
        {
            "stack_id": stack_id,
            "cluster_name": cluster_name,
            "deployment_token": deployment_token,
            "execution": to_jsonable(execution),
            "input": to_jsonable(input_value),
            "input_sha256": input_sha256,
            "state_machine_resource": stack_resource,
        }
    )
    ctx.persist()
    _poll_addon_execution(
        ctx,
        region=region,
        execution=execution,
        input_json=input_json,
        evidence=evidence,
    )


def _validate_health_payload(
    ctx: RunContext,
    payload: Any,
    *,
    endpoint_region: str | None,
) -> dict[str, Any]:
    """Require a healthy, well-formed response bound to one deployed cluster."""
    if not isinstance(payload, dict):
        raise RuntimeError("health response is not a JSON object")
    if payload.get("status") != "healthy":
        raise RuntimeError("health response status is not exactly 'healthy'")
    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, str) or "T" not in timestamp:
        raise RuntimeError("health response timestamp is not an ISO date-time")
    try:
        datetime.fromisoformat(timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp)
    except ValueError as exc:
        raise RuntimeError("health response timestamp is not an ISO date-time") from exc
    payload_region = payload.get("region")
    if payload_region not in ctx.deployment_regions:
        raise RuntimeError(f"health response Region is not deployed: {payload_region!r}")
    if endpoint_region is not None and payload_region != endpoint_region:
        raise RuntimeError(
            f"regional health response came from {payload_region!r}, expected {endpoint_region!r}"
        )
    expected_cluster_id = f"{ctx.config.project_name}-{payload_region}"
    if payload.get("cluster_id") != expected_cluster_id:
        raise RuntimeError(
            f"health response cluster_id is not {expected_cluster_id!r}: "
            f"{payload.get('cluster_id')!r}"
        )
    return payload


def _health_stability_samples(
    ctx: RunContext,
    *,
    global_url: str,
    regional_urls: dict[str, str],
) -> list[dict[str, Any]]:
    """Collect three fail-fast, single-attempt rounds from every enabled endpoint."""
    probes: list[dict[str, Any]] = [
        {"scope": "global", "region": None, "endpoint": global_url},
        *(
            {"scope": "regional", "region": region, "endpoint": regional_urls[region]}
            for region in ctx.deployment_regions
            if region in regional_urls
        ),
    ]
    samples: list[dict[str, Any]] = []
    ctx.checkpoint.state["topology_health_samples"] = samples
    ctx.persist()
    interval = min(max(0.0, float(ctx.settings.poll_interval_seconds)), 5.0)

    for round_number in range(1, _HEALTH_STABILITY_ROUNDS + 1):
        for probe in probes:
            started = time.monotonic()
            payload: Any = None
            sample: dict[str, Any]
            try:
                payload = ctx.aws_client.call_api(
                    method="GET",
                    path="/api/v1/health",
                    region=probe["region"],
                    max_attempts=1,
                )
            except Exception as exc:
                sample = {
                    **probe,
                    "round": round_number,
                    "timestamp": utc_now(),
                    "latency_seconds": round(max(0.0, time.monotonic() - started), 6),
                    "payload": None,
                    "error": _bounded_topology_evidence(f"{type(exc).__name__}: {exc}"),
                }
                samples.append(sample)
                ctx.persist()
                raise RuntimeError(
                    f"Health stability call failed for {probe['endpoint']} in round "
                    f"{round_number}: {sample['error']}"
                ) from exc

            error: str | None = None
            try:
                _validate_health_payload(ctx, payload, endpoint_region=probe["region"])
            except RuntimeError as exc:
                error = _bounded_topology_evidence(str(exc))
            sample = {
                **probe,
                "round": round_number,
                "timestamp": utc_now(),
                "latency_seconds": round(max(0.0, time.monotonic() - started), 6),
                "payload": to_jsonable(payload),
                "error": error,
            }
            samples.append(sample)
            ctx.persist()
            if error is not None:
                raise RuntimeError(
                    f"Malformed health response from {probe['endpoint']} in round "
                    f"{round_number}: {error}"
                )
        if round_number < _HEALTH_STABILITY_ROUNDS and interval > 0:
            time.sleep(interval)
    return samples


def action_topology(ctx: RunContext) -> dict[str, Any]:
    """Verify deterministic add-on convergence before stable API and data-plane health."""
    _reconcile_stack_ownership(ctx)
    stack_details: dict[str, Any] = {}
    target_stack_regions = ctx.checkpoint.state["target_stack_regions"]
    for stack_name, region in target_stack_regions.items():
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

    convergence: dict[str, Any] = {
        "started_at": utc_now(),
        "status": "running",
        "regions": {},
    }
    ctx.checkpoint.state["topology_convergence"] = convergence
    ctx.persist()
    for region in ctx.deployment_regions:
        stack_name = f"{ctx.config.project_name}-{region}"
        evidence: dict[str, Any] = {
            "region": region,
            "stack_name": stack_name,
            "result": "pending",
            "observations": [],
        }
        convergence["regions"][region] = evidence
        try:
            if target_stack_regions.get(stack_name) != region:
                raise RuntimeError(
                    f"Checkpoint does not bind exact regional stack {stack_name} to {region}"
                )
            stack = stack_details.get(stack_name)
            if not isinstance(stack, dict):
                raise RuntimeError(f"Exact regional stack was not described: {region}:{stack_name}")
            _converge_region_addons(
                ctx,
                region=region,
                stack_name=stack_name,
                stack=stack,
                evidence=evidence,
            )
        except Exception as exc:
            evidence["result"] = "failed"
            evidence["completed_at"] = utc_now()
            evidence["error"] = _bounded_topology_evidence(f"{type(exc).__name__}: {exc}")
            convergence["status"] = "failed"
            convergence["completed_at"] = utc_now()
            ctx.persist()
            raise
        evidence["result"] = "succeeded"
        evidence["completed_at"] = utc_now()
        ctx.persist()
    convergence["status"] = "succeeded"
    convergence["completed_at"] = utc_now()
    ctx.persist()

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
    global_url = str(getattr(global_endpoint, "url", "") or "")
    if not global_url:
        raise RuntimeError("Global API endpoint has no URL")
    direct_regional_access = _direct_regional_access_enabled(ctx)
    regional_urls: dict[str, str] = {}
    if direct_regional_access:
        for region in ctx.deployment_regions:
            endpoint = ctx.aws_client.get_regional_api_endpoint(region, force_refresh=True)
            endpoint_url = str(getattr(endpoint, "url", "") or "")
            if not endpoint_url:
                raise RuntimeError(f"Direct regional API endpoint is absent in {region}")
            regional_urls[region] = endpoint_url

    health_samples = _health_stability_samples(
        ctx,
        global_url=global_url,
        regional_urls=regional_urls,
    )
    global_samples = [sample for sample in health_samples if sample["scope"] == "global"]
    global_api = {
        "url": global_url,
        "health": global_samples[-1]["payload"],
        "samples": global_samples,
    }
    if direct_regional_access:
        regional_endpoints = {
            region: {
                "url": regional_urls[region],
                "health": next(
                    sample["payload"]
                    for sample in reversed(health_samples)
                    if sample["region"] == region
                ),
                "samples": [sample for sample in health_samples if sample["region"] == region],
            }
            for region in ctx.deployment_regions
        }
    else:
        regional_endpoints = {
            region: {
                "skipped": True,
                "reason": "direct caller access is disabled by cdk.json",
                "samples": [],
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
        "convergence": convergence,
        "health_samples": health_samples,
        "global_api": global_api,
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


def _central_workload_identity(record: dict[str, Any]) -> tuple[str, str, str] | None:
    values = (
        record.get("k8s_job_name"),
        record.get("k8s_job_namespace"),
        record.get("k8s_job_uid"),
    )
    populated = [value is not None for value in values]
    if any(populated) and not all(populated):
        raise RuntimeError("Checkpoint contains a partial central Kubernetes identity")
    if not any(populated):
        return None
    identity = tuple(str(value or "") for value in values)
    if not all(identity):
        raise RuntimeError("Checkpoint contains an empty central Kubernetes identity field")
    return cast(tuple[str, str, str], identity)


def _effective_job_identity(record: dict[str, Any]) -> tuple[str, str]:
    if record.get("path") == "dynamodb":
        central_identity = _central_workload_identity(record)
        if central_identity is None:
            raise RuntimeError(
                "Central workload Kubernetes identity has not been bound from DynamoDB"
            )
        return central_identity[0], central_identity[1]
    return str(record["name"]), str(record["namespace"])


def _job_reference_identity(record: dict[str, Any]) -> tuple[str, str]:
    if record.get("path") == "dynamodb":
        central_identity = _central_workload_identity(record)
        if central_identity is not None:
            return central_identity[0], central_identity[1]
    return str(record["name"]), str(record["namespace"])


def _job_api_path(record: dict[str, Any], suffix: str = "") -> str:
    actual_name, actual_namespace = _effective_job_identity(record)
    namespace = quote(actual_namespace, safe="")
    name = quote(actual_name, safe="")
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


def _validate_central_workload_metadata(
    record: dict[str, Any],
    metadata: dict[str, Any],
    labels: dict[str, Any],
    uid: str,
) -> None:
    queue_job_id = str(record.get("central_queue_job_id") or "")
    expected_uid = str(record.get("k8s_job_uid") or "")
    if not queue_job_id or not expected_uid:
        raise RuntimeError("Central workload is missing immutable queue/UID authority")
    if uid != expected_uid:
        raise RuntimeError("Kubernetes Job UID differs from persisted central worker identity")
    if labels.get(_CENTRAL_MANAGED_BY_LABEL) != "central-queue":
        raise RuntimeError("Central Job managed-by label does not match the worker contract")
    expected_queue_key = hashlib.sha256(queue_job_id.encode("utf-8")).hexdigest()[:32]
    if labels.get(_CENTRAL_QUEUE_KEY_LABEL) != expected_queue_key:
        raise RuntimeError("Central Job queue-key label does not match its queue ID")
    annotations = metadata.get("annotations")
    if not isinstance(annotations, dict):
        raise RuntimeError("Central Job lookup omitted ownership annotations")
    if annotations.get(_CENTRAL_QUEUE_ID_ANNOTATION) != queue_job_id:
        raise RuntimeError("Central Job queue ID annotation does not match the checkpoint")
    if annotations.get(_CENTRAL_ORIGINAL_NAME_ANNOTATION) != record["name"]:
        raise RuntimeError("Central Job original-name annotation does not match the request")


def _get_owned_job(ctx: RunContext, record: dict[str, Any]) -> dict[str, Any] | None:
    """Return a Job only after authoritative HTTP and UID/label verification."""
    actual_name, actual_namespace = _effective_job_identity(record)
    response = ctx.aws_client.make_authenticated_request(
        method="GET",
        path=_job_api_path(record),
        target_region=record.get("transport_region"),
    )
    if response.status_code == 404:
        return None
    if not response.ok:
        raise RuntimeError(
            f"Job lookup failed for {record['region']}:{actual_namespace}/{actual_name}: "
            f"{response.status_code} {response.text}"
        )
    data = _response_json(response, "Job lookup")
    _verify_response_region(data, str(record["region"]), "Job lookup")
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        raise RuntimeError("Job lookup omitted metadata")
    if metadata.get("name") != actual_name or metadata.get("namespace") != actual_namespace:
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
    if record.get("path") == "dynamodb":
        _validate_central_workload_metadata(record, metadata, labels, uid)
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
                actual_name, actual_namespace = _effective_job_identity(record)
                raise TimeoutError(
                    f"Job {record['region']}:{actual_namespace}/{actual_name} "
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
            actual_name, actual_namespace = _effective_job_identity(record)
            raise TimeoutError(
                f"Job {record['region']}:{actual_namespace}/{actual_name} "
                f"did not complete within {ctx.settings.job_timeout_seconds}s"
            )
        time.sleep(ctx.settings.poll_interval_seconds)


def _owned_job_logs(ctx: RunContext, record: dict[str, Any], tail: int = 200) -> str:
    if _get_owned_job(ctx, record) is None:
        raise RuntimeError("Owned Job disappeared before its logs were read")
    actual_name, actual_namespace = _effective_job_identity(record)
    response = ctx.aws_client.make_authenticated_request(
        method="GET",
        path=_job_api_path(record, f"/logs?tail={tail}"),
        target_region=record.get("transport_region"),
    )
    if not response.ok:
        raise RuntimeError(f"Job log lookup failed: {response.status_code} {response.text}")
    data = _response_json(response, "Job log lookup")
    _verify_response_region(data, str(record["region"]), "Job log lookup")
    if data.get("job_name") != actual_name or data.get("namespace") != actual_namespace:
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
            actual_name, actual_namespace = _effective_job_identity(record)
            raise TimeoutError(
                f"Job {record['region']}:{actual_namespace}/{actual_name} remained visible"
            )
        time.sleep(min(5, ctx.settings.poll_interval_seconds))


def _delete_owned_job(ctx: RunContext, record: dict[str, Any]) -> dict[str, Any]:
    state = str(record.get("submission_state") or "registered")
    if record.get("path") == "dynamodb" and _central_workload_identity(record) is None:
        if state in {"registered", "prepared", "not_submitted"}:
            ctx.mark_job_not_submitted(record)
            ctx.mark_job_deleted(record)
            return {"not_submitted": True, "already_absent": True}
        raise RuntimeError(
            "Central Job submission may have escaped but no worker-persisted Kubernetes "
            "identity was bound; cleanup remains unresolved"
        )

    current = _get_owned_job(ctx, record)
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
    actual_name, actual_namespace = _effective_job_identity(record)
    if appeared is None:
        raise RuntimeError(
            f"Job {actual_namespace}/{actual_name} never appeared in {record['region']}"
        )
    final, history = _wait_for_owned_job_terminal(ctx, record)
    status = _job_status(final)
    if status != "succeeded":
        raise RuntimeError(
            f"Job {actual_namespace}/{actual_name} in {record['region']} "
            f"finished with status {status}"
        )
    logs = _owned_job_logs(ctx, record)
    if marker not in logs:
        raise RuntimeError(f"Job logs did not contain expected marker {marker!r}")
    evidence = {
        "name": actual_name,
        "namespace": actual_namespace,
        "requested_name": record["name"],
        "requested_namespace": record["namespace"],
        "region": record["region"],
        "transport_region": record.get("transport_region"),
        "uid": record.get("uid"),
        "central_queue_job_id": record.get("central_queue_job_id"),
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


def _central_queue_kubernetes_job_name(original_name: str, job_id: str) -> str:
    suffix = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]
    prefix = re.sub(r"[^a-z0-9-]+", "-", original_name.lower()).strip("-")
    prefix = prefix[: 63 - len(suffix) - 1].rstrip("-") or "gco-job"
    return f"{prefix}-{suffix}"


def _central_persisted_kubernetes_identity(
    job: dict[str, Any],
    *,
    required: bool,
) -> tuple[str, str, str] | None:
    raw = (
        job.get("k8s_job_name"),
        job.get("k8s_job_namespace"),
        job.get("k8s_job_uid"),
    )
    populated = [value is not None for value in raw]
    if not any(populated):
        if required:
            raise RuntimeError("Central DynamoDB record omitted worker Kubernetes identity")
        return None
    if not all(populated):
        raise RuntimeError("Central DynamoDB record contains a partial Kubernetes identity")
    identity = tuple(str(value or "") for value in raw)
    if not all(identity):
        raise RuntimeError("Central DynamoDB record contains an empty Kubernetes identity field")
    return cast(tuple[str, str, str], identity)


def _validate_central_checkpoint_kubernetes_identity(
    central_record: dict[str, Any],
    identity: dict[str, str],
) -> None:
    """Reject partial or conflicting central identity before mutating either record."""
    previous = {key: central_record.get(key) for key in identity}
    populated = [value is not None for value in previous.values()]
    if any(populated) and not all(populated):
        raise RuntimeError("Checkpoint central record contains a partial Kubernetes identity")
    source = central_record.get("k8s_identity_source")
    if source is not None and source != "dynamodb":
        raise RuntimeError("Central checkpoint Kubernetes identity has an unexpected source")
    if source is not None and not any(populated):
        raise RuntimeError("Central checkpoint identity source has no Kubernetes identity")
    for key, value in identity.items():
        if previous[key] is not None and previous[key] != value:
            raise RuntimeError(f"Central checkpoint Kubernetes identity changed: {key}")


def _reconcile_central_workload_identity(
    ctx: RunContext,
    central_record: dict[str, Any],
    persisted_job: dict[str, Any],
    *,
    workload_record: dict[str, Any] | None = None,
    require_identity: bool = True,
) -> dict[str, Any]:
    """Bind exact worker evidence without mutating requested replay identity."""
    _validate_central_job_identity(central_record, persisted_job)
    identity = _central_persisted_kubernetes_identity(
        persisted_job,
        required=require_identity,
    )
    record = workload_record or _central_workload_record(ctx, central_record)
    if identity is None:
        return record

    actual_name, actual_namespace, actual_uid = identity
    expected_name = _central_queue_kubernetes_job_name(
        str(central_record["job_name"]),
        str(central_record["job_id"]),
    )
    if actual_name != expected_name:
        raise RuntimeError(
            f"Central worker persisted unexpected Kubernetes Job name {actual_name!r}; "
            f"expected {expected_name!r}"
        )
    if actual_namespace != central_record["namespace"]:
        raise RuntimeError("Central worker persisted a different Kubernetes namespace")

    central_identity = {
        "k8s_job_name": actual_name,
        "k8s_job_namespace": actual_namespace,
        "k8s_job_uid": actual_uid,
    }
    _validate_central_checkpoint_kubernetes_identity(central_record, central_identity)
    ctx.bind_central_job_identity(
        record,
        job_id=str(central_record["job_id"]),
        name=actual_name,
        namespace=actual_namespace,
        uid=actual_uid,
        appearance_timeout_seconds=_job_appearance_timeout(ctx),
    )
    with ctx.state_lock:
        _validate_central_checkpoint_kubernetes_identity(central_record, central_identity)
        central_record.update(central_identity)
        central_record["k8s_identity_source"] = "dynamodb"
        central_record["workload_appearance_deadline"] = record.get("appearance_deadline")
        ctx.persist_callback(ctx.checkpoint)
    return record


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


def _reconcile_central_cleanup_workload(
    ctx: RunContext,
    central_record: dict[str, Any],
    persisted_job: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Reconcile workload authority from terminal DynamoDB cleanup evidence."""
    _validate_central_job_identity(central_record, persisted_job)
    terminal_status = str(persisted_job.get("status") or "unknown")
    if terminal_status not in _TERMINAL_QUEUE_STATUSES:
        raise RuntimeError("Central cleanup evidence is not terminal")

    worker_proved_not_created = persisted_job.get("workload_not_created") is True
    if worker_proved_not_created and terminal_status != "failed":
        raise RuntimeError(
            "Central worker no-workload proof is valid only for a failed queue record"
        )
    persisted_identity = _central_persisted_kubernetes_identity(
        persisted_job,
        required=terminal_status == "succeeded"
        or (terminal_status == "failed" and not worker_proved_not_created),
    )
    workload_record = _central_workload_record(ctx, central_record)

    if worker_proved_not_created:
        if persisted_identity is not None:
            raise RuntimeError(
                "Failed central Job has both no-workload proof and Kubernetes identity"
            )
        if _central_workload_identity(workload_record) is not None:
            raise RuntimeError(
                "Worker-proven uncreated central Job already has checkpointed workload identity"
            )
        if _central_workload_identity(central_record) is not None:
            raise RuntimeError(
                "Worker-proven uncreated central Job already has central checkpoint identity"
            )
        if workload_record.get("uid"):
            raise RuntimeError(
                "Worker-proven uncreated central Job already has Kubernetes UID authority"
            )
        job_id = str(central_record["job_id"])
        state = str(workload_record.get("submission_state") or "registered")
        prior_proof = workload_record.get("central_worker_not_created_job_id")
        if state == "deleted":
            if prior_proof != job_id:
                raise RuntimeError(
                    "Deleted central workload lacks matching worker no-workload proof"
                )
        else:
            ctx.mark_central_job_not_created_by_worker(workload_record, job_id=job_id)
        return workload_record, True

    if terminal_status != "cancelled":
        return (
            _reconcile_central_workload_identity(
                ctx,
                central_record,
                persisted_job,
                workload_record=workload_record,
            ),
            False,
        )

    if persisted_identity is not None or _central_workload_identity(workload_record) is not None:
        raise RuntimeError("Cancelled-before-claim central Job unexpectedly has workload identity")
    if workload_record.get("uid"):
        raise RuntimeError(
            "Cancelled-before-claim central Job already has Kubernetes UID authority"
        )
    job_id = str(central_record["job_id"])
    state = str(workload_record.get("submission_state") or "registered")
    prior_proof = workload_record.get("central_cancelled_before_claim_job_id")
    if state == "deleted":
        if prior_proof != job_id:
            raise RuntimeError("Deleted central workload lacks matching cancellation proof")
    else:
        ctx.mark_central_job_cancelled_before_claim(workload_record, job_id=job_id)
    return workload_record, True


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
    record = _reconcile_central_workload_identity(
        ctx,
        central_record,
        item,
        workload_record=record,
    )
    if record.get("deleted"):
        evidence = record.get("validation_evidence")
        if not isinstance(evidence, dict) or evidence.get("marker") != marker:
            raise RuntimeError(
                "Central Job was deleted without checkpointed live-validation evidence; "
                "refusing an idempotency replay"
            )
        actual_name, actual_namespace = _effective_job_identity(record)
        expected_evidence = {
            "name": actual_name,
            "namespace": actual_namespace,
            "uid": record.get("k8s_job_uid"),
            "central_queue_job_id": job_id,
        }
        for key, expected in expected_evidence.items():
            if evidence.get(key) != expected:
                raise RuntimeError(
                    f"Central Job deletion evidence does not match actual identity: {key}"
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
        "k8s_job_name": record.get("k8s_job_name"),
        "k8s_job_namespace": record.get("k8s_job_namespace"),
        "k8s_job_uid": record.get("k8s_job_uid"),
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

    _, workload_not_submitted = _reconcile_central_cleanup_workload(
        ctx,
        central_job,
        persisted,
    )

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
    reconciled_central_workloads: set[int] = set()
    for central_job in ctx.checkpoint.state.get("central_jobs", []):
        job_id = str(central_job["job_id"])
        try:
            if central_job.get("cleanup_complete"):
                persisted = _read_central_job_item(ctx, job_id)
                _validate_central_job_identity(central_job, persisted)
                persisted_status = str(persisted.get("status") or "unknown")
                checkpoint_status = str(
                    central_job.get("status")
                    or (central_job.get("cleanup_result") or {}).get("terminal_status")
                    or ""
                )
                if persisted_status not in _TERMINAL_QUEUE_STATUSES:
                    raise RuntimeError(
                        f"Previously completed central cleanup for {job_id} is no longer terminal"
                    )
                if checkpoint_status and checkpoint_status != persisted_status:
                    raise RuntimeError(
                        f"Central cleanup status changed from {checkpoint_status} "
                        f"to {persisted_status}"
                    )
                workload_record, _ = _reconcile_central_cleanup_workload(
                    ctx,
                    central_job,
                    persisted,
                )
                result["central_jobs"].append(
                    copy.deepcopy(central_job.get("cleanup_result") or {})
                )
            else:
                result["central_jobs"].append(_cleanup_central_job(ctx, central_job))
                workload_record = _central_workload_record(ctx, central_job)
            reconciled_central_workloads.add(id(workload_record))
        except Exception as exc:  # noqa: BLE001 - preserve every unresolved resource
            error = f"{type(exc).__name__}: {exc}"
            result["errors"].append({"resource": f"central:{job_id}", "error": error})
            result["unresolved"].append({"resource": f"central:{job_id}", "reason": error})

    for record in ctx.checkpoint.state.get("jobs", []):
        if record.get("deleted"):
            continue
        requested_reference = f"{record['region']}:{record['namespace']}/{record['name']}"
        reference = requested_reference
        try:
            if record.get("path") == "dynamodb" and id(record) not in reconciled_central_workloads:
                raise RuntimeError(
                    "Central workload was not reconciled from terminal DynamoDB evidence "
                    "in this cleanup attempt"
                )
            actual_name, actual_namespace = _job_reference_identity(record)
            reference = f"{record['region']}:{actual_namespace}/{actual_name}"
            deletion = _delete_owned_job(ctx, record)
            result["jobs"].append(
                {
                    "region": record["region"],
                    "namespace": actual_namespace,
                    "name": actual_name,
                    "requested_namespace": record["namespace"],
                    "requested_name": record["name"],
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
            name = str(record.get("k8s_job_name") or record["name"])
            namespace = str(record.get("k8s_job_namespace") or record["namespace"])
            reference = f"{record['region']}:{namespace}/{name}"
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


def _log_group_adoption_blockers(
    identity: Mapping[str, Any],
    *,
    run_id: str,
    cleanup_token: str,
) -> list[str]:
    """Explain why a regenerated same-name log group cannot be adopted.

    Teardown-time Lambda invocations flush their final events after their log
    groups were tagged or deleted, recreating untagged generations that belong
    to this run. Adoption is refused for any generation carrying another
    owner's markers: a foreign validation run/cleanup token, or CloudFormation
    stack tags (a real deployment's explicit LogGroup resources are always
    stack-tagged, while Lambda-recreated groups start with no tags at all).
    """
    tags_value = identity.get("tags")
    tags: dict[str, str] = dict(tags_value) if isinstance(tags_value, Mapping) else {}
    blockers = []
    if tags.get(_RUN_STACK_TAG) not in (None, run_id):
        blockers.append(f"foreign {_RUN_STACK_TAG}={tags.get(_RUN_STACK_TAG)!r}")
    if tags.get(_LOG_CLEANUP_TOKEN_TAG) not in (None, cleanup_token):
        blockers.append(f"foreign {_LOG_CLEANUP_TOKEN_TAG}")
    stack_tags = sorted(key for key in tags if key.startswith("aws:cloudformation:"))
    if stack_tags:
        blockers.append("cloudformation-owned generation: " + ", ".join(stack_tags))
    return blockers


def _adopt_regenerated_log_group(
    ctx: RunContext,
    record: dict[str, Any],
    logs_client: Any,
    *,
    region: str,
    name: str,
    observed_generation: Mapping[str, Any],
    authority_tags: Mapping[str, str],
) -> dict[str, Any] | None:
    """Tag and take ownership of a self-regenerated log-group generation.

    Callers must already hold the invocation-level proof that every exact
    target stack is absent, so no live deployment can own this name. Returns
    the stabilized post-tag identity, or ``None`` when the generation did not
    stabilize under this run's authority tags.
    """
    stack_absence = _verify_target_stack_absence(ctx)
    if not stack_absence["all_absent"]:
        raise RuntimeError("Log-group adoption requires every exact target stack to be absent")
    arn = str(observed_generation.get("arn") or "")
    if not arn:
        raise RuntimeError(f"Regenerated log group omitted its ARN: {region}:{name}")
    logs_client.tag_resource(resourceArn=arn, tags=dict(authority_tags))
    post_tag = _observe_log_group_stability(
        logs_client,
        region,
        name,
        expected_identity={**observed_generation, "tags": dict(authority_tags)},
        expected_tags=authority_tags,
        required_present=_LOG_GROUP_CLEANUP_STABLE_OBSERVATIONS,
        required_absent=_LOG_GROUP_ABSENCE_OBSERVATIONS,
    )
    _record_log_group_observation(
        ctx,
        record,
        phase="cleanup-adoption-post-tag",
        outcome=post_tag,
    )
    if post_tag["status"] != "present":
        return None
    identity = post_tag.get("identity")
    if not isinstance(identity, dict):
        raise RuntimeError(f"Adopted log group omitted its identity: {region}:{name}")
    with ctx.state_lock:
        record["observed_identity"] = copy.deepcopy(identity)
        adoptions = record.setdefault("adopted_generations", [])
        if not isinstance(adoptions, list):
            raise RuntimeError("Log-group adopted_generations must be a list")
        adoptions.append(
            {
                "adopted_at": utc_now(),
                "generation": _log_group_generation(identity),
                "stack_absence_proof_at": stack_absence.get("verified_at") or utc_now(),
            }
        )
        ctx.persist_callback(ctx.checkpoint)
    return identity


def _cleanup_owned_log_groups(ctx: RunContext) -> dict[str, Any]:
    """Converge every checkpointed log group to stable absence.

    Each record is processed independently: one blocked generation never
    strands the rest. Teardown-time Lambda invocations recreate their own
    groups after tagging, so untagged same-name regenerations are adopted
    (re-tagged under this run's proven stack-absence authority) and deleted;
    generations carrying another owner's markers stay strictly preserved.
    Bounded extra passes absorb log deliveries that land mid-sweep.
    """
    records = ctx.checkpoint.state.get("owned_log_groups", [])
    if not isinstance(records, list):
        raise RuntimeError("Checkpoint owned_log_groups must be a list")
    results: list[dict[str, Any]] = []
    authorization: dict[str, Any] = {"needed": False}
    helper_cleanup: dict[str, Any] = {"needed": False, "deleted": True}
    cleanup_error: Exception | None = None
    helper_error: Exception | None = None
    cleanup_token = str(ctx.checkpoint.state.get("log_group_cleanup_token") or "")
    authority_tags = {
        _RUN_STACK_TAG: ctx.settings.run_id,
        _LOG_CLEANUP_TOKEN_TAG: cleanup_token,
    }
    try:
        if records:
            stack_absence = _verify_target_stack_absence(ctx)
            if not stack_absence["all_absent"]:
                raise RuntimeError(
                    "Log-group cleanup requires every exact target stack to be absent"
                )
        if records and not re.fullmatch(r"[0-9a-f]{32}", cleanup_token):
            raise RuntimeError("Checkpoint log-group cleanup token is malformed")

        validated: list[tuple[dict[str, Any], str, str]] = []
        for raw_record in records:
            if not isinstance(raw_record, dict):
                raise RuntimeError("Checkpoint owned_log_groups must contain objects")
            region, name = _validated_owned_log_group_identity(ctx, raw_record)
            validated.append((raw_record, region, name))

        restricted_clients: dict[str, Any] = {}
        session_credentials: dict[str, Any] | None = None

        def _restricted_logs(region: str) -> Any:
            """Create the tag-conditioned deletion session lazily, once."""
            nonlocal session_credentials, authorization
            if session_credentials is None:
                helper = _ensure_log_cleanup_helper(ctx)
                if not helper.get("needed"):
                    raise RuntimeError("Log cleanup role was not created for pending groups")
                session_name = (
                    "live-validation-logs-"
                    + uuid.uuid5(_LOG_CLEANUP_HELPER_NAMESPACE, ctx.settings.run_id).hex[:16]
                )
                assumption = ctx.session.client("sts", region_name=helper["region"]).assume_role(
                    RoleArn=helper["role_arn"],
                    RoleSessionName=session_name,
                    DurationSeconds=_LOG_CLEANUP_SESSION_SECONDS,
                    ExternalId=helper["external_id"],
                    Policy=_canonical_json(helper["session_policy"]),
                )
                credentials = assumption.get("Credentials") or {}
                if any(
                    not credentials.get(field)
                    for field in ("AccessKeyId", "SecretAccessKey", "SessionToken")
                ):
                    raise RuntimeError("AssumeRole omitted cleanup session credentials")
                assumed_user_arn = str((assumption.get("AssumedRoleUser") or {}).get("Arn") or "")
                expected_assumed_arn = (
                    f"arn:{helper['partition']}:sts::{ctx.settings.expected_account}:assumed-role/"
                    f"{helper['role_arn'].rsplit('/', 1)[-1]}/{session_name}"
                )
                if assumed_user_arn != expected_assumed_arn:
                    raise RuntimeError("AssumeRole returned an unexpected cleanup principal")
                expiration = credentials.get("Expiration")
                session_credentials = credentials
                authorization = {
                    "needed": True,
                    "mode": "sts-assume-role-session-policy",
                    "role_arn": helper["role_arn"],
                    "helper_stack_id": helper["stack_id"],
                    "atomic_resource_tag_condition": True,
                    "condition_tag_keys": [_RUN_STACK_TAG, _LOG_CLEANUP_TOKEN_TAG],
                    "session_expiration": (
                        expiration.isoformat() if expiration is not None else None
                    ),
                }
            if region not in restricted_clients:
                assert session_credentials is not None
                restricted_clients[region] = ctx.session.client(
                    "logs",
                    region_name=region,
                    aws_access_key_id=session_credentials["AccessKeyId"],
                    aws_secret_access_key=session_credentials["SecretAccessKey"],
                    aws_session_token=session_credentials["SessionToken"],
                )
            return restricted_clients[region]

        def _blocked_entry(
            record: dict[str, Any],
            region: str,
            name: str,
            *,
            status: str,
            phase: str,
            outcome: dict[str, Any],
            retryable: bool,
            delete_requested: bool = False,
        ) -> dict[str, Any]:
            disposition = _set_log_group_disposition(
                ctx,
                record,
                status=status,
                phase=phase,
                outcome=outcome,
            )
            return {
                "region": region,
                "name": name,
                "original_identity": copy.deepcopy(record.get("observed_identity")),
                "deleted": False,
                "blocked": True,
                "retryable": retryable,
                "delete_requested": delete_requested,
                "observation": copy.deepcopy(outcome),
                "replacement_evidence": copy.deepcopy(record.get("replacement_evidence", [])),
                "original_generation_disposition": disposition,
            }

        completed: dict[tuple[str, str], dict[str, Any]] = {}
        blocked: dict[tuple[str, str], dict[str, Any]] = {}
        for sweep in range(1, _LOG_GROUP_CLEANUP_MAX_PASSES + 1):
            if sweep > 1:
                # Absorb straggling teardown log deliveries before re-observing.
                time.sleep(_LOG_GROUP_OBSERVATION_POLL_SECONDS * sweep)
            blocked.clear()
            for record, region, name in validated:
                key = (region, name)
                if key in completed:
                    continue
                observed = record.get("observed_identity")
                if not isinstance(observed, dict):
                    raise RuntimeError(
                        f"Log-group checkpoint identity is malformed: {region}:{name}"
                    )
                _log_group_generation(observed)
                normal_logs = ctx.session.client("logs", region_name=region)
                initial = _observe_log_group_stability(
                    normal_logs,
                    region,
                    name,
                    expected_identity=observed,
                    expected_tags=authority_tags,
                    required_present=_LOG_GROUP_CLEANUP_STABLE_OBSERVATIONS,
                    required_absent=_LOG_GROUP_ABSENCE_OBSERVATIONS,
                )
                _record_log_group_observation(
                    ctx,
                    record,
                    phase="cleanup-pending-stability",
                    outcome=initial,
                )
                if initial["status"] == "absent":
                    record["deleted"] = True
                    disposition = _set_log_group_disposition(
                        ctx,
                        record,
                        status="already-absent-confirmed",
                        phase="cleanup-pending-stability",
                        outcome=initial,
                    )
                    completed[key] = {
                        "region": region,
                        "name": name,
                        "original_identity": copy.deepcopy(observed),
                        "already_absent": True,
                        "absence_observations": initial["attempt_count"],
                        "original_generation_disposition": disposition,
                    }
                    continue

                identity: dict[str, Any] | None = None
                if initial["status"] == "present":
                    candidate = initial.get("identity")
                    if not isinstance(candidate, dict):
                        raise RuntimeError(
                            f"Stable log-group observation omitted identity: {region}:{name}"
                        )
                    identity = candidate
                elif initial["status"] == "replacement":
                    replacement_identity = initial.get("identity")
                    if not isinstance(replacement_identity, dict):
                        # The regeneration vanished mid-observation; the next
                        # pass will see stable absence.
                        blocked[key] = _blocked_entry(
                            record,
                            region,
                            name,
                            status="replacement-without-identity",
                            phase="cleanup-pending-stability",
                            outcome=initial,
                            retryable=True,
                        )
                        continue
                    blockers = _log_group_adoption_blockers(
                        replacement_identity,
                        run_id=ctx.settings.run_id,
                        cleanup_token=cleanup_token,
                    )
                    if blockers:
                        blocked[key] = _blocked_entry(
                            record,
                            region,
                            name,
                            status="replacement-observed-before-delete",
                            phase="cleanup-pending-stability",
                            outcome={**initial, "adoption_blockers": blockers},
                            retryable=False,
                        )
                        continue
                    adopted = _adopt_regenerated_log_group(
                        ctx,
                        record,
                        normal_logs,
                        region=region,
                        name=name,
                        observed_generation=replacement_identity,
                        authority_tags=authority_tags,
                    )
                    if adopted is None:
                        blocked[key] = _blocked_entry(
                            record,
                            region,
                            name,
                            status="adoption-did-not-stabilize",
                            phase="cleanup-adoption-post-tag",
                            outcome=initial,
                            retryable=True,
                        )
                        continue
                    identity = adopted
                else:
                    if initial["status"] == "tag-drift":
                        disposition_status = "authority-tag-drift-before-delete"
                        retryable = False
                    else:
                        disposition_status = "identity-not-stable-before-delete"
                        retryable = True
                    blocked[key] = _blocked_entry(
                        record,
                        region,
                        name,
                        status=disposition_status,
                        phase="cleanup-pending-stability",
                        outcome=initial,
                        retryable=retryable,
                    )
                    continue

                restricted_logs = _restricted_logs(region)
                # No persistence, sleep, or unrelated API call is permitted between
                # this single exact read and the tag-conditioned delete request.
                pre_delete = _observe_log_group_stability(
                    normal_logs,
                    region,
                    name,
                    expected_identity=identity,
                    expected_tags=authority_tags,
                    required_present=1,
                    required_absent=1,
                )
                if pre_delete["status"] == "present":
                    try:
                        restricted_logs.delete_log_group(logGroupName=name)
                    except ClientError as exc:
                        _record_log_group_observation(
                            ctx,
                            record,
                            phase="cleanup-immediate-pre-delete",
                            outcome=pre_delete,
                        )
                        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                            raise
                    else:
                        _record_log_group_observation(
                            ctx,
                            record,
                            phase="cleanup-immediate-pre-delete",
                            outcome=pre_delete,
                        )
                    record["delete_requested_at"] = utc_now()
                    ctx.persist()
                else:
                    _record_log_group_observation(
                        ctx,
                        record,
                        phase="cleanup-immediate-pre-delete",
                        outcome=pre_delete,
                    )

                if pre_delete["status"] not in {"present", "absent"}:
                    if pre_delete["status"] == "replacement":
                        disposition_status = "replacement-observed-immediately-before-delete"
                        retryable = not _log_group_adoption_blockers(
                            pre_delete.get("identity") or {},
                            run_id=ctx.settings.run_id,
                            cleanup_token=cleanup_token,
                        )
                    elif pre_delete["status"] == "tag-drift":
                        disposition_status = "authority-tag-drift-immediately-before-delete"
                        retryable = False
                    else:
                        disposition_status = "identity-not-stable-immediately-before-delete"
                        retryable = True
                    blocked[key] = _blocked_entry(
                        record,
                        region,
                        name,
                        status=disposition_status,
                        phase="cleanup-immediate-pre-delete",
                        outcome=pre_delete,
                        retryable=retryable,
                    )
                    continue

                absence = _observe_log_group_stability(
                    normal_logs,
                    region,
                    name,
                    expected_identity=identity,
                    expected_tags=authority_tags,
                    required_present=None,
                    required_absent=_LOG_GROUP_ABSENCE_OBSERVATIONS,
                )
                _record_log_group_observation(
                    ctx,
                    record,
                    phase="cleanup-post-delete-absence",
                    outcome=absence,
                )
                if absence["status"] != "absent":
                    if absence["status"] == "replacement":
                        disposition_status = "replacement-observed-before-confirmed-absence"
                        retryable = not _log_group_adoption_blockers(
                            absence.get("identity") or {},
                            run_id=ctx.settings.run_id,
                            cleanup_token=cleanup_token,
                        )
                    elif absence["status"] == "tag-drift":
                        disposition_status = "authority-tag-drift-after-delete-request"
                        retryable = False
                    else:
                        disposition_status = "absence-not-stable-after-delete-request"
                        retryable = True
                    blocked[key] = _blocked_entry(
                        record,
                        region,
                        name,
                        status=disposition_status,
                        phase="cleanup-post-delete-absence",
                        outcome=absence,
                        retryable=retryable,
                        delete_requested=pre_delete["status"] == "present",
                    )
                    continue

                record["deleted"] = True
                disposition = _set_log_group_disposition(
                    ctx,
                    record,
                    status="deleted-confirmed-absent",
                    phase="cleanup-post-delete-absence",
                    outcome=absence,
                )
                completed[key] = {
                    "region": region,
                    "name": name,
                    "arn": identity["arn"],
                    "creation_time": identity["creation_time"],
                    "stack_id": record["stack_id"],
                    "source_logical_id": record["source_logical_id"],
                    "source_resource_type": record["source_resource_type"],
                    "authority_phase": record["authority_phase"],
                    "atomic_resource_tag_condition": True,
                    "absence_observations": absence["attempt_count"],
                    "deleted": True,
                    "adopted": bool(record.get("adopted_generations")),
                    "original_generation_disposition": disposition,
                }
                ctx.persist()

            if not blocked or not any(entry["retryable"] for entry in blocked.values()):
                break

        results = [*completed.values(), *blocked.values()]
        if blocked:
            summary = ", ".join(
                f"{region}:{name} ({entry['original_generation_disposition']['status']})"
                for (region, name), entry in sorted(blocked.items())
            )
            raise RuntimeError(f"Log-group cleanup could not converge for: {summary}")
    except Exception as exc:  # noqa: BLE001 - attach helper cleanup and partial evidence
        cleanup_error = exc
    finally:
        try:
            helper_cleanup = _delete_log_cleanup_helper(ctx)
        except Exception as exc:  # noqa: BLE001 - preserve both independent failures
            helper_error = exc

    errors = []
    if cleanup_error is not None:
        errors.append(
            {"phase": "log-groups", "error": f"{type(cleanup_error).__name__}: {cleanup_error}"}
        )
    if helper_error is not None:
        errors.append(
            {
                "phase": "cleanup-helper",
                "error": f"{type(helper_error).__name__}: {helper_error}",
            }
        )
    details = {
        "log_groups": results,
        "authorization": authorization,
        "helper_stack_cleanup": helper_cleanup,
        "errors": errors,
    }
    ctx.checkpoint.state["last_log_group_cleanup"] = copy.deepcopy(details)
    ctx.persist()
    if errors:
        message = "Retained CloudWatch log cleanup failed: " + json.dumps(errors, sort_keys=True)
        primary_error = cleanup_error if cleanup_error is not None else helper_error
        raise _LogGroupCleanupError(message, details) from primary_error
    return details


def _schedule_retained_kms_keys(ctx: RunContext) -> dict[str, Any]:
    records = ctx.checkpoint.state.get("owned_kms_keys", [])
    retained_records = [
        record
        for record in records
        if _validated_owned_kms_identity(ctx, record)[3] == "harness-schedule"
    ]
    if retained_records and not ctx.settings.confirm_kms_key_deletion:
        raise RuntimeError("Retained KMS keys exist but this identity did not confirm key deletion")
    results: list[dict[str, Any]] = []
    for record in records:
        region, key_id, arn, cleanup_policy = _validated_owned_kms_identity(ctx, record)
        record.setdefault("cleanup_policy", cleanup_policy)
        kms = ctx.session.client("kms", region_name=region)
        try:
            metadata = kms.describe_key(KeyId=key_id).get("KeyMetadata", {})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "NotFoundException":
                record["scheduled"] = True
                record["deleted"] = True
                results.append(
                    {
                        "arn": arn,
                        "cleanup_policy": cleanup_policy,
                        "already_absent": True,
                    }
                )
                ctx.persist()
                continue
            raise
        if metadata.get("Arn") != arn:
            raise RuntimeError(f"KMS key ARN changed for {key_id}")
        tags = _kms_tags(kms, key_id)
        if tags.get(_RUN_STACK_TAG) != record["run_tag"]:
            raise RuntimeError(f"KMS run ownership changed for {arn}")

        state = str(metadata.get("KeyState") or "")
        if state != "PendingDeletion" and cleanup_policy == "harness-schedule":
            if state not in {"Enabled", "Disabled"}:
                raise RuntimeError(f"KMS key {arn} is {state}; refusing to schedule deletion")
            kms.schedule_key_deletion(
                KeyId=key_id,
                PendingWindowInDays=_KMS_PENDING_WINDOW_DAYS,
            )
            metadata = kms.describe_key(KeyId=key_id).get("KeyMetadata", {})
            state = str(metadata.get("KeyState") or "")
        if state != "PendingDeletion":
            raise RuntimeError(
                f"Expected {cleanup_policy} KMS key {arn} to be PendingDeletion; found {state}"
            )
        deletion_date = metadata.get("DeletionDate")
        record["scheduled"] = True
        record["deletion_date"] = deletion_date.isoformat() if deletion_date is not None else None
        if not record["deletion_date"]:
            raise RuntimeError(f"Pending-deletion KMS key omitted its deletion date: {arn}")
        results.append(
            {
                "arn": arn,
                "state": state,
                "cleanup_policy": cleanup_policy,
                "deletion_date": record["deletion_date"],
            }
        )
        ctx.persist()
    return {
        "keys": results,
        "deletion_window": {
            "harness_schedule_days": _KMS_PENDING_WINDOW_DAYS,
            "cloudformation_delete": "observed per key deletion_date",
        },
    }


def _retained_resource_cleanup(ctx: RunContext) -> dict[str, Any]:
    result: dict[str, Any] = {"started_at": utc_now(), "errors": []}
    try:
        result["cloudwatch_logs"] = _cleanup_owned_log_groups(ctx)
    except Exception as exc:  # noqa: BLE001 - preserve partial evidence
        if isinstance(exc, _LogGroupCleanupError):
            result["cloudwatch_logs"] = copy.deepcopy(exc.details)
        result["errors"].append(
            {"phase": "cloudwatch-logs", "error": f"{type(exc).__name__}: {exc}"}
        )
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


def _workload_cleanup_snapshot_sha256(ctx: RunContext) -> str:
    payload = to_jsonable(
        {
            "jobs": copy.deepcopy(ctx.checkpoint.state.get("jobs", [])),
            "central_jobs": copy.deepcopy(ctx.checkpoint.state.get("central_jobs", [])),
        }
    )
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_workload_cleanup_barrier(
    ctx: RunContext,
    cleanup_result: dict[str, Any],
) -> dict[str, Any]:
    if (
        cleanup_result.get("complete") is not True
        or cleanup_result.get("errors")
        or cleanup_result.get("unresolved")
    ):
        raise RuntimeError("Cannot checkpoint an incomplete workload cleanup barrier")
    barrier = {
        "complete": True,
        "completed_at": str(cleanup_result.get("ended_at") or utc_now()),
        "snapshot_sha256": _workload_cleanup_snapshot_sha256(ctx),
        "job_count": len(ctx.checkpoint.state.get("jobs", [])),
        "central_job_count": len(ctx.checkpoint.state.get("central_jobs", [])),
    }
    ctx.checkpoint.state["workload_cleanup_barrier"] = barrier
    ctx.persist()
    return barrier


def _validated_workload_cleanup_barrier(ctx: RunContext) -> dict[str, Any]:
    barrier = ctx.checkpoint.state.get("workload_cleanup_barrier")
    if not isinstance(barrier, dict) or barrier.get("complete") is not True:
        raise RuntimeError("Checkpoint lacks a complete workload cleanup barrier")
    expected = str(barrier.get("snapshot_sha256") or "")
    current = _workload_cleanup_snapshot_sha256(ctx)
    if not expected or expected != current:
        raise RuntimeError("Checkpoint workload identity changed after cleanup completed")
    return barrier


def _resume_workload_cleanup_after_stack_absence(
    ctx: RunContext,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use an existing barrier, or create one only for a proven empty legacy run."""
    jobs = ctx.checkpoint.state.get("jobs", [])
    central_jobs = ctx.checkpoint.state.get("central_jobs", [])
    if not isinstance(jobs, list) or not isinstance(central_jobs, list):
        raise RuntimeError("Checkpoint workload collections must be lists")

    if ctx.checkpoint.state.get("workload_cleanup_barrier") is None:
        if jobs or central_jobs:
            raise RuntimeError(
                "Target stacks are absent but no completed workload cleanup barrier "
                "was checkpointed"
            )
        workload_cleanup = cleanup_workloads(ctx)
        barrier = _record_workload_cleanup_barrier(ctx, workload_cleanup)
    else:
        barrier = _validated_workload_cleanup_barrier(ctx)
        workload_cleanup = {
            "complete": True,
            "reconciled_from_checkpoint_barrier": True,
            "barrier": copy.deepcopy(barrier),
        }
    _validated_workload_cleanup_barrier(ctx)
    return workload_cleanup, barrier


def _record_target_stack_absence(
    ctx: RunContext,
    stack_absence: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    if stack_absence.get("all_absent") is not True:
        raise RuntimeError("Cannot checkpoint target stack absence while a stack remains")
    workload_barrier = _validated_workload_cleanup_barrier(ctx)
    proof = {
        "verified_at": utc_now(),
        "source": source,
        "workload_cleanup_snapshot_sha256": workload_barrier["snapshot_sha256"],
        "stack_absence": copy.deepcopy(stack_absence),
    }
    ctx.checkpoint.state["target_stacks_absent"] = proof
    ctx.checkpoint.state.setdefault("target_stack_absence_proofs", []).append(proof)
    ctx.persist()
    return proof


def destroy_deployment(ctx: RunContext) -> dict[str, Any]:
    """Retry exact-owned teardown, preserving every structured attempt."""
    if not ctx.checkpoint.deployment_attempted:
        return {"needed": False, "attempts": []}

    initial_absence = _verify_target_stack_absence(ctx)
    if ctx.checkpoint.destroyed and initial_absence["all_absent"]:
        workload_cleanup, workload_barrier = _resume_workload_cleanup_after_stack_absence(ctx)
        absence_proof = _record_target_stack_absence(
            ctx,
            initial_absence,
            source="destroy-already-destroyed-initial-absence",
        )
        _checkpoint_retained_kms_keys(ctx)
        retained_cleanup = _retained_resource_cleanup(ctx)
        final_absence = _verify_target_stack_absence(ctx)
        if not final_absence["all_absent"]:
            raise RuntimeError(
                "A target stack reappeared during repeated retained cleanup: "
                + json.dumps(final_absence["residual"], sort_keys=True)
            )
        completion_proof = _record_target_stack_absence(
            ctx,
            final_absence,
            source="destroy-already-destroyed-completion",
        )
        return {
            "needed": True,
            "already_destroyed": True,
            "workload_cleanup": workload_cleanup,
            "workload_cleanup_barrier": workload_barrier,
            "stack_absence_proof": absence_proof,
            "stack_absence_completion_proof": completion_proof,
            "stack_absence": final_absence,
            "retained_cleanup": retained_cleanup,
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

    if initial_absence["all_absent"]:
        workload_cleanup, workload_barrier = _resume_workload_cleanup_after_stack_absence(ctx)
        absence_proof = _record_target_stack_absence(
            ctx,
            initial_absence,
            source="destroy-resume-initial-absence",
        )
        _checkpoint_new_ecr_repositories(ctx)
        _checkpoint_new_ecr_images(ctx)
        _checkpoint_retained_kms_keys(ctx)
        retained_cleanup = _retained_resource_cleanup(ctx)
        final_absence = _verify_target_stack_absence(ctx)
        if not final_absence["all_absent"]:
            raise RuntimeError(
                "A target stack reappeared during resumed retained cleanup: "
                + json.dumps(final_absence["residual"], sort_keys=True)
            )
        completion_proof = _record_target_stack_absence(
            ctx,
            final_absence,
            source="destroy-resume-completion",
        )
        ctx.checkpoint.destroyed = True
        ctx.persist()
        return {
            "needed": True,
            "resumed_after_stack_absence": True,
            "workload_cleanup": workload_cleanup,
            "workload_cleanup_barrier": workload_barrier,
            "stack_absence_proof": absence_proof,
            "stack_absence_completion_proof": completion_proof,
            "stack_absence": final_absence,
            "retained_cleanup": retained_cleanup,
            "attempts": ctx.checkpoint.state.get("destroy_attempts", []),
            "workload_cleanup_attempts": ctx.checkpoint.state.get("workload_cleanup_attempts", []),
            "retained_cleanup_attempts": ctx.checkpoint.state.get("retained_cleanup_attempts", []),
        }

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
    workload_cleanup_barrier = _record_workload_cleanup_barrier(ctx, workload_cleanup)
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
            helper_authority = _ensure_log_cleanup_helper(ctx)
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
                "log_cleanup_helper": helper_authority,
            }
            if overall:
                absence_before_cleanup = _verify_target_stack_absence(ctx)
                attempt["stack_absence_before_retained_cleanup"] = absence_before_cleanup
                if not absence_before_cleanup["all_absent"]:
                    raise RuntimeError(
                        "Target stack absence was not proved after destroy: "
                        + json.dumps(absence_before_cleanup["residual"], sort_keys=True)
                    )
                attempt["target_stack_absence_proof"] = _record_target_stack_absence(
                    ctx,
                    absence_before_cleanup,
                    source="destroy-before-retained-cleanup",
                )
                attempt["retained_cleanup"] = _retained_resource_cleanup(ctx)
                absence_before_completion = _verify_target_stack_absence(ctx)
                attempt["stack_absence_before_completion"] = absence_before_completion
                if not absence_before_completion["all_absent"]:
                    raise RuntimeError(
                        "A target stack reappeared during retained cleanup: "
                        + json.dumps(absence_before_completion["residual"], sort_keys=True)
                    )
                attempt["target_stack_absence_completion_proof"] = _record_target_stack_absence(
                    ctx,
                    absence_before_completion,
                    source="destroy-completion",
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
        if not overall:
            try:
                attempt["log_cleanup_helper_cleanup"] = _delete_log_cleanup_helper(ctx)
            except Exception as helper_exc:  # noqa: BLE001 - retain both teardown failures
                helper_error = f"{type(helper_exc).__name__}: {helper_exc}"
                attempt["log_cleanup_helper_cleanup_error"] = helper_error
                previous_error = str(attempt.get("error") or "")
                attempt["error"] = "; ".join(
                    part for part in (previous_error, f"cleanup helper: {helper_error}") if part
                )
        attempt["ended_at"] = utc_now()
        attempts.append(attempt)
        ctx.persist()
        if overall:
            ctx.checkpoint.destroyed = True
            ctx.persist()
            return {
                "needed": True,
                "workload_cleanup": workload_cleanup,
                "workload_cleanup_barrier": workload_cleanup_barrier,
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
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    accepted: list[dict[str, Any]] = []
    for record in ctx.checkpoint.state.get("owned_kms_keys", []):
        region, key_id, arn, cleanup_policy = _validated_owned_kms_identity(ctx, record)
        identity = (region, arn)
        if not record.get("scheduled"):
            raise RuntimeError(f"Owned KMS key was not scheduled for deletion: {arn}")
        if identity in expected:
            raise RuntimeError(f"Duplicate KMS checkpoint identity: {region}:{arn}")

        kms = ctx.session.client("kms", region_name=region)
        try:
            metadata = kms.describe_key(KeyId=key_id).get("KeyMetadata", {})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NotFoundException":
                raise
            evidence = {
                "region": region,
                "key_id": key_id,
                "arn": arn,
                "state": "Deleted",
                "already_absent": True,
                "stack_id": record["stack_id"],
                "logical_id": record["logical_id"],
                "ownership_authority": record["ownership_authority"],
                "cleanup_policy": cleanup_policy,
                "run_tag": record["run_tag"],
            }
        else:
            if metadata.get("Arn") != arn:
                raise RuntimeError(f"KMS key ARN changed for {key_id}")
            state = str(metadata.get("KeyState") or "")
            if state != "PendingDeletion":
                raise RuntimeError(
                    f"Expected {cleanup_policy} KMS key {arn} to be PendingDeletion; found {state}"
                )
            tags = _kms_tags(kms, key_id)
            if tags.get(_RUN_STACK_TAG) != record["run_tag"]:
                raise RuntimeError(f"KMS run ownership changed for {arn}")
            deletion_date = metadata.get("DeletionDate")
            observed_deletion_date = (
                deletion_date.isoformat() if deletion_date is not None else None
            )
            if not observed_deletion_date or observed_deletion_date != record.get("deletion_date"):
                raise RuntimeError(f"KMS deletion date changed for {arn}")
            evidence = {
                "region": region,
                "key_id": key_id,
                "arn": arn,
                "state": state,
                "description": str(metadata.get("Description") or ""),
                "deletion_date": observed_deletion_date,
                "tags": tags,
                "stack_id": record["stack_id"],
                "logical_id": record["logical_id"],
                "ownership_authority": record["ownership_authority"],
                "cleanup_policy": cleanup_policy,
                "run_tag": record["run_tag"],
            }
        expected[identity] = evidence
        accepted.append(evidence)

    for region, resources in list(inventory.get("regional", {}).items()):
        resources["kms_keys"] = [
            key
            for key in resources.get("kms_keys", [])
            if (region, str(key.get("arn") or "")) not in expected
        ]
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
        expected_account=ctx.settings.expected_account,
        project_name=ctx.config.project_name,
        seed_region=ctx.config.global_region,
        validation_run_id=ctx.settings.run_id,
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
