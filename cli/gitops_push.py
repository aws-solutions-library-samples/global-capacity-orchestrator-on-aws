"""Mirror a local directory into a GCO-managed CodeCommit GitOps repository.

Backs ``gco stacks capabilities gitops push`` and the ``gitops_push`` MCP tool,
and is what the live-validation harness uses to seed the per-cluster
repository it validates against. The GitOps hand-off's default source
(``eks_capabilities.argocd.gitops.source: codecommit``, see
``docs/EKS_CAPABILITIES.md``) gives every selected cluster its own AWS
CodeCommit repository that the hosted Argo CD reads with its capability role.
This module fills it: the branch's tree becomes an exact mirror of the local
directory, one commit per push and only when something changed.

The push goes through the CodeCommit API (``GetBranch`` / ``GetDifferences`` /
``CreateCommit``) rather than ``git push`` on purpose. It needs nothing but the
operator's AWS credentials — no Git credential helper, no
``git-remote-codecommit``, no SSH key upload — so the same call works from a
laptop, CI, and the harness, and the diff is computed locally by comparing Git
blob ids (``GetDifferences`` returns them for every remote file), so unchanged
files are never re-uploaded.

What counts as "the directory": inside a Git work tree the tracked and
untracked-but-not-ignored files under the directory (what ``git add -A`` would
commit, so ``.gitignore`` is honored); outside one, every regular file below
it except ``.git``. Symlinks and files above CodeCommit's per-file limit are
refused rather than silently altered. Imports nothing from ``aws_cdk``: the CLI
runs without the CDK toolchain installed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from gco.eks_capabilities_config import (
    GITOPS_CODECOMMIT_DEFAULT_BRANCH,
    gitops_codecommit_enabled_in_region,
    gitops_codecommit_repository_name,
    gitops_enabled_in_region,
    gitops_repository_url,
    gitops_source,
)

from .eks_capabilities import cluster_name_for, load_eks_capabilities_config

#: CodeCommit refuses individual files above 6 MiB in a CreateCommit request.
MAX_FILE_BYTES = 6 * 1024 * 1024
#: CreateCommit accepts at most 100 file operations per request.
MAX_FILES_PER_COMMIT = 100
#: Total file content per CreateCommit request GCO stays under (the service's
#: request-size ceiling is 20 MB; leave headroom for the JSON envelope).
MAX_BYTES_PER_COMMIT = 15 * 1024 * 1024

#: CodeCommit ``fileMode`` values (the API's own spelling).
FILE_MODE_NORMAL = "NORMAL"
FILE_MODE_EXECUTABLE = "EXECUTABLE"
#: Git tree modes ``GetDifferences`` reports -> CodeCommit ``fileMode``.
_GIT_MODE_TO_FILE_MODE: dict[str, str] = {
    "100644": FILE_MODE_NORMAL,
    "100755": FILE_MODE_EXECUTABLE,
}

DEFAULT_AUTHOR_NAME = "gco"
DEFAULT_AUTHOR_EMAIL = "gco@localhost"


class GitOpsPushError(RuntimeError):
    """The push cannot proceed (bad source directory, wrong source, missing repository)."""


@dataclass(frozen=True)
class LocalFile:
    """One file of the local tree, addressed by its repository-relative POSIX path."""

    path: str
    content: bytes
    mode: str
    blob_id: str


@dataclass
class PushPlan:
    """The diff between the local tree and the remote branch."""

    put: list[LocalFile] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    added: int = 0
    modified: int = 0
    unchanged: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.put or self.delete)

    @property
    def deleted(self) -> int:
        return len(self.delete)


@dataclass
class PushResult:
    """What one region's push did (or, for a dry run, would do)."""

    region: str
    repository_name: str
    repository_url: str
    branch: str
    source_path: str
    source_revision: str | None
    parent_commit_id: str | None
    head_commit_id: str | None
    commit_ids: list[str]
    added: int
    modified: int
    deleted: int
    unchanged: int
    dry_run: bool

    @property
    def changed(self) -> bool:
        return bool(self.added or self.modified or self.deleted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "repository_name": self.repository_name,
            "repository_url": self.repository_url,
            "branch": self.branch,
            "source_path": self.source_path,
            "source_revision": self.source_revision,
            "parent_commit_id": self.parent_commit_id,
            "head_commit_id": self.head_commit_id,
            "commit_ids": list(self.commit_ids),
            "added": self.added,
            "modified": self.modified,
            "deleted": self.deleted,
            "unchanged": self.unchanged,
            "changed": self.changed,
            "dry_run": self.dry_run,
        }


# ─── the local tree ──────────────────────────────────────────────────────────


def git_blob_id(content: bytes) -> str:
    """The Git object id of ``content`` as a blob (what CodeCommit reports per file).

    SHA-1 is not a choice here: Git's object model (and therefore the
    ``blobId`` CodeCommit returns from ``GetDifferences``) is defined over it,
    and the id is compared for equality to skip unchanged files, never used as
    a signature or integrity guarantee.
    """
    header = f"blob {len(content)}\0".encode()
    # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1 -- Git blob ids are SHA-1 by definition; content addressing, not a security control
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def _git(source_dir: Path, *args: str) -> str | None:
    """Run ``git -C source_dir`` and return stdout, or ``None`` when git is absent or fails."""
    if shutil.which("git") is None:
        return None
    result = subprocess.run(  # fixed argv, never a shell string
        ["git", "-C", str(source_dir), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _git_listed_paths(source_dir: Path) -> list[str] | None:
    """Repository-relative paths under ``source_dir`` per Git, or None outside a work tree."""
    if _git(source_dir, "rev-parse", "--is-inside-work-tree") is None:
        return None
    listing = _git(
        source_dir, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."
    )
    if listing is None:
        return None
    return [item for item in listing.split("\0") if item]


def local_source_revision(source_dir: Path) -> str | None:
    """``<HEAD sha>`` or ``<HEAD sha>-dirty`` when the directory is in a Git work tree, else None."""
    head = _git(source_dir, "rev-parse", "HEAD")
    if head is None or not head.strip():
        return None
    porcelain = _git(source_dir, "status", "--porcelain", "--untracked-files=all", "--", ".")
    dirty = bool(porcelain and porcelain.strip())
    return head.strip() + ("-dirty" if dirty else "")


def _walk_paths(source_dir: Path) -> list[str]:
    paths: list[str] = []
    for root, dirnames, filenames in os.walk(source_dir):
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        for filename in sorted(filenames):
            relative = Path(root, filename).relative_to(source_dir)
            paths.append(relative.as_posix())
    return paths


def collect_local_tree(source_dir: Path) -> list[LocalFile]:
    """Read every file the push mirrors, in path order.

    Honors ``.gitignore`` through ``git ls-files`` when the directory is inside
    a Git work tree (tracked plus untracked-but-not-ignored files; a tracked
    file deleted from disk is skipped, so the push mirrors what is really
    there). Refuses symlinks and files over :data:`MAX_FILE_BYTES` by name.
    """
    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        raise GitOpsPushError(f"{source_dir} is not a directory")
    relative_paths = _git_listed_paths(source_dir)
    if relative_paths is None:
        relative_paths = _walk_paths(source_dir)

    files: list[LocalFile] = []
    symlinks: list[str] = []
    oversized: list[str] = []
    for relative in sorted(dict.fromkeys(relative_paths)):
        posix = PurePosixPath(relative)
        if ".git" in posix.parts:
            continue
        absolute = source_dir / Path(*posix.parts)
        try:
            info = absolute.lstat()
        except FileNotFoundError:
            continue  # tracked in Git but deleted from disk: not part of the tree
        if stat.S_ISLNK(info.st_mode):
            symlinks.append(posix.as_posix())
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if info.st_size > MAX_FILE_BYTES:
            oversized.append(posix.as_posix())
            continue
        content = absolute.read_bytes()
        mode = FILE_MODE_EXECUTABLE if info.st_mode & stat.S_IXUSR else FILE_MODE_NORMAL
        files.append(
            LocalFile(
                path=posix.as_posix(), content=content, mode=mode, blob_id=git_blob_id(content)
            )
        )
    if symlinks:
        raise GitOpsPushError(
            "symbolic links are not mirrored; replace them with regular files: "
            + ", ".join(symlinks)
        )
    if oversized:
        raise GitOpsPushError(
            f"files above CodeCommit's {MAX_FILE_BYTES // (1024 * 1024)} MiB per-file limit: "
            + ", ".join(oversized)
        )
    if not files:
        raise GitOpsPushError(
            f"{source_dir} contains no files to push; refusing to empty the repository branch "
            "(delete objects by removing them from the directory and pushing again, not by "
            "pushing an empty tree)"
        )
    return files


# ─── the remote branch ───────────────────────────────────────────────────────


def _client_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        return str(response.get("Error", {}).get("Code") or "")
    return ""


def branch_head(client: Any, repository_name: str, branch: str) -> str | None:
    """The branch's commit id, or ``None`` for a branch that does not exist yet."""
    from botocore.exceptions import ClientError

    try:
        response = client.get_branch(repositoryName=repository_name, branchName=branch)
    except ClientError as exc:
        code = _client_error_code(exc)
        if code == "BranchDoesNotExistException":
            return None
        if code == "RepositoryDoesNotExistException":
            raise GitOpsPushError(
                f"CodeCommit repository {repository_name!r} does not exist; deploy the regional "
                "stack with eks_capabilities.argocd.gitops enabled (source: codecommit) first"
            ) from exc
        raise
    commit_id = (response.get("branch") or {}).get("commitId")
    return str(commit_id) if commit_id else None


def remote_tree(client: Any, repository_name: str, commit_id: str) -> dict[str, tuple[str, str]]:
    """``{path: (blob_id, file_mode)}`` for every file at ``commit_id``.

    ``GetDifferences`` with only an ``afterCommitSpecifier`` lists the whole
    tree, blob ids included, in pages — one paginated call instead of a
    ``GetFolder`` walk.
    """
    tree: dict[str, tuple[str, str]] = {}
    paginator = client.get_paginator("get_differences")
    for page in paginator.paginate(repositoryName=repository_name, afterCommitSpecifier=commit_id):
        for difference in page.get("differences") or []:
            blob = difference.get("afterBlob") or {}
            path = blob.get("path")
            blob_id = blob.get("blobId")
            if not path or not blob_id:
                continue
            mode = _GIT_MODE_TO_FILE_MODE.get(
                str(blob.get("mode") or ""), str(blob.get("mode") or "")
            )
            tree[str(path)] = (str(blob_id), mode)
    return tree


def plan_push(local_files: Iterable[LocalFile], remote: Mapping[str, tuple[str, str]]) -> PushPlan:
    """Which files to put (new or changed content/mode) and which remote paths to delete."""
    plan = PushPlan()
    local_by_path = {item.path: item for item in local_files}
    for path, item in sorted(local_by_path.items()):
        existing = remote.get(path)
        if existing is None:
            plan.put.append(item)
            plan.added += 1
        elif existing != (item.blob_id, item.mode):
            plan.put.append(item)
            plan.modified += 1
        else:
            plan.unchanged += 1
    plan.delete = sorted(path for path in remote if path not in local_by_path)
    return plan


# ─── the commits ─────────────────────────────────────────────────────────────


def _chunks(plan: PushPlan) -> list[tuple[list[LocalFile], list[str]]]:
    """Split the plan into CreateCommit-sized batches (file count and byte budget)."""
    batches: list[tuple[list[LocalFile], list[str]]] = []
    puts: list[LocalFile] = []
    deletes: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal puts, deletes, size
        if puts or deletes:
            batches.append((puts, deletes))
        puts, deletes, size = [], [], 0

    for item in plan.put:
        batch_full = len(puts) + len(deletes) >= MAX_FILES_PER_COMMIT
        over_budget = size + len(item.content) > MAX_BYTES_PER_COMMIT
        if (puts or deletes) and (batch_full or over_budget):
            flush()
        puts.append(item)
        size += len(item.content)
    for path in plan.delete:
        if len(puts) + len(deletes) >= MAX_FILES_PER_COMMIT:
            flush()
        deletes.append(path)
    flush()
    return batches


def default_author() -> tuple[str, str]:
    """``(name, email)`` from the operator's Git identity, or GCO's placeholders."""
    if shutil.which("git") is None:
        return DEFAULT_AUTHOR_NAME, DEFAULT_AUTHOR_EMAIL
    name = subprocess.run(  # fixed argv
        ["git", "config", "--get", "user.name"], capture_output=True, text=True, check=False
    ).stdout.strip()
    email = subprocess.run(  # fixed argv
        ["git", "config", "--get", "user.email"], capture_output=True, text=True, check=False
    ).stdout.strip()
    return name or DEFAULT_AUTHOR_NAME, email or DEFAULT_AUTHOR_EMAIL


def default_commit_message(source_dir: Path, source_revision: str | None) -> str:
    label = source_dir.resolve().name or str(source_dir)
    suffix = f" @ {source_revision}" if source_revision else ""
    return f"gco gitops push: {label}{suffix}"


def push_tree(
    client: Any,
    *,
    region: str,
    repository_name: str,
    repository_url: str,
    branch: str,
    local_files: list[LocalFile],
    source_path: str,
    source_revision: str | None,
    message: str,
    author: tuple[str, str] | None = None,
    dry_run: bool = False,
) -> PushResult:
    """Make ``branch`` mirror ``local_files``; return what happened.

    Chains one ``CreateCommit`` per :func:`_chunks` batch (each parented on
    the previous commit), so a large first push lands as a short series of
    commits rather than failing the service's per-request limits. An empty
    repository (no branch yet) gets its first commit with no parent, which
    also creates the branch. No change means no commit.
    """
    parent = branch_head(client, repository_name, branch)
    remote = remote_tree(client, repository_name, parent) if parent else {}
    plan = plan_push(local_files, remote)
    result = PushResult(
        region=region,
        repository_name=repository_name,
        repository_url=repository_url,
        branch=branch,
        source_path=source_path,
        source_revision=source_revision,
        parent_commit_id=parent,
        head_commit_id=parent,
        commit_ids=[],
        added=plan.added,
        modified=plan.modified,
        deleted=plan.deleted,
        unchanged=plan.unchanged,
        dry_run=dry_run,
    )
    if dry_run or not plan.changed:
        return result

    author_name, author_email = author or default_author()
    batches = _chunks(plan)
    head = parent
    for index, (puts, deletes) in enumerate(batches, start=1):
        request: dict[str, Any] = {
            "repositoryName": repository_name,
            "branchName": branch,
            "authorName": author_name,
            "email": author_email,
            "commitMessage": (
                message if len(batches) == 1 else f"{message} (part {index}/{len(batches)})"
            ),
            "keepEmptyFolders": False,
        }
        if head:
            request["parentCommitId"] = head
        if puts:
            request["putFiles"] = [
                {"filePath": item.path, "fileMode": item.mode, "fileContent": item.content}
                for item in puts
            ]
        if deletes:
            request["deleteFiles"] = [{"filePath": path} for path in deletes]
        response = client.create_commit(**request)
        head = str(response["commitId"])
        result.commit_ids.append(head)
    result.head_commit_id = head
    return result


# ─── resolution against the GCO configuration ────────────────────────────────


def resolve_gitops_repository(
    region: str,
    project_name: str,
    config: Mapping[str, Any],
) -> tuple[str, str]:
    """``(repository_name, repository_url)`` of the region's GCO-managed repository.

    Raises :class:`GitOpsPushError` when the hand-off is off for the region or
    reads an operator-owned repository (``source: git``), which GCO never
    writes to.
    """
    cluster_name = cluster_name_for(project_name, region)
    if not gitops_enabled_in_region(config, region):
        raise GitOpsPushError(
            f"{region}: the Argo CD GitOps hand-off is not enabled for this region "
            "(eks_capabilities.argocd.gitops.enabled with argocd.enabled and, if set, "
            "argocd.regions naming it); nothing to push to"
        )
    if not gitops_codecommit_enabled_in_region(config, region):
        raise GitOpsPushError(
            f"{region}: gitops.source is {gitops_source(config)!r} — Argo CD reads the "
            f"operator-owned repository {gitops_repository_url(config, region=region, cluster_name=cluster_name)!r}; "
            "push to it with your own Git tooling. 'gitops push' only writes GCO-managed "
            "CodeCommit repositories (source: codecommit)"
        )
    return (
        gitops_codecommit_repository_name(cluster_name),
        gitops_repository_url(config, region=region, cluster_name=cluster_name),
    )


def push_gitops_repository(
    region: str,
    project_name: str,
    source_dir: Path,
    *,
    config: Mapping[str, Any] | None = None,
    branch: str | None = None,
    message: str | None = None,
    dry_run: bool = False,
    local_files: list[LocalFile] | None = None,
    codecommit_client: Any | None = None,
) -> PushResult:
    """Mirror ``source_dir`` into one region's GCO-managed repository.

    ``local_files`` lets a multi-region caller read the directory once;
    ``codecommit_client`` lets tests and the harness inject a client.
    """
    if config is None:
        config = load_eks_capabilities_config()
    repository_name, repository_url = resolve_gitops_repository(region, project_name, config)
    files = local_files if local_files is not None else collect_local_tree(source_dir)
    revision = local_source_revision(source_dir)
    if codecommit_client is None:
        import boto3

        codecommit_client = boto3.client("codecommit", region_name=region)
    return push_tree(
        codecommit_client,
        region=region,
        repository_name=repository_name,
        repository_url=repository_url,
        branch=branch or GITOPS_CODECOMMIT_DEFAULT_BRANCH,
        local_files=files,
        source_path=str(source_dir.resolve()),
        source_revision=revision,
        message=message or default_commit_message(source_dir, revision),
        dry_run=dry_run,
    )
