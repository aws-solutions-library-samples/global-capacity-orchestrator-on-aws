"""Console: read any GCO API route, see the answer as JSON or a table, and download it."""

from __future__ import annotations

import json
import time
from typing import Any

import streamlit as st

from cli.dashboard import frames, ui
from cli.dashboard.data import CONSOLE_ROUTES, GLOBAL_ONLY_ROUTES, DashboardError, describe_error

HISTORY_KEY = "console_history"
RESULT_KEY = "console_result"
HISTORY_LENGTH = 12

#: Query parameters each route documents (docs/API.md), offered as hints.
ROUTE_PARAMS: dict[str, str] = {
    "/api/v1/global/jobs": "limit=50, status=running, namespace=gco-jobs",
    "/api/v1/queue/jobs": "limit=50, status=queued, target_region=us-east-1",
    "/api/v1/jobs": "limit=50, status=failed, namespace=gco-jobs",
}

#: The ``gco`` command that reads the same thing, where one exists.
ROUTE_COMMANDS: dict[str, str] = {
    "/api/v1/global/health": "gco jobs health --all-regions",
    "/api/v1/global/jobs": "gco jobs list --all-regions",
    "/api/v1/queue/stats": "gco queue stats",
    "/api/v1/queue/jobs": "gco queue list",
    "/api/v1/jobs": "gco jobs list -r REGION",
    "/api/v1/health": "gco jobs health -r REGION",
    "/api/v1/policy": "gco status --with-policy",
    "/api/v1/templates": "gco templates list",
    "/api/v1/webhooks": "gco webhooks list",
    "/api/v1/cost/status": "gco costs report status",
    "/api/v1/cost/reports": "gco costs report list",
}


def _send(path: str, region: str | None, params: dict[str, str]) -> None:
    started = time.perf_counter()
    try:
        payload = ui.fleet().api_get(path, region=region, params=params or None)
        error = None
    except Exception as exc:
        payload, error = None, describe_error(exc)
    elapsed = round((time.perf_counter() - started) * 1000)
    st.session_state[RESULT_KEY] = {
        "path": path,
        "region": region,
        "params": params,
        "payload": payload,
        "error": str(error) if error else None,
        "hint": error.hint if error else None,
        "ms": elapsed,
    }
    history: list[dict[str, Any]] = st.session_state.setdefault(HISTORY_KEY, [])
    history.insert(
        0,
        {
            "path": path,
            "region": region or "global",
            "params": params,
            "ms": elapsed,
            "ok": error is None,
        },
    )
    del history[HISTORY_LENGTH:]


def render() -> None:
    topology = ui.load_topology()
    ui.page_header(
        "Console",
        "Read any GCO API route with your credentials; nothing here changes the fleet",
        icon=":material/terminal:",
    )
    # Regional API mode has no global API to answer the global-only routes.
    routes = [p for p in CONSOLE_ROUTES if not (topology.regional_api and p in GLOBAL_ONLY_ROUTES)]
    request, recent = st.columns([3, 1], gap="large")
    with request:
        controls = st.container(horizontal=True, vertical_alignment="bottom", gap="medium")
        path = str(
            controls.selectbox(
                "Route",
                routes,
                format_func=lambda p: f"GET {p}  ·  {CONSOLE_ROUTES[p]}",
                key="console_route",
                width=560,
            )
        )
        global_only = path in GLOBAL_ONLY_ROUTES
        with controls:
            region = ui.region_picker(
                topology,
                key="console_region",
                allow_global=True,
                disabled=global_only,
                help="Global-only routes answer from the cross-Region aggregator."
                if global_only
                else None,
                width=320,
            )
        if topology.regional_api:
            st.caption(
                "Regional API mode has no global API, so the `/api/v1/global` routes are not "
                "listed."
            )
        text = st.text_input(
            "Query parameters",
            key=f"console_params_{path}",
            placeholder=ROUTE_PARAMS.get(path, "key=value, key=value"),
        )
        try:
            params = ui.parse_pairs(text, what="query parameter")
        except DashboardError as exc:
            ui.show_error(exc)
            return
        target = None if global_only or region == ui.GLOBAL else region
        if target is None and topology.regional_api:
            # Unpinned, the request would ride the default Region's bridge;
            # pinning it there says so in the answer's header and history.
            target = ui.fleet().config.default_region
        if st.button("Send", type="primary", icon=":material/send:", key="console_send"):
            with st.spinner(f"GET {path}"):
                _send(path, target, params)
        command = ROUTE_COMMANDS.get(path)
        if command:
            st.caption(f"From a terminal: `{command}`")
    with recent:
        st.markdown("**Recent**")
        history: list[dict[str, Any]] = st.session_state.get(HISTORY_KEY, [])
        if not history:
            st.caption("Requests you send appear here.")
        for index, item in enumerate(history):
            icon = ":material/check_circle:" if item["ok"] else ":material/error:"
            label = f"{item['path'].removeprefix('/api/v1')} · {item['region']} · {item['ms']} ms"
            if st.button(label, icon=icon, key=f"console_again_{index}", type="tertiary"):
                _send(
                    item["path"],
                    None if item["region"] == "global" else item["region"],
                    item["params"],
                )
                st.rerun()
        st.markdown("**Fleet status document**")
        st.caption("Everything `gco -o json status --with-policy` reports, as one JSON file.")
        if st.button("Prepare", icon=":material/description:", key="console_status"):
            document = ui.attempt(ui.load_fleet_status_with_policy)
            if document is not None:
                st.download_button(
                    "Download gco-status.json",
                    json.dumps(document, indent=2, default=str),
                    file_name="gco-status.json",
                    mime="application/json",
                    icon=":material/download:",
                    on_click="ignore",
                    key="console_status_download",
                )

    result = st.session_state.get(RESULT_KEY)
    if not result:
        return
    st.divider()
    where = result["region"] or "global API"
    st.markdown(f"**GET {result['path']}** · {where} · {result['ms']} ms")
    if result["error"]:
        ui.show_error(DashboardError(result["error"], hint=result["hint"]))
        return
    payload = result["payload"]
    body = json.dumps(payload, indent=2, default=str)
    table = frames.records_table(payload)
    # An answer of empty objects ({} or [{}]) tabulates as rows without
    # columns, which is nothing to show.
    rows = 0 if table.empty else len(table)
    as_table, as_json = st.tabs([f"Table ({ui.plural(rows, 'row')})", "JSON"])
    with as_table:
        if table.empty:
            st.caption("The answer has no rows to tabulate.")
        else:
            st.dataframe(table, hide_index=True, width="stretch")
    with as_json:
        st.json(body, expanded=2)
    name = result["path"].strip("/").replace("/", "-")
    with st.container(horizontal=True):
        st.download_button(
            "JSON",
            body,
            file_name=f"{name}.json",
            mime="application/json",
            icon=":material/download:",
            on_click="ignore",
            key="console_json",
        )
        if not table.empty:
            st.download_button(
                "CSV",
                table.to_csv(index=False),
                file_name=f"{name}.csv",
                mime="text/csv",
                icon=":material/table_view:",
                on_click="ignore",
                key="console_csv",
            )
