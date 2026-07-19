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

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Mirror the path-injection pattern used across the Mission tests so
# ``mission.*`` resolves regardless of how pytest is invoked.
sys.path.insert(0, str(PROJECT_ROOT / "gco_mcp"))

import mission.sampling as mission_sampling  # noqa: E402

import gco.bedrock as bedrock_config  # noqa: E402
from cli.capacity.advisor import BedrockCapacityAdvisor  # noqa: E402
from gco.bedrock import (  # noqa: E402
    DEFAULT_BEDROCK_MODEL_ID,
    get_default_bedrock_model_id,
)
from tests._scaffold_replay import (  # noqa: E402
    CANONICAL_CAPTURE_SLUGS,
    PREMIER_FIXTURE,
    PREMIER_FIXTURE_PATH,
    PREMIER_MODEL_ID,
)

# Pin the intended default independently from the loader so an accidental
# same-family or lower-tier change is visible in review and CI.
_EXPECTED_DEFAULT_MODEL_ID = "us.amazon.nova-premier-v1:0"
_EXPECTED_FIXTURE_NAME = "us_amazon_nova_premier_v1_0.json"
_RUNTIME_SOURCE_ROOTS = ("cli", "gco", "gco_mcp", "scripts", ".github/scripts")
_RUNTIME_SOURCE_SUFFIXES = {".py", ".sh"}


def test_cdk_json_is_the_single_operational_default_source() -> None:
    """The shared resolver and both compatibility aliases consume cdk.json."""
    from mission.sampling import DEFAULT_BEDROCK_MODEL_ID as mission_default

    configured = get_default_bedrock_model_id(PROJECT_ROOT / "cdk.json")

    assert configured == _EXPECTED_DEFAULT_MODEL_ID
    assert configured == DEFAULT_BEDROCK_MODEL_ID
    assert configured == mission_default
    assert configured == BedrockCapacityAdvisor.DEFAULT_MODEL


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
    assert PREMIER_MODEL_ID == _EXPECTED_DEFAULT_MODEL_ID
    assert PREMIER_FIXTURE.model_id == _EXPECTED_DEFAULT_MODEL_ID
    assert PREMIER_FIXTURE.path == PREMIER_FIXTURE_PATH
    assert PREMIER_FIXTURE_PATH.name == _EXPECTED_FIXTURE_NAME
    assert set(CANONICAL_CAPTURE_SLUGS) <= {capture.slug for capture in PREMIER_FIXTURE.captures}


def test_default_model_is_a_system_defined_inference_profile_id() -> None:
    """The default shape matches the dependency scanner's family comparator."""
    model_id = DEFAULT_BEDROCK_MODEL_ID
    assert model_id.startswith(("us.", "eu.", "apac.")), model_id
    assert "-v" in model_id, model_id
    assert ":" in model_id.rsplit("-v", 1)[-1], model_id


def test_cdk_json_contains_only_one_default_model_value() -> None:
    """The configured value is a scalar, not duplicated in sibling context keys."""
    payload = json.loads((PROJECT_ROOT / "cdk.json").read_text(encoding="utf-8"))
    bedrock = payload["context"]["bedrock"]

    assert bedrock == {"default_model_id": _EXPECTED_DEFAULT_MODEL_ID}
