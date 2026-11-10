"""sandboxes: prove a queued Job runs inside an AWS Nitro Sandbox."""

from __future__ import annotations

from typing import Any

from ..checks.jobs import _run_api_transport_lifecycle
from ..context import effective_kubernetes_version
from ..models import RunContext

#: The first EKS minor with AWS Nitro Sandboxes (docs/NITRO_SANDBOXES.md).
NITRO_SANDBOX_MINIMUM_MINOR = (1, 36)


def action_sandboxes(ctx: RunContext) -> dict[str, Any]:
    """Run a Kueue-queued ``isolated-sandbox`` Job through the API, or say why not.

    Completion proves four things at once: the API admitted the sandboxed
    manifest, which includes finding the cluster's ``isolated-sandbox``
    RuntimeClass; ``gco-cluster-queue`` admitted a workload whose requests carry
    the sandbox slot; EKS Auto Mode provisioned a sandbox-capable node; and the
    pod ran in a sandbox, because it prints the marker only when its disks are
    virtio-blk devices. A cluster below EKS 1.36 cannot run sandboxes, so the
    action records the minor and skips. A Region without Nitro Sandboxes fails
    instead, with the API naming the missing RuntimeClass: a release run there
    cannot validate the feature, and saying so is the result.
    """
    version = effective_kubernetes_version(ctx)
    minor = tuple(int(part) for part in version.split("."))
    if minor < NITRO_SANDBOX_MINIMUM_MINOR:
        result: dict[str, Any] = {
            "status": "skipped",
            "reason": (
                f"EKS {version} predates AWS Nitro Sandboxes, which need 1.36 or later; "
                "pass --kubernetes-version 1.36 or newer to validate them"
            ),
        }
    else:
        lifecycle = _run_api_transport_lifecycle(
            ctx,
            manifest_filename="nitro-sandbox-smoke-job.yaml",
            path="nitro-sandbox",
            marker_prefix="NITRO_SANDBOX",
        )
        result = {"status": "validated", **lifecycle}
    evidence = {"kubernetes_version": version, "sandboxes": result}
    ctx.checkpoint.state["sandbox_validation"] = evidence
    ctx.persist()
    return evidence
