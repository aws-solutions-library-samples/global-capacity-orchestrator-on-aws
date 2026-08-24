"""
Tests for the shared async subprocess runner used by long-running MCP tools
(`gco_mcp/tools/_long_task.py::_run_long_task`).

Covers four behaviours of the helper:

* the lifecycle path — drains stdout/stderr from a real Python subprocess,
  emits progress messages, increments the progress counter on every
  ``(CREATE|UPDATE|DELETE)_COMPLETE`` line, and returns the success JSON
  on a clean exit;
* the cancellation path — on ``asyncio.CancelledError`` the runner sends
  ``SIGTERM``, waits up to 10 s for graceful shutdown, sends ``SIGKILL`` if
  needed, and re-raises the cancellation;
* the stack-op cancellation disclaimer — when ``is_stack_op=True`` the
  re-raised ``CancelledError`` carries the literal CFN partial-state
  disclaimer so operators know AWS state may be inconsistent;
* path-traversal rejection — any non-flag argv element containing a ``..``
  segment short-circuits before ``asyncio.create_subprocess_exec`` is
  called, returning a structured error JSON.
"""

import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure gco_mcp/ is importable, mirroring tests/test_mcp_audit.py and
# tests/test_mcp_feature_flags.py.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import cli_runner  # noqa: E402
from tools._long_task import _run_long_task  # noqa: E402


class _FakeProgress(dict):
    """Minimal async-callable stand-in for FastMCP's Progress dependency.

    Subclasses ``dict`` so the JSON encoder used by the audit decorator can
    serialize an instance without complaint when the fake is passed in via
    a wrapper's ``progress=`` keyword. The ``messages`` and ``increments``
    attributes the test code reads from are kept as plain attributes — the
    encoder only sees the empty mapping.
    """

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []
        self.increments: int = 0
        self.totals: list[int] = []

    async def set_message(self, msg: str) -> None:
        self.messages.append(msg)

    async def increment(self) -> None:
        self.increments += 1

    async def set_total(self, total: int) -> None:
        self.totals.append(total)


class _FakeCtx(dict):
    """Minimal async-callable stand-in for FastMCP's Context dependency.

    Subclasses ``dict`` for the same JSON-serialization reason as
    ``_FakeProgress`` — it lets the audit decorator's argument sanitizer
    handle a ``ctx=`` keyword without the encoder choking on a non-mapping
    object.
    """

    def __init__(self) -> None:
        super().__init__()
        self.infos: list[str] = []

    async def info(self, msg: str) -> None:
        self.infos.append(msg)


# Lifecycle test
async def test_run_long_task_lifecycle() -> None:
    """A short-lived subprocess that emits a CDK per-stack done marker
    yields a success payload with a stack-completion count and per-stack
    progress increments.

    The child prints a few lines including ``✅  gco-test-stack`` (CDK's
    per-stack done marker) so we can assert both the line-by-line progress
    drain and the per-stack increment.
    """
    progress = _FakeProgress()
    ctx = _FakeCtx()
    argv = [
        sys.executable,
        "-c",
        (
            "import sys, time\n"
            "print('starting'); sys.stdout.flush()\n"
            "print('\\u2705  gco-test-stack'); sys.stdout.flush()\n"
            "time.sleep(0.05)\n"
            "print('done'); sys.stdout.flush()\n"
        ),
    ]

    result = await _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=False)

    parsed = json.loads(result)
    assert parsed["status"] == "ok"
    assert parsed["stacks_completed"] == 1
    assert "duration_seconds" in parsed
    assert isinstance(parsed["duration_seconds"], int)
    assert parsed["duration_seconds"] >= 0
    # The stack-done line carried gco-test-stack, so last_stack tracks it.
    assert parsed["last_stack"] == "gco-test-stack"
    assert any("starting" in m for m in progress.messages), progress.messages
    assert any("done" in m for m in progress.messages), progress.messages
    # Exactly one stack-done line → exactly one increment.
    assert progress.increments == 1


# Cancellation test
async def test_run_long_task_cancellation() -> None:
    """An in-flight task that sleeps for a minute terminates within 11 s on cancel.

    The 11 s budget is the 10 s SIGTERM grace window the runner enforces
    plus a small safety margin for scheduler jitter.
    """
    progress = _FakeProgress()
    ctx = _FakeCtx()
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]

    coro_task = asyncio.create_task(
        _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=False)
    )
    # Let the subprocess actually start before we cancel it.
    await asyncio.sleep(0.5)

    t0 = time.monotonic()
    coro_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await coro_task
    elapsed = time.monotonic() - t0

    assert elapsed < 11.0, f"cancellation took {elapsed:.2f}s, expected <11s"


# Cancellation-disclaimer test for stack ops
async def test_run_long_task_cancellation_includes_disclaimer() -> None:
    """When ``is_stack_op=True``, the re-raised CancelledError names the disclaimer.

    Operators need to know that cancellation may have left CloudFormation
    in a partial state so they go check the stack via ``stack_status`` or
    the AWS console.
    """
    progress = _FakeProgress()
    ctx = _FakeCtx()
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]

    coro_task = asyncio.create_task(
        # is_stack_op defaults to True; pass it explicitly for clarity.
        _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=True)
    )
    await asyncio.sleep(0.5)
    coro_task.cancel()

    try:
        await coro_task
    except asyncio.CancelledError as e:
        assert e.args, "expected CancelledError to carry a disclaimer message"
        msg = str(e.args[0])
        assert "Partial CloudFormation state may remain" in msg, msg
        assert "stack_status" in msg, msg
    else:
        pytest.fail("expected CancelledError, got clean completion")


# Path-traversal rejection test
async def test_run_long_task_rejects_path_traversal() -> None:
    """A ``..`` segment in any non-flag argv element short-circuits before spawn.

    Patches ``asyncio.create_subprocess_exec`` inside the ``_long_task``
    module so we can confirm the runner never reaches the spawn call. The
    structured error JSON also pinpoints which argv index tripped the
    check.
    """
    progress = _FakeProgress()
    ctx = _FakeCtx()
    argv = [sys.executable, "../etc/passwd"]

    with patch(
        "tools._long_task.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    ) as mock_spawn:
        result = await _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=False)
        assert mock_spawn.call_count == 0, "subprocess must not be spawned for traversal argv"

    parsed = json.loads(result)
    assert parsed == {
        "error": "path_traversal_detected",
        "argv_index": 1,
        "value": "../etc/passwd",
    }


# Failure surfacing — non-zero exit raises ToolError with structured payload
async def test_run_long_task_failure_raises_tool_error() -> None:
    """When the subprocess exits non-zero, the runner raises ``ToolError``
    with a structured JSON payload so the MCP client renders it as a
    tool-level error rather than success-shaped data."""
    from fastmcp.exceptions import ToolError

    progress = _FakeProgress()
    ctx = _FakeCtx()
    argv = [
        sys.executable,
        "-c",
        (
            "import sys\n"
            "print('\\u2705  gco-global', flush=True)\n"
            "print('CREATE_FAILED gco-us-east-1: hit a wall', flush=True)\n"
            "sys.stderr.write('Resource handler failed: AccessDeniedException\\n')\n"
            "sys.stderr.flush()\n"
            "sys.exit(2)\n"
        ),
    ]

    with pytest.raises(ToolError) as excinfo:
        await _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=True)

    payload = json.loads(str(excinfo.value))
    assert payload["error"] == "exit_code=2"
    assert payload["exit_code"] == 2
    assert payload["stacks_completed"] == 1, "the one stack done line before the failure"
    assert payload["last_stack"] == "gco-us-east-1"
    assert any("CREATE_FAILED gco-us-east-1" in line for line in payload["failed_events"])
    assert any("AccessDeniedException" in line for line in payload["stderr_tail"])
    assert "Partial CloudFormation state" in payload["disclaimer"]
    assert isinstance(payload["duration_seconds"], int)


async def test_run_long_task_failure_omits_disclaimer_when_not_stack_op() -> None:
    """Non-stack failures (e.g. images_build) don't include the
    CloudFormation partial-state disclaimer."""
    from fastmcp.exceptions import ToolError

    progress = _FakeProgress()
    ctx = _FakeCtx()
    argv = [sys.executable, "-c", "import sys; sys.exit(1)"]

    with pytest.raises(ToolError) as excinfo:
        await _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=False)

    payload = json.loads(str(excinfo.value))
    assert payload["exit_code"] == 1
    assert "disclaimer" not in payload


async def test_run_long_task_success_carries_last_stack_name() -> None:
    """When the output mentions a ``gco-*`` stack name and emits CDK's
    per-stack done markers, ``last_stack`` reflects the most recent one
    and ``stacks_completed`` matches the number of stack-done markers."""
    progress = _FakeProgress()
    ctx = _FakeCtx()
    argv = [
        sys.executable,
        "-c",
        (
            "import sys\n"
            "print('gco-global: deploying', flush=True)\n"
            "print('\\u2705  gco-global', flush=True)\n"
            "print('gco-us-east-1: deploying', flush=True)\n"
            "print('\\u2705  gco-us-east-1', flush=True)\n"
        ),
    ]

    result = await _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=False)

    parsed = json.loads(result)
    assert parsed["status"] == "ok"
    assert parsed["stacks_completed"] == 2
    assert parsed["last_stack"] == "gco-us-east-1"


async def test_run_long_task_emits_heartbeat_when_subprocess_quiet(monkeypatch) -> None:
    """When the subprocess goes silent past the heartbeat interval, the
    runner emits a 'still running' progress message and ctx.info so the
    MCP client doesn't render a stalled state."""
    import tools._long_task as long_task_mod

    # Compress the heartbeat interval so the test stays fast (default is
    # 30 s; a 0.2 s interval still exercises the same code path).
    monkeypatch.setattr(long_task_mod, "_HEARTBEAT_INTERVAL_SECONDS", 0.2)

    progress = _FakeProgress()
    ctx = _FakeCtx()
    argv = [
        sys.executable,
        "-c",
        # First line, then quiet for ~0.6 s, then exit. With a 0.2 s
        # heartbeat, two heartbeat ticks should fire before exit.
        (
            "import sys, time\n"
            "print('starting some long quiet phase', flush=True)\n"
            "time.sleep(0.6)\n"
        ),
    ]

    result = await _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=False)

    parsed = json.loads(result)
    assert parsed["status"] == "ok"
    # At least one 'still running' heartbeat must have been emitted on
    # both progress and ctx.info channels.
    assert any("still running" in m for m in progress.messages), progress.messages
    assert any("still running" in m for m in ctx.infos), ctx.infos


async def test_run_long_task_heartbeat_does_not_double_count_progress(monkeypatch) -> None:
    """The heartbeat updates the progress *message* but does NOT call
    ``progress.increment()``. Increment is reserved for AWS-side
    milestones (CDK per-stack done markers), so heartbeat ticks during a
    quiet phase don't pollute the completion counter."""
    import tools._long_task as long_task_mod

    monkeypatch.setattr(long_task_mod, "_HEARTBEAT_INTERVAL_SECONDS", 0.2)

    progress = _FakeProgress()
    ctx = _FakeCtx()
    argv = [
        sys.executable,
        "-c",
        # One stack done marker then quiet for ~0.6 s. Should yield
        # exactly 1 increment, even though several heartbeats fire.
        ("import sys, time\nprint('\\u2705  gco-global', flush=True)\ntime.sleep(0.6)\n"),
    ]

    result = await _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=False)

    parsed = json.loads(result)
    assert parsed["stacks_completed"] == 1
    assert progress.increments == 1, (
        f"expected 1 increment from the single stack done line, got {progress.increments}"
    )


async def test_run_long_task_failure_payload_caps_stderr_tail() -> None:
    """The structured failure payload caps stderr at the configured tail
    length so a runaway error log doesn't blow up the ToolError message."""
    import tools._long_task as long_task_mod
    from fastmcp.exceptions import ToolError

    progress = _FakeProgress()
    ctx = _FakeCtx()
    # 200 stderr lines — well over the configured cap.
    argv = [
        sys.executable,
        "-c",
        (
            "import sys\n"
            "for i in range(200):\n"
            "    sys.stderr.write(f'error line {i}\\n')\n"
            "sys.stderr.flush()\n"
            "sys.exit(1)\n"
        ),
    ]

    with pytest.raises(ToolError) as excinfo:
        await _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=False)

    payload = json.loads(str(excinfo.value))
    assert len(payload["stderr_tail"]) == long_task_mod._STDERR_TAIL_LINES
    # The most recent lines win — first line in the payload should be
    # the first one within the tail window, not error line 0.
    assert payload["stderr_tail"][-1] == "error line 199"


async def test_run_long_task_forwards_total_units_to_set_total() -> None:
    """When ``total_units`` is supplied, it's forwarded to
    ``progress.set_total(...)`` once at startup so the client renders a
    real percentage instead of indeterminate progress."""
    progress = _FakeProgress()
    ctx = _FakeCtx()
    argv = [sys.executable, "-c", "print('done')"]

    await _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=False, total_units=4)

    assert progress.totals == [4], f"expected exactly one set_total(4) call, got {progress.totals}"


async def test_run_long_task_skips_set_total_when_unset() -> None:
    """No ``total_units`` argument means no ``set_total`` call — the
    client falls back to indeterminate progress, same as before this
    feature landed."""
    progress = _FakeProgress()
    ctx = _FakeCtx()
    argv = [sys.executable, "-c", "print('done')"]

    await _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=False)

    assert progress.totals == [], (
        f"expected no set_total calls when total_units is None, got {progress.totals}"
    )


async def test_run_long_task_dedupes_repeated_stack_done_lines() -> None:
    """When the same stack-done marker appears twice (e.g. CDK echoes
    on stdout and stderr), only the first occurrence increments the
    progress counter."""
    progress = _FakeProgress()
    ctx = _FakeCtx()
    argv = [
        sys.executable,
        "-c",
        (
            "import sys\n"
            # Same stack name twice — should yield exactly 1 increment.
            "print('\\u2705  gco-global', flush=True)\n"
            "print('\\u2705  gco-global', flush=True)\n"
            # Different stack — yields a second increment.
            "print('\\u2705  gco-us-east-1', flush=True)\n"
        ),
    ]

    result = await _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=False)

    parsed = json.loads(result)
    assert parsed["stacks_completed"] == 2
    assert progress.increments == 2


async def test_run_long_task_writes_disk_status_artifacts(tmp_path, monkeypatch) -> None:
    """A successful invocation leaves a JSON status file and a raw log
    file in the configured status directory, with terminal state set
    and the log capturing both stdout and stderr lines.

    This is the integration test for the disk-backed observability
    channel — it proves the writer is wired through ``_run_long_task``
    and that operators (or other agents) reading the directory will
    see the same state the MCP wire surfaces.
    """
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path))
    monkeypatch.delenv("GCO_DISABLE_TASK_STATUS", raising=False)

    progress = _FakeProgress()
    ctx = _FakeCtx()
    argv = [
        sys.executable,
        "-c",
        (
            "import sys\n"
            "print('hello stdout', flush=True)\n"
            "print('worry stderr', file=sys.stderr, flush=True)\n"
            "print('\\u2705  gco-global', flush=True)\n"
        ),
    ]

    result = await _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=False)
    parsed = json.loads(result)
    task_id = parsed["task_id"]
    assert task_id, "success payload must include the task_id"

    status_path = tmp_path / f"{task_id}.json"
    log_path = tmp_path / f"{task_id}.log"
    assert status_path.exists()
    assert log_path.exists()

    record = json.loads(status_path.read_text())
    assert record["state"] == "succeeded"
    assert record["exit_code"] == 0
    assert record["stacks_completed"] == 1
    assert record["last_stack"] == "gco-global"

    log_text = log_path.read_text()
    assert "[stdout] hello stdout" in log_text
    assert "[stderr] worry stderr" in log_text


async def test_run_long_task_tool_name_derived_from_gco_argv(tmp_path, monkeypatch) -> None:
    """A logical ``gco`` argv records a readable tool name while spawning the
    version-matched executable from the MCP environment at the project root."""
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path))
    monkeypatch.delenv("GCO_DISABLE_TASK_STATUS", raising=False)

    progress = _FakeProgress()
    ctx = _FakeCtx()
    child_argv = [
        sys.executable,
        "-c",
        "print('done', flush=True)",
    ]
    logical_argv = ["gco", "stacks", "deploy-all", "-y"]
    real_spawn = asyncio.create_subprocess_exec
    observed: dict[str, object] = {}

    async def fake_spawn(*spawn_argv, **kwargs):
        observed["argv"] = spawn_argv
        observed["cwd"] = kwargs.get("cwd")
        # Run a benign quick command instead of the real GCO operation.
        return await real_spawn(*child_argv, **kwargs)

    with patch("tools._long_task.asyncio.create_subprocess_exec", side_effect=fake_spawn):
        result = await _run_long_task(
            logical_argv,
            ctx=ctx,
            progress=progress,
            is_stack_op=False,
        )

    assert observed["argv"] == (
        cli_runner._gco_executable(),
        "stacks",
        "deploy-all",
        "-y",
    )
    assert observed["cwd"] == str(cli_runner.PROJECT_ROOT)

    parsed = json.loads(result)
    task_id = parsed["task_id"]
    assert task_id.startswith("stacks_deploy_all-"), task_id

    record = json.loads((tmp_path / f"{task_id}.json").read_text())
    assert record["tool"] == "stacks_deploy_all"
    # Status records preserve the logical command, not a machine-specific
    # resolved executable path.
    assert record["argv"] == logical_argv


async def test_run_long_task_reuses_safe_protocol_task_id(tmp_path, monkeypatch) -> None:
    """A safe FastMCP task ID is reused for the disk status/log pair."""
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path))
    monkeypatch.delenv("GCO_DISABLE_TASK_STATUS", raising=False)

    progress = _FakeProgress()
    ctx = _FakeCtx()
    task_context = MagicMock(spec=["task_id"])
    task_context.task_id = "protocol-task-123"

    with patch(
        "fastmcp.server.dependencies.get_task_context",
        return_value=task_context,
    ):
        result = await _run_long_task(
            [sys.executable, "-c", "print('done', flush=True)"],
            ctx=ctx,
            progress=progress,
            is_stack_op=False,
        )

    parsed = json.loads(result)
    assert parsed["task_id"] == "protocol-task-123"
    assert (tmp_path / "protocol-task-123.json").is_file()
    assert (tmp_path / "protocol-task-123.log").is_file()


async def test_run_long_task_failure_writes_failed_state_to_disk(tmp_path, monkeypatch) -> None:
    """A non-zero exit leaves ``state=failed`` and the recorded
    exit_code on disk so observers see the same outcome the MCP
    client gets via the structured ToolError."""
    from fastmcp.exceptions import ToolError

    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path))
    monkeypatch.delenv("GCO_DISABLE_TASK_STATUS", raising=False)

    progress = _FakeProgress()
    ctx = _FakeCtx()
    argv = [
        sys.executable,
        "-c",
        "import sys; print('boom', file=sys.stderr, flush=True); sys.exit(2)",
    ]

    with pytest.raises(ToolError) as excinfo:
        await _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=False)

    payload = json.loads(str(excinfo.value))
    task_id = payload["task_id"]
    record = json.loads((tmp_path / f"{task_id}.json").read_text())
    assert record["state"] == "failed"
    assert record["exit_code"] == 2


# =============================================================================
# Long-running stack lifecycle tools — registration + argv kick-off
# =============================================================================
#
# The deploy/destroy/bootstrap tools register conditionally: deploy_stack /
# deploy_all / bootstrap_cdk under ``GCO_ENABLE_INFRASTRUCTURE_DEPLOY`` and
# destroy_stack / destroy_all under ``GCO_ENABLE_INFRASTRUCTURE_DESTROY``.
# Each one builds a CLI argv and hands it to ``_run_long_task`` to drive the
# subprocess and stream progress through FastMCP's task protocol.
#
# These tests mock ``_run_long_task`` so the suite runs with no AWS access
# and no spawned subprocesses — what we care about here is the constructed
# argv, not the real CDK invocation. The cancellation-disclaimer test below
# drives a real subprocess so we can confirm the disclaimer surfaces.

import importlib  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402

import run_mcp  # noqa: E402


def _list_tool_names() -> set[str]:
    """Snapshot every registered tool name from the live mcp instance."""
    tools = asyncio.run(run_mcp.mcp._list_tools())
    return {t.name for t in tools}


# =============================================================================
# Absent-by-default registration tests
# =============================================================================


_INFRASTRUCTURE_GATED_TOOLS = (
    "deploy_stack",
    "deploy_all",
    "bootstrap_cdk",
    "destroy_stack",
    "destroy_all",
)


def _force_unregister_infrastructure_tools() -> None:
    """Strip infrastructure-gated tool registrations from the live mcp singleton.

    Earlier tests in the suite may set GCO_ENABLE_INFRASTRUCTURE_DEPLOY /
    GCO_ENABLE_INFRASTRUCTURE_DESTROY and reload run_mcp, which leaves the
    gated tool names registered against the module-level FastMCP instance.
    Default-env assertions in this file need to start from a clean registry,
    so this helper drops the names before each absent-by-default check.
    """
    for name in _INFRASTRUCTURE_GATED_TOOLS:
        with contextlib.suppress(Exception):
            run_mcp.mcp.local_provider.remove_tool(name)


class TestInfrastructureDeployFlag:
    """deploy_stack / deploy_all / bootstrap_cdk register only under the deploy flag."""

    def test_deploy_stack_absent_by_default(self):
        # Clean env (no infrastructure flag) → tool is unregistered.
        for var in (
            "GCO_ENABLE_INFRASTRUCTURE_DEPLOY",
            "GCO_ENABLE_INFRASTRUCTURE_DESTROY",
            "GCO_ENABLE_ALL_TOOLS",
        ):
            os.environ.pop(var, None)
        _force_unregister_infrastructure_tools()
        importlib.reload(run_mcp)
        # Strip again after reload — run_mcp's reload blocks may re-register
        # tools when env vars from a previous test were patched but cleared
        # without unsetting them on the live singleton.
        _force_unregister_infrastructure_tools()

        names = _list_tool_names()
        assert "deploy_stack" not in names
        assert "deploy_all" not in names
        assert "bootstrap_cdk" not in names


class TestInfrastructureDestroyFlag:
    """destroy_stack / destroy_all register only under the destroy flag."""

    def test_destroy_stack_absent_by_default(self):
        for var in (
            "GCO_ENABLE_INFRASTRUCTURE_DEPLOY",
            "GCO_ENABLE_INFRASTRUCTURE_DESTROY",
            "GCO_ENABLE_ALL_TOOLS",
        ):
            os.environ.pop(var, None)
        _force_unregister_infrastructure_tools()
        importlib.reload(run_mcp)
        _force_unregister_infrastructure_tools()

        names = _list_tool_names()
        assert "destroy_stack" not in names
        assert "destroy_all" not in names


# =============================================================================
# argv kick-off tests — mock ``_run_long_task`` and inspect the constructed argv
# =============================================================================


class TestInfrastructureDeployTools:
    """Constructed argv for deploy_stack / deploy_all / bootstrap_cdk."""

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DEPLOY": "true"})
    def test_deploy_stack_argv_includes_yes_but_not_require_approval(self):
        # ``gco stacks deploy`` doesn't accept ``--require-approval`` —
        # that flag is for ``cdk deploy`` and is forwarded internally by
        # the CLI when ``-y`` is set. Including it here previously caused
        # ``Error: No such option: --require-approval`` and an exit_code=2
        # ToolError on every deploy_stack call.
        importlib.reload(run_mcp)
        with patch("tools.stacks._run_long_task", new_callable=AsyncMock) as mock_task:
            mock_task.return_value = '{"status": "ok", "completes": 0}'
            asyncio.run(
                run_mcp.deploy_stack(
                    stack_name="gco-us-east-1",
                    ctx=_FakeCtx(),
                    progress=_FakeProgress(),
                )
            )
        argv = mock_task.call_args.args[0]
        assert argv[:4] == ["gco", "stacks", "deploy", "gco-us-east-1"]
        assert "--require-approval" not in argv
        assert "-y" in argv
        # is_stack_op must default to True so cancellation surfaces the disclaimer.
        assert mock_task.call_args.kwargs["is_stack_op"] is True

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DEPLOY": "true"})
    def test_deploy_stack_argv_includes_outputs_file_and_tags(self):
        importlib.reload(run_mcp)
        with patch("tools.stacks._run_long_task", new_callable=AsyncMock) as mock_task:
            mock_task.return_value = '{"status": "ok", "completes": 0}'
            asyncio.run(
                run_mcp.deploy_stack(
                    stack_name="gco-global",
                    yes=False,
                    outputs_file="/tmp/out.json",
                    tags=["Environment=prod", "Owner=ml"],
                    ctx=_FakeCtx(),
                    progress=_FakeProgress(),
                )
            )
        argv = mock_task.call_args.args[0]
        # ``yes=False`` skips the -y flag.
        assert "-y" not in argv
        assert "--outputs-file" in argv
        assert "/tmp/out.json" in argv
        # Each tag becomes a ``--tag value`` pair.
        tag_indices = [i for i, v in enumerate(argv) if v == "--tag"]
        assert len(tag_indices) == 2
        assert {argv[i + 1] for i in tag_indices} == {"Environment=prod", "Owner=ml"}

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DEPLOY": "true"})
    def test_deploy_all_argv_includes_yes_but_not_require_approval(self):
        # Same Click-flag mismatch as deploy_stack: ``gco stacks deploy-all``
        # doesn't accept ``--require-approval``.
        importlib.reload(run_mcp)
        with patch("tools.stacks._run_long_task", new_callable=AsyncMock) as mock_task:
            mock_task.return_value = '{"status": "ok", "completes": 0}'
            asyncio.run(
                run_mcp.deploy_all(
                    parallel=True,
                    max_workers=8,
                    ctx=_FakeCtx(),
                    progress=_FakeProgress(),
                )
            )
        argv = mock_task.call_args.args[0]
        assert argv[:3] == ["gco", "stacks", "deploy-all"]
        assert "--require-approval" not in argv
        assert "-y" in argv
        assert "--parallel" in argv
        assert "--max-workers" in argv
        assert "8" in argv
        assert mock_task.call_args.kwargs["is_stack_op"] is True

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DEPLOY": "true"})
    def test_bootstrap_cdk_argv_includes_region_and_account(self):
        importlib.reload(run_mcp)
        with patch("tools.stacks._run_long_task", new_callable=AsyncMock) as mock_task:
            mock_task.return_value = '{"status": "ok", "completes": 0}'
            asyncio.run(
                run_mcp.bootstrap_cdk(
                    region="eu-west-1",
                    account="123456789012",
                    ctx=_FakeCtx(),
                    progress=_FakeProgress(),
                )
            )
        argv = mock_task.call_args.args[0]
        assert argv[:3] == ["gco", "stacks", "bootstrap"]
        assert "--region" in argv
        assert "eu-west-1" in argv
        assert "--account" in argv
        assert "123456789012" in argv
        # Bootstrap is a stack op too — cancellation should carry the disclaimer.
        assert mock_task.call_args.kwargs["is_stack_op"] is True

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DEPLOY": "true"})
    def test_bootstrap_cdk_omits_account_when_not_provided(self):
        importlib.reload(run_mcp)
        with patch("tools.stacks._run_long_task", new_callable=AsyncMock) as mock_task:
            mock_task.return_value = '{"status": "ok", "completes": 0}'
            asyncio.run(
                run_mcp.bootstrap_cdk(
                    region="us-west-2",
                    ctx=_FakeCtx(),
                    progress=_FakeProgress(),
                )
            )
        argv = mock_task.call_args.args[0]
        assert "--account" not in argv


class TestInfrastructureDestroyTools:
    """Constructed argv for destroy_stack / destroy_all."""

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DESTROY": "true"})
    def test_destroy_stack_argv_includes_yes(self):
        importlib.reload(run_mcp)
        with patch("tools.stacks._run_long_task", new_callable=AsyncMock) as mock_task:
            mock_task.return_value = '{"status": "ok", "completes": 0}'
            asyncio.run(
                run_mcp.destroy_stack(
                    stack_name="gco-us-east-1",
                    ctx=_FakeCtx(),
                    progress=_FakeProgress(),
                )
            )
        argv = mock_task.call_args.args[0]
        assert argv[:4] == ["gco", "stacks", "destroy", "gco-us-east-1"]
        assert "-y" in argv
        assert mock_task.call_args.kwargs["is_stack_op"] is True

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DESTROY": "true"})
    def test_destroy_stack_forwards_retain_volumes(self):
        importlib.reload(run_mcp)
        with patch("tools.stacks._run_long_task", new_callable=AsyncMock) as mock_task:
            mock_task.return_value = '{"status": "ok", "completes": 0}'
            asyncio.run(
                run_mcp.destroy_stack(
                    stack_name="gco-us-east-1",
                    retain_volumes=True,
                    ctx=_FakeCtx(),
                    progress=_FakeProgress(),
                )
            )
        assert "--retain-volumes" in mock_task.call_args.args[0]

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DESTROY": "true"})
    def test_destroy_stack_deletes_volumes_by_default(self):
        importlib.reload(run_mcp)
        with patch("tools.stacks._run_long_task", new_callable=AsyncMock) as mock_task:
            mock_task.return_value = '{"status": "ok", "completes": 0}'
            asyncio.run(
                run_mcp.destroy_stack(
                    stack_name="gco-us-east-1",
                    ctx=_FakeCtx(),
                    progress=_FakeProgress(),
                )
            )
        assert "--retain-volumes" not in mock_task.call_args.args[0]

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DESTROY": "true"})
    def test_destroy_stack_omits_yes_when_disabled(self):
        importlib.reload(run_mcp)
        with patch("tools.stacks._run_long_task", new_callable=AsyncMock) as mock_task:
            mock_task.return_value = '{"status": "ok", "completes": 0}'
            asyncio.run(
                run_mcp.destroy_stack(
                    stack_name="gco-us-east-1",
                    yes=False,
                    ctx=_FakeCtx(),
                    progress=_FakeProgress(),
                )
            )
        argv = mock_task.call_args.args[0]
        assert "-y" not in argv

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DESTROY": "true"})
    def test_destroy_all_argv_includes_yes_and_parallel(self):
        importlib.reload(run_mcp)
        with patch("tools.stacks._run_long_task", new_callable=AsyncMock) as mock_task:
            mock_task.return_value = '{"status": "ok", "completes": 0}'
            asyncio.run(
                run_mcp.destroy_all(
                    parallel=True,
                    max_workers=4,
                    ctx=_FakeCtx(),
                    progress=_FakeProgress(),
                )
            )
        argv = mock_task.call_args.args[0]
        assert argv[:3] == ["gco", "stacks", "destroy-all"]
        assert "-y" in argv
        assert "--parallel" in argv
        assert "--max-workers" in argv
        assert "4" in argv
        assert mock_task.call_args.kwargs["is_stack_op"] is True


# =============================================================================
# Cancellation-disclaimer integration test for the stack lifecycle path
# =============================================================================


class TestDeployStackCancellation:
    """Cancellation of a real subprocess surfaces the partial-state disclaimer."""

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DEPLOY": "true"})
    def test_deploy_stack_cancellation_includes_disclaimer(self):
        """Drive ``_run_long_task`` against a long-sleep subprocess and cancel.

        Patches ``asyncio.create_subprocess_exec`` inside the long-task
        helper so the runner spawns a Python sleep instead of the real
        ``gco stacks deploy``. This keeps the test hermetic while still
        exercising the same drain + cancel + disclaimer path.
        """
        importlib.reload(run_mcp)

        progress = _FakeProgress()
        ctx = _FakeCtx()

        async def _drive() -> None:
            real_create = asyncio.create_subprocess_exec

            async def _fake_create(*_argv: str, **kwargs: object):
                # Replace the gco invocation with a long-running sleep.
                # Forward the captured kwargs (stdout/stderr=PIPE) untouched.
                return await real_create(
                    sys.executable,
                    "-c",
                    "import time; time.sleep(60)",
                    **kwargs,  # type: ignore[arg-type]
                )

            with patch(
                "tools._long_task.asyncio.create_subprocess_exec",
                side_effect=_fake_create,
            ):
                coro = asyncio.create_task(
                    run_mcp.deploy_stack(
                        stack_name="gco-us-east-1",
                        ctx=ctx,
                        progress=progress,
                    )
                )
                # Let the subprocess actually start before cancelling.
                await asyncio.sleep(0.5)
                coro.cancel()
                try:
                    await coro
                except asyncio.CancelledError as e:
                    msg = str(e.args[0]) if e.args else ""
                    assert "Partial CloudFormation state may remain" in msg, msg
                    assert "stack_status" in msg, msg
                    return
                pytest.fail("expected CancelledError, got clean completion")

        asyncio.run(_drive())


# =============================================================================
# Audit log task-id correlation for deploy_stack
# =============================================================================


class TestDeployStackAuditTaskId:
    """When request_context.meta.task_id is set, the audit entry exposes it."""

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DEPLOY": "true"})
    def test_deploy_stack_audit_includes_task_id(self, caplog, monkeypatch):
        importlib.reload(run_mcp)

        # Patch the audit decorator's context lookup so the entry sees a
        # synthetic FastMCP context with a known task_id. ``audit`` is
        # imported lazily here so the reload above wins.
        import audit  # noqa: F401  - imported for monkeypatch target

        meta = MagicMock(spec=["task_id"])
        meta.task_id = "deploy-task-xyz"
        request_context = MagicMock()
        request_context.meta = meta
        fake_ctx = MagicMock()
        fake_ctx.request_context = request_context
        fake_ctx.request_id = "req-deploy-001"
        fake_ctx.client_id = "kiro"

        monkeypatch.setattr("audit._try_get_fastmcp_context", lambda: fake_ctx)

        # Stub the long-task helper so the call resolves without a real
        # subprocess. We're auditing the entry shape, not the spawn.
        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch("tools.stacks._run_long_task", new_callable=AsyncMock) as mock_task,
        ):
            mock_task.return_value = '{"status": "ok", "completes": 0}'
            asyncio.run(
                run_mcp.deploy_stack(
                    stack_name="gco-us-east-1",
                    ctx=_FakeCtx(),
                    progress=_FakeProgress(),
                )
            )

        entries = [
            json.loads(r.message)
            for r in caplog.records
            if r.name == "gco.mcp.audit"
            and json.loads(r.message).get("event") == "mcp.tool.invocation"
        ]
        assert entries, "expected an audit entry for deploy_stack"
        entry = entries[-1]
        assert entry["tool"] == "deploy_stack"
        assert entry["task_id"] == "deploy-task-xyz"
        assert entry["request_id"] == "req-deploy-001"
        assert entry["client_id"] == "kiro"


# =============================================================================
# Task-config mode contract — deploy/destroy tools must be optional, not required
# =============================================================================


class TestInfrastructureToolTaskMode:
    """The five long-running infrastructure tools opt into the FastMCP task
    protocol with ``mode="optional"`` rather than ``mode="required"``.

    Required mode locks out clients that don't speak the task protocol —
    notably the GCO MCP orchestrator's ``call_tool`` proxy and any other
    client that calls a tool synchronously without sending ``task_meta``.
    Optional mode lets clients with task-protocol support poll
    ``tasks://gco/{task_id}`` while clients without it run the tool
    inline with progress streamed through the FastMCP Progress dependency.

    If the running fastmcp version doesn't expose ``task_config`` on its
    registered Tool objects, the tests skip gracefully — TaskConfig is
    best-effort wired in the tool module.
    """

    def _expect_optional_mode(self, tool_name: str) -> None:
        """Assert ``tool_name`` is registered with ``task_config.mode == "optional"``."""
        tools = asyncio.run(run_mcp.mcp._list_tools())
        tool = next((t for t in tools if t.name == tool_name), None)
        assert tool is not None, f"{tool_name} must register under its feature flag"
        cfg = getattr(tool, "task_config", None)
        if cfg is None:
            pytest.skip("fastmcp build doesn't expose task_config on registered tools")
        assert getattr(cfg, "mode", None) == "optional", (
            f"{tool_name}.task_config.mode must be 'optional' so clients without "
            "task-protocol support can call it inline"
        )

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DEPLOY": "true"})
    def test_deploy_stack_task_mode_is_optional(self):
        importlib.reload(run_mcp)
        self._expect_optional_mode("deploy_stack")

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DEPLOY": "true"})
    def test_deploy_all_task_mode_is_optional(self):
        importlib.reload(run_mcp)
        self._expect_optional_mode("deploy_all")

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DEPLOY": "true"})
    def test_bootstrap_cdk_task_mode_is_optional(self):
        importlib.reload(run_mcp)
        self._expect_optional_mode("bootstrap_cdk")

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DESTROY": "true"})
    def test_destroy_stack_task_mode_is_optional(self):
        importlib.reload(run_mcp)
        self._expect_optional_mode("destroy_stack")

    @patch.dict(os.environ, {"GCO_ENABLE_INFRASTRUCTURE_DESTROY": "true"})
    def test_destroy_all_task_mode_is_optional(self):
        importlib.reload(run_mcp)
        self._expect_optional_mode("destroy_all")


# ---------------------------------------------------------------------------
# _format_duration helper — direct unit tests
# ---------------------------------------------------------------------------


class TestFormatDuration:
    """The duration formatter used by the heartbeat message renderer.

    Pinned here so the seconds / minutes / hours branches are
    exercised directly without standing up a long-running subprocess
    test for each cardinality.
    """

    def test_seconds_only(self) -> None:
        from tools._long_task import _format_duration

        assert _format_duration(0) == "0s"
        assert _format_duration(1) == "1s"
        assert _format_duration(45) == "45s"
        assert _format_duration(59) == "59s"

    def test_minutes_and_seconds(self) -> None:
        from tools._long_task import _format_duration

        # Exactly one minute zero-pads the seconds.
        assert _format_duration(60) == "1m00s"
        assert _format_duration(90) == "1m30s"
        assert _format_duration(125) == "2m05s"
        # Largest minute-only value is one second short of an hour.
        assert _format_duration(3599) == "59m59s"

    def test_hours_minutes_seconds(self) -> None:
        from tools._long_task import _format_duration

        assert _format_duration(3600) == "1h00m00s"
        assert _format_duration(3661) == "1h01m01s"
        assert _format_duration(7200) == "2h00m00s"
        # Multi-day operation — the hour count climbs without rolling.
        assert _format_duration(86400) == "24h00m00s"


async def test_bounded_stream_reader_truncates_oversized_line(monkeypatch) -> None:
    """A no-newline stream is chunked and explicitly truncated below its limit."""
    import tools._long_task as long_task_mod

    monkeypatch.setattr(long_task_mod, "_STREAM_READ_BYTES", 7)
    monkeypatch.setattr(long_task_mod, "_STREAM_LINE_MAX_BYTES", 64)
    reader = asyncio.StreamReader(limit=8)
    reader.feed_data(b"x" * 10_000)
    reader.feed_eof()

    lines = [line async for line in long_task_mod._bounded_stream_lines(reader)]

    assert len(lines) == 1
    assert lines[0].endswith("...[truncated]")
    assert len(lines[0].encode("utf-8")) <= 64


async def test_long_task_bounds_failed_events_and_diagnostic_lines() -> None:
    """Failure payloads retain only a bounded tail of bounded event lines."""
    import tools._long_task as long_task_mod
    from fastmcp.exceptions import ToolError

    progress = _FakeProgress()
    ctx = _FakeCtx()
    script = (
        "import sys\n"
        "for i in range(30):\n"
        " print(f'CREATE_FAILED gco-stack-{i} ' + 'x' * 10000, flush=True)\n"
        "sys.exit(1)\n"
    )

    with pytest.raises(ToolError) as excinfo:
        await _run_long_task(
            [sys.executable, "-c", script],
            ctx=ctx,
            progress=progress,
            is_stack_op=False,
        )

    payload = json.loads(str(excinfo.value))
    assert len(payload["failed_events"]) == long_task_mod._FAILED_EVENT_LINES
    assert all(
        len(line.encode("utf-8")) <= long_task_mod._DIAGNOSTIC_LINE_MAX_BYTES
        for line in payload["failed_events"]
    )
    assert "gco-stack-29" in payload["failed_events"][-1]


async def test_client_notification_failures_do_not_abort_subprocess() -> None:
    class BrokenClient:
        async def set_total(self, _total: int) -> None:
            raise RuntimeError("client disconnected")

        async def set_message(self, _message: str) -> None:
            raise RuntimeError("client disconnected")

        async def increment(self) -> None:
            raise RuntimeError("client disconnected")

        async def info(self, _message: str) -> None:
            raise RuntimeError("client disconnected")

    result = await _run_long_task(
        [sys.executable, "-c", "print('✅  gco-global', flush=True)"],
        ctx=BrokenClient(),
        progress=BrokenClient(),
        is_stack_op=False,
        total_units=1,
    )

    payload = json.loads(result)
    assert payload["status"] == "ok"
    assert payload["stacks_completed"] == 1


async def test_spawn_failure_leaves_terminal_pre_spawn_status(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path))
    monkeypatch.delenv("GCO_DISABLE_TASK_STATUS", raising=False)

    with (
        patch(
            "tools._long_task.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=FileNotFoundError("missing executable"),
        ),
        pytest.raises(FileNotFoundError, match="missing executable"),
    ):
        await _run_long_task(
            ["missing-gco"],
            ctx=_FakeCtx(),
            progress=_FakeProgress(),
            is_stack_op=False,
        )

    status_files = list(tmp_path.glob("*.json"))
    assert len(status_files) == 1
    record = json.loads(status_files[0].read_text(encoding="utf-8"))
    assert record["state"] == "failed"
    assert record["pid"] is None
    assert "FileNotFoundError" in record["error"]
