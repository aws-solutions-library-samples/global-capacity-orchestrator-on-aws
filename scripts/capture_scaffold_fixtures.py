#!/usr/bin/env python3
"""Capture raw model output for the Mission scaffolder prompt.

The scaffolder's sampling path is sensitive to the shapes a model
emits — different families default to different Pythonic idioms
(``r.get(...)``, comprehension dict-access, attribute walks). The
fixture-replay test (`tests/test_scaffold_fixture_replay.py`)
asserts that every captured response round-trips cleanly through
``_parse_response`` -> ``_normalize_sampled_criteria`` ->
``validate_criteria``. This script populates the fixture directory by calling
the fixture directory by calling each Bedrock model on a fixed set of
canonical directives.

Usage:

    # Capture every default model against every canonical directive
    # (writes one JSON file per model under
    # tests/fixtures/scaffold_responses/).
    python3 scripts/capture_scaffold_fixtures.py

    # Capture a single model.
    python3 scripts/capture_scaffold_fixtures.py --model MODEL_ID

    # Capture every uncaptured text-generation model line visible in the live
    # Bedrock catalog. The command prints the candidate list and paid-call
    # budget before making requests; one preferred inference profile represents
    # each underlying model line. Discovery captures four models concurrently
    # by default; use --workers to tune account throughput.
    python3 scripts/capture_scaffold_fixtures.py --discover-all-models

    # Review the same live-catalog selection without invoking any model.
    python3 scripts/capture_scaffold_fixtures.py \
        --discover-all-models --list-candidates

    # Use a different region.
    python3 scripts/capture_scaffold_fixtures.py --region us-west-2

The script needs AWS credentials with ``bedrock:InvokeModel`` access
to the listed models. Anthropic models — including the stock default —
additionally require the one-time Anthropic first-time-use case form on
the account; without it Bedrock answers ``FTUFormNotFilled`` and the
capture for that model fails with that code (see
``docs/CUSTOMIZATION.md``, Bedrock Model Selection).

The configured default also consumes ``cdk.json``
``context.bedrock.generation_reasoning``. At the stock ``high`` effort each capture
can use substantially more billed output tokens and take longer;
Claude models from Opus 4.7 onward additionally reject ``temperature``,
``topP``, and ``topK``, which GCO omits for the canonical default.
Failures (missing model access, transient ClientError) are reported
per-model and never abort the run — every model that does succeed lands
in the fixture directory and protects the validator path on every CI run
thereafter.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

# Mirror the path-injection pattern used throughout the Mission tree
# so ``mission.*`` resolves regardless of how the script is launched.
_REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(_REPO_ROOT), str(_REPO_ROOT / "gco_mcp")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import mission.criteria_scaffold as criteria_scaffold  # noqa: E402
from mission.sampling import (  # noqa: E402
    BedrockSamplingBackend,
    SamplingPrompt,
    SamplingTransportError,
)
from mission.validation import MissionValidationError, validate_criteria  # noqa: E402

from gco.bedrock import (  # noqa: E402
    BedrockFTUFormNotAcceptedError,
    get_default_mission_model_id,
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
_CURATED_MODELS: tuple[str, ...] = (
    # Anthropic family — also the family the configured default belongs to.
    # NOTE: Anthropic models require a one-time First-Time-Use (FTU) form per
    # account/org before the first invoke; capture fails with
    # ``FTUFormNotFilled`` until it is submitted.
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-opus-4-5-20251101-v1:0",
    "us.anthropic.claude-3-haiku-20240307-v1:0",
    # Amazon Nova family — first-party, no FTU form. The configured GCO
    # default is prepended lazily by ``_default_models`` and deduplicated from
    # this curated set before any paid calls are made, whichever family it
    # belongs to.
    "us.amazon.nova-pro-v1:0",
    "us.amazon.nova-lite-v1:0",
    "us.amazon.nova-micro-v1:0",
    "us.amazon.nova-2-lite-v1:0",
    # Meta Llama family — Llama 4 + recent Llama 3.
    "us.meta.llama4-maverick-17b-instruct-v1:0",
    "us.meta.llama4-scout-17b-instruct-v1:0",
    "us.meta.llama3-3-70b-instruct-v1:0",
    "us.meta.llama3-1-70b-instruct-v1:0",
    # Mistral family — the visible text-instruction profile.
    "us.mistral.pixtral-large-2502-v1:0",
    # DeepSeek family.
    "us.deepseek.r1-v1:0",
)


def _default_models() -> tuple[str, ...]:
    """Return the configured Mission default plus the curated set, in stable order.

    Scaffold fixtures replay Mission sampling responses, so the canonical
    member of the set follows ``context.bedrock.mission_default_model_id``.
    """
    return tuple(dict.fromkeys((get_default_mission_model_id(), *_CURATED_MODELS)))


_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "scaffold_responses"

# Discovery is deliberately narrower than ``list-foundation-models``. Scaffold
# captures require a normal text message and a generated text answer through
# Converse; embeddings, rerankers, media transformers, speech-only models, and
# safety classifiers do not satisfy that contract even when their catalog entry
# mentions TEXT somewhere.
_GEOGRAPHY_PROFILE_PREFIX_RE = re.compile(r"^(?:global|us|us-gov|eu|apac|jp|au|ca|sa|il|mx)\.")
_NON_GENERATION_MODEL_FRAGMENTS = (
    "embed",
    "rerank",
    "stable-",
    "stability.",
    "nova-2-sonic",
    "twelvelabs.",
    "safeguard",
)


def _base_model_id(model_id: str) -> str:
    """Collapse a geography-scoped profile id to its foundation-model line."""
    return _GEOGRAPHY_PROFILE_PREFIX_RE.sub("", model_id)


def _is_text_generation_candidate(summary: dict[str, Any]) -> bool:
    """Return whether a live catalog entry can plausibly serve this fixture."""
    model_id = str(summary.get("modelId", ""))
    return bool(
        summary.get("modelLifecycle", {}).get("status") == "ACTIVE"
        and "TEXT" in summary.get("inputModalities", [])
        and "TEXT" in summary.get("outputModalities", [])
        and not any(fragment in model_id for fragment in _NON_GENERATION_MODEL_FRAGMENTS)
    )


def _fixture_model_ids(output_dir: Path) -> frozenset[str]:
    """Read exact model ids already represented under *output_dir*."""
    model_ids: set[str] = set()
    for path in sorted(output_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        model_id = payload.get("model_id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError(f"{path}: model_id must be a non-empty string")
        model_ids.add(model_id.strip())
    return frozenset(model_ids)


def _profile_preference(profile_id: str) -> tuple[int, str]:
    """Prefer a global profile, then US, then another geography."""
    if profile_id.startswith("global."):
        rank = 0
    elif profile_id.startswith("us."):
        rank = 1
    else:
        rank = 2
    return rank, profile_id


def _discover_all_models(
    region: str,
    output_dir: Path,
    *,
    bedrock_client: Any | None = None,
) -> tuple[str, ...]:
    """Discover every uncaptured text-generation candidate visible in Bedrock.

    One inference profile represents each underlying model line (global is
    preferred where available). Direct on-demand model ids are added only when
    no profile represents that line. The configured/curated registry is folded
    in first so legacy-but-intentional captures remain required candidates.
    Existing fixtures suppress the entire underlying line, avoiding paid
    recapture under a different geography prefix.
    """
    if bedrock_client is None:
        import boto3

        bedrock_client = boto3.client("bedrock", region_name=region)

    model_page = bedrock_client.list_foundation_models(byOutputModality="TEXT")
    active_models = {
        summary["modelId"]: summary
        for summary in model_page.get("modelSummaries", [])
        if _is_text_generation_candidate(summary)
    }

    profiles: list[dict[str, Any]] = []
    next_token: str | None = None
    while True:
        request: dict[str, Any] = {
            "typeEquals": "SYSTEM_DEFINED",
            "maxResults": 1000,
        }
        if next_token:
            request["nextToken"] = next_token
        page = bedrock_client.list_inference_profiles(**request)
        profiles.extend(page.get("inferenceProfileSummaries", []))
        next_token = page.get("nextToken")
        if not next_token:
            break

    existing_bases = {_base_model_id(model_id) for model_id in _fixture_model_ids(output_dir)}
    selected_by_base: dict[str, str] = {}

    # Curated entries win exact-id selection, including intentional older
    # profiles that no longer appear as ACTIVE foundation models.
    for model_id in _default_models():
        base = _base_model_id(model_id)
        if base not in existing_bases:
            selected_by_base.setdefault(base, model_id)
    curated_bases = set(selected_by_base)

    for profile in profiles:
        if profile.get("status") != "ACTIVE":
            continue
        profile_id = profile.get("inferenceProfileId")
        if not isinstance(profile_id, str) or any(
            fragment in profile_id for fragment in _NON_GENERATION_MODEL_FRAGMENTS
        ):
            continue
        base_ids = {
            str(model.get("modelArn", "")).rsplit("/", 1)[-1] for model in profile.get("models", [])
        }
        for base in base_ids:
            if base not in active_models or base in existing_bases or base in curated_bases:
                continue
            current = selected_by_base.get(base)
            if current is None or _profile_preference(profile_id) < _profile_preference(current):
                selected_by_base[base] = profile_id

    represented_bases = existing_bases | set(selected_by_base)
    for model_id, summary in active_models.items():
        if model_id in represented_bases:
            continue
        if "ON_DEMAND" not in summary.get("inferenceTypesSupported", []):
            continue
        selected_by_base[model_id] = model_id

    # Put Anthropic last: an account-wide FTU failure should not prevent other
    # providers from being captured in the same broad run.
    return tuple(
        sorted(
            selected_by_base.values(),
            key=lambda model_id: ("anthropic." in model_id, model_id),
        )
    )


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


def _backend_for_capture(
    model_id: str,
    region: str,
    *,
    read_timeout_seconds: int | None = None,
) -> BedrockSamplingBackend:
    """Preserve canonical provenance and optionally bound one capture request."""
    if model_id == get_default_mission_model_id():
        backend = BedrockSamplingBackend.from_canonical_default(region=region)
    else:
        backend = BedrockSamplingBackend(model_id=model_id, region=region)

    if read_timeout_seconds is not None:
        import boto3
        from botocore.config import Config

        backend._client = boto3.Session().client(
            "bedrock-runtime",
            region_name=region,
            config=Config(
                connect_timeout=min(10, read_timeout_seconds),
                read_timeout=read_timeout_seconds,
            ),
        )
    return backend


async def _capture_one(
    backend: BedrockSamplingBackend,
    directive: _Directive,
) -> dict[str, Any]:
    """Render the scaffold prompt and capture the raw model response.

    Returns a dict carrying the prompt inputs, the rendered prompt digest, and
    the untouched response so fixture provenance is auditable without storing
    the full repeated prompt in every capture.
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

    # A successful Converse call is not enough for a positive playback
    # fixture: the untouched raw text must also survive the exact production
    # parse/normalize/validate path. Fail closed before the per-model file is
    # published; a later live run can retry a model that emitted malformed or
    # semantically ambiguous criteria.
    parsed = criteria_scaffold._parse_response(raw)
    if len(parsed) > criteria_scaffold.DEFAULT_MAX_CRITERIA:
        parsed = parsed[: criteria_scaffold.DEFAULT_MAX_CRITERIA]
    parsed = criteria_scaffold._normalize_sampled_criteria(parsed)
    validated = validate_criteria(parsed)
    criteria_scaffold._validate_sampled_criteria_context(
        validated,
        directive=directive.text,
        allowlist=list(directive.allowlist),
    )

    return {
        "prompt_directive": directive.text,
        "prompt_allowlist": list(directive.allowlist),
        "prompt_sha256": hashlib.sha256(prompt_str.encode("utf-8")).hexdigest(),
        "raw_response": raw,
    }


async def _capture_model(
    model_id: str,
    region: str,
    output_dir: Path,
    *,
    read_timeout_seconds: int | None = None,
) -> bool:
    """Capture all canonical directives for one model. Returns False on failure.

    Every directive is written into the same per-model JSON file under
    its ``slug`` key. A failure on one directive aborts the whole
    model's capture so the fixture file is either written wholesale
    or not at all — an incomplete fixture would silently weaken the
    replay test.
    """
    backend = _backend_for_capture(
        model_id,
        region,
        read_timeout_seconds=read_timeout_seconds,
    )
    captures: dict[str, dict[str, Any]] = {}
    for directive in _DIRECTIVES:
        try:
            captures[directive.slug] = await _capture_one(backend, directive)
        except BedrockFTUFormNotAcceptedError:
            # Account-wide gate, not a per-model failure: every Anthropic model
            # in the run would fail identically, so abort with the remediation
            # instead of repeating it once per model.
            raise
        except SamplingTransportError as exc:
            cause = f"; {exc.__cause__}" if exc.__cause__ is not None else ""
            print(
                f"[{model_id}] capture failed for {directive.slug!r}: {exc.code}: {exc}{cause}",
                file=sys.stderr,
            )
            return False
        except (MissionValidationError, ValueError) as exc:
            details = (
                f"; details={exc.details!r}" if isinstance(exc, MissionValidationError) else ""
            )
            print(
                f"[{model_id}] capture rejected for {directive.slug!r}: "
                f"{type(exc).__name__}: {exc}{details}",
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
    serialized = json.dumps(payload, indent=2) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        assert temporary_path is not None
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, output_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    print(f"[{model_id}] wrote {output_path.relative_to(_REPO_ROOT)}")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _positive_seconds(raw: str) -> int:
    """Argparse type for a strictly positive request timeout."""
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return value


def _positive_workers(raw: str) -> int:
    """Argparse type for a strictly positive model-worker count."""
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("workers must be greater than zero")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 1)[0] if __doc__ else "",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--model",
        action="append",
        dest="models",
        default=None,
        help=(
            "Bedrock model id to capture against; repeatable. "
            "Defaults to a curated cross-family set."
        ),
    )
    selection.add_argument(
        "--discover-all-models",
        action="store_true",
        help=(
            "Query the live Bedrock catalog and capture every uncaptured "
            "text-generation model line (one preferred profile per line)."
        ),
    )
    parser.add_argument(
        "--list-candidates",
        action="store_true",
        help="Print the selected models and paid-call budget without invoking Bedrock.",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("GCO_MISSION_BEDROCK_REGION", "us-east-1"),
        help="Bedrock region (default: us-east-1).",
    )
    parser.add_argument(
        "--read-timeout-seconds",
        type=_positive_seconds,
        default=None,
        help=(
            "Per-Converse read timeout. Discovery defaults to 300 seconds; "
            "curated/explicit capture retains the normal backend timeout."
        ),
    )
    parser.add_argument(
        "--workers",
        type=_positive_workers,
        default=None,
        help=(
            "Models to capture concurrently. Discovery defaults to 4; "
            "curated/explicit capture defaults to 1."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_FIXTURE_DIR,
        help=("Directory to write fixtures into. Defaults to tests/fixtures/scaffold_responses/."),
    )
    return parser


def _selected_models(args: argparse.Namespace) -> tuple[str, ...]:
    """Resolve explicit, discovered, or curated model selection."""
    if args.discover_all_models:
        return _discover_all_models(args.region, args.output_dir)
    if args.models:
        return tuple(dict.fromkeys(args.models))
    return _default_models()


async def _main_async(args: argparse.Namespace) -> int:
    models = _selected_models(args)
    if args.discover_all_models or args.list_candidates:
        print(
            f"Selected {len(models)} model candidate(s); a capture run makes "
            f"up to {len(models) * len(_DIRECTIVES)} paid Converse calls."
        )
        for model_id in models:
            print(model_id)
    if args.list_candidates:
        return 0
    if not models:
        print("No uncaptured model candidates found.")
        return 0

    read_timeout_seconds = getattr(args, "read_timeout_seconds", None)
    if read_timeout_seconds is None and args.discover_all_models:
        read_timeout_seconds = 300
    if args.discover_all_models:
        print(f"Per-request read timeout: {read_timeout_seconds}s")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    workers = getattr(args, "workers", None)
    if workers is None:
        workers = 4 if args.discover_all_models else 1
    if args.discover_all_models or workers > 1:
        print(f"Concurrent model workers: {workers}")

    if workers == 1:
        successes = 0
        failures = 0
        for model_id in models:
            try:
                ok = await _capture_model(
                    model_id,
                    args.region,
                    args.output_dir,
                    read_timeout_seconds=read_timeout_seconds,
                )
            except BedrockFTUFormNotAcceptedError as exc:
                # Preserve the historical curated-run behavior: a serial run
                # stops at the account-wide Anthropic prerequisite.
                print(f"\n{exc}", file=sys.stderr)
                print(f"Captured {successes} model(s) before aborting.", file=sys.stderr)
                return 1
            if ok:
                successes += 1
            else:
                failures += 1
        skipped = 0
    else:
        semaphore = asyncio.Semaphore(workers)
        anthropic_ftu = asyncio.Event()

        async def _capture_concurrently(
            model_id: str,
        ) -> bool | None | BedrockFTUFormNotAcceptedError:
            async with semaphore:
                if anthropic_ftu.is_set() and "anthropic." in model_id:
                    print(f"[{model_id}] skipped after Anthropic FTU failure", file=sys.stderr)
                    return None
                try:
                    return await _capture_model(
                        model_id,
                        args.region,
                        args.output_dir,
                        read_timeout_seconds=read_timeout_seconds,
                    )
                except BedrockFTUFormNotAcceptedError as exc:
                    anthropic_ftu.set()
                    return exc
                except Exception as exc:  # noqa: BLE001 - isolate broad model failures
                    print(
                        f"[{model_id}] unexpected capture failure: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    return False

        results = await asyncio.gather(*(_capture_concurrently(model_id) for model_id in models))
        successes = sum(result is True for result in results)
        failures = sum(
            result is False or isinstance(result, BedrockFTUFormNotAcceptedError)
            for result in results
        )
        skipped = sum(result is None for result in results)
        ftu_error = next(
            (result for result in results if isinstance(result, BedrockFTUFormNotAcceptedError)),
            None,
        )
        if ftu_error is not None:
            print(f"\n{ftu_error}", file=sys.stderr)

    print(f"\nCaptured {successes} model(s); {failures} failed; {skipped} skipped.")
    # A non-zero exit when *every* model failed is useful for cron
    # wrappers; a partial-failure run still exits 0 so a denied or unsupported
    # model doesn't stop successful fixtures from being committed.
    return 0 if successes > 0 else 1


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
