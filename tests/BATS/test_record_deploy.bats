#!/usr/bin/env bats
# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for demo/record_deploy.sh
# ─────────────────────────────────────────────────────────────────────────────

load 'helpers.sh'

SCRIPT="$REPO_ROOT/demo/record_deploy.sh"
LIB="$REPO_ROOT/demo/lib_demo.sh"

@test "record_deploy.sh exists and is executable" {
    [ -f "$SCRIPT" ]
    [ -x "$SCRIPT" ]
}

@test "record_deploy.sh passes bash -n syntax check" {
    bash -n "$SCRIPT"
}

@test "record_deploy.sh passes shellcheck" {
    command -v shellcheck &>/dev/null || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "record_deploy.sh sources lib_demo.sh" {
    grep -q "source.*lib_demo.sh" "$SCRIPT"
}

@test "default speed is 15x for long deploy" {
    run bash -c 'SPEED="${DEMO_SPEED:-15}"; echo "$SPEED"'
    [ "$output" = "15" ]
}

@test "default dimensions are 140x37" {
    grep -q 'COLS="${DEMO_COLS:-140}"' "$SCRIPT"
    grep -q 'ROWS="${DEMO_ROWS:-37}"' "$SCRIPT"
}

@test "output files go to demo/ directory" {
    grep -q 'CAST_FILE=.*deploy\.cast' "$SCRIPT"
    grep -q 'GIF_FILE=.*deploy\.gif' "$SCRIPT"
}

@test "runs the repository-bound deploy command" {
    grep -q "python3 -m cli.main stacks deploy-all -y" "$SCRIPT"
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
    grep -q 'mktemp -d .*\.deploy-recording\.XXXXXX' "$SCRIPT"
    grep -q 'trap .*cleanup_recording_temps.*EXIT' "$SCRIPT"
    grep -q 'rollback_recording_publication' "$SCRIPT"
    grep -q "trap 'exit 129' HUP" "$SCRIPT"
    grep -q "trap 'exit 130' INT" "$SCRIPT"
    grep -q "trap 'exit 143' TERM" "$SCRIPT"
    grep -q 'rm -rf -- .*RECORDING_TMP_DIR' "$SCRIPT"
}

@test "uses asciinema --return to propagate the recorded deploy status" {
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

@test "failed recorded deploy leaves the existing cast and GIF unchanged" {
    local fixture="$BATS_TEST_TMPDIR/deploy recorder; literal \$checkout"
    local fake_bin="$fixture/bin"
    local argv_file="$BATS_TEST_TMPDIR/deploy-asciinema.argv"
    local python_file="$BATS_TEST_TMPDIR/deploy-python.argv"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$fixture/demo/lib_demo.sh"
    printf '{}\n' > "$fixture/cdk.json"
    printf 'existing deploy cast\n' > "$fixture/demo/deploy.cast"
    printf 'existing deploy gif\n' > "$fixture/demo/deploy.gif"

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
    [ "$(sed -n '5p' "$python_file")" = "deploy-all" ]
    [ "$(sed -n '6p' "$python_file")" = "-y" ]
    [ "$(cat "$fixture/demo/deploy.cast")" = "existing deploy cast" ]
    [ "$(cat "$fixture/demo/deploy.gif")" = "existing deploy gif" ]
    [ -z "$(compgen -G "$fixture/demo/.deploy-recording.*" || true)" ]
}


@test "deploy render-existing mode performs no AWS or asciinema call" {
    local fixture="$BATS_TEST_TMPDIR/deploy-render"
    local fake_bin="$fixture/bin"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$fixture/demo/lib_demo.sh"
    {
        printf '{"version":2,"width":140,"height":37}\n'
        printf '[0.1,"o","verified deploy cast"]\n'
    } > "$fixture/demo/deploy.cast"
    printf 'old gif\n' > "$fixture/demo/deploy.gif"

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
    [ "$(cat "$fixture/demo/deploy.gif")" = "new gif" ]
    [ -z "$(compgen -G "$fixture/demo/.deploy-recording.*" || true)" ]
}


@test "deploy render-existing without agg preserves the pair" {
    local fixture="$BATS_TEST_TMPDIR/deploy-no-agg"
    local fake_bin="$fixture/bin"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$fixture/demo/lib_demo.sh"
    printf '{"version":2,"width":140,"height":37}\n' > "$fixture/demo/deploy.cast"
    printf 'old gif\n' > "$fixture/demo/deploy.gif"

    run env PATH="$fake_bin:/usr/bin:/bin" RENDER_EXISTING=1 \
        GCO_RECORDING_REPO_ROOT="$fixture" bash "$SCRIPT"

    [ "$status" -ne 0 ]
    [ "$(cat "$fixture/demo/deploy.gif")" = "old gif" ]
    [ -z "$(compgen -G "$fixture/demo/.deploy-recording.*" || true)" ]
}

@test "deploy rejects SKIP_SANITIZE before touching artifacts" {
    local fixture="$BATS_TEST_TMPDIR/deploy-skip-sanitize"
    local fake_bin="$fixture/bin"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$fixture/demo/lib_demo.sh"
    printf '{"version":2,"width":140,"height":37}\n' > "$fixture/demo/deploy.cast"
    printf 'old gif\n' > "$fixture/demo/deploy.gif"
    cat > "$fake_bin/agg" <<'FAKE_AGG'
#!/usr/bin/env bash
exit 97
FAKE_AGG
    chmod +x "$fake_bin/agg"

    run env PATH="$fake_bin:$PATH" RENDER_EXISTING=1 SKIP_SANITIZE=1 \
        GCO_RECORDING_REPO_ROOT="$fixture" bash "$SCRIPT"

    [ "$status" -ne 0 ]
    [ "$(cat "$fixture/demo/deploy.gif")" = "old gif" ]
    [ -z "$(compgen -G "$fixture/demo/.deploy-recording.*" || true)" ]
}

@test "GCO_DEMO_ENABLE reaches the recorded deploy as --enable" {
    # The whole single-knob design rests on this argv actually carrying the
    # override; without it the recording would narrate a topology the deploy
    # never provisioned.
    local fixture="$BATS_TEST_TMPDIR/deploy-enable"
    local fake_bin="$fixture/bin"
    local python_file="$BATS_TEST_TMPDIR/deploy-enable-python.argv"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$fixture/demo/lib_demo.sh"
    printf '{}\n' > "$fixture/cdk.json"
    printf 'existing deploy cast\n' > "$fixture/demo/deploy.cast"
    printf 'existing deploy gif\n' > "$fixture/demo/deploy.gif"

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
# The recorder also uses python3 for the CLI-importability preflight and the
# override validation; only record the argv of the recorded deploy itself.
for arg in "$@"; do
    if [ "$arg" = "deploy-all" ]; then
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
    # The value must be one argv entry, not word-split into five.
    [ "$(grep -c -- '--enable' "$python_file")" -eq 1 ]
}

@test "omitting GCO_DEMO_ENABLE records a bare deploy with no --enable" {
    local fixture="$BATS_TEST_TMPDIR/deploy-noenable"
    local fake_bin="$fixture/bin"
    local python_file="$BATS_TEST_TMPDIR/deploy-noenable-python.argv"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$fixture/demo/lib_demo.sh"
    printf '{}\n' > "$fixture/cdk.json"
    printf 'existing deploy cast\n' > "$fixture/demo/deploy.cast"
    printf 'existing deploy gif\n' > "$fixture/demo/deploy.gif"

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
    if [ "$arg" = "deploy-all" ]; then
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
        SKIP_GIF=1 \
        FAKE_PYTHON_INVOCATION_FILE="$python_file" \
        GCO_RECORDING_REPO_ROOT="$fixture" bash "$SCRIPT"

    [ "$status" -eq 0 ]
    run grep -Fq -- '--enable' "$python_file"
    [ "$status" -ne 0 ]
}

@test "an invalid GCO_DEMO_ENABLE aborts before any AWS or asciinema call" {
    local fixture="$BATS_TEST_TMPDIR/deploy-badenable"
    local fake_bin="$fixture/bin"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$fixture/demo/lib_demo.sh"
    printf '{}\n' > "$fixture/cdk.json"

    cat > "$fake_bin/asciinema" <<'FAKE_ASCIINEMA'
#!/usr/bin/env bash
printf 'asciinema must not run\n' >&2
exit 99
FAKE_ASCIINEMA
    chmod +x "$fake_bin/asciinema"

    run env \
        PATH="$fake_bin:$PATH" \
        GCO_RECORDING_LIVE=1 \
        GCO_EXPECTED_GIT_SHA=0000000000000000000000000000000000000000 \
        GCO_EXPECTED_ACCOUNT_ID=123456789012 \
        GCO_DEMO_ENABLE="fsx_lustre,slurmm" \
        GCO_RECORDING_REPO_ROOT="$fixture" bash "$SCRIPT"

    [ "$status" -ne 0 ]
    [[ "$output" == *"unknown feature or chart"* ]]
    [[ "$output" != *"asciinema must not run"* ]]
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
    printf '{"version":2,"width":140,"height":37}\n[0.1,"o","previous deploy"]\n' > "$FIXTURE/demo/deploy.cast"
    printf 'previous gif\n' > "$FIXTURE/demo/deploy.gif"
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
printf '{"version":2,"width":140,"height":37}\n[0.5,"o","account 123456789012 key %s \xe2\x9c\x85 done"]\n' "$key_id" > "$output_file"
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
    echo "gco-us-east-1 CREATE_COMPLETE"
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

@test "a guarded live deploy recording publishes a sanitized cast and GIF" {
    make_live_fixture
    run_recorder

    [ "$status" -eq 0 ]
    [ -e "$ASCIINEMA_MARKER" ]
    [[ "$output" == *"This will run python3 -m cli.main stacks deploy-all -y"* ]]
    [[ "$output" == *"Cast sanitized and verified"* ]]
    [[ "$output" == *"Recording pair published: ${FIXTURE}/demo/deploy.cast"* ]]
    [[ "$output" == *"GIF published: ${FIXTURE}/demo/deploy.gif"* ]]
    grep -q '000000000000' "$FIXTURE/demo/deploy.cast"
    grep -q 'REDACTED_AWS_ACCESS_KEY_ID' "$FIXTURE/demo/deploy.cast"
    ! grep -q '123456789012' "$FIXTURE/demo/deploy.cast"
    [ "$(cat "$FIXTURE/demo/deploy.gif")" = "rendered gif" ]
    [ -z "$(compgen -G "$FIXTURE/demo/.deploy-recording.*" || true)" ]
    [ ! -e "$FIXTURE/.git/gco-legacy-recording.lock" ]
}

@test "without agg a live deploy recording warns and publishes the cast alone" {
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
    [ ! -e "$FIXTURE/demo/deploy.gif" ]
    grep -q '000000000000' "$FIXTURE/demo/deploy.cast"
}

@test "deploy preflight lists every missing prerequisite and records nothing" {
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
    [ -z "$(compgen -G "$FIXTURE/demo/.deploy-recording.*" || true)" ]
}

@test "deploy RENDER_EXISTING must be 0 or 1" {
    make_live_fixture
    run_recorder RENDER_EXISTING=2

    [ "$status" -eq 1 ]
    [[ "$output" == *"RENDER_EXISTING must be 0 or 1"* ]]
    [ ! -e "$ASCIINEMA_MARKER" ]
}

@test "deploy render-existing refuses when there is no cast to re-render" {
    make_live_fixture
    rm -f "$FIXTURE/demo/deploy.cast"
    run env PATH="$FAKE_BIN:$PATH" RENDER_EXISTING=1 GCO_RECORDING_REPO_ROOT="$FIXTURE" bash "$SCRIPT"

    [ "$status" -eq 1 ]
    [[ "$output" == *"Existing deploy cast not found"* ]]
    [ "$(cat "$FIXTURE/demo/deploy.gif")" = "previous gif" ]
}

@test "deploy low disk space is a warning, not a refusal" {
    make_live_fixture
    write_stub "$FAKE_BIN" df <<'FAKE_DF'
#!/usr/bin/env bash
printf 'Filesystem 1M-blocks Used Available Use%% Mounted on\n'
printf 'fake 1000 950 42 95%% /\n'
FAKE_DF
    run env PATH="$FAKE_BIN:$PATH" RENDER_EXISTING=1 GCO_RECORDING_REPO_ROOT="$FIXTURE" bash "$SCRIPT"

    [ "$status" -eq 0 ]
    [[ "$output" == *"Low disk space: 42 MB"* ]]
    [ "$(cat "$FIXTURE/demo/deploy.gif")" = "rendered gif" ]
}

@test "when deploy publication fails and rollback cannot restore the pair, staging is preserved and the lock is still released" {
    make_live_fixture
    # The GIF cannot be moved into place, and the restore copy of the previous
    # cast fails too: rollback cannot complete, so the recorder must say where
    # the staged artifacts are instead of deleting them.
    write_stub "$FAKE_BIN" mv <<'FAKE_MV'
#!/usr/bin/env bash
case "${!#}" in
    */demo/deploy.gif) echo "mv: cannot move to '${!#}': Input/output error" >&2; exit 1 ;;
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
    [[ "$output" == *"Recording publication rollback failed; preserving staging at ${FIXTURE}/demo/.deploy-recording."* ]]
    [ -n "$(compgen -G "$FIXTURE/demo/.deploy-recording.*" || true)" ]
    [ ! -e "$FIXTURE/.git/gco-legacy-recording.lock" ]
}

@test "a deploy staging directory the cleanup cannot remove fails the run after publication" {
    make_live_fixture
    write_stub "$FAKE_BIN" rm <<'FAKE_RM'
#!/usr/bin/env bash
for arg in "$@"; do
    case "$arg" in
        */.deploy-recording.*/*) ;;
        */.deploy-recording.*) echo "rm: cannot remove '$arg': Directory not empty" >&2; exit 1 ;;
    esac
done
exec /bin/rm "$@"
FAKE_RM
    run_recorder

    [ "$status" -eq 1 ]
    [[ "$output" == *"Recording pair published"* ]]
    [ -n "$(compgen -G "$FIXTURE/demo/.deploy-recording.*" || true)" ]
    [ ! -e "$FIXTURE/.git/gco-legacy-recording.lock" ]
}

@test "a deploy recording lock that cannot be released is reported" {
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
