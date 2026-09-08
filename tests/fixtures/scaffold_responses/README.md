# Scaffolder fixture replay

Captured raw model output for the Mission scaffolder prompt. Each
`*.json` file holds one model's response to a small set of canonical
directives. The checked-in catalog currently covers 64 models from 15
providers: 192 real, paid Converse responses. The replay test
(`tests/test_scaffold_fixture_replay.py`) drives every captured response
through the full scaffolder pipeline — JSON extraction, model-output
normalisation, predicate autofix, and strict structural validation — so
a regression that breaks one model is caught against every model on the
next CI run.

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
tests/fixtures/
├── scaffold_response_capture_provenance.json # prompt hashes for capture cohorts
└── scaffold_responses/
    ├── README.md                              # this file
    ├── global_anthropic_claude_opus_5.json    # canonical default
    ├── global_amazon_nova_2_lite_v1_0.json    # historical capture
    ├── us_amazon_nova_premier_v1_0.json       # historical capture
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
      "prompt_sha256": "<sha256-of-the-exact-rendered-prompt>",
      "raw_response": "[\n  {...}\n]"
    },
    "metric_drive_loss": { ... },
    "event_goal_reached": { ... }
  }
}
```

New captures store the SHA-256 digest of the exact rendered prompt beside each
raw response. The provenance sidecar binds the initial live-catalog cohort to
its source Git SHA and three prompt digests without rewriting those untouched
responses; a closed list identifies older historical fixtures that predate
prompt hashing. The strict loader requires every non-legacy fixture to have one
of those two provenance forms and binds each canonical slug to its exact
directive and allowlist.

The three slugs (`search_inference_docs`, `metric_drive_loss`,
`event_goal_reached`) cover the three template branches in
`criteria_scaffold._classify_directive` — search-flavoured (preferred
shape: `tool_call_succeeded`), metric-flavoured (`metric_threshold`),
and event-flavoured (`event`). A model that handles all three is
likely fine on the long tail.

## Adding a new model

When the scaffolder breaks against a new
[Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html)
model — or when you just want to add a model to the safety net — run
the capture script once and commit the resulting JSON:

```bash
# Capture the canonical global Claude Opus 5 default. This makes exactly
# three sequential paid Converse calls, one per canonical directive.
python3 scripts/capture_scaffold_fixtures.py \
  --model global.anthropic.claude-opus-5 \
  --region us-east-1

# Capture against a different single model.
python3 scripts/capture_scaffold_fixtures.py \
  --model us.amazon.nova-micro-v1:0

# Capture against every model in the maintained curated list (re-captures
# existing entries and refreshes them).
python3 scripts/capture_scaffold_fixtures.py

# Discover every uncaptured text-generation model line currently visible in
# the account's Bedrock catalog, print the paid-call budget, and capture all
# candidates that accept the canonical Converse prompts.
python3 scripts/capture_scaffold_fixtures.py --discover-all-models

# Override the conservative default of four concurrent model workers.
python3 scripts/capture_scaffold_fixtures.py \
  --discover-all-models --workers 8

# Inspect that same live candidate list without making paid model calls.
python3 scripts/capture_scaffold_fixtures.py \
  --discover-all-models --list-candidates
```

Discovery requires the Bedrock catalog APIs in addition to
`bedrock:InvokeModel`. It keeps one preferred inference profile per underlying
model line (global, then US, then another geography), falls back to a direct
on-demand model only when no profile represents that line, and skips any line
already represented by a fixture. Embedding, reranking, image-editing,
speech-only, video-only, and safety-classifier entries are excluded because
this fixture schema requires a normal text message and generated text response.
A catalog candidate is not assumed compatible: denied, unsupported, malformed,
or structurally invalid responses fail per model and write no fixture.

The script prints the candidate count and maximum call count before broad
capture. Each candidate makes at most three sequential paid Converse calls;
models run concurrently with four workers by default. Use `--workers` to tune
throughput against account throttling limits. Each worker owns a model/client,
keeps that model's directives sequential, and atomically replaces a distinct
fixture only after all three raw responses pass the production parse,
normalization, structural validation, and directive/allowlist context checks.
Publication uses a flushed same-directory temporary file plus `os.replace`, so
parallel capture cannot publish partial or invalid files or truncate a prior
valid fixture. Broad discovery bounds each request to five minutes so one
model does not indefinitely block the rest; override that budget with
`--read-timeout-seconds`. Review provider/model terms and the generated diff
before committing; fixture capture is deliberately manual and never runs in CI.
Models that are no longer invocable should be removed from the maintained
curated tuple; already captured historical fixtures may remain as replay
evidence while they continue to satisfy the fixture contract.

The script needs AWS credentials with `bedrock:InvokeModel` access to the
listed models. Anthropic models — including the stock default — also require
the one-time
[Anthropic first-time-use form](../../../docs/CUSTOMIZATION.md#accepting-the-anthropic-first-time-use-form);
without it capture fails with `FTUFormNotFilled`.

Claude Fable 5 and Fable 5.1 additionally require an account- or project-level
[`aws_review` data-retention mode](https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html).
That policy permits AWS to retain prompts and completions for up to 30 days and
potentially review them within the AWS boundary. The capture script never
changes retention policy; obtain explicit data-governance approval before an
operator enables it.

When the requested ID is the configured default, the script also applies
`cdk.json` `context.bedrock.generation_reasoning`; the stock `high` effort can
materially increase billed output tokens and latency. The shared request
builder removes generic sampling controls from model lines that reject or
constrain them, including newer Claude, OpenAI, and xAI models. Failures are
reported per model and never abort unrelated workers — every model that does
succeed lands in the fixture directory and protects the validator path on every
CI run thereafter.

After capturing, run the replay test and commit:

```bash
python3 -m pytest tests/test_scaffold_fixture_replay.py -v
git add tests/fixtures/scaffold_responses/<new_fixture>.json
```

## When the replay test fails

A red `test_captured_response_round_trips_through_scaffolder` means
one of three things:

1. **The scaffolder regressed.** A change to the prompt builder, the
   model-output normalizers, predicate autofix, or validator no longer
   accepts a previously supported shape. Fix forward only when the
   rewrite is mechanical and meaning-preserving; never weaken the
   validator merely to make a fixture green.

2. **The model emitted a new, safely normalizable idiom.** Extend a
   narrow normalizer when the transformation is unambiguous, then keep
   the untouched raw response as regression evidence. Ambiguous output
   should fail capture and remain outside the positive corpus.

3. **The fixture is stale.** Models occasionally revise their output
   style. Re-run the capture script for the affected model; the
   publication gate preserves the prior file unless all three new raw
   responses pass validation.

The failure message includes the model ID and directive slug so the exact
(model, directive) pair is identifiable from a single test output line.

## What the capture does NOT do

- It does **not** drive a full Mission run — only the scaffolder prompt is
  exercised, because that is where model-shape sensitivity lives. The verdict
  cascade, evaluator, and Final_Report paths are deterministic and have their
  own tests.
- It does **not** include operator-supplied PII or workload details — the
  canonical directives are intentionally generic ("Find documentation about
  inference endpoints.").
- It does **not** enable model access, submit Anthropic's FTU form, or change
  account/project data-retention policy.
- It does **not** run on every CI build by default — only replay does. Capture
  is offline, manual, credentialed, and paid. Schedule it as a quarterly canary
  if you want fresh data; otherwise the existing fixtures continue to protect
  the validator surface.
