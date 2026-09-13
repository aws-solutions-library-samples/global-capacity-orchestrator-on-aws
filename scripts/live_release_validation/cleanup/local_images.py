"""Reclaim the local container images CDK asset publishing leaves behind.

Every ``deploy`` builds each service image locally, tags it ``cdkasset-<hash>``
plus the ECR asset-repository reference it was pushed under, and never removes
either. Hashes change with the source, so successive runs accumulate one full
image set each — a validation host filled its disk with hundreds of stale asset
images, which failed an image build mid-deploy and then the checkpoint write
that guaranteed cleanup depends on. Once an image is published to ECR the local
copy has no further use (CDK checks ECR before rebuilding), so the deploy
action prunes exactly the CDK asset images plus dangling build layers, and
nothing else the operator keeps in the local store.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from typing import Any

from cli._container_runtime import detect_container_runtime

#: Local tag CDK assigns to every built asset image.
_ASSET_TAG_PREFIX = "cdkasset-"
#: The bootstrap ECR repository CDK pushes asset images to
#: (``cdk-<qualifier>-container-assets-<account>-<region>``), optionally
#: prefixed by the registry host.
_ASSET_REPOSITORY_PATTERN = re.compile(r"(?:^|/)cdk-[a-z0-9]+-container-assets-\d{12}-[a-z0-9-]+$")
_COMMAND_TIMEOUT_SECONDS = 300
#: Cap on the error text retained from a failed runtime command.
_MAX_ERROR_CHARS = 500

RunCommand = Callable[..., "subprocess.CompletedProcess[str]"]


def _is_cdk_asset_repository(repository: str) -> bool:
    """Whether ``repository`` is a CDK asset image reference (either tag form)."""
    if repository.rsplit("/", 1)[-1].startswith(_ASSET_TAG_PREFIX):
        return True
    return _ASSET_REPOSITORY_PATTERN.search(repository) is not None


def _run(runtime: str, arguments: list[str], run: RunCommand) -> subprocess.CompletedProcess[str]:
    return run(
        [runtime, *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )


def prune_cdk_asset_images(
    *,
    runtime: str | None = None,
    run: RunCommand = subprocess.run,
) -> dict[str, Any]:
    """Remove local CDK asset images and dangling layers; never raise.

    Returns evidence for the deploy record: the runtime used, every image
    reference removed, whether dangling layers were pruned, and any runtime
    error text. Absence of a runtime or a failing command is reported, not
    raised — reclaiming local disk must never change a deploy's verdict.
    """
    runtime = runtime or detect_container_runtime()
    if runtime is None:
        return {
            "runtime": None,
            "removed_images": [],
            "dangling_pruned": False,
            "errors": [],
            "skipped": "no container runtime detected",
        }

    errors: list[str] = []
    removed: list[str] = []
    listing = _run(runtime, ["images", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"], run)
    if listing.returncode != 0:
        errors.append(f"images: {(listing.stderr or '').strip()[:_MAX_ERROR_CHARS]}")
    else:
        image_ids: set[str] = set()
        for line in (listing.stdout or "").splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            reference, image_id = parts
            repository = reference.rsplit(":", 1)[0]
            if _is_cdk_asset_repository(repository):
                removed.append(reference)
                image_ids.add(image_id)
        if image_ids:
            result = _run(runtime, ["rmi", "-f", *sorted(image_ids)], run)
            if result.returncode != 0:
                errors.append(f"rmi: {(result.stderr or '').strip()[:_MAX_ERROR_CHARS]}")

    prune = _run(runtime, ["image", "prune", "-f"], run)
    if prune.returncode != 0:
        errors.append(f"image prune: {(prune.stderr or '').strip()[:_MAX_ERROR_CHARS]}")

    return {
        "runtime": runtime,
        "removed_images": sorted(removed),
        "dangling_pruned": prune.returncode == 0,
        "errors": errors,
    }


def prune_local_cdk_asset_images_safely() -> dict[str, Any]:
    """``prune_cdk_asset_images`` that also absorbs unexpected exceptions."""
    try:
        return prune_cdk_asset_images()
    except Exception as exc:  # noqa: BLE001 - disk reclamation is best-effort by contract
        return {
            "runtime": None,
            "removed_images": [],
            "dangling_pruned": False,
            "errors": [f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]],
        }
