"""
MCP tool modules — each file registers tools against the shared ``mcp`` server.

Import ``register_all_tools()`` to register every tool group at once.
"""


def register_all_tools() -> None:
    """Import all tool modules so their @mcp.tool() decorators fire."""
    # Intentionally unused — each submodule registers @mcp.tool() handlers
    # at import time. The noqa silences F401.
    from tools import (  # noqa: F401
        analytics,
        capacity,
        config,
        costs,
        dag,
        docs,
        examples,
        images,
        inference,
        jobs,
        metrics,
        mission,
        models,
        monitoring,
        nodepools,
        queue,
        semantic_progress,
        stacks,
        storage,
        tasks,
        templates,
        webhooks,
    )
