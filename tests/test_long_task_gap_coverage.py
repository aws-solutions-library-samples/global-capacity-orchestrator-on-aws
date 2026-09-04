"""Deterministic branch coverage for ``tools._long_task``.

These tests isolate stream framing, notification failures, subprocess cleanup,
and runner lifecycle edges with in-memory streams and fake processes. They never
spawn an operating-system process or wait on wall-clock grace periods.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gco_mcp"))

from tools import _long_task as long_task  # noqa: E402


class _ChunkStream:
    """Small ``StreamReader`` stand-in returning predetermined byte chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _Progress:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.increments = 0
        self.totals: list[int] = []

    async def set_message(self, message: str) -> None:
        self.messages.append(message)

    async def increment(self) -> None:
        self.increments += 1

    async def set_total(self, total: int) -> None:
        self.totals.append(total)


class _Context:
    def __init__(self) -> None:
        self.infos: list[str] = []

    async def info(self, message: str) -> None:
        self.infos.append(message)


class _WriterSpy:
    """In-memory replacement that exposes every status-writer interaction."""

    latest: _WriterSpy | None = None

    def __init__(
        self,
        task_id: str,
        tool: str,
        argv: list[str],
        *,
        pid: int | None,
        total_units: int | None = None,
    ) -> None:
        type(self).latest = self
        self.task_id = task_id
        self.tool = tool
        self.argv = argv
        self.pid = pid
        self.total_units = total_units
        self.lines: list[tuple[str, str]] = []
        self.incremented: list[str] = []
        self.last_stacks: list[str] = []
        self.finishes: list[dict[str, object]] = []

    def set_pid(self, pid: int | None) -> None:
        self.pid = pid

    def record_line(self, line: str, *, stream: str) -> None:
        self.lines.append((stream, line))

    def increment_stacks(self, stack_name: str) -> None:
        self.incremented.append(stack_name)

    def set_last_stack(self, stack_name: str) -> None:
        self.last_stacks.append(stack_name)

    def finish(
        self,
        *,
        state: str,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> None:
        self.finishes.append({"state": state, "exit_code": exit_code, "error": error})


@pytest.fixture(autouse=True)
def _reset_writer_spy_state():
    """Prevent a failed constructor from exposing a prior test's writer."""
    _WriterSpy.latest = None
    yield
    _WriterSpy.latest = None


class _FakeProcess:
    """Async-process stand-in with controllable wait and signal behavior."""

    def __init__(
        self,
        *,
        stdout: _ChunkStream | None,
        stderr: _ChunkStream | None,
        exit_code: int,
        pid: int = 4242,
        terminate_error: OSError | None = None,
        kill_error: OSError | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.pid = pid
        self.returncode: int | None = None
        self.exit_code = exit_code
        self.terminate_error = terminate_error
        self.kill_error = kill_error
        self.wait_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0

    async def wait(self) -> int:
        self.wait_calls += 1
        self.returncode = self.exit_code
        return self.exit_code

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_error is not None:
            raise self.terminate_error

    def kill(self) -> None:
        self.kill_calls += 1
        if self.kill_error is not None:
            raise self.kill_error


class TestArgumentAndTextGuards:
    def test_traversal_skips_flags_and_detects_backslash_segment(self) -> None:
        assert long_task._argv_has_traversal(["--cache=../outside", "safe"]) is None
        value = "root\\..\\" + "x" * 150

        assert long_task._argv_has_traversal(["gco", "--flag", value]) == (2, value[:100])

    def test_bounded_text_handles_normal_tiny_and_split_utf8_budgets(self) -> None:
        assert long_task._bounded_text("short", 10) == "short"
        assert long_task._bounded_text("abcdef", 3) == "..."
        bounded = long_task._bounded_text("é" * 20, 15)
        assert bounded == long_task._TRUNCATED_TEXT
        assert len(bounded.encode("utf-8")) <= 15


class TestBoundedStreamFraming:
    async def test_stream_handles_crlf_empty_reset_invalid_utf8_and_final_partial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(long_task, "_STREAM_LINE_MAX_BYTES", 20)
        stream = _ChunkStream(
            [
                b"one\r\n\n",
                b"abcdefghi",
                b"ignored\nnext\n",
                b"\xff\n",
                b"tail\r",
            ]
        )

        lines = [line async for line in long_task._bounded_stream_lines(stream)]

        assert lines == [
            "one",
            "",
            "abcdef...[truncated]",
            "next",
            "�",
            "tail",
        ]
        assert all(len(line.encode("utf-8")) <= 20 for line in lines)


class TestBestEffortNotifications:
    async def test_missing_method_is_ignored(self) -> None:
        await long_task._best_effort_client_call(object(), "not_supported", "message")

    async def test_cancellation_is_not_swallowed(self) -> None:
        class CancellingClient:
            async def notify(self) -> None:
                raise asyncio.CancelledError("caller cancelled")

        with pytest.raises(asyncio.CancelledError, match="caller cancelled"):
            await long_task._best_effort_client_call(CancellingClient(), "notify")


class TestTerminateAndReap:
    async def test_already_reaped_process_is_not_signalled(self) -> None:
        process = _FakeProcess(
            stdout=_ChunkStream([]),
            stderr=_ChunkStream([]),
            exit_code=0,
        )
        process.returncode = 0
        wait_task = asyncio.create_task(process.wait())

        await long_task._terminate_and_reap(process, wait_task)  # type: ignore[arg-type]

        assert process.terminate_calls == 0
        assert process.kill_calls == 0
        assert process.wait_calls == 1

    async def test_graceful_termination_returns_without_kill(self) -> None:
        process = _FakeProcess(
            stdout=_ChunkStream([]),
            stderr=_ChunkStream([]),
            exit_code=-15,
        )
        wait_task = asyncio.create_task(process.wait())

        await long_task._terminate_and_reap(process, wait_task)  # type: ignore[arg-type]

        assert process.terminate_calls == 1
        assert process.kill_calls == 0
        assert process.returncode == -15

    async def test_timeout_kills_and_replaces_cancelled_wait_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        process = _FakeProcess(
            stdout=_ChunkStream([]),
            stderr=_ChunkStream([]),
            exit_code=-9,
        )
        wait_task = asyncio.create_task(asyncio.sleep(60))
        wait_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wait_task

        async def force_timeout(_awaitable: object, timeout: float) -> None:
            raise TimeoutError

        monkeypatch.setattr(long_task.asyncio, "wait_for", force_timeout)

        await long_task._terminate_and_reap(process, wait_task)  # type: ignore[arg-type]

        assert process.terminate_calls == 1
        assert process.kill_calls == 1
        assert process.wait_calls == 1
        assert process.returncode == -9

    async def test_failed_wait_and_signal_errors_fall_back_to_final_wait(self) -> None:
        process = _FakeProcess(
            stdout=_ChunkStream([]),
            stderr=_ChunkStream([]),
            exit_code=0,
            terminate_error=OSError("terminate unavailable"),
            kill_error=OSError("kill unavailable"),
        )

        async def failed_wait() -> int:
            raise RuntimeError("wait task failed")

        wait_task = asyncio.create_task(failed_wait())
        await asyncio.sleep(0)

        await long_task._terminate_and_reap(process, wait_task)  # type: ignore[arg-type]

        assert process.terminate_calls == 1
        assert process.kill_calls == 1
        assert process.wait_calls == 1
        assert process.returncode == 0


class TestMockedRunnerLifecycle:
    async def test_two_component_gco_command_uses_fallback_id_and_skips_zero_total(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        process = _FakeProcess(
            stdout=_ChunkStream(
                [
                    b"\n",
                    "gco-stack: deploying\n✨  gco-stack\n✨  gco-stack\nordinary\n".encode(),
                ]
            ),
            stderr=_ChunkStream([b"warning\n"]),
            exit_code=0,
        )
        observed: dict[str, Any] = {}

        async def fake_spawn(*argv: str, **kwargs: object) -> _FakeProcess:
            observed["argv"] = argv
            observed["kwargs"] = kwargs
            return process

        monkeypatch.setattr(long_task, "TaskStatusWriter", _WriterSpy)
        monkeypatch.setattr(long_task, "_try_get_task_id", lambda _ctx: "../unsafe")
        monkeypatch.setattr(long_task, "make_task_id", lambda _tool: "generated-task")
        monkeypatch.setattr(long_task.cli_runner, "_gco_executable", lambda: "/fake/gco")
        monkeypatch.setattr(long_task.asyncio, "create_subprocess_exec", fake_spawn)
        progress = _Progress()
        ctx = _Context()

        result = await long_task._run_long_task(
            ["gco", "images"],
            ctx=ctx,
            progress=progress,
            is_stack_op=False,
            total_units=0,
        )

        payload = json.loads(result)
        writer = _WriterSpy.latest
        assert writer is not None
        assert payload == {
            "status": "ok",
            "task_id": "generated-task",
            "stacks_completed": 1,
            "duration_seconds": payload["duration_seconds"],
            "last_stack": "gco-stack",
        }
        assert observed["argv"] == ("/fake/gco", "images")
        assert observed["kwargs"] == {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": str(long_task.cli_runner.PROJECT_ROOT),
        }
        assert writer.tool == "images"
        assert writer.argv == ["gco", "images"]
        assert writer.pid == process.pid
        assert writer.incremented == ["gco-stack"]
        assert ("stdout", "") not in writer.lines
        assert writer.finishes == [{"state": "succeeded", "exit_code": 0, "error": None}]
        assert progress.increments == 1
        assert progress.totals == []
        assert ctx.infos == ["stderr: warning"]

    async def test_empty_argv_uses_task_name_then_records_spawn_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        generated_for: list[str] = []
        spawned: list[tuple[str, ...]] = []

        async def fake_spawn(*argv: str, **_kwargs: object) -> _FakeProcess:
            spawned.append(argv)
            raise TypeError("missing program")

        def make_id(tool: str) -> str:
            generated_for.append(tool)
            return "empty-task"

        monkeypatch.setattr(long_task, "TaskStatusWriter", _WriterSpy)
        monkeypatch.setattr(long_task, "_try_get_task_id", lambda _ctx: None)
        monkeypatch.setattr(long_task, "make_task_id", make_id)
        monkeypatch.setattr(long_task.asyncio, "create_subprocess_exec", fake_spawn)

        with pytest.raises(TypeError, match="missing program"):
            await long_task._run_long_task(
                [],
                ctx=_Context(),
                progress=_Progress(),
                is_stack_op=False,
            )

        assert generated_for == ["task"]
        assert spawned == [()]
        writer = _WriterSpy.latest
        assert writer is not None
        assert writer.tool == "task"
        assert writer.argv == []
        assert writer.finishes == [
            {"state": "failed", "exit_code": None, "error": "TypeError: missing program"}
        ]

    async def test_missing_stdout_pipe_fails_and_publishes_terminal_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        process = _FakeProcess(
            stdout=None,
            stderr=_ChunkStream([]),
            exit_code=0,
        )

        async def fake_spawn(*_argv: str, **_kwargs: object) -> _FakeProcess:
            return process

        monkeypatch.setattr(long_task, "TaskStatusWriter", _WriterSpy)
        monkeypatch.setattr(long_task, "_try_get_task_id", lambda _ctx: None)
        monkeypatch.setattr(long_task, "make_task_id", lambda _tool: "missing-pipe")
        monkeypatch.setattr(long_task.asyncio, "create_subprocess_exec", fake_spawn)

        with pytest.raises(RuntimeError, match="stdout pipe was not created"):
            await long_task._run_long_task(
                ["command"],
                ctx=_Context(),
                progress=_Progress(),
                is_stack_op=False,
            )

        writer = _WriterSpy.latest
        assert writer is not None
        assert writer.finishes == [
            {
                "state": "failed",
                "exit_code": 0,
                "error": "RuntimeError: stdout pipe was not created",
            }
        ]
        assert process.terminate_calls == 0
        assert process.wait_calls == 1

    async def test_set_pid_failure_creates_wait_task_then_terminates_and_reaps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class PidFailWriter(_WriterSpy):
            latest: PidFailWriter | None = None

            def set_pid(self, pid: int | None) -> None:
                super().set_pid(pid)
                raise RuntimeError("x" * 200)

        process = _FakeProcess(
            stdout=_ChunkStream([]),
            stderr=_ChunkStream([]),
            exit_code=-15,
        )

        async def fake_spawn(*_argv: str, **_kwargs: object) -> _FakeProcess:
            return process

        monkeypatch.setattr(long_task, "_DIAGNOSTIC_LINE_MAX_BYTES", 32)
        monkeypatch.setattr(long_task, "TaskStatusWriter", PidFailWriter)
        monkeypatch.setattr(long_task, "_try_get_task_id", lambda _ctx: None)
        monkeypatch.setattr(long_task, "make_task_id", lambda _tool: "pid-failed")
        monkeypatch.setattr(long_task.asyncio, "create_subprocess_exec", fake_spawn)

        with pytest.raises(RuntimeError, match="x{20}"):
            await long_task._run_long_task(
                ["command"],
                ctx=_Context(),
                progress=_Progress(),
                is_stack_op=False,
            )

        writer = PidFailWriter.latest
        assert writer is not None
        assert process.terminate_calls == 1
        assert process.kill_calls == 0
        assert process.wait_calls == 1
        assert process.returncode == -15
        assert len(writer.finishes) == 1
        finish = writer.finishes[0]
        assert finish["state"] == "failed"
        assert finish["exit_code"] == -15
        error = finish["error"]
        assert isinstance(error, str)
        assert error.endswith(long_task._TRUNCATED_TEXT)
        assert len(error.encode("utf-8")) <= 32

    async def test_cancellation_terminates_fake_process_without_wall_clock_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class BlockingProcess(_FakeProcess):
            def __init__(self) -> None:
                super().__init__(
                    stdout=_ChunkStream([]),
                    stderr=_ChunkStream([]),
                    exit_code=-15,
                )
                self.wait_started = asyncio.Event()
                self.release = asyncio.Event()

            async def wait(self) -> int:
                self.wait_calls += 1
                self.wait_started.set()
                await self.release.wait()
                self.returncode = self.exit_code
                return self.exit_code

            def terminate(self) -> None:
                super().terminate()
                self.release.set()

        process = BlockingProcess()

        async def fake_spawn(*_argv: str, **_kwargs: object) -> BlockingProcess:
            return process

        monkeypatch.setattr(long_task, "TaskStatusWriter", _WriterSpy)
        monkeypatch.setattr(long_task, "_try_get_task_id", lambda _ctx: None)
        monkeypatch.setattr(long_task, "make_task_id", lambda _tool: "cancelled-task")
        monkeypatch.setattr(long_task.asyncio, "create_subprocess_exec", fake_spawn)

        runner = asyncio.create_task(
            long_task._run_long_task(
                ["command"],
                ctx=_Context(),
                progress=_Progress(),
                is_stack_op=False,
            )
        )
        await process.wait_started.wait()
        runner.cancel()

        with pytest.raises(asyncio.CancelledError):
            await runner

        writer = _WriterSpy.latest
        assert writer is not None
        assert process.terminate_calls == 1
        assert process.kill_calls == 0
        assert process.returncode == -15
        assert writer.finishes == [{"state": "cancelled", "exit_code": -15, "error": "cancelled"}]


class TestResidualCleanupBranches:
    async def test_exact_payload_budget_truncates_when_more_bytes_arrive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A line that fills the payload budget still marks later discarded bytes."""
        monkeypatch.setattr(
            long_task,
            "_STREAM_LINE_MAX_BYTES",
            len(long_task._TRUNCATED_TEXT.encode()) + 4,
        )
        stream = _ChunkStream([b"abcd", b"e\n"])

        assert [line async for line in long_task._bounded_stream_lines(stream)] == [
            "abcd" + long_task._TRUNCATED_TEXT
        ]

    async def test_failed_wait_that_sets_returncode_does_not_kill(self) -> None:
        """A reaped process is not signalled again when its wait task raises."""
        process = _FakeProcess(
            stdout=_ChunkStream([]),
            stderr=_ChunkStream([]),
            exit_code=-15,
        )

        async def failed_after_reap() -> int:
            process.returncode = -15
            raise RuntimeError("wait callback failed")

        wait_task = asyncio.create_task(failed_after_reap())
        await long_task._terminate_and_reap(process, wait_task)  # type: ignore[arg-type]

        assert process.terminate_calls == 1
        assert process.kill_calls == 0
        assert process.returncode == -15

    async def test_cancellation_consumes_pending_drains_and_coordination(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Caller cancellation explicitly cancels every still-pending auxiliary future."""

        class BlockingStream:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.cancelled = False

            async def read(self, _size: int) -> bytes:
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
                return b""

        class BlockingProcess(_FakeProcess):
            def __init__(self) -> None:
                self.stdout_stream = BlockingStream()
                self.stderr_stream = BlockingStream()
                super().__init__(
                    stdout=self.stdout_stream,  # type: ignore[arg-type]
                    stderr=self.stderr_stream,  # type: ignore[arg-type]
                    exit_code=-15,
                )
                self.release = asyncio.Event()

            async def wait(self) -> int:
                self.wait_calls += 1
                await self.release.wait()
                self.returncode = self.exit_code
                return self.exit_code

            def terminate(self) -> None:
                self.terminate_calls += 1
                self.release.set()

        process = BlockingProcess()
        real_gather = asyncio.gather
        coordination = asyncio.get_running_loop().create_future()
        gather_calls = 0

        def selective_gather(*awaitables: object, **kwargs: object):
            nonlocal gather_calls
            gather_calls += 1
            if gather_calls == 1:
                return coordination
            return real_gather(*awaitables, **kwargs)

        async def fake_spawn(*_argv: str, **_kwargs: object) -> BlockingProcess:
            return process

        monkeypatch.setattr(long_task, "TaskStatusWriter", _WriterSpy)
        monkeypatch.setattr(long_task, "_try_get_task_id", lambda _ctx: None)
        monkeypatch.setattr(long_task, "make_task_id", lambda _tool: "pending-cleanup")
        monkeypatch.setattr(long_task.asyncio, "create_subprocess_exec", fake_spawn)
        monkeypatch.setattr(long_task.asyncio, "gather", selective_gather)

        runner = asyncio.create_task(
            long_task._run_long_task(
                ["command"],
                ctx=_Context(),
                progress=_Progress(),
                is_stack_op=False,
            )
        )
        await process.stdout_stream.started.wait()
        await process.stderr_stream.started.wait()
        runner.cancel()

        with pytest.raises(asyncio.CancelledError):
            await runner

        assert process.stdout_stream.cancelled is True
        assert process.stderr_stream.cancelled is True
        assert coordination.cancelled() is True
        assert process.terminate_calls == 1


class TestCancellationBeforeHeartbeatExists:
    """Cancellation raised before ``heartbeat`` is ever assigned.

    ``heartbeat`` is created immediately before the process-wait/drain
    coordination, with nothing awaited in between. The only way to observe
    ``heartbeat is None`` inside the ``CancelledError`` handler is to cancel
    while still inside (or before) ``asyncio.create_subprocess_exec`` itself.
    """

    async def test_cancellation_during_spawn_skips_heartbeat_cancel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawn_started = asyncio.Event()

        async def blocking_spawn(*_argv: str, **_kwargs: object) -> Any:
            spawn_started.set()
            await asyncio.Event().wait()  # Never resolves; cancellation interrupts this.

        monkeypatch.setattr(long_task, "TaskStatusWriter", _WriterSpy)
        monkeypatch.setattr(long_task, "_try_get_task_id", lambda _ctx: None)
        monkeypatch.setattr(long_task, "make_task_id", lambda _tool: "pre-spawn-cancel")
        monkeypatch.setattr(long_task.asyncio, "create_subprocess_exec", blocking_spawn)

        runner = asyncio.create_task(
            long_task._run_long_task(
                ["command"],
                ctx=_Context(),
                progress=_Progress(),
                is_stack_op=False,
            )
        )
        await spawn_started.wait()
        runner.cancel()

        with pytest.raises(asyncio.CancelledError):
            await runner

        writer = _WriterSpy.latest
        assert writer is not None
        # process was never assigned, so exit_code is None and cleanup is a no-op.
        assert writer.finishes == [{"state": "cancelled", "exit_code": None, "error": "cancelled"}]
