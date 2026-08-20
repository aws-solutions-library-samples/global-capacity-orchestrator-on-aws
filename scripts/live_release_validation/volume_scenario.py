"""Pure end-to-end EBS volume-scenario cases, selections, and run identities.

One place decides which volume-scenario values may exist, which of them a
checkpoint identity can be fenced to, and how the two-lifecycle driver names
each case's isolated run. Holds no AWS, checkpoint, or context state so
``models`` can validate ``RunSettings`` without importing a higher layer;
``ownership/volumes.py`` owns the durable, context-aware authorization built
on top of this contract.
"""

from __future__ import annotations

from typing import Literal, get_args

#: One exact checkpoint identity. ``both`` is deliberately absent: it selects
#: two isolated runs and is never an identity a checkpoint can be resumed
#: against.
VolumeScenarioCase = Literal["disabled", "retain-override", "delete"]

#: Every case a run may carry, in declaration order.
VOLUME_SCENARIO_CASES: tuple[VolumeScenarioCase, ...] = get_args(VolumeScenarioCase)

#: Top-level driver instruction. Expands into the two live cases; the scenario
#: driver, not a checkpoint, owns it.
VOLUME_SCENARIO_BOTH = "both"

#: Everything the operator may select on the command line.
VOLUME_SCENARIO_SELECTIONS: tuple[str, ...] = (*VOLUME_SCENARIO_CASES, VOLUME_SCENARIO_BOTH)

#: Cases that deploy and destroy real infrastructure, in the order the driver
#: runs them: retention evidence first, then implicit authorized deletion.
VOLUME_SCENARIO_LIVE_CASES: tuple[VolumeScenarioCase, ...] = ("retain-override", "delete")

#: Run-ID suffix each live case owns. Distinct suffixes keep report
#: directories, checkpoints, and deployment identities disjoint so neither
#: lifecycle can resume or mutate the other's checkpoint.
_VOLUME_SCENARIO_RUN_SUFFIXES: dict[VolumeScenarioCase, str] = {
    "retain-override": "volumes-retain-override",
    "delete": "volumes-delete",
}


def validated_volume_scenario_case(value: object) -> VolumeScenarioCase:
    """Return one exact case, rejecting driver instructions and unknown values."""
    if value == VOLUME_SCENARIO_BOTH:
        raise ValueError(
            "Volume scenario 'both' is a scenario-driver instruction, not a checkpoint "
            "identity; the driver runs one case per isolated lifecycle"
        )
    if value not in VOLUME_SCENARIO_CASES:
        raise ValueError(
            f"Unknown volume scenario case: {value!r}. Expected one of "
            + ", ".join(VOLUME_SCENARIO_CASES)
        )
    return value


def validated_volume_scenario_settings(
    case: object,
    *,
    confirm_fixture_cleanup: bool,
) -> VolumeScenarioCase:
    """Return the exact case, refusing fixture-cleanup authority without a scenario."""
    validated = validated_volume_scenario_case(case)
    if confirm_fixture_cleanup and validated == "disabled":
        raise ValueError(
            "Retained-fixture EBS cleanup authorization requires an enabled volume "
            "scenario case; it deletes only this run's exact recorded volumes"
        )
    return validated


def expand_volume_scenario_selection(selection: object) -> tuple[VolumeScenarioCase, ...]:
    """Map one operator selection to the case(s) that must run as separate runs."""
    if selection == VOLUME_SCENARIO_BOTH:
        return VOLUME_SCENARIO_LIVE_CASES
    return (validated_volume_scenario_case(selection),)


def volume_scenario_run_id(run_id: str, case: VolumeScenarioCase) -> str:
    """Return the per-case run identity for one isolated scenario lifecycle."""
    if not run_id:
        raise ValueError("A volume scenario lifecycle requires a non-empty base run ID")
    suffix = _VOLUME_SCENARIO_RUN_SUFFIXES.get(validated_volume_scenario_case(case))
    if suffix is None:
        raise ValueError(f"Volume scenario case {case!r} has no isolated lifecycle run identity")
    return f"{run_id}-{suffix}"
