# Swarm

Swarm is supervised fleets of [Mission](MISSION.md) sessions: one
**orchestrator** session spawns and drives concurrent **child** sessions
until the orchestrator's own deterministic verdict cascade reaches a
terminal verdict. The orchestrator is an ordinary Mission session with a
`role` — it has a directive, machine-checkable criteria, a budget, an
audit trail, and a Final_Report — and its criteria evaluate over a
deterministic snapshot of its children's states.

The whole feature is gated: set `GCO_ENABLE_SWARM=true` (or the umbrella
`GCO_ENABLE_ALL_TOOLS=true`) before any `gco swarm` subcommand or
`swarm_*` MCP tool resolves. Enabling `GCO_ENABLE_MISSION` alongside is
recommended so children are individually inspectable through the mission
surfaces.

## Table of Contents

- [The supervisor model](#the-supervisor-model)
- [The determinism boundary](#the-determinism-boundary)
- [The rails](#the-rails)
- [The spawn contract](#the-spawn-contract)
- [Fleet criteria and the Children_Observation](#fleet-criteria-and-the-children_observation)
- [Restart policies](#restart-policies)
- [Plans: scaffold, review, run](#plans-scaffold-review-run)
- [Quickstart (no AWS required)](#quickstart-no-aws-required)
- [Observability: rollup, heartbeats, audit](#observability-rollup-heartbeats-audit)
- [Crash recovery and the single-runner guard](#crash-recovery-and-the-single-runner-guard)
- [Exhaustion and termination](#exhaustion-and-termination)
- [Cost](#cost)
- [Developing and testing swarm locally](#developing-and-testing-swarm-locally)
- [Surfaces](#surfaces)

## The supervisor model

A swarm is a supervisor tree one level deep:

- The **orchestrator** session carries the swarm rails (`swarm` config),
  a **child registry** (one entry per supervised slot), and an
  allowlist that includes three **supervisor tools**: `mission_spawn`,
  `children_status`, and `child_abort`.
- **Child** sessions are ordinary Mission sessions with
  `parent_session_id` set. They run concurrently as asyncio tasks inside
  the driving process, each looping the standard five-phase engine to a
  terminal verdict, bounded by the swarm's concurrency semaphore.
- A **slot** is the stable supervision identity. Respawns point the slot
  at a replacement session; lineage is preserved on the registry entry.

The supervisor tools are deliberately **not** MCP tools. They exist only
as in-process dispatcher entries wired into an orchestrator engine, and
the allowlist resolver excludes them — along with the `mission_*` control
tools and the `swarm_*` MCP tools — from every resolvable session
allowlist. A child can never spawn (depth is fixed at one), no session
can drive sessions through its Execute phase, and every spawn passes the
full admission pipeline regardless of who proposed it.

## The determinism boundary

Swarm keeps Mission's core invariant: **the control path is fully
deterministic; sampling is advisory only.**

| Decision | Mechanism | Sampled? |
|---|---|---|
| Orchestrator verdicts | the unchanged Mission verdict cascade | No |
| Child verdicts | the unchanged Mission verdict cascade | No |
| Spawn admission | pure validators (rails, budgets, allowlists, overlap) | No |
| Respawn on child failure | fixed per-slot restart policy + `max_respawns` | No |
| Fleet snapshot and aggregates | pure, slot-ordered read of persisted state | No |
| Which children to propose | advisory sampler; validated at proposal and again at dispatch | Yes (advisory) |
| Replacement directive text on revision respawns | advisory sampler over the failed child's report lessons | Yes (advisory) |

The recommended default posture follows from the table: a **sampled
orchestrator supervising no-sampling children**. Children default to
`use_sampling=false` — intelligence at the root, determinism at the
leaves, and one sampling loop instead of N+1.

## The rails

The swarm config is validated at start time and persisted on the
orchestrator session:

| Key | Default | Meaning |
|---|---|---|
| `max_children` | required | Cap on live (non-settled) child slots. No `-1` sentinel — an unbounded fleet is exactly what this rail prevents. |
| `child_iteration_pool` | required | Pooled iteration budget. Every spawn reserves the child's `max_iterations` from it; settling a terminal child refunds the unconsumed remainder. |
| `max_concurrent_children` | `3` | Semaphore bound on simultaneously advancing children — the swarm's primary throughput control. |
| `allow_overlapping_mutating_tools` | `false` | Opt-out of the rule that two live children must not share a non-`safe`-tagged tool. |

Child budgets are **mandatory-finite**: both `max_iterations` and
`max_wall_clock_seconds` must be strictly positive, and the `-1` uncapped
sentinel Mission budgets accept is rejected on children. A supervised
worker must be self-terminating on both axes even if its supervisor
dies, and the iteration cap doubles as the slot's pool reservation.

The orchestrator's own `BudgetControls` bound the swarm overall. When the
orchestrator reaches any terminal verdict, the runner cancels child
drivers and aborts every non-terminal child before terminal finalization
completes, settling each slot's reservation.

## The spawn contract

Every spawn — scaffolded plan entry, sampled proposal, or respawn — runs
one admission pipeline, first failure wins, each rejection carrying a
stable `details.reason` token:

1. depth — only an orchestrator session may spawn
2. slot shape (1–64 chars, alphanumeric plus `. _ -`) and uniqueness
3. child budget shape (strictly positive; `-1` rejected)
4. restart policy and `max_respawns`
5. `use_sampling` shape (children default `false`)
6. fleet cap over live slots
7. iteration-pool balance
8. directive, criteria, and cadence via the shared Mission validators
9. allowlist resolution (loop-management names unreachable)
10. mutating-tool overlap against live siblings (unless opted out)

Rejections come back as the standard structured envelope; on a sampled
orchestrator the rejection reason feeds the next strategy-revision
prompt, so the model learns the exact rule it broke.

## Fleet criteria and the Children_Observation

Every orchestrator iteration merges a deterministic fleet snapshot into
the Observation at the end of the Observe phase:

```jsonc
obs["children"] = [
  {"slot": "docs-worker", "session_id": "mission-...", "status": "completed",
   "final_verdict": "complete", "respawn_count": 0, "iterations_consumed": 1},
  {"slot": "examples-worker", "session_id": "mission-...", "status": "running",
   "respawn_count": 0, "iterations_consumed": 2}
]
obs["metrics"] += {"children_total": 2, "children_running": 1,
                   "children_completed": 1, "children_failed": 0,
                   "iteration_pool_remaining": 4}
```

Entries are slot-ordered and built from persisted state only, so
identical child states produce byte-identical observations. A child
whose session cannot be loaded surfaces with the distinct
`"unreadable"` status token (counted as failed) rather than being
omitted — criteria read unmet or inconclusive, never falsely met. A slot
whose session ended unmet but whose restart policy still owes it a
respawn reports `"respawning"` and counts as running, so fleet criteria
don't fire in the window between a failure and its replacement.

Ordinary Mission criteria express fleet goals with no new criterion
kind:

```json
[
  {"criterion_id": "fleet_completed", "kind": "metric_threshold",
   "required": true, "metric": "metrics.children_completed", "op": ">=", "target": 2},
  {"criterion_id": "no_failures", "kind": "metric_threshold",
   "required": true, "metric": "metrics.children_failed", "op": "==", "target": 0}
]
```

Predicates work too — `all(c.get('status') == 'completed' for c in
obs['children'])` uses only constructs the predicate sandbox already
allows.

## Restart policies

Fixed per slot at spawn time; never chosen by the sampler at failure
time:

| Policy | Behavior |
|---|---|
| `never` (default) | One shot. `max_respawns` is forced to 0. |
| `on_failure` | A child ending `failed`/`terminated` without meeting its criteria is respawned with the same directive, up to `max_respawns` (default 1). |
| `on_failure_with_revision` | Same, but the replacement directive may be revised from the failed child's Final_Report lessons (advisory sampling; falls back to the verbatim directive). |

A `completed` child is never respawned. Respawns draw from the same
iteration pool and fleet cap as first spawns — the pool refund from the
failed attempt is what typically funds the retry.

## Plans: scaffold, review, run

A **Swarm_Plan** is a JSON array of spawn requests, admission-validated
across the whole plan (the pool, the fleet cap, and the mutating-tool
overlap rule are enforced entry-by-entry against the registry the plan
would build). A returned plan cannot be rejected at spawn time against
the same rails and registered-tool set.

Two producers:

- **Sampled** — a sampling backend proposes the decomposition; every
  entry runs through the spawn validators; rejections feed a bounded
  retry loop with the precise reason; exhaustion falls back
  deterministically.
- **Deterministic** (always available, the CI path) — one worker
  mirroring the swarm directive, criteria from the Mission criteria
  scaffold, `restart_policy: never`, budget bounded by the pool.

**When reviewing a plan, strike any criterion that cannot be decided.**
Mission completes a session only when every required criterion is met
*and no criterion is inconclusive* — and that second half counts
criteria marked `required: false` too. A criterion over a metric path the
child's tools never emit (`metrics.results_count`, say, when the tool
returns a plain result list) is therefore permanently undecidable: the
child cannot complete, burns its whole budget, terminates unmet, and the
orchestrator's fleet criteria never go met either, so the swarm runs to
its own cap. One speculative criterion can cost a fleet its entire pool.
The plan prompt warns the model about this, but sampled plans are still
worth reading for it: prefer `tool_call_succeeded`, and only use
`metric_threshold` / `metric_trend` for metrics you know are emitted.

**Review deterministic criteria before trusting a green verdict.** The
Mission criteria scaffold derives concrete criteria (for example a
`tool_call_succeeded` entry) only when it recognises the directive's
shape against the allowlist. Otherwise it emits its documented
placeholder — a `predicate` criterion whose expression is `True`,
labelled `TODO: replace this placeholder with a real success condition`.
A child holding that placeholder does one round of real work and then
completes, because the criterion is met however that round turned out,
and the orchestrator's fleet criteria in turn go `met` off the completed
child. The swarm reports `complete` / `criteria_met` without having
attested anything about the goal. This is inherited Mission scaffold
behaviour, not swarm-specific, but a fleet multiplies it: prefer
`gco swarm scaffold-plan` and read the criteria before `gco swarm run`,
or supply a sampled plan.

`gco swarm scaffold-plan` (CLI) and `swarm_plan` (MCP) expose the
scaffolder standalone for the review-then-run workflow; `gco swarm run`
chains scaffold → start → prime → drive. Plan spawns are dispatched through the
runner's spawn seam before the first orchestrator iteration, so the
iteration records show the fleet arriving through the
Children_Observation while the child-lifecycle audit carries every
spawn.

## Quickstart (no AWS required)

Swarm inherits Mission's portability: safe-tier catalog tools
(`find_docs`, `find_examples`) run against in-process fixtures, so a
whole fleet completes with no AWS credentials.

```bash
export GCO_ENABLE_SWARM=true

# Review the plan first:
gco swarm scaffold-plan \
  --directive "Find documentation about inference endpoints." \
  --tool-allowlist find_docs \
  --no-sampling

# Then run it (add --dry-run to smoke-test the loop mechanics only):
gco swarm run \
  --directive "Find documentation about inference endpoints." \
  --tool-allowlist find_docs \
  --max-children 2 --child-iteration-pool 10 \
  --no-sampling
```

Spawn envelopes and per-iteration verdicts stream to stderr as JSON
lines; the orchestrator's Final_Report — including the per-child outcome
table under `swarm_children` — lands on stdout.

## Observability: rollup, heartbeats, audit

- **Rollup** — `gco swarm status SESSION_ID` (and the `swarm_status`
  MCP tool) return one document: rails, pool balance
  (`reserved`/`consumed`/`remaining`), runner heartbeat state, the
  slot-ordered child table, and a findings list (orphaned runner,
  unreadable children, exhausted pool).
- **Heartbeats** — the runner writes one disk-backed task-status record
  for the swarm (`swarm-{session_id}`) and one per slot
  (`swarm-{session_id}-{slot}`) under `~/.gco/tasks/`, readable through
  `gco tasks list` and the `task_status` MCP tool, with the channel's
  standard PID-based orphan detection.
- **Audit** — every spawn, respawn, respawn denial, terminal
  transition, and supervisor abort emits a
  `mission_child_lifecycle_event` carrying the **parent** session id
  (so one filtered read reconstructs the fleet's history) plus the
  child id and slot. Child engines keep emitting their own phase,
  verdict, and sampling events under their own session ids.
- **Reports and memory** — children and orchestrators write Final_Reports
  and mission-memory items through the unchanged Mission terminal path.
  The orchestrator's report gains a `swarm_children` outcome table,
  refreshed after the abort cascade so the durable artifact reflects
  final settled states.

## Crash recovery and the single-runner guard

All state is persisted per iteration, so a dead driving process loses
nothing: the swarm heartbeat reads as orphaned, every child session
remains individually inspectable and abortable through the mission
surfaces, and the next `gco swarm iterate SESSION_ID` re-schedules every
live slot and runs restart-policy evaluation for children that went
terminal while unsupervised. Spawned children a crash orphaned before
the registry flush are adopted on startup with conservative
reservations, so pool accounting stays honest across the crash.

Exactly one live runner may drive a swarm at a time. The runner probes
the swarm heartbeat before starting: a `running` record under a live
foreign PID refuses startup with `swarm_runner_active` naming the
holder; a dead PID reads as orphaned and is taken over. The guard is
advisory and same-host by design — the scope of the disk-backed task
channel.

`gco swarm iterate --max-orchestrator-iterations N` bounds one call and
**detaches** rather than terminates: drivers are cancelled, children stay
resumable, no abort cascade runs, and the heartbeat is released.

## Exhaustion and termination

Swarm adds no branches to the Mission verdict cascade. The existing
branches terminate every fleet shape:

- **Completion** — the fleet criteria go `met`.
- **Budget caps** — the orchestrator's own `max_iterations` /
  `max_wall_clock_seconds` fire first in the cascade, exactly as they do
  for any Mission session.
- **Stagnation** — an exhausted pool with unmet criteria produces no
  criterion transitions, so `no_progress_counter` reaches
  `stagnation_threshold` and the session terminates `no_progress`. The
  cost of not having a bespoke pool-exhaustion branch is at most
  `stagnation_threshold` cheap polling iterations, and the rollup's
  findings list names the exhausted pool while it happens.

Any terminal verdict triggers the abort cascade: live children are
terminated through the standard abort transition and every slot settles
its reservation before finalization completes.

### The orchestrator paces itself to its fleet

Stagnation is judged from what the orchestrator *observes*, and it
observes children through their persisted sessions — so a supervisor
that iterated as fast as it could would spend its whole stagnation
window watching a fleet that had not yet had a chance to run, and
terminate `no_progress` on a swarm that was working fine. Real children
suspend many times per iteration (every live tool dispatch does), so
this is the common case, not an edge case.

The runner therefore gates each orchestrator iteration after the first
on observable fleet progress: a child recording an iteration or a slot
settling wakes the supervisor. Two properties follow:

- **Patience is bounded.** If nothing moves within
  `DEFAULT_FLEET_PROGRESS_TIMEOUT` (30s), the orchestrator iterates
  anyway, so a genuinely wedged fleet still reaches the stagnation
  cascade — it just takes `stagnation_threshold` patience windows to get
  there instead of burning through them in one event-loop turn.
- **Exits never wait.** Budget caps, pauses, terminal verdicts, and the
  bounded-iteration (`swarm iterate`) shape are all checked before the
  gate, so detaching and finalizing stay prompt.

Consequence worth knowing when reading a report: an orchestrator that
supervised a healthy fleet runs *few* iterations, because it only spends
one when the fleet has something new to show it.

## Cost

Swarm deliberately has **no cost cap**, for the same documented reasons
Mission has none: real-time workload cost tracking is structurally
inaccurate, so a cap would fire unpredictably. The swarm-level controls
are the pooled iteration budget, the fleet cap, the concurrency bound,
and the orchestrator's wall clock. For money guardrails, configure AWS
Budgets and Cost Anomaly Detection at the account level — and note that
a swarm multiplies tool-call and (when children opt in) sampling volume
by the fleet size, which is exactly why children default to
no-sampling.

## Developing and testing swarm locally

The swarm test suites live in `tests/test_swarm_*.py` and run without
AWS credentials. One machine-specific precondition: the session-scoped
test fixture pre-builds every Lambda asset, and the inference-streaming
Lambda's packager enforces the exact npm version pinned in its
`packageManager` field against `npm --version` on PATH. Put a matching
npm first on PATH (nvm's node v24 line ships it;
`.github/scripts/use-pinned-npm.sh` is the CI equivalent), then run the
focused loop:

```bash
export PATH="$HOME/.nvm/versions/node/v24.18.0/bin:$PATH"  # exact pinned npm

# Focused swarm loop (--no-cov skips the coverage floor on partial runs):
pytest tests/test_swarm_*.py --no-cov
```

The full suite with the coverage floor runs in CI; prefer the focused
loop locally.

## Surfaces

- **CLI** — `gco swarm run | start | iterate | status | abort | list |
  scaffold-plan`; see [CLI.md](CLI.md#swarm-commands).
- **MCP** — `swarm_start`, `swarm_iterate`, `swarm_status`,
  `swarm_abort`, `swarm_list`, `swarm_plan`; see the
  [feature-flag table](../gco_mcp/README.md#feature-flags).
- **Agent cockpit** — `gco autopilot -e swarm -e mission` launches a
  Claude Code session with the swarm tools registered; the agent drives
  the same MCP surface as any client, and the engine keeps the control
  path.
- **Sessions** — orchestrators and children persist through the standard
  Mission state backends (filesystem or DynamoDB) and are readable with
  `gco mission status` / `mission_status` when the mission flag is on.
