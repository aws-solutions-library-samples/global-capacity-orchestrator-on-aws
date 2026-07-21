"""Serializable run, checkpoint, action, and report models."""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
import time
import traceback as traceback_module
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal, TextIO, cast

SCHEMA_VERSION = 2
ActionStatus = Literal["passed", "failed", "skipped"]


def utc_now() -> str:
    """Return an RFC 3339-compatible UTC timestamp."""
    return datetime.now(UTC).isoformat()


def to_jsonable(value: Any) -> Any:
    """Convert report values to stable JSON-compatible primitives."""
    if is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(asdict(value))
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(to_jsonable(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_REPORT_FILENAMES = frozenset({"live-release-validation.json", "live-release-validation.md"})


def _validate_private_regular_metadata(metadata: os.stat_result, path: Path) -> None:
    """Reject links, special files, foreign owners, and non-private POSIX modes."""
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Live-validation output must be a regular file, not {path}")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PermissionError(f"Live-validation output is not owned by this user: {path}")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != _PRIVATE_FILE_MODE:
        raise PermissionError(f"Live-validation output must have mode 0600: {path}")


def _validate_private_regular_file(path: Path) -> None:
    _validate_private_regular_metadata(path.lstat(), path)


def _validate_private_directory_metadata(metadata: os.stat_result, directory: Path) -> None:
    """Require a real, current-user-owned, owner-only run directory."""
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"Live-validation output directory must be real: {directory}")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PermissionError(
            f"Live-validation output directory is not owned by this user: {directory}"
        )
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != _PRIVATE_DIRECTORY_MODE:
        raise PermissionError(
            f"Live-validation output directory must already have mode 0700: {directory}"
        )


def ensure_private_directory(directory: Path) -> None:
    """Create a private directory or validate it without changing existing permissions."""
    directory = Path(directory)
    with suppress(FileExistsError):
        directory.mkdir(parents=True, mode=_PRIVATE_DIRECTORY_MODE, exist_ok=False)
    _validate_private_directory_metadata(directory.lstat(), directory)


def _assert_directory_binding(directory: Path, descriptor: int) -> None:
    """Fail if the verified pathname no longer names the pinned directory."""
    try:
        current = directory.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"Live-validation output directory was rebound while open: {directory}"
        ) from exc
    if not os.path.samestat(current, os.fstat(descriptor)):
        raise RuntimeError(f"Live-validation output directory was rebound while open: {directory}")


@contextmanager
def _open_private_directory(directory: Path) -> Iterator[int | None]:
    """Pin a validated directory so artifact I/O cannot follow a rebound pathname."""
    directory = Path(directory)
    ensure_private_directory(directory)
    if os.name == "nt":
        # The supported live-validation platforms are macOS and Linux. Keep
        # offline model/report use functional on Windows with the validated
        # path fallback below, where POSIX directory descriptors are absent.
        yield None
        return

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_only = getattr(os, "O_DIRECTORY", 0)
    if not no_follow or not directory_only:
        raise RuntimeError("Secure directory-descriptor operations are unavailable")

    before = directory.lstat()
    descriptor = os.open(
        directory,
        os.O_RDONLY | no_follow | directory_only | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if not os.path.samestat(before, opened):
            raise RuntimeError(
                f"Live-validation output directory changed while opening: {directory}"
            )
        _validate_private_directory_metadata(opened, directory)
        yield descriptor
        _assert_directory_binding(directory, descriptor)
    finally:
        os.close(descriptor)


def ensure_private_run_directory(directory: Path, checkpoint_path: Path) -> None:
    """Validate that an existing private directory is dedicated to one harness run."""
    directory = Path(directory)
    allowed_names = {*_REPORT_FILENAMES, checkpoint_path.name}
    with _open_private_directory(directory) as descriptor:
        if descriptor is None:
            entries = [(entry.name, entry.lstat()) for entry in directory.iterdir()]
        else:
            entries = [
                (
                    name,
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False),
                )
                for name in os.listdir(descriptor)
            ]
        for name, metadata in entries:
            is_temporary = any(
                name.startswith(f".{allowed_name}.") and name.endswith(".tmp")
                for allowed_name in allowed_names
            )
            entry = directory / name
            if name not in allowed_names and not is_temporary:
                raise ValueError(
                    "Live-validation output directory contains an unrelated entry and is not "
                    f"dedicated to this run: {entry}"
                )
            _validate_private_regular_metadata(metadata, entry)


def _read_private_text(path: Path) -> str:
    """Read one owner-only regular file relative to a pinned directory."""
    with _open_private_directory(path.parent) as descriptor:
        if descriptor is None:
            _validate_private_regular_file(path)
            return path.read_text(encoding="utf-8")

        opened_descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=descriptor,
        )
        descriptor_to_close: int | None = opened_descriptor
        try:
            _validate_private_regular_metadata(os.fstat(opened_descriptor), path)
            text_handle: TextIO = os.fdopen(opened_descriptor, mode="r", encoding="utf-8")
            descriptor_to_close = None
            with text_handle:
                return text_handle.read()
        finally:
            if descriptor_to_close is not None:
                os.close(descriptor_to_close)


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically persist owner-only text relative to a pinned private directory."""
    with _open_private_directory(path.parent) as descriptor:
        if descriptor is None:
            temporary_path_to_unlink: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as named_handle:
                    temporary_path = Path(named_handle.name)
                    temporary_path_to_unlink = temporary_path
                    named_handle.write(content)
                    named_handle.flush()
                    os.fsync(named_handle.fileno())
                os.replace(temporary_path, path)
                temporary_path_to_unlink = None
            finally:
                if temporary_path_to_unlink is not None:
                    temporary_path_to_unlink.unlink(missing_ok=True)
            return

        temporary_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
        temporary_name_to_unlink: str | None = temporary_name
        descriptor_to_close: int | None = None
        try:
            opened_descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                _PRIVATE_FILE_MODE,
                dir_fd=descriptor,
            )
            descriptor_to_close = opened_descriptor
            os.fchmod(opened_descriptor, _PRIVATE_FILE_MODE)
            text_handle: TextIO = os.fdopen(opened_descriptor, mode="w", encoding="utf-8")
            descriptor_to_close = None
            with text_handle:
                text_handle.write(content)
                text_handle.flush()
                os.fsync(text_handle.fileno())

            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            temporary_name_to_unlink = None
        finally:
            if descriptor_to_close is not None:
                os.close(descriptor_to_close)
            if temporary_name_to_unlink is not None:
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name_to_unlink, dir_fd=descriptor)


def atomic_write_json(path: Path, value: Any) -> None:
    """Atomically persist owner-only JSON inside a validated private directory."""
    content = json.dumps(to_jsonable(value), indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, content)


@dataclass(frozen=True)
class RunSettings:
    """Immutable operator inputs for one live validation run."""

    run_id: str
    repo_root: Path
    report_dir: Path
    checkpoint_path: Path
    expected_account: str
    expected_sha: str
    expected_branch: str
    profile: str
    requested_actions: tuple[str, ...]
    protected_stack_names: tuple[str, ...] = ("CDKToolkit", "GCOGitHubOIDCStack")
    max_workers: int = 4
    job_timeout_seconds: int = 1800
    queue_timeout_seconds: int = 900
    poll_interval_seconds: int = 10
    destroy_attempts: int = 3
    destroy_retry_delay_seconds: int = 30
    confirm_kms_key_deletion: bool = False
    resume: bool = False

    def __post_init__(self) -> None:
        """Normalize output paths without resolving symlinks and enforce one run directory."""
        report_dir = Path(os.path.abspath(os.fspath(self.report_dir)))
        checkpoint_path = Path(os.path.abspath(os.fspath(self.checkpoint_path)))
        object.__setattr__(self, "report_dir", report_dir)
        object.__setattr__(self, "checkpoint_path", checkpoint_path)
        if checkpoint_path.parent != report_dir:
            raise ValueError("Checkpoint must be a direct child of the report directory")
        if checkpoint_path.name in _REPORT_FILENAMES:
            raise ValueError(
                f"Checkpoint filename is reserved for a validation report: {checkpoint_path.name}"
            )

    def identity(self) -> dict[str, Any]:
        """Return fields that must remain identical across resume attempts."""
        return {
            "run_id": self.run_id,
            "repo_root": str(self.repo_root.resolve()),
            "expected_account": self.expected_account,
            "expected_sha": self.expected_sha,
            "expected_branch": self.expected_branch,
            "profile": self.profile,
            "requested_actions": list(self.requested_actions),
            "protected_stack_names": list(self.protected_stack_names),
            "confirm_kms_key_deletion": self.confirm_kms_key_deletion,
        }


@dataclass
class ActionResult:
    """One action's durable report entry."""

    name: str
    description: str
    status: ActionStatus
    started_at: str
    ended_at: str
    duration_seconds: float
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    traceback: str | None = None

    @classmethod
    def passed(
        cls,
        *,
        name: str,
        description: str,
        started_at: str,
        started_monotonic: float,
        ended_monotonic: float,
        details: dict[str, Any] | None = None,
    ) -> ActionResult:
        return cls(
            name=name,
            description=description,
            status="passed",
            started_at=started_at,
            ended_at=utc_now(),
            duration_seconds=round(ended_monotonic - started_monotonic, 3),
            details=details or {},
        )

    @classmethod
    def failed(
        cls,
        *,
        name: str,
        description: str,
        started_at: str,
        started_monotonic: float,
        ended_monotonic: float,
        error: BaseException,
        details: dict[str, Any] | None = None,
    ) -> ActionResult:
        return cls(
            name=name,
            description=description,
            status="failed",
            started_at=started_at,
            ended_at=utc_now(),
            duration_seconds=round(ended_monotonic - started_monotonic, 3),
            details=details or {},
            error=f"{type(error).__name__}: {error}",
            traceback="".join(
                traceback_module.format_exception(type(error), error, error.__traceback__)
            ),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ActionResult:
        return cls(
            name=str(value["name"]),
            description=str(value.get("description", "")),
            status=value["status"],
            started_at=str(value.get("started_at", "")),
            ended_at=str(value.get("ended_at", "")),
            duration_seconds=float(value.get("duration_seconds", 0.0)),
            details=dict(value.get("details") or {}),
            error=value.get("error"),
            traceback=value.get("traceback"),
        )


@dataclass
class RunCheckpoint:
    """Crash-safe state used to resume and prove resource ownership."""

    identity: dict[str, Any]
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    completed_actions: list[str] = field(default_factory=list)
    action_results: dict[str, ActionResult] = field(default_factory=dict)
    deployment_attempted: bool = False
    destroyed: bool = False
    baseline: dict[str, Any] | None = None
    state: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        self.updated_at = utc_now()
        serialized = to_jsonable(self)
        if not isinstance(serialized, dict):
            raise TypeError("RunCheckpoint did not serialize to an object")
        return serialized

    @classmethod
    def from_path(cls, path: Path) -> RunCheckpoint:
        try:
            raw = json.loads(_read_private_text(path))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Unable to read checkpoint {path}: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Checkpoint {path} does not use supported schema {SCHEMA_VERSION}")
        results_raw = raw.get("action_results") or {}
        if not isinstance(results_raw, dict):
            raise ValueError(f"Checkpoint {path} has invalid action_results")
        return cls(
            identity=dict(raw.get("identity") or {}),
            created_at=str(raw.get("created_at") or utc_now()),
            updated_at=str(raw.get("updated_at") or utc_now()),
            completed_actions=[str(item) for item in raw.get("completed_actions") or []],
            action_results={
                str(name): ActionResult.from_dict(value)
                for name, value in results_raw.items()
                if isinstance(value, dict)
            },
            deployment_attempted=bool(raw.get("deployment_attempted", False)),
            destroyed=bool(raw.get("destroyed", False)),
            baseline=dict(raw["baseline"]) if isinstance(raw.get("baseline"), dict) else None,
            state=dict(raw.get("state") or {}),
            schema_version=SCHEMA_VERSION,
        )


@dataclass
class ValidationReport:
    """Attachable JSON/Markdown summary for one run."""

    run_id: str
    identity: dict[str, Any]
    selected_actions: list[str]
    started_at: str
    ended_at: str | None = None
    status: Literal["running", "passed", "partial", "failed", "interrupted"] = "running"
    action_results: list[ActionResult] = field(default_factory=list)
    cleanup: dict[str, Any] = field(default_factory=dict)
    baseline: dict[str, Any] | None = None
    final_inventory: dict[str, Any] | None = None
    fatal_error: str | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        serialized = to_jsonable(self)
        if not isinstance(serialized, dict):
            raise TypeError("ValidationReport did not serialize to an object")
        return serialized

    def write(self, directory: Path) -> tuple[Path, Path]:
        """Write both attachable formats and return their paths."""
        ensure_private_directory(directory)
        json_path = directory / "live-release-validation.json"
        markdown_path = directory / "live-release-validation.md"
        atomic_write_json(json_path, self.to_dict())
        atomic_write_text(markdown_path, self.to_markdown())
        return json_path, markdown_path

    def to_markdown(self) -> str:
        """Render a compact human-reviewable report."""
        identity = self.identity
        selected_scope = ", ".join(f"`{name}`" for name in self.selected_actions) or "_none_"
        lines = [
            "# GCO Live Release Validation",
            "",
            f"- **Run:** `{self.run_id}`",
            f"- **Status:** **{self.status.upper()}**",
            f"- **Account:** `{identity.get('expected_account', 'unknown')}`",
            f"- **Commit:** `{identity.get('expected_sha', 'unknown')}`",
            f"- **Branch:** `{identity.get('expected_branch', 'unknown')}`",
            f"- **Profile:** `{identity.get('profile', 'unknown')}`",
            f"- **Selected action scope:** {selected_scope}",
            f"- **Started:** `{self.started_at}`",
            f"- **Ended:** `{self.ended_at or 'in progress'}`",
            "",
            "## Actions",
            "",
            "| Action | Status | Duration | Error |",
            "|---|---:|---:|---|",
        ]
        for result in self.action_results:
            error = (result.error or "").replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| `{result.name}` | {result.status} | {result.duration_seconds:.3f}s | {error} |"
            )
        if not self.action_results:
            lines.append("| _none_ | skipped | 0s | |")

        lines.extend(["", "## Cleanup", "", "```json"])
        lines.append(json.dumps(to_jsonable(self.cleanup), indent=2, sort_keys=True))
        lines.append("```")

        final_summary = (self.final_inventory or {}).get("summary", {})
        lines.extend(["", "## Final inventory", "", "```json"])
        lines.append(json.dumps(to_jsonable(final_summary), indent=2, sort_keys=True))
        lines.append("```")

        failures = [result for result in self.action_results if result.status == "failed"]
        if self.fatal_error or failures:
            lines.extend(["", "## Failures", ""])
            if self.fatal_error:
                lines.extend(["```text", self.fatal_error, "```", ""])
            for result in failures:
                lines.append(f"### `{result.name}`")
                lines.extend(
                    ["", "```text", result.traceback or result.error or "unknown", "```", ""]
                )

        lines.append("")
        return "\n".join(lines)


@dataclass
class RunContext:
    """Mutable dependencies and durable state shared by action handlers."""

    settings: RunSettings
    checkpoint: RunCheckpoint
    report: ValidationReport
    cdk_context: dict[str, Any]
    deployment_regions: tuple[str, ...]
    config: Any
    session: Any
    stack_manager: Any
    aws_client: Any
    job_manager: Any
    persist_callback: Callable[[RunCheckpoint], None]
    state_lock: RLock = field(default_factory=RLock, repr=False)

    def persist(self) -> None:
        with self.state_lock:
            self.persist_callback(self.checkpoint)

    def register_job(
        self,
        *,
        name: str,
        namespace: str,
        region: str,
        path: str,
        run_label: str,
        transport_region: str | None,
    ) -> dict[str, Any]:
        """Checkpoint one deterministic Job before submitting it.

        The record is not destructive authority until an exact Kubernetes UID
        has been observed together with the expected run/path labels.
        """
        with self.state_lock:
            raw_jobs = self.checkpoint.state.setdefault("jobs", [])
            if not isinstance(raw_jobs, list) or any(
                not isinstance(item, dict) for item in raw_jobs
            ):
                raise RuntimeError("Checkpoint jobs must be a list of objects")
            jobs = cast(list[dict[str, Any]], raw_jobs)
            matches = [
                item
                for item in jobs
                if item.get("name") == name
                and item.get("namespace") == namespace
                and item.get("region") == region
                and item.get("path") == path
            ]
            if len(matches) > 1:
                raise RuntimeError(
                    f"Checkpoint contains duplicate Job records for {region}:{namespace}/{name}"
                )
            expected = {
                "name": name,
                "namespace": namespace,
                "region": region,
                "path": path,
                "run_label": run_label,
                "transport_region": transport_region,
            }
            if matches:
                record = matches[0]
                for key, value in expected.items():
                    if record.get(key) != value:
                        raise RuntimeError(
                            f"Checkpoint Job identity changed for {region}:{namespace}/{name}: {key}"
                        )
                record.setdefault(
                    "submission_state",
                    "appeared" if record.get("uid") else "registered",
                )
            else:
                record = {
                    **expected,
                    "uid": None,
                    "deleted": False,
                    "submission_state": "registered",
                }
                jobs.append(record)
            self.persist_callback(self.checkpoint)
            return record

    def prepare_job_submission(
        self,
        record: dict[str, Any],
        *,
        envelope: dict[str, Any],
        resumable: bool,
    ) -> None:
        """Persist one immutable canonical envelope before any submission attempt."""
        canonical = to_jsonable(envelope)
        if not isinstance(canonical, dict):
            raise RuntimeError("A Job submission envelope must be a JSON object")
        with self.state_lock:
            state = str(record.get("submission_state") or "registered")
            previous = record.get("submission_envelope")
            if previous is not None and previous != canonical:
                raise RuntimeError("Checkpointed Job submission envelope changed")
            previous_resumable = record.get("submission_resumable")
            if previous_resumable is not None and bool(previous_resumable) != resumable:
                raise RuntimeError("Checkpointed Job resumability contract changed")
            if state == "registered":
                record["submission_state"] = "prepared"
            elif state not in {
                "prepared",
                "submitting",
                "submitted",
                "appeared",
                "deleted",
                "blocked",
                "not_submitted",
            }:
                raise RuntimeError(f"Cannot prepare Job submission from state {state!r}")
            record["submission_envelope"] = canonical
            record["submission_resumable"] = resumable
            self.persist_callback(self.checkpoint)

    def begin_job_submission(
        self,
        record: dict[str, Any],
        *,
        reconciliation_timeout_seconds: int,
    ) -> None:
        """Persist the ambiguous check/use boundary immediately before submission."""
        with self.state_lock:
            state = str(record.get("submission_state") or "registered")
            resumable = bool(record.get("submission_resumable", False))
            if state != "prepared" and not (state == "submitting" and resumable):
                raise RuntimeError(f"Cannot begin Job submission from state {state!r}")
            if not isinstance(record.get("submission_envelope"), dict):
                raise RuntimeError("Cannot submit a Job without a checkpointed envelope")
            now = time.time()
            record["submission_state"] = "submitting"
            record["submission_started_at"] = now
            record["submission_reconcile_deadline"] = now + reconciliation_timeout_seconds
            record["submission_attempts"] = int(record.get("submission_attempts") or 0) + 1
            self.persist_callback(self.checkpoint)

    def finish_job_submission(
        self,
        record: dict[str, Any],
        submission: dict[str, Any],
        *,
        appearance_timeout_seconds: int,
    ) -> None:
        """Persist an acknowledgement and begin a fresh bounded appearance window."""
        with self.state_lock:
            state = str(record.get("submission_state") or "")
            if state not in {"submitting", "submitted", "appeared"}:
                raise RuntimeError(f"Cannot finish Job submission from state {state!r}")
            acknowledged_at = time.time()
            if state != "appeared":
                record["submission_state"] = "submitted"
            record["submission"] = to_jsonable(submission)
            record["submission_acknowledged_at"] = acknowledged_at
            record["appearance_deadline"] = acknowledged_at + appearance_timeout_seconds
            self.persist_callback(self.checkpoint)

    def block_job_submission(self, record: dict[str, Any], reason: str) -> None:
        """Fail closed when a non-idempotent submission cannot be reconciled."""
        with self.state_lock:
            record["submission_state"] = "blocked"
            record["submission_blocked_reason"] = reason
            record["submission_blocked_at"] = time.time()
            self.persist_callback(self.checkpoint)

    def mark_job_not_submitted(self, record: dict[str, Any]) -> None:
        """Record authoritative absence only before a side effect could escape."""
        with self.state_lock:
            state = str(record.get("submission_state") or "registered")
            if state not in {"registered", "prepared", "not_submitted"}:
                raise RuntimeError(f"Cannot mark Job not submitted from state {state!r}")
            record["submission_state"] = "not_submitted"
            record["not_submitted_at"] = time.time()
            self.persist_callback(self.checkpoint)

    def mark_central_job_cancelled_before_claim(
        self,
        record: dict[str, Any],
        *,
        job_id: str,
    ) -> None:
        """Record terminal queue proof that a central Job never reached a worker."""
        if not job_id:
            raise RuntimeError("Central cancellation proof requires a queue Job ID")
        with self.state_lock:
            state = str(record.get("submission_state") or "registered")
            previous_job_id = record.get("central_cancelled_before_claim_job_id")
            if state == "not_submitted" and previous_job_id == job_id:
                return
            if state not in {"submitting", "submitted"}:
                raise RuntimeError(f"Cannot apply central cancellation proof from state {state!r}")
            if record.get("path") != "dynamodb" or record.get("uid"):
                raise RuntimeError(
                    "Central cancellation proof cannot replace immutable Kubernetes UID evidence"
                )
            record["submission_state"] = "not_submitted"
            record["central_cancelled_before_claim_job_id"] = job_id
            record["central_cancelled_before_claim_at"] = time.time()
            self.persist_callback(self.checkpoint)

    def mark_central_job_not_created_by_worker(
        self,
        record: dict[str, Any],
        *,
        job_id: str,
    ) -> None:
        """Record explicit worker proof that Kubernetes mutation never began."""
        if not job_id:
            raise RuntimeError("Central worker no-workload proof requires a queue Job ID")
        with self.state_lock:
            state = str(record.get("submission_state") or "registered")
            previous_job_id = record.get("central_worker_not_created_job_id")
            if state == "not_submitted" and previous_job_id == job_id:
                return
            if state not in {"submitting", "submitted"}:
                raise RuntimeError(
                    f"Cannot apply central worker no-workload proof from state {state!r}"
                )
            central_identity = (
                record.get("k8s_job_name"),
                record.get("k8s_job_namespace"),
                record.get("k8s_job_uid"),
            )
            if (
                record.get("path") != "dynamodb"
                or record.get("uid") is not None
                or any(value is not None for value in central_identity)
            ):
                raise RuntimeError(
                    "Central worker no-workload proof cannot replace Kubernetes identity evidence"
                )
            if record.get("central_cancelled_before_claim_job_id") is not None:
                raise RuntimeError(
                    "Central worker no-workload proof conflicts with cancellation proof"
                )
            record["submission_state"] = "not_submitted"
            record["central_worker_not_created_job_id"] = job_id
            record["central_worker_not_created_at"] = time.time()
            self.persist_callback(self.checkpoint)

    def bind_central_job_identity(
        self,
        record: dict[str, Any],
        *,
        job_id: str,
        name: str,
        namespace: str,
        uid: str,
        appearance_timeout_seconds: int,
    ) -> bool:
        """Bind a requested central-queue record to its immutable Kubernetes Job.

        The requested name and namespace remain canonical submission/replay
        identity. The worker-persisted identity is a separate destructive
        authority and starts one fresh workload-appearance window when first
        observed.
        """
        if record.get("path") != "dynamodb":
            raise RuntimeError("Central Kubernetes identity requires a DynamoDB workload record")
        if not all((job_id, name, namespace, uid)):
            raise RuntimeError("Central Kubernetes identity fields must all be non-empty")
        if appearance_timeout_seconds <= 0:
            raise RuntimeError("Central workload appearance timeout must be positive")

        immutable = {
            "central_queue_job_id": job_id,
            "k8s_job_name": name,
            "k8s_job_namespace": namespace,
            "k8s_job_uid": uid,
        }
        with self.state_lock:
            present = {key: record.get(key) for key in immutable}
            populated = [value is not None for value in present.values()]
            if any(populated):
                if not all(populated):
                    raise RuntimeError("Checkpoint contains a partial central Kubernetes identity")
                for key, value in immutable.items():
                    if present[key] != value:
                        raise RuntimeError(
                            f"Central Kubernetes identity changed for {job_id}: {key}"
                        )
                if record.get("uid") != uid:
                    raise RuntimeError(
                        "Central Kubernetes UID disagrees with checkpoint ownership authority"
                    )
                return False

            previous_uid = record.get("uid")
            if previous_uid is not None and previous_uid != uid:
                raise RuntimeError(
                    f"Kubernetes Job UID changed from {previous_uid!r} to {uid!r}; "
                    "refusing central ownership"
                )
            bound_at = time.time()
            was_deleted = bool(record.get("deleted"))
            record.update(immutable)
            record["uid"] = uid
            record["central_identity_bound_at"] = bound_at
            record["appearance_deadline"] = bound_at + appearance_timeout_seconds
            if was_deleted:
                record["requested_identity_deletion_superseded_at"] = bound_at
                record.pop("deleted_at", None)
            record["deleted"] = False
            record["submission_state"] = "appeared"
            self.persist_callback(self.checkpoint)
            return True

    def record_job_uid(self, record: dict[str, Any], uid: str) -> None:
        """Bind a pending Job record to one immutable Kubernetes UID."""
        if not uid:
            raise RuntimeError("Cannot checkpoint an empty Kubernetes Job UID")
        with self.state_lock:
            persisted_central_uid = record.get("k8s_job_uid")
            if persisted_central_uid is not None and persisted_central_uid != uid:
                raise RuntimeError(
                    "Observed Kubernetes Job UID differs from persisted central worker identity"
                )
            previous = record.get("uid")
            if previous == uid:
                return
            if previous is not None:
                raise RuntimeError(
                    f"Kubernetes Job UID changed from {previous!r} to {uid!r}; refusing ownership"
                )
            record["uid"] = uid
            record["submission_state"] = "appeared"
            self.persist_callback(self.checkpoint)

    def mark_job_deleted(self, record: dict[str, Any]) -> None:
        with self.state_lock:
            record["deleted"] = True
            record["submission_state"] = "deleted"
            record["deleted_at"] = time.time()
            self.persist_callback(self.checkpoint)
