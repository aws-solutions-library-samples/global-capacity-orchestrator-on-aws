"""Contract guard: every MCP tool builds a ``gco`` invocation the real CLI parses.

Most MCP tools shell out through ``cli_runner._run_cli`` /
``cli_runner._run_cli_async`` or the long-task runner (``_run_long_task``).
Their own tests assert the argv a tool *constructs*, so a tool could emit a
subcommand, flag, positional or value the CLI rejects and every test stayed
green: ``nodepools_create_odcr`` passed options ``create-odcr`` does not have,
``templates_create`` passed the template name as the manifest path,
``analytics_user_add`` passed the username positionally, and
``files_access_points`` left out the file system ID.

A subprocess (``GCO_ENABLE_ALL_TOOLS`` set, so gated tools are registered too)
calls every tool whose body uses one of those runners with representative
arguments: every parameter set (booleans both ways), and only the required
ones. The runners are patched to record the argv and the ``env`` a tool hands
the child (secrets such as ``GCO_WEBHOOK_URL`` reach the CLI that way). The
parent then parses each argv the way ``gco --output json <argv>`` would, with
``make_context`` at every level of the real Click tree and the recorded
variables set, so unknown commands and options, missing required arguments
and options, extra arguments, invalid choices and missing files all fail.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Runs in a subprocess so import-time, flag-gated tool registration doesn't
# perturb the in-process FastMCP singleton the other MCP tests rely on. It
# calls every tool that uses a CLI runner and prints
# {tool_name: [{"runner", "argv", "env"} or {"runner": "error"|"none", ...}]}.
_DUMP_SNIPPET = r"""
import asyncio, contextlib, inspect, json, os, sys, types, typing
sys.path.insert(0, "gco_mcp")
import local_data

# Upload tools stage a descriptor-backed snapshot; hand the CLI the confined
# path instead so the parent can check it exists.
@contextlib.contextmanager
def _staged(contract):
    yield local_data.StagedUpload(argument=str(contract.resolved_path), directory_fd=0)

local_data.stage_upload_path = _staged
import cli_runner

_captured = []

def _record(runner, argv, env):
    _captured.append({"runner": runner, "argv": [str(a) for a in argv], "env": dict(env or {})})

def _fake_run_cli(*argv, env=None, **_options):
    _record("sync", argv, env)
    return "{}"

async def _fake_run_cli_async(*argv, **_options):
    _record("async", argv, None)
    return "{}"

cli_runner._run_cli = _fake_run_cli
cli_runner._run_cli_async = _fake_run_cli_async
import run_mcp

async def _fake_long_task(argv, **_options):
    _record("long", list(argv)[1:], None)  # drop the logical "gco"
    return "{}"

for _name, _module in list(sys.modules.items()):
    if _name.startswith("tools") and hasattr(_module, "_run_long_task"):
        _module._run_long_task = _fake_long_task

_ROOT = os.environ["GCO_STORAGE_LOCAL_ROOT"]
# Representative values: "tool.parameter" first, then the parameter name. A
# parameter the CLI constrains (a click.Choice, an existing path) needs one.
_VALUES = {
    "region": "us-east-1",
    "regions": ["us-east-1"],
    "namespace": "gco-jobs",
    "manifest_path": "examples/simple-job.yaml",
    "script_path": "README.md",
    "context": ".",
    "local_path": "weights.bin",
    "source_path": "weights.bin",
    "local_dir": "sync",
    "backend": "pcs",
    "fault_tolerance": "high",
    # ai-recommend refuses names that are not EC2 instance types or Regions.
    "ai_recommend.instance_type": ["p5.48xlarge"],
    "bedrock_region": "us-east-1",
    "configure_mooncake_store.offload": "cpu",
    "cost_k8s_top.by": "namespace",
    "cost_k8s_trend.granularity": "daily",
    "create_reservation.match_criteria": "open",
    "create_reservation.tenancy": "default",
    "deploy_disaggregated_inference.mooncake_mode": "disaggregated",
    "deploy_inference.framework": "vllm",
    # ``inference deploy -e`` takes KEY=VALUE entries.
    "env_vars": ["MODEL=example-model"],
    "files_get.fs_type": "efs",
    "find_training_plans.target": "training-job",
    "list_training_plans.status": "Active",
    "metrics_from_job_logs.aggregation": "last",
    "metrics_from_job_logs.regex": None,
    "queue_list.status": "queued",
    "set_codex_reasoning_effort.reasoning_effort": "high",
    "set_deployment_region.role": "global",
    "set_eks_endpoint_access.mode": "private",
    "webhooks_create.events": ["job.completed"],
}

# Variant 2 passes only the required parameters; these tools also need one
# optional setting to do anything at all.
_MINIMAL = {
    "configure_mooncake_store": {"enabled": True},
    "metrics_from_job_logs": {"json_key": "loss"},
}

def _literal(annotation):
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return list(typing.get_args(annotation))
    if origin in (typing.Union, types.UnionType):
        for arg in typing.get_args(annotation):
            found = _literal(arg)
            if found:
                return found
    return None

def _value(tool, name, annotation, variant):
    for key in (f"{tool}.{name}", name):
        if key in _VALUES:
            return _VALUES[key]
    literal = _literal(annotation)
    if literal:
        return literal[variant % len(literal)]
    text = str(annotation).lower()
    if "list" in text or "sequence" in text or "tuple" in text:
        return ["x"]
    if "dict" in text or "mapping" in text:
        return {"x": "y"}
    if "bool" in text:
        return variant == 0
    if "float" in text:
        return 1.0
    if "int" in text:
        return 1
    return "x"

class _Context:
    request_context = None

    def __getattr__(self, _name):
        async def _noop(*_args, **_kwargs):
            return None
        return _noop

def _uses_a_runner(fn):
    for candidate in {fn, inspect.unwrap(fn)}:
        try:
            source = inspect.getsource(candidate)
        except (OSError, TypeError):
            continue
        if "_run_cli" in source or "_run_long_task" in source:
            return True
    return False

result = {}
for tool in asyncio.run(run_mcp.mcp._list_tools()):
    fn = getattr(run_mcp, tool.name, None)
    if fn is None or not callable(fn) or not _uses_a_runner(fn):
        continue
    calls = []
    # 0 and 1: every parameter (booleans both ways); 2: required ones only.
    for variant in (0, 1, 2):
        _captured.clear()
        kwargs = {}
        for name, param in inspect.signature(fn).parameters.items():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            if name in ("ctx", "progress"):
                kwargs[name] = _Context()
            elif variant < 2 or param.default is inspect.Parameter.empty:
                kwargs[name] = _value(tool.name, name, param.annotation, variant)
        if variant == 2:
            kwargs.update(_MINIMAL.get(tool.name, {}))
        try:
            outcome = fn(**kwargs)
            if inspect.iscoroutine(outcome):
                outcome = asyncio.run(outcome)
        except Exception as exc:
            calls.append({"runner": "error", "detail": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        if not _captured:
            calls.append({"runner": "none", "detail": str(outcome)[:300]})
        calls.extend(_captured)
    result[tool.name] = calls
print(json.dumps(result))
"""


def _dump_tool_calls(local_root: Path) -> dict[str, list[dict[str, Any]]]:
    (local_root / "weights.bin").write_bytes(b"weights")
    (local_root / "sync").mkdir()
    env = dict(os.environ)
    env["GCO_ENABLE_ALL_TOOLS"] = "true"
    env["GCO_STORAGE_LOCAL_ROOT"] = str(local_root)
    env["GCO_METRICS_LOCAL_ROOT"] = str(local_root)
    env["GCO_DISABLE_TASK_STATUS"] = "1"
    # analytics_login_url without a password argument runs the CLI only when
    # the server has one to hand down (the child inherits it).
    env["GCO_STUDIO_PASSWORD"] = "contract-test"
    proc = subprocess.run(
        [sys.executable, "-c", _DUMP_SNIPPET],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return dict(json.loads(proc.stdout.strip().splitlines()[-1]))


def _parse_cli_argv(argv: list[str]) -> click.Context:
    """Parse one tool argv the way ``gco --output json <argv>`` would.

    Walks the subcommand path and builds each context with ``make_context``,
    so Click converts and validates every value (required options, choices,
    ``click.Path(exists=True)``, extra arguments) without running a command
    callback. Raises ``click.UsageError`` when the CLI would reject the argv.
    """
    from cli.main import cli

    ctx = cli.make_context("gco", ["--output", "json", *argv])
    node: click.Command = cli
    index = 0
    while isinstance(node, click.Group):
        if index >= len(argv):
            raise click.UsageError(f"incomplete command path {argv!r}", ctx=ctx)
        name = argv[index]
        sub = node.get_command(ctx, name)
        if sub is None:
            raise click.UsageError(f"no such command {name!r}", ctx=ctx)
        index += 1
        ctx = sub.make_context(name, argv[index:], parent=ctx)
        node = sub
    return ctx


def _argv_built_by(tool: object, *args: object, **kwargs: object) -> list[str]:
    """Run one MCP tool with ``cli_runner._run_cli`` captured; return its argv."""
    import asyncio
    import inspect
    from unittest.mock import patch

    sys.path.insert(0, str(PROJECT_ROOT / "gco_mcp"))
    import cli_runner

    captured: list[list[str]] = []

    def fake_run_cli(*argv: str, **_kwargs: object) -> str:
        captured.append(list(argv))
        return "{}"

    with patch.object(cli_runner, "_run_cli", fake_run_cli):
        result = tool(*args, **kwargs)  # type: ignore[operator]
        if inspect.iscoroutine(result):
            asyncio.run(result)
    assert len(captured) == 1, captured
    return captured[0]


_EXAMPLE_MANIFEST = str(PROJECT_ROOT / "examples" / "simple-job.yaml")


def test_templates_create_passes_the_manifest_and_name_where_the_cli_expects_them() -> None:
    """``gco templates create MANIFEST_PATH --name NAME``: the tool used to pass the
    template name as the manifest path, so every call failed."""
    sys.path.insert(0, str(PROJECT_ROOT / "gco_mcp"))
    import run_mcp

    argv = _argv_built_by(
        run_mcp.templates_create,
        "gpu-template",
        _EXAMPLE_MANIFEST,
        region="us-east-1",
        description="GPU training template",
    )

    params = _parse_cli_argv(argv).params
    assert params["manifest_path"] == _EXAMPLE_MANIFEST
    assert params["name"] == "gpu-template"
    assert params["region"] == "us-east-1"
    assert params["description"] == "GPU training template"


def test_analytics_user_add_passes_the_username_as_an_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``gco analytics users add --username NAME``: the tool used to pass the
    username positionally, which the command rejects."""
    monkeypatch.delenv("GCO_STUDIO_PASSWORD", raising=False)
    sys.path.insert(0, str(PROJECT_ROOT / "gco_mcp"))
    import run_mcp

    argv = _argv_built_by(run_mcp.analytics_user_add, "alice", "alice@example.com")

    params = _parse_cli_argv(argv).params
    assert params["username"] == "alice"
    assert params["email"] == "alice@example.com"
    assert params["password"] is None
    assert params["generate_password"] is False


def test_files_access_points_passes_the_file_system_id_and_required_region() -> None:
    """``gco files access-points FILE_SYSTEM_ID --region REGION``: the tool used to
    omit the file system ID and to make the (required) region optional."""
    import inspect

    sys.path.insert(0, str(PROJECT_ROOT / "gco_mcp"))
    import run_mcp

    argv = _argv_built_by(run_mcp.files_access_points, "fs-0123456789abcdef0", "us-east-1")

    params = _parse_cli_argv(argv).params
    assert params["file_system_id"] == "fs-0123456789abcdef0"
    assert params["region"] == "us-east-1"
    signature = inspect.signature(run_mcp.files_access_points)
    assert signature.parameters["file_system_id"].default is inspect.Parameter.empty
    assert signature.parameters["region"].default is inspect.Parameter.empty


def test_every_mcp_tool_argv_parses_against_the_cli(tmp_path: Path) -> None:
    from unittest.mock import patch

    calls = _dump_tool_calls(tmp_path)
    runners = {call["runner"] for tool_calls in calls.values() for call in tool_calls}
    # Sanity floor: the bulk of the catalog shells out, through all three
    # runners; far fewer means the harness is broken and the guard vacuous.
    assert len(calls) >= 140, f"only {len(calls)} tools use a CLI runner"
    assert {"sync", "async", "long"} <= runners, runners

    problems: list[str] = []
    parsed = 0
    for tool, tool_calls in sorted(calls.items()):
        for call in tool_calls:
            if call["runner"] in ("error", "none"):
                # A tool that raised or refused with representative arguments
                # was not checked; give it a representative value instead.
                problems.append(f"{tool}: did not call the CLI ({call['detail']})")
                continue
            try:
                with patch.dict(os.environ, call["env"]):
                    _parse_cli_argv(call["argv"])
                parsed += 1
            except click.UsageError as exc:
                problems.append(f"{tool}: {exc.format_message()}  (argv={call['argv']})")
    assert not problems, "MCP tools build CLI invocations the gco CLI rejects:\n  " + "\n  ".join(
        problems
    )
    assert parsed >= 3 * 140
