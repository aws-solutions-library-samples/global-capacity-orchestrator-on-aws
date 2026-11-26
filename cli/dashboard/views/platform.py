"""Platform: stacks and add-ons, services, job policy, templates, webhooks and the request path."""

from __future__ import annotations

import functools
from typing import Any

import streamlit as st
import yaml

from cli.dashboard import charts, frames, ui
from cli.dashboard.data import DashboardError, Topology, load_manifest_text
from cli.dashboard.regions import region_label

WEBHOOK_EVENTS = ("job.completed", "job.failed", "job.started")


def _stacks(fleet: dict[str, Any] | None) -> None:
    stacks = frames.stacks_frame(fleet)
    if stacks.empty:
        st.caption("The status document lists no stacks.")
        return
    # A stack the status document could not read is not known to be deployed
    # either, so it counts as neither and is named below the counts.
    absent = ["not deployed", "unknown"]
    expected = stacks[stacks["role"] == "expected"]
    deployed = int((~expected["status"].isin(absent)).sum())
    healthy = int((expected["health"] == "healthy").sum())
    with st.container(horizontal=True, gap="small"):
        st.metric(
            "Expected stacks deployed",
            f"{deployed} / {len(expected)}",
            border=True,
            height="stretch",
        )
        st.metric("Healthy", f"{healthy} / {len(expected)}", border=True, height="stretch")
        optional = stacks[stacks["role"] == "optional"]
        st.metric(
            "Optional stacks",
            int((~optional["status"].isin(absent)).sum()),
            border=True,
            height="stretch",
            help="API bridges, analytics and other stacks a feature adds",
        )
    unread = stacks.loc[stacks["health"] == "unknown", "stack"].tolist()
    if unread:
        st.caption(
            f":material/warning: {ui.plural(len(unread), 'stack')} could not be read: "
            f"{', '.join(unread)}."
        )
    st.dataframe(
        stacks.assign(health=stacks["health"].map(lambda h: [h] if h else [])),
        hide_index=True,
        width="stretch",
        height=ui.table_height(len(stacks)),
        column_config={
            "stack": st.column_config.TextColumn("Stack", pinned=True),
            "region": st.column_config.TextColumn("Region"),
            "status": st.column_config.TextColumn("CloudFormation status"),
            "health": ui.health_column(),
            "updated": st.column_config.DatetimeColumn("Updated", format="distance"),
            "role": st.column_config.TextColumn(
                "Role",
                help="Expected stacks are always deployed; optional ones come with a feature",
            ),
        },
    )
    topology = ui.load_topology()
    ui.cli_hint(
        "gco stacks list",
        ui.shell_words(
            "gco", "stacks", "status", f"{topology.project}-global", "-r", topology.global_region
        ),
    )


def _addons(fleet: dict[str, Any] | None) -> None:
    """Each Region's add-on convergence, from the status document's ``addons`` section.

    A failed convergence never fails the regional stack, so the stacks above
    read healthy while a Region's base pass or one of its charts failed.
    """
    section = ((fleet or {}).get("sections") or {}).get("addons")
    if not section:
        return
    st.markdown("**Add-ons**")
    # Only a section that could not read every Region carries a reason.
    reason = str(section.get("reason") or "")
    if reason:
        st.caption(f":material/warning: {reason[:1].upper()}{reason[1:]}.")
    for error in section.get("errors") or []:
        st.caption(f":material/warning: {error}")
    waiting = [
        str(region)
        for region, entry in ((section.get("data") or {}).get("by_region") or {}).items()
        if frames.as_dict(entry).get("readable", True) and not frames.as_dict(entry).get("addons")
    ]
    if waiting:
        st.caption(f"No add-on status recorded yet in {', '.join(waiting)}.")
    addons = frames.addons_frame(fleet)
    if not addons.empty:
        st.dataframe(
            addons.assign(health=addons["health"].map(lambda h: [h])),
            hide_index=True,
            width="stretch",
            height=ui.table_height(len(addons)),
            column_config={
                "region": st.column_config.TextColumn("Region", pinned=True),
                "addon": st.column_config.TextColumn(
                    "Add-on",
                    help="A convergence phase (the kubectl applier's passes, the validators) "
                    "or a Helm chart",
                ),
                "status": st.column_config.TextColumn("Status"),
                "health": ui.health_column(),
                "message": st.column_config.TextColumn("Message"),
                "updated": st.column_config.DatetimeColumn("Updated", format="distance"),
            },
        )
    ui.cli_hint("gco stacks addons status --all-regions")


def _services(topology: Topology, regions: tuple[str, ...]) -> None:
    ui.prefetch(
        ui.load_global_health,
        functools.partial(ui.load_regional_status, regions),
        functools.partial(ui.load_cluster_metrics, regions),
    )
    health = ui.attempt(ui.load_global_health)
    statuses = ui.attempt(ui.load_regional_status, regions) or []
    services = frames.services_frame(health, statuses)
    services = services[services["region"].isin(regions)].reset_index(drop=True)
    if health:
        st.markdown(
            f"Cross-Region aggregator: **{health.get('overall_status', 'unknown')}**, "
            f"{health.get('healthy_regions', 0)} of {health.get('total_regions', 0)} Regions healthy."
        )
    st.dataframe(
        services.assign(health=services["health"].map(lambda h: [h])),
        hide_index=True,
        width="stretch",
        column_config={
            "region": st.column_config.TextColumn("Region", pinned=True),
            "health": ui.health_column(),
            "cluster": st.column_config.TextColumn("Cluster"),
            "version": st.column_config.TextColumn("API version"),
            "templates": st.column_config.NumberColumn("Templates"),
            "webhooks": st.column_config.NumberColumn("Webhooks"),
            "error": st.column_config.TextColumn("Error"),
            "processor": st.column_config.CheckboxColumn("Manifest processor"),
            "queue_worker": st.column_config.TextColumn("Queue worker"),
            "last_pass": st.column_config.DatetimeColumn("Last queue pass", format="distance"),
            "worker_error": st.column_config.TextColumn("Worker error"),
        },
    )
    st.markdown("**Health monitor thresholds**")
    for read in ui.attempt(ui.load_cluster_metrics, regions) or []:
        if not read.ok:
            st.caption(f"{read.region}: {read.error}")
            continue
        utilization = read.data.get("resource_utilization") or {}
        thresholds = read.data.get("thresholds") or {}
        violations = read.data.get("threshold_violations") or []
        with st.container(border=True):
            top = st.container(horizontal=True, vertical_alignment="center")
            top.markdown(f"**{region_label(read.region)}**")
            top.markdown(
                ":red-badge[over threshold]" if violations else ":green-badge[within thresholds]"
            )
            cols = st.columns(3)
            for col, key, name in (
                (cols[0], "cpu", "CPU requested"),
                (cols[1], "memory", "Memory requested"),
                (cols[2], "gpu", "GPU allocated"),
            ):
                value = utilization.get(f"{key}_percent")
                limit = thresholds.get(f"{key}_threshold")
                label = f"{name}: {value:.0f}%" if isinstance(value, int | float) else f"{name}: —"
                label += (
                    f" (limit {limit}%)"
                    if isinstance(limit, int | float) and limit >= 0
                    else " (no limit)"
                )
                if (
                    key == "gpu"
                    and utilization.get("gpu_allocatable") == 0
                    and not utilization.get("gpu_requested")
                ):
                    # 0% of no GPUs is not an idle GPU: the Region has none.
                    col.caption(f"{name}: no GPU nodes")
                    continue
                col.progress(min(float(value or 0), 100.0) / 100, text=label)
            for violation in violations:
                st.caption(f":material/warning: {violation}")
    ui.cli_hint("gco jobs health --all-regions", "gco status")


def _policy(regions: tuple[str, ...]) -> None:
    fleet = ui.attempt(ui.load_fleet_status_with_policy)
    section = ((fleet or {}).get("sections") or {}).get("policy") or {}
    data = section.get("data") or {}
    if data:
        compared = [str(region) for region in data.get("compared") or []]
        unreadable = data.get("unreadable") or {}
        # ``agree`` is also true when fewer than two policies were read: one
        # Region cannot disagree with itself, but that is not a comparison.
        if data.get("agree") and len(compared) >= 2 and not unreadable:
            st.success(
                f"Every Region enforces the same job policy ({', '.join(compared)}).",
                icon=":material/verified_user:",
            )
        elif data.get("agree") and len(compared) >= 2:
            # Agreement among the Regions read says nothing of the others,
            # which are named below.
            st.warning(
                f"The Regions whose policy could be read ({', '.join(compared)}) enforce the "
                "same job policy.",
                icon=":material/policy:",
            )
        elif data.get("agree") and compared and not unreadable:
            st.info(
                f"{compared[0]} is the only workload Region, so there is no other policy to "
                "compare with.",
                icon=":material/policy:",
            )
        elif data.get("agree"):
            st.warning(
                (f"Only {compared[0]}'s policy" if compared else "No Region's policy")
                + " could be read, so there is nothing to compare.",
                icon=":material/policy:",
            )
        else:
            st.warning("The Regions enforce different job policies:", icon=":material/policy:")
            for drift in data.get("drift") or []:
                if not isinstance(drift, dict):
                    continue
                values = drift.get("values")
                shown = (
                    "; ".join(f"{region}: {value}" for region, value in values.items())
                    if isinstance(values, dict)
                    else str(values)
                )
                st.markdown(f"- `{drift.get('field')}`: {shown}")
        for region, error in unreadable.items():
            st.caption(f":material/warning: {region}: {error}")
        for region, namespaces in (data.get("enforcement_gaps") or {}).items():
            st.caption(
                f":material/warning: {region}: the ResourceQuota, LimitRange and GPU cap of "
                f"{', '.join(map(str, namespaces))} could not be read, so only the per-manifest "
                "caps below are known to apply."
            )
    reads = ui.attempt(ui.load_regional_policy, regions) or []
    policy = frames.policy_frame(reads)
    st.dataframe(
        policy,
        hide_index=True,
        width="stretch",
        column_config={
            "region": st.column_config.TextColumn("Region", pinned=True),
            "validation": st.column_config.CheckboxColumn("Validation"),
            "namespaces": st.column_config.ListColumn("Namespaces"),
            "kinds": st.column_config.ListColumn("Kinds"),
            "max_cpu": st.column_config.TextColumn("CPU cap"),
            "max_memory": st.column_config.TextColumn("Memory cap"),
            "max_gpus": st.column_config.NumberColumn("GPU cap"),
            "container_gpus": st.column_config.TextColumn(
                "GPUs per container",
                help="The most GPUs one container may ask for, read from the cluster's "
                "admission policy (cdk.json resource_quota.container_max_gpu)",
            ),
            "registries": st.column_config.NumberColumn("Trusted registries"),
            "dockerhub_orgs": st.column_config.ListColumn("Docker Hub orgs"),
            "blocked": st.column_config.ListColumn("Blocked"),
        },
    )
    st.caption(
        "The CPU, memory and GPU caps are per manifest, and GPUs per container is the most one "
        "container may ask for. `gco jobs submit --check-policy` checks a manifest against the "
        "per-manifest caps first."
    )
    ui.cli_hint("gco status --with-policy")


def _template_card(template: dict[str, Any], topology: Topology) -> None:
    """One stored template: its manifest, a form to run it, and delete."""
    name = str(template.get("name"))
    with st.expander(
        f"{name} · {template.get('description') or 'no description'}",
        icon=":material/description:",
    ):
        st.code(yaml.safe_dump(template.get("manifest") or {}, sort_keys=False), language="yaml")
        defaults = template.get("parameters") or {}
        if defaults:
            st.caption("Defaults: " + ", ".join(f"{k}={v}" for k, v in defaults.items()))
        row = st.container(horizontal=True, vertical_alignment="bottom")
        job_name = row.text_input("Job name", key=f"tpl_job_{name}", width=200)
        region = str(
            row.selectbox(
                "Region",
                list(topology.workload_regions),
                format_func=region_label,
                key=f"tpl_region_{name}",
                width=230,
            )
        )
        overrides = row.text_input(
            "Parameters", key=f"tpl_params_{name}", placeholder="image=..., gpus=2"
        )
        if row.button(
            "Run", icon=":material/play_arrow:", key=f"tpl_run_{name}", disabled=not job_name
        ):
            try:
                parameters = ui.parse_pairs(overrides, what="parameter")
            except DashboardError as exc:
                ui.show_error(exc)
                return
            created = ui.act(
                "Creating the Job...",
                lambda: ui.fleet().run_template(
                    name, job_name=job_name, region=region, parameters=parameters
                ),
                success=f"Created {job_name} in {region} from {name}",
            )
            if created is not None:
                st.rerun()
        if row.button("Delete", icon=":material/delete:", key=f"tpl_delete_{name}"):
            ui.confirm(
                f"Delete template **{name}**? Jobs created from it are not affected.",
                label="Delete",
                action=lambda: ui.fleet().delete_template(name),
                success=f"Deleted template {name}",
            )


def _templates(topology: Topology) -> None:
    templates = ui.attempt(ui.load_templates)
    if templates is None:
        return
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("**Job templates**")
        if not templates:
            st.caption("No templates yet.")
        for template in templates:
            _template_card(template, topology)
    with right, st.form("new_template", clear_on_submit=False, border=True):
        st.markdown("**New template**")
        name = st.text_input("Name", max_chars=63)
        description = st.text_input("Description")
        manifest_text = st.text_area(
            "Job manifest",
            height=200,
            placeholder="apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: '{{name}}'\nspec: ...",
            help="{{name}} and any {{parameter}} are filled in when the template runs.",
        )
        defaults_text = st.text_input("Default parameters", placeholder="image=busybox:1.38.0")
        if st.form_submit_button("Create", icon=":material/add:"):
            try:
                manifests = load_manifest_text(manifest_text)
                defaults = ui.parse_pairs(defaults_text, what="parameter")
                if len(manifests) != 1:
                    raise DashboardError("A template holds exactly one manifest.")
            except DashboardError as exc:
                ui.show_error(exc)
            else:
                if (
                    ui.act(
                        "Creating...",
                        lambda: ui.fleet().create_template(
                            name, manifests[0], description=description or None, parameters=defaults
                        ),
                        success=f"Created template {name}",
                    )
                    is not None
                ):
                    st.rerun()


def _webhook_card(hook: dict[str, Any]) -> None:
    """One registered webhook: where it posts, which events, and delete."""
    hook_id = str(hook.get("id") or hook.get("webhook_id"))
    url = str(hook.get("url"))
    with st.container(border=True, horizontal=True, vertical_alignment="center"):
        st.markdown(
            f"`{url}`  \n"
            + " ".join(f":blue-badge[{e}]" for e in hook.get("events") or [])
            + (f" :gray-badge[{hook.get('namespace')}]" if hook.get("namespace") else "")
        )
        if st.button("Delete", icon=":material/delete:", key=f"hook_delete_{hook_id}"):
            ui.confirm(
                f"Stop sending job events to `{url}`?",
                label="Delete",
                action=lambda: ui.fleet().delete_webhook(hook_id),
                success="Webhook deleted",
            )


def _webhooks() -> None:
    hooks = ui.attempt(ui.load_webhooks)
    if hooks is None:
        return
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("**Webhooks**")
        if not hooks:
            st.caption("No webhooks registered.")
        for hook in hooks:
            _webhook_card(hook)
    with right, st.form("new_webhook", border=True):
        st.markdown("**Register a webhook**")
        url = st.text_input("HTTPS URL", placeholder="https://hooks.example.com/gco")
        events = st.multiselect(
            "Events", list(WEBHOOK_EVENTS), default=["job.completed", "job.failed"]
        )
        namespace = st.text_input("Only this namespace", placeholder="every namespace")
        secret = st.text_input(
            "Signing secret", type="password", help="Signs each delivery with HMAC"
        )
        if (
            st.form_submit_button("Register", icon=":material/add_link:")
            and ui.act(
                "Registering...",
                lambda: ui.fleet().create_webhook(
                    url, events, namespace=namespace or None, secret=secret or None
                ),
                success="Webhook registered",
            )
            is not None
        ):
            st.rerun()


def _topology(topology: Topology, regions: tuple[str, ...]) -> None:
    fleet = ui.attempt(ui.load_fleet_status)
    metrics = ui.attempt(ui.load_cluster_metrics, regions) or []
    snapshot = ui.attempt(ui.load_jobs, regions)
    jobs = frames.jobs_frame(snapshot.jobs if snapshot else [])
    rows = frames.regions_frame(
        topology, fleet, metrics, jobs=jobs, endpoints=ui.attempt(ui.load_endpoints) or []
    )
    rows = rows[rows["region"].isin(regions)].reset_index(drop=True)
    st.graphviz_chart(
        charts.topology_dot(
            rows,
            project=topology.project,
            control_region=topology.global_region,
            api_region=topology.api_gateway_region,
            dark=ui.is_dark(),
        ),
        width="stretch",
    )
    st.caption(
        "Every request is signed with your AWS credentials (SigV4) and checked by IAM at the "
        "API Gateway. Regions are coloured by health."
    )


def render() -> None:
    topology = ui.load_topology()
    regions = ui.selected_regions(topology)
    ui.page_header(
        "Platform",
        "The stacks, services and policies behind the fleet",
        icon=":material/dns:",
    )
    tabs = st.tabs(
        ["Stacks", "Services", "Policy", "Templates", "Webhooks", "Topology"],
        key="platform_tab",
        on_change="rerun",
    )
    stacks, services, policy, templates, webhooks, topo = tabs
    if stacks.open:
        with stacks:
            # One read of the status document draws both the stacks and their add-ons.
            fleet = ui.attempt(ui.load_fleet_status)
            _stacks(fleet)
            _addons(fleet)
    if services.open:
        with services:
            _services(topology, regions)
    if policy.open:
        with policy:
            _policy(regions)
    if templates.open:
        with templates:
            _templates(topology)
    if webhooks.open:
        with webhooks:
            _webhooks()
    if topo.open:
        with topo:
            _topology(topology, regions)
