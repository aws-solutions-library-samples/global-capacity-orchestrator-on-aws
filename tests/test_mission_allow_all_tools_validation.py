"""Property-based checks for the Mission all-tools allowlist resolver.

When a session asks for every registered tool, the resolver expands that
request to the concrete set of currently-registered tool names with the
session-management control tools removed. The expansion is a pure set
operation: it reads only the keys of the registered-tools mapping, subtracts
the control names it is handed, and hands the survivors through the same
allowlist validator an operator-typed list would pass through.

The check below pins the central expansion invariant: the resolved list is
exactly the registered names minus the control names, sorted and free of
duplicates, a subset of what was registered, disjoint from the control set,
and stable under a second pass of the allowlist validator. Each generated
registry carries the full vocabulary of classification tags (``safe``,
``low-risk``, ``destructive``, ``infrastructure``, ``cost-incurring``,
``data-upload``, ``image``) so the check also confirms a tool is never
dropped on account of its tag — only control membership decides exclusion.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

# Mirror the import pattern the other Mission tests use: ``gco_mcp/run_mcp.py``
# adds ``gco_mcp/`` to ``sys.path`` at runtime, but pytest has to do it itself
# before the import below resolves.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission import validation  # noqa: E402

# The full tag vocabulary applied to MCP tools. Every generated registry
# carries one tool of each tag so the expansion is exercised against the
# whole classification space on every example.
_TAGS: tuple[str, ...] = (
    "safe",
    "low-risk",
    "destructive",
    "infrastructure",
    "cost-incurring",
    "data-upload",
    "image",
)

# Tool names: non-empty, printable, whitespace-free ASCII so every name is a
# valid allowlist entry that the underlying validator accepts.
_TOOL_NAME = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126, blacklist_categories=("Cs",)),
    min_size=1,
    max_size=12,
)


def _tag_tool_name(tag: str) -> str:
    """A stable per-tag tool name, distinct from generated arbitrary names."""
    return f"__tagged__{tag}"


@st.composite
def _registry_and_control(draw):  # type: ignore[no-untyped-def]
    """Build a registered-tools mapping and an overlapping control set.

    The registry always contains one tool of every classification tag, plus
    an arbitrary number of additional tools each tagged with an arbitrary
    value. The control set mixes names drawn from the registry with invented
    names that are not registered, giving arbitrary overlap. Examples whose
    registered-minus-control set would be empty are discarded so the resolver
    takes its non-empty expansion path.
    """
    registered: dict[str, set[str]] = {}

    # Arbitrary extra tools with arbitrary tags.
    extra_names = draw(st.lists(_TOOL_NAME, max_size=12, unique=True))
    for name in extra_names:
        registered[name] = {draw(st.sampled_from(_TAGS))}

    # Guarantee one tool of every tag. Applied last so a name clash with an
    # arbitrary tool can never drop a tag from the registry.
    for tag in _TAGS:
        registered[_tag_tool_name(tag)] = {tag}

    registered_names = list(registered)

    # Control set with arbitrary overlap: some names lifted from the registry
    # (so they will be excluded) and some invented names that are absent from
    # it (so they exercise the "control name not even registered" case).
    overlap = draw(st.lists(st.sampled_from(registered_names), max_size=len(registered_names)))
    outsiders = draw(st.lists(_TOOL_NAME, max_size=8))
    control = set(overlap) | set(outsiders)

    # Keep only registries where at least one non-control tool survives.
    assume(sorted(set(registered) - control))
    return registered, control


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(data=_registry_and_control())
def test_all_tools_resolution_is_registered_set_minus_control_tools(
    data: tuple[dict[str, set[str]], set[str]],
) -> None:
    """Expanding to all tools yields exactly the registry minus the control set.

    The resolved list must equal ``sorted(set(registered) - control)`` exactly:
    duplicate-free, a subset of the registered names, disjoint from the control
    names, and unchanged when re-run through the allowlist validator. A tool of
    every classification tag survives the expansion unless it is a control
    name, confirming the tag itself never causes a drop.
    """
    registered, control = data

    result = validation.resolve_effective_allowlist(
        allow_all_tools=True,
        explicit_allowlist=None,
        registered_tools=registered,
        control_tools=control,
    )

    expected = sorted(set(registered) - control)

    # Exact equality is the core invariant.
    assert result == expected

    # No duplicates survive the expansion.
    assert len(result) == len(set(result))

    # Every resolved name was registered; none came from outside the registry.
    assert set(result) <= set(registered)

    # No control name leaks into the resolved list.
    assert set(result).isdisjoint(control)

    # The resolved list satisfies every invariant an operator-typed list would:
    # re-running the registry validator returns it unchanged.
    assert validation.validate_tool_allowlist(result, registered) == result

    # Tag-agnosticism: a tool of each tag is retained unless it is a control
    # name, regardless of which tag it carries.
    for tag in _TAGS:
        tagged = _tag_tool_name(tag)
        if tagged not in control:
            assert tagged in result


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    registered=st.dictionaries(_TOOL_NAME, st.sets(st.sampled_from(_TAGS)), max_size=12),
    explicit=st.lists(_TOOL_NAME, min_size=1, max_size=12),
)
def test_all_tools_with_an_explicit_list_is_rejected_as_mutually_exclusive(
    registered: dict[str, set[str]],
    explicit: list[str],
) -> None:
    """Asking for all tools while also naming tools is a contradiction.

    When the all-tools expansion is requested at the same time as a non-empty
    explicit list of tool names, the two inputs disagree about what the
    session may reach. The resolver refuses to guess: it raises a validation
    error tagged ``allow_all_and_explicit_allowlist_mutually_exclusive`` and
    returns nothing, so no allowlist — and therefore no session — can be built
    from the conflicting request. The registered set is irrelevant to this
    decision; the conflict is detected before any expansion happens.
    """
    with pytest.raises(validation.MissionValidationError) as exc_info:
        validation.resolve_effective_allowlist(
            allow_all_tools=True,
            explicit_allowlist=explicit,
            registered_tools=registered,
        )

    error = exc_info.value
    assert error.code == "validation_error"
    assert error.details is not None
    assert error.details["reason"] == "allow_all_and_explicit_allowlist_mutually_exclusive"


# The three distinct, stable reason tokens the resolver attaches when it
# refuses an allowlist request. They are deliberately different strings so an
# operator can tell the three rejection conditions apart from the token alone.
_EMPTY_REGISTRY_REASON = "allow_all_tools_empty_registry"
_EMPTY_LIST_REASON = "empty"
_MUTUALLY_EXCLUSIVE_REASON = "allow_all_and_explicit_allowlist_mutually_exclusive"


@st.composite
def _rejection_case(draw):  # type: ignore[no-untyped-def]
    """Draw one rejection scenario plus the reason token it should produce.

    The resolver refuses an allowlist request under three separate conditions,
    and this strategy spreads its examples evenly across all three:

    * Empty expansion — all-tools is requested with no explicit list, but the
      registered names minus the control names come out empty. This covers
      both an empty registry and a registry whose every name is a control
      name. The expected token is ``allow_all_tools_empty_registry``.
    * Empty explicit list — all-tools is not requested and the explicit list is
      missing (``None``) or empty (``[]``). The expected token is ``empty``.
    * Conflicting request — all-tools is requested together with a non-empty
      explicit list. The expected token is
      ``allow_all_and_explicit_allowlist_mutually_exclusive``.

    Returns a ``(kwargs, expected_reason)`` pair: the keyword arguments to hand
    the resolver and the single reason token that call must raise with.
    """
    which = draw(st.sampled_from(("empty_expansion", "empty_list", "conflict")))

    if which == "empty_expansion":
        # Control names are arbitrary; the registry holds only a subset of
        # them, so registered-minus-control is always empty (the ``S ⊆ C`` and
        # ``S empty`` cases both fall out of this construction). The explicit
        # list stays falsy so the conflict branch is not taken instead.
        control_names = draw(st.lists(_TOOL_NAME, max_size=8, unique=True))
        reg_keys = (
            draw(st.lists(st.sampled_from(control_names), unique=True)) if control_names else []
        )
        registered = {name: {draw(st.sampled_from(_TAGS))} for name in reg_keys}
        kwargs = {
            "allow_all_tools": True,
            "explicit_allowlist": draw(st.sampled_from([None, []])),
            "registered_tools": registered,
            "control_tools": set(control_names),
        }
        return kwargs, _EMPTY_REGISTRY_REASON

    if which == "empty_list":
        registered = draw(st.dictionaries(_TOOL_NAME, st.sets(st.sampled_from(_TAGS)), max_size=12))
        kwargs = {
            "allow_all_tools": False,
            "explicit_allowlist": draw(st.sampled_from([None, []])),
            "registered_tools": registered,
        }
        return kwargs, _EMPTY_LIST_REASON

    # which == "conflict"
    registered = draw(st.dictionaries(_TOOL_NAME, st.sets(st.sampled_from(_TAGS)), max_size=12))
    explicit = draw(st.lists(_TOOL_NAME, min_size=1, max_size=12))
    kwargs = {
        "allow_all_tools": True,
        "explicit_allowlist": explicit,
        "registered_tools": registered,
    }
    return kwargs, _MUTUALLY_EXCLUSIVE_REASON


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(case=_rejection_case())
def test_each_rejection_condition_carries_its_own_distinct_reason(
    case: tuple[dict, str],
) -> None:
    """Every rejection condition reports its own reason and never another's.

    The resolver has three separate ways to refuse a request — an empty
    all-tools expansion, an empty explicit list, and an all-tools request that
    also names tools. Each one must raise a validation error whose ``reason``
    is its own dedicated token, and the three tokens must stay distinct so no
    single input could be read as two different failures at once. For every
    generated scenario the raised reason equals the token that scenario should
    produce and matches neither of the other two.
    """
    kwargs, expected_reason = case

    # The three tokens are genuinely different strings, so matching one rules
    # out the other two by construction.
    all_reasons = {
        _EMPTY_REGISTRY_REASON,
        _EMPTY_LIST_REASON,
        _MUTUALLY_EXCLUSIVE_REASON,
    }
    assert len(all_reasons) == 3

    with pytest.raises(validation.MissionValidationError) as exc_info:
        validation.resolve_effective_allowlist(**kwargs)

    error = exc_info.value
    assert error.code == "validation_error"
    assert error.details is not None
    assert error.details["field"] == "tool_allowlist"

    reason = error.details["reason"]

    # The scenario maps to exactly its own reason ...
    assert reason == expected_reason
    # ... and never collides with either of the other two classes.
    assert reason not in (all_reasons - {expected_reason})


@st.composite
def _explicit_list_inputs(draw):  # type: ignore[no-untyped-def]
    """Draw a registry, a non-empty explicit list, and a flag-lookup mapping.

    The all-tools switch is left off here, so the resolver should behave as a
    transparent pass-through to the underlying allowlist validator. To make
    that pass-through meaningful the inputs are constructed to land on both
    sides of the validator's decision:

    * The registry holds an arbitrary set of tool names (each carries an
      arbitrary classification tag; only the names are read).
    * The explicit list mixes names lifted from the registry (which the
      validator accepts) with invented names absent from it (which the
      validator rejects as not-registered), and may repeat a name so the
      duplicate-name rejection is reached too. It is never empty.
    * The flag-lookup is ``None``, an empty mapping, or a mapping that favours
      the not-registered names so the rejection's ``flag`` annotation is
      populated on at least some examples.
    """
    registered_names = draw(st.lists(_TOOL_NAME, max_size=10, unique=True))
    registered = {name: {draw(st.sampled_from(_TAGS))} for name in registered_names}

    # Invented names deliberately kept out of the registry so the explicit list
    # can drive the not-registered rejection. Drop any accidental overlap.
    raw_outsiders = draw(st.lists(_TOOL_NAME, max_size=6, unique=True))
    outsiders = [name for name in raw_outsiders if name not in registered]

    pool = registered_names + outsiders
    if pool:
        # Sampling with repeats lets a name appear twice, reaching the
        # duplicate-name rejection as well as the registered/not-registered mix.
        explicit = draw(st.lists(st.sampled_from(pool), min_size=1, max_size=10))
    else:
        explicit = draw(st.lists(_TOOL_NAME, min_size=1, max_size=10))

    # flag_lookup is dict[str, str] | None. Bias the keys toward names that may
    # be missing from the registry so the validator's flag annotation fires.
    flag_candidates = registered_names + outsiders + explicit
    if flag_candidates and draw(st.booleans()):
        keys = draw(st.lists(st.sampled_from(flag_candidates), max_size=8, unique=True))
        flag_lookup: dict[str, str] | None = {name: f"GCO_ENABLE_{name}" for name in keys}
    else:
        flag_lookup = draw(st.sampled_from([None, {}]))

    return registered, explicit, flag_lookup


def _capture_outcome(call):  # type: ignore[no-untyped-def]
    """Run ``call`` and capture either its return value or its error signature.

    Returns ``("returned", value)`` when the call produces a value, or
    ``("raised", code, details)`` when it raises a validation error. Two calls
    can then be compared for an identical outcome whether they return a list or
    reject with an error — a raised error is compared by its code and its full
    structured details, not by object identity.
    """
    try:
        return ("returned", call())
    except validation.MissionValidationError as exc:
        return ("raised", exc.code, exc.details)


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(inputs=_explicit_list_inputs())
def test_not_all_tools_branch_matches_the_underlying_allowlist_validator(
    inputs: tuple[dict[str, set[str]], list[str], dict[str, str] | None],
) -> None:
    """With all-tools off, resolution is the plain allowlist validator verbatim.

    When the all-tools switch is off and an explicit list is supplied, the
    resolver must neither add to nor subtract from what the underlying
    allowlist validator already does: for the same registry, explicit list, and
    flag-lookup, the resolver returns the identical normalized list the
    validator returns, and when the validator rejects the list the resolver
    raises the identical error — same code and same structured details. The two
    outcomes are captured and compared so both the accepting and the rejecting
    paths are pinned to the validator's behaviour exactly.
    """
    registered, explicit, flag_lookup = inputs

    via_resolver = _capture_outcome(
        lambda: validation.resolve_effective_allowlist(
            allow_all_tools=False,
            explicit_allowlist=explicit,
            registered_tools=registered,
            flag_lookup=flag_lookup,
        )
    )
    via_validator = _capture_outcome(
        lambda: validation.validate_tool_allowlist(explicit, registered, flag_lookup)
    )

    assert via_resolver == via_validator


# ---------------------------------------------------------------------------
# Concrete example checks
#
# The property checks above sweep the resolver across a wide input space. The
# deterministic examples below pin a handful of fixed, hand-picked scenarios so
# the exact returned list and the exact rejection signatures are nailed down in
# isolation, independent of any generated data.
# ---------------------------------------------------------------------------


def test_known_registry_with_a_control_tool_resolves_to_sorted_survivors() -> None:
    """A fixed registry containing one control name resolves to the rest, sorted.

    Five tools are registered, one of which is a control name handed in as the
    excluded set. The expansion drops that one control name and returns the
    four survivors in sorted order, free of duplicates and with the control
    name absent.
    """
    registered = {
        "list_jobs": {"safe"},
        "find_docs": {"safe"},
        "cost_summary": {"cost-incurring"},
        "find_examples": {"low-risk"},
        "mission_status": {"safe"},
    }
    control = {"mission_status"}

    result = validation.resolve_effective_allowlist(
        allow_all_tools=True,
        explicit_allowlist=None,
        registered_tools=registered,
        control_tools=control,
    )

    assert result == ["cost_summary", "find_docs", "find_examples", "list_jobs"]
    assert "mission_status" not in result
    assert len(result) == len(set(result))


def test_empty_registry_is_rejected_as_empty_registry() -> None:
    """Asking for all tools when nothing is registered is refused.

    With an empty registry there is nothing to expand to, so the resolver
    refuses the request with the empty-registry reason and never returns a
    list.
    """
    with pytest.raises(validation.MissionValidationError) as exc_info:
        validation.resolve_effective_allowlist(
            allow_all_tools=True,
            explicit_allowlist=None,
            registered_tools={},
        )

    error = exc_info.value
    assert error.code == "validation_error"
    assert error.details is not None
    assert error.details["field"] == "tool_allowlist"
    assert error.details["reason"] == _EMPTY_REGISTRY_REASON


def test_registry_of_only_control_tools_is_rejected_as_empty_registry() -> None:
    """A registry holding nothing but control names also resolves to empty.

    When every registered name is a control name, subtracting the control set
    leaves nothing to allow, so the resolver raises the same empty-registry
    reason it raises for a genuinely empty registry.
    """
    registered = {
        "mission_start": {"safe"},
        "mission_status": {"safe"},
        "mission_iterate": {"safe"},
    }
    control = {"mission_start", "mission_status", "mission_iterate"}

    with pytest.raises(validation.MissionValidationError) as exc_info:
        validation.resolve_effective_allowlist(
            allow_all_tools=True,
            explicit_allowlist=None,
            registered_tools=registered,
            control_tools=control,
        )

    error = exc_info.value
    assert error.code == "validation_error"
    assert error.details is not None
    assert error.details["reason"] == _EMPTY_REGISTRY_REASON


def test_all_tools_with_an_explicit_list_is_rejected_as_mutually_exclusive_example() -> None:
    """Naming tools while also asking for all of them is a fixed contradiction.

    A concrete registry paired with a concrete non-empty explicit list is
    refused with the mutual-exclusivity reason; the registry contents are
    irrelevant because the conflict is detected before any expansion.
    """
    registered = {"list_jobs": {"safe"}, "find_docs": {"safe"}}

    with pytest.raises(validation.MissionValidationError) as exc_info:
        validation.resolve_effective_allowlist(
            allow_all_tools=True,
            explicit_allowlist=["list_jobs"],
            registered_tools=registered,
        )

    error = exc_info.value
    assert error.code == "validation_error"
    assert error.details is not None
    assert error.details["reason"] == _MUTUALLY_EXCLUSIVE_REASON


def test_not_all_tools_with_empty_list_is_rejected_as_empty() -> None:
    """With all-tools off and no names supplied, the empty reason is raised.

    The explicit path is taken and an empty list has nothing to allow, so the
    resolver delegates to the underlying validator's existing empty rejection.
    """
    with pytest.raises(validation.MissionValidationError) as exc_info:
        validation.resolve_effective_allowlist(
            allow_all_tools=False,
            explicit_allowlist=[],
            registered_tools={"list_jobs": {"safe"}},
        )

    error = exc_info.value
    assert error.code == "validation_error"
    assert error.details is not None
    assert error.details["field"] == "tool_allowlist"
    assert error.details["reason"] == _EMPTY_LIST_REASON


def test_default_control_set_excludes_the_nine_session_management_names() -> None:
    """Omitting the control set falls back to the built-in nine-name exclusion.

    The resolver is called without an explicit control set, so it uses its
    default. A registry that contains all nine built-in session-management
    names alongside three ordinary tools resolves to exactly the three ordinary
    tools, sorted; none of the nine built-in names survive.
    """
    ordinary = {
        "list_jobs": {"safe"},
        "find_docs": {"safe"},
        "cost_summary": {"cost-incurring"},
    }
    registered = {name: {"safe"} for name in validation.MISSION_CONTROL_TOOLS}
    registered.update(ordinary)

    result = validation.resolve_effective_allowlist(
        allow_all_tools=True,
        explicit_allowlist=None,
        registered_tools=registered,
    )

    assert result == ["cost_summary", "find_docs", "list_jobs"]
    assert set(result).isdisjoint(validation.MISSION_CONTROL_TOOLS)
    # All nine built-in control names are kept out of the resolved list.
    assert len(validation.MISSION_CONTROL_TOOLS) == 9
    for control_name in validation.MISSION_CONTROL_TOOLS:
        assert control_name not in result
