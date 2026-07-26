"""Delete exactly run-created ECR images and repositories."""

from __future__ import annotations

from typing import Any

from ..constants import (
    _RUN_STACK_TAG,
)
from ..inventory import (
    collect_ecr_inventory,
    describe_ecr_image_by_tag,
)
from ..models import RunContext
from ..ownership.ecr import (
    _ecr_creation_identity,
    _ecr_image_identity,
)


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
