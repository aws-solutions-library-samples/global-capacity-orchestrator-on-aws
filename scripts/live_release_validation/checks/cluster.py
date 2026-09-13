"""kubectl plumbing shared by the cluster-facing checks.

The ``platform-workloads`` and ``network-posture`` actions read and create
Kubernetes objects on every deployed Region's cluster. Both reach the private
API endpoint the way the ``inference`` action does — access entry, SSM tunnel,
and the isolated ``kubeconfig`` inside the private report directory
(``scripts.example_job_validation.kube.cluster_session``) — and both need
fail-closed JSON reads that distinguish "the object is absent" from "the read
broke". This module owns that plumbing so neither check re-implements it.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from scripts.example_job_validation import kube

from ..models import RunContext

#: ``kubectl(*argv, timeout=..., **subprocess_kwargs) -> (returncode, stdout, stderr)``
KubectlRunner = Callable[..., tuple[int, str, str]]

_OUTPUT_LIMIT = 1_000


class KubectlError(RuntimeError):
    """A kubectl invocation the checks could not interpret."""


def _truncated(value: str) -> str:
    return value if len(value) <= _OUTPUT_LIMIT else value[-_OUTPUT_LIMIT:]


def _not_found(stderr: str) -> bool:
    lowered = stderr.casefold()
    return "notfound" in lowered or "not found" in lowered


def kubectl_json(
    kubectl: KubectlRunner,
    record: dict[str, Any],
    *arguments: str,
    timeout: float,
) -> Any | None:
    """Run ``kubectl <arguments> --output json``; ``None`` when the object is absent.

    Any other non-zero exit, and any unparsable output, is recorded in
    ``record["last_kubectl_error"]`` (bounded) and raised, so a broken tunnel
    or a revoked permission can never read as "the object was not there".
    """
    code, stdout, stderr = kubectl(*arguments, "--output", "json", timeout=timeout)
    if code != 0:
        if _not_found(stderr):
            return None
        record["last_kubectl_error"] = {
            "argv": list(arguments),
            "returncode": code,
            "stdout": _truncated(stdout),
            "stderr": _truncated(stderr),
        }
        raise KubectlError(
            f"kubectl {' '.join(arguments[:2])} failed with exit {code}; "
            "the checkpoint holds the output"
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        record["last_kubectl_error"] = {
            "argv": list(arguments),
            "returncode": code,
            "stdout": _truncated(stdout),
            "error": f"{type(exc).__name__}: {exc}",
        }
        raise KubectlError(
            f"kubectl {' '.join(arguments[:2])} returned invalid JSON; "
            "the checkpoint holds the output"
        ) from None


@contextmanager
def cluster_kubectl(ctx: RunContext, region: str) -> Iterator[KubectlRunner]:
    """Yield a tunnelled kubectl for one Region's cluster via the isolated kubeconfig.

    The kubeconfig is the single private-directory file the runner already
    accounts for (``RunSettings.kubeconfig_path``); Regions are visited one at
    a time, and each session re-points it at its own cluster.
    """
    settings = ctx.settings
    kubeconfig_path = settings.kubeconfig_path
    if kubeconfig_path.parent != settings.report_dir:
        raise KubectlError("isolated kubeconfig escaped the private report dir")
    cluster_name = f"{ctx.config.project_name}-{region}"
    with kube.cluster_session(
        settings.repo_root,
        cluster_name,
        region,
        kubeconfig_path=kubeconfig_path,
        gco_command=(sys.executable, "-m", "cli.main"),
    ) as kubectl:
        yield kubectl
