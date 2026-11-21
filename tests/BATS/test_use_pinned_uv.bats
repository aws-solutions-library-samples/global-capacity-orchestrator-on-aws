#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for .github/scripts/use-pinned-uv.sh
# -----------------------------------------------------------------------------
# The script installs the uv release Dockerfile.dev pins (ARG UV_VERSION) and
# checks what it got. `pip` is a fake that records its arguments and "installs"
# a fake `uv` reporting the requested version (or another one, to fake a
# broken install), so the outcomes run without touching a real Python
# environment: the pinned release installed and verified, an install that
# leaves the wrong uv or none at all, and the Dockerfile refusals.
#
# Run:  bats tests/BATS/test_use_pinned_uv.bats
# -----------------------------------------------------------------------------

load 'helpers.sh'

SCRIPT="$REPO_ROOT/.github/scripts/use-pinned-uv.sh"

setup() {
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export PIP_CALLS="$BATS_TEST_TMPDIR/pip-calls"
    export FAKE_BIN
    : > "$PIP_CALLS"
    # FAKE_UV_INSTALLS_TO: the version the installed uv reports (default: the
    # requested one); FAKE_UV_INSTALLS_TO=none installs no uv at all.
    write_stub "$FAKE_BIN" pip <<'FAKE_PIP'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PIP_CALLS"
requested=""
for arg in "$@"; do
    case "$arg" in uv==*) requested="${arg#uv==}" ;; esac
done
reported="${FAKE_UV_INSTALLS_TO:-$requested}"
[ "$reported" = "none" ] && exit 0
printf '#!/usr/bin/env bash\necho "uv %s (0000000 2026-11-17)"\n' "$reported" > "$FAKE_BIN/uv"
chmod +x "$FAKE_BIN/uv"
FAKE_PIP
    DOCKERFILE="$BATS_TEST_TMPDIR/Dockerfile.dev"
    printf 'FROM python:3.14-slim\nARG UV_VERSION=0.13.6\nARG OTHER_VERSION=9.9.9\n' > "$DOCKERFILE"
}

run_pinned() {
    run env PATH="$FAKE_BIN:/usr/bin:/bin" "$@" bash "$SCRIPT" "$DOCKERFILE"
}

@test "use-pinned-uv.sh passes bash -n and shellcheck" {
    bash -n "$SCRIPT"
    command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "the release Dockerfile.dev pins is installed, then verified" {
    run_pinned
    [ "$status" -eq 0 ]
    [[ "$output" == *"Using uv 0.13.6 pinned by ${DOCKERFILE}"* ]]
    [ "$(cat "$PIP_CALLS")" = "install uv==0.13.6" ]
}

@test "an install that leaves another uv behind is a failure, not a pass" {
    run_pinned FAKE_UV_INSTALLS_TO=0.12.0
    [ "$status" -eq 1 ]
    [[ "$output" == *"uv version mismatch: expected 0.13.6, found 0.12.0"* ]]
}

@test "an install that leaves no uv at all is a failure" {
    run_pinned FAKE_UV_INSTALLS_TO=none
    [ "$status" -eq 1 ]
    [[ "$output" == *"uv version mismatch: expected 0.13.6, found nothing"* ]]
}

@test "a Dockerfile without an exact UV_VERSION is refused before pip runs" {
    printf 'FROM python:3.14-slim\nARG UV_VERSION=latest\n' > "$DOCKERFILE"
    run_pinned
    [ "$status" -eq 1 ]
    [[ "$output" == *"ARG UV_VERSION must pin an exact uv release: ${DOCKERFILE}"* ]]
    [ ! -s "$PIP_CALLS" ]
}

@test "a missing Dockerfile is refused" {
    rm -f "$DOCKERFILE"
    run_pinned
    [ "$status" -eq 1 ]
    [[ "$output" == *"Dockerfile not found: ${DOCKERFILE}"* ]]
}

@test "the Dockerfile defaults to ./Dockerfile.dev, and the committed one pins uv exactly" {
    local project="$BATS_TEST_TMPDIR/project"
    mkdir -p "$project"
    cp "$REPO_ROOT/Dockerfile.dev" "$project/Dockerfile.dev"
    local pinned
    pinned="$(sed -n 's/^ARG UV_VERSION=//p' "$REPO_ROOT/Dockerfile.dev")"
    [[ "$pinned" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
    run env PATH="$FAKE_BIN:/usr/bin:/bin" bash -c 'cd "$1" && exec bash "$2"' _ "$project" "$SCRIPT"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Using uv ${pinned} pinned by Dockerfile.dev"* ]]
}
