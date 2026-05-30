"""Tests for the semantic-progress judge tool wrapper.

The pure ``mission_judge`` package is exercised in isolation by its sibling
test modules; this file pins down the *tool* surface in
``mcp/tools/semantic_progress.py`` — the thin ``@mcp.tool`` wrapper a Mission
session actually calls. Every test here runs against a stubbed sampling
backend, so no live LLM or Bedrock call is ever made: the single
non-deterministic step in the whole flow is replaced by a backend whose
``sample`` returns a canned string.

The success-path slice below covers what a successful invocation returns: the
canonical ``{"metrics": {"progress_score": <float>}}`` shape with a finite
numeric score, the provenance fields placed beside ``metrics``, the
exactly-once backend call, the ``"<backend>:<model>"`` source identifier
(including a model id that itself contains a colon), and the clamp that folds
an out-of-range model score onto the nearest bound while preserving the raw
value in provenance. It also checks the registered tool's gating-prefixed
description and its read-only tag set.

Shared helpers (the stub backend, the gate-on import helper, the registry
introspection helper, and the canonical-shape assertion) are kept at module
scope so later test slices for this same tool can reuse them.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``mcp/run_mcp.py`` puts ``mcp/`` on ``sys.path`` at runtime; mirror that here
# so the tool wrapper and the pure ``mission_judge`` package import the same way
# they do in production, matching the convention used by the sibling tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from mission.sampling import SamplingTransportError  # noqa: E402
from mission_judge.shape import ErrorCode, is_finite_float  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The flag that gates the whole tool registration, plus the umbrella flag that
# overrides it. The gate-on import helper sets the former and clears the latter
# so the tool registers in isolation.
_FLAG = "GCO_ENABLE_SEMANTIC_PROGRESS"
_UMBRELLA = "GCO_ENABLE_ALL_TOOLS"

# The dotted import path and registered name of the tool under test.
_TOOL_IMPORT = "tools.semantic_progress"
_TOOL_NAME = "metrics_semantic_progress"

# The literal prefix the registered tool's description must begin with, and the
# exact tag set it must carry.
_GATED_PREFIX = "[gated by GCO_ENABLE_SEMANTIC_PROGRESS]"
_EXPECTED_TAGS = {"safe", "metrics"}

# A Bedrock-style model id whose own value contains a colon. The source
# identifier is ``"<backend>:<model>"``, so a model id with an embedded colon
# proves the join preserves every colon rather than splitting on the first.
_MODEL_ID_WITH_COLON = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

# Every provenance field a successful result must carry beside ``metrics``.
_PROVENANCE_KEYS = (
    "rationale",
    "source",
    "backend_name",
    "model_id",
    "rubric_version",
    "raw_score",
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class StubBackend:
    """A sampling backend stub that returns a canned string from ``sample``.

    Stands in for the real ``SamplingBackend`` the tool resolves through the
    sampling seam, so no live LLM or Bedrock call is made. It exposes the two
    attributes the tool reads for provenance (``backend_name`` and
    ``model_id``), counts how many times ``sample`` is awaited so a test can
    assert the exactly-once contract, and either returns a canned response or
    raises a supplied error to drive the failure paths.
    """

    def __init__(
        self,
        *,
        backend_name: str,
        model_id: str,
        sample_result: str | None = None,
        sample_error: BaseException | None = None,
    ) -> None:
        self.backend_name = backend_name
        self.model_id = model_id
        self._sample_result = sample_result
        self._sample_error = sample_error
        # Number of times ``sample`` has been awaited; the exactly-once
        # assertion reads this.
        self.sample_calls = 0

    async def sample(self, prompt: Any) -> str:
        """Record the call and return the canned text (or raise the canned error)."""
        self.sample_calls += 1
        if self._sample_error is not None:
            raise self._sample_error
        assert self._sample_result is not None, "stub has neither a result nor an error"
        return self._sample_result


def make_stub_backend(
    *,
    backend_name: str = "bedrock",
    model_id: str = _MODEL_ID_WITH_COLON,
    sample_result: str | None = None,
    sample_error: BaseException | None = None,
) -> StubBackend:
    """Build a :class:`StubBackend` with sensible defaults for the success path.

    Defaults model the CLI (Bedrock) path with a colon-bearing model id, which
    is the most demanding success case for the source-identifier join. Callers
    override ``backend_name`` / ``model_id`` to exercise the MCP path or a
    different model, and pass ``sample_error`` to drive a transport failure.
    """
    return StubBackend(
        backend_name=backend_name,
        model_id=model_id,
        sample_result=sample_result,
        sample_error=sample_error,
    )


def canned_response(score: Any, rationale: str = "made steady progress") -> str:
    """Serialise a model response carrying ``score`` and ``rationale`` as JSON.

    The tool parses the backend's raw string as a JSON object with a numeric
    ``score`` field and a ``rationale`` field; this builds exactly that shape.
    ``score`` is written verbatim (so a test can pass a number or a deliberately
    malformed value), and the rationale is JSON-escaped.
    """
    return json.dumps({"score": score, "rationale": rationale})


def import_judge_module() -> ModuleType:
    """Import the tool wrapper with the gate ON and return the fresh module.

    The whole tool registration lives inside an ``if is_enabled(...)`` block
    that is evaluated once at import time, so a module imported earlier with the
    flag unset would expose no tool. This sets the per-tool flag, clears the
    umbrella flag so the gate is exercised in isolation, drops any cached
    module, and imports it fresh so the gated decorator fires and the callable
    is bound at module scope.
    """
    os.environ[_FLAG] = "true"
    os.environ.pop(_UMBRELLA, None)
    sys.modules.pop(_TOOL_IMPORT, None)
    return importlib.import_module(_TOOL_IMPORT)


def force_unregister_judge() -> None:
    """Drop the tool off the shared FastMCP singleton.

    Importing the wrapper with the gate on registers the tool against the
    process-wide server instance. Removing it on teardown keeps the
    registration from leaking into a sibling test's tool-count or tool-name
    snapshot. Failures are suppressed so teardown is best-effort.
    """
    with contextlib.suppress(Exception):
        import server

        server.mcp.local_provider.remove_tool(_TOOL_NAME)


def registered_tools() -> dict[str, Any]:
    """Return the real registry as a ``{name: Tool}`` map.

    Uses the private ``mcp._list_tools()`` to bypass the catalog-replacement
    transform the server wires in — the public ``list_tools()`` would only ever
    expose the handful of synthetic entry-point tools regardless of what is
    registered. The underlying registry is what these tests assert against.
    """
    import server

    tools = asyncio.run(server.mcp._list_tools())
    return {t.name: t for t in tools}


def call_judge(module: ModuleType, **kwargs: Any) -> dict[str, Any]:
    """Invoke the async tool handler synchronously and return its result."""
    return asyncio.run(module.metrics_semantic_progress(**kwargs))


def assert_canonical_progress_shape(
    result: dict[str, Any],
    *,
    expected_key: str,
    expected_value: float,
) -> None:
    """Assert ``result`` is the canonical success shape carrying one numeric score.

    Pins down every clause a success result must satisfy: it is not an error
    envelope, its top-level ``metrics`` object maps exactly ``expected_key`` to
    ``expected_value``, that value passes the finite-number guard, and every
    provenance field is present at the top level but never inside ``metrics``.
    """
    # A success result is never an error envelope.
    assert "code" not in result, f"expected success shape, got envelope: {result!r}"

    # Top-level metrics object maps the chosen key to the numeric score.
    assert "metrics" in result, f"missing top-level 'metrics': {result!r}"
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    assert metrics == {expected_key: expected_value}, (
        f"expected metrics {{{expected_key!r}: {expected_value!r}}}, got {metrics!r}"
    )

    # The emitted score is a real, finite number.
    assert is_finite_float(metrics[expected_key])

    # Provenance lives outside ``metrics``: present at the top level and absent
    # from the metrics object, which carries only the numeric entry.
    for prov_key in _PROVENANCE_KEYS:
        assert prov_key in result, f"missing provenance field {prov_key!r}: {result!r}"
        assert prov_key not in metrics, f"provenance {prov_key!r} leaked into metrics"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def judge_module() -> Any:
    """Yield the tool module imported with the gate ON, cleaning up afterwards.

    Sets the gating flag, imports the wrapper fresh so the gated decorator
    fires, and yields the module. On teardown the tool is force-unregistered
    from the shared FastMCP singleton and the environment is restored, so a
    registration here never leaks into a sibling test.
    """
    prev_flag = os.environ.get(_FLAG)
    prev_umbrella = os.environ.get(_UMBRELLA)
    try:
        module = import_judge_module()
        yield module
    finally:
        force_unregister_judge()
        for key, value in ((_FLAG, prev_flag), (_UMBRELLA, prev_umbrella)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ===========================================================================
# Success path — canonical shape, provenance, exactly-once sampling
# ===========================================================================


def test_success_returns_canonical_shape_with_finite_score(judge_module: Any) -> None:
    """A canned score reads back as a finite numeric metric in the canonical shape.

    With the sampling seam patched to a stub whose ``sample`` returns a JSON
    object carrying ``score`` 0.7, the tool must emit that value under
    ``metrics.progress_score`` as a finite number, with all provenance placed
    beside ``metrics``.
    """
    stub = make_stub_backend(sample_result=canned_response(0.7))
    with patch.object(judge_module, "select_sampling_backend", return_value=stub):
        result = call_judge(judge_module, directive="train the model to convergence")

    assert_canonical_progress_shape(result, expected_key="progress_score", expected_value=0.7)


def test_success_awaits_sample_exactly_once(judge_module: Any) -> None:
    """The backend's ``sample`` is awaited exactly once per invocation.

    The single non-deterministic step must run once and never be retried, so a
    successful call leaves the stub's call counter at exactly one.
    """
    stub = make_stub_backend(sample_result=canned_response(0.5))
    with patch.object(judge_module, "select_sampling_backend", return_value=stub):
        call_judge(judge_module, directive="reduce validation loss")

    assert stub.sample_calls == 1, f"expected exactly one sample call, got {stub.sample_calls}"


def test_success_source_identifier_preserves_colon_in_model_id(judge_module: Any) -> None:
    """The source identifier is ``"<backend>:<model>"`` with embedded colons kept.

    The model id itself contains a colon, so the source-identifier join must
    concatenate the backend name, a single separator colon, and the model id
    verbatim — preserving every colon already inside the model id rather than
    splitting on the first one.
    """
    stub = make_stub_backend(
        backend_name="bedrock",
        model_id=_MODEL_ID_WITH_COLON,
        sample_result=canned_response(0.6),
    )
    with patch.object(judge_module, "select_sampling_backend", return_value=stub):
        result = call_judge(judge_module, directive="ship the feature")

    assert result["source"] == f"bedrock:{_MODEL_ID_WITH_COLON}"
    # The embedded colon survives: the model id contributes its own colon on top
    # of the single backend/model separator.
    assert result["source"].count(":") >= 2
    assert result["backend_name"] == "bedrock"
    assert result["model_id"] == _MODEL_ID_WITH_COLON


def test_success_mcp_backend_reports_mcp_provenance(judge_module: Any) -> None:
    """An MCP-path backend surfaces ``backend_name == "mcp"`` in provenance.

    When the seam resolves the MCP path, the tool must echo that backend name
    and form the source identifier from it, leaving the metric value untouched.
    """
    stub = make_stub_backend(
        backend_name="mcp",
        model_id="client-routed-model",
        sample_result=canned_response(0.4),
    )
    with patch.object(judge_module, "select_sampling_backend", return_value=stub):
        result = call_judge(judge_module, directive="answer the user's question")

    assert result["backend_name"] == "mcp"
    assert result["model_id"] == "client-routed-model"
    assert result["source"] == "mcp:client-routed-model"
    assert_canonical_progress_shape(result, expected_key="progress_score", expected_value=0.4)


def test_success_bedrock_backend_echoes_resolved_model_id(judge_module: Any) -> None:
    """A Bedrock-path backend echoes its resolved model id in provenance.

    When the seam resolves the Bedrock path, the tool must report
    ``backend_name == "bedrock"`` and surface the backend's resolved model id
    unchanged, so an operator can trace which model produced the score.
    """
    resolved_model = "us.anthropic.claude-3-5-haiku-20241022-v1:0"
    stub = make_stub_backend(
        backend_name="bedrock",
        model_id=resolved_model,
        sample_result=canned_response(0.9),
    )
    with patch.object(judge_module, "select_sampling_backend", return_value=stub):
        result = call_judge(judge_module, directive="optimise the pipeline")

    assert result["backend_name"] == "bedrock"
    assert result["model_id"] == resolved_model
    assert result["source"] == f"bedrock:{resolved_model}"


def test_success_above_range_score_clamps_to_one_and_keeps_raw(judge_module: Any) -> None:
    """A model score above the range clamps to 1.0 while raw_score keeps 1.4.

    A model that returns 1.4 — outside the closed ``[0.0, 1.0]`` interval —
    must surface a clamped ``metrics.progress_score`` of 1.0 for the
    deterministic criterion to read, while the unmodified pre-clamp value is
    preserved in provenance as ``raw_score``.
    """
    stub = make_stub_backend(sample_result=canned_response(1.4))
    with patch.object(judge_module, "select_sampling_backend", return_value=stub):
        result = call_judge(judge_module, directive="finish everything")

    assert_canonical_progress_shape(result, expected_key="progress_score", expected_value=1.0)
    assert result["raw_score"] == 1.4


# ===========================================================================
# Registered-tool metadata — gating prefix and read-only tags
# ===========================================================================


def test_registered_tool_description_prefix_and_tags(judge_module: Any) -> None:
    """The registered tool is gating-prefixed and carries the read-only tags.

    With the gate on, the tool's description must begin with the literal gating
    prefix and its tag set must be exactly the read-only ``safe`` plus the
    ``metrics`` domain tag.
    """
    # ``judge_module`` registered the tool as a side effect of importing under
    # the gate; introspect the live registry for it.
    assert judge_module is not None
    registry = registered_tools()
    tool = registry.get(_TOOL_NAME)
    assert tool is not None, f"{_TOOL_NAME} must register when the gate is on"

    assert tool.description is not None
    assert tool.description.startswith(_GATED_PREFIX), (
        f"description must begin with {_GATED_PREFIX!r}, "
        f"got {tool.description[: len(_GATED_PREFIX)]!r}"
    )

    assert set(tool.tags) == _EXPECTED_TAGS, f"expected tags {_EXPECTED_TAGS!r}, got {tool.tags!r}"


# ---------------------------------------------------------------------------
# Shared helper — error-envelope assertion
# ---------------------------------------------------------------------------


def assert_error_envelope(result: dict[str, Any], *, expected_code: str) -> None:
    """Assert ``result`` is a structured error envelope carrying ``expected_code``.

    A failure result must be the ``{"code", "details"}`` envelope: it carries
    the expected stable code, nests its diagnostics under ``details``, and
    never exposes a top-level ``metrics`` key — so the Observe_Phase merge
    skips it and leaves the consuming check undecided rather than acting on
    bad data.
    """
    assert isinstance(result, dict), f"expected a dict envelope, got {result!r}"
    assert "metrics" not in result, f"error envelope must not carry top-level metrics: {result!r}"
    assert result.get("code") == expected_code, (
        f"expected code {expected_code!r}, got {result.get('code')!r}"
    )
    assert "details" in result and isinstance(result["details"], dict), (
        f"envelope must nest diagnostics under 'details': {result!r}"
    )


# ===========================================================================
# Failure classes — one per stable code, each returns an envelope, none raise
# ===========================================================================


@pytest.mark.parametrize("bad_name", ["bad.name", "bad name", "x" * 129])
def test_invalid_output_name_returns_envelope_before_sampling(
    judge_module: Any, bad_name: str
) -> None:
    """A malformed output_name fails fast with no backend call.

    Output-name validation runs before the directive guard and before any
    backend is selected, so a name carrying a separator, embedded whitespace,
    or one that overruns the length cap yields the invalid-output-name
    envelope while the stub's ``sample`` is never awaited.
    """
    stub = make_stub_backend(sample_result=canned_response(0.5))
    with patch.object(judge_module, "select_sampling_backend", return_value=stub):
        result = call_judge(
            judge_module,
            directive="train the model to convergence",
            output_name=bad_name,
        )

    # ``call_judge`` returned rather than raising, so nothing escaped the tool.
    assert_error_envelope(result, expected_code=ErrorCode.INVALID_OUTPUT_NAME)
    assert stub.sample_calls == 0, "name validation must precede backend selection"


@pytest.mark.parametrize("blank_directive", ["", "   ", "\t\n  "])
def test_missing_directive_returns_envelope_without_sampling(
    judge_module: Any, blank_directive: str
) -> None:
    """An empty or whitespace-only directive fails before any backend call.

    The directive guard fires before a backend is selected, so a blank
    directive yields the missing-directive envelope and the stub's ``sample``
    is never awaited.
    """
    stub = make_stub_backend(sample_result=canned_response(0.5))
    with patch.object(judge_module, "select_sampling_backend", return_value=stub):
        result = call_judge(judge_module, directive=blank_directive)

    # The tool returned an envelope instead of raising — nothing escaped.
    assert_error_envelope(result, expected_code=ErrorCode.MISSING_DIRECTIVE)
    assert stub.sample_calls == 0, "the directive guard must fire before any backend call"


def test_no_sampling_backend_returns_envelope(judge_module: Any) -> None:
    """A seam that resolves no backend yields the no-backend envelope.

    With the sampling seam patched to return ``None``, the tool cannot obtain a
    backend to sample, so it surfaces the no-sampling-backend code and carries
    no top-level metrics.
    """
    with patch.object(judge_module, "select_sampling_backend", return_value=None):
        result = call_judge(judge_module, directive="train the model to convergence")

    # The tool returned an envelope instead of raising — nothing escaped.
    assert_error_envelope(result, expected_code=ErrorCode.NO_SAMPLING_BACKEND)


def test_sampling_transport_error_preserves_transport_code(judge_module: Any) -> None:
    """A transport failure is wrapped, preserving the upstream code and provenance.

    When the backend's ``sample`` raises ``SamplingTransportError``, the tool
    surfaces the sampling-transport-error code and threads the upstream
    transport code plus the resolved backend name and model id into
    ``details`` so an operator can trace what failed.
    """
    upstream_code = "bedrock_throttling"
    stub = make_stub_backend(
        backend_name="bedrock",
        model_id=_MODEL_ID_WITH_COLON,
        sample_error=SamplingTransportError(upstream_code),
    )
    with patch.object(judge_module, "select_sampling_backend", return_value=stub):
        result = call_judge(judge_module, directive="train the model to convergence")

    # The transport error was caught and converted — it did not escape.
    assert_error_envelope(result, expected_code=ErrorCode.SAMPLING_TRANSPORT_ERROR)
    assert result["details"]["transport_code"] == upstream_code
    assert result["details"]["backend_name"] == "bedrock"
    assert result["details"]["model_id"] == _MODEL_ID_WITH_COLON
    # The single ``sample`` call ran once before raising; there is no retry.
    assert stub.sample_calls == 1


# Each entry is an ``(id, raw_backend_output)`` pair that the score parser must
# reject: non-JSON text, an object missing the score field, and a score that is
# NaN, infinite, or a boolean. ``json.dumps`` emits bare ``NaN``/``Infinity``
# tokens that ``json.loads`` round-trips back to the float specials, so those
# reach the finite-number guard rather than failing as non-JSON.
_INVALID_MODEL_OUTPUTS: tuple[tuple[str, str], ...] = (
    ("non_json", "not json at all"),
    ("missing_score_field", json.dumps({"rationale": "no score present"})),
    ("nan_score", canned_response(float("nan"))),
    ("inf_score", canned_response(float("inf"))),
    ("bool_score", canned_response(True)),
)


@pytest.mark.parametrize(
    "raw_output",
    [pytest.param(text, id=label) for label, text in _INVALID_MODEL_OUTPUTS],
)
def test_invalid_model_score_returns_envelope(judge_module: Any, raw_output: str) -> None:
    """Untrustworthy model output yields the invalid-model-score envelope.

    Non-JSON text, an object with no score field, and a score that is NaN,
    infinite, or a boolean each fail parsing, so the tool surfaces the
    invalid-model-score code with no top-level metrics rather than emitting a
    bogus number a threshold comparison could act on.
    """
    stub = make_stub_backend(sample_result=raw_output)
    with patch.object(judge_module, "select_sampling_backend", return_value=stub):
        result = call_judge(judge_module, directive="train the model to convergence")

    # The parse failure became an envelope instead of escaping the tool.
    assert_error_envelope(result, expected_code=ErrorCode.INVALID_MODEL_SCORE)


# ---------------------------------------------------------------------------
# Shared helpers — gating-state import and full save/restore
# ---------------------------------------------------------------------------


def _set_or_pop(key: str, value: str | None) -> None:
    """Set ``key`` to ``value`` in the environment, or remove it when ``value`` is None."""
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


def import_judge_with_flags(*, flag: str | None, umbrella: str | None) -> ModuleType:
    """Import the tool wrapper fresh under an explicit gating-flag state.

    Generalises :func:`import_judge_module` (which always imports with the
    per-tool flag on) to the three gating states the registration checks
    need: per-tool flag on, per-tool flag and umbrella both off, and the
    umbrella on with the per-tool flag off. The whole registration lives
    inside an ``if is_enabled(...)`` block evaluated once at import time, so
    each state needs a fresh import.

    Sets the per-tool flag and the umbrella flag to the supplied values
    (``None`` removes the variable), drops the tool off the shared FastMCP
    singleton so a registration left by an earlier import cannot mask an
    expected-absent assertion, evicts the cached module, and imports it
    fresh so the gated decorator is re-evaluated against the env just set.
    """
    _set_or_pop(_FLAG, flag)
    _set_or_pop(_UMBRELLA, umbrella)
    # Clear any prior registration before the fresh import: when the gate is
    # off the import registers nothing, so a leftover registration would
    # otherwise survive and defeat the absence assertion; when the gate is on
    # it also avoids a duplicate-registration error.
    force_unregister_judge()
    sys.modules.pop(_TOOL_IMPORT, None)
    return importlib.import_module(_TOOL_IMPORT)


@pytest.fixture
def registration_state() -> Any:
    """Snapshot and restore env, cached module, and registry around a test.

    The registration checks drive the gating flag through its three states
    and import the wrapper fresh each time, mutating process-wide env, the
    shared FastMCP singleton, and ``sys.modules``. This saves all three
    before the test and restores them afterwards — popping the tool off the
    singleton, putting both env vars back to their prior values, and
    restoring the previously cached module object (or evicting it when there
    was none) — so the default posture (tool unregistered, since it is
    gated off by default) is re-established and a sibling suite's tool-count
    or tool-name snapshot never sees leaked registration state.
    """
    prev_flag = os.environ.get(_FLAG)
    prev_umbrella = os.environ.get(_UMBRELLA)
    prev_module = sys.modules.get(_TOOL_IMPORT)
    try:
        yield
    finally:
        # Order matters: drop the registration first so the shared singleton
        # is clean regardless of which module object ends up cached.
        force_unregister_judge()
        _set_or_pop(_FLAG, prev_flag)
        _set_or_pop(_UMBRELLA, prev_umbrella)
        if prev_module is not None:
            sys.modules[_TOOL_IMPORT] = prev_module
        else:
            sys.modules.pop(_TOOL_IMPORT, None)


# ===========================================================================
# Flag-gated registration — absent by default, present when opted in
# ===========================================================================
#
# These checks assert against the underlying registry via ``registered_tools``
# (which calls the private ``mcp._list_tools()``), not the public
# ``list_tools()``. The server wires a catalog-replacement search transform
# that swaps the public listing for a small synthetic entry-point set, so the
# public listing would not faithfully reflect a gated tool's registration
# either way. The underlying registry is the honest signal for whether the
# gated decorator fired, and it stays deterministic for a fixed flag state.


def test_tool_absent_when_flag_and_umbrella_unset(registration_state: Any) -> None:
    """With neither the per-tool flag nor the umbrella set, the tool stays unregistered.

    This is the default-off posture: importing the wrapper with both
    ``GCO_ENABLE_SEMANTIC_PROGRESS`` and ``GCO_ENABLE_ALL_TOOLS`` cleared
    leaves the gated decorator un-fired, so the tool name never appears in
    the registry.
    """
    import_judge_with_flags(flag=None, umbrella=None)

    assert _TOOL_NAME not in registered_tools(), (
        f"{_TOOL_NAME} must not register when both the per-tool flag and the umbrella are unset"
    )


def test_tool_present_with_gating_prefix_and_safe_tag_when_flag_enabled(
    registration_state: Any,
) -> None:
    """Setting the per-tool flag registers the tool with its gating prefix and safe tag.

    With ``GCO_ENABLE_SEMANTIC_PROGRESS`` set and the umbrella cleared, the
    tool appears in the registry, its description begins with the literal
    gating prefix, and its tag set carries the read-only ``safe`` tag.
    """
    import_judge_with_flags(flag="true", umbrella=None)

    registry = registered_tools()
    tool = registry.get(_TOOL_NAME)
    assert tool is not None, f"{_TOOL_NAME} must register when the per-tool flag is set"

    assert tool.description is not None
    assert tool.description.startswith(_GATED_PREFIX), (
        f"description must begin with {_GATED_PREFIX!r}, "
        f"got {tool.description[: len(_GATED_PREFIX)]!r}"
    )
    assert "safe" in set(tool.tags), f"tool must carry the 'safe' tag, got {tool.tags!r}"


def test_tool_present_when_umbrella_enabled_and_flag_unset(registration_state: Any) -> None:
    """The umbrella alone registers the tool even with the per-tool flag unset.

    Setting ``GCO_ENABLE_ALL_TOOLS=true`` while leaving
    ``GCO_ENABLE_SEMANTIC_PROGRESS`` unset must enable the gate through the
    umbrella's mutually-inclusive override, so the tool registers without its
    own flag being set.
    """
    import_judge_with_flags(flag=None, umbrella="true")

    assert _TOOL_NAME in registered_tools(), (
        f"{_TOOL_NAME} must register when the umbrella {_UMBRELLA} is on, "
        "even with the per-tool flag unset"
    )


# ---------------------------------------------------------------------------
# Strategies — fixed inputs whose only downstream variability is the backend
# ---------------------------------------------------------------------------

# A directive carrying at least one non-whitespace character, so the directive
# guard never short-circuits and every example exercises the full scoring path
# (the one whose metric value and provenance the determinism claim is about).
_det_directives = st.text(min_size=1, max_size=256).filter(lambda s: s.strip() != "")

# Optional recent progress context: absent, empty, or arbitrary text within a
# modest bound. All three shapes flow through the same deterministic prompt
# builder, so the result must not depend on which was supplied.
_det_context = st.one_of(st.none(), st.text(max_size=512))

# A valid output name: 1..128 printable, non-space characters with the "."
# separator excluded — exactly one well-formed path segment. ``None`` selects
# the default ``progress_score`` key.
_det_output_names = st.one_of(
    st.none(),
    st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters="."),
        min_size=1,
        max_size=128,
    ),
)

# A finite score inside the closed unit interval: emitted verbatim (no clamp),
# so the metric value mirrors exactly what the canned backend response carries.
_det_scores = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# A rationale comfortably under the retention ceiling, so it round-trips through
# the canned response unchanged and contributes a stable provenance field.
_det_rationales = st.text(max_size=256)


# ===========================================================================
# Determinism boundary — fixed inputs and a fixed sample() yield identical results
# ===========================================================================


@settings(
    max_examples=150,
    deadline=None,
    # ``judge_module`` is function-scoped, so Hypothesis flags that it is built
    # once for the whole test rather than per example. That is exactly the
    # intent here: a single gated import and registration is shared across every
    # example, and the only per-example variation is the freshly patched backend
    # inside the body. Suppressing the check keeps that deliberate reuse clean.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    directive=_det_directives,
    recent_context=_det_context,
    output_name=_det_output_names,
    score=_det_scores,
    rationale=_det_rationales,
)
def test_two_invocations_with_a_fixed_sample_return_identical_results(
    judge_module: Any,
    directive: str,
    recent_context: str | None,
    output_name: str | None,
    score: float,
    rationale: str,
) -> None:
    """Given fixed inputs and a fixed backend response, two calls match exactly.

    Everything in the tool except the single backend ``sample`` call is a pure
    function of the inputs. So when the backend is pinned to return one fixed
    canned string, two invocations with the same directive, context, and output
    name must produce byte-identical results — the same metric value under the
    same key and the same provenance (source, backend name, model id, rubric
    version, raw score, rationale). The only thing that could differ between
    runs is the backend answer, and here it cannot.
    """
    # One fixed backend answer reused by both invocations: the determinism
    # claim is conditional on the sample() output being held constant.
    canned = canned_response(score, rationale)
    first_backend = make_stub_backend(sample_result=canned)
    second_backend = make_stub_backend(sample_result=canned)

    kwargs: dict[str, Any] = {"directive": directive, "recent_context": recent_context}
    if output_name is not None:
        kwargs["output_name"] = output_name

    # ``select_sampling_backend`` is patched per example, handing out a fresh
    # stub on each of the two calls so neither invocation can observe state the
    # other left behind.
    with patch.object(
        judge_module,
        "select_sampling_backend",
        side_effect=[first_backend, second_backend],
    ):
        first = call_judge(judge_module, **kwargs)
        second = call_judge(judge_module, **kwargs)

    # Each invocation drove its own backend exactly once.
    assert first_backend.sample_calls == 1
    assert second_backend.sample_calls == 1

    # The whole result — metric value and every provenance field — is identical.
    assert first == second

    # And it is the canonical success shape, so the equality above is over a
    # real score plus provenance rather than two matching error envelopes.
    expected_key = output_name if output_name is not None else "progress_score"
    assert_canonical_progress_shape(first, expected_key=expected_key, expected_value=score)


# ===========================================================================
# Registry determinism — repeated listings are stable and gate the judge
# ===========================================================================
#
# Like the registration checks above, this property reads the underlying
# registry via ``registered_tools`` (the private ``mcp._list_tools()``), not the
# public ``list_tools()``. The server wires a catalog-replacement search
# transform that swaps the public listing for a small synthetic entry-point set,
# so the public listing would not faithfully reflect a gated tool's registration
# for any flag state. The underlying registry is the honest signal, and the
# claim under test is precisely that it stays stable for a fixed flag state.


@settings(
    max_examples=150,
    deadline=None,
    # ``registration_state`` is function-scoped, so Hypothesis flags that it is
    # set up once for the whole test rather than per example. That is the intent:
    # one snapshot of env, cached module, and registry is taken up front and
    # restored on teardown, while each example re-imports the wrapper under its
    # own flag state and unregisters it again before the next. Suppressing the
    # check keeps that deliberate single snapshot/restore clean.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(flag_on=st.booleans(), umbrella_on=st.booleans())
def test_registry_is_stable_and_gates_the_judge_for_a_fixed_flag_state(
    registration_state: Any,
    flag_on: bool,
    umbrella_on: bool,
) -> None:
    """Repeated registry reads are identical and the judge is gated exactly.

    For any fixed startup flag state, listing the registry more than once with
    no intervening mutation must return the identical set of tool names — the
    registry is a deterministic function of the flags it was built under, not of
    when it is read. And the judge tool obeys its gate: it is present if and only
    if its own per-tool flag or the umbrella override is enabled, and absent
    otherwise.

    Each example imports the wrapper fresh under the chosen flag state (which
    fires or skips the gated decorator), reads the registry three times and
    asserts the full name set is byte-for-byte the same across all three reads,
    then asserts the judge's presence equals ``flag_on or umbrella_on``. The
    ``finally`` drops the registration so no example leaks a registered tool into
    a sibling suite's tool-count or tool-name snapshot.
    """
    flag = "true" if flag_on else None
    umbrella = "true" if umbrella_on else None

    try:
        # Import under the chosen flag state. ``import_judge_with_flags`` clears
        # any prior registration first, so a leftover from an earlier example can
        # neither mask an expected-absent state nor double-register.
        import_judge_with_flags(flag=flag, umbrella=umbrella)

        # Read the registry several times with nothing changing in between; a
        # fixed flag state must produce the identical name set on every read.
        names_first = set(registered_tools())
        names_second = set(registered_tools())
        names_third = set(registered_tools())

        assert names_first == names_second == names_third, (
            "repeated registry listings must be identical for a fixed flag state; "
            f"diff first/second={names_first ^ names_second!r}, "
            f"first/third={names_first ^ names_third!r}"
        )

        # The judge appears exactly when its own flag or the umbrella override is
        # enabled, mirroring the enablement rule the gate is evaluated through.
        expected_present = flag_on or umbrella_on
        assert (_TOOL_NAME in names_first) == expected_present, (
            f"{_TOOL_NAME} present={_TOOL_NAME in names_first} must equal "
            f"(flag_on or umbrella_on)={expected_present} "
            f"for flag_on={flag_on}, umbrella_on={umbrella_on}"
        )
    finally:
        # Leave the shared singleton clean after every example, regardless of the
        # flag state or an assertion failure, so the default-off posture is
        # restored and no registration leaks downstream.
        force_unregister_judge()
