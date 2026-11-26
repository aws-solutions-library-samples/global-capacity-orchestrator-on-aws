"""The dashboard's shared Streamlit plumbing (``cli.dashboard.ui``) and its entrypoint (``app.py``).

The cached ``load_*`` readers are called directly against the in-memory
``FakeFleet`` from ``tests/_dashboard_fakes.py``. Everything that needs a
script run (session state, widgets, toasts, dialogs, the sidebar) renders
under Streamlit's AppTest through a one-line script that calls a body the test
supplies on every run, so the body's Streamlit calls land in that run. The
access-token gate, the page set-up and the navigation are exercised through
the real ``app.py``. Pure helpers are plain unit tests.
"""

from __future__ import annotations

import functools
import hmac
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx
from streamlit.testing.v1 import AppTest

from cli.dashboard import brand, data, ui
from cli.dashboard.data import DashboardError, UtilizationRows
from tests._dashboard_fakes import (  # noqa: F401
    APP,
    REGIONS,
    FakeFleet,
    fake_fleet_fixture,
    topology,
)

JOB = ("train-1", "gco-jobs", "us-east-1")
LABELS = ["us-east-1 · N. Virginia", "us-west-2 · Oregon", "eu-west-1 · Ireland"]
TOKEN = "test-token"
SIDEBAR_FOOTER = (
    "Every change made here runs as the signed-in AWS principal, exactly as the matching "
    "`gco` command would."
)
LOCKED = (
    "This dashboard needs its access link. Run `gco dashboard open` (or `gco dashboard status`) "
    "on the machine that started it."
)


def _body_script(body: Callable[[], None]) -> None:
    body()


def run_body(
    body: Callable[[], None],
    *,
    state: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> AppTest:
    """Run ``body`` as an app script once, with ``state`` and ``context`` set first."""
    app = AppTest.from_function(_body_script, kwargs={"body": body}, default_timeout=30)
    for key, value in (state or {}).items():
        app.session_state[key] = value
    app.context.update(context or {})
    return app.run()


def values(elements: Any) -> list[Any]:
    return [element.value for element in elements]


def spy(monkeypatch: pytest.MonkeyPatch, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Record every call to ``st.<name>``; the real function still runs."""
    real = getattr(st, name)
    seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        seen.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(st, name, wrapper)
    return seen


# ---------------------------------------------------------------------------
# The data object and the cached reads
# ---------------------------------------------------------------------------


def test_fleet_builds_one_data_object_from_the_cli_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(project_name="gco")
    built: list[object] = []

    class Recorder:
        def __init__(self, given: object) -> None:
            built.append(given)

    monkeypatch.setattr(ui, "get_config", lambda: config)
    monkeypatch.setattr(ui, "FleetData", Recorder)
    ui.fleet.clear()
    try:
        first, second = ui.fleet(), ui.fleet()
    finally:
        ui.fleet.clear()
    assert isinstance(first, Recorder)
    assert first is second
    assert built == [config]


#: (loader, its arguments, the FleetData method it reads, and that call's args and kwargs).
LOADERS: list[tuple[str, tuple[Any, ...], str, tuple[Any, ...], dict[str, Any]]] = [
    ("load_topology", (), "topology", (), {}),
    ("load_identity", (), "identity", (), {}),
    ("load_fleet_status", (), "fleet_status", (), {"with_costs": False, "with_policy": False}),
    (
        "load_fleet_status_with_policy",
        (),
        "fleet_status",
        (),
        {"with_costs": False, "with_policy": True},
    ),
    ("load_cluster_metrics", (REGIONS,), "cluster_metrics", (REGIONS,), {}),
    (
        "load_utilization",
        (REGIONS, 3),
        "utilization_history",
        (REGIONS,),
        {"hours": 3, "period": 60},
    ),
    ("load_jobs", (REGIONS,), "jobs", (), {}),
    (
        "load_queue_jobs",
        ("queued", "us-east-1"),
        "queue_jobs",
        (),
        {"status": "queued", "target_region": "us-east-1"},
    ),
    ("load_queue_stats", (), "queue_stats", (), {}),
    ("load_endpoints", (), "endpoints", (), {}),
    ("load_examples", (), "examples", (), {}),
    ("load_costs", (30,), "costs", (), {"days": 30}),
    (
        "load_spot_history",
        ("g5.xlarge", ("us-west-2",), 7),
        "spot_price_history",
        ("g5.xlarge", "us-west-2"),
        {"days": 7},
    ),
    (
        "load_instance_report",
        ("g5.xlarge", REGIONS),
        "instance_report",
        ("g5.xlarge", REGIONS),
        {},
    ),
    ("load_global_health", (), "global_health", (), {}),
    ("load_regional_status", (REGIONS,), "regional_status", (REGIONS,), {}),
    ("load_templates", (), "templates", (), {}),
    ("load_webhooks", (), "webhooks", (), {}),
    ("load_regional_policy", (REGIONS,), "regional_policy", (REGIONS,), {}),
    ("load_cost_monitors", (REGIONS,), "cost_monitors", (REGIONS,), {}),
    (
        "load_allocation_costs",
        (7, "hourly"),
        "allocation_costs",
        (),
        {"days": 7, "granularity": "hourly"},
    ),
    ("load_job_detail", JOB, "job_detail", JOB, {}),
    ("load_job_pods", JOB, "job_pods", JOB, {}),
    ("load_job_events", JOB, "job_events", JOB, {}),
    ("load_job_metrics", JOB, "job_metrics", JOB, {}),
    ("load_job_logs", (*JOB, 50, "main"), "job_logs", JOB, {"tail": 50, "container": "main"}),
    ("load_queue_job", ("q-2",), "queue_job", ("q-2",), {}),
    (
        "load_served_model",
        ("qwen-chat", "us-east-1"),
        "served_model",
        ("qwen-chat", "us-east-1"),
        {},
    ),
    (
        "load_placement_score",
        ("g5.xlarge", "us-west-2", 4),
        "placement_score",
        ("g5.xlarge", "us-west-2"),
        {"capacity": 4},
    ),
]

#: Loaders "Refresh now" and every write keep: billed or slow-moving reads.
KEPT_BY_REFRESH = {
    "load_identity",
    "load_costs",
    "load_spot_history",
    "load_instance_report",
    "load_allocation_costs",
    "load_served_model",
    "load_placement_score",
}


def test_the_loader_table_covers_every_loader() -> None:
    assert {row[0] for row in LOADERS} == {name for name in dir(ui) if name.startswith("load_")}


@pytest.mark.parametrize(
    ("loader", "args", "method", "called_args", "called_kwargs"),
    LOADERS,
    ids=[row[0] for row in LOADERS],
)
def test_each_loader_reads_its_fleet_method_once_and_caches_the_answer(
    fake_fleet: FakeFleet,
    loader: str,
    args: tuple[Any, ...],
    method: str,
    called_args: tuple[Any, ...],
    called_kwargs: dict[str, Any],
) -> None:
    answer = [{"answer": method}]
    fake_fleet.overrides[method] = lambda *_args, **_kwargs: answer
    read = getattr(ui, loader)
    assert read(*args) == answer
    assert read(*args) == answer
    assert fake_fleet.called(method) == [(called_args, called_kwargs)]


def test_utilization_over_more_than_six_hours_reads_five_minute_samples(
    fake_fleet: FakeFleet,
) -> None:
    ui.load_utilization(REGIONS, 24)
    assert fake_fleet.called("utilization_history") == [((REGIONS,), {"hours": 24, "period": 300})]


def test_the_regions_utilization_could_not_read_survive_the_cache(fake_fleet: FakeFleet) -> None:
    sample = {"region": "us-east-1", "metric": "cpu", "timestamp": "2026-11-25T18:00:00Z"}
    fake_fleet.overrides["utilization_history"] = UtilizationRows(
        [{**sample, "value": 3.0}], {"us-west-2": "Throttling: Rate exceeded"}
    )
    for _ in range(2):  # the first read, then the cached copy
        rows = ui.load_utilization(REGIONS, 3)
        assert rows == [{**sample, "value": 3.0}]
        assert isinstance(rows, UtilizationRows)
        assert rows.errors == {"us-west-2": "Throttling: Rate exceeded"}
    assert len(fake_fleet.called("utilization_history")) == 1


def test_spot_history_reads_each_region_and_joins_the_rows(fake_fleet: FakeFleet) -> None:
    rows = ui.load_spot_history("g5.xlarge", ("us-east-1", "us-west-2"), 14)
    assert fake_fleet.called("spot_price_history") == [
        (("g5.xlarge", "us-east-1"), {"days": 14}),
        (("g5.xlarge", "us-west-2"), {"days": 14}),
    ]
    assert [row["region"] for row in rows] == ["us-east-1"] * 6 + ["us-west-2"] * 6


def test_a_failed_read_is_not_cached(fake_fleet: FakeFleet) -> None:
    fake_fleet.fail["queue_stats"] = DashboardError("throttled")
    with pytest.raises(DashboardError, match="throttled"):
        ui.load_queue_stats()
    del fake_fleet.fail["queue_stats"]
    assert ui.load_queue_stats()["summary"]["total_jobs"] == 3
    assert len(fake_fleet.called("queue_stats")) == 2


def test_refresh_drops_the_live_reads_and_endpoint_discovery_but_keeps_billed_reads(
    fake_fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = ("gco-api-gateway", "us-east-2")
    monkeypatch.setitem(data._GLOBAL_ENDPOINTS, stale, (float("inf"), None))
    monkeypatch.setitem(data._REGIONAL_ENDPOINTS, stale, (float("inf"), None))
    first_args = {}
    for loader, args, *_ in LOADERS:
        first_args.setdefault(loader, args)
    for loader, args in first_args.items():
        getattr(ui, loader)(*args)

    ui.refresh()

    reread = set()
    for loader, args in first_args.items():
        before = len(fake_fleet.calls)
        getattr(ui, loader)(*args)
        if len(fake_fleet.calls) > before:
            reread.add(loader)
    assert reread == set(first_args) - KEPT_BY_REFRESH
    assert {loader.__name__ for loader in ui.LIVE_LOADERS} == reread
    assert stale not in data._GLOBAL_ENDPOINTS
    assert stale not in data._REGIONAL_ENDPOINTS


# ---------------------------------------------------------------------------
# Toasts, prefetching, failures and confirmations
# ---------------------------------------------------------------------------


class TestFlash:
    def test_toasts_queued_before_a_rerun_show_on_the_next_run_only(
        self, fake_fleet: FakeFleet
    ) -> None:
        runs: list[int] = []

        def body() -> None:
            runs.append(1)
            ui.show_flash()
            if st.button("Save", key="save"):
                ui.flash("Saved")
                ui.flash("Stopped qwen-chat", icon=":material/stop_circle:")
                st.rerun()

        app = run_body(body)
        assert not app.toast

        app.button(key="save").click().run()
        assert len(runs) == 3
        assert [(toast.value, toast.proto.icon) for toast in app.toast] == [
            ("Saved", ":material/check_circle:"),
            ("Stopped qwen-chat", ":material/stop_circle:"),
        ]
        assert ui.FLASH_KEY not in app.session_state

        app.run()
        assert not app.toast

    def test_show_flash_with_nothing_queued_draws_nothing(self, fake_fleet: FakeFleet) -> None:
        app = run_body(ui.show_flash)
        assert not app.toast
        assert not app.exception


class TestPrefetch:
    def test_loaders_run_on_threads_that_carry_the_script_run_context(
        self, fake_fleet: FakeFleet
    ) -> None:
        seen: dict[str, Any] = {}
        probes: list[tuple[int, object]] = []

        def probe() -> None:
            probes.append((threading.get_ident(), get_script_run_ctx(suppress_warning=True)))

        def broken() -> None:
            raise RuntimeError("the API is unreachable")

        def body() -> None:
            seen["thread"] = threading.get_ident()
            seen["context"] = get_script_run_ctx()
            ui.prefetch(
                ui.load_queue_stats,
                broken,
                probe,
                functools.partial(ui.load_cluster_metrics, REGIONS),
                message="Warming up...",
            )
            seen["stats"] = ui.load_queue_stats()
            seen["metrics"] = ui.load_cluster_metrics(REGIONS)

        app = run_body(body)
        assert not app.exception
        assert not app.error
        [(thread, context)] = probes
        assert thread != seen["thread"]
        assert context is seen["context"]
        # The page's own reads were answered from the cache the threads filled.
        assert len(fake_fleet.called("queue_stats")) == 1
        assert len(fake_fleet.called("cluster_metrics")) == 1
        assert seen["stats"]["summary"]["total_jobs"] == 3
        assert [read.region for read in seen["metrics"]] == list(REGIONS)

    def test_a_slow_loader_is_abandoned_after_the_timeout(self, fake_fleet: FakeFleet) -> None:
        release, finished = threading.Event(), threading.Event()
        waited: list[bool] = []

        def slow() -> None:
            release.wait(30)
            finished.set()

        def body() -> None:
            ui.prefetch(slow, timeout=0.01)
            waited.append(finished.is_set())

        run_body(body)
        release.set()
        assert finished.wait(30)
        assert waited == [False]

    def test_hung_loaders_share_one_timeout(
        self, fake_fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = threading.Event()
        waits: list[float | None] = []

        class RecordingThread(threading.Thread):
            def join(self, timeout: float | None = None) -> None:
                waits.append(timeout)
                super().join(timeout)

        monkeypatch.setattr(ui, "threading", SimpleNamespace(Thread=RecordingThread))

        def hung() -> None:
            release.wait(30)

        try:
            run_body(lambda: ui.prefetch(hung, hung, hung, timeout=0.2))
        finally:
            release.set()
        # One deadline for all three: the first join waits it out and the
        # others find it passed (a timeout each would wait 0.6 s in all).
        assert len(waits) == 3
        assert all(wait is not None for wait in waits)
        assert sum(wait or 0.0 for wait in waits) <= 0.25
        assert max(wait or 0.0 for wait in waits[1:]) < 0.05


class TestFailures:
    @pytest.mark.parametrize(
        ("error", "card"),
        [
            (
                DashboardError("Quota exceeded.", hint="Ask for a higher limit."),
                "**Quota exceeded.**\n\nAsk for a higher limit.",
            ),
            (DashboardError("Quota exceeded."), "**Quota exceeded.**"),
        ],
        ids=["with-hint", "without-hint"],
    )
    def test_show_error_draws_the_message_and_any_hint(
        self, fake_fleet: FakeFleet, error: DashboardError, card: str
    ) -> None:
        app = run_body(lambda: ui.show_error(error))
        assert values(app.error) == [card]
        assert app.error[0].icon == ":material/error:"

    def test_show_error_draws_where_it_is_told(self, fake_fleet: FakeFleet) -> None:
        app = run_body(lambda: ui.show_error(DashboardError("Gone."), where=st.sidebar))
        assert values(app.sidebar.error) == ["**Gone.**"]
        assert not app.main.error

    def test_attempt_returns_the_reads_answer(self, fake_fleet: FakeFleet) -> None:
        seen: list[Any] = []
        app = run_body(lambda: seen.append(ui.attempt(lambda a, *, b: a + b, 2, b=3)))
        assert seen == [5]
        assert not app.error

    def test_attempt_turns_a_failure_into_an_error_card_and_none(
        self, fake_fleet: FakeFleet
    ) -> None:
        seen: list[Any] = []

        def broken() -> None:
            raise ValueError("bad payload")

        app = run_body(lambda: seen.append(ui.attempt(broken, where=st.sidebar)))
        assert seen == [None]
        assert values(app.sidebar.error) == ["**ValueError: bad payload**"]

    def test_act_runs_the_write_refreshes_the_live_reads_and_queues_a_toast(
        self, fake_fleet: FakeFleet
    ) -> None:
        seen: dict[str, Any] = {}

        def body() -> None:
            ui.load_queue_stats()
            ui.load_costs(30)
            seen["result"] = ui.act("Deleting...", lambda: {"deleted": True}, success="Deleted")
            seen["flash"] = list(st.session_state.get(ui.FLASH_KEY, []))
            ui.load_queue_stats()
            ui.load_costs(30)

        app = run_body(body)
        assert seen == {
            "result": {"deleted": True},
            "flash": [("Deleted", ":material/check_circle:")],
        }
        assert not app.error
        assert len(fake_fleet.called("queue_stats")) == 2
        assert len(fake_fleet.called("costs")) == 1

    def test_act_reports_a_failed_write_and_changes_nothing(self, fake_fleet: FakeFleet) -> None:
        seen: dict[str, Any] = {}

        def refused() -> None:
            raise DashboardError("The API refused the manifests.", hint="Fix the namespace.")

        def body() -> None:
            ui.load_queue_stats()
            seen["result"] = ui.act("Submitting...", refused, success="Submitted", where=st.sidebar)
            seen["flash"] = st.session_state.get(ui.FLASH_KEY)
            ui.load_queue_stats()

        app = run_body(body)
        assert seen == {"result": None, "flash": None}
        assert values(app.sidebar.error) == [
            "**The API refused the manifests.**\n\nFix the namespace."
        ]
        assert len(fake_fleet.called("queue_stats")) == 1


class TestConfirm:
    @staticmethod
    def open(action: Callable[[], object], runs: list[int], **options: Any) -> AppTest:
        def body() -> None:
            runs.append(1)
            ui.show_flash()
            ui.confirm(
                "Delete **train-1** in us-east-1?",
                label="Delete",
                action=action,
                success="Deleted train-1",
                **options,
            )

        return run_body(body)

    def test_asks_before_running_anything(self, fake_fleet: FakeFleet) -> None:
        ran: list[str] = []
        app = self.open(lambda: ran.append("delete"), [])
        [dialog] = app.get("dialog")
        assert dialog.proto.dialog.title == "Please confirm"
        assert values(app.markdown) == ["Delete **train-1** in us-east-1?"]
        assert [(button.key, button.label) for button in app.button] == [
            ("confirm_cancel", "Cancel"),
            ("confirm_go", "Delete"),
        ]
        assert app.button(key="confirm_go").proto.icon == ":material/delete:"
        assert app.button(key="confirm_go").proto.type == "primary"
        assert ran == []

    def test_confirming_runs_the_action_and_toasts_after_the_rerun(
        self, fake_fleet: FakeFleet
    ) -> None:
        ran: list[str] = []
        runs: list[int] = []
        app = self.open(lambda: ran.append("delete") or {"deleted": True}, runs)
        ui.load_queue_stats()
        app.button(key="confirm_go").click().run()
        assert ran == ["delete"]
        assert len(runs) == 3
        assert [toast.value for toast in app.toast] == ["Deleted train-1"]
        ui.load_queue_stats()
        assert len(fake_fleet.called("queue_stats")) == 2

    def test_a_failed_action_keeps_the_dialog_open_with_the_error(
        self, fake_fleet: FakeFleet
    ) -> None:
        runs: list[int] = []

        def refused() -> None:
            raise DashboardError("The Job is already gone.")

        app = self.open(refused, runs)
        app.button(key="confirm_go").click().run()
        assert len(runs) == 2
        assert values(app.error) == ["**The Job is already gone.**"]
        assert not app.toast
        assert ui.FLASH_KEY not in app.session_state

    def test_cancel_closes_without_running_the_action(self, fake_fleet: FakeFleet) -> None:
        ran: list[str] = []
        runs: list[int] = []
        app = self.open(lambda: ran.append("delete"), runs)
        app.button(key="confirm_cancel").click().run()
        assert ran == []
        assert len(runs) == 3
        assert not app.toast

    def test_the_confirm_button_takes_the_callers_icon(self, fake_fleet: FakeFleet) -> None:
        app = self.open(lambda: None, [], icon=":material/stop_circle:")
        assert app.button(key="confirm_go").proto.icon == ":material/stop_circle:"


# ---------------------------------------------------------------------------
# The access token (through app.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def compared(fake_fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch) -> list[tuple[bytes, bytes]]:
    """Require ``?token=TOKEN`` and record each constant-time comparison the gate makes."""
    monkeypatch.setenv(ui.TOKEN_ENV, TOKEN)
    seen: list[tuple[bytes, bytes]] = []

    def compare_digest(supplied: bytes, expected: bytes) -> bool:
        seen.append((supplied, expected))
        return hmac.compare_digest(supplied, expected)

    monkeypatch.setattr(ui, "hmac", SimpleNamespace(compare_digest=compare_digest))
    return seen


def open_app(*, token: str | None = None) -> AppTest:
    app = AppTest.from_file(str(APP), default_timeout=60)
    if token is not None:
        app.query_params["token"] = token
    return app.run()


class TestRequireAccess:
    def test_without_a_configured_token_every_session_is_admitted(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = open_app()
        assert values(app.title) == ["Fleet"]
        assert "_admitted" not in app.session_state
        assert not app.warning

    def test_a_missing_token_locks_the_dashboard(
        self,
        fake_fleet: FakeFleet,
        compared: list[tuple[bytes, bytes]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        navigation = spy(monkeypatch, "navigation")
        app = AppTest.from_file(str(APP), default_timeout=60)
        app.session_state[ui.FLASH_KEY] = [("Saved", ":material/check_circle:")]
        app.run()
        assert values(app.title) == ["GCO dashboard"]
        assert values(app.warning) == [LOCKED]
        assert app.warning[0].icon == ":material/lock:"
        assert navigation == []
        assert fake_fleet.calls == []
        assert not app.sidebar.children
        assert not app.toast
        assert compared == []

    def test_a_wrong_token_locks_the_dashboard(
        self, fake_fleet: FakeFleet, compared: list[tuple[bytes, bytes]]
    ) -> None:
        app = open_app(token="guessed")
        assert values(app.title) == ["GCO dashboard"]
        assert values(app.warning) == [LOCKED]
        assert compared == [(b"guessed", TOKEN.encode())]
        assert "_admitted" not in app.session_state

    def test_the_right_token_admits_the_session(
        self, fake_fleet: FakeFleet, compared: list[tuple[bytes, bytes]]
    ) -> None:
        app = open_app(token=TOKEN)
        assert values(app.title) == ["Fleet"]
        assert app.session_state["_admitted"] is True
        assert compared == [(TOKEN.encode(), TOKEN.encode())]
        assert app.query_params == {"token": TOKEN}

    def test_an_admitted_session_puts_the_token_back_on_the_url(
        self, fake_fleet: FakeFleet, compared: list[tuple[bytes, bytes]]
    ) -> None:
        app = open_app(token=TOKEN)
        app.run()
        assert app.query_params == {"token": TOKEN}
        app.query_params.clear()
        app.switch_page("app_pages/costs.py")
        app.run()
        assert values(app.title) == ["Costs"]
        assert app.query_params == {"token": TOKEN}
        # Admitted once, the session is not asked for the token again.
        assert compared == [(TOKEN.encode(), TOKEN.encode())]


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("picked", "expected"),
    [
        (None, REGIONS),
        ([], REGIONS),
        (["eu-west-1", "us-east-1"], ("us-east-1", "eu-west-1")),
        (["ap-south-1"], REGIONS),
    ],
    ids=["unset", "nothing-picked", "topology-order", "unknown-region"],
)
def test_selected_regions_follows_the_sidebar_filter(
    fake_fleet: FakeFleet, picked: list[str] | None, expected: tuple[str, ...]
) -> None:
    seen: list[tuple[str, ...]] = []
    state = {} if picked is None else {ui.REGION_FILTER_KEY: picked}
    run_body(lambda: seen.append(ui.selected_regions(topology())), state=state)
    assert seen == [expected]


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, None),
        ({ui.AUTO_REFRESH_KEY: False, ui.REFRESH_SECONDS_KEY: 60}, None),
        ({ui.AUTO_REFRESH_KEY: True}, 30),
        ({ui.AUTO_REFRESH_KEY: True, ui.REFRESH_SECONDS_KEY: 60}, 60),
        ({ui.AUTO_REFRESH_KEY: True, ui.REFRESH_SECONDS_KEY: "soon"}, 30),
    ],
    ids=["unset", "off", "on-default", "on-60s", "on-garbled"],
)
def test_refresh_interval(
    fake_fleet: FakeFleet, state: dict[str, Any], expected: int | None
) -> None:
    seen: list[int | None] = []
    run_body(lambda: seen.append(ui.refresh_interval()), state=state)
    assert seen == [expected]


@pytest.mark.parametrize(
    ("theme", "dark"), [(None, True), ("dark", True), ("light", False)], ids=str
)
def test_is_dark_follows_the_browser_theme(
    fake_fleet: FakeFleet, theme: str | None, dark: bool
) -> None:
    seen: list[bool] = []
    run_body(lambda: seen.append(ui.is_dark()), context={} if theme is None else {"theme": theme})
    assert seen == [dark]


# ---------------------------------------------------------------------------
# Shared widgets
# ---------------------------------------------------------------------------


class _FrozenClock(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return datetime(2026, 11, 25, 18, 4, 5, tzinfo=UTC)


def test_page_header_shows_the_title_and_when_the_page_was_read(
    fake_fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ui, "datetime", _FrozenClock)
    app = run_body(lambda: ui.page_header("Capacity", "Where work lands", icon=":material/memory:"))
    assert values(app.title) == ["Capacity"]
    assert app.title[0].proto.icon == ":material/memory:"
    assert values(app.caption) == ["Where work lands · read 18:04:05 UTC"]


def test_cli_hint_lists_the_commands_as_one_bash_block(fake_fleet: FakeFleet) -> None:
    app = run_body(lambda: ui.cli_hint("gco jobs list", "gco jobs logs train-1"))
    [code] = app.code
    assert code.value == "gco jobs list\ngco jobs logs train-1"
    assert code.language == "bash"
    assert code.proto.wrap_lines is True


class TestSidebar:
    @staticmethod
    def open(runs: list[int] | None = None, **state: Any) -> AppTest:
        def body() -> None:
            (runs if runs is not None else []).append(1)
            ui.sidebar(ui.load_topology())

        return run_body(body, state=state)

    def test_summarises_the_deployment_and_the_signed_in_principal(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = self.open()
        side = app.sidebar
        assert values(side.markdown) == ["### gco"]
        assert values(side.caption) == [
            "Control plane us-east-2 · API us-east-2 · global API mode",
            ":material/badge: tester",
            SIDEBAR_FOOTER,
        ]
        assert not side.warning
        assert not side.error
        regions = side.multiselect(key=ui.REGION_FILTER_KEY)
        assert regions.options == LABELS
        assert regions.proto.placeholder == "All workload Regions"
        assert side.toggle(key=ui.AUTO_REFRESH_KEY).value is False
        interval = side.select_slider(key=ui.REFRESH_SECONDS_KEY)
        assert interval.options == ["15 s", "30 s", "1 min", "5 min"]
        assert interval.value == 30
        assert interval.disabled is True
        assert [button.label for button in side.button] == ["Refresh now"]
        assert not app.main.children

    def test_regional_api_mode_and_a_missing_configuration(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["topology"] = topology(
            regional_api=True, configured=False, workload_regions=()
        )
        side = self.open().sidebar
        assert (
            side.caption[0].value == "Control plane us-east-2 · API us-east-2 · regional API mode"
        )
        assert values(side.warning) == [
            "No workload Regions are configured. Start the dashboard from a GCO checkout "
            "(its cdk.json lists the Regions)."
        ]
        assert side.multiselect(key=ui.REGION_FILTER_KEY).options == []

    def test_an_unreadable_identity_shows_the_error_instead_of_the_principal(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["identity"] = DashboardError(
            "No AWS credentials were found.", hint="Sign in and refresh the page."
        )
        side = self.open().sidebar
        assert values(side.error) == [
            "**No AWS credentials were found.**\n\nSign in and refresh the page."
        ]
        assert ":material/badge: tester" not in values(side.caption)

    def test_auto_refresh_enables_the_interval(self, fake_fleet: FakeFleet) -> None:
        app = self.open()
        app.sidebar.toggle(key=ui.AUTO_REFRESH_KEY).set_value(True).run()
        interval = app.sidebar.select_slider(key=ui.REFRESH_SECONDS_KEY)
        assert interval.disabled is False
        interval.set_value(300).run()
        assert app.session_state[ui.REFRESH_SECONDS_KEY] == 300

    def test_refresh_now_drops_the_live_reads_and_reruns(
        self, fake_fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = ("gco-api-gateway", "us-east-2")
        monkeypatch.setitem(data._GLOBAL_ENDPOINTS, stale, (float("inf"), None))
        runs: list[int] = []
        app = self.open(runs)
        assert len(fake_fleet.called("topology")) == 1
        app.sidebar.button[0].click().run()
        assert len(runs) == 3
        assert len(fake_fleet.called("topology")) == 2
        assert len(fake_fleet.called("identity")) == 1
        assert stale not in data._GLOBAL_ENDPOINTS


def test_detail_header_puts_the_callers_actions_on_the_title_row(fake_fleet: FakeFleet) -> None:
    def body() -> None:
        head = ui.detail_header("train-1", ":blue-badge[running]")
        head.button("Delete", key="delete")

    app = run_body(body)
    [row] = app.main.children.values()
    assert [child.type for child in row.children.values()] == [
        "subheader",
        "markdown",
        "space",
        "button",
    ]
    assert values(row.subheader) == ["train-1"]
    assert values(row.markdown) == [":blue-badge[running]"]
    assert row.button(key="delete").label == "Delete"


class TestRegionPicker:
    def test_lists_the_workload_regions_and_returns_the_choice(self, fake_fleet: FakeFleet) -> None:
        seen: list[str] = []
        app = run_body(lambda: seen.append(ui.region_picker(topology(), key="pick")))
        box = app.selectbox(key="pick")
        assert box.label == "Region"
        assert box.options == LABELS
        assert box.disabled is False
        box.set_value("us-west-2").run()
        assert seen == ["us-east-1", "us-west-2"]

    def test_global_comes_first_when_allowed(self, fake_fleet: FakeFleet) -> None:
        seen: list[str] = []

        def body() -> None:
            seen.append(
                ui.region_picker(
                    topology(),
                    key="target",
                    label="Target",
                    allow_global=True,
                    help="Where the API sends it",
                    disabled=True,
                    width=240,
                )
            )

        box = run_body(body).selectbox(key="target")
        assert box.label == "Target"
        assert box.options == ["Global API (nearest healthy Region)", *LABELS]
        assert box.help == "Where the API sends it"
        assert box.disabled is True
        assert seen == [ui.GLOBAL]

    def test_no_regions_falls_back_to_global(self, fake_fleet: FakeFleet) -> None:
        seen: list[str] = []
        run_body(lambda: seen.append(ui.region_picker(topology(workload_regions=()), key="pick")))
        assert seen == [ui.GLOBAL]

    def test_regional_api_mode_never_offers_the_global_api(self, fake_fleet: FakeFleet) -> None:
        # An unpinned request rides the default Region's bridge there, so a
        # "Global API" choice would be a false label.
        seen: list[str] = []
        app = run_body(
            lambda: seen.append(
                ui.region_picker(topology(regional_api=True), key="target", allow_global=True)
            )
        )
        assert app.selectbox(key="target").options == LABELS
        assert seen == ["us-east-1"]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "rows", "expected"),
    [
        (SimpleNamespace(selection=SimpleNamespace(rows=[2])), 3, 2),
        (SimpleNamespace(selection=SimpleNamespace(rows=[0])), 1, 0),
        (SimpleNamespace(selection=SimpleNamespace(rows=[3])), 3, None),
        (SimpleNamespace(selection=SimpleNamespace(rows=[-1])), 3, None),
        (SimpleNamespace(selection=SimpleNamespace(rows=["2"])), 3, None),
        (SimpleNamespace(selection=SimpleNamespace(rows=[])), 3, None),
        (SimpleNamespace(selection=SimpleNamespace(rows=None)), 3, None),
        (SimpleNamespace(selection=None), 3, None),
        (None, 3, None),
    ],
    ids=[
        "in-range",
        "first",
        "past-the-end",
        "negative",
        "not-an-int",
        "empty",
        "rows-none",
        "no-selection",
        "no-event",
    ],
)
def test_selected_row(event: object, rows: int, expected: int | None) -> None:
    assert ui.selected_row(event, rows) == expected


@pytest.mark.parametrize(
    ("rows", "limit", "height"),
    [(0, 18, 73), (1, 18, 73), (5, 18, 213), (18, 18, 668), (40, 18, 668), (40, 3, 143)],
)
def test_table_height(rows: int, limit: int, height: int) -> None:
    assert ui.table_height(rows, limit=limit) == height


@pytest.mark.parametrize(("count", "text"), [(0, "0 jobs"), (1, "1 job"), (2, "2 jobs")], ids=str)
def test_plural(count: int, text: str) -> None:
    assert ui.plural(count, "job") == text


@pytest.mark.parametrize(
    ("column", "label", "colors"),
    [
        (ui.status_column, "Status", brand.STATUS_COLORS),
        (ui.endpoint_state_column, "State", brand.ENDPOINT_COLORS),
        (ui.health_column, "Health", brand.HEALTH_COLORS),
    ],
    ids=["status", "endpoint-state", "health"],
)
def test_coloured_label_columns(
    column: Callable[..., Any], label: str, colors: dict[str, str]
) -> None:
    default = column()
    assert default["label"] == label
    assert default["type_config"]["type"] == "multiselect"
    assert default["type_config"]["options"] == [
        {"value": value, "color": color} for value, color in colors.items()
    ]
    custom = column("Verdict", width="small", help="Per Region")
    assert (custom["label"], custom["width"], custom["help"]) == ("Verdict", "small", "Per Region")


class TestParsePairs:
    @pytest.mark.parametrize(
        ("text", "pairs"),
        [
            ("", {}),
            (" , \n ", {}),
            ("team=ml", {"team": "ml"}),
            ("team = ml, tier=gold\nowner=", {"team": "ml", "tier": "gold", "owner": ""}),
            ("query=a=b", {"query": "a=b"}),
            ("team=ml,team=data", {"team": "data"}),
        ],
        ids=["empty", "blank-entries", "one", "commas-and-lines", "equals-in-value", "last-wins"],
    )
    def test_parses_key_value_pairs(self, text: str, pairs: dict[str, str]) -> None:
        assert ui.parse_pairs(text) == pairs

    @pytest.mark.parametrize(
        ("text", "entry"),
        [("team=ml, gpu", "gpu"), ("=ml", "=ml"), ("  = x", "= x")],
        ids=["no-separator", "no-key", "blank-key"],
    )
    def test_names_the_first_entry_that_is_not_a_pair(self, text: str, entry: str) -> None:
        with pytest.raises(DashboardError) as raised:
            ui.parse_pairs(text, what="query parameter")
        assert str(raised.value) == (f"'{entry}' is not a query parameter of the form key=value.")

    def test_the_default_noun_is_label(self) -> None:
        with pytest.raises(DashboardError, match=r"^'gpu' is not a label of the form key=value\.$"):
            ui.parse_pairs("gpu")


@pytest.mark.parametrize(
    ("words", "line"),
    [
        (("gco", "jobs", "list"), "gco jobs list"),
        (("gco", "jobs", "logs", "my job"), "gco jobs logs 'my job'"),
        (("echo", "it's"), "echo 'it'\"'\"'s'"),
        (("gco", ""), "gco ''"),
        ((), ""),
    ],
    ids=["plain", "space", "quote", "empty-word", "no-words"],
)
def test_shell_words_quotes_only_where_needed(words: tuple[str, ...], line: str) -> None:
    assert ui.shell_words(*words) == line


# ---------------------------------------------------------------------------
# The entrypoint
# ---------------------------------------------------------------------------

PAGES = [
    ("Fleet", ":material/public:", "", "overview.py"),
    ("Jobs", ":material/work:", "jobs", "jobs.py"),
    ("Queue", ":material/queue:", "queue", "queue.py"),
    ("Inference", ":material/smart_toy:", "inference", "inference.py"),
    ("Capacity", ":material/memory:", "capacity", "capacity.py"),
    ("Costs", ":material/payments:", "costs", "costs.py"),
    ("Platform", ":material/dns:", "platform", "platform.py"),
    ("Console", ":material/terminal:", "console", "console.py"),
]


class TestApp:
    def test_sets_up_a_wide_page_with_help_links(
        self, fake_fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configured = spy(monkeypatch, "set_page_config")
        open_app()
        [(args, config)] = configured
        assert args == ()
        assert config["page_title"] == "GCO dashboard"
        assert config["page_icon"] == brand.ICON_SVG
        assert config["layout"] == "wide"
        assert config["initial_sidebar_state"] == "expanded"
        assert set(config["menu_items"]) == {"Get help", "Report a bug", "About"}
        assert config["menu_items"]["Get help"].endswith("/docs/DASHBOARD.md")
        assert config["menu_items"]["Report a bug"].endswith("/issues")

    def test_navigation_offers_the_eight_pages_with_the_fleet_first(
        self, fake_fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        navigation = spy(monkeypatch, "navigation")
        app = open_app()
        [((pages,), options)] = navigation
        assert options == {"position": "top"}
        assert [(page.title, page.icon, page.url_path) for page in pages] == [
            (title, icon, url_path) for title, icon, url_path, _ in PAGES
        ]
        pages_dir = Path(APP).parent / "app_pages"
        assert all((pages_dir / script).is_file() for *_, script in PAGES)
        # The default page is the Fleet, and the sidebar sits beside it.
        assert values(app.title) == ["Fleet"]
        assert values(app.sidebar.markdown) == ["### gco"]
        assert app.sidebar.caption[-1].value == SIDEBAR_FOOTER

    def test_toasts_queued_by_the_last_run_show_on_the_next_page(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = AppTest.from_file(str(APP), default_timeout=60)
        app.session_state[ui.FLASH_KEY] = [("Endpoint deleted", ":material/delete:")]
        app.run()
        assert [(toast.value, toast.proto.icon) for toast in app.toast] == [
            ("Endpoint deleted", ":material/delete:")
        ]
        app.run()
        assert not app.toast
