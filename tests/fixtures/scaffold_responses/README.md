# Scaffolder fixture replay

Captured raw model output for the Mission scaffolder prompt. Each
`*.json` file holds one model's response to a small set of canonical
directives. The replay test
(`tests/test_scaffold_fixture_replay.py`) drives every captured
response through the full scaffolder pipeline — JSON extraction,
metric-path normalisation, predicate autofix, and the structural
validator — so a regression that breaks one model is caught against
every model on the next CI run.

## Table of Contents

- [Why this exists](#why-this-exists)
- [File layout](#file-layout)
- [Adding a new model](#adding-a-new-model)
- [When the replay test fails](#when-the-replay-test-fails)
- [What the capture does NOT do](#what-the-capture-does-not-do)

## Why this exists

The scaffolder's sampling path is sensitive to the shapes a model
emits. Different families default to different Pythonic idioms
(`r.get(...)` vs comprehension dict-access, `obs.metrics.val_loss`
vs `obs['metrics']['val_loss']`, `'foo' in str(x).lower()` vs
literal subscript matching). One-off live tests find one shape and
leave the next-shaped emission to surprise the next operator. The
fixture replay turns that into a property: every shape we have ever
seen continues to round-trip through the validator.

## File layout

```text
tests/fixtures/scaffold_responses/
├── README.md                                   # this file
├── global_amazon_nova_2_lite_v1_0.json           # canonical default
├── us_amazon_nova_premier_v1_0.json              # historical capture
├── us_anthropic_claude_sonnet_4_5_*.json
├── us_anthropic_claude_haiku_4_5_*.json
├── us_amazon_nova_pro_v1_0.json
├── us_amazon_nova_lite_v1_0.json
├── us_meta_llama3_3_70b_instruct_v1_0.json
└── ...
```

Each file follows the schema:

```json
{
  "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
  "region": "us-east-1",
  "captured_at": "2026-05-29T12:36:00+00:00",
  "captures": {
    "search_inference_docs": {
      "prompt_directive": "Find documentation about inference endpoints.",
      "prompt_allowlist": ["find_examples", "find_docs"],
      "raw_response": "[\n  {...}\n]"
    },
    "metric_drive_loss": { ... },
    "event_goal_reached": { ... }
  }
}
```

The three slugs (`search_inference_docs`, `metric_drive_loss`,
`event_goal_reached`) cover the three template branches in
`criteria_scaffold._classify_directive` — search-flavoured (preferred
shape: `tool_call_succeeded`), metric-flavoured (`metric_threshold`),
and event-flavoured (`event`). A model that handles all three is
likely fine on the long tail.

## Adding a new model

When the scaffolder breaks against a new Bedrock model — or when you
just want to add a model to the safety net — run the capture script
once and commit the resulting JSON:

```bash
# Capture the canonical global Nova 2 Lite default. This makes exactly
# three sequential paid Converse calls, one per canonical directive.
python3 scripts/capture_scaffold_fixtures.py \
  --model global.amazon.nova-2-lite-v1:0 \
  --region us-east-1

# Capture against a different single model.
python3 scripts/capture_scaffold_fixtures.py \
  --model us.amazon.nova-micro-v1:0

# Capture against every model in the default list (re-captures
# existing entries and refreshes them).
python3 scripts/capture_scaffold_fixtures.py
```

The script needs AWS credentials with `bedrock:InvokeModel` access
to the listed models. When the requested id is the configured default, the
script also applies `cdk.json` `context.bedrock.thinking`; the stock Nova 2 Lite
`high` effort setting can materially increase billed output tokens and latency.
AWS requires `maxTokens`, `temperature`, and `topP` to remain unset in that
mode. Failures (denied access, transient errors) are reported per-model and
never abort the run — every model that does succeed lands in the fixture
directory and protects the validator path on every CI run thereafter.

After capturing, run the replay test and commit:

```bash
python3 -m pytest tests/test_scaffold_fixture_replay.py -v
git add tests/fixtures/scaffold_responses/<new_fixture>.json
```

## When the replay test fails

A red `test_captured_response_round_trips_through_scaffolder` means
one of three things:

1. **The scaffolder regressed.** A change to the prompt builder, the
   metric-path normaliser, the autofix, or the validator no longer
   accepts a shape some model emits. Fix forward by either
   broadening the validator (when the shape is genuinely safe — see
   `gco_mcp/mission/predicate.py::_ALLOWED_CALLABLES` and
   `_ALLOWED_METHOD_CALLS` for the precedent), adding an autofix in
   `criteria_scaffold._autofix_predicate`, or tightening the prompt
   so future captures emit a shape the validator already handles.

2. **The captured model added a new idiom.** A re-capture surfaces
   a shape the scaffolder hasn't seen. Same options as above —
   broaden, autofix, or steer.

3. **The fixture is stale.** Models occasionally rev their default
   output style. Re-run the capture script for the affected model
   and commit the refreshed JSON.

The failure message includes the model id and directive slug so the
exact (model, directive) pair is identifiable from a single test
output line.

## What the capture does NOT do

- It does **not** drive a full Mission run — only the scaffolder
  prompt is exercised, because that is where model-shape
  sensitivity lives. The verdict cascade, evaluator, and Final_Report
  paths are deterministic and have their own tests.
- It does **not** include any operator-supplied PII or workload
  details — the canonical directives are intentionally generic
  ("Find documentation about inference endpoints.").
- It does **not** run on every CI build by default — only the
  replay test does. The capture script is offline, manual, and
  needs credentials. Schedule it as a quarterly canary if you want
  fresh data; otherwise the existing fixtures continue to protect
  the validator surface.
