#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Record the `gco deps maintain` demo as an animated GIF
# ─────────────────────────────────────────────────────────────────────────────
# Records a short terminal session of `gco deps maintain --dry-run` and
# converts it to a GIF with agg. The recording is credential-free and
# deterministic: it reads the committed sample findings document
# (demo/deps-maintain-findings.json, the same shape the monthly deps-scan
# embeds in its issue), prints the launch plan with every finding sorted into
# its tier, and shows the head of the prompt the Claude Code session would
# start with. Nothing is created and no model is called, so the GIF can be
# re-recorded by anyone with the two recording tools.
#
# The recording drives the *checked-out* CLI through a `gco` PATH shim
# (`python3 -m cli.main`), never a globally installed gco, so the GIF always
# reflects the code in this working tree.
#
# Output files (deposited in demo/):
#   demo/deps-maintain.cast + demo/deps-maintain.gif
#
# Prerequisites:
#   - asciinema: brew install asciinema  (or pip install asciinema)
#   - agg:       brew install agg        (or cargo install agg)
#   - jq (the prompt excerpt is read from the JSON plan)
#   - python3 with the repo's dependencies importable (dev container, or
#     an environment where `python3 -m cli.main --help` works)
#
# Usage:
#   bash demo/record_deps_maintain.sh
#
# Options (via environment variables):
#   DEMO_COLS=110        Terminal width for recording (default: 110)
#   DEMO_ROWS=34         Terminal height for recording (default: 34)
#   DEMO_SPEED=1.4       Playback speed multiplier for GIF (default: 1.4)
#   DEMO_THEME=monokai   agg color theme (default: monokai)
#   DEMO_FONT_FAMILY     agg font fallback chain (default: see lib_demo.sh)
#   SKIP_GIF=1           Only produce the .cast file, skip GIF conversion
#
# The cast is post-processed like the other demo recordings before the GIF
# is rendered: sanitize_cast redacts anything shaped like an AWS account ID
# or access-key ID and folds $HOME into ~ (verified afterwards by
# verify_cast_sanitized), strip_emoji_from_cast rewrites codepoints agg's
# text engine can't render, and rebase_cast_to_marker makes the banner the
# first frame so static previews show content. See demo/lib_demo.sh.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The checkout being recorded: normally the one this script lives in. The BATS
# suite points GCO_RECORDING_REPO_ROOT at a disposable fixture repository so
# the tracked recorder runs in place against it.
REPO_ROOT="$(cd "${GCO_RECORDING_REPO_ROOT:-$SCRIPT_DIR/..}" && pwd)"
DEMO_DIR="${REPO_ROOT}/demo"

# shellcheck source=demo/lib_demo.sh
source "${SCRIPT_DIR}/lib_demo.sh"
setup_colors

CAST_FILE="${DEMO_DIR}/deps-maintain.cast"
GIF_FILE="${DEMO_DIR}/deps-maintain.gif"
FINDINGS_FILE="demo/deps-maintain-findings.json"
# A fixed branch keeps the on-screen plan identical from one recording to the
# next; the real command defaults to maint/deps-<today>.
DEMO_BRANCH="maint/deps-2026-10-01"
BANNER_TEXT="GCO Dependency Maintenance"

COLS="${DEMO_COLS:-110}"
ROWS="${DEMO_ROWS:-34}"
SPEED="${DEMO_SPEED:-1.4}"
THEME="${DEMO_THEME:-monokai}"

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

echo "=== GCO Dependency Maintenance Demo Recorder ==="
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

if command -v jq &>/dev/null; then
    preflight_pass "jq installed"
else
    preflight_fail "jq not installed (the prompt excerpt is read from the JSON plan)" \
        "brew install jq  (macOS) or  apt-get install jq  (Linux)"
fi

if (cd "$REPO_ROOT" && python3 -m cli.main --version &>/dev/null); then
    preflight_pass "GCO CLI importable (python3 -m cli.main)"
else
    preflight_fail "GCO CLI not importable from this python3" \
        "Run inside the dev container, or install the repo's deps (pip install -e .)"
fi

if [ -f "${SCRIPT_DIR}/lib_demo.sh" ] && [ -f "${REPO_ROOT}/cdk.json" ] \
   && [ -f "${REPO_ROOT}/${FINDINGS_FILE}" ]; then
    preflight_pass "Repository layout looks right (sample findings present)"
else
    preflight_fail "Repository layout unexpected" \
        "Run from a full GCO checkout that carries ${FINDINGS_FILE}"
fi

echo ""
if [ "$PREFLIGHT_FAIL" -gt 0 ]; then
    echo "${RED}${BOLD}${PREFLIGHT_FAIL} check(s) failed. Fix the issues above before recording.${RESET}"
    exit 1
fi

# ── Driver ──────────────────────────────────────────────────────────────────
# A `gco` PATH shim keeps the on-screen command honest (`$ gco deps maintain …`)
# while guaranteeing the recording exercises this checkout's code.
SHIM_DIR="$(mktemp -d)"
DRIVER="$(mktemp)"
trap 'rm -rf "$SHIM_DIR" "$DRIVER"' EXIT
cat > "${SHIM_DIR}/gco" <<'GCO_SHIM'
#!/usr/bin/env bash
exec python3 -m cli.main "$@"
GCO_SHIM
chmod +x "${SHIM_DIR}/gco"

cat > "$DRIVER" <<DRIVER_SCRIPT
#!/usr/bin/env bash
set -euo pipefail
# Bash 5 enables checkwinsize by default and would overwrite the exported
# COLUMNS with the outer terminal's width after the first external command,
# wrapping every later banner in the COLS-wide render.
shopt -u checkwinsize
cd "${REPO_ROOT}"
export PATH="${SHIM_DIR}:\${PATH}"
export COLUMNS="${COLS}" LINES="${ROWS}"

# shellcheck source=demo/lib_demo.sh
source "${REPO_ROOT}/demo/lib_demo.sh"
setup_colors

banner "${BANNER_TEXT}"
narrate "Every month the deps-scan workflow opens one rolling issue listing every pin that drifted."
narrate "gco deps maintain hands those findings to a Claude Code session and comes back with a draft PR."
sleep 3

run_cmd "gco deps maintain --dry-run --findings ${FINDINGS_FILE} --branch ${DEMO_BRANCH}"
sleep 6

spacer
narrate "Each finding is sorted by blast radius: mechanical and semantic are applied, judgment is analysed."
narrate "The session opens from a prompt that names each finding's procedure in docs/MAINTENANCE.md:"
sleep 2
run_cmd "gco -o json deps maintain --dry-run --findings ${FINDINGS_FILE} --branch ${DEMO_BRANCH} | jq -r .prompt | sed -n '1,24p'"
sleep 5

spacer
highlight "Run it for real with:  gco deps maintain"
narrate "A worktree on a new branch, the session, then a draft PR with next steps for the maintainer."
sleep 3
DRIVER_SCRIPT
chmod +x "$DRIVER"

# ── Record ───────────────────────────────────────────────────────────────────

echo "Recording deps maintain demo (${COLS}x${ROWS})..."
echo "Output: ${CAST_FILE}"
echo ""

rm -f "$CAST_FILE"

# --return makes asciinema exit with the driver's status, so a CLI that
# failed inside the recording stops here with a cast to read, not a GIF.
export REPO_ROOT SHIM_DIR COLS ROWS
if ! asciinema rec \
    --return \
    --cols "$COLS" \
    --rows "$ROWS" \
    --idle-time-limit 1.5 \
    --overwrite \
    --command "bash --norc --noprofile $DRIVER" \
    "$CAST_FILE"; then
    echo "" >&2
    echo "✗ The recording's driver exited non-zero; the CLI failed inside the session." >&2
    echo "  Read ${CAST_FILE} before re-recording." >&2
    exit 1
fi

echo ""
echo "✓ Recording saved: ${CAST_FILE}"

# Prove the plan and the prompt actually landed before rendering: a CLI that
# failed to import, or a changed option, would otherwise publish a GIF of an
# error message.
if grep -q "launch plan" "$CAST_FILE" && grep -q "Dry run only" "$CAST_FILE" \
   && grep -q "Findings to apply" "$CAST_FILE"; then
    echo "✓ Plan and prompt verified in the recording"
else
    echo "✗ The recording does not show the launch plan and the prompt." >&2
    echo "  The CLI may have failed; read ${CAST_FILE} before re-recording." >&2
    exit 1
fi

# ── Sanitize and verify ─────────────────────────────────────────────────────

sanitize_cast "$CAST_FILE"
verify_cast_sanitized "$CAST_FILE"
echo "✓ Cast sanitized and verified (AWS account IDs → 000000000000, \$HOME → ~)"

strip_emoji_from_cast "$CAST_FILE"
echo "✓ Tofu-triggering codepoints stripped"

rebase_cast_to_marker "$CAST_FILE" "$BANNER_TEXT"
echo "✓ Banner is the first frame"

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
echo "  ![GCO dependency maintenance](demo/deps-maintain.gif)"
