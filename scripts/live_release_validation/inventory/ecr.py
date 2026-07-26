"""ECR repository, image, and manifest inventory."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from botocore.exceptions import ClientError

from ._shared import (
    _ECR_MANIFEST_MEDIA_TYPES,
    _normalize_json_text,
    _tags_to_dict,
)


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
