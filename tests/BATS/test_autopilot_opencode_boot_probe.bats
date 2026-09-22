#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for .github/scripts/autopilot_opencode_boot_probe.sh
# -----------------------------------------------------------------------------
# The OpenCode twin of test_autopilot_codex_boot_probe.bats, against the same
# faked world (autopilot_probe_fixture.sh). The engine delta the probe
# documents shows up in what is asserted: OpenCode is silent about healthy MCP
# servers, so per-server connection proof comes from a separate `mcp list`
# phase (whose ANSI-dimmed status text the probe de-colours), and the session
# markers are read from opencode's --print-logs stderr — including the
# small_model pin and the credential-chain regression guard.
#
# Run:  bats tests/BATS/test_autopilot_opencode_boot_probe.bats
# -----------------------------------------------------------------------------

load 'helpers.sh'
load 'autopilot_probe_fixture.sh'

SCRIPT="$REPO_ROOT/.github/scripts/autopilot_opencode_boot_probe.sh"

setup() {
    make_probe_fixture
}

probe() {
    run_probe "$SCRIPT" "$@"
}

@test "autopilot_opencode_boot_probe.sh passes bash -n and shellcheck" {
    bash -n "$SCRIPT"
    command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "the whole probe passes when every phase produces its evidence" {
    probe

    [ "$status" -eq 0 ]
    local work="$FAKE_RUNNER_TEMP/autopilot-opencode-boot-probe"
    [[ "$output" == *"✓ preflight OK (pin ${FAKE_OPENCODE_PIN}, default model ${FAKE_OPENCODE_MODEL})"* ]]
    [[ "$output" == *"✓ session plan resolves: 4 MCP servers (aws-docs filesystem gco memory)"* ]]
    [[ "$output" == *"✓ pre-warm aws-docs: launched and exited on stdin EOF"* ]]
    [[ "$output" == *"✓ autopilot installed the pin and exec'd opencode ${FAKE_OPENCODE_PIN} with the session-precedence argv"* ]]
    [[ "$output" == *"✓ written OPENCODE_CONFIG matches the printed plan (${work}/config/opencode/opencode.json)"* ]]
    [[ "$output" == *"✓ opencode connected all 4 planned MCP servers from the generated config"* ]]
    [[ "$output" == *"booting the full session (budget 420s)"* ]]
    [[ "$output" == *"✓ session ran to opencode's own failed-turn exit (rc 1)"* ]]
    [[ "$output" == *"✓ opencode loaded the generated plan (${work}/config/opencode/opencode.json)"* ]]
    [[ "$output" == *"✓ opencode dispatched to Bedrock with the shipped default model (${FAKE_OPENCODE_MODEL})"* ]]
    [[ "$output" == *"✓ title generation stayed on the session model (small_model pin honoured)"* ]]
    [[ "$output" == *"✓ AWS rejected the fabricated credentials — the exact credential boundary"* ]]
    [[ "$output" == *"MCP connection report (opencode mcp list):"* ]]
    [[ "$output" == *"  ✓ gco connected"* ]]
    [[ "$output" == *"autopilot opencode boot probe: PASS"* ]]
    # The JSON plan was pre-warmed recipe by recipe, environment first.
    grep -qxF -- "FASTMCP_LOG_LEVEL=ERROR uvx awslabs.aws-documentation-mcp-server@latest" "$PREWARM_LAUNCHES"
    grep -qxF -- "npx -y @modelcontextprotocol/server-memory" "$PREWARM_LAUNCHES"
    [ "$(grep -c '' "$PREWARM_LAUNCHES")" -eq 4 ]
    # Every autopilot invocation selected the engine; the utility subcommand
    # and the session both went through autopilot, and the logs were kept
    # for the artifact.
    [ "$(sed -n 1p "$GCO_CALLS")" = "autopilot --engine opencode --print-config" ]
    [ "$(sed -n 2p "$GCO_CALLS")" = "autopilot --engine opencode -y -- --version" ]
    [ "$(sed -n 3p "$GCO_CALLS")" = "autopilot --engine opencode -- mcp list" ]
    [ "$(sed -n 4p "$GCO_CALLS")" = "autopilot --engine opencode -- run --print-logs --log-level INFO Reply with the single word OK." ]
    [ -f "$work/print-config.json" ]
    [ -f "$work/config/opencode/opencode.json" ]
    [ -f "$work/version-probe.log" ]
    grep -q "4 server(s)" "$work/mcp-list.log"
    grep -q "The security token included in the request is invalid" "$work/session.log"
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
    [[ "$output" == *"── ${FAKE_RUNNER_TEMP}/autopilot-opencode-boot-probe/prewarm/gco.log ──"* ]]
    [[ "$output" == *"✗ pre-warm gco: launch recipe failed (exit 125)"* ]]
    [[ "$output" == *"✗ 1 companion launch recipe(s) failed to start at all"* ]]
    ! grep -q -- '-y -- --version' "$GCO_CALLS"
}

@test "mcp list must report every planned server connected, with no unhealthy entry" {
    probe FAKE_MCP_LIST=missing
    [ "$status" -eq 1 ]
    [[ "$output" == *"── mcp list output (${FAKE_RUNNER_TEMP}/autopilot-opencode-boot-probe/mcp-list.log) ──"* ]]
    [[ "$output" == *"✗ opencode mcp list did not report every planned server connected: mcp-list:memory mcp-list:count"* ]]
    # The session phase never ran: the MCP proof is a precondition for it.
    ! grep -q -- '-- run' "$GCO_CALLS"

    make_probe_fixture
    probe FAKE_MCP_LIST=unhealthy
    [ "$status" -eq 1 ]
    # The raw (still ANSI-coloured) list is echoed for the job log.
    [[ "$output" == *"◇  ✗ memory "* ]]
    [[ "$output" == *"MCP connection closed"* ]]
    [[ "$output" == *"✗ opencode mcp list did not report every planned server connected: mcp-list:memory mcp-list:unhealthy-entry"* ]]

    make_probe_fixture
    probe FAKE_MCP_LIST=exit-nonzero
    [ "$status" -eq 1 ]
    [[ "$output" == *"Error: failed to read"* ]]
    [[ "$output" == *"✗ gco autopilot --engine opencode -- mcp list exited 1"* ]]

    make_probe_fixture
    probe FAKE_MCP_LIST_TIMEOUT=1 BOOT_TIMEOUT_SECONDS=9
    [ "$status" -eq 1 ]
    [[ "$output" == *"fake timeout: killed after 9s"* ]]
    [[ "$output" == *"✗ gco autopilot --engine opencode -- mcp list exited 124"* ]]
}

@test "a session still running at the boot budget fails with the log tail" {
    probe FAKE_SESSION_TIMEOUT=1 BOOT_TIMEOUT_SECONDS=7

    [ "$status" -eq 1 ]
    [[ "$output" == *"✓ opencode connected all 4 planned MCP servers from the generated config"* ]]
    [[ "$output" == *"booting the full session (budget 7s)"* ]]
    [[ "$output" == *"── session stdout/stderr"* ]]
    [[ "$output" == *"fake timeout: killed after 7s"* ]]
    [[ "$output" == *"✗ session still running after 7s — it never reached opencode's own failed-turn exit"* ]]
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
    [[ "$output" == *"✗ session did not produce these boot markers: generated-config-loaded bedrock-dispatch:${FAKE_OPENCODE_MODEL} small-model-pin:${FAKE_OPENCODE_MODEL} credential-boundary-sigv4-rejection"* ]]
}

@test "a session that never consulted the exported static keys fails as a credential-chain bypass" {
    probe FAKE_SESSION=credential-chain-bypass

    [ "$status" -eq 1 ]
    [[ "$output" == *"── session stdout/stderr"* ]]
    [[ "$output" == *"Could not load credentials from any providers"* ]]
    [[ "$output" == *"✗ opencode never used the exported static keys — the generated config bypassed the SDK credential chain (pinned AWS profile?)"* ]]
    # This diagnosis wins over the generic missing-marker report.
    [[ "$output" != *"did not produce these boot markers"* ]]
}

@test "the install path is verified: pin in --version, binary on PATH, config written and matching" {
    probe FAKE_INSTALL=wrong-version
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ autopilot exec'd opencode, but its --version output does not carry the pin ${FAKE_OPENCODE_PIN}: 9.9.9"* ]]

    make_probe_fixture
    probe FAKE_INSTALL=no-binary
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ autopilot reported an install but opencode is not on PATH"* ]]

    make_probe_fixture
    probe FAKE_INSTALL=no-config
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ autopilot did not write the isolated OpenCode config to ${FAKE_RUNNER_TEMP}/autopilot-opencode-boot-probe/config/opencode/opencode.json"* ]]

    make_probe_fixture
    probe FAKE_INSTALL=other-config
    [ "$status" -ne 0 ]
    [[ "$output" == *"AssertionError: planned ['aws-docs', 'filesystem', 'gco', 'memory'] != written ['aws-docs', 'filesystem', 'gco']"* ]]
}

@test "a generated JSON that fails the shared contract stops the probe" {
    probe FAKE_CONTRACT_VERIFY=fail

    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ generated OpenCode config failed the shared autopilot CI contract"* ]]
    [ ! -s "$PREWARM_LAUNCHES" ]
}

@test "preflight refuses a missing tool and a preinstalled opencode" {
    rm -f "$FAKE_BIN/npm"
    probe
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ required tool missing: npm"* ]]

    make_probe_fixture
    stub_noop "$FAKE_BIN" opencode
    probe
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ opencode is already installed at ${FAKE_BIN}/opencode — this probe must exercise autopilot's own install path"* ]]
    [ ! -s "$GCO_CALLS" ]
}
