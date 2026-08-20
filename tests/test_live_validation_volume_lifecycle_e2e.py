"""Both volume-scenario lifecycles end to end, offline, with isolated evidence.

Sibling modules already own the pieces in isolation: the driver's planning and
ordering (``test_live_validation_volume_scenario_driver.py``), the
command-equivalent request and the callback barrier
(``test_live_validation_volume_destroy_request.py``), the independent
observations, ineligible preservation, fixture cleanup, and residual accounting
(``test_live_validation_volume_post_destroy.py``), and the pre-destroy inventory
with its fencing (``test_live_validation_volume_inventory*.py``). This module
owns what only appears when the whole thing runs *together*:

* one ``--volume-scenario both`` driver run in which each case executes its real
  chain — pre-destroy inventory, strict target capture, command-equivalent
  request, published ``ebs-volumes`` callbacks from the real
  ``VolumeCleanupService``, the durable callback barrier, independent
  post-destroy observation, fixture cleanup, and final-inventory residual
  accounting — so the retain case ends with proof of retention and no bill and
  the delete case ends with its eligible volumes actually gone;
* cross-case evidence isolation: neither lifecycle's checkpoint, volume
  identities, nor observations are visible to or mutated by the other; and
* the two fences that only matter across a whole lifecycle — a first case that
  fails leaves the second case undeployed with no AWS request and its own
  checkpoint absent but resumable evidence for itself, and a run whose
  credential preflight never recorded (or recorded a foreign) caller identity
  fails closed at every stage of the chain without one EC2 request.

Only CloudFormation, EKS, kubectl, and git are stood in for; the volume policy,
discovery, deletion, evidence, and accounting code is the real thing running
against an in-memory EC2 double. Nothing here touches live infrastructure, and
no live validation run is ever launched.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from cli.volume_cleanup import (
    ClusterAbsenceProof,
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    VolumeCleanupRequest,
    VolumeCleanupService,
    VolumePolicy,
)
from scripts.live_release_validation import scenario_driver
from scripts.live_release_validation.actions import destroy as destroy_action
from scripts.live_release_validation.actions import final_inventory as final_inventory_action
from scripts.live_release_validation.actions import volume_inventory
from scripts.live_release_validation.checks import volume_outcomes
from scripts.live_release_validation.models import (
    RunCheckpoint,
    RunContext,
    RunSettings,
    ValidationReport,
    utc_now,
)
from scripts.live_release_validation.ownership import volume_requests, volume_targets
from scripts.live_release_validation.ownership import volumes as ownership_volumes
from scripts.live_release_validation.volume_scenario import (
    VOLUME_SCENARIO_BOTH,
    VolumeScenarioCase,
    volume_scenario_run_id,
)

_ACCOUNT = "123456789012"
_CALLER_ARN = f"arn:aws:iam::{_ACCOUNT}:role/live-validation"
_BRANCH = "chore/volumes"
_PROJECT = "gco-live"
_REGION = "us-east-1"
_BASE_RUN_ID = "run-e2e"

#: Volumes per case, named so a leak between lifecycles is unmistakable.
_CASE_VOLUME_IDS: dict[VolumeScenarioCase, tuple[str, ...]] = {
    "retain-override": ("vol-0aaaaaaaaaaaaaaa1", "vol-0aaaaaaaaaaaaaaa2"),
    "delete": ("vol-0dddddddddddddd01", "vol-0dddddddddddddd02"),
}

#: The request the retain case's command resolves to; used where a stage is
#: exercised on its own rather than through the destroy action.
_RETAIN_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.RETAIN,
    deletion_authorized=False,
    authorization_source=DeletionAuthorizationSource.NONE,
)


def _stack_name() -> str:
    return f"{_PROJECT}-{_REGION}"


def _stack_id() -> str:
    return f"arn:aws:cloudformation:{_REGION}:{_ACCOUNT}:stack/{_stack_name()}/abc"


def _cluster_tag_key() -> str:
    return f"kubernetes.io/cluster/{_stack_name()}"


def _target() -> RegionalVolumeTarget:
    return RegionalVolumeTarget(
        stack_name=_stack_name(),
        stack_id=_stack_id(),
        region=_REGION,
        cluster_name=_stack_name(),
        cluster_tag_key=_cluster_tag_key(),
    )


def _volume_dto(volume_id: str, *, size_gib: int = 50) -> dict[str, Any]:
    return {
        "VolumeId": volume_id,
        "AvailabilityZone": f"{_REGION}a",
        "Size": size_gib,
        "State": "available",
        "Tags": [{"Key": _cluster_tag_key(), "Value": "owned"}],
        "Attachments": [],
    }


def _not_found(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "InvalidVolume.NotFound", "Message": "absent"}},
        operation,
    )


class _FakeEc2:
    """In-memory EC2 for tag-key discovery, exact describes, and deletions."""

    def __init__(
        self,
        volumes: Mapping[str, dict[str, Any]],
        *,
        on_delete: Callable[[str], None] | None = None,
    ) -> None:
        self.volumes: dict[str, dict[str, Any] | None] = dict(volumes)
        self._on_delete = on_delete
        self.discovered: list[str] = []
        self.described: list[str] = []
        self.deleted: list[str] = []

    def get_paginator(self, operation: str) -> Any:
        assert operation == "describe_volumes"
        return SimpleNamespace(paginate=self._paginate)

    def _paginate(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        assert kwargs["Filters"] == [{"Name": "tag-key", "Values": [_cluster_tag_key()]}]
        self.discovered.append(_cluster_tag_key())
        present = [dto for dto in self.volumes.values() if dto is not None]
        yield {"Volumes": present}

    def describe_volumes(self, **kwargs: Any) -> dict[str, Any]:
        volume_ids = list(kwargs["VolumeIds"])
        assert len(volume_ids) == 1, "recorded volumes are described one exact ID at a time"
        volume_id = volume_ids[0]
        self.described.append(volume_id)
        dto = self.volumes.get(volume_id)
        if dto is None:
            raise _not_found("DescribeVolumes")
        return {"Volumes": [dto]}

    def delete_volume(self, **kwargs: Any) -> dict[str, Any]:
        volume_id = str(kwargs["VolumeId"])
        self.deleted.append(volume_id)
        if self._on_delete is not None:
            self._on_delete(volume_id)
        if self.volumes.get(volume_id) is None:
            raise _not_found("DeleteVolume")
        self.volumes[volume_id] = None
        return {}


class _FakeSession:
    """Session double handing out one EC2 double for the exact target Region."""

    def __init__(self, ec2: _FakeEc2) -> None:
        self._ec2 = ec2
        self.requested: list[tuple[str, str | None]] = []

    def client(self, service_name: str, region_name: str | None = None) -> _FakeEc2:
        self.requested.append((service_name, region_name))
        assert service_name == "ec2", "the volume scenario only reads and deletes EBS"
        assert region_name == _REGION
        return self._ec2


def _settings(
    report_root: Path,
    *,
    case: VolumeScenarioCase,
    run_id: str,
    **overrides: Any,
) -> RunSettings:
    """One case's fenced settings, in its own sibling private report directory."""
    report_dir = report_root / run_id
    base = RunSettings(
        run_id=run_id,
        repo_root=report_root,
        report_dir=report_dir,
        checkpoint_path=report_dir / "checkpoint.json",
        expected_account=_ACCOUNT,
        expected_sha="a" * 40,
        expected_branch=_BRANCH,
        profile="configured",
        requested_actions=("all",),
        destroy_attempts=1,
        destroy_retry_delay_seconds=0,
        volume_scenario_case=case,
        confirm_ebs_fixture_cleanup=case == "retain-override",
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _runner_for(settings: RunSettings) -> Any:
    """A runner with nothing but settings, for its real owner-only checkpoint I/O."""
    from scripts.live_release_validation.runner import LiveValidationRunner

    instance = object.__new__(LiveValidationRunner)
    instance.settings = settings
    return instance


def _context(
    settings: RunSettings,
    session: Any,
    *,
    persist: Callable[[RunCheckpoint], None] | None = None,
    caller_arn: str | None = _CALLER_ARN,
) -> RunContext:
    checkpoint = RunCheckpoint(identity=settings.identity())
    checkpoint.deployment_attempted = True
    checkpoint.baseline = {"ecr_regions": [_REGION]}
    checkpoint.state.update(
        {
            "enabled_regions": [_REGION],
            "owned_stacks": {_REGION: {_stack_name(): {"stack_id": _stack_id()}}},
            "target_stack_regions": {_stack_name(): _REGION},
            "bootstrap_stacks": {},
        }
    )
    if caller_arn is not None:
        checkpoint.state["account_arn"] = caller_arn
    report = ValidationReport(
        run_id=settings.run_id,
        identity=settings.identity(),
        selected_actions=list(settings.requested_actions),
        started_at="2026-07-17T00:00:00+00:00",
    )
    return RunContext(
        settings=settings,
        checkpoint=checkpoint,
        report=report,
        # Observability sizing has its own module; keep this chain on evidence.
        cdk_context={"cluster_observability": {"enabled": False}},
        deployment_regions=(_REGION,),
        config=SimpleNamespace(project_name=_PROJECT, global_region=_REGION),
        session=session,
        stack_manager=MagicMock(),
        aws_client=MagicMock(),
        job_manager=MagicMock(),
        persist_callback=persist or MagicMock(),
    )


@contextmanager
def _cluster(volume_ids: Sequence[str]) -> Iterator[Callable[..., Any]]:
    """A kubectl double whose live PVCs are bound to exactly these volumes."""
    pvcs = [
        {
            "metadata": {
                "namespace": "gco-monitoring",
                "name": f"data-{volume_id}",
                "uid": f"pvc-uid-{volume_id}",
            },
            "spec": {
                "storageClassName": "gp3",
                "volumeName": f"pv-{volume_id}",
                "resources": {"requests": {"storage": "50Gi"}},
            },
            "status": {"phase": "Bound"},
        }
        for volume_id in volume_ids
    ]
    pvs = [
        {
            "metadata": {"name": f"pv-{volume_id}", "uid": f"pv-uid-{volume_id}"},
            "spec": {"csi": {"driver": "ebs.csi.aws.com", "volumeHandle": volume_id}},
        }
        for volume_id in volume_ids
    ]

    def runner(*args: str, timeout: int = 120) -> tuple[int, str, str]:
        items = pvcs if args[1] == "persistentvolumeclaims" else pvs
        return 0, json.dumps({"items": items}), ""

    yield runner


def _orchestrator(
    ctx: RunContext,
    *,
    publish: bool = True,
) -> Callable[..., tuple[bool, list[str], list[str]]]:
    """Stand in for CloudFormation/EKS, running the real cleanup service per target."""

    def destroy_orchestrated(**kwargs: Any) -> tuple[bool, list[str], list[str]]:
        request = kwargs["volume_cleanup_request"]
        assert isinstance(request, VolumeCleanupRequest)
        capture = ctx.checkpoint.state["volume_scenario"][volume_targets.STRICT_DESTROY_TARGETS_KEY]
        targets = volume_targets.strict_volume_cleanup_targets(capture)
        service = VolumeCleanupService(
            lambda service_name, *, region_name: ctx.session.client(
                service_name,
                region_name=region_name,
            )
        )
        for stack_name in sorted(targets):
            target = targets[stack_name]
            outcome = service.cleanup(
                target=target,
                absence=ClusterAbsenceProof(
                    stack_name=target.stack_name,
                    region=target.region,
                    cluster_name=target.cluster_name,
                    verified_at=utc_now(),
                ),
                request=request,
            )
            if publish:
                kwargs["on_cleanup_complete"](
                    volume_requests.VOLUME_CLEANUP_CALLBACK_NAME,
                    dict(outcome.to_dict()),
                )
        return True, sorted(targets), []

    return destroy_orchestrated


_PRESENT = {"all_absent": False, "residual": [{"stack_name": _stack_name()}], "absent": []}
_ABSENT = {"all_absent": True, "residual": [], "absent": []}


def _run_inventory(ctx: RunContext, volume_ids: Sequence[str]) -> dict[str, Any]:
    """Run the real pre-destroy inventory action against the doubles."""
    with (
        patch.object(volume_inventory, "cluster_kubectl", lambda *_: _cluster(volume_ids)),
        patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH),
        patch.object(ownership_volumes, "_authorize_owned_stack"),
    ):
        return volume_inventory.action_volume_inventory(ctx)


def _run_destroy(ctx: RunContext, *, publish: bool = True) -> dict[str, Any]:
    """Run the real destroy action with only its non-volume helpers stood in for."""
    ctx.stack_manager.destroy_orchestrated.side_effect = _orchestrator(ctx, publish=publish)
    with (
        patch.object(destroy_action, "cleanup_workloads", return_value={"complete": True}),
        patch.object(destroy_action, "_reconcile_stack_ownership"),
        patch.object(destroy_action, "_checkpoint_new_ecr_repositories"),
        patch.object(destroy_action, "_checkpoint_new_ecr_images"),
        patch.object(destroy_action, "_checkpoint_retained_kms_keys"),
        patch.object(destroy_action, "_ensure_log_cleanup_helper", return_value={}),
        patch.object(destroy_action, "_delete_log_cleanup_helper", return_value={}),
        patch.object(destroy_action, "_prepared_change_set_authority", return_value={}),
        patch.object(destroy_action, "_retained_resource_cleanup", return_value={}),
        patch.object(
            destroy_action,
            "_verify_target_stack_absence",
            side_effect=[_PRESENT, _ABSENT, _ABSENT],
        ),
        patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH),
        patch.object(ownership_volumes, "_authorize_owned_stack"),
    ):
        return destroy_action.destroy_deployment(ctx)


def _run_final_inventory(ctx: RunContext) -> dict[str, Any]:
    """Run the real final-inventory action with only its EBS accounting live."""
    with (
        patch.object(final_inventory_action, "_verify_target_stack_absence", return_value=_ABSENT),
        patch.object(final_inventory_action, "capture_baseline", return_value={}),
        patch.object(final_inventory_action, "_strip_expected_retained_ecr", return_value=({}, [])),
        patch.object(final_inventory_action, "compare_baseline", return_value=[]),
        patch.object(final_inventory_action, "collect_project_resources", return_value={}),
        patch.object(final_inventory_action, "_strip_baseline_ecr", return_value={}),
        patch.object(final_inventory_action, "_strip_accepted_retained_ecr", return_value={}),
        patch.object(final_inventory_action, "_strip_expected_pending_kms", return_value=({}, [])),
        patch.object(
            final_inventory_action, "summarize_project_resources", return_value={"stacks": 0}
        ),
        patch.object(final_inventory_action, "project_resources_are_absent", return_value=True),
        patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH),
    ):
        return final_inventory_action.action_final_inventory(ctx)


class _Lifecycles:
    """Runs one real evidence chain per case and keeps every boundary it crossed."""

    def __init__(self, report_root: Path, *, publish: bool = True) -> None:
        self.report_root = report_root
        self.publish = publish
        self.ec2: dict[VolumeScenarioCase, _FakeEc2] = {}
        self.sessions: dict[VolumeScenarioCase, _FakeSession] = {}
        self.results: dict[VolumeScenarioCase, dict[str, Any]] = {}
        self.checkpoint_text: dict[VolumeScenarioCase, str] = {}
        #: The persisted post-destroy observation status at the moment each
        #: deletion was requested, per case. Proves fixture deletion follows
        #: durable retention evidence and command deletion precedes it.
        self.observation_status_at_delete: list[tuple[VolumeScenarioCase, str | None]] = []

    def settings_factory(self, case: VolumeScenarioCase, run_id: str) -> RunSettings:
        return _settings(self.report_root, case=case, run_id=run_id)

    def _persisted_observation_status(self, settings: RunSettings) -> str | None:
        checkpoint = RunCheckpoint.from_path(settings.checkpoint_path)
        scenario = checkpoint.state.get("volume_scenario") or {}
        observations = scenario.get(volume_outcomes.POST_DESTROY_OBSERVATIONS_KEY)
        return observations.get("status") if isinstance(observations, Mapping) else None

    def run(self, settings: RunSettings) -> int:
        case = settings.volume_scenario_case
        volume_ids = _CASE_VOLUME_IDS[case]
        ec2 = _FakeEc2(
            {volume_id: _volume_dto(volume_id) for volume_id in volume_ids},
            on_delete=lambda _: self.observation_status_at_delete.append(
                (case, self._persisted_observation_status(settings))
            ),
        )
        session = _FakeSession(ec2)
        self.ec2[case] = ec2
        self.sessions[case] = session

        runner = _runner_for(settings)
        ctx = _context(settings, session, persist=runner._persist_checkpoint)
        runner._persist_checkpoint(ctx.checkpoint)
        try:
            inventory = _run_inventory(ctx, volume_ids)
            destroyed = _run_destroy(ctx, publish=self.publish)
            final = _run_final_inventory(ctx)
        except Exception as exc:
            self.results[case] = {"error": f"{type(exc).__name__}: {exc}"}
            self.checkpoint_text[case] = settings.checkpoint_path.read_text(encoding="utf-8")
            return 1
        self.results[case] = {
            "inventory": inventory,
            "destroy": destroyed,
            "final_inventory": final,
        }
        self.checkpoint_text[case] = settings.checkpoint_path.read_text(encoding="utf-8")
        return 0


def _scenario_state(settings: RunSettings) -> dict[str, Any]:
    """Read one case's persisted scenario evidence back off disk."""
    checkpoint = RunCheckpoint.from_path(settings.checkpoint_path)
    state = checkpoint.state["volume_scenario"]
    assert isinstance(state, dict)
    return state


class TestBothLifecyclesProduceTheirOwnEvidenceChain:
    """One driver run, two isolated lifecycles, two complete evidence chains."""

    @staticmethod
    def _drive(report_root: Path, *, publish: bool = True) -> tuple[int, _Lifecycles]:
        lifecycles = _Lifecycles(report_root, publish=publish)
        exit_code = scenario_driver.run_volume_scenario_driver(
            VOLUME_SCENARIO_BOTH,
            base_run_id=_BASE_RUN_ID,
            settings_factory=lifecycles.settings_factory,
            run_lifecycle=lifecycles.run,
            log=lambda _: None,
        )
        # Surface a broken chain here rather than in one downstream assertion.
        assert [result.get("error") for result in lifecycles.results.values()] == [None, None]
        return exit_code, lifecycles

    @staticmethod
    def _settings_for(report_root: Path, case: VolumeScenarioCase) -> RunSettings:
        return _settings(
            report_root,
            case=case,
            run_id=volume_scenario_run_id(_BASE_RUN_ID, case),
        )

    def test_both_cases_complete_with_their_own_durable_chain(self, tmp_path: Path) -> None:
        exit_code, lifecycles = self._drive(tmp_path)

        assert exit_code == 0
        assert sorted(lifecycles.results) == ["delete", "retain-override"]
        for case in ("retain-override", "delete"):
            settings = self._settings_for(tmp_path, case)
            state = _scenario_state(settings)
            assert state["case"] == case
            # Pre-inventory, targets, request, callbacks, and observations are all
            # durable evidence of one teardown before it was marked complete.
            assert state[ownership_volumes.PRE_DESTROY_INVENTORY_KEY]["status"] == "recorded"
            assert state[volume_targets.STRICT_DESTROY_TARGETS_KEY]["status"] == "captured"
            assert state[volume_requests.STRICT_DESTROY_REQUEST_KEY]["status"] == "resolved"
            evidence = state[volume_requests.STRICT_DESTROY_CLEANUP_EVIDENCE_KEY]
            assert evidence["status"] == "recorded"
            assert sorted(evidence["targets"]) == [_stack_name()]
            assert state[volume_outcomes.POST_DESTROY_OBSERVATIONS_KEY]["status"] == "verified"
            assert RunCheckpoint.from_path(settings.checkpoint_path).destroyed is True
            # Only the one exact regional target ever reached the cleanup helper.
            destroyed = lifecycles.results[case]["destroy"]
            assert destroyed["volume_cleanup_targets"] == [_stack_name()]
            final = lifecycles.results[case]["final_inventory"]
            assert final["ebs_volume_residuals"]["status"] == "clear"
            assert final["summary"]["ebs_fixture_volumes"] == 0

    def test_each_case_exercises_the_command_its_case_stands_for(self, tmp_path: Path) -> None:
        self._drive(tmp_path)

        retain = _scenario_state(self._settings_for(tmp_path, "retain-override"))
        delete = _scenario_state(self._settings_for(tmp_path, "delete"))

        retain_request = retain[volume_requests.STRICT_DESTROY_REQUEST_KEY]
        assert retain_request["command_line"] == "gco stacks destroy-all -y --retain-volumes"
        assert retain_request["policy"] == "retain"
        assert retain_request["deletion_authorized"] is False

        delete_request = delete[volume_requests.STRICT_DESTROY_REQUEST_KEY]
        assert delete_request["command_line"] == "gco stacks destroy-all -y"
        assert delete_request["policy"] == "delete"
        assert delete_request["deletion_authorized"] is True
        assert delete_request["authorization_source"] == "destroy-all-with-yes"
        # The implicit-delete lifecycle needed neither the flag nor a prompt.
        assert delete_request["delete_flag_supplied"] is False
        assert delete_request["volume_confirmation_required"] is False

    def test_the_retain_case_keeps_its_volumes_then_removes_its_own_fixtures(
        self, tmp_path: Path
    ) -> None:
        _, lifecycles = self._drive(tmp_path)

        settings = self._settings_for(tmp_path, "retain-override")
        state = _scenario_state(settings)
        volume_ids = list(_CASE_VOLUME_IDS["retain-override"])
        observed = state[volume_outcomes.POST_DESTROY_OBSERVATIONS_KEY]["targets"][_stack_name()]
        assert [volume["expected_presence"] for volume in observed["volumes"]] == ["present"] * 2
        assert [volume["published_action"] for volume in observed["volumes"]] == ["retained"] * 2
        # Fixture cleanup is the harness's own teardown, after that evidence is
        # durable — never part of the retain outcome the run just measured.
        cleanup = state[ownership_volumes.FIXTURE_CLEANUP_KEY]
        assert cleanup["status"] == "cleaned"
        assert cleanup["targets"][_stack_name()]["candidate_volume_ids"] == volume_ids
        assert lifecycles.ec2["retain-override"].deleted == volume_ids
        assert [
            status
            for case, status in lifecycles.observation_status_at_delete
            if case == "retain-override"
        ] == ["verified", "verified"]

    def test_the_delete_case_disposes_of_its_volumes_and_cleans_no_fixture(
        self, tmp_path: Path
    ) -> None:
        _, lifecycles = self._drive(tmp_path)

        settings = self._settings_for(tmp_path, "delete")
        state = _scenario_state(settings)
        volume_ids = list(_CASE_VOLUME_IDS["delete"])
        observed = state[volume_outcomes.POST_DESTROY_OBSERVATIONS_KEY]["targets"][_stack_name()]
        assert [volume["expected_presence"] for volume in observed["volumes"]] == ["absent"] * 2
        assert [volume["published_action"] for volume in observed["volumes"]] == [
            "delete-requested"
        ] * 2
        # Every deletion came from the command under test, during the cleanup
        # barrier and therefore before any independent observation existed; the
        # fixture teardown belongs to the retain case alone.
        assert lifecycles.ec2["delete"].deleted == volume_ids
        assert [
            status for case, status in lifecycles.observation_status_at_delete if case == "delete"
        ] == [None, None]
        assert state[ownership_volumes.FIXTURE_CLEANUP_KEY]["status"] == "skipped"

    def test_neither_lifecycle_can_see_or_change_the_others_evidence(self, tmp_path: Path) -> None:
        _, lifecycles = self._drive(tmp_path)

        retain_settings = self._settings_for(tmp_path, "retain-override")
        delete_settings = self._settings_for(tmp_path, "delete")
        assert retain_settings.checkpoint_path != delete_settings.checkpoint_path
        assert retain_settings.identity() != delete_settings.identity()

        retain_text = retain_settings.checkpoint_path.read_text(encoding="utf-8")
        delete_text = delete_settings.checkpoint_path.read_text(encoding="utf-8")
        # The second lifecycle left the first case's checkpoint byte-identical to
        # what that case itself last persisted.
        assert retain_text == lifecycles.checkpoint_text["retain-override"]
        for volume_id in _CASE_VOLUME_IDS["delete"]:
            assert volume_id not in retain_text
        for volume_id in _CASE_VOLUME_IDS["retain-override"]:
            assert volume_id not in delete_text
        # Each case observed, deleted, and reported only its own identities.
        pairs: tuple[tuple[VolumeScenarioCase, VolumeScenarioCase], ...] = (
            ("retain-override", "delete"),
            ("delete", "retain-override"),
        )
        for case, other in pairs:
            ec2 = lifecycles.ec2[case]
            assert set(ec2.described) == set(_CASE_VOLUME_IDS[case])
            assert set(ec2.deleted) <= set(_CASE_VOLUME_IDS[case])
            assert not set(ec2.described) & set(_CASE_VOLUME_IDS[other])

    def test_each_case_owns_a_private_report_directory_with_only_its_checkpoint(
        self, tmp_path: Path
    ) -> None:
        self._drive(tmp_path)

        directories = sorted(path.name for path in tmp_path.iterdir() if path.is_dir())
        assert directories == [
            f"{_BASE_RUN_ID}-volumes-delete",
            f"{_BASE_RUN_ID}-volumes-retain-override",
        ]
        for case in ("retain-override", "delete"):
            settings = self._settings_for(tmp_path, case)
            assert [path.name for path in settings.report_dir.iterdir()] == ["checkpoint.json"]


class TestAFailedLifecycleStopsTheNextOne:
    """A case that does not finish leaves the next case undeployed."""

    @staticmethod
    def _settings_for(report_root: Path, case: VolumeScenarioCase) -> RunSettings:
        return _settings(
            report_root,
            case=case,
            run_id=volume_scenario_run_id(_BASE_RUN_ID, case),
        )

    def test_the_second_case_is_never_deployed_and_reaches_no_aws(self, tmp_path: Path) -> None:
        # The orchestrator publishes no `ebs-volumes` callback, so the retain
        # lifecycle cannot prove its evidence is durable and fails.
        lifecycles = _Lifecycles(tmp_path, publish=False)

        exit_code, results = scenario_driver.run_volume_scenario_lifecycles(
            scenario_driver.plan_volume_scenario_lifecycles(
                VOLUME_SCENARIO_BOTH,
                base_run_id=_BASE_RUN_ID,
                settings_factory=lifecycles.settings_factory,
            ),
            run_lifecycle=lifecycles.run,
            log=lambda _: None,
        )

        assert exit_code == 1
        assert [(result.case, result.status) for result in results] == [
            ("retain-override", "failed"),
            ("delete", "not-started"),
        ]
        assert "delete" not in lifecycles.sessions
        assert not self._settings_for(tmp_path, "delete").report_dir.exists()

    def test_the_failed_case_keeps_resumable_blocked_evidence_of_its_own(
        self, tmp_path: Path
    ) -> None:
        lifecycles = _Lifecycles(tmp_path, publish=False)
        scenario_driver.run_volume_scenario_driver(
            VOLUME_SCENARIO_BOTH,
            base_run_id=_BASE_RUN_ID,
            settings_factory=lifecycles.settings_factory,
            run_lifecycle=lifecycles.run,
            log=lambda _: None,
        )

        settings = self._settings_for(tmp_path, "retain-override")
        resumed = _runner_for(dataclasses.replace(settings, resume=True))._load_checkpoint()
        assert resumed.identity == settings.identity()
        assert resumed.destroyed is False
        state = resumed.state["volume_scenario"]
        # The evidence recorded before the failure survives, and the barrier that
        # stopped teardown says exactly which outcome was missing.
        assert state[ownership_volumes.PRE_DESTROY_INVENTORY_KEY]["status"] == "recorded"
        evidence = state[volume_requests.STRICT_DESTROY_CLEANUP_EVIDENCE_KEY]
        assert evidence["status"] == "blocked"
        assert evidence["problems"] == [f"no persisted ebs-volumes outcome for: {_stack_name()}"]
        assert volume_outcomes.POST_DESTROY_OBSERVATIONS_KEY not in state
        assert lifecycles.ec2["retain-override"].deleted == []


class TestCredentialPreflightFencesTheWholeChain:
    """Without a proven caller identity, no stage of the chain reaches EC2."""

    @staticmethod
    def _recorded_inventory(volume_ids: Sequence[str]) -> dict[str, Any]:
        """The pre-destroy evidence the later stages would otherwise consume."""
        return {
            "status": "recorded",
            "case": "retain-override",
            "regions": {
                _REGION: {
                    "region": _REGION,
                    "stack_name": _stack_name(),
                    "stack_id": _stack_id(),
                    "cluster_name": _stack_name(),
                    "cluster_tag_key": _cluster_tag_key(),
                    "result": "recorded",
                    "volume_ids": list(volume_ids),
                    "volumes": [
                        {
                            "volume_id": volume_id,
                            "region": _REGION,
                            "availability_zone": f"{_REGION}a",
                            "size_gib": 50,
                            "state": "available",
                            "cluster_tag_key": _cluster_tag_key(),
                            "cluster_tag_value": "owned",
                            "attachment_ids": [],
                            "observed": True,
                        }
                        for volume_id in volume_ids
                    ],
                }
            },
        }

    def _context(self, tmp_path: Path, *, caller_arn: str | None) -> tuple[RunContext, _FakeEc2]:
        volume_ids = _CASE_VOLUME_IDS["retain-override"]
        ec2 = _FakeEc2({volume_id: _volume_dto(volume_id) for volume_id in volume_ids})
        settings = _settings(tmp_path, case="retain-override", run_id="run-preflight")
        ctx = _context(settings, _FakeSession(ec2), caller_arn=caller_arn)
        ctx.checkpoint.state["volume_scenario"] = {
            "case": "retain-override",
            ownership_volumes.PRE_DESTROY_INVENTORY_KEY: self._recorded_inventory(volume_ids),
        }
        return ctx, ec2

    @pytest.mark.parametrize(
        ("caller_arn", "expected"),
        [
            (None, "requires a checkpointed caller identity"),
            ("arn:aws:iam::210987654321:role/other", "does not match expected account"),
        ],
        ids=["preflight-never-ran", "foreign-account"],
    )
    @pytest.mark.parametrize(
        "stage",
        ["inventory", "target-capture", "request", "observation"],
    )
    def test_every_stage_fails_closed_without_one_ec2_request(
        self,
        tmp_path: Path,
        caller_arn: str | None,
        expected: str,
        stage: str,
    ) -> None:
        ctx, ec2 = self._context(tmp_path, caller_arn=caller_arn)

        with (
            patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH),
            patch.object(ownership_volumes, "_authorize_owned_stack"),
            patch.object(
                volume_inventory,
                "cluster_kubectl",
                lambda *_: _cluster(_CASE_VOLUME_IDS["retain-override"]),
            ),
            pytest.raises(RuntimeError, match=expected),
        ):
            if stage == "inventory":
                volume_inventory.action_volume_inventory(ctx)
            elif stage == "target-capture":
                volume_targets.capture_strict_volume_targets(ctx)
            elif stage == "request":
                volume_requests.resolve_strict_volume_cleanup_request(ctx)
            else:
                volume_outcomes.verify_post_destroy_volume_outcomes(
                    ctx,
                    request=_RETAIN_REQUEST,
                    targets={_stack_name(): _target()},
                    destroy_sequence=1,
                )

        assert ctx.session.requested == []
        assert ec2.described == []
        assert ec2.deleted == []

    def test_the_blocked_target_capture_records_why_before_it_raises(self, tmp_path: Path) -> None:
        ctx, _ = self._context(tmp_path, caller_arn=None)

        with (
            patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH),
            patch.object(ownership_volumes, "_authorize_owned_stack"),
            pytest.raises(RuntimeError, match="cannot establish an exact volume target identity"),
        ):
            volume_targets.capture_strict_volume_targets(ctx)

        capture = ctx.checkpoint.state["volume_scenario"][volume_targets.STRICT_DESTROY_TARGETS_KEY]
        assert capture["status"] == "blocked"
        entry = capture["targets"][_stack_name()]
        assert entry["complete"] is False
        assert entry["reason_code"] == "strict-stack-authorization-failed"
        assert "checkpointed caller identity" in entry["reason"]


def _attached_dto(
    volume_id: str,
    *,
    state: str = "in-use",
    instance_id: str = "i-0abcdef0123456789",
    size_gib: int = 50,
) -> dict[str, Any]:
    """One owned volume that is ineligible for deletion because it is attached."""
    dto = _volume_dto(volume_id, size_gib=size_gib)
    dto["State"] = state
    dto["Attachments"] = [{"VolumeId": volume_id, "InstanceId": instance_id, "State": "attached"}]
    return dto


def _unavailable_dto(
    volume_id: str,
    *,
    state: str = "error",
    size_gib: int = 50,
) -> dict[str, Any]:
    """One owned, detached volume that is ineligible because it is not ``available``."""
    dto = _volume_dto(volume_id, size_gib=size_gib)
    dto["State"] = state
    return dto


def _prepared_context(
    report_root: Path,
    *,
    case: VolumeScenarioCase,
    run_id: str,
    ec2: _FakeEc2,
    **setting_overrides: Any,
) -> RunContext:
    """Persist one case's fresh fenced checkpoint and return its ready context.

    This mirrors what ``_Lifecycles.run`` does for a single case, but hands the
    test its own EC2 double and setting overrides so a recorded volume can be
    ineligible, a fixture cleanup can be left unauthorized, and the whole chain
    can be driven against exactly those facts.
    """
    settings = _settings(report_root, case=case, run_id=run_id, **setting_overrides)
    runner = _runner_for(settings)
    ctx = _context(settings, _FakeSession(ec2), persist=runner._persist_checkpoint)
    runner._persist_checkpoint(ctx.checkpoint)
    return ctx


def _observed_volumes(ctx: RunContext) -> dict[str, dict[str, Any]]:
    """Read one case's persisted post-destroy observation, keyed by volume ID."""
    observations = _scenario_state(ctx.settings)[volume_outcomes.POST_DESTROY_OBSERVATIONS_KEY]
    assert observations["status"] == "verified"
    entry = observations["targets"][_stack_name()]
    return {volume["volume_id"]: volume for volume in entry["volumes"]}


class TestIneligibleVolumesArePreservedThroughTheWholeChain:
    """An attached or non-``available`` recorded volume survives both policies.

    The isolated ``test_live_validation_volume_post_destroy.py`` module already
    proves the post-destroy check preserves ineligible volumes from hand-built
    evidence. What only appears end to end — and is pinned here — is that a
    volume the pre-destroy inventory recorded as attached or non-``available``
    stays that way through the *real* discovery, classification, deletion, and
    independent-observation chain: the command under test never deletes it, and
    the independent observation confirms it remained with a safety outcome.
    """

    def test_the_delete_case_disposes_eligible_and_preserves_ineligible(
        self, tmp_path: Path
    ) -> None:
        eligible = "vol-0e11111111111111"
        attached = "vol-0e22222222222222"
        unavailable = "vol-0e33333333333333"
        ec2 = _FakeEc2(
            {
                eligible: _volume_dto(eligible),
                attached: _attached_dto(attached),
                unavailable: _unavailable_dto(unavailable),
            }
        )
        ctx = _prepared_context(tmp_path, case="delete", run_id="run-delete-ineligible", ec2=ec2)

        _run_inventory(ctx, [eligible, attached, unavailable])
        destroyed = _run_destroy(ctx)

        assert destroyed["volume_cleanup_targets"] == [_stack_name()]
        # Only the owned, available, detached volume was ever deleted; the two
        # ineligible volumes were never handed to DeleteVolume.
        assert ec2.deleted == [eligible]
        observed = _observed_volumes(ctx)
        assert observed[eligible]["expected_presence"] == "absent"
        assert observed[eligible]["published_action"] == "delete-requested"
        for ineligible in (attached, unavailable):
            assert observed[ineligible]["expected_presence"] == "present"
            assert observed[ineligible]["observed_present"] is True
            assert observed[ineligible]["published_action"] == "skipped"
            assert observed[ineligible]["published_action_result"] == "safety-preserved"

    def test_the_retain_case_preserves_eligible_and_ineligible_alike(self, tmp_path: Path) -> None:
        eligible = "vol-0e44444444444444"
        attached = "vol-0e55555555555555"
        ec2 = _FakeEc2({eligible: _volume_dto(eligible), attached: _attached_dto(attached)})
        # Fixture cleanup is deliberately withheld here so this test isolates the
        # retention half of the chain; the residual class below drives the rest.
        ctx = _prepared_context(
            tmp_path,
            case="retain-override",
            run_id="run-retain-ineligible",
            ec2=ec2,
            confirm_ebs_fixture_cleanup=False,
        )

        _run_inventory(ctx, [eligible, attached])
        _run_destroy(ctx)

        observed = _observed_volumes(ctx)
        assert observed[eligible]["expected_presence"] == "present"
        assert observed[eligible]["published_action"] == "retained"
        assert observed[attached]["expected_presence"] == "present"
        # The retain policy keeps the eligible volume by policy and the attached
        # one by the safety predicate that would also have blocked its deletion.
        assert observed[attached]["published_action"] == "retained"
        assert observed[attached]["published_action_result"] == "safety-preserved"
        assert ec2.deleted == []
        fixture_cleanup = _scenario_state(ctx.settings)[ownership_volumes.FIXTURE_CLEANUP_KEY]
        assert fixture_cleanup["status"] == "unauthorized"


class TestResidualEbsAccountingFailsTheRun:
    """A volume this run recorded but could not remove fails final inventory.

    Final inventory is the only stage that turns leftover billable storage into
    a failed run, so the whole chain has to reach it with a residual. Two ways
    that happens end to end: the retain case authorizes no fixture cleanup, so
    its retained volumes are still there; and the delete case preserves an
    ineligible volume the command correctly refused to delete. Either way the
    recorded volume must surface as a residual rather than be hidden.
    """

    def test_unauthorized_fixture_cleanup_leaves_retained_residuals(self, tmp_path: Path) -> None:
        first = "vol-0f11111111111111"
        second = "vol-0f22222222222222"
        ec2 = _FakeEc2({first: _volume_dto(first), second: _volume_dto(second)})
        ctx = _prepared_context(
            tmp_path,
            case="retain-override",
            run_id="run-retain-residual",
            ec2=ec2,
            confirm_ebs_fixture_cleanup=False,
        )

        _run_inventory(ctx, [first, second])
        _run_destroy(ctx)
        assert ec2.deleted == []

        with pytest.raises(RuntimeError, match="Recorded EBS volumes remain after teardown"):
            _run_final_inventory(ctx)

        residuals = ctx.checkpoint.state["final_inventory"]["ebs_volume_residuals"]
        assert residuals["status"] == "residual"
        assert residuals["residual_volume_ids"] == [first, second]
        assert residuals["fixture_cleanup_status"] == "unauthorized"

    def test_a_preserved_ineligible_volume_is_a_delete_case_residual(self, tmp_path: Path) -> None:
        eligible = "vol-0f33333333333333"
        attached = "vol-0f44444444444444"
        ec2 = _FakeEc2({eligible: _volume_dto(eligible), attached: _attached_dto(attached)})
        ctx = _prepared_context(tmp_path, case="delete", run_id="run-delete-residual", ec2=ec2)

        _run_inventory(ctx, [eligible, attached])
        _run_destroy(ctx)
        assert ec2.deleted == [eligible]

        with pytest.raises(RuntimeError, match="Recorded EBS volumes remain after teardown"):
            _run_final_inventory(ctx)

        residuals = ctx.checkpoint.state["final_inventory"]["ebs_volume_residuals"]
        assert residuals["status"] == "residual"
        # The disposed volume is proved gone; only the preserved ineligible one
        # remains as billable storage the operator must resolve.
        assert residuals["residual_volume_ids"] == [attached]
        assert residuals["pending_deletion_volume_ids"] == []
