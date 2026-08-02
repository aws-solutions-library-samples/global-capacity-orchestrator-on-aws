"""Behavior coverage for ``gco autopilot``.

Everything is driven through :class:`click.testing.CliRunner` with the
install/exec boundaries mocked, so no test installs npm packages, launches
Claude Code, or talks to Bedrock. Two lockstep guards live here as well:
the companion registry must match the "Recommended Companion MCP Servers"
tables in ``gco_mcp/README.md``, and the registry's source formatting must
stay extractable by the deps-scan regexes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli.autopilot import (
    CLAUDE_CODE_PACKAGE,
    CLAUDE_CODE_VERSION,
    COMPANION_MCP_SERVERS,
)
from cli.commands.autopilot_cmd import autopilot
from cli.config import GCOConfig
from gco.bedrock import get_default_bedrock_model_id

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AUTOPILOT_SOURCE = _REPO_ROOT / "cli" / "autopilot.py"
_MCP_README = _REPO_ROOT / "gco_mcp" / "README.md"

#: The two companions removed after they stopped starting against the
#: current ``mcp`` SDK. They must never reappear in generated configs.
_PRUNED_PACKAGES = ("mcp-server-fetch", "mcp-server-calculator")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolated_autopilot_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep tests independent of the developer's shell and home directory."""
    monkeypatch.delenv("GCO_AUTOPILOT_MODEL", raising=False)
    monkeypatch.delenv("GCO_AUTOPILOT_SMALL_FAST_MODEL", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("GCO_AUTOPILOT_CONFIG_DIR", str(tmp_path / "autopilot"))


def _config(**overrides: Any) -> GCOConfig:
    values: dict[str, Any] = {
        "project_name": "test-gco",
        "default_region": "us-east-1",
        "output_format": "table",
    }
    values.update(overrides)
    return GCOConfig(**values)


def _invoke(
    runner: CliRunner,
    args: list[str],
    *,
    config: GCOConfig | None = None,
    input_text: str | None = None,
) -> Any:
    kwargs: dict[str, Any] = {"obj": config or _config()}
    if input_text is not None:
        kwargs["input"] = input_text
    return runner.invoke(autopilot, args, **kwargs)


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


def test_dry_run_defaults_to_the_canonical_bedrock_model(runner: CliRunner) -> None:
    result = _invoke(runner, ["--dry-run"])

    assert result.exit_code == 0
    assert get_default_bedrock_model_id() in result.output
    assert "Dry run only" in result.output


def test_model_flag_overrides_the_default(runner: CliRunner) -> None:
    result = _invoke(runner, ["--dry-run", "-m", "global.anthropic.claude-sonnet-4-6"])

    assert result.exit_code == 0
    assert "global.anthropic.claude-sonnet-4-6" in result.output


def test_model_env_var_overrides_the_default(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GCO_AUTOPILOT_MODEL", "us.anthropic.claude-haiku-4-5-v1:0")

    result = _invoke(runner, ["--dry-run"])

    assert result.exit_code == 0
    assert "us.anthropic.claude-haiku-4-5-v1:0" in result.output


def test_model_flag_beats_the_env_var(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCO_AUTOPILOT_MODEL", "us.anthropic.claude-haiku-4-5-v1:0")

    result = _invoke(runner, ["--dry-run", "-m", "global.anthropic.claude-opus-5"])

    assert result.exit_code == 0
    assert "global.anthropic.claude-opus-5" in result.output
    assert "claude-haiku-4-5" not in result.output


def test_non_claude_model_warns_but_continues(runner: CliRunner) -> None:
    result = _invoke(runner, ["--dry-run", "-m", "us.amazon.nova-pro-v1:0"])

    assert result.exit_code == 0
    assert "does not look like a Claude model" in result.output


# ---------------------------------------------------------------------------
# Generated MCP config
# ---------------------------------------------------------------------------


def test_print_config_emits_the_full_curated_server_set(runner: CliRunner) -> None:
    result = _invoke(runner, ["--print-config"])

    assert result.exit_code == 0
    config = json.loads(result.output)
    servers = config["mcpServers"]
    expected = {"gco"} | {companion.name for companion in COMPANION_MCP_SERVERS}
    assert set(servers) == expected
    for name, entry in servers.items():
        assert isinstance(entry["command"], str) and entry["command"], name
        assert isinstance(entry["args"], list) and all(
            isinstance(arg, str) for arg in entry["args"]
        ), name
        for key, value in entry.get("env", {}).items():
            assert isinstance(key, str) and isinstance(value, str), name


def test_print_config_never_references_the_pruned_packages(runner: CliRunner) -> None:
    result = _invoke(runner, ["--print-config"])

    assert result.exit_code == 0
    for package in _PRUNED_PACKAGES:
        assert package not in result.output


def test_no_companions_leaves_only_the_gco_server(runner: CliRunner) -> None:
    result = _invoke(runner, ["--no-companions", "--print-config"])

    assert result.exit_code == 0
    assert sorted(json.loads(result.output)["mcpServers"]) == ["gco"]


def test_filesystem_server_is_scoped_to_the_launch_directory(runner: CliRunner) -> None:
    result = _invoke(runner, ["--print-config"])

    assert result.exit_code == 0
    config = json.loads(result.output)
    assert str(Path.cwd()) in config["mcpServers"]["filesystem"]["args"]
    assert "{workspace}" not in result.output


def test_gco_server_runs_from_the_source_checkout_in_development(runner: CliRunner) -> None:
    result = _invoke(runner, ["--print-config"])

    assert result.exit_code == 0
    gco_entry = json.loads(result.output)["mcpServers"]["gco"]
    assert gco_entry["args"] == [str(_REPO_ROOT / "gco_mcp" / "run_mcp.py")]


def test_eks_companion_defaults_to_read_only(runner: CliRunner) -> None:
    result = _invoke(runner, ["--print-config"])

    assert result.exit_code == 0
    eks_args = json.loads(result.output)["mcpServers"]["eks"]["args"]
    assert "--allow-write" not in eks_args
    assert "--allow-sensitive-data-access" not in eks_args


# ---------------------------------------------------------------------------
# Install and launch flow
# ---------------------------------------------------------------------------


def test_declining_the_install_exits_with_instructions(runner: CliRunner) -> None:
    with patch("cli.commands.autopilot_cmd.find_claude_binary", return_value=None):
        result = _invoke(runner, [], input_text="n\n")

    assert result.exit_code == 1
    assert f"npm install -g {CLAUDE_CODE_PACKAGE}@{CLAUDE_CODE_VERSION}" in result.output


def test_yes_installs_the_pin_and_execs_claude(runner: CliRunner) -> None:
    exec_calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_exec(argv: list[str], env: dict[str, str]) -> int:
        exec_calls.append((argv, env))
        return 0

    with (
        patch(
            "cli.commands.autopilot_cmd.find_claude_binary",
            side_effect=[None, "/tmp/bin/claude"],
        ),
        patch("cli.commands.autopilot_cmd.install_claude_code", return_value=0) as install,
        patch("cli.commands.autopilot_cmd.exec_claude", side_effect=fake_exec),
    ):
        result = _invoke(runner, ["--yes"])

    assert result.exit_code == 0
    install.assert_called_once_with()
    (argv, _env) = exec_calls[0]
    assert argv[0] == "/tmp/bin/claude"
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1].endswith("mcp.json")


def test_launch_wires_the_bedrock_environment(runner: CliRunner) -> None:
    captured: dict[str, str] = {}

    def fake_exec(argv: list[str], env: dict[str, str]) -> int:
        captured.update(env)
        return 0

    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.exec_claude", side_effect=fake_exec),
    ):
        result = _invoke(runner, ["-m", "global.anthropic.claude-opus-5"])

    assert result.exit_code == 0
    assert captured["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert captured["ANTHROPIC_MODEL"] == "global.anthropic.claude-opus-5"
    assert captured["AWS_REGION"] == "us-east-1"
    assert "ANTHROPIC_SMALL_FAST_MODEL" not in captured


def test_launch_respects_a_caller_supplied_aws_region(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    captured: dict[str, str] = {}

    def fake_exec(argv: list[str], env: dict[str, str]) -> int:
        captured.update(env)
        return 0

    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.exec_claude", side_effect=fake_exec),
    ):
        result = _invoke(runner, [])

    assert result.exit_code == 0
    assert captured["AWS_REGION"] == "eu-west-1"


def test_small_fast_model_flag_reaches_the_environment(runner: CliRunner) -> None:
    captured: dict[str, str] = {}

    def fake_exec(argv: list[str], env: dict[str, str]) -> int:
        captured.update(env)
        return 0

    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.exec_claude", side_effect=fake_exec),
    ):
        result = _invoke(runner, ["--small-fast-model", "us.anthropic.claude-haiku-4-5-v1:0"])

    assert result.exit_code == 0
    assert captured["ANTHROPIC_SMALL_FAST_MODEL"] == "us.anthropic.claude-haiku-4-5-v1:0"


def test_arguments_after_the_separator_pass_through_to_claude(runner: CliRunner) -> None:
    exec_argv: list[str] = []

    def fake_exec(argv: list[str], env: dict[str, str]) -> int:
        exec_argv.extend(argv)
        return 0

    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.exec_claude", side_effect=fake_exec),
    ):
        result = _invoke(runner, ["--", "--continue"])

    assert result.exit_code == 0
    assert exec_argv[-1] == "--continue"


def test_a_failed_install_surfaces_the_npm_exit_code(runner: CliRunner) -> None:
    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value=None),
        patch("cli.commands.autopilot_cmd.install_claude_code", return_value=2),
    ):
        result = _invoke(runner, ["--yes"])

    assert result.exit_code == 1
    assert "exit code 2" in result.output


def test_missing_npm_gets_a_dedicated_remediation(runner: CliRunner) -> None:
    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value=None),
        patch("cli.commands.autopilot_cmd.install_claude_code", return_value=127),
    ):
        result = _invoke(runner, ["--yes"])

    assert result.exit_code == 1
    assert "npm was not found" in result.output


def test_launch_writes_the_session_config_before_exec(runner: CliRunner, tmp_path: Path) -> None:
    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.exec_claude", return_value=0),
    ):
        result = _invoke(runner, [])

    assert result.exit_code == 0
    written = tmp_path / "autopilot" / "mcp.json"
    assert written.is_file()
    assert "gco" in json.loads(written.read_text(encoding="utf-8"))["mcpServers"]


# ---------------------------------------------------------------------------
# Launch-plan edges and helper branches
# ---------------------------------------------------------------------------


def test_outside_a_checkout_the_gco_server_uses_the_pinned_uvx_form(
    runner: CliRunner,
) -> None:
    from cli import __version__

    with patch("cli.autopilot._source_checkout_root", return_value=None):
        result = _invoke(runner, ["--print-config"])

    assert result.exit_code == 0
    gco_entry = json.loads(result.output)["mcpServers"]["gco"]
    assert gco_entry["command"] == "uvx"
    assert f"@v{__version__}" in " ".join(gco_entry["args"])
    assert gco_entry["args"][-1] == "gco-mcp"


def test_dry_run_renders_fast_model_flags_and_claude_path(runner: CliRunner) -> None:
    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.has_resumable_session", return_value=True),
    ):
        result = _invoke(
            runner,
            [
                "--dry-run",
                "--small-fast-model",
                "us.anthropic.claude-haiku-4-5-v1:0",
                "-e",
                "mission",
            ],
        )

    assert result.exit_code == 0
    assert "Fast model:        us.anthropic.claude-haiku-4-5-v1:0" in result.output
    assert "GCO_ENABLE_MISSION=true" in result.output
    assert "Claude Code:       /tmp/bin/claude" in result.output
    assert "found — launch will offer to resume" in result.output


def test_json_dry_run_emits_the_plan_without_the_full_config(runner: CliRunner) -> None:
    result = _invoke(runner, ["--dry-run"], config=_config(output_format="json"))

    assert result.exit_code == 0
    plan = json.loads(result.output)
    assert plan["model"] == get_default_bedrock_model_id()
    assert "mcp_config" not in plan
    assert "resumable_session" in plan


def test_plan_resolution_failures_exit_with_a_clear_error(runner: CliRunner) -> None:
    with patch(
        "cli.commands.autopilot_cmd.resolve_model",
        side_effect=RuntimeError("cdk.json unreadable"),
    ):
        result = _invoke(runner, ["--dry-run"])

    assert result.exit_code == 1
    assert "Failed to resolve the autopilot launch plan" in result.output


def test_claude_missing_from_path_after_install_exits_with_guidance(
    runner: CliRunner,
) -> None:
    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value=None),
        patch("cli.commands.autopilot_cmd.install_claude_code", return_value=0),
    ):
        result = _invoke(runner, ["--yes"])

    assert result.exit_code == 1
    assert "not on PATH" in result.output


def test_unwritable_config_location_exits_with_the_os_error(runner: CliRunner) -> None:
    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch(
            "cli.commands.autopilot_cmd.write_mcp_config",
            side_effect=OSError("read-only file system"),
        ),
    ):
        result = _invoke(runner, [])

    assert result.exit_code == 1
    assert "Failed to prepare the session" in result.output


def test_install_claude_code_reports_127_without_npm() -> None:
    from cli.autopilot import install_claude_code

    with patch("cli.autopilot.shutil.which", return_value=None):
        assert install_claude_code() == 127


def test_install_claude_code_invokes_the_pinned_npm_install() -> None:
    from cli.autopilot import claude_install_command, install_claude_code

    with (
        patch("cli.autopilot.shutil.which", return_value="/usr/bin/npm"),
        patch("cli.autopilot.subprocess.call", return_value=0) as call,
    ):
        assert install_claude_code() == 0
    call.assert_called_once_with(claude_install_command())
    assert claude_install_command()[-1] == f"{CLAUDE_CODE_PACKAGE}@{CLAUDE_CODE_VERSION}"


def test_exec_claude_replaces_the_process_on_posix() -> None:
    from cli.autopilot import exec_claude

    # The mocked execvpe returns (the real one never does), so the
    # unreachable guard must trip — proving the call happened and that
    # no code path silently continues past a failed exec.
    with (
        patch("cli.autopilot.os.execvpe") as execvpe,
        pytest.raises(AssertionError, match="unreachable"),
    ):
        exec_claude(["/tmp/bin/claude", "--continue"], {"A": "1"})
    execvpe.assert_called_once_with(
        "/tmp/bin/claude", ["/tmp/bin/claude", "--continue"], {"A": "1"}
    )


def test_exec_claude_runs_a_child_process_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli.autopilot as autopilot_module

    monkeypatch.setattr(autopilot_module.sys, "platform", "win32")
    with patch("cli.autopilot.subprocess.call", return_value=7) as call:
        assert autopilot_module.exec_claude(["claude"], {"A": "1"}) == 7
    call.assert_called_once_with(["claude"], env={"A": "1"})


def test_source_checkout_detection_requires_all_markers(tmp_path: Path) -> None:
    from cli.autopilot import _source_checkout_root

    # A directory without the checkout markers is not a checkout...
    assert _source_checkout_root(tmp_path) is None
    # ...and the repository this test runs from is one.
    assert _source_checkout_root() == _REPO_ROOT


def test_stdin_interactivity_helper_reflects_the_real_stdin() -> None:
    from cli.commands.autopilot_cmd import _stdin_is_interactive

    assert _stdin_is_interactive() is sys.stdin.isatty()


def test_has_resumable_session_fails_quiet_on_os_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from unittest.mock import MagicMock

    import cli.autopilot as autopilot_module

    exploding = MagicMock()
    exploding.__truediv__ = MagicMock(side_effect=OSError("boom"))
    monkeypatch.setattr(autopilot_module, "_CLAUDE_PROJECTS_DIR", exploding)

    assert autopilot_module.has_resumable_session(tmp_path) is False


# ---------------------------------------------------------------------------
# GCO MCP feature flags
# ---------------------------------------------------------------------------


def test_enable_sets_the_flag_on_the_gco_server_only(runner: CliRunner) -> None:
    result = _invoke(runner, ["-e", "mission", "--print-config"])

    assert result.exit_code == 0
    servers = json.loads(result.output)["mcpServers"]
    assert servers["gco"]["env"] == {"GCO_ENABLE_MISSION": "true"}
    for name, entry in servers.items():
        if name != "gco":
            assert "GCO_ENABLE_MISSION" not in entry.get("env", {}), name


def test_enable_accepts_short_dashed_and_full_forms(runner: CliRunner) -> None:
    result = _invoke(
        runner,
        ["-e", "infrastructure-deploy", "-e", "GCO_ENABLE_CAPACITY_PURCHASE", "--print-config"],
    )

    assert result.exit_code == 0
    env = json.loads(result.output)["mcpServers"]["gco"]["env"]
    assert env["GCO_ENABLE_INFRASTRUCTURE_DEPLOY"] == "true"
    assert env["GCO_ENABLE_CAPACITY_PURCHASE"] == "true"


def test_enable_rejects_unknown_flags_with_the_valid_list(runner: CliRunner) -> None:
    result = _invoke(runner, ["-e", "bogus-flag", "--print-config"])

    assert result.exit_code == 1
    assert "Unknown GCO MCP feature flag" in result.output
    assert "all-tools" in result.output


def test_enable_flag_set_matches_the_mcp_servers_own_registry() -> None:
    """Every flag the server evaluates must be reachable through --enable.

    The CLI carries its own copy of the flag registry — it must not import
    the MCP package at runtime (installs don't guarantee ``gco_mcp`` on the
    CLI's import path, and mypy maps the PEP 420 namespace file under two
    module names when both trees are checked together). This guard is what
    keeps the copies in lockstep: changing a flag in
    ``gco_mcp/feature_flags.py`` without mirroring it in
    ``cli/autopilot.py`` fails here.
    """
    from cli.autopilot import known_gco_mcp_flags, resolve_mcp_flags

    sys.path.insert(0, str(_REPO_ROOT))
    try:
        from gco_mcp.feature_flags import ALL_FLAGS, FLAG_ALL_TOOLS
    finally:
        sys.path.pop(0)

    assert known_gco_mcp_flags() == (FLAG_ALL_TOOLS, *ALL_FLAGS)
    for flag in known_gco_mcp_flags():
        short = flag.removeprefix("GCO_ENABLE_").lower().replace("_", "-")
        assert resolve_mcp_flags((short,)) == {flag: "true"}


def test_mcp_env_sets_arbitrary_server_environment(runner: CliRunner) -> None:
    result = _invoke(runner, ["--mcp-env", "GCO_MCP_TOOL_SEARCH=bm25", "--print-config"])

    assert result.exit_code == 0
    env = json.loads(result.output)["mcpServers"]["gco"]["env"]
    assert env["GCO_MCP_TOOL_SEARCH"] == "bm25"


def test_mcp_env_rejects_malformed_pairs(runner: CliRunner) -> None:
    result = _invoke(runner, ["--mcp-env", "NOEQUALS", "--print-config"])

    assert result.exit_code == 1
    assert "KEY=VALUE" in result.output


def test_mcp_env_wins_over_enable_for_the_same_key(runner: CliRunner) -> None:
    result = _invoke(
        runner,
        ["-e", "mission", "--mcp-env", "GCO_ENABLE_MISSION=false", "--print-config"],
    )

    assert result.exit_code == 0
    env = json.loads(result.output)["mcpServers"]["gco"]["env"]
    assert env["GCO_ENABLE_MISSION"] == "false"


def test_launch_writes_feature_flags_into_the_session_config(
    runner: CliRunner, tmp_path: Path
) -> None:
    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.exec_claude", return_value=0),
    ):
        result = _invoke(runner, ["-e", "all-tools"])

    assert result.exit_code == 0
    written = json.loads((tmp_path / "autopilot" / "mcp.json").read_text(encoding="utf-8"))
    assert written["mcpServers"]["gco"]["env"] == {"GCO_ENABLE_ALL_TOOLS": "true"}


# ---------------------------------------------------------------------------
# Runtime import isolation
# ---------------------------------------------------------------------------


def test_cli_never_requires_the_mcp_package_at_runtime(tmp_path: Path) -> None:
    """The CLI must work without ``gco_mcp`` being importable at all.

    ``cli/`` and ``gco_mcp/`` are separate top-level trees: installs do not
    guarantee the MCP package is on the CLI's import path (the CI smoke run
    proved it), and importing it from ``cli/`` also makes mypy map the
    PEP 420 namespace file under two module names. This guard runs the
    autopilot surfaces in a subprocess with ``gco_mcp`` imports blocked by
    a meta-path hook, so any future runtime dependency fails the unit
    shards immediately instead of only the CLI smoke job.
    """
    script = textwrap.dedent(
        """
        import sys

        class BlockGcoMcp:
            def find_spec(self, name, path=None, target=None):
                if name == "gco_mcp" or name.startswith("gco_mcp."):
                    raise ImportError("the CLI must not import gco_mcp at runtime")
                return None

        sys.meta_path.insert(0, BlockGcoMcp())

        from click.testing import CliRunner
        from cli.main import cli

        for args in (["--help"], ["autopilot", "--print-config"], ["autopilot", "--dry-run"]):
            result = CliRunner().invoke(cli, args, obj=None)
            assert result.exit_code == 0, (args, result.output)
        print("cli-import-isolation-ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "GCO_AUTOPILOT_CONFIG_DIR": str(tmp_path / "autopilot")},
        check=False,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "cli-import-isolation-ok" in result.stdout


# ---------------------------------------------------------------------------
# Plugins and skills/agents imports
# ---------------------------------------------------------------------------


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    skill = tmp_path / "team-skills" / "capacity-planner"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: capacity-planner\ndescription: Plan GPU capacity.\n---\nPlan.\n",
        encoding="utf-8",
    )
    return tmp_path / "team-skills"


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    agents = tmp_path / "my-agents"
    agents.mkdir()
    (agents / "cost-reviewer.md").write_text(
        "---\nname: cost-reviewer\ndescription: Reviews costs.\n---\nReview.\n",
        encoding="utf-8",
    )
    return agents


def _launch_argv(runner: CliRunner, args: list[str]) -> tuple[Any, list[str]]:
    argv_seen: list[str] = []

    def fake_exec(argv: list[str], env: dict[str, str]) -> int:
        argv_seen.extend(argv)
        return 0

    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.exec_claude", side_effect=fake_exec),
    ):
        result = _invoke(runner, args)
    return result, argv_seen


def test_skills_and_agents_are_staged_as_a_session_plugin(
    runner: CliRunner, tmp_path: Path, skills_dir: Path, agents_dir: Path
) -> None:
    result, argv = _launch_argv(runner, ["--skills", str(skills_dir), "--agents", str(agents_dir)])

    assert result.exit_code == 0
    staged = Path(argv[argv.index("--plugin-dir") + 1])
    assert staged == tmp_path / "autopilot" / "gco-autopilot-imports"
    assert (staged / "skills" / "capacity-planner" / "SKILL.md").is_file()
    assert (staged / "agents" / "cost-reviewer.md").is_file()
    manifest = json.loads((staged / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "gco-autopilot-imports"


def test_staged_imports_are_rebuilt_from_scratch_each_launch(
    runner: CliRunner, tmp_path: Path, skills_dir: Path
) -> None:
    stale = tmp_path / "autopilot" / "gco-autopilot-imports" / "skills" / "old-skill"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("stale\n", encoding="utf-8")

    result, argv = _launch_argv(runner, ["--skills", str(skills_dir)])

    assert result.exit_code == 0
    staged = Path(argv[argv.index("--plugin-dir") + 1])
    assert (staged / "skills" / "capacity-planner").is_dir()
    assert not (staged / "skills" / "old-skill").exists()


def test_plugin_flag_passes_dirs_through_to_claude(runner: CliRunner, tmp_path: Path) -> None:
    plugin = tmp_path / "incident-response"
    plugin.mkdir()

    result, argv = _launch_argv(runner, ["--plugin", str(plugin)])

    assert result.exit_code == 0
    assert argv[argv.index("--plugin-dir") + 1] == str(plugin)


def test_plugin_env_var_merges_with_flags(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first-plugin"
    second = tmp_path / "second-plugin"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("GCO_AUTOPILOT_PLUGIN_DIRS", str(second))

    result, argv = _launch_argv(runner, ["--plugin", str(first)])

    assert result.exit_code == 0
    plugin_values = [argv[i + 1] for i, a in enumerate(argv) if a == "--plugin-dir"]
    assert plugin_values == [str(first), str(second)]


def test_missing_plugin_path_fails_the_launch_plan(runner: CliRunner) -> None:
    result = _invoke(runner, ["--dry-run", "--plugin", "/definitely/not/there"])

    assert result.exit_code == 1
    assert "Plugin path does not exist" in result.output


def test_agents_path_that_is_a_file_is_rejected(runner: CliRunner, tmp_path: Path) -> None:
    not_a_dir = tmp_path / "agent.md"
    not_a_dir.write_text("---\nname: x\n---\n", encoding="utf-8")

    result = _invoke(runner, ["--dry-run", "--agents", str(not_a_dir)])

    assert result.exit_code == 1
    assert "--agents path is not a directory" in result.output


def test_empty_skills_directory_is_rejected_as_a_typo(runner: CliRunner, tmp_path: Path) -> None:
    empty = tmp_path / "empty-skills"
    empty.mkdir()

    result = _invoke(runner, ["--dry-run", "--skills", str(empty)])

    assert result.exit_code == 1
    assert "contains no */SKILL.md" in result.output


def test_dry_run_lists_imports_without_staging_them(
    runner: CliRunner, tmp_path: Path, skills_dir: Path, agents_dir: Path
) -> None:
    plugin = tmp_path / "incident-response"
    plugin.mkdir()
    result = _invoke(
        runner,
        [
            "--dry-run",
            "--plugin",
            str(plugin),
            "--skills",
            str(skills_dir),
            "--agents",
            str(agents_dir),
        ],
    )

    assert result.exit_code == 0
    assert f"Plugins:           {plugin}" in result.output
    assert "Imports:" in result.output
    assert "staged as a session plugin" in result.output
    assert not (tmp_path / "autopilot" / "gco-autopilot-imports").exists()


def test_launch_without_imports_carries_no_plugin_flags(runner: CliRunner) -> None:
    result, argv = _launch_argv(runner, [])

    assert result.exit_code == 0
    assert "--plugin-dir" not in argv


# ---------------------------------------------------------------------------
# Session resumption
# ---------------------------------------------------------------------------


def _exec_capture(exec_argv: list[str]) -> Any:
    def fake_exec(argv: list[str], env: dict[str, str]) -> int:
        exec_argv.extend(argv)
        return 0

    return fake_exec


def test_continue_flag_forwards_claudes_continue(runner: CliRunner) -> None:
    exec_argv: list[str] = []
    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.exec_claude", side_effect=_exec_capture(exec_argv)),
    ):
        result = _invoke(runner, ["--continue"])

    assert result.exit_code == 0
    assert "--continue" in exec_argv


def test_resume_with_session_id_forwards_the_id(runner: CliRunner) -> None:
    exec_argv: list[str] = []
    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.exec_claude", side_effect=_exec_capture(exec_argv)),
    ):
        result = _invoke(runner, ["--resume", "abc-123"])

    assert result.exit_code == 0
    index = exec_argv.index("--resume")
    assert exec_argv[index + 1] == "abc-123"


def test_bare_resume_opens_claudes_session_picker(runner: CliRunner) -> None:
    exec_argv: list[str] = []
    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.exec_claude", side_effect=_exec_capture(exec_argv)),
    ):
        result = _invoke(runner, ["--resume"])

    assert result.exit_code == 0
    assert exec_argv[-1] == "--resume"


def test_continue_and_resume_together_is_an_error(runner: CliRunner) -> None:
    result = _invoke(runner, ["--continue", "--resume", "abc"])

    assert result.exit_code == 1
    assert "not both" in result.output


def test_interactive_launch_offers_to_resume_an_existing_session(runner: CliRunner) -> None:
    exec_argv: list[str] = []
    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.exec_claude", side_effect=_exec_capture(exec_argv)),
        patch("cli.commands.autopilot_cmd.has_resumable_session", return_value=True),
        patch("cli.commands.autopilot_cmd._stdin_is_interactive", return_value=True),
    ):
        result = _invoke(runner, [], input_text="y\n")

    assert result.exit_code == 0
    assert "Resume your previous Claude Code session" in result.output
    assert "--continue" in exec_argv


def test_declining_the_resume_prompt_starts_fresh(runner: CliRunner) -> None:
    exec_argv: list[str] = []
    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.exec_claude", side_effect=_exec_capture(exec_argv)),
        patch("cli.commands.autopilot_cmd.has_resumable_session", return_value=True),
        patch("cli.commands.autopilot_cmd._stdin_is_interactive", return_value=True),
    ):
        result = _invoke(runner, [], input_text="n\n")

    assert result.exit_code == 0
    assert "Resume your previous Claude Code session" in result.output
    assert "--continue" not in exec_argv


def test_no_resume_prompt_without_a_tty(runner: CliRunner) -> None:
    exec_argv: list[str] = []
    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.exec_claude", side_effect=_exec_capture(exec_argv)),
        patch("cli.commands.autopilot_cmd.has_resumable_session", return_value=True),
    ):
        result = _invoke(runner, [])

    assert result.exit_code == 0
    assert "Resume your previous" not in result.output
    assert "--continue" not in exec_argv


def test_yes_skips_the_resume_prompt_and_starts_fresh(runner: CliRunner) -> None:
    exec_argv: list[str] = []
    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.exec_claude", side_effect=_exec_capture(exec_argv)),
        patch("cli.commands.autopilot_cmd.has_resumable_session", return_value=True),
        patch("cli.commands.autopilot_cmd._stdin_is_interactive", return_value=True),
    ):
        result = _invoke(runner, ["--yes"])

    assert result.exit_code == 0
    assert "Resume your previous" not in result.output
    assert "--continue" not in exec_argv


def test_passthrough_resume_flags_suppress_the_prompt_and_injection(runner: CliRunner) -> None:
    exec_argv: list[str] = []
    with (
        patch("cli.commands.autopilot_cmd.find_claude_binary", return_value="/tmp/bin/claude"),
        patch("cli.commands.autopilot_cmd.exec_claude", side_effect=_exec_capture(exec_argv)),
        patch("cli.commands.autopilot_cmd.has_resumable_session", return_value=True),
        patch("cli.commands.autopilot_cmd._stdin_is_interactive", return_value=True),
    ):
        result = _invoke(runner, ["--", "--continue"])

    assert result.exit_code == 0
    assert "Resume your previous" not in result.output
    assert exec_argv.count("--continue") == 1


def test_dry_run_reports_session_resumability(runner: CliRunner) -> None:
    with patch("cli.commands.autopilot_cmd.has_resumable_session", return_value=False):
        result = _invoke(runner, ["--dry-run"])

    assert result.exit_code == 0
    assert "none for this workspace" in result.output


def test_has_resumable_session_reads_claudes_transcript_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import cli.autopilot as autopilot_module

    workspace = tmp_path / "work" / "my_repo"
    workspace.mkdir(parents=True)
    projects = tmp_path / "claude-projects"
    monkeypatch.setattr(autopilot_module, "_CLAUDE_PROJECTS_DIR", projects)

    assert autopilot_module.has_resumable_session(workspace) is False

    encoded = autopilot_module._claude_project_dir_name(workspace)
    assert re.fullmatch(r"[A-Za-z0-9-]+", encoded)
    assert "_" not in encoded and "/" not in encoded

    session_dir = projects / encoded
    session_dir.mkdir(parents=True)
    assert autopilot_module.has_resumable_session(workspace) is False

    (session_dir / "some-session.jsonl").write_text("{}\n", encoding="utf-8")
    assert autopilot_module.has_resumable_session(workspace) is True


# ---------------------------------------------------------------------------
# Lockstep guards: pins, README tables, and the deps-scan extraction contract
# ---------------------------------------------------------------------------


def test_claude_code_pin_is_an_exact_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", CLAUDE_CODE_VERSION)


def test_companion_registry_matches_the_mcp_readme_tables() -> None:
    """The code registry and the README recommendation tables move together."""
    readme = _MCP_README.read_text(encoding="utf-8")
    section = readme.split("## Recommended Companion MCP Servers", 1)[1]
    section = section.split("### Example combined config", 1)[0]
    table_packages = set(re.findall(r"^\|\s*\*\*[^|]+\*\*\s*\|\s*\[`([^`]+)`\]", section, re.M))
    registry_packages = {companion.package for companion in COMPANION_MCP_SERVERS}
    assert table_packages == registry_packages, (
        "gco_mcp/README.md companion tables and cli/autopilot.py "
        "COMPANION_MCP_SERVERS have drifted apart"
    )


def test_readme_example_config_parses_and_omits_pruned_packages() -> None:
    readme = _MCP_README.read_text(encoding="utf-8")
    example = readme.split("### Example combined config", 1)[1]
    block = re.search(r"```json\n(.*?)```", example, re.S)
    assert block is not None
    config = json.loads(block.group(1))
    assert "gco" in config["mcpServers"]
    for package in _PRUNED_PACKAGES:
        assert package not in block.group(1)


def test_registry_source_stays_extractable_by_the_deps_scanner() -> None:
    """Locks the file format the scanner regexes depend on.

    ``extract_claude_code_pin`` and ``extract_companion_mcp_packages`` in
    ``.github/scripts/lib_dependency_scan.sh`` parse ``cli/autopilot.py``
    textually (no imports) so BATS can test them against fixtures. This
    guard fails if a refactor breaks that textual contract.
    """
    source = _AUTOPILOT_SOURCE.read_text(encoding="utf-8")

    pin = re.search(r'^CLAUDE_CODE_VERSION = "([^"]+)"$', source, re.M)
    assert pin is not None and pin.group(1) == CLAUDE_CODE_VERSION

    blocks = re.findall(
        r'CompanionServer\(\s*name="([^"]+)",\s*registry="([^"]+)",\s*package="([^"]+)",',
        source,
    )
    assert blocks == [
        (companion.name, companion.registry, companion.package)
        for companion in COMPANION_MCP_SERVERS
    ]
    assert {registry for _, registry, _ in blocks} <= {"npm", "pypi"}
