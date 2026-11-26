"""Unit tests for ``cli.dashboard.charts``: the Altair specs and the deck.gl world map.

Each Altair builder is fed frames built by ``cli.dashboard.frames`` from the
canned fleet in ``tests/_dashboard_fakes.py`` and asserted through
``chart.to_dict()`` (the Vega-Lite spec Streamlit receives): marks, encodings,
colour scales, selections and the rows drawn. The world map is asserted
through its layers and ``deck.to_json()`` (what pydeck sends to deck.gl), and
the topology diagram through its DOT text. One test renders the Fleet page
under Streamlit's AppTest, with the ``fake_fleet`` fixture answering, to check
the map is framed for the height the page draws it at. No browser, network or
AWS.
"""

from __future__ import annotations

import inspect
import json
import math
import string
from datetime import timedelta
from typing import Any

import pandas as pd
import pytest
import streamlit
from hypothesis import given, settings
from hypothesis import strategies as st
from streamlit.testing.v1 import AppTest

from cli.dashboard import brand, charts, frames
from cli.dashboard.data import RegionRead
from cli.dashboard.regions import REGION_COORDINATES, region_info
from cli.jobs import JobInfo, parse_job_info
from tests._dashboard_fakes import (
    APP,
    NOW,
    REGIONS,
    REPO_ROOT,
    FakeFleet,
    fake_fleet_fixture,  # noqa: F401
    fleet_status_document,
    metrics_read,
    queue_record,
    sample_endpoint,
    sample_jobs,
    topology,
)

STARTED = NOW - timedelta(minutes=40)
FAILED_AT = NOW - timedelta(minutes=10)
#: (lon, lat) of the canned workload Regions and of the control plane (us-east-2).
WORKLOAD_POINTS = [(-77.49, 39.04), (-119.70, 45.84), (-6.26, 53.35)]
CONTROL_POINT = (-83.00, 39.96)


def _spec(chart: Any) -> dict[str, Any]:
    spec: dict[str, Any] = chart.to_dict()
    return spec


def _values(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """The rows a chart draws (every builder here draws a single dataset)."""
    (values,) = spec["datasets"].values()
    return list(values)


def _when(value: str) -> pd.Timestamp:
    return pd.Timestamp(value)


def _job(**fields: Any) -> JobInfo:
    values: dict[str, Any] = {
        "name": "job",
        "namespace": "gco-jobs",
        "region": "us-east-1",
        "status": "running",
        "created_time": NOW - timedelta(minutes=5),
        "start_time": NOW - timedelta(minutes=5),
    }
    values.update(fields)
    return JobInfo(**values)


def failed_job_record() -> dict[str, Any]:
    """A Job as the API returns it once it failed: no ``completionTime``, a Failed condition."""
    return {
        "metadata": {
            "name": "train-oom",
            "namespace": "gco-jobs",
            "creationTimestamp": STARTED.isoformat(),
            "labels": {"team": "ml"},
        },
        "status": {
            "startTime": STARTED.isoformat(),
            "failed": 1,
            "conditions": [
                {
                    "type": "Failed",
                    "status": "True",
                    "reason": "BackoffLimitExceeded",
                    "lastTransitionTime": FAILED_AT.isoformat(),
                }
            ],
        },
    }


@pytest.fixture
def jobs() -> pd.DataFrame:
    return frames.jobs_frame(sample_jobs(), now=NOW)


@pytest.fixture
def region_rows(jobs: pd.DataFrame) -> pd.DataFrame:
    return frames.regions_frame(
        topology(),
        fleet_status_document(),
        [metrics_read(region) for region in REGIONS],
        jobs=jobs,
        endpoints=[sample_endpoint()],
    )


def _hand_rows(entries: list[dict[str, Any]]) -> pd.DataFrame:
    """Region rows shaped like ``regions_frame``'s, placed from the coordinate table."""
    records = []
    for entry in entries:
        info = region_info(entry["region"])
        base = {
            "region": info.code,
            "city": info.city,
            "name": info.name,
            "latitude": info.latitude,
            "longitude": info.longitude,
            "health": "healthy",
            "running": 0,
            "queued": 0,
            "endpoints": 0,
        }
        records.append(base | entry)
    return pd.DataFrame(records)


def _deck(regions: pd.DataFrame, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("control_region", "us-east-2")
    deck: dict[str, Any] = json.loads(charts.world_map(regions, **kwargs).to_json())
    return deck


def _layer(deck: dict[str, Any], layer_id: str) -> dict[str, Any]:
    (layer,) = [layer for layer in deck["layers"] if layer.get("id") == layer_id]
    return layer


class TestStatusScale:
    def test_known_statuses_in_legend_order_then_the_rest_muted(self) -> None:
        scale = charts._status_scale(["failed", "mystery", "running", "alpha"]).to_dict()
        assert scale == {
            "domain": ["running", "failed", "alpha", "mystery"],
            "range": [
                brand.STATUS_COLORS["running"],
                brand.STATUS_COLORS["failed"],
                brand.MUTED,
                brand.MUTED,
            ],
        }

    def test_no_statuses(self) -> None:
        assert charts._status_scale([]).to_dict() == {"domain": [], "range": []}


class TestLandPolygons:
    def test_land_is_closed_lon_lat_rings(self) -> None:
        polygons = charts.land_polygons()
        assert len(polygons) > 100
        assert all(len(ring) >= 4 and ring[0] == ring[-1] for ring in polygons)
        assert all(
            -180 <= lon <= 180 and -90 <= lat <= 90 for ring in polygons for lon, lat in ring
        )

    def test_land_is_the_public_domain_natural_earth_outline(self) -> None:
        asset = REPO_ROOT / "cli" / "dashboard" / "assets" / "world_land.json"
        document = json.loads(asset.read_text("utf-8"))
        assert document["source"].startswith("Natural Earth")
        assert "public domain" in document["source"]
        assert document["polygons"] == charts.land_polygons()

    def test_land_is_read_once(self) -> None:
        assert charts.land_polygons() is charts.land_polygons()


class TestWorldMap:
    def test_draws_the_packaged_land_and_no_basemap(self, region_rows: pd.DataFrame) -> None:
        deck = charts.world_map(region_rows, control_region="us-east-2")
        assert deck.map_provider is None
        text = deck.to_json()
        document = json.loads(text)
        assert "mapProvider" not in document
        # Nothing to fetch: no tile server, style sheet or font URL anywhere.
        assert "http" not in text
        land = document["layers"][0]
        assert land["@@type"] == "PolygonLayer"
        assert land["data"] == [{"polygon": ring} for ring in charts.land_polygons()]
        assert (land["filled"], land["stroked"], land["pickable"]) == (True, True, False)

    def test_layer_stack(self, region_rows: pd.DataFrame) -> None:
        deck = _deck(region_rows)
        assert [(layer["@@type"], layer.get("id")) for layer in deck["layers"][2:]] == [
            ("ScatterplotLayer", "region-halo"),
            ("ScatterplotLayer", "regions"),
            ("ScatterplotLayer", "control-plane"),
            ("TextLayer", "region-labels"),
            ("TextLayer", "control-plane-label"),
        ]
        assert [layer["@@type"] for layer in deck["layers"][:2]] == ["PolygonLayer", "ArcLayer"]
        pickable = {layer.get("id") for layer in deck["layers"] if layer["pickable"]}
        assert pickable == {"regions", "control-plane"}

    def test_literal_properties_are_not_serialized_as_accessors(
        self, region_rows: pd.DataFrame
    ) -> None:
        deck = _deck(region_rows)
        # pydeck sends a bare string as a data accessor ("@@=..."); only these are meant to be.
        accessors = {
            value
            for layer in deck["layers"]
            for value in layer.values()
            if isinstance(value, str) and value.startswith("@@=")
        }
        assert accessors == {
            "@@=polygon",
            "@@=source",
            "@@=target",
            "@@=target_color",
            "@@=position",
            "@@=halo",
            "@@=size + 9",
            "@@=color",
            "@@=size",
            "@@=label",
            "@@=anchor",
            "@@=offset",
        }
        for layer_id in ("region-halo", "regions", "control-plane"):
            assert _layer(deck, layer_id)["radiusUnits"] == "pixels"
        labels = _layer(deck, "region-labels")
        hub = _layer(deck, "control-plane-label")
        assert (labels["getTextAnchor"], labels["getAlignmentBaseline"]) == ("@@=anchor", "center")
        assert (hub["getTextAnchor"], hub["getAlignmentBaseline"]) == ("end", "top")
        for layer in (labels, hub):
            assert layer["fontFamily"] == (
                '"Source Sans 3", "Source Sans Pro", -apple-system, "Segoe UI", sans-serif'
            )
            assert not layer["characterSet"].startswith(("@@=", "'"))
            assert (layer["fontWeight"], layer["getSize"], layer["fontSettings"]) == (
                600,
                13,
                {"sdf": True},
            )

    def test_region_dots_coloured_by_health_and_sized_by_work(self) -> None:
        rows = _hand_rows(
            [
                {"region": "us-east-1", "health": "healthy", "running": 1, "endpoints": 1},
                {"region": "us-west-2", "health": "degraded", "running": 100},
                {"region": "eu-west-1", "health": "unhealthy", "endpoints": 2},
                {"region": "ap-northeast-1", "health": "mystery"},
            ]
        )
        deck = _deck(rows)
        dots = {row["region"]: row for row in _layer(deck, "regions")["data"]}
        assert {region: row["color"] for region, row in dots.items()} == {
            "us-east-1": brand.rgba(brand.HEALTH_COLORS["healthy"]),
            "us-west-2": brand.rgba(brand.HEALTH_COLORS["degraded"]),
            "eu-west-1": brand.rgba(brand.HEALTH_COLORS["unhealthy"]),
            "ap-northeast-1": brand.rgba(brand.MUTED),
        }
        assert dots["us-west-2"]["halo"] == brand.rgba(brand.HEALTH_COLORS["degraded"], 70)
        # 8 px, plus half a pixel per running job (up to 40), plus 1.5 per endpoint.
        assert {region: row["size"] for region, row in dots.items()} == {
            "us-east-1": 10.0,
            "us-west-2": 28.0,
            "eu-west-1": 11.0,
            "ap-northeast-1": 8.0,
        }
        assert dots["ap-northeast-1"]["position"] == [139.69, 35.68]
        assert _layer(deck, "region-halo")["getRadius"] == "@@=size + 9"

    def test_region_labels_and_tooltips(self, region_rows: pd.DataFrame) -> None:
        deck = charts.world_map(region_rows, control_region="us-east-2")
        document = json.loads(deck.to_json())
        labels = [row["label"] for row in _layer(document, "region-labels")["data"]]
        assert labels == ["us-east-1 · N. Virginia", "us-west-2 · Oregon", "eu-west-1 · Ireland"]
        (east, *_) = _layer(document, "regions")["data"]
        assert east["tooltip"] == (
            "us-east-1 · US East (N. Virginia)\n"
            "health: healthy\n"
            "running jobs: 1  queued: 2\n"
            "inference endpoints: 1"
        )
        # Each label sits past its dot's halo, on the side its arc does not
        # come from: Oregon is west of the Ohio control plane.
        sides = {
            row["region"]: (row["anchor"], row["offset"])
            for row in _layer(document, "region-labels")["data"]
        }
        size = {row["region"]: row["size"] for row in _layer(document, "regions")["data"]}
        assert sides == {
            "us-east-1": ("start", [int(size["us-east-1"]) + 10, 0]),
            "us-west-2": ("end", [-(int(size["us-west-2"]) + 10), 0]),
            "eu-west-1": ("start", [int(size["eu-west-1"]) + 10, 0]),
        }
        assert _layer(document, "region-labels")["getPixelOffset"] == "@@=offset"
        # Streamlit sends the Deck's tooltip template beside the JSON spec.
        assert deck._tooltip == {"text": "{tooltip}"}

    @pytest.mark.parametrize(
        ("control", "region", "anchor"),
        [
            ("us-east-2", "us-west-2", "end"),
            ("us-east-2", "eu-west-1", "start"),
            # The shorter way round from Ohio to Sydney is west, over the Pacific.
            ("us-east-2", "ap-southeast-2", "end"),
            ("ap-southeast-2", "us-west-2", "start"),
            # No arc reaches the control plane's own Region, or any Region when
            # the control plane is off the map.
            ("us-west-2", "us-west-2", "start"),
            ("zz-nowhere-9", "us-west-2", "start"),
        ],
    )
    def test_a_label_sits_on_the_side_its_arc_does_not_come_from(
        self, control: str, region: str, anchor: str
    ) -> None:
        deck = _deck(_hand_rows([{"region": region}]), control_region=control)
        (row,) = _layer(deck, "region-labels")["data"]
        gap = int(row["size"]) + 10
        assert (row["anchor"], row["offset"]) == (anchor, [-gap if anchor == "end" else gap, 0])

    def test_arcs_run_from_the_control_plane_to_each_workload_region(
        self, region_rows: pd.DataFrame
    ) -> None:
        arcs = _deck(region_rows)["layers"][1]
        healthy = brand.rgba(brand.HEALTH_COLORS["healthy"])
        assert arcs["data"] == [
            {"source": list(CONTROL_POINT), "target": list(point), "target_color": healthy}
            for point in WORKLOAD_POINTS
        ]
        assert arcs["getSourceColor"] == brand.rgba(brand.ORANGE, 230)
        assert arcs["greatCircle"] is True

    def test_control_plane_marker_and_label(self, region_rows: pd.DataFrame) -> None:
        deck = _deck(region_rows)
        hub = {
            "position": list(CONTROL_POINT),
            "label": "us-east-2 · control plane",
            "tooltip": "us-east-2 · US East (Ohio)\nglobal API, registry and queue",
        }
        assert _layer(deck, "control-plane")["data"] == [hub]
        assert _layer(deck, "control-plane-label")["data"] == [hub]
        assert _layer(deck, "control-plane")["getFillColor"] == brand.rgba(brand.ORANGE)
        assert _layer(deck, "control-plane-label")["getPixelOffset"] == [-8, 12]

    def test_control_region_that_also_runs_work_has_no_arc_to_itself(
        self, region_rows: pd.DataFrame
    ) -> None:
        deck = _deck(region_rows, control_region="us-east-1")
        assert [arc["target"] for arc in deck["layers"][1]["data"]] == [
            list(point) for point in WORKLOAD_POINTS[1:]
        ]
        assert _layer(deck, "control-plane")["data"][0]["label"] == "us-east-1 · control plane"

    def test_regions_without_coordinates_are_left_off(self) -> None:
        deck = _deck(_hand_rows([{"region": "us-east-1"}, {"region": "zz-nowhere-9"}]))
        for layer_id in ("region-halo", "regions", "region-labels"):
            assert [row["region"] for row in _layer(deck, layer_id)["data"]] == ["us-east-1"]
        assert [arc["target"] for arc in deck["layers"][1]["data"]] == [[-77.49, 39.04]]

    def test_control_region_off_the_map_draws_no_arcs_or_marker(
        self, region_rows: pd.DataFrame
    ) -> None:
        deck = _deck(region_rows, control_region="zz-nowhere-9")
        assert deck["layers"][1]["data"] == []
        assert _layer(deck, "control-plane")["data"] == []
        assert _layer(deck, "control-plane-label")["data"] == []
        latitude, longitude, zoom = charts.fit_view(WORKLOAD_POINTS, height=440)
        view = deck["initialViewState"]
        assert (view["latitude"], view["longitude"], view["zoom"]) == pytest.approx(
            (latitude, longitude, zoom)
        )

    def test_glyph_set_covers_every_label_character(self) -> None:
        rows = _hand_rows(
            [
                {"region": "eu-north-1", "city": "Ålesund"},
                {"region": "eu-west-1", "city": "Land's End"},
            ]
        )
        deck = _deck(rows)
        labels = _layer(deck, "region-labels")
        glyphs = labels["characterSet"]
        drawn = {c for row in labels["data"] for c in row["label"]}
        drawn |= set("us-east-2 · control plane")
        assert drawn - {"'"} <= set(glyphs)
        assert set(string.ascii_letters + string.digits + " -.()·Å") <= set(glyphs)
        # A quote cannot travel inside pydeck's quoted literal, so it is left out.
        assert "'" not in glyphs
        assert glyphs == "".join(sorted(set(glyphs)))
        assert _layer(deck, "control-plane-label")["characterSet"] == glyphs

    @pytest.mark.parametrize(
        ("dark", "fill", "line", "outline", "text"),
        [
            (True, [30, 37, 52, 255], [52, 62, 82, 255], [11, 15, 23, 255], [230, 234, 242, 255]),
            (
                False,
                [222, 227, 235, 255],
                [190, 198, 210, 255],
                [255, 255, 255, 255],
                [23, 27, 38, 255],
            ),
        ],
    )
    def test_theme_palettes(
        self,
        region_rows: pd.DataFrame,
        dark: bool,
        fill: list[int],
        line: list[int],
        outline: list[int],
        text: list[int],
    ) -> None:
        deck = _deck(region_rows, dark=dark)
        land = deck["layers"][0]
        assert (land["getFillColor"], land["getLineColor"]) == (fill, line)
        for layer_id in ("region-labels", "control-plane-label"):
            layer = _layer(deck, layer_id)
            assert (layer["outlineColor"], layer["getColor"]) == (outline, text)

    def test_the_default_height_is_the_440_px_the_fleet_page_draws(self) -> None:
        # Regions far apart north to south, so the height (not the width) sets
        # the zoom; the control plane is off the map, so only they are framed.
        rows = _hand_rows([{"region": "us-east-1"}, {"region": "sa-east-1"}])
        points = [(-77.49, 39.04), (-46.63, -23.55)]
        view = _deck(rows, control_region="zz-nowhere-9")["initialViewState"]
        assert (view["latitude"], view["longitude"], view["zoom"]) == pytest.approx(
            charts.fit_view(points, height=440)
        )
        assert charts.fit_view(points, height=440) != pytest.approx(
            charts.fit_view(points, height=460)
        )

    @pytest.mark.parametrize("height", [460, 300])
    def test_flat_map_frames_the_regions_and_control_plane(
        self, region_rows: pd.DataFrame, height: int
    ) -> None:
        deck = charts.world_map(region_rows, control_region="us-east-2", height=height)
        document = json.loads(deck.to_json())
        latitude, longitude, zoom = charts.fit_view(
            [*WORKLOAD_POINTS, CONTROL_POINT], height=height
        )
        assert document["initialViewState"] == {
            "latitude": pytest.approx(latitude),
            "longitude": pytest.approx(longitude),
            "zoom": pytest.approx(zoom),
            "minZoom": 0.5,
            "maxZoom": 6,
        }
        assert document["views"] == [{"@@type": "MapView", "controller": True}]

    def test_globe_centres_on_the_regions(self, region_rows: pd.DataFrame) -> None:
        deck = _deck(region_rows, globe=True)
        points = [*WORKLOAD_POINTS, CONTROL_POINT]
        assert deck["initialViewState"] == {
            "latitude": pytest.approx(sum(lat for _, lat in points) / len(points)),
            "longitude": pytest.approx(sum(lon for lon, _ in points) / len(points)),
            "zoom": 0.9,
            "minZoom": 0.5,
            "maxZoom": 6,
        }
        assert deck["views"] == [{"@@type": "_GlobeView", "controller": True}]

    @pytest.mark.parametrize(
        ("globe", "view"),
        [(True, (30.0, -30.0, 0.9)), (False, (28.0, -30.0, 1.15))],
    )
    def test_nothing_to_place_faces_the_atlantic(
        self, globe: bool, view: tuple[float, float, float]
    ) -> None:
        empty = frames.regions_frame(topology(workload_regions=()), None, [])
        deck = _deck(empty, control_region="zz-nowhere-9", globe=globe)
        state = deck["initialViewState"]
        assert (state["latitude"], state["longitude"], state["zoom"]) == view
        assert all(layer["data"] == [] for layer in deck["layers"][1:])


class TestWorldMapOnTheFleetPage:
    def test_the_map_is_framed_for_the_height_the_page_draws_it_at(
        self, fake_fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The deck's own height does not size the map in Streamlit, so the
        # height world_map frames for has to be the one st.pydeck_chart draws.
        framed: list[Any] = []
        drawn: list[Any] = []

        def recording(function: Any, heights: list[Any]) -> Any:
            def record(*args: Any, **kwargs: Any) -> Any:
                call = inspect.signature(function).bind(*args, **kwargs)
                call.apply_defaults()
                heights.append(call.arguments["height"])
                return function(*args, **kwargs)

            return record

        monkeypatch.setattr(charts, "world_map", recording(charts.world_map, framed))
        monkeypatch.setattr(streamlit, "pydeck_chart", recording(streamlit.pydeck_chart, drawn))
        app = AppTest.from_file(str(APP), default_timeout=60)
        app.run()
        assert not app.exception, [e.message for e in app.exception]
        assert len(drawn) == 1
        assert framed == drawn


def _offsets(
    points: list[tuple[float, float]], view: tuple[float, float, float]
) -> list[tuple[float, float]]:
    """Each ``(lon, lat)``'s pixel offset from the centre of a Web Mercator view."""
    latitude, longitude, zoom = view
    world = 512 * 2**zoom
    centre_y = charts._mercator_y(latitude)
    return [
        ((lon - longitude) / 360 * world, (charts._mercator_y(lat) - centre_y) * world)
        for lon, lat in points
    ]


def _inside_padded_frame(
    offsets: list[tuple[float, float]], *, width: int = 1200, height: int = 440
) -> bool:
    slack = 1e-6
    return all(
        -width / 2 + 200 - slack <= x <= width / 2 - 230 + slack
        and -height / 2 + 70 - slack <= y <= height / 2 - 70 + slack
        for x, y in offsets
    )


class TestFitView:
    def test_no_points_shows_the_atlantic(self) -> None:
        assert charts.fit_view([]) == (28.0, -30.0, 1.15)

    def test_one_point_is_centred_at_the_closest_zoom(self) -> None:
        latitude, longitude, zoom = charts.fit_view([(-77.49, 39.04)])
        assert zoom == 4.0
        assert latitude == pytest.approx(39.04)
        # Shifted so the point sits 15 px left of centre: half the extra room
        # the labels get on the right (230 px against 200 px on the left).
        ((x, y),) = _offsets([(-77.49, 39.04)], (latitude, longitude, zoom))
        assert (x, y) == pytest.approx((-15.0, 0.0))

    def test_widest_pair_touches_the_label_padding(self) -> None:
        points = [*WORKLOAD_POINTS, CONTROL_POINT]
        view = charts.fit_view(points)
        xs = [x for x, _ in _offsets(points, view)]
        # us-west-2 sits on the left padding and eu-west-1 on the right one.
        assert (min(xs), max(xs)) == pytest.approx((-600 + 200, 600 - 230))
        assert _inside_padded_frame(_offsets(points, view))

    def test_every_published_region_fits_one_frame(self) -> None:
        points = [(lon, lat) for lat, lon in REGION_COORDINATES.values()]
        view = charts.fit_view(points)
        assert view[2] > 0.6
        assert _inside_padded_frame(_offsets(points, view))

    @settings(max_examples=200, deadline=None)
    @given(
        st.lists(
            st.tuples(
                st.floats(min_value=-175, max_value=175), st.floats(min_value=-40, max_value=65)
            ),
            min_size=1,
            max_size=8,
        )
    )
    def test_points_always_land_inside_the_padded_frame(
        self, points: list[tuple[float, float]]
    ) -> None:
        view = charts.fit_view(points)
        assert 0.6 <= view[2] <= 4.0
        assert _inside_padded_frame(_offsets(points, view))

    def test_zoom_never_drops_below_the_floor(self) -> None:
        assert charts.fit_view([(-180.0, 0.0), (180.0, 0.0)])[2] == 0.6

    def test_tiny_maps_keep_a_minimum_usable_area(self) -> None:
        _, _, zoom = charts.fit_view([(0.0, 0.0), (10.0, 0.0)], width=300)
        assert zoom == pytest.approx(math.log2(200 / (512 * 10 / 360)))
        _, _, zoom = charts.fit_view([(0.0, -10.0), (0.0, 10.0)], height=100)
        assert zoom == pytest.approx(math.log2(120 / (512 * 2 * charts._mercator_y(10.0))))

    def test_mercator_y(self) -> None:
        assert charts._mercator_y(0.0) == pytest.approx(0.0)
        assert charts._mercator_y(-30.0) == pytest.approx(-charts._mercator_y(30.0))
        assert charts._mercator_y(10.0) < charts._mercator_y(20.0) < charts._mercator_y(60.0)
        # The poles are clamped to 85 degrees, where Web Mercator stops.
        assert charts._mercator_y(90.0) == charts._mercator_y(85.0)
        assert charts._mercator_y(-90.0) == charts._mercator_y(-85.0)


class TestJobCharts:
    def test_status_by_region(self, jobs: pd.DataFrame) -> None:
        counts = frames.status_counts(jobs)
        spec = _spec(charts.status_by_region(counts))
        assert _values(spec) == counts.to_dict("records")
        assert spec["mark"] == {"type": "bar", "cornerRadiusEnd": 3, "height": 18}
        encoding = spec["encoding"]
        assert (encoding["y"]["field"], encoding["x"]["aggregate"], encoding["x"]["field"]) == (
            "region",
            "sum",
            "jobs",
        )
        assert encoding["color"]["scale"] == {
            "domain": ["running", "pending", "succeeded", "failed"],
            "range": [
                brand.STATUS_COLORS[s] for s in ("running", "pending", "succeeded", "failed")
            ],
        }
        assert spec["params"] == [
            {
                "name": charts.STATUS_PICK,
                "select": {"type": "point", "fields": ["status", "region"], "on": "click"},
            }
        ]
        assert encoding["opacity"] == {
            "condition": {"param": charts.STATUS_PICK, "value": 1.0},
            "value": 0.35,
        }
        # Vega-Lite orders a stack by an ordinal field; a nominal one draws a warning.
        assert encoding["order"] == {"field": "status", "type": "ordinal"}

    def test_status_by_region_without_jobs(self) -> None:
        spec = _spec(charts.status_by_region(frames.status_counts(frames.jobs_frame([]))))
        assert _values(spec) == []
        assert spec["encoding"]["color"]["scale"] == {"domain": [], "range": []}

    def test_timeline_bars_run_from_start_to_finish_or_now(self, jobs: pd.DataFrame) -> None:
        before = pd.Timestamp.now(tz="UTC")
        spec = _spec(charts.job_timeline(jobs))
        after = pd.Timestamp.now(tz="UTC")
        bars = {row["name"]: row for row in _values(spec)}
        # The pending Job has not started, so it has no bar.
        assert sorted(bars) == sorted(set(jobs["name"]) - {"speech-asr-train"})
        running = bars["llm-finetune-a"]
        assert before <= _when(running["end"]) <= after
        done = bars["eval-suite-1"]
        assert _when(done["end"]) == _when(done["finished"])
        assert done["runtime_min"] == 10.0
        assert done["job"] == "eval-suite-1  ·  us-east-1"

    def test_failed_job_bar_ends_at_its_failed_condition_never_now(self) -> None:
        job = parse_job_info(failed_job_record(), "us-east-1")
        (bar,) = _values(_spec(charts.job_timeline(frames.jobs_frame([job]))))
        assert bar["status"] == "failed"
        assert _when(bar["end"]) == FAILED_AT
        assert bar["runtime_min"] == 30.0

    @pytest.mark.parametrize("status", ["succeeded", "failed"])
    def test_finished_job_without_an_end_is_left_out(self, status: str) -> None:
        rows = frames.jobs_frame(
            [_job(name="no-end", status=status, completion_time=None), _job(name="live")]
        )
        assert [bar["name"] for bar in _values(_spec(charts.job_timeline(rows)))] == ["live"]

    def test_timeline_of_jobs_that_are_all_running_runs_to_now(self) -> None:
        # No drawn Job has finished, so every bar's end is filled with now.
        started = pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=5)
        rows = frames.jobs_frame(
            [
                _job(name="a", created_time=started, start_time=started),
                _job(name="b", created_time=started, start_time=started, region="us-west-2"),
            ]
        )
        before = pd.Timestamp.now(tz="UTC")
        bars = _values(_spec(charts.job_timeline(rows)))
        after = pd.Timestamp.now(tz="UTC")
        assert [bar["name"] for bar in bars] == ["a", "b"]
        assert all(before <= _when(bar["end"]) <= after for bar in bars)
        assert all(bar["runtime_min"] >= 5.0 for bar in bars)

    def test_timeline_keeps_the_newest_jobs(self, jobs: pd.DataFrame) -> None:
        spec = _spec(charts.job_timeline(jobs, limit=2))
        assert [bar["name"] for bar in _values(spec)] == ["llm-finetune-a", "vision-pretrain"]

    def test_timeline_encoding(self, jobs: pd.DataFrame) -> None:
        spec = _spec(charts.job_timeline(jobs))
        encoding = spec["encoding"]
        assert encoding["x"] == {"field": "started", "title": None, "type": "temporal"}
        assert encoding["x2"] == {"field": "end"}
        assert encoding["y"]["field"] == "job"
        assert encoding["y"]["sort"] == {"field": "started", "order": "descending"}
        assert encoding["color"]["scale"]["domain"] == ["running", "succeeded", "failed"]
        assert {
            "field": "runtime_min",
            "format": ".1f",
            "title": "minutes",
            "type": "quantitative",
        } in (encoding["tooltip"])

    def test_timeline_without_jobs(self) -> None:
        spec = _spec(charts.job_timeline(frames.jobs_frame([])))
        assert _values(spec) == []
        assert spec["encoding"]["color"]["scale"]["domain"] == []

    def test_events_timeline(self) -> None:
        events = frames.events_frame(FakeFleet().job_events("train", "gco-jobs", "us-east-1"))
        spec = _spec(charts.events_timeline(events))
        assert [row["reason"] for row in _values(spec)] == ["BackoffLimitExceeded", "Scheduled"]
        assert spec["mark"]["type"] == "circle"
        assert spec["encoding"]["color"]["scale"] == {
            "domain": ["Normal", "Warning"],
            "range": ["#60A5FA", "#EF4444"],
        }
        assert (spec["encoding"]["size"]["field"], spec["encoding"]["y"]["field"]) == (
            "count",
            "reason",
        )

    def test_container_usage_sets_cpu_beside_memory(self) -> None:
        usage = frames.container_metrics_frame(
            FakeFleet().job_metrics("train", "gco-jobs", "us-east-1")
        )
        spec = _spec(charts.container_usage(usage))
        cpu, memory = spec["hconcat"]
        assert (cpu["mark"]["color"], cpu["encoding"]["x"]["field"]) == (
            brand.ORANGE,
            "cpu_millicores",
        )
        assert (memory["mark"]["color"], memory["encoding"]["x"]["field"]) == (
            "#2DD4BF",
            "memory_mib",
        )
        assert memory["encoding"]["y"]["axis"] is None
        assert _values(spec) == usage.to_dict("records")


class TestQueueCharts:
    def test_heatmap_gives_every_region_every_status(self) -> None:
        stats = frames.queue_stats_frame(FakeFleet().queue_stats())
        spec = _spec(charts.queue_heatmap(stats))
        cells = {
            (row["region"], row["status"]): (row["jobs"], row["label"]) for row in _values(spec)
        }
        assert len(cells) == 3 * len(charts.QUEUE_STATUSES)
        assert cells[("us-east-1", "queued")] == (1, "1")
        assert cells[("us-west-2", "failed")] == (1, "1")
        assert cells[("eu-west-1", "succeeded")] == (1, "1")
        assert cells[("us-east-1", "running")] == (0, "")
        rect, text = spec["layer"]
        assert rect["encoding"]["x"]["sort"] == list(charts.QUEUE_STATUSES)
        assert rect["encoding"]["color"]["scale"]["domain"] == [
            s for s in brand.STATUS_COLORS if s in charts.QUEUE_STATUSES
        ]
        assert rect["encoding"]["opacity"]["condition"]["test"] == "datum.jobs > 0"
        assert rect["encoding"]["opacity"]["value"] == 0.08
        assert (rect["mark"]["stroke"], text["encoding"]["text"]["field"]) == (brand.INK, "label")

    def test_heatmap_has_no_column_for_unknown_statuses(self) -> None:
        stats = pd.DataFrame([{"region": "us-east-1", "status": "mystery", "jobs": 4}])
        rows = _values(_spec(charts.queue_heatmap(stats)))
        assert [row["status"] for row in rows] == list(charts.QUEUE_STATUSES)
        assert {row["jobs"] for row in rows} == {0}

    def test_empty_heatmap(self) -> None:
        assert _values(_spec(charts.queue_heatmap(frames.queue_stats_frame({})))) == []

    def test_live_status_runs_to_now(self) -> None:
        history = frames.status_history_frame(queue_record("q", "sweep", "us-east-1", "running"))
        before = pd.Timestamp.now(tz="UTC")
        spec = _spec(charts.status_history(history))
        after = pd.Timestamp.now(tz="UTC")
        rows = _values(spec)
        assert [row["status"] for row in rows] == ["queued", "claimed", "running"]
        assert before <= _when(rows[-1]["until"]) <= after
        assert rows[-1]["seconds"] == pytest.approx(
            (_when(rows[-1]["until"]) - _when(rows[-1]["timestamp"])).total_seconds()
        )
        # The chart fills its own copy; the caller's frame keeps the open end.
        assert pd.isna(history["until"].iloc[-1])
        bars, dots = spec["layer"]
        assert bars["encoding"]["y"]["sort"] == ["queued", "claimed", "running"]
        assert bars["encoding"]["x2"] == {"field": "until"}
        assert (bars["mark"]["type"], dots["mark"]["type"]) == ("bar", "point")
        assert bars["encoding"]["color"]["scale"]["domain"] == ["running", "queued", "claimed"]

    def test_terminal_status_is_a_dot_without_length(self) -> None:
        history = frames.status_history_frame(queue_record("q", "dedup", "eu-west-1", "succeeded"))
        rows = _values(_spec(charts.status_history(history)))
        assert rows[-1]["until"] == rows[-1]["timestamp"]
        assert [row["seconds"] for row in rows] == [5.0, 115.0, 0.0]

    def test_history_without_readable_times_still_draws(self) -> None:
        record = {"status_history": [{"status": "queued", "timestamp": "not a time"}]}
        rows = _values(_spec(charts.status_history(frames.status_history_frame(record))))
        assert [(row["status"], row["timestamp"]) for row in rows] == [("queued", "")]

    def test_sqs_depth(self) -> None:
        spec = _spec(charts.sqs_depth(frames.sqs_frame(fleet_status_document())))
        messages = {(row["region"], row["state"]): row["messages"] for row in _values(spec)}
        assert len(messages) == 12
        assert messages[("us-east-1", "Waiting")] == 2
        assert messages[("us-east-1", "In flight")] == 1
        assert messages[("us-west-2", "Dead letters")] == 1
        assert messages[("eu-west-1", "Delayed")] == 1
        assert spec["encoding"]["x"]["scale"] == {"domain": [0, 3]}
        assert spec["encoding"]["color"]["scale"] == {
            "domain": ["Waiting", "In flight", "Delayed", "Dead letters"],
            "range": ["#A78BFA", "#3B82F6", "#F59E0B", "#EF4444"],
        }

    @pytest.mark.parametrize(
        "fleet",
        [None, {"sections": {"queue": {"data": {"by_region": {"us-east-1": {"available": 0}}}}}}],
    )
    def test_idle_queues_keep_a_readable_axis(self, fleet: dict[str, Any] | None) -> None:
        spec = _spec(charts.sqs_depth(frames.sqs_frame(fleet)))
        assert spec["encoding"]["x"]["scale"] == {"domain": [0, 1]}


class TestUtilizationAndCapacityCharts:
    @pytest.mark.parametrize(
        ("metric", "title", "domain", "rows"),
        [
            ("cpu", "CPU requested %", [0, 100], 18),
            ("memory", "Memory requested %", [0, 100], 18),
            ("gpu", "GPU allocated %", [0, 100], 18),
            ("active_jobs", "Active jobs", None, 18),
            ("disk", "disk", [0, 100], 0),
        ],
    )
    def test_utilization_lines(
        self, metric: str, title: str, domain: list[int] | None, rows: int
    ) -> None:
        series = frames.utilization_frame(FakeFleet().utilization_history(REGIONS))
        spec = _spec(charts.utilization_lines(series, metric))
        drawn = _values(spec)
        assert len(drawn) == rows
        assert {row["metric"] for row in drawn} <= {metric}
        lines, points, rule = spec["layer"]
        assert lines["encoding"]["y"]["title"] == title
        assert lines["encoding"]["y"].get("scale", {}).get("domain") == domain
        assert lines["encoding"]["color"]["scale"]["range"] == list(brand.SERIES)
        assert (lines["mark"]["type"], points["mark"]["type"], rule["mark"]["type"]) == (
            "line",
            "point",
            "rule",
        )
        (hover,) = spec["params"]
        assert hover["select"] == {
            "type": "point",
            "clear": "pointerout",
            "fields": ["timestamp"],
            "nearest": True,
            "on": "pointerover",
        }
        assert points["encoding"]["opacity"]["condition"] == {
            "param": hover["name"],
            "value": 1,
            "empty": False,
        }
        assert rule["mark"]["color"] == brand.MUTED

    def test_spot_history(self) -> None:
        prices = frames.spot_frame(FakeFleet().spot_price_history("g5.xlarge", "us-east-1"))
        spec = _spec(charts.spot_history(prices))
        assert len(_values(spec)) == 6
        assert spec["mark"] == {"type": "line", "interpolate": "step-after", "strokeWidth": 2}
        assert spec["encoding"]["y"]["scale"] == {"zero": False}
        assert spec["encoding"]["color"]["field"] == "availability_zone"
        (zones,) = spec["params"]
        assert (zones["bind"], zones["select"]["fields"]) == ("legend", ["availability_zone"])
        assert spec["encoding"]["opacity"]["condition"]["param"] == zones["name"]

    def test_price_comparison(self) -> None:
        reads = FakeFleet().instance_report("g5.xlarge", REGIONS)
        reads.append(RegionRead("ap-south-1", data={"on_demand_price": 3.1, "spot_price": None}))
        spec = _spec(charts.price_comparison(frames.instance_report_frame(reads)))
        bars = {(row["region"], row["market"]): row["usd"] for row in _values(spec)}
        assert (bars[("us-east-1", "On-Demand")], bars[("us-east-1", "Spot")]) == (2.5, 0.5)
        # A Region without a Spot price shows only its On-Demand bar.
        assert ("ap-south-1", "Spot") not in bars
        assert bars[("ap-south-1", "On-Demand")] == 3.1
        assert len(bars) == 7
        assert spec["encoding"]["color"]["scale"] == {
            "domain": ["On-Demand", "Spot"],
            "range": ["#60A5FA", brand.ORANGE],
        }
        assert spec["encoding"]["yOffset"]["field"] == "market"

    def test_region_scores(self) -> None:
        ranking = frames.recommendation_frame(FakeFleet().recommend_region())
        spec = _spec(charts.region_scores(ranking))
        assert spec["encoding"]["y"]["sort"] == {"field": "rank", "order": "ascending"}
        assert spec["encoding"]["color"] == {
            "condition": {"test": "datum.recommended", "value": brand.ORANGE},
            "value": brand.MUTED,
        }
        assert [(row["region"], row["recommended"]) for row in _values(spec)] == [
            ("us-west-2", True),
            ("us-east-1", False),
        ]


class TestInferenceCharts:
    def test_endpoint_replicas(self) -> None:
        spec = _spec(charts.endpoint_replicas(frames.endpoint_regions_frame(sample_endpoint())))
        assert sorted((row["region"], row["replicas"], row["count"]) for row in _values(spec)) == [
            ("us-east-1", "desired", 1),
            ("us-east-1", "ready", 1),
            ("us-west-2", "desired", 1),
            ("us-west-2", "ready", 1),
        ]
        assert spec["encoding"]["color"]["scale"] == {
            "domain": ["ready", "desired"],
            "range": ["#10B981", "#475569"],
        }

    def test_latency_bars(self) -> None:
        checks = FakeFleet().endpoint_health_all("qwen-chat", ["us-east-1"])
        checks.append({"region": "us-west-2", "healthy": False, "latency_ms": 5000})
        spec = _spec(charts.latency_bars(frames.health_checks_frame(checks)))
        assert [(row["region"], row["healthy"]) for row in _values(spec)] == [
            ("global", True),
            ("us-east-1", True),
            ("us-west-2", False),
        ]
        assert spec["encoding"]["color"] == {
            "condition": {"test": "datum.healthy", "value": "#10B981"},
            "value": "#EF4444",
        }
        assert spec["encoding"]["x"]["field"] == "latency_ms"


class TestCostCharts:
    def test_cost_trend(self) -> None:
        daily = frames.cost_frames(FakeFleet().costs())["daily"]
        spec = _spec(charts.cost_trend(daily, "EUR"))
        assert len(_values(spec)) == 5
        area, points = spec["layer"]
        assert area["encoding"]["y"]["title"] == "EUR"
        assert area["mark"]["line"] == {"color": brand.ORANGE}
        assert area["mark"]["color"]["stops"][-1] == {"color": brand.ORANGE, "offset": 1}
        assert (points["mark"]["type"], points["mark"]["color"]) == ("point", brand.ORANGE)

    def test_ranked_bars(self) -> None:
        services = frames.cost_frames(FakeFleet().costs())["services"]
        spec = _spec(charts.ranked_bars(services, category="service", value="amount", title="USD"))
        assert spec["encoding"]["y"] == {
            "axis": {"labelLimit": 260},
            "field": "service",
            "sort": "-x",
            "title": None,
            "type": "nominal",
        }
        assert spec["encoding"]["x"] == {"field": "amount", "title": "USD", "type": "quantitative"}
        assert _values(spec) == services.to_dict("records")

    @pytest.mark.parametrize(
        ("table", "category"), [("namespaces", "namespace"), ("regions", "region")]
    )
    def test_allocation_stack(self, table: str, category: str) -> None:
        frame = frames.allocation_frames(FakeFleet().allocation_costs())[table]
        spec = _spec(charts.allocation_stack(frame, category=category))
        assert len(_values(spec)) == len(frame)
        assert spec["encoding"]["y"]["field"] == category
        assert spec["encoding"]["color"]["scale"] == {
            "domain": ["CPU", "Memory", "GPU", "Storage"],
            "range": ["#60A5FA", "#2DD4BF", brand.ORANGE, "#A78BFA"],
        }
        assert spec["encoding"]["order"] == {"field": "component", "type": "ordinal"}

    @pytest.mark.parametrize(
        ("granularity", "labels", "angle"),
        [("daily", ["Nov 24", "Nov 25"], 0), ("hourly", ["Nov 24 00:00", "Nov 25 00:00"], -40)],
    )
    def test_allocation_trend(self, granularity: str, labels: list[str], angle: int) -> None:
        trend = frames.allocation_frames(FakeFleet().allocation_costs())["trend"]
        spec = _spec(charts.allocation_trend(trend, granularity=granularity))
        assert [row["label"] for row in _values(spec)] == labels
        assert spec["encoding"]["x"]["axis"]["labelAngle"] == angle
        assert spec["encoding"]["x"]["sort"] == {"field": "period", "order": "ascending"}
        assert "label" not in trend.columns

    def test_allocation_trend_defaults_to_days(self) -> None:
        trend = frames.allocation_frames(
            {"trend": [{"period": "2026-11-25T16:00Z", "total_cost": 1}]}
        )["trend"]
        assert [row["label"] for row in _values(_spec(charts.allocation_trend(trend)))] == [
            "Nov 25"
        ]


class TestTopologyDot:
    def test_request_path_to_every_region(self, region_rows: pd.DataFrame) -> None:
        dot = charts.topology_dot(
            region_rows, project="gco", control_region="us-east-2", api_region="us-east-2"
        )
        lines = dot.splitlines()
        assert (lines[0], lines[-1]) == ("digraph gco {", "}")
        assert '  api [label="Global API Gateway\\nus-east-2 · IAM (SigV4)", ' in dot
        assert (
            '  registry [label="gco-global · us-east-2\\njob queue, endpoint registry", '
            "shape=cylinder];"
        ) in lines
        assert '  you -> api [label="HTTPS"];' in lines
        healthy = brand.HEALTH_COLORS["healthy"]
        assert lines[-10:-1] == [
            f'  r_us_east_1 [label="gco-us-east-1\\nN. Virginia\\n1 running · 1 endpoint", '
            f'color="{healthy}", penwidth=2.2];',
            '  ga -> r_us_east_1 [label="internal ALB"];',
            '  agg -> r_us_east_1 [style=dashed, label="API bridge"];',
            f'  r_us_west_2 [label="gco-us-west-2\\nOregon\\n1 running · 1 endpoint", '
            f'color="{healthy}", penwidth=2.2];',
            '  ga -> r_us_west_2 [label="internal ALB"];',
            '  agg -> r_us_west_2 [style=dashed, label="API bridge"];',
            f'  r_eu_west_1 [label="gco-eu-west-1\\nIreland\\n0 running · 0 endpoints", '
            f'color="{healthy}", penwidth=2.2];',
            '  ga -> r_eu_west_1 [label="internal ALB"];',
            '  agg -> r_eu_west_1 [style=dashed, label="API bridge"];',
        ]

    @pytest.mark.parametrize(
        ("dark", "ink", "panel", "line"),
        [(True, "#E8ECF3", "#141A26", "#475569"), (False, "#171B26", "#F4F6FA", "#94A3B8")],
    )
    def test_theme_palettes(
        self, region_rows: pd.DataFrame, dark: bool, ink: str, panel: str, line: str
    ) -> None:
        dot = charts.topology_dot(
            region_rows,
            project="gco",
            control_region="us-east-2",
            api_region="us-east-2",
            dark=dark,
        )
        assert f'fillcolor="{panel}", color="{line}", fontcolor="{ink}"' in dot
        assert f'  edge [color="{line}", fontcolor="{ink}",' in dot

    def test_names_are_escaped_and_node_ids_sanitized(self) -> None:
        rows = pd.DataFrame(
            [{"region": "local.zone-1", "city": 'Say "hi"', "health": "mystery", "running": 3}]
        )
        dot = charts.topology_dot(
            rows, project='a"b\\c', control_region="us-east-2", api_region="eu-west-1"
        )
        assert '  registry [label="a\\"b\\\\c-global · us-east-2\\n' in dot
        assert (
            '  r_local_zone_1 [label="a\\"b\\\\c-local.zone-1\\nSay \\"hi\\"\\n'
            f'3 running · 0 endpoints", color="{brand.MUTED}", penwidth=2.2];'
        ) in dot.splitlines()
        assert "Global API Gateway\\neu-west-1" in dot

    def test_missing_fields_read_as_nothing(self) -> None:
        dot = charts.topology_dot(
            pd.DataFrame([{"region": "us-east-1"}]),
            project="gco",
            control_region="us-east-2",
            api_region="us-east-2",
        )
        assert (
            '  r_us_east_1 [label="gco-us-east-1\\n\\n0 running · 0 endpoints", '
            f'color="{brand.MUTED}", penwidth=2.2];'
        ) in dot.splitlines()

    def test_no_regions_is_still_a_graph(self) -> None:
        empty = frames.regions_frame(topology(workload_regions=()), None, [])
        dot = charts.topology_dot(
            empty, project="gco", control_region="us-east-2", api_region="us-east-2"
        )
        lines = dot.splitlines()
        assert (lines[0], lines[-1]) == ("digraph gco {", "}")
        assert not [line for line in lines if line.startswith("  r_")]
        assert all(line.endswith(";") for line in lines[1:-1])

    @pytest.mark.parametrize(
        ("count", "text"), [(0, "0 endpoints"), (1, "1 endpoint"), (2, "2 endpoints")]
    )
    def test_count(self, count: int, text: str) -> None:
        assert charts._count(count, "endpoint") == text

    def test_dot_text_escapes_quotes_and_backslashes(self) -> None:
        assert charts._dot_text('C:\\x "y"') == 'C:\\\\x \\"y\\"'
        assert charts._dot_text("plain") == "plain"
