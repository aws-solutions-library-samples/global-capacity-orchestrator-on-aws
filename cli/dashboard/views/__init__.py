"""One module per dashboard page; each exposes ``render()``.

The modules are imported here so that importing this package proves the whole
page graph imports (the dashboard image's build-time smoke test does exactly
that).
"""

from cli.dashboard.views import (
    capacity,
    console,
    costs,
    inference,
    jobs,
    overview,
    platform,
    queue,
)

__all__ = [
    "capacity",
    "console",
    "costs",
    "inference",
    "jobs",
    "overview",
    "platform",
    "queue",
]
