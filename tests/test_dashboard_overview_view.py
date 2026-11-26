"""The Fleet page (``cli.dashboard.views.overview``) rendered through the dashboard's entrypoint.

Each test starts the real ``app.py`` under Streamlit's AppTest. The Fleet is
the default page, so the first run renders it against the ``fake_fleet``
fixture's canned fleet (three workload Regions). The tests check what an
operator sees: the headline badge and KPI strip, the map and the Region
cards, the auto-refreshing live panel, the utilization tabs, the findings and
the latest jobs, and how each part reads when its source fails or is empty.
The page's pure helpers (``_kpis``, ``_gpu_share``, ``_gpu_metric``,
``_series``, ``_gauge``, ``_gpu_gauge``) are unit-tested directly, including
how GPU allocation reads for Regions without GPUs and for health monitors
that send no GPU counts.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, call

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from cli.dashboard import charts, frames, ui
from cli.dashboard.data import DashboardError, JobsSnapshot, RegionRead, UtilizationRows
from cli.dashboard.views import overview
from cli.status import SECTION_ADDONS, Section, derive_findings
from tests._dashboard_fakes import (  # noqa: F401
    APP,
    NOW,
    REGIONS,
    FakeFleet,
    fake_fleet_fixture,
    fleet_status_document,
    job_info,
    metrics_read,
    sample_endpoint,
    sample_jobs,
    topology,
)

LABELS = ["us-east-1 · N. Virginia", "us-west-2 · Oregon", "eu-west-1 · Ireland"]
NO_JOBS = "No Kubernetes Jobs in the selected Regions."


class _FrozenClock(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return datetime(2026, 11, 25, 18, 4, 5, tzinfo=UTC)


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every "read at" and "updated at" time on the page reads 18:04:05 UTC."""
    monkeypatch.setattr(ui, "datetime", _FrozenClock)
    monkeypatch.setattr(overview, "datetime", _FrozenClock)


def open_fleet(
    state: dict[str, Any] | None = None, context: dict[str, Any] | None = None
) -> AppTest:
    """The Fleet page after its first run, with ``state`` and ``context`` set before it."""
    app = AppTest.from_file(str(APP), default_timeout=60)
    for key, value in (state or {}).items():
        app.session_state[key] = value
    app.context.update(context or {})
    app.run()
    assert not app.exception, [e.message for e in app.exception]
    return app


def values(elements: Any) -> list[Any]:
    return [element.value for element in elements]


def metrics(app: AppTest) -> dict[str, tuple[str, str]]:
    return {metric.label: (metric.value, metric.delta) for metric in app.metric}


def headline(app: AppTest) -> list[str]:
    """The headline badge (Region cards carry badges too, without an icon)."""
    return [m.value for m in app.markdown if "-badge[:material/" in m.value]


def cards(app: AppTest) -> list[Any]:
    [grid] = app.get("grid_container")
    return list(grid.children.values())


def card_summary(card: Any) -> dict[str, Any]:
    return {
        "markdown": values(card.markdown),
        "gauges": [(bar.value, bar.proto.text) for bar in card.get("progress")],
        "captions": values(card.caption),
    }


def deck(app: AppTest) -> dict[str, Any]:
    [chart] = app.get("deck_gl_json_chart")
    spec: dict[str, Any] = json.loads(chart.proto.json)
    return spec


def layer(spec: dict[str, Any], key: str) -> dict[str, Any]:
    return next(item for item in spec["layers"] if key in (item.get("id"), item["@@type"]))


def column(app: AppTest, subheader: str) -> Any:
    return next(block for block in app.columns if subheader in values(block.subheader))


def utilization_tabs(app: AppTest) -> list[tuple[str, str]]:
    """Each utilization tab's label and the y-axis title of the chart in it."""
    tabs = []
    for tab in app.tabs:
        [chart] = tab.get("vega_lite_chart")
        spec = json.loads(chart.proto.spec)
        tabs.append((tab.label, spec["layer"][0]["encoding"]["y"]["title"]))
    return tabs


def spy_fragment(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record the ``run_every`` of every ``st.fragment`` the page declares."""
    real = st.fragment
    seen: list[Any] = []

    def fragment(func: Any = None, *, run_every: Any = None) -> Any:
        seen.append(run_every)
        return real(func, run_every=run_every)

    monkeypatch.setattr(st, "fragment", fragment)
    return seen


FINDINGS = [
    {"severity": "warn", "section": "queue", "message": "1 message in the us-west-2 DLQ"},
    {"severity": "error", "section": "stacks", "message": "gco-us-east-1 rolled back"},
    {"severity": "error", "section": "capacity", "message": "No GPU capacity in eu-west-1"},
]


class TestHeadline:
    def test_a_clean_fleet_reads_all_systems_healthy(
        self, fake_fleet: FakeFleet, frozen_clock: None
    ) -> None:
        app = open_fleet()
        assert values(app.title) == ["Fleet"]
        assert app.caption[0].value == "gco · 3 workload Regions · read 18:04:05 UTC"
        assert headline(app) == [":green-badge[:material/check_circle: All systems healthy]"]
        attention = column(app, "Needs attention")
        assert values(attention.success) == [
            "No findings: every section of `gco status` reads clean."
        ]
        assert not app.error
        assert not app.warning

    def test_errors_turn_the_badge_red_and_are_listed_before_warnings(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.overrides["fleet_status"] = fleet_status_document(findings=FINDINGS)
        app = open_fleet()
        assert headline(app) == [":red-badge[:material/error: 2 problems need attention]"]
        attention = column(app, "Needs attention")
        assert values(attention.error) == [
            "**capacity** · No GPU capacity in eu-west-1",
            "**stacks** · gco-us-east-1 rolled back",
        ]
        assert values(attention.warning) == ["**queue** · 1 message in the us-west-2 DLQ"]
        assert {e.icon for e in attention.error} == {":material/error:"}
        assert attention.warning[0].icon == ":material/warning:"
        assert not attention.success

    def test_one_error_is_one_problem_that_needs_attention(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["fleet_status"] = fleet_status_document(findings=FINDINGS[1:2])
        app = open_fleet()
        assert headline(app) == [":red-badge[:material/error: 1 problem needs attention]"]

    def test_a_failed_add_on_convergence_needs_attention(self, fake_fleet: FakeFleet) -> None:
        """Live, the base pass failed in every Region while this page read all healthy.

        The findings are the ones ``gco status`` derives from its ``addons`` section.
        """
        failure = "04-resource-quotas.yaml:ValidatingAdmissionPolicy/gco-jobs-container-gpu-limit"
        status = fleet_status_document()
        addons = status["sections"][SECTION_ADDONS]
        for entry in addons["data"]["by_region"].values():
            entry["addons"][0].update(status="failed", health="unhealthy", message=failure)
        section = Section(name=SECTION_ADDONS, status=addons["status"], data=addons["data"])
        status["findings"] = [
            dataclasses.asdict(finding) for finding in derive_findings({SECTION_ADDONS: section})
        ]
        fake_fleet.overrides["fleet_status"] = status

        app = open_fleet()

        assert headline(app) == [":red-badge[:material/error: 3 problems need attention]"]
        assert values(column(app, "Needs attention").error) == [
            f"**addons** · base-manifests failed in {region}: {failure}" for region in REGIONS
        ]

    def test_warnings_alone_leave_the_fleet_healthy(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["fleet_status"] = fleet_status_document(findings=FINDINGS[:1])
        app = open_fleet()
        assert headline(app) == [":orange-badge[:material/warning: Healthy with 1 warning]"]
        assert values(column(app, "Needs attention").warning) == [
            "**queue** · 1 message in the us-west-2 DLQ"
        ]

    def test_an_unreadable_status_document_reads_unavailable(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["fleet_status"] = DashboardError(
            "The status document could not be read.", hint="Run `gco status` for details."
        )
        app = open_fleet()
        assert headline(app) == [":gray-badge[:material/help: Fleet status unavailable]"]
        card = "**The status document could not be read.**\n\nRun `gco status` for details."
        # Once for the live panel, once for "Needs attention"; prefetch stays quiet.
        assert values(app.error) == [card, card]
        attention = column(app, "Needs attention")
        assert values(attention.error) == [card]
        assert not attention.success
        assert not attention.warning
        # Without the stacks section no Region can be called healthy.
        assert metrics(app)["Regions healthy"] == ("0 / 3", "")
        assert [card_summary(c)["markdown"][1] for c in cards(app)] == [":gray-badge[unknown]"] * 3


class TestKpiStrip:
    def test_counts_regions_jobs_waiting_work_gpus_inference_and_dead_letters(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = open_fleet()
        assert metrics(app) == {
            "Regions healthy": ("3 / 3", ""),
            "Running jobs": ("2", ""),
            "Waiting": ("3", ""),
            "GPU allocated": ("50%", "3 of 6 GPUs"),
            "Inference": ("1 endpoint", "2 replicas ready"),
            "Dead letters": ("1", "inspect the DLQ"),
        }

    def test_gpu_allocated_counts_the_fleets_gpus_and_skips_regions_without_any(
        self, fake_fleet: FakeFleet
    ) -> None:
        # One GPU each in us-east-1 and us-west-2, both requested; eu-west-1 has
        # no GPU nodes. The mean of the three percentages would read 67%.
        fake_fleet.overrides["cluster_metrics"] = [
            metrics_read("us-east-1", gpu=100.0, gpu_counts=(1, 1)),
            metrics_read("us-west-2", gpu=100.0, gpu_counts=(1, 1)),
            metrics_read("eu-west-1", gpu=0.0, gpu_counts=(0, 0)),
        ]
        app = open_fleet()
        assert metrics(app)["GPU allocated"] == ("100%", "2 of 2 GPUs")
        east, west, ireland = map(card_summary, cards(app))
        assert east["gauges"][0] == west["gauges"][0] == (100, "GPU 1/1")
        assert ireland["gauges"] == [(6, "CPU 6%"), (40, "Mem 40%")]
        assert ireland["captions"][0] == "No GPU nodes"

    def test_a_fleet_without_gpus_says_so(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["cluster_metrics"] = lambda regions: [
            metrics_read(region, gpu=0.0, gpu_counts=(0, 0)) for region in regions
        ]
        app = open_fleet()
        assert metrics(app)["GPU allocated"] == ("no GPUs", "")
        assert [card_summary(c)["captions"][0] for c in cards(app)] == ["No GPU nodes"] * 3
        app.run()
        # No percentage to chart either.
        gpu = next(m for m in app.metric if m.label == "GPU allocated")
        assert list(gpu.proto.chart_data) == []

    def test_a_monitor_without_gpu_counts_leaves_the_average_of_the_shares(
        self, fake_fleet: FakeFleet
    ) -> None:
        # us-west-2 runs a health monitor that predates the counts.
        fake_fleet.overrides["cluster_metrics"] = [
            metrics_read("us-east-1", gpu=100.0, gpu_counts=(1, 1)),
            metrics_read("us-west-2", gpu=50.0, gpu_counts=None),
            metrics_read("eu-west-1", gpu=0.0, gpu_counts=(0, 0)),
        ]
        app = open_fleet()
        # eu-west-1, which has no GPUs, is left out of the average.
        assert metrics(app)["GPU allocated"] == ("75%", "")
        west = card_summary(cards(app)[1])
        assert west["gauges"][0] == (50, "GPU 50%")

    def test_an_empty_dead_letter_queue_reads_clear(self, fake_fleet: FakeFleet) -> None:
        document = fleet_status_document()
        for counts in document["sections"]["queue"]["data"]["by_region"].values():
            counts["dlq"] = 0
        fake_fleet.overrides["fleet_status"] = document
        assert metrics(open_fleet())["Dead letters"] == ("0", "clear")

    def test_sparklines_appear_once_the_session_has_history(self, fake_fleet: FakeFleet) -> None:
        app = open_fleet()
        assert [list(m.proto.chart_data) for m in app.metric] == [[]] * 6
        app.run()
        sparklines = {m.label: list(m.proto.chart_data) for m in app.metric}
        assert sparklines == {
            "Regions healthy": [],
            "Running jobs": [2.0, 2.0],
            "Waiting": [3.0, 3.0],
            "GPU allocated": [50.0, 50.0],
            "Inference": [],
            "Dead letters": [],
        }

    def test_history_keeps_the_last_forty_readings(self, fake_fleet: FakeFleet) -> None:
        old = [{"running": n, "queued": 0, "gpu": None} for n in range(45)]
        app = open_fleet({overview.HISTORY_KEY: old})
        history = app.session_state[overview.HISTORY_KEY]
        assert len(history) == overview.HISTORY_LENGTH
        assert [h["running"] for h in history] == [*range(6, 45), 2]
        running = next(m for m in app.metric if m.label == "Running jobs")
        assert list(running.proto.chart_data) == [float(n) for n in [*range(6, 45), 2]]
        # Readings without a GPU number are skipped, leaving one: too few to draw.
        gpu = next(m for m in app.metric if m.label == "GPU allocated")
        assert list(gpu.proto.chart_data) == []


class TestMapAndRegionCards:
    @pytest.mark.parametrize(
        ("regions", "columns"),
        [
            (("us-east-1",), 1),
            (REGIONS, 3),
            ((*REGIONS, "ap-south-1", "ap-northeast-1", "sa-east-1"), 4),
        ],
        ids=["one", "three-fill-the-row", "six-wrap-at-four"],
    )
    def test_the_cards_take_one_column_each_up_to_four(
        self, fake_fleet: FakeFleet, regions: tuple[str, ...], columns: int
    ) -> None:
        fake_fleet.overrides["topology"] = topology(workload_regions=regions)
        app = open_fleet()
        [grid] = app.get("grid_container")
        assert len(cards(app)) == len(regions)
        assert grid.proto.columns == columns

    def test_the_map_wires_every_region_to_the_control_plane(self, fake_fleet: FakeFleet) -> None:
        app = open_fleet()
        spec = deck(app)
        assert spec["views"] == [{"@@type": "MapView", "controller": True}]
        assert [row["label"] for row in layer(spec, "regions")["data"]] == LABELS
        assert [row["label"] for row in layer(spec, "control-plane")["data"]] == [
            "us-east-2 · control plane"
        ]
        assert len(layer(spec, "ArcLayer")["data"]) == 3
        assert layer(spec, "PolygonLayer")["getFillColor"] == [30, 37, 52, 255]
        assert app.toggle(key="fleet_globe").value is False

    def test_the_globe_toggle_redraws_the_map_as_a_globe(self, fake_fleet: FakeFleet) -> None:
        app = open_fleet()
        app.toggle(key="fleet_globe").set_value(True).run()
        assert deck(app)["views"] == [{"@@type": "_GlobeView", "controller": True}]

    def test_a_light_browser_theme_draws_a_light_map(self, fake_fleet: FakeFleet) -> None:
        app = open_fleet(context={"theme": "light"})
        assert layer(deck(app), "PolygonLayer")["getFillColor"] == [222, 227, 235, 255]

    def test_each_card_shows_health_gauges_and_the_work_running_there(
        self, fake_fleet: FakeFleet
    ) -> None:
        summaries = [card_summary(card) for card in cards(open_fleet())]
        gauges = [(50, "GPU 1/2"), (6, "CPU 6%"), (40, "Mem 40%")]
        assert summaries == [
            {
                "markdown": ["**us-east-1** · N. Virginia", ":green-badge[healthy]"],
                "gauges": gauges,
                "captions": [
                    ":material/play_circle: 1 running · :material/hourglass_top: 2 waiting · "
                    ":material/smart_toy: 1 endpoint"
                ],
            },
            {
                "markdown": ["**us-west-2** · Oregon", ":green-badge[healthy]"],
                "gauges": gauges,
                "captions": [
                    ":material/play_circle: 1 running · :material/hourglass_top: 0 waiting · "
                    ":material/smart_toy: 1 endpoint"
                ],
            },
            {
                "markdown": ["**eu-west-1** · Ireland", ":green-badge[healthy]"],
                "gauges": gauges,
                "captions": [
                    ":material/play_circle: 0 running · :material/hourglass_top: 0 waiting · "
                    ":material/smart_toy: 0 endpoints"
                ],
            },
        ]

    def test_a_region_whose_monitor_did_not_answer_says_why(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["cluster_metrics"] = lambda regions: [
            RegionRead(region=r, error="Read timed out") if r == "eu-west-1" else metrics_read(r)
            for r in regions
        ]
        app = open_fleet()
        ireland = card_summary(cards(app)[2])
        assert ireland == {
            "markdown": ["**eu-west-1** · Ireland", ":orange-badge[degraded]"],
            "gauges": [],
            "captions": [
                "GPU —",
                "CPU —",
                "Mem —",
                ":material/play_circle: 0 running · :material/hourglass_top: 0 waiting · "
                ":material/smart_toy: 0 endpoints",
                ":material/warning: Read timed out",
            ],
        }
        assert metrics(app)["Regions healthy"] == ("2 / 3", "")
        # Only the Regions that answered count.
        assert metrics(app)["GPU allocated"] == ("50%", "2 of 4 GPUs")

    def test_a_region_whose_stack_could_not_be_read_is_unknown_not_degraded(
        self, fake_fleet: FakeFleet
    ) -> None:
        status = fleet_status_document()
        regional = status["sections"]["stacks"]["data"]["expected"][3]
        assert regional["name"] == "gco-eu-west-1"
        regional.update(status=None, health="unknown", updated_time=None)
        fake_fleet.overrides["fleet_status"] = status
        app = open_fleet()
        badges = [card_summary(c)["markdown"][1] for c in cards(app)]
        assert badges == [":green-badge[healthy]", ":green-badge[healthy]", ":gray-badge[unknown]"]
        assert metrics(app)["Regions healthy"] == ("2 / 3", "")

    def test_without_any_monitor_readings_the_gpu_share_is_unknown(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["cluster_metrics"] = DashboardError("The monitors are unreachable.")
        app = open_fleet()
        assert values(app.error) == ["**The monitors are unreachable.**"]
        assert metrics(app)["GPU allocated"] == ("—", "")
        assert metrics(app)["Regions healthy"] == ("0 / 3", "")
        assert all(card_summary(c)["gauges"] == [] for c in cards(app))

    def test_regions_whose_jobs_could_not_be_read_are_called_out(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.overrides["jobs"] = JobsSnapshot(
            [job_info("train-1", "us-east-1", "running")],
            {"eu-west-1": "HTTP 504 from the regional API"},
            3,
            1,
            "global",
        )
        app = open_fleet()
        assert values(app.warning) == [
            "Jobs in eu-west-1 could not be read: HTTP 504 from the regional API"
        ]
        assert metrics(app)["Running jobs"] == ("1", "")

    def test_an_unreadable_endpoint_registry_is_reported_once(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["endpoints"] = DashboardError("The endpoint registry is unreachable.")
        app = open_fleet()
        assert values(app.error) == ["**The endpoint registry is unreachable.**"]
        assert metrics(app)["Inference"] == ("0 endpoints", "0 replicas ready")
        assert all(c["captions"][-1].endswith("0 endpoints") for c in map(card_summary, cards(app)))

    def test_the_region_filter_narrows_every_panel(self, fake_fleet: FakeFleet) -> None:
        app = open_fleet({ui.REGION_FILTER_KEY: ["us-west-2"]})
        assert [card_summary(c)["markdown"][0] for c in cards(app)] == ["**us-west-2** · Oregon"]
        assert [row["label"] for row in layer(deck(app), "regions")["data"]] == [
            "us-west-2 · Oregon"
        ]
        assert metrics(app)["Regions healthy"] == ("1 / 1", "")
        assert fake_fleet.called("cluster_metrics") == [((("us-west-2",),), {})]
        assert fake_fleet.called("utilization_history") == [
            ((("us-west-2",),), {"hours": 3, "period": 60})
        ]
        assert app.dataframe[0].value["region"].unique().tolist() == ["us-west-2"]


class TestLivePanel:
    def test_with_auto_refresh_the_live_panel_reruns_on_the_sidebar_interval(
        self, fake_fleet: FakeFleet, frozen_clock: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_every = spy_fragment(monkeypatch)
        app = open_fleet()
        app.sidebar.toggle(key=ui.AUTO_REFRESH_KEY).set_value(True).run()
        app.sidebar.select_slider(key=ui.REFRESH_SECONDS_KEY).set_value(15).run()
        assert run_every == [None, 30, 15]
        assert "Updated 18:04:05 UTC · refreshing every 15 s" in values(app.caption)
        assert not app.warning

    def test_without_auto_refresh_the_live_panel_stays_put(
        self, fake_fleet: FakeFleet, frozen_clock: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_every = spy_fragment(monkeypatch)
        app = open_fleet()
        assert run_every == [None]
        assert "Updated 18:04:05 UTC" in values(app.caption)

    def test_the_live_reads_are_made_once_per_run(self, fake_fleet: FakeFleet) -> None:
        open_fleet()
        for method in ("fleet_status", "cluster_metrics", "jobs", "endpoints", "queue_stats"):
            assert len(fake_fleet.called(method)) == 1, method


class TestUtilization:
    def test_one_tab_per_metric_over_the_last_three_hours(self, fake_fleet: FakeFleet) -> None:
        app = open_fleet()
        assert app.segmented_control(key=overview.HOURS_KEY).value == 3
        assert utilization_tabs(app) == [
            ("GPU", "GPU allocated %"),
            ("CPU", "CPU requested %"),
            ("Memory", "Memory requested %"),
            ("Active jobs", "Active jobs"),
        ]
        assert fake_fleet.called("utilization_history") == [
            ((REGIONS,), {"hours": 3, "period": 60})
        ]

    def test_a_day_long_window_reads_five_minute_samples(self, fake_fleet: FakeFleet) -> None:
        app = open_fleet()
        app.segmented_control(key=overview.HOURS_KEY).set_value(24).run()
        assert fake_fleet.called("utilization_history")[-1] == (
            (REGIONS,),
            {"hours": 24, "period": 300},
        )
        assert len(app.tabs) == 4

    def test_regions_whose_history_could_not_be_read_are_named_above_the_others_charts(
        self, fake_fleet: FakeFleet
    ) -> None:
        samples = [
            {
                "region": "us-east-1",
                "metric": metric,
                "timestamp": NOW - timedelta(minutes=minute),
                "value": 10.0 + minute,
            }
            for metric in ("cpu", "memory", "gpu", "active_jobs")
            for minute in (0, 5)
        ]
        denied = (
            "AWS denied the request: not authorized to perform: cloudwatch:GetMetricData. "
            "The signed-in principal lacks a permission this view needs."
        )
        fake_fleet.overrides["utilization_history"] = UtilizationRows(
            samples, {"us-west-2": denied, "eu-west-1": "Throttling: Rate exceeded"}
        )
        app = open_fleet()
        assert values(app.warning) == [
            f"Utilization in us-west-2 could not be read: {denied}",
            "Utilization in eu-west-1 could not be read: Throttling: Rate exceeded",
        ]
        assert {warning.icon for warning in app.warning} == {":material/warning:"}
        assert [label for label, _ in utilization_tabs(app)] == [
            "GPU",
            "CPU",
            "Memory",
            "Active jobs",
        ]
        assert not app.error

    @staticmethod
    def spy_lines(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
        """The Regions each utilization chart is drawn with, by metric."""
        real = charts.utilization_lines
        drawn: dict[str, list[str]] = {}

        def lines(series: pd.DataFrame, metric: str) -> Any:
            drawn[metric] = sorted(series.loc[series["metric"] == metric, "region"].unique())
            return real(series, metric)

        monkeypatch.setattr(charts, "utilization_lines", lines)
        return drawn

    def test_a_region_without_gpu_nodes_is_left_off_the_gpu_chart(
        self, fake_fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its 0% is not an idle GPU; it still has CPU, memory and job lines."""
        fake_fleet.overrides["cluster_metrics"] = lambda regions: [
            metrics_read(region, gpu_counts=(0, 0) if region == "eu-west-1" else (2, 1))
            for region in regions
        ]
        drawn = self.spy_lines(monkeypatch)
        app = open_fleet()
        assert drawn["gpu"] == ["us-east-1", "us-west-2"]
        assert drawn["cpu"] == drawn["memory"] == drawn["active_jobs"] == sorted(REGIONS)
        assert values(app.tabs[0].caption) == ["No GPU nodes in eu-west-1."]

    def test_a_fleet_without_gpu_nodes_draws_no_gpu_chart(
        self, fake_fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_fleet.overrides["cluster_metrics"] = lambda regions: [
            metrics_read(region, gpu_counts=(0, 0)) for region in regions
        ]
        drawn = self.spy_lines(monkeypatch)
        app = open_fleet()
        gpu_tab = app.tabs[0]
        assert "gpu" not in drawn
        assert not gpu_tab.get("vega_lite_chart")
        assert values(gpu_tab.info) == ["No Region has GPU nodes right now."]
        assert values(gpu_tab.caption) == ["No GPU nodes in eu-west-1, us-east-1, us-west-2."]

    @pytest.mark.parametrize("cause", ["unreadable", "older-monitor", "gpus-requested"])
    def test_without_counts_showing_no_gpus_every_region_keeps_its_gpu_line(
        self, fake_fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch, cause: str
    ) -> None:
        if cause == "unreadable":
            # The live panel reports the failed read; the chart says nothing more.
            fake_fleet.fail["cluster_metrics"] = DashboardError("The health monitors timed out.")
        else:
            counts = None if cause == "older-monitor" else (0, 1)
            fake_fleet.overrides["cluster_metrics"] = lambda regions: [
                metrics_read(region, gpu_counts=counts) for region in regions
            ]
        drawn = self.spy_lines(monkeypatch)
        app = open_fleet()
        assert drawn["gpu"] == sorted(REGIONS)
        assert not values(app.tabs[0].caption)

    def test_no_samples_yet(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["utilization_history"] = []
        app = open_fleet()
        assert values(app.info) == [
            "No utilization samples yet: the health monitors publish one a minute."
        ]
        assert not app.tabs

    def test_an_unreadable_history_shows_the_error_and_no_charts(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["utilization_history"] = DashboardError("GetMetricData was throttled.")
        app = open_fleet()
        assert values(app.error) == ["**GetMetricData was throttled.**"]
        assert not app.tabs
        assert not app.info


class TestLatestJobs:
    def test_lists_the_eight_newest_jobs_and_links_to_the_jobs_page(
        self, fake_fleet: FakeFleet
    ) -> None:
        jobs = [job_info(f"job-{n}", "us-east-1", "running", minutes_ago=n) for n in range(1, 11)]
        fake_fleet.overrides["jobs"] = JobsSnapshot(jobs, {}, 3, len(jobs), "global")
        app = open_fleet()
        recent = column(app, "Latest jobs")
        [table] = recent.dataframe
        assert list(table.value.columns) == ["name", "region", "status", "created", "runtime_s"]
        assert table.value["name"].tolist() == [f"job-{n}" for n in range(1, 9)]
        [link] = recent.get("page_link")
        assert (link.proto.label, link.proto.icon) == ("All jobs", ":material/arrow_forward:")

    def test_no_jobs_in_the_selected_regions(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["jobs"] = JobsSnapshot([], {}, 3, 0, "global")
        app = open_fleet()
        assert NO_JOBS in values(column(app, "Latest jobs").caption)
        assert not app.dataframe
        assert metrics(app)["Running jobs"] == ("0", "")

    def test_unreadable_jobs_read_as_none(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["jobs"] = DashboardError("The global API timed out.")
        app = open_fleet()
        recent = column(app, "Latest jobs")
        assert values(recent.error) == ["**The global API timed out.**"]
        assert NO_JOBS in values(recent.caption)
        assert not app.warning
        assert metrics(app)["Running jobs"] == ("0", "")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_kpis_of_an_empty_fleet() -> None:
    kpis = overview._kpis(
        frames.regions_frame(topology(), None, []),
        frames.jobs_frame([]),
        frames.endpoints_frame([]),
        {},
    )
    assert kpis == {
        "healthy": 0,
        "regions": 3,
        "running": 0,
        "failed": 0,
        "queued": 0,
        "gpu": None,
        "gpu_requested": None,
        "gpu_allocatable": None,
        "endpoints": 0,
        "replicas": 0,
        "dlq": 0,
    }


def test_kpis_count_across_the_frames() -> None:
    jobs = frames.jobs_frame(sample_jobs())
    regions = frames.regions_frame(
        topology(),
        fleet_status_document(),
        [
            metrics_read("us-east-1", gpu=25.0, gpu_counts=(4, 1)),
            metrics_read("us-west-2", gpu=91.7, gpu_counts=(12, 11)),
        ],
        jobs=jobs,
    )
    endpoints = frames.endpoints_frame(
        [sample_endpoint(), sample_endpoint(endpoint_name="old", desired_state="stopped")]
    )
    kpis = overview._kpis(regions, jobs, endpoints, {"summary": {"total_queued": 4}})
    assert kpis == {
        "healthy": 2,
        "regions": 3,
        "running": 2,
        "failed": 1,
        "queued": 6,
        # 12 of 16 GPUs: the larger Region weighs more (the mean would be 58%).
        "gpu": 75.0,
        "gpu_requested": 12,
        "gpu_allocatable": 16,
        "endpoints": 1,
        "replicas": 4,
        "dlq": 1,
    }


def _region_rows(*rows: tuple[float | None, int | None, int | None]) -> pd.DataFrame:
    """Region rows holding just ``(gpu, gpu_allocatable, gpu_requested)``."""
    return pd.DataFrame(list(rows), columns=["gpu", "gpu_allocatable", "gpu_requested"])


@pytest.mark.parametrize(
    ("rows", "share"),
    [
        ((), (None, None, None)),
        (((None, None, None),), (None, None, None)),
        (((100.0, 1, 1), (100.0, 1, 1), (0.0, 0, 0)), (100.0, 2, 2)),
        (((100.0, 1, 1), (0.0, 99, 0)), (1.0, 1, 100)),
        (((50.0, 2, 1), (None, None, None)), (50.0, 1, 2)),
        (((0.0, 0, 0), (0.0, 0, 0)), (None, 0, 0)),
        (((0.0, 0, 2),), (None, 2, 0)),
        (((100.0, 1, 1), (50.0, None, None), (0.0, 0, 0)), (75.0, None, None)),
        (((40.0, None, None), (20.0, None, None)), (30.0, None, None)),
    ],
    ids=[
        "no-regions",
        "no-answers",
        "a-region-without-gpus-adds-nothing",
        "weighted-by-gpus",
        "unanswered-regions-are-left-out",
        "no-gpus",
        "requests-without-allocatable-gpus",
        "a-monitor-without-counts",
        "no-monitor-reports-counts",
    ],
)
def test_gpu_share(rows: tuple[Any, ...], share: tuple[Any, Any, Any]) -> None:
    assert overview._gpu_share(_region_rows(*rows)) == share


@pytest.mark.parametrize(
    ("gpu", "requested", "allocatable", "shown"),
    [
        (50.0, 3, 6, ("50%", "3 of 6 GPUs")),
        (100.0, 1, 1, ("100%", "1 of 1 GPU")),
        (None, 0, 0, ("no GPUs", None)),
        (None, 2, 0, ("—", "2 of 0 GPUs")),
        (75.0, None, None, ("75%", None)),
        (None, None, None, ("—", None)),
    ],
    ids=["some", "one", "no-gpus", "requests-only", "average", "unknown"],
)
def test_gpu_metric(gpu: Any, requested: Any, allocatable: Any, shown: Any) -> None:
    kpis = {"gpu": gpu, "gpu_requested": requested, "gpu_allocatable": allocatable}
    assert overview._gpu_metric(kpis) == shown


NAN = float("nan")


@pytest.mark.parametrize(
    ("gpu", "allocatable", "requested", "drawn"),
    [
        (50.0, 2, 1, call.progress(0.5, text="GPU 1/2")),
        (100.0, 1.0, 1.0, call.progress(1.0, text="GPU 1/1")),
        (0.0, 4, 0, call.progress(0.0, text="GPU 0/4")),
        (0.0, 0, 0, call.caption("No GPU nodes")),
        (300.0, 1, 3, call.progress(1.0, text="GPU 3/1")),
        (0.0, 0, 2, call.progress(1.0, text="GPU 2/0")),
        (75.0, NAN, NAN, call.progress(0.75, text="GPU 75%")),
        (None, None, None, call.caption("GPU —")),
    ],
    ids=[
        "half",
        "float-cells",
        "idle",
        "no-gpu-nodes",
        "over-committed",
        "requests-only",
        "no-counts",
        "no-reading",
    ],
)
def test_gpu_gauge(gpu: Any, allocatable: Any, requested: Any, drawn: Any) -> None:
    where = MagicMock()
    row = {"gpu": gpu, "gpu_allocatable": allocatable, "gpu_requested": requested}
    overview._gpu_gauge(where, row)
    assert where.method_calls == [drawn]


@pytest.mark.parametrize(
    ("history", "series"),
    [
        ([], None),
        ([{"gpu": 5}], None),
        ([{"gpu": 5}, {}], None),
        ([{"gpu": None}, {"gpu": 5}, {"gpu": 7.5}], [5.0, 7.5]),
    ],
    ids=["empty", "one-reading", "one-with-a-number", "skips-missing"],
)
def test_series_needs_two_readings(history: list[dict[str, Any]], series: Any) -> None:
    assert overview._series(history, "gpu") == series


@pytest.mark.parametrize(
    ("value", "drawn"),
    [
        (25, call.progress(0.25, text="GPU 25%")),
        (0, call.progress(0.0, text="GPU 0%")),
        (250.0, call.progress(1.0, text="GPU 250%")),
        (None, call.caption("GPU —")),
        (float("nan"), call.caption("GPU —")),
        ("n/a", call.caption("GPU —")),
    ],
    ids=["quarter", "zero", "over-full", "missing", "nan", "text"],
)
def test_gauge(value: object, drawn: Any) -> None:
    where = MagicMock()
    overview._gauge(where, "GPU", value)
    assert where.method_calls == [drawn]
