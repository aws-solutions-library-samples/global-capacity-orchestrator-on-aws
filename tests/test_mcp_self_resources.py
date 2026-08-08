"""Tests for the self-indexing MCP resources (``mcp://gco/...``).

Four templates surface the live FastMCP catalog through resource URIs:

* ``mcp://gco/tools/index``
* ``mcp://gco/tools/{tool_name}``
* ``mcp://gco/resources/index``
* ``mcp://gco/feature-flags``

These tests round-trip each URI through the FastMCP in-process Client
(matching the pattern in ``test_mission_mcp_tools.py``) and assert the
JSON shape. The resources are always-on so no feature-flag setup is
needed beyond what the server's startup wiring already does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure gco_mcp/ is importable, mirroring every other test module.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # noqa: E402, I001 — sys.path tweak above must run first


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_resource_text(uri: str) -> str:
    """Read ``uri`` through the in-process FastMCP client, returning the body."""
    import asyncio

    from fastmcp import Client

    async def _run() -> str:
        async with Client(run_mcp.mcp) as client:
            blocks = await client.read_resource(uri)
            assert blocks, f"empty content for {uri}"
            return blocks[0].text

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestToolsIndex:
    """``mcp://gco/tools/index`` enumerates registered tools."""

    def test_tools_index_includes_known_tool(self):
        """``find_examples`` is always-on; assert it appears in the index."""
        body = _read_resource_text("mcp://gco/tools/index")
        payload = json.loads(body)
        names = {entry["name"] for entry in payload["tools"]}
        assert "find_examples" in names, f"find_examples missing; saw: {sorted(names)[:10]}..."

    def test_tools_index_entries_have_documented_shape(self):
        """Every entry carries name, description, tags, source_path, source_line, gating_flag."""
        body = _read_resource_text("mcp://gco/tools/index")
        payload = json.loads(body)
        for entry in payload["tools"]:
            assert "name" in entry
            assert "description" in entry
            assert "tags" in entry
            # source_path may be None for dynamically-generated tools;
            # the shape contract just requires the key.
            assert "source_path" in entry
            assert "source_line" in entry
            assert "gating_flag" in entry


class TestToolDetail:
    """``mcp://gco/tools/{tool_name}`` returns single-tool detail."""

    def test_tools_detail_returns_specific_tool(self):
        """``find_examples`` detail matches the index entry's shape."""
        body = _read_resource_text("mcp://gco/tools/find_examples")
        entry = json.loads(body)
        assert entry["name"] == "find_examples"
        # tags is a list of strings (sorted by the handler)
        assert isinstance(entry["tags"], list)
        # find_examples is always-on, so gating_flag is null
        assert entry["gating_flag"] is None

    def test_tools_detail_404_for_unknown(self):
        """An unknown tool name surfaces a not-found error."""
        # ResourceError / FastMCP wraps as a generic exception; assert
        # only that read failed rather than coupling to the concrete class.
        with pytest.raises(Exception):  # noqa: B017 — generic upstream error type
            _read_resource_text("mcp://gco/tools/this_tool_does_not_exist_anywhere")


class TestResourcesIndex:
    """``mcp://gco/resources/index`` enumerates static resources + templates."""

    def test_resources_index_has_documented_shape(self):
        """Index payload has resources and templates lists."""
        body = _read_resource_text("mcp://gco/resources/index")
        payload = json.loads(body)
        assert "resources" in payload
        assert "templates" in payload
        assert isinstance(payload["resources"], list)
        assert isinstance(payload["templates"], list)

    def test_resources_index_includes_source_template(self):
        """The ``source://gco/file/{filepath*}`` template is always-on; assert it appears."""
        body = _read_resource_text("mcp://gco/resources/index")
        payload = json.loads(body)
        uris = {t["uri_template"] for t in payload["templates"]}
        # source_file_resource template — present in every catalog snapshot.
        assert any("source://gco/file" in uri or "source://gco/config" in uri for uri in uris), (
            f"source:// templates missing; saw: {sorted(uris)[:5]}"
        )


class TestFeatureFlags:
    """``mcp://gco/feature-flags`` exposes the gating table."""

    def test_feature_flags_lists_mission(self):
        """``GCO_ENABLE_MISSION`` appears with its nine gated tools."""
        body = _read_resource_text("mcp://gco/feature-flags")
        payload = json.loads(body)
        flags = {f["name"]: f for f in payload["flags"]}
        assert "GCO_ENABLE_MISSION" in flags
        mission = flags["GCO_ENABLE_MISSION"]
        assert mission["default"] is False
        assert "mission_start" in mission["gated_tools"]
        assert "mission_iterate" in mission["gated_tools"]
        assert len(mission["gated_tools"]) == 9

    def test_feature_flags_includes_umbrella(self):
        """``GCO_ENABLE_ALL_TOOLS`` appears with empty gated_tools (it's the umbrella)."""
        body = _read_resource_text("mcp://gco/feature-flags")
        payload = json.loads(body)
        flags = {f["name"]: f for f in payload["flags"]}
        assert "GCO_ENABLE_ALL_TOOLS" in flags
        assert flags["GCO_ENABLE_ALL_TOOLS"]["gated_tools"] == []

    def test_feature_flags_has_capacity_purchase(self):
        """Capacity-purchase flag is registered with reserve_capacity gated."""
        body = _read_resource_text("mcp://gco/feature-flags")
        payload = json.loads(body)
        flags = {f["name"]: f for f in payload["flags"]}
        assert "GCO_ENABLE_CAPACITY_PURCHASE" in flags
        assert "reserve_capacity" in flags["GCO_ENABLE_CAPACITY_PURCHASE"]["gated_tools"]

    def test_feature_flag_map_covers_every_gated_tool(self):
        body = _read_resource_text("mcp://gco/feature-flags")
        payload = json.loads(body)
        actual = {
            item["name"]: set(item["gated_tools"])
            for item in payload["flags"]
            if item["name"] != "GCO_ENABLE_ALL_TOOLS"
        }
        expected = {
            "GCO_ENABLE_CAPACITY_PURCHASE": {"reserve_capacity", "create_reservation"},
            "GCO_ENABLE_MODEL_UPLOAD": {"models_upload", "upload_to_regional_bucket"},
            "GCO_ENABLE_IMAGE_PUBLISH": {"images_build", "images_push", "images_mirror"},
            "GCO_ENABLE_INFRASTRUCTURE_DEPLOY": {
                "deploy_stack",
                "deploy_all",
                "bootstrap_cdk",
                "addons_install",
            },
            "GCO_ENABLE_INFRASTRUCTURE_DESTROY": {"destroy_stack", "destroy_all"},
            "GCO_ENABLE_DESTRUCTIVE_OPERATIONS": {
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
                "task_prune",
            },
            "GCO_ENABLE_MISSION": {
                "mission_start",
                "mission_status",
                "mission_iterate",
                "mission_checkpoint",
                "mission_complete",
                "mission_abort",
                "mission_resume",
                "mission_history",
                "mission_list",
            },
            "GCO_ENABLE_LOCAL_METRICS": {"metrics_from_local_file"},
            "GCO_ENABLE_LOCAL_STORAGE_SYNC": {"sync_storage_bucket"},
            "GCO_ENABLE_SEMANTIC_PROGRESS": {"metrics_semantic_progress"},
            "GCO_ENABLE_CONFIG_MANAGEMENT": {
                "list_deployment_regions",
                "add_deployment_region",
                "remove_deployment_region",
                "set_deployment_region",
                "set_default_bedrock_model",
                "set_claude_code_default_model",
            },
        }
        assert actual == expected
