"""Repository-wide Markdown link guard.

Every tracked Markdown file outside ``wiki/`` is scanned and three classes
of link are proven to resolve against the checkout:

* **relative paths** — ``[text](docs/CLI.md)``, ``![img](images/x.png)``,
  ``<img src="images/x.png">``, ``<a href="LICENSE">`` — must name a file or
  directory that exists inside the repository (percent-encoding and
  root-relative ``/path`` forms included);
* **anchors** — ``#fragment`` on the same page or on a linked ``.md`` page —
  must match a heading rendered with GitHub's slug rules (or an explicit
  ``<a id=…>`` anchor). Same-file fragments are also covered by markdownlint
  MD051, but cross-file fragments are not, and the two checks agreeing is a
  useful calibration of the slugger below;
* **repository deep links** — ``https://github.com/<org>/<repo>/blob|tree/main/<path>``
  — must point at a path that exists in the checkout, exactly like the wiki
  guard in ``tests/test_wiki.py`` does for ``wiki/``.

``wiki/`` pages are excluded on purpose: ``mkdocs build --strict`` and
``tests/test_wiki.py`` already validate them, and their relative links resolve
against the *built* site, where ``scripts/mkdocs_hooks.py`` injects assets
that do not exist in the source tree. The one exception is ``wiki/README.md``,
the directory's GitHub-facing README: it is excluded from the MkDocs build and
its links (``../mkdocs.yml``, ``../scripts/mkdocs_hooks.py``, …) resolve
against the source tree like every other README's, so it is scanned here.

External URLs are deliberately out of scope — a unit test must not depend on
the network — which is also why nothing here is a substitute for a periodic
manual pass over third-party links.

Fenced code blocks, inline code spans, and HTML comments are ignored so that
literal examples such as ``[name](url)`` in a tutorial never count as links.
Each failing test lists every offender as ``path:line: kind target`` so the
fix is mechanical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_URL = "https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws"

#: Directory names never scanned (mirrors .github/config/.markdownlint-cli2.yaml
#: plus the worktree/IDE dirs a local checkout may carry).
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".worktrees",
        ".kiro",
        ".venv",
        "venv",
        "node_modules",
        "cdk.out",
        "site",
        "build",
        "dist",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".hypothesis",
        ".cache",
        ".example-job-validation",
        "kubectl-applier-simple-build",
        "helm-installer-build",
    }
)
#: Top-level trees with their own link validation (see module docstring).
EXCLUDED_TOP_LEVEL = frozenset({"wiki"})
#: Files inside an excluded tree that are scanned anyway (see module docstring).
SCANNED_EXCEPTIONS = frozenset({"wiki/README.md"})

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
#: ``](target)`` and ``](<target>)`` with optional title; one level of balanced
#: parentheses is allowed inside a bare target, as CommonMark permits.
_INLINE_TARGET = re.compile(r"\]\(\s*(?:<(?P<angle>[^>]*)>|(?P<bare>(?:[^()\s]|\([^()\s]*\))+))")
_HTML_ATTR = re.compile(r"\b(?:src|href)=\"(?P<target>[^\"]+)\"")
_AUTOLINK = re.compile(r"<(?P<target>https?://[^>\s]+)>")
_BARE_DEEP_LINK = re.compile(re.escape(REPO_URL) + r"/(?:blob|tree)/main/[^\s<>)\"']+")
_REFERENCE_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(?P<target>\S+)", re.MULTILINE)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_REPO_DEEP_LINK = re.compile(
    re.escape(REPO_URL) + r"/(?:blob|tree)/main/(?P<path>[^#?]+)(?:#(?P<fragment>.*))?$"
)
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(?P<text>.*?)\s*(?:(?<=\s)#+\s*)?$")
_EXPLICIT_ANCHOR = re.compile(r"<a\b[^>]*\b(?:id|name)=\"(?P<anchor>[^\"]+)\"")
_MD_LINK_TEXT = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]+>")
_EMPHASIS_UNDERSCORE = re.compile(r"(?<!\w)_([^_\s][^_]*?)_(?!\w)")
_SLUG_DROP = re.compile(r"[^\w\- ]", re.UNICODE)


@dataclass(frozen=True)
class Link:
    """One link occurrence: where it is and what it points at."""

    source: Path
    line: int
    target: str

    def describe(self, kind: str) -> str:
        return f"{self.source.relative_to(PROJECT_ROOT)}:{self.line}: {kind} {self.target}"


def markdown_files() -> list[Path]:
    """Every scanned Markdown file, sorted, honoring both exclusion lists."""
    files: list[Path] = []
    for path in PROJECT_ROOT.rglob("*.md"):
        relative = path.relative_to(PROJECT_ROOT)
        parts = relative.parts
        if any(part in EXCLUDED_DIR_NAMES for part in parts):
            continue
        if parts[0] in EXCLUDED_TOP_LEVEL and relative.as_posix() not in SCANNED_EXCEPTIONS:
            continue
        files.append(path)
    return sorted(files)


def strip_code(text: str) -> str:
    """Blank out fenced blocks, inline code spans, and HTML comments, keeping line numbers."""
    text = _HTML_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    out: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        opener = _FENCE.match(line)
        if fence is None and opener:
            fence = opener.group(1)
            out.append("")
            continue
        if fence is not None:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence):
                fence = None
            out.append("")
            continue
        out.append(_CODE_SPAN.sub(" ", line))
    return "\n".join(out)


def extract_links(path: Path) -> list[Link]:
    """All link targets in ``path`` (inline, image, HTML attribute, reference definition)."""
    stripped = strip_code(path.read_text(encoding="utf-8"))
    links: list[Link] = []
    for line_no, line in enumerate(stripped.split("\n"), start=1):
        for match in _INLINE_TARGET.finditer(line):
            target = (
                match.group("angle") if match.group("angle") is not None else match.group("bare")
            )
            links.append(Link(path, line_no, target.strip()))
        for match in _HTML_ATTR.finditer(line):
            links.append(Link(path, line_no, match.group("target").strip()))
        for match in _AUTOLINK.finditer(line):
            links.append(Link(path, line_no, match.group("target").strip()))
        # Bare repository deep links in prose are auto-linked by GitHub and
        # rot just as silently as the bracketed kind.
        for match in _BARE_DEEP_LINK.finditer(line):
            candidate = Link(path, line_no, match.group(0))
            if candidate not in links:
                links.append(candidate)
    for match in _REFERENCE_DEF.finditer(stripped):
        line_no = stripped.count("\n", 0, match.start()) + 1
        links.append(Link(path, line_no, match.group("target").strip()))
    return links


def github_slug(heading_text: str) -> str:
    """GitHub's heading → anchor rule (github-slugger) for one rendered heading.

    Inline markup is rendered first (link text kept, code-span contents kept,
    HTML tags and emphasis markers dropped), then: lowercase, drop everything
    that is not a word character, hyphen, or space, and turn spaces into
    hyphens. Underscores are word characters and survive, which is what makes
    ``#gco-tasks-show-task_id`` right and ``#gco-tasks-show-taskid`` wrong.
    """
    text = _CODE_SPAN.sub(lambda m: m.group(2), heading_text)
    text = _MD_LINK_TEXT.sub(lambda m: m.group(1), text)
    text = _HTML_TAG.sub("", text)
    text = text.replace("*", "")
    text = _EMPHASIS_UNDERSCORE.sub(lambda m: m.group(1), text)
    text = _SLUG_DROP.sub("", text.strip().lower())
    return text.replace(" ", "-")


def heading_lines(text: str) -> list[str]:
    """Heading text of every ATX heading outside fenced code blocks, in order.

    Inline code is kept (``### \\`gco tasks show\\``` renders its span text
    into the anchor), so only fences are skipped here.
    """
    headings: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        opener = _FENCE.match(line)
        if fence is None and opener:
            fence = opener.group(1)
            continue
        if fence is not None:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence):
                fence = None
            continue
        heading = _HEADING.match(line)
        if heading:
            headings.append(heading.group("text"))
    return headings


def anchors_in(path: Path) -> frozenset[str]:
    """Every fragment a link may legitimately point at inside ``path``."""
    text = path.read_text(encoding="utf-8")
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    for heading_text in heading_lines(text):
        slug = github_slug(heading_text)
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    for match in _EXPLICIT_ANCHOR.finditer(text):
        anchors.add(match.group("anchor"))
    return frozenset(anchors)


def is_external(target: str) -> bool:
    return bool(_SCHEME.match(target)) or target.startswith("//")


def resolve_relative(link: Link) -> Path:
    path_part = unquote(link.target.split("#", 1)[0])
    if path_part.startswith("/"):
        return (PROJECT_ROOT / path_part.lstrip("/")).resolve()
    return (link.source.parent / path_part).resolve()


def _inside_repo(path: Path) -> bool:
    try:
        path.relative_to(PROJECT_ROOT)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Scan once per session; every test reads from the same inventory.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Inventory:
    files: tuple[Path, ...]
    links: tuple[Link, ...]


def _scan() -> Inventory:
    files = tuple(markdown_files())
    links: list[Link] = []
    for path in files:
        links.extend(extract_links(path))
    return Inventory(files=files, links=tuple(links))


INVENTORY = _scan()


def _relative_links() -> list[Link]:
    return [
        link
        for link in INVENTORY.links
        if not is_external(link.target) and not link.target.startswith("#") and link.target
    ]


def _deep_links() -> list[tuple[Link, re.Match[str]]]:
    out: list[tuple[Link, re.Match[str]]] = []
    for link in INVENTORY.links:
        match = _REPO_DEEP_LINK.match(link.target)
        if match:
            out.append((link, match))
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scan_covers_the_documentation_set() -> None:
    """A broken enumeration must fail loudly rather than pass on an empty scan."""
    names = {path.relative_to(PROJECT_ROOT).as_posix() for path in INVENTORY.files}
    assert "README.md" in names and "docs/CLI.md" in names and ".github/CI.md" in names
    assert {name for name in names if name.startswith("wiki/")} == SCANNED_EXCEPTIONS, (
        "wiki/ pages have their own guard; only the directory README is scanned here"
    )
    assert len(INVENTORY.files) >= 60, f"sanity floor: only {len(INVENTORY.files)} Markdown files"
    assert len(_relative_links()) >= 400, (
        f"sanity floor: only {len(_relative_links())} relative links"
    )
    assert len(_deep_links()) >= 1, "sanity floor: no repository deep links found"


def test_relative_links_resolve_to_paths_inside_the_repository() -> None:
    problems: list[str] = []
    for link in _relative_links():
        target = resolve_relative(link)
        if not _inside_repo(target):
            problems.append(link.describe("escapes the repository:"))
        elif not target.exists():
            problems.append(link.describe("missing path:"))
    assert not problems, "Relative Markdown links that do not resolve:\n  " + "\n  ".join(problems)


def test_anchors_resolve_to_headings() -> None:
    """Same-file ``#fragment`` links and ``.md#fragment`` cross-file links."""
    cache: dict[Path, frozenset[str]] = {}
    problems: list[str] = []
    for link in INVENTORY.links:
        if is_external(link.target) or "#" not in link.target:
            continue
        path_part, fragment = link.target.split("#", 1)
        if path_part:
            target = resolve_relative(link)
            if target.suffix != ".md" or not target.is_file():
                continue  # the path test reports missing files
        else:
            target = link.source
        anchors = cache.setdefault(target, anchors_in(target))
        if unquote(fragment) not in anchors:
            problems.append(link.describe("missing anchor:"))
    assert not problems, "Markdown links whose #fragment matches no heading:\n  " + "\n  ".join(
        problems
    )


def test_repository_deep_links_resolve_to_tracked_paths() -> None:
    cache: dict[Path, frozenset[str]] = {}
    problems: list[str] = []
    for link, match in _deep_links():
        target = (PROJECT_ROOT / unquote(match.group("path"))).resolve()
        if not _inside_repo(target) or not target.exists():
            problems.append(link.describe("missing repository path:"))
            continue
        fragment = match.group("fragment")
        if fragment and target.suffix == ".md" and target.is_file():
            anchors = cache.setdefault(target, anchors_in(target))
            if unquote(fragment) not in anchors:
                problems.append(link.describe("missing anchor:"))
    assert not problems, (
        "GitHub deep links into this repository that do not resolve:\n  " + "\n  ".join(problems)
    )


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Config File", "config-file"),
        ("`gco tasks show TASK_ID`", "gco-tasks-show-task_id"),
        ("Schedulers & Orchestrators", "schedulers--orchestrators"),
        ("What `project_name` scopes", "what-project_name-scopes"),
        ("`--allow-all-tools` / `allow_all_tools`", "--allow-all-tools--allow_all_tools"),
        ("Stack Stuck in REVIEW_IN_PROGRESS", "stack-stuck-in-review_in_progress"),
        ("**Bold** and *italic* and _emphasis_", "bold-and-italic-and-emphasis"),
        ("[Linked](https://example.com) heading", "linked-heading"),
        ("Step 1: Clone & Build (the dev container)", "step-1-clone--build-the-dev-container"),
        (
            "`pip install` fails with dependency conflicts",
            "pip-install-fails-with-dependency-conflicts",
        ),
    ],
)
def test_github_slug_follows_github_rules(heading: str, slug: str) -> None:
    assert github_slug(heading) == slug


def test_duplicate_headings_get_numeric_suffixes(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text(
        '# Example\n\ntext\n\n## Example\n\n## Example\n\n<a id="custom"></a>\n', encoding="utf-8"
    )
    assert anchors_in(page) == frozenset({"example", "example-1", "example-2", "custom"})


def test_code_and_comments_are_not_scanned(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text(
        "\n".join(
            [
                'Real: [a](real.md) and <img src="pic.png">',
                "Span: `[b](span.md)` stays out",
                "<!-- [c](comment.md) -->",
                "```",
                "[d](fenced.md)",
                "```",
                "~~~text",
                "[e](tilde.md)",
                "~~~",
                "[ref]: reference.md",
            ]
        ),
        encoding="utf-8",
    )
    targets = sorted(link.target for link in extract_links(page))
    assert targets == ["pic.png", "real.md", "reference.md"]
