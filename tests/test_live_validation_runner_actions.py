"""Offline coverage for the live-release-validation runner, models, CLI, and actions.

Covers ``scripts/live_release_validation/{runner,models,context,artifact_io,
cli_args,__main__,inference_contract}.py`` and the action modules
``actions/{baseline,central_queue,convergence,deploy,destroy,final_inventory,
inference,jobs,preflight,topology}.py``. Every AWS, git, subprocess, signal,
clock, and Kubernetes boundary is faked; the runner is driven end to end
against a throwaway repository root under ``tmp_path`` with a fake action
registry. The behaviours pinned here are: guaranteed cleanup plus reporting
once runtime construction succeeds, checkpoint identity and resume rules,
signal and KeyboardInterrupt exit codes, strict ``cdk.json`` parsing,
owner-only artifact I/O fallbacks, CLI argument validation and the blocked
recovery report, the ``RunContext`` Job-record state machine, and each
action's fail-closed error branches.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import signal
import stat
import subprocess
import sys
from collections import UserDict
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts.live_release_validation import __main__ as live_main
from scripts.live_release_validation import (
    artifact_io,
    cli_args,
    context,
    inference_contract,
    models,
    runner,
)
from scripts.live_release_validation.actions import baseline as actions_baseline
from scripts.live_release_validation.actions import central_queue as actions_central_queue
from scripts.live_release_validation.actions import convergence as actions_convergence
from scripts.live_release_validation.actions import deploy as actions_deploy
from scripts.live_release_validation.actions import destroy as actions_destroy
from scripts.live_release_validation.actions import final_inventory as actions_final_inventory
from scripts.live_release_validation.actions import inference as actions_inference
from scripts.live_release_validation.actions import jobs as actions_jobs
from scripts.live_release_validation.actions import preflight as actions_preflight
from scripts.live_release_validation.actions import topology as actions_topology
from scripts.live_release_validation.checks import central_queue as checks_central_queue
from scripts.live_release_validation.checks.inference import ManagedInferenceValidationError
from scripts.live_release_validation.constants import _RUN_JOB_LABEL
from scripts.live_release_validation.models import (
    ActionResult,
    RunCheckpoint,
    RunContext,
    RunSettings,
    ValidationReport,
    atomic_write_json,
)
from scripts.live_release_validation.registry import ActionDefinition, build_action_registry
from tests._live_validation_patching import patch_live_validation_helper
from tests.test_live_release_validation import _central_job, _context, _real_context, _response
from tests.test_live_validation_inference import _runtime
from tests.test_live_validation_inference import _settings as _inference_settings

_ACCOUNT = "123456789012"
_SHA = "a" * 40
_EFS_CONTEXT = {"gco_live_validation_disable_efs_automatic_backups": "true"}
_BASELINE = {"protected_stacks": {}, "ecr_regions": ["us-east-1"], "ecr_repositories": {}}


# ---------------------------------------------------------------------------
# Runner fixtures: a real LiveValidationRunner over a fake repository root.
# ---------------------------------------------------------------------------


def _cdk_context(regional: tuple[str, ...] = ("us-east-1",)) -> dict[str, Any]:
    return {
        "project_name": "gco-live",
        "deployment_regions": {
            "global": "us-west-2",
            "api_gateway": "us-east-1",
            "monitoring": "us-east-1",
            "regional": list(regional),
        },
    }


def _write_cdk_json(root: Path, payload: Any = None) -> Path:
    path = root / "cdk.json"
    if payload is None:
        payload = {"context": _cdk_context()}
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


def _run_settings(tmp_path: Path, **overrides: Any) -> RunSettings:
    report_dir = tmp_path / "report"
    fields: dict[str, Any] = {
        "run_id": "run-123",
        "repo_root": tmp_path,
        "report_dir": report_dir,
        "checkpoint_path": report_dir / "checkpoint.json",
        "expected_account": _ACCOUNT,
        "expected_sha": _SHA,
        "expected_branch": "chore/test",
        "profile": "configured",
        "requested_actions": ("all",),
    }
    fields.update(overrides)
    return RunSettings(**fields)


@contextlib.contextmanager
def _runner_boundaries() -> Iterator[SimpleNamespace]:
    """Replace every AWS-facing client the runner constructs with mocks."""
    with (
        patch.object(runner, "ThrottleResilientSession") as session,
        patch.object(runner, "GCOAWSClient") as aws_client,
        patch.object(runner, "JobManager") as job_manager,
        patch.object(runner, "StackManager") as stack_manager,
    ):
        yield SimpleNamespace(
            session=session,
            aws_client=aws_client,
            job_manager=job_manager,
            stack_manager=stack_manager,
        )


def _evidence_handler(name: str) -> Callable[[RunContext], dict[str, Any]]:
    def handler(ctx: RunContext) -> dict[str, Any]:
        return {"action": name}

    return handler


def _record_baseline(ctx: RunContext) -> dict[str, Any]:
    ctx.checkpoint.baseline = dict(_BASELINE)
    return {"baseline": True}


def _attempt_deploy(ctx: RunContext) -> dict[str, Any]:
    ctx.checkpoint.deployment_attempted = True
    ctx.checkpoint.destroyed = False
    ctx.persist()
    return {"deployed": True}


def _mark_destroyed(ctx: RunContext) -> dict[str, Any]:
    ctx.checkpoint.destroyed = True
    return {"destroyed": True}


def _fake_registry(
    handlers: dict[str, Callable[[RunContext], dict[str, Any]]] | None = None,
) -> dict[str, ActionDefinition]:
    """The real registry names/dependencies with offline evidence handlers."""
    defaults: dict[str, Callable[[RunContext], dict[str, Any]]] = {
        "baseline": _record_baseline,
        "deploy": _attempt_deploy,
        "destroy": _mark_destroyed,
    }
    defaults.update(handlers or {})
    return {
        name: dataclasses.replace(
            definition,
            handler=defaults.get(name) or _evidence_handler(name),
        )
        for name, definition in build_action_registry().items()
    }


def _build_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: RunSettings | None = None,
    registry: dict[str, ActionDefinition] | None = None,
) -> runner.LiveValidationRunner:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    # The constructor chdirs into repo_root; monkeypatch restores the original
    # working directory even for tests that never reach run().
    monkeypatch.chdir(tmp_path)
    if not (tmp_path / "cdk.json").exists():
        _write_cdk_json(tmp_path)
    with _runner_boundaries():
        return runner.LiveValidationRunner(
            settings or _run_settings(tmp_path),
            registry=registry if registry is not None else _fake_registry(),
        )


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class _NoExtraContextSettings(RunSettings):
    """A sibling-harness settings type that threads no extra CDK context."""

    def extra_cdk_context(self) -> dict[str, str]:
        return {}


class TestRunnerConstruction:
    def test_constructor_wires_clients_checkpoint_and_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.chdir(tmp_path)
        _write_cdk_json(tmp_path, {"context": _cdk_context(("us-east-1", "eu-west-1"))})
        settings = _run_settings(tmp_path, optional_schedulers=("yunikorn",))

        with _runner_boundaries() as boundaries:
            instance = runner.LiveValidationRunner(settings)

        assert instance.deployment_regions == ("us-east-1", "eu-west-1")
        assert instance.config.project_name == "gco-live"
        assert instance.config.default_region == "us-east-1"
        assert instance.config.global_region == "us-west-2"
        assert instance.config.api_gateway_region == "us-east-1"
        assert instance.config.output_format == "json"
        assert instance.selected_actions == tuple(build_action_registry())
        assert instance.registry.keys() == build_action_registry().keys()
        assert settings.checkpoint_path.is_file()
        assert stat.S_IMODE(settings.checkpoint_path.stat().st_mode) == 0o600
        assert _read_json(settings.checkpoint_path)["identity"] == settings.identity()
        assert instance.report.identity == settings.identity()
        assert instance.report.started_at == instance.checkpoint.created_at
        assert instance.report.selected_actions == list(instance.selected_actions)
        boundaries.aws_client.assert_called_once_with(instance.config)
        assert instance.aws_client._session is instance.session
        boundaries.job_manager.assert_called_once_with(instance.config)
        assert instance.job_manager._aws_client is instance.aws_client
        boundaries.stack_manager.assert_called_once_with(instance.config, project_root=tmp_path)
        instance.stack_manager.set_extra_cdk_context.assert_called_once_with(
            {**_EFS_CONTEXT, "helm_enabled_overrides": "yunikorn"}
        )
        assert instance.context.settings is settings
        assert instance.context.checkpoint is instance.checkpoint
        assert instance.context.persist_callback == instance._persist_checkpoint
        assert instance._identity_verified is False
        assert Path.cwd().resolve() == tmp_path.resolve()

    def test_sibling_settings_without_extra_context_skip_cdk_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report_dir = tmp_path / "report"
        settings = _NoExtraContextSettings(
            run_id="run-123",
            repo_root=tmp_path,
            report_dir=report_dir,
            checkpoint_path=report_dir / "checkpoint.json",
            expected_account=_ACCOUNT,
            expected_sha=_SHA,
            expected_branch="chore/test",
            profile="configured",
            requested_actions=("preflight",),
        )

        instance = _build_runner(tmp_path, monkeypatch, settings=settings)

        instance.stack_manager.set_extra_cdk_context.assert_not_called()
        assert instance.selected_actions == ("preflight",)

    def test_constructor_failure_restores_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        _write_cdk_json(tmp_path, "{not json")
        settings = _run_settings(tmp_path)

        with _runner_boundaries(), pytest.raises(ValueError, match="Unable to read"):
            runner.LiveValidationRunner(settings)

        assert Path.cwd().resolve() == elsewhere.resolve()
        assert not settings.checkpoint_path.exists()

    @pytest.mark.parametrize(
        ("payload", "match"),
        [
            ("{not json", "Unable to read"),
            (["not", "an", "object"], "context must be an object"),
            ({"context": "text"}, "context must be an object"),
            (
                {"context": {"deployment_regions": _cdk_context()["deployment_regions"]}},
                "project_name must be a non-empty string",
            ),
            (
                {"context": {"project_name": "", "deployment_regions": {}}},
                "project_name must be a non-empty string",
            ),
            (
                {"context": {"project_name": "gco", "deployment_regions": []}},
                "deployment_regions must be an object",
            ),
            (
                {
                    "context": {
                        "project_name": "gco",
                        "deployment_regions": {
                            "global": "",
                            "api_gateway": "us-east-1",
                            "monitoring": "us-east-1",
                            "regional": ["us-east-1"],
                        },
                    }
                },
                r"deployment_regions\.global must be non-empty",
            ),
            (
                {
                    "context": {
                        "project_name": "gco",
                        "deployment_regions": {
                            "global": "us-west-2",
                            "api_gateway": "us-east-1",
                            "monitoring": "us-east-1",
                        },
                    }
                },
                "regional must be a non-empty list",
            ),
            (
                {
                    "context": {
                        "project_name": "gco",
                        "deployment_regions": {
                            "global": "us-west-2",
                            "api_gateway": "us-east-1",
                            "monitoring": "us-east-1",
                            "regional": [],
                        },
                    }
                },
                "regional must be a non-empty list",
            ),
            (
                {
                    "context": {
                        "project_name": "gco",
                        "deployment_regions": {
                            "global": "us-west-2",
                            "api_gateway": "us-east-1",
                            "monitoring": "us-east-1",
                            "regional": ["us-east-1", 7],
                        },
                    }
                },
                "regional must be a non-empty list",
            ),
            (
                {
                    "context": {
                        "project_name": "gco",
                        "deployment_regions": {
                            "global": "us-west-2",
                            "api_gateway": "us-east-1",
                            "monitoring": "us-east-1",
                            "regional": ["us-east-1", "us-east-1"],
                        },
                    }
                },
                "contains duplicates",
            ),
        ],
    )
    def test_cdk_json_is_validated_strictly(self, tmp_path: Path, payload: Any, match: str) -> None:
        _write_cdk_json(tmp_path, payload)

        with pytest.raises(ValueError, match=match):
            runner.LiveValidationRunner._load_cdk_context(tmp_path)

    def test_missing_cdk_json_is_reported_as_unreadable(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unable to read"):
            runner.LiveValidationRunner._load_cdk_context(tmp_path)

    def test_valid_cdk_json_returns_context_and_regional_tuple(self, tmp_path: Path) -> None:
        _write_cdk_json(tmp_path, {"context": _cdk_context(("us-east-1", "eu-west-1"))})

        loaded, regional = runner.LiveValidationRunner._load_cdk_context(tmp_path)

        assert loaded == _cdk_context(("us-east-1", "eu-west-1"))
        assert regional == ("us-east-1", "eu-west-1")


class TestRunnerCheckpointLoading:
    def test_resume_requires_an_existing_checkpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValueError, match="--resume requires an existing checkpoint"):
            _build_runner(tmp_path, monkeypatch, settings=_run_settings(tmp_path, resume=True))

    def test_resume_rejects_a_checkpoint_with_a_different_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _run_settings(tmp_path, resume=True)
        other = settings.identity()
        other["expected_sha"] = "b" * 40
        atomic_write_json(settings.checkpoint_path, RunCheckpoint(identity=other).to_dict())

        with pytest.raises(ValueError, match="identity does not match"):
            _build_runner(tmp_path, monkeypatch, settings=settings)

    def test_fresh_run_refuses_to_overwrite_an_existing_checkpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _run_settings(tmp_path)
        atomic_write_json(
            settings.checkpoint_path,
            RunCheckpoint(identity=settings.identity()).to_dict(),
        )

        with pytest.raises(ValueError, match="Checkpoint already exists"):
            _build_runner(tmp_path, monkeypatch, settings=settings)

    def test_resume_loads_completed_actions_into_the_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _run_settings(tmp_path, resume=True)
        previous = RunCheckpoint(identity=settings.identity())
        previous.completed_actions.append("preflight")
        previous.action_results["preflight"] = ActionResult(
            name="preflight",
            description="d",
            status="passed",
            started_at="s",
            ended_at="e",
            duration_seconds=1.5,
            details={"account": _ACCOUNT},
        )
        previous.baseline = dict(_BASELINE)
        atomic_write_json(settings.checkpoint_path, previous.to_dict())

        instance = _build_runner(tmp_path, monkeypatch, settings=settings)

        assert instance.checkpoint.completed_actions == ["preflight"]
        assert instance.checkpoint.baseline == _BASELINE
        assert [result.name for result in instance.report.action_results] == ["preflight"]
        assert instance.report.action_results[0].details == {"account": _ACCOUNT}
        assert instance.report.baseline == _BASELINE
        assert instance.report.started_at == previous.created_at


class TestRunnerActionResolution:
    def test_dependencies_are_expanded_in_registry_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = _build_runner(
            tmp_path,
            monkeypatch,
            settings=_run_settings(tmp_path, requested_actions=("api", "topology")),
        )

        assert instance.selected_actions == ("preflight", "baseline", "deploy", "topology", "api")

    def test_empty_request_means_the_whole_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = _build_runner(
            tmp_path, monkeypatch, settings=_run_settings(tmp_path, requested_actions=())
        )

        assert instance.selected_actions == tuple(build_action_registry())

    def test_all_cannot_be_combined_with_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValueError, match="'all' cannot be combined"):
            _build_runner(
                tmp_path,
                monkeypatch,
                settings=_run_settings(tmp_path, requested_actions=("all", "preflight")),
            )

    def test_unknown_actions_are_listed_with_the_available_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValueError, match="Unknown actions: bogus, zed. Available: preflight"):
            _build_runner(
                tmp_path,
                monkeypatch,
                settings=_run_settings(tmp_path, requested_actions=("zed", "bogus")),
            )

    def test_deploy_dependent_actions_are_derived_transitively(self) -> None:
        registry = build_action_registry()

        derived = runner.LiveValidationRunner._derive_deploy_dependent_actions(registry)

        assert derived == frozenset(registry) - {
            "preflight",
            "baseline",
            "destroy",
            "final-inventory",
        }

    def test_dependency_cycles_and_foreign_dependencies_do_not_recurse_forever(self) -> None:
        def handler(ctx: RunContext) -> dict[str, Any]:
            return {}

        registry = {
            "deploy": ActionDefinition("deploy", "deploy", (), handler),
            "ping": ActionDefinition("ping", "ping", ("pong",), handler),
            "pong": ActionDefinition("pong", "pong", ("ping", "missing"), handler),
            "verify": ActionDefinition("verify", "verify", ("deploy", "ping"), handler),
            "destroy": ActionDefinition("destroy", "destroy", ("deploy",), handler),
            "final-inventory": ActionDefinition("final-inventory", "fi", ("destroy",), handler),
        }

        derived = runner.LiveValidationRunner._derive_deploy_dependent_actions(registry)

        assert derived == frozenset({"deploy", "verify"})


class TestRunnerExecuteAction:
    def test_passing_action_is_checkpointed_and_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        instance = _build_runner(tmp_path, monkeypatch)
        definition = instance.registry["preflight"]

        details = instance._execute_action(definition)

        assert details == {"action": "preflight"}
        assert instance.checkpoint.completed_actions == ["preflight"]
        result = instance.checkpoint.action_results["preflight"]
        assert result.status == "passed"
        assert result.details == {"action": "preflight"}
        persisted = _read_json(instance.settings.checkpoint_path)
        assert persisted["completed_actions"] == ["preflight"]
        assert persisted["action_results"]["preflight"]["status"] == "passed"
        report = _read_json(instance.settings.report_dir / "live-release-validation.json")
        assert [entry["name"] for entry in report["action_results"]] == ["preflight"]
        assert (instance.settings.report_dir / "live-release-validation.md").is_file()
        output = capsys.readouterr().out
        assert "[run] preflight:" in output
        assert "[pass] preflight" in output

    def test_completed_actions_are_skipped_unless_preflight_or_forced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        baseline_handler = MagicMock(return_value={"fresh": True})
        preflight_handler = MagicMock(return_value={"verified": True})
        instance = _build_runner(
            tmp_path,
            monkeypatch,
            registry=_fake_registry({"baseline": baseline_handler, "preflight": preflight_handler}),
        )
        for name in ("preflight", "baseline"):
            instance.checkpoint.completed_actions.append(name)
            instance.checkpoint.action_results[name] = ActionResult(
                name=name,
                description="d",
                status="passed",
                started_at="s",
                ended_at="e",
                duration_seconds=0.0,
                details={"checkpointed": name},
            )

        assert instance._execute_action(instance.registry["baseline"]) == {
            "checkpointed": "baseline"
        }
        baseline_handler.assert_not_called()
        assert "[skip] baseline: checkpoint already passed" in capsys.readouterr().out

        assert instance._execute_action(instance.registry["preflight"]) == {"verified": True}
        preflight_handler.assert_called_once_with(instance.context)

        assert instance._execute_action(instance.registry["baseline"], always_run=True) == {
            "fresh": True
        }
        baseline_handler.assert_called_once_with(instance.context)
        assert instance.checkpoint.completed_actions == ["preflight", "baseline"]

    def test_incomplete_deploy_dependent_action_cannot_resume_after_teardown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler = MagicMock()
        instance = _build_runner(
            tmp_path, monkeypatch, registry=_fake_registry({"topology": handler})
        )
        instance.checkpoint.destroyed = True

        with pytest.raises(RuntimeError, match="Cannot resume incomplete action 'topology'"):
            instance._execute_action(instance.registry["topology"])

        handler.assert_not_called()
        assert instance._execute_action(instance.registry["baseline"]) == {"baseline": True}

    def test_failed_action_records_error_and_drops_completion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def explode(ctx: RunContext) -> dict[str, Any]:
            raise RuntimeError("HEAD moved")

        instance = _build_runner(
            tmp_path, monkeypatch, registry=_fake_registry({"preflight": explode})
        )
        instance.checkpoint.completed_actions.append("preflight")

        with pytest.raises(RuntimeError, match="HEAD moved"):
            instance._execute_action(instance.registry["preflight"])

        assert instance.checkpoint.completed_actions == []
        result = instance.checkpoint.action_results["preflight"]
        assert result.status == "failed"
        assert result.error == "RuntimeError: HEAD moved"
        assert "HEAD moved" in (result.traceback or "")
        persisted = _read_json(instance.settings.checkpoint_path)
        assert persisted["action_results"]["preflight"]["error"] == "RuntimeError: HEAD moved"
        report = _read_json(instance.settings.report_dir / "live-release-validation.json")
        assert report["action_results"][0]["status"] == "failed"
        assert "[fail] preflight: RuntimeError: HEAD moved" in capsys.readouterr().out


class TestRunnerGuaranteedCleanup:
    def test_nothing_deployed_needs_no_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = _build_runner(tmp_path, monkeypatch)

        with patch.object(runner, "destroy_deployment") as destroy:
            instance._guaranteed_cleanup()

        destroy.assert_not_called()
        assert instance.report.cleanup == {"needed": False}

    def test_unverified_identity_blocks_automatic_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = _build_runner(tmp_path, monkeypatch)
        instance.checkpoint.deployment_attempted = True

        with patch.object(runner, "destroy_deployment") as destroy:
            instance._guaranteed_cleanup()

        destroy.assert_not_called()
        assert instance.report.cleanup["needed"] is True
        assert instance.report.cleanup["completed"] is False
        assert "unverified identity" in instance.report.cleanup["blocked"]

    def test_successful_destroy_is_recorded_once_without_baseline_inventory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = _build_runner(tmp_path, monkeypatch)
        instance.checkpoint.deployment_attempted = True
        instance._identity_verified = True
        details = {"needed": True, "attempts": [{"sequence": 1}]}

        with (
            patch.object(runner, "destroy_deployment", return_value=details) as destroy,
            patch.object(runner, "action_final_inventory") as final_inventory,
        ):
            instance._guaranteed_cleanup()

        destroy.assert_called_once_with(instance.context)
        final_inventory.assert_not_called()
        assert instance.report.cleanup == {"completed": True, **details}
        assert instance.checkpoint.completed_actions == ["destroy"]
        recorded = instance.checkpoint.action_results["destroy"]
        assert recorded.status == "passed"
        assert recorded.details == details
        assert recorded.duration_seconds == 0.0
        assert recorded.description == build_action_registry()["destroy"].description
        assert _read_json(instance.settings.checkpoint_path)["completed_actions"] == ["destroy"]

    def test_destroy_failure_is_reported_with_checkpointed_attempts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = _build_runner(tmp_path, monkeypatch)
        instance.checkpoint.deployment_attempted = True
        instance._identity_verified = True
        instance.checkpoint.state["destroy_attempts"] = [{"sequence": 1, "error": "cdk"}]
        instance.checkpoint.state["workload_cleanup_attempts"] = [{"job": "x"}]

        with patch.object(
            runner, "destroy_deployment", side_effect=RuntimeError("teardown stalled")
        ):
            instance._guaranteed_cleanup()

        cleanup = instance.report.cleanup
        assert cleanup["needed"] is True
        assert cleanup["completed"] is False
        assert cleanup["error"] == "RuntimeError: teardown stalled"
        assert "teardown stalled" in cleanup["traceback"]
        assert cleanup["attempts"] == [{"sequence": 1, "error": "cdk"}]
        assert cleanup["workload_cleanup_attempts"] == [{"job": "x"}]
        assert cleanup["retained_cleanup_attempts"] == []
        assert "destroy" not in instance.checkpoint.completed_actions

    @pytest.mark.parametrize(
        "interruption",
        [KeyboardInterrupt(), runner._LiveValidationSignal(signal.SIGTERM)],
        ids=["keyboard-interrupt", "sigterm"],
    )
    def test_destroy_interruptions_propagate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: BaseException
    ) -> None:
        instance = _build_runner(tmp_path, monkeypatch)
        instance.checkpoint.deployment_attempted = True
        instance._identity_verified = True

        with (
            patch.object(runner, "destroy_deployment", side_effect=interruption),
            pytest.raises(type(interruption)),
        ):
            instance._guaranteed_cleanup()

        assert instance.report.cleanup == {}

    def test_final_inventory_runs_after_destroy_when_a_baseline_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = _build_runner(tmp_path, monkeypatch)
        instance.checkpoint.deployment_attempted = True
        instance.checkpoint.baseline = dict(_BASELINE)
        instance.checkpoint.completed_actions.extend(["destroy", "final-inventory"])
        instance._identity_verified = True
        inventory = {"summary": {"total": 0}}

        with (
            patch.object(runner, "destroy_deployment", return_value={"needed": True}),
            patch.object(runner, "action_final_inventory", return_value=inventory) as final,
        ):
            instance._guaranteed_cleanup()

        final.assert_called_once_with(instance.context)
        assert instance.checkpoint.completed_actions == ["destroy", "final-inventory"]
        recorded = instance.checkpoint.action_results["final-inventory"]
        assert recorded.status == "passed"
        assert recorded.details == inventory
        assert "destroy" not in instance.checkpoint.action_results

    def test_final_inventory_failure_is_checkpointed_as_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = _build_runner(tmp_path, monkeypatch)
        instance.checkpoint.deployment_attempted = True
        instance.checkpoint.baseline = dict(_BASELINE)
        instance.checkpoint.completed_actions.append("final-inventory")
        instance._identity_verified = True

        with (
            patch.object(runner, "destroy_deployment", return_value={"needed": True}),
            patch.object(
                runner, "action_final_inventory", side_effect=RuntimeError("residue remains")
            ),
        ):
            instance._guaranteed_cleanup()

        assert instance.checkpoint.completed_actions == ["destroy"]
        recorded = instance.checkpoint.action_results["final-inventory"]
        assert recorded.status == "failed"
        assert recorded.error == "RuntimeError: residue remains"
        assert "residue remains" in (recorded.traceback or "")
        persisted = _read_json(instance.settings.checkpoint_path)
        assert persisted["action_results"]["final-inventory"]["status"] == "failed"

    @pytest.mark.parametrize(
        "interruption",
        [KeyboardInterrupt(), runner._LiveValidationSignal(signal.SIGHUP)],
        ids=["keyboard-interrupt", "sighup"],
    )
    def test_final_inventory_interruptions_propagate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: BaseException
    ) -> None:
        instance = _build_runner(tmp_path, monkeypatch)
        instance.checkpoint.deployment_attempted = True
        instance.checkpoint.baseline = dict(_BASELINE)
        instance._identity_verified = True

        with (
            patch.object(runner, "destroy_deployment", return_value={"needed": True}),
            patch.object(runner, "action_final_inventory", side_effect=interruption),
            pytest.raises(type(interruption)),
        ):
            instance._guaranteed_cleanup()

        assert "final-inventory" not in instance.checkpoint.action_results


class TestRunnerRun:
    def _reports(self, instance: runner.LiveValidationRunner) -> tuple[dict[str, Any], str]:
        report_dir = instance.settings.report_dir
        return (
            _read_json(report_dir / "live-release-validation.json"),
            (report_dir / "live-release-validation.md").read_text(encoding="utf-8"),
        )

    def test_complete_registry_passes_and_reconciles_teardown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        instance = _build_runner(tmp_path, monkeypatch)
        inventory = {"summary": {"residual": 0}}
        previous_cwd = Path.cwd()

        with (
            patch.object(runner, "destroy_deployment", return_value={"needed": True}) as destroy,
            patch.object(runner, "action_final_inventory", return_value=inventory),
        ):
            exit_code = instance.run()

        assert exit_code == 0
        assert instance.report.status == "passed"
        assert instance.report.ended_at is not None
        destroy.assert_called_once_with(instance.context)
        assert instance.report.cleanup == {"completed": True, "needed": True}
        assert instance.checkpoint.completed_actions == list(build_action_registry())
        assert instance.report.final_inventory == inventory
        report, markdown = self._reports(instance)
        assert report["status"] == "passed"
        assert report["final_inventory"] == inventory
        assert [entry["name"] for entry in report["action_results"]] == list(
            build_action_registry()
        )
        assert "- **Status:** **PASSED**" in markdown
        output = capsys.readouterr().out
        assert "JSON report:" in output
        assert "Markdown report:" in output
        assert Path.cwd() == previous_cwd

    def test_partial_scope_without_deploy_needs_no_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = _build_runner(
            tmp_path,
            monkeypatch,
            settings=_run_settings(tmp_path, requested_actions=("baseline",)),
        )

        with patch.object(runner, "destroy_deployment") as destroy:
            exit_code = instance.run()

        assert exit_code == 0
        destroy.assert_not_called()
        assert instance.report.status == "partial"
        assert instance.report.cleanup == {"needed": False}
        report, markdown = self._reports(instance)
        assert report["status"] == "partial"
        assert "- **Selected action scope:** `preflight`, `baseline`" in markdown

    def test_resume_skips_checkpointed_actions_but_reverifies_preflight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        settings = _run_settings(tmp_path, requested_actions=("baseline",), resume=True)
        previous = RunCheckpoint(identity=settings.identity())
        for name in ("preflight", "baseline"):
            previous.completed_actions.append(name)
            previous.action_results[name] = ActionResult(
                name=name,
                description="d",
                status="passed",
                started_at="s",
                ended_at="e",
                duration_seconds=0.0,
                details={"checkpointed": name},
            )
        atomic_write_json(settings.checkpoint_path, previous.to_dict())
        preflight_handler = MagicMock(return_value={"reverified": True})
        baseline_handler = MagicMock()
        instance = _build_runner(
            tmp_path,
            monkeypatch,
            settings=settings,
            registry=_fake_registry({"preflight": preflight_handler, "baseline": baseline_handler}),
        )

        assert instance.run() == 0

        preflight_handler.assert_called_once_with(instance.context)
        baseline_handler.assert_not_called()
        assert instance.checkpoint.action_results["preflight"].details == {"reverified": True}
        assert instance.checkpoint.action_results["baseline"].details == {
            "checkpointed": "baseline"
        }
        assert "[skip] baseline" in capsys.readouterr().out

    def test_failure_after_deploy_runs_guaranteed_cleanup_and_records_teardown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unhealthy(ctx: RunContext) -> dict[str, Any]:
            raise RuntimeError("stack is ROLLBACK_COMPLETE")

        instance = _build_runner(
            tmp_path, monkeypatch, registry=_fake_registry({"topology": unhealthy})
        )
        inventory = {"summary": {"residual": 0}}
        teardown = {"needed": True, "attempts": [{"sequence": 1}]}

        with (
            patch.object(runner, "destroy_deployment", return_value=teardown) as destroy,
            patch.object(runner, "action_final_inventory", return_value=inventory),
        ):
            exit_code = instance.run()

        assert exit_code == 1
        assert instance.report.status == "failed"
        assert "stack is ROLLBACK_COMPLETE" in (instance.report.fatal_error or "")
        destroy.assert_called_once_with(instance.context)
        assert instance.report.cleanup == {"completed": True, **teardown}
        assert instance.checkpoint.completed_actions == [
            "preflight",
            "baseline",
            "deploy",
            "destroy",
            "final-inventory",
        ]
        statuses = {result.name: result.status for result in instance.report.action_results}
        assert statuses == {
            "preflight": "passed",
            "baseline": "passed",
            "deploy": "passed",
            "topology": "failed",
            "destroy": "passed",
            "final-inventory": "passed",
        }
        assert instance.report.final_inventory == inventory
        report, markdown = self._reports(instance)
        assert report["cleanup"]["completed"] is True
        assert "## Failures" in markdown
        assert "### `topology`" in markdown

    def test_failed_final_inventory_after_cleanup_fails_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = _build_runner(
            tmp_path,
            monkeypatch,
            settings=_run_settings(tmp_path, requested_actions=("destroy",)),
        )

        with (
            patch.object(runner, "destroy_deployment", return_value={"needed": True}),
            patch.object(
                runner, "action_final_inventory", side_effect=RuntimeError("residue remains")
            ),
        ):
            exit_code = instance.run()

        assert exit_code == 1
        assert instance.report.status == "failed"
        assert instance.report.fatal_error is None
        assert instance.report.cleanup == {"completed": True, "needed": True}
        result = instance.checkpoint.action_results["final-inventory"]
        assert result.status == "failed"
        assert instance.report.final_inventory is None
        assert "final-inventory" not in instance.checkpoint.completed_actions

    def test_sigterm_during_an_action_cleans_up_and_exits_with_signal_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        holder: dict[str, runner.LiveValidationRunner] = {}

        def interrupted(ctx: RunContext) -> dict[str, Any]:
            holder["instance"]._handle_signal(signal.SIGTERM, None)
            raise AssertionError("signal handler must interrupt the action")

        instance = _build_runner(
            tmp_path, monkeypatch, registry=_fake_registry({"topology": interrupted})
        )
        holder["instance"] = instance
        previous_handlers = {
            signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP)
        }

        with (
            patch.object(runner, "destroy_deployment", return_value={"needed": True}) as destroy,
            patch.object(runner, "action_final_inventory", return_value={"summary": {}}),
        ):
            exit_code = instance.run()

        assert exit_code == 128 + signal.SIGTERM
        assert instance.report.status == "interrupted"
        assert instance.report.fatal_error == (
            "SIGTERM: validation interrupted; controlled cleanup started"
        )
        assert instance._received_signal == signal.SIGTERM
        destroy.assert_called_once_with(instance.context)
        assert instance.checkpoint.action_results["topology"].status == "failed"
        assert instance.checkpoint.action_results["topology"].error == (
            "_LiveValidationSignal: Received SIGTERM"
        )
        assert instance._previous_signal_handlers == {}
        for signum, previous in previous_handlers.items():
            assert signal.getsignal(signum) == previous
        report, _ = self._reports(instance)
        assert report["status"] == "interrupted"

    def test_keyboard_interrupt_during_an_action_exits_130(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def interrupted(ctx: RunContext) -> dict[str, Any]:
            raise KeyboardInterrupt

        instance = _build_runner(
            tmp_path, monkeypatch, registry=_fake_registry({"baseline": interrupted})
        )

        with patch.object(runner, "destroy_deployment") as destroy:
            exit_code = instance.run()

        assert exit_code == 130
        destroy.assert_not_called()
        assert instance.report.status == "interrupted"
        assert instance.report.fatal_error == "KeyboardInterrupt: validation interrupted"
        assert instance.report.cleanup == {"needed": False}
        assert instance.checkpoint.action_results["baseline"].status == "failed"

    def test_signal_during_cleanup_is_reported_as_a_cleanup_interruption(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = _build_runner(tmp_path, monkeypatch)
        instance.checkpoint.state["destroy_attempts"] = [{"sequence": 1}]

        with patch.object(
            runner,
            "destroy_deployment",
            side_effect=runner._LiveValidationSignal(signal.SIGHUP),
        ):
            exit_code = instance.run()

        assert exit_code == 128 + signal.SIGHUP
        assert instance.report.status == "interrupted"
        assert instance.report.fatal_error == "SIGHUP: validation interrupted during cleanup"
        assert instance.report.cleanup == {
            "needed": True,
            "completed": False,
            "runner_error": "_LiveValidationSignal: Received SIGHUP",
            "workload_cleanup_attempts": [],
            "attempts": [{"sequence": 1}],
            "retained_cleanup_attempts": [],
        }

    def test_keyboard_interrupt_during_cleanup_exits_130(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = _build_runner(tmp_path, monkeypatch)

        with patch.object(runner, "destroy_deployment", side_effect=KeyboardInterrupt):
            exit_code = instance.run()

        assert exit_code == 130
        assert instance.report.status == "interrupted"
        assert instance.report.fatal_error == "KeyboardInterrupt: cleanup interrupted"
        assert instance.report.cleanup["runner_error"] == "KeyboardInterrupt: "

    def test_runner_error_inside_cleanup_fails_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = _build_runner(tmp_path, monkeypatch)

        with patch.object(
            runner.LiveValidationRunner,
            "_guaranteed_cleanup",
            side_effect=RuntimeError("checkpoint disk full"),
        ):
            exit_code = instance.run()

        assert exit_code == 1
        assert instance.report.status == "failed"
        assert instance.report.fatal_error is None
        assert instance.report.cleanup["completed"] is False
        assert instance.report.cleanup["runner_error"] == "RuntimeError: checkpoint disk full"
        report, markdown = self._reports(instance)
        assert report["status"] == "failed"
        assert '"runner_error": "RuntimeError: checkpoint disk full"' in markdown


class TestSignalPlumbing:
    def test_signal_exception_names_the_signal(self) -> None:
        exc = runner._LiveValidationSignal(signal.SIGTERM)

        assert exc.signum == signal.SIGTERM
        assert exc.signal_name == "SIGTERM"
        assert str(exc) == "Received SIGTERM"
        assert isinstance(exc, BaseException)
        assert not isinstance(exc, Exception)

    def test_handlers_are_installed_for_available_signals_and_restored(self) -> None:
        instance = object.__new__(runner.LiveValidationRunner)
        instance._previous_signal_handlers = {}
        instance._received_signal = None
        fake_signal = SimpleNamespace(
            SIGTERM=signal.SIGTERM,
            Signals=signal.Signals,
            signal=MagicMock(),
            getsignal=MagicMock(return_value="previous-handler"),
        )

        with patch.object(runner, "signal", fake_signal):
            instance._install_signal_handlers()
            assert instance._previous_signal_handlers == {signal.SIGTERM: "previous-handler"}
            fake_signal.signal.assert_called_once_with(signal.SIGTERM, instance._handle_signal)
            with pytest.raises(runner._LiveValidationSignal, match="Received SIGTERM"):
                instance._handle_signal(signal.SIGTERM, None)
            assert instance._received_signal == signal.SIGTERM
            instance._restore_signal_handlers()

        fake_signal.signal.assert_called_with(signal.SIGTERM, "previous-handler")
        assert instance._previous_signal_handlers == {}


# ---------------------------------------------------------------------------
# models.py: serialization, checkpoint loading, report rendering, Job records.
# ---------------------------------------------------------------------------


class TestModelSerialization:
    def test_to_jsonable_normalizes_every_supported_value_kind(self) -> None:
        stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

        assert models.to_jsonable(stamp) == "2026-01-02T03:04:05+00:00"
        assert models.to_jsonable(Path("/tmp/report")) == "/tmp/report"
        assert models.to_jsonable({"b", "a"}) == ["a", "b"]
        assert models.to_jsonable(frozenset({2, 1})) == [1, 2]
        assert models.to_jsonable((1, "two", None, True)) == [1, "two", None, True]
        assert models.to_jsonable({1: {"nested": (Path("x"),)}}) == {"1": {"nested": ["x"]}}
        assert models.to_jsonable(object()).startswith("<object object at")
        assert (
            models.to_jsonable(
                ActionResult(
                    name="n",
                    description="d",
                    status="passed",
                    started_at="s",
                    ended_at="e",
                    duration_seconds=1.0,
                )
            )["name"]
            == "n"
        )

    @pytest.mark.parametrize(
        "instance",
        [
            RunCheckpoint(identity={}),
            ValidationReport(run_id="r", identity={}, selected_actions=[], started_at="s"),
        ],
        ids=["checkpoint", "report"],
    )
    def test_serialization_guard_fails_closed_on_non_object_output(self, instance: Any) -> None:
        with (
            patch.object(models, "to_jsonable", return_value=["not", "an", "object"]),
            pytest.raises(TypeError, match="did not serialize to an object"),
        ):
            instance.to_dict()

    def test_action_result_constructors_round_trip(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError as exc:
            failed = ActionResult.failed(
                name="topology",
                description="d",
                started_at="2026-01-01T00:00:00+00:00",
                started_monotonic=10.0,
                ended_monotonic=12.3456,
                error=exc,
            )
        passed = ActionResult.passed(
            name="preflight",
            description="d",
            started_at="2026-01-01T00:00:00+00:00",
            started_monotonic=1.0,
            ended_monotonic=1.5,
        )

        assert passed.status == "passed"
        assert passed.details == {}
        assert passed.duration_seconds == 0.5
        assert failed.status == "failed"
        assert failed.duration_seconds == 2.346
        assert failed.error == "ValueError: boom"
        assert "ValueError: boom" in (failed.traceback or "")
        restored = ActionResult.from_dict(models.to_jsonable(failed))
        assert restored == failed
        minimal = ActionResult.from_dict({"name": "x", "status": "skipped", "details": None})
        assert minimal.description == ""
        assert minimal.duration_seconds == 0.0
        assert minimal.details == {}
        assert minimal.error is None


class TestCheckpointLoading:
    def _write(self, tmp_path: Path, payload: Any) -> Path:
        path = tmp_path / "report" / "checkpoint.json"
        atomic_write_json(path, payload)
        return path

    def test_missing_checkpoint_is_unreadable(self, tmp_path: Path) -> None:
        report_dir = tmp_path / "report"
        report_dir.mkdir(mode=0o700)

        with pytest.raises(ValueError, match="Unable to read checkpoint"):
            RunCheckpoint.from_path(report_dir / "checkpoint.json")

    def test_undecodable_checkpoint_bytes_are_unreadable(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"schema_version": models.SCHEMA_VERSION})
        path.write_bytes(b"\xff\xfe not utf-8")

        with pytest.raises(ValueError, match="Unable to read checkpoint"):
            RunCheckpoint.from_path(path)

    @pytest.mark.parametrize(
        "payload",
        [["not", "an", "object"], {"schema_version": 1, "identity": {}}],
        ids=["list", "old-schema"],
    )
    def test_unsupported_schema_is_rejected(self, tmp_path: Path, payload: Any) -> None:
        path = self._write(tmp_path, payload)

        with pytest.raises(ValueError, match="does not use supported schema 2"):
            RunCheckpoint.from_path(path)

    def test_non_object_action_results_are_rejected(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            {"schema_version": models.SCHEMA_VERSION, "action_results": [{"name": "x"}]},
        )

        with pytest.raises(ValueError, match="invalid action_results"):
            RunCheckpoint.from_path(path)

    def test_loader_keeps_only_object_action_results_and_fills_defaults(
        self, tmp_path: Path
    ) -> None:
        path = self._write(
            tmp_path,
            {
                "schema_version": models.SCHEMA_VERSION,
                "identity": None,
                "created_at": "",
                "completed_actions": ["preflight", 7],
                "action_results": {
                    "preflight": {"name": "preflight", "status": "passed"},
                    "garbage": "not-an-object",
                },
                "deployment_attempted": 1,
                "baseline": ["not", "a", "dict"],
            },
        )

        loaded = RunCheckpoint.from_path(path)

        assert loaded.identity == {}
        assert loaded.created_at
        assert loaded.completed_actions == ["preflight", "7"]
        assert list(loaded.action_results) == ["preflight"]
        assert loaded.deployment_attempted is True
        assert loaded.destroyed is False
        assert loaded.baseline is None
        assert loaded.state == {}


class TestReportMarkdown:
    def test_action_rows_escape_pipes_and_list_failures_without_fatal_error(self) -> None:
        report = ValidationReport(
            run_id="run-123",
            identity={"expected_account": _ACCOUNT},
            selected_actions=["preflight", "baseline"],
            started_at="2026-07-18T00:00:00+00:00",
            status="failed",
            action_results=[
                ActionResult(
                    name="preflight",
                    description="d",
                    status="passed",
                    started_at="s",
                    ended_at="e",
                    duration_seconds=1.25,
                ),
                ActionResult(
                    name="baseline",
                    description="d",
                    status="failed",
                    started_at="s",
                    ended_at="e",
                    duration_seconds=0.5,
                    error="RuntimeError: a|b\nsecond line",
                ),
            ],
            cleanup={"needed": False},
            final_inventory={"summary": {"residual": 0}},
        )

        markdown = report.to_markdown()

        assert "| `preflight` | passed | 1.250s |  |" in markdown
        assert "| `baseline` | failed | 0.500s | RuntimeError: a\\|b second line |" in markdown
        assert "| _none_ | skipped | 0s | |" not in markdown
        assert '"residual": 0' in markdown
        assert "## Failures" in markdown
        assert "```text\nRuntimeError: a|b\nsecond line\n```" in markdown
        assert "### `baseline`" in markdown
        assert markdown.count("```text") == 1

    def test_fatal_error_and_tracebacks_are_rendered(self) -> None:
        report = ValidationReport(
            run_id="run-123",
            identity={},
            selected_actions=[],
            started_at="s",
            status="interrupted",
            fatal_error="SIGTERM: validation interrupted",
            action_results=[
                ActionResult(
                    name="deploy",
                    description="d",
                    status="failed",
                    started_at="s",
                    ended_at="e",
                    duration_seconds=0.0,
                    error="RuntimeError: cdk",
                    traceback="Traceback...\nRuntimeError: cdk\n",
                ),
                ActionResult(
                    name="mystery",
                    description="d",
                    status="failed",
                    started_at="s",
                    ended_at="e",
                    duration_seconds=0.0,
                ),
            ],
        )

        markdown = report.to_markdown()

        assert "- **Selected action scope:** _none_" in markdown
        assert "- **Account:** `unknown`" in markdown
        assert "- **Ended:** `in progress`" in markdown
        assert "```text\nSIGTERM: validation interrupted\n```" in markdown
        assert "```text\nTraceback...\nRuntimeError: cdk\n\n```" in markdown
        assert "```text\nunknown\n```" in markdown

    def test_empty_report_renders_placeholder_row_and_writes_both_files(
        self, tmp_path: Path
    ) -> None:
        report = ValidationReport(
            run_id="run-123", identity={}, selected_actions=["preflight"], started_at="s"
        )

        json_path, markdown_path = report.write(tmp_path / "report")

        assert json_path.name == "live-release-validation.json"
        assert markdown_path.name == "live-release-validation.md"
        assert _read_json(json_path)["status"] == "running"
        markdown = markdown_path.read_text(encoding="utf-8")
        assert "| _none_ | skipped | 0s | |" in markdown
        assert "## Failures" not in markdown


class TestRunContextJobRecords:
    def _ctx(self, tmp_path: Path) -> RunContext:
        return _real_context(tmp_path)

    def _register(self, ctx: RunContext, **overrides: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "name": "gco-live-api-run-123",
            "namespace": "gco-jobs",
            "region": "us-east-1",
            "path": "api",
            "run_label": "run-123",
            "transport_region": "us-east-1",
        }
        fields.update(overrides)
        return ctx.register_job(**fields)

    def test_register_job_persists_a_fresh_record(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)

        record = self._register(ctx)

        assert record == {
            "name": "gco-live-api-run-123",
            "namespace": "gco-jobs",
            "region": "us-east-1",
            "path": "api",
            "run_label": "run-123",
            "transport_region": "us-east-1",
            "uid": None,
            "deleted": False,
            "submission_state": "registered",
        }
        assert ctx.checkpoint.state["jobs"] == [record]
        ctx.persist_callback.assert_called_once_with(ctx.checkpoint)

    @pytest.mark.parametrize("jobs", [{"name": "x"}, [1]], ids=["object", "non-object-item"])
    def test_register_job_rejects_malformed_job_collections(
        self, tmp_path: Path, jobs: Any
    ) -> None:
        ctx = self._ctx(tmp_path)
        ctx.checkpoint.state["jobs"] = jobs

        with pytest.raises(RuntimeError, match="jobs must be a list of objects"):
            self._register(ctx)

    def test_register_job_rejects_duplicate_records(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        first = self._register(ctx)
        ctx.checkpoint.state["jobs"].append(dict(first))

        with pytest.raises(RuntimeError, match="duplicate Job records"):
            self._register(ctx)

    def test_register_job_reuses_a_matching_record_and_derives_state(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        first = self._register(ctx)
        del first["submission_state"]
        first["uid"] = "uid-1"

        again = self._register(ctx)

        assert again is first
        assert again["submission_state"] == "appeared"
        assert len(ctx.checkpoint.state["jobs"]) == 1

    def test_register_job_rejects_changed_identity_fields(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        self._register(ctx)

        with pytest.raises(RuntimeError, match="identity changed .*: transport_region"):
            self._register(ctx, transport_region=None)

    def test_prepare_submission_requires_an_object_envelope(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        record = self._register(ctx)

        with pytest.raises(RuntimeError, match="must be a JSON object"):
            ctx.prepare_job_submission(record, envelope=["list"], resumable=False)

    def test_prepare_submission_moves_registered_to_prepared_once(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        record = self._register(ctx)
        envelope = {"transport": "api", "path": Path("manifest.yaml")}

        ctx.prepare_job_submission(record, envelope=envelope, resumable=False)

        assert record["submission_state"] == "prepared"
        assert record["submission_envelope"] == {"transport": "api", "path": "manifest.yaml"}
        assert record["submission_resumable"] is False

        record["submission_state"] = "submitted"
        ctx.prepare_job_submission(record, envelope=envelope, resumable=False)
        assert record["submission_state"] == "submitted"

    def test_prepare_submission_rejects_drift_and_impossible_states(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        record = self._register(ctx)
        ctx.prepare_job_submission(record, envelope={"transport": "api"}, resumable=True)

        with pytest.raises(RuntimeError, match="envelope changed"):
            ctx.prepare_job_submission(record, envelope={"transport": "sqs"}, resumable=True)
        with pytest.raises(RuntimeError, match="resumability contract changed"):
            ctx.prepare_job_submission(record, envelope={"transport": "api"}, resumable=False)
        record["submission_state"] = "weird"
        with pytest.raises(RuntimeError, match="Cannot prepare Job submission from state 'weird'"):
            ctx.prepare_job_submission(record, envelope={"transport": "api"}, resumable=True)

    def test_begin_submission_state_machine(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        record = self._register(ctx)

        with pytest.raises(RuntimeError, match="Cannot begin Job submission from state"):
            ctx.begin_job_submission(record, reconciliation_timeout_seconds=30)
        record["submission_state"] = "prepared"
        with pytest.raises(RuntimeError, match="without a checkpointed envelope"):
            ctx.begin_job_submission(record, reconciliation_timeout_seconds=30)

        record["submission_envelope"] = {"transport": "api"}
        with patch.object(models.time, "time", return_value=1000.0):
            ctx.begin_job_submission(record, reconciliation_timeout_seconds=30)
        assert record["submission_state"] == "submitting"
        assert record["submission_started_at"] == 1000.0
        assert record["submission_reconcile_deadline"] == 1030.0
        assert record["submission_attempts"] == 1

        with pytest.raises(RuntimeError, match="from state 'submitting'"):
            ctx.begin_job_submission(record, reconciliation_timeout_seconds=30)
        record["submission_resumable"] = True
        ctx.begin_job_submission(record, reconciliation_timeout_seconds=30)
        assert record["submission_attempts"] == 2

    def test_finish_submission_state_machine(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        record = self._register(ctx)

        with pytest.raises(RuntimeError, match="Cannot finish Job submission from state"):
            ctx.finish_job_submission(record, {"job": 1}, appearance_timeout_seconds=20)

        record["submission_state"] = "submitting"
        with patch.object(models.time, "time", return_value=2000.0):
            ctx.finish_job_submission(record, {"job_name": "x"}, appearance_timeout_seconds=20)
        assert record["submission_state"] == "submitted"
        assert record["submission"] == {"job_name": "x"}
        assert record["appearance_deadline"] == 2020.0

        record["submission_state"] = "appeared"
        ctx.finish_job_submission(record, {"job_name": "y"}, appearance_timeout_seconds=20)
        assert record["submission_state"] == "appeared"
        assert record["submission"] == {"job_name": "y"}

    def test_block_and_not_submitted_transitions(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        record = self._register(ctx)

        ctx.mark_job_not_submitted(record)
        assert record["submission_state"] == "not_submitted"
        assert record["not_submitted_at"] > 0

        record["submission_state"] = "submitted"
        with pytest.raises(RuntimeError, match="Cannot mark Job not submitted from state"):
            ctx.mark_job_not_submitted(record)

        ctx.block_job_submission(record, "ambiguous boundary")
        assert record["submission_state"] == "blocked"
        assert record["submission_blocked_reason"] == "ambiguous boundary"
        assert record["submission_blocked_at"] > 0

    def test_central_cancellation_proof_rules(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        record = self._register(ctx, path="dynamodb", transport_region=None)

        with pytest.raises(RuntimeError, match="requires a queue Job ID"):
            ctx.mark_central_job_cancelled_before_claim(record, job_id="")
        with pytest.raises(RuntimeError, match="from state 'registered'"):
            ctx.mark_central_job_cancelled_before_claim(record, job_id="job-1")

        record["submission_state"] = "submitted"
        record["path"] = "api"
        with pytest.raises(RuntimeError, match="cannot replace immutable Kubernetes UID"):
            ctx.mark_central_job_cancelled_before_claim(record, job_id="job-1")
        record["path"] = "dynamodb"
        record["uid"] = "uid-1"
        with pytest.raises(RuntimeError, match="cannot replace immutable Kubernetes UID"):
            ctx.mark_central_job_cancelled_before_claim(record, job_id="job-1")

        record["uid"] = None
        ctx.mark_central_job_cancelled_before_claim(record, job_id="job-1")
        assert record["submission_state"] == "not_submitted"
        assert record["central_cancelled_before_claim_job_id"] == "job-1"
        persist_calls = ctx.persist_callback.call_count
        ctx.mark_central_job_cancelled_before_claim(record, job_id="job-1")
        assert ctx.persist_callback.call_count == persist_calls

    def test_central_worker_no_workload_proof_rules(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        record = self._register(ctx, path="dynamodb", transport_region=None)

        with pytest.raises(RuntimeError, match="requires a queue Job ID"):
            ctx.mark_central_job_not_created_by_worker(record, job_id="")
        with pytest.raises(RuntimeError, match="from state 'registered'"):
            ctx.mark_central_job_not_created_by_worker(record, job_id="job-1")

        record["submission_state"] = "submitting"
        record["k8s_job_name"] = "worker-name"
        with pytest.raises(RuntimeError, match="cannot replace Kubernetes identity evidence"):
            ctx.mark_central_job_not_created_by_worker(record, job_id="job-1")
        del record["k8s_job_name"]
        record["central_cancelled_before_claim_job_id"] = "job-1"
        with pytest.raises(RuntimeError, match="conflicts with cancellation proof"):
            ctx.mark_central_job_not_created_by_worker(record, job_id="job-1")

        del record["central_cancelled_before_claim_job_id"]
        ctx.mark_central_job_not_created_by_worker(record, job_id="job-1")
        assert record["submission_state"] == "not_submitted"
        assert record["central_worker_not_created_job_id"] == "job-1"
        persist_calls = ctx.persist_callback.call_count
        ctx.mark_central_job_not_created_by_worker(record, job_id="job-1")
        assert ctx.persist_callback.call_count == persist_calls

    def test_bind_central_identity_rejects_invalid_inputs(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        record = self._register(ctx)
        bind = {"job_id": "job-1", "name": "k8s", "namespace": "gco-jobs", "uid": "uid-1"}

        with pytest.raises(RuntimeError, match="requires a DynamoDB workload record"):
            ctx.bind_central_job_identity(record, appearance_timeout_seconds=30, **bind)
        record["path"] = "dynamodb"
        with pytest.raises(RuntimeError, match="must all be non-empty"):
            ctx.bind_central_job_identity(
                record, appearance_timeout_seconds=30, **{**bind, "uid": ""}
            )
        with pytest.raises(RuntimeError, match="timeout must be positive"):
            ctx.bind_central_job_identity(record, appearance_timeout_seconds=0, **bind)

        record["central_queue_job_id"] = "job-1"
        with pytest.raises(RuntimeError, match="partial central Kubernetes identity"):
            ctx.bind_central_job_identity(record, appearance_timeout_seconds=30, **bind)

    def test_bind_central_identity_is_idempotent_and_detects_drift(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        record = self._register(ctx, path="dynamodb", transport_region=None)
        record["deleted"] = True
        record["deleted_at"] = 5.0
        bind = {"job_id": "job-1", "name": "k8s", "namespace": "gco-jobs", "uid": "uid-1"}

        with patch.object(models.time, "time", return_value=3000.0):
            assert (
                ctx.bind_central_job_identity(record, appearance_timeout_seconds=30, **bind) is True
            )
        assert record["uid"] == "uid-1"
        assert record["k8s_job_uid"] == "uid-1"
        assert record["deleted"] is False
        assert "deleted_at" not in record
        assert record["requested_identity_deletion_superseded_at"] == 3000.0
        assert record["appearance_deadline"] == 3030.0
        assert record["submission_state"] == "appeared"

        assert ctx.bind_central_job_identity(record, appearance_timeout_seconds=30, **bind) is False
        with pytest.raises(RuntimeError, match="identity changed for job-1: k8s_job_name"):
            ctx.bind_central_job_identity(
                record, appearance_timeout_seconds=30, **{**bind, "name": "other"}
            )
        record["uid"] = "uid-other"
        with pytest.raises(RuntimeError, match="disagrees with checkpoint ownership authority"):
            ctx.bind_central_job_identity(record, appearance_timeout_seconds=30, **bind)

    def test_bind_central_identity_refuses_a_different_observed_uid(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        record = self._register(ctx, path="dynamodb", transport_region=None)
        record["uid"] = "uid-observed"

        with pytest.raises(RuntimeError, match="UID changed from 'uid-observed' to 'uid-1'"):
            ctx.bind_central_job_identity(
                record,
                job_id="job-1",
                name="k8s",
                namespace="gco-jobs",
                uid="uid-1",
                appearance_timeout_seconds=30,
            )

    def test_record_job_uid_rules(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        record = self._register(ctx)

        with pytest.raises(RuntimeError, match="empty Kubernetes Job UID"):
            ctx.record_job_uid(record, "")
        record["k8s_job_uid"] = "uid-central"
        with pytest.raises(RuntimeError, match="differs from persisted central worker identity"):
            ctx.record_job_uid(record, "uid-1")
        del record["k8s_job_uid"]

        ctx.record_job_uid(record, "uid-1")
        assert record["uid"] == "uid-1"
        assert record["submission_state"] == "appeared"
        persist_calls = ctx.persist_callback.call_count
        ctx.record_job_uid(record, "uid-1")
        assert ctx.persist_callback.call_count == persist_calls
        with pytest.raises(RuntimeError, match="UID changed from 'uid-1' to 'uid-2'"):
            ctx.record_job_uid(record, "uid-2")

        ctx.mark_job_deleted(record)
        assert record["deleted"] is True
        assert record["submission_state"] == "deleted"


# ---------------------------------------------------------------------------
# context.py: git identity, profile, and Region topology helpers.
# ---------------------------------------------------------------------------


class TestContextHelpers:
    def _completed(self, returncode: int, stdout: str = "", stderr: str = "") -> Any:
        return subprocess.CompletedProcess(["git"], returncode, stdout=stdout, stderr=stderr)

    def test_run_git_returns_trimmed_stdout(self, tmp_path: Path) -> None:
        with patch.object(
            context.subprocess, "run", return_value=self._completed(0, "  main\n")
        ) as run:
            assert context._run_git(tmp_path, "symbolic-ref", "--short", "HEAD") == "main"

        run.assert_called_once_with(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

    @pytest.mark.parametrize(
        ("stdout", "stderr", "message"),
        [
            ("", "fatal: not a git repository\n", "fatal: not a git repository"),
            ("stdout detail", "", "stdout detail"),
            ("", "", "unknown git error"),
        ],
    )
    def test_run_git_failures_surface_the_best_available_message(
        self, tmp_path: Path, stdout: str, stderr: str, message: str
    ) -> None:
        with (
            patch.object(
                context.subprocess, "run", return_value=self._completed(128, stdout, stderr)
            ),
            pytest.raises(RuntimeError, match=f"git rev-parse HEAD failed: {message}"),
        ):
            context._run_git(tmp_path, "rev-parse", "HEAD")

    def test_run_git_unchecked_failure_returns_output(self, tmp_path: Path) -> None:
        with patch.object(context.subprocess, "run", return_value=self._completed(1, "", "x")):
            assert context._run_git(tmp_path, "symbolic-ref", "HEAD", check=False) == ""

    def test_resolve_branch_returns_the_checked_out_branch(self, tmp_path: Path) -> None:
        with patch_live_validation_helper("_run_git", return_value="chore/test") as run_git:
            assert context._resolve_branch(tmp_path) == "chore/test"

        run_git.assert_called_once_with(tmp_path, "symbolic-ref", "--short", "HEAD", check=False)

    @pytest.mark.parametrize(
        ("profile", "regions", "match"),
        [
            ("configured", ("us-east-1",), None),
            ("single-region", ("us-east-1",), None),
            ("single-region", ("us-east-1", "eu-west-1"), "exactly one regional Region"),
            ("multi-region", ("us-east-1",), "at least two regional Regions"),
            ("multi-region", ("us-east-1", "eu-west-1"), None),
            ("ci", ("us-east-1",), "Unknown validation profile: ci"),
        ],
    )
    def test_validate_profile(
        self, profile: str, regions: tuple[str, ...], match: str | None
    ) -> None:
        ctx = SimpleNamespace(settings=SimpleNamespace(profile=profile), deployment_regions=regions)

        if match is None:
            context._validate_profile(ctx)
        else:
            with pytest.raises(RuntimeError, match=match):
                context._validate_profile(ctx)

    def test_topology_regions_dedupes_in_order(self) -> None:
        ctx = SimpleNamespace(
            cdk_context={
                "deployment_regions": {
                    "global": "us-west-2",
                    "api_gateway": "us-east-1",
                    "monitoring": "us-west-2",
                }
            },
            deployment_regions=("us-east-1", "eu-west-1"),
        )

        assert context._topology_regions(ctx) == ("us-west-2", "us-east-1", "eu-west-1")

    @pytest.mark.parametrize(
        ("partition", "configured", "expected"),
        [("aws", True, True), ("aws-cn", False, True), ("aws", False, False)],
    )
    def test_direct_regional_access_depends_on_partition_and_configuration(
        self, partition: str, configured: bool, expected: bool
    ) -> None:
        ctx = _context()
        ctx.session.get_partition_for_region.return_value = partition
        ctx.cdk_context = {"api_gateway": {"regional_api_enabled": configured}}

        assert context._direct_regional_access_enabled(ctx) is expected
        ctx.session.get_partition_for_region.assert_called_with("us-east-1")

    def test_direct_regional_access_requires_a_resolvable_partition(self) -> None:
        ctx = _context()
        ctx.session.get_partition_for_region.return_value = None
        ctx.cdk_context = {}

        with pytest.raises(RuntimeError, match="Could not resolve AWS partition for us-east-1"):
            context._direct_regional_access_enabled(ctx)

    def test_job_transport_region_prefers_direct_access_then_single_region(self) -> None:
        ctx = _context()
        ctx.session.get_partition_for_region.return_value = "aws"

        assert context._job_transport_region(ctx, "us-east-1") == "us-east-1"

        ctx.cdk_context = {}
        assert context._job_transport_region(ctx, "us-east-1") is None

        ctx.deployment_regions = ("us-east-1", "eu-west-1")
        with pytest.raises(RuntimeError, match="regional_api_enabled=true"):
            context._job_transport_region(ctx, "eu-west-1")

    @pytest.mark.parametrize(
        ("name", "expected"),
        [("gco", True), ("gco/api", True), ("gco-worker", True), ("gcoworker", False)],
    )
    def test_project_ecr_name(self, name: str, expected: bool) -> None:
        assert context._project_ecr_name(name, "gco") is expected


# ---------------------------------------------------------------------------
# artifact_io.py: owner-only directory validation and atomic writes.
# ---------------------------------------------------------------------------


def _foreign_stat(path: Path) -> os.stat_result:
    """The path's real metadata rewritten to belong to another user."""
    real = path.lstat()
    return os.stat_result(
        (
            real.st_mode,
            real.st_ino,
            real.st_dev,
            real.st_nlink,
            os.geteuid() + 1,
            real.st_gid,
            real.st_size,
            int(real.st_atime),
            int(real.st_mtime),
            int(real.st_ctime),
        )
    )


class _WindowsLikeOs:
    """``os`` as artifact_io sees it on Windows: ``name == "nt"``, everything else real.

    Only the module reference inside ``artifact_io`` is swapped, so pathlib and
    tempfile keep dispatching on the real platform.
    """

    name = "nt"

    def __getattr__(self, attribute: str) -> Any:
        return getattr(os, attribute)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission model only")
class TestArtifactIo:
    def _private_dir(self, tmp_path: Path) -> Path:
        directory = tmp_path / "report"
        directory.mkdir(mode=0o700)
        return directory

    def _private_file(self, directory: Path, name: str = "checkpoint.json") -> Path:
        path = directory / name
        path.write_text("{}", encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def test_regular_file_validation_rejects_foreign_owner_and_loose_mode(
        self, tmp_path: Path
    ) -> None:
        directory = self._private_dir(tmp_path)
        path = self._private_file(directory)

        artifact_io._validate_private_regular_file(path)
        with pytest.raises(PermissionError, match="not owned by this user"):
            artifact_io._validate_private_regular_metadata(_foreign_stat(path), path)

        # Deliberately loose: the production check must reject it.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(path, 0o644)
        with pytest.raises(PermissionError, match="must have mode 0600"):
            artifact_io._validate_private_regular_file(path)
        with pytest.raises(ValueError, match="must be a regular file"):
            artifact_io._validate_private_regular_metadata(directory.lstat(), directory)

    def test_directory_validation_rejects_files_and_foreign_owners(self, tmp_path: Path) -> None:
        directory = self._private_dir(tmp_path)
        path = self._private_file(directory)

        with pytest.raises(ValueError, match="directory must be real"):
            artifact_io._validate_private_directory_metadata(path.lstat(), path)
        with pytest.raises(PermissionError, match="directory is not owned by this user"):
            artifact_io._validate_private_directory_metadata(_foreign_stat(directory), directory)

    def test_directory_binding_assertion_fails_when_the_path_disappears(
        self, tmp_path: Path
    ) -> None:
        directory = self._private_dir(tmp_path)
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            artifact_io._assert_directory_binding(directory, descriptor)
            with pytest.raises(RuntimeError, match="rebound while open"):
                artifact_io._assert_directory_binding(tmp_path / "vanished", descriptor)
        finally:
            os.close(descriptor)

    def test_descriptor_operations_must_be_available(self, tmp_path: Path) -> None:
        directory = self._private_dir(tmp_path)

        with (
            patch.object(artifact_io.os, "O_DIRECTORY", 0),
            pytest.raises(RuntimeError, match="descriptor operations are unavailable"),
        ):
            artifact_io.atomic_write_text(directory / "checkpoint.json", "{}")

        assert list(directory.iterdir()) == []

    def test_directory_swapped_while_opening_is_rejected(self, tmp_path: Path) -> None:
        directory = self._private_dir(tmp_path)

        with (
            patch.object(artifact_io.os.path, "samestat", return_value=False),
            pytest.raises(RuntimeError, match="changed while opening"),
        ):
            artifact_io.atomic_write_text(directory / "checkpoint.json", "{}")

        assert list(directory.iterdir()) == []

    def test_read_rejects_a_loose_file_and_closes_its_descriptor(self, tmp_path: Path) -> None:
        directory = self._private_dir(tmp_path)
        path = self._private_file(directory)
        # Deliberately loose: the production check must reject it.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(path, 0o644)
        closed: list[int] = []
        real_close = os.close

        def tracked_close(descriptor: int) -> None:
            closed.append(descriptor)
            real_close(descriptor)

        with (
            patch.object(artifact_io.os, "close", side_effect=tracked_close),
            pytest.raises(PermissionError, match="must have mode 0600"),
        ):
            artifact_io.read_private_text(path)

        assert len(closed) == 2, "both the pinned directory and the file descriptor close"

    def test_read_private_text_returns_the_owner_only_content(self, tmp_path: Path) -> None:
        directory = self._private_dir(tmp_path)
        path = self._private_file(directory)
        path.write_text('{"generation": 3}', encoding="utf-8")

        assert artifact_io.read_private_text(path) == '{"generation": 3}'

    def test_failed_write_closes_and_unlinks_the_temporary_file(self, tmp_path: Path) -> None:
        directory = self._private_dir(tmp_path)
        target = directory / "checkpoint.json"

        with (
            patch.object(artifact_io.os, "fchmod", side_effect=OSError("fchmod refused")),
            pytest.raises(OSError, match="fchmod refused"),
        ):
            artifact_io.atomic_write_text(target, "{}")

        assert list(directory.iterdir()) == []

    def test_failed_replace_leaves_no_temporary_file(self, tmp_path: Path) -> None:
        directory = self._private_dir(tmp_path)
        target = directory / "checkpoint.json"

        with (
            patch.object(artifact_io.os, "replace", side_effect=OSError("replace refused")),
            pytest.raises(OSError, match="replace refused"),
        ):
            artifact_io.atomic_write_text(target, "{}")

        assert list(directory.iterdir()) == []

    def test_portable_fallbacks_without_directory_descriptors(self, tmp_path: Path) -> None:
        """The ``os.name == "nt"`` branches use plain paths instead of dir_fd operations."""
        directory = self._private_dir(tmp_path)
        target = directory / "checkpoint.json"
        windows_like = _WindowsLikeOs()

        with patch.object(artifact_io, "os", windows_like):
            artifact_io.atomic_write_text(target, '{"generation": 1}')
            assert artifact_io.read_private_text(target) == '{"generation": 1}'
            artifact_io.ensure_private_run_directory(directory, target)
            with (
                patch.object(windows_like, "fsync", side_effect=OSError("fsync refused")),
                pytest.raises(OSError, match="fsync refused"),
            ):
                artifact_io.atomic_write_text(target, '{"generation": 2}')
            (directory / "unrelated.txt").write_text("x", encoding="utf-8")
            with pytest.raises(ValueError, match="unrelated entry"):
                artifact_io.ensure_private_run_directory(directory, target)

        assert sorted(entry.name for entry in directory.iterdir()) == [
            "checkpoint.json",
            "unrelated.txt",
        ]
        assert target.read_text(encoding="utf-8") == '{"generation": 1}'

    def test_run_directory_tolerates_in_flight_temporaries_only(self, tmp_path: Path) -> None:
        directory = self._private_dir(tmp_path)
        checkpoint = directory / "checkpoint.json"
        self._private_file(directory, ".checkpoint.json.abc123.tmp")
        self._private_file(directory, "live-release-validation.json")

        artifact_io.ensure_private_run_directory(directory, checkpoint)

        self._private_file(directory, ".other.json.abc123.tmp")
        with pytest.raises(ValueError, match="unrelated entry"):
            artifact_io.ensure_private_run_directory(directory, checkpoint)


# ---------------------------------------------------------------------------
# cli_args.py and __main__.py: argument parsing, validation, and entry point.
# ---------------------------------------------------------------------------


def _fake_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    _write_cdk_json(root)
    return root


class TestCliArgs:
    def test_repository_root_accepts_an_explicit_checkout(self, tmp_path: Path) -> None:
        root = _fake_repo(tmp_path / "checkout")

        assert cli_args.repository_root(str(root)) == root.resolve()

    def test_repository_root_discovers_the_git_toplevel(self, tmp_path: Path) -> None:
        root = _fake_repo(tmp_path / "checkout")
        completed = subprocess.CompletedProcess(["git"], 0, stdout=f"{root}\n", stderr="")

        with patch.object(cli_args.subprocess, "run", return_value=completed) as run:
            assert cli_args.repository_root(None) == root.resolve()

        run.assert_called_once_with(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_repository_root_requires_a_git_checkout(self) -> None:
        completed = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="fatal")

        with (
            patch.object(cli_args.subprocess, "run", return_value=completed),
            pytest.raises(ValueError, match="Run from a Git checkout or pass --repo-root"),
        ):
            cli_args.repository_root("")

    def test_repository_root_requires_git_and_cdk_json(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()

        with pytest.raises(ValueError, match="Not a GCO repository root"):
            cli_args.repository_root(str(tmp_path))

    def test_split_csv_names_dedupes_and_requires_a_name(self) -> None:
        assert cli_args.split_csv_names(" api, sqs ,api,, ") == ("api", "sqs")

        with pytest.raises(argparse.ArgumentTypeError, match="expected at least one name"):
            cli_args.split_csv_names(" , ")

    def test_path_from_root_resolves_relative_defaults_and_absolute_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        default = Path(".live-release-validation") / "run-1"

        assert cli_args.path_from_root(tmp_path, None, default) == tmp_path / default
        assert cli_args.path_from_root(tmp_path, "reports/../out", default) == tmp_path / "out"
        assert cli_args.path_from_root(tmp_path, str(tmp_path / "abs"), default) == (
            tmp_path / "abs"
        )
        assert cli_args.path_from_root(tmp_path, "~/reports", default) == (
            tmp_path / "home" / "reports"
        )


def _cli_argv(*extra: str, actions: str = "preflight") -> list[str]:
    return [
        "--expected-account",
        _ACCOUNT,
        "--expected-sha",
        _SHA,
        "--expected-branch",
        "chore/test",
        "--actions",
        actions,
        *extra,
    ]


def _inference_argv(*extra: str) -> list[str]:
    return _cli_argv(
        "--inference-region",
        "us-east-1",
        "--inference-vllm-image",
        "registry.example/vllm@sha256:" + "b" * 64,
        "--inference-vllm-model-id",
        "publisher/vllm-model",
        "--inference-vllm-model-revision",
        "c" * 40,
        "--inference-tgi-image",
        "registry.example/tgi@sha256:" + "d" * 64,
        "--inference-tgi-model-id",
        "publisher/tgi-model",
        "--inference-tgi-model-revision",
        "e" * 40,
        *extra,
        actions="inference",
    )


class TestMainArgumentValidation:
    @pytest.mark.parametrize(
        ("argv", "match"),
        [
            (["--expected-account", "123"], "12-digit AWS account ID"),
            (["--expected-sha", "abc"], "40-character commit SHA"),
            (["--expected-branch", "   "], "--expected-branch is required"),
            (["--run-id", "bad id!"], "--run-id must be 1-80 safe filename characters"),
            (["--protected-stack", "1bad"], "Invalid --protected-stack name: '1bad'"),
            (["--max-workers", "0"], "--max-workers must be positive"),
            (["--destroy-retry-delay-seconds", "-5"], "--destroy-retry-delay-seconds must be"),
            (["--optional-schedulers", "bogus"], "--optional-schedulers accepts yunikorn, slurm"),
            (["--optional-schedulers", "all,slurm"], "'all' cannot be combined"),
            (["--inference-gpu-count", "-1"], "--inference-gpu-count must be non-negative"),
        ],
    )
    def test_invalid_arguments_are_rejected(
        self, argv: list[str], match: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parser = live_main._build_parser()
        args = parser.parse_args(_cli_argv(*argv))

        with pytest.raises(SystemExit) as excinfo:
            live_main._validate_args(parser, args)

        assert excinfo.value.code == 2
        assert match in capsys.readouterr().err

    def test_inference_requires_every_runtime_input_and_consent(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parser = live_main._build_parser()

        args = parser.parse_args(_cli_argv(actions="inference"))
        with pytest.raises(SystemExit):
            live_main._validate_args(parser, args)
        assert "--inference-region is required when inference runs" in capsys.readouterr().err

        args = parser.parse_args(_inference_argv())
        with pytest.raises(SystemExit):
            live_main._validate_args(parser, args)
        assert "--confirm-inference-deployment is required" in capsys.readouterr().err

        args = parser.parse_args(_inference_argv("--confirm-inference-deployment"))
        live_main._validate_args(parser, args)

    def test_parser_lists_actions_and_accepts_protected_stacks(self) -> None:
        parser = live_main._build_parser()

        args = parser.parse_args(
            _cli_argv("--protected-stack", "SharedNetwork", "--protected-stack", "Audit")
        )
        live_main._validate_args(parser, args)

        assert args.protected_stack == ["SharedNetwork", "Audit"]
        assert parser.epilog is not None
        assert parser.epilog.startswith("Actions: preflight, baseline, deploy")
        assert args.inference_vllm_port == 8000
        assert args.inference_tgi_port == 8080


class TestMainSettings:
    def _settings(self, tmp_path: Path, argv: list[str]) -> RunSettings:
        parser = live_main._build_parser()
        return live_main._settings_from_args(parser, parser.parse_args(argv))

    def test_non_inference_run_uses_defaults_and_derives_run_id(self, tmp_path: Path) -> None:
        root = _fake_repo(tmp_path)

        settings = self._settings(
            tmp_path,
            _cli_argv(
                "--repo-root",
                str(root),
                "--protected-stack",
                "CDKToolkit",
                "--protected-stack",
                "Audit",
                "--optional-schedulers",
                "slurm,yunikorn",
                "--expected-sha",
                _SHA.upper(),
                actions="preflight,baseline",
            ),
        )

        assert settings.repo_root == root.resolve()
        assert settings.expected_sha == _SHA
        assert settings.requested_actions == ("preflight", "baseline")
        assert settings.inference_enabled is False
        assert settings.inference_runtimes == ()
        assert settings.proxy_tls_cpu_request == "100m"
        assert settings.proxy_tls_cpu_target == 70
        assert settings.protected_stack_names == ("CDKToolkit", "GCOGitHubOIDCStack", "Audit")
        assert settings.optional_schedulers == ("slurm", "yunikorn")
        assert settings.report_dir == root.resolve() / ".live-release-validation" / settings.run_id
        assert settings.checkpoint_path == settings.report_dir / "checkpoint.json"
        assert settings.run_id.endswith("-" + _SHA[:12])
        assert settings.resume is False

    def test_explicit_paths_resume_and_all_schedulers(self, tmp_path: Path) -> None:
        root = _fake_repo(tmp_path)

        settings = self._settings(
            tmp_path,
            _cli_argv(
                "--repo-root",
                str(root),
                "--run-id",
                "run-123",
                "--report-dir",
                "out/run-123",
                "--checkpoint",
                str(root / "out/run-123/state.json"),
                "--resume",
                "--confirm-kms-key-deletion",
                "--optional-schedulers",
                "all",
                "--profile",
                "single-region",
            ),
        )

        assert settings.report_dir == root.resolve() / "out" / "run-123"
        assert settings.checkpoint_path == root.resolve() / "out" / "run-123" / "state.json"
        assert settings.resume is True
        assert settings.confirm_kms_key_deletion is True
        assert settings.optional_schedulers == ("yunikorn", "slurm")
        assert settings.profile == "single-region"

    @pytest.mark.parametrize(
        ("cdk_payload", "match"),
        [
            (
                {"context": {"inference_proxy": ["not", "an", "object"]}},
                "must be an object or null",
            ),
            ("{not json", "could not read inference_proxy settings"),
            (
                {"context": {"inference_proxy": {"tls_proxy_cpu_request_millicores": "125"}}},
                "TLS CPU settings must be integers",
            ),
            (
                {
                    "context": {
                        "inference_proxy": {
                            "tls_proxy_cpu_target_utilization_percentage": True,
                        }
                    }
                },
                "TLS CPU settings must be integers",
            ),
        ],
    )
    def test_inference_proxy_settings_are_validated(
        self,
        tmp_path: Path,
        cdk_payload: Any,
        match: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / ".git").mkdir()
        _write_cdk_json(tmp_path, cdk_payload)

        with pytest.raises(SystemExit):
            self._settings(
                tmp_path,
                _inference_argv("--confirm-inference-deployment", "--repo-root", str(tmp_path)),
            )

        assert match in capsys.readouterr().err


class TestMainEntryPoint:
    @pytest.fixture(autouse=True)
    def _local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    def _argv(self, root: Path, *extra: str) -> list[str]:
        return [
            "live_release_validation",
            "--repo-root",
            str(root),
            "--run-id",
            "run-123",
            "--report-dir",
            str(root / "report"),
            *_cli_argv(*extra),
        ]

    def test_list_actions_prints_the_registry_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["live_release_validation", "--list-actions"])

        with patch.object(live_main, "LiveValidationRunner") as runner_type:
            assert live_main.main() == 0

        runner_type.assert_not_called()
        output = capsys.readouterr().out
        assert "preflight" in output
        assert "[depends: none]" in output
        assert "final-inventory" in output
        assert "[depends: destroy]" in output

    def test_settings_failure_before_any_report_directory_returns_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", self._argv(tmp_path))

        with patch.object(live_main, "LiveValidationRunner") as runner_type:
            assert live_main.main() == 1

        runner_type.assert_not_called()
        err = capsys.readouterr().err
        assert "Live validation could not start: ValueError: Not a GCO repository root" in err
        assert "JSON report" not in err
        assert not (tmp_path / "report").exists()

    def test_keyboard_interrupt_before_the_runner_returns_130(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", self._argv(_fake_repo(tmp_path)))

        with patch.object(live_main, "LiveValidationRunner", side_effect=KeyboardInterrupt):
            assert live_main.main() == 130

        assert "interrupted before the runner initialized" in capsys.readouterr().err

    def test_runner_result_is_returned_as_the_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "argv", self._argv(_fake_repo(tmp_path)))
        instance = MagicMock()
        instance.run.return_value = 7

        with patch.object(live_main, "LiveValidationRunner", return_value=instance) as runner_type:
            assert live_main.main() == 7

        settings = runner_type.call_args.args[0]
        assert isinstance(settings, RunSettings)
        assert settings.run_id == "run-123"
        instance.run.assert_called_once_with()

    def test_constructor_failure_without_checkpoint_writes_a_plain_failure_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _fake_repo(tmp_path)
        monkeypatch.setattr(sys, "argv", self._argv(root))

        with patch.object(
            live_main, "LiveValidationRunner", side_effect=RuntimeError("session bootstrap")
        ):
            assert live_main.main() == 1

        report = _read_json(root / "report" / "live-release-validation.json")
        assert report["status"] == "failed"
        assert report["cleanup"] == {}
        assert "RuntimeError: session bootstrap" in report["fatal_error"]
        assert report["selected_actions"] == ["preflight"]
        err = capsys.readouterr().err
        assert "Live validation could not start: RuntimeError: session bootstrap" in err
        assert "JSON report:" in err
        assert "Markdown report:" in err

    @pytest.mark.parametrize(
        "checkpoint_payload",
        [
            {"schema_version": 1},
            {"schema_version": models.SCHEMA_VERSION, "deployment_attempted": False},
        ],
        ids=["unreadable", "not-deployed"],
    )
    def test_constructor_failure_without_deployed_checkpoint_claims_no_cleanup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        checkpoint_payload: dict[str, Any],
    ) -> None:
        root = _fake_repo(tmp_path)
        report_dir = root / "report"
        report_dir.mkdir(mode=0o700)
        atomic_write_json(report_dir / "checkpoint.json", checkpoint_payload)
        monkeypatch.setattr(sys, "argv", self._argv(root))

        with patch.object(live_main, "LiveValidationRunner", side_effect=ValueError("mismatch")):
            assert live_main.main() == 1

        report = _read_json(report_dir / "live-release-validation.json")
        assert report["status"] == "failed"
        assert report["cleanup"] == {}

    def test_recovery_command_appends_resume_when_it_was_not_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _fake_repo(tmp_path)
        argv = self._argv(root)
        parser = live_main._build_parser()
        settings = live_main._settings_from_args(parser, parser.parse_args(argv[1:]))
        report_dir = root / "report"
        report_dir.mkdir(mode=0o700)
        atomic_write_json(
            settings.checkpoint_path,
            RunCheckpoint(identity=settings.identity(), deployment_attempted=True).to_dict(),
        )
        monkeypatch.setattr(sys, "argv", argv)

        with patch.object(
            live_main, "LiveValidationRunner", side_effect=RuntimeError("client bootstrap")
        ):
            assert live_main.main() == 1

        report = _read_json(report_dir / "live-release-validation.json")
        cleanup = report["cleanup"]
        assert cleanup["needed"] is True
        assert cleanup["completed"] is False
        assert "construction failed" in cleanup["blocked"]
        recovery = cleanup["recovery_command"]
        assert recovery.startswith(f"{sys.executable} -m scripts.live_release_validation ")
        assert recovery.endswith(" --resume")
        assert "--run-id run-123" in recovery


# ---------------------------------------------------------------------------
# inference_contract.py: strict runtime matrix validation.
# ---------------------------------------------------------------------------


class TestInferenceContractValidation:
    def test_validate_runtime_rejects_unknown_frameworks(self) -> None:
        runtime = dataclasses.replace(_runtime("vllm"), framework="triton")

        with pytest.raises(ValueError, match="must be 'vllm' or 'tgi'"):
            inference_contract._validate_runtime(runtime)

    @pytest.mark.parametrize(
        ("changes", "match"),
        [
            ({"selected_region": "US-EAST-1"}, "lowercase AWS Region name"),
            ({"selected_region": "useast1"}, "lowercase AWS Region name"),
            ({"inference_runtimes": [_runtime("vllm"), _runtime("tgi")]}, "vLLM then TGI"),
            ({"inference_runtimes": (_runtime("tgi"), _runtime("vllm"))}, "vLLM then TGI"),
            (
                {"inference_runtimes": (_runtime("vllm", model_id=" padded"), _runtime("tgi"))},
                "vllm model_id must be a non-empty trimmed value",
            ),
            (
                {
                    "inference_runtimes": (
                        _runtime("vllm"),
                        dataclasses.replace(_runtime("tgi"), model_id=""),
                    )
                },
                "tgi model_id must be a non-empty trimmed value",
            ),
            (
                {
                    "inference_runtimes": (
                        _runtime("vllm"),
                        dataclasses.replace(_runtime("tgi"), port=8000),
                    )
                },
                "tgi live validation port must be 8080",
            ),
            ({"request_prompt": "   "}, "request_prompt must be non-empty"),
            ({"namespace": "Bad_Namespace"}, "DNS-safe Kubernetes name"),
            ({"gpu_count": True}, "gpu_count must be a non-negative integer"),
            ({"gpu_count": -1}, "gpu_count must be a non-negative integer"),
            ({"request_max_tokens": 0}, "request_max_tokens must be a positive integer"),
            ({"job_timeout_seconds": True}, "job_timeout_seconds must be a positive integer"),
            ({"baseline_replicas": 2}, "start with one replica"),
            ({"autoscale_initial_replicas": 2}, "start with one replica"),
            ({"hpa_cpu_target": True}, "hpa_cpu_target must be an integer"),
            ({"hpa_cpu_target": 0}, "from 1 through 100"),
            ({"hpa_cpu_target": 101}, "from 1 through 100"),
            ({"hpa_stability_intervals": 1}, "at least two"),
            ({"health_path": "/healthz"}, "official /health path"),
            ({"proxy_tls_cpu_request": "100"}, "positive millicore quantity"),
            ({"proxy_tls_cpu_request": "0m"}, "positive millicore quantity"),
            ({"proxy_tls_cpu_target": 0}, "proxy_tls_cpu_target must be an integer from 1"),
            ({"proxy_tls_cpu_target": True}, "proxy_tls_cpu_target must be an integer from 1"),
            ({"proxy_tls_cpu_target": "70"}, "proxy_tls_cpu_target must be an integer from 1"),
        ],
    )
    def test_invalid_inference_settings_are_rejected(
        self, tmp_path: Path, changes: dict[str, Any], match: str
    ) -> None:
        with pytest.raises(ValueError, match=match):
            _inference_settings(tmp_path, **changes)

    def test_framework_env_and_extra_args_are_official_launcher_inputs(
        self, tmp_path: Path
    ) -> None:
        settings = _inference_settings(tmp_path)
        vllm, tgi = settings.inference_runtimes

        assert settings.framework_env(vllm) == {"MODEL": "test/vllm-model"}
        assert settings.framework_env(tgi) == {
            "MODEL_ID": "test/tgi-model",
            "PORT": "8080",
            "REVISION": "d" * 40,
        }
        assert settings.deploy_extra_args(vllm) == (
            "--model",
            "test/vllm-model",
            "--revision",
            "c" * 40,
        )
        assert settings.deploy_extra_args(tgi) == ()
        assert vllm.model_info_path == "/v1/models"
        assert tgi.model_info_path == "/info"
        assert settings.kubeconfig_path == settings.report_dir / "kubeconfig"


# ---------------------------------------------------------------------------
# actions/: one class per action module.
# ---------------------------------------------------------------------------


class TestActionBaseline:
    def test_reused_baseline_requires_list_shaped_efs_acceptance(self) -> None:
        ctx = _context(state={"baseline_accepted_efs_automatic_backup_recovery_points": "x"})

        with pytest.raises(RuntimeError, match="EFS acceptance evidence must be a list"):
            actions_baseline.action_baseline(ctx)

    def test_fresh_baseline_requires_preflight_regions(self) -> None:
        ctx = _context(state={})
        ctx.checkpoint.baseline = None

        with (
            patch_live_validation_helper("capture_baseline") as capture,
            pytest.raises(RuntimeError, match="Preflight did not record enabled AWS Regions"),
        ):
            actions_baseline.action_baseline(ctx)

        capture.assert_not_called()


class TestActionConvergence:
    def _ctx(self, *, baseline_dlq: int = 0) -> SimpleNamespace:
        ctx = _context(state={"queue_baseline": {"us-east-1": {"dlq_messages": baseline_dlq}}})
        ctx.settings.queue_timeout_seconds = 100
        ctx.settings.poll_interval_seconds = 5
        return ctx

    @contextlib.contextmanager
    def _clock(self) -> Iterator[list[int]]:
        ticks = iter(range(0, 10_000))
        sleeps: list[int] = []
        with (
            patch.object(actions_convergence.time, "monotonic", side_effect=lambda: next(ticks)),
            patch.object(actions_convergence.time, "sleep", side_effect=sleeps.append),
        ):
            yield sleeps

    def test_missing_queue_baseline_fails_closed(self) -> None:
        ctx = _context(state={})

        with pytest.raises(RuntimeError, match="did not record queue baselines"):
            actions_convergence.action_convergence(ctx)

        ctx.job_manager.get_queue_status.assert_not_called()

    def test_three_stable_observations_and_terminal_records_pass(self) -> None:
        ctx = self._ctx(baseline_dlq=2)
        ctx.checkpoint.state["central_jobs"] = [{"job_id": "central-1"}]
        statuses = iter(
            [
                {"messages_available": 1, "dlq_messages": 2},
                {"messages_available": 0, "dlq_messages": 2},
                {"messages_available": 0, "dlq_messages": 2},
                {"messages_available": 0, "dlq_messages": 2},
            ]
        )
        ctx.job_manager.get_queue_status.side_effect = lambda region: next(statuses)
        item = {"job_id": "central-1", "status": "succeeded"}

        with (
            self._clock() as sleeps,
            patch_live_validation_helper("_read_central_job_item", return_value=item) as read_item,
        ):
            result = actions_convergence.action_convergence(ctx)

        assert result["stable_observations"] == 3
        assert [
            sample["counts"]["us-east-1"]["available"] for sample in result["queue_samples"]
        ] == [
            1,
            0,
            0,
            0,
        ]
        assert result["dynamodb_records"] == {"central-1": item}
        assert sleeps == [5, 5, 5]
        read_item.assert_called_once_with(ctx, "central-1")

    def test_unstable_counters_time_out_with_recent_samples(self) -> None:
        ctx = self._ctx()
        ctx.settings.queue_timeout_seconds = 12
        ctx.job_manager.get_queue_status.return_value = {"messages_in_flight": 1}

        with self._clock(), pytest.raises(TimeoutError, match="did not converge") as excinfo:
            actions_convergence.action_convergence(ctx)

        assert '"in_flight": 1' in str(excinfo.value)
        assert str(excinfo.value).count('"at"') == 5

    def test_regressed_dynamodb_record_fails(self) -> None:
        ctx = self._ctx()
        ctx.checkpoint.state["central_jobs"] = [{"job_id": "central-1"}]
        ctx.job_manager.get_queue_status.return_value = {}

        with (
            self._clock(),
            patch_live_validation_helper(
                "_read_central_job_item", return_value={"status": "failed"}
            ),
            pytest.raises(RuntimeError, match="DynamoDB record central-1 regressed to failed"),
        ):
            actions_convergence.action_convergence(ctx)


class TestActionDeploy:
    _STACK_ID = "arn:aws:cloudformation:us-east-1:123456789012:stack/gco-live-global/stack-uuid"
    _CHANGE_SET_ID = (
        "arn:aws:cloudformation:us-east-1:123456789012:changeSet/run-123/change-set-uuid"
    )

    def _ctx(self) -> SimpleNamespace:
        return _context(
            state={
                "target_stack_regions": {
                    "gco-live-global": "us-east-1",
                    "gco-live-us-east-1": "us-east-1",
                },
                "bootstrap_stacks": {"us-east-1": {"stack_id": "bootstrap-id"}},
                "owned_stacks": {
                    "us-east-1": {
                        "gco-live-global": {
                            "name": "gco-live-global",
                            "region": "us-east-1",
                            "stack_id": self._STACK_ID,
                            "run_tag": "run-123",
                            "authority": "prepared-change-set",
                            "change_set_id": self._CHANGE_SET_ID,
                            "change_set_type": "CREATE",
                        }
                    }
                },
            }
        )

    @contextlib.contextmanager
    def _ownership(self) -> Iterator[dict[str, MagicMock]]:
        with (
            patch_live_validation_helper("_reconcile_stack_ownership") as reconcile,
            patch_live_validation_helper("_checkpoint_new_ecr_repositories") as repositories,
            patch_live_validation_helper("_checkpoint_new_ecr_images") as images,
            patch_live_validation_helper("_checkpoint_retained_kms_keys") as kms,
            patch_live_validation_helper("_record_stack_identity") as record_identity,
            patch_live_validation_helper("_record_prepared_stack_identity") as record_prepared,
            patch_live_validation_helper("_record_ecr_repository_creation") as record_repository,
            patch_live_validation_helper("_authorize_owned_stack") as authorize,
        ):
            yield {
                "reconcile": reconcile,
                "repositories": repositories,
                "images": images,
                "kms": kms,
                "record_identity": record_identity,
                "record_prepared": record_prepared,
                "record_repository": record_repository,
                "authorize": authorize,
            }

    def test_deploy_requires_a_baseline(self) -> None:
        ctx = self._ctx()
        ctx.checkpoint.baseline = None

        with pytest.raises(RuntimeError, match="baseline is required before deployment"):
            actions_deploy.action_deploy(ctx)

        assert ctx.checkpoint.deployment_attempted is True
        ctx.stack_manager.deploy_orchestrated.assert_not_called()

    def test_callbacks_checkpoint_every_observed_identity(self) -> None:
        ctx = self._ctx()
        ctx.checkpoint.deployment_attempted = False
        ctx.checkpoint.destroyed = True
        described = {
            "name": "gco-live-global",
            "stack_id": self._STACK_ID,
            "status": "CREATE_COMPLETE",
        }
        new_stack_id = self._STACK_ID.replace("stack-uuid", "regional-uuid")
        new_change_set = self._CHANGE_SET_ID.replace("change-set-uuid", "regional-cs")
        repository = {"repositoryName": "gco-live/worker"}

        def deploy_orchestrated(**kwargs: Any) -> tuple[bool, list[str], list[str]]:
            assert kwargs["expected_stack_ids"] == {
                "gco-live-global": self._STACK_ID,
                "gco-live-us-east-1": None,
            }
            assert kwargs["prepared_change_sets"] == {
                "gco-live-global": {
                    self._CHANGE_SET_ID: {
                        "change_set_id": self._CHANGE_SET_ID,
                        "stack_id": self._STACK_ID,
                        "change_set_type": "CREATE",
                    }
                },
                "gco-live-us-east-1": {},
            }
            kwargs["on_stack_start"]("gco-live-us-east-1")
            kwargs["on_change_set_prepared"](
                "gco-live-us-east-1", "us-east-1", new_stack_id, new_change_set, "CREATE"
            )
            kwargs["on_ecr_repository_created"]("us-east-1", repository)
            kwargs["authorize_stack"]("gco-live-us-east-1", "us-east-1", new_stack_id)
            kwargs["on_stack_complete"]("gco-live-us-east-1", True)
            kwargs["on_stack_complete"]("gco-live-global", False)
            assert kwargs["expected_stack_ids"]["gco-live-us-east-1"] == new_stack_id
            assert new_change_set in kwargs["prepared_change_sets"]["gco-live-us-east-1"]
            return True, ["gco-live-us-east-1"], []

        ctx.stack_manager.deploy_orchestrated.side_effect = deploy_orchestrated
        describe = MagicMock(
            side_effect=lambda session, region, name: (
                described if name == "gco-live-us-east-1" else None
            )
        )

        with (
            self._ownership() as ownership,
            patch_live_validation_helper("describe_stack", describe),
        ):
            result = actions_deploy.action_deploy(ctx)

        assert ctx.checkpoint.deployment_attempted is True
        assert ctx.checkpoint.destroyed is False
        assert result["overall_success"] is True
        assert result["successful_stacks"] == ["gco-live-us-east-1"]
        assert [event["event"] for event in result["events"]] == [
            "started",
            "completed",
            "completed",
        ]
        assert result["events"][1]["success"] is True
        assert result["events"][2]["success"] is False
        assert ctx.checkpoint.state["deploy_events"] == result["events"]
        assert ctx.checkpoint.state["deploy_result"] == result
        kwargs = ctx.stack_manager.deploy_orchestrated.call_args.kwargs
        assert kwargs["tags"] == {"GcoLiveValidationRun": "run-123"}
        assert kwargs["strict_deployment_token"] == "run-123"
        assert kwargs["allow_bootstrap"] is False
        assert kwargs["parallel"] is False
        ownership["record_identity"].assert_called_once_with(
            ctx, "gco-live-us-east-1", "us-east-1", described
        )
        ownership["record_prepared"].assert_called_once_with(
            ctx, "gco-live-us-east-1", "us-east-1", new_stack_id, new_change_set, "CREATE"
        )
        ownership["record_repository"].assert_called_once_with(ctx, "us-east-1", repository)
        ownership["authorize"].assert_called_once_with(
            ctx, "gco-live-us-east-1", "us-east-1", new_stack_id
        )
        for name in ("reconcile", "repositories", "images", "kms"):
            ownership[name].assert_called_once_with(ctx)

    def test_success_report_for_an_absent_stack_fails_the_completion_callback(self) -> None:
        ctx = self._ctx()

        def deploy_orchestrated(**kwargs: Any) -> tuple[bool, list[str], list[str]]:
            kwargs["on_stack_complete"]("gco-live-global", True)
            raise AssertionError("unreachable")

        ctx.stack_manager.deploy_orchestrated.side_effect = deploy_orchestrated

        with (
            self._ownership() as ownership,
            patch_live_validation_helper("describe_stack", return_value=None),
            pytest.raises(RuntimeError, match="CDK reported success but us-east-1:gco-live-global"),
        ):
            actions_deploy.action_deploy(ctx)

        ownership["record_identity"].assert_not_called()
        ownership["reconcile"].assert_called_once_with(ctx)

    @pytest.mark.parametrize(
        ("failed", "match"),
        [(["gco-live-global"], "failed for: gco-live-global"), ([], "failed for: unknown")],
    )
    def test_failed_orchestration_raises_after_checkpointing_the_result(
        self, failed: list[str], match: str
    ) -> None:
        ctx = self._ctx()
        ctx.stack_manager.deploy_orchestrated.return_value = (False, [], failed)

        with self._ownership(), pytest.raises(RuntimeError, match=match):
            actions_deploy.action_deploy(ctx)

        assert ctx.checkpoint.state["deploy_result"]["overall_success"] is False
        assert ctx.checkpoint.state["deploy_result"]["failed_stacks"] == failed


class TestActionDestroy:
    _STACK_ID = "arn:aws:cloudformation:us-east-1:123456789012:stack/gco-live-global/stack-uuid"
    _CHANGE_SET_ID = (
        "arn:aws:cloudformation:us-east-1:123456789012:changeSet/run-123/change-set-uuid"
    )
    _RESIDUAL = {"all_absent": False, "absent": [], "residual": [{"name": "gco-live-global"}]}
    _ABSENT = {"all_absent": True, "absent": [{"name": "gco-live-global"}], "residual": []}
    _COMPLETE_CLEANUP = {"complete": True, "errors": [], "unresolved": [], "ended_at": "done"}

    def _ctx(self, *, destroyed: bool = False, completed: list[str] | None = None) -> Any:
        ctx = _context(
            state={
                "target_stack_regions": {"gco-live-global": "us-east-1"},
                "owned_stacks": {
                    "us-east-1": {
                        "gco-live-global": {
                            "name": "gco-live-global",
                            "region": "us-east-1",
                            "stack_id": self._STACK_ID,
                            "run_tag": "run-123",
                            "authority": "prepared-change-set",
                            "change_set_id": self._CHANGE_SET_ID,
                            "change_set_type": "CREATE",
                        }
                    }
                },
                "bootstrap_stacks": {"us-east-1": {"stack_id": "toolkit", "status": "x"}},
            }
        )
        ctx.checkpoint.destroyed = destroyed
        ctx.checkpoint.completed_actions = list(completed or [])
        return ctx

    @contextlib.contextmanager
    def _teardown_helpers(self, *, absence: list[dict[str, Any]]) -> Iterator[dict[str, MagicMock]]:
        with (
            patch_live_validation_helper("_verify_target_stack_absence", side_effect=absence),
            patch_live_validation_helper(
                "cleanup_workloads", return_value=dict(self._COMPLETE_CLEANUP)
            ) as workloads,
            patch_live_validation_helper("_reconcile_stack_ownership"),
            patch_live_validation_helper("_checkpoint_new_ecr_repositories"),
            patch_live_validation_helper("_checkpoint_new_ecr_images"),
            patch_live_validation_helper("_checkpoint_retained_kms_keys") as kms,
            patch_live_validation_helper(
                "_retained_resource_cleanup", return_value={"errors": []}
            ) as retained,
            patch_live_validation_helper(
                "_ensure_log_cleanup_helper", return_value={"role": "helper"}
            ) as ensure_helper,
            patch_live_validation_helper(
                "_delete_log_cleanup_helper", return_value={"deleted": True}
            ) as delete_helper,
            patch_live_validation_helper("_record_prepared_stack_identity") as record_prepared,
            patch_live_validation_helper("_authorize_owned_stack") as authorize,
            patch_live_validation_helper("_record_ecr_repository_creation") as record_repository,
            patch.object(actions_destroy.time, "sleep") as sleep,
        ):
            yield {
                "workloads": workloads,
                "kms": kms,
                "retained": retained,
                "ensure_helper": ensure_helper,
                "delete_helper": delete_helper,
                "record_prepared": record_prepared,
                "authorize": authorize,
                "record_repository": record_repository,
                "sleep": sleep,
            }

    def test_barrier_helpers_fail_closed(self) -> None:
        ctx = self._ctx()

        with pytest.raises(RuntimeError, match="incomplete workload cleanup barrier"):
            actions_destroy._record_workload_cleanup_barrier(
                ctx, {"complete": True, "errors": [{"resource": "job"}]}
            )
        with pytest.raises(RuntimeError, match="lacks a complete workload cleanup barrier"):
            actions_destroy._validated_workload_cleanup_barrier(ctx)

        actions_destroy._record_workload_cleanup_barrier(ctx, dict(self._COMPLETE_CLEANUP))
        ctx.checkpoint.state["jobs"] = [{"name": "appeared-later"}]
        with pytest.raises(RuntimeError, match="workload identity changed after cleanup"):
            actions_destroy._validated_workload_cleanup_barrier(ctx)

        ctx.checkpoint.state["jobs"] = "not-a-list"
        with pytest.raises(RuntimeError, match="workload collections must be lists"):
            actions_destroy._resume_workload_cleanup_after_stack_absence(ctx)

        with pytest.raises(RuntimeError, match="while a stack remains"):
            actions_destroy._record_target_stack_absence(ctx, dict(self._RESIDUAL), source="test")

    def test_nothing_deployed_needs_no_teardown(self) -> None:
        ctx = self._ctx()
        ctx.checkpoint.deployment_attempted = False

        with patch_live_validation_helper("_verify_target_stack_absence") as verify:
            assert actions_destroy.action_destroy(ctx) == {"needed": False, "attempts": []}

        verify.assert_not_called()

    def test_already_destroyed_reappearance_during_retained_cleanup_fails(self) -> None:
        ctx = self._ctx(destroyed=True, completed=["destroy", "final-inventory"])

        with (
            self._teardown_helpers(absence=[dict(self._ABSENT), dict(self._RESIDUAL)]),
            pytest.raises(RuntimeError, match="reappeared during repeated retained cleanup"),
        ):
            actions_destroy.destroy_deployment(ctx)

        assert ctx.checkpoint.state["target_stacks_absent"]["source"] == (
            "destroy-already-destroyed-initial-absence"
        )

    def test_resumed_absence_reappearance_during_retained_cleanup_fails(self) -> None:
        ctx = self._ctx()

        with (
            self._teardown_helpers(absence=[dict(self._ABSENT), dict(self._RESIDUAL)]),
            pytest.raises(RuntimeError, match="reappeared during resumed retained cleanup"),
        ):
            actions_destroy.destroy_deployment(ctx)

        assert ctx.checkpoint.destroyed is False
        assert ctx.checkpoint.state["target_stacks_absent"]["source"] == (
            "destroy-resume-initial-absence"
        )

    def test_stale_destroyed_flag_is_reopened_even_when_partially_recorded(self) -> None:
        ctx = self._ctx(destroyed=True, completed=["destroy"])
        ctx.stack_manager.destroy_orchestrated.return_value = (True, ["gco-live-global"], [])

        with self._teardown_helpers(
            absence=[dict(self._RESIDUAL), dict(self._ABSENT), dict(self._ABSENT)]
        ):
            result = actions_destroy.destroy_deployment(ctx)

        assert ctx.checkpoint.destroyed is True
        assert ctx.checkpoint.completed_actions == []
        reopen = ctx.checkpoint.state["stale_destroyed_reconciliations"][0]
        assert reopen["stack_absence"] == self._RESIDUAL
        assert result["attempts"][0]["overall_success"] is True

    def test_teardown_callbacks_record_helper_outcomes_and_prepared_identities(self) -> None:
        ctx = self._ctx()
        new_change_set = self._CHANGE_SET_ID.replace("change-set-uuid", "teardown-cs")
        repository = {"repositoryName": "gco-live/worker"}

        def destroy_orchestrated(**kwargs: Any) -> tuple[bool, list[str], list[str]]:
            assert kwargs["expected_stack_ids"] == {"gco-live-global": self._STACK_ID}
            kwargs["on_cleanup_complete"]("log-groups", {"deleted": ["/aws/eks/x"]})
            kwargs["on_change_set_prepared"](
                "gco-live-global", "us-east-1", self._STACK_ID, new_change_set, "DELETE"
            )
            kwargs["on_ecr_repository_created"]("us-east-1", repository)
            kwargs["authorize_stack"]("gco-live-global", "us-east-1", self._STACK_ID)
            assert new_change_set in kwargs["prepared_change_sets"]["gco-live-global"]
            return True, ["gco-live-global"], []

        ctx.stack_manager.destroy_orchestrated.side_effect = destroy_orchestrated

        with self._teardown_helpers(
            absence=[dict(self._RESIDUAL), dict(self._ABSENT), dict(self._ABSENT)]
        ) as helpers:
            result = actions_destroy.destroy_deployment(ctx)

        attempt = result["attempts"][0]
        assert attempt["sequence"] == 1
        assert attempt["invocation_attempt"] == 1
        assert attempt["log_cleanup_helper"] == {"role": "helper"}
        assert attempt["helper_outcomes"][0]["name"] == "log-groups"
        assert attempt["helper_outcomes"][0]["details"] == {"deleted": ["/aws/eks/x"]}
        assert ctx.checkpoint.state["destroy_helper_outcomes"] == attempt["helper_outcomes"]
        assert attempt["target_stack_absence_proof"]["source"] == "destroy-before-retained-cleanup"
        assert attempt["target_stack_absence_completion_proof"]["source"] == "destroy-completion"
        assert result["stack_absence"] == self._ABSENT
        assert result["workload_cleanup_barrier"]["complete"] is True
        assert ctx.checkpoint.destroyed is True
        helpers["record_prepared"].assert_called_once_with(
            ctx, "gco-live-global", "us-east-1", self._STACK_ID, new_change_set, "DELETE"
        )
        helpers["record_repository"].assert_called_once_with(ctx, "us-east-1", repository)
        helpers["authorize"].assert_called_once_with(
            ctx, "gco-live-global", "us-east-1", self._STACK_ID
        )
        helpers["delete_helper"].assert_not_called()
        helpers["sleep"].assert_not_called()

    def test_failed_attempts_retry_with_delay_then_report_the_last_failure(self) -> None:
        ctx = self._ctx()
        ctx.settings.destroy_attempts = 2
        ctx.settings.destroy_retry_delay_seconds = 7
        ctx.stack_manager.destroy_orchestrated.return_value = (False, [], ["gco-live-global"])

        with (
            self._teardown_helpers(absence=[dict(self._RESIDUAL)]) as helpers,
            pytest.raises(
                RuntimeError,
                match="did not succeed after 2 invocation attempts; last failure: gco-live-global",
            ),
        ):
            actions_destroy.destroy_deployment(ctx)

        attempts = ctx.checkpoint.state["destroy_attempts"]
        assert [attempt["sequence"] for attempt in attempts] == [1, 2]
        assert all(attempt["overall_success"] is False for attempt in attempts)
        assert all(
            attempt["log_cleanup_helper_cleanup"] == {"deleted": True} for attempt in attempts
        )
        assert "error" not in attempts[0]
        helpers["sleep"].assert_called_once_with(7)
        assert helpers["delete_helper"].call_count == 2
        assert ctx.checkpoint.destroyed is False

    def test_orchestration_exception_and_helper_cleanup_failure_are_both_preserved(self) -> None:
        ctx = self._ctx()
        ctx.stack_manager.destroy_orchestrated.side_effect = RuntimeError("cdk exploded")

        with (
            self._teardown_helpers(absence=[dict(self._RESIDUAL)]) as helpers,
            pytest.raises(RuntimeError, match="last failure: RuntimeError: cdk exploded; cleanup"),
        ):
            helpers["delete_helper"].side_effect = ValueError("helper gone")
            actions_destroy.destroy_deployment(ctx)

        attempt = ctx.checkpoint.state["destroy_attempts"][0]
        assert attempt["overall_success"] is False
        assert attempt["successful_stacks"] == []
        assert attempt["log_cleanup_helper_cleanup_error"] == "ValueError: helper gone"
        assert (
            attempt["error"]
            == "RuntimeError: cdk exploded; cleanup helper: ValueError: helper gone"
        )
        assert "ended_at" in attempt

    @pytest.mark.parametrize(
        ("absence", "match"),
        [
            ([_RESIDUAL, _RESIDUAL], "absence was not proved after destroy"),
            ([_RESIDUAL, _ABSENT, _RESIDUAL], "reappeared during retained cleanup"),
        ],
        ids=["never-absent", "reappeared"],
    )
    def test_unproved_absence_after_a_successful_destroy_is_an_attempt_error(
        self, absence: list[dict[str, Any]], match: str
    ) -> None:
        ctx = self._ctx()
        ctx.stack_manager.destroy_orchestrated.return_value = (True, ["gco-live-global"], [])

        with (
            self._teardown_helpers(absence=[dict(entry) for entry in absence]) as helpers,
            pytest.raises(RuntimeError, match=f"last failure: RuntimeError: .*{match}"),
        ):
            actions_destroy.destroy_deployment(ctx)

        attempt = ctx.checkpoint.state["destroy_attempts"][0]
        assert attempt["overall_success"] is False
        assert attempt["successful_stacks"] == ["gco-live-global"]
        assert match in attempt["error"]
        assert attempt["log_cleanup_helper_cleanup"] == {"deleted": True}
        helpers["delete_helper"].assert_called_once_with(ctx)
        assert ctx.checkpoint.destroyed is False


class TestActionFinalInventory:
    _PROJECT_INVENTORY = {"cloudformation_stacks": {}, "regional": {}, "global_accelerators": []}

    def _ctx(self, *, enabled: bool = True) -> Any:
        ctx = _context(state={"enabled_regions": ["us-east-1"]} if enabled else {})
        ctx.settings.protected_stack_names = ("CDKToolkit",)
        return ctx

    @contextlib.contextmanager
    def _inventory(
        self,
        *,
        stack_absence: dict[str, Any],
        differences: list[Any],
        absent: bool,
    ) -> Iterator[None]:
        inventory = dict(self._PROJECT_INVENTORY)
        with (
            patch_live_validation_helper(
                "_verify_target_stack_absence", return_value=stack_absence
            ),
            patch_live_validation_helper("capture_baseline", return_value=dict(_BASELINE)),
            patch_live_validation_helper(
                "_strip_expected_retained_ecr",
                return_value=(dict(_BASELINE), [{"repository": "accepted"}]),
            ),
            patch_live_validation_helper("compare_baseline", return_value=differences),
            patch_live_validation_helper("collect_project_resources", return_value=inventory),
            patch_live_validation_helper("_strip_baseline_ecr", return_value=inventory),
            patch_live_validation_helper("_strip_accepted_retained_ecr", return_value=inventory),
            patch_live_validation_helper(
                "_strip_expected_pending_kms", return_value=(inventory, [{"key": "pending"}])
            ),
            patch_live_validation_helper(
                "_strip_accepted_efs_automatic_backup_recovery_points",
                return_value=(inventory, []),
            ),
            patch_live_validation_helper(
                "_strip_expired_table_streams", return_value=(inventory, [{"arn": "stream"}])
            ),
            patch_live_validation_helper("summarize_project_resources", return_value={"total": 0}),
            patch_live_validation_helper("project_resources_are_absent", return_value=absent),
        ):
            yield

    def test_requires_a_baseline_and_enabled_regions(self) -> None:
        ctx = self._ctx()
        ctx.checkpoint.baseline = None
        with pytest.raises(RuntimeError, match="cannot compare without a baseline"):
            actions_final_inventory.action_final_inventory(ctx)

        ctx = self._ctx(enabled=False)
        with pytest.raises(RuntimeError, match="omitted enabled Regions"):
            actions_final_inventory.action_final_inventory(ctx)

    def test_clean_account_returns_the_full_evidence_bundle(self) -> None:
        ctx = self._ctx()
        ctx.checkpoint.destroyed = True
        ctx.checkpoint.completed_actions = ["destroy"]
        absent = {"all_absent": True, "absent": [], "residual": []}

        with self._inventory(stack_absence=absent, differences=[], absent=True):
            result = actions_final_inventory.action_final_inventory(ctx)

        assert result["summary"] == {"total": 0}
        assert result["stack_absence"] == absent
        assert result["baseline_differences"] == []
        assert result["accepted_retained_ecr"] == [{"repository": "accepted"}]
        assert result["accepted_pending_kms_keys"] == [{"key": "pending"}]
        assert result["accepted_expired_dynamodb_streams"] == [{"arn": "stream"}]
        assert result["residual_project_resources"] == self._PROJECT_INVENTORY
        assert ctx.report.final_inventory is result
        assert ctx.checkpoint.state["final_inventory"] == result
        assert ctx.checkpoint.destroyed is True
        assert ctx.checkpoint.completed_actions == ["destroy"]
        ctx.persist.assert_called_once()

    def test_baseline_differences_fail_after_persisting_evidence(self) -> None:
        ctx = self._ctx()
        absent = {"all_absent": True, "absent": [], "residual": []}
        differences = [{"stack": "CDKToolkit", "change": "modified"}]

        with (
            self._inventory(stack_absence=absent, differences=differences, absent=True),
            pytest.raises(RuntimeError, match="Protected stack/ECR baseline changed"),
        ):
            actions_final_inventory.action_final_inventory(ctx)

        assert ctx.checkpoint.state["final_inventory"]["baseline_differences"] == differences

    def test_residual_project_resources_fail(self) -> None:
        ctx = self._ctx()
        absent = {"all_absent": True, "absent": [], "residual": []}

        with (
            self._inventory(stack_absence=absent, differences=[], absent=False),
            pytest.raises(RuntimeError, match="Project resources remain after teardown"),
        ):
            actions_final_inventory.action_final_inventory(ctx)

    def test_residual_stack_reopens_partially_recorded_teardown(self) -> None:
        ctx = self._ctx()
        ctx.checkpoint.destroyed = True
        ctx.checkpoint.completed_actions = ["destroy"]
        residual = {"all_absent": False, "absent": [], "residual": [{"name": "gco-live-global"}]}

        with (
            self._inventory(stack_absence=residual, differences=[], absent=True),
            pytest.raises(RuntimeError, match="Target stacks remain after teardown"),
        ):
            actions_final_inventory.action_final_inventory(ctx)

        assert ctx.checkpoint.destroyed is False
        assert ctx.checkpoint.completed_actions == []
        assert ctx.checkpoint.state["stale_destroyed_reconciliations"][0]["source"] == (
            "final-inventory"
        )


class TestActionInference:
    def _ctx(self, tmp_path: Path, settings: Any) -> SimpleNamespace:
        ctx = SimpleNamespace(
            settings=settings,
            deployment_regions=("us-east-1",),
            config=SimpleNamespace(project_name="gco", global_region="us-west-2"),
            checkpoint=SimpleNamespace(state={}),
        )
        ctx.persist = MagicMock()
        return ctx

    @contextlib.contextmanager
    def _session(self, lifecycle: Any) -> Iterator[MagicMock]:
        sessions = MagicMock()

        @contextlib.contextmanager
        def cluster_session(*args: Any, **kwargs: Any) -> Iterator[Any]:
            sessions(*args, **kwargs)
            yield lambda *command, **options: (0, "{}", "")

        state: dict[str, Any] = {"phase": "initialized"}
        with (
            patch.object(actions_inference, "initialize_run_state", return_value=((), state)),
            patch.object(actions_inference.kube, "cluster_session", cluster_session),
            patch.object(actions_inference, "ManagedInferenceLifecycle", return_value=lifecycle),
        ):
            sessions.state = state
            yield sessions

    def test_inference_requires_the_main_settings_contract(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path, _run_settings(tmp_path))

        with (
            patch.object(actions_inference, "initialize_run_state") as initialize,
            pytest.raises(ManagedInferenceValidationError, match="requires the main RunSettings"),
        ):
            actions_inference.action_inference(ctx)

        initialize.assert_not_called()

    def test_selected_region_must_be_deployed(self, tmp_path: Path) -> None:
        settings = _inference_settings(tmp_path, selected_region="eu-west-1")
        ctx = self._ctx(tmp_path, settings)

        with (
            self._session(MagicMock()) as sessions,
            pytest.raises(ManagedInferenceValidationError, match="not part of this deployment"),
        ):
            actions_inference.action_inference(ctx)

        assert sessions.state["session_error"] == (
            "selected region is not in the deployed regional topology"
        )
        ctx.persist.assert_called_once_with()
        sessions.assert_not_called()

    def test_kubeconfig_must_stay_inside_the_private_report_directory(self, tmp_path: Path) -> None:
        class _EscapingSettings(RunSettings):
            @property
            def kubeconfig_path(self) -> Path:
                return Path("/tmp/elsewhere/kubeconfig")

        settings = _inference_settings(tmp_path)
        escaping = _EscapingSettings(
            **{f.name: getattr(settings, f.name) for f in dataclasses.fields(settings)}
        )
        ctx = self._ctx(tmp_path, escaping)

        with (
            self._session(MagicMock()) as sessions,
            pytest.raises(ManagedInferenceValidationError, match="escaped the private report dir"),
        ):
            actions_inference.action_inference(ctx)

        sessions.assert_not_called()

    def test_successful_session_verifies_the_shared_proxy_then_executes(
        self, tmp_path: Path
    ) -> None:
        settings = _inference_settings(tmp_path)
        ctx = self._ctx(tmp_path, settings)
        lifecycle = MagicMock()
        lifecycle.execute.return_value = {"all_endpoints_absent": True}

        with self._session(lifecycle) as sessions:
            assert actions_inference.action_inference(ctx) == {"all_endpoints_absent": True}

        sessions.assert_called_once_with(
            settings.repo_root,
            "gco-us-east-1",
            "us-east-1",
            kubeconfig_path=settings.kubeconfig_path,
            gco_command=(sys.executable, "-m", "cli.main"),
        )
        lifecycle.verify_shared_proxy_autoscaling.assert_called_once_with(sessions.state)
        lifecycle.execute.assert_called_once_with()
        ctx.persist.assert_not_called()

    def test_validation_errors_propagate_unchanged(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path, _inference_settings(tmp_path))
        lifecycle = MagicMock()
        lifecycle.verify_shared_proxy_autoscaling.side_effect = ManagedInferenceValidationError(
            "proxy HPA missing"
        )

        with (
            self._session(lifecycle) as sessions,
            pytest.raises(ManagedInferenceValidationError, match="proxy HPA missing"),
        ):
            actions_inference.action_inference(ctx)

        assert "session_error" not in sessions.state
        ctx.persist.assert_not_called()

    def test_session_failures_are_checkpointed_then_reported_without_chaining(
        self, tmp_path: Path
    ) -> None:
        ctx = self._ctx(tmp_path, _inference_settings(tmp_path))
        lifecycle = MagicMock()
        lifecycle.execute.side_effect = subprocess.TimeoutExpired("kubectl", 5)

        with (
            self._session(lifecycle) as sessions,
            pytest.raises(
                ManagedInferenceValidationError, match="cluster session failed"
            ) as excinfo,
        ):
            actions_inference.action_inference(ctx)

        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True
        assert sessions.state["session_error"].startswith("TimeoutExpired: ")
        ctx.persist.assert_called_once_with()

    def test_keyboard_interrupt_is_checkpointed_and_re_raised(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path, _inference_settings(tmp_path))
        lifecycle = MagicMock()
        lifecycle.execute.side_effect = KeyboardInterrupt

        with self._session(lifecycle) as sessions, pytest.raises(KeyboardInterrupt):
            actions_inference.action_inference(ctx)

        assert sessions.state["session_error"] == "KeyboardInterrupt: "
        ctx.persist.assert_called_once_with()


class TestActionJobs:
    _NAME = "gco-live-sqs-run-123"
    _NAMESPACE = "gco-jobs"
    _MANIFESTS = [{"kind": "Job", "metadata": {"name": _NAME, "namespace": _NAMESPACE}}]

    def test_api_lifecycle_delegates_to_the_shared_transport(self) -> None:
        ctx = _context()

        with patch_live_validation_helper(
            "_run_api_transport_lifecycle", return_value={"status": "succeeded"}
        ) as lifecycle:
            assert actions_jobs.action_api_lifecycle(ctx) == {"status": "succeeded"}

        lifecycle.assert_called_once_with(
            ctx,
            manifest_filename="api-smoke-job.yaml",
            path="api",
            marker_prefix="API",
        )

    def _record(self, state: str, **extra: Any) -> dict[str, Any]:
        return {
            "name": self._NAME,
            "namespace": self._NAMESPACE,
            "region": "us-east-1",
            "path": "sqs",
            "transport_region": "us-east-1",
            "submission_state": state,
            **extra,
        }

    @contextlib.contextmanager
    def _sqs_helpers(
        self,
        record: dict[str, Any],
        *,
        existing: Any = None,
        reconciled: Any = None,
        appeared: Any = None,
    ) -> Iterator[dict[str, MagicMock]]:
        with (
            patch_live_validation_helper(
                "_load_manifest", return_value=(self._MANIFESTS, self._NAME, self._NAMESPACE)
            ),
            patch_live_validation_helper("_register_job", return_value=record) as register,
            patch_live_validation_helper("_get_owned_job", return_value=existing),
            patch_live_validation_helper(
                "_wait_for_ambiguous_job_reconciliation", return_value=reconciled
            ) as reconcile,
            patch_live_validation_helper(
                "_wait_for_owned_job_appearance", return_value=appeared
            ) as appearance,
            patch_live_validation_helper("_job_appearance_timeout", return_value=45),
            patch_live_validation_helper(
                "_complete_job_lifecycle", return_value={"status": "succeeded"}
            ) as complete,
        ):
            yield {
                "register": register,
                "reconcile": reconcile,
                "appearance": appearance,
                "complete": complete,
            }

    def test_fresh_sqs_submission_is_checkpointed_around_the_transport_call(self) -> None:
        ctx = _context()
        ctx.block_job_submission = MagicMock()
        record = self._record("prepared")
        ctx.job_manager.submit_job_sqs.return_value = {"job_name": self._NAME, "message_id": "m"}

        with self._sqs_helpers(record) as helpers:
            result = actions_jobs.action_sqs_lifecycle(ctx)

        assert result == {
            "status": "succeeded",
            "submission": {"job_name": self._NAME, "message_id": "m"},
        }
        helpers["register"].assert_called_once_with(
            ctx,
            name=self._NAME,
            namespace=self._NAMESPACE,
            execution_region="us-east-1",
            path="sqs",
        )
        ctx.prepare_job_submission.assert_called_once_with(
            record,
            envelope={
                "transport": "direct-sqs",
                "manifests": self._MANIFESTS,
                "region": "us-east-1",
                "namespace": self._NAMESPACE,
                "labels": {_RUN_JOB_LABEL: "run-123"},
                "priority": 100,
            },
            resumable=False,
        )
        ctx.begin_job_submission.assert_called_once_with(record, reconciliation_timeout_seconds=45)
        ctx.job_manager.submit_job_sqs.assert_called_once_with(
            self._MANIFESTS,
            region="us-east-1",
            namespace=self._NAMESPACE,
            labels={_RUN_JOB_LABEL: "run-123"},
            priority=100,
        )
        ctx.finish_job_submission.assert_called_once_with(
            record, {"job_name": self._NAME, "message_id": "m"}, appearance_timeout_seconds=45
        )
        assert ctx.checkpoint.state["sqs_submission"] == {"job_name": self._NAME, "message_id": "m"}
        helpers["complete"].assert_called_once_with(
            ctx, record=record, marker="GCO_LIVE_SQS_run-123"
        )
        ctx.block_job_submission.assert_not_called()

    def test_unexpected_submission_name_fails_before_finishing(self) -> None:
        ctx = _context()
        record = self._record("prepared")
        ctx.job_manager.submit_job_sqs.return_value = {"job_name": "someone-else"}

        with (
            self._sqs_helpers(record),
            pytest.raises(RuntimeError, match="unexpected job name: someone-else"),
        ):
            actions_jobs.action_sqs_lifecycle(ctx)

        ctx.finish_job_submission.assert_not_called()

    def test_existing_job_is_reconciled_without_a_second_submission(self) -> None:
        ctx = _context()
        record = self._record("submitted")

        with self._sqs_helpers(record, existing={"metadata": {"uid": "uid-1"}}) as helpers:
            result = actions_jobs.action_sqs_lifecycle(ctx)

        assert result["submission"] == {"reconciled_existing_job": True}
        ctx.job_manager.submit_job_sqs.assert_not_called()
        ctx.begin_job_submission.assert_not_called()
        helpers["reconcile"].assert_not_called()
        helpers["appearance"].assert_not_called()

    def test_ambiguous_submission_that_reconciles_is_reused(self) -> None:
        ctx = _context()
        record = self._record("submitting")

        with self._sqs_helpers(record, reconciled={"metadata": {"uid": "uid-1"}}) as helpers:
            result = actions_jobs.action_sqs_lifecycle(ctx)

        assert result["submission"] == {"reconciled_existing_job": True}
        helpers["reconcile"].assert_called_once_with(ctx, record)
        ctx.job_manager.submit_job_sqs.assert_not_called()

    def test_ambiguous_submission_without_a_job_is_blocked_forever(self) -> None:
        ctx = _context()
        ctx.block_job_submission = MagicMock()
        record = self._record("submitting")

        with (
            self._sqs_helpers(record),
            pytest.raises(RuntimeError, match="automatic replay is forbidden"),
        ):
            actions_jobs.action_sqs_lifecycle(ctx)

        ctx.block_job_submission.assert_called_once()
        assert ctx.block_job_submission.call_args.args[0] is record
        assert "non-idempotent boundary" in ctx.block_job_submission.call_args.args[1]
        ctx.job_manager.submit_job_sqs.assert_not_called()

    def test_acknowledged_submission_waits_for_appearance(self) -> None:
        ctx = _context()
        record = self._record("submitted")

        with self._sqs_helpers(record, appeared={"metadata": {"uid": "uid-1"}}) as helpers:
            result = actions_jobs.action_sqs_lifecycle(ctx)

        assert result["submission"] == {"reconciled_existing_job": True}
        helpers["appearance"].assert_called_once_with(ctx, record)
        ctx.job_manager.submit_job_sqs.assert_not_called()

    def test_acknowledged_submission_that_never_appears_cannot_be_resubmitted(self) -> None:
        ctx = _context()
        record = self._record("submitted")

        with (
            self._sqs_helpers(record),
            pytest.raises(RuntimeError, match="Cannot submit SQS Job from state 'submitted'"),
        ):
            actions_jobs.action_sqs_lifecycle(ctx)

        ctx.job_manager.submit_job_sqs.assert_not_called()

    @pytest.mark.parametrize(
        ("extra", "match"),
        [
            ({"submission_blocked_reason": "earlier ambiguity"}, "earlier ambiguity"),
            ({}, "SQS submission blocked"),
        ],
        ids=["recorded-reason", "default-reason"],
    )
    def test_blocked_records_fail_with_the_checkpointed_reason(
        self, extra: dict[str, Any], match: str
    ) -> None:
        ctx = _context()
        record = self._record("blocked", **extra)

        with self._sqs_helpers(record), pytest.raises(RuntimeError, match=match):
            actions_jobs.action_sqs_lifecycle(ctx)

        ctx.job_manager.submit_job_sqs.assert_not_called()


class TestActionPreflight:
    _TOOLKIT = {
        "stack_id": "arn:aws:cloudformation:us-east-1:123456789012:stack/CDKToolkit/toolkit",
        "status": "CREATE_COMPLETE",
    }
    _ECR_IMAGES = [{"repository": "gco-live/worker", "digest": "sha256:" + "0" * 64}]

    def _ctx(
        self,
        tmp_path: Path,
        *,
        selected: tuple[str, ...] = ("preflight", "baseline", "deploy", "topology"),
        regions: tuple[str, ...] = ("us-east-1",),
        deployment_attempted: bool = False,
        state: dict[str, Any] | None = None,
        direct_access: bool = True,
    ) -> Any:
        ctx = _context(state=dict(state or {}))
        ctx.settings.repo_root = tmp_path
        ctx.settings.expected_sha = _SHA
        ctx.settings.expected_branch = "chore/test"
        ctx.settings.profile = "configured"
        ctx.settings.resume = deployment_attempted
        ctx.checkpoint.deployment_attempted = deployment_attempted
        ctx.deployment_regions = regions
        ctx.cdk_context = {
            **_cdk_context(regions),
            "api_gateway": {"regional_api_enabled": direct_access},
        }
        ctx.report = SimpleNamespace(selected_actions=list(selected))
        sts = MagicMock()
        sts.get_caller_identity.return_value = {
            "Account": _ACCOUNT,
            "Arn": f"arn:aws:sts::{_ACCOUNT}:assumed-role/Operator/session",
        }
        ctx.session.client.return_value = sts
        ctx.session.get_partition_for_region.return_value = "aws"
        stacks = ["gco-live-global", *(f"gco-live-{region}" for region in regions)]
        ctx.stack_manager.list_stacks.return_value = stacks
        ctx.stack_manager._get_destroy_region.side_effect = lambda name: (
            "us-west-2" if name == "gco-live-global" else name.removeprefix("gco-live-")
        )
        return ctx

    @contextlib.contextmanager
    def _boundaries(
        self,
        *,
        head: str = _SHA,
        dirty: str = "",
        branch: str = "chore/test",
        enabled_regions: list[str] | None = None,
        toolkit: dict[str, dict[str, Any] | None] | None = None,
        existing: dict[str, Any] | None = None,
        plugin: str | None = "/usr/local/bin/session-manager-plugin",
    ) -> Iterator[dict[str, MagicMock]]:
        regions = enabled_regions or ["us-east-1", "us-west-2", "eu-west-1"]
        toolkits = toolkit or dict.fromkeys(regions, self._TOOLKIT)
        with (
            patch_live_validation_helper("_run_git", side_effect=[head, dirty]) as run_git,
            patch_live_validation_helper("_resolve_branch", return_value=branch),
            patch_live_validation_helper(
                "discover_enabled_regions", return_value=list(regions)
            ) as discover,
            patch_live_validation_helper(
                "describe_stack",
                side_effect=lambda session, region, name: toolkits.get(region),
            ) as describe,
            patch_live_validation_helper(
                "_expected_ecr_images", return_value=list(self._ECR_IMAGES)
            ) as ecr,
            patch_live_validation_helper(
                "collect_project_stacks", return_value=dict(existing or {})
            ) as collect,
            patch_live_validation_helper("_reconcile_stack_ownership") as reconcile,
            patch.object(actions_preflight.shutil, "which", return_value=plugin) as which,
        ):
            yield {
                "run_git": run_git,
                "discover": discover,
                "describe": describe,
                "ecr": ecr,
                "collect": collect,
                "reconcile": reconcile,
                "which": which,
            }

    def test_fresh_preflight_checkpoints_identity_and_returns_evidence(
        self, tmp_path: Path
    ) -> None:
        ctx = self._ctx(tmp_path)

        with self._boundaries() as boundaries:
            result = actions_preflight.action_preflight(ctx)

        assert result["account"] == _ACCOUNT
        assert result["caller_arn"].endswith("assumed-role/Operator/session")
        assert result["sha"] == _SHA
        assert result["branch"] == "chore/test"
        assert result["profile"] == "configured"
        assert result["deployment_regions"] == ["us-east-1"]
        assert result["topology_regions"] == ["us-west-2", "us-east-1"]
        assert result["enabled_regions"] == ["us-east-1", "us-west-2", "eu-west-1"]
        assert result["target_stack_regions"] == {
            "gco-live-global": "us-west-2",
            "gco-live-us-east-1": "us-east-1",
        }
        assert result["bootstrap_stacks"] == {
            "us-east-1": self._TOOLKIT,
            "us-west-2": self._TOOLKIT,
        }
        assert result["expected_ecr_images"] == self._ECR_IMAGES
        assert result["direct_regional_access"] is True
        assert result["session_manager_plugin"] == "not-required"
        assert result["kms_key_deletion_confirmed"] is True
        assert result["resume"] is False
        state = ctx.checkpoint.state
        assert state["account_arn"] == result["caller_arn"]
        assert state["enabled_regions"] == result["enabled_regions"]
        assert state["target_stack_regions"] == result["target_stack_regions"]
        assert state["topology_regions"] == result["topology_regions"]
        assert state["bootstrap_stacks"] == result["bootstrap_stacks"]
        assert state["expected_ecr_images"] == self._ECR_IMAGES
        assert state["direct_regional_access"] is True
        assert state["preexisting_project_stacks"] == {}
        ctx.persist.assert_called_once_with()
        boundaries["reconcile"].assert_not_called()
        boundaries["which"].assert_not_called()
        boundaries["run_git"].assert_any_call(tmp_path, "rev-parse", "HEAD")
        boundaries["run_git"].assert_any_call(
            tmp_path, "status", "--porcelain=v1", "--untracked-files=all"
        )
        boundaries["ecr"].assert_called_once_with(ctx, ["gco-live-global", "gco-live-us-east-1"])
        boundaries["collect"].assert_called_once_with(
            ctx.session, result["enabled_regions"], "gco-live"
        )
        ctx.session.client.assert_called_once_with("sts", region_name="us-east-1")

    def test_resumed_preflight_reconciles_ownership_and_keeps_preexisting_evidence(
        self, tmp_path: Path
    ) -> None:
        ctx = self._ctx(
            tmp_path,
            selected=("preflight", "baseline", "deploy", "topology", "inference"),
            deployment_attempted=True,
            state={
                "preexisting_project_stacks": {"kept": True},
                "bootstrap_stacks": {"us-east-1": self._TOOLKIT, "us-west-2": self._TOOLKIT},
                "expected_ecr_images": list(self._ECR_IMAGES),
                "target_stack_regions": {
                    "gco-live-global": "us-west-2",
                    "gco-live-us-east-1": "us-east-1",
                },
            },
        )

        with self._boundaries(existing={"us-east-1": ["gco-live-us-east-1"]}) as boundaries:
            result = actions_preflight.action_preflight(ctx)

        assert result["session_manager_plugin"] == "/usr/local/bin/session-manager-plugin"
        assert result["resume"] is True
        assert ctx.checkpoint.state["preexisting_project_stacks"] == {"kept": True}
        boundaries["reconcile"].assert_called_once_with(ctx)
        boundaries["which"].assert_called_once_with("session-manager-plugin")

    def test_git_identity_mismatches_fail_before_any_aws_call(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        with (
            self._boundaries(head="b" * 40),
            pytest.raises(RuntimeError, match="does not match expected SHA"),
        ):
            actions_preflight.action_preflight(ctx)

        with (
            self._boundaries(branch="main"),
            pytest.raises(RuntimeError, match="Current branch 'main' does not match"),
        ):
            actions_preflight.action_preflight(ctx)

        with (
            self._boundaries(dirty=" M cdk.json"),
            pytest.raises(RuntimeError, match="requires a clean worktree.*\n M cdk.json"),
        ):
            actions_preflight.action_preflight(ctx)

        ctx.session.client.assert_not_called()

    def test_missing_session_manager_plugin_blocks_inference_runs(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path, selected=("preflight", "inference"))

        with (
            self._boundaries(plugin=None),
            pytest.raises(RuntimeError, match="Session Manager plugin"),
        ):
            actions_preflight.action_preflight(ctx)

        ctx.session.client.assert_not_called()

    @pytest.mark.parametrize("action", ["platform-workloads", "network-posture"])
    def test_every_cluster_facing_action_needs_the_tunnel_plugin(
        self, tmp_path: Path, action: str
    ) -> None:
        """The kubectl checks tunnel through SSM exactly like inference does."""
        from scripts.live_release_validation.constants import _CLUSTER_TUNNEL_ACTIONS
        from scripts.live_release_validation.registry import build_action_registry

        assert _CLUSTER_TUNNEL_ACTIONS <= set(build_action_registry())
        ctx = self._ctx(tmp_path, selected=("preflight", "topology", action))

        with (
            self._boundaries(plugin=None),
            pytest.raises(RuntimeError, match=f"The {action} action\\(s\\) reach the private"),
        ):
            actions_preflight.action_preflight(ctx)

        ctx.session.client.assert_not_called()
        with self._boundaries() as boundaries:
            result = actions_preflight.action_preflight(ctx)
        assert result["session_manager_plugin"] == "/usr/local/bin/session-manager-plugin"
        boundaries["which"].assert_called_once_with("session-manager-plugin")

    @pytest.mark.parametrize(
        ("identity", "match"),
        [
            ({"Account": "999999999999"}, "caller account 999999999999 does not match"),
            ({}, "caller account unknown does not match"),
        ],
    )
    def test_caller_account_must_match(
        self, tmp_path: Path, identity: dict[str, Any], match: str
    ) -> None:
        ctx = self._ctx(tmp_path)
        ctx.session.client.return_value.get_caller_identity.return_value = identity

        with self._boundaries() as boundaries, pytest.raises(RuntimeError, match=match):
            actions_preflight.action_preflight(ctx)

        boundaries["discover"].assert_not_called()

    def test_deploy_requires_explicit_kms_deletion_consent(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        ctx.settings.confirm_kms_key_deletion = False

        with (
            self._boundaries() as boundaries,
            pytest.raises(RuntimeError, match="--confirm-kms-key-deletion"),
        ):
            actions_preflight.action_preflight(ctx)

        boundaries["discover"].assert_not_called()

    def test_multi_region_job_actions_require_direct_regional_access(self, tmp_path: Path) -> None:
        ctx = self._ctx(
            tmp_path,
            selected=("preflight", "baseline", "deploy", "topology", "sqs"),
            regions=("us-east-1", "eu-west-1"),
            direct_access=False,
        )

        with (
            self._boundaries(),
            pytest.raises(RuntimeError, match="Multi-Region Job actions require"),
        ):
            actions_preflight.action_preflight(ctx)

    def test_multi_region_without_job_actions_is_allowed_without_direct_access(
        self, tmp_path: Path
    ) -> None:
        ctx = self._ctx(
            tmp_path,
            regions=("us-east-1", "eu-west-1"),
            direct_access=False,
        )

        with self._boundaries():
            result = actions_preflight.action_preflight(ctx)

        assert result["direct_regional_access"] is False
        assert result["deployment_regions"] == ["us-east-1", "eu-west-1"]
        assert set(result["bootstrap_stacks"]) == {"eu-west-1", "us-east-1", "us-west-2"}

    def test_cdk_target_stacks_must_exist_and_belong_to_the_project(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        ctx.stack_manager.list_stacks.return_value = []
        with self._boundaries(), pytest.raises(RuntimeError, match="CDK returned no target stacks"):
            actions_preflight.action_preflight(ctx)

        ctx.stack_manager.list_stacks.return_value = ["gco-live-global", "Zeta", "Alpha"]
        with (
            self._boundaries(),
            pytest.raises(RuntimeError, match="non-project CDK stacks: Alpha, Zeta"),
        ):
            actions_preflight.action_preflight(ctx)

    def test_every_target_stack_needs_a_resolvable_enabled_region(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        ctx.stack_manager._get_destroy_region.side_effect = lambda name: (
            None if name == "gco-live-global" else "us-east-1"
        )
        with (
            self._boundaries(),
            pytest.raises(
                RuntimeError,
                match='Could not resolve target stack Regions: {"gco-live-global": null',
            ),
        ):
            actions_preflight.action_preflight(ctx)

        ctx = self._ctx(tmp_path)
        with (
            self._boundaries(enabled_regions=["us-east-1"]),
            pytest.raises(RuntimeError, match="not enabled for this account: us-west-2"),
        ):
            actions_preflight.action_preflight(ctx)

    @pytest.mark.parametrize(
        ("toolkit", "found"),
        [
            (None, "absent"),
            ({"stack_id": "toolkit", "status": "ROLLBACK_COMPLETE"}, "ROLLBACK_COMPLETE"),
        ],
        ids=["absent", "unhealthy"],
    )
    def test_target_regions_need_a_healthy_bootstrap_stack(
        self, tmp_path: Path, toolkit: dict[str, Any] | None, found: str
    ) -> None:
        ctx = self._ctx(tmp_path)

        with (
            self._boundaries(toolkit={"us-east-1": self._TOOLKIT, "us-west-2": toolkit}),
            pytest.raises(
                RuntimeError,
                match=f"Region us-west-2 must already contain a healthy CDKToolkit stack; found {found}",
            ),
        ):
            actions_preflight.action_preflight(ctx)

    @pytest.mark.parametrize(
        ("state", "match"),
        [
            (
                {"bootstrap_stacks": {"us-east-1": {"stack_id": "other", "status": "x"}}},
                "CDKToolkit ARN/status changed",
            ),
            ({"expected_ecr_images": []}, "ECR image targets changed"),
            (
                {"target_stack_regions": {"gco-live-global": "us-west-2"}},
                "CDK target stacks changed since the checkpoint",
            ),
        ],
        ids=["bootstrap", "ecr", "targets"],
    )
    def test_checkpointed_identities_must_not_drift(
        self, tmp_path: Path, state: dict[str, Any], match: str
    ) -> None:
        ctx = self._ctx(tmp_path, deployment_attempted=True, state=state)

        with self._boundaries(), pytest.raises(RuntimeError, match=match):
            actions_preflight.action_preflight(ctx)

        ctx.persist.assert_not_called()

    def test_fresh_runs_refuse_preexisting_project_stacks(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)

        with (
            self._boundaries(existing={"us-east-1": ["gco-live-us-east-1"]}),
            pytest.raises(RuntimeError, match="refuse pre-existing project stacks"),
        ):
            actions_preflight.action_preflight(ctx)

        ctx.persist.assert_not_called()


class _MappingStack(UserDict[str, Any]):
    """A Mapping-shaped stack description that is deliberately not a ``dict``."""


class TestActionTopology:
    _STACKS = {
        "gco-live-global": {"name": "gco-live-global", "status": "CREATE_COMPLETE"},
        "gco-live-us-east-1": {"name": "gco-live-us-east-1", "status": "UPDATE_COMPLETE"},
    }

    def _ctx(self, *, direct_access: bool = True) -> Any:
        ctx = _context(
            state={
                "target_stack_regions": {
                    "gco-live-global": "us-west-2",
                    "gco-live-us-east-1": "us-east-1",
                }
            }
        )
        ctx.session.get_partition_for_region.return_value = "aws"
        ctx.cdk_context = {"api_gateway": {"regional_api_enabled": direct_access}}
        eks = MagicMock()
        eks.describe_cluster.return_value = {
            "cluster": {
                "arn": "arn:aws:eks:us-east-1:123456789012:cluster/gco-live-us-east-1",
                "status": "ACTIVE",
                "version": "1.33",
                "resourcesVpcConfig": {
                    "endpointPublicAccess": False,
                    "endpointPrivateAccess": True,
                },
            }
        }
        dynamodb = MagicMock()
        dynamodb.describe_table.return_value = {
            "Table": {
                "TableStatus": "ACTIVE",
                "TableArn": "arn:aws:dynamodb:us-east-1:123456789012:table/gco-live-jobs",
            }
        }
        ctx.clients = {"eks": eks, "dynamodb": dynamodb}
        ctx.session.client.side_effect = lambda service, region_name=None: ctx.clients[service]
        ctx.aws_client.get_api_endpoint.return_value = SimpleNamespace(
            url="https://global.example/api"
        )
        ctx.aws_client.get_regional_api_endpoint.return_value = SimpleNamespace(
            url="https://regional.example/api"
        )
        ctx.job_manager.get_queue_status.return_value = {
            "messages_available": 0,
            "dlq_messages": 0,
        }
        return ctx

    @contextlib.contextmanager
    def _checks(self, *, stacks: dict[str, Any] | None = None) -> Iterator[dict[str, MagicMock]]:
        described = self._STACKS if stacks is None else stacks
        health = [
            {"scope": "global", "region": None, "payload": {"status": "healthy", "round": 1}},
            {"scope": "regional", "region": "us-east-1", "payload": {"status": "healthy"}},
            {"scope": "global", "region": None, "payload": {"status": "healthy", "round": 2}},
        ]
        with (
            patch_live_validation_helper("_reconcile_stack_ownership") as reconcile,
            patch_live_validation_helper(
                "describe_stack", side_effect=lambda session, region, name: described.get(name)
            ),
            patch_live_validation_helper("_record_stack_identity") as record_identity,
            patch_live_validation_helper("_converge_region_addons") as converge,
            patch_live_validation_helper(
                "_alb_https_target_evidence", return_value={"listener": "https"}
            ) as alb,
            patch_live_validation_helper("_health_warmup_samples", return_value=[{"warmup": True}]),
            patch_live_validation_helper("_health_stability_samples", return_value=health),
            patch_live_validation_helper(
                "_metrics_reachability_samples", return_value=[{"metrics": True}]
            ),
        ):
            yield {
                "reconcile": reconcile,
                "record_identity": record_identity,
                "converge": converge,
                "alb": alb,
            }

    def test_full_topology_evidence_with_direct_regional_access(self) -> None:
        ctx = self._ctx()

        with self._checks() as checks:
            result = actions_topology.action_topology(ctx)

        assert result["stacks"] == self._STACKS
        assert result["clusters"]["us-east-1"] == {
            "name": "gco-live-us-east-1",
            "arn": "arn:aws:eks:us-east-1:123456789012:cluster/gco-live-us-east-1",
            "status": "ACTIVE",
            "version": "1.33",
            "endpoint_public_access": False,
            "endpoint_private_access": True,
        }
        assert result["convergence"]["status"] == "succeeded"
        assert result["convergence"]["regions"]["us-east-1"]["result"] == "succeeded"
        assert result["alb_https_targets"] == {"us-east-1": {"listener": "https"}}
        assert result["global_api"]["url"] == "https://global.example/api"
        assert result["global_api"]["health"] == {"status": "healthy", "round": 2}
        assert len(result["global_api"]["samples"]) == 2
        assert result["regional_apis"]["us-east-1"]["url"] == "https://regional.example/api"
        assert result["regional_apis"]["us-east-1"]["health"] == {"status": "healthy"}
        assert result["queue_baseline"] == {
            "us-east-1": {"messages_available": 0, "dlq_messages": 0}
        }
        assert result["jobs_table"] == {
            "name": "gco-live-jobs",
            "arn": "arn:aws:dynamodb:us-east-1:123456789012:table/gco-live-jobs",
            "status": "ACTIVE",
        }
        assert ctx.checkpoint.state["queue_baseline"] == result["queue_baseline"]
        assert ctx.checkpoint.state["topology_convergence"] is result["convergence"]
        checks["reconcile"].assert_called_once_with(ctx)
        assert checks["record_identity"].call_count == 2
        checks["converge"].assert_called_once()
        assert checks["converge"].call_args.kwargs["stack_name"] == "gco-live-us-east-1"
        checks["alb"].assert_called_once_with(
            ctx, region="us-east-1", cluster_name="gco-live-us-east-1"
        )
        ctx.aws_client.get_api_endpoint.assert_called_once_with(force_refresh=True)
        ctx.aws_client.get_regional_api_endpoint.assert_called_once_with(
            "us-east-1", force_refresh=True
        )
        ctx.clients["dynamodb"].describe_table.assert_called_once_with(TableName="gco-live-jobs")

    def test_regional_probes_are_skipped_without_direct_access(self) -> None:
        ctx = self._ctx(direct_access=False)

        with self._checks():
            result = actions_topology.action_topology(ctx)

        assert result["regional_apis"] == {
            "us-east-1": {
                "skipped": True,
                "reason": "direct caller access is disabled by cdk.json",
                "samples": [],
            }
        }
        ctx.aws_client.get_regional_api_endpoint.assert_not_called()

    def test_absent_or_unhealthy_stacks_fail_before_convergence(self) -> None:
        ctx = self._ctx()
        with (
            self._checks(stacks={"gco-live-global": self._STACKS["gco-live-global"]}) as checks,
            pytest.raises(RuntimeError, match="absent: gco-live-us-east-1 \\(us-east-1\\)"),
        ):
            actions_topology.action_topology(ctx)
        checks["converge"].assert_not_called()

        unhealthy = {
            **self._STACKS,
            "gco-live-global": {"name": "gco-live-global", "status": "ROLLBACK_COMPLETE"},
        }
        with (
            self._checks(stacks=unhealthy),
            pytest.raises(RuntimeError, match="gco-live-global is ROLLBACK_COMPLETE"),
        ):
            actions_topology.action_topology(ctx)

    def test_unbound_regional_stack_fails_convergence_with_persisted_evidence(self) -> None:
        ctx = self._ctx()
        ctx.checkpoint.state["target_stack_regions"] = {"gco-live-global": "us-west-2"}

        with (
            self._checks(),
            pytest.raises(RuntimeError, match="does not bind exact regional stack"),
        ):
            actions_topology.action_topology(ctx)

        convergence = ctx.checkpoint.state["topology_convergence"]
        assert convergence["status"] == "failed"
        evidence = convergence["regions"]["us-east-1"]
        assert evidence["result"] == "failed"
        assert evidence["error"].startswith("RuntimeError: Checkpoint does not bind")
        assert "completed_at" in evidence

    def test_non_dict_stack_description_is_rejected_as_undescribed(self) -> None:
        ctx = self._ctx()
        stacks = {
            **self._STACKS,
            "gco-live-us-east-1": _MappingStack(self._STACKS["gco-live-us-east-1"]),
        }

        with (
            self._checks(stacks=stacks),
            pytest.raises(RuntimeError, match="was not described: us-east-1:gco-live-us-east-1"),
        ):
            actions_topology.action_topology(ctx)

    def test_convergence_errors_are_bounded_and_re_raised(self) -> None:
        ctx = self._ctx()

        with self._checks() as checks, pytest.raises(TimeoutError, match="add-ons still running"):
            checks["converge"].side_effect = TimeoutError("add-ons still running")
            actions_topology.action_topology(ctx)

        evidence = ctx.checkpoint.state["topology_convergence"]["regions"]["us-east-1"]
        assert evidence["error"] == "TimeoutError: add-ons still running"
        ctx.clients["eks"].describe_cluster.assert_not_called()

    def test_inactive_cluster_fails(self) -> None:
        ctx = self._ctx()
        ctx.clients["eks"].describe_cluster.return_value = {"cluster": {"status": "UPDATING"}}

        with (
            self._checks(),
            pytest.raises(RuntimeError, match="gco-live-us-east-1 is not ACTIVE: UPDATING"),
        ):
            actions_topology.action_topology(ctx)

    def test_global_and_regional_endpoints_must_expose_urls(self) -> None:
        ctx = self._ctx()
        ctx.aws_client.get_api_endpoint.return_value = SimpleNamespace(url="")
        with self._checks(), pytest.raises(RuntimeError, match="Global API endpoint has no URL"):
            actions_topology.action_topology(ctx)

        ctx = self._ctx()
        ctx.aws_client.get_regional_api_endpoint.return_value = None
        with (
            self._checks(),
            pytest.raises(RuntimeError, match="regional API endpoint is absent in us-east-1"),
        ):
            actions_topology.action_topology(ctx)

    def test_fresh_queue_must_be_empty(self) -> None:
        ctx = self._ctx()
        ctx.job_manager.get_queue_status.return_value = {"messages_delayed": 2}

        with (
            self._checks(),
            pytest.raises(
                RuntimeError, match='Fresh queue in us-east-1 is not empty: {.*"delayed": 2'
            ),
        ):
            actions_topology.action_topology(ctx)

        assert "queue_baseline" not in ctx.checkpoint.state

    def test_jobs_table_must_be_active(self) -> None:
        ctx = self._ctx()
        ctx.clients["dynamodb"].describe_table.return_value = {"Table": {"TableStatus": "CREATING"}}

        with (
            self._checks(),
            pytest.raises(RuntimeError, match="DynamoDB table gco-live-jobs is not ACTIVE"),
        ):
            actions_topology.action_topology(ctx)


class TestActionCentralQueue:
    _KEY = "gco-live-validation:run-123:central"
    _MARKER = "GCO_LIVE_DDB_run-123"
    _MANIFEST = {"apiVersion": "batch/v1", "kind": "Job"}

    def _job_id(self) -> str:
        return checks_central_queue._central_queue_job_id(self._KEY)

    def _envelope(self) -> dict[str, Any]:
        return {
            "transport": "central-queue",
            "body": {
                "manifest": self._MANIFEST,
                "target_region": "us-east-1",
                "namespace": "gco-jobs",
                "priority": 100,
                "labels": {_RUN_JOB_LABEL: "run-123"},
            },
            "idempotency_key": self._KEY,
            "job_id": self._job_id(),
            "transport_region": None,
        }

    def _records(
        self,
        *,
        state: str,
        deleted: bool = False,
        bound: bool = False,
        evidence: dict[str, Any] | None = None,
        **extra: Any,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        job_id = self._job_id()
        queue_job = _central_job(job_id)
        record: dict[str, Any] = {
            "name": queue_job["job_name"],
            "namespace": queue_job["namespace"],
            "region": "us-east-1",
            "path": "dynamodb",
            "transport_region": None,
            "submission_state": state,
            "submission_envelope": self._envelope(),
            "appearance_deadline": 9999999999.0,
            "uid": None,
            "deleted": deleted,
            **extra,
        }
        if bound:
            record.update(
                {
                    "central_queue_job_id": job_id,
                    "k8s_job_name": queue_job["k8s_job_name"],
                    "k8s_job_namespace": queue_job["k8s_job_namespace"],
                    "k8s_job_uid": queue_job["k8s_job_uid"],
                    "uid": queue_job["k8s_job_uid"],
                }
            )
        if evidence is not None:
            record["validation_evidence"] = evidence
        central_record = {
            "job_id": job_id,
            "idempotency_key": self._KEY,
            "job_name": queue_job["job_name"],
            "namespace": queue_job["namespace"],
            "target_region": "us-east-1",
            "transport_region": None,
            "marker": self._MARKER,
            "appearance_deadline": 9999999999.0,
        }
        return queue_job, record, central_record

    @contextlib.contextmanager
    def _helpers(
        self,
        *,
        record: dict[str, Any],
        central_record: dict[str, Any],
        queue_job: dict[str, Any] | None,
        terminal: dict[str, Any],
        item: dict[str, Any],
    ) -> Iterator[dict[str, MagicMock]]:
        with (
            patch_live_validation_helper(
                "_central_manifest",
                return_value=(self._MANIFEST, record["name"], record["namespace"], self._MARKER),
            ),
            patch_live_validation_helper("_register_job", return_value=record),
            patch_live_validation_helper("_register_central_job", return_value=central_record),
            patch_live_validation_helper("_get_central_queue_job", return_value=queue_job),
            patch_live_validation_helper(
                "_wait_for_central_queue_appearance", return_value=terminal
            ) as appearance,
            patch_live_validation_helper(
                "_wait_for_central_queue_terminal",
                return_value=(terminal, [{"status": terminal.get("status"), "at": 1.0}]),
            ),
            patch_live_validation_helper("_read_central_job_item", return_value=item),
            patch_live_validation_helper(
                "_complete_job_lifecycle", return_value={"status": "succeeded", "deletion": {}}
            ) as complete,
        ):
            yield {"appearance": appearance, "complete": complete}

    @staticmethod
    def _bind_identity_on(ctx: Any) -> None:
        """Make the fake context bind worker identity the way RunContext does."""

        def bind(record: dict[str, Any], **identity: Any) -> bool:
            record.update(
                {
                    "central_queue_job_id": identity["job_id"],
                    "k8s_job_name": identity["name"],
                    "k8s_job_namespace": identity["namespace"],
                    "k8s_job_uid": identity["uid"],
                    "uid": identity["uid"],
                    "submission_state": "appeared",
                }
            )
            return True

        ctx.bind_central_job_identity.side_effect = bind

    def test_acknowledged_submission_without_a_queue_record_replays_without_begin(self) -> None:
        ctx = _context()
        self._bind_identity_on(ctx)
        queue_job, record, central_record = self._records(state="submitted")
        ctx.aws_client.make_authenticated_request.return_value = _response(200, {"job": queue_job})

        with self._helpers(
            record=record,
            central_record=central_record,
            queue_job=None,
            terminal=queue_job,
            item=queue_job,
        ) as helpers:
            result = actions_central_queue.action_central_queue_lifecycle(ctx)

        ctx.begin_job_submission.assert_not_called()
        ctx.finish_job_submission.assert_called_once()
        assert central_record["submission_state"] == "submitted"
        assert central_record["submission"] == {"job": queue_job}
        assert result["k8s_job_uid"] == "uid-central-1"
        assert result["workload_lifecycle"] == {"status": "succeeded", "deletion": {}}
        helpers["complete"].assert_called_once_with(ctx, record=record, marker=self._MARKER)
        assert central_record["cleanup_complete"] is True

    @pytest.mark.parametrize(
        ("response", "match"),
        [
            (_response(409, text="drift"), "request drift was detected"),
            (_response(503, text="unavailable"), "submission failed: 503 unavailable"),
            (_response(201, {"job": "not-an-object"}), "response omitted job"),
        ],
        ids=["409", "503", "no-job"],
    )
    def test_replay_responses_are_validated(self, response: MagicMock, match: str) -> None:
        ctx = _context()
        queue_job, record, central_record = self._records(state="prepared")
        ctx.aws_client.make_authenticated_request.return_value = response

        with (
            self._helpers(
                record=record,
                central_record=central_record,
                queue_job=None,
                terminal=queue_job,
                item=queue_job,
            ),
            pytest.raises(RuntimeError, match=match),
        ):
            actions_central_queue.action_central_queue_lifecycle(ctx)

        ctx.begin_job_submission.assert_called_once()
        ctx.finish_job_submission.assert_not_called()

    def test_changed_persisted_envelope_is_never_replayed(self) -> None:
        ctx = _context()
        queue_job, record, central_record = self._records(state="prepared")
        record["submission_envelope"]["body"]["priority"] = 1

        with (
            self._helpers(
                record=record,
                central_record=central_record,
                queue_job=None,
                terminal=queue_job,
                item=queue_job,
            ),
            pytest.raises(RuntimeError, match="replay envelope changed"),
        ):
            actions_central_queue.action_central_queue_lifecycle(ctx)

        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_absent_queue_record_in_a_non_replayable_state_fails(self) -> None:
        ctx = _context()
        queue_job, record, central_record = self._records(state="registered")

        with (
            self._helpers(
                record=record,
                central_record=central_record,
                queue_job=None,
                terminal=queue_job,
                item=queue_job,
            ),
            pytest.raises(RuntimeError, match="state 'registered' is not replayable"),
        ):
            actions_central_queue.action_central_queue_lifecycle(ctx)

    def test_malformed_checkpointed_submission_is_rejected(self) -> None:
        ctx = _context()
        queue_job, record, central_record = self._records(state="submitted", submission="bogus")

        with (
            self._helpers(
                record=record,
                central_record=central_record,
                queue_job=queue_job,
                terminal=queue_job,
                item=queue_job,
            ),
            pytest.raises(RuntimeError, match="central queue submission is malformed"),
        ):
            actions_central_queue.action_central_queue_lifecycle(ctx)

    def test_prepared_record_with_a_live_queue_job_is_reconciled_without_finishing(self) -> None:
        ctx = _context()
        queue_job, record, central_record = self._records(
            state="prepared", submission={"reconciled_existing_job": True}
        )

        with self._helpers(
            record=record,
            central_record=central_record,
            queue_job=queue_job,
            terminal=queue_job,
            item=queue_job,
        ):
            result = actions_central_queue.action_central_queue_lifecycle(ctx)

        ctx.finish_job_submission.assert_not_called()
        ctx.aws_client.make_authenticated_request.assert_not_called()
        assert central_record["submission_state"] == "reconciled"
        assert result["submission"] == {"reconciled_existing_job": True}

    def test_non_succeeded_queue_job_fails_with_its_error_message(self) -> None:
        ctx = _context()
        queue_job, record, central_record = self._records(state="submitted")
        failed = {**queue_job, "status": "failed", "error_message": "OOMKilled"}

        with (
            self._helpers(
                record=record,
                central_record=central_record,
                queue_job=queue_job,
                terminal=failed,
                item=queue_job,
            ),
            pytest.raises(RuntimeError, match="finished as failed: OOMKilled"),
        ):
            actions_central_queue.action_central_queue_lifecycle(ctx)

        assert central_record["status"] == "failed"

    def test_dynamodb_record_must_be_succeeded(self) -> None:
        ctx = _context()
        queue_job, record, central_record = self._records(state="submitted")

        with (
            self._helpers(
                record=record,
                central_record=central_record,
                queue_job=queue_job,
                terminal=queue_job,
                item={**queue_job, "status": "running"},
            ),
            pytest.raises(RuntimeError, match="is running, expected succeeded"),
        ):
            actions_central_queue.action_central_queue_lifecycle(ctx)

    def test_deleted_workload_requires_checkpointed_marker_evidence(self) -> None:
        ctx = _context()
        queue_job, record, central_record = self._records(
            state="submitted",
            deleted=True,
            bound=True,
            evidence={"marker": "GCO_LIVE_DDB_other"},
        )

        with (
            self._helpers(
                record=record,
                central_record=central_record,
                queue_job=queue_job,
                terminal=queue_job,
                item=queue_job,
            ) as helpers,
            pytest.raises(RuntimeError, match="deleted without checkpointed live-validation"),
        ):
            actions_central_queue.action_central_queue_lifecycle(ctx)

        helpers["complete"].assert_not_called()

    def test_deleted_workload_evidence_must_match_the_bound_identity(self) -> None:
        ctx = _context()
        queue_job, record, central_record = self._records(
            state="submitted", deleted=True, bound=True
        )
        record["validation_evidence"] = {
            "marker": self._MARKER,
            "name": queue_job["k8s_job_name"],
            "namespace": queue_job["k8s_job_namespace"],
            "uid": "uid-someone-else",
            "central_queue_job_id": self._job_id(),
        }

        with (
            self._helpers(
                record=record,
                central_record=central_record,
                queue_job=queue_job,
                terminal=queue_job,
                item=queue_job,
            ),
            pytest.raises(RuntimeError, match="does not match actual identity: uid"),
        ):
            actions_central_queue.action_central_queue_lifecycle(ctx)
