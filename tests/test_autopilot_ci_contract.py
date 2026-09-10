"""Tests for the shared engine-aware Autopilot CI contract."""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
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
    CODEX_BEDROCK_PROVIDER,
    CODEX_PACKAGE,
    CODEX_VERSION,
    COMPANION_MCP_SERVERS,
    AutopilotEngine,
    build_codex_config_toml,
    build_mcp_config,
    claude_install_command,
    codex_install_command,
)
from gco.bedrock import (  # noqa: E402
    get_default_claude_code_model_id,
    get_default_codex_model_id,
    get_default_codex_reasoning_effort,
)


def _real_config(**kwargs) -> dict:
    return build_mcp_config(Path("/tmp/workspace"), **kwargs)


def _real_codex_config(
    *,
    include_companions: bool = True,
    gco_mcp_env: dict[str, str] | None = None,
    region: str = "us-east-2",
) -> dict:
    mcp_config = build_mcp_config(
        Path("/tmp/workspace"),
        include_companions=include_companions,
        gco_mcp_env=gco_mcp_env,
    )
    rendered = build_codex_config_toml(
        mcp_config,
        model=get_default_codex_model_id(),
        region=region,
        reasoning_effort=get_default_codex_reasoning_effort(),
    )
    return tomllib.loads(rendered)


def _real_plan(
    engine: AutopilotEngine = AutopilotEngine.CLAUDE_CODE,
    *,
    binary: str | None = None,
    **overrides,
) -> dict:
    if engine is AutopilotEngine.CODEX:
        model = get_default_codex_model_id()
        reasoning = get_default_codex_reasoning_effort()
        pin = f"{CODEX_PACKAGE}@{CODEX_VERSION}"
        install_command = " ".join(codex_install_command())
        codex_config = build_codex_config_toml(
            _real_config(),
            model=model,
            region="us-east-2",
            reasoning_effort=reasoning,
        )
        claude_binary = None
        claude_pin = None
        codex_binary = binary
        codex_pin = pin
    else:
        model = get_default_claude_code_model_id()
        reasoning = None
        pin = f"{CLAUDE_CODE_PACKAGE}@{CLAUDE_CODE_VERSION}"
        install_command = " ".join(claude_install_command())
        codex_config = None
        claude_binary = binary
        claude_pin = pin
        codex_binary = None
        codex_pin = None

    plan = {
        "engine": engine.value,
        "engine_binary": binary,
        "engine_pin": pin,
        "model": model,
        "reasoning_effort": reasoning,
        "region": "us-east-2",
        "mcp_servers": contract.expected_servers(),
        "install_command": install_command,
        "claude_binary": claude_binary,
        "claude_code_pin": claude_pin,
        "codex_binary": codex_binary,
        "codex_pin": codex_pin,
        "codex_config": codex_config,
    }
    plan.update(overrides)
    return plan


class TestFactsDeriveFromProduction:
    def test_expected_servers_mirror_the_companion_registry(self) -> None:
        assert contract.expected_servers() == sorted(
            {"gco"} | {companion.name for companion in COMPANION_MCP_SERVERS}
        )
        assert contract.expected_servers(include_companions=False) == ["gco"]

    @pytest.mark.parametrize(
        ("engine", "version", "install_command", "model"),
        [
            (
                AutopilotEngine.CLAUDE_CODE,
                CLAUDE_CODE_VERSION,
                claude_install_command(),
                get_default_claude_code_model_id(),
            ),
            (
                AutopilotEngine.CODEX,
                CODEX_VERSION,
                codex_install_command(),
                get_default_codex_model_id(),
            ),
        ],
    )
    def test_cli_facts_match_production(
        self,
        capsys: pytest.CaptureFixture[str],
        engine: AutopilotEngine,
        version: str,
        install_command: list[str],
        model: str,
    ) -> None:
        suffix = [] if engine is AutopilotEngine.CLAUDE_CODE else ["--engine", engine.value]
        assert contract.main(["pin", *suffix]) == 0
        assert capsys.readouterr().out.strip() == version
        assert contract.main(["install-command", *suffix]) == 0
        assert capsys.readouterr().out.strip() == " ".join(install_command)
        assert contract.main(["default-model", *suffix]) == 0
        assert capsys.readouterr().out.strip() == model

    def test_expected_servers_command(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert contract.main(["expected-servers"]) == 0
        assert capsys.readouterr().out.split() == contract.expected_servers()


class TestVerifyClaudeConfig:
    def test_real_generated_config_is_valid(self) -> None:
        assert contract.verify_config(_real_config()) == []

    def test_no_companions_shape(self) -> None:
        config = _real_config(include_companions=False)
        assert contract.verify_config(config, include_companions=False) == []
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
        assert any("mcp-server-fetch" in problem for problem in contract.verify_config(config))

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


class TestVerifyCodexConfig:
    def test_real_generated_config_is_valid(self) -> None:
        assert contract.verify_codex_config(_real_codex_config(), expected_region="us-east-2") == []

    def test_model_provider_reasoning_and_wire_drift_are_reported(self) -> None:
        config = _real_codex_config()
        config["model"] = "global.openai.other"
        config["model_provider"] = "other"
        config["model_reasoning_effort"] = "low"
        config["model_providers"][CODEX_BEDROCK_PROVIDER]["wire_api"] = "chat"
        problems = "\n".join(contract.verify_codex_config(config))
        assert "shipped default" in problems
        assert "provider" in problems
        assert "reasoning effort" in problems
        assert "wire API" in problems

    def test_server_contract_and_feature_env_are_shared_with_claude(self) -> None:
        expect = {"GCO_ENABLE_ALL_TOOLS": "true"}
        config = _real_codex_config(gco_mcp_env=expect)
        gco_args = list(config["mcp_servers"]["gco"]["args"])
        assert (
            contract.verify_codex_config(
                config,
                expect_gco_env=expect,
                gco_args=gco_args,
            )
            == []
        )
        config["mcp_servers"]["gco"]["enabled"] = False
        assert any("must be enabled" in item for item in contract.verify_codex_config(config))

    def test_no_companions_shape(self) -> None:
        config = _real_codex_config(include_companions=False)
        assert contract.verify_codex_config(config, include_companions=False) == []
        assert contract.verify_codex_config(config) != []


class TestVerifyPlan:
    @pytest.mark.parametrize("engine", list(AutopilotEngine))
    def test_valid_plan_with_absent_and_present_binary(self, engine: AutopilotEngine) -> None:
        keyword = "codex_binary" if engine is AutopilotEngine.CODEX else "claude_binary"
        assert contract.verify_plan(_real_plan(engine), engine=engine, **{keyword: "absent"}) == []
        assert (
            contract.verify_plan(
                _real_plan(engine, binary=f"/usr/local/bin/{engine.value}"),
                engine=engine,
                **{keyword: "present"},
            )
            == []
        )

    @pytest.mark.parametrize("engine", list(AutopilotEngine))
    def test_model_and_pin_drift_are_reported(self, engine: AutopilotEngine) -> None:
        problems = contract.verify_plan(
            _real_plan(engine, model="provider.nonexistent", engine_pin="pkg@0.0.0"),
            engine=engine,
        )
        assert any("shipped default" in problem for problem in problems)
        assert any("engine pin" in problem for problem in problems)

    def test_codex_reasoning_and_generated_config_are_verified(self) -> None:
        plan = _real_plan(AutopilotEngine.CODEX, reasoning_effort="low")
        plan["codex_config"] = "not = [valid"
        problems = contract.verify_plan(plan, engine=AutopilotEngine.CODEX)
        assert any("reasoning" in problem for problem in problems)
        assert any("invalid TOML" in problem for problem in problems)

    def test_engine_and_binary_state_mismatches_are_reported(self) -> None:
        assert (
            contract.verify_plan(_real_plan(binary="/usr/bin/claude"), claude_binary="absent") != []
        )
        assert (
            contract.verify_plan(
                _real_plan(AutopilotEngine.CODEX),
                engine=AutopilotEngine.CODEX,
                codex_binary="present",
            )
            != []
        )
        assert (
            contract.verify_plan(
                _real_plan(AutopilotEngine.CODEX), engine=AutopilotEngine.CLAUDE_CODE
            )
            != []
        )


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

    def test_verify_codex_config_exit_codes(self, tmp_path: Path) -> None:
        good = tmp_path / "codex.toml"
        good.write_text(
            build_codex_config_toml(
                _real_config(),
                model=get_default_codex_model_id(),
                region="us-east-2",
                reasoning_effort=get_default_codex_reasoning_effort(),
            ),
            encoding="utf-8",
        )
        assert contract.main(["verify-codex-config", str(good), "--region", "us-east-2"]) == 0
        assert contract.main(["verify-codex-config", str(good), "--region", "eu-west-1"]) == 1

    @pytest.mark.parametrize("engine", list(AutopilotEngine))
    def test_verify_plan_exit_codes(self, tmp_path: Path, engine: AutopilotEngine) -> None:
        plan = tmp_path / f"{engine.value}.json"
        plan.write_text(json.dumps(_real_plan(engine)), encoding="utf-8")
        binary_option = "--codex-binary" if engine is AutopilotEngine.CODEX else "--claude-binary"
        assert (
            contract.main(
                [
                    "verify-plan",
                    str(plan),
                    "--engine",
                    engine.value,
                    binary_option,
                    "absent",
                ]
            )
            == 0
        )
        assert (
            contract.main(
                [
                    "verify-plan",
                    str(plan),
                    "--engine",
                    engine.value,
                    binary_option,
                    "present",
                ]
            )
            == 1
        )

    def test_malformed_env_pair_is_rejected(self, tmp_path: Path) -> None:
        config = tmp_path / "config.json"
        config.write_text(json.dumps(_real_config()), encoding="utf-8")
        with pytest.raises(SystemExit):
            contract.main(["verify-config", str(config), "--expect-gco-env", "NOEQUALS"])


class TestConfigShapeRejections:
    """Malformed documents must produce problems, never a vacuous pass.

    ``verify_config`` is handed a document that a *generator* produced, so the
    realistic failure is not a hand-typo but a generator change that alters the
    shape. Every one of these returns a problem list rather than raising, because
    the CI step prints all problems at once instead of stopping at the first.
    """

    def test_a_non_mapping_server_section_is_a_single_clear_problem(self) -> None:
        """Claude's JSON: the whole point is one clear message, not a cascade."""
        problems = contract.verify_config(
            {"mcpServers": ["not", "a", "mapping"]},
            include_companions=False,
            expect_gco_env=None,
            gco_args=None,
        )

        assert problems == ["config carries no mcpServers mapping"]

    def test_the_shared_mapping_check_also_guards_its_own_input(self) -> None:
        """Both engines funnel into ``_verify_server_mapping``.

        ``verify_config`` rejects a bad ``mcpServers`` before delegating, but the
        Codex path reaches the shared checker with a differently-shaped document,
        so the guard has to exist on both sides of the call.
        """
        problems = contract._verify_server_mapping(
            ["not", "a", "mapping"],
            include_companions=False,
            expect_gco_env=None,
            gco_args=None,
        )

        assert problems == ["config carries no MCP server mapping"]

    def test_a_non_mapping_entry_is_reported_and_skipped(self) -> None:
        """One bad entry must not stop the remaining entries being checked."""
        config = _real_config()
        config["mcpServers"]["gco"] = "not-a-mapping"

        problems = contract.verify_config(
            config, include_companions=False, expect_gco_env=None, gco_args=None
        )

        assert any("entry must be a mapping" in problem for problem in problems)

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            pytest.param("command", "", "command must be a non-empty string", id="empty-command"),
            pytest.param("command", 7, "command must be a non-empty string", id="numeric-command"),
            pytest.param("args", "uvx", "args must be a list of strings", id="args-not-a-list"),
            pytest.param("args", [1, 2], "args must be a list of strings", id="args-not-strings"),
        ],
    )
    def test_launch_recipe_fields_are_shape_checked(
        self, field: str, value: object, expected: str
    ) -> None:
        config = _real_config()
        config["mcpServers"]["gco"][field] = value

        problems = contract.verify_config(
            config, include_companions=False, expect_gco_env=None, gco_args=None
        )

        assert any(expected in problem for problem in problems)

    def test_a_non_mapping_gco_entry_does_not_crash_the_env_check(self) -> None:
        """The env assertion must still report, rather than raise, on bad input."""
        config = _real_config()
        config["mcpServers"]["gco"] = "not-a-mapping"

        problems = contract.verify_config(
            config,
            include_companions=False,
            expect_gco_env={"GCO_PROFILE": "ci"},
            gco_args=None,
        )

        assert any("gco env 'GCO_PROFILE'" in problem for problem in problems)

    def test_a_non_mapping_env_is_treated_as_absent(self) -> None:
        config = _real_config()
        config["mcpServers"]["gco"]["env"] = ["GCO_PROFILE=ci"]

        problems = contract.verify_config(
            config,
            include_companions=False,
            expect_gco_env={"GCO_PROFILE": "ci"},
            gco_args=None,
        )

        assert any("gco env 'GCO_PROFILE'" in problem for problem in problems)


class TestEnvPairParsing:
    """``--expect-gco-env KEY=VALUE`` parsing."""

    def test_a_well_formed_pair_splits_on_the_first_equals(self) -> None:
        """Values legitimately contain '=' (base64, query strings), so only the
        first separator may be treated as the delimiter."""
        assert contract._parse_env_pair("GCO_TOKEN=a=b=c") == ("GCO_TOKEN", "a=b=c")

    @pytest.mark.parametrize("pair", ("noequals", "=novalue"))
    def test_a_malformed_pair_is_rejected_by_argparse(self, pair: str) -> None:
        with pytest.raises(contract.argparse.ArgumentTypeError, match="expects KEY=VALUE"):
            contract._parse_env_pair(pair)


def test_the_module_puts_the_repository_on_sys_path_when_it_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The script is run by CI as a file, not imported as part of the package.

    ``python3 .github/scripts/autopilot_ci_contract.py`` puts *that directory* on
    sys.path, not the repository root, so the script inserts the root itself
    before importing ``cli.autopilot``. Under pytest the root is already there
    and the guard never fires, so it is exercised by re-executing the module with
    the root removed -- otherwise this line would be permanently unverified and
    a regression would only surface as a CI-only ImportError.
    """
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != str(_PROJECT_ROOT)])
    spec = importlib.util.spec_from_file_location("_autopilot_contract_pathless", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    assert str(_PROJECT_ROOT) in sys.path, "the module did not restore the repository root"
    assert module.expected_servers(include_companions=False), "the module failed to import cli"


class TestCodexConfigRejections:
    """Each Codex-specific invariant, violated one at a time.

    These start from the real generated TOML and break exactly one thing, so a
    problem message can be attributed to the line that produced it rather than
    to a cascade from a malformed document.
    """

    def test_update_checks_left_enabled_are_reported(self) -> None:
        """A generated config must never let Codex phone home for updates.

        The pin is the contract; an update check that succeeded would replace
        the pinned binary mid-session.
        """
        config = _real_codex_config()
        config["check_for_update_on_startup"] = True

        problems = contract.verify_codex_config(config, expected_region="us-east-2")

        assert "Codex update checks must be disabled in the generated config" in problems

    def test_a_missing_provider_aws_table_is_reported(self) -> None:
        config = _real_codex_config()
        del config["model_providers"][CODEX_BEDROCK_PROVIDER]["aws"]

        problems = contract.verify_codex_config(config, expected_region="us-east-2")

        assert any(".aws provider table" in problem for problem in problems)

    @pytest.mark.parametrize("region", ("", None, 7), ids=("empty", "absent", "not-a-string"))
    def test_an_unusable_provider_region_is_reported(self, region: object) -> None:
        config = _real_codex_config()
        aws = config["model_providers"][CODEX_BEDROCK_PROVIDER]["aws"]
        if region is None:
            del aws["region"]
        else:
            aws["region"] = region

        problems = contract.verify_codex_config(config, expected_region="us-east-2")

        assert any("region must be non-empty" in problem for problem in problems)

    def test_a_disabled_or_slow_mcp_server_is_reported(self) -> None:
        """Codex races MCP init against the first turn; both knobs matter."""
        config = _real_codex_config()
        gco = config["mcp_servers"]["gco"]
        gco["enabled"] = False
        gco["startup_timeout_sec"] = 1

        problems = contract.verify_codex_config(config, expected_region="us-east-2")

        assert "gco: Codex MCP server must be enabled" in problems
        assert any("startup timeout must be" in problem for problem in problems)

    def test_a_non_mapping_server_table_reports_once_and_skips_per_server_checks(
        self,
    ) -> None:
        """The per-server loop must not run over a non-mapping."""
        config = _real_codex_config()
        config["mcp_servers"] = "not-a-table"

        problems = contract.verify_codex_config(config, expected_region="us-east-2")

        assert problems == ["config carries no MCP server mapping"]


class TestPlanRejections:
    """The remaining ``verify_plan`` branches."""

    def test_selected_binary_field_disagreeing_with_engine_binary_is_reported(self) -> None:
        """``engine_binary`` and the per-engine field are two views of one fact."""
        plan = _real_plan(AutopilotEngine.CODEX, binary="/usr/bin/codex")
        plan["codex_binary"] = "/somewhere/else/codex"

        problems = contract.verify_plan(plan, engine=AutopilotEngine.CODEX, codex_binary="present")

        assert "plan codex_binary disagrees with engine_binary" in problems

    def test_a_non_text_codex_config_in_the_plan_is_reported(self) -> None:
        plan = _real_plan(AutopilotEngine.CODEX)
        plan["codex_config"] = {"already": "parsed"}

        problems = contract.verify_plan(plan, engine=AutopilotEngine.CODEX, codex_binary="absent")

        assert "Codex plan config must be TOML text when present" in problems

    def test_invalid_toml_in_the_plan_is_reported(self) -> None:
        plan = _real_plan(AutopilotEngine.CODEX)
        plan["codex_config"] = "model = [unterminated"

        problems = contract.verify_plan(plan, engine=AutopilotEngine.CODEX, codex_binary="absent")

        assert any("invalid TOML" in problem for problem in problems)

    def test_a_plan_listing_the_wrong_servers_is_reported(self) -> None:
        plan = _real_plan(AutopilotEngine.CLAUDE_CODE)
        plan["mcp_servers"] = ["gco", "some-server-nobody-asked-for"]

        problems = contract.verify_plan(plan, claude_binary="absent")

        assert any(problem.startswith("plan servers") for problem in problems)

    def test_a_codex_plan_without_the_rendered_config_is_still_valid(self) -> None:
        """The public JSON formatter omits the large config on purpose.

        ``-o json --dry-run`` prints the plan without the generated TOML, so its
        absence is the normal CI case and must not be reported as a problem.
        """
        plan = _real_plan(AutopilotEngine.CODEX)
        plan.pop("codex_config", None)

        assert contract.verify_plan(plan, engine=AutopilotEngine.CODEX, codex_binary="absent") == []
