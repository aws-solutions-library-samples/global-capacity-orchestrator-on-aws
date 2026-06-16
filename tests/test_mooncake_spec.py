"""Tests for the Mooncake endpoint-spec shape, constants, and byte-size helper.

These exercise the additive ``mooncake`` block surface in ``cli/inference.py``:
the enumerated vocabularies, the byte-size bounds, and the
``author_byte_size`` helper that renders sizes as canonical base-10 integer
decimal strings for a clean DynamoDB round-trip.
"""

from __future__ import annotations

import pytest

from cli.inference import (
    MOONCAKE_BYTE_SIZE_MAX,
    MOONCAKE_BYTE_SIZE_MIN,
    MOONCAKE_MODES,
    MOONCAKE_OFFLOAD_TIERS,
    MOONCAKE_PROXY_SCHEDULING,
    MOONCAKE_TRANSFER_PROTOCOLS,
    author_byte_size,
)


class TestMooncakeConstants:
    def test_modes_are_the_three_known_modes(self):
        assert {"disaggregated", "store", "both"} == MOONCAKE_MODES

    def test_transfer_protocols(self):
        assert {"rdma", "tcp"} == MOONCAKE_TRANSFER_PROTOCOLS

    def test_offload_tiers(self):
        assert {"cpu", "disk", "none"} == MOONCAKE_OFFLOAD_TIERS

    def test_proxy_scheduling(self):
        assert {"round_robin"} == MOONCAKE_PROXY_SCHEDULING

    def test_byte_size_bounds_are_signed_64_bit_range(self):
        assert MOONCAKE_BYTE_SIZE_MIN == 0
        assert MOONCAKE_BYTE_SIZE_MAX == 9223372036854775807


class TestAuthorByteSize:
    def test_authors_int_as_decimal_string(self):
        assert author_byte_size(2147483648) == "2147483648"

    def test_authors_zero(self):
        assert author_byte_size(0) == "0"

    def test_authors_ceiling(self):
        assert author_byte_size(MOONCAKE_BYTE_SIZE_MAX) == str(MOONCAKE_BYTE_SIZE_MAX)

    def test_normalizes_digit_string_input(self):
        assert author_byte_size("0002147483648") == "2147483648"

    def test_strips_surrounding_whitespace(self):
        assert author_byte_size("  1024  ") == "1024"

    def test_output_is_digits_only(self):
        out = author_byte_size(10**18)
        assert out.isdigit()
        assert "." not in out and "e" not in out.lower()

    @pytest.mark.parametrize("bad", [-1, MOONCAKE_BYTE_SIZE_MAX + 1])
    def test_rejects_out_of_range_int(self, bad):
        with pytest.raises(ValueError):
            author_byte_size(bad)

    @pytest.mark.parametrize("bad", ["-1", "1.5", "2e9", "0x10", "", "   ", "abc", "1_000"])
    def test_rejects_non_base10_integer_strings(self, bad):
        with pytest.raises(ValueError):
            author_byte_size(bad)

    @pytest.mark.parametrize("bad", [True, False])
    def test_rejects_bool(self, bad):
        with pytest.raises(ValueError):
            author_byte_size(bad)

    @pytest.mark.parametrize("bad", [1.0, 2.5, None, [1], {"x": 1}])
    def test_rejects_non_integer_types(self, bad):
        with pytest.raises(ValueError):
            author_byte_size(bad)
