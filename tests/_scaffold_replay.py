"""Strict shared loader for captured Mission scaffolder responses.

The replay fixtures are test inputs, not optional best-effort data.  Loading the
catalog therefore fails loudly when a JSON file is malformed, a required field
is missing, or one of the three canonical directive captures is absent.  CLI
and pipeline tests import the same validated default-model fixture from this
module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from gco.bedrock import get_default_mission_model_id

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "scaffold_responses"

# Scaffold fixtures are Mission sampling captures, so replay follows the
# Mission knob.
DEFAULT_MODEL_ID = get_default_mission_model_id()
CANONICAL_CAPTURE_SLUGS = (
    "search_inference_docs",
    "metric_drive_loss",
    "event_goal_reached",
)


class FixtureContractError(ValueError):
    """A checked-in scaffold fixture does not satisfy the replay contract."""


@dataclass(frozen=True)
class ScaffoldReplayCapture:
    """One validated model response for one canonical directive."""

    test_id: str
    model_id: str
    slug: str
    directive: str
    allowlist: tuple[str, ...]
    raw_response: str


@dataclass(frozen=True)
class ScaffoldReplayFixture:
    """One validated per-model fixture file."""

    path: Path
    model_id: str
    region: str
    captured_at: str
    captures: tuple[ScaffoldReplayCapture, ...]

    def capture(self, slug: str) -> ScaffoldReplayCapture:
        """Return a capture by slug, failing clearly if it is unavailable."""
        for capture in self.captures:
            if capture.slug == slug:
                return capture
        raise KeyError(f"{self.model_id} fixture has no {slug!r} capture")


def model_fixture_slug(model_id: str) -> str:
    """Return the filename-safe slug used by the live capture script."""
    output: list[str] = []
    previous_was_underscore = False
    for character in model_id:
        if character.isalnum():
            output.append(character)
            previous_was_underscore = False
        elif not previous_was_underscore:
            output.append("_")
            previous_was_underscore = True
    return "".join(output).strip("_")


def _location(path: Path, field: str) -> str:
    try:
        displayed_path = path.relative_to(REPOSITORY_ROOT)
    except ValueError:
        displayed_path = path
    return f"{displayed_path}:{field}"


def _nonempty_string(value: Any, *, path: Path, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FixtureContractError(f"{_location(path, field)} must be a non-empty string")
    return value


def load_fixture(path: Path) -> ScaffoldReplayFixture:
    """Load and strictly validate one scaffold replay fixture."""
    try:
        raw_payload = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FixtureContractError(f"could not read {path}: {exc}") from exc

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise FixtureContractError(
            f"{_location(path, f'line {exc.lineno}')} contains invalid JSON: {exc.msg}"
        ) from exc

    if not isinstance(payload, dict):
        raise FixtureContractError(f"{path} must contain a JSON object")

    model_id = _nonempty_string(payload.get("model_id"), path=path, field="model_id")
    expected_name = f"{model_fixture_slug(model_id)}.json"
    if path.name != expected_name:
        raise FixtureContractError(
            f"{path.name} does not match model_id {model_id!r}; expected {expected_name}"
        )

    region = _nonempty_string(payload.get("region"), path=path, field="region")
    captured_at = _nonempty_string(payload.get("captured_at"), path=path, field="captured_at")
    try:
        capture_time = datetime.fromisoformat(captured_at)
    except ValueError as exc:
        raise FixtureContractError(
            f"{_location(path, 'captured_at')} must be an ISO-8601 timestamp"
        ) from exc
    if capture_time.tzinfo is None or capture_time.utcoffset() is None:
        raise FixtureContractError(f"{_location(path, 'captured_at')} must include a UTC offset")

    captures_payload = payload.get("captures")
    if not isinstance(captures_payload, dict):
        raise FixtureContractError(f"{_location(path, 'captures')} must be a JSON object")

    missing = sorted(set(CANONICAL_CAPTURE_SLUGS) - captures_payload.keys())
    if missing:
        raise FixtureContractError(
            f"{_location(path, 'captures')} is missing canonical captures: {', '.join(missing)}"
        )

    ordered_slugs = (
        *CANONICAL_CAPTURE_SLUGS,
        *sorted(set(captures_payload) - set(CANONICAL_CAPTURE_SLUGS)),
    )
    captures: list[ScaffoldReplayCapture] = []
    for slug in ordered_slugs:
        capture_payload = captures_payload[slug]
        capture_field = f"captures.{slug}"
        if not isinstance(capture_payload, dict):
            raise FixtureContractError(f"{_location(path, capture_field)} must be a JSON object")

        directive = _nonempty_string(
            capture_payload.get("prompt_directive"),
            path=path,
            field=f"{capture_field}.prompt_directive",
        )
        raw_response = _nonempty_string(
            capture_payload.get("raw_response"),
            path=path,
            field=f"{capture_field}.raw_response",
        )
        allowlist_payload = capture_payload.get("prompt_allowlist")
        if not isinstance(allowlist_payload, list) or not all(
            isinstance(name, str) and name.strip() for name in allowlist_payload
        ):
            raise FixtureContractError(
                f"{_location(path, f'{capture_field}.prompt_allowlist')} must be "
                "a list of non-empty strings"
            )
        if len(allowlist_payload) != len(set(allowlist_payload)):
            raise FixtureContractError(
                f"{_location(path, f'{capture_field}.prompt_allowlist')} contains "
                "duplicate tool names"
            )

        captures.append(
            ScaffoldReplayCapture(
                test_id=f"{path.stem}::{slug}",
                model_id=model_id,
                slug=slug,
                directive=directive,
                allowlist=tuple(allowlist_payload),
                raw_response=raw_response,
            )
        )

    return ScaffoldReplayFixture(
        path=path,
        model_id=model_id,
        region=region,
        captured_at=captured_at,
        captures=tuple(captures),
    )


def load_fixture_catalog(
    fixture_dir: Path = FIXTURE_DIR,
) -> tuple[ScaffoldReplayFixture, ...]:
    """Load every JSON fixture and enforce catalog-level invariants."""
    if not fixture_dir.is_dir():
        raise FixtureContractError(f"fixture directory is missing: {fixture_dir}")

    paths = sorted(fixture_dir.glob("*.json"))
    if not paths:
        raise FixtureContractError(f"no JSON fixtures found under {fixture_dir}")

    fixtures = tuple(load_fixture(path) for path in paths)
    model_ids = [fixture.model_id for fixture in fixtures]
    duplicate_model_ids = sorted(
        model_id for model_id in set(model_ids) if model_ids.count(model_id) > 1
    )
    if duplicate_model_ids:
        raise FixtureContractError("duplicate fixture model ids: " + ", ".join(duplicate_model_ids))

    if DEFAULT_MODEL_ID not in model_ids:
        raise FixtureContractError(f"default model fixture is missing: {DEFAULT_MODEL_ID}")

    return fixtures


FIXTURES = load_fixture_catalog()
FIXTURES_BY_MODEL = {fixture.model_id: fixture for fixture in FIXTURES}
DEFAULT_FIXTURE = FIXTURES_BY_MODEL[DEFAULT_MODEL_ID]
DEFAULT_FIXTURE_PATH = FIXTURE_DIR / f"{model_fixture_slug(DEFAULT_MODEL_ID)}.json"
if DEFAULT_FIXTURE.path != DEFAULT_FIXTURE_PATH:
    raise FixtureContractError(
        f"Default fixture must be stored at {DEFAULT_FIXTURE_PATH}, not {DEFAULT_FIXTURE.path}"
    )

REPLAY_CASES = tuple(capture for fixture in FIXTURES for capture in fixture.captures)
