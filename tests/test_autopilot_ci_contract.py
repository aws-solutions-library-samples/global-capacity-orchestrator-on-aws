"""Tests for the shared autopilot CI contract (.github/scripts).

The contract is the ONE place the three autopilot-exercising CI jobs
(unit:cli:autopilot, the dev-container matrix step, and
integration:autopilot:boot) get their expected server set, config-shape
checks, and plan checks from. Facts must derive from the production
modules — a literal drifting here would defeat the single-source scheme —
so these tests hold the contract in lockstep with ``cli.autopilot`` and
``gco.bedrock`` and pin the failure modes each verifier must catch.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _PROJECT_ROOT / ".github" / "scripts" / "autopilot_ci_contract.py"

_spec = importlib.util.spec_from_file_location("autopilot_ci_contract", _SCRIPT)
assert _spec is not None and _spec.loader is not None
contract = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("autopilot_ci_contract", contract)
_spec.loader.exec_module(contract)

from cli.autopilot import (  # noqa: E402
    CLAUDE_CODE_PACKAGE,
    CLAUDE_CODE_VERSION,
    COMPANION_MCP_SERVERS,
    build_mcp_config,
    claude_install_command,
)
from gco.bedrock import get_default_claude_code_model_id  # noqa: E402


def _real_config(**kwargs) -> dict:
    return build_mcp_config(Path("/tmp/workspace"), **kwargs)


def _real_plan(**overrides) -> dict:
    plan = {
        "model": get_default_claude_code_model_id(),
        "mcp_servers": contract.expected_servers(),
        "claude_code_pin": f"{CLAUDE_CODE_PACKAGE}@{CLAUDE_CODE_VERSION}",
        "claude_binary": None,
    }
    plan.update(overrides)
    return plan


class TestFactsDeriveFromProduction:
    def test_expected_servers_mirror_the_companion_registry(self) -> None:
        assert contract.expected_servers() == sorted(
            {"gco"} | {c.name for c in COMPANION_MCP_SERVERS}
        )
        assert contract.expected_servers(include_companions=False) == ["gco"]

    def test_cli_facts_match_production(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert contract.main(["pin"]) == 0
        assert capsys.readouterr().out.strip() == CLAUDE_CODE_VERSION
        assert contract.main(["install-command"]) == 0
        assert capsys.readouterr().out.strip() == " ".join(claude_install_command())
        assert contract.main(["default-model"]) == 0
        assert capsys.readouterr().out.strip() == get_default_claude_code_model_id()
        assert contract.main(["expected-servers"]) == 0
        assert capsys.readouterr().out.split() == contract.expected_servers()


class TestVerifyConfig:
    def test_real_generated_config_is_valid(self) -> None:
        assert contract.verify_config(_real_config()) == []

    def test_no_companions_shape(self) -> None:
        config = _real_config(include_companions=False)
        assert contract.verify_config(config, include_companions=False) == []
        # And the full expectation flags the missing companions.
        assert contract.verify_config(config) != []

    def test_missing_and_unexpected_servers_are_reported(self) -> None:
        config = _real_config()
        del config["mcpServers"]["aws-docs"]
        config["mcpServers"]["rogue"] = {"command": "npx", "args": []}
        problems = "\n".join(contract.verify_config(config))
        assert "missing=['aws-docs']" in problems
        assert "unexpected=['rogue']" in problems

    def test_pruned_packages_may_not_reappear(self) -> None:
        config = _real_config()
        config["mcpServers"]["deepwiki"]["args"] = ["-y", "mcp-server-fetch"]
        problems = contract.verify_config(config)
        assert any("mcp-server-fetch" in problem for problem in problems)

    def test_entry_shape_violations_are_reported(self) -> None:
        config = _real_config()
        config["mcpServers"]["memory"]["command"] = ""
        config["mcpServers"]["shell"]["args"] = [1, 2]
        problems = "\n".join(contract.verify_config(config))
        assert "memory: command" in problems
        assert "shell: args" in problems

    def test_gco_env_expectation_and_leak_detection(self) -> None:
        expect = {"GCO_ENABLE_ALL_TOOLS": "true"}
        config = _real_config(gco_mcp_env=dict(expect))
        assert contract.verify_config(config, expect_gco_env=expect) == []

        missing = _real_config()
        assert any(
            "GCO_ENABLE_ALL_TOOLS" in problem
            for problem in contract.verify_config(missing, expect_gco_env=expect)
        )

        leaked = _real_config(gco_mcp_env=dict(expect))
        leaked["mcpServers"]["memory"]["env"] = dict(expect)
        assert any(
            "leaked onto memory" in problem
            for problem in contract.verify_config(leaked, expect_gco_env=expect)
        )

    def test_gco_args_exact_match(self) -> None:
        config = _real_config()
        args = list(config["mcpServers"]["gco"]["args"])
        assert contract.verify_config(config, gco_args=args) == []
        assert contract.verify_config(config, gco_args=["/somewhere/else.py"]) != []

    def test_config_without_servers_mapping(self) -> None:
        assert contract.verify_config({}) == ["config carries no mcpServers mapping"]


class TestVerifyPlan:
    def test_valid_plan(self) -> None:
        assert contract.verify_plan(_real_plan(), claude_binary="absent") == []
        assert (
            contract.verify_plan(
                _real_plan(claude_binary="/usr/local/bin/claude"), claude_binary="present"
            )
            == []
        )

    def test_model_drift_is_reported(self) -> None:
        problems = contract.verify_plan(_real_plan(model="anthropic.claude-nonexistent"))
        assert any("shipped default" in problem for problem in problems)

    def test_pin_drift_is_reported(self) -> None:
        problems = contract.verify_plan(_real_plan(claude_code_pin="pkg@0.0.0"))
        assert any("pin" in problem for problem in problems)

    def test_binary_state_mismatches_are_reported(self) -> None:
        assert (
            contract.verify_plan(
                _real_plan(claude_binary="/usr/bin/claude"), claude_binary="absent"
            )
            != []
        )
        assert contract.verify_plan(_real_plan(), claude_binary="present") != []


class TestCommandLine:
    def test_verify_config_exit_codes(self, tmp_path: Path) -> None:
        good = tmp_path / "good.json"
        good.write_text(json.dumps(_real_config()), encoding="utf-8")
        assert contract.main(["verify-config", str(good)]) == 0

        bad = tmp_path / "bad.json"
        broken = _real_config()
        del broken["mcpServers"]["gco"]
        bad.write_text(json.dumps(broken), encoding="utf-8")
        assert contract.main(["verify-config", str(bad)]) == 1

    def test_verify_plan_exit_codes(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps(_real_plan()), encoding="utf-8")
        assert contract.main(["verify-plan", str(plan), "--claude-binary", "absent"]) == 0
        assert contract.main(["verify-plan", str(plan), "--claude-binary", "present"]) == 1

    def test_malformed_env_pair_is_rejected(self, tmp_path: Path) -> None:
        config = tmp_path / "config.json"
        config.write_text(json.dumps(_real_config()), encoding="utf-8")
        with pytest.raises(SystemExit):
            contract.main(["verify-config", str(config), "--expect-gco-env", "NOEQUALS"])
