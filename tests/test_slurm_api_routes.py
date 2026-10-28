"""
Tests for gco/services/api_routes/slurm.py — the /api/v1/slurm/* router.

The router is the authenticated front for slurmrestd. These tests mount it
alone (no auth middleware; the proxy logic is transport-independent) with a
stub ``SlurmRestClient`` whose endpoint methods answer from canned
slurmrestd payloads, so the summarising, filtering, request shaping and
error mapping all run. They prove: the disabled-feature 404 with its remedy
on every route, the ``status`` roll-up, the partition/node/job summaries
including slurmrestd's ``{set, infinite, number}`` wrappers, the queue
filters, the sbatch request body (caller attribution in ``comment``, GPU
``tres_per_node``, time-limit conversion, exclusive), the cancel signal
passthrough, the sacct window parameters, the request-body validators, and
the 400/404/503/500 translations of the client's errors.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gco.services import slurm_client
from gco.services.api_routes import slurm as slurm_routes
from gco.services.api_routes.slurm import router
from gco.services.slurm_client import SlurmApiError, SlurmUnavailableError


def _n(number: int | None = None, *, infinite: bool = False) -> dict[str, Any]:
    """slurmrestd's ``uint*_no_val`` wrapper."""
    if infinite:
        return {"set": True, "infinite": True, "number": 0}
    if number is None:
        return {"set": False, "infinite": False, "number": 0}
    return {"set": True, "infinite": False, "number": number}


PARTITION = {
    "name": "cpu",
    "partition": {"state": ["UP", "DEFAULT"]},
    "nodes": {"total": 2, "configured": "cpu-[0-1]"},
    "maximums": {"time": _n(1440)},
    "cpus": {"total": 8},
}
GPU_PARTITION = {
    "name": "gpu",
    "partition": {"state": "UP"},
    "nodes": {"total": 0, "allowed_allocation": "gpu-0"},
    "maximums": {"time": _n(infinite=True)},
}
NODE = {
    "name": "cpu-0",
    "hostname": "cpu-0",
    "state": ["IDLE"],
    "partitions": ["cpu"],
    "cpus": 4,
    "alloc_cpus": 1,
    "real_memory": 7000,
    "alloc_memory": 1024,
    "gres": "",
    "gres_used": "",
    "reason": "",
}
SCALED_IN_NODE = {
    "name": "cpu-1",
    "hostname": "cpu-1",
    "state": ["DOWN", "DRAIN", "DYNAMIC_NORM", "NOT_RESPONDING"],
    "partitions": ["cpu"],
    "cpus": 8,
    "reason": "slurm-operator: Pod (gco-jobs/slinky-slurm-worker-cpu-1) is pending termination for scale-in",
}
GPU_NODE = {
    "name": "gpu-0",
    "hostname": "gpu-0",
    "state": ["MIXED"],
    "partitions": ["gpu"],
    "cpus": 8,
    "alloc_cpus": 8,
    "real_memory": 30000,
    "alloc_memory": 30000,
    "gres": "gpu:1",
    "gres_used": "gpu:1(IDX:0)",
    "reason": None,
}
RUNNING_JOB = {
    "job_id": 11,
    "name": "train",
    "user_name": "slurm",
    "account": "root",
    "partition": "gpu",
    "job_state": ["RUNNING"],
    "state_reason": "None",
    "nodes": "gpu-0",
    "node_count": _n(1),
    "cpus": _n(8),
    "tres_req_str": "cpu=8,mem=30000M,node=1,gres/gpu=1",
    "tres_alloc_str": "cpu=8,mem=30000M,node=1,gres/gpu=1",
    "submit_time": _n(1_700_000_000),
    "start_time": _n(1_700_000_010),
    "end_time": _n(infinite=True),
    "time_limit": _n(60),
    "exit_code": {"status": ["SUCCESS"], "return_code": _n(0)},
    "comment": "gco:arn:aws:iam::1:user/alice",
    "current_working_directory": "/home/slurm",
    "standard_output": "slurm-%j.out",
    "standard_error": "/var/log/%x-%u-%%-%J.err",
}
PENDING_JOB = {
    **RUNNING_JOB,
    "job_id": 12,
    "name": "queued",
    "partition": "cpu",
    "job_state": ["PENDING"],
    "state_reason": "Resources",
    "nodes": "",
    "exit_code": None,
}
STATELESS_JOB = {"job_id": 13, "name": "odd"}
ACCOUNTING_JOB = {
    "job_id": 11,
    "name": "train",
    "user": "slurm",
    "account": "root",
    "partition": "gpu",
    "cluster": "gco-slurm",
    "state": {"current": ["COMPLETED"], "reason": "None"},
    "nodes": "gpu-0",
    "time": {
        "submission": 1_700_000_000,
        "start": 1_700_000_010,
        "end": 1_700_000_070,
        "elapsed": 60,
        "limit": _n(60),
    },
    "exit_code": {"status": ["SUCCESS"], "return_code": _n(0)},
    "tres": {"allocated": [{"type": "cpu", "count": 8}, {"type": "node", "count": 1}, "junk"]},
    "comment": {"job": "gco:alice", "system": ""},
    "allocation_nodes": 1,
    "required": {"CPUs": 8},
    "working_directory": "/home/slurm",
    "stdout_expanded": "/home/slurm/slurm-11.out",
    "stderr_expanded": "",
}


class _ClientStub:
    """Stands in for ``SlurmRestClient``: canned answers, recorded calls."""

    version = "v0.0.44"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.answers: dict[str, Any] = {
            "ping": {
                "pings": [
                    {
                        "hostname": "slinky-slurm-controller-0",
                        "pinged": "UP",
                        "latency": 123,
                        "mode": "primary",
                    }
                ]
            },
            "partitions": {"partitions": [PARTITION, GPU_PARTITION, "junk"]},
            "nodes": {"nodes": [NODE, GPU_NODE, SCALED_IN_NODE, 5]},
            "jobs": {"jobs": [RUNNING_JOB, PENDING_JOB, STATELESS_JOB, None]},
            "job": {"jobs": [RUNNING_JOB]},
            "submit": {"job_id": 42, "step_id": "batch", "job_submit_user_msg": "ok"},
            "cancel": {},
            "accounting_jobs": {"jobs": [ACCOUNTING_JOB, {"job_id": 12, "comment": "plain"}, 1]},
            "accounting_job": {"jobs": [ACCOUNTING_JOB]},
        }
        self.errors: dict[str, Exception] = {}

    def _answer(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args, kwargs))
        if name in self.errors:
            raise self.errors[name]
        return self.answers[name]

    async def ping(self) -> Any:
        return self._answer("ping")

    async def partitions(self) -> Any:
        return self._answer("partitions")

    async def nodes(self) -> Any:
        return self._answer("nodes")

    async def jobs(self) -> Any:
        return self._answer("jobs")

    async def job(self, job_id: int) -> Any:
        return self._answer("job", job_id)

    async def submit(self, job: Mapping[str, Any], script: str) -> Any:
        return self._answer("submit", dict(job), script)

    async def cancel(self, job_id: int, *, signal: str | None = None) -> Any:
        return self._answer("cancel", job_id, signal=signal)

    async def accounting_jobs(self, params: Mapping[str, Any]) -> Any:
        return self._answer("accounting_jobs", dict(params))

    async def accounting_job(self, job_id: int) -> Any:
        return self._answer("accounting_job", job_id)


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _ClientStub:
    double = _ClientStub()
    monkeypatch.setattr(slurm_client, "get_client", lambda: double)
    return double


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


SCRIPT = "#!/bin/bash\nsrun hostname\n"
ROUTES = [
    ("GET", "/api/v1/slurm/status", None),
    ("GET", "/api/v1/slurm/partitions", None),
    ("GET", "/api/v1/slurm/nodes", None),
    ("GET", "/api/v1/slurm/jobs", None),
    ("POST", "/api/v1/slurm/jobs", {"script": SCRIPT}),
    ("GET", "/api/v1/slurm/jobs/11", None),
    ("DELETE", "/api/v1/slurm/jobs/11", None),
    ("GET", "/api/v1/slurm/accounting/jobs", None),
]


class TestDisabled:
    @pytest.mark.parametrize(("method", "path", "body"), ROUTES)
    def test_every_route_answers_404_with_the_remedy(self, client, monkeypatch, method, path, body):
        monkeypatch.setenv("GCO_SLURM_ENABLED", "false")
        slurm_client.reset_client()

        result = client.request(method, path, json=body)

        assert result.status_code == 404
        assert result.json()["detail"] == slurm_client.DISABLED_DETAIL


class TestStatus:
    def test_rolls_up_controller_partitions_nodes_and_jobs(self, client, stub):
        result = client.get("/api/v1/slurm/status")

        assert result.status_code == 200
        body = result.json()
        assert body["enabled"] is True
        assert body["api_version"] == "v0.0.44"
        assert body["controller"] == [
            {
                "hostname": "slinky-slurm-controller-0",
                "responding": True,
                "latency_us": 123,
                "mode": "primary",
            }
        ]
        assert [p["name"] for p in body["partitions"]] == ["cpu", "gpu"]
        assert body["nodes"] == {
            "total": 3,
            "by_state": {"IDLE": 1, "MIXED": 1},
            "scaled_in": 1,
        }
        assert body["jobs"] == {
            "total": 3,
            "by_state": {"RUNNING": 1, "PENDING": 1, "UNKNOWN": 1},
        }
        assert "timestamp" in body
        assert [name for name, _a, _k in stub.calls] == ["ping", "partitions", "nodes", "jobs"]

    def test_newer_ping_shape_and_a_stateless_node(self, client, stub):
        stub.answers["ping"] = {
            "pings": [
                {"hostname": "c", "responding": True},
                {"hostname": "b", "pinged": "DOWN"},
                "junk",
            ]
        }
        stub.answers["nodes"] = {"nodes": [{"name": "n"}]}

        body = client.get("/api/v1/slurm/status").json()

        assert [c["responding"] for c in body["controller"]] == [True, False]
        assert body["nodes"] == {"total": 1, "by_state": {"UNKNOWN": 1}, "scaled_in": 0}

    def test_unreachable_restd_is_503_and_logged(self, client, stub, caplog):
        stub.errors["ping"] = SlurmUnavailableError("slurmrestd unreachable at http://x")

        with caplog.at_level(logging.WARNING, logger=slurm_routes.__name__):
            result = client.get("/api/v1/slurm/status")

        assert result.status_code == 503
        assert result.json()["detail"] == slurm_client.UNAVAILABLE_DETAIL
        assert "Slurm reading status failed (HTTP 503): SlurmUnavailableError" in caplog.text

    def test_api_version_drift_is_503_with_the_pin_remedy(self, client, stub):
        # The client raises this when /ping answers 404 — the deployed image
        # does not serve SLURM_API_VERSION. The route must hand the operator
        # the precise remedy, not the generic transport one.
        detail = slurm_client.API_VERSION_DETAIL.format(version="v0.0.44")
        stub.errors["ping"] = SlurmUnavailableError("does not serve v0.0.44", detail=detail)

        result = client.get("/api/v1/slurm/status")

        assert result.status_code == 503
        assert result.json()["detail"] == detail
        assert "/slurm/v0.0.44/ping" in result.json()["detail"]


class TestPartitions:
    def test_summarises_each_partition(self, client, stub):
        result = client.get("/api/v1/slurm/partitions")

        assert result.status_code == 200
        cpu, gpu = result.json()["partitions"]
        assert cpu == {
            "name": "cpu",
            "state": ["UP", "DEFAULT"],
            "default": True,
            "total_nodes": 2,
            "node_names": "cpu-[0-1]",
            "max_time_minutes": 1440,
            "total_cpus": 8,
        }
        assert gpu == {
            "name": "gpu",
            "state": ["UP"],
            "default": False,
            "total_nodes": 0,
            "node_names": "gpu-0",
            "max_time_minutes": None,
            "total_cpus": None,
        }

    def test_bare_partition_shapes_do_not_break(self, client, stub):
        stub.answers["partitions"] = {
            "partitions": [{"name": "odd", "nodes": "cpu-0", "partition": 1}]
        }

        [odd] = client.get("/api/v1/slurm/partitions").json()["partitions"]

        assert odd["state"] == []
        assert odd["default"] is False
        assert odd["node_names"] == ""

    def test_token_problem_is_the_token_503(self, client, stub):
        stub.errors["partitions"] = SlurmUnavailableError("could not read Secret gco-jobs/t: 403")

        result = client.get("/api/v1/slurm/partitions")

        assert result.status_code == 503
        assert result.json()["detail"] == slurm_client.TOKEN_UNAVAILABLE_DETAIL


class TestNodes:
    def test_lists_every_node(self, client, stub):
        result = client.get("/api/v1/slurm/nodes")

        assert result.status_code == 200
        cpu, gpu, scaled = result.json()["nodes"]
        assert cpu == {
            "name": "cpu-0",
            "hostname": "cpu-0",
            "state": ["IDLE"],
            "scaled_in": False,
            "partitions": ["cpu"],
            "cpus": 4,
            "cpus_allocated": 1,
            "real_memory_mb": 7000,
            "memory_allocated_mb": 1024,
            "gres": "",
            "gres_used": "",
            "reason": "",
        }
        assert gpu["gres_used"] == "gpu:1(IDX:0)"
        assert gpu["reason"] == ""
        assert scaled["scaled_in"] is True
        assert scaled["state"] == ["DOWN", "DRAIN", "DYNAMIC_NORM", "NOT_RESPONDING"]

    @pytest.mark.parametrize(
        ("item", "expected"),
        [
            ({"state": ["DOWN", "DRAIN"], "reason": "slurm-operator: Pod is terminating"}, True),
            ({"state": ["DOWN", "DRAIN"], "reason": "Not responding"}, False),
            ({"state": ["DOWN"], "reason": "slurm-operator: Pod is terminating"}, False),
            ({"state": ["IDLE", "DRAIN"], "reason": "slurm-operator: draining"}, False),
            ({"state": "DOWN+DRAIN", "reason": None}, False),
        ],
    )
    def test_scaled_in_detection(self, item, expected):
        assert slurm_routes._scaled_in(item) is expected

    def test_filters_by_partition(self, client, stub):
        body = client.get("/api/v1/slurm/nodes", params={"partition": "gpu"}).json()
        assert [n["name"] for n in body["nodes"]] == ["gpu-0"]

    def test_slurm_error_is_passed_through(self, client, stub):
        stub.errors["nodes"] = SlurmApiError(502, [], "Zero Bytes were transmitted")

        result = client.get("/api/v1/slurm/nodes")

        assert result.status_code == 502
        assert result.json()["detail"] == "Zero Bytes were transmitted"


class TestJobs:
    def test_summarises_the_queue(self, client, stub):
        result = client.get("/api/v1/slurm/jobs")

        assert result.status_code == 200
        body = result.json()
        assert body["total"] == 3
        running, pending, odd = body["jobs"]
        assert running == {
            "job_id": 11,
            "name": "train",
            "user": "slurm",
            "account": "root",
            "partition": "gpu",
            "state": ["RUNNING"],
            "state_reason": "None",
            "nodes": "gpu-0",
            "node_count": 1,
            "cpus": 8,
            "tres_requested": "cpu=8,mem=30000M,node=1,gres/gpu=1",
            "tres_allocated": "cpu=8,mem=30000M,node=1,gres/gpu=1",
            "submit_time": 1_700_000_000,
            "start_time": 1_700_000_010,
            "end_time": None,
            "time_limit_minutes": 60,
            "exit_code": {"status": ["SUCCESS"], "return_code": 0},
            "comment": "gco:arn:aws:iam::1:user/alice",
            "working_directory": "/home/slurm",
            "standard_output": "/home/slurm/slurm-11.out",
            "standard_error": "/var/log/train-slurm-%-11.err",
        }
        assert pending["exit_code"] is None
        assert pending["nodes"] == ""
        assert odd["state"] == []
        assert odd["node_count"] is None

    @pytest.mark.parametrize(
        ("params", "expected_ids"),
        [
            ({"state": "pending"}, [12]),
            ({"partition": "gpu"}, [11]),
            ({"name": "odd"}, [13]),
            ({"limit": 1}, [11]),
            ({"state": "RUNNING", "partition": "cpu"}, []),
        ],
    )
    def test_filters(self, client, stub, params, expected_ids):
        body = client.get("/api/v1/slurm/jobs", params=params).json()
        assert [j["job_id"] for j in body["jobs"]] == expected_ids

    def test_limit_is_validated(self, client, stub):
        assert client.get("/api/v1/slurm/jobs", params={"limit": 0}).status_code == 422
        assert stub.calls == []

    def test_error_path(self, client, stub):
        stub.errors["jobs"] = SlurmUnavailableError("slurmrestd unreachable")
        assert client.get("/api/v1/slurm/jobs").status_code == 503


class TestJob:
    def test_returns_the_one_job(self, client, stub):
        result = client.get("/api/v1/slurm/jobs/11")

        assert result.status_code == 200
        assert result.json()["job"]["job_id"] == 11
        assert result.json()["job"]["source"] == "slurmctld"
        assert stub.calls == [("job", (11,), {})]

    @pytest.mark.parametrize("forgotten", ["empty", "rc2017"])
    def test_a_job_slurmctld_forgot_comes_from_accounting(self, client, stub, forgotten):
        """After MinJobAge slurmctld answers rc 2017 (or an empty list); slurmdbd still has it."""
        if forgotten == "empty":
            stub.answers["job"] = {"jobs": []}
        else:
            stub.errors["job"] = SlurmApiError(
                404, [{"error_number": 2017}], "Invalid job id specified"
            )

        result = client.get("/api/v1/slurm/jobs/11")

        assert result.status_code == 200, result.json()
        assert result.json()["job"] == {
            "job_id": 11,
            "name": "train",
            "user": "slurm",
            "account": "root",
            "partition": "gpu",
            "state": ["COMPLETED"],
            "state_reason": "None",
            "nodes": "gpu-0",
            "node_count": 1,
            "cpus": 8,
            "tres_requested": "cpu=8,node=1",
            "tres_allocated": "cpu=8,node=1",
            "submit_time": 1_700_000_000,
            "start_time": 1_700_000_010,
            "end_time": 1_700_000_070,
            "time_limit_minutes": 60,
            "exit_code": {"status": ["SUCCESS"], "return_code": 0},
            "comment": "gco:alice",
            "working_directory": "/home/slurm",
            "standard_output": "/home/slurm/slurm-11.out",
            "standard_error": None,
            "source": "slurmdbd",
        }
        assert [name for name, _a, _k in stub.calls] == ["job", "accounting_job"]

    def test_unknown_everywhere_is_404(self, client, stub):
        stub.answers["job"] = {"jobs": []}
        stub.answers["accounting_job"] = {
            "jobs": [],
            "warnings": [{"description": "found nothing"}],
        }

        result = client.get("/api/v1/slurm/jobs/404")

        assert result.status_code == 404
        assert result.json()["detail"] == "Slurm job 404 not found"

    def test_an_unreachable_slurmctld_is_503_without_a_fallback(self, client, stub):
        stub.errors["job"] = SlurmUnavailableError("slurmrestd unreachable")

        result = client.get("/api/v1/slurm/jobs/11")

        assert result.status_code == 503
        assert [name for name, _a, _k in stub.calls] == ["job"]

    def test_a_non_404_slurmctld_error_is_not_masked(self, client, stub):
        stub.errors["job"] = SlurmApiError(502, [], "Zero Bytes were transmitted")

        result = client.get("/api/v1/slurm/jobs/11")

        assert result.status_code == 502
        assert [name for name, _a, _k in stub.calls] == ["job"]

    def test_an_accounting_failure_during_the_fallback_is_reported(self, client, stub, caplog):
        stub.answers["job"] = {"jobs": []}
        stub.errors["accounting_job"] = SlurmUnavailableError("slurmrestd unreachable")

        with caplog.at_level(logging.WARNING, logger=slurm_routes.__name__):
            result = client.get("/api/v1/slurm/jobs/11")

        assert result.status_code == 503
        assert "reading job 11 from accounting failed" in caplog.text

    def test_sparse_accounting_record_as_job(self):
        job = slurm_routes._accounting_record_as_job({"job_id": 5, "tres": {"allocated": "x"}})
        assert job["job_id"] == 5
        assert job["tres_allocated"] == ""
        assert job["standard_output"] is None
        assert job["source"] == "slurmdbd"

    def test_non_integer_id_is_422(self, client, stub):
        assert client.get("/api/v1/slurm/jobs/abc").status_code == 422


class TestSubmit:
    def test_minimal_submission_attributes_the_caller(self, client, stub):
        result = client.post(
            "/api/v1/slurm/jobs",
            json={"script": SCRIPT},
            headers={"x-gco-caller": "  arn:aws:sts::1:assumed-role/dev/alice  "},
        )

        assert result.status_code == 201
        body = result.json()
        assert body["job_id"] == 42
        assert body["step_id"] == "batch"
        assert body["name"] == "gco-job"
        assert body["partition"] is None
        assert body["submitted_by"] == "arn:aws:sts::1:assumed-role/dev/alice"
        assert body["message"] == "ok"
        [(name, (job, script), _kwargs)] = stub.calls
        assert name == "submit"
        assert script == SCRIPT
        assert job == {
            "name": "gco-job",
            "nodes": "1",
            "tasks": 1,
            "cpus_per_task": 1,
            "current_working_directory": "/home/slurm",
            "comment": "gco:arn:aws:sts::1:assumed-role/dev/alice",
            "environment": ["PATH=/usr/local/bin:/usr/bin:/bin"],
        }

    def test_full_submission_shapes_every_option(self, client, stub):
        result = client.post(
            "/api/v1/slurm/jobs",
            json={
                "script": SCRIPT,
                "name": "train.v2",
                "partition": "gpu",
                "nodes": 2,
                "tasks": 16,
                "cpus_per_task": 4,
                "gpus_per_node": 8,
                "memory_mb": 65536,
                "time_limit": " 1-02:30:15 ",
                "working_directory": "/home/slurm/run",
                "environment": {"OMP_NUM_THREADS": "4", "PATH": "/opt/bin"},
                "comment": "nightly",
                "exclusive": True,
            },
            headers={"x-gco-caller-arn": "arn:aws:iam::1:user/bob"},
        )

        assert result.status_code == 201
        [(_name, (job, _script), _kwargs)] = stub.calls
        assert job == {
            "name": "train.v2",
            "nodes": "2",
            "tasks": 16,
            "cpus_per_task": 4,
            "current_working_directory": "/home/slurm/run",
            "comment": "gco:arn:aws:iam::1:user/bob nightly",
            "environment": ["PATH=/opt/bin", "OMP_NUM_THREADS=4"],
            "partition": "gpu",
            "tres_per_node": "gres/gpu:8",
            "memory_per_node": {"set": True, "number": 65536},
            "time_limit": {"set": True, "number": 1 * 1440 + 2 * 60 + 30 + 1},
            "shared": ["none"],
        }
        assert result.json()["submitted_by"] == "arn:aws:iam::1:user/bob"

    def test_without_a_caller_header_the_api_is_the_submitter(self, client, stub):
        client.post("/api/v1/slurm/jobs", json={"script": SCRIPT})
        [(_name, (job, _script), _kwargs)] = stub.calls
        assert job["comment"] == "gco:gco-api"

    def test_long_caller_identities_are_truncated(self, client, stub):
        client.post(
            "/api/v1/slurm/jobs", json={"script": SCRIPT}, headers={"x-gco-caller": "a" * 300}
        )
        [(_name, (job, _script), _kwargs)] = stub.calls
        assert job["comment"] == "gco:" + "a" * 200

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("UNLIMITED", {"set": True, "infinite": True}),
            ("infinite", {"set": True, "infinite": True}),
            ("90", {"set": True, "number": 90}),
            ("0", {"set": True, "number": 1}),
            ("10:00", {"set": True, "number": 10}),
            ("10:30", {"set": True, "number": 11}),
            ("01:00:00", {"set": True, "number": 60}),
            ("01:00:01", {"set": True, "number": 61}),
            ("2-00", {"set": True, "number": 2880}),
            ("1-12:30", {"set": True, "number": 1440 + 750}),
            ("1-00:00:30", {"set": True, "number": 1441}),
        ],
    )
    def test_time_limit_conversion(self, value, expected):
        assert slurm_routes._time_limit_minutes(value) == expected

    @pytest.mark.parametrize(
        ("body", "fragment"),
        [
            ({"script": "   "}, "script must not be empty"),
            ({"script": "echo hi"}, "interpreter line"),
            ({"script": "#!/bin/bash\n" + "x" * (64 * 1024)}, "exceeds"),
            ({"script": SCRIPT, "name": "bad name"}, "name must be"),
            ({"script": SCRIPT, "name": "-leading"}, "name must be"),
            ({"script": SCRIPT, "partition": "GPU"}, "lowercase partition"),
            ({"script": SCRIPT, "time_limit": "soon"}, "time_limit must be"),
            ({"script": SCRIPT, "working_directory": "relative"}, "absolute path"),
            ({"script": SCRIPT, "working_directory": "/a\n/b"}, "absolute path"),
            ({"script": SCRIPT, "environment": {"1BAD": "x"}}, "is not valid"),
            ({"script": SCRIPT, "environment": {"OK": "a\nb"}}, "single-line"),
            ({"script": SCRIPT, "environment": {f"V{i}": "x" for i in range(65)}}, "at most 64"),
            ({"script": SCRIPT, "nodes": 0}, "greater than or equal to 1"),
            ({"script": SCRIPT, "gpus_per_node": 65}, "less than or equal to 64"),
            ({"script": SCRIPT, "comment": "c" * 513}, "at most 512"),
        ],
    )
    def test_request_validation(self, client, stub, body, fragment):
        result = client.post("/api/v1/slurm/jobs", json=body)

        assert result.status_code == 422
        assert fragment in str(result.json()["detail"])
        assert stub.calls == []

    def test_time_limit_may_be_omitted_or_null(self, client, stub):
        assert (
            client.post(
                "/api/v1/slurm/jobs", json={"script": SCRIPT, "time_limit": None}
            ).status_code
            == 201
        )
        [(_name, (job, _script), _kwargs)] = stub.calls
        assert "time_limit" not in job

    def test_slurm_rejection_is_a_400(self, client, stub, caplog):
        stub.errors["submit"] = SlurmApiError(400, [], "Invalid partition name specified")

        with caplog.at_level(logging.WARNING, logger=slurm_routes.__name__):
            result = client.post("/api/v1/slurm/jobs", json={"script": SCRIPT, "partition": "nope"})

        assert result.status_code == 400
        assert result.json()["detail"] == "Invalid partition name specified"
        assert "failed" not in caplog.text, "4xx answers are the caller's, not the operator's"

    def test_unexpected_error_is_a_500_without_the_cause(self, client, stub, caplog):
        stub.errors["submit"] = ValueError("secret-ish internals")

        with caplog.at_level(logging.WARNING, logger=slurm_routes.__name__):
            result = client.post("/api/v1/slurm/jobs", json={"script": SCRIPT})

        assert result.status_code == 500
        assert result.json()["detail"] == "Internal error while submitting job"
        assert "secret-ish internals" in caplog.text
        assert "ValueError" in caplog.text


class TestCancel:
    def test_cancels_with_the_default_signal(self, client, stub):
        result = client.delete("/api/v1/slurm/jobs/11")

        assert result.status_code == 200
        assert result.json() == {
            "timestamp": result.json()["timestamp"],
            "job_id": 11,
            "cancelled": True,
        }
        assert stub.calls == [("cancel", (11,), {"signal": None})]

    def test_passes_a_signal_through(self, client, stub):
        client.delete("/api/v1/slurm/jobs/11", params={"signal": "SIGUSR1"})
        assert stub.calls == [("cancel", (11,), {"signal": "SIGUSR1"})]

    def test_signal_shape_is_validated(self, client, stub):
        assert (
            client.delete("/api/v1/slurm/jobs/11", params={"signal": "kill -9"}).status_code == 422
        )
        assert stub.calls == []

    def test_unknown_job_is_404(self, client, stub):
        stub.errors["cancel"] = SlurmApiError(
            404, [{"error_number": 2017}], "Invalid job id specified"
        )
        assert client.delete("/api/v1/slurm/jobs/99").status_code == 404


class TestAccounting:
    def test_summarises_sacct_rows(self, client, stub):
        result = client.get("/api/v1/slurm/accounting/jobs")

        assert result.status_code == 200
        body = result.json()
        assert body["total"] == 2
        full, sparse = body["jobs"]
        assert full == {
            "job_id": 11,
            "name": "train",
            "user": "slurm",
            "account": "root",
            "partition": "gpu",
            "cluster": "gco-slurm",
            "state": ["COMPLETED"],
            "state_reason": "None",
            "nodes": "gpu-0",
            "submit_time": 1_700_000_000,
            "start_time": 1_700_000_010,
            "end_time": 1_700_000_070,
            "elapsed_seconds": 60,
            "time_limit_minutes": 60,
            "exit_code": {"status": ["SUCCESS"], "return_code": 0},
            "tres_allocated": [{"type": "cpu", "count": 8}, {"type": "node", "count": 1}, "junk"],
            "comment": "gco:alice",
        }
        assert sparse["comment"] == "plain"
        assert sparse["state"] == []
        assert sparse["exit_code"] == {"status": [], "return_code": None}
        assert stub.calls == [("accounting_jobs", ({},), {})]

    def test_window_and_filters_become_slurmdb_parameters(self, client, stub):
        result = client.get(
            "/api/v1/slurm/accounting/jobs",
            params={
                "start": "2026-10-01T00:00:00",
                "end": "now",
                "user": "alice",
                "partition": "gpu",
                "state": "COMPLETED,FAILED",
                "job_name": "train.v2",
                "limit": 1,
            },
        )

        assert result.status_code == 200
        assert len(result.json()["jobs"]) == 1
        assert stub.calls == [
            (
                "accounting_jobs",
                (
                    {
                        "start_time": "2026-10-01T00:00:00",
                        "end_time": "now",
                        "users": "alice",
                        "partition": "gpu",
                        "state": "COMPLETED,FAILED",
                        "job_name": "train.v2",
                    },
                ),
                {},
            )
        ]

    def test_a_state_filter_defaults_the_window_end_to_now(self, client, stub):
        client.get(
            "/api/v1/slurm/accounting/jobs", params={"state": "COMPLETED", "start": "now-1days"}
        )
        assert stub.calls == [
            (
                "accounting_jobs",
                ({"start_time": "now-1days", "end_time": "now", "state": "COMPLETED"},),
                {},
            )
        ]
        stub.calls.clear()
        client.get("/api/v1/slurm/accounting/jobs", params={"state": "FAILED", "end": "2026-10-08"})
        assert stub.calls[0][1][0] == {"end_time": "2026-10-08", "state": "FAILED"}

    @pytest.mark.parametrize(
        "params",
        [
            {"user": "al ice"},
            {"partition": "GPU"},
            {"state": "COMPLETED;DROP"},
            {"job_name": "a b"},
        ],
    )
    def test_filter_shapes_are_validated(self, client, stub, params):
        assert client.get("/api/v1/slurm/accounting/jobs", params=params).status_code == 422
        assert stub.calls == []

    def test_accounting_off_surfaces_slurm_error(self, client, stub):
        stub.errors["accounting_jobs"] = SlurmApiError(502, [], "Unable to contact slurmdbd")

        result = client.get("/api/v1/slurm/accounting/jobs")

        assert result.status_code == 502
        assert result.json()["detail"] == "Unable to contact slurmdbd"


class TestHelpers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (_n(5), 5),
            (_n(infinite=True), None),
            (_n(), None),
            ({"set": True}, None),
            (True, None),
            (None, None),
            ("7", 7),
            (3.9, 3),
            ("many", None),
        ],
    )
    def test_number_unwraps_slurmrestd_values(self, value, expected):
        assert slurm_routes._number(value) == expected

    def test_states_normalises_lists_strings_and_junk(self):
        assert slurm_routes._states(["A", 1]) == ["A", "1"]
        assert slurm_routes._states("A") == ["A"]
        assert slurm_routes._states(None) == []

    def test_exit_code_requires_an_object(self):
        assert slurm_routes._exit_code("0:0") is None

    @pytest.mark.parametrize(
        ("item", "pattern", "expected"),
        [
            (
                {"job_id": 7, "current_working_directory": "/home/slurm"},
                "slurm-%j.out",
                "/home/slurm/slurm-7.out",
            ),
            (
                {"job_id": 7, "current_working_directory": "/home/slurm/"},
                "out/%J.txt",
                "/home/slurm/out/7.txt",
            ),
            ({"job_id": 7}, "slurm-%j.out", "slurm-7.out"),
            (
                {"job_id": 7, "name": "train", "user_name": "slurm"},
                "/logs/%x.%u.%%.out",
                "/logs/train.slurm.%.out",
            ),
            (
                {
                    "job_id": 9,
                    "array_job_id": _n(8),
                    "array_task_id": _n(0),
                    "current_working_directory": "/w",
                },
                "slurm-%A_%a.out",
                "/w/slurm-8_0.out",
            ),
            (
                {
                    "job_id": 9,
                    "array_job_id": _n(),
                    "array_task_id": _n(),
                    "current_working_directory": "/w",
                },
                "%A_%a",
                "/w/%A_%a",
            ),
            ({"job_id": 7}, "", None),
            ({"job_id": 7}, None, None),
        ],
    )
    def test_output_path_expansion(self, item, pattern, expected):
        assert slurm_routes._output_path(item, pattern) == expected


class TestRouterRegistration:
    def test_slurm_router_is_mounted_on_the_manifest_api(self):
        from gco.services.manifest_api import app as manifest_app

        paths = set(manifest_app.openapi()["paths"])
        assert {
            "/api/v1/slurm/status",
            "/api/v1/slurm/partitions",
            "/api/v1/slurm/nodes",
            "/api/v1/slurm/jobs",
            "/api/v1/slurm/jobs/{job_id}",
            "/api/v1/slurm/accounting/jobs",
        } <= paths
