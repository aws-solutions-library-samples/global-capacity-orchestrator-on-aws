"""The "no container runtime" message distinguishes absent from not-running.

Detection requires a runtime that answers ``<runtime> info``, not merely one
that is installed, so those two states need different advice. Telling someone
to install Finch on a host that already has it — because its VM happened to be
stopped — sends them to a page they have already followed. That happened during
a live release-validation run on 2026-08-26 and cost a full deploy attempt, so
the branch is pinned here.
"""

from __future__ import annotations

from unittest.mock import patch

from cli._container_runtime import container_runtime_error_message


def _which(*present: str):
    """Fake shutil.which that reports only *present* binaries."""
    return lambda name: f"/usr/local/bin/{name}" if name in present else None


class TestNothingInstalled:
    def test_advises_installing(self) -> None:
        with patch("shutil.which", _which()):
            message = container_runtime_error_message()

        assert "No container runtime found" in message
        assert "docs.docker.com" in message
        assert "brew install finch" in message
        assert "podman.io" in message

    def test_does_not_claim_something_is_installed(self) -> None:
        with patch("shutil.which", _which()):
            message = container_runtime_error_message()
        assert "did not respond" not in message


class TestInstalledButNotRunning:
    def test_names_what_is_installed_and_how_to_start_it(self) -> None:
        with patch("shutil.which", _which("finch")):
            message = container_runtime_error_message()

        assert "No container runtime is running" in message
        assert "finch" in message
        assert "finch vm start" in message

    def test_does_not_advise_installing_what_is_already_there(self) -> None:
        """The bug being fixed: an install hint for an installed runtime."""
        with patch("shutil.which", _which("finch")):
            message = container_runtime_error_message()

        assert "brew install finch" not in message
        assert "docs.docker.com" not in message

    def test_lists_every_installed_runtime(self) -> None:
        with patch("shutil.which", _which("docker", "podman")):
            message = container_runtime_error_message()

        assert "docker" in message
        assert "podman" in message
        assert "podman machine start" in message
        # Finch is absent here, so its start hint must not appear.
        assert "finch vm start" not in message

    def test_mentions_the_probe_timeout(self) -> None:
        """A VM mid-boot also reports unavailable; say so rather than misleading."""
        with patch("shutil.which", _which("finch")):
            message = container_runtime_error_message()
        assert "5s timeout" in message


class TestCdkDockerHint:
    def test_offered_only_where_it_applies(self) -> None:
        """CDK_DOCKER is honored by the image path, so only that caller offers it."""
        with patch("shutil.which", _which("finch")):
            assert "CDK_DOCKER" in container_runtime_error_message(allow_cdk_docker=True)
            assert "CDK_DOCKER" not in container_runtime_error_message()

    def test_offered_in_the_not_installed_branch_too(self) -> None:
        with patch("shutil.which", _which()):
            assert "CDK_DOCKER" in container_runtime_error_message(allow_cdk_docker=True)
