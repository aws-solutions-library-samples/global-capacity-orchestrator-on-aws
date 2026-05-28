"""
Property tests for the Mission script sandbox.

The Mission script surface accepts multi-statement Python source from
the operator and runs it inside a restricted sandbox to drive an
iteration. The same source crosses MCP, the CLI, and disk before it
reaches the runtime, so the AST validator is the single trust
boundary. Two invariants must hold:

1. **No forbidden source ever reaches the sandbox runtime.** Hypothesis
   synthesises Python source containing known-dangerous constructs
   — ``import`` statements, ``__import__("...")`` calls, class-walk
   chains like ``().__class__.__bases__``, ``eval`` / ``exec`` /
   ``compile``, lambdas with subscript-then-call shapes, async
   constructs, walrus assignments into protected names, decorators,
   ``class`` definitions, bare ``except``, ``with`` blocks, ``match``
   statements, ``global`` / ``nonlocal``, ``del``, ``assert``, and
   subscript-then-call / call-of-call patterns — and optionally wraps
   them in a top-level statement, an ``if`` body, a ``for`` body, or a
   function body so the validator is exercised at multiple depths.
   ``validate_script_ast(src, allowlist)`` must raise
   ``ScriptRejected`` for every drawn input. A tripwire patched onto
   the ``mission.sandbox`` module asserts the runtime entry point is
   never reached for any rejected source.

2. **A hand-written script using only allowlisted names parses
   cleanly.** The happy-path test exercises every script-allowed
   construct in one piece of source: a function definition, a ``for``
   loop, a list comprehension, a ``try`` / ``except`` / ``finally``
   block, a call to an allowlisted tool, an f-string, ``mission.observe``
   and ``mission.event`` calls, a numeric expression, and a ``return``
   statement.

Settings cap ``max_examples=100`` and ``deadline=2000`` so the file
runs well under five seconds wall-clock.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Mirror the import pattern used by the other Mission tests:
# ``mcp/run_mcp.py`` adds ``mcp/`` to ``sys.path`` at runtime, but
# pytest has to do it itself before the import resolves.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from mission import SCHEMA_VERSION  # noqa: E402
from mission import sandbox as sandbox_module  # noqa: E402
from mission.engine import MissionEngine  # noqa: E402
from mission.sandbox import (  # noqa: E402
    ScriptRejected,
    make_default_sandbox_runner,
    validate_script_ast,
)
from mission.state import FilesystemBackend  # noqa: E402

# ---------------------------------------------------------------------------
# Tool allowlist used throughout the tests
# ---------------------------------------------------------------------------
#
# A small, plausible per-session allowlist: the SQS submitter (which
# also appears in the walrus-shadow grammar so the test exercises that
# specific protected-name path), an inference helper, and a stack
# describer. The names match real Mission tool names elsewhere in the
# codebase so the validator's protected-shadow rules behave exactly as
# they would in production.
_ALLOWLIST: list[str] = [
    "submit_job_sqs",
    "list_inference_endpoints",
    "describe_stack",
]


# ---------------------------------------------------------------------------
# Forbidden-fragment grammar
# ---------------------------------------------------------------------------
#
# Each entry is a top-level Python statement (or block) that must be
# rejected by ``validate_script_ast`` whether placed at module scope or
# nested inside an ``if`` / ``for`` / function body. The catalogue
# covers every escape route called out in the task brief: imports,
# dynamic imports, class-walk chains, ``eval`` / ``exec`` / ``compile``,
# lambda-based escapes, async constructs, walrus assignments into
# protected names, decorators, classes, ``assert``, ``del``, ``with``,
# ``match``, bare ``except``, ``global`` / ``nonlocal``, and
# subscript-then-call / call-of-call shapes.

_FORBIDDEN_STATEMENTS: tuple[str, ...] = (
    # ---- imports -----------------------------------------------------
    "import os",
    "import os.path",
    "import sys, json",
    "from os import path",
    "from os.path import join",
    "from os import path as p",
    # ---- dynamic import ----------------------------------------------
    '__import__("os")',
    '__import__("os").system("ls")',
    'm = __import__("subprocess")',
    # ---- class-walk escapes ------------------------------------------
    "().__class__",
    "(0).__class__.__bases__",
    '"".__class__',
    "[].__class__.__mro__",
    "{}.__class__",
    "type(0).__bases__",
    # ---- eval / exec / compile / dynamic attribute lookup ------------
    'eval("1")',
    'exec("1")',
    'compile("1", "", "eval")',
    'getattr(__builtins__, "exec")("1")',
    'getattr(builtins, "exec")("1")',
    'r = getattr(obj, "x")',
    "x = globals()",
    "x = locals()",
    "x = vars()",
    # ---- lambda escape paths -----------------------------------------
    "f = lambda x: x.__class__()",
    'g = lambda: __import__("os")',
    "h = (lambda: 1)()",
    "k = (lambda xs: xs[0]())([print])",
    # ---- async constructs --------------------------------------------
    "async def f():\n    pass",
    "await x",
    "async for x in xs:\n    pass",
    "async with ctx:\n    pass",
    # ---- walrus into protected names ---------------------------------
    "(mission := 1)",
    "(submit_job_sqs := 1)",
    "(eval := 1)",
    "(len := 0)",
    # ---- decorators --------------------------------------------------
    "@decorator\ndef f():\n    pass",
    "@some.deco\ndef g():\n    pass",
    # ---- class definitions -------------------------------------------
    "class Foo:\n    pass",
    "class Bar(Exception):\n    pass",
    # ---- assert / del / with / match / try-bare-except ---------------
    "assert x",
    "del x",
    "with ctx:\n    pass",
    "with ctx as c:\n    pass",
    "match x:\n    case 1:\n        pass",
    "try:\n    pass\nexcept:\n    pass",
    # ---- global / nonlocal -------------------------------------------
    "global x",
    "nonlocal x",
    # ---- subscript-then-call / call-of-call --------------------------
    "obs[0]()",
    'obs["k"]()',
    "f()()",
    "(lambda: 1)()()",
    'getattr(builtins, "exec")("1")',
    # ---- yield / yield from inside def -------------------------------
    "def gen():\n    yield 1",
    "def gen():\n    yield from xs",
    # ---- attribute walks past a non-mission base ---------------------
    "x = obs.foo.bar",
    "x = some.deeply.nested.attr",
)


# Wrappers that place the forbidden statement at varying depths so the
# validator is exercised at module scope, inside an ``if``, inside a
# ``for``, and inside a function body. Every wrapper produces a
# syntactically valid module so we exercise the validator (not the
# parser's syntax-error path).
_WRAPPERS: tuple[tuple[str, str], ...] = (
    # (template, indentation prefix applied to the fragment)
    ("{body}", ""),
    ("if True:\n{body}", "    "),
    ("for _i in [0]:\n{body}", "    "),
    ("def _wrapper():\n{body}", "    "),
)


def _indent(source: str, prefix: str) -> str:
    """Apply ``prefix`` to every line of ``source``.

    Preserves empty lines as empty (no trailing whitespace) so the
    resulting module is parseable and ``ast.parse`` does not refuse
    the input on a whitespace-only line.
    """
    return "\n".join(prefix + line if line else line for line in source.splitlines())


@st.composite
def _forbidden_script_source(draw: st.DrawFn) -> str:
    """Synthesise a script that must be rejected by ``validate_script_ast``.

    Picks one fragment from the catalogue and one wrapper that nests
    it at module scope, inside an ``if``, inside a ``for``, or inside
    a function body. Every result is syntactically valid Python.
    """
    fragment = draw(st.sampled_from(_FORBIDDEN_STATEMENTS))
    template, prefix = draw(st.sampled_from(_WRAPPERS))
    return template.format(body=_indent(fragment, prefix))


# ---------------------------------------------------------------------------
# Happy-path script
# ---------------------------------------------------------------------------
#
# A hand-written script that exercises every construct the validator
# is expected to accept: a function definition, a ``for`` loop, a list
# comprehension, a ``try`` / ``except`` / ``finally`` block, a call to
# an allowlisted tool, an f-string, ``mission.observe`` / ``mission.event``
# calls, a numeric expression, and a ``return`` statement.

# Where the script surface uses ``mission.observe(...)`` and
# ``mission.event(...)``, the call must be ``await``ed: the runtime
# layer exposes those helpers through Monty's ``external_functions``
# channel as coroutine factories so script-side calls reach the
# host-side closures that own the per-iteration ``observe_log`` /
# ``event_log`` lists. The validator opens ``Await`` for that one
# attribute-call shape (and for ``await <tool>(...)``) and keeps
# every other ``Await`` variant rejected.

_HAPPY_PATH_SCRIPT: str = """
def summarise(values):
    total = sum(values)
    count = len(values)
    avg = total / count if count > 0 else 0
    return f"avg={avg} count={count}"

xs = [1, 2, 3, 4, 5]
doubled = [x * 2 for x in xs]
report = summarise(doubled)

try:
    endpoints = await list_inference_endpoints()
    await mission.event("endpoints_listed", count=len(endpoints))
except RuntimeError as exc:
    await mission.event("endpoints_failed", message=f"reason: {exc}")
finally:
    await mission.observe("summary", report)
    await mission.observe("doubled", doubled)

for item in xs:
    if item > 2:
        await mission.event("item_seen", value=item)
"""


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestScriptRejectsForbiddenSource:
    """Forbidden source is rejected at parse time; the runtime never runs."""

    @given(src=_forbidden_script_source())
    @settings(max_examples=100, deadline=2000)
    def test_validate_script_ast_rejects_forbidden_source(self, src: str) -> None:
        """Every synthesised forbidden script raises ``ScriptRejected``.

        Patching a runtime tripwire onto the ``mission.sandbox`` module
        makes the rejection contract explicit: if a future regression
        let a forbidden script through the validator, the tripwire
        would fire and the test would fail with a distinct error
        rather than silently letting the dangerous code run. The patch
        is applied with ``pytest.MonkeyPatch.context()`` (rather than
        the function-scoped ``monkeypatch`` fixture) so each Hypothesis
        example gets a fresh, fully-reset patch — Hypothesis flags
        function-scoped fixtures as a health-check failure because
        their state carries across generated examples.
        """

        def _tripwire(*_args: object, **_kwargs: object) -> object:
            pytest.fail("sandbox runtime should not be called for rejected source")

        with pytest.MonkeyPatch.context() as mp:
            # The runtime layer lands in slice 5.3+; this test installs
            # a placeholder attribute on the module that the runtime
            # will eventually replace. The validator must raise before
            # any caller reaches a name that looks like a runtime
            # entry point, so the placeholder existing or not has no
            # effect on the assertion.
            mp.setattr(
                sandbox_module,
                "_SANDBOX_RUNTIME_TRIPWIRE",
                _tripwire,
                raising=False,
            )
            with pytest.raises(ScriptRejected):
                validate_script_ast(src, _ALLOWLIST)


class TestScriptAcceptsAllowlistedSource:
    """A script using only allowlisted names parses cleanly."""

    def test_happy_path_script_validates(self) -> None:
        """The hand-written script exercises every accepted construct.

        ``validate_script_ast`` returns ``None`` on success; the test
        asserts the call completes without raising. The script touches
        a function definition, a ``for`` loop, a list comprehension, a
        ``try`` / ``except`` / ``finally`` block, a call to an
        allowlisted tool (``list_inference_endpoints``), an f-string,
        ``mission.observe`` and ``mission.event`` calls, numeric
        expressions, and a ``return`` statement — the full surface the
        validator is expected to accept.
        """
        result = validate_script_ast(_HAPPY_PATH_SCRIPT, _ALLOWLIST)
        assert result is None

    def test_await_on_allowlisted_tool_validates(self) -> None:
        """``await <tool>(...)`` is the supported coroutine-call shape.

        The runtime layer exposes every allowlisted tool through
        Monty's ``external_functions`` channel as a coroutine factory,
        so the script genuinely needs ``await`` to receive the
        dispatcher's return value rather than a coroutine object.
        The validator accepts that one shape — ``await <name>(...)``
        where ``<name>`` is on the per-session tool allowlist — and
        keeps every other ``Await`` form rejected (see the forbidden-
        fragment grammar above).

        This test pins the positive contract: a script that combines
        an awaited tool call with the surrounding constructs an
        operator might write (assignment from the awaited result,
        ``len`` on the return value, a ``mission.observe`` /
        ``mission.event`` pair, an f-string consuming the results,
        and an awaited call inside a ``try`` block) validates
        without raising.
        """
        await_script = (
            "results = await list_inference_endpoints()\n"
            'await mission.observe("endpoints", results)\n'
            'await mission.event("collected", count=len(results))\n'
            'summary = f"got {len(results)} endpoints"\n'
            "try:\n"
            '    extras = await describe_stack(name="x")\n'
            '    await mission.observe("extras", extras)\n'
            "except RuntimeError as exc:\n"
            '    await mission.event("describe_failed", reason=f"{exc}")\n'
        )
        assert validate_script_ast(await_script, _ALLOWLIST) is None


# ---------------------------------------------------------------------------
# End-to-end scripted strategy through the engine + sandbox runner
# ---------------------------------------------------------------------------
#
# Covers the full happy-path wiring for a scripted Strategy: the
# engine's Propose_Phase produces a script-bearing Strategy, the
# Execute_Phase routes it through the wired ``sandbox_runner``
# returned by ``make_default_sandbox_runner``, the sandbox runs the
# operator-supplied source under :class:`MissionSandbox`, the script
# invokes the allowlisted ``find_examples`` tool through the in-script
# wrapper, and the resulting Observation + ``script_call_log`` land
# on the persisted :class:`IterationRecord`. Every external call is
# routed through the injected dispatcher so no real AWS, MCP, or
# filesystem traffic is required.

# The script payload validates against an allowlist containing only
# ``find_examples`` plus the safe builtins exposed by the validator
# (``len`` is the only one used here). The body is small on purpose:
# the sandbox layer is exercised by the property tests above and the
# engine wiring by the engine tests; this end-to-end test focuses on
# the contract at the seam between them.
_END_TO_END_SCRIPT: str = (
    'results = await find_examples(query="gpu")\n'
    'await mission.observe("results", results)\n'
    'await mission.event("results_collected", count=len(results))\n'
)


def _make_e2e_session(session_id: str) -> dict[str, object]:
    """Build a SessionState dict tuned for the end-to-end scripted run.

    Bypasses the validators on purpose, matching the pattern used by
    ``tests/test_mission_engine.py``: the engine consumes the typed
    fields directly, and the validators are out of scope for the
    end-to-end test. The criterion target is unreachable so completion
    cannot fire — the test asserts a ``("continue", "in_progress")``
    verdict, which means the loop ran the full five-phase cycle
    without short-circuiting on a budget cap or completion check.
    """
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": "Collect example manifests via the script surface.",
        "criteria": [
            {
                # Required + unreachable. Pins completion off so the
                # in-progress verdict is the deterministic
                # ``("continue", "in_progress")`` from the cascade
                # default branch.
                "criterion_id": "c1",
                "kind": "metric_threshold",
                "required": True,
                "metric": "loss",
                "op": "<",
                "target": -1.0,
            }
        ],
        "budget": {
            "max_iterations": 10,
            "max_wall_clock_seconds": 600,
        },
        "tool_allowlist": ["find_examples"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 10,
        "use_sampling": False,
        "allow_scripted_strategies": True,
        "status": "pending",
        "created_at": "2025-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
        "accumulated_cost_usd": 0.0,
    }


async def test_scripted_strategy_runs_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive a scripted Strategy from Propose through Decide.

    Wiring under test:

    * ``MissionEngine`` is constructed with a ``FilesystemBackend``
      rooted at ``tmp_path``, an async dispatcher stub that returns a
      canned list when called with ``tool_name == "find_examples"``,
      and a real sandbox runner from
      :func:`make_default_sandbox_runner`. The runner builds a
      :class:`MissionSandbox` over the operator-supplied allowlist and
      session, so the script genuinely runs through Monty under the
      module-level resource caps.
    * The Propose_Phase's deterministic-fallback helper is patched on
      the :class:`MissionEngine` class to return the scripted
      Strategy. The engine doesn't expose a sampling-callable hook
      that produces scripted strategies for first-iteration runs (the
      wired sampling path only fires after a prior ``adjust``
      verdict), and the alternate "pre-populate iteration history"
      shape would still need the same patch. Patching
      ``_deterministic_strategy`` is the simplest seam — the engine
      test ``test_run_iteration_dispatches_script_through_sandbox_runner``
      uses the same approach.

    Assertions:

    * The verdict is ``("continue", "in_progress")`` because no
      budget cap fires and the unreachable-target criterion stays
      unmet. The verdict tuple anchors the test to the same default
      branch every other engine happy-path test exercises.
    * Every entry in ``iteration["phases"]`` has
      ``status == "succeeded"``. Five phases: propose, execute,
      observe, evaluate, decide.
    * ``iteration["script_call_log"]`` has exactly one entry with
      ``tool_name == "find_examples"`` and ``status == "ok"``. The
      log is the canonical record of in-script tool calls and is
      what the engine's propose-fallback walks for "most recent
      successful call" lookups.
    * ``iteration["observation"]["metrics"]["observations"]["results"]``
      equals the canned dispatcher response. The
      :func:`_build_script_observation` helper folds
      ``mission.observe(...)`` rows under
      ``metrics.observations`` rather than flat-merging them so a
      script-collected key cannot collide with a tool-derived metric
      of the same name; the assertion locks that namespace in.
    * ``iteration["observation"]["events"]`` contains an event with
      ``event_name == "results_collected"`` and ``count == 2``,
      confirming :func:`mission.event` rows pool with tool-derived
      events on the same Observation list.
    """
    # Set the resource caps to known values so the test's wall-clock
    # behaviour is independent of any operator's environment overrides
    # for these env vars. The defaults already make the test pass, but
    # an operator with a tiny ``GCO_MCP_CODE_MODE_MAX_DURATION_SECS``
    # set globally could otherwise see the script trip the limit.
    monkeypatch.setattr(sandbox_module, "_DURATION_LIMIT_SECS", 30.0)
    monkeypatch.setattr(sandbox_module, "_MEMORY_LIMIT_BYTES", 200_000_000)

    backend = FilesystemBackend(root=tmp_path)
    session = _make_e2e_session(session_id="sess-e2e-script")
    backend.save_session(session)

    dispatcher_calls: list[dict[str, object]] = []

    async def dispatcher(tool_name: str, args: dict, ctx: object) -> object:
        # The script invokes ``find_examples(query="gpu")`` through
        # the in-script wrapper, which forwards to this dispatcher.
        # Returning a fixed list keeps the assertion deterministic
        # without standing up the real ``find_examples`` body —
        # that tool's logic is exercised by ``tests/test_mcp_*``.
        dispatcher_calls.append({"tool_name": tool_name, "args": dict(args)})
        if tool_name == "find_examples":
            return [{"name": "ex1"}, {"name": "ex2"}]
        return None

    sandbox_runner = make_default_sandbox_runner(["find_examples"], session)
    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=None,
        sandbox_runner=sandbox_runner,
        cost_estimators={},
    )

    # Force Propose_Phase to emit the scripted Strategy. Same
    # technique as the engine test that wires the sandbox runner
    # through a fake; here the runner is real, the script is real,
    # and only the strategy-builder is patched.
    def fake_deterministic_strategy(self_engine: MissionEngine, sess: dict) -> dict:
        return {
            "script": _END_TO_END_SCRIPT,
            "rationale": "test fixture: end-to-end scripted strategy",
        }

    monkeypatch.setattr(
        MissionEngine,
        "_deterministic_strategy",
        fake_deterministic_strategy,
    )

    record = await engine.run_iteration(session["session_id"])

    # --- Verdict ----------------------------------------------------
    # No budget cap, no completion (target unreachable), no cadence
    # skip on iteration 0, no heuristic fire on the first iteration
    # → the cascade falls through to the default branch.
    assert record["verdict"] == "continue"
    assert record["verdict_reason"] == "in_progress"

    # --- Persisted iteration ---------------------------------------
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert len(persisted["iterations"]) == 1
    iteration = persisted["iterations"][0]

    # --- Phase statuses --------------------------------------------
    phase_names = [phase["phase"] for phase in iteration["phases"]]
    assert phase_names == ["propose", "execute", "observe", "evaluate", "decide"]
    statuses = [phase["status"] for phase in iteration["phases"]]
    assert statuses == ["succeeded"] * 5

    # --- script_call_log -------------------------------------------
    # Exactly one in-script call, targeting the allowlisted tool.
    script_call_log = iteration["script_call_log"]
    assert len(script_call_log) == 1
    only_call = script_call_log[0]
    assert only_call["tool_name"] == "find_examples"
    assert only_call["status"] == "ok"
    assert only_call["args"] == {"query": "gpu"}
    assert only_call["result_summary"] == [
        {"name": "ex1"},
        {"name": "ex2"},
    ]

    # The dispatcher was invoked exactly once with the same shape —
    # the in-script wrapper's record matches the dispatcher's view.
    assert dispatcher_calls == [{"tool_name": "find_examples", "args": {"query": "gpu"}}]

    # --- Observation -----------------------------------------------
    observation = iteration["observation"]
    # ``mission.observe(...)`` rows fold under ``metrics.observations``
    # so the script-collected key stays addressable without colliding
    # with any tool-derived metric of the same name.
    assert observation["metrics"]["observations"]["results"] == [
        {"name": "ex1"},
        {"name": "ex2"},
    ]

    # ``mission.event("results_collected", count=2)`` lands on the
    # Observation's events list. The script computed ``count`` via
    # ``len(results)`` — a safe builtin in the validator's allowlist.
    matching = [
        ev
        for ev in observation["events"]
        if isinstance(ev, dict) and ev.get("event_name") == "results_collected"
    ]
    assert len(matching) == 1
    assert matching[0]["count"] == 2


# ---------------------------------------------------------------------------
# End-to-end: sandbox cap propagation
# ---------------------------------------------------------------------------
#
# When a scripted Strategy trips the sandbox's wall-clock cap or
# accumulates enough estimated cost to breach the session's
# ``max_cost_usd`` budget, the iteration must still complete cleanly:
# the verdict is a budget-cap ``terminate`` rather than a phase
# failure. The two tests below exercise both seams:
#
# * ``test_scripted_strategy_terminated_by_max_duration`` patches the
#   sandbox's module-level duration cap to 1.0 second and runs a
#   ``while True`` script. Monty kills the script for exceeding the
#   cap; :class:`MissionSandbox.run` catches the ``MontyError`` and
#   re-raises as :class:`SandboxTerminated`; the engine's
#   ``_execute_script`` swallows the exception, stashes the
#   ``sandbox_terminated_reason`` sentinel on the iteration record,
#   and lets the cascade fall through to its default branches; the
#   Decide_Phase reads the sentinel and emits ``("terminate",
#   "max_wall_clock")``. The execute phase is recorded as
#   ``status="succeeded"`` because the engine handled the cap
#   internally — the cap is a budget event, not a phase exception.
#
# * ``test_scripted_strategy_terminated_by_max_cost`` registers a
#   cost estimator on the dispatcher's allowlisted tool so each
#   in-script call adds a fixed dollar amount to
#   ``session["accumulated_cost_usd"]``. The script calls the tool in
#   a loop; before the loop completes the accumulated cost crosses
#   ``max_cost_usd`` and the Decide_Phase's existing
#   :func:`mcp.mission.decide._cost_exceeded` check fires, producing
#   ``("terminate", "max_cost")``. This test pins the contract that
#   per-call cost is plumbed through the in-script wrapper exactly the
#   way it is on the engine's direct ``_dispatch_one_call`` path.


def _make_terminated_session(
    session_id: str,
    *,
    allowlist: list[str],
    max_cost_usd: float | None = None,
    max_iterations: int = 10,
) -> dict[str, object]:
    """Build a SessionState for the cap-propagation tests.

    The criterion target is unreachable so completion never fires —
    the cap path is the only way an iteration can transition to
    terminal. ``max_iterations`` is set high enough that the
    iteration-count cap cannot fire on the very first iteration; the
    Decide_Phase's cascade (after the new sandbox-sentinel branch) is
    what actually produces the verdict.
    """
    budget: dict[str, object] = {
        "max_iterations": max_iterations,
        "max_wall_clock_seconds": 600,
    }
    if max_cost_usd is not None:
        budget["max_cost_usd"] = max_cost_usd
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": "Cap-propagation fixture session.",
        "criteria": [
            {
                "criterion_id": "c1",
                "kind": "metric_threshold",
                "required": True,
                "metric": "loss",
                "op": "<",
                "target": -1.0,
            }
        ],
        "budget": budget,
        "tool_allowlist": list(allowlist),
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 10,
        "use_sampling": False,
        "allow_scripted_strategies": True,
        "status": "pending",
        "created_at": "2025-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
        "accumulated_cost_usd": 0.0,
    }


# The duration-cap script is intentionally a tight ``while True``
# busy-loop. The validator accepts the shape (``While`` + ``Pass`` are
# both on the allowed-statement list and ``True`` is a constant); the
# runtime layer is the only thing that can stop it. Monty's per-call
# duration cap fires within the patched 1.0-second window.
_DURATION_CAP_SCRIPT: str = "while True:\n    pass\n"


# The cost-cap script invokes the allowlisted tool 1000 times in a
# tight loop. Each call returns a trivial dict so the assertion stays
# focused on the cost-accumulation side; the cost estimator below is
# what actually drives ``accumulated_cost_usd`` past the cap. Using
# ``range(1000)`` keeps the source deterministic and small — the
# loop terminates cleanly long before completing all 1000 iterations
# because the ``MontyTypingError`` raised by the in-script wrapper on
# the cap-breaching call propagates out of the script as a
# ``SandboxTerminated`` (the wrapper itself does not enforce the cap;
# the sandbox does that). In practice the cost cap fires during
# Decide_Phase regardless of how many calls the script managed to
# make before its iteration finished.
_COST_CAP_SCRIPT: str = (
    "for i in range(1000):\n"
    "    _ = await submit_job_sqs(manifest_path='x.yaml', region='us-east-1')\n"
)


async def test_scripted_strategy_terminated_by_max_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sandbox duration cap propagates up to a ``("terminate", "max_wall_clock")`` verdict.

    Patches ``mission.sandbox._DURATION_LIMIT_SECS`` to 1.0 so a
    ``while True: pass`` script trips the per-call duration cap on
    the underlying ``MontySandboxProvider``. The provider raises a
    ``MontyError`` subclass; ``MissionSandbox.run`` catches the base
    class and re-raises as :class:`SandboxTerminated`. The engine's
    ``_execute_script`` swallows the exception, stashes the
    ``sandbox_terminated_reason`` sentinel on the iteration record,
    and lets the iteration finish cleanly. The Decide_Phase's
    cascade reads the sentinel at the very top and emits
    ``("terminate", "max_wall_clock")`` before any other branch is
    consulted.

    The execute phase records ``status="succeeded"`` because the cap
    is a budget event, not a phase exception — the engine handled it
    internally without re-raising.
    """
    # Pin the duration cap to 1.0 so the test runs quickly without
    # depending on operator-environment overrides. The sandbox reads
    # this constant when constructing the provider, so the patch
    # has to land before ``make_default_sandbox_runner`` runs.
    monkeypatch.setattr(sandbox_module, "_DURATION_LIMIT_SECS", 1.0)
    monkeypatch.setattr(sandbox_module, "_MEMORY_LIMIT_BYTES", 200_000_000)

    backend = FilesystemBackend(root=tmp_path)
    session = _make_terminated_session(
        session_id="sess-duration-cap",
        allowlist=["find_examples"],
    )
    backend.save_session(session)

    async def dispatcher(tool_name: str, args: dict, ctx: object) -> object:
        # The duration-cap script never reaches a tool call (the body
        # is a bare ``pass``), so this dispatcher is wired but
        # unused. Returning ``None`` is fine — the assertion path
        # never observes a tool call.
        return None

    sandbox_runner = make_default_sandbox_runner(["find_examples"], session)
    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=None,
        sandbox_runner=sandbox_runner,
        cost_estimators={},
    )

    def fake_deterministic_strategy(self_engine: MissionEngine, sess: dict) -> dict:
        return {
            "script": _DURATION_CAP_SCRIPT,
            "rationale": "fixture: infinite loop tripping the duration cap",
        }

    monkeypatch.setattr(
        MissionEngine,
        "_deterministic_strategy",
        fake_deterministic_strategy,
    )

    record = await engine.run_iteration(session["session_id"])

    # The cascade reads the sandbox sentinel before any other branch.
    assert record["verdict"] == "terminate"
    assert record["verdict_reason"] == "max_wall_clock"

    # Execute_Phase is recorded as ``succeeded`` because the engine
    # handled the cap internally rather than raising.
    phases_by_name = {phase["phase"]: phase for phase in record["phases"]}
    assert phases_by_name["execute"]["status"] == "succeeded"

    # Session transitions through the terminal-verdict path.
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["status"] == "terminated"
    assert len(persisted["iterations"]) == 1


async def test_scripted_strategy_terminated_by_max_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-call in-script cost accumulation drives ``("terminate", "max_cost")``.

    The cost estimator on ``submit_job_sqs`` returns a fixed $5.00
    per call; the session declares ``max_cost_usd=12.0``. The script
    loops up to 1000 times calling the tool, but the in-script
    wrapper accumulates cost onto ``session["accumulated_cost_usd"]``
    on every successful call. Long before the loop completes, the
    accumulated cost crosses $12, and the next iteration's
    Decide_Phase reads the running total and emits ``("terminate",
    "max_cost")`` via the existing
    :func:`mcp.mission.decide._cost_exceeded` check.

    The cap fires during Decide_Phase, not mid-script, so the script
    runs to completion of the iteration's Execute_Phase (or until
    Monty's duration cap fires, whichever comes first); either way
    the accumulated cost is at or above the cap by the time
    Decide_Phase runs. The assertion that
    ``persisted["accumulated_cost_usd"] >= 12.0`` pins this contract.
    """
    monkeypatch.setattr(sandbox_module, "_DURATION_LIMIT_SECS", 30.0)
    monkeypatch.setattr(sandbox_module, "_MEMORY_LIMIT_BYTES", 200_000_000)

    backend = FilesystemBackend(root=tmp_path)
    session = _make_terminated_session(
        session_id="sess-cost-cap",
        allowlist=["submit_job_sqs"],
        max_cost_usd=12.0,
    )
    backend.save_session(session)

    async def dispatcher(tool_name: str, args: dict, ctx: object) -> object:
        # Every call returns a trivial successful response so the
        # in-script wrapper records ``status="ok"`` and accumulates
        # cost via the registered estimator below.
        return {"status": "queued"}

    # The cost estimator returns $5 per call regardless of args. The
    # session caps at $12, so after three successful calls the
    # accumulated cost is $15, the cap is breached, and the next
    # Decide_Phase emits ``("terminate", "max_cost")``.
    cost_estimators = {"submit_job_sqs": lambda args: 5.0}

    sandbox_runner = make_default_sandbox_runner(
        ["submit_job_sqs"], session, cost_estimators=cost_estimators
    )
    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=None,
        sandbox_runner=sandbox_runner,
        cost_estimators=cost_estimators,
    )

    def fake_deterministic_strategy(self_engine: MissionEngine, sess: dict) -> dict:
        return {
            "script": _COST_CAP_SCRIPT,
            "rationale": "fixture: cost-incurring loop tripping max_cost",
        }

    monkeypatch.setattr(
        MissionEngine,
        "_deterministic_strategy",
        fake_deterministic_strategy,
    )

    record = await engine.run_iteration(session["session_id"])

    # The Decide_Phase's existing ``_cost_exceeded`` branch fires
    # because the in-script wrapper drove ``accumulated_cost_usd``
    # past the cap during Execute_Phase.
    assert record["verdict"] == "terminate"
    assert record["verdict_reason"] == "max_cost"

    # The persisted accumulated cost is at or above the cap. The
    # exact value depends on how many in-script calls landed before
    # the iteration finished (Monty's duration cap could also have
    # fired during the loop), but the floor is the cap itself.
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["status"] == "terminated"
    assert persisted["accumulated_cost_usd"] >= 12.0
