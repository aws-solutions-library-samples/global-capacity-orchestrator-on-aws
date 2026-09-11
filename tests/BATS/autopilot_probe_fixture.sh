#!/usr/bin/env bash
# =============================================================================
# autopilot_probe_fixture.sh — the faked world the two autopilot boot probes
# run against in test_autopilot_claude_code_boot_probe.bats and
# test_autopilot_codex_boot_probe.bats
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
#             (FAKE_PRINT_CONFIG, FAKE_INSTALL, FAKE_SESSION below)
#   python3   answers the shared CI contract (.github/scripts/
#             autopilot_ci_contract.py) from fixture facts and hands every
#             other invocation — the probe's own inline scripts — to the
#             real interpreter. The contract imports the project's Python,
#             which the interpreter the bats job has cannot parse, so the
#             contract's *facts* are fixtures here; the contract itself is
#             tested in tests/test_autopilot_ci_contract.py.
#   timeout   never runs a pre-warm launch recipe (that would resolve real
#             packages); it records the recipe and answers FAKE_PREWARM.
#             The Codex session it runs for real (or times out on request).
#   npm, uvx, sleep   present on PATH, do nothing.
#
# Loaded with `load 'autopilot_probe_fixture.sh'` after helpers.sh.
# =============================================================================

# Fixture facts. They need not match the real pins, only be consistent
# between the fake contract and the fake gco.
FAKE_CLAUDE_PIN="2.1.252"
FAKE_CODEX_PIN="0.152.0"
FAKE_CLAUDE_MODEL="global.anthropic.claude-opus-5"
FAKE_CODEX_MODEL="global.openai.gpt-5.6-sol"
FAKE_SERVERS="aws-docs filesystem gco memory"
export FAKE_CLAUDE_PIN FAKE_CODEX_PIN FAKE_CLAUDE_MODEL FAKE_CODEX_MODEL FAKE_SERVERS

# make_probe_fixture — creates $FAKE_BIN, $FAKE_HOME, $FAKE_RUNNER_TEMP and
# $PROBE_PATH: the fakes first, then everything installed except the engine
# binaries (the probes refuse to run when claude or codex is preinstalled,
# and the machine running the suite may well have them).
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
        path_without "$BATS_TEST_TMPDIR/tools" claude codex gco npm uvx timeout sleep
    fi
    PROBE_PATH="$FAKE_BIN:$BATS_TEST_TMPDIR/tools"
    REAL_PYTHON3="$(command -v python3)"
    export REAL_PYTHON3 FAKE_BIN

    write_stub "$FAKE_BIN" python3 <<'FAKE_PYTHON'
#!/usr/bin/env bash
case "${1:-}" in
    *autopilot_ci_contract.py)
        engine=claude
        case " $* " in *" --engine codex "*) engine=codex ;; esac
        case "${2:-}" in
            pin)
                if [ "$engine" = codex ]; then echo "$FAKE_CODEX_PIN"; else echo "$FAKE_CLAUDE_PIN"; fi ;;
            default-model)
                if [ "$engine" = codex ]; then echo "$FAKE_CODEX_MODEL"; else echo "$FAKE_CLAUDE_MODEL"; fi ;;
            expected-servers)
                # shellcheck disable=SC2086
                printf '%s\n' $FAKE_SERVERS ;;
            verify-config|verify-codex-config)
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

    # FAKE_INSTALL:  full | wrong-version | no-binary | no-config | other-config
    # FAKE_SESSION:  complete | delayed | partial | hang | exit-zero
    write_stub "$FAKE_BIN" gco <<'FAKE_GCO'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GCO_CALLS"
engine=claude
case " $* " in *" --engine codex "*) engine=codex ;; esac
if [ "$engine" = codex ]; then pin="$FAKE_CODEX_PIN"; model="$FAKE_CODEX_MODEL"; binary=codex
else pin="$FAKE_CLAUDE_PIN"; model="$FAKE_CLAUDE_MODEL"; binary=claude; fi

servers="$FAKE_SERVERS"
[ "${1:-}" = "autopilot" ] || exit 0

# render_config <servers...> — the session plan for the selected engine.
render_config() {
    local name
    if [ "$engine" = codex ]; then
        printf 'model = "%s"\nmodel_provider = "bedrock"\n' "$model"
        for name in "$@"; do
            printf '\n[mcp_servers.%s]\n' "$name"
            server_entry_toml "$name"
        done
    else
        printf '{\n  "mcpServers": {\n'
        local first=1
        for name in "$@"; do
            [ "$first" -eq 1 ] || printf ',\n'
            first=0
            printf '    "%s": ' "$name"
            server_entry_json "$name"
        done
        printf '\n  }\n}\n'
    fi
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
if [ "$binary" = codex ]; then version_line="codex-cli $pin"; else version_line="$pin (Claude Code)"; fi

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
            if [ "$engine" = codex ]; then
                mkdir -p "$GCO_AUTOPILOT_CONFIG_DIR/codex"
                written="$GCO_AUTOPILOT_CONFIG_DIR/codex/config.toml"
            else
                mkdir -p "$GCO_AUTOPILOT_CONFIG_DIR"
                written="$GCO_AUTOPILOT_CONFIG_DIR/mcp.json"
            fi
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
esac
exit 0
FAKE_GCO

    # FAKE_PREWARM: ';'-separated "<launch substring>=<exit code>" table for
    # the pre-warm recipes (default 0). FAKE_SESSION_TIMEOUT=1 makes the
    # foreground Codex session time out instead of running.
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
if [ "${FAKE_SESSION_TIMEOUT:-0}" = "1" ]; then
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
