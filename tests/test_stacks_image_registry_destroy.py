"""
Tests for the image-registry pre-destroy guards in
``cli/stacks.StackManager``.

Covers the two rules added by the global-stack image-registry
integration:

  - ``removal_policy: "destroy"`` AND ``empty_on_delete: false``
    refuses with the literal helpful-error message.
  - ``removal_policy: "destroy"`` AND ``empty_on_delete: true``
    prints the inventory summary, then prompts on a TTY (proceeds
    on non-TTY).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cdk_json_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Yield a factory that writes a cdk.json under ``tmp_path``.

    The factory accepts an ``images`` dict and writes it to
    ``cdk.json`` under tmp_path. The fixture also chdirs into
    tmp_path so ``_find_cdk_json`` resolves it.
    """

    def make(images_block: dict | None) -> Path:
        ctx: dict = {"context": {}}
        if images_block is not None:
            ctx["context"]["images"] = images_block
        path = tmp_path / "cdk.json"
        path.write_text(json.dumps(ctx))
        return path

    monkeypatch.chdir(tmp_path)
    return make


@pytest.fixture
def manager_factory(tmp_path: Path):
    """Build a StackManager with a mock config + project_root pinned to tmp_path."""

    def make() -> object:
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        config.global_region = "us-east-2"
        with patch.object(StackManager, "_ensure_lambda_build", lambda self: None):
            mgr = StackManager(config, project_root=tmp_path)
        return mgr

    return make


# ---------------------------------------------------------------------------
# 9.15 — refuse-with-helpful-error
# ---------------------------------------------------------------------------


def test_destroy_refuses_when_destroy_policy_and_not_empty_on_delete(
    manager_factory, cdk_json_factory, capsys: pytest.CaptureFixture
):
    """The literal helpful-error message is printed and destroy aborts."""
    cdk_json_factory({"removal_policy": "destroy", "empty_on_delete": False})
    mgr = manager_factory()

    with patch.object(mgr, "_run_cdk") as mock_cdk:
        result = mgr.destroy(stack_name="gco-global", force=True)

    assert result is False
    # CFN delete must not have been invoked.
    mock_cdk.assert_not_called()
    out = capsys.readouterr().out
    assert (
        "Repos under gco/* are not empty and empty_on_delete is false. "
        "Run 'gco images cleanup --all' first, or set "
        "images.empty_on_delete: true in cdk.json." in out
    )


# ---------------------------------------------------------------------------
# 9.14 — pre-destroy inventory summary
# ---------------------------------------------------------------------------


def test_destroy_prints_inventory_when_destroy_and_empty_on_delete(
    manager_factory, cdk_json_factory, capsys: pytest.CaptureFixture
):
    """The inventory summary is printed before CFN delete fires."""
    cdk_json_factory({"removal_policy": "destroy", "empty_on_delete": True})
    mgr = manager_factory()

    fake_inventory = {
        "repo_count": 3,
        "tag_count": 17,
        "total_bytes": 5 * 1024**3,  # 5 GiB
        "endpoint_refs": 2,
        "job_refs": 1,
    }
    with (
        patch.object(
            mgr, "_build_image_registry_inventory", return_value=fake_inventory
        ) as mock_inv,
        patch.object(mgr, "_run_cdk") as mock_cdk,
        patch.object(mgr, "_stack_exists_in_cloudformation", return_value=False),
    ):
        mock_cdk.return_value = MagicMock(returncode=0)
        result = mgr.destroy(stack_name="gco-global", force=True)

    assert result is True
    mock_inv.assert_called_once()
    out = capsys.readouterr().out
    assert "Image registry inventory before destroy" in out
    assert "repos:            3" in out
    assert "tags:             17" in out
    assert "5.00 GiB" in out
    assert "referencing endpoints: 2" in out
    assert "recent job refs:  1" in out


def test_destroy_with_retain_policy_skips_inventory_and_proceeds(
    manager_factory, cdk_json_factory, capsys: pytest.CaptureFixture
):
    """The default ``retain`` policy skips the preflight entirely."""
    cdk_json_factory({"removal_policy": "retain"})
    mgr = manager_factory()

    with (
        patch.object(mgr, "_build_image_registry_inventory") as mock_inv,
        patch.object(mgr, "_run_cdk") as mock_cdk,
        patch.object(mgr, "_stack_exists_in_cloudformation", return_value=False),
    ):
        mock_cdk.return_value = MagicMock(returncode=0)
        result = mgr.destroy(stack_name="gco-global", force=True)

    assert result is True
    mock_inv.assert_not_called()
    out = capsys.readouterr().out
    assert "Image registry inventory" not in out


def test_destroy_no_cdk_json_defaults_to_retain(
    manager_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When no cdk.json is present, defaults apply and destroy proceeds."""
    monkeypatch.chdir(tmp_path)
    mgr = manager_factory()
    with (
        patch.object(mgr, "_run_cdk") as mock_cdk,
        patch.object(mgr, "_stack_exists_in_cloudformation", return_value=False),
    ):
        mock_cdk.return_value = MagicMock(returncode=0)
        result = mgr.destroy(stack_name="gco-global", force=True)
    assert result is True


def test_destroy_other_stacks_not_affected_by_image_preflight(manager_factory, cdk_json_factory):
    """The preflight only runs for gco-global; regional stacks are unchanged."""
    cdk_json_factory({"removal_policy": "destroy", "empty_on_delete": False})
    mgr = manager_factory()
    with (
        patch.object(mgr, "_image_registry_destroy_preflight") as mock_pre,
        patch.object(mgr, "_run_cdk") as mock_cdk,
        patch.object(mgr, "_stack_exists_in_cloudformation", return_value=False),
    ):
        mock_cdk.return_value = MagicMock(returncode=0)
        result = mgr.destroy(stack_name="gco-us-east-1", force=True)

    assert result is True
    mock_pre.assert_not_called()


def test_destroy_tty_prompt_decline(
    manager_factory, cdk_json_factory, capsys: pytest.CaptureFixture
):
    """A TTY operator answering ``n`` aborts the destroy."""
    cdk_json_factory({"removal_policy": "destroy", "empty_on_delete": True})
    mgr = manager_factory()

    with (
        patch.object(
            mgr,
            "_build_image_registry_inventory",
            return_value={
                "repo_count": 1,
                "tag_count": 1,
                "total_bytes": 0,
                "endpoint_refs": 0,
                "job_refs": 0,
            },
        ),
        patch.object(mgr, "_run_cdk") as mock_cdk,
        patch("cli.stacks.sys.stdin") as mock_stdin,
        patch("cli.stacks.confirm", return_value=False),
    ):
        mock_stdin.isatty.return_value = True
        # ``force=False`` makes the prompt path active.
        result = mgr.destroy(stack_name="gco-global", force=False)

    assert result is False
    mock_cdk.assert_not_called()
    out = capsys.readouterr().out
    assert "Aborted." in out


def test_destroy_tty_prompt_accept(manager_factory, cdk_json_factory):
    """A TTY operator answering ``y`` proceeds with the destroy."""
    cdk_json_factory({"removal_policy": "destroy", "empty_on_delete": True})
    mgr = manager_factory()

    with (
        patch.object(
            mgr,
            "_build_image_registry_inventory",
            return_value={
                "repo_count": 0,
                "tag_count": 0,
                "total_bytes": 0,
                "endpoint_refs": 0,
                "job_refs": 0,
            },
        ),
        patch.object(mgr, "_run_cdk") as mock_cdk,
        patch.object(mgr, "_stack_exists_in_cloudformation", return_value=False),
        patch("cli.stacks.sys.stdin") as mock_stdin,
        patch("cli.stacks.confirm", return_value=True),
    ):
        mock_stdin.isatty.return_value = True
        mock_cdk.return_value = MagicMock(returncode=0)
        result = mgr.destroy(stack_name="gco-global", force=False)
    assert result is True
