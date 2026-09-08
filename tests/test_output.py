"""
Tests for cli/output.py — the table/JSON/YAML output formatter.

Covers the _serialize_value helper (datetime, dataclass, dict, list,
primitive passthrough), OutputFormatter initialization and format
selection (table/json/yaml with set_format validation), and the JSON-
specific formatter paths. Extended table-rendering cases like price-
column detection, string truncation, and column filtering live in
test_output_extended.py.
"""

import io
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from unittest.mock import MagicMock, patch

import click
import pytest
import yaml


class TestSerializeValue:
    """Tests for _serialize_value function."""

    def test_serialize_datetime(self):
        """Test serializing datetime."""
        from cli.output import _serialize_value

        dt = datetime(2024, 1, 15, 10, 30, 0)
        result = _serialize_value(dt)
        assert "2024-01-15" in result

    def test_serialize_dataclass(self):
        """Test serializing dataclass."""
        from cli.output import _serialize_value

        @dataclass
        class TestData:
            name: str
            value: int

        data = TestData(name="test", value=42)
        result = _serialize_value(data)
        assert result == {"name": "test", "value": 42}

    def test_serialize_dict(self):
        """Test serializing dict with nested values."""
        from cli.output import _serialize_value

        data = {"timestamp": datetime(2024, 1, 1), "value": 123}
        result = _serialize_value(data)
        assert "2024-01-01" in result["timestamp"]
        assert result["value"] == 123

    def test_serialize_list(self):
        """Test serializing list."""
        from cli.output import _serialize_value

        data = [datetime(2024, 1, 1), "test", 123]
        result = _serialize_value(data)
        assert len(result) == 3
        assert "2024-01-01" in result[0]

    def test_serialize_primitive(self):
        """Test serializing primitive values."""
        from cli.output import _serialize_value

        assert _serialize_value("test") == "test"
        assert _serialize_value(123) == 123
        assert _serialize_value(True) is True


class TestOutputFormatter:
    """Tests for OutputFormatter class."""

    def test_formatter_initialization(self):
        """Test OutputFormatter initialization."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()
            assert formatter._format == "table"

    def test_set_format_valid(self):
        """Test setting valid format."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()
            formatter.set_format("json")
            assert formatter._format == "json"

    def test_set_format_invalid(self):
        """Test setting invalid format raises error."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()
            with pytest.raises(ValueError, match="Invalid format"):
                formatter.set_format("invalid")


class TestOutputFormatterJSON:
    """Tests for JSON formatting."""

    def test_format_json_dict(self):
        """Test JSON formatting of dict."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="json")
            formatter = OutputFormatter()

            data = {"name": "test", "value": 123}
            result = formatter.format(data)

            parsed = json.loads(result)
            assert parsed["name"] == "test"
            assert parsed["value"] == 123

    def test_format_json_list(self):
        """Test JSON formatting of list."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="json")
            formatter = OutputFormatter()

            data = [{"name": "item1"}, {"name": "item2"}]
            result = formatter.format(data)

            parsed = json.loads(result)
            assert len(parsed) == 2

    def test_format_json_datetime(self):
        """Test JSON formatting with datetime."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="json")
            formatter = OutputFormatter()

            data = {"timestamp": datetime(2024, 1, 15, 10, 30, 0)}
            result = formatter.format(data)

            parsed = json.loads(result)
            assert "2024-01-15" in parsed["timestamp"]


class TestOutputFormatterYAML:
    """Tests for YAML formatting."""

    def test_format_yaml_dict(self):
        """Test YAML formatting of dict."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="yaml")
            formatter = OutputFormatter()

            data = {"name": "test", "value": 123}
            result = formatter.format(data)

            parsed = yaml.safe_load(result)
            assert parsed["name"] == "test"
            assert parsed["value"] == 123

    def test_format_yaml_list(self):
        """Test YAML formatting of list."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="yaml")
            formatter = OutputFormatter()

            data = [{"name": "item1"}, {"name": "item2"}]
            result = formatter.format(data)

            parsed = yaml.safe_load(result)
            assert len(parsed) == 2


class TestOutputFormatterTable:
    """Tests for table formatting."""

    def test_format_table_none(self):
        """Test table formatting of None."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()

            result = formatter.format(None)
            assert result == "No data"

    def test_format_table_empty_list(self):
        """Test table formatting of empty list."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()

            result = formatter.format([])
            assert result == "No results"

    def test_format_table_dict(self):
        """Test table formatting of dict."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()

            data = {"name": "test", "status": "running"}
            result = formatter.format(data, columns=["name", "status"])

            assert "NAME" in result
            assert "STATUS" in result
            assert "test" in result
            assert "running" in result

    def test_format_table_list_of_dicts(self):
        """Test table formatting of list of dicts."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()

            data = [
                {"name": "job1", "status": "running"},
                {"name": "job2", "status": "completed"},
            ]
            result = formatter.format(data, columns=["name", "status"])

            assert "NAME" in result
            assert "job1" in result
            assert "job2" in result

    def test_format_table_dataclass(self):
        """Test table formatting of dataclass."""
        from cli.output import OutputFormatter

        @dataclass
        class TestData:
            name: str
            value: int

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()

            data = TestData(name="test", value=42)
            result = formatter.format(data, columns=["name", "value"])

            assert "NAME" in result
            assert "test" in result

    def test_format_table_simple_list(self):
        """Test table formatting of simple list."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()

            data = ["item1", "item2", "item3"]
            result = formatter.format(data)

            assert "item1" in result
            assert "item2" in result

    def test_format_table_scalar_falls_back_to_str(self):
        """A scalar that is neither a dataclass, dict, nor list (e.g. an int
        or plain string) is rendered with str() rather than as a table."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()

            assert formatter.format(42) == "42"
            assert formatter.format("plain string") == "plain string"


class TestOutputFormatterCells:
    """Tests for cell formatting."""

    def test_format_cell_none(self):
        """Test formatting None cell."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()

            result = formatter._format_cell(None, 10)
            assert "-" in result

    def test_format_cell_datetime(self):
        """Test formatting datetime cell."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()

            dt = datetime(2024, 1, 15, 10, 30)
            result = formatter._format_cell(dt, 20)
            assert "2024-01-15" in result

    def test_format_cell_bool(self):
        """Test formatting boolean cell."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()

            assert "Yes" in formatter._format_cell(True, 10)
            assert "No" in formatter._format_cell(False, 10)

    def test_format_cell_float(self):
        """Test formatting float cell."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()

            result = formatter._format_cell(3.14159, 10)
            assert "3.1416" in result

    def test_format_cell_dict(self):
        """Test formatting dict cell."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()

            result = formatter._format_cell({"key": "value"}, 10)
            assert "<dict>" in result

    def test_format_cell_list(self):
        """Test formatting list cell."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()

            result = formatter._format_cell([1, 2, 3], 15)
            assert "3 items" in result


class TestOutputFormatterPrint:
    """Tests for print methods."""

    def test_print_success(self, capsys):
        """Test print_success method."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()
            formatter.print_success("Operation completed")

            captured = capsys.readouterr()
            assert "✓" in captured.out
            assert "Operation completed" in captured.out

    def test_print_error(self, capsys):
        """Test print_error method."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()
            formatter.print_error("Something failed")

            captured = capsys.readouterr()
            assert "✗" in captured.err
            assert "Something failed" in captured.err

    def test_print_warning(self, capsys):
        """Test print_warning method."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()
            formatter.print_warning("Be careful")

            captured = capsys.readouterr()
            assert "⚠" in captured.err
            assert "Be careful" in captured.err

    def test_print_info(self, capsys):
        """Test print_info method."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = OutputFormatter()
            formatter.print_info("FYI")

            captured = capsys.readouterr()
            assert "ℹ" in captured.out
            assert "FYI" in captured.out

    @pytest.mark.parametrize("output_format", ["json", "yaml"])
    @pytest.mark.parametrize("method_name", ["print_success", "print_info"])
    def test_human_messages_are_suppressed_in_machine_modes(
        self, capsys, output_format, method_name
    ):
        """Human-only helpers must never contaminate structured stdout."""
        from cli.output import OutputFormatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format=output_format)
            formatter = OutputFormatter()
            getattr(formatter, method_name)("human progress")

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


class TestConvenienceFunctions:
    """Tests for convenience formatting functions."""

    def test_format_job_table(self):
        """Test format_job_table function."""
        from cli.output import format_job_table

        jobs = [
            {
                "name": "job1",
                "namespace": "default",
                "region": "us-east-1",
                "status": "running",
                "active_pods": 1,
                "succeeded_pods": 0,
                "failed_pods": 0,
            }
        ]

        result = format_job_table(jobs)
        assert "NAME" in result
        assert "job1" in result

    def test_format_capacity_table(self):
        """Test format_capacity_table function."""
        from cli.output import format_capacity_table

        estimates = [
            {
                "instance_type": "g4dn.xlarge",
                "region": "us-east-1",
                "availability_zone": "us-east-1a",
                "capacity_type": "spot",
                "availability": "high",
                "price_per_hour": 0.50,
                "recommendation": "Good",
            }
        ]

        result = format_capacity_table(estimates)
        assert "INSTANCE_TYPE" in result
        assert "g4dn.xlarge" in result

    def test_format_file_system_table(self):
        """Test format_file_system_table function."""
        from cli.output import format_file_system_table

        file_systems = [
            {
                "file_system_id": "fs-12345678",
                "file_system_type": "efs",
                "region": "us-east-1",
                "status": "available",
                "dns_name": "fs-12345678.efs.us-east-1.amazonaws.com",
            }
        ]

        result = format_file_system_table(file_systems)
        assert "FILE_SYSTEM_ID" in result
        assert "fs-12345678" in result

    def test_format_stack_table(self):
        """Test format_stack_table function."""
        from cli.output import format_stack_table

        stacks = [
            {
                "region": "us-east-1",
                "stack_name": "gco-us-east-1",
                "cluster_name": "gco-us-east-1",
                "status": "CREATE_COMPLETE",
                "efs_file_system_id": "fs-12345678",
            }
        ]

        result = format_stack_table(stacks)
        assert "REGION" in result
        assert "us-east-1" in result


class TestGetOutputFormatter:
    """Tests for get_output_formatter factory function."""

    def test_get_output_formatter(self):
        """Test factory function returns OutputFormatter."""
        from cli.output import OutputFormatter, get_output_formatter

        with patch("cli.output.get_config") as mock_config:
            mock_config.return_value = MagicMock(output_format="table")
            formatter = get_output_formatter()
            assert isinstance(formatter, OutputFormatter)

    def test_get_output_formatter_with_config(self):
        """Test factory function with custom config."""
        from cli.output import OutputFormatter, get_output_formatter

        custom_config = MagicMock(output_format="json")
        formatter = get_output_formatter(custom_config)
        assert isinstance(formatter, OutputFormatter)
        assert formatter._format == "json"


class _FakeTemporary:
    def __init__(
        self,
        data: bytes = b"captured",
        *,
        seek_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.data = data
        self.seek_error = seek_error
        self.close_error = close_error
        self.closed = False

    def seek(self, _offset: int) -> None:
        if self.seek_error is not None:
            raise self.seek_error

    def read(self) -> bytes:
        return self.data

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def test_machine_output_detection_and_interactive_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli.output as output_module

    assert output_module._machine_output_active() is False

    parameter_context = click.Context(click.Command("parameter"))
    parameter_context.params["output_format"] = "json"
    with parameter_context:
        assert output_module._machine_output_active() is True

    configured = MagicMock()
    configured.output_format = "yaml"
    configured_context = click.Context(click.Command("configured"), obj=configured)
    with configured_context:
        assert output_module._machine_output_active() is True

    parent = click.Context(
        click.Command("parent"),
        obj=MagicMock(output_format="yaml"),
    )
    child = click.Context(click.Command("child"), parent=parent, obj=object())
    with child:
        assert output_module._machine_output_active() is True

    token = output_module._structured_emissions_var.set([])
    try:
        with (
            patch.object(output_module.click, "confirm", return_value=True) as confirm_mock,
            patch.object(output_module.click, "prompt", return_value="value") as prompt_mock,
            patch.object(output_module.click, "echo") as echo_mock,
        ):
            assert output_module.confirm("continue?") is True
            assert output_module.confirm("continue?", err=False) is True
            assert output_module.prompt("value?") == "value"
            assert output_module.prompt("value?", err=False) == "value"
            output_module.interactive_echo("context")
            output_module.interactive_echo("context", err=False)
    finally:
        output_module._structured_emissions_var.reset(token)

    assert confirm_mock.call_args_list[0].kwargs["err"] is True
    assert confirm_mock.call_args_list[1].kwargs["err"] is False
    assert prompt_mock.call_args_list[0].kwargs["err"] is True
    assert prompt_mock.call_args_list[1].kwargs["err"] is False
    assert echo_mock.call_args_list[0].kwargs["err"] is True
    assert echo_mock.call_args_list[1].kwargs["err"] is False


def test_strict_json_registration_and_mismatched_native_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import cli.output as output_module

    with pytest.raises(ValueError, match="non-standard JSON constant"):
        output_module._reject_nonstandard_json_constant("NaN")

    token = output_module._structured_emissions_var.set([])
    try:
        with pytest.raises(ValueError, match="non-standard JSON constant"):
            output_module.emit_structured_document(
                {"value": float("nan")},
                output_format="json",
                rendered='{"value": NaN}',
            )
    finally:
        output_module._structured_emissions_var.reset(token)

    assert output_module._structured_document_from_text(
        '{"actual": true}\n',
        "json",
        [({"expected": True}, '{"expected": true}\n')],
    ) == {"status": "ok", "output": '{"actual": true}'}
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--config", "custom.yml", "command"], (None, "custom.yml")),
        (["--config=custom.yml", "command"], (None, "custom.yml")),
        (["--output=json", "command"], ("json", None)),
        (["--region", "us-east-1", "command"], (None, None)),
        (["--unknown", "command"], (None, None)),
        (["-ccustom"], (None, "custom")),
        (["-vc", "custom.yml", "command"], (None, "custom.yml")),
        (["-vr", "us-east-1", "command"], (None, None)),
        (["-vx", "command"], (None, None)),
        (["-"], (None, None)),
        (["-o"], (None, None)),
        (["--", "--output", "json"], (None, None)),
    ],
)
def test_root_output_settings_edge_cases(
    args: list[str],
    expected: tuple[str | None, str | None],
) -> None:
    from cli.output import _root_output_settings

    assert _root_output_settings(args) == expected


def test_requested_format_failure_and_shell_completion_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli.output as output_module

    monkeypatch.setattr(
        output_module,
        "get_config",
        MagicMock(side_effect=RuntimeError("bad config")),
    )
    assert output_module._requested_output_format([]) is None

    monkeypatch.setenv("_GCO_COMPLETE", "bash_complete")
    assert output_module._shell_completion_requested(None, "gco") is True
    assert output_module._shell_completion_requested("_OTHER_COMPLETE", "gco") is False
    monkeypatch.setenv("_OTHER_COMPLETE", "fish_complete")
    assert output_module._shell_completion_requested("_OTHER_COMPLETE", None) is True


def test_stdout_capture_noop_release_and_replay_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli.output as output_module

    capture = output_module._StdoutCapture()
    capture._release_lock()

    text_stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", text_stream)
    capture.text = "fallback"
    capture.replay()
    assert text_stream.getvalue() == "fallback"

    capture.raw_bytes = b"abc"
    capture._stream_fd = 17
    capture._stream = MagicMock()
    writes: list[bytes] = []

    def write_one(_fd: int, data: memoryview) -> int:
        writes.append(bytes(data))
        return 1

    monkeypatch.setattr(output_module.os, "write", write_one)
    capture.replay()
    assert writes == [b"abc", b"bc", b"c"]

    monkeypatch.setattr(output_module.os, "write", lambda _fd, _data: 0)
    with pytest.raises(OSError, match="made no progress"):
        capture.replay()


def test_stdout_capture_enter_releases_lock_when_fallback_setup_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli.output as output_module

    stream = MagicMock()
    stream.fileno.return_value = 17
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(output_module.os, "dup", MagicMock(side_effect=OSError("no fd")))
    redirect = MagicMock()
    redirect.__enter__.side_effect = KeyboardInterrupt
    monkeypatch.setattr(output_module, "redirect_stdout", MagicMock(return_value=redirect))

    capture = output_module._StdoutCapture()
    with pytest.raises(KeyboardInterrupt):
        capture.__enter__()
    assert capture._owns_lock is False


def test_stdout_capture_fallback_cleanup_errors_preserve_active_exception() -> None:
    from cli.output import _StdoutCapture

    capture = _StdoutCapture()
    capture._redirect = MagicMock()
    capture._redirect.__exit__.side_effect = RuntimeError("redirect cleanup")
    capture._string_buffer = MagicMock()
    capture._string_buffer.getvalue.side_effect = RuntimeError("buffer cleanup")

    capture.__exit__(RuntimeError, RuntimeError("command failed"), None)


def test_stdout_capture_cleanup_error_raises_without_active_exception() -> None:
    from cli.output import _StdoutCapture

    capture = _StdoutCapture()
    capture._redirect = MagicMock()
    capture._redirect.__exit__.side_effect = RuntimeError("redirect cleanup")
    capture._string_buffer = io.StringIO("captured")

    with pytest.raises(RuntimeError, match="redirect cleanup"):
        capture.__exit__(None, None, None)


def test_stdout_capture_fd_cleanup_covers_recovery_and_decode_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli.output as output_module

    capture = output_module._StdoutCapture()
    capture._stream = MagicMock()
    capture._stream.flush.side_effect = RuntimeError("flush cleanup")
    capture._stream_fd = 17
    capture._saved_fd = 18
    capture._stream_encoding = "not-a-real-codec"
    capture._temporary = _FakeTemporary(
        b"captured",
        close_error=RuntimeError("close temporary"),
    )
    restore_attempts = 0
    real_dup2 = output_module.os.dup2

    def flaky_dup2(source: int, target: int) -> None:
        nonlocal restore_attempts
        if (source, target) != (18, 17):
            real_dup2(source, target)
            return
        restore_attempts += 1
        if restore_attempts == 1:
            raise OSError("first restore")

    real_close = output_module.os.close

    def flaky_close(fd: int) -> None:
        if fd == 18:
            raise OSError("close saved")
        real_close(fd)

    monkeypatch.setattr(output_module.os, "dup2", flaky_dup2)
    monkeypatch.setattr(output_module.os, "close", flaky_close)

    capture.__exit__(RuntimeError, RuntimeError("command failed"), None)

    assert capture.text == "captured"
    assert capture.raw_bytes == b"captured"
    assert capture._saved_fd is None
    assert capture._temporary is None


def test_stdout_capture_fd_cleanup_covers_unrestored_and_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli.output as output_module

    capture = output_module._StdoutCapture()
    capture._stream = MagicMock()
    capture._stream_fd = 17
    capture._saved_fd = 18
    capture._temporary = _FakeTemporary(seek_error=RuntimeError("seek cleanup"))
    real_dup2 = output_module.os.dup2

    def broken_dup2(source: int, target: int) -> None:
        if (source, target) == (18, 17):
            raise OSError("restore failed")
        real_dup2(source, target)

    target_closes: list[int] = []
    real_close = output_module.os.close

    def tracked_close(fd: int) -> None:
        if fd == 18:
            target_closes.append(fd)
            return
        real_close(fd)

    monkeypatch.setattr(output_module.os, "dup2", broken_dup2)
    monkeypatch.setattr(output_module.os, "close", tracked_close)

    capture.__exit__(RuntimeError, RuntimeError("command failed"), None)

    assert target_closes == []
    assert capture._saved_fd == 18
    assert capture._temporary is not None


def test_table_formatter_prints_a_list_of_dataclasses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.config import GCOConfig
    from cli.output import OutputFormatter

    @dataclass
    class Row:
        name: str
        value: int

    formatter = OutputFormatter(GCOConfig(output_format="table"))
    formatter.print([Row(name="first", value=1), Row(name="second", value=2)])

    output = capsys.readouterr().out
    assert "NAME" in output
    assert "first" in output
    assert "second" in output
