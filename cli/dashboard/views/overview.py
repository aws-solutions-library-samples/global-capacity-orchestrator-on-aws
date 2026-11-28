"""Fleet: every Region at a glance, on a map, with what needs attention."""

from __future__ import annotations

import contextlib
import functools
import math
from datetime import UTC, datetime
from typing import Any

import streamlit as st

from cli.dashboard import brand, charts, frames, ui
from cli.dashboard.data import RegionRead, Topology

HISTORY_KEY = "_fleet_history"
HISTORY_LENGTH = 40
HOURS_KEY = "fleet_hours"


def _gpu_share(regions: Any) -> tuple[float | None, int | None, int | None]:
    """The fleet's GPU allocation: ``(percent, requested GPUs, allocatable GPUs)``.

    Only Regions whose health monitor answered count. When each reports its
    GPU counts, the share is the fleet's requested GPUs over its allocatable
    ones, so a Region without GPUs adds nothing and a Region with many GPUs
    outweighs one with a few (``percent`` is None when there are no GPUs).
    A monitor deployed before the counts existed reports only its
    percentage, so then the percentages are averaged, leaving out the
    Regions whose counts show they have no GPUs.
    """
    answered = regions[regions["gpu"].notna()]
    if answered.empty:
        return None, None, None
    counted = answered["gpu_allocatable"].notna() & answered["gpu_requested"].notna()
    if counted.all():
        allocatable = int(answered["gpu_allocatable"].sum())
        requested = int(answered["gpu_requested"].sum())
        return (requested / allocatable * 100 if allocatable else None), requested, allocatable
    no_gpus = counted & (answered["gpu_allocatable"] == 0) & (answered["gpu_requested"] == 0)
    return float(answered.loc[~no_gpus, "gpu"].mean()), None, None


def _kpis(regions: Any, jobs: Any, endpoints: Any, queue_stats: dict[str, Any]) -> dict[str, Any]:
    """The headline numbers, computed from the frames the page already holds."""
    gpu, gpu_requested, gpu_allocatable = _gpu_share(regions)
    healthy = int((regions["health"] == "healthy").sum())
    summary = queue_stats.get("summary") or {}
    return {
        "healthy": healthy,
        "regions": len(regions),
        "running": int((jobs["status"] == "running").sum()) if not jobs.empty else 0,
        "failed": int((jobs["status"] == "failed").sum()) if not jobs.empty else 0,
        "queued": int(regions["queued"].sum()) + int(summary.get("total_queued", 0) or 0),
        "gpu": gpu,
        "gpu_requested": gpu_requested,
        "gpu_allocatable": gpu_allocatable,
        "endpoints": int((endpoints["state"] == "running").sum()) if not endpoints.empty else 0,
        "replicas": int(endpoints["ready"].sum()) if not endpoints.empty else 0,
        "dlq": int(regions["dlq"].sum()),
    }


def _gpu_metric(kpis: dict[str, Any]) -> tuple[str, str | None]:
    """The "GPU allocated" value and, where the counts are known, how many GPUs that is."""
    requested, allocatable = kpis["gpu_requested"], kpis["gpu_allocatable"]
    if allocatable == 0 and requested == 0:
        return "no GPUs", None
    value = "—" if kpis["gpu"] is None else f"{kpis['gpu']:.0f}%"
    if allocatable is None:
        return value, None
    return value, f"{requested} of {ui.plural(allocatable, 'GPU')}"


def _remember(kpis: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep a short rolling history of the headline numbers for the sparklines."""
    history: list[dict[str, Any]] = st.session_state.setdefault(HISTORY_KEY, [])
    history.append(kpis)
    del history[:-HISTORY_LENGTH]
    return history


def _series(history: list[dict[str, Any]], key: str) -> list[float] | None:
    values = [float(h[key]) for h in history if h.get(key) is not None]
    return values if len(values) > 1 else None


def _headline(fleet: dict[str, Any] | None, kpis: dict[str, Any]) -> None:
    findings = (fleet or {}).get("findings") or []
    errors = sum(1 for f in findings if f.get("severity") == "error")
    if fleet is None:
        st.badge("Fleet status unavailable", icon=":material/help:", color="gray")
    elif errors:
        st.badge(
            f"{ui.plural(errors, 'problem')} {'needs' if errors == 1 else 'need'} attention",
            icon=":material/error:",
            color="red",
        )
    elif findings:
        st.badge(
            f"Healthy with {ui.plural(len(findings), 'warning')}",
            icon=":material/warning:",
            color="orange",
        )
    else:
        st.badge("All systems healthy", icon=":material/check_circle:", color="green")


def _kpi_strip(kpis: dict[str, Any], history: list[dict[str, Any]]) -> None:
    with st.container(horizontal=True, gap="small"):
        st.metric(
            "Regions healthy",
            f"{kpis['healthy']} / {kpis['regions']}",
            border=True,
            height="stretch",
            help="Regional stack deployed and its health monitor answering",
        )
        st.metric(
            "Running jobs",
            kpis["running"],
            border=True,
            height="stretch",
            chart_data=_series(history, "running"),
            chart_type="area",
            help="Kubernetes Jobs with an active pod, across every Region",
        )
        st.metric(
            "Waiting",
            kpis["queued"],
            border=True,
            height="stretch",
            chart_data=_series(history, "queued"),
            chart_type="bar",
            help="SQS messages not yet picked up plus central-queue records still queued",
        )
        gpu_value, gpu_count = _gpu_metric(kpis)
        st.metric(
            "GPU allocated",
            gpu_value,
            delta=gpu_count,
            delta_color="off",
            delta_arrow="off",
            border=True,
            height="stretch",
            chart_data=_series(history, "gpu"),
            help="GPUs requested by running pods, out of the GPUs the nodes offer, per the "
            "health monitors (the Regions' average share while one reports no GPU counts)",
        )
        st.metric(
            "Inference",
            ui.plural(kpis["endpoints"], "endpoint"),
            delta=f"{ui.plural(kpis['replicas'], 'replica')} ready",
            delta_color="off",
            delta_arrow="off",
            border=True,
            height="stretch",
            help="Endpoints the registry wants running, and their ready replicas across Regions",
        )
        st.metric(
            "Dead letters",
            kpis["dlq"],
            border=True,
            height="stretch",
            delta="clear" if kpis["dlq"] == 0 else "inspect the DLQ",
            delta_color="normal" if kpis["dlq"] == 0 else "inverse",
            delta_arrow="off",
            help="Messages SQS moved to a dead-letter queue after repeated failures",
        )


def _gauge(where: Any, label: str, value: object) -> None:
    if not isinstance(value, int | float) or math.isnan(value):
        where.caption(f"{label} —")
        return
    where.progress(min(float(value), 100.0) / 100, text=f"{label} {float(value):.0f}%")


def _count(value: object) -> int | None:
    """A GPU count from a Region row; None where the Region sent none (an empty cell)."""
    return int(value) if isinstance(value, int | float) and not math.isnan(value) else None


def _gpu_gauge(where: Any, row: dict[str, Any]) -> None:
    """A Region's GPUs requested of those allocatable, or that it has no GPU nodes.

    A Region whose health monitor sends no counts shows its percentage.
    """
    allocatable, requested = _count(row["gpu_allocatable"]), _count(row["gpu_requested"])
    if allocatable is None or requested is None:
        _gauge(where, "GPU", row["gpu"])
    elif allocatable == requested == 0:
        where.caption("No GPU nodes")
    else:
        # More requested than allocatable (a device plugin restarting) is a full bar.
        share = requested / allocatable if allocatable else 1.0
        where.progress(min(share, 1.0), text=f"GPU {requested}/{allocatable}")


def _region_card(row: Any) -> None:
    with st.container(border=True, height="stretch"):
        top = st.container(horizontal=True, vertical_alignment="center", gap="small")
        top.markdown(f"**{row['region']}** · {row['city']}")
        top.markdown(brand.health_badge(row["health"]))
        gpu, cpu, memory = st.columns(3, gap="small")
        _gpu_gauge(gpu, row)
        _gauge(cpu, "CPU", row["cpu"])
        _gauge(memory, "Mem", row["memory"])
        st.caption(
            f":material/play_circle: {row['running']} running · "
            f":material/hourglass_top: {row['queued']} waiting · "
            f":material/smart_toy: {ui.plural(row['endpoints'], 'endpoint')}"
        )
        if row["error"]:
            st.caption(f":material/warning: {row['error']}")


def _findings(fleet: dict[str, Any] | None) -> None:
    table = frames.findings_frame(fleet)
    if fleet is None:
        return
    if table.empty:
        st.success(
            "No findings: every section of `gco status` reads clean.", icon=":material/verified:"
        )
        return
    for row in table.itertuples():
        icon = ":material/error:" if row.severity == "error" else ":material/warning:"
        line = f"**{row.section}** · {row.message}"
        if row.severity == "error":
            st.error(line, icon=icon)
        else:
            st.warning(line, icon=icon)


def _utilization(regions: tuple[str, ...]) -> None:
    hours = st.segmented_control(
        "Window",
        options=[1, 3, 12, 24],
        default=3,
        format_func=lambda h: f"{h} h",
        key=HOURS_KEY,
        label_visibility="collapsed",
        required=True,
    )
    rows = ui.attempt(ui.load_utilization, regions, int(hours or 3))
    if rows is None:
        return
    # FleetData's rows name the Regions it could not read (a row list from
    # anywhere else names none); the other Regions are charted as usual.
    failed: dict[str, str] = getattr(rows, "errors", {})
    for region, error in failed.items():
        st.warning(f"Utilization in {region} could not be read: {error}", icon=":material/warning:")
    series = frames.utilization_frame(rows)
    if series.empty:
        st.info("No utilization samples yet: the health monitors publish one a minute.")
        return
    gpu, cpu, memory, active = st.tabs(["GPU", "CPU", "Memory", "Active jobs"])
    # A Region without GPU nodes publishes 0% GPU, which is not an idle GPU:
    # its line is left off the GPU chart (the health monitors' counts tell).
    no_gpus = _without_gpu_nodes(regions)
    gpu_series = series[~((series["metric"] == "gpu") & series["region"].isin(no_gpus))]
    if (gpu_series["metric"] == "gpu").any():
        gpu.altair_chart(charts.utilization_lines(gpu_series, "gpu"), width="stretch")
    else:
        gpu.info("No Region has GPU nodes right now.", icon=":material/info:")
    if no_gpus:
        gpu.caption(f"No GPU nodes in {', '.join(sorted(no_gpus))}.")
    for tab, metric in ((cpu, "cpu"), (memory, "memory"), (active, "active_jobs")):
        tab.altair_chart(charts.utilization_lines(series, metric), width="stretch")


def _without_gpu_nodes(regions: tuple[str, ...]) -> set[str]:
    """The Regions whose health monitor counts no GPUs, allocatable or requested."""
    reads: list[RegionRead] = []
    # The live panel above reports a failed read; this one stays quiet.
    with contextlib.suppress(Exception):
        reads = ui.load_cluster_metrics(regions)
    found = set()
    for read in reads:
        utilization = (read.data.get("resource_utilization") or {}) if read.ok else {}
        if utilization.get("gpu_allocatable") == 0 and not utilization.get("gpu_requested"):
            found.add(read.region)
    return found


def _recent_jobs(jobs: Any) -> None:
    if jobs.empty:
        st.caption("No Kubernetes Jobs in the selected Regions.")
        return
    recent = jobs.head(8)[["name", "region", "status", "created", "runtime_s"]]
    st.dataframe(
        recent,
        hide_index=True,
        width="stretch",
        column_config={
            "name": st.column_config.TextColumn("Job"),
            "region": st.column_config.TextColumn("Region"),
            "status": st.column_config.MultiselectColumn(
                "Status",
                options=list(brand.STATUS_COLORS),
                color=list(brand.STATUS_COLORS.values()),
            ),
            "created": st.column_config.DatetimeColumn("Submitted", format="distance"),
            "runtime_s": st.column_config.NumberColumn("Runtime", format="duration"),
        },
    )


def render() -> None:
    topology: Topology = ui.load_topology()
    regions = ui.selected_regions(topology)
    ui.page_header(
        "Fleet",
        f"{topology.project} · {ui.plural(len(topology.workload_regions), 'workload Region')}",
        icon=":material/public:",
    )

    @st.fragment(run_every=ui.refresh_interval())
    def live() -> None:
        ui.prefetch(
            ui.load_fleet_status,
            functools.partial(ui.load_cluster_metrics, regions),
            functools.partial(ui.load_jobs, regions),
            ui.load_endpoints,
            ui.load_queue_stats,
        )
        fleet = ui.attempt(ui.load_fleet_status)
        metrics: list[RegionRead] = ui.attempt(ui.load_cluster_metrics, regions) or []
        snapshot = ui.attempt(ui.load_jobs, regions)
        jobs = frames.jobs_frame(snapshot.jobs if snapshot else [])
        jobs = frames.filter_jobs(jobs, regions=regions)
        endpoint_list = ui.attempt(ui.load_endpoints) or []
        endpoints = frames.endpoints_frame(endpoint_list)
        queue_stats = ui.attempt(ui.load_queue_stats) or {}
        region_rows = frames.regions_frame(
            topology,
            fleet,
            metrics,
            jobs=jobs,
            endpoints=list(endpoint_list),
        )
        region_rows = region_rows[region_rows["region"].isin(regions)].reset_index(drop=True)
        kpis = _kpis(region_rows, jobs, endpoints, queue_stats)
        history = _remember(kpis)

        _headline(fleet, kpis)
        _kpi_strip(kpis, history)
        if snapshot and snapshot.errors:
            for region, error in snapshot.errors.items():
                st.warning(
                    f"Jobs in {region} could not be read: {error}", icon=":material/warning:"
                )

        map_header = st.container(horizontal=True, vertical_alignment="center")
        map_header.subheader("Regions", anchor=False)
        globe = map_header.toggle("Globe", key="fleet_globe", help="Show the Regions on a globe")
        st.pydeck_chart(
            charts.world_map(
                region_rows,
                control_region=topology.global_region,
                dark=ui.is_dark(),
                globe=globe,
            ),
            height=440,
        )
        records = region_rows.to_dict("records")
        # Up to four cards a row, and no more columns than cards, so a fleet
        # of three Regions fills the row instead of leaving a gap.
        with st.grid(columns=min(max(len(records), 1), 4), min_column_width=300, gap="small"):
            for row in records:
                _region_card(row)
        st.caption(
            f"Updated {datetime.now(UTC):%H:%M:%S} UTC"
            + (f" · refreshing every {ui.refresh_interval()} s" if ui.refresh_interval() else "")
        )

    live()

    st.subheader("Utilization", anchor=False)
    _utilization(regions)

    attention, recent = st.columns([1, 1], gap="large")
    with attention:
        st.subheader("Needs attention", anchor=False)
        _findings(ui.attempt(ui.load_fleet_status))
    with recent:
        st.subheader("Latest jobs", anchor=False)
        snapshot = ui.attempt(ui.load_jobs, regions)
        _recent_jobs(
            frames.filter_jobs(
                frames.jobs_frame(snapshot.jobs if snapshot else []), regions=regions
            )
        )
        st.page_link("app_pages/jobs.py", label="All jobs", icon=":material/arrow_forward:")
