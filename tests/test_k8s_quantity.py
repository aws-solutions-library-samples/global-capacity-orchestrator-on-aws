"""``gco.k8s_quantity`` parses Kubernetes resource quantities as the API server does.

The admission gate, the SQS consumer's gate, the health monitor, the Job
metrics endpoint and the synth-time quota checks all read quantities through
this module. Each used to carry its own suffix table, and they disagreed: the
admission gate refused ``cpu: 1`` written as a YAML number and every decimal
CPU, which the API server accepts. These tests pin the grammar of
``resource.Quantity`` (``k8s.io/apimachinery/pkg/api/resource``), the
round-up conversions ``MilliValue()`` and ``Value()`` perform, and the
refusals (booleans, non-finite numbers, fractional devices, negatives).
"""

from __future__ import annotations

import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from gco import k8s_quantity
from gco.k8s_quantity import (
    MAX_QUANTITY,
    cpu_millicores,
    device_count,
    memory_bytes,
    parse_quantity,
)


class TestGrammar:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # Plain numbers, written as strings: integer, decimal, and the
            # leading/trailing-dot and signed forms the grammar allows.
            ("2", Decimal("2")),
            (" 4 ", Decimal("4")),
            ("1.5", Decimal("1.5")),
            (".5", Decimal("0.5")),
            ("1.", Decimal("1")),
            ("+3", Decimal("3")),
            ("-1", Decimal("-1")),
            # Decimal SI suffixes, nano through exa.
            ("2507348n", Decimal("0.002507348")),
            ("2507348u", Decimal("2.507348")),
            ("500m", Decimal("0.5")),
            ("1k", Decimal(1000)),
            ("2M", Decimal(2 * 10**6)),
            ("3G", Decimal(3 * 10**9)),
            ("4T", Decimal(4 * 10**12)),
            ("5P", Decimal(5 * 10**15)),
            ("1E", Decimal(10**18)),
            # Binary SI suffixes, Ki through Ei.
            ("1Ki", Decimal(1024)),
            ("256Mi", Decimal(256 * 2**20)),
            ("1.5Gi", Decimal(3 * 2**29)),
            ("1Ti", Decimal(2**40)),
            ("1Pi", Decimal(2**50)),
            ("1Ei", Decimal(2**60)),
            # Decimal exponents: "E" followed by digits is an exponent, not exa.
            ("1e3", Decimal(1000)),
            ("1E3", Decimal(1000)),
            ("1E+2", Decimal(100)),
            ("129e6", Decimal(129 * 10**6)),
            ("1e-3", Decimal("0.001")),
        ],
    )
    def test_strings(self, value: str, expected: Decimal) -> None:
        assert parse_quantity(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # A manifest can carry a quantity as a YAML or JSON number (the API
            # server unmarshals Quantity from either), and a DynamoDB read
            # hands back Decimal.
            (1, Decimal(1)),
            (2147483648, Decimal(2147483648)),
            (0.5, Decimal("0.5")),
            (0.025, Decimal("0.025")),
            (1e-7, Decimal("1e-7")),
            (Decimal("1E+3"), Decimal(1000)),
            (Decimal("0.25"), Decimal("0.25")),
        ],
    )
    def test_numbers(self, value: object, expected: Decimal) -> None:
        assert parse_quantity(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "2Gib",  # an extra unit letter
            "1ki",  # a lowercase binary prefix
            "1 Gi",  # whitespace inside the quantity
            "1e",  # an exponent without digits
            "e3",  # no number
            "lots",
            "1.5.5",
            "--1",
        ],
    )
    def test_refuses_what_the_api_server_refuses(self, value: str) -> None:
        with pytest.raises(ValueError, match=r"is not a Kubernetes quantity$"):
            parse_quantity(value)

    @pytest.mark.parametrize("value", ["", "   "])
    def test_refuses_an_empty_string(self, value: str) -> None:
        with pytest.raises(ValueError, match=r"^empty resource quantity$"):
            parse_quantity(value)

    @pytest.mark.parametrize(
        "value",
        [
            # Python counts True as an int; a boolean is still not a quantity.
            True,
            False,
            None,
            [],
            {"cpu": 1},
            float("inf"),
            float("nan"),
            Decimal("NaN"),
            Decimal("Infinity"),
        ],
    )
    def test_refuses_values_that_are_not_quantities(self, value: object) -> None:
        with pytest.raises(ValueError, match=r"is not a Kubernetes quantity$"):
            parse_quantity(value)


class TestRange:
    @pytest.mark.parametrize(
        "value",
        ["1e999999999", "9223372036854775808", "8Ei", 1e20, 1e308, "1" + "0" * 5000],
    )
    def test_large_magnitudes_are_capped_as_the_api_server_caps_them(self, value: object) -> None:
        assert parse_quantity(value) == MAX_QUANTITY

    def test_a_large_negative_keeps_its_sign_when_capped(self) -> None:
        assert parse_quantity("-1e999999999") == -MAX_QUANTITY

    def test_a_tiny_positive_value_rounds_up_rather_than_vanishing(self) -> None:
        # The exponent is bounded, so 1e-999999999 is "smaller than anything
        # representable" without underflowing to zero; rounding up then gives
        # the one millicore Quantity.MilliValue() reports.
        assert parse_quantity("1e-999999999") > 0
        assert cpu_millicores("1e-999999999") == 1

    def test_the_cap_is_the_largest_int64(self) -> None:
        assert MAX_QUANTITY == 2**63 - 1


class TestConversions:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("500m", 500),
            ("2", 2000),
            (1, 1000),
            (0.5, 500),
            ("1.5", 1500),
            ("0.0001", 1),  # rounded up, as MilliValue() rounds
            ("2507348n", 3),
            ("-0", 0),
        ],
    )
    def test_cpu_millicores(self, value: object, expected: int) -> None:
        assert cpu_millicores(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1Gi", 2**30),
            ("1.5Gi", 3 * 2**29),
            ("100M", 10**8),
            ("1e9", 10**9),
            (2147483648, 2**31),
            ("0.5", 1),  # half a byte is a byte, as Value() rounds
            ("8Ei", 2**63 - 1),
        ],
    )
    def test_memory_bytes(self, value: object, expected: int) -> None:
        assert memory_bytes(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(1, 1), ("1", 1), ("1.0", 1), (2.0, 2), ("1e0", 1), ("0", 0)],
    )
    def test_device_count(self, value: object, expected: int) -> None:
        assert device_count(value) == expected

    @pytest.mark.parametrize("value", ["0.5", 1.5, "500m"])
    def test_device_count_refuses_a_fraction(self, value: object) -> None:
        # The API server refuses a fractional extended resource; rounding it
        # would count devices the scheduler never sees.
        with pytest.raises(ValueError, match="is not a whole number of devices"):
            device_count(value)

    @pytest.mark.parametrize("convert", [cpu_millicores, memory_bytes, device_count])
    def test_conversions_refuse_a_negative_quantity(self, convert: object) -> None:
        # A negative request would offset a manifest's other containers in a
        # cap total, so the conversions refuse one.
        with pytest.raises(ValueError, match="is negative; a resource quantity cannot be"):
            convert("-1")  # type: ignore[operator]


class TestMessages:
    def test_the_message_names_the_value(self) -> None:
        with pytest.raises(ValueError, match=r"^'2Gib' is not a Kubernetes quantity$"):
            parse_quantity("2Gib")

    def test_a_long_value_is_shortened_in_the_message(self) -> None:
        with pytest.raises(ValueError, match=r"\.\.\. is not a Kubernetes quantity$") as excinfo:
            parse_quantity("9" * 5000 + "x")
        assert len(str(excinfo.value)) < 80


def test_module_is_standard_library_only() -> None:
    # Every service image copies gco/ wholesale, so the parser must not pull a
    # third-party package into a service's import graph.
    source = Path(k8s_quantity.__file__).read_text(encoding="utf-8")
    imports = re.findall(r"^(?:import|from)\s+(\S+)", source, flags=re.MULTILINE)
    assert set(imports) == {"__future__", "math", "re", "decimal"}
    assert all(name in sys.stdlib_module_names for name in imports)
