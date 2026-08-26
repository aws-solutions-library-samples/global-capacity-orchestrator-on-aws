"""
Tests for cli/images.py — the ImageManager surface for ECR-backed
container image registries.

Property-based tests verify that the validation regexes round-trip
across the full input space, that the URI rewrite helper is identity
for non-ECR refs, and that obvious path-traversal attempts are
rejected up front. Deterministic tests pin the lifecycle policy
shape, the default URI behaviour, immutable-tag rejection, and
runtime detection wiring.

ECR mocking follows the established project pattern (see
tests/test_models_cli.py) — patch the boto3 clients constructed
inside ImageManager via :class:`unittest.mock.patch`.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli._image_uri import aws_partition, aws_url_suffix, ecr_registry_host
from cli.images import (
    _NAME_RE,
    _TAG_RE,
    ImageManager,
    _rewrite_image_uri_for_region,
)

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
# Property-based tests
# ---------------------------------------------------------------------------


@given(name=st.from_regex(r"^[a-z][a-z0-9-]{0,62}$", fullmatch=True))
@settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_image_name_validation_round_trip(name: str, manager: ImageManager) -> None:
    """Every name matching ``_NAME_RE`` must round-trip through ``_validate_name``."""
    assert _NAME_RE.match(name)
    assert manager._validate_name(name) == name


@given(tag=st.from_regex(r"^[a-zA-Z0-9_][a-zA-Z0-9_.\-]{0,127}$", fullmatch=True))
@settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_image_tag_validation_round_trip(tag: str, manager: ImageManager) -> None:
    """Every tag matching ``_TAG_RE`` must round-trip through ``_validate_tag``."""
    assert _TAG_RE.match(tag)
    assert manager._validate_tag(tag) == tag


_NON_ECR_HOSTS = st.sampled_from(
    [
        "docker.io",
        "ghcr.io",
        "quay.io",
        "registry.k8s.io",
        "public.ecr.aws",
        "gcr.io",
        "registry.example.com",
    ]
)


@given(
    host=_NON_ECR_HOSTS,
    repo=st.from_regex(r"^[a-z][a-z0-9-/_]{0,40}[a-z0-9]$", fullmatch=True),
    tag=st.from_regex(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,30}$", fullmatch=True),
    target=st.sampled_from(["us-east-1", "eu-west-1", "ap-northeast-1"]),
)
@settings(max_examples=100, deadline=None)
def test_rewrite_image_uri_identity_for_non_ecr(
    host: str, repo: str, tag: str, target: str
) -> None:
    """Non-ECR URIs must pass through ``_rewrite_image_uri_for_region`` unchanged."""
    uri = f"{host}/{repo}:{tag}"
    assert _rewrite_image_uri_for_region(uri, target) == uri


@given(
    account=st.from_regex(r"^[1-9][0-9]{11}$", fullmatch=True),
    src_region=st.sampled_from(["us-east-1", "us-east-2", "us-west-2", "eu-west-1"]),
    dst_region=st.sampled_from(
        ["us-east-1", "us-east-2", "us-west-2", "eu-west-1", "ap-southeast-1"]
    ),
    repo=st.from_regex(r"^gco/[a-z][a-z0-9-]{0,30}$", fullmatch=True),
    tag=st.from_regex(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,20}$", fullmatch=True),
)
@settings(max_examples=200, deadline=None)
def test_rewrite_image_uri_swaps_region_for_ecr(
    account: str, src_region: str, dst_region: str, repo: str, tag: str
) -> None:
    """ECR URIs must have their region segment swapped for the target."""
    src_uri = f"{account}.dkr.ecr.{src_region}.amazonaws.com/{repo}:{tag}"
    expected = f"{account}.dkr.ecr.{dst_region}.amazonaws.com/{repo}:{tag}"
    assert _rewrite_image_uri_for_region(src_uri, dst_region) == expected


def test_ecr_helpers_use_partition_metadata() -> None:
    """Registry hosts and ARNs must follow the target region's partition."""
    assert aws_partition("cn-north-1") == "aws-cn"
    assert aws_url_suffix("cn-north-1") == "amazonaws.com.cn"
    assert (
        ecr_registry_host("123456789012", "eusc-de-east-1")
        == "123456789012.dkr.ecr.eusc-de-east-1.amazonaws.eu"
    )


def test_rewrite_image_uri_uses_target_partition_suffix() -> None:
    src = "123456789012.dkr.ecr.cn-north-1.amazonaws.com.cn/gco/model:v1"
    assert _rewrite_image_uri_for_region(src, "cn-northwest-1") == (
        "123456789012.dkr.ecr.cn-northwest-1.amazonaws.com.cn/gco/model:v1"
    )


def test_rewrite_image_uri_rejects_ecr_lookalike_suffix() -> None:
    uri = "123456789012.dkr.ecr.us-east-1.registry.example.com/gco/model:v1"
    assert _rewrite_image_uri_for_region(uri, "us-west-2") == uri


@given(
    bad_path=st.from_regex(r"^[a-z]+(/\.\.)+/[a-z]+$", fullmatch=True),
)
@settings(
    max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_path_traversal_rejection(bad_path: str, manager: ImageManager) -> None:
    """Build context strings containing ``..`` segments must be rejected."""
    with pytest.raises(ValueError, match="path traversal"):
        manager._validate_context(bad_path)


# ---------------------------------------------------------------------------
# Validation negative cases (deterministic)
# ---------------------------------------------------------------------------


def test_validate_name_rejects_uppercase(manager: ImageManager) -> None:
    with pytest.raises(ValueError, match="Invalid image name"):
        manager._validate_name("My-App")


def test_validate_name_rejects_leading_digit(manager: ImageManager) -> None:
    with pytest.raises(ValueError, match="Invalid image name"):
        manager._validate_name("1abc")


def test_validate_name_rejects_too_long(manager: ImageManager) -> None:
    with pytest.raises(ValueError, match="Invalid image name"):
        manager._validate_name("a" * 100)


def test_validate_tag_rejects_dot_prefix(manager: ImageManager) -> None:
    with pytest.raises(ValueError, match="Invalid image tag"):
        manager._validate_tag(".hidden")


def test_validate_tag_rejects_too_long(manager: ImageManager) -> None:
    with pytest.raises(ValueError, match="Invalid image tag"):
        manager._validate_tag("a" * 200)


# ---------------------------------------------------------------------------
# Deterministic ECR behaviour
# ---------------------------------------------------------------------------


def _ecr_with_repo_already_exists() -> Any:
    """Build a mock ECR client whose CreateRepository raises on duplicate."""
    mock_ecr = MagicMock()

    class _RepoAlreadyExists(ClientError):
        pass

    mock_ecr.exceptions.RepositoryAlreadyExistsException = _RepoAlreadyExists
    mock_ecr.exceptions.LifecyclePolicyNotFoundException = type(
        "LifecyclePolicyNotFoundException", (ClientError,), {}
    )
    mock_ecr.exceptions.RepositoryNotFoundException = type(
        "RepositoryNotFoundException", (ClientError,), {}
    )
    mock_ecr.exceptions.ImageNotFoundException = type("ImageNotFoundException", (ClientError,), {})
    return mock_ecr, _RepoAlreadyExists


def test_images_init_idempotent(manager: ImageManager) -> None:
    """init() is safe to call twice — second call detects the existing repo."""
    mock_ecr, repo_exists = _ecr_with_repo_already_exists()
    calls = {"create": 0}

    def create_side(**kwargs):
        calls["create"] += 1
        if calls["create"] >= 2:
            err = repo_exists(
                {"Error": {"Code": "RepositoryAlreadyExistsException"}},
                "CreateRepository",
            )
            raise err
        return {}

    mock_ecr.create_repository.side_effect = create_side
    mock_ecr.put_lifecycle_policy.return_value = {}

    with patch.object(manager, "_ecr_client", return_value=mock_ecr):
        first = manager.init("my-app")
        second = manager.init("my-app")

    assert first["created"] is True
    assert second["created"] is False
    assert mock_ecr.put_lifecycle_policy.call_count == 2


def test_images_uri_default_tag(manager: ImageManager) -> None:
    """get_uri() with no tag returns the registry path with `:latest`."""
    uri = manager.get_uri("my-app")
    assert uri == "123456789012.dkr.ecr.us-east-2.amazonaws.com/gco/my-app:latest"


def test_images_uri_with_explicit_tag(manager: ImageManager) -> None:
    uri = manager.get_uri("my-app", tag="v1.2.3")
    assert uri.endswith("/gco/my-app:v1.2.3")


def test_default_lifecycle_policy_keeps_20_tagged_expires_untagged_7d(
    manager: ImageManager,
) -> None:
    """The default lifecycle policy matches the documented retention rules."""
    policy = manager._default_lifecycle_policy()
    rules = policy["rules"]
    assert len(rules) == 2

    keep_rule = next(r for r in rules if r["selection"]["tagStatus"] == "tagged")
    assert keep_rule["selection"]["countType"] == "imageCountMoreThan"
    assert keep_rule["selection"]["countNumber"] == 20
    assert keep_rule["action"]["type"] == "expire"

    expire_rule = next(r for r in rules if r["selection"]["tagStatus"] == "untagged")
    assert expire_rule["selection"]["countType"] == "sinceImagePushed"
    assert expire_rule["selection"]["countUnit"] == "days"
    assert expire_rule["selection"]["countNumber"] == 7
    assert expire_rule["action"]["type"] == "expire"


@pytest.mark.parametrize("runtime", ["docker", "finch", "podman"])
def test_images_build_runtime_detection(manager: ImageManager, runtime: str, tmp_path: Any) -> None:
    """build() invokes the detected runtime in subprocess argv."""
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM scratch\n")

    mock_ecr, _ = _ecr_with_repo_already_exists()
    mock_ecr.create_repository.return_value = {}
    mock_ecr.put_lifecycle_policy.return_value = {}
    mock_ecr.describe_repositories.return_value = {"repositories": []}
    mock_ecr.get_authorization_token.return_value = {
        "authorizationData": [
            {"authorizationToken": "QVdTOnRva2Vu"}  # base64("AWS:token")
        ]
    }
    mock_ecr.describe_images.return_value = {"imageDetails": []}

    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        result.stdout = "digest: sha256:" + "0" * 64 + "\n"
        result.stderr = ""
        return result

    with (
        patch("cli.images.detect_container_runtime", return_value=runtime),
        patch.object(manager, "_ecr_client", return_value=mock_ecr),
        patch("cli.images.subprocess.run", side_effect=fake_run),
    ):
        result = manager.build(str(ctx), name="my-app", tag="v1")

    assert any(cmd[0] == runtime and cmd[1] == "build" for cmd in captured), (
        f"Expected `{runtime} build` in captured argv: {captured}"
    )
    assert any(cmd[0] == runtime and cmd[1] == "push" for cmd in captured), (
        f"Expected `{runtime} push` in captured argv: {captured}"
    )
    assert result["runtime"] == runtime
    assert result["tag"] == "v1"
    assert result["repository"] == "gco/my-app"


def test_images_build_no_runtime_raises(manager: ImageManager, tmp_path: Any) -> None:
    """build() raises a descriptive error when no runtime is available."""
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM scratch\n")

    with (
        patch("cli.images.detect_container_runtime", return_value=None),
        pytest.raises(RuntimeError, match="container runtime"),
    ):
        manager.build(str(ctx), name="my-app", tag="v1")


def test_images_build_immutable_tag_rejection(manager: ImageManager, tmp_path: Any) -> None:
    """A second build of the same tag against an IMMUTABLE repo errors out."""
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM scratch\n")

    mock_ecr, _ = _ecr_with_repo_already_exists()
    mock_ecr.create_repository.return_value = {}
    mock_ecr.put_lifecycle_policy.return_value = {}
    mock_ecr.describe_repositories.return_value = {
        "repositories": [{"imageTagMutability": "IMMUTABLE"}],
    }
    mock_ecr.describe_images.return_value = {
        "imageDetails": [{"imageTags": ["v1"], "imageDigest": "sha256:abc"}],
    }
    mock_ecr.get_authorization_token.return_value = {
        "authorizationData": [{"authorizationToken": "QVdTOnRva2Vu"}]
    }

    with (
        patch("cli.images.detect_container_runtime", return_value="docker"),
        patch.object(manager, "_ecr_client", return_value=mock_ecr),
        patch("cli.images.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with pytest.raises(RuntimeError, match=r"already exists on immutable repo.*--tag"):
            manager.build(str(ctx), name="my-app", tag="v1")


# ---------------------------------------------------------------------------
# Region resolution + factory
# ---------------------------------------------------------------------------


def test_region_falls_back_to_global_when_unset(mock_config: Any, monkeypatch: Any) -> None:
    """Without an explicit region or AWS_DEFAULT_REGION, use the global region."""
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with patch("cli.images.get_config", return_value=mock_config):
        mgr = ImageManager(config=mock_config)
    assert mgr.region == mock_config.global_region


def test_region_uses_aws_default_region(mock_config: Any, monkeypatch: Any) -> None:
    """When config has no regions, AWS_DEFAULT_REGION wins."""
    config_no_regions = MagicMock(spec=["global_region", "project_name"])
    config_no_regions.global_region = "us-east-2"
    config_no_regions.project_name = "gco"
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    with patch("cli.images.get_config", return_value=config_no_regions):
        mgr = ImageManager(config=config_no_regions)
    assert mgr.region == "ap-northeast-1"


# ---------------------------------------------------------------------------
# Smoke checks for the validation regexes themselves
# ---------------------------------------------------------------------------


def test_name_regex_accepts_minimal():
    assert _NAME_RE.match("a")


def test_name_regex_rejects_empty():
    assert not _NAME_RE.match("")


def test_tag_regex_accepts_typical_versions():
    for tag in ("v1", "v1.2.3", "1.0.0-rc1", "main", "_init"):
        assert _TAG_RE.match(tag), f"expected {tag!r} to match _TAG_RE"


def test_digest_extraction_finds_sha256(manager: ImageManager) -> None:
    output = (
        "The push refers to repository [foo/bar]\nv1: digest: sha256:" + "a" * 64 + " size: 1234\n"
    )
    digest = manager._extract_digest(output)
    assert digest is not None
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", digest)


def test_digest_extraction_returns_none_when_absent(manager: ImageManager) -> None:
    assert manager._extract_digest("no digest here") is None
