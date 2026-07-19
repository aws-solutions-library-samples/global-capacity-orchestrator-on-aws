"""Replay captured Bedrock model output through the scaffolder pipeline.

The Mission scaffolder's sampling path is sensitive to the shapes a model emits.
``scripts/capture_scaffold_fixtures.py`` writes one JSON file per model, and the
strict shared loader rejects malformed files or fixtures missing any canonical
capture.  This test drives every validated (model, directive) pair through the
same parse, normalization, autofix, and validation pipeline used live.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Mirror the path-injection pattern used throughout the Mission tests so
# ``mission.*`` resolves regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission import criteria_scaffold  # noqa: E402
from mission.validation import validate_criteria  # noqa: E402

from tests._scaffold_replay import (  # noqa: E402
    CANONICAL_CAPTURE_SLUGS,
    FIXTURES,
    REPLAY_CASES,
)

# Match the scaffolder's max-criteria cap so replay applies the same truncation
# as live ``generate_sampled_criteria``.  Five is its documented fallback.
_MAX = getattr(criteria_scaffold, "DEFAULT_MAX_CRITERIA", 5)


@pytest.mark.parametrize(
    ("model_id", "directive", "allowlist", "raw_response"),
    [
        (
            case.model_id,
            case.directive,
            list(case.allowlist),
            case.raw_response,
        )
        for case in REPLAY_CASES
    ],
    ids=[case.test_id for case in REPLAY_CASES],
)
def test_captured_response_round_trips_through_scaffolder(
    model_id: str,
    directive: str,
    allowlist: list[str],
    raw_response: str,
) -> None:
    """Every captured response must survive the full scaffolder pipeline."""
    assert model_id
    assert directive
    assert allowlist

    parsed = criteria_scaffold._parse_response(raw_response)
    if len(parsed) > _MAX:
        parsed = parsed[:_MAX]
    parsed = [criteria_scaffold._normalize_kind_name(criterion) for criterion in parsed]
    parsed = [criteria_scaffold._normalize_metric_path(criterion) for criterion in parsed]
    parsed = [criteria_scaffold._autofix_predicate(criterion) for criterion in parsed]

    validated = validate_criteria(parsed)
    assert validated


def test_fixture_catalog_contains_every_canonical_capture() -> None:
    """No checked-in model fixture may silently omit a canonical branch."""
    assert FIXTURES
    required_slugs = set(CANONICAL_CAPTURE_SLUGS)
    for fixture in FIXTURES:
        assert required_slugs <= {capture.slug for capture in fixture.captures}, fixture.model_id
