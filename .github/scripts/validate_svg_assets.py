#!/usr/bin/env python3
"""Validate the tracked SVG assets against the reviewed content policy.

SVG is XML with a scripting model. A file can carry ``<script>``, ``on*`` event
handlers, ``<foreignObject>`` (arbitrary HTML), and references that make the
renderer fetch remote resources (``<image>``, ``<use>``, ``xlink:href``, CSS
``url()``). GitHub and MkDocs embed the repository's SVGs as ``<img>``, where
none of that runs — but each SVG is also published as its own document on the
Pages origin (``scripts/mkdocs_hooks.py`` copies ``diagrams/api_specs/*.svg`` to
``/api/``), and the interaction diagram's links only work when it is opened that
way. Opened as a document, an SVG runs whatever it carries, on an origin every
project site in the GitHub organisation shares.

``diagrams/api_specs/generate.py --check`` proves the committed bytes are what
the renderer produces; it cannot say whether what the renderer produces is safe,
and it says nothing about an SVG someone exports from a drawing tool. This
validator pins the content itself, independent of how the file was made:

* an explicit allowlist of SVG paths with a byte ceiling each — a new SVG fails
  until a reviewer adds it here, exactly like the demo GIFs;
* an element and attribute *allowlist* (drawing primitives, text, ``<a>``)
  rather than a denylist of known-dangerous names;
* no DOCTYPE, processing instructions or CDATA sections (entity expansion,
  external entities, ``<?xml-stylesheet?>``);
* the default SVG namespace only — no ``xlink`` or other foreign namespaces;
* every ``href`` is a same-document ``#id`` or a page under the ``site_url``
  declared in ``mkdocs.yml``; every ``url(#id)`` paint or marker reference and
  every ``aria-labelledby`` token resolves to an ``id`` the file defines.

Parsing uses the stdlib expat parser directly, with handlers that reject the
DOCTYPE before any entity could be declared, so the amplification and external
entity vectors are closed by policy rather than left to parser defaults.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# The semgrep rule suppressed below warns about entity expansion and external
# entities. _PolicyParser installs a StartDoctypeDeclHandler that raises, so no
# DTD — and therefore no entity declaration of either kind — is ever accepted,
# and the input is a tracked repository file rather than network data.
# defusedxml would add a dependency to guard a vector this policy already
# refuses.
from xml.parsers import expat  # nosemgrep: python.lang.security.use-defused-xml.use-defused-xml

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KIB = 1024

SVG_NAMESPACE = "http://www.w3.org/2000/svg"


@dataclass(frozen=True)
class SvgPolicy:
    """Maximum accepted size for one intentionally tracked SVG."""

    max_bytes: int


# Any new SVG, or an intentional increase, requires a review of this list.
SVG_POLICIES = {
    # The API interaction diagram, ~25 KiB today. It grows with the route
    # inventory — one text line per new route prefix — not with rendering.
    Path("diagrams/api_specs/api-topology.svg"): SvgPolicy(96 * KIB),
}

# Drawing primitives, text and links: what the interaction diagram is drawn
# from, and nothing that executes, embeds foreign content or fetches. Widening
# this set is a reviewed change; ``tests/test_validate_svg_assets.py`` refuses
# the names that would reintroduce any of those capabilities.
ALLOWED_ELEMENTS = frozenset(
    {"svg", "defs", "marker", "path", "rect", "text", "title", "desc", "a"}
)

# Geometry and presentation only. No ``style`` (CSS ``url()``/``@import``), no
# ``on*`` handlers, no ``class`` (nothing to select it). ``href`` and every
# ``url(…)`` value are checked individually below.
ALLOWED_ATTRIBUTES = frozenset(
    {
        "aria-labelledby",
        "d",
        "fill",
        "font-family",
        "font-size",
        "font-weight",
        "height",
        "href",
        "id",
        "marker-end",
        "markerHeight",
        "markerWidth",
        "orient",
        "refX",
        "refY",
        "role",
        "rx",
        "stroke",
        "stroke-dasharray",
        "stroke-width",
        "text-anchor",
        "viewBox",
        "width",
        "x",
        "y",
    }
)

_LOCAL_URL_RE = re.compile(r"^url\(#([A-Za-z_][\w.-]*)\)$")


class ValidationError(ValueError):
    """A tracked SVG violates the reviewed content policy."""


class _PolicyParser:
    """expat handlers that reject anything outside the policy as it is parsed."""

    def __init__(self, relative_path: Path, site_url: str) -> None:
        self.relative_path = relative_path
        self.site_url = site_url
        self.element_count = 0
        self.link_count = 0
        self.defined_ids: set[str] = set()
        self.referenced_ids: set[str] = set()
        self._depth = 0
        # ``namespace_separator`` makes every element and attribute name arrive
        # as ``"<namespace URI> <local name>"`` when it is namespaced, so a
        # foreign namespace cannot hide behind a familiar-looking prefix.
        parser = expat.ParserCreate(namespace_separator=" ")
        parser.StartElementHandler = self._start_element
        parser.EndElementHandler = self._end_element
        parser.StartNamespaceDeclHandler = self._start_namespace
        # Entities can only be declared inside a DOCTYPE, so refusing the
        # DOCTYPE itself closes entity expansion and external entities in one
        # place; ``<?xml-stylesheet?>`` is the processing instruction that
        # would load a stylesheet, and CDATA is how script bodies are wrapped.
        parser.StartDoctypeDeclHandler = self._reject("a DOCTYPE declaration")
        parser.ProcessingInstructionHandler = self._reject("a processing instruction")
        parser.StartCdataSectionHandler = self._reject("a CDATA section")
        self._parser = parser

    def _reject(self, what: str) -> Callable[..., None]:
        def handler(*_args: object) -> None:
            raise ValidationError(f"{self.relative_path}: {what} is not allowed")

        return handler

    def _start_namespace(self, prefix: str | None, uri: str | None) -> None:
        if prefix is not None or uri != SVG_NAMESPACE:
            raise ValidationError(
                f"{self.relative_path}: namespace {uri!r} (prefix {prefix!r}) is not "
                f"allowed; only the default {SVG_NAMESPACE} namespace is"
            )

    def _start_element(self, name: str, attributes: dict[str, str]) -> None:
        namespace, _, local = name.rpartition(" ")
        if namespace != SVG_NAMESPACE:
            raise ValidationError(f"{self.relative_path}: <{local}> is not in the SVG namespace")
        if local not in ALLOWED_ELEMENTS:
            raise ValidationError(f"{self.relative_path}: <{local}> is not an allowed element")
        if self._depth == 0 and local != "svg":
            raise ValidationError(
                f"{self.relative_path}: the root element must be <svg>, not <{local}>"
            )
        self._depth += 1
        self.element_count += 1
        for attribute, value in attributes.items():
            self._check_attribute(local, attribute, value)

    def _end_element(self, _name: str) -> None:
        self._depth -= 1

    def _check_attribute(self, element: str, attribute: str, value: str) -> None:
        if " " in attribute:
            # The namespace handler has already refused every declared foreign
            # namespace; this catches the implicit ``xml:`` one (``xml:space``,
            # ``xml:base`` — the latter rebases every relative reference).
            namespace, _, local = attribute.rpartition(" ")
            raise ValidationError(
                f"{self.relative_path}: <{element}> attribute {local!r} in namespace "
                f"{namespace!r} is not allowed"
            )
        if attribute not in ALLOWED_ATTRIBUTES:
            raise ValidationError(
                f"{self.relative_path}: <{element} {attribute}> is not an allowed attribute"
            )
        if attribute == "id":
            if value in self.defined_ids:
                raise ValidationError(f"{self.relative_path}: duplicate id {value!r}")
            self.defined_ids.add(value)
        elif attribute == "href":
            self._check_href(element, value)
        elif attribute == "aria-labelledby":
            self.referenced_ids.update(value.split())
        elif "url(" in value:
            match = _LOCAL_URL_RE.fullmatch(value)
            if match is None:
                raise ValidationError(
                    f"{self.relative_path}: <{element} {attribute}={value!r}> must be a "
                    "same-document url(#id) reference"
                )
            self.referenced_ids.add(match.group(1))

    def _check_href(self, element: str, value: str) -> None:
        if element != "a":
            raise ValidationError(
                f"{self.relative_path}: href is only allowed on <a>, not on <{element}>"
            )
        if value.startswith("#"):
            self.referenced_ids.add(value[1:])
        elif not value.startswith(self.site_url):
            raise ValidationError(
                f"{self.relative_path}: <a href={value!r}> must be a same-document #id or a "
                f"page under {self.site_url}"
            )
        self.link_count += 1

    def feed(self, data: bytes) -> None:
        """Parse the whole document, then resolve every same-document reference."""
        try:
            self._parser.Parse(data, True)
        except expat.ExpatError as exc:
            raise ValidationError(f"{self.relative_path}: not well-formed XML ({exc})") from exc
        dangling = sorted(self.referenced_ids - self.defined_ids)
        if dangling:
            raise ValidationError(
                f"{self.relative_path}: references to ids the file does not define: "
                + ", ".join(dangling)
            )


def _tracked_svgs() -> set[Path]:
    """Every ``*.svg`` git sees: tracked, plus untracked files it does not ignore.

    Including untracked files means a freshly generated diagram is validated
    before it is committed, and a stray export dropped into the tree fails
    locally rather than on the PR. CI checkouts are clean, so there the two
    sets coincide.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    return {
        Path(os.fsdecode(raw_path))
        for raw_path in result.stdout.split(b"\0")
        if raw_path and Path(os.fsdecode(raw_path)).suffix.lower() == ".svg"
    }


def _validate_allowlist() -> None:
    tracked = _tracked_svgs()
    expected = set(SVG_POLICIES)
    missing = sorted(expected - tracked)
    unexpected = sorted(tracked - expected)
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing: " + ", ".join(map(str, missing)))
        if unexpected:
            details.append("not allowlisted: " + ", ".join(map(str, unexpected)))
        raise ValidationError("SVG allowlist mismatch (" + "; ".join(details) + ")")


def _site_url() -> str:
    """The published site's URL prefix, from the one place it is declared."""
    config = yaml.safe_load((PROJECT_ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    site_url = config.get("site_url") if isinstance(config, dict) else None
    if not isinstance(site_url, str) or not site_url.startswith("https://"):
        raise ValidationError("mkdocs.yml: site_url must be declared as an https:// URL")
    return site_url.rstrip("/") + "/"


def _validate_svg(relative_path: Path, policy: SvgPolicy, site_url: str) -> tuple[int, int, int]:
    path = PROJECT_ROOT / relative_path
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"{relative_path}: expected a regular file")

    file_size = path.stat().st_size
    if file_size > policy.max_bytes:
        raise ValidationError(
            f"{relative_path}: {file_size:,} bytes exceeds {policy.max_bytes:,}-byte limit"
        )

    parser = _PolicyParser(relative_path, site_url)
    parser.feed(path.read_bytes())
    return file_size, parser.element_count, parser.link_count


def main() -> int:
    try:
        _validate_allowlist()
        site_url = _site_url()
        for relative_path, policy in SVG_POLICIES.items():
            file_size, element_count, link_count = _validate_svg(relative_path, policy, site_url)
            print(
                f"PASS {relative_path}: {file_size:,} bytes, "
                f"{element_count} elements, {link_count} links"
            )
    except (OSError, subprocess.SubprocessError, ValidationError, yaml.YAMLError) as exc:
        print(f"SVG validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
