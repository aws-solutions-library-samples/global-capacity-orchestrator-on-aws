#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for .github/scripts/autopilot_codex_boot_probe.sh
# -----------------------------------------------------------------------------
# The Codex twin of test_autopilot_claude_code_boot_probe.bats, against the
# same faked world (autopilot_probe_fixture.sh). The engine delta the probe
# documents shows up in what is asserted: the session runs in the foreground
# to codex's own bounded-retry exit, and the markers are read from its
# RUST_LOG=info stderr rather than a debug-log directory.
#
# Run:  bats tests/BATS/test_autopilot_codex_boot_probe.bats
# -----------------------------------------------------------------------------

load 'helpers.sh'
load 'autopilot_probe_fixture.sh'

SCRIPT="$REPO_ROOT/.github/scripts/autopilot_codex_boot_probe.sh"

setup() {
    make_probe_fixture
}

probe() {
    run_probe "$SCRIPT" "$@"
}

@test "autopilot_codex_boot_probe.sh passes bash -n and shellcheck" {
    bash -n "$SCRIPT"
    command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "the whole probe passes when every phase produces its evidence" {
    probe

    [ "$status" -eq 0 ]
    [[ "$output" == *"✓ preflight OK (pin ${FAKE_CODEX_PIN}, default model ${FAKE_CODEX_MODEL})"* ]]
    [[ "$output" == *"✓ session plan resolves: 4 MCP servers (aws-docs filesystem gco memory)"* ]]
    [[ "$output" == *"✓ pre-warm aws-docs: launched and exited on stdin EOF"* ]]
    [[ "$output" == *"✓ autopilot installed the pin and exec'd codex ${FAKE_CODEX_PIN} with the session-precedence argv"* ]]
    [[ "$output" == *"✓ written CODEX_HOME config matches the printed plan"* ]]
    [[ "$output" == *"✓ session ran to codex's own bounded-retry exit (rc 1)"* ]]
    [[ "$output" == *"✓ codex loaded the generated plan: all 4 servers in the session's mcp_servers list"* ]]
    [[ "$output" == *"✓ MCP subsystem live under codex (initialize handshake observed)"* ]]
    [[ "$output" == *"✓ codex dispatched to Bedrock Runtime with the shipped default model (${FAKE_CODEX_MODEL})"* ]]
    [[ "$output" == *"✓ AWS rejected the fabricated credentials — the exact credential boundary"* ]]
    [[ "$output" == *"MCP initialize report (servers' self-reported names):"* ]]
    [[ "$output" == *'  "gco"'* ]]
    [[ "$output" == *"autopilot codex boot probe: PASS"* ]]
    # The TOML plan was pre-warmed recipe by recipe, env first.
    grep -qxF -- "FASTMCP_LOG_LEVEL=ERROR uvx awslabs.aws-documentation-mcp-server@latest" "$PREWARM_LAUNCHES"
    grep -qxF -- "npx -y @modelcontextprotocol/server-memory" "$PREWARM_LAUNCHES"
    [ "$(grep -c '' "$PREWARM_LAUNCHES")" -eq 4 ]
    # Every autopilot invocation selected the engine; the session log was
    # kept for the artifact.
    [ "$(sed -n 1p "$GCO_CALLS")" = "autopilot --engine codex --print-config" ]
    [ "$(sed -n 2p "$GCO_CALLS")" = "autopilot --engine codex -y -- --version" ]
    [ "$(sed -n 3p "$GCO_CALLS")" = "autopilot --engine codex -- exec Reply with the single word OK." ]
    local work="$FAKE_RUNNER_TEMP/autopilot-codex-boot-probe"
    [ -f "$work/print-config.toml" ]
    [ -f "$work/config/codex/config.toml" ]
    grep -q "Turn error: unexpected status 401" "$work/session.log"
}

@test "pre-warm outcomes are classified: EOF exit, warm-up timeout, and server-specific exits all pass" {
    probe FAKE_PREWARM="server-filesystem=124;server-memory=3"

    [ "$status" -eq 0 ]
    [[ "$output" == *"✓ pre-warm filesystem: launched and ran until the warm-up timeout"* ]]
    [[ "$output" == *"✓ pre-warm memory: launched and exited on stdin EOF (rc 3)"* ]]
}

@test "a launch recipe that cannot start fails the probe with its log" {
    probe FAKE_PREWARM="run_mcp.py=125"

    [ "$status" -eq 1 ]
    [[ "$output" == *"── ${FAKE_RUNNER_TEMP}/autopilot-codex-boot-probe/prewarm/gco.log ──"* ]]
    [[ "$output" == *"✗ pre-warm gco: launch recipe failed (exit 125)"* ]]
    [[ "$output" == *"✗ 1 companion launch recipe(s) failed to start at all"* ]]
    ! grep -q -- '-y -- --version' "$GCO_CALLS"
}

@test "a session still running at the boot budget fails with the log tail" {
    probe FAKE_SESSION_TIMEOUT=1 BOOT_TIMEOUT_SECONDS=7

    [ "$status" -eq 1 ]
    [[ "$output" == *"booting the full session (budget 7s)"* ]]
    [[ "$output" == *"── session stdout/stderr"* ]]
    [[ "$output" == *"fake timeout: killed after 7s"* ]]
    [[ "$output" == *"✗ session still running after 7s — it never reached codex's own bounded-retry exit"* ]]
}

@test "a session that exits 0 with fabricated credentials fails the probe" {
    probe FAKE_SESSION=exit-zero

    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ session exited 0 with fabricated credentials — the credential boundary was never enforced"* ]]
}

@test "a session that omits markers fails naming the missing ones" {
    probe FAKE_SESSION=partial

    [ "$status" -eq 1 ]
    [[ "$output" == *"── session stdout/stderr"* ]]
    [[ "$output" == *"✗ session did not produce these boot markers: mcp-initialize-handshake bedrock-dispatch:${FAKE_CODEX_MODEL} bedrock-runtime-endpoint credential-boundary-401"* ]]
}

@test "the install path is verified: pin in --version, binary on PATH, config written and matching" {
    probe FAKE_INSTALL=wrong-version
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ autopilot exec'd codex, but its --version output does not carry the pin ${FAKE_CODEX_PIN}: 9.9.9"* ]]

    make_probe_fixture
    probe FAKE_INSTALL=no-binary
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ autopilot reported an install but codex is not on PATH"* ]]

    make_probe_fixture
    probe FAKE_INSTALL=no-config
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ autopilot did not write the isolated Codex config to ${FAKE_RUNNER_TEMP}/autopilot-codex-boot-probe/config/codex/config.toml"* ]]

    make_probe_fixture
    probe FAKE_INSTALL=other-config
    [ "$status" -ne 0 ]
    [[ "$output" == *"AssertionError: planned ['aws-docs', 'filesystem', 'gco', 'memory'] != written ['aws-docs', 'filesystem', 'gco']"* ]]
}

@test "a generated TOML that fails the shared contract stops the probe" {
    probe FAKE_CONTRACT_VERIFY=fail

    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ generated Codex config failed the shared autopilot CI contract"* ]]
    [ ! -s "$PREWARM_LAUNCHES" ]
}

@test "preflight refuses a missing tool and a preinstalled codex" {
    rm -f "$FAKE_BIN/npm"
    probe
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ required tool missing: npm"* ]]

    make_probe_fixture
    stub_noop "$FAKE_BIN" codex
    probe
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ codex is already installed at ${FAKE_BIN}/codex — this probe must exercise autopilot's own install path"* ]]
    [ ! -s "$GCO_CALLS" ]
}
