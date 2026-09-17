"""Unit tests for :mod:`cli.upgrade`, the engine behind ``gco upgrade``.

Every subprocess seam (git, pip, uv, npm, the container runtime) is faked at
``cli.upgrade._run`` or ``subprocess.run``, so the suite needs no network, no
git remote, and no AWS. Real temporary git repositories are used where the
checkout semantics themselves are under test (preserving ``cdk.json`` across a
detached checkout), because those are the behaviours a mock would merely echo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cli import upgrade as engine
from cli.upgrade import (
    CheckoutState,
    InstallProbe,
    ReleaseTag,
    StackCycleResult,
    UpgradeError,
    UpgradePlan,
)

# ---------------------------------------------------------------------------
# Release tags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("v8.0.1", (8, 0, 1)),
        (" v10.2.30 ", (10, 2, 30)),
        ("v0.0.0", (0, 0, 0)),
    ],
)
def test_release_tag_parses_the_release_shape(name: str, expected: tuple[int, int, int]) -> None:
    tag = ReleaseTag.parse(name)
    assert tag is not None
    assert tag.version == expected
    assert tag.name == name.strip()
    assert tag.dotted == ".".join(str(part) for part in expected)


@pytest.mark.parametrize("name", ["8.0.1", "v8.0", "v8.0.1-rc1", "release-8", "", "v8.0.1.2"])
def test_release_tag_rejects_everything_else(name: str) -> None:
    assert ReleaseTag.parse(name) is None


def test_parse_release_tags_sorts_numerically_and_deduplicates() -> None:
    tags = engine.parse_release_tags(["v8.0.10", "v8.0.9", "junk", "v10.0.0", "v8.0.9", "v9.1.0"])
    assert [tag.name for tag in tags] == ["v8.0.9", "v8.0.10", "v9.1.0", "v10.0.0"]


def test_latest_release_is_the_highest_version() -> None:
    tags = engine.parse_release_tags(["v8.0.1", "v8.1.0", "v8.0.9"])
    assert engine.latest_release(tags).name == "v8.1.0"


def test_latest_release_without_tags_explains_the_remote() -> None:
    with pytest.raises(UpgradeError, match="No release tags"):
        engine.latest_release([])


def test_resolve_target_defaults_to_latest_and_honours_ref() -> None:
    tags = engine.parse_release_tags(["v8.0.1", "v8.1.0"])
    assert engine.resolve_target(tags, None).name == "v8.1.0"
    assert engine.resolve_target(tags, "v8.0.1").name == "v8.0.1"


@pytest.mark.parametrize(
    ("ref", "message"),
    [("main", "must name a release tag"), ("v9.9.9", "does not exist on the remote")],
)
def test_resolve_target_rejects_non_release_or_unknown_refs(ref: str, message: str) -> None:
    tags = engine.parse_release_tags(["v8.0.1", "v8.1.0"])
    with pytest.raises(UpgradeError, match=message):
        engine.resolve_target(tags, ref)


def test_resolve_target_lists_no_known_tags_when_there_are_none() -> None:
    with pytest.raises(UpgradeError, match="latest: none"):
        engine.resolve_target([], "v1.0.0")


# ---------------------------------------------------------------------------
# Subprocess seam
# ---------------------------------------------------------------------------


def test_run_translates_a_missing_binary() -> None:
    with (
        patch.object(engine.subprocess, "run", side_effect=FileNotFoundError("no git")),
        pytest.raises(UpgradeError, match="git is not installed or not on PATH"),
    ):
        engine._run(["git", "--version"])


def test_run_translates_a_timeout() -> None:
    with (
        patch.object(
            engine.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["npm", "ci"], timeout=1),
        ),
        pytest.raises(UpgradeError, match="npm ci did not finish"),
    ):
        engine._run(["npm", "ci"], timeout=1)


def test_run_passes_cwd_and_fixed_argv() -> None:
    completed = subprocess.CompletedProcess(["x"], 0, stdout="", stderr="")
    with patch.object(engine.subprocess, "run", return_value=completed) as run:
        engine._run(["git", "status"], cwd=Path("/repo"))
    assert run.call_args.args[0] == ["git", "status"]
    assert run.call_args.kwargs["cwd"] == "/repo"
    assert run.call_args.kwargs["shell"] is False if "shell" in run.call_args.kwargs else True


def test_run_git_raises_with_stderr_or_status() -> None:
    failed = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="fatal: not a repo\n")
    with (
        patch.object(engine, "_run", return_value=failed),
        pytest.raises(UpgradeError, match="git fetch --tags failed: fatal: not a repo"),
    ):
        engine.run_git(Path("/repo"), "fetch", "--tags")

    silent = subprocess.CompletedProcess(["git"], 2, stdout="", stderr="")
    with (
        patch.object(engine, "_run", return_value=silent),
        pytest.raises(UpgradeError, match="exit status 2"),
    ):
        engine.run_git(Path("/repo"), "status")


# ---------------------------------------------------------------------------
# Real git repositories: checkout semantics
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # test fixture, fixed argv
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def release_repo(tmp_path: Path) -> Path:
    """A repository with v8.0.1 (HEAD~1) and v8.1.0 (HEAD) whose cdk.json differs."""
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "tag.gpgsign", "false")
    (root / "cdk.json").write_text('{"context": {"project_name": "gco", "v": 1}}\n')
    (root / "VERSION").write_text("8.0.1\n")
    _git(root, "add", ".")
    _git(root, "commit", "--quiet", "-m", "release 8.0.1")
    _git(root, "tag", "v8.0.1")
    (root / "cdk.json").write_text('{"context": {"project_name": "gco", "v": 2}}\n')
    (root / "VERSION").write_text("8.1.0\n")
    _git(root, "commit", "--quiet", "-am", "release 8.1.0")
    _git(root, "tag", "v8.1.0")
    _git(root, "checkout", "--quiet", "v8.0.1")
    return root


def test_find_checkout_root_walks_up_to_cdk_json_and_git(release_repo: Path) -> None:
    nested = release_repo / "docs" / "deep"
    nested.mkdir(parents=True)
    assert engine.find_checkout_root(nested) == release_repo.resolve()


def test_find_checkout_root_refuses_a_non_checkout(tmp_path: Path) -> None:
    (tmp_path / "cdk.json").write_text("{}")  # cdk.json without .git is an installed copy
    with pytest.raises(UpgradeError, match="must run inside a git checkout"):
        engine.find_checkout_root(tmp_path)


def test_find_checkout_root_defaults_to_the_working_directory(
    release_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(release_repo)
    assert engine.find_checkout_root() == release_repo.resolve()


def test_inspect_checkout_reports_tag_version_and_local_changes(release_repo: Path) -> None:
    (release_repo / "cdk.json").write_text('{"context": {"project_name": "mine"}}\n')
    (release_repo / "VERSION").write_text("8.0.1-local\n")

    state = engine.inspect_checkout(release_repo)

    assert state.root == release_repo
    assert state.ref == "v8.0.1"  # detached HEAD on a tag reports the tag
    assert state.version_file == "8.0.1-local"
    assert state.preserved_modified == ["cdk.json"]
    assert state.dirty_paths == ["VERSION"]
    assert state.to_dict()["root"] == str(release_repo)


def test_inspect_checkout_on_a_branch_reports_the_branch(release_repo: Path) -> None:
    _git(release_repo, "checkout", "--quiet", "main")
    state = engine.inspect_checkout(release_repo)
    assert state.ref == "main"
    assert state.dirty_paths == [] and state.preserved_modified == []


def test_inspect_checkout_detached_off_tag_falls_back_to_the_sha(release_repo: Path) -> None:
    _git(release_repo, "checkout", "--quiet", "main")
    (release_repo / "extra.txt").write_text("x")
    _git(release_repo, "add", "extra.txt")
    _git(release_repo, "commit", "--quiet", "-m", "untagged")
    _git(release_repo, "checkout", "--quiet", "--detach", "HEAD")
    state = engine.inspect_checkout(release_repo)
    assert state.ref == state.head


def test_inspect_checkout_without_version_file(release_repo: Path) -> None:
    (release_repo / "VERSION").unlink()
    _git(release_repo, "commit", "--quiet", "-am", "drop version")
    assert engine.inspect_checkout(release_repo).version_file is None


def test_inspect_checkout_handles_renames_in_status(release_repo: Path) -> None:
    _git(release_repo, "checkout", "--quiet", "main")
    _git(release_repo, "mv", "VERSION", "VERSION.txt")
    state = engine.inspect_checkout(release_repo)
    assert state.dirty_paths == ["VERSION.txt"]


def test_checkout_is_at_compares_commits(release_repo: Path) -> None:
    assert engine.checkout_is_at(release_repo, ReleaseTag((8, 0, 1), "v8.0.1")) is True
    assert engine.checkout_is_at(release_repo, ReleaseTag((8, 1, 0), "v8.1.0")) is False


def test_fetch_release_tags_fetches_then_lists(release_repo: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run_git(root: Path, *args: str) -> str:
        calls.append(args)
        if args[0] == "tag":
            return "v8.0.1\nv8.1.0\nnot-a-release\n"
        return ""

    with patch.object(engine, "run_git", side_effect=fake_run_git):
        tags = engine.fetch_release_tags(release_repo, "upstream")
    assert calls[0] == ("fetch", "--tags", "--quiet", "upstream")
    assert [tag.name for tag in tags] == ["v8.0.1", "v8.1.0"]


def test_checkout_release_preserves_a_modified_cdk_json(release_repo: Path) -> None:
    """The operator's cdk.json survives even though the release changed the file."""
    mine = '{"context": {"project_name": "mine", "regions": ["eu-west-1"]}}\n'
    (release_repo / "cdk.json").write_text(mine)
    log: list[str] = []

    report = engine.checkout_release(release_repo, ReleaseTag((8, 1, 0), "v8.1.0"), log=log.append)

    assert (release_repo / "cdk.json").read_text() == mine
    assert (release_repo / "VERSION").read_text() == "8.1.0\n"
    assert _git(release_repo, "describe", "--tags", "--exact-match", "HEAD").strip() == "v8.1.0"
    assert report == {
        "previous_ref": "v8.0.1",
        "previous_head": report["previous_head"],
        "checked_out": "v8.1.0",
        "preserved": ["cdk.json"],
        "restored": ["cdk.json"],
    }
    assert log and "Checking out v8.1.0 (was v8.0.1" in log[0]


def test_checkout_release_preserves_a_committed_cdk_json(release_repo: Path) -> None:
    """A fork that committed its cdk.json keeps it: the bytes win over the release's."""
    _git(release_repo, "checkout", "--quiet", "main")
    committed = '{"context": {"project_name": "fork"}}\n'
    (release_repo / "cdk.json").write_text(committed)
    _git(release_repo, "commit", "--quiet", "-am", "fork config")

    engine.checkout_release(release_repo, ReleaseTag((8, 0, 1), "v8.0.1"))

    assert (release_repo / "cdk.json").read_text() == committed
    assert (release_repo / "VERSION").read_text() == "8.0.1\n"


def test_checkout_release_leaves_an_identical_cdk_json_alone(release_repo: Path) -> None:
    """Nothing to restore when the release ships the very same bytes."""
    _git(release_repo, "checkout", "--quiet", "v8.1.0")
    _git(release_repo, "checkout", "--quiet", "--detach", "v8.0.1")
    (release_repo / "cdk.json").write_text('{"context": {"project_name": "gco", "v": 2}}\n')

    report = engine.checkout_release(release_repo, ReleaseTag((8, 1, 0), "v8.1.0"))

    assert report["restored"] == []
    assert report["preserved"] == ["cdk.json"]


def test_checkout_release_refuses_other_local_modifications(release_repo: Path) -> None:
    (release_repo / "VERSION").write_text("hacked\n")
    with pytest.raises(UpgradeError, match=r"local modifications.*VERSION"):
        engine.checkout_release(release_repo, ReleaseTag((8, 1, 0), "v8.1.0"))
    assert _git(release_repo, "describe", "--tags", "--exact-match", "HEAD").strip() == "v8.0.1"


def test_checkout_release_without_preserved_files(release_repo: Path) -> None:
    _git(release_repo, "checkout", "--quiet", "main")
    _git(release_repo, "rm", "--quiet", "cdk.json")
    _git(release_repo, "commit", "--quiet", "-m", "no cdk.json")
    _git(release_repo, "tag", "v8.2.0")
    _git(release_repo, "checkout", "--quiet", "--detach", "v8.2.0")

    report = engine.checkout_release(release_repo, ReleaseTag((8, 1, 0), "v8.1.0"))

    assert report["preserved"] == [] and report["restored"] == []
    assert (release_repo / "cdk.json").is_file()


# ---------------------------------------------------------------------------
# The local install
# ---------------------------------------------------------------------------


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["tool"], returncode, stdout=stdout, stderr=stderr)


def test_image_exists_reflects_the_inspect_exit_code() -> None:
    with patch.object(engine, "_run", return_value=_completed(0)):
        assert engine.image_exists("docker", "gco-dev") is True
    with patch.object(engine, "_run", return_value=_completed(1)):
        assert engine.image_exists("docker", "gco-dev") is False


def test_probe_install_with_an_editable_checkout_and_image(tmp_path: Path) -> None:
    (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
    (tmp_path / "node_modules" / ".bin" / "cdk").write_text("")
    with (
        patch.object(engine, "_cli_source_root", return_value=tmp_path.resolve()),
        patch.object(engine, "detect_container_runtime", return_value="docker"),
        patch.object(engine, "image_exists", return_value=True) as exists,
    ):
        probe = engine.probe_install(tmp_path, image="custom-dev")
    assert probe.cli_editable_from_checkout is True
    assert probe.node_toolchain is True
    assert probe.container_runtime == "docker"
    assert probe.dev_image == "custom-dev" and probe.dev_image_present is True
    assert probe.cli_version == engine.__version__
    exists.assert_called_once_with("docker", "custom-dev")
    assert probe.to_dict()["python"] == engine.sys.executable


def test_probe_install_without_runtime_never_inspects_images(tmp_path: Path) -> None:
    with (
        patch.object(engine, "_cli_source_root", return_value=Path("/elsewhere")),
        patch.object(engine, "detect_container_runtime", return_value=None),
        patch.object(engine, "image_exists") as exists,
    ):
        probe = engine.probe_install(tmp_path)
    assert probe.cli_editable_from_checkout is False
    assert probe.node_toolchain is False
    assert probe.dev_image_present is False
    exists.assert_not_called()


def test_cli_source_root_is_the_package_parent() -> None:
    assert engine._cli_source_root() == Path(engine.__file__).resolve().parent.parent


def test_refresh_python_install_uses_pip_first(tmp_path: Path) -> None:
    with patch.object(engine, "_run", return_value=_completed(0)) as run:
        report = engine.refresh_python_install(tmp_path)
    assert report["tool"] == "pip" and report["status"] == "ok"
    argv = run.call_args.args[0]
    assert argv[:4] == [engine.sys.executable, "-m", "pip", "install"]
    assert argv[-2:] == ["-e", str(tmp_path)]


def test_refresh_python_install_falls_back_to_uv(tmp_path: Path) -> None:
    results = iter([_completed(1, stderr="No module named pip"), _completed(0)])
    log: list[str] = []
    with (
        patch.object(engine, "_run", side_effect=lambda *a, **k: next(results)) as run,
        patch.object(engine.shutil, "which", return_value="/usr/local/bin/uv"),
    ):
        report = engine.refresh_python_install(tmp_path, log=log.append)
    assert report["tool"] == "uv" and report["status"] == "ok"
    assert run.call_args.args[0][:3] == ["/usr/local/bin/uv", "pip", "install"]
    assert any("retrying with uv" in line for line in log)


def test_refresh_python_install_reports_both_failures(tmp_path: Path) -> None:
    results = iter([_completed(1, stderr="pip broke"), _completed(1, stderr="uv broke")])
    with (
        patch.object(engine, "_run", side_effect=lambda *a, **k: next(results)),
        patch.object(engine.shutil, "which", return_value="/usr/local/bin/uv"),
        pytest.raises(UpgradeError, match="uv broke"),
    ):
        engine.refresh_python_install(tmp_path)


def test_refresh_python_install_without_uv_reports_pip_failure(tmp_path: Path) -> None:
    with (
        patch.object(engine, "_run", return_value=_completed(1, stdout="pip said no")),
        patch.object(engine.shutil, "which", return_value=None),
        pytest.raises(UpgradeError, match="pip said no"),
    ):
        engine.refresh_python_install(tmp_path)


def test_refresh_node_toolchain_skips_without_npm(tmp_path: Path) -> None:
    with patch.object(engine.shutil, "which", return_value=None):
        assert engine.refresh_node_toolchain(tmp_path) == {
            "status": "skipped",
            "reason": "npm is not on PATH",
        }


def test_refresh_node_toolchain_runs_npm_ci(tmp_path: Path) -> None:
    with (
        patch.object(engine.shutil, "which", return_value="/usr/bin/npm"),
        patch.object(engine, "_run", return_value=_completed(0)) as run,
    ):
        report = engine.refresh_node_toolchain(tmp_path)
    assert report["status"] == "ok"
    assert run.call_args.args[0] == [
        "/usr/bin/npm",
        "ci",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    ]
    assert run.call_args.kwargs["cwd"] == tmp_path


def test_refresh_node_toolchain_failure_is_a_warning(tmp_path: Path) -> None:
    with (
        patch.object(engine.shutil, "which", return_value="/usr/bin/npm"),
        patch.object(engine, "_run", return_value=_completed(1, stderr="lockfile mismatch")),
    ):
        report = engine.refresh_node_toolchain(tmp_path)
    assert report["status"] == "warning"
    assert "lockfile mismatch" in report["message"]


def test_rebuild_dev_image_skips_without_dockerfile(tmp_path: Path) -> None:
    report = engine.rebuild_dev_image(tmp_path, runtime="docker")
    assert report["status"] == "skipped"


def test_rebuild_dev_image_builds_with_the_runtime(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile.dev").write_text("FROM scratch\n")
    with patch.object(engine, "_run", return_value=_completed(0)) as run:
        report = engine.rebuild_dev_image(tmp_path, runtime="finch", image="gco-dev")
    assert report == {"status": "ok", "image": "gco-dev", "runtime": "finch"}
    assert run.call_args.args[0] == [
        "finch",
        "build",
        "-f",
        str(tmp_path / "Dockerfile.dev"),
        "-t",
        "gco-dev",
        str(tmp_path),
    ]


def test_rebuild_dev_image_failure_is_a_warning(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile.dev").write_text("FROM scratch\n")
    with patch.object(engine, "_run", return_value=_completed(1, stderr="pull failed")):
        report = engine.rebuild_dev_image(tmp_path, runtime="docker")
    assert report["status"] == "warning"
    assert "setup-dev-alias.sh" in report["message"] and "pull failed" in report["message"]


def test_require_container_runtime() -> None:
    with patch.object(engine, "detect_container_runtime", return_value="podman"):
        assert engine.require_container_runtime() == "podman"
    with (
        patch.object(engine, "detect_container_runtime", return_value=None),
        patch.object(engine, "container_runtime_error_message", return_value="no runtime here"),
        pytest.raises(UpgradeError, match="no runtime here"),
    ):
        engine.require_container_runtime()


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def _checkout_state(root: Path = Path("/repo")) -> CheckoutState:
    return CheckoutState(root=root, head="abc1234", ref="main", version_file="8.0.1")


def _install_probe(**overrides: object) -> InstallProbe:
    values: dict[str, object] = {
        "cli_version": "8.0.1",
        "cli_path": "/repo",
        "cli_editable_from_checkout": True,
        "python": "/usr/bin/python3",
        "node_toolchain": False,
        "container_runtime": "docker",
        "dev_image": "gco-dev",
        "dev_image_present": True,
    }
    values.update(overrides)
    return InstallProbe(**values)  # type: ignore[arg-type]


def test_split_stacks_orders_the_workload_tier_for_teardown() -> None:
    stacks = [
        "gco-global",
        "gco-api-gateway",
        "gco-us-east-1",
        "gco-regional-api-us-east-1",
        "gco-monitoring",
    ]
    control_plane, workload = engine.split_stacks(stacks, "gco")
    assert control_plane == ["gco-global", "gco-api-gateway", "gco-monitoring"]
    assert workload == ["gco-regional-api-us-east-1", "gco-us-east-1"]


def test_build_plan_fetches_and_probes(tmp_path: Path) -> None:
    tags = engine.parse_release_tags(["v8.0.1", "v8.1.0"])
    with (
        patch.object(engine, "inspect_checkout", return_value=_checkout_state(tmp_path)),
        patch.object(engine, "fetch_release_tags", return_value=tags) as fetch,
        patch.object(engine, "probe_install", return_value=_install_probe()) as probe,
        patch.object(engine, "checkout_is_at", return_value=False),
    ):
        plan = engine.build_plan(
            tmp_path,
            project_name="gco",
            stacks=["gco-global", "gco-us-east-1"],
            remote="upstream",
            image="my-dev",
        )
    fetch.assert_called_once_with(tmp_path, "upstream")
    probe.assert_called_once_with(tmp_path, image="my-dev")
    assert plan.target.name == "v8.1.0" and plan.latest.name == "v8.1.0"
    assert plan.already_at_target is False
    assert plan.control_plane_stacks == ["gco-global"]
    assert plan.workload_stacks == ["gco-us-east-1"]
    document = plan.to_dict()
    assert document["target"] == "v8.1.0"
    assert document["remote"] == "upstream"
    assert document["checkout_version"] == "8.0.1"
    assert document["install"]["dev_image"] == "gco-dev"


def test_build_plan_skip_fetch_reads_local_tags_and_pins_ref(tmp_path: Path) -> None:
    with (
        patch.object(engine, "inspect_checkout", return_value=_checkout_state(tmp_path)),
        patch.object(engine, "run_git", return_value="v8.0.1\nv8.1.0\n") as git,
        patch.object(engine, "fetch_release_tags") as fetch,
        patch.object(engine, "probe_install", return_value=_install_probe()),
        patch.object(engine, "checkout_is_at", return_value=True),
    ):
        plan = engine.build_plan(
            tmp_path, project_name="gco", stacks=[], ref="v8.0.1", skip_fetch=True
        )
    fetch.assert_not_called()
    git.assert_called_once_with(tmp_path, "tag", "--list", "v*")
    assert plan.target.name == "v8.0.1" and plan.latest.name == "v8.1.0"
    assert plan.already_at_target is True


# ---------------------------------------------------------------------------
# The stack cycle
# ---------------------------------------------------------------------------


def _manager(
    destroy_results: list[tuple[bool, list[str], list[str]]],
    deploy_result: tuple[bool, list[str], list[str]] = (True, ["gco-global", "gco-us-east-1"], []),
) -> MagicMock:
    manager = MagicMock()
    manager.destroy_orchestrated.side_effect = list(destroy_results)
    manager.deploy_orchestrated.return_value = deploy_result
    return manager


def test_run_stack_cycle_happy_path_passes_the_callbacks_through() -> None:
    manager = _manager([(True, ["gco-regional-api-us-east-1", "gco-us-east-1"], [])])
    on_start, on_complete = MagicMock(), MagicMock()
    log: list[str] = []
    waits: list[float] = []

    result = engine.run_stack_cycle(
        manager,
        parallel=True,
        max_workers=8,
        on_stack_start=on_start,
        on_stack_complete=on_complete,
        log=log.append,
        sleep=waits.append,
    )

    assert result.ok is True and result.phase_failed is None
    assert result.destroyed == ["gco-regional-api-us-east-1", "gco-us-east-1"]
    assert result.deployed == ["gco-global", "gco-us-east-1"]
    assert result.teardown_attempts == 1 and waits == []
    destroy_kwargs = manager.destroy_orchestrated.call_args.kwargs
    assert destroy_kwargs["keep_control_plane"] is True and destroy_kwargs["force"] is True
    assert destroy_kwargs["parallel"] is True and destroy_kwargs["max_workers"] == 8
    assert destroy_kwargs["on_stack_start"] is on_start
    deploy_kwargs = manager.deploy_orchestrated.call_args.kwargs
    assert deploy_kwargs["require_approval"] is False
    assert deploy_kwargs["on_stack_complete"] is on_complete
    manager.cleanup_orphaned_network_interfaces.assert_not_called()
    assert log[0].startswith("Phase 1/2") and log[-1].startswith("Phase 2/2")
    assert result.to_dict()["ok"] is True


def test_run_stack_cycle_retries_the_teardown_and_merges_destroyed_stacks() -> None:
    manager = _manager(
        [
            (False, ["gco-regional-api-us-east-1"], ["gco-us-east-1"]),
            # The retry re-reports the bridge (deleting an absent stack succeeds);
            # it must not be listed twice.
            (True, ["gco-regional-api-us-east-1", "gco-us-east-1"], []),
        ]
    )
    waits: list[float] = []

    result = engine.run_stack_cycle(manager, sleep=waits.append, retry_wait_seconds=5)

    assert result.ok is True
    assert result.teardown_attempts == 2
    assert result.destroyed == ["gco-regional-api-us-east-1", "gco-us-east-1"]
    assert waits == [5]
    manager.cleanup_orphaned_network_interfaces.assert_called_once_with()
    manager.deploy_orchestrated.assert_called_once()


def test_run_stack_cycle_gives_up_after_max_attempts_without_deploying() -> None:
    manager = _manager([(False, [], ["gco-us-east-1"])] * 3)

    result = engine.run_stack_cycle(manager, sleep=lambda _s: None, max_attempts=3)

    assert result.ok is False
    assert result.phase_failed == "teardown"
    assert result.failed == ["gco-us-east-1"]
    assert result.teardown_attempts == 3
    manager.deploy_orchestrated.assert_not_called()


def test_run_stack_cycle_reports_a_failed_deploy() -> None:
    manager = _manager(
        [(True, ["gco-us-east-1"], [])], deploy_result=(False, ["gco-global"], ["gco-api-gateway"])
    )

    result = engine.run_stack_cycle(manager, sleep=lambda _s: None)

    assert result.ok is False
    assert result.phase_failed == "deploy"
    assert result.deployed == ["gco-global"]
    assert result.failed == ["gco-api-gateway"]


def test_run_stack_cycle_uses_time_sleep_by_default() -> None:
    manager = _manager([(False, [], ["x"]), (True, ["x"], [])])
    with patch("time.sleep") as sleep:
        engine.run_stack_cycle(manager, retry_wait_seconds=1.5)
    sleep.assert_called_once_with(1.5)


def test_upgrade_plan_and_cycle_result_are_plain_dataclasses() -> None:
    plan = UpgradePlan(
        current_version="8.0.1",
        target=ReleaseTag((8, 1, 0), "v8.1.0"),
        latest=ReleaseTag((8, 1, 0), "v8.1.0"),
        checkout=_checkout_state(),
        install=_install_probe(),
        already_at_target=False,
        control_plane_stacks=["gco-global"],
        workload_stacks=["gco-us-east-1"],
        remote="origin",
    )
    assert plan.to_dict()["workload_stacks"] == ["gco-us-east-1"]
    assert StackCycleResult().ok is True
    assert SimpleNamespace(**StackCycleResult(phase_failed="deploy").to_dict()).ok is False
