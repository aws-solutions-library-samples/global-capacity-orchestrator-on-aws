"""The job submission dialog the Jobs and Queue pages share (``cli.dashboard.submit``).

Each test renders ``submit_dialog`` through Streamlit's AppTest from a script
that opens the dialog on every run. A browser reruns an open dialog on its
own; AppTest reruns the whole script, so a click inside the dialog registers
only if the script opens it again. The ``fake_fleet`` fixture answers every
read and records every write. The tests fill the form as an operator would and
check what the dialog shows (the manifest summary, the equivalent ``gco``
command, error cards, toasts) and what it sent to the fleet.
"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from cli.dashboard import submit
from cli.dashboard.data import DashboardError, ExampleManifest, load_manifest_text
from tests._dashboard_fakes import FakeFleet, fake_fleet_fixture, simple_manifest  # noqa: F401

SIMPLE = "gco jobs submit examples/simple-job.yaml"
TWO_JOBS = """\
apiVersion: batch/v1
kind: Job
metadata:
  name: first
---
apiVersion: batch/v1
kind: Job
metadata:
  name: second
"""
NO_EXAMPLES = "No example Job manifests were found under examples/ in this checkout."
RECOMMEND_HINT = "The recommendation reads each Region's queue depth, GPU use and running jobs."


def _dialog_script(placement: str) -> None:
    from cli.dashboard import submit, ui

    ui.show_flash()
    submit.submit_dialog(placement)


def open_dialog(placement: str = "region") -> AppTest:
    """The dialog as a page opens it, after its first run."""
    app = AppTest.from_function(_dialog_script, kwargs={"placement": placement}, default_timeout=30)
    return app.run()


def paste(app: AppTest, text: str) -> AppTest:
    """Switch the manifest source to pasted YAML and paste ``text``."""
    app.segmented_control(key="submit_source").set_value("paste").run()
    app.text_area(key="submit_yaml_paste").input(text)
    return app.run()


def values(elements: Iterable[Any]) -> list[Any]:
    return [element.value for element in elements]


def command(app: AppTest) -> str:
    """The ``gco`` command the dialog says does the same thing."""
    (code,) = values(app.code)
    return str(code)


def enabled(app: AppTest, key: str) -> bool:
    return not app.button(key=key).disabled


def button_keys(app: AppTest) -> list[str | None]:
    return [button.key for button in app.button]


def charts(app: AppTest) -> int:
    return len(app.get("vega_lite_chart"))


class TestManifestSources:
    def test_opens_on_the_simple_job_example(self, fake_fleet: FakeFleet) -> None:
        app = open_dialog()

        assert app.segmented_control(key="submit_source").value == "example"
        assert app.selectbox(key="submit_example").value.name == "simple-job"
        assert values(app.caption) == [
            "A minimal job.",
            "1 object: `Job/hello-gco`",
            "The same, from a terminal:",
        ]
        assert command(app) == f"{SIMPLE} -r us-east-1"
        assert enabled(app, "submit_validate") and enabled(app, "submit_go")
        placeholder = app.text_input(key="submit_namespace").placeholder
        assert placeholder == fake_fleet.config.default_namespace
        assert not app.error and not app.exception

    def test_a_gpu_example_names_its_prerequisites_and_flags_the_gpu(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = open_dialog()
        app.selectbox(key="submit_example").select_index(1).run()

        assert app.selectbox(key="submit_example").options == [
            "Simple Kubernetes Job Example",
            "GPU Job Example  ·  GPU",
        ]
        captions = values(app.caption)
        assert captions[:2] == [
            "Runs nvidia-smi.",
            ":material/checklist: Needs: GPU nodepool (enabled by default)",
        ]
        assert captions[2] == (
            "1 object: `Job/gpu-test-job` · :orange-badge[:material/memory: requests GPUs]"
        )
        assert command(app) == "gco jobs submit examples/gpu-job.yaml -r us-east-1"

    def test_without_simple_job_the_first_example_is_chosen(
        self, fake_fleet: FakeFleet, tmp_path: Path
    ) -> None:
        for name in ("alpha", "beta"):
            (tmp_path / f"{name}.yaml").write_text(
                f"apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: {name}-job\n",
                encoding="utf-8",
            )
        fake_fleet.overrides["examples"] = [
            ExampleManifest(
                name=name, path=str(tmp_path / f"{name}.yaml"), kinds=("Job",), gpu=False
            )
            for name in ("alpha", "beta")
        ]

        app = open_dialog()

        picker = app.selectbox(key="submit_example")
        assert picker.options == ["alpha", "beta"]
        assert picker.value.name == "alpha"
        # No summary and no prerequisites: only the manifest summary and the command.
        assert values(app.caption) == ["1 object: `Job/alpha-job`", "The same, from a terminal:"]
        assert command(app) == "gco jobs submit examples/alpha.yaml -r us-east-1"

    def test_an_example_gone_since_the_list_was_read_is_reported(
        self, fake_fleet: FakeFleet, tmp_path: Path
    ) -> None:
        (tmp_path / "kept.yaml").write_text(
            "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: kept-job\n", encoding="utf-8"
        )
        listed = [
            ExampleManifest(
                name=name, path=str(tmp_path / f"{name}.yaml"), kinds=("Job",), gpu=False
            )
            for name in ("gone", "kept")
        ]

        # The cached list still names gone.yaml, which a branch switch removed;
        # read again, the checkout lists only kept.yaml.
        def examples() -> list[ExampleManifest]:
            return listed if len(fake_fleet.called("examples")) == 1 else listed[1:]

        fake_fleet.overrides["examples"] = examples

        app = open_dialog()

        assert not app.exception
        assert values(app.warning) == [
            f"Could not read examples/gone.yaml ({os.strerror(errno.ENOENT)}). "
            "Pick another example."
        ]
        assert command(app) == "gco jobs submit job.yaml -r us-east-1"
        assert not enabled(app, "submit_validate") and not enabled(app, "submit_go")

        # The stale list was dropped, so the next run reads the checkout again.
        app.run()

        assert len(fake_fleet.called("examples")) == 2
        assert app.selectbox(key="submit_example").options == ["kept"]
        assert not app.warning
        assert command(app) == "gco jobs submit examples/kept.yaml -r us-east-1"
        assert enabled(app, "submit_go")

    def test_a_checkout_without_examples_says_so(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["examples"] = []

        app = open_dialog()

        assert values(app.info) == [NO_EXAMPLES]
        assert command(app) == "gco jobs submit job.yaml -r us-east-1"
        assert not enabled(app, "submit_validate") and not enabled(app, "submit_go")

    def test_unreadable_examples_show_the_error_and_an_empty_list(
        self, fake_fleet: FakeFleet
    ) -> None:
        fake_fleet.fail["examples"] = DashboardError("examples/ is unreadable")

        app = open_dialog()

        assert values(app.error) == ["**examples/ is unreadable**"]
        assert values(app.info) == [NO_EXAMPLES]
        assert not enabled(app, "submit_go")

    def test_clearing_the_example_yaml_leaves_nothing_to_send(self, fake_fleet: FakeFleet) -> None:
        app = open_dialog()
        app.text_area(key="submit_yaml_simple-job").input("").run()

        assert "1 object: `Job/hello-gco`" not in values(app.caption)
        assert not enabled(app, "submit_validate") and not enabled(app, "submit_go")
        assert not app.error

    def test_pasted_yaml_with_two_jobs_is_sent_whole(self, fake_fleet: FakeFleet) -> None:
        app = paste(open_dialog(), TWO_JOBS)

        assert "2 objects: `Job/first`, `Job/second`" in values(app.caption)
        assert command(app) == "gco jobs submit job.yaml -r us-east-1"

        app.button(key="submit_go").click().run()

        assert fake_fleet.called("submit_job") == [
            (
                (load_manifest_text(TWO_JOBS),),
                {"region": "us-east-1", "namespace": None, "labels": {}},
            )
        ]
        assert values(app.toast) == ["Submitted first to us-east-1"]

    @pytest.mark.parametrize("text", ["", "   \n\t\n"])
    def test_an_empty_paste_is_not_an_error(self, fake_fleet: FakeFleet, text: str) -> None:
        app = paste(open_dialog(), text)

        assert not app.error
        assert values(app.caption) == ["The same, from a terminal:"]
        assert not enabled(app, "submit_validate") and not enabled(app, "submit_go")

    def test_an_uploaded_manifest_is_named_in_the_command(self, fake_fleet: FakeFleet) -> None:
        app = open_dialog()
        app.segmented_control(key="submit_source").set_value("upload").run()

        assert app.file_uploader(key="submit_upload").allowed_type == [".yaml", ".yml"]
        assert command(app) == "gco jobs submit job.yaml -r us-east-1"
        assert not enabled(app, "submit_go")

        uploaded = "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: trainer\n"
        app.file_uploader(key="submit_upload").upload(
            "train.yaml", uploaded.encode(), "application/x-yaml"
        ).run()

        assert "1 object: `Job/trainer`" in values(app.caption)
        assert command(app) == "gco jobs submit train.yaml -r us-east-1"

        app.button(key="submit_go").click().run()

        assert fake_fleet.called("submit_job") == [
            (
                (load_manifest_text(uploaded),),
                {"region": "us-east-1", "namespace": None, "labels": {}},
            )
        ]
        assert values(app.toast) == ["Submitted trainer to us-east-1"]


class TestValidation:
    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("kind: [unclosed\n", "The YAML does not parse: "),
            ("# only a comment\n", "The YAML holds no manifests."),
            ("- one\n- two\n", "Every YAML document must be a Kubernetes object with a kind."),
            ("metadata:\n  name: kindless\n", "Every YAML document must be a Kubernetes object"),
        ],
    )
    def test_yaml_that_is_not_manifests_is_reported(
        self, fake_fleet: FakeFleet, text: str, message: str
    ) -> None:
        app = paste(open_dialog(), text)

        (error,) = values(app.error)
        assert error.startswith(message)
        assert values(app.caption) == ["The same, from a terminal:"]
        assert not enabled(app, "submit_validate") and not enabled(app, "submit_go")

    def test_a_label_that_is_not_a_pair_stops_the_form(self, fake_fleet: FakeFleet) -> None:
        app = open_dialog()
        app.text_input(key="submit_labels").input("team=ml, oops").run()

        assert values(app.error) == ["'oops' is not a label of the form key=value."]
        assert not app.code
        assert "submit_validate" not in button_keys(app)
        assert "submit_go" not in button_keys(app)

    def test_validate_runs_a_dry_run_in_the_region(self, fake_fleet: FakeFleet) -> None:
        app = open_dialog()
        app.button(key="submit_validate").click().run()

        assert fake_fleet.called("submit_job") == [
            (
                ([simple_manifest()],),
                {"region": "us-east-1", "namespace": None, "labels": {}, "dry_run": True},
            )
        ]
        assert values(app.success) == ["The Region's API accepts these manifests."]
        assert json.loads(app.json[0].value) == {"success": True, "resources": [], "errors": None}
        assert not app.toast

    @pytest.mark.parametrize(
        ("errors", "shown"),
        [
            (
                ["image is not trusted", "kind is not allowed"],
                "image is not trusted; kind is not allowed",
            ),
            (None, "Rejected."),
        ],
    )
    def test_a_rejected_dry_run_says_why(
        self, fake_fleet: FakeFleet, errors: list[str] | None, shown: str
    ) -> None:
        fake_fleet.overrides["submit_job"] = {"success": False, "errors": errors}

        app = open_dialog()
        app.button(key="submit_validate").click().run()

        assert values(app.error) == [shown]
        assert not app.success
        assert json.loads(app.json[0].value) == {"success": False, "errors": errors}

    def test_a_dry_run_that_fails_shows_the_error_card(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["submit_job"] = DashboardError(
            "The API refused the manifests (400): namespace not allowed"
        )

        app = open_dialog()
        app.button(key="submit_validate").click().run()

        assert values(app.error) == [
            "**The API refused the manifests (400): namespace not allowed**"
        ]
        assert not app.json and not app.success


class TestRegionPlacement:
    def test_submits_to_the_chosen_region_with_namespace_and_labels(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = open_dialog()
        app.selectbox(key="submit_region").select("us-west-2")
        app.text_input(key="submit_namespace").input("team-a")
        app.text_input(key="submit_labels").input("team=ml, project=sweep").run()

        assert command(app) == f"{SIMPLE} -r us-west-2 -l team=ml -l project=sweep -n team-a"

        app.button(key="submit_go").click().run()

        assert fake_fleet.called("submit_job") == [
            (
                ([simple_manifest()],),
                {
                    "region": "us-west-2",
                    "namespace": "team-a",
                    "labels": {"team": "ml", "project": "sweep"},
                },
            )
        ]
        assert values(app.toast) == ["Submitted hello-gco to us-west-2"]
        assert not app.error

    def test_a_failed_submission_keeps_the_form_with_the_error(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["submit_job"] = DashboardError(
            "us-east-1 is not reachable", hint="Check the regional stack."
        )

        app = open_dialog()
        app.button(key="submit_go").click().run()

        assert values(app.error) == ["**us-east-1 is not reachable**\n\nCheck the regional stack."]
        assert not app.toast
        assert enabled(app, "submit_go")

    def test_a_manifest_without_a_name_is_called_job(self, fake_fleet: FakeFleet) -> None:
        app = paste(open_dialog(), "apiVersion: v1\nkind: ConfigMap\n")

        assert "1 object: `ConfigMap/?`" in values(app.caption)

        app.button(key="submit_go").click().run()

        assert values(app.toast) == ["Submitted job to us-east-1"]

    def test_an_unknown_default_placement_starts_on_a_region(self, fake_fleet: FakeFleet) -> None:
        app = open_dialog("nowhere")

        assert app.segmented_control(key="submit_placement").value == "region"
        assert app.segmented_control(key="submit_placement").options == [
            "A Region",
            "Best Region now",
            "Central queue",
            "Region's SQS queue",
        ]


class TestBestRegionPlacement:
    def test_nothing_can_be_sent_before_a_recommendation(self, fake_fleet: FakeFleet) -> None:
        app = open_dialog("best")

        assert RECOMMEND_HINT in values(app.caption)
        assert "submit_region" not in [box.key for box in app.selectbox]
        assert command(app) == SIMPLE
        assert not enabled(app, "submit_validate") and not enabled(app, "submit_go")
        assert not fake_fleet.called("recommend_region")

    def test_recommend_then_validate_and_submit_there(self, fake_fleet: FakeFleet) -> None:
        app = open_dialog("best")
        app.text_input(key="submit_instance_type").input("g6e.xlarge")
        app.button(key="submit_recommend").click().run()

        assert fake_fleet.called("recommend_region") == [
            (([simple_manifest()],), {"instance_type": "g6e.xlarge"})
        ]
        assert values(app.success) == ["**us-west-2 · Oregon**: empty queue, 100% GPU available"]
        assert charts(app) == 1
        assert command(app) == f"{SIMPLE} -r us-west-2"

        app.button(key="submit_validate").click().run()

        assert fake_fleet.called("submit_job")[-1][1] == {
            "region": "us-west-2",
            "namespace": None,
            "labels": {},
            "dry_run": True,
        }

        app.button(key="submit_go").click().run()

        assert fake_fleet.called("submit_job")[-1][1] == {
            "region": "us-west-2",
            "namespace": None,
            "labels": {},
        }
        assert values(app.toast) == ["Submitted hello-gco to us-west-2"]
        # The recommendation is spent: the next job is scored afresh.
        assert submit.RECOMMENDATION_KEY not in app.session_state
        assert command(app) == SIMPLE

    def test_a_recommendation_holds_only_for_what_was_scored(self, fake_fleet: FakeFleet) -> None:
        app = open_dialog("best")
        app.button(key="submit_recommend").click().run()

        assert command(app) == f"{SIMPLE} -r us-west-2"

        # The GPU example was never scored: the CPU job's answer must not place it.
        app.selectbox(key="submit_example").select_index(1).run()

        assert not app.success
        assert RECOMMEND_HINT in values(app.caption)
        assert command(app) == "gco jobs submit examples/gpu-job.yaml"
        assert not enabled(app, "submit_go")

        # Back on the job that was scored, its answer applies again ...
        app.selectbox(key="submit_example").select_index(0).run()

        assert command(app) == f"{SIMPLE} -r us-west-2"

        # ... until the instance type it was scored for changes.
        app.text_input(key="submit_instance_type").input("p5.48xlarge").run()

        assert not app.success
        assert not enabled(app, "submit_go")
        assert len(fake_fleet.called("recommend_region")) == 1
        assert not fake_fleet.called("submit_job")

    def test_without_an_instance_type_regions_are_scored_on_queue_signals(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = open_dialog("best")
        app.button(key="submit_recommend").click().run()

        assert fake_fleet.called("recommend_region") == [
            (([simple_manifest()],), {"instance_type": None})
        ]

    def test_a_recommendation_without_a_ranking_draws_no_chart(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["recommend_region"] = {
            "region": "eu-west-1",
            "reason": "the only Region with capacity",
        }

        app = open_dialog("best")
        app.button(key="submit_recommend").click().run()

        assert values(app.success) == ["**eu-west-1 · Ireland**: the only Region with capacity"]
        assert charts(app) == 0
        assert command(app) == f"{SIMPLE} -r eu-west-1"

    def test_a_failed_recommendation_shows_the_error(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["recommend_region"] = DashboardError("capacity checker unavailable")

        app = open_dialog("best")
        app.button(key="submit_recommend").click().run()

        assert values(app.error) == ["**capacity checker unavailable**"]
        assert RECOMMEND_HINT in values(app.caption)
        assert not enabled(app, "submit_go")

    def test_an_answer_without_a_region_recommends_nothing(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.overrides["recommend_region"] = {"region": None, "reason": "no capacity"}

        app = open_dialog("best")
        app.button(key="submit_recommend").click().run()

        assert not app.success
        assert RECOMMEND_HINT in values(app.caption)
        assert not enabled(app, "submit_go")

    def test_without_a_manifest_there_is_nothing_to_place(self, fake_fleet: FakeFleet) -> None:
        app = paste(open_dialog("best"), "")

        assert "submit_recommend" not in button_keys(app)
        assert command(app) == "gco jobs submit job.yaml"


class TestCentralQueuePlacement:
    def test_queues_one_job_with_a_priority_and_a_spot_price_gate(
        self, fake_fleet: FakeFleet
    ) -> None:
        app = open_dialog("queue")

        assert [expander.label for expander in app.expander] == ["Spot price gate"]
        assert command(app) == "gco queue submit examples/simple-job.yaml -r us-east-1"
        # The central queue has no dry run.
        assert not enabled(app, "submit_validate") and enabled(app, "submit_go")

        app.slider(key="submit_priority").set_value(40)
        app.text_input(key="submit_spot_type").input("g5.xlarge")
        app.number_input(key="submit_spot_price").set_value(0.5).run()

        assert command(app) == (
            "gco queue submit examples/simple-job.yaml -r us-east-1 -p 40 "
            "--max-spot-price 0.5 --spot-instance-type g5.xlarge"
        )

        app.button(key="submit_go").click().run()

        assert fake_fleet.called("queue_submit") == [
            (
                (simple_manifest(),),
                {
                    "region": "us-east-1",
                    "namespace": None,
                    "priority": 40,
                    "labels": {},
                    "max_spot_price": 0.5,
                    "spot_instance_type": "g5.xlarge",
                },
            )
        ]
        assert values(app.toast) == ["Queued hello-gco for us-east-1"]

    def test_without_a_cap_the_job_is_queued_ungated(self, fake_fleet: FakeFleet) -> None:
        app = open_dialog("queue")
        app.selectbox(key="submit_region").select("eu-west-1")
        app.text_input(key="submit_namespace").input("gco-jobs").run()

        assert command(app) == "gco queue submit examples/simple-job.yaml -r eu-west-1 -n gco-jobs"

        app.button(key="submit_go").click().run()

        assert fake_fleet.called("queue_submit") == [
            (
                (simple_manifest(),),
                {
                    "region": "eu-west-1",
                    "namespace": "gco-jobs",
                    "priority": 0,
                    "labels": {},
                    "max_spot_price": None,
                    "spot_instance_type": None,
                },
            )
        ]
        assert values(app.toast) == ["Queued hello-gco for eu-west-1"]

    @pytest.mark.parametrize(
        ("instance_type", "cap", "problem"),
        [
            ("g5.xlarge", 0.0, "Set a cap for g5.xlarge, or clear the instance type."),
            ("", 0.5, "Name an instance type for the cap, or set the cap to 0."),
            # Blank space names no instance type.
            ("  ", 0.5, "Name an instance type for the cap, or set the cap to 0."),
        ],
    )
    def test_a_half_filled_spot_gate_is_explained_and_not_sent(
        self, fake_fleet: FakeFleet, instance_type: str, cap: float, problem: str
    ) -> None:
        app = open_dialog("queue")
        # Filling in half the gate and clicking Submit straight away reach the
        # dialog in one run. Submit turns disabled in that run, so the click is dropped.
        app.text_input(key="submit_spot_type").input(instance_type)
        app.number_input(key="submit_spot_price").set_value(cap)
        app.button(key="submit_go").click().run()

        assert not fake_fleet.called("queue_submit")
        assert not app.error and not app.toast
        assert values(app.warning) == [problem]
        assert command(app) == "gco queue submit examples/simple-job.yaml -r us-east-1"
        assert not enabled(app, "submit_go")

        # With both halves the gate is in the command and Submit is back.
        app.text_input(key="submit_spot_type").input("g5.xlarge")
        app.number_input(key="submit_spot_price").set_value(0.5).run()

        assert not app.warning
        assert command(app) == (
            "gco queue submit examples/simple-job.yaml -r us-east-1 "
            "--max-spot-price 0.5 --spot-instance-type g5.xlarge"
        )
        assert enabled(app, "submit_go")

    @pytest.mark.parametrize(
        ("instance_type", "cap", "problem"),
        [
            ("G5.XLARGE", 0.5, "spot_instance_type is not a valid EC2 instance type"),
            ("g5", 0.5, "spot_instance_type is not a valid EC2 instance type"),
            ("g5.xlarge", 5000.0, "max_spot_price must be between 0.0001 and 1000.0 USD/hour"),
            ("g5.xlarge", 0.00001, "max_spot_price must be between 0.0001 and 1000.0 USD/hour"),
        ],
        ids=["uppercase-type", "type-without-size", "cap-too-high", "cap-too-low"],
    )
    def test_a_gate_the_queue_would_refuse_is_explained_before_submit(
        self, fake_fleet: FakeFleet, instance_type: str, cap: float, problem: str
    ) -> None:
        app = open_dialog("queue")
        # Both halves set, but the queue's own check refuses them: the dialog
        # says why beside the fields, and the click on Submit is dropped.
        app.text_input(key="submit_spot_type").input(instance_type)
        app.number_input(key="submit_spot_price").set_value(cap)
        app.button(key="submit_go").click().run()

        assert values(app.warning) == [problem]
        assert not enabled(app, "submit_go")
        assert not fake_fleet.called("queue_submit")
        assert not app.error and not app.toast

        app.text_input(key="submit_spot_type").input("g5.xlarge")
        app.number_input(key="submit_spot_price").set_value(0.5).run()

        assert not app.warning
        assert enabled(app, "submit_go")

    def test_the_queue_takes_exactly_one_manifest(self, fake_fleet: FakeFleet) -> None:
        app = paste(open_dialog("queue"), TWO_JOBS)

        assert values(app.warning) == ["The central queue takes exactly one Job manifest."]
        assert not enabled(app, "submit_go")

    def test_without_a_manifest_there_is_no_warning(self, fake_fleet: FakeFleet) -> None:
        app = paste(open_dialog("queue"), "")

        assert not app.warning
        assert not enabled(app, "submit_go")

    def test_a_refused_queue_submission_shows_why(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["queue_submit"] = DashboardError(
            "max_spot_price and spot_instance_type must be provided together"
        )

        app = open_dialog("queue")
        app.button(key="submit_go").click().run()

        assert values(app.error) == [
            "**max_spot_price and spot_instance_type must be provided together**"
        ]
        assert not app.toast


class TestSqsPlacement:
    def test_sends_the_manifests_to_the_regions_sqs_queue(self, fake_fleet: FakeFleet) -> None:
        app = open_dialog("sqs")

        assert not app.expander  # The Spot price gate is the central queue's.
        app.selectbox(key="submit_region").select("eu-west-1")
        app.slider(key="submit_priority").set_value(10).run()

        assert command(app) == "gco jobs submit-sqs examples/simple-job.yaml -r eu-west-1 -p 10"
        assert not enabled(app, "submit_validate")

        app.button(key="submit_go").click().run()

        assert fake_fleet.called("submit_job_sqs") == [
            (
                ([simple_manifest()],),
                {"region": "eu-west-1", "namespace": None, "labels": {}, "priority": 10},
            )
        ]
        assert values(app.toast) == ["Sent hello-gco to eu-west-1's SQS queue"]

    def test_a_failed_send_shows_the_error(self, fake_fleet: FakeFleet) -> None:
        fake_fleet.fail["submit_job_sqs"] = DashboardError(
            "AWS denied the request: sqs:SendMessage"
        )

        app = open_dialog("sqs")
        app.button(key="submit_go").click().run()

        assert values(app.error) == ["**AWS denied the request: sqs:SendMessage**"]
        assert not app.toast


class TestCliCommand:
    @pytest.mark.parametrize(
        ("placement", "region", "expected"),
        [
            ("region", "us-east-1", "gco jobs submit job.yaml -r us-east-1"),
            ("region", None, "gco jobs submit job.yaml"),
            ("best", "us-west-2", "gco jobs submit job.yaml -r us-west-2"),
            ("queue", None, "gco queue submit job.yaml -r REGION"),
            ("sqs", None, "gco jobs submit-sqs job.yaml -r REGION"),
        ],
    )
    def test_each_placement_names_its_command(
        self, placement: str, region: str | None, expected: str
    ) -> None:
        assert submit._cli(placement, "job.yaml", region, {}, []) == expected

    def test_paths_labels_and_extra_words_are_shell_quoted(self) -> None:
        line = submit._cli(
            "region", "my jobs/job.yaml", "us-east-1", {"note": "two words"}, ["-n", "team-a"]
        )

        assert (
            line == "gco jobs submit 'my jobs/job.yaml' -r us-east-1 -l 'note=two words' -n team-a"
        )
