#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Record the GCO Autopilot demo as an animated GIF
# ─────────────────────────────────────────────────────────────────────────────
# Records a short terminal session of `gco autopilot` and converts it to a
# GIF with agg. Two modes, selected by DEMO_MODE:
#
#   live (default)  A real one-shot session: autopilot launches Claude Code
#                   on Amazon Bedrock in print mode (`-- -p "<question>"`)
#                   and the model answers a GCO question grounded by the GCO
#                   MCP server's tools. Requires the `claude` binary and AWS
#                   credentials with Bedrock access for GCO's default model.
#                   This is the recording embedded at the top of the README.
#   plan            Credential-free fallback: records `--dry-run` resolving
#                   the launch plan (model, MCP server set, install offer).
#
# The recording drives the *checked-out* CLI through a `gco` PATH shim
# (`python3 -m cli.main`), never a globally installed gco, so the GIF always
# reflects the code in this working tree. Model latency is compressed by
# asciinema's --idle-time-limit, so the live mode stays a short GIF.
#
# Output files (deposited in demo/):
#   demo/autopilot.cast  — asciinema recording (lightweight JSON text)
#   demo/autopilot.gif   — animated GIF for embedding in READMEs
#
# Prerequisites:
#   - asciinema: brew install asciinema  (or pip install asciinema)
#   - agg:       brew install agg        (or cargo install agg)
#   - python3 with the repo's dependencies importable (dev container, or
#     an environment where `python3 -m cli.main --help` works)
#
# Usage:
#   bash demo/record_autopilot.sh
#
# Options (via environment variables):
#   DEMO_MODE=live       "live" (real Bedrock-backed answer, default) or
#                        "plan" (credential-free --dry-run recording)
#   DEMO_COLS=110        Terminal width for recording (default: 110)
#   DEMO_ROWS=30         Terminal height for recording (default: 30)
#   DEMO_SPEED=1.6       Playback speed multiplier for GIF (default: 1.6)
#   DEMO_THEME=monokai   agg color theme (default: monokai)
#   DEMO_FONT_FAMILY     agg font fallback chain (default: see lib_demo.sh)
#   SKIP_GIF=1           Only produce the .cast file, skip GIF conversion
#   SKIP_SANITIZE=1      Skip AWS-account-ID redaction (debugging only)
#   SKIP_EMOJI_STRIP=1   Skip emoji substitution (debugging only)
#
# The recorded .cast is post-processed exactly like the other demo
# recordings before the GIF is rendered: sanitize_cast redacts anything
# shaped like an AWS account ID or access-key ID (verified afterwards by
# verify_cast_sanitized), and strip_emoji_from_cast rewrites codepoints
# agg's text engine can't render. See demo/lib_demo.sh.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=demo/lib_demo.sh
source "${SCRIPT_DIR}/lib_demo.sh"
setup_colors

CAST_FILE="${SCRIPT_DIR}/autopilot.cast"
GIF_FILE="${SCRIPT_DIR}/autopilot.gif"

COLS="${DEMO_COLS:-110}"
ROWS="${DEMO_ROWS:-30}"
SPEED="${DEMO_SPEED:-1.6}"
THEME="${DEMO_THEME:-monokai}"
DEMO_MODE="${DEMO_MODE:-live}"

case "$DEMO_MODE" in
    live|plan) : ;;
    *) echo "error: DEMO_MODE must be 'live' or 'plan', got '$DEMO_MODE'" >&2; exit 1 ;;
esac

# ── Preflight Checks ────────────────────────────────────────────────────────

PREFLIGHT_FAIL=0

preflight_pass() {
    echo "  ${GREEN}${BOLD}✓${RESET} $1"
}

preflight_fail() {
    echo "  ${RED}${BOLD}✗${RESET} $1"
    echo "    ${DIM}Fix: $2${RESET}"
    PREFLIGHT_FAIL=$((PREFLIGHT_FAIL + 1))
}

echo "=== GCO Autopilot Demo Recorder ==="
echo ""

if command -v asciinema &>/dev/null; then
    preflight_pass "asciinema installed ($(asciinema --version 2>&1 | head -1))"
else
    preflight_fail "asciinema not installed" \
        "brew install asciinema  (macOS) or  pip install asciinema  (Linux)"
fi

if [ "${SKIP_GIF:-}" != "1" ]; then
    if command -v agg &>/dev/null; then
        preflight_pass "agg installed ($(agg --version 2>&1 | head -1))"
    else
        preflight_fail "agg not installed" \
            "brew install agg  (macOS) or  cargo install agg  (Rust), or set SKIP_GIF=1"
    fi
fi

if (cd "$REPO_ROOT" && python3 -m cli.main --version &>/dev/null); then
    preflight_pass "GCO CLI importable (python3 -m cli.main)"
else
    preflight_fail "GCO CLI not importable from this python3" \
        "Run inside the dev container, or install the repo's deps (pip install -e .)"
fi

if [ -f "${SCRIPT_DIR}/lib_demo.sh" ] && [ -f "${REPO_ROOT}/cdk.json" ]; then
    preflight_pass "Repository layout looks right"
else
    preflight_fail "Repository layout unexpected" "Run from a full GCO checkout"
fi

if [ "$DEMO_MODE" = "live" ]; then
    if command -v claude &>/dev/null; then
        preflight_pass "Claude Code installed ($(claude --version 2>&1 | head -1))"
    else
        preflight_fail "Claude Code not installed (live mode launches a real session)" \
            "Run 'gco autopilot -y' once to install the pinned release, or set DEMO_MODE=plan"
    fi
    if command -v expect &>/dev/null; then
        preflight_pass "expect installed (drives the interactive TUI)"
    else
        preflight_fail "expect not installed (live mode scripts the TUI)" \
            "macOS ships /usr/bin/expect; on Linux: apt install expect. Or set DEMO_MODE=plan"
    fi
    if aws sts get-caller-identity &>/dev/null; then
        preflight_pass "AWS credentials resolve (Bedrock access is exercised by the recording)"
    else
        preflight_fail "No AWS credentials (live mode makes a real Bedrock call)" \
            "Configure credentials with Bedrock model access, or set DEMO_MODE=plan"
    fi
fi

if [ "$PREFLIGHT_FAIL" -gt 0 ]; then
    echo ""
    echo "  ${RED}${BOLD}${PREFLIGHT_FAIL} check(s) failed. Fix the issues above before recording.${RESET}"
    exit 1
fi

echo ""

# ── Build the demo driver ───────────────────────────────────────────────────
# A `gco` PATH shim keeps the on-screen command honest (`$ gco autopilot …`)
# while guaranteeing the recording exercises this checkout's code.

SHIM_DIR="$(mktemp -d)"
DRIVER="$(mktemp)"
EXPECT_SCRIPT=""
trap 'rm -rf "$SHIM_DIR" "$DRIVER" ${EXPECT_SCRIPT:+"$EXPECT_SCRIPT"}' EXIT

cat > "${SHIM_DIR}/gco" <<'GCO_SHIM'
#!/usr/bin/env bash
exec python3 -m cli.main "$@"
GCO_SHIM
chmod +x "${SHIM_DIR}/gco"

if [ "$DEMO_MODE" = "live" ]; then
    # A real interactive session, driven end-to-end: expect(1) spawns the
    # actual `gco autopilot` TUI, types a question with human-ish pacing,
    # approves the GCO MCP tool-permission dialog on camera (the security
    # model is part of the demo), waits for the grounded answer, and exits
    # with /exit. Timing-based matches keep it robust to cosmetic TUI
    # changes; the post-recording check below verifies the answer actually
    # landed before the GIF is rendered.
    EXPECT_SCRIPT="$(mktemp)"
    cat > "$EXPECT_SCRIPT" <<'EXPECT_DRIVER'
#!/usr/bin/expect -f
set timeout 300
set stty_init "rows 30 columns 110"
# Human-ish typing: avg 80ms/char, 400ms max — visible but not sluggish.
set send_human {0.08 0.12 1 0.02 0.4}

# Types a question word-by-word. Per-character typing (send -h) makes the
# TUI composer redraw hundreds of times, which both bloats the cast and
# renders glitchy under agg; word chunks keep a live typing feel with ~20x
# fewer redraws.
proc type_words {text} {
    foreach word [split $text " "] {
        send -- "$word "
        sleep 0.1
    }
}

# The GCO MCP server's tools are pre-approved for the session with
# claude's own --allowedTools flag (through autopilot's passthrough), so
# the recording is deterministic — no version-specific permission-dialog
# text to script against. The displayed command shows exactly this.
# log_user is toggled off around spawn so expect's own echo of the spawn
# line doesn't appear in the recording (the driver already printed the
# pretty prompt line).
log_user 0
spawn gco autopilot -- --allowedTools mcp__gco
log_user 1

# Autopilot's own resume prompt (when this workspace has previous
# sessions), first-run dialogs if any (workspace trust, theme picker),
# then wait for the input prompt. Every match keeps consuming until the
# composer is up; a quiet timeout just falls through to the settle sleep.
expect {
    -re {Resume your previous Claude Code session} { sleep 2; send "n\r"; exp_continue }
    -nocase -re {trust the files|do you trust} { send "\r"; exp_continue }
    -nocase -re {choose the text style|select theme} { send "\r"; exp_continue }
    -re {\? for shortcuts|Try "} {}
    timeout {}
}
sleep 4

type_words "Which gco command submits a job via SQS, and why is that the recommended path? Answer briefly, using only the GCO MCP doc tools (no shell commands)."
sleep 1
send -- "\r"

# Wait for the grounded answer itself (it inevitably names submit-sqs),
# then let it finish rendering (recorded idle is capped at 2s). A stray
# permission dialog is still answered, belt-and-suspenders.
expect {
    -nocase -re {do you want to (proceed|allow)|allow this tool} { sleep 2; send "2"; exp_continue }
    -timeout 240 -re {submit-sqs} {}
    timeout {}
}
sleep 30

send -- "/exit\r"
expect eof
EXPECT_DRIVER

    cat > "$DRIVER" <<DRIVER_SCRIPT
#!/usr/bin/env bash
set -euo pipefail
cd "\$REPO_ROOT"
export PATH="\${SHIM_DIR}:\${PATH}"
# tput cols runs inside command substitutions in lib_demo.sh, where stdout
# is a pipe rather than the recording PTY, so it falls back to 80 unless
# COLUMNS is exported. Without this the banner renders 80 wide on a
# ${COLS}-column recording and sits awkwardly off-center.
export COLUMNS="\${COLS}" LINES="\${ROWS}"

# shellcheck source=demo/lib_demo.sh
source "\${REPO_ROOT}/demo/lib_demo.sh"
setup_colors

banner "GCO Autopilot"
narrate "One command turns your terminal into a working Claude Code setup:"
narrate "Claude Code on Amazon Bedrock + the GCO MCP server + companion MCPs."
sleep 3

echo ""
echo "  \${MAGENTA}\\\$ \${WHITE}\${BOLD}gco autopilot -- --allowedTools mcp__gco\${RESET}"
sleep 1

expect -f "$EXPECT_SCRIPT"

# The TUI leaves residual chrome behind on exit; give the outro its own
# clean screen instead of printing into the leftovers.
printf '\033[2J\033[H'
banner "GCO Autopilot"
spacer
highlight "A real session: the model grounded its answer in GCO's MCP server."
narrate "Sessions resume next launch; import your own skills with --skills."
narrate "Get started:  gco autopilot"
sleep 4
DRIVER_SCRIPT
else
    cat > "$DRIVER" <<'DRIVER_SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
cd "$REPO_ROOT"
export PATH="${SHIM_DIR}:${PATH}"
# See the live driver: COLUMNS keeps tput-in-substitution honest so the
# banner spans the full recording width.
export COLUMNS="${COLS}" LINES="${ROWS}"

# shellcheck source=demo/lib_demo.sh
source "${REPO_ROOT}/demo/lib_demo.sh"
setup_colors

banner "GCO Autopilot"
narrate "One command from a plain terminal to a working Claude Code setup:"
narrate "Claude Code + the GCO MCP server + the recommended companion MCPs,"
narrate "on Amazon Bedrock with GCO's canonical default model."
sleep 3

run_cmd "gco autopilot --dry-run"
sleep 4

spacer
highlight "That's the whole setup. Launch it for real with:  gco autopilot"
narrate "Missing Claude Code? Autopilot offers the exact pinned install first."
sleep 3
DRIVER_SCRIPT
fi
chmod +x "$DRIVER"

# ── Record ───────────────────────────────────────────────────────────────────

echo "Recording autopilot demo (${COLS}x${ROWS})..."
echo "Output: ${CAST_FILE}"
echo ""

rm -f "$CAST_FILE"

# --idle-time-limit caps recorded pauses (model thinking time in live mode)
# so the GIF stays short without editing the cast by hand.
export REPO_ROOT SHIM_DIR COLS ROWS
asciinema rec \
    --cols "$COLS" \
    --rows "$ROWS" \
    --idle-time-limit 1.5 \
    --overwrite \
    --command "bash --norc --noprofile $DRIVER" \
    "$CAST_FILE"

echo ""
echo "✓ Recording saved: ${CAST_FILE}"

# In live mode, prove the answer actually landed before rendering: the
# TUI drive is timing-based, so a slow model or a changed dialog could
# produce a cast that cuts off early. Fail loudly instead of publishing it.
if [ "$DEMO_MODE" = "live" ]; then
    # TUI redraws interleave ANSI escapes and can split words across output
    # events, so the check joins the rendered stream, strips escapes, and
    # normalizes to alphanumerics before searching for the answer.
    if python3 - "$CAST_FILE" <<'PYEOF'
import json
import re
import sys
from pathlib import Path

stream = []
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    doc = json.loads(line)
    if isinstance(doc, list) and len(doc) >= 3 and doc[1] == "o":
        stream.append(doc[2])
joined = "".join(stream)
plain = re.sub(
    r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\\\)|\x1b[P^_].*?\x1b\\\\|\x1b.",
    "",
    joined,
)
normalized = re.sub(r"[^a-z0-9]", "", plain.lower())
raise SystemExit(0 if "submitsqs" in normalized else 1)
PYEOF
    then
        echo "✓ Live answer verified in the recording (mentions submit-sqs)"
    else
        echo "✗ The recording does not contain the expected answer content." >&2
        echo "  The session may have stalled or a dialog changed. Re-run the recorder." >&2
        exit 1
    fi
fi

# ── Sanitize and verify ─────────────────────────────────────────────────────

sanitize_cast "$CAST_FILE"
verify_cast_sanitized "$CAST_FILE"
echo "✓ Cast sanitized and verified (AWS account IDs → 000000000000)"

strip_emoji_from_cast "$CAST_FILE"
echo "✓ Tofu-triggering codepoints stripped"

# ── Strip terminal query/response artifacts ─────────────────────────────────
# The Claude Code TUI probes the terminal (focus tracking, OSC 11 background
# color, device attributes, XTVERSION), and pieces of those query/response
# exchanges land in the recorded output stream. agg's renderer doesn't
# understand them and paints fragments like ``^[[O`` or ``^[]11;rgb:...``
# literally. They carry no visual content, so they are removed outright.
python3 - "$CAST_FILE" <<'PYEOF'
import json
import re
import sys
from pathlib import Path

ARTIFACTS = re.compile(
    r"\x1b\[[IO]"                                # focus in/out events
    r"|\x1b\]1[01];[^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC 10/11 color query/response
    r"|\x1b\[\?\d+(?:;\d+)*c"                    # device-attribute responses
    r"|\x1b\[>\d+(?:;\d+)*c"                     # secondary DA responses
    r"|\x1bP>\|[^\x1b]*\x1b\\"                   # XTVERSION response
)

path = Path(sys.argv[1])
documents = []
for line in path.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    documents.append(json.loads(line))

for document in documents:
    if isinstance(document, list) and len(document) >= 3 and document[1] == "o":
        document[2] = ARTIFACTS.sub("", document[2])

path.write_text(
    "\n".join(json.dumps(d, ensure_ascii=False, separators=(",", ":")) for d in documents)
    + "\n",
    encoding="utf-8",
)
PYEOF
echo "✓ Terminal query/response artifacts stripped"

# ── Convert to GIF ──────────────────────────────────────────────────────────

if [ "${SKIP_GIF:-}" != "1" ]; then
    echo ""
    echo "Converting to GIF (speed=${SPEED}x, theme=${THEME})..."
    render_gif "$CAST_FILE" "$GIF_FILE" "$SPEED" "$THEME" "$COLS" "$ROWS"
    echo "✓ GIF saved: ${GIF_FILE}"
    GIF_SIZE=$(du -h "$GIF_FILE" | cut -f1); echo "  Size: $GIF_SIZE"
fi

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "=== Done ==="
echo ""
echo "Files:"
echo "  ${CAST_FILE}"
[ "${SKIP_GIF:-}" != "1" ] && echo "  ${GIF_FILE}"
echo ""
echo "Any new GIF must satisfy the reviewed policy in"
echo ".github/scripts/validate_demo_gifs.py (size/dimensions/frames)."
echo ""
echo "Embed in README:"
echo '  ![GCO Autopilot](demo/autopilot.gif)'
