"""Destroying the stacks ignores the deprecated ``images`` destroy settings.

No stack creates the ``<project_name>/*`` ECR repositories: the gco CLI creates
them through the ECR API (``gco images init`` / ``build`` / ``push`` /
``mirror``), and no CloudFormation resource deletes them, so a destroy has
always kept them. ``images.removal_policy`` and ``images.empty_on_delete``
only drove a CLI check before the destroy, which refused to destroy the
global stack (``removal_policy: "destroy"`` with ``empty_on_delete: false``)
or asked to "delete every <project>/* repo" (with ``empty_on_delete: true``)
although nothing deleted them. Both keys are now deprecated and ignored, and
the destroy runs the same whatever they say.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cli.stacks import StackManager

_DEPRECATED_DESTROY_SETTINGS: list[dict[str, Any]] = [
    {"removal_policy": "destroy", "empty_on_delete": False},
    {"removal_policy": "destroy", "empty_on_delete": True},
    {"removal_policy": "retain"},
]


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StackManager:
    """A StackManager over a temporary checkout (cwd) with no Lambda build."""
    monkeypatch.chdir(tmp_path)
    config = MagicMock()
    config.project_name = "gco"
    config.global_region = "us-east-2"
    with patch.object(StackManager, "_ensure_lambda_build", lambda self: None):
        return StackManager(config, project_root=tmp_path)


def _write_images_block(tmp_path: Path, images: dict[str, Any]) -> None:
    (tmp_path / "cdk.json").write_text(json.dumps({"context": {"images": images}}))


@pytest.mark.parametrize("images", _DEPRECATED_DESTROY_SETTINGS)
def test_the_global_stack_destroy_runs_whatever_the_images_block_says(
    manager: StackManager,
    tmp_path: Path,
    images: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_images_block(tmp_path, images)
    with (
        patch.object(manager, "_run_cdk", return_value=MagicMock(returncode=0)) as run_cdk,
        patch.object(manager, "_stack_exists_in_cloudformation", return_value=False),
        patch("cli.stacks.sys.stdin") as stdin,
    ):
        stdin.isatty.return_value = True
        assert manager.destroy(stack_name="gco-global", force=False) is True
    run_cdk.assert_called_once()
    assert "repo" not in capsys.readouterr().out.lower()


def test_the_orchestrated_teardown_is_not_stopped_by_the_images_block(
    manager: StackManager, tmp_path: Path
) -> None:
    _write_images_block(tmp_path, {"removal_policy": "destroy", "empty_on_delete": False})
    stacks = ["gco-global", "gco-us-east-1"]
    with (
        patch.object(StackManager, "list_stacks", return_value=stacks),
        patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=False),
        patch.object(StackManager, "destroy", return_value=True) as destroy,
    ):
        success, _successful, failed = manager.destroy_orchestrated(force=True)
    assert success is True
    assert failed == []
    assert destroy.call_count >= len(stacks)
