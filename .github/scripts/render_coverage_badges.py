"""Render the shields.io endpoint JSON for the README's three coverage badges.

``pages.yml`` publishes one coverage report per test stack and, next to each,
the badge JSON the README's ``img.shields.io/endpoint`` badges read. The three
numbers come from three different tools, so they are read here, in one place,
and written in one schema:

``python-coverage-badge.json``
    from ``coverage.json`` (coverage.py's JSON report, shipped in the
    ``pytest-coverage`` artifact): ``totals.percent_covered``, which counts
    statements and branches together because ``[tool.coverage.run]`` measures
    branches.

``bash-coverage-badge.json``
    from ``summary.json`` written by ``check_bash_coverage.py --report``
    (shipped in the ``bash-coverage-report`` artifact): the statement coverage
    after the checker's lexer corrections — the number the gate enforces, not
    SimpleCov's raw line count. A summary recording a failed floor is refused
    rather than badged.

``nodejs-coverage-badge.json``
    from the ``lcov.info`` Node's test runner writes (``--test-reporter=lcov``,
    shipped in the ``node-inference-streaming-proxy-coverage`` artifact): lines
    and branches together (``LH+BRH`` over ``LF+BRF``), the same combination
    coverage.py reports, so the three badges measure alike.

Every badge is bright green at exactly 100% and red below it. That is the same
floor the three test jobs enforce (``--cov-fail-under=100``, the shell checker,
``--test-coverage-lines=100`` and friends), so the badge never introduces a
second threshold: green means the gate would pass on this measurement.

Usage::

    python3 .github/scripts/render_coverage_badges.py \\
        --python coverage-data/coverage.json \\
        --bash bash-coverage-data/report/summary.json \\
        --node node-coverage-data/lcov.info \\
        --out site

Exit codes::

    0  the three badge files were written
    2  an input is missing, unreadable or not the document it should be
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FLOOR = 100.0

BADGES: tuple[tuple[str, str, str], ...] = (
    # (command-line option, badge label, output file name)
    ("python", "python coverage", "python-coverage-badge.json"),
    ("bash", "bash coverage", "bash-coverage-badge.json"),
    ("node", "node.js coverage", "nodejs-coverage-badge.json"),
)


class BadgeError(Exception):
    """An input could not be read or does not carry the number it should."""


def _read_json(path: Path, what: str) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BadgeError(f"could not read {what} from {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BadgeError(f"{path} is not {what} (expected a JSON object)")
    return raw


def _number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadgeError(f"{where} is not a number: {value!r}")
    return float(value)


def python_percent(path: Path) -> float:
    """``totals.percent_covered`` from coverage.py's JSON report."""
    data = _read_json(path, "coverage.py's JSON report")
    totals = data.get("totals")
    if not isinstance(totals, dict) or "percent_covered" not in totals:
        raise BadgeError(
            f"{path} has no totals.percent_covered; is it coverage.py's coverage.json?"
        )
    return _number(totals["percent_covered"], f"{path}: totals.percent_covered")


def bash_percent(path: Path) -> float:
    """``percent`` from the shell checker's summary, refusing a failed floor."""
    data = _read_json(path, "the shell coverage summary")
    if data.get("ok") is not True:
        raise BadgeError(
            f"{path} records a run that did not pass the shell coverage floor "
            f"(ok={data.get('ok')!r}); a badge is not rendered for it"
        )
    if "percent" not in data:
        raise BadgeError(f"{path} has no percent; is it check_bash_coverage.py's summary.json?")
    return _number(data["percent"], f"{path}: percent")


def node_percent(path: Path) -> float:
    """Lines and branches together from an lcov tracefile.

    ``LF``/``LH`` are the lines found and hit per source file, ``BRF``/``BRH``
    the branches. The tracefile is summed across every ``SF:`` record, so a
    second instrumented file would count too; today there is one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BadgeError(f"could not read the lcov tracefile {path}: {exc}") from exc
    totals = {"LF": 0, "LH": 0, "BRF": 0, "BRH": 0}
    records = 0
    for line in text.splitlines():
        key, _sep, value = line.partition(":")
        if key == "SF":
            records += 1
        elif key in totals:
            try:
                totals[key] += int(value)
            except ValueError as exc:
                raise BadgeError(f"{path}: malformed lcov line {line!r}") from exc
    if records == 0:
        raise BadgeError(f"{path} holds no SF: records; is it an lcov tracefile?")
    found = totals["LF"] + totals["BRF"]
    if found == 0:
        raise BadgeError(f"{path} reports no lines or branches, so there is nothing to measure")
    return 100.0 * (totals["LH"] + totals["BRH"]) / found


def badge(label: str, percent: float) -> dict[str, object]:
    """The shields.io endpoint document for one badge."""
    return {
        "schemaVersion": 1,
        "label": label,
        "message": f"{percent:.1f}%",
        "color": "brightgreen" if percent >= FLOOR else "red",
    }


READERS = {"python": python_percent, "bash": bash_percent, "node": node_percent}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--python", type=Path, required=True, help="coverage.py's coverage.json")
    parser.add_argument(
        "--bash", type=Path, required=True, help="check_bash_coverage.py's summary.json"
    )
    parser.add_argument("--node", type=Path, required=True, help="Node's lcov.info tracefile")
    parser.add_argument(
        "--out", type=Path, required=True, help="directory the three badge files are written to"
    )
    args = parser.parse_args(argv)

    rendered: list[tuple[Path, dict[str, object]]] = []
    try:
        for option, label, name in BADGES:
            percent = READERS[option](getattr(args, option))
            rendered.append((args.out / name, badge(label, percent)))
    except BadgeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    # Nothing is written until every input has been read, so a broken input
    # leaves no half-rendered set behind for the deploy to publish.
    args.out.mkdir(parents=True, exist_ok=True)
    for path, document in rendered:
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        print(f"{path.name}: {document['label']} {document['message']} ({document['color']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
