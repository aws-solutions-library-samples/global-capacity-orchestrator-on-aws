"""Offline contracts for the two-lifecycle EBS volume-scenario driver.

``--volume-scenario both`` must become two *fresh*, separately fenced runs —
``<run-id>-volumes-retain-override`` then ``<run-id>-volumes-delete`` — and this
module pins that boundary without deploying anything:

* planning derives both per-case run identities from one base run ID, keeps the
  driver order (retention evidence first), and hands each case its own private
  report directory, checkpoint, and resume identity;
* every way one case could observe or overwrite the other's state (shared
  checkpoint, shared or nested report directory, duplicated run ID or identity,
  a case fenced to the wrong scenario, settings pre-set to resume, an ignored
  derived run ID) is refused while nothing has run;
* execution is sequential and stops at the first case that does not finish, so
  a second live deployment never stacks on an unresolved teardown; and
* the CLI routes ``both`` to the driver and refuses the options that would pin
  both lifecycles to one location or resume them.

Every lifecycle runner is injected, so no test here starts a live run.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from scripts.live_release_validation import scenario_driver
from scripts.live_release_validation.models import RunSettings
from scripts.live_release_validation.volume_scenario import VolumeScenarioCase

_ACCOUNT = "123456789012"
_SHA = "a" * 40
_BRANCH = "chore/volumes"
_BASE_RUN_ID = "run-123"
_RETAIN_RUN_ID = "run-123-volumes-retain-override"
_DELETE_RUN_ID = "run-123-volumes-delete"


def _settings(
    tmp_path: Path,
    case: VolumeScenarioCase,
    run_id: str,
    **overrides: Any,
) -> RunSettings:
    report_dir = tmp_path / ".live-release-validation" / run_id
    base = RunSettings(
        run_id=run_id,
        repo_root=tmp_path,
        report_dir=report_dir,
        checkpoint_path=report_dir / "checkpoint.json",
        expected_account=_ACCOUNT,
        expected_sha=_SHA,
        expected_branch=_BRANCH,
        profile="configured",
        requested_actions=("all",),
        volume_scenario_case=case,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _factory(tmp_path: Path, **overrides: Any) -> scenario_driver.SettingsFactory:
    """A settings factory that mirrors what the CLI derives for each case."""

    def build(case: VolumeScenarioCase, run_id: str) -> RunSettings:
        return _settings(tmp_path, case, run_id, **overrides)

    return build


def _plan(tmp_path: Path, **overrides: Any) -> tuple[scenario_driver.ScenarioLifecycle, ...]:
    return scenario_driver.plan_volume_scenario_lifecycles(
        "both",
        base_run_id=_BASE_RUN_ID,
        settings_factory=_factory(tmp_path, **overrides),
    )


class TestLifecyclePlanning:
    """`both` expands into two provably isolated, ordered lifecycles."""

    def test_both_plans_the_two_live_cases_in_driver_order(self, tmp_path: Path) -> None:
        lifecycles = _plan(tmp_path)

        assert [lifecycle.case for lifecycle in lifecycles] == ["retain-override", "delete"]
        assert [lifecycle.run_id for lifecycle in lifecycles] == [_RETAIN_RUN_ID, _DELETE_RUN_ID]

    def test_each_case_owns_a_sibling_report_directory_and_checkpoint(self, tmp_path: Path) -> None:
        retain, delete = _plan(tmp_path)

        assert retain.report_dir != delete.report_dir
        assert retain.report_dir.parent == delete.report_dir.parent
        assert retain.checkpoint_path != delete.checkpoint_path
        assert retain.checkpoint_path.parent == retain.report_dir
        assert delete.checkpoint_path.parent == delete.report_dir

    def test_each_case_carries_its_own_fresh_resume_identity(self, tmp_path: Path) -> None:
        retain, delete = _plan(tmp_path)

        assert retain.settings.identity() != delete.settings.identity()
        assert retain.settings.identity()["volume_scenario_case"] == "retain-override"
        assert delete.settings.identity()["volume_scenario_case"] == "delete"
        assert retain.settings.resume is False
        assert delete.settings.resume is False

    def test_planning_creates_no_run_state(self, tmp_path: Path) -> None:
        for lifecycle in _plan(tmp_path):
            assert not lifecycle.report_dir.exists()
            assert not lifecycle.checkpoint_path.exists()

    def test_a_single_case_selection_runs_directly(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="selects a single lifecycle"):
            scenario_driver.plan_volume_scenario_lifecycles(
                "retain-override",
                base_run_id=_BASE_RUN_ID,
                settings_factory=_factory(tmp_path),
            )

    def test_an_empty_base_run_id_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="non-empty base run ID"):
            scenario_driver.plan_volume_scenario_lifecycles(
                "both",
                base_run_id="",
                settings_factory=_factory(tmp_path),
            )

    def test_a_shared_checkpoint_is_refused(self, tmp_path: Path) -> None:
        shared = tmp_path / "shared"

        with pytest.raises(ValueError, match="may resume or mutate"):
            _plan(tmp_path, report_dir=shared, checkpoint_path=shared / "checkpoint.json")

    def test_a_nested_report_directory_is_refused(self, tmp_path: Path) -> None:
        outer = tmp_path / "reports"

        def build(case: VolumeScenarioCase, run_id: str) -> RunSettings:
            report_dir = outer if case == "retain-override" else outer / "inner"
            return _settings(
                tmp_path,
                case,
                run_id,
                report_dir=report_dir,
                checkpoint_path=report_dir / "checkpoint.json",
            )

        with pytest.raises(ValueError, match="sibling private report directories"):
            scenario_driver.plan_volume_scenario_lifecycles(
                "both",
                base_run_id=_BASE_RUN_ID,
                settings_factory=build,
            )

    def test_settings_preset_to_resume_are_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="cannot resume"):
            _plan(tmp_path, resume=True)

    def test_settings_fenced_to_the_wrong_case_are_refused(self, tmp_path: Path) -> None:
        def build(case: VolumeScenarioCase, run_id: str) -> RunSettings:
            del case
            return _settings(tmp_path, "retain-override", run_id)

        with pytest.raises(ValueError, match="fenced to volume scenario case"):
            scenario_driver.plan_volume_scenario_lifecycles(
                "both",
                base_run_id=_BASE_RUN_ID,
                settings_factory=build,
            )

    def test_an_ignored_derived_run_id_is_refused(self, tmp_path: Path) -> None:
        def build(case: VolumeScenarioCase, run_id: str) -> RunSettings:
            del run_id
            return _settings(tmp_path, case, _BASE_RUN_ID)

        with pytest.raises(ValueError, match="derived run ID"):
            scenario_driver.plan_volume_scenario_lifecycles(
                "both",
                base_run_id=_BASE_RUN_ID,
                settings_factory=build,
            )


class TestLifecycleExecution:
    """Cases run one at a time, and a case that does not finish stops the rest."""

    def test_both_cases_run_sequentially_in_order(self, tmp_path: Path) -> None:
        observed: list[RunSettings] = []

        def run(settings: RunSettings) -> int:
            observed.append(settings)
            return 0

        exit_code, results = scenario_driver.run_volume_scenario_lifecycles(
            _plan(tmp_path),
            run_lifecycle=run,
            log=lambda _message: None,
        )

        assert exit_code == 0
        assert [settings.run_id for settings in observed] == [_RETAIN_RUN_ID, _DELETE_RUN_ID]
        assert [settings.volume_scenario_case for settings in observed] == [
            "retain-override",
            "delete",
        ]
        assert {settings.checkpoint_path for settings in observed} == {
            tmp_path / ".live-release-validation" / _RETAIN_RUN_ID / "checkpoint.json",
            tmp_path / ".live-release-validation" / _DELETE_RUN_ID / "checkpoint.json",
        }
        assert [result.status for result in results] == ["completed", "completed"]
        assert [result.exit_code for result in results] == [0, 0]

    def test_a_failed_first_case_stops_the_second_lifecycle(self, tmp_path: Path) -> None:
        observed: list[str] = []

        def run(settings: RunSettings) -> int:
            observed.append(settings.run_id)
            return 1

        exit_code, results = scenario_driver.run_volume_scenario_lifecycles(
            _plan(tmp_path),
            run_lifecycle=run,
            log=lambda _message: None,
        )

        assert exit_code == 1
        assert observed == [_RETAIN_RUN_ID]
        assert [result.status for result in results] == ["failed", "not-started"]
        assert results[1].exit_code is None
        assert "did not complete" in (results[1].error or "")

    def test_a_failed_second_case_returns_its_exit_code(self, tmp_path: Path) -> None:
        exit_code, results = scenario_driver.run_volume_scenario_lifecycles(
            _plan(tmp_path),
            run_lifecycle=lambda settings: 0 if settings.run_id == _RETAIN_RUN_ID else 2,
            log=lambda _message: None,
        )

        assert exit_code == 2
        assert [result.status for result in results] == ["completed", "failed"]
        assert results[1].exit_code == 2

    def test_a_runner_failure_is_recorded_and_stops_the_driver(self, tmp_path: Path) -> None:
        observed: list[str] = []

        def run(settings: RunSettings) -> int:
            observed.append(settings.run_id)
            raise ValueError("Checkpoint already exists")

        exit_code, results = scenario_driver.run_volume_scenario_lifecycles(
            _plan(tmp_path),
            run_lifecycle=run,
            log=lambda _message: None,
        )

        assert exit_code == 1
        assert observed == [_RETAIN_RUN_ID]
        assert results[0].status == "failed"
        assert results[0].error == "ValueError: Checkpoint already exists"
        assert results[1].status == "not-started"

    def test_every_case_is_named_in_the_driver_summary(self, tmp_path: Path) -> None:
        messages: list[str] = []

        scenario_driver.run_volume_scenario_lifecycles(
            _plan(tmp_path),
            run_lifecycle=lambda _settings: 0,
            log=messages.append,
        )

        summary = [message for message in messages if message.startswith("[scenario summary]")]
        assert len(summary) == 2
        assert any(_RETAIN_RUN_ID in message for message in summary)
        assert any(_DELETE_RUN_ID in message for message in summary)

    def test_results_serialize_for_evidence(self, tmp_path: Path) -> None:
        _, results = scenario_driver.run_volume_scenario_lifecycles(
            _plan(tmp_path),
            run_lifecycle=lambda _settings: 0,
            log=lambda _message: None,
        )

        assert results[0].to_dict() == {
            "case": "retain-override",
            "run_id": _RETAIN_RUN_ID,
            "report_dir": str(tmp_path / ".live-release-validation" / _RETAIN_RUN_ID),
            "checkpoint_path": str(
                tmp_path / ".live-release-validation" / _RETAIN_RUN_ID / "checkpoint.json"
            ),
            "status": "completed",
            "exit_code": 0,
            "error": None,
        }

    def test_the_driver_plans_then_runs_every_case(self, tmp_path: Path) -> None:
        observed: list[str] = []

        def run(settings: RunSettings) -> int:
            observed.append(settings.run_id)
            return 0

        exit_code = scenario_driver.run_volume_scenario_driver(
            "both",
            base_run_id=_BASE_RUN_ID,
            settings_factory=_factory(tmp_path),
            run_lifecycle=run,
            log=lambda _message: None,
        )

        assert exit_code == 0
        assert observed == [_RETAIN_RUN_ID, _DELETE_RUN_ID]


class TestScenarioDriverCli:
    """`both` reaches the driver, and nothing may pin the two cases together."""

    def _args(self, tmp_path: Path, *argv: str) -> tuple[Any, Any]:
        from scripts.live_release_validation import __main__ as live_main

        parser = live_main._build_parser()
        return parser, parser.parse_args(
            [
                "--expected-account",
                _ACCOUNT,
                "--expected-sha",
                _SHA,
                "--expected-branch",
                _BRANCH,
                "--repo-root",
                str(tmp_path),
                "--run-id",
                _BASE_RUN_ID,
                "--volume-scenario",
                "both",
                *argv,
            ]
        )

    def test_the_cli_hands_the_driver_one_base_run_id_and_both_cases(self, tmp_path: Path) -> None:
        from scripts.live_release_validation import __main__ as live_main

        captured: dict[str, Any] = {}

        def fake_driver(selection: object, **kwargs: Any) -> int:
            captured["selection"] = selection
            captured["base_run_id"] = kwargs["base_run_id"]
            captured["lifecycles"] = scenario_driver.plan_volume_scenario_lifecycles(
                selection,
                base_run_id=kwargs["base_run_id"],
                settings_factory=kwargs["settings_factory"],
            )
            return 0

        parser, args = self._args(tmp_path)
        with (
            patch.object(live_main, "_repository_root", return_value=tmp_path),
            patch.object(live_main, "run_volume_scenario_driver", side_effect=fake_driver),
        ):
            assert live_main._run_scenario_driver(parser, args) == 0

        assert captured["selection"] == "both"
        assert captured["base_run_id"] == _BASE_RUN_ID
        retain, delete = captured["lifecycles"]
        assert (retain.case, delete.case) == ("retain-override", "delete")
        assert (retain.run_id, delete.run_id) == (_RETAIN_RUN_ID, _DELETE_RUN_ID)
        assert retain.report_dir == tmp_path / ".live-release-validation" / _RETAIN_RUN_ID
        assert delete.report_dir == tmp_path / ".live-release-validation" / _DELETE_RUN_ID
        assert retain.settings.identity() != delete.settings.identity()

    @pytest.mark.parametrize(
        "argv",
        [
            ("--resume",),
            ("--report-dir", "shared-reports"),
            ("--checkpoint", "shared-reports/checkpoint.json"),
        ],
        ids=["resume", "report-dir", "checkpoint"],
    )
    def test_options_that_would_join_the_two_lifecycles_are_rejected(
        self,
        tmp_path: Path,
        argv: tuple[str, ...],
    ) -> None:
        from scripts.live_release_validation import __main__ as live_main

        parser, args = self._args(tmp_path, *argv)

        with pytest.raises(SystemExit):
            live_main._validate_args(parser, args)

    def test_main_routes_both_to_the_driver_without_building_a_runner(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from scripts.live_release_validation import __main__ as live_main

        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "live-validation",
                "--expected-account",
                _ACCOUNT,
                "--expected-sha",
                _SHA,
                "--expected-branch",
                _BRANCH,
                "--repo-root",
                str(tmp_path),
                "--run-id",
                _BASE_RUN_ID,
                "--volume-scenario",
                "both",
            ],
        )

        with (
            patch.object(live_main, "run_volume_scenario_driver", return_value=0) as driver,
            patch.object(live_main, "LiveValidationRunner") as runner,
        ):
            assert live_main.main() == 0

        driver.assert_called_once()
        runner.assert_not_called()

    def test_main_runs_a_single_case_directly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from scripts.live_release_validation import __main__ as live_main

        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "live-validation",
                "--expected-account",
                _ACCOUNT,
                "--expected-sha",
                _SHA,
                "--expected-branch",
                _BRANCH,
                "--repo-root",
                str(tmp_path),
                "--run-id",
                _BASE_RUN_ID,
                "--volume-scenario",
                "retain-override",
            ],
        )

        with (
            patch.object(live_main, "_repository_root", return_value=tmp_path),
            patch.object(live_main, "run_volume_scenario_driver") as driver,
            patch.object(live_main, "LiveValidationRunner") as runner,
        ):
            runner.return_value.run.return_value = 0
            assert live_main.main() == 0

        driver.assert_not_called()
        settings = runner.call_args.args[0]
        assert settings.run_id == _BASE_RUN_ID
        assert settings.volume_scenario_case == "retain-override"
