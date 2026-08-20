"""Offline contracts for the strict-validation EBS volume-scenario identity.

Covers the pure case/selection contract, the run-settings identity fields, the
resume rejection of a changed case, the fenced checkpoint state section, and the
authorization gate that runs before any destructive volume work. Every AWS
boundary is mocked; nothing here touches live infrastructure.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts.live_release_validation import volume_scenario
from scripts.live_release_validation.models import (
    RunCheckpoint,
    RunContext,
    RunSettings,
    ValidationReport,
)
from scripts.live_release_validation.ownership import volumes as ownership_volumes

_ACCOUNT = "123456789012"
_CALLER_ARN = f"arn:aws:iam::{_ACCOUNT}:role/live-validation"
_BRANCH = "chore/volumes"
_STACK_NAME = "gco-live-us-east-1"
_STACK_ID = f"arn:aws:cloudformation:us-east-1:{_ACCOUNT}:stack/{_STACK_NAME}/abc"


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
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _context(settings: RunSettings, **state: Any) -> RunContext:
    checkpoint = RunCheckpoint(identity=settings.identity())
    checkpoint.state.update({"account_arn": _CALLER_ARN, **state})
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
        cdk_context={},
        deployment_regions=("us-east-1",),
        config=SimpleNamespace(project_name="gco-live", global_region="us-east-1"),
        session=MagicMock(),
        stack_manager=MagicMock(),
        aws_client=MagicMock(),
        job_manager=MagicMock(),
        persist_callback=MagicMock(),
    )


class TestVolumeScenarioContract:
    """`both` selects two runs; only the three exact cases are identities."""

    def test_cases_exclude_the_driver_selection(self) -> None:
        assert volume_scenario.VOLUME_SCENARIO_CASES == ("disabled", "retain-override", "delete")
        assert "both" not in volume_scenario.VOLUME_SCENARIO_CASES
        assert volume_scenario.VOLUME_SCENARIO_SELECTIONS == (
            "disabled",
            "retain-override",
            "delete",
            "both",
        )

    def test_both_is_never_a_checkpoint_identity(self) -> None:
        with pytest.raises(ValueError, match="scenario-driver instruction"):
            volume_scenario.validated_volume_scenario_case("both")

    def test_unknown_case_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown volume scenario case"):
            volume_scenario.validated_volume_scenario_case("retain")

    def test_both_expands_to_two_ordered_live_cases(self) -> None:
        assert volume_scenario.expand_volume_scenario_selection("both") == (
            "retain-override",
            "delete",
        )
        assert volume_scenario.expand_volume_scenario_selection("delete") == ("delete",)

    def test_live_cases_get_disjoint_run_identities(self) -> None:
        retain = volume_scenario.volume_scenario_run_id("run-123", "retain-override")
        delete = volume_scenario.volume_scenario_run_id("run-123", "delete")
        assert retain == "run-123-volumes-retain-override"
        assert delete == "run-123-volumes-delete"
        assert retain != delete

    def test_disabled_case_has_no_lifecycle_identity(self) -> None:
        with pytest.raises(ValueError, match="no isolated lifecycle run identity"):
            volume_scenario.volume_scenario_run_id("run-123", "disabled")

    def test_fixture_cleanup_requires_an_enabled_case(self) -> None:
        with pytest.raises(ValueError, match="requires an enabled volume"):
            volume_scenario.validated_volume_scenario_settings(
                "disabled",
                confirm_fixture_cleanup=True,
            )
        assert (
            volume_scenario.validated_volume_scenario_settings(
                "retain-override",
                confirm_fixture_cleanup=True,
            )
            == "retain-override"
        )


class TestRunSettingsScenarioIdentity:
    """Resume identity carries the case and the fixture-cleanup authorization."""

    def test_default_run_disables_the_scenario(self, tmp_path: Path) -> None:
        identity = _settings(tmp_path).identity()

        assert identity["volume_scenario_case"] == "disabled"
        assert identity["confirm_ebs_fixture_cleanup"] is False

    def test_changed_case_changes_the_resume_identity(self, tmp_path: Path) -> None:
        retain = _settings(tmp_path, volume_scenario_case="retain-override")
        delete = dataclasses.replace(retain, volume_scenario_case="delete")

        assert retain.identity() != delete.identity()
        assert delete.identity()["volume_scenario_case"] == "delete"

    def test_changed_fixture_authorization_changes_the_resume_identity(
        self,
        tmp_path: Path,
    ) -> None:
        fenced = _settings(tmp_path, volume_scenario_case="retain-override")
        authorized = dataclasses.replace(fenced, confirm_ebs_fixture_cleanup=True)

        assert fenced.identity() != authorized.identity()

    def test_driver_selection_is_not_a_valid_setting(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="scenario-driver instruction"):
            _settings(tmp_path, volume_scenario_case="both")

    def test_fixture_authorization_without_a_scenario_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="requires an enabled volume"):
            _settings(tmp_path, confirm_ebs_fixture_cleanup=True)


class TestResumeRejectsChangedScenario:
    """A checkpoint may only resume into the exact case it started with."""

    def _load(self, settings: RunSettings) -> RunCheckpoint:
        from scripts.live_release_validation.runner import LiveValidationRunner

        instance = object.__new__(LiveValidationRunner)
        instance.settings = settings
        return instance._load_checkpoint()

    def test_resume_with_a_changed_case_is_rejected(self, tmp_path: Path) -> None:
        retain = _settings(tmp_path, volume_scenario_case="retain-override")
        self._load(retain)

        resumed = self._load(dataclasses.replace(retain, resume=True))
        assert resumed.identity["volume_scenario_case"] == "retain-override"

        changed = dataclasses.replace(retain, volume_scenario_case="delete", resume=True)
        with pytest.raises(ValueError, match="Checkpoint identity does not match"):
            self._load(changed)


class TestFencedScenarioState:
    """The checkpoint section is created once and pinned to one case."""

    def test_disabled_runs_cannot_open_the_scenario_section(self, tmp_path: Path) -> None:
        ctx = _context(_settings(tmp_path))

        with pytest.raises(RuntimeError, match="volume scenario is disabled"):
            ownership_volumes._volume_scenario_state(ctx)

        assert "volume_scenario" not in ctx.checkpoint.state

    def test_state_records_case_and_authorization_and_persists(self, tmp_path: Path) -> None:
        settings = _settings(
            tmp_path,
            volume_scenario_case="retain-override",
            confirm_ebs_fixture_cleanup=True,
        )
        ctx = _context(settings)

        state = ownership_volumes._volume_scenario_state(ctx)

        assert state == {"case": "retain-override", "fixture_cleanup_authorized": True}
        assert ctx.checkpoint.state["volume_scenario"] is state
        assert ctx.persist_callback.call_count == 1  # type: ignore[attr-defined]

    def test_case_change_against_existing_state_is_rejected(self, tmp_path: Path) -> None:
        ctx = _context(
            _settings(tmp_path, volume_scenario_case="delete"),
            volume_scenario={"case": "retain-override"},
        )

        with pytest.raises(RuntimeError, match="volume scenario case changed"):
            ownership_volumes._volume_scenario_state(ctx)

    def test_malformed_state_section_is_rejected(self, tmp_path: Path) -> None:
        ctx = _context(
            _settings(tmp_path, volume_scenario_case="delete"),
            volume_scenario=["retain-override"],
        )

        with pytest.raises(RuntimeError, match="volume_scenario state must be an object"):
            ownership_volumes._volume_scenario_state(ctx)


class TestVolumeScenarioAuthorization:
    """Identity, account, branch, authorization, and stack ownership come first."""

    def _authorize(self, ctx: RunContext, **kwargs: Any) -> dict[str, Any]:
        with patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH):
            return ownership_volumes._authorize_volume_scenario(
                ctx,
                action="pre-destroy-inventory",
                **kwargs,
            )

    def test_authorized_boundary_records_exact_evidence(self, tmp_path: Path) -> None:
        ctx = _context(_settings(tmp_path, volume_scenario_case="delete"))

        authorization = self._authorize(ctx)

        assert authorization["case"] == "delete"
        assert authorization["run_id"] == "run-123"
        assert authorization["account"] == _ACCOUNT
        assert authorization["branch"] == _BRANCH
        assert authorization["target"] is None
        assert authorization["fixture_cleanup"] is False
        assert authorization["authorized_at"]
        recorded = ctx.checkpoint.state["volume_scenario"]["authorizations"]
        assert recorded["pre-destroy-inventory"] == authorization
        ctx.session.client.assert_not_called()

    def test_identity_mismatch_blocks_the_boundary(self, tmp_path: Path) -> None:
        ctx = _context(_settings(tmp_path, volume_scenario_case="delete"))
        ctx.checkpoint.identity["expected_branch"] = "other"

        with pytest.raises(RuntimeError, match="Checkpoint identity does not match"):
            self._authorize(ctx)

    def test_missing_preflight_identity_blocks_the_boundary(self, tmp_path: Path) -> None:
        ctx = _context(_settings(tmp_path, volume_scenario_case="delete"))
        ctx.checkpoint.state["account_arn"] = ""

        with pytest.raises(RuntimeError, match="requires a checkpointed caller identity"):
            self._authorize(ctx)

    def test_account_mismatch_blocks_the_boundary(self, tmp_path: Path) -> None:
        ctx = _context(_settings(tmp_path, volume_scenario_case="delete"))
        ctx.checkpoint.state["account_arn"] = "arn:aws:iam::210987654321:role/other"

        with pytest.raises(RuntimeError, match="does not match expected account"):
            self._authorize(ctx)

    def test_branch_drift_blocks_the_boundary(self, tmp_path: Path) -> None:
        ctx = _context(_settings(tmp_path, volume_scenario_case="delete"))

        with (
            patch.object(ownership_volumes, "_resolve_branch", return_value="main"),
            pytest.raises(RuntimeError, match="does not match expected branch"),
        ):
            ownership_volumes._authorize_volume_scenario(ctx, action="destroy")

    def test_fixture_cleanup_requires_explicit_authorization(self, tmp_path: Path) -> None:
        ctx = _context(_settings(tmp_path, volume_scenario_case="retain-override"))

        with pytest.raises(RuntimeError, match="--confirm-ebs-fixture-cleanup"):
            self._authorize(ctx, fixture_cleanup=True)

    def test_fixture_cleanup_belongs_to_the_retain_override_case(self, tmp_path: Path) -> None:
        ctx = _context(
            _settings(
                tmp_path,
                volume_scenario_case="delete",
                confirm_ebs_fixture_cleanup=True,
            )
        )

        with pytest.raises(RuntimeError, match="belongs to the retain-override case"):
            self._authorize(ctx, fixture_cleanup=True)

    def test_incomplete_stack_identity_blocks_before_any_aws_call(self, tmp_path: Path) -> None:
        ctx = _context(_settings(tmp_path, volume_scenario_case="delete"))

        with (
            patch.object(ownership_volumes, "_authorize_owned_stack") as authorize_stack,
            pytest.raises(RuntimeError, match="without an exact stack name"),
        ):
            self._authorize(ctx, stack_name="gco-live-us-east-1", region="us-east-1")

        authorize_stack.assert_not_called()
        ctx.session.client.assert_not_called()

    def test_exact_stack_identity_is_authorized_and_recorded(self, tmp_path: Path) -> None:
        ctx = _context(_settings(tmp_path, volume_scenario_case="delete"))
        target = {
            "stack_name": _STACK_NAME,
            "region": "us-east-1",
            "stack_id": _STACK_ID,
        }

        with patch.object(ownership_volumes, "_authorize_owned_stack") as authorize_stack:
            authorization = self._authorize(ctx, **target)

        authorize_stack.assert_called_once_with(
            ctx,
            target["stack_name"],
            target["region"],
            target["stack_id"],
        )
        assert authorization["target"] == target


class TestScenarioCliParsing:
    """The CLI accepts every selection but never invents a run identity."""

    def _args(self, tmp_path: Path, *argv: str) -> tuple[Any, Any]:
        from scripts.live_release_validation import __main__ as live_main

        parser = live_main._build_parser()
        return parser, parser.parse_args(
            [
                "--expected-account",
                _ACCOUNT,
                "--expected-sha",
                "a" * 40,
                "--expected-branch",
                _BRANCH,
                "--repo-root",
                str(tmp_path),
                "--run-id",
                "run-123",
                *argv,
            ]
        )

    def _settings_from(self, tmp_path: Path, *argv: str, **kwargs: Any) -> RunSettings:
        from scripts.live_release_validation import __main__ as live_main

        parser, args = self._args(tmp_path, *argv)
        with patch.object(live_main, "_repository_root", return_value=tmp_path):
            return live_main._settings_from_args(parser, args, **kwargs)

    def test_default_selection_disables_the_scenario(self, tmp_path: Path) -> None:
        settings = self._settings_from(tmp_path)

        assert settings.volume_scenario_case == "disabled"
        assert settings.confirm_ebs_fixture_cleanup is False

    def test_single_case_selection_fences_the_run(self, tmp_path: Path) -> None:
        settings = self._settings_from(tmp_path, "--volume-scenario", "retain-override")

        assert settings.volume_scenario_case == "retain-override"
        assert settings.run_id == "run-123"

    def test_both_requires_the_driver_to_supply_each_case(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            self._settings_from(tmp_path, "--volume-scenario", "both")

    def test_driver_supplied_case_derives_sibling_run_identities(self, tmp_path: Path) -> None:
        from scripts.live_release_validation.volume_scenario import volume_scenario_run_id

        cases = [
            self._settings_from(
                tmp_path,
                "--volume-scenario",
                "both",
                volume_scenario_case=case,
                run_id_override=volume_scenario_run_id("run-123", case),
            )
            for case in ("retain-override", "delete")
        ]

        assert [settings.volume_scenario_case for settings in cases] == [
            "retain-override",
            "delete",
        ]
        assert [settings.run_id for settings in cases] == [
            "run-123-volumes-retain-override",
            "run-123-volumes-delete",
        ]
        assert cases[0].report_dir != cases[1].report_dir
        assert cases[0].checkpoint_path != cases[1].checkpoint_path

    def test_case_outside_the_selection_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            self._settings_from(
                tmp_path,
                "--volume-scenario",
                "retain-override",
                volume_scenario_case="delete",
            )

    def test_fixture_cleanup_flag_requires_a_selected_scenario(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            self._settings_from(tmp_path, "--confirm-ebs-fixture-cleanup")

    def test_fixture_cleanup_flag_is_carried_into_settings(self, tmp_path: Path) -> None:
        settings = self._settings_from(
            tmp_path,
            "--volume-scenario",
            "retain-override",
            "--confirm-ebs-fixture-cleanup",
        )

        assert settings.confirm_ebs_fixture_cleanup is True
        assert settings.identity()["confirm_ebs_fixture_cleanup"] is True
