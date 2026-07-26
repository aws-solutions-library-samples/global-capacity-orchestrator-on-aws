"""Protected-resource identity matching for the ownership boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

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
