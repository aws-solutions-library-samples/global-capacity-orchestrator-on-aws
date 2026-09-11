#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for .github/scripts/autopilot_claude_code_boot_probe.sh
# -----------------------------------------------------------------------------
# The probe boots the real Claude Code session in integration:autopilot:
# claude-code-boot. Here it runs against the faked world in
# autopilot_probe_fixture.sh, which scripts what each external answers, so
# every phase and every failure branch of the probe's own logic is exercised
# offline: preflight, the session plan, the pre-warm classification, the
# install path, and the marker collection from claude's debug log.
#
# Run:  bats tests/BATS/test_autopilot_claude_code_boot_probe.bats
# -----------------------------------------------------------------------------

load 'helpers.sh'
load 'autopilot_probe_fixture.sh'

SCRIPT="$REPO_ROOT/.github/scripts/autopilot_claude_code_boot_probe.sh"

setup() {
    make_probe_fixture
}

probe() {
    run_probe "$SCRIPT" "$@"
}

@test "autopilot_claude_code_boot_probe.sh passes bash -n and shellcheck" {
    bash -n "$SCRIPT"
    command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "the whole probe passes when every phase produces its evidence" {
    probe

    [ "$status" -eq 0 ]
    [[ "$output" == *"✓ preflight OK (pin ${FAKE_CLAUDE_PIN}, default model ${FAKE_CLAUDE_MODEL})"* ]]
    [[ "$output" == *"✓ session plan resolves: 4 MCP servers (aws-docs filesystem gco memory)"* ]]
    [[ "$output" == *"✓ pre-warm aws-docs: launched and exited on stdin EOF"* ]]
    [[ "$output" == *"✓ autopilot installed the pin and exec'd claude ${FAKE_CLAUDE_PIN} with the generated config argv"* ]]
    [[ "$output" == *"✓ written session config matches the printed plan"* ]]
    [[ "$output" == *"✓ all 4 MCP servers completed the handshake under claude"* ]]
    [[ "$output" == *"✓ claude dispatched to Bedrock with the shipped default model (${FAKE_CLAUDE_MODEL})"* ]]
    [[ "$output" == *"✓ AWS rejected the fabricated credentials with 403 — the exact credential boundary"* ]]
    [[ "$output" == *'MCP server "gco": Successfully connected (transport: stdio) in 12ms'* ]]
    [[ "$output" == *"autopilot boot probe: PASS"* ]]
    # Each recipe was pre-warmed exactly as the plan spells it, env first.
    grep -qxF -- "FASTMCP_LOG_LEVEL=ERROR uvx awslabs.aws-documentation-mcp-server@latest" "$PREWARM_LAUNCHES"
    grep -qxF -- "npx -y @modelcontextprotocol/server-filesystem ${REPO_ROOT}" "$PREWARM_LAUNCHES"
    grep -qxF -- "python3 ${REPO_ROOT}/gco_mcp/run_mcp.py" "$PREWARM_LAUNCHES"
    [ "$(grep -c '' "$PREWARM_LAUNCHES")" -eq 4 ]
    # The three autopilot invocations, in order; the evidence was gathered
    # into the work directory for the artifact upload.
    [ "$(sed -n 1p "$GCO_CALLS")" = "autopilot --print-config" ]
    [ "$(sed -n 2p "$GCO_CALLS")" = "autopilot -y -- --version" ]
    [ "$(sed -n 3p "$GCO_CALLS")" = "autopilot -- --debug -p Reply with the single word OK." ]
    local work="$FAKE_RUNNER_TEMP/autopilot-claude-code-boot-probe"
    [ -f "$work/print-config.json" ]
    [ -f "$work/version-probe.log" ]
    [ -f "$work/config/mcp.json" ]
    [ -n "$(compgen -G "$work/claude-debug/*.txt")" ]
}

@test "pre-warm outcomes are classified: EOF exit, warm-up timeout, and server-specific exits all pass" {
    probe FAKE_PREWARM="server-filesystem=124;server-memory=3"

    [ "$status" -eq 0 ]
    [[ "$output" == *"✓ pre-warm filesystem: launched and ran until the warm-up timeout"* ]]
    [[ "$output" == *"✓ pre-warm memory: launched and exited on stdin EOF (rc 3)"* ]]
    [[ "$output" == *"✓ pre-warm gco: launched and exited on stdin EOF"* ]]
}

@test "a launch recipe that cannot start fails the probe with its log" {
    probe FAKE_PREWARM="aws-documentation=127;server-memory=126"

    [ "$status" -eq 1 ]
    [[ "$output" == *"── ${FAKE_RUNNER_TEMP}/autopilot-claude-code-boot-probe/prewarm/aws-docs.log ──"* ]]
    [[ "$output" == *"fake launch: FASTMCP_LOG_LEVEL=ERROR uvx awslabs.aws-documentation-mcp-server@latest"* ]]
    [[ "$output" == *"✗ pre-warm aws-docs: launch recipe failed (exit 127)"* ]]
    [[ "$output" == *"✗ pre-warm memory: launch recipe failed (exit 126)"* ]]
    [[ "$output" == *"✗ 2 companion launch recipe(s) failed to start at all"* ]]
    ! grep -q -- '-y -- --version' "$GCO_CALLS"
}

@test "a session that takes a while to log its markers is polled until they appear" {
    probe FAKE_SESSION=delayed

    [ "$status" -eq 0 ]
    [[ "$output" == *"autopilot boot probe: PASS"* ]]
}

@test "a session that exits without every marker fails naming the missing ones" {
    probe FAKE_SESSION=partial

    [ "$status" -eq 1 ]
    [[ "$output" == *"── session stdout/stderr"* ]]
    [[ "$output" == *"── claude debug logs"* ]]
    [[ "$output" == *"✗ session did not reach these boot markers within 300s: credential-boundary-403"* ]]
}

@test "a session that never logs anything fails at the boot budget and is stopped" {
    probe FAKE_SESSION=hang BOOT_TIMEOUT_SECONDS=1

    [ "$status" -eq 1 ]
    [[ "$output" == *"(no debug logs found)"* ]]
    [[ "$output" == *"✗ session did not reach these boot markers within 1s: mcp-connect:aws-docs mcp-connect:filesystem mcp-connect:gco mcp-connect:memory bedrock-dispatch:${FAKE_CLAUDE_MODEL} credential-boundary-403"* ]]
}

@test "the install path is verified: pin in --version, binary on PATH, config written and matching" {
    probe FAKE_INSTALL=wrong-version
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ autopilot exec'd claude, but its --version output does not carry the pin ${FAKE_CLAUDE_PIN}: 9.9.9"* ]]

    make_probe_fixture
    probe FAKE_INSTALL=no-binary
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ autopilot reported an install but claude is not on PATH"* ]]

    make_probe_fixture
    probe FAKE_INSTALL=no-config
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ autopilot did not write the session config to ${FAKE_RUNNER_TEMP}/autopilot-claude-code-boot-probe/config/mcp.json"* ]]

    make_probe_fixture
    probe FAKE_INSTALL=other-config
    [ "$status" -ne 0 ]
    [[ "$output" == *"AssertionError: planned ['aws-docs', 'filesystem', 'gco', 'memory'] != written ['aws-docs', 'filesystem', 'gco']"* ]]
}

@test "a generated config that fails the shared contract stops the probe" {
    probe FAKE_CONTRACT_VERIFY=fail

    [ "$status" -eq 1 ]
    [[ "$output" == *"contract: generated config lists an unexpected server"* ]]
    [[ "$output" == *"✗ generated config failed the shared autopilot CI contract"* ]]
    [ ! -s "$PREWARM_LAUNCHES" ]
}

@test "preflight refuses a missing tool and a preinstalled claude" {
    # uvx is faked, and the fixture PATH carries no real one behind it.
    rm -f "$FAKE_BIN/uvx"
    probe
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ required tool missing: uvx"* ]]

    make_probe_fixture
    stub_noop "$FAKE_BIN" claude
    probe
    [ "$status" -eq 1 ]
    [[ "$output" == *"✗ claude is already installed at ${FAKE_BIN}/claude — this probe must exercise autopilot's own install path"* ]]
    [ ! -s "$GCO_CALLS" ]
}

@test "the probe exports fail-closed fabricated credentials to every child" {
    write_stub "$FAKE_BIN/../creds-probe" gco <<'FAKE_GCO'
#!/usr/bin/env bash
printf 'key=%s secret=%s creds=%s config=%s imds=%s profile=%s\n' \
    "$AWS_ACCESS_KEY_ID" "${#AWS_SECRET_ACCESS_KEY}" "$AWS_SHARED_CREDENTIALS_FILE" \
    "$AWS_CONFIG_FILE" "$AWS_EC2_METADATA_DISABLED" "${AWS_PROFILE-<unset>}" > "$CREDS_SEEN"
exit 1
FAKE_GCO
    run env PATH="$BATS_TEST_TMPDIR/creds-probe:$PROBE_PATH" HOME="$FAKE_HOME" RUNNER_TEMP="$FAKE_RUNNER_TEMP" \
        CREDS_SEEN="$BATS_TEST_TMPDIR/creds-seen" AWS_PROFILE=operator AWS_SESSION_TOKEN=real-token \
        bash "$SCRIPT"

    [ "$status" -ne 0 ]
    # Assembled at runtime, as the probe does, so no AKIA-shaped literal
    # sits in the tree for a secret scanner to trip on.
    local fake_key
    fake_key="$(printf 'AKIA%s' '00000000000fake0')"
    [ "$(cat "$BATS_TEST_TMPDIR/creds-seen")" = "key=${fake_key} secret=40 creds=/dev/null config=/dev/null imds=true profile=<unset>" ]
}
