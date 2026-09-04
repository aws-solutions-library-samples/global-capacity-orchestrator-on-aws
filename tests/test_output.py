"""
Tests for cli/output.py — the table/JSON/YAML output formatter.

Covers the _serialize_value helper (datetime, dataclass, dict, list,
primitive passthrough), OutputFormatter initialization and format
selection (table/json/yaml with set_format validation), and the JSON-
specific formatter paths. Extended table-rendering cases like price-
column detection, string truncation, and column filtering live in
test_output_extended.py.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from unittest.mock import MagicMock, patch

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
