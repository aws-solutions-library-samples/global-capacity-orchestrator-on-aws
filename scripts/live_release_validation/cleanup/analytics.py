"""What the analytics probe leaves for the teardown, once the stacks are gone.

The probe's Studio lifecycle configuration is account-level and stays
attached to the profile and app it leaves for the stack's cleanup custom
resource, so it can be deleted only after the analytics stack is. The
SageMaker-managed home EFS of the Studio domain carries no project tag, so no
inventory scanner sees it; the cleanup custom resource deletes it, and this
sweep proves it is gone. It runs as a phase of the retained-resource cleanup
(``cleanup/retained.py``), which every teardown path runs once the stacks are
absent.
"""

from __future__ import annotations

import copy
from typing import Any

from botocore.exceptions import ClientError

from ..checks.analytics import LIFECYCLE_CONFIGS_STATE
from ..models import RunContext


class AnalyticsResidueError(RuntimeError):
    """The sweep left analytics residue behind; carries the partial evidence."""

    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.details = copy.deepcopy(details)


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code") or "")


def cleanup_analytics_residue(ctx: RunContext) -> dict[str, Any]:
    """Delete the run's lifecycle configurations and require the domain's home EFS gone.

    Only names the checkpoint recorded before they were created are touched,
    and one already gone counts as deleted. Idempotent, so every resume that
    finds the stacks gone runs it again. Raises :class:`AnalyticsResidueError`
    with the outcome when anything is left.
    """
    outcome: dict[str, Any] = {
        "deleted_lifecycle_configs": [],
        "absent_lifecycle_configs": [],
        "errors": [],
    }
    for record in ctx.checkpoint.state.get(LIFECYCLE_CONFIGS_STATE) or []:
        region, name = str(record.get("region") or ""), str(record.get("name") or "")
        if not region or not name:
            outcome["errors"].append(f"invalid lifecycle-config record {record!r}")
            continue
        sagemaker = ctx.session.client("sagemaker", region_name=region)
        try:
            sagemaker.delete_studio_lifecycle_config(StudioLifecycleConfigName=name)
        except ClientError as exc:
            if _error_code(exc) == "ResourceNotFound":
                outcome["absent_lifecycle_configs"].append(f"{region}:{name}")
            else:
                outcome["errors"].append(f"{region}:{name}: {_error_code(exc)}")
        else:
            outcome["deleted_lifecycle_configs"].append(f"{region}:{name}")
    domain = ctx.checkpoint.state.get("analytics_domain") or {}
    home_efs_id = str(domain.get("home_efs_id") or "")
    if home_efs_id:
        efs = ctx.session.client("efs", region_name=str(domain.get("region")))
        try:
            efs.describe_file_systems(FileSystemId=home_efs_id)
        except ClientError as exc:
            if _error_code(exc) != "FileSystemNotFound":
                outcome["errors"].append(f"home EFS {home_efs_id}: {_error_code(exc)}")
            else:
                outcome["home_efs"] = "absent"
        else:
            outcome["errors"].append(
                f"The Studio domain's home EFS {home_efs_id} survived the analytics stack"
            )
    if outcome["errors"]:
        raise AnalyticsResidueError("; ".join(outcome["errors"]), outcome)
    return outcome
