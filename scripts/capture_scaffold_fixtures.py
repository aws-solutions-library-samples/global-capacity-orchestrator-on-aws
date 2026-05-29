#!/usr/bin/env python3
"""Capture raw model output for the Mission scaffolder prompt.

The scaffolder's sampling path is sensitive to the shapes a model
emits — different families default to different Pythonic idioms
(``r.get(...)``, comprehension dict-access, attribute walks). The
fixture-replay test (`tests/test_scaffold_fixture_replay.py`)
asserts that every captured response round-trips cleanly through
``_parse_response`` -> ``_normalize_metric_path`` ->
``_autofix_predicate`` -> ``validate_criteria``. This script populates
the fixture directory by calling each Bedrock model on a fixed set of
canonical directives.

Usage:

    # Capture every default model against every canonical directive
    # (writes one JSON file per model under
    # tests/fixtures/scaffold_responses/).
    python scripts/capture_scaffold_fixtures.py

    # Capture a single model.
    python scripts/capture_scaffold_fixtures.py --model us.amazon.nova-pro-v1:0

    # Use a different region.
    python scripts/capture_scaffold_fixtures.py --region us-west-2

The script needs AWS credentials with ``bedrock:InvokeModel`` access
to the listed models. Failures (missing model access, transient
ClientError) are reported per-model and never abort the run — every
model that does succeed lands in the fixture directory and protects
the validator path on every CI run thereafter.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

# Mirror the path-injection pattern used throughout the Mission tree
# so ``mission.*`` resolves regardless of how the script is launched.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mcp"))

from mission import criteria_scaffold  # noqa: E402
from mission.sampling import (  # noqa: E402
    BedrockSamplingBackend,
    SamplingPrompt,
    SamplingTransportError,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Directive:
    """A canonical directive paired with the allowlist used at scaffolding time.

    The triplet covers the three template branches in
    ``criteria_scaffold._classify_directive``: search-flavoured
    directives (preferred shape: ``tool_call_succeeded``),
    metric-flavoured directives (preferred shape:
    ``metric_threshold``), and event-flavoured directives (preferred
    shape: ``event``). Any model that handles all three shapes is
    likely fine on the long tail.
    """

    slug: str
    text: str
    allowlist: tuple[str, ...]


_DIRECTIVES: tuple[_Directive, ...] = (
    _Directive(
        slug="search_inference_docs",
        text="Find documentation about inference endpoints.",
        allowlist=("find_examples", "find_docs"),
    ),
    _Directive(
        slug="metric_drive_loss",
        text="Drive validation loss below 0.1 on the demo training tool.",
        allowlist=("find_examples",),
    ),
    _Directive(
        slug="event_goal_reached",
        text="Wait for the training job to emit a goal_reached event.",
        allowlist=("find_examples",),
    ),
)


# Default models to capture against. Every entry is a Bedrock
# inference-profile id that the calling principal must have invoke
# access to. Add a model here and the next ``capture`` run picks it
# up; failures (denied access, transient errors) are reported per-
# model and never abort the run.
#
# The list intentionally spans families (Anthropic, Amazon Nova,
# Meta Llama, Mistral, DeepSeek) and sizes (small / mid / large)
# so the replay test stays representative of the long tail of
# Pythonic emission shapes. When a new family or size lands in
# Bedrock, add it here and re-run the capture script.
_DEFAULT_MODELS: tuple[str, ...] = (
    # Anthropic family — current default + adjacent sizes for diversity.
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-opus-4-5-20251101-v1:0",
    "us.anthropic.claude-3-5-haiku-20241022-v1:0",
    "us.anthropic.claude-3-haiku-20240307-v1:0",
    # Amazon Nova family — every visible CRIS profile.
    "us.amazon.nova-premier-v1:0",
    "us.amazon.nova-pro-v1:0",
    "us.amazon.nova-lite-v1:0",
    "us.amazon.nova-micro-v1:0",
    "us.amazon.nova-2-lite-v1:0",
    # Meta Llama family — Llama 4 + recent Llama 3.
    "us.meta.llama4-maverick-17b-instruct-v1:0",
    "us.meta.llama4-scout-17b-instruct-v1:0",
    "us.meta.llama3-3-70b-instruct-v1:0",
    "us.meta.llama3-2-90b-instruct-v1:0",
    "us.meta.llama3-1-70b-instruct-v1:0",
    # Mistral family — the visible text-instruction profile.
    "us.mistral.pixtral-large-2502-v1:0",
    # DeepSeek family.
    "us.deepseek.r1-v1:0",
)


_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "scaffold_responses"


# ---------------------------------------------------------------------------
# Capture helpers
# ---------------------------------------------------------------------------


def _slug_for_model(model_id: str) -> str:
    """Turn a Bedrock model id into a filesystem-safe slug.

    ``us.anthropic.claude-haiku-4-5-20251001-v1:0`` ->
    ``us_anthropic_claude_haiku_4_5_20251001_v1_0``. The replacement
    is intentionally minimal — every non-alphanumeric becomes an
    underscore — so two model ids with different metadata produce
    different slugs.
    """
    out = []
    prev_underscore = False
    for ch in model_id:
        if ch.isalnum():
            out.append(ch)
            prev_underscore = False
        elif not prev_underscore:
            out.append("_")
            prev_underscore = True
    return "".join(out).strip("_")


class _PromptAdapter:
    """Tiny stand-in for ``SamplingPrompt`` used by the scaffolder.

    The Bedrock backend calls ``prompt.assemble()`` to render the
    string it sends to Converse. We bypass the full ``SamplingPrompt``
    constructor (which requires session-shaped data we don't have at
    capture time) by giving the backend an object whose ``assemble``
    returns the rendered scaffold prompt directly.
    """

    def __init__(self, text: str) -> None:
        self._text = text

    def assemble(self) -> str:
        return self._text


async def _capture_one(
    backend: BedrockSamplingBackend,
    directive: _Directive,
) -> dict[str, Any]:
    """Render the scaffold prompt and capture the raw model response.

    Returns a dict carrying both the prompt and the response so a
    fixture can be diffed against the prompt that produced it when
    the scaffolder rev'd.
    """
    prompt_str = criteria_scaffold.build_scaffold_prompt(
        directive.text,
        allowlist=list(directive.allowlist),
    )
    # ``BedrockSamplingBackend.sample`` is typed against
    # :class:`SamplingPrompt`, which requires session-shaped data we
    # don't have at capture time. The backend only ever calls
    # ``prompt.assemble()`` on its argument, so a duck-typed
    # ``_PromptAdapter`` is sufficient at runtime; cast to satisfy
    # mypy without weakening the production signature.
    raw = await backend.sample(cast(SamplingPrompt, _PromptAdapter(prompt_str)))
    return {
        "prompt_directive": directive.text,
        "prompt_allowlist": list(directive.allowlist),
        "raw_response": raw,
    }


async def _capture_model(
    model_id: str,
    region: str,
    output_dir: Path,
) -> bool:
    """Capture all canonical directives for one model. Returns False on failure.

    Every directive is written into the same per-model JSON file under
    its ``slug`` key. A failure on one directive aborts the whole
    model's capture so the fixture file is either written wholesale
    or not at all — an incomplete fixture would silently weaken the
    replay test.
    """
    backend = BedrockSamplingBackend(model_id=model_id, region=region)
    captures: dict[str, dict[str, Any]] = {}
    for directive in _DIRECTIVES:
        try:
            captures[directive.slug] = await _capture_one(backend, directive)
        except SamplingTransportError as exc:
            print(
                f"[{model_id}] capture failed for {directive.slug!r}: {exc.code}: {exc}",
                file=sys.stderr,
            )
            return False
        except Exception as exc:  # noqa: BLE001 - surface and keep going
            print(
                f"[{model_id}] unexpected error for {directive.slug!r}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return False

    output_path = output_dir / f"{_slug_for_model(model_id)}.json"
    payload = {
        "model_id": model_id,
        "region": region,
        "captured_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "captures": captures,
    }
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"[{model_id}] wrote {output_path.relative_to(_REPO_ROOT)}")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 1)[0] if __doc__ else "",
    )
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        default=None,
        help=(
            "Bedrock model id to capture against; repeatable. "
            "Defaults to a curated cross-family set."
        ),
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("GCO_MISSION_BEDROCK_REGION", "us-east-1"),
        help="Bedrock region (default: us-east-1).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_FIXTURE_DIR,
        help=("Directory to write fixtures into. Defaults to tests/fixtures/scaffold_responses/."),
    )
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    models = tuple(args.models) if args.models else _DEFAULT_MODELS
    args.output_dir.mkdir(parents=True, exist_ok=True)

    successes = 0
    failures = 0
    for model_id in models:
        ok = await _capture_model(model_id, args.region, args.output_dir)
        if ok:
            successes += 1
        else:
            failures += 1

    print(f"\nCaptured {successes} model(s); {failures} failed.")
    # A non-zero exit when *every* model failed is useful for cron
    # wrappers; a partial-failure run still exits 0 so a single denied
    # model doesn't stop the rest from being committed.
    return 0 if successes > 0 else 1


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
