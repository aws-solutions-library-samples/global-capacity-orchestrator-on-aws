"""Independent post-destroy EBS assertions, fixture cleanup, and residuals.

Covers ``checks/volume_outcomes.py``, ``cleanup/volume_fixtures.py``, and their
wiring into ``actions/destroy.py`` and ``actions/final_inventory.py``, offline:

* the retain-override case must find every recorded volume still present with its
  exact recorded tag and a retention outcome, and the delete case must find every
  eligible recorded volume absent while ineligible volumes remain with a safety
  outcome — each proved from live EC2 facts for the exact checkpointed volume IDs
  rather than from the callbacks the code under test published;
* the retained fixtures are deleted only after that evidence is durable, only for
  exact checkpointed identities, only with ``--confirm-ebs-fixture-cleanup``, and
  only through production's just-in-time safety recheck; and
* final inventory accounts for every recorded volume, so an unauthorized or
  incomplete fixture cleanup fails the run instead of leaving billable storage.

Every AWS boundary is a fake; nothing here touches live infrastructure. The only
deletions are recorded calls on an in-memory EC2 double.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    RegionalVolumeTarget,
    VolumeAction,
    VolumeActionResult,
    VolumeCleanupRequest,
    VolumeOutcome,
    VolumePolicy,
    VolumeReasonCode,
    VolumeSnapshot,
    aggregate_target_outcome,
    classify_volume,
)
from scripts.live_release_validation.actions import destroy as destroy_action
from scripts.live_release_validation.actions import final_inventory as final_inventory_action
from scripts.live_release_validation.checks import volume_outcomes
from scripts.live_release_validation.cleanup import volume_fixtures
from scripts.live_release_validation.models import (
    RunCheckpoint,
    RunContext,
    RunSettings,
    ValidationReport,
)
from scripts.live_release_validation.ownership import volume_requests
from scripts.live_release_validation.ownership import volumes as ownership_volumes

_ACCOUNT = "123456789012"
_CALLER_ARN = f"arn:aws:iam::{_ACCOUNT}:role/live-validation"
_BRANCH = "chore/volumes"
_PROJECT = "gco-live"
_PRIMARY = "us-east-1"

_RETAIN_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.RETAIN,
    deletion_authorized=False,
    authorization_source=DeletionAuthorizationSource.NONE,
)
_DELETE_REQUEST = VolumeCleanupRequest(
    policy=VolumePolicy.DELETE,
    deletion_authorized=True,
    authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
)


def _stack_name(region: str = _PRIMARY) -> str:
    return f"{_PROJECT}-{region}"


def _stack_id(region: str = _PRIMARY) -> str:
    return f"arn:aws:cloudformation:{region}:{_ACCOUNT}:stack/{_stack_name(region)}/abc"


def _cluster_tag_key(region: str = _PRIMARY) -> str:
    return f"kubernetes.io/cluster/{_stack_name(region)}"


def _target(region: str = _PRIMARY) -> RegionalVolumeTarget:
    return RegionalVolumeTarget(
        stack_name=_stack_name(region),
        stack_id=_stack_id(region),
        region=region,
        cluster_name=_stack_name(region),
        cluster_tag_key=_cluster_tag_key(region),
    )


def _snapshot(
    volume_id: str,
    *,
    region: str = _PRIMARY,
    state: str = "available",
    tag_value: str | None = "owned",
    attachments: Sequence[str] = (),
    size_gib: int = 50,
) -> VolumeSnapshot:
    return VolumeSnapshot(
        volume_id=volume_id,
        region=region,
        availability_zone=f"{region}a",
        size_gib=size_gib,
        state=state,
        cluster_tag_value=tag_value,
        attachment_ids=tuple(attachments),
    )


def _recorded(snapshot: VolumeSnapshot) -> dict[str, Any]:
    """One pre-destroy inventory record, exactly as the inventory action writes it."""
    return {
        "volume_id": snapshot.volume_id,
        "region": snapshot.region,
        "availability_zone": snapshot.availability_zone,
        "size_gib": snapshot.size_gib,
        "state": snapshot.state,
        "cluster_tag_key": _cluster_tag_key(snapshot.region),
        "cluster_tag_value": snapshot.cluster_tag_value,
        "attachment_ids": list(snapshot.attachment_ids),
        "observed": True,
    }


def _volume_dto(snapshot: VolumeSnapshot, *, tag_key: str | None = None) -> dict[str, Any]:
    """One EC2 ``DescribeVolumes`` DTO for a snapshot's live facts."""
    tags = (
        []
        if snapshot.cluster_tag_value is None
        else [
            {
                "Key": tag_key or _cluster_tag_key(snapshot.region),
                "Value": snapshot.cluster_tag_value,
            }
        ]
    )
    return {
        "VolumeId": snapshot.volume_id,
        "AvailabilityZone": snapshot.availability_zone,
        "Size": snapshot.size_gib,
        "State": snapshot.state,
        "Tags": tags,
        "Attachments": [
            {"VolumeId": snapshot.volume_id, "InstanceId": instance_id, "State": "attached"}
            for instance_id in snapshot.attachment_ids
        ],
    }


def _client_error(code: str, operation: str = "DescribeVolumes") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class _FakeEc2:
    """In-memory EC2 double for exact-volume describes and deletions."""

    def __init__(self, volumes: Mapping[str, dict[str, Any] | str | None]) -> None:
        #: volume ID -> DTO (present), an error code string, or ``None`` (absent).
        self._volumes = dict(volumes)
        self.described: list[str] = []
        self.deleted: list[str] = []

    def describe_volumes(self, **kwargs: Any) -> dict[str, Any]:
        volume_ids = list(kwargs["VolumeIds"])
        assert len(volume_ids) == 1, "recorded volumes are described one exact ID at a time"
        volume_id = volume_ids[0]
        self.described.append(volume_id)
        entry = self._volumes.get(volume_id, None)
        if entry is None:
            raise _client_error("InvalidVolume.NotFound")
        if isinstance(entry, str):
            raise _client_error(entry)
        return {"Volumes": [entry]}

    def delete_volume(self, **kwargs: Any) -> dict[str, Any]:
        volume_id = str(kwargs["VolumeId"])
        self.deleted.append(volume_id)
        entry = self._volumes.get(volume_id)
        if entry is None:
            raise _client_error("InvalidVolume.NotFound", "DeleteVolume")
        self._volumes[volume_id] = None
        return {}


class _FakeSession:
    """Session double that hands out one EC2 double per Region."""

    def __init__(self, clients: Mapping[str, _FakeEc2]) -> None:
        self._clients = dict(clients)
        self.requested: list[tuple[str, str]] = []

    def client(self, service_name: str, region_name: str | None = None) -> _FakeEc2:
        assert service_name == "ec2", "post-destroy observation only reads EC2"
        assert region_name is not None
        self.requested.append((service_name, region_name))
        return self._clients[region_name]


def _settings(tmp_path: Path, **overrides: Any) -> RunSettings:
    base = RunSettings(
        run_id="run-123",
        repo_root=tmp_path,
        report_dir=tmp_path / "report",
        checkpoint_path=tmp_path / "report" / "checkpoint.json",
        expected_account=_ACCOUNT,
        expected_sha="a" * 40,
        expected_branch=_BRANCH,
        profile="configured",
        requested_actions=("all",),
        volume_scenario_case="retain-override",
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _published(
    request: VolumeCleanupRequest,
    records: Sequence[VolumeOutcome],
    *,
    region: str = _PRIMARY,
) -> dict[str, Any]:
    """One target outcome exactly as the production aggregator publishes it."""
    outcome = aggregate_target_outcome(
        target=_target(region),
        request=request,
        records=list(records),
    )
    return dict(outcome.to_dict())


def _retained(snapshot: VolumeSnapshot) -> VolumeOutcome:
    outcome = classify_volume(snapshot, _RETAIN_REQUEST).outcome
    assert outcome is not None
    return outcome


def _preserved(snapshot: VolumeSnapshot) -> VolumeOutcome:
    outcome = classify_volume(snapshot, _DELETE_REQUEST).outcome
    assert outcome is not None
    return outcome


def _delete_requested(snapshot: VolumeSnapshot) -> VolumeOutcome:
    return VolumeOutcome(
        volume_id=snapshot.volume_id,
        region=snapshot.region,
        availability_zone=snapshot.availability_zone,
        size_gib=snapshot.size_gib,
        observed_state=snapshot.state,
        cluster_tag_value=snapshot.cluster_tag_value,
        attachment_ids=snapshot.attachment_ids,
        policy=VolumePolicy.DELETE,
        action=VolumeAction.DELETE_REQUESTED,
        action_result=VolumeActionResult.SUCCESS,
        reason_code=VolumeReasonCode.DELETE_REQUEST_ACCEPTED,
        reason="EC2 accepted the deletion request.",
        follow_up="No action is required.",
        recheck=snapshot,
    )


def _context(
    settings: RunSettings,
    *,
    session: Any,
    recorded: Sequence[VolumeSnapshot] = (),
    published: dict[str, Any] | None = None,
    destroy_sequence: int = 1,
    persist_callback: Callable[[RunCheckpoint], None] | None = None,
    inventory_result: str = "recorded",
) -> RunContext:
    case = settings.volume_scenario_case
    volume_ids = [snapshot.volume_id for snapshot in recorded]
    checkpoint = RunCheckpoint(identity=settings.identity())
    checkpoint.state.update(
        {
            "account_arn": _CALLER_ARN,
            "enabled_regions": [_PRIMARY],
            "owned_stacks": {_PRIMARY: {_stack_name(): {"stack_id": _stack_id()}}},
            "volume_scenario": {
                "case": case,
                volume_outcomes.PRE_DESTROY_INVENTORY_KEY: {
                    "status": inventory_result,
                    "case": case,
                    "regions": {
                        _PRIMARY: {
                            "region": _PRIMARY,
                            "stack_name": _stack_name(),
                            "stack_id": _stack_id(),
                            "cluster_name": _stack_name(),
                            "cluster_tag_key": _cluster_tag_key(),
                            "result": inventory_result,
                            "volume_ids": volume_ids,
                            "volumes": [_recorded(snapshot) for snapshot in recorded],
                        }
                    },
                },
                "strict_destroy_targets": {
                    "status": "captured",
                    "case": case,
                    "targets": {
                        _stack_name(): {
                            "stack_name": _stack_name(),
                            "stack_id": _stack_id(),
                            "region": _PRIMARY,
                            "cluster_name": _stack_name(),
                            "cluster_tag_key": _cluster_tag_key(),
                            "recorded_volume_ids": volume_ids,
                            "recorded_volume_count": len(volume_ids),
                            "complete": True,
                            "result": "captured",
                        }
                    },
                },
            },
            "destroy_helper_outcomes": (
                []
                if published is None
                else [
                    {
                        "destroy_sequence": destroy_sequence,
                        "name": volume_requests.VOLUME_CLEANUP_CALLBACK_NAME,
                        "at": "2026-07-17T00:00:01+00:00",
                        "details": published,
                    }
                ]
            ),
        }
    )
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
        cdk_context={"cluster_observability": {"enabled": False}},
        deployment_regions=(_PRIMARY,),
        config=SimpleNamespace(project_name=_PROJECT, global_region=_PRIMARY),
        session=session,
        stack_manager=MagicMock(),
        aws_client=MagicMock(),
        job_manager=MagicMock(),
        persist_callback=persist_callback or MagicMock(),
    )


def _verify(
    ctx: RunContext,
    *,
    request: VolumeCleanupRequest | None,
    targets: Mapping[str, RegionalVolumeTarget] | None = None,
    destroy_sequence: int = 1,
) -> dict[str, Any]:
    with patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH):
        return volume_outcomes.verify_post_destroy_volume_outcomes(
            ctx,
            request=request,
            targets={_stack_name(): _target()} if targets is None else targets,
            destroy_sequence=destroy_sequence,
        )


def _scenario_state(ctx: RunContext) -> dict[str, Any]:
    state = ctx.checkpoint.state["volume_scenario"]
    assert isinstance(state, dict)
    return state


class TestRetainOverrideObservations:
    """Retained volumes must still exist, with their exact recorded tag."""

    def test_present_volumes_with_retention_outcomes_verify(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        attached = _snapshot("vol-0000000000000002", state="in-use", attachments=("i-abc",))
        session = _FakeSession(
            {_PRIMARY: _FakeEc2({s.volume_id: _volume_dto(s) for s in (kept, attached)})}
        )
        ctx = _context(
            _settings(tmp_path),
            session=session,
            recorded=(kept, attached),
            published=_published(_RETAIN_REQUEST, [_retained(kept), _retained(attached)]),
        )

        observations = _verify(ctx, request=_RETAIN_REQUEST)

        assert observations["status"] == "verified"
        assert observations["problems"] == []
        entry = observations["targets"][_stack_name()]
        assert entry["recorded_volume_count"] == 2
        assert [volume["expected_presence"] for volume in entry["volumes"]] == ["present"] * 2
        assert [volume["observed_present"] for volume in entry["volumes"]] == [True, True]
        # The ineligible volume is verified against a preservation outcome.
        assert entry["volumes"][1]["published_action_result"] == "safety-preserved"
        assert entry["volumes"][1]["recorded_safety_reasons"] == [
            "state-not-available",
            "attachments-present",
        ]
        assert _scenario_state(ctx)[volume_outcomes.POST_DESTROY_OBSERVATIONS_KEY] == observations

    def test_the_observation_describes_exactly_the_checkpointed_ids(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        ec2 = _FakeEc2({kept.volume_id: _volume_dto(kept), "vol-000000000000ffff": {}})
        ctx = _context(
            _settings(tmp_path),
            session=_FakeSession({_PRIMARY: ec2}),
            recorded=(kept,),
            published=_published(_RETAIN_REQUEST, [_retained(kept)]),
        )

        _verify(ctx, request=_RETAIN_REQUEST)

        assert ec2.described == [kept.volume_id]

    def test_a_vanished_retained_volume_fails_the_run(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        ctx = _context(
            _settings(tmp_path),
            session=_FakeSession({_PRIMARY: _FakeEc2({})}),
            recorded=(kept,),
            published=_published(_RETAIN_REQUEST, [_retained(kept)]),
        )

        with pytest.raises(RuntimeError, match="must still exist with its exact"):
            _verify(ctx, request=_RETAIN_REQUEST)

        observations = _scenario_state(ctx)[volume_outcomes.POST_DESTROY_OBSERVATIONS_KEY]
        assert observations["status"] == "failed"
        assert kept.volume_id in observations["problems"][0]

    def test_a_changed_cluster_tag_value_fails_the_run(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        drifted = _snapshot("vol-0000000000000001", tag_value="shared")
        ctx = _context(
            _settings(tmp_path),
            session=_FakeSession({_PRIMARY: _FakeEc2({kept.volume_id: _volume_dto(drifted)})}),
            recorded=(kept,),
            published=_published(_RETAIN_REQUEST, [_retained(kept)]),
        )

        with pytest.raises(RuntimeError, match="not the recorded 'owned'"):
            _verify(ctx, request=_RETAIN_REQUEST)

    def test_a_deleted_report_for_a_retained_volume_fails_the_run(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        ctx = _context(
            _settings(tmp_path),
            session=_FakeSession({_PRIMARY: _FakeEc2({kept.volume_id: _volume_dto(kept)})}),
            recorded=(kept,),
            published=_published(_DELETE_REQUEST, [_delete_requested(kept)]),
        )

        with pytest.raises(RuntimeError, match="was reported as 'delete-requested'"):
            _verify(ctx, request=_RETAIN_REQUEST)


class TestDeleteCaseObservations:
    """Eligible volumes must be gone; ineligible ones must be preserved."""

    @staticmethod
    def _delete_settings(tmp_path: Path) -> RunSettings:
        return _settings(tmp_path, volume_scenario_case="delete")

    def test_eligible_absence_and_ineligible_preservation_verify(self, tmp_path: Path) -> None:
        deleted = _snapshot("vol-0000000000000001")
        attached = _snapshot("vol-0000000000000002", state="in-use", attachments=("i-abc",))
        ctx = _context(
            self._delete_settings(tmp_path),
            session=_FakeSession({_PRIMARY: _FakeEc2({attached.volume_id: _volume_dto(attached)})}),
            recorded=(deleted, attached),
            published=_published(
                _DELETE_REQUEST,
                [_delete_requested(deleted), _preserved(attached)],
            ),
        )

        observations = _verify(ctx, request=_DELETE_REQUEST)

        assert observations["status"] == "verified"
        entry = observations["targets"][_stack_name()]
        assert [volume["expected_presence"] for volume in entry["volumes"]] == [
            "absent",
            "present",
        ]
        assert entry["volumes"][1]["published_action"] == "skipped"

    def test_a_surviving_eligible_volume_fails_the_run(self, tmp_path: Path) -> None:
        survivor = _snapshot("vol-0000000000000001")
        ctx = _context(
            self._delete_settings(tmp_path),
            session=_FakeSession({_PRIMARY: _FakeEc2({survivor.volume_id: _volume_dto(survivor)})}),
            recorded=(survivor,),
            published=_published(_DELETE_REQUEST, [_delete_requested(survivor)]),
        )

        with pytest.raises(RuntimeError, match="but still exists in state 'available'"):
            _verify(ctx, request=_DELETE_REQUEST)

    def test_unprovable_absence_fails_the_run(self, tmp_path: Path) -> None:
        deleted = _snapshot("vol-0000000000000001")
        ctx = _context(
            self._delete_settings(tmp_path),
            session=_FakeSession(
                {_PRIMARY: _FakeEc2({deleted.volume_id: "UnauthorizedOperation"})}
            ),
            recorded=(deleted,),
            published=_published(_DELETE_REQUEST, [_delete_requested(deleted)]),
        )

        with pytest.raises(RuntimeError, match="absence is unproven"):
            _verify(ctx, request=_DELETE_REQUEST)

    def test_a_deleted_report_for_an_ineligible_volume_fails_the_run(self, tmp_path: Path) -> None:
        attached = _snapshot("vol-0000000000000002", state="in-use", attachments=("i-abc",))
        ctx = _context(
            self._delete_settings(tmp_path),
            session=_FakeSession({_PRIMARY: _FakeEc2({attached.volume_id: _volume_dto(attached)})}),
            recorded=(attached,),
            published=_published(_DELETE_REQUEST, [_delete_requested(attached)]),
        )

        with pytest.raises(RuntimeError, match="was reported as 'delete-requested'"):
            _verify(ctx, request=_DELETE_REQUEST)

    def test_a_recorded_volume_with_no_published_record_fails_the_run(self, tmp_path: Path) -> None:
        deleted = _snapshot("vol-0000000000000001")
        ctx = _context(
            self._delete_settings(tmp_path),
            session=_FakeSession({_PRIMARY: _FakeEc2({})}),
            recorded=(deleted,),
            published=_published(_DELETE_REQUEST, []),
        )

        with pytest.raises(RuntimeError, match="published no per-volume outcome"):
            _verify(ctx, request=_DELETE_REQUEST)


class TestObservationFencing:
    """No scenario, no inventory, and no target all fail closed."""

    def test_a_disabled_scenario_observes_nothing(self, tmp_path: Path) -> None:
        ctx = _context(
            _settings(tmp_path, volume_scenario_case="disabled"),
            session=_FakeSession({}),
        )

        observations = _verify(ctx, request=None, targets={})

        assert observations["status"] == "skipped"

    def test_a_missing_pre_destroy_inventory_fails_the_run(self, tmp_path: Path) -> None:
        ctx = _context(
            _settings(tmp_path),
            session=_FakeSession({}),
            recorded=(),
            published=_published(_RETAIN_REQUEST, []),
        )
        _scenario_state(ctx).pop(volume_outcomes.PRE_DESTROY_INVENTORY_KEY)

        with pytest.raises(RuntimeError, match="no pre-destroy volume inventory is checkpointed"):
            _verify(ctx, request=_RETAIN_REQUEST)

    def test_a_drifted_checkpoint_identity_refuses_to_observe(self, tmp_path: Path) -> None:
        ctx = _context(
            _settings(tmp_path),
            session=_FakeSession({}),
            published=_published(_RETAIN_REQUEST, []),
        )
        ctx.checkpoint.identity = dict(ctx.checkpoint.identity) | {"run_id": "someone-else"}

        with pytest.raises(RuntimeError, match="Checkpoint identity does not match"):
            _verify(ctx, request=_RETAIN_REQUEST)

    def test_zero_captured_targets_fails_the_run(self, tmp_path: Path) -> None:
        ctx = _context(
            _settings(tmp_path),
            session=_FakeSession({}),
            published=_published(_RETAIN_REQUEST, []),
        )

        with pytest.raises(RuntimeError, match="captured no exact regional volume target"):
            _verify(ctx, request=_RETAIN_REQUEST, targets={})


def _cleanup(
    ctx: RunContext,
    observations: Mapping[str, Any],
    *,
    request: VolumeCleanupRequest | None = _RETAIN_REQUEST,
) -> dict[str, Any]:
    with patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH):
        return volume_fixtures.cleanup_validation_fixture_volumes(
            ctx,
            request=request,
            targets={_stack_name(): _target()},
            observations=observations,
        )


class TestFixtureCleanup:
    """Retained fixtures are deleted only under explicit, fenced authorization."""

    @staticmethod
    def _retained_context(
        tmp_path: Path,
        *,
        snapshots: Sequence[VolumeSnapshot],
        confirm: bool = True,
    ) -> tuple[RunContext, _FakeEc2]:
        ec2 = _FakeEc2({s.volume_id: _volume_dto(s) for s in snapshots})
        ctx = _context(
            _settings(tmp_path, confirm_ebs_fixture_cleanup=confirm),
            session=_FakeSession({_PRIMARY: ec2}),
            recorded=snapshots,
            published=_published(_RETAIN_REQUEST, [_retained(s) for s in snapshots]),
        )
        return ctx, ec2

    def test_authorized_cleanup_deletes_only_recorded_fixtures(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        ctx, ec2 = self._retained_context(tmp_path, snapshots=(kept,))
        observations = _verify(ctx, request=_RETAIN_REQUEST)

        evidence = _cleanup(ctx, observations)

        assert evidence["status"] == "cleaned"
        assert ec2.deleted == [kept.volume_id]
        target = evidence["targets"][_stack_name()]
        assert target["candidate_volume_ids"] == [kept.volume_id]
        assert target["volumes"][0]["action"] == "delete-requested"
        state = _scenario_state(ctx)
        assert state[ownership_volumes.FIXTURE_CLEANUP_KEY] == evidence
        assert f"fixture-cleanup:{_PRIMARY}" in state["authorizations"]

    def test_the_just_in_time_recheck_preserves_an_ineligible_fixture(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        ctx, ec2 = self._retained_context(tmp_path, snapshots=(kept,))
        observations = _verify(ctx, request=_RETAIN_REQUEST)
        # The volume is reattached between the observation and the fixture deletion.
        ec2._volumes[kept.volume_id] = _volume_dto(
            _snapshot(kept.volume_id, state="in-use", attachments=("i-abc",))
        )

        evidence = _cleanup(ctx, observations)

        assert ec2.deleted == []
        assert evidence["status"] == "unresolved"
        assert "safety-recheck-changed" in evidence["problems"][0]

    def test_an_already_absent_fixture_is_an_idempotent_success(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        ctx, ec2 = self._retained_context(tmp_path, snapshots=(kept,))
        observations = _verify(ctx, request=_RETAIN_REQUEST)
        ec2._volumes[kept.volume_id] = None

        evidence = _cleanup(ctx, observations)

        assert evidence["status"] == "cleaned"
        assert evidence["targets"][_stack_name()]["volumes"][0]["action"] == "already-absent"

    def test_without_explicit_authorization_nothing_is_deleted(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        ctx, ec2 = self._retained_context(tmp_path, snapshots=(kept,), confirm=False)
        observations = _verify(ctx, request=_RETAIN_REQUEST)

        evidence = _cleanup(ctx, observations)

        assert evidence["status"] == "unauthorized"
        assert "--confirm-ebs-fixture-cleanup" in evidence["reason"]
        assert ec2.deleted == []
        assert _scenario_state(ctx)[ownership_volumes.FIXTURE_CLEANUP_KEY] == evidence

    def test_unverified_retention_evidence_refuses_to_delete(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        ctx, ec2 = self._retained_context(tmp_path, snapshots=(kept,))
        observations = _verify(ctx, request=_RETAIN_REQUEST)

        with pytest.raises(ValueError, match="durable, verified post-destroy retention"):
            _cleanup(ctx, {**observations, "status": "observing"})

        assert ec2.deleted == []

    def test_the_delete_case_cleans_no_fixture(self, tmp_path: Path) -> None:
        attached = _snapshot("vol-0000000000000002", state="in-use", attachments=("i-abc",))
        ec2 = _FakeEc2({attached.volume_id: _volume_dto(attached)})
        ctx = _context(
            _settings(tmp_path, volume_scenario_case="delete"),
            session=_FakeSession({_PRIMARY: ec2}),
            recorded=(attached,),
            published=_published(_DELETE_REQUEST, [_preserved(attached)]),
        )
        observations = _verify(ctx, request=_DELETE_REQUEST)

        evidence = _cleanup(ctx, observations, request=_DELETE_REQUEST)

        assert evidence["status"] == "skipped"
        assert ec2.deleted == []

    def test_a_disabled_scenario_cleans_no_fixture(self, tmp_path: Path) -> None:
        ctx = _context(
            _settings(tmp_path, volume_scenario_case="disabled"),
            session=_FakeSession({}),
        )

        evidence = _cleanup(ctx, {"status": "skipped"}, request=None)

        assert evidence["status"] == "skipped"


class TestResidualAccounting:
    """Final inventory refuses to hide a volume this run recorded."""

    def test_absent_volumes_leave_no_residual(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        ctx = _context(
            _settings(tmp_path),
            session=_FakeSession({_PRIMARY: _FakeEc2({})}),
            recorded=(kept,),
        )

        with patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH):
            residuals = volume_outcomes.volume_residual_inventory(ctx)

        assert residuals["status"] == "clear"
        assert residuals["residual_volume_ids"] == []
        assert residuals["recorded_volume_count"] == 1

    def test_a_surviving_volume_is_a_residual(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        ctx = _context(
            _settings(tmp_path),
            session=_FakeSession({_PRIMARY: _FakeEc2({kept.volume_id: _volume_dto(kept)})}),
            recorded=(kept,),
        )
        _scenario_state(ctx)[ownership_volumes.FIXTURE_CLEANUP_KEY] = {"status": "unauthorized"}

        with patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH):
            residuals = volume_outcomes.volume_residual_inventory(ctx)

        assert residuals["status"] == "residual"
        assert residuals["residual_volume_ids"] == [kept.volume_id]
        assert residuals["fixture_cleanup_status"] == "unauthorized"
        assert "--confirm-ebs-fixture-cleanup" in residuals["follow_up"]

    def test_an_accepted_deletion_in_flight_is_not_a_residual(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        deleting = _snapshot(kept.volume_id, state="deleting")
        ctx = _context(
            _settings(tmp_path),
            session=_FakeSession({_PRIMARY: _FakeEc2({kept.volume_id: _volume_dto(deleting)})}),
            recorded=(kept,),
        )

        with patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH):
            residuals = volume_outcomes.volume_residual_inventory(ctx)

        assert residuals["residual_volume_ids"] == []
        assert residuals["pending_deletion_volume_ids"] == [kept.volume_id]

    def test_an_unreadable_volume_is_a_residual(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        ctx = _context(
            _settings(tmp_path),
            session=_FakeSession({_PRIMARY: _FakeEc2({kept.volume_id: "RequestLimitExceeded"})}),
            recorded=(kept,),
        )

        with patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH):
            residuals = volume_outcomes.volume_residual_inventory(ctx)

        assert residuals["residual_volume_ids"] == [kept.volume_id]

    def test_a_disabled_scenario_accounts_for_nothing(self, tmp_path: Path) -> None:
        ctx = _context(
            _settings(tmp_path, volume_scenario_case="disabled"),
            session=_FakeSession({}),
        )

        residuals = volume_outcomes.volume_residual_inventory(ctx)

        assert residuals["status"] == "skipped"
        assert residuals["residual_volume_ids"] == []
        assert "volume_scenario" in ctx.checkpoint.state


class TestFinalInventoryFailsOnResiduals:
    """The residual accounting is wired into the final-inventory action."""

    @staticmethod
    def _run(ctx: RunContext) -> dict[str, Any]:
        ctx.checkpoint.baseline = {"ecr_regions": [_PRIMARY]}
        absent = {"all_absent": True, "residual": [], "absent": []}
        with (
            patch.object(
                final_inventory_action, "_verify_target_stack_absence", return_value=absent
            ),
            patch.object(final_inventory_action, "capture_baseline", return_value={}),
            patch.object(
                final_inventory_action,
                "_strip_expected_retained_ecr",
                return_value=({}, []),
            ),
            patch.object(final_inventory_action, "compare_baseline", return_value=[]),
            patch.object(final_inventory_action, "collect_project_resources", return_value={}),
            patch.object(final_inventory_action, "_strip_baseline_ecr", return_value={}),
            patch.object(final_inventory_action, "_strip_accepted_retained_ecr", return_value={}),
            patch.object(
                final_inventory_action, "_strip_expected_pending_kms", return_value=({}, [])
            ),
            patch.object(
                final_inventory_action, "summarize_project_resources", return_value={"stacks": 0}
            ),
            patch.object(final_inventory_action, "project_resources_are_absent", return_value=True),
            patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH),
        ):
            return final_inventory_action.action_final_inventory(ctx)

    def test_a_retained_fixture_fails_final_inventory(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        ctx = _context(
            _settings(tmp_path),
            session=_FakeSession({_PRIMARY: _FakeEc2({kept.volume_id: _volume_dto(kept)})}),
            recorded=(kept,),
        )

        with pytest.raises(RuntimeError, match="Recorded EBS volumes remain after teardown"):
            self._run(ctx)

        residuals = ctx.checkpoint.state["final_inventory"]["ebs_volume_residuals"]
        assert residuals["residual_volume_ids"] == [kept.volume_id]

    def test_a_cleaned_fixture_passes_and_is_counted(self, tmp_path: Path) -> None:
        kept = _snapshot("vol-0000000000000001")
        ctx = _context(
            _settings(tmp_path),
            session=_FakeSession({_PRIMARY: _FakeEc2({})}),
            recorded=(kept,),
        )

        result = self._run(ctx)

        assert result["ebs_volume_residuals"]["status"] == "clear"
        assert result["summary"]["ebs_fixture_volumes"] == 0


class TestDestroyPersistsEvidenceBeforeCompletion:
    """Observations and fixture cleanup are durable before ``destroyed=True``."""

    def test_the_destroy_action_records_both_before_marking_teardown_complete(
        self, tmp_path: Path
    ) -> None:
        observed_at: list[bool] = []
        cleaned_at: list[bool] = []

        def observe(ctx: RunContext, **kwargs: Any) -> dict[str, Any]:
            observed_at.append(ctx.checkpoint.destroyed)
            return {"status": "verified", "targets": {}}

        def clean(ctx: RunContext, **kwargs: Any) -> dict[str, Any]:
            cleaned_at.append(ctx.checkpoint.destroyed)
            return {"status": "cleaned"}

        settings = _settings(tmp_path)
        ctx = _context(settings, session=_FakeSession({}))
        ctx.checkpoint.deployment_attempted = True
        ctx.checkpoint.state["target_stack_regions"] = {_stack_name(): _PRIMARY}
        ctx.checkpoint.state["bootstrap_stacks"] = {}
        ctx.stack_manager.destroy_orchestrated.return_value = (True, [_stack_name()], [])
        present = {"all_absent": False, "residual": [{"stack_name": _stack_name()}], "absent": []}
        absent = {"all_absent": True, "residual": [], "absent": []}

        with (
            patch.object(destroy_action, "cleanup_workloads", return_value={"complete": True}),
            patch.object(destroy_action, "_reconcile_stack_ownership"),
            patch.object(destroy_action, "_checkpoint_new_ecr_repositories"),
            patch.object(destroy_action, "_checkpoint_new_ecr_images"),
            patch.object(destroy_action, "_checkpoint_retained_kms_keys"),
            patch.object(destroy_action, "_ensure_log_cleanup_helper", return_value={}),
            patch.object(destroy_action, "_prepared_change_set_authority", return_value={}),
            patch.object(destroy_action, "_retained_resource_cleanup", return_value={}),
            patch.object(
                destroy_action,
                "_verify_target_stack_absence",
                side_effect=[present, absent, absent],
            ),
            patch.object(
                destroy_action,
                "capture_strict_volume_targets",
                return_value={"status": "captured", "case": "retain-override"},
            ),
            patch.object(
                destroy_action,
                "strict_volume_cleanup_targets",
                return_value={_stack_name(): _target()},
            ),
            patch.object(
                destroy_action,
                "resolve_strict_volume_cleanup_request",
                return_value=(_RETAIN_REQUEST, {"status": "resolved"}),
            ),
            patch.object(destroy_action, "verify_volume_cleanup_evidence", return_value={}),
            patch.object(
                destroy_action, "verify_post_destroy_volume_outcomes", side_effect=observe
            ),
            patch.object(destroy_action, "cleanup_validation_fixture_volumes", side_effect=clean),
        ):
            result = destroy_action.destroy_deployment(ctx)

        assert observed_at == [False]
        assert cleaned_at == [False]
        assert result["volume_post_destroy_observations"]["status"] == "verified"
        assert result["volume_fixture_cleanup"]["status"] == "cleaned"
        assert ctx.checkpoint.destroyed is True
