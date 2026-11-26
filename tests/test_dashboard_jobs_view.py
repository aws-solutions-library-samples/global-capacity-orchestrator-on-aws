"""The Jobs page (``cli.dashboard.views.jobs``) rendered through the dashboard's entrypoint.

Each test starts the real ``app.py`` under Streamlit's AppTest, switches to the
Jobs page and uses it as an operator would: the filter bar, the status chart,
row selection, the lazy detail tabs, the log viewer and the writes (retry,
delete, bulk cleanup). AppTest cannot click a dataframe row or a chart
segment, so those two are patched (``ui.selected_row`` and the chart's
selection). The ``fake_fleet`` fixture answers every read from canned data and
records every write, so each test checks what the page shows and what it asked
the fleet to do.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import Any

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from cli.dashboard import charts, ui
from cli.dashboard.data import DashboardError, JobsSnapshot
from cli.dashboard.views import jobs
from cli.jobs import JobInfo
from tests._dashboard_fakes import APP, FakeFleet, fake_fleet_fixture, job_info  # noqa: F401

JOBS_PAGE = "app_pages/jobs.py"
WHERE = ("data-validate", "gco-jobs", "us-east-1")
FAILED = job_info("data-validate", "us-east-1", "failed")
#: An older, finished Job listed below the one a test selects.
FINISHED = job_info("feature-store-sync", "us-west-2", "succeeded", minutes_ago=240)
FAILED_CONDITION = "**Failed**: BackoffLimitExceeded. Job has reached the specified backoff limit"
DETAIL_READS = ("job_detail", "job_pods", "job_events", "job_logs", "job_metrics")
SELECT_ROW = ":material/touch_app: Select a row for its pods, events, logs and usage."
NO_JOBS = "No Kubernetes Jobs in the selected Regions yet. Submit one with the button above."


def snapshot(
    *found: JobInfo, errors: dict[str, str] | None = None, total: int | None = None
) -> JobsSnapshot:
    """A jobs read holding ``found``, as the cross-Region aggregator returns it."""
    return JobsSnapshot(
        list(found), dict(errors or {}), 3, len(found) if total is None else total, "global"
    )


def cleanup_answers(
    *,
    matched: int,
    deleted: int,
    delete_errors: list[Any] | None = None,
    region_results: list[Any] | None = None,
) -> Callable[..., dict[str, Any]]:
    """A ``cleanup_jobs`` whose preview matches ``matched`` Jobs and whose delete removes
    ``deleted``, failing in the Regions ``delete_errors`` names; the delete's per-Region
    counts are ``region_results``."""

    def cleanup(**query: Any) -> dict[str, Any]:
        if query["dry_run"]:
            return {"dry_run": True, "total_matched": matched, "total_deleted": 0, "errors": None}
        return {
            "dry_run": False,
            "total_matched": deleted,
            "total_deleted": deleted,
            "region_results": region_results,
            "errors": delete_errors,
        }

    return cleanup


def open_jobs(state: dict[str, Any] | None = None) -> AppTest:
    """The Jobs page after one run, with ``state`` put in the session first."""
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.run()
    app.switch_page(JOBS_PAGE)
    for key, value in (state or {}).items():
        app.session_state[key] = value
    return app.run()


def values(elements: Iterable[Any]) -> list[Any]:
    return [element.value for element in elements]


def metrics(app: AppTest) -> dict[str, str]:
    return {metric.label: metric.value for metric in app.metric}


def keys(elements: Iterable[Any]) -> list[str | None]:
    return [element.key for element in elements]


def jobs_table(app: AppTest) -> Any:
    """The jobs table as the page hands it to ``st.dataframe``."""
    return app.dataframe[0].value


def chart_count(app: AppTest) -> int:
    return len(app.get("vega_lite_chart"))


@pytest.fixture
def first_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Select the first row of the jobs table (AppTest cannot click one)."""
    monkeypatch.setattr(ui, "selected_row", lambda event, rows: 0 if rows else None)


@pytest.fixture
def failed_job(fake_fleet: FakeFleet, first_row: None) -> FakeFleet:
    """A fleet whose only Job, ``data-validate`` in us-east-1, failed and is selected."""
    fake_fleet.overrides["jobs"] = snapshot(FAILED)
    return fake_fleet


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


def spy_log_fragment(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The ``run_every`` of each log-viewer fragment, recorded as the page defines it."""
    real = st.fragment
    intervals: list[Any] = []

    def fragment(func: Callable[..., Any] | None = None, **options: Any) -> Any:
        def define(body: Callable[..., Any]) -> Any:
            if body.__qualname__ == "_logs.<locals>.view":
                intervals.append(options.get("run_every"))
            return real(body, **options)

        return define if func is None else define(func)

    monkeypatch.setattr(st, "fragment", fragment)
    return intervals


class TestJobsTable:
    def test_lists_every_job_newest_first_with_status_counts(self, fake_fleet: FakeFleet) -> None:
        app = open_jobs()

        assert values(app.title) == ["Jobs"]
        assert values(app.caption)[0].startswith("Kubernetes Jobs in 3 Regions · read ")
        assert metrics(app) == {
            "Jobs": "7",
            ":material/play_circle: Running": "2",
            ":material/hourglass_top: Pending": "1",
            ":material/check_circle: Succeeded": "3",
            ":material/error: Failed": "1",
        }
        assert app.metric[0].help == "7 Jobs in the selected Regions before filtering"
        table = jobs_table(app)
        assert list(table["name"][:3]) == ["speech-asr-train", "llm-finetune-a", "vision-pretrain"]
        assert set(table["name"][3:]) == {
            "eval-suite-1",
            "data-validate",
            "feature-store-sync",
            "queued-sweep-lr-1e4-abc",
        }
        # The status column is a coloured-label column, which takes lists.
        assert list(table["status"][0]) == ["pending"]
        assert chart_count(app) == 2
        assert SELECT_ROW in values(app.caption)
        assert not app.subheader
        assert not any(c.startswith("Showing the newest") for c in values(app.caption))
        assert not app.exception and not app.error

    def test_search_matches_labels_as_well_as_names(self, fake_fleet: FakeFleet) -> None:
        app = open_jobs()
        app.text_input(key=jobs.SEARCH_KEY).input("central-queue").run()

        assert list(jobs_table(app)["name"]) == ["queued-sweep-lr-1e4-abc"]
        assert metrics(app)["Jobs"] == "1"
        assert app.metric[0].help == "7 Jobs in the selected Regions before filtering"

    def test_status_pills_keep_only_those_statuses(self, fake_fleet: FakeFleet) -> None:
        app = open_jobs()

        pills = app.pills(key=jobs.STATUS_KEY)
        assert pills.options == ["pending", "running", "succeeded", "failed"]
        pills.select("failed").select("pending").run()

        assert set(jobs_table(app)["name"]) == {"data-validate", "speech-asr-train"}
        assert metrics(app)["Jobs"] == "2"

    def test_a_namespace_filter_appears_once_jobs_span_namespaces(
        self, fake_fleet: FakeFleet
    ) -> None:
        team = dataclasses.replace(
            job_info("team-train", "us-west-2", "running"), namespace="team-a"
        )
        fake_fleet.overrides["jobs"] = snapshot(FAILED, team)

        app = open_jobs()
        picker = app.multiselect(key=jobs.NAMESPACE_KEY)
        assert picker.options == ["gco-jobs", "team-a"]
        picker.select("team-a").run()

        assert list(jobs_table(app)["name"]) == ["team-train"]

    def test_one_namespace_needs_no_namespace_filter(self, fake_fleet: FakeFleet) -> None:
        app = open_jobs()

        assert jobs.NAMESPACE_KEY not in keys(app.multiselect)

    @pytest.mark.parametrize(
        ("source", "names"),
        [
            ("central queue", {"queued-sweep-lr-1e4-abc"}),
            (
                "direct",
                {
                    "speech-asr-train",
                    "llm-finetune-a",
                    "vision-pretrain",
                    "eval-suite-1",
                    "data-validate",
                    "feature-store-sync",
                },
            ),
        ],
    )
    def test_the_source_filter_splits_queued_from_direct_jobs(
        self, fake_fleet: FakeFleet, source: str, names: set[str]
    ) -> None:
        app = open_jobs()

        control = app.segmented_control(key=jobs.SOURCE_KEY)
        assert control.options == ["All", "Submitted directly", "From the queue"]
        control.set_value(source).run()

        assert set(jobs_table(app)["name"]) == names

    def test_the_sidebar_region_filter_narrows_the_page(self, fake_fleet: FakeFleet) -> None:
        app = open_jobs({ui.REGION_FILTER_KEY: ["us-west-2"]})

        assert values(app.caption)[0].startswith("Kubernetes Jobs in 1 Region · read ")
        assert set(jobs_table(app)["name"]) == {"vision-pretrain", "feature-store-sync"}
        assert metrics(app)["Jobs"] == "2"

    def test_clicking_a_status_segment_shows_only_those_jobs(
        self, fake_fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = st.altair_chart

        def altair_chart(chart: Any, **options: Any) -> Any:
            event = real(chart, **options)
            if options.get("key") != jobs.CHART_KEY:
                return event
            picked = [{"region": "us-east-1", "status": "succeeded"}]
            return SimpleNamespace(selection={charts.STATUS_PICK: picked})

        monkeypatch.setattr(st, "altair_chart", altair_chart)

        app = open_jobs()

        assert set(jobs_table(app)["name"]) == {"eval-suite-1", "queued-sweep-lr-1e4-abc"}

    def test_the_timeline_draws_jobs_that_have_not_finished(self, fake_fleet: FakeFleet) -> None:
        # No Job has a finish time, so every bar runs to now.
        fake_fleet.overrides["jobs"] = snapshot(
            job_info("llm-finetune-a", "us-east-1", "running"),
            job_info("speech-asr-train", "eu-west-1", "pending"),
        )

        app = open_jobs()

        assert chart_count(app) == 2  # The status chart and the timeline.
        assert "None of these Jobs has started yet." not in values(app.caption)
        assert not app.exception

    def test_the_timeline_waits_for_a_job_to_start(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["jobs"] = snapshot(
            job_info("speech-asr-train", "eu-west-1", "pending")
        )

        app = open_jobs()

        assert "None of these Jobs has started yet." in values(app.caption)
        assert chart_count(app) == 1  # The status chart; no timeline.

    def test_a_truncated_listing_says_how_many_are_shown(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["jobs"] = snapshot(FAILED, total=250)

        app = open_jobs()

        assert "Showing the newest 1 of 250 Jobs." in values(app.caption)

    def test_submit_job_opens_the_dialog_on_a_region(self, fake_fleet: FakeFleet) -> None:
        app = open_jobs()
        app.button(key="jobs_submit").click().run()

        assert app.segmented_control(key="submit_placement").value == "region"
        assert app.selectbox(key="submit_example").value.name == "simple-job"
        assert not app.exception


class TestJobsReads:
    def test_a_failed_read_shows_the_error_card_and_no_table(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["jobs"] = DashboardError(
            "The jobs API timed out", hint="The API Gateway front door timed out; try again."
        )

        app = open_jobs()

        assert values(app.error) == [
            "**The jobs API timed out**\n\nThe API Gateway front door timed out; try again."
        ]
        assert not app.dataframe and not app.metric
        # The header actions do not depend on the read.
        assert "jobs_submit" in keys(app.button) and "cleanup_preview" in keys(app.button)

    def test_a_region_that_could_not_be_read_is_named(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["jobs"] = snapshot(
            FAILED, errors={"eu-west-1": "Regional request failed"}
        )

        app = open_jobs()

        assert values(app.warning) == [
            "Jobs in eu-west-1 could not be read: Regional request failed"
        ]
        assert list(jobs_table(app)["name"]) == ["data-validate"]

    def test_no_jobs_yet(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["jobs"] = snapshot()

        app = open_jobs()

        assert values(app.info) == [NO_JOBS]
        assert not app.dataframe and not app.metric

    def test_jobs_outside_the_selected_regions_do_not_count(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["jobs"] = snapshot(FAILED)

        app = open_jobs({ui.REGION_FILTER_KEY: ["eu-west-1"]})

        assert values(app.info) == [NO_JOBS]


class TestCleanup:
    def test_the_command_follows_the_form(self, fake_fleet: FakeFleet) -> None:
        app = open_jobs()

        assert "**Delete finished Jobs in every Region**" in values(app.markdown)
        assert "gco jobs bulk-delete --all-regions -s succeeded --execute -y" in values(app.code)

        app.segmented_control(key="cleanup_status").set_value("failed")
        app.number_input(key="cleanup_days").set_value(7)
        app.text_input(key="cleanup_namespace").input("team-a").run()

        assert "gco jobs bulk-delete --all-regions -s failed -d 7 -n team-a --execute -y" in values(
            app.code
        )
        assert not fake_fleet.called("cleanup_jobs")

    def test_preview_first_then_delete(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["cleanup_jobs"] = cleanup_answers(matched=2, deleted=2)

        app = open_jobs()
        app.button(key="cleanup_preview").click().run()

        query = {"status": "succeeded", "older_than_days": None, "namespace": None}
        assert fake_fleet.called("cleanup_jobs") == [((), {**query, "dry_run": True})]
        assert "2 Jobs match." in values(app.markdown)
        delete = app.button(key="cleanup_go")
        assert delete.label == "Delete 2 Jobs"
        reads = len(fake_fleet.called("jobs"))

        delete.click().run()

        assert fake_fleet.called("cleanup_jobs")[-1] == ((), {**query, "dry_run": False})
        assert values(app.toast) == ["Deleted 2 finished Jobs"]
        # The delete's report takes the preview's place, with nothing left to delete.
        assert "Deleted 2 Jobs." in values(app.markdown)
        assert "2 Jobs match." not in values(app.markdown)
        assert "cleanup_go" not in keys(app.button)
        assert not app.warning
        # The table is read again, without the deleted Jobs.
        assert len(fake_fleet.called("jobs")) == reads + 1

        # A new preview replaces the report.
        app.button(key="cleanup_preview").click().run()

        assert "2 Jobs match." in values(app.markdown)
        assert "Deleted 2 Jobs." not in values(app.markdown)
        assert app.button(key="cleanup_go").label == "Delete 2 Jobs"

    def test_the_delete_reports_what_it_deleted_and_where_it_failed(
        self, fake_fleet: FakeFleet
    ) -> None:
        # Between the preview and the delete a Region stopped answering, and
        # an error that is not an object is shown as its text.
        failed = [{"region": "eu-west-1", "error": "Regional request failed"}, "aggregator timeout"]
        fake_fleet.overrides["cleanup_jobs"] = cleanup_answers(
            matched=3, deleted=2, delete_errors=failed
        )

        app = open_jobs()
        app.button(key="cleanup_preview").click().run()

        assert app.button(key="cleanup_go").label == "Delete 3 Jobs"

        app.button(key="cleanup_go").click().run()

        assert values(app.toast) == ["Deleted 2 finished Jobs"]
        assert "Deleted 2 Jobs." in values(app.markdown)
        assert values(app.warning) == ["eu-west-1: Regional request failed", "aggregator timeout"]
        assert "cleanup_go" not in keys(app.button)

        # The report belongs to the form that asked for it.
        app.segmented_control(key="cleanup_status").set_value("failed").run()

        assert "Deleted 2 Jobs." not in values(app.markdown)
        assert not app.warning

    def test_the_delete_reports_the_jobs_each_region_could_not_delete(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.overrides["cleanup_jobs"] = cleanup_answers(
            matched=4,
            deleted=1,
            region_results=[
                {"region": "us-east-1", "matched": 3, "deleted": 1, "failed": 2},
                {"region": "us-west-2", "matched": 1, "deleted": 0, "failed": 1},
                # A Region that deleted everything, and records the page cannot read.
                {"region": "eu-west-1", "matched": 0, "deleted": 0, "failed": 0},
                {"region": "ap-south-1", "failed": None},
                "not-a-result",
            ],
        )

        app = open_jobs()
        app.button(key="cleanup_preview").click().run()
        app.button(key="cleanup_go").click().run()

        assert values(app.toast) == ["Deleted 1 finished Job; 3 could not be deleted"]
        assert app.toast[0].icon == ":material/warning:"
        assert "Deleted 1 Job." in values(app.markdown)
        assert values(app.warning) == [
            "us-east-1: 2 Jobs could not be deleted.",
            "us-west-2: 1 Job could not be deleted.",
        ]
        assert "cleanup_go" not in keys(app.button)

    def test_the_preview_asks_with_the_forms_filters(self, fake_fleet: FakeFleet) -> None:
        app = open_jobs()
        app.segmented_control(key="cleanup_status").set_value("failed")
        app.number_input(key="cleanup_days").set_value(30)
        app.text_input(key="cleanup_namespace").input("team-a")
        app.button(key="cleanup_preview").click().run()

        assert fake_fleet.called("cleanup_jobs") == [
            (
                (),
                {"status": "failed", "older_than_days": 30, "namespace": "team-a", "dry_run": True},
            )
        ]

    def test_changing_the_form_drops_the_preview(self, fake_fleet: FakeFleet) -> None:
        app = open_jobs()
        app.button(key="cleanup_preview").click().run()
        app.segmented_control(key="cleanup_status").set_value("failed").run()

        assert "2 Jobs match." not in values(app.markdown)
        assert "cleanup_go" not in keys(app.button)

    def test_regions_the_preview_could_not_read_are_named(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["cleanup_jobs"] = {
            "dry_run": True,
            "total_matched": 0,
            "errors": [{"region": "eu-west-1", "error": "Regional request failed"}],
        }

        app = open_jobs()
        app.button(key="cleanup_preview").click().run()

        assert "0 Jobs match." in values(app.markdown)
        assert values(app.warning) == ["eu-west-1: Regional request failed"]
        assert "cleanup_go" not in keys(app.button)

    def test_a_failed_preview_shows_the_error(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["cleanup_jobs"] = DashboardError("bulk delete is not allowed")

        app = open_jobs()
        app.button(key="cleanup_preview").click().run()

        assert values(app.error) == ["**bulk delete is not allowed**"]
        assert not [m for m in values(app.markdown) if m.endswith("match.")]
        assert "cleanup_go" not in keys(app.button)

    def test_a_failed_delete_keeps_the_preview(self, fake_fleet: FakeFleet) -> None:
        def cleanup(**query: Any) -> dict[str, Any]:
            if not query["dry_run"]:
                raise DashboardError("delete refused")
            return {"dry_run": True, "total_matched": 1, "errors": None}

        fake_fleet.overrides["cleanup_jobs"] = cleanup

        app = open_jobs()
        app.button(key="cleanup_preview").click().run()
        app.button(key="cleanup_go").click().run()

        assert values(app.error) == ["**delete refused**"]
        assert "1 Job match." in values(app.markdown)
        assert not app.toast


class TestJobDetailOverview:
    def test_selecting_a_row_opens_its_overview(self, failed_job: FakeFleet) -> None:
        app = open_jobs()

        assert values(app.subheader) == ["data-validate"]
        assert (
            ":red-badge[failed] :gray-badge[us-east-1 · N. Virginia] :gray-badge[gco-jobs]"
            in values(app.markdown)
        )
        assert [tab.label for tab in app.tabs] == [
            "Overview",
            "Pods",
            "Events",
            "Logs",
            "Metrics",
            "CLI",
        ]
        shown = metrics(app)
        assert (shown["Active pods"], shown["Succeeded"], shown["Failed"], shown["Runtime"]) == (
            "0",
            "0",
            "1",
            "10 min 0 s",
        )
        # Failed is shown; the controller's FailureTarget bookkeeping is not.
        assert values(app.error) == [FAILED_CONDITION]
        markdown = values(app.markdown)
        assert (
            ":material/dns: Ran on **m5a.large** (on-demand) in `us-east-1c`, "
            "NodePool `general-purpose`"
        ) in markdown
        assert "**Images** `busybox`" in markdown
        assert "**Labels** :gray-badge[team=ml]" in markdown
        assert failed_job.called("job_detail") == [(WHERE, {})]
        assert SELECT_ROW not in values(app.caption)

    def test_a_running_job_with_unplaced_pods(self, fake_fleet: FakeFleet, first_row: None) -> None:
        running = job_info("llm-finetune-a", "us-east-1", "running", minutes_ago=30)
        fake_fleet.overrides["jobs"] = snapshot(running, FINISHED)
        fake_fleet.overrides["job_detail"] = {
            "metadata": {"name": "llm-finetune-a", "namespace": "gco-jobs"},
            "spec": {"template": {"spec": {"containers": [{"name": "main"}, "not-a-container"]}}},
            "status": {
                "active": 2,
                "conditions": [
                    "not-a-condition",
                    {"type": "Complete", "status": "False"},
                    {
                        "type": "Suspended",
                        "status": "True",
                        "reason": "JobSuspended",
                        "message": "Job suspended",
                    },
                ],
            },
            "scheduling": {
                "node_instance_type": "g6e.xlarge",
                "unscheduled": [
                    {
                        "pod": "llm-finetune-a-x2",
                        "reason": "Unschedulable",
                        "message": "0/3 nodes are available",
                    },
                    {"name": "llm-finetune-a-x3"},
                ],
                "node_lookup_error": "nodes is forbidden",
            },
        }

        app = open_jobs()

        shown = metrics(app)
        assert (shown["Active pods"], shown["Runtime"]) == ("2", "—")
        captions = values(app.caption)
        assert "**Suspended**: JobSuspended. Job suspended" in captions
        assert ":material/info: Node details unavailable: nodes is forbidden" in captions
        assert not app.error
        markdown = values(app.markdown)
        assert (
            ":material/dns: Running on **g6e.xlarge** (unknown capacity) in `?`, NodePool `?`"
            in markdown
        )
        assert not [m for m in markdown if m.startswith(("**Images**", "**Labels**"))]
        assert [warning.rstrip() for warning in values(app.warning)] == [
            "Pod `llm-finetune-a-x2` is not scheduled: Unschedulable 0/3 nodes are available",
            "Pod `llm-finetune-a-x3` is not scheduled: Pending",
        ]

    def test_a_bare_detail_record_shows_only_the_counts(
        self, fake_fleet: FakeFleet, first_row: None
    ) -> None:
        fake_fleet.overrides["jobs"] = snapshot(
            job_info("speech-asr-train", "eu-west-1", "pending")
        )
        fake_fleet.overrides["job_detail"] = {"metadata": {"name": "speech-asr-train"}}

        app = open_jobs()

        shown = metrics(app)
        assert (shown["Active pods"], shown["Succeeded"], shown["Failed"], shown["Runtime"]) == (
            "0",
            "0",
            "0",
            "—",
        )
        assert not app.warning and not app.error
        assert not [m for m in values(app.markdown) if ":material/dns:" in m]

    def test_a_failed_detail_read_shows_the_error(self, failed_job: FakeFleet) -> None:
        failed_job.fail["job_detail"] = DashboardError("job not found")

        app = open_jobs()

        assert values(app.error) == ["**job not found**"]
        assert "Active pods" not in metrics(app)
        # The panel's title and actions do not depend on the read.
        assert values(app.subheader) == ["data-validate"]
        assert "jobs_delete" in keys(app.button)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0 s"), (59.9, "59 s"), (61, "1 min 1 s"), (3600, "1 h 0 min"), (7325, "2 h 2 min")],
)
def test_durations_read_in_the_largest_units(seconds: float, text: str) -> None:
    assert jobs._duration(seconds) == text


class TestJobDetailTabs:
    @pytest.mark.parametrize(
        ("tab", "reads"),
        [
            ("Overview", {"job_detail"}),
            ("Pods", {"job_pods"}),
            ("Events", {"job_events"}),
            # The log viewer needs the pods' container names.
            ("Logs", {"job_pods", "job_logs"}),
            ("Metrics", {"job_metrics"}),
            ("CLI", set()),
        ],
    )
    def test_each_tab_reads_only_what_it_shows(
        self, failed_job: FakeFleet, tab: str, reads: set[str]
    ) -> None:
        app = open_jobs({jobs.TAB_KEY: tab})

        assert {name for name, _, _ in failed_job.calls if name in DETAIL_READS} == reads
        for name in reads:
            assert failed_job.called(name)[0][0] == WHERE
        assert not app.exception

    def test_pods_tab_lists_each_pod(self, failed_job: FakeFleet) -> None:
        app = open_jobs({jobs.TAB_KEY: "Pods"})

        pods = app.dataframe[1].value
        assert list(pods["pod"]) == ["data-validate-x1"]
        assert pods.iloc[0]["phase"] == "Failed"
        assert pods.iloc[0]["restarts"] == 1
        assert list(pods.iloc[0]["containers"]) == ["main", "sidecar"]

    def test_a_job_without_pods_says_so(self, failed_job: FakeFleet) -> None:
        failed_job.overrides["job_pods"] = {"pods": []}

        app = open_jobs({jobs.TAB_KEY: "Pods"})

        assert values(app.info) == [
            "No pods: the Job has not started one yet, or they were cleaned up."
        ]
        assert len(app.dataframe) == 1  # Only the jobs table.

    def test_a_failed_pods_read_shows_the_error(self, failed_job: FakeFleet) -> None:
        failed_job.fail["job_pods"] = DashboardError("pods are unreadable")

        app = open_jobs({jobs.TAB_KEY: "Pods"})

        assert values(app.error) == ["**pods are unreadable**"]
        assert len(app.dataframe) == 1

    def test_events_tab_counts_warnings_and_charts_them(self, failed_job: FakeFleet) -> None:
        app = open_jobs({jobs.TAB_KEY: "Events"})

        assert "2 events about the Job, its pods, nodes and NodeClaims · :red[1 warning]" in values(
            app.caption
        )
        events = app.dataframe[1].value
        assert list(events["reason"]) == ["BackoffLimitExceeded", "Scheduled"]
        assert list(events["object"]) == ["Job/data-validate", "Pod/data-validate-x1"]
        assert chart_count(app) == 3  # Status chart, job timeline, events timeline.

    def test_events_without_warnings_are_counted_plainly(self, failed_job: FakeFleet) -> None:
        failed_job.overrides["job_events"] = {
            "events": [
                {
                    "type": "Normal",
                    "reason": "Completed",
                    "message": "Job completed",
                    "lastTimestamp": "2026-11-25T17:50:00+00:00",
                    "involvedObject": {"kind": "Job", "name": "data-validate"},
                }
            ]
        }

        app = open_jobs({jobs.TAB_KEY: "Events"})

        assert "1 event about the Job, its pods, nodes and NodeClaims" in values(app.caption)

    def test_a_job_without_events_says_so(self, failed_job: FakeFleet) -> None:
        failed_job.overrides["job_events"] = {"events": []}

        app = open_jobs({jobs.TAB_KEY: "Events"})

        assert values(app.info) == ["No events recorded for this Job, its pods or their nodes."]

    def test_a_failed_events_read_shows_the_error(self, failed_job: FakeFleet) -> None:
        failed_job.fail["job_events"] = DashboardError("events are unreadable")

        app = open_jobs({jobs.TAB_KEY: "Events"})

        assert values(app.error) == ["**events are unreadable**"]
        assert chart_count(app) == 2

    def test_metrics_tab_totals_the_usage(self, failed_job: FakeFleet) -> None:
        app = open_jobs({jobs.TAB_KEY: "Metrics"})

        shown = metrics(app)
        assert (shown["CPU"], shown["Memory"], shown["Pods sampled"]) == ("5 m", "12.5 MiB", "1")
        assert chart_count(app) == 3

    def test_samples_without_a_summary_read_as_zero(self, failed_job: FakeFleet) -> None:
        failed_job.overrides["job_metrics"] = {
            "pods": [{"pod_name": "data-validate-x1", "containers": [{"name": "main"}]}]
        }

        app = open_jobs({jobs.TAB_KEY: "Metrics"})

        shown = metrics(app)
        assert (shown["CPU"], shown["Memory"], shown["Pods sampled"]) == ("0 m", "0.0 MiB", "0")

    def test_a_job_without_samples_says_so(self, failed_job: FakeFleet) -> None:
        failed_job.overrides["job_metrics"] = {"summary": {}, "pods": []}

        app = open_jobs({jobs.TAB_KEY: "Metrics"})

        assert values(app.info) == [
            "No usage samples: metrics-server reports only running containers."
        ]

    def test_a_failed_metrics_read_shows_the_error(self, failed_job: FakeFleet) -> None:
        failed_job.fail["job_metrics"] = DashboardError("metrics-server is not installed")

        app = open_jobs({jobs.TAB_KEY: "Metrics"})

        assert values(app.error) == ["**metrics-server is not installed**"]
        assert "CPU" not in metrics(app)

    def test_cli_tab_lists_the_equivalent_commands(self, failed_job: FakeFleet) -> None:
        app = open_jobs({jobs.TAB_KEY: "CLI"})

        assert (
            "gco jobs get data-validate -n gco-jobs -r us-east-1\n"
            "gco jobs logs data-validate -n gco-jobs -r us-east-1 --tail 200\n"
            "gco jobs events data-validate -n gco-jobs -r us-east-1\n"
            "gco jobs delete data-validate -n gco-jobs -r us-east-1"
        ) in values(app.code)


class TestLogViewer:
    def test_reads_the_first_container_and_offers_a_download(self, failed_job: FakeFleet) -> None:
        app = open_jobs({jobs.TAB_KEY: "Logs"})

        picker = app.selectbox(key="jobs_log_container")
        assert (picker.options, picker.value) == (["main", "sidecar"], "main")
        assert app.select_slider(key="jobs_log_tail").value == 200
        assert app.toggle(key="jobs_log_follow").value is False
        assert "step 1\nstep 2" in values(app.code)
        assert keys(app.download_button) == ["jobs_log_download"]
        assert failed_job.called("job_logs") == [(WHERE, {"tail": 200, "container": "main"})]

    def test_a_container_and_a_tail_change_what_is_read(self, failed_job: FakeFleet) -> None:
        app = open_jobs({jobs.TAB_KEY: "Logs"})
        app.selectbox(key="jobs_log_container").select("sidecar")
        app.select_slider(key="jobs_log_tail").set_value(1000).run()

        assert failed_job.called("job_logs")[-1] == (WHERE, {"tail": 1000, "container": "sidecar"})

    def test_one_container_needs_no_picker(self, failed_job: FakeFleet) -> None:
        failed_job.overrides["job_pods"] = {
            "pods": [
                {
                    "metadata": {"name": "data-validate-x1"},
                    "status": {"phase": "Failed", "containerStatuses": [{"name": "main"}]},
                }
            ]
        }

        app = open_jobs({jobs.TAB_KEY: "Logs"})

        assert "jobs_log_container" not in keys(app.selectbox)
        assert failed_job.called("job_logs") == [(WHERE, {"tail": 200, "container": None})]

    def test_logs_are_read_even_when_the_pods_are_not(self, failed_job: FakeFleet) -> None:
        failed_job.fail["job_pods"] = DashboardError("pods are unreadable")

        app = open_jobs({jobs.TAB_KEY: "Logs"})

        assert values(app.error) == ["**pods are unreadable**"]
        assert failed_job.called("job_logs") == [(WHERE, {"tail": 200, "container": None})]
        assert "step 1\nstep 2" in values(app.code)

    def test_empty_logs_say_so(self, failed_job: FakeFleet) -> None:
        failed_job.overrides["job_logs"] = ""

        app = open_jobs({jobs.TAB_KEY: "Logs"})

        assert "(no output yet)" in values(app.code)
        assert keys(app.download_button) == ["jobs_log_download"]

    def test_a_failed_log_read_shows_the_error_and_no_download(self, failed_job: FakeFleet) -> None:
        failed_job.fail["job_logs"] = DashboardError("logs are unavailable")

        app = open_jobs({jobs.TAB_KEY: "Logs"})

        assert values(app.error) == ["**logs are unavailable**"]
        assert not app.download_button

    def test_follow_rereads_the_logs_every_five_seconds(
        self, failed_job: FakeFleet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        intervals = spy_log_fragment(monkeypatch)

        app = open_jobs({jobs.TAB_KEY: "Logs"})
        assert intervals == [None]

        app.toggle(key="jobs_log_follow").set_value(True).run()
        assert intervals == [None, 5]

        app.toggle(key="jobs_log_follow").set_value(False).run()
        assert intervals == [None, 5, None]


class TestJobActions:
    def test_retry_resubmits_a_failed_job(self, failed_job: FakeFleet) -> None:
        app = open_jobs()
        app.button(key="jobs_retry").click().run()

        assert failed_job.called("retry_job") == [(WHERE, {})]
        assert values(app.toast) == ["Retried data-validate", "The retry is data-validate-retry-1"]
        assert values(app.error) == [FAILED_CONDITION]  # The overview's, not the retry's.

    def test_a_retry_without_a_new_name_is_still_reported(self, failed_job: FakeFleet) -> None:
        failed_job.overrides["retry_job"] = {}

        app = open_jobs()
        app.button(key="jobs_retry").click().run()

        assert values(app.toast) == ["Retried data-validate", "The retry is submitted"]

    def test_a_failed_retry_shows_the_error(self, failed_job: FakeFleet) -> None:
        failed_job.fail["retry_job"] = DashboardError("the Job's spec is gone")

        app = open_jobs()
        app.button(key="jobs_retry").click().run()

        assert values(app.error) == ["**the Job's spec is gone**", FAILED_CONDITION]
        assert not app.toast

    @pytest.mark.parametrize("status", ["pending", "running", "succeeded"])
    def test_only_a_failed_job_offers_retry(
        self, fake_fleet: FakeFleet, first_row: None, status: str
    ) -> None:
        newest = job_info("eval-suite-1", "us-east-1", status, minutes_ago=30)
        fake_fleet.overrides["jobs"] = snapshot(newest, FINISHED)

        app = open_jobs()

        assert values(app.subheader) == ["eval-suite-1"]

        assert "jobs_retry" not in keys(app.button)
        assert "jobs_delete" in keys(app.button)

    def test_delete_asks_first_then_deletes_that_exact_job(
        self, failed_job: FakeFleet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened = spy_confirm(monkeypatch)

        app = open_jobs()
        app.button(key="jobs_delete").click().run()

        message = (
            "Delete Job **data-validate** in **us-east-1** (gco-jobs)? Its pods are deleted too."
        )
        assert message in values(app.markdown)
        assert [b.label for b in app.button if b.key in ("confirm_cancel", "confirm_go")] == [
            "Cancel",
            "Delete",
        ]
        assert not failed_job.called("delete_job")

        dialog = press_confirm(opened[0])

        # The uid pins the delete to this Job, not a newer one of the same name.
        assert failed_job.called("delete_job") == [(WHERE, {"uid": "uid-data-validate"})]
        assert values(dialog.toast) == ["Deleted data-validate"]

    def test_a_job_without_a_uid_is_deleted_by_name(
        self, fake_fleet: FakeFleet, first_row: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_fleet.overrides["jobs"] = snapshot(dataclasses.replace(FAILED, uid=None))
        opened = spy_confirm(monkeypatch)

        app = open_jobs()
        app.button(key="jobs_delete").click().run()
        press_confirm(opened[0])

        assert fake_fleet.called("delete_job") == [(WHERE, {"uid": None})]
