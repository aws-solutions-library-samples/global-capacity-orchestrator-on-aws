#!/usr/bin/env bash
# =============================================================================
# autopilot_opencode_boot_probe.sh — boot the real `gco autopilot` OpenCode session
# =============================================================================
#
# Drives `gco autopilot --engine opencode` end-to-end the way a first-time
# user does, and verifies the session boots to the last point reachable
# without real AWS credentials. Used by integration:autopilot:opencode-boot
# (integration-tests.yml). The Claude Code and Codex twins live in
# autopilot_claude_code_boot_probe.sh and autopilot_codex_boot_probe.sh; the
# phases are parallel on purpose so the probes stay comparable engine to
# engine.
#
# What runs for real (nothing about autopilot is mocked):
#
#   1. `gco autopilot --engine opencode --print-config` resolves the session
#      plan from this checkout (the in-tree gco MCP server plus the curated
#      companion registry, rendered as OpenCode's opencode.json).
#   2. Every mcp.* entry in the generated JSON is pre-warmed by running its
#      exact launch recipe (uvx/npx resolve, install, boot, exit on stdin
#      EOF). Warm caches keep the integrated boot inside OpenCode's configured
#      per-server timeout on cold runners.
#   3. `gco autopilot --engine opencode -y -- --version` exercises autopilot's
#      own install path: detect the missing binary, npm-install the pinned
#      release (with the postinstall that fetches OpenCode's native binary),
#      re-detect it, write the isolated OPENCODE_CONFIG file, and exec
#      opencode with the session-precedence overrides. The passthrough
#      `--version` makes that exec exit 0 deterministically.
#   4. `gco autopilot --engine opencode -- mcp list` connects every planned
#      MCP server under OpenCode itself and prints a per-server status. This
#      is the positive MCP proof for this engine: the pinned OpenCode release
#      logs MCP servers only when they FAIL ("server unavailable"), so a
#      quiet session log is not evidence — `mcp list` is.
#   5. `gco autopilot --engine opencode -- run "..."` boots the full
#      non-interactive stack: opencode loads the generated config, connects
#      the MCP servers, and dispatches to Amazon Bedrock (Converse, via the
#      AI SDK's amazon-bedrock provider) with the shipped default model. With
#      the fail-closed fake credentials exported below, SigV4 validation
#      answers UnrecognizedClientException — proving a signed request left
#      the wire. The probe asserts, from opencode's own --print-logs stderr:
#
#        - loading path=.../opencode/opencode.json     (the generated config)
#        - stream providerID=amazon-bedrock modelID=<default> agent=build
#        - stream ... small=true agent=title modelID=<default>
#          (the small_model pin: title generation stays on the session model
#          instead of OpenCode's auto-picked Claude Haiku)
#        - The security token included in the request is invalid.
#
#      and that the SDK credential chain was NOT bypassed: "Could not load
#      credentials from any providers" is the signature of a generated config
#      that pinned an AWS profile over the exported static keys, which makes
#      the AWS SDK skip them entirely.
#
# Engine delta vs the other probes, asserted honestly: Codex logs an MCP
# initialize handshake and Claude Code a per-server "Successfully connected"
# line; OpenCode is silent about healthy servers. Per-server *launch* proof
# therefore lives in the pre-warm phase, per-server *connection under
# OpenCode* in the `mcp list` phase, and the session phase proves the Bedrock
# dispatch and the credential boundary.
#
# Marker stability: the log markers above were captured from the pinned
# OpenCode release (cli/autopilot.py OPENCODE_VERSION). A pin bump can
# rephrase them; the failure output names the missing marker so the bump PR
# can refresh this probe alongside the pin.
#
# Requirements: gco (this checkout, installed), node+npm (pinned via
# .github/scripts/use-pinned-npm.sh; npm >= 11.5 so --allow-scripts is
# honoured), uv/uvx, python3, GNU coreutils `timeout`. The `opencode` binary
# must NOT be preinstalled — the probe exists to prove autopilot's own install
# path works.
#
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

WORK_DIR="${RUNNER_TEMP:-$(mktemp -d)}/autopilot-opencode-boot-probe"
mkdir -p "$WORK_DIR"

# Autopilot writes the session config (the isolated opencode.json beneath it)
# here instead of ~/.gco/autopilot.
export GCO_AUTOPILOT_CONFIG_DIR="${WORK_DIR}/config"

SESSION_LOG="${WORK_DIR}/session.log"
MCP_LIST_LOG="${WORK_DIR}/mcp-list.log"
PREWARM_DIR="${WORK_DIR}/prewarm"
mkdir -p "$PREWARM_DIR"

# How long the integrated session may take to reach the credential boundary.
# OpenCode fails the turn on the first rejected request (no retry loop), so
# this budget is dominated by MCP connection time on a cold runner.
BOOT_TIMEOUT_SECONDS="${BOOT_TIMEOUT_SECONDS:-420}"

# The one EXIT trap: gather evidence for the always-uploaded artifact. The
# generated JSON already lives under WORK_DIR (GCO_AUTOPILOT_CONFIG_DIR), so
# only the session log needs copying.
collect_and_cleanup() {
    cp -f "$SESSION_LOG" "${WORK_DIR}/session.log" 2>/dev/null || true
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
unset AWS_BEARER_TOKEN_BEDROCK AWS_CONTAINER_CREDENTIALS_RELATIVE_URI AWS_CONTAINER_CREDENTIALS_FULL_URI 2>/dev/null || true

# ── Preflight ────────────────────────────────────────────────────────────────

for tool in gco npm uvx python3 timeout; do
    command -v "$tool" >/dev/null || fail "required tool missing: $tool"
done

if command -v opencode >/dev/null; then
    fail "opencode is already installed at $(command -v opencode) — this probe must exercise autopilot's own install path"
fi

# Facts come from the shared autopilot CI contract — the same single source
# unit:cli:autopilot, the dev-container step, and the other probes assert
# against.
CONTRACT=".github/scripts/autopilot_ci_contract.py"
OPENCODE_PIN="$(python3 "$CONTRACT" pin --engine opencode)"
EXPECTED_MODEL="$(python3 "$CONTRACT" default-model --engine opencode)"
pass "preflight OK (pin ${OPENCODE_PIN}, default model ${EXPECTED_MODEL})"

# ── Phase 1: resolve the session plan from this checkout ────────────────────

GENERATED_CONFIG="${WORK_DIR}/print-config.json"
gco autopilot --engine opencode --print-config > "$GENERATED_CONFIG"

# Full structural validation from the shared contract (model/small_model
# selectors, provider region, update/share/permission policy, exact expected
# server set, local MCP entry shapes). `--no-profile` is the regression guard
# for the credential chain: with static keys exported, the config must not
# pin an AWS profile, or the SDK would skip those keys.
python3 "$CONTRACT" verify-opencode-config "$GENERATED_CONFIG" --no-profile \
    || fail "generated OpenCode config failed the shared autopilot CI contract"
mapfile -t SERVER_NAMES < <(python3 "$CONTRACT" expected-servers)
[ "${#SERVER_NAMES[@]}" -ge 2 ] || fail "contract lists ${#SERVER_NAMES[@]} servers; expected the gco server plus companions"
pass "session plan resolves: ${#SERVER_NAMES[@]} MCP servers (${SERVER_NAMES[*]})"

# ── Phase 2: pre-warm every server's exact launch recipe ────────────────────
# Each companion is launched exactly as the generated JSON specifies and
# handed EOF on stdin, which a stdio MCP server treats as client disconnect.
# This resolves and installs every uvx/npx package (an independent
# per-package install check with a pinpointed log on failure) and warms the
# caches so the integrated boot below is not racing package managers against
# OpenCode's per-server timeout.

mapfile -t PREWARM_CMDS < <(python3 - "$GENERATED_CONFIG" <<'PY'
import json, shlex, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)
for name, entry in sorted(config["mcp"].items()):
    env_prefix = " ".join(
        f"{key}={shlex.quote(str(value))}" for key, value in entry.get("environment", {}).items()
    )
    command = " ".join(shlex.quote(str(part)) for part in entry["command"])
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
            # doesn't exit on EOF. The in-session behavior happens later
            # under opencode, where connection management is opencode's job.
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
# opencode is absent, so `-y` makes autopilot npm-install the exact pin
# (allowing only opencode-ai's own postinstall, which fetches the native
# binary), re-detect the binary, write the isolated opencode.json, and exec it
# with the session-precedence argv. `--version` in the passthrough position
# makes that real exec terminate deterministically.

VERSION_OUTPUT="$(gco autopilot --engine opencode -y -- --version 2>&1 | tee "${WORK_DIR}/version-probe.log")"
echo "$VERSION_OUTPUT" | grep -qF "$OPENCODE_PIN" \
    || fail "autopilot exec'd opencode, but its --version output does not carry the pin ${OPENCODE_PIN}: ${VERSION_OUTPUT}"
command -v opencode >/dev/null || fail "autopilot reported an install but opencode is not on PATH"
pass "autopilot installed the pin and exec'd opencode ${OPENCODE_PIN} with the session-precedence argv"

WRITTEN_CONFIG="${GCO_AUTOPILOT_CONFIG_DIR}/opencode/opencode.json"
[ -f "$WRITTEN_CONFIG" ] || fail "autopilot did not write the isolated OpenCode config to ${WRITTEN_CONFIG}"
python3 - "$GENERATED_CONFIG" "$WRITTEN_CONFIG" <<'PY'
import json, sys
def servers(path):
    with open(path, encoding="utf-8") as handle:
        return set(json.load(handle)["mcp"])
planned, written = servers(sys.argv[1]), servers(sys.argv[2])
assert planned == written, f"planned {sorted(planned)} != written {sorted(written)}"
PY
pass "written OPENCODE_CONFIG matches the printed plan (${WRITTEN_CONFIG})"

# ── Phase 4: every planned MCP server connects under opencode itself ────────
# `mcp list` is an OpenCode utility subcommand: autopilot hands it the
# generated config through the same environment a session gets and withholds
# the session-only flags. OpenCode connects each configured server and prints
# one status line per server (`✓ <name> connected`; `✗ <name> failed`,
# `○ <name> disabled`, `⚠ <name> needs authentication` otherwise) plus a
# `<N> server(s)` trailer. The status text is dimmed with an ANSI colour code
# even when stdout is not a terminal, so the output is de-coloured before it
# is matched.

MCP_LIST_RC=0
timeout "$BOOT_TIMEOUT_SECONDS" gco autopilot --engine opencode -- mcp list \
    </dev/null >"$MCP_LIST_LOG" 2>&1 || MCP_LIST_RC=$?
[ "$MCP_LIST_RC" -eq 0 ] || {
    echo "── mcp list output (${MCP_LIST_LOG}) ──"
    tail -60 "$MCP_LIST_LOG" || true
    fail "gco autopilot --engine opencode -- mcp list exited ${MCP_LIST_RC}"
}
ESC="$(printf '\033')"
MCP_LIST="$(sed "s/${ESC}\[[0-9;]*m//g" "$MCP_LIST_LOG")"
MISSING=""
for name in "${SERVER_NAMES[@]}"; do
    grep -qF "✓ ${name} connected" <<<"$MCP_LIST" || MISSING+="mcp-list:${name} "
done
grep -qF "${#SERVER_NAMES[@]} server(s)" <<<"$MCP_LIST" || MISSING+="mcp-list:count "
if grep -qE '(✗|○|⚠) [^ ]+ ' <<<"$MCP_LIST"; then
    MISSING+="mcp-list:unhealthy-entry "
fi
if [ -n "$MISSING" ]; then
    echo "── mcp list output (${MCP_LIST_LOG}) ──"
    tail -60 "$MCP_LIST_LOG" || true
    fail "opencode mcp list did not report every planned server connected: ${MISSING}"
fi
pass "opencode connected all ${#SERVER_NAMES[@]} planned MCP servers from the generated config"

# ── Phase 5: full session boot, stopped at the credential boundary ──────────
# --print-logs surfaces opencode's structured log on stderr: the config files
# it loads, the Bedrock stream events (provider, model, agent) and the
# terminal error. `opencode run` fails the turn on the first rejected request
# and exits nonzero by itself, so this run is awaited in the foreground; the
# expected exit is nonzero and asserted as such.

echo "booting the full session (budget ${BOOT_TIMEOUT_SECONDS}s): gco autopilot --engine opencode -- run ..."
SESSION_RC=0
timeout "$BOOT_TIMEOUT_SECONDS" \
    gco autopilot --engine opencode -- run --print-logs --log-level INFO "Reply with the single word OK." \
    </dev/null >"$SESSION_LOG" 2>&1 || SESSION_RC=$?

if [ "$SESSION_RC" -eq 124 ]; then
    echo "── session stdout/stderr (${SESSION_LOG}) ──"
    tail -50 "$SESSION_LOG" || true
    fail "session still running after ${BOOT_TIMEOUT_SECONDS}s — it never reached opencode's own failed-turn exit"
fi
[ "$SESSION_RC" -ne 0 ] \
    || fail "session exited 0 with fabricated credentials — the credential boundary was never enforced"
pass "session ran to opencode's own failed-turn exit (rc ${SESSION_RC})"

LOGS="$(cat "$SESSION_LOG")"

MISSING=""
grep -qF "loading path=${WRITTEN_CONFIG}" <<<"$LOGS" \
    || MISSING+="generated-config-loaded "
grep -qE "message=stream providerID=amazon-bedrock modelID=${EXPECTED_MODEL}([[:space:]].*)?agent=build" <<<"$LOGS" \
    || MISSING+="bedrock-dispatch:${EXPECTED_MODEL} "
grep -qE "message=stream providerID=amazon-bedrock modelID=${EXPECTED_MODEL}([[:space:]].*)?small=true" <<<"$LOGS" \
    || MISSING+="small-model-pin:${EXPECTED_MODEL} "
grep -qE 'security token included in the request is invalid|UnrecognizedClientException|InvalidSignatureException' <<<"$LOGS" \
    || MISSING+="credential-boundary-sigv4-rejection "

if grep -qF "Could not load credentials from any providers" <<<"$LOGS"; then
    echo "── session stdout/stderr (${SESSION_LOG}) ──"
    tail -80 "$SESSION_LOG" || true
    fail "opencode never used the exported static keys — the generated config bypassed the SDK credential chain (pinned AWS profile?)"
fi

if [ -n "$MISSING" ]; then
    echo "── session stdout/stderr (${SESSION_LOG}) ──"
    tail -80 "$SESSION_LOG" || true
    fail "session did not produce these boot markers: ${MISSING}"
fi

pass "opencode loaded the generated plan (${WRITTEN_CONFIG})"
pass "opencode dispatched to Bedrock with the shipped default model (${EXPECTED_MODEL})"
pass "title generation stayed on the session model (small_model pin honoured)"
pass "AWS rejected the fabricated credentials — the exact credential boundary"

# Per-server connection evidence for the job summary (from Phase 4).
echo ""
echo "MCP connection report (opencode mcp list):"
grep -F '✓ ' <<<"$MCP_LIST" | sed 's/^[^✓]*✓/  ✓/' || true

echo ""
echo "autopilot opencode boot probe: PASS"
