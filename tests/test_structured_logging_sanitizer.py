"""Tests for ``gco/services/structured_logging.py::sanitize_log_value``.

The sanitizer is the single log-injection (CWE-117) barrier for every
untrusted value the services log. These tests pin the neutralization
contract: no output can introduce a line break (ASCII or Unicode) or a
raw control character into a plain-text log sink, while printable text —
including non-ASCII — passes through unchanged.
"""

from __future__ import annotations

import pytest

from gco.services.structured_logging import sanitize_log_value


class TestLineBreakNeutralization:
    def test_newline_becomes_literal_escape(self) -> None:
        assert sanitize_log_value("a\nb") == "a\\nb"

    def test_carriage_return_becomes_literal_escape(self) -> None:
        assert sanitize_log_value("a\rb") == "a\\rb"

    def test_crlf_forged_entry_stays_on_one_line(self) -> None:
        forged = "image:latest\r\nERROR forged entry admin=true"
        sanitized = sanitize_log_value(forged)
        assert "\n" not in sanitized and "\r" not in sanitized
        assert sanitized == "image:latest\\r\\nERROR forged entry admin=true"

    @pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
    def test_unicode_line_separators_are_neutralized(self, separator: str) -> None:
        sanitized = sanitize_log_value(f"a{separator}b")
        assert separator not in sanitized
        assert sanitized == "a?b"


class TestControlCharacterNeutralization:
    def test_ansi_escape_sequences_cannot_style_log_viewers(self) -> None:
        assert sanitize_log_value("\x1b[31mred\x1b[0m") == "?[31mred?[0m"

    def test_nul_and_del_are_replaced(self) -> None:
        assert sanitize_log_value("a\x00b\x7fc") == "a?b?c"

    def test_tab_is_treated_as_control_character(self) -> None:
        assert sanitize_log_value("a\tb") == "a?b"


class TestPassthrough:
    def test_printable_ascii_is_unchanged(self) -> None:
        value = "docker.io/library/busybox:1.38.0@sha256:abc123"
        assert sanitize_log_value(value) == value

    def test_printable_unicode_is_unchanged(self) -> None:
        assert sanitize_log_value("namespace-émoji-✓") == "namespace-émoji-✓"

    def test_non_string_values_are_coerced(self) -> None:
        assert sanitize_log_value(42) == "42"
        assert sanitize_log_value(None) == "None"

    def test_output_never_contains_raw_control_characters(self) -> None:
        hostile = "".join(chr(code) for code in range(0, 32)) + "\x7f\u0085\u2028\u2029"
        sanitized = sanitize_log_value(hostile)
        assert all(ord(ch) >= 32 and ord(ch) != 127 for ch in sanitized)
