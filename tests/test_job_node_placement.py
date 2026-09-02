"""Node-placement reporting on the job read surface.

A job constrained to a *set* of interchangeable instance types is placed by
Karpenter within that set, so the submitted manifest records only what the run
was *authorized* to use. These tests pin the surface that reports what it
actually used, at all three layers:

* ``gco.services.api_shared._collect_pod_scheduling`` — the collector that maps
  a Job's pods onto their nodes and reads each distinct node's labels once.
* the ``/api/v1/jobs/{ns}/{name}`` and ``.../pods`` routes — that the block is
  attached, and that a Node read which fails or is refused degrades the
  placement fields to ``None`` instead of failing the job read.
* ``cli.jobs.JobManager`` — that ``JobInfo`` carries the fields through to the
  top level of the CLI's JSON payload, including on the TrainJob path, and that
  they stay unset rather than guessed when placement is unknown.

The through-line every layer shares: an absent instance type is honest, a
guessed one that looks verified is not. Nothing here infers hardware from the
manifest.
"""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from cli.jobs import JobInfo, JobManager, _extract_scheduling
from gco.services.api_shared import (
    NODE_CAPACITY_TYPE_LABEL,
    NODE_INSTANCE_TYPE_LABEL,
    _collect_pod_scheduling,
    _parse_node_to_dict,
)
from tests._auth import bypass_backend_auth

# The eligible set from a plan that authorizes two interchangeable types. The
# estimate prices the more expensive member because the budget is a ceiling;
# reconciliation needs to know when the cheaper one actually ran.
EXPENSIVE_MEMBER = "g5.4xlarge"
CHEAPEST_MEMBER = "g5.2xlarge"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _pod(
    name: str,
    node_name: str | None,
    *,
    phase: str = "Running",
    created: datetime | None = None,
) -> MagicMock:
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.creation_timestamp = created or datetime(2024, 1, 1, tzinfo=UTC)
    pod.spec.node_name = node_name
    pod.status.phase = phase
    return pod


def _node(labels: dict[str, Any] | None) -> MagicMock:
    node = MagicMock()
    node.metadata.labels = labels
    return node


def _core_v1(nodes: dict[str, Any]) -> MagicMock:
    """A CoreV1Api whose ``read_node`` serves ``nodes``; unknown names 404."""
    core_v1 = MagicMock()

    def _read_node(name: str) -> Any:
        if name not in nodes:
            raise RuntimeError(f'nodes "{name}" not found')
        result = nodes[name]
        if isinstance(result, Exception):
            raise result
        return result

    core_v1.read_node.side_effect = _read_node
    return core_v1


def _gpu_node_labels(instance_type: str, capacity_type: str = "on-demand") -> dict[str, str]:
    return {
        NODE_INSTANCE_TYPE_LABEL: instance_type,
        NODE_CAPACITY_TYPE_LABEL: capacity_type,
        "topology.kubernetes.io/zone": "us-east-1a",
        "topology.kubernetes.io/region": "us-east-1",
        "kubernetes.io/arch": "amd64",
        "karpenter.sh/nodepool": "gco-gpu",
        # Not reported: hundreds of these exist on a real node and none of them
        # answer "what hardware did this run on".
        "beta.kubernetes.io/os": "linux",
    }


# ---------------------------------------------------------------------------
# The collector
# ---------------------------------------------------------------------------


class TestCollectPodScheduling:
    def test_reports_the_node_and_its_instance_and_capacity_type(self) -> None:
        core_v1 = _core_v1({"ip-10-0-1-7": _node(_gpu_node_labels(CHEAPEST_MEMBER, "spot"))})

        info = _collect_pod_scheduling(core_v1, [_pod("trainer-abc", "ip-10-0-1-7")])

        assert info["node_name"] == "ip-10-0-1-7"
        assert info["node_instance_type"] == CHEAPEST_MEMBER
        assert info["node_capacity_type"] == "spot"
        assert info["node_labels"][NODE_INSTANCE_TYPE_LABEL] == CHEAPEST_MEMBER
        assert info["node_lookup_error"] is None
        assert info["unscheduled_pods"] == 0
        assert len(info["nodes"]) == 1
        assert info["nodes"][0]["name"] == "ip-10-0-1-7"
        assert info["nodes"][0]["instance_type"] == CHEAPEST_MEMBER
        assert info["nodes"][0]["capacity_type"] == "spot"
        assert info["nodes"][0]["pods"] == [{"name": "trainer-abc", "phase": "Running"}]

    def test_reports_only_the_placement_relevant_labels(self) -> None:
        """A node carries hundreds of labels; the payload stays a fixed set."""
        core_v1 = _core_v1({"n1": _node(_gpu_node_labels(CHEAPEST_MEMBER))})

        info = _collect_pod_scheduling(core_v1, [_pod("p1", "n1")])

        assert set(info["node_labels"]) == {
            NODE_INSTANCE_TYPE_LABEL,
            NODE_CAPACITY_TYPE_LABEL,
            "topology.kubernetes.io/zone",
            "topology.kubernetes.io/region",
            "kubernetes.io/arch",
            "karpenter.sh/nodepool",
        }
        assert "beta.kubernetes.io/os" not in info["node_labels"]

    def test_reads_each_distinct_node_exactly_once(self) -> None:
        """The cost of this feature is one Node read per *distinct* node."""
        core_v1 = _core_v1(
            {
                "n1": _node(_gpu_node_labels(CHEAPEST_MEMBER)),
                "n2": _node(_gpu_node_labels(EXPENSIVE_MEMBER)),
            }
        )
        pods = [
            _pod("p1", "n1", created=datetime(2024, 1, 1, 0, 0, tzinfo=UTC)),
            _pod("p2", "n1", created=datetime(2024, 1, 1, 0, 1, tzinfo=UTC)),
            _pod("p3", "n2", created=datetime(2024, 1, 1, 0, 2, tzinfo=UTC)),
        ]

        info = _collect_pod_scheduling(core_v1, pods)

        assert core_v1.read_node.call_count == 2
        assert [n["name"] for n in info["nodes"]] == ["n1", "n2"]
        assert [p["name"] for p in info["nodes"][0]["pods"]] == ["p1", "p2"]

    def test_a_single_pod_job_costs_one_extra_api_call(self) -> None:
        core_v1 = _core_v1({"n1": _node(_gpu_node_labels(CHEAPEST_MEMBER))})

        _collect_pod_scheduling(core_v1, [_pod("p1", "n1")])

        assert core_v1.read_node.call_count == 1

    def test_reports_the_member_that_ran_not_the_priciest_eligible_one(self) -> None:
        """The acceptance case: a run authorized for two types used the cheaper.

        Nothing in the collector's inputs mentions the eligible set, so it
        cannot echo the plan back — it can only report the node's own label.
        """
        core_v1 = _core_v1({"n1": _node(_gpu_node_labels(CHEAPEST_MEMBER, "spot"))})

        info = _collect_pod_scheduling(core_v1, [_pod("p1", "n1")])

        assert info["node_instance_type"] == CHEAPEST_MEMBER
        assert info["node_instance_type"] != EXPENSIVE_MEMBER

    def test_a_retried_job_that_moved_instance_types_reports_both(self) -> None:
        """OOM on the smaller box, retry on the larger: §9 diagnosis needs both."""
        core_v1 = _core_v1(
            {
                "small": _node(_gpu_node_labels(CHEAPEST_MEMBER)),
                "large": _node(_gpu_node_labels(EXPENSIVE_MEMBER)),
            }
        )
        pods = [
            _pod("p-1", "small", phase="Failed", created=datetime(2024, 1, 1, 0, 0, tzinfo=UTC)),
            _pod("p-2", "large", phase="Succeeded", created=datetime(2024, 1, 1, 1, 0, tzinfo=UTC)),
        ]

        info = _collect_pod_scheduling(core_v1, pods)

        assert [(n["name"], n["instance_type"]) for n in info["nodes"]] == [
            ("small", CHEAPEST_MEMBER),
            ("large", EXPENSIVE_MEMBER),
        ]
        assert [n["pods"][0]["phase"] for n in info["nodes"]] == ["Failed", "Succeeded"]

    def test_the_scalar_fields_track_the_earliest_scheduled_pod(self) -> None:
        """Stable for the life of the job: later retries do not rewrite it."""
        core_v1 = _core_v1(
            {
                "first": _node(_gpu_node_labels(CHEAPEST_MEMBER)),
                "second": _node(_gpu_node_labels(EXPENSIVE_MEMBER)),
            }
        )
        # Deliberately out of chronological order — the API does not promise one.
        pods = [
            _pod("late", "second", created=datetime(2024, 1, 2, tzinfo=UTC)),
            _pod("early", "first", created=datetime(2024, 1, 1, tzinfo=UTC)),
        ]

        info = _collect_pod_scheduling(core_v1, pods)

        assert info["node_name"] == "first"
        assert info["node_instance_type"] == CHEAPEST_MEMBER

    def test_unscheduled_pods_are_counted_and_trigger_no_node_read(self) -> None:
        core_v1 = _core_v1({})

        info = _collect_pod_scheduling(core_v1, [_pod("pending-pod", None)])

        assert info["unscheduled_pods"] == 1
        assert info["node_name"] is None
        assert info["nodes"] == []
        core_v1.read_node.assert_not_called()

    def test_a_job_with_no_pods_reports_empty_placement(self) -> None:
        core_v1 = _core_v1({})

        info = _collect_pod_scheduling(core_v1, [])

        assert info["node_name"] is None
        assert info["node_instance_type"] is None
        assert info["node_labels"] == {}
        assert info["nodes"] == []
        core_v1.read_node.assert_not_called()

    @pytest.mark.parametrize(
        "failure",
        [
            RuntimeError('nodes "n1" is forbidden: User cannot get resource "nodes"'),
            RuntimeError('nodes "n1" not found'),
        ],
        ids=["rbac-refused", "node-reclaimed"],
    )
    def test_a_failed_node_read_says_so_instead_of_guessing(self, failure: Exception) -> None:
        core_v1 = _core_v1({"n1": failure})

        info = _collect_pod_scheduling(core_v1, [_pod("p1", "n1")])

        # The node the pod landed on is still known — it came from the pod.
        assert info["node_name"] == "n1"
        # Its hardware is not, and the payload admits that rather than
        # substituting a plausible-looking value.
        assert info["node_instance_type"] is None
        assert info["node_capacity_type"] is None
        assert info["node_labels"] == {}
        assert info["node_lookup_error"] is not None
        assert "n1" in info["node_lookup_error"]

    def test_one_unreadable_node_does_not_hide_the_readable_ones(self) -> None:
        core_v1 = _core_v1(
            {
                "good": _node(_gpu_node_labels(CHEAPEST_MEMBER)),
                "gone": RuntimeError('nodes "gone" not found'),
            }
        )
        pods = [
            _pod("p1", "good", created=datetime(2024, 1, 1, tzinfo=UTC)),
            _pod("p2", "gone", created=datetime(2024, 1, 2, tzinfo=UTC)),
        ]

        info = _collect_pod_scheduling(core_v1, pods)

        assert info["node_instance_type"] == CHEAPEST_MEMBER
        assert [n["instance_type"] for n in info["nodes"]] == [CHEAPEST_MEMBER, None]
        assert "gone" in (info["node_lookup_error"] or "")

    def test_a_node_missing_the_instance_type_label_reports_none(self) -> None:
        """Not "unknown": a caller must be able to tell absent from a value."""
        core_v1 = _core_v1({"n1": _node({"kubernetes.io/arch": "arm64"})})

        info = _collect_pod_scheduling(core_v1, [_pod("p1", "n1")])

        assert info["node_instance_type"] is None
        assert info["node_capacity_type"] is None
        assert info["node_labels"] == {"kubernetes.io/arch": "arm64"}

    def test_the_payload_is_json_serializable_for_a_malformed_node(self) -> None:
        """A Node whose labels are not a str->str dict cannot 500 the endpoint."""
        core_v1 = _core_v1({"n1": _node({NODE_INSTANCE_TYPE_LABEL: object(), 7: "x"})})

        info = _collect_pod_scheduling(core_v1, [_pod("p1", "n1")])

        assert info["node_instance_type"] is None
        json.dumps(info)

    def test_a_node_with_no_labels_at_all_is_tolerated(self) -> None:
        core_v1 = _core_v1({"n1": _node(None)})

        info = _collect_pod_scheduling(core_v1, [_pod("p1", "n1")])

        assert info["node_name"] == "n1"
        assert info["node_instance_type"] is None
        json.dumps(info)

    def test_pods_without_creation_timestamps_still_sort(self) -> None:
        core_v1 = _core_v1({"n1": _node(_gpu_node_labels(CHEAPEST_MEMBER))})
        pod = _pod("p1", "n1")
        pod.metadata.creation_timestamp = None

        info = _collect_pod_scheduling(core_v1, [pod])

        assert info["node_instance_type"] == CHEAPEST_MEMBER


class TestParseNodeToDict:
    def test_the_name_comes_from_the_pod_not_the_node_object(self) -> None:
        """Keeps the pod/node join exact whatever the Node object echoes back."""
        node = _node(_gpu_node_labels(CHEAPEST_MEMBER))
        node.metadata.name = "a-different-name"

        parsed = _parse_node_to_dict(node, "ip-10-0-1-7")

        assert parsed["name"] == "ip-10-0-1-7"


# ---------------------------------------------------------------------------
# The API routes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_authentication():
    """Route behavior only; the HMAC verifier has its own tests."""
    with bypass_backend_auth():
        yield


@pytest.fixture
def processor() -> MagicMock:
    proc = MagicMock()
    proc.cluster_id = "test-cluster"
    proc.region = "us-east-1"
    proc.core_v1 = MagicMock()
    proc.batch_v1 = MagicMock()
    proc.custom_objects = MagicMock()
    proc.allowed_namespaces = {"default", "gco-jobs"}
    proc.validation_enabled = True
    return proc


def _batch_job(name: str = "trainer") -> MagicMock:
    job = MagicMock()
    job.metadata.name = name
    job.metadata.namespace = "gco-jobs"
    job.metadata.creation_timestamp = datetime(2024, 1, 1, tzinfo=UTC)
    job.metadata.labels = {}
    job.metadata.annotations = {}
    job.metadata.uid = "job-uid"
    job.spec.parallelism = 1
    job.spec.completions = 1
    job.spec.backoff_limit = 0
    container = MagicMock()
    container.name = "main"
    container.image = "trainer:1"
    container.resources.limits = {}
    container.resources.requests = {}
    job.spec.template.spec.containers = [container]
    job.spec.template.spec.init_containers = []
    job.status.active = 0
    job.status.succeeded = 1
    job.status.failed = 0
    job.status.start_time = datetime(2024, 1, 1, tzinfo=UTC)
    job.status.completion_time = datetime(2024, 1, 1, 1, tzinfo=UTC)
    job.status.conditions = []
    return job


def _client(processor: MagicMock) -> Any:
    from fastapi.testclient import TestClient

    from gco.services.manifest_api import app

    return patch(
        "gco.services.manifest_api.create_manifest_processor_from_env",
        return_value=processor,
    ), TestClient(app, raise_server_exceptions=False)


class TestGetJobRouteReportsPlacement:
    def _get(self, processor: MagicMock, path: str) -> dict[str, Any]:
        patcher, client = _client(processor)
        with patcher, client as c:
            response = c.get(path, headers={})
            assert response.status_code == 200, response.text
            payload: dict[str, Any] = response.json()
            return payload

    def test_get_job_includes_the_node_and_its_instance_type(self, processor: MagicMock) -> None:
        processor.batch_v1.read_namespaced_job.return_value = _batch_job()
        pods = MagicMock()
        pods.items = [_pod("trainer-abc", "ip-10-0-1-7")]
        processor.core_v1.list_namespaced_pod.return_value = pods
        processor.core_v1.read_node.return_value = _node(_gpu_node_labels(CHEAPEST_MEMBER, "spot"))

        data = self._get(processor, "/api/v1/jobs/gco-jobs/trainer")

        assert data["scheduling"]["node_name"] == "ip-10-0-1-7"
        assert data["scheduling"]["node_instance_type"] == CHEAPEST_MEMBER
        assert data["scheduling"]["node_capacity_type"] == "spot"
        processor.core_v1.read_node.assert_called_once_with(name="ip-10-0-1-7")

    def test_the_pods_are_selected_by_the_job_name_label(self, processor: MagicMock) -> None:
        processor.batch_v1.read_namespaced_job.return_value = _batch_job()
        pods = MagicMock()
        pods.items = []
        processor.core_v1.list_namespaced_pod.return_value = pods

        self._get(processor, "/api/v1/jobs/gco-jobs/trainer")

        processor.core_v1.list_namespaced_pod.assert_called_once_with(
            namespace="gco-jobs", label_selector="job-name=trainer"
        )

    def test_a_refused_node_read_does_not_fail_the_job_read(self, processor: MagicMock) -> None:
        """RBAC not yet rolled out must degrade, not break `gco jobs get`."""
        processor.batch_v1.read_namespaced_job.return_value = _batch_job()
        pods = MagicMock()
        pods.items = [_pod("trainer-abc", "ip-10-0-1-7")]
        processor.core_v1.list_namespaced_pod.return_value = pods
        processor.core_v1.read_node.side_effect = RuntimeError("403 Forbidden")

        data = self._get(processor, "/api/v1/jobs/gco-jobs/trainer")

        assert data["metadata"]["name"] == "trainer"
        assert data["scheduling"]["node_name"] == "ip-10-0-1-7"
        assert data["scheduling"]["node_instance_type"] is None
        assert "403" in data["scheduling"]["node_lookup_error"]

    def test_a_failed_pod_listing_does_not_fail_the_job_read(self, processor: MagicMock) -> None:
        processor.batch_v1.read_namespaced_job.return_value = _batch_job()
        processor.core_v1.list_namespaced_pod.side_effect = RuntimeError("pods unavailable")

        data = self._get(processor, "/api/v1/jobs/gco-jobs/trainer")

        assert data["metadata"]["name"] == "trainer"
        assert data["scheduling"]["node_name"] is None
        assert "pods unavailable" in data["scheduling"]["node_lookup_error"]

    def test_a_completed_job_whose_pods_were_collected_reports_no_placement(
        self, processor: MagicMock
    ) -> None:
        processor.batch_v1.read_namespaced_job.return_value = _batch_job()
        pods = MagicMock()
        pods.items = []
        processor.core_v1.list_namespaced_pod.return_value = pods

        data = self._get(processor, "/api/v1/jobs/gco-jobs/trainer")

        assert data["scheduling"]["node_name"] is None
        assert data["scheduling"]["node_instance_type"] is None
        assert data["scheduling"]["node_lookup_error"] is None
        processor.core_v1.read_node.assert_not_called()

    def test_get_job_pods_attaches_hardware_to_each_pod(self, processor: MagicMock) -> None:
        pod = _pod("trainer-abc", "ip-10-0-1-7")
        pod.metadata.namespace = "gco-jobs"
        pod.metadata.labels = {"job-name": "trainer"}
        pod.metadata.uid = "pod-uid"
        container = MagicMock()
        container.name = "main"
        container.image = "trainer:1"
        pod.spec.containers = [container]
        pod.spec.init_containers = []
        pod.status.host_ip = "10.0.0.1"
        pod.status.pod_ip = "10.0.1.1"
        pod.status.start_time = datetime(2024, 1, 1, tzinfo=UTC)
        pod.status.container_statuses = []
        pod.status.init_container_statuses = []
        pods = MagicMock()
        pods.items = [pod]
        processor.core_v1.list_namespaced_pod.return_value = pods
        processor.core_v1.read_node.return_value = _node(_gpu_node_labels(EXPENSIVE_MEMBER))

        data = self._get(processor, "/api/v1/jobs/gco-jobs/trainer/pods")

        assert data["pods"][0]["node"]["instance_type"] == EXPENSIVE_MEMBER
        assert data["pods"][0]["node"]["name"] == "ip-10-0-1-7"
        assert data["scheduling"]["node_instance_type"] == EXPENSIVE_MEMBER

    def test_an_unscheduled_pod_gets_a_null_node_block(self, processor: MagicMock) -> None:
        pod = _pod("trainer-abc", None)
        pod.metadata.namespace = "gco-jobs"
        pod.metadata.labels = {"job-name": "trainer"}
        pod.metadata.uid = "pod-uid"
        pod.spec.containers = []
        pod.spec.init_containers = []
        pod.status.host_ip = None
        pod.status.pod_ip = None
        pod.status.start_time = None
        pod.status.container_statuses = []
        pod.status.init_container_statuses = []
        pods = MagicMock()
        pods.items = [pod]
        processor.core_v1.list_namespaced_pod.return_value = pods

        data = self._get(processor, "/api/v1/jobs/gco-jobs/trainer/pods")

        assert data["pods"][0]["node"] is None
        assert data["scheduling"]["unscheduled_pods"] == 1


# ---------------------------------------------------------------------------
# The CLI client
# ---------------------------------------------------------------------------


def _job_payload(scheduling: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "metadata": {
            "name": "trainer",
            "namespace": "gco-jobs",
            "creationTimestamp": "2024-01-01T00:00:00Z",
            "labels": {},
        },
        "spec": {
            "parallelism": 1,
            "completions": 1,
            "template": {"spec": {"containers": [{"name": "main", "image": "trainer:1"}]}},
        },
        "status": {"active": 1, "succeeded": 0, "failed": 0},
    }
    if scheduling is not None:
        payload["scheduling"] = scheduling
    return payload


def _scheduling(
    instance_type: str = CHEAPEST_MEMBER, capacity_type: str = "spot"
) -> dict[str, Any]:
    labels = {
        NODE_INSTANCE_TYPE_LABEL: instance_type,
        NODE_CAPACITY_TYPE_LABEL: capacity_type,
    }
    return {
        "node_name": "ip-10-0-1-7",
        "node_instance_type": instance_type,
        "node_capacity_type": capacity_type,
        "node_labels": labels,
        "nodes": [
            {
                "name": "ip-10-0-1-7",
                "instance_type": instance_type,
                "capacity_type": capacity_type,
                "labels": labels,
                "pods": [{"name": "trainer-abc", "phase": "Running"}],
            }
        ],
        "unscheduled_pods": 0,
        "node_lookup_error": None,
    }


@pytest.fixture
def manager() -> JobManager:
    with patch("cli.jobs.get_config"), patch("cli.jobs.get_aws_client"):
        mgr = JobManager()
    mgr._aws_client = MagicMock()
    mgr.config = MagicMock(default_region="us-east-1")
    return mgr


class TestJobInfoCarriesPlacement:
    def test_get_job_surfaces_the_instance_type_the_pod_landed_on(
        self, manager: JobManager
    ) -> None:
        manager._aws_client.get_job_details.return_value = _job_payload(_scheduling())

        job = manager.get_job("trainer", "gco-jobs", "us-east-1")

        assert job is not None
        assert job.node_name == "ip-10-0-1-7"
        assert job.node_instance_type == CHEAPEST_MEMBER
        assert job.node_capacity_type == "spot"
        assert job.node_labels[NODE_INSTANCE_TYPE_LABEL] == CHEAPEST_MEMBER
        assert job.nodes[0]["pods"] == [{"name": "trainer-abc", "phase": "Running"}]

    def test_a_run_on_a_non_cheapest_member_reports_that_member(self, manager: JobManager) -> None:
        """Neither direction is inferred from the plan; the label decides."""
        manager._aws_client.get_job_details.return_value = _job_payload(
            _scheduling(EXPENSIVE_MEMBER, "on-demand")
        )

        job = manager.get_job("trainer", "gco-jobs", "us-east-1")

        assert job is not None
        assert job.node_instance_type == EXPENSIVE_MEMBER
        assert job.node_capacity_type == "on-demand"

    def test_the_fields_are_top_level_in_the_serialized_payload(self, manager: JobManager) -> None:
        """The CLI's JSON output is the read surface consumers parse."""
        manager._aws_client.get_job_details.return_value = _job_payload(_scheduling())

        job = manager.get_job("trainer", "gco-jobs", "us-east-1")

        payload = asdict(job)  # type: ignore[arg-type]
        assert payload["node_instance_type"] == CHEAPEST_MEMBER
        assert payload["node_name"] == "ip-10-0-1-7"
        assert payload["node_capacity_type"] == "spot"
        assert payload["node_labels"][NODE_INSTANCE_TYPE_LABEL] == CHEAPEST_MEMBER
        # Round-trips through the CLI's --output json path unchanged.
        assert json.loads(json.dumps(payload, default=str))["node_instance_type"] == (
            CHEAPEST_MEMBER
        )

    def test_a_response_without_placement_leaves_the_fields_unset(
        self, manager: JobManager
    ) -> None:
        """An older regional bridge must not break the read, or invent a value."""
        manager._aws_client.get_job_details.return_value = _job_payload(None)

        job = manager.get_job("trainer", "gco-jobs", "us-east-1")

        assert job is not None
        assert job.name == "trainer"
        assert job.node_name is None
        assert job.node_instance_type is None
        assert job.node_capacity_type is None
        assert job.node_labels == {}
        assert job.nodes == []

    @pytest.mark.parametrize(
        "scheduling", [None, "not-a-dict", 7, [], {"nodes": "nope", "node_labels": 3}]
    )
    def test_a_malformed_placement_block_is_ignored_not_propagated(
        self, manager: JobManager, scheduling: Any
    ) -> None:
        payload = _job_payload()
        payload["scheduling"] = scheduling

        job = manager._parse_job_info(payload, "us-east-1")

        assert job.node_labels == {}
        assert job.nodes == []
        assert isinstance(job.node_labels, dict)
        assert isinstance(job.nodes, list)

    def test_placement_defaults_are_not_shared_between_instances(self) -> None:
        """Mutable defaults must be per-instance, or one job leaks into another."""
        first = JobInfo(name="a", namespace="n", region="r", status="running")
        second = JobInfo(name="b", namespace="n", region="r", status="running")

        first.node_labels["x"] = "y"
        first.nodes.append({"name": "n1"})

        assert second.node_labels == {}
        assert second.nodes == []

    def test_the_new_fields_are_all_optional(self) -> None:
        """JobInfo stays constructible from the four required fields."""
        job = JobInfo(name="a", namespace="n", region="r", status="pending")

        placement = {f.name for f in fields(job) if f.name.startswith(("node_", "nodes"))}
        assert placement == {
            "node_name",
            "node_instance_type",
            "node_capacity_type",
            "node_labels",
            "nodes",
        }


class TestListJobsStaysCheap:
    def test_the_list_endpoint_does_not_report_or_pay_for_placement(
        self, manager: JobManager
    ) -> None:
        """A Node read per job per region is not worth it on the list path."""
        manager._aws_client.get_jobs.return_value = {"jobs": [_job_payload(None)]}

        jobs = manager.list_jobs(region="us-east-1")

        assert len(jobs) == 1
        assert jobs[0].node_instance_type is None
        manager._aws_client.get_job_pods.assert_not_called()


class TestTrainJobPlacement:
    @staticmethod
    def _http_404() -> requests.exceptions.HTTPError:
        response = MagicMock()
        response.status_code = 404
        return requests.exceptions.HTTPError(response=response)

    @staticmethod
    def _trainjob_resource() -> dict[str, Any]:
        return {
            "resource": {
                "exists": True,
                "metadata": {
                    "name": "trainer",
                    "namespace": "gco-jobs",
                    "creationTimestamp": "2024-01-01T00:00:00Z",
                    "labels": {},
                },
                "spec": {"trainer": {"image": "trainer:1", "numNodes": 2}},
                "status": {"jobsStatus": [{"active": 2}]},
            }
        }

    def test_a_trainjob_reports_placement_from_its_child_jobs_pods(
        self, manager: JobManager
    ) -> None:
        manager._aws_client.get_job_details.side_effect = self._http_404()
        manager._aws_client.call_api.return_value = self._trainjob_resource()
        manager._aws_client.get_job_pods.return_value = {
            "pods": [],
            "scheduling": _scheduling(),
        }

        job = manager.get_job("trainer", "gco-jobs", "us-east-1")

        assert job is not None
        assert job.status == "running"
        assert job.node_instance_type == CHEAPEST_MEMBER
        # The torch runtime materializes a TrainJob as child Job '<name>-node-0'.
        manager._aws_client.get_job_pods.assert_called_once_with(
            "trainer-node-0", "gco-jobs", "us-east-1"
        )

    def test_a_trainjob_with_no_pods_yet_still_resolves(self, manager: JobManager) -> None:
        manager._aws_client.get_job_details.side_effect = self._http_404()
        manager._aws_client.call_api.return_value = self._trainjob_resource()
        manager._aws_client.get_job_pods.side_effect = RuntimeError("no pods found")

        job = manager.get_job("trainer", "gco-jobs", "us-east-1")

        assert job is not None
        assert job.name == "trainer"
        assert job.node_instance_type is None


class TestExtractScheduling:
    def test_it_copies_rather_than_aliasing_the_response(self) -> None:
        """A JobInfo must not share mutable state with the parsed response."""
        scheduling = _scheduling()
        extracted = _extract_scheduling({"scheduling": scheduling})

        extracted["node_labels"]["injected"] = "value"
        extracted["nodes"].append({"name": "extra"})

        assert "injected" not in scheduling["node_labels"]
        assert len(scheduling["nodes"]) == 1

    def test_the_returned_keys_match_the_jobinfo_fields(self) -> None:
        """Guards the ``**_extract_scheduling(...)`` splat against drift."""
        extracted = _extract_scheduling({"scheduling": _scheduling()})

        assert set(extracted) <= {f.name for f in fields(JobInfo)}


# ---------------------------------------------------------------------------
# Operator-visible output
# ---------------------------------------------------------------------------


def _invoke_cli(args: list[str], manager: MagicMock) -> Any:
    from click.testing import CliRunner

    from cli.main import cli

    with patch("cli.commands.jobs_cmd.get_job_manager", return_value=manager):
        return CliRunner().invoke(cli, args)


def _job_info(**overrides: Any) -> JobInfo:
    base: dict[str, Any] = {
        "name": "trainer",
        "namespace": "gco-jobs",
        "region": "us-east-1",
        "status": "running",
        "node_name": "ip-10-0-1-7",
        "node_instance_type": CHEAPEST_MEMBER,
        "node_capacity_type": "spot",
        "node_labels": {
            NODE_INSTANCE_TYPE_LABEL: CHEAPEST_MEMBER,
            NODE_CAPACITY_TYPE_LABEL: "spot",
        },
        "nodes": [
            {
                "name": "ip-10-0-1-7",
                "instance_type": CHEAPEST_MEMBER,
                "capacity_type": "spot",
                "labels": {},
                "pods": [{"name": "trainer-abc", "phase": "Running"}],
            }
        ],
    }
    base.update(overrides)
    return JobInfo(**base)


class TestJobsGetOutput:
    def test_the_table_shows_the_node_its_instance_type_and_capacity_type(self) -> None:
        manager = MagicMock()
        manager.get_job.return_value = _job_info()

        result = _invoke_cli(["jobs", "get", "trainer", "-r", "us-east-1"], manager)

        assert result.exit_code == 0, result.output
        assert "Placement" in result.output
        assert "ip-10-0-1-7" in result.output
        assert CHEAPEST_MEMBER in result.output
        assert "spot" in result.output

    def test_json_output_exposes_the_fields_for_machine_consumers(self) -> None:
        manager = MagicMock()
        manager.get_job.return_value = _job_info()

        result = _invoke_cli(
            ["--output", "json", "jobs", "get", "trainer", "-r", "us-east-1"], manager
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["node_instance_type"] == CHEAPEST_MEMBER
        assert payload["node_name"] == "ip-10-0-1-7"
        assert payload["node_capacity_type"] == "spot"
        assert payload["node_labels"][NODE_INSTANCE_TYPE_LABEL] == CHEAPEST_MEMBER
        # No placement block bleeds into machine-readable output.
        assert "Placement" not in result.output

    def test_an_unscheduled_job_prints_no_placement_block(self) -> None:
        manager = MagicMock()
        manager.get_job.return_value = _job_info(
            node_name=None,
            node_instance_type=None,
            node_capacity_type=None,
            node_labels={},
            nodes=[],
        )

        result = _invoke_cli(["jobs", "get", "trainer", "-r", "us-east-1"], manager)

        assert result.exit_code == 0, result.output
        assert "Placement" not in result.output

    def test_a_known_node_with_unknown_hardware_still_shows_the_node(self) -> None:
        """The RBAC-refused / node-reclaimed case renders '-', not a guess."""
        manager = MagicMock()
        manager.get_job.return_value = _job_info(
            node_instance_type=None,
            node_capacity_type=None,
            node_labels={},
            nodes=[
                {
                    "name": "ip-10-0-1-7",
                    "instance_type": None,
                    "capacity_type": None,
                    "labels": {},
                    "pods": [],
                }
            ],
        )

        result = _invoke_cli(["jobs", "get", "trainer", "-r", "us-east-1"], manager)

        assert result.exit_code == 0, result.output
        assert "ip-10-0-1-7" in result.output
        assert CHEAPEST_MEMBER not in result.output

    def test_a_non_dataclass_payload_is_printed_without_a_placement_block(self) -> None:
        """`gco jobs get` must survive a plain-dict job payload."""
        manager = MagicMock()
        manager.get_job.return_value = {"metadata": {"name": "trainer"}}

        result = _invoke_cli(["jobs", "get", "trainer", "-r", "us-east-1"], manager)

        assert result.exit_code == 0, result.output
        assert "Placement" not in result.output


class TestJobsPodsOutput:
    def test_the_pod_table_shows_each_pods_instance_type(self) -> None:
        manager = MagicMock()
        manager.get_job_pods.return_value = {
            "count": 1,
            "pods": [
                {
                    "metadata": {"name": "trainer-abc"},
                    "spec": {"nodeName": "ip-10-0-1-7"},
                    "status": {"phase": "Running", "containerStatuses": []},
                    "node": {
                        "name": "ip-10-0-1-7",
                        "instance_type": CHEAPEST_MEMBER,
                        "capacity_type": "spot",
                        "labels": {},
                    },
                }
            ],
        }

        result = _invoke_cli(["jobs", "pods", "trainer", "-r", "us-east-1"], manager)

        assert result.exit_code == 0, result.output
        assert "INSTANCE TYPE" in result.output
        assert CHEAPEST_MEMBER in result.output

    def test_a_pod_without_a_node_block_renders_a_dash(self) -> None:
        """Tolerates an older regional bridge that omits the field."""
        manager = MagicMock()
        manager.get_job_pods.return_value = {
            "count": 1,
            "pods": [
                {
                    "metadata": {"name": "trainer-abc"},
                    "spec": {},
                    "status": {"containerStatuses": [{"restartCount": 2}]},
                }
            ],
        }

        result = _invoke_cli(["jobs", "pods", "trainer", "-r", "us-east-1"], manager)

        assert result.exit_code == 0, result.output
        assert "Unknown" in result.output
        assert "INSTANCE TYPE" in result.output
