"""ECR expectation, ownership checkpointing, and baseline stripping."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from typing import Any

from ..constants import (
    _RUN_STACK_TAG,
)
from ..context import (
    _topology_regions,
)
from ..inventory import (
    collect_ecr_inventory,
)
from ..models import RunContext
from ..protected import (
    _EC2_TAGGED_RESOURCE_IDENTITIES,
    _PROTECTED_GLOBAL_RESOURCE_CATEGORIES,
    _PROTECTED_REGIONAL_RESOURCE_CATEGORIES,
    _baseline_protected_identities,
    _ec2_tagged_resource_identity,
    _eks_pod_parent_cluster,
    _matches_protected_physical_identity,
    _tagged_resource_is_protected,
)


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
