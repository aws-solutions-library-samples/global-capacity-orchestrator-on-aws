# Mission

Mission is GCO's goal-directed iteration loop. The operator declares a directive (natural-language goal), success criteria (machine-checkable), a tool allowlist (what the loop may invoke), and a budget. Mission runs five-phase iterations until a verdict is reached.

## Overview

A Mission session is a structured loop the operator drives through the CLI or the MCP tools. Each iteration runs five phases in order:

1. **Propose** — pick the next strategy (deterministic by default; advisory LLM sampling shapes it when enabled).
2. **Execute** — dispatch the strategy's tool calls, or run a script in the sandbox.
3. **Observe** — normalise tool results into a single Observation dict.
4. **Evaluate** — score the Observation against every Criterion.
5. **Decide** — emit a Verdict (`continue`, `adjust`, `complete`, or `terminate`) plus a reason.

The control path is fully deterministic. Sampling, when enabled, only shapes the next strategy and the closing notes in the Final_Report — it never changes the verdict, the budget arithmetic, or the criterion evaluation. Sessions persist as JSON under `~/.gco/missions/` (filesystem backend) or in the `<project>-missions` DynamoDB table when `GCO_MISSION_STATE_BACKEND=dynamodb`.

The whole feature is gated. Set `GCO_ENABLE_MISSION=true` (or the umbrella `GCO_ENABLE_ALL_TOOLS=true`) before any Mission CLI subcommand or MCP tool resolves. See [Feature Flags](../mcp/README.md#feature-flags) for the table.

## Quickstart

End-to-end via the CLI, with no AWS credentials required:

```bash
export GCO_ENABLE_MISSION=true

# Write a criteria file.
cat > criteria.json <<'EOF'
[
  {"criterion_id": "loss",
   "kind": "metric_threshold",
   "required": true,
   "metric": "val_loss", "op": "<", "target": 0.1}
]
EOF

# Start a session.
gco mission start \
  --directive "Drive validation loss below 0.1." \
  --criteria-file criteria.json \
  --max-iterations 10 --max-wall-clock 3600 \
  --tool-allowlist find_examples
# → {"session_id": "mission-abc123...", "status": "pending", ...}

# Iterate one step.
gco mission iterate mission-abc123 --max-iterations-this-call 1

# Check progress.
gco mission status mission-abc123 --output table

# End it manually.
gco mission complete mission-abc123
```

`--run` collapses start + iterate into one synchronous call; verdicts stream to stderr as JSON lines and the Final_Report lands on stdout when the session terminates.

## The No-AWS Smoke Test

Mission's safe-tier tools (`find_examples`, `find_docs`) hit only in-memory fixtures, so the loop runs end-to-end without AWS credentials.

```bash
export GCO_ENABLE_MISSION=true

cat > criteria.json <<'EOF'
[
  {"criterion_id": "results",
   "kind": "predicate",
   "required": true,
   "expression": "len(obs.get('tool_results', [])) > 0"}
]
EOF

gco mission start --run \
  --directive "Find documentation about inference endpoints." \
  --criteria-file criteria.json \
  --max-iterations 1 --max-wall-clock 30 \
  --tool-allowlist find_examples \
  --tool-allowlist find_docs
```

The session terminates on `max_iterations` if the predicate is not satisfied within the iteration budget. The Final_Report streams to stdout as JSON. The same flow runs in CI as `tests/test_mission_no_aws.py` — `boto3.Session` is patched to raise on any client construction so the test fails loudly if a Mission code path tries to reach AWS.

## Sampling

Sampling is **advisory only**. The deterministic verdict cascade decides every Verdict; the sampler shapes only the next Strategy and the closing notes on the Final_Report.

Two backends:

| Backend | Used By | Transport |
|---------|---------|-----------|
| MCP | The `mission_*` MCP tools when the host advertises sampling capability | `ctx.sample(...)` (FastMCP `Context`) |
| Bedrock | The `gco mission` CLI | `bedrock-runtime:Converse` |

Resolution precedence at session start:

1. Explicit `--no-sampling` / `--use-sampling` flag (or the `use_sampling` field on the MCP `mission_start` payload).
2. MCP capability detection — when running inside an MCP host that advertises `sampling`, the MCP backend is selected.
3. CLI Bedrock credential probe — when `boto3` resolves credentials, the Bedrock backend is selected.
4. Otherwise sampling is off and the loop runs in deterministic-fallback mode.

Defaults:

- Model — `us.anthropic.claude-sonnet-4-5-20250929-v1:0`. Override via `GCO_MISSION_BEDROCK_MODEL_ID` or `--bedrock-model-id`.
- Region — `us-east-1`. Override via `GCO_MISSION_BEDROCK_REGION`.

Every sampling attempt emits one structured audit event (`sampling_purpose`, `sampling_status`, `sampling_backend`, `sampling_model_id`, `model_output_bytes`, `validation_error`). Sampling rejections (transport errors, malformed JSON, schema mismatch, allowlist or budget violations, AST rejections on proposed scripts) cause an automatic deterministic fallback — the iteration still runs.

## Scripted Strategies

Scripted strategies are opt-in via `--allow-scripted-strategies` on the CLI (or `allow_scripted_strategies=true` on the MCP `mission_start` payload). With the flag set, a Strategy may carry a Python `script` instead of a list of `tool_calls`. Scripts run inside the Mission sandbox with two layers of defence:

**Parse-time AST validator.** The script must be valid Python in `exec` mode. The validator rejects: `import` and `from ... import`, class definitions, `__import__` / `eval` / `exec` / `compile`, dunder names (anything starting with `__`), `Lambda` containing forbidden patterns, async constructs other than `await <tool>(...)` for allowlisted tools, walrus assignments to protected names, decorators (the decorator allowlist is currently empty), `with`, `match`, `assert`, `del`, `global`, and `nonlocal`. Subscript-then-call chains on disallowed names are rejected too. Violations raise `ScriptRejected` before the script is ever evaluated.

**Runtime resource caps.** The script runs under the same sandbox provider used by the wider tooling. Two env vars cap it:

| Variable | Default | Effect |
|----------|---------|--------|
| `GCO_MCP_CODE_MODE_MAX_DURATION_SECS` | `30` | Wall-clock cap on a single script's execution. |
| `GCO_MCP_CODE_MODE_MAX_MEMORY` | `268435456` (256 MiB) | Resident memory cap on the sandbox process. |

Inside the script, allowlisted tools are exposed as awaitable callables. A `mission` namespace exposes `mission.observe(key, value)` and `mission.event(name)` for incremental Observation building. Every in-script tool call goes through the standard MCP audit decorator and emits a follow-up Mission audit event with `via_script=true`.

A script that exceeds either cap is terminated; partial observations recorded via `mission.observe(...)` are preserved, the iteration's verdict resolves to `terminate`, and the reason carries the budget cap that fired.

## Budget Controls

Three caps gate every session. Each enforces hard termination when exceeded:

| Cap | Required | Notes |
|-----|----------|-------|
| `max_iterations` | always | Hard cap on the iteration count. |
| `max_wall_clock_seconds` | always | Wall-clock cap from `started_at`. |
| `max_cost_usd` | conditional | Required when the allowlist contains a tool tagged `cost-incurring`, `data-upload`, `image`, or `infrastructure`. The validator checks the registered tool catalog to decide. |

Per-iteration cost is tallied from per-tool cost estimators registered with the engine. The Verdict cascade evaluates budget caps first, before any criterion or stagnation check, so a session always exits cleanly the moment a cap is breached.

## Checkpoint Cadences

A Cadence controls when the verdict cascade actually runs. Off-cadence iterations short-circuit with `("continue", "cadence_skip")` — the strategy executes, the Observation is recorded, but no Criterion is consulted and no progress counter advances.

| Kind | Required Field | Behaviour |
|------|----------------|-----------|
| `every_iteration` (default) | — | Evaluate every iteration. |
| `every_n_iterations` | `n` | Evaluate every `n`th iteration starting from iteration 0. |
| `every_t_seconds` | `t` | Evaluate when ≥ `t` seconds have elapsed since `last_checkpoint_at`. |
| `on_event` | `event_name` | Evaluate when the prior Observation contains the matching event. |

Stagnation tracking only advances on iterations that actually evaluate. A session with `cadence=every_n_iterations(n=3)` and `stagnation_threshold=4` checks for stagnation every third iteration, so the threshold is reached at iteration 12 in the worst case rather than iteration 4.

## CLI Reference

All `gco mission` subcommands require `GCO_ENABLE_MISSION=true`. Without the flag, the group exits with code 2 and prints the hint to stderr.

| Subcommand | Purpose |
|------------|---------|
| `gco mission start` | Validate inputs, resolve sampling, persist a new session. With `--run`, iterate to completion synchronously. |
| `gco mission status <id>` | Print the full session JSON (or a table summary with `--output table`). |
| `gco mission iterate <id> [--max-iterations-this-call N]` | Drive one or more iterations of an existing session. |
| `gco mission checkpoint <id>` | Re-run the verdict cascade on the latest iteration without producing a new one. |
| `gco mission complete <id>` | Force the session into `completed` and write a Final_Report. |
| `gco mission abort <id> [--pause]` | Terminate the session, or pause it with `--pause`. |
| `gco mission resume <id>` | Transition a paused session back to `running`. |
| `gco mission history <id> [--format full\|summary]` | Print the iteration history. |
| `gco mission list [--status STATUS]` | List known sessions, optionally filtered by status. |

`gco mission start --help` prints the full option list — directive text, criteria file path, the three budget caps, the tool allowlist, cadence parameters, stagnation threshold, sampling toggles, and the scripted-strategy opt-in.

## MCP Tool Reference

The MCP surface mirrors the CLI. All nine tools are gated by `GCO_ENABLE_MISSION` (or the umbrella) and emit structured audit events on every invocation.

| Tool | Purpose |
|------|---------|
| `mission_start` | Validate inputs, resolve sampling, persist a new session. |
| `mission_status` | Return the full session JSON. |
| `mission_iterate` | Drive one or more iterations. Long-running; supports MCP progress reporting. |
| `mission_checkpoint` | Re-run the verdict cascade on the latest iteration. |
| `mission_complete` | Force the session into `completed`. |
| `mission_abort` | Terminate or pause the session. |
| `mission_resume` | Transition `paused → running`. |
| `mission_history` | Return iteration history (`full` or `summary` format). |
| `mission_list` | List known sessions. |

Two resource templates expose session state:

- `mission://sessions/{session_id}` — the live session JSON. Available as soon as the session exists.
- `mission://sessions/{session_id}/report` — the Final_Report JSON. Available once the session reaches a terminal state (`completed`, `terminated`, `failed`).

Tool-only clients reach both resources through the synthetic `read_resource` tool produced by the Resources As Tools transform.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Mission tools are gated. Set GCO_ENABLE_MISSION=true ...` | The feature flag is unset. | `export GCO_ENABLE_MISSION=true` before the command. |
| `validation_error` with `field=criteria, reason=criterion_id_missing_or_invalid` | A Criterion is missing `criterion_id` or has a non-string value. | Fix the criteria JSON; every entry needs a unique non-empty string `criterion_id`. |
| `validation_error` with `field=budget, reason=max_cost_usd_required` | The allowlist includes a cost-incurring tool but no `--max-cost`. | Pass `--max-cost <usd>`, or remove the cost-incurring tool from the allowlist. |
| `bedrock_AccessDeniedException` (sampling event `validation_error`) | IAM does not allow `bedrock:InvokeModel` for the resolved model. | Grant the permission on the calling principal, or pass `--no-sampling` to fall back to deterministic mode. |
| `bedrock_no_credentials` (sampling event `validation_error`) | The CLI could not resolve AWS credentials. | Configure credentials, or pass `--no-sampling`. The session still runs deterministically. |
| `session_not_found` | The session id does not exist on the configured backend. | Confirm with `gco mission list`; the id may have a typo or live on a different backend. |
| `session_terminal` | The session already ended (`completed`, `terminated`, or `failed`). | Read the Final_Report via the report resource, or start a new session. |
| `session_paused` | The session was paused via `gco mission abort --pause`. | Run `gco mission resume <id>` first. |
| `Tool '<name>' is not in the allowlist` (script audit event) | A script called a tool that is not in the session's allowlist. | Add the tool to `--tool-allowlist`, or remove the call from the script. |

For a list of every gated tool and the flag that controls it, see [Feature Flags](../mcp/README.md#feature-flags).
