"""
Tests for the container image registry resources (gco_mcp/resources/images.py).

Covers the four resource paths:

* ``images://gco/index`` — list of every gco/* repo with summary metadata.
* ``images://gco/{name}/tags`` — per-repo tag list.
* ``images://gco/{name}/{tag}`` — full ECR describe for a single tag.
* ``images://gco/replication/status`` — registry-wide replication state.

Each test mocks ``cli.images.get_image_manager`` so the resources never
reach real AWS credentials. Mirrors the read_resource pattern used by
``tests/test_mcp_server.py::TestDocResources``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure gco_mcp/ is importable, mirroring the other test modules.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # noqa: E402


def _read_resource(uri: str) -> str:
    """Synchronous helper that returns the text content of a resource read."""
    result = asyncio.run(run_mcp.mcp.read_resource(uri))
    return result.contents[0].content


def _fake_repos() -> list[dict]:
    return [
        {
            "name": "gco/my-app",
            "arn": "arn:aws:ecr:us-east-1:123456789012:repository/gco/my-app",
            "uri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/gco/my-app",
            "created_at": "2025-01-01T00:00:00Z",
            "image_count": 3,
            "tag_mutability": "MUTABLE",
        },
        {
            "name": "gco/svc",
            "arn": "arn:aws:ecr:us-east-1:123456789012:repository/gco/svc",
            "uri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/gco/svc",
            "created_at": "2025-02-01T00:00:00Z",
            "image_count": 0,
            "tag_mutability": "IMMUTABLE",
        },
    ]


class TestImagesIndexResource:
    """images://gco/index — list every gco/* repository."""

    def test_index_lists_repositories(self):
        manager = MagicMock()
        manager.list_repos.return_value = _fake_repos()
        with patch("cli.images.get_image_manager", return_value=manager):
            content = _read_resource("images://gco/index")
        assert "Image Registry" in content
        assert "`gco/my-app`" in content
        assert "`gco/svc`" in content
        # Each repo shows up in the per-repo resources section.
        assert "images://gco/my-app/tags" in content
        assert "images://gco/svc/tags" in content
        # Replication shortcut is advertised too.
        assert "images://gco/replication/status" in content

    def test_index_handles_empty_registry(self):
        manager = MagicMock()
        manager.list_repos.return_value = []
        with patch("cli.images.get_image_manager", return_value=manager):
            content = _read_resource("images://gco/index")
        # Friendly empty-state copy that points operators at `gco images init`.
        assert "No repositories found" in content
        assert "gco images init" in content


class TestImagesTagsResource:
    """images://gco/{name}/tags — list every tag on a repo."""

    def test_tags_lists_each_tag_with_metadata(self):
        manager = MagicMock()
        manager.list_tags.return_value = [
            {
                "tag": "v1",
                "digest": "sha256:aaa",
                "pushed_at": "2025-01-01T00:00:00Z",
                "size_bytes": 1024,
            },
            {
                "tag": "latest",
                "digest": "sha256:bbb",
                "pushed_at": "2025-02-01T00:00:00Z",
                "size_bytes": 2048,
            },
        ]
        with patch("cli.images.get_image_manager", return_value=manager):
            content = _read_resource("images://gco/my-app/tags")
        manager.list_tags.assert_called_once_with("my-app")
        assert "Tags for `gco/my-app`" in content
        assert "`v1`" in content
        assert "`latest`" in content
        assert "sha256:aaa" in content
        # Per-tag resources appear too.
        assert "images://gco/my-app/v1" in content
        assert "images://gco/my-app/latest" in content

    def test_tags_empty_repo_renders_friendly_message(self):
        manager = MagicMock()
        manager.list_tags.return_value = []
        with patch("cli.images.get_image_manager", return_value=manager):
            content = _read_resource("images://gco/empty/tags")
        assert "No tags found" in content


class TestImagesDescribeResource:
    """images://gco/{name}/{tag} — full ECR describe for a single tag."""

    def test_describe_returns_json_payload(self):
        manager = MagicMock()
        manager.describe.return_value = {
            "name": "gco/my-app",
            "tag": "v1",
            "digest": "sha256:abc",
            "pushed_at": "2025-01-01T00:00:00Z",
            "size_bytes": 1024,
            "tags": ["v1", "latest"],
            "scan_findings_summary": None,
        }
        with patch("cli.images.get_image_manager", return_value=manager):
            content = _read_resource("images://gco/my-app/v1")
        manager.describe.assert_called_once_with("my-app", "v1")
        parsed = json.loads(content)
        assert parsed["name"] == "gco/my-app"
        assert parsed["tag"] == "v1"
        assert parsed["digest"] == "sha256:abc"

    def test_describe_missing_tag_returns_error_payload(self):
        manager = MagicMock()
        manager.describe.return_value = {}
        with patch("cli.images.get_image_manager", return_value=manager):
            content = _read_resource("images://gco/my-app/missing")
        parsed = json.loads(content)
        assert parsed["error"] == "tag not found"
        assert parsed["name"] == "gco/my-app"
        assert parsed["tag"] == "missing"


class TestImagesReplicationStatusResource:
    """images://gco/replication/status — registry-wide replication state."""

    def test_replication_status_lists_per_region_rows(self):
        manager = MagicMock()
        manager.replication_status.return_value = [
            {
                "repository": "gco/my-app",
                "digest": "sha256:abc",
                "region": "us-west-2",
                "status": "COMPLETE",
                "registry_id": "123456789012",
            },
            {
                "repository": "gco/my-app",
                "digest": "sha256:abc",
                "region": "eu-west-1",
                "status": "IN_PROGRESS",
                "registry_id": "123456789012",
            },
        ]
        with patch("cli.images.get_image_manager", return_value=manager):
            content = _read_resource("images://gco/replication/status")
        assert "Replication Status" in content
        assert "`gco/my-app`" in content
        assert "us-west-2" in content
        assert "COMPLETE" in content
        assert "IN_PROGRESS" in content

    def test_replication_status_empty_renders_guidance(self):
        manager = MagicMock()
        manager.replication_status.return_value = []
        with patch("cli.images.get_image_manager", return_value=manager):
            content = _read_resource("images://gco/replication/status")
        assert "No replication entries" in content
        assert "gco images replication get" in content
