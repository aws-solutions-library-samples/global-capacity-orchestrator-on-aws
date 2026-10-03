"""Tests for ``gco deps maintain`` (``cli/commands/deps_cmd.py``).

The command's job is plumbing: find the findings (issue, file or live scan),
let ``cli.maintenance`` classify them, show the plan, create the worktree and
session files, and hand the terminal to Claude Code. Everything that touches
the world is faked here: ``gh`` and ``git`` through ``subprocess.run``, the
``claude`` binary and its installer, the exec, the prompt, and the autopilot
config directory (pointed at a temp dir). Every refusal has a test, since a
wrong answer from any of them would either start a session on the wrong
findings or leave a half-made worktree behind.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.commands import deps_cmd
from cli.commands.deps_cmd import deps
from cli.config import GCOConfig
from cli.maintenance import FINDINGS_BEGIN_MARKER, FINDINGS_END_MARKER, FINDINGS_SCHEMA

REPO_ROOT = Path(__file__).resolve().parents[1]
ISSUE_URL = "https://example.invalid/org/repo/issues/434"
REPO_URL = "https://example.invalid/org/repo"


def _findings(*, surfaces: list[dict[str, object]] | None = None, **overrides: object) -> dict:
    document: dict[str, object] = {
        "schema": FINDINGS_SCHEMA,
        "generated_at": "2026-10-01T06:12:00Z",
        "scan_complete": True,
        "has_drift": True,
        "incomplete_reasons": [],
        "surfaces": surfaces
        if surfaces is not None
        else [
            {
                "surface": "Python Packages",
                "urgency": "routine",
                "count": 1,
                "skipped": None,
                "findings": [{"name": "urllib3", "current": "2.7.0", "latest": "2.8.0"}],
            },
            {
                "surface": "EKS Kubernetes Version",
                "urgency": "act soon",
                "count": 1,
                "skipped": None,
                "findings": [
                    {
                        "pin": "kubernetes_version",
                        "current": "1.36",
                        "latest": "1.37",
                        "standard_support_ends": "2027-01-15",
                    }
                ],
            },
            {
                "surface": "EKS Add-ons",
                "urgency": "routine",
                "count": 0,
                "skipped": "No AWS credentials",
                "findings": [],
            },
        ],
    }
    document.update(overrides)
    return document


def _issue_body(document: dict) -> str:
    return (
        "# Dependency Update Report\n\n<details>\n\n"
        f"{FINDINGS_BEGIN_MARKER}\n```json\n{json.dumps(document)}\n```\n{FINDINGS_END_MARKER}\n\n"
        "</details>\n"
    )


class _World:
    """Faked ``gh`` and ``git`` behind ``subprocess.run``, plus what they did."""

    def __init__(self, document: dict | None = None) -> None:
        self.document = document if document is not None else _findings()
        self.calls: list[list[str]] = []
        self.issue_list: object = [
            {"number": 12, "title": "Something else"},
            {"number": 434, "title": "[Automated] Dependency updates available"},
        ]
        self.issue_body: str | None = None
        self.issue_url: str | None = ISSUE_URL
        self.fail: set[str] = set()  # "gh issue list", "git fetch", ...
        self.repo_url_rc = 0

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        key = " ".join(argv[:3])
        if key.startswith("gh issue list"):
            if "gh issue list" in self.fail:
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 401: bad token")
            body = (
                self.issue_list if isinstance(self.issue_list, str) else json.dumps(self.issue_list)
            )
            return subprocess.CompletedProcess(argv, 0, body, "")
        if key.startswith("gh issue view"):
            if "gh issue view" in self.fail:
                return subprocess.CompletedProcess(argv, 1, "", "")
            if self.issue_body == "not json":
                return subprocess.CompletedProcess(argv, 0, "not json", "")
            body = self.issue_body if self.issue_body is not None else _issue_body(self.document)
            payload: dict[str, object] = {"body": body}
            if self.issue_url is not None:
                payload["url"] = self.issue_url
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        if key.startswith("gh repo view"):
            return subprocess.CompletedProcess(argv, self.repo_url_rc, REPO_URL + "\n", "")
        if key.startswith("git rev-parse"):
            return subprocess.CompletedProcess(argv, 0, str(REPO_ROOT / ".git") + "\n", "")
        if key.startswith("git fetch"):
            if "git fetch" in self.fail:
                return subprocess.CompletedProcess(argv, 128, "", "fatal: no remote")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if key.startswith("git worktree"):
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A faked world with Claude Code installed and nothing launched yet."""
    state = _World()
    monkeypatch.setattr(deps_cmd.subprocess, "run", state)
    monkeypatch.setattr(deps_cmd, "_repo_root", lambda: REPO_ROOT)
    monkeypatch.setattr(deps_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(deps_cmd, "find_claude_binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr(deps_cmd, "resolve_model", lambda explicit: (explicit or "model-x", []))
    monkeypatch.setattr(deps_cmd, "effective_aws_region", lambda default: "us-east-1")
    monkeypatch.setattr(deps_cmd, "config_path", lambda: tmp_path / "autopilot" / "mcp.json")
    monkeypatch.setattr(deps_cmd, "build_claude_env", lambda model, region: {"MODEL": model})
    state.launched: list[tuple[list[str], dict[str, str], Path]] = []

    def fake_exec(argv, env):
        state.launched.append((argv, env, Path.cwd()))
        return 0

    monkeypatch.setattr(deps_cmd, "exec_claude", fake_exec)
    monkeypatch.setattr(deps_cmd.os, "chdir", lambda path: None)
    monkeypatch.setattr(deps_cmd, "confirm", lambda *a, **k: True)
    state.tmp_path = tmp_path
    return state


def _invoke(args, output_format: str = "table", **kwargs):
    runner = CliRunner()
    return runner.invoke(
        deps,
        ["maintain", *args],
        obj=GCOConfig(output_format=output_format),
        catch_exceptions=False,
        **kwargs,
    )


# ─── Refusals before any lookup ─────────────────────────────────────────────


def test_machine_output_needs_dry_run(world):
    result = _invoke([], output_format="json")
    assert result.exit_code == 2
    assert "require terminal output" in result.output
    assert world.calls == []


def test_only_one_findings_source(world, tmp_path):
    path = tmp_path / "f.json"
    path.write_text("{}", encoding="utf-8")
    result = _invoke(["--issue", "1", "--findings", str(path)])
    assert result.exit_code == 1
    assert "only one of --issue, --findings and --scan" in result.output


def test_other_engines_are_refused(world, monkeypatch):
    result = _invoke(["--engine", "codex", "--dry-run"])
    assert result.exit_code == 1
    assert "claude-code engine only" in result.output
    monkeypatch.setenv("GCO_AUTOPILOT_ENGINE", "nonsense")
    result = _invoke(["--dry-run"])
    assert result.exit_code == 1
    assert "Unknown autopilot engine" in result.output


# ─── Finding the findings ───────────────────────────────────────────────────


def test_the_rolling_issue_is_found_by_title_and_its_findings_lifted_out(world):
    result = _invoke(["--dry-run", "--branch", "maint/deps-2026-10-03"])
    assert result.exit_code == 0, result.output
    list_call = next(call for call in world.calls if call[:3] == ["gh", "issue", "list"])
    assert "--label" in list_call and "dependencies" in list_call and "automated" in list_call
    view_call = next(call for call in world.calls if call[:3] == ["gh", "issue", "view"])
    assert view_call[3] == "434"
    assert f"Findings:          {ISSUE_URL}" in result.output
    assert "Apply — mechanical (1)" in result.output
    assert "[Python Packages] urllib3: 2.7.0 -> 2.8.0" in result.output
    assert "Report only — judgment (1)" in result.output
    assert "Skipped surfaces (1): EKS Add-ons" in result.output
    assert "Dry run only" in result.output
    assert world.launched == []
    # No git mutation happened; only the common-dir lookup for the default path.
    assert [call[:2] for call in world.calls if call[0] == "git"] == [["git", "rev-parse"]]
    assert "/.worktrees/maint-deps-2026-10-03" in result.output


def test_the_table_plan_counts_incomplete_lookups(world):
    document = _findings(incomplete_reasons=["PyPI lookup failed for x", "npm down"])
    document["surfaces"] = [record for record in document["surfaces"] if not record["skipped"]]
    world.document = document
    result = _invoke(["--dry-run"])
    assert result.exit_code == 0
    assert "Incomplete lookups: 2" in result.output
    assert "Skipped surfaces" not in result.output


def test_an_explicit_issue_number_skips_the_search(world):
    result = _invoke(["--issue", "77", "--dry-run"])
    assert result.exit_code == 0
    assert not any(call[:3] == ["gh", "issue", "list"] for call in world.calls)
    assert any(call[:4] == ["gh", "issue", "view", "77"] for call in world.calls)


def test_issue_lookup_failures_are_named(world):
    world.fail.add("gh issue list")
    result = _invoke(["--dry-run"])
    assert result.exit_code == 1
    assert "gh issue list failed: HTTP 401: bad token" in result.output

    world.fail.clear()
    world.issue_list = "garbage"
    result = _invoke(["--dry-run"])
    assert result.exit_code == 1
    assert "something other than JSON" in result.output

    world.issue_list = [{"number": 1, "title": "unrelated"}]
    result = _invoke(["--dry-run"])
    assert result.exit_code == 1
    assert "no open issue titled" in result.output
    assert "--issue, --findings or --scan" in result.output

    world.issue_list = [{"number": 434, "title": "[Automated] Dependency updates available"}]
    world.issue_body = "not json"
    result = _invoke(["--dry-run"])
    assert result.exit_code == 1
    assert "something other than JSON" in result.output

    world.issue_body = "# a report without the markers\n"
    result = _invoke(["--dry-run"])
    assert result.exit_code == 1
    assert "issue #434: the issue body carries no embedded findings" in result.output

    world.issue_body = None
    world.fail.add("gh issue view")
    result = _invoke(["--dry-run"])
    assert result.exit_code == 1
    assert "gh issue view failed: 1" in result.output


def test_without_gh_the_issue_path_says_so(world, monkeypatch):
    monkeypatch.setattr(deps_cmd.shutil, "which", lambda name: None)
    result = _invoke(["--dry-run"])
    assert result.exit_code == 1
    assert "gh (the GitHub CLI) is required" in result.output


def test_the_issue_url_falls_back_to_its_number(world):
    world.issue_url = None
    result = _invoke(["--dry-run"])
    assert result.exit_code == 0
    assert "Findings:          issue #434" in result.output


def test_a_findings_file_is_read_directly(world, tmp_path):
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(_findings()), encoding="utf-8")
    result = _invoke(["--findings", str(path), "--dry-run"])
    assert result.exit_code == 0
    assert f"Findings:          {path}" in result.output
    assert not any(call[0] == "gh" and call[1] == "issue" for call in world.calls)

    path.write_text("nope", encoding="utf-8")
    result = _invoke(["--findings", str(path), "--dry-run"])
    assert result.exit_code == 1
    assert "not valid JSON" in result.output


def test_a_live_scan_supplies_the_findings(world, monkeypatch):
    envelopes = [{"findings": _findings()}, {"findings": None}]
    monkeypatch.setattr(deps_cmd, "_run_full_scan", lambda repo_root, *, stream: envelopes.pop(0))
    result = _invoke(["--scan", "--dry-run"])
    assert result.exit_code == 0
    assert "Findings:          a live `gco deps scan` run" in result.output
    result = _invoke(["--scan", "--dry-run"])
    assert result.exit_code == 1
    assert "produced no findings document" in result.output


def test_a_document_the_policy_refuses_is_an_error(world):
    world.document = {"schema": "other/1", "surfaces": []}
    result = _invoke(["--dry-run"])
    assert result.exit_code == 1
    assert "findings schema 'other/1' is not" in result.output


# ─── The plan ───────────────────────────────────────────────────────────────


def test_nothing_to_do_ends_before_any_session(world):
    world.document = _findings(surfaces=[])
    result = _invoke([])
    assert result.exit_code == 0
    assert "reports nothing to apply or analyse; there is no maintenance session" in result.output
    assert world.launched == []

    world.document = _findings(surfaces=[], scan_complete=False)
    result = _invoke([])
    assert result.exit_code == 0
    assert "but the scan was incomplete" in result.output

    # Machine output still gets the plan, so a caller can see the empty counts.
    result = _invoke(["--dry-run"], output_format="json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["summary"]["apply"] == {"mechanical": 0, "semantic": 0}


def test_json_dry_run_is_the_plan_with_the_prompt(world):
    result = _invoke(
        ["--dry-run", "--branch", "maint/x", "--act-on", "mechanical", "--companions"],
        output_format="json",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["branch"] == "maint/x"
    assert payload["act_on"] == ["mechanical"]
    assert payload["permission_mode"] == "acceptEdits"
    assert payload["allowed_rules"] > 40 and payload["denied_rules"] > 10
    assert "aws-docs" in payload["mcp_servers"] and "gco" not in payload["mcp_servers"]
    assert payload["summary"] == {
        "apply": {"mechanical": 1},
        "report_only": {"semantic": 0, "judgment": 1},
        "skipped_surfaces": {"EKS Add-ons": "No AWS credentials"},
        "incomplete_reasons": [],
    }
    assert payload["prompt"].startswith("# Dependency maintenance session")
    assert f"Repository: {REPO_URL}" in payload["prompt"]
    assert payload["prompt_path"].endswith("/maintenance/maint-x/prompt.md")
    assert payload["dry_run"] is True


def test_model_warnings_and_errors_surface(world, monkeypatch):
    monkeypatch.setattr(deps_cmd, "resolve_model", lambda explicit: ("m", ["odd model id"]))
    result = _invoke(["--dry-run"])
    assert result.exit_code == 0
    assert "odd model id" in result.output

    def refuse(explicit):
        raise ValueError("--model must be a non-empty Bedrock model id")

    monkeypatch.setattr(deps_cmd, "resolve_model", refuse)
    result = _invoke(["--dry-run", "-m", " "])
    assert result.exit_code == 1
    assert "non-empty Bedrock model id" in result.output


def test_the_repository_url_is_optional(world, monkeypatch):
    world.repo_url_rc = 1
    result = _invoke(["--dry-run"], output_format="json")
    assert "Repository:" not in json.loads(result.stdout)["prompt"]
    monkeypatch.setattr(deps_cmd.shutil, "which", lambda name: None if name == "gh" else "/x")
    world.repo_url_rc = 0
    # Without gh the issue path is refused, so read a file instead.
    path = world.tmp_path / "f.json"
    path.write_text(json.dumps(_findings()), encoding="utf-8")
    result = _invoke(["--findings", str(path), "--dry-run"], output_format="json")
    assert "Repository:" not in json.loads(result.stdout)["prompt"]


# ─── Claude Code ────────────────────────────────────────────────────────────


def test_a_missing_claude_is_offered_installed_then_launched(world, monkeypatch):
    binaries = iter([None, "/opt/claude"])
    monkeypatch.setattr(deps_cmd, "find_claude_binary", lambda: next(binaries))
    installs: list[int] = []
    monkeypatch.setattr(deps_cmd, "install_claude_code", lambda: installs.append(1) or 0)
    result = _invoke(["--branch", "maint/x", "-y"])
    assert result.exit_code == 0, result.output
    assert "Claude Code is not installed" in result.output
    assert installs == [1]
    argv, _env, _cwd = world.launched[0]
    assert argv[0] == "/opt/claude"


def test_declining_the_install_is_an_error(world, monkeypatch):
    monkeypatch.setattr(deps_cmd, "find_claude_binary", lambda: None)
    monkeypatch.setattr(deps_cmd, "confirm", lambda *a, **k: False)
    result = _invoke([])
    assert result.exit_code == 1
    assert "Claude Code is required" in result.output
    assert world.launched == []


@pytest.mark.parametrize(
    ("rc", "message"),
    [(127, "npm was not found on PATH"), (3, "failed with exit code 3")],
)
def test_install_failures_are_named(world, monkeypatch, rc, message):
    monkeypatch.setattr(deps_cmd, "find_claude_binary", lambda: None)
    monkeypatch.setattr(deps_cmd, "install_claude_code", lambda: rc)
    result = _invoke(["-y"])
    assert result.exit_code == 1
    assert message in result.output


def test_an_install_that_leaves_no_binary_on_path_is_an_error(world, monkeypatch):
    monkeypatch.setattr(deps_cmd, "find_claude_binary", lambda: None)
    monkeypatch.setattr(deps_cmd, "install_claude_code", lambda: 0)
    result = _invoke(["-y"])
    assert result.exit_code == 1
    assert "not on PATH" in result.output


# ─── The session ────────────────────────────────────────────────────────────


def test_a_launch_creates_the_worktree_and_session_files_then_execs(world, monkeypatch):
    result = _invoke(["--branch", "maint/deps-2026-10-03", "-y", "--", "--max-turns", "50"])
    assert result.exit_code == 0, result.output
    assert "GCO dependency maintenance — launch plan" in result.output
    assert "Dry run only" not in result.output
    assert "Launching Claude Code on Bedrock (model-x)" in result.output
    worktree = REPO_ROOT / ".worktrees" / "maint-deps-2026-10-03"
    git_calls = [call for call in world.calls if call[0] == "git"]
    assert ["git", "fetch", "--quiet", "origin", "main"] in git_calls
    assert [
        "git",
        "worktree",
        "add",
        "--quiet",
        "-b",
        "maint/deps-2026-10-03",
        str(worktree),
        "origin/main",
    ] in git_calls
    session = world.tmp_path / "autopilot" / "maintenance" / "maint-deps-2026-10-03"
    assert (
        (session / "prompt.md")
        .read_text(encoding="utf-8")
        .startswith("# Dependency maintenance session")
    )
    assert json.loads((session / "findings.json").read_text(encoding="utf-8")) == _findings()
    assert json.loads((session / "mcp.json").read_text(encoding="utf-8")) == {"mcpServers": {}}
    plan = json.loads((session / "plan.json").read_text(encoding="utf-8"))
    assert plan["branch"] == "maint/deps-2026-10-03"
    argv, env, _cwd = world.launched[0]
    assert argv[0] == "/usr/bin/claude"
    assert argv[1:3] == ["--mcp-config", str(session / "mcp.json")]
    assert "Bash(git push -u origin maint/deps-2026-10-03)" in argv
    assert argv[-3:] == ["--max-turns", "50", (session / "prompt.md").read_text(encoding="utf-8")]
    assert env == {"MODEL": "model-x"}


def test_print_mode_and_an_explicit_worktree(world, tmp_path):
    target = tmp_path / "elsewhere"
    result = _invoke(["--print", "--worktree", str(target), "--branch", "maint/x", "-y"])
    assert result.exit_code == 0, result.output
    argv, _env, _cwd = world.launched[0]
    assert argv[-2] == "-p"
    assert any(
        call[:3] == ["git", "worktree", "add"] and str(target) in call for call in world.calls
    )


def test_declining_the_launch_creates_nothing(world, monkeypatch):
    monkeypatch.setattr(deps_cmd, "confirm", lambda *a, **k: False)
    result = _invoke([])
    assert result.exit_code == 0
    assert "Nothing created." in result.output
    assert not any(call[:2] == ["git", "worktree"] for call in world.calls)
    assert world.launched == []


def test_an_existing_worktree_path_is_refused(world, tmp_path):
    target = tmp_path / "taken"
    target.mkdir()
    result = _invoke(["--worktree", str(target), "-y"])
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert world.launched == []


def test_a_failed_fetch_stops_before_the_worktree(world):
    world.fail.add("git fetch")
    result = _invoke(["-y"])
    assert result.exit_code == 1
    assert "git fetch --quiet failed: fatal: no remote" in result.output
    assert not any(call[:2] == ["git", "worktree"] for call in world.calls)


def test_unwritable_session_files_are_an_error(world, monkeypatch):
    def refuse(self, *a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(deps_cmd.Path, "write_text", refuse)
    result = _invoke(["-y"])
    assert result.exit_code == 1
    assert "Failed to write the session files" in result.output
    assert world.launched == []


def test_an_exec_failure_names_the_reinstall(world, monkeypatch):
    def refuse(argv, env):
        raise OSError("exec format error")

    monkeypatch.setattr(deps_cmd, "exec_claude", refuse)
    result = _invoke(["-y"])
    assert result.exit_code == 1
    assert "Failed to launch Claude Code at /usr/bin/claude: exec format error" in result.output


def test_the_exit_code_of_a_returning_exec_is_propagated(world, monkeypatch):
    monkeypatch.setattr(deps_cmd, "exec_claude", lambda argv, env: 7)
    result = _invoke(["-y"])
    assert result.exit_code == 7


def test_the_default_worktree_hangs_off_the_main_checkout(world):
    """``git rev-parse --git-common-dir`` resolves a linked worktree to its repository."""
    assert deps_cmd._main_checkout_root(REPO_ROOT / ".worktrees" / "x") == REPO_ROOT
