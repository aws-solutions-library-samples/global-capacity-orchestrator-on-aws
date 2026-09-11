#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Record the GCO live feature demo as an animated GIF
# ─────────────────────────────────────────────────────────────────────────────
# Live mode executes demo/live_demo.sh against an existing deployment using the
# repository CLI. It mutates Kubernetes jobs and an inference endpoint. Offline
# render mode only verifies and re-renders the existing tracked cast.
#
# Live mode snapshots only the authorized current kubectl context into a
# mode-0600 file beneath the private staging directory. Every recorder child
# inherits that single KUBECONFIG, so CLI refreshes cannot alter the operator's
# kubeconfig. The sensitive snapshot is removed by the recorder cleanup trap.
#
# Output files:
#   demo/live_demo.cast
#   demo/live_demo.gif
#
# Usage:
#   GCO_RECORDING_LIVE=1 \
#   GCO_EXPECTED_GIT_SHA=<40-char-sha> \
#   GCO_EXPECTED_ACCOUNT_ID=<12-digit-account> \
#   bash demo/record_demo.sh
#   RENDER_EXISTING=1 bash demo/record_demo.sh  # no AWS/Kubernetes calls
#
# Options:
#   GCO_RECORDING_LIVE=1    Required acknowledgement for live recording
#   GCO_EXPECTED_GIT_SHA    Required full reviewed SHA for live recording
#   GCO_EXPECTED_ACCOUNT_ID Required authorized account for live recording
#   RENDER_EXISTING=1       Re-render the existing verified cast without AWS
#   DEMO_COLS=116           Terminal width (default: 116)
#   DEMO_ROWS=36            Terminal height (default: 36)
#   DEMO_SPEED=3            GIF playback speed (default: 3)
#   DEMO_THEME=monokai      agg color theme
#   DEMO_FONT_FAMILY        agg font chain (default: see lib_demo.sh)
#   SKIP_GIF=1              Publish only the cast and remove any stale GIF
#   SKIP_EMOJI_STRIP=1      Skip known unsupported-glyph substitutions
#
# Publishable recordings are always sanitized and independently verified.
# SKIP_SANITIZE is deliberately rejected by this script.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

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

CAST_FILE="${DEMO_DIR}/live_demo.cast"
GIF_FILE="${DEMO_DIR}/live_demo.gif"
COLS="${DEMO_COLS:-116}"
ROWS="${DEMO_ROWS:-36}"
SPEED="${DEMO_SPEED:-3}"
THEME="${DEMO_THEME:-monokai}"
RENDER_EXISTING="${RENDER_EXISTING:-0}"

RECORDING_TMP_DIR=""
RECORDING_KUBECONFIG=""
cleanup_recording_temps() {
    local exit_code="$1"
    local rollback_succeeded=1
    trap - EXIT
    trap '' HUP INT TERM

    if [ -n "$RECORDING_KUBECONFIG" ] && \
            ! rm -f -- "$RECORDING_KUBECONFIG" "${RECORDING_KUBECONFIG}.tmp"; then
        echo "Unable to remove the staged credential-bearing kubeconfig." >&2
        exit_code=1
    fi
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

echo "=== GCO Live Demo Recorder ==="
echo ""
echo "  ${BOLD}Preflight Check${RESET}"
echo ""

if [ "${SKIP_GIF:-}" != "1" ]; then
    if command -v agg &>/dev/null; then
        preflight_pass "agg installed ($(agg --version 2>&1 | head -1))"
    else
        if [ "$RENDER_EXISTING" = "1" ]; then
            preflight_fail "agg is required for RENDER_EXISTING=1" \
                "Install agg; the existing live-demo GIF will be preserved"
        else
            preflight_warn "agg not installed — will produce .cast only" \
                "brew install agg (macOS) or cargo install agg"
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
            preflight_pass "asciinema installed ($(asciinema --version 2>&1 | head -1))"
        else
            preflight_fail "asciinema not installed" \
                "brew install asciinema (macOS) or pip install asciinema"
        fi
        for required_file in live_demo.sh lib_demo.sh; do
            if [ -f "${DEMO_DIR}/${required_file}" ]; then
                preflight_pass "${required_file} found"
            else
                preflight_fail "${required_file} not found" "Restore demo/${required_file}"
            fi
        done
        if [ -f "${REPO_ROOT}/cdk.json" ]; then
            preflight_pass "cdk.json found"
        else
            preflight_fail "cdk.json not found" "Run from a GCO checkout"
        fi
        override_status=0
        verify_enablement_overrides "$REPO_ROOT" || override_status=$?
        case "$override_status" in
            0)
                if [ -n "${GCO_DEMO_ENABLE:-}" ]; then
                    preflight_pass "Run-scoped enablement overrides valid (${GCO_DEMO_ENABLE})"
                else
                    preflight_pass "No run-scoped overrides (cdk.json defaults apply)"
                fi
                ;;
            2)
                preflight_fail "Cannot validate GCO_DEMO_ENABLE" \
                    "python3 must be available to check the requested names"
                ;;
            *)
                preflight_fail "GCO_DEMO_ENABLE names an unknown feature or chart" \
                    "Use names from gco/enablement_overrides.py (see gco stacks deploy-all --help)"
                ;;
        esac
        if command -v jq &>/dev/null; then
            preflight_pass "jq installed ($(jq --version 2>&1))"
        else
            preflight_fail "jq not installed" "brew install jq or apt install jq"
        fi
        if command -v kubectl &>/dev/null; then
            preflight_pass "kubectl installed"
        else
            preflight_fail "kubectl not installed" "Install kubectl before recording"
        fi
        if (cd "$REPO_ROOT" && python3 -c 'from cli.main import main; assert callable(main)'); then
            preflight_pass "Repository GCO CLI module importable"
        else
            preflight_fail "Repository GCO CLI module is not importable" \
                "Install this checkout's Python dependencies"
        fi
        authorization_verified=0
        if verify_legacy_live_recording_authorization "$REPO_ROOT"; then
            preflight_pass "Live consent, Git SHA, and AWS account guards verified"
            authorization_verified=1
        else
            preflight_fail "Live recording authorization failed" \
                "Set GCO_RECORDING_LIVE, GCO_EXPECTED_GIT_SHA, and GCO_EXPECTED_ACCOUNT_ID"
        fi

        kube_context_verified=0
        if [ "$authorization_verified" -eq 1 ] && [ -f "${REPO_ROOT}/cdk.json" ] && \
                command -v jq &>/dev/null && command -v kubectl &>/dev/null; then
            recording_project=$(jq -r '.context.project_name // "gco"' "${REPO_ROOT}/cdk.json")
            detect_region "${REPO_ROOT}/cdk.json"
            recording_region="$REGION"
            if verify_recording_kube_context \
                    "${recording_project}-${recording_region}" "$recording_region"; then
                preflight_pass "kubectl context matches the authorized GCO EKS cluster"
                kube_context_verified=1
            else
                preflight_fail "kubectl context does not match the authorized cluster" \
                    "Select ${recording_project}-${recording_region} before recording"
            fi
        fi
        if [ "$kube_context_verified" -eq 1 ]; then
            if kubectl get nodes --request-timeout=5s &>/dev/null; then
                preflight_pass "kubectl connected to cluster"
            else
                preflight_fail "kubectl cannot reach the cluster" \
                    "Run scripts/setup-cluster-access.sh before recording"
            fi
        fi
        ;;
    1)
        if [ -f "$CAST_FILE" ]; then
            preflight_pass "Existing live-demo cast found for offline rendering"
        else
            preflight_fail "Existing live-demo cast not found" \
                "Record once with guarded live mode before using RENDER_EXISTING=1"
        fi
        ;;
    *)
        preflight_fail "RENDER_EXISTING must be 0 or 1" \
            "Use RENDER_EXISTING=1 only for offline re-rendering"
        ;;
esac

AVAILABLE_MB=$(df -m "${DEMO_DIR}" 2>/dev/null | awk 'NR==2{print $4}' || echo "0")
if [ "$AVAILABLE_MB" -gt 100 ]; then
    preflight_pass "Disk space: ${AVAILABLE_MB} MB available"
else
    preflight_warn "Low disk space: ${AVAILABLE_MB} MB" "Free up space before rendering"
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

RECORDING_TMP_DIR=$(mktemp -d "${DEMO_DIR}/.live-demo-recording.XXXXXX")
chmod 700 "$RECORDING_TMP_DIR"
RAW_CAST_FILE="${RECORDING_TMP_DIR}/live_demo.cast"
RAW_GIF_FILE="${RECORDING_TMP_DIR}/live_demo.gif"
WRAPPER="${RECORDING_TMP_DIR}/run.sh"
RECORDING_KUBECONFIG="${RECORDING_TMP_DIR}/kubeconfig"

if [ "$RENDER_EXISTING" = "1" ]; then
    echo "Re-rendering verified live-demo cast (${COLS}x${ROWS}, speed=${SPEED}x)..."
    cp -p "$CAST_FILE" "$RAW_CAST_FILE"
else
    KUBECONFIG_TMP="${RECORDING_KUBECONFIG}.tmp"
    if ! (umask 077; kubectl config view --raw --minify --flatten > "$KUBECONFIG_TMP"); then
        echo "Unable to snapshot the authorized kubectl context for recording." >&2
        exit 1
    fi
    if [ ! -s "$KUBECONFIG_TMP" ]; then
        echo "The authorized kubectl context snapshot is empty." >&2
        exit 1
    fi
    chmod 600 "$KUBECONFIG_TMP"
    mv -f -- "$KUBECONFIG_TMP" "$RECORDING_KUBECONFIG"
    export KUBECONFIG="$RECORDING_KUBECONFIG"

    recording_project=$(jq -r '.context.project_name // "gco"' "${REPO_ROOT}/cdk.json")
    detect_region "${REPO_ROOT}/cdk.json"
    recording_region="$REGION"
    if ! verify_recording_kube_context \
            "${recording_project}-${recording_region}" "$recording_region"; then
        echo "The isolated kubeconfig does not match the authorized cluster." >&2
        exit 1
    fi
    if ! kubectl get nodes --request-timeout=5s &>/dev/null; then
        echo "The isolated kubeconfig cannot reach the authorized cluster." >&2
        exit 1
    fi
    echo "✓ Private kubeconfig snapshot verified; operator kubeconfig remains untouched"

    cat > "$WRAPPER" <<'WRAPPER_SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
cd "$REPO_ROOT"
export COLUMNS="$GCO_RECORDING_COLUMNS"
export GCO_DEMO_FAST=1
export GCO_DEMO_NONINTERACTIVE=1
export GCO_DEMO_GUARDED_RECORDING=1
gco() { python3 -m cli.main "$@"; }
# shellcheck source=demo/live_demo.sh
source "${REPO_ROOT}/demo/live_demo.sh"
WRAPPER_SCRIPT
    chmod +x "$WRAPPER"

    echo "Recording live demo (${COLS}x${ROWS})..."
    echo "Output: ${CAST_FILE}"
    export REPO_ROOT
    # Inherited by the wrapper so detect_features narrates exactly the features
    # the paired deploy recording provisioned with the same value.
    export GCO_DEMO_ENABLE="${GCO_DEMO_ENABLE:-}"
    export GCO_RECORDING_COLUMNS="$COLS"
    export GCO_RECORDING_WRAPPER="$WRAPPER"
    asciinema rec \
        --return \
        --cols "$COLS" \
        --rows "$ROWS" \
        --overwrite \
        --command "bash --norc --noprofile \"\$GCO_RECORDING_WRAPPER\"" \
        "$RAW_CAST_FILE"
    echo "✓ Raw recording complete; sanitizing before publication"
fi

sanitize_cast "$RAW_CAST_FILE"
verify_cast_sanitized "$RAW_CAST_FILE"
echo "✓ Cast sanitized and independently verified"

strip_emoji_from_cast "$RAW_CAST_FILE"
echo "✓ Unsupported glyphs normalized for agg"

if [ "${SKIP_GIF:-}" != "1" ]; then
    echo "Converting to GIF (speed=${SPEED}x, theme=${THEME})..."
    render_gif "$RAW_CAST_FILE" "$RAW_GIF_FILE" "$SPEED" "$THEME" "$COLS" "$ROWS"
fi

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

echo ""
echo "=== Done ==="
echo "To replay:       asciinema play ${CAST_FILE}"
echo "To re-render:    RENDER_EXISTING=1 DEMO_SPEED=${SPEED} bash $0"
echo "Embed in README: ![GCO Live Demo](demo/live_demo.gif)"
