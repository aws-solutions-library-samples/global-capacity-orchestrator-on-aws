#!/usr/bin/env python3
"""Regenerate or structurally verify every committed diagram catalogue."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from diagrams.code_diagrams._renderer import _output_stem_for  # noqa: E402
from diagrams.code_diagrams._source_marker import SENTINEL  # noqa: E402
from diagrams.code_diagrams._targets import TARGETS  # noqa: E402
from diagrams.code_diagrams.generate import (  # noqa: E402
    REGENERATION_HINT,
    newest_provenance_stamp,
    verify_targets_match_provenance_manifest,
)
from diagrams.infra_diagrams._catalog import INFRA_DIAGRAM_NAMES  # noqa: E402
from gco.lambda_shared_sources import LAMBDA_SHARED_SOURCE_TARGETS  # noqa: E402

_TIMESTAMP_RE = re.compile(r"Generated at \(UTC\):[^\n]*?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")
_SOURCE_COMMIT_RE = re.compile(r"Generated from Git commit:[^\n]*?([0-9a-f]{40})")
_HTML_SOURCE_COMMIT_RE = re.compile(r'<meta name="gco-source-commit" content="([0-9a-f]{40})">')
_FLOW_DIGEST_RE = re.compile(r'<meta name="gco-flow-digest" content="([0-9a-f]{16})">')
_MARKER_BLOCK_RE = re.compile(
    rf"(?s)# <{re.escape(SENTINEL)}> BEGIN[^\n]*\n(.*?)# <{re.escape(SENTINEL)}> END"
)
_MARKER_ENTRY_RE = re.compile(
    r"#   \* ``([^`]+)`` -> ``([^`]+\.html)``\n"
    r"(?:#     \(PNG: ``([^`]+\.png)``\)\n)?"
)
_MARKER_BODY_RE = re.compile(
    r"# Generated at \(UTC\): \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\n"
    r"# Generated from Git commit: [0-9a-f]{40}\n"
    r"# Flowchart\(s\) generated from this file:\n"
    r"(?:#   \* ``[^`]+`` -> ``[^`]+\.html``\n"
    r"#     \(PNG: ``[^`]+\.png``\)\n)+"
    r"# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> "
    r"GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> "
    r"python diagrams/generate\.py --code-only``\.\n?\Z"
)
MarkerPointer = tuple[str, str, str | None]


def _shared_source_copy_issues(project_root: Path, target_sources: set[str]) -> list[str]:
    """Require every checked-in shared Lambda copy to equal its target source."""
    issues: list[str] = []
    for source, copies in LAMBDA_SHARED_SOURCE_TARGETS.items():
        if source not in target_sources:
            continue
        canonical_path = project_root / source
        if not canonical_path.is_file():
            issues.append(f"missing canonical shared source: {source}")
            continue
        canonical = canonical_path.read_bytes()
        for copy in copies:
            copy_path = project_root / copy
            if not copy_path.is_file():
                issues.append(f"missing shared source copy: {copy}")
            elif copy_path.read_bytes() != canonical:
                issues.append(f"shared source copy drifted: {copy} != {source}")
    return issues


def _marker_pointer_issues(
    source: str,
    expected: set[MarkerPointer],
    source_name: str,
) -> list[str]:
    """Compare a source marker's exact generated grammar and pointers."""
    begin = f"# <{SENTINEL}> BEGIN"
    end = f"# <{SENTINEL}> END"
    if source.count(SENTINEL) != 2 or source.count(begin) != 1 or source.count(end) != 1:
        return [f"source marker delimiter count invalid: {source_name}"]
    blocks = _MARKER_BLOCK_RE.findall(source)
    if len(blocks) != 1:
        return [f"source marker block count invalid: {source_name}"]

    issues: list[str] = []
    if _MARKER_BODY_RE.fullmatch(blocks[0]) is None:
        issues.append(f"source marker grammar invalid: {source_name}")
    matches = _MARKER_ENTRY_RE.findall(blocks[0])
    actual: set[MarkerPointer] = {
        (function, html_path, png_path or None) for function, html_path, png_path in matches
    }
    if len(matches) != len(actual):
        issues.append(f"source marker contains duplicate pointers: {source_name}")
    if actual != expected:
        issues.append(
            f"source marker pointers drifted: {source_name}: "
            f"missing={sorted(expected - actual)!r}, stale={sorted(actual - expected)!r}"
        )
    return issues


def _code_artifact_contract(project_root: Path) -> list[str]:
    output_dir = project_root / "diagrams" / "code_diagrams"
    expected_html: set[Path] = set()
    expected_png: set[Path] = set()
    for target in TARGETS:
        stem = _output_stem_for(target, output_dir=output_dir)
        expected_html.add(stem.parent / f"{stem.name}.html")
        expected_png.add(stem.parent / f"{stem.name}.png")

    actual_html = set(output_dir.rglob("*.html"))
    actual_png = set(output_dir.rglob("*.png"))
    issues = [
        *(
            f"missing code artifact: {path.relative_to(project_root)}"
            for path in sorted(expected_html - actual_html)
        ),
        *(
            f"missing code artifact: {path.relative_to(project_root)}"
            for path in sorted(expected_png - actual_png)
        ),
        *(
            f"orphan code artifact: {path.relative_to(project_root)}"
            for path in sorted(actual_html - expected_html)
        ),
        *(
            f"orphan code artifact: {path.relative_to(project_root)}"
            for path in sorted(actual_png - expected_png)
        ),
    ]

    # Freshness is verified against the committed digest manifest, not Git
    # history: squash merges delete branch commits, so a recorded SHA is
    # provenance metadata rather than a resolvable object. The manifest also
    # carries each source's own stamp, which is what lets one PR restamp only
    # the sources it changed.
    manifest: dict[str, dict[str, str]] = {}
    try:
        manifest = verify_targets_match_provenance_manifest(
            project_root=project_root,
            targets=TARGETS,
        )
    except RuntimeError as exc:
        issues.append(str(exc))

    readme = (output_dir / "README.md").read_text(encoding="utf-8")
    readme_timestamps = set(_TIMESTAMP_RE.findall(readme))
    readme_source_commits = set(_SOURCE_COMMIT_RE.findall(readme))
    if len(readme_timestamps) != 1:
        issues.append(f"code index timestamp invalid: {sorted(readme_timestamps)}")
    if len(readme_source_commits) != 1:
        issues.append(f"code index source commit invalid: {sorted(readme_source_commits)}")
    if manifest and len(readme_timestamps) == 1 and len(readme_source_commits) == 1:
        newest_at, newest_commit = newest_provenance_stamp(manifest)
        if (next(iter(readme_timestamps)), next(iter(readme_source_commits))) != (
            newest_at,
            newest_commit,
        ):
            issues.append(
                "diagrams/code_diagrams/README.md: its header stamp must match the "
                f"newest provenance entry ({newest_at} / {newest_commit}) — rerun the "
                f"generator to rewrite the index ({REGENERATION_HINT})"
            )
    expected_index_links: set[str] = set()
    checked_sources: set[str] = set()
    html_source: dict[Path, str] = {}
    for target in TARGETS:
        stem = _output_stem_for(target, output_dir=output_dir)
        for suffix in ("html", "png"):
            path = stem.parent / f"{stem.name}.{suffix}"
            if suffix == "html":
                html_source[path] = target.source
            relative = path.relative_to(output_dir).as_posix()
            expected_index_links.add(relative)
            if f"./{relative}" not in readme:
                issues.append(f"code index omitted: {relative}")
        source = (project_root / target.source).read_text(encoding="utf-8")
        if target.source not in checked_sources:
            checked_sources.add(target.source)
            expected_pointers: set[MarkerPointer] = set()
            for source_target in TARGETS:
                if source_target.source != target.source:
                    continue
                source_stem = _output_stem_for(source_target, output_dir=output_dir)
                html_path = source_stem.parent / f"{source_stem.name}.html"
                png_path = source_stem.parent / f"{source_stem.name}.png"
                expected_pointers.add(
                    (
                        source_target.function,
                        html_path.relative_to(project_root).as_posix(),
                        png_path.relative_to(project_root).as_posix(),
                    )
                )
            issues.extend(_marker_pointer_issues(source, expected_pointers, target.source))
            source_timestamps = set(_TIMESTAMP_RE.findall(source))
            marker_source_commits = set(_SOURCE_COMMIT_RE.findall(source))
            if len(source_timestamps) != 1:
                issues.append(
                    f"source marker timestamp invalid: {target.source}: {sorted(source_timestamps)}"
                )
            if len(marker_source_commits) != 1:
                issues.append(
                    f"source marker commit invalid: {target.source}: "
                    f"{sorted(marker_source_commits)}"
                )
            # Each source's marker must agree with that source's own recorded
            # provenance — not with the rest of the catalogue.
            if (
                manifest
                and len(source_timestamps) == 1
                and len(marker_source_commits) == 1
                and target.source in manifest
            ):
                entry = manifest[target.source]
                if (next(iter(source_timestamps)), next(iter(marker_source_commits))) != (
                    entry["generated_at"],
                    entry["source_commit"],
                ):
                    issues.append(
                        f"{target.source}: its marker block's stamp disagrees with the "
                        "provenance recorded for it — regenerate this source's diagrams "
                        f"({REGENERATION_HINT})"
                    )
        if f"``{target.function}``" not in source:
            issues.append(f"source marker omitted: {target.source}:{target.function}")

    indexed_artifacts = set(re.findall(r"\]\(\./([^)]+\.(?:html|png))\)", readme))
    for relative in sorted(indexed_artifacts - expected_index_links):
        issues.append(f"orphan code index entry: {relative}")

    for html in expected_html & actual_html:
        html_text = html.read_text(encoding="utf-8")
        html_timestamps = set(_TIMESTAMP_RE.findall(html_text))
        if len(html_timestamps) != 1:
            issues.append(
                f"code artifact timestamp invalid: {html.relative_to(project_root)}: "
                f"{sorted(html_timestamps)}"
            )
        html_source_commits = set(_HTML_SOURCE_COMMIT_RE.findall(html_text))
        if len(html_source_commits) != 1:
            issues.append(
                f"code artifact source commit invalid: {html.relative_to(project_root)}: "
                f"{sorted(html_source_commits)}"
            )
        elif f"<code>{next(iter(html_source_commits))}</code>" not in html_text:
            issues.append(
                f"code artifact visible source commit omitted: {html.relative_to(project_root)}"
            )
        # An artifact must carry its own source's stamp; sibling artifacts
        # derived from other sources are free to be a different vintage.
        owner = html_source.get(html)
        if (
            manifest
            and owner in manifest
            and len(html_timestamps) == 1
            and len(html_source_commits) == 1
        ):
            entry = manifest[owner]
            if (next(iter(html_timestamps)), next(iter(html_source_commits))) != (
                entry["generated_at"],
                entry["source_commit"],
            ):
                issues.append(
                    f"{html.relative_to(project_root)}: this artifact's stamp disagrees "
                    f"with the provenance recorded for {owner} — regenerate that "
                    f"source's diagrams ({REGENERATION_HINT})"
                )
        flow_digests = set(_FLOW_DIGEST_RE.findall(html_text))
        if len(flow_digests) != 1:
            issues.append(
                f"code artifact flow digest invalid: {html.relative_to(project_root)}: "
                f"{sorted(flow_digests)}"
            )
        elif f"<code>{next(iter(flow_digests))}</code>" not in html_text:
            issues.append(
                f"code artifact visible flow digest omitted: {html.relative_to(project_root)}"
            )

    allowed_marker_sources = {target.source for target in TARGETS}
    issues.extend(_shared_source_copy_issues(project_root, allowed_marker_sources))
    for source, copies in LAMBDA_SHARED_SOURCE_TARGETS.items():
        if source in allowed_marker_sources:
            allowed_marker_sources.update(copies)
    marker_roots = [
        project_root / "app.py",
        project_root / "cli",
        project_root / "gco",
        project_root / "gco_mcp",
        project_root / "lambda",
    ]
    for marker_root in marker_roots:
        paths = [marker_root] if marker_root.is_file() else marker_root.rglob("*.py")
        for path in paths:
            if not path.is_file() or "-build" in path.as_posix():
                continue
            if SENTINEL not in path.read_text(encoding="utf-8"):
                continue
            relative = path.relative_to(project_root).as_posix()
            if relative not in allowed_marker_sources:
                issues.append(f"retired source marker: {relative}")
    return issues


def _infra_artifact_contract(project_root: Path) -> list[str]:
    output_dir = project_root / "diagrams" / "infra_diagrams"
    expected = {output_dir / f"{name}.png" for name in INFRA_DIAGRAM_NAMES}
    actual = set(output_dir.glob("*.png"))
    issues = [
        *(f"missing infrastructure artifact: {path.name}" for path in sorted(expected - actual)),
        *(f"orphan infrastructure artifact: {path.name}" for path in sorted(actual - expected)),
    ]
    issues.extend(
        f"transient Graphviz sidecar: {path.name}" for path in sorted(output_dir.glob("*.dot"))
    )
    return issues


def check_diagram_contract(
    project_root: Path = ROOT,
    *,
    code: bool = True,
    infra: bool = True,
) -> list[str]:
    """Return structural catalogue violations without modifying the checkout."""
    issues: list[str] = []
    if code:
        issues.extend(_code_artifact_contract(project_root))
    if infra:
        issues.extend(_infra_artifact_contract(project_root))
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--code-only", action="store_true")
    selection.add_argument("--infra-only", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify artifact/index/marker structure without rendering",
    )
    args = parser.parse_args()
    code = not args.infra_only
    infra = not args.code_only

    if args.check:
        issues = check_diagram_contract(code=code, infra=infra)
        if issues:
            for issue in issues:
                print(f"ERROR: {issue}", file=sys.stderr)
            raise SystemExit(1)
        print("Diagram artifact contract is current")
        return

    if code and "SOURCE_DATE_EPOCH" not in os.environ:
        parser.error("canonical code generation requires integer SOURCE_DATE_EPOCH")
    if code and "GCO_DIAGRAM_SOURCE_COMMIT" not in os.environ:
        parser.error("canonical code generation requires 40-character GCO_DIAGRAM_SOURCE_COMMIT")
    if code:
        subprocess.run(
            [sys.executable, "diagrams/code_diagrams/generate.py", "--require-png"],
            cwd=ROOT,
            check=True,
        )
    if infra:
        subprocess.run(
            [sys.executable, "diagrams/infra_diagrams/generate.py", "--stack", "all"],
            cwd=ROOT,
            check=True,
        )

    issues = check_diagram_contract(code=code, infra=infra)
    if issues:
        raise RuntimeError(
            "diagram generation completed with structural drift: " + "; ".join(issues)
        )


if __name__ == "__main__":
    main()
