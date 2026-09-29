"""The run's private workspace: a git mirror, the base-release clone, and its venv.

Nothing here touches AWS. The workspace holds

* ``mirror.git``: a bare repository with exactly two refs, the base release
  tag and the candidate commit (``refs/heads/candidate``). It borrows objects
  from the operator's repository through git alternates instead of copying
  them, and it is where the run's synthetic release tag lives, so no tag is
  ever created in the operator's repository;
* ``base``: a clone of the mirror checked out at the base tag. Its ``origin``
  is the mirror, which is how ``gco upgrade`` finds the synthetic tag;
* ``venv``: the base release's ``gco``, installed editable from the clone
  with the ``cdk`` extra pinned by the release's own lock file;
* ``logs``: the full output of every command the harness runs there.

Every command runs under ``umask 022`` (a restrictive umask makes the CDK
Lambda assets unreadable, which the deploy refuses) and with the base venv
first on ``PATH``, because the CDK app runs whichever ``python3`` it finds.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from cli.upgrade import ReleaseTag

#: File whose content (the run id) marks a directory as this run's workspace.
MARKER_NAME = ".gco-upgrade-validation-run"
#: Where the private mirror keeps the candidate commit.
CANDIDATE_REF = "refs/heads/candidate"
#: Lines of each command's output kept in the report.
LOG_TAIL_LINES = 40
#: Seconds a timed-out or interrupted command gets to exit after SIGTERM.
TERMINATE_GRACE_SECONDS = 60.0
#: Every workspace command creates files under this umask.
COMMAND_UMASK = 0o022

#: Environment the base ``gco`` must not inherit: another interpreter's paths,
#: an activated venv, git plumbing overrides, and output-format defaults.
_SCRUBBED_ENVIRONMENT = frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GCO_OUTPUT_FORMAT",
    }
)

Echo = Callable[[str], None]


@dataclass(frozen=True)
class Workspace:
    """Paths inside one run's workspace directory."""

    root: Path

    @property
    def mirror(self) -> Path:
        return self.root / "mirror.git"

    @property
    def clone(self) -> Path:
        return self.root / "base"

    @property
    def venv(self) -> Path:
        return self.root / "venv"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def cache(self) -> Path:
        return self.root / "gco-cache"

    @property
    def marker(self) -> Path:
        return self.root / MARKER_NAME

    @property
    def gco(self) -> Path:
        return self.venv / "bin" / "gco"

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    def owned_by(self, run_id: str) -> bool:
        """Whether the directory carries this run's marker."""
        try:
            return self.marker.read_text(encoding="utf-8").strip() == run_id
        except OSError:
            return False


def reset_workspace(workspace: Workspace, run_id: str) -> None:
    """Create an empty workspace for this run, clearing its own earlier attempt."""
    if workspace.root.exists():
        if not workspace.owned_by(run_id):
            raise RuntimeError(
                f"{workspace.root} exists and is not this run's workspace; remove it or "
                "pass another --workspace-dir"
            )
        shutil.rmtree(workspace.root)
    workspace.root.mkdir(parents=True, mode=0o700)
    workspace.marker.write_text(f"{run_id}\n", encoding="utf-8")
    workspace.logs.mkdir(mode=0o700)


def remove_workspace(workspace: Workspace, run_id: str) -> bool:
    """Delete this run's workspace; ``False`` when there is none."""
    if not workspace.root.exists():
        return False
    if not workspace.owned_by(run_id):
        raise RuntimeError(f"Refusing to delete {workspace.root}: it is not this run's workspace")
    shutil.rmtree(workspace.root)
    return True


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - fixed git argv, no shell
        ["git", *arguments],
        cwd=cwd,
        env=command_environment(None),
        capture_output=True,
        text=True,
        check=False,
        umask=COMMAND_UMASK,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise RuntimeError(f"git {' '.join(arguments)} failed in {cwd}: {message}")
    # Only trailing whitespace: porcelain status lines start with a space.
    return result.stdout.rstrip()


def build_mirror(
    workspace: Workspace,
    *,
    source: Path,
    branch: str,
    candidate_sha: str,
    base_ref: str,
    base_commit: str,
) -> dict[str, str]:
    """Create the bare mirror holding only the base tag and the candidate commit."""
    common_dir = Path(_git(source, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    _git(workspace.root, "init", "--quiet", "--bare", str(workspace.mirror))
    alternates = workspace.mirror / "objects" / "info" / "alternates"
    alternates.write_text(f"{common_dir / 'objects'}\n", encoding="utf-8")
    _git(
        workspace.mirror,
        "fetch",
        "--quiet",
        "--no-tags",
        str(source),
        f"+refs/tags/{base_ref}:refs/tags/{base_ref}",
        f"+refs/heads/{branch}:{CANDIDATE_REF}",
    )
    fetched_candidate = _git(workspace.mirror, "rev-parse", CANDIDATE_REF)
    fetched_base = _git(workspace.mirror, "rev-parse", f"refs/tags/{base_ref}^{{commit}}")
    if fetched_candidate != candidate_sha:
        raise RuntimeError(
            f"Branch {branch} is at {fetched_candidate}, not the validated {candidate_sha}"
        )
    if fetched_base != base_commit:
        raise RuntimeError(f"{base_ref} now names {fetched_base}, not {base_commit}")
    return {"path": str(workspace.mirror), "objects_from": str(common_dir / "objects")}


def clone_base(workspace: Workspace, *, base_ref: str, base_commit: str) -> dict[str, str]:
    """Clone the mirror at the base tag (a detached checkout, as a release install is)."""
    _git(
        workspace.root,
        "clone",
        "--quiet",
        "--shared",
        "--branch",
        base_ref,
        str(workspace.mirror),
        str(workspace.clone),
    )
    head = checkout_head(workspace)
    if head != base_commit:
        raise RuntimeError(f"The base clone is at {head}, not {base_ref} ({base_commit})")
    return {"path": str(workspace.clone), "head": head}


def checkout_head(workspace: Workspace) -> str:
    return _git(workspace.clone, "rev-parse", "HEAD")


def tracked_changes(workspace: Workspace) -> list[str]:
    """Tracked paths the clone's working tree changes (untracked files ignored)."""
    status = _git(workspace.clone, "status", "--porcelain=v1", "--untracked-files=no")
    return sorted(line[3:] for line in status.splitlines() if line.strip())


def tag_candidate(workspace: Workspace, *, tag: str, candidate_sha: str) -> dict[str, str]:
    """Create the synthetic release tag at the candidate commit, in the mirror only."""
    existing = _git(workspace.mirror, "tag", "--list", tag)
    if existing:
        target = _git(workspace.mirror, "rev-parse", f"refs/tags/{tag}^{{commit}}")
        if target != candidate_sha:
            raise RuntimeError(f"The mirror's {tag} names {target}, not {candidate_sha}")
    else:
        _git(workspace.mirror, "tag", tag, candidate_sha)
    return {"tag": tag, "commit": candidate_sha, "repository": str(workspace.mirror)}


def synthetic_release_tag(base_ref: str) -> str:
    """The next patch release after ``base_ref``: the name the candidate is tagged with.

    ``gco upgrade --ref`` accepts only release-shaped tags that exist on the
    remote. The tag is created in the run's private mirror, which holds no
    other release tag, so it can never collide with a published one.
    """
    tag = ReleaseTag.parse(base_ref)
    if tag is None:
        raise ValueError(f"{base_ref!r} is not a release tag")
    major, minor, patch = tag.version
    return f"v{major}.{minor}.{patch + 1}"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_run_context(
    clone: Path,
    *,
    run_tag_key: str,
    run_id: str,
    context: Mapping[str, str],
) -> dict[str, Any]:
    """Add the run tag and the run-scoped context to the clone's cdk.json.

    The app applies ``context.tags`` to every stack (``cdk.Tags.of(app)``),
    and ``gco upgrade`` writes cdk.json back byte for byte after its
    checkout, so both the base deploy and the upgrade's redeploy carry them.
    """
    path = clone / "cdk.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    settings = document.get("context") if isinstance(document, dict) else None
    if not isinstance(settings, dict):
        raise RuntimeError(f"{path} has no context object")
    tags = settings.get("tags") or {}
    if not isinstance(tags, dict):
        raise RuntimeError(f"{path} context.tags must be an object")
    settings["tags"] = {**tags, run_tag_key: run_id}
    settings.update(context)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"path": str(path), "sha256": sha256_file(path), "keys": sorted(context)}


def read_cloud_assembly(clone: Path) -> dict[str, dict[str, Any]]:
    """Stack artifacts of the clone's synthesized cloud assembly, by stack name."""
    manifest = clone / "cdk.out" / "manifest.json"
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read the base cloud assembly {manifest}: {exc}") from exc
    artifacts = document.get("artifacts") if isinstance(document, dict) else None
    if not isinstance(artifacts, dict):
        raise RuntimeError(f"{manifest} lists no artifacts")
    stacks: dict[str, dict[str, Any]] = {}
    for artifact_id, artifact in artifacts.items():
        if not isinstance(artifact, dict) or artifact.get("type") != "aws:cloudformation:stack":
            continue
        properties = artifact.get("properties") or {}
        name = str(properties.get("stackName") or artifact_id)
        tags = properties.get("tags")
        if not isinstance(tags, dict):
            tags = {
                str(tag.get("Key")): str(tag.get("Value"))
                for entries in (artifact.get("metadata") or {}).values()
                for entry in entries
                if entry.get("type") == "aws:cdk:stack-tags"
                for tag in entry.get("data") or []
            }
        stacks[name] = {
            "environment": str(artifact.get("environment") or ""),
            "tags": {str(key): str(value) for key, value in tags.items()},
        }
    return stacks


def command_environment(
    workspace: Workspace | None,
    *,
    identity: Mapping[str, str] | None = None,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment every workspace command runs with.

    ``workspace`` puts the base venv first on ``PATH`` (the CDK app runs
    ``python3``) and gives the base ``gco`` a cache of its own; ``identity``
    pins the ``GCO_*`` project and Region settings to the harness's, so a
    user config file cannot point the base ``gco`` at another deployment.
    """
    environment = {
        key: value
        for key, value in (os.environ if base is None else base).items()
        if key not in _SCRUBBED_ENVIRONMENT
    }
    if workspace is not None:
        bin_dir = workspace.venv / "bin"
        environment["PATH"] = os.pathsep.join(
            part for part in (str(bin_dir), environment.get("PATH", "")) if part
        )
        environment["VIRTUAL_ENV"] = str(workspace.venv)
        environment["GCO_CACHE_DIR"] = str(workspace.cache)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment.update(identity or {})
    return environment


@dataclass(frozen=True)
class CommandResult:
    """What one workspace command did."""

    argv: tuple[str, ...]
    exit_code: int
    duration_seconds: float
    timed_out: bool
    log_path: Path
    stdout: str
    tail: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "timed_out": self.timed_out,
            "log": str(self.log_path),
            "tail": list(self.tail),
        }


def _terminate(process: subprocess.Popen[str], *, grace_seconds: float) -> None:
    """Stop the command's whole process group: SIGTERM, then SIGKILL after the grace."""
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            continue


def running_command(pid: int) -> str | None:
    """The command line of the process with this ID, or ``None`` when there is none."""
    result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - fixed ps argv, no shell
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    command = result.stdout.strip()
    return command if result.returncode == 0 and command else None


def run_logged(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
    timeout_seconds: float,
    echo: Echo | None = None,
    on_start: Callable[[int], None] | None = None,
    grace_seconds: float = TERMINATE_GRACE_SECONDS,
) -> CommandResult:
    """Run one command, streaming stdout and stderr to ``log_path`` and ``echo``.

    The command gets its own session, so a timeout, or the harness being
    interrupted, stops it and every CDK process it started instead of
    leaving them running. Standard output is also kept whole: ``gco -o json``
    writes its result document there last. ``on_start`` receives the process
    ID as soon as the command starts.
    """
    tail: deque[str] = deque(maxlen=LOG_TAIL_LINES)
    stdout_lines: list[str] = []
    lock = threading.Lock()
    started = time.monotonic()
    timed_out = False
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(argv)}\n")
        log.flush()
        # nosemgrep: dangerous-subprocess-use-audit - fixed argv lists, never a shell
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
            umask=COMMAND_UMASK,
        )

        def pump(stream: IO[str], keep: bool) -> None:
            for line in stream:
                with lock:
                    log.write(line)
                    log.flush()
                    tail.append(line.rstrip("\n"))
                    if keep:
                        stdout_lines.append(line)
                if echo is not None:
                    echo(line)

        assert process.stdout is not None and process.stderr is not None
        if on_start is not None:
            on_start(process.pid)
        pumps = [
            threading.Thread(target=pump, args=(process.stdout, True), daemon=True),
            threading.Thread(target=pump, args=(process.stderr, False), daemon=True),
        ]
        for thread in pumps:
            thread.start()
        try:
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate(process, grace_seconds=grace_seconds)
        except BaseException:
            _terminate(process, grace_seconds=grace_seconds)
            raise
        finally:
            for thread in pumps:
                thread.join()
    return CommandResult(
        argv=tuple(argv),
        exit_code=process.returncode,
        duration_seconds=round(time.monotonic() - started, 3),
        timed_out=timed_out,
        log_path=log_path,
        stdout="".join(stdout_lines),
        tail=tuple(tail),
    )


def console_echo(prefix: str) -> Echo:
    """Mirror a command's output on the harness console, one prefixed line at a time."""

    def echo(line: str) -> None:
        text = line.rstrip("\n")
        sys.stdout.write(f"[{prefix}] {text}\n")
        sys.stdout.flush()

    return echo


_DOCUMENT_START = re.compile(r"(?m)^\{")


def final_json_document(text: str) -> dict[str, Any] | None:
    """The JSON object ``gco -o json`` writes last, after any streamed CDK output."""
    for match in reversed(list(_DOCUMENT_START.finditer(text))):
        try:
            document = json.loads(text[match.start() :])
        except ValueError:
            continue
        return document if isinstance(document, dict) else None
    return None


def gco_result_document(text: str) -> dict[str, Any] | None:
    """A ``gco -o json`` command's own result document.

    When the command also printed plain text, ``gco`` wraps everything in
    ``{"status": "ok", "output": "<text>"}``; the command's document is then
    the last one inside that text.
    """
    document = final_json_document(text)
    while (
        document is not None
        and set(document) == {"status", "output"}
        and isinstance(document["output"], str)
    ):
        inner = final_json_document(document["output"])
        if inner is None:
            break
        document = inner
    return document
