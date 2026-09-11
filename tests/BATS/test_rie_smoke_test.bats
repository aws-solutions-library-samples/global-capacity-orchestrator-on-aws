#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for .github/scripts/rie_smoke_test.sh
# -----------------------------------------------------------------------------
# The script boots a Lambda image under the Runtime Interface Emulator with
# `docker run`, polls the invoke URL with `curl`, and checks the JSON error
# envelope with python3. `docker` and `curl` are fakes that record their argv
# and answer as scripted (the container that never serves, the container that
# exits, the reply that is not JSON or has the wrong type or message); the
# envelope check is the real python3. `sleep` is a no-op so the polling loop
# runs at full speed.
#
# Run:  bats tests/BATS/test_rie_smoke_test.bats
# -----------------------------------------------------------------------------

load 'helpers.sh'

SCRIPT="$REPO_ROOT/.github/scripts/rie_smoke_test.sh"

setup() {
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export DOCKER_CALLS="$BATS_TEST_TMPDIR/docker-calls"
    export CURL_CALLS="$BATS_TEST_TMPDIR/curl-calls"
    : > "$DOCKER_CALLS"
    : > "$CURL_CALLS"
    # FAKE_CONTAINER_RUNNING: what `docker ps -q --filter name=...` reports
    # (default: a container id; empty means the container is gone).
    write_stub "$FAKE_BIN" docker <<'FAKE_DOCKER'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_CALLS"
case "${1:-}" in
    run) echo "0123456789ab" ;;
    ps) printf '%s\n' "${FAKE_CONTAINER_RUNNING-0123456789ab}" ;;
    logs) echo "fake container log line" >&2 ;;
esac
exit 0
FAKE_DOCKER
    # FAKE_CURL_ANSWER_AFTER: how many attempts stay unanswered (default 0);
    # FAKE_CURL_REPLY: the body once it answers (default: the designed error).
    write_stub "$FAKE_BIN" curl <<'FAKE_CURL'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CURL_CALLS"
if [ "$(grep -c '' "$CURL_CALLS")" -le "${FAKE_CURL_ANSWER_AFTER:-0}" ]; then
    echo "curl: (7) Failed to connect" >&2
    exit 7
fi
designed='{"errorType": "ValueError", "errorMessage": "Unsupported certificate manager event: CiSmoke"}'
printf '%s' "${FAKE_CURL_REPLY-$designed}"
FAKE_CURL
    stub_noop "$FAKE_BIN" sleep
}

run_smoke() {
    run env PATH="$FAKE_BIN:$PATH" "$@" bash "$SCRIPT" \
        --image lambda-img:ci --name lambda-rie --host-port 19000 \
        --event '{"RequestType": "CiSmoke"}' \
        --expect-error-type ValueError \
        --expect-message-substring "Unsupported certificate manager event"
}

@test "rie_smoke_test.sh passes bash -n and shellcheck" {
    bash -n "$SCRIPT"
    command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "a handler that raises the designed error passes, and the container is removed" {
    run_smoke

    [ "$status" -eq 0 ]
    [[ "$output" == *'RIE response: {"errorType": "ValueError"'* ]]
    [[ "$output" == *"handler raised ValueError as designed: Unsupported certificate manager event: CiSmoke"* ]]
    # Stale container removed first, then the platform's filesystem contract.
    [ "$(sed -n 1p "$DOCKER_CALLS")" = "rm -f lambda-rie" ]
    grep -qx -- 'run -d --name lambda-rie --read-only --tmpfs /tmp -p 19000:8080 lambda-img:ci' "$DOCKER_CALLS"
    grep -q -- '-X POST http://127.0.0.1:19000/2015-03-31/functions/function/invocations -d {"RequestType": "CiSmoke"}' "$CURL_CALLS"
    # The EXIT trap removes the container on the way out.
    [ "$(tail -n1 "$DOCKER_CALLS")" = "rm -f lambda-rie" ]
}

@test "--env pairs reach docker run and the endpoint is polled until it answers" {
    run env PATH="$FAKE_BIN:$PATH" FAKE_CURL_ANSWER_AFTER=2 bash "$SCRIPT" \
        --image lambda-img:ci --name lambda-rie --host-port 19000 \
        --env AWS_REGION=us-east-1 --env LOG_LEVEL=DEBUG --boot-timeout 30 \
        --event '{"RequestType": "CiSmoke"}' \
        --expect-error-type ValueError \
        --expect-message-substring "Unsupported certificate manager event"

    [ "$status" -eq 0 ]
    grep -qx -- 'run -d --name lambda-rie --read-only --tmpfs /tmp -p 19000:8080 --env AWS_REGION=us-east-1 --env LOG_LEVEL=DEBUG lambda-img:ci' "$DOCKER_CALLS"
    [ "$(grep -c '' "$CURL_CALLS")" -eq 3 ]
    # While waiting, the script checked the container was still alive.
    grep -qxF -- 'ps -q --filter name=^lambda-rie$' "$DOCKER_CALLS"
}

@test "a container that exits before serving fails with its logs" {
    run_smoke FAKE_CURL_ANSWER_AFTER=99 FAKE_CONTAINER_RUNNING=

    [ "$status" -eq 1 ]
    [[ "$output" == *"::error::container lambda-rie exited before serving an invocation"* ]]
    [[ "$output" == *"fake container log line"* ]]
    [ "$(tail -n1 "$DOCKER_CALLS")" = "rm -f lambda-rie" ]
}

@test "an endpoint that never answers within the boot timeout fails with the logs" {
    # SECONDS starts at the deadline: a zero boot timeout expires on the first
    # unanswered poll.
    run env PATH="$FAKE_BIN:$PATH" FAKE_CURL_ANSWER_AFTER=99 bash "$SCRIPT" \
        --image lambda-img:ci --name lambda-rie --host-port 19000 --boot-timeout 0 \
        --event '{"RequestType": "CiSmoke"}' \
        --expect-error-type ValueError \
        --expect-message-substring "Unsupported certificate manager event"

    [ "$status" -eq 1 ]
    [[ "$output" == *"::error::RIE endpoint for lambda-img:ci did not answer within 0s"* ]]
    [[ "$output" == *"fake container log line"* ]]
}

@test "a reply that is not the designed error envelope fails with the logs" {
    local reply
    while IFS='|' read -r reply expected; do
        : > "$DOCKER_CALLS"
        : > "$CURL_CALLS"
        run env PATH="$FAKE_BIN:$PATH" FAKE_CURL_REPLY="$reply" bash "$SCRIPT" \
            --image lambda-img:ci --name lambda-rie --host-port 19000 \
            --event '{"RequestType": "CiSmoke"}' \
            --expect-error-type ValueError \
            --expect-message-substring "Unsupported certificate manager event"
        echo "case: $reply"
        [ "$status" -eq 1 ]
        [[ "$output" == *"$expected"* ]]
        [[ "$output" == *"fake container log line"* ]]
    done <<'CASES'
not json at all|RIE reply is not JSON: not json at all
["a", "list"]|RIE reply is not an error envelope
{"errorType": "KeyError", "errorMessage": "Unsupported certificate manager event"}|expected errorType 'ValueError', got 'KeyError'
{"errorType": "ValueError", "errorMessage": "Unable to locate credentials"}|expected errorMessage to contain 'Unsupported certificate manager event', got 'Unable to locate credentials'
CASES
}

@test "every required argument is checked and an unknown one is refused" {
    run bash "$SCRIPT" --image lambda-img:ci --name lambda-rie --host-port 19000 \
        --event '{}' --expect-error-type ValueError
    [ "$status" -eq 2 ]
    [[ "$output" == *"missing required argument: --expect-message-substring"* ]]

    run bash "$SCRIPT" --name lambda-rie
    [ "$status" -eq 2 ]
    [[ "$output" == *"missing required argument: --image"* ]]

    run bash "$SCRIPT" --image lambda-img:ci --platform linux/amd64
    [ "$status" -eq 2 ]
    [[ "$output" == *"unknown argument: --platform"* ]]
}
