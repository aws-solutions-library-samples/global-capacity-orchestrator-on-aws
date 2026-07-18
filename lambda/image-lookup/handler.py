"""
Lookup-or-create custom resource handler for ECR repositories.

Handles CloudFormation custom resource events for ``gco/<name>``
repositories. The handler implements the adopt-or-create pattern so
that a previously retained repository (left over from a prior deploy
with ``RemovalPolicy=RETAIN``) is rebound to the new stack rather than
failing with ``RepositoryAlreadyExistsException``.

Event shape (CloudFormation custom resource):

    event["RequestType"]      — "Create" | "Update" | "Delete"
    event["ResourceProperties"]:
        RepositoryName        — full repo name like ``gco/my-app``
        RemovalPolicy         — ``"retain"`` | ``"destroy"``
        EmptyOnDelete         — ``True`` | ``False``
        LifecyclePolicy       — optional JSON string of the lifecycle policy

Behaviour:

    Create / Update:
        DescribeRepositories → if found, adopt; else CreateRepository.
        Then PutLifecyclePolicy when ``LifecyclePolicy`` is provided.

    Delete:
        Read tags via ListTagsForResource. If ``gco:retain=true`` is
        set on the repo, log + return success without deleting (the
        retain tag wins regardless of stack-level ``RemovalPolicy``).
        Else: when ``RemovalPolicy=="destroy"`` AND
        ``EmptyOnDelete==True``, BatchDeleteImage every image then
        DeleteRepository. When ``RemovalPolicy=="destroy"`` AND
        ``EmptyOnDelete==False``, DeleteRepository (which ECR rejects
        for non-empty repos — surfaces as a CloudFormation rollback).
        When ``RemovalPolicy=="retain"``, return success without any
        delete call.

The handler returns the standard CloudFormation custom resource shape
``{"PhysicalResourceId": <repo_arn>, "Data": {...}}``. The CDK
``Provider`` framework wraps this into the protocol-required response
envelope when invoked through ``CustomResource``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-07-18T01:03:40Z
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/image-lookup/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/image-lookup/handler.lambda_handler.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _ecr_client() -> Any:
    """Return a region-default ECR boto3 client."""
    return boto3.client("ecr")


def _describe_repository(ecr: Any, repository_name: str) -> dict[str, Any] | None:
    """Return the repository description if it exists, else ``None``.

    ECR raises ``RepositoryNotFoundException`` when the named repository
    does not exist; we translate that into ``None`` so the caller can
    distinguish missing from an actual API error.
    """
    try:
        resp = ecr.describe_repositories(repositoryNames=[repository_name])
    except ecr.exceptions.RepositoryNotFoundException:
        return None
    except Exception as exc:  # noqa: BLE001
        # Some boto3 stubs surface RepositoryNotFoundException via the
        # generic ClientError shape rather than the typed exception. Sniff
        # the error code and translate consistently.
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code == "RepositoryNotFoundException":
            return None
        raise

    repos = resp.get("repositories", [])
    return repos[0] if repos else None


def _create_repository(ecr: Any, repository_name: str) -> dict[str, Any]:
    """Create the named repository with project-standard configuration."""
    resp = ecr.create_repository(
        repositoryName=repository_name,
        imageTagMutability="MUTABLE",
        imageScanningConfiguration={"scanOnPush": True},
    )
    repo: dict[str, Any] = resp.get("repository", {})
    return repo


def _apply_lifecycle_policy(ecr: Any, repository_name: str, lifecycle_policy: str | None) -> None:
    """Apply ``lifecycle_policy`` (a JSON string) to the repository when set.

    Silently no-ops when the value is empty or whitespace. Validates the
    JSON shape before calling ``put_lifecycle_policy`` so a malformed
    policy surfaces as a custom-resource error rather than a confusing
    ECR-side validation failure.
    """
    if not lifecycle_policy or not lifecycle_policy.strip():
        return
    # Validate the JSON parses; ``put_lifecycle_policy`` accepts the raw
    # string, but this gives a clearer error message on invalid input.
    json.loads(lifecycle_policy)
    ecr.put_lifecycle_policy(
        repositoryName=repository_name,
        lifecyclePolicyText=lifecycle_policy,
    )


def _has_retain_tag(ecr: Any, repository_arn: str) -> bool:
    """Return True when ``gco:retain=true`` is present on the repository."""
    try:
        resp = ecr.list_tags_for_resource(resourceArn=repository_arn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_tags_for_resource failed for %s: %s", repository_arn, exc)
        return False
    for tag in resp.get("tags", []) or []:
        if tag.get("Key") == "gco:retain" and str(tag.get("Value", "")).lower() == "true":
            return True
    return False


def _delete_all_images(ecr: Any, repository_name: str) -> int:
    """BatchDeleteImage every image in the repository.

    Returns the number of images deleted. ECR's ``batch_delete_image``
    accepts up to 100 IDs per call so we paginate through both
    ``describe_images`` (to discover digests) and the chunked delete.
    """
    digests: list[dict[str, str]] = []
    paginator = ecr.get_paginator("describe_images")
    for page in paginator.paginate(repositoryName=repository_name):
        for detail in page.get("imageDetails", []):
            digest = detail.get("imageDigest")
            if digest:
                digests.append({"imageDigest": digest})

    deleted = 0
    for chunk_start in range(0, len(digests), 100):
        chunk = digests[chunk_start : chunk_start + 100]
        if not chunk:
            continue
        resp = ecr.batch_delete_image(
            repositoryName=repository_name,
            imageIds=chunk,
        )
        deleted += len(resp.get("imageIds", []))
    return deleted


def _handle_create_or_update(ecr: Any, properties: dict[str, Any]) -> dict[str, Any]:
    """Adopt-or-create the repository and apply the lifecycle policy."""
    repository_name = properties["RepositoryName"]
    lifecycle_policy = properties.get("LifecyclePolicy")

    existing = _describe_repository(ecr, repository_name)
    if existing is not None:
        repository_arn = existing.get("repositoryArn")
        repository_uri = existing.get("repositoryUri")
        adopted = True
    else:
        created = _create_repository(ecr, repository_name)
        repository_arn = created.get("repositoryArn")
        repository_uri = created.get("repositoryUri")
        adopted = False

    if lifecycle_policy:
        _apply_lifecycle_policy(ecr, repository_name, lifecycle_policy)

    return {
        "PhysicalResourceId": repository_arn or repository_name,
        "Data": {
            "RepositoryArn": repository_arn or "",
            "RepositoryUri": repository_uri or "",
            "RepositoryName": repository_name,
            "Adopted": "true" if adopted else "false",
        },
    }


def _handle_delete(ecr: Any, properties: dict[str, Any], physical_id: str) -> dict[str, Any]:
    """Honor the retain tag, removal policy, and empty-on-delete switches."""
    repository_name = properties["RepositoryName"]
    removal_policy = str(properties.get("RemovalPolicy", "retain")).lower()
    empty_on_delete = bool(properties.get("EmptyOnDelete", False))

    existing = _describe_repository(ecr, repository_name)
    if existing is None:
        # Already gone — treat as success.
        logger.info("Repository %s already absent on Delete; skipping.", repository_name)
        return {"PhysicalResourceId": physical_id, "Data": {"Deleted": "false"}}

    repository_arn = existing.get("repositoryArn", physical_id)

    if _has_retain_tag(ecr, repository_arn):
        logger.info(
            "Repository %s carries gco:retain=true; preserving despite removal_policy=%s.",
            repository_name,
            removal_policy,
        )
        return {
            "PhysicalResourceId": physical_id,
            "Data": {"Deleted": "false", "Reason": "retain-tag"},
        }

    if removal_policy != "destroy":
        logger.info(
            "removal_policy=%s for %s; leaving the repository in place.",
            removal_policy,
            repository_name,
        )
        return {
            "PhysicalResourceId": physical_id,
            "Data": {"Deleted": "false", "Reason": "removal-policy-retain"},
        }

    if empty_on_delete:
        deleted = _delete_all_images(ecr, repository_name)
        logger.info("Deleted %d images from %s before repo deletion.", deleted, repository_name)

    ecr.delete_repository(repositoryName=repository_name, force=False)
    return {"PhysicalResourceId": physical_id, "Data": {"Deleted": "true"}}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lookup-or-create custom resource handler for ECR repositories.

    Dispatches on ``event["RequestType"]`` and returns the standard
    CloudFormation custom resource response shape. The CDK Provider
    framework wraps this dict into the protocol-required envelope.
    """
    request_type = event.get("RequestType", "")
    properties = event.get("ResourceProperties", {}) or {}
    physical_id = event.get("PhysicalResourceId", "")
    logger.info("Image-Lookup CR event: %s for %s", request_type, properties.get("RepositoryName"))

    ecr = _ecr_client()

    if request_type in ("Create", "Update"):
        return _handle_create_or_update(ecr, properties)
    if request_type == "Delete":
        return _handle_delete(ecr, properties, physical_id)

    raise ValueError(f"Unsupported RequestType: {request_type!r}")
