"""The dashboard's charts: Altair specs and a deck.gl world map.

Every builder is a pure function from a frame (see :mod:`cli.dashboard.frames`)
to a chart object, so its encoding is testable without a browser. Altair charts
render with Streamlit's theme; colours that carry meaning (status, health) are
pinned to :mod:`cli.dashboard.brand` so they read the same on every page.
"""

from __future__ import annotations

import functools
import json
import math
import string
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import pydeck as pdk

from cli.dashboard import brand
from cli.dashboard.regions import region_info

#: What a builder returns: one chart, a layered chart or a concatenation.
type ChartSpec = alt.Chart | alt.LayerChart | alt.HConcatChart | alt.FacetChart

#: Interactive-selection parameter names the pages read back.
STATUS_PICK = "status_pick"
REGION_PICK = "region_pick"


def _status_scale(statuses: Sequence[str]) -> alt.Scale:
    known = [s for s in brand.STATUS_COLORS if s in statuses]
    other = sorted(set(statuses) - set(known))
    domain = known + other
    return alt.Scale(
        domain=domain,
        range=[brand.STATUS_COLORS.get(s, brand.MUTED) for s in domain],
    )


@functools.cache
def land_polygons() -> list[list[list[float]]]:
    """Natural Earth's 1:110m land outlines, shipped with the package."""
    text = (Path(__file__).with_name("assets") / "world_land.json").read_text("utf-8")
    polygons: list[list[list[float]]] = json.loads(text)["polygons"]
    return polygons


def world_map(
    regions: Any,
    *,
    control_region: str,
    dark: bool = True,
    globe: bool = False,
    height: int = 440,
) -> pdk.Deck:
    """Workload Regions on an offline world map, wired to the control-plane Region.

    No basemap tiles are fetched: the land comes from the packaged outline,
    so the map renders the same behind a proxy or offline. Region dots are
    coloured by health and sized by running work; arcs run from the
    control-plane Region (the global API and registry) to each workload
    Region.

    ``height`` is the height the page draws the map at
    (``st.pydeck_chart(height=...)``, 440 px on the Fleet page). It frames
    the flat map's view; the deck's own height does not size the map in
    Streamlit, so the two must be kept equal.
    """
    land_fill = [30, 37, 52, 255] if dark else [222, 227, 235, 255]
    land_line = [52, 62, 82, 255] if dark else [190, 198, 210, 255]
    label_color = [230, 234, 242, 255] if dark else [23, 27, 38, 255]

    placed = regions[regions["latitude"].notna() & regions["longitude"].notna()].copy()
    placed["label"] = placed["region"] + " · " + placed["city"]
    placed["color"] = placed["health"].map(
        lambda h: brand.rgba(brand.HEALTH_COLORS.get(h, brand.MUTED))
    )
    placed["halo"] = placed["health"].map(
        lambda h: brand.rgba(brand.HEALTH_COLORS.get(h, brand.MUTED), 70)
    )
    placed["size"] = 8 + placed["running"].clip(upper=40) * 0.5 + placed["endpoints"] * 1.5
    placed["tooltip"] = placed.apply(
        lambda row: (
            f"{row['region']} · {row['name']}\n"
            f"health: {row['health']}\n"
            f"running jobs: {row['running']}  queued: {row['queued']}\n"
            f"inference endpoints: {row['endpoints']}"
        ),
        axis=1,
    )
    records = placed.drop(columns=["latitude", "longitude"]).assign(
        position=placed[["longitude", "latitude"]].values.tolist()
    )
    rows = json.loads(records.to_json(orient="records"))

    control = region_info(control_region)
    for row in rows:
        # The label goes on the side of the dot the arc does not come from,
        # past the dot's halo: a Region west of the control plane (us-west-2
        # from us-east-2) is labelled to its left.
        west = (
            control.on_map
            and row["region"] != control_region
            and _west_of(float(row["position"][0]), float(control.longitude or 0.0))
        )
        gap = int(row["size"]) + 10
        row["anchor"] = "end" if west else "start"
        row["offset"] = [-gap, 0] if west else [gap, 0]
    arcs = (
        [
            {
                "source": [control.longitude, control.latitude],
                "target": row["position"],
                "target_color": row["color"],
            }
            for row in rows
            if row["region"] != control_region
        ]
        if control.on_map
        else []
    )
    hub = (
        [
            {
                "position": [control.longitude, control.latitude],
                "label": f"{control_region} · control plane",
                "tooltip": f"{control_region} · {control.name}\nglobal API, registry and queue",
            }
        ]
        if control.on_map
        else []
    )

    # pydeck turns a bare string property into a data accessor; a string
    # wrapped in single quotes is passed to deck.gl as the literal text.
    font = """'"Source Sans 3", "Source Sans Pro", -apple-system, "Segoe UI", sans-serif'"""
    # deck.gl draws only the glyphs it is told about (ASCII by default), so the
    # label characters are listed: the separator dot is not ASCII.
    characters = set(string.ascii_letters + string.digits + " -.()·")
    characters |= {c for row in rows + hub for c in row["label"]}
    glyphs = "'" + "".join(sorted(characters - {"'"})) + "'"
    text_style: dict[str, Any] = {
        "get_position": "position",
        "get_text": "label",
        "get_color": label_color,
        "get_size": 13,
        "font_family": font,
        "font_weight": 600,
        "character_set": glyphs,
        "outline_width": 3,
        "outline_color": [11, 15, 23, 255] if dark else [255, 255, 255, 255],
        "font_settings": {"sdf": True},
        "pickable": False,
    }

    layers = [
        pdk.Layer(
            "PolygonLayer",
            data=[{"polygon": polygon} for polygon in land_polygons()],
            get_polygon="polygon",
            get_fill_color=land_fill,
            get_line_color=land_line,
            line_width_min_pixels=0.6,
            stroked=True,
            filled=True,
            pickable=False,
        ),
        pdk.Layer(
            "ArcLayer",
            data=arcs,
            get_source_position="source",
            get_target_position="target",
            get_source_color=brand.rgba(brand.ORANGE, 230),
            get_target_color="target_color",
            get_width=2.4,
            get_height=0.45,
            great_circle=True,
            pickable=False,
        ),
        pdk.Layer(
            "ScatterplotLayer",
            data=rows,
            id="region-halo",
            get_position="position",
            get_fill_color="halo",
            get_radius="size + 9",
            radius_units="'pixels'",
            pickable=False,
        ),
        pdk.Layer(
            "ScatterplotLayer",
            data=rows,
            id="regions",
            get_position="position",
            get_fill_color="color",
            get_line_color=[255, 255, 255, 230],
            line_width_min_pixels=1.5,
            stroked=True,
            get_radius="size",
            radius_units="'pixels'",
            pickable=True,
        ),
        pdk.Layer(
            "ScatterplotLayer",
            data=hub,
            id="control-plane",
            get_position="position",
            get_fill_color=brand.rgba(brand.ORANGE),
            get_line_color=[255, 255, 255, 255],
            line_width_min_pixels=2,
            stroked=True,
            get_radius=8,
            radius_units="'pixels'",
            pickable=True,
        ),
        # Workload Regions are labelled beside their dot, away from their arc,
        # and the control plane below its own, so neighbours (us-east-1
        # beside us-east-2) never print over each other.
        pdk.Layer(
            "TextLayer",
            data=rows,
            id="region-labels",
            get_text_anchor="anchor",
            get_alignment_baseline="'center'",
            get_pixel_offset="offset",
            **text_style,
        ),
        # Below and to the left of its dot: a workload Region north-west of
        # the control plane (us-west-2 beside us-east-2) labels to its right
        # at its own latitude, so the two labels sit on different lines.
        pdk.Layer(
            "TextLayer",
            data=hub,
            id="control-plane-label",
            get_text_anchor="'end'",
            get_alignment_baseline="'top'",
            get_pixel_offset=[-8, 12],
            **text_style,
        ),
    ]
    points = [(float(lon), float(lat)) for lon, lat in placed[["longitude", "latitude"]].values]
    if control.on_map:
        points.append((float(control.longitude or 0.0), float(control.latitude or 0.0)))
    if globe:
        centre_lon = sum(p[0] for p in points) / len(points) if points else -30.0
        centre_lat = sum(p[1] for p in points) / len(points) if points else 30.0
        view = pdk.ViewState(
            latitude=centre_lat, longitude=centre_lon, zoom=0.9, min_zoom=0.5, max_zoom=6
        )
    else:
        latitude, longitude, zoom = fit_view(points, height=height)
        view = pdk.ViewState(
            latitude=latitude, longitude=longitude, zoom=zoom, min_zoom=0.5, max_zoom=6
        )
    return pdk.Deck(
        layers=layers,
        initial_view_state=view,
        # No basemap: the land layer is the map, so nothing is fetched from a
        # tile server (leaving map_style unset keeps Streamlit from picking one).
        map_provider=None,
        views=[pdk.View(type="_GlobeView" if globe else "MapView", controller=True)],
        tooltip={"text": "{tooltip}"},
        height=height,
    )


def _west_of(longitude: float, reference: float) -> bool:
    """Whether ``longitude`` lies west of ``reference`` along the shorter way round."""
    return ((longitude - reference + 180.0) % 360.0) - 180.0 < 0


def _mercator_y(latitude: float) -> float:
    """Web Mercator y in world units (one world is 1.0 wide)."""
    clamped = max(min(latitude, 85.0), -85.0)
    return math.log(math.tan(math.pi / 4 + math.radians(clamped) / 2)) / (2 * math.pi)


def fit_view(
    points: Sequence[tuple[float, float]],
    *,
    width: int = 1200,
    height: int = 440,
    pad_left: int = 200,
    pad_right: int = 230,
    pad_y: int = 70,
) -> tuple[float, float, float]:
    """``(latitude, longitude, zoom)`` that frames ``(lon, lat)`` points with label room.

    Workload labels run to the right of their dots and the control-plane label
    to the left of its own, so the frame keeps more room on those sides. The
    width is the dashboard's usual wide-layout map width: a wider browser
    shows more ocean, a narrower one starts closer to the edges.
    """
    if not points:
        return 28.0, -30.0, 1.15
    lons = [p[0] for p in points]
    ys = [_mercator_y(p[1]) for p in points]
    x_span = (max(lons) - min(lons)) / 360
    y_span = max(ys) - min(ys)
    usable_x = max(width - pad_left - pad_right, 200)
    usable_y = max(height - 2 * pad_y, 120)
    zoom_x = math.log2(usable_x / (512 * x_span)) if x_span > 0 else 4.0
    zoom_y = math.log2(usable_y / (512 * y_span)) if y_span > 0 else 4.0
    zoom = max(min(zoom_x, zoom_y, 4.0), 0.6)
    # Shift the centre so the padding falls on the label sides.
    pixels_per_degree = 512 * 2**zoom / 360
    centre_lon = (max(lons) + min(lons)) / 2 + (pad_right - pad_left) / 2 / pixels_per_degree
    centre_y = (max(ys) + min(ys)) / 2
    centre_lat = math.degrees(2 * math.atan(math.exp(centre_y * 2 * math.pi)) - math.pi / 2)
    return centre_lat, centre_lon, zoom


def status_by_region(counts: Any) -> ChartSpec:
    """Jobs per Region, stacked by status; click a segment to filter the table."""
    pick = alt.selection_point(name=STATUS_PICK, fields=["status", "region"], on="click")
    statuses = sorted(counts["status"].unique()) if not counts.empty else []
    chart: ChartSpec = (
        alt.Chart(counts)
        .mark_bar(cornerRadiusEnd=3, height=18)
        .encode(
            y=alt.Y("region:N", title=None, sort="ascending"),
            x=alt.X("sum(jobs):Q", title="Jobs", axis=alt.Axis(tickMinStep=1)),
            color=alt.Color(
                "status:N", scale=_status_scale(statuses), legend=alt.Legend(orient="bottom")
            ),
            order=alt.Order("status:O"),
            opacity=alt.condition(pick, alt.value(1.0), alt.value(0.35)),
            tooltip=["region:N", "status:N", alt.Tooltip("sum(jobs):Q", title="jobs")],
        )
        .add_params(pick)
        .properties(height=alt.Step(30))
    )
    return chart


def job_timeline(jobs: Any, *, limit: int = 40) -> ChartSpec:
    """When each recent Job ran: a bar from its start to its finish (or now)."""
    now = pd.Timestamp.now(tz="UTC")
    finished = jobs["status"].isin(["succeeded", "failed"])
    # Running Jobs extend to now; a finished one without a recorded end is left out.
    drawable = jobs["started"].notna() & (jobs["finished"].notna() | ~finished)
    recent = jobs[drawable].head(limit).copy()
    recent["end"] = recent["finished"].fillna(now)
    recent["runtime_min"] = (recent["end"] - recent["started"]).dt.total_seconds() / 60
    recent["job"] = recent["name"] + "  ·  " + recent["region"]
    statuses = sorted(recent["status"].unique()) if not recent.empty else []
    chart: ChartSpec = (
        alt.Chart(recent)
        .mark_bar(cornerRadius=3, height=12)
        .encode(
            x=alt.X("started:T", title=None),
            x2="end:T",
            y=alt.Y(
                "job:N",
                title=None,
                sort=alt.EncodingSortField(field="started", order="descending"),
                axis=alt.Axis(labelLimit=320),
            ),
            color=alt.Color(
                "status:N", scale=_status_scale(statuses), legend=alt.Legend(orient="bottom")
            ),
            tooltip=[
                "name:N",
                "region:N",
                "status:N",
                alt.Tooltip("started:T", format="%b %d %H:%M:%S"),
                alt.Tooltip("runtime_min:Q", title="minutes", format=".1f"),
            ],
        )
        .properties(height=alt.Step(20))
    )
    return chart


_METRIC_TITLES = {
    "cpu": "CPU requested %",
    "memory": "Memory requested %",
    "gpu": "GPU allocated %",
    "active_jobs": "Active jobs",
}


def utilization_lines(frame: Any, metric: str) -> ChartSpec:
    """One metric's per-minute series, a line per Region, with a hover rule."""
    data = frame[frame["metric"] == metric]
    hover = alt.selection_point(
        fields=["timestamp"], nearest=True, on="pointerover", empty=False, clear="pointerout"
    )
    percent = metric != "active_jobs"
    base = alt.Chart(data).encode(
        x=alt.X("timestamp:T", title=None),
        y=alt.Y(
            "value:Q",
            title=_METRIC_TITLES.get(metric, metric),
            scale=alt.Scale(domain=[0, 100]) if percent else alt.Undefined,
        ),
        color=alt.Color(
            "region:N",
            scale=alt.Scale(range=list(brand.SERIES)),
            legend=alt.Legend(orient="bottom", title=None),
        ),
    )
    lines = base.mark_line(interpolate="monotone", strokeWidth=2)
    points = base.mark_point(filled=True, size=40).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0))
    )
    rule = (
        alt.Chart(data)
        .mark_rule(color=brand.MUTED, strokeDash=[3, 3])
        .encode(
            x="timestamp:T",
            opacity=alt.condition(hover, alt.value(0.6), alt.value(0)),
            tooltip=[
                alt.Tooltip("timestamp:T", format="%H:%M"),
                "region:N",
                alt.Tooltip("value:Q", format=".1f"),
            ],
        )
        .add_params(hover)
    )
    chart: ChartSpec = alt.layer(lines, points, rule).properties(height=220)
    return chart


def spot_history(frame: Any) -> ChartSpec:
    """Spot price per availability zone over time (stepped, as EC2 changes it)."""
    zone_pick = alt.selection_point(fields=["availability_zone"], bind="legend")
    chart: ChartSpec = (
        alt.Chart(frame)
        .mark_line(interpolate="step-after", strokeWidth=2)
        .encode(
            x=alt.X("timestamp:T", title=None),
            y=alt.Y("price:Q", title="USD / hour", scale=alt.Scale(zero=False)),
            color=alt.Color(
                "availability_zone:N",
                title="Zone",
                scale=alt.Scale(range=list(brand.SERIES)),
                legend=alt.Legend(orient="bottom"),
            ),
            opacity=alt.condition(zone_pick, alt.value(1.0), alt.value(0.15)),
            tooltip=[
                "availability_zone:N",
                alt.Tooltip("timestamp:T", format="%b %d %H:%M"),
                alt.Tooltip("price:Q", format="$.4f"),
            ],
        )
        .add_params(zone_pick)
        .properties(height=300)
    )
    return chart


#: Every status a central-queue record moves through, in order.
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


def queue_heatmap(stats: Any) -> ChartSpec:
    """Central-queue records as a Region by status board, coloured by status.

    Every status gets a column, so an empty column reads as "nothing here"
    rather than as a missing one; a cell's opacity grows with its count.
    """
    regions = sorted(set(stats["region"])) if not stats.empty else []
    grid = pd.DataFrame(
        [{"region": r, "status": s} for r in regions for s in QUEUE_STATUSES],
        columns=["region", "status"],
    )
    board = grid.merge(stats, on=["region", "status"], how="left").fillna({"jobs": 0})
    board["jobs"] = board["jobs"].astype(int)
    board["label"] = board["jobs"].map(lambda n: str(n) if n else "")
    base = alt.Chart(board).encode(
        x=alt.X(
            "status:N",
            title=None,
            sort=list(QUEUE_STATUSES),
            axis=alt.Axis(orient="top", labelAngle=0, domain=False, ticks=False),
        ),
        y=alt.Y("region:N", title=None, axis=alt.Axis(domain=False, ticks=False)),
    )
    cells = base.mark_rect(cornerRadius=5, stroke=brand.INK, strokeWidth=3).encode(
        color=alt.Color("status:N", scale=_status_scale(list(QUEUE_STATUSES)), legend=None),
        opacity=alt.condition(
            "datum.jobs > 0",
            alt.Opacity("jobs:Q", scale=alt.Scale(range=[0.45, 1.0]), legend=None),
            alt.value(0.08),
        ),
        tooltip=["region:N", "status:N", "jobs:Q"],
    )
    labels = base.mark_text(fontWeight="bold", fontSize=13, color="white").encode(text="label:N")
    chart: ChartSpec = alt.layer(cells, labels).properties(height=alt.Step(38))
    return chart


def status_history(frame: Any) -> ChartSpec:
    """A queue record's journey: a bar per status for the time spent in it.

    A dot marks when each status began, so a state that lasted a few
    milliseconds, or a terminal one, still shows on the line.
    """
    data = frame.copy()
    data["until"] = data["until"].fillna(pd.Timestamp.now(tz="UTC"))
    data["seconds"] = (data["until"] - data["timestamp"]).dt.total_seconds()
    statuses = list(data["status"])
    base = alt.Chart(data).encode(
        y=alt.Y("status:N", title=None, sort=statuses),
        color=alt.Color("status:N", scale=_status_scale(sorted(set(statuses))), legend=None),
        tooltip=[
            "status:N",
            alt.Tooltip("timestamp:T", title="from", format="%H:%M:%S.%L"),
            alt.Tooltip("seconds:Q", title="seconds", format=",.1f"),
            "message:N",
        ],
    )
    bars = base.mark_bar(height=16, cornerRadius=3).encode(
        x=alt.X("timestamp:T", title=None, axis=alt.Axis(format="%H:%M:%S")), x2="until:T"
    )
    dots = base.mark_point(filled=True, size=70, opacity=1).encode(x="timestamp:T")
    chart: ChartSpec = alt.layer(bars, dots).properties(height=alt.Step(26))
    return chart


def events_timeline(frame: Any) -> ChartSpec:
    """Kubernetes events by reason over time, warnings in red."""
    chart: ChartSpec = (
        alt.Chart(frame)
        .mark_circle(size=90, opacity=0.85)
        .encode(
            x=alt.X("time:T", title=None),
            y=alt.Y("reason:N", title=None),
            color=alt.Color(
                "type:N",
                scale=alt.Scale(domain=["Normal", "Warning"], range=["#60A5FA", "#EF4444"]),
                legend=alt.Legend(orient="bottom", title=None),
            ),
            size=alt.Size("count:Q", legend=None),
            tooltip=["time:T", "type:N", "reason:N", "object:N", "message:N", "count:Q"],
        )
        .properties(height=alt.Step(24))
    )
    return chart


def container_usage(frame: Any) -> ChartSpec:
    """CPU and memory per container, side by side."""
    cpu = (
        alt.Chart(frame)
        .mark_bar(color=brand.ORANGE, cornerRadiusEnd=3)
        .encode(
            x=alt.X("cpu_millicores:Q", title="CPU (millicores)"),
            y=alt.Y("container:N", title=None),
            tooltip=["pod:N", "container:N", "cpu_millicores:Q"],
        )
    )
    memory = (
        alt.Chart(frame)
        .mark_bar(color="#2DD4BF", cornerRadiusEnd=3)
        .encode(
            x=alt.X("memory_mib:Q", title="Memory (MiB)"),
            y=alt.Y("container:N", title=None, axis=None),
            tooltip=["pod:N", "container:N", alt.Tooltip("memory_mib:Q", format=".1f")],
        )
    )
    chart: ChartSpec = alt.hconcat(cpu, memory)
    return chart


def price_comparison(frame: Any) -> ChartSpec:
    """On-Demand against Spot price per Region for one instance type."""
    long = frame.melt(
        id_vars=["region"], value_vars=["on_demand", "spot"], var_name="market", value_name="usd"
    ).dropna(subset=["usd"])
    long["market"] = long["market"].map({"on_demand": "On-Demand", "spot": "Spot"})
    chart: ChartSpec = (
        alt.Chart(long)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            y=alt.Y("region:N", title=None),
            x=alt.X("usd:Q", title="USD / hour"),
            yOffset="market:N",
            color=alt.Color(
                "market:N",
                scale=alt.Scale(domain=["On-Demand", "Spot"], range=["#60A5FA", brand.ORANGE]),
                legend=alt.Legend(orient="bottom", title=None),
            ),
            tooltip=["region:N", "market:N", alt.Tooltip("usd:Q", format="$.4f")],
        )
        .properties(height=alt.Step(14))
    )
    return chart


def endpoint_replicas(frame: Any) -> ChartSpec:
    """Ready against desired replicas per Region for one endpoint."""
    long = frame.melt(
        id_vars=["region"], value_vars=["ready", "desired"], var_name="replicas", value_name="count"
    )
    chart: ChartSpec = (
        alt.Chart(long)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            y=alt.Y("region:N", title=None),
            x=alt.X("count:Q", title="Replicas", axis=alt.Axis(tickMinStep=1)),
            yOffset="replicas:N",
            color=alt.Color(
                "replicas:N",
                scale=alt.Scale(domain=["ready", "desired"], range=["#10B981", "#475569"]),
                legend=alt.Legend(orient="bottom", title=None),
            ),
            tooltip=["region:N", "replicas:N", "count:Q"],
        )
        .properties(height=alt.Step(14))
    )
    return chart


def cost_trend(daily: Any, currency: str) -> ChartSpec:
    """Daily spend as a filled area with its points."""
    base = alt.Chart(daily).encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("amount:Q", title=currency),
        tooltip=[alt.Tooltip("date:T", format="%b %d"), alt.Tooltip("amount:Q", format=",.2f")],
    )
    area = base.mark_area(
        line={"color": brand.ORANGE},
        color={
            "gradient": "linear",
            "stops": [
                {"color": "rgba(255,110,66,0.05)", "offset": 0},
                {"color": brand.ORANGE, "offset": 1},
            ],
            "x1": 1,
            "x2": 1,
            "y1": 1,
            "y2": 0,
        },
        interpolate="monotone",
    )
    chart: ChartSpec = alt.layer(
        area, base.mark_point(color=brand.ORANGE, filled=True, size=30)
    ).properties(height=260)
    return chart


def ranked_bars(frame: Any, *, category: str, value: str, title: str) -> ChartSpec:
    """A descending horizontal bar chart (spend by service or Region)."""
    chart: ChartSpec = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=3, color=brand.ORANGE)
        .encode(
            y=alt.Y(f"{category}:N", sort="-x", title=None, axis=alt.Axis(labelLimit=260)),
            x=alt.X(f"{value}:Q", title=title),
            tooltip=[f"{category}:N", alt.Tooltip(f"{value}:Q", format=",.2f")],
        )
        .properties(height=alt.Step(22))
    )
    return chart


def region_scores(frame: Any) -> ChartSpec:
    """A recommendation's score per Region; lower ranks first, the pick in orange."""
    chart: ChartSpec = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            y=alt.Y("region:N", title=None, sort=alt.EncodingSortField("rank", order="ascending")),
            x=alt.X("score:Q", title="Score (lower is better)"),
            color=alt.condition(
                alt.datum.recommended, alt.value(brand.ORANGE), alt.value(brand.MUTED)
            ),
            tooltip=[
                "region:N",
                alt.Tooltip("score:Q", format=".3f"),
                alt.Tooltip("queue_depth:Q", title="queue depth"),
                alt.Tooltip("gpu_utilization:Q", title="GPU %", format=".0f"),
                alt.Tooltip("running_jobs:Q", title="running jobs"),
                "telemetry:N",
            ],
        )
        .properties(height=alt.Step(26))
    )
    return chart


_SQS_STATES = {
    "available": ("Waiting", "#A78BFA"),
    "in_flight": ("In flight", "#3B82F6"),
    "delayed": ("Delayed", "#F59E0B"),
    "dlq": ("Dead letters", "#EF4444"),
}


def sqs_depth(frame: Any) -> ChartSpec:
    """Each Region's SQS job queue: waiting, in flight, delayed and dead-lettered."""
    long = frame.melt(
        id_vars=["region"], value_vars=list(_SQS_STATES), var_name="state", value_name="messages"
    )
    long["state"] = long["state"].map(lambda s: _SQS_STATES[s][0])
    # Empty queues still get a readable 0-1 axis rather than a degenerate one.
    busiest = int(frame[list(_SQS_STATES)].sum(axis=1).max()) if not frame.empty else 0
    chart: ChartSpec = (
        alt.Chart(long)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            y=alt.Y("region:N", title=None),
            x=alt.X(
                "sum(messages):Q",
                title="Messages",
                scale=alt.Scale(domain=[0, max(busiest, 1)]),
                axis=alt.Axis(tickMinStep=1, format="d"),
            ),
            color=alt.Color(
                "state:N",
                scale=alt.Scale(
                    domain=[label for label, _ in _SQS_STATES.values()],
                    range=[color for _, color in _SQS_STATES.values()],
                ),
                legend=alt.Legend(orient="bottom", title=None),
            ),
            tooltip=["region:N", "state:N", "messages:Q"],
        )
        .properties(height=alt.Step(30))
    )
    return chart


_COMPONENT_COLORS = {
    "CPU": "#60A5FA",
    "Memory": "#2DD4BF",
    "GPU": brand.ORANGE,
    "Storage": "#A78BFA",
}


def allocation_stack(frame: Any, *, category: str) -> ChartSpec:
    """Kubernetes allocation cost per namespace or Region, split by resource."""
    chart: ChartSpec = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=2)
        .encode(
            y=alt.Y(f"{category}:N", title=None, sort="-x", axis=alt.Axis(labelLimit=220)),
            x=alt.X("sum(amount):Q", title="USD"),
            color=alt.Color(
                "component:N",
                scale=alt.Scale(
                    domain=list(_COMPONENT_COLORS), range=list(_COMPONENT_COLORS.values())
                ),
                legend=alt.Legend(orient="bottom", title=None),
            ),
            order=alt.Order("component:O"),
            tooltip=[f"{category}:N", "component:N", alt.Tooltip("amount:Q", format="$,.4f")],
        )
        .properties(height=alt.Step(22))
    )
    return chart


def allocation_trend(trend: Any, *, granularity: str = "daily") -> ChartSpec:
    """Kubernetes allocation cost per period, one banded bar per hour or day.

    The periods are drawn as ordinal bands rather than points on a time
    axis, so a window holding a single day still draws a full-width bar.
    """
    data = trend.copy()
    data["label"] = data["period"].dt.strftime(
        "%b %d %H:%M" if granularity == "hourly" else "%b %d"
    )
    chart: ChartSpec = (
        alt.Chart(data)
        .mark_bar(color=brand.ORANGE, cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X(
                "label:O",
                title=None,
                sort=alt.EncodingSortField("period", order="ascending"),
                axis=alt.Axis(labelAngle=-40 if granularity == "hourly" else 0, labelOverlap=True),
            ),
            y=alt.Y("amount:Q", title="USD"),
            tooltip=[
                alt.Tooltip("label:N", title="period"),
                alt.Tooltip("amount:Q", format="$,.4f"),
            ],
        )
        .properties(height=240)
    )
    return chart


def latency_bars(frame: Any) -> ChartSpec:
    """Endpoint health probe latency per Region, failures in red."""
    chart: ChartSpec = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            y=alt.Y("region:N", title=None),
            x=alt.X("latency_ms:Q", title="Round trip (ms)"),
            color=alt.condition(alt.datum.healthy, alt.value("#10B981"), alt.value("#EF4444")),
            tooltip=[
                "region:N",
                "healthy:N",
                "http_status:N",
                alt.Tooltip("latency_ms:Q", format=".0f"),
            ],
        )
        .properties(height=alt.Step(26))
    )
    return chart


def _count(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _dot_text(value: str) -> str:
    """A DOT double-quoted string body (quotes and backslashes escaped)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def topology_dot(
    regions: Any, *, project: str, control_region: str, api_region: str, dark: bool = True
) -> str:
    """The request path from this dashboard to every Region's cluster, as Graphviz DOT.

    Calls are SigV4-signed to the global API Gateway, which forwards through
    Global Accelerator to each Region's internal load balancer; the
    cross-Region aggregator reaches the same clusters through each Region's
    API bridge. Region nodes are coloured by health.
    """
    ink = "#E8ECF3" if dark else "#171B26"
    panel = "#141A26" if dark else "#F4F6FA"
    line = "#475569" if dark else "#94A3B8"
    lines = [
        "digraph gco {",
        '  graph [rankdir=LR, bgcolor="transparent", nodesep=0.35, ranksep=0.7, pad=0.2];',
        f'  node [shape=box, style="rounded,filled", fillcolor="{panel}", color="{line}", '
        f'fontcolor="{ink}", fontname="Helvetica", fontsize=11, penwidth=1.2];',
        f'  edge [color="{line}", fontcolor="{ink}", fontname="Helvetica", fontsize=9, arrowsize=0.6];',
        '  you [label="This dashboard\\n(your AWS credentials)", shape=box];',
        f'  api [label="Global API Gateway\\n{_dot_text(api_region)} · IAM (SigV4)", '
        f'color="{brand.ORANGE}", penwidth=2];',
        '  ga [label="Global Accelerator\\nhealth-based routing"];',
        '  agg [label="Cross-Region aggregator\\n/api/v1/global/*"];',
        f'  registry [label="{_dot_text(project)}-global · {_dot_text(control_region)}\\n'
        'job queue, endpoint registry", shape=cylinder];',
        '  you -> api [label="HTTPS"];',
        '  api -> ga [label="/api/v1/*, /inference/*"];',
        "  api -> agg;",
        "  api -> registry [style=dashed];",
    ]
    for row in regions.to_dict("records"):
        region = str(row["region"])
        node = "r_" + "".join(c if c.isalnum() else "_" for c in region)
        color = brand.HEALTH_COLORS.get(str(row.get("health")), brand.MUTED)
        label = "\\n".join(
            _dot_text(part)
            for part in (
                f"{project}-{region}",
                str(row.get("city", "")),
                f"{int(row.get('running', 0) or 0)} running · "
                + _count(int(row.get("endpoints", 0) or 0), "endpoint"),
            )
        )
        lines.append(f'  {node} [label="{label}", color="{color}", penwidth=2.2];')
        lines.append(f'  ga -> {node} [label="internal ALB"];')
        lines.append(f'  agg -> {node} [style=dashed, label="API bridge"];')
    lines.append("}")
    return "\n".join(lines)
