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
    python diagrams/code_diagrams/generate.py           # all targets
    python diagrams/code_diagrams/generate.py --target lambda/analytics-presigned-url/handler.py:lambda_handler
    python diagrams/code_diagrams/generate.py --skip-png
    python diagrams/code_diagrams/generate.py --skip-marker  # don't touch source files
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root to path so this script can be run standalone
# (``python diagrams/code_diagrams/generate.py``) without a prior
# ``pip install -e .``. The project root is two parents up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from diagrams.code_diagrams._renderer import (  # noqa: E402
    prune_orphaned_artifacts,
    render_all,
    write_readme,
)
from diagrams.code_diagrams._source_marker import (  # noqa: E402
    strip_all_markers,
    upsert_markers,
)
from diagrams.code_diagrams._targets import TARGETS, Target  # noqa: E402
from diagrams.code_diagrams._timestamp import generation_timestamp_utc  # noqa: E402


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
        "--skip-png",
        action="store_true",
        help=(
            "Skip Playwright PNG rendering, remove selected targets' stale "
            "PNGs, and still write HTML."
        ),
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

    targets = _filter_targets(TARGETS, args.target)
    full_catalog = args.target is None
    generated_at = generation_timestamp_utc()

    print("🧭 GCO Code Flowchart Generator")
    print("=" * 50)
    print(f"   Project root : {project_root}")
    print(f"   Output dir   : {output_dir}")
    print(f"   Targets      : {len(targets)}")
    print(f"   Generated at : {generated_at}")

    results = render_all(
        targets=targets,
        project_root=project_root,
        output_dir=output_dir,
        render_png=not args.skip_png,
        generated_at=generated_at,
    )

    if not args.skip_marker:
        if full_catalog:
            upsert_markers(results, project_root=project_root)
        else:
            print(
                "\n🖋  Skipping source-marker refresh for a partial run; "
                "a full run preserves every marker entry."
            )

    if full_catalog:
        prune_orphaned_artifacts(targets=TARGETS, output_dir=output_dir)
        write_readme(results, output_dir=output_dir)
    else:
        print("\n📝 Keeping the full-catalog README unchanged for a partial run.")

    print("\n" + "=" * 50)
    print("✅ Code flowchart generation complete!")
    print(f"   Output directory: {output_dir.absolute()}")


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
