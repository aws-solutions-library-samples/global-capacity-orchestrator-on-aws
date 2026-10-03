#!/usr/bin/env bats
# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for demo/record_deps_maintain.sh
# ─────────────────────────────────────────────────────────────────────────────
# The recorder drives `gco deps maintain --dry-run` under asciinema and renders
# a GIF with agg. Here the tracked recorder runs in place against a fixture
# checkout (GCO_RECORDING_REPO_ROOT) with every external faked on PATH:
# asciinema runs the recorder's driver for real and writes a cast from what it
# printed, agg writes a marker file, jq lifts the prompt out of the JSON plan,
# and the repository CLI is answered by a python3 that delegates everything
# else — the sanitizer, the verifier, the glyph and frame-zero passes — to the
# real interpreter. The default recording, SKIP_GIF, every preflight refusal
# and the post-recording contract failure are covered.
#
# Run:  bats tests/BATS/test_record_deps_maintain.bats
# ─────────────────────────────────────────────────────────────────────────────

load 'helpers.sh'

SCRIPT="$REPO_ROOT/demo/record_deps_maintain.sh"
LIB="$REPO_ROOT/demo/lib_demo.sh"

@test "record_deps_maintain.sh passes bash -n (including macOS Bash 3.2 when present) and shellcheck" {
    bash -n "$SCRIPT"
    [ -x /bin/bash ] && /bin/bash -n "$SCRIPT"
    command -v shellcheck &>/dev/null || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "record_deps_maintain.sh keeps the reviewed recording contract in its text" {
    grep -q 'asciinema rec \\' "$SCRIPT"
    grep -q -- '--idle-time-limit 1.5' "$SCRIPT"
    grep -q 'exec python3 -m cli.main "\$@"' "$SCRIPT"
    grep -q 'gco deps maintain --dry-run --findings' "$SCRIPT"
    grep -q 'rebase_cast_to_marker "\$CAST_FILE" "\$BANNER_TEXT"' "$SCRIPT"
    # The sample document the recording reads is committed and is a findings document.
    python3 -c "
import json, sys
doc = json.load(open('$REPO_ROOT/demo/deps-maintain-findings.json'))
sys.exit(0 if doc['schema'] == 'gco.dependency-scan.findings/1' and len(doc['surfaces']) > 20 else 1)
"
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
    printf '{"schema":"gco.dependency-scan.findings/1","surfaces":[]}\n' > "$FIXTURE/demo/deps-maintain-findings.json"
    if [ ! -d "$BATS_TEST_TMPDIR/tools" ]; then
        path_without "$BATS_TEST_TMPDIR/tools" asciinema agg python3 jq sleep gco
    fi
    RECORD_PATH="$FAKE_BIN:$BATS_TEST_TMPDIR/tools"
    REAL_PYTHON3="$(command -v python3)"
    export REAL_PYTHON3

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
"$REAL_PYTHON3" - "$captured" "$output_file" <<'PY'
import json
import sys

text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
with open(sys.argv[2], "w", encoding="utf-8") as out:
    out.write(json.dumps({"version": 2, "width": 110, "height": 34}) + "\n")
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
    # FAKE_CLI_IMPORTABLE=0 makes `python3 -m cli.main --version` fail;
    # FAKE_CLI_PLAN=broken makes the dry run print usage instead of a plan.
    write_stub "$FAKE_BIN" python3 <<'FAKE_PYTHON'
#!/usr/bin/env bash
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "cli.main" ]; then
    printf '%s\n' "${*:3}" >> "$CLI_CALLS"
    if [ "${3:-}" = "--version" ]; then
        [ "${FAKE_CLI_IMPORTABLE:-1}" = "1" ] || exit 1
        echo "gco 8.9.1"
        exit 0
    fi
    # FAKE_CLI_PLAN=usage: a CLI whose options moved prints usage and exits
    # 0, so the recorder's own contract check has to catch it.
    # FAKE_CLI_PLAN=crash: the CLI exits non-zero, which asciinema --return
    # propagates and the recorder reports.
    case "${FAKE_CLI_PLAN:-ok}" in
        usage) echo "Usage: gco deps maintain [OPTIONS]"; exit 0 ;;
        crash) echo "Error: the findings document is not a JSON object"; exit 1 ;;
    esac
    if [ "${3:-}" = "-o" ]; then
        printf '{"prompt": "# Dependency maintenance session\\n## Findings to apply\\n- [Python Packages] urllib3: 2.7.0 -> 2.8.0\\n"}\n'
    else
        echo "  GCO dependency maintenance — launch plan"
        echo "  Worktree:          ${HOME}/checkout/.worktrees/maint-deps-2026-10-01"
        echo "  Apply — mechanical (1)"
        echo "    - [Python Packages] urllib3: 2.7.0 -> 2.8.0"
        echo "  Dry run only — no worktree was created and nothing was launched."
    fi
    exit 0
fi
exec "$REAL_PYTHON3" "$@"
FAKE_PYTHON
    write_stub "$FAKE_BIN" jq <<'FAKE_JQ'
#!/usr/bin/env bash
"$REAL_PYTHON3" -c 'import json, sys
try:
    print(json.load(sys.stdin)["prompt"], end="")
except (ValueError, KeyError):
    pass'
FAKE_JQ
    stub_noop "$FAKE_BIN" sleep
}

run_recorder() {
    run env PATH="$RECORD_PATH" TERM=xterm "$@" GCO_RECORDING_REPO_ROOT="$FIXTURE" bash "$SCRIPT"
}

@test "the default recording drives the dry run, verifies the plan and prompt, and publishes the pair" {
    make_fixture
    run_recorder

    [ "$status" -eq 0 ]
    [[ "$output" == *"=== GCO Dependency Maintenance Demo Recorder ==="* ]]
    [[ "$output" == *"asciinema installed (asciinema 2.4.0)"* ]]
    [[ "$output" == *"agg installed (agg 1.5.0)"* ]]
    [[ "$output" == *"jq installed"* ]]
    [[ "$output" == *"GCO CLI importable"* ]]
    [[ "$output" == *"Repository layout looks right (sample findings present)"* ]]
    [[ "$output" == *"Recording saved: ${FIXTURE}/demo/deps-maintain.cast"* ]]
    [[ "$output" == *"✓ Plan and prompt verified in the recording"* ]]
    [[ "$output" == *"✓ Cast sanitized and verified"* ]]
    [[ "$output" == *"✓ Tofu-triggering codepoints stripped"* ]]
    [[ "$output" == *"✓ Banner is the first frame"* ]]
    [[ "$output" == *"✓ GIF saved: ${FIXTURE}/demo/deps-maintain.gif"* ]]
    [[ "$output" == *"![GCO dependency maintenance](demo/deps-maintain.gif)"* ]]
    grep -qx -- '--return' "$ASCIINEMA_ARGV"
    grep -qx -- '--idle-time-limit' "$ASCIINEMA_ARGV"
    # The driver ran both dry runs through this checkout's CLI with the fixed branch.
    grep -q -- 'deps maintain --dry-run --findings demo/deps-maintain-findings.json --branch maint/deps-2026-10-01' "$CLI_CALLS"
    grep -q -- '-o json deps maintain --dry-run' "$CLI_CALLS"
    grep -qx -- '--version' "$CLI_CALLS"
    # The cast carries the banner, the plan and the prompt excerpt, with $HOME folded.
    grep -q 'GCO Dependency Maintenance' "$FIXTURE/demo/deps-maintain.cast"
    grep -q 'Dry run only' "$FIXTURE/demo/deps-maintain.cast"
    grep -q 'Findings to apply' "$FIXTURE/demo/deps-maintain.cast"
    grep -q '~/checkout/.worktrees/maint-deps-2026-10-01' "$FIXTURE/demo/deps-maintain.cast"
    ! grep -q "$HOME/checkout" "$FIXTURE/demo/deps-maintain.cast"
    # The banner is at t=0 after the rebase.
    grep -q '^\[0.0,"o",' "$FIXTURE/demo/deps-maintain.cast"
    [ "$(cat "$FIXTURE/demo/deps-maintain.gif")" = "rendered gif" ]
}

@test "SKIP_GIF records and verifies the cast without agg" {
    make_fixture
    rm -f "$FAKE_BIN/agg"
    run_recorder SKIP_GIF=1

    [ "$status" -eq 0 ]
    [[ "$output" != *"agg"* ]]
    [[ "$output" == *"✓ Banner is the first frame"* ]]
    [[ "$output" != *"GIF saved"* ]]
    [ -f "$FIXTURE/demo/deps-maintain.cast" ]
    [ ! -e "$FIXTURE/demo/deps-maintain.gif" ]
}

@test "preflight reports every missing prerequisite in one pass" {
    make_fixture
    rm -f "$FAKE_BIN/asciinema" "$FAKE_BIN/agg" "$FAKE_BIN/jq" "$FIXTURE/demo/deps-maintain-findings.json"
    run_recorder FAKE_CLI_IMPORTABLE=0

    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ asciinema not installed"* ]]
    [[ "$output" == *"✗ agg not installed"* ]]
    [[ "$output" == *"✗ jq not installed"* ]]
    [[ "$output" == *"✗ GCO CLI not importable from this python3"* ]]
    [[ "$output" == *"✗ Repository layout unexpected"* ]]
    [[ "$output" == *"5 check(s) failed. Fix the issues above before recording."* ]]
    [ ! -s "$ASCIINEMA_ARGV" ]
}

@test "a recording that shows no plan is refused before it is rendered" {
    make_fixture
    run_recorder FAKE_CLI_PLAN=usage

    [ "$status" -eq 1 ]
    [[ "$output" == *"Recording saved:"* ]]
    [[ "$output" == *"✗ The recording does not show the launch plan and the prompt."* ]]
    [[ "$output" != *"Cast sanitized"* ]]
    [ ! -e "$FIXTURE/demo/deps-maintain.gif" ]
}

@test "a CLI that fails inside the session stops the recorder with the cast to read" {
    make_fixture
    run_recorder FAKE_CLI_PLAN=crash

    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ The recording's driver exited non-zero; the CLI failed inside the session."* ]]
    [[ "$output" == *"Read ${FIXTURE}/demo/deps-maintain.cast before re-recording."* ]]
    [[ "$output" != *"Recording saved:"* ]]
    [ -f "$FIXTURE/demo/deps-maintain.cast" ]
    [ ! -e "$FIXTURE/demo/deps-maintain.gif" ]
}
