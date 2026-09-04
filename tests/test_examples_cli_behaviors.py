"""Behavior tests for the example-manifest validation CLI wrapper."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, call

import pytest
from click.testing import CliRunner

from cli.commands import examples_cmd
from cli.commands.examples_cmd import examples
from cli.commands.release_cmd import CONSENT_FLAG


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_static_validation_forwards_selection_and_propagates_harness_exit(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path("/checkout")
    run = Mock(return_value=CompletedProcess([], 7))
    monkeypatch.setattr(examples_cmd, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(examples_cmd.subprocess, "run", run)

    result = runner.invoke(
        examples,
        [
            "validate",
            "--static-only",
            "--examples",
            "simple-job,pipeline-dag",
            "--skip-examples",
            "pipeline-dag",
        ],
    )

    assert result.exit_code == 7
    run.assert_called_once_with(
        [
            sys.executable,
            "-m",
            "scripts.example_job_validation",
            "--static-only",
            "--examples",
            "simple-job,pipeline-dag",
            "--skip-examples",
            "pipeline-dag",
        ],
        cwd=repo_root,
        check=False,
    )


@pytest.mark.parametrize(
    ("extra_args", "message"),
    [
        ([], "--expected-account must be an exact 12-digit"),
        (["--expected-account", "123"], "--expected-account must be an exact 12-digit"),
        (
            ["--expected-account", "12345678901x"],
            "--expected-account must be an exact 12-digit",
        ),
        (
            ["--expected-account", "1234567890123"],
            "--expected-account must be an exact 12-digit",
        ),
        (["--expected-account", "123456789012"], "Refusing to run without explicit consent"),
        (
            ["--expected-account", "123456789012", CONSENT_FLAG, "--actions", " , , "],
            "--actions must name at least one action",
        ),
        (
            ["--expected-account", "123456789012", CONSENT_FLAG, "--actions", "all"],
            "selected actions imply the deploy action",
        ),
        (
            ["--expected-account", "123456789012", CONSENT_FLAG, "--actions", "deploy"],
            "selected actions imply the deploy action",
        ),
        (
            ["--expected-account", "123456789012", CONSENT_FLAG, "--actions", "custom"],
            "selected actions imply the deploy action",
        ),
        (
            [
                "--expected-account",
                "123456789012",
                CONSENT_FLAG,
                "--actions",
                "static",
                "--resume",
            ],
            "--resume replays an exact checkpoint identity",
        ),
        (
            [
                "--expected-account",
                "123456789012",
                CONSENT_FLAG,
                "--actions",
                "static",
                "--resume",
                "--run-id",
                "RID",
            ],
            "--resume replays an exact checkpoint identity",
        ),
        (
            [
                "--expected-account",
                "123456789012",
                CONSENT_FLAG,
                "--actions",
                "static",
                "--resume",
                "--report-dir",
                "/reports/RID",
            ],
            "--resume replays an exact checkpoint identity",
        ),
    ],
)
def test_live_validation_rejects_unsafe_or_incomplete_requests(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
    message: str,
) -> None:
    run_git = Mock()
    run = Mock()
    monkeypatch.setattr(examples_cmd, "_repo_root", lambda: Path("/checkout"))
    monkeypatch.setattr(examples_cmd, "_run_git", run_git)
    monkeypatch.setattr(examples_cmd.subprocess, "run", run)

    result = runner.invoke(examples, ["validate", *extra_args])

    assert result.exit_code == 1
    assert message in result.output
    run_git.assert_not_called()
    run.assert_not_called()


def test_live_validation_builds_exact_explicit_command_and_output(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path("/checkout")
    sha = "0123456789abcdef0123456789abcdef01234567"
    run_git = Mock(side_effect=[sha, "feature/ops"])
    run = Mock(return_value=CompletedProcess([], 9))
    monkeypatch.setattr(examples_cmd, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(examples_cmd, "_run_git", run_git)
    monkeypatch.setattr(examples_cmd.subprocess, "run", run)

    result = runner.invoke(
        examples,
        [
            "validate",
            "--expected-account",
            "123456789012",
            CONSENT_FLAG,
            "--confirm-kms-key-deletion",
            "--examples",
            "simple-job,pipeline-dag",
            "--skip-examples",
            "pipeline-dag",
            "--max-parallel",
            "2",
            "--actions",
            " static,deploy,deploy ",
            "--run-id",
            "RID-1",
            "--report-dir",
            "/reports/RID-1",
            "--resume",
            "--protected-stack",
            "SharedA",
            "--protected-stack",
            "SharedB",
        ],
    )

    assert result.exit_code == 9
    run_git.assert_has_calls(
        [
            call(repo_root, "rev-parse", "HEAD"),
            call(repo_root, "symbolic-ref", "--short", "HEAD"),
        ]
    )
    run.assert_called_once()
    invocation = run.call_args
    assert invocation.args[0] == [
        sys.executable,
        "-m",
        "scripts.example_job_validation",
        "--repo-root",
        "/checkout",
        "--expected-account",
        "123456789012",
        "--expected-sha",
        sha,
        "--expected-branch",
        "feature/ops",
        "--actions",
        "deploy,static",
        "--run-id",
        "RID-1",
        "--report-dir",
        "/reports/RID-1",
        "--checkpoint",
        "/reports/RID-1/checkpoint.json",
        "--examples",
        "simple-job,pipeline-dag",
        "--skip-examples",
        "pipeline-dag",
        "--max-parallel",
        "2",
        "--confirm-kms-key-deletion",
        "--resume",
        "--protected-stack",
        "SharedA",
        "--protected-stack",
        "SharedB",
    ]
    assert invocation.kwargs == {
        "cwd": repo_root,
        "env": dict(os.environ),
        "check": False,
    }
    assert result.output.splitlines() == [
        "run-id:     RID-1",
        f"sha:        {sha}",
        "branch:     feature/ops",
        "account:    123456789012",
        "actions:    deploy,static",
        "examples:   simple-job,pipeline-dag minus pipeline-dag",
        "report-dir: /reports/RID-1",
    ]


def test_live_validation_derives_default_identity_and_omits_optional_flags(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path("/checkout")
    sha = "0123456789abcdef0123456789abcdef01234567"
    home = Path("/home/test")
    run = Mock(return_value=CompletedProcess([], 0))

    class FrozenDateTime:
        @classmethod
        def now(cls, tz: object) -> datetime:
            assert tz is UTC
            return datetime(2026, 9, 3, 12, 34, 56, tzinfo=UTC)

    monkeypatch.setattr(examples_cmd, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(
        examples_cmd,
        "_run_git",
        Mock(side_effect=[sha, "main"]),
    )
    monkeypatch.setattr(examples_cmd, "datetime", FrozenDateTime)
    monkeypatch.setattr(examples_cmd.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(examples_cmd.subprocess, "run", run)

    result = runner.invoke(
        examples,
        [
            "validate",
            "--expected-account",
            "123456789012",
            CONSENT_FLAG,
            "--actions",
            "preflight,baseline,static",
        ],
    )

    assert result.exit_code == 0, result.output
    run_id = "20260903T123456Z-0123456789ab"
    report_dir = home / "gco-example-job-validation-reports" / run_id
    command = run.call_args.args[0]
    assert command[command.index("--actions") + 1] == "baseline,preflight,static"
    assert command[command.index("--run-id") + 1] == run_id
    assert command[command.index("--report-dir") + 1] == str(report_dir)
    assert command[command.index("--checkpoint") + 1] == str(report_dir / "checkpoint.json")
    assert "--examples" not in command
    assert "--skip-examples" not in command
    assert "--max-parallel" not in command
    assert "--confirm-kms-key-deletion" not in command
    assert "--resume" not in command
    assert "--protected-stack" not in command
    assert "examples:   all" in result.output


def test_click_rejects_negative_parallelism_before_launch(runner: CliRunner) -> None:
    result = runner.invoke(examples, ["validate", "--static-only", "--max-parallel", "-1"])

    assert result.exit_code == 2
    assert "-1 is not in the range" in result.output
