"""
Output formatting for GCO CLI.

Provides consistent output formatting across all CLI commands
with support for table, JSON, and YAML formats.
"""

import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any

import yaml

from .config import GCOConfig, get_config


def _serialize_value(value: Any) -> Any:
    """Serialize a value for output."""
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize_value(v) for v in value]
    return value


class OutputFormatter:
    """
    Formats output for CLI commands.

    Supports:
    - Table format (human-readable)
    - JSON format (machine-readable)
    - YAML format (configuration-friendly)
    """

    def __init__(self, config: GCOConfig | None = None):
        self.config = config or get_config()
        self._format = self.config.output_format

    def set_format(self, format_type: str) -> None:
        """Set the output format."""
        if format_type not in ("table", "json", "yaml"):
            raise ValueError(f"Invalid format: {format_type}")
        self._format = format_type

    def format(self, data: Any, columns: list[str] | None = None) -> str:
        """
        Format data for output.

        Args:
            data: Data to format (dict, list, or dataclass)
            columns: Column names for table format

        Returns:
            Formatted string
        """
        if self._format == "json":
            return self._format_json(data)
        if self._format == "yaml":
            return self._format_yaml(data)
        return self._format_table(data, columns)

    def _format_json(self, data: Any) -> str:
        """Format data as JSON."""
        serialized = _serialize_value(data)
        return json.dumps(serialized, indent=2, default=str)

    def _format_yaml(self, data: Any) -> str:
        """Format data as YAML."""
        serialized = _serialize_value(data)
        return str(yaml.dump(serialized, default_flow_style=False, sort_keys=False))

    def _format_table(self, data: Any, columns: list[str] | None = None) -> str:
        """Format data as a table."""
        if data is None:
            return "No data"

        # Convert to list of dicts
        if is_dataclass(data) and not isinstance(data, type):
            rows = [asdict(data)]
        elif isinstance(data, dict):
            rows = [data]
        elif isinstance(data, list):
            if not data:
                return "No results"
            if is_dataclass(data[0]) and not isinstance(data[0], type):
                rows = [asdict(item) for item in data]
            elif isinstance(data[0], dict):
                rows = data
            else:
                # Simple list
                return "\n".join(str(item) for item in data)
        else:
            return str(data)

        # Determine columns. `rows` is always non-empty here: every branch
        # above either returns early (None, empty list) or assigns at least
        # one row (a dict becomes `[data]`, even an empty `{}`).
        if columns is None:
            columns = list(rows[0].keys())

        # Filter to only requested columns
        rows = [{k: v for k, v in row.items() if k in columns} for row in rows]

        # Calculate column widths
        widths = {}
        for col in columns:
            col_values = [str(row.get(col, "")) for row in rows]
            widths[col] = max(len(col), max(len(v) for v in col_values) if col_values else 0)

        # Build table
        lines = []

        # Header
        header = "  ".join(col.upper().ljust(widths[col]) for col in columns)
        lines.append(header)
        lines.append("-" * len(header))

        # Rows
        for row in rows:
            line = "  ".join(
                self._format_cell(row.get(col, ""), widths[col], col) for col in columns
            )
            lines.append(line)

        return "\n".join(lines)

    def _format_cell(self, value: Any, width: int, column_name: str = "") -> str:
        """Format a single cell value."""
        if value is None:
            return "-".ljust(width)
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M").ljust(width)
        if isinstance(value, bool):
            return ("Yes" if value else "No").ljust(width)
        if isinstance(value, float):
            # Add dollar sign for price columns (but not stability/ratio columns)
            col_lower = column_name.lower()
            if "price" in col_lower and "stability" not in col_lower:
                return f"${value:.4f}".ljust(width)
            return f"{value:.4f}".ljust(width)
        if isinstance(value, dict):
            return "<dict>".ljust(width)
        if isinstance(value, list):
            return f"[{len(value)} items]".ljust(width)
        return str(str(value)[:width]).ljust(width)

    def print(self, data: Any, columns: list[str] | None = None) -> None:
        """Format and print data."""
        print(self.format(data, columns))

    def print_success(self, message: str) -> None:
        """Print a success message."""
        print(f"✓ {message}")

    def print_error(self, message: str) -> None:
        """Print an error message."""
        print(f"✗ {message}", file=sys.stderr)

    def print_warning(self, message: str) -> None:
        """Print a warning message."""
        print(f"⚠ {message}", file=sys.stderr)

    def print_info(self, message: str) -> None:
        """Print an info message."""
        print(f"ℹ {message}")


# Convenience functions for common output patterns


def format_job_table(jobs: list[Any]) -> str:
    """Format jobs as a table."""
    formatter = OutputFormatter()
    return formatter.format(
        jobs,
        columns=[
            "name",
            "namespace",
            "region",
            "status",
            "active_pods",
            "succeeded_pods",
            "failed_pods",
        ],
    )


def format_capacity_table(estimates: list[Any]) -> str:
    """Format capacity estimates as a table."""
    formatter = OutputFormatter()
    return formatter.format(
        estimates,
        columns=[
            "instance_type",
            "region",
            "availability_zone",
            "capacity_type",
            "availability",
            "price_per_hour",
            "recommendation",
        ],
    )


def format_file_system_table(file_systems: list[Any]) -> str:
    """Format file systems as a table."""
    formatter = OutputFormatter()
    return formatter.format(
        file_systems, columns=["file_system_id", "file_system_type", "region", "status", "dns_name"]
    )


def format_stack_table(stacks: list[Any]) -> str:
    """Format regional stacks as a table."""
    formatter = OutputFormatter()
    return formatter.format(
        stacks, columns=["region", "stack_name", "cluster_name", "status", "efs_file_system_id"]
    )


def get_output_formatter(config: GCOConfig | None = None) -> OutputFormatter:
    """Get a configured output formatter instance."""
    return OutputFormatter(config)
