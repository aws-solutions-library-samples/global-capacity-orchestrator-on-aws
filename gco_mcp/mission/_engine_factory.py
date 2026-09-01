"""Shared :class:`MissionEngine` factory used by the MCP tool and the CLI.

Both the ``gco_mcp/tools/mission.py`` MCP tool surface and the
``cli/commands/mission_cmd.py`` Click subcommands need to build a
:class:`mcp.mission.engine.MissionEngine` with production-wired
dependencies — a real tool dispatcher that routes through the live
FastMCP registry, a sampling callable that runs the
``Strategy_Revision`` prompt against the resolved backend, and an
optional sandbox runner for scripted strategies. The wiring used to
live only inside the MCP tool module, which left the CLI with a stub
dispatcher and ``sampling_callable=None``. That made
``gco mission run`` and ``gco mission iterate`` useful for smoke-
testing the engine bookkeeping but unable to converge on goals that
depend on actual tool-result content.

This module hosts the shared factory. The MCP tool surface and the
CLI both call :func:`build_engine_dependencies` to obtain the same
``(tool_dispatcher, sampling_callable, sandbox_runner)`` triple, then
hand them to :class:`mcp.mission.engine.MissionEngine`. The CLI also
uses :func:`make_stub_dispatcher` to opt into the canned-response
behaviour explicitly through ``--dry-run`` for the smoke-test use
case the original stub was designed for.

Why a separate module rather than living inside the MCP tool? The
MCP tool's body is gated by ``GCO_ENABLE_MISSION``; the CLI must be
able to import the factory regardless of the flag, because the
flag-gating happens in the Click group, not at import time. Splitting
the factory out keeps the MCP tool body lean and lets the CLI reach
the same wiring without crossing a feature-flag boundary.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-01T14:42:56Z
# Generated from Git commit: 89b000378ed5a912a38c06f4feab2b029936ebcc
# Flowchart(s) generated from this file:
#   * ``build_engine_dependencies`` -> ``diagrams/code_diagrams/gco_mcp/mission/_engine_factory.build_engine_dependencies.html``
#     (PNG: ``diagrams/code_diagrams/gco_mcp/mission/_engine_factory.build_engine_dependencies.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


# The mission package and the FastMCP server module both live under
# ``gco_mcp/``; the path-injection pattern matches the rest of the MCP
# surface so ``import server`` and ``import mission.*`` resolve
# without making the ``mcp`` directory a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mission import sampling as mission_sampling  # noqa: E402
from mission import state as mission_state  # noqa: E402
from mission.engine import (  # noqa: E402
    MissionEngine,
    ObservationAugmenter,
    SandboxRunner,
    ToolDispatcher,
)

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from mission.types import SessionState, ToolCallRecord


__all__ = [
    "EngineDependencies",
    "MissionToolResultError",
    "build_engine_dependencies",
    "build_mission_engine",
    "fetch_registered_tool_metadata",
    "make_stub_dispatcher",
    "remaining_wall_clock_seconds",
]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class EngineDependencies:
    """Dependency triple consumed by :class:`MissionEngine`.

    Holds the callables :class:`MissionEngine` needs at construction
    time so callers can build them once and pass them through. A simple
    namespace class rather than a NamedTuple so fields can be optional
    without the boilerplate.
    """

    __slots__ = (
        "final_lessons_callable",
        "memory_store",
        "observation_augmenters",
        "sampling_callable",
        "sandbox_runner",
        "tool_dispatcher",
    )

    def __init__(
        self,
        *,
        tool_dispatcher: ToolDispatcher,
        sampling_callable: Callable[..., Awaitable[Any]] | None,
        sandbox_runner: SandboxRunner | None,
        final_lessons_callable: Callable[..., Awaitable[Any]] | None = None,
        memory_store: Any | None = None,
        observation_augmenters: Sequence[ObservationAugmenter] | None = None,
    ) -> None:
        self.tool_dispatcher = tool_dispatcher
        self.sampling_callable = sampling_callable
        self.sandbox_runner = sandbox_runner
        self.final_lessons_callable = final_lessons_callable
        self.memory_store = memory_store
        self.observation_augmenters = observation_augmenters


# ---------------------------------------------------------------------------
# Helpers shared between the MCP tool and the CLI
# ---------------------------------------------------------------------------


def remaining_wall_clock_seconds(session: Mapping[str, Any]) -> float | None:
    """Return remaining wall-clock seconds for ``session``, or ``None``.

    Mirrors the behaviour the MCP tool surface used to keep private:
    sessions that haven't started yet report the full cap; sessions
    whose ``max_wall_clock_seconds`` is the ``-1`` "uncapped" sentinel
    return ``None`` so the sampling prompt's budget context renders
    ``"remaining_wall_clock_seconds": null``; running sessions return
    the difference between cap and elapsed time clamped at zero.
    """
    cap = session.get("budget", {}).get("max_wall_clock_seconds")
    if cap is None or cap == -1:
        return None
    started_raw = session.get("started_at")
    if not started_raw:
        return float(cap)
    try:
        started = datetime.fromisoformat(started_raw)
    except TypeError, ValueError:
        return float(cap)
    elapsed = (datetime.now(UTC) - started).total_seconds()
    return max(0.0, float(cap) - elapsed)


async def fetch_registered_tool_metadata() -> tuple[dict[str, Any], dict[str, str]]:
    """Return (registered_tools, tool_docstrings) from the live FastMCP registry.

    Reads through ``server.mcp._list_tools()``, the same low-level path
    the MCP tool surface used to walk inline. Returns two parallel
    dicts so the sampler closure can be built without the caller
    needing to call ``mcp.*`` introspection twice.

    A blanket ``Exception`` swallow yields ``({}, {})`` so the engine
    factory still produces a usable dispatcher when the registry is
    not yet populated (CLI path before ``register_all_tools`` ran,
    test harness with a stub mcp instance, etc.).
    """
    try:
        from server import mcp  # noqa: PLC0415 - lazy
    except Exception:
        return {}, {}
    try:
        tools = await mcp._list_tools()
    except Exception:
        return {}, {}
    registered = {t.name: t for t in tools}
    docstrings = {name: (getattr(t, "description", "") or "") for name, t in registered.items()}
    return registered, docstrings


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------


class MissionToolResultError(RuntimeError):
    """A live FastMCP tool returned a typed transport-level error result."""

    def __init__(self, tool_name: str, details: Any) -> None:
        self.tool_name = tool_name
        self.details = details
        if details is None:
            summary = "no error details"
        elif isinstance(details, str):
            summary = details
        else:
            summary = json.dumps(details, sort_keys=True, default=str)
        super().__init__(f"tool {tool_name!r} returned an error result: {summary}")


def _tool_result_payload(result: Any) -> Any:
    """Unwrap a FastMCP result into a JSON-serialisable observation payload."""
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    content_blocks = getattr(result, "content", None) or []
    if content_blocks:
        first = content_blocks[0]
        text_payload = getattr(first, "text", None)
        if isinstance(text_payload, str):
            try:
                return json.loads(text_payload)
            except TypeError, ValueError:
                return text_payload
    return None


async def _live_dispatch_tool(
    tool_name: str,
    args: dict[str, Any],
    ctx_inner: Any | None,
) -> Any:
    """Dispatch ``tool_name`` against the live FastMCP registry.

    Looks the tool up via ``server.mcp.get_tool`` and invokes it with
    ``args``. The raw FastMCP ``ToolResult`` Pydantic model is not
    JSON-serialisable, so the helper unwraps it:

    * Prefer ``structured_content`` when present — every FastMCP tool
      with a typed return surfaces a JSON-able dict here.
    * Fall back to the first content block's ``text`` field;
      best-effort JSON-parse so structured string-returning tools
      round-trip as dicts.
    * A result with FastMCP's typed ``is_error`` flag raises
      :class:`MissionToolResultError`, so the engine records the call as
      ``failed`` instead of treating an error body as a successful observation.
    * Anything else returns ``None`` so the engine records a benign
      placeholder rather than a non-serialisable object.

    ``RuntimeError`` propagates for unknown tool names so the engine's
    per-call try/except records a ``failed`` outcome rather than
    silently invoking nothing.
    """
    # Reuse the active request context when one exists so tools that
    # introspect ``get_context()`` see the right one. Fall back to
    # ``ctx_inner`` when no request is active (CLI path / unit-test
    # path).
    context: Any | None
    try:
        from fastmcp.server.dependencies import get_context  # noqa: PLC0415

        try:
            context = get_context()
        except Exception:
            context = ctx_inner
    except Exception:
        context = ctx_inner
    del context  # FastMCP uses contextvars internally

    from server import mcp  # noqa: PLC0415

    tool_obj = await mcp.get_tool(tool_name)
    if tool_obj is None:
        raise RuntimeError(f"tool {tool_name!r} not registered")
    result = await tool_obj.run(args)
    payload = _tool_result_payload(result)
    if getattr(result, "is_error", False) is True:
        raise MissionToolResultError(tool_name, payload)
    return payload


def make_stub_dispatcher() -> ToolDispatcher:
    """Return a tool dispatcher that returns canned-ok responses.

    Reserved for ``--dry-run`` smoke testing. Returns
    ``{"_status": "ok", "_stub": True, ...}`` for every call so the
    engine bookkeeping converges without invoking any real tool —
    useful for exercising the loop without spending Bedrock or AWS
    credits, and for unit tests that don't want a live registry.

    Production code paths use :func:`build_engine_dependencies` so
    this stub never fires unless the operator opts in explicitly.
    """

    async def _dispatch(tool_name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {
            "_status": "ok",
            "_stub": True,
            "tool_name": tool_name,
            "args": dict(args),
        }

    return _dispatch


# ---------------------------------------------------------------------------
# Sandbox runner
# ---------------------------------------------------------------------------


def _build_sandbox_runner(session: Mapping[str, Any]) -> SandboxRunner | None:
    """Wire a real sandbox runner when the session permits scripted strategies."""
    if not session.get("allow_scripted_strategies"):
        return None
    try:
        from mission.sandbox import MissionSandbox  # noqa: PLC0415
    except ImportError:
        return None

    sandbox = MissionSandbox(
        list(session.get("tool_allowlist") or []),
        cast("SessionState", session),
    )

    async def _sandbox_runner(
        script: str,
        ctx_arg: Any,
        dispatcher: ToolDispatcher,
    ) -> tuple[dict[str, Any], list[ToolCallRecord]]:
        obs, calls = await sandbox.run(script, ctx_arg, dispatcher)
        return obs, cast("list[ToolCallRecord]", calls)

    return _sandbox_runner


# ---------------------------------------------------------------------------
# Mission-memory store
# ---------------------------------------------------------------------------


def _build_memory_store() -> Any | None:
    """Construct the mission-memory store for production wiring.

    One seam for both memory paths — the engine's best-effort terminal
    write and the sampler closure's prior-missions retrieval — so the
    test-suite conftest can neutralise real AWS reach by patching this
    single function. Construction itself is free (table/index names
    resolve lazily from SSM on first use), but it is still guarded: any
    unexpected failure degrades to "no memory", never to a failed
    engine build.
    """
    try:
        from mission.memory import MissionMemoryStore  # noqa: PLC0415

        return MissionMemoryStore()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Sampling callable
# ---------------------------------------------------------------------------


def _build_sampling_callable(
    session: Mapping[str, Any],
    ctx: Any | None,
    *,
    registered_tools: dict[str, Any],
    tool_docstrings: dict[str, str],
) -> Callable[..., Awaitable[Any]] | None:
    """Wire the Strategy_Revision sampler when the session opted in."""
    if not session.get("use_sampling"):
        return None
    if session.get("sampling_backend_resolved") == "none":
        return None

    backend_obj = mission_sampling.select_sampling_backend(
        model_id=session.get("bedrock_model_id"),
    )

    # Slow-moving live signals (per-region queue depth, GPU utilisation,
    # deployed-region list, reservation counts). Cached on the closure
    # so one ``build_engine_dependencies`` call — which spans the
    # multi-iteration drive of one CLI invocation or one MCP request —
    # only pays the AWS round-trip once. Outer-list trick keeps the
    # cache mutable through the inner closure without ``nonlocal``.
    from mission._environment import gather_session_environment  # noqa: PLC0415

    env_cache: list[Mapping[str, Any] | None] = []
    # Prior similar missions from the memory vector index. Same
    # one-slot cache trick as ``env_cache``: retrieval costs one
    # embedding call plus one SearchVectors round-trip, and the
    # directive never changes mid-session, so pay it once per engine
    # wiring. Retrieval is inherently gated on ``use_sampling`` —
    # this closure only exists for sampling sessions — which keeps the
    # deterministic Propose path free of network calls, exactly what
    # the determinism suite pins down. Best-effort: any failure
    # (absent table, backfilling index, Bedrock down, no credentials)
    # degrades to "no prior context". An empty result list also maps
    # to ``None`` so the prompt section only renders when there is
    # something to say.
    memory_cache: list[list[Mapping[str, Any]] | None] = []

    async def _sampler(*, session: dict[str, Any], ctx: Any | None) -> Any:
        iterations = session.get("iterations") or []
        latest = iterations[-1] if iterations else None
        if latest is None:
            return None
        budget = session.get("budget") or {}
        # ``max_iterations=-1`` is the "uncapped" sentinel; the prompt
        # expects an informational remaining-iterations count, so we
        # report zero in that mode (the model is told nothing about
        # the iteration axis when there's no cap to count down from).
        # Finite caps subtract the count of recorded iterations and
        # clamp at zero.
        cap = int(budget.get("max_iterations", 0))
        remaining_iters = 0 if cap == -1 else max(0, cap - len(iterations))
        if not env_cache:
            try:
                env_cache.append(gather_session_environment(session))
            except Exception:  # noqa: BLE001
                env_cache.append(None)
        env_ctx = env_cache[0]
        if not memory_cache:
            try:
                store = _build_memory_store()
                results = (
                    store.search_similar(str(session.get("directive_text") or ""))
                    if store is not None
                    else None
                )
                memory_cache.append(results or None)
            except Exception:  # noqa: BLE001
                memory_cache.append(None)
        prior_missions = memory_cache[0]
        return await mission_sampling.maybe_sample_strategy_revision(
            backend=backend_obj,
            session=cast("SessionState", session),
            iteration=latest,
            allowlist=list(session.get("tool_allowlist") or []),
            registered_tools=registered_tools,
            tool_docstrings=tool_docstrings,
            remaining_iterations=remaining_iters,
            remaining_wall_clock_secs=remaining_wall_clock_seconds(session),
            allow_scripts=bool(session.get("allow_scripted_strategies", False)),
            environment_context=env_ctx,
            prior_missions=prior_missions,
        )

    return _sampler


# ---------------------------------------------------------------------------
# Final lessons callable
# ---------------------------------------------------------------------------


def _build_final_lessons_callable(
    session: Mapping[str, Any],
    ctx: Any | None,
    tool_docstrings: dict[str, str],
) -> Callable[..., Awaitable[Any]] | None:
    """Wire the Final_Report lessons overlay when sampling is enabled.

    When the session opted into sampling and a backend resolves, the
    engine calls this after a terminal verdict to produce model-derived
    ``lessons`` and ``recommended_followups`` for the Final_Report.
    Without it, the report uses deterministic templates.
    """
    if not session.get("use_sampling"):
        return None
    if session.get("sampling_backend_resolved") == "none":
        return None

    backend_obj = mission_sampling.select_sampling_backend(
        model_id=session.get("bedrock_model_id"),
    )

    async def _final_lessons(*, session: dict[str, Any], ctx: Any | None) -> Any:
        return await mission_sampling.maybe_sample_final_lessons(
            backend=backend_obj,
            session=cast("SessionState", session),
            tool_docstrings=tool_docstrings,
        )

    return _final_lessons


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


async def build_engine_dependencies(
    session: Mapping[str, Any],
    ctx: Any | None,
    *,
    use_stub_dispatcher: bool = False,
    extra_tool_metadata: tuple[dict[str, Any], dict[str, str]] | None = None,
) -> EngineDependencies:
    """Build the :class:`MissionEngine` dependency triple for ``session``.

    Looks up the live FastMCP registry to populate the registered-tools
    map and per-tool docstring cache, then assembles the production
    wiring: a live tool dispatcher (or the canned-stub when
    ``use_stub_dispatcher`` is True), the Strategy_Revision sampler
    when sampling resolved to a real backend, and a sandbox runner
    when the session permits scripted strategies.

    The CLI passes ``use_stub_dispatcher=True`` only on the
    ``--dry-run`` path; the MCP tool surface always uses the live
    dispatcher.

    ``extra_tool_metadata`` is an optional ``(tools_map, docstrings)``
    pair merged over the live registry snapshot before the sampler is
    built. The swarm layer uses it to teach an orchestrator's
    Strategy_Revision sampler the in-process supervisor tools — which
    are deliberately never FastMCP-registered — so spawn proposals
    validate against the catalog like any other call. It never touches
    dispatch: the dispatcher wrapper routes those names in-process.
    """
    if use_stub_dispatcher:
        # The stub dispatcher means real tools never run, which means
        # there's nothing to inform the sampling prompt; downgrade to
        # the deterministic propose path for symmetry. The session's
        # ``use_sampling`` flag stays as the operator set it so the
        # criteria-scaffold path still runs through Bedrock.
        return EngineDependencies(
            tool_dispatcher=make_stub_dispatcher(),
            sampling_callable=None,
            sandbox_runner=_build_sandbox_runner(session),
        )

    registered_tools, tool_docstrings = await fetch_registered_tool_metadata()
    if extra_tool_metadata is not None:
        extra_tools, extra_docs = extra_tool_metadata
        registered_tools = {**registered_tools, **extra_tools}
        tool_docstrings = {**tool_docstrings, **extra_docs}
    sampling_callable = _build_sampling_callable(
        session,
        ctx,
        registered_tools=registered_tools,
        tool_docstrings=tool_docstrings,
    )
    sandbox_runner = _build_sandbox_runner(session)
    final_lessons = _build_final_lessons_callable(session, ctx, tool_docstrings)
    return EngineDependencies(
        tool_dispatcher=_live_dispatch_tool,
        sampling_callable=sampling_callable,
        sandbox_runner=sandbox_runner,
        final_lessons_callable=final_lessons,
        # The terminal-verdict memory write is best-effort inside the
        # engine, so the store is wired unconditionally on the live
        # path (the stub-dispatcher / --dry-run branch above stays
        # memory-free: throwaway smoke sessions must not become
        # institutional memory).
        memory_store=_build_memory_store(),
    )


async def build_mission_engine(
    session: Mapping[str, Any],
    ctx: Any | None,
    *,
    use_stub_dispatcher: bool = False,
) -> MissionEngine:
    """Build a :class:`MissionEngine` instance ready to drive ``session``.

    Convenience wrapper over :func:`build_engine_dependencies` that
    also resolves the persistence backend through
    :func:`mcp.mission.state.get_backend`. Most callers should use
    this; the lower-level :func:`build_engine_dependencies` is
    available for code that needs to instantiate the engine itself
    (e.g. with a custom backend).
    """
    deps = await build_engine_dependencies(session, ctx, use_stub_dispatcher=use_stub_dispatcher)
    backend = mission_state.get_backend()
    return MissionEngine(
        backend=backend,
        tool_dispatcher=deps.tool_dispatcher,
        sampling_callable=deps.sampling_callable,
        sandbox_runner=deps.sandbox_runner,
        final_lessons_callable=deps.final_lessons_callable,
        memory_store=deps.memory_store,
        observation_augmenters=deps.observation_augmenters,
    )
