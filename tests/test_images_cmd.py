"""
Tests for the ``gco images`` Click subgroup.

Each command is exercised through ``CliRunner`` against a mocked
``ImageManager`` so tests stay isolated from boto3 and any container
runtime. Coverage spans every command surface — read-only,
administrative, build/push, destructive, lifecycle, replication —
plus the validation and error-handling paths inside the wrapping
Click callbacks.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def mock_config() -> Any:
    """Stub ``cli.main.get_config`` so we never touch a real cdk.json."""
    cfg = MagicMock()
    cfg.output_format = "table"
    cfg.global_region = "us-east-2"
    cfg.project_name = "gco"
    cfg.regions = ["us-east-2", "us-west-2"]
    with patch("cli.main.get_config", return_value=cfg):
        yield cfg


def _patch_image_manager(mock_mgr: Any) -> Any:
    """Patch the lazy ``get_image_manager`` factory inside the command callbacks."""
    return patch("cli.images.get_image_manager", return_value=mock_mgr)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["init", "build", "push"])
def test_retain_help_says_the_tag_is_only_a_marker(runner: CliRunner, command: str) -> None:
    """IMAGES-RETAIN-TEXT: no GCO stack deletes project repositories, so the
    flag must not promise that it is what keeps one through a destroy."""
    result = runner.invoke(cli, ["images", command, "--help"])
    assert result.exit_code == 0, result.output
    help_text = " ".join(result.output.split())
    assert "Tag the repository gco:retain=true. A marker only" in help_text
    assert "every one survives a stack destroy" in help_text


class TestImagesInit:
    def test_init_success_creates_repo(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.init.return_value = {"name": "gco/svc", "created": True, "retain": False}
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "init", "svc"])
        assert result.exit_code == 0, result.output
        assert "Created repository" in result.output
        mock_mgr.init.assert_called_once_with("svc", retain=False)

    def test_init_existing_repo_is_idempotent(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.init.return_value = {"name": "gco/svc", "created": False, "retain": False}
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "init", "svc"])
        assert result.exit_code == 0, result.output
        assert "already existed" in result.output

    def test_init_with_retain_flag(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.init.return_value = {"name": "gco/svc", "created": True, "retain": True}
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "init", "svc", "--retain"])
        assert result.exit_code == 0, result.output
        mock_mgr.init.assert_called_once_with("svc", retain=True)

    def test_init_failure_returns_non_zero(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.init.side_effect = RuntimeError("api blocked")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "init", "svc"])
        assert result.exit_code != 0
        assert "Failed to init" in result.output


# ---------------------------------------------------------------------------
# Read-only commands
# ---------------------------------------------------------------------------


class TestImagesList:
    def test_list_empty(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.list_repos.return_value = []
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "list"])
        assert result.exit_code == 0
        assert "No repositories found" in result.output

    def test_list_renders_repos(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.list_repos.return_value = [
            {"name": "gco/svc", "image_count": 3, "tag_mutability": "MUTABLE"},
        ]
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "list"])
        assert result.exit_code == 0
        assert "gco/svc" in result.output

    def test_list_failure_non_zero(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.list_repos.side_effect = RuntimeError("ddb")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "list"])
        assert result.exit_code != 0


class TestImagesTags:
    def test_tags_empty(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.list_tags.return_value = []
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "tags", "svc"])
        assert result.exit_code == 0
        assert "No tags found" in result.output

    def test_tags_renders_rows(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.list_tags.return_value = [
            {"tag": "v1", "digest": "sha256:abc", "pushed_at": None, "size_bytes": 1234},
        ]
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "tags", "svc"])
        assert result.exit_code == 0
        assert "v1" in result.output

    def test_tags_failure_non_zero(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.list_tags.side_effect = RuntimeError("forbidden")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "tags", "svc"])
        assert result.exit_code != 0


class TestImagesDescribe:
    @pytest.mark.parametrize("output_format", ["table", "json"])
    def test_describe_missing_tag_is_an_error(
        self, runner: CliRunner, mock_config: Any, output_format: str
    ) -> None:
        """N43: a missing tag exited 0 (and printed {"status": "ok"} in JSON
        mode), so a script asking "does this image exist?" got a yes."""
        mock_config.output_format = output_format
        mock_mgr = MagicMock()
        mock_mgr.describe.return_value = {}
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "describe", "svc", "v1"])
        assert result.exit_code == 1
        assert "Tag 'v1' not found in svc" in result.stderr
        assert result.stdout == ""

    def test_describe_renders_detail(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.describe.return_value = {
            "name": "gco/svc",
            "tag": "v1",
            "digest": "sha256:abc",
        }
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "describe", "svc", "v1"])
        assert result.exit_code == 0
        assert "sha256:abc" in result.output

    def test_describe_failure_non_zero(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.describe.side_effect = RuntimeError("denied")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "describe", "svc", "v1"])
        assert result.exit_code != 0


class TestImagesUri:
    def test_uri_default_tag(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.get_uri.return_value = (
            "123456789012.dkr.ecr.us-east-2.amazonaws.com/gco/svc:latest"
        )
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "uri", "svc"])
        assert result.exit_code == 0
        assert ":latest" in result.output
        mock_mgr.get_uri.assert_called_once_with("svc", tag="latest")

    def test_uri_with_explicit_tag(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.get_uri.return_value = "123456789012.dkr.ecr.us-east-2.amazonaws.com/gco/svc:v1"
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "uri", "svc", "--tag", "v1"])
        assert result.exit_code == 0
        assert ":v1" in result.output

    def test_uri_failure_non_zero(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.get_uri.side_effect = ValueError("bad name")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "uri", "svc"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# build / push
# ---------------------------------------------------------------------------


class TestImagesBuild:
    def test_build_passes_through_options(self, runner: CliRunner, tmp_path: Any) -> None:
        ctx = tmp_path / "ctx"
        ctx.mkdir()
        (ctx / "Dockerfile").write_text("FROM scratch\n")
        mock_mgr = MagicMock()
        mock_mgr.build.return_value = {
            "image_uri": "123456789012.dkr.ecr.us-east-2.amazonaws.com/gco/svc:v1",
            "digest": "sha256:" + "0" * 64,
            "size_bytes": 4096,
            "runtime": "docker",
            "repository": "gco/svc",
            "tag": "v1",
            "region": "us-east-2",
            "retain": False,
        }
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(
                cli,
                [
                    "images",
                    "build",
                    str(ctx),
                    "--name",
                    "svc",
                    "--tag",
                    "v1",
                    "--build-arg",
                    "FOO=bar",
                    "--build-arg",
                    "BAZ=qux",
                    "--platform",
                    "linux/arm64",
                    "--retain",
                ],
            )
        assert result.exit_code == 0, result.output
        kwargs = mock_mgr.build.call_args.kwargs
        assert kwargs["context"] == str(ctx)
        assert kwargs["name"] == "svc"
        assert kwargs["tag"] == "v1"
        assert kwargs["platform"] == "linux/arm64"
        assert kwargs["retain"] is True
        assert kwargs["build_args"] == {"FOO": "bar", "BAZ": "qux"}

    def test_build_args_without_equals_errors(self, runner: CliRunner, tmp_path: Any) -> None:
        ctx = tmp_path / "ctx"
        ctx.mkdir()
        (ctx / "Dockerfile").write_text("FROM scratch\n")
        mock_mgr = MagicMock()
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(
                cli,
                [
                    "images",
                    "build",
                    str(ctx),
                    "--name",
                    "svc",
                    "--build-arg",
                    "BAD",
                ],
            )
        assert result.exit_code != 0
        assert "missing '='" in result.output
        mock_mgr.build.assert_not_called()

    def test_build_failure_propagates(self, runner: CliRunner, tmp_path: Any) -> None:
        ctx = tmp_path / "ctx"
        ctx.mkdir()
        (ctx / "Dockerfile").write_text("FROM scratch\n")
        mock_mgr = MagicMock()
        mock_mgr.build.side_effect = RuntimeError("daemon down")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(
                cli,
                ["images", "build", str(ctx), "--name", "svc"],
            )
        assert result.exit_code != 0
        assert "Failed to build" in result.output


class TestImagesPush:
    def test_push_success(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.push.return_value = {
            "image_uri": "123456789012.dkr.ecr.us-east-2.amazonaws.com/gco/svc:v1",
            "digest": "sha256:" + "0" * 64,
        }
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(
                cli,
                [
                    "images",
                    "push",
                    "svc",
                    "--tag",
                    "v1",
                    "--local-image",
                    "local/svc:scratch",
                ],
            )
        assert result.exit_code == 0, result.output
        kwargs = mock_mgr.push.call_args.kwargs
        assert kwargs["name"] == "svc"
        assert kwargs["tag"] == "v1"
        assert kwargs["local_image"] == "local/svc:scratch"

    def test_push_failure(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.push.side_effect = RuntimeError("auth")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(
                cli,
                [
                    "images",
                    "push",
                    "svc",
                    "--tag",
                    "v1",
                    "--local-image",
                    "x:1",
                ],
            )
        assert result.exit_code != 0
        assert "Failed to push" in result.output


# ---------------------------------------------------------------------------
# Destructive
# ---------------------------------------------------------------------------


class TestImagesDeleteTag:
    def test_delete_tag_succeeds(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.delete_tag.return_value = {
            "name": "gco/svc",
            "tag": "v1",
            "deleted": [{"digest": "sha256:abc", "tag": "v1"}],
            "failures": [],
        }
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "delete-tag", "svc", "v1", "-y"])
        assert result.exit_code == 0, result.output
        assert "Deleted 1 image" in result.output

    def test_delete_tag_requires_yes(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "delete-tag", "svc", "v1"])
        assert result.exit_code != 0
        mock_mgr.delete_tag.assert_not_called()

    def test_delete_tag_failure_propagates(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.delete_tag.side_effect = RuntimeError("nope")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "delete-tag", "svc", "v1", "-y"])
        assert result.exit_code != 0
        assert "Failed to delete tag" in result.output


class TestImagesDeleteRepo:
    def test_delete_repo_with_force(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.delete_repo.return_value = {
            "name": "gco/svc",
            "deleted": True,
            "registry_id": "123456789012",
        }
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "delete-repo", "svc", "--force", "-y"])
        assert result.exit_code == 0, result.output
        kwargs = mock_mgr.delete_repo.call_args.kwargs
        assert kwargs["force"] is True

    def test_delete_repo_failure(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.delete_repo.side_effect = RuntimeError("denied")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "delete-repo", "svc", "-y"])
        assert result.exit_code != 0


class TestImagesCleanup:
    def test_cleanup_requires_name_or_all(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "cleanup", "-y"])
        assert result.exit_code != 0
        assert "--name" in result.output

    def test_cleanup_one_repo(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.cleanup.return_value = {
            "repos_touched": 1,
            "tags_deleted": 7,
            "bytes_freed": 1024,
        }
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "cleanup", "--name", "svc", "-y"])
        assert result.exit_code == 0, result.output
        assert "tags_deleted=7" in result.output
        kwargs = mock_mgr.cleanup.call_args.kwargs
        assert kwargs["name"] == "svc"
        assert kwargs["all"] is False

    def test_cleanup_all_flag(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.cleanup.return_value = {
            "repos_touched": 4,
            "tags_deleted": 17,
            "bytes_freed": 8192,
        }
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "cleanup", "--all", "-y"])
        assert result.exit_code == 0, result.output
        kwargs = mock_mgr.cleanup.call_args.kwargs
        assert kwargs["all"] is True

    def test_cleanup_failure(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.cleanup.side_effect = RuntimeError("xxx")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "cleanup", "--all", "-y"])
        assert result.exit_code != 0


class TestImagesPrune:
    def test_prune_dry_run_default(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.prune.return_value = {
            "dry_run": True,
            "repos_touched": 2,
            "tags_deleted": 5,
            "bytes_freed": 1024,
        }
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "prune", "-y"])
        assert result.exit_code == 0, result.output
        assert "Would delete" in result.output
        kwargs = mock_mgr.prune.call_args.kwargs
        assert kwargs["dry_run"] is True

    def test_prune_no_dry_run(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.prune.return_value = {
            "dry_run": False,
            "repos_touched": 1,
            "tags_deleted": 3,
            "bytes_freed": 512,
        }
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "prune", "--no-dry-run", "-y"])
        assert result.exit_code == 0
        assert "Deleted: " in result.output
        kwargs = mock_mgr.prune.call_args.kwargs
        assert kwargs["dry_run"] is False

    def test_prune_failure(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.prune.side_effect = RuntimeError("ddb")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "prune", "-y"])
        assert result.exit_code != 0


class TestImagesOrphans:
    def test_orphans_empty(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.orphans.return_value = []
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "orphans"])
        assert result.exit_code == 0
        assert "No orphans" in result.output

    def test_orphans_with_results(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.orphans.return_value = [
            {
                "repository": "gco/svc",
                "tag": "stale",
                "digest": "sha256:abc",
                "pushed_at": "2026-01-01T00:00:00+00:00",
                "uri": "123.dkr.ecr.us-east-2.amazonaws.com/gco/svc:stale",
            }
        ]
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "orphans", "--threshold-days", "60"])
        assert result.exit_code == 0, result.output
        kwargs = mock_mgr.orphans.call_args.kwargs
        assert kwargs["threshold_days"] == 60
        assert "stale" in result.output

    def test_orphans_failure(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.orphans.side_effect = RuntimeError("ddb")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "orphans"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Lifecycle subgroup
# ---------------------------------------------------------------------------


class TestLifecycleGet:
    def test_get_missing_policy(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.lifecycle_get.return_value = {}
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "lifecycle", "get", "svc"])
        assert result.exit_code == 0
        assert "No lifecycle policy" in result.output

    def test_get_present_policy(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.lifecycle_get.return_value = {
            "name": "gco/svc",
            "policy": {"rules": []},
        }
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "lifecycle", "get", "svc"])
        assert result.exit_code == 0
        assert "gco/svc" in result.output

    def test_get_failure(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.lifecycle_get.side_effect = RuntimeError("fail")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "lifecycle", "get", "svc"])
        assert result.exit_code != 0


class TestLifecycleSet:
    def test_set_round_trips_file(self, runner: CliRunner, tmp_path: Any) -> None:
        policy_path = tmp_path / "policy.json"
        policy = {"rules": [{"rulePriority": 1}]}
        policy_path.write_text(json.dumps(policy))
        mock_mgr = MagicMock()
        mock_mgr.lifecycle_set.return_value = {
            "name": "gco/svc",
            "registry_id": "123456789012",
            "policy": policy,
        }
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(
                cli,
                [
                    "images",
                    "lifecycle",
                    "set",
                    "svc",
                    "--file",
                    str(policy_path),
                ],
            )
        assert result.exit_code == 0, result.output
        args = mock_mgr.lifecycle_set.call_args.args
        assert args[0] == "svc"
        assert args[1] == policy

    def test_set_missing_file(self, runner: CliRunner, tmp_path: Any) -> None:
        mock_mgr = MagicMock()
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(
                cli,
                [
                    "images",
                    "lifecycle",
                    "set",
                    "svc",
                    "--file",
                    str(tmp_path / "missing.json"),
                ],
            )
        assert result.exit_code != 0
        mock_mgr.lifecycle_set.assert_not_called()


# ---------------------------------------------------------------------------
# Replication subgroup
# ---------------------------------------------------------------------------


class TestReplicationGet:
    def test_no_policy(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.replication_get.return_value = {}
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "replication", "get"])
        assert result.exit_code == 0
        assert "No replication" in result.output

    def test_with_policy(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.replication_get.return_value = {
            "registryId": "123456789012",
            "policy": {"rules": []},
        }
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "replication", "get"])
        assert result.exit_code == 0
        assert "123456789012" in result.output

    def test_failure(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.replication_get.side_effect = RuntimeError("fail")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "replication", "get"])
        assert result.exit_code != 0


class TestReplicationStatus:
    def test_empty(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.replication_status.return_value = []
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "replication", "status"])
        assert result.exit_code == 0
        assert "No replication status" in result.output

    def test_renders_rows(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.replication_status.return_value = [
            {
                "repository": "gco/svc",
                "digest": "sha256:abc",
                "region": "us-west-2",
                "status": "COMPLETE",
                "registry_id": "123456789012",
            }
        ]
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "replication", "status"])
        assert result.exit_code == 0
        assert "us-west-2" in result.output

    def test_failure(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.replication_status.side_effect = RuntimeError("fail")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "replication", "status"])
        assert result.exit_code != 0


class TestReplicationSync:
    def test_writes_destinations(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.replication_sync.return_value = {
            "destinations": ["us-west-2", "eu-west-1"],
            "configuration": {"rules": []},
        }
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "replication", "sync"])
        assert result.exit_code == 0, result.output
        assert "us-west-2" in result.output

    def test_no_destinations(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.replication_sync.return_value = {
            "destinations": [],
            "configuration": {"rules": []},
        }
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "replication", "sync"])
        assert result.exit_code == 0
        assert "none" in result.output

    def test_failure(self, runner: CliRunner) -> None:
        mock_mgr = MagicMock()
        mock_mgr.replication_sync.side_effect = RuntimeError("fail")
        with _patch_image_manager(mock_mgr):
            result = runner.invoke(cli, ["images", "replication", "sync"])
        assert result.exit_code != 0
