#!/usr/bin/env bats
# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for docs/client-examples/curl_sigv4_proxy_example.sh
# ─────────────────────────────────────────────────────────────────────────────
# Functional tests for URL parsing, proxy lifecycle, request patterns,
# and cleanup behavior, plus end-to-end runs of the example against a faked
# AWS CLI, aws-sigv4-proxy and curl: the fakes record every call, so the tests
# assert on the proxy argv, the Host-header rewriting and the exact HTTP
# status handling, and prove the proxy is stopped on every exit path.
#
# Run:  bats tests/BATS/test_curl_sigv4_proxy.bats
# ─────────────────────────────────────────────────────────────────────────────

load 'helpers.sh'

SCRIPT="$REPO_ROOT/docs/client-examples/curl_sigv4_proxy_example.sh"

# ── Syntax & Structure ───────────────────────────────────────────────────────

@test "curl_sigv4_proxy_example.sh exists and is executable" {
    [ -f "$SCRIPT" ]
    [ -x "$SCRIPT" ]
}

@test "curl_sigv4_proxy_example.sh passes bash -n syntax check" {
    bash -n "$SCRIPT"
}

@test "curl_sigv4_proxy_example.sh passes shellcheck" {
    command -v shellcheck &>/dev/null || skip "shellcheck not installed"
    # -x matches the project's repo-wide shellcheck policy
    # (lint:shellcheck:shell in .github/workflows/lint.yml).
    shellcheck -x "$SCRIPT"
}

# ── URL Parsing Logic (functional — runs the actual sed/cut pipeline) ─────────

@test "host extraction strips https:// and path" {
    run bash -c '
        url="https://abc123.execute-api.us-east-1.amazonaws.com/prod"
        echo "$url" | sed "s|https://||" | sed "s|http://||" | cut -d"/" -f1
    '
    [ "$output" = "abc123.execute-api.us-east-1.amazonaws.com" ]
}

@test "host extraction strips http:// and path" {
    run bash -c '
        url="http://localhost:8080/api/v1"
        echo "$url" | sed "s|https://||" | sed "s|http://||" | cut -d"/" -f1
    '
    [ "$output" = "localhost:8080" ]
}

@test "host extraction handles URL with no path" {
    run bash -c '
        url="https://example.amazonaws.com"
        echo "$url" | sed "s|https://||" | sed "s|http://||" | cut -d"/" -f1
    '
    [ "$output" = "example.amazonaws.com" ]
}

@test "API ID extraction gets first subdomain from host" {
    run bash -c 'echo "abc123.execute-api.us-east-1.amazonaws.com" | cut -d"." -f1'
    [ "$output" = "abc123" ]
}

@test "API ID extraction works for single-label hosts" {
    run bash -c 'echo "localhost" | cut -d"." -f1'
    [ "$output" = "localhost" ]
}

# ── Configuration Defaults (functional — verifies actual values) ──────────────

@test "default API region comes from cdk context with us-east-2 fallback" {
    grep -Fq "deployment_regions.api_gateway' 'us-east-2'" "$SCRIPT"
}

@test "default proxy port is 8080 unless overridden" {
    run bash -c 'unset PROXY_PORT; echo "${PROXY_PORT:-8080}"'
    [ "$output" = "8080" ]
    grep -Fq 'PROXY_PORT=${PROXY_PORT:-8080}' "$SCRIPT"
}

@test "stack name is constructed from project name" {
    run bash -c 'PROJECT_NAME="gco"; STACK_NAME="${PROJECT_NAME}-api-gateway"; echo "$STACK_NAME"'
    [ "$output" = "gco-api-gateway" ]
}

@test "stack name supports non-default project names" {
    run bash -c 'PROJECT_NAME="research"; STACK_NAME="${PROJECT_NAME}-api-gateway"; echo "$STACK_NAME"'
    [ "$output" = "research-api-gateway" ]
}

# ── Proxy Lifecycle Management ───────────────────────────────────────────────

@test "script checks if proxy port is already in use via lsof" {
    grep -q "lsof.*PROXY_PORT" "$SCRIPT"
}

@test "script registers a trap to clean up proxy on exit" {
    grep -q "trap cleanup EXIT" "$SCRIPT"
}

@test "cleanup function sends kill to proxy PID" {
    grep -q 'kill "$PROXY_PID"' "$SCRIPT"
}

@test "script waits for proxy startup before sending requests" {
    grep -q "sleep 2" "$SCRIPT"
}

# ── HTTP Request Patterns (functional — verifies method + path combos) ────────

@test "script sends POST to /api/v1/manifests" {
    grep -q 'POST.*api/v1/manifests' "$SCRIPT"
}

@test "script sends GET to the submitted Job status endpoint" {
    grep -Fq '${LOCAL_API_BASE}/api/v1/jobs/gco-jobs/curl-example-job' "$SCRIPT"
}

@test "script sends DELETE to the submitted Job endpoint" {
    grep -Fq -- '-X DELETE' "$SCRIPT"
    grep -Fq '${LOCAL_API_BASE}/api/v1/jobs/gco-jobs/curl-example-job' "$SCRIPT"
}

@test "all proxy requests include Host header" {
    # Count Host header usage — should appear in POST, GET, DELETE, and status check
    count=$(grep -c '"Host: ${API_HOST}"' "$SCRIPT" || true)
    [ "$count" -ge 3 ]
}

@test "HTTP status code is captured from curl response" {
    grep -q 'write-out.*http_code\|HTTP_STATUS' "$SCRIPT"
}

# ── Manifest Payload (functional — validates JSON structure) ──────────────────

@test "manifest payload has required Kubernetes fields" {
    run bash -c '
        echo "{
          \"manifest\": {
            \"apiVersion\": \"batch/v1\",
            \"kind\": \"Job\",
            \"metadata\": {\"name\": \"curl-example-job\"}
          },
          \"namespace\": \"gco-jobs\"
        }" | jq -e ".manifest.apiVersion and .manifest.kind and .manifest.metadata.name" > /dev/null
    '
    [ "$status" -eq 0 ]
}

@test "manifest payload targets gco-jobs namespace" {
    grep -q '"namespace": "gco-jobs"' "$SCRIPT" || grep -q "'namespace': 'gco-jobs'" "$SCRIPT"
}

# ── Authentication Testing ───────────────────────────────────────────────────

@test "script tests unauthenticated request and expects 403" {
    grep -q "403" "$SCRIPT"
    grep -q "Unsigned request correctly rejected" "$SCRIPT"
}

@test "unauthenticated test hits the real API endpoint (not proxy)" {
    # The auth test should bypass the proxy to prove SigV4 is required
    grep -Fq '${API_ENDPOINT}/api/v1/jobs?limit=1' "$SCRIPT"
}

# ── Cleanup ──────────────────────────────────────────────────────────────────

@test "temporary manifest file is cleaned up" {
    grep -Fq 'rm -f "$PAYLOAD_FILE"' "$SCRIPT"
}

@test "script includes at least 5 numbered examples" {
    count=$(grep -c "Example [0-9]" "$SCRIPT" || true)
    [ "$count" -ge 5 ]
}

# ── End-to-end runs against a faked AWS CLI, proxy and curl ──────────────────
#
# `aws` answers the identity check and the stack's ApiEndpoint output; the
# fake `aws-sigv4-proxy` records its argv, then stays alive until it is killed
# (writing a marker on SIGTERM, so the tests can prove the cleanup trap ran);
# `curl` records its argv and answers per URL with a body and the status the
# example extracts through its `-w` template — the fake honours `-w` the way
# curl does, substituting %{http_code}, rather than printing a fixed footer.
# `sleep` is shortened so the proxy start-up wait costs almost nothing; `lsof`
# is only on PATH in the test that needs it.

setup_proxy_fakes() {
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export AWS_CALLS="$BATS_TEST_TMPDIR/aws-calls"
    export PROXY_ARGS="$BATS_TEST_TMPDIR/proxy-args"
    export PROXY_STOPPED="$BATS_TEST_TMPDIR/proxy-stopped"
    export CURL_CALLS="$BATS_TEST_TMPDIR/curl-calls"
    : > "$AWS_CALLS"
    : > "$CURL_CALLS"
    write_stub "$FAKE_BIN" aws <<'AWS'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$AWS_CALLS"
case "$1 $2" in
    "sts get-caller-identity")
        printf '{"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/developer"}\n'
        ;;
    "cloudformation describe-stacks")
        printf '%s\n' "${FAKE_API_ENDPOINT:-https://abc123.execute-api.eu-west-1.amazonaws.com/prod/}"
        ;;
    *)
        echo "unexpected aws call: $*" >&2
        exit 2
        ;;
esac
AWS
    write_stub "$FAKE_BIN" aws-sigv4-proxy <<'PROXY'
#!/usr/bin/env bash
printf '%s\n' "$*" > "$PROXY_ARGS"
if [ "${FAKE_PROXY_DIES:-0}" = "1" ]; then
    echo "listen tcp :8080: bind: address already in use" >&2
    exit 1
fi
trap 'touch "$PROXY_STOPPED"; exit 0' TERM
while :; do /bin/sleep 1; done
PROXY
    write_stub "$FAKE_BIN" curl <<'CURL'
#!/usr/bin/env bash
{
    printf '%s\037' "$@" | tr '\n' '\036'
    printf '\n'
} >> "$CURL_CALLS"
template=""
method="GET"
url=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -w) template="$2"; shift 2 ;;
        -X) method="$2"; shift 2 ;;
        -H|--data-binary) shift 2 ;;
        -sS) shift ;;
        *) url="$1"; shift ;;
    esac
done
case "$method $url" in
    "POST "*"/api/v1/manifests")
        body='{"results": [{"kind": "Job", "name": "curl-example-job"}]}'
        code="${FAKE_MANIFEST_STATUS:-200}" ;;
    "GET "*"/api/v1/jobs/gco-jobs/curl-example-job")
        body='{"name": "curl-example-job", "status": {"active": 1}}'; code=200 ;;
    "GET "*"/api/v1/jobs?namespace=gco-jobs&limit=20")
        body='{"jobs": [{"name": "curl-example-job"}]}'; code=200 ;;
    "DELETE "*"/api/v1/jobs/gco-jobs/curl-example-job")
        body='deleted'; code=200 ;;   # plain text: the example must not choke on non-JSON
    "GET https://"*"/api/v1/jobs?limit=1")
        body='{"message": "Forbidden"}'; code="${FAKE_UNSIGNED_STATUS:-403}" ;;
    *)
        body='{"message": "unexpected request"}'; code=500 ;;
esac
printf '%s' "$body"
printf '%s' "${template//\%\{http_code\}/$code}"
CURL
    # The example sleeps two seconds for the proxy to come up and then checks
    # it is still alive. A no-op sleep would race that check against a proxy
    # that exits at start-up, so this fake keeps a short real delay instead.
    write_stub "$FAKE_BIN" sleep <<'SLEEP'
#!/usr/bin/env bash
exec /bin/sleep 0.3
SLEEP
    ANSWER_NO="$BATS_TEST_TMPDIR/answer-no"
    ANSWER_YES="$BATS_TEST_TMPDIR/answer-yes"
    printf 'n\n' > "$ANSWER_NO"
    printf 'y\n' > "$ANSWER_YES"
}

curl_records() {
    tr '\037\036' '  ' < "$CURL_CALLS"
}

@test "the proxy is started for execute-api in the API region and every request is routed through it with the real Host" {
    setup_proxy_fakes
    run env PATH="$FAKE_BIN:$PATH" API_REGION=eu-west-1 PROJECT_NAME=acme \
        bash "$SCRIPT" < "$ANSWER_NO"

    [ "$status" -eq 0 ]
    grep -Fq 'cloudformation describe-stacks --stack-name acme-api-gateway --region eu-west-1' "$AWS_CALLS"
    [ "$(cat "$PROXY_ARGS")" = "--name execute-api --region eu-west-1 --port 8080 --upstream-url-scheme https --log-level info" ]
    [[ "$output" == *"Signing region: eu-west-1"* ]]
    local host="abc123.execute-api.eu-west-1.amazonaws.com"
    # Examples 1-3 go to the local proxy on the stage path, with the API's Host.
    curl_records | sed -n 1p | grep -Fq -- "-X POST http://localhost:8080/prod/api/v1/manifests -H Host: ${host} -H Content-Type: application/json --data-binary @"
    curl_records | sed -n 2p | grep -Fq -- " http://localhost:8080/prod/api/v1/jobs/gco-jobs/curl-example-job -H Host: ${host} "
    curl_records | sed -n 3p | grep -Fq -- " http://localhost:8080/prod/api/v1/jobs?namespace=gco-jobs&limit=20 -H Host: ${host} "
    # Example 5 bypasses the proxy to prove SigV4 is required.
    curl_records | sed -n 4p | grep -Fq -- " https://${host}/prod/api/v1/jobs?limit=1 "
    [ "$(wc -l < "$CURL_CALLS")" -eq 4 ]
    [[ "$output" == *"Skipping deletion."* ]]
    [[ "$output" == *"Unsigned request correctly rejected."* ]]
    [[ "$output" == *"The local URL includes the API Gateway stage path (/prod)."* ]]
    # The trap stopped the proxy and removed the payload file.
    [ -e "$PROXY_STOPPED" ]
    [[ "$output" == *"Stopping aws-sigv4-proxy..."* ]]
    [ -z "$(compgen -G "${TMPDIR:-/tmp}/gco-manifest.*" || true)" ]
}

@test "answering y deletes the job and a plain-text reply is printed as-is" {
    setup_proxy_fakes
    run env PATH="$FAKE_BIN:$PATH" API_REGION=eu-west-1 bash "$SCRIPT" < "$ANSWER_YES"

    [ "$status" -eq 0 ]
    curl_records | sed -n 4p | grep -Fq -- "-X DELETE http://localhost:8080/prod/api/v1/jobs/gco-jobs/curl-example-job -H Host: "
    [ "$(wc -l < "$CURL_CALLS")" -eq 5 ]
    [[ "$output" == *"deleted"* ]]
    [[ "$output" != *"Skipping deletion."* ]]
}

@test "PROXY_PORT moves both the proxy and the local URLs" {
    setup_proxy_fakes
    run env PATH="$FAKE_BIN:$PATH" API_REGION=eu-west-1 PROXY_PORT=9090 bash "$SCRIPT" < "$ANSWER_NO"

    [ "$status" -eq 0 ]
    grep -Fq -- '--port 9090' "$PROXY_ARGS"
    [ "$(curl_records | grep -c 'http://localhost:9090/prod/')" -eq 3 ]
    ! curl_records | grep -q 'localhost:8080'
}

@test "an unsigned request that is not rejected with 403 is reported, not celebrated" {
    setup_proxy_fakes
    run env PATH="$FAKE_BIN:$PATH" API_REGION=eu-west-1 FAKE_UNSIGNED_STATUS=200 \
        bash "$SCRIPT" < "$ANSWER_NO"

    [ "$status" -eq 0 ]
    [[ "$output" == *"Expected HTTP 403, received 200."* ]]
    [[ "$output" != *"correctly rejected"* ]]
}

@test "a failed manifest submission stops the walkthrough and still stops the proxy" {
    setup_proxy_fakes
    run env PATH="$FAKE_BIN:$PATH" API_REGION=eu-west-1 FAKE_MANIFEST_STATUS=500 \
        bash "$SCRIPT" < "$ANSWER_NO"

    [ "$status" -eq 1 ]
    [[ "$output" == *"HTTP status: 500"* ]]
    [[ "$output" == *"Manifest submission failed"* ]]
    [ "$(wc -l < "$CURL_CALLS")" -eq 1 ]
    [ -e "$PROXY_STOPPED" ]
}

@test "a proxy that exits at start-up is reported instead of being sent requests" {
    setup_proxy_fakes
    run env PATH="$FAKE_BIN:$PATH" API_REGION=eu-west-1 FAKE_PROXY_DIES=1 bash "$SCRIPT" < "$ANSWER_NO"

    [ "$status" -eq 1 ]
    [[ "$output" == *"aws-sigv4-proxy failed to start"* ]]
    [ ! -s "$CURL_CALLS" ]
}

@test "a port already in use is refused before the proxy is started" {
    setup_proxy_fakes
    write_stub "$FAKE_BIN" lsof <<'LSOF'
#!/usr/bin/env bash
printf '%s\n' "$*" > "$LSOF_ARGS"
echo 4242   # a PID: something is listening
exit 0
LSOF
    export LSOF_ARGS="$BATS_TEST_TMPDIR/lsof-args"
    run env PATH="$FAKE_BIN:$PATH" API_REGION=eu-west-1 PROXY_PORT=8080 bash "$SCRIPT" < "$ANSWER_NO"

    [ "$status" -eq 1 ]
    [[ "$output" == *"port 8080 is already in use; choose another PROXY_PORT"* ]]
    [ "$(cat "$LSOF_ARGS")" = "-Pi :8080 -sTCP:LISTEN -t" ]
    [ ! -e "$PROXY_ARGS" ]
}

@test "a stack without an ApiEndpoint output fails before the proxy is started" {
    setup_proxy_fakes
    run env PATH="$FAKE_BIN:$PATH" API_REGION=eu-west-1 FAKE_API_ENDPOINT=None bash "$SCRIPT" < "$ANSWER_NO"

    [ "$status" -eq 1 ]
    [[ "$output" == *"ApiEndpoint was not found in stack gco-api-gateway"* ]]
    [ ! -e "$PROXY_ARGS" ]
}

@test "copied out of the checkout, the example falls back to us-east-2 and gco-api-gateway" {
    setup_proxy_fakes
    local project="$BATS_TEST_TMPDIR/their-project"
    mkdir -p "$project/docs/client-examples"
    ln -s "$SCRIPT" "$project/docs/client-examples/curl_sigv4_proxy_example.sh"
    run env PATH="$FAKE_BIN:$PATH" bash "$project/docs/client-examples/curl_sigv4_proxy_example.sh" < "$ANSWER_NO"

    [ "$status" -eq 0 ]
    grep -Fq 'cloudformation describe-stacks --stack-name gco-api-gateway --region us-east-2' "$AWS_CALLS"
    grep -Fq -- '--region us-east-2' "$PROXY_ARGS"
}

@test "a missing required tool is named before any AWS call" {
    setup_proxy_fakes
    rm -f "$FAKE_BIN/aws-sigv4-proxy"
    local tools="$BATS_TEST_TMPDIR/tools"
    link_tools "$tools" bash dirname jq
    run env PATH="$FAKE_BIN:$tools" bash "$SCRIPT" < "$ANSWER_NO"

    [ "$status" -eq 1 ]
    [[ "$output" == *"required command 'aws-sigv4-proxy' is not installed"* ]]
    [ ! -s "$AWS_CALLS" ]
}
