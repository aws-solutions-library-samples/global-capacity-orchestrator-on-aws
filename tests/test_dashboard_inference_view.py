"""The dashboard's Inference page: endpoint list, detail tabs, lifecycle actions, deploy dialog.

``cli/dashboard/views/inference.py`` is rendered through the dashboard's real
entrypoint with Streamlit's AppTest, against the in-memory ``FakeFleet`` from
``tests/_dashboard_fakes.py`` (no AWS, no network). Each test checks what an
operator sees (metrics, tables, captions, error cards, toasts) and what the
page asked the fleet to do (``FakeFleet.calls``).

AppTest cannot select a table row, so ``ui.selected_row`` is monkeypatched
to pick one. A click inside a dialog registers only when the dialog function
runs again on that rerun (the browser reruns an open dialog with the
arguments it was opened with), so the deploy dialog is driven from a script
that calls it on every run. The confirmations the Manage tab opens are
recorded as the page opens them (the real dialog still draws) and confirmed
from such a script with the same arguments.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from cli.dashboard import charts, ui
from cli.dashboard.data import DashboardError
from cli.dashboard.regions import region_label
from cli.dashboard.views import inference
from tests._dashboard_fakes import (
    APP,
    REGIONS,
    FakeFleet,
    fake_fleet_fixture,  # noqa: F401
    sample_endpoint,
    topology,
)

NAME = "qwen-chat"
IMAGE = "vllm/vllm-openai:v0.32.0"
NEW_IMAGE = "vllm/vllm-openai:v0.33.0"
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
REFUSAL_CARD = "**The API refused the change.**\n\nCheck the endpoint's state and retry."
FIRST_ENDPOINT = "The first endpoint; select a row for another."
NAME_RULE = "The name must be lowercase letters, digits and '-', start with a letter, 55 at most."


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _run(app: AppTest) -> AppTest:
    app.run()
    assert not app.exception, [e.message for e in app.exception]
    return app


def _click(app: AppTest, key: str) -> AppTest:
    app.button(key=key).click()
    return _run(app)


def _page(tab: str | None = None, **state: Any) -> AppTest:
    """The Inference page drawn by the dashboard's entrypoint, with ``tab`` open."""
    app = AppTest.from_file(str(APP), default_timeout=60)
    _run(app)
    app.switch_page("app_pages/inference.py")
    if tab is not None:
        app.session_state[inference.TAB_KEY] = tab
    for key, value in state.items():
        app.session_state[key] = value
    return _run(app)


def _values(elements: Any) -> list[Any]:
    return [element.value for element in elements]


def _keys(widgets: Any) -> set[str]:
    return {widget.key for widget in widgets}


def _rows(frame: Any, *columns: str) -> list[tuple[Any, ...]]:
    """``columns`` of a drawn table, row by row, label cells as plain lists."""
    return [
        tuple(value.tolist() if hasattr(value, "tolist") else value for value in row)
        for row in frame[list(columns)].itertuples(index=False)
    ]


def _serve(fake: FakeFleet, *endpoints: dict[str, Any]) -> None:
    """Make the registry hold ``endpoints``."""
    fake.overrides["endpoints"] = list(endpoints)


def _spec(**changes: Any) -> dict[str, Any]:
    """The sample endpoint's spec with ``changes`` applied."""
    return {**sample_endpoint()["spec"], **changes}


def _refusal() -> DashboardError:
    return DashboardError(
        "The API refused the change.", hint="Check the endpoint's state and retry."
    )


def _creating() -> dict[str, Any]:
    """A second endpoint, still coming up in one Region and serving no named model."""
    return sample_endpoint(
        endpoint_name="llama-batch",
        desired_state="creating",
        target_regions=["us-east-1"],
        region_status={
            "us-east-1": {"state": "creating", "replicas_ready": 0, "replicas_desired": 2}
        },
        spec={"image": IMAGE, "gpu_count": 4, "replicas": 2, "env": {}},
    )


def _deleted() -> dict[str, Any]:
    """A deleted endpoint whose last status would change every metric if it counted."""
    return sample_endpoint(
        endpoint_name="old-model",
        desired_state="deleted",
        target_regions=["eu-west-1"],
        region_status={
            "eu-west-1": {"state": "running", "replicas_ready": 3, "replicas_desired": 3}
        },
        spec={"image": IMAGE, "gpu_count": 8, "replicas": 3},
    )


@dataclass
class Confirmation:
    """A confirmation the page opened: the dialog function and what it was called with."""

    dialog: Callable[..., None]
    message: str
    options: dict[str, Any]


@pytest.fixture
def confirmations(monkeypatch: pytest.MonkeyPatch) -> list[Confirmation]:
    """Record every ``ui.confirm`` the page opens; the real dialog still draws."""
    opened: list[Confirmation] = []
    dialog = ui.confirm

    def recording(message: str, **options: Any) -> None:
        opened.append(Confirmation(dialog, message, options))
        dialog(message, **options)

    monkeypatch.setattr(ui, "confirm", recording)
    return opened


def _confirmation_script(
    dialog: Callable[..., None], message: str, options: dict[str, Any]
) -> None:
    from cli.dashboard import ui

    ui.show_flash()
    dialog(message, **options)


def _confirm(opened: Confirmation) -> AppTest:
    """Press the confirm button of an opened confirmation."""
    app = AppTest.from_function(
        _confirmation_script,
        kwargs={"dialog": opened.dialog, "message": opened.message, "options": opened.options},
        default_timeout=60,
    )
    return _click(_run(app), "confirm_go")


def _deploy_script() -> None:
    from cli.dashboard import ui
    from cli.dashboard.views import inference

    ui.show_flash()
    inference.deploy_dialog()


def _deploy_dialog() -> AppTest:
    return _run(AppTest.from_function(_deploy_script, default_timeout=60))


# ---------------------------------------------------------------------------
# The reply timer
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class TestTimer:
    """``_Timer`` measures a reply stream's time to first token and its throughput."""

    @pytest.fixture
    def clock(self, monkeypatch: pytest.MonkeyPatch) -> _Clock:
        clock = _Clock()
        monkeypatch.setattr(inference, "time", SimpleNamespace(perf_counter=clock))
        return clock

    def test_reports_first_token_chunks_and_rate(self, clock: _Clock) -> None:
        timer = inference._Timer()

        def stream() -> Any:
            for at, chunk in (
                (100.25, "Several "),
                (100.5, "Regions "),
                (101.0, "keep "),
                (101.5, "it up."),
            ):
                clock.now = at
                yield chunk

        assert list(timer.wrap(stream())) == ["Several ", "Regions ", "keep ", "it up."]
        clock.now = 102.0
        assert timer.summary() == "first token 0.25 s · 4 chunks in 2.0 s · 2/s"

    def test_reports_a_reply_without_tokens(self, clock: _Clock) -> None:
        timer = inference._Timer()
        assert list(timer.wrap(iter([]))) == []
        clock.now = 101.5
        assert timer.summary() == "no tokens in 1.5 s"

    def test_a_reply_with_no_elapsed_time_has_no_rate(self, clock: _Clock) -> None:
        timer = inference._Timer()
        assert list(timer.wrap(iter(["a", "b"]))) == ["a", "b"]
        assert timer.summary() == "first token 0.00 s · 2 chunks in 0.0 s · 0/s"


# ---------------------------------------------------------------------------
# The endpoint list and the detail panel
# ---------------------------------------------------------------------------


class TestEndpointList:
    def test_a_failed_read_draws_the_error_card_and_nothing_else(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["endpoints"] = DashboardError(
            "Could not reach the GCO API: ConnectTimeout", hint="Check the network and retry."
        )
        app = _page()
        assert _values(app.error) == [
            "**Could not reach the GCO API: ConnectTimeout**\n\nCheck the network and retry."
        ]
        assert not app.metric
        assert not app.dataframe
        assert not app.tabs

    def test_an_empty_registry_says_how_to_add_one(self, fake_fleet: FakeFleet) -> None:
        _serve(fake_fleet)
        app = _page()
        assert _values(app.info) == ["No inference endpoints. Deploy one with the button above."]
        assert not app.metric
        assert not app.dataframe
        assert not app.tabs

    def test_metrics_count_live_endpoints_and_the_table_lists_all(
        self, fake_fleet: FakeFleet
    ) -> None:
        _serve(fake_fleet, sample_endpoint(), _creating(), _deleted())
        app = _page()
        assert [(metric.label, metric.value) for metric in app.metric] == [
            ("Endpoints", "2"),
            (":material/play_circle: Running", "1"),
            ("Replicas ready", "2 / 4"),
            ("Regions serving", "2"),
            ("GPUs requested", "10"),
        ]
        assert _rows(app.dataframe[0].value, "endpoint", "state") == [
            (NAME, ["running"]),
            ("llama-batch", ["creating"]),
            ("old-model", ["deleted"]),
        ]

    def test_the_first_endpoint_is_shown_until_a_row_is_picked(self, fake_fleet: FakeFleet) -> None:
        _serve(fake_fleet, sample_endpoint(), _creating())
        app = _page()
        assert _values(app.subheader) == [NAME]
        assert FIRST_ENDPOINT in _values(app.caption)

    def test_a_picked_row_opens_that_endpoint(
        self, fake_fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[int] = []

        def pick_second(event: object, rows: int) -> int:
            asked.append(rows)
            return 1

        monkeypatch.setattr(ui, "selected_row", pick_second)
        _serve(fake_fleet, sample_endpoint(), _creating())
        app = _page()
        assert set(asked) == {2}
        assert _values(app.subheader) == ["llama-batch"]
        # A creating endpoint with no MODEL gets a violet badge and no model badge.
        assert ":violet-badge[creating]" in _values(app.markdown)
        assert FIRST_ENDPOINT not in _values(app.caption)

    @pytest.mark.parametrize(
        ("state", "colour"),
        [("running", "green"), ("stopped", "gray"), ("deleted", "gray"), ("updating", "violet")],
    )
    def test_the_state_badge_is_coloured_by_state(
        self, fake_fleet: FakeFleet, state: str, colour: str
    ) -> None:
        _serve(fake_fleet, sample_endpoint(desired_state=state))
        app = _page()
        assert f":{colour}-badge[{state}] :gray-badge[{MODEL}]" in _values(app.markdown)

    def test_a_record_without_a_name_opens_no_detail_panel(self, fake_fleet: FakeFleet) -> None:
        nameless = sample_endpoint()
        del nameless["endpoint_name"]
        _serve(fake_fleet, nameless)
        app = _page()
        assert _rows(app.dataframe[0].value, "endpoint") == [("",)]
        assert not app.subheader
        assert not app.tabs

    @pytest.mark.parametrize("tab", ["Overview", "Playground", "Health", "Manage"])
    def test_only_the_open_tab_is_drawn(self, fake_fleet: FakeFleet, tab: str) -> None:
        app = _page(tab)
        assert [t.label for t in app.tabs] == ["Overview", "Playground", "Health", "Manage"]
        assert [t.label for t in app.tabs if t.children] == [tab]

    def test_the_deploy_button_opens_the_deploy_dialog(self, fake_fleet: FakeFleet) -> None:
        app = _click(_page(), "inference_deploy")
        assert any(
            code.startswith(f"gco inference deploy NAME -i {IMAGE}") for code in _values(app.code)
        )
        assert app.button(key="deploy_go").disabled


# ---------------------------------------------------------------------------
# Overview tab
# ---------------------------------------------------------------------------


def _facts(app: AppTest) -> list[str]:
    text = next(value for value in _values(app.markdown) if value.startswith("**Image**"))
    return text.split("  \n")


class TestOverview:
    def test_replicas_by_region_and_the_spec(self, fake_fleet: FakeFleet) -> None:
        app = _page()
        assert len(app.get("vega_lite_chart")) == 1
        assert _rows(app.dataframe[1].value, "region", "state", "ready", "desired", "error") == [
            ("us-east-1", ["running"], 1, 1, ""),
            ("us-west-2", ["running"], 1, 1, ""),
        ]
        assert _facts(app) == [
            f"**Image** `{IMAGE}`",
            "**Framework** vllm",
            f"**Model** `{MODEL}`",
            "**Port · health** 8000 · `/health`",
            "**GPUs per replica** 1",
            "**Capacity** Spot when available, else On-Demand",
            "**Scaling** 1 per Region, fixed",
            "**Path** `/inference/qwen-chat`",
        ]

    def test_failing_and_unsynced_regions_are_shown_as_reported(
        self, fake_fleet: FakeFleet
    ) -> None:
        _serve(
            fake_fleet,
            sample_endpoint(
                target_regions=list(REGIONS),
                region_status={
                    "us-east-1": {"state": "running", "replicas_ready": 1, "replicas_desired": 1},
                    "us-west-2": {
                        "state": "error",
                        "replicas_ready": 0,
                        "replicas_desired": 1,
                        "error": "ImagePullBackOff",
                    },
                },
            ),
        )
        app = _page()
        assert _rows(app.dataframe[1].value, "region", "state", "ready", "desired", "error") == [
            ("us-east-1", ["running"], 1, 1, ""),
            ("us-west-2", ["error"], 0, 1, "ImagePullBackOff"),
            ("eu-west-1", ["waiting"], 0, 0, ""),
        ]
        assert app.metric[2].value == "1 / 2"

    def test_an_endpoint_no_monitor_has_synced_waits_everywhere(
        self, fake_fleet: FakeFleet
    ) -> None:
        _serve(fake_fleet, sample_endpoint(region_status=None))
        app = _page()
        assert _rows(app.dataframe[1].value, "region", "state", "ready", "desired") == [
            ("us-east-1", ["waiting"], 0, 0),
            ("us-west-2", ["waiting"], 0, 0),
        ]
        assert app.metric[2].value == "0 / 0"

    def test_an_endpoint_without_target_regions_has_no_chart(self, fake_fleet: FakeFleet) -> None:
        _serve(fake_fleet, sample_endpoint(target_regions=[], region_status={}))
        app = _page()
        assert "No target Regions." in _values(app.caption)
        assert not app.get("vega_lite_chart")
        assert app.dataframe[1].value.empty

    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            pytest.param(
                _spec(
                    autoscaling={
                        "enabled": True,
                        "min_replicas": 1,
                        "max_replicas": 4,
                        "metrics": [{"type": "gpu", "target": 60}, {"type": "cpu", "target": 70}],
                    }
                ),
                {"**Scaling** 1-4 replicas on gpu:60, cpu:70"},
                id="autoscaled",
            ),
            pytest.param(
                _spec(replicas=3, autoscaling={"enabled": False, "min_replicas": 1}),
                {"**Scaling** 3 per Region, fixed"},
                id="autoscaling-off",
            ),
            pytest.param(
                {"image": "nvcr.io/nvidia/tritonserver:26.09-py3", "capacity_type": "on-demand"},
                {
                    "**Framework** not pinned",
                    "**Model** set by the arguments",
                    "**GPUs per replica** 0",
                    "**Capacity** on-demand",
                    "**Scaling** 1 per Region, fixed",
                },
                id="bare-spec",
            ),
        ],
    )
    def test_the_spec_facts_follow_the_spec(
        self, fake_fleet: FakeFleet, spec: dict[str, Any], expected: set[str]
    ) -> None:
        _serve(fake_fleet, sample_endpoint(spec=spec))
        assert expected <= set(_facts(_page()))


# ---------------------------------------------------------------------------
# Playground tab
# ---------------------------------------------------------------------------


def _send(app: AppTest, prompt: str) -> AppTest:
    app.chat_input(key=f"chat_input_{NAME}").set_value(prompt)
    return _run(app)


def _conversation(app: AppTest) -> list[tuple[str, list[str]]]:
    return [(message.name, _values(message.markdown)) for message in app.chat_message]


class TestPlayground:
    def test_an_endpoint_that_is_not_running_cannot_chat(self, fake_fleet: FakeFleet) -> None:
        _serve(fake_fleet, sample_endpoint(desired_state="stopped"))
        app = _page("Playground")
        assert _values(app.info) == ["The endpoint is stopped; start it to chat."]
        assert not app.chat_input
        assert not fake_fleet.called("served_model")

    def test_routes_are_the_global_api_and_deployed_target_regions(
        self, fake_fleet: FakeFleet
    ) -> None:
        _serve(fake_fleet, sample_endpoint(target_regions=["us-east-1", "ap-south-1", "us-west-2"]))
        app = _page("Playground")
        assert app.selectbox(key=f"chat_region_{NAME}").options == [
            "Global API (nearest healthy Region)",
            region_label("us-east-1"),
            region_label("us-west-2"),
        ]

    def test_regional_api_mode_offers_only_the_endpoints_regions(
        self, fake_fleet: FakeFleet
    ) -> None:
        # There is no global API: an unpinned chat would ride the default
        # Region's bridge, so "Global API" would be a false label.
        fake_fleet.overrides["topology"] = topology(regional_api=True)
        app = _page("Playground")
        assert app.selectbox(key=f"chat_region_{NAME}").options == [
            region_label("us-east-1"),
            region_label("us-west-2"),
        ]
        app = _send(app, "Hello there")
        assert fake_fleet.called("served_model") == [((NAME, "us-east-1"), {})]
        assert [kwargs["region"] for _, kwargs in fake_fleet.called("chat")] == ["us-east-1"]

    def test_regional_api_mode_without_a_workload_region_to_route_through(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.overrides["topology"] = topology(regional_api=True)
        _serve(fake_fleet, sample_endpoint(target_regions=["ap-south-1"]))
        app = _page("Playground")
        assert _values(app.info) == [
            "The endpoint serves in no workload Region, so there is no Region to chat through."
        ]
        assert not app.chat_input
        assert not fake_fleet.called("served_model")

    def test_chats_through_the_global_api(self, fake_fleet: FakeFleet) -> None:
        app = _send(_page("Playground"), "Hello there")
        assert _conversation(app) == [
            ("user", ["Hello there"]),
            ("assistant", ["Several Regions keep it up."]),
        ]
        assert fake_fleet.called("served_model") == [((NAME, None), {})]
        assert fake_fleet.called("chat") == [
            (
                (NAME, [{"role": "user", "content": "Hello there"}]),
                {"model": MODEL, "max_tokens": 256, "temperature": 0.7, "region": None},
            )
        ]
        assert (
            f"Talking to `{MODEL}` through `/inference/qwen-chat`, signed with your credentials."
            in _values(app.caption)
        )
        assert _values(app.chat_message[1].caption)[0].startswith("first token ")

    def test_chats_through_a_region_with_a_system_prompt_and_settings(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.overrides["served_model"] = lambda name, region: f"served-in-{region}"
        app = _page("Playground")
        app.selectbox(key=f"chat_region_{NAME}").select("us-west-2")
        app.slider(key=f"chat_tokens_{NAME}").set_value(512)
        app.slider(key=f"chat_temp_{NAME}").set_value(0.2)
        app.text_area(key=f"chat_system_{NAME}").input("Answer in one line.")
        app = _send(app, "Which Region answers?")
        assert fake_fleet.called("chat") == [
            (
                (
                    NAME,
                    [
                        {"role": "system", "content": "Answer in one line."},
                        {"role": "user", "content": "Which Region answers?"},
                    ],
                ),
                {
                    "model": "served-in-us-west-2",
                    "max_tokens": 512,
                    "temperature": 0.2,
                    "region": "us-west-2",
                },
            )
        ]
        assert any(c.startswith("Talking to `served-in-us-west-2`") for c in _values(app.caption))

    def test_the_conversation_carries_over_and_clears(self, fake_fleet: FakeFleet) -> None:
        app = _run(_send(_page("Playground"), "Hello there"))
        # A later run redraws the exchange from session state, the reply with its timing.
        assert _conversation(app) == [
            ("user", ["Hello there"]),
            ("assistant", ["Several Regions keep it up."]),
        ]
        assert not app.chat_message[0].caption
        assert _values(app.chat_message[1].caption)[0].startswith("first token ")
        fake_fleet.chat_chunks = ["Still ", "here."]
        app = _send(app, "And now?")
        assert fake_fleet.called("chat")[-1][0][1] == [
            {"role": "user", "content": "Hello there"},
            {"role": "assistant", "content": "Several Regions keep it up."},
            {"role": "user", "content": "And now?"},
        ]
        assert _conversation(app)[-1] == ("assistant", ["Still here."])
        app = _click(app, f"chat_clear_{NAME}")
        assert not app.chat_message

    def test_a_failed_reply_shows_the_error_and_drops_the_prompt(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["chat"] = DashboardError("The endpoint answered HTTP 503: warming up")
        app = _send(_page("Playground"), "Hello there")
        assert _values(app.error) == ["**The endpoint answered HTTP 503: warming up**"]
        assert len(fake_fleet.called("chat")) == 1
        assert not _run(app).chat_message

    def test_a_reply_without_tokens_says_so(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.chat_chunks = []
        app = _send(_page("Playground"), "Hello there")
        assert _values(app.chat_message[1].caption)[0].startswith("no tokens in ")

    def test_an_unknown_served_model_stops_the_playground(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["served_model"] = DashboardError("Endpoint 'qwen-chat' does not exist.")
        app = _page("Playground")
        assert _values(app.error) == ["**Endpoint 'qwen-chat' does not exist.**"]
        assert not app.chat_input
        assert not fake_fleet.called("chat")


# ---------------------------------------------------------------------------
# Health tab
# ---------------------------------------------------------------------------


class TestHealth:
    def test_nothing_is_probed_until_asked(self, fake_fleet: FakeFleet) -> None:
        app = _page("Health")
        assert (
            "Calls the endpoint's health path through the global API and through each Region's "
            "API, the way `gco inference health` does, and times each round trip."
            in _values(app.caption)
        )
        assert not fake_fleet.called("endpoint_health_all")
        assert len(app.dataframe) == 1  # the endpoint list only
        assert not app.get("vega_lite_chart")

    def test_probes_every_route_and_keeps_the_results(self, fake_fleet: FakeFleet) -> None:
        app = _click(_page("Health"), f"health_run_{NAME}")
        assert fake_fleet.called("endpoint_health_all") == [
            ((NAME, ("us-east-1", "us-west-2")), {})
        ]
        columns = ("region", "healthy", "http_status", "latency_ms", "error")
        expected = [
            ("global", ["healthy"], 200, 250.0, ""),
            ("us-east-1", ["healthy"], 200, 900.0, ""),
            ("us-west-2", ["healthy"], 200, 900.0, ""),
        ]
        assert _rows(app.dataframe[1].value, *columns) == expected
        assert len(app.get("vega_lite_chart")) == 1
        # The results stay in session state: a later run redraws them without probing.
        app = _run(app)
        assert len(fake_fleet.called("endpoint_health_all")) == 1
        assert _rows(app.dataframe[1].value, *columns) == expected

    def test_failed_probes_read_unhealthy_and_only_timed_ones_are_charted(
        self, fake_fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_fleet.overrides["endpoint_health_all"] = [
            {"region": "global", "healthy": True, "http_status": 200, "latency_ms": 250},
            {"region": "us-east-1", "healthy": False, "http_status": 503, "latency_ms": 40},
            {
                "region": "us-west-2",
                "healthy": False,
                "http_status": None,
                "latency_ms": None,
                "error": "Could not reach the GCO API: ConnectTimeout",
            },
        ]
        charted: list[Any] = []
        latency_bars = charts.latency_bars

        def recording(frame: Any) -> Any:
            charted.append(frame)
            return latency_bars(frame)

        monkeypatch.setattr(charts, "latency_bars", recording)
        app = _click(_page("Health"), f"health_run_{NAME}")
        assert _rows(app.dataframe[1].value, "region", "healthy", "error") == [
            ("global", ["healthy"], ""),
            ("us-east-1", ["unhealthy"], ""),
            ("us-west-2", ["unhealthy"], "Could not reach the GCO API: ConnectTimeout"),
        ]
        assert [list(frame["region"]) for frame in charted] == [["global", "us-east-1"]]

    def test_regional_api_mode_probes_through_each_region_only(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["topology"] = topology(regional_api=True)
        fake_fleet.overrides["endpoint_health_all"] = lambda name, regions: [
            {"region": region, "healthy": True, "http_status": 200, "latency_ms": 900}
            for region in regions
        ]
        app = _page("Health")
        assert (
            "Calls the endpoint's health path through each of its Regions' APIs, the way "
            "`gco inference health -r REGION` does, and times each round trip."
            in _values(app.caption)
        )
        app = _click(app, f"health_run_{NAME}")
        assert fake_fleet.called("endpoint_health_all") == [
            ((NAME, ("us-east-1", "us-west-2")), {})
        ]
        assert _rows(app.dataframe[1].value, "region", "healthy") == [
            ("us-east-1", ["healthy"]),
            ("us-west-2", ["healthy"]),
        ]

    def test_an_endpoint_without_a_region_to_probe_says_so(self, fake_fleet: FakeFleet) -> None:
        # Regional API mode probes only the endpoint's Regions, and it has none.
        fake_fleet.overrides["topology"] = topology(regional_api=True)
        _serve(fake_fleet, sample_endpoint(target_regions=[], region_status={}))
        fake_fleet.overrides["endpoint_health_all"] = []
        app = _click(_page("Health"), f"health_run_{NAME}")
        assert fake_fleet.called("endpoint_health_all") == [((NAME, ()), {})]
        assert _values(app.info) == ["The endpoint has no target Region to probe."]
        assert len(app.dataframe) == 1  # the endpoint list only
        assert not app.get("vega_lite_chart")

    def test_results_for_another_endpoint_are_not_shown(self, fake_fleet: FakeFleet) -> None:
        other = ("llama-batch", [{"region": "global", "healthy": True, "latency_ms": 10}])
        app = _page("Health", **{inference.HEALTH_KEY: other})
        assert len(app.dataframe) == 1
        assert not fake_fleet.called("endpoint_health_all")


# ---------------------------------------------------------------------------
# Manage tab
# ---------------------------------------------------------------------------


def _replicas_3(app: AppTest) -> None:
    app.number_input(key=f"scale_{NAME}").set_value(3)


def _new_image(app: AppTest) -> None:
    app.text_input(key=f"image_{NAME}").input(NEW_IMAGE)


def _canary_at_25(app: AppTest) -> None:
    _new_image(app)
    app.number_input(key=f"canary_weight_{NAME}").set_value(25)


def _add_eu_west_1(app: AppTest) -> None:
    app.selectbox(key=f"add_region_{NAME}").select("eu-west-1")


def _drop_us_west_2(app: AppTest) -> None:
    app.selectbox(key=f"drop_region_{NAME}").select("us-west-2")


_CANARY = {"spec": _spec(canary={"image": NEW_IMAGE, "weight": 20})}

#: Writes the Manage tab makes as soon as their button is pressed: the endpoint
#: changes, the inputs set first, the button, and the fleet call and toast expected.
DIRECT_WRITES = [
    pytest.param(
        {},
        _replicas_3,
        f"scale_go_{NAME}",
        "scale_endpoint",
        ((NAME, 3), {}),
        f"Scaled {NAME} to 3 per Region",
        id="scale",
    ),
    pytest.param(
        {"desired_state": "stopped"},
        None,
        f"start_{NAME}",
        "start_endpoint",
        ((NAME,), {}),
        f"Started {NAME}",
        id="start",
    ),
    pytest.param(
        {},
        _canary_at_25,
        f"canary_{NAME}",
        "start_canary",
        # The page passes the weight; the fake records its own default replicas.
        ((NAME, NEW_IMAGE), {"weight": 25, "replicas": 1}),
        f"The canary of {NEW_IMAGE} takes 25% of {NAME}'s traffic",
        id="start-canary",
    ),
    pytest.param(
        _CANARY,
        None,
        f"promote_{NAME}",
        "promote_canary",
        ((NAME,), {}),
        "Canary promoted",
        id="promote-canary",
    ),
    pytest.param(
        _CANARY,
        None,
        f"rollback_{NAME}",
        "rollback_canary",
        ((NAME,), {}),
        "Canary removed",
        id="roll-back-canary",
    ),
    pytest.param(
        {"target_regions": ["us-east-1"]},
        _add_eu_west_1,
        f"add_region_go_{NAME}",
        "add_endpoint_region",
        ((NAME, "eu-west-1"), {}),
        f"{NAME} added to eu-west-1",
        id="add-region",
    ),
]

#: Writes that open a confirmation first: as above, plus the question asked.
CONFIRMED_WRITES = [
    pytest.param(
        _new_image,
        f"image_go_{NAME}",
        f"Replace `{IMAGE}` with `{NEW_IMAGE}` in every Region?",
        "update_endpoint_image",
        ((NAME, NEW_IMAGE), {}),
        f"Rolling {NAME} to {NEW_IMAGE}",
        id="roll-out-image",
    ),
    pytest.param(
        None,
        f"stop_{NAME}",
        f"Stop **{NAME}** in every Region? Its pods are scaled to zero; its configuration "
        "stays, so Start brings it back.",
        "stop_endpoint",
        ((NAME,), {}),
        f"Stopped {NAME}",
        id="stop",
    ),
    pytest.param(
        None,
        f"delete_{NAME}",
        f"Delete **{NAME}**? Each Region's monitor removes its Deployment, Service and "
        "routes. Redeploying needs the full spec again.",
        "delete_endpoint",
        ((NAME,), {}),
        f"Deleting {NAME}",
        id="delete",
    ),
    pytest.param(
        _drop_us_west_2,
        f"drop_region_go_{NAME}",
        f"Remove **{NAME}** from **us-west-2**? That Region's monitor deletes its pods.",
        "remove_endpoint_region",
        ((NAME, "us-west-2"), {}),
        f"{NAME} removed from us-west-2",
        id="remove-region",
    ),
]


def _manage(inputs: Callable[[AppTest], None] | None) -> AppTest:
    app = _page("Manage")
    if inputs is not None:
        inputs(app)
        app = _run(app)
    return app


class TestManageWrites:
    @pytest.mark.parametrize(
        ("endpoint", "inputs", "button", "method", "call", "toast"), DIRECT_WRITES
    )
    def test_a_write_runs_and_toasts(
        self,
        fake_fleet: FakeFleet,
        endpoint: dict[str, Any],
        inputs: Callable[[AppTest], None] | None,
        button: str,
        method: str,
        call: tuple[Any, ...],
        toast: str,
    ) -> None:
        _serve(fake_fleet, sample_endpoint(**endpoint))
        app = _click(_manage(inputs), button)
        assert fake_fleet.called(method) == [call]
        assert _values(app.toast) == [toast]
        assert not app.error

    @pytest.mark.parametrize(
        ("endpoint", "inputs", "button", "method", "call", "toast"), DIRECT_WRITES
    )
    def test_a_refused_write_draws_the_error_card(
        self,
        fake_fleet: FakeFleet,
        endpoint: dict[str, Any],
        inputs: Callable[[AppTest], None] | None,
        button: str,
        method: str,
        call: tuple[Any, ...],
        toast: str,
    ) -> None:
        _serve(fake_fleet, sample_endpoint(**endpoint))
        fake_fleet.fail[method] = _refusal()
        app = _click(_manage(inputs), button)
        assert fake_fleet.called(method) == [call]
        assert _values(app.error) == [REFUSAL_CARD]
        assert not app.toast

    @pytest.mark.parametrize(
        ("inputs", "button", "question", "method", "call", "toast"), CONFIRMED_WRITES
    )
    def test_a_confirmed_write_asks_first_then_runs_and_toasts(
        self,
        fake_fleet: FakeFleet,
        confirmations: list[Confirmation],
        inputs: Callable[[AppTest], None] | None,
        button: str,
        question: str,
        method: str,
        call: tuple[Any, ...],
        toast: str,
    ) -> None:
        app = _click(_manage(inputs), button)
        assert [opened.message for opened in confirmations] == [question]
        assert question in _values(app.markdown)
        assert not fake_fleet.called(method)
        dialog = _confirm(confirmations[0])
        assert fake_fleet.called(method) == [call]
        assert _values(dialog.toast) == [toast]

    @pytest.mark.parametrize(
        ("inputs", "button", "question", "method", "call", "toast"), CONFIRMED_WRITES
    )
    def test_a_refused_confirmed_write_draws_the_error_card(
        self,
        fake_fleet: FakeFleet,
        confirmations: list[Confirmation],
        inputs: Callable[[AppTest], None] | None,
        button: str,
        question: str,
        method: str,
        call: tuple[Any, ...],
        toast: str,
    ) -> None:
        fake_fleet.fail[method] = _refusal()
        _click(_manage(inputs), button)
        dialog = _confirm(confirmations[0])
        assert fake_fleet.called(method) == [call]
        assert _values(dialog.error) == [REFUSAL_CARD]
        assert not dialog.toast


class TestManageControls:
    @pytest.mark.parametrize(
        ("stored", "shown", "most"),
        [
            pytest.param(None, 1, 64, id="unset"),
            pytest.param(0, 0, 64, id="scaled-to-zero"),
            pytest.param(3, 3, 64, id="three"),
            # More than the input's usual limit (the CLI sets no limit) stays editable.
            pytest.param(100, 100, 100, id="above-the-limit"),
        ],
    )
    def test_a_fixed_size_endpoint_is_scaled_by_hand_from_its_count(
        self, fake_fleet: FakeFleet, stored: int | None, shown: int, most: int
    ) -> None:
        _serve(fake_fleet, sample_endpoint(spec=_spec(replicas=stored)))
        app = _page("Manage")
        replicas = app.number_input(key=f"scale_{NAME}")
        assert (replicas.value, replicas.max, replicas.disabled, replicas.help) == (
            shown,
            most,
            False,
            "",
        )
        assert not app.button(key=f"scale_go_{NAME}").disabled

    def test_an_autoscaled_endpoint_is_not(self, fake_fleet: FakeFleet) -> None:
        autoscaling = {"enabled": True, "min_replicas": 1, "max_replicas": 4}
        _serve(fake_fleet, sample_endpoint(spec=_spec(replicas=2, autoscaling=autoscaling)))
        app = _page("Manage")
        replicas = app.number_input(key=f"scale_{NAME}")
        assert (replicas.value, replicas.disabled) == (2, True)
        assert replicas.help == "Its HPA or KEDA object scales an autoscaled endpoint"
        assert app.button(key=f"scale_go_{NAME}").disabled

    @pytest.mark.parametrize(("typed", "enabled"), [(IMAGE, False), ("", False), (NEW_IMAGE, True)])
    def test_rollout_and_canary_need_a_different_image(
        self, fake_fleet: FakeFleet, typed: str, enabled: bool
    ) -> None:
        app = _page("Manage")
        app.text_input(key=f"image_{NAME}").input(typed)
        app = _run(app)
        assert app.button(key=f"image_go_{NAME}").disabled is not enabled
        assert app.button(key=f"canary_{NAME}").disabled is not enabled

    def test_a_running_canary_is_described_with_its_actions(self, fake_fleet: FakeFleet) -> None:
        _serve(fake_fleet, sample_endpoint(**_CANARY))
        app = _page("Manage")
        assert f"Canary running: `{NEW_IMAGE}` takes 20%" in _values(app.caption)
        assert {f"promote_{NAME}", f"rollback_{NAME}"} <= _keys(app.button)

    def test_without_a_canary_there_is_nothing_to_promote(self, fake_fleet: FakeFleet) -> None:
        app = _page("Manage")
        assert not any(c.startswith("Canary running") for c in _values(app.caption))
        assert not {f"promote_{NAME}", f"rollback_{NAME}"} & _keys(app.button)

    @pytest.mark.parametrize(
        ("state", "shown", "hidden"),
        [
            ("running", f"stop_{NAME}", f"start_{NAME}"),
            ("stopped", f"start_{NAME}", f"stop_{NAME}"),
        ],
    )
    def test_the_lifecycle_button_matches_the_state(
        self, fake_fleet: FakeFleet, state: str, shown: str, hidden: str
    ) -> None:
        _serve(fake_fleet, sample_endpoint(desired_state=state))
        keys = _keys(_page("Manage").button)
        assert {shown, f"delete_{NAME}"} <= keys
        assert hidden not in keys

    @pytest.mark.parametrize(
        ("workload", "targets", "add", "remove", "everywhere"),
        [
            pytest.param(("us-east-1",), ["us-east-1"], None, None, True, id="the-only-region"),
            pytest.param(
                REGIONS,
                ["us-east-1"],
                [region_label("us-west-2"), region_label("eu-west-1")],
                None,
                False,
                id="one-of-three",
            ),
            pytest.param(
                REGIONS,
                list(REGIONS),
                None,
                [region_label(r) for r in REGIONS],
                False,
                id="all-three",
            ),
        ],
    )
    def test_region_controls_offer_what_can_change(
        self,
        fake_fleet: FakeFleet,
        workload: tuple[str, ...],
        targets: list[str],
        add: list[str] | None,
        remove: list[str] | None,
        everywhere: bool,
    ) -> None:
        fake_fleet.overrides["topology"] = topology(workload_regions=workload)
        _serve(fake_fleet, sample_endpoint(target_regions=targets))
        app = _page("Manage")
        offered = {box.key: box.options for box in app.selectbox}
        assert offered.get(f"add_region_{NAME}") == add
        assert offered.get(f"drop_region_{NAME}") == remove
        assert ("Serving in every workload Region." in _values(app.caption)) is everywhere

    def test_the_cli_hint_follows_the_inputs(self, fake_fleet: FakeFleet) -> None:
        app = _page("Manage")
        assert _values(app.code) == [
            f"gco inference status {NAME}\n"
            f"gco inference scale {NAME} -r 1\n"
            f"gco inference update-image {NAME} -i {IMAGE}\n"
            f"gco inference invoke {NAME} -p Hello"
        ]
        app.number_input(key=f"scale_{NAME}").set_value(3)
        app.text_input(key=f"image_{NAME}").input("")
        app = _run(app)
        assert _values(app.code)[0].splitlines()[1:3] == [
            f"gco inference scale {NAME} -r 3",
            f"gco inference update-image {NAME} -i IMAGE",
        ]


# ---------------------------------------------------------------------------
# Deploy dialog
# ---------------------------------------------------------------------------


def _registration(fake: FakeFleet) -> dict[str, Any]:
    (call,) = fake.called("deploy_endpoint")
    assert call[0] == ()
    return call[1]


class TestDeployDialog:
    def test_opens_on_the_vllm_preset(self, fake_fleet: FakeFleet) -> None:
        app = _deploy_dialog()
        assert app.segmented_control(key="deploy_preset").options == [
            "vLLM",
            "SGLang",
            "Triton",
            "Custom",
        ]
        assert app.selectbox(key="deploy_capacity").options == [
            "Spot when available",
            "Spot only",
            "On-Demand",
        ]
        assert app.multiselect(key="deploy_regions").value == list(REGIONS)
        assert _values(app.code) == [
            f"gco inference deploy NAME -i {IMAGE} --framework vllm -e MODEL={MODEL} "
            "--extra-args=--max-model-len --extra-args 4096"
        ]
        assert not app.warning
        # Nothing to deploy until the endpoint has a name.
        assert app.button(key="deploy_go").disabled
        # Only a preset with a NAME placeholder explains it.
        assert app.text_area(key="deploy_args_vllm").help == ""

    @pytest.mark.parametrize("name", ["Qwen-chat", "7b-chat", "qwen_chat", "qwen-"])
    def test_an_invalid_name_is_refused(self, fake_fleet: FakeFleet, name: str) -> None:
        app = _deploy_dialog()
        app.text_input(key="deploy_name").input(name)
        app = _run(app)
        assert _values(app.warning) == [NAME_RULE]
        assert app.button(key="deploy_go").disabled

    @pytest.mark.parametrize("name", ["q", "qwen-2-chat", "a" * 55])
    def test_a_valid_name_enables_deploy(self, fake_fleet: FakeFleet, name: str) -> None:
        app = _deploy_dialog()
        app.text_input(key="deploy_name").input(name)
        app = _run(app)
        assert not app.warning
        assert not app.button(key="deploy_go").disabled

    def test_an_image_and_a_region_are_required(self, fake_fleet: FakeFleet) -> None:
        app = _deploy_dialog()
        app.text_input(key="deploy_name").input(NAME)
        app.segmented_control(key="deploy_preset").set_value("custom")
        app.multiselect(key="deploy_regions").set_value([])
        app = _run(app)
        assert _values(app.warning) == ["Name a container image.", "Pick at least one Region."]
        assert app.button(key="deploy_go").disabled

    def test_deploys_the_preset_as_shown(self, fake_fleet: FakeFleet) -> None:
        app = _deploy_dialog()
        app.text_input(key="deploy_name").input(NAME)
        app = _click(_run(app), "deploy_go")
        assert _registration(fake_fleet) == {
            "endpoint_name": NAME,
            "image": IMAGE,
            "framework": "vllm",
            "target_regions": list(REGIONS),
            "replicas": 1,
            "gpu_count": 1,
            "gpu_type": None,
            "port": 8000,
            "health_check_path": "/health",
            "env": {"MODEL": MODEL},
            "autoscaling": None,
            "capacity_type": None,
            "extra_args": ["--max-model-len", "4096"],
        }
        assert _values(app.toast) == [f"{NAME} registered; each Region's monitor is creating it"]

    def test_every_option_reaches_the_command_and_the_registration(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = _deploy_dialog()
        app.segmented_control(key="deploy_preset").set_value("sglang")
        app.text_input(key="deploy_name").input("sg-chat")
        app.multiselect(key="deploy_regions").set_value(["us-east-1", "eu-west-1"])
        app.number_input(key="deploy_replicas").set_value(2)
        app.number_input(key="deploy_gpus").set_value(4)
        app.selectbox(key="deploy_capacity").select("on-demand")
        app.text_input(key="deploy_gpu_type").input("g6e.xlarge")
        app.toggle(key="deploy_autoscale").set_value(True)
        app = _run(app)
        app.text_input(key="deploy_health_sglang").input("/ready")
        app.text_area(key="deploy_args_sglang").input("--tp\n2\n")
        # A maximum below the minimum is raised to it.
        app.number_input(key="deploy_min").set_value(2)
        app.number_input(key="deploy_max").set_value(1)
        app.selectbox(key="deploy_metric").select("cpu:70")
        app = _run(app)
        assert _values(app.code) == [
            "gco inference deploy sg-chat -i lmsysorg/sglang:v0.5.23 --framework sglang "
            f"--port 30000 -e MODEL={MODEL} --health-path /ready -r us-east-1 -r eu-west-1 "
            "--replicas 2 --gpu-count 4 --gpu-type g6e.xlarge --capacity-type on-demand "
            "--min-replicas 2 --max-replicas 2 --autoscale-metric cpu:70 "
            "--extra-args=--tp --extra-args 2"
        ]
        app = _click(app, "deploy_go")
        assert _registration(fake_fleet) == {
            "endpoint_name": "sg-chat",
            "image": "lmsysorg/sglang:v0.5.23",
            "framework": "sglang",
            "target_regions": ["us-east-1", "eu-west-1"],
            "replicas": 2,
            "gpu_count": 4,
            "gpu_type": "g6e.xlarge",
            "port": 30000,
            "health_check_path": "/ready",
            "env": {"MODEL": MODEL},
            "autoscaling": {
                "enabled": True,
                "min_replicas": 2,
                "max_replicas": 2,
                "metrics": [{"type": "cpu", "target": 70}],
            },
            "capacity_type": "on-demand",
            "extra_args": ["--tp", "2"],
        }

    def test_triton_serves_the_endpoints_model_repository(self, fake_fleet: FakeFleet) -> None:
        app = _deploy_dialog()
        app.text_input(key="deploy_name").input("tri-chat")
        app.segmented_control(key="deploy_preset").set_value("triton")
        app = _run(app)
        assert _values(app.code) == [
            "gco inference deploy tri-chat -i nvcr.io/nvidia/tritonserver:26.09-py3 "
            "--health-path /v2/health/ready --extra-args tritonserver "
            "--extra-args=--model-repository=/models/tri-chat"
        ]
        app = _click(app, "deploy_go")
        registered = _registration(fake_fleet)
        assert (registered["framework"], registered["env"], registered["health_check_path"]) == (
            None,
            None,
            "/v2/health/ready",
        )
        assert registered["extra_args"] == ["tritonserver", "--model-repository=/models/tri-chat"]

    def test_triton_takes_a_name_typed_after_the_preset_was_picked(
        self, fake_fleet: FakeFleet
    ) -> None:
        # The box keeps the value it was first drawn with, so the name is
        # filled in from the preset's arguments, not from the box.
        app = _deploy_dialog()
        app.segmented_control(key="deploy_preset").set_value("triton")
        app = _run(app)
        box = app.text_area(key="deploy_args_triton")
        assert box.value == "tritonserver\n--model-repository=/models/NAME"
        assert box.help == "Until you edit them, `NAME` here stands for the endpoint's name."
        app.text_input(key="deploy_name").input("tri-chat")
        app = _run(app)
        assert _values(app.code) == [
            "gco inference deploy tri-chat -i nvcr.io/nvidia/tritonserver:26.09-py3 "
            "--health-path /v2/health/ready --extra-args tritonserver "
            "--extra-args=--model-repository=/models/tri-chat"
        ]
        app = _click(app, "deploy_go")
        assert _registration(fake_fleet)["extra_args"] == [
            "tritonserver",
            "--model-repository=/models/tri-chat",
        ]

    @pytest.mark.parametrize(
        ("text", "command", "sent"),
        [
            (
                "tritonserver\n\n--model-repository=/models/NAME\n",
                "--extra-args tritonserver --extra-args=--model-repository=/models/tri-chat",
                ["tritonserver", "--model-repository=/models/tri-chat"],
            ),
            (
                "tritonserver\n--model-repository=/models/NAME\n--log-verbose=1",
                "--extra-args tritonserver --extra-args=--model-repository=/models/NAME "
                "--extra-args=--log-verbose=1",
                ["tritonserver", "--model-repository=/models/NAME", "--log-verbose=1"],
            ),
        ],
        ids=["blank-lines-are-not-an-edit", "an-edit-is-sent-as-written"],
    )
    def test_only_the_presets_own_arguments_take_the_name(
        self, fake_fleet: FakeFleet, text: str, command: str, sent: list[str]
    ) -> None:
        app = _deploy_dialog()
        app.segmented_control(key="deploy_preset").set_value("triton")
        app.text_input(key="deploy_name").input("tri-chat")
        app = _run(app)
        app.text_area(key="deploy_args_triton").input(text)
        app = _run(app)
        assert _values(app.code) == [
            "gco inference deploy tri-chat -i nvcr.io/nvidia/tritonserver:26.09-py3 "
            f"--health-path /v2/health/ready {command}"
        ]
        app = _click(app, "deploy_go")
        assert _registration(fake_fleet)["extra_args"] == sent

    def test_cleared_optional_fields_fall_back_to_the_defaults(self, fake_fleet: FakeFleet) -> None:
        app = _deploy_dialog()
        app.text_input(key="deploy_name").input(NAME)
        app.text_input(key="deploy_model_vllm").input("")
        app.text_input(key="deploy_health_vllm").input("")
        app.text_area(key="deploy_args_vllm").input("")
        app = _run(app)
        assert _values(app.code) == [f"gco inference deploy {NAME} -i {IMAGE} --framework vllm"]
        app = _click(app, "deploy_go")
        registered = _registration(fake_fleet)
        assert (registered["env"], registered["health_check_path"], registered["extra_args"]) == (
            None,
            "/health",
            None,
        )

    def test_a_refused_deployment_draws_the_error_card(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["deploy_endpoint"] = DashboardError("Endpoint 'qwen-chat' already exists.")
        app = _deploy_dialog()
        app.text_input(key="deploy_name").input(NAME)
        app = _click(_run(app), "deploy_go")
        assert len(fake_fleet.called("deploy_endpoint")) == 1
        assert _values(app.error) == ["**Endpoint 'qwen-chat' already exists.**"]
        assert not app.toast
