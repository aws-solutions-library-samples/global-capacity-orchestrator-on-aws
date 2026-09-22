#!/usr/bin/env bash
# =============================================================================
# autopilot_probe_fixture.sh — the faked world the three autopilot boot probes
# run against in test_autopilot_claude_code_boot_probe.bats,
# test_autopilot_codex_boot_probe.bats and
# test_autopilot_opencode_boot_probe.bats
# =============================================================================
#
# The probes drive the real `gco autopilot`, install the real engine binary
# from npm and boot a real session against Amazon Bedrock — none of which a
# unit suite can do. What the suites prove is the probe's own logic: the
# phases run in order, every assertion fires on the evidence it is meant to
# fire on, and every failure branch names what went wrong. So every external
# the probe touches is a fake on PATH, scripted through environment
# variables:
#
#   gco       renders the session plan, "installs" the engine binary into
#             $FAKE_BIN, writes the session config, and plays the session
#             (FAKE_PRINT_CONFIG, FAKE_INSTALL, FAKE_MCP_LIST, FAKE_SESSION
#             below)
#   python3   answers the shared CI contract (.github/scripts/
#             autopilot_ci_contract.py) from fixture facts and hands every
#             other invocation — the probe's own inline scripts — to the
#             real interpreter. The contract imports the project's Python,
#             which the interpreter the bats job has cannot parse, so the
#             contract's *facts* are fixtures here; the contract itself is
#             tested in tests/test_autopilot_ci_contract.py.
#   timeout   never runs a pre-warm launch recipe (that would resolve real
#             packages); it records the recipe and answers FAKE_PREWARM.
#             The Codex and OpenCode foreground runs it runs for real (or
#             times out on request).
#   npm, uvx, sleep   present on PATH, do nothing.
#
# Loaded with `load 'autopilot_probe_fixture.sh'` after helpers.sh.
# =============================================================================

# Fixture facts. They need not match the real pins, only be consistent
# between the fake contract and the fake gco.
FAKE_CLAUDE_PIN="2.1.252"
FAKE_CODEX_PIN="0.152.0"
FAKE_OPENCODE_PIN="1.19.4"
FAKE_CLAUDE_MODEL="global.anthropic.claude-opus-5"
FAKE_CODEX_MODEL="global.openai.gpt-5.6-sol"
FAKE_OPENCODE_MODEL="global.moonshotai.kimi-k9"
FAKE_SERVERS="aws-docs filesystem gco memory"
export FAKE_CLAUDE_PIN FAKE_CODEX_PIN FAKE_OPENCODE_PIN
export FAKE_CLAUDE_MODEL FAKE_CODEX_MODEL FAKE_OPENCODE_MODEL FAKE_SERVERS

# make_probe_fixture — creates $FAKE_BIN, $FAKE_HOME, $FAKE_RUNNER_TEMP and
# $PROBE_PATH: the fakes first, then everything installed except the engine
# binaries (the probes refuse to run when claude, codex or opencode is
# preinstalled, and the machine running the suite may well have them).
make_probe_fixture() {
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    FAKE_HOME="$BATS_TEST_TMPDIR/home"
    FAKE_RUNNER_TEMP="$BATS_TEST_TMPDIR/runner-temp"
    export GCO_CALLS="$BATS_TEST_TMPDIR/gco-calls"
    export PREWARM_LAUNCHES="$BATS_TEST_TMPDIR/prewarm-launches"
    : > "$GCO_CALLS"
    : > "$PREWARM_LAUNCHES"
    rm -rf "$FAKE_BIN" "$FAKE_HOME" "$FAKE_RUNNER_TEMP"
    mkdir -p "$FAKE_BIN" "$FAKE_HOME" "$FAKE_RUNNER_TEMP"
    if [ ! -d "$BATS_TEST_TMPDIR/tools" ]; then
        path_without "$BATS_TEST_TMPDIR/tools" claude codex opencode gco npm uvx timeout sleep
    fi
    PROBE_PATH="$FAKE_BIN:$BATS_TEST_TMPDIR/tools"
    REAL_PYTHON3="$(command -v python3)"
    export REAL_PYTHON3 FAKE_BIN

    write_stub "$FAKE_BIN" python3 <<'FAKE_PYTHON'
#!/usr/bin/env bash
case "${1:-}" in
    *autopilot_ci_contract.py)
        engine=claude
        case " $* " in
            *" --engine codex "*) engine=codex ;;
            *" --engine opencode "*) engine=opencode ;;
        esac
        case "${2:-}" in
            pin)
                case "$engine" in
                    codex) echo "$FAKE_CODEX_PIN" ;;
                    opencode) echo "$FAKE_OPENCODE_PIN" ;;
                    *) echo "$FAKE_CLAUDE_PIN" ;;
                esac ;;
            default-model)
                case "$engine" in
                    codex) echo "$FAKE_CODEX_MODEL" ;;
                    opencode) echo "$FAKE_OPENCODE_MODEL" ;;
                    *) echo "$FAKE_CLAUDE_MODEL" ;;
                esac ;;
            expected-servers)
                # shellcheck disable=SC2086
                printf '%s\n' $FAKE_SERVERS ;;
            verify-config|verify-codex-config|verify-opencode-config)
                if [ "${FAKE_CONTRACT_VERIFY:-ok}" = "fail" ]; then
                    echo "contract: generated config lists an unexpected server" >&2
                    exit 1
                fi
                ;;
        esac
        exit 0
        ;;
esac
exec "$REAL_PYTHON3" "$@"
FAKE_PYTHON

    # FAKE_INSTALL:   full | wrong-version | no-binary | no-config | other-config
    # FAKE_MCP_LIST:  complete | missing | unhealthy | exit-nonzero
    # FAKE_SESSION:   complete | delayed | partial | hang | exit-zero |
    #                 credential-chain-bypass (OpenCode only)
    write_stub "$FAKE_BIN" gco <<'FAKE_GCO'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GCO_CALLS"
engine=claude
case " $* " in
    *" --engine codex "*) engine=codex ;;
    *" --engine opencode "*) engine=opencode ;;
esac
case "$engine" in
    codex) pin="$FAKE_CODEX_PIN"; model="$FAKE_CODEX_MODEL"; binary=codex ;;
    opencode) pin="$FAKE_OPENCODE_PIN"; model="$FAKE_OPENCODE_MODEL"; binary=opencode ;;
    *) pin="$FAKE_CLAUDE_PIN"; model="$FAKE_CLAUDE_MODEL"; binary=claude ;;
esac

servers="$FAKE_SERVERS"
[ "${1:-}" = "autopilot" ] || exit 0

# render_config <servers...> — the session plan for the selected engine.
render_config() {
    local name first
    case "$engine" in
        codex)
            printf 'model = "%s"\nmodel_provider = "bedrock"\n' "$model"
            for name in "$@"; do
                printf '\n[mcp_servers.%s]\n' "$name"
                server_entry_toml "$name"
            done
            ;;
        opencode)
            printf '{\n  "model": "amazon-bedrock/%s",\n  "small_model": "amazon-bedrock/%s",\n  "mcp": {\n' "$model" "$model"
            first=1
            for name in "$@"; do
                [ "$first" -eq 1 ] || printf ',\n'
                first=0
                printf '    "%s": ' "$name"
                server_entry_opencode "$name"
            done
            printf '\n  }\n}\n'
            ;;
        *)
            printf '{\n  "mcpServers": {\n'
            first=1
            for name in "$@"; do
                [ "$first" -eq 1 ] || printf ',\n'
                first=0
                printf '    "%s": ' "$name"
                server_entry_json "$name"
            done
            printf '\n  }\n}\n'
            ;;
    esac
}
server_entry_json() {
    case "$1" in
        gco) printf '{"command": "python3", "args": ["%s/gco_mcp/run_mcp.py"]}' "$PWD" ;;
        aws-docs) printf '{"command": "uvx", "args": ["awslabs.aws-documentation-mcp-server@latest"], "env": {"FASTMCP_LOG_LEVEL": "ERROR"}}' ;;
        filesystem) printf '{"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "%s"]}' "$PWD" ;;
        *) printf '{"command": "npx", "args": ["-y", "@modelcontextprotocol/server-%s"]}' "$1" ;;
    esac
}
server_entry_toml() {
    case "$1" in
        gco) printf 'command = "python3"\nargs = ["%s/gco_mcp/run_mcp.py"]\n' "$PWD" ;;
        aws-docs) printf 'command = "uvx"\nargs = ["awslabs.aws-documentation-mcp-server@latest"]\nenv = { FASTMCP_LOG_LEVEL = "ERROR" }\n' ;;
        filesystem) printf 'command = "npx"\nargs = ["-y", "@modelcontextprotocol/server-filesystem", "%s"]\n' "$PWD" ;;
        *) printf 'command = "npx"\nargs = ["-y", "@modelcontextprotocol/server-%s"]\n' "$1" ;;
    esac
}
# OpenCode's local MCP entry: one command list (no args split), environment
# instead of env, plus the enabled/timeout policy autopilot pins.
server_entry_opencode() {
    case "$1" in
        gco) printf '{"type": "local", "command": ["python3", "%s/gco_mcp/run_mcp.py"], "enabled": true, "timeout": 60000}' "$PWD" ;;
        aws-docs) printf '{"type": "local", "command": ["uvx", "awslabs.aws-documentation-mcp-server@latest"], "environment": {"FASTMCP_LOG_LEVEL": "ERROR"}, "enabled": true, "timeout": 60000}' ;;
        filesystem) printf '{"type": "local", "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "%s"], "enabled": true, "timeout": 60000}' "$PWD" ;;
        *) printf '{"type": "local", "command": ["npx", "-y", "@modelcontextprotocol/server-%s"], "enabled": true, "timeout": 60000}' "$1" ;;
    esac
}
case "$binary" in
    codex) version_line="codex-cli $pin" ;;
    opencode) version_line="$pin" ;;
    *) version_line="$pin (Claude Code)" ;;
esac

case " $* " in
    *" --print-config"*)
        # shellcheck disable=SC2086
        render_config $servers
        ;;
    *" -y -- --version"*)
        # Autopilot's own install path: put the binary on PATH, write the
        # session config, exec `<binary> --version`.
        if [ "${FAKE_INSTALL:-full}" != "no-binary" ]; then
            printf '#!/usr/bin/env bash\n[ "${1:-}" = --version ] && echo "%s"\nexit 0\n' "$version_line" > "$FAKE_BIN/$binary"
            chmod +x "$FAKE_BIN/$binary"
        fi
        if [ "${FAKE_INSTALL:-full}" != "no-config" ]; then
            case "$engine" in
                codex)
                    mkdir -p "$GCO_AUTOPILOT_CONFIG_DIR/codex"
                    written="$GCO_AUTOPILOT_CONFIG_DIR/codex/config.toml" ;;
                opencode)
                    mkdir -p "$GCO_AUTOPILOT_CONFIG_DIR/opencode"
                    written="$GCO_AUTOPILOT_CONFIG_DIR/opencode/opencode.json" ;;
                *)
                    mkdir -p "$GCO_AUTOPILOT_CONFIG_DIR"
                    written="$GCO_AUTOPILOT_CONFIG_DIR/mcp.json" ;;
            esac
            if [ "${FAKE_INSTALL:-full}" = "other-config" ]; then
                # shellcheck disable=SC2086
                render_config ${servers/ memory/} > "$written"
            else
                # shellcheck disable=SC2086
                render_config $servers > "$written"
            fi
        fi
        if [ "${FAKE_INSTALL:-full}" = "wrong-version" ]; then
            echo "9.9.9"
        else
            echo "$version_line"
        fi
        ;;
    *" -- --debug -p "*)
        # The Claude session: claude's own debug log is the evidence.
        debug_dir="$HOME/.claude/debug"
        mkdir -p "$debug_dir"
        log="$debug_dir/session-$$.txt"
        case "${FAKE_SESSION:-complete}" in
            hang) exec /bin/sleep 60 ;;
            delayed) /bin/sleep 1 ;;
        esac
        # shellcheck disable=SC2086
        for name in $servers; do
            echo "[DEBUG] MCP server \"${name}\": Successfully connected (transport: stdio) in 12ms" >> "$log"
        done
        echo "[DEBUG] dispatching to bedrock model=${model}" >> "$log"
        if [ "${FAKE_SESSION:-complete}" != "partial" ]; then
            echo "[DEBUG] API error (attempt 1/10): 403 The security token included in the request is invalid" >> "$log"
        fi
        exit 0
        ;;
    *" -- exec "*)
        # The Codex session: RUST_LOG=info tracing on stderr, then codex's own
        # bounded-retry exit.
        # shellcheck disable=SC2086
        list="$(printf '%s, ' $servers)"
        echo "INFO codex_core: session configured mcp_server_count=4 mcp_servers=\"${list%, }\"" >&2
        # A partial session only reports its plan: no handshake, no dispatch,
        # no endpoint, no credential boundary.
        if [ "${FAKE_SESSION:-complete}" != "partial" ]; then
            echo "INFO rmcp: Service initialized as client peer_info=Implementation { name: \"gco\", version: \"1\" }" >&2
            echo "INFO codex_core: turn model=${model} provider=bedrock" >&2
            echo "INFO reqwest: POST https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/responses" >&2
            echo "ERROR codex_core: Turn error: unexpected status 401 Unauthorized: The security token included in the request is invalid" >&2
        fi
        if [ "${FAKE_SESSION:-complete}" = "exit-zero" ]; then exit 0; fi
        exit 1
        ;;
    *" -- mcp list"*)
        # OpenCode's utility subcommand, in its real shape: a clack-style
        # frame, one `<icon> <name> <status>` line per configured server with
        # the status dimmed by an ANSI colour code (even off-terminal), the
        # launch recipe beneath it, and a `<N> server(s)` trailer.
        case "${FAKE_MCP_LIST:-complete}" in
            exit-nonzero)
                echo "Error: failed to read ${GCO_AUTOPILOT_CONFIG_DIR}/opencode/opencode.json" >&2
                exit 1 ;;
        esac
        dim=$'\033[90m'
        echo "┌  MCP Servers"
        echo $'\033[0m'
        echo "│"
        count=0
        # shellcheck disable=SC2086
        for name in $servers; do
            case "${FAKE_MCP_LIST:-complete}:$name" in
                missing:memory) continue ;;
                unhealthy:memory)
                    echo "◇  ✗ ${name} ${dim}failed"
                    echo "│    MCP connection closed"
                    echo "│      ${dim}npx -y @modelcontextprotocol/server-${name}"
                    echo "│"
                    count=$((count + 1))
                    continue ;;
            esac
            echo "◇  ✓ ${name} ${dim}connected"
            echo "│      ${dim}npx -y @modelcontextprotocol/server-${name}"
            echo "│"
            count=$((count + 1))
        done
        echo "└  ${count} server(s)"
        exit 0
        ;;
    *" -- run "*)
        # The OpenCode session: --print-logs structured lines on stderr, then
        # opencode's own failed-turn exit.
        written="$GCO_AUTOPILOT_CONFIG_DIR/opencode/opencode.json"
        oclog() { echo "timestamp=2026-01-01T00:00:00.000Z level=$1 run=fake $2" >&2; }
        case "${FAKE_SESSION:-complete}" in
            hang) exec /bin/sleep 60 ;;
            delayed) /bin/sleep 1 ;;
        esac
        oclog INFO "message=loading path=${HOME}/.config/opencode/opencode.json"
        stream="message=stream providerID=amazon-bedrock modelID=${model} session.id=ses_fake"
        case "${FAKE_SESSION:-complete}" in
            credential-chain-bypass)
                # The signature of a generated config that pinned an AWS
                # profile over the exported static keys: the SDK skips them
                # and finds nothing else.
                oclog INFO "message=loading path=${written}"
                oclog INFO "${stream} small=true agent=title mode=primary"
                oclog ERROR "message=\"stream error\" providerID=amazon-bedrock modelID=${model} error.error=\"AI_APICallError: Could not load credentials from any providers\""
                echo "Error: Could not load credentials from any providers" >&2
                exit 1 ;;
            partial)
                # Only the global config load: the generated config was never
                # read, so no dispatch, no title stream, no credential
                # boundary either.
                ;;
            *)
                oclog INFO "message=loading path=${written}"
                oclog INFO "${stream} small=true agent=title mode=primary"
                oclog ERROR "message=\"stream error\" providerID=amazon-bedrock modelID=${model} small=true agent=title error.error=\"AI_APICallError: undefined: The security token included in the request is invalid.\""
                oclog INFO "${stream} small=false agent=build mode=primary"
                oclog ERROR "message=\"stream error\" providerID=amazon-bedrock modelID=${model} small=false agent=build error.error=\"AI_APICallError: undefined: The security token included in the request is invalid.\""
                echo "Error: undefined: The security token included in the request is invalid." >&2 ;;
        esac
        if [ "${FAKE_SESSION:-complete}" = "exit-zero" ]; then exit 0; fi
        exit 1
        ;;
esac
exit 0
FAKE_GCO

    # FAKE_PREWARM: ';'-separated "<launch substring>=<exit code>" table for
    # the pre-warm recipes (default 0). FAKE_SESSION_TIMEOUT=1 makes the
    # foreground Codex/OpenCode session time out instead of running;
    # FAKE_MCP_LIST_TIMEOUT=1 does the same for OpenCode's `mcp list`.
    write_stub "$FAKE_BIN" timeout <<'FAKE_TIMEOUT'
#!/usr/bin/env bash
if [ "${2:-}" = "bash" ] && [ "${3:-}" = "-c" ]; then
    printf '%s\n' "$4" >> "$PREWARM_LAUNCHES"
    echo "fake launch: $4"
    table="${FAKE_PREWARM:-}"
    while [ -n "$table" ]; do
        entry="${table%%;*}"
        case "$4" in *"${entry%%=*}"*) exit "${entry#*=}" ;; esac
        [ "$table" = "$entry" ] && break
        table="${table#*;}"
    done
    exit 0
fi
case " $* " in
    *" -- mcp list "*) timed_out="${FAKE_MCP_LIST_TIMEOUT:-0}" ;;
    *) timed_out="${FAKE_SESSION_TIMEOUT:-0}" ;;
esac
if [ "$timed_out" = "1" ]; then
    echo "fake timeout: killed after $1s" >&2
    exit 124
fi
shift
exec "$@"
FAKE_TIMEOUT
    stub_noop "$FAKE_BIN" npm uvx sleep
}

# run_probe <script> [VAR=value ...] — the probe with the faked PATH, a
# throwaway HOME and RUNNER_TEMP, and the real python3 recorded for the fake.
run_probe() {
    local script="$1"
    shift
    run env PATH="$PROBE_PATH" HOME="$FAKE_HOME" RUNNER_TEMP="$FAKE_RUNNER_TEMP" "$@" bash "$script"
}
