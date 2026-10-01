#!/usr/bin/env python3
"""Give same-named functions in an lcov tracefile distinct names before genhtml.

Node's test runner writes one ``FN:<line>,<name>`` and one ``FNDA:<count>,<name>``
record per function, named as V8 names it. Two closures in one file can share
a name (``index.mjs`` has an ``onAbort`` in ``openUpstream`` and another in
``sleep``). lcov 2.4's ``genhtml`` (the Ubuntu 26.04 package) treats a repeated
name inside one ``SF:`` block as an inconsistent tracefile and stops::

    genhtml: ERROR: (inconsistent) "lcov.info":72: duplicate function 'onAbort'
    starts on line "index.mjs":974 but previous definition started on 897

``--ignore-errors inconsistent`` would merge the two functions' hit counts
under one name and hide a real inconsistency the next time one appears.
Renaming one closure in the source would recur with the next same-named
closure. This script instead renames only the repeated functions, to
``<name>@L<line>``, in both records, so the report shows each function at its
own line with its own count and every other name stays as V8 wrote it.

The k-th ``FNDA`` of a name belongs to the k-th ``FN`` of that name: Node
writes both lists in the same order. The script fails closed when a record's
``FN`` and ``FNDA`` names do not agree in multiplicity, when a line is not a
tracefile record, or when ``FNF`` disagrees with the functions listed, rather
than guess.

Usage::

    python3 .github/scripts/disambiguate_lcov_functions.py lcov.info [--output OTHER]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: ``FN:<start>[,<end>],<name>`` (lcov 2 may write an end line; Node does not).
FN_RE = re.compile(r"^FN:(\d+)(?:,(\d+))?,(.+)$")
#: ``FNDA:<count>,<name>``
FNDA_RE = re.compile(r"^FNDA:(\d+),(.+)$")
#: Every other record kind a tracefile may carry; anything else is not lcov.
RECORD_RE = re.compile(r"^(?:TN:|SF:|FNF:|FNH:|DA:|LF:|LH:|BRDA:|BRF:|BRH:|VER:|end_of_record$)")


class TracefileError(ValueError):
    """The tracefile is not something this script can rename safely."""


@dataclass
class Record:
    """One ``SF:`` block's function records, as indices into the tracefile's lines."""

    source: str
    fn: list[tuple[int, str, str]] = field(default_factory=list)  # (index, line, name)
    fnda: list[tuple[int, str]] = field(default_factory=list)  # (index, name)
    fnf: int | None = None


def parse_records(lines: Sequence[str]) -> list[Record]:
    """Split the tracefile into ``SF:`` blocks, refusing any line that is not a record."""
    records: list[Record] = []
    current: Record | None = None
    for index, line in enumerate(lines):
        if not line.strip() or line.startswith("TN:"):
            continue
        if line.startswith("SF:"):
            current = Record(source=line.removeprefix("SF:"))
            records.append(current)
            continue
        if current is None:
            raise TracefileError(f"line {index + 1}: {line!r} appears before any SF: record")
        if fn := FN_RE.match(line):
            current.fn.append((index, fn.group(1), fn.group(3)))
        elif fnda := FNDA_RE.match(line):
            current.fnda.append((index, fnda.group(2)))
        elif line.startswith("FNF:"):
            current.fnf = int(line.removeprefix("FNF:"))
        elif not RECORD_RE.match(line):
            raise TracefileError(f"line {index + 1}: {line!r} is not an lcov record")
        if line == "end_of_record":
            current = None
    return records


def _check(record: Record) -> set[str]:
    """Validate one record's function lists; return the names that repeat."""
    fn_names = Counter(name for _, _, name in record.fn)
    fnda_names = Counter(name for _, name in record.fnda)
    if fn_names != fnda_names:
        disagreements = ", ".join(
            f"{name!r} FN x{fn_names[name]} FNDA x{fnda_names[name]}"
            for name in sorted(set(fn_names) | set(fnda_names))
            if fn_names[name] != fnda_names[name]
        )
        raise TracefileError(f"{record.source}: FN and FNDA records disagree: {disagreements}")
    if record.fnf is not None and record.fnf != len(record.fn):
        raise TracefileError(f"{record.source}: FNF:{record.fnf} but {len(record.fn)} FN records")
    return {name for name, count in fn_names.items() if count > 1}


def disambiguate(text: str) -> tuple[str, list[str]]:
    """Return the tracefile with repeated function names made distinct, and the renames."""
    lines = text.splitlines()
    renames: list[str] = []
    for record in parse_records(lines):
        repeated = _check(record)
        new_names: dict[str, list[str]] = {name: [] for name in repeated}
        for index, line, name in record.fn:
            if name in repeated:
                new_name = f"{name}@L{line}"
                new_names[name].append(new_name)
                lines[index] = lines[index].removesuffix(name) + new_name
                renames.append(f"{record.source}: {name} at line {line} -> {new_name}")
        for index, name in record.fnda:
            if name in repeated:
                lines[index] = lines[index].removesuffix(name) + new_names[name].pop(0)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), renames


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rename repeated function names in an lcov tracefile to <name>@L<line>."
    )
    parser.add_argument("tracefile", type=Path, help="lcov tracefile to read")
    parser.add_argument(
        "--output",
        type=Path,
        help="where to write the result (default: rewrite the tracefile in place)",
    )
    args = parser.parse_args(argv)
    try:
        text, renames = disambiguate(args.tracefile.read_text(encoding="utf-8"))
    except (OSError, TracefileError) as exc:
        print(f"::error::{args.tracefile}: {exc}", flush=True)
        return 1
    (args.output or args.tracefile).write_text(text, encoding="utf-8")
    for rename in renames:
        print(rename)
    print(f"{args.tracefile}: {len(renames)} function name(s) made distinct", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
