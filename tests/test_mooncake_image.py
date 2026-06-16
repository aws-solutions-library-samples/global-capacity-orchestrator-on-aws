"""The Mooncake-enabled vLLM image is a first-party GCO image with pinned deps.

GCO ships a small set of first-party images it builds itself (health-monitor,
manifest-processor, queue-processor, inference-monitor). The Mooncake-enabled
vLLM image joins that set: it must be discoverable and retrievable by name
through the same :class:`cli.images.ImageManager` interface as the others, and
it must be the default image disaggregated prefill/decode deploys serve from.

Its ``dockerfiles/mooncake-vllm-dockerfile`` builds reproducibly only if both
upstream dependencies are pinned: the ``vllm/vllm-openai`` base image tag and
the ``mooncake-transfer-engine`` release. A mutable or rolling tag such as
``latest`` on either one breaks build reproducibility and provenance, so the
tests below assert both are pinned to explicit version identifiers and that no
floating tag leaks in.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cli.images import ImageManager

# The logical name the image is registered under, and the Dockerfile that
# produces it (relative to the repo root).
_IMAGE_NAME = "mooncake-vllm"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCKERFILE = _REPO_ROOT / "dockerfiles" / "mooncake-vllm-dockerfile"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# The optional-dependencies group in pyproject.toml that holds the exact,
# pinned extra packages this image layers onto the vLLM base. The Dockerfile
# reads this group at build time, so it is the single source of truth for the
# transfer-engine pin (rather than a hardcoded version in the Dockerfile).
_IMAGE_DEP_GROUP = "mooncake-image"

# Names of the other first-party images, used to confirm the new image is
# registered with the same convention rather than as a one-off.
_PEER_IMAGES = ("health-monitor", "manifest-processor", "queue-processor")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_config() -> Any:
    config = MagicMock()
    config.global_region = "us-east-2"
    config.project_name = "gco"
    config.regions = ["us-east-2", "us-west-2", "eu-west-1"]
    return config


@pytest.fixture
def manager(mock_config: Any) -> ImageManager:
    with patch("cli.images.get_config", return_value=mock_config):
        mgr = ImageManager(config=mock_config, region="us-east-2")
    mgr._account_id_cache = "123456789012"
    return mgr


# ---------------------------------------------------------------------------
# Registry discoverability / retrieval
# ---------------------------------------------------------------------------


def test_image_is_listed_alongside_the_other_first_party_images(manager: ImageManager) -> None:
    """The image appears in the maintained-image listing next to its peers."""
    listed = {row["name"] for row in manager.list_maintained_images()}
    assert _IMAGE_NAME in listed
    # Registered with the same convention as the existing first-party images.
    for peer in _PEER_IMAGES:
        assert peer in listed


def test_image_is_retrievable_by_name(manager: ImageManager) -> None:
    """Looking the image up by name returns its repository, Dockerfile, and URI."""
    info = manager.get_maintained_image(_IMAGE_NAME)
    assert info["name"] == _IMAGE_NAME
    assert info["repository"] == f"gco/{_IMAGE_NAME}"
    assert info["dockerfile"] == "dockerfiles/mooncake-vllm-dockerfile"
    assert info["uri"].endswith(f"/gco/{_IMAGE_NAME}:latest")


def test_listed_entry_matches_lookup_by_name(manager: ImageManager) -> None:
    """The listing row and the by-name lookup describe the same image."""
    listed = {row["name"]: row for row in manager.list_maintained_images()}
    assert listed[_IMAGE_NAME] == manager.get_maintained_image(_IMAGE_NAME)


def test_image_uses_the_shared_gco_repository_prefix(manager: ImageManager) -> None:
    """The image lives under the same ``gco/`` prefix as every project repo."""
    info = manager.get_maintained_image(_IMAGE_NAME)
    host = "123456789012.dkr.ecr.us-east-2.amazonaws.com"
    assert info["uri"] == f"{host}/gco/{_IMAGE_NAME}:latest"


def test_default_disaggregated_image_resolves_to_this_image(manager: ImageManager) -> None:
    """Disaggregated deploys default to the Mooncake-enabled vLLM image."""
    assert manager.default_disaggregated_image_uri().endswith(f"/gco/{_IMAGE_NAME}:latest")
    assert manager.default_disaggregated_image_uri("v1") == manager.get_uri(_IMAGE_NAME, "v1")


# ---------------------------------------------------------------------------
# Dockerfile pinning
# ---------------------------------------------------------------------------


def _dockerfile_text() -> str:
    assert _DOCKERFILE.is_file(), f"Dockerfile not found: {_DOCKERFILE}"
    return _DOCKERFILE.read_text(encoding="utf-8")


def _image_dependency_specs() -> list[str]:
    """Return the pinned extra-package specs the image installs from pyproject.

    These live in the ``mooncake-image`` optional-dependencies group and are the
    exact specs the Dockerfile pip-installs at build time.
    """
    assert _PYPROJECT.is_file(), f"pyproject.toml not found: {_PYPROJECT}"
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    groups = data["project"]["optional-dependencies"]
    assert _IMAGE_DEP_GROUP in groups, (
        f"expected a {_IMAGE_DEP_GROUP!r} optional-dependencies group in pyproject.toml"
    )
    return groups[_IMAGE_DEP_GROUP]


def test_base_image_tag_is_pinned_to_an_explicit_version() -> None:
    """The vLLM base resolves to a concrete version tag, never a floating one."""
    text = _dockerfile_text()

    # The base version is supplied through a build ARG with an explicit default.
    arg_match = re.search(r"^ARG\s+VLLM_BASE_VERSION=(\S+)", text, re.MULTILINE)
    assert arg_match, "expected an ARG default pinning the vLLM base version"
    base_version = arg_match.group(1)
    assert base_version.lower() not in {"latest", "main", "edge", "nightly"}
    # An explicit version identifier (e.g. v0.11.0), not a bare/empty default.
    assert re.search(r"\d", base_version), f"base version not pinned: {base_version!r}"

    # The FROM line consumes that ARG rather than hardcoding a rolling tag.
    from_match = re.search(r"^FROM\s+vllm/vllm-openai:(\S+)", text, re.MULTILINE)
    assert from_match, "expected a FROM vllm/vllm-openai:<tag> line"
    base_ref = from_match.group(1)
    assert "${VLLM_BASE_VERSION}" in base_ref or base_ref == base_version
    assert ":latest" not in from_match.group(0).lower()


def test_transfer_engine_version_is_pinned_to_an_explicit_release() -> None:
    """The Mooncake transfer engine is pinned to an exact ``==`` release.

    The pin lives in the ``mooncake-image`` optional-dependencies group in
    pyproject.toml (the image's single source of truth for its extra packages),
    not hardcoded in the Dockerfile. It must be an exact ``==`` version with no
    floating spec (no bare name, ``>=``, ``~=``, ``*``, or rolling tag).
    """
    specs = _image_dependency_specs()

    engine_specs = [s for s in specs if s.replace(" ", "").startswith("mooncake-transfer-engine")]
    assert engine_specs, (
        f"expected mooncake-transfer-engine in the {_IMAGE_DEP_GROUP!r} group, got {specs!r}"
    )

    for spec in specs:
        compact = spec.replace(" ", "")
        # Every entry pins exactly with ``==`` — no floating operators.
        for floating in (">=", "<=", "~=", ">", "<", "!="):
            assert floating not in compact, f"floating spec {spec!r} in {_IMAGE_DEP_GROUP!r}"
        assert "==" in compact, f"unpinned spec {spec!r} in {_IMAGE_DEP_GROUP!r}"
        version = compact.split("==", 1)[1]
        assert version.lower() not in {"latest", "main", "edge", "nightly", "*", ""}
        assert re.search(r"\d", version), f"version not pinned: {spec!r}"


def test_dockerfile_installs_extras_from_the_pyproject_group() -> None:
    """The Dockerfile sources its extra packages from the pyproject group.

    Rather than hardcoding the package versions, the build reads the
    ``mooncake-image`` group out of pyproject.toml and pip-installs exactly
    those specs, so the pins stay in one place.
    """
    text = _dockerfile_text()
    # The Dockerfile copies pyproject.toml in and references the group by name.
    assert "pyproject.toml" in text
    assert _IMAGE_DEP_GROUP in text
    # It installs the resolved specs with pip (from the extracted requirements).
    assert "pip install" in text
    assert "-r " in text


def test_dockerfile_uses_no_floating_latest_tag() -> None:
    """No dependency in the Dockerfile falls back to a ``latest`` tag."""
    text = _dockerfile_text().lower()
    assert ":latest" not in text
