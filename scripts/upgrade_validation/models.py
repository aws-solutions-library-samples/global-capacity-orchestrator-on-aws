"""Run settings for the upgrade validation harness."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from cli.upgrade import RELEASE_TAG_RE
from scripts.live_release_validation.models import RunSettings

#: CDK context that makes the regional stack retain its custom-resource
#: provider log groups, so the harness deletes them once the stack is gone
#: (``gco/stacks/regional_stack.py``). The release harness adds it to the change
#: sets it prepares; here it lives in the base checkout's cdk.json.
PROVIDER_LOG_CONTEXT = "gco_live_validation_retain_provider_log_groups"

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def _real(path: Path) -> Path:
    return Path(os.path.realpath(os.fspath(path)))


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


@dataclass(frozen=True)
class UpgradeRunSettings(RunSettings):
    """Operator inputs for one upgrade-validation run.

    Extends the live-release-validation settings with the release the run
    deploys first (``base_ref``, pinned to ``base_commit`` so a moved tag cannot
    change what a resumed run deploys) and the private workspace that holds
    that release's clone, its git mirror, and its virtual environment. The
    workspace must not overlap the checkout (the preflight's clean-tree rule
    and the CDK asset staging both read it) or the report directory (which
    may hold only report files, and outlives the workspace).
    """

    allows_run_tag_adoption: ClassVar[bool] = True

    #: The release tag deployed first and upgraded from (``vMAJOR.MINOR.PATCH``).
    base_ref: str = ""
    #: The commit ``base_ref`` named when the run started.
    base_commit: str = ""
    #: Private directory for the base clone, its mirror, its venv, and logs.
    workspace_dir: Path = Path()
    #: Wall-clock caps for the two long ``gco`` subprocesses.
    deploy_timeout_seconds: int = 3 * 60 * 60
    upgrade_timeout_seconds: int = 4 * 60 * 60
    #: How long the sentinel calls wait for the API to answer.
    api_ready_timeout_seconds: int = 15 * 60

    def __post_init__(self) -> None:
        super().__post_init__()
        if not RELEASE_TAG_RE.fullmatch(self.base_ref):
            raise ValueError(f"base_ref must be a release tag vX.Y.Z, not {self.base_ref!r}")
        if not _COMMIT_RE.fullmatch(self.base_commit):
            raise ValueError("base_commit must be a lowercase 40-character commit SHA")
        if self.base_commit == self.expected_sha.lower():
            raise ValueError(
                f"{self.base_ref} is the checked-out commit, so there is nothing to upgrade"
            )
        workspace = Path(os.path.abspath(os.fspath(self.workspace_dir)))
        object.__setattr__(self, "workspace_dir", workspace)
        for label, directory in (
            ("the checkout", self.repo_root),
            ("the report directory", self.report_dir),
        ):
            if _overlaps(_real(workspace), _real(directory)):
                raise ValueError(f"The workspace {workspace} must not overlap {label}")
        for name in (
            "deploy_timeout_seconds",
            "upgrade_timeout_seconds",
            "api_ready_timeout_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")

    def base_cdk_context(self) -> dict[str, str]:
        """Run-scoped context the base checkout's cdk.json carries.

        ``gco`` in the base checkout deploys, and ``gco upgrade`` redeploys,
        without the harness's ``--context`` flags, so the context has to live
        in the one file the upgrade preserves byte for byte.
        """
        return {**self.extra_cdk_context(), PROVIDER_LOG_CONTEXT: "true"}

    def identity(self) -> dict[str, Any]:
        identity = super().identity()
        identity.update(
            {
                "base_ref": self.base_ref,
                "base_commit": self.base_commit,
                "workspace_dir": str(self.workspace_dir),
                "base_cdk_context": self.base_cdk_context(),
                "stack_ownership": "run-tag-adoption",
            }
        )
        return identity
