"""The dashboard's colours and mark, kept in one place.

The orange and the ink are the project logo's (``images/README.md``): deep
orange ``#FF6E42`` on dark backgrounds, ink ``#171B26``. Status colours are
shared by every table badge and chart so "running" is the same blue on the map,
in the jobs table and in the timeline.
"""

from __future__ import annotations

#: The project icon (``images/gco-icon.svg``, byte for byte; a test keeps them equal).
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" role="img" aria-labelledby="title">
  <title id="title">Global Capacity Orchestrator icon</title>
  <rect x="0" y="0" width="200" height="200" rx="44" fill="#171B26"/>
  <path d="M148.01 56.77A64.6 64.6 0 1 0 164.6 100L149.4 100A49.4 49.4 0 1 1 136.71 66.94A7.6 7.6 0 0 0 148.01 56.77Z" fill="#F2F4F7"/>
  <path d="M100 92.4L164.15 92.4A64.6 64.6 0 0 1 164.15 107.6L100 107.6Z" fill="#F2F4F7"/>
  <path d="M127.88 73.08A38.76 38.76 0 1 0 127.88 126.92A7.6 7.6 0 0 0 116.95 116.37A23.56 23.56 0 1 1 116.95 83.63A7.6 7.6 0 0 0 127.88 73.08Z" fill="#FF6E42"/>
  <path d="M110.64 100A10.64 10.64 0 1 0 89.36 100A10.64 10.64 0 1 0 110.64 100Z" fill="#F2F4F7"/>
</svg>
"""

ORANGE = "#FF6E42"
INK = "#171B26"
PAPER = "#F2F4F7"
MUTED = "#8A93A6"

#: One colour per job or queue status, in the order legends list them.
STATUS_COLORS: dict[str, str] = {
    "running": "#3B82F6",
    "pending": "#F59E0B",
    "queued": "#A78BFA",
    "claimed": "#8B5CF6",
    "applying": "#6366F1",
    "succeeded": "#10B981",
    "failed": "#EF4444",
    "cancelled": "#64748B",
}

#: Streamlit badge colours for the same statuses (``st.badge`` / ``:color-badge[]``).
STATUS_BADGES: dict[str, str] = {
    "running": "blue",
    "pending": "orange",
    "queued": "violet",
    "claimed": "violet",
    "applying": "violet",
    "succeeded": "green",
    "failed": "red",
    "cancelled": "gray",
}

#: Inference endpoint states (the registry's desired state and each Region's state).
ENDPOINT_COLORS: dict[str, str] = {
    "running": "#10B981",
    "deploying": "#8B5CF6",
    "creating": "#8B5CF6",
    "updating": "#F59E0B",
    "waiting": "#64748B",
    "stopped": "#94A3B8",
    "error": "#EF4444",
    "failed": "#EF4444",
    "deleted": "#475569",
}

#: Region health, as the map and the Region cards colour it.
HEALTH_COLORS: dict[str, str] = {
    "healthy": "#10B981",
    "degraded": "#F59E0B",
    "unhealthy": "#EF4444",
    "unknown": "#64748B",
}

HEALTH_BADGES: dict[str, str] = {
    "healthy": "green",
    "degraded": "orange",
    "unhealthy": "red",
    "unknown": "gray",
}

#: Categorical series colours (Regions, services, zones), brand orange first.
SERIES = (
    "#FF6E42",
    "#2DD4BF",
    "#60A5FA",
    "#F472B6",
    "#FBBF24",
    "#A78BFA",
    "#34D399",
    "#F87171",
    "#38BDF8",
    "#C084FC",
)


def rgba(hex_color: str, alpha: int = 255) -> list[int]:
    """``#RRGGBB`` as the ``[r, g, b, a]`` list deck.gl layers take."""
    value = hex_color.lstrip("#")
    return [int(value[i : i + 2], 16) for i in (0, 2, 4)] + [alpha]


def status_badge(status: str) -> str:
    """A status as Streamlit badge markdown, e.g. ``:blue-badge[running]``."""
    color = STATUS_BADGES.get(status, "gray")
    return f":{color}-badge[{status}]"


def health_badge(health: str) -> str:
    """A health verdict as Streamlit badge markdown."""
    color = HEALTH_BADGES.get(health, "gray")
    return f":{color}-badge[{health}]"
