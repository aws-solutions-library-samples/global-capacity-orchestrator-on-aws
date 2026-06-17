"""
Tests for the container image registry MCP tools (mcp/tools/images.py).

Covers four behaviours:

* Default-env registration — only the read-only and administrative
  ``images_*`` tools are registered when no flags are set.
* Image-publish gating — ``images_build`` and ``images_push`` register
  only when ``GCO_ENABLE_IMAGE_PUBLISH=true``.
* Destructive gating — ``images_cleanup`` / ``images_prune`` /
  ``images_delete_tag`` / ``images_delete_repo`` register only when
  ``GCO_ENABLE_DESTRUCTIVE_OPERATIONS=true``.
* Destructive tools emit ``ctx.warning(...)`` so the audit log captures
  a ``client_messages`` entry with ``level: "warning"``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure mcp/ is importable, mirroring the other test modules.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

import run_mcp  # noqa: E402


def _list_tool_names() -> set[str]:
    """Snapshot every registered tool name from the live mcp instance."""
    tools = asyncio.run(run_mcp.mcp._list_tools())
    return {t.name for t in tools}


# Every gated image tool that this file exercises. Reused as the cleanup
# target for ``_clean_gated_image_tools`` so default-env tests see an
# unpolluted registry no matter what order the suite runs in — earlier
# files (e.g. ``test_mcp_destructive_gating.py``) might set the
# destructive flag and leak ``images_*`` tool registrations into the
# module-level ``mcp`` singleton.
_GATED_IMAGE_TOOLS = (
    "images_build",
    "images_push",
    "images_mirror",
    "images_cleanup",
    "images_prune",
    "images_delete_tag",
    "images_delete_repo",
)


def _force_unregister_gated_image_tools() -> None:
    for name in _GATED_IMAGE_TOOLS:
        with contextlib.suppress(Exception):
            run_mcp.mcp.local_provider.remove_tool(name)


# =============================================================================
# Default-env registration: read-only and administrative tools are present;
# gated build/push and destructive variants are NOT.
# =============================================================================


class TestImageToolsDefaultEnv:
    """Under default env, only the unconditional images_* tools register."""

    @pytest.fixture(autouse=True)
    def _clean_gated_image_tools(self):
        _force_unregister_gated_image_tools()
        importlib.reload(run_mcp)
        _force_unregister_gated_image_tools()

    def test_unconditional_tools_present(self):
        names = _list_tool_names()
        # Read-only "safe" tools.
        for n in (
            "images_list",
            "images_tags",
            "images_describe",
            "images_uri",
            "images_replication_get",
            "images_replication_status",
            "images_orphans",
            # Image-mirror read-only tools (plan + status) are default-on too.
            "images_mirror_plan",
            "images_mirror_status",
        ):
            assert n in names, f"expected unconditional tool {n!r} to be registered"
        # Administrative "low-risk" tools.
        for n in (
            "images_init",
            "images_lifecycle_get",
            "images_lifecycle_set",
            "images_replication_sync",
        ):
            assert n in names, f"expected unconditional tool {n!r} to be registered"

    def test_images_build_absent_by_default(self):
        names = _list_tool_names()
        # Co-located coverage of the publish-gated pair plus images_mirror,
        # which shares the GCO_ENABLE_IMAGE_PUBLISH gate.
        assert "images_build" not in names
        assert "images_push" not in names
        assert "images_mirror" not in names

    def test_images_delete_repo_absent_by_default(self):
        names = _list_tool_names()
        # Co-located coverage of every destructive image tool.
        assert "images_delete_repo" not in names
        assert "images_delete_tag" not in names
        assert "images_cleanup" not in names
        assert "images_prune" not in names


# =============================================================================
# Gated registration — image-publish flag exposes images_build / images_push
# =============================================================================


class TestImagePublishGating:
    """Build/push tools register only under ``GCO_ENABLE_IMAGE_PUBLISH``."""

    @patch.dict(os.environ, {"GCO_ENABLE_IMAGE_PUBLISH": "true"})
    def test_images_build_present_when_image_publish_flag_set(self):
        # Reload run_mcp so the gated registrations and re-exports both run.
        importlib.reload(run_mcp)
        names = _list_tool_names()
        assert "images_build" in names
        assert "images_push" in names
        # images_mirror shares the GCO_ENABLE_IMAGE_PUBLISH gate.
        assert "images_mirror" in names
        # The reload block also rebinds the module-level names so callers
        # (and audit-log tests) can reach them through ``run_mcp.``.
        assert hasattr(run_mcp, "images_build")
        assert hasattr(run_mcp, "images_push")
        assert hasattr(run_mcp, "images_mirror")

    @patch.dict(os.environ, {"GCO_ENABLE_IMAGE_PUBLISH": "true"})
    def test_images_build_task_mode_is_optional(self):
        """The publish-gated tools opt in to the FastMCP task protocol.

        The tool's ``task_config.mode`` should be ``"optional"`` so MCP
        clients can choose between synchronous and background-task
        execution. If the running fastmcp version doesn't expose
        ``task_config`` on its registered Tool objects, the test skips
        gracefully — TaskConfig is best-effort wired in the tool module.
        """
        importlib.reload(run_mcp)
        tools = asyncio.run(run_mcp.mcp._list_tools())
        build = next((t for t in tools if t.name == "images_build"), None)
        assert build is not None, "images_build must register under the flag"
        cfg = getattr(build, "task_config", None)
        if cfg is None:
            pytest.skip("fastmcp build doesn't expose task_config on registered tools")
        assert getattr(cfg, "mode", None) == "optional"


# =============================================================================
# Gated registration — destructive flag exposes the four destructive tools
# =============================================================================


class TestImageDestructiveGating:
    """Cleanup/prune/delete tools register only under destructive flag."""

    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    def test_images_delete_repo_present_when_destructive_flag_set(self):
        importlib.reload(run_mcp)
        names = _list_tool_names()
        for n in (
            "images_cleanup",
            "images_prune",
            "images_delete_tag",
            "images_delete_repo",
        ):
            assert n in names, f"expected destructive tool {n!r} to be registered"
        # Module-level rebinds also work.
        assert hasattr(run_mcp, "images_delete_tag")
        assert hasattr(run_mcp, "images_delete_repo")
        assert hasattr(run_mcp, "images_cleanup")
        assert hasattr(run_mcp, "images_prune")


# =============================================================================
# ctx.warning capture — destructive tools should record a client_messages
# entry with level="warning" via the audit middleware spy.
# =============================================================================


def _audit_invocation_entries(caplog) -> list[dict]:
    """Return every ``mcp.tool.invocation`` entry in caplog."""
    out: list[dict] = []
    for record in caplog.records:
        if record.name != "gco.mcp.audit":
            continue
        try:
            entry = json.loads(record.message)
        except json.JSONDecodeError:
            continue
        if entry.get("event") == "mcp.tool.invocation":
            out.append(entry)
    return out


class TestImageDestructiveCtxWarning:
    """Destructive tools emit ``ctx.warning`` so the audit entry has client_messages."""

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    async def test_images_destructive_tools_emit_ctx_warning(self, caplog):
        # Pull in the gated tools fresh so the FastMCP middleware sees the
        # registered tool when the Client routes the call.
        importlib.reload(run_mcp)

        # Stub ImageManager.delete_tag so the call doesn't reach boto3.
        fake_manager = MagicMock()
        fake_manager.delete_tag.return_value = {
            "name": "gco/my-app",
            "tag": "old",
            "deleted": [{"digest": "sha256:abc", "tag": "old"}],
            "failures": [],
        }

        from fastmcp import Client

        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch("cli.images.get_image_manager", return_value=fake_manager),
        ):
            async with Client(run_mcp.mcp) as client:
                result = await client.call_tool(
                    "images_delete_tag", {"name": "my-app", "tag": "old"}
                )

        # The tool wraps the manager's return shape in a JSON string.
        assert result.content, "expected content from images_delete_tag"
        text_payload = result.content[0].text
        assert "sha256:abc" in text_payload

        invocations = _audit_invocation_entries(caplog)
        delete_entries = [e for e in invocations if e.get("tool") == "images_delete_tag"]
        assert delete_entries, "expected an audit entry for images_delete_tag"
        entry = delete_entries[-1]
        assert entry["status"] == "success"
        msgs = entry.get("client_messages") or []
        warnings = [m for m in msgs if m.get("level") == "warning"]
        assert warnings, f"expected a warning in client_messages, got {msgs!r}"
        # The warning text mentions the destructive intent so operators see why
        # the tool flagged the call.
        assert any("cannot be undone" in m.get("message", "") for m in warnings)


# =============================================================================
# Image-mirror tools — read-only plan/status (default-on) and the gated
# execute tool, each wrapping the cli._image_mirror core (not ImageManager).
# =============================================================================


class TestImageMirrorReadOnlyTools:
    """images_mirror_plan / images_mirror_status wrap the mirror core; no writes."""

    @pytest.mark.asyncio
    async def test_images_mirror_plan_returns_plan_with_enabled_flag(self):
        from fastmcp import Client

        fake_plan = {
            "region": "us-east-1",
            "registry": "123456789012.dkr.ecr.us-east-1.amazonaws.com/gco/dockerhub",
            "ecr_namespace": "gco/dockerhub",
            "images": [
                {
                    "source_ref": "docker.io/volcanosh/vc-scheduler:v1.15.0",
                    "dest_repo": "gco/dockerhub/volcanosh/vc-scheduler",
                    "dest_ref": (
                        "123456789012.dkr.ecr.us-east-1.amazonaws.com"
                        "/gco/dockerhub/volcanosh/vc-scheduler:v1.15.0"
                    ),
                    "tag": "v1.15.0",
                }
            ],
        }
        with (
            patch("cli._image_mirror.plan_mirror", return_value=dict(fake_plan)) as mock_plan,
            patch(
                "cli._image_mirror.read_mirror_config",
                return_value={"enabled": True, "ecr_namespace": "gco/dockerhub"},
            ),
        ):
            async with Client(run_mcp.mcp) as client:
                result = await client.call_tool("images_mirror_plan", {"region": "us-east-1"})

        payload = json.loads(result.content[0].text)
        # The tool merges the config-level ``enabled`` flag into the plan output.
        assert payload["enabled"] is True
        assert payload["images"][0]["dest_repo"] == "gco/dockerhub/volcanosh/vc-scheduler"
        # Region forwarded; ecr_namespace defaulted (None passed through to the core).
        mock_plan.assert_called_once_with("us-east-1", None)

    @pytest.mark.asyncio
    async def test_images_mirror_plan_forwards_namespace(self):
        from fastmcp import Client

        with (
            patch("cli._image_mirror.plan_mirror", return_value={"images": []}) as mock_plan,
            patch(
                "cli._image_mirror.read_mirror_config",
                return_value={"enabled": False, "ecr_namespace": "gco/custom"},
            ),
        ):
            async with Client(run_mcp.mcp) as client:
                await client.call_tool(
                    "images_mirror_plan",
                    {"region": "eu-west-1", "ecr_namespace": "gco/custom"},
                )
        mock_plan.assert_called_once_with("eu-west-1", "gco/custom")

    @pytest.mark.asyncio
    async def test_images_mirror_status_reports_presence(self):
        from fastmcp import Client

        fake_status = {
            "region": "us-east-1",
            "registry": "123456789012.dkr.ecr.us-east-1.amazonaws.com/gco/dockerhub",
            "ecr_namespace": "gco/dockerhub",
            "images": [
                {
                    "dest_ref": (
                        "123456789012.dkr.ecr.us-east-1.amazonaws.com"
                        "/gco/dockerhub/volcanosh/vc-scheduler:v1.15.0"
                    ),
                    "dest_repo": "gco/dockerhub/volcanosh/vc-scheduler",
                    "tag": "v1.15.0",
                    "mirrored": True,
                }
            ],
            "all_mirrored": True,
            "missing": [],
        }
        with patch("cli._image_mirror.mirror_status", return_value=fake_status) as mock_status:
            async with Client(run_mcp.mcp) as client:
                result = await client.call_tool("images_mirror_status", {"region": "us-east-1"})
        payload = json.loads(result.content[0].text)
        assert payload["all_mirrored"] is True
        assert payload["missing"] == []
        mock_status.assert_called_once_with("us-east-1", None)


class TestImageMirrorExecuteGating:
    """images_mirror registers only under GCO_ENABLE_IMAGE_PUBLISH and runs the core."""

    @patch.dict(os.environ, {"GCO_ENABLE_IMAGE_PUBLISH": "true"})
    def test_images_mirror_present_when_flag_set(self):
        importlib.reload(run_mcp)
        names = _list_tool_names()
        assert "images_mirror" in names
        assert hasattr(run_mcp, "images_mirror")

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_IMAGE_PUBLISH": "true"})
    async def test_images_mirror_invokes_core_and_captures_log(self):
        importlib.reload(run_mcp)
        from fastmcp import Client

        def _fake_mirror_images(region, ecr_namespace=None, skip_existing=True, log=print):
            log(f"copying into {region}")
            return {
                "registry": "123456789012.dkr.ecr.us-east-1.amazonaws.com/gco/dockerhub",
                "strategy": "buildx",
                "mirrored": [
                    "123456789012.dkr.ecr.us-east-1.amazonaws.com"
                    "/gco/dockerhub/volcanosh/vc-scheduler:v1.15.0"
                ],
                "skipped": [],
            }

        with patch("cli._image_mirror.mirror_images", side_effect=_fake_mirror_images) as mock_mi:
            async with Client(run_mcp.mcp) as client:
                result = await client.call_tool("images_mirror", {"region": "us-east-1"})

        payload = json.loads(result.content[0].text)
        assert payload["strategy"] == "buildx"
        # Log lines streamed by the core are captured into the result.
        assert payload["log"] == ["copying into us-east-1"]
        # kwargs forwarded to the core; ``log`` is the in-tool line collector.
        _, kwargs = mock_mi.call_args
        assert kwargs["skip_existing"] is True
        assert kwargs["ecr_namespace"] is None
