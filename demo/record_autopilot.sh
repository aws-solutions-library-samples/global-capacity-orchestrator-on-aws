#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Record the GCO Autopilot demo as an animated GIF
# ─────────────────────────────────────────────────────────────────────────────
# Records a short terminal session of `gco autopilot` and converts it to a
# GIF with agg. Select the engine with DEMO_ENGINE (``claude-code`` by
# default, or ``codex``) and the scenario with DEMO_MODE:
#
#   live (default)  A real interactive session on Amazon Bedrock. The recorder
#                   launches the selected TUI, types a GCO question, waits for
#                   an MCP-grounded answer, then exits. Requires the selected
#                   engine binary, expect(1), and Bedrock-enabled credentials.
#   plan            Credential-free recording of the selected engine's
#                   `--dry-run` plan: model, reasoning, MCP set, config path,
#                   and exact lazy-install pin. This is the Codex demo mode.
#
# The recording drives the *checked-out* CLI through a `gco` PATH shim
# (`python3 -m cli.main`), never a globally installed gco, so the GIF always
# reflects the code in this working tree. Model latency is compressed by
# asciinema's --idle-time-limit, so the live mode stays a short GIF.
#
# Output files (deposited in demo/):
#   Claude: demo/autopilot-claude-code.cast + demo/autopilot-claude-code.gif
#   Codex:  demo/autopilot-codex.cast + demo/autopilot-codex.gif
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
#   DEMO_ENGINE=claude-code  "claude-code" (default) or "codex"
#   DEMO_MODE=live       "live" (real selected-engine Bedrock session, default)
#                        or "plan" (credential-free engine launch plan)
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
# The checkout being recorded: normally the one this script lives in. The BATS
# suite points GCO_RECORDING_REPO_ROOT at a disposable fixture repository so
# the tracked recorder runs in place against it; left unset, every path below
# is the same as before the override existed.
REPO_ROOT="$(cd "${GCO_RECORDING_REPO_ROOT:-$SCRIPT_DIR/..}" && pwd)"
DEMO_DIR="${REPO_ROOT}/demo"

# shellcheck source=demo/lib_demo.sh
source "${SCRIPT_DIR}/lib_demo.sh"
setup_colors

DEMO_ENGINE="${DEMO_ENGINE:-claude-code}"
DEMO_MODE="${DEMO_MODE:-live}"

case "$DEMO_ENGINE" in
    claude-code)
        CAST_FILE="${DEMO_DIR}/autopilot-claude-code.cast"
        GIF_FILE="${DEMO_DIR}/autopilot-claude-code.gif"
        ;;
    codex)
        CAST_FILE="${DEMO_DIR}/autopilot-codex.cast"
        GIF_FILE="${DEMO_DIR}/autopilot-codex.gif"
        ;;
    *) echo "error: DEMO_ENGINE must be 'claude-code' or 'codex', got '$DEMO_ENGINE'" >&2; exit 1 ;;
esac

COLS="${DEMO_COLS:-110}"
ROWS="${DEMO_ROWS:-30}"
SPEED="${DEMO_SPEED:-1.6}"
THEME="${DEMO_THEME:-monokai}"

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

echo "=== GCO Autopilot Demo Recorder (${DEMO_ENGINE}, ${DEMO_MODE}) ==="
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
    if [ "$DEMO_ENGINE" = "codex" ]; then
        ENGINE_BINARY="codex"
        ENGINE_LABEL="Codex"
        INSTALL_HINT="gco autopilot --engine codex -y"
    else
        ENGINE_BINARY="claude"
        ENGINE_LABEL="Claude Code"
        INSTALL_HINT="gco autopilot -y"
    fi
    if command -v "$ENGINE_BINARY" &>/dev/null; then
        preflight_pass "$ENGINE_LABEL installed ($("$ENGINE_BINARY" --version 2>&1 | head -1))"
    else
        preflight_fail "$ENGINE_LABEL not installed (live mode launches a real session)" \
            "Run '$INSTALL_HINT' once to install the pin, or set DEMO_MODE=plan"
    fi
    if [ "$DEMO_ENGINE" != "codex" ]; then
        for companion_runtime in uvx npx; do
            if command -v "$companion_runtime" &>/dev/null; then
                preflight_pass "$companion_runtime installed"
            else
                preflight_fail "$companion_runtime not installed (Claude live mode starts companions)" \
                    "Use gco-dev, install the missing runtime, or set DEMO_MODE=plan"
            fi
        done
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

if [ "$DEMO_MODE" = "live" ] && [ "$DEMO_ENGINE" = "codex" ]; then
    # Mirror the Claude recording with Codex's real inline TUI. Read-only,
    # never-approve, and run-scoped trust for this reviewed checkout are hidden
    # recording plumbing: the prompt explicitly uses GCO MCP documentation
    # tools, no workspace mutation is needed, and no trust setting is persisted.
    EXPECT_SCRIPT="$(mktemp)"
    cat > "$EXPECT_SCRIPT" <<'EXPECT_DRIVER'
#!/usr/bin/expect -f
set timeout 420
set stty_init "rows 30 columns 110"

# Codex enables CSI-u keyboard reporting; this emits a physical Enter only
# after a positively identified composer redraw below.
proc press_enter {} {
    send -- "\033\[13u"
}

# The recording uses only the local GCO MCP server and exposes exactly the two
# documentation tools needed on camera. GCO startup is required, both tools are
# explicitly preapproved, and Codex's built-in shell is removed entirely.
set gco_required {mcp_servers.gco.required=true}
set gco_tools {mcp_servers.gco.enabled_tools=["find_docs","read_resource"]}
set find_docs_approval {mcp_servers.gco.tools.find_docs.approval_mode="approve"}
set read_resource_approval {mcp_servers.gco.tools.read_resource.approval_mode="approve"}
set prompt {Use only the GCO MCP find_docs tool, then read_resource on a returned documentation URI; do not use shell commands or other tools. Which gco command submits a job through SQS, and why is that recommended? Answer in two short lines. Begin when ready}
log_user 0
spawn gco autopilot --engine codex --no-companions -- -c $gco_required -c $gco_tools -c $find_docs_approval -c $read_resource_approval --disable shell_tool --sandbox read-only --ask-for-approval never --no-alt-screen -- $prompt
log_user 1

# Codex accepts the initial prompt as prefilled composer text. Monitor startup
# for dialogs, then append one harmless space to force a current composer redraw;
# only a positive match of that redraw is allowed to submit the turn.
set submitted 0
set timeout 20
expect {
    -nocase -re {trust the contents|trust the files|do you trust} { exit 6 }
    -nocase -re {do you want to (proceed|allow)|allow this tool} { exit 7 }
    -re {Working|Calling gco\.find_docs} { set submitted 1 }
    timeout {}
    eof { exit 3 }
}
if {!$submitted} {
    send -- "."
    set timeout 10
    expect {
        -re {Begin when ready.*\.} { press_enter }
        -nocase -re {trust the contents|trust the files|do you trust} { exit 6 }
        -nocase -re {do you want to (proceed|allow)|allow this tool} { exit 7 }
        timeout { exit 8 }
        eof { exit 3 }
    }
}
# Wait for the explanatory final answer, not an intermediate tool query that
# happens to mention the command. Trust and approval dialogs remain explicit
# failures for the entire turn; never send an unqualified keypress on timeout.
set timeout 420
expect {
    -nocase -re {trust the contents|trust the files|do you trust} { exit 6 }
    -nocase -re {do you want to (proceed|allow)|allow this tool} { exit 7 }
    -nocase -re {recommended (for production )?because|resilient production|durable, asynchronous} {}
    timeout { exit 4 }
    eof { exit 5 }
}
# Leave the complete short answer on screen before exiting. The matched phrase
# can appear just before the final line finishes rendering.
sleep 35

# Codex uses Ctrl+C for a clean TUI exit; unlike Claude Code, `/exit` is not
# a terminating slash command in this version.
send "\003"
expect eof
EXPECT_DRIVER

    cat > "$DRIVER" <<DRIVER_SCRIPT
#!/usr/bin/env bash
set -euo pipefail
cd "\$REPO_ROOT"
export PATH="\${SHIM_DIR}:\${PATH}"
export COLUMNS="\${COLS}" LINES="\${ROWS}"

# shellcheck source=demo/lib_demo.sh
source "\${REPO_ROOT}/demo/lib_demo.sh"
setup_colors

banner "GCO Autopilot — Codex"
narrate "A live Codex session scoped to GCO documentation tools:"
narrate "Amazon Bedrock + required GCO MCP; no shell or companion servers."
sleep 3

echo ""
echo "  \${MAGENTA}\\\$ \${WHITE}\${BOLD}gco autopilot --engine codex --no-companions\${RESET}"
sleep 1

expect -f "$EXPECT_SCRIPT"

printf '\033[2J\033[H'
banner "GCO Autopilot — Codex"
spacer
highlight "A real session: Codex used only GCO's approved documentation tools."
narrate "Default Autopilot can include companions; this recording is least-privilege."
narrate "Get started:  gco autopilot --engine codex"
sleep 4
DRIVER_SCRIPT
elif [ "$DEMO_MODE" = "live" ]; then
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

# Type with Expect's humanized per-character timing. Claude Code's native TUI
# processes terminal key events individually; word-sized writes can be discarded
# while its keyboard protocol is active.
proc type_words {text} {
    send -h -- "$text"
}

# Claude Code 2.1.235 enables CSI-u keyboard reporting after startup. A raw
# carriage return is text input in that mode; CSI 13 u is the physical Enter
# key. The resume prompt appears before CSI-u is enabled and still uses \r.
proc press_enter {} {
    send -- "\033\[13u"
}

# The GCO MCP server's tools are pre-approved for the session with
# claude's own --allowedTools flag (through autopilot's passthrough), so
# the recording is deterministic — no version-specific permission-dialog
# text to script against. The driver deliberately displays plain
# `gco autopilot`: the flag is recording plumbing, not part of the user
# journey — an interactive user running the plain command gets the
# identical session, with claude's ordinary one-click permission prompt
# standing in for the pre-approval.
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
    -nocase -re {trust the files|do you trust|project you created or one you trust|yes, i trust this folder|enter.*confirm} { press_enter; exp_continue }
    -nocase -re {choose the text style|select theme} { press_enter; exp_continue }
    -re {Welcome|\? for shortcuts|Try "} {}
    timeout {}
}
sleep 4

type_words "Which gco command submits a job via SQS, and why is that recommended? Check the GCO MCP docs (no shell commands). Answer in exactly two short lines."
sleep 1
press_enter

# Wait for the grounded answer itself (it inevitably names submit-sqs),
# then let the complete two-line answer finish rendering. Recorded idle is
# capped, so the generous hold improves reliability without bloating the GIF.
expect {
    -nocase -re {do you want to (proceed|allow)|allow this tool} { sleep 2; send "2"; press_enter; exp_continue }
    -timeout 240 -re {submit-sqs} {}
    timeout {}
}
sleep 60

send -- "/exit"
press_enter
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
echo "  \${MAGENTA}\\\$ \${WHITE}\${BOLD}gco autopilot\${RESET}"
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
elif [ "$DEMO_ENGINE" = "codex" ]; then
    cat > "$DRIVER" <<'DRIVER_SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
cd "$REPO_ROOT"
export PATH="${SHIM_DIR}:${PATH}"
export COLUMNS="${COLS}" LINES="${ROWS}"

# shellcheck source=demo/lib_demo.sh
source "${REPO_ROOT}/demo/lib_demo.sh"
setup_colors

banner "GCO Autopilot — Codex"
narrate "Choose Codex without giving up GCO's one-command setup:"
narrate "OpenAI Codex + the GCO MCP server + companion MCPs on Amazon Bedrock."
sleep 3

run_cmd "gco autopilot --engine codex --dry-run"
sleep 5

spacer
highlight "Launch it for real with:  gco autopilot --engine codex"
narrate "The exact Codex pin and isolated CODEX_HOME persist in gco-dev."
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
narrate "on Amazon Bedrock with GCO's default Claude Code model."
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
    --return \
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
    # events, so checks join the rendered stream and normalize to alphanumerics.
    # Codex has a stricter contract: successful calls to both approved GCO docs
    # tools, no shell/companions/prompts, and no live credential values.
    if python3 - "$CAST_FILE" "$DEMO_ENGINE" <<'PYEOF'
import json
import os
import re
import sys
from pathlib import Path

documents = []
stream = []
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    doc = json.loads(line)
    documents.append(doc)
    if isinstance(doc, list) and len(doc) >= 3 and doc[1] == "o":
        stream.append(doc[2])
exit_events = [
    doc
    for doc in documents
    if isinstance(doc, list) and len(doc) >= 3 and doc[1] == "x"
]
header_version = documents[0].get("version") if isinstance(documents[0], dict) else None
valid_exit = header_version != 3 or (
    bool(exit_events) and str(exit_events[-1][2]) == "0"
)
joined = "".join(stream)
plain = re.sub(
    r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\\\)|\x1b[P^_].*?\x1b\\\\|\x1b.",
    "",
    joined,
)
normalized = re.sub(r"[^a-z0-9]", "", plain.lower())
required = ["submitsqs"]
forbidden = []
if sys.argv[2] == "codex":
    required.extend(("calledgcofinddocs", "calledgcoreadresource"))
    forbidden.extend(
        (
            "callingshell",
            "calledshell",
            "trustthecontents",
            "trustthefiles",
            "doyoutrust",
            "doyouwanttoproceed",
            "doyouwanttoallow",
            "allowthistool",
            "awsdocs",
            "awspricing",
            "ddgsearch",
            "deepwiki",
            "filesystem",
            "innermonologue",
            "mcptasks",
            "memorymcp",
            "playwright",
            "sequentialthinking",
        )
    )
credential_names = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
)
credential_leak = any(
    value and len(value) >= 8 and value in joined
    for name in credential_names
    if (value := os.environ.get(name))
)
raise SystemExit(
    0
    if valid_exit
    and all(marker in normalized for marker in required)
    and not any(marker in normalized for marker in forbidden)
    and not credential_leak
    else 1
)
PYEOF
    then
        if [ "$DEMO_ENGINE" = "codex" ]; then
            echo "✓ Live Codex recording verified (GCO docs tools only; no credentials/prompts)"
        else
            echo "✓ Live answer verified in the recording (mentions submit-sqs)"
        fi
    else
        echo "✗ The recording failed its required answer/tool/security contract." >&2
        echo "  The session may have stalled, used another tool, prompted, or exposed credentials." >&2
        exit 1
    fi
fi

# ── Sanitize and verify ─────────────────────────────────────────────────────

sanitize_cast "$CAST_FILE"
verify_cast_sanitized "$CAST_FILE"
echo "✓ Cast sanitized and verified (AWS account IDs → 000000000000)"

strip_emoji_from_cast "$CAST_FILE"
echo "✓ Tofu-triggering codepoints stripped"

# ── Strip terminal query/response artifacts and TUI tofu glyphs ─────────────
# Two Claude-Code-specific cleanups on top of lib_demo.sh's shared passes:
#
# 1. The TUI probes the terminal (focus tracking, OSC 11 background color,
#    device attributes, XTVERSION), and pieces of those query/response
#    exchanges land in the recorded output stream. agg's renderer doesn't
#    understand them and paints fragments like ``^[[O`` or ``^[]11;rgb:...``
#    literally. They carry no visual content, so they are removed outright.
#
# 2. The TUI emits three codepoints Menlo has no glyph for, and agg's
#    first-family-wins renderer paints them as tofu boxes (same root cause
#    strip_emoji_from_cast documents). Verified against Menlo.ttc's cmap:
#      ⏺ U+23FA BLACK CIRCLE FOR RECORD  → ● U+25CF (in Menlo, same intent)
#      ⏸ U+23F8 DOUBLE VERTICAL BAR      → ║ U+2551 (in Menlo, same width)
#      ⎿ U+23BF DENTISTRY SYMBOL ...     → └ U+2514 (in Menlo, same elbow)
#    Everything else the TUI uses (box drawing, quadrant blocks, the
#    spinner asterisks ✻✶✳✢✽, ❯, arrows) is covered by Menlo.
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

# Single-codepoint substitutions (str.translate); multi-char targets are
# fine as translate values.
TUI_TOFU = {
    0x23FA: "\u25cf",  # ⏺ → ●
    0x23F8: "\u2551",  # ⏸ → ║
    0x23BF: "\u2514",  # ⎿ → └
}

path = Path(sys.argv[1])
documents = []
for line in path.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    documents.append(json.loads(line))

for document in documents:
    if isinstance(document, list) and len(document) >= 3 and document[1] == "o":
        document[2] = ARTIFACTS.sub("", document[2]).translate(TUI_TOFU)

# Static GIF previews show frame zero without advancing the animation. Make the
# first rendered frame the recorder banner rather than the empty PTY that
# precedes it. Asciicast v2 uses absolute timestamps; v3 uses per-event delays.
events = [
    document
    for document in documents
    if isinstance(document, list) and len(document) >= 3
]
banner_event = next(
    (
        event
        for event in events
        if event[1] == "o"
        and isinstance(event[2], str)
        and "GCO Autopilot" in event[2]
    ),
    None,
)
header_version = documents[0].get("version") if isinstance(documents[0], dict) else None
if banner_event is not None and header_version == 2:
    banner_time = float(banner_event[0])
    for event in events:
        event[0] = round(max(0.0, float(event[0]) - banner_time), 6)
elif banner_event is not None and header_version == 3:
    for event in events:
        event[0] = 0
        if event is banner_event:
            break

path.write_text(
    "\n".join(json.dumps(d, ensure_ascii=False, separators=(",", ":")) for d in documents)
    + "\n",
    encoding="utf-8",
)
PYEOF
echo "✓ Terminal query/response artifacts stripped, TUI tofu glyphs substituted"

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
GIF_BASENAME="$(basename "$GIF_FILE")"
echo "  ![GCO Autopilot](demo/${GIF_BASENAME})"
