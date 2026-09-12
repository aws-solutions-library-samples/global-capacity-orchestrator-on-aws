"""
Tests for scripts/live_release_validation/cleanup/local_images.py — the local
CDK asset image prune the deploy action runs after publishing.

Every runtime command is a recorded fake: the tests pin which image references
count as CDK assets (the ``cdkasset-<hash>`` tag in either registry form and the
``cdk-<qualifier>-container-assets-<account>-<region>`` ECR repository), that
everything else in the local store is left alone, that shared image ids are
removed once, that dangling layers are pruned, and that no failure — a missing
runtime, a failing command, or an unexpected exception — ever escapes into the
deploy verdict.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from scripts.live_release_validation.cleanup import local_images

_LISTING = "\n".join(
    [
        "docker.io/library/cdkasset-aaa111:latest sha-A",
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/cdk-hnb659fds-container-assets-123456789012-us-east-1:aaa111 sha-A",
        "cdkasset-bbb222:latest sha-B",
        "123456789012.dkr.ecr.us-east-2.amazonaws.com/cdk-abc12xyz-container-assets-123456789012-us-east-2:bbb222 sha-B",
        "public.ecr.aws/lambda/python:3.14 sha-C",
        "docker.io/library/python:3.14.7-slim sha-D",
        "localhost/my-app:cdkasset-lookalike sha-E",
        "<none>:<none> sha-F",
        "malformed line with too many fields here",
    ]
)


class _Recorder:
    """Fake ``subprocess.run`` returning scripted results per subcommand."""

    def __init__(self, results: dict[str, subprocess.CompletedProcess[str]]) -> None:
        self.results = results
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "check": False,
            "timeout": local_images._COMMAND_TIMEOUT_SECONDS,
        }
        return self.results[argv[1]]


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


class TestAssetRepositoryDetection:
    @pytest.mark.parametrize(
        "repository",
        [
            "cdkasset-aaa111",
            "docker.io/library/cdkasset-aaa111",
            "localhost/cdkasset-aaa111",
            "123456789012.dkr.ecr.us-east-1.amazonaws.com/cdk-hnb659fds-container-assets-123456789012-us-east-1",
            "cdk-hnb659fds-container-assets-123456789012-eu-central-1",
        ],
    )
    def test_cdk_asset_references_are_recognised(self, repository: str) -> None:
        assert local_images._is_cdk_asset_repository(repository) is True

    @pytest.mark.parametrize(
        "repository",
        [
            "public.ecr.aws/lambda/python",
            "docker.io/library/python",
            "localhost/my-app",
            "123456789012.dkr.ecr.us-east-1.amazonaws.com/gco/dockerhub/volcano",
            "cdk-hnb659fds-container-assets-123456789012",
            "notcdkasset-aaa111",
            "<none>",
        ],
    )
    def test_everything_else_is_left_alone(self, repository: str) -> None:
        assert local_images._is_cdk_asset_repository(repository) is False


class TestPruneCdkAssetImages:
    def test_removes_exactly_the_asset_images_once_per_id_and_prunes_dangling(self) -> None:
        run = _Recorder({"images": _ok(_LISTING), "rmi": _ok(), "image": _ok()})
        evidence = local_images.prune_cdk_asset_images(runtime="podman", run=run)
        assert evidence == {
            "runtime": "podman",
            "removed_images": [
                "123456789012.dkr.ecr.us-east-1.amazonaws.com/cdk-hnb659fds-container-assets-123456789012-us-east-1:aaa111",
                "123456789012.dkr.ecr.us-east-2.amazonaws.com/cdk-abc12xyz-container-assets-123456789012-us-east-2:bbb222",
                "cdkasset-bbb222:latest",
                "docker.io/library/cdkasset-aaa111:latest",
            ],
            "dangling_pruned": True,
            "errors": [],
        }
        assert run.calls == [
            ["podman", "images", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"],
            # One force-remove for both ids; the shared tags fall with them.
            ["podman", "rmi", "-f", "sha-A", "sha-B"],
            ["podman", "image", "prune", "-f"],
        ]

    def test_nothing_to_remove_still_prunes_dangling_layers(self) -> None:
        run = _Recorder({"images": _ok("python:3.14 sha-D\n"), "image": _ok()})
        evidence = local_images.prune_cdk_asset_images(runtime="docker", run=run)
        assert evidence["removed_images"] == []
        assert evidence["dangling_pruned"] is True
        assert [call[1] for call in run.calls] == ["images", "image"]

    def test_listing_failure_is_reported_and_dangling_prune_still_runs(self) -> None:
        run = _Recorder({"images": _fail("cannot connect to the daemon"), "image": _ok()})
        evidence = local_images.prune_cdk_asset_images(runtime="docker", run=run)
        assert evidence["removed_images"] == []
        assert evidence["errors"] == ["images: cannot connect to the daemon"]
        assert evidence["dangling_pruned"] is True

    def test_remove_and_prune_failures_are_captured_not_raised(self) -> None:
        run = _Recorder(
            {
                "images": _ok("cdkasset-aaa111:latest sha-A\n"),
                "rmi": _fail("image is in use by a container " + "x" * 600),
                "image": _fail("prune failed"),
            }
        )
        evidence = local_images.prune_cdk_asset_images(runtime="docker", run=run)
        assert evidence["removed_images"] == ["cdkasset-aaa111:latest"]
        assert evidence["dangling_pruned"] is False
        assert len(evidence["errors"]) == 2
        assert evidence["errors"][0].startswith("rmi: image is in use")
        assert len(evidence["errors"][0]) <= len("rmi: ") + local_images._MAX_ERROR_CHARS
        assert evidence["errors"][1] == "image prune: prune failed"

    def test_detects_the_runtime_when_none_is_given(self) -> None:
        run = _Recorder({"images": _ok(""), "image": _ok()})
        with patch.object(local_images, "detect_container_runtime", return_value="finch"):
            evidence = local_images.prune_cdk_asset_images(run=run)
        assert evidence["runtime"] == "finch"
        assert run.calls[0][0] == "finch"

    def test_no_runtime_is_a_skip_not_an_error(self) -> None:
        run = _Recorder({})
        with patch.object(local_images, "detect_container_runtime", return_value=None):
            evidence = local_images.prune_cdk_asset_images(run=run)
        assert evidence == {
            "runtime": None,
            "removed_images": [],
            "dangling_pruned": False,
            "errors": [],
            "skipped": "no container runtime detected",
        }
        assert run.calls == []


class TestSafeWrapper:
    def test_delegates_to_the_prune(self) -> None:
        with patch.object(
            local_images, "prune_cdk_asset_images", return_value={"runtime": "docker"}
        ) as prune:
            assert local_images.prune_local_cdk_asset_images_safely() == {"runtime": "docker"}
        prune.assert_called_once_with()

    def test_unexpected_exceptions_become_evidence(self) -> None:
        with patch.object(
            local_images,
            "prune_cdk_asset_images",
            side_effect=subprocess.TimeoutExpired(cmd="podman images", timeout=300),
        ):
            evidence = local_images.prune_local_cdk_asset_images_safely()
        assert evidence["runtime"] is None
        assert evidence["removed_images"] == []
        assert evidence["dangling_pruned"] is False
        assert evidence["errors"][0].startswith("TimeoutExpired:")
