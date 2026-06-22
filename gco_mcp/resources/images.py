"""Container image registry resources (images:// scheme) for the GCO MCP server.

Each handler delegates to ``cli/images.py::ImageManager`` so the MCP
layer never re-implements ECR semantics. Handlers are sync — FastMCP's
@mcp.resource decorator handles sync resource handlers.
"""

from __future__ import annotations

import json
from typing import Any

from server import mcp


def _get_manager() -> Any:
    """Lazy-import ``cli.images.get_image_manager`` so MCP server import
    doesn't pull boto3 prematurely.
    """
    from cli.images import get_image_manager

    return get_image_manager()


@mcp.resource("images://gco/index")
def images_index() -> str:
    """List every gco/* repository in ECR with summary metadata."""
    try:
        repos = _get_manager().list_repos()
    except Exception as e:  # noqa: BLE001
        return f"# Image Registry\n\nFailed to list repositories: {e}\n"

    lines = ["# Image Registry — `gco/*` repositories\n"]
    if not repos:
        lines.append("No repositories found under the `gco/` prefix.")
        lines.append("")
        lines.append("Run `gco images init <name>` to create the first repo.")
        return "\n".join(lines)

    lines.append("| Repository | Image Count | Tag Mutability | Created |")
    lines.append("| --- | --- | --- | --- |")
    for repo in repos:
        name = repo.get("name", "?")
        count = repo.get("image_count", "?")
        mutability = repo.get("tag_mutability", "?")
        created = repo.get("created_at", "?")
        lines.append(f"| `{name}` | {count} | {mutability} | {created} |")

    lines.append("")
    lines.append("## Per-repo resources")
    for repo in repos:
        name = repo.get("name", "")
        if not name.startswith("gco/"):
            continue
        bare = name.removeprefix("gco/")
        lines.append(f"- `images://gco/{bare}/tags` — list every tag on `{name}`")
    lines.append("")
    lines.append("## Registry-wide resources")
    lines.append("- `images://gco/replication/status` — replication state across regions")
    return "\n".join(lines)


@mcp.resource("images://gco/{name}/tags")
def images_tags_resource(name: str) -> str:
    """List every tag on a single repository.

    Args:
        name: Repository name (without the ``gco/`` prefix).
    """
    try:
        rows = _get_manager().list_tags(name)
    except Exception as e:  # noqa: BLE001
        return f"# Tags for `gco/{name}`\n\nFailed to list tags: {e}\n"

    lines = [f"# Tags for `gco/{name}`\n"]
    if not rows:
        lines.append("No tags found on this repository.")
        return "\n".join(lines)

    lines.append("| Tag | Digest | Pushed | Size (bytes) |")
    lines.append("| --- | --- | --- | --- |")
    for row in rows:
        tag = row.get("tag") or "(untagged)"
        digest = row.get("digest", "?")
        pushed = row.get("pushed_at", "?")
        size = row.get("size_bytes", "?")
        lines.append(f"| `{tag}` | `{digest}` | {pushed} | {size} |")

    lines.append("")
    lines.append("## Per-tag resources")
    seen_tags: set[str] = set()
    for row in rows:
        tag = row.get("tag")
        if not tag or tag in seen_tags:
            continue
        seen_tags.add(tag)
        lines.append(f"- `images://gco/{name}/{tag}` — full describe for `gco/{name}:{tag}`")
    return "\n".join(lines)


@mcp.resource("images://gco/{name}/{tag}")
def images_describe_resource(name: str, tag: str) -> str:
    """Full ECR describe payload for a single tag, as JSON.

    Args:
        name: Repository name (without the ``gco/`` prefix).
        tag: Image tag.
    """
    try:
        result = _get_manager().describe(name, tag)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e), "name": f"gco/{name}", "tag": tag}, indent=2)

    if not result:
        return json.dumps({"error": "tag not found", "name": f"gco/{name}", "tag": tag}, indent=2)
    return json.dumps(result, indent=2, default=str)


@mcp.resource("images://gco/replication/status")
def images_replication_status_resource() -> str:
    """Registry-wide replication state across regions."""
    try:
        rows = _get_manager().replication_status()
    except Exception as e:  # noqa: BLE001
        return f"# Replication Status\n\nFailed to read replication state: {e}\n"

    lines = ["# Replication Status — `gco/*` repositories\n"]
    if not rows:
        lines.append("No replication entries reported. The replication rule may be")
        lines.append("disabled, or no images have replicated yet.")
        lines.append("")
        lines.append("Run `gco images replication get` to inspect the configuration.")
        return "\n".join(lines)

    lines.append("| Repository | Region | Status | Digest | Registry ID |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in rows:
        repo = row.get("repository", "?")
        region = row.get("region", "?")
        status = row.get("status", "?")
        digest = row.get("digest", "?")
        registry_id = row.get("registry_id", "?")
        lines.append(f"| `{repo}` | `{region}` | {status} | `{digest}` | {registry_id} |")
    return "\n".join(lines)
