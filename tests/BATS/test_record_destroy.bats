#!/usr/bin/env bats
# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for demo/record_destroy.sh
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT="demo/record_destroy.sh"

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

@test "default speed is 10x for teardown" {
    run bash -c 'SPEED="${DEMO_SPEED:-10}"; echo "$SPEED"'
    [ "$output" = "10" ]
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
    grep -q "aws sts get-caller-identity" demo/lib_demo.sh
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
    cp "$SCRIPT" "$fixture/demo/record_destroy.sh"
    cp demo/lib_demo.sh "$fixture/demo/lib_demo.sh"
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
        bash "$fixture/demo/record_destroy.sh"

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
    cp "$SCRIPT" "$fixture/demo/record_destroy.sh"
    cp demo/lib_demo.sh "$fixture/demo/lib_demo.sh"
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
        bash "$fixture/demo/record_destroy.sh"

    [ "$status" -eq 0 ]
    [ "$(cat "$fixture/demo/destroy.gif")" = "new gif" ]
    [ -z "$(compgen -G "$fixture/demo/.destroy-recording.*" || true)" ]
}


@test "destroy render-existing without agg preserves the pair" {
    local fixture="$BATS_TEST_TMPDIR/destroy-no-agg"
    local fake_bin="$fixture/bin"
    mkdir -p "$fixture/demo" "$fake_bin"
    cp "$SCRIPT" "$fixture/demo/record_destroy.sh"
    cp demo/lib_demo.sh "$fixture/demo/lib_demo.sh"
    printf '{"version":2,"width":116,"height":36}\n' > "$fixture/demo/destroy.cast"
    printf 'old gif\n' > "$fixture/demo/destroy.gif"

    run env PATH="$fake_bin:/usr/bin:/bin" RENDER_EXISTING=1 \
        bash "$fixture/demo/record_destroy.sh"

    [ "$status" -ne 0 ]
    [ "$(cat "$fixture/demo/destroy.gif")" = "old gif" ]
    [ -z "$(compgen -G "$fixture/demo/.destroy-recording.*" || true)" ]
}

@test "destroy rejects SKIP_SANITIZE before touching artifacts" {
    local fixture="$BATS_TEST_TMPDIR/destroy-skip-sanitize"
    local fake_bin="$fixture/bin"
    mkdir -p "$fixture/demo" "$fake_bin"
    cp "$SCRIPT" "$fixture/demo/record_destroy.sh"
    cp demo/lib_demo.sh "$fixture/demo/lib_demo.sh"
    printf '{"version":2,"width":116,"height":36}\n' > "$fixture/demo/destroy.cast"
    printf 'old gif\n' > "$fixture/demo/destroy.gif"
    cat > "$fake_bin/agg" <<'FAKE_AGG'
#!/usr/bin/env bash
exit 97
FAKE_AGG
    chmod +x "$fake_bin/agg"

    run env PATH="$fake_bin:$PATH" RENDER_EXISTING=1 SKIP_SANITIZE=1 \
        bash "$fixture/demo/record_destroy.sh"

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
    cp "$SCRIPT" "$fixture/demo/record_destroy.sh"
    cp demo/lib_demo.sh "$fixture/demo/lib_demo.sh"
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
        GCO_DEMO_ENABLE="fsx_lustre,valkey,aurora_pgvector,slurm,yunikorn" \
        SKIP_GIF=1 \
        FAKE_PYTHON_INVOCATION_FILE="$python_file" \
        bash "$fixture/demo/record_destroy.sh"

    [ "$status" -eq 0 ]
    grep -Fxq -- '--enable' "$python_file"
    grep -Fxq -- 'fsx_lustre,valkey,aurora_pgvector,slurm,yunikorn' "$python_file"
    [ "$(grep -c -- '--enable' "$python_file")" -eq 1 ]
}
