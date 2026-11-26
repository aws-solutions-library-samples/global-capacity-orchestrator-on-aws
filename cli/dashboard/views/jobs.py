"""Jobs: every Kubernetes Job in every Region, filtered, charted, inspected and acted on."""

from __future__ import annotations

import contextlib
from typing import Any

import streamlit as st

from cli.dashboard import brand, charts, frames, submit, ui
from cli.dashboard.data import Topology
from cli.dashboard.regions import region_label
from cli.jobs import parse_job_info

SEARCH_KEY = "jobs_search"
STATUS_KEY = "jobs_status"
NAMESPACE_KEY = "jobs_namespace"
SOURCE_KEY = "jobs_source"
TABLE_KEY = "jobs_table"
CHART_KEY = "jobs_status_chart"
TAB_KEY = "jobs_detail_tab"
#: The bulk cleanup's last answer: ``(params, dry_run, answer)`` for the
#: preview, or for the delete that followed it.
CLEANUP_KEY = "jobs_cleanup"
STATUSES = ("pending", "running", "succeeded", "failed")
SOURCES = {"all": "All", "direct": "Submitted directly", "central queue": "From the queue"}


def _filters(jobs: Any) -> Any:
    """The filter bar; returns the rows it lets through."""
    with st.container(horizontal=True, vertical_alignment="bottom", gap="medium"):
        text = st.text_input(
            "Search",
            key=SEARCH_KEY,
            type="search",
            live=True,
            placeholder="Search names and labels",
            label_visibility="collapsed",
            width=300,
        )
        statuses = st.pills(
            "Status",
            list(STATUSES),
            selection_mode="multi",
            key=STATUS_KEY,
            label_visibility="collapsed",
        )
        namespaces = sorted(n for n in jobs["namespace"].unique() if n)
        chosen_namespaces: list[str] = []
        if len(namespaces) > 1:
            chosen_namespaces = st.multiselect(
                "Namespace",
                namespaces,
                key=NAMESPACE_KEY,
                placeholder="Every namespace",
                label_visibility="collapsed",
                width=240,
            )
        source = st.segmented_control(
            "Source",
            list(SOURCES),
            format_func=lambda k: SOURCES[k],
            default="all",
            required=True,
            key=SOURCE_KEY,
            label_visibility="collapsed",
        )
    rows = frames.filter_jobs(
        jobs, statuses=statuses or (), namespaces=chosen_namespaces, text=text or ""
    )
    if source and source != "all":
        rows = rows[rows["source"] == source].reset_index(drop=True)
    return rows


def _kpis(shown: Any, total: int) -> None:
    counts = shown["status"].value_counts()
    with st.container(horizontal=True, gap="small"):
        st.metric(
            "Jobs",
            f"{len(shown)}",
            border=True,
            height="stretch",
            help=f"{total} Jobs in the selected Regions before filtering",
        )
        for status, icon in (
            ("running", ":material/play_circle:"),
            ("pending", ":material/hourglass_top:"),
            ("succeeded", ":material/check_circle:"),
            ("failed", ":material/error:"),
        ):
            st.metric(
                f"{icon} {status.capitalize()}",
                int(counts.get(status, 0)),
                border=True,
                height="stretch",
            )


def _job_namespaces(topology: Topology) -> list[str]:
    """The namespaces the fleet's Jobs run in, from the cached jobs list."""
    snapshot = None
    # The page reports a failed read below; the cleanup form only offers less.
    with contextlib.suppress(Exception):
        snapshot = ui.load_jobs(topology.workload_regions)
    return sorted({job.namespace for job in snapshot.jobs if job.namespace}) if snapshot else []


def _cleanup(topology: Topology) -> None:
    with st.popover("Clean up", icon=":material/cleaning_services:"):
        st.markdown("**Delete finished Jobs in every Region**")
        status = st.segmented_control(
            "Status",
            ["succeeded", "failed"],
            default="succeeded",
            required=True,
            key="cleanup_status",
        )
        older = int(
            st.number_input(
                "Older than (days)",
                min_value=0,
                max_value=365,
                value=0,
                key="cleanup_days",
                help="0 matches any age.",
            )
        )
        # A namespace the fleet's Jobs run in, not free text: one with no Jobs
        # has nothing to delete, and a typed name would reach the API's URL.
        namespace = st.selectbox(
            "Namespace",
            ["", *_job_namespaces(topology)],
            format_func=lambda name: name or "Every allowed namespace",
            key="cleanup_namespace",
        )
        params = (str(status), older or None, namespace or None)
        words = ["gco", "jobs", "bulk-delete", "--all-regions", "-s", str(status)]
        if older:
            words += ["-d", str(older)]
        if namespace:
            words += ["-n", namespace]
        st.code(ui.shell_words(*words, "--execute", "-y"), language="bash", wrap_lines=True)
        if st.button("Preview", icon=":material/preview:", key="cleanup_preview"):
            preview = ui.attempt(
                ui.fleet().cleanup_jobs,
                status=params[0],
                older_than_days=params[1],
                namespace=params[2],
                dry_run=True,
            )
            st.session_state[CLEANUP_KEY] = (params, True, preview)
        stored = st.session_state.get(CLEANUP_KEY)
        if not stored or stored[0] != params or not isinstance(stored[2], dict):
            return
        _, dry_run, answer = stored
        if dry_run:
            count = int(answer.get("total_matched", 0) or 0)
            st.write(f"{ui.plural(count, 'Job')} match.")
        else:
            count = int(answer.get("total_deleted", 0) or 0)
            st.write(f"Deleted {ui.plural(count, 'Job')}.")
        for error in answer.get("errors") or []:
            text = (
                f"{error.get('region')}: {error.get('error')}"
                if isinstance(error, dict)
                else str(error)
            )
            st.warning(text, icon=":material/warning:")
        for region, failed in _failed(answer):
            st.warning(
                f"{region}: {ui.plural(failed, 'Job')} could not be deleted.",
                icon=":material/warning:",
            )
        if not (dry_run and count):
            return
        if st.button(
            f"Delete {ui.plural(count, 'Job')}",
            type="primary",
            icon=":material/delete:",
            key="cleanup_go",
        ):
            _delete_finished(params)


def _failed(answer: dict[str, Any]) -> list[tuple[str, int]]:
    """``(region, count)`` for each Region whose delete left matched Jobs undeleted."""
    counts = [
        (str(result.get("region")), int(result.get("failed") or 0))
        for result in answer.get("region_results") or []
        if isinstance(result, dict)
    ]
    return [(region, count) for region, count in counts if count > 0]


def _delete_finished(params: tuple[str, int | None, str | None]) -> None:
    """Run the delete the preview offered, then report what the delete did.

    Jobs can finish or go between the preview and the delete, a Region can
    fail to answer and a Job can fail to delete, so the toast counts what the
    delete's own answer says it deleted and failed to (``ui.act`` fixes its
    message before the delete runs). The answer takes the preview's place in
    the popover, so the Regions and Jobs the delete missed stay on screen
    after the rerun.
    """
    with st.spinner("Deleting..."):
        done = ui.attempt(
            ui.fleet().cleanup_jobs,
            status=params[0],
            older_than_days=params[1],
            namespace=params[2],
            dry_run=False,
        )
    if done is None:
        return
    ui.refresh()
    deleted = f"Deleted {ui.plural(int(done.get('total_deleted', 0) or 0), 'finished Job')}"
    failed = sum(count for _, count in _failed(done))
    if failed:
        ui.flash(f"{deleted}; {failed} could not be deleted", icon=":material/warning:")
    else:
        ui.flash(deleted)
    st.session_state[CLEANUP_KEY] = (params, False, done)
    st.rerun()


def _table(rows: Any) -> dict[str, Any] | None:
    """The jobs table; returns the selected row, if any."""
    display = rows.assign(status=rows["status"].map(lambda s: [s]))
    event = st.dataframe(
        display,
        hide_index=True,
        width="stretch",
        height=min(38 + 35 * max(len(display), 1), 460),
        on_select="rerun",
        selection_mode="single-row",
        key=TABLE_KEY,
        column_order=[
            "name",
            "region",
            "namespace",
            "status",
            "progress",
            "created",
            "runtime_s",
            "team",
            "source",
            "labels",
        ],
        column_config={
            "name": st.column_config.TextColumn("Job", pinned=True),
            "region": st.column_config.TextColumn("Region"),
            "namespace": st.column_config.TextColumn("Namespace"),
            "status": ui.status_column(),
            "progress": st.column_config.ProgressColumn(
                "Completions", min_value=0, max_value=1, format="percent"
            ),
            "created": st.column_config.DatetimeColumn("Submitted", format="distance"),
            "runtime_s": st.column_config.NumberColumn("Runtime", format="duration"),
            "team": st.column_config.TextColumn("Team"),
            "source": st.column_config.TextColumn("Source"),
            "labels": st.column_config.ListColumn("Labels"),
        },
    )
    index = ui.selected_row(event, len(rows))
    return None if index is None else dict(rows.iloc[index])


#: Job conditions worth showing; the controller's bookkeeping ones
#: (FailureTarget, SuccessCriteriaMet) repeat what these already say.
SHOWN_CONDITIONS = ("Complete", "Failed", "Suspended")


def _overview(job: dict[str, Any]) -> None:
    detail = ui.attempt(ui.load_job_detail, job["name"], job["namespace"], job["region"])
    if detail is None:
        return
    status = detail.get("status") or {}
    scheduling = detail.get("scheduling") or {}
    # The detail record carries each condition's transition time, so a failed
    # Job's runtime is known here even where the list record lacks it.
    info = parse_job_info(detail, job["region"])
    with st.container(horizontal=True, gap="small"):
        st.metric("Active pods", int(status.get("active") or 0), border=True, height="stretch")
        st.metric("Succeeded", int(status.get("succeeded") or 0), border=True, height="stretch")
        st.metric("Failed", int(status.get("failed") or 0), border=True, height="stretch")
        runtime = info.duration_seconds
        st.metric(
            "Runtime",
            "—" if runtime is None else _duration(float(runtime)),
            border=True,
            height="stretch",
        )
    for condition in status.get("conditions") or []:
        if not isinstance(condition, dict) or condition.get("status") != "True":
            continue
        kind = condition.get("type")
        if kind not in SHOWN_CONDITIONS:
            continue
        text = f"**{kind}**: {condition.get('reason') or ''}. {condition.get('message') or ''}"
        if kind == "Failed":
            st.error(text, icon=":material/error:")
        else:
            st.caption(text)
    node_type = scheduling.get("node_instance_type")
    if node_type:
        labels = scheduling.get("node_labels") or {}
        verb = "Ran" if job["status"] in ("succeeded", "failed") else "Running"
        st.markdown(
            f":material/dns: {verb} on **{node_type}** "
            f"({scheduling.get('node_capacity_type') or 'unknown capacity'}) in "
            f"`{labels.get('topology.kubernetes.io/zone', '?')}`, NodePool "
            f"`{labels.get('karpenter.sh/nodepool', '?')}`"
        )
    for pod in scheduling.get("unscheduled") or []:
        st.warning(
            f"Pod `{pod.get('pod') or pod.get('name')}` is not scheduled: "
            f"{pod.get('reason') or 'Pending'} {pod.get('message') or ''}",
            icon=":material/pending:",
        )
    if scheduling.get("node_lookup_error"):
        st.caption(f":material/info: Node details unavailable: {scheduling['node_lookup_error']}")
    images = sorted(
        {
            str(c.get("image"))
            for c in ((detail.get("spec") or {}).get("template") or {})
            .get("spec", {})
            .get("containers")
            or []
            if isinstance(c, dict) and c.get("image")
        }
    )
    if images:
        st.markdown("**Images** " + " ".join(f"`{image}`" for image in images))
    labels = (detail.get("metadata") or {}).get("labels") or {}
    if labels:
        st.markdown(
            "**Labels** " + " ".join(f":gray-badge[{k}={v}]" for k, v in sorted(labels.items()))
        )


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def _pods(job: dict[str, Any]) -> list[str]:
    payload = ui.attempt(ui.load_job_pods, job["name"], job["namespace"], job["region"])
    if payload is None:
        return []
    pods = frames.pods_frame(payload)
    if pods.empty:
        st.info("No pods: the Job has not started one yet, or they were cleaned up.")
        return []
    st.dataframe(
        pods,
        hide_index=True,
        width="stretch",
        column_config={
            "pod": st.column_config.TextColumn("Pod", pinned=True),
            "phase": st.column_config.TextColumn("Phase"),
            "node": st.column_config.TextColumn("Node"),
            "instance_type": st.column_config.TextColumn("Instance"),
            "capacity_type": st.column_config.TextColumn("Capacity"),
            "zone": st.column_config.TextColumn("Zone"),
            "started": st.column_config.DatetimeColumn("Started", format="distance"),
            "restarts": st.column_config.NumberColumn("Restarts"),
            "ready": st.column_config.CheckboxColumn("Ready"),
            "containers": st.column_config.ListColumn("Containers"),
        },
    )
    return sorted({c for names in pods["containers"] for c in names})


def _events(job: dict[str, Any]) -> None:
    payload = ui.attempt(ui.load_job_events, job["name"], job["namespace"], job["region"])
    if payload is None:
        return
    events = frames.events_frame(payload)
    if events.empty:
        st.info("No events recorded for this Job, its pods or their nodes.")
        return
    warnings = int((events["type"] == "Warning").sum())
    st.caption(
        f"{ui.plural(len(events), 'event')} about the Job, its pods, nodes and NodeClaims"
        + (f" · :red[{ui.plural(warnings, 'warning')}]" if warnings else "")
    )
    st.altair_chart(charts.events_timeline(events), width="stretch")
    st.dataframe(
        events,
        hide_index=True,
        width="stretch",
        column_config={
            "time": st.column_config.DatetimeColumn("When", format="distance"),
            "message": st.column_config.TextColumn("Message", width="large"),
        },
    )


def _logs(job: dict[str, Any], containers: list[str]) -> None:
    controls = st.container(horizontal=True, vertical_alignment="bottom")
    container = None
    if len(containers) > 1:
        container = controls.selectbox("Container", containers, key="jobs_log_container", width=220)
    tail = int(
        controls.select_slider(
            "Last lines",
            options=[50, 100, 200, 500, 1000, 2000],
            value=200,
            key="jobs_log_tail",
            width=320,
        )
    )
    follow = controls.toggle(
        "Follow", key="jobs_log_follow", help="Re-read the logs every 5 seconds"
    )

    @st.fragment(run_every=5 if follow else None)
    def view() -> None:
        text = ui.attempt(
            ui.load_job_logs, job["name"], job["namespace"], job["region"], tail, container
        )
        if text is None:
            return
        st.code(text or "(no output yet)", language=None, wrap_lines=True, height=420)
        st.download_button(
            "Download",
            text or "",
            file_name=f"{job['name']}-{job['region']}.log",
            mime="text/plain",
            icon=":material/download:",
            on_click="ignore",
            key="jobs_log_download",
        )

    view()


def _metrics(job: dict[str, Any]) -> None:
    payload = ui.attempt(ui.load_job_metrics, job["name"], job["namespace"], job["region"])
    if payload is None:
        return
    usage = frames.container_metrics_frame(payload)
    if usage.empty:
        st.info("No usage samples: metrics-server reports only running containers.")
        return
    summary = payload.get("summary") or {}
    with st.container(horizontal=True, gap="small"):
        st.metric("CPU", f"{int(summary.get('total_cpu_millicores') or 0)} m", border=True)
        st.metric("Memory", f"{float(summary.get('total_memory_mib') or 0):.1f} MiB", border=True)
        st.metric("Pods sampled", int(summary.get("pod_count") or 0), border=True)
    st.altair_chart(charts.container_usage(usage), width="stretch")


def _detail(job: dict[str, Any]) -> None:
    name, namespace, region = job["name"], job["namespace"], job["region"]
    with st.container(border=True):
        actions = ui.detail_header(
            name,
            f"{brand.status_badge(job['status'])} :gray-badge[{region_label(region)}] "
            f":gray-badge[{namespace}]",
        )
        if job["status"] == "failed" and actions.button(
            "Retry", icon=":material/replay:", key="jobs_retry"
        ):
            result = ui.act(
                "Retrying...",
                lambda: ui.fleet().retry_job(name, namespace, region),
                success=f"Retried {name}",
            )
            if result is not None:
                ui.flash(
                    f"The retry is {result.get('new_job', 'submitted')}", icon=":material/replay:"
                )
                st.rerun()
        if actions.button("Delete", icon=":material/delete:", key="jobs_delete"):
            ui.confirm(
                f"Delete Job **{name}** in **{region}** ({namespace})? Its pods are deleted too.",
                label="Delete",
                action=lambda: ui.fleet().delete_job(
                    name, namespace, region, uid=job.get("uid") or None
                ),
                success=f"Deleted {name}",
            )
        overview, pods, events, logs, metrics, cli = st.tabs(
            ["Overview", "Pods", "Events", "Logs", "Metrics", "CLI"], key=TAB_KEY, on_change="rerun"
        )
        containers: list[str] = []
        if overview.open:
            with overview:
                _overview(job)
        if pods.open or logs.open:
            with pods:
                containers = _pods(job)
        if events.open:
            with events:
                _events(job)
        if logs.open:
            with logs:
                _logs(job, containers)
        if metrics.open:
            with metrics:
                _metrics(job)
        if cli.open:
            with cli:
                ui.cli_hint(
                    ui.shell_words("gco", "jobs", "get", name, "-n", namespace, "-r", region),
                    ui.shell_words(
                        "gco", "jobs", "logs", name, "-n", namespace, "-r", region, "--tail", "200"
                    ),
                    ui.shell_words("gco", "jobs", "events", name, "-n", namespace, "-r", region),
                    ui.shell_words("gco", "jobs", "delete", name, "-n", namespace, "-r", region),
                )


def render() -> None:
    topology: Topology = ui.load_topology()
    regions = ui.selected_regions(topology)
    ui.page_header(
        "Jobs",
        f"Kubernetes Jobs in {ui.plural(len(regions), 'Region')}",
        icon=":material/work:",
    )
    with st.container(horizontal=True, horizontal_alignment="right"):
        _cleanup(topology)
        if st.button("Submit job", type="primary", icon=":material/add:", key="jobs_submit"):
            submit.submit_dialog("region")

    snapshot = ui.attempt(ui.load_jobs, regions)
    if snapshot is None:
        return
    for region, error in snapshot.errors.items():
        st.warning(f"Jobs in {region} could not be read: {error}", icon=":material/warning:")
    jobs = frames.filter_jobs(frames.jobs_frame(snapshot.jobs), regions=regions)
    if jobs.empty:
        st.info(
            "No Kubernetes Jobs in the selected Regions yet. Submit one with the button above.",
            icon=":material/work_off:",
        )
        return

    rows = _filters(jobs)
    _kpis(rows, len(jobs))

    by_region, timeline = st.columns([2, 3], gap="large")
    with by_region:
        st.markdown("**By Region and status**")
        event = st.altair_chart(
            charts.status_by_region(frames.status_counts(rows)),
            width="stretch",
            on_select="rerun",
            selection_mode=[charts.STATUS_PICK],
            key=CHART_KEY,
        )
        picks = (getattr(event, "selection", None) or {}).get(charts.STATUS_PICK) or []
        st.caption("Click a bar segment to show only those Jobs; click empty space to clear.")
    with timeline:
        st.markdown("**When they ran**")
        if rows["started"].notna().any():
            st.altair_chart(charts.job_timeline(rows), width="stretch")
        else:
            st.caption("None of these Jobs has started yet.")

    rows = frames.filter_pairs(rows, picks)
    if snapshot.total > len(snapshot.jobs):
        st.caption(f"Showing the newest {len(snapshot.jobs)} of {snapshot.total} Jobs.")
    selected = _table(rows)
    if selected is None:
        st.caption(":material/touch_app: Select a row for its pods, events, logs and usage.")
        return
    _detail(selected)
