"""Processes the MCP server starts never read the server's stdin.

Under the stdio transport the server's stdin is the client's JSON-RPC message
stream. The CLI runners (``cli_runner._run_cli``, ``_run_cli_async``,
``tools._long_task._run_long_task``) and the kubectl-backed resources used to
start children that inherited it, so a ``gco`` prompt (``destroy_stack`` with
``yes=False``, a password prompt) read and discarded the client's queued
messages, or blocked until the call timed out. Every child now gets
``/dev/null`` as stdin: the queued messages stay with the server, and a prompt
reads end-of-file and fails at once. ``analytics_login_url`` without a
password refuses before starting the CLI, because a password prompt can also
open the controlling terminal.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gco_mcp"))

import cli_runner
import run_mcp

ROOT = Path(__file__).resolve().parent.parent

#: What an MCP client could have queued on the server's stdin.
_QUEUED = b"".join(
    json.dumps(message).encode() + b"\n"
    for message in (
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "list_jobs"}},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 5}},
    )
)

#: Runs one CLI runner in a fresh interpreter whose stdin holds ``_QUEUED``
#: (so the pytest worker's own stdin, its xdist channel, is never touched),
#: then reports the runner's result and how many queued bytes are left.
_DRIVER = r"""
import asyncio, json, os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "gco_mcp"))
import cli_runner
mode, fake = sys.argv[1], sys.argv[2]
cli_runner._gco_executable = lambda: fake
if mode == "sync":
    out = cli_runner._run_cli("probe", timeout_seconds=8)
elif mode == "async":
    out = asyncio.run(cli_runner._run_cli_async("probe", timeout_seconds=8))
else:
    from tools._long_task import _run_long_task

    async def run():
        return await asyncio.wait_for(
            _run_long_task(["gco", "probe"], ctx=None, progress=None, is_stack_op=False), 8
        )

    try:
        out = asyncio.run(run())
    except BaseException as exc:
        out = f"{type(exc).__name__}: {exc}"
os.set_blocking(0, False)
try:
    left = os.read(0, 65536)
except BlockingIOError:
    left = b""
print(json.dumps({"result": out, "left": len(left)}))
"""

#: A ``gco`` stand-in that reads its stdin to the end, like a prompt would.
_READS_STDIN = """
import json, os, sys
data = sys.stdin.buffer.read()
with open(os.environ["PROBE_OUT"], "w") as fh:
    json.dump({"stdin_bytes": len(data)}, fh)
print(json.dumps({"stdin_bytes": len(data)}))
"""

#: A ``gco`` stand-in that asks for confirmation the way ``gco stacks destroy`` does.
_PROMPTS = """
import click

@click.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def main():
    click.confirm("Are you sure you want to destroy gco-us-east-1?", abort=True)
    print("{}")

main()
"""


def _fake_gco(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "gco"
    script.write_text(f"#!{sys.executable}\n{body}")
    script.chmod(0o755)
    return script


def _drive(mode: str, fake: Path, tmp_path: Path) -> tuple[dict[str, Any], float]:
    """Run ``_DRIVER`` with ``_QUEUED`` on its stdin; the write end stays open."""
    env = {
        **os.environ,
        "PROBE_OUT": str(tmp_path / "probe.json"),
        "GCO_DISABLE_TASK_STATUS": "1",
    }
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-c", _DRIVER, mode, str(fake)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
        env=env,
    )
    assert proc.stdin is not None
    try:
        # A live client keeps its end open, so a reader of this pipe would block.
        proc.stdin.write(_QUEUED)
        proc.stdin.flush()
        proc.wait(timeout=60)
    finally:
        proc.stdin.close()
        if proc.poll() is None:  # only when the driver hangs
            proc.kill()
            proc.wait()
    elapsed = time.monotonic() - started
    assert proc.stdout is not None and proc.stderr is not None
    stdout = proc.stdout.read().decode()
    stderr = proc.stderr.read().decode()
    proc.stdout.close()
    proc.stderr.close()
    assert proc.returncode == 0, stderr
    return json.loads(stdout.strip().splitlines()[-1]), elapsed


@pytest.mark.parametrize("mode", ["sync", "async", "long-task"])
def test_children_read_end_of_file_and_leave_the_server_stdin_alone(
    mode: str, tmp_path: Path
) -> None:
    report, _elapsed = _drive(mode, _fake_gco(tmp_path, _READS_STDIN), tmp_path)
    assert json.loads((tmp_path / "probe.json").read_text()) == {"stdin_bytes": 0}
    assert report["left"] == len(_QUEUED)
    if mode != "long-task":
        assert json.loads(report["result"]) == {"stdin_bytes": 0}
    else:
        assert json.loads(report["result"])["status"] == "ok"


@pytest.mark.parametrize("mode", ["sync", "async", "long-task"])
def test_a_cli_prompt_fails_at_once(mode: str, tmp_path: Path) -> None:
    report, elapsed = _drive(mode, _fake_gco(tmp_path, _PROMPTS), tmp_path)
    assert report["left"] == len(_QUEUED)
    assert "timed out" not in report["result"] and "TimeoutError" not in report["result"]
    assert elapsed < 8
    if mode == "long-task":
        assert "exit_code=1" in report["result"]
    else:
        assert json.loads(report["result"])["exit_code"] == 1
        assert "Aborted!" in json.loads(report["result"])["error"]


# ---------------------------------------------------------------------------
# The kubectl-backed resources
# ---------------------------------------------------------------------------

_CONTEXT = "arn:aws:eks:us-east-1:123456789012:cluster/gco-us-east-1"


def _kubectl_ok(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args[0], 0, stdout='{"items": []}', stderr="")


@pytest.mark.parametrize(
    ("module", "call"),
    [
        ("resources.jobs", lambda m: m._job_resource_for_region("us-east-1", "train")),
        ("resources.cluster", lambda m: m._pending_pods("us-east-1")),
        (
            "resources.k8s",
            lambda m: m._k8s_live_resource_for_region("us-east-1", "gco-jobs", "job", "train"),
        ),
    ],
    ids=["jobs", "cluster", "k8s"],
)
def test_kubectl_resources_start_kubectl_without_the_server_stdin(module: str, call: Any) -> None:
    loaded = sys.modules[module]
    with (
        patch.object(loaded, "eks_context_for_region", return_value=_CONTEXT),
        patch.object(cli_runner.subprocess, "run", side_effect=_kubectl_ok) as run,
    ):
        call(loaded)
    assert run.call_args.args[0][0] == "kubectl"
    assert run.call_args.kwargs["stdin"] is subprocess.DEVNULL


# ---------------------------------------------------------------------------
# analytics_login_url without a password
# ---------------------------------------------------------------------------


def test_login_url_without_a_password_refuses_before_running_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GCO_STUDIO_PASSWORD", raising=False)
    with patch.object(cli_runner, "_run_cli") as run_cli:
        payload = json.loads(asyncio.run(run_mcp.analytics_login_url(username="alice")))
    run_cli.assert_not_called()
    assert payload["code"] == "password_required"
    assert "GCO_STUDIO_PASSWORD" in payload["error"]


@pytest.mark.parametrize(
    ("password", "server_env", "child_env"),
    [
        ("s3cret", None, {"GCO_STUDIO_PASSWORD": "s3cret"}),
        (None, "from-env", None),
    ],
    ids=["argument", "server-environment"],
)
def test_login_url_runs_the_cli_with_a_password(
    monkeypatch: pytest.MonkeyPatch,
    password: str | None,
    server_env: str | None,
    child_env: dict[str, str] | None,
) -> None:
    if server_env is None:
        monkeypatch.delenv("GCO_STUDIO_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("GCO_STUDIO_PASSWORD", server_env)
    with patch.object(cli_runner, "_run_cli", return_value='{"url": "https://example.com"}') as run:
        result = asyncio.run(run_mcp.analytics_login_url(username="alice", password=password))
    assert json.loads(result) == {"url": "https://example.com"}
    assert run.call_args.args == ("analytics", "studio", "login", "--username", "alice")
    assert run.call_args.kwargs == {"env": child_env}
