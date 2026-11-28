"""The Capacity page (``cli.dashboard.views.capacity``) rendered through the dashboard's entrypoint.

Each test starts the real ``app.py`` under Streamlit's AppTest, switches to
the Capacity page and checks its three panels against the ``fake_fleet``
fixture's canned fleet: each Region's load from the status document, with
the health monitors' GPU counts beside it (a Region without GPU nodes shows
no GPU use), the placement recommendation (asked for with a button), and the
instance explorer (offerings and prices across Regions, Spot price history,
and Spot placement scores, which run only on a button and are cached for an
hour). Every panel is also checked with its read failing or coming back empty.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from cli.dashboard import ui
from cli.dashboard.data import DashboardError, RegionRead
from cli.dashboard.views import capacity
from tests._dashboard_fakes import (  # noqa: F401
    APP,
    REGIONS,
    FakeFleet,
    fake_fleet_fixture,
    metrics_read,
)

CAPACITY_PAGE = "app_pages/capacity.py"
EAST, WEST, IRELAND = "us-east-1 · N. Virginia", "us-west-2 · Oregon", "eu-west-1 · Ireland"
SCORING = (
    "Scores each Region on its SQS backlog, GPU allocation and active jobs; with an instance "
    "type, also on its Spot placement score, Spot discount and Capacity Block trend."
)
NO_LOAD = "No capacity readings in the status document."


def open_capacity(state: dict[str, Any] | None = None) -> AppTest:
    """The Capacity page after one run, with ``state`` put in the session first."""
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.run()
    app.switch_page(CAPACITY_PAGE)
    for key, value in (state or {}).items():
        app.session_state[key] = value
    app.run()
    assert not app.exception, [e.message for e in app.exception]
    return app


def values(elements: Any) -> list[Any]:
    return [element.value for element in elements]


def metrics(app: AppTest) -> dict[str, tuple[str, str]]:
    return {metric.label: (metric.value, metric.delta) for metric in app.metric}


def table_with(app: AppTest, column: str) -> Any:
    """The dataframe holding ``column`` (each panel's table has its own columns)."""
    return next(frame.value for frame in app.dataframe if column in frame.value.columns)


def has_table_with(app: AppTest, column: str) -> bool:
    return any(column in frame.value.columns for frame in app.dataframe)


def chart_count(app: AppTest) -> int:
    return len(app.get("vega_lite_chart"))


def report(changes: dict[str, dict[str, Any]]) -> Any:
    """An ``instance_report`` answer: the fake's readings with ``changes`` per Region."""

    def answer(instance_type: str, regions: tuple[str, ...]) -> list[RegionRead]:
        reads = FakeFleet().instance_report(instance_type, regions)
        return [RegionRead(r.region, {**r.data, **changes.get(r.region, {})}) for r in reads]

    return answer


class TestLoad:
    def test_lists_each_regions_capacity_signals(self, fake_fleet: FakeFleet) -> None:
        app = open_capacity()
        assert values(app.title) == ["Capacity"]
        table = table_with(app, "telemetry")
        assert table["region"].tolist() == [EAST, WEST]
        assert table["gpu"].tolist() == [40.0, 0.0]
        # The health monitors' counts: 1 of 2 GPUs requested in each Region.
        assert table["gpu_allocatable"].tolist() == [2, 2]
        assert table["gpu_requested"].tolist() == [1, 1]
        assert fake_fleet.called("cluster_metrics") == [((REGIONS,), {})]
        assert table["cpu"].tolist() == [6.5, 7.0]
        assert table["queue_depth"].tolist() == [2, 0]
        assert table["running_jobs"].tolist() == [2, 1]
        assert table["telemetry"].tolist() == ["complete", "partial"]
        assert [list(missing) for missing in table["missing"]] == [[], ["gpu"]]
        assert NO_LOAD not in values(app.caption)

    def test_a_region_without_gpu_nodes_shows_no_gpu_use(self, fake_fleet: FakeFleet) -> None:
        # The status document reads 0% for us-west-2; its health monitor counts no GPUs.
        fake_fleet.overrides["cluster_metrics"] = [
            metrics_read("us-east-1", gpu=100.0, gpu_counts=(1, 1)),
            metrics_read("us-west-2", gpu=0.0, gpu_counts=(0, 0)),
        ]
        table = table_with(open_capacity(), "telemetry")
        assert table["gpu"].iloc[0] == 40.0
        assert pd.isna(table["gpu"].iloc[1])
        assert table["gpu_allocatable"].tolist() == [1, 0]
        assert table["gpu_requested"].tolist() == [1, 0]

    def test_without_the_health_monitors_the_counts_are_empty(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["cluster_metrics"] = DashboardError("The monitors are unreachable.")
        app = open_capacity()
        assert values(app.error) == ["**The monitors are unreachable.**"]
        table = table_with(app, "telemetry")
        assert table["gpu"].tolist() == [40.0, 0.0]
        assert table[["gpu_allocatable", "gpu_requested"]].isna().all().all()

    def test_regions_outside_the_filter_are_left_out(self, fake_fleet: FakeFleet) -> None:
        app = open_capacity({ui.REGION_FILTER_KEY: ["eu-west-1"]})
        assert NO_LOAD in values(app.caption)
        assert not has_table_with(app, "telemetry")

    def test_an_unreadable_status_document(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["fleet_status"] = DashboardError("The status document timed out.")
        app = open_capacity()
        assert values(app.error) == ["**The status document timed out.**"]
        assert NO_LOAD in values(app.caption)


class TestRecommend:
    def test_before_asking_it_explains_the_scoring(self, fake_fleet: FakeFleet) -> None:
        app = open_capacity()
        assert SCORING in values(app.caption)
        assert app.code[0].value == "gco capacity recommend-region --gpu-count 1"
        assert app.segmented_control(key="reco_backend").options == ["EKS", "HyperPod", "PCS"]
        assert app.selectbox(key="reco_type").options[0] == "Any (score on cluster load only)"
        assert not app.success
        assert fake_fleet.called("recommend_region") == []

    def test_recommend_ranks_every_region(self, fake_fleet: FakeFleet) -> None:
        app = open_capacity()
        charts_before = chart_count(app)
        app.button(key="reco_go").click().run()
        assert fake_fleet.called("recommend_region") == [
            (((),), {"gpu_count": 1, "instance_type": None, "backend": "eks"})
        ]
        assert values(app.success) == ["**us-west-2 · Oregon** · empty queue, 100% GPU available"]
        assert app.success[0].icon == ":material/near_me:"
        ranking = table_with(app, "rank")
        assert "recommended" not in ranking.columns
        assert ranking["rank"].tolist() == [1, 2]
        assert ranking["region"].tolist() == ["us-west-2", "us-east-1"]
        assert ranking["score"].tolist() == [0.12, 0.4]
        assert chart_count(app) == charts_before + 1
        assert SCORING not in values(app.caption)
        assert app.code[0].value == "gco capacity recommend-region --gpu-count 1"

    def test_gpus_instance_type_and_backend_reach_the_api_and_the_hint(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = open_capacity()
        app.number_input(key="reco_gpus").set_value(4)
        app.selectbox(key="reco_type").set_value("g5.xlarge")
        app.segmented_control(key="reco_backend").set_value("hyperpod")
        app.button(key="reco_go").click().run()
        assert fake_fleet.called("recommend_region") == [
            (((),), {"gpu_count": 4, "instance_type": "g5.xlarge", "backend": "hyperpod"})
        ]
        assert app.code[0].value == (
            "gco capacity recommend-region --gpu-count 4 -i g5.xlarge --backend hyperpod"
        )
        assert len(app.success) == 1

    def test_partial_telemetry_is_noted(self, fake_fleet: FakeFleet) -> None:
        answer = FakeFleet().recommend_region()
        answer["telemetry_errors"] = ["us-east-1: no DCGM samples", "eu-west-1: timed out"]
        fake_fleet.overrides["recommend_region"] = answer
        app = open_capacity()
        app.button(key="reco_go").click().run()
        assert (
            ":material/info: Partial telemetry: us-east-1: no DCGM samples; eu-west-1: timed out"
            in values(app.caption)
        )

    def test_an_answer_without_a_ranking_shows_only_the_pick(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["recommend_region"] = {
            "region": "eu-west-1",
            "reason": "the only Region with free GPUs",
            "all_regions": [],
        }
        app = open_capacity()
        charts_before = chart_count(app)
        app.button(key="reco_go").click().run()
        assert values(app.success) == ["**eu-west-1 · Ireland** · the only Region with free GPUs"]
        assert not has_table_with(app, "rank")
        assert chart_count(app) == charts_before
        assert not any(
            c.startswith(":material/info: Partial telemetry") for c in values(app.caption)
        )

    def test_a_failed_recommendation_shows_the_error_and_the_scoring(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["recommend_region"] = DashboardError(
            "The global API timed out.", hint="Try again."
        )
        app = open_capacity()
        app.button(key="reco_go").click().run()
        assert values(app.error) == ["**The global API timed out.**\n\nTry again."]
        assert not app.success
        assert SCORING in values(app.caption)
        assert app.session_state[capacity.RECOMMEND_KEY] == ((1, "", "eks"), None)

    def test_changing_the_request_hides_the_old_answer(self, fake_fleet: FakeFleet) -> None:
        app = open_capacity()
        app.button(key="reco_go").click().run()
        assert len(app.success) == 1
        app.number_input(key="reco_gpus").set_value(8).run()
        assert not app.success
        assert SCORING in values(app.caption)
        assert app.code[0].value == "gco capacity recommend-region --gpu-count 8"
        assert len(fake_fleet.called("recommend_region")) == 1


class TestExplorer:
    def test_summarises_the_instance_type_where_it_is_offered(self, fake_fleet: FakeFleet) -> None:
        app = open_capacity()
        assert app.selectbox(key="explore_type").value == "g7.2xlarge"
        assert fake_fleet.called("instance_report") == [(("g7.2xlarge", REGIONS), {})]
        assert metrics(app) == {
            "vCPUs": ("8", ""),
            "Memory": ("32 GiB", ""),
            "Accelerators": ("1 L4", ""),
            "Cheapest Spot": ("$0.5000/h", EAST),
            "Best Spot discount": ("80%", EAST),
        }
        by_region = table_with(app, "offered")
        assert list(by_region.columns) == [
            "region",
            "city",
            "offered",
            "on_demand",
            "spot",
            "savings",
            "stability",
            "error",
        ]
        assert by_region["region"].tolist() == list(REGIONS)
        assert by_region["offered"].tolist() == [True, True, False]
        assert "**On-Demand against Spot**" in values(app.markdown)
        assert chart_count(app) == 2

    def test_spot_history_for_the_chosen_region_and_window(self, fake_fleet: FakeFleet) -> None:
        app = open_capacity()
        region = app.segmented_control(key="explore_region")
        assert region.options == [EAST, WEST]
        assert region.value == "us-east-1"
        assert fake_fleet.called("spot_price_history") == [
            (("g7.2xlarge", "us-east-1"), {"days": 7})
        ]
        assert "**Spot price, last 7 days**" in values(app.markdown)
        assert "Click a zone in the legend to highlight it." in values(app.caption)
        assert app.code[-1].value == (
            "gco capacity instance-info g7.2xlarge\n"
            "gco capacity spot-prices -i g7.2xlarge -r us-east-1 -d 7"
        )

        region.set_value("us-west-2")
        app.segmented_control(key="explore_days").set_value(30)
        app.run()
        assert fake_fleet.called("spot_price_history")[-1] == (
            ("g7.2xlarge", "us-west-2"),
            {"days": 30},
        )
        assert "**Spot price, last 30 days**" in values(app.markdown)
        assert app.code[-1].value.endswith("spot-prices -i g7.2xlarge -r us-west-2 -d 30")

    def test_another_instance_type_is_read_everywhere(self, fake_fleet: FakeFleet) -> None:
        app = open_capacity()
        app.selectbox(key="explore_type").set_value("p5.48xlarge").run()
        assert fake_fleet.called("instance_report")[-1] == (("p5.48xlarge", REGIONS), {})
        assert app.code[-1].value.startswith("gco capacity instance-info p5.48xlarge\n")

    def test_no_spot_price_changes_in_the_window(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["spot_price_history"] = []
        app = open_capacity()
        assert "No Spot price changes recorded in this window." in values(app.caption)
        assert "Click a zone in the legend to highlight it." not in values(app.caption)
        assert chart_count(app) == 1

    def test_an_unreadable_report_stops_the_explorer(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["instance_report"] = DashboardError("DescribeInstanceTypes was denied.")
        app = open_capacity()
        assert values(app.error) == ["**DescribeInstanceTypes was denied.**"]
        assert not app.metric
        assert "explore_region" not in [group.key for group in app.get("button_group")]
        assert fake_fleet.called("spot_price_history") == []

    def test_an_instance_type_offered_nowhere(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["instance_report"] = lambda instance_type, regions: [
            RegionRead("us-east-1", {"offered": False}),
            RegionRead("us-west-2", error="AccessDenied"),
            RegionRead("eu-west-1", {"offered": False}),
        ]
        app = open_capacity()
        assert values(app.warning) == ["g7.2xlarge is not offered in the selected Regions."]
        assert app.warning[0].icon == ":material/block:"
        assert not app.metric
        assert fake_fleet.called("spot_price_history") == []

    def test_without_spot_prices_there_are_no_spot_figures(self, fake_fleet: FakeFleet) -> None:
        cpu_only = {"spot_price": None, "gpu_count": 0, "gpu_type": None}
        fake_fleet.overrides["instance_report"] = report(
            {"us-east-1": cpu_only, "us-west-2": cpu_only}
        )
        assert metrics(open_capacity()) == {
            "vCPUs": ("8", ""),
            "Memory": ("32 GiB", ""),
            "Accelerators": ("0", ""),
        }

    def test_without_on_demand_prices_there_is_no_discount(self, fake_fleet: FakeFleet) -> None:
        unpriced = {"on_demand_price": None}
        fake_fleet.overrides["instance_report"] = report(
            {"us-east-1": unpriced, "us-west-2": unpriced}
        )
        shown = metrics(open_capacity())
        assert shown["Cheapest Spot"] == ("$0.5000/h", EAST)
        assert "Best Spot discount" not in shown


class TestPlacementScores:
    def test_scores_are_read_only_on_request(self, fake_fleet: FakeFleet) -> None:
        app = open_capacity()
        assert fake_fleet.called("placement_score") == []
        assert EAST not in metrics(app)

    def test_check_scores_asks_ec2_for_each_offering_region(self, fake_fleet: FakeFleet) -> None:
        app = open_capacity()
        app.button(key="explore_scores").click().run()
        assert fake_fleet.called("placement_score") == [
            (("g7.2xlarge", "us-east-1"), {"capacity": 1}),
            (("g7.2xlarge", "us-west-2"), {"capacity": 1}),
        ]
        shown = metrics(app)
        assert shown[EAST] == ("7 / 10", "")
        assert shown[WEST] == ("7 / 10", "")
        assert IRELAND not in shown

    def test_scores_stay_on_screen_and_in_the_cache(self, fake_fleet: FakeFleet) -> None:
        app = open_capacity()
        app.button(key="explore_scores").click().run()
        app.run()
        assert metrics(app)[EAST] == ("7 / 10", "")
        app.button(key="explore_scores").click().run()
        assert len(fake_fleet.called("placement_score")) == 2

    def test_a_new_request_size_hides_the_old_scores(self, fake_fleet: FakeFleet) -> None:
        app = open_capacity()
        app.button(key="explore_scores").click().run()
        app.number_input(key="explore_count").set_value(50).run()
        assert EAST not in metrics(app)
        app.button(key="explore_scores").click().run()
        assert fake_fleet.called("placement_score")[-1] == (
            ("g7.2xlarge", "us-west-2"),
            {"capacity": 50},
        )
        assert metrics(app)[WEST] == ("7 / 10", "")

    @pytest.mark.parametrize("outcome", ["no-score", "refused"])
    def test_a_region_without_a_score_shows_a_dash(
        self, fake_fleet: FakeFleet, outcome: str
    ) -> None:
        def score(instance_type: str, region: str, *, capacity: int) -> int | None:
            if region != "us-west-2":
                return 9
            if outcome == "refused":
                raise DashboardError("Too many placement-score queries today.")
            return None

        fake_fleet.overrides["placement_score"] = score
        app = open_capacity()
        app.button(key="explore_scores").click().run()
        shown = metrics(app)
        assert shown[EAST] == ("9 / 10", "")
        assert shown[WEST] == ("—", "")
        errors = ["**Too many placement-score queries today.**"] if outcome == "refused" else []
        assert values(app.error) == errors
