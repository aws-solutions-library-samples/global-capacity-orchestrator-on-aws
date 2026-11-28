"""The Queue page (``cli.dashboard.views.queue``) rendered through the dashboard's entrypoint.

Each test starts the real ``app.py`` under Streamlit's AppTest, switches to the
Queue page and checks the board an operator sees: the record counts, the
central queue's heatmap, each Region's SQS depth, the records table and its
status filter, and a record's detail with its journey, its manifest and the
cancel action. AppTest cannot click a dataframe row, so ``ui.selected_row`` is
patched to select one. The ``fake_fleet`` fixture answers every read from
canned data and records every write.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from cli.dashboard import ui
from cli.dashboard.data import DashboardError
from cli.dashboard.views import queue
from tests._dashboard_fakes import (  # noqa: F401
    APP,
    FakeFleet,
    fake_fleet_fixture,
    fleet_status_document,
    queue_record,
)

QUEUE_PAGE = "app_pages/queue.py"
SELECT_RECORD = ":material/touch_app: Select a record for its journey through the queue."
NO_RECORDS = "No central-queue records. Queue one with the button above."
NO_STATS = "The central queue holds no records for these Regions."
DLQ = "1 messages are in dead-letter queues: `gco jobs queue-status --all-regions` shows which."


def listing(*records: dict[str, Any], cursor: str | None = None) -> dict[str, Any]:
    """A ``GET /api/v1/queue/jobs`` answer holding ``records``."""
    return {"jobs": list(records), "next_cursor": cursor}


def open_queue(state: dict[str, Any] | None = None) -> AppTest:
    """The Queue page after one run, with ``state`` put in the session first."""
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.run()
    app.switch_page(QUEUE_PAGE)
    for key, value in (state or {}).items():
        app.session_state[key] = value
    return app.run()


def values(elements: Iterable[Any]) -> list[Any]:
    return [element.value for element in elements]


def metrics(app: AppTest) -> dict[str, str]:
    return {metric.label: metric.value for metric in app.metric}


def keys(elements: Iterable[Any]) -> list[str | None]:
    return [element.key for element in elements]


def records_table(app: AppTest) -> Any:
    return app.dataframe[0].value


def chart_count(app: AppTest) -> int:
    return len(app.get("vega_lite_chart"))


@pytest.fixture
def first_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Select the first row of the records table (AppTest cannot click one)."""
    monkeypatch.setattr(ui, "selected_row", lambda event, rows: 0 if rows else None)


def spy_confirm(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Record every confirmation the page asks for; the real dialog still opens."""
    real = ui.confirm
    opened: list[tuple[str, dict[str, Any]]] = []

    def confirm(message: str, **options: Any) -> None:
        opened.append((message, options))
        real(message, **options)

    monkeypatch.setattr(ui, "confirm", confirm)
    return opened


def _confirm_script(message: str, options: dict[str, Any]) -> None:
    from cli.dashboard import ui

    ui.show_flash()
    ui.confirm(message, **options)


def press_confirm(opened: tuple[str, dict[str, Any]]) -> AppTest:
    """Reopen a recorded confirmation on every run, as a browser keeps it open, and confirm."""
    message, options = opened
    app = AppTest.from_function(
        _confirm_script, kwargs={"message": message, "options": options}, default_timeout=30
    )
    app.run()
    app.button(key="confirm_go").click()
    return app.run()


class TestQueueBoard:
    def test_counts_charts_and_lists_every_record(self, fake_fleet: FakeFleet) -> None:
        app = open_queue()

        assert values(app.title) == ["Queue"]
        assert values(app.caption)[0].startswith(
            "The central job queue in DynamoDB, and each Region's SQS job queue · read "
        )
        assert metrics(app) == {
            "Records": "3",
            ":material/hourglass_top: Waiting": "1",
            ":material/play_circle: In progress": "0",
            ":material/check_circle: Succeeded": "1",
            ":material/error: Failed": "1",
            ":material/block: Cancelled": "0",
        }
        assert chart_count(app) == 2  # The central queue's heatmap and the SQS depths.
        assert values(app.warning) == [DLQ]
        table = records_table(app)
        assert set(table["name"]) == {"queued-sweep", "queued-dedup", "gated-train"}
        assert set(table["spot_gate"]) == {"", "g5.xlarge <= $0.5/h"}
        # The status column is a coloured-label column, which takes lists.
        assert sorted(list(status) for status in table["status"]) == [
            ["failed"],
            ["queued"],
            ["succeeded"],
        ]
        # The pills offer only statuses present, in the queue's own order.
        assert app.pills(key=queue.STATUS_KEY).options == ["queued", "succeeded", "failed"]
        assert SELECT_RECORD in values(app.caption)
        assert not any(c.startswith("Showing the newest") for c in values(app.caption))
        assert not app.subheader and not app.error and not app.exception

    def test_in_progress_counts_every_stage_after_a_claim(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["queue_stats"] = {
            "by_region": {
                "us-east-1": {"queued": 5, "claimed": 1, "applying": 2},
                "us-west-2": {"pending": 3, "running": 4, "cancelled": 6},
                "eu-west-1": "not-a-count-table",
            }
        }

        app = open_queue()

        shown = metrics(app)
        assert shown["Records"] == "21"
        assert shown[":material/hourglass_top: Waiting"] == "5"
        assert shown[":material/play_circle: In progress"] == "10"
        assert shown[":material/block: Cancelled"] == "6"

    def test_the_region_filter_narrows_every_panel(self, fake_fleet: FakeFleet) -> None:
        app = open_queue({ui.REGION_FILTER_KEY: ["us-east-1"]})

        assert metrics(app)["Records"] == "1"
        assert list(records_table(app)["name"]) == ["queued-sweep"]
        assert not app.warning  # The only dead letter is in us-west-2.
        assert chart_count(app) == 2

    def test_status_pills_filter_the_records(self, fake_fleet: FakeFleet) -> None:
        app = open_queue()
        app.pills(key=queue.STATUS_KEY).select("failed").run()

        assert list(records_table(app)["name"]) == ["gated-train"]
        # The counts above are the whole queue's.
        assert metrics(app)["Records"] == "3"

    def test_a_paged_listing_says_there_is_more(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["queue_jobs"] = listing(
            queue_record("q-1", "queued-sweep", "us-east-1", "queued"), cursor="page-2"
        )

        app = open_queue()

        assert "Showing the newest 1 records; `gco queue list` pages through the rest." in values(
            app.caption
        )

    def test_sqs_queues_without_dead_letters_raise_no_warning(self, fake_fleet: FakeFleet) -> None:
        status = fleet_status_document()
        for counts in status["sections"]["queue"]["data"]["by_region"].values():
            counts["dlq"] = 0
        fake_fleet.overrides["fleet_status"] = status

        app = open_queue()

        assert not app.warning
        assert chart_count(app) == 2

    def test_an_empty_queue(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["queue_stats"] = {"by_region": {}}
        fake_fleet.overrides["queue_jobs"] = listing()

        app = open_queue()

        assert set(metrics(app).values()) == {"0"}
        assert NO_STATS in values(app.caption)
        assert values(app.info) == [NO_RECORDS]
        assert not app.dataframe
        assert chart_count(app) == 1  # Only the SQS depths.

    def test_records_outside_the_selected_regions_do_not_count(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["queue_jobs"] = listing(
            queue_record("q-1", "queued-sweep", "us-east-1", "queued")
        )

        app = open_queue({ui.REGION_FILTER_KEY: ["eu-west-1"]})

        assert values(app.info) == [NO_RECORDS]

    def test_queue_a_job_opens_the_dialog_on_the_central_queue(self, fake_fleet: FakeFleet) -> None:
        app = open_queue()
        app.button(key="queue_submit").click().run()

        assert app.segmented_control(key="submit_placement").value == "queue"
        assert [expander.label for expander in app.expander] == ["Spot price gate"]
        assert not app.exception


class TestQueueReads:
    def test_unreadable_stats_leave_the_counts_at_zero(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["queue_stats"] = DashboardError("queue stats are unreadable")

        app = open_queue()

        assert values(app.error) == ["**queue stats are unreadable**"]
        assert set(metrics(app).values()) == {"0"}
        assert NO_STATS in values(app.caption)
        # The records are a separate read.
        assert len(records_table(app)) == 3

    def test_an_unreadable_status_document_hides_the_sqs_panel(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["fleet_status"] = DashboardError("the status document is unreadable")

        app = open_queue()

        assert values(app.error) == ["**the status document is unreadable**"]
        assert "No SQS readings (the status document could not read them)." in values(app.caption)
        assert chart_count(app) == 1  # Only the central queue's heatmap.
        assert not app.warning

    def test_an_unreadable_listing_shows_the_error_and_no_table(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["queue_jobs"] = DashboardError(
            "The API answered HTTP 503: unavailable", hint=None
        )

        app = open_queue()

        assert values(app.error) == ["**The API answered HTTP 503: unavailable**"]
        assert not app.dataframe and not app.info
        assert metrics(app)["Records"] == "3"


class TestQueueRecordDetail:
    def test_a_queued_record_shows_its_facts_journey_and_manifest(
        self, fake_fleet: FakeFleet, first_row: None
    ) -> None:
        fake_fleet.overrides["queue_jobs"] = listing(
            queue_record("q-1", "queued-sweep", "us-east-1", "queued")
        )

        app = open_queue()

        assert fake_fleet.called("queue_job") == [(("q-1",), {})]
        assert values(app.subheader) == ["queued-sweep"]
        markdown = values(app.markdown)
        assert (
            ":violet-badge[queued] :gray-badge[us-east-1 · N. Virginia] :gray-badge[priority 50]"
            in markdown
        )
        assert (
            "**Job ID** `q-1`  \n**Namespace** `gco-jobs`  \n"
            "**Kubernetes Job** not created yet  \n**Claimed by** no Region yet"
        ) in markdown
        assert [tab.label for tab in app.tabs] == ["Journey", "Manifest"]
        history = app.dataframe[1].value
        assert list(history["status"]) == ["queued"]
        assert list(history["message"]) == ["Job submitted"]
        assert "until" not in history.columns
        assert chart_count(app) == 3  # Heatmap, SQS depths and the record's journey.
        code = values(app.code)
        assert "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: queued-sweep" in code
        assert "gco queue get q-1" in code
        assert "queue_cancel" in keys(app.button)
        assert SELECT_RECORD not in values(app.caption)
        assert not app.error

    @pytest.mark.parametrize(
        ("status", "claimed_by", "claimer"),
        [
            # The API clears claimed_by when a record finishes; the history keeps it.
            ("succeeded", None, "eu-west-1"),
            ("running", "eu-west-1/worker-7/lease-3", "eu-west-1"),
        ],
    )
    def test_a_claimed_record_names_the_region_that_ran_it(
        self,
        fake_fleet: FakeFleet,
        first_row: None,
        status: str,
        claimed_by: str | None,
        claimer: str,
    ) -> None:
        fake_fleet.overrides["queue_jobs"] = listing(
            queue_record("q-2", "queued-dedup", "eu-west-1", status, claimed_by=claimed_by)
        )

        app = open_queue()

        facts = next(m for m in values(app.markdown) if m.startswith("**Job ID**"))
        assert f"**Claimed by** the {claimer} queue worker" in facts
        assert "**Kubernetes Job** `queued-dedup-0123`" in facts
        assert list(app.dataframe[1].value["status"]) == ["queued", "claimed", status]
        # Only a record no Region has claimed can be cancelled.
        assert "queue_cancel" not in keys(app.button)

    def test_a_failed_gated_record_shows_its_error_and_its_gate(
        self, fake_fleet: FakeFleet, first_row: None
    ) -> None:
        fake_fleet.overrides["queue_jobs"] = listing(
            queue_record(
                "q-3",
                "gated-train",
                "us-west-2",
                "failed",
                error_message="image pull failed",
                spot_max_price=0.5,
                spot_instance_type="g5.xlarge",
            )
        )

        app = open_queue()

        assert values(app.error) == ["image pull failed"]
        facts = next(m for m in values(app.markdown) if m.startswith("**Job ID**"))
        assert facts.endswith("**Spot gate** g5.xlarge at or under $0.5/h")

    def test_a_record_without_history_or_manifest_says_so(
        self, fake_fleet: FakeFleet, first_row: None
    ) -> None:
        fake_fleet.overrides["queue_jobs"] = listing(
            queue_record("q-4", "bare", "us-east-1", "queued", status_history=[], manifest=None)
        )

        app = open_queue()

        captions = values(app.caption)
        assert "No status history recorded." in captions
        assert "The record carries no manifest." in captions
        assert chart_count(app) == 2
        assert len(app.dataframe) == 1  # Only the records table.

    def test_an_unreadable_record_shows_the_error_and_no_panel(
        self, fake_fleet: FakeFleet, first_row: None
    ) -> None:
        fake_fleet.fail["queue_job"] = DashboardError("record q-1 is unreadable")

        app = open_queue({ui.REGION_FILTER_KEY: ["us-east-1"]})

        assert values(app.error) == ["**record q-1 is unreadable**"]
        assert not app.subheader and not app.tabs
        assert list(records_table(app)["name"]) == ["queued-sweep"]

    def test_a_record_that_has_gone_shows_no_panel(
        self, fake_fleet: FakeFleet, first_row: None
    ) -> None:
        fake_fleet.overrides["queue_job"] = {}

        app = open_queue({ui.REGION_FILTER_KEY: ["us-east-1"]})

        assert not app.subheader and not app.tabs and not app.error

    def test_cancel_asks_first_then_cancels_that_record(
        self, fake_fleet: FakeFleet, first_row: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_fleet.overrides["queue_jobs"] = listing(
            queue_record("q-1", "queued-sweep", "us-east-1", "queued")
        )
        opened = spy_confirm(monkeypatch)

        app = open_queue()
        app.button(key="queue_cancel").click().run()

        assert (
            "Cancel queued job **queued-sweep**? No Region has claimed it yet, "
            "so nothing is running."
        ) in values(app.markdown)
        assert [b.label for b in app.button if b.key in ("confirm_cancel", "confirm_go")] == [
            "Cancel",
            "Cancel job",
        ]
        assert not fake_fleet.called("queue_cancel")

        dialog = press_confirm(opened[0])

        assert fake_fleet.called("queue_cancel") == [
            (("q-1",), {"reason": "cancelled from the dashboard"})
        ]
        assert values(dialog.toast) == ["Cancelled queued-sweep"]
