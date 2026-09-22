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
    OPENCODE_BEDROCK_PROVIDER,
    OPENCODE_MCP_TIMEOUT_MS,
    OPENCODE_PACKAGE,
    OPENCODE_VERSION,
    AutopilotEngine,
    build_codex_config_toml,
    build_mcp_config,
    build_opencode_config,
    claude_install_command,
    codex_install_command,
    opencode_install_command,
)
from gco.bedrock import (  # noqa: E402
    get_default_claude_code_model_id,
    get_default_codex_model_id,
    get_default_codex_reasoning_effort,
    get_default_opencode_model_id,
)

#: The engine-specific plan fields and CLI options ``verify_plan`` reads.
_BINARY_KEYWORDS = {
    AutopilotEngine.CLAUDE_CODE: "claude_binary",
    AutopilotEngine.CODEX: "codex_binary",
    AutopilotEngine.OPENCODE: "opencode_binary",
}
_BINARY_OPTIONS = {
    AutopilotEngine.CLAUDE_CODE: "--claude-binary",
    AutopilotEngine.CODEX: "--codex-binary",
    AutopilotEngine.OPENCODE: "--opencode-binary",
}


@pytest.fixture(autouse=True)
def _no_ambient_aws_credential_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render OpenCode's provider block the way an empty environment does.

    ``build_opencode_config`` pins ``profile: default`` only when none of
    these variables is set, so a developer's exported ``AWS_PROFILE`` would
    otherwise change the shape every OpenCode assertion below relies on.
    """
    for name in (
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    ):
        monkeypatch.delenv(name, raising=False)


def _real_config(**kwargs) -> dict:
    return build_mcp_config(Path("/tmp/workspace"), **kwargs)


def _real_opencode_config(
    *,
    include_companions: bool = True,
    gco_mcp_env: dict[str, str] | None = None,
    region: str = "us-east-2",
    small_model: str | None = None,
) -> dict:
    mcp_config = build_mcp_config(
        Path("/tmp/workspace"),
        include_companions=include_companions,
        gco_mcp_env=gco_mcp_env,
    )
    return build_opencode_config(
        mcp_config,
        model=get_default_opencode_model_id(),
        region=region,
        small_model=small_model,
    )


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
    codex_config = None
    opencode_config = None
    reasoning = None
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
    elif engine is AutopilotEngine.OPENCODE:
        model = get_default_opencode_model_id()
        pin = f"{OPENCODE_PACKAGE}@{OPENCODE_VERSION}"
        install_command = " ".join(opencode_install_command())
        opencode_config = _real_opencode_config()
    else:
        model = get_default_claude_code_model_id()
        pin = f"{CLAUDE_CODE_PACKAGE}@{CLAUDE_CODE_VERSION}"
        install_command = " ".join(claude_install_command())

    plan = {
        "engine": engine.value,
        "engine_binary": binary,
        "engine_pin": pin,
        "model": model,
        "small_fast_model": None,
        "reasoning_effort": reasoning,
        "region": "us-east-2",
        "mcp_servers": contract.expected_servers(),
        "install_command": install_command,
        "claude_binary": binary if engine is AutopilotEngine.CLAUDE_CODE else None,
        "claude_code_pin": pin if engine is AutopilotEngine.CLAUDE_CODE else None,
        "codex_binary": binary if engine is AutopilotEngine.CODEX else None,
        "codex_pin": pin if engine is AutopilotEngine.CODEX else None,
        "opencode_binary": binary if engine is AutopilotEngine.OPENCODE else None,
        "opencode_pin": pin if engine is AutopilotEngine.OPENCODE else None,
        "codex_config": codex_config,
        "opencode_config": opencode_config,
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
            (
                AutopilotEngine.OPENCODE,
                OPENCODE_VERSION,
                opencode_install_command(),
                get_default_opencode_model_id(),
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


class TestVerifyOpenCodeConfig:
    def test_real_generated_config_is_valid(self) -> None:
        config = _real_opencode_config()
        assert (
            contract.verify_opencode_config(
                config,
                expected_region="us-east-2",
                expected_profile="default",
            )
            == []
        )

    def test_model_small_model_update_share_and_permission_drift_are_reported(self) -> None:
        config = _real_opencode_config()
        config["model"] = f"{OPENCODE_BEDROCK_PROVIDER}/global.other.model"
        config["small_model"] = "anthropic/claude-haiku"
        config["autoupdate"] = True
        config["share"] = "auto"
        config["permission"]["bash"] = "allow"
        del config["permission"]["edit"]

        problems = "\n".join(contract.verify_opencode_config(config))

        assert "shipped default" in problems
        assert "small_model" in problems
        assert "auto-update must be disabled" in problems
        assert "share policy" in problems
        assert "permission 'bash' must be 'ask', got 'allow'" in problems
        assert "permission 'edit' must be 'ask', got None" in problems

    def test_a_missing_permission_mapping_is_a_single_problem(self) -> None:
        config = _real_opencode_config()
        config["permission"] = "ask"

        problems = contract.verify_opencode_config(config)

        assert "OpenCode config carries no permission mapping" in problems
        assert not any("permission 'bash'" in problem for problem in problems)

    def test_the_small_model_pin_follows_the_fast_model_option(self) -> None:
        config = _real_opencode_config(small_model="us.anthropic.claude-haiku-4-5-v1:0")

        assert (
            contract.verify_opencode_config(
                config,
                expected_small_model="us.anthropic.claude-haiku-4-5-v1:0",
            )
            == []
        )
        assert any("small_model" in problem for problem in contract.verify_opencode_config(config))
        del config["provider"][OPENCODE_BEDROCK_PROVIDER]["models"][
            "us.anthropic.claude-haiku-4-5-v1:0"
        ]
        assert any(
            "does not declare model 'us.anthropic.claude-haiku-4-5-v1:0'" in problem
            for problem in contract.verify_opencode_config(
                config,
                expected_small_model="us.anthropic.claude-haiku-4-5-v1:0",
            )
        )

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda config: config.__setitem__("provider", "amazon"), id="no-table"),
            pytest.param(
                lambda config: config.__setitem__("provider", {"anthropic": {}}),
                id="other-provider-only",
            ),
            pytest.param(
                lambda config: config["provider"].__setitem__(OPENCODE_BEDROCK_PROVIDER, []),
                id="provider-not-a-mapping",
            ),
        ],
    )
    def test_a_missing_bedrock_provider_table_is_reported(self, mutate) -> None:
        config = _real_opencode_config()
        mutate(config)

        problems = contract.verify_opencode_config(config)

        assert f"OpenCode config carries no provider.{OPENCODE_BEDROCK_PROVIDER} table" in problems

    def test_missing_provider_options_and_models_are_reported(self) -> None:
        config = _real_opencode_config()
        provider = config["provider"][OPENCODE_BEDROCK_PROVIDER]
        provider["options"] = ["region"]
        provider["models"] = "global.moonshotai.kimi-k3"

        problems = contract.verify_opencode_config(config)

        assert f"OpenCode provider.{OPENCODE_BEDROCK_PROVIDER} carries no options" in problems
        assert "OpenCode provider carries no models declaration" in problems

    @pytest.mark.parametrize("region", ["", None, 7], ids=("empty", "absent", "not-a-string"))
    def test_an_unusable_provider_region_is_reported(self, region: object) -> None:
        config = _real_opencode_config()
        options = config["provider"][OPENCODE_BEDROCK_PROVIDER]["options"]
        if region is None:
            del options["region"]
        else:
            options["region"] = region

        problems = contract.verify_opencode_config(config, expected_region="us-east-2")

        assert any("region must be non-empty" in problem for problem in problems)

    def test_a_region_mismatch_is_reported(self) -> None:
        config = _real_opencode_config(region="eu-west-1")

        problems = contract.verify_opencode_config(config, expected_region="us-east-2")

        assert "OpenCode provider region 'eu-west-1' != expected 'us-east-2'" in problems

    def test_the_profile_pin_is_checked_both_ways(self) -> None:
        pinned = _real_opencode_config()
        assert pinned["provider"][OPENCODE_BEDROCK_PROVIDER]["options"]["profile"] == "default"
        assert contract.verify_opencode_config(pinned, expected_profile="default") == []
        assert any(
            "profile 'default' != expected 'ci'" in problem
            for problem in contract.verify_opencode_config(pinned, expected_profile="ci")
        )
        assert any(
            "must be omitted" in problem
            for problem in contract.verify_opencode_config(pinned, forbid_profile=True)
        )

        unpinned = _real_opencode_config()
        del unpinned["provider"][OPENCODE_BEDROCK_PROVIDER]["options"]["profile"]
        assert contract.verify_opencode_config(unpinned, forbid_profile=True) == []
        assert contract.verify_opencode_config(unpinned) == []
        assert any(
            "profile None != expected 'default'" in problem
            for problem in contract.verify_opencode_config(unpinned, expected_profile="default")
        )

    @pytest.mark.parametrize("profile", ["", 3], ids=("empty", "not-a-string"))
    def test_an_unusable_pinned_profile_is_reported_without_expectations(
        self, profile: object
    ) -> None:
        config = _real_opencode_config()
        config["provider"][OPENCODE_BEDROCK_PROVIDER]["options"]["profile"] = profile

        problems = contract.verify_opencode_config(config)

        assert any("profile must be non-empty" in problem for problem in problems)

    def test_an_undeclared_session_model_is_reported(self) -> None:
        config = _real_opencode_config()
        models = config["provider"][OPENCODE_BEDROCK_PROVIDER]["models"]
        models[get_default_opencode_model_id()] = "declared-as-text"

        problems = contract.verify_opencode_config(config)

        assert any("does not declare model" in problem for problem in problems)

    def test_server_contract_and_feature_env_are_shared_with_claude(self) -> None:
        expect = {"GCO_ENABLE_ALL_TOOLS": "true"}
        config = _real_opencode_config(gco_mcp_env=expect)
        gco_args = list(config["mcp"]["gco"]["command"][1:])
        assert (
            contract.verify_opencode_config(
                config,
                expect_gco_env=expect,
                gco_args=gco_args,
            )
            == []
        )
        assert any(
            "GCO_ENABLE_ALL_TOOLS" in problem
            for problem in contract.verify_opencode_config(
                _real_opencode_config(),
                expect_gco_env=expect,
            )
        )
        assert contract.verify_opencode_config(config, gco_args=["/somewhere/else.py"]) != []

    def test_local_type_enabled_and_timeout_are_enforced_per_server(self) -> None:
        config = _real_opencode_config()
        gco = config["mcp"]["gco"]
        gco["type"] = "remote"
        gco["enabled"] = False
        gco["timeout"] = 5_000

        problems = contract.verify_opencode_config(config)

        assert "gco: OpenCode MCP server type must be 'local'" in problems
        assert "gco: OpenCode MCP server must be enabled" in problems
        assert f"gco: OpenCode MCP timeout must be {OPENCODE_MCP_TIMEOUT_MS} ms" in problems

    def test_no_companions_shape(self) -> None:
        config = _real_opencode_config(include_companions=False)
        assert contract.verify_opencode_config(config, include_companions=False) == []
        assert contract.verify_opencode_config(config) != []

    def test_a_non_mapping_server_table_reports_once_and_skips_per_server_checks(
        self,
    ) -> None:
        config = _real_opencode_config()
        config["mcp"] = "not-a-table"

        problems = contract.verify_opencode_config(config, expected_profile="default")

        assert problems == ["config carries no MCP server mapping"]

    def test_a_non_mapping_entry_is_reported_once_and_skipped(self) -> None:
        config = _real_opencode_config(include_companions=False)
        config["mcp"]["gco"] = "not-a-mapping"

        problems = contract.verify_opencode_config(config, include_companions=False)

        assert any("entry must be a mapping" in problem for problem in problems)
        assert not any("type must be 'local'" in problem for problem in problems)

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param("uvx gco-mcp", "args must be a list of strings", id="string-command"),
            pytest.param([], "command must be a non-empty string", id="empty-argv"),
            pytest.param(["uvx", 7], "command must be a non-empty string", id="non-string-argv"),
            pytest.param(None, "command must be a non-empty string", id="absent"),
        ],
    )
    def test_a_malformed_local_command_is_projected_onto_the_shared_checks(
        self, command: object, expected: str
    ) -> None:
        """OpenCode's ``command`` is the whole argv; the projection must not raise."""
        config = _real_opencode_config(include_companions=False)
        if command is None:
            del config["mcp"]["gco"]["command"]
        else:
            config["mcp"]["gco"]["command"] = command

        problems = contract.verify_opencode_config(config, include_companions=False)

        assert any(expected in problem for problem in problems)

    def test_the_environment_block_is_projected_onto_the_shared_env_check(self) -> None:
        expect = {"GCO_ENABLE_MISSION": "true"}
        config = _real_opencode_config(include_companions=True, gco_mcp_env=expect)
        assert config["mcp"]["gco"]["environment"] == expect
        config["mcp"]["memory"]["environment"] = dict(expect)

        problems = contract.verify_opencode_config(config, expect_gco_env=expect)

        assert any("leaked onto memory" in problem for problem in problems)

    def test_normalizer_passes_non_mappings_through_untouched(self) -> None:
        assert contract._normalize_opencode_servers("text") == "text"
        assert contract._normalize_opencode_servers(None) is None
        assert contract._normalize_opencode_servers({"gco": 7}) == {"gco": 7}


class TestVerifyPlan:
    @pytest.mark.parametrize("engine", list(AutopilotEngine))
    def test_valid_plan_with_absent_and_present_binary(self, engine: AutopilotEngine) -> None:
        keyword = _BINARY_KEYWORDS[engine]
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
        binary_option = _BINARY_OPTIONS[engine]
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
        """Every engine funnels into ``_verify_server_mapping``.

        ``verify_config`` rejects a bad ``mcpServers`` before delegating, but the
        Codex and OpenCode paths reach the shared checker with differently-shaped
        documents, so the guard has to exist on both sides of the call.
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

    @pytest.mark.parametrize("pair", ["noequals", "=novalue"])
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

    @pytest.mark.parametrize("region", ["", None, 7], ids=("empty", "absent", "not-a-string"))
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

    def test_an_opencode_plan_without_the_rendered_config_is_still_valid(self) -> None:
        plan = _real_plan(AutopilotEngine.OPENCODE)
        plan.pop("opencode_config", None)

        assert (
            contract.verify_plan(plan, engine=AutopilotEngine.OPENCODE, opencode_binary="absent")
            == []
        )

    def test_a_non_object_opencode_config_in_the_plan_is_reported(self) -> None:
        plan = _real_plan(AutopilotEngine.OPENCODE)
        plan["opencode_config"] = '{"model": "text"}'

        problems = contract.verify_plan(
            plan, engine=AutopilotEngine.OPENCODE, opencode_binary="absent"
        )

        assert "OpenCode plan config must be a JSON object when present" in problems

    def test_an_opencode_plan_config_is_verified_against_the_plan_region_and_fast_model(
        self,
    ) -> None:
        plan = _real_plan(AutopilotEngine.OPENCODE, region="eu-west-1")
        problems = contract.verify_plan(
            plan, engine=AutopilotEngine.OPENCODE, opencode_binary="absent"
        )
        assert "OpenCode provider region 'us-east-2' != expected 'eu-west-1'" in problems

        fast = _real_plan(
            AutopilotEngine.OPENCODE,
            small_fast_model="us.anthropic.claude-haiku-4-5-v1:0",
            opencode_config=_real_opencode_config(small_model="us.anthropic.claude-haiku-4-5-v1:0"),
        )
        assert (
            contract.verify_plan(fast, engine=AutopilotEngine.OPENCODE, opencode_binary="absent")
            == []
        )
        fast["small_fast_model"] = None
        assert any(
            "small_model" in problem
            for problem in contract.verify_plan(
                fast, engine=AutopilotEngine.OPENCODE, opencode_binary="absent"
            )
        )

    @pytest.mark.parametrize(
        ("engine", "foreign_field", "foreign_value"),
        [
            (AutopilotEngine.CODEX, "opencode_config", {"model": "x"}),
            (AutopilotEngine.OPENCODE, "codex_config", 'model = "x"'),
            (AutopilotEngine.CLAUDE_CODE, "opencode_config", {"model": "x"}),
        ],
    )
    def test_a_plan_carrying_another_engines_config_is_reported(
        self,
        engine: AutopilotEngine,
        foreign_field: str,
        foreign_value: object,
    ) -> None:
        plan = _real_plan(engine)
        plan[foreign_field] = foreign_value
        owner = contract._PLAN_GENERATED_CONFIG_FIELDS[foreign_field]

        problems = contract.verify_plan(plan, engine=engine, **{_BINARY_KEYWORDS[engine]: "absent"})

        assert (
            f"{engine.value} plan unexpectedly carries a {owner.value} config ({foreign_field})"
            in problems
        )

    def test_selected_engine_state_leaking_into_opencode_fields_is_reported(self) -> None:
        plan = _real_plan(AutopilotEngine.CLAUDE_CODE, opencode_pin="opencode-ai@0.0.0")

        problems = contract.verify_plan(plan, claude_binary="absent")

        assert "plan leaks selected-engine state into opencode_pin/opencode_binary" in problems


class TestOpenCodeCommandLine:
    def test_verify_opencode_config_exit_codes(self, tmp_path: Path) -> None:
        good = tmp_path / "opencode.json"
        good.write_text(json.dumps(_real_opencode_config()), encoding="utf-8")
        assert (
            contract.main(
                [
                    "verify-opencode-config",
                    str(good),
                    "--region",
                    "us-east-2",
                    "--profile",
                    "default",
                ]
            )
            == 0
        )
        assert contract.main(["verify-opencode-config", str(good), "--region", "eu-west-1"]) == 1
        assert contract.main(["verify-opencode-config", str(good), "--no-profile"]) == 1

        unpinned = _real_opencode_config(small_model="us.anthropic.claude-haiku-4-5-v1:0")
        del unpinned["provider"][OPENCODE_BEDROCK_PROVIDER]["options"]["profile"]
        keys = tmp_path / "opencode-env-credentials.json"
        keys.write_text(json.dumps(unpinned), encoding="utf-8")
        assert (
            contract.main(
                [
                    "verify-opencode-config",
                    str(keys),
                    "--no-profile",
                    "--small-model",
                    "us.anthropic.claude-haiku-4-5-v1:0",
                ]
            )
            == 0
        )
        assert contract.main(["verify-opencode-config", str(keys), "--profile", "default"]) == 1

    def test_profile_and_no_profile_are_mutually_exclusive(self, tmp_path: Path) -> None:
        config = tmp_path / "opencode.json"
        config.write_text(json.dumps(_real_opencode_config()), encoding="utf-8")
        with pytest.raises(SystemExit):
            contract.main(
                ["verify-opencode-config", str(config), "--profile", "default", "--no-profile"]
            )

    def test_no_companions_flag_reaches_the_opencode_checker(self, tmp_path: Path) -> None:
        config = tmp_path / "opencode.json"
        config.write_text(
            json.dumps(_real_opencode_config(include_companions=False)),
            encoding="utf-8",
        )
        assert contract.main(["verify-opencode-config", str(config), "--no-companions"]) == 0
        assert contract.main(["verify-opencode-config", str(config)]) == 1
