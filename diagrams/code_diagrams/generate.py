#!/usr/bin/env python3
"""Generate code flowcharts for GCO using pyflowchart + Playwright.

For each ``(source_file, function)`` target in :data:`TARGETS`:

1. Parse the function body with :mod:`pyflowchart` to produce a
   flowchart.js DSL string.
2. Emit an interactive HTML page (flowchart.js renders client-side).
3. Render the same diagram to a PNG using a headless Chromium via
   :mod:`playwright` (optional — when skipped or unavailable, any older PNG
   for that target is removed so artifact timestamps cannot disagree).
4. Stamp the HTML, PNG, generated catalogue, and source marker with one
   invocation-wide UTC generation time.
5. Insert (idempotently) a source comment near the top of the source file
   pointing at the generated HTML and PNG.

Outputs mirror the source tree under ``diagrams/code_diagrams/``:

    lambda/analytics-presigned-url/handler.py::lambda_handler
        -> diagrams/code_diagrams/lambda/analytics-presigned-url/handler.lambda_handler.{html,png}

The README in ``diagrams/code_diagrams/`` is regenerated at the end
with a hierarchical, grouped-by-top-level-directory index so the
listing reflects the actual project layout.

Usage:
    GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/code_diagrams/generate.py
    GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/code_diagrams/generate.py --target lambda/analytics-presigned-url/handler.py:lambda_handler
    GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/code_diagrams/generate.py --skip-png
    GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/code_diagrams/generate.py --skip-marker
    python diagrams/code_diagrams/generate.py --strip-markers
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

# Add project root to path so direct script invocation works without a prior
# ``pip install -e .``. The project root is two parents up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from diagrams.code_diagrams._renderer import (  # noqa: E402
    RenderedTarget,
    _output_stem_for,
    prune_orphaned_artifacts,
    render_all,
    write_readme,
)
from diagrams.code_diagrams._source_marker import (  # noqa: E402
    SENTINEL,
    strip_all_markers,
    strip_markers_from,
    upsert_markers,
)
from diagrams.code_diagrams._targets import TARGETS, Target  # noqa: E402
from diagrams.code_diagrams._timestamp import (  # noqa: E402
    generation_source_commit,
    generation_timestamp_utc,
)
from gco.lambda_shared_sources import LAMBDA_SHARED_SOURCE_TARGETS  # noqa: E402

_MARKER_BYTES_RE = re.compile(
    rb"(?:\r?\n)?# <" + re.escape(SENTINEL.encode()) + rb"> BEGIN[^\r\n]*\r?\n.*?"
    rb"# <" + re.escape(SENTINEL.encode()) + rb"> END(?:\r?\n){2}",
    re.DOTALL,
)

#: Committed next to the catalogue README. Records, per charted source, the
#: SHA-256 of its marker-stripped bytes plus the timestamp and commit that
#: produced its artifacts. Two properties follow from keeping provenance
#: *per source* rather than catalogue-wide:
#:
#: 1. The contract check verifies working-tree sources against these digests
#:    instead of resolving ``source_commit`` from Git history — a squash-merged
#:    PR deletes its branch commits, so a recorded SHA is a human-readable
#:    label, never a lookup key that must resolve in every future clone.
#: 2. Regeneration is incremental. Changing one charted file restamps only
#:    that file's artifacts, so a PR's diagram diff stays proportional to the
#:    code it actually touched instead of restamping all ~200 artifacts.
PROVENANCE_MANIFEST_NAME = "provenance.json"

#: Bumped when the manifest layout changes. v1 recorded one catalogue-wide
#: ``generated_at`` / ``source_commit`` plus a flat ``source_digests`` map;
#: v2 records a per-source ``sources`` mapping.
PROVENANCE_SCHEMA_VERSION = 2


def _without_generated_marker(source: bytes) -> bytes:
    """Remove only the generated marker bytes; preserve every other byte."""
    return _MARKER_BYTES_RE.sub(b"", source)


def source_content_digest(source: bytes) -> str:
    """SHA-256 hex digest of a charted source with generated markers removed.

    Marker blocks are excluded so that restamping timestamps/commits during
    regeneration never changes a source's recorded digest — only substantive
    code changes do.
    """
    return hashlib.sha256(_without_generated_marker(source)).hexdigest()


def provenance_manifest_path(project_root: Path) -> Path:
    """Absolute path of the committed provenance manifest."""
    return project_root / "diagrams" / "code_diagrams" / PROVENANCE_MANIFEST_NAME


def load_provenance_manifest(project_root: Path) -> dict[str, dict[str, str]]:
    """Return ``{source: {digest, generated_at, source_commit}}``.

    Raises :class:`RuntimeError` with an actionable message when the manifest
    is absent or unreadable — the catalogue cannot be verified without it.
    """
    path = provenance_manifest_path(project_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(
            f"missing {PROVENANCE_MANIFEST_NAME}: regenerate the code diagram catalogue"
        ) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unreadable {PROVENANCE_MANIFEST_NAME}: {exc}") from exc

    sources = raw.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise RuntimeError(f"{PROVENANCE_MANIFEST_NAME} has no sources mapping")
    entries: dict[str, dict[str, str]] = {}
    for source, entry in sources.items():
        if not isinstance(entry, dict) or not {
            "digest",
            "generated_at",
            "source_commit",
        } <= set(entry):
            raise RuntimeError(
                f"{PROVENANCE_MANIFEST_NAME} entry for {source} must record "
                "digest, generated_at, and source_commit"
            )
        entries[source] = {
            key: str(entry[key]) for key in ("digest", "generated_at", "source_commit")
        }
    return entries


def newest_provenance_stamp(manifest: dict[str, dict[str, str]]) -> tuple[str, str]:
    """Return the ``(generated_at, source_commit)`` of the newest entry.

    The catalogue README carries one stamp describing the most recent
    regeneration; per-source stamps live in the manifest and the markers.
    """
    newest = max(manifest.values(), key=lambda entry: entry["generated_at"])
    return newest["generated_at"], newest["source_commit"]


def write_provenance_manifest(
    *,
    project_root: Path,
    output_dir: Path,
    regenerated_targets: list[Target],
    generated_at: str,
    source_commit: str,
    catalog: list[Target],
) -> Path:
    """Merge this run's regenerated sources into the manifest and write it.

    Entries for sources this run did not regenerate are preserved verbatim,
    which is what keeps an incremental run's diff small. Sources no longer in
    ``catalog`` are dropped.
    """
    try:
        existing = load_provenance_manifest(project_root)
    except RuntimeError:
        existing = {}

    charted = {target.source for target in catalog}
    merged = {source: entry for source, entry in existing.items() if source in charted}
    for source in sorted({target.source for target in regenerated_targets}):
        merged[source] = {
            "digest": source_content_digest((project_root / source).read_bytes()),
            "generated_at": generated_at,
            "source_commit": source_commit,
        }

    manifest = {"schema_version": PROVENANCE_SCHEMA_VERSION, "sources": merged}
    path = output_dir / PROVENANCE_MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def select_stale_targets(
    *,
    project_root: Path,
    targets: list[Target],
    output_dir: Path,
) -> list[Target]:
    """Return the targets an incremental run must re-render.

    A target is stale when its source's marker-stripped bytes no longer match
    the recorded digest (a substantive code change), when the source has no
    recorded provenance at all (newly charted), or when either committed
    artifact is missing. Everything else is already current and is left byte-
    for-byte alone.
    """
    try:
        manifest = load_provenance_manifest(project_root)
    except RuntimeError:
        return list(targets)

    digest_cache: dict[str, str] = {}
    stale: list[Target] = []
    for target in targets:
        entry = manifest.get(target.source)
        if entry is None:
            stale.append(target)
            continue
        if target.source not in digest_cache:
            digest_cache[target.source] = source_content_digest(
                (project_root / target.source).read_bytes()
            )
        if digest_cache[target.source] != entry["digest"]:
            stale.append(target)
            continue
        stem = _output_stem_for(target, output_dir=output_dir)
        if (
            not stem.with_name(f"{stem.name}.html").is_file()
            or not stem.with_name(f"{stem.name}.png").is_file()
        ):
            stale.append(target)
    return stale


def _verify_targets_match_source_commit(
    *, project_root: Path, targets: list[Target], source_commit: str
) -> None:
    """Require marker-excluded target bytes to equal a real Git commit."""
    object_type = subprocess.run(  # noqa: S603 — fixed Git command, hex-only ref
        ["git", "cat-file", "-t", source_commit],
        cwd=project_root,
        capture_output=True,
        check=False,
    )
    if object_type.returncode != 0:
        detail = object_type.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"GCO_DIAGRAM_SOURCE_COMMIT {source_commit} does not resolve: {detail}")
    if object_type.stdout.strip() != b"commit":
        actual_type = object_type.stdout.decode(errors="replace").strip()
        raise RuntimeError(
            f"GCO_DIAGRAM_SOURCE_COMMIT {source_commit} is a {actual_type}, not a commit"
        )

    mismatches: list[str] = []
    for source in sorted({target.source for target in targets}):
        result = subprocess.run(  # noqa: S603 — fixed Git command and catalog paths
            ["git", "show", f"{source_commit}:{source}"],
            cwd=project_root,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(
                f"cannot read {source} from GCO_DIAGRAM_SOURCE_COMMIT {source_commit}: {detail}"
            )
        current = (project_root / source).read_bytes()
        if _without_generated_marker(current) != _without_generated_marker(result.stdout):
            mismatches.append(source)
    if mismatches:
        raise RuntimeError(
            "charted source bytes differ from GCO_DIAGRAM_SOURCE_COMMIT after "
            f"removing only generated markers: {mismatches}. Commit substantive "
            "source changes first, then regenerate from that commit."
        )


def verify_targets_match_provenance_manifest(
    *, project_root: Path, targets: list[Target]
) -> dict[str, dict[str, str]]:
    """Require charted source bytes to match the committed digest manifest.

    This is the repository-side freshness contract, and it is deliberately
    self-contained: it compares working-tree bytes (markers stripped) against
    the SHA-256 digests recorded at generation time and never resolves a
    recorded commit from Git history. The generation-time check
    (:func:`_verify_targets_match_source_commit`) still anchors generation to a
    real committed state on the machine running the generator — but once
    committed, the catalogue must stay verifiable in any clone: a squash merge
    deletes branch commits, so a recorded SHA can legitimately be unreachable
    while the catalogue remains exactly current.

    Returns the loaded manifest so the caller can cross-check per-source
    marker and artifact stamps against it.
    """
    manifest = load_provenance_manifest(project_root)

    charted = sorted({target.source for target in targets})
    missing = sorted(set(charted) - set(manifest))
    retired = sorted(set(manifest) - set(charted))
    if missing or retired:
        raise RuntimeError(
            f"{PROVENANCE_MANIFEST_NAME} is out of sync with the target catalogue "
            f"(missing: {missing}, retired: {retired}); regenerate the catalogue"
        )

    mismatches = [
        source
        for source in charted
        if source_content_digest((project_root / source).read_bytes()) != manifest[source]["digest"]
    ]
    if mismatches:
        raise RuntimeError(
            "charted source bytes differ from the recorded provenance digests after "
            f"removing only generated markers: {mismatches}. Commit substantive "
            "source changes first, then regenerate (``python diagrams/generate.py "
            "--code-only`` re-renders only the sources that changed)."
        )
    return manifest


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Generate GCO code flowcharts (HTML + PNG).",
    )
    parser.add_argument(
        "--target",
        action="append",
        default=None,
        metavar="PATH:FUNC",
        help=(
            "Only generate the named target(s). Repeatable. "
            "Format: ``path/to/file.py:function_name``. "
            "Default: all targets."
        ),
    )
    parser.add_argument(
        "--all",
        dest="force_all",
        action="store_true",
        help=(
            "Re-render every target instead of only the sources whose bytes "
            "changed. Restamps the whole catalogue, so prefer the default "
            "incremental run unless you changed the generator itself."
        ),
    )
    parser.add_argument(
        "--skip-png",
        action="store_true",
        help=(
            "Skip Playwright PNG rendering, remove selected targets' stale "
            "PNGs, and still write HTML."
        ),
    )
    parser.add_argument(
        "--require-png",
        action="store_true",
        help="Fail a canonical run if any selected PNG could not be rendered.",
    )
    parser.add_argument(
        "--skip-marker",
        action="store_true",
        help="Don't insert ``# Flowchart:`` markers into source files.",
    )
    parser.add_argument(
        "--strip-markers",
        action="store_true",
        help=(
            "Remove every existing ``# <pyflowchart-code-diagram>`` "
            "block from the source tree and exit. Useful when "
            "refactoring the generator's placement rules or when "
            "tearing down the feature entirely. Does not regenerate "
            "flowcharts — combine with a normal run afterwards if "
            "you want fresh markers."
        ),
    )
    args = parser.parse_args()
    if args.require_png and args.skip_png:
        parser.error("--require-png cannot be combined with --skip-png")

    project_root = Path(__file__).resolve().parent.parent.parent
    output_dir = Path(__file__).resolve().parent

    if args.strip_markers:
        print("🧹 Stripping pyflowchart markers from source files")
        print("=" * 50)
        print(f"   Project root : {project_root}")
        modified = strip_all_markers(project_root)
        print("\n" + "=" * 50)
        print(f"✅ Stripped markers from {modified} file(s).")
        return

    selected = _filter_targets(TARGETS, args.target)
    full_catalog = args.target is None
    generated_at = generation_timestamp_utc()
    source_commit = generation_source_commit()

    # Incremental by default: re-render only the sources whose marker-stripped
    # bytes differ from the recorded provenance (plus newly charted targets and
    # any missing artifact). This is what keeps a PR's diagram diff
    # proportional to the code it changed. ``--all`` and an explicit
    # ``--target`` both bypass the staleness filter.
    incremental = full_catalog and not args.force_all
    targets = (
        select_stale_targets(
            project_root=project_root,
            targets=selected,
            output_dir=output_dir,
        )
        if incremental
        else selected
    )

    print("🧭 GCO Code Flowchart Generator")
    print("=" * 50)
    print(f"   Project root : {project_root}")
    print(f"   Output dir   : {output_dir}")
    print(f"   Mode         : {'incremental' if incremental else 'full'}")
    print(f"   Targets      : {len(targets)} of {len(selected)} selected")
    print(f"   Generated at : {generated_at}")
    print(f"   Source commit: {source_commit}")

    if not targets:
        # Nothing changed. Leave every committed artifact, marker, README, and
        # manifest byte untouched so a no-op regeneration is a no-op diff.
        print("\n✅ Every charted source is already current; nothing to re-render.")
        return

    _verify_targets_match_source_commit(
        project_root=project_root,
        targets=targets,
        source_commit=source_commit,
    )

    results = render_all(
        targets=targets,
        project_root=project_root,
        output_dir=output_dir,
        render_png=not args.skip_png,
        generated_at=generated_at,
        source_commit=source_commit,
    )
    if args.require_png:
        missing_pngs = [
            result.target.source + ":" + result.target.function
            for result in results
            if result.png_path is None
        ]
        if missing_pngs:
            sys.exit(f"Canonical generation requires every PNG; missing: {missing_pngs}")

    if not args.skip_marker:
        # Refresh markers only in the sources we just re-rendered, so an
        # incremental run leaves every other charted file byte-identical.
        # Retired sources still get their stale markers pruned — the checked-in
        # copies of charted shared Lambda sources are legitimate marker
        # carriers and must stay in the allowed set.
        allowed_markers = {target.source for target in TARGETS}
        for canonical, copies in LAMBDA_SHARED_SOURCE_TARGETS.items():
            if canonical in allowed_markers:
                allowed_markers.update(copies)
        prune_retired_markers(project_root, charted=allowed_markers)
        upsert_markers(results, project_root=project_root)
        _sync_shared_lambda_copies(project_root)

    if full_catalog:
        prune_orphaned_artifacts(targets=TARGETS, output_dir=output_dir)
        manifest_path = write_provenance_manifest(
            project_root=project_root,
            output_dir=output_dir,
            regenerated_targets=targets,
            generated_at=generated_at,
            source_commit=source_commit,
            catalog=TARGETS,
        )
        print(f"📝 Wrote {manifest_path}")
        # The README indexes the whole catalogue, so entries for sources this
        # run did not touch are reconstructed from their recorded provenance.
        write_readme(
            _catalog_readme_entries(
                project_root=project_root,
                output_dir=output_dir,
                results=results,
            ),
            output_dir=output_dir,
        )
    else:
        print("\n📝 Keeping the full-catalog README and manifest unchanged for a partial run.")

    print("\n" + "=" * 50)
    print("✅ Code flowchart generation complete!")
    print(f"   Output directory: {output_dir.absolute()}")


def prune_retired_markers(project_root: Path, *, charted: set[str]) -> int:
    """Strip markers from files that are no longer charted targets.

    A full ``strip_all_markers`` + reinsert pass would rewrite every charted
    source on every run, which is exactly the catalogue-wide churn incremental
    generation exists to avoid. Only genuinely retired sources are touched.
    """
    modified = 0
    for source_path in sorted(project_root.rglob("*.py")):
        relative = source_path.relative_to(project_root).as_posix()
        if relative in charted or "-build" in relative:
            continue
        if not relative.startswith(("app.py", "cli/", "gco/", "gco_mcp/", "lambda/")):
            continue
        original = source_path.read_text(encoding="utf-8")
        if SENTINEL not in original:
            continue
        stripped = strip_markers_from(original)
        if stripped != original:
            source_path.write_text(stripped, encoding="utf-8")
            print(f"   🧹 stripped retired marker from {relative}")
            modified += 1
    return modified


def _catalog_readme_entries(
    *,
    project_root: Path,
    output_dir: Path,
    results: list[RenderedTarget],
) -> list[RenderedTarget]:
    """Return one entry per catalogue target for the README index.

    Freshly rendered targets contribute their real results; every other target
    is reconstructed from its recorded provenance stamp and committed artifact
    paths, so an incremental run still writes a complete index without
    re-rendering (or restamping) the untouched majority.
    """
    rendered = {(result.target.source, result.target.function): result for result in results}
    manifest = load_provenance_manifest(project_root)
    entries: list[RenderedTarget] = []
    for target in TARGETS:
        existing = rendered.get((target.source, target.function))
        if existing is not None:
            entries.append(existing)
            continue
        entry = manifest[target.source]
        stem = _output_stem_for(target, output_dir=output_dir)
        png_path = stem.with_name(f"{stem.name}.png")
        entries.append(
            RenderedTarget(
                target=target,
                html_path=stem.with_name(f"{stem.name}.html"),
                png_path=png_path if png_path.is_file() else None,
                generated_at=entry["generated_at"],
                source_commit=entry["source_commit"],
            )
        )
    return entries


def _sync_shared_lambda_copies(project_root: Path) -> None:
    """Propagate refreshed canonical shared Lambda sources to their copies.

    ``upsert_markers`` rewrites the pyflowchart header inside canonical
    shared sources (``lambda/tls-shared/backend_tls.py``,
    ``lambda/proxy-shared/proxy_utils.py``), but the checked-in per-function
    copies are not diagram targets, so a regeneration used to leave them one
    header behind. That drift is exactly what
    ``tests/test_lambda_shared_sources.py`` rejects and what made every
    deploy rewrite tracked files mid-run (``StackManager._sync_lambda_sources``
    re-syncs at deploy time). Reuse the deploy path's own map so the two
    sync points can never disagree about what a copy is.
    """
    synced = 0
    for source_rel, target_rels in LAMBDA_SHARED_SOURCE_TARGETS.items():
        source = project_root / source_rel
        if not source.is_file():
            continue
        source_bytes = source.read_bytes()
        for target_rel in target_rels:
            target = project_root / target_rel
            if not target.parent.is_dir():
                continue
            if target.is_file() and target.read_bytes() == source_bytes:
                continue
            target.write_bytes(source_bytes)
            synced += 1
            print(f"   Synced shared copy: {target_rel} <- {source_rel}")
    if synced:
        print(f"🔁 Refreshed {synced} shared Lambda cop{'y' if synced == 1 else 'ies'}.")


def _filter_targets(
    all_targets: list[Target],
    requested: list[str] | None,
) -> list[Target]:
    """Filter :data:`TARGETS` by optional ``PATH:FUNC`` arguments."""
    if not requested:
        return list(all_targets)
    wanted = set(requested)
    filtered = [t for t in all_targets if f"{t.source}:{t.function}" in wanted]
    missing = wanted - {f"{t.source}:{t.function}" for t in filtered}
    if missing:
        sys.exit(f"Unknown target(s): {sorted(missing)}")
    return filtered


if __name__ == "__main__":
    main()
