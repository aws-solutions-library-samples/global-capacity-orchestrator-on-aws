"""Residual runtime coverage for the non-Mission MCP server surface.

These tests exercise real fallback, cleanup, import, and audit contracts that
were missing from the archived PR coverage report.  They avoid AWS and process
side effects through narrow mocks and keep the shared MCP registry intact.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MCP_ROOT = PROJECT_ROOT / "gco_mcp"
sys.path.insert(0, str(MCP_ROOT))

import audit  # noqa: E402
import audit_middleware  # noqa: E402
import cli_runner  # noqa: E402
import completions  # noqa: E402
import iam  # noqa: E402
import run_mcp  # noqa: E402
import server  # noqa: E402


def test_audit_sanitizer_bounds_hostile_json_shapes() -> None:
    """Non-finite values, cycles, depth, keys, and item counts stay JSON-safe."""
    circular: dict[str, object] = {}
    circular["self"] = circular
    deeply_nested: object = "leaf"
    for _ in range(audit._MAX_AUDIT_DEPTH + 1):
        deeply_nested = {"child": deeply_nested}

    sanitized = audit._sanitize_arguments(
        {
            "non_finite": float("nan"),
            "cycle": circular,
            "deep": deeply_nested,
            "mapping": {object(): "value"},
            "long_list": list(range(audit._MAX_CONTAINER_ITEMS + 1)),
        }
    )

    encoded = json.dumps(sanitized, allow_nan=False)
    assert sanitized["non_finite"] == "<unserializable: float>"
    assert sanitized["cycle"]["self"] == audit._CIRCULAR_VALUE
    assert audit._MAX_DEPTH_VALUE in encoded
    assert sanitized["mapping"] == {"<key:object>": "value"}
    assert sanitized["long_list"][-1] == "<truncated-items:1>"


def test_audit_context_helpers_tolerate_absent_and_broken_contexts() -> None:
    """Audit enrichment remains best-effort outside a healthy MCP request."""

    class BrokenContext:
        @property
        def request_context(self) -> object:
            raise RuntimeError("request was torn down")

        @property
        def task_id(self) -> None:
            return None

        @property
        def client_id(self) -> None:
            return None

    with patch(
        "fastmcp_tasks.context.get_task_context",
        side_effect=RuntimeError("no task context"),
    ):
        assert audit._try_get_task_id(None) is None
        entry: dict[str, object] = {}
        audit._add_request_context_fields(entry, BrokenContext())

    assert entry == {}


@pytest.mark.asyncio
async def test_audit_context_spies_delegate_when_capture_is_absent_or_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Message and elicitation spies never block normal Context delegation."""
    delegated: list[tuple[str, str]] = []

    class FakeContext:
        async def warning(self, message: str, *_args: object, **_kwargs: object) -> str:
            delegated.append(("warning", message))
            return "warning-ok"

        async def info(self, message: str, *_args: object, **_kwargs: object) -> str:
            delegated.append(("info", message))
            return "info-ok"

        async def error(self, message: str, *_args: object, **_kwargs: object) -> str:
            delegated.append(("error", message))
            return "error-ok"

        async def elicit(self, message: str, *_args: object, **_kwargs: object) -> object:
            delegated.append(("elicit", message))
            return SimpleNamespace(action="decline", data=None)

    monkeypatch.setattr(audit_middleware, "Context", FakeContext)
    audit_middleware._install_context_patches()
    context = FakeContext()

    # Default ContextVar values are None: every wrapper takes its no-capture edge.
    assert await context.warning("w0") == "warning-ok"
    assert await context.info("i0") == "info-ok"
    assert await context.error("e0") == "error-ok"
    await context.elicit("q0")

    full_messages = [{} for _ in range(audit_middleware._MAX_CAPTURED_MESSAGES)]
    message_token = audit.audit_messages_var.set(full_messages)
    full_elicitations = [{} for _ in range(audit_middleware._MAX_CAPTURED_ELICITATIONS)]
    elicitation_token = audit.audit_elicitations_var.set(full_elicitations)
    try:
        await context.warning("w1")
        await context.info("i1")
        await context.error("e1")
        await context.elicit("q1")
    finally:
        audit.audit_messages_var.reset(message_token)
        audit.audit_elicitations_var.reset(elicitation_token)

    captured: list[dict[str, object]] = []
    elicitation_token = audit.audit_elicitations_var.set(captured)
    try:
        await context.elicit("q2")
    finally:
        audit.audit_elicitations_var.reset(elicitation_token)

    assert len(full_messages) == audit_middleware._MAX_CAPTURED_MESSAGES
    assert len(full_elicitations) == audit_middleware._MAX_CAPTURED_ELICITATIONS
    assert captured == [{"message": "q2", "action": "decline"}]
    assert delegated == [
        ("warning", "w0"),
        ("info", "i0"),
        ("error", "e0"),
        ("elicit", "q0"),
        ("warning", "w1"),
        ("info", "i1"),
        ("error", "e1"),
        ("elicit", "q1"),
        ("elicit", "q2"),
    ]


def test_project_root_resolution_honors_valid_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit existing project root wins over package and cwd discovery."""
    project = tmp_path / "explicit-project"
    project.mkdir()
    monkeypatch.setenv("GCO_PROJECT_ROOT", str(project))

    assert cli_runner._resolve_project_root() == project.resolve()


def test_project_root_resolution_warns_then_discovers_cwd_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An invalid override is ignored and a nested client cwd finds its checkout."""
    project = tmp_path / "checkout"
    nested = project / "a" / "b"
    nested.mkdir(parents=True)
    (project / "cdk.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GCO_PROJECT_ROOT", str(tmp_path / "missing"))
    monkeypatch.setattr(
        cli_runner, "__file__", str(tmp_path / "site" / "gco_mcp" / "cli_runner.py")
    )
    monkeypatch.setattr(cli_runner.Path, "cwd", classmethod(lambda _cls: nested))

    assert cli_runner._resolve_project_root() == project
    assert "is not a directory; ignoring it" in capsys.readouterr().err


def test_project_root_resolution_falls_back_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed layouts without cdk.json preserve the MCP client's cwd."""
    cwd = tmp_path / "plain-cwd"
    cwd.mkdir()
    monkeypatch.delenv("GCO_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(
        cli_runner, "__file__", str(tmp_path / "site" / "gco_mcp" / "cli_runner.py")
    )
    monkeypatch.setattr(cli_runner.Path, "cwd", classmethod(lambda _cls: cwd))

    assert cli_runner._resolve_project_root() == cwd


def test_sync_cli_runner_skips_flags_and_forwards_private_descriptors() -> None:
    """Literal flags bypass path validation while pass_fds reaches subprocess.run."""
    completed = SimpleNamespace(stdout="", stderr="", returncode=0)
    with (
        patch.object(cli_runner, "_gco_executable", return_value="/venv/bin/gco"),
        patch.object(cli_runner.subprocess, "run", return_value=completed) as run,
    ):
        result = cli_runner._run_cli("--literal-flag", "safe", pass_fds=(7,))

    assert json.loads(result) == {"status": "ok"}
    assert run.call_args.kwargs["pass_fds"] == (7,)
    assert run.call_args.kwargs["cwd"] == str(cli_runner.PROJECT_ROOT)


@pytest.mark.asyncio
async def test_async_cli_runner_skips_flag_validation() -> None:
    """Async wrappers treat dash-prefixed argv as literal flags, not paths."""

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b'{"ok": true}', b""

    with patch.object(
        cli_runner.asyncio,
        "create_subprocess_exec",
        new=AsyncMock(return_value=Process()),
    ):
        result = await cli_runner._run_cli_async("--literal-flag", "safe")

    assert json.loads(result) == {"ok": True}


@pytest.mark.asyncio
async def test_cli_stop_skips_kill_when_terminate_completes_process() -> None:
    """A grace timeout does not kill a process that exited after terminate."""

    class Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.released = asyncio.Event()
            self.terminated = False
            self.killed = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await self.released.wait()
            return b"", b""

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0
            self.released.set()

        def kill(self) -> None:
            self.killed = True

    process = Process()
    communication = asyncio.create_task(process.communicate())
    await cli_runner._stop_cli_process(process, communication, grace_seconds=0)  # type: ignore[arg-type]

    assert process.terminated is True
    assert process.killed is False


@pytest.mark.asyncio
async def test_completion_providers_cover_packages_and_defensive_topic_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Static completion candidates come from package and topic registries."""
    from mcp_types import CompletionArgument, ResourceTemplateReference
    from resources import docs as docs_resources

    package_result = await completions._complete_argument(
        ResourceTemplateReference(type="ref/resource", uri="docs://gco/packages/{package_name}"),
        CompletionArgument(name="package_name", value=""),
        None,
    )
    assert (
        package_result
        == sorted(docs_resources.PACKAGE_DOC_METADATA)[: completions._MAX_COMPLETIONS]
    )

    monkeypatch.setattr(
        docs_resources,
        "DOC_METADATA",
        {
            "valid": {"topics": ["gpu", 7]},
            "malformed": {"topics": "not-a-list"},
        },
    )
    topic_result = await completions._complete_argument(
        ResourceTemplateReference(type="ref/resource", uri="docs://gco/docs/by-topic/{topic}"),
        CompletionArgument(name="topic", value=""),
        None,
    )
    assert topic_result == ["7", "gpu"]


def test_role_assumption_logs_and_reraises_missing_boto3(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A configured role fails loudly and emits only sanitized dependency context."""
    role_arn = "arn:aws:iam::123456789012:role/gco-mcp"
    monkeypatch.setenv("GCO_MCP_ROLE_ARN", role_arn)
    caplog.set_level(logging.ERROR, logger="gco.mcp.audit")

    with patch.dict(sys.modules, {"boto3": None}), pytest.raises(ImportError):
        iam.assume_mcp_role()

    payload = json.loads(caplog.records[-1].message)
    assert payload["event"] == "mcp.server.role_assumption.error"
    assert payload["role_arn"] == role_arn
    assert payload["error"] == "boto3 is not installed; cannot assume role"
    assert "SecretAccessKey" not in caplog.text


def test_run_mcp_main_orders_runtime_initialization_before_server_run() -> None:
    """The executable entrypoint performs audit and IAM setup before serving."""
    calls: list[str] = []
    with (
        patch.object(run_mcp, "emit_startup_log", side_effect=lambda: calls.append("audit")),
        patch.object(run_mcp, "assume_mcp_role", side_effect=lambda: calls.append("iam")),
        patch.object(run_mcp.mcp, "run", side_effect=lambda: calls.append("serve")),
    ):
        run_mcp.main()

    assert calls == ["audit", "iam", "serve"]


def test_package_first_run_mcp_import_aliases_legacy_name_and_repairs_sys_path() -> None:
    """A package-first load shares one module and restores both runtime roots."""
    root_text = str(PROJECT_ROOT)
    mcp_text = str(MCP_ROOT)
    prior_path = list(sys.path)
    prior_legacy = sys.modules.pop("run_mcp", None)
    prior_package = sys.modules.pop("gco_mcp.run_mcp", None)
    try:
        sys.path[:] = [entry for entry in sys.path if entry not in {root_text, mcp_text}]
        spec = importlib.util.spec_from_file_location(
            "gco_mcp.run_mcp",
            MCP_ROOT / "run_mcp.py",
        )
        assert spec is not None and spec.loader is not None
        imported = importlib.util.module_from_spec(spec)
        sys.modules["gco_mcp.run_mcp"] = imported
        spec.loader.exec_module(imported)

        assert sys.modules["run_mcp"] is imported
        assert sys.modules["gco_mcp.run_mcp"] is imported
        assert root_text in sys.path
        assert mcp_text in sys.path
    finally:
        sys.modules.pop("run_mcp", None)
        sys.modules.pop("gco_mcp.run_mcp", None)
        if prior_legacy is not None:
            sys.modules["run_mcp"] = prior_legacy
        if prior_package is not None:
            sys.modules["gco_mcp.run_mcp"] = prior_package
        sys.path[:] = prior_path


def test_server_integer_env_falls_back_for_non_numeric_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed code-mode memory limits retain the bounded default."""
    monkeypatch.setenv("GCO_TEST_INT", "not-an-int")
    assert server._int_env("GCO_TEST_INT", 17) == 17
