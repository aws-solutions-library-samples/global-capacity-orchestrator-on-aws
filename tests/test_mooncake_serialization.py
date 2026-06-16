"""Round-trip behavior of a ``mooncake`` endpoint-spec block through the store.

An endpoint spec carries an optional nested ``mooncake`` block. The store
layer in :mod:`gco.services.inference_store` writes that block to DynamoDB via
``_serialize_for_dynamo`` and reads it back via ``_deserialize_from_dynamo``.
DynamoDB has no integer type — it stores every number as a ``Decimal`` — so a
faithful round-trip has three legs: serialize the block, model DynamoDB's
number-as-``Decimal`` storage, then deserialize.

This module checks that any well-formed ``mooncake`` block survives that round
trip unchanged: every field is present afterwards with an equal value, none is
added or dropped, integer counts come back as integers, and the byte-size
fields authored as base-10 integer decimal strings come back as the same
strings (never coerced through a float into a ``Decimal``). Equality is judged
so that an integer and its decimal-string spelling count as the same value.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli.inference import (
    MOONCAKE_BYTE_SIZE_MAX,
    MOONCAKE_OFFLOAD_TIERS,
    MOONCAKE_PROXY_SCHEDULING,
    MOONCAKE_TRANSFER_PROTOCOLS,
    author_byte_size,
    validate_mooncake_spec,
)
from gco.services.inference_store import (
    _deserialize_from_dynamo,
    _serialize_for_dynamo,
)

# ---------------------------------------------------------------------------
# Modeling DynamoDB's number storage
# ---------------------------------------------------------------------------
#
# ``_serialize_for_dynamo`` keeps Python ``int`` values as ``int`` and renders
# ``float`` values as decimal strings; booleans and strings pass through. The
# real table then stores every number as a ``Decimal``. The helper below
# reproduces that storage leg so the test exercises the same coercion the
# monitor sees on reload: real integers become ``Decimal`` while booleans
# (which are ``int`` subclasses in Python) and strings are left alone, matching
# how DynamoDB keeps a BOOL distinct from a Number.


def _store_as_dynamo(value: Any) -> Any:
    """Recursively turn the serialized structure into what DynamoDB returns."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):  # pragma: no cover - serializer stringifies floats
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _store_as_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_store_as_dynamo(v) for v in value]
    return value


def _round_trip(block: dict[str, Any]) -> dict[str, Any]:
    """Serialize, model DynamoDB storage, then deserialize a ``mooncake`` block."""
    return _deserialize_from_dynamo(_store_as_dynamo(_serialize_for_dynamo(block)))


# ---------------------------------------------------------------------------
# Equality that treats an integer and its decimal-string spelling as equal
# ---------------------------------------------------------------------------


def _normalize(value: Any) -> Any:
    """Canonicalize a value so 5, "5", and Decimal(5) all compare equal.

    A whole number — whether spelled as ``int`` or as a digit-only string —
    collapses to a ``("num", int)`` tag. Booleans stay booleans (they are not
    numbers here), and containers are normalized element-wise. This encodes the
    "an integer and its decimal-string representation are equal" rule the round
    trip is allowed to differ by.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return ("num", int(value))
    if isinstance(value, Decimal):
        return ("num", int(value)) if value == int(value) else ("num", value)
    if isinstance(value, str) and value.isdigit():
        return ("num", int(value))
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Generator for well-formed ``mooncake`` blocks
# ---------------------------------------------------------------------------

_MODES = sorted({"disaggregated", "store", "both"})
_DISAGGREGATED = {"disaggregated", "both"}

_byte_sizes = st.integers(min_value=0, max_value=MOONCAKE_BYTE_SIZE_MAX).map(author_byte_size)
_counts = st.integers(min_value=1, max_value=1000)
_names = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=0,
    max_size=24,
)


@st.composite
def _store_blocks(draw: st.DrawFn) -> dict[str, Any]:
    enabled = draw(st.booleans())
    store: dict[str, Any] = {"enabled": enabled}
    if draw(st.booleans()):
        store["master_server_address"] = draw(_names)
    if draw(st.booleans()):
        store["metadata_server"] = draw(_names)
    if draw(st.booleans()):
        store["protocol"] = draw(st.sampled_from(sorted(MOONCAKE_TRANSFER_PROTOCOLS)))
    if draw(st.booleans()):
        store["device_name"] = draw(_names)
    if draw(st.booleans()):
        store["offload"] = draw(st.sampled_from(sorted(MOONCAKE_OFFLOAD_TIERS)))
    if draw(st.booleans()):
        store["global_segment_size"] = draw(_byte_sizes)
    if draw(st.booleans()):
        store["local_buffer_size"] = draw(_byte_sizes)
    # The cold tier may only be requested while the hot store is enabled.
    if enabled and draw(st.booleans()):
        store["cold_tier_enabled"] = True
    return store


@st.composite
def _transfer_blocks(draw: st.DrawFn) -> dict[str, Any]:
    transfer: dict[str, Any] = {}
    if draw(st.booleans()):
        transfer["protocol"] = draw(st.sampled_from(sorted(MOONCAKE_TRANSFER_PROTOCOLS)))
    if draw(st.booleans()):
        transfer["device_name"] = draw(_names)
    if draw(st.booleans()):
        transfer["num_workers"] = draw(st.integers(min_value=1, max_value=64))
    if draw(st.booleans()):
        transfer["bootstrap_base_port"] = draw(st.integers(min_value=1024, max_value=65535))
    if draw(st.booleans()):
        transfer["abort_request_timeout"] = draw(st.integers(min_value=0, max_value=3600))
    return transfer


@st.composite
def _role_autoscaling(draw: st.DrawFn) -> dict[str, Any]:
    low = draw(st.integers(min_value=1, max_value=50))
    high = draw(st.integers(min_value=low, max_value=low + 50))
    return {"min_replicas": low, "max_replicas": high}


@st.composite
def _mooncake_blocks(draw: st.DrawFn) -> dict[str, Any]:
    mode = draw(st.sampled_from(_MODES))
    block: dict[str, Any] = {"mode": mode}

    if mode in _DISAGGREGATED or draw(st.booleans()):
        block["topology"] = {"prefill": draw(_counts), "decode": draw(_counts)}

    if draw(st.booleans()):
        block["store"] = draw(_store_blocks())
    if draw(st.booleans()):
        block["transfer"] = draw(_transfer_blocks())
    if draw(st.booleans()):
        proxy: dict[str, Any] = {}
        if draw(st.booleans()):
            proxy["image"] = draw(_names)
        if draw(st.booleans()):
            proxy["scheduling"] = draw(st.sampled_from(sorted(MOONCAKE_PROXY_SCHEDULING)))
        if draw(st.booleans()):
            proxy["admin_api_key_secret"] = draw(_names)
        block["proxy"] = proxy

    # Autoscaling is only meaningful for split topologies.
    if mode in _DISAGGREGATED and draw(st.booleans()):
        autoscaling: dict[str, Any] = {"enabled": draw(st.booleans())}
        if draw(st.booleans()):
            autoscaling["prefill"] = draw(_role_autoscaling())
        if draw(st.booleans()):
            autoscaling["decode"] = draw(_role_autoscaling())
        block["autoscaling"] = autoscaling

    return block


# ---------------------------------------------------------------------------
# The round-trip behavior
# ---------------------------------------------------------------------------


@settings(
    max_examples=300,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(block=_mooncake_blocks())
def test_mooncake_block_survives_store_round_trip(block: dict[str, Any]) -> None:
    """A well-formed mooncake block reloads equal to what was written.

    The block is run through serialize → DynamoDB number storage →
    deserialize, and the result must match the original under integer /
    decimal-string equality with no field added or dropped at any depth.
    """
    # The generated block is well-formed: confirm it before asserting on the
    # round trip so a generator drift surfaces as its own clear failure.
    validate_mooncake_spec(block)

    restored = _round_trip(block)

    assert _normalize(restored) == _normalize(block)


@settings(
    max_examples=300,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(block=_mooncake_blocks())
def test_mooncake_round_trip_preserves_field_set(block: dict[str, Any]) -> None:
    """Every nested key present before the round trip is present afterward.

    Beyond value equality, the set of keys at each nesting level is identical,
    so the store neither invents nor drops a field while persisting the block.
    """
    restored = _round_trip(block)

    def _key_shape(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: _key_shape(v) for k, v in sorted(value.items())}
        if isinstance(value, list):
            return [_key_shape(v) for v in value]
        return None

    assert _key_shape(restored) == _key_shape(block)


def test_mooncake_byte_size_strings_are_not_coerced_through_decimal() -> None:
    """A byte-size authored as a digit string reloads as the same string.

    This pins the concrete concern behind authoring sizes as strings: a large
    value must not pass through a float and arrive back as a ``Decimal`` or a
    rounded number. It comes back as the exact decimal string that was stored.
    """
    block = {
        "mode": "store",
        "store": {
            "enabled": True,
            "global_segment_size": author_byte_size(2147483648),
            "local_buffer_size": author_byte_size(MOONCAKE_BYTE_SIZE_MAX),
        },
    }

    restored = _round_trip(block)

    seg = restored["store"]["global_segment_size"]
    buf = restored["store"]["local_buffer_size"]
    assert seg == "2147483648"
    assert isinstance(seg, str)
    assert buf == str(MOONCAKE_BYTE_SIZE_MAX)
    assert isinstance(buf, str)


def test_mooncake_integer_counts_reload_as_integers() -> None:
    """Topology counts written as integers come back as integers, not strings."""
    block = {"mode": "disaggregated", "topology": {"prefill": 2, "decode": 3}}

    restored = _round_trip(block)

    assert restored["topology"]["prefill"] == 2
    assert restored["topology"]["decode"] == 3
    assert isinstance(restored["topology"]["prefill"], int)
    assert isinstance(restored["topology"]["decode"], int)
