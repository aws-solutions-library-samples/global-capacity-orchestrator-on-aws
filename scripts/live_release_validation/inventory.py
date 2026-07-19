"""Fail-closed AWS inventory collection and comparison helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from botocore.exceptions import ClientError

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
    "instances",
    "vpcs",
    "subnets",
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
    "ec2_instances",
    "ec2_networking",
    "ecr_repositories",
    "kms_keys",
    "lambda_functions",
    "api_gateway_v1_apis",
    "api_gateway_v2_apis",
    "cloudwatch_log_groups",
    "secrets_manager",
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


def _normalize_json_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _optional_ecr_configuration(
    client: Any,
    operation: str,
    *,
    repository_name: str,
    response_key: str,
    not_found_code: str,
) -> Any:
    try:
        response = getattr(client, operation)(repositoryName=repository_name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == not_found_code:
            return None
        raise
    return _normalize_json_text(response.get(response_key))


def _collect_repository_images(client: Any, repository_name: str) -> list[dict[str, Any]]:
    """Collect every digest, tag set, and exact manifest in one repository."""
    details: dict[str, dict[str, Any]] = {}
    for page in client.get_paginator("describe_images").paginate(repositoryName=repository_name):
        for image in page.get("imageDetails", []):
            digest = str(image.get("imageDigest") or "")
            if not digest:
                raise RuntimeError(f"ECR image in {repository_name} omitted its digest")
            details[digest] = image

    manifests: dict[str, dict[str, Any]] = {}
    digests = sorted(details)
    for offset in range(0, len(digests), 100):
        requested = digests[offset : offset + 100]
        response = client.batch_get_image(
            repositoryName=repository_name,
            imageIds=[{"imageDigest": digest} for digest in requested],
            acceptedMediaTypes=list(_ECR_MANIFEST_MEDIA_TYPES),
        )
        failures = response.get("failures", [])
        if failures:
            raise RuntimeError(
                f"ECR manifest lookup failed for {repository_name}: "
                + json.dumps(failures, sort_keys=True)
            )
        for image in response.get("images", []):
            digest = str((image.get("imageId") or {}).get("imageDigest") or "")
            if digest:
                manifests[digest] = image
        missing = sorted(set(requested) - set(manifests))
        if missing:
            raise RuntimeError(
                f"ECR manifest lookup omitted {repository_name} digests: " + ", ".join(missing)
            )

    images: list[dict[str, Any]] = []
    for digest, detail in details.items():
        manifest = manifests[digest]
        pushed_at = detail.get("imagePushedAt")
        images.append(
            {
                "digest": digest,
                "tags": sorted(str(tag) for tag in detail.get("imageTags") or []),
                "size_bytes": int(detail.get("imageSizeInBytes") or 0),
                "pushed_at": pushed_at.isoformat() if pushed_at is not None else None,
                "manifest_media_type": str(
                    manifest.get("imageManifestMediaType")
                    or detail.get("imageManifestMediaType")
                    or ""
                ),
                "artifact_media_type": str(detail.get("artifactMediaType") or ""),
                "manifest": _normalize_json_text(manifest.get("imageManifest", "")),
            }
        )
    return sorted(images, key=lambda item: item["digest"])


def describe_ecr_image_by_tag(
    session: Any,
    *,
    region: str,
    repository_name: str,
    tag: str,
) -> dict[str, Any] | None:
    """Resolve one ECR tag to its exact digest and manifest.

    ``None`` means ECR authoritatively reported that the tag is absent. Any
    ambiguous, incomplete, or failed lookup raises so callers cannot turn a
    best-effort read into deletion authority.
    """
    client = session.client("ecr", region_name=region)
    try:
        response = client.describe_images(
            repositoryName=repository_name,
            imageIds=[{"imageTag": tag}],
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {
            "ImageNotFoundException",
            "RepositoryNotFoundException",
        }:
            return None
        raise

    details = response.get("imageDetails", [])
    if not details:
        raise RuntimeError(
            f"ECR tag lookup omitted an authoritative identity for {region}:{repository_name}:{tag}"
        )
    if len(details) != 1:
        raise RuntimeError(
            f"ECR tag {region}:{repository_name}:{tag} resolved to {len(details)} images"
        )
    detail = details[0]
    digest = str(detail.get("imageDigest") or "")
    tags = sorted(str(image_tag) for image_tag in detail.get("imageTags") or [])
    if not digest or tag not in tags:
        raise RuntimeError(
            f"ECR tag lookup returned an invalid identity for {region}:{repository_name}:{tag}"
        )

    manifest_response = client.batch_get_image(
        repositoryName=repository_name,
        imageIds=[{"imageDigest": digest}],
        acceptedMediaTypes=list(_ECR_MANIFEST_MEDIA_TYPES),
    )
    failures = manifest_response.get("failures", [])
    if failures:
        raise RuntimeError(
            f"ECR manifest lookup failed for {repository_name}:{tag}: "
            + json.dumps(failures, sort_keys=True)
        )
    manifests = manifest_response.get("images", [])
    if len(manifests) != 1:
        raise RuntimeError(
            f"ECR manifest lookup returned {len(manifests)} records for {repository_name}:{tag}"
        )
    manifest = manifests[0]
    manifest_digest = str((manifest.get("imageId") or {}).get("imageDigest") or "")
    if manifest_digest != digest:
        raise RuntimeError(f"ECR manifest digest changed for {region}:{repository_name}:{tag}")
    pushed_at = detail.get("imagePushedAt")
    return {
        "digest": digest,
        "tags": tags,
        "size_bytes": int(detail.get("imageSizeInBytes") or 0),
        "pushed_at": pushed_at.isoformat() if pushed_at is not None else None,
        "manifest_media_type": str(
            manifest.get("imageManifestMediaType") or detail.get("imageManifestMediaType") or ""
        ),
        "artifact_media_type": str(detail.get("artifactMediaType") or ""),
        "manifest": _normalize_json_text(manifest.get("imageManifest", "")),
    }


def collect_ecr_inventory(session: Any, regions: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """Collect exact ECR repository identity/configuration in each topology Region."""
    inventory: dict[str, list[dict[str, Any]]] = {}
    for region in sorted(set(regions)):
        client = session.client("ecr", region_name=region)
        repositories: list[dict[str, Any]] = []
        for page in client.get_paginator("describe_repositories").paginate():
            for repository in page.get("repositories", []):
                name = str(repository.get("repositoryName") or "")
                arn = str(repository.get("repositoryArn") or "")
                tags = _tags_to_dict(client.list_tags_for_resource(resourceArn=arn).get("tags", []))
                repositories.append(
                    {
                        "name": name,
                        "arn": arn,
                        "registry_id": str(repository.get("registryId") or ""),
                        "uri": str(repository.get("repositoryUri") or ""),
                        "created_at": (
                            repository["createdAt"].isoformat()
                            if repository.get("createdAt") is not None
                            else None
                        ),
                        "tag_mutability": str(repository.get("imageTagMutability") or ""),
                        "tag_mutability_exclusions": sorted(
                            repository.get("imageTagMutabilityExclusionFilters") or [],
                            key=lambda item: json.dumps(item, sort_keys=True),
                        ),
                        "scan_configuration": dict(
                            repository.get("imageScanningConfiguration") or {}
                        ),
                        "encryption": dict(repository.get("encryptionConfiguration") or {}),
                        "lifecycle_policy": _optional_ecr_configuration(
                            client,
                            "get_lifecycle_policy",
                            repository_name=name,
                            response_key="lifecyclePolicyText",
                            not_found_code="LifecyclePolicyNotFoundException",
                        ),
                        "repository_policy": _optional_ecr_configuration(
                            client,
                            "get_repository_policy",
                            repository_name=name,
                            response_key="policyText",
                            not_found_code="RepositoryPolicyNotFoundException",
                        ),
                        "tags": tags,
                        "images": _collect_repository_images(client, name),
                    }
                )
        inventory[region] = sorted(repositories, key=lambda item: item["name"])
    return inventory


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


def compare_baseline(expected: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    """Return exact protected-stack/ECR differences."""
    differences: list[dict[str, Any]] = []
    for category in ("protected_stacks", "ecr_repositories"):
        before_by_region = expected.get(category) or {}
        after_by_region = actual.get(category) or {}
        for region in sorted(set(before_by_region) | set(after_by_region)):
            before = before_by_region.get(region, [])
            after = after_by_region.get(region, [])
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


def _list_eks_clusters(
    session: Any,
    region: str,
    project_name: str | None,
) -> list[str]:
    """List all clusters, optionally narrowing the authoritative result to the project."""
    client = session.client("eks", region_name=region)
    names: set[str] = set()
    for page in client.get_paginator("list_clusters").paginate():
        for raw_name in page.get("clusters", []):
            name = str(raw_name or "")
            if not name:
                raise RuntimeError(f"EKS returned a cluster without a name in {region}")
            names.add(name)
    if project_name is None:
        return sorted(names)
    return sorted(name for name in names if _project_owned_name(name, project_name))


def _list_sqs_queues(session: Any, region: str, project_name: str) -> list[str]:
    client = session.client("sqs", region_name=region)
    urls: list[str] = []
    for page in client.get_paginator("list_queues").paginate(QueueNamePrefix=project_name):
        for queue_url in page.get("QueueUrls", []):
            queue_name = urlparse(str(queue_url)).path.rsplit("/", 1)[-1]
            if _project_owned_name(queue_name, project_name):
                urls.append(str(queue_url))
    return sorted(set(urls))


def _list_dynamodb_tables(session: Any, region: str, project_name: str) -> list[str]:
    client = session.client("dynamodb", region_name=region)
    names: list[str] = []
    for page in client.get_paginator("list_tables").paginate():
        names.extend(
            str(name)
            for name in page.get("TableNames", [])
            if _project_owned_name(str(name), project_name)
        )
    return sorted(set(names))


def _list_load_balancers(session: Any, region: str, project_name: str) -> list[str]:
    client = session.client("elbv2", region_name=region)
    load_balancers: list[dict[str, Any]] = []
    for page in client.get_paginator("describe_load_balancers").paginate():
        load_balancers.extend(page.get("LoadBalancers", []))

    owned: list[str] = []
    for start in range(0, len(load_balancers), 20):
        batch = load_balancers[start : start + 20]
        arns = [str(item["LoadBalancerArn"]) for item in batch]
        tags_by_arn = {
            str(item["ResourceArn"]): _tags_to_dict(item.get("Tags", []))
            for item in client.describe_tags(ResourceArns=arns).get("TagDescriptions", [])
        }
        for load_balancer in batch:
            arn = str(load_balancer["LoadBalancerArn"])
            name = str(load_balancer.get("LoadBalancerName") or "")
            if _project_owned_name(name, project_name) or _tags_are_project_owned(
                tags_by_arn.get(arn, {}), project_name
            ):
                owned.append(arn)
    return sorted(set(owned))


def _list_instances(session: Any, region: str, project_name: str) -> list[str]:
    client = session.client("ec2", region_name=region)
    state_filter = {
        "Name": "instance-state-name",
        "Values": ["pending", "running", "stopping", "stopped", "shutting-down"],
    }
    instance_ids: set[str] = set()
    paginator = client.get_paginator("describe_instances")
    for page in paginator.paginate(Filters=[state_filter]):
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                if not _ec2_resource_is_project_owned(instance, project_name):
                    continue
                instance_id = str(instance.get("InstanceId") or "")
                if not instance_id:
                    raise RuntimeError(
                        f"EC2 returned a project-owned instance without an ID in {region}"
                    )
                instance_ids.add(instance_id)
    return sorted(instance_ids)


def _list_project_kms_keys(
    session: Any,
    region: str,
    project_name: str,
) -> list[dict[str, Any]]:
    """List exact customer-managed KMS keys tagged to project stacks."""
    client = session.client("kms", region_name=region)
    keys: list[dict[str, Any]] = []
    for page in client.get_paginator("list_keys").paginate():
        for summary in page.get("Keys", []):
            key_id = str(summary.get("KeyId") or "")
            if not key_id:
                continue
            metadata = client.describe_key(KeyId=key_id).get("KeyMetadata", {})
            if metadata.get("KeyManager") != "CUSTOMER":
                continue
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
                    break
            if not _tags_are_project_owned(tags, project_name):
                continue
            deletion_date = metadata.get("DeletionDate")
            keys.append(
                {
                    "key_id": key_id,
                    "arn": str(metadata.get("Arn") or ""),
                    "state": str(metadata.get("KeyState") or ""),
                    "description": str(metadata.get("Description") or ""),
                    "deletion_date": (
                        deletion_date.isoformat() if deletion_date is not None else None
                    ),
                    "tags": tags,
                }
            )
    return sorted(keys, key=lambda item: (item["arn"], item["key_id"]))


def _list_project_ecr_repositories(
    session: Any,
    region: str,
    project_name: str,
) -> list[str]:
    repositories = collect_ecr_inventory(session, [region])[region]
    return sorted(
        item["name"] for item in repositories if _project_owned_name(item["name"], project_name)
    )


def _global_accelerator_control_region(session: Any, seed_region: str) -> str | None:
    """Return the partition's supported Global Accelerator control Region."""
    partition = session.get_partition_for_region(seed_region)
    control_region = _GLOBAL_ACCELERATOR_CONTROL_REGIONS.get(str(partition))
    if control_region is None:
        return None
    service_regions = set(
        session.get_available_regions("globalaccelerator", partition_name=partition)
    )
    if control_region not in service_regions:
        raise RuntimeError(
            "AWS SDK does not advertise the required Global Accelerator control Region "
            f"{control_region} for partition {partition}"
        )
    return control_region


def _list_global_accelerators(
    session: Any,
    control_region: str | None,
    project_name: str,
) -> list[str]:
    if control_region is None:
        return []
    client = session.client("globalaccelerator", region_name=control_region)
    accelerators: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs = {"NextToken": token} if token else {}
        response = client.list_accelerators(**kwargs)
        accelerators.extend(response.get("Accelerators", []))
        token = response.get("NextToken")
        if not token:
            break

    owned: list[str] = []
    for accelerator in accelerators:
        arn = str(accelerator.get("AcceleratorArn") or "")
        name = str(accelerator.get("Name") or "")
        tags = _tags_to_dict(client.list_tags_for_resource(ResourceArn=arn).get("Tags", []))
        if _project_owned_name(name, project_name) or _tags_are_project_owned(tags, project_name):
            owned.append(arn)
    return sorted(set(owned))


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


def _list_project_tagged_resources(
    session: Any,
    region: str,
    project_name: str,
) -> list[dict[str, Any]]:
    """List project-scoped resources exposed by the regional Tagging API."""
    client = session.client("resourcegroupstaggingapi", region_name=region)
    resources: dict[str, dict[str, Any]] = {}
    for page in client.get_paginator("get_resources").paginate():
        for mapping in page.get("ResourceTagMappingList", []):
            arn = str(mapping.get("ResourceARN") or "")
            if not arn:
                raise RuntimeError(f"Resource Groups Tagging API omitted an ARN in {region}")
            tags = _tags_to_dict(mapping.get("Tags", []))
            if _tags_are_project_owned(tags, project_name) or _arn_is_project_owned(
                arn, project_name
            ):
                resources[arn] = {"arn": arn, "tags": tags}
    return [resources[arn] for arn in sorted(resources)]


def _ec2_items(client: Any, operation: str, response_key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in client.get_paginator(operation).paginate():
        items.extend(page.get(response_key, []))
    return items


def _list_project_ec2_networking(
    session: Any,
    region: str,
    project_name: str,
    project_instance_ids: Iterable[str],
) -> dict[str, list[str]]:
    """Collect tagged networking plus untagged dependants of owned resources."""
    client = session.client("ec2", region_name=region)

    vpc_ids: set[str] = set()
    for vpc in _ec2_items(client, "describe_vpcs", "Vpcs"):
        vpc_id = str(vpc.get("VpcId") or "")
        if not vpc_id:
            raise RuntimeError(f"EC2 returned a VPC without an ID in {region}")
        if _ec2_resource_is_project_owned(vpc, project_name):
            vpc_ids.add(vpc_id)

    subnet_ids: set[str] = set()
    for subnet in _ec2_items(client, "describe_subnets", "Subnets"):
        subnet_id = str(subnet.get("SubnetId") or "")
        if not subnet_id:
            raise RuntimeError(f"EC2 returned a subnet without an ID in {region}")
        if (
            _ec2_resource_is_project_owned(subnet, project_name)
            or str(subnet.get("VpcId") or "") in vpc_ids
        ):
            subnet_ids.add(subnet_id)

    security_group_ids: set[str] = set()
    for security_group in _ec2_items(client, "describe_security_groups", "SecurityGroups"):
        group_id = str(security_group.get("GroupId") or "")
        if not group_id:
            raise RuntimeError(f"EC2 returned a security group without an ID in {region}")
        if (
            _ec2_resource_is_project_owned(security_group, project_name)
            or _project_owned_name(str(security_group.get("GroupName") or ""), project_name)
            or str(security_group.get("VpcId") or "") in vpc_ids
        ):
            security_group_ids.add(group_id)

    instance_ids = set(project_instance_ids)
    network_interface_ids: set[str] = set()
    for interface in _ec2_items(
        client,
        "describe_network_interfaces",
        "NetworkInterfaces",
    ):
        interface_id = str(interface.get("NetworkInterfaceId") or "")
        if not interface_id:
            raise RuntimeError(f"EC2 returned a network interface without an ID in {region}")
        group_ids = {str(group.get("GroupId") or "") for group in interface.get("Groups", [])}
        attachment_instance_id = str((interface.get("Attachment") or {}).get("InstanceId") or "")
        if (
            _ec2_resource_is_project_owned(interface, project_name)
            or str(interface.get("VpcId") or "") in vpc_ids
            or str(interface.get("SubnetId") or "") in subnet_ids
            or bool(group_ids & security_group_ids)
            or attachment_instance_id in instance_ids
        ):
            network_interface_ids.add(interface_id)

    elastic_ip_ids: set[str] = set()
    for address in client.describe_addresses().get("Addresses", []):
        identifier = str(address.get("AllocationId") or address.get("PublicIp") or "")
        if not identifier:
            raise RuntimeError(f"EC2 returned an Elastic IP without an identity in {region}")
        if (
            _ec2_resource_is_project_owned(address, project_name)
            or str(address.get("NetworkInterfaceId") or "") in network_interface_ids
            or str(address.get("InstanceId") or "") in instance_ids
        ):
            elastic_ip_ids.add(identifier)

    return {
        "vpcs": sorted(vpc_ids),
        "subnets": sorted(subnet_ids),
        "network_interfaces": sorted(network_interface_ids),
        "security_groups": sorted(security_group_ids),
        "elastic_ips": sorted(elastic_ip_ids),
    }


def _list_lambda_functions(session: Any, region: str, project_name: str) -> list[str]:
    client = session.client("lambda", region_name=region)
    functions: set[str] = set()
    for page in client.get_paginator("list_functions").paginate():
        for function in page.get("Functions", []):
            name = str(function.get("FunctionName") or "")
            arn = str(function.get("FunctionArn") or "")
            if not name or not arn:
                raise RuntimeError(f"Lambda returned a function without identity in {region}")
            tags = _mapping_tags(client.list_tags(Resource=arn).get("Tags"))
            if _project_owned_name(name, project_name) or _tags_are_project_owned(
                tags, project_name
            ):
                functions.add(arn)
    return sorted(functions)


def _list_api_gateway_v1_apis(session: Any, region: str, project_name: str) -> list[str]:
    client = session.client("apigateway", region_name=region)
    apis: set[str] = set()
    for page in client.get_paginator("get_rest_apis").paginate():
        for api in page.get("items", []):
            api_id = str(api.get("id") or "")
            name = str(api.get("name") or "")
            tags = _mapping_tags(api.get("tags"))
            if _project_owned_name(name, project_name) or _tags_are_project_owned(
                tags, project_name
            ):
                if not api_id:
                    raise RuntimeError(f"API Gateway v1 returned an API without an ID in {region}")
                apis.add(api_id)
    return sorted(apis)


def _list_api_gateway_v2_apis(session: Any, region: str, project_name: str) -> list[str]:
    client = session.client("apigatewayv2", region_name=region)
    apis: set[str] = set()
    for page in client.get_paginator("get_apis").paginate():
        for api in page.get("Items", []):
            api_id = str(api.get("ApiId") or "")
            name = str(api.get("Name") or "")
            tags = _mapping_tags(api.get("Tags"))
            if _project_owned_name(name, project_name) or _tags_are_project_owned(
                tags, project_name
            ):
                if not api_id:
                    raise RuntimeError(f"API Gateway v2 returned an API without an ID in {region}")
                apis.add(api_id)
    return sorted(apis)


def _list_cloudwatch_log_groups(
    session: Any,
    region: str,
    project_name: str,
) -> list[str]:
    client = session.client("logs", region_name=region)
    log_groups: set[str] = set()
    for page in client.get_paginator("describe_log_groups").paginate():
        for log_group in page.get("logGroups", []):
            name = str(log_group.get("logGroupName") or "")
            arn = str(log_group.get("logGroupArn") or log_group.get("arn") or "").removesuffix(":*")
            if not name or not arn:
                raise RuntimeError(
                    f"CloudWatch Logs returned a log group without identity in {region}"
                )
            tags = _mapping_tags(client.list_tags_for_resource(resourceArn=arn).get("tags"))
            if _name_or_path_is_project_owned(name, project_name) or _tags_are_project_owned(
                tags, project_name
            ):
                log_groups.add(name)
    return sorted(log_groups)


def _list_secrets(session: Any, region: str, project_name: str) -> list[str]:
    client = session.client("secretsmanager", region_name=region)
    secrets: set[str] = set()
    for page in client.get_paginator("list_secrets").paginate(IncludePlannedDeletion=True):
        for secret in page.get("SecretList", []):
            name = str(secret.get("Name") or "")
            arn = str(secret.get("ARN") or "")
            tags = _tags_to_dict(secret.get("Tags", []))
            if _project_owned_name(name, project_name) or _tags_are_project_owned(
                tags, project_name
            ):
                if not arn:
                    raise RuntimeError(
                        f"Secrets Manager returned a project-owned secret without an ARN in {region}"
                    )
                secrets.add(arn)
    return sorted(secrets)


def _list_s3_bucket_tags(client: Any, bucket_name: str) -> dict[str, str]:
    try:
        response = client.get_bucket_tagging(Bucket=bucket_name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchTagSet", "NoSuchTagSetError"}:
            return {}
        raise
    return _tags_to_dict(response.get("TagSet", []))


def _list_project_s3_buckets(session: Any, seed_region: str, project_name: str) -> list[str]:
    client = session.client("s3", region_name=seed_region)
    buckets: set[str] = set()
    for bucket in client.list_buckets().get("Buckets", []):
        name = str(bucket.get("Name") or "")
        if not name:
            raise RuntimeError("S3 returned a bucket without a name")
        tags = _list_s3_bucket_tags(client, name)
        if _project_owned_name(name, project_name) or _tags_are_project_owned(tags, project_name):
            buckets.add(name)
    return sorted(buckets)


def _list_iam_tags(
    client: Any,
    operation: str,
    identifier_name: str,
    identifier: str,
) -> dict[str, str]:
    tags: dict[str, str] = {}
    marker: str | None = None
    while True:
        kwargs: dict[str, Any] = {identifier_name: identifier}
        if marker:
            kwargs["Marker"] = marker
        response = getattr(client, operation)(**kwargs)
        tags.update(_tags_to_dict(response.get("Tags", [])))
        if not response.get("IsTruncated"):
            return tags
        marker = str(response.get("Marker") or "")
        if not marker:
            raise RuntimeError(f"IAM {operation} truncated its response without a Marker")


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


def _list_project_iam_resources(
    session: Any,
    seed_region: str,
    project_name: str,
) -> dict[str, list[str]]:
    client = session.client("iam", region_name=seed_region)
    resources: dict[str, set[str]] = {
        "iam_roles": set(),
        "iam_policies": set(),
        "iam_instance_profiles": set(),
        "iam_users": set(),
        "iam_groups": set(),
    }

    for page in client.get_paginator("list_roles").paginate():
        for role in page.get("Roles", []):
            name = str(role.get("RoleName") or "")
            arn = str(role.get("Arn") or "")
            if not name or not arn:
                raise RuntimeError("IAM returned a role without identity")
            tags = _list_iam_tags(client, "list_role_tags", "RoleName", name)
            if _iam_resource_is_project_owned(
                name, str(role.get("Path") or ""), tags, project_name
            ):
                resources["iam_roles"].add(arn)

    for page in client.get_paginator("list_policies").paginate(Scope="Local"):
        for policy in page.get("Policies", []):
            name = str(policy.get("PolicyName") or "")
            arn = str(policy.get("Arn") or "")
            if not name or not arn:
                raise RuntimeError("IAM returned a customer-managed policy without identity")
            tags = _list_iam_tags(client, "list_policy_tags", "PolicyArn", arn)
            if _iam_resource_is_project_owned(
                name, str(policy.get("Path") or ""), tags, project_name
            ):
                resources["iam_policies"].add(arn)

    for page in client.get_paginator("list_instance_profiles").paginate():
        for profile in page.get("InstanceProfiles", []):
            name = str(profile.get("InstanceProfileName") or "")
            arn = str(profile.get("Arn") or "")
            if not name or not arn:
                raise RuntimeError("IAM returned an instance profile without identity")
            tags = _list_iam_tags(
                client,
                "list_instance_profile_tags",
                "InstanceProfileName",
                name,
            )
            if _iam_resource_is_project_owned(
                name, str(profile.get("Path") or ""), tags, project_name
            ):
                resources["iam_instance_profiles"].add(arn)

    for page in client.get_paginator("list_users").paginate():
        for user in page.get("Users", []):
            name = str(user.get("UserName") or "")
            arn = str(user.get("Arn") or "")
            if not name or not arn:
                raise RuntimeError("IAM returned a user without identity")
            tags = _list_iam_tags(client, "list_user_tags", "UserName", name)
            if _iam_resource_is_project_owned(
                name, str(user.get("Path") or ""), tags, project_name
            ):
                resources["iam_users"].add(arn)

    for page in client.get_paginator("list_groups").paginate():
        for group in page.get("Groups", []):
            name = str(group.get("GroupName") or "")
            arn = str(group.get("Arn") or "")
            if not name or not arn:
                raise RuntimeError("IAM returned a group without identity")
            if _project_owned_name(name, project_name) or _name_or_path_is_project_owned(
                str(group.get("Path") or ""), project_name
            ):
                resources["iam_groups"].add(arn)

    return {key: sorted(values) for key, values in resources.items()}


def _backup_tags(client: Any, arn: str) -> dict[str, str]:
    return _mapping_tags(client.list_tags(ResourceArn=arn).get("Tags"))


def _list_project_backup_resources(
    session: Any,
    region: str,
    project_name: str,
) -> dict[str, list[str]]:
    client = session.client("backup", region_name=region)
    resources: dict[str, set[str]] = {
        "backup_vaults": set(),
        "backup_plans": set(),
        "backup_selections": set(),
        "backup_recovery_points": set(),
    }

    vaults: list[dict[str, Any]] = []
    for page in client.get_paginator("list_backup_vaults").paginate():
        vaults.extend(page.get("BackupVaultList", []))
    owned_vault_names: set[str] = set()
    for vault in vaults:
        name = str(vault.get("BackupVaultName") or "")
        arn = str(vault.get("BackupVaultArn") or "")
        if not name or not arn:
            raise RuntimeError(f"AWS Backup returned a vault without identity in {region}")
        tags = _backup_tags(client, arn)
        if _project_owned_name(name, project_name) or _tags_are_project_owned(tags, project_name):
            owned_vault_names.add(name)
            resources["backup_vaults"].add(arn)

    for vault in vaults:
        vault_name = str(vault["BackupVaultName"])
        for page in client.get_paginator("list_recovery_points_by_backup_vault").paginate(
            BackupVaultName=vault_name
        ):
            for recovery_point in page.get("RecoveryPoints", []):
                arn = str(recovery_point.get("RecoveryPointArn") or "")
                if not arn:
                    raise RuntimeError(
                        f"AWS Backup returned a recovery point without an ARN in {region}"
                    )
                tags = _backup_tags(client, arn)
                resource_name = str(recovery_point.get("ResourceName") or "")
                resource_arn = str(recovery_point.get("ResourceArn") or "")
                if (
                    vault_name in owned_vault_names
                    or _name_or_path_is_project_owned(resource_name, project_name)
                    or _arn_is_project_owned(resource_arn, project_name)
                    or _tags_are_project_owned(tags, project_name)
                ):
                    resources["backup_recovery_points"].add(arn)

    plans: list[dict[str, Any]] = []
    for page in client.get_paginator("list_backup_plans").paginate():
        plans.extend(page.get("BackupPlansList", []))
    for plan in plans:
        plan_id = str(plan.get("BackupPlanId") or "")
        name = str(plan.get("BackupPlanName") or "")
        arn = str(plan.get("BackupPlanArn") or "")
        if not plan_id or not name or not arn:
            raise RuntimeError(f"AWS Backup returned a plan without identity in {region}")
        tags = _backup_tags(client, arn)
        owned_plan = _project_owned_name(name, project_name) or _tags_are_project_owned(
            tags, project_name
        )
        if owned_plan:
            resources["backup_plans"].add(arn)
        for page in client.get_paginator("list_backup_selections").paginate(BackupPlanId=plan_id):
            for selection in page.get("BackupSelectionsList", []):
                selection_id = str(selection.get("SelectionId") or "")
                selection_name = str(selection.get("SelectionName") or "")
                if not selection_id:
                    raise RuntimeError(f"AWS Backup returned a selection without an ID in {region}")
                if owned_plan or _project_owned_name(selection_name, project_name):
                    resources["backup_selections"].add(f"{plan_id}:{selection_id}")

    return {key: sorted(values) for key, values in resources.items()}


def collect_project_resources(
    session: Any,
    *,
    enabled_regions: Iterable[str],
    project_name: str,
    seed_region: str,
) -> dict[str, Any]:
    """Collect project resources with explicit, fail-closed scanner coverage."""
    regions = sorted(set(enabled_regions))
    partition = session.get_partition_for_region(seed_region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for {seed_region}")

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
            else:
                regional[region][category] = collector(session, region, project_name)
        completed_scanners.append(scanner)

        if scanner == "ec2_instances":
            scanner_regions["ec2_networking"] = applicable_regions
            for region in applicable_regions:
                regional[region].update(
                    _list_project_ec2_networking(
                        session,
                        region,
                        project_name,
                        regional[region]["instances"],
                    )
                )
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
        "cloudformation_stacks": cloudformation_stacks,
        "authoritative_eks_clusters": authoritative_eks_clusters,
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
