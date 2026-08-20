"""The Floci emulator pin agrees everywhere it is written down.

The pinned image appears in two kinds of places with different strictness:

* ``.github/workflows/floci-tests.yml`` — the enforced pin, written as
  ``floci/floci:<tag>@sha256:<digest>`` once per service-container job;
* ``docs/FLOCI_TESTING.md`` — the local-run example, written as
  ``floci/floci:<tag>`` (running a local emulator by digest would be
  operator-hostile, so the doc carries the tag only).

Because the doc copy is prose, nothing enforced it: the #278 dependency
sweep bumped the workflow to 1.7.0 and the doc kept telling contributors to
run 1.6.0 locally — exactly the split-brain this module now rejects. Every
workflow occurrence must carry the same tag *and* digest, and every doc
occurrence must carry the workflow's tag.

Pure text checks: no Docker, no network, no AWS. This is deliberately NOT a
``test_floci_*`` module name — those globs select emulator-backed suites in
``floci-tests.yml``; this file is plain unit-suite material.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "floci-tests.yml"
DOC = PROJECT_ROOT / "docs" / "FLOCI_TESTING.md"

# Pinned form: floci/floci:<tag>@sha256:<64 hex>. Tags never contain '@'.
_PINNED = re.compile(r"floci/floci:([^@\s]+)@sha256:([0-9a-f]{64})")
# Any concrete image reference (pinned or tag-only). Excludes the literal
# placeholder ``floci/floci:<tag>`` / ``floci/floci:<new-tag>`` used by
# upgrade instructions in both files.
_ANY = re.compile(r"floci/floci:([^@\s`)]+)")
_PLACEHOLDER = re.compile(r"^<[^>]+>$")


def _workflow_pins() -> list[tuple[str, str]]:
    return _PINNED.findall(WORKFLOW.read_text())


def _concrete_tags(text: str) -> list[str]:
    return [
        tag.split("@", 1)[0]
        for tag in _ANY.findall(text)
        if not _PLACEHOLDER.match(tag.split("@", 1)[0])
    ]


class TestWorkflowPin:
    def test_workflow_has_at_least_one_pinned_image(self):
        assert _workflow_pins(), (
            f"no floci/floci:<tag>@sha256:<digest> pin found in {WORKFLOW}; "
            "either the pin format changed or the service containers are gone"
        )

    def test_workflow_pins_are_identical(self):
        pins = set(_workflow_pins())
        assert len(pins) == 1, (
            f"floci-tests.yml pins disagree between jobs: {sorted(pins)!r}; "
            "both service containers must run the same tag@digest"
        )

    def test_workflow_has_no_unpinned_reference(self):
        tags = _concrete_tags(WORKFLOW.read_text())
        pinned_tag = _workflow_pins()[0][0]
        stray = [t for t in tags if t != pinned_tag]
        assert not stray, f"floci-tests.yml references versions besides the pin: {stray!r}"


class TestDocMatchesWorkflow:
    def test_doc_mentions_a_concrete_version(self):
        assert _concrete_tags(DOC.read_text()), (
            f"{DOC} no longer shows a concrete floci/floci:<tag> local-run "
            "example; update this guard if the doc format changed on purpose"
        )

    def test_doc_versions_match_the_workflow_pin(self):
        pinned_tag = _workflow_pins()[0][0]
        doc_tags = set(_concrete_tags(DOC.read_text()))
        assert doc_tags == {pinned_tag}, (
            f"docs/FLOCI_TESTING.md tells contributors to run {sorted(doc_tags)!r} "
            f"but the workflow pins {pinned_tag!r}; bump the doc alongside the "
            "workflow (see 'Updating the Floci version' in that doc)"
        )
