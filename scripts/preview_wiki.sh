#!/usr/bin/env bash
#
# preview_wiki.sh — build the orientation wiki exactly as CI does, then serve
# it locally with live reload for editing wiki/ pages.
#
# Two phases:
#   1. `mkdocs build --strict` — the byte-for-byte command the PR gate
#      (lint:mkdocs:strict) and the Pages deploy run, so anything that would
#      fail CI fails here first, before you look at a rendered page.
#   2. `mkdocs serve` — live-reloading preview; edits to wiki/*.md and
#      mkdocs.yml re-render on save. (The serve phase is intentionally not
#      strict: a mid-edit broken link should show an error in the terminal,
#      not kill your preview loop. Re-run the script — or wait for the
#      strict build in phase 1 of your next run — for the CI verdict.)
#
# The published site also carries the coverage report at /coverage/, merged
# in by pages.yml at deploy time from the Unit Tests artifact — it is NOT
# part of the local MkDocs build, so the nav's "Coverage report" entry
# points at the live site and a locally served /coverage/ 404s. That is
# expected.
#
# Usage:
#   ./scripts/preview_wiki.sh                 # strict build, then serve on :8000
#   ./scripts/preview_wiki.sh --build-only    # strict build into site/, no server
#   ./scripts/preview_wiki.sh --port 9000     # serve on another port
#
# Requirements: the docs toolchain on the CURRENT python — install with
#   pip install -e ".[docs]"
# (in a clean venv, or use the dev container; see CONTRIBUTING.md
# "Developing the wiki" for the container port-forward invocation). The
# script never installs anything itself.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

BUILD_ONLY=0
PORT=8000

while [ "$#" -gt 0 ]; do
    case "$1" in
        --build-only)
            BUILD_ONLY=1
            shift
            ;;
        --port)
            [ "$#" -ge 2 ] || { echo "error: --port needs a value" >&2; exit 2; }
            PORT="$2"
            shift 2
            ;;
        -h | --help)
            sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "error: unknown argument '$1' (try --help)" >&2
            exit 2
            ;;
    esac
done

if ! command -v mkdocs >/dev/null 2>&1; then
    echo "error: mkdocs is not on PATH." >&2
    echo "Install the docs toolchain into your active environment first:" >&2
    echo "    pip install -e \".[docs]\"" >&2
    echo "See CONTRIBUTING.md — \"Developing the wiki\"." >&2
    exit 1
fi

echo "==> Strict build (the exact check CI runs)"
mkdocs build --strict

if [ "$BUILD_ONLY" -eq 1 ]; then
    echo "==> Built site/ — open site/index.html, or serve it for working search:"
    echo "    ./scripts/preview_wiki.sh --port ${PORT}"
    exit 0
fi

echo "==> Serving with live reload at http://127.0.0.1:${PORT}/ (Ctrl-C to stop)"
exec mkdocs serve --dev-addr "127.0.0.1:${PORT}"
