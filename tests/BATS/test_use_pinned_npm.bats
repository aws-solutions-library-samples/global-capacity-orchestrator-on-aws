#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for .github/scripts/use-pinned-npm.sh
# -----------------------------------------------------------------------------
# The script pins the runner's npm to the exact release a package.json
# declares in `packageManager`. `node` is real (it only parses the manifest);
# `npm` is a fake that reports a scripted version, records every invocation,
# and can switch versions when "installed", so the four outcomes — already
# pinned, installed then verified, installed but still wrong, and the manifest
# refusals — run without touching a global npm.
#
# Run:  bats tests/BATS/test_use_pinned_npm.bats
# -----------------------------------------------------------------------------

load 'helpers.sh'

SCRIPT="$REPO_ROOT/.github/scripts/use-pinned-npm.sh"

setup() {
    command -v node >/dev/null 2>&1 || skip "node not installed"
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export NPM_CALLS="$BATS_TEST_TMPDIR/npm-calls"
    export NPM_VERSION_FILE="$BATS_TEST_TMPDIR/npm-version"
    : > "$NPM_CALLS"
    # FAKE_NPM_INSTALLS_TO: the version `npm install --global npm@X` leaves
    # behind (default: X itself; set to another value to fake a broken install).
    write_stub "$FAKE_BIN" npm <<'FAKE_NPM'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$NPM_CALLS"
case "${1:-}" in
    --version) cat "$NPM_VERSION_FILE" ;;
    install)
        requested=""
        for arg in "$@"; do
            case "$arg" in npm@*) requested="${arg#npm@}" ;; esac
        done
        printf '%s\n' "${FAKE_NPM_INSTALLS_TO:-$requested}" > "$NPM_VERSION_FILE"
        ;;
esac
exit 0
FAKE_NPM
    MANIFEST="$BATS_TEST_TMPDIR/package.json"
    printf '{"name":"probe","packageManager":"npm@11.6.2"}\n' > "$MANIFEST"
}

run_pinned() {
    run env PATH="$FAKE_BIN:$PATH" "$@" bash "$SCRIPT" "$MANIFEST"
}

@test "use-pinned-npm.sh passes bash -n and shellcheck" {
    bash -n "$SCRIPT"
    command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "an npm that already matches the manifest is used as-is" {
    printf '11.6.2\n' > "$NPM_VERSION_FILE"
    run_pinned

    [ "$status" -eq 0 ]
    [ "$output" = "Using npm 11.6.2 declared by ${MANIFEST}" ]
    ! grep -q '^install' "$NPM_CALLS"
}

@test "a different npm is replaced by the declared release, then verified" {
    printf '10.9.0\n' > "$NPM_VERSION_FILE"
    run_pinned

    [ "$status" -eq 0 ]
    [ "$output" = "Using npm 11.6.2 declared by ${MANIFEST}" ]
    grep -qx -- 'install --global npm@11.6.2 --ignore-scripts --no-audit --no-fund' "$NPM_CALLS"
    [ "$(cat "$NPM_VERSION_FILE")" = "11.6.2" ]
}

@test "an install that leaves the wrong npm behind is a failure, not a pass" {
    printf '10.9.0\n' > "$NPM_VERSION_FILE"
    run_pinned FAKE_NPM_INSTALLS_TO=10.9.0

    [ "$status" -eq 1 ]
    [[ "$output" == *"npm version mismatch: expected 11.6.2, found 10.9.0"* ]]
}

@test "a missing npm is installed rather than treated as a version" {
    # `npm --version` failing (no npm at all) must not abort under set -e.
    write_stub "$FAKE_BIN" npm <<'FAKE_NPM'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$NPM_CALLS"
case "${1:-}" in
    --version) [ -e "$NPM_VERSION_FILE" ] || exit 127; cat "$NPM_VERSION_FILE" ;;
    install) printf '11.6.2\n' > "$NPM_VERSION_FILE" ;;
esac
exit 0
FAKE_NPM
    rm -f "$NPM_VERSION_FILE"
    run_pinned

    [ "$status" -eq 0 ]
    [[ "$output" == *"Using npm 11.6.2"* ]]
    grep -q '^install --global npm@11.6.2' "$NPM_CALLS"
}

@test "the manifest defaults to ./package.json" {
    printf '11.6.2\n' > "$NPM_VERSION_FILE"
    local project="$BATS_TEST_TMPDIR/project"
    mkdir -p "$project"
    cp "$MANIFEST" "$project/package.json"
    run env PATH="$FAKE_BIN:$PATH" bash -c 'cd "$1" && exec bash "$2"' _ "$project" "$SCRIPT"

    [ "$status" -eq 0 ]
    [ "$output" = "Using npm 11.6.2 declared by package.json" ]
}

@test "a missing manifest is refused" {
    rm -f "$MANIFEST"
    run_pinned

    [ "$status" -eq 1 ]
    [[ "$output" == *"npm manifest not found: ${MANIFEST}"* ]]
}

@test "a manifest without an exact npm pin is refused" {
    printf '11.6.2\n' > "$NPM_VERSION_FILE"
    local value
    for value in '"npm@^11"' '"pnpm@9.0.0"' '"npm@11.6"' 'null'; do
        printf '{"name":"probe","packageManager":%s}\n' "$value" > "$MANIFEST"
        run_pinned
        echo "case: $value"
        [ "$status" -eq 1 ]
        [[ "$output" == *"packageManager must declare an exact npm version: ${MANIFEST}"* ]]
    done
    ! grep -q '^install' "$NPM_CALLS"
}
