#!/usr/bin/env bats
# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for demo/record_autopilot.sh
# ─────────────────────────────────────────────────────────────────────────────
# The recorder drives a real engine session under asciinema and renders a GIF
# with agg. Here the tracked recorder runs in place against a fixture
# checkout (GCO_RECORDING_REPO_ROOT) with every external faked on PATH:
# asciinema runs the recorder's driver for real and writes a cast from what it
# printed, expect plays a scripted TUI transcript, the repository CLI is
# answered by a python3 that delegates everything else — the sanitizer, the
# verifier, the glyph and artifact passes, the live-answer contract — to the
# real interpreter. Both engines, both modes, SKIP_GIF, every preflight
# refusal and the post-recording contract failures are covered.
#
# Run:  bats tests/BATS/test_record_autopilot.bats
# ─────────────────────────────────────────────────────────────────────────────

load 'helpers.sh'

SCRIPT="$REPO_ROOT/demo/record_autopilot.sh"
LIB="$REPO_ROOT/demo/lib_demo.sh"

@test "record_autopilot.sh passes bash -n (including macOS Bash 3.2 when present) and shellcheck" {
    bash -n "$SCRIPT"
    [ -x /bin/bash ] && /bin/bash -n "$SCRIPT"
    command -v shellcheck &>/dev/null || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "record_autopilot.sh keeps the reviewed live-recording contract in its text" {
    grep -q 'asciinema rec \\' "$SCRIPT"
    grep -q -- '--idle-time-limit 1.5' "$SCRIPT"
    grep -q 'spawn gco autopilot -- --allowedTools mcp__gco' "$SCRIPT"
    grep -q 'exec python3 -m cli.main "\$@"' "$SCRIPT"
}

make_fixture() {
    FIXTURE="$BATS_TEST_TMPDIR/checkout"
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export CLI_CALLS="$BATS_TEST_TMPDIR/cli-calls"
    export ASCIINEMA_ARGV="$BATS_TEST_TMPDIR/asciinema-argv"
    : > "$CLI_CALLS"
    : > "$ASCIINEMA_ARGV"
    mkdir -p "$FIXTURE/demo" "$FAKE_BIN"
    ln -s "$LIB" "$FIXTURE/demo/lib_demo.sh"
    printf '{"context":{"project_name":"gco"}}\n' > "$FIXTURE/cdk.json"
    # Everything the recorder and its driver call is either faked in
    # $FAKE_BIN or reached through a PATH mirror that hides the machine's own
    # copies, so removing a fake makes the tool absent everywhere.
    if [ ! -d "$BATS_TEST_TMPDIR/tools" ]; then
        path_without "$BATS_TEST_TMPDIR/tools" asciinema agg python3 expect aws claude codex uvx npx sleep gco
    fi
    RECORD_PATH="$FAKE_BIN:$BATS_TEST_TMPDIR/tools"
    REAL_PYTHON3="$(command -v python3)"
    export REAL_PYTHON3

    # FAKE_CAST_VERSION: 2 (default) or 3; FAKE_CAST_EXIT: the v3 exit event.
    write_stub "$FAKE_BIN" asciinema <<'FAKE_ASCIINEMA'
#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then echo "asciinema 2.4.0"; exit 0; fi
printf '%s\n' "$@" > "$ASCIINEMA_ARGV"
output_file=""
command=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --command) command="$2"; shift 2 ;;
        --cols|--rows|--idle-time-limit) shift 2 ;;
        --return|--overwrite) shift ;;
        *) output_file="$1"; shift ;;
    esac
done
captured="$(mktemp)"
status=0
bash -c "$command" > "$captured" 2>&1 || status=$?
"$REAL_PYTHON3" - "$captured" "$output_file" "${FAKE_CAST_VERSION:-2}" "${FAKE_CAST_EXIT:-0}" <<'PY'
import json
import sys

text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
version = int(sys.argv[3])
with open(sys.argv[2], "w", encoding="utf-8") as out:
    if version == 3:
        out.write(json.dumps({"version": 3, "term": {"cols": 110, "rows": 30}}) + "\n")
        out.write(json.dumps([0.5, "o", "\x1b[H"]) + "\n")
        out.write(json.dumps([0.5, "o", text], ensure_ascii=False) + "\n")
        out.write(json.dumps([0.1, "x", sys.argv[4]]) + "\n")
    else:
        out.write(json.dumps({"version": 2, "width": 110, "height": 30}) + "\n")
        out.write(json.dumps([0.5, "o", "\x1b[H"]) + "\n")
        out.write(json.dumps([1.0, "o", text], ensure_ascii=False) + "\n")
PY
rm -f "$captured"
exit "$status"
FAKE_ASCIINEMA
    write_stub "$FAKE_BIN" agg <<'FAKE_AGG'
#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then echo "agg 1.5.0"; exit 0; fi
printf 'rendered gif\n' > "${!#}"
FAKE_AGG
    # FAKE_CLI_IMPORTABLE=0 makes `python3 -m cli.main --version` fail.
    write_stub "$FAKE_BIN" python3 <<'FAKE_PYTHON'
#!/usr/bin/env bash
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "cli.main" ]; then
    printf '%s\n' "${*:3}" >> "$CLI_CALLS"
    case "${3:-}" in
        --version)
            [ "${FAKE_CLI_IMPORTABLE:-1}" = "1" ] || exit 1
            echo "gco 7.6.4"
            ;;
        autopilot)
            echo "Engine:       ${*:4}"
            echo "Model:        global.anthropic.claude-opus-5"
            echo "MCP servers:  gco, aws-docs, filesystem"
            echo "Install pin:  npm install -g @anthropic-ai/claude-code@2.1.252"
            ;;
    esac
    exit 0
fi
exec "$REAL_PYTHON3" "$@"
FAKE_PYTHON
    # The scripted TUI: a transcript with the markers the post-recording
    # contract looks for. FAKE_EXPECT_EXIT ends the session with that status,
    # FAKE_EXPECT_ANSWER=stalled leaves the answer out, FAKE_EXPECT_LEAK=1
    # echoes the secret access key the way a careless TUI might.
    write_stub "$FAKE_BIN" expect <<'FAKE_EXPECT'
#!/usr/bin/env bash
script="${2:-}"
if grep -q -- '--engine codex' "$script" 2>/dev/null; then
    echo "Working"
    echo "Calling gco.find_docs"
    echo "Called gco.find_docs"
    echo "Called gco.read_resource"
    if [ "${FAKE_EXPECT_ANSWER:-answered}" = "answered" ]; then
        echo "gco jobs submit-sqs job.yaml — recommended because the queue is durable, asynchronous and retried."
    fi
else
    echo "Welcome to Claude Code!"
    echo "> Which gco command submits a job via SQS?"
    printf '\xe2\x8f\xba gco - find_docs (MCP)\n\xe2\x8e\xbf  read docs/CLI.md\n'
    if [ "${FAKE_EXPECT_ANSWER:-answered}" = "answered" ]; then
        printf '\xe2\x8f\xba Use gco jobs submit-sqs job.yaml \xe2\x80\x94 the queue is durable and retried.\n'
    fi
fi
[ "${FAKE_EXPECT_LEAK:-0}" = "1" ] && echo "export AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:-}"
exit "${FAKE_EXPECT_EXIT:-0}"
FAKE_EXPECT
    write_stub "$FAKE_BIN" aws <<'FAKE_AWS'
#!/usr/bin/env bash
[ "${FAKE_AWS_CREDENTIALS:-ok}" = "ok" ] || exit 253
echo '{"Account": "123456789012"}'
FAKE_AWS
    write_stub "$FAKE_BIN" claude <<'FAKE_CLAUDE'
#!/usr/bin/env bash
echo "2.1.252 (Claude Code)"
FAKE_CLAUDE
    write_stub "$FAKE_BIN" codex <<'FAKE_CODEX'
#!/usr/bin/env bash
echo "codex-cli 0.152.0"
FAKE_CODEX
    stub_noop "$FAKE_BIN" uvx npx sleep
}

run_recorder() {
    # run_recorder [VAR=value ...] — the recorder against $FIXTURE.
    run env PATH="$RECORD_PATH" TERM=xterm "$@" GCO_RECORDING_REPO_ROOT="$FIXTURE" bash "$SCRIPT"
}

@test "the default live Claude recording drives the TUI, verifies the answer and publishes the pair" {
    make_fixture
    run_recorder

    [ "$status" -eq 0 ]
    [[ "$output" == *"=== GCO Autopilot Demo Recorder (claude-code, live) ==="* ]]
    [[ "$output" == *"asciinema installed (asciinema 2.4.0)"* ]]
    [[ "$output" == *"Claude Code installed (2.1.252 (Claude Code))"* ]]
    [[ "$output" == *"uvx installed"* ]]
    [[ "$output" == *"npx installed"* ]]
    [[ "$output" == *"expect installed (drives the interactive TUI)"* ]]
    [[ "$output" == *"AWS credentials resolve"* ]]
    [[ "$output" == *"Recording saved: ${FIXTURE}/demo/autopilot-claude-code.cast"* ]]
    [[ "$output" == *"✓ Live answer verified in the recording (mentions submit-sqs)"* ]]
    [[ "$output" == *"✓ Cast sanitized and verified"* ]]
    [[ "$output" == *"✓ Terminal query/response artifacts stripped, TUI tofu glyphs substituted"* ]]
    [[ "$output" == *"✓ GIF saved: ${FIXTURE}/demo/autopilot-claude-code.gif"* ]]
    [[ "$output" == *"![GCO Autopilot](demo/autopilot-claude-code.gif)"* ]]
    # asciinema got the reviewed argv; the driver it ran printed the banner
    # and the scripted session into the cast, with the tofu glyphs rewritten.
    grep -qx -- '--return' "$ASCIINEMA_ARGV"
    grep -qx -- '--idle-time-limit' "$ASCIINEMA_ARGV"
    grep -q 'GCO Autopilot' "$FIXTURE/demo/autopilot-claude-code.cast"
    grep -q 'submit-sqs' "$FIXTURE/demo/autopilot-claude-code.cast"
    grep -q $'\u25cf gco - find_docs' "$FIXTURE/demo/autopilot-claude-code.cast"
    ! grep -q $'\u23fa' "$FIXTURE/demo/autopilot-claude-code.cast"
    [ "$(cat "$FIXTURE/demo/autopilot-claude-code.gif")" = "rendered gif" ]
    # The recording exercised this checkout's CLI, never an installed gco.
    grep -qx -- '--version' "$CLI_CALLS"
}

@test "the live Codex recording uses the docs-only expect driver and the stricter contract" {
    make_fixture
    run_recorder DEMO_ENGINE=codex FAKE_CAST_VERSION=3

    [ "$status" -eq 0 ]
    [[ "$output" == *"=== GCO Autopilot Demo Recorder (codex, live) ==="* ]]
    [[ "$output" == *"Codex installed (codex-cli 0.152.0)"* ]]
    [[ "$output" != *"uvx installed"* ]]
    [[ "$output" == *"✓ Live Codex recording verified (GCO docs tools only; no credentials/prompts)"* ]]
    [[ "$output" == *"![GCO Autopilot](demo/autopilot-codex.gif)"* ]]
    grep -q 'GCO Autopilot — Codex' "$FIXTURE/demo/autopilot-codex.cast"
    grep -q 'Called gco.read_resource' "$FIXTURE/demo/autopilot-codex.cast"
    # An asciicast v3 keeps its exit event and starts at the banner.
    grep -q '"version": *3' "$FIXTURE/demo/autopilot-codex.cast"
    grep -q '\[0.1,"x","0"\]\|\[0,"x","0"\]' "$FIXTURE/demo/autopilot-codex.cast"
}

@test "plan mode records the credential-free dry run for either engine" {
    make_fixture
    run_recorder DEMO_MODE=plan SKIP_GIF=1
    [ "$status" -eq 0 ]
    [[ "$output" == *"=== GCO Autopilot Demo Recorder (claude-code, plan) ==="* ]]
    [[ "$output" != *"expect installed"* ]]
    [[ "$output" != *"AWS credentials"* ]]
    [[ "$output" != *"Live answer verified"* ]]
    [[ "$output" != *"GIF saved"* ]]
    grep -qx -- 'autopilot --dry-run' "$CLI_CALLS"
    grep -q 'Install pin:' "$FIXTURE/demo/autopilot-claude-code.cast"
    [ ! -e "$FIXTURE/demo/autopilot-claude-code.gif" ]

    : > "$CLI_CALLS"
    run_recorder DEMO_MODE=plan DEMO_ENGINE=codex
    [ "$status" -eq 0 ]
    grep -qx -- 'autopilot --engine codex --dry-run' "$CLI_CALLS"
    grep -q 'GCO Autopilot — Codex' "$FIXTURE/demo/autopilot-codex.cast"
    [ "$(cat "$FIXTURE/demo/autopilot-codex.gif")" = "rendered gif" ]
}

@test "an unknown engine or mode is refused before preflight" {
    make_fixture
    run_recorder DEMO_ENGINE=gemini
    [ "$status" -eq 1 ]
    [[ "$output" == *"error: DEMO_ENGINE must be 'claude-code' or 'codex', got 'gemini'"* ]]

    run_recorder DEMO_MODE=rehearsal
    [ "$status" -eq 1 ]
    [[ "$output" == *"error: DEMO_MODE must be 'live' or 'plan', got 'rehearsal'"* ]]
    [ ! -s "$ASCIINEMA_ARGV" ]
}

@test "preflight reports every missing prerequisite of a live Claude recording in one pass" {
    make_fixture
    rm -f "$FAKE_BIN/asciinema" "$FAKE_BIN/agg" "$FAKE_BIN/claude" "$FAKE_BIN/uvx" "$FAKE_BIN/npx" "$FAKE_BIN/expect" "$FIXTURE/cdk.json"
    run_recorder FAKE_CLI_IMPORTABLE=0 FAKE_AWS_CREDENTIALS=missing

    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ asciinema not installed"* ]]
    [[ "$output" == *"✗ agg not installed"* ]]
    [[ "$output" == *"✗ GCO CLI not importable from this python3"* ]]
    [[ "$output" == *"✗ Repository layout unexpected"* ]]
    [[ "$output" == *"✗ Claude Code not installed (live mode launches a real session)"* ]]
    [[ "$output" == *"Run 'gco autopilot -y' once to install the pin, or set DEMO_MODE=plan"* ]]
    [[ "$output" == *"✗ uvx not installed (Claude live mode starts companions)"* ]]
    [[ "$output" == *"✗ npx not installed (Claude live mode starts companions)"* ]]
    [[ "$output" == *"✗ expect not installed (live mode scripts the TUI)"* ]]
    [[ "$output" == *"✗ No AWS credentials (live mode makes a real Bedrock call)"* ]]
    [[ "$output" == *"9 check(s) failed. Fix the issues above before recording."* ]]
    [ ! -s "$ASCIINEMA_ARGV" ]
}

@test "a missing Codex binary names its own install hint, and SKIP_GIF waives agg" {
    make_fixture
    rm -f "$FAKE_BIN/codex" "$FAKE_BIN/agg"
    run_recorder DEMO_ENGINE=codex SKIP_GIF=1

    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ Codex not installed (live mode launches a real session)"* ]]
    [[ "$output" == *"Run 'gco autopilot --engine codex -y' once to install the pin, or set DEMO_MODE=plan"* ]]
    [[ "$output" != *"agg not installed"* ]]
    [[ "$output" == *"1 check(s) failed."* ]]
}

@test "a session the expect driver abandons propagates its exit status and publishes nothing" {
    make_fixture
    run_recorder FAKE_EXPECT_EXIT=6

    [ "$status" -eq 6 ]
    [[ "$output" != *"Recording saved"* ]]
    [ ! -e "$FIXTURE/demo/autopilot-claude-code.gif" ]
}

@test "a live recording without the grounded answer fails the contract and renders no GIF" {
    make_fixture
    run_recorder FAKE_EXPECT_ANSWER=stalled

    [ "$status" -eq 1 ]
    [[ "$output" == *"Recording saved"* ]]
    [[ "$output" == *"✗ The recording failed its required answer/tool/security contract."* ]]
    [[ "$output" == *"The session may have stalled, used another tool, prompted, or exposed credentials."* ]]
    [ ! -e "$FIXTURE/demo/autopilot-claude-code.gif" ]
}

@test "a live recording that exposes a credential value or exits abnormally fails the contract" {
    make_fixture
    run_recorder FAKE_EXPECT_LEAK=1 AWS_SECRET_ACCESS_KEY=fake-secret-value-for-the-leak-test
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ The recording failed its required answer/tool/security contract."* ]]

    run_recorder DEMO_ENGINE=codex FAKE_CAST_VERSION=3 FAKE_CAST_EXIT=1
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ The recording failed its required answer/tool/security contract."* ]]
}
