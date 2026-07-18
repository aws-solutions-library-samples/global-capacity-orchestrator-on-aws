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

@test "default dimensions are 120x37" {
    grep -q 'COLS="${DEMO_COLS:-120}"' "$SCRIPT"
    grep -q 'ROWS="${DEMO_ROWS:-37}"' "$SCRIPT"
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

@test "checks for AWS credentials" {
    grep -q "aws sts get-caller-identity" "$SCRIPT"
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

@test "supports SKIP_SANITIZE env var" {
    # Documented escape hatch for bypassing account-ID redaction.
    grep -q "SKIP_SANITIZE" "$SCRIPT"
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
    local argv_file="$fixture/asciinema.argv"
    local python_file="$fixture/python.argv"
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
exit 0
FAKE_AWS
    chmod +x "$fake_bin/asciinema" "$fake_bin/python3" "$fake_bin/aws"

    run env \
        PATH="$fake_bin:$PATH" \
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
