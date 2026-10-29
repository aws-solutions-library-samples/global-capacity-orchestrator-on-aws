"""Tests for the AWS PCS pieces of the live release validation harness.

``checks/pcs.py`` is the probe behind the ``schedulers`` action's ``pcs``
entry and the ``pcs_overrides`` CDK context every release run carries;
``actions/preflight.py::_pcs_preflight`` runs the CLI's account preflight
for the same merged block before ``deploy``; ``cleanup/pcs_nodes.py`` waits
for the instances PCS launched to finish terminating after teardown. The
GCO API is faked at ``make_authenticated_request`` (the seam every API check
uses), ``gco slurm output`` at ``subprocess.run``, and EC2 through the same
paginated-client fake the inventory tests use, so no test touches AWS or a
subprocess.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cli.pcs_preflight import FAIL, PASS, WARN, Check, PreflightReport
from scripts.live_release_validation.actions import preflight as actions_preflight
from scripts.live_release_validation.checks import pcs as checks_pcs
from scripts.live_release_validation.cleanup import pcs_nodes as cleanup_pcs_nodes
from tests._live_validation_patching import patch_live_validation_helper

_REGION = "us-east-1"
_PROJECT = "gco-live"
_OVERRIDES = checks_pcs.pcs_overrides_json()
_MARKER = "GCO_LIVE_PCS_run-123"
_NAME = "gco-live-pcs-run-123"


def _ctx(
    *,
    cdk_context: dict[str, Any] | None = None,
    overrides: str = _OVERRIDES,
    regions: tuple[str, ...] = (_REGION,),
    job_timeout_seconds: int = 30,
) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            run_id="run-123",
            pcs_overrides_json=overrides,
            job_timeout_seconds=job_timeout_seconds,
            poll_interval_seconds=0,
            command_timeout_seconds=120,
            repo_root="/repo",
        ),
        cdk_context={"api_gateway": {"regional_api_enabled": True}, **(cdk_context or {})},
        deployment_regions=regions,
        config=SimpleNamespace(project_name=_PROJECT, global_region=_REGION),
        session=MagicMock(),
        aws_client=MagicMock(),
    )


def _response(payload: Any, *, status: int = 200) -> SimpleNamespace:
    return SimpleNamespace(
        ok=status < 400, status_code=status, text=json.dumps(payload), json=lambda: payload
    )


def _bad_json_response() -> SimpleNamespace:
    def boom() -> Any:
        raise ValueError("not json")

    return SimpleNamespace(ok=True, status_code=200, text="<html>", json=boom)


def _pcs_backend(**overrides: Any) -> dict[str, Any]:
    entry = {
        "name": "pcs",
        "kind": "pcs",
        "cluster_id": "pcs_0a1b2c3d4e",
        "cluster_name": f"{_PROJECT}-pcs",
        "status": "ACTIVE",
        "slurm_version": "25.11",
        "rest_mode": "STANDARD",
        "identity": {"name": "ec2-user", "uid": 1000, "gid": 1000},
    }
    entry.update(overrides)
    return entry


def _backends(*entries: dict[str, Any]) -> dict[str, Any]:
    names = [entry["name"] for entry in entries]
    return {
        "backends": list(entries),
        "default": names[0] if len(names) == 1 else None,
        "selection_required": len(names) > 1,
    }


def _job(states: list[str], *, reason: str = "", exit_code: Any = None) -> dict[str, Any]:
    return {
        "job": {
            "job_id": 42,
            "state": states,
            "state_reason": reason,
            "nodes": "pcs-node-1",
            "exit_code": exit_code,
        },
        "source": "slurmctld",
    }


def _accounting(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"jobs": list(rows)}


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "job_id": 42,
        "name": _NAME,
        "state": "COMPLETED",
        "comment": "gco:arn:aws:iam::123456789012:user/validator live-release-validation",
        "cluster": f"{_PROJECT}-pcs",
    }
    row.update(overrides)
    return row


class _Api:
    """``make_authenticated_request`` answering a scripted sequence per (method, path)."""

    def __init__(self, script: list[tuple[str, str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(
        self, *, method: str, path: str, target_region: str | None, body: Any = None
    ) -> SimpleNamespace:
        assert target_region == _REGION
        self.calls.append((method, path, body))
        if not self.script:
            raise AssertionError(f"unexpected request {method} {path}")
        expected_method, expected_path, response = self.script.pop(0)
        assert (method, path) == (expected_method, expected_path), (method, path)
        return response if isinstance(response, SimpleNamespace) else _response(response)


def _completed(*, stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _output_document(content: str = f"{_MARKER}\npcs-node-1\n") -> str:
    return json.dumps({"job_id": 42, "backend": "pcs", "truncated": False, "content": content})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks_pcs.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cleanup_pcs_nodes.time, "sleep", lambda _s: None)


# ---------------------------------------------------------------------------
# The run's override and the merged block
# ---------------------------------------------------------------------------


class TestOverridesAndConfig:
    def test_the_release_override_is_canonical_json(self) -> None:
        assert _OVERRIDES == '{"enabled":true,"managed":{"metrics":{"enabled":true}}}'
        assert json.loads(_OVERRIDES) == checks_pcs.RELEASE_PCS_OVERRIDES

    def test_effective_config_merges_cdk_json_its_overlay_and_the_override(self) -> None:
        ctx = _ctx(
            cdk_context={
                "pcs": {"enabled": False, "managed": {"size": "MEDIUM"}},
                "pcs_regions": {_REGION: {"managed": {"login": {"enabled": False}}}},
            }
        )
        config = checks_pcs.effective_pcs_config(ctx, _REGION)
        assert config["enabled"] is True
        assert config["managed"]["metrics"]["enabled"] is True
        assert config["managed"]["size"] == "MEDIUM"
        assert config["managed"]["login"]["enabled"] is False
        other = checks_pcs.effective_pcs_config(ctx, "eu-west-1")
        assert other["managed"]["login"]["enabled"] is True

    def test_without_an_override_cdk_json_decides(self) -> None:
        ctx = _ctx(cdk_context={}, overrides="")
        assert checks_pcs.effective_pcs_config(ctx, _REGION)["enabled"] is False
        assert checks_pcs.effective_pcs_enablement(ctx) == {
            "enabled": False,
            "source": "cdk.json pcs",
            "mode": "managed",
            "region": _REGION,
        }
        ctx = _ctx(cdk_context={"pcs": {"enabled": True}}, overrides="")
        assert checks_pcs.effective_pcs_enablement(ctx)["source"] == "cdk.json pcs"
        assert checks_pcs.effective_pcs_enablement(_ctx())["source"] == (
            "run-override (pcs_overrides)"
        )

    def test_an_invalid_block_is_named(self) -> None:
        ctx = _ctx(cdk_context={"pcs": {"mode": "existing"}})
        with pytest.raises(checks_pcs.PcsValidationError, match="us-east-1: the pcs block"):
            checks_pcs.effective_pcs_config(ctx, _REGION)


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


_ACCOUNTING_PATH = (
    f"/api/v1/slurm/accounting/jobs?backend=pcs&start=now-6hours&job_name={_NAME}&limit=200"
)


def _happy_script(*, backends: dict[str, Any] | None = None) -> list[tuple[str, str, Any]]:
    return [
        ("GET", "/api/v1/slurm/backends", backends or _backends(_pcs_backend())),
        ("POST", "/api/v1/slurm/jobs?backend=pcs", {"job_id": 42, "backend": "pcs"}),
        # The same state twice is one history entry; slurmdbd first knows the
        # job as RUNNING without a comment, then as COMPLETED with it.
        ("GET", "/api/v1/slurm/jobs/42?backend=pcs", _job(["PENDING"], reason="Resources")),
        ("GET", "/api/v1/slurm/jobs/42?backend=pcs", _job(["PENDING"], reason="Resources")),
        ("GET", "/api/v1/slurm/jobs/42?backend=pcs", _job(["RUNNING"])),
        ("GET", "/api/v1/slurm/jobs/42?backend=pcs", _job(["COMPLETED"], exit_code=0)),
        ("GET", _ACCOUNTING_PATH, _accounting()),
        ("GET", _ACCOUNTING_PATH, _accounting(_row(state="RUNNING", comment=""))),
        ("GET", _ACCOUNTING_PATH, _accounting(_row(job_id=41), _row())),
    ]


def _probe(ctx: SimpleNamespace, script: list[tuple[str, str, Any]], run: Callable[..., Any]):
    api = _Api(script)
    ctx.aws_client.make_authenticated_request.side_effect = api
    with patch.object(checks_pcs.subprocess, "run", side_effect=run) as subprocess_run:
        evidence = checks_pcs.probe_pcs(ctx)
    return evidence, api, subprocess_run


class TestProbe:
    def test_end_to_end_evidence(self) -> None:
        ctx = _ctx()
        runs: list[list[str]] = []

        def run(argv: list[str], **kwargs: Any) -> Any:
            runs.append(argv)
            assert kwargs["cwd"] == "/repo" and kwargs["timeout"] == 120.0
            assert kwargs["shell"] is False and kwargs["check"] is False
            return _completed(stdout=_output_document())

        evidence, api, _ = _probe(ctx, _happy_script(), run)

        assert evidence["region"] == _REGION and evidence["mode"] == "managed"
        assert evidence["marker"] == _MARKER
        assert evidence["backend"] == {
            "backends": ["pcs"],
            "selection_required": False,
            "default": "pcs",
            "cluster_id": "pcs_0a1b2c3d4e",
            "cluster_name": f"{_PROJECT}-pcs",
            "slurm_version": "25.11",
            "status": "ACTIVE",
            "identity": {"name": "ec2-user", "uid": 1000, "gid": 1000},
        }
        assert evidence["job"]["job_id"] == 42 and evidence["job"]["name"] == _NAME
        completion = evidence["completion"]
        assert completion["states"] == ["COMPLETED"] and completion["exit_code"] == 0
        assert [entry["states"] for entry in completion["history"]] == [
            ["PENDING"],
            ["RUNNING"],
            ["COMPLETED"],
        ]
        assert evidence["accounting"]["states"] == ["COMPLETED"]
        assert evidence["accounting"]["comment"].startswith("gco:")
        assert evidence["output"]["truncated"] is False
        assert evidence["output"]["backend"] == "pcs"
        assert evidence["output"]["attempts"] == [{"attempt": 1, "returncode": 0}]
        # The submission body: the marker in a bash script, the run's name,
        # a bounded wall time and the attribution comment.
        [(_m, _p, body)] = [call for call in api.calls if call[0] == "POST"]
        assert body["script"].startswith("#!/bin/bash\n")
        assert f"echo {_MARKER}" in body["script"]
        assert body["name"] == _NAME and body["time_limit"] == "00:15:00"
        assert body["comment"] == "live-release-validation"
        # The output read is the real CLI, pinned to the Region and backend.
        assert runs == [
            [
                sys.executable,
                "-m",
                "cli.main",
                "--output",
                "json",
                "slurm",
                "output",
                "42",
                "-r",
                _REGION,
                "--backend",
                "pcs",
            ]
        ]

    def test_both_backends_require_a_selection(self) -> None:
        ctx = _ctx()
        slinky = {"name": "slinky", "kind": "slinky"}
        script = _happy_script(backends=_backends(slinky, _pcs_backend()))
        evidence, _api, _ = _probe(
            ctx, script, lambda *_a, **_k: _completed(stdout=_output_document())
        )
        assert evidence["backend"]["backends"] == ["pcs", "slinky"]
        assert evidence["backend"]["selection_required"] is True
        assert evidence["backend"]["default"] is None

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            (_backends({"name": "slinky", "kind": "slinky"}), "does not list pcs"),
            (_backends({"name": "pcs", "error": "503 cluster CREATING"}), "reports an error"),
            (_backends(_pcs_backend(status="CREATING")), "status is 'CREATING'"),
            (_backends(_pcs_backend(rest_mode="NONE")), "rest_mode is 'NONE'"),
            (_backends(_pcs_backend(slurm_version="25.05")), "is not one the API speaks"),
            (_backends(_pcs_backend(cluster_name="other")), "cluster_name is 'other'"),
            (_backends(_pcs_backend(slurm_version="26.05")), "the block asked for '25.11'"),
            (
                {**_backends(_pcs_backend()), "selection_required": True},
                "selection_required is True",
            ),
            ({**_backends(_pcs_backend()), "default": None}, "default is None"),
        ],
    )
    def test_an_unusable_backend_listing_fails(self, payload: dict[str, Any], message: str) -> None:
        ctx = _ctx()
        ctx.aws_client.make_authenticated_request.side_effect = _Api(
            [("GET", "/api/v1/slurm/backends", payload)]
        )
        with pytest.raises(checks_pcs.PcsValidationError, match=message):
            checks_pcs.probe_pcs(ctx)

    @pytest.mark.parametrize(
        ("response", "message"),
        [
            ({"backend": "pcs"}, "returned no job id"),
            ({"job_id": 42, "backend": "slinky"}, "answered by backend 'slinky'"),
        ],
    )
    def test_a_wrong_submission_answer_fails(self, response: dict[str, Any], message: str) -> None:
        ctx = _ctx()
        ctx.aws_client.make_authenticated_request.side_effect = _Api(
            [
                ("GET", "/api/v1/slurm/backends", _backends(_pcs_backend())),
                ("POST", "/api/v1/slurm/jobs?backend=pcs", response),
            ]
        )
        with pytest.raises(checks_pcs.PcsValidationError, match=message):
            checks_pcs.probe_pcs(ctx)

    def test_a_job_that_ends_badly_or_never_ends_fails(self) -> None:
        ctx = _ctx()
        ctx.aws_client.make_authenticated_request.side_effect = _Api(
            [
                ("GET", "/api/v1/slurm/backends", _backends(_pcs_backend())),
                ("POST", "/api/v1/slurm/jobs?backend=pcs", {"job_id": 42, "backend": "pcs"}),
                (
                    "GET",
                    "/api/v1/slurm/jobs/42?backend=pcs",
                    _job(["FAILED"], reason="NonZeroExitCode", exit_code=1),
                ),
            ]
        )
        with pytest.raises(checks_pcs.PcsValidationError, match=r"ended FAILED .*NonZeroExitCode"):
            checks_pcs.probe_pcs(ctx)

        ctx = _ctx(job_timeout_seconds=0)
        ctx.aws_client.make_authenticated_request.side_effect = _Api(
            [
                ("GET", "/api/v1/slurm/backends", _backends(_pcs_backend())),
                ("POST", "/api/v1/slurm/jobs?backend=pcs", {"job_id": 42, "backend": "pcs"}),
                ("GET", "/api/v1/slurm/jobs/42?backend=pcs", _job(["PENDING"], reason="Priority")),
            ]
        )
        with pytest.raises(checks_pcs.PcsValidationError, match=r"did not complete within 0s"):
            checks_pcs.probe_pcs(ctx)

    def test_accounting_must_record_completion_with_the_attribution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accounting = (
            f"/api/v1/slurm/accounting/jobs?backend=pcs&start=now-6hours&job_name={_NAME}&limit=200"
        )
        head = [
            ("GET", "/api/v1/slurm/backends", _backends(_pcs_backend())),
            ("POST", "/api/v1/slurm/jobs?backend=pcs", {"job_id": 42, "backend": "pcs"}),
            ("GET", "/api/v1/slurm/jobs/42?backend=pcs", _job(["COMPLETED"])),
        ]
        for row, message in (
            (_row(state="CANCELLED"), "records PCS job 42 as"),
            (_row(comment="someone-else"), "lost the caller attribution"),
        ):
            ctx = _ctx()
            ctx.aws_client.make_authenticated_request.side_effect = _Api(
                [*head, ("GET", accounting, _accounting(row))]
            )
            with pytest.raises(checks_pcs.PcsValidationError, match=message):
                checks_pcs.probe_pcs(ctx)
        # A record that never arrives.
        monkeypatch.setattr(checks_pcs, "_ACCOUNTING_WAIT_SECONDS", 0)
        ctx = _ctx()
        ctx.aws_client.make_authenticated_request.side_effect = _Api(
            [*head, ("GET", accounting, _accounting())]
        )
        with pytest.raises(checks_pcs.PcsValidationError, match="has no COMPLETED record"):
            checks_pcs.probe_pcs(ctx)

    def test_the_output_read_retries_transient_failures_and_requires_the_marker(self) -> None:
        ctx = _ctx()
        answers = iter(
            [
                _completed(returncode=1, stderr="InvalidInstanceId: not yet managed"),
                _completed(stdout=_output_document()),
            ]
        )
        evidence, _api, _ = _probe(ctx, _happy_script(), lambda *_a, **_k: next(answers))
        assert [a["returncode"] for a in evidence["output"]["attempts"]] == [1, 0]
        assert evidence["output"]["attempts"][0]["stderr"].startswith("InvalidInstanceId")

        with pytest.raises(checks_pcs.PcsValidationError, match="does not carry the marker"):
            _probe(
                _ctx(), _happy_script(), lambda *_a, **_k: _completed(stdout=_output_document(""))
            )
        with pytest.raises(checks_pcs.PcsValidationError, match="printed non-JSON"):
            _probe(_ctx(), _happy_script(), lambda *_a, **_k: _completed(stdout="not json"))
        with pytest.raises(checks_pcs.PcsValidationError, match="failed 4 times for job 42"):
            _probe(
                _ctx(),
                _happy_script(),
                lambda *_a, **_k: _completed(returncode=1, stderr="AccessDenied"),
            )

    def test_the_output_read_is_skipped_without_a_managed_login_node(self) -> None:
        ctx = _ctx(cdk_context={"pcs": {"managed": {"login": {"enabled": False}}}})
        script = _happy_script()
        evidence, _api, subprocess_run = _probe(ctx, script, MagicMock())
        subprocess_run.assert_not_called()
        assert evidence["output"]["status"] == "skipped"
        assert "no managed login node" in evidence["output"]["reason"]

    def test_an_existing_cluster_is_probed_without_the_managed_expectations(self) -> None:
        # A sibling harness pointing at an operator-run cluster: the name and
        # version come from the cluster, and there is no login node to read.
        ctx = _ctx(
            cdk_context={"pcs": {"enabled": True, "mode": "existing", "cluster_id": "pcs_mine"}},
            overrides="",
        )
        backends = _backends(_pcs_backend(cluster_name="their-hpc", slurm_version="26.05"))
        evidence, _api, subprocess_run = _probe(ctx, _happy_script(backends=backends), MagicMock())
        subprocess_run.assert_not_called()
        assert evidence["mode"] == "existing"
        assert evidence["backend"]["cluster_name"] == "their-hpc"
        assert evidence["output"]["status"] == "skipped"

    def test_api_transport_failures_are_named(self) -> None:
        for response, message in (
            (_response({"detail": "boom"}, status=503), "answered HTTP 503"),
            (_bad_json_response(), "answered non-JSON"),
            (_response(["list"]), "answered list"),
        ):
            ctx = _ctx()
            ctx.aws_client.make_authenticated_request.side_effect = _Api(
                [("GET", "/api/v1/slurm/backends", response)]
            )
            with pytest.raises(checks_pcs.PcsValidationError, match=message):
                checks_pcs.probe_pcs(ctx)


# ---------------------------------------------------------------------------
# The preflight action's PCS step
# ---------------------------------------------------------------------------


def _report(region: str, *checks: tuple[str, str, str]) -> PreflightReport:
    return PreflightReport(
        region=region,
        mode="managed",
        enabled=True,
        cluster=f"{_PROJECT}-pcs",
        checks=[Check(*check) for check in checks],
    )


class TestPreflightStep:
    def test_every_region_with_pcs_on_is_checked_with_the_merged_block(self) -> None:
        ctx = _ctx(
            regions=(_REGION, "eu-west-1"),
            cdk_context={"pcs_regions": {"eu-west-1": {"enabled": False}}},
        )
        reports = {
            _REGION: _report(_REGION, ("pcs-service", PASS, "answers"), ("spot-role", WARN, "?")),
            "eu-west-1": _report("eu-west-1", ("pcs-service", PASS, "answers")),
        }
        calls: list[tuple[str, dict[str, Any], str, Any]] = []

        def fake_run_preflight(region, config, *, project, client_factory):
            calls.append((region, config, project, client_factory))
            return reports[region]

        with patch_live_validation_helper("run_preflight", side_effect=fake_run_preflight):
            summary = actions_preflight._pcs_preflight(ctx)

        # The override turns PCS on everywhere, the overlay notwithstanding.
        assert [call[0] for call in calls] == [_REGION, "eu-west-1"]
        assert all(call[1]["enabled"] is True for call in calls)
        assert all(call[1]["managed"]["metrics"]["enabled"] is True for call in calls)
        assert all(call[2] == _PROJECT for call in calls)
        assert all(call[3] is ctx.session.client for call in calls)
        assert summary["ok"] is True
        assert [r["region"] for r in summary["regions"]] == [_REGION, "eu-west-1"]
        assert summary["regions_without_pcs"] == []

    def test_regions_without_pcs_are_recorded_and_not_checked(self) -> None:
        ctx = _ctx(regions=(_REGION, "eu-west-1"), overrides="")
        ctx.cdk_context["pcs_regions"] = {_REGION: {"enabled": True}}
        with patch_live_validation_helper(
            "run_preflight", return_value=_report(_REGION, ("pcs-service", PASS, "ok"))
        ) as run:
            summary = actions_preflight._pcs_preflight(ctx)
        assert run.call_count == 1
        assert summary["regions_without_pcs"] == ["eu-west-1"]
        # Nothing on anywhere: an empty, passing summary.
        ctx = _ctx(overrides="")
        with patch_live_validation_helper("run_preflight") as run:
            assert actions_preflight._pcs_preflight(ctx) == {
                "ok": True,
                "regions": [],
                "regions_without_pcs": [_REGION],
            }
        run.assert_not_called()

    def test_a_failure_names_the_check_and_stops_the_run(self) -> None:
        ctx = _ctx()
        report = _report(
            _REGION,
            ("pcs-service", PASS, "answers"),
            ("instance-types", FAIL, "c6i.large (gco-compute) is not available in us-east-1"),
        )
        with (
            patch_live_validation_helper("run_preflight", return_value=report),
            pytest.raises(RuntimeError, match=r"instance-types: c6i.large .* not available"),
        ):
            actions_preflight._pcs_preflight(ctx)


# ---------------------------------------------------------------------------
# Waiting for PCS nodes to terminate after teardown
# ---------------------------------------------------------------------------


def _ec2_pages(*instances: tuple[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "Reservations": [
                {
                    "Instances": [
                        {"InstanceId": instance_id, "State": {"Name": state}}
                        for instance_id, state in instances
                    ]
                }
            ]
        }
    ]


def _ec2_session(responses: list[list[dict[str, Any]]]) -> tuple[MagicMock, MagicMock]:
    paginator = MagicMock()
    paginator.paginate.side_effect = lambda **_kwargs: responses.pop(0)
    client = MagicMock()
    client.get_paginator.return_value = paginator
    session = MagicMock()
    session.client.return_value = client
    return session, paginator


class TestPcsNodeDrain:
    def test_waits_until_the_shutting_down_nodes_are_gone(self) -> None:
        ctx = _ctx()
        ctx.session, paginator = _ec2_session(
            [
                _ec2_pages(("i-aaa", "shutting-down"), ("i-bbb", "running")),
                _ec2_pages(("i-aaa", "shutting-down")),
                _ec2_pages(),
            ]
        )
        result = cleanup_pcs_nodes._wait_for_pcs_nodes_terminated(ctx, timeout_seconds=60)

        assert result["regions"][_REGION] == {
            "initial_instances": [
                {"instance_id": "i-aaa", "state": "shutting-down"},
                {"instance_id": "i-bbb", "state": "running"},
            ],
            "remaining_instances": [],
            "polls": 2,
            "terminated": True,
        }
        filters = paginator.paginate.call_args.kwargs["Filters"]
        assert {"Name": "tag:gco:project", "Values": [_PROJECT]} in filters
        assert {"Name": "tag-key", "Values": ["aws:pcs:compute-node-group-id"]} in filters
        assert {
            "Name": "instance-state-name",
            "Values": ["pending", "running", "stopping", "stopped", "shutting-down"],
        } in filters
        ctx.session.client.assert_called_with("ec2", region_name=_REGION)

    def test_nothing_to_wait_for(self) -> None:
        ctx = _ctx(regions=(_REGION, "eu-west-1"))
        ctx.session, _paginator = _ec2_session([_ec2_pages(), _ec2_pages()])
        result = cleanup_pcs_nodes._wait_for_pcs_nodes_terminated(ctx)
        assert all(
            entry["terminated"] and entry["polls"] == 0 for entry in result["regions"].values()
        )
        assert set(result["regions"]) == {_REGION, "eu-west-1"}

    def test_nodes_still_live_at_the_deadline_are_named(self) -> None:
        ctx = _ctx()
        ctx.session, _paginator = _ec2_session(
            [_ec2_pages(("i-aaa", "shutting-down")), _ec2_pages(("i-aaa", "shutting-down"))]
        )
        with pytest.raises(
            RuntimeError, match=r"still live after 0s: us-east-1: i-aaa \(shutting-down\)"
        ):
            cleanup_pcs_nodes._wait_for_pcs_nodes_terminated(ctx, timeout_seconds=0)

    def test_an_instance_without_an_id_fails_closed(self) -> None:
        ctx = _ctx()
        ctx.session, _paginator = _ec2_session(
            [[{"Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]}]]
        )
        with pytest.raises(RuntimeError, match="instance without an ID"):
            cleanup_pcs_nodes._wait_for_pcs_nodes_terminated(ctx)
