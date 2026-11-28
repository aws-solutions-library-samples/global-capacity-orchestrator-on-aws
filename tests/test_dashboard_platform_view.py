"""The Platform page (``cli.dashboard.views.platform``) rendered through the dashboard's entrypoint.

Each test starts the real ``app.py`` under Streamlit's AppTest, switches to the
Platform page and opens one of its lazy tabs by putting the tab's label in
``platform_tab`` first: the stacks the fleet status document lists and each
Region's add-on records (failures first), each Region's services and
health-monitor thresholds, the job policy each Region
enforces (agreement, drift, or why there was nothing to compare), the job
templates and webhooks with their writes, and the request path drawn as
Graphviz DOT. The in-memory
``FakeFleet`` from ``tests/_dashboard_fakes.py`` answers every read and
records every call; no AWS, no network.

Writes are driven through their buttons and forms, and the checks are the call
the page made, the toast it queued and the error card a refusal draws. A
delete opens ``ui.confirm``: each confirmation is recorded as the page opens
it (the real dialog still draws) and confirmed from a script that calls the
dialog on every run, since a click inside a dialog registers only when the
dialog runs again, as it does in a browser that keeps it open.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from cli.dashboard import brand, ui
from cli.dashboard.data import DashboardError, RegionRead
from tests._dashboard_fakes import (
    APP,
    REGIONS,
    FakeFleet,
    fake_fleet_fixture,  # noqa: F401
    fleet_status_document,
    topology,
)

PLATFORM_PAGE = "app_pages/platform.py"
TAB_KEY = "platform_tab"
TEMPLATE = "gpu-training"
HOOK_URL = "https://hooks.example.com/gco"
REFUSAL_CARD = "**The API refused the change.**\n\nCheck the request and retry."
REGION_LABELS = ["us-east-1 · N. Virginia", "us-west-2 · Oregon", "eu-west-1 · Ireland"]
ONE_JOB = """\
apiVersion: batch/v1
kind: Job
metadata:
  name: '{{name}}'
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: main
          image: '{{image}}'
"""
ONE_JOB_MANIFEST = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {"name": "{{name}}"},
    "spec": {
        "template": {
            "spec": {
                "restartPolicy": "Never",
                "containers": [{"name": "main", "image": "{{image}}"}],
            }
        }
    },
}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _run(app: AppTest) -> AppTest:
    app.run()
    assert not app.exception, [e.message for e in app.exception]
    return app


def _page(tab: str | None = None, *, regions: list[str] | None = None) -> AppTest:
    """The Platform page drawn by the dashboard's entrypoint, with ``tab`` open.

    ``regions`` sets the sidebar's Region filter first.
    """
    app = AppTest.from_file(str(APP), default_timeout=60)
    _run(app)
    # The first run draws the Fleet page; forget its reads (``ui.fleet`` is the
    # fixture's FakeFleet), so a test sees only what the Platform page asked for.
    ui.fleet().calls.clear()
    app.switch_page(PLATFORM_PAGE)
    if tab is not None:
        app.session_state[TAB_KEY] = tab
    if regions is not None:
        app.session_state[ui.REGION_FILTER_KEY] = regions
    return _run(app)


def _click(app: AppTest, key: str) -> AppTest:
    app.button(key=key).click()
    return _run(app)


def _submit(app: AppTest, label: str) -> AppTest:
    """Press the submit button labelled ``label`` of a form on the page."""
    next(button for button in app.form_submit_button if button.label == label).click()
    return _run(app)


def _labelled(widgets: Iterable[Any], label: str) -> Any:
    return next(widget for widget in widgets if widget.label == label)


def _values(elements: Iterable[Any]) -> list[Any]:
    return [element.value for element in elements]


def _keys(widgets: Iterable[Any]) -> set[str | None]:
    return {widget.key for widget in widgets}


def _metrics(app: AppTest) -> dict[str, str]:
    return {metric.label: metric.value for metric in app.metric}


def _rows(frame: Any, *columns: str) -> list[tuple[Any, ...]]:
    """``columns`` of a drawn table, row by row, list cells as plain lists."""
    return [
        tuple(value.tolist() if hasattr(value, "tolist") else value for value in row)
        for row in frame[list(columns)].itertuples(index=False)
    ]


def _refusal() -> DashboardError:
    return DashboardError("The API refused the change.", hint="Check the request and retry.")


@dataclass
class Confirmation:
    """A confirmation the page opened: the dialog and what it was called with."""

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


# ---------------------------------------------------------------------------
# Lazy tabs
# ---------------------------------------------------------------------------

#: The reads only one tab makes. Stacks and Topology make none of their own
#: (they reuse the status document, metrics, Jobs and endpoints).
TAB_READS: dict[str, set[str]] = {
    "Stacks": set(),
    "Services": {"global_health", "regional_status"},
    "Policy": {"regional_policy"},
    "Templates": {"templates"},
    "Webhooks": {"webhooks"},
    "Topology": set(),
}


class TestTabs:
    @pytest.mark.parametrize("tab", list(TAB_READS))
    def test_only_the_open_tab_reads_what_it_shows(self, fake_fleet: FakeFleet, tab: str) -> None:
        _page(tab)

        made = {name for name, _, _ in fake_fleet.calls}
        assert made & set().union(*TAB_READS.values()) == TAB_READS[tab]


# ---------------------------------------------------------------------------
# Stacks
# ---------------------------------------------------------------------------


class TestStacks:
    def test_counts_the_expected_stacks_and_lists_every_stack(self, fake_fleet: FakeFleet) -> None:
        app = _page()

        assert _values(app.title) == ["Platform"]
        assert [tab.label for tab in app.tabs] == [
            "Stacks",
            "Services",
            "Policy",
            "Templates",
            "Webhooks",
            "Topology",
        ]
        assert _metrics(app) == {
            "Expected stacks deployed": "5 / 5",
            "Healthy": "5 / 5",
            "Optional stacks": "3",
        }
        assert _rows(app.dataframe[0].value, "stack", "region", "status", "health", "role") == [
            ("gco-global", "us-east-2", "CREATE_COMPLETE", ["healthy"], "expected"),
            ("gco-us-east-1", "us-east-1", "UPDATE_COMPLETE", ["healthy"], "expected"),
            ("gco-us-west-2", "us-west-2", "UPDATE_COMPLETE", ["healthy"], "expected"),
            ("gco-eu-west-1", "eu-west-1", "UPDATE_COMPLETE", ["healthy"], "expected"),
            ("gco-us-east-1-persistent", "us-east-1", "CREATE_COMPLETE", ["healthy"], "expected"),
            ("gco-regional-api-us-east-1", "us-east-1", "CREATE_COMPLETE", ["healthy"], "optional"),
            ("gco-regional-api-us-west-2", "us-west-2", "CREATE_COMPLETE", ["healthy"], "optional"),
            ("gco-regional-api-eu-west-1", "eu-west-1", "CREATE_COMPLETE", ["healthy"], "optional"),
            ("gco-analytics", "us-east-2", "not deployed", ["not-deployed"], "optional"),
        ]
        assert _values(app.code) == [
            "gco stacks list\ngco stacks status gco-global -r us-east-2",
            "gco stacks addons status --all-regions",
        ]
        assert not app.error

    def test_undeployed_and_unhealthy_expected_stacks_lower_the_counts(
        self, fake_fleet: FakeFleet
    ) -> None:
        status = fleet_status_document()
        stacks = status["sections"]["stacks"]["data"]
        stacks["expected"][1].update(status="UPDATE_ROLLBACK_COMPLETE", health="unhealthy")
        stacks["expected"][2].update(status=None, health="not-deployed")
        del stacks["expected"][3]["health"]
        stacks["optional"] = []
        fake_fleet.overrides["fleet_status"] = status

        app = _page()

        assert _metrics(app) == {
            "Expected stacks deployed": "4 / 5",
            "Healthy": "2 / 5",
            "Optional stacks": "0",
        }
        assert _rows(app.dataframe[0].value, "stack", "status", "health")[1:4] == [
            ("gco-us-east-1", "UPDATE_ROLLBACK_COMPLETE", ["unhealthy"]),
            ("gco-us-west-2", "not deployed", ["not-deployed"]),
            # A stack the document gives no verdict for draws no label.
            ("gco-eu-west-1", "UPDATE_COMPLETE", []),
        ]

    def test_stacks_that_could_not_be_read_count_as_neither_deployed_nor_absent(
        self, fake_fleet: FakeFleet
    ) -> None:
        # gco status lists a stack it could not read with health "unknown".
        status = fleet_status_document()
        stacks = status["sections"]["stacks"]["data"]
        stacks["expected"][2].update(status=None, health="unknown", updated_time=None)
        stacks["optional"][1].update(status=None, health="unknown", updated_time=None)
        fake_fleet.overrides["fleet_status"] = status

        app = _page()

        assert _metrics(app) == {
            "Expected stacks deployed": "4 / 5",
            "Healthy": "4 / 5",
            "Optional stacks": "2",
        }
        assert (
            ":material/warning: 2 stacks could not be read: gco-us-west-2, "
            "gco-regional-api-us-west-2."
        ) in _values(app.caption)
        rows = _rows(app.dataframe[0].value, "stack", "status", "health")
        assert rows[2] == ("gco-us-west-2", "unknown", ["unknown"])
        assert rows[6] == ("gco-regional-api-us-west-2", "unknown", ["unknown"])

    def test_a_status_document_without_stacks_says_so(self, fake_fleet: FakeFleet) -> None:
        status = fleet_status_document()
        status["sections"]["stacks"]["data"] = {}
        # A document from before the add-ons section draws no add-on block either.
        del status["sections"]["addons"]
        fake_fleet.overrides["fleet_status"] = status

        app = _page()

        assert "The status document lists no stacks." in _values(app.caption)
        assert not app.metric and not app.dataframe and not app.code

    def test_an_unreadable_status_document_draws_the_error_card(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["fleet_status"] = DashboardError(
            "No AWS credentials were found.",
            hint="Sign in (for example `aws sso login`) and refresh the page.",
        )

        app = _page()

        assert _values(app.error) == [
            "**No AWS credentials were found.**\n\n"
            "Sign in (for example `aws sso login`) and refresh the page."
        ]
        assert "The status document lists no stacks." in _values(app.caption)
        assert not app.metric


# ---------------------------------------------------------------------------
# Add-ons, on the Stacks tab
# ---------------------------------------------------------------------------

#: What the kubectl applier records when the base pass cannot apply the
#: admission policy, as it did live in every Region.
BASE_PASS_FAILURE = "04-resource-quotas.yaml:ValidatingAdmissionPolicy/gco-jobs-container-gpu-limit"


def _addon(name: str, status: str, health: str, message: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "health": health,
        "message": message,
        "updated_at": "2026-11-25T15:10:00+00:00",
    }


def _with_addons(by_region: dict[str, Any], **section: Any) -> dict[str, Any]:
    """The canned status document with its ``addons`` section replaced."""
    status = fleet_status_document()
    status["sections"]["addons"] = {"status": "ok", "data": {"by_region": by_region}, **section}
    return status


class TestAddons:
    def test_each_regions_add_ons_are_listed_failures_first(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["fleet_status"] = _with_addons(
            {
                "us-east-1": {
                    "readable": True,
                    "addons": [
                        _addon("base-manifests", "applied", "healthy", "applied=41 expected=41"),
                        _addon("volcano", "unknown", "unknown", "not-json"),
                    ],
                },
                "us-west-2": {
                    "readable": True,
                    "addons": [
                        _addon("base-manifests", "failed", "unhealthy", BASE_PASS_FAILURE),
                        _addon("keda", "installed", "healthy", "keda installed"),
                    ],
                },
                "eu-west-1": {
                    "readable": True,
                    "addons": [
                        _addon(
                            "_teardown",
                            "teardown-active",
                            "in-progress",
                            "execution teardown-abc123",
                        )
                    ],
                },
            }
        )

        app = _page()

        assert "**Add-ons**" in _values(app.markdown)
        table = app.dataframe[1]
        assert _rows(table.value, "region", "addon", "status", "health", "message") == [
            ("us-west-2", "base-manifests", "failed", ["unhealthy"], BASE_PASS_FAILURE),
            (
                "eu-west-1",
                "_teardown",
                "teardown-active",
                ["degraded"],
                "execution teardown-abc123",
            ),
            ("us-east-1", "volcano", "unknown", ["unknown"], "not-json"),
            ("us-east-1", "base-manifests", "applied", ["healthy"], "applied=41 expected=41"),
            ("us-west-2", "keda", "installed", ["healthy"], "keda installed"),
        ]
        # Health is the same coloured column as the stacks table's.
        health = json.loads(table.proto.columns)["health"]
        assert health["type_config"]["options"] == [
            {"value": verdict, "color": color} for verdict, color in brand.HEALTH_COLORS.items()
        ]
        assert _values(app.code)[-1] == "gco stacks addons status --all-regions"
        assert not app.error

    def test_the_canned_fleet_has_converged_everywhere(self, fake_fleet: FakeFleet) -> None:
        app = _page()

        rows = _rows(app.dataframe[1].value, "region", "addon", "health")
        assert rows == [
            (region, addon, ["healthy"])
            for region in REGIONS
            for addon in ("base-manifests", "keda")
        ]
        assert not [c for c in _values(app.caption) if "add-on" in c.lower()]

    def test_unreadable_regions_and_regions_without_records_are_named(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.overrides["fleet_status"] = _with_addons(
            {
                "us-east-1": {"readable": True, "addons": [_addon("keda", "installed", "healthy")]},
                "us-west-2": {"readable": False, "addons": []},
                "eu-west-1": {"readable": True, "addons": []},
            },
            status="partial",
            reason="add-on status could not be read in 1 of 3 regions",
            errors=["us-west-2: ClientError: An error occurred (AccessDeniedException)"],
        )

        app = _page()

        captions = _values(app.caption)
        assert ":material/warning: Add-on status could not be read in 1 of 3 regions." in captions
        assert (
            ":material/warning: us-west-2: ClientError: An error occurred (AccessDeniedException)"
        ) in captions
        assert "No add-on status recorded yet in eu-west-1." in captions
        assert _rows(app.dataframe[1].value, "region", "addon") == [("us-east-1", "keda")]

    def test_before_any_region_records_its_add_ons_there_is_no_table(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.overrides["fleet_status"] = _with_addons(
            {region: {"readable": True, "addons": []} for region in REGIONS}, status="empty"
        )

        app = _page()

        assert "No add-on status recorded yet in us-east-1, us-west-2, eu-west-1." in _values(
            app.caption
        )
        assert len(app.dataframe) == 1
        assert _values(app.code)[-1] == "gco stacks addons status --all-regions"

    def test_a_section_that_could_not_be_gathered_says_why(self, fake_fleet: FakeFleet) -> None:
        status = fleet_status_document()
        status["sections"]["addons"] = {
            "status": "unavailable",
            "data": {},
            "reason": "deployment regions could not be resolved; see the regions section",
            "errors": [],
        }
        fake_fleet.overrides["fleet_status"] = status

        app = _page()

        assert (
            ":material/warning: Deployment regions could not be resolved; see the regions section."
            in _values(app.caption)
        )
        assert len(app.dataframe) == 1


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


def _region_headings(app: AppTest) -> list[str]:
    """The threshold cards' titles, one per Region whose health monitor answered."""
    return [m for m in _values(app.markdown) if m.strip("*") in REGION_LABELS]


def _bars(app: AppTest) -> list[tuple[int, str]]:
    return [(bar.value, bar.text) for bar in app.progress]


class TestServices:
    def test_shows_the_aggregator_each_regions_services_and_thresholds(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = _page("Services")

        markdown = _values(app.markdown)
        assert "Cross-Region aggregator: **healthy**, 3 of 3 Regions healthy." in markdown
        table = app.dataframe[0].value
        assert _rows(
            table,
            "region",
            "health",
            "cluster",
            "version",
            "processor",
            "queue_worker",
            "templates",
            "webhooks",
            "error",
        ) == [
            (region, ["healthy"], f"gco-{region}", "2.0.0", True, "running", 1, 0, "")
            for region in sorted(REGIONS)
        ]
        assert "**Health monitor thresholds**" in markdown
        assert _region_headings(app) == [f"**{label}**" for label in REGION_LABELS]
        assert markdown.count(":green-badge[within thresholds]") == 3
        assert _bars(app) == 3 * [
            (6, "CPU requested: 6% (limit 80%)"),
            (40, "Memory requested: 40% (limit 80%)"),
            (50, "GPU allocated: 50% (no limit)"),
        ]
        assert _values(app.code) == ["gco jobs health --all-regions\ngco status"]
        assert not app.error

    def test_the_region_filter_narrows_the_table_and_the_cards(self, fake_fleet: FakeFleet) -> None:
        app = _page("Services", regions=["us-west-2"])

        assert _rows(app.dataframe[0].value, "region") == [("us-west-2",)]
        assert _region_headings(app) == ["**us-west-2 · Oregon**"]
        assert len(app.progress) == 3
        assert ((("us-west-2",),), {}) in fake_fleet.called("regional_status")
        assert ((("us-west-2",),), {}) in fake_fleet.called("cluster_metrics")

    def test_threshold_cards_flag_violations_and_unreadable_monitors(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.overrides["cluster_metrics"] = [
            RegionRead(
                region="us-east-1",
                data={
                    "resource_utilization": {
                        "cpu_percent": 130.4,
                        "memory_percent": 40,
                        "gpu_percent": None,
                    },
                    "thresholds": {"cpu_threshold": 80, "memory_threshold": 0, "gpu_threshold": -1},
                    "threshold_violations": ["CPU: 130.4% > 80%"],
                },
            ),
            # A monitor whose cluster reads fail: every value null, the read named.
            RegionRead(
                region="us-west-2",
                data={
                    "status": "unhealthy",
                    "message": "Health check error: listing pods failed",
                    "resource_utilization": {
                        "cpu_percent": None,
                        "memory_percent": None,
                        "gpu_percent": None,
                    },
                    "thresholds": {
                        "cpu_threshold": 80,
                        "memory_threshold": 80,
                        "gpu_threshold": 60,
                    },
                    "threshold_violations": [],
                },
            ),
            # A monitor that reports nothing at all.
            RegionRead(region="eu-west-1", data={"resource_utilization": None}),
            RegionRead(region="ap-south-1", error="The health monitor timed out."),
        ]

        app = _page("Services")

        markdown = _values(app.markdown)
        assert _region_headings(app) == [f"**{label}**" for label in REGION_LABELS]
        assert markdown.count(":red-badge[over threshold]") == 1
        # Nothing was measured, so neither is within its thresholds.
        assert markdown.count(":green-badge[within thresholds]") == 0
        assert markdown.count(":orange-badge[no cluster reading]") == 2
        assert _bars(app) == [
            # Over-commit is drawn as a full bar; the label keeps the real number.
            (100, "CPU requested: 130% (limit 80%)"),
            (40, "Memory requested: 40% (limit 0%)"),
            (0, "GPU allocated: — (no limit)"),
        ]
        captions = _values(app.caption)
        assert ":material/warning: CPU: 130.4% > 80%" in captions
        assert ":material/warning: Health check error: listing pods failed" in captions
        assert ":material/warning: The health monitor sent no cluster reading" in captions
        assert "ap-south-1: The health monitor timed out." in captions

    def test_a_region_without_gpu_nodes_says_so_instead_of_an_idle_gpu_bar(
        self, fake_fleet: FakeFleet
    ) -> None:
        def reading(allocatable: int, requested: int) -> dict[str, Any]:
            return {
                "resource_utilization": {
                    "cpu_percent": 10,
                    "memory_percent": 20,
                    "gpu_percent": 0 if not allocatable else 100 * requested / allocatable,
                    "gpu_allocatable": allocatable,
                    "gpu_requested": requested,
                },
                "thresholds": {"cpu_threshold": 80, "memory_threshold": 80, "gpu_threshold": -1},
            }

        fake_fleet.overrides["cluster_metrics"] = [
            RegionRead(region="us-east-1", data=reading(0, 0)),
            # GPUs requested while the device plugin reports none: a real state, drawn.
            RegionRead(region="us-west-2", data=reading(0, 1)),
            RegionRead(region="eu-west-1", data=reading(4, 2)),
        ]

        app = _page("Services")

        assert "GPU allocated: no GPU nodes" in _values(app.caption)
        assert [label for _, label in _bars(app) if label.startswith("GPU")] == [
            "GPU allocated: 0% (no limit)",
            "GPU allocated: 50% (no limit)",
        ]

    def test_an_unreadable_aggregator_leaves_each_regions_own_status(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["global_health"] = DashboardError("The aggregator timed out.")

        app = _page("Services")

        assert _values(app.error) == ["**The aggregator timed out.**"]
        assert not any(m.startswith("Cross-Region aggregator") for m in _values(app.markdown))
        assert _rows(app.dataframe[0].value, "region", "health", "cluster", "processor") == [
            (region, ["unknown"], f"gco-{region}", True) for region in sorted(REGIONS)
        ]
        assert len(app.progress) == 9

    def test_unreadable_regional_status_leaves_the_aggregators_verdicts(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["regional_status"] = DashboardError("The regional APIs are unreachable.")

        app = _page("Services")

        assert _values(app.error) == ["**The regional APIs are unreachable.**"]
        assert _rows(
            app.dataframe[0].value, "region", "health", "cluster", "processor", "version"
        ) == [(region, ["healthy"], f"gco-{region}", False, "") for region in sorted(REGIONS)]

    def test_unreadable_health_monitors_draw_no_threshold_cards(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["cluster_metrics"] = DashboardError("CloudWatch is unreachable.")

        app = _page("Services")

        assert _values(app.error) == ["**CloudWatch is unreachable.**"]
        assert "**Health monitor thresholds**" in _values(app.markdown)
        assert not _region_headings(app) and not app.progress
        assert len(app.dataframe[0].value) == 3


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _drifting_policies() -> list[RegionRead]:
    """us-west-2 deployed with a lower GPU cap and root blocked; eu-west-1 unreadable."""
    east, west, _ = FakeFleet().regional_policy(REGIONS)
    west.data["policy"]["manifest_caps"]["max_gpu_count"] = 8
    west.data["policy"]["manifest_security_policy"]["block_run_as_root"] = True
    return [east, west, RegionRead(region="eu-west-1", error="The API answered HTTP 503")]


def _drifting_status() -> dict[str, Any]:
    """The status document's comparison of :func:`_drifting_policies`."""
    status = fleet_status_document()
    status["sections"]["policy"]["data"] = {
        "compared": ["us-east-1", "us-west-2"],
        "unreadable": {"eu-west-1": "The API answered HTTP 503"},
        "agree": False,
        "drift": [
            {"field": "max_gpu_count", "values": {"us-east-1": 16, "us-west-2": 8}},
            {
                "field": "security",
                "values": {
                    "us-east-1": {"block_privileged": True, "block_run_as_root": False},
                    "us-west-2": {"block_privileged": True, "block_run_as_root": True},
                },
            },
            # Records the page cannot read are skipped or shown as they came.
            "not-a-drift-record",
            {"field": "allowed_kinds", "values": ["Job", "Pod"]},
        ],
        "enforcement_gaps": {"us-west-2": ["gco-jobs", "gco-inference"]},
    }
    return status


def _compared(compared: list[str], *, unreadable: dict[str, str]) -> dict[str, Any]:
    """A status document whose policy section read only ``compared``'s policies.

    ``agree`` stays true, as ``cli/status.py`` reports it when fewer than two
    policies were read: no field can differ.
    """
    status = fleet_status_document()
    status["sections"]["policy"]["data"].update(compared=compared, unreadable=unreadable)
    return status


class TestPolicy:
    def test_agreeing_regions_and_what_each_admits(self, fake_fleet: FakeFleet) -> None:
        app = _page("Policy")

        assert _values(app.success) == [
            "Every Region enforces the same job policy (us-east-1, us-west-2, eu-west-1)."
        ]
        assert not app.warning
        assert ((), {"with_costs": False, "with_policy": True}) in fake_fleet.called("fleet_status")
        table = app.dataframe[0].value
        assert _rows(
            table,
            "region",
            "validation",
            "namespaces",
            "kinds",
            "max_cpu",
            "max_memory",
            "max_gpus",
            "registries",
            "dockerhub_orgs",
            "blocked",
        ) == [
            (
                region,
                True,
                ["gco-jobs"],
                ["Job", "Pod"],
                "384",
                "4096Gi",
                16,
                2,
                ["vllm"],
                ["privileged"],
            )
            for region in REGIONS
        ]
        assert (
            "The CPU, memory and GPU caps are per manifest, and GPUs per container is the most "
            "one container may ask for. `gco jobs submit --check-policy` checks a manifest "
            "against the per-manifest caps first."
        ) in _values(app.caption)
        assert _values(app.code) == ["gco status --with-policy"]
        assert not app.error

    def test_each_regions_per_container_gpu_cap_comes_from_its_admission_policy(
        self, fake_fleet: FakeFleet
    ) -> None:
        """Regression: the cap moved out of the gco-jobs LimitRange.

        A LimitRange GPU entry gave every pod a toleration of the GPU node
        taint, so the gco-jobs-container-gpu-limit admission policy holds the
        cap and GET /api/v1/policy reports it under ``admission_policies``.
        """
        east, west, eu = FakeFleet().regional_policy(REGIONS)
        east.data["cluster_enforcement"] = {
            "gco-jobs": {
                "status": "ok",
                "admission_policies": {
                    "gco-jobs-container-gpu-limit": {"container_max": {"nvidia.com/gpu": "8"}}
                },
            }
        }
        west.data["cluster_enforcement"] = {
            "gco-jobs": {"status": "unavailable", "reason": "403 Forbidden"}
        }
        fake_fleet.overrides["regional_policy"] = [east, west, eu]

        app = _page("Policy")

        table = app.dataframe[0].value
        assert _rows(table, "region", "max_gpus", "container_gpus") == [
            ("us-east-1", 16, "8"),
            ("us-west-2", 16, ""),
            ("eu-west-1", 16, ""),
        ]

    def test_drift_unreadable_regions_and_enforcement_gaps_are_spelled_out(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.overrides["fleet_status"] = _drifting_status()
        fake_fleet.overrides["regional_policy"] = _drifting_policies()

        app = _page("Policy")

        assert _values(app.warning) == ["The Regions enforce different job policies:"]
        assert not app.success
        drift = [m for m in _values(app.markdown) if m.startswith("- ")]
        assert drift == [
            "- `max_gpu_count`: us-east-1: 16; us-west-2: 8",
            "- `security`: us-east-1: {'block_privileged': True, 'block_run_as_root': False}; "
            "us-west-2: {'block_privileged': True, 'block_run_as_root': True}",
            "- `allowed_kinds`: ['Job', 'Pod']",
        ]
        captions = _values(app.caption)
        assert ":material/warning: eu-west-1: The API answered HTTP 503" in captions
        assert (
            ":material/warning: us-west-2: the ResourceQuota, LimitRange and GPU cap of "
            "gco-jobs, gco-inference could not be read, so only the per-manifest caps below "
            "are known to apply."
        ) in captions
        table = app.dataframe[0].value
        assert _rows(table, "region", "blocked", "error") == [
            ("us-east-1", ["privileged"], ""),
            ("us-west-2", ["privileged", "run as root"], ""),
            ("eu-west-1", [], "The API answered HTTP 503"),
        ]
        assert list(table["max_gpus"])[:2] == [16, 8]

    def test_a_single_region_deployment_has_no_other_policy_to_compare(
        self, fake_fleet: FakeFleet
    ) -> None:
        # The status document calls one policy "agreeing": nothing differs.
        fake_fleet.overrides["fleet_status"] = _compared(["us-east-1"], unreadable={})

        app = _page("Policy")

        assert not app.success and not app.warning
        assert _values(app.info) == [
            "us-east-1 is the only workload Region, so there is no other policy to compare with."
        ]

    @pytest.mark.parametrize(
        ("compared", "banner"),
        [
            (
                ["us-east-1"],
                "Only us-east-1's policy could be read, so there is nothing to compare.",
            ),
            ([], "No Region's policy could be read, so there is nothing to compare."),
        ],
        ids=["one-readable", "none-readable"],
    )
    def test_unreadable_policies_leave_nothing_to_compare(
        self, fake_fleet: FakeFleet, compared: list[str], banner: str
    ) -> None:
        unreadable = {
            region: "The API answered HTTP 503" for region in REGIONS if region not in compared
        }
        fake_fleet.overrides["fleet_status"] = _compared(compared, unreadable=unreadable)

        app = _page("Policy")

        assert not app.success and not app.info
        assert _values(app.warning) == [banner]
        captions = _values(app.caption)
        for region in unreadable:
            assert f":material/warning: {region}: The API answered HTTP 503" in captions

    def test_agreement_among_the_readable_regions_does_not_speak_for_the_others(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.overrides["fleet_status"] = _compared(
            ["us-east-1", "us-west-2"], unreadable={"eu-west-1": "The API answered HTTP 503"}
        )

        app = _page("Policy")

        assert not app.success and not app.info
        assert _values(app.warning) == [
            "The Regions whose policy could be read (us-east-1, us-west-2) enforce the same "
            "job policy."
        ]
        assert ":material/warning: eu-west-1: The API answered HTTP 503" in _values(app.caption)

    def test_without_a_comparison_only_each_regions_policy_draws(
        self, fake_fleet: FakeFleet
    ) -> None:
        status = fleet_status_document()
        status["sections"]["policy"] = {"status": "skipped", "reason": "not requested"}
        fake_fleet.overrides["fleet_status"] = status

        app = _page("Policy")

        assert not app.success and not app.warning and not app.error
        assert list(app.dataframe[0].value["region"]) == list(REGIONS)

    def test_an_unreadable_comparison_still_draws_each_regions_policy(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["fleet_status"] = DashboardError("The status document is unreadable.")

        app = _page("Policy")

        assert _values(app.error) == ["**The status document is unreadable.**"]
        assert not app.success and not app.warning
        assert list(app.dataframe[0].value["region"]) == list(REGIONS)

    def test_unreadable_regional_policies_leave_an_empty_table(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["regional_policy"] = DashboardError("The regional APIs are unreachable.")

        app = _page("Policy")

        assert _values(app.error) == ["**The regional APIs are unreachable.**"]
        assert app.success  # The comparison is a read of its own.
        assert app.dataframe[0].value.empty

    def test_the_region_filter_limits_the_policy_reads(self, fake_fleet: FakeFleet) -> None:
        app = _page("Policy", regions=["eu-west-1"])

        assert fake_fleet.called("regional_policy") == [((("eu-west-1",),), {})]
        assert list(app.dataframe[0].value["region"]) == ["eu-west-1"]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _fill_run(
    app: AppTest, *, job: str, region: str = "us-west-2", parameters: str = ""
) -> AppTest:
    """Fill the gpu-training card's run row."""
    app.text_input(key=f"tpl_job_{TEMPLATE}").input(job)
    app.selectbox(key=f"tpl_region_{TEMPLATE}").select(region)
    app.text_input(key=f"tpl_params_{TEMPLATE}").input(parameters)
    return _run(app)


def _create(
    app: AppTest,
    *,
    name: str = "tiny-job",
    description: str = "",
    manifest: str = ONE_JOB,
    defaults: str = "",
) -> AppTest:
    """Fill the new-template form and press Create."""
    _labelled(app.text_input, "Name").input(name)
    _labelled(app.text_input, "Description").input(description)
    _labelled(app.text_area, "Job manifest").input(manifest)
    _labelled(app.text_input, "Default parameters").input(defaults)
    return _submit(app, "Create")


class TestTemplateList:
    def test_each_template_shows_its_manifest_defaults_and_run_row(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.overrides["templates"] = [*FakeFleet().templates(), {"name": "bare"}]

        app = _page("Templates")

        assert [expander.label for expander in app.expander] == [
            "gpu-training · GPU training",
            "bare · no description",
        ]
        assert _values(app.code) == [
            "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: '{{name}}'",
            "{}",
        ]
        defaults = [c for c in _values(app.caption) if c.startswith("Defaults: ")]
        assert defaults == ["Defaults: image=pytorch/pytorch:latest"]
        assert app.selectbox(key="tpl_region_bare").options == REGION_LABELS
        assert {"tpl_run_bare", "tpl_delete_bare", f"tpl_run_{TEMPLATE}"} <= _keys(app.button)
        # Run waits for a job name.
        assert app.button(key=f"tpl_run_{TEMPLATE}").disabled
        assert "**New template**" in _values(app.markdown)
        assert not app.error

    def test_typing_a_job_name_enables_run(self, fake_fleet: FakeFleet) -> None:
        app = _fill_run(_page("Templates"), job="train-1")

        assert not app.button(key=f"tpl_run_{TEMPLATE}").disabled
        assert not fake_fleet.called("run_template")

    def test_no_templates_says_so_and_still_offers_the_form(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["templates"] = []

        app = _page("Templates")

        assert "No templates yet." in _values(app.caption)
        assert not app.expander
        assert [button.label for button in app.form_submit_button] == ["Create"]

    def test_unreadable_templates_draw_the_error_card_and_no_form(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["templates"] = DashboardError("The template store is unreachable.")

        app = _page("Templates")

        assert _values(app.error) == ["**The template store is unreachable.**"]
        assert not app.form and not app.expander


class TestTemplateRun:
    def test_run_creates_the_job_in_the_picked_region_with_the_parameters(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = _fill_run(
            _page("Templates"), job="train-1", parameters="image=pytorch/pytorch:2.9.0, gpus=2"
        )

        app = _click(app, f"tpl_run_{TEMPLATE}")

        assert fake_fleet.called("run_template") == [
            (
                (TEMPLATE,),
                {
                    "job_name": "train-1",
                    "region": "us-west-2",
                    "parameters": {"image": "pytorch/pytorch:2.9.0", "gpus": "2"},
                },
            )
        ]
        assert _values(app.toast) == ["Created train-1 in us-west-2 from gpu-training"]
        assert app.session_state[TAB_KEY] == "Templates"
        assert not app.error

    def test_run_without_parameters_keeps_the_templates_defaults(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = _click(_fill_run(_page("Templates"), job="train-2"), f"tpl_run_{TEMPLATE}")

        assert fake_fleet.called("run_template") == [
            ((TEMPLATE,), {"job_name": "train-2", "region": "us-west-2", "parameters": {}})
        ]
        assert _values(app.toast) == ["Created train-2 in us-west-2 from gpu-training"]

    def test_malformed_parameters_draw_the_error_and_run_nothing(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = _fill_run(_page("Templates"), job="train-1", parameters="image=busybox, gpus")

        app = _click(app, f"tpl_run_{TEMPLATE}")

        assert _values(app.error) == ["**'gpus' is not a parameter of the form key=value.**"]
        assert not fake_fleet.called("run_template")
        assert not app.toast

    def test_a_refused_run_draws_the_error_card(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["run_template"] = _refusal()

        app = _click(_fill_run(_page("Templates"), job="train-1"), f"tpl_run_{TEMPLATE}")

        assert len(fake_fleet.called("run_template")) == 1
        assert _values(app.error) == [REFUSAL_CARD]
        assert not app.toast


class TestTemplateDelete:
    QUESTION = "Delete template **gpu-training**? Jobs created from it are not affected."

    def test_delete_asks_first_then_deletes_the_template(
        self, fake_fleet: FakeFleet, confirmations: list[Confirmation]
    ) -> None:
        app = _click(_page("Templates"), f"tpl_delete_{TEMPLATE}")

        assert [opened.message for opened in confirmations] == [self.QUESTION]
        assert self.QUESTION in _values(app.markdown)
        assert {"confirm_cancel", "confirm_go"} <= _keys(app.button)
        assert not fake_fleet.called("delete_template")

        dialog = _confirm(confirmations[0])

        assert fake_fleet.called("delete_template") == [((TEMPLATE,), {})]
        assert _values(dialog.toast) == ["Deleted template gpu-training"]

    def test_a_refused_delete_draws_the_error_card_in_the_dialog(
        self, fake_fleet: FakeFleet, confirmations: list[Confirmation]
    ) -> None:
        fake_fleet.fail["delete_template"] = _refusal()
        _click(_page("Templates"), f"tpl_delete_{TEMPLATE}")

        dialog = _confirm(confirmations[0])

        assert fake_fleet.called("delete_template") == [((TEMPLATE,), {})]
        assert _values(dialog.error) == [REFUSAL_CARD]
        assert not dialog.toast


class TestTemplateCreate:
    def test_create_stores_the_manifest_with_its_description_and_defaults(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = _create(_page("Templates"), description="A tiny job", defaults="image=busybox:1.38.0")

        assert fake_fleet.called("create_template") == [
            (
                ("tiny-job", ONE_JOB_MANIFEST),
                {"description": "A tiny job", "parameters": {"image": "busybox:1.38.0"}},
            )
        ]
        assert _values(app.toast) == ["Created template tiny-job"]
        assert not app.error

    def test_a_blank_description_and_no_defaults_send_none_and_nothing(
        self, fake_fleet: FakeFleet
    ) -> None:
        _create(_page("Templates"))

        assert fake_fleet.called("create_template") == [
            (("tiny-job", ONE_JOB_MANIFEST), {"description": None, "parameters": {}})
        ]

    def test_yaml_that_does_not_parse_draws_the_parser_error(self, fake_fleet: FakeFleet) -> None:
        app = _create(_page("Templates"), manifest="kind: [Job")

        (card,) = _values(app.error)
        assert card.startswith("**The YAML does not parse: ")
        assert not fake_fleet.called("create_template")

    @pytest.mark.parametrize(
        ("manifest", "defaults", "card"),
        [
            pytest.param("", "", "**The YAML holds no manifests.**", id="no-manifest"),
            pytest.param(
                "- not: a Kubernetes object\n",
                "",
                "**Every YAML document must be a Kubernetes object with a kind.**",
                id="not-an-object",
            ),
            pytest.param(
                f"{ONE_JOB}---\n{ONE_JOB}",
                "",
                "**A template holds exactly one manifest.**",
                id="two-manifests",
            ),
            pytest.param(
                ONE_JOB,
                "image",
                "**'image' is not a parameter of the form key=value.**",
                id="malformed-defaults",
            ),
        ],
    )
    def test_invalid_input_draws_the_error_and_stores_nothing(
        self, fake_fleet: FakeFleet, manifest: str, defaults: str, card: str
    ) -> None:
        app = _create(_page("Templates"), manifest=manifest, defaults=defaults)

        assert _values(app.error) == [card]
        assert not fake_fleet.called("create_template")
        assert not app.toast

    def test_a_refused_template_draws_the_error_card(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["create_template"] = _refusal()

        app = _create(_page("Templates"))

        assert len(fake_fleet.called("create_template")) == 1
        assert _values(app.error) == [REFUSAL_CARD]
        assert not app.toast


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def _register(app: AppTest, *, url: str, **fields: Any) -> AppTest:
    """Fill the register form (``events``, ``namespace``, ``secret``) and press Register."""
    _labelled(app.text_input, "HTTPS URL").input(url)
    if "events" in fields:
        _labelled(app.multiselect, "Events").set_value(fields["events"])
    if "namespace" in fields:
        _labelled(app.text_input, "Only this namespace").input(fields["namespace"])
    if "secret" in fields:
        _labelled(app.text_input, "Signing secret").input(fields["secret"])
    return _submit(app, "Register")


class TestWebhookList:
    def test_each_webhook_shows_where_it_posts_and_what_it_sends(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.overrides["webhooks"] = [
            *FakeFleet().webhooks(),
            {
                "webhook_id": "ef56ab78",
                "url": "https://ops.example.com/hooks",
                "events": ["job.failed", "job.started"],
            },
        ]

        app = _page("Webhooks")

        markdown = _values(app.markdown)
        assert f"`{HOOK_URL}`  \n:blue-badge[job.completed] :gray-badge[gco-jobs]" in markdown
        assert (
            "`https://ops.example.com/hooks`  \n:blue-badge[job.failed] :blue-badge[job.started]"
        ) in markdown
        assert {"hook_delete_ab12cd34", "hook_delete_ef56ab78"} <= _keys(app.button)
        events = _labelled(app.multiselect, "Events")
        assert events.options == ["job.completed", "job.failed", "job.started"]
        assert events.value == ["job.completed", "job.failed"]
        assert not app.error

    def test_no_webhooks_says_so_and_still_offers_the_form(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["webhooks"] = []

        app = _page("Webhooks")

        assert "No webhooks registered." in _values(app.caption)
        assert [button.label for button in app.form_submit_button] == ["Register"]

    def test_unreadable_webhooks_draw_the_error_card_and_no_form(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["webhooks"] = DashboardError("The webhook store is unreachable.")

        app = _page("Webhooks")

        assert _values(app.error) == ["**The webhook store is unreachable.**"]
        assert not app.form and "**Webhooks**" not in _values(app.markdown)


class TestWebhookRegister:
    def test_register_sends_the_url_and_the_default_events(self, fake_fleet: FakeFleet) -> None:
        app = _register(_page("Webhooks"), url="https://hooks.example.com/new")

        assert fake_fleet.called("create_webhook") == [
            (
                ("https://hooks.example.com/new", ["job.completed", "job.failed"]),
                {"namespace": None, "secret": None},
            )
        ]
        assert _values(app.toast) == ["Webhook registered"]
        assert not app.error

    def test_register_with_a_namespace_a_secret_and_other_events(
        self, fake_fleet: FakeFleet
    ) -> None:
        _register(
            _page("Webhooks"),
            url="https://hooks.example.com/new",
            events=["job.started"],
            namespace="gco-jobs",
            secret="example-signing-key",
        )

        assert fake_fleet.called("create_webhook") == [
            (
                ("https://hooks.example.com/new", ["job.started"]),
                {"namespace": "gco-jobs", "secret": "example-signing-key"},
            )
        ]

    def test_a_refused_registration_draws_the_error_card(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["create_webhook"] = DashboardError(
            "Pick at least one event to subscribe to."
        )

        app = _register(_page("Webhooks"), url="https://hooks.example.com/new", events=[])

        assert fake_fleet.called("create_webhook") == [
            (("https://hooks.example.com/new", []), {"namespace": None, "secret": None})
        ]
        assert _values(app.error) == ["**Pick at least one event to subscribe to.**"]
        assert not app.toast


class TestWebhookDelete:
    @pytest.mark.parametrize(
        ("hook", "hook_id"),
        [
            pytest.param(
                {"id": "ab12cd34", "url": HOOK_URL, "events": ["job.completed"]},
                "ab12cd34",
                id="id",
            ),
            # Older API answers name the identifier webhook_id.
            pytest.param({"webhook_id": "ef56ab78", "url": HOOK_URL}, "ef56ab78", id="webhook-id"),
        ],
    )
    def test_delete_asks_first_then_deletes_that_webhook(
        self,
        fake_fleet: FakeFleet,
        confirmations: list[Confirmation],
        hook: dict[str, Any],
        hook_id: str,
    ) -> None:
        fake_fleet.overrides["webhooks"] = [hook]
        question = f"Stop sending job events to `{HOOK_URL}`?"

        app = _click(_page("Webhooks"), f"hook_delete_{hook_id}")

        assert [opened.message for opened in confirmations] == [question]
        assert question in _values(app.markdown)
        assert not fake_fleet.called("delete_webhook")

        dialog = _confirm(confirmations[0])

        assert fake_fleet.called("delete_webhook") == [((hook_id,), {})]
        assert _values(dialog.toast) == ["Webhook deleted"]

    def test_a_refused_delete_draws_the_error_card_in_the_dialog(
        self, fake_fleet: FakeFleet, confirmations: list[Confirmation]
    ) -> None:
        fake_fleet.fail["delete_webhook"] = _refusal()
        _click(_page("Webhooks"), "hook_delete_ab12cd34")

        dialog = _confirm(confirmations[0])

        assert fake_fleet.called("delete_webhook") == [(("ab12cd34",), {})]
        assert _values(dialog.error) == [REFUSAL_CARD]
        assert not dialog.toast


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------

#: A Region's node in the DOT: its id, its label and its health colour.
_REGION_NODE = re.compile(r'^  (r_\w+) \[label="([^"]*)", color="([^"]*)"', re.MULTILINE)


def _dot(app: AppTest) -> str:
    (chart,) = app.get("graphviz_chart")
    return chart.proto.spec


def _region_nodes(app: AppTest) -> list[tuple[str, str, str]]:
    return _REGION_NODE.findall(_dot(app))


class TestTopology:
    def test_draws_the_request_path_to_every_region(self, fake_fleet: FakeFleet) -> None:
        app = _page("Topology")

        healthy = brand.HEALTH_COLORS["healthy"]
        assert _region_nodes(app) == [
            ("r_us_east_1", "gco-us-east-1\\nN. Virginia\\n1 running · 1 endpoint", healthy),
            ("r_us_west_2", "gco-us-west-2\\nOregon\\n1 running · 1 endpoint", healthy),
            ("r_eu_west_1", "gco-eu-west-1\\nIreland\\n0 running · 0 endpoints", healthy),
        ]
        dot = _dot(app)
        assert 'api [label="Global API Gateway\\nus-east-2 · IAM (SigV4)"' in dot
        assert 'registry [label="gco-global · us-east-2\\njob queue, endpoint registry"' in dot
        # AppTest reports no browser theme, which reads as dark.
        assert 'fontcolor="#E8ECF3"' in dot
        assert (
            "Every request is signed with your AWS credentials (SigV4) and checked by IAM at "
            "the API Gateway. Regions are coloured by health."
        ) in _values(app.caption)
        assert not app.error

    def test_separate_api_and_control_plane_regions_in_a_light_theme(
        self, fake_fleet: FakeFleet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_fleet.overrides["topology"] = topology(
            global_region="us-east-2", api_gateway_region="us-west-2"
        )
        monkeypatch.setattr(ui, "is_dark", lambda: False)

        app = _page("Topology")

        dot = _dot(app)
        assert 'api [label="Global API Gateway\\nus-west-2 · IAM (SigV4)"' in dot
        assert 'registry [label="gco-global · us-east-2\\n' in dot
        assert 'fontcolor="#171B26"' in dot

    def test_the_region_filter_draws_only_the_picked_regions(self, fake_fleet: FakeFleet) -> None:
        app = _page("Topology", regions=["eu-west-1"])

        assert [node for node, _, _ in _region_nodes(app)] == ["r_eu_west_1"]

    def test_unreadable_reads_draw_their_error_cards_and_a_bare_map(
        self, fake_fleet: FakeFleet
    ) -> None:
        for read in ("fleet_status", "cluster_metrics", "jobs", "endpoints"):
            fake_fleet.fail[read] = DashboardError(f"The {read} read failed.")

        app = _page("Topology")

        assert _values(app.error) == [
            "**The fleet_status read failed.**",
            "**The cluster_metrics read failed.**",
            "**The jobs read failed.**",
            "**The endpoints read failed.**",
        ]
        unknown = brand.HEALTH_COLORS["unknown"]
        assert _region_nodes(app) == [
            ("r_us_east_1", "gco-us-east-1\\nN. Virginia\\n0 running · 0 endpoints", unknown),
            ("r_us_west_2", "gco-us-west-2\\nOregon\\n0 running · 0 endpoints", unknown),
            ("r_eu_west_1", "gco-eu-west-1\\nIreland\\n0 running · 0 endpoints", unknown),
        ]
