"""Tests for the SVG content-policy validator (``.github/scripts/validate_svg_assets.py``).

An SVG is XML with a scripting model, and every SVG the repository tracks is
published as its own document on the Pages origin — so the validator pins what a
tracked SVG may *contain*, independent of how it was produced. Each rejection
below is one specific capability an SVG could smuggle in: script and
event-handler execution, foreign content, remote fetches, entity expansion,
namespace games, and links that leave the site. The fixtures are minimal
hand-written documents so each test exercises exactly one rule.

The allowlist and policy tables are module-level data and ``PROJECT_ROOT`` is a
module-level path, so the end-to-end path runs against a throwaway git
repository under ``tmp_path``. Two tests at the end read the real repository:
the committed allowlist must match the SVGs git sees, and every one of them must
satisfy the policy — the same run the security workflow performs.
"""

from __future__ import annotations

import importlib.util
import subprocess  # fixed argv, no shell: builds a throwaway git repo
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / ".github" / "scripts" / "validate_svg_assets.py"
_spec = importlib.util.spec_from_file_location("validate_svg_assets", _SCRIPT)
assert _spec is not None and _spec.loader is not None
validator = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("validate_svg_assets", validator)
_spec.loader.exec_module(validator)

GIT = "/usr/bin/git"
SITE = "https://example.test/gco/"
SVG_NS = validator.SVG_NAMESPACE

# ---------------------------------------------------------------------------
# Document builders
# ---------------------------------------------------------------------------


def _svg(body: str = "", *, root: str = f'xmlns="{SVG_NS}"', prologue: str = "") -> str:
    """A minimal policy-compliant document around ``body``.

    ``root`` replaces the root element's attributes; ``prologue`` is inserted
    between the XML declaration and the root element (DOCTYPE, PI, comment).
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"{prologue}"
        f'<svg {root} width="10" height="10" viewBox="0 0 10 10">\n'
        f"{body}\n"
        "</svg>\n"
    )


def _parse(text: str, *, site_url: str = SITE) -> validator._PolicyParser:
    parser = validator._PolicyParser(Path("x.svg"), site_url)
    parser.feed(text.encode("utf-8"))
    return parser


def _rejects(text: str, fragment: str) -> None:
    with pytest.raises(validator.ValidationError, match=fragment):
        _parse(text)


# ---------------------------------------------------------------------------
# What a compliant document looks like
# ---------------------------------------------------------------------------


def test_a_compliant_document_passes_and_is_counted() -> None:
    parser = _parse(
        _svg(
            '<title id="t">Title</title>\n'
            "<defs>\n"
            '  <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" '
            'markerHeight="8" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" '
            'fill="#334155"/></marker>\n'
            "</defs>\n"
            '<rect x="1" y="1" width="8" height="8" rx="2" fill="#ffffff" stroke="#000000" '
            'stroke-width="1" stroke-dasharray="6 4"/>\n'
            '<path d="M 1 1 L 9 9" stroke="#334155" marker-end="url(#arrow)"/>\n'
            f'<a href="{SITE}api/thing/"><text x="2" y="5" font-family="monospace" '
            'font-size="3" font-weight="600" text-anchor="middle">label</text></a>\n'
            '<a href="#t"><text x="2" y="8">back to the title</text></a>\n'
            "<!-- comments are inert -->\n"
            '<desc id="d">Text content, including &lt;angle brackets&gt; and colons: fine.</desc>',
            root=f'xmlns="{SVG_NS}" role="img" aria-labelledby="t d"',
        )
    )
    # svg, title, defs, marker, its path, rect, path, a, text, a, text, desc
    assert parser.element_count == 12
    assert parser.link_count == 2
    assert parser.defined_ids == {"t", "arrow", "d"}
    assert parser.referenced_ids == {"t", "d", "arrow"}


# ---------------------------------------------------------------------------
# Vocabulary: elements and attributes are allowlisted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "element",
    [
        "script",
        "foreignObject",
        "image",
        "use",
        "style",
        "animate",
        "set",
        "iframe",
        "feImage",
        "g",  # harmless, but not in today's vocabulary: widening it is a reviewed change
    ],
)
def test_elements_outside_the_allowlist_are_rejected(element: str) -> None:
    _rejects(_svg(f"<{element}/>"), rf"<{element}> is not an allowed element")


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("onload", "alert(1)"),
        ("onclick", "alert(1)"),
        ("style", "fill: url(https://evil.example/paint.svg#p)"),
        ("class", "x"),
        ("transform", "scale(1)"),  # harmless, but not in today's vocabulary
    ],
)
def test_attributes_outside_the_allowlist_are_rejected(attribute: str, value: str) -> None:
    _rejects(
        _svg(f'<rect {attribute}="{value}" width="1" height="1"/>'),
        rf"<rect {attribute}> is not an allowed attribute",
    )


def test_the_allowlists_never_admit_executable_or_fetching_vocabulary() -> None:
    """The one review a wider allowlist must survive: none of these names, ever."""
    executable_or_fetching = {
        "script",
        "handler",
        "foreignObject",
        "iframe",
        "embed",
        "object",
        "video",
        "audio",
        "image",
        "use",
        "style",
        "animate",
        "animateMotion",
        "animateTransform",
        "set",
        "feImage",
        "cursor",
        "font-face-uri",
    }
    assert not validator.ALLOWED_ELEMENTS & executable_or_fetching
    assert not {name for name in validator.ALLOWED_ATTRIBUTES if name.startswith("on")}
    assert not validator.ALLOWED_ATTRIBUTES & {"style", "class", "xlink:href", "src"}


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------


def test_xlink_namespace_is_rejected_before_its_href_is_seen() -> None:
    _rejects(
        _svg(
            '<a xlink:href="https://evil.example/"><text x="1" y="1">go</text></a>',
            root=f'xmlns="{SVG_NS}" xmlns:xlink="http://www.w3.org/1999/xlink"',
        ),
        r"namespace 'http://www.w3.org/1999/xlink' \(prefix 'xlink'\) is not allowed",
    )


def test_a_foreign_default_namespace_is_rejected() -> None:
    _rejects(
        _svg(root='xmlns="http://www.w3.org/1999/xhtml"'),
        r"namespace 'http://www.w3.org/1999/xhtml' \(prefix None\) is not allowed",
    )


def test_a_document_without_the_svg_namespace_is_rejected() -> None:
    _rejects(_svg(root='role="img"'), r"<svg> is not in the SVG namespace")


def test_the_implicit_xml_namespace_is_rejected_on_attributes() -> None:
    """``xml:`` needs no declaration, so the namespace handler never sees it."""
    _rejects(
        _svg('<text xml:space="preserve" x="1" y="1"> spaced </text>'),
        r"<text> attribute 'space' in namespace 'http://www.w3.org/XML/1998/namespace' "
        r"is not allowed",
    )


def test_the_root_element_must_be_svg() -> None:
    _rejects(
        f'<a xmlns="{SVG_NS}" href="{SITE}"><text x="1" y="1">x</text></a>',
        r"the root element must be <svg>, not <a>",
    )


# ---------------------------------------------------------------------------
# Prologue: DOCTYPE, processing instructions, CDATA, entities, well-formedness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prologue",
    [
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
        '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n',
        '<!DOCTYPE svg [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>\n',
        '<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>\n',
    ],
)
def test_any_doctype_is_rejected_before_entities_can_be_declared(prologue: str) -> None:
    _rejects(_svg(prologue=prologue), r"a DOCTYPE declaration is not allowed")


def test_processing_instructions_are_rejected() -> None:
    _rejects(
        _svg(prologue='<?xml-stylesheet href="https://evil.example/x.css" type="text/css"?>\n'),
        r"a processing instruction is not allowed",
    )


def test_cdata_sections_are_rejected() -> None:
    _rejects(
        _svg('<text x="1" y="1"><![CDATA[ alert(1) ]]></text>'),
        r"a CDATA section is not allowed",
    )


def test_an_undeclared_entity_reference_is_not_well_formed() -> None:
    _rejects(_svg('<text x="1" y="1">&evil;</text>'), r"not well-formed XML \(undefined entity")


def test_malformed_xml_is_rejected() -> None:
    _rejects(_svg("<rect"), r"not well-formed XML")


# ---------------------------------------------------------------------------
# References: href, url(#id), aria-labelledby, ids
# ---------------------------------------------------------------------------


def test_href_is_only_allowed_on_anchors() -> None:
    _rejects(
        _svg(f'<rect href="{SITE}" width="1" height="1"/>'),
        r"href is only allowed on <a>, not on <rect>",
    )


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "data:text/html,&lt;script&gt;alert(1)&lt;/script&gt;",
        "https://evil.example/",
        "http://example.test/gco/api/",  # the site, but not over https
        "//example.test/gco/api/",
        "https://example.test/other-site/",
        "api/relative/",
        "",
    ],
)
def test_anchor_targets_outside_the_site_are_rejected(href: str) -> None:
    _rejects(
        _svg(f'<a href="{href}"><text x="1" y="1">go</text></a>'),
        r"must be a same-document #id or a page under https://example\.test/gco/",
    )


def test_anchor_targets_under_the_site_are_links() -> None:
    parser = _parse(_svg(f'<a href="{SITE}api/cluster-gateway/"><text x="1" y="1">go</text></a>'))
    assert parser.link_count == 1


def test_a_same_document_anchor_must_point_at_a_defined_id() -> None:
    _rejects(
        _svg('<a href="#nowhere"><text x="1" y="1">go</text></a>'),
        r"references to ids the file does not define: nowhere",
    )


@pytest.mark.parametrize(
    "value",
    [
        "url(https://evil.example/m.svg#arrow)",
        "url(#arrow) url(https://evil.example/)",
        "url(m.svg#arrow)",
        "url(#)",
    ],
)
def test_url_references_must_be_same_document(value: str) -> None:
    _rejects(
        _svg(f'<path d="M 0 0" marker-end="{value}"/>'),
        r"must be a same-document url\(#id\) reference",
    )


def test_a_paint_reference_to_an_undefined_id_is_rejected() -> None:
    _rejects(
        _svg('<rect fill="url(#gradient)" width="1" height="1"/>'),
        r"references to ids the file does not define: gradient",
    )


def test_aria_labelledby_tokens_must_be_defined_ids() -> None:
    _rejects(
        _svg('<title id="t">x</title>', root=f'xmlns="{SVG_NS}" aria-labelledby="t missing"'),
        r"references to ids the file does not define: missing",
    )


def test_duplicate_ids_are_rejected() -> None:
    _rejects(
        _svg('<title id="t">a</title><desc id="t">b</desc>'),
        r"duplicate id 't'",
    )


# ---------------------------------------------------------------------------
# File-level checks
# ---------------------------------------------------------------------------


def _point_at(monkeypatch: pytest.MonkeyPatch, root: Path, policies: dict) -> None:
    monkeypatch.setattr(validator, "PROJECT_ROOT", root)
    monkeypatch.setattr(validator, "SVG_POLICIES", policies)


def test_validate_svg_reports_size_elements_and_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _svg(f'<a href="{SITE}api/x/"><text x="1" y="1">x</text></a>')
    (tmp_path / "ok.svg").write_text(text, encoding="utf-8")
    monkeypatch.setattr(validator, "PROJECT_ROOT", tmp_path)

    result = validator._validate_svg(Path("ok.svg"), validator.SvgPolicy(1 * validator.KIB), SITE)

    assert result == (len(text.encode("utf-8")), 3, 1)


def test_validate_svg_rejects_a_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "real.svg").write_text(_svg(), encoding="utf-8")
    (tmp_path / "link.svg").symlink_to(tmp_path / "real.svg")
    monkeypatch.setattr(validator, "PROJECT_ROOT", tmp_path)

    with pytest.raises(validator.ValidationError, match=r"link\.svg: expected a regular file"):
        validator._validate_svg(Path("link.svg"), validator.SvgPolicy(1 * validator.KIB), SITE)


def test_validate_svg_rejects_an_oversized_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "big.svg").write_text(_svg("<!-- " + "x" * 200 + " -->"), encoding="utf-8")
    monkeypatch.setattr(validator, "PROJECT_ROOT", tmp_path)

    with pytest.raises(validator.ValidationError, match=r"bytes exceeds 100-byte limit"):
        validator._validate_svg(Path("big.svg"), validator.SvgPolicy(100), SITE)


# ---------------------------------------------------------------------------
# Inventory against a throwaway repository
# ---------------------------------------------------------------------------


def _fake_repo(tmp_path: Path, tracked: dict[str, str], *, site_url: str | None = SITE) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for relative, payload in tracked.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
    if site_url is not None:
        (root / "mkdocs.yml").write_text(f"site_name: t\nsite_url: {site_url}\n", encoding="utf-8")
    for argv in (
        [GIT, "init", "-q", "."],
        [GIT, "config", "user.email", "t@example.com"],
        [GIT, "config", "user.name", "t"],
        [GIT, "add", "-A"],
    ):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)
    return root


def test_tracked_svgs_sees_tracked_and_untracked_but_not_ignored_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_repo(
        tmp_path,
        {"d/a.svg": _svg(), "d/B.SVG": _svg(), "docs/x.md": "#", ".gitignore": "build/\n"},
    )
    (root / "d" / "fresh.svg").write_text(_svg(), encoding="utf-8")  # untracked, not ignored
    (root / "build").mkdir()
    (root / "build" / "ignored.svg").write_text(_svg(), encoding="utf-8")
    monkeypatch.setattr(validator, "PROJECT_ROOT", root)

    assert validator._tracked_svgs() == {Path("d/a.svg"), Path("d/B.SVG"), Path("d/fresh.svg")}


def test_allowlist_passes_when_the_tree_and_the_policy_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_repo(tmp_path, {"d/a.svg": _svg()})
    _point_at(monkeypatch, root, {Path("d/a.svg"): validator.SvgPolicy(1 * validator.KIB)})

    validator._validate_allowlist()


@pytest.mark.parametrize(
    ("tracked", "allowlisted", "message"),
    [
        (
            ["d/new.svg"],
            ["d/reviewed.svg"],
            r"SVG allowlist mismatch \(missing: d/reviewed\.svg; not allowlisted: d/new\.svg\)",
        ),
        ([], ["d/reviewed.svg"], r"SVG allowlist mismatch \(missing: d/reviewed\.svg\)"),
        (["d/new.svg"], [], r"SVG allowlist mismatch \(not allowlisted: d/new\.svg\)"),
    ],
)
def test_allowlist_names_every_unreviewed_and_every_vanished_svg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracked: list[str],
    allowlisted: list[str],
    message: str,
) -> None:
    """A new SVG nobody reviewed and a deleted one both need a human."""
    root = _fake_repo(tmp_path, {path: _svg() for path in tracked})
    _point_at(
        monkeypatch,
        root,
        {Path(path): validator.SvgPolicy(1 * validator.KIB) for path in allowlisted},
    )

    with pytest.raises(validator.ValidationError, match=message):
        validator._validate_allowlist()


# ---------------------------------------------------------------------------
# site_url
# ---------------------------------------------------------------------------


def test_site_url_is_read_from_mkdocs_and_normalised_to_a_trailing_slash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_repo(tmp_path, {}, site_url="https://example.test/gco")
    monkeypatch.setattr(validator, "PROJECT_ROOT", root)

    assert validator._site_url() == "https://example.test/gco/"


@pytest.mark.parametrize(
    "mkdocs",
    [
        "site_name: t\n",  # no site_url
        "site_name: t\nsite_url: http://example.test/\n",  # not https
        "site_name: t\nsite_url: 42\n",  # not a string
        "- just\n- a list\n",  # not a mapping
    ],
)
def test_site_url_must_be_an_https_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mkdocs: str
) -> None:
    (tmp_path / "mkdocs.yml").write_text(mkdocs, encoding="utf-8")
    monkeypatch.setattr(validator, "PROJECT_ROOT", tmp_path)

    with pytest.raises(validator.ValidationError, match=r"site_url must be declared as an https"):
        validator._site_url()


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_passes_and_reports_each_svg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _svg(f'<a href="{SITE}api/x/"><text x="1" y="1">x</text></a>')
    root = _fake_repo(tmp_path, {"d/a.svg": text})
    _point_at(monkeypatch, root, {Path("d/a.svg"): validator.SvgPolicy(1 * validator.KIB)})

    assert validator.main() == 0

    captured = capsys.readouterr()
    assert (
        captured.out == f"PASS d/a.svg: {len(text.encode('utf-8')):,} bytes, 3 elements, 1 links\n"
    )
    assert captured.err == ""


def test_main_fails_closed_on_a_policy_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fake_repo(tmp_path, {"d/a.svg": _svg("<script>alert(1)</script>")})
    _point_at(monkeypatch, root, {Path("d/a.svg"): validator.SvgPolicy(1 * validator.KIB)})

    assert validator.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("SVG validation failed: d/a.svg: <script> is not an allowed element\n")


def test_main_fails_closed_when_mkdocs_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An OSError (here: no mkdocs.yml) is a failure, not a crash."""
    root = _fake_repo(tmp_path, {"d/a.svg": _svg()}, site_url=None)
    _point_at(monkeypatch, root, {Path("d/a.svg"): validator.SvgPolicy(1 * validator.KIB)})

    assert validator.main() == 1
    assert "SVG validation failed: " in capsys.readouterr().err


def test_main_fails_closed_on_unparseable_mkdocs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fake_repo(tmp_path, {"d/a.svg": _svg()}, site_url=None)
    (root / "mkdocs.yml").write_text("site_url: [unclosed\n", encoding="utf-8")
    _point_at(monkeypatch, root, {Path("d/a.svg"): validator.SvgPolicy(1 * validator.KIB)})

    assert validator.main() == 1
    assert "SVG validation failed: " in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The real repository
# ---------------------------------------------------------------------------


def test_the_committed_allowlist_matches_the_svgs_git_sees() -> None:
    """The real inventory must agree with the real policy table."""
    validator._validate_allowlist()


def test_every_repository_svg_satisfies_the_policy(capsys: pytest.CaptureFixture[str]) -> None:
    """The same run ``security:bandit:sast`` performs, on the committed assets."""
    assert validator.main() == 0
    out = capsys.readouterr().out
    assert out.startswith("PASS diagrams/api_specs/api-topology.svg: ")
    assert out.count("PASS ") == len(validator.SVG_POLICIES)


def test_the_interaction_diagram_links_every_spec_sheet_and_nothing_else() -> None:
    """The one SVG with links: seven, all under the documented site, one per document."""
    site_url = validator._site_url()
    parser = validator._PolicyParser(Path("api-topology.svg"), site_url)
    parser.feed((REPO_ROOT / "diagrams" / "api_specs" / "api-topology.svg").read_bytes())
    assert parser.link_count == len(list((REPO_ROOT / "docs" / "openapi").glob("*.json")))
    assert parser.defined_ids >= {"title", "desc", "arrow", "arrow-fanout"}


def test_the_security_workflow_runs_the_validator() -> None:
    """The policy is only supply-chain CI if the security job actually runs it."""
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["security-bandit-sast"]["steps"]
    commands = [step.get("run", "") for step in steps if isinstance(step, dict)]
    assert any(
        "python .github/scripts/validate_svg_assets.py" in command for command in commands
    ), "security:bandit:sast no longer runs .github/scripts/validate_svg_assets.py"
