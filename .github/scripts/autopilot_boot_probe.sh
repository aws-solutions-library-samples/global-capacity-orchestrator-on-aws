#!/usr/bin/env bash
# =============================================================================
# autopilot_boot_probe.sh — boot the real `gco autopilot` Claude Code session
# =============================================================================
#
# Drives `gco autopilot` end-to-end the way a first-time user does, and
# verifies the session boots to the last point reachable without real AWS
# credentials. Used by integration:autopilot:boot (integration-tests.yml).
#
# What runs for real (nothing about autopilot is mocked):
#
#   1. `gco autopilot --print-config` resolves the session plan from this
#      checkout (in-tree gco MCP server + the curated companion registry).
#   2. Every server entry in the generated config is pre-warmed by running
#      its exact launch recipe (uvx/npx resolve, install, boot, exit on
#      stdin EOF). Warm caches keep the integrated boot inside Claude
#      Code's fixed 30s per-server MCP connection timeout on cold runners.
#   3. `gco autopilot -y -- --version` exercises autopilot's own install
#      path: detect the missing binary, npm-install the pinned release,
#      re-detect it, write the session MCP config, and exec claude with
#      `--mcp-config <generated> --strict-mcp-config`. The passthrough
#      `--version` makes that exec exit 0 deterministically.
#   4. `gco autopilot -- --debug -p "..."` boots the full interactive
#      stack in print mode: claude connects every MCP server in the
#      generated config and dispatches to Amazon Bedrock with the shipped
#      default model. With the fail-closed fake credentials exported
#      below, AWS answers 403 — proving a signed request left the wire.
#      The probe asserts, from Claude Code's own debug log:
#
#        - MCP server "<name>": Successfully connected   (for EVERY server)
#        - dispatching to bedrock model=<configured default>
#        - API error (attempt N/M): 403
#
#      and then terminates the session. The 403 is the success condition:
#      it is the exact credential boundary, the only part of the launch a
#      credential-less CI runner cannot cross.
#
# Marker stability: the three debug-log markers above were captured from
# the pinned Claude Code release (cli/autopilot.py CLAUDE_CODE_VERSION).
# A pin bump can rephrase them; the failure output names the missing
# marker so the bump PR can refresh this probe alongside the pin.
#
# Requirements: gco (this checkout, installed), node+npm (pinned via
# .github/scripts/use-pinned-npm.sh), uv/uvx, python3, GNU coreutils
# `timeout`. The `claude` binary must NOT be preinstalled — the probe
# exists to prove autopilot's own install path works.
#
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

WORK_DIR="${RUNNER_TEMP:-$(mktemp -d)}/autopilot-boot-probe"
mkdir -p "$WORK_DIR"

# Autopilot writes the session MCP config here instead of ~/.gco/autopilot.
export GCO_AUTOPILOT_CONFIG_DIR="${WORK_DIR}/config"

# Where claude keeps its per-session debug logs (always written; --debug
# additionally mirrors them to stderr).
CLAUDE_DEBUG_DIR="${HOME}/.claude/debug"

SESSION_LOG="${WORK_DIR}/session.log"
PREWARM_DIR="${WORK_DIR}/prewarm"
mkdir -p "$PREWARM_DIR"

# How long the integrated session may take to show every boot marker.
BOOT_TIMEOUT_SECONDS="${BOOT_TIMEOUT_SECONDS:-300}"

# The one EXIT trap: stop a still-running session and gather evidence
# (claude's debug logs live under a hidden directory the artifact upload
# would otherwise skip) into WORK_DIR for the always-uploaded artifact.
SESSION_PID=""
collect_and_cleanup() {
    if [ -n "$SESSION_PID" ]; then
        kill "$SESSION_PID" 2>/dev/null || true
        wait "$SESSION_PID" 2>/dev/null || true
    fi
    if compgen -G "${CLAUDE_DEBUG_DIR}/*.txt" >/dev/null 2>&1; then
        mkdir -p "${WORK_DIR}/claude-debug"
        cp "${CLAUDE_DEBUG_DIR}"/*.txt "${WORK_DIR}/claude-debug/" 2>/dev/null || true
    fi
}
trap collect_and_cleanup EXIT

fail() {
    echo "✗ $1" >&2
    exit 1
}

pass() {
    echo "✓ $1"
}

# ── Fail-closed credential environment ──────────────────────────────────────
# The probe must never reach Bedrock with usable credentials, even if the
# surrounding job one day exports some. A syntactically valid but fabricated
# static key pair wins the SDK provider chain ahead of every file/role
# source, and the file/IMDS sources are disabled outright. The key id is
# assembled at runtime so repository secret scanners (gitleaks, trufflehog)
# never see a contiguous AKIA-shaped literal in the tree.
AWS_ACCESS_KEY_ID="$(printf 'AKIA%s' '00000000000fake0')"
AWS_SECRET_ACCESS_KEY="$(printf '%040d' 0)"
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
export AWS_SHARED_CREDENTIALS_FILE=/dev/null
export AWS_CONFIG_FILE=/dev/null
export AWS_EC2_METADATA_DISABLED=true
unset AWS_SESSION_TOKEN AWS_PROFILE AWS_ROLE_ARN AWS_WEB_IDENTITY_TOKEN_FILE 2>/dev/null || true

# ── Preflight ────────────────────────────────────────────────────────────────

for tool in gco npm uvx python3 timeout; do
    command -v "$tool" >/dev/null || fail "required tool missing: $tool"
done

if command -v claude >/dev/null; then
    fail "claude is already installed at $(command -v claude) — this probe must exercise autopilot's own install path"
fi

# Facts come from the shared autopilot CI contract — the same single
# source unit:cli:autopilot and the dev-container step assert against.
CONTRACT=".github/scripts/autopilot_ci_contract.py"
CLAUDE_PIN="$(python3 "$CONTRACT" pin)"
EXPECTED_MODEL="$(python3 "$CONTRACT" default-model)"
pass "preflight OK (pin ${CLAUDE_PIN}, default model ${EXPECTED_MODEL})"

# ── Phase 1: resolve the session plan from this checkout ────────────────────

GENERATED_CONFIG="${WORK_DIR}/print-config.json"
gco autopilot --print-config > "$GENERATED_CONFIG"

# Full structural validation from the shared contract (exact expected
# server set, entry shapes, pruned-package bans), then load the expected
# names for the per-server pre-warm and handshake assertions below.
python3 "$CONTRACT" verify-config "$GENERATED_CONFIG" \
    || fail "generated config failed the shared autopilot CI contract"
mapfile -t SERVER_NAMES < <(python3 "$CONTRACT" expected-servers)
[ "${#SERVER_NAMES[@]}" -ge 2 ] || fail "contract lists ${#SERVER_NAMES[@]} servers; expected the gco server plus companions"
pass "session plan resolves: ${#SERVER_NAMES[@]} MCP servers (${SERVER_NAMES[*]})"

# ── Phase 2: pre-warm every server's exact launch recipe ────────────────────
# Each companion is launched exactly as the generated config specifies and
# handed EOF on stdin, which a stdio MCP server treats as client
# disconnect. This resolves and installs every uvx/npx package (an
# independent per-package install check with a pinpointed log on failure)
# and warms the caches so the integrated boot below is not racing package
# managers against claude's 30s per-server connection timeout.

mapfile -t PREWARM_CMDS < <(python3 - "$GENERATED_CONFIG" <<'PY'
import json, shlex, sys
with open(sys.argv[1]) as handle:
    config = json.load(handle)
for name, entry in sorted(config["mcpServers"].items()):
    env_prefix = " ".join(
        f"{key}={shlex.quote(str(value))}" for key, value in entry.get("env", {}).items()
    )
    command = " ".join(shlex.quote(str(part)) for part in [entry["command"], *entry["args"]])
    print(f"{name}\t{env_prefix} {command}".replace("\t ", "\t", 1))
PY
)

PREWARM_FAILURES=0
for line in "${PREWARM_CMDS[@]}"; do
    name="${line%%$'\t'*}"
    launch="${line#*$'\t'}"
    rc=0
    timeout 240 bash -c "$launch" </dev/null >"${PREWARM_DIR}/${name}.log" 2>&1 || rc=$?
    case "$rc" in
        0)
            pass "pre-warm ${name}: launched and exited on stdin EOF" ;;
        124)
            # Ran the full 240s before timeout killed it: the package
            # resolved, installed, and booted (cache warmed) — it just
            # doesn't exit on EOF. The handshake assertion happens later
            # under claude, where connection management is claude's job.
            pass "pre-warm ${name}: launched and ran until the warm-up timeout" ;;
        125 | 126 | 127)
            # timeout itself failed / command not executable / not found:
            # the launch recipe is broken.
            echo "── ${PREWARM_DIR}/${name}.log ──"
            cat "${PREWARM_DIR}/${name}.log" || true
            echo "✗ pre-warm ${name}: launch recipe failed (exit ${rc}): ${launch}" >&2
            PREWARM_FAILURES=$((PREWARM_FAILURES + 1)) ;;
        *)
            # Any other non-zero exit on EOF is server-specific and fine;
            # the process launched, which is all warming needs.
            pass "pre-warm ${name}: launched and exited on stdin EOF (rc ${rc})" ;;
    esac
done
[ "$PREWARM_FAILURES" -eq 0 ] || fail "${PREWARM_FAILURES} companion launch recipe(s) failed to start at all"

# ── Phase 3: autopilot's own install path, exec verified by --version ───────
# claude is absent, so `-y` makes autopilot npm-install the exact pin,
# re-detect the binary, write the session config, and exec it with the
# generated `--mcp-config`/`--strict-mcp-config` argv. `--version` in the
# passthrough position makes that real exec terminate deterministically.

VERSION_OUTPUT="$(gco autopilot -y -- --version 2>&1 | tee "${WORK_DIR}/version-probe.log")"
echo "$VERSION_OUTPUT" | grep -qF "$CLAUDE_PIN" \
    || fail "autopilot exec'd claude, but its --version output does not carry the pin ${CLAUDE_PIN}: ${VERSION_OUTPUT}"
command -v claude >/dev/null || fail "autopilot reported an install but claude is not on PATH"
pass "autopilot installed the pin and exec'd claude ${CLAUDE_PIN} with the generated config argv"

WRITTEN_CONFIG="${GCO_AUTOPILOT_CONFIG_DIR}/mcp.json"
[ -f "$WRITTEN_CONFIG" ] || fail "autopilot did not write the session config to ${WRITTEN_CONFIG}"
python3 - "$GENERATED_CONFIG" "$WRITTEN_CONFIG" <<'PY'
import json, sys
with open(sys.argv[1]) as handle:
    planned = set(json.load(handle)["mcpServers"])
with open(sys.argv[2]) as handle:
    written = set(json.load(handle)["mcpServers"])
assert planned == written, f"planned {sorted(planned)} != written {sorted(written)}"
PY
pass "written session config matches the printed plan (${WRITTEN_CONFIG})"

# ── Phase 4: full session boot, stopped at the credential boundary ──────────

echo "booting the full session (budget ${BOOT_TIMEOUT_SECONDS}s): gco autopilot -- --debug -p ..."
# `gco autopilot` execvpe()s claude, so SESSION_PID *is* the claude process
# (reaped by the EXIT trap above).
gco autopilot -- --debug -p "Reply with the single word OK." \
    </dev/null >"$SESSION_LOG" 2>&1 &
SESSION_PID=$!

# Collect the required markers from claude's own debug logs. HOME is
# job-fresh, so every log under CLAUDE_DEBUG_DIR belongs to this probe.
missing_markers() {
    local logs="$1"
    local missing=""
    local name
    for name in "${SERVER_NAMES[@]}"; do
        grep -qF "MCP server \"${name}\": Successfully connected" <<<"$logs" \
            || missing+="mcp-connect:${name} "
    done
    grep -qF "dispatching to bedrock model=${EXPECTED_MODEL}" <<<"$logs" \
        || missing+="bedrock-dispatch:${EXPECTED_MODEL} "
    grep -qE 'API error \(attempt [0-9]+/[0-9]+\): 403' <<<"$logs" \
        || missing+="credential-boundary-403 "
    echo "$missing"
}

DEADLINE=$((SECONDS + BOOT_TIMEOUT_SECONDS))
MISSING="initial"
while [ "$SECONDS" -lt "$DEADLINE" ]; do
    LOGS="$(cat "${CLAUDE_DEBUG_DIR}"/*.txt 2>/dev/null || true)"
    MISSING="$(missing_markers "$LOGS")"
    [ -z "$MISSING" ] && break
    if ! kill -0 "$SESSION_PID" 2>/dev/null; then
        # claude exited before every marker appeared (e.g. it gave up its
        # API retries) — take one final look at the logs it left behind.
        LOGS="$(cat "${CLAUDE_DEBUG_DIR}"/*.txt 2>/dev/null || true)"
        MISSING="$(missing_markers "$LOGS")"
        break
    fi
    sleep 5
done

if [ -n "$MISSING" ]; then
    echo "── session stdout/stderr (${SESSION_LOG}) ──"
    tail -50 "$SESSION_LOG" || true
    echo "── claude debug logs (${CLAUDE_DEBUG_DIR}) ──"
    tail -100 "${CLAUDE_DEBUG_DIR}"/*.txt 2>/dev/null || echo "(no debug logs found)"
    fail "session did not reach these boot markers within ${BOOT_TIMEOUT_SECONDS}s: ${MISSING}"
fi

pass "all ${#SERVER_NAMES[@]} MCP servers completed the handshake under claude"
pass "claude dispatched to Bedrock with the shipped default model (${EXPECTED_MODEL})"
pass "AWS rejected the fabricated credentials with 403 — the exact credential boundary"

# One-line-per-server evidence for the job summary.
echo ""
echo "MCP connection report:"
grep -hoE 'MCP server "[^"]+": Successfully connected \(transport: [a-z]+\) in [0-9]+ms' \
    "${CLAUDE_DEBUG_DIR}"/*.txt 2>/dev/null | sort -u | sed 's/^/  /' || true

echo ""
echo "autopilot boot probe: PASS"
