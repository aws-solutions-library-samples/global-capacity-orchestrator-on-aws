"""Property-based tests for the Mission sampling prompt builder.

The :class:`mission.sampling.SamplingPrompt` builder is the
deterministic prompt-assembly half of the advisory LLM path. Backends
and orchestration land alongside it in subsequent commits; this file
pins down the four invariants that guarantee the rest of the pipe
stays sound:

* :func:`test_assemble_is_deterministic` — same inputs produce a
  byte-identical output. This is the property every downstream
  prompt-replay test relies on.
* :func:`test_assemble_respects_byte_cap` — the rendered prompt is
  never larger than :data:`mission.sampling.PROMPT_BYTE_BUDGET` UTF-8
  bytes, even when the caller passes Iteration histories large enough
  to bust the cap.
* :func:`test_observation_field_truncation_threshold` — an Observation
  field exactly :data:`OBSERVATION_FIELD_BYTE_CAP` bytes long is **not**
  truncated; one byte more is. The truncated field has length
  :data:`OBSERVATION_FIELD_TRUNCATE_TO` bytes plus the marker, and the
  original byte length is recorded under ``_original_bytes``.
* :func:`test_oldest_iterations_dropped_first_when_over_budget` — when
  the cap forces a drop, the oldest iteration disappears from the
  output first; the most recent surviving iterations are present
  verbatim.
* :func:`test_assemble_final_lessons_uses_distinct_schema` — the
  Final_Report path emits :data:`FINAL_LESSONS_SCHEMA` keys, not the
  Strategy_Revision schema's keys.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Mirror the path-injection pattern used by every other Mission test:
# ``mcp/run_mcp.py`` adds ``mcp/`` to ``sys.path`` at runtime, so test
# files have to do the same before the imports below resolve.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from mission import sampling  # noqa: E402
from mission.sampling import (  # noqa: E402
    FINAL_LESSONS_SCHEMA,
    OBSERVATION_FIELD_BYTE_CAP,
    OBSERVATION_FIELD_TRUNCATE_TO,
    PROMPT_BYTE_BUDGET,
    STRATEGY_REVISION_SCHEMA,
    TRUNCATION_MARKER,
    SamplingPrompt,
)

_PBT_SETTINGS = settings(
    max_examples=40,
    deadline=4000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_observation(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a minimum well-formed Observation dict for tests."""
    obs: dict[str, Any] = {
        "tool_results": [],
        "metrics": {},
        "events": [],
        "phase_started_at": "2025-01-01T00:00:00+00:00",
        "phase_ended_at": "2025-01-01T00:00:00+00:00",
    }
    if extra:
        obs.update(extra)
    return obs


def _make_iteration(
    *,
    iteration_index: int = 0,
    observation: dict[str, Any] | None = None,
    strategy: dict[str, Any] | None = None,
    verdict: str = "continue",
    verdict_reason: str = "in_progress",
) -> dict[str, Any]:
    """Build a minimum well-formed IterationRecord dict for tests."""
    return {
        "iteration_index": iteration_index,
        "started_at": "2025-01-01T00:00:00+00:00",
        "ended_at": "2025-01-01T00:00:01+00:00",
        "phases": [],
        "strategy": strategy or {"tool_calls": [{"tool_name": "noop", "args": {}}]},
        "observation": observation if observation is not None else _make_observation(),
        "criteria_evaluation": [],
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "checkpoint_evaluated": True,
    }


def _make_prompt(
    *,
    directive: str = "Train the model until validation loss < 0.1.",
    success_criteria: list[dict[str, Any]] | None = None,
    criteria_status: list[dict[str, Any]] | None = None,
    recent_iterations: list[dict[str, Any]] | None = None,
    tool_allowlist: list[str] | None = None,
    tool_docstrings: dict[str, str] | None = None,
    remaining_iterations: int = 7,
    remaining_wall_clock_secs: float | None = 1800.0,
    allow_scripts: bool = False,
) -> SamplingPrompt:
    """Construct a default SamplingPrompt with overridable fields."""
    return SamplingPrompt(
        directive=directive,
        success_criteria=success_criteria
        or [
            {
                "criterion_id": "loss_below_target",
                "kind": "metric_threshold",
                "required": True,
                "metric": "val_loss",
                "op": "<",
                "target": 0.1,
            }
        ],
        criteria_status=criteria_status
        or [
            {
                "criterion_id": "loss_below_target",
                "status": "unmet",
                "evidence": {"val_loss": 0.42},
                "evaluated_at": "2025-01-01T00:00:00+00:00",
            }
        ],
        recent_iterations=recent_iterations or [],
        tool_allowlist=tool_allowlist or ["submit_job_sqs", "find_examples"],
        tool_docstrings=tool_docstrings
        or {
            "submit_job_sqs": "Submit a job to the SQS queue.",
            "find_examples": "Search the example catalog.",
        },
        remaining_iterations=remaining_iterations,
        remaining_wall_clock_secs=remaining_wall_clock_secs,
        allow_scripts=allow_scripts,
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


# Hypothesis strategies for the variable inputs in the determinism test.
_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=64,
)


@st.composite
def _observation_strategy(draw: st.DrawFn) -> dict[str, Any]:
    """Draw an Observation dict with a free metrics block and event list."""
    return {
        "tool_results": draw(st.lists(st.integers(min_value=-100, max_value=100), max_size=3)),
        "metrics": draw(
            st.dictionaries(_text, st.integers(min_value=0, max_value=100), max_size=3)
        ),
        "events": draw(st.lists(st.fixed_dictionaries({"event_name": _text}), max_size=2)),
        "phase_started_at": "2025-01-01T00:00:00+00:00",
        "phase_ended_at": "2025-01-01T00:00:01+00:00",
    }


_nonempty_text = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=127),
    min_size=1,
    max_size=12,
)


@st.composite
def _iteration_strategy(draw: st.DrawFn, iteration_index: int = 0) -> dict[str, Any]:
    """Draw an IterationRecord with a varied Observation and strategy."""
    return _make_iteration(
        iteration_index=iteration_index,
        observation=draw(_observation_strategy()),
        strategy={
            "tool_calls": [{"tool_name": draw(_nonempty_text), "args": {}}],
        },
    )


@_PBT_SETTINGS
@given(
    directive=_text.filter(lambda s: s.strip() != ""),
    iteration_count=st.integers(min_value=0, max_value=4),
    remaining_iters=st.integers(min_value=0, max_value=20),
)
def test_assemble_is_deterministic(
    directive: str,
    iteration_count: int,
    remaining_iters: int,
) -> None:
    """Same SamplingPrompt input → byte-identical assemble() output."""
    iterations = [_make_iteration(iteration_index=i) for i in range(iteration_count)]
    p = _make_prompt(
        directive=directive,
        recent_iterations=iterations,
        remaining_iterations=remaining_iters,
    )
    first = p.assemble()
    second = p.assemble()
    assert first == second
    # Also: a freshly-constructed identical prompt produces the same output.
    p2 = _make_prompt(
        directive=directive,
        recent_iterations=iterations,
        remaining_iterations=remaining_iters,
    )
    assert p2.assemble() == first


# ---------------------------------------------------------------------------
# Byte cap
# ---------------------------------------------------------------------------


@_PBT_SETTINGS
@given(
    iteration_count=st.integers(min_value=0, max_value=8),
    payload_size=st.integers(min_value=0, max_value=20_000),
)
def test_assemble_respects_byte_cap(
    iteration_count: int,
    payload_size: int,
) -> None:
    """No matter how large the input, assemble() output is ≤ PROMPT_BYTE_BUDGET."""
    # Each iteration carries a chunky tool_results payload so the
    # total prompt size — without the size guard — would dwarf the
    # cap. The guard must drop oldest iterations until it fits.
    iterations = [
        _make_iteration(
            iteration_index=i,
            observation=_make_observation({"tool_results": ["x" * payload_size]}),
        )
        for i in range(iteration_count)
    ]
    p = _make_prompt(recent_iterations=iterations)
    text = p.assemble()
    assert len(text.encode("utf-8")) <= PROMPT_BYTE_BUDGET, (
        f"prompt {len(text.encode('utf-8'))} bytes exceeded cap {PROMPT_BYTE_BUDGET}"
    )

    final_text = p.assemble_final_lessons()
    assert len(final_text.encode("utf-8")) <= PROMPT_BYTE_BUDGET


# ---------------------------------------------------------------------------
# Per-field truncation threshold
# ---------------------------------------------------------------------------


def test_observation_field_truncation_threshold() -> None:
    """A field exactly OBSERVATION_FIELD_BYTE_CAP bytes is kept verbatim;
    one byte over is truncated."""
    # JSON-serialise of a pure ASCII string `s` is `"s"` — length n + 2.
    # Pick the inner string lengths so the serialised form lands at the
    # boundary cleanly.
    at_cap_inner = OBSERVATION_FIELD_BYTE_CAP - 2  # serialised len == cap
    over_cap_inner = OBSERVATION_FIELD_BYTE_CAP - 1  # serialised len == cap + 1

    # ---- exactly at cap: not truncated ----
    iteration_at = _make_iteration(
        observation=_make_observation({"tool_results": "a" * at_cap_inner})
    )
    p_at = _make_prompt(recent_iterations=[iteration_at])
    text_at = p_at.assemble()
    # The original payload survives verbatim; no marker, no _original_bytes map.
    assert "a" * at_cap_inner in text_at
    assert TRUNCATION_MARKER not in text_at
    assert "_original_bytes" not in text_at

    # ---- one byte over: truncated ----
    iteration_over = _make_iteration(
        observation=_make_observation({"tool_results": "a" * over_cap_inner})
    )
    p_over = _make_prompt(recent_iterations=[iteration_over])
    text_over = p_over.assemble()
    assert TRUNCATION_MARKER in text_over
    assert "_original_bytes" in text_over
    # The original byte length recorded under _original_bytes is the
    # serialised JSON byte length (i.e. inner length + 2 for the quotes).
    assert str(over_cap_inner + 2) in text_over


# ---------------------------------------------------------------------------
# Drop-oldest-first when over budget
# ---------------------------------------------------------------------------


def test_oldest_iterations_dropped_first_when_over_budget() -> None:
    """When the cap forces a drop, the oldest iteration disappears first."""
    # Each iteration carries oversized content in **multiple** Observation
    # fields. Per-field truncation caps each field at ~2 KB post-summary,
    # so four oversized fields per iteration give ~8 KB per-iteration
    # footprint after truncation. With a chunky directive the assembled
    # prompt would land well above the 32 KB cap; the drop loop must
    # peel iterations off the front until it fits.
    iterations: list[dict[str, Any]] = []
    big_blob = "z" * (OBSERVATION_FIELD_BYTE_CAP * 3)
    for i in range(5):
        marker = f"ITER-MARKER-{i:02d}"
        # Each marker is unique per iteration so the drop ordering test
        # below can detect which iterations survived.
        iterations.append(
            _make_iteration(
                iteration_index=i,
                observation=_make_observation(
                    {
                        "tool_results": [marker + big_blob],
                        "metrics": {"big_metric_field": marker + big_blob},
                        "events": [{"event_name": marker + big_blob}],
                        "errors": [{"message": marker + big_blob}],
                    }
                ),
            )
        )
    # A bulky directive contributes another ~12 KB so the budget bites
    # even after per-field truncation runs.
    bulky_directive = "A" * 12_000
    p = _make_prompt(directive=bulky_directive, recent_iterations=iterations)
    text = p.assemble()

    assert len(text.encode("utf-8")) <= PROMPT_BYTE_BUDGET

    # The most recent iteration's marker survives.
    assert "ITER-MARKER-04" in text
    # At least one of the older iterations was dropped — assert by
    # finding the lowest surviving index, then asserting every index
    # below it is absent.
    surviving = [i for i in range(5) if f"ITER-MARKER-{i:02d}" in text]
    assert surviving, "all iterations dropped — drop policy too aggressive"
    lowest = min(surviving)
    for i in range(lowest):
        assert f"ITER-MARKER-{i:02d}" not in text, (
            f"iteration {i} survived but iteration {lowest} (newer) is also "
            "present — drop policy did not honour oldest-first ordering"
        )
    # And the surviving set is contiguous from `lowest` to 4.
    assert surviving == list(range(lowest, 5))
    # At least one drop must have occurred to force the property to bite.
    assert lowest >= 1


# ---------------------------------------------------------------------------
# Final lessons schema
# ---------------------------------------------------------------------------


def test_assemble_final_lessons_uses_distinct_schema() -> None:
    """The Final_Report path emits FINAL_LESSONS_SCHEMA, not the strategy schema."""
    p = _make_prompt(
        recent_iterations=[
            _make_iteration(iteration_index=0, verdict="complete", verdict_reason="criteria_met")
        ]
    )
    text = p.assemble_final_lessons()

    # Final_Report keys are present.
    assert "lessons" in text
    assert "recommended_followups" in text
    # Strategy_Revision-only keys are absent.
    assert "revision_rationale" not in text
    assert "next_strategy" not in text
    # The schema title pinned by the constants is the one rendered.
    assert FINAL_LESSONS_SCHEMA["title"] in text
    assert STRATEGY_REVISION_SCHEMA["title"] not in text


def test_assemble_uses_strategy_revision_schema() -> None:
    """Sanity-check the inverse of the final-lessons test."""
    p = _make_prompt()
    text = p.assemble()
    assert "revision_rationale" in text
    assert "next_strategy" in text
    assert STRATEGY_REVISION_SCHEMA["title"] in text
    # Final_Report-only schema title is absent.
    assert FINAL_LESSONS_SCHEMA["title"] not in text


# ---------------------------------------------------------------------------
# Light unit-test coverage of the helper paths
# ---------------------------------------------------------------------------


def test_predicate_parsed_ast_stripped_from_criteria_block() -> None:
    """Private cached AST under ``_parsed_ast`` does not leak into the prompt."""
    # The validators stash a parsed AST under this private key; the
    # prompt builder must never try to render it (it is not JSON-safe).
    criteria = [
        {
            "criterion_id": "c1",
            "kind": "predicate",
            "required": True,
            "expression": "obs['x'] > 0",
            "_parsed_ast": object(),  # unserialisable sentinel
        }
    ]
    p = _make_prompt(success_criteria=criteria, criteria_status=[])
    text = p.assemble()  # must not raise

    assert "_parsed_ast" not in text
    # The status block fills in inconclusive when no matching status exists.
    assert "inconclusive" in text


def test_more_than_five_iterations_clipped_to_last_five() -> None:
    """The builder slices defensively when the caller passes >5 iterations."""
    iterations = [_make_iteration(iteration_index=i) for i in range(8)]
    # Tag the eight iterations so we can detect which survive.
    for it in iterations:
        it["strategy"] = {
            "tool_calls": [{"tool_name": f"marker_{it['iteration_index']}", "args": {}}]
        }

    p = _make_prompt(recent_iterations=iterations)
    text = p.assemble()
    # Iterations 3, 4, 5, 6, 7 must be present; 0, 1, 2 must not.
    for i in range(3, 8):
        assert f"marker_{i}" in text, f"iteration {i} should survive"
    for i in range(3):
        assert f"marker_{i}" not in text, f"iteration {i} should have been clipped"


def test_strategy_revision_schema_round_trips_as_json() -> None:
    """The embedded schemas are valid JSON."""
    # If the schema is rendered into the prompt verbatim, json.dumps
    # / json.loads must round-trip it cleanly.
    encoded = json.dumps(STRATEGY_REVISION_SCHEMA)
    assert json.loads(encoded) == STRATEGY_REVISION_SCHEMA
    encoded_final = json.dumps(FINAL_LESSONS_SCHEMA)
    assert json.loads(encoded_final) == FINAL_LESSONS_SCHEMA


def test_module_exports_constants() -> None:
    """The tunables are accessible as module-level constants for the tests."""
    assert sampling.OBSERVATION_FIELD_BYTE_CAP == OBSERVATION_FIELD_BYTE_CAP
    assert sampling.OBSERVATION_FIELD_TRUNCATE_TO == OBSERVATION_FIELD_TRUNCATE_TO
    assert sampling.PROMPT_BYTE_BUDGET == PROMPT_BYTE_BUDGET
    assert sampling.TRUNCATION_MARKER == TRUNCATION_MARKER
    assert OBSERVATION_FIELD_BYTE_CAP > OBSERVATION_FIELD_TRUNCATE_TO
    assert PROMPT_BYTE_BUDGET > OBSERVATION_FIELD_BYTE_CAP


# ---------------------------------------------------------------------------
# SamplingBackend protocol + SamplingTransportError
# ---------------------------------------------------------------------------


from mission.sampling import (  # noqa: E402
    SamplingBackend,
    SamplingTransportError,
)


def test_sampling_backend_protocol_runtime_checkable() -> None:
    """A class with the three required members satisfies isinstance(...,
    SamplingBackend); a class missing any of them does not."""

    class _GoodBackend:
        backend_name = "mcp"
        model_id = "stub-model"

        async def sample(self, prompt: SamplingPrompt) -> str:
            return "{}"

    class _MissingModelId:
        backend_name = "bedrock"

        async def sample(self, prompt: SamplingPrompt) -> str:
            return "{}"

    assert isinstance(_GoodBackend(), SamplingBackend) is True
    assert isinstance(_MissingModelId(), SamplingBackend) is False


def test_sampling_transport_error_code_attribute() -> None:
    """A bare-code construction exposes the code verbatim and stringifies
    to just the code."""
    err = SamplingTransportError("mcp_unavailable")
    assert err.code == "mcp_unavailable"
    assert err.message is None
    assert str(err) == "mcp_unavailable"


def test_sampling_transport_error_with_message() -> None:
    """When both code and message are supplied, str(err) shows both."""
    err = SamplingTransportError(
        "bedrock_AccessDeniedException",
        "operator does not have bedrock:InvokeModel",
    )
    assert err.code == "bedrock_AccessDeniedException"
    assert err.message == "operator does not have bedrock:InvokeModel"
    rendered = str(err)
    assert "bedrock_AccessDeniedException" in rendered
    assert "operator does not have bedrock:InvokeModel" in rendered


# ---------------------------------------------------------------------------
# MCPSamplingBackend
# ---------------------------------------------------------------------------


import asyncio  # noqa: E402

from mission.sampling import MCPSamplingBackend  # noqa: E402


def _run(coro):  # type: ignore[no-untyped-def]
    """Run an async coroutine synchronously inside a sync test."""
    return asyncio.run(coro)


class _FakeCtxReturningString:
    """Fake Context whose ``sample`` returns a plain string."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def sample(self, text: str, **kwargs: Any) -> str:
        self.calls.append({"text": text, "kwargs": kwargs})
        return '{"revision_rationale": "ok", "next_strategy": {}, "confidence": 0.5}'


class _TextResult:
    """Stand-in for FastMCP's response objects with a ``.text`` attribute."""

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeCtxReturningTextObj:
    """Fake Context whose ``sample`` returns an object with ``.text``."""

    async def sample(self, text: str, **kwargs: Any) -> Any:  # noqa: ARG002
        return _TextResult("response-from-text-attr")


class _FakeCtxReturningInt:
    """Fake Context whose ``sample`` returns a non-string, non-text-attr value."""

    async def sample(self, text: str, **kwargs: Any) -> Any:  # noqa: ARG002
        return 42


class _FakeCtxRaising:
    """Fake Context whose ``sample`` raises a runtime error."""

    async def sample(self, text: str, **kwargs: Any) -> str:  # noqa: ARG002
        raise RuntimeError("boom")


class _FakeCtxOnlySnakeCase:
    """Fake Context that only accepts ``model_preferences`` (snake_case).

    The first call attempt with ``modelPreferences=`` raises ``TypeError``
    so the backend's compatibility shim retries with the snake_case
    spelling. The second call records which kwarg was used.
    """

    def __init__(self) -> None:
        self.last_kwarg_name: str | None = None

    async def sample(
        self,
        text: str,
        *,
        model_preferences: Any = None,
        **kwargs: Any,
    ) -> str:
        if "modelPreferences" in kwargs:
            # FastMCP versions that don't recognise the camelCase form
            # raise ``TypeError`` for the unknown keyword. Mirror that.
            raise TypeError("sample() got an unexpected keyword argument 'modelPreferences'")
        self.last_kwarg_name = "model_preferences"
        return "snake-case-ok"


class _FakeCtxRecordingKwargs:
    """Fake Context that just records the kwargs it was called with."""

    def __init__(self) -> None:
        self.captured_kwargs: dict[str, Any] | None = None

    async def sample(self, text: str, **kwargs: Any) -> str:  # noqa: ARG002
        self.captured_kwargs = dict(kwargs)
        return "no-prefs-ok"


def test_mcp_backend_used_path() -> None:
    """A bare ctx returning a string flows straight through the backend."""
    ctx = _FakeCtxReturningString()
    backend = MCPSamplingBackend(ctx, model_id="test-model")
    prompt = _make_prompt()
    out = _run(backend.sample(prompt))
    assert isinstance(out, str)
    assert "revision_rationale" in out
    assert backend.backend_name == "mcp"
    assert backend.model_id == "test-model"
    # The ctx received the assembled prompt verbatim.
    assert len(ctx.calls) == 1
    assert ctx.calls[0]["text"] == prompt.assemble()


def test_mcp_backend_returns_object_with_text_attr() -> None:
    """An object exposing ``.text`` is unwrapped into the string."""
    ctx = _FakeCtxReturningTextObj()
    backend = MCPSamplingBackend(ctx)
    out = _run(backend.sample(_make_prompt()))
    assert out == "response-from-text-attr"


def test_mcp_backend_returns_unexpected_type_raises_transport_error() -> None:
    """A non-string, non-text-attr return value yields a transport error."""
    ctx = _FakeCtxReturningInt()
    backend = MCPSamplingBackend(ctx)
    try:
        _run(backend.sample(_make_prompt()))
    except SamplingTransportError as err:
        assert err.code == "mcp_unexpected_response_type"
        assert err.message is not None
        assert "int" in err.message
    else:
        raise AssertionError("expected SamplingTransportError")


def test_mcp_backend_transport_error_wraps_underlying_exception() -> None:
    """Transport-level exceptions are re-raised with code ``mcp_<ClassName>``."""
    ctx = _FakeCtxRaising()
    backend = MCPSamplingBackend(ctx)
    try:
        _run(backend.sample(_make_prompt()))
    except SamplingTransportError as err:
        assert err.code == "mcp_RuntimeError"
        # The original RuntimeError is preserved on the cause chain.
        assert isinstance(err.__cause__, RuntimeError)
        assert str(err.__cause__) == "boom"
    else:
        raise AssertionError("expected SamplingTransportError")


def test_mcp_backend_falls_back_to_snake_case_pref_kwarg() -> None:
    """When ctx rejects ``modelPreferences``, the backend retries with
    ``model_preferences``."""
    ctx = _FakeCtxOnlySnakeCase()
    backend = MCPSamplingBackend(ctx, prefs={"hints": ["claude"]})
    out = _run(backend.sample(_make_prompt()))
    assert out == "snake-case-ok"
    assert ctx.last_kwarg_name == "model_preferences"


def test_mcp_backend_omits_prefs_when_none() -> None:
    """A ``prefs=None`` backend never passes a preferences kwarg to ctx."""
    ctx = _FakeCtxRecordingKwargs()
    backend = MCPSamplingBackend(ctx, prefs=None)
    out = _run(backend.sample(_make_prompt()))
    assert out == "no-prefs-ok"
    assert ctx.captured_kwargs == {}
    assert "modelPreferences" not in (ctx.captured_kwargs or {})
    assert "model_preferences" not in (ctx.captured_kwargs or {})


def test_mcp_backend_satisfies_protocol() -> None:
    """An :class:`MCPSamplingBackend` instance is a SamplingBackend."""
    ctx = _FakeCtxReturningString()
    backend = MCPSamplingBackend(ctx, model_id="m")
    assert isinstance(backend, SamplingBackend) is True


# ---------------------------------------------------------------------------
# BedrockSamplingBackend
# ---------------------------------------------------------------------------


import unittest.mock as mock  # noqa: E402

from mission.sampling import (  # noqa: E402
    BEDROCK_MAX_TOKENS,
    BEDROCK_TEMPERATURE,
    DEFAULT_BEDROCK_MODEL_ID,
    DEFAULT_BEDROCK_REGION,
    ENV_BEDROCK_MODEL_ID,
    ENV_BEDROCK_REGION,
    BedrockSamplingBackend,
)


class _FakeBedrockClient:
    """Stand-in for the ``bedrock-runtime`` client.

    Records every ``converse`` call's kwargs and returns either a
    canned response dict or raises a queued exception.
    """

    def __init__(
        self,
        *,
        response: Any | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self._response = response
        self._raises = raises
        self.converse_calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> Any:
        self.converse_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._response


def _well_shaped_response(text: str = "ok") -> dict[str, Any]:
    """Build a minimal Converse response with the expected shape."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}],
            }
        }
    }


def _patch_boto3_session(client: Any) -> mock._patch:
    """Patch the lazy ``boto3`` import in ``mission.sampling._get_client``.

    The backend imports ``boto3`` *inside* ``_get_client``, so the
    fixture has to inject a fake module into ``sys.modules`` before the
    method runs. ``mock.patch.dict`` on ``sys.modules`` is the standard
    pattern for this.
    """
    fake_session = mock.MagicMock()
    fake_session.client.return_value = client
    fake_boto3 = mock.MagicMock()
    fake_boto3.Session.return_value = fake_session
    return mock.patch.dict("sys.modules", {"boto3": fake_boto3})


def test_bedrock_backend_resolves_defaults(monkeypatch) -> None:
    """No env vars, no constructor args: defaults flow through."""
    monkeypatch.delenv(ENV_BEDROCK_MODEL_ID, raising=False)
    monkeypatch.delenv(ENV_BEDROCK_REGION, raising=False)

    backend = BedrockSamplingBackend()
    assert backend.model_id == DEFAULT_BEDROCK_MODEL_ID
    assert backend._region == DEFAULT_BEDROCK_REGION


def test_bedrock_backend_resolves_from_env(monkeypatch) -> None:
    """Env vars override the module-level defaults."""
    monkeypatch.setenv(ENV_BEDROCK_MODEL_ID, "anthropic.test-model-v1")
    monkeypatch.setenv(ENV_BEDROCK_REGION, "eu-west-2")

    backend = BedrockSamplingBackend()
    assert backend.model_id == "anthropic.test-model-v1"
    assert backend._region == "eu-west-2"


def test_bedrock_backend_constructor_args_override_env(monkeypatch) -> None:
    """Constructor arguments win over env vars."""
    monkeypatch.setenv(ENV_BEDROCK_MODEL_ID, "from-env")
    monkeypatch.setenv(ENV_BEDROCK_REGION, "from-env-region")

    backend = BedrockSamplingBackend(model_id="from-arg", region="from-arg-region")
    assert backend.model_id == "from-arg"
    assert backend._region == "from-arg-region"


def test_bedrock_backend_used_path() -> None:
    """End-to-end happy path: ``sample`` returns the unwrapped text."""
    fake_client = _FakeBedrockClient(response=_well_shaped_response("ok"))
    with _patch_boto3_session(fake_client):
        backend = BedrockSamplingBackend(model_id="m", region="us-east-1")
        out = _run(backend.sample(_make_prompt()))
    assert out == "ok"
    assert len(fake_client.converse_calls) == 1


def test_bedrock_backend_no_credentials() -> None:
    """A NoCredentialsError at client-build time tags as bedrock_no_credentials."""
    from botocore.exceptions import NoCredentialsError

    no_creds = NoCredentialsError()
    fake_session = mock.MagicMock()
    fake_session.client.side_effect = no_creds
    fake_boto3 = mock.MagicMock()
    fake_boto3.Session.return_value = fake_session

    with mock.patch.dict("sys.modules", {"boto3": fake_boto3}):
        backend = BedrockSamplingBackend()
        try:
            _run(backend.sample(_make_prompt()))
        except SamplingTransportError as err:
            assert err.code == "bedrock_no_credentials"
            assert err.__cause__ is no_creds
        else:
            raise AssertionError("expected SamplingTransportError")


def test_bedrock_backend_partial_credentials() -> None:
    """A PartialCredentialsError is treated identically to NoCredentialsError."""
    from botocore.exceptions import PartialCredentialsError

    partial = PartialCredentialsError(provider="env", cred_var="AWS_SECRET_ACCESS_KEY")
    fake_session = mock.MagicMock()
    fake_session.client.side_effect = partial
    fake_boto3 = mock.MagicMock()
    fake_boto3.Session.return_value = fake_session

    with mock.patch.dict("sys.modules", {"boto3": fake_boto3}):
        backend = BedrockSamplingBackend()
        try:
            _run(backend.sample(_make_prompt()))
        except SamplingTransportError as err:
            assert err.code == "bedrock_no_credentials"
            assert err.__cause__ is partial
        else:
            raise AssertionError("expected SamplingTransportError")


def test_bedrock_backend_client_error_access_denied() -> None:
    """A ClientError surfaces as ``bedrock_<ErrorCode>``."""
    from botocore.exceptions import ClientError

    err = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
        "Converse",
    )
    fake_client = _FakeBedrockClient(raises=err)
    with _patch_boto3_session(fake_client):
        backend = BedrockSamplingBackend()
        try:
            _run(backend.sample(_make_prompt()))
        except SamplingTransportError as caught:
            assert caught.code == "bedrock_AccessDeniedException"
            assert caught.__cause__ is err
        else:
            raise AssertionError("expected SamplingTransportError")


def test_bedrock_backend_malformed_response_keyerror() -> None:
    """A response missing ``message`` under ``output`` raises bedrock_malformed_response."""
    fake_client = _FakeBedrockClient(response={"output": {}})
    with _patch_boto3_session(fake_client):
        backend = BedrockSamplingBackend()
        try:
            _run(backend.sample(_make_prompt()))
        except SamplingTransportError as caught:
            assert caught.code == "bedrock_malformed_response"
            assert isinstance(caught.__cause__, KeyError)
        else:
            raise AssertionError("expected SamplingTransportError")


def test_bedrock_backend_malformed_response_indexerror() -> None:
    """An empty ``content`` list raises bedrock_malformed_response."""
    fake_client = _FakeBedrockClient(response={"output": {"message": {"content": []}}})
    with _patch_boto3_session(fake_client):
        backend = BedrockSamplingBackend()
        try:
            _run(backend.sample(_make_prompt()))
        except SamplingTransportError as caught:
            assert caught.code == "bedrock_malformed_response"
            assert isinstance(caught.__cause__, IndexError)
        else:
            raise AssertionError("expected SamplingTransportError")


def test_bedrock_backend_satisfies_protocol() -> None:
    """A bare instance is a SamplingBackend (runtime-checkable protocol)."""
    backend = BedrockSamplingBackend()
    assert isinstance(backend, SamplingBackend) is True


def test_bedrock_backend_lazy_client_init() -> None:
    """Constructor builds no client; first ``sample`` builds exactly one;
    second ``sample`` reuses the cached one."""
    fake_client = _FakeBedrockClient(response=_well_shaped_response("ok"))
    fake_session = mock.MagicMock()
    fake_session.client.return_value = fake_client
    fake_boto3 = mock.MagicMock()
    fake_boto3.Session.return_value = fake_session

    with mock.patch.dict("sys.modules", {"boto3": fake_boto3}):
        backend = BedrockSamplingBackend()
        # No boto3 calls yet.
        assert fake_boto3.Session.call_count == 0
        assert fake_session.client.call_count == 0

        # First sample: exactly one Session() and one client(...) call.
        _run(backend.sample(_make_prompt()))
        assert fake_boto3.Session.call_count == 1
        assert fake_session.client.call_count == 1
        first_call_kwargs = fake_session.client.call_args
        assert first_call_kwargs.args == ("bedrock-runtime",)
        assert first_call_kwargs.kwargs == {"region_name": DEFAULT_BEDROCK_REGION}

        # Second sample: client cached, no further client(...) calls.
        _run(backend.sample(_make_prompt()))
        assert fake_session.client.call_count == 1
        # Two converse calls happened on the same cached client.
        assert len(fake_client.converse_calls) == 2


def test_bedrock_backend_passes_correct_inference_config() -> None:
    """The Converse call carries the prompt text, the resolved model id,
    and the pinned inference config."""
    fake_client = _FakeBedrockClient(response=_well_shaped_response("ok"))
    with _patch_boto3_session(fake_client):
        backend = BedrockSamplingBackend(model_id="explicit-model", region="us-west-2")
        prompt = _make_prompt()
        _run(backend.sample(prompt))

    assert len(fake_client.converse_calls) == 1
    kwargs = fake_client.converse_calls[0]
    assert kwargs["modelId"] == "explicit-model"
    assert kwargs["messages"] == [{"role": "user", "content": [{"text": prompt.assemble()}]}]
    # Pinned to the named constants so a tunable bump in one place
    # carries through here without producing a test-only regression.
    assert kwargs["inferenceConfig"] == {
        "maxTokens": BEDROCK_MAX_TOKENS,
        "temperature": BEDROCK_TEMPERATURE,
    }


# ---------------------------------------------------------------------------
# select_sampling_backend resolver
# ---------------------------------------------------------------------------


from mission.sampling import select_sampling_backend  # noqa: E402


def test_select_returns_mcp_when_ctx_has_sampling_capability() -> None:
    """A ctx that advertises sampling capability resolves to MCPSamplingBackend
    with model_id forwarded."""

    class _Caps:
        sampling = True

    class _Ctx:
        session_capabilities = _Caps()

    backend = select_sampling_backend(_Ctx(), model_id="m", prefs={"hints": []})
    assert isinstance(backend, MCPSamplingBackend)
    assert backend.model_id == "m"


def test_select_returns_none_when_ctx_lacks_sampling_capability() -> None:
    """A ctx with session_capabilities.sampling falsy resolves to None."""

    class _Caps:
        sampling = False

    class _Ctx:
        session_capabilities = _Caps()

    assert select_sampling_backend(_Ctx(), None, None) is None


def test_select_returns_none_when_session_capabilities_is_none() -> None:
    """A ctx with session_capabilities set to None resolves to None."""

    class _Ctx:
        session_capabilities = None

    assert select_sampling_backend(_Ctx(), None, None) is None


def test_select_returns_none_when_session_capabilities_attr_missing() -> None:
    """A ctx that exposes no session_capabilities attr at all (and no
    fastmcp.client_capabilities fallback) resolves to None."""

    class _Ctx:
        pass

    assert select_sampling_backend(_Ctx(), None, None) is None


def test_select_returns_bedrock_when_ctx_is_none() -> None:
    """The CLI path (ctx=None) resolves to BedrockSamplingBackend pinned to
    the module default model id."""
    backend = select_sampling_backend(None, model_id=None, prefs=None)
    assert isinstance(backend, BedrockSamplingBackend)
    assert backend.model_id == DEFAULT_BEDROCK_MODEL_ID


def test_select_falls_back_to_fastmcp_client_capabilities() -> None:
    """When session_capabilities is missing, the resolver consults the older
    fastmcp.client_capabilities path."""

    class _ClientCaps:
        sampling = True

    class _FastMCP:
        client_capabilities = _ClientCaps()

    class _Ctx:
        fastmcp = _FastMCP()  # no session_capabilities

    backend = select_sampling_backend(_Ctx(), None, None)
    assert isinstance(backend, MCPSamplingBackend)


def test_select_passes_model_id_and_prefs_to_mcp_backend() -> None:
    """model_id and prefs are forwarded to MCPSamplingBackend verbatim."""

    class _Caps:
        sampling = True

    class _Ctx:
        session_capabilities = _Caps()

    backend = select_sampling_backend(_Ctx(), "test-model", {"hints": ["claude"]})
    assert isinstance(backend, MCPSamplingBackend)
    assert backend.model_id == "test-model"
    assert backend._prefs == {"hints": ["claude"]}


# ---------------------------------------------------------------------------
# validate_strategy_against_catalog
# ---------------------------------------------------------------------------


from mission.sampling import (  # noqa: E402
    MissionValidationError,
    validate_strategy_against_catalog,
)
from pydantic import BaseModel  # noqa: E402


class _SubmitJobArgs(BaseModel):
    """Pydantic model mirroring the shape submit_job_sqs expects."""

    manifest_path: str
    region: str
    namespace: str | None = None


class _FakeTool:
    """Stand-in for FastMCP's ``Tool`` exposing ``input_schema``."""

    def __init__(self, name: str, input_schema: Any | None = None) -> None:
        self.name = name
        self.input_schema = input_schema


# Catalog reused across tests. Includes one tool with a Pydantic args
# model and one tool with no args schema (to exercise the skip path).
_REGISTERED: dict[str, _FakeTool] = {
    "submit_job_sqs": _FakeTool("submit_job_sqs", _SubmitJobArgs),
    "find_examples": _FakeTool("find_examples", None),
}


def _good_strategy(args_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a Strategy that targets ``submit_job_sqs`` with valid args."""
    args = {"manifest_path": "x.yaml", "region": "us-east-1"}
    if args_overrides is not None:
        args.update(args_overrides)
    return {"tool_calls": [{"tool_name": "submit_job_sqs", "args": args}]}


def test_validate_strategy_accepts_well_formed_tool_calls() -> None:
    """A Strategy whose calls match the allowlist and the args schema accepts."""
    validate_strategy_against_catalog(
        strategy=_good_strategy(),
        allowlist=["submit_job_sqs"],
        registered_tools=_REGISTERED,
        allow_scripts=False,
    )


def test_validate_strategy_rejects_tool_not_allowlisted() -> None:
    """A tool name not in the allowlist surfaces ``tool_not_allowlisted``."""
    try:
        validate_strategy_against_catalog(
            strategy=_good_strategy(),
            allowlist=["other_tool"],
            registered_tools=_REGISTERED,
            allow_scripts=False,
        )
    except MissionValidationError as err:
        assert err.code == "validation_error"
        assert err.details is not None
        assert err.details["reason"] == "tool_not_allowlisted"
        assert err.details["tool_name"] == "submit_job_sqs"
        assert err.details["allowlist"] == ["other_tool"]
    else:
        raise AssertionError("expected MissionValidationError")


def test_validate_strategy_rejects_tool_args_invalid() -> None:
    """Args missing required fields surface ``tool_args_invalid``."""
    strategy: dict[str, Any] = {"tool_calls": [{"tool_name": "submit_job_sqs", "args": {}}]}
    try:
        validate_strategy_against_catalog(
            strategy=strategy,
            allowlist=["submit_job_sqs"],
            registered_tools=_REGISTERED,
            allow_scripts=False,
        )
    except MissionValidationError as err:
        assert err.code == "validation_error"
        assert err.details is not None
        assert err.details["reason"] == "tool_args_invalid"
        assert err.details["tool_name"] == "submit_job_sqs"
        # Pydantic v2 ``.errors()`` payload: a list of dicts. The exact
        # contents vary across releases, so we just check the shape.
        assert isinstance(err.details["errors"], list)
        assert len(err.details["errors"]) >= 1
    else:
        raise AssertionError("expected MissionValidationError")


def test_validate_strategy_skips_args_check_when_tool_has_no_input_schema() -> None:
    """A tool whose registered ``input_schema`` is None bypasses args validation."""
    strategy: dict[str, Any] = {
        "tool_calls": [{"tool_name": "find_examples", "args": {"anything": "goes"}}]
    }
    # Should not raise — find_examples has no schema in the fake catalog.
    validate_strategy_against_catalog(
        strategy=strategy,
        allowlist=["find_examples"],
        registered_tools=_REGISTERED,
        allow_scripts=False,
    )


def test_validate_strategy_rejects_script_when_allow_scripts_false() -> None:
    """A scripted strategy with ``allow_scripts=False`` is rejected upstream."""
    strategy: dict[str, Any] = {"script": "x = 1"}
    try:
        validate_strategy_against_catalog(
            strategy=strategy,
            allowlist=["submit_job_sqs"],
            registered_tools=_REGISTERED,
            allow_scripts=False,
        )
    except MissionValidationError as err:
        # Delegated to validate_strategy; the structural validator
        # produces the ``scripts_not_allowed_by_session`` reason.
        assert err.code == "validation_error"
        assert err.details is not None
        assert err.details["reason"] == "scripts_not_allowed_by_session"
    else:
        raise AssertionError("expected MissionValidationError")


def test_validate_strategy_accepts_clean_script_when_allow_scripts_true() -> None:
    """A script using only allowlisted names and safe builtins accepts."""
    strategy: dict[str, Any] = {
        "script": ("x = 1\nsubmit_job_sqs(manifest_path='x.yaml', region='us-east-1')\n")
    }
    validate_strategy_against_catalog(
        strategy=strategy,
        allowlist=["submit_job_sqs"],
        registered_tools=_REGISTERED,
        allow_scripts=True,
    )


def test_validate_strategy_rejects_script_with_dunder() -> None:
    """A script using ``__import__`` is rejected by the AST validator."""
    strategy: dict[str, Any] = {"script": "x = __import__('os')"}
    try:
        validate_strategy_against_catalog(
            strategy=strategy,
            allowlist=["submit_job_sqs"],
            registered_tools=_REGISTERED,
            allow_scripts=True,
        )
    except MissionValidationError as err:
        assert err.code == "validation_error"
        assert err.details is not None
        # The reason comes from the sandbox's AST validator. Either a
        # ``dunder_name`` or a related stable token from the validator.
        assert "reason" in err.details
        # The reason should reference dunder / import / builtin paths.
        reason = err.details["reason"]
        assert any(
            tok in reason
            for tok in (
                "dunder",
                "import",
                "builtin",
                "name_not_allowed",
                "forbidden_call",
            )
        ), f"unexpected reason: {reason!r}"
    else:
        raise AssertionError("expected MissionValidationError")


def test_validate_strategy_rejects_both_tool_calls_and_script() -> None:
    """Both shapes present at once is rejected by the structural validator."""
    strategy: dict[str, Any] = {
        "tool_calls": [
            {
                "tool_name": "submit_job_sqs",
                "args": {"manifest_path": "x.yaml", "region": "us-east-1"},
            }
        ],
        "script": "x = 1",
    }
    try:
        validate_strategy_against_catalog(
            strategy=strategy,
            allowlist=["submit_job_sqs"],
            registered_tools=_REGISTERED,
            allow_scripts=True,
        )
    except MissionValidationError as err:
        assert err.code == "validation_error"
        assert err.details is not None
        assert err.details["reason"] == "must_have_exactly_one_of_tool_calls_or_script"
    else:
        raise AssertionError("expected MissionValidationError")


def test_validate_strategy_rejects_neither_tool_calls_nor_script() -> None:
    """An empty strategy dict is rejected — neither shape present."""
    try:
        validate_strategy_against_catalog(
            strategy={},
            allowlist=["submit_job_sqs"],
            registered_tools=_REGISTERED,
            allow_scripts=False,
        )
    except MissionValidationError as err:
        assert err.code == "validation_error"
        assert err.details is not None
        assert err.details["reason"] == "must_have_exactly_one_of_tool_calls_or_script"
    else:
        raise AssertionError("expected MissionValidationError")


def test_validate_strategy_estimator_zero_for_unmapped_tool() -> None:
    """Tools without a registered cost estimator contribute 0 to the total."""
    strategy: dict[str, Any] = {
        "tool_calls": [
            {
                "tool_name": "submit_job_sqs",
                "args": {"manifest_path": "x.yaml", "region": "us-east-1"},
            },
            {
                "tool_name": "find_examples",
                "args": {"query": "anything"},
            },
        ]
    }
    # find_examples has no estimator → 0 contribution. Total = 1.0 ≤ 10.
    validate_strategy_against_catalog(
        strategy=strategy,
        allowlist=["submit_job_sqs", "find_examples"],
        registered_tools=_REGISTERED,
        allow_scripts=False,
    )


# ---------------------------------------------------------------------------
# maybe_sample_strategy_revision / maybe_sample_final_lessons orchestration
# ---------------------------------------------------------------------------


from mission.sampling import (  # noqa: E402
    SamplingFallback,
    SamplingUsed,
    maybe_sample_final_lessons,
    maybe_sample_strategy_revision,
)


class _FakeBackend:
    """In-test SamplingBackend whose ``sample`` returns canned text or raises.

    Mirrors the duck-typed protocol surface so ``isinstance(_, SamplingBackend)``
    holds. Instances either return a queued string (the ``returns`` argument)
    or raise the queued exception (``raises``).
    """

    def __init__(
        self,
        *,
        backend_name: str = "mcp",
        model_id: str = "fake-model-v1",
        returns: str | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.backend_name = backend_name
        self.model_id = model_id
        self._returns = returns
        self._raises = raises
        self.calls: list[Any] = []

    async def sample(self, prompt: Any) -> str:  # noqa: ARG002
        self.calls.append(prompt)
        if self._raises is not None:
            raise self._raises
        return self._returns if self._returns is not None else ""


def _make_session(
    *,
    session_id: str = "sess-abc",
    iterations: list[dict[str, Any]] | None = None,
    tool_allowlist: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal SessionState dict for orchestration tests."""
    return {
        "version": 1,
        "session_id": session_id,
        "directive_text": "Drive validation loss below 0.1.",
        "criteria": [
            {
                "criterion_id": "loss_below_target",
                "kind": "metric_threshold",
                "required": True,
                "metric": "val_loss",
                "op": "<",
                "target": 0.1,
            }
        ],
        "budget": {"max_iterations": 10, "max_wall_clock_seconds": 3600},
        "tool_allowlist": tool_allowlist or ["submit_job_sqs"],
        "checkpoint_cadence": {"kind": "every_n", "n": 1},
        "stagnation_threshold": 3,
        "use_sampling": True,
        "allow_scripted_strategies": False,
        "status": "running",
        "created_at": "2025-01-01T00:00:00+00:00",
        "iterations": iterations if iterations is not None else [],
        "no_progress_counter": 0,
    }


def _make_iteration_for_orch(
    *,
    iteration_index: int = 0,
    verdict: str = "adjust",
    verdict_reason: str = "no_observable_change",
    criteria_evaluation: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal IterationRecord with an unmet criterion by default."""
    return {
        "iteration_index": iteration_index,
        "started_at": "2025-01-01T00:00:00+00:00",
        "ended_at": "2025-01-01T00:00:01+00:00",
        "phases": [],
        "strategy": {
            "tool_calls": [
                {
                    "tool_name": "submit_job_sqs",
                    "args": {"manifest_path": "x.yaml", "region": "us-east-1"},
                }
            ]
        },
        "observation": {
            "tool_results": [],
            "metrics": {"val_loss": 0.42},
            "events": [],
            "phase_started_at": "2025-01-01T00:00:00+00:00",
            "phase_ended_at": "2025-01-01T00:00:01+00:00",
        },
        "criteria_evaluation": criteria_evaluation
        if criteria_evaluation is not None
        else [
            {
                "criterion_id": "loss_below_target",
                "status": "unmet",
                "evidence": {"val_loss": 0.42},
                "evaluated_at": "2025-01-01T00:00:00+00:00",
            }
        ],
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "checkpoint_evaluated": True,
    }


def test_maybe_sample_strategy_revision_no_backend() -> None:
    """``backend=None`` short-circuits to a deterministic fallback and emits
    a ``sampling_status="disabled"`` audit event."""
    session = _make_session()
    iteration = _make_iteration_for_orch()

    with (
        mock.patch("mission.audit.emit_sampling_event") as emit,
        mock.patch.object(sampling._mission_audit, "emit_sampling_event", emit),
    ):
        result = _run(
            maybe_sample_strategy_revision(
                backend=None,
                session=session,
                iteration=iteration,
                allowlist=["submit_job_sqs"],
                registered_tools=_REGISTERED,
                tool_docstrings={},
                remaining_iterations=5,
                remaining_wall_clock_secs=600.0,
                allow_scripts=False,
            )
        )

    assert isinstance(result, SamplingFallback)
    assert result.reason == "no_backend_resolved"
    assert result.backend_name == "none"
    assert result.model_id is None
    assert result.rationale  # non-empty deterministic template
    # Exactly one audit event, status=disabled, backend=none.
    assert emit.call_count == 1
    kwargs = emit.call_args.kwargs
    assert kwargs["sampling_purpose"] == "strategy_revision"
    assert kwargs["sampling_status"] == "disabled"
    assert kwargs["sampling_backend"] == "none"


def test_maybe_sample_strategy_revision_used_path() -> None:
    """A backend that returns a valid Strategy_Revision JSON yields ``SamplingUsed``."""
    session = _make_session()
    iteration = _make_iteration_for_orch()
    payload = {
        "revision_rationale": "try X",
        "next_strategy": {
            "tool_calls": [
                {
                    "tool_name": "submit_job_sqs",
                    "args": {"manifest_path": "x.yaml", "region": "us-east-1"},
                }
            ]
        },
        "confidence": 0.7,
    }
    backend = _FakeBackend(backend_name="mcp", model_id="fake-mcp", returns=json.dumps(payload))

    with mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit:
        result = _run(
            maybe_sample_strategy_revision(
                backend=backend,
                session=session,
                iteration=iteration,
                allowlist=["submit_job_sqs"],
                registered_tools=_REGISTERED,
                tool_docstrings={"submit_job_sqs": "submit"},
                remaining_iterations=5,
                remaining_wall_clock_secs=600.0,
                allow_scripts=False,
            )
        )

    assert isinstance(result, SamplingUsed)
    assert result.parsed == payload
    assert result.backend_name == "mcp"
    assert result.model_id == "fake-mcp"
    assert emit.call_count == 1
    kwargs = emit.call_args.kwargs
    assert kwargs["sampling_purpose"] == "strategy_revision"
    assert kwargs["sampling_status"] == "used"
    assert kwargs["sampling_backend"] == "mcp"
    assert kwargs["sampling_model_id"] == "fake-mcp"
    assert kwargs["model_output_bytes"] > 0


def test_maybe_sample_strategy_revision_transport_error() -> None:
    """A backend that raises :class:`SamplingTransportError` falls back with
    ``reason="transport_error"`` and audit ``validation_error=err.code``."""
    session = _make_session()
    iteration = _make_iteration_for_orch()
    backend = _FakeBackend(
        backend_name="mcp",
        model_id="fake-mcp",
        raises=SamplingTransportError("mcp_unavailable"),
    )

    with mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit:
        result = _run(
            maybe_sample_strategy_revision(
                backend=backend,
                session=session,
                iteration=iteration,
                allowlist=["submit_job_sqs"],
                registered_tools=_REGISTERED,
                tool_docstrings={},
                remaining_iterations=5,
                remaining_wall_clock_secs=600.0,
                allow_scripts=False,
            )
        )

    assert isinstance(result, SamplingFallback)
    assert result.reason == "transport_error"
    assert result.backend_name == "mcp"
    assert result.model_id == "fake-mcp"
    assert result.rationale  # deterministic template
    assert emit.call_count == 1
    kwargs = emit.call_args.kwargs
    assert kwargs["sampling_status"] == "rejected"
    assert kwargs["validation_error"] == "mcp_unavailable"


def test_maybe_sample_strategy_revision_json_parse_error() -> None:
    """A backend whose output cannot be parsed as JSON falls back as
    ``reason="json_parse"``."""
    session = _make_session()
    iteration = _make_iteration_for_orch()
    backend = _FakeBackend(returns="this is not JSON at all")

    with mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit:
        result = _run(
            maybe_sample_strategy_revision(
                backend=backend,
                session=session,
                iteration=iteration,
                allowlist=["submit_job_sqs"],
                registered_tools=_REGISTERED,
                tool_docstrings={},
                remaining_iterations=5,
                remaining_wall_clock_secs=600.0,
                allow_scripts=False,
            )
        )

    assert isinstance(result, SamplingFallback)
    assert result.reason == "json_parse"
    assert emit.call_count == 1
    kwargs = emit.call_args.kwargs
    assert kwargs["sampling_status"] == "rejected"
    assert kwargs["validation_error"] == "json_parse"


def test_maybe_sample_strategy_revision_schema_mismatch() -> None:
    """A parsed payload missing the required keys falls back as
    ``reason="schema_mismatch"``."""
    session = _make_session()
    iteration = _make_iteration_for_orch()
    backend = _FakeBackend(returns=json.dumps({"unrelated": "keys"}))

    with mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit:
        result = _run(
            maybe_sample_strategy_revision(
                backend=backend,
                session=session,
                iteration=iteration,
                allowlist=["submit_job_sqs"],
                registered_tools=_REGISTERED,
                tool_docstrings={},
                remaining_iterations=5,
                remaining_wall_clock_secs=600.0,
                allow_scripts=False,
            )
        )

    assert isinstance(result, SamplingFallback)
    assert result.reason == "schema_mismatch"
    assert emit.call_count == 1
    kwargs = emit.call_args.kwargs
    assert kwargs["sampling_status"] == "rejected"
    assert kwargs["validation_error"] == "schema_mismatch"


def test_maybe_sample_strategy_revision_tool_not_allowlisted() -> None:
    """A schema-valid payload whose ``next_strategy`` names a non-allowlisted
    tool falls back as ``reason="tool_not_allowlisted"``."""
    session = _make_session()
    iteration = _make_iteration_for_orch()
    payload = {
        "revision_rationale": "swap to forbidden tool",
        "next_strategy": {
            "tool_calls": [
                {"tool_name": "forbidden_tool", "args": {}},
            ]
        },
        "confidence": 0.5,
    }
    backend = _FakeBackend(returns=json.dumps(payload))

    with mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit:
        result = _run(
            maybe_sample_strategy_revision(
                backend=backend,
                session=session,
                iteration=iteration,
                allowlist=["submit_job_sqs"],
                registered_tools=_REGISTERED,
                tool_docstrings={},
                remaining_iterations=5,
                remaining_wall_clock_secs=600.0,
                allow_scripts=False,
            )
        )

    assert isinstance(result, SamplingFallback)
    assert result.reason == "tool_not_allowlisted"
    assert emit.call_count == 1
    kwargs = emit.call_args.kwargs
    assert kwargs["sampling_status"] == "rejected"
    assert kwargs["validation_error"] == "tool_not_allowlisted"


def test_maybe_sample_strategy_revision_extracts_json_from_prose() -> None:
    """The extractor pulls the JSON object out of a prose-wrapped response."""
    session = _make_session()
    iteration = _make_iteration_for_orch()
    payload = {
        "revision_rationale": "x",
        "next_strategy": {
            "tool_calls": [
                {
                    "tool_name": "submit_job_sqs",
                    "args": {"manifest_path": "x.yaml", "region": "us-east-1"},
                }
            ]
        },
        "confidence": 0.5,
    }
    wrapped = "Here is the answer: " + json.dumps(payload) + " hope that helps"
    backend = _FakeBackend(returns=wrapped)

    with mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit:
        result = _run(
            maybe_sample_strategy_revision(
                backend=backend,
                session=session,
                iteration=iteration,
                allowlist=["submit_job_sqs"],
                registered_tools=_REGISTERED,
                tool_docstrings={},
                remaining_iterations=5,
                remaining_wall_clock_secs=600.0,
                allow_scripts=False,
            )
        )

    assert isinstance(result, SamplingUsed)
    assert result.parsed == payload
    assert emit.call_args.kwargs["sampling_status"] == "used"


def test_maybe_sample_final_lessons_used_path() -> None:
    """A backend returning a valid lessons JSON yields ``SamplingUsed``
    and emits an audit event with ``sampling_purpose="final_lessons"``."""
    session = _make_session(
        iterations=[
            _make_iteration_for_orch(
                iteration_index=0, verdict="complete", verdict_reason="criteria_met"
            )
        ]
    )
    payload = {
        "lessons": ["learned A", "learned B"],
        "recommended_followups": ["next steps"],
    }
    backend = _FakeBackend(
        backend_name="bedrock", model_id="fake-bedrock", returns=json.dumps(payload)
    )

    with mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit:
        result = _run(
            maybe_sample_final_lessons(
                backend=backend,
                session=session,
            )
        )

    assert isinstance(result, SamplingUsed)
    assert result.parsed == payload
    assert result.backend_name == "bedrock"
    assert result.model_id == "fake-bedrock"
    assert emit.call_count == 1
    kwargs = emit.call_args.kwargs
    assert kwargs["sampling_purpose"] == "final_lessons"
    assert kwargs["sampling_status"] == "used"
    # Out-of-loop call: iteration_index is not present.
    args = emit.call_args.args
    assert args[1] is None  # iteration_index_or_purpose


def test_maybe_sample_final_lessons_no_backend() -> None:
    """``backend=None`` returns a fallback with empty rationale and emits
    ``sampling_status="disabled"``."""
    session = _make_session()

    with mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit:
        result = _run(maybe_sample_final_lessons(backend=None, session=session))

    assert isinstance(result, SamplingFallback)
    assert result.reason == "no_backend_resolved"
    assert result.backend_name == "none"
    assert result.model_id is None
    assert result.rationale == ""
    assert emit.call_count == 1
    kwargs = emit.call_args.kwargs
    assert kwargs["sampling_purpose"] == "final_lessons"
    assert kwargs["sampling_status"] == "disabled"
    assert kwargs["sampling_backend"] == "none"


# ---------------------------------------------------------------------------
# resolve_sampling_state — session-start backend selection
# ---------------------------------------------------------------------------


from mission.sampling import resolve_sampling_state  # noqa: E402


class _CapsSamplingTrue:
    sampling = True


class _CapsSamplingFalse:
    sampling = False


class _CtxWithSampling:
    session_capabilities = _CapsSamplingTrue()


class _CtxWithoutSampling:
    session_capabilities = _CapsSamplingFalse()


def test_resolve_explicit_false_short_circuits_with_mcp_ctx() -> None:
    """Explicit ``use_sampling_param=False`` overrides MCP capability detection."""
    result = resolve_sampling_state(_CtxWithSampling(), use_sampling_param=False)
    assert result == (False, "none")


def test_resolve_explicit_false_short_circuits_without_ctx() -> None:
    """Explicit ``use_sampling_param=False`` overrides any CLI credential probe."""
    # No ctx and no boto3 patch needed — explicit False short-circuits before
    # the credential probe ever runs.
    result = resolve_sampling_state(None, use_sampling_param=False)
    assert result == (False, "none")


def test_resolve_mcp_capability_detected() -> None:
    """A ctx that advertises sampling resolves to ``(True, "mcp")`` when the
    caller did not specify a preference."""
    result = resolve_sampling_state(_CtxWithSampling(), use_sampling_param=None)
    assert result == (True, "mcp")


def test_resolve_mcp_capability_when_explicit_true() -> None:
    """Explicit ``use_sampling_param=True`` plus MCP capability still resolves
    to the MCP backend."""
    result = resolve_sampling_state(_CtxWithSampling(), use_sampling_param=True)
    assert result == (True, "mcp")


def test_resolve_no_mcp_capability_explicit_true() -> None:
    """A ctx without sampling capability + explicit opt-in returns
    ``(True, "none")`` so the caller can decide whether to error or proceed
    deterministic-only."""
    result = resolve_sampling_state(_CtxWithoutSampling(), use_sampling_param=True)
    assert result == (True, "none")


def test_resolve_no_mcp_capability_implicit() -> None:
    """A ctx without sampling capability + no explicit opt-in returns
    ``(False, "none")``."""
    result = resolve_sampling_state(_CtxWithoutSampling(), use_sampling_param=None)
    assert result == (False, "none")


def test_resolve_cli_with_credentials() -> None:
    """``ctx=None`` + AWS credentials available → ``(True, "bedrock")``."""
    with mock.patch.object(sampling, "_bedrock_credentials_available", return_value=True):
        result = resolve_sampling_state(None, use_sampling_param=None)
    assert result == (True, "bedrock")


def test_resolve_cli_without_credentials_implicit() -> None:
    """``ctx=None`` + no credentials + no explicit opt-in → ``(False, "none")``."""
    with mock.patch.object(sampling, "_bedrock_credentials_available", return_value=False):
        result = resolve_sampling_state(None, use_sampling_param=None)
    assert result == (False, "none")


def test_resolve_cli_without_credentials_explicit_true() -> None:
    """``ctx=None`` + no credentials + explicit opt-in → ``(True, "none")``."""
    with mock.patch.object(sampling, "_bedrock_credentials_available", return_value=False):
        result = resolve_sampling_state(None, use_sampling_param=True)
    assert result == (True, "none")


def test_resolve_handles_missing_boto3_gracefully() -> None:
    """A missing-boto3 environment behaves identically to no-credentials.

    The probe imports ``boto3`` inside the function and catches a broad
    ``Exception`` so an ``ImportError`` cannot crash the helper. Force
    the ImportError by setting ``sys.modules["boto3"] = None`` (the
    standard idiom for making ``import boto3`` raise) and confirm both
    layers — the probe and the resolver — degrade cleanly.
    """
    with mock.patch.dict("sys.modules", {"boto3": None}):
        # The probe must absorb the ImportError and report no creds.
        assert sampling._bedrock_credentials_available() is False
        # The resolver must in turn route to the no-creds branch.
        result = resolve_sampling_state(None, use_sampling_param=None)
    assert result == (False, "none")


# ---------------------------------------------------------------------------
# TestMCPSampling — end-to-end through MCPSamplingBackend
# ---------------------------------------------------------------------------
#
# These tests wire the real :class:`MCPSamplingBackend` through
# :func:`maybe_sample_strategy_revision` so the full pipeline is
# exercised: assemble prompt → ctx.sample → JSON extract → schema
# validate → catalog validate → audit emit. The fixtures stub
# ``Context.sample`` to return canned text (or raise) so the harness
# stays free of any real MCP transport.
# ---------------------------------------------------------------------------


class _CapsSampling:
    """Capabilities object that advertises sampling support."""

    sampling = True


class _CannedCtx:
    """Minimal Context stand-in for end-to-end MCP sampling tests.

    The ``sample`` coroutine returns the value supplied at construction
    time (typically a canned JSON string) or raises ``raises`` if set.
    The :attr:`session_capabilities` attribute is wired so
    :func:`select_sampling_backend` resolves to MCP.
    """

    session_capabilities = _CapsSampling()

    def __init__(
        self,
        *,
        returns: str | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self._returns = returns
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def sample(self, text: str, **kwargs: Any) -> str:
        self.calls.append({"text": text, "kwargs": dict(kwargs)})
        if self._raises is not None:
            raise self._raises
        return self._returns if self._returns is not None else ""


class TestMCPSampling:
    """End-to-end sampling tests with :class:`MCPSamplingBackend` wiring.

    Each test instantiates the real backend (via
    :func:`select_sampling_backend` to mirror engine wiring), calls
    :func:`maybe_sample_strategy_revision`, and asserts on both the
    returned envelope (:class:`SamplingUsed` / :class:`SamplingFallback`)
    and the audit event payload captured via ``mock.patch.object`` on
    :func:`mission.audit.emit_sampling_event`.
    """

    @staticmethod
    def _make_session() -> dict[str, Any]:
        """Build a minimal SessionState dict for orchestration tests."""
        return _make_session()

    @staticmethod
    def _make_iteration() -> dict[str, Any]:
        """Build a minimal IterationRecord with an unmet criterion."""
        return _make_iteration_for_orch()

    @staticmethod
    def _bind_backend(ctx: _CannedCtx) -> MCPSamplingBackend:
        """Resolve the backend through the public selector to mirror engine wiring."""
        backend = select_sampling_backend(ctx, model_id="m", prefs=None)
        # The selector returns ``MCPSamplingBackend`` when the context
        # advertises sampling capability; assert the shape so a future
        # change to the resolver doesn't silently re-route these tests.
        assert isinstance(backend, MCPSamplingBackend)
        return backend

    def test_used_path(self) -> None:
        """Well-formed JSON with allowlisted tool calls → :class:`SamplingUsed`,
        audit event has ``sampling_status="used"`` and ``sampling_backend="mcp"``."""
        payload = {
            "revision_rationale": "swap to a fresh manifest",
            "next_strategy": {
                "tool_calls": [
                    {
                        "tool_name": "submit_job_sqs",
                        "args": {
                            "manifest_path": "x.yaml",
                            "region": "us-east-1",
                        },
                    }
                ]
            },
            "confidence": 0.7,
        }
        ctx = _CannedCtx(returns=json.dumps(payload))
        backend = self._bind_backend(ctx)

        with mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit:
            result = _run(
                maybe_sample_strategy_revision(
                    backend=backend,
                    session=self._make_session(),
                    iteration=self._make_iteration(),
                    allowlist=["submit_job_sqs"],
                    registered_tools=_REGISTERED,
                    tool_docstrings={"submit_job_sqs": "submit"},
                    remaining_iterations=5,
                    remaining_wall_clock_secs=600.0,
                    allow_scripts=False,
                )
            )

        assert isinstance(result, SamplingUsed)
        assert result.parsed == payload
        assert result.backend_name == "mcp"
        assert result.model_id == "m"
        # The ctx received the assembled prompt verbatim — confirms
        # the backend went all the way through to ``ctx.sample``.
        assert len(ctx.calls) == 1

        assert emit.call_count == 1
        kwargs = emit.call_args.kwargs
        assert kwargs["sampling_purpose"] == "strategy_revision"
        assert kwargs["sampling_status"] == "used"
        assert kwargs["sampling_backend"] == "mcp"
        assert kwargs["sampling_model_id"] == "m"
        assert kwargs["model_output_bytes"] > 0

    def test_rejected_bad_json(self) -> None:
        """Sampler returns non-JSON → fallback, audit ``sampling_status="rejected"``,
        ``validation_error="json_parse"``."""
        ctx = _CannedCtx(returns="this is definitely not JSON at all")
        backend = self._bind_backend(ctx)

        with mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit:
            result = _run(
                maybe_sample_strategy_revision(
                    backend=backend,
                    session=self._make_session(),
                    iteration=self._make_iteration(),
                    allowlist=["submit_job_sqs"],
                    registered_tools=_REGISTERED,
                    tool_docstrings={},
                    remaining_iterations=5,
                    remaining_wall_clock_secs=600.0,
                    allow_scripts=False,
                )
            )

        assert isinstance(result, SamplingFallback)
        assert result.reason == "json_parse"
        assert result.backend_name == "mcp"
        assert result.rationale  # deterministic template populated

        assert emit.call_count == 1
        kwargs = emit.call_args.kwargs
        assert kwargs["sampling_status"] == "rejected"
        assert kwargs["sampling_backend"] == "mcp"
        assert kwargs["validation_error"] == "json_parse"

    def test_rejected_non_allowlisted_tool(self) -> None:
        """JSON is well-formed but proposes a tool not in the allowlist →
        fallback, ``validation_error="tool_not_allowlisted"``."""
        payload = {
            "revision_rationale": "use a forbidden tool",
            "next_strategy": {
                "tool_calls": [
                    {"tool_name": "definitely_not_in_allowlist", "args": {}},
                ]
            },
            "confidence": 0.4,
        }
        ctx = _CannedCtx(returns=json.dumps(payload))
        backend = self._bind_backend(ctx)

        with mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit:
            result = _run(
                maybe_sample_strategy_revision(
                    backend=backend,
                    session=self._make_session(),
                    iteration=self._make_iteration(),
                    allowlist=["submit_job_sqs"],
                    registered_tools=_REGISTERED,
                    tool_docstrings={},
                    remaining_iterations=5,
                    remaining_wall_clock_secs=600.0,
                    allow_scripts=False,
                )
            )

        assert isinstance(result, SamplingFallback)
        assert result.reason == "tool_not_allowlisted"
        assert result.backend_name == "mcp"

        assert emit.call_count == 1
        kwargs = emit.call_args.kwargs
        assert kwargs["sampling_status"] == "rejected"
        assert kwargs["sampling_backend"] == "mcp"
        assert kwargs["validation_error"] == "tool_not_allowlisted"

    def test_rejected_script_ast_failure(self) -> None:
        """Proposed strategy has a script with ``__import__`` → fallback,
        ``validation_error`` carries a stable script-rejection token."""
        payload = {
            "revision_rationale": "try a clever script",
            "next_strategy": {
                # The script tries to escape via ``__import__`` — the
                # AST validator rejects this with a ``forbidden_call_target``
                # token (the rejected node is the ``Call`` itself; the
                # dunder filter on the inner Name fires only when the
                # target shape lets the visitor recurse).
                "script": "x = __import__('os')\n",
            },
            "confidence": 0.5,
        }
        ctx = _CannedCtx(returns=json.dumps(payload))
        backend = self._bind_backend(ctx)

        with mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit:
            result = _run(
                maybe_sample_strategy_revision(
                    backend=backend,
                    session=self._make_session(),
                    iteration=self._make_iteration(),
                    allowlist=["submit_job_sqs"],
                    registered_tools=_REGISTERED,
                    tool_docstrings={},
                    remaining_iterations=5,
                    remaining_wall_clock_secs=600.0,
                    # Even with allow_scripts=True, the AST validator
                    # rejects the ``__import__`` call site.
                    allow_scripts=True,
                )
            )

        assert isinstance(result, SamplingFallback)
        assert result.backend_name == "mcp"
        # The reason carries the script-level rejection token. The
        # exact token depends on which validator clause fires first;
        # accept any of the stable tokens documented for ``__import__``.
        assert any(
            tok in result.reason
            for tok in (
                "dunder",
                "import",
                "builtin",
                "name_not_allowed",
                "forbidden_call",
                "script_rejected",
            )
        ), f"unexpected reason: {result.reason!r}"

        assert emit.call_count == 1
        kwargs = emit.call_args.kwargs
        assert kwargs["sampling_status"] == "rejected"
        assert kwargs["sampling_backend"] == "mcp"
        # Mirror the same allow-list of stable tokens for the audit
        # field. ``validation_error`` is the same string the
        # orchestration helper records under ``details.reason``.
        assert any(
            tok in kwargs["validation_error"]
            for tok in (
                "dunder",
                "import",
                "builtin",
                "name_not_allowed",
                "forbidden_call",
                "script_rejected",
            )
        ), f"unexpected validation_error: {kwargs['validation_error']!r}"

    def test_unavailable(self) -> None:
        """``Context.sample`` raises → fallback, ``sampling_status="rejected"``,
        ``validation_error`` starts with ``"mcp_"`` and tags the exception class."""
        ctx = _CannedCtx(raises=RuntimeError("transport blew up"))
        backend = self._bind_backend(ctx)

        with mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit:
            result = _run(
                maybe_sample_strategy_revision(
                    backend=backend,
                    session=self._make_session(),
                    iteration=self._make_iteration(),
                    allowlist=["submit_job_sqs"],
                    registered_tools=_REGISTERED,
                    tool_docstrings={},
                    remaining_iterations=5,
                    remaining_wall_clock_secs=600.0,
                    allow_scripts=False,
                )
            )

        assert isinstance(result, SamplingFallback)
        assert result.reason == "transport_error"
        assert result.backend_name == "mcp"
        assert result.rationale  # deterministic template populated

        assert emit.call_count == 1
        kwargs = emit.call_args.kwargs
        assert kwargs["sampling_status"] == "rejected"
        assert kwargs["sampling_backend"] == "mcp"
        # The MCP backend wraps any non-SamplingTransportError exception
        # as ``mcp_<ExceptionClassName>`` — assert the prefix and the
        # specific exception class so future transport changes still
        # trip the test if they break the contract.
        assert kwargs["validation_error"].startswith("mcp_")
        assert kwargs["validation_error"] == "mcp_RuntimeError"


# ---------------------------------------------------------------------------
# TestBedrockSampling — end-to-end through BedrockSamplingBackend
# ---------------------------------------------------------------------------
#
# These tests exercise the full Bedrock-backed sampling pipeline:
# assemble prompt → ``boto3.Session().client("bedrock-runtime").converse``
# → text extract → JSON parse → schema validate → catalog validate →
# audit emit. The fixtures patch ``boto3`` via the same ``sys.modules``
# injection pattern as the direct backend tests above
# (:func:`_patch_boto3_session`), and the canned response shapes mirror
# ``tests/test_capacity.py::TestBedrockCapacityAdvisor::test_get_recommendation_success``.
# ---------------------------------------------------------------------------


class TestBedrockSampling:
    """End-to-end sampling tests with :class:`BedrockSamplingBackend` wiring.

    Each test instantiates the real backend, drives the full
    orchestration helper :func:`maybe_sample_strategy_revision`, and
    asserts on both the returned envelope (:class:`SamplingUsed` /
    :class:`SamplingFallback`) and the audit event payload captured via
    ``mock.patch.object`` on :func:`mission.audit.emit_sampling_event`.
    """

    @staticmethod
    def _make_session() -> dict[str, Any]:
        """Build a minimal SessionState dict for orchestration tests."""
        return _make_session()

    @staticmethod
    def _make_iteration() -> dict[str, Any]:
        """Build a minimal IterationRecord with an unmet criterion."""
        return _make_iteration_for_orch()

    @staticmethod
    def _strategy_revision_payload() -> dict[str, Any]:
        """A schema-valid Strategy_Revision payload that matches the catalog.

        Wraps a single ``submit_job_sqs`` call with arguments that the
        ``_SubmitJobArgs`` Pydantic model in this file accepts.
        """
        return {
            "revision_rationale": "swap to a fresh manifest",
            "next_strategy": {
                "tool_calls": [
                    {
                        "tool_name": "submit_job_sqs",
                        "args": {
                            "manifest_path": "x.yaml",
                            "region": "us-east-1",
                        },
                    }
                ]
            },
            "confidence": 0.5,
        }

    def test_used_path(self) -> None:
        """Converse returns valid JSON in the expected shape →
        :class:`SamplingUsed`, audit event has ``sampling_backend="bedrock"``
        and ``sampling_model_id`` matches :data:`DEFAULT_BEDROCK_MODEL_ID`."""
        payload = self._strategy_revision_payload()
        fake_client = _FakeBedrockClient(response=_well_shaped_response(json.dumps(payload)))

        with (
            _patch_boto3_session(fake_client),
            mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit,
        ):
            backend = BedrockSamplingBackend()
            result = _run(
                maybe_sample_strategy_revision(
                    backend=backend,
                    session=self._make_session(),
                    iteration=self._make_iteration(),
                    allowlist=["submit_job_sqs"],
                    registered_tools=_REGISTERED,
                    tool_docstrings={"submit_job_sqs": "submit"},
                    remaining_iterations=5,
                    remaining_wall_clock_secs=600.0,
                    allow_scripts=False,
                )
            )

        assert isinstance(result, SamplingUsed)
        assert result.parsed == payload
        assert result.backend_name == "bedrock"
        # The model id flows through from the backend to the envelope.
        assert result.model_id == DEFAULT_BEDROCK_MODEL_ID
        # The Converse call landed on the cached client exactly once.
        assert len(fake_client.converse_calls) == 1
        assert fake_client.converse_calls[0]["modelId"] == DEFAULT_BEDROCK_MODEL_ID

        assert emit.call_count == 1
        kwargs = emit.call_args.kwargs
        assert kwargs["sampling_purpose"] == "strategy_revision"
        assert kwargs["sampling_status"] == "used"
        assert kwargs["sampling_backend"] == "bedrock"
        assert kwargs["sampling_model_id"] == DEFAULT_BEDROCK_MODEL_ID
        assert kwargs["model_output_bytes"] > 0

    def test_rejected_access_denied(self) -> None:
        """``ClientError`` with code ``AccessDeniedException`` →
        :class:`SamplingFallback` with ``reason="transport_error"``,
        audit ``validation_error="bedrock_AccessDeniedException"``."""
        from botocore.exceptions import ClientError

        err = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
            "Converse",
        )
        fake_client = _FakeBedrockClient(raises=err)

        with (
            _patch_boto3_session(fake_client),
            mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit,
        ):
            backend = BedrockSamplingBackend()
            result = _run(
                maybe_sample_strategy_revision(
                    backend=backend,
                    session=self._make_session(),
                    iteration=self._make_iteration(),
                    allowlist=["submit_job_sqs"],
                    registered_tools=_REGISTERED,
                    tool_docstrings={},
                    remaining_iterations=5,
                    remaining_wall_clock_secs=600.0,
                    allow_scripts=False,
                )
            )

        assert isinstance(result, SamplingFallback)
        assert result.reason == "transport_error"
        assert result.backend_name == "bedrock"
        assert result.model_id == DEFAULT_BEDROCK_MODEL_ID
        assert result.rationale  # deterministic template populated

        assert emit.call_count == 1
        kwargs = emit.call_args.kwargs
        assert kwargs["sampling_status"] == "rejected"
        assert kwargs["sampling_backend"] == "bedrock"
        assert kwargs["validation_error"] == "bedrock_AccessDeniedException"

    def test_rejected_throttling(self) -> None:
        """``ClientError`` with code ``ThrottlingException`` →
        :class:`SamplingFallback`, audit
        ``validation_error="bedrock_ThrottlingException"``."""
        from botocore.exceptions import ClientError

        err = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "Converse",
        )
        fake_client = _FakeBedrockClient(raises=err)

        with (
            _patch_boto3_session(fake_client),
            mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit,
        ):
            backend = BedrockSamplingBackend()
            result = _run(
                maybe_sample_strategy_revision(
                    backend=backend,
                    session=self._make_session(),
                    iteration=self._make_iteration(),
                    allowlist=["submit_job_sqs"],
                    registered_tools=_REGISTERED,
                    tool_docstrings={},
                    remaining_iterations=5,
                    remaining_wall_clock_secs=600.0,
                    allow_scripts=False,
                )
            )

        assert isinstance(result, SamplingFallback)
        assert result.reason == "transport_error"
        assert result.backend_name == "bedrock"

        assert emit.call_count == 1
        kwargs = emit.call_args.kwargs
        assert kwargs["sampling_status"] == "rejected"
        assert kwargs["sampling_backend"] == "bedrock"
        assert kwargs["validation_error"] == "bedrock_ThrottlingException"

    def test_rejected_malformed_converse_response(self) -> None:
        """Converse returns a dict missing ``output.message.content[0].text``
        → :class:`SamplingFallback`, audit
        ``validation_error="bedrock_malformed_response"``."""
        # Well-formed JSON envelope but missing the nested ``content``
        # path the backend reads from. ``output.message`` is present
        # but lacks ``content`` entirely — ``KeyError`` flows through
        # the backend's ``except (KeyError, IndexError, TypeError)``
        # branch and surfaces as ``bedrock_malformed_response``.
        fake_client = _FakeBedrockClient(response={"output": {"message": {"role": "assistant"}}})

        with (
            _patch_boto3_session(fake_client),
            mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit,
        ):
            backend = BedrockSamplingBackend()
            result = _run(
                maybe_sample_strategy_revision(
                    backend=backend,
                    session=self._make_session(),
                    iteration=self._make_iteration(),
                    allowlist=["submit_job_sqs"],
                    registered_tools=_REGISTERED,
                    tool_docstrings={},
                    remaining_iterations=5,
                    remaining_wall_clock_secs=600.0,
                    allow_scripts=False,
                )
            )

        assert isinstance(result, SamplingFallback)
        assert result.reason == "transport_error"
        assert result.backend_name == "bedrock"

        assert emit.call_count == 1
        kwargs = emit.call_args.kwargs
        assert kwargs["sampling_status"] == "rejected"
        assert kwargs["sampling_backend"] == "bedrock"
        assert kwargs["validation_error"] == "bedrock_malformed_response"

    def test_rejected_no_credentials(self) -> None:
        """Bedrock client construction raises ``NoCredentialsError`` →
        backend's first ``sample`` raises
        :class:`SamplingTransportError("bedrock_no_credentials")`, the
        orchestration helper falls back, the audit event records
        ``validation_error="bedrock_no_credentials"``, and
        :func:`resolve_sampling_state` reports the no-credentials state
        deterministically so the engine can route the entire session
        through the deterministic-fallback path without re-probing on
        every iteration.
        """
        from botocore.exceptions import NoCredentialsError

        # ---- Step 1: ``resolve_sampling_state`` reports no credentials.
        # The CLI path with no credentials and no explicit opt-in lands
        # at ``(False, "none")``; with an explicit opt-in (``True``) it
        # lands at ``(True, "none")`` — in both cases the resolved
        # backend label is ``"none"`` so the engine never picks
        # Bedrock for the lifetime of the session. The session-start
        # call records the no-credentials state once, here, rather
        # than re-probing on every iteration.
        with mock.patch.object(sampling, "_bedrock_credentials_available", return_value=False):
            assert resolve_sampling_state(None, use_sampling_param=True) == (True, "none")
            assert resolve_sampling_state(None, use_sampling_param=None) == (False, "none")

        # ---- Step 2: even when the CLI selector is asked for a
        # backend (the call site that doesn't pre-check credentials),
        # the returned ``BedrockSamplingBackend`` raises
        # ``SamplingTransportError("bedrock_no_credentials")`` on first
        # ``sample`` because the underlying ``Session().client(...)``
        # call raises ``NoCredentialsError``.
        no_creds = NoCredentialsError()
        fake_session = mock.MagicMock()
        fake_session.client.side_effect = no_creds
        fake_boto3 = mock.MagicMock()
        fake_boto3.Session.return_value = fake_session

        with mock.patch.dict("sys.modules", {"boto3": fake_boto3}):
            backend = select_sampling_backend(None, model_id=None, prefs=None)
            assert isinstance(backend, BedrockSamplingBackend)

            # Direct ``sample`` call surfaces the tagged transport error.
            try:
                _run(backend.sample(_make_prompt()))
            except SamplingTransportError as caught:
                assert caught.code == "bedrock_no_credentials"
                assert caught.__cause__ is no_creds
            else:
                raise AssertionError("expected SamplingTransportError")

            # ---- Step 3: routing the same backend through the full
            # orchestration helper produces a fallback envelope tagged
            # with the same code on the audit event.
            with mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit:
                result = _run(
                    maybe_sample_strategy_revision(
                        backend=backend,
                        session=self._make_session(),
                        iteration=self._make_iteration(),
                        allowlist=["submit_job_sqs"],
                        registered_tools=_REGISTERED,
                        tool_docstrings={},
                        remaining_iterations=5,
                        remaining_wall_clock_secs=600.0,
                        allow_scripts=False,
                    )
                )

        assert isinstance(result, SamplingFallback)
        assert result.reason == "transport_error"
        assert result.backend_name == "bedrock"
        assert result.rationale  # deterministic template populated

        assert emit.call_count == 1
        kwargs = emit.call_args.kwargs
        assert kwargs["sampling_status"] == "rejected"
        assert kwargs["sampling_backend"] == "bedrock"
        assert kwargs["validation_error"] == "bedrock_no_credentials"

    def test_custom_model_id_via_env_var(self, monkeypatch) -> None:
        """``GCO_MISSION_BEDROCK_MODEL_ID`` overrides the default; the
        backend's :attr:`model_id`, the Converse ``modelId`` argument,
        and the audit event's ``sampling_model_id`` all match."""
        custom_model = "anthropic.claude-3-haiku-20240307-v1:0"
        monkeypatch.setenv(ENV_BEDROCK_MODEL_ID, custom_model)
        # Region untouched — the test focuses on the model id.
        monkeypatch.delenv(ENV_BEDROCK_REGION, raising=False)

        payload = self._strategy_revision_payload()
        fake_client = _FakeBedrockClient(response=_well_shaped_response(json.dumps(payload)))

        with (
            _patch_boto3_session(fake_client),
            mock.patch.object(sampling._mission_audit, "emit_sampling_event") as emit,
        ):
            backend = BedrockSamplingBackend()
            assert backend.model_id == custom_model

            result = _run(
                maybe_sample_strategy_revision(
                    backend=backend,
                    session=self._make_session(),
                    iteration=self._make_iteration(),
                    allowlist=["submit_job_sqs"],
                    registered_tools=_REGISTERED,
                    tool_docstrings={},
                    remaining_iterations=5,
                    remaining_wall_clock_secs=600.0,
                    allow_scripts=False,
                )
            )

        assert isinstance(result, SamplingUsed)
        assert result.backend_name == "bedrock"
        assert result.model_id == custom_model
        # Converse received the custom model id verbatim.
        assert len(fake_client.converse_calls) == 1
        assert fake_client.converse_calls[0]["modelId"] == custom_model

        assert emit.call_count == 1
        kwargs = emit.call_args.kwargs
        assert kwargs["sampling_status"] == "used"
        assert kwargs["sampling_backend"] == "bedrock"
        assert kwargs["sampling_model_id"] == custom_model
