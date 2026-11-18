"""Released cdk.json files keep passing the configuration validation.

``gco upgrade`` keeps an operator's cdk.json byte for byte, so the current
``ConfigLoader`` must accept every cdk.json an earlier release shipped. Each
file under ``tests/fixtures/cdk_json/`` is one release's committed cdk.json
(see that directory's README); each is loaded through a real ``aws_cdk.App``
and the full validation.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
import pytest

from gco.config.config_loader import ConfigLoader

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cdk_json"
RELEASED = sorted(FIXTURES.glob("*.json"))


def _context(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["context"]


def test_the_v8_23_0_release_is_covered() -> None:
    """The release the compatibility rules were written against stays in the set."""
    assert FIXTURES / "v8.23.0.json" in RELEASED


@pytest.mark.parametrize("path", RELEASED, ids=[path.stem for path in RELEASED])
def test_released_cdk_json_passes_validation(path: Path) -> None:
    """The released file validates unchanged and resolves its project and Regions."""
    context = _context(path)
    config = ConfigLoader(cdk.App(context=context))
    assert config.get_project_name() == context["project_name"]
    assert config.get_regions() == context["deployment_regions"]["regional"]


@pytest.mark.parametrize("path", RELEASED, ids=[path.stem for path in RELEASED])
def test_released_cdk_json_raises_only_deprecation_warnings(path: Path) -> None:
    """Anything the release shipped that no longer takes effect is reported as deprecated."""
    config = ConfigLoader(cdk.App(context=_context(path)))
    for warning in config.warnings:
        assert " is deprecated and ignored" in warning, warning


def test_v8_23_0_deprecations_are_the_expected_keys() -> None:
    """Pins which v8.23.0 keys are deprecated, so a new deprecation is a reviewed change."""
    config = ConfigLoader(cdk.App(context=_context(FIXTURES / "v8.23.0.json")))
    deprecated = {warning.split(" is deprecated and ignored")[0] for warning in config.warnings}
    assert deprecated == {
        "alb_config",
        "manifest_processor.image",
        "images.removal_policy",
        "images.empty_on_delete",
        "images.lifecycle",
    }
