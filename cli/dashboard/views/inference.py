"""Inference: endpoints across Regions, a chat playground, health probes and lifecycle actions."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from typing import Any

import streamlit as st

from cli.dashboard import charts, frames, ui
from cli.dashboard.data import Topology, describe_error
from cli.dashboard.regions import region_label

TABLE_KEY = "inference_table"
TAB_KEY = "inference_tab"
HEALTH_KEY = "inference_health"

#: DNS-1035 label of at most 55 characters (``gco inference deploy``'s rule).
ENDPOINT_NAME = re.compile(r"^[a-z]([-a-z0-9]{0,53}[a-z0-9])?$")

#: Serving runtimes the deploy dialog presets, as docs/INFERENCE.md documents them.
PRESETS: dict[str, dict[str, Any]] = {
    "vllm": {
        "label": "vLLM",
        "image": "vllm/vllm-openai:v0.32.0",
        "port": 8000,
        "health": "/health",
        "framework": "vllm",
        "model": "Qwen/Qwen2.5-1.5B-Instruct",
        "args": "--max-model-len\n4096",
    },
    "sglang": {
        "label": "SGLang",
        "image": "lmsysorg/sglang:v0.5.23",
        "port": 30000,
        "health": "/health",
        "framework": "sglang",
        "model": "Qwen/Qwen2.5-1.5B-Instruct",
        "args": "",
    },
    "triton": {
        "label": "Triton",
        "image": "nvcr.io/nvidia/tritonserver:26.09-py3",
        "port": 8000,
        "health": "/v2/health/ready",
        "framework": None,
        "model": "",
        "args": "tritonserver\n--model-repository=/models/NAME",
    },
    "custom": {
        "label": "Custom",
        "image": "",
        "port": 8000,
        "health": "/health",
        "framework": None,
        "model": "",
        "args": "",
    },
}


def _lines(text: str) -> list[str]:
    """The non-blank lines of ``text``, stripped: one server argument each."""
    return [line.strip() for line in text.splitlines() if line.strip()]


class _Timer:
    """Wraps a token stream to measure time to first token and throughput."""

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.first: float | None = None
        self.chunks = 0

    def wrap(self, stream: Iterator[str]) -> Iterator[str]:
        for chunk in stream:
            if self.first is None:
                self.first = time.perf_counter() - self.started
            self.chunks += 1
            yield chunk

    def summary(self) -> str:
        total = time.perf_counter() - self.started
        if self.first is None:
            return f"no tokens in {total:.1f} s"
        rate = self.chunks / total if total else 0.0
        return (
            f"first token {self.first:.2f} s · {self.chunks} chunks in {total:.1f} s · {rate:.0f}/s"
        )


def _kpis(frame: Any) -> None:
    live = frame[frame["state"] != "deleted"]
    regions = {r for regions in live["regions"] for r in regions}
    with st.container(horizontal=True, gap="small"):
        st.metric("Endpoints", len(live), border=True, height="stretch")
        st.metric(
            ":material/play_circle: Running",
            int((live["state"] == "running").sum()),
            border=True,
            height="stretch",
        )
        st.metric(
            "Replicas ready",
            f"{int(live['ready'].sum())} / {int(live['desired'].sum())}",
            border=True,
            height="stretch",
            help="Ready against desired replicas across every target Region, as the "
            "inference monitors last synced them",
        )
        st.metric("Regions serving", len(regions), border=True, height="stretch")
        st.metric(
            "GPUs requested",
            int((live["gpus"] * live["desired"]).sum()),
            border=True,
            height="stretch",
            help="GPUs per replica times desired replicas",
        )


def _overview(endpoint: dict[str, Any]) -> None:
    spec = frames.as_dict(endpoint.get("spec"))
    by_region = frames.endpoint_regions_frame(endpoint)
    chart, table = st.columns([2, 3], gap="large")
    with chart:
        st.markdown("**Replicas by Region**")
        if by_region.empty:
            st.caption("No target Regions.")
        else:
            st.altair_chart(charts.endpoint_replicas(by_region), width="stretch")
    with table:
        st.markdown("**What each Region's monitor last reported**")
        st.dataframe(
            by_region.assign(state=by_region["state"].map(lambda s: [s])),
            hide_index=True,
            width="stretch",
            column_config={
                "region": st.column_config.TextColumn("Region"),
                "state": ui.endpoint_state_column(),
                "ready": st.column_config.NumberColumn("Ready"),
                "desired": st.column_config.NumberColumn("Desired"),
                "last_sync": st.column_config.DatetimeColumn("Last sync", format="distance"),
                "error": st.column_config.TextColumn("Error"),
            },
        )
    autoscaling = frames.as_dict(spec.get("autoscaling"))
    env = frames.as_dict(spec.get("env"))
    facts = [
        ("Image", f"`{spec.get('image', '')}`"),
        ("Framework", str(spec.get("framework") or "not pinned")),
        ("Model", f"`{env.get('MODEL')}`" if env.get("MODEL") else "set by the arguments"),
        ("Port · health", f"{spec.get('port', '')} · `{spec.get('health_check_path', '/health')}`"),
        ("GPUs per replica", str(spec.get("gpu_count", 0))),
        ("Capacity", str(spec.get("capacity_type") or "Spot when available, else On-Demand")),
        (
            "Scaling",
            f"{autoscaling.get('min_replicas')}-{autoscaling.get('max_replicas')} replicas on "
            + ", ".join(
                f"{m.get('type')}:{m.get('target')}" for m in autoscaling.get("metrics") or []
            )
            if autoscaling and autoscaling.get("enabled")
            else f"{spec.get('replicas', 1)} per Region, fixed",
        ),
        ("Path", f"`{endpoint.get('ingress_path', '')}`"),
    ]
    st.markdown("  \n".join(f"**{label}** {value}" for label, value in facts))


def _playground(endpoint: dict[str, Any], topology: Topology) -> None:
    name = str(endpoint.get("endpoint_name"))
    targets = [r for r in frames.target_regions(endpoint) if r in topology.workload_regions]
    # Regional API mode has no global API: an unpinned request would ride the
    # default Region's bridge, so only the endpoint's Regions are routes.
    routes = targets if topology.regional_api else [ui.GLOBAL, *targets]
    if not routes:
        st.info(
            "The endpoint serves in no workload Region, so there is no Region to chat through.",
            icon=":material/info:",
        )
        return
    controls = st.container(horizontal=True, vertical_alignment="bottom", gap="medium")
    region = controls.selectbox(
        "Route",
        routes,
        format_func=lambda r: (
            "Global API (nearest healthy Region)" if r == ui.GLOBAL else region_label(r)
        ),
        key=f"chat_region_{name}",
        width=300,
    )
    max_tokens = int(
        controls.slider("Max tokens", 16, 2048, 256, key=f"chat_tokens_{name}", width=260)
    )
    temperature = float(
        controls.slider("Temperature", 0.0, 1.5, 0.7, 0.05, key=f"chat_temp_{name}", width=220)
    )
    history_key = f"chat_history_{name}"
    if controls.button("Clear", icon=":material/delete_sweep:", key=f"chat_clear_{name}"):
        st.session_state[history_key] = []
    route = None if region == ui.GLOBAL else str(region)
    model = ui.attempt(ui.load_served_model, name, route)
    if model is None:
        return
    with st.expander("System prompt", icon=":material/tune:"):
        system = st.text_area(
            "System prompt",
            key=f"chat_system_{name}",
            placeholder="You are a concise assistant.",
            label_visibility="collapsed",
        )
    st.caption(
        f"Talking to `{model}` through `{endpoint.get('ingress_path')}`, signed with your credentials."
    )
    history: list[dict[str, str]] = st.session_state.setdefault(history_key, [])
    # The conversation is drawn into a container placed above the input, so
    # a new exchange appears above the box rather than below it.
    conversation = st.container()
    prompt = st.chat_input(f"Message {model}", key=f"chat_input_{name}")
    with conversation:
        for message in history:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
                if message.get("stats"):
                    st.caption(message["stats"])
    if not prompt:
        return
    history.append({"role": "user", "content": prompt})
    with conversation, st.chat_message("user"):
        st.markdown(prompt)
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": m["role"], "content": m["content"]} for m in history
    ]
    with conversation, st.chat_message("assistant"):
        timer = _Timer()
        try:
            reply = st.write_stream(
                timer.wrap(
                    ui.fleet().chat(
                        name,
                        messages,
                        model=model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        region=route,
                    )
                )
            )
        except Exception as exc:
            ui.show_error(describe_error(exc))
            history.pop()
            return
        stats = timer.summary()
        st.caption(stats)
    history.append({"role": "assistant", "content": str(reply), "stats": stats})


def _health(endpoint: dict[str, Any], topology: Topology) -> None:
    name = str(endpoint.get("endpoint_name"))
    targets = frames.target_regions(endpoint)
    if topology.regional_api:
        # Regional API mode has no global API to probe through.
        how = "through each of its Regions' APIs, the way `gco inference health -r REGION` does"
    else:
        how = (
            "through the global API and through each Region's API, "
            "the way `gco inference health` does"
        )
    st.caption(f"Calls the endpoint's health path {how}, and times each round trip.")
    if st.button("Run health checks", icon=":material/monitor_heart:", key=f"health_run_{name}"):
        with st.spinner("Probing..."):
            st.session_state[HEALTH_KEY] = (name, ui.fleet().endpoint_health_all(name, targets))
    stored = st.session_state.get(HEALTH_KEY)
    if not stored or stored[0] != name:
        return
    if not stored[1]:
        st.info("The endpoint has no target Region to probe.", icon=":material/info:")
        return
    checks = frames.health_checks_frame(stored[1])
    st.altair_chart(charts.latency_bars(checks.dropna(subset=["latency_ms"])), width="stretch")
    st.caption("The first call through a Region's API bridge includes its Lambda's cold start.")
    st.dataframe(
        checks.assign(healthy=checks["healthy"].map(lambda ok: ["healthy" if ok else "unhealthy"])),
        hide_index=True,
        width="stretch",
        column_config={
            "region": st.column_config.TextColumn("Route"),
            "healthy": ui.health_column("Verdict"),
            "http_status": st.column_config.NumberColumn("HTTP status", format="%d"),
            "latency_ms": st.column_config.NumberColumn("Round trip (ms)", format="%d"),
            "error": st.column_config.TextColumn("Error"),
        },
    )


def _rerun_if(result: object) -> None:
    """Rerun after a change succeeded (``ui.act`` returns None when it failed)."""
    if result is not None:
        st.rerun()


def _row() -> Any:
    """A row of controls with their buttons aligned to the inputs' baseline."""
    return st.container(horizontal=True, vertical_alignment="bottom", gap="small")


def _manage(endpoint: dict[str, Any], topology: Topology) -> None:
    fleet = ui.fleet()
    name = str(endpoint.get("endpoint_name"))
    state = str(endpoint.get("desired_state"))
    spec = frames.as_dict(endpoint.get("spec"))
    image = str(spec.get("image", ""))
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("**Scale**")
        autoscaled = bool(frames.as_dict(spec.get("autoscaling")).get("enabled"))
        row = _row()
        current_replicas = int(spec["replicas"]) if spec.get("replicas") is not None else 1
        replicas = int(
            row.number_input(
                "Replicas per Region",
                min_value=0,
                max_value=max(64, current_replicas),
                value=current_replicas,
                key=f"scale_{name}",
                disabled=autoscaled,
                help="Its HPA or KEDA object scales an autoscaled endpoint" if autoscaled else None,
                width=200,
            )
        )
        if row.button(
            "Scale", icon=":material/stacks:", key=f"scale_go_{name}", disabled=autoscaled
        ):
            _rerun_if(
                ui.act(
                    "Scaling...",
                    lambda: fleet.scale_endpoint(name, replicas),
                    success=f"Scaled {name} to {replicas} per Region",
                )
            )
        st.markdown("**Image**")
        row = _row()
        new_image = row.text_input("New image", value=image, key=f"image_{name}", width=380)
        changed = bool(new_image) and new_image != image
        if row.button(
            "Roll out",
            icon=":material/published_with_changes:",
            key=f"image_go_{name}",
            disabled=not changed,
        ):
            ui.confirm(
                f"Replace `{image}` with `{new_image}` in every Region?",
                label="Roll out",
                action=lambda: fleet.update_endpoint_image(name, new_image),
                success=f"Rolling {name} to {new_image}",
                icon=":material/published_with_changes:",
            )
        row = _row()
        weight = int(
            row.number_input("Canary share (%)", 1, 99, 10, key=f"canary_weight_{name}", width=200)
        )
        if row.button(
            "Start canary", icon=":material/science:", key=f"canary_{name}", disabled=not changed
        ):
            _rerun_if(
                ui.act(
                    "Starting the canary...",
                    lambda: fleet.start_canary(name, new_image, weight=weight),
                    success=f"The canary of {new_image} takes {weight}% of {name}'s traffic",
                )
            )
        canary = frames.as_dict(spec.get("canary"))
        if canary:
            st.caption(
                f"Canary running: `{canary.get('image', '')}` takes {canary.get('weight', '?')}%"
            )
            row = _row()
            if row.button("Promote canary", icon=":material/upgrade:", key=f"promote_{name}"):
                _rerun_if(
                    ui.act(
                        "Promoting...",
                        lambda: fleet.promote_canary(name),
                        success="Canary promoted",
                    )
                )
            if row.button("Roll back canary", icon=":material/undo:", key=f"rollback_{name}"):
                _rerun_if(
                    ui.act(
                        "Rolling back...",
                        lambda: fleet.rollback_canary(name),
                        success="Canary removed",
                    )
                )
    with right:
        st.markdown("**Lifecycle**")
        row = _row()
        if state == "stopped":
            if row.button("Start", icon=":material/play_arrow:", key=f"start_{name}"):
                _rerun_if(
                    ui.act(
                        "Starting...", lambda: fleet.start_endpoint(name), success=f"Started {name}"
                    )
                )
        elif row.button("Stop", icon=":material/stop:", key=f"stop_{name}"):
            ui.confirm(
                f"Stop **{name}** in every Region? Its pods are scaled to zero; its "
                "configuration stays, so Start brings it back.",
                label="Stop",
                action=lambda: fleet.stop_endpoint(name),
                success=f"Stopped {name}",
                icon=":material/stop:",
            )
        if row.button("Delete", icon=":material/delete:", key=f"delete_{name}"):
            ui.confirm(
                f"Delete **{name}**? Each Region's monitor removes its Deployment, Service "
                "and routes. Redeploying needs the full spec again.",
                label="Delete",
                action=lambda: fleet.delete_endpoint(name),
                success=f"Deleting {name}",
            )
        st.markdown("**Regions**")
        current = frames.target_regions(endpoint)
        missing = [r for r in topology.workload_regions if r not in current]
        if missing:
            row = _row()
            add = str(
                row.selectbox(
                    "Add a Region",
                    missing,
                    format_func=region_label,
                    key=f"add_region_{name}",
                    width=280,
                )
            )
            if row.button("Add", icon=":material/add_location:", key=f"add_region_go_{name}"):
                _rerun_if(
                    ui.act(
                        "Adding...",
                        lambda: fleet.add_endpoint_region(name, add),
                        success=f"{name} added to {add}",
                    )
                )
        if len(current) > 1:
            row = _row()
            drop = str(
                row.selectbox(
                    "Remove a Region",
                    current,
                    format_func=region_label,
                    key=f"drop_region_{name}",
                    width=280,
                )
            )
            if row.button("Remove", icon=":material/wrong_location:", key=f"drop_region_go_{name}"):
                ui.confirm(
                    f"Remove **{name}** from **{drop}**? That Region's monitor deletes its pods.",
                    label="Remove",
                    action=lambda: fleet.remove_endpoint_region(name, drop),
                    success=f"{name} removed from {drop}",
                    icon=":material/wrong_location:",
                )
        elif not missing:
            st.caption("Serving in every workload Region.")
    ui.cli_hint(
        ui.shell_words("gco", "inference", "status", name),
        ui.shell_words("gco", "inference", "scale", name, "-r", str(replicas)),
        ui.shell_words("gco", "inference", "update-image", name, "-i", new_image or "IMAGE"),
        ui.shell_words("gco", "inference", "invoke", name, "-p", "Hello"),
    )


@st.dialog("Deploy an inference endpoint", width="large", icon=":material/smart_toy:")
def deploy_dialog() -> None:
    topology = ui.load_topology()
    preset_key = st.segmented_control(
        "Runtime",
        list(PRESETS),
        format_func=lambda k: str(PRESETS[k]["label"]),
        default="vllm",
        required=True,
        key="deploy_preset",
    )
    preset = PRESETS[str(preset_key)]
    left, right = st.columns(2)
    name = left.text_input("Name", key="deploy_name", placeholder="qwen-chat", max_chars=55)
    image = right.text_input("Image", value=str(preset["image"]), key=f"deploy_image_{preset_key}")
    model = left.text_input(
        "Model (MODEL)",
        value=str(preset["model"]),
        key=f"deploy_model_{preset_key}",
        help="A Hugging Face id or a path under /models; the renderer passes it to the server.",
    )
    port = int(
        right.number_input("Port", 1, 65535, int(preset["port"]), key=f"deploy_port_{preset_key}")
    )
    regions = st.multiselect(
        "Regions",
        list(topology.workload_regions),
        default=list(topology.workload_regions),
        format_func=region_label,
        key="deploy_regions",
    )
    a, b, c = st.columns(3)
    replicas = int(a.number_input("Replicas per Region", 1, 64, 1, key="deploy_replicas"))
    gpus = int(b.number_input("GPUs per replica", 0, 16, 1, key="deploy_gpus"))
    capacity = c.selectbox(
        "Capacity",
        ["any", "spot", "on-demand"],
        key="deploy_capacity",
        format_func=lambda v: {
            "any": "Spot when available",
            "spot": "Spot only",
            "on-demand": "On-Demand",
        }[v],
    )
    gpu_type = a.text_input(
        "Preferred instance type", key="deploy_gpu_type", placeholder="g6e.xlarge (a hint)"
    )
    health = b.text_input(
        "Health path", value=str(preset["health"]), key=f"deploy_health_{preset_key}"
    )
    autoscale = c.toggle("Autoscale", key="deploy_autoscale")
    autoscaling = None
    scaled_on = ""
    if autoscale:
        x, y, z = st.columns(3)
        low = int(x.number_input("Min replicas", 1, 64, 1, key="deploy_min"))
        high = int(y.number_input("Max replicas", 1, 128, 4, key="deploy_max"))
        scaled_on = str(z.selectbox("On", ["gpu:60", "cpu:70", "memory:80"], key="deploy_metric"))
        kind, target = scaled_on.split(":")
        autoscaling = {
            "enabled": True,
            "min_replicas": low,
            "max_replicas": max(high, low),
            "metrics": [{"type": kind, "target": int(target)}],
        }
    preset_args = str(preset["args"])
    # A keyed widget keeps its first value, so NAME cannot be filled in as the
    # box is drawn (the name may be typed after the preset is picked). While
    # the box still holds the preset's arguments, NAME is the endpoint's name.
    args_text = st.text_area(
        "Server arguments (one per line)",
        value=preset_args,
        key=f"deploy_args_{preset_key}",
        height=90,
        help="Until you edit them, `NAME` here stands for the endpoint's name."
        if "NAME" in preset_args
        else None,
    )
    extra_args = _lines(args_text)
    if extra_args == _lines(preset_args):
        extra_args = [arg.replace("NAME", name or "NAME") for arg in extra_args]

    words = ["gco", "inference", "deploy", name or "NAME", "-i", image]
    if preset["framework"]:
        words += ["--framework", str(preset["framework"])]
    if port != 8000:
        words += ["--port", str(port)]
    if model:
        words += ["-e", f"MODEL={model}"]
    if health and health != "/health":
        words += ["--health-path", health]
    if set(regions) != set(topology.workload_regions):
        for region in regions:
            words += ["-r", region]
    if replicas != 1:
        words += ["--replicas", str(replicas)]
    if gpus != 1:
        words += ["--gpu-count", str(gpus)]
    if gpu_type:
        words += ["--gpu-type", gpu_type]
    if capacity != "any":
        words += ["--capacity-type", str(capacity)]
    if autoscaling:
        words += [
            "--min-replicas",
            str(autoscaling["min_replicas"]),
            "--max-replicas",
            str(autoscaling["max_replicas"]),
            "--autoscale-metric",
            scaled_on,
        ]
    for arg in extra_args:
        words.append(f"--extra-args={arg}" if arg.startswith("-") else "--extra-args")
        if not arg.startswith("-"):
            words.append(arg)
    st.caption("The same, from a terminal:")
    st.code(ui.shell_words(*words), language="bash", wrap_lines=True)

    problems = []
    if name and not ENDPOINT_NAME.match(name):
        problems.append(
            "The name must be lowercase letters, digits and '-', start with a letter, 55 at most."
        )
    if not image:
        problems.append("Name a container image.")
    if not regions:
        problems.append("Pick at least one Region.")
    for problem in problems:
        st.warning(problem, icon=":material/warning:")
    with st.container(horizontal=True, horizontal_alignment="right"):
        go = st.button(
            "Deploy",
            type="primary",
            icon=":material/rocket_launch:",
            disabled=bool(problems) or not name,
            key="deploy_go",
        )
    if not go:
        return
    kwargs: dict[str, Any] = {
        "endpoint_name": name,
        "image": image,
        "framework": preset["framework"],
        "target_regions": regions,
        "replicas": replicas,
        "gpu_count": gpus,
        "gpu_type": gpu_type or None,
        "port": port,
        "health_check_path": health or "/health",
        "env": {"MODEL": model} if model else None,
        "autoscaling": autoscaling,
        "capacity_type": None if capacity == "any" else capacity,
        "extra_args": extra_args or None,
    }
    if (
        ui.act(
            "Registering...",
            lambda: ui.fleet().deploy_endpoint(**kwargs),
            success=f"{name} registered; each Region's monitor is creating it",
        )
        is not None
    ):
        st.rerun()


def render() -> None:
    topology = ui.load_topology()
    ui.page_header(
        "Inference",
        "Model endpoints in the registry, as each Region's inference monitor runs them",
        icon=":material/smart_toy:",
    )
    with st.container(horizontal=True, horizontal_alignment="right"):
        if st.button(
            "Deploy endpoint", type="primary", icon=":material/add:", key="inference_deploy"
        ):
            deploy_dialog()
    endpoints = ui.attempt(ui.load_endpoints)
    if endpoints is None:
        return
    frame = frames.endpoints_frame(endpoints)
    if frame.empty:
        st.info(
            "No inference endpoints. Deploy one with the button above.", icon=":material/smart_toy:"
        )
        return
    _kpis(frame)
    event = st.dataframe(
        frame.assign(state=frame["state"].map(lambda s: [s])),
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=TABLE_KEY,
        column_order=[
            "endpoint",
            "state",
            "availability",
            "ready",
            "desired",
            "regions",
            "model",
            "image",
            "gpus",
            "created",
        ],
        column_config={
            "endpoint": st.column_config.TextColumn("Endpoint", pinned=True),
            "state": ui.endpoint_state_column(),
            "availability": st.column_config.ProgressColumn(
                "Ready", min_value=0, max_value=1, format="percent"
            ),
            "ready": st.column_config.NumberColumn("Ready replicas"),
            "desired": st.column_config.NumberColumn("Desired"),
            "regions": st.column_config.ListColumn("Regions"),
            "model": st.column_config.TextColumn("Model"),
            "image": st.column_config.TextColumn("Image"),
            "gpus": st.column_config.NumberColumn("GPUs / replica"),
            "created": st.column_config.DatetimeColumn("Created", format="distance"),
        },
    )
    picked = ui.selected_row(event, len(frame))
    index = picked if picked is not None else 0
    name = str(frame.iloc[index]["endpoint"])
    endpoint = next((e for e in endpoints if e.get("endpoint_name") == name), None)
    if endpoint is None:
        return
    with st.container(border=True):
        state = str(endpoint.get("desired_state"))
        color = (
            "green"
            if state == "running"
            else ("gray" if state in ("stopped", "deleted") else "violet")
        )
        model = frames.as_dict(frames.as_dict(endpoint.get("spec")).get("env")).get("MODEL")
        head = ui.detail_header(
            name, f":{color}-badge[{state}]" + (f" :gray-badge[{model}]" if model else "")
        )
        if picked is None:
            head.caption("The first endpoint; select a row for another.")
        overview, playground, health, manage = st.tabs(
            ["Overview", "Playground", "Health", "Manage"], key=TAB_KEY, on_change="rerun"
        )
        if overview.open:
            with overview:
                _overview(endpoint)
        if playground.open:
            with playground:
                if state == "running":
                    _playground(endpoint, topology)
                else:
                    st.info(f"The endpoint is {state}; start it to chat.", icon=":material/info:")
        if health.open:
            with health:
                _health(endpoint, topology)
        if manage.open:
            with manage:
                _manage(endpoint, topology)
