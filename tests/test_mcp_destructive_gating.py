"""
Tests for destructive-operations and model-upload feature-flag gating.

Covers:

* ``delete_job`` and ``delete_inference`` are absent under the default
  (clean-env) configuration and present when
  ``GCO_ENABLE_DESTRUCTIVE_OPERATIONS=true``.
* Argv translation for the new destructive tools (``delete_template``,
  ``delete_webhook``, ``delete_model``, ``delete_nodepool``,
  ``analytics_user_remove``, ``cancel_queue_job``).
* ``models_upload`` is absent by default and present under
  ``GCO_ENABLE_MODEL_UPLOAD=true`` with the expected positional argv.
* The umbrella ``GCO_ENABLE_ALL_TOOLS=true`` registers every gated tool in
  one shot, even with every per-tool flag unset.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure gco_mcp/ is importable, mirroring the other test modules.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # noqa: E402

# Names that should appear when ``GCO_ENABLE_ALL_TOOLS=true`` is the only
# flag set. Sourced from the design's enumeration table — every gated tool
# across the per-flag families. Also reused as the cleanup target for the
# ``_clean_gated_tools`` fixture so default-env tests see an unpolluted
# registry no matter what order the suite runs in.
UMBRELLA_FLAG_TOOLS = (
    # Capacity purchase.
    "reserve_capacity",
    "create_reservation",
    # Model/local-data upload.
    "models_upload",
    "upload_to_regional_bucket",
    # Image publish.
    "images_build",
    "images_push",
    "images_mirror",
    # Infrastructure lifecycle.
    "deploy_stack",
    "deploy_all",
    "bootstrap_cdk",
    "destroy_stack",
    "destroy_all",
    # Destructive operations.
    "delete_job",
    "delete_inference",
    "delete_template",
    "delete_webhook",
    "delete_model",
    "delete_nodepool",
    "analytics_user_remove",
    "monitoring_user_remove",
    "cancel_queue_job",
    "cancel_reservation",
    "images_cleanup",
    "images_prune",
    "images_delete_tag",
    "images_delete_repo",
    # Sensitive local/cost readers.
    "metrics_from_local_file",
    "sync_storage_bucket",
    "metrics_semantic_progress",
    # Managed deployment config.
    "list_deployment_regions",
    "add_deployment_region",
    "remove_deployment_region",
    "set_deployment_region",
    "set_default_bedrock_model",
    "set_claude_code_default_model",
    # Mission family.
    "mission_start",
    "mission_status",
    "mission_iterate",
    "mission_checkpoint",
    "mission_complete",
    "mission_abort",
    "mission_resume",
    "mission_history",
    "mission_list",
)


def _force_unregister_gated_tools() -> None:
    """Strip every gated tool from the live ``mcp`` singleton.

    The ``FastMCP`` instance is module-level in ``gco_mcp/server.py`` and
    survives ``importlib.reload(run_mcp)``. Once a flag-set test registers
    a gated tool, that registration leaks into every subsequent test —
    including default-env "absent by default" assertions in this file and
    in ``tests/test_mcp_images.py``. ``remove_tool`` is FastMCP's official
    way to drop a name; calling it here resets the registry to its clean
    default before each default-env test runs.
    """
    for name in UMBRELLA_FLAG_TOOLS:
        with contextlib.suppress(Exception):
            # ``remove_tool`` raises when the name isn't registered — that's
            # fine, we wanted the post-state regardless.
            run_mcp.mcp.local_provider.remove_tool(name)


def _list_tool_names() -> set[str]:
    """Snapshot every registered tool name from the live mcp instance."""
    tools = asyncio.run(run_mcp.mcp._list_tools())
    return {t.name for t in tools}


# =============================================================================
# Default-env: destructive tools absent
# =============================================================================


class TestDestructiveDefaultEnv:
    """Under default env none of the destructive tools register."""

    @pytest.fixture(autouse=True)
    def _clean_gated_tools(self):
        # The mcp singleton retains every tool another test registered.
        # Strip the gated names before each default-env assertion so the
        # snapshot reflects "default behaviour", not "leaked state".
        _force_unregister_gated_tools()
        importlib.reload(run_mcp)
        # Re-strip after the reload because run_mcp's own reload blocks
        # may re-register tools when env vars from a previous test were
        # patched but never cleared (e.g. via patch.dict at module scope).
        _force_unregister_gated_tools()

    def test_delete_job_absent_by_default(self):
        names = _list_tool_names()
        assert "delete_job" not in names

    def test_delete_inference_absent_by_default(self):
        names = _list_tool_names()
        assert "delete_inference" not in names

    def test_new_destructive_tools_absent_by_default(self):
        names = _list_tool_names()
        for n in (
            "delete_template",
            "delete_webhook",
            "delete_model",
            "delete_nodepool",
            "analytics_user_remove",
            "cancel_queue_job",
        ):
            assert n not in names, f"expected destructive tool {n!r} to be absent by default"

    def test_models_upload_absent_by_default(self):
        names = _list_tool_names()
        assert "models_upload" not in names
        assert "upload_to_regional_bucket" not in names


# =============================================================================
# Destructive-flag gating: delete_job / delete_inference register under flag
# =============================================================================


class TestDestructiveFlagGating:
    """Setting GCO_ENABLE_DESTRUCTIVE_OPERATIONS registers the gated tools."""

    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    def test_delete_job_present_when_destructive_flag_set(self):
        importlib.reload(run_mcp)
        names = _list_tool_names()
        assert "delete_job" in names
        assert hasattr(run_mcp, "delete_job")

    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    def test_delete_inference_present_when_destructive_flag_set(self):
        importlib.reload(run_mcp)
        names = _list_tool_names()
        assert "delete_inference" in names
        assert hasattr(run_mcp, "delete_inference")

    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    def test_new_destructive_tools_present_when_destructive_flag_set(self):
        importlib.reload(run_mcp)
        names = _list_tool_names()
        for n in (
            "delete_template",
            "delete_webhook",
            "delete_model",
            "delete_nodepool",
            "analytics_user_remove",
            "cancel_queue_job",
        ):
            assert n in names, f"expected destructive tool {n!r} to register under the flag"
            assert hasattr(run_mcp, n)


# =============================================================================
# Argv translation for the new destructive tools.
#
# Every test mocks ``cli_runner.subprocess.run`` so the call doesn't reach
# the real CLI, then asserts the constructed argv matches the documented
# CLI shape (positional args, ``-y`` / ``--yes`` confirmation flag, and any
# optional flags only when set).
# =============================================================================


class TestDestructiveArgv:
    """Argv translation for the new destructive tools."""

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    async def test_delete_template_minimal(self):
        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.delete_template(name="t1")
            cmd = mock.call_args[0][0]
            assert "templates" in cmd
            assert "delete" in cmd
            assert "t1" in cmd
            assert "-y" in cmd
            # Optional region absent when not supplied.
            assert "-r" not in cmd

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    async def test_delete_template_with_region(self):
        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.delete_template(name="t1", region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "templates" in cmd
            assert "delete" in cmd
            assert "t1" in cmd
            assert "-y" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    async def test_delete_webhook_minimal(self):
        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.delete_webhook(name="hook1")
            cmd = mock.call_args[0][0]
            assert "webhooks" in cmd
            assert "delete" in cmd
            assert "hook1" in cmd
            assert "-y" in cmd
            assert "-r" not in cmd

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    async def test_delete_webhook_with_region(self):
        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.delete_webhook(name="hook1", region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "webhooks" in cmd
            assert "delete" in cmd
            assert "hook1" in cmd
            assert "-y" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    async def test_delete_model(self):
        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.delete_model(model_name="llama3-8b")
            cmd = mock.call_args[0][0]
            assert "models" in cmd
            assert "delete" in cmd
            assert "llama3-8b" in cmd
            assert "-y" in cmd

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    async def test_delete_nodepool_minimal(self):
        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.delete_nodepool(nodepool_name="np1", region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "nodepools" in cmd
            assert "delete" in cmd
            assert "np1" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]
            assert "-y" in cmd
            assert "--cluster" not in cmd

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    async def test_delete_nodepool_with_cluster(self):
        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.delete_nodepool(
                nodepool_name="np1", region="us-east-1", cluster="my-cluster"
            )
            cmd = mock.call_args[0][0]
            assert "nodepools" in cmd
            assert "delete" in cmd
            assert "np1" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]
            assert cmd[cmd.index("--cluster") : cmd.index("--cluster") + 2] == [
                "--cluster",
                "my-cluster",
            ]
            assert "-y" in cmd

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    async def test_analytics_user_remove(self):
        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.analytics_user_remove(username="alice")
            cmd = mock.call_args[0][0]
            assert "analytics" in cmd
            assert "users" in cmd
            assert "remove" in cmd
            # Username goes through the named flag, mirroring the CLI surface.
            assert cmd[cmd.index("--username") : cmd.index("--username") + 2] == [
                "--username",
                "alice",
            ]
            assert "--yes" in cmd

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    async def test_cancel_queue_job_minimal(self):
        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.cancel_queue_job(job_id="abc")
            cmd = mock.call_args[0][0]
            assert "queue" in cmd
            assert "cancel" in cmd
            assert "abc" in cmd
            assert "-y" in cmd
            assert "-r" not in cmd

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    async def test_cancel_queue_job_with_region(self):
        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.cancel_queue_job(job_id="abc", region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "queue" in cmd
            assert "cancel" in cmd
            assert "abc" in cmd
            assert "-y" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]


# =============================================================================
# Model-upload gating + argv
# =============================================================================


class TestModelUploadGating:
    """Both local-data upload tools register only under GCO_ENABLE_MODEL_UPLOAD."""

    @patch.dict(os.environ, {"GCO_ENABLE_MODEL_UPLOAD": "true"})
    def test_upload_tools_present_when_model_upload_flag_set(self):
        importlib.reload(run_mcp)
        names = _list_tool_names()
        assert {"models_upload", "upload_to_regional_bucket"}.issubset(names)
        assert hasattr(run_mcp, "models_upload")
        assert hasattr(run_mcp, "upload_to_regional_bucket")

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_MODEL_UPLOAD": "true"})
    async def test_models_upload_argv_minimal(self, tmp_path: Path):
        root = tmp_path / "local-root"
        source = root / "weights"
        source.mkdir(parents=True)
        with patch.dict(os.environ, {"GCO_STORAGE_LOCAL_ROOT": str(root)}):
            importlib.reload(run_mcp)
            with patch("cli_runner.subprocess.run") as mock:
                mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
                await run_mcp.models_upload(model_name="llama3-8b", source_path="./weights")
                cmd = mock.call_args.args[0]
                assert "models" in cmd
                assert "upload" in cmd
                # The CLI receives a descriptor-backed private snapshot rather
                # than a raceable absolute source path.
                upload_argument = cmd[cmd.index("upload") + 1]
                assert upload_argument.startswith("/dev/fd/")
                assert mock.call_args.kwargs["pass_fds"]
                assert cmd[cmd.index("--name") : cmd.index("--name") + 2] == [
                    "--name",
                    "llama3-8b",
                ]
                assert "-r" not in cmd
            assert not list(root.glob(".gco-mcp-upload-*"))


# =============================================================================
# Umbrella flag — GCO_ENABLE_ALL_TOOLS turns every gate on
# =============================================================================


class TestUmbrellaFlag:
    """``GCO_ENABLE_ALL_TOOLS=true`` registers every gated tool."""

    def test_all_gated_tools_register_under_umbrella_flag(self):
        # Clear every per-tool flag and set only the umbrella. Every gated
        # tool should appear in the registry after the reload.
        clean = {
            "GCO_ENABLE_ALL_TOOLS": "true",
            "GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "",
            "GCO_ENABLE_MODEL_UPLOAD": "",
            "GCO_ENABLE_IMAGE_PUBLISH": "",
            "GCO_ENABLE_INFRASTRUCTURE_DEPLOY": "",
            "GCO_ENABLE_INFRASTRUCTURE_DESTROY": "",
            "GCO_ENABLE_CAPACITY_PURCHASE": "",
            "GCO_ENABLE_MISSION": "",
            "GCO_ENABLE_LOCAL_METRICS": "",
            "GCO_ENABLE_LOCAL_STORAGE_SYNC": "",
            "GCO_ENABLE_SEMANTIC_PROGRESS": "",
            "GCO_ENABLE_CONFIG_MANAGEMENT": "",
        }
        with patch.dict(os.environ, clean):
            importlib.reload(run_mcp)
            names = _list_tool_names()
        missing = [n for n in UMBRELLA_FLAG_TOOLS if n not in names]
        assert not missing, f"umbrella flag did not register: {missing!r}"
