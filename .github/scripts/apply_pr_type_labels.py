#!/usr/bin/env python3
"""Apply the conventional-commit type label a pull request already declares.

``.github/pull_request_template.md`` asks every author to tick exactly one
"Type of change" box, and ``.github/release.yml`` groups the generated release
notes by label. Nothing connected the two, so a PR could declare ``feat:`` in
its body and still land under "Other changes" because no label was ever
applied. v6.5.0 and v6.5.1 both shipped that way: every entry in "Other
changes", with the one genuinely breaking change indistinguishable from a docs
tweak.

This closes the loop by treating the ticked checkbox as the source of truth and
syncing the nine type labels to match it. The author keeps control — they edit
the checkbox, the label follows — and no second thing has to be remembered at
merge time for the changelog to come out right.

Scope is deliberately narrow: only the nine labels in ``TYPE_LABELS`` are ever
added or removed. Every other label on the PR (``dependencies``, ``automated``,
``ignore-for-release``, triage labels) is untouched, so this can never fight a
human or another workflow over a label it does not own.

Ticking nothing is a no-op rather than a strip. A body with no box ticked is
far more likely to be a template not yet filled in than a deliberate
instruction to remove the labels, and silently clearing them would be the more
surprising of the two readings.

Usage::

    apply_pr_type_labels.py --pr 297                 # sync labels
    apply_pr_type_labels.py --pr 297 --dry-run       # print the plan only

``GH_TOKEN`` and ``GH_REPO`` come from the environment, as the ``gh`` CLI
expects. The pull-request body is read through ``gh`` rather than passed in on
the command line or through the workflow's ``run:`` script, so untrusted text
never reaches a shell.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

#: The nine "Type of change" boxes in the pull-request template, each mapped to
#: the label of the same name. Kept in template order so ``--dry-run`` output
#: and the tests read in the same sequence a contributor sees the checkboxes.
#: A test asserts this tuple and the template stay in lockstep, so adding a
#: type to one without the other fails rather than silently doing nothing.
TYPE_LABELS: tuple[str, ...] = (
    "feat",
    "fix",
    "docs",
    "refactor",
    "perf",
    "test",
    "ci",
    "chore",
    "breaking",
)

#: A ticked box in the template's "Type of change" list, e.g.
#: ``- [x] `feat:` New feature (non-breaking)``. GitHub renders ``[X]`` and
#: ``[x]`` identically, so both count.
_TICKED_TYPE = re.compile(r"^\s*[-*]\s*\[[xX]\]\s*`(\w+):`", re.MULTILINE)


def declared_types(body: str | None) -> list[str]:
    """Return the recognized types ticked in ``body``, in template order.

    Unknown ticked boxes are ignored rather than treated as an error: the
    template carries other checklists (testing, live validation) whose items are
    not types at all, and a contributor inventing ``- [x] `wibble:`` should get
    no label, not a failed run.
    """
    if not body:
        return []
    ticked = {match.group(1) for match in _TICKED_TYPE.finditer(body)}
    return [name for name in TYPE_LABELS if name in ticked]


def label_plan(current: list[str], declared: list[str]) -> tuple[list[str], list[str]]:
    """Return ``(to_add, to_remove)`` for the type labels only.

    ``current`` may contain any labels at all; only those in ``TYPE_LABELS`` are
    eligible for removal, which is what keeps unrelated labels safe.
    """
    if not declared:
        return [], []
    have = set(current)
    want = set(declared)
    to_add = [name for name in TYPE_LABELS if name in want and name not in have]
    to_remove = [name for name in TYPE_LABELS if name in have and name not in want]
    return to_add, to_remove


def _gh_json(args: list[str]) -> dict:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout or "{}")


def fetch_pull_request(number: int) -> tuple[str | None, list[str]]:
    """Return ``(body, current_label_names)`` for one pull request."""
    data = _gh_json(["pr", "view", str(number), "--json", "body,labels"])
    labels = [str(item["name"]) for item in data.get("labels") or []]
    return data.get("body"), labels


def apply_labels(number: int, to_add: list[str], to_remove: list[str]) -> None:
    """Add and remove labels in one ``gh pr edit`` call."""
    args = ["pr", "edit", str(number)]
    for name in to_add:
        args += ["--add-label", name]
    for name in to_remove:
        args += ["--remove-label", name]
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"could not update labels: {proc.stderr.strip()[:400]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, required=True, help="Pull request number")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the plan without changing any labels.",
    )
    args = parser.parse_args(argv)

    body, current = fetch_pull_request(args.pr)
    declared = declared_types(body)
    if not declared:
        print(
            f"PR #{args.pr}: no recognized type checkbox is ticked; leaving labels "
            "unchanged. Tick one in the 'Type of change' list so the release notes "
            "can categorize this change."
        )
        return 0

    to_add, to_remove = label_plan(current, declared)
    print(f"PR #{args.pr}: declares {', '.join(declared)}")
    if not to_add and not to_remove:
        print("  labels already match; nothing to do")
        return 0
    if to_add:
        print(f"  add:    {', '.join(to_add)}")
    if to_remove:
        print(f"  remove: {', '.join(to_remove)}")
    if args.dry_run:
        print("  (dry run: no changes made)")
        return 0
    apply_labels(args.pr, to_add, to_remove)
    print("  labels updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
