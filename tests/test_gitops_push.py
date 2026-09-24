"""``gco stacks capabilities gitops push``: mirroring a directory into CodeCommit.

* ``cli/gitops_push.py`` — the local tree (Git-aware listing with the plain
  walk fallback, symlink/oversize/empty refusals, executable bit, Git blob
  ids), the remote tree from ``GetDifferences``, the diff plan, and the chained
  ``CreateCommit`` batches (empty repository, parent chaining, the 100-file and
  byte budgets, no commit when nothing changed, dry run) against an in-memory
  CodeCommit fake that behaves like the service.
* Resolution against the ``eks_capabilities`` block: only ``source: codecommit``
  regions are pushable, with the CLI's own repository naming.
* The Click command through ``CliRunner`` in table and JSON modes.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner

from cli import gitops_push
from cli.main import cli
from gco import eks_capabilities_config as caps

_REGION = "us-east-1"
_PROJECT = "gco"
_REPOSITORY = "gco-us-east-1-gitops"
_IDC_ARN = "arn:aws:sso:::instance/ssoins-1234567890abcdef"
_ADMIN_MAPPING = {"role": "ADMIN", "identities": [{"id": "u-admin", "type": "SSO_USER"}]}


def _config(*, source: str = "codecommit", gitops_enabled: bool = True) -> dict[str, Any]:
    gitops: dict[str, Any] = {"enabled": gitops_enabled}
    if source == "git":
        gitops.update({"source": "git", "repo_url": "https://github.com/example/tenants.git"})
    return caps.normalize_eks_capabilities_config(
        {
            "argocd": {
                "enabled": True,
                "idc_instance_arn": _IDC_ARN,
                "rbac_role_mappings": [_ADMIN_MAPPING],
                "gitops": gitops,
            }
        }
    )


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakeCodeCommit:
    """Enough of the CodeCommit API for a push: branches, trees by commit, CreateCommit."""

    def __init__(self, *, exists: bool = True, tree: dict[str, tuple[bytes, str]] | None = None):
        self.exists = exists
        self.commits: dict[str, dict[str, tuple[str, str]]] = {}
        self.branches: dict[str, str] = {}
        self.create_calls: list[dict[str, Any]] = []
        self._counter = 0
        if tree is not None:
            commit_id = self._new_commit_id()
            self.commits[commit_id] = {
                path: (gitops_push.git_blob_id(content), mode)
                for path, (content, mode) in tree.items()
            }
            self.branches["main"] = commit_id

    def _new_commit_id(self) -> str:
        self._counter += 1
        # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1 -- fake CodeCommit mints 40-hex Git commit ids; SHA-1 is the Git id shape, not a security control
        return hashlib.sha1(f"commit-{self._counter}".encode(), usedforsecurity=False).hexdigest()

    # --- API surface ---------------------------------------------------------
    def get_branch(self, *, repositoryName: str, branchName: str) -> dict[str, Any]:
        if not self.exists:
            raise _client_error("RepositoryDoesNotExistException", "GetBranch")
        if branchName not in self.branches:
            raise _client_error("BranchDoesNotExistException", "GetBranch")
        return {"branch": {"branchName": branchName, "commitId": self.branches[branchName]}}

    def get_paginator(self, operation: str) -> Any:
        assert operation == "get_differences"
        fake = self

        class _Paginator:
            def paginate(self, *, repositoryName: str, afterCommitSpecifier: str) -> list[dict]:
                tree = fake.commits[afterCommitSpecifier]
                items = [
                    {
                        "changeType": "A",
                        "afterBlob": {
                            "blobId": blob_id,
                            "path": path,
                            "mode": "100755" if mode == "EXECUTABLE" else "100644",
                        },
                    }
                    for path, (blob_id, mode) in sorted(tree.items())
                ]
                # Two pages, to prove pagination is honored.
                half = max(1, len(items) // 2)
                return [{"differences": items[:half]}, {"differences": items[half:]}]

        return _Paginator()

    def create_commit(self, **request: Any) -> dict[str, Any]:
        self.create_calls.append(request)
        branch = request["branchName"]
        parent = request.get("parentCommitId")
        if branch in self.branches:
            assert parent == self.branches[branch], "parent must be the branch head"
        else:
            assert parent is None, "an empty repository takes no parent"
        tree = dict(self.commits.get(parent, {})) if parent else {}
        for item in request.get("putFiles", []):
            assert len(item["fileContent"]) <= gitops_push.MAX_FILE_BYTES
            tree[item["filePath"]] = (
                gitops_push.git_blob_id(item["fileContent"]),
                item["fileMode"],
            )
        for item in request.get("deleteFiles", []):
            tree.pop(item["filePath"], None)
        assert len(request.get("putFiles", [])) + len(request.get("deleteFiles", [])) <= 100
        commit_id = self._new_commit_id()
        self.commits[commit_id] = tree
        self.branches[branch] = commit_id
        return {"commitId": commit_id}

    def branch_tree(self, branch: str = "main") -> dict[str, tuple[str, str]]:
        return self.commits[self.branches[branch]]


def _write_tree(
    root: Path, files: dict[str, bytes | str], *, executable: set[str] = frozenset()
) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode() if isinstance(content, str) else content
        path.write_bytes(data)
        if relative in executable:
            path.chmod(0o755)


# ─── the local tree ──────────────────────────────────────────────────────────


class TestLocalTree:
    def test_git_blob_id_matches_git(self) -> None:
        content = b"hello\n"
        expected = (
            subprocess.run(
                ["git", "hash-object", "--stdin"], input=content, capture_output=True, check=True
            )
            .stdout.decode()
            .strip()
        )
        assert gitops_push.git_blob_id(content) == expected

    def test_plain_directory_walk_skips_git_dir_and_orders_paths(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {"b/two.yaml": "b", "a.yaml": "a", ".git/HEAD": "ref", "b/.hidden": "h"},
            executable={"a.yaml"},
        )
        with patch.object(gitops_push, "_git_listed_paths", return_value=None):
            files = gitops_push.collect_local_tree(tmp_path)
        assert [item.path for item in files] == ["a.yaml", "b/.hidden", "b/two.yaml"]
        by_path = {item.path: item for item in files}
        assert by_path["a.yaml"].mode == gitops_push.FILE_MODE_EXECUTABLE
        assert by_path["b/two.yaml"].mode == gitops_push.FILE_MODE_NORMAL
        assert by_path["a.yaml"].blob_id == gitops_push.git_blob_id(b"a")

    def test_git_work_tree_listing_honors_gitignore_and_untracked_files(
        self, tmp_path: Path
    ) -> None:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        _write_tree(
            tmp_path,
            {
                "tracked.yaml": "t",
                "untracked.yaml": "u",
                "ignored.log": "i",
                ".gitignore": "*.log\n",
                "sub/deep.yaml": "d",
            },
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "tracked.yaml", ".gitignore"], check=True
        )
        files = gitops_push.collect_local_tree(tmp_path)
        assert [item.path for item in files] == [
            ".gitignore",
            "sub/deep.yaml",
            "tracked.yaml",
            "untracked.yaml",
        ]
        # A subdirectory pushes only its own files, relative to itself.
        sub_files = gitops_push.collect_local_tree(tmp_path / "sub")
        assert [item.path for item in sub_files] == ["deep.yaml"]

    def test_tracked_but_deleted_file_is_not_part_of_the_tree(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        _write_tree(tmp_path, {"keep.yaml": "k", "gone.yaml": "g"})
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
        (tmp_path / "gone.yaml").unlink()
        assert [item.path for item in gitops_push.collect_local_tree(tmp_path)] == ["keep.yaml"]

    def test_symlinks_are_refused_by_name(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"real.yaml": "r"})
        os.symlink(tmp_path / "real.yaml", tmp_path / "link.yaml")
        with (
            patch.object(gitops_push, "_git_listed_paths", return_value=None),
            pytest.raises(gitops_push.GitOpsPushError, match=r"symbolic links.*link\.yaml"),
        ):
            gitops_push.collect_local_tree(tmp_path)

    def test_oversized_files_are_refused_by_name(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"ok.yaml": "ok"})
        big = tmp_path / "big.bin"
        with big.open("wb") as handle:
            handle.truncate(gitops_push.MAX_FILE_BYTES + 1)
        with (
            patch.object(gitops_push, "_git_listed_paths", return_value=None),
            pytest.raises(gitops_push.GitOpsPushError, match=r"6 MiB per-file limit: big\.bin"),
        ):
            gitops_push.collect_local_tree(tmp_path)

    def test_empty_directory_is_refused(self, tmp_path: Path) -> None:
        with (
            patch.object(gitops_push, "_git_listed_paths", return_value=None),
            pytest.raises(gitops_push.GitOpsPushError, match="contains no files to push"),
        ):
            gitops_push.collect_local_tree(tmp_path)

    def test_not_a_directory(self, tmp_path: Path) -> None:
        with pytest.raises(gitops_push.GitOpsPushError, match="is not a directory"):
            gitops_push.collect_local_tree(tmp_path / "missing")

    def test_gitlink_files_and_special_files_are_skipped(self, tmp_path: Path) -> None:
        """A ``.git`` *file* (worktree / submodule gitlink) and non-regular files never ship."""
        _write_tree(tmp_path, {"a.yaml": "a", ".git": "gitdir: /elsewhere/.git/worktrees/x\n"})
        os.mkfifo(tmp_path / "pipe")
        with patch.object(gitops_push, "_git_listed_paths", return_value=None):
            files = gitops_push.collect_local_tree(tmp_path)
        assert [item.path for item in files] == ["a.yaml"]

    def test_without_a_git_binary_the_tree_is_walked_and_the_author_is_the_placeholder(
        self, tmp_path: Path
    ) -> None:
        _write_tree(tmp_path, {"a.yaml": "a"})
        with patch.object(gitops_push.shutil, "which", return_value=None):
            assert gitops_push._git(tmp_path, "rev-parse", "HEAD") is None
            assert gitops_push._git_listed_paths(tmp_path) is None
            assert [item.path for item in gitops_push.collect_local_tree(tmp_path)] == ["a.yaml"]
            assert gitops_push.default_author() == (
                gitops_push.DEFAULT_AUTHOR_NAME,
                gitops_push.DEFAULT_AUTHOR_EMAIL,
            )

    def test_a_work_tree_whose_listing_fails_falls_back_to_the_walk(self, tmp_path: Path) -> None:
        """``rev-parse`` says work tree but ``ls-files`` fails: walk rather than push nothing."""
        _write_tree(tmp_path, {"a.yaml": "a"})

        def fake_git(source_dir: Path, *args: str) -> str | None:
            return "true\n" if args[0] == "rev-parse" else None

        with patch.object(gitops_push, "_git", side_effect=fake_git):
            assert gitops_push._git_listed_paths(tmp_path) is None
            assert [item.path for item in gitops_push.collect_local_tree(tmp_path)] == ["a.yaml"]

    def test_source_revision_marks_dirty_trees(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        _write_tree(tmp_path, {"a.yaml": "a"})
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                "commit",
                "-q",
                "-m",
                "init",
            ],
            check=True,
        )
        head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert gitops_push.local_source_revision(tmp_path) == head
        (tmp_path / "b.yaml").write_text("b")
        assert gitops_push.local_source_revision(tmp_path) == f"{head}-dirty"
        with patch.object(gitops_push, "_git", return_value=None):
            assert gitops_push.local_source_revision(tmp_path) is None
        assert gitops_push.default_commit_message(tmp_path, head) == (
            f"gco gitops push: {tmp_path.name} @ {head}"
        )


# ─── remote tree, plan, commits ──────────────────────────────────────────────


def _local(
    files: dict[str, bytes], *, executable: set[str] = frozenset()
) -> list[gitops_push.LocalFile]:
    return [
        gitops_push.LocalFile(
            path=path,
            content=content,
            mode=gitops_push.FILE_MODE_EXECUTABLE
            if path in executable
            else gitops_push.FILE_MODE_NORMAL,
            blob_id=gitops_push.git_blob_id(content),
        )
        for path, content in sorted(files.items())
    ]


def _push(
    client: FakeCodeCommit, files: list[gitops_push.LocalFile], **overrides: Any
) -> gitops_push.PushResult:
    kwargs: dict[str, Any] = {
        "region": _REGION,
        "repository_name": _REPOSITORY,
        "repository_url": f"https://git-codecommit.{_REGION}.amazonaws.com/v1/repos/{_REPOSITORY}",
        "branch": "main",
        "local_files": files,
        "source_path": "/work/manifests",
        "source_revision": "abc123",
        "message": "gco gitops push: manifests @ abc123",
        "author": ("t", "t@example.com"),
    }
    kwargs.update(overrides)
    return gitops_push.push_tree(client, **kwargs)


class TestPushTree:
    def test_first_push_into_an_empty_repository_creates_the_branch(self) -> None:
        client = FakeCodeCommit()
        result = _push(client, _local({"a.yaml": b"a", "dir/b.yaml": b"b"}, executable={"a.yaml"}))
        assert result.parent_commit_id is None
        assert len(result.commit_ids) == 1
        assert result.head_commit_id == result.commit_ids[0]
        assert (result.added, result.modified, result.deleted, result.unchanged) == (2, 0, 0, 0)
        assert result.changed
        request = client.create_calls[0]
        assert "parentCommitId" not in request
        assert request["commitMessage"] == "gco gitops push: manifests @ abc123"
        assert request["authorName"] == "t" and request["email"] == "t@example.com"
        assert request["keepEmptyFolders"] is False
        assert [(item["filePath"], item["fileMode"]) for item in request["putFiles"]] == [
            ("a.yaml", "EXECUTABLE"),
            ("dir/b.yaml", "NORMAL"),
        ]
        assert "deleteFiles" not in request

    def test_diff_puts_changed_files_deletes_removed_ones_and_skips_unchanged(self) -> None:
        client = FakeCodeCommit(
            tree={
                "same.yaml": (b"same", "NORMAL"),
                "changed.yaml": (b"old", "NORMAL"),
                "mode.sh": (b"#!/bin/sh\n", "NORMAL"),
                "removed.yaml": (b"gone", "NORMAL"),
            }
        )
        parent = client.branches["main"]
        result = _push(
            client,
            _local(
                {
                    "same.yaml": b"same",
                    "changed.yaml": b"new",
                    "mode.sh": b"#!/bin/sh\n",
                    "new.yaml": b"n",
                },
                executable={"mode.sh"},
            ),
        )
        assert result.parent_commit_id == parent
        assert (result.added, result.modified, result.deleted, result.unchanged) == (1, 2, 1, 1)
        request = client.create_calls[0]
        assert request["parentCommitId"] == parent
        assert sorted(item["filePath"] for item in request["putFiles"]) == [
            "changed.yaml",
            "mode.sh",
            "new.yaml",
        ]
        assert request["deleteFiles"] == [{"filePath": "removed.yaml"}]
        assert set(client.branch_tree()) == {"same.yaml", "changed.yaml", "mode.sh", "new.yaml"}
        assert client.branch_tree()["mode.sh"][1] == "EXECUTABLE"

    def test_no_change_means_no_commit(self) -> None:
        client = FakeCodeCommit(tree={"a.yaml": (b"a", "NORMAL")})
        result = _push(client, _local({"a.yaml": b"a"}))
        assert not result.changed
        assert result.commit_ids == []
        assert result.head_commit_id == result.parent_commit_id == client.branches["main"]
        assert client.create_calls == []

    def test_dry_run_plans_without_committing(self) -> None:
        client = FakeCodeCommit(tree={"a.yaml": (b"a", "NORMAL")})
        result = _push(client, _local({"b.yaml": b"b"}), dry_run=True)
        assert result.dry_run and result.changed
        assert (result.added, result.deleted) == (1, 1)
        assert client.create_calls == []
        assert result.head_commit_id == client.branches["main"]

    def test_large_pushes_are_chained_in_service_sized_batches(self) -> None:
        client = FakeCodeCommit(tree={f"old-{index}.yaml": (b"o", "NORMAL") for index in range(30)})
        files = _local({f"file-{index:03}.yaml": b"x" for index in range(250)})
        result = _push(client, files)
        # 250 puts + 30 deletes = 280 operations -> 3 commits of <= 100 each,
        # every one parented on the previous.
        assert len(result.commit_ids) == 3
        assert result.head_commit_id == result.commit_ids[-1]
        parents = [request.get("parentCommitId") for request in client.create_calls]
        assert parents[0] == result.parent_commit_id
        assert parents[1:] == result.commit_ids[:-1]
        messages = [request["commitMessage"] for request in client.create_calls]
        assert messages == [f"gco gitops push: manifests @ abc123 (part {i}/3)" for i in (1, 2, 3)]
        assert set(client.branch_tree()) == {item.path for item in files}

    def test_byte_budget_splits_batches(self) -> None:
        client = FakeCodeCommit()
        chunk = b"x" * (6 * 1024 * 1024)  # at the per-file limit
        files = _local({f"big-{index}.bin": chunk for index in range(3)})
        result = _push(client, files)
        # 18 MiB over a 15 MiB budget -> two requests.
        assert len(result.commit_ids) == 2
        assert [len(request["putFiles"]) for request in client.create_calls] == [2, 1]

    def test_missing_repository_names_the_deploy(self) -> None:
        client = FakeCodeCommit(exists=False)
        with pytest.raises(
            gitops_push.GitOpsPushError, match="does not exist; deploy the regional stack"
        ):
            _push(client, _local({"a.yaml": b"a"}))

    def test_other_client_errors_propagate(self) -> None:
        class Broken(FakeCodeCommit):
            def get_branch(self, **_: Any) -> dict[str, Any]:
                raise _client_error("AccessDeniedException", "GetBranch")

        with pytest.raises(ClientError, match="AccessDeniedException"):
            _push(Broken(), _local({"a.yaml": b"a"}))

    def test_remote_tree_maps_git_modes_and_paginates(self) -> None:
        client = FakeCodeCommit(
            tree={"a": (b"a", "NORMAL"), "b": (b"b", "EXECUTABLE"), "c": (b"c", "NORMAL")}
        )
        tree = gitops_push.remote_tree(client, _REPOSITORY, client.branches["main"])
        assert tree == {
            "a": (gitops_push.git_blob_id(b"a"), "NORMAL"),
            "b": (gitops_push.git_blob_id(b"b"), "EXECUTABLE"),
            "c": (gitops_push.git_blob_id(b"c"), "NORMAL"),
        }

    def test_result_document(self) -> None:
        client = FakeCodeCommit()
        result = _push(client, _local({"a.yaml": b"a"}))
        document = result.to_dict()
        assert document["region"] == _REGION
        assert document["repository_name"] == _REPOSITORY
        assert document["changed"] is True and document["dry_run"] is False
        assert document["commit_ids"] == result.commit_ids
        json.dumps(document)  # JSON-safe for --output json

    def test_delete_only_push_sends_no_put_files(self) -> None:
        """Files removed locally, nothing else changed: the commit carries only deletions."""
        client = FakeCodeCommit(tree={"keep.yaml": (b"k", "NORMAL"), "gone.yaml": (b"g", "NORMAL")})
        result = _push(client, _local({"keep.yaml": b"k"}))
        assert (result.added, result.modified, result.deleted, result.unchanged) == (0, 0, 1, 1)
        request = client.create_calls[0]
        assert "putFiles" not in request
        assert request["deleteFiles"] == [{"filePath": "gone.yaml"}]
        assert set(client.branch_tree()) == {"keep.yaml"}

    def test_delete_batches_split_at_the_file_limit_and_an_empty_plan_has_no_batches(
        self,
    ) -> None:
        plan = gitops_push.PushPlan()
        plan.delete = [f"old-{index:03}.yaml" for index in range(150)]
        assert plan.deleted == 150
        batches = gitops_push._chunks(plan)
        assert [(len(puts), len(deletes)) for puts, deletes in batches] == [(0, 100), (0, 50)]
        assert gitops_push._chunks(gitops_push.PushPlan()) == []

    def test_remote_tree_skips_differences_without_a_blob(self) -> None:
        """Deletions and malformed entries in GetDifferences carry no afterBlob path/id."""

        class Sparse(FakeCodeCommit):
            def get_paginator(self, operation: str) -> Any:
                class _Paginator:
                    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
                        return [
                            {
                                "differences": [
                                    {"changeType": "D", "beforeBlob": {"path": "gone.yaml"}},
                                    {"afterBlob": {"path": "no-id.yaml"}},
                                    {"afterBlob": {"blobId": "abc", "mode": "100644"}},
                                    {
                                        "afterBlob": {
                                            "blobId": "def",
                                            "path": "ok.yaml",
                                            "mode": "100644",
                                        }
                                    },
                                ]
                            }
                        ]

                return _Paginator()

        assert gitops_push.remote_tree(Sparse(), _REPOSITORY, "c1") == {
            "ok.yaml": ("def", "NORMAL")
        }

    def test_client_error_code_reads_only_botocore_shaped_errors(self) -> None:
        assert gitops_push._client_error_code(_client_error("Throttling", "GetBranch")) == (
            "Throttling"
        )
        assert gitops_push._client_error_code(RuntimeError("plain")) == ""


# ─── resolution against cdk.json ─────────────────────────────────────────────


class TestResolveRepository:
    def test_codecommit_source_yields_the_managed_repository(self) -> None:
        assert gitops_push.resolve_gitops_repository(_REGION, _PROJECT, _config()) == (
            _REPOSITORY,
            f"https://git-codecommit.{_REGION}.amazonaws.com/v1/repos/{_REPOSITORY}",
        )

    def test_git_source_is_not_pushable(self) -> None:
        with pytest.raises(
            gitops_push.GitOpsPushError, match=r"gitops\.source is 'git'.*your own Git tooling"
        ):
            gitops_push.resolve_gitops_repository(_REGION, _PROJECT, _config(source="git"))

    def test_disabled_hand_off_is_not_pushable(self) -> None:
        with pytest.raises(gitops_push.GitOpsPushError, match="not enabled for this region"):
            gitops_push.resolve_gitops_repository(_REGION, _PROJECT, _config(gitops_enabled=False))

    def test_push_gitops_repository_wires_everything(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"cm.yaml": "kind: ConfigMap\n"})
        client = FakeCodeCommit()
        with patch.object(gitops_push, "_git_listed_paths", return_value=None):
            result = gitops_push.push_gitops_repository(
                _REGION,
                _PROJECT,
                tmp_path,
                config=_config(),
                message="seed",
                codecommit_client=client,
            )
        assert result.repository_name == _REPOSITORY
        assert result.branch == caps.GITOPS_CODECOMMIT_DEFAULT_BRANCH
        assert result.source_path == str(tmp_path.resolve())
        assert client.create_calls[0]["commitMessage"] == "seed"
        assert list(client.branch_tree()) == ["cm.yaml"]

    def test_default_client_and_config(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"cm.yaml": "x"})
        client = FakeCodeCommit()
        with (
            patch.object(gitops_push, "_git_listed_paths", return_value=None),
            patch("cli.gitops_push.load_eks_capabilities_config", return_value=_config()),
            patch("boto3.client", return_value=client) as boto_client,
        ):
            gitops_push.push_gitops_repository(_REGION, _PROJECT, tmp_path)
        boto_client.assert_called_once_with("codecommit", region_name=_REGION)


# ─── the Click command ───────────────────────────────────────────────────────


class TestPushCommand:
    def _invoke(
        self,
        args: list[str],
        client: FakeCodeCommit,
        config: dict[str, Any] | None = None,
        *,
        config_error: Exception | None = None,
    ) -> Any:
        loader = MagicMock(return_value=config or _config())
        if config_error is not None:
            loader.side_effect = config_error
        with (
            patch("cli.eks_capabilities.load_eks_capabilities_config", loader),
            patch("cli.gitops_push.load_eks_capabilities_config", return_value=config or _config()),
            patch("cli.gitops_push._git_listed_paths", return_value=None),
            patch("cli.gitops_push.default_author", return_value=("t", "t@example.com")),
            patch("boto3.client", return_value=client),
            patch("cli.commands.stacks_cmd._project_name", return_value=_PROJECT),
            patch(
                "cli.commands.stacks_cmd._load_cdk_json",
                return_value={"regional": [_REGION, "us-west-2"]},
            ),
        ):
            return CliRunner().invoke(cli, args)

    def test_pushes_and_reports(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"cm.yaml": "kind: ConfigMap\n"})
        client = FakeCodeCommit()
        result = self._invoke(
            ["stacks", "capabilities", "gitops", "push", "--path", str(tmp_path), "-y"], client
        )
        assert result.exit_code == 0, result.output
        assert f"{_REPOSITORY}@main ->" in result.output
        assert "+1 added" in result.output
        assert list(client.branch_tree()) == ["cm.yaml"]

    def test_json_output_is_the_result_document(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"cm.yaml": "x"})
        client = FakeCodeCommit()
        result = self._invoke(
            [
                "--output",
                "json",
                "stacks",
                "capabilities",
                "gitops",
                "push",
                "--path",
                str(tmp_path),
                "-y",
            ],
            client,
        )
        assert result.exit_code == 0, result.output
        document = json.loads(result.output)
        assert document["repository_name"] == _REPOSITORY
        assert document["added"] == 1 and document["changed"] is True

    def test_all_regions_pushes_once_per_region_and_lists_documents(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"cm.yaml": "x"})
        client = FakeCodeCommit()
        result = self._invoke(
            [
                "--output",
                "json",
                "stacks",
                "capabilities",
                "gitops",
                "push",
                "--path",
                str(tmp_path),
                "-A",
                "-y",
            ],
            client,
        )
        assert result.exit_code == 0, result.output
        documents = json.loads(result.output)
        assert [item["region"] for item in documents] == [_REGION, "us-west-2"]
        assert [item["repository_name"] for item in documents] == [
            _REPOSITORY,
            "gco-us-west-2-gitops",
        ]

    def test_dry_run_commits_nothing(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"cm.yaml": "x"})
        client = FakeCodeCommit()
        result = self._invoke(
            ["stacks", "capabilities", "gitops", "push", "--path", str(tmp_path), "--dry-run"],
            client,
        )
        assert result.exit_code == 0, result.output
        assert "dry run" in result.output
        assert client.create_calls == []

    def test_confirmation_is_required_without_yes(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"cm.yaml": "x"})
        client = FakeCodeCommit()
        result = self._invoke(
            ["stacks", "capabilities", "gitops", "push", "--path", str(tmp_path)], client
        )
        assert result.exit_code != 0
        assert client.create_calls == []

    def test_git_source_region_fails_before_reading_the_tree(self, tmp_path: Path) -> None:
        client = FakeCodeCommit()
        result = self._invoke(
            ["stacks", "capabilities", "gitops", "push", "--path", str(tmp_path), "-y"],
            client,
            config=_config(source="git"),
        )
        assert result.exit_code == 1
        assert "your own Git tooling" in result.output
        assert client.create_calls == []

    def test_missing_repository_is_reported_per_region(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"cm.yaml": "x"})
        result = self._invoke(
            ["stacks", "capabilities", "gitops", "push", "--path", str(tmp_path), "-y"],
            FakeCodeCommit(exists=False),
        )
        assert result.exit_code == 1
        assert "deploy the regional stack" in result.output

    def test_nothing_to_commit_is_not_an_error(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"cm.yaml": "x"})
        client = FakeCodeCommit(tree={"cm.yaml": (b"x", "NORMAL")})
        result = self._invoke(
            ["stacks", "capabilities", "gitops", "push", "--path", str(tmp_path), "-y"], client
        )
        assert result.exit_code == 0, result.output
        assert "already matches" in result.output
        assert client.create_calls == []

    def test_unreadable_cdk_json_block_exits_before_any_aws_call(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"cm.yaml": "x"})
        client = FakeCodeCommit()
        result = self._invoke(
            ["stacks", "capabilities", "gitops", "push", "--path", str(tmp_path), "-y"],
            client,
            config_error=caps.EksCapabilitiesConfigError(
                "eks_capabilities.argocd.gitops.source must be one of codecommit, git"
            ),
        )
        assert result.exit_code == 1
        assert "Failed to read eks_capabilities from cdk.json" in result.output
        assert "gitops.source must be one of" in result.output
        assert client.create_calls == []

    def test_unexpected_codecommit_errors_are_reported_per_region(self, tmp_path: Path) -> None:
        """A throttle or outage mid-push is reported with its Region and fails the command."""
        _write_tree(tmp_path, {"cm.yaml": "x"})

        class Throttled(FakeCodeCommit):
            def create_commit(self, **request: Any) -> dict[str, Any]:
                raise _client_error("ThrottlingException", "CreateCommit")

        result = self._invoke(
            ["stacks", "capabilities", "gitops", "push", "--path", str(tmp_path), "-A", "-y"],
            Throttled(),
        )
        assert result.exit_code == 1
        assert f"[{_REGION}] Push to CodeCommit failed" in result.output
        assert "[us-west-2] Push to CodeCommit failed" in result.output
        assert "ThrottlingException" in result.output
        assert "Traceback" not in result.output


def test_gitops_push_module_imports_without_aws_cdk() -> None:
    """The CLI runs without the CDK toolchain; the push library must not pull it in."""
    code = (
        "import sys; sys.modules['aws_cdk'] = None; import cli.gitops_push; "
        "assert 'aws_cdk' not in sys.modules or sys.modules['aws_cdk'] is None"
    )
    subprocess.run(
        [__import__("sys").executable, "-c", code],
        check=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
