"""
Structured JSON logging for GCO services.

Provides a JSON log formatter that outputs structured logs compatible with
CloudWatch Logs Insights queries. Use ``configure_structured_logging()``
at service startup to enable JSON output for all loggers.

Example CloudWatch Insights query:
    fields @timestamp, level, message, cluster_id, region
    | filter level = "ERROR"
    | sort @timestamp desc

Environment Variables:
    LOG_FORMAT: "json" for structured logging, "text" for human-readable (default: json)
    LOG_LEVEL: Logging level (default: INFO)
"""

import json
import logging
import os
import traceback
from datetime import UTC, datetime
from typing import Any


class StructuredJsonFormatter(logging.Formatter):
    """
    Formats log records as single-line JSON objects.

    Each log line includes:
    - timestamp (ISO 8601)
    - level
    - logger (logger name)
    - message
    - Any extra fields passed via the ``extra`` dict

    Exceptions are serialized into an ``exception`` field with type,
    message, and traceback.
    """

    def __init__(self, service_name: str = "gco", **default_fields: Any):
        super().__init__()
        self.service_name = service_name
        self.default_fields = default_fields

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
        }

        # Include default fields (e.g., cluster_id, region)
        log_entry.update(self.default_fields)

        # Include any extra fields passed via logger.info("msg", extra={...})
        # Filter out standard LogRecord attributes
        standard_attrs = {
            "name",
            "msg",
            "args",
            "created",
            "relativeCreated",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "pathname",
            "filename",
            "module",
            "thread",
            "threadName",
            "process",
            "processName",
            "levelname",
            "levelno",
            "msecs",
            "message",
            "taskName",
        }
        for key, value in record.__dict__.items():
            if key not in standard_attrs and not key.startswith("_"):
                log_entry[key] = value

        # Serialize exception info
        if record.exc_info and record.exc_info[1]:
            exc_type, exc_value, exc_tb = record.exc_info
            log_entry["exception"] = {
                "type": exc_type.__name__ if exc_type else "Unknown",
                "message": str(exc_value),
                "traceback": traceback.format_exception(exc_type, exc_value, exc_tb),
            }

        return json.dumps(log_entry, default=str)


def configure_structured_logging(
    service_name: str = "gco",
    level: str | None = None,
    **default_fields: Any,
) -> None:
    """
    Configure the root logger with structured JSON output.

    Call this once at service startup. All existing loggers will inherit
    the JSON formatter.

    Args:
        service_name: Name included in every log line (e.g., "health-monitor").
        level: Log level override. Defaults to LOG_LEVEL env var or INFO.
        **default_fields: Extra fields included in every log line
            (e.g., cluster_id="...", region="us-east-1").
    """
    log_format = os.environ.get("LOG_FORMAT", "json").lower()
    log_level = level or os.environ.get("LOG_LEVEL") or "INFO"

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Remove existing handlers to avoid duplicate output
    root_logger.handlers.clear()

    handler = logging.StreamHandler()

    if log_format == "json":
        handler.setFormatter(StructuredJsonFormatter(service_name=service_name, **default_fields))
    else:
        # Human-readable fallback for local development
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )

    root_logger.addHandler(handler)


#: Unicode characters some log viewers and parsers render as line breaks.
#: Neutralized alongside CR/LF so they cannot forge log entries either:
#: U+0085 NEL, U+2028 LINE SEPARATOR, U+2029 PARAGRAPH SEPARATOR.
_UNICODE_LINE_BREAKS = "\u0085\u2028\u2029"


def sanitize_log_value(value: Any) -> str:
    """
    Sanitize a value for safe inclusion in log messages.

    Neutralizes CR/LF (rendered as the literal two-character sequences
    ``\\r``/``\\n``), Unicode line separators, and other control characters
    that could be used for log injection (log forging), where an attacker
    embeds line breaks or terminal escapes in user input to spoof log
    entries. Printable text — including non-ASCII — passes through unchanged.

    Always call this on untrusted inputs (HTTP path/query params, job IDs,
    namespaces, etc.) before passing them to ``logger.info/warning/error``.

    Args:
        value: Any value; will be coerced to ``str``.

    Returns:
        A single-line-safe string suitable for plain-text log sinks.
    """
    text = str(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    return "".join(
        ch if ord(ch) >= 32 and ord(ch) != 127 and ch not in _UNICODE_LINE_BREAKS else "?"
        for ch in text
    )
