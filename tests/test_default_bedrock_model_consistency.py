"""The advisory Bedrock default has one checked-in source in ``cdk.json``.

Mission sampling and the capacity advisor retain lazy compatibility aliases,
but both resolve the value through :mod:`gco.bedrock`. The dependency scanner
reads the same JSON path directly, and the configured model must have a
complete captured scaffolder fixture.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Mirror the path-injection pattern used across the Mission tests so
# ``mission.*`` resolves regardless of how pytest is invoked.
sys.path.insert(0, str(PROJECT_ROOT / "gco_mcp"))

import mission.sampling as mission_sampling  # noqa: E402

import gco.bedrock as bedrock_config  # noqa: E402
from cli.capacity.advisor import BedrockCapacityAdvisor  # noqa: E402
from gco.bedrock import (  # noqa: E402
    DEFAULT_BEDROCK_MODEL_ID,
    build_bedrock_converse_options,
    get_default_bedrock_configuration,
    get_default_bedrock_model_id,
    get_default_bedrock_thinking_effort,
)
from tests._scaffold_replay import (  # noqa: E402
    CANONICAL_CAPTURE_SLUGS,
    DEFAULT_FIXTURE,
    DEFAULT_FIXTURE_PATH,
    DEFAULT_MODEL_ID,
)

# Pin the intended default independently from the loader so an accidental
# same-family or lower-tier change is visible in review and CI.
_EXPECTED_DEFAULT_MODEL_ID = "global.amazon.nova-2-lite-v1:0"
_EXPECTED_FIXTURE_NAME = "global_amazon_nova_2_lite_v1_0.json"
_EXPECTED_THINKING = {"effort": "high"}
_RUNTIME_SOURCE_ROOTS = ("cli", "gco", "gco_mcp", "scripts", ".github/scripts")
_RUNTIME_SOURCE_SUFFIXES = {".py", ".sh"}


def test_cdk_json_is_the_single_operational_default_source() -> None:
    """The shared resolver and both compatibility aliases consume cdk.json."""
    from mission.sampling import DEFAULT_BEDROCK_MODEL_ID as mission_default

    configured = get_default_bedrock_model_id(PROJECT_ROOT / "cdk.json")
    configuration = get_default_bedrock_configuration(PROJECT_ROOT / "cdk.json")

    assert configured == _EXPECTED_DEFAULT_MODEL_ID
    assert configuration.model_id == _EXPECTED_DEFAULT_MODEL_ID
    assert configuration.thinking_effort == _EXPECTED_THINKING["effort"]
    assert get_default_bedrock_thinking_effort(PROJECT_ROOT / "cdk.json") == "high"
    assert configured == DEFAULT_BEDROCK_MODEL_ID
    assert configured == mission_default
    assert configured == BedrockCapacityAdvisor.DEFAULT_MODEL


def test_fixture_capture_preserves_canonical_reasoning_provenance(monkeypatch: Any) -> None:
    """Canonical capture bypasses model env overrides; other IDs stay explicit."""
    from scripts import capture_scaffold_fixtures as capture

    monkeypatch.setenv("GCO_MISSION_BEDROCK_MODEL_ID", "anthropic.environment-override")

    canonical = capture._backend_for_capture(_EXPECTED_DEFAULT_MODEL_ID, "us-east-1")
    assert canonical.model_id == _EXPECTED_DEFAULT_MODEL_ID
    assert canonical._uses_default_model is True

    explicit = capture._backend_for_capture("anthropic.explicit-override", "us-east-1")
    assert explicit.model_id == "anthropic.explicit-override"
    assert explicit._uses_default_model is False


def test_lazy_mission_alias_is_discoverable_without_resolution(monkeypatch: Any) -> None:
    """Introspection advertises the compatibility alias without loading config."""

    def _unexpected_resolution() -> str:
        raise AssertionError("dir() must not resolve the Bedrock model default")

    monkeypatch.setattr(
        mission_sampling,
        "get_default_bedrock_model_id",
        _unexpected_resolution,
    )

    assert "DEFAULT_BEDROCK_MODEL_ID" not in vars(mission_sampling)
    assert "DEFAULT_BEDROCK_MODEL_ID" in dir(mission_sampling)


def test_exact_default_literal_is_absent_from_all_runtime_sources() -> None:
    """No runtime module or shell script may recreate the operational pin."""
    violations: list[str] = []
    for root_name in _RUNTIME_SOURCE_ROOTS:
        for path in sorted((PROJECT_ROOT / root_name).rglob("*")):
            if not path.is_file() or path.suffix not in _RUNTIME_SOURCE_SUFFIXES:
                continue
            relative_path = path.relative_to(PROJECT_ROOT).as_posix()
            if _EXPECTED_DEFAULT_MODEL_ID in path.read_text(encoding="utf-8"):
                violations.append(relative_path)

    assert violations == []


def test_ambient_cdk_json_cannot_override_the_canonical_default(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Current-directory configuration is outside the model trust boundary."""
    ambient = tmp_path / "cdk.json"
    ambient.write_text(
        json.dumps({"context": {"bedrock": {"default_model_id": "us.amazon.nova-pro-v1:0"}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert get_default_bedrock_model_id() == _EXPECTED_DEFAULT_MODEL_ID


def test_default_cdk_json_is_shipped_for_installed_cli_and_mcp_use() -> None:
    """Clone-free installations retain access to the same canonical file."""
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    data_files = project["tool"]["setuptools"]["data-files"]

    assert "cdk.json" in data_files["share/gco"]


def test_installed_config_uses_distribution_metadata(tmp_path: Path, monkeypatch: Any) -> None:
    """Non-default installation schemes resolve their recorded data path."""
    installed_config = tmp_path / "custom-scheme" / "share" / "gco" / "cdk.json"
    installed_config.parent.mkdir(parents=True)
    installed_config.write_text("{}", encoding="utf-8")
    recorded_path = Path("../../../share/gco/cdk.json")

    class _FakeDistribution:
        files = (recorded_path,)

        @staticmethod
        def locate_file(relative_path: Path) -> Path:
            assert relative_path == recorded_path
            return installed_config

    def _fake_distribution(name: str) -> _FakeDistribution:
        assert name == "gco-cli"
        return _FakeDistribution()

    monkeypatch.setattr(bedrock_config.metadata, "distribution", _fake_distribution)

    assert bedrock_config._installed_cdk_json_path() == installed_config.resolve()


def test_default_model_has_the_exact_complete_replay_fixture() -> None:
    """The configured default has all canonical live-captured responses."""
    assert DEFAULT_MODEL_ID == _EXPECTED_DEFAULT_MODEL_ID
    assert DEFAULT_FIXTURE.model_id == _EXPECTED_DEFAULT_MODEL_ID
    assert DEFAULT_FIXTURE.path == DEFAULT_FIXTURE_PATH
    assert DEFAULT_FIXTURE_PATH.name == _EXPECTED_FIXTURE_NAME
    assert set(CANONICAL_CAPTURE_SLUGS) <= {capture.slug for capture in DEFAULT_FIXTURE.captures}


def test_default_model_is_a_system_defined_inference_profile_id() -> None:
    """The default shape matches the dependency scanner's family comparator."""
    model_id = DEFAULT_BEDROCK_MODEL_ID
    assert model_id.startswith(("global.", "us.", "eu.", "apac.", "jp.")), model_id
    assert "-v" in model_id, model_id
    assert ":" in model_id.rsplit("-v", 1)[-1], model_id


def test_cdk_json_contains_only_one_default_model_value() -> None:
    """The configured value is a scalar, not duplicated in sibling context keys."""
    payload = json.loads((PROJECT_ROOT / "cdk.json").read_text(encoding="utf-8"))
    bedrock = payload["context"]["bedrock"]

    assert bedrock == {
        "default_model_id": _EXPECTED_DEFAULT_MODEL_ID,
        "thinking": _EXPECTED_THINKING,
    }


def test_default_high_thinking_translates_to_native_converse_fields() -> None:
    options = build_bedrock_converse_options(
        _EXPECTED_DEFAULT_MODEL_ID,
        inference_config={"maxTokens": 2048, "temperature": 0.2, "topP": 0.9},
        cdk_json_path=PROJECT_ROOT / "cdk.json",
    )

    assert "inferenceConfig" not in options
    assert options == {
        "additionalModelRequestFields": {
            "reasoningConfig": {
                "type": "enabled",
                "maxReasoningEffort": "high",
            }
        }
    }


def test_explicit_other_model_keeps_inference_config_without_nova_reasoning(
    tmp_path: Path,
) -> None:
    inference_config = {"maxTokens": 2048, "temperature": 0.2}

    options = build_bedrock_converse_options(
        "us.anthropic.claude-3-haiku-20240307-v1:0",
        inference_config=inference_config,
        cdk_json_path=tmp_path / "missing-cdk.json",
    )

    assert options == {"inferenceConfig": inference_config}


def test_explicit_nova_override_does_not_load_or_apply_canonical_reasoning(
    tmp_path: Path,
) -> None:
    inference_config = {"maxTokens": 1024, "temperature": 0.2}

    options = build_bedrock_converse_options(
        _EXPECTED_DEFAULT_MODEL_ID,
        inference_config=inference_config,
        cdk_json_path=tmp_path / "missing-cdk.json",
        apply_default_reasoning=False,
    )

    assert options == {"inferenceConfig": inference_config}


def _bedrock_payload(model_id: Any = _EXPECTED_DEFAULT_MODEL_ID) -> dict[str, Any]:
    return {
        "context": {
            "bedrock": {
                "default_model_id": model_id,
                "thinking": dict(_EXPECTED_THINKING),
            }
        }
    }


def test_non_nova_canonical_model_never_receives_nova_reasoning(tmp_path: Path) -> None:
    config_path = tmp_path / "cdk.json"
    config_path.write_text(
        json.dumps(_bedrock_payload("us.example.model-v1:0")),
        encoding="utf-8",
    )
    inference_config = {"maxTokens": 512, "temperature": 0.1}

    options = build_bedrock_converse_options(
        "us.example.model-v1:0",
        inference_config=inference_config,
        cdk_json_path=config_path,
    )

    assert options == {"inferenceConfig": inference_config}


def test_source_config_path_requires_checkout_markers(tmp_path: Path, monkeypatch: Any) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    source_config = checkout / "cdk.json"
    markers = (checkout / "app.py", checkout / "pyproject.toml")
    for marker in markers:
        marker.write_text("# checkout marker\n", encoding="utf-8")

    monkeypatch.setattr(bedrock_config, "_SOURCE_CDK_JSON", source_config)
    monkeypatch.setattr(bedrock_config, "_SOURCE_CHECKOUT_MARKERS", markers)

    assert bedrock_config._source_cdk_json_path() == source_config
    markers[-1].unlink()
    assert bedrock_config._source_cdk_json_path() is None


@pytest.mark.parametrize(
    "recorded_files",
    [
        None,
        (),
        (Path("share/gco/not-cdk.json"), Path("share/other/cdk.json")),
    ],
)
def test_installed_config_returns_none_without_recorded_canonical_file(
    recorded_files: tuple[Path, ...] | None,
    monkeypatch: Any,
) -> None:
    class _DistributionWithoutConfig:
        files = recorded_files

        @staticmethod
        def locate_file(_relative_path: Path) -> Path:
            raise AssertionError("an unrelated metadata entry must not be located")

    monkeypatch.setattr(
        bedrock_config.metadata,
        "distribution",
        lambda _name: _DistributionWithoutConfig(),
    )

    assert bedrock_config._installed_cdk_json_path() is None


def test_installed_config_returns_none_when_distribution_is_absent(monkeypatch: Any) -> None:
    def _missing_distribution(_name: str) -> Any:
        raise bedrock_config.metadata.PackageNotFoundError("gco-cli")

    monkeypatch.setattr(bedrock_config.metadata, "distribution", _missing_distribution)

    assert bedrock_config._installed_cdk_json_path() is None


@pytest.mark.parametrize("failure_stage", ["distribution", "locate_file"])
def test_installed_config_wraps_metadata_inspection_errors(
    failure_stage: str, monkeypatch: Any
) -> None:
    cause = OSError(f"broken {failure_stage}")

    class _BrokenDistribution:
        files = (Path("../../../share/gco/cdk.json"),)

        @staticmethod
        def locate_file(_relative_path: Path) -> Path:
            raise cause

    def _distribution(_name: str) -> _BrokenDistribution:
        if failure_stage == "distribution":
            raise cause
        return _BrokenDistribution()

    monkeypatch.setattr(bedrock_config.metadata, "distribution", _distribution)

    with pytest.raises(bedrock_config.BedrockModelConfigurationError) as exc_info:
        bedrock_config._installed_cdk_json_path()

    assert "Unable to inspect installed gco-cli package data" in str(exc_info.value)
    assert exc_info.value.__cause__ is cause


def test_missing_source_config_does_not_fall_back_to_installed_copy(
    tmp_path: Path, monkeypatch: Any
) -> None:
    missing_source = tmp_path / "checkout" / "cdk.json"
    installed = tmp_path / "installed" / "cdk.json"
    installed.parent.mkdir()
    installed.write_text(json.dumps(_bedrock_payload("installed.model-v1:0")), encoding="utf-8")

    monkeypatch.setattr(bedrock_config, "_source_cdk_json_path", lambda: missing_source)
    monkeypatch.setattr(bedrock_config, "_installed_cdk_json_path", lambda: installed)

    with pytest.raises(
        bedrock_config.BedrockModelConfigurationError,
        match="Canonical Bedrock config is not a file",
    ):
        bedrock_config.get_default_bedrock_model_id()


def test_installed_config_is_selected_outside_source_checkout(
    tmp_path: Path, monkeypatch: Any
) -> None:
    installed = tmp_path / "installed" / "cdk.json"
    installed.parent.mkdir()
    installed.write_text(
        json.dumps(_bedrock_payload(" installed.model-v1:0 ")),
        encoding="utf-8",
    )

    monkeypatch.setattr(bedrock_config, "_source_cdk_json_path", lambda: None)
    monkeypatch.setattr(bedrock_config, "_installed_cdk_json_path", lambda: installed)

    assert bedrock_config.get_default_bedrock_model_id() == "installed.model-v1:0"


def test_canonical_config_errors_when_no_owned_path_exists(monkeypatch: Any) -> None:
    monkeypatch.setattr(bedrock_config, "_source_cdk_json_path", lambda: None)
    monkeypatch.setattr(bedrock_config, "_installed_cdk_json_path", lambda: None)

    with pytest.raises(
        bedrock_config.BedrockModelConfigurationError,
        match="Could not locate canonical cdk.json",
    ):
        bedrock_config._canonical_cdk_json_path()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (None, "document root must be an object"),
        ([], "document root must be an object"),
        ({}, "context must be an object"),
        ({"context": []}, "context must be an object"),
        ({"context": {}}, "context.bedrock must be an object"),
        ({"context": {"bedrock": []}}, "context.bedrock must be an object"),
        (
            {"context": {"bedrock": {}}},
            "default_model_id must be a non-empty string",
        ),
        (_bedrock_payload(None), "default_model_id must be a non-empty string"),
        (_bedrock_payload(42), "default_model_id must be a non-empty string"),
        (_bedrock_payload(""), "default_model_id must be a non-empty string"),
        (_bedrock_payload("   \n"), "default_model_id must be a non-empty string"),
    ],
)
def test_model_payload_validation_fails_closed(
    payload: Any,
    message: str,
) -> None:
    path = Path("/canonical/cdk.json")

    with pytest.raises(bedrock_config.BedrockModelConfigurationError) as exc_info:
        bedrock_config._model_id_from_payload(payload, path)

    assert str(path) in str(exc_info.value)
    assert message in str(exc_info.value)


def test_model_payload_trims_valid_identifier() -> None:
    assert (
        bedrock_config._model_id_from_payload(
            _bedrock_payload("  us.example.model-v1:0\n"),
            Path("/canonical/cdk.json"),
        )
        == "us.example.model-v1:0"
    )


@pytest.mark.parametrize(
    ("thinking", "message"),
    [
        (None, "thinking must be an object"),
        ([], "thinking must be an object"),
        ({}, "thinking must contain only 'effort'"),
        ({"effort": "maximum"}, "thinking.effort must be one of high, low, medium"),
        ({"effort": []}, "thinking.effort must be one of high, low, medium"),
        ({"effort": {}}, "thinking.effort must be one of high, low, medium"),
        (
            {"effort": "high", "budget": 32000},
            "thinking must contain only 'effort'",
        ),
    ],
)
def test_thinking_payload_validation_fails_closed(thinking: Any, message: str) -> None:
    payload = _bedrock_payload()
    payload["context"]["bedrock"]["thinking"] = thinking
    path = Path("/canonical/cdk.json")

    with pytest.raises(bedrock_config.BedrockModelConfigurationError) as exc_info:
        bedrock_config._bedrock_configuration_from_payload(payload, path)

    assert str(path) in str(exc_info.value)
    assert message in str(exc_info.value)


@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_explicit_config_must_be_a_file(tmp_path: Path, path_kind: str) -> None:
    path = tmp_path / path_kind
    if path_kind == "directory":
        path.mkdir()

    with pytest.raises(
        bedrock_config.BedrockModelConfigurationError,
        match="Canonical Bedrock config is not a file",
    ):
        bedrock_config.get_default_bedrock_model_id(path)


@pytest.mark.parametrize("error_type", [OSError, UnicodeError])
def test_unreadable_config_is_wrapped(
    tmp_path: Path,
    monkeypatch: Any,
    error_type: type[Exception],
) -> None:
    path = tmp_path / "cdk.json"
    path.write_text("{}", encoding="utf-8")
    cause = error_type("cannot decode canonical config")

    def _unreadable(_path: Path, **_kwargs: Any) -> str:
        raise cause

    monkeypatch.setattr(Path, "read_text", _unreadable)

    with pytest.raises(bedrock_config.BedrockModelConfigurationError) as exc_info:
        bedrock_config.get_default_bedrock_model_id(path)

    assert "Unable to read" in str(exc_info.value)
    assert exc_info.value.__cause__ is cause


def test_invalid_json_is_wrapped_with_decode_error(tmp_path: Path) -> None:
    path = tmp_path / "cdk.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(bedrock_config.BedrockModelConfigurationError) as exc_info:
        bedrock_config.get_default_bedrock_model_id(path)

    assert "Invalid JSON" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)


def test_explicit_valid_config_does_not_consult_implicit_path(
    tmp_path: Path, monkeypatch: Any
) -> None:
    path = tmp_path / "cdk.json"
    path.write_text(
        json.dumps(_bedrock_payload(" explicit.model-v2:0 ")),
        encoding="utf-8",
    )

    def _unexpected_implicit_resolution() -> Path:
        raise AssertionError("an explicit path must bypass canonical discovery")

    monkeypatch.setattr(
        bedrock_config,
        "_canonical_cdk_json_path",
        _unexpected_implicit_resolution,
    )

    assert bedrock_config.get_default_bedrock_model_id(path) == "explicit.model-v2:0"


def test_lazy_module_alias_resolves_through_shared_loader(monkeypatch: Any) -> None:
    resolutions = []

    def _resolve() -> str:
        resolutions.append("resolved")
        return "lazy.model-v1:0"

    monkeypatch.setattr(bedrock_config, "get_default_bedrock_model_id", _resolve)

    assert bedrock_config.DEFAULT_BEDROCK_MODEL_ID == "lazy.model-v1:0"
    assert resolutions == ["resolved"]
    assert "DEFAULT_BEDROCK_MODEL_ID" not in vars(bedrock_config)


def test_lazy_module_alias_rejects_unknown_attribute_without_loading(monkeypatch: Any) -> None:
    def _unexpected_resolution() -> str:
        raise AssertionError("unknown attributes must not load configuration")

    monkeypatch.setattr(
        bedrock_config,
        "get_default_bedrock_model_id",
        _unexpected_resolution,
    )

    with pytest.raises(AttributeError) as exc_info:
        _ = bedrock_config.NOT_A_BEDROCK_SETTING

    assert "NOT_A_BEDROCK_SETTING" in str(exc_info.value)


def test_bedrock_dir_is_sorted_and_advertises_lazy_alias_without_loading(
    monkeypatch: Any,
) -> None:
    def _unexpected_resolution() -> str:
        raise AssertionError("dir() must not load configuration")

    monkeypatch.setattr(
        bedrock_config,
        "get_default_bedrock_model_id",
        _unexpected_resolution,
    )

    names = bedrock_config.__dir__()

    assert names == sorted(names)
    assert "DEFAULT_BEDROCK_MODEL_ID" in names
