#!/usr/bin/env python3
"""Stage the wiki's source tree and build or preview it with Zensical.

The orientation wiki is published from three places in the repository, none of
which may be copied by hand into another: the pages under ``wiki/``, the tracked
screenshots under ``images/`` and the generated API spec sheets (plus their
interaction diagram) under ``diagrams/api_specs/``. MkDocs pulled the last two
into the build through a hook and kept ``wiki/README.md`` out of it with
``exclude_docs``; Zensical supports neither, so this script does the same three
jobs explicitly, as a staging step Zensical then reads from (``docs_dir`` in
``zensical.toml`` is ``build/wiki``)::

    wiki/*.md                    -> build/wiki/<page>.md        (README.md excluded)
    images/*                     -> build/wiki/assets/images/   (README.md excluded)
    diagrams/api_specs/*.md|svg  -> build/wiki/api/

Staging is a sync: files are copied when missing or changed, files that no
longer have a source are removed, and the tree is never deleted wholesale, so
the preview server keeps watching a directory that exists.

Usage::

    python scripts/build_wiki.py stage             # assemble build/wiki only
    python scripts/build_wiki.py build             # stage, then `zensical build --clean --strict`
    python scripts/build_wiki.py serve [--port N]  # stage, strict build, then `zensical serve`
                                                   # re-staging whenever a source changes

``build`` is the exact command the ``lint:zensical:strict`` PR job and the
``pages.yml`` deploy run. ``serve`` runs it first, so anything that would fail
CI fails before a page is opened; the server itself is not strict (a mid-edit
broken link shows up in the terminal instead of killing the preview loop).

Requirements: the docs toolchain on the current interpreter — ``pip install -e
".[docs]"``. The script never installs anything itself.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where the staged tree lives, relative to the repository root. Matches
#: ``docs_dir`` in zensical.toml; tests/test_wiki.py pins the two together.
STAGE_DIR = Path("build") / "wiki"
#: The three sources and where each lands inside the staged tree.
WIKI_DIR = Path("wiki")
IMAGES_DIR = Path("images")
API_SPECS_DIR = Path("diagrams") / "api_specs"
ASSETS_PREFIX = Path("assets") / "images"
API_PREFIX = Path("api")
#: A directory README documents the directory for contributors browsing
#: GitHub; it is never a page (or an asset) of the site.
README = "README.md"
#: What of diagrams/api_specs/ is content: the sheets and the diagram they
#: embed. The generator and its package marker stay out of the site.
API_SUFFIXES = frozenset({".md", ".svg"})

DEFAULT_PORT = 8000
#: How often ``serve`` looks for source changes, in seconds.
POLL_SECONDS = 1.0


class ToolchainError(RuntimeError):
    """Zensical is not installed on the interpreter running this script."""


def staged_files(repo_root: Path) -> dict[Path, Path]:
    """``{relative path inside the stage: source file}`` for everything the site includes."""
    mapping: dict[Path, Path] = {}
    for page in sorted((repo_root / WIKI_DIR).glob("*.md")):
        if page.name != README:
            mapping[Path(page.name)] = page
    for asset in sorted((repo_root / IMAGES_DIR).iterdir()):
        if asset.is_file() and asset.name != README:
            mapping[ASSETS_PREFIX / asset.name] = asset
    for sheet in sorted((repo_root / API_SPECS_DIR).iterdir()):
        if sheet.is_file() and sheet.suffix in API_SUFFIXES:
            mapping[API_PREFIX / sheet.name] = sheet
    return mapping


def _differs(source: Path, target: Path) -> bool:
    if not target.is_file():
        return True
    src, dst = source.stat(), target.stat()
    return (src.st_size, src.st_mtime_ns) != (dst.st_size, dst.st_mtime_ns)


def stage(repo_root: Path) -> tuple[list[Path], list[Path]]:
    """Sync the staged tree with its sources; return ``(copied, removed)`` relative paths."""
    stage_root = repo_root / STAGE_DIR
    stage_root.mkdir(parents=True, exist_ok=True)
    mapping = staged_files(repo_root)
    copied: list[Path] = []
    for relative, source in mapping.items():
        target = stage_root / relative
        if _differs(source, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)  # copy2 keeps mtime, which _differs compares
            copied.append(relative)
    removed: list[Path] = []
    for path in sorted(stage_root.rglob("*")):
        if path.is_file() and path.relative_to(stage_root) not in mapping:
            path.unlink()
            removed.append(path.relative_to(stage_root))
    for directory in sorted((p for p in stage_root.rglob("*") if p.is_dir()), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()
    return copied, removed


def snapshot(repo_root: Path) -> dict[Path, tuple[int, int]]:
    """Sizes and mtimes of every source file, so ``serve`` can tell when to re-stage."""
    return {
        relative: (source.stat().st_size, source.stat().st_mtime_ns)
        for relative, source in staged_files(repo_root).items()
    }


def zensical_executable() -> str:
    """The ``zensical`` console script of the running interpreter, or the one on PATH."""
    sibling = Path(sys.executable).with_name("zensical")
    if sibling.is_file():
        return str(sibling)
    on_path = shutil.which("zensical")
    if on_path is None:
        raise ToolchainError(
            "zensical is not installed for this interpreter.\n"
            'Install the docs toolchain into your active environment first: pip install -e ".[docs]"\n'
            'See CONTRIBUTING.md — "Developing the wiki".'
        )
    return on_path


def _report(action: str, copied: list[Path], removed: list[Path]) -> None:
    print(f"==> {action}: {len(copied)} copied, {len(removed)} removed -> {STAGE_DIR}/", flush=True)


def build(repo_root: Path) -> int:
    """Stage, then run the strict Zensical build CI runs. Returns the exit status."""
    zensical = zensical_executable()
    _report("Staging the wiki sources", *stage(repo_root))
    print("==> Strict build (the exact check CI runs)", flush=True)
    return subprocess.run(
        [zensical, "build", "--clean", "--strict"], cwd=repo_root, check=False
    ).returncode


def serve(repo_root: Path, port: int) -> int:
    """Stage, build strictly, then serve with live reload, re-staging sources as they change."""
    status = build(repo_root)
    if status != 0:
        return status
    zensical = zensical_executable()
    print(
        f"==> Serving with live reload at http://127.0.0.1:{port}/ (Ctrl-C to stop)",
        flush=True,
    )
    process = subprocess.Popen(
        [zensical, "serve", "--dev-addr", f"127.0.0.1:{port}"], cwd=repo_root
    )
    seen = snapshot(repo_root)
    try:
        while process.poll() is None:
            time.sleep(POLL_SECONDS)
            current = snapshot(repo_root)
            if current != seen:
                seen = current
                _report("Sources changed; re-staging", *stage(repo_root))
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        return 0
    return process.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage the wiki's sources into build/wiki and build or preview them with Zensical."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "stage", help="assemble build/wiki from wiki/, images/ and diagrams/api_specs/"
    )
    commands.add_parser("build", help="stage, then `zensical build --clean --strict` into site/")
    serve_parser = commands.add_parser(
        "serve", help="stage, build strictly, then `zensical serve` with live reload"
    )
    serve_parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port to serve on (default {DEFAULT_PORT})"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "stage":
            _report("Staging the wiki sources", *stage(REPO_ROOT))
            return 0
        if args.command == "build":
            return build(REPO_ROOT)
        return serve(REPO_ROOT, args.port)
    except ToolchainError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
