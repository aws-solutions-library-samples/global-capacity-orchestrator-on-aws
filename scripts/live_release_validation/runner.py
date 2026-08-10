"""Dependency-aware runner with checkpointing and guaranteed cleanup."""

from __future__ import annotations

import json
import os
import signal
import time
import traceback
from pathlib import Path
from typing import Any, Literal

from cli.aws_client import GCOAWSClient
from cli.config import GCOConfig
from cli.jobs import JobManager
from cli.stacks import StackManager

from .actions import action_final_inventory, destroy_deployment
from .aws_session import ThrottleResilientSession
from .models import (
    ActionResult,
    RunCheckpoint,
    RunContext,
    RunSettings,
    ValidationReport,
    atomic_write_json,
    ensure_private_run_directory,
    utc_now,
)
from .registry import ActionDefinition, build_action_registry


class _LiveValidationSignal(BaseException):
    """Controlled interruption raised by SIGTERM/SIGHUP handlers."""

    def __init__(self, signum: int):
        self.signum = signum
        self.signal_name = signal.Signals(signum).name
        super().__init__(f"Received {self.signal_name}")


def require_local_execution() -> None:
    """Reject GitHub Actions before creating checkpoints or AWS clients.

    One verified exception: a run explicitly pointed at a local AWS emulator
    (see ``emulator.py``) may execute in CI. The emulator proof runs first
    and fails closed, so CI still cannot reach a real AWS account through
    this path — a real endpoint fails the URL rules, and real credentials
    fail the identity-echo probe.
    """
    if os.environ.get("GITHUB_ACTIONS", "").strip().casefold() != "true":
        return
    from .emulator import emulator_endpoint_requested, verify_emulator_endpoint

    endpoint = emulator_endpoint_requested()
    if endpoint is None:
        raise RuntimeError(
            "Live release validation is local-only and must not run in GitHub Actions"
        )
    verify_emulator_endpoint(endpoint)


class LiveValidationRunner:
    """Run selected live actions and always report and clean up."""

    def __init__(
        self,
        settings: RunSettings,
        registry: dict[str, ActionDefinition] | None = None,
    ):
        require_local_execution()
        ensure_private_run_directory(settings.report_dir, settings.checkpoint_path)
        self.settings = settings
        # A sibling harness (scripts/example_job_validation) reuses this runner
        # with its own action registry; the default remains the live release
        # validation registry.
        self.registry = registry if registry is not None else build_action_registry()
        self._deploy_dependent_actions = self._derive_deploy_dependent_actions(self.registry)
        self.selected_actions = self._resolve_actions(settings.requested_actions)
        self._previous_cwd = Path.cwd()
        os.chdir(settings.repo_root)
        try:
            self.cdk_context, self.deployment_regions = self._load_cdk_context(settings.repo_root)
            self.config = self._build_config(self.cdk_context, self.deployment_regions)
            self.checkpoint = self._load_checkpoint()
            self.report = ValidationReport(
                run_id=settings.run_id,
                identity=settings.identity(),
                selected_actions=list(self.selected_actions),
                started_at=self.checkpoint.created_at,
                action_results=list(self.checkpoint.action_results.values()),
                baseline=self.checkpoint.baseline,
            )
            # Adaptive throttle retries for every harness client: the
            # inventory scanners issue one metadata read per resource across
            # every enabled Region, and a Regional TPS squeeze must surface
            # as a bounded wait, not a failed action. See aws_session.py.
            self.session = ThrottleResilientSession()
            self.aws_client = GCOAWSClient(self.config)
            self.aws_client._session = self.session
            self.job_manager = JobManager(self.config)
            self.job_manager._aws_client = self.aws_client
            self.stack_manager = StackManager(self.config, project_root=settings.repo_root)
            extra_cdk_context = settings.extra_cdk_context()
            if extra_cdk_context:
                # Force-enable the requested off-by-default features for
                # every CDK invocation of this run (deploy, destroy, list all
                # synthesize the same graph) without touching cdk.json — the
                # preflight clean-worktree rule stays intact and the overrides
                # are part of the checkpoint identity.
                self.stack_manager.set_extra_cdk_context(extra_cdk_context)
            self.context = RunContext(
                settings=settings,
                checkpoint=self.checkpoint,
                report=self.report,
                cdk_context=self.cdk_context,
                deployment_regions=self.deployment_regions,
                config=self.config,
                session=self.session,
                stack_manager=self.stack_manager,
                aws_client=self.aws_client,
                job_manager=self.job_manager,
                persist_callback=self._persist_checkpoint,
            )
        except BaseException:
            os.chdir(self._previous_cwd)
            raise
        self._identity_verified = False
        self._received_signal: int | None = None
        self._previous_signal_handlers: dict[int, Any] = {}

    def _install_signal_handlers(self) -> None:
        """Route termination signals through the normal cleanup/report path."""
        for signal_name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, signal_name, None)
            if signum is None:
                continue
            self._previous_signal_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle_signal)

    def _restore_signal_handlers(self) -> None:
        for signum, previous in self._previous_signal_handlers.items():
            signal.signal(signum, previous)
        self._previous_signal_handlers.clear()

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        self._received_signal = signum
        raise _LiveValidationSignal(signum)

    @staticmethod
    def _derive_deploy_dependent_actions(
        registry: dict[str, ActionDefinition],
    ) -> frozenset[str]:
        """Actions that must not resume incomplete once teardown is recorded.

        Derived from the registry rather than hardcoded: ``deploy`` itself plus
        every action that transitively depends on it — except the teardown pair
        (``destroy``/``final-inventory``), which exist precisely to run against
        a destroyed deployment.
        """

        def depends_on_deploy(name: str, seen: frozenset[str] = frozenset()) -> bool:
            if name == "deploy":
                return True
            if name in seen:
                return False
            return any(
                depends_on_deploy(dep, seen | {name})
                for dep in registry[name].dependencies
                if dep in registry
            )

        return frozenset(
            name
            for name in registry
            if name not in {"destroy", "final-inventory"} and depends_on_deploy(name)
        )

    @staticmethod
    def _load_cdk_context(repo_root: Path) -> tuple[dict[str, Any], tuple[str, ...]]:
        path = repo_root / "cdk.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Unable to read {path}: {exc}") from exc
        context = data.get("context") if isinstance(data, dict) else None
        if not isinstance(context, dict):
            raise ValueError("cdk.json context must be an object")
        project_name = context.get("project_name")
        regions = context.get("deployment_regions")
        if not isinstance(project_name, str) or not project_name:
            raise ValueError("cdk.json context.project_name must be a non-empty string")
        if not isinstance(regions, dict):
            raise ValueError("cdk.json context.deployment_regions must be an object")
        for key in ("global", "api_gateway", "monitoring"):
            if not isinstance(regions.get(key), str) or not regions[key]:
                raise ValueError(f"cdk.json deployment_regions.{key} must be non-empty")
        regional = regions.get("regional")
        if (
            not isinstance(regional, list)
            or not regional
            or any(not isinstance(item, str) or not item for item in regional)
        ):
            raise ValueError("cdk.json deployment_regions.regional must be a non-empty list")
        if len(set(regional)) != len(regional):
            raise ValueError("cdk.json deployment_regions.regional contains duplicates")
        return context, tuple(regional)

    @staticmethod
    def _build_config(context: dict[str, Any], deployment_regions: tuple[str, ...]) -> GCOConfig:
        regions = context["deployment_regions"]
        return GCOConfig(
            project_name=context["project_name"],
            default_region=deployment_regions[0],
            api_gateway_region=regions["api_gateway"],
            global_region=regions["global"],
            monitoring_region=regions["monitoring"],
            default_namespace="gco-jobs",
            output_format="json",
            use_regional_api=False,
        )

    def _load_checkpoint(self) -> RunCheckpoint:
        path = self.settings.checkpoint_path
        if self.settings.resume:
            if not path.is_file():
                raise ValueError(f"--resume requires an existing checkpoint: {path}")
            checkpoint = RunCheckpoint.from_path(path)
            if checkpoint.identity != self.settings.identity():
                raise ValueError(
                    "Checkpoint identity does not match this invocation. "
                    "Account, SHA, branch, profile, actions, run ID, repository, and "
                    "protected stacks must remain exact."
                )
            return checkpoint
        if path.exists():
            raise ValueError(
                f"Checkpoint already exists: {path}. Use --resume with identical inputs "
                "or choose a new --run-id/report directory."
            )
        checkpoint = RunCheckpoint(identity=self.settings.identity())
        self._persist_checkpoint(checkpoint)
        return checkpoint

    def _persist_checkpoint(self, checkpoint: RunCheckpoint) -> None:
        atomic_write_json(self.settings.checkpoint_path, checkpoint.to_dict())

    def _resolve_actions(self, requested: tuple[str, ...]) -> tuple[str, ...]:
        if not requested or "all" in requested:
            if requested and len(requested) != 1:
                raise ValueError("'all' cannot be combined with individual action names")
            return tuple(self.registry)

        unknown = sorted(set(requested) - set(self.registry))
        if unknown:
            raise ValueError(
                f"Unknown actions: {', '.join(unknown)}. Available: " + ", ".join(self.registry)
            )

        selected: set[str] = set()

        def include(name: str) -> None:
            if name in selected:
                return
            for dependency in self.registry[name].dependencies:
                include(dependency)
            selected.add(name)

        for name in requested:
            include(name)
        return tuple(name for name in self.registry if name in selected)

    def _refresh_report_results(self) -> None:
        ordered_names = list(self.registry)
        self.report.action_results = [
            self.checkpoint.action_results[name]
            for name in ordered_names
            if name in self.checkpoint.action_results
        ]
        self.report.baseline = self.checkpoint.baseline
        final_result = self.checkpoint.action_results.get("final-inventory")
        if final_result is not None and final_result.status == "passed":
            self.report.final_inventory = final_result.details

    def _write_report(self) -> tuple[Path, Path]:
        self._refresh_report_results()
        return self.report.write(self.settings.report_dir)

    def _successful_status(self) -> Literal["passed", "partial"]:
        """Reserve passed for execution of the complete action registry."""
        if self.selected_actions == tuple(self.registry):
            return "passed"
        return "partial"

    def _execute_action(
        self,
        definition: ActionDefinition,
        *,
        always_run: bool = False,
    ) -> dict[str, Any]:
        if (
            not always_run
            and definition.name != "preflight"
            and definition.name in self.checkpoint.completed_actions
        ):
            result = self.checkpoint.action_results[definition.name]
            print(f"[skip] {definition.name}: checkpoint already passed")
            return result.details

        if (
            self.checkpoint.destroyed
            and definition.name in self._deploy_dependent_actions
            and definition.name not in self.checkpoint.completed_actions
        ):
            raise RuntimeError(
                f"Cannot resume incomplete action {definition.name!r}: the checkpoint "
                "already records infrastructure teardown"
            )

        print(f"[run] {definition.name}: {definition.description}")
        started_at = utc_now()
        started = time.monotonic()
        try:
            details = definition.handler(self.context)
        except BaseException as exc:
            result = ActionResult.failed(
                name=definition.name,
                description=definition.description,
                started_at=started_at,
                started_monotonic=started,
                ended_monotonic=time.monotonic(),
                error=exc,
            )
            self.checkpoint.action_results[definition.name] = result
            if definition.name in self.checkpoint.completed_actions:
                self.checkpoint.completed_actions.remove(definition.name)
            self._persist_checkpoint(self.checkpoint)
            self._write_report()
            print(f"[fail] {definition.name}: {result.error}")
            raise

        result = ActionResult.passed(
            name=definition.name,
            description=definition.description,
            started_at=started_at,
            started_monotonic=started,
            ended_monotonic=time.monotonic(),
            details=details,
        )
        self.checkpoint.action_results[definition.name] = result
        if definition.name not in self.checkpoint.completed_actions:
            self.checkpoint.completed_actions.append(definition.name)
        self._persist_checkpoint(self.checkpoint)
        self._write_report()
        print(f"[pass] {definition.name} ({result.duration_seconds:.3f}s)")
        return details

    def _guaranteed_cleanup(self) -> None:
        if not self.checkpoint.deployment_attempted:
            self.report.cleanup = {"needed": False}
            return
        if not self._identity_verified:
            self.report.cleanup = {
                "needed": True,
                "completed": False,
                "blocked": (
                    "Current invocation did not pass exact account/git preflight; "
                    "automatic cleanup was not allowed against an unverified identity"
                ),
            }
            return

        try:
            details = destroy_deployment(self.context)
            self.report.cleanup = {"completed": True, **details}
            definition = self.registry["destroy"]
            if "destroy" not in self.checkpoint.completed_actions:
                now = utc_now()
                result = ActionResult(
                    name="destroy",
                    description=definition.description,
                    status="passed",
                    started_at=now,
                    ended_at=now,
                    duration_seconds=0.0,
                    details=details,
                )
                self.checkpoint.action_results["destroy"] = result
                self.checkpoint.completed_actions.append("destroy")
                self._persist_checkpoint(self.checkpoint)
        except _LiveValidationSignal, KeyboardInterrupt:
            raise
        except BaseException as exc:
            self.report.cleanup = {
                "needed": True,
                "completed": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                "workload_cleanup_attempts": self.checkpoint.state.get(
                    "workload_cleanup_attempts", []
                ),
                "attempts": self.checkpoint.state.get("destroy_attempts", []),
                "retained_cleanup_attempts": self.checkpoint.state.get(
                    "retained_cleanup_attempts", []
                ),
            }

        if self.checkpoint.baseline is not None:
            try:
                details = action_final_inventory(self.context)
                definition = self.registry["final-inventory"]
                now = utc_now()
                result = ActionResult(
                    name="final-inventory",
                    description=definition.description,
                    status="passed",
                    started_at=now,
                    ended_at=now,
                    duration_seconds=0.0,
                    details=details,
                )
                self.checkpoint.action_results["final-inventory"] = result
                if "final-inventory" not in self.checkpoint.completed_actions:
                    self.checkpoint.completed_actions.append("final-inventory")
                self._persist_checkpoint(self.checkpoint)
            except _LiveValidationSignal, KeyboardInterrupt:
                raise
            except BaseException as exc:
                definition = self.registry["final-inventory"]
                now = utc_now()
                self.checkpoint.action_results["final-inventory"] = ActionResult(
                    name="final-inventory",
                    description=definition.description,
                    status="failed",
                    started_at=now,
                    ended_at=now,
                    duration_seconds=0.0,
                    error=f"{type(exc).__name__}: {exc}",
                    traceback="".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    ),
                )
                if "final-inventory" in self.checkpoint.completed_actions:
                    self.checkpoint.completed_actions.remove("final-inventory")
                self._persist_checkpoint(self.checkpoint)

    def run(self) -> int:
        """Execute selected actions, then report and clean up in all cases."""
        failure: BaseException | None = None
        interrupted = False
        interrupt_exit_code: int | None = None
        try:
            self._install_signal_handlers()
            try:
                for name in self.selected_actions:
                    definition = self.registry[name]
                    self._execute_action(definition)
                    if name == "preflight":
                        self._identity_verified = True
            except _LiveValidationSignal as exc:
                interrupted = True
                interrupt_exit_code = 128 + exc.signum
                failure = exc
                self.report.fatal_error = (
                    f"{exc.signal_name}: validation interrupted; controlled cleanup started"
                )
            except KeyboardInterrupt as exc:
                interrupted = True
                interrupt_exit_code = 130
                failure = exc
                self.report.fatal_error = "KeyboardInterrupt: validation interrupted"
            except BaseException as exc:
                failure = exc
                self.report.fatal_error = "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                )
            finally:
                try:
                    if self.checkpoint.deployment_attempted:
                        # Always reconcile exact target-stack absence on resume, even
                        # when an older checkpoint says teardown completed. This lets
                        # destroy_deployment reopen stale terminal state safely.
                        self._guaranteed_cleanup()
                    else:
                        self.report.cleanup = {"needed": False}
                except BaseException as cleanup_exc:
                    if isinstance(cleanup_exc, _LiveValidationSignal):
                        interrupted = True
                        interrupt_exit_code = 128 + cleanup_exc.signum
                        failure = cleanup_exc
                        self.report.fatal_error = (
                            f"{cleanup_exc.signal_name}: validation interrupted during cleanup"
                        )
                    elif isinstance(cleanup_exc, KeyboardInterrupt):
                        interrupted = True
                        interrupt_exit_code = 130
                        failure = cleanup_exc
                        self.report.fatal_error = "KeyboardInterrupt: cleanup interrupted"
                    self.report.cleanup = {
                        "needed": True,
                        "completed": False,
                        "runner_error": f"{type(cleanup_exc).__name__}: {cleanup_exc}",
                        "workload_cleanup_attempts": self.checkpoint.state.get(
                            "workload_cleanup_attempts", []
                        ),
                        "attempts": self.checkpoint.state.get("destroy_attempts", []),
                        "retained_cleanup_attempts": self.checkpoint.state.get(
                            "retained_cleanup_attempts", []
                        ),
                    }

                self._refresh_report_results()
                failed_results = [
                    result for result in self.report.action_results if result.status == "failed"
                ]
                cleanup_failed = bool(
                    self.checkpoint.deployment_attempted
                    and not self.report.cleanup.get("completed", False)
                )
                if interrupted:
                    self.report.status = "interrupted"
                elif failure is not None or failed_results or cleanup_failed:
                    self.report.status = "failed"
                else:
                    self.report.status = self._successful_status()
                self.report.ended_at = utc_now()
                json_path, markdown_path = self._write_report()
                print(f"JSON report: {json_path}")
                print(f"Markdown report: {markdown_path}")
        finally:
            self._restore_signal_handlers()
            os.chdir(self._previous_cwd)

        if interrupt_exit_code is not None:
            return interrupt_exit_code
        return 0 if self.report.status in {"passed", "partial"} else 1
