"""
Output formatting for GCO CLI.

Provides consistent output formatting across all CLI commands
with support for table, JSON, and YAML formats.
"""

import json
import os
import sys
import tempfile
import threading
from collections.abc import Sequence
from contextlib import redirect_stdout
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import datetime
from io import StringIO
from typing import Any

import click
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


_structured_emissions_var: ContextVar[list[tuple[Any, str]] | None] = ContextVar(
    "gco_structured_emissions", default=None
)
_structured_exit_code_var: ContextVar[int | None] = ContextVar(
    "gco_structured_exit_code", default=None
)
_STDOUT_CAPTURE_LOCK = threading.RLock()


def _reject_nonstandard_json_constant(token: str) -> None:
    raise ValueError(f"non-standard JSON constant: {token}")


def _machine_output_active() -> bool:
    """Return whether the active root or direct group uses JSON/YAML output."""
    if _structured_emissions_var.get() is not None:
        return True

    context = click.get_current_context(silent=True)
    while context is not None:
        output_format = context.params.get("output_format")
        if output_format in {"json", "yaml"}:
            return True
        configured_format = getattr(context.obj, "output_format", None)
        if configured_format in {"json", "yaml"}:
            return True
        context = context.parent
    return False


def confirm(message: str, *args: Any, **kwargs: Any) -> bool:
    """Run a Click confirmation without contaminating machine stdout."""
    if "err" not in kwargs and _machine_output_active():
        kwargs["err"] = True
    return click.confirm(message, *args, **kwargs)


def prompt(message: str, *args: Any, **kwargs: Any) -> Any:
    """Run a Click prompt without contaminating machine stdout."""
    if "err" not in kwargs and _machine_output_active():
        kwargs["err"] = True
    return click.prompt(message, *args, **kwargs)


def interactive_echo(message: Any = None, **kwargs: Any) -> None:
    """Emit interactive context on stderr in machine-readable modes."""
    if "err" not in kwargs and _machine_output_active():
        kwargs["err"] = True
    click.echo(message, **kwargs)


def _canonical_emission_text(text: str) -> str:
    """Normalize platform line endings only for native-emission matching."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _structured_document_from_text(
    text: str,
    output_format: str,
    emissions: Sequence[tuple[Any, str]] = (),
) -> Any:
    """Normalize one command's successful stdout into one structured value.

    A single document emitted through :func:`emit_structured_document` keeps
    its native mapping/list/null schema (or receives the scalar envelope).
    Unregistered stdout is always raw text: YAML accepts most prose as valid
    syntax, so parsing captured bytes cannot reliably distinguish a command
    payload from logs, help text, or streamed output.
    """
    del output_format  # The requested format controls rendering, not classification.
    stripped = text.strip()
    if not stripped:
        return {"status": "ok"}

    if len(emissions) == 1:
        document, emitted_text = emissions[0]
        if _canonical_emission_text(text) == _canonical_emission_text(emitted_text):
            if isinstance(document, (dict, list)) or document is None:
                return document
            return {"status": "ok", "result": document}

    return {"status": "ok", "output": text.rstrip("\r\n")}


def _render_structured_document(document: Any, output_format: str) -> str:
    """Render a normalized document in the exact root-requested format."""
    serialized = _serialize_value(document)
    if output_format == "json":
        return json.dumps(serialized, indent=2, default=str, allow_nan=False)
    return str(yaml.safe_dump(serialized, default_flow_style=False, sort_keys=False)).rstrip("\n")


def emit_structured_document(
    document: Any,
    *,
    output_format: str,
    rendered: str | None = None,
    err: bool = False,
    nl: bool = True,
) -> None:
    """Emit and register one command-native machine document.

    ``rendered`` lets legacy JSON-only command surfaces preserve their exact
    bytes while still identifying the underlying payload to the root output
    transaction. Error documents stay on stderr and are never registered as a
    successful stdout payload.
    """
    output = (
        rendered if rendered is not None else _render_structured_document(document, output_format)
    )
    emitted_text = f"{output}\n" if nl else output
    emissions = _structured_emissions_var.get()
    if not err and emissions is not None:
        registered_document = _serialize_value(document)
        if output_format == "json":
            registered_document = json.loads(
                output,
                parse_constant=_reject_nonstandard_json_constant,
            )
        emissions.append((registered_document, emitted_text))
    click.echo(output, err=err, nl=nl)


def _write_structured_capture(
    text: str,
    output_format: str,
    emissions: Sequence[tuple[Any, str]],
) -> None:
    rendered = _render_structured_document(
        _structured_document_from_text(text, output_format, emissions),
        output_format,
    )
    sys.stdout.write(f"{rendered}\n")


def _root_output_settings(args: Sequence[str]) -> tuple[str | None, str | None]:
    """Read root output/config values with Click-compatible short clusters."""
    output_format: str | None = None
    config_file: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--" or not arg.startswith("-"):
            break

        if arg in {"--output", "--config", "--region"}:
            value = args[index + 1] if index + 1 < len(args) else None
            if arg == "--output":
                output_format = value
            elif arg == "--config":
                config_file = value
            index += 2
            continue
        if arg.startswith("--output="):
            output_format = arg.partition("=")[2]
            index += 1
            continue
        if arg.startswith("--config="):
            config_file = arg.partition("=")[2]
            index += 1
            continue
        if arg.startswith("--"):
            index += 1
            continue

        cluster = arg[1:]
        position = 0
        consumed_next = False
        while position < len(cluster):
            option = cluster[position]
            if option == "v":
                position += 1
                continue
            if option not in {"o", "c", "r"}:
                break

            attached_value = cluster[position + 1 :].removeprefix("=")
            value = attached_value or (args[index + 1] if index + 1 < len(args) else None)
            consumed_next = not attached_value and value is not None
            if option == "o":
                output_format = value
            elif option == "c":
                config_file = value
            # A value-taking short option consumes the rest of its cluster.
            break
        index += 2 if consumed_next else 1
    return output_format, config_file


def _requested_output_format(args: Sequence[str]) -> str | None:
    """Resolve the root output mode early enough to include eager options."""
    output_format, config_file = _root_output_settings(args)
    if output_format is not None:
        return output_format
    try:
        return get_config(config_file).output_format
    except Exception:
        # Let Click and the root callback surface the original config failure.
        return None


def _shell_completion_requested(complete_var: str | None, prog_name: str | None) -> bool:
    """Keep Click's shell-completion protocol outside output normalization."""
    if complete_var is None:
        detected_name = prog_name or os.path.basename(sys.argv[0])
        complete_name = detected_name.replace("-", "_").replace(".", "_")
        complete_var = f"_{complete_name}_COMPLETE".upper()
    return bool(os.environ.get(complete_var))


class _StdoutCapture:
    """Capture Python and inherited child-process stdout for one invocation."""

    def __init__(self) -> None:
        self.text = ""
        self.raw_bytes: bytes | None = None
        self._stream: Any = None
        self._stream_encoding = "utf-8"
        self._stream_fd: int | None = None
        self._saved_fd: int | None = None
        self._temporary: Any = None
        self._string_buffer: StringIO | None = None
        self._redirect: Any = None
        self._owns_lock = False

    def _release_lock(self) -> None:
        if self._owns_lock:
            self._owns_lock = False
            _STDOUT_CAPTURE_LOCK.release()

    def replay(self) -> None:
        """Replay captured output without applying text newline translation twice."""
        if self.raw_bytes is None or self._stream_fd is None:
            sys.stdout.write(self.text)
            return

        self._stream.flush()
        remaining = memoryview(self.raw_bytes)
        while remaining:
            written = os.write(self._stream_fd, remaining)
            if written <= 0:
                raise OSError("stdout replay made no progress")
            remaining = remaining[written:]

    def __enter__(self) -> _StdoutCapture:
        _STDOUT_CAPTURE_LOCK.acquire()
        self._owns_lock = True
        self._stream = sys.stdout
        self._stream_encoding = getattr(self._stream, "encoding", None) or "utf-8"
        try:
            try:
                self._stream_fd = self._stream.fileno()
                self._stream.flush()
                self._saved_fd = os.dup(self._stream_fd)
                self._temporary = tempfile.TemporaryFile(mode="w+b")
                os.dup2(self._temporary.fileno(), self._stream_fd)
            except AttributeError, OSError, ValueError:
                if self._saved_fd is not None:
                    os.close(self._saved_fd)
                    self._saved_fd = None
                if self._temporary is not None:
                    self._temporary.close()
                    self._temporary = None
                self._stream_fd = None
                self._string_buffer = StringIO()
                self._redirect = redirect_stdout(self._string_buffer)
                self._redirect.__enter__()
            return self
        except BaseException:
            self._release_lock()
            raise

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        cleanup_error: Exception | None = None

        def remember(error: Exception) -> None:
            nonlocal cleanup_error
            if cleanup_error is None:
                cleanup_error = error

        try:
            if self._redirect is not None:
                try:
                    self._redirect.__exit__(exc_type, exc, traceback)
                except Exception as error:
                    remember(error)
                try:
                    assert self._string_buffer is not None
                    self.text = self._string_buffer.getvalue()
                except Exception as error:
                    remember(error)
            else:
                assert self._stream_fd is not None
                assert self._saved_fd is not None
                assert self._temporary is not None
                try:
                    self._stream.flush()
                except Exception as error:
                    remember(error)
                restored = False
                try:
                    os.dup2(self._saved_fd, self._stream_fd)
                    restored = True
                except Exception:
                    # Keep the original descriptor alive and retry once before
                    # giving up; a transient restore failure must not strand
                    # process stdout on the capture file.
                    try:
                        os.dup2(self._saved_fd, self._stream_fd)
                        restored = True
                    except Exception as error:
                        remember(error)
                if restored:
                    try:
                        os.close(self._saved_fd)
                    except Exception as error:
                        remember(error)
                    finally:
                        self._saved_fd = None
                try:
                    self._temporary.seek(0)
                    captured_bytes = self._temporary.read()
                    self.raw_bytes = captured_bytes
                    try:
                        self.text = captured_bytes.decode(
                            self._stream_encoding,
                            errors="replace",
                        )
                    except LookupError:
                        self.text = captured_bytes.decode("utf-8", errors="replace")
                except Exception as error:
                    remember(error)
                finally:
                    if restored:
                        try:
                            self._temporary.close()
                        except Exception as error:
                            remember(error)
                        self._temporary = None
        finally:
            self._release_lock()

        if cleanup_error is not None and exc_type is None:
            raise cleanup_error


class StructuredOutputGroup(click.Group):
    """Root group that commits exactly one document for JSON/YAML success.

    Stdout is buffered only for machine-readable modes; stderr stays live for
    warnings and errors. Non-zero exits and exceptions replay stdout unchanged,
    preserving existing diagnostics and exit codes. Table mode bypasses the
    transaction entirely, including its streaming behavior.
    """

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except click.exceptions.Exit as exc:
            _structured_exit_code_var.set(exc.exit_code)
            raise

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        effective_args = list(sys.argv[1:] if args is None else args)
        if _shell_completion_requested(complete_var, prog_name):
            return super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=standalone_mode,
                windows_expand_args=windows_expand_args,
                **extra,
            )

        output_format = _requested_output_format(effective_args)
        if output_format not in {"json", "yaml"}:
            return super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=standalone_mode,
                windows_expand_args=windows_expand_args,
                **extra,
            )

        emissions: list[tuple[Any, str]] = []
        emissions_token = _structured_emissions_var.set(emissions)
        exit_code_token = _structured_exit_code_var.set(None)
        capture = _StdoutCapture()
        _STDOUT_CAPTURE_LOCK.acquire()
        try:
            try:
                with capture:
                    result = super().main(
                        args=args,
                        prog_name=prog_name,
                        complete_var=complete_var,
                        standalone_mode=standalone_mode,
                        windows_expand_args=windows_expand_args,
                        **extra,
                    )
            except (click.exceptions.Exit, SystemExit) as exc:
                exit_code = getattr(exc, "exit_code", getattr(exc, "code", 1))
                if exit_code in {None, 0}:
                    _write_structured_capture(capture.text, output_format, emissions)
                else:
                    capture.replay()
                raise
            except BaseException:
                capture.replay()
                raise

            captured_exit_code = _structured_exit_code_var.get()
            if captured_exit_code not in {None, 0}:
                capture.replay()
            else:
                _write_structured_capture(capture.text, output_format, emissions)
            return result
        finally:
            try:
                _structured_exit_code_var.reset(exit_code_token)
                _structured_emissions_var.reset(emissions_token)
            finally:
                _STDOUT_CAPTURE_LOCK.release()


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
        """Format data as strict, interoperable JSON."""
        serialized = _serialize_value(data)
        return json.dumps(serialized, indent=2, default=str, allow_nan=False)

    def _format_yaml(self, data: Any) -> str:
        """Format data as YAML."""
        serialized = _serialize_value(data)
        return str(yaml.safe_dump(serialized, default_flow_style=False, sort_keys=False))

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
        """Format and print data, registering native machine documents."""
        rendered = self.format(data, columns)
        if self._format in {"json", "yaml"}:
            emit_structured_document(
                data,
                output_format=self._format,
                rendered=rendered,
            )
        else:
            print(rendered)

    def print_success(self, message: str) -> None:
        """Print a human success message in table mode only."""
        if self._format == "table":
            print(f"✓ {message}")

    def print_error(self, message: str) -> None:
        """Print an error message."""
        print(f"✗ {message}", file=sys.stderr)

    def print_warning(self, message: str) -> None:
        """Print a warning message."""
        print(f"⚠ {message}", file=sys.stderr)

    def print_info(self, message: str) -> None:
        """Print a human informational message in table mode only."""
        if self._format == "table":
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
