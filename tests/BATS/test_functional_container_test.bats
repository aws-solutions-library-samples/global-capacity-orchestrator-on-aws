#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for .github/scripts/functional_container_test.sh
# -----------------------------------------------------------------------------
# The script boots a distroless service image under the manifests'
# pod-equivalent constraints and asserts its serving contract through `curl`,
# `docker exec` and `docker stop`. `docker` and `curl` are fakes that record
# every invocation and answer as scripted: which HTTP status and body each
# path returns, whether the container is still running, what exec and stop
# report. `sleep` is a no-op so the waits run at full speed.
#
# Run:  bats tests/BATS/test_functional_container_test.bats
# -----------------------------------------------------------------------------

load 'helpers.sh'

SCRIPT="$REPO_ROOT/.github/scripts/functional_container_test.sh"

setup() {
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export DOCKER_CALLS="$BATS_TEST_TMPDIR/docker-calls"
    export CURL_CALLS="$BATS_TEST_TMPDIR/curl-calls"
    : > "$DOCKER_CALLS"
    : > "$CURL_CALLS"
    # FAKE_RUNNING: `docker inspect .State.Running` answer (default true).
    # FAKE_EXIT_CODE: `docker inspect .State.ExitCode` after stop (default 0).
    # FAKE_EXEC_STATUS: exit status of `docker exec` (default 0).
    write_stub "$FAKE_BIN" docker <<'FAKE_DOCKER'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_CALLS"
case "${1:-}" in
    run) echo "0123456789ab" ;;
    inspect)
        case "$*" in
            *".State.Running"*) printf '%s\n' "${FAKE_RUNNING:-true}" ;;
            *".State.ExitCode"*) printf '%s\n' "${FAKE_EXIT_CODE:-0}" ;;
        esac
        ;;
    exec) exit "${FAKE_EXEC_STATUS:-0}" ;;
    logs) echo "fake container log line" ;;
esac
exit 0
FAKE_DOCKER
    # The fake curl honours `-o FILE` and `-w '%{http_code}'` the way curl
    # does. FAKE_HTTP is a ';'-separated table of "path=code[=body]"; a path
    # not listed answers 000 with no body (connection refused). FAKE_READY_AFTER
    # makes the first N requests to any path fail that way, so the boot wait
    # has something to wait for.
    write_stub "$FAKE_BIN" curl <<'FAKE_CURL'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CURL_CALLS"
out=""
write_out=""
url=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift 2 ;;
        -w) write_out="$2"; shift 2 ;;
        -s) shift ;;
        *) url="$1"; shift ;;
    esac
done
path="${url#http://127.0.0.1:*/}"
path="/${path}"
code="000"
body=""
if [ "$(grep -c '' "$CURL_CALLS")" -gt "${FAKE_READY_AFTER:-0}" ]; then
    table="${FAKE_HTTP:-/healthz=200=ok}"
    while [ -n "$table" ]; do
        entry="${table%%;*}"
        if [ "${entry%%=*}" = "$path" ]; then
            rest="${entry#*=}"
            code="${rest%%=*}"
            [ "$rest" != "$code" ] && body="${rest#*=}"
            break
        fi
        [ "$table" = "$entry" ] && break
        table="${table#*;}"
    done
fi
if [ -n "$out" ]; then printf '%s' "$body" > "$out"; fi
[ "$write_out" = '%{http_code}' ] && printf '%s' "$code"
[ "$code" != "000" ]
FAKE_CURL
    stub_noop "$FAKE_BIN" sleep
}

run_functional() {
    # run_functional [VAR=value ...] -- <extra script args...>
    local vars=()
    while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
        vars+=("$1")
        shift
    done
    [ "$#" -gt 0 ] && shift
    run env PATH="$FAKE_BIN:$PATH" "${vars[@]}" bash "$SCRIPT" \
        --image svc:ci --name svc-fn --host-port 18080 "$@"
}

@test "functional_container_test.sh passes bash -n and shellcheck" {
    bash -n "$SCRIPT"
    command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "a healthy service passes every stage under the pod-equivalent constraints" {
    run_functional FAKE_READY_AFTER=2 \
        FAKE_HTTP="/healthz=200=ok;/readyz=200;/metrics=503=kubernetes unreachable" \
        -- --env LOG_LEVEL=DEBUG --env GCO_REGION=us-east-1 \
        --kubeconfig "$BATS_TEST_TMPDIR/kubeconfig" --container-port 9090 \
        --probe "/readyz=200" --probe "/metrics=503=kubernetes unreachable" \
        --exec-python "import sys; sys.exit(0)" --min-uptime 5 --stop-timeout 10

    [ "$status" -eq 0 ]
    [[ "$output" == *"svc-fn: serving (/healthz -> 200)"* ]]
    [[ "$output" == *"svc-fn: GET /readyz -> 200 OK"* ]]
    [[ "$output" == *'svc-fn: GET /metrics -> 503 (body matches "kubernetes unreachable") OK'* ]]
    [[ "$output" == *"svc-fn: exec python -c 'import sys; sys.exit(0)' OK"* ]]
    [[ "$output" == *"svc-fn: still serving after 5s with unreachable dependencies OK"* ]]
    [[ "$output" == *"svc-fn: SIGTERM shutdown exited 0 OK"* ]]
    [[ "$output" == *"svc-fn: functional container test PASSED"* ]]
    # The run carried every constraint the manifests' securityContext enforces,
    # the env pairs, the kubeconfig mount, and the mapped container port.
    grep -qxF -- "run -d --name svc-fn --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges -p 127.0.0.1:18080:9090 -e LOG_LEVEL=DEBUG -e GCO_REGION=us-east-1 -v ${BATS_TEST_TMPDIR}/kubeconfig:/kubeconfig:ro -e KUBECONFIG=/kubeconfig svc:ci" "$DOCKER_CALLS"
    grep -qxF -- "exec svc-fn python -c import sys; sys.exit(0)" "$DOCKER_CALLS"
    grep -qxF -- "stop -t 10 svc-fn" "$DOCKER_CALLS"
    # Two unanswered polls before /healthz came up, each checking the container
    # was still running; the trap removed the container at the end.
    [ "$(grep -c 'http://127.0.0.1:18080/healthz' "$CURL_CALLS")" -ge 4 ]
    grep -qF -- "inspect -f {{.State.Running}} svc-fn" "$DOCKER_CALLS"
    [ "$(tail -n1 "$DOCKER_CALLS")" = "rm -f svc-fn" ]
}

@test "a service that exits during startup fails with its logs" {
    run_functional FAKE_READY_AFTER=99 FAKE_RUNNING=false

    [ "$status" -eq 1 ]
    [[ "$output" == *"::error::svc-fn: container exited during startup (last /healthz code: 000)"* ]]
    [[ "$output" == *"===== svc-fn: container logs ====="* ]]
    [[ "$output" == *"fake container log line"* ]]
}

@test "a service that never serves within the boot timeout fails" {
    # The deadline is measured in SECONDS, so a zero timeout expires on the
    # first unanswered poll (the default, 60s, is what the workflows rely on).
    run_functional FAKE_READY_AFTER=99 -- --boot-timeout 0

    [ "$status" -eq 1 ]
    [[ "$output" == *"::error::svc-fn: /healthz never returned 200 within 0s (last: 000)"* ]]
    [[ "$output" == *"fake container log line"* ]]
    [ "$(tail -n1 "$DOCKER_CALLS")" = "rm -f svc-fn" ]
}

@test "--wait-path chooses the endpoint the boot wait polls" {
    run_functional FAKE_HTTP="/livez=200" -- --wait-path /livez

    [ "$status" -eq 0 ]
    [[ "$output" == *"svc-fn: serving (/livez -> 200)"* ]]
    grep -q 'http://127.0.0.1:18080/livez' "$CURL_CALLS"
    ! grep -q 'http://127.0.0.1:18080/healthz' "$CURL_CALLS"
}

@test "a probe with the wrong status or body fails and shows the body" {
    run_functional FAKE_HTTP="/healthz=200=ok;/readyz=503=warming up" -- --probe "/readyz=200"
    [ "$status" -eq 1 ]
    [[ "$output" == *"response body: warming up"* ]]
    [[ "$output" == *"::error::svc-fn: GET /readyz: expected HTTP 200, got 503"* ]]

    : > "$DOCKER_CALLS"
    : > "$CURL_CALLS"
    run_functional FAKE_HTTP="/healthz=200=ok;/readyz=503=warming up" -- --probe "/readyz=503=kubernetes unreachable"
    [ "$status" -eq 1 ]
    [[ "$output" == *"response body: warming up"* ]]
    [[ "$output" == *"::error::svc-fn: GET /readyz: body does not contain 'kubernetes unreachable'"* ]]
}

@test "an exec command that fails in the live container fails the test" {
    run_functional FAKE_EXEC_STATUS=1 -- --exec-python "import nosuchmodule"

    [ "$status" -eq 1 ]
    [[ "$output" == *"::error::svc-fn: exec command failed in live container: python -c 'import nosuchmodule'"* ]]
}

@test "a service that dies or degrades within --min-uptime fails" {
    # /healthz answers 200 at once, so the boot wait never consults docker
    # inspect; the first inspect is the uptime check, which finds it dead.
    run_functional FAKE_RUNNING=false -- --min-uptime 3
    [ "$status" -eq 1 ]
    [[ "$output" == *"::error::svc-fn: container died within 3s of becoming healthy"* ]]

    # Alive, but /healthz no longer answers 200 after the wait.
    : > "$DOCKER_CALLS"
    : > "$CURL_CALLS"
    write_stub "$FAKE_BIN" curl <<'FAKE_CURL'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CURL_CALLS"
# First request: healthy. Every later one: 503.
if [ "$(grep -c '' "$CURL_CALLS")" -le 1 ]; then printf '200'; else printf '503'; fi
FAKE_CURL
    run_functional -- --min-uptime 3
    [ "$status" -eq 1 ]
    [[ "$output" == *"::error::svc-fn: /healthz degraded to 503 after 3s"* ]]
}

@test "the shutdown exit status must match --expect-stop-exit" {
    run_functional FAKE_EXIT_CODE=143 -- --expect-stop-exit 0
    [ "$status" -eq 1 ]
    [[ "$output" == *"::error::svc-fn: SIGTERM shutdown: expected exit 0, got 143"* ]]

    # inference-monitor has no SIGTERM handler today, so its job expects 143.
    : > "$DOCKER_CALLS"
    : > "$CURL_CALLS"
    run_functional FAKE_EXIT_CODE=143 -- --expect-stop-exit 143
    [ "$status" -eq 0 ]
    [[ "$output" == *"svc-fn: SIGTERM shutdown exited 143 OK"* ]]
    grep -qxF -- "stop -t 30 svc-fn" "$DOCKER_CALLS"
}

@test "the required arguments and unknown arguments are checked first" {
    run bash "$SCRIPT" --image svc:ci --name svc-fn
    [ "$status" -eq 2 ]
    [[ "$output" == *"required: --image, --name, --host-port"* ]]

    run bash "$SCRIPT" --image svc:ci --name svc-fn --host-port 18080 --platform linux/amd64
    [ "$status" -eq 2 ]
    [[ "$output" == *"unknown argument: --platform"* ]]
}
