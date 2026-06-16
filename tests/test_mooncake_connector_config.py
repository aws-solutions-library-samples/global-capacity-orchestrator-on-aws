"""KV connector configuration emitted for each worker role.

A disaggregated endpoint wires prefill, decode, and single-instance store pods
together through vLLM's ``--kv-transfer-config``. The
:func:`gco.services.inference_monitor.build_kv_transfer_config` function turns a
``mooncake`` spec block plus a worker role into that connector JSON.

This module checks the contract that holds for every recognized
``(mode, role)`` pair: the function returns a parseable JSON object, and its
``kv_role`` reflects what the role does on the KV path — prefill produces KV
(``kv_producer``), decode consumes it (``kv_consumer``), and a single store
instance both produces and consumes (``kv_both``).
"""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from gco.services.inference_monitor import build_kv_transfer_config

# The roles each mode supports, and the kv_role each role must carry. These
# mirror the connector's own mapping so the test states the expected behavior
# independently of the implementation under test.
_ROLES_BY_MODE = {
    "disaggregated": ("prefill", "decode"),
    "store": ("single",),
    "both": ("prefill", "decode"),
}
_EXPECTED_KV_ROLE = {
    "prefill": "kv_producer",
    "decode": "kv_consumer",
    "single": "kv_both",
}


@st.composite
def _mode_role_pairs(draw: st.DrawFn) -> tuple[str, str]:
    """Draw a recognized ``(mode, role)`` pair the connector accepts."""
    mode = draw(st.sampled_from(sorted(_ROLES_BY_MODE)))
    role = draw(st.sampled_from(_ROLES_BY_MODE[mode]))
    return mode, role


@given(pair=_mode_role_pairs())
def test_connector_config_is_json_with_role_appropriate_kv_role(
    pair: tuple[str, str],
) -> None:
    """Each recognized role yields parseable JSON whose kv_role fits the role.

    For every supported mode and role, the emitted configuration parses as a
    JSON object and its ``kv_role`` is ``kv_producer`` for prefill,
    ``kv_consumer`` for decode, and ``kv_both`` for a single store instance.
    """
    mode, role = pair

    rendered = build_kv_transfer_config({"mode": mode}, role)

    parsed = json.loads(rendered)
    assert isinstance(parsed, dict)
    assert parsed["kv_role"] == _EXPECTED_KV_ROLE[role]
