"""The Console page (``cli.dashboard.views.console``) rendered through the dashboard's entrypoint.

Each test starts the real ``app.py`` under Streamlit's AppTest, switches to the
Console page and uses it as an operator would: pick a route and a Region, type
query parameters, press Send, then read the answer in its Table and JSON tabs
(unkeyed tabs, so both render), download it, send it again from the Recent
list, or download the fleet status document. The in-memory ``FakeFleet`` from
``tests/_dashboard_fakes.py`` answers ``api_get`` and records every call; no
AWS, no network.

The request timer reads a fake clock, so the milliseconds the page reports are
exact, and the download buttons are recorded as they draw so the files they
offer can be read back (AppTest keeps only a media URL).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import pytest
import requests
import streamlit as st
from streamlit.testing.v1 import AppTest

from cli.dashboard import ui
from cli.dashboard.data import CONSOLE_ROUTES, GLOBAL_ONLY_ROUTES, DashboardError
from cli.dashboard.views import console
from tests._dashboard_fakes import (
    APP,
    REGIONS,
    FakeFleet,
    fake_fleet_fixture,  # noqa: F401
    fleet_status_document,
    topology,
)

CONSOLE_PAGE = "app_pages/console.py"
FIRST_ROUTE = "/api/v1/global/health"
#: What ``FakeFleet.api_get`` answers unless a test overrides it.
ANSWER = {"regions": [{"region": region, "status": "healthy"} for region in REGIONS], "count": 3}
GLOBAL_ONLY_HELP = "Global-only routes answer from the cross-Region aggregator."
REGION_LABELS = ["us-east-1 · N. Virginia", "us-west-2 · Oregon", "eu-west-1 · Ireland"]
#: What each download button offered during a test, by widget key.
Downloads = dict[str | None, dict[str, Any]]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _run(app: AppTest) -> AppTest:
    app.run()
    assert not app.exception, [e.message for e in app.exception]
    return app


def _page(**state: Any) -> AppTest:
    """The Console page drawn by the dashboard's entrypoint, with ``state`` in the session."""
    app = AppTest.from_file(str(APP), default_timeout=60)
    _run(app)
    # The first run draws the Fleet page; forget its reads (``ui.fleet`` is the
    # fixture's FakeFleet), so a test sees only what the Console page asked for.
    ui.fleet().calls.clear()
    app.switch_page(CONSOLE_PAGE)
    for key, value in state.items():
        app.session_state[key] = value
    return _run(app)


def _click(app: AppTest, key: str) -> AppTest:
    app.button(key=key).click()
    return _run(app)


def _route(app: AppTest, path: str) -> AppTest:
    app.selectbox(key="console_route").select(path)
    return _run(app)


def _send(app: AppTest) -> AppTest:
    return _click(app, "console_send")


def _values(elements: Iterable[Any]) -> list[Any]:
    return [element.value for element in elements]


def _keys(widgets: Iterable[Any]) -> list[str | None]:
    return [widget.key for widget in widgets]


def _recent(app: AppTest) -> list[tuple[str | None, str, str]]:
    """The Recent list: each button's key, label and icon."""
    return [
        (button.key, button.label, button.proto.icon)
        for button in app.button
        if (button.key or "").startswith("console_again_")
    ]


class _Clock:
    """A ``time`` stand-in whose ``perf_counter`` moves 125 ms per reading (exact in binary)."""

    def __init__(self) -> None:
        self.now = 0.0

    def perf_counter(self) -> float:
        self.now += 0.125
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Time the console's requests on a fake clock: each takes exactly 125 ms."""
    fake = _Clock()
    monkeypatch.setattr(console, "time", fake)
    return fake


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> Downloads:
    """What each download button offers, by key; the real button still draws."""
    offered: Downloads = {}
    real = st.download_button

    def recording(label: str, data: Any, **options: Any) -> Any:
        offered[options.get("key")] = {"label": label, "data": data, **options}
        return real(label, data, **options)

    monkeypatch.setattr(st, "download_button", recording)
    return offered


# ---------------------------------------------------------------------------
# The request form
# ---------------------------------------------------------------------------


class TestRequestForm:
    def test_opens_on_the_first_route_with_nothing_sent(self, fake_fleet: FakeFleet) -> None:
        app = _page()

        assert _values(app.title) == ["Console"]
        route = app.selectbox(key="console_route")
        assert route.value == FIRST_ROUTE
        assert route.options == [
            f"GET {path}  ·  {about}" for path, about in CONSOLE_ROUTES.items()
        ]
        region = app.selectbox(key="console_region")
        assert region.value == ui.GLOBAL
        assert region.options == [
            "Global API (nearest healthy Region)",
            "us-east-1 · N. Virginia",
            "us-west-2 · Oregon",
            "eu-west-1 · Ireland",
        ]
        # The first route answers only from the aggregator, so the picker is off.
        assert region.disabled
        assert region.help == GLOBAL_ONLY_HELP
        params = app.text_input(key=f"console_params_{FIRST_ROUTE}")
        assert params.placeholder == "key=value, key=value"
        captions = _values(app.caption)
        assert captions[0].startswith(
            "Read any GCO API route with your credentials; nothing here changes the fleet · read "
        )
        assert "From a terminal: `gco jobs health --all-regions`" in captions
        assert "Requests you send appear here." in captions
        assert (
            "Everything `gco -o json status --with-policy` reports, as one JSON file." in captions
        )
        assert "**Fleet status document**" in _values(app.markdown)
        assert not fake_fleet.called("api_get")
        assert not app.tabs and not app.json and not app.download_button and not app.error

    @pytest.mark.parametrize(
        ("path", "global_only", "placeholder", "command"),
        [
            pytest.param(
                "/api/v1/global/jobs",
                True,
                "limit=50, status=running, namespace=gco-jobs",
                "gco jobs list --all-regions",
                id="global-jobs",
            ),
            pytest.param(
                "/api/v1/global/status", True, "key=value, key=value", None, id="global-status"
            ),
            pytest.param(
                "/api/v1/queue/jobs",
                False,
                "limit=50, status=queued, target_region=us-east-1",
                "gco queue list",
                id="queue-jobs",
            ),
            pytest.param(
                "/api/v1/jobs",
                False,
                "limit=50, status=failed, namespace=gco-jobs",
                "gco jobs list -r REGION",
                id="jobs",
            ),
            pytest.param("/api/v1/metrics", False, "key=value, key=value", None, id="metrics"),
        ],
    )
    def test_each_route_offers_its_parameters_and_terminal_command(
        self,
        fake_fleet: FakeFleet,
        path: str,
        global_only: bool,
        placeholder: str,
        command: str | None,
    ) -> None:
        app = _route(_page(), path)

        region = app.selectbox(key="console_region")
        assert region.disabled is global_only
        assert region.help == (GLOBAL_ONLY_HELP if global_only else "")
        assert app.text_input(key=f"console_params_{path}").placeholder == placeholder
        terminal = [c for c in _values(app.caption) if c.startswith("From a terminal:")]
        assert terminal == ([f"From a terminal: `{command}`"] if command else [])

    def test_malformed_query_parameters_stop_the_page_before_it_sends(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = _page()
        app.text_input(key=f"console_params_{FIRST_ROUTE}").input("limit=5, status")
        app = _run(app)

        assert _values(app.error) == [
            "**'status' is not a query parameter of the form key=value.**"
        ]
        # The page stops at the error: no Send button, no Recent list.
        assert "console_send" not in _keys(app.button)
        assert "**Recent**" not in _values(app.markdown)
        assert not fake_fleet.called("api_get")


# ---------------------------------------------------------------------------
# Sending a request
# ---------------------------------------------------------------------------


class TestSend:
    def test_a_global_route_reads_the_global_api_and_shows_the_answer(
        self, fake_fleet: FakeFleet, clock: _Clock
    ) -> None:
        app = _send(_page())

        assert fake_fleet.called("api_get") == [((FIRST_ROUTE,), {"region": None, "params": None})]
        assert "**GET /api/v1/global/health** · global API · 125 ms" in _values(app.markdown)
        assert [tab.label for tab in app.tabs] == ["Table (3 rows)", "JSON"]
        table = app.dataframe[0].value
        assert table.to_dict("records") == ANSWER["regions"]
        assert json.loads(app.json[0].value) == ANSWER
        assert [button.label for button in app.download_button] == ["JSON", "CSV"]
        assert _recent(app) == [
            ("console_again_0", "/global/health · global · 125 ms", ":material/check_circle:")
        ]
        assert "Requests you send appear here." not in _values(app.caption)
        assert not app.error

    def test_a_regional_route_reads_the_picked_region_with_the_query_parameters(
        self, fake_fleet: FakeFleet, clock: _Clock
    ) -> None:
        app = _route(_page(), "/api/v1/jobs")
        app.selectbox(key="console_region").select("us-west-2")
        app.text_input(key="console_params_/api/v1/jobs").input("limit=5, status=failed")
        app = _send(app)

        assert fake_fleet.called("api_get") == [
            (
                ("/api/v1/jobs",),
                {"region": "us-west-2", "params": {"limit": "5", "status": "failed"}},
            )
        ]
        assert "**GET /api/v1/jobs** · us-west-2 · 125 ms" in _values(app.markdown)
        assert app.session_state[console.RESULT_KEY]["params"] == {"limit": "5", "status": "failed"}

    def test_global_on_a_regional_route_lets_the_global_api_route_it(
        self, fake_fleet: FakeFleet, clock: _Clock
    ) -> None:
        app = _send(_route(_page(), "/api/v1/health"))

        assert fake_fleet.called("api_get") == [
            (("/api/v1/health",), {"region": None, "params": None})
        ]
        assert "**GET /api/v1/health** · global API · 125 ms" in _values(app.markdown)

    def test_a_global_only_route_ignores_the_region_picked_before(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = _route(_page(), "/api/v1/jobs")
        app.selectbox(key="console_region").select("eu-west-1")
        app = _send(_route(_run(app), "/api/v1/global/jobs"))

        assert app.selectbox(key="console_region").value == "eu-west-1"
        assert fake_fleet.called("api_get") == [
            (("/api/v1/global/jobs",), {"region": None, "params": None})
        ]

    @pytest.mark.parametrize(
        ("payload", "label", "csv"),
        [
            pytest.param(
                {
                    "jobs": [
                        {"name": "train-a", "status": {"phase": "Running"}},
                        {"name": "eval-b", "status": {"phase": "Failed"}},
                    ],
                    "count": 2,
                },
                "Table (2 rows)",
                "name,status.phase\ntrain-a,Running\neval-b,Failed\n",
                id="nested-records",
            ),
            pytest.param(
                [{"region": "us-east-1", "ok": True}],
                "Table (1 row)",
                "region,ok\nus-east-1,True\n",
                id="list-of-records",
            ),
            pytest.param(
                {"status": "healthy", "region": "us-east-1"},
                "Table (1 row)",
                "status,region\nhealthy,us-east-1\n",
                id="scalar-fields",
            ),
        ],
    )
    def test_an_answer_with_records_draws_the_table_and_offers_json_and_csv(
        self,
        fake_fleet: FakeFleet,
        downloads: Downloads,
        payload: Any,
        label: str,
        csv: str,
    ) -> None:
        fake_fleet.overrides["api_get"] = payload

        app = _send(_page())

        assert [tab.label for tab in app.tabs] == [label, "JSON"]
        # The CSV holds exactly the table drawn.
        assert app.dataframe[0].value.to_csv(index=False) == csv
        assert json.loads(app.json[0].value) == payload
        assert _keys(app.download_button) == ["console_json", "console_csv"]
        assert downloads["console_json"]["file_name"] == "api-v1-global-health.json"
        assert downloads["console_json"]["mime"] == "application/json"
        assert json.loads(downloads["console_json"]["data"]) == payload
        assert downloads["console_csv"]["file_name"] == "api-v1-global-health.csv"
        assert downloads["console_csv"]["mime"] == "text/csv"
        assert downloads["console_csv"]["data"] == csv

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param([], id="empty-list"),
            pytest.param(42, id="number"),
            pytest.param(None, id="null"),
            pytest.param("accepted", id="string"),
            # pandas makes these rows without columns: still nothing to tabulate.
            pytest.param({}, id="empty-object"),
            pytest.param([{}, {}], id="empty-objects"),
            pytest.param({"spec": {}}, id="only-empty-fields"),
        ],
    )
    def test_an_answer_without_records_shows_json_only(
        self, fake_fleet: FakeFleet, downloads: Downloads, payload: Any
    ) -> None:
        fake_fleet.overrides["api_get"] = payload

        app = _send(_page())

        assert [tab.label for tab in app.tabs] == ["Table (0 rows)", "JSON"]
        assert "The answer has no rows to tabulate." in _values(app.caption)
        assert not app.dataframe
        # The JSON view and the download hold the answer as valid JSON.
        assert json.loads(app.json[0].value) == payload
        assert json.loads(downloads["console_json"]["data"]) == payload
        assert _keys(app.download_button) == ["console_json"]
        assert "console_csv" not in downloads

    @pytest.mark.parametrize(
        ("error", "card"),
        [
            pytest.param(
                DashboardError(
                    "AWS denied the request: not authorized",
                    hint="The signed-in principal lacks a permission this view needs.",
                ),
                "**AWS denied the request: not authorized**\n\n"
                "The signed-in principal lacks a permission this view needs.",
                id="with-a-hint",
            ),
            pytest.param(
                requests.ConnectionError("connection refused"),
                "**Could not reach the GCO API: ConnectionError**",
                id="unreachable",
            ),
        ],
    )
    def test_a_failed_read_draws_the_error_card_and_is_listed_as_failed(
        self, fake_fleet: FakeFleet, clock: _Clock, error: Exception, card: str
    ) -> None:
        fake_fleet.fail["api_get"] = error

        app = _send(_page())

        assert _values(app.error) == [card]
        # The time is still reported: a slow failure is worth knowing about.
        assert "**GET /api/v1/global/health** · global API · 125 ms" in _values(app.markdown)
        assert not app.tabs and not app.json and not app.download_button
        assert _recent(app) == [
            ("console_again_0", "/global/health · global · 125 ms", ":material/error:")
        ]


# ---------------------------------------------------------------------------
# Regional API mode (no global API)
# ---------------------------------------------------------------------------

REGIONAL_ONLY = (
    "Regional API mode has no global API, so the `/api/v1/global` routes are not listed."
)


class TestRegionalApiMode:
    def test_the_global_only_routes_are_not_offered_and_the_page_says_why(
        self, fake_fleet: FakeFleet, clock: _Clock
    ) -> None:
        fake_fleet.overrides["topology"] = topology(regional_api=True)

        app = _page()

        route = app.selectbox(key="console_route")
        assert route.options == [
            f"GET {path}  ·  {about}"
            for path, about in CONSOLE_ROUTES.items()
            if path not in GLOBAL_ONLY_ROUTES
        ]
        assert route.value == "/api/v1/queue/stats"
        region = app.selectbox(key="console_region")
        assert region.options == REGION_LABELS and not region.disabled
        assert REGIONAL_ONLY in _values(app.caption)

        app = _send(app)

        # Every request names the bridge it rode.
        assert fake_fleet.called("api_get") == [
            (("/api/v1/queue/stats",), {"region": "us-east-1", "params": None})
        ]
        assert "**GET /api/v1/queue/stats** · us-east-1 · 125 ms" in _values(app.markdown)
        assert not app.error

    def test_without_workload_regions_a_request_rides_the_default_regions_bridge(
        self, fake_fleet: FakeFleet, clock: _Clock
    ) -> None:
        fake_fleet.overrides["topology"] = topology(regional_api=True, workload_regions=())
        fake_fleet.config.default_region = "eu-west-1"

        app = _send(_page())

        assert fake_fleet.called("api_get") == [
            (("/api/v1/queue/stats",), {"region": "eu-west-1", "params": None})
        ]
        assert "**GET /api/v1/queue/stats** · eu-west-1 · 125 ms" in _values(app.markdown)
        assert _recent(app) == [
            ("console_again_0", "/queue/stats · eu-west-1 · 125 ms", ":material/check_circle:")
        ]

    def test_the_global_api_mode_offers_every_route_without_the_note(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = _page()

        assert len(app.selectbox(key="console_route").options) == len(CONSOLE_ROUTES)
        assert REGIONAL_ONLY not in _values(app.caption)


# ---------------------------------------------------------------------------
# The Recent list
# ---------------------------------------------------------------------------


class TestRecent:
    def test_requests_are_listed_newest_first_and_can_be_sent_again(
        self, fake_fleet: FakeFleet, clock: _Clock
    ) -> None:
        app = _route(_send(_page()), "/api/v1/jobs")
        app.selectbox(key="console_region").select("us-west-2")
        app.text_input(key="console_params_/api/v1/jobs").input("limit=5")
        app = _send(app)

        assert _recent(app) == [
            ("console_again_0", "/jobs · us-west-2 · 125 ms", ":material/check_circle:"),
            ("console_again_1", "/global/health · global · 125 ms", ":material/check_circle:"),
        ]

        # The older, global request goes out again to the global API...
        app = _click(app, "console_again_1")
        assert fake_fleet.called("api_get")[-1] == (
            (FIRST_ROUTE,),
            {"region": None, "params": None},
        )
        assert "**GET /api/v1/global/health** · global API · 125 ms" in _values(app.markdown)

        # ...and the regional one to its Region, with its parameters.
        app = _click(app, "console_again_1")
        assert fake_fleet.called("api_get")[-1] == (
            ("/api/v1/jobs",),
            {"region": "us-west-2", "params": {"limit": "5"}},
        )
        assert "**GET /api/v1/jobs** · us-west-2 · 125 ms" in _values(app.markdown)
        assert len(fake_fleet.called("api_get")) == 4
        assert [item["path"] for item in app.session_state[console.HISTORY_KEY]] == [
            "/api/v1/jobs",
            FIRST_ROUTE,
            "/api/v1/jobs",
            FIRST_ROUTE,
        ]

    def test_only_the_last_twelve_requests_are_kept(self, fake_fleet: FakeFleet) -> None:
        earlier = list(CONSOLE_ROUTES)[1 : 1 + console.HISTORY_LENGTH]
        history = [
            {"path": path, "region": "global", "params": {}, "ms": 10, "ok": True}
            for path in earlier
        ]

        app = _send(_page(**{console.HISTORY_KEY: history}))

        kept = [item["path"] for item in app.session_state[console.HISTORY_KEY]]
        assert kept == [FIRST_ROUTE, *earlier[:-1]]
        assert len(_recent(app)) == console.HISTORY_LENGTH


# ---------------------------------------------------------------------------
# The fleet status document
# ---------------------------------------------------------------------------


class TestStatusDocument:
    def test_prepare_offers_the_status_document_with_its_policy_section(
        self, fake_fleet: FakeFleet, downloads: Downloads
    ) -> None:
        app = _click(_page(), "console_status")

        assert ((), {"with_costs": False, "with_policy": True}) in fake_fleet.called("fleet_status")
        assert [button.label for button in app.download_button] == ["Download gco-status.json"]
        offered = downloads["console_status_download"]
        assert offered["file_name"] == "gco-status.json"
        assert offered["mime"] == "application/json"
        assert json.loads(offered["data"]) == fleet_status_document()
        assert not app.error

    def test_an_unreadable_status_document_draws_the_error_card(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["fleet_status"] = DashboardError("The status document is unreadable.")

        app = _click(_page(), "console_status")

        assert _values(app.error) == ["**The status document is unreadable.**"]
        assert not app.download_button
