"""Strict destroy-time capture of exact regional EBS volume-cleanup targets.

Covers ``ownership/volume_targets.py`` and its one caller in the strict destroy
action, offline:

* a complete capture records the exact CloudFormation stack ARN, Region, EKS
  cluster physical ID, exact cluster tag key, and recorded volume identities for
  every configured Region, behind the scenario authorization gate and with no
  EKS or EC2 client created;
* every way target identity can be missing or ambiguous — no checkpointed
  inventory, a failed inventory, a missing or drifted stack ARN, a missing
  cluster physical ID, a tag-key mismatch, a duplicated or out-of-Region
  recorded volume, and refused stack authorization — is captured as a blocked
  target, persisted with a machine-readable reason, and then raised;
* only complete strict targets reach the common cleanup helper accessor; and
* the destroy action captures identity before it asks the orchestrator to delete
  anything, so a blocked identity stops teardown instead of destroying the
  evidence the scenario still needed.

Every AWS, git, and orchestration boundary is mocked; nothing here touches live
infrastructure or deletes anything.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cli.volume_cleanup import (
    RegionalVolumeTarget,
    TargetVolumeCleanupOutcome,
    VolumeCleanupRequest,
    VolumeCleanupStatus,
)
from scripts.live_release_validation.actions import destroy as destroy_action
from scripts.live_release_validation.actions import volume_inventory
from scripts.live_release_validation.models import (
    RunCheckpoint,
    RunContext,
    RunSettings,
    ValidationReport,
)
from scripts.live_release_validation.ownership import volume_targets
from scripts.live_release_validation.ownership import volumes as ownership_volumes

_ACCOUNT = "123456789012"
_CALLER_ARN = f"arn:aws:iam::{_ACCOUNT}:role/live-validation"
_BRANCH = "chore/volumes"
_PROJECT = "gco-live"
_PRIMARY = "us-east-1"
_SECONDARY = "us-west-2"


def _stack_name(region: str) -> str:
    return f"{_PROJECT}-{region}"


def _stack_id(region: str) -> str:
    return f"arn:aws:cloudformation:{region}:{_ACCOUNT}:stack/{_stack_name(region)}/abc"


def _cluster_tag_key(region: str) -> str:
    return f"kubernetes.io/cluster/{_stack_name(region)}"


def _volume_id(region: str) -> str:
    return f"vol-0{region.replace('-', '')}"


def _owned_stacks(*regions: str) -> dict[str, Any]:
    return {region: {_stack_name(region): {"stack_id": _stack_id(region)}} for region in regions}


def _completed_volume_outcome(
    region: str,
    *,
    request: VolumeCleanupRequest,
) -> dict[str, Any]:
    """One serialized target cleanup outcome, as StackManager publishes it."""
    return dict(
        TargetVolumeCleanupOutcome(
            stack_name=_stack_name(region),
            stack_id=_stack_id(region),
            target_region=region,
            target_cluster=_stack_name(region),
            cluster_tag_key=_cluster_tag_key(region),
            policy=request.policy,
            deletion_authorized=request.deletion_authorized,
            authorization_source=request.authorization_source,
            status=VolumeCleanupStatus.COMPLETED,
            successful=True,
        ).to_dict()
    )


def _recorded_volume(
    region: str,
    *,
    volume_id: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    record = {
        "volume_id": volume_id or _volume_id(region),
        "region": region,
        "availability_zone": f"{region}a",
        "size_gib": 50,
        "state": "available",
        "cluster_tag_key": _cluster_tag_key(region),
        "cluster_tag_value": "owned",
        "attachment_ids": [],
        "observed": True,
    }
    record.update(overrides)
    return record


def _region_inventory(
    region: str,
    *,
    volumes: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    recorded = volumes if volumes is not None else [_recorded_volume(region)]
    evidence: dict[str, Any] = {
        "region": region,
        "stack_name": _stack_name(region),
        "stack_id": _stack_id(region),
        "cluster_name": _stack_name(region),
        "cluster_tag_key": _cluster_tag_key(region),
        "recorded_at": "2026-07-17T00:00:00+00:00",
        "volumes": recorded,
        "volume_ids": [str(volume["volume_id"]) for volume in recorded],
        "result": "recorded",
    }
    evidence.update(overrides)
    return evidence


def _inventory(*evidence: dict[str, Any]) -> dict[str, Any]:
    """The checkpointed pre-destroy inventory holding the given Regions' evidence."""
    return {
        "status": "recorded",
        "case": "retain-override",
        "regions": {str(item["region"]): item for item in evidence},
    }


def _regions_inventory(*regions: str) -> dict[str, Any]:
    """A fully recorded inventory for every given Region."""
    return _inventory(*(_region_inventory(region) for region in regions))


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


def _context(
    settings: RunSettings,
    *,
    session: Any,
    regions: tuple[str, ...] = (_PRIMARY,),
    **state: Any,
) -> RunContext:
    checkpoint = RunCheckpoint(identity=settings.identity())
    checkpoint.state.update(
        {
            "account_arn": _CALLER_ARN,
            "owned_stacks": _owned_stacks(*regions),
            **state,
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
        deployment_regions=regions,
        config=SimpleNamespace(project_name=_PROJECT, global_region=_PRIMARY),
        session=session,
        stack_manager=MagicMock(),
        aws_client=MagicMock(),
        job_manager=MagicMock(),
        persist_callback=MagicMock(),
    )


def _scenario_context(
    tmp_path: Path,
    *,
    inventory: dict[str, Any] | None,
    regions: tuple[str, ...] = (_PRIMARY,),
    **settings_overrides: Any,
) -> tuple[RunContext, MagicMock]:
    """A context whose scenario state already holds a pre-destroy inventory."""
    session = MagicMock()
    scenario: dict[str, Any] = {"case": "retain-override"}
    if inventory is not None:
        scenario[ownership_volumes.PRE_DESTROY_INVENTORY_KEY] = inventory
    ctx = _context(
        _settings(tmp_path, **settings_overrides),
        session=session,
        regions=regions,
        volume_scenario=scenario,
    )
    return ctx, session


def _capture(
    ctx: RunContext,
    *,
    branch: str = _BRANCH,
    stack_authorization: Exception | None = None,
) -> dict[str, Any]:
    with (
        patch.object(ownership_volumes, "_resolve_branch", return_value=branch),
        patch.object(
            ownership_volumes,
            "_authorize_owned_stack",
            side_effect=stack_authorization,
        ),
    ):
        return volume_targets.capture_strict_volume_targets(ctx)


def _blocked_capture(
    ctx: RunContext,
    *,
    stack_authorization: Exception | None = None,
) -> dict[str, Any]:
    """Run a capture expected to block, returning the persisted evidence."""
    with pytest.raises(RuntimeError, match="cannot establish an exact volume target identity"):
        _capture(ctx, stack_authorization=stack_authorization)
    persisted = ctx.checkpoint.state["volume_scenario"][volume_targets.STRICT_DESTROY_TARGETS_KEY]
    assert isinstance(persisted, dict)
    return persisted


def _blocked_reason(capture: dict[str, Any], region: str) -> str:
    entry = capture["targets"][_stack_name(region)]
    assert entry["complete"] is False
    assert entry["result"] == "blocked"
    assert entry["reason"]
    return str(entry["reason_code"])


class TestCompleteCapture:
    """Complete identity is captured from checkpointed pre-destroy evidence."""

    def test_every_region_records_its_exact_identity_and_volumes(self, tmp_path: Path) -> None:
        ctx, session = _scenario_context(
            tmp_path,
            inventory=_regions_inventory(_PRIMARY, _SECONDARY),
            regions=(_PRIMARY, _SECONDARY),
        )

        capture = _capture(ctx)

        assert capture["status"] == "captured"
        assert capture["blocked"] == []
        assert sorted(capture["targets"]) == [_stack_name(_PRIMARY), _stack_name(_SECONDARY)]
        for region in (_PRIMARY, _SECONDARY):
            entry = capture["targets"][_stack_name(region)]
            assert entry["complete"] is True
            assert entry["stack_id"] == _stack_id(region)
            assert entry["region"] == region
            assert entry["cluster_name"] == _stack_name(region)
            assert entry["cluster_tag_key"] == _cluster_tag_key(region)
            assert entry["recorded_volume_ids"] == [_volume_id(region)]
            assert entry["recorded_volume_count"] == 1
        # Identity capture is a pure read of checkpointed evidence.
        session.client.assert_not_called()

    def test_capture_is_persisted_per_region_before_the_next_one(self, tmp_path: Path) -> None:
        ctx, _ = _scenario_context(
            tmp_path,
            inventory=_regions_inventory(_PRIMARY, _SECONDARY),
            regions=(_PRIMARY, _SECONDARY),
        )

        capture = _capture(ctx)

        persisted = ctx.checkpoint.state["volume_scenario"][
            volume_targets.STRICT_DESTROY_TARGETS_KEY
        ]
        assert persisted is capture
        # One opening write, one per Region, one completion write.
        assert ctx.persist_callback.call_count >= 4  # type: ignore[attr-defined]

    def test_each_region_is_authorized_against_its_exact_stack(self, tmp_path: Path) -> None:
        ctx, _ = _scenario_context(
            tmp_path,
            inventory=_regions_inventory(_PRIMARY, _SECONDARY),
            regions=(_PRIMARY, _SECONDARY),
        )

        with (
            patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH),
            patch.object(ownership_volumes, "_authorize_owned_stack") as authorize,
        ):
            volume_targets.capture_strict_volume_targets(ctx)

        assert [call.args for call in authorize.call_args_list] == [
            (ctx, _stack_name(region), region, _stack_id(region))
            for region in (_PRIMARY, _SECONDARY)
        ]
        authorizations = ctx.checkpoint.state["volume_scenario"]["authorizations"]
        assert sorted(authorizations) == [
            f"strict-volume-target:{_PRIMARY}",
            f"strict-volume-target:{_SECONDARY}",
        ]

    def test_a_disabled_scenario_captures_nothing(self, tmp_path: Path) -> None:
        ctx, session = _scenario_context(
            tmp_path,
            inventory=None,
            volume_scenario_case="disabled",
        )
        ctx.checkpoint.state.pop("volume_scenario")

        capture = volume_targets.capture_strict_volume_targets(ctx)

        assert capture["status"] == "skipped"
        assert "volume_scenario" not in ctx.checkpoint.state
        session.client.assert_not_called()


class TestMissingOrAmbiguousIdentityBlocks:
    """Fail closed, with the reason durable, before any EKS or EC2 request."""

    def test_no_checkpointed_inventory_blocks_the_region(self, tmp_path: Path) -> None:
        ctx, session = _scenario_context(tmp_path, inventory=None)

        capture = _blocked_capture(ctx)

        assert _blocked_reason(capture, _PRIMARY) == "pre-destroy-inventory-missing"
        assert capture["status"] == "blocked"
        assert [entry["stack_name"] for entry in capture["blocked"]] == [_stack_name(_PRIMARY)]
        session.client.assert_not_called()

    def test_a_failed_inventory_blocks_the_region(self, tmp_path: Path) -> None:
        ctx, _ = _scenario_context(
            tmp_path,
            inventory=_inventory({"region": _PRIMARY, "result": "failed"}),
        )

        capture = _blocked_capture(ctx)

        assert _blocked_reason(capture, _PRIMARY) == "pre-destroy-inventory-incomplete"

    def test_a_missing_stack_arn_blocks_the_region(self, tmp_path: Path) -> None:
        ctx, _ = _scenario_context(
            tmp_path,
            inventory=_inventory(_region_inventory(_PRIMARY, stack_id="")),
        )

        capture = _blocked_capture(ctx)

        assert _blocked_reason(capture, _PRIMARY) == "missing-strict-stack-arn"

    def test_a_stack_arn_that_ownership_does_not_authorize_blocks(self, tmp_path: Path) -> None:
        other = f"arn:aws:cloudformation:{_PRIMARY}:{_ACCOUNT}:stack/{_stack_name(_PRIMARY)}/other"
        ctx, _ = _scenario_context(
            tmp_path,
            inventory=_inventory(_region_inventory(_PRIMARY, stack_id=other)),
        )

        capture = _blocked_capture(ctx)

        assert _blocked_reason(capture, _PRIMARY) == "strict-stack-arn-mismatch"

    def test_an_unowned_stack_blocks_the_region(self, tmp_path: Path) -> None:
        ctx, _ = _scenario_context(tmp_path, inventory=_regions_inventory(_PRIMARY))
        ctx.checkpoint.state["owned_stacks"] = {}

        capture = _blocked_capture(ctx)

        assert _blocked_reason(capture, _PRIMARY) == "owned-stack-record-missing"

    def test_a_missing_cluster_physical_id_blocks_the_region(self, tmp_path: Path) -> None:
        ctx, _ = _scenario_context(
            tmp_path,
            inventory=_inventory(_region_inventory(_PRIMARY, cluster_name="")),
        )

        capture = _blocked_capture(ctx)

        assert _blocked_reason(capture, _PRIMARY) == "strict-cluster-identity-unresolved"

    def test_a_cluster_physical_id_that_is_not_the_stack_blocks(self, tmp_path: Path) -> None:
        ctx, _ = _scenario_context(
            tmp_path,
            inventory=_inventory(
                _region_inventory(_PRIMARY, cluster_name="someone-elses-cluster"),
            ),
        )

        capture = _blocked_capture(ctx)

        assert _blocked_reason(capture, _PRIMARY) == "strict-cluster-name-mismatch"

    def test_a_recorded_tag_key_mismatch_blocks_the_region(self, tmp_path: Path) -> None:
        ctx, _ = _scenario_context(
            tmp_path,
            inventory=_inventory(
                _region_inventory(
                    _PRIMARY,
                    cluster_tag_key="kubernetes.io/cluster/other",
                ),
            ),
        )

        capture = _blocked_capture(ctx)

        assert _blocked_reason(capture, _PRIMARY) == "strict-cluster-tag-mismatch"

    def test_a_duplicated_recorded_volume_blocks_the_region(self, tmp_path: Path) -> None:
        evidence = _region_inventory(_PRIMARY)
        evidence["volume_ids"] = [_volume_id(_PRIMARY), _volume_id(_PRIMARY)]
        ctx, _ = _scenario_context(tmp_path, inventory=_inventory(evidence))

        capture = _blocked_capture(ctx)

        assert _blocked_reason(capture, _PRIMARY) == "recorded-volume-identity-ambiguous"

    def test_a_volume_recorded_for_two_targets_blocks_both(self, tmp_path: Path) -> None:
        shared = _volume_id(_PRIMARY)
        ctx, _ = _scenario_context(
            tmp_path,
            inventory=_inventory(
                _region_inventory(_PRIMARY),
                _region_inventory(
                    _SECONDARY,
                    volumes=[_recorded_volume(_SECONDARY, volume_id=shared)],
                ),
            ),
            regions=(_PRIMARY, _SECONDARY),
        )

        capture = _blocked_capture(ctx)

        assert _blocked_reason(capture, _PRIMARY) == "recorded-volume-identity-ambiguous"
        assert _blocked_reason(capture, _SECONDARY) == "recorded-volume-identity-ambiguous"
        assert volume_targets.strict_volume_cleanup_targets(capture) == {}

    def test_a_recorded_volume_outside_the_target_region_blocks(self, tmp_path: Path) -> None:
        ctx, _ = _scenario_context(
            tmp_path,
            inventory=_inventory(
                _region_inventory(
                    _PRIMARY,
                    volumes=[
                        {
                            **_recorded_volume(_PRIMARY),
                            "region": _SECONDARY,
                        }
                    ],
                ),
            ),
        )

        capture = _blocked_capture(ctx)

        assert _blocked_reason(capture, _PRIMARY) == "recorded-volume-outside-target-region"

    def test_a_recorded_volume_with_a_foreign_tag_key_blocks(self, tmp_path: Path) -> None:
        ctx, _ = _scenario_context(
            tmp_path,
            inventory=_inventory(
                _region_inventory(
                    _PRIMARY,
                    volumes=[
                        _recorded_volume(
                            _PRIMARY,
                            cluster_tag_key=_cluster_tag_key(_SECONDARY),
                        )
                    ],
                ),
            ),
        )

        capture = _blocked_capture(ctx)

        assert _blocked_reason(capture, _PRIMARY) == "recorded-volume-tag-mismatch"

    def test_an_unobserved_recorded_volume_blocks_the_region(self, tmp_path: Path) -> None:
        evidence = _region_inventory(_PRIMARY)
        evidence["volumes"] = []
        ctx, _ = _scenario_context(tmp_path, inventory=_inventory(evidence))

        capture = _blocked_capture(ctx)

        assert _blocked_reason(capture, _PRIMARY) == "recorded-volume-identities-incomplete"

    def test_refused_stack_authorization_blocks_the_region(self, tmp_path: Path) -> None:
        ctx, session = _scenario_context(tmp_path, inventory=_regions_inventory(_PRIMARY))

        capture = _blocked_capture(
            ctx,
            stack_authorization=RuntimeError("Run ownership changed"),
        )

        assert _blocked_reason(capture, _PRIMARY) == "strict-stack-authorization-failed"
        assert "Run ownership changed" in capture["targets"][_stack_name(_PRIMARY)]["reason"]
        session.client.assert_not_called()

    def test_one_blocked_region_does_not_hide_the_other(self, tmp_path: Path) -> None:
        ctx, _ = _scenario_context(
            tmp_path,
            inventory=_inventory(
                _region_inventory(_PRIMARY),
                _region_inventory(_SECONDARY, cluster_name=""),
            ),
            regions=(_PRIMARY, _SECONDARY),
        )

        capture = _blocked_capture(ctx)

        assert capture["targets"][_stack_name(_PRIMARY)]["complete"] is True
        assert _blocked_reason(capture, _SECONDARY) == "strict-cluster-identity-unresolved"


class TestOnlyCompleteTargetsTravelForward:
    """The cleanup helper accessor cannot return an unresolved identity."""

    def test_complete_entries_become_exact_cleanup_targets(self, tmp_path: Path) -> None:
        ctx, _ = _scenario_context(
            tmp_path,
            inventory=_regions_inventory(_PRIMARY, _SECONDARY),
            regions=(_PRIMARY, _SECONDARY),
        )

        targets = volume_targets.strict_volume_cleanup_targets(_capture(ctx))

        assert targets == {
            _stack_name(region): RegionalVolumeTarget(
                stack_name=_stack_name(region),
                stack_id=_stack_id(region),
                region=region,
                cluster_name=_stack_name(region),
                cluster_tag_key=_cluster_tag_key(region),
            )
            for region in (_PRIMARY, _SECONDARY)
        }

    def test_blocked_and_incomplete_entries_are_excluded(self, tmp_path: Path) -> None:
        ctx, _ = _scenario_context(
            tmp_path,
            inventory=_inventory(
                _region_inventory(_PRIMARY),
                _region_inventory(_SECONDARY, cluster_name=""),
            ),
            regions=(_PRIMARY, _SECONDARY),
        )
        capture = _blocked_capture(ctx)
        # A complete-looking entry that lost a field cannot be reconstructed.
        capture["targets"][_stack_name(_PRIMARY)]["stack_id"] = ""

        assert volume_targets.strict_volume_cleanup_targets(capture) == {}

    def test_a_capture_without_targets_returns_nothing(self) -> None:
        assert volume_targets.strict_volume_cleanup_targets({"status": "skipped"}) == {}


class TestTheDestroyActionCapturesBeforeDeleting:
    """Teardown cannot begin from an identity this run could not establish."""

    @staticmethod
    def _destroy_context(tmp_path: Path, inventory: dict[str, Any] | None) -> RunContext:
        ctx, _ = _scenario_context(tmp_path, inventory=inventory)
        ctx.checkpoint.deployment_attempted = True
        ctx.checkpoint.state["target_stack_regions"] = {_stack_name(_PRIMARY): _PRIMARY}
        ctx.checkpoint.state["bootstrap_stacks"] = {}
        return ctx

    @staticmethod
    def _run(ctx: RunContext, calls: list[str]) -> Any:
        def destroy_orchestrated(**kwargs: Any) -> tuple[bool, list[str], list[str]]:
            calls.append("destroy-orchestrated")
            # Teardown completion is gated on one durable ebs-volumes callback
            # per captured target; publish this Region's the way StackManager does.
            kwargs["on_cleanup_complete"](
                "ebs-volumes",
                dict(
                    _completed_volume_outcome(
                        _PRIMARY,
                        request=kwargs["volume_cleanup_request"],
                    )
                ),
            )
            return True, [_stack_name(_PRIMARY)], []

        ctx.stack_manager.destroy_orchestrated.side_effect = destroy_orchestrated
        absence = {
            "all_absent": False,
            "residual": [{"stack_name": _stack_name(_PRIMARY)}],
            "absent": [],
        }
        absent = {"all_absent": True, "residual": [], "absent": []}
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
                side_effect=[absence, absent, absent],
            ),
            patch.object(
                destroy_action,
                "capture_strict_volume_targets",
                side_effect=lambda context: (
                    calls.append("capture"),
                    volume_targets.capture_strict_volume_targets(context),
                )[1],
            ),
            # Independent post-destroy observation and fixture cleanup have their
            # own module and tests; this one is scoped to identity capture.
            patch.object(
                destroy_action,
                "verify_post_destroy_volume_outcomes",
                return_value={"status": "verified", "targets": {}},
            ),
            patch.object(
                destroy_action,
                "cleanup_validation_fixture_volumes",
                return_value={"status": "skipped"},
            ),
            patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH),
            patch.object(ownership_volumes, "_authorize_owned_stack"),
            patch.object(destroy_action.time, "sleep"),
        ):
            return destroy_action.destroy_deployment(ctx)

    def test_identity_is_captured_before_the_orchestrator_deletes(self, tmp_path: Path) -> None:
        ctx = self._destroy_context(tmp_path, _regions_inventory(_PRIMARY))
        calls: list[str] = []

        result = self._run(ctx, calls)

        assert calls == ["capture", "destroy-orchestrated"]
        assert result["volume_targets"]["status"] == "captured"
        assert result["volume_cleanup_targets"] == [_stack_name(_PRIMARY)]

    def test_a_blocked_identity_stops_teardown(self, tmp_path: Path) -> None:
        ctx = self._destroy_context(tmp_path, None)
        calls: list[str] = []

        with pytest.raises(RuntimeError, match="cannot establish an exact volume target identity"):
            self._run(ctx, calls)

        assert calls == ["capture"]
        ctx.stack_manager.destroy_orchestrated.assert_not_called()
        persisted = ctx.checkpoint.state["volume_scenario"][
            volume_targets.STRICT_DESTROY_TARGETS_KEY
        ]
        assert persisted["status"] == "blocked"


def test_the_inventory_key_has_one_owner() -> None:
    """The capture reads the same checkpoint key the inventory action writes."""
    assert (
        volume_inventory.PRE_DESTROY_INVENTORY_KEY
        is ownership_volumes.PRE_DESTROY_INVENTORY_KEY
        is volume_targets.PRE_DESTROY_INVENTORY_KEY
    )
