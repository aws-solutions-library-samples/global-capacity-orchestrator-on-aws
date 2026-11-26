"""The job submission dialog the Jobs and Queue pages share.

One form covers every way GCO accepts a Job: straight to a Region's API
(``gco jobs submit``), to whichever Region the capacity-aware placement picks
right now, onto the central DynamoDB queue (``gco queue submit``, with an
optional Spot price gate), or onto a Region's SQS queue (``gco jobs
submit-sqs``). The manifest comes from the checkout's ``examples/``, from
pasted YAML, or from an uploaded file, and the dialog always shows the
``gco`` command that does the same thing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st

from cli.dashboard import charts, frames, ui
from cli.dashboard.data import DashboardError, ExampleManifest, load_manifest_text
from cli.dashboard.regions import region_label

#: How a Job can be placed, in the order the dialog offers them.
PLACEMENTS: dict[str, str] = {
    "region": "A Region",
    "best": "Best Region now",
    "queue": "Central queue",
    "sqs": "Region's SQS queue",
}

SOURCES: dict[str, str] = {"example": "Example", "paste": "Paste YAML", "upload": "Upload"}

RECOMMENDATION_KEY = "submit_recommendation"
DEFAULT_EXAMPLE = "simple-job"


def _manifest_text(examples: list[ExampleManifest]) -> tuple[str, str]:
    """The YAML the operator chose and the file name the CLI command should name."""
    source = st.segmented_control(
        "Manifest",
        list(SOURCES),
        format_func=lambda k: SOURCES[k],
        default="example",
        required=True,
        key="submit_source",
    )
    if source == "example":
        if not examples:
            st.info("No example Job manifests were found under examples/ in this checkout.")
            return "", "job.yaml"
        # Start on the example that needs nothing beyond a deployed Region.
        names = [e.name for e in examples]
        chosen = st.selectbox(
            "Example",
            examples,
            index=names.index(DEFAULT_EXAMPLE) if DEFAULT_EXAMPLE in names else 0,
            format_func=lambda e: (e.title or e.name) + ("  ·  GPU" if e.gpu else ""),
            key="submit_example",
        )
        if chosen.summary:
            st.caption(chosen.summary)
        if chosen.prerequisites and not chosen.prerequisites.lower().startswith("none"):
            st.caption(f":material/checklist: Needs: {chosen.prerequisites}")
        try:
            text = Path(chosen.path).read_text(encoding="utf-8")
        except OSError as exc:
            # The list is cached for two minutes, so a file renamed or deleted
            # since (a branch switch, say) is still offered. Drop the list so
            # the next run reads the checkout again.
            ui.load_examples.clear()
            st.warning(
                f"Could not read examples/{chosen.name}.yaml ({exc.strerror or exc}). "
                "Pick another example.",
                icon=":material/warning:",
            )
            return "", "job.yaml"
        edited = st.text_area(
            "YAML",
            value=text,
            height=260,
            key=f"submit_yaml_{chosen.name}",
            label_visibility="collapsed",
        )
        return edited or "", f"examples/{chosen.name}.yaml"
    if source == "paste":
        pasted = st.text_area(
            "YAML",
            height=260,
            key="submit_yaml_paste",
            placeholder="apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: my-job\n...",
            label_visibility="collapsed",
        )
        return pasted or "", "job.yaml"
    upload = st.file_uploader("Manifest file", type=["yaml", "yml"], key="submit_upload")
    if upload is None:
        return "", "job.yaml"
    return upload.getvalue().decode("utf-8", errors="replace"), upload.name


def _summary(manifests: list[dict[str, Any]]) -> None:
    from cli.dashboard.data import manifest_requests_gpu

    parts = [f"`{m.get('kind')}/{(m.get('metadata') or {}).get('name', '?')}`" for m in manifests]
    gpu = (
        " · :orange-badge[:material/memory: requests GPUs]"
        if manifest_requests_gpu(manifests)
        else ""
    )
    st.caption(f"{ui.plural(len(manifests), 'object')}: " + ", ".join(parts) + gpu)


def _recommend(manifests: list[dict[str, Any]]) -> str | None:
    """Ask the capacity-aware placement where to run; remember the answer."""
    instance_type = st.text_input(
        "Instance type (optional)",
        key="submit_instance_type",
        placeholder="e.g. g6e.xlarge, for scoring on Spot placement and price",
        help="With a type, Regions are scored on its Spot placement score and price "
        "as well as queue depth, GPU use and running jobs.",
    )
    asked = (manifests, instance_type or None)
    if st.button("Recommend a Region", icon=":material/travel_explore:", key="submit_recommend"):
        with st.spinner("Scoring every Region..."):
            result = ui.attempt(
                ui.fleet().recommend_region, manifests, instance_type=instance_type or None
            )
        st.session_state[RECOMMENDATION_KEY] = (asked, result)
    # An answer scored for another manifest or instance type does not apply to this one.
    stored = st.session_state.get(RECOMMENDATION_KEY)
    result = stored[1] if isinstance(stored, tuple) and stored[0] == asked else None
    if not isinstance(result, dict) or not result.get("region"):
        st.caption("The recommendation reads each Region's queue depth, GPU use and running jobs.")
        return None
    region = str(result["region"])
    st.success(f"**{region_label(region)}**: {result.get('reason', '')}", icon=":material/near_me:")
    ranking = frames.recommendation_frame(result)
    if not ranking.empty:
        st.altair_chart(charts.region_scores(ranking), width="stretch")
    return region


def _cli(
    placement: str, path: str, region: str | None, labels: dict[str, str], extra: list[str]
) -> str:
    words: list[str]
    if placement == "queue":
        words = ["gco", "queue", "submit", path, "-r", region or "REGION"]
    elif placement == "sqs":
        words = ["gco", "jobs", "submit-sqs", path, "-r", region or "REGION"]
    else:
        words = ["gco", "jobs", "submit", path]
        if region:
            words += ["-r", region]
    for key, value in labels.items():
        words += ["-l", f"{key}={value}"]
    return ui.shell_words(*words, *extra)


@st.dialog("Submit a job", width="large", icon=":material/rocket_launch:")
def submit_dialog(default_placement: str = "region") -> None:
    """Choose a manifest and a placement, check it, and submit it."""
    topology = ui.load_topology()
    config = ui.fleet().config
    examples = ui.attempt(ui.load_examples) or []
    text, path = _manifest_text(examples)

    manifests: list[dict[str, Any]] = []
    if text.strip():
        try:
            manifests = load_manifest_text(text)
        except DashboardError as exc:
            st.error(str(exc), icon=":material/error:")
    if manifests:
        _summary(manifests)

    placement = st.segmented_control(
        "Placement",
        list(PLACEMENTS),
        format_func=lambda k: PLACEMENTS[k],
        default=default_placement if default_placement in PLACEMENTS else "region",
        required=True,
        key="submit_placement",
    )
    region: str | None = None
    priority = 0
    spot_type: str | None = None
    spot_price: float | None = None
    gate_problem: str | None = None
    if placement == "best":
        region = _recommend(manifests) if manifests else None
    else:
        left, right = st.columns(2)
        with left:
            region = ui.region_picker(topology, key="submit_region", label="Target Region")
        if placement in ("queue", "sqs"):
            with right:
                priority = int(
                    st.slider(
                        "Priority",
                        0,
                        100,
                        0,
                        key="submit_priority",
                        help="Higher runs first. The central queue dispatches by priority, "
                        "then submission time.",
                    )
                )
        if placement == "queue":
            with st.expander("Spot price gate", icon=":material/price_check:"):
                st.caption(
                    "Hold the job in the queue until the Spot price of an instance type "
                    "in the target Region drops to a cap."
                )
                gate_left, gate_right = st.columns(2)
                typed = gate_left.text_input("Instance type", key="submit_spot_type") or ""
                spot_type = typed.strip() or None
                cap = gate_right.number_input(
                    "Max USD / hour",
                    min_value=0.0,
                    value=0.0,
                    step=0.05,
                    format="%.4f",
                    key="submit_spot_price",
                )
                spot_price = float(cap) if cap else None
                # The queue refuses a gate it cannot enforce, and only after
                # Submit. Say here what is wrong with it; Submit stays off until
                # neither half is set or both are and pass the queue's own check.
                if spot_type and spot_price is None:
                    gate_problem = f"Set a cap for {spot_type}, or clear the instance type."
                elif spot_price is not None and not spot_type:
                    gate_problem = "Name an instance type for the cap, or set the cap to 0."
                elif spot_type and spot_price is not None:
                    from gco.services.spot_price_gate import validate_spot_gate_fields

                    gate_problem = validate_spot_gate_fields(spot_price, spot_type)
                if gate_problem:
                    st.warning(gate_problem, icon=":material/warning:")

    options_left, options_right = st.columns(2)
    namespace = options_left.text_input(
        "Namespace",
        value="",
        placeholder=config.default_namespace,
        key="submit_namespace",
        help="Used only by manifests that declare no metadata.namespace.",
    )
    label_text = options_right.text_input(
        "Labels", key="submit_labels", placeholder="team=ml, project=sweep"
    )
    try:
        labels = ui.parse_pairs(label_text)
    except DashboardError as exc:
        st.error(str(exc), icon=":material/error:")
        return

    extra: list[str] = []
    if placement in ("queue", "sqs") and priority:
        extra += ["-p", str(priority)]
    if placement == "queue" and spot_price is not None and spot_type:
        extra += ["--max-spot-price", str(spot_price), "--spot-instance-type", spot_type]
    if namespace:
        extra += ["-n", namespace]
    st.caption("The same, from a terminal:")
    st.code(_cli(str(placement), path, region, labels, extra), language="bash", wrap_lines=True)

    ready = bool(manifests) and bool(region) and not gate_problem
    if placement == "queue" and len(manifests) != 1:
        ready = False
        if manifests:
            st.warning(
                "The central queue takes exactly one Job manifest.", icon=":material/warning:"
            )

    with st.container(horizontal=True, horizontal_alignment="right"):
        validate = st.button(
            "Validate",
            icon=":material/fact_check:",
            disabled=not ready or placement not in ("region", "best"),
            help="Dry run: the Region's API validates the manifests without applying them.",
            key="submit_validate",
        )
        go = st.button(
            "Submit", type="primary", icon=":material/send:", disabled=not ready, key="submit_go"
        )

    fleet = ui.fleet()
    if validate and region:
        result = ui.attempt(
            fleet.submit_job,
            manifests,
            region=region,
            namespace=namespace or None,
            labels=labels,
            dry_run=True,
        )
        if result is not None:
            if result.get("success", True):
                st.success("The Region's API accepts these manifests.", icon=":material/verified:")
            else:
                st.error("; ".join(str(e) for e in result.get("errors") or []) or "Rejected.")
            st.json(result, expanded=False)
    if not (go and region):
        return
    name = str((manifests[0].get("metadata") or {}).get("name", "job"))
    if placement == "queue":
        done = ui.act(
            "Queueing...",
            lambda: fleet.queue_submit(
                manifests[0],
                region=str(region),
                namespace=namespace or None,
                priority=priority,
                labels=labels,
                max_spot_price=spot_price,
                spot_instance_type=spot_type,
            ),
            success=f"Queued {name} for {region}",
        )
    elif placement == "sqs":
        done = ui.act(
            "Sending to SQS...",
            lambda: fleet.submit_job_sqs(
                manifests,
                region=str(region),
                namespace=namespace or None,
                labels=labels,
                priority=priority,
            ),
            success=f"Sent {name} to {region}'s SQS queue",
        )
    else:
        done = ui.act(
            "Submitting...",
            lambda: fleet.submit_job(
                manifests, region=str(region), namespace=namespace or None, labels=labels
            ),
            success=f"Submitted {name} to {region}",
        )
    if done is not None:
        st.session_state.pop(RECOMMENDATION_KEY, None)
        st.rerun()
