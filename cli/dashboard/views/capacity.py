"""Capacity: each Region's load, where new work would land, and what an instance type costs."""

from __future__ import annotations

from typing import Any

import streamlit as st

from cli.dashboard import charts, frames, ui
from cli.dashboard.data import Topology
from cli.dashboard.regions import region_label

RECOMMEND_KEY = "capacity_recommendation"
SCORES_KEY = "capacity_placement_scores"

#: Instance types the explorer offers first (any other can be typed in).
INSTANCE_TYPES = (
    "g7.2xlarge",
    "g6e.xlarge",
    "g6e.12xlarge",
    "g6.xlarge",
    "g5.xlarge",
    "g5.12xlarge",
    "g4dn.xlarge",
    "p4d.24xlarge",
    "p5.48xlarge",
    "p5e.48xlarge",
    "inf2.xlarge",
    "trn1.2xlarge",
    "trn2.48xlarge",
    "m7i.xlarge",
    "c7i.2xlarge",
)


def _load(fleet: dict[str, Any] | None, regions: tuple[str, ...]) -> None:
    # The health monitors' GPU counts say which Regions have no GPUs at all.
    metrics = ui.attempt(ui.load_cluster_metrics, regions) or []
    load = frames.capacity_frame(fleet, metrics)
    load = load[load["region"].isin(regions)].reset_index(drop=True)
    if load.empty:
        st.caption("No capacity readings in the status document.")
        return
    st.dataframe(
        load.assign(region=load["region"].map(region_label)),
        hide_index=True,
        width="stretch",
        column_config={
            "region": st.column_config.TextColumn("Region"),
            "gpu": st.column_config.ProgressColumn(
                "GPU use",
                min_value=0,
                max_value=100,
                format="%.0f%%",
                help="What placement scores on: GPU compute utilization from ContainerInsights "
                "(DCGM), or the share of GPUs pods request where a Region has no DCGM samples. "
                "Empty where the Region has no GPU nodes",
            ),
            "gpu_allocatable": st.column_config.NumberColumn(
                "GPUs", help="Allocatable GPUs on the Region's nodes, per its health monitor"
            ),
            "gpu_requested": st.column_config.NumberColumn(
                "GPUs requested", help="GPUs requested by running pods, per its health monitor"
            ),
            "cpu": st.column_config.ProgressColumn(
                "CPU requested", min_value=0, max_value=100, format="%.0f%%"
            ),
            "queue_depth": st.column_config.NumberColumn("SQS backlog", help="Messages waiting"),
            "running_jobs": st.column_config.NumberColumn("Active jobs"),
            "telemetry": st.column_config.TextColumn("Telemetry"),
            "missing": st.column_config.ListColumn("Signals missing"),
        },
    )


def _recommend() -> None:
    controls = st.container(horizontal=True, vertical_alignment="bottom", gap="medium")
    gpus = int(
        controls.number_input(
            "GPUs", min_value=0, max_value=512, value=1, key="reco_gpus", width=120
        )
    )
    instance_type = controls.selectbox(
        "Instance type",
        ["", *INSTANCE_TYPES],
        format_func=lambda t: t or "Any (score on cluster load only)",
        accept_new_options=True,
        key="reco_type",
        width=300,
    )
    backend = controls.segmented_control(
        "Backend",
        ["eks", "hyperpod", "pcs"],
        format_func=lambda k: {"eks": "EKS", "hyperpod": "HyperPod", "pcs": "PCS"}[k],
        default="eks",
        required=True,
        key="reco_backend",
        help="HyperPod and PCS rank only the Regions running that backend, on its own capacity.",
    )
    params = (gpus, str(instance_type or ""), str(backend))
    if controls.button(
        "Recommend", type="primary", icon=":material/travel_explore:", key="reco_go"
    ):
        with st.spinner("Scoring every Region..."):
            result = ui.attempt(
                ui.fleet().recommend_region,
                gpu_count=gpus,
                instance_type=params[1] or None,
                backend=params[2],
            )
        st.session_state[RECOMMEND_KEY] = (params, result)
    words = ["gco", "capacity", "recommend-region", "--gpu-count", str(gpus)]
    if params[1]:
        words += ["-i", params[1]]
    if params[2] != "eks":
        words += ["--backend", params[2]]
    stored = st.session_state.get(RECOMMEND_KEY)
    if not stored or stored[0] != params or not isinstance(stored[1], dict):
        st.caption(
            "Scores each Region on its SQS backlog, GPU allocation and active jobs; with an "
            "instance type, also on its Spot placement score, Spot discount and Capacity Block trend."
        )
        ui.cli_hint(ui.shell_words(*words))
        return
    result = stored[1]
    st.success(
        f"**{region_label(str(result.get('region')))}** · {result.get('reason', '')}",
        icon=":material/near_me:",
    )
    if result.get("telemetry_errors"):
        st.caption(
            ":material/info: Partial telemetry: " + "; ".join(map(str, result["telemetry_errors"]))
        )
    ranking = frames.recommendation_frame(result)
    if not ranking.empty:
        chart, table = st.columns([2, 3], gap="large")
        chart.altair_chart(charts.region_scores(ranking), width="stretch")
        table.dataframe(
            ranking.drop(columns=["recommended"]),
            hide_index=True,
            width="stretch",
            column_config={
                "score": st.column_config.NumberColumn("Score", format="%.3f"),
                "gpu_utilization": st.column_config.NumberColumn("GPU %", format="%.0f"),
                "spot_placement": st.column_config.ProgressColumn(
                    "Spot placement", min_value=0, max_value=1
                ),
                "spot_price_ratio": st.column_config.NumberColumn(
                    "Spot / On-Demand", format="%.2f"
                ),
            },
        )
    ui.cli_hint(ui.shell_words(*words))


def _explorer(topology: Topology, regions: tuple[str, ...]) -> None:
    controls = st.container(horizontal=True, vertical_alignment="bottom", gap="medium")
    instance_type = str(
        controls.selectbox(
            "Instance type",
            list(INSTANCE_TYPES),
            accept_new_options=True,
            key="explore_type",
            width=300,
        )
        or INSTANCE_TYPES[0]
    )
    days = int(
        controls.segmented_control(
            "Spot history",
            [1, 7, 14, 30],
            default=7,
            required=True,
            format_func=lambda d: f"{d} d",
            key="explore_days",
        )
        or 7
    )
    reads = ui.attempt(ui.load_instance_report, instance_type, regions)
    if reads is None:
        return
    report = frames.instance_report_frame(reads)
    offered = report[report["offered"] == True]  # noqa: E712 (pandas mask)
    if offered.empty:
        st.warning(
            f"{instance_type} is not offered in the selected Regions.", icon=":material/block:"
        )
        return
    first = offered.iloc[0]
    with st.container(horizontal=True, gap="small"):
        st.metric("vCPUs", int(first["vcpus"] or 0), border=True, height="stretch")
        st.metric(
            "Memory", f"{float(first['memory_gib'] or 0):.0f} GiB", border=True, height="stretch"
        )
        st.metric(
            "Accelerators",
            f"{int(first['gpus'] or 0)} {first['gpu_type'] or ''}".strip(),
            border=True,
            height="stretch",
        )
        spot = offered.dropna(subset=["spot"])
        if not spot.empty:
            cheapest = spot.loc[spot["spot"].idxmin()]
            st.metric(
                "Cheapest Spot",
                f"${float(cheapest['spot']):.4f}/h",
                border=True,
                height="stretch",
                delta=region_label(str(cheapest["region"])),
                delta_color="off",
                delta_arrow="off",
            )
            best = spot.loc[spot["savings"].idxmax()] if spot["savings"].notna().any() else None
            if best is not None:
                st.metric(
                    "Best Spot discount",
                    f"{float(best['savings']):.0%}",
                    border=True,
                    height="stretch",
                    delta=region_label(str(best["region"])),
                    delta_color="off",
                    delta_arrow="off",
                )
    prices, table = st.columns([2, 3], gap="large")
    with prices:
        st.markdown("**On-Demand against Spot**")
        st.altair_chart(charts.price_comparison(offered), width="stretch")
    with table:
        st.markdown("**By Region**")
        st.dataframe(
            report.drop(columns=["vcpus", "memory_gib", "gpus", "gpu_type"]),
            hide_index=True,
            width="stretch",
            column_config={
                "region": st.column_config.TextColumn("Region"),
                "city": st.column_config.TextColumn("City"),
                "offered": st.column_config.CheckboxColumn("Offered"),
                "error": st.column_config.TextColumn("Error"),
                "on_demand": st.column_config.NumberColumn("On-Demand", format="$%.4f"),
                "spot": st.column_config.NumberColumn("Spot (lowest AZ)", format="$%.4f"),
                "savings": st.column_config.ProgressColumn(
                    "Discount", min_value=0, max_value=1, format="percent"
                ),
                "stability": st.column_config.ProgressColumn(
                    "Price stability",
                    min_value=0,
                    max_value=1,
                    help="1 is a flat price; lower means it moved more over the last week",
                ),
            },
        )

    st.markdown(f"**Spot price, last {days} days**")
    region = st.segmented_control(
        "Region",
        list(offered["region"]),
        format_func=region_label,
        default=str(offered.iloc[0]["region"]),
        required=True,
        key="explore_region",
        label_visibility="collapsed",
    )
    history = ui.attempt(ui.load_spot_history, instance_type, (str(region),), days)
    if history:
        st.altair_chart(charts.spot_history(frames.spot_frame(history)), width="stretch")
        st.caption("Click a zone in the legend to highlight it.")
    else:
        st.caption("No Spot price changes recorded in this window.")

    st.markdown("**Spot placement scores**")
    score_controls = st.container(horizontal=True, vertical_alignment="bottom")
    count = int(score_controls.number_input("Instances", 1, 500, 1, key="explore_count", width=120))
    if score_controls.button("Check scores", icon=":material/speed:", key="explore_scores"):
        scores: dict[str, Any] = {}
        with st.spinner("Asking EC2..."):
            for target in offered["region"]:
                scores[str(target)] = ui.attempt(
                    ui.load_placement_score, instance_type, str(target), count
                )
        st.session_state[SCORES_KEY] = ((instance_type, count), scores)
    score_controls.caption(
        "How likely EC2 can fill a Spot request of this size, 1 (unlikely) to 10. "
        "AWS limits distinct queries per account per day."
    )
    stored = st.session_state.get(SCORES_KEY)
    if stored and stored[0] == (instance_type, count):
        with st.container(horizontal=True, gap="small"):
            for target, score in stored[1].items():
                st.metric(
                    region_label(target), "—" if score is None else f"{score} / 10", border=True
                )
    ui.cli_hint(
        ui.shell_words("gco", "capacity", "instance-info", instance_type),
        ui.shell_words(
            "gco",
            "capacity",
            "spot-prices",
            "-i",
            instance_type,
            "-r",
            str(region),
            "-d",
            str(days),
        ),
    )


def render() -> None:
    topology = ui.load_topology()
    regions = ui.selected_regions(topology)
    ui.page_header(
        "Capacity",
        "Each Region's load, where new work would land, and what an instance type costs where",
        icon=":material/memory:",
    )
    st.subheader("Load right now", anchor=False)
    _load(ui.attempt(ui.load_fleet_status), regions)
    st.subheader("Where would work land?", anchor=False)
    _recommend()
    st.subheader("Instance explorer", anchor=False)
    _explorer(topology, regions)
