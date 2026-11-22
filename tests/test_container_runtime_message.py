"""The "no container runtime" message distinguishes absent from not-running.

Detection requires a runtime that answers ``<runtime> info``, not merely one
that is installed, so those two states need different advice. Telling someone
to install Finch on a host that already has it — because its VM happened to be
stopped — sends them to a page they have already followed. That happened during
a live release-validation run on 2026-08-26 and cost a full deploy attempt, so
the branch is pinned here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import cli._container_runtime as container_runtime
from cli._container_runtime import container_runtime_error_message


@pytest.fixture(autouse=True)
def _outside_the_dev_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """Judge the host branches the same way wherever the suite runs.

    ``Dockerfile.dev`` sets ``GCO_DEV_CONTAINER=1`` and, in older images, the
    npm prefix ``cli.stacks._running_in_dev_container`` also accepts, so a
    run inside ``gco-dev`` would otherwise take the container branch.
    """
    monkeypatch.delenv("GCO_DEV_CONTAINER", raising=False)
    monkeypatch.delenv("NPM_CONFIG_PREFIX", raising=False)


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


class TestInsideTheDevContainer:
    """In ``gco-dev`` the advice is about the socket the ``gco`` function mounts.

    The container's PATH has the Docker CLI, so the host branch would name
    Docker as installed-but-stopped and say to start Docker Desktop, even on a
    Finch host whose function mounted no socket. A walkthrough of the Learning
    Path on a Finch host got exactly that from `deploy-all`.
    """

    @pytest.fixture(autouse=True)
    def _in_dev_container(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("GCO_DEV_CONTAINER", "1")
        self.socket = tmp_path / "docker.sock"
        monkeypatch.setattr(container_runtime, "DEV_CONTAINER_SOCKET", str(self.socket))

    def test_no_socket_names_the_finch_route_and_the_setup_script(self) -> None:
        with patch("shutil.which", _which("docker")):
            message = container_runtime_error_message()
        assert "No container runtime is reachable from the gco-dev container" in message
        assert "finch-daemon, which Finch runs in its VM from v1.8.0" in message
        assert "CDK_DOCKER=finch" in message
        assert "Installing on your host instead" in message
        assert "./scripts/setup-dev-alias.sh" in message
        assert "Installed with --no-socket: re-run ./scripts/setup-dev-alias.sh without it" in message
        assert "open -a Docker" not in message
        assert "docs.docker.com" not in message

    def test_a_mounted_socket_points_at_the_runtime_behind_it(self) -> None:
        self.socket.touch()
        with patch("shutil.which", _which("docker")):
            message = container_runtime_error_message()
        assert (
            f"answered on the socket the gco function mounts in the gco-dev container ({self.socket})"
            in message
        )
        assert "colima start" in message
        assert "podman machine start" in message
        assert "finch vm stop && finch vm start" in message
        assert "CDK_DOCKER=finch" not in message

    def test_an_image_from_before_the_marker_counts_as_the_dev_container(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GCO_DEV_CONTAINER")
        monkeypatch.setenv("NPM_CONFIG_PREFIX", "/root/.npm-global")
        assert "gco-dev container" in container_runtime_error_message()

    def test_no_cdk_docker_path_hint_in_the_container(self) -> None:
        """A binary path cannot reach a daemon the container has no socket for."""
        with patch("shutil.which", _which("docker")):
            assert "CDK_DOCKER=<path>" not in container_runtime_error_message(allow_cdk_docker=True)

    def test_both_messages_start_like_the_host_ones(self) -> None:
        """Callers and tests match on the shared opening words."""
        without_socket = container_runtime_error_message()
        self.socket.touch()
        with_socket = container_runtime_error_message()
        assert without_socket.startswith("No container runtime")
        assert with_socket.startswith("No container runtime")
