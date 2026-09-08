#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Record a GCO teardown as an animated GIF
# ─────────────────────────────────────────────────────────────────────────────
# Records `python3 -m cli.main stacks destroy-all -y` from the guarded checkout
# using asciinema, then converts to an animated GIF using agg.
#
# Output files (deposited in demo/):
#   demo/destroy.cast  — asciinema recording
#   demo/destroy.gif   — animated GIF for embedding in READMEs
#
# Prerequisites:
#   - asciinema: brew install asciinema
#   - agg:       brew install agg
#   - Repository Python dependencies installed
#   - AWS credentials configured
#
# Usage:
#   GCO_RECORDING_LIVE=1 \
#   GCO_EXPECTED_GIT_SHA=<40-char-sha> \
#   GCO_EXPECTED_ACCOUNT_ID=<12-digit-account> \
#   bash demo/record_destroy.sh
#   RENDER_EXISTING=1 bash demo/record_destroy.sh  # no AWS calls
#
# Options (via environment variables):
#   GCO_RECORDING_LIVE=1   Required acknowledgement for live recording
#   GCO_EXPECTED_GIT_SHA   Required full reviewed SHA for live recording
#   GCO_EXPECTED_ACCOUNT_ID Required authorized account for live recording
#   RENDER_EXISTING=1      Re-render the existing verified cast without AWS
#   DEMO_COLS=116          Terminal width (default: 116)
#   DEMO_ROWS=36           Terminal height (default: 36)
#   DEMO_SPEED=10          Playback speed for GIF (default: 10)
#   DEMO_THEME=monokai       agg color theme (default: monokai)
#   DEMO_FONT_FAMILY         agg font fallback chain (default: see lib_demo.sh)
#   SKIP_GIF=1               Only produce the .cast file
#   SKIP_SANITIZE=1          Rejected for publishable recordings
#   SKIP_EMOJI_STRIP=1       Skip emoji substitution (debugging only)
#
# The raw cast and GIF are written under a same-filesystem temporary directory.
# The tracked pair is published only after these passes succeed. Because POSIX
# cannot atomically rename two files as one unit, the previous pair is preserved
# and restored on command failure or handled HUP/INT/TERM interruption. SIGKILL
# cannot be trapped; each individual final-path rename remains atomic.
#
# The recorded .cast is post-processed in three passes before the GIF is
# rendered:
#   1. sanitize_cast — account IDs and AWS access-key IDs are replaced.
#   2. verify_cast_sanitized — independently rejects any residual pattern.
#   3. strip_emoji_from_cast — rewrites the five codepoints agg's text
#      engine can't render with Menlo (ℹ ✅ ✨ 📦 🚀) to safe monochrome
#      equivalents. See lib_demo.sh for the full mapping and rationale.
#
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=demo/lib_demo.sh
source "${SCRIPT_DIR}/lib_demo.sh"
setup_colors

CAST_FILE="${SCRIPT_DIR}/destroy.cast"
GIF_FILE="${SCRIPT_DIR}/destroy.gif"

# Raw recordings, renders, and prior-artifact backups stay in demo/ so every
# individual rename is same-filesystem atomic. The shared publication helper
# tracks whether paired publication is in progress; EXIT cleanup rolls it back
# before deleting staging. Preserve staging if rollback itself cannot complete.
RECORDING_TMP_DIR=""
cleanup_recording_temps() {
    local exit_code="$1"
    local rollback_succeeded=1
    trap - EXIT
    trap '' HUP INT TERM

    if ! rollback_recording_publication; then
        echo "Recording publication rollback failed; preserving staging at ${RECORDING_TMP_DIR}." >&2
        rollback_succeeded=0
        exit_code=1
    fi
    if [ -n "$RECORDING_TMP_DIR" ] && [ "$rollback_succeeded" -eq 1 ]; then
        if ! rm -rf -- "${RECORDING_TMP_DIR:?}"; then
            exit_code=1
        fi
    fi
    if ! release_legacy_recording_lock; then
        exit_code=1
    fi
    exit "$exit_code"
}
trap 'cleanup_recording_temps "$?"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Terminal dimensions (same as record_demo.sh)
COLS="${DEMO_COLS:-116}"
ROWS="${DEMO_ROWS:-36}"

# Teardown is already concise; preserve the current pacing.
SPEED="${DEMO_SPEED:-10}"
THEME="${DEMO_THEME:-monokai}"
RENDER_EXISTING="${RENDER_EXISTING:-0}"

# ── Preflight ────────────────────────────────────────────────────────────────

PREFLIGHT_PASS=0
PREFLIGHT_FAIL=0
PREFLIGHT_WARN=0

preflight_pass() {
    echo "  ${GREEN}${BOLD}✓${RESET} $1"
    PREFLIGHT_PASS=$((PREFLIGHT_PASS + 1))
}

preflight_fail() {
    echo "  ${RED}${BOLD}✗${RESET} $1"
    echo "    ${DIM}Fix: $2${RESET}"
    PREFLIGHT_FAIL=$((PREFLIGHT_FAIL + 1))
}

preflight_warn() {
    echo "  ${YELLOW}${BOLD}!${RESET} $1"
    echo "    ${DIM}$2${RESET}"
    PREFLIGHT_WARN=$((PREFLIGHT_WARN + 1))
}

echo "=== GCO Destroy Recorder ==="
echo ""
echo "  ${BOLD}Preflight Check${RESET}"
echo ""

# GIF rendering is required unless explicitly producing a cast only.
if [ "${SKIP_GIF:-}" != "1" ]; then
    if command -v agg &>/dev/null; then
        preflight_pass "agg installed"
    else
        if [ "$RENDER_EXISTING" = "1" ]; then
            preflight_fail "agg is required for RENDER_EXISTING=1" \
                "Install agg; the existing destroy GIF will be preserved"
        else
            preflight_warn "agg not installed — will produce .cast only" \
                "brew install agg"
            SKIP_GIF=1
        fi
    fi
fi

if [ "${SKIP_SANITIZE:-}" = "1" ]; then
    preflight_fail "SKIP_SANITIZE is not allowed for publishable recordings" \
        "Unset SKIP_SANITIZE so verification remains fail-closed"
fi

case "$RENDER_EXISTING" in
    0)
        if command -v asciinema &>/dev/null; then
            preflight_pass "asciinema installed"
        else
            preflight_fail "asciinema not installed" "brew install asciinema"
        fi
        if (cd "$REPO_ROOT" && python3 -c 'from cli.main import main; assert callable(main)'); then
            preflight_pass "Repository GCO CLI module importable"
        else
            preflight_fail "Repository GCO CLI module is not importable" \
                "Install this checkout's Python dependencies before recording"
        fi
        if [ -f "${REPO_ROOT}/cdk.json" ]; then
            preflight_pass "cdk.json found"
        else
            preflight_fail "cdk.json not found" "Run from repo root"
        fi
        if verify_legacy_live_recording_authorization "$REPO_ROOT"; then
            preflight_pass "Live consent, Git SHA, and AWS account guards verified"
        else
            preflight_fail "Live recording authorization failed" \
                "Set GCO_RECORDING_LIVE, GCO_EXPECTED_GIT_SHA, and GCO_EXPECTED_ACCOUNT_ID"
        fi
        ;;
    1)
        if [ -f "$CAST_FILE" ]; then
            preflight_pass "Existing destroy cast found for offline rendering"
        else
            preflight_fail "Existing destroy cast not found" \
                "Record once with guarded live mode before using RENDER_EXISTING=1"
        fi
        ;;
    *)
        preflight_fail "RENDER_EXISTING must be 0 or 1" \
            "Use RENDER_EXISTING=1 only for offline re-rendering"
        ;;
esac

# Check disk space
AVAILABLE_MB=$(df -m "${SCRIPT_DIR}" 2>/dev/null | awk 'NR==2{print $4}' || echo "0")
if [ "$AVAILABLE_MB" -gt 100 ]; then
    preflight_pass "Disk space: ${AVAILABLE_MB} MB available"
else
    preflight_warn "Low disk space: ${AVAILABLE_MB} MB" "Free up space"
fi

echo ""
echo "  ${DIM}──────────────────────────────────────────────────────────────${RESET}"
echo "  ${BOLD}Results:${RESET}  ${GREEN}${PREFLIGHT_PASS} passed${RESET}  ${RED}${PREFLIGHT_FAIL} failed${RESET}  ${YELLOW}${PREFLIGHT_WARN} warnings${RESET}"
echo "  ${DIM}──────────────────────────────────────────────────────────────${RESET}"

if [ "$PREFLIGHT_FAIL" -gt 0 ]; then
    echo ""
    echo "  ${RED}${BOLD}Fix the issues above before recording.${RESET}"
    exit 1
fi

acquire_legacy_recording_lock "$REPO_ROOT"

# ── Record ───────────────────────────────────────────────────────────────────

# Stage every raw output beside the final files so successful `mv` publication
# cannot cross filesystems. Existing tracked artifacts remain untouched until
# verification and GIF rendering succeed.
RECORDING_TMP_DIR=$(mktemp -d "${SCRIPT_DIR}/.destroy-recording.XXXXXX")
RAW_CAST_FILE="${RECORDING_TMP_DIR}/destroy.cast"
RAW_GIF_FILE="${RECORDING_TMP_DIR}/destroy.gif"
WRAPPER="${RECORDING_TMP_DIR}/run.sh"

if [ "$RENDER_EXISTING" = "1" ]; then
    echo "Re-rendering verified destroy cast (${COLS}x${ROWS}, speed=${SPEED}x)..."
    cp -p "$CAST_FILE" "$RAW_CAST_FILE"
else
    echo ""
    echo "Recording destroy (${COLS}x${ROWS})..."
    echo "Output: ${CAST_FILE}"
    echo ""
    echo "  ${YELLOW}${BOLD}This will run python3 -m cli.main stacks destroy-all -y${RESET}"
    echo "  ${DIM}The destroy takes 10-20 minutes. The recording captures everything.${RESET}"
    echo ""

    # Create a wrapper script so asciinema runs one repository-bound command.
    cat > "$WRAPPER" <<'WRAPPER_SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
cd "$REPO_ROOT"
export COLUMNS="$GCO_RECORDING_COLUMNS"
python3 -m cli.main stacks destroy-all -y
WRAPPER_SCRIPT
    chmod +x "$WRAPPER"

    export REPO_ROOT
    export GCO_RECORDING_COLUMNS="$COLS"
    export GCO_RECORDING_WRAPPER="$WRAPPER"
    asciinema rec \
        --return \
        --cols "$COLS" \
        --rows "$ROWS" \
        --overwrite \
        --command "bash --norc --noprofile \"\$GCO_RECORDING_WRAPPER\"" \
        "$RAW_CAST_FILE"

    echo ""
    echo "✓ Raw recording complete; sanitizing before publication"
fi

# ── Sanitize ────────────────────────────────────────────────────────────────
# Redact any AWS account/access-key IDs before anyone can view the cast or the
# GIF derived from it. See sanitize_cast() in lib_demo.sh for details.

sanitize_cast "$RAW_CAST_FILE"
if [ "${SKIP_SANITIZE:-}" = "1" ]; then
    echo "! Cast sanitization skipped; do not commit or distribute this artifact"
else
    verify_cast_sanitized "$RAW_CAST_FILE"
    echo "✓ Cast sanitized and verified (AWS account/access-key IDs redacted)"
fi

# ── Strip tofu-triggering codepoints ────────────────────────────────────────
# Rewrite the handful of Unicode characters Menlo can't render so agg never
# falls back to the system's LastResort tofu font. See strip_emoji_from_cast()
# in lib_demo.sh for the substitution table.

strip_emoji_from_cast "$RAW_CAST_FILE"
echo "✓ Tofu-triggering codepoints stripped (ℹ→i, ✅→✓, ✨→*, 📦→[pkg], 🚀→>>)"

# Render from the sanitized staging cast before publishing either artifact. If
# agg fails, the previous tracked cast/GIF pair remains untouched.
if [ "${SKIP_GIF:-}" != "1" ]; then
    echo ""
    echo "Converting to GIF (speed=${SPEED}x, theme=${THEME})..."
    render_gif "$RAW_CAST_FILE" "$RAW_GIF_FILE" "$SPEED" "$THEME" "$COLS" "$ROWS"
fi

# Publish the fully prepared pair through the shared rollback transaction. An
# empty staged GIF removes any older final GIF as the second transaction step.
PUBLISH_GIF_FILE=""
if [ "${SKIP_GIF:-}" != "1" ]; then
    PUBLISH_GIF_FILE="$RAW_GIF_FILE"
fi
publish_recording_artifacts \
    "$RAW_CAST_FILE" "$PUBLISH_GIF_FILE" "$CAST_FILE" "$GIF_FILE"

echo "✓ Recording pair published: ${CAST_FILE}"
echo "  Size: $(du -h "$CAST_FILE" | cut -f1)"
if [ "${SKIP_GIF:-}" != "1" ]; then
    echo "✓ GIF published: ${GIF_FILE}"
    echo "  Size: $(du -h "$GIF_FILE" | cut -f1)"
fi

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "=== Done ==="
echo ""
echo "Files:"
echo "  ${CAST_FILE}"
[ "${SKIP_GIF:-}" != "1" ] && echo "  ${GIF_FILE}"
echo ""
echo "To replay:       asciinema play ${CAST_FILE}"
echo "To record again:  re-run $0 from the exact guarded checkout"
echo ""
echo "Embed in README:"
echo '  ![GCO Destroy](demo/destroy.gif)'
