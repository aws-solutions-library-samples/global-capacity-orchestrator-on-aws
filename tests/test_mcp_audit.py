"""
Tests for the MCP server's audit logging and argument sanitization.

Exercises gco_mcp/run_mcp.py's _sanitize_arguments helper that scrubs
incoming tool arguments before they hit the audit log: case-insensitive
redaction of keys containing token/secret/password/key, truncation of
string values larger than 1024 bytes with a [truncated] suffix, and
correct handling of non-string, None, and empty-dict inputs. Redaction
takes priority over truncation so sensitive values don't leak even
when they're oversize. A Hypothesis sweep rounds out the example-based
coverage.
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Ensure gco_mcp/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # noqa: E402


class TestSanitizeArguments:
    """Tests for _sanitize_arguments() function."""

    def test_passes_through_normal_args(self):
        args = {"region": "us-east-1", "name": "my-job"}
        result = run_mcp._sanitize_arguments(args)
        assert result == {"region": "us-east-1", "name": "my-job"}

    def test_redacts_token_key(self):
        args = {"auth_token": "super-secret-value", "name": "test"}
        result = run_mcp._sanitize_arguments(args)
        assert result["auth_token"] == "[REDACTED]"
        assert result["name"] == "test"

    def test_redacts_secret_key(self):
        args = {"client_secret": "abc123"}
        result = run_mcp._sanitize_arguments(args)
        assert result["client_secret"] == "[REDACTED]"

    def test_redacts_password_key(self):
        args = {"password": "hunter2", "db_password": "pass123"}
        result = run_mcp._sanitize_arguments(args)
        assert result["password"] == "[REDACTED]"
        assert result["db_password"] == "[REDACTED]"

    def test_redacts_key_key(self):
        args = {"api_key": "sk-12345", "access_key_id": "AKIA..."}
        result = run_mcp._sanitize_arguments(args)
        assert result["api_key"] == "[REDACTED]"
        assert result["access_key_id"] == "[REDACTED]"

    def test_redaction_is_case_insensitive(self):
        args = {"API_TOKEN": "val", "Secret": "val", "PASSWORD": "val"}
        result = run_mcp._sanitize_arguments(args)
        assert all(v == "[REDACTED]" for v in result.values())

    def test_truncates_large_string_values(self):
        large_value = "x" * 2000
        args = {"manifest": large_value}
        result = run_mcp._sanitize_arguments(args)
        assert result["manifest"].endswith("[truncated]")
        assert len(result["manifest"]) == 100 + len("[truncated]")

    def test_does_not_truncate_small_values(self):
        args = {"name": "short-value"}
        result = run_mcp._sanitize_arguments(args)
        assert result["name"] == "short-value"

    def test_truncates_at_1kb_boundary(self):
        # Exactly 1024 bytes should NOT be truncated
        value_1024 = "a" * 1024
        result = run_mcp._sanitize_arguments({"data": value_1024})
        assert result["data"] == value_1024

        # 1025 bytes should be truncated
        value_1025 = "a" * 1025
        result = run_mcp._sanitize_arguments({"data": value_1025})
        assert result["data"].endswith("[truncated]")

    def test_redaction_takes_priority_over_truncation(self):
        # A sensitive key with a large value should be redacted, not truncated
        args = {"secret_token": "x" * 2000}
        result = run_mcp._sanitize_arguments(args)
        assert result["secret_token"] == "[REDACTED]"

    def test_handles_non_string_values(self):
        args = {"count": 42, "flag": True, "items": [1, 2, 3]}
        result = run_mcp._sanitize_arguments(args)
        assert result["count"] == 42
        assert result["flag"] is True
        assert result["items"] == [1, 2, 3]

    def test_handles_empty_dict(self):
        result = run_mcp._sanitize_arguments({})
        assert result == {}

    def test_handles_none_values(self):
        args = {"region": None}
        result = run_mcp._sanitize_arguments(args)
        assert result["region"] is None

    def test_unserializable_value_replaced_with_type_placeholder(self):
        """FastMCP injects ``Context`` and ``Progress`` dependencies as
        keyword arguments on every long-running tool. Those objects aren't
        JSON-serializable, so without this guard the audit decorator raises
        ``TypeError: Object of type Context is not JSON serializable``
        before the tool ever runs. Replacing the value with a type-only
        placeholder lets the audit log keep its shape and the tool keep
        running."""

        class _UnserializableContext:
            pass

        args = {"region": "us-east-1", "ctx": _UnserializableContext()}
        result = run_mcp._sanitize_arguments(args)
        assert result["region"] == "us-east-1"
        assert result["ctx"] == "<unserializable: _UnserializableContext>"

        # The whole sanitized dict must round-trip through json.dumps now.
        json.dumps(result)

    def test_unserializable_object_with_sensitive_key_still_redacted(self):
        """Redaction takes priority over the unserializable-fallback so
        objects sitting under sensitive key names never leak even a type
        name."""

        class _Token:
            pass

        args = {"auth_token": _Token()}
        result = run_mcp._sanitize_arguments(args)
        assert result["auth_token"] == "[REDACTED]"
        json.dumps(result)

    def test_recursively_redacts_nested_mapping_keys(self):
        args = {
            "payload": {
                "credentials": {"api_token": "nested-secret", "region": "us-east-1"},
                "items": [{"db_password": "also-secret"}],
            }
        }

        result = run_mcp._sanitize_arguments(args)

        assert result["payload"]["credentials"]["api_token"] == "[REDACTED]"
        assert result["payload"]["credentials"]["region"] == "us-east-1"
        assert result["payload"]["items"][0]["db_password"] == "[REDACTED]"
        assert "nested-secret" not in json.dumps(result)
        assert "also-secret" not in json.dumps(result)

    def test_circular_and_oversized_containers_are_bounded(self):
        circular: list[object] = []
        circular.append(circular)
        oversized = {f"field-{index}": index for index in range(150)}

        result = run_mcp._sanitize_arguments({"circular": circular, "oversized": oversized})

        assert result["circular"] == ["<circular-reference>"]
        assert result["oversized"]["<truncated-items>"] == 50
        assert len(result["oversized"]) == 101
        json.dumps(result)


class TestAuditLoggedDecorator:
    """Tests for the audit_logged decorator."""

    def test_logs_successful_invocation(self, caplog):
        @run_mcp.audit_logged
        def sample_tool(name: str = "test") -> str:
            return "result"

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            result = sample_tool(name="test")

        assert result == "result"
        # Find the audit log entry
        audit_records = [r for r in caplog.records if r.name == "gco.mcp.audit"]
        assert len(audit_records) == 1
        entry = json.loads(audit_records[0].message)
        assert entry["event"] == "mcp.tool.invocation"
        assert entry["tool"] == "sample_tool"
        assert entry["status"] == "success"
        assert "duration_ms" in entry
        assert "timestamp" in entry
        assert entry["arguments"] == {"name": "test"}

    def test_logs_failed_invocation(self, caplog):
        @run_mcp.audit_logged
        def failing_tool(name: str = "test") -> str:
            raise ValueError("something went wrong")

        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            pytest.raises(ValueError, match="something went wrong"),
        ):
            failing_tool(name="test")

        audit_records = [r for r in caplog.records if r.name == "gco.mcp.audit"]
        assert len(audit_records) == 1
        entry = json.loads(audit_records[0].message)
        assert entry["status"] == "error"
        assert "something went wrong" in entry["error"]
        assert entry["tool"] == "failing_tool"

    def test_preserves_function_name(self):
        @run_mcp.audit_logged
        def my_tool() -> str:
            return "ok"

        assert my_tool.__name__ == "my_tool"

    def test_preserves_function_docstring(self):
        @run_mcp.audit_logged
        def my_tool() -> str:
            """My tool docstring."""
            return "ok"

        assert my_tool.__doc__ == "My tool docstring."

    def test_sanitizes_arguments_in_log(self, caplog):
        @run_mcp.audit_logged
        def tool_with_secret(api_token: str = "", name: str = "") -> str:
            return "ok"

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            tool_with_secret(api_token="secret-value", name="visible")

        audit_records = [r for r in caplog.records if r.name == "gco.mcp.audit"]
        entry = json.loads(audit_records[0].message)
        assert entry["arguments"]["api_token"] == "[REDACTED]"
        assert entry["arguments"]["name"] == "visible"

    def test_duration_is_positive(self, caplog):
        @run_mcp.audit_logged
        def slow_tool() -> str:
            return "ok"

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            slow_tool()

        audit_records = [r for r in caplog.records if r.name == "gco.mcp.audit"]
        entry = json.loads(audit_records[0].message)
        assert entry["duration_ms"] >= 0

    def test_timestamp_is_iso8601(self, caplog):
        @run_mcp.audit_logged
        def my_tool() -> str:
            return "ok"

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            my_tool()

        audit_records = [r for r in caplog.records if r.name == "gco.mcp.audit"]
        entry = json.loads(audit_records[0].message)
        # ISO 8601 timestamps contain 'T' and end with timezone info
        assert "T" in entry["timestamp"]

    def test_error_message_truncated_to_200_chars(self, caplog):
        long_error = "x" * 500

        @run_mcp.audit_logged
        def failing_tool() -> str:
            raise RuntimeError(long_error)

        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            pytest.raises(RuntimeError),
        ):
            failing_tool()

        audit_records = [r for r in caplog.records if r.name == "gco.mcp.audit"]
        entry = json.loads(audit_records[0].message)
        assert len(entry["error"]) <= 200


class TestAuditLoggedOnRealTools:
    """Tests that audit_logged works correctly on actual MCP tool functions."""

    def test_list_jobs_is_audit_logged(self, caplog):
        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch("cli_runner.subprocess.run") as mock,
        ):
            mock.return_value = MagicMock(returncode=0, stdout='{"jobs":[]}', stderr="")
            run_mcp.list_jobs(region="us-east-1")

        audit_records = [r for r in caplog.records if r.name == "gco.mcp.audit"]
        assert len(audit_records) == 1
        entry = json.loads(audit_records[0].message)
        assert entry["tool"] == "list_jobs"
        assert entry["status"] == "success"

    def test_get_model_uri_is_audit_logged(self, caplog):
        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch("cli_runner.subprocess.run") as mock,
        ):
            mock.return_value = MagicMock(returncode=0, stdout="s3://bucket/model", stderr="")
            run_mcp.get_model_uri(model_name="llama3")

        audit_records = [r for r in caplog.records if r.name == "gco.mcp.audit"]
        assert len(audit_records) == 1
        entry = json.loads(audit_records[0].message)
        assert entry["tool"] == "get_model_uri"


class TestStartupLog:
    """Tests for the startup audit log entry."""

    def test_startup_log_fields(self, caplog):
        # Startup logging is explicit at server runtime, not an import side effect.
        # The constants remain importable for version and logger assertions.
        # ``_MCP_SERVER_VERSION`` tracks the project-wide ``VERSION`` file via
        # ``gco._version.__version__`` — assert the server mirrors the project
        # version exactly rather than hardcoding a literal here, which would
        # drift on every release and mask the real intent of this check.
        from gco._version import __version__ as project_version

        assert project_version == run_mcp._MCP_SERVER_VERSION
        assert run_mcp.audit_logger.name == "gco.mcp.audit"


# =============================================================================
# Property-Based Tests (Hypothesis)
# =============================================================================

# Property: MCP Audit Log Completeness

# Strategy: valid Python identifiers for tool names
_tool_name_strategy = st.from_regex(r"[a-z][a-z0-9_]{0,29}", fullmatch=True)

# Strategy: argument dicts with random string keys and values
_arg_dict_strategy = st.dictionaries(
    keys=st.from_regex(r"[a-z][a-z0-9_]{0,19}", fullmatch=True),
    values=st.text(min_size=0, max_size=200),
    min_size=0,
    max_size=10,
)


class _AuditLogCapture(logging.Handler):
    """A logging handler that captures audit log records for property testing."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def clear(self):
        self.records.clear()


class TestMcpAuditLogCompletenessProperty:
    """Property test: every MCP tool invocation produces a complete audit log entry."""

    REQUIRED_FIELDS = {"event", "tool", "arguments", "status", "duration_ms", "timestamp"}

    @settings(max_examples=100)
    @given(tool_name=_tool_name_strategy, arg_dict=_arg_dict_strategy)
    def test_successful_invocation_has_all_required_fields(self, tool_name, arg_dict):
        """For any successful tool invocation, the audit log entry contains all required fields."""

        # Dynamically create a decorated function with the given tool name
        def tool_func(**kwargs):
            return "ok"

        tool_func.__name__ = tool_name
        tool_func.__qualname__ = tool_name
        decorated = run_mcp.audit_logged(tool_func)

        handler = _AuditLogCapture()
        handler.setLevel(logging.INFO)
        logger = logging.getLogger("gco.mcp.audit")
        original_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            handler.clear()
            decorated(**arg_dict)

            audit_records = [r for r in handler.records if r.name == "gco.mcp.audit"]
            assert len(audit_records) == 1, f"Expected 1 audit record, got {len(audit_records)}"

            entry = json.loads(audit_records[0].message)

            # Verify all required fields are present
            missing = self.REQUIRED_FIELDS - set(entry.keys())
            assert not missing, f"Missing required fields: {missing}"

            # Verify field values are correct types and non-trivial
            assert entry["event"] == "mcp.tool.invocation"
            assert entry["tool"] == tool_name
            assert isinstance(entry["arguments"], dict)
            assert entry["status"] == "success"
            assert isinstance(entry["duration_ms"], (int, float))
            assert entry["duration_ms"] >= 0
            # Verify timestamp is a valid ISO 8601 string
            assert isinstance(entry["timestamp"], str)
            datetime.fromisoformat(entry["timestamp"])
        finally:
            logger.removeHandler(handler)
            logger.setLevel(original_level)

    @settings(max_examples=100)
    @given(tool_name=_tool_name_strategy, arg_dict=_arg_dict_strategy)
    def test_failed_invocation_has_all_required_fields(self, tool_name, arg_dict):
        """For any failed tool invocation, the audit log entry contains all required fields."""

        def tool_func(**kwargs):
            raise RuntimeError("simulated failure")

        tool_func.__name__ = tool_name
        tool_func.__qualname__ = tool_name
        decorated = run_mcp.audit_logged(tool_func)

        handler = _AuditLogCapture()
        handler.setLevel(logging.INFO)
        logger = logging.getLogger("gco.mcp.audit")
        original_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            handler.clear()
            with pytest.raises(RuntimeError, match="simulated failure"):
                decorated(**arg_dict)

            audit_records = [r for r in handler.records if r.name == "gco.mcp.audit"]
            assert len(audit_records) == 1, f"Expected 1 audit record, got {len(audit_records)}"

            entry = json.loads(audit_records[0].message)

            # Verify all required fields are present
            missing = self.REQUIRED_FIELDS - set(entry.keys())
            assert not missing, f"Missing required fields: {missing}"

            # Verify field values
            assert entry["event"] == "mcp.tool.invocation"
            assert entry["tool"] == tool_name
            assert isinstance(entry["arguments"], dict)
            assert entry["status"] == "error"
            assert isinstance(entry["duration_ms"], (int, float))
            assert entry["duration_ms"] >= 0
            assert isinstance(entry["timestamp"], str)
            datetime.fromisoformat(entry["timestamp"])
            # Failed invocations should also have an error field
            assert "error" in entry
        finally:
            logger.removeHandler(handler)
            logger.setLevel(original_level)


# Property: MCP Audit Log Sanitization

# Strategy: sensitive key names that must always trigger redaction
_sensitive_key_prefixes = st.sampled_from(["token", "secret", "password", "key"])
_sensitive_key_strategy = st.builds(
    lambda prefix, suffix: f"{suffix}{prefix}{suffix}",
    prefix=_sensitive_key_prefixes,
    suffix=st.from_regex(r"[a-z_]{0,10}", fullmatch=True),
)

# Strategy: large string values that exceed 1KB (1024 bytes)
_large_value_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "S", "Z")),
    min_size=1025,
    max_size=5000,
)


class TestMcpAuditLogSanitizationProperty:
    """Property test: sensitive values are always redacted and large values are always truncated."""

    @settings(max_examples=100)
    @given(
        sensitive_key=_sensitive_key_strategy,
        sensitive_value=st.text(min_size=1, max_size=200),
    )
    def test_sensitive_keys_are_always_redacted(self, sensitive_key, sensitive_value):
        """For any argument whose key matches a sensitive pattern, the value is redacted."""
        args = {sensitive_key: sensitive_value}
        result = run_mcp._sanitize_arguments(args)

        # The sanitized value must be "[REDACTED]"
        assert result[sensitive_key] == "[REDACTED]", (
            f"Key '{sensitive_key}' with value '{sensitive_value}' was not redacted"
        )
        # Verify the original value doesn't appear as any dict value in the result
        for v in result.values():
            if isinstance(v, str) and v != "[REDACTED]":
                assert sensitive_value != v, (
                    f"Original sensitive value leaked as a dict value for key '{sensitive_key}'"
                )

    @settings(max_examples=100)
    @given(
        normal_key=st.from_regex(
            r"(region|name|manifest|data|payload|content|body|config|path|file)[0-9]{0,3}",
            fullmatch=True,
        ),
        large_value=_large_value_strategy,
    )
    def test_large_values_are_always_truncated(self, normal_key, large_value):
        """For any non-sensitive argument with a value > 1KB, the value is truncated."""
        args = {normal_key: large_value}
        result = run_mcp._sanitize_arguments(args)

        sanitized_val = result[normal_key]
        # Must end with "[truncated]"
        assert isinstance(sanitized_val, str), (
            f"Expected string result for truncated value, got {type(sanitized_val)}"
        )
        assert sanitized_val.endswith("[truncated]"), (
            f"Large value for key '{normal_key}' (len={len(large_value)}) was not truncated"
        )
        # Truncated output should be first 100 chars + "[truncated]"
        assert len(sanitized_val) == 100 + len("[truncated]"), (
            f"Truncated value has unexpected length: {len(sanitized_val)}"
        )
        # The full original value must not appear in the output
        assert large_value not in sanitized_val, (
            "Full original large value leaked in sanitized output"
        )

    @settings(max_examples=100)
    @given(
        sensitive_key=_sensitive_key_strategy,
        large_sensitive_value=_large_value_strategy,
    )
    def test_sensitive_large_values_are_redacted_not_truncated(
        self, sensitive_key, large_sensitive_value
    ):
        """For sensitive keys with large values, redaction takes priority over truncation."""
        args = {sensitive_key: large_sensitive_value}
        result = run_mcp._sanitize_arguments(args)

        # Must be redacted, not truncated
        assert result[sensitive_key] == "[REDACTED]", (
            f"Sensitive key '{sensitive_key}' with large value was not redacted"
        )
        # Original value must not appear anywhere
        sanitized_str = json.dumps(result)
        assert large_sensitive_value not in sanitized_str, (
            "Original large sensitive value leaked in sanitized output"
        )


# =============================================================================
# Async wrapper + Context-aware audit entry tests (Tasks 3.1–3.4)
# =============================================================================

import asyncio  # noqa: E402

import audit  # noqa: E402
from audit_middleware import AuditCaptureMiddleware  # noqa: E402
from fastmcp import Client, FastMCP  # noqa: E402
from fastmcp.client.elicitation import ElicitResult  # noqa: E402
from fastmcp.server.dependencies import get_context  # noqa: E402


def _audit_entries(caplog) -> list[dict]:
    """Pull the JSON audit entries out of caplog, in order."""
    return [json.loads(r.message) for r in caplog.records if r.name == "gco.mcp.audit"]


def _last_invocation_entry(caplog) -> dict:
    """Return the last `mcp.tool.invocation` entry from caplog."""
    invocations = [e for e in _audit_entries(caplog) if e.get("event") == "mcp.tool.invocation"]
    assert invocations, "no tool-invocation audit entries captured"
    return invocations[-1]


class TestAsyncAuditDecorator:
    """audit_logged dispatches on asyncio.iscoroutinefunction."""

    def test_async_tool_success_emits_entry(self, caplog):
        """An async tool returning JSON-string emits a success entry.

        Direct-invocation path (no active FastMCP request), so request_id is
        not asserted here — that's covered by the Client round-trip tests
        below. The point of this test is the dispatch on
        ``asyncio.iscoroutinefunction``.
        """

        @run_mcp.audit_logged
        async def async_tool(name: str = "test") -> str:
            return '{"ok": true}'

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            result = asyncio.run(async_tool(name="test"))

        assert result == '{"ok": true}'
        entry = _last_invocation_entry(caplog)
        assert entry["status"] == "success"
        assert entry["tool"] == "async_tool"
        assert entry["arguments"] == {"name": "test"}
        assert "duration_ms" in entry
        assert entry["duration_ms"] >= 0
        assert "timestamp" in entry
        # No active MCP request → no request_id field.
        assert "request_id" not in entry

    def test_async_tool_error_emits_entry(self, caplog):
        """An async tool that raises emits an error entry with truncated text."""

        long_msg = "boom " * 100  # ~500 chars

        @run_mcp.audit_logged
        async def async_failing_tool() -> str:
            raise RuntimeError(long_msg)

        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            pytest.raises(RuntimeError),
        ):
            asyncio.run(async_failing_tool())

        entry = _last_invocation_entry(caplog)
        assert entry["status"] == "error"
        assert entry["tool"] == "async_failing_tool"
        assert "error" in entry
        # Error text capped at 200 chars per audit decorator contract.
        assert len(entry["error"]) <= 200

    def test_audit_omits_request_id_when_context_absent(self, caplog):
        """No active MCP request → no request_id, client_id, or task_id."""

        @run_mcp.audit_logged
        async def standalone_tool() -> str:
            return "ok"

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            asyncio.run(standalone_tool())

        entry = _last_invocation_entry(caplog)
        assert "request_id" not in entry
        assert "client_id" not in entry
        assert "task_id" not in entry
        # And no Context-driven capture buffers either.
        assert "client_messages" not in entry
        assert "elicitations" not in entry


class TestAuditContextCapture:
    """End-to-end Client round-trip captures Context messages, elicitations, task_id."""

    @pytest.mark.asyncio
    async def test_audit_captures_client_warning(self, caplog):
        """ctx.warning() during a tool call lands in client_messages."""

        test_mcp = FastMCP("audit-test-warning")
        test_mcp.add_middleware(AuditCaptureMiddleware())

        @test_mcp.tool
        @run_mcp.audit_logged
        async def warner() -> str:
            """Tool that emits a warning."""
            ctx = get_context()
            await ctx.warning("hello world")
            await ctx.info("an info line")
            return "ok"

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            async with Client(test_mcp) as client:
                await client.call_tool("warner", {})

        entry = _last_invocation_entry(caplog)
        assert entry["status"] == "success"
        assert entry["tool"] == "warner"
        # request_id is set by FastMCP's session for every request.
        assert "request_id" in entry
        assert "client_messages" in entry
        msgs = entry["client_messages"]
        assert any(
            m.get("level") == "warning" and m.get("message") == "hello world" for m in msgs
        ), msgs
        assert any(m.get("level") == "info" and m.get("message") == "an info line" for m in msgs), (
            msgs
        )

    @pytest.mark.asyncio
    async def test_audit_captures_client_error(self, caplog):
        """ctx.error() during a tool call lands in client_messages.

        Exercises the ``_spy_error`` wrapper installed by
        ``audit_middleware._install_context_patches``: the message must
        be appended to the active capture buffer with ``level=error``
        before the original ``Context.error`` runs.
        """

        test_mcp = FastMCP("audit-test-error")
        test_mcp.add_middleware(AuditCaptureMiddleware())

        @test_mcp.tool
        @run_mcp.audit_logged
        async def errorer() -> str:
            """Tool that emits an error log line."""
            ctx = get_context()
            await ctx.error("something broke")
            return "ok"

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            async with Client(test_mcp) as client:
                await client.call_tool("errorer", {})

        entry = _last_invocation_entry(caplog)
        assert entry["status"] == "success"
        assert entry["tool"] == "errorer"
        msgs = entry["client_messages"]
        assert any(
            m.get("level") == "error" and m.get("message") == "something broke" for m in msgs
        ), msgs

    @pytest.mark.asyncio
    async def test_audit_captures_elicitation_response(self, caplog):
        """ctx.elicit() returning accept lands in the elicitations field."""

        test_mcp = FastMCP("audit-test-elicit")
        test_mcp.add_middleware(AuditCaptureMiddleware())

        @test_mcp.tool
        @run_mcp.audit_logged
        async def elicitor() -> str:
            """Tool that elicits user confirmation."""
            ctx = get_context()
            res = await ctx.elicit("Please confirm", response_type=str)
            return f"action={getattr(res, 'action', 'unknown')}"

        async def handler(message, response_type, params, context):
            return ElicitResult(action="accept", content="yes")

        # ``ctx.elicit()`` is era-gated in FastMCP 4: it works on
        # handshake-era connections and raises on the sessionless
        # 2026-07-28 protocol (which mode="auto" clients negotiate).
        # Pin the client to the legacy era so the middleware's elicit
        # capture path stays exercisable.
        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            async with Client(test_mcp, elicitation_handler=handler, mode="legacy") as client:
                await client.call_tool("elicitor", {})

        entry = _last_invocation_entry(caplog)
        assert entry["status"] == "success"
        assert "elicitations" in entry
        elics = entry["elicitations"]
        assert len(elics) == 1
        assert elics[0]["action"] == "accept"
        # Message text is preserved.
        assert elics[0].get("message") == "Please confirm"

    def test_audit_includes_supported_task_context_id(self, caplog, monkeypatch):
        """The tasks-extension task context is the primary task-ID source."""

        fake_ctx = MagicMock()
        fake_ctx.request_context = MagicMock()
        fake_ctx.request_context.meta.task_id = "legacy-metadata-id"
        fake_ctx.task_id = "context-property-id"
        fake_ctx.request_id = "fake-req-id"
        fake_ctx.client_id = None
        task_context = MagicMock(spec=["task_id"])
        task_context.task_id = "supported-task-123"

        monkeypatch.setattr(audit, "_try_get_fastmcp_context", lambda: fake_ctx)

        @run_mcp.audit_logged
        async def task_tool() -> str:
            return "ok"

        with (
            patch(
                "fastmcp_tasks.context.get_task_context",
                return_value=task_context,
            ),
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
        ):
            asyncio.run(task_tool())

        entry = _last_invocation_entry(caplog)
        assert entry["task_id"] == "supported-task-123"
        assert entry["request_id"] == "fake-req-id"
        assert "client_id" not in entry

    def test_audit_task_id_falls_back_and_is_bounded(self, monkeypatch):
        fake_ctx = MagicMock()
        fake_ctx.task_id = "x" * 2_000
        fake_ctx.request_context = None
        with patch(
            "fastmcp_tasks.context.get_task_context",
            side_effect=RuntimeError("no task context"),
        ):
            task_id = audit._try_get_task_id(fake_ctx)

        assert task_id is not None
        assert task_id.endswith("[truncated]")
        assert len(task_id.encode("utf-8")) <= audit._MAX_TASK_ID_BYTES


class TestAuditResourceReads:
    """Central middleware audits static and templated resource reads."""

    @pytest.mark.asyncio
    async def test_static_resource_read_emits_success_entry(self, caplog):
        test_mcp = FastMCP("audit-test-resource")
        test_mcp.add_middleware(AuditCaptureMiddleware())

        @test_mcp.resource("audit://gco/static")
        def static_resource() -> str:
            return "resource body"

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            async with Client(test_mcp) as client:
                blocks = await client.read_resource("audit://gco/static")

        assert blocks[0].text == "resource body"
        entries = [
            entry for entry in _audit_entries(caplog) if entry.get("event") == "mcp.resource.read"
        ]
        assert len(entries) == 1
        assert entries[0]["resource_uri"] == "audit://gco/static"
        assert entries[0]["status"] == "success"
        assert entries[0]["duration_ms"] >= 0
        assert "timestamp" in entries[0]
        assert "request_id" in entries[0]


# =============================================================================
# Startup-log tests for the new all_tools_enabled / tool_search fields (3.7)
# =============================================================================


def _last_startup_entry(caplog) -> dict:
    """Return the last `mcp.server.startup` entry from caplog."""
    startups = [e for e in _audit_entries(caplog) if e.get("event") == "mcp.server.startup"]
    assert startups, "no startup audit entries captured"
    return startups[-1]


class TestStartupLogNewFields:
    """emit_startup_log() conditionally emits all_tools_enabled and tool_search."""

    def test_emit_startup_log_includes_all_tools_enabled_when_set(self, caplog, monkeypatch):
        monkeypatch.setenv("GCO_ENABLE_ALL_TOOLS", "true")
        monkeypatch.delenv("GCO_MCP_TOOL_SEARCH", raising=False)

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            run_mcp.emit_startup_log()

        entry = _last_startup_entry(caplog)
        assert entry["all_tools_enabled"] is True

    @pytest.mark.parametrize(
        "value",
        ["bm25", "regex", "code_mode", "off"],
    )
    def test_emit_startup_log_includes_tool_search_when_set(self, caplog, monkeypatch, value):
        monkeypatch.delenv("GCO_ENABLE_ALL_TOOLS", raising=False)
        monkeypatch.setenv("GCO_MCP_TOOL_SEARCH", value)

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            run_mcp.emit_startup_log()

        entry = _last_startup_entry(caplog)
        assert entry["tool_search"] == value
        if value == "code_mode":
            # Experimental-tier API path → flag for audit consumers.
            assert entry["code_mode_experimental"] is True
        else:
            assert "code_mode_experimental" not in entry

    def test_emit_startup_log_omits_optional_fields_by_default(self, caplog, monkeypatch):
        monkeypatch.setenv("GCO_ENABLE_ALL_TOOLS", "")
        monkeypatch.delenv("GCO_MCP_TOOL_SEARCH", raising=False)

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            run_mcp.emit_startup_log()

        entry = _last_startup_entry(caplog)
        # all_tools_enabled is omitted when the umbrella flag is unset.
        assert "all_tools_enabled" not in entry
        # tool_search is ALWAYS emitted — the resolver falls back to "bm25"
        # when the env var is unset, mirroring gco_mcp/server.py's wiring.
        assert entry["tool_search"] == "bm25"
        assert "code_mode_experimental" not in entry

    def test_emit_startup_log_unknown_tool_search_falls_back_to_bm25(self, caplog, monkeypatch):
        monkeypatch.delenv("GCO_ENABLE_ALL_TOOLS", raising=False)
        monkeypatch.setenv("GCO_MCP_TOOL_SEARCH", "banana")

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            run_mcp.emit_startup_log()

        entry = _last_startup_entry(caplog)
        assert entry["tool_search"] == "bm25"
        assert "code_mode_experimental" not in entry

    def test_emit_startup_log_includes_mission_enabled_when_set(self, caplog, monkeypatch):
        """GCO_ENABLE_MISSION=true → mission_enabled: true in the startup entry.

        Mirrors the all_tools_enabled / tool_search convention: the field is
        only emitted when the gating flag is on. Audit consumers can use
        this to detect whether the Mission loop is active for a given run
        without inspecting env vars.
        """
        monkeypatch.delenv("GCO_ENABLE_ALL_TOOLS", raising=False)
        monkeypatch.delenv("GCO_MCP_TOOL_SEARCH", raising=False)
        monkeypatch.setenv("GCO_ENABLE_MISSION", "true")

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            run_mcp.emit_startup_log()

        entry = _last_startup_entry(caplog)
        assert entry["mission_enabled"] is True

    def test_emit_startup_log_includes_mission_enabled_via_umbrella(self, caplog, monkeypatch):
        """GCO_ENABLE_ALL_TOOLS=true also flips mission_enabled on.

        Per the established feature_flags.is_enabled semantics, the umbrella
        flag enables every per-tool flag — the Mission flag included.
        """
        monkeypatch.delenv("GCO_ENABLE_MISSION", raising=False)
        monkeypatch.delenv("GCO_MCP_TOOL_SEARCH", raising=False)
        monkeypatch.setenv("GCO_ENABLE_ALL_TOOLS", "true")

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            run_mcp.emit_startup_log()

        entry = _last_startup_entry(caplog)
        assert entry["mission_enabled"] is True

    def test_emit_startup_log_omits_mission_enabled_when_unset(self, caplog, monkeypatch):
        """GCO_ENABLE_MISSION unset → field omitted, matching all_tools_enabled."""
        monkeypatch.delenv("GCO_ENABLE_ALL_TOOLS", raising=False)
        monkeypatch.delenv("GCO_ENABLE_MISSION", raising=False)
        monkeypatch.delenv("GCO_MCP_TOOL_SEARCH", raising=False)

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            run_mcp.emit_startup_log()

        entry = _last_startup_entry(caplog)
        assert "mission_enabled" not in entry

    def test_emit_startup_log_lists_enabled_per_tool_flags(self, caplog, monkeypatch):
        """enabled_flags lists every effectively-enabled per-tool flag, sorted.

        Two unrelated per-tool flags are set; both must appear (sorted) so an
        audit consumer sees the full gated-tool surface for the run from a
        single line. Every other ALL_FLAGS entry is cleared first so the
        assertion is exact regardless of the runner's shell.
        """
        monkeypatch.delenv("GCO_ENABLE_ALL_TOOLS", raising=False)
        monkeypatch.delenv("GCO_MCP_TOOL_SEARCH", raising=False)
        for flag in audit.feature_flags.ALL_FLAGS:
            monkeypatch.delenv(flag, raising=False)
        monkeypatch.setenv("GCO_ENABLE_DESTRUCTIVE_OPERATIONS", "true")
        monkeypatch.setenv("GCO_ENABLE_IMAGE_PUBLISH", "true")

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            run_mcp.emit_startup_log()

        entry = _last_startup_entry(caplog)
        assert entry["enabled_flags"] == [
            "GCO_ENABLE_DESTRUCTIVE_OPERATIONS",
            "GCO_ENABLE_IMAGE_PUBLISH",
        ]

    def test_emit_startup_log_omits_enabled_flags_when_none_set(self, caplog, monkeypatch):
        """enabled_flags is omitted entirely when no per-tool flag is enabled."""
        monkeypatch.delenv("GCO_ENABLE_ALL_TOOLS", raising=False)
        monkeypatch.delenv("GCO_MCP_TOOL_SEARCH", raising=False)
        for flag in audit.feature_flags.ALL_FLAGS:
            monkeypatch.delenv(flag, raising=False)

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            run_mcp.emit_startup_log()

        entry = _last_startup_entry(caplog)
        assert "enabled_flags" not in entry

    def test_emit_startup_log_enabled_flags_under_umbrella_lists_all(self, caplog, monkeypatch):
        """The umbrella flag surfaces the full ALL_FLAGS set in enabled_flags.

        With GCO_ENABLE_ALL_TOOLS=true every per-tool flag is effectively
        enabled, so enabled_flags equals the sorted ALL_FLAGS — the effective
        gated-tool surface, reported alongside the all_tools_enabled boolean
        that explains why.
        """
        monkeypatch.delenv("GCO_MCP_TOOL_SEARCH", raising=False)
        for flag in audit.feature_flags.ALL_FLAGS:
            monkeypatch.delenv(flag, raising=False)
        monkeypatch.setenv("GCO_ENABLE_ALL_TOOLS", "true")

        with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
            run_mcp.emit_startup_log()

        entry = _last_startup_entry(caplog)
        assert entry["all_tools_enabled"] is True
        assert entry["enabled_flags"] == sorted(audit.feature_flags.ALL_FLAGS)
