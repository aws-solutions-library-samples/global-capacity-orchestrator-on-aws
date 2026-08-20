"""Sequential two-lifecycle driver for the end-to-end EBS volume scenario.

``--volume-scenario both`` is a driver instruction, never a checkpoint
identity. This module expands it into two *fresh* runs that share nothing but
the operator's base run ID:

* ``<run-id>-volumes-retain-override`` proves ``destroy-all -y
  --retain-volumes`` keeps the recorded volumes; and
* ``<run-id>-volumes-delete`` proves ``destroy-all -y`` alone authorizes
  deleting the eligible ones.

Each case gets its own :class:`~.models.RunSettings`: its own private report
directory, its own checkpoint, its own resume identity, and its own complete
deploy/validate/destroy lifecycle through one :class:`~.runner
.LiveValidationRunner`. Isolation is proved *before* the first lifecycle
starts — a plan whose two cases would share a run ID, report directory,
checkpoint, or resume identity, or whose settings arrive pre-set to resume, is
refused with nothing deployed. That is what keeps one case from resuming or
mutating the other case's checkpoint.

The cases run strictly in order: retention evidence first, then implicit
authorized deletion. A case that does not finish cleanly stops the driver, so
a second live deployment never stacks on top of an unresolved teardown.
Holds no AWS state of its own; the runner owns every AWS interaction.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .models import RunSettings
from .runner import LiveValidationRunner
from .volume_scenario import (
    VOLUME_SCENARIO_BOTH,
    VolumeScenarioCase,
    expand_volume_scenario_selection,
    volume_scenario_run_id,
)

#: Builds one case's settings from its case and derived run ID. The CLI binds
#: this to its own argument parsing so the driver never re-reads ``argv``.
SettingsFactory = Callable[[VolumeScenarioCase, str], RunSettings]

#: Executes one complete lifecycle and returns its process exit code. Injected
#: so tests can drive the ordering contract without a live run.
LifecycleRunner = Callable[[RunSettings], int]

Logger = Callable[[str], None]

#: ``not-started`` is reserved for a case the driver refused to begin because
#: an earlier case did not complete.
LifecycleStatus = Literal["completed", "failed", "not-started"]

_NOT_STARTED_REASON = (
    "Not started: an earlier volume-scenario lifecycle did not complete, so this "
    "case was not deployed. Resolve that run, then rerun this case with "
    "--volume-scenario and its own --run-id."
)


@dataclass(frozen=True)
class ScenarioLifecycle:
    """One isolated deploy/validate/destroy lifecycle for one exact case."""

    case: VolumeScenarioCase
    settings: RunSettings

    @property
    def run_id(self) -> str:
        """The per-case run identity this lifecycle is fenced to."""
        return self.settings.run_id

    @property
    def report_dir(self) -> Path:
        """The private report directory this lifecycle owns exclusively."""
        return self.settings.report_dir

    @property
    def checkpoint_path(self) -> Path:
        """The checkpoint this lifecycle owns exclusively."""
        return self.settings.checkpoint_path


@dataclass(frozen=True)
class ScenarioLifecycleResult:
    """Durable, JSON-friendly record of what one lifecycle did."""

    case: VolumeScenarioCase
    run_id: str
    report_dir: Path
    checkpoint_path: Path
    status: LifecycleStatus
    exit_code: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the stable mapping used in driver output and evidence."""
        return {
            "case": self.case,
            "run_id": self.run_id,
            "report_dir": str(self.report_dir),
            "checkpoint_path": str(self.checkpoint_path),
            "status": self.status,
            "exit_code": self.exit_code,
            "error": self.error,
        }


def run_isolated_lifecycle(settings: RunSettings) -> int:
    """Run one case end to end through its own runner instance."""
    return LiveValidationRunner(settings).run()


def _validated_lifecycle(
    case: VolumeScenarioCase,
    run_id: str,
    settings: RunSettings,
) -> ScenarioLifecycle:
    """Bind one case to settings that are fenced to exactly that case."""
    if settings.run_id != run_id:
        raise ValueError(
            f"Volume scenario case {case!r} must run under its derived run ID "
            f"{run_id!r}, not {settings.run_id!r}"
        )
    if settings.volume_scenario_case != case:
        raise ValueError(
            f"Run {run_id!r} is fenced to volume scenario case "
            f"{settings.volume_scenario_case!r} but the driver planned {case!r}"
        )
    if settings.resume:
        raise ValueError(
            f"Volume scenario {VOLUME_SCENARIO_BOTH!r} runs two fresh lifecycles and "
            f"cannot resume; resume run {run_id!r} on its own with "
            f"--volume-scenario {case} --run-id {run_id}"
        )
    return ScenarioLifecycle(case=case, settings=settings)


def _assert_lifecycle_isolation(lifecycles: Sequence[ScenarioLifecycle]) -> None:
    """Refuse any plan whose lifecycles could observe each other's state."""
    for index, lifecycle in enumerate(lifecycles):
        for other in lifecycles[index + 1 :]:
            if lifecycle.case == other.case:
                raise ValueError(
                    f"Volume scenario case {lifecycle.case!r} was planned twice; each "
                    "case runs exactly one isolated lifecycle"
                )
            if lifecycle.run_id == other.run_id:
                raise ValueError(
                    f"Volume scenario cases {lifecycle.case!r} and {other.case!r} share "
                    f"run ID {lifecycle.run_id!r}; each case needs its own run identity"
                )
            if lifecycle.checkpoint_path == other.checkpoint_path:
                raise ValueError(
                    f"Volume scenario cases {lifecycle.case!r} and {other.case!r} share "
                    f"checkpoint {lifecycle.checkpoint_path}; neither case may resume or "
                    "mutate the other's checkpoint"
                )
            if _nested(lifecycle.report_dir, other.report_dir):
                raise ValueError(
                    f"Volume scenario cases {lifecycle.case!r} and {other.case!r} must use "
                    f"sibling private report directories, not {lifecycle.report_dir} and "
                    f"{other.report_dir}"
                )
            if lifecycle.settings.identity() == other.settings.identity():
                raise ValueError(
                    f"Volume scenario cases {lifecycle.case!r} and {other.case!r} resolve to "
                    "the same resume identity; each lifecycle must be separately fenced"
                )


def _nested(first: Path, second: Path) -> bool:
    """True when either directory contains the other."""
    return first.is_relative_to(second) or second.is_relative_to(first)


def plan_volume_scenario_lifecycles(
    selection: object,
    *,
    base_run_id: str,
    settings_factory: SettingsFactory,
) -> tuple[ScenarioLifecycle, ...]:
    """Expand one selection into isolated, ordered lifecycles without running them.

    Raises:
        ValueError: If the selection is not a multi-case driver instruction, the
            base run ID is empty, or the resulting lifecycles are not provably
            isolated from each other.
    """
    cases = expand_volume_scenario_selection(selection)
    if len(cases) < 2:
        raise ValueError(
            f"Volume scenario {selection!r} selects a single lifecycle and runs "
            f"directly; the driver expands only {VOLUME_SCENARIO_BOTH!r} into isolated "
            "lifecycles"
        )
    if not base_run_id:
        raise ValueError("The volume-scenario driver requires a non-empty base run ID")

    planned: list[ScenarioLifecycle] = []
    for case in cases:
        run_id = volume_scenario_run_id(base_run_id, case)
        planned.append(_validated_lifecycle(case, run_id, settings_factory(case, run_id)))
    lifecycles = tuple(planned)
    _assert_lifecycle_isolation(lifecycles)
    return lifecycles


def run_volume_scenario_lifecycles(
    lifecycles: Sequence[ScenarioLifecycle],
    *,
    run_lifecycle: LifecycleRunner | None = None,
    log: Logger = print,
) -> tuple[int, tuple[ScenarioLifecycleResult, ...]]:
    """Run planned lifecycles in order, stopping at the first unfinished case.

    Returns:
        The first non-zero lifecycle exit code (``0`` when every case
        completed) and one result record per planned case.
    """
    execute = run_isolated_lifecycle if run_lifecycle is None else run_lifecycle
    total = len(lifecycles)
    results: list[ScenarioLifecycleResult] = []
    exit_code = 0

    for index, lifecycle in enumerate(lifecycles, start=1):
        if exit_code:
            results.append(_result(lifecycle, "not-started", error=_NOT_STARTED_REASON))
            log(f"[scenario {index}/{total}] {lifecycle.case}: not started ({lifecycle.run_id})")
            continue

        log(
            f"[scenario {index}/{total}] {lifecycle.case}: starting isolated lifecycle "
            f"{lifecycle.run_id} in {lifecycle.report_dir}"
        )
        try:
            case_exit = execute(lifecycle.settings)
        except Exception as exc:
            exit_code = 1
            results.append(
                _result(lifecycle, "failed", exit_code=1, error=f"{type(exc).__name__}: {exc}")
            )
            log(f"[scenario {index}/{total}] {lifecycle.case}: failed to run: {exc}")
            continue

        if case_exit:
            exit_code = case_exit
            results.append(_result(lifecycle, "failed", exit_code=case_exit))
            log(f"[scenario {index}/{total}] {lifecycle.case}: exited {case_exit}")
        else:
            results.append(_result(lifecycle, "completed", exit_code=0))
            log(f"[scenario {index}/{total}] {lifecycle.case}: completed")

    for result in results:
        log(
            f"[scenario summary] {result.case}: {result.status} "
            f"(run {result.run_id}, reports {result.report_dir})"
        )
    return exit_code, tuple(results)


def _result(
    lifecycle: ScenarioLifecycle,
    status: LifecycleStatus,
    *,
    exit_code: int | None = None,
    error: str | None = None,
) -> ScenarioLifecycleResult:
    return ScenarioLifecycleResult(
        case=lifecycle.case,
        run_id=lifecycle.run_id,
        report_dir=lifecycle.report_dir,
        checkpoint_path=lifecycle.checkpoint_path,
        status=status,
        exit_code=exit_code,
        error=error,
    )


def run_volume_scenario_driver(
    selection: object,
    *,
    base_run_id: str,
    settings_factory: SettingsFactory,
    run_lifecycle: LifecycleRunner | None = None,
    log: Logger = print,
) -> int:
    """Plan and run every isolated lifecycle one selection expands into."""
    lifecycles = plan_volume_scenario_lifecycles(
        selection,
        base_run_id=base_run_id,
        settings_factory=settings_factory,
    )
    exit_code, _ = run_volume_scenario_lifecycles(
        lifecycles,
        run_lifecycle=run_lifecycle,
        log=log,
    )
    return exit_code
