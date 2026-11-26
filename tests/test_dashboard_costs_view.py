"""The Costs page (``cli.dashboard.views.costs``) rendered through the dashboard's entrypoint.

Each test starts the real ``app.py`` under Streamlit's AppTest, switches to
the Costs page and checks its two lazy tabs against the ``fake_fleet``
fixture's canned reports. "Kubernetes allocation" (OpenCost rows queried with
Athena) is the default tab. "AWS bill" is opened through the tabs' session
key, as a click on it would; it calls Cost Explorer only after its button is
pressed, keeps the answer (15 minutes) until Reload, and explains a zero bill.
Each tab is also checked with its report failing, empty or carrying errors.
"""

from __future__ import annotations

from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from cli.dashboard.data import DashboardError
from cli.dashboard.views import costs
from tests._dashboard_fakes import (  # noqa: F401
    APP,
    REGIONS,
    FakeFleet,
    fake_fleet_fixture,
)

COSTS_PAGE = "app_pages/costs.py"
BILL_TAB = {"costs_tab": "AWS bill"}
EAST = "us-east-1 · N. Virginia"
BILLING = (
    "Cost Explorer bills USD 0.01 per request, and a load makes about five, so this view reads "
    "only when you ask and keeps the answer for 15 minutes."
)
NO_REPORTS = (
    "No allocation reports in this window yet: each Region's cost monitor writes one an hour."
)
ZERO_BILL = (
    "Cost Explorer attributes no spend in the last 30 days to the project's tags. Its queries "
    "filter on the `Project` tag, which counts only once that key is activated as a cost "
    "allocation tag (`gco costs allocation status`; in an Organization only the management "
    "account can activate it), and new spend reaches Cost Explorer about a day late. The "
    "Kubernetes allocation tab reads OpenCost instead and needs neither."
)
INACTIVE_TAGS = (
    "Spend is attributed only through active cost allocation tags; activate the others with "
    "`gco costs allocation activate` (management account only in an Organization)."
)


def open_costs(state: dict[str, Any] | None = None) -> AppTest:
    """The Costs page after one run, with ``state`` put in the session first."""
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.run()
    app.switch_page(COSTS_PAGE)
    for key, value in (state or {}).items():
        app.session_state[key] = value
    app.run()
    assert not app.exception, [e.message for e in app.exception]
    return app


def values(elements: Any) -> list[Any]:
    return [element.value for element in elements]


def metrics(app: AppTest) -> dict[str, tuple[str, str]]:
    return {metric.label: (metric.value, metric.delta) for metric in app.metric}


def chart_count(app: AppTest) -> int:
    return len(app.get("vega_lite_chart"))


def allocation_tab(app: AppTest) -> Any:
    return app.tabs[0]


def bill_tab(app: AppTest) -> Any:
    return app.tabs[1]


def button_keys(app: AppTest) -> list[str | None]:
    """The AWS bill tab's buttons (the sidebar has its own)."""
    return [button.key for button in bill_tab(app).button]


def allocation(**changes: Any) -> dict[str, Any]:
    """The fake's allocation report with ``changes`` applied."""
    return {**FakeFleet().allocation_costs(), **changes}


def bill(**changes: Any) -> dict[str, Any]:
    """The fake's Cost Explorer report with ``changes`` applied."""
    return {**FakeFleet().costs(), **changes}


def load_bill(state: dict[str, Any] | None = None) -> AppTest:
    """The AWS bill tab after its "Load from Cost Explorer" button was pressed."""
    app = open_costs({**BILL_TAB, **(state or {})})
    return app.button(key="bill_load").click().run()


class TestAllocation:
    def test_is_the_default_tab_and_breaks_the_spend_down(self, fake_fleet: FakeFleet) -> None:
        app = open_costs()
        assert values(app.title) == ["Costs"]
        assert [tab.label for tab in app.tabs] == ["Kubernetes allocation", "AWS bill"]
        assert not bill_tab(app).children
        assert fake_fleet.called("allocation_costs") == [((), {"days": 7, "granularity": "hourly"})]
        assert fake_fleet.called("costs") == []
        assert metrics(app) == {
            "Allocated, last 7 d": ("$5.12", ""),
            "Top namespace": ("gco-inference", "$4.62"),
            "Top Region": (EAST, "$4.59"),
            "GPU share": ("88%", ""),
        }
        assert values(allocation_tab(app).markdown) == [
            "**Over time (hourly)**",
            "**By namespace**",
            "**By Region**",
        ]
        assert chart_count(app) == 3
        assert fake_fleet.called("cost_monitors") == [((REGIONS,), {})]
        [monitors] = app.dataframe
        assert monitors.value["region"].tolist() == list(REGIONS)
        assert monitors.value["opencost"].tolist() == [True, True, True]
        assert values(app.code) == [
            "gco costs k8s namespaces --days 7\ngco costs k8s trend --days 7 --granularity hourly"
        ]
        assert not allocation_tab(app).caption

    def test_a_month_long_window_buckets_by_day(self, fake_fleet: FakeFleet) -> None:
        app = open_costs()
        app.segmented_control(key="alloc_days").set_value(30).run()
        assert app.segmented_control(key="alloc_granularity_False").value == "daily"
        assert fake_fleet.called("allocation_costs")[-1] == (
            (),
            {"days": 30, "granularity": "daily"},
        )
        assert "Allocated, last 30 d" in metrics(app)
        assert "**Over time (daily)**" in values(allocation_tab(app).markdown)
        assert values(app.code) == [
            "gco costs k8s namespaces --days 30\ngco costs k8s trend --days 30 --granularity daily"
        ]

    def test_daily_buckets_can_be_picked_for_a_week(self, fake_fleet: FakeFleet) -> None:
        app = open_costs()
        app.segmented_control(key="alloc_granularity_True").set_value("daily").run()
        assert fake_fleet.called("allocation_costs")[-1] == (
            (),
            {"days": 7, "granularity": "daily"},
        )

    def test_report_errors_are_listed_above_the_figures(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["allocation_costs"] = allocation(
            errors=["us-west-2: the Athena query timed out", "eu-west-1: no reports"]
        )
        app = open_costs()
        assert values(allocation_tab(app).caption) == [
            ":material/warning: us-west-2: the Athena query timed out",
            ":material/warning: eu-west-1: no reports",
        ]
        assert len(app.metric) == 4

    def test_an_unreadable_report(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["allocation_costs"] = DashboardError(
            "The OpenCost reports are not set up.",
            hint="They need cost_monitoring.enabled in cdk.json.",
        )
        app = open_costs()
        assert values(app.error) == [
            "**The OpenCost reports are not set up.**\n\nThey need cost_monitoring.enabled in "
            "cdk.json."
        ]
        assert not app.metric
        assert fake_fleet.called("cost_monitors") == []

    def test_an_empty_window_explains_the_hourly_reports(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["allocation_costs"] = {
            "namespaces": [],
            "regions": [],
            "trend": [],
            "errors": [],
        }
        app = open_costs()
        assert metrics(app) == {"Allocated, last 7 d": ("$0.00", ""), "GPU share": ("—", "")}
        assert values(app.info) == [NO_REPORTS]
        assert app.info[0].icon == ":material/schedule:"
        assert chart_count(app) == 0
        assert not app.code
        assert fake_fleet.called("cost_monitors") == []

    def test_regions_without_a_namespace_breakdown(self, fake_fleet: FakeFleet) -> None:
        # A failed namespace query comes back empty; the total and the GPU
        # share both come from the Regions instead (3.89 of 5.12 is GPU).
        fake_fleet.overrides["allocation_costs"] = allocation(namespaces=[])
        app = open_costs()
        assert metrics(app) == {
            "Allocated, last 7 d": ("$5.12", ""),
            "Top Region": (EAST, "$4.59"),
            "GPU share": ("76%", ""),
        }
        assert chart_count(app) == 3
        assert not app.info

    def test_namespaces_without_a_region_breakdown(self, fake_fleet: FakeFleet) -> None:
        # A failed Region query comes back empty; the namespaces still give
        # the total and the GPU share (4.5 of 5.12 is GPU).
        fake_fleet.overrides["allocation_costs"] = allocation(regions=[], trend=[])
        app = open_costs()
        assert metrics(app) == {
            "Allocated, last 7 d": ("$5.12", ""),
            "Top namespace": ("gco-inference", "$4.62"),
            "GPU share": ("88%", ""),
        }
        assert chart_count(app) == 3
        assert not app.info

    def test_a_region_breakdown_alone_is_not_an_empty_window(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["allocation_costs"] = allocation(
            namespaces=[],
            trend=[],
            errors=["namespaces: the Athena query timed out", "trend: the Athena query timed out"],
        )
        app = open_costs()
        assert not app.info
        assert metrics(app)["Allocated, last 7 d"] == ("$5.12", "")
        assert chart_count(app) == 3
        assert len(app.code) == 1

    def test_unreadable_cost_monitors_leave_the_charts(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["cost_monitors"] = DashboardError("The cost monitors are unreachable.")
        app = open_costs()
        assert values(app.error) == ["**The cost monitors are unreachable.**"]
        assert not app.dataframe
        assert chart_count(app) == 3
        assert len(app.code) == 1


class TestBill:
    def test_waits_for_its_button_before_asking_cost_explorer(self, fake_fleet: FakeFleet) -> None:
        app = open_costs(BILL_TAB)
        assert not allocation_tab(app).children
        assert values(bill_tab(app).caption) == [BILLING]
        assert app.segmented_control(key="bill_days").value == 30
        assert button_keys(app) == ["bill_load"]
        assert not app.metric
        assert fake_fleet.called("costs") == []
        assert fake_fleet.called("allocation_costs") == []

    def test_loading_shows_spend_forecast_breakdowns_and_tags(self, fake_fleet: FakeFleet) -> None:
        app = load_bill()
        assert fake_fleet.called("costs") == [((), {"days": 30})]
        assert app.session_state[costs.BILL_KEY] == 30
        assert button_keys(app) == ["bill_load", "bill_reload"]
        assert metrics(app) == {
            "Spend, last 30 d": ("42.50 USD", ""),
            "Daily average": ("3.00 USD", ""),
            "Next 30 days (forecast)": ("50.00 USD", ""),
        }
        assert chart_count(app) == 3
        assert values(bill_tab(app).markdown) == [
            "**By service**",
            "**By Region**",
            "**Cost allocation tags**",
        ]
        assert values(app.warning) == [INACTIVE_TAGS]
        [tags] = app.dataframe
        assert tags.value.to_dict("records") == [
            {"tag_key": "Project", "type": "UserDefined", "status": "Inactive"}
        ]
        assert values(app.code) == [
            "gco costs summary --days 30\ngco costs trend --days 30\ngco costs allocation status"
        ]

    def test_the_bill_is_kept_until_reloaded(self, fake_fleet: FakeFleet) -> None:
        app = load_bill()
        app.run()
        assert len(fake_fleet.called("costs")) == 1
        assert "Spend, last 30 d" in metrics(app)
        app.button(key="bill_reload").click().run()
        assert len(fake_fleet.called("costs")) == 2
        assert "Spend, last 30 d" in metrics(app)

    def test_another_window_waits_for_another_load(self, fake_fleet: FakeFleet) -> None:
        app = load_bill()
        app.segmented_control(key="bill_days").set_value(7).run()
        assert not app.metric
        assert button_keys(app) == ["bill_load"]
        assert len(fake_fleet.called("costs")) == 1
        app.button(key="bill_load").click().run()
        assert fake_fleet.called("costs")[-1] == ((), {"days": 7})
        assert "Spend, last 7 d" in metrics(app)

    @pytest.mark.parametrize(
        "daily",
        [[], [{"date": "2026-11-24", "amount": 0.0}, {"date": "2026-11-25", "amount": 0.0}]],
        ids=["no-days", "zero-days"],
    )
    def test_a_zero_bill_explains_the_project_tag(
        self, fake_fleet: FakeFleet, daily: list[dict[str, Any]]
    ) -> None:
        fake_fleet.overrides["costs"] = bill(total=0.0, daily=daily)
        app = load_bill()
        assert values(app.info) == [ZERO_BILL]
        assert app.info[0].icon == ":material/sell:"
        assert values(app.code) == ["gco costs allocation status\ngco costs allocation activate"]
        assert not app.metric
        assert chart_count(app) == 0

    def test_daily_spend_shows_before_the_total_catches_up(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["costs"] = bill(total=None)
        app = load_bill()
        assert not app.info
        assert metrics(app)["Spend, last 30 d"] == ("0.00 USD", "")
        assert metrics(app)["Daily average"] == ("3.00 USD", "")

    def test_report_errors_are_listed(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["costs"] = bill(errors=["The forecast needs more history."])
        app = load_bill()
        assert values(bill_tab(app).caption) == [
            BILLING,
            ":material/warning: The forecast needs more history.",
        ]
        assert len(app.metric) == 3

    def test_an_unreadable_bill(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["costs"] = DashboardError(
            "AWS denied the request: ce:GetCostAndUsage",
            hint="The signed-in principal lacks a permission this view needs.",
        )
        app = load_bill()
        assert values(app.error) == [
            "**AWS denied the request: ce:GetCostAndUsage**\n\n"
            "The signed-in principal lacks a permission this view needs."
        ]
        assert not app.metric
        assert button_keys(app) == ["bill_load", "bill_reload"]

    def test_a_bill_without_a_forecast_daily_series_or_tags(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["costs"] = bill(
            total=12.0,
            currency="EUR",
            by_region={},
            daily=[],
            forecast={},
            allocation_tags=[],
        )
        app = load_bill()
        assert metrics(app) == {
            "Spend, last 30 d": ("12.00 EUR", ""),
            "Daily average": ("0.00 EUR", ""),
        }
        assert chart_count(app) == 2
        assert values(bill_tab(app).markdown) == ["**By service**", "**By Region**"]
        assert not app.dataframe
        assert not app.warning

    def test_active_tags_need_no_warning(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["costs"] = bill(
            allocation_tags=[{"tag_key": "Project", "type": "UserDefined", "status": "Active"}]
        )
        app = load_bill()
        assert "**Cost allocation tags**" in values(bill_tab(app).markdown)
        assert len(app.dataframe) == 1
        assert not app.warning
