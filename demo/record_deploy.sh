#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Record a fresh GCO deployment as an animated GIF
# ─────────────────────────────────────────────────────────────────────────────
# Records `python3 -m cli.main stacks deploy-all -y` from the guarded checkout
# using asciinema, then converts to an animated GIF using agg.
#
# Output files (deposited in demo/):
#   demo/deploy.cast  — asciinema recording
#   demo/deploy.gif   — animated GIF for embedding in READMEs
#
# Prerequisites:
#   - asciinema: brew install asciinema
#   - agg:       brew install agg
#   - Repository Python dependencies installed
#   - AWS credentials configured
#
# Usage:
#   bash demo/record_deploy.sh
#
# Options (via environment variables):
#   GCO_EXPECTED_GIT_SHA     Optional full SHA guard; when set, HEAD must match
#                            and only the four lifecycle artifacts may be dirty
#   GCO_EXPECTED_ACCOUNT_ID  Optional 12-digit AWS account guard
#   DEMO_COLS=160            Terminal width (default: 160)
#   DEMO_ROWS=40             Terminal height (default: 40)
#   DEMO_SPEED=10            Playback speed for GIF (default: 10 — deploy is long)
#   DEMO_THEME=monokai       agg color theme (default: monokai)
#   DEMO_FONT_FAMILY         agg font fallback chain (default: see lib_demo.sh)
#   SKIP_GIF=1               Only produce the .cast file
#   SKIP_SANITIZE=1          Skip account/access-key redaction (debugging only)
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

CAST_FILE="${SCRIPT_DIR}/deploy.cast"
GIF_FILE="${SCRIPT_DIR}/deploy.gif"

# Raw recordings, renders, and prior-artifact backups stay in demo/ so every
# individual rename is same-filesystem atomic. The shared publication helper
# tracks whether paired publication is in progress; EXIT cleanup rolls it back
# before deleting staging. Preserve staging if rollback itself cannot complete.
RECORDING_TMP_DIR=""
cleanup_recording_temps() {
    local exit_code="$1"
    local rollback_succeeded=1
    trap - EXIT HUP INT TERM

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
    exit "$exit_code"
}
trap 'cleanup_recording_temps "$?"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Terminal dimensions (same as record_demo.sh)
COLS="${DEMO_COLS:-160}"
ROWS="${DEMO_ROWS:-40}"

# Deploy takes up to an hour — 10x speed makes the GIF watchable (~5-6 min)
SPEED="${DEMO_SPEED:-10}"
THEME="${DEMO_THEME:-monokai}"

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

echo "=== GCO Deploy Recorder ==="
echo ""
echo "  ${BOLD}Preflight Check${RESET}"
echo ""

# Check tools
if command -v asciinema &>/dev/null; then
    preflight_pass "asciinema installed"
else
    preflight_fail "asciinema not installed" "brew install asciinema"
fi

if [ "${SKIP_GIF:-}" != "1" ]; then
    if command -v agg &>/dev/null; then
        preflight_pass "agg installed"
    else
        preflight_warn "agg not installed — will produce .cast only" \
            "brew install agg"
        SKIP_GIF=1
    fi
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

# A supplied SHA makes provenance fail-closed. Recorder outputs are the only
# permitted dirty paths so this same checkout can record deploy then destroy.
if [ -n "${GCO_EXPECTED_GIT_SHA:-}" ]; then
    if verify_recording_git_state "$REPO_ROOT" \
            "demo/deploy.cast" "demo/deploy.gif" \
            "demo/destroy.cast" "demo/destroy.gif"; then
        preflight_pass "Git HEAD and source tree match the expected SHA"
    else
        preflight_fail "Git provenance guard failed" \
            "Checkout the exact CI-green SHA and remove unexpected changes"
    fi
else
    preflight_warn "Exact Git SHA guard not set" \
        "Set GCO_EXPECTED_GIT_SHA to publish an auditable recording"
fi

# Check AWS credentials
if aws sts get-caller-identity &>/dev/null; then
    preflight_pass "AWS credentials configured"
else
    preflight_fail "AWS credentials not configured" "aws configure or aws sso login"
fi

if [ -n "${GCO_EXPECTED_ACCOUNT_ID:-}" ]; then
    if verify_recording_aws_account; then
        preflight_pass "Active AWS account matches the expected account"
    else
        preflight_fail "AWS account guard failed" \
            "Select credentials for GCO_EXPECTED_ACCOUNT_ID"
    fi
else
    preflight_warn "Expected AWS account guard not set" \
        "Set GCO_EXPECTED_ACCOUNT_ID to publish an auditable recording"
fi

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

# ── Record ───────────────────────────────────────────────────────────────────

echo ""
echo "Recording deploy (${COLS}x${ROWS})..."
echo "Output: ${CAST_FILE}"
echo ""
echo "  ${YELLOW}${BOLD}This will run python3 -m cli.main stacks deploy-all -y${RESET}"
echo "  ${DIM}The deploy can take up to an hour. The recording captures everything.${RESET}"
echo ""

# Stage every raw output beside the final files so successful `mv` publication
# cannot cross filesystems. Existing tracked artifacts remain untouched until
# sanitization (and GIF rendering, when enabled) succeeds.
RECORDING_TMP_DIR=$(mktemp -d "${SCRIPT_DIR}/.deploy-recording.XXXXXX")
RAW_CAST_FILE="${RECORDING_TMP_DIR}/deploy.cast"
RAW_GIF_FILE="${RECORDING_TMP_DIR}/deploy.gif"
WRAPPER="${RECORDING_TMP_DIR}/run.sh"

# Create a wrapper script so asciinema runs a single command without
# needing --env or shell features like && in --command. Keep checkout paths
# out of generated shell syntax: the fixed wrapper reads quoted environment
# variables at runtime, preserving spaces and metacharacters as data.
cat > "$WRAPPER" <<'WRAPPER_SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
cd "$REPO_ROOT"
export COLUMNS="$GCO_RECORDING_COLUMNS"
python3 -m cli.main stacks deploy-all -y
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
echo '  ![GCO Deploy](demo/deploy.gif)'
