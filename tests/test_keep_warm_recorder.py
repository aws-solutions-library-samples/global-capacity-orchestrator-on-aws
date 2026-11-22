"""The health monitor's keep-warm idle recorder (gco.services.keep_warm_recorder).

Each pass looks at the ``gco.aws/keep-warm`` nodes and the pods bound to each,
rebuilds each node's idle gaps from those pods, records each gap on the Job
whose pod finished last before it, and closes a record when the next pod takes
the node (``reused``) or the node is gone (``reclaimed``). These tests drive
passes against fake Kubernetes clients that apply the merge patches the
recorder sends, so a sequence of passes reads back exactly what a caller of
Get Job would see.
"""

from __future__ import annotations

import json
import logging
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gco.keep_warm import IDLE_ANNOTATION, IDLE_OPEN_LABEL, KEEP_WARM_LABEL, load_idle_records
from gco.services import keep_warm_recorder as kwr

T0 = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _node(name: str = "node-a", *, keep_warm: bool = True) -> Any:
    labels = {
        "node.kubernetes.io/instance-type": "g6.xlarge",
        "karpenter.sh/capacity-type": "on-demand",
    }
    if keep_warm:
        labels[KEEP_WARM_LABEL] = "true"
    return types.SimpleNamespace(metadata=types.SimpleNamespace(name=name, labels=labels))


def _pod(
    name: str,
    *,
    node: str = "node-a",
    job: str | None = "job-a",
    job_uid: str = "uid-a",
    namespace: str = "gco-jobs",
    started: int | None = 0,
    finished: int | None = None,
    phase: str | None = None,
    owner_kind: str = "Job",
    annotations: dict[str, str] | None = None,
    container_finishes: bool = True,
    ready_time: int | None = None,
) -> Any:
    owners = []
    if job is not None:
        owners.append(types.SimpleNamespace(kind=owner_kind, name=job, uid=job_uid))
    conditions = []
    if started is not None:
        conditions.append(
            types.SimpleNamespace(
                type="PodScheduled", status="True", last_transition_time=_at(started)
            )
        )
    if ready_time is not None:
        conditions.append(
            types.SimpleNamespace(
                type="Ready", status="False", last_transition_time=_at(ready_time)
            )
        )
    container_statuses = []
    if finished is not None and container_finishes:
        container_statuses.append(
            types.SimpleNamespace(
                state=types.SimpleNamespace(
                    terminated=types.SimpleNamespace(finished_at=_at(finished))
                )
            )
        )
    status = types.SimpleNamespace(
        phase=phase or ("Succeeded" if finished is not None else "Running"),
        conditions=conditions,
        start_time=_at(started) if started is not None else None,
        container_statuses=container_statuses,
    )
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(
            name=name,
            namespace=namespace,
            annotations=annotations or {},
            owner_references=owners,
            creation_timestamp=_at(started or 0),
        ),
        spec=types.SimpleNamespace(node_name=node),
        status=status,
    )


def _job(name: str = "job-a", *, uid: str = "uid-a", namespace: str = "gco-jobs") -> Any:
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(
            name=name,
            namespace=namespace,
            uid=uid,
            annotations={},
            labels={},
            resource_version="1",
        )
    )


class ApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class FakeCluster:
    """Nodes, pods and Jobs, with the reads and the merge patch the recorder uses."""

    def __init__(self) -> None:
        self.nodes: list[Any] = []
        self.pods: list[Any] = []
        self.jobs: dict[tuple[str, str], Any] = {}
        self.patches: list[tuple[str, str, dict[str, Any], str]] = []
        self.read_errors: dict[tuple[str, str], int] = {}
        self.reads: list[tuple[str, str]] = []
        self.patch_errors: dict[tuple[str, str], int] = {}
        self.node_list_error: Exception | None = None

    # core_v1
    def list_node(self, *, label_selector: str, _request_timeout: int) -> Any:
        if self.node_list_error is not None:
            raise self.node_list_error
        key, _, value = label_selector.partition("=")
        return types.SimpleNamespace(
            items=[node for node in self.nodes if node.metadata.labels.get(key) == value]
        )

    def list_pod_for_all_namespaces(self, *, field_selector: str, _request_timeout: int) -> Any:
        node_name = field_selector.removeprefix("spec.nodeName=")
        return types.SimpleNamespace(
            items=[pod for pod in self.pods if pod.spec.node_name == node_name]
        )

    # batch_v1
    def list_job_for_all_namespaces(self, *, label_selector: str, _request_timeout: int) -> Any:
        key, _, value = label_selector.partition("=")
        return types.SimpleNamespace(
            items=[job for job in self.jobs.values() if job.metadata.labels.get(key) == value]
        )

    def read_namespaced_job(self, name: str, namespace: str, *, _request_timeout: int) -> Any:
        self.reads.append((namespace, name))
        if (namespace, name) in self.read_errors:
            raise ApiError(self.read_errors[(namespace, name)])
        if (namespace, name) not in self.jobs:
            raise ApiError(404)
        return self.jobs[(namespace, name)]

    def patch_namespaced_job(
        self,
        name: str,
        namespace: str,
        *,
        body: dict[str, Any],
        _content_type: str,
        _request_timeout: int,
    ) -> None:
        if (namespace, name) in self.patch_errors:
            raise ApiError(self.patch_errors[(namespace, name)])
        self.patches.append((namespace, name, body, _content_type))
        job = self.jobs[(namespace, name)]
        metadata = body["metadata"]
        assert metadata["resourceVersion"] == job.metadata.resource_version
        job.metadata.annotations.update(metadata["annotations"])
        for key, value in metadata["labels"].items():
            if value is None:
                job.metadata.labels.pop(key, None)
            else:
                job.metadata.labels[key] = value
        job.metadata.resource_version = str(int(job.metadata.resource_version) + 1)


class FakeElector:
    def __init__(self, held: bool = True) -> None:
        self.held = held
        self.released = False
        self.release_error: Exception | None = None

    def try_acquire(self, coordination_v1: Any) -> Any:
        return types.SimpleNamespace(held=self.held)

    def release(self, coordination_v1: Any) -> bool:
        if self.release_error is not None:
            raise self.release_error
        self.released = True
        return True


def _recorder(
    cluster: FakeCluster, *, now: int, elector: FakeElector | None = None
) -> kwr.KeepWarmRecorder:
    return kwr.KeepWarmRecorder(
        cluster,
        cluster,
        object(),
        elector=elector or FakeElector(),  # type: ignore[arg-type]
        window_seconds=600,
        job_namespaces={"gco-jobs"},
        request_timeout=7,
        clock=lambda: _at(now),
    )


def _records(cluster: FakeCluster, name: str = "job-a") -> list[dict[str, Any]]:
    return load_idle_records(cluster.jobs[("gco-jobs", name)].metadata.annotations)


def _idle_cluster() -> FakeCluster:
    """node-a, where job-a's pod finished at +100s and nothing runs now."""
    cluster = FakeCluster()
    cluster.nodes = [_node()]
    cluster.pods = [_pod("trial-a", finished=100)]
    cluster.jobs[("gco-jobs", "job-a")] = _job()
    return cluster


class TestOpening:
    def test_an_idle_node_opens_an_interval_on_the_job_that_left_it_idle(self) -> None:
        cluster = _idle_cluster()

        result = _recorder(cluster, now=130).run_once()

        assert result.held is True
        assert result.nodes == 1
        assert result.opened == ["node-a (gco-jobs/job-a)"]
        assert _records(cluster) == [
            {
                "node": "node-a",
                "instance_type": "g6.xlarge",
                "capacity_type": "on-demand",
                "pod": "trial-a",
                "since": "2026-11-21T18:01:40Z",
                "window_seconds": 600,
            }
        ]
        job = cluster.jobs[("gco-jobs", "job-a")]
        assert job.metadata.labels[IDLE_OPEN_LABEL] == "true"
        (namespace, name, body, content_type) = cluster.patches[0]
        assert (namespace, name, content_type) == (
            "gco-jobs",
            "job-a",
            "application/merge-patch+json",
        )
        assert body["metadata"]["resourceVersion"] == "1"

    def test_a_second_pass_opens_nothing_more(self) -> None:
        cluster = _idle_cluster()
        _recorder(cluster, now=130).run_once()

        result = _recorder(cluster, now=160).run_once()

        assert result.opened == []
        assert len(cluster.patches) == 1
        assert len(_records(cluster)) == 1

    def test_the_last_pod_to_finish_is_the_one_charged(self) -> None:
        cluster = _idle_cluster()
        cluster.pods.append(_pod("trial-b", job="job-b", job_uid="uid-b", finished=150))
        cluster.jobs[("gco-jobs", "job-b")] = _job("job-b", uid="uid-b")

        _recorder(cluster, now=170).run_once()

        assert _records(cluster) == []
        assert _records(cluster, "job-b")[0]["pod"] == "trial-b"

    def test_daemonset_and_mirror_pods_do_not_keep_a_node_busy(self) -> None:
        cluster = _idle_cluster()
        cluster.pods += [
            _pod("agent", job="cloudwatch-agent", owner_kind="DaemonSet", started=0),
            _pod("static", job=None, annotations={"kubernetes.io/config.mirror": "x"}),
        ]

        _recorder(cluster, now=130).run_once()

        assert len(_records(cluster)) == 1

    def test_a_running_pod_keeps_the_node_busy(self) -> None:
        cluster = _idle_cluster()
        cluster.pods.append(_pod("trial-b", job="job-b", started=90))

        result = _recorder(cluster, now=130).run_once()

        assert result.opened == []
        assert cluster.patches == []

    @pytest.mark.parametrize(
        "change",
        [
            lambda c: setattr(c.pods[0].metadata, "owner_references", []),
            lambda c: setattr(c.pods[0].metadata, "namespace", "elsewhere"),
            lambda c: c.jobs.clear(),
            lambda c: setattr(c.jobs[("gco-jobs", "job-a")].metadata, "uid", "another-uid"),
            lambda c: setattr(c, "pods", []),
        ],
        ids=["bare-pod", "other-namespace", "job-deleted", "job-recreated", "never-used"],
    )
    def test_no_job_to_charge_means_no_record(self, change: Any) -> None:
        cluster = _idle_cluster()
        change(cluster)

        result = _recorder(cluster, now=130).run_once()

        assert result.opened == []
        assert cluster.patches == []

    def test_an_unreadable_job_is_an_error_not_a_record(self) -> None:
        cluster = _idle_cluster()
        cluster.read_errors[("gco-jobs", "job-a")] = 500

        result = _recorder(cluster, now=130).run_once()

        assert result.errors == ["gco-jobs/job-a: 500"]
        assert cluster.patches == []

    def test_nodes_outside_the_pool_and_unnamed_nodes_are_ignored(self) -> None:
        cluster = _idle_cluster()
        cluster.nodes = [
            _node("node-a", keep_warm=False),
            types.SimpleNamespace(
                metadata=types.SimpleNamespace(name="", labels={KEEP_WARM_LABEL: "true"})
            ),
        ]

        result = _recorder(cluster, now=130).run_once()

        assert result.nodes == 0
        assert cluster.patches == []


class TestGapsBetweenPasses:
    """Gaps rebuilt from the pod list, however short, and each Job read once."""

    def test_a_gap_that_began_and_ended_between_passes_is_recorded_as_reused(self) -> None:
        cluster = _idle_cluster()
        # job-b's pod took the node 10 seconds after job-a's finished, and
        # is still running when the first pass looks.
        cluster.pods.append(_pod("trial-b", job="job-b", job_uid="uid-b", started=110))
        cluster.jobs[("gco-jobs", "job-b")] = _job("job-b", uid="uid-b")

        result = _recorder(cluster, now=130).run_once()

        assert result.opened == result.closed == ["node-a (gco-jobs/job-a)"]
        (entry,) = _records(cluster)
        assert (entry["since"], entry["until"], entry["seconds"], entry["ended_by"]) == (
            "2026-11-21T18:01:40Z",
            "2026-11-21T18:01:50Z",
            10,
            "reused",
        )
        assert IDLE_OPEN_LABEL not in cluster.jobs[("gco-jobs", "job-a")].metadata.labels
        assert _records(cluster, "job-b") == []

    def test_every_gap_goes_to_the_job_that_left_the_node_idle(self) -> None:
        cluster = FakeCluster()
        cluster.nodes = [_node()]
        # job-1's pod ran inside job-0's, so job-0 still held the node after it.
        spans = [(0, 100), (10, 50), (90, 120), (150, 200), (230, None)]
        for index, (start, finish) in enumerate(spans):
            name = f"job-{index}"
            cluster.pods.append(
                _pod(
                    f"trial-{index}",
                    job=name,
                    job_uid=f"uid-{index}",
                    started=start,
                    finished=finish,
                )
            )
            cluster.jobs[("gco-jobs", name)] = _job(name, uid=f"uid-{index}")

        _recorder(cluster, now=240).run_once()

        # job-0 finished while job-2 still ran, so job-2 left the node idle.
        assert _records(cluster, "job-0") == _records(cluster, "job-1") == []
        assert [entry["seconds"] for entry in _records(cluster, "job-2")] == [30]
        assert [entry["seconds"] for entry in _records(cluster, "job-3")] == [30]
        assert _records(cluster, "job-4") == []

    def test_a_recorded_gap_does_not_send_another_read(self) -> None:
        cluster = _idle_cluster()
        cluster.pods.append(
            _pod("trial-b", job="job-b", job_uid="uid-b", started=110, finished=120)
        )
        cluster.jobs[("gco-jobs", "job-b")] = _job("job-b", uid="uid-b")
        recorder = _recorder(cluster, now=130)

        recorder.run_once()
        reads = list(cluster.reads)
        recorder.run_once()

        # job-a's closed gap is not read again, and job-b's open one is found
        # through its label.
        assert reads == [("gco-jobs", "job-a"), ("gco-jobs", "job-b")]
        assert cluster.reads == reads

    def test_a_refused_read_is_warned_once_and_not_retried(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cluster = _idle_cluster()
        cluster.read_errors[("gco-jobs", "job-a")] = 403
        recorder = _recorder(cluster, now=130)

        with caplog.at_level(logging.WARNING, logger=kwr.__name__):
            first = recorder.run_once()
            second = recorder.run_once()

        assert first.errors == ["gco-jobs/job-a: 403"]
        assert second.errors == []
        assert cluster.reads == [("gco-jobs", "job-a")]
        assert caplog.text.count("may not read or patch Jobs in gco-jobs") == 1

    def test_a_failed_read_is_retried_on_the_next_pass_once_per_job(self) -> None:
        cluster = _idle_cluster()
        cluster.nodes.append(_node("node-b"))
        cluster.pods.append(_pod("trial-a2", node="node-b", finished=90))
        cluster.read_errors[("gco-jobs", "job-a")] = 500
        recorder = _recorder(cluster, now=130)

        recorder.run_once()
        assert cluster.reads == [("gco-jobs", "job-a")]
        del cluster.read_errors[("gco-jobs", "job-a")]
        result = recorder.run_once()

        assert sorted(result.opened) == ["node-a (gco-jobs/job-a)", "node-b (gco-jobs/job-a)"]
        assert len(_records(cluster)) == 2

    def test_an_open_job_from_an_earlier_incarnation_is_not_charged(self) -> None:
        cluster = _idle_cluster()
        _recorder(cluster, now=130).run_once()
        # A Job of the same name replaced it; the listed pods are the old one's.
        cluster.jobs[("gco-jobs", "job-a")].metadata.uid = "uid-new"
        cluster.pods.append(_pod("trial-c", started=170, finished=180))

        result = _recorder(cluster, now=190).run_once()

        assert result.opened == []
        assert result.closed == ["node-a (gco-jobs/job-a)"]

    def test_the_settled_gaps_shrink_with_the_pod_lists(self) -> None:
        cluster = _idle_cluster()
        recorder = _recorder(cluster, now=130)

        recorder.run_once()
        assert len(recorder._settled) == 1
        cluster.pods = []
        recorder.run_once()

        assert recorder._settled == set()


class TestClosing:
    def test_the_next_pod_closes_the_interval_as_reused(self) -> None:
        cluster = _idle_cluster()
        _recorder(cluster, now=130).run_once()
        cluster.pods.append(_pod("trial-b", job="job-b", job_uid="uid-b", started=170))

        result = _recorder(cluster, now=190).run_once()

        assert result.closed == ["node-a (gco-jobs/job-a)"]
        (entry,) = _records(cluster)
        assert entry["until"] == "2026-11-21T18:02:50Z"
        assert entry["seconds"] == 70
        assert entry["ended_by"] == "reused"
        assert IDLE_OPEN_LABEL not in cluster.jobs[("gco-jobs", "job-a")].metadata.labels

    def test_a_pod_that_ran_between_passes_closes_one_interval_and_opens_the_next(self) -> None:
        cluster = _idle_cluster()
        _recorder(cluster, now=130).run_once()
        cluster.pods.append(
            _pod("trial-b", job="job-b", job_uid="uid-b", started=140, finished=150)
        )
        cluster.jobs[("gco-jobs", "job-b")] = _job("job-b", uid="uid-b")

        result = _recorder(cluster, now=160).run_once()

        assert result.closed == ["node-a (gco-jobs/job-a)"]
        assert result.opened == ["node-a (gco-jobs/job-b)"]
        assert _records(cluster)[0]["seconds"] == 40
        assert _records(cluster, "job-b")[0]["since"] == "2026-11-21T18:02:30Z"

    def test_a_node_that_is_gone_closes_the_interval_as_reclaimed(self) -> None:
        cluster = _idle_cluster()
        _recorder(cluster, now=130).run_once()
        cluster.nodes, cluster.pods = [], []

        result = _recorder(cluster, now=730).run_once()

        assert result.closed == ["node-a (gco-jobs/job-a)"]
        (entry,) = _records(cluster)
        assert (entry["until"], entry["seconds"], entry["ended_by"]) == (
            "2026-11-21T18:12:10Z",
            630,
            "reclaimed",
        )

    def test_an_open_interval_is_found_again_by_a_new_leader(self) -> None:
        cluster = _idle_cluster()
        _recorder(cluster, now=130).run_once()
        cluster.nodes, cluster.pods = [], []

        # A fresh recorder knows nothing in memory; the open label leads it back.
        _recorder(cluster, now=760).run_once()

        assert _records(cluster)[0]["ended_by"] == "reclaimed"

    def test_the_predecessor_pod_itself_never_closes_its_interval(self) -> None:
        cluster = _idle_cluster()
        # A pod that started and finished in the same second.
        cluster.pods = [_pod("trial-a", started=100, finished=100)]
        _recorder(cluster, now=130).run_once()

        result = _recorder(cluster, now=160).run_once()

        assert result.closed == []
        assert "until" not in _records(cluster)[0]

    def test_a_clock_behind_the_interval_start_closes_it_at_zero_seconds(self) -> None:
        cluster = _idle_cluster()
        _recorder(cluster, now=130).run_once()
        cluster.nodes, cluster.pods = [], []

        _recorder(cluster, now=50).run_once()

        (entry,) = _records(cluster)
        assert entry["seconds"] == 0
        assert entry["until"] == entry["since"]

    def test_open_jobs_outside_the_job_namespaces_are_not_touched(self) -> None:
        cluster = _idle_cluster()
        cluster.pods = []
        outside = _job("job-x", namespace="elsewhere")
        outside.metadata.labels[IDLE_OPEN_LABEL] = "true"
        cluster.jobs[("elsewhere", "job-x")] = outside

        result = _recorder(cluster, now=130).run_once()

        assert result.closed == []
        assert cluster.patches == []

    def test_closed_intervals_are_left_alone(self) -> None:
        cluster = _idle_cluster()
        _recorder(cluster, now=130).run_once()
        cluster.nodes, cluster.pods = [], []
        _recorder(cluster, now=730).run_once()
        job = cluster.jobs[("gco-jobs", "job-a")]
        job.metadata.labels[IDLE_OPEN_LABEL] = "true"  # a stale label, say

        result = _recorder(cluster, now=800).run_once()

        assert result.closed == []


class TestPodFacts:
    def test_a_failed_pod_without_container_finishes_ends_at_its_ready_change(self) -> None:
        pod = _pod("p", finished=200, phase="Failed", container_finishes=False, ready_time=180)
        facts = kwr.pod_facts(pod)
        assert facts is not None
        assert facts.finished_at == _at(180)

    def test_without_a_ready_change_it_ends_where_it_started(self) -> None:
        pod = _pod("p", started=40, finished=200, phase="Failed", container_finishes=False)
        facts = kwr.pod_facts(pod)
        assert facts is not None
        assert facts.finished_at == _at(40)

    def test_start_falls_back_to_start_time_then_creation(self) -> None:
        pod = _pod("p", started=None)
        pod.status.start_time = _at(5)
        assert kwr.pod_facts(pod).started_at == _at(5)  # type: ignore[union-attr]
        pod.status.start_time = None
        # A naive time is read as UTC, as the Kubernetes client never sends one.
        pod.metadata.creation_timestamp = datetime(2026, 11, 21, 18, 0, 3)  # noqa: DTZ001
        assert kwr.pod_facts(pod).started_at == _at(3)  # type: ignore[union-attr]

    def test_a_pod_without_metadata_is_ignored(self) -> None:
        assert kwr.pod_facts(types.SimpleNamespace(metadata=None)) is None

    def test_the_first_job_owner_counts_and_other_owner_kinds_are_skipped(self) -> None:
        pod = _pod("p")
        pod.metadata.owner_references = [
            types.SimpleNamespace(kind="Workload", name="w", uid="uid-w"),
            types.SimpleNamespace(kind="Job", name="first", uid="uid-1"),
            types.SimpleNamespace(kind="Job", name="second", uid="uid-2"),
        ]
        facts = kwr.pod_facts(pod)
        assert facts is not None
        assert (facts.job, facts.job_uid) == ("first", "uid-1")

    def test_an_owner_without_a_name_is_no_job(self) -> None:
        pod = _pod("p")
        pod.metadata.owner_references = [types.SimpleNamespace(kind="Job", name="", uid="")]
        facts = kwr.pod_facts(pod)
        assert facts is not None
        assert (facts.job, facts.job_uid) == (None, None)

    def test_a_node_without_finished_pods_has_no_gap(self) -> None:
        observation = kwr.observe_node(_node(), [])
        assert observation.busy is False
        assert observation.idle_gaps() == []


class TestWrites:
    def test_a_refused_patch_is_reported_and_warned_once_per_namespace(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cluster = _idle_cluster()
        cluster.patch_errors[("gco-jobs", "job-a")] = 403
        recorder = _recorder(cluster, now=130)

        with caplog.at_level(logging.WARNING, logger=kwr.__name__):
            first = recorder.run_once()
            # A refusal is for good: the next pass does not try the same gap again.
            second = recorder.run_once()
            recorder._settled.clear()
            third = recorder.run_once()

        assert first.errors == third.errors == ["gco-jobs/job-a: 403"]
        assert second.errors == []
        assert caplog.text.count("may not read or patch Jobs in gco-jobs") == 1

    def test_a_job_deleted_before_the_patch_is_not_tried_again(self) -> None:
        cluster = _idle_cluster()
        cluster.patch_errors[("gco-jobs", "job-a")] = 404
        recorder = _recorder(cluster, now=130)

        assert recorder.run_once().errors == ["gco-jobs/job-a: 404"]
        assert recorder.run_once().errors == []
        assert cluster.reads == [("gco-jobs", "job-a")]

    def test_a_conflicting_patch_is_reported_without_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cluster = _idle_cluster()
        cluster.patch_errors[("gco-jobs", "job-a")] = 409

        with caplog.at_level(logging.WARNING, logger=kwr.__name__):
            result = _recorder(cluster, now=130).run_once()

        assert result.errors == ["gco-jobs/job-a: 409"]
        assert "may not read or patch" not in caplog.text

    def test_the_record_is_valid_json_in_the_annotation(self) -> None:
        cluster = _idle_cluster()
        _recorder(cluster, now=130).run_once()
        raw = cluster.jobs[("gco-jobs", "job-a")].metadata.annotations[IDLE_ANNOTATION]
        assert json.loads(raw)[0]["node"] == "node-a"


class TestElectionAndFailures:
    def test_a_replica_without_the_lease_reads_and_writes_nothing(self) -> None:
        cluster = _idle_cluster()
        cluster.node_list_error = AssertionError("must not list")

        result = _recorder(cluster, now=130, elector=FakeElector(held=False)).run_once()

        assert result.held is False
        assert result.errors == []

    def test_a_failed_pass_is_reported_and_never_raises(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cluster = _idle_cluster()
        cluster.node_list_error = ApiError(500)

        with caplog.at_level(logging.WARNING, logger=kwr.__name__):
            result = _recorder(cluster, now=130).run_once()

        assert result.errors == ["ApiError: HTTP 500"]
        assert "Keep-warm idle recording failed" in caplog.text

    def test_opened_and_closed_intervals_are_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        cluster = _idle_cluster()
        with caplog.at_level(logging.INFO, logger=kwr.__name__):
            _recorder(cluster, now=130).run_once()
        assert "opened on node-a (gco-jobs/job-a), closed on none" in caplog.text

    def test_release_hands_the_lease_over(self) -> None:
        elector = FakeElector()
        assert _recorder(FakeCluster(), now=0, elector=elector).release() is True
        assert elector.released is True

    def test_a_failed_release_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        elector = FakeElector()
        elector.release_error = ApiError(500)
        with caplog.at_level(logging.WARNING, logger=kwr.__name__):
            assert _recorder(FakeCluster(), now=0, elector=elector).release() is False
        assert "Could not release the keep-warm Lease" in caplog.text


class TestHealthMonitorWiring:
    """The health monitor builds the recorder from its environment and runs it each loop."""

    @staticmethod
    def _monitor(env: dict[str, str]) -> Any:
        from unittest.mock import patch

        from kubernetes import config as k8s_config

        from gco.models import ResourceThresholds
        from gco.services.health_monitor import HealthMonitor

        with (
            patch("gco.services.health_monitor.config") as mock_config,
            patch("gco.services.health_monitor.client"),
            patch.dict("os.environ", env, clear=True),
        ):
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.return_value = None
            return HealthMonitor(
                "c1", "us-east-1", ResourceThresholds(80, 80, 60), job_namespaces={"gco-jobs"}
            )

    def test_the_recorder_uses_its_own_lease_and_the_configured_window(self) -> None:
        monitor = self._monitor(
            {
                "POD_NAME": "health-monitor-0",
                "POD_NAMESPACE": "gco-system",
                "KEEP_WARM_LEASE_NAME": "gco-health-monitor-keep-warm",
                "KEEP_WARM_LEASE_DURATION": "120",
                "GPU_KEEP_WARM_WINDOW_SECONDS": "900",
            }
        )
        recorder = monitor.keep_warm_recorder
        identity = recorder.elector.identity
        assert (identity.name, identity.namespace, identity.holder, identity.duration_seconds) == (
            "gco-health-monitor-keep-warm",
            "gco-system",
            "health-monitor-0",
            120,
        )
        assert recorder.elector.label == "keep-warm"
        assert recorder.window_seconds == 900
        assert recorder.job_namespaces == frozenset({"gco-jobs"})
        assert recorder.batch_v1 is monitor.batch_v1

    def test_the_defaults_without_an_environment(self) -> None:
        recorder = self._monitor({}).keep_warm_recorder
        assert recorder.elector.identity.name == "gco-health-monitor-keep-warm"
        assert recorder.window_seconds == 600

    @pytest.mark.parametrize("raw", ["ten minutes", "0", "-30"])
    def test_an_unusable_window_falls_back_to_the_default(
        self, raw: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        from unittest.mock import patch

        from gco.services.health_monitor import _keep_warm_window_seconds_from_env

        with (
            patch.dict("os.environ", {"GPU_KEEP_WARM_WINDOW_SECONDS": raw}, clear=True),
            caplog.at_level(logging.WARNING),
        ):
            assert _keep_warm_window_seconds_from_env() == 600
        assert ("is not an integer" in caplog.text) is (raw == "ten minutes")

    @pytest.mark.asyncio
    async def test_record_keep_warm_idle_runs_a_pass_off_the_event_loop(self) -> None:
        import threading

        monitor = self._monitor({})
        seen: dict[str, Any] = {}

        def run_once() -> kwr.RecorderPass:
            seen["thread"] = threading.current_thread()
            return kwr.RecorderPass(held=True)

        monitor.keep_warm_recorder.run_once = run_once
        result = await monitor.record_keep_warm_idle()

        assert result.held is True
        assert seen["thread"] is not threading.main_thread()

    @pytest.mark.asyncio
    async def test_the_health_loop_records_after_the_alb_sync(self) -> None:
        import asyncio
        import contextlib
        from unittest.mock import AsyncMock, MagicMock

        import gco.services.health_api as health_api

        calls: list[str] = []
        monitor = MagicMock()
        monitor.get_health_status = AsyncMock(return_value=MagicMock(status="healthy"))
        monitor.sync_alb_registration = AsyncMock(side_effect=lambda: calls.append("alb"))
        monitor.record_keep_warm_idle = AsyncMock(side_effect=lambda: calls.append("keep-warm"))
        original = health_api.health_monitor
        original_metrics = health_api.health_metrics
        health_api.health_monitor = monitor
        health_api.health_metrics = None
        try:
            task = asyncio.create_task(health_api.background_health_monitor())
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            health_api.health_monitor = original
            health_api.health_metrics = original_metrics
        assert calls[:2] == ["alb", "keep-warm"]

    def test_shutdown_releases_the_keep_warm_lease(self) -> None:
        from unittest.mock import MagicMock, patch

        from fastapi.testclient import TestClient

        import gco.services.health_api as health_api

        monitor = MagicMock(cluster_id="c1", region="us-east-1")
        with (
            patch.object(health_api, "create_health_monitor_from_env", return_value=monitor),
            patch.object(health_api, "HealthMonitorMetrics"),
            patch.object(health_api, "create_webhook_dispatcher_from_env"),
            TestClient(health_api.app),
        ):
            pass
        monitor.keep_warm_recorder.release.assert_called_once_with()
