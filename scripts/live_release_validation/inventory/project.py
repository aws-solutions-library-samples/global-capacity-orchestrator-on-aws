"""Aggregate project-resource collection, baselines, and absence proofs."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from ._shared import (
    _GLOBAL_PROJECT_RESOURCE_CATEGORIES,
    _PROJECT_RESOURCE_CATEGORIES,
    _PROJECT_RESOURCE_SCANNERS,
    _REGIONAL_PROJECT_RESOURCE_CATEGORIES,
    _project_owned_name,
)
from .ecr import (
    collect_ecr_inventory,
)
from .scanners import (
    _global_accelerator_control_region,
    _list_api_gateway_v1_apis,
    _list_api_gateway_v2_apis,
    _list_cloudwatch_log_groups,
    _list_cluster_volumes,
    _list_dynamodb_tables,
    _list_eks_clusters,
    _list_global_accelerators,
    _list_instance_inventory,
    _list_instances,
    _list_lambda_functions,
    _list_load_balancers,
    _list_project_backup_resources,
    _list_project_ec2_networking,
    _list_project_ecr_repositories,
    _list_project_iam_resources,
    _list_project_kms_keys,
    _list_project_s3_buckets,
    _list_project_tagged_resources,
    _list_secrets,
    _list_sqs_queues,
    _list_target_groups,
)
from .stacks import (
    collect_project_stacks,
    collect_stack_inventory,
    describe_stack_fingerprint,
)


def capture_baseline(
    session: Any,
    *,
    enabled_regions: Iterable[str],
    ecr_regions: Iterable[str],
    protected_stack_names: Iterable[str],
) -> dict[str, Any]:
    """Capture protected CloudFormation and complete ECR baselines."""
    protected_names = set(protected_stack_names)
    stack_inventory = collect_stack_inventory(session, enabled_regions)
    protected: dict[str, list[dict[str, Any]]] = {}
    for region, stacks in stack_inventory.items():
        fingerprints = []
        for stack in stacks:
            if stack["name"] not in protected_names:
                continue
            fingerprint = describe_stack_fingerprint(session, region, stack["stack_id"])
            if fingerprint is None:
                raise RuntimeError(
                    f"Protected stack disappeared while fingerprinting: {region}:{stack['name']}"
                )
            fingerprints.append(fingerprint)
        if fingerprints:
            protected[region] = sorted(
                fingerprints,
                key=lambda item: (item["name"], item["stack_id"]),
            )
    return {
        "enabled_regions": sorted(set(enabled_regions)),
        "ecr_regions": sorted(set(ecr_regions)),
        "protected_stack_names": sorted(protected_names),
        "protected_stacks": protected,
        "ecr_repositories": collect_ecr_inventory(session, ecr_regions),
    }


def _tagged_ecr_surface(repositories: Any) -> Any:
    """Return ECR repositories with untagged images dropped.

    Copying a multi-arch image into a mirror repository leaves the per-platform
    child manifests untagged: only the manifest list carries the tag. Those
    children are not an addressable surface — nothing can reference them by tag,
    and the retained-image acceptance mechanism keys on tags
    (``retained_ecr_image_deltas`` -> ``_image_with_tag``), so an untagged child
    can never be declared and can never be accepted.

    Comparing them therefore made the check unsatisfiable: a run that mirrors a
    multi-arch image into a repository that already existed in the baseline
    always reported drift no matter how correct it was. Observed live on a run
    whose Volcano mirror repositories held identical tags before and after while
    their untagged child count grew from 8 to 12.

    Dropping untagged images keeps every guarantee that is actually enforceable:
    a repository appearing or disappearing is still a difference, and so is any
    tag that is added, removed, or repointed to a different digest.
    """
    if not isinstance(repositories, list):
        return repositories
    comparable = []
    for repository in repositories:
        if not isinstance(repository, dict):
            comparable.append(repository)
            continue
        images = repository.get("images")
        if not isinstance(images, list):
            comparable.append(repository)
            continue
        comparable.append({**repository, "images": [i for i in images if _image_tags(i)]})
    return comparable


def _image_tags(image: Any) -> list[Any]:
    """Tags carried by one ECR image record, tolerating malformed entries."""
    if not isinstance(image, dict):
        return []
    tags = image.get("tags")
    return list(tags) if isinstance(tags, list) else []


def compare_baseline(expected: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    """Return exact protected-stack/ECR differences."""
    differences: list[dict[str, Any]] = []
    for category in ("protected_stacks", "ecr_repositories"):
        before_by_region = expected.get(category) or {}
        after_by_region = actual.get(category) or {}
        for region in sorted(set(before_by_region) | set(after_by_region)):
            before = before_by_region.get(region, [])
            after = after_by_region.get(region, [])
            if category == "ecr_repositories":
                before = _tagged_ecr_surface(before)
                after = _tagged_ecr_surface(after)
            if before != after:
                differences.append(
                    {
                        "category": category,
                        "region": region,
                        "before": before,
                        "after": after,
                    }
                )
    return differences


def collect_project_resources(
    session: Any,
    *,
    enabled_regions: Iterable[str],
    expected_account: str,
    project_name: str,
    seed_region: str,
    validation_run_id: str | None = None,
) -> dict[str, Any]:
    """Collect project resources with explicit, fail-closed scanner coverage."""
    regions = sorted(set(enabled_regions))
    partition = session.get_partition_for_region(seed_region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for {seed_region}")
    if len(expected_account) != 12 or not expected_account.isdigit():
        raise RuntimeError("EC2 existence authority requires an exact 12-digit account ID")

    service_names = (
        "resourcegroupstaggingapi",
        "eks",
        "sqs",
        "dynamodb",
        "elbv2",
        "ec2",
        "ecr",
        "kms",
        "lambda",
        "apigateway",
        "apigatewayv2",
        "logs",
        "secretsmanager",
        "backup",
    )
    service_regions = {
        service: set(session.get_available_regions(service, partition_name=partition))
        for service in service_names
    }
    regional: dict[str, dict[str, list[Any]]] = {
        region: {category: [] for category in _REGIONAL_PROJECT_RESOURCE_CATEGORIES}
        for region in regions
    }
    authoritative_eks_clusters: dict[str, list[str]] = {}
    authoritative_ec2_resources: dict[str, dict[str, list[str]]] = {}
    completed_scanners: list[str] = []
    scanner_regions: dict[str, list[str]] = {}

    cloudformation_stacks = collect_project_stacks(session, regions, project_name)
    scanner_regions["cloudformation_stacks"] = regions
    completed_scanners.append("cloudformation_stacks")

    regional_collectors = (
        (
            "resource_groups_tagging_api",
            "resourcegroupstaggingapi",
            "tagged_resources",
            _list_project_tagged_resources,
        ),
        ("eks_clusters", "eks", "eks_clusters", _list_eks_clusters),
        ("sqs_queues", "sqs", "sqs_queues", _list_sqs_queues),
        ("dynamodb_tables", "dynamodb", "dynamodb_tables", _list_dynamodb_tables),
        ("load_balancers", "elbv2", "load_balancers", _list_load_balancers),
        ("target_groups", "elbv2", "target_groups", _list_target_groups),
        ("ec2_instances", "ec2", "instances", _list_instances),
        (
            "ecr_repositories",
            "ecr",
            "ecr_repositories",
            _list_project_ecr_repositories,
        ),
        ("kms_keys", "kms", "kms_keys", _list_project_kms_keys),
        ("lambda_functions", "lambda", "lambda_functions", _list_lambda_functions),
        (
            "api_gateway_v1_apis",
            "apigateway",
            "api_gateway_v1_apis",
            _list_api_gateway_v1_apis,
        ),
        (
            "api_gateway_v2_apis",
            "apigatewayv2",
            "api_gateway_v2_apis",
            _list_api_gateway_v2_apis,
        ),
        (
            "cloudwatch_log_groups",
            "logs",
            "cloudwatch_log_groups",
            _list_cloudwatch_log_groups,
        ),
        ("secrets_manager", "secretsmanager", "secrets", _list_secrets),
        # Last of the regional collectors: the CSI driver's volumes are the one
        # category identified purely by a Kubernetes tag, so they are scanned
        # independently of the stack- and project-tag matching above.
        ("cluster_volumes", "ec2", "cluster_volumes", _list_cluster_volumes),
    )
    for scanner, service, category, collector in regional_collectors:
        applicable_regions = sorted(set(regions) & service_regions[service])
        scanner_regions[scanner] = applicable_regions
        for region in applicable_regions:
            if scanner == "eks_clusters":
                cluster_names = _list_eks_clusters(session, region, None)
                authoritative_eks_clusters[region] = cluster_names
                regional[region][category] = [
                    name for name in cluster_names if _project_owned_name(name, project_name)
                ]
            elif scanner == "kms_keys":
                regional[region][category] = _list_project_kms_keys(
                    session,
                    region,
                    project_name,
                    validation_run_id,
                )
            elif scanner == "ec2_instances":
                project_instances, all_instances = _list_instance_inventory(
                    session,
                    region,
                    project_name,
                )
                regional[region][category] = project_instances
                authoritative_ec2_resources.setdefault(region, {})[category] = all_instances
            else:
                regional[region][category] = collector(session, region, project_name)
        completed_scanners.append(scanner)

        if scanner == "ec2_instances":
            scanner_regions["ec2_networking"] = applicable_regions
            for region in applicable_regions:
                project_networking, authoritative_networking = _list_project_ec2_networking(
                    session,
                    region,
                    project_name,
                    regional[region]["instances"],
                )
                regional[region].update(project_networking)
                authoritative_ec2_resources.setdefault(region, {}).update(authoritative_networking)
            completed_scanners.append("ec2_networking")

    backup_regions = sorted(set(regions) & service_regions["backup"])
    scanner_regions["aws_backup"] = backup_regions
    for region in backup_regions:
        regional[region].update(_list_project_backup_resources(session, region, project_name))
    completed_scanners.append("aws_backup")

    s3_buckets = _list_project_s3_buckets(session, seed_region, project_name)
    scanner_regions["s3_buckets"] = ["global"]
    completed_scanners.append("s3_buckets")

    iam_resources = _list_project_iam_resources(session, seed_region, project_name)
    scanner_regions["iam"] = ["global"]
    completed_scanners.append("iam")

    global_accelerator_region = _global_accelerator_control_region(session, seed_region)
    global_accelerators = _list_global_accelerators(
        session,
        global_accelerator_region,
        project_name,
    )
    scanner_regions["global_accelerators"] = (
        [global_accelerator_region] if global_accelerator_region else []
    )
    completed_scanners.append("global_accelerators")

    coverage = {
        "complete": completed_scanners == list(_PROJECT_RESOURCE_SCANNERS),
        "required_scanners": list(_PROJECT_RESOURCE_SCANNERS),
        "completed_scanners": completed_scanners,
        "scanner_regions": scanner_regions,
        "enabled_regions": regions,
        "resource_categories": list(_PROJECT_RESOURCE_CATEGORIES),
    }
    if not coverage["complete"]:
        raise RuntimeError(
            "Project resource inventory did not run every required scanner: "
            + json.dumps(coverage, sort_keys=True)
        )

    populated_regional = {
        region: resources for region, resources in regional.items() if any(resources.values())
    }
    return {
        "coverage": coverage,
        "authority_scope": {"partition": partition, "account": expected_account},
        "cloudformation_stacks": cloudformation_stacks,
        "authoritative_eks_clusters": authoritative_eks_clusters,
        "authoritative_ec2_resources": authoritative_ec2_resources,
        "regional": populated_regional,
        "global_accelerators": global_accelerators,
        "s3_buckets": s3_buckets,
        **iam_resources,
    }


def summarize_project_resources(inventory: dict[str, Any]) -> dict[str, int]:
    """Flatten every residual resource category into report-friendly counts."""
    summary = dict.fromkeys(_PROJECT_RESOURCE_CATEGORIES, 0)
    summary["cloudformation_stacks"] = sum(
        len(items) for items in inventory.get("cloudformation_stacks", {}).values()
    )
    for resources in inventory.get("regional", {}).values():
        for category in _REGIONAL_PROJECT_RESOURCE_CATEGORIES:
            summary[category] += len(resources.get(category, []))
    for category in _GLOBAL_PROJECT_RESOURCE_CATEGORIES:
        summary[category] = len(inventory.get(category, []))
    return summary


def project_resources_are_absent(inventory: dict[str, Any]) -> bool:
    """Return true only for an explicitly complete, all-zero inventory."""
    coverage = inventory.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("complete") is not True:
        return False
    required = coverage.get("required_scanners")
    completed = coverage.get("completed_scanners")
    categories = coverage.get("resource_categories")
    if required != list(_PROJECT_RESOURCE_SCANNERS):
        return False
    if completed != list(_PROJECT_RESOURCE_SCANNERS):
        return False
    if categories != list(_PROJECT_RESOURCE_CATEGORIES):
        return False
    return all(count == 0 for count in summarize_project_resources(inventory).values())
