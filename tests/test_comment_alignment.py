"""Guard: trailing comments that were aligned into a column stay in it.

A block such as the ``[tool.ruff.lint] select`` list pads every ``# ...``
comment to one column. The failure mode is a later edit that appends a longer
entry: the new line is written with the minimum gap, the others keep the old
column, and nothing in review shows it because the diff is one line. This
happened eleven times across the tree before this guard existed
(``pyproject.toml``, seven ``bash`` fences in ``docs/``, a directory tree in
``docs/COST_MONITORING.md`` ...).

The rule is deliberately narrow so it never argues with a deliberate style. A
*run* of trailing comments (consecutive non-blank lines at one indentation, a
line without a comment or a full-line comment not breaking it) is flagged only
when both of these hold:

* the comment columns differ between lines, **and**
* the gaps (spaces between the code and the marker) differ between lines.

Constant-gap blocks (Black / ``ruff format`` style: exactly two spaces, columns
vary) never trip it; neither do fully aligned blocks. Any flagged block is fixed
by one of: re-padding to a single column (the failure message prints the padded
lines), separating groups with a blank line, or moving the odd comment onto its
own line above the code.

Scope: every tracked TOML, YAML, shell, Dockerfile, ignore-list and requirements
file, ``//`` comments in JavaScript, and the fenced code blocks in Markdown
(where the same drift hides in docs). Python is out of scope on purpose:
``ruff format --check`` already normalizes every trailing comment there.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

HASH_SUFFIXES = {".toml", ".yaml", ".yml", ".sh", ".bash", ".bats", ".zsh", ".txt", ".cfg", ".ini"}
HASH_NAMES = {
    "Dockerfile",
    "Gemfile",
    "CODEOWNERS",
    "Makefile",
    ".gitignore",
    ".dockerignore",
    ".trivyignore",
    ".semgrepignore",
    ".pip-audit-ignore",
    ".npm-audit-ignore",
    ".simplecov",
}
SLASH_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".mts"}
# Generated (never hand-edited) or comment-free files.
SKIP_NAMES = {"requirements-lock.txt", "LICENSE", "VERSION"}
# Fence languages whose ``#`` is not a comment marker (or is ambiguous). Every
# other language, including a bare fence and ``text`` (directory trees, sample
# output annotated with ``# ...``), is scanned with ``#``.
FENCE_SKIP = {
    "json",
    "jsonc",
    "json5",
    "html",
    "xml",
    "svg",
    "mermaid",
    "markdown",
    "md",
    "diff",
    "csv",
}
FENCE_SLASH = {"js", "javascript", "ts", "typescript", "mjs"}

# Sanity floor for the enumeration, so an empty or mis-scoped walk fails loudly
# instead of passing vacuously.
MIN_SCANNED_FILES = 150


@dataclass
class Line:
    number: int
    text: str
    code_end: int | None = None
    marker_col: int | None = None
    full_comment: bool = False

    @property
    def gap(self) -> int:
        assert self.code_end is not None and self.marker_col is not None
        return self.marker_col - self.code_end

    @property
    def indent(self) -> int:
        return len(self.text) - len(self.text.lstrip(" \t"))

    @property
    def blank(self) -> bool:
        return not self.text.strip()


@dataclass
class Run:
    path: str
    lines: list[Line] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        if len(self.lines) < 2:
            return False
        columns = {line.marker_col for line in self.lines}
        gaps = {line.gap for line in self.lines}
        return len(columns) > 1 and len(gaps) > 1

    def padded(self) -> list[str]:
        """The run re-padded to one column: the smallest gap in use, applied to the longest code."""
        min_gap = min(line.gap for line in self.lines)
        target = max(line.code_end + min_gap for line in self.lines if line.code_end is not None)
        out: list[str] = []
        for line in self.lines:
            assert line.code_end is not None and line.marker_col is not None
            code, comment = line.text[: line.code_end], line.text[line.marker_col :]
            out.append(code + " " * (target - line.code_end) + comment)
        return out


def _outside_quotes(prefix: str) -> bool:
    """True when ``prefix`` closes every quote it opens (a marker inside a string is not a comment)."""
    quote: str | None = None
    i = 0
    while i < len(prefix):
        char = prefix[i]
        if char == "\\" and quote == '"':
            i += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        i += 1
    return quote is None


def _trailing_marker(text: str, marker: str) -> int | None:
    """Column of a comment marker that follows code and whitespace, or None.

    Requiring whitespace before the marker leaves ``url#fragment``, ``$#``,
    ``${#array[@]}`` and ``"#fff"`` alone; the quote check leaves ``"a # b"`` alone.
    """
    start = 0
    while True:
        idx = text.find(marker, start)
        if idx <= 0:
            return None
        before = text[:idx]
        if before.strip() and before[-1] in " \t" and _outside_quotes(before):
            return idx
        start = idx + len(marker)


def annotate(lines: list[Line], marker: str) -> None:
    for line in lines:
        stripped = line.text.lstrip()
        if not stripped:
            continue
        if stripped.startswith(marker):
            line.full_comment = True
            continue
        idx = _trailing_marker(line.text, marker)
        if idx is not None:
            line.marker_col = idx
            line.code_end = len(line.text[:idx].rstrip())


def collect_runs(path: str, lines: list[Line]) -> list[Run]:
    runs: list[Run] = []
    current = Run(path)
    run_indent: int | None = None

    def close() -> None:
        nonlocal current, run_indent
        if current.lines:
            runs.append(current)
        current, run_indent = Run(path), None

    for line in lines:
        if line.blank:
            close()
        elif line.marker_col is not None:
            if run_indent is not None and line.indent != run_indent:
                close()
            run_indent = line.indent
            current.lines.append(line)
        elif not line.full_comment and run_indent is not None and line.indent < run_indent:
            close()
    close()
    return runs


def _fences(lines: list[Line]) -> list[tuple[int, int, str]]:
    """``(start, end, lang)`` index ranges of the content of every fenced block."""
    blocks: list[tuple[int, int, str]] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].text.lstrip()
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            lang = stripped[3:].strip().split(" ")[0].lower()
            j = i + 1
            while j < len(lines) and not lines[j].text.lstrip().startswith(fence):
                j += 1
            blocks.append((i + 1, j, lang))
            i = j + 1
            continue
        i += 1
    return blocks


def marker_for(rel: str) -> str | None:
    """``"#"``, ``"//"``, ``"md"`` (scan fences), or None when the file is out of scope."""
    path = Path(rel)
    if path.name in SKIP_NAMES:
        return None
    if (
        path.name in HASH_NAMES
        or path.name.startswith("Dockerfile")
        or path.suffix in HASH_SUFFIXES
    ):
        return "#"
    if path.suffix in SLASH_SUFFIXES:
        return "//"
    if path.suffix == ".md":
        return "md"
    return None


def scan_text(rel: str, source: str) -> list[Run]:
    marker = marker_for(rel)
    if marker is None:
        return []
    lines = [Line(i + 1, text) for i, text in enumerate(source.split("\n"))]
    if marker != "md":
        annotate(lines, marker)
        return collect_runs(rel, lines)
    runs: list[Run] = []
    for start, end, lang in _fences(lines):
        if lang in FENCE_SKIP:
            continue
        block = lines[start:end]
        annotate(block, "//" if lang in FENCE_SLASH else "#")
        runs.extend(collect_runs(rel, block))
    return runs


def flagged_runs(rel: str, source: str) -> list[Run]:
    return [run for run in scan_text(rel, source) if run.flagged]


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=PROJECT_ROOT, check=True, capture_output=True
    )
    return [raw.decode() for raw in result.stdout.split(b"\0") if raw]


def _describe(run: Run) -> str:
    first, last = run.lines[0].number, run.lines[-1].number
    original = "\n".join(f"    {line.number:>5}: {line.text}" for line in run.lines)
    fixed = "\n".join(f"           {text}" for text in run.padded())
    return f"{run.path}:{first}-{last}\n{original}\n  aligned:\n{fixed}"


def test_trailing_comments_in_aligned_blocks_share_one_column() -> None:
    scanned = 0
    offenders: list[Run] = []
    for rel in _tracked_files():
        if marker_for(rel) is None:
            continue
        path = PROJECT_ROOT / rel
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError, FileNotFoundError:
            continue
        scanned += 1
        offenders.extend(flagged_runs(rel, source))

    assert scanned >= MIN_SCANNED_FILES, f"only {scanned} files scanned; the enumeration is broken"
    assert not offenders, (
        f"{len(offenders)} block(s) of trailing comments have drifted out of their column. "
        "Re-pad them as shown (or separate the groups with a blank line, or move the "
        "odd comment onto its own line):\n\n" + "\n\n".join(_describe(run) for run in offenders)
    )


# --- Self-tests: the rule must flag exactly the drift shape and nothing else -----------------


def _flag(rel: str, source: str) -> list[tuple[int, int]]:
    return [(run.lines[0].number, run.lines[-1].number) for run in flagged_runs(rel, source)]


def test_knocked_out_member_of_an_aligned_block_is_flagged() -> None:
    source = '[a]\nx = [\n    "E",      # errors\n    "PLW1510",  # subprocess\n    "DTZ",    # datetimes\n]\n'
    assert _flag("pyproject.toml", source) == [(3, 5)]


def test_aligned_block_and_constant_gap_block_pass() -> None:
    aligned = "a: 1       # one\nbbbbbb: 2  # two\n"
    constant_gap = "a: 1  # one\nbbbbbb: 2  # two\nc: 3  # three\n"
    assert _flag("x.yaml", aligned) == []
    assert _flag("x.yaml", constant_gap) == []


def test_blank_line_and_indentation_change_start_a_new_run() -> None:
    separated = "a: 1   # one\nbb: 2  # two\n\nlong_key: 3 # three\n"
    nested = "spec:\n  a: 1          # one\n  bb: 2         # two\n  template:\n    c: 3  # three\n"
    assert _flag("x.yaml", separated) == []
    assert _flag("x.yaml", nested) == []


def test_closing_bracket_ends_the_run() -> None:
    source = 'select = [\n    "E",    # one\n    "UP",   # two\n]\nignore = [\n    "E501", # three\n    "B008", # four\n]\n'
    assert _flag("x.toml", source) == []


def test_markers_inside_strings_urls_and_shell_expansions_are_not_comments() -> None:
    shell = (
        'color="#fff"      # hex\n'
        'echo "a # b"      # quoted hash\n'
        "n=${#items[@]} # count\n"
        "url=https://example.com/a#frag   # fragment\n"
    )
    # Only the four real trailing comments count; they sit at two columns with
    # two gaps, so the block is (correctly) flagged as drifted.
    runs = flagged_runs("x.sh", shell)
    assert [line.marker_col for line in runs[0].lines] == [18, 18, 15, 33]


def test_markdown_scans_fenced_code_only_and_skips_comment_free_languages() -> None:
    doc = (
        "# Heading\n\nprose with a # sign   # not code\n\n"
        "```bash\ncmd one        # a\ncmd two-longer   # b\n```\n\n"
        '```json\n{"a": 1}   # not a comment\n{"bb": 2} # either\n```\n'
    )
    assert _flag("docs/X.md", doc) == [(6, 7)]


def test_python_and_generated_files_are_out_of_scope() -> None:
    source = "x = 1      # one\nlonger = 2  # two\n"
    assert marker_for("gco/x.py") is None
    assert marker_for("requirements-lock.txt") is None
    assert _flag("gco/x.py", source) == []


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("pyproject.toml", "#"),
        ("dockerfiles/Dockerfile.cost-monitor", "#"),
        (".github/config/.trivyignore", "#"),
        ("lambda/inference-streaming-proxy/index.mjs", "//"),
        ("docs/CLI.md", "md"),
        ("diagrams/out.png", None),
    ],
)
def test_marker_for_classifies_the_repository_file_shapes(rel: str, expected: str | None) -> None:
    assert marker_for(rel) == expected


def test_padded_output_uses_the_smallest_gap_against_the_longest_code() -> None:
    source = '    "E",      # errors\n    "PLW1510",  # subprocess\n'
    (run,) = flagged_runs("x.toml", source)
    assert run.padded() == ['    "E",        # errors', '    "PLW1510",  # subprocess']
