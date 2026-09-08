"""Strict shared loader for captured Mission scaffolder responses.

The replay fixtures are test inputs, not optional best-effort data.  Loading the
catalog therefore fails loudly when a JSON file is malformed, a required field
is missing, or one of the three canonical directive captures is absent.  CLI
and pipeline tests import the same validated default-model fixture from this
module.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from gco.bedrock import get_default_mission_model_id

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "scaffold_responses"
PROVENANCE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "scaffold_response_capture_provenance.json"
)

# Scaffold fixtures are Mission sampling captures, so replay follows the
# Mission knob. Canonical slugs are bound to exact prompt inputs so a renamed
# or repurposed capture cannot masquerade as one of the three branches.
DEFAULT_MODEL_ID = get_default_mission_model_id()
CANONICAL_CAPTURE_INPUTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "search_inference_docs": (
        "Find documentation about inference endpoints.",
        ("find_examples", "find_docs"),
    ),
    "metric_drive_loss": (
        "Drive validation loss below 0.1 on the demo training tool.",
        ("find_examples",),
    ),
    "event_goal_reached": (
        "Wait for the training job to emit a goal_reached event.",
        ("find_examples",),
    ),
}
CANONICAL_CAPTURE_SLUGS = tuple(CANONICAL_CAPTURE_INPUTS)


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
    prompt_sha256: str | None
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


def _load_prompt_provenance(path: Path) -> tuple[frozenset[str], dict[str, dict[str, str]]]:
    """Load legacy exceptions and prompt hashes for pre-hash capture cohorts."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureContractError(f"could not load prompt provenance {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise FixtureContractError(f"{path} must contain provenance version 1")

    legacy_payload = payload.get("legacy_unversioned_model_ids")
    if not isinstance(legacy_payload, list) or not all(
        isinstance(model_id, str) and model_id.strip() for model_id in legacy_payload
    ):
        raise FixtureContractError(f"{path}:legacy_unversioned_model_ids must be strings")
    if len(legacy_payload) != len(set(legacy_payload)):
        raise FixtureContractError(f"{path}:legacy_unversioned_model_ids contains duplicates")
    legacy = frozenset(legacy_payload)

    cohorts = payload.get("cohorts")
    if not isinstance(cohorts, list):
        raise FixtureContractError(f"{path}:cohorts must be a list")
    prompt_hashes: dict[str, dict[str, str]] = {}
    for index, cohort in enumerate(cohorts):
        if not isinstance(cohort, dict):
            raise FixtureContractError(f"{path}:cohorts.{index} must be an object")
        _nonempty_string(cohort.get("name"), path=path, field=f"cohorts.{index}.name")
        source_sha = _nonempty_string(
            cohort.get("source_git_sha"),
            path=path,
            field=f"cohorts.{index}.source_git_sha",
        )
        if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
            raise FixtureContractError(f"{path}:cohorts.{index}.source_git_sha is not a full SHA")

        hashes_payload = cohort.get("prompt_sha256_by_slug")
        if not isinstance(hashes_payload, dict) or set(hashes_payload) != set(
            CANONICAL_CAPTURE_SLUGS
        ):
            raise FixtureContractError(
                f"{path}:cohorts.{index}.prompt_sha256_by_slug must cover canonical slugs"
            )
        hashes: dict[str, str] = {}
        for slug in CANONICAL_CAPTURE_SLUGS:
            digest = hashes_payload.get(slug)
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise FixtureContractError(
                    f"{path}:cohorts.{index}.prompt_sha256_by_slug.{slug} is invalid"
                )
            hashes[slug] = digest

        model_ids = cohort.get("model_ids")
        if not isinstance(model_ids, list) or not all(
            isinstance(model_id, str) and model_id.strip() for model_id in model_ids
        ):
            raise FixtureContractError(f"{path}:cohorts.{index}.model_ids must be strings")
        if len(model_ids) != len(set(model_ids)):
            raise FixtureContractError(f"{path}:cohorts.{index}.model_ids contains duplicates")
        for model_id in model_ids:
            if model_id in legacy or model_id in prompt_hashes:
                raise FixtureContractError(f"{path}: duplicate provenance for {model_id}")
            prompt_hashes[model_id] = dict(hashes)

    return legacy, prompt_hashes


LEGACY_UNVERSIONED_MODELS, COHORT_PROMPT_HASHES = _load_prompt_provenance(PROVENANCE_PATH)


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
        allowlist = tuple(allowlist_payload)

        expected_inputs = CANONICAL_CAPTURE_INPUTS.get(slug)
        if expected_inputs is not None and (directive, allowlist) != expected_inputs:
            raise FixtureContractError(
                f"{_location(path, capture_field)} does not match canonical prompt inputs"
            )

        prompt_sha256 = capture_payload.get("prompt_sha256")
        if prompt_sha256 is not None:
            if (
                not isinstance(prompt_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", prompt_sha256) is None
            ):
                raise FixtureContractError(
                    f"{_location(path, f'{capture_field}.prompt_sha256')} is invalid"
                )
        else:
            cohort_hashes = COHORT_PROMPT_HASHES.get(model_id)
            if cohort_hashes is not None:
                prompt_sha256 = cohort_hashes.get(slug)
                if prompt_sha256 is None:
                    raise FixtureContractError(
                        f"{_location(path, capture_field)} has no cohort prompt hash"
                    )
            elif model_id not in LEGACY_UNVERSIONED_MODELS:
                raise FixtureContractError(
                    f"{_location(path, capture_field)} has no prompt provenance"
                )

        captures.append(
            ScaffoldReplayCapture(
                test_id=f"{path.stem}::{slug}",
                model_id=model_id,
                slug=slug,
                directive=directive,
                allowlist=allowlist,
                prompt_sha256=prompt_sha256,
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

    documented_models = LEGACY_UNVERSIONED_MODELS | COHORT_PROMPT_HASHES.keys()
    stale_provenance = sorted(set(documented_models) - set(model_ids))
    if stale_provenance:
        raise FixtureContractError(
            "prompt provenance references missing fixtures: " + ", ".join(stale_provenance)
        )

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
