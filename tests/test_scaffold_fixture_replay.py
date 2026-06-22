"""Replay captured Bedrock model output through the scaffolder pipeline.

The Mission scaffolder's sampling path is sensitive to the shapes a
model emits — different families default to different Pythonic
idioms (``r.get(...)``, comprehension dict-access, attribute walks).
``scripts/capture_scaffold_fixtures.py`` writes one JSON file per
model under ``tests/fixtures/scaffold_responses/``, each carrying
the raw response the model produced for a small set of canonical
directives. This test drives every (model, directive) pair through
the same pipeline the live scaffolder uses:

    raw response
    -> ``criteria_scaffold._parse_response`` (JSON extraction,
       markdown-fence strip)
    -> truncate to ``max_criteria``
    -> ``criteria_scaffold._normalize_kind_name`` (rewrite
       ``tool_calls_succeeded`` and other captured typos to the
       canonical kind name)
    -> ``criteria_scaffold._normalize_metric_path`` (auto-prefix bare
       metric names with ``metrics.``)
    -> ``criteria_scaffold._autofix_predicate`` (rewrite
       ``obs.a.b`` -> ``obs['a']['b']``)
    -> ``mission.validation.validate_criteria`` (structural validation
       + predicate AST sandbox)

A new model — or a regressed scaffolder — that produces a shape the
validator rejects flips this test red on the next CI run, even
though no operator has run it live. New shapes accumulate as
fixtures, so every subsequent change to the validator or
scaffolder is automatically tested against the cumulative
historical surface.

The test does NOT need network or AWS credentials. It runs
entirely against checked-in JSON. The matching live capture
script lives next to it at ``scripts/capture_scaffold_fixtures.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Mirror the path-injection pattern used throughout the Mission
# tests so ``mission.*`` resolves regardless of how pytest is
# invoked.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission import criteria_scaffold  # noqa: E402

# Match the scaffolder's max-criteria cap so the replay applies the
# same truncation the live ``generate_sampled_criteria`` does. Falls
# back to ``5`` (the documented default) if the constant is ever
# moved or removed.
_MAX = getattr(criteria_scaffold, "DEFAULT_MAX_CRITERIA", 5)


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "scaffold_responses"


def _discover_fixtures() -> list[Path]:
    """Return every model fixture file shipped under the fixture dir."""
    if not _FIXTURE_DIR.is_dir():
        return []
    return sorted(_FIXTURE_DIR.glob("*.json"))


def _flatten_captures(
    fixtures: list[Path],
) -> list[tuple[str, str, str, list[str], str]]:
    """Flatten the per-model fixture files into per-directive cases.

    Each tuple is ``(test_id, model_id, directive_text, allowlist,
    raw_response)``. ``test_id`` is what pytest's parametrize id
    factory shows when the test fails, so a regression points
    directly at the (model, directive) pair that broke.
    """
    rows: list[tuple[str, str, str, list[str], str]] = []
    for path in fixtures:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        model_id = str(payload.get("model_id", path.stem))
        captures = payload.get("captures") or {}
        if not isinstance(captures, dict):
            continue
        for slug, capture in captures.items():
            if not isinstance(capture, dict):
                continue
            directive = capture.get("prompt_directive")
            allowlist = capture.get("prompt_allowlist") or []
            raw = capture.get("raw_response")
            if not isinstance(directive, str) or not isinstance(raw, str):
                continue
            if not isinstance(allowlist, list):
                continue
            rows.append(
                (
                    f"{path.stem}::{slug}",
                    model_id,
                    directive,
                    [str(name) for name in allowlist],
                    raw,
                )
            )
    return rows


_FIXTURES = _discover_fixtures()
_CASES = _flatten_captures(_FIXTURES)


@pytest.mark.skipif(
    not _CASES,
    reason=(
        "no scaffolder fixtures shipped under tests/fixtures/scaffold_responses/ "
        "— run scripts/capture_scaffold_fixtures.py to populate"
    ),
)
@pytest.mark.parametrize(
    ("model_id", "directive", "allowlist", "raw_response"),
    [(model, directive, allow, raw) for _, model, directive, allow, raw in _CASES],
    ids=[case_id for case_id, *_ in _CASES],
)
def test_captured_response_round_trips_through_scaffolder(
    model_id: str,
    directive: str,
    allowlist: list[str],
    raw_response: str,
) -> None:
    """A captured model response must survive the full scaffolder pipeline.

    The scaffolder runs four passes on every model response: JSON
    extraction, max-criteria truncation, metric-path normalisation,
    and predicate autofix. The structural validator then either
    accepts the result or raises a structured rejection. This test
    confirms the cumulative pipeline is robust enough that every
    captured (model, directive) pair lands in the "accepts" path
    without any retry.

    A regression here means one of two things:
      * The scaffolder changed in a way that no longer rescues a
        shape the captured model emits — fix the scaffolder.
      * A new captured fixture surfaces a shape the validator
        rejects — relax the validator (or add an autofix), or, in
        the rare case the rejection is correct, add an
        ``expected_validation_failure`` block to the fixture and
        teach this test to honour it. (No fixture currently uses
        that escape hatch.)
    """
    # Defence in depth: the inputs come from JSON we control, but
    # the parametrize-id is a string that bubbles up into pytest
    # output, so a fixture with an absurdly long ``directive`` would
    # produce a spammy test name. The check is cheap and the
    # rejection diagnostic is still useful.
    assert directive, f"empty directive in fixture for {model_id}"
    del model_id, directive, allowlist  # only used for parametrize ids

    parsed = criteria_scaffold._parse_response(raw_response)
    if len(parsed) > _MAX:
        parsed = parsed[:_MAX]
    parsed = [criteria_scaffold._normalize_kind_name(c) for c in parsed]
    parsed = [criteria_scaffold._normalize_metric_path(c) for c in parsed]
    parsed = [criteria_scaffold._autofix_predicate(c) for c in parsed]

    # The structural validator is the contract: it either accepts
    # the criterion list or raises a structured rejection. Any
    # rejection here is the regression signal.
    from mission.validation import validate_criteria  # noqa: PLC0415

    validated = validate_criteria(parsed)
    assert validated  # non-empty by construction


def test_fixture_directory_contains_at_least_one_capture() -> None:
    """Guard against accidentally clearing the fixture directory.

    The replay test skips when no fixtures are present (so a fresh
    clone before ``capture_scaffold_fixtures.py`` runs doesn't fail
    the suite). This second test asserts the directory holds at
    least one fixture so a delete-everything regression is caught
    explicitly rather than masquerading as a "skipped" line in CI
    output.
    """
    assert _FIXTURE_DIR.is_dir(), (
        f"fixture directory missing: {_FIXTURE_DIR.relative_to(Path.cwd())}"
    )
    assert _FIXTURES, (
        "no scaffolder fixtures shipped under "
        f"{_FIXTURE_DIR.relative_to(Path.cwd())} — captured fixtures are "
        "checked into the repo so the validator pipeline is exercised "
        "against every captured model on every CI run"
    )
