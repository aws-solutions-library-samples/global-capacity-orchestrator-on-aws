# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for demo/record_demo.sh
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT="demo/record_demo.sh"

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
    grep -q 'source demo/live_demo.sh' "$SCRIPT"
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
    cp "$SCRIPT" "$fixture/demo/record_demo.sh"
    cp demo/lib_demo.sh "$fixture/demo/lib_demo.sh"
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
        bash "$fixture/demo/record_demo.sh"

    [ "$status" -eq 0 ]
    grep -q 'verified existing cast' "$fixture/demo/live_demo.cast"
    [ "$(cat "$fixture/demo/live_demo.gif")" = "new gif" ]
    [ -z "$(compgen -G "$fixture/demo/.live-demo-recording.*" || true)" ]
}


@test "render-existing without agg fails and preserves the live pair" {
    local fixture="$BATS_TEST_TMPDIR/live-no-agg"
    local fake_bin="$fixture/bin"
    mkdir -p "$fixture/demo" "$fake_bin"
    cp "$SCRIPT" "$fixture/demo/record_demo.sh"
    cp demo/lib_demo.sh "$fixture/demo/lib_demo.sh"
    printf '{"version":2,"width":116,"height":36}\n' > "$fixture/demo/live_demo.cast"
    printf 'old gif\n' > "$fixture/demo/live_demo.gif"

    run env PATH="$fake_bin:/usr/bin:/bin" RENDER_EXISTING=1 \
        bash "$fixture/demo/record_demo.sh"

    [ "$status" -ne 0 ]
    grep -q 'width.*116' "$fixture/demo/live_demo.cast"
    [ "$(cat "$fixture/demo/live_demo.gif")" = "old gif" ]
    [ -z "$(compgen -G "$fixture/demo/.live-demo-recording.*" || true)" ]
}

@test "SKIP_SANITIZE fails before touching live artifacts" {
    local fixture="$BATS_TEST_TMPDIR/live-skip-sanitize"
    local fake_bin="$fixture/bin"
    mkdir -p "$fixture/demo" "$fake_bin"
    cp "$SCRIPT" "$fixture/demo/record_demo.sh"
    cp demo/lib_demo.sh "$fixture/demo/lib_demo.sh"
    printf '{"version":2,"width":116,"height":36}\n' > "$fixture/demo/live_demo.cast"
    printf 'old gif\n' > "$fixture/demo/live_demo.gif"
    cat > "$fake_bin/agg" <<'FAKE_AGG'
#!/usr/bin/env bash
exit 97
FAKE_AGG
    chmod +x "$fake_bin/agg"

    run env PATH="$fake_bin:$PATH" RENDER_EXISTING=1 SKIP_SANITIZE=1 \
        bash "$fixture/demo/record_demo.sh"

    [ "$status" -ne 0 ]
    [ "$(cat "$fixture/demo/live_demo.gif")" = "old gif" ]
    [ -z "$(compgen -G "$fixture/demo/.live-demo-recording.*" || true)" ]
}


@test "wrong kubectl context fails before asciinema starts" {
    local fixture="$BATS_TEST_TMPDIR/live-wrong-context"
    local fake_bin="$fixture/bin"
    local asciinema_marker="$BATS_TEST_TMPDIR/unexpected-asciinema"
    mkdir -p "$fixture/demo" "$fake_bin"
    cp "$SCRIPT" "$fixture/demo/record_demo.sh"
    cp demo/lib_demo.sh demo/live_demo.sh "$fixture/demo/"
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
        bash "$fixture/demo/record_demo.sh"

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
    cp "$SCRIPT" "$fixture/demo/record_demo.sh"
    cp demo/lib_demo.sh demo/live_demo.sh "$fixture/demo/"
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
        bash "$fixture/demo/record_demo.sh"

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
