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
import json
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


# ---------------------------------------------------------------------------
# The gh boundary and main()
#
# ``gh`` is faked at ``subprocess.run`` so nothing here needs a GitHub token or
# a network. What is worth pinning at this layer is the *shape* of the calls the
# script makes -- a label change is a write to a real pull request, so the exact
# argv matters -- and the exit-code policy, which is "never fail the workflow
# over a label": a body with no box ticked is a no-op, not an error.
# ---------------------------------------------------------------------------

_FEAT_BODY = "## Type of change\n\n- [x] `feat:` New feature (non-breaking)\n- [ ] `fix:` Bug fix\n"


class _FakeGh:
    """Record every ``gh`` invocation and answer ``pr view`` with a fixed PR."""

    def __init__(self, *, body: str | None, labels: list[str], edit_fails: bool = False) -> None:
        self.body = body
        self.labels = labels
        self.edit_fails = edit_fails
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:  # noqa: ARG002
        self.calls.append(argv)
        assert argv[0] == "gh", argv
        if argv[1:3] == ["pr", "view"]:
            payload = {"body": self.body, "labels": [{"name": name} for name in self.labels]}
            return _Completed(0, stdout=json.dumps(payload))
        if argv[1:3] == ["pr", "edit"]:
            if self.edit_fails:
                return _Completed(1, stderr="HTTP 403: Resource not accessible by integration")
            return _Completed(0)
        raise AssertionError(f"unexpected gh invocation: {argv}")

    def edits(self) -> list[list[str]]:
        return [call for call in self.calls if call[1:3] == ["pr", "edit"]]


class _Completed:
    def __init__(self, returncode: int, *, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_fetch_pull_request_reads_body_and_label_names(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = _FakeGh(body=_FEAT_BODY, labels=["dependencies", "fix"])
    monkeypatch.setattr(script.subprocess, "run", gh)

    body, labels = script.fetch_pull_request(42)

    assert body == _FEAT_BODY
    assert labels == ["dependencies", "fix"]
    assert gh.calls == [["gh", "pr", "view", "42", "--json", "body,labels"]]


def test_fetch_pull_request_tolerates_a_null_label_list(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``gh`` emits ``"labels": null`` for a PR with none, not ``[]``."""

    def run(argv: list[str], **kwargs: Any) -> Any:  # noqa: ARG001
        return _Completed(0, stdout=json.dumps({"body": _FEAT_BODY, "labels": None}))

    monkeypatch.setattr(script.subprocess, "run", run)

    assert script.fetch_pull_request(42) == (_FEAT_BODY, [])


def test_a_failing_gh_command_surfaces_its_stderr(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stderr excerpt is the only diagnostic a workflow log will show."""

    def run(argv: list[str], **kwargs: Any) -> Any:  # noqa: ARG001
        return _Completed(1, stderr="gh: Not Found (HTTP 404)")

    monkeypatch.setattr(script.subprocess, "run", run)

    with pytest.raises(RuntimeError, match=r"gh pr view 42 .* failed: gh: Not Found"):
        script.fetch_pull_request(42)


def test_apply_labels_issues_one_edit_with_every_change(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ``gh pr edit``, not one per label -- fewer API calls and atomic."""
    gh = _FakeGh(body=None, labels=[])
    monkeypatch.setattr(script.subprocess, "run", gh)

    script.apply_labels(42, ["feat", "docs"], ["fix"])

    assert gh.edits() == [
        [
            "gh",
            "pr",
            "edit",
            "42",
            "--add-label",
            "feat",
            "--add-label",
            "docs",
            "--remove-label",
            "fix",
        ]
    ]


def test_apply_labels_raises_when_the_edit_is_refused(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = _FakeGh(body=None, labels=[], edit_fails=True)
    monkeypatch.setattr(script.subprocess, "run", gh)

    with pytest.raises(RuntimeError, match="could not update labels: HTTP 403"):
        script.apply_labels(42, ["feat"], [])


def test_main_leaves_labels_alone_when_nothing_is_ticked(
    script: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unfilled template is likelier than a request to clear the labels."""
    gh = _FakeGh(body="## Type of change\n\n- [ ] `feat:` New feature\n", labels=["fix"])
    monkeypatch.setattr(script.subprocess, "run", gh)

    assert script.main(["--pr", "42"]) == 0

    assert gh.edits() == [], "a body with no ticked box must not touch labels"
    assert "no recognized type checkbox is ticked" in capsys.readouterr().out


def test_main_is_a_no_op_when_labels_already_match(
    script: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = _FakeGh(body=_FEAT_BODY, labels=["feat", "dependencies"])
    monkeypatch.setattr(script.subprocess, "run", gh)

    assert script.main(["--pr", "42"]) == 0

    assert gh.edits() == []
    assert "labels already match; nothing to do" in capsys.readouterr().out


def test_main_dry_run_prints_the_plan_without_editing(
    script: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = _FakeGh(body=_FEAT_BODY, labels=["fix", "dependencies"])
    monkeypatch.setattr(script.subprocess, "run", gh)

    assert script.main(["--pr", "42", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert gh.edits() == []
    assert "add:    feat" in out
    assert "remove: fix" in out
    assert "(dry run: no changes made)" in out


def test_main_applies_the_plan_and_leaves_foreign_labels_untouched(
    script: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only type labels move; ``dependencies`` survives the sync."""
    gh = _FakeGh(body=_FEAT_BODY, labels=["fix", "dependencies"])
    monkeypatch.setattr(script.subprocess, "run", gh)

    assert script.main(["--pr", "42"]) == 0

    edits = gh.edits()
    assert len(edits) == 1
    assert "--add-label" in edits[0] and edits[0][edits[0].index("--add-label") + 1] == "feat"
    assert "--remove-label" in edits[0] and edits[0][edits[0].index("--remove-label") + 1] == "fix"
    assert "dependencies" not in edits[0]
    assert "labels updated" in capsys.readouterr().out


def test_main_prints_only_the_side_of_the_plan_that_has_entries(
    script: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Add-only and remove-only plans must not print an empty other side."""
    gh = _FakeGh(body=_FEAT_BODY, labels=[])
    monkeypatch.setattr(script.subprocess, "run", gh)
    assert script.main(["--pr", "42", "--dry-run"]) == 0
    add_only = capsys.readouterr().out
    assert "add:    feat" in add_only and "remove:" not in add_only

    gh = _FakeGh(body="- [x] `fix:` Bug fix\n", labels=["fix", "feat"])
    monkeypatch.setattr(script.subprocess, "run", gh)
    assert script.main(["--pr", "42", "--dry-run"]) == 0
    remove_only = capsys.readouterr().out
    assert "remove: feat" in remove_only and "add:" not in remove_only
