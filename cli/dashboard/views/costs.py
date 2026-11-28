"""Costs: Kubernetes allocation cost (OpenCost via Athena) and the AWS bill (Cost Explorer)."""

from __future__ import annotations

from typing import Any

import streamlit as st

from cli.dashboard import charts, frames, ui
from cli.dashboard.regions import region_label

BILL_KEY = "costs_bill_days"
WINDOWS = (1, 7, 14, 30)


def _allocation(regions: tuple[str, ...]) -> None:
    controls = st.container(horizontal=True, vertical_alignment="bottom", gap="medium")
    days = int(
        controls.segmented_control(
            "Window",
            list(WINDOWS),
            default=7,
            required=True,
            format_func=lambda d: f"{d} d",
            key="alloc_days",
        )
        or 7
    )
    granularity = str(
        controls.segmented_control(
            "Buckets",
            ["hourly", "daily"],
            default="hourly" if days <= 7 else "daily",
            required=True,
            key=f"alloc_granularity_{days <= 7}",
        )
        or "daily"
    )
    report = ui.attempt(ui.load_allocation_costs, days, granularity)
    if report is None:
        return
    for error in report.get("errors") or []:
        st.caption(f":material/warning: {error}")
    tables = frames.allocation_frames(report)
    namespace_totals = tables["namespace_totals"]
    region_totals = tables["region_totals"]
    # The namespace and Region breakdowns split the same spend, but each is its
    # own query and a failed one comes back empty (its error is listed above).
    # The total and the GPU share are read from the same breakdown so they
    # agree: the namespaces, or the Regions when the namespaces have no rows.
    if namespace_totals.empty:
        totals, components = region_totals, tables["regions"]
    else:
        totals, components = namespace_totals, tables["namespaces"]
    total = float(totals["amount"].sum())
    with st.container(horizontal=True, gap="small"):
        st.metric(
            f"Allocated, last {days} d",
            f"${total:,.2f}",
            border=True,
            height="stretch",
            help="What OpenCost attributes to pods by their CPU, memory, GPU and storage",
        )
        if not namespace_totals.empty:
            top = namespace_totals.loc[namespace_totals["amount"].idxmax()]
            st.metric(
                "Top namespace",
                str(top["namespace"]),
                border=True,
                height="stretch",
                delta=f"${float(top['amount']):,.2f}",
                delta_color="off",
                delta_arrow="off",
            )
        if not region_totals.empty:
            top_region = region_totals.loc[region_totals["amount"].idxmax()]
            st.metric(
                "Top Region",
                region_label(str(top_region["region"])),
                border=True,
                height="stretch",
                delta=f"${float(top_region['amount']):,.2f}",
                delta_color="off",
                delta_arrow="off",
            )
        gpu_total = float(components.loc[components["component"] == "GPU", "amount"].sum())
        st.metric(
            "GPU share", f"{gpu_total / total:.0%}" if total else "—", border=True, height="stretch"
        )
    if tables["trend"].empty and totals.empty:
        st.info(
            "No allocation reports in this window yet: each Region's cost monitor writes one an hour.",
            icon=":material/schedule:",
        )
        return
    st.markdown(f"**Over time ({granularity})**")
    st.altair_chart(
        charts.allocation_trend(tables["trend"], granularity=granularity), width="stretch"
    )
    by_namespace, by_region = st.columns(2, gap="large")
    with by_namespace:
        st.markdown("**By namespace**")
        st.altair_chart(
            charts.allocation_stack(tables["namespaces"], category="namespace"), width="stretch"
        )
    with by_region:
        st.markdown("**By Region**")
        st.altair_chart(
            charts.allocation_stack(tables["regions"], category="region"), width="stretch"
        )
        monitors = ui.attempt(ui.load_cost_monitors, regions)
        if monitors:
            status = frames.cost_monitor_frame(monitors)
            st.dataframe(
                status,
                hide_index=True,
                width="stretch",
                column_config={
                    "region": st.column_config.TextColumn("Region"),
                    "namespaces": st.column_config.NumberColumn("Namespaces"),
                    "error": st.column_config.TextColumn("Error"),
                    "opencost": st.column_config.CheckboxColumn("OpenCost"),
                    "returning_data": st.column_config.CheckboxColumn("Data"),
                    "interval_min": st.column_config.NumberColumn("Every (min)"),
                    "last_window_end": st.column_config.DatetimeColumn(
                        "Last report", format="distance"
                    ),
                    "last_cost": st.column_config.NumberColumn("Last hour", format="$%.4f"),
                },
            )
    ui.cli_hint(
        ui.shell_words("gco", "costs", "k8s", "namespaces", "--days", str(days)),
        ui.shell_words(
            "gco", "costs", "k8s", "trend", "--days", str(days), "--granularity", granularity
        ),
    )


def _bill() -> None:
    st.caption(
        "Cost Explorer bills USD 0.01 per request, and a load makes about five, so this view "
        "reads only when you ask and keeps the answer for 15 minutes."
    )
    controls = st.container(horizontal=True, vertical_alignment="bottom", gap="medium")
    days = int(
        controls.segmented_control(
            "Window",
            [7, 14, 30, 90],
            default=30,
            required=True,
            format_func=lambda d: f"{d} d",
            key="bill_days",
        )
        or 30
    )
    if controls.button("Load from Cost Explorer", icon=":material/receipt_long:", key="bill_load"):
        st.session_state[BILL_KEY] = days
    if st.session_state.get(BILL_KEY) != days:
        return
    if controls.button("Reload", icon=":material/refresh:", key="bill_reload"):
        ui.load_costs.clear()
    report = ui.attempt(ui.load_costs, days)
    if report is None:
        return
    for error in report.get("errors") or []:
        st.caption(f":material/warning: {error}")
    tables = frames.cost_frames(report)
    currency = str(report.get("currency") or "USD")
    daily = tables["daily"]
    forecast = report.get("forecast") or {}
    if not float(report.get("total") or 0) and daily["amount"].sum() == 0:
        st.info(
            f"Cost Explorer attributes no spend in the last {days} days to the project's tags. "
            "Its queries filter on the `Project` tag, which counts only once that key is "
            "activated as a cost allocation tag (`gco costs allocation status`; in an "
            "Organization only the management account can activate it), and new spend "
            "reaches Cost Explorer about a day late. The Kubernetes allocation tab reads "
            "OpenCost instead and needs neither.",
            icon=":material/sell:",
        )
        ui.cli_hint("gco costs allocation status", "gco costs allocation activate")
        return
    with st.container(horizontal=True, gap="small"):
        st.metric(
            f"Spend, last {days} d",
            f"{float(report.get('total') or 0):,.2f} {currency}",
            border=True,
            height="stretch",
            help="Resources tagged with the project, unblended",
        )
        st.metric(
            "Daily average",
            f"{float(daily['amount'].mean()) if not daily.empty else 0:,.2f} {currency}",
            border=True,
            height="stretch",
        )
        if forecast:
            st.metric(
                "Next 30 days (forecast)",
                f"{float(forecast.get('forecast_total') or 0):,.2f} {currency}",
                border=True,
                height="stretch",
            )
    if not daily.empty:
        st.altair_chart(charts.cost_trend(daily, currency), width="stretch")
    services, regions = st.columns(2, gap="large")
    with services:
        st.markdown("**By service**")
        st.altair_chart(
            charts.ranked_bars(
                tables["services"].head(12), category="service", value="amount", title=currency
            ),
            width="stretch",
        )
    with regions:
        st.markdown("**By Region**")
        st.altair_chart(
            charts.ranked_bars(
                tables["regions"], category="region", value="amount", title=currency
            ),
            width="stretch",
        )
    tags: list[dict[str, Any]] = report.get("allocation_tags") or []
    if tags:
        st.markdown("**Cost allocation tags**")
        inactive = [t for t in tags if str(t.get("status", "")).lower() != "active"]
        if inactive:
            st.warning(
                "Spend is attributed only through active cost allocation tags; activate the others "
                "with `gco costs allocation activate` (management account only in an Organization).",
                icon=":material/sell:",
            )
        st.dataframe(tags, hide_index=True, width="stretch")
    ui.cli_hint(
        ui.shell_words("gco", "costs", "summary", "--days", str(days)),
        ui.shell_words("gco", "costs", "trend", "--days", str(days)),
        "gco costs allocation status",
    )


def render() -> None:
    topology = ui.load_topology()
    regions = ui.selected_regions(topology)
    ui.page_header(
        "Costs",
        "What the pods cost by namespace and Region, and what AWS bills for the project",
        icon=":material/payments:",
    )
    allocation, bill = st.tabs(
        ["Kubernetes allocation", "AWS bill"], key="costs_tab", on_change="rerun"
    )
    if allocation.open:
        with allocation:
            _allocation(regions)
    if bill.open:
        with bill:
            _bill()
