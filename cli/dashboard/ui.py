"""Streamlit plumbing every page shares: cached reads, session state and widgets.

Reads go through the ``load_*`` functions below. Each wraps one
:class:`~cli.dashboard.data.FleetData` call in ``st.cache_data`` with a TTL
matched to how fast its data changes, so moving between pages is instant and a
refresh costs one round of AWS calls rather than one per widget. Cost Explorer
bills per request, so its loader caches for longest and only runs on request.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

from cli.config import get_config
from cli.dashboard import brand
from cli.dashboard.data import (
    DashboardError,
    ExampleManifest,
    FleetData,
    JobsSnapshot,
    RegionRead,
    ServingDefaults,
    Topology,
    clear_discovery_cache,
    describe_error,
)
from cli.dashboard.regions import region_label
from cli.dashboard.server import TOKEN_ENV

REGION_FILTER_KEY = "region_filter"
AUTO_REFRESH_KEY = "auto_refresh"
REFRESH_SECONDS_KEY = "refresh_seconds"
REFRESH_CHOICES = (15, 30, 60, 300)


@st.cache_resource(show_spinner=False)
def fleet() -> FleetData:
    """The one data object for this server process (configuration is static)."""
    return FleetData(get_config())


# ---------------------------------------------------------------------------
# Cached reads
# ---------------------------------------------------------------------------


@st.cache_data(ttl=300, show_spinner=False)
def load_topology() -> Topology:
    return fleet().topology()


@st.cache_data(ttl=900, show_spinner=False)
def load_identity() -> dict[str, str]:
    return fleet().identity()


@st.cache_data(ttl=60, show_spinner=False)
def load_fleet_status() -> dict[str, Any]:
    return fleet().fleet_status()


@st.cache_data(ttl=300, show_spinner="Reading every Region's enforced job policy...")
def load_fleet_status_with_policy() -> dict[str, Any]:
    return fleet().fleet_status(with_policy=True)


@st.cache_data(ttl=15, show_spinner=False)
def load_cluster_metrics(regions: tuple[str, ...]) -> list[RegionRead]:
    return fleet().cluster_metrics(regions)


@st.cache_data(ttl=60, show_spinner="Reading the health monitors' CloudWatch series...")
def load_utilization(regions: tuple[str, ...], hours: int) -> list[dict[str, Any]]:
    """The samples; FleetData's list also names, in ``errors``, the Regions it could not read."""
    return fleet().utilization_history(regions, hours=hours, period=60 if hours <= 6 else 300)


@st.cache_data(ttl=20, show_spinner=False)
def load_jobs(regions: tuple[str, ...]) -> JobsSnapshot:
    return fleet().jobs(regions)


@st.cache_data(ttl=20, show_spinner=False)
def load_queue_jobs(status: str | None, region: str | None) -> dict[str, Any]:
    return fleet().queue_jobs(status=status, target_region=region)


@st.cache_data(ttl=20, show_spinner=False)
def load_queue_stats() -> dict[str, Any]:
    return fleet().queue_stats()


@st.cache_data(ttl=20, show_spinner=False)
def load_endpoints() -> list[dict[str, Any]]:
    return fleet().endpoints()


@st.cache_data(ttl=120, show_spinner=False)
def load_examples() -> list[ExampleManifest]:
    return fleet().examples()


@st.cache_data(ttl=120, show_spinner=False)
def load_inference_example(name: str) -> ServingDefaults:
    return fleet().inference_example(name)


@st.cache_data(ttl=900, show_spinner="Asking Cost Explorer...")
def load_costs(days: int) -> dict[str, Any]:
    return fleet().costs(days=days)


@st.cache_data(ttl=900, show_spinner="Reading Spot price history...")
def load_spot_history(
    instance_type: str, regions: tuple[str, ...], days: int
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for region in regions:
        rows.extend(fleet().spot_price_history(instance_type, region, days=days))
    return rows


@st.cache_data(ttl=900, show_spinner="Reading offerings and prices in every Region...")
def load_instance_report(instance_type: str, regions: tuple[str, ...]) -> list[RegionRead]:
    return fleet().instance_report(instance_type, regions)


@st.cache_data(ttl=60, show_spinner=False)
def load_global_health() -> dict[str, Any]:
    return fleet().global_health()


@st.cache_data(ttl=60, show_spinner=False)
def load_regional_status(regions: tuple[str, ...]) -> list[RegionRead]:
    return fleet().regional_status(regions)


@st.cache_data(ttl=60, show_spinner=False)
def load_templates() -> list[dict[str, Any]]:
    return fleet().templates()


@st.cache_data(ttl=60, show_spinner=False)
def load_webhooks() -> list[dict[str, Any]]:
    return fleet().webhooks()


@st.cache_data(ttl=300, show_spinner="Reading each Region's job policy...")
def load_regional_policy(regions: tuple[str, ...]) -> list[RegionRead]:
    return fleet().regional_policy(regions)


@st.cache_data(ttl=120, show_spinner="Reading each Region's cost monitor...")
def load_cost_monitors(regions: tuple[str, ...]) -> list[RegionRead]:
    return fleet().cost_monitors(regions)


@st.cache_data(ttl=600, show_spinner="Querying the OpenCost reports with Athena...")
def load_allocation_costs(days: int, granularity: str) -> dict[str, Any]:
    return fleet().allocation_costs(days=days, granularity=granularity)


@st.cache_data(ttl=10, show_spinner=False)
def load_job_detail(name: str, namespace: str, region: str) -> dict[str, Any]:
    return fleet().job_detail(name, namespace, region)


@st.cache_data(ttl=10, show_spinner=False)
def load_job_pods(name: str, namespace: str, region: str) -> dict[str, Any]:
    return fleet().job_pods(name, namespace, region)


@st.cache_data(ttl=15, show_spinner=False)
def load_job_events(name: str, namespace: str, region: str) -> dict[str, Any]:
    return fleet().job_events(name, namespace, region)


@st.cache_data(ttl=15, show_spinner=False)
def load_job_metrics(name: str, namespace: str, region: str) -> dict[str, Any]:
    return fleet().job_metrics(name, namespace, region)


@st.cache_data(ttl=5, show_spinner=False)
def load_job_logs(name: str, namespace: str, region: str, tail: int, container: str | None) -> str:
    return fleet().job_logs(name, namespace, region, tail=tail, container=container)


@st.cache_data(ttl=10, show_spinner=False)
def load_queue_job(job_id: str) -> dict[str, Any]:
    return fleet().queue_job(job_id)


@st.cache_data(ttl=300, show_spinner=False)
def load_served_model(name: str, region: str | None) -> str:
    return fleet().served_model(name, region)


@st.cache_data(ttl=3600, show_spinner=False)
def load_placement_score(instance_type: str, region: str, capacity: int) -> int | None:
    return fleet().placement_score(instance_type, region, capacity=capacity)


#: Reads of live fleet state: dropped by "Refresh now" and after every change
#: made from the dashboard. Billed or slow-moving reads (Cost Explorer, Spot
#: price history, placement scores, identity) are not, so a write never
#: re-bills Cost Explorer; each of those has its own reload control.
LIVE_LOADERS: tuple[Any, ...] = (
    load_topology,
    load_fleet_status,
    load_fleet_status_with_policy,
    load_cluster_metrics,
    load_utilization,
    load_jobs,
    load_queue_jobs,
    load_queue_stats,
    load_endpoints,
    load_examples,
    load_inference_example,
    load_global_health,
    load_regional_status,
    load_templates,
    load_webhooks,
    load_regional_policy,
    load_cost_monitors,
    load_job_detail,
    load_job_pods,
    load_job_events,
    load_job_metrics,
    load_job_logs,
    load_queue_job,
)


def refresh() -> None:
    """Drop the live reads and endpoint discovery, so the next run re-reads AWS."""
    for loader in LIVE_LOADERS:
        loader.clear()
    clear_discovery_cache()


FLASH_KEY = "_flash"


def flash(message: str, *, icon: str = ":material/check_circle:") -> None:
    """Queue a toast for the next run (a toast drawn just before ``st.rerun`` is lost)."""
    st.session_state.setdefault(FLASH_KEY, []).append((message, icon))


def show_flash() -> None:
    """Draw and forget the toasts queued by :func:`flash`."""
    for message, icon in st.session_state.pop(FLASH_KEY, []):
        st.toast(message, icon=icon)


def prefetch(
    *loaders: Callable[[], object], message: str = "Reading the fleet...", timeout: float = 60.0
) -> None:
    """Warm independent cached reads at the same time instead of one after another.

    Each loader runs on its own thread carrying this script run's context, so
    ``st.cache_data`` stores its result for the page to read back instantly.
    A loader that fails, or is still running after ``timeout`` seconds (for
    all of them together), is left for the page's own call to report. The
    loaders passed here draw no spinner of their own (an element drawn from
    a worker thread can land anywhere on the page); this one spinner, on the
    script's own thread, stands for them all.
    """
    context = get_script_run_ctx()

    def run(loader: Callable[[], object]) -> None:
        with contextlib.suppress(Exception):
            loader()

    threads = [threading.Thread(target=run, args=(loader,), daemon=True) for loader in loaders]
    with st.spinner(message):
        for thread in threads:
            add_script_run_ctx(thread, context)
            thread.start()
        # One deadline for them all: hung loaders cost ``timeout``, not one each.
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))


# ---------------------------------------------------------------------------
# Running reads and actions with friendly failures
# ---------------------------------------------------------------------------


def show_error(error: DashboardError, *, where: Any = None) -> None:
    """An error card with the operator hint, if the failure came with one."""
    target = where if where is not None else st
    body = f"**{error}**"
    if error.hint:
        body += f"\n\n{error.hint}"
    target.error(body, icon=":material/error:")


def attempt[T](read: Callable[..., T], *args: Any, where: Any = None, **kwargs: Any) -> T | None:
    """Run a read; on failure draw the error card and return None."""
    try:
        return read(*args, **kwargs)
    except Exception as exc:
        show_error(describe_error(exc), where=where)
        return None


def act[T](label: str, action: Callable[[], T], *, success: str, where: Any = None) -> T | None:
    """Run a write; on success drop the live reads it made stale and queue a toast.

    Returns the action's result, or None when it failed (the error card is
    drawn in ``where``, the current container by default). Callers usually
    ``st.rerun()`` after a success, which shows the toast and the new state.
    """
    try:
        with st.spinner(label):
            result = action()
    except Exception as exc:
        show_error(describe_error(exc), where=where)
        return None
    refresh()
    flash(success)
    return result


@st.dialog("Please confirm", width="small")
def confirm(
    message: str,
    *,
    label: str,
    action: Callable[[], object],
    success: str,
    icon: str = ":material/delete:",
) -> None:
    """A modal that runs ``action`` only when the operator confirms it."""
    st.markdown(message)
    with st.container(horizontal=True, horizontal_alignment="right"):
        if st.button("Cancel", key="confirm_cancel"):
            st.rerun()
        if st.button(label, type="primary", icon=icon, key="confirm_go") and (
            act(label, action, success=success) is not None
        ):
            st.rerun()


# ---------------------------------------------------------------------------
# Access token
# ---------------------------------------------------------------------------


def require_access() -> bool:
    """When the launcher set a token, admit a session only with ``?token=`` matching it.

    The token reaches the browser in the URL ``gco dashboard start`` opens.
    A session that presented it once stays admitted while it lives, and the
    token is put back on the URL after every page change so a reload works.
    """
    expected = os.environ.get(TOKEN_ENV, "")
    if not expected:
        return True
    if st.session_state.get("_admitted"):
        if st.query_params.get("token") != expected:
            st.query_params["token"] = expected
        return True
    supplied = st.query_params.get("token") or ""
    if supplied and hmac.compare_digest(supplied.encode(), expected.encode()):
        st.session_state["_admitted"] = True
        return True
    st.title("GCO dashboard", anchor=False)
    st.warning(
        "This dashboard needs its access link. Run `gco dashboard open` (or "
        "`gco dashboard status`) on the machine that started it.",
        icon=":material/lock:",
    )
    return False


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


def selected_regions(topology: Topology) -> tuple[str, ...]:
    """The sidebar's Region filter (every workload Region when nothing is picked)."""
    picked = st.session_state.get(REGION_FILTER_KEY) or []
    chosen = tuple(r for r in topology.workload_regions if r in picked)
    return chosen or topology.workload_regions


def refresh_interval() -> int | None:
    """Seconds between automatic refreshes, or None when auto-refresh is off."""
    if not st.session_state.get(AUTO_REFRESH_KEY):
        return None
    seconds = st.session_state.get(REFRESH_SECONDS_KEY, 30)
    return int(seconds) if isinstance(seconds, int) else 30


def is_dark() -> bool:
    """Whether the browser is showing the dark theme (dark when unknown)."""
    theme = getattr(st.context, "theme", None)
    return getattr(theme, "type", "dark") != "light"


# ---------------------------------------------------------------------------
# Shared widgets
# ---------------------------------------------------------------------------


def page_header(title: str, caption: str, *, icon: str) -> None:
    """The page title, what the page shows, and when it was read."""
    st.title(title, anchor=False, icon=icon)
    st.caption(f"{caption} · read {datetime.now(UTC):%H:%M:%S} UTC")


def cli_hint(*commands: str) -> None:
    """The ``gco`` commands that do what this panel shows, for the terminal-minded."""
    st.code("\n".join(commands), language="bash", wrap_lines=True)


def sidebar(topology: Topology) -> None:
    """Deployment summary, identity, the global Region filter and refresh controls."""
    with st.sidebar:
        st.markdown(f"### {topology.project}")
        st.caption(
            f"Control plane {topology.global_region} · API {topology.api_gateway_region} · "
            f"{'regional' if topology.regional_api else 'global'} API mode"
        )
        identity = attempt(load_identity)
        if identity:
            st.caption(f":material/badge: {identity['principal']}")
        if not topology.configured:
            st.warning(
                "No workload Regions are configured. Start the dashboard from a GCO "
                "checkout (its cdk.json lists the Regions).",
                icon=":material/warning:",
            )
        st.multiselect(
            "Regions",
            list(topology.workload_regions),
            key=REGION_FILTER_KEY,
            format_func=region_label,
            placeholder="All workload Regions",
        )
        st.toggle("Auto-refresh", key=AUTO_REFRESH_KEY)
        st.select_slider(
            "Every",
            options=list(REFRESH_CHOICES),
            value=30,
            key=REFRESH_SECONDS_KEY,
            format_func=lambda s: f"{s // 60} min" if s >= 60 else f"{s} s",
            disabled=not st.session_state.get(AUTO_REFRESH_KEY),
        )
        if st.button("Refresh now", icon=":material/refresh:", width="stretch"):
            refresh()
            st.rerun()
        st.divider()
        st.caption(
            "Every change made here runs as the signed-in AWS principal, exactly as "
            "the matching `gco` command would."
        )


def selected_row(event: object, rows: int) -> int | None:
    """The row a single-row ``st.dataframe`` selection holds, if it is still on the table.

    A selection survives reruns by position, so after a filter narrows the
    table it can point past the end; that reads as no selection.
    """
    selection = getattr(event, "selection", None)
    picked = list(getattr(selection, "rows", None) or [])
    if picked and isinstance(picked[0], int) and 0 <= picked[0] < rows:
        return picked[0]
    return None


def table_height(rows: int, *, limit: int = 18) -> int:
    """A dataframe height that shows ``rows`` rows (up to ``limit``) without a scrollbar."""
    return 38 + 35 * max(min(rows, limit), 1)


def detail_header(title: str, badges: str) -> Any:
    """A detail panel's title row: name and badges left, the returned row's free space right.

    Callers add their action buttons to the returned container, so the
    actions sit on the title's line instead of a row of their own.
    """
    head = st.container(horizontal=True, vertical_alignment="center", gap="small")
    head.subheader(title, anchor=False, width="content")
    head.markdown(badges, width="content")
    head.space("stretch")
    return head


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


#: The option a Region picker shows for "let the global API route it".
GLOBAL = "global"


def region_picker(
    topology: Topology,
    *,
    key: str,
    label: str = "Region",
    allow_global: bool = False,
    help: str | None = None,
    disabled: bool = False,
    width: int | Literal["stretch"] = "stretch",
) -> str:
    """A Region selectbox; with ``allow_global``, "global" (the global API) comes first.

    Regional API mode has no global API (an unpinned request rides the
    default Region's bridge), so there "global" is never offered.
    """
    offer_global = allow_global and not topology.regional_api
    options = ([GLOBAL] if offer_global else []) + list(topology.workload_regions)
    choice = st.selectbox(
        label,
        options,
        key=key,
        format_func=lambda r: (
            "Global API (nearest healthy Region)" if r == GLOBAL else region_label(r)
        ),
        help=help,
        disabled=disabled,
        width=width,
    )
    return str(choice) if choice is not None else GLOBAL


def status_column(label: str = "Status", **kwargs: Any) -> Any:
    """A dataframe column that draws job and queue statuses as coloured labels."""
    return st.column_config.MultiselectColumn(
        label,
        options=list(brand.STATUS_COLORS),
        color=list(brand.STATUS_COLORS.values()),
        **kwargs,
    )


def endpoint_state_column(label: str = "State", **kwargs: Any) -> Any:
    """A dataframe column that draws inference endpoint states as coloured labels."""
    return st.column_config.MultiselectColumn(
        label,
        options=list(brand.ENDPOINT_COLORS),
        color=list(brand.ENDPOINT_COLORS.values()),
        **kwargs,
    )


def health_column(label: str = "Health", **kwargs: Any) -> Any:
    """A dataframe column that draws health verdicts as coloured labels."""
    return st.column_config.MultiselectColumn(
        label,
        options=list(brand.HEALTH_COLORS),
        color=list(brand.HEALTH_COLORS.values()),
        **kwargs,
    )


def parse_pairs(text: str, *, what: str = "label") -> dict[str, str]:
    """``key=value`` pairs separated by commas or new lines, as a dict.

    Raises DashboardError naming the first entry that is not a pair, so a
    form can show it beside the field.
    """
    pairs: dict[str, str] = {}
    for raw in text.replace("\n", ",").split(","):
        entry = raw.strip()
        if not entry:
            continue
        key, separator, value = entry.partition("=")
        if not separator or not key.strip():
            raise DashboardError(f"'{entry}' is not a {what} of the form key=value.")
        pairs[key.strip()] = value.strip()
    return pairs


def shell_words(*words: str) -> str:
    """A command line with each word quoted for a POSIX shell where it needs it."""
    import shlex

    return " ".join(shlex.quote(word) for word in words)
