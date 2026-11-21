"""Kubernetes resource quantities, parsed the way the API server parses them.

One parser behind every quantity GCO reads or compares: the admission gate and
the CLI's pre-check (:mod:`gco.job_admission`), the SQS consumer's gate, the
health monitor's capacity and usage sums, the Job metrics endpoint, the
HyperPod queue quotas, and the synth-time quota checks
(:func:`gco.resource_governance.parse_k8s_quantity`). Each of those used to
carry its own suffix table, and they disagreed: the admission gate refused
``cpu: 1`` written as a YAML number (``'int' object has no attribute
'strip'``) and any decimal CPU such as ``"0.5"``, the SQS gate refused ``Pi``,
the metrics endpoint dropped a pod whose CPU usage metrics-server rendered in
microcores, and the HyperPod validator accepted ``4ki`` but refused ``4Ki``.

A quantity is what ``resource.Quantity`` in ``k8s.io/apimachinery`` accepts
(``pkg/api/resource/quantity.go``): an optionally signed decimal number
(``1``, ``1.5``, ``.5``, ``1.``) followed by nothing, a binary SI suffix
(``Ki Mi Gi Ti Pi Ei``), a decimal SI suffix (``n u m k M G T P E``) or a
decimal exponent (``e3``, ``E-2``). ``E`` alone is exa; ``E`` followed by
digits is an exponent. In a manifest it can also be a YAML or JSON number
(``cpu: 1``, ``memory: 2147483648``, ``cpu: 0.5``): the API server unmarshals
a Quantity from either form, and so does this. A boolean is not a quantity,
although Python counts ``True`` as an ``int``.

Arithmetic is exact (:class:`decimal.Decimal`). The integer conversions round
up, as ``Quantity.MilliValue()`` and ``Quantity.Value()`` do: ``0.0001`` CPU is
``1m`` and half a byte is one byte, so a cap is never under-counted. Like the
API server, a magnitude above ``2**63 - 1`` is capped there.

Standard library only: every service image copies ``gco/`` wholesale, so
anything imported here is imported by every service.
"""

from __future__ import annotations

import math
import re
from decimal import MAX_EMAX, MIN_EMIN, ROUND_CEILING, Context, Decimal

MAX_QUANTITY = Decimal(2**63 - 1)
"""Largest magnitude the API server represents; a larger quantity is capped to it."""

_BINARY_SI = {
    suffix: Decimal(2 ** (10 * power))
    for power, suffix in enumerate(("Ki", "Mi", "Gi", "Ti", "Pi", "Ei"), start=1)
}
_DECIMAL_SI = {
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
}

# The exponent alternative is tried first, so "1E3" is a thousand and a bare
# "1E" falls through to exa. Matched with fullmatch: "2Gib", "1 Gi" and "1e"
# are not quantities, and the API server refuses them too.
_QUANTITY = re.compile(
    r"(?P<number>[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))"
    r"(?P<suffix>[eE][+-]?[0-9]+|[KMGTPE]i|[numkMGTPE])?"
)

# Precision for any quantity the API server represents (2**63 at nano
# resolution is 28 digits), an exponent range wide enough that a tiny positive
# value rounds up to one unit instead of underflowing to zero, and rounding up
# wherever a result has to be rounded.
_CONTEXT = Context(prec=40, Emax=MAX_EMAX, Emin=MIN_EMIN, rounding=ROUND_CEILING)

# Past this an exponent is capped (large) or rounds up to one unit (small)
# whatever its exact value, so bounding it changes no result and keeps the
# scaling inside the context.
_EXPONENT_BOUND = 60

_SHOWN_LENGTH = 40


def _shown(value: object) -> str:
    """``value`` for an error message, bounded so a hostile one stays readable."""
    text = repr(value)
    return text if len(text) <= _SHOWN_LENGTH else text[: _SHOWN_LENGTH - 3] + "..."


def _quantity_text(value: object) -> str:
    """The text of a quantity written as a string or as a YAML, JSON or DynamoDB number."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty resource quantity")
        return text
    if isinstance(value, bool):
        raise ValueError(f"{_shown(value)} is not a Kubernetes quantity")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        # str() is the shortest round-trip form: 0.025 -> "0.025", 1e20 -> "1e+20".
        return str(value)
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)
    raise ValueError(f"{_shown(value)} is not a Kubernetes quantity")


def parse_quantity(value: object) -> Decimal:
    """``value`` as a Kubernetes quantity, in base units (cores, bytes, devices).

    The sign is kept: whether a negative quantity is acceptable is the
    caller's decision (a resource request never is; :func:`cpu_millicores` and
    the other conversions refuse one).

    Raises:
        ValueError: if ``value`` is not a quantity.
    """
    text = _quantity_text(value)
    match = _QUANTITY.fullmatch(text)
    if match is None:
        raise ValueError(f"{_shown(value)} is not a Kubernetes quantity")
    number = Decimal(match["number"])
    suffix = match["suffix"] or ""
    # The suffix tables first: "Ei" and "E" start like an exponent but are not one.
    if suffix in _BINARY_SI:
        quantity = _CONTEXT.multiply(number, _BINARY_SI[suffix])
    elif suffix in _DECIMAL_SI:
        quantity = _CONTEXT.multiply(number, _DECIMAL_SI[suffix])
    elif suffix:
        exponent = max(-_EXPONENT_BOUND, min(_EXPONENT_BOUND, int(suffix[1:])))
        quantity = number.scaleb(exponent, _CONTEXT)
    else:
        quantity = number
    if _CONTEXT.abs(quantity) > MAX_QUANTITY:
        return MAX_QUANTITY.copy_sign(quantity)
    return quantity


def _resource_quantity(value: object) -> Decimal:
    """A quantity a resource request or limit may carry: never negative."""
    quantity = parse_quantity(value)
    if quantity < 0:
        raise ValueError(f"{_shown(value)} is negative; a resource quantity cannot be")
    return quantity


def cpu_millicores(value: object) -> int:
    """A CPU quantity in millicores, rounded up as ``Quantity.MilliValue()`` rounds."""
    millicores = _CONTEXT.multiply(_resource_quantity(value), Decimal(1000))
    return int(millicores.to_integral_value(rounding=ROUND_CEILING))


def memory_bytes(value: object) -> int:
    """A memory or storage quantity in bytes, rounded up as ``Quantity.Value()`` rounds."""
    return int(_resource_quantity(value).to_integral_value(rounding=ROUND_CEILING))


def device_count(value: object) -> int:
    """An extended-resource quantity such as ``nvidia.com/gpu``, as a whole number.

    The API server refuses a fractional extended resource ("must be an
    integer"), so a fraction is an error here rather than a count rounded into
    one the scheduler would never see.
    """
    quantity = _resource_quantity(value)
    whole = quantity.to_integral_value()
    if quantity != whole:
        raise ValueError(f"{_shown(value)} is not a whole number of devices")
    return int(whole)
