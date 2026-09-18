"""
Tests for shared API helpers in gco/services/api_shared.py.

Covers the processor readiness guard (503 when unset) and namespace
allowlist check (403 on deny), the Kubernetes-to-dict converters for
Jobs, Pods, and Events — including computed_status derivation across
pending/running/succeeded/failed states and container state mapping —
and the lightweight {{ var }} template parameter substitution used by
the manifest submission path.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from gco.services.api_shared import (
    _apply_template_parameters,
    _check_namespace,
    _check_processor,
    _parse_event_to_dict,
    _parse_job_to_dict,
    _parse_pod_to_dict,
)

# ---------------------------------------------------------------------------
# _check_processor
# ---------------------------------------------------------------------------


class TestCheckProcessor:
    """Tests for _check_processor helper."""

    def test_returns_processor_when_set(self):
        import gco.services.manifest_api as manifest_api_module

        mock_proc = MagicMock()
        original = manifest_api_module.manifest_processor
        try:
            manifest_api_module.manifest_processor = mock_proc
            result = _check_processor()
            assert result is mock_proc
        finally:
            manifest_api_module.manifest_processor = original

    def test_raises_503_when_none(self):
        import gco.services.manifest_api as manifest_api_module

        original = manifest_api_module.manifest_processor
        try:
            manifest_api_module.manifest_processor = None
            with pytest.raises(HTTPException) as exc_info:
                _check_processor()
            assert exc_info.value.status_code == 503
            assert "not initialized" in exc_info.value.detail
        finally:
            manifest_api_module.manifest_processor = original


# ---------------------------------------------------------------------------
# _check_namespace
# ---------------------------------------------------------------------------


class TestCheckNamespace:
    """Tests for _check_namespace helper."""

    def _make_processor(self):
        proc = MagicMock()
        proc.allowed_namespaces = {"default", "gco-jobs"}
        return proc

    def test_allowed_namespace_passes(self):
        proc = self._make_processor()
        # Should not raise
        _check_namespace("default", proc)
        _check_namespace("gco-jobs", proc)

    def test_disallowed_namespace_raises_403(self):
        proc = self._make_processor()
        with pytest.raises(HTTPException) as exc_info:
            _check_namespace("kube-system", proc)
        assert exc_info.value.status_code == 403
        assert "kube-system" in exc_info.value.detail


# ---------------------------------------------------------------------------
# _parse_job_to_dict
# ---------------------------------------------------------------------------


class TestParseJobToDict:
    """Tests for _parse_job_to_dict helper."""

    def _make_job(
        self,
        active=0,
        succeeded=0,
        failed=0,
        conditions=None,
        start_time=None,
        completion_time=None,
        containers=None,
        init_containers=None,
    ):
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)

        job = MagicMock()
        job.metadata.name = "test-job"
        job.metadata.namespace = "gco-jobs"
        job.metadata.creation_timestamp = ts
        job.metadata.labels = {"app": "worker"}
        job.metadata.annotations = {"note": "test"}
        job.metadata.uid = "uid-1234"

        job.status.active = active
        job.status.succeeded = succeeded
        job.status.failed = failed
        job.status.start_time = start_time
        job.status.completion_time = completion_time
        job.status.conditions = conditions

        job.spec.parallelism = 1
        job.spec.completions = 1
        job.spec.backoff_limit = 6

        # The pod template carries the container image refs that the
        # orphan-image cross-reference reads through ``image_refs`` on
        # the JobInfo dataclass. Default to a single container so
        # existing tests continue to exercise a realistic shape.
        if containers is None:
            container = MagicMock()
            container.name = "main"
            container.image = "registry/test-job:v1"
            containers = [container]
        if init_containers is None:
            init_containers = []
        job.spec.template.spec.containers = containers
        job.spec.template.spec.init_containers = init_containers

        return job

    def _make_condition(self, ctype, cstatus):
        cond = MagicMock()
        cond.type = ctype
        cond.status = cstatus
        cond.reason = "SomeReason"
        cond.message = "some message"
        cond.last_transition_time = datetime(2024, 1, 15, 10, 5, 0, tzinfo=UTC)
        return cond

    def test_pending_status(self):
        job = self._make_job(active=0, conditions=[])
        result = _parse_job_to_dict(job)
        assert result["computed_status"] == "pending"

    def test_running_status(self):
        job = self._make_job(active=1, conditions=[])
        result = _parse_job_to_dict(job)
        assert result["computed_status"] == "running"

    def test_succeeded_status(self):
        cond = self._make_condition("Complete", "True")
        job = self._make_job(succeeded=1, conditions=[cond])
        result = _parse_job_to_dict(job)
        assert result["computed_status"] == "succeeded"

    def test_failed_status(self):
        cond = self._make_condition("Failed", "True")
        job = self._make_job(failed=1, conditions=[cond])
        result = _parse_job_to_dict(job)
        assert result["computed_status"] == "failed"

    def test_metadata_fields(self):
        job = self._make_job(conditions=[])
        result = _parse_job_to_dict(job)
        meta = result["metadata"]
        assert meta["name"] == "test-job"
        assert meta["namespace"] == "gco-jobs"
        assert meta["labels"] == {"app": "worker"}
        assert meta["annotations"] == {"note": "test"}
        assert meta["uid"] == "uid-1234"
        assert "2024-01-15" in meta["creationTimestamp"]

    def test_spec_fields(self):
        job = self._make_job(conditions=[])
        result = _parse_job_to_dict(job)
        spec = result["spec"]
        assert spec["parallelism"] == 1
        assert spec["completions"] == 1
        assert spec["backoffLimit"] == 6

    def test_status_fields_with_times(self):
        st = datetime(2024, 1, 15, 10, 1, 0, tzinfo=UTC)
        ct = datetime(2024, 1, 15, 10, 5, 0, tzinfo=UTC)
        job = self._make_job(succeeded=1, conditions=[], start_time=st, completion_time=ct)
        result = _parse_job_to_dict(job)
        assert result["status"]["startTime"] == "2024-01-15T10:01:00+00:00"
        assert result["status"]["completionTime"] == "2024-01-15T10:05:00+00:00"

    def test_status_defaults_zero(self):
        job = self._make_job(active=None, succeeded=None, failed=None, conditions=[])
        result = _parse_job_to_dict(job)
        assert result["status"]["active"] == 0
        assert result["status"]["succeeded"] == 0
        assert result["status"]["failed"] == 0

    def test_conditions_serialized(self):
        cond = self._make_condition("Complete", "True")
        job = self._make_job(conditions=[cond])
        result = _parse_job_to_dict(job)
        conds = result["status"]["conditions"]
        assert len(conds) == 1
        assert conds[0]["type"] == "Complete"
        assert conds[0]["status"] == "True"
        assert conds[0]["lastTransitionTime"] is not None

    def test_template_carries_container_image_refs(self):
        c1 = MagicMock()
        c1.name = "trainer"
        c1.image = "registry/train:v2"
        c2 = MagicMock()
        c2.name = "sidecar"
        c2.image = "registry/aux:v1"

        init = MagicMock()
        init.name = "data-prep"
        init.image = "registry/init:v3"

        job = self._make_job(conditions=[], containers=[c1, c2], init_containers=[init])
        result = _parse_job_to_dict(job)

        template_spec = result["spec"]["template"]["spec"]
        assert {c["name"] for c in template_spec["containers"]} == {"trainer", "sidecar"}
        assert {c["image"] for c in template_spec["containers"]} == {
            "registry/train:v2",
            "registry/aux:v1",
        }
        assert template_spec["initContainers"] == [
            {"name": "data-prep", "image": "registry/init:v3"}
        ]

    def test_template_handles_missing_containers_lists(self):
        """Older serialised Jobs may lack ``containers`` / ``initContainers``
        attributes entirely. The helper coerces missing collections to
        empty lists rather than blowing up — the parse path must not
        regress when reading historical Job objects."""
        job = self._make_job(conditions=[])
        # Force the container list attributes to None to simulate a
        # malformed kubernetes-client response.
        job.spec.template.spec.containers = None
        job.spec.template.spec.init_containers = None

        result = _parse_job_to_dict(job)

        template_spec = result["spec"]["template"]["spec"]
        assert template_spec["containers"] == []
        assert template_spec["initContainers"] == []


# ---------------------------------------------------------------------------
# _parse_pod_to_dict
# ---------------------------------------------------------------------------


class TestParsePodToDict:
    """Tests for _parse_pod_to_dict helper."""

    def _make_pod(self, container_statuses=None, init_container_statuses=None):
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)

        pod = MagicMock()
        pod.metadata.name = "test-pod-abc12"
        pod.metadata.namespace = "gco-jobs"
        pod.metadata.creation_timestamp = ts
        pod.metadata.labels = {"app": "worker"}
        pod.metadata.uid = "pod-uid-5678"

        pod.status.phase = "Running"
        pod.status.host_ip = "10.0.0.1"
        pod.status.pod_ip = "10.0.1.5"
        pod.status.start_time = ts
        pod.status.container_statuses = container_statuses
        pod.status.init_container_statuses = init_container_statuses

        pod.spec.node_name = "ip-10-0-0-1.ec2.internal"
        container = MagicMock()
        container.name = "worker"
        container.image = "public.ecr.aws/test/worker:v1"
        pod.spec.containers = [container]
        pod.spec.init_containers = []

        return pod

    def _make_container_status_running(self):
        cs = MagicMock()
        cs.name = "worker"
        cs.ready = True
        cs.restart_count = 0
        cs.image = "public.ecr.aws/test/worker:v1"
        cs.state.running.started_at = datetime(2024, 1, 15, 10, 0, 5, tzinfo=UTC)
        cs.state.waiting = None
        cs.state.terminated = None
        return cs

    def _make_container_status_waiting(self):
        cs = MagicMock()
        cs.name = "worker"
        cs.ready = False
        cs.restart_count = 0
        cs.image = "public.ecr.aws/test/worker:v1"
        cs.state.running = None
        cs.state.waiting.reason = "ContainerCreating"
        cs.state.terminated = None
        return cs

    def _make_container_status_terminated(self):
        cs = MagicMock()
        cs.name = "worker"
        cs.ready = False
        cs.restart_count = 1
        cs.image = "public.ecr.aws/test/worker:v1"
        cs.state.running = None
        cs.state.waiting = None
        cs.state.terminated.exit_code = 0
        cs.state.terminated.reason = "Completed"
        return cs

    def test_running_container(self):
        cs = self._make_container_status_running()
        pod = self._make_pod(container_statuses=[cs])
        result = _parse_pod_to_dict(pod)
        statuses = result["status"]["containerStatuses"]
        assert len(statuses) == 1
        assert statuses[0]["state"] == "running"
        assert statuses[0]["startedAt"] is not None

    def test_waiting_container(self):
        cs = self._make_container_status_waiting()
        pod = self._make_pod(container_statuses=[cs])
        result = _parse_pod_to_dict(pod)
        statuses = result["status"]["containerStatuses"]
        assert statuses[0]["state"] == "waiting"
        assert statuses[0]["reason"] == "ContainerCreating"

    def test_terminated_container(self):
        cs = self._make_container_status_terminated()
        pod = self._make_pod(container_statuses=[cs])
        result = _parse_pod_to_dict(pod)
        statuses = result["status"]["containerStatuses"]
        assert statuses[0]["state"] == "terminated"
        assert statuses[0]["exitCode"] == 0
        assert statuses[0]["reason"] == "Completed"

    def test_metadata_fields(self):
        pod = self._make_pod(container_statuses=[])
        result = _parse_pod_to_dict(pod)
        meta = result["metadata"]
        assert meta["name"] == "test-pod-abc12"
        assert meta["namespace"] == "gco-jobs"
        assert meta["uid"] == "pod-uid-5678"

    def test_spec_fields(self):
        pod = self._make_pod(container_statuses=[])
        result = _parse_pod_to_dict(pod)
        spec = result["spec"]
        assert spec["nodeName"] == "ip-10-0-0-1.ec2.internal"
        assert len(spec["containers"]) == 1
        assert spec["containers"][0]["name"] == "worker"

    def test_no_container_statuses(self):
        pod = self._make_pod(container_statuses=None)
        result = _parse_pod_to_dict(pod)
        assert result["status"]["containerStatuses"] == []

    def test_init_container_statuses(self):
        init_cs = MagicMock()
        init_cs.name = "init-setup"
        init_cs.ready = True
        init_cs.restart_count = 0
        pod = self._make_pod(container_statuses=[], init_container_statuses=[init_cs])
        result = _parse_pod_to_dict(pod)
        init_statuses = result["status"]["initContainerStatuses"]
        assert len(init_statuses) == 1
        assert init_statuses[0]["name"] == "init-setup"


# ---------------------------------------------------------------------------
# _parse_event_to_dict
# ---------------------------------------------------------------------------


class TestParseEventToDict:
    """Tests for _parse_event_to_dict helper."""

    def _make_event(self):
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        event = MagicMock()
        event.type = "Normal"
        event.reason = "Scheduled"
        event.message = "Successfully assigned pod"
        event.count = 3
        event.first_timestamp = ts
        event.last_timestamp = ts
        event.source.component = "default-scheduler"
        event.source.host = "node-1"
        event.involved_object.kind = "Pod"
        event.involved_object.name = "test-pod"
        event.involved_object.namespace = "gco-jobs"
        return event

    def test_basic_fields(self):
        event = self._make_event()
        result = _parse_event_to_dict(event)
        assert result["type"] == "Normal"
        assert result["reason"] == "Scheduled"
        assert result["message"] == "Successfully assigned pod"
        assert result["count"] == 3

    def test_timestamps(self):
        event = self._make_event()
        result = _parse_event_to_dict(event)
        assert result["firstTimestamp"] == "2024-01-15T10:00:00+00:00"
        assert result["lastTimestamp"] == "2024-01-15T10:00:00+00:00"

    def test_source(self):
        event = self._make_event()
        result = _parse_event_to_dict(event)
        assert result["source"]["component"] == "default-scheduler"
        assert result["source"]["host"] == "node-1"

    def test_involved_object(self):
        event = self._make_event()
        result = _parse_event_to_dict(event)
        assert result["involvedObject"]["kind"] == "Pod"
        assert result["involvedObject"]["name"] == "test-pod"
        assert result["involvedObject"]["namespace"] == "gco-jobs"

    def test_null_count_defaults_to_one(self):
        event = self._make_event()
        event.count = None
        result = _parse_event_to_dict(event)
        assert result["count"] == 1

    def test_null_timestamps(self):
        event = self._make_event()
        event.first_timestamp = None
        event.last_timestamp = None
        result = _parse_event_to_dict(event)
        assert result["firstTimestamp"] is None
        assert result["lastTimestamp"] is None


# ---------------------------------------------------------------------------
# _apply_template_parameters
# ---------------------------------------------------------------------------


class TestApplyTemplateParameters:
    """Tests for _apply_template_parameters helper."""

    def test_basic_substitution(self):
        manifest = {"metadata": {"name": "{{ job_name }}"}}
        result = _apply_template_parameters(manifest, {"job_name": "my-job"})
        assert result["metadata"]["name"] == "my-job"

    def test_multiple_parameters(self):
        manifest = {
            "metadata": {"name": "{{ name }}", "namespace": "{{ ns }}"},
            "spec": {"image": "{{ image }}"},
        }
        params = {"name": "my-job", "ns": "gco-jobs", "image": "nginx:latest"}
        result = _apply_template_parameters(manifest, params)
        assert result["metadata"]["name"] == "my-job"
        assert result["metadata"]["namespace"] == "gco-jobs"
        assert result["spec"]["image"] == "nginx:latest"

    def test_no_match_leaves_template(self):
        manifest = {"metadata": {"name": "{{ job_name }}"}}
        result = _apply_template_parameters(manifest, {"other_key": "value"})
        assert result["metadata"]["name"] == "{{ job_name }}"

    def test_whitespace_variants(self):
        manifest = {"a": "{{key}}", "b": "{{  key  }}"}
        result = _apply_template_parameters(manifest, {"key": "val"})
        assert result["a"] == "val"
        assert result["b"] == "val"

    def test_numeric_value(self):
        manifest = {"spec": {"replicas": "{{ count }}"}}
        result = _apply_template_parameters(manifest, {"count": 3})
        assert result["spec"]["replicas"] == "3"

    def test_empty_parameters(self):
        manifest = {"metadata": {"name": "static"}}
        result = _apply_template_parameters(manifest, {})
        assert result["metadata"]["name"] == "static"
