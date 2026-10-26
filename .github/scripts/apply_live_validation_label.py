#!/usr/bin/env python3
"""Label a pull request that touches a deployed path ``live-validation-required``.

docs/LIVE_RELEASE_VALIDATION.md decides when a change needs a live release
validation run, and the pull-request template asks the author to record that
decision. Nothing pointed a reviewer at the decision before merge, so a weekly
Dependabot batch that bumped the AWS SDK clients bundled into the inference
streaming Lambda merged on green like a CI-only update.

This adds the label when any changed file sits on a path that ships in, or
drives, a deployment (``DEPLOYED_PREFIXES`` and ``DEPLOYED_FILES``), and prints
which files matched. It is a prompt, not the decision: the docs' rule is
risk-based, so a docstring-only edit under ``gco/`` is labelled too, and the
author still records "Not required" with a reason in the template.

Scope is deliberately narrow. The script only ever adds the one label it owns
and never removes it, so a maintainer who applies it by hand, or Dependabot's
``runtime`` group, which applies it from ``.github/dependabot.yml``, is never
overridden. Markdown files are ignored everywhere: a README next to a Lambda
does not ship.

Usage::

    apply_live_validation_label.py --pr 490             # add the label if needed
    apply_live_validation_label.py --pr 490 --dry-run   # print the plan only

``GH_TOKEN`` and ``GH_REPO`` come from the environment, as the ``gh`` CLI
expects. The changed-file list is read through ``gh api`` with only the PR
number, so no pull-request text reaches a shell.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

#: The label this script owns. Dependabot's ``runtime`` group applies the same
#: label from ``.github/dependabot.yml``; a test keeps the two in agreement.
LABEL = "live-validation-required"

#: Directories whose contents ship in a deployment: the CDK app and services
#: (``gco/``), every Lambda (``lambda/``) and the service images
#: (``dockerfiles/``).
DEPLOYED_PREFIXES: tuple[str, ...] = ("gco/", "lambda/", "dockerfiles/")

#: Single files that change what a deployment synthesizes or runs: the CDK
#: entry point and context, the Python runtime graph, the root npm graph (it
#: carries the AWS CDK CLI), and the deploy/destroy orchestration.
DEPLOYED_FILES: frozenset[str] = frozenset(
    {
        "app.py",
        "cdk.json",
        "pyproject.toml",
        "requirements-lock.txt",
        "package.json",
        "package-lock.json",
        "cli/stacks.py",
    }
)


def deployed_paths(paths: list[str]) -> list[str]:
    """Return the changed paths that sit on a deployed path, sorted."""
    matched = {
        path
        for path in paths
        if not path.lower().endswith(".md")
        and (path in DEPLOYED_FILES or path.startswith(DEPLOYED_PREFIXES))
    }
    return sorted(matched)


def _gh(args: list[str]) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


def changed_files(number: int) -> list[str]:
    """Return every file the pull request changes.

    ``gh pr view --json files`` and ``gh pr diff`` both stop short on large pull
    requests, so this pages the REST files endpoint instead. ``--jq`` emits one
    JSON string per line, which keeps file names with spaces intact.
    """
    out = _gh(
        [
            "api",
            "--paginate",
            f"repos/{{owner}}/{{repo}}/pulls/{number}/files?per_page=100",
            "--jq",
            ".[].filename | tojson",
        ]
    )
    return [str(json.loads(line)) for line in out.splitlines() if line.strip()]


def current_labels(number: int) -> list[str]:
    data = json.loads(_gh(["pr", "view", str(number), "--json", "labels"]) or "{}")
    return [str(item["name"]) for item in data.get("labels") or []]


def add_label(number: int) -> None:
    _gh(["pr", "edit", str(number), "--add-label", LABEL])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, required=True, help="Pull request number")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the plan without changing any labels.",
    )
    args = parser.parse_args(argv)

    matched = deployed_paths(changed_files(args.pr))
    if not matched:
        print(f"PR #{args.pr}: no deployed path changed; leaving labels unchanged.")
        return 0

    print(f"PR #{args.pr}: {len(matched)} changed file(s) on a deployed path:")
    for path in matched[:20]:
        print(f"  {path}")
    if len(matched) > 20:
        print(f"  ... and {len(matched) - 20} more")
    print(
        "Record the live release validation decision in the PR template "
        "(docs/LIVE_RELEASE_VALIDATION.md)."
    )
    if LABEL in current_labels(args.pr):
        print(f"  '{LABEL}' is already applied; nothing to do")
        return 0
    if args.dry_run:
        print(f"  would add: {LABEL} (dry run: no changes made)")
        return 0
    add_label(args.pr)
    print(f"  added: {LABEL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
