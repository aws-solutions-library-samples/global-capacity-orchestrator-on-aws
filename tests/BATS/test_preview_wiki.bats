#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for scripts/preview_wiki.sh
# -----------------------------------------------------------------------------
# The script runs `mkdocs build --strict` and then `exec`s `mkdocs serve`. A
# fake `mkdocs` on PATH records its argv (and can fail the strict build), so
# every argument path and both phases run without the docs toolchain.
#
# Run:  bats tests/BATS/test_preview_wiki.bats
# -----------------------------------------------------------------------------

load 'helpers.sh'

SCRIPT="$REPO_ROOT/scripts/preview_wiki.sh"

setup() {
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export MKDOCS_CALLS="$BATS_TEST_TMPDIR/mkdocs-calls"
    : > "$MKDOCS_CALLS"
    # FAKE_MKDOCS_BUILD_STATUS: exit status of `mkdocs build ...` (default 0).
    write_stub "$FAKE_BIN" mkdocs <<'FAKE_MKDOCS'
#!/usr/bin/env bash
printf '%s|%s\n' "$PWD" "$*" >> "$MKDOCS_CALLS"
case "${1:-}" in
    build) echo "INFO - Documentation built"; exit "${FAKE_MKDOCS_BUILD_STATUS:-0}" ;;
    serve) echo "INFO - Serving on $*"; exit 0 ;;
esac
exit 0
FAKE_MKDOCS
}

run_preview() {
    run env PATH="$FAKE_BIN:$PATH" bash "$SCRIPT" "$@"
}

@test "preview_wiki.sh passes bash -n and shellcheck" {
    bash -n "$SCRIPT"
    command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "a plain run builds strictly from the repository root, then serves on :8000" {
    run_preview

    [ "$status" -eq 0 ]
    [[ "$output" == *"==> Strict build (the exact check CI runs)"* ]]
    [[ "$output" == *"==> Serving with live reload at http://127.0.0.1:8000/ (Ctrl-C to stop)"* ]]
    [ "$(sed -n 1p "$MKDOCS_CALLS")" = "${REPO_ROOT}|build --strict" ]
    [ "$(sed -n 2p "$MKDOCS_CALLS")" = "${REPO_ROOT}|serve --dev-addr 127.0.0.1:8000" ]
    [ "$(wc -l < "$MKDOCS_CALLS")" -eq 2 ]
}

@test "--port serves on the requested port" {
    run_preview --port 9000

    [ "$status" -eq 0 ]
    [[ "$output" == *"http://127.0.0.1:9000/"* ]]
    grep -qx -- "${REPO_ROOT}|serve --dev-addr 127.0.0.1:9000" "$MKDOCS_CALLS"
}

@test "--build-only stops after the strict build and says how to serve" {
    run_preview --build-only --port 9000

    [ "$status" -eq 0 ]
    [[ "$output" == *"==> Built site/ — open site/index.html, or serve it for working search:"* ]]
    [[ "$output" == *"./scripts/preview_wiki.sh --port 9000"* ]]
    [ "$(wc -l < "$MKDOCS_CALLS")" -eq 1 ]
    ! grep -q '|serve' "$MKDOCS_CALLS"
}

@test "a strict build failure stops the run before anything is served" {
    run env PATH="$FAKE_BIN:$PATH" FAKE_MKDOCS_BUILD_STATUS=1 bash "$SCRIPT"

    [ "$status" -eq 1 ]
    [[ "$output" == *"==> Strict build (the exact check CI runs)"* ]]
    [[ "$output" != *"Serving with live reload"* ]]
    ! grep -q '|serve' "$MKDOCS_CALLS"
}

@test "--help prints the header documentation" {
    run_preview --help

    [ "$status" -eq 0 ]
    [[ "$output" == *"preview_wiki.sh — build the orientation wiki exactly as CI does"* ]]
    [[ "$output" == *"./scripts/preview_wiki.sh --build-only"* ]]
    [[ "$output" != *"set -euo pipefail"* ]]
    [ ! -s "$MKDOCS_CALLS" ]
}

@test "--port without a value and an unknown argument are usage errors" {
    run_preview --port
    [ "$status" -eq 2 ]
    [[ "$output" == *"error: --port needs a value"* ]]

    run_preview --serve
    [ "$status" -eq 2 ]
    [[ "$output" == *"error: unknown argument '--serve' (try --help)"* ]]
    [ ! -s "$MKDOCS_CALLS" ]
}

@test "without mkdocs on PATH the script explains how to install the toolchain" {
    local tools="$BATS_TEST_TMPDIR/tools"
    path_without "$tools" mkdocs
    run env PATH="$tools" bash "$SCRIPT" --build-only

    [ "$status" -eq 1 ]
    [[ "$output" == *"error: mkdocs is not on PATH."* ]]
    [[ "$output" == *'pip install -e ".[docs]"'* ]]
    [[ "$output" == *'See CONTRIBUTING.md — "Developing the wiki".'* ]]
}
