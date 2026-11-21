"""Reading keep-warm idle time back: Get Job, Get Job Pods and ``gco jobs get``.

The health monitor writes each interval onto the Job's
``gco.aws/keep-warm-idle`` annotation. Get Job reports it parsed as a
``keep_warm`` block (``null`` when there is none), Get Job Pods names the
keep-warm label among each node's labels, and ``gco jobs get`` prints the
intervals in table mode.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from kubernetes.client import (
    V1Container,
    V1Job,
    V1JobSpec,
    V1JobStatus,
    V1ObjectMeta,
    V1PodSpec,
    V1PodTemplateSpec,
)

from gco.keep_warm import IDLE_ANNOTATION, KEEP_WARM_LABEL, dump_idle_records

CLOSED = {
    "node": "node-keep-warm-a",
    "instance_type": "g6.xlarge",
    "capacity_type": "on-demand",
    "pod": "trial-1-abcde",
    "since": "2026-11-21T18:01:40Z",
    "until": "2026-11-21T18:02:50Z",
    "seconds": 70,
    "ended_by": "reused",
    "window_seconds": 600,
}
OPEN = {
    "node": "node-keep-warm-b",
    "instance_type": "g6.xlarge",
    "capacity_type": "spot",
    "pod": "trial-1-fghij",
    "since": "2026-11-21T18:05:00Z",
    "window_seconds": 600,
}


def _v1_job(annotations: dict[str, str] | None) -> V1Job:
    return V1Job(
        metadata=V1ObjectMeta(name="trial-1", namespace="gco-jobs", annotations=annotations),
        spec=V1JobSpec(
            template=V1PodTemplateSpec(
                spec=V1PodSpec(containers=[V1Container(name="trial", image="busybox:1.38.0")])
            )
        ),
        status=V1JobStatus(),
    )


class TestGetJob:
    def test_reports_the_recorded_intervals(self) -> None:
        from gco.services.api_shared import _parse_job_to_dict

        job = _v1_job({IDLE_ANNOTATION: dump_idle_records([CLOSED, OPEN])})

        assert _parse_job_to_dict(job)["keep_warm"] == {
            "idle": [CLOSED, OPEN],
            "idle_seconds": 70,
            "open": True,
        }

    @pytest.mark.parametrize("annotations", [None, {"other": "x"}, {IDLE_ANNOTATION: "garbage"}])
    def test_is_null_without_a_readable_record(self, annotations: Any) -> None:
        from gco.services.api_shared import _parse_job_to_dict

        assert _parse_job_to_dict(_v1_job(annotations))["keep_warm"] is None


class TestGetJobPods:
    def test_a_node_reports_its_keep_warm_label_and_pool(self) -> None:
        from gco.services.api_shared import _parse_node_to_dict

        node = MagicMock()
        node.metadata.labels = {
            "node.kubernetes.io/instance-type": "g6.xlarge",
            "karpenter.sh/nodepool": "gpu-keep-warm-pool",
            KEEP_WARM_LABEL: "true",
        }

        labels = _parse_node_to_dict(node, "node-keep-warm-a")["labels"]

        assert labels[KEEP_WARM_LABEL] == "true"
        assert labels["karpenter.sh/nodepool"] == "gpu-keep-warm-pool"


class TestCliJobInfo:
    @staticmethod
    def _info(keep_warm: Any) -> Any:
        from cli.jobs import JobManager

        manager = JobManager.__new__(JobManager)
        payload = {
            "metadata": {"name": "trial-1", "namespace": "gco-jobs"},
            "status": {},
            "spec": {},
            "keep_warm": keep_warm,
        }
        return manager._parse_job_info(payload, "us-east-1")

    def test_the_block_is_carried_through(self) -> None:
        summary = {"idle": [CLOSED], "idle_seconds": 70, "open": False}
        assert self._info(summary).keep_warm == summary

    @pytest.mark.parametrize("value", [None, "not a mapping", ["x"]])
    def test_anything_but_a_mapping_is_none(self, value: Any) -> None:
        assert self._info(value).keep_warm is None


class TestJobsGetTable:
    @staticmethod
    def _invoke(keep_warm: Any) -> str:
        from cli.jobs import JobInfo
        from cli.main import cli

        job = JobInfo(
            name="trial-1",
            namespace="gco-jobs",
            region="us-east-1",
            status="succeeded",
            keep_warm=keep_warm,
        )
        with patch("cli.commands.jobs_cmd.get_job_manager") as get_manager:
            get_manager.return_value.get_job.return_value = job
            result = CliRunner().invoke(cli, ["jobs", "get", "trial-1", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        return result.output

    def test_prints_each_interval_and_the_closed_total(self) -> None:
        output = self._invoke({"idle": [CLOSED, OPEN], "idle_seconds": 70, "open": True})

        assert "Keep-warm idle time" in output
        assert "node-keep-warm-a" in output
        assert "70       reused" in output
        assert "still idle" in output
        assert "Closed idle time: 70 s (one or more still open)" in output

    def test_prints_nothing_without_intervals(self) -> None:
        assert "Keep-warm idle time" not in self._invoke(None)
        assert "Keep-warm idle time" not in self._invoke({"idle": ["junk"], "open": False})

    def test_a_closed_only_summary_has_no_open_note(self) -> None:
        output = self._invoke({"idle": [CLOSED], "idle_seconds": 70, "open": False})
        assert "Closed idle time: 70 s\n" in output
