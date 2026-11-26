"""Unit tests for ``cli/dashboard/container.py``, which runs the dashboard in a container of its own.

Every runtime command is faked at the ``subprocess.run`` boundary by a
recorder that answers by subcommand, so no Docker, Finch or Podman runs, and
the tests assert the exact command lines. The ``run`` command line gets the
closest look: it carries the container's whole security posture (loopback-only
publish, read-only root filesystem and mounts, no capabilities, no privilege
escalation, the caller's uid), tells the server the host port its port is
published on, and must never carry a secret value. The state
file lives under ``tmp_path`` (``GCO_DASHBOARD_STATE``, or ``HOME`` for the
default path), and the health poll runs on a fake clock. The PID check asks
the real kernel about this process and one that has exited; another user's
process and Windows are faked.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli.dashboard import container
from cli.dashboard.container import STATE_ENV, ContainerError, DashboardState
from cli.dashboard.server import BROWSER_PORT_ENV, DEFAULT_PORT, TOKEN_ENV

RUNTIMES = ("docker", "finch", "podman")
TOKEN = "token-value-never-on-argv"
SECRET = "example-secret-value"
LABEL_FORMAT = '{{ index .Config.Labels "io.gco.dashboard.source" }}'
CAPTURED = {"capture_output": True, "text": True, "check": False, "timeout": 120, "env": None}


class FakeRuntime:
    """Stands in for ``subprocess.run``: records each call and answers by subcommand.

    ``answers`` maps the word after the runtime (``inspect``, ``image``,
    ``rm``, ``run``, ``build``, ``logs``) to ``(returncode, stdout, stderr)``
    or to an exception to raise; anything unlisted succeeds silently.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.answers: dict[str, tuple[int, str, str] | BaseException] = {}

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        answer = self.answers.get(argv[1], (0, "", ""))
        if isinstance(answer, BaseException):
            raise answer
        returncode, stdout, stderr = answer
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


@pytest.fixture(autouse=True)
def run(monkeypatch: pytest.MonkeyPatch) -> FakeRuntime:
    """Every test runs against the fake runtime, so no real container command can start.

    The module catches ``subprocess.TimeoutExpired`` by that name, so the
    stand-in carries the real class beside the fake ``run``.
    """
    fake = FakeRuntime()
    monkeypatch.setattr(
        container,
        "subprocess",
        SimpleNamespace(run=fake, TimeoutExpired=subprocess.TimeoutExpired),
    )
    return fake


@pytest.fixture(autouse=True)
def state_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The state file and HOME both live under ``tmp_path``."""
    path = tmp_path / "state" / "dashboard.json"
    monkeypatch.setenv(STATE_ENV, str(path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return path


@pytest.fixture
def ids(monkeypatch: pytest.MonkeyPatch) -> str:
    """A fixed caller uid:gid, so command lines are predictable on any host."""
    monkeypatch.setattr(os, "getuid", lambda: 1234, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 5678, raising=False)
    return "1234:5678"


def _values(argv: list[str], flag: str) -> list[str]:
    """Every value given to ``flag`` in ``argv``."""
    return [argv[index + 1] for index, item in enumerate(argv) if item == flag]


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def _checkout(root: Path) -> Path:
    """The two markers ``find_build_context`` looks for."""
    return _tree(root, {"Dockerfile.dashboard": b"FROM scratch\n", "cli/dashboard/app.py": b""})


def test_names_ports_and_the_state_override() -> None:
    assert (container.NAME, container.IMAGE_TAG) == ("gco-dashboard", "local")
    assert container.CONTAINER_PORT == DEFAULT_PORT == 8501
    assert STATE_ENV == "GCO_DASHBOARD_STATE"


@pytest.mark.parametrize(
    ("runtime", "ref"),
    [
        ("docker", "gco-dashboard:local"),
        ("finch", "gco-dashboard:local"),
        ("podman", "localhost/gco-dashboard:local"),
    ],
)
def test_image_ref_is_local_for_each_runtime(runtime: str, ref: str) -> None:
    assert container.image_ref(runtime) == ref


# ---------------------------------------------------------------------------
# Build context and source fingerprint
# ---------------------------------------------------------------------------


class TestBuildContext:
    def test_finds_the_checkout_from_a_directory_inside_it(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path / "checkout")

        assert container.find_build_context(checkout / "cli" / "dashboard") == checkout
        assert container.find_build_context(checkout) == checkout

    def test_the_first_candidate_holding_a_checkout_wins(self, tmp_path: Path) -> None:
        first = _checkout(tmp_path / "first")
        second = _checkout(tmp_path / "second")
        (tmp_path / "elsewhere").mkdir()

        assert container.find_build_context(tmp_path / "elsewhere", second, first) == second

    @pytest.mark.parametrize("removed", ["Dockerfile.dashboard", "cli"])
    def test_needs_both_the_dockerfile_and_the_cli_tree(self, tmp_path: Path, removed: str) -> None:
        checkout = _checkout(tmp_path / "checkout")
        target = checkout / removed
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

        assert container.find_build_context(checkout) is None

    def test_no_candidates_find_nothing(self) -> None:
        assert container.find_build_context() is None

    def test_defaults_are_this_cli_checkout_then_the_working_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        repository = Path(container.__file__).resolve().parents[2]

        assert container.default_build_contexts() == (repository, Path.cwd())
        assert container.find_build_context(*container.default_build_contexts()) == repository


class TestSourceFingerprint:
    def test_hashes_each_inputs_relative_path_and_bytes(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"pyproject.toml": b"[project]\n", "cli/a.py": b"A = 1\n"})

        expected = hashlib.sha256(b"pyproject.toml\0[project]\n\0cli/a.py\0A = 1\n\0").hexdigest()
        assert container.source_fingerprint(root) == expected

    def test_inputs_are_the_build_files_and_trees_without_caches_or_bytecode(
        self, tmp_path: Path
    ) -> None:
        root = _tree(
            tmp_path,
            {
                "Dockerfile.dashboard": b"FROM scratch\n",
                "pyproject.toml": b"",
                "cli/main.py": b"",
                "cli/dashboard/app.py": b"",
                "cli/__pycache__/main.cpython-314.pyc": b"",
                "cli/dashboard/stale.pyc": b"",
                "cli/dashboard/stale.pyo": b"",
                "cli/.ruff_cache/0.17/index": b"",
                "gco/.mypy_cache/meta.json": b"",
                "gco/.pytest_cache/v/lastfailed": b"",
                "gco/models.py": b"",
                "dockerfiles/build_scratch_rootfs.py": b"",
                "dockerfiles/unrelated.py": b"",
                "README.md": b"",
                "tests/test_x.py": b"",
            },
        )

        files = [path.relative_to(root).as_posix() for path in container._input_files(root)]

        assert files == [
            "Dockerfile.dashboard",
            "pyproject.toml",
            "cli/dashboard/app.py",
            "cli/main.py",
            "gco/models.py",
            "dockerfiles/build_scratch_rootfs.py",
        ]

    def test_changes_with_any_input_byte_or_path(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"cli/a.py": b"A = 1\n"})
        original = container.source_fingerprint(root)

        (root / "cli" / "a.py").write_bytes(b"A = 2\n")
        edited = container.source_fingerprint(root)
        (root / "cli" / "a.py").rename(root / "cli" / "b.py")
        renamed = container.source_fingerprint(root)

        assert len({original, edited, renamed}) == 3

    def test_ignores_caches_bytecode_and_files_outside_the_inputs(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"cli/a.py": b"A = 1\n"})
        original = container.source_fingerprint(root)

        _tree(
            root,
            {
                "cli/__pycache__/a.cpython-314.pyc": b"\x00",
                "cli/a.pyc": b"\x00",
                "README.md": b"docs",
                "tests/test_a.py": b"",
            },
        )

        assert container.source_fingerprint(root) == original

    def test_a_tree_without_inputs_has_the_empty_digest(self, tmp_path: Path) -> None:
        assert container.source_fingerprint(tmp_path) == hashlib.sha256().hexdigest()


# ---------------------------------------------------------------------------
# Runtime commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "stderr", "message"),
    [
        ("", "warning: slow\nError: no such container\n", ": Error: no such container"),
        ("only stdout\n", "", ": only stdout"),
        ("", "  \n", ""),
        (None, None, ""),
    ],
    ids=["last-stderr-line", "stdout-fallback", "blank", "uncaptured"],
)
def test_a_failure_names_the_command_its_exit_and_last_output_line(
    stdout: str | None, stderr: str | None, message: str
) -> None:
    argv = ["docker", "rm", "-f", "gco-dashboard"]
    result = subprocess.CompletedProcess(argv, 1, stdout, stderr)

    error = container._failure(argv, result)

    assert isinstance(error, ContainerError)
    assert str(error) == f"'docker rm -f' failed (exit 1){message}"


def test_commands_get_a_copy_of_the_environment_they_are_given(run: FakeRuntime) -> None:
    environ = {"PATH": "/usr/bin"}

    container._run(["docker", "info"], env=environ, timeout=None)

    ((argv, kwargs),) = run.calls
    assert argv == ["docker", "info"]
    assert kwargs == {**CAPTURED, "timeout": None, "env": environ}
    assert kwargs["env"] is not environ


def _start(runtime: str) -> None:
    container.start_container(
        runtime, port=8501, token=TOKEN, project_dir=Path("/home/tester/gco"), aws_dir=None
    )


class TestRuntimeThatCannotAnswer:
    """A runtime that hangs or cannot be executed raises ``ContainerError``, as a failure does.

    ``subprocess.run`` stops a command at its timeout by raising
    ``TimeoutExpired``, and raises ``OSError`` when the runtime's executable
    is missing; either would otherwise end a ``gco dashboard`` command in a
    traceback.
    """

    @pytest.mark.parametrize(
        ("verb", "call", "command"),
        [
            ("image", lambda: container.image_fingerprint("docker"), "docker image inspect"),
            ("inspect", lambda: container.container_status("docker"), "docker inspect --format"),
            ("rm", lambda: container.remove_container("docker"), "docker rm -f"),
            ("run", lambda: _start("docker"), "docker run --detach"),
        ],
        ids=["image-fingerprint", "container-status", "remove", "start"],
    )
    def test_a_command_stopped_at_its_timeout_names_itself_and_the_runtime(
        self, run: FakeRuntime, verb: str, call: Callable[[], object], command: str
    ) -> None:
        run.answers["inspect"] = (0, "exited\n", "")  # removal and start find a container first
        run.answers[verb] = subprocess.TimeoutExpired(["docker", verb], 120)

        with pytest.raises(ContainerError) as excinfo:
            call()

        assert str(excinfo.value) == (
            f"'{command}' did not finish within 120s and was stopped; "
            "check that docker is responding"
        )
        assert isinstance(excinfo.value.__cause__, subprocess.TimeoutExpired)
        assert run.calls[-1][1]["timeout"] == 120

    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            (
                FileNotFoundError(2, "No such file or directory", "finch"),
                "No such file or directory",
            ),
            (PermissionError(13, "Permission denied", "finch"), "Permission denied"),
            (OSError("exec format error"), "exec format error"),
        ],
        ids=["missing", "not-executable", "no-errno"],
    )
    def test_a_runtime_that_cannot_be_executed_names_it_and_why(
        self, run: FakeRuntime, error: OSError, reason: str
    ) -> None:
        """A state file can name a runtime this machine (or the dev image) does not have."""
        run.answers["inspect"] = error

        with pytest.raises(ContainerError, match=rf"^'finch' could not run: {reason}$") as excinfo:
            container.container_status("finch")

        assert excinfo.value.__cause__ is error


class TestImageFingerprint:
    @pytest.mark.parametrize("runtime", RUNTIMES)
    def test_reads_the_source_label_of_the_local_image(
        self, run: FakeRuntime, runtime: str
    ) -> None:
        run.answers["image"] = (0, "abc123\n", "")

        assert container.image_fingerprint(runtime) == "abc123"
        assert run.calls == [
            (
                [
                    runtime,
                    "image",
                    "inspect",
                    "--format",
                    LABEL_FORMAT,
                    container.image_ref(runtime),
                ],
                CAPTURED,
            )
        ]

    @pytest.mark.parametrize(
        "answer",
        [
            (1, "", "Error: No such image: gco-dashboard:local"),
            (0, "<no value>\n", ""),
            (0, "\n", ""),
        ],
        ids=["no-image", "no-label", "empty-label"],
    )
    def test_is_none_without_an_image_or_its_label(
        self, run: FakeRuntime, answer: tuple[int, str, str]
    ) -> None:
        run.answers["image"] = answer

        assert container.image_fingerprint("docker") is None


class TestBuildImage:
    @pytest.mark.parametrize("runtime", RUNTIMES)
    def test_builds_the_checkout_with_its_fingerprint_label_streaming_progress(
        self, run: FakeRuntime, tmp_path: Path, runtime: str
    ) -> None:
        fingerprint = "f" * 64

        container.build_image(runtime, tmp_path, fingerprint)

        assert run.calls == [
            (
                [
                    runtime,
                    "build",
                    "-f",
                    str(tmp_path / "Dockerfile.dashboard"),
                    "-t",
                    container.image_ref(runtime),
                    "--label",
                    f"io.gco.dashboard.source={fingerprint}",
                    str(tmp_path),
                ],
                {"check": False},
            )
        ]

    def test_a_failed_build_raises(self, run: FakeRuntime, tmp_path: Path) -> None:
        run.answers["build"] = (2, "", "")

        with pytest.raises(ContainerError, match=r"^'docker build' failed \(exit 2\); see above$"):
            container.build_image("docker", tmp_path, "abc")


class TestContainerLifecycle:
    def test_status_reads_the_named_containers_state(self, run: FakeRuntime) -> None:
        run.answers["inspect"] = (0, "running\n", "")

        assert container.container_status("docker") == "running"
        assert run.calls == [
            (["docker", "inspect", "--format", "{{.State.Status}}", "gco-dashboard"], CAPTURED)
        ]

    @pytest.mark.parametrize(
        "answer",
        [(1, "", "Error: No such object: gco-dashboard"), (0, "\n", "")],
        ids=["absent", "blank"],
    )
    def test_status_is_none_without_a_container(
        self, run: FakeRuntime, answer: tuple[int, str, str]
    ) -> None:
        run.answers["inspect"] = answer

        assert container.container_status("finch") is None

    def test_remove_does_nothing_without_a_container(self, run: FakeRuntime) -> None:
        run.answers["inspect"] = (1, "", "Error: No such object: gco-dashboard")

        assert container.remove_container("docker") is False
        assert [argv[1] for argv in run.argvs()] == ["inspect"]

    @pytest.mark.parametrize("runtime", RUNTIMES)
    def test_remove_force_removes_an_existing_container(
        self, run: FakeRuntime, runtime: str
    ) -> None:
        run.answers["inspect"] = (0, "exited\n", "")

        assert container.remove_container(runtime) is True
        assert run.calls[-1] == ([runtime, "rm", "-f", "gco-dashboard"], CAPTURED)

    def test_a_failed_removal_raises_with_the_runtimes_message(self, run: FakeRuntime) -> None:
        run.answers["inspect"] = (0, "running\n", "")
        run.answers["rm"] = (1, "", "Error response from daemon: removal in progress\n")

        with pytest.raises(
            ContainerError,
            match=r"^'docker rm -f' failed \(exit 1\): Error response from daemon: removal in progress$",
        ):
            container.remove_container("docker")


# ---------------------------------------------------------------------------
# The run command line
# ---------------------------------------------------------------------------

ENVIRON = {
    "AWS_PROFILE": "dev",
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "",
    "AWS_SECRET_ACCESS_KEY": SECRET,
    "GCO_PROJECT_NAME": "gco",
    "HOME": "/home/tester",
    "UNRELATED": "x",
    TOKEN_ENV: TOKEN,
}


def _run_args(runtime: str, tmp_path: Path, **overrides: Any) -> list[str]:
    aws = tmp_path / "aws"
    aws.mkdir(exist_ok=True)
    options: dict[str, Any] = {
        "port": 8501,
        "project_dir": tmp_path / "checkout",
        "aws_dir": aws,
        "environ": ENVIRON,
    }
    options.update(overrides)
    return container.run_args(runtime, **options)


class TestRunArgs:
    def test_the_full_docker_command_line(self, tmp_path: Path, ids: str) -> None:
        argv = _run_args("docker", tmp_path, port=9000)

        assert argv == [
            "docker",
            "run",
            "--detach",
            "--name",
            "gco-dashboard",
            "--label",
            "io.gco.dashboard=1",
            "--publish",
            "127.0.0.1:9000:8501",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "1234:5678",
            "--env",
            "HOME=/home/gco",
            "--env",
            "USER=gco",
            "--env",
            "GCO_DASHBOARD_BROWSER_PORT=9000",
            "--env",
            "GCO_DASHBOARD_TOKEN",
            "--env",
            "AWS_PROFILE",
            "--env",
            "AWS_REGION",
            "--env",
            "AWS_SECRET_ACCESS_KEY",
            "--env",
            "GCO_PROJECT_NAME",
            "--volume",
            f"{tmp_path / 'aws'}:/home/gco/.aws:ro",
            "--volume",
            f"{tmp_path / 'checkout'}:/workspace:ro",
            "--workdir",
            "/workspace",
            "gco-dashboard:local",
        ]

    @pytest.mark.parametrize("runtime", RUNTIMES)
    def test_security_posture_holds_on_every_runtime(
        self, tmp_path: Path, ids: str, runtime: str
    ) -> None:
        argv = _run_args(runtime, tmp_path)

        assert _values(argv, "--publish") == ["127.0.0.1:8501:8501"]
        assert "--read-only" in argv
        assert _values(argv, "--tmpfs") == ["/tmp"]
        assert _values(argv, "--cap-drop") == ["ALL"]
        assert _values(argv, "--security-opt") == ["no-new-privileges"]
        volumes = _values(argv, "--volume")
        assert len(volumes) == 2
        assert all(volume.endswith(":ro") for volume in volumes)
        assert "--privileged" not in argv

    @pytest.mark.parametrize("runtime", RUNTIMES)
    def test_only_fixed_values_and_the_port_reach_the_command_line(
        self, tmp_path: Path, ids: str, runtime: str
    ) -> None:
        argv = _run_args(runtime, tmp_path)

        assert [item for item in _values(argv, "--env") if "=" in item] == [
            "HOME=/home/gco",
            "USER=gco",
            "GCO_DASHBOARD_BROWSER_PORT=8501",
        ]
        assert TOKEN_ENV in _values(argv, "--env")
        assert not any(TOKEN in item or SECRET in item for item in argv)
        assert "dev" not in argv

    @pytest.mark.parametrize("runtime", RUNTIMES)
    @pytest.mark.parametrize("port", [8501, 8502, 9000])
    def test_the_server_is_told_the_host_port_its_port_is_published_on(
        self, tmp_path: Path, ids: str, runtime: str, port: int
    ) -> None:
        """Streamlit in the container then prints the address a browser uses, not 8501."""
        environ = {**ENVIRON, BROWSER_PORT_ENV: "7777"}  # a stale host value is not forwarded

        argv = _run_args(runtime, tmp_path, port=port, environ=environ)

        assert _values(argv, "--publish") == [f"127.0.0.1:{port}:8501"]
        assert [item for item in _values(argv, "--env") if BROWSER_PORT_ENV in item] == [
            f"GCO_DASHBOARD_BROWSER_PORT={port}"
        ]

    @pytest.mark.parametrize("runtime", ["docker", "finch"])
    def test_docker_and_finch_run_as_the_callers_uid_and_gid(
        self, tmp_path: Path, ids: str, runtime: str
    ) -> None:
        argv = _run_args(runtime, tmp_path)

        assert _values(argv, "--user") == [ids]
        assert "--userns" not in argv
        assert argv[-1] == "gco-dashboard:local"

    def test_podman_keeps_the_callers_uid_through_its_user_namespace(
        self, tmp_path: Path, ids: str
    ) -> None:
        argv = _run_args("podman", tmp_path)

        assert _values(argv, "--userns") == ["keep-id"]
        assert "--user" not in argv
        assert argv[-1] == "localhost/gco-dashboard:local"

    @pytest.mark.parametrize("missing", ["getuid", "getgid"])
    def test_without_posix_ids_the_runtime_maps_the_user_itself(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str
    ) -> None:
        """Windows has no ``os.getuid``/``os.getgid``."""
        monkeypatch.delattr(os, missing)

        argv = _run_args("docker", tmp_path)

        assert "--user" not in argv
        assert "--userns" not in argv

    @pytest.mark.parametrize("aws_dir", [None, "missing"], ids=["none", "absent"])
    def test_aws_configuration_is_mounted_only_when_it_exists(
        self, tmp_path: Path, ids: str, aws_dir: str | None
    ) -> None:
        argv = _run_args(
            "docker", tmp_path, aws_dir=None if aws_dir is None else tmp_path / aws_dir
        )

        assert _values(argv, "--volume") == [f"{tmp_path / 'checkout'}:/workspace:ro"]

    def test_only_variables_set_on_the_host_are_forwarded(self, tmp_path: Path, ids: str) -> None:
        environ = dict.fromkeys(container.FORWARDED_ENV, "") | {"AWS_REGION": "eu-west-1"}

        argv = _run_args("docker", tmp_path, environ=environ)

        assert _values(argv, "--env") == [
            "HOME=/home/gco",
            "USER=gco",
            "GCO_DASHBOARD_BROWSER_PORT=8501",
            TOKEN_ENV,
            "AWS_REGION",
        ]

    def test_every_forwarded_variable_is_passed_by_name_in_order(
        self, tmp_path: Path, ids: str
    ) -> None:
        environ = dict.fromkeys(container.FORWARDED_ENV, "set")

        argv = _run_args("docker", tmp_path, environ=environ)

        assert _values(argv, "--env")[4:] == list(container.FORWARDED_ENV)


class TestStartContainer:
    def test_runs_the_container_with_the_token_in_its_environment_only(
        self, run: FakeRuntime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ids: str
    ) -> None:
        monkeypatch.setenv("AWS_PROFILE", "dev")
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        run.answers["inspect"] = (1, "", "Error: No such object: gco-dashboard")

        container.start_container(
            "docker", port=9000, token=TOKEN, project_dir=tmp_path, aws_dir=None
        )

        (inspect, _), (argv, kwargs) = run.calls
        assert inspect[:2] == ["docker", "inspect"]
        assert kwargs["env"][TOKEN_ENV] == TOKEN
        assert kwargs["env"]["AWS_PROFILE"] == "dev"
        assert kwargs["timeout"] == 120
        assert argv == container.run_args(
            "docker", port=9000, project_dir=tmp_path, aws_dir=None, environ=kwargs["env"]
        )
        assert {TOKEN_ENV, "AWS_PROFILE", "GCO_DASHBOARD_BROWSER_PORT=9000"} <= set(
            _values(argv, "--env")
        )
        assert not any(TOKEN in item for item in argv)
        assert TOKEN_ENV not in os.environ  # the caller's environment is left alone

    def test_without_a_token_none_is_inherited_from_the_caller(
        self, run: FakeRuntime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(TOKEN_ENV, "left-over")
        run.answers["inspect"] = (1, "", "")

        container.start_container(
            "docker", port=8501, token=None, project_dir=tmp_path, aws_dir=None
        )

        _, (_, kwargs) = run.calls
        assert TOKEN_ENV not in kwargs["env"]
        assert os.environ[TOKEN_ENV] == "left-over"

    @pytest.mark.parametrize("runtime", RUNTIMES)
    def test_a_previous_container_is_replaced(
        self, run: FakeRuntime, tmp_path: Path, runtime: str
    ) -> None:
        run.answers["inspect"] = (0, "exited\n", "")

        container.start_container(
            runtime, port=8501, token=TOKEN, project_dir=tmp_path, aws_dir=None
        )

        assert [argv[:2] for argv in run.argvs()] == [
            [runtime, "inspect"],
            [runtime, "rm"],
            [runtime, "run"],
        ]

    def test_a_failed_start_raises_with_the_runtimes_message_and_no_token(
        self, run: FakeRuntime, tmp_path: Path
    ) -> None:
        run.answers["inspect"] = (1, "", "")
        run.answers["run"] = (125, "", "docker: Error response from daemon: port is allocated.\n")

        with pytest.raises(ContainerError) as excinfo:
            container.start_container(
                "docker", port=8501, token=TOKEN, project_dir=tmp_path, aws_dir=None
            )

        assert str(excinfo.value) == (
            "'docker run --detach' failed (exit 125): "
            "docker: Error response from daemon: port is allocated."
        )


# ---------------------------------------------------------------------------
# Health wait and logs
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(
        container, "time", SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep)
    )
    return fake


def _health_answers(monkeypatch: pytest.MonkeyPatch, answers: Iterable[bool]) -> list[int]:
    polled: list[int] = []
    replies = iter(answers)

    def fake_healthy(port: int) -> bool:
        polled.append(port)
        return next(replies)

    monkeypatch.setattr(container, "healthy", fake_healthy)
    return polled


class TestWaitUntilHealthy:
    def test_returns_once_the_dashboard_answers(
        self, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
    ) -> None:
        polled = _health_answers(monkeypatch, [False, False, True])

        assert container.wait_until_healthy(9000, timeout=10, interval=0.5) is True
        assert polled == [9000, 9000, 9000]
        assert clock.sleeps == [0.5, 0.5]

    def test_gives_up_after_a_last_poll_at_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
    ) -> None:
        polled = _health_answers(monkeypatch, itertools.repeat(False))

        assert container.wait_until_healthy(9000, timeout=3, interval=1) is False
        assert len(polled) == 4
        assert clock.sleeps == [1, 1, 1]

    def test_by_default_polls_every_second_for_ninety_seconds(
        self, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
    ) -> None:
        _health_answers(monkeypatch, itertools.repeat(False))

        assert container.wait_until_healthy(8501) is False
        assert clock.sleeps == [1.0] * 90


class TestStreamLogs:
    @pytest.mark.parametrize(
        ("follow", "tail", "flags"),
        [
            (False, None, []),
            (True, None, ["--follow"]),
            (False, 50, ["--tail", "50"]),
            (True, 0, ["--follow", "--tail", "0"]),
        ],
    )
    def test_streams_the_containers_logs_and_returns_the_exit_code(
        self, run: FakeRuntime, follow: bool, tail: int | None, flags: list[str]
    ) -> None:
        run.answers["logs"] = (3, "", "")

        assert container.stream_logs("podman", follow=follow, tail=tail) == 3
        assert run.calls == [(["podman", "logs", *flags, "gco-dashboard"], {"check": False})]

    def test_ctrl_c_ends_following_cleanly(self, run: FakeRuntime) -> None:
        run.answers["logs"] = KeyboardInterrupt()

        assert container.stream_logs("docker", follow=True, tail=None) == 0


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------


def _state(**overrides: Any) -> DashboardState:
    values: dict[str, Any] = {
        "mode": "container",
        "port": 9000,
        "token": TOKEN,
        "project_dir": "/home/tester/gco",
        "started_at": "2026-11-25T18:00:00+00:00",
        "runtime": "docker",
        "container": "gco-dashboard",
        "image": "gco-dashboard:local",
    }
    values.update(overrides)
    return DashboardState(**values)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestStateFile:
    def test_path_defaults_to_the_users_gco_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv(STATE_ENV)

        assert container.state_path() == tmp_path / "home" / ".gco" / "dashboard.json"

    def test_path_follows_the_override(self, state_file: Path) -> None:
        assert container.state_path() == state_file

    def test_save_writes_an_owner_only_file_that_loads_back(self, state_file: Path) -> None:
        state = _state()

        container.save_state(state)

        assert _mode(state_file) == 0o600
        assert json.loads(state_file.read_text(encoding="utf-8")) == dataclasses.asdict(state)
        assert container.load_state() == state

    def test_save_in_the_default_location_creates_the_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv(STATE_ENV)

        container.save_state(_state(mode="serve", runtime=None, container=None, image=None))

        path = tmp_path / "home" / ".gco" / "dashboard.json"
        assert _mode(path) == 0o600
        assert container.load_state() == _state(
            mode="serve", runtime=None, container=None, image=None
        )

    def test_save_tightens_and_truncates_an_existing_file(self, state_file: Path) -> None:
        state_file.parent.mkdir(parents=True)
        state_file.write_text("x" * 4096, encoding="utf-8")
        state_file.chmod(0o644)

        container.save_state(_state(token=None))

        assert _mode(state_file) == 0o600
        assert container.load_state() == _state(token=None)

    def test_save_survives_a_filesystem_without_chmod(
        self, monkeypatch: pytest.MonkeyPatch, state_file: Path
    ) -> None:
        def refuse(self: Path, mode: int, *, follow_symlinks: bool = True) -> None:
            raise PermissionError("chmod is not supported here")

        monkeypatch.setattr(Path, "chmod", refuse)

        container.save_state(_state())

        assert container.load_state() == _state()
        assert _mode(state_file) == 0o600  # the creation mode already was

    def test_a_missing_file_loads_as_none(self, state_file: Path) -> None:
        assert not state_file.exists()
        assert container.load_state() is None

    @pytest.mark.parametrize(
        "content",
        [b"{", b"[]", b"null", b'"text"', b"42", b'{"mode": "serve"}', b"\xff\xfe"],
        ids=["truncated", "list", "null", "string", "number", "missing-fields", "not-utf8"],
    )
    def test_unreadable_contents_load_as_none(self, state_file: Path, content: bytes) -> None:
        state_file.parent.mkdir(parents=True)
        state_file.write_bytes(content)

        assert container.load_state() is None

    def test_a_directory_in_place_of_the_file_loads_as_none(self, state_file: Path) -> None:
        state_file.mkdir(parents=True)

        assert container.load_state() is None

    def test_fields_written_by_a_newer_version_are_ignored(self, state_file: Path) -> None:
        state_file.parent.mkdir(parents=True)
        document = {**dataclasses.asdict(_state()), "theme": "dark"}
        state_file.write_text(json.dumps(document), encoding="utf-8")

        assert container.load_state() == _state()

    def test_a_served_dashboards_pid_loads_back(self, state_file: Path) -> None:
        state = _state(mode="serve", runtime=None, container=None, image=None, pid=4321)

        container.save_state(state)

        assert json.loads(state_file.read_text(encoding="utf-8"))["pid"] == 4321
        assert container.load_state() == state

    def test_optional_fields_default_when_absent(self, state_file: Path) -> None:
        state_file.parent.mkdir(parents=True)
        document = {
            "mode": "serve",
            "port": 8501,
            "token": None,
            "project_dir": "/home/tester/gco",
            "started_at": "2026-11-25T18:00:00+00:00",
        }
        state_file.write_text(json.dumps(document), encoding="utf-8")

        assert container.load_state() == DashboardState(**document)

    def test_clear_removes_the_file_and_tolerates_its_absence(self, state_file: Path) -> None:
        container.save_state(_state())

        container.clear_state()
        container.clear_state()

        assert not state_file.exists()

    @pytest.mark.parametrize(
        ("token", "url"),
        [(TOKEN, f"http://localhost:9000/?token={TOKEN}"), (None, "http://localhost:9000/")],
    )
    def test_url_opens_the_dashboard_with_its_token(self, token: str | None, url: str) -> None:
        assert _state(token=token).url == url


def test_now_is_an_aware_utc_timestamp_to_the_second() -> None:
    stamp = container.now()

    moment = datetime.fromisoformat(stamp)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", stamp)
    assert moment.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - moment) < timedelta(minutes=5)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("0", False), ("true", False), ("", False), (None, False)],
)
def test_dev_container_is_recognised_by_its_marker(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    if value is None:
        monkeypatch.delenv("GCO_DEV_CONTAINER", raising=False)
    else:
        monkeypatch.setenv("GCO_DEV_CONTAINER", value)

    assert container.in_dev_container() is expected


def _no_signal(pid: int, signal: int) -> None:
    raise AssertionError(f"os.kill({pid}, {signal}) must not run")


class TestProcessAlive:
    def test_this_process_runs(self) -> None:
        assert container.process_alive(os.getpid()) is True

    def test_a_process_that_exited_does_not(self) -> None:
        child = subprocess.run(
            [sys.executable, "-c", "import os; print(os.getpid())"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert container.process_alive(int(child.stdout)) is False

    def test_another_users_process_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        signals: list[tuple[int, int]] = []

        def refuse(pid: int, signal: int) -> None:
            signals.append((pid, signal))
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "kill", refuse)

        assert container.process_alive(4321) is True
        assert signals == [(4321, 0)]

    def test_a_pid_beyond_what_the_system_allows_names_no_process(self) -> None:
        assert container.process_alive(2**31) is False

    @pytest.mark.parametrize(
        "pid",
        [None, 0, -1, True, "4321", 4321.0],
        ids=["none", "zero", "negative", "bool", "text", "float"],
    )
    def test_a_missing_or_malformed_pid_cannot_be_told(
        self, monkeypatch: pytest.MonkeyPatch, pid: Any
    ) -> None:
        """Signal 0 to PID 0 reaches this process group and to -1 every process: both answer."""
        monkeypatch.setattr(os, "kill", _no_signal)

        assert container.process_alive(pid) is None

    def test_windows_cannot_tell_without_interrupting_the_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(os, "kill", _no_signal)

        assert container.process_alive(os.getpid()) is None
