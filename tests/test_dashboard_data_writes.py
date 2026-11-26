"""Behaviour of ``FleetData``'s actions: submissions, deletes, the queue, endpoints, hooks.

Every action the dashboard can take goes through ``cli.dashboard.data``,
which hands it to the CLI's job and inference managers or posts it through
the signed API client. These tests drive each action against autospecs of
those boundaries and check what reaches them: the manifests are private
copies, empty options are left out, names and IDs are encoded as one path
segment (and refused when they would climb the path), the spot price gate
and the queue namespace are validated before any request, a bulk delete in
regional API mode is summed across every Region's bridge with each failure
named, and an endpoint change the registry refuses becomes a
``DashboardError``. Nothing reaches AWS or the network.
"""

from __future__ import annotations

import copy
from types import MappingProxyType
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from cli.aws_client import APIRequestError
from cli.dashboard import data
from cli.dashboard.data import DashboardError, FleetData
from cli.inference import InferenceManager
from cli.jobs import JobManager
from tests.test_dashboard_data import (
    api_fixture,  # noqa: F401
    fleet_fixture,  # noqa: F401
    patch_factory,
    regional_api_fixture,  # noqa: F401
    regional_fleet_fixture,  # noqa: F401
)

_JOB: dict[str, Any] = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {"name": "trainer"},
    "spec": {
        "template": {
            "spec": {
                "containers": [{"name": "main", "image": "busybox:1.38.0"}],
                "restartPolicy": "Never",
            }
        }
    },
}


@pytest.fixture(name="jobs")
def jobs_fixture(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, MagicMock]:
    """The CLI's job manager, as ``cli.dashboard.data`` imports its factory."""
    return patch_factory(monkeypatch, "cli.dashboard.data.get_job_manager", JobManager)


@pytest.fixture(name="inference")
def inference_fixture(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, MagicMock]:
    return patch_factory(monkeypatch, "cli.inference.get_inference_manager", InferenceManager)


# ---------------------------------------------------------------------------
# Job submission
# ---------------------------------------------------------------------------


class TestSubmitJob:
    def test_the_manager_gets_private_copies_and_every_option(
        self, fleet: FleetData, jobs: tuple[Any, MagicMock]
    ) -> None:
        manager, factory = jobs
        received: list[Any] = []

        def submit(manifests: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            received.append(copy.deepcopy(manifests))
            # The CLI stamps its own labels into the manifests it is given.
            manifests[0]["metadata"]["labels"] = {"gco.io/submitted-by": "cli"}
            return {"status": "success", "resources": [{"name": "trainer", "status": "created"}]}

        manager.submit_job.side_effect = submit
        original = copy.deepcopy(_JOB)
        result = fleet.submit_job(
            [MappingProxyType(_JOB)],
            region="us-west-2",
            namespace="ml",
            labels={"team": "ml"},
            dry_run=True,
        )
        assert result == {
            "status": "success",
            "resources": [{"name": "trainer", "status": "created"}],
        }
        assert original == _JOB
        assert received == [[original]]
        assert manager.submit_job.call_args.kwargs == {
            "namespace": "ml",
            "target_region": "us-west-2",
            "dry_run": True,
            "labels": {"team": "ml"},
        }
        factory.assert_called_once_with(fleet.config)

    @pytest.mark.parametrize("labels", [None, {}])
    def test_defaults_and_empty_labels_send_nothing_extra(
        self, fleet: FleetData, jobs: tuple[Any, MagicMock], labels: dict[str, str] | None
    ) -> None:
        manager, _ = jobs
        manager.submit_job.return_value = {"status": "success"}
        fleet.submit_job([_JOB], region="us-east-1", labels=labels)
        manager.submit_job.assert_called_once_with(
            [_JOB], namespace=None, target_region="us-east-1", dry_run=False, labels=None
        )

    def test_submit_job_sqs_queues_private_copies_on_the_regions_queue(
        self, fleet: FleetData, jobs: tuple[Any, MagicMock]
    ) -> None:
        manager, _ = jobs
        manager.submit_job_sqs.return_value = {"message_id": "m-1", "queue": "gco-jobs-us-east-1"}
        assert fleet.submit_job_sqs(
            [_JOB], region="us-east-1", namespace="ml", labels={"team": "ml"}, priority=7
        ) == {"message_id": "m-1", "queue": "gco-jobs-us-east-1"}
        manager.submit_job_sqs.assert_called_once_with(
            [_JOB], region="us-east-1", namespace="ml", labels={"team": "ml"}, priority=7
        )
        assert manager.submit_job_sqs.call_args.args[0][0] is not _JOB

    def test_submit_job_sqs_defaults(self, fleet: FleetData, jobs: tuple[Any, MagicMock]) -> None:
        manager, _ = jobs
        fleet.submit_job_sqs([_JOB], region="eu-west-1", labels={})
        manager.submit_job_sqs.assert_called_once_with(
            [_JOB], region="eu-west-1", namespace=None, labels=None, priority=0
        )


class TestJobActions:
    @pytest.mark.parametrize(("uid", "expected"), [("uid-trainer", "uid-trainer"), (None, None)])
    def test_delete_job_passes_the_uid_guard(
        self,
        fleet: FleetData,
        jobs: tuple[Any, MagicMock],
        uid: str | None,
        expected: str | None,
    ) -> None:
        manager, _ = jobs
        manager.delete_job.return_value = {"status": "deleted", "job_name": "trainer"}
        kwargs = {"uid": uid} if uid else {}
        assert fleet.delete_job("trainer", "gco-jobs", "us-east-1", **kwargs) == {
            "status": "deleted",
            "job_name": "trainer",
        }
        manager.delete_job.assert_called_once_with(
            "trainer", "gco-jobs", region="us-east-1", expected_uid=expected
        )

    def test_retry_job_asks_the_jobs_region(
        self, fleet: FleetData, jobs: tuple[Any, MagicMock]
    ) -> None:
        manager, _ = jobs
        manager.retry_job.return_value = {"new_job_name": "trainer-retry-1"}
        assert fleet.retry_job("trainer", "gco-jobs", "us-west-2") == {
            "new_job_name": "trainer-retry-1"
        }
        manager.retry_job.assert_called_once_with("trainer", "gco-jobs", region="us-west-2")


class TestCleanupJobs:
    def test_global_mode_is_one_aggregator_call(
        self, fleet: FleetData, jobs: tuple[Any, MagicMock]
    ) -> None:
        manager, _ = jobs
        answer = {"dry_run": True, "total_matched": 3, "total_deleted": 0, "errors": None}
        manager.bulk_delete_global.return_value = answer
        assert (
            fleet.cleanup_jobs(
                status="succeeded", older_than_days=7, namespace="gco-jobs", dry_run=True
            )
            == answer
        )
        manager.bulk_delete_global.assert_called_once_with(
            namespace="gco-jobs", status="succeeded", older_than_days=7, dry_run=True
        )
        manager.bulk_delete_jobs.assert_not_called()

    def test_regional_mode_sums_every_bridge_and_names_each_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        regional_fleet: FleetData,
        jobs: tuple[Any, MagicMock],
    ) -> None:
        manager, _ = jobs
        monkeypatch.setattr(
            data, "_load_cdk_json", lambda: {"regional": ["us-east-1", "us-west-2", "eu-west-1"]}
        )
        answers = {
            "us-east-1": {"total_matched": 3, "deleted_count": 2, "failed_count": 1},
            "us-west-2": {"total_matched": 1, "deleted_count": 1, "failed_count": 0},
        }

        def bulk(*, region: str | None = None, **kwargs: Any) -> dict[str, Any]:
            if region == "eu-west-1":
                raise APIRequestError(500, "Internal Server Error")
            return answers[str(region)]

        manager.bulk_delete_jobs.side_effect = bulk
        # The aggregator's shape: each answering Region's counts, the Jobs it
        # failed to delete among them, and the Regions that did not answer.
        assert regional_fleet.cleanup_jobs(
            status="failed", older_than_days=None, namespace=None, dry_run=False
        ) == {
            "dry_run": False,
            "total_matched": 4,
            "total_deleted": 3,
            "regions_queried": 3,
            "region_results": [
                {"region": "us-east-1", "matched": 3, "deleted": 2, "failed": 1},
                {"region": "us-west-2", "matched": 1, "deleted": 1, "failed": 0},
            ],
            "errors": [
                {
                    "region": "eu-west-1",
                    "error": "API request failed: Internal Server Error (HTTP 500)",
                }
            ],
        }
        assert sorted(
            made.kwargs["region"] for made in manager.bulk_delete_jobs.call_args_list
        ) == [
            "eu-west-1",
            "us-east-1",
            "us-west-2",
        ]
        for made in manager.bulk_delete_jobs.call_args_list:
            assert made == call(
                namespace=None,
                status="failed",
                older_than_days=None,
                region=made.kwargs["region"],
                dry_run=False,
            )
        manager.bulk_delete_global.assert_not_called()

    def test_a_clean_regional_dry_run_reports_no_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        regional_fleet: FleetData,
        jobs: tuple[Any, MagicMock],
    ) -> None:
        manager, _ = jobs
        monkeypatch.setattr(
            data, "_load_cdk_json", lambda: {"regional": ["us-east-1", "us-west-2"]}
        )
        manager.bulk_delete_jobs.return_value = {"dry_run": True, "total_matched": 2}
        assert regional_fleet.cleanup_jobs(
            status="succeeded", older_than_days=3, namespace="ml", dry_run=True
        ) == {
            "dry_run": True,
            "total_matched": 4,
            "total_deleted": 0,
            "regions_queried": 2,
            "region_results": [
                {"region": "us-east-1", "matched": 2, "deleted": 0, "failed": 0},
                {"region": "us-west-2", "matched": 2, "deleted": 0, "failed": 0},
            ],
            "errors": None,
        }

    def test_a_count_a_bridge_answers_as_null_is_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        regional_fleet: FleetData,
        jobs: tuple[Any, MagicMock],
    ) -> None:
        manager, _ = jobs
        monkeypatch.setattr(
            data, "_load_cdk_json", lambda: {"regional": ["us-east-1", "us-west-2"]}
        )
        answers = {
            "us-east-1": {"total_matched": None, "deleted_count": None, "failed_count": None},
            "us-west-2": {"total_matched": 2, "deleted_count": 1, "failed_count": 1},
        }
        manager.bulk_delete_jobs.side_effect = lambda *, region, **kwargs: answers[region]
        report = regional_fleet.cleanup_jobs(
            status="succeeded", older_than_days=None, namespace=None, dry_run=False
        )
        assert (report["total_matched"], report["total_deleted"]) == (2, 1)
        assert report["region_results"] == [
            {"region": "us-east-1", "matched": 0, "deleted": 0, "failed": 0},
            {"region": "us-west-2", "matched": 2, "deleted": 1, "failed": 1},
        ]
        assert report["errors"] is None


# ---------------------------------------------------------------------------
# Central job queue
# ---------------------------------------------------------------------------


class TestQueueSubmit:
    def test_a_manifest_without_a_namespace_takes_the_configured_default(
        self, fleet: FleetData, api: Any
    ) -> None:
        api.call_api.return_value = {"job_id": "q-1", "status": "queued"}
        assert fleet.queue_submit(_JOB, region="us-west-2") == {"job_id": "q-1", "status": "queued"}
        api.call_api.assert_called_once_with(
            "POST",
            "/api/v1/queue/jobs",
            region=None,
            body={
                "manifest": _JOB,
                "target_region": "us-west-2",
                "namespace": "gco-jobs",
                "priority": 0,
                "labels": None,
            },
        )
        assert api.call_api.call_args.kwargs["body"]["manifest"] is not _JOB

    def test_the_manifests_own_namespace_and_every_option_are_sent(
        self, fleet: FleetData, api: Any
    ) -> None:
        manifest = copy.deepcopy(_JOB)
        manifest["metadata"]["namespace"] = "ml-team"
        fleet.queue_submit(
            manifest,
            region="eu-west-1",
            namespace="ml-team",
            priority=5,
            labels={"team": "ml"},
            max_spot_price=1.25,
            spot_instance_type="g5.xlarge",
        )
        assert api.call_api.call_args.kwargs["body"] == {
            "manifest": manifest,
            "target_region": "eu-west-1",
            "namespace": "ml-team",
            "priority": 5,
            "labels": {"team": "ml"},
            "max_spot_price": 1.25,
            "spot_instance_type": "g5.xlarge",
        }

    def test_a_namespace_that_contradicts_the_manifest_is_refused_before_any_call(
        self, fleet: FleetData, api: Any
    ) -> None:
        manifest = copy.deepcopy(_JOB)
        manifest["metadata"]["namespace"] = "ml-team"
        with pytest.raises(
            DashboardError,
            match="the manifest declares namespace 'ml-team' but --namespace is 'other'",
        ) as raised:
            fleet.queue_submit(manifest, region="us-east-1", namespace="other")
        assert isinstance(raised.value.__cause__, ValueError)
        api.call_api.assert_not_called()

    @pytest.mark.parametrize(
        ("price", "instance_type", "message"),
        [
            (1.5, None, "max_spot_price and spot_instance_type must be provided together"),
            (None, "g5.xlarge", "max_spot_price and spot_instance_type must be provided together"),
            (0.0, "g5.xlarge", "max_spot_price must be between"),
            (1.0, "not a type!", "spot_instance_type is not a valid EC2 instance type"),
        ],
        ids=["price-only", "type-only", "price-out-of-range", "bad-type"],
    )
    def test_an_incomplete_or_invalid_spot_gate_is_refused_before_any_call(
        self,
        fleet: FleetData,
        api: Any,
        price: float | None,
        instance_type: str | None,
        message: str,
    ) -> None:
        with pytest.raises(DashboardError, match=message):
            fleet.queue_submit(
                _JOB, region="us-east-1", max_spot_price=price, spot_instance_type=instance_type
            )
        api.call_api.assert_not_called()

    def test_regional_mode_posts_through_the_target_regions_bridge(
        self, regional_fleet: FleetData, regional_api: Any
    ) -> None:
        regional_fleet.queue_submit(_JOB, region="us-west-2", namespace="ml")
        sent = regional_api.call_api.call_args
        assert sent.kwargs["region"] == "us-west-2"
        assert sent.kwargs["body"]["namespace"] == "ml"


class TestQueueCancel:
    @pytest.mark.parametrize(
        ("reason", "params"),
        [("superseded by run 2", {"reason": "superseded by run 2"}), (None, None), ("", None)],
        ids=["reason", "none", "empty"],
    )
    def test_cancel_encodes_the_id_and_sends_a_reason_only_when_given(
        self, fleet: FleetData, api: Any, reason: str | None, params: dict[str, str] | None
    ) -> None:
        api.call_api.return_value = {"status": "cancelled"}
        assert fleet.queue_cancel("q 1", reason=reason) == {"status": "cancelled"}
        api.call_api.assert_called_once_with(
            "DELETE", "/api/v1/queue/jobs/q%201", region=None, params=params
        )

    def test_regional_mode_cancels_through_the_default_bridge(
        self, regional_fleet: FleetData, regional_api: Any
    ) -> None:
        regional_fleet.queue_cancel("q-1")
        assert regional_api.call_api.call_args.kwargs["region"] == "us-east-1"


# ---------------------------------------------------------------------------
# Inference endpoints
# ---------------------------------------------------------------------------

_IMAGE = "vllm/vllm-openai:v0.12.0"

_MUTATIONS = [
    pytest.param("scale_endpoint", ("llm", 3), {}, "scale", call("llm", 3), id="scale"),
    pytest.param("stop_endpoint", ("llm",), {}, "stop", call("llm"), id="stop"),
    pytest.param("start_endpoint", ("llm",), {}, "start", call("llm"), id="start"),
    pytest.param("delete_endpoint", ("llm",), {}, "delete", call("llm"), id="delete"),
    pytest.param(
        "update_endpoint_image",
        ("llm", _IMAGE),
        {},
        "update_image",
        call("llm", _IMAGE),
        id="image",
    ),
    pytest.param(
        "add_endpoint_region",
        ("llm", "eu-west-1"),
        {},
        "add_region",
        call("llm", "eu-west-1"),
        id="add-region",
    ),
    pytest.param(
        "remove_endpoint_region",
        ("llm", "us-west-2"),
        {},
        "remove_region",
        call("llm", "us-west-2"),
        id="remove-region",
    ),
    pytest.param(
        "start_canary",
        ("llm", _IMAGE),
        {"weight": 20, "replicas": 2},
        "canary_deploy",
        call("llm", _IMAGE, weight=20, replicas=2),
        id="canary",
    ),
    pytest.param(
        "start_canary",
        ("llm", _IMAGE),
        {"weight": 10},
        "canary_deploy",
        call("llm", _IMAGE, weight=10, replicas=1),
        id="canary-one-replica",
    ),
    pytest.param("promote_canary", ("llm",), {}, "promote_canary", call("llm"), id="promote"),
    pytest.param("rollback_canary", ("llm",), {}, "rollback_canary", call("llm"), id="rollback"),
]


class TestEndpointChanges:
    def test_deploy_forwards_every_option_to_the_manager(
        self, fleet: FleetData, inference: tuple[Any, MagicMock]
    ) -> None:
        manager, factory = inference
        manager.deploy.return_value = {"endpoint_name": "llm", "desired_state": "deploying"}
        options = {
            "endpoint_name": "llm",
            "image": "vllm/vllm-openai:v0.11.0",
            "target_regions": ["us-east-1", "us-west-2"],
            "replicas": 2,
            "gpu_count": 1,
            "env": {"MODEL": "example-org/example-model"},
        }
        assert fleet.deploy_endpoint(**options) == {
            "endpoint_name": "llm",
            "desired_state": "deploying",
        }
        manager.deploy.assert_called_once_with(**options)
        factory.assert_called_once_with(fleet.config)

    @pytest.mark.parametrize(("method", "args", "kwargs", "manager_method", "expected"), _MUTATIONS)
    def test_a_change_returns_the_updated_record(
        self,
        fleet: FleetData,
        inference: tuple[Any, MagicMock],
        method: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        manager_method: str,
        expected: Any,
    ) -> None:
        manager, factory = inference
        record = {"endpoint_name": "llm", "desired_state": "running"}
        getattr(manager, manager_method).return_value = record
        assert getattr(fleet, method)(*args, **kwargs) == record
        assert getattr(manager, manager_method).call_args_list == [expected]
        factory.assert_called_once_with(fleet.config)

    @pytest.mark.parametrize(("method", "args", "kwargs", "manager_method", "expected"), _MUTATIONS)
    def test_a_change_the_registry_refuses_is_an_error(
        self,
        fleet: FleetData,
        inference: tuple[Any, MagicMock],
        method: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        manager_method: str,
        expected: Any,
    ) -> None:
        manager, _ = inference
        getattr(manager, manager_method).return_value = None
        with pytest.raises(
            DashboardError,
            match=r"^Endpoint 'llm' was not changed: it does not exist or is not in a state "
            r"that allows this\.$",
        ):
            getattr(fleet, method)(*args, **kwargs)


# ---------------------------------------------------------------------------
# Webhooks and templates
# ---------------------------------------------------------------------------


class TestWebhooks:
    def test_a_webhook_needs_at_least_one_event(self, fleet: FleetData, api: Any) -> None:
        with pytest.raises(DashboardError, match=r"^Pick at least one event to subscribe to\.$"):
            fleet.create_webhook("https://hooks.example.com/gco", [])
        api.call_api.assert_not_called()

    def test_a_webhook_is_posted_with_a_trimmed_url_and_only_the_options_given(
        self, fleet: FleetData, api: Any
    ) -> None:
        api.call_api.return_value = {"webhook": {"id": "wh-1"}}
        assert fleet.create_webhook(
            "  https://hooks.example.com/gco  ", ("job.completed", "job.failed")
        ) == {"webhook": {"id": "wh-1"}}
        fleet.create_webhook(
            "https://hooks.example.com/ml",
            ["job.failed"],
            namespace="ml",
            secret="not-a-real-signing-secret",
        )
        assert api.call_api.call_args_list == [
            call(
                "POST",
                "/api/v1/webhooks",
                region=None,
                body={
                    "url": "https://hooks.example.com/gco",
                    "events": ["job.completed", "job.failed"],
                },
            ),
            call(
                "POST",
                "/api/v1/webhooks",
                region=None,
                body={
                    "url": "https://hooks.example.com/ml",
                    "events": ["job.failed"],
                    "namespace": "ml",
                    "secret": "not-a-real-signing-secret",
                },
            ),
        ]

    def test_delete_webhook_encodes_the_id(self, fleet: FleetData, api: Any) -> None:
        api.call_api.return_value = {"deleted": True}
        assert fleet.delete_webhook("wh/1") == {"deleted": True}
        api.call_api.assert_called_once_with("DELETE", "/api/v1/webhooks/wh%2F1", region=None)

    def test_regional_mode_manages_webhooks_through_the_default_bridge(
        self, regional_fleet: FleetData, regional_api: Any
    ) -> None:
        regional_fleet.create_webhook("https://hooks.example.com/gco", ["job.failed"])
        regional_fleet.delete_webhook("wh-1")
        assert [made.kwargs["region"] for made in regional_api.call_api.call_args_list] == [
            "us-east-1",
            "us-east-1",
        ]


class TestTemplates:
    def test_create_template_sends_a_copy_and_only_the_options_given(
        self, fleet: FleetData, api: Any
    ) -> None:
        api.call_api.return_value = {"template": {"name": "nightly"}}
        assert fleet.create_template("nightly", MappingProxyType(_JOB)) == {
            "template": {"name": "nightly"}
        }
        fleet.create_template(
            "sweep", _JOB, description="Learning-rate sweep", parameters={"lr": "1e-4"}
        )
        first, second = api.call_api.call_args_list
        assert first == call(
            "POST", "/api/v1/templates", region=None, body={"name": "nightly", "manifest": _JOB}
        )
        assert first.kwargs["body"]["manifest"] is not _JOB
        assert second.kwargs["body"] == {
            "name": "sweep",
            "manifest": _JOB,
            "description": "Learning-rate sweep",
            "parameters": {"lr": "1e-4"},
        }

    def test_delete_template_encodes_the_name(self, fleet: FleetData, api: Any) -> None:
        fleet.delete_template("nightly eval")
        api.call_api.assert_called_once_with(
            "DELETE", "/api/v1/templates/nightly%20eval", region=None
        )

    def test_run_template_creates_the_job_in_one_region(self, fleet: FleetData, api: Any) -> None:
        api.call_api.return_value = {"success": True, "job_name": "nightly-1"}
        assert fleet.run_template("nightly", job_name="nightly-1", region="us-west-2") == {
            "success": True,
            "job_name": "nightly-1",
        }
        fleet.run_template(
            "sweep", job_name="sweep-1", region="eu-west-1", namespace="ml", parameters={"lr": 0.01}
        )
        assert api.call_api.call_args_list == [
            call(
                "POST",
                "/api/v1/jobs/from-template/nightly",
                region="us-west-2",
                body={"name": "nightly-1", "namespace": "gco-jobs"},
            ),
            call(
                "POST",
                "/api/v1/jobs/from-template/sweep",
                region="eu-west-1",
                body={"name": "sweep-1", "namespace": "ml", "parameters": {"lr": 0.01}},
            ),
        ]

    def test_regional_mode_manages_templates_through_the_default_bridge(
        self, regional_fleet: FleetData, regional_api: Any
    ) -> None:
        regional_fleet.create_template("nightly", _JOB)
        regional_fleet.delete_template("nightly")
        assert [made.kwargs["region"] for made in regional_api.call_api.call_args_list] == [
            "us-east-1",
            "us-east-1",
        ]


@pytest.mark.parametrize(
    ("method", "args", "kwargs"),
    [
        pytest.param("delete_webhook", ("..",), {}, id="webhook"),
        pytest.param("delete_template", ("a/..",), {}, id="template"),
        pytest.param("queue_cancel", (".",), {}, id="queue"),
        pytest.param(
            "run_template", ("..",), {"job_name": "x", "region": "us-east-1"}, id="run-template"
        ),
    ],
)
def test_a_name_that_would_climb_the_api_path_is_refused_before_any_call(
    fleet: FleetData, api: Any, method: str, args: tuple[str, ...], kwargs: dict[str, str]
) -> None:
    with pytest.raises(ValueError, match="cannot be used as a name or ID in an API request path"):
        getattr(fleet, method)(*args, **kwargs)
    api.call_api.assert_not_called()
