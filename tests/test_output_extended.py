"""
Extended tests for cli/output.OutputFormatter.

Covers _format_cell's price-column detection — floats in columns
whose name contains "price" get a $ prefix, but "price_stability"
is excluded by an explicit substring match so percentages don't get
mislabeled as currency — plus string truncation and padding. Also
exercises _format_table's column filtering against an explicit list
(versus the fallback to all keys from the first row) and confirms
the format() method routes to the correct underlying formatter.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import yaml

from cli.config import GCOConfig
from cli.output import OutputFormatter, _serialize_value


class TestFormatCellPriceColumns:
    """Tests for _format_cell price column detection."""

    def test_price_column_gets_dollar_sign(self):
        """Float in a 'price' column should get $ prefix."""
        config = GCOConfig(output_format="table")
        formatter = OutputFormatter(config)
        result = formatter._format_cell(1.2345, 15, "price_per_hour")
        assert result.strip() == "$1.2345"

    def test_stability_column_no_dollar_sign(self):
        """Float in a 'price_stability' column should NOT get $ prefix."""
        config = GCOConfig(output_format="table")
        formatter = OutputFormatter(config)
        result = formatter._format_cell(0.95, 15, "price_stability")
        assert "$" not in result
        assert "0.9500" in result

    def test_non_price_float_no_dollar_sign(self):
        """Float in a non-price column should not get $ prefix."""
        config = GCOConfig(output_format="table")
        formatter = OutputFormatter(config)
        result = formatter._format_cell(3.14, 15, "cpu_utilization")
        assert "$" not in result

    def test_price_column_case_insensitive(self):
        """Price detection should be case-insensitive."""
        config = GCOConfig(output_format="table")
        formatter = OutputFormatter(config)
        result = formatter._format_cell(9.99, 15, "PRICE")
        assert "$" in result


class TestFormatCellStringTruncation:
    """Tests for _format_cell string truncation."""

    def test_long_string_truncated(self):
        """Strings longer than width should be truncated."""
        config = GCOConfig(output_format="table")
        formatter = OutputFormatter(config)
        result = formatter._format_cell("a" * 100, 10)
        assert len(result) == 10

    def test_short_string_padded(self):
        """Strings shorter than width should be left-padded."""
        config = GCOConfig(output_format="table")
        formatter = OutputFormatter(config)
        result = formatter._format_cell("hi", 10)
        assert len(result) == 10
        assert result.startswith("hi")


class TestFormatTableColumnFiltering:
    """Tests for _format_table with column filtering."""

    def test_table_filters_to_requested_columns(self):
        """Table should only show requested columns."""
        config = GCOConfig(output_format="table")
        formatter = OutputFormatter(config)
        data = [
            {"name": "job1", "status": "running", "region": "us-east-1", "extra": "ignored"},
            {"name": "job2", "status": "done", "region": "us-west-2", "extra": "ignored"},
        ]
        result = formatter.format(data, columns=["name", "status"])
        assert "NAME" in result
        assert "STATUS" in result
        assert "REGION" not in result
        assert "EXTRA" not in result

    def test_table_with_no_columns_uses_all_keys(self):
        """Table with no columns specified should use all keys from first row."""
        config = GCOConfig(output_format="table")
        formatter = OutputFormatter(config)
        data = [{"a": 1, "b": 2}]
        result = formatter.format(data)
        assert "A" in result
        assert "B" in result


class TestFormatMethodRouting:
    """Tests for format method routing to correct formatter."""

    def test_format_routes_to_json(self):
        """format() with json config should produce JSON."""
        config = GCOConfig(output_format="json")
        formatter = OutputFormatter(config)
        result = formatter.format({"key": "value"})
        assert '"key": "value"' in result

    def test_format_routes_to_yaml(self):
        """format() with yaml config should produce YAML."""
        config = GCOConfig(output_format="yaml")
        formatter = OutputFormatter(config)
        result = formatter.format({"key": "value"})
        assert "key: value" in result

    def test_format_routes_to_table(self):
        """format() with table config should produce table."""
        config = GCOConfig(output_format="table")
        formatter = OutputFormatter(config)
        result = formatter.format({"key": "value"})
        assert "KEY" in result

    def test_set_format_overrides_config(self):
        """set_format should override the config format."""
        config = GCOConfig(output_format="table")
        formatter = OutputFormatter(config)
        formatter.set_format("json")
        result = formatter.format({"key": "value"})
        assert '"key"' in result


class TestSerializeValueNested:
    """Tests for _serialize_value with nested structures."""

    def test_nested_dict_with_datetime(self):
        """Should serialize datetime inside nested dict."""
        dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
        data = {"outer": {"inner": dt}}
        result = _serialize_value(data)
        assert result["outer"]["inner"] == dt.isoformat()

    def test_list_of_datetimes(self):
        """Should serialize list of datetimes."""
        dt1 = datetime(2024, 1, 1, tzinfo=UTC)
        dt2 = datetime(2024, 6, 1, tzinfo=UTC)
        result = _serialize_value([dt1, dt2])
        assert result == [dt1.isoformat(), dt2.isoformat()]

    def test_nested_dataclass(self):
        """Should serialize nested dataclass."""

        @dataclass
        class Inner:
            value: int

        @dataclass
        class Outer:
            inner: Inner
            name: str

        data = Outer(inner=Inner(value=42), name="test")
        result = _serialize_value(data)
        assert result == {"inner": {"value": 42}, "name": "test"}

    def test_datetimes_inside_dataclasses_use_isoformat(self):
        """asdict copies datetimes through; they must still become ISO strings."""
        dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)

        @dataclass
        class Inner:
            when: datetime

        @dataclass
        class Outer:
            created: datetime
            inner: Inner
            history: list[datetime]
            window: tuple[datetime, datetime]

        data = Outer(created=dt, inner=Inner(when=dt), history=[dt], window=(dt, dt))
        result = _serialize_value({"rows": [data]})
        assert result == {
            "rows": [
                {
                    "created": dt.isoformat(),
                    "inner": {"when": dt.isoformat()},
                    "history": [dt.isoformat()],
                    "window": [dt.isoformat(), dt.isoformat()],
                }
            ]
        }

    @pytest.mark.parametrize("output_format", ["json", "yaml"])
    def test_formatter_renders_dataclass_datetimes_like_top_level_ones(self, output_format):
        """One timestamp format in JSON and YAML, wherever the datetime sits."""
        dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)

        @dataclass
        class Row:
            created: datetime

        formatter = OutputFormatter(GCOConfig(output_format=output_format))
        rendered = formatter.format({"top": dt, "row": Row(created=dt)})
        parsed = json.loads(rendered) if output_format == "json" else yaml.safe_load(rendered)
        assert parsed == {"top": "2024-01-15T10:30:00+00:00", "row": {"created": parsed["top"]}}

    def test_primitive_passthrough(self):
        """Primitives should pass through unchanged."""
        assert _serialize_value(42) == 42
        assert _serialize_value("hello") == "hello"
        assert _serialize_value(True) is True
        assert _serialize_value(None) is None


class TestPrintMethods:
    """Tests for print helper methods."""

    def test_print_data(self, capsys):
        """print() should output formatted data."""
        config = GCOConfig(output_format="json")
        formatter = OutputFormatter(config)
        formatter.print({"test": 1})
        captured = capsys.readouterr()
        assert '"test": 1' in captured.out

    def test_print_success_prefix(self, capsys):
        """print_success should prefix with checkmark."""
        formatter = OutputFormatter()
        formatter.print_success("Done")
        captured = capsys.readouterr()
        assert "✓ Done" in captured.out

    def test_print_error_to_stderr(self, capsys):
        """print_error should write to stderr."""
        formatter = OutputFormatter()
        formatter.print_error("Failed")
        captured = capsys.readouterr()
        assert "✗ Failed" in captured.err

    def test_print_warning_to_stderr(self, capsys):
        """print_warning should write to stderr."""
        formatter = OutputFormatter()
        formatter.print_warning("Caution")
        captured = capsys.readouterr()
        assert "⚠ Caution" in captured.err

    def test_print_info_prefix(self, capsys):
        """print_info should prefix with info symbol."""
        formatter = OutputFormatter()
        formatter.print_info("Note")
        captured = capsys.readouterr()
        assert "ℹ Note" in captured.out


class TestTableEdgeCases:
    """Tests for table formatting edge cases."""

    def test_table_with_none_data(self):
        """Table with None data should return 'No data'."""
        formatter = OutputFormatter(GCOConfig(output_format="table"))
        result = formatter.format(None)
        assert result == "No data"

    def test_table_with_empty_list(self):
        """Table with empty list should return 'No results'."""
        formatter = OutputFormatter(GCOConfig(output_format="table"))
        result = formatter.format([])
        assert result == "No results"

    def test_table_with_simple_list(self):
        """Table with list of primitives should join with newlines."""
        formatter = OutputFormatter(GCOConfig(output_format="table"))
        result = formatter.format(["alpha", "beta", "gamma"])
        assert "alpha" in result
        assert "beta" in result
        assert "gamma" in result

    def test_table_with_list_value_in_cell(self):
        """List values in cells should show item count."""
        formatter = OutputFormatter(GCOConfig(output_format="table"))
        result = formatter.format({"items": [1, 2, 3]})
        assert "[3 items]" in result

    def test_table_with_dict_value_in_cell(self):
        """Dict values in cells should show <dict>."""
        formatter = OutputFormatter(GCOConfig(output_format="table"))
        result = formatter.format({"config": {"a": 1}})
        assert "<dict>" in result

    def test_table_with_bool_values(self):
        """Bool values should show Yes/No."""
        formatter = OutputFormatter(GCOConfig(output_format="table"))
        result = formatter.format({"enabled": True, "disabled": False})
        assert "Yes" in result
        assert "No" in result
