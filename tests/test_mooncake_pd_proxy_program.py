"""Request-shaping contract of the Mooncake PD proxy program.

The proxy turns one client request into a prefill priming call and a decode
call. These checks pin that shaping: prefill is asked for a single token and to
export its KV (``do_remote_decode``), decode relays whatever transfer params
prefill returned and asks to import them (``do_remote_prefill``), the original
request is never mutated, and only the OpenAI generation paths are
disaggregated.
"""

from __future__ import annotations

import gco.services.mooncake_pd_proxy as proxy


def test_prefill_priming_asks_for_one_token_and_remote_decode() -> None:
    body = {"model": "m", "prompt": "hi", "max_tokens": 128, "stream": True}
    pf = proxy._prefill_body(body)

    assert pf["max_tokens"] == 1
    assert pf["stream"] is False
    assert pf["kv_transfer_params"]["do_remote_decode"] is True
    assert pf["kv_transfer_params"]["do_remote_prefill"] is False
    # The caller's body is left untouched.
    assert body["max_tokens"] == 128
    assert body["stream"] is True


def test_prefill_priming_caps_chat_completion_tokens() -> None:
    pf = proxy._prefill_body({"messages": [], "max_completion_tokens": 64})
    assert pf["max_completion_tokens"] == 1


def test_decode_relays_prefill_transfer_params() -> None:
    body = {"model": "m", "prompt": "hi", "max_tokens": 128}
    dc = proxy._decode_body(body, {"remote_block_ids": [1, 2], "remote_engine_id": "e"})

    assert dc["max_tokens"] == 128
    assert dc["kv_transfer_params"]["do_remote_prefill"] is True
    assert dc["kv_transfer_params"]["do_remote_decode"] is False
    # Inner connector fields are relayed verbatim (pass-through, not interpreted).
    assert dc["kv_transfer_params"]["remote_block_ids"] == [1, 2]
    assert dc["kv_transfer_params"]["remote_engine_id"] == "e"


def test_decode_serves_directly_when_prefill_returns_no_params() -> None:
    # If priming returns nothing, decode still serves the request correctly.
    dc = proxy._decode_body({"model": "m", "prompt": "hi"}, {})
    assert "kv_transfer_params" not in dc


def test_only_generation_paths_are_disaggregated() -> None:
    assert proxy._is_serving_path("/inference/ep/v1/completions")
    assert proxy._is_serving_path("/inference/ep/v1/chat/completions")
    assert proxy._is_serving_path("/inference/ep/v1/embeddings")
    assert not proxy._is_serving_path("/inference/ep/v1/models")
    assert not proxy._is_serving_path("/health")
