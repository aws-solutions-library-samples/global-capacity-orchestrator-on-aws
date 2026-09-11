#!/usr/bin/env bats
# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for demo/record_destroy.sh
# ─────────────────────────────────────────────────────────────────────────────

load 'helpers.sh'

SCRIPT="$REPO_ROOT/demo/record_destroy.sh"
LIB="$REPO_ROOT/demo/lib_demo.sh"

@test "record_destroy.sh exists and is executable" {
    [ -f "$SCRIPT" ]
    [ -x "$SCRIPT" ]
}

@test "record_destroy.sh passes bash -n syntax check" {
    bash -n "$SCRIPT"
}

@test "record_destroy.sh passes shellcheck" {
    command -v shellcheck &>/dev/null || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "record_destroy.sh sources lib_demo.sh" {
    grep -q "source.*lib_demo.sh" "$SCRIPT"
}

@test "default speed is 50x for teardown" {
    grep -q 'SPEED="${DEMO_SPEED:-50}"' "$SCRIPT"
}

@test "default dimensions are 116x36 with canvas headroom" {
    grep -q 'COLS="${DEMO_COLS:-116}"' "$SCRIPT"
    grep -q 'ROWS="${DEMO_ROWS:-36}"' "$SCRIPT"
}

@test "output files go to demo/ directory" {
    grep -q 'CAST_FILE=.*destroy\.cast' "$SCRIPT"
    grep -q 'GIF_FILE=.*destroy\.gif' "$SCRIPT"
}

@test "runs the repository-bound destroy command" {
    grep -q "python3 -m cli.main stacks destroy-all -y" "$SCRIPT"
}

@test "checks for asciinema installation" {
    grep -q "command -v asciinema" "$SCRIPT"
}

@test "delegates AWS identity verification to the shared guard" {
    grep -q "verify_legacy_live_recording_authorization" "$SCRIPT"
    grep -q "aws sts get-caller-identity" "$LIB"
}

@test "requires explicit live consent and reviewed SHA/account guards" {
    grep -q 'GCO_RECORDING_LIVE=1' "$SCRIPT"
    grep -q 'GCO_EXPECTED_GIT_SHA' "$SCRIPT"
    grep -q 'GCO_EXPECTED_ACCOUNT_ID' "$SCRIPT"
    grep -q 'verify_legacy_live_recording_authorization' "$SCRIPT"
}

@test "supports offline render-existing mode" {
    grep -q 'RENDER_EXISTING="${RENDER_EXISTING:-0}"' "$SCRIPT"
    grep -q 'cp -p "$CAST_FILE" "$RAW_CAST_FILE"' "$SCRIPT"
}

@test "checks the repository GCO CLI module" {
    grep -q "from cli.main import main" "$SCRIPT"
}

@test "stages raw artifacts beside finals with rollback-aware signal cleanup" {
    grep -q 'mktemp -d .*\.destroy-recording\.XXXXXX' "$SCRIPT"
    grep -q 'trap .*cleanup_recording_temps.*EXIT' "$SCRIPT"
    grep -q 'rollback_recording_publication' "$SCRIPT"
    grep -q "trap 'exit 129' HUP" "$SCRIPT"
    grep -q "trap 'exit 130' INT" "$SCRIPT"
    grep -q "trap 'exit 143' TERM" "$SCRIPT"
    grep -q 'rm -rf -- .*RECORDING_TMP_DIR' "$SCRIPT"
}

@test "uses asciinema --return to propagate the recorded destroy status" {
    grep -A2 'asciinema rec' "$SCRIPT" | grep -q -- '--return'
}

@test "supports SKIP_GIF env var" {
    grep -q "SKIP_GIF" "$SCRIPT"
}

@test "documents SKIP_SANITIZE rejection" {
    grep -q "SKIP_SANITIZE is not allowed for publishable recordings" "$SCRIPT"
}

@test "supports SKIP_EMOJI_STRIP env var" {
    # Documented escape hatch for bypassing the emoji substitution pass.
    grep -q "SKIP_EMOJI_STRIP" "$SCRIPT"
}

@test "calls sanitize_cast before rendering the GIF" {
    # Ordering matters: the .cast must be redacted before agg reads it, so
    # both the committed cast and the derived gif have the account ID scrubbed.
    local sanitize_line render_line
    sanitize_line=$(grep -n 'sanitize_cast "\$RAW_CAST_FILE"' "$SCRIPT" | head -1 | cut -d: -f1)
    render_line=$(grep -n 'render_gif ' "$SCRIPT" | head -1 | cut -d: -f1)
    [ -n "$sanitize_line" ]
    [ -n "$render_line" ]
    [ "$sanitize_line" -lt "$render_line" ]
}

@test "strips tofu-triggering codepoints after sanitize, before render" {
    # Pipeline: sanitize_cast → strip_emoji_from_cast → render_gif.
    local sanitize_line strip_line render_line
    sanitize_line=$(grep -n 'sanitize_cast "\$RAW_CAST_FILE"' "$SCRIPT" | head -1 | cut -d: -f1)
    strip_line=$(grep -n 'strip_emoji_from_cast "\$RAW_CAST_FILE"' "$SCRIPT" | head -1 | cut -d: -f1)
    render_line=$(grep -n 'render_gif ' "$SCRIPT" | head -1 | cut -d: -f1)
    [ -n "$sanitize_line" ]
    [ -n "$strip_line" ]
    [ -n "$render_line" ]
    [ "$sanitize_line" -lt "$strip_line" ]
    [ "$strip_line" -lt "$render_line" ]
}

@test "renders staged files and publishes the pair only after verification" {
    local verify_line render_line publish_line
    verify_line=$(grep -n 'verify_cast_sanitized "\$RAW_CAST_FILE"' "$SCRIPT" | head -1 | cut -d: -f1)
    render_line=$(grep -n 'render_gif "\$RAW_CAST_FILE" "\$RAW_GIF_FILE"' "$SCRIPT" | head -1 | cut -d: -f1)
    publish_line=$(grep -n 'publish_recording_artifacts' "$SCRIPT" | tail -1 | cut -d: -f1)
    [ "$verify_line" -lt "$render_line" ]
    [ "$render_line" -lt "$publish_line" ]
}

@test "calls render_gif with staged positional args" {
    grep -q 'render_gif "\$RAW_CAST_FILE" "\$RAW_GIF_FILE" "\$SPEED" "\$THEME" "\$COLS" "\$ROWS"' "$SCRIPT"
}

@test "failed recorded destroy leaves the existing cast and GIF unchanged" {
    local fixture="$BATS_TEST_TMPDIR/destroy recorder; literal \$checkout"
    local fake_bin="$fixture/bin"
    local argv_file="$BATS_TEST_TMPDIR/destroy-asciinema.argv"
    local python_file="$BATS_TEST_TMPDIR/destroy-python.argv"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$fixture/demo/lib_demo.sh"
    printf '{}\n' > "$fixture/cdk.json"
    printf 'existing destroy cast\n' > "$fixture/demo/destroy.cast"
    printf 'existing destroy gif\n' > "$fixture/demo/destroy.gif"

    cat > "$fake_bin/asciinema" <<'FAKE_ASCIINEMA'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$FAKE_ASCIINEMA_ARGV_FILE"
output_file=""
child_command=""
return_child_status=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --return) return_child_status=1; shift ;;
        --cols|--rows) shift 2 ;;
        --command) child_command="$2"; shift 2 ;;
        --overwrite) shift ;;
        *) output_file="$1"; shift ;;
    esac
done
if [ -n "$child_command" ]; then
    bash -c "$child_command"
fi
printf '{"version": 2, "width": 80, "height": 24}\n' > "$output_file"
if [ "$return_child_status" -eq 1 ]; then
    exit "${FAKE_ASCIINEMA_CHILD_STATUS:-0}"
fi
FAKE_ASCIINEMA
    cat > "$fake_bin/python3" <<'FAKE_PYTHON'
#!/usr/bin/env bash
{
    printf '%s\n' "$PWD"
    printf '%s\n' "$@"
} > "$FAKE_PYTHON_INVOCATION_FILE"
exit 0
FAKE_PYTHON
    cat > "$fake_bin/aws" <<'FAKE_AWS'
#!/usr/bin/env bash
printf '%s\n' '123456789012'
FAKE_AWS
    chmod +x "$fake_bin/asciinema" "$fake_bin/python3" "$fake_bin/aws"

    git -C "$fixture" init -q
    git -C "$fixture" add .
    git -C "$fixture" -c user.name=CI -c user.email=ci@example.invalid \
        commit -q -m recording-fixture
    local expected_sha
    expected_sha=$(git -C "$fixture" rev-parse HEAD)

    run env \
        PATH="$fake_bin:$PATH" \
        GCO_RECORDING_LIVE=1 \
        GCO_EXPECTED_GIT_SHA="$expected_sha" \
        GCO_EXPECTED_ACCOUNT_ID=123456789012 \
        SKIP_GIF=1 \
        FAKE_ASCIINEMA_ARGV_FILE="$argv_file" \
        FAKE_ASCIINEMA_CHILD_STATUS=42 \
        FAKE_PYTHON_INVOCATION_FILE="$python_file" \
        GCO_RECORDING_REPO_ROOT="$fixture" bash "$SCRIPT"

    [ "$status" -eq 42 ]
    grep -qx -- '--return' "$argv_file"
    grep -Fxq "bash --norc --noprofile \"\$GCO_RECORDING_WRAPPER\"" "$argv_file"
    [ "$(sed -n '1p' "$python_file")" = "$fixture" ]
    [ "$(sed -n '2p' "$python_file")" = "-m" ]
    [ "$(sed -n '3p' "$python_file")" = "cli.main" ]
    [ "$(sed -n '4p' "$python_file")" = "stacks" ]
    [ "$(sed -n '5p' "$python_file")" = "destroy-all" ]
    [ "$(sed -n '6p' "$python_file")" = "-y" ]
    [ "$(cat "$fixture/demo/destroy.cast")" = "existing destroy cast" ]
    [ "$(cat "$fixture/demo/destroy.gif")" = "existing destroy gif" ]
    [ -z "$(compgen -G "$fixture/demo/.destroy-recording.*" || true)" ]
}


@test "destroy render-existing mode performs no AWS or asciinema call" {
    local fixture="$BATS_TEST_TMPDIR/destroy-render"
    local fake_bin="$fixture/bin"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$fixture/demo/lib_demo.sh"
    {
        printf '{"version":2,"width":120,"height":37}\n'
        printf '[0.1,"o","verified destroy cast"]\n'
    } > "$fixture/demo/destroy.cast"
    printf 'old gif\n' > "$fixture/demo/destroy.gif"

    cat > "$fake_bin/agg" <<'FAKE_AGG'
#!/usr/bin/env bash
printf 'new gif\n' > "${!#}"
FAKE_AGG
    for command in aws asciinema; do
        cat > "$fake_bin/$command" <<'FORBIDDEN'
#!/usr/bin/env bash
exit 97
FORBIDDEN
    done
    chmod +x "$fake_bin"/*
    git -C "$fixture" init -q
    git -C "$fixture" add .
    git -C "$fixture" -c user.name=CI -c user.email=ci@example.invalid \
        commit -q -m render-fixture

    run env PATH="$fake_bin:$PATH" RENDER_EXISTING=1 \
        GCO_RECORDING_REPO_ROOT="$fixture" bash "$SCRIPT"

    [ "$status" -eq 0 ]
    [ "$(cat "$fixture/demo/destroy.gif")" = "new gif" ]
    [ -z "$(compgen -G "$fixture/demo/.destroy-recording.*" || true)" ]
}


@test "destroy render-existing without agg preserves the pair" {
    local fixture="$BATS_TEST_TMPDIR/destroy-no-agg"
    local fake_bin="$fixture/bin"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$fixture/demo/lib_demo.sh"
    printf '{"version":2,"width":116,"height":36}\n' > "$fixture/demo/destroy.cast"
    printf 'old gif\n' > "$fixture/demo/destroy.gif"

    run env PATH="$fake_bin:/usr/bin:/bin" RENDER_EXISTING=1 \
        GCO_RECORDING_REPO_ROOT="$fixture" bash "$SCRIPT"

    [ "$status" -ne 0 ]
    [ "$(cat "$fixture/demo/destroy.gif")" = "old gif" ]
    [ -z "$(compgen -G "$fixture/demo/.destroy-recording.*" || true)" ]
}

@test "destroy rejects SKIP_SANITIZE before touching artifacts" {
    local fixture="$BATS_TEST_TMPDIR/destroy-skip-sanitize"
    local fake_bin="$fixture/bin"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$fixture/demo/lib_demo.sh"
    printf '{"version":2,"width":116,"height":36}\n' > "$fixture/demo/destroy.cast"
    printf 'old gif\n' > "$fixture/demo/destroy.gif"
    cat > "$fake_bin/agg" <<'FAKE_AGG'
#!/usr/bin/env bash
exit 97
FAKE_AGG
    chmod +x "$fake_bin/agg"

    run env PATH="$fake_bin:$PATH" RENDER_EXISTING=1 SKIP_SANITIZE=1 \
        GCO_RECORDING_REPO_ROOT="$fixture" bash "$SCRIPT"

    [ "$status" -ne 0 ]
    [ "$(cat "$fixture/demo/destroy.gif")" = "old gif" ]
    [ -z "$(compgen -G "$fixture/demo/.destroy-recording.*" || true)" ]
}

@test "destroy recorder prints the correct README embed text" {
    grep -q 'echo "Embed in README:"' "$SCRIPT"
    grep -F -q "echo '  ![GCO Destroy](demo/destroy.gif)'" "$SCRIPT"
}

@test "GCO_DEMO_ENABLE reaches the recorded destroy as --enable" {
    # The recorded teardown must evaluate the same app the recorded deploy did,
    # so the pair is an honest before/after rather than two different runs.
    local fixture="$BATS_TEST_TMPDIR/destroy-enable"
    local fake_bin="$fixture/bin"
    local python_file="$BATS_TEST_TMPDIR/destroy-enable-python.argv"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$fixture/demo/lib_demo.sh"
    printf '{}\n' > "$fixture/cdk.json"
    printf 'existing destroy cast\n' > "$fixture/demo/destroy.cast"
    printf 'existing destroy gif\n' > "$fixture/demo/destroy.gif"

    cat > "$fake_bin/asciinema" <<'FAKE_ASCIINEMA'
#!/usr/bin/env bash
output_file=""
child_command=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --return) shift ;;
        --cols|--rows) shift 2 ;;
        --command) child_command="$2"; shift 2 ;;
        --overwrite) shift ;;
        *) output_file="$1"; shift ;;
    esac
done
if [ -n "$child_command" ]; then
    bash -c "$child_command"
fi
printf '{"version": 2, "width": 80, "height": 24}\n' > "$output_file"
FAKE_ASCIINEMA
    cat > "$fake_bin/python3" <<'FAKE_PYTHON'
#!/usr/bin/env bash
for arg in "$@"; do
    if [ "$arg" = "destroy-all" ]; then
        printf '%s\n' "$@" > "$FAKE_PYTHON_INVOCATION_FILE"
        break
    fi
done
exit 0
FAKE_PYTHON
    cat > "$fake_bin/aws" <<'FAKE_AWS'
#!/usr/bin/env bash
printf '%s\n' '123456789012'
FAKE_AWS
    chmod +x "$fake_bin/asciinema" "$fake_bin/python3" "$fake_bin/aws"

    git -C "$fixture" init -q
    git -C "$fixture" add .
    git -C "$fixture" -c user.name=CI -c user.email=ci@example.invalid \
        commit -q -m recording-fixture
    local expected_sha
    expected_sha=$(git -C "$fixture" rev-parse HEAD)

    run env \
        PATH="$fake_bin:$PATH" \
        GCO_RECORDING_LIVE=1 \
        GCO_EXPECTED_GIT_SHA="$expected_sha" \
        GCO_EXPECTED_ACCOUNT_ID=123456789012 \
        GCO_DEMO_ENABLE="fsx_lustre,valkey,aurora_pgvector,vector_store,slurm,yunikorn" \
        SKIP_GIF=1 \
        FAKE_PYTHON_INVOCATION_FILE="$python_file" \
        GCO_RECORDING_REPO_ROOT="$fixture" bash "$SCRIPT"

    [ "$status" -eq 0 ]
    grep -Fxq -- '--enable' "$python_file"
    grep -Fxq -- 'fsx_lustre,valkey,aurora_pgvector,vector_store,slurm,yunikorn' "$python_file"
    [ "$(grep -c -- '--enable' "$python_file")" -eq 1 ]
}

# ── Guarded live runs against a fixture checkout ─────────────────────────────
# The tests above each build their own fixture to prove one contract. The ones
# below share a fixture in which every external the recorder touches is faked
# well enough for a whole guarded run to complete, so the publication path and
# each preflight and cleanup failure can be exercised for real.

make_live_fixture() {
    # Creates $FIXTURE, $FAKE_BIN and the recording files; prints nothing.
    FIXTURE="$BATS_TEST_TMPDIR/checkout"
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export ASCIINEMA_MARKER="$BATS_TEST_TMPDIR/asciinema-started"
    mkdir -p "$FIXTURE/demo" "$FAKE_BIN"
    ln -s "$LIB" "$FIXTURE/demo/lib_demo.sh"
    printf '{"context":{"project_name":"gco"}}\n' > "$FIXTURE/cdk.json"
    printf '{"version":2,"width":116,"height":36}\n[0.1,"o","previous destroy"]\n' > "$FIXTURE/demo/destroy.cast"
    printf 'previous gif\n' > "$FIXTURE/demo/destroy.gif"
    EXPECTED_SHA="$(init_fixture_repo "$FIXTURE")"

    write_stub "$FAKE_BIN" asciinema <<'FAKE_ASCIINEMA'
#!/usr/bin/env bash
: > "$ASCIINEMA_MARKER"
output_file=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --cols|--rows|--command) shift 2 ;;
        --return|--overwrite) shift ;;
        *) output_file="$1"; shift ;;
    esac
done
# Run the recorder's wrapper the way asciinema would, then write a cast whose
# output stream carries an account ID and an access-key ID for the sanitizer.
wrapper_status=0
bash --norc --noprofile "$GCO_RECORDING_WRAPPER" >/dev/null 2>&1 || wrapper_status=$?
key_id="AKIA""EXAMPLEEXAMPLE01"
printf '{"version":2,"width":116,"height":36}\n[0.5,"o","account 123456789012 key %s \xe2\x9c\x85 done"]\n' "$key_id" > "$output_file"
exit "$wrapper_status"
FAKE_ASCIINEMA
    write_stub "$FAKE_BIN" agg <<'FAKE_AGG'
#!/usr/bin/env bash
printf 'rendered gif\n' > "${!#}"
FAKE_AGG
    # Only the repository CLI (`python3 -m cli.main ...`) and the importability
    # probe are faked; the sanitizer, verifier, glyph substitution and override
    # validator run on the real interpreter with PYTHONPATH at this checkout.
    REAL_PYTHON3="$(command -v python3)"
    export REAL_PYTHON3
    export PYTHONPATH="$REPO_ROOT"
    write_stub "$FAKE_BIN" python3 <<'FAKE_PYTHON'
#!/usr/bin/env bash
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "cli.main" ]; then
    echo "gco-us-east-1 DELETE_COMPLETE"
    exit 0
fi
case "${1:-} ${2:-}" in
    "-c from cli.main"*) exit 0 ;;
esac
exec "$REAL_PYTHON3" "$@"
FAKE_PYTHON
    write_stub "$FAKE_BIN" aws <<'FAKE_AWS'
#!/usr/bin/env bash
printf '%s\n' '123456789012'
FAKE_AWS
}

run_recorder() {
    # run_recorder [VAR=value ...] — the guarded live invocation against $FIXTURE.
    run env PATH="$FAKE_BIN:$PATH" TERM=xterm \
        GCO_RECORDING_LIVE=1 \
        GCO_EXPECTED_GIT_SHA="$EXPECTED_SHA" \
        GCO_EXPECTED_ACCOUNT_ID=123456789012 \
        "$@" \
        GCO_RECORDING_REPO_ROOT="$FIXTURE" bash "$SCRIPT"
}

@test "a guarded live destroy recording publishes a sanitized cast and GIF" {
    make_live_fixture
    run_recorder

    [ "$status" -eq 0 ]
    [ -e "$ASCIINEMA_MARKER" ]
    [[ "$output" == *"This will run python3 -m cli.main stacks destroy-all -y"* ]]
    [[ "$output" == *"Cast sanitized and verified"* ]]
    [[ "$output" == *"Recording pair published: ${FIXTURE}/demo/destroy.cast"* ]]
    [[ "$output" == *"GIF published: ${FIXTURE}/demo/destroy.gif"* ]]
    grep -q '000000000000' "$FIXTURE/demo/destroy.cast"
    grep -q 'REDACTED_AWS_ACCESS_KEY_ID' "$FIXTURE/demo/destroy.cast"
    ! grep -q '123456789012' "$FIXTURE/demo/destroy.cast"
    [ "$(cat "$FIXTURE/demo/destroy.gif")" = "rendered gif" ]
    [ -z "$(compgen -G "$FIXTURE/demo/.destroy-recording.*" || true)" ]
    [ ! -e "$FIXTURE/.git/gco-legacy-recording.lock" ]
}

@test "without agg a live destroy recording warns and publishes the cast alone" {
    make_live_fixture
    rm -f "$FAKE_BIN/agg"
    local tools="$BATS_TEST_TMPDIR/tools"
    path_without "$tools" agg
    run env PATH="$FAKE_BIN:$tools" TERM=xterm \
        GCO_RECORDING_LIVE=1 GCO_EXPECTED_GIT_SHA="$EXPECTED_SHA" GCO_EXPECTED_ACCOUNT_ID=123456789012 \
        GCO_RECORDING_REPO_ROOT="$FIXTURE" bash "$SCRIPT"

    [ "$status" -eq 0 ]
    [[ "$output" == *"agg not installed — will produce .cast only"* ]]
    [[ "$output" == *"1 warnings"* ]]
    [[ "$output" != *"GIF published"* ]]
    [ ! -e "$FIXTURE/demo/destroy.gif" ]
    grep -q '000000000000' "$FIXTURE/demo/destroy.cast"
}

@test "destroy preflight lists every missing prerequisite and records nothing" {
    # No asciinema or python3 on PATH, no cdk.json, no authorization, and an
    # override that cannot be validated without python3: every check is
    # reported in one pass and neither asciinema nor the lock is touched.
    make_live_fixture
    rm -f "$FIXTURE/cdk.json" "$FAKE_BIN/asciinema" "$FAKE_BIN/python3"
    local tools="$BATS_TEST_TMPDIR/tools"
    link_tools "$tools" bash dirname git df awk head cut tput
    run env PATH="$FAKE_BIN:$tools" GCO_DEMO_ENABLE=valkey GCO_RECORDING_REPO_ROOT="$FIXTURE" bash "$SCRIPT"

    [ "$status" -eq 1 ]
    [[ "$output" == *"asciinema not installed"* ]]
    [[ "$output" == *"Repository GCO CLI module is not importable"* ]]
    [[ "$output" == *"cdk.json not found"* ]]
    [[ "$output" == *"Cannot validate GCO_DEMO_ENABLE"* ]]
    [[ "$output" == *"Live recording authorization failed"* ]]
    [[ "$output" == *"Fix the issues above before recording."* ]]
    [ ! -e "$ASCIINEMA_MARKER" ]
    [ ! -e "$FIXTURE/.git/gco-legacy-recording.lock" ]
    [ -z "$(compgen -G "$FIXTURE/demo/.destroy-recording.*" || true)" ]
}

@test "an unknown GCO_DEMO_ENABLE name is refused before anything is destroyed" {
    make_live_fixture
    run_recorder GCO_DEMO_ENABLE=valkeyy

    [ "$status" -eq 1 ]
    [[ "$output" == *"GCO_DEMO_ENABLE names an unknown feature or chart"* ]]
    [[ "$output" == *"gco stacks destroy-all --help"* ]]
    [ ! -e "$ASCIINEMA_MARKER" ]
}

@test "destroy RENDER_EXISTING must be 0 or 1" {
    make_live_fixture
    run_recorder RENDER_EXISTING=2

    [ "$status" -eq 1 ]
    [[ "$output" == *"RENDER_EXISTING must be 0 or 1"* ]]
    [ ! -e "$ASCIINEMA_MARKER" ]
}

@test "destroy render-existing refuses when there is no cast to re-render" {
    make_live_fixture
    rm -f "$FIXTURE/demo/destroy.cast"
    run env PATH="$FAKE_BIN:$PATH" RENDER_EXISTING=1 GCO_RECORDING_REPO_ROOT="$FIXTURE" bash "$SCRIPT"

    [ "$status" -eq 1 ]
    [[ "$output" == *"Existing destroy cast not found"* ]]
    [ "$(cat "$FIXTURE/demo/destroy.gif")" = "previous gif" ]
}

@test "destroy low disk space is a warning, not a refusal" {
    make_live_fixture
    write_stub "$FAKE_BIN" df <<'FAKE_DF'
#!/usr/bin/env bash
printf 'Filesystem 1M-blocks Used Available Use%% Mounted on\n'
printf 'fake 1000 950 42 95%% /\n'
FAKE_DF
    run env PATH="$FAKE_BIN:$PATH" RENDER_EXISTING=1 GCO_RECORDING_REPO_ROOT="$FIXTURE" bash "$SCRIPT"

    [ "$status" -eq 0 ]
    [[ "$output" == *"Low disk space: 42 MB"* ]]
    [ "$(cat "$FIXTURE/demo/destroy.gif")" = "rendered gif" ]
}

@test "when destroy publication fails and rollback cannot restore the pair, staging is preserved and the lock is still released" {
    make_live_fixture
    # The GIF cannot be moved into place, and the restore copy of the previous
    # cast fails too: rollback cannot complete, so the recorder must say where
    # the staged artifacts are instead of deleting them.
    write_stub "$FAKE_BIN" mv <<'FAKE_MV'
#!/usr/bin/env bash
case "${!#}" in
    */demo/destroy.gif) echo "mv: cannot move to '${!#}': Input/output error" >&2; exit 1 ;;
esac
exec /bin/mv "$@"
FAKE_MV
    write_stub "$FAKE_BIN" cp <<'FAKE_CP'
#!/usr/bin/env bash
case "${!#}" in
    */.restore-cast) echo "cp: cannot create '${!#}': Input/output error" >&2; exit 1 ;;
esac
exec /bin/cp "$@"
FAKE_CP
    run_recorder

    [ "$status" -eq 1 ]
    [[ "$output" == *"Recording publication failed and rollback could not complete."* ]]
    [[ "$output" == *"Recording publication rollback failed; preserving staging at ${FIXTURE}/demo/.destroy-recording."* ]]
    [ -n "$(compgen -G "$FIXTURE/demo/.destroy-recording.*" || true)" ]
    [ ! -e "$FIXTURE/.git/gco-legacy-recording.lock" ]
}

@test "a destroy staging directory the cleanup cannot remove fails the run after publication" {
    make_live_fixture
    write_stub "$FAKE_BIN" rm <<'FAKE_RM'
#!/usr/bin/env bash
for arg in "$@"; do
    case "$arg" in
        */.destroy-recording.*/*) ;;
        */.destroy-recording.*) echo "rm: cannot remove '$arg': Directory not empty" >&2; exit 1 ;;
    esac
done
exec /bin/rm "$@"
FAKE_RM
    run_recorder

    [ "$status" -eq 1 ]
    [[ "$output" == *"Recording pair published"* ]]
    [ -n "$(compgen -G "$FIXTURE/demo/.destroy-recording.*" || true)" ]
    [ ! -e "$FIXTURE/.git/gco-legacy-recording.lock" ]
}

@test "a destroy recording lock that cannot be released is reported" {
    make_live_fixture
    write_stub "$FAKE_BIN" rm <<'FAKE_RM'
#!/usr/bin/env bash
for arg in "$@"; do
    case "$arg" in
        */gco-legacy-recording.lock) echo "rm: cannot remove '$arg': Operation not permitted" >&2; exit 1 ;;
    esac
done
exec /bin/rm "$@"
FAKE_RM
    run_recorder

    [ "$status" -eq 1 ]
    [[ "$output" == *"Recording pair published"* ]]
    [[ "$output" == *"Unable to release legacy recording lock"* ]]
}
