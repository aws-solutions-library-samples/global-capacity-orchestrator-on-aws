"""Queue: the central job queue (DynamoDB) and each Region's SQS queue."""

from __future__ import annotations

import functools
from typing import Any

import streamlit as st
import yaml

from cli.dashboard import brand, charts, frames, submit, ui
from cli.dashboard.data import Topology
from cli.dashboard.regions import region_label

STATUS_KEY = "queue_status"
TABLE_KEY = "queue_table"
QUEUE_STATUSES = (
    "queued",
    "claimed",
    "applying",
    "pending",
    "running",
    "succeeded",
    "failed",
    "cancelled",
)
IN_PROGRESS = ("claimed", "applying", "pending", "running")


def _kpis(stats: Any) -> None:
    totals = stats.groupby("status")["jobs"].sum() if not stats.empty else {}

    def count(*statuses: str) -> int:
        return int(sum(int(totals.get(s, 0)) for s in statuses)) if len(totals) else 0

    with st.container(horizontal=True, gap="small"):
        st.metric(
            "Records",
            count(*QUEUE_STATUSES),
            border=True,
            height="stretch",
            help="Central-queue records in the selected Regions",
        )
        st.metric(
            ":material/hourglass_top: Waiting",
            count("queued"),
            border=True,
            height="stretch",
            help="Queued and not yet claimed by a Region's queue worker",
        )
        st.metric(
            ":material/play_circle: In progress",
            count(*IN_PROGRESS),
            border=True,
            height="stretch",
            help="Claimed by a Region and applied (or being applied) as a Kubernetes Job",
        )
        st.metric(
            ":material/check_circle: Succeeded", count("succeeded"), border=True, height="stretch"
        )
        st.metric(":material/error: Failed", count("failed"), border=True, height="stretch")
        st.metric(":material/block: Cancelled", count("cancelled"), border=True, height="stretch")


def _detail(record: dict[str, Any]) -> None:
    job = ui.attempt(ui.load_queue_job, record["job_id"])
    if not job:
        return
    with st.container(border=True):
        actions = ui.detail_header(
            str(job.get("job_name") or record["name"]),
            f"{brand.status_badge(str(job.get('status', '')))} "
            f":gray-badge[{region_label(str(job.get('target_region', '')))}] "
            f":gray-badge[priority {job.get('priority', 0)}]",
        )
        # Only a record no Region has claimed can be cancelled (the API answers 409 otherwise).
        if job.get("status") == "queued" and actions.button(
            "Cancel", icon=":material/cancel:", key="queue_cancel"
        ):
            job_id = str(job["job_id"])
            ui.confirm(
                f"Cancel queued job **{job.get('job_name')}**? No Region has claimed it yet, "
                "so nothing is running.",
                label="Cancel job",
                action=lambda: ui.fleet().queue_cancel(
                    job_id, reason="cancelled from the dashboard"
                ),
                success=f"Cancelled {job.get('job_name')}",
                icon=":material/cancel:",
            )
        if job.get("error_message"):
            st.error(str(job["error_message"]), icon=":material/error:")
        claimer = frames.claimed_by(job)
        facts = [
            ("Job ID", f"`{job.get('job_id')}`"),
            ("Namespace", f"`{job.get('namespace')}`"),
            (
                "Kubernetes Job",
                f"`{job.get('k8s_job_name')}`" if job.get("k8s_job_name") else "not created yet",
            ),
            ("Claimed by", f"the {claimer} queue worker" if claimer else "no Region yet"),
        ]
        if job.get("spot_max_price"):
            facts.append(
                (
                    "Spot gate",
                    f"{job.get('spot_instance_type')} at or under ${job.get('spot_max_price')}/h",
                )
            )
        st.markdown("  \n".join(f"**{label}** {value}" for label, value in facts))
        history = frames.status_history_frame(job)
        journey, manifest = st.tabs(["Journey", "Manifest"])
        with journey:
            if history.empty:
                st.caption("No status history recorded.")
            else:
                st.altair_chart(charts.status_history(history), width="stretch")
                st.dataframe(
                    history.drop(columns=["until"]),
                    column_order=["status", "timestamp", "seconds", "message"],
                    hide_index=True,
                    width="stretch",
                    column_config={
                        "status": ui.status_column(),
                        "timestamp": st.column_config.DatetimeColumn(
                            "When", format="YYYY-MM-DD HH:mm:ss"
                        ),
                        "seconds": st.column_config.NumberColumn("For", format="duration"),
                        "message": st.column_config.TextColumn("Message", width="large"),
                    },
                )
        with manifest:
            document = job.get("manifest")
            if isinstance(document, dict):
                st.code(yaml.safe_dump(document, sort_keys=False), language="yaml")
            else:
                st.caption("The record carries no manifest.")
        ui.cli_hint(ui.shell_words("gco", "queue", "get", str(job.get("job_id"))))


def render() -> None:
    topology: Topology = ui.load_topology()
    regions = ui.selected_regions(topology)
    ui.page_header(
        "Queue",
        "The central job queue in DynamoDB, and each Region's SQS job queue",
        icon=":material/queue:",
    )
    with st.container(horizontal=True, horizontal_alignment="right"):
        if st.button("Queue a job", type="primary", icon=":material/add:", key="queue_submit"):
            submit.submit_dialog("queue")

    ui.prefetch(
        ui.load_queue_stats,
        functools.partial(ui.load_queue_jobs, None, None),
        ui.load_fleet_status,
    )
    stats = frames.queue_stats_frame(ui.attempt(ui.load_queue_stats) or {})
    stats = stats[stats["region"].isin(regions)].reset_index(drop=True)
    _kpis(stats)

    central, sqs = st.columns(2, gap="large")
    with central:
        st.markdown("**Central queue by Region and status**")
        if stats.empty:
            st.caption("The central queue holds no records for these Regions.")
        else:
            st.altair_chart(charts.queue_heatmap(stats), width="stretch")
    with sqs:
        st.markdown("**Regional SQS queues**")
        depth = frames.sqs_frame(ui.attempt(ui.load_fleet_status))
        depth = depth[depth["region"].isin(regions)].reset_index(drop=True)
        if depth.empty:
            st.caption("No SQS readings (the status document could not read them).")
        else:
            st.altair_chart(charts.sqs_depth(depth), width="stretch")
            if int(depth["dlq"].sum()):
                st.warning(
                    f"{int(depth['dlq'].sum())} messages are in dead-letter queues: "
                    "`gco jobs queue-status --all-regions` shows which.",
                    icon=":material/report:",
                )

    listing = ui.attempt(ui.load_queue_jobs, None, None)
    if listing is None:
        return
    records = frames.queue_frame(listing.get("jobs") or [])
    records = records[records["region"].isin(regions)].reset_index(drop=True)
    if records.empty:
        st.info(
            "No central-queue records. Queue one with the button above.", icon=":material/inbox:"
        )
        return
    statuses = st.pills(
        "Status",
        [s for s in QUEUE_STATUSES if s in set(records["status"])],
        selection_mode="multi",
        key=STATUS_KEY,
        label_visibility="collapsed",
    )
    if statuses:
        records = records[records["status"].isin(statuses)].reset_index(drop=True)
    if listing.get("next_cursor"):
        st.caption(
            f"Showing the newest {len(records)} records; `gco queue list` pages through the rest."
        )
    display = records.assign(status=records["status"].map(lambda s: [s]))
    event = st.dataframe(
        display,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=TABLE_KEY,
        column_order=[
            "name",
            "region",
            "status",
            "priority",
            "submitted",
            "updated",
            "k8s_job",
            "spot_gate",
            "error",
        ],
        column_config={
            "name": st.column_config.TextColumn("Job", pinned=True),
            "region": st.column_config.TextColumn("Target Region"),
            "status": ui.status_column(),
            "priority": st.column_config.ProgressColumn(
                "Priority", min_value=0, max_value=100, format="%d"
            ),
            "submitted": st.column_config.DatetimeColumn("Submitted", format="distance"),
            "updated": st.column_config.DatetimeColumn("Updated", format="distance"),
            "k8s_job": st.column_config.TextColumn("Kubernetes Job"),
            "spot_gate": st.column_config.TextColumn("Spot gate"),
            "error": st.column_config.TextColumn("Error"),
        },
    )
    index = ui.selected_row(event, len(records))
    if index is not None:
        _detail(dict(records.iloc[index]))
    else:
        st.caption(":material/touch_app: Select a record for its journey through the queue.")
