# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for demo/record_demo.sh
# ─────────────────────────────────────────────────────────────────────────────

load 'helpers.sh'

SCRIPT="$REPO_ROOT/demo/record_demo.sh"
LIB="$REPO_ROOT/demo/lib_demo.sh"

@test "record_demo.sh exists, is executable, and passes bash syntax" {
    [ -x "$SCRIPT" ]
    bash -n "$SCRIPT"
}

@test "record_demo.sh passes shellcheck" {
    command -v shellcheck &>/dev/null || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "record_demo.sh uses safe defaults for the refreshed GIF" {
    grep -q 'COLS="${DEMO_COLS:-116}"' "$SCRIPT"
    grep -q 'ROWS="${DEMO_ROWS:-36}"' "$SCRIPT"
    grep -q 'SPEED="${DEMO_SPEED:-3}"' "$SCRIPT"
    grep -q 'THEME="${DEMO_THEME:-monokai}"' "$SCRIPT"
}

@test "record_demo.sh keeps the stable live-demo output names" {
    grep -q 'CAST_FILE=.*live_demo\.cast' "$SCRIPT"
    grep -q 'GIF_FILE=.*live_demo\.gif' "$SCRIPT"
}

@test "record_demo.sh requires guarded live authorization" {
    grep -q 'GCO_RECORDING_LIVE=1' "$SCRIPT"
    grep -q 'GCO_EXPECTED_GIT_SHA' "$SCRIPT"
    grep -q 'GCO_EXPECTED_ACCOUNT_ID' "$SCRIPT"
    grep -q 'verify_legacy_live_recording_authorization' "$SCRIPT"
}

@test "record_demo.sh provides an explicit offline render-existing mode" {
    grep -q 'RENDER_EXISTING="${RENDER_EXISTING:-0}"' "$SCRIPT"
    grep -q 'cp -p "$CAST_FILE" "$RAW_CAST_FILE"' "$SCRIPT"
}

@test "record_demo.sh rejects the sanitization bypass" {
    grep -q 'SKIP_SANITIZE is not allowed for publishable recordings' "$SCRIPT"
}

@test "record_demo.sh stages and transactionally publishes the pair" {
    grep -q 'mktemp -d .*\.live-demo-recording\.XXXXXX' "$SCRIPT"
    grep -q 'trap .*cleanup_recording_temps.*EXIT' "$SCRIPT"
    grep -q 'rollback_recording_publication' "$SCRIPT"
    grep -q "trap 'exit 129' HUP" "$SCRIPT"
    grep -q "trap 'exit 130' INT" "$SCRIPT"
    grep -q "trap 'exit 143' TERM" "$SCRIPT"
    grep -q 'publish_recording_artifacts' "$SCRIPT"

    local credential_cleanup_line rollback_line
    credential_cleanup_line=$(grep -n 'rm -f -- "$RECORDING_KUBECONFIG"' "$SCRIPT" | cut -d: -f1)
    rollback_line=$(grep -n 'if ! rollback_recording_publication' "$SCRIPT" | head -1 | cut -d: -f1)
    [ -n "$credential_cleanup_line" ]
    [ "$credential_cleanup_line" -lt "$rollback_line" ]
}

@test "record_demo.sh uses asciinema --return and configured dimensions" {
    grep -A4 'asciinema rec' "$SCRIPT" | grep -q -- '--return'
    grep -A6 'asciinema rec' "$SCRIPT" | grep -q -- '--cols "$COLS"'
    grep -A6 'asciinema rec' "$SCRIPT" | grep -q -- '--rows "$ROWS"'
}

@test "record_demo.sh binds the demo to the repository CLI and private kubeconfig" {
    grep -q 'gco() { python3 -m cli.main "$@"; }' "$SCRIPT"
    grep -q 'kubectl config view --raw --minify --flatten' "$SCRIPT"
    grep -q 'export KUBECONFIG="$RECORDING_KUBECONFIG"' "$SCRIPT"
    grep -q 'export COLUMNS="$GCO_RECORDING_COLUMNS"' "$SCRIPT"
    grep -q 'source "${REPO_ROOT}/demo/live_demo.sh"' "$SCRIPT"
}

@test "record_demo.sh verifies before render and publishes after render" {
    local sanitize_line verify_line strip_line render_line publish_line
    sanitize_line=$(grep -n 'sanitize_cast "$RAW_CAST_FILE"' "$SCRIPT" | head -1 | cut -d: -f1)
    verify_line=$(grep -n 'verify_cast_sanitized "$RAW_CAST_FILE"' "$SCRIPT" | head -1 | cut -d: -f1)
    strip_line=$(grep -n 'strip_emoji_from_cast "$RAW_CAST_FILE"' "$SCRIPT" | head -1 | cut -d: -f1)
    render_line=$(grep -n 'render_gif "$RAW_CAST_FILE" "$RAW_GIF_FILE"' "$SCRIPT" | head -1 | cut -d: -f1)
    publish_line=$(grep -n 'publish_recording_artifacts' "$SCRIPT" | tail -1 | cut -d: -f1)
    [ "$sanitize_line" -lt "$verify_line" ]
    [ "$verify_line" -lt "$strip_line" ]
    [ "$strip_line" -lt "$render_line" ]
    [ "$render_line" -lt "$publish_line" ]
}

@test "render-existing mode performs no AWS Kubernetes or asciinema call" {
    local fixture="$BATS_TEST_TMPDIR/live render fixture"
    local fake_bin="$fixture/bin"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$fixture/demo/lib_demo.sh"
    printf '{}\n' > "$fixture/cdk.json"
    {
        printf '{"version":2,"width":120,"height":37}\n'
        printf '[0.1,"o","verified existing cast"]\n'
    } > "$fixture/demo/live_demo.cast"
    printf 'old gif\n' > "$fixture/demo/live_demo.gif"

    cat > "$fake_bin/agg" <<'FAKE_AGG'
#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then
    echo "agg test"
    exit 0
fi
output="${!#}"
printf 'new gif\n' > "$output"
FAKE_AGG
    for command in aws kubectl asciinema; do
        cat > "$fake_bin/$command" <<'FORBIDDEN'
#!/usr/bin/env bash
echo "unexpected live command" >&2
exit 97
FORBIDDEN
    done
    chmod +x "$fake_bin"/*
    git -C "$fixture" init -q
    git -C "$fixture" add .
    git -C "$fixture" -c user.name=CI -c user.email=ci@example.invalid \
        commit -q -m render-fixture

    run env PATH="$fake_bin:$PATH" RENDER_EXISTING=1 \
        GCO_RECORDING_REPO_ROOT="$fixture" bash "$SCRIPT"

    [ "$status" -eq 0 ]
    grep -q 'verified existing cast' "$fixture/demo/live_demo.cast"
    [ "$(cat "$fixture/demo/live_demo.gif")" = "new gif" ]
    [ -z "$(compgen -G "$fixture/demo/.live-demo-recording.*" || true)" ]
}


@test "render-existing without agg fails and preserves the live pair" {
    local fixture="$BATS_TEST_TMPDIR/live-no-agg"
    local fake_bin="$fixture/bin"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$fixture/demo/lib_demo.sh"
    printf '{"version":2,"width":116,"height":36}\n' > "$fixture/demo/live_demo.cast"
    printf 'old gif\n' > "$fixture/demo/live_demo.gif"

    run env PATH="$fake_bin:/usr/bin:/bin" RENDER_EXISTING=1 \
        GCO_RECORDING_REPO_ROOT="$fixture" bash "$SCRIPT"

    [ "$status" -ne 0 ]
    grep -q 'width.*116' "$fixture/demo/live_demo.cast"
    [ "$(cat "$fixture/demo/live_demo.gif")" = "old gif" ]
    [ -z "$(compgen -G "$fixture/demo/.live-demo-recording.*" || true)" ]
}

@test "SKIP_SANITIZE fails before touching live artifacts" {
    local fixture="$BATS_TEST_TMPDIR/live-skip-sanitize"
    local fake_bin="$fixture/bin"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$fixture/demo/lib_demo.sh"
    printf '{"version":2,"width":116,"height":36}\n' > "$fixture/demo/live_demo.cast"
    printf 'old gif\n' > "$fixture/demo/live_demo.gif"
    cat > "$fake_bin/agg" <<'FAKE_AGG'
#!/usr/bin/env bash
exit 97
FAKE_AGG
    chmod +x "$fake_bin/agg"

    run env PATH="$fake_bin:$PATH" RENDER_EXISTING=1 SKIP_SANITIZE=1 \
        GCO_RECORDING_REPO_ROOT="$fixture" bash "$SCRIPT"

    [ "$status" -ne 0 ]
    [ "$(cat "$fixture/demo/live_demo.gif")" = "old gif" ]
    [ -z "$(compgen -G "$fixture/demo/.live-demo-recording.*" || true)" ]
}


@test "wrong kubectl context fails before asciinema starts" {
    local fixture="$BATS_TEST_TMPDIR/live-wrong-context"
    local fake_bin="$fixture/bin"
    local asciinema_marker="$BATS_TEST_TMPDIR/unexpected-asciinema"
    mkdir -p "$fixture/demo" "$fake_bin"
    ln -s "$LIB" "$REPO_ROOT/demo/live_demo.sh" "$fixture/demo/"
    cat > "$fixture/cdk.json" <<'JSON'
{"context":{"project_name":"gco","deployment_regions":{"regional":["us-east-1"]}}}
JSON

    cat > "$fake_bin/asciinema" <<'FAKE_ASCIINEMA'
#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then echo "asciinema test"; exit 0; fi
touch "$ASCIINEMA_MARKER"
exit 97
FAKE_ASCIINEMA
    cat > "$fake_bin/agg" <<'FAKE_AGG'
#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then echo "agg test"; exit 0; fi
exit 97
FAKE_AGG
    cat > "$fake_bin/python3" <<'FAKE_PYTHON'
#!/usr/bin/env bash
exit 0
FAKE_PYTHON
    cat > "$fake_bin/aws" <<'FAKE_AWS'
#!/usr/bin/env bash
case "${1:-}" in
    sts) printf '%s\n' '123456789012' ;;
    eks) printf '%s\n' 'https://expected.eks.example' ;;
esac
FAKE_AWS
    cat > "$fake_bin/kubectl" <<'FAKE_KUBECTL'
#!/usr/bin/env bash
if [ "${1:-}" = "config" ]; then
    printf '%s\n' 'https://wrong.eks.example'
    exit 0
fi
exit 97
FAKE_KUBECTL
    chmod +x "$fake_bin"/*

    git -C "$fixture" init -q
    git -C "$fixture" add .
    git -C "$fixture" -c user.name=CI -c user.email=ci@example.invalid \
        commit -q -m recording-fixture
    local expected_sha
    expected_sha=$(git -C "$fixture" rev-parse HEAD)

    run env PATH="$fake_bin:$PATH" \
        ASCIINEMA_MARKER="$asciinema_marker" \
        GCO_RECORDING_LIVE=1 \
        GCO_EXPECTED_GIT_SHA="$expected_sha" \
        GCO_EXPECTED_ACCOUNT_ID=123456789012 \
        GCO_RECORDING_REPO_ROOT="$fixture" bash "$SCRIPT"

    [ "$status" -ne 0 ]
    [[ "$output" == *"kubectl context does not match"* ]]
    [ ! -e "$asciinema_marker" ]
    [ -z "$(compgen -G "$fixture/demo/.live-demo-recording.*" || true)" ]
}

@test "region override remains bound when isolated context drifts before cleanup" {
    local fixture="$BATS_TEST_TMPDIR/live-context-drift"
    local fake_bin="$fixture/bin"
    local asciinema_marker="$BATS_TEST_TMPDIR/asciinema-started"
    local asciinema_kubeconfig_file="$BATS_TEST_TMPDIR/asciinema-kubeconfig"
    local kubeconfig_mode_file="$BATS_TEST_TMPDIR/kubeconfig-mode"
    local aws_calls_file="$BATS_TEST_TMPDIR/aws-calls"
    local kubectl_calls_file="$BATS_TEST_TMPDIR/kubectl-calls"
    local python_calls_file="$BATS_TEST_TMPDIR/python-calls"
    local context_count_file="$BATS_TEST_TMPDIR/context-count"
    local original_kubeconfig="$BATS_TEST_TMPDIR/operator-kubeconfig"
    mkdir -p "$fixture/demo" "$fake_bin"
    printf '%s\n' 'operator-kubeconfig-sentinel' > "$original_kubeconfig"
    ln -s "$LIB" "$REPO_ROOT/demo/live_demo.sh" "$fixture/demo/"
    cat > "$fixture/cdk.json" <<'JSON'
{
  "context": {
    "project_name": "research",
    "deployment_regions": {"regional": ["us-east-1"]},
    "eks_cluster": {"endpoint_access": "PUBLIC"}
  }
}
JSON

    cat > "$fake_bin/asciinema" <<'FAKE_ASCIINEMA'
#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then
    echo "asciinema test"
    exit 0
fi
touch "$ASCIINEMA_MARKER"
printf '%s\n' "$KUBECONFIG" > "$ASCIINEMA_KUBECONFIG_FILE"
mode=$(stat -c '%a' "$KUBECONFIG" 2>/dev/null || stat -f '%Lp' "$KUBECONFIG")
printf '%s\n' "$mode" > "$KUBECONFIG_MODE_FILE"
bash "$GCO_RECORDING_WRAPPER"
FAKE_ASCIINEMA
    cat > "$fake_bin/agg" <<'FAKE_AGG'
#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then
    echo "agg test"
    exit 0
fi
exit 97
FAKE_AGG
    cat > "$fake_bin/python3" <<'FAKE_PYTHON'
#!/usr/bin/env bash
printf '%s|%s\n' "${KUBECONFIG:-<unset>}" "$*" >> "$PYTHON_CALLS_FILE"
if [ "${1:-}" = "-c" ]; then
    exit 0
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "cli.main" ]; then
    case "${3:-}" in
        --version) echo "gco test" ;;
        stacks) echo "research-ap-southeast-1 CREATE_COMPLETE" ;;
    esac
    exit 0
fi
exit 97
FAKE_PYTHON
    cat > "$fake_bin/aws" <<'FAKE_AWS'
#!/usr/bin/env bash
printf '%s|%s\n' "${KUBECONFIG:-<unset>}" "$*" >> "$AWS_CALLS_FILE"
case "${1:-}" in
    sts) printf '%s\n' '123456789012' ;;
    eks) printf '%s\n' 'https://expected.eks.example' ;;
    *) exit 97 ;;
esac
FAKE_AWS
    cat > "$fake_bin/kubectl" <<'FAKE_KUBECTL'
#!/usr/bin/env bash
printf '%s|%s\n' "${KUBECONFIG:-<unset>}" "$*" >> "$KUBECTL_CALLS_FILE"
if [ "$*" = "config view --raw --minify --flatten" ]; then
    cat <<'KUBECONFIG'
apiVersion: v1
clusters:
- cluster:
    server: https://expected.eks.example
  name: authorized
contexts:
- context:
    cluster: authorized
    user: authorized
  name: authorized
current-context: authorized
kind: Config
users:
- name: authorized
  user:
    token: test-token
KUBECONFIG
    exit 0
fi
case "${1:-} ${2:-}" in
    "config view")
        count=0
        if [ -f "$KUBE_CONTEXT_COUNT_FILE" ]; then
            count=$(cat "$KUBE_CONTEXT_COUNT_FILE")
        fi
        count=$((count + 1))
        printf '%s\n' "$count" > "$KUBE_CONTEXT_COUNT_FILE"
        if [ "$count" -lt 3 ]; then
            printf '%s\n' 'https://expected.eks.example'
        else
            printf '%s\n' 'https://changed.eks.example'
        fi
        ;;
    "version --client")
        printf '%s\n' '{"clientVersion":{"gitVersion":"v1.35.0"}}'
        ;;
    "get nodes")
        printf '%s\n' 'NAME STATUS' 'node-1 Ready'
        ;;
    "delete jobs")
        exit 98
        ;;
    *) exit 0 ;;
esac
FAKE_KUBECTL
    for command in clear sleep; do
        cat > "$fake_bin/$command" <<'FAKE_NOOP'
#!/usr/bin/env bash
exit 0
FAKE_NOOP
    done
    chmod +x "$fake_bin"/*

    git -C "$fixture" init -q
    git -C "$fixture" add .
    git -C "$fixture" -c user.name=CI -c user.email=ci@example.invalid \
        commit -q -m recording-fixture
    local expected_sha
    expected_sha=$(git -C "$fixture" rev-parse HEAD)

    run env PATH="$fake_bin:$PATH" \
        TERM=xterm \
        KUBECONFIG="$original_kubeconfig" \
        ASCIINEMA_MARKER="$asciinema_marker" \
        ASCIINEMA_KUBECONFIG_FILE="$asciinema_kubeconfig_file" \
        KUBECONFIG_MODE_FILE="$kubeconfig_mode_file" \
        AWS_CALLS_FILE="$aws_calls_file" \
        KUBECTL_CALLS_FILE="$kubectl_calls_file" \
        PYTHON_CALLS_FILE="$python_calls_file" \
        KUBE_CONTEXT_COUNT_FILE="$context_count_file" \
        GCO_RECORDING_LIVE=1 \
        GCO_EXPECTED_GIT_SHA="$expected_sha" \
        GCO_EXPECTED_ACCOUNT_ID=123456789012 \
        GCO_DEMO_REGION=ap-southeast-1 \
        GCO_RECORDING_REPO_ROOT="$fixture" bash "$SCRIPT"

    [ "$status" -ne 0 ]
    [[ "$output" == *"kubectl context does not match"* ]]
    [ -e "$asciinema_marker" ]
    [ "$(cat "$original_kubeconfig")" = "operator-kubeconfig-sentinel" ]
    local isolated_kubeconfig
    isolated_kubeconfig=$(cat "$asciinema_kubeconfig_file")
    [[ "$isolated_kubeconfig" == "$fixture/demo/.live-demo-recording."*"/kubeconfig" ]]
    [ "$(cat "$kubeconfig_mode_file")" = "600" ]
    [ ! -e "$isolated_kubeconfig" ]
    [ "$(cat "$context_count_file")" = "3" ]
    [ "$(grep -c '|eks describe-cluster --name research-ap-southeast-1 --region ap-southeast-1 ' \
        "$aws_calls_file")" = "3" ]
    [ "$(grep -F -c "${isolated_kubeconfig}|eks describe-cluster --name research-ap-southeast-1" \
        "$aws_calls_file")" = "2" ]
    grep -F -q "${isolated_kubeconfig}|-m cli.main stacks list" "$python_calls_file"
    grep -F -q "${isolated_kubeconfig}|version --client" "$kubectl_calls_file"
    ! grep -q 'research-us-east-1' "$aws_calls_file"
    ! grep -q '|delete jobs --all ' "$kubectl_calls_file"
    [ -z "$(compgen -G "$fixture/demo/.live-demo-recording.*" || true)" ]
}

@test "exports GCO_DEMO_ENABLE so the recorded demo detects the same features" {
    # detect_features reads this variable inside the asciinema child, so the
    # recorder must export it rather than relying on it happening to be set.
    grep -q 'export GCO_DEMO_ENABLE="\${GCO_DEMO_ENABLE:-}"' "$SCRIPT"
    # And it must be exported before the recording starts.
    local export_line recording_line
    export_line=$(grep -n 'export GCO_DEMO_ENABLE=' "$SCRIPT" | head -1 | cut -d: -f1)
    recording_line=$(grep -n 'asciinema rec' "$SCRIPT" | head -1 | cut -d: -f1)
    [ "$export_line" -lt "$recording_line" ]
}

@test "validates GCO_DEMO_ENABLE during preflight" {
    grep -q 'verify_enablement_overrides "\$REPO_ROOT"' "$SCRIPT"
    grep -q 'unknown feature or chart' "$SCRIPT"
}

# ── In-place runs against a fixture checkout ─────────────────────────────────
#
# The tracked recorder runs with GCO_RECORDING_REPO_ROOT pointing at a
# disposable git repository that carries symlinks to the tracked lib_demo.sh
# and live_demo.sh, a cdk.json, and (where a test needs them) a previous
# cast/GIF pair. Every external tool is a PATH shim: `aws` answers the account
# and EKS-endpoint lookups, `kubectl` the context/nodes/kubeconfig calls,
# `asciinema` runs the wrapper it is handed (or refuses), `agg` renders a
# marker GIF, and `python3` stands in for the repository CLI. The shims record
# what reached them, so the tests assert on the recorder's behaviour at each
# boundary rather than on its text.

# live_demo.sh downloads the EFS example's results to the fixed path
# /tmp/gco-demo-results and reads them back; the fake CLI has to write there.
# Never clobber a real download, and leave nothing behind.
DEMO_RESULTS_DIR="/tmp/gco-demo-results"

setup() {
    if [ -e "$DEMO_RESULTS_DIR" ]; then
        echo "refusing to run: $DEMO_RESULTS_DIR already exists (a real demo download?)" >&2
        return 1
    fi
}

teardown() {
    rm -rf "$DEMO_RESULTS_DIR"
}

make_live_fixture() {
    # Creates $FIXTURE, $FAKE_BIN and the recording files; prints nothing.
    FIXTURE="$BATS_TEST_TMPDIR/checkout"
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export ASCIINEMA_MARKER="$BATS_TEST_TMPDIR/asciinema-started"
    export KUBECTL_CALLS="$BATS_TEST_TMPDIR/kubectl-calls"
    export AWS_CALLS="$BATS_TEST_TMPDIR/aws-calls"
    : > "$KUBECTL_CALLS"
    : > "$AWS_CALLS"
    mkdir -p "$FIXTURE/demo" "$FAKE_BIN"
    ln -s "$LIB" "$REPO_ROOT/demo/live_demo.sh" "$FIXTURE/demo/"
    cat > "$FIXTURE/cdk.json" <<'JSON'
{"context":{"project_name":"gco","deployment_regions":{"regional":["us-east-1"]},"eks_cluster":{"endpoint_access":"PUBLIC"}}}
JSON
    printf '{"version":2,"width":116,"height":36}\n[0.1,"o","previous demo"]\n' > "$FIXTURE/demo/live_demo.cast"
    printf 'previous gif\n' > "$FIXTURE/demo/live_demo.gif"
    EXPECTED_SHA="$(init_fixture_repo "$FIXTURE")"

    write_stub "$FAKE_BIN" asciinema <<'FAKE_ASCIINEMA'
#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then echo "asciinema 2.4.0"; exit 0; fi
touch "$ASCIINEMA_MARKER"
output_file=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --cols|--rows|--command) shift 2 ;;
        --return|--overwrite) shift ;;
        *) output_file="$1"; shift ;;
    esac
done
# Run the recorder's wrapper the way asciinema would, then write a cast whose
# output stream carries an account ID and an access-key ID for the sanitizer.
wrapper_status=0
bash --norc --noprofile "$GCO_RECORDING_WRAPPER" >/dev/null 2>&1 || wrapper_status=$?
key_id="AKIA""EXAMPLEEXAMPLE01"
printf '{"version":2,"width":116,"height":36}\n[0.5,"o","account 123456789012 key %s \xe2\x9c\x85 done"]\n' "$key_id" > "$output_file"
exit "$wrapper_status"
FAKE_ASCIINEMA
    write_stub "$FAKE_BIN" agg <<'FAKE_AGG'
#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then echo "agg 1.5.0"; exit 0; fi
printf 'rendered gif\n' > "${!#}"
FAKE_AGG
    # Only the repository CLI (`python3 -m cli.main ...`) is faked. Everything
    # else — the sanitizer, the verifier, the glyph substitution and the
    # override validator, which the recorder runs through `python3 -` and
    # `python3 -c` — is the real interpreter, with PYTHONPATH pointing at this
    # checkout so `gco.enablement_overrides` imports from inside the fixture.
    # The CLI-importability check is `-c` too; it is answered directly, since
    # the fixture carries no cli package.
    REAL_PYTHON3="$(command -v python3)"
    export REAL_PYTHON3
    export PYTHONPATH="$REPO_ROOT"
    write_stub "$FAKE_BIN" python3 <<'FAKE_PYTHON'
#!/usr/bin/env bash
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "cli.main" ]; then
    case "${3:-}" in
        --version) echo "gco 7.6.4" ;;
        stacks) echo "gco-us-east-1 CREATE_COMPLETE" ;;
        inference) echo "endpoint registered" ;;
        files)
            # `gco files download <job> <dest>`: the demo then reads
            # <dest>/results.json, so the fake CLI has to deliver one.
            if [ "${4:-}" = "download" ]; then
                mkdir -p "$6"
                printf '{"status": "ok"}\n' > "$6/results.json"
            fi
            ;;
        *) : ;;
    esac
    exit 0
fi
case "${1:-} ${2:-}" in
    "-c from cli.main"*) exit 0 ;;
esac
exec "$REAL_PYTHON3" "$@"
FAKE_PYTHON
    write_stub "$FAKE_BIN" aws <<'FAKE_AWS'
#!/usr/bin/env bash
printf '%s|%s\n' "${KUBECONFIG:-<unset>}" "$*" >> "$AWS_CALLS"
case "${1:-}" in
    sts) printf '%s\n' '123456789012' ;;
    eks) printf '%s\n' 'https://expected.eks.example' ;;
    *) exit 97 ;;
esac
FAKE_AWS
    write_stub "$FAKE_BIN" kubectl <<'FAKE_KUBECTL'
#!/usr/bin/env bash
printf '%s|%s\n' "${KUBECONFIG:-<unset>}" "$*" >> "$KUBECTL_CALLS"
isolated=0
case "${KUBECONFIG:-}" in */.live-demo-recording.*/kubeconfig) isolated=1 ;; esac
if [ "$*" = "config view --raw --minify --flatten" ]; then
    if [ "${FAKE_KUBECONFIG_SNAPSHOT:-ok}" = "fail" ]; then exit 1; fi
    if [ "${FAKE_KUBECONFIG_SNAPSHOT:-ok}" = "empty" ]; then exit 0; fi
    printf 'apiVersion: v1\ncurrent-context: authorized\n'
    exit 0
fi
case "${1:-} ${2:-}" in
    "config view")
        if [ "$isolated" -eq 1 ] && [ "${FAKE_ISOLATED_CONTEXT:-same}" = "drifts" ]; then
            printf '%s\n' 'https://changed.eks.example'
        else
            printf '%s\n' 'https://expected.eks.example'
        fi
        ;;
    "version --client") printf '%s\n' '{"clientVersion":{"gitVersion":"v1.35.0"}}' ;;
    "get nodes")
        if [ "${FAKE_NODES:-ok}" = "fail" ]; then exit 1; fi
        if [ "$isolated" -eq 1 ] && [ "${FAKE_ISOLATED_NODES:-ok}" = "fail" ]; then exit 1; fi
        printf 'NAME STATUS\nnode-1 Ready\n'
        ;;
    "get pods")
        # The inference endpoint the demo pre-deploys is ready at once; the
        # gco-jobs namespace has no leftover pods to wait for.
        case "$*" in *"-n gco-inference"*) printf 'demo-llm-7c9d 1/1 Running 0 4m\n' ;; esac
        ;;
    *) exit 0 ;;
esac
FAKE_KUBECTL
    stub_noop "$FAKE_BIN" clear sleep
}

run_recorder() {
    # run_recorder [VAR=value ...] — the guarded live invocation against $FIXTURE.
    run env PATH="$FAKE_BIN:$PATH" TERM=xterm \
        GCO_RECORDING_LIVE=1 \
        GCO_EXPECTED_GIT_SHA="$EXPECTED_SHA" \
        GCO_EXPECTED_ACCOUNT_ID=123456789012 \
        GCO_DEMO_NONINTERACTIVE=1 \
        "$@" \
        GCO_RECORDING_REPO_ROOT="$FIXTURE" bash "$SCRIPT"
}

@test "a guarded live recording publishes a sanitized cast and GIF and removes the private kubeconfig" {
    make_live_fixture
    run_recorder

    [ "$status" -eq 0 ]
    [ -e "$ASCIINEMA_MARKER" ]
    [[ "$output" == *"Private kubeconfig snapshot verified; operator kubeconfig remains untouched"* ]]
    [[ "$output" == *"Raw recording complete; sanitizing before publication"* ]]
    [[ "$output" == *"Cast sanitized and independently verified"* ]]
    [[ "$output" == *"Recording pair published: ${FIXTURE}/demo/live_demo.cast"* ]]
    [[ "$output" == *"GIF published: ${FIXTURE}/demo/live_demo.gif"* ]]
    # The published cast is the sanitized recording, with the glyph agg cannot
    # render rewritten; the previous pair is gone.
    grep -q '000000000000' "$FIXTURE/demo/live_demo.cast"
    grep -q 'REDACTED_AWS_ACCESS_KEY_ID' "$FIXTURE/demo/live_demo.cast"
    ! grep -q '123456789012' "$FIXTURE/demo/live_demo.cast"
    grep -q $'\u2713' "$FIXTURE/demo/live_demo.cast"
    [ "$(cat "$FIXTURE/demo/live_demo.gif")" = "rendered gif" ]
    # The recorded demo ran against the isolated kubeconfig, never the operator's.
    grep -q '/.live-demo-recording.*/kubeconfig|get nodes' "$KUBECTL_CALLS"
    ! grep -q '^<unset>|delete jobs' "$KUBECTL_CALLS"
    # Staging, the kubeconfig inside it, and the lock are all gone.
    [ -z "$(compgen -G "$FIXTURE/demo/.live-demo-recording.*" || true)" ]
    [ ! -e "$FIXTURE/.git/gco-legacy-recording.lock" ]
}

@test "SKIP_GIF=1 publishes the cast alone and removes the stale GIF" {
    make_live_fixture
    run_recorder SKIP_GIF=1

    [ "$status" -eq 0 ]
    [[ "$output" == *"Recording pair published"* ]]
    [[ "$output" != *"GIF published"* ]]
    [ ! -e "$FIXTURE/demo/live_demo.gif" ]
    grep -q '000000000000' "$FIXTURE/demo/live_demo.cast"
}

@test "without agg a live recording warns and falls back to a cast-only publication" {
    make_live_fixture
    # The whole demo runs under the recorder, so the toolchain has to stay
    # intact; only agg — faked or really installed — disappears from PATH.
    rm -f "$FAKE_BIN/agg"
    local tools="$BATS_TEST_TMPDIR/tools"
    path_without "$tools" agg
    run env PATH="$FAKE_BIN:$tools" TERM=xterm \
        GCO_RECORDING_LIVE=1 GCO_EXPECTED_GIT_SHA="$EXPECTED_SHA" GCO_EXPECTED_ACCOUNT_ID=123456789012 \
        GCO_DEMO_NONINTERACTIVE=1 GCO_RECORDING_REPO_ROOT="$FIXTURE" bash "$SCRIPT"

    [ "$status" -eq 0 ]
    [[ "$output" == *"agg not installed — will produce .cast only"* ]]
    [[ "$output" == *"1 warnings"* ]]
    [ ! -e "$FIXTURE/demo/live_demo.gif" ]
    grep -q '000000000000' "$FIXTURE/demo/live_demo.cast"
}

@test "preflight lists every missing prerequisite and records nothing" {
    # No asciinema, jq, kubectl or working CLI on PATH, no cdk.json, no
    # authorization, and an override that cannot be validated: every check is
    # reported in one pass, the run exits 1, and neither asciinema nor the lock
    # is ever touched.
    make_live_fixture
    rm -f "$FIXTURE/cdk.json" "$FAKE_BIN/asciinema" "$FAKE_BIN/kubectl" "$FAKE_BIN/python3"
    local tools="$BATS_TEST_TMPDIR/tools"
    link_tools "$tools" bash dirname git df awk head cut tput
    run env PATH="$FAKE_BIN:$tools" GCO_DEMO_ENABLE=valkey GCO_RECORDING_REPO_ROOT="$FIXTURE" bash "$SCRIPT"

    [ "$status" -eq 1 ]
    [[ "$output" == *"asciinema not installed"* ]]
    [[ "$output" == *"cdk.json not found"* ]]
    [[ "$output" == *"Cannot validate GCO_DEMO_ENABLE"* ]]
    [[ "$output" == *"jq not installed"* ]]
    [[ "$output" == *"kubectl not installed"* ]]
    [[ "$output" == *"Repository GCO CLI module is not importable"* ]]
    [[ "$output" == *"Live recording authorization failed"* ]]
    [[ "$output" == *"Fix the issues above before recording."* ]]
    [ ! -e "$ASCIINEMA_MARKER" ]
    [ ! -e "$FIXTURE/.git/gco-legacy-recording.lock" ]
    [ -z "$(compgen -G "$FIXTURE/demo/.live-demo-recording.*" || true)" ]
}

@test "preflight fails when a demo script is missing from the recorded checkout" {
    make_live_fixture
    rm -f "$FIXTURE/demo/live_demo.sh"
    run_recorder

    [ "$status" -eq 1 ]
    [[ "$output" == *"live_demo.sh not found"* ]]
    [[ "$output" == *"lib_demo.sh found"* ]]
    [ ! -e "$ASCIINEMA_MARKER" ]
}

@test "a valid GCO_DEMO_ENABLE is confirmed in preflight and exported to the recording" {
    make_live_fixture
    run_recorder GCO_DEMO_ENABLE=valkey

    [ "$status" -eq 0 ]
    [[ "$output" == *"Run-scoped enablement overrides valid (valkey)"* ]]
}

@test "an unknown GCO_DEMO_ENABLE name is refused before anything is recorded" {
    make_live_fixture
    run_recorder GCO_DEMO_ENABLE=valkeyy

    [ "$status" -eq 1 ]
    [[ "$output" == *"GCO_DEMO_ENABLE names an unknown feature or chart"* ]]
    [ ! -e "$ASCIINEMA_MARKER" ]
}

@test "RENDER_EXISTING must be 0 or 1" {
    make_live_fixture
    run_recorder RENDER_EXISTING=2

    [ "$status" -eq 1 ]
    [[ "$output" == *"RENDER_EXISTING must be 0 or 1"* ]]
}

@test "render-existing refuses when there is no cast to re-render" {
    make_live_fixture
    rm -f "$FIXTURE/demo/live_demo.cast"
    run env PATH="$FAKE_BIN:$PATH" RENDER_EXISTING=1 GCO_RECORDING_REPO_ROOT="$FIXTURE" bash "$SCRIPT"

    [ "$status" -eq 1 ]
    [[ "$output" == *"Existing live-demo cast not found"* ]]
    [ "$(cat "$FIXTURE/demo/live_demo.gif")" = "previous gif" ]
}

@test "low disk space is a warning, not a refusal" {
    make_live_fixture
    write_stub "$FAKE_BIN" df <<'FAKE_DF'
#!/usr/bin/env bash
printf 'Filesystem 1M-blocks Used Available Use%% Mounted on\n'
printf 'fake 1000 950 42 95%% /\n'
FAKE_DF
    run env PATH="$FAKE_BIN:$PATH" RENDER_EXISTING=1 GCO_RECORDING_REPO_ROOT="$FIXTURE" bash "$SCRIPT"

    [ "$status" -eq 0 ]
    [[ "$output" == *"Low disk space: 42 MB"* ]]
    [ "$(cat "$FIXTURE/demo/live_demo.gif")" = "rendered gif" ]
}

@test "an unreachable cluster fails preflight after the context matched" {
    make_live_fixture
    run_recorder FAKE_NODES=fail

    [ "$status" -eq 1 ]
    [[ "$output" == *"kubectl context matches the authorized GCO EKS cluster"* ]]
    [[ "$output" == *"kubectl cannot reach the cluster"* ]]
    [ ! -e "$ASCIINEMA_MARKER" ]
}

@test "a failed kubeconfig snapshot stops the recording before asciinema starts" {
    make_live_fixture
    run_recorder FAKE_KUBECONFIG_SNAPSHOT=fail

    [ "$status" -eq 1 ]
    [[ "$output" == *"Unable to snapshot the authorized kubectl context for recording."* ]]
    [ ! -e "$ASCIINEMA_MARKER" ]
    [ -z "$(compgen -G "$FIXTURE/demo/.live-demo-recording.*" || true)" ]
}

@test "an empty kubeconfig snapshot is refused" {
    make_live_fixture
    run_recorder FAKE_KUBECONFIG_SNAPSHOT=empty

    [ "$status" -eq 1 ]
    [[ "$output" == *"The authorized kubectl context snapshot is empty."* ]]
    [ ! -e "$ASCIINEMA_MARKER" ]
}

@test "the isolated kubeconfig must still point at the authorized cluster" {
    make_live_fixture
    run_recorder FAKE_ISOLATED_CONTEXT=drifts

    [ "$status" -eq 1 ]
    [[ "$output" == *"The isolated kubeconfig does not match the authorized cluster."* ]]
    [ ! -e "$ASCIINEMA_MARKER" ]
    # The operator's context passed; only the isolated copy was re-checked.
    grep -q '^<unset>|config view --minify' "$KUBECTL_CALLS"
    grep -q '/kubeconfig|config view --minify' "$KUBECTL_CALLS"
}

@test "the isolated kubeconfig must reach the cluster" {
    make_live_fixture
    run_recorder FAKE_ISOLATED_NODES=fail

    [ "$status" -eq 1 ]
    [[ "$output" == *"The isolated kubeconfig cannot reach the authorized cluster."* ]]
    [ ! -e "$ASCIINEMA_MARKER" ]
}

@test "a kubeconfig the cleanup cannot remove turns a successful recording into a failure" {
    # The snapshot carries credentials; if it cannot be deleted the operator
    # must hear about it. The staging directory that holds it cannot be
    # removed either (that is what an immovable kubeconfig looks like in
    # practice), and the run must still end with a failing status rather than
    # the success the publication itself earned.
    make_live_fixture
    write_stub "$FAKE_BIN" rm <<'FAKE_RM'
#!/usr/bin/env bash
for arg in "$@"; do
    case "$arg" in
        */.live-demo-recording.*/kubeconfig) echo "rm: cannot remove '$arg': Operation not permitted" >&2; exit 1 ;;
        */.live-demo-recording.*/*) ;;
        */.live-demo-recording.*) echo "rm: cannot remove '$arg': Directory not empty" >&2; exit 1 ;;
    esac
done
exec /bin/rm "$@"
FAKE_RM
    run_recorder

    [ "$status" -eq 1 ]
    [[ "$output" == *"Recording pair published"* ]]
    [[ "$output" == *"Unable to remove the staged credential-bearing kubeconfig."* ]]
    [ -e "$(compgen -G "$FIXTURE/demo/.live-demo-recording.*")/kubeconfig" ]
    [ ! -e "$FIXTURE/.git/gco-legacy-recording.lock" ]
}

@test "when publication fails and rollback cannot restore the pair, staging is preserved and the lock is still released" {
    make_live_fixture
    # The GIF cannot be moved into place, and the restore copy of the previous
    # cast fails too: rollback cannot complete, so the recorder must say where
    # the staged artifacts are instead of deleting them.
    write_stub "$FAKE_BIN" mv <<'FAKE_MV'
#!/usr/bin/env bash
case "${!#}" in
    */demo/live_demo.gif) echo "mv: cannot move to '${!#}': Input/output error" >&2; exit 1 ;;
esac
exec /bin/mv "$@"
FAKE_MV
    write_stub "$FAKE_BIN" cp <<'FAKE_CP'
#!/usr/bin/env bash
case "${!#}" in
    */.restore-cast) echo "cp: cannot create '${!#}': Input/output error" >&2; exit 1 ;;
esac
exec /bin/cp "$@"
FAKE_CP
    run_recorder

    [ "$status" -eq 1 ]
    [[ "$output" == *"Recording publication failed and rollback could not complete."* ]]
    [[ "$output" == *"Recording publication rollback failed; preserving staging at ${FIXTURE}/demo/.live-demo-recording."* ]]
    [ -n "$(compgen -G "$FIXTURE/demo/.live-demo-recording.*" || true)" ]
    [ ! -e "$FIXTURE/.git/gco-legacy-recording.lock" ]
}

@test "a recording lock that cannot be released is reported" {
    make_live_fixture
    write_stub "$FAKE_BIN" rm <<'FAKE_RM'
#!/usr/bin/env bash
for arg in "$@"; do
    case "$arg" in
        */gco-legacy-recording.lock) echo "rm: cannot remove '$arg': Operation not permitted" >&2; exit 1 ;;
    esac
done
exec /bin/rm "$@"
FAKE_RM
    run_recorder

    [ "$status" -eq 1 ]
    [[ "$output" == *"Recording pair published"* ]]
    [[ "$output" == *"Unable to release legacy recording lock"* ]]
}
