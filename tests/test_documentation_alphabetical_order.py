"""Alphabetical-order contracts for the documentation that is an inventory.

Some documentation is read top to bottom; some is scanned for one name. The
second kind — file inventories in package READMEs, the per-module MCP tool
tables, the CLI command groups, the workflow job rosters — only works when
its order is predictable, and nothing else in the gates checked that: the
coverage tests (``tests/test_docs_coverage.py``,
``tests/test_ci_scripts_readme_coverage.py``) prove every item is *present*,
so a row appended at the bottom of an otherwise-alphabetical table passed
every check while quietly breaking the scan. It drifted for real: four of the
seventeen module tables in ``gco_mcp/tools/README.md`` carried one appended
row each, two workflow headers that announce "alphabetical by display name"
were not, and one of them had lost a job entirely.

Scope is deliberate. Only inventories whose readers look things up by name
are registered below; tables ordered by workflow, learning path, apply
phase, or risk tier (``docs/README.md``, the ``gco_mcp/README.md`` domain
tables, ``tests/README.md``'s curated areas) are not, and adding one is a
one-line registration. Every registered anchor must resolve to a real,
non-trivial inventory — a renamed heading fails loudly instead of leaving
the contract silently unenforced.

Sort keys: case-insensitive; *natural* where names carry ordinals (the
numbered applier manifests — alphabetical there is literally the order
``kubectl-applier-simple/handler.py`` walks ``sorted(os.listdir(...))`` —
and the ADR index); directories after files where an inventory mixes them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

# ─── Markdown parsing ────────────────────────────────────────────────────────

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SEPARATOR = re.compile(r"^\|\s*:?-{2,}")
_LINK = re.compile(r"^\[([^\]]+)\]\([^)]*\)$")


@dataclass(frozen=True)
class Table:
    heading: str
    header: tuple[str, ...]
    first_cells: tuple[str, ...]
    line: int


def _cells(row: str) -> tuple[str, ...]:
    return tuple(cell.strip() for cell in row.strip().strip("|").split("|"))


def _normalize(cell: str) -> str:
    """The comparable name of an inventory row: no emphasis, code ticks, or link."""
    text = cell.strip().strip("*").strip()
    match = _LINK.match(text)
    if match:
        text = match.group(1)
    return text.strip("`").strip()


def _tables(text: str) -> list[Table]:
    lines = text.splitlines()
    tables: list[Table] = []
    heading = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _HEADING.match(line)
        if match:
            heading = match.group(2)
        if line.startswith("|") and index + 1 < len(lines) and _SEPARATOR.match(lines[index + 1]):
            header = _cells(line)
            rows: list[str] = []
            cursor = index + 2
            while cursor < len(lines) and lines[cursor].startswith("|"):
                rows.append(_normalize(_cells(lines[cursor])[0]))
                cursor += 1
            tables.append(Table(heading, header, tuple(rows), index + 1))
            index = cursor
            continue
        index += 1
    return tables


def _headings(text: str, level: int) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING.match(line)
        if match and len(match.group(1)) == level:
            found.append((match.group(2), number))
    return found


# ─── Ordering ────────────────────────────────────────────────────────────────


def _natural_key(name: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name.casefold())
    )


def _assert_alphabetical(
    items: list[str],
    *,
    where: str,
    natural: bool = False,
    dirs_last: bool = False,
    trailing: tuple[str, ...] = (),
    minimum: int = 3,
) -> None:
    """Fail with the first misplaced entry and the order the inventory should have."""
    assert len(items) >= minimum, (
        f"{where}: expected an inventory of at least {minimum} entries, found {items!r}"
    )
    duplicates = sorted({name for name in items if items.count(name) > 1})
    assert not duplicates, f"{where}: duplicate inventory entries {duplicates!r}"

    body = list(items)
    tail: list[str] = []
    while body and body[-1] in trailing:
        tail.insert(0, body.pop())
    assert tail == [name for name in trailing if name in tail], (
        f"{where}: trailing entries must appear in the order {list(trailing)!r}, found {tail!r}"
    )

    def key(name: str) -> tuple[object, ...]:
        base: tuple[object, ...] = _natural_key(name) if natural else (name.casefold(),)
        return (name.endswith("/"), *base) if dirs_last else base

    expected = sorted(body, key=key)
    if body != expected:
        misplaced = next(
            actual for actual, wanted in zip(body, expected, strict=True) if actual != wanted
        )
        raise AssertionError(
            f"{where}: not alphabetical — first entry out of place is {misplaced!r}.\n"
            f"  found:    {body}\n"
            f"  expected: {expected}"
        )


# ─── Registry: Markdown tables ───────────────────────────────────────────────


@dataclass(frozen=True)
class TableSpec:
    """One table under an exact heading, or every table with an exact header row."""

    path: str
    heading: str | None = None
    header: tuple[str, ...] | None = None
    natural: bool = False
    dirs_last: bool = False
    trailing: tuple[str, ...] = ()
    minimum: int = 3

    @property
    def id(self) -> str:
        anchor = f"## {self.heading}" if self.heading else "|" + "|".join(self.header or ()) + "|"
        return f"{self.path} {anchor}"


TABLE_SPECS: tuple[TableSpec, ...] = (
    # Repository automation: what lives where under .github/.
    TableSpec(".github/AUTOMATION.md", heading="Directory Map"),
    TableSpec(".github/scripts/README.md", heading="Files"),
    # CLI package inventories (the commands table lists command modules; the
    # commands package's own table closes with its package marker).
    TableSpec("cli/README.md", heading="commands/"),
    TableSpec("cli/commands/README.md", heading="Files", trailing=("__init__.py",)),
    # Library package inventories.
    TableSpec("gco/models/README.md", heading="Files"),
    TableSpec("gco/services/README.md", heading="Module Inventory"),
    TableSpec("gco/services/README.md", heading="API Routes"),
    TableSpec("gco/stacks/README.md", heading="Files"),
    TableSpec("gco_mcp/resources/README.md", heading="Files"),
    # MCP tools: the module inventory plus every per-module tool table
    # (tests/test_docs_coverage.py proves each registered tool has a row;
    # this proves the row is where a reader scanning the module will look).
    TableSpec("gco_mcp/tools/README.md", heading="Files"),
    TableSpec(
        "gco_mcp/tools/README.md",
        header=("Tool", "Description"),
        minimum=1,  # a module may expose one tool (cluster.py: cluster_tunnel_command)
    ),
    # Lambda and manifest inventories. The applier walks its manifests in
    # sorted filename order, so the numbered group tables and the post-Helm
    # table read in the order the files are applied.
    TableSpec("lambda/README.md", heading="Contents"),
    TableSpec(
        "lambda/kubectl-applier-simple/manifests/README.md",
        header=("File", "Contents"),
        natural=True,
        minimum=1,  # a numbered group may hold one file today (50–59: dcgm-exporter)
    ),
    # Top-level scripts: files first, then the two harness directories.
    TableSpec("scripts/README.md", heading="Contents", dirs_last=True),
    # Architecture decision records, by number.
    TableSpec("docs/adr/README.md", heading="Index", natural=True),
)


def _select_tables(spec: TableSpec) -> list[Table]:
    text = (ROOT / spec.path).read_text(encoding="utf-8")
    tables = _tables(text)
    if spec.heading is not None:
        under = [table for table in tables if table.heading == spec.heading]
        assert under, f"{spec.id}: no table found under a heading named {spec.heading!r}"
        return under[:1]
    assert spec.header is not None
    matching = [table for table in tables if table.header == spec.header]
    assert len(matching) >= 2, (
        f"{spec.id}: header-row selection expects several tables, found {len(matching)}; "
        "register the single table by heading instead"
    )
    return matching


@pytest.mark.parametrize("spec", TABLE_SPECS, ids=[spec.id for spec in TABLE_SPECS])
def test_inventory_tables_are_alphabetical(spec: TableSpec) -> None:
    for table in _select_tables(spec):
        _assert_alphabetical(
            list(table.first_cells),
            where=f"{spec.path}:{table.line} (under {table.heading!r})",
            natural=spec.natural,
            dirs_last=spec.dirs_last,
            trailing=spec.trailing,
            minimum=spec.minimum,
        )


def test_every_table_spec_resolves_to_a_distinct_inventory() -> None:
    """Two specs must not silently point at the same table."""
    seen: dict[tuple[str, int], str] = {}
    for spec in TABLE_SPECS:
        for table in _select_tables(spec):
            key = (spec.path, table.line)
            assert key not in seen, (
                f"{spec.id} and {seen[key]} both select {spec.path}:{table.line}"
            )
            seen[key] = spec.id


# ─── Registry: heading sequences ─────────────────────────────────────────────


@dataclass(frozen=True)
class HeadingSpec:
    """Backticked (file/module-named) headings of one level, optionally within one section."""

    path: str
    level: int
    within: str | None = None
    strip_suffix: str = ""

    @property
    def id(self) -> str:
        scope = f" within {self.within!r}" if self.within else ""
        return f"{self.path} h{self.level}{scope}"


HEADING_SPECS: tuple[HeadingSpec, ...] = (
    # One section per composite action, per config file, per MCP tool module.
    HeadingSpec(".github/actions/README.md", level=3, within="Actions"),
    HeadingSpec(".github/config/README.md", level=2),
    HeadingSpec("gco_mcp/tools/README.md", level=3),
    # docs/CLI.md's Commands section: one group per command family — the
    # "every gco command, alphabetically" the wiki promises.
    HeadingSpec("docs/CLI.md", level=3, within="Commands", strip_suffix=" Commands"),
)


def _select_headings(spec: HeadingSpec) -> list[str]:
    text = (ROOT / spec.path).read_text(encoding="utf-8")
    headings = _headings(text, spec.level)
    if spec.within is not None:
        parents = [line for title, line in _headings(text, spec.level - 1) if title == spec.within]
        assert parents, f"{spec.id}: no level-{spec.level - 1} heading named {spec.within!r}"
        start = parents[0]
        ends = [line for _title, line in _headings(text, spec.level - 1) if line > start]
        end = ends[0] if ends else float("inf")
        headings = [(title, line) for title, line in headings if start < line < end]
    if spec.strip_suffix:
        names = [re.sub(r"\s+Commands?$", "", title) for title, _line in headings]
    else:
        names = [
            title.strip("`")
            for title, _line in headings
            if title.startswith("`") and title.endswith("`")
        ]
    return names


@pytest.mark.parametrize("spec", HEADING_SPECS, ids=[spec.id for spec in HEADING_SPECS])
def test_inventory_headings_are_alphabetical(spec: HeadingSpec) -> None:
    _assert_alphabetical(_select_headings(spec), where=spec.id, minimum=4)


# ─── Workflow job rosters ────────────────────────────────────────────────────

_ROSTER_BANNER = "# Jobs (alphabetical by display name):"
_ROSTER_ENTRY = re.compile(r"^#\s{1,3}-\s+(.+?)(?:\s{2,}.*|\s+—.*)?$")
_MATRIX_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")


def _roster_workflows() -> list[Path]:
    return sorted(
        path
        for path in (ROOT / ".github" / "workflows").glob("*.yml")
        if _ROSTER_BANNER in path.read_text(encoding="utf-8")
    )


def _listed_jobs(text: str) -> list[str]:
    """Display names in the header roster; continuation lines carry no dash."""
    names: list[str] = []
    for line in text.split(_ROSTER_BANNER, 1)[1].splitlines()[1:]:
        if not line.startswith("#"):
            break
        match = _ROSTER_ENTRY.match(line)
        if match:
            names.append(match.group(1))
    return names


def _base_name(display_name: str) -> str:
    """A job's display name without its matrix suffix: ``(shard 1/4)``, ``(amd64)``."""
    return _MATRIX_SUFFIX.sub("", display_name)


def _actual_jobs(text: str) -> set[str]:
    """Base display names of the workflow's jobs (a job without ``name`` shows its id)."""
    return {
        _base_name(str((job or {}).get("name") or job_id))
        for job_id, job in (yaml.safe_load(text).get("jobs") or {}).items()
    }


def test_workflows_that_announce_an_alphabetical_roster_exist() -> None:
    names = {path.name for path in _roster_workflows()}
    assert {"integration-tests.yml", "security.yml", "unit-tests.yml"} <= names, names


@pytest.mark.parametrize("path", _roster_workflows(), ids=lambda path: path.name)
def test_workflow_job_roster_is_alphabetical_and_complete(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    listed = _listed_jobs(text)
    _assert_alphabetical(listed, where=f"{path.name} header roster", minimum=2)
    # A matrix job may be rostered once per human-readable form (the
    # combining job and its "(shard N/M)" slices), so completeness is judged
    # on base names while the ordering and duplicate checks above see the
    # full entries.
    actual = _actual_jobs(text)
    rostered = {_base_name(name) for name in listed}
    missing = sorted(actual - rostered)
    stale = sorted(rostered - actual)
    assert not missing and not stale, (
        f"{path.name}: the header roster must list every job by display name — "
        f"missing={missing!r} stale={stale!r}"
    )
