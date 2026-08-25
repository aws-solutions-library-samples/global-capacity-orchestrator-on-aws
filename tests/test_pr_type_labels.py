"""Contract for the PR type-label sync (``.github/scripts/apply_pr_type_labels.py``).

The script turns the "Type of change" checkbox an author already ticks into the
label ``.github/release.yml`` groups release notes by. Three things have to hold
for that to be worth having, and each gets a test here:

1. **Agreement with the template** — the script's ``TYPE_LABELS`` and the
   template's checkbox list are the same set, so adding a type to one without
   the other fails loudly instead of silently never being labelled.
2. **Agreement with the release config** — every type the script can apply is a
   label ``release.yml`` either categorizes or deliberately leaves to the
   catch-all, so no label is applied that the changelog cannot place.
3. **Narrow blast radius** — only the nine type labels are ever added or
   removed. A PR's ``dependencies``, ``automated`` or triage labels must survive
   a sync untouched, because this workflow does not own them.

Parsing is exercised directly against bodies rather than through ``gh``: the
subprocess boundary is a thin wrapper, and the interesting failure modes are all
in reading a body a human filled in by hand.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _PROJECT_ROOT / ".github" / "scripts" / "apply_pr_type_labels.py"
_TEMPLATE = _PROJECT_ROOT / ".github" / "pull_request_template.md"
_RELEASE_CONFIG = _PROJECT_ROOT / ".github" / "release.yml"


def _load_script() -> Any:
    """Import the helper by path — ``.github`` is not an importable package."""
    spec = importlib.util.spec_from_file_location("apply_pr_type_labels", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script() -> Any:
    return _load_script()


def _template_types() -> list[str]:
    pattern = re.compile(r"^- \[[ xX]\] `(\w+):`", re.MULTILINE)
    return pattern.findall(_TEMPLATE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Agreement with the template and the release config
# ---------------------------------------------------------------------------


def test_type_labels_match_the_pull_request_template(script: Any) -> None:
    """The script and the template must describe the same set of types."""
    assert sorted(script.TYPE_LABELS) == sorted(_template_types()), (
        "the 'Type of change' checkboxes in .github/pull_request_template.md and "
        "TYPE_LABELS in .github/scripts/apply_pr_type_labels.py have drifted; a "
        "type present in only one place either never gets a label or gets one no "
        "template box can request"
    )


def test_template_order_is_preserved(script: Any) -> None:
    """Order is part of the contract: dry-run output should read like the form."""
    assert list(script.TYPE_LABELS) == _template_types()


def test_every_type_label_is_placeable_by_the_release_config(script: Any) -> None:
    """No type may be applied that release.yml cannot file somewhere.

    A label with no category and no catch-all would vanish from the notes
    entirely, which is worse than the "Other changes" bucket this replaced.
    """
    config = yaml.safe_load(_RELEASE_CONFIG.read_text(encoding="utf-8"))
    categorized: set[str] = set()
    has_catch_all = False
    for category in config["changelog"]["categories"]:
        for label in category["labels"]:
            if label == "*":
                has_catch_all = True
            else:
                categorized.add(label)
    unplaceable = [name for name in script.TYPE_LABELS if name not in categorized]
    assert not unplaceable or has_catch_all, (
        f"these type labels have no release.yml category and there is no '*' "
        f"catch-all to absorb them: {unplaceable}"
    )


# ---------------------------------------------------------------------------
# declared_types
# ---------------------------------------------------------------------------


def test_a_ticked_box_is_detected(script: Any) -> None:
    body = "## Type of change\n\n- [x] `feat:` New feature (non-breaking)\n- [ ] `fix:` Bug fix\n"
    assert script.declared_types(body) == ["feat"]


def test_capital_x_counts(script: Any) -> None:
    """GitHub renders [X] and [x] identically, so both must parse."""
    assert script.declared_types("- [X] `docs:` Documentation only") == ["docs"]


def test_unticked_boxes_are_ignored(script: Any) -> None:
    body = "- [ ] `feat:` New feature\n- [ ] `fix:` Bug fix\n"
    assert script.declared_types(body) == []


def test_multiple_ticks_are_all_returned_in_template_order(script: Any) -> None:
    """#297 legitimately ticked two boxes (feat + docs); that must not be lossy."""
    body = "- [x] `docs:` Documentation only\n- [x] `feat:` New feature\n"
    assert script.declared_types(body) == ["feat", "docs"]


def test_unknown_ticked_tokens_are_ignored(script: Any) -> None:
    """Other checklists in the template are ticked too and are not types."""
    body = "- [x] `wibble:` Not a type\n- [x] `feat:` New feature\n"
    assert script.declared_types(body) == ["feat"]


def test_other_ticked_checklist_items_do_not_produce_labels(script: Any) -> None:
    body = "## Testing\n\n- [x] `pytest tests/` passes locally\n- [x] New tests added\n"
    assert script.declared_types(body) == []


@pytest.mark.parametrize("body", [None, "", "no checkboxes here at all"])
def test_empty_bodies_declare_nothing(script: Any, body: str | None) -> None:
    assert script.declared_types(body) == []


# ---------------------------------------------------------------------------
# label_plan
# ---------------------------------------------------------------------------


def test_missing_label_is_added(script: Any) -> None:
    assert script.label_plan([], ["feat"]) == (["feat"], [])


def test_matching_label_is_left_alone(script: Any) -> None:
    assert script.label_plan(["feat"], ["feat"]) == ([], [])


def test_corrected_type_replaces_the_stale_label(script: Any) -> None:
    """Author re-ticks fix instead of feat: the old label has to go."""
    assert script.label_plan(["feat"], ["fix"]) == (["fix"], ["feat"])


def test_unrelated_labels_are_never_removed(script: Any) -> None:
    """The sweep owns nine labels and must not touch anything else."""
    current = ["dependencies", "automated", "ignore-for-release", "python", "feat"]
    to_add, to_remove = script.label_plan(current, ["feat"])
    assert (to_add, to_remove) == ([], [])
    current_with_stale = [*current, "fix"]
    to_add, to_remove = script.label_plan(current_with_stale, ["feat"])
    assert to_add == []
    assert to_remove == ["fix"], "only the stale type label may be removed"


def test_declaring_nothing_is_a_no_op_not_a_strip(script: Any) -> None:
    """An unfilled template must not silently clear an existing label."""
    assert script.label_plan(["feat", "dependencies"], []) == ([], [])
