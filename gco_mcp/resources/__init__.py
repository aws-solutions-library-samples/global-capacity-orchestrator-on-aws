"""
MCP resource modules — each file registers resources against the shared ``mcp`` server.

Import ``register_all_resources()`` to register every resource group at once.
"""


def register_all_resources() -> None:
    """Import all resource modules so their @mcp.resource() decorators fire."""
    # These imports are intentionally unused — we pull them in for their
    # side effects (each module registers @mcp.resource() handlers at
    # import time). The noqa silences F401.
    # The live-state modules expose a ``register()`` for their template
    # handlers. The static manifest paths in ``k8s.py`` are decorated at
    # import time; ``k8s.register()`` only wires the live ``gco://k8s/...``
    # template.
    from server import mcp as _mcp

    from resources import (  # noqa: F401
        ci,
        clients,
        config,
        demos,
        docs,
        iam_policies,
        images,
        infra,
        k8s,
        scripts,
        source,
        tests,
    )
    from resources import (
        cluster as _cluster,
    )
    from resources import (
        costs as _costs,
    )
    from resources import (
        inference as _inference,
    )
    from resources import (
        jobs as _jobs,
    )
    from resources import (
        mission as _mission,
    )
    from resources import (
        self as _self_introspection,
    )
    from resources import (
        tasks as _tasks,
    )

    _jobs.register(_mcp)
    _inference.register(_mcp)
    k8s.register(_mcp)
    _cluster.register(_mcp)
    _costs.register(_mcp)
    _tasks.register(_mcp)
    _mission.register(_mcp)
    _self_introspection.register(_mcp)
