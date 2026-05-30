# Mission — Goal-Directed Iteration Loop

`mcp/mission/` is the internal package behind GCO's **Mission** feature: a
goal-directed iteration loop that drives an orchestrated workflow toward a set
of machine-checkable success criteria. An operator declares a natural-language
directive, a tool allowlist, and a budget; the engine runs repeated five-phase
iterations (propose → execute → observe → evaluate → decide) until it reaches a
terminal verdict.

Mission is off by default. Enable it by setting `GCO_ENABLE_MISSION=true` for
the MCP server (see [Feature Flags](../README.md#feature-flags)). For the
user-facing guide — concepts, CLI walkthrough, and worked examples — read
[`docs/MISSION.md`](../../docs/MISSION.md).

## Table of Contents

- [Overview](#overview)
- [The Five-Phase Loop](#the-five-phase-loop)
- [Module Map](#module-map)
  - [Engine and Lifecycle](#engine-and-lifecycle)
  - [Domain Types and State](#domain-types-and-state)
  - [Verdict and Cadence](#verdict-and-cadence)
  - [Input Validation and Sandboxes](#input-validation-and-sandboxes)
  - [Advisory Sampling](#advisory-sampling)
  - [Criteria Scaffolding](#criteria-scaffolding)
  - [Reporting and Audit](#reporting-and-audit)
  - [Wiring Helpers](#wiring-helpers)
- [Design Principles](#design-principles)
- [Related Documentation](#related-documentation)

## Overview

Mission turns a high-level goal into a controlled, auditable loop. The operator
supplies four things:

- A **directive** — the natural-language goal.
- **Success criteria** — machine-checkable predicates the loop evaluates each
  iteration to decide whether the goal is met.
- A **tool allowlist** — the subset of MCP tools the loop may call.
- A **budget** — caps on iterations and wall-clock time that bound the run.

The engine then iterates until it reaches one of four verdicts: `continue`
(keep going), `adjust` (revise the strategy), `complete` (criteria satisfied),
or `terminate` (budget exhausted or no progress). The verdict cascade is fully
deterministic; an optional advisory LLM can shape the *next strategy* on an
`adjust`, but it never moves the verdict itself.

## The Five-Phase Loop

Each iteration walks through the same five phases. Every phase emits exactly one
structured audit event, so a run is fully reconstructable from its audit trail.

| Phase | Responsibility |
|-------|----------------|
| **Propose** | Select the strategy (tool sequence or scripted strategy) for this iteration. |
| **Execute** | Run the proposed strategy against the tool allowlist. |
| **Observe** | Collect the results into a structured observation. |
| **Evaluate** | Check the observation against each success criterion. |
| **Decide** | Run the deterministic verdict cascade to pick the next action. |

## Module Map

### Engine and Lifecycle

| Module | Description |
|--------|-------------|
| [`engine.py`](engine.py) | The `MissionEngine` — drives one `run_iteration` lifecycle through all five phases, persists the iteration record, and writes a final report on a terminal verdict. Dependencies are injected at construction so phases stay pure and testable. |

### Domain Types and State

| Module | Description |
|--------|-------------|
| [`types.py`](types.py) | Shared `TypedDict` domain types (session state, iteration and phase records, verdict labels) so the engine, validators, and tool wrappers agree on one shape that round-trips through JSON. |
| [`state.py`](state.py) | The `MissionStateBackend` persistence protocol plus concrete backends (filesystem, DynamoDB) for loading, saving, listing, and deleting session records. |

### Verdict and Cadence

| Module | Description |
|--------|-------------|
| [`decide.py`](decide.py) | The pure, deterministic verdict cascade — given the session, the in-progress iteration, and a passed-in wall-clock value, returns the `(verdict, reason)` tuple. No I/O, no clock reads, no randomness. |
| [`checkpoints.py`](checkpoints.py) | Pure cadence resolver that decides whether a given iteration produces a real verdict or a synthetic `cadence_skip`, and records when the last real verdict happened. |

### Input Validation and Sandboxes

| Module | Description |
|--------|-------------|
| [`validation.py`](validation.py) | Pure shared validators that normalize operator-supplied JSON (criteria, strategies, budget) before it touches state, raising a structured error with a stable code on rejection. |
| [`predicate.py`](predicate.py) | Restricted AST evaluator for `predicate` criteria — parses an untrusted Python expression once, validates it against a tight allowlist, and evaluates it against an observation with builtins cleared. |
| [`sandbox.py`](sandbox.py) | Restricted AST validator for scripted strategies — the multi-statement counterpart to `predicate.py`, parsing untrusted script source in `exec` mode against an explicit node allowlist. |

### Advisory Sampling

| Module | Description |
|--------|-------------|
| [`sampling.py`](sampling.py) | Prompt builders for the optional advisory LLM path — assembles deterministic strategy-revision and final-report prompts from session data. Transport-agnostic and side-effect-free. |
| [`_environment.py`](_environment.py) | Gathers slow-moving live environment signals (deployed regions, per-region cluster metrics, reservation counts) that fill the optional environment-context block of the sampling prompt. |

### Criteria Scaffolding

| Module | Description |
|--------|-------------|
| [`criteria_scaffold.py`](criteria_scaffold.py) | Helpers behind `gco mission scaffold-criteria` — turns a natural-language directive into a validated criteria array, via a pure keyword-matching path or an async sampling-backed path with retry-on-rejection. |

### Reporting and Audit

| Module | Description |
|--------|-------------|
| [`final_report.py`](final_report.py) | Builds and atomically persists the durable JSON final report that ends a session, capturing the directive, criteria, budget, full iteration history, and terminal verdict. |
| [`audit.py`](audit.py) | Thin Mission-specific audit emitters for phase, verdict, and sampling events, each writing one structured entry through the shared audit logger. |

### Wiring Helpers

| Module | Description |
|--------|-------------|
| [`_engine_factory.py`](_engine_factory.py) | Shared factory that builds a production-wired `MissionEngine` (live tool dispatcher, sampling callable, sandbox runner) for both the MCP tool surface and the CLI, plus a stub dispatcher for `--dry-run` smoke tests. |

## Design Principles

- **Deterministic core, advisory edge.** The verdict cascade is pure and
  reproducible. LLM sampling is strictly advisory — it can shape the next
  strategy on an `adjust`, but it never changes the verdict.
- **Untrusted input is sandboxed at parse time.** Predicate expressions and
  scripted strategies are validated against tight AST allowlists before any
  execution, so a malformed or hostile input is rejected immediately rather
  than on iteration N.
- **Pure functions take their context as arguments.** Validators, the verdict
  cascade, and the cadence resolver perform no I/O and read no clocks — the
  caller passes in the wall-clock value and any external context, which keeps
  them trivial to unit-test.
- **Bounded by budget.** Every run terminates cleanly when an iteration or
  wall-clock cap fires.

## Related Documentation

- [`docs/MISSION.md`](../../docs/MISSION.md) — the user-facing Mission guide
  (concepts, CLI usage, examples).
- [`mcp/README.md`](../README.md) — the GCO MCP server, including the
  [`GCO_ENABLE_MISSION`](../README.md#feature-flags) feature flag and the
  `mission_*` tools.
