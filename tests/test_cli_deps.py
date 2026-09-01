"""Tests for ``gco deps scan`` (cli/commands/deps_cmd.py).

The scanner itself is a shell script exercised by CI and BATS; these tests
pin the *wrapper* contract: GITHUB_OUTPUT plumbing and parsing, report
handling for drift / clean / incomplete scans, the JSON envelope shape the
MCP tool passes through, the nodepools-only fast path (offline + online +
credential skip), and operational-failure surfacing. Every subprocess is
faked — no network, no git, no bash.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from cli.commands import deps_cmd
from cli.commands.deps_cmd import deps

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _pin_repo_root(monkeypatch):
    """Resolve the repo root without shelling out to git."""
    monkeypatch.setattr(deps_cmd, "_repo_root", lambda: REPO_ROOT)


def _invoke(args, output_format: str = "table"):
    runner = CliRunner()
    return runner.invoke(
        deps,
        args,
        obj=SimpleNamespace(output_format=output_format),
        catch_exceptions=False,
    )


class _FakeScan:
    """Stand-in for ``subprocess.run`` that emulates the scanner script."""

    def __init__(
        self,
        *,
        has_drift: bool = True,
        scan_complete: bool = True,
        returncode: int = 0,
        report_body: str = "# Dependency Update Report\n\nfake drift\n",
    ) -> None:
        self.has_drift = has_drift
        self.scan_complete = scan_complete
        self.returncode = returncode
        self.report_body = report_body
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        env = kwargs.get("env") or {}
        self.envs.append(env)
        if self.returncode != 0:
            return subprocess.CompletedProcess(argv, self.returncode, "", "boom")
        output_path = Path(env["GITHUB_OUTPUT"])
        lines = [
            f"has_drift={'true' if self.has_drift else 'false'}",
            f"scan_complete={'true' if self.scan_complete else 'false'}",
        ]
        if self.has_drift:
            report_path = output_path.parent / "report.md"
            report_path.write_text(self.report_body, encoding="utf-8")
            lines.append(f"report_path={report_path}")
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "scan log line\n", "")


class TestFullScan:
    def test_drift_report_reaches_stdout(self, monkeypatch):
        fake = _FakeScan(has_drift=True)
        monkeypatch.setattr(deps_cmd.subprocess, "run", fake)
        result = _invoke(["scan"])
        assert result.exit_code == 0
        assert "fake drift" in result.output
        # The scanner ran through bash against the checked-in script.
        assert fake.calls[0][0] == "bash"
        assert fake.calls[0][1].endswith("dependency-scan.sh")

    def test_github_output_env_is_private_and_step_summary_stripped(self, monkeypatch):
        fake = _FakeScan()
        monkeypatch.setattr(deps_cmd.subprocess, "run", fake)
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", "/tmp/should-not-be-used")
        _invoke(["scan"])
        env = fake.envs[0]
        assert "GITHUB_OUTPUT" in env
        assert env["GITHUB_OUTPUT"] != "/tmp/should-not-be-used"
        assert "GITHUB_STEP_SUMMARY" not in env

    def test_clean_scan_synthesizes_up_to_date_report(self, monkeypatch):
        monkeypatch.setattr(deps_cmd.subprocess, "run", _FakeScan(has_drift=False))
        result = _invoke(["scan"])
        assert result.exit_code == 0
        assert "All dependencies are up to date." in result.output

    def test_incomplete_clean_scan_is_flagged_provisional(self, monkeypatch):
        monkeypatch.setattr(
            deps_cmd.subprocess,
            "run",
            _FakeScan(has_drift=False, scan_complete=False),
        )
        result = _invoke(["scan"])
        assert "provisional" in result.output

    def test_json_envelope_shape(self, monkeypatch):
        monkeypatch.setattr(deps_cmd.subprocess, "run", _FakeScan(has_drift=True))
        result = _invoke(["scan"], output_format="json")
        payload = json.loads(result.output)
        assert payload["has_drift"] is True
        assert payload["scan_complete"] is True
        assert "fake drift" in payload["report_markdown"]
        # JSON mode captures the log instead of streaming it.
        assert payload["log_tail"] == ["scan log line"]

    def test_report_option_writes_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(deps_cmd.subprocess, "run", _FakeScan(has_drift=True))
        target = tmp_path / "report.md"
        result = _invoke(["scan", "--report", str(target)])
        assert result.exit_code == 0
        assert "fake drift" in target.read_text(encoding="utf-8")
        assert "fake drift" not in result.output

    def test_scanner_failure_is_an_error(self, monkeypatch):
        monkeypatch.setattr(deps_cmd.subprocess, "run", _FakeScan(returncode=3))
        runner = CliRunner()
        result = runner.invoke(deps, ["scan"], obj=SimpleNamespace(output_format="table"))
        assert result.exit_code != 0
        assert "exited with status 3" in result.output

    def test_missing_tools_warn_but_do_not_fail(self, monkeypatch):
        monkeypatch.setattr(deps_cmd.subprocess, "run", _FakeScan(has_drift=False))
        monkeypatch.setattr(deps_cmd.shutil, "which", lambda tool: None)
        result = _invoke(["scan"])
        assert result.exit_code == 0
        assert "warning: missing tools" in result.stderr


class _FakeCatalogRuns:
    """Route the nodepools-only subprocess calls to canned answers."""

    def __init__(
        self,
        *,
        offline_rc: int = 0,
        offline_out: str = "## Accelerator catalog and NodePool policy\n\n**Status: PASS**\n",
        online_rc: int = 0,
        online_summary: dict | None = None,
        online_report: str = "## Online EC2 accelerator catalog drift\n\ncurrent\n",
    ) -> None:
        self.offline_rc = offline_rc
        self.offline_out = offline_out
        self.online_rc = online_rc
        self.online_summary = online_summary or {
            "status": "current",
            "drift_count": 0,
            "regions_checked": 17,
        }
        self.online_report = online_report
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if "validate" in argv:
            return subprocess.CompletedProcess(argv, self.offline_rc, self.offline_out, "")
        assert "check-online" in argv
        report_index = argv.index("--report") + 1
        Path(argv[report_index]).write_text(self.online_report, encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, self.online_rc, json.dumps(self.online_summary), ""
        )


class TestNodepoolsOnly:
    def test_offline_pass_online_current(self, monkeypatch):
        fake = _FakeCatalogRuns()
        monkeypatch.setattr(deps_cmd.subprocess, "run", fake)
        monkeypatch.setattr(deps_cmd, "_sts_identity_available", lambda: True)
        result = _invoke(["scan", "--nodepools-only"], output_format="json")
        payload = json.loads(result.output)
        assert payload["nodepools_only"] is True
        assert payload["offline"]["status"] == "pass"
        assert payload["online"]["status"] == "current"
        assert payload["has_drift"] is False
        assert payload["scan_complete"] is True
        # Both invocations went through this interpreter, not bash.
        assert all(argv[0] == sys.executable for argv in fake.calls)

    def test_offline_findings_counted(self, monkeypatch):
        fake = _FakeCatalogRuns(
            offline_rc=1,
            offline_out=(
                "## Accelerator catalog and NodePool policy\n\n"
                "**Status: ACTION REQUIRED**\n\n"
                "### deprecated-family\n\ndetail\n\n### unknown-family\n\ndetail\n"
            ),
        )
        monkeypatch.setattr(deps_cmd.subprocess, "run", fake)
        monkeypatch.setattr(deps_cmd, "_sts_identity_available", lambda: False)
        result = _invoke(["scan", "--nodepools-only"], output_format="json")
        payload = json.loads(result.output)
        assert payload["offline"]["status"] == "findings"
        assert payload["offline"]["finding_count"] == 2
        assert payload["has_drift"] is True

    def test_missing_credentials_skip_online(self, monkeypatch):
        monkeypatch.setattr(deps_cmd.subprocess, "run", _FakeCatalogRuns())
        monkeypatch.setattr(deps_cmd, "_sts_identity_available", lambda: False)
        result = _invoke(["scan", "--nodepools-only"], output_format="json")
        payload = json.loads(result.output)
        assert payload["online"]["status"] == "skipped"
        assert "credentials" in payload["online"]["skip_reason"]
        assert payload["scan_complete"] is False

    def test_online_drift_sets_has_drift(self, monkeypatch):
        fake = _FakeCatalogRuns(
            online_rc=1,
            online_summary={"status": "drift", "drift_count": 3, "regions_checked": 17},
        )
        monkeypatch.setattr(deps_cmd.subprocess, "run", fake)
        monkeypatch.setattr(deps_cmd, "_sts_identity_available", lambda: True)
        result = _invoke(["scan", "--nodepools-only"], output_format="json")
        payload = json.loads(result.output)
        assert payload["online"]["status"] == "drift"
        assert payload["has_drift"] is True

    def test_operational_failure_is_an_error(self, monkeypatch):
        monkeypatch.setattr(deps_cmd.subprocess, "run", _FakeCatalogRuns(offline_rc=2))
        runner = CliRunner()
        result = runner.invoke(
            deps, ["scan", "--nodepools-only"], obj=SimpleNamespace(output_format="table")
        )
        assert result.exit_code != 0
        assert "failed operationally" in result.output


class TestRepoRootGuard:
    def test_outside_checkout_fails_clearly(self, monkeypatch):
        def fake_git(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 128, "", "fatal: not a git repository")

        monkeypatch.undo()  # drop the autouse _repo_root pin for this test
        monkeypatch.setattr(deps_cmd.subprocess, "run", fake_git)
        runner = CliRunner()
        result = runner.invoke(deps, ["scan"], obj=SimpleNamespace(output_format="table"))
        assert result.exit_code != 0
        assert "GCO checkout" in result.output
