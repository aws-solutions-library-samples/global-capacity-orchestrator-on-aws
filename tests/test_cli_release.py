"""Unit tests for ``gco release validate``.

The command is a no-prompt wrapper around the live-validation harness: it
derives commit/branch/run-id/report-dir, demands unambiguous consent flags,
and executes ``python -m scripts.live_release_validation`` as a subprocess.
These tests fake every subprocess (both the ``git`` derivations and the
harness invocation) so they are hermetic: CI checkouts are detached-HEAD,
and nothing here may ever actually launch a validation run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.commands.release_cmd import release


class FakeProcesses:
    """Route git calls to canned answers and capture the harness launch."""

    def __init__(self, tmp_path: Path, harness_returncode: int = 0):
        self.repo_root = tmp_path
        (tmp_path / "cdk.json").write_text("{}")
        (tmp_path / "scripts").mkdir()
        self.harness_calls: list[dict] = []
        self.harness_returncode = harness_returncode

    def __call__(self, command, **kwargs):
        if command[0] == "git":
            outputs = {
                ("rev-parse", "--show-toplevel"): str(self.repo_root),
                ("rev-parse", "HEAD"): "a" * 40,
                ("symbolic-ref", "--short", "HEAD"): "test/floci-integration",
            }
            key = tuple(command[1:])
            if key not in outputs:  # pragma: no cover - defensive
                raise AssertionError(f"unexpected git call: {command}")
            return subprocess.CompletedProcess(command, 0, stdout=outputs[key] + "\n", stderr="")
        self.harness_calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, self.harness_returncode)


@pytest.fixture()
def fake_processes(tmp_path, monkeypatch):
    fake = FakeProcesses(tmp_path)
    monkeypatch.setattr("cli.commands.release_cmd.subprocess.run", fake)
    return fake


def _invoke(*args: str):
    return CliRunner().invoke(release, ["validate", *args])


CONSENT = "--i-understand-this-deploys-and-destroys-infrastructure"
BASE = ("--expected-account", "123456789012", CONSENT, "--confirm-kms-key-deletion")


class TestConsentGates:
    def test_refuses_without_the_consent_flag(self, fake_processes):
        result = _invoke("--expected-account", "123456789012")
        assert result.exit_code != 0
        assert CONSENT in result.output, "the error must name the exact flag to add"
        assert fake_processes.harness_calls == [], "no consent, no subprocess — ever"

    def test_refuses_a_malformed_account(self, fake_processes):
        result = _invoke("--expected-account", "12345", CONSENT)
        assert result.exit_code != 0
        assert "12-digit" in result.output
        assert fake_processes.harness_calls == []

    def test_deploy_requires_kms_confirmation(self, fake_processes):
        result = _invoke("--expected-account", "123456789012", CONSENT)
        assert result.exit_code != 0
        assert "--confirm-kms-key-deletion" in result.output
        assert fake_processes.harness_calls == []

    def test_non_deploy_actions_skip_the_kms_gate(self, fake_processes):
        result = _invoke(
            "--expected-account", "123456789012", CONSENT, "--actions", "preflight,baseline"
        )
        assert result.exit_code == 0, result.output
        assert len(fake_processes.harness_calls) == 1
        assert "--confirm-kms-key-deletion" not in fake_processes.harness_calls[0]["command"]

    def test_workload_actions_imply_deploy_for_the_kms_gate(self, fake_processes):
        """`--actions api` expands to deploy inside the harness; the KMS
        consent gate must fire for it exactly as it does for `deploy`."""
        for actions in ("api", "sqs", "schedulers", "destroy"):
            result = _invoke("--expected-account", "123456789012", CONSENT, "--actions", actions)
            assert result.exit_code != 0, f"--actions {actions} skipped the KMS gate"
            assert "--confirm-kms-key-deletion" in result.output
        assert fake_processes.harness_calls == []

    def test_resume_requires_run_id_and_report_dir(self, fake_processes):
        result = _invoke(*BASE, "--resume")
        assert result.exit_code != 0
        assert "--run-id" in result.output and "--report-dir" in result.output
        assert fake_processes.harness_calls == []

    def test_empty_actions_are_rejected(self, fake_processes):
        result = _invoke(*BASE, "--actions", " , ")
        assert result.exit_code != 0
        assert "at least one action" in result.output


class TestHarnessInvocation:
    def test_derives_identity_and_composes_the_harness_command(self, fake_processes):
        result = _invoke(*BASE)
        assert result.exit_code == 0, result.output
        call = fake_processes.harness_calls[0]
        command = call["command"]
        assert command[1:3] == ["-m", "scripts.live_release_validation"]

        def value_of(flag: str) -> str:
            return command[command.index(flag) + 1]

        assert value_of("--expected-account") == "123456789012"
        assert value_of("--expected-sha") == "a" * 40
        assert value_of("--expected-branch") == "test/floci-integration"
        assert value_of("--actions") == "all"
        assert value_of("--repo-root") == str(fake_processes.repo_root)
        assert "--confirm-kms-key-deletion" in command
        run_id = value_of("--run-id")
        assert run_id.endswith("-" + "a" * 12), "run id must embed the derived SHA prefix"
        report_dir = Path(value_of("--report-dir"))
        assert report_dir.name == run_id
        assert not str(report_dir).startswith(str(fake_processes.repo_root)), (
            "the default report directory must live OUTSIDE the checkout, or the "
            "harness's clean-worktree preflight would fail on its own output"
        )
        assert value_of("--checkpoint") == str(report_dir / "checkpoint.json")
        assert call["cwd"] == fake_processes.repo_root
        # Echoed derivations keep the operator informed without a prompt.
        assert "run-id:" in result.output and "branch:" in result.output

    def test_explicit_overrides_and_protected_stacks_are_forwarded(self, fake_processes, tmp_path):
        report_dir = tmp_path / "reports"
        result = _invoke(
            *BASE,
            "--run-id",
            "run-42",
            "--report-dir",
            str(report_dir),
            "--resume",
            "--protected-stack",
            "SharedAlarms",
            "--protected-stack",
            "OrgTrail",
            "--profile",
            "single-region",
        )
        assert result.exit_code == 0, result.output
        command = fake_processes.harness_calls[0]["command"]
        assert "--resume" in command
        joined = " ".join(command)
        assert "--protected-stack SharedAlarms" in joined
        assert "--protected-stack OrgTrail" in joined
        assert "--profile single-region" in joined
        assert "--run-id run-42" in joined

    def test_harness_exit_code_propagates(self, tmp_path, monkeypatch):
        fake = FakeProcesses(tmp_path, harness_returncode=3)
        monkeypatch.setattr("cli.commands.release_cmd.subprocess.run", fake)
        result = _invoke(*BASE)
        assert result.exit_code == 3, "operators script on the harness's exit code"

    def test_emulator_endpoint_sets_the_verified_env_pair(self, fake_processes):
        result = _invoke(*BASE, "--emulator-endpoint", "http://127.0.0.1:4566/")
        assert result.exit_code == 0, result.output
        env = fake_processes.harness_calls[0]["env"]
        assert env["GCO_LIVE_VALIDATION_EMULATOR"] == "http://127.0.0.1:4566"
        assert env["AWS_ENDPOINT_URL"] == "http://127.0.0.1:4566"
        assert "emulator:" in result.output

    def test_without_emulator_flag_no_emulator_env_leaks(self, fake_processes):
        result = _invoke(*BASE)
        assert result.exit_code == 0
        env = fake_processes.harness_calls[0]["env"]
        assert "GCO_LIVE_VALIDATION_EMULATOR" not in env

    def test_optional_schedulers_flag_is_forwarded_verbatim(self, fake_processes):
        result = _invoke(*BASE, "--optional-schedulers", "yunikorn,slurm")
        assert result.exit_code == 0, result.output
        command = fake_processes.harness_calls[0]["command"]
        index = command.index("--optional-schedulers")
        assert command[index + 1] == "yunikorn,slurm"
        assert "schedulers:" in result.output, "the echo must show the forced enablement"

    def test_optional_schedulers_absent_by_default(self, fake_processes):
        result = _invoke(*BASE)
        assert result.exit_code == 0
        assert "--optional-schedulers" not in fake_processes.harness_calls[0]["command"]


class TestRepoRootValidation:
    def test_refuses_a_non_gco_checkout(self, tmp_path, monkeypatch):
        bare = tmp_path / "bare"
        bare.mkdir()

        def fake_run(command, **kwargs):
            assert command[0] == "git"
            return subprocess.CompletedProcess(command, 0, stdout=str(bare) + "\n", stderr="")

        monkeypatch.setattr("cli.commands.release_cmd.subprocess.run", fake_run)
        result = _invoke(*BASE)
        assert result.exit_code != 0
        assert "not a GCO checkout" in result.output

    def test_git_failure_is_reported_not_raised(self, tmp_path, monkeypatch):
        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 128, stdout="", stderr="fatal: not a repo")

        monkeypatch.setattr("cli.commands.release_cmd.subprocess.run", fake_run)
        result = _invoke(*BASE)
        assert result.exit_code != 0
        assert "fatal: not a repo" in result.output
