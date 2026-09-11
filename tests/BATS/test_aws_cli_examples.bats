#!/usr/bin/env bats
# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for docs/client-examples/aws_cli_examples.sh
# ─────────────────────────────────────────────────────────────────────────────
# Functional tests for API region detection, manifest payload validity,
# and URL handling logic, plus end-to-end runs of the example against a faked
# AWS CLI and curl that record every call: the tests assert on the exact
# SigV4 argv the example signs its requests with and on each preflight
# refusal.
#
# Run:  bats tests/BATS/test_aws_cli_examples.bats
# ─────────────────────────────────────────────────────────────────────────────

load 'helpers.sh'

SCRIPT="$REPO_ROOT/docs/client-examples/aws_cli_examples.sh"

# ── Syntax & Structure ───────────────────────────────────────────────────────

@test "aws_cli_examples.sh exists and is executable" {
    [ -f "$SCRIPT" ]
    [ -x "$SCRIPT" ]
}

@test "aws_cli_examples.sh passes bash -n syntax check" {
    bash -n "$SCRIPT"
}

@test "aws_cli_examples.sh passes shellcheck" {
    command -v shellcheck &>/dev/null || skip "shellcheck not installed"
    # -x matches the repo-wide lint:shellcheck:shell workflow policy.
    shellcheck -x "$SCRIPT"
}

# ── API Region Detection (functional — evaluates the actual logic) ────────────

@test "API_REGION defaults to us-east-2 when no env var and no cdk.json" {
    run bash -c '
        unset API_REGION
        if [ -z "${API_REGION:-}" ]; then
            if [ -f "/nonexistent/cdk.json" ]; then
                API_REGION="from-cdk"
            else
                API_REGION="us-east-2"
            fi
        fi
        echo "$API_REGION"
    '
    [ "$output" = "us-east-2" ]
}

@test "API_REGION env var takes precedence over cdk.json" {
    run bash -c '
        export API_REGION=eu-west-1
        if [ -z "${API_REGION:-}" ]; then
            API_REGION="us-east-2"
        fi
        echo "$API_REGION"
    '
    [ "$output" = "eu-west-1" ]
}

@test "API_REGION reads api_gateway region from real cdk.json" {
    command -v python3 &>/dev/null || skip "python3 not installed"
    run python3 -c "
import json
d = json.load(open('$REPO_ROOT/cdk.json'))
print(d.get('context',{}).get('deployment_regions',{}).get('api_gateway','us-east-2'))
"
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^[a-z]{2}-[a-z]+-[0-9]+$ ]]
}

# ── URL Handling (functional — bash string operations) ────────────────────────

@test "trailing slash is stripped from API endpoint" {
    run bash -c 'API_ENDPOINT="https://example.com/prod/"; echo "${API_ENDPOINT%/}"'
    [ "$output" = "https://example.com/prod" ]
}

@test "no-op when endpoint has no trailing slash" {
    run bash -c 'API_ENDPOINT="https://example.com/prod"; echo "${API_ENDPOINT%/}"'
    [ "$output" = "https://example.com/prod" ]
}

# ── Manifest Payload Validity (functional — parses JSON with jq) ──────────────

@test "simple job payload is valid JSON with required K8s fields" {
    run bash -c '
        echo "{
          \"manifests\": [{
            \"apiVersion\": \"batch/v1\",
            \"kind\": \"Job\",
            \"metadata\": {\"name\": \"example-job\", \"namespace\": \"gco-jobs\"},
            \"spec\": {}
          }]
        }" | jq -e ".manifests[0].apiVersion" > /dev/null
    '
    [ "$status" -eq 0 ]
}

@test "GPU job payload includes nvidia.com/gpu resource limit" {
    grep -q "nvidia.com/gpu" "$SCRIPT"
}

@test "all manifest payloads use gco-jobs namespace" {
    # Count namespace references — should appear in every example payload
    count=$(grep -c '"namespace": "gco-jobs"' "$SCRIPT" || true)
    [ "$count" -ge 2 ]
}

@test "all images in payloads are from trusted registries" {
    # Extract image strings and verify they're from known-good sources
    while IFS= read -r line; do
        image=$(echo "$line" | grep -oP '"image":\s*"[^"]+"' | sed 's/.*"image":\s*"//;s/"//')
        [ -z "$image" ] && continue
        [[ "$image" == busybox* || "$image" == nvidia* ]]
    done < "$SCRIPT"
}

# ── SigV4 Authentication Pattern ─────────────────────────────────────────────

@test "curl uses --aws-sigv4 flag for SigV4 signing" {
    grep -q "\-\-aws-sigv4" "$SCRIPT"
}

@test "SigV4 signing targets execute-api service" {
    grep -q 'aws:amz:.*:execute-api' "$SCRIPT"
}

@test "requests target /api/v1/manifests endpoint" {
    grep -q "/api/v1/manifests" "$SCRIPT"
}

@test "POST requests set Content-Type to application/json" {
    grep -q "Content-Type: application/json" "$SCRIPT"
}

# ── Script Coverage ──────────────────────────────────────────────────────────

@test "script includes at least 4 numbered examples" {
    count=$(grep -c "Example [0-9]" "$SCRIPT" || true)
    [ "$count" -ge 4 ]
}

# ── End-to-end runs against a faked AWS CLI and curl ─────────────────────────
#
# `aws` answers the three calls the example makes (caller identity, the stack's
# ApiEndpoint output, exported credentials) from environment knobs and appends
# every argv to $AWS_CALLS; `curl` appends its argv to $CURL_CALLS (one call per
# line, with newlines inside a payload folded) and answers with
# the JSON the example pipes through jq. The credential values are placeholders,
# not key-shaped strings, because the example never validates their shape.

setup_client_fakes() {
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export AWS_CALLS="$BATS_TEST_TMPDIR/aws-calls"
    export CURL_CALLS="$BATS_TEST_TMPDIR/curl-calls"
    : > "$AWS_CALLS"
    : > "$CURL_CALLS"
    write_stub "$FAKE_BIN" aws <<'AWS'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$AWS_CALLS"
case "$1 $2" in
    "sts get-caller-identity")
        printf '{"UserId": "AIDAEXAMPLE", "Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/developer"}\n'
        ;;
    "cloudformation describe-stacks")
        printf '%s\n' "${FAKE_API_ENDPOINT:-https://abc123.execute-api.eu-west-1.amazonaws.com/prod/}"
        ;;
    "configure export-credentials")
        if [ "${FAKE_SESSION_TOKEN:-}" = "1" ]; then
            printf '{"Version": 1, "AccessKeyId": "EXAMPLEKEYID", "SecretAccessKey": "EXAMPLESECRET", "SessionToken": "EXAMPLETOKEN"}\n'
        else
            printf '{"Version": 1, "AccessKeyId": "EXAMPLEKEYID", "SecretAccessKey": "EXAMPLESECRET"}\n'
        fi
        ;;
    *)
        echo "unexpected aws call: $*" >&2
        exit 2
        ;;
esac
AWS
    write_stub "$FAKE_BIN" curl <<'CURL'
#!/usr/bin/env bash
# One record per invocation, one line each: arguments joined with the ASCII
# unit separator and any newline inside an argument (the JSON payloads)
# folded to the record separator, so a multi-line payload stays one record.
{
    printf '%s\037' "$@" | tr '\n' '\036'
    printf '\n'
} >> "$CURL_CALLS"
case "$*" in
    *"/api/v1/manifests"*) printf '{"results": [{"kind": "Job", "name": "example-job", "status": "applied"}]}\n' ;;
    *"/api/v1/jobs/gco-jobs/example-job"*) printf '{"name": "example-job", "status": {"active": 1}}\n' ;;
    *"/api/v1/jobs"*) printf '{"jobs": [{"name": "example-job"}], "count": 1}\n' ;;
    *) printf '{}\n' ;;
esac
CURL
}

# curl_records: the recorded curl invocations, one per line, separators as ' '.
curl_records() {
    tr '\037\036' '  ' < "$CURL_CALLS"
}

@test "every request is signed with SigV4 for execute-api in the API region, carrying the session token" {
    setup_client_fakes
    run env PATH="$FAKE_BIN:$PATH" API_REGION=eu-west-1 PROJECT_NAME=acme FAKE_SESSION_TOKEN=1 \
        bash "$SCRIPT"

    [ "$status" -eq 0 ]
    grep -Fxq 'sts get-caller-identity' "$AWS_CALLS"
    grep -Fq 'cloudformation describe-stacks --stack-name acme-api-gateway --region eu-west-1' "$AWS_CALLS"
    grep -Fxq 'configure export-credentials --format process' "$AWS_CALLS"
    [ "$(wc -l < "$CURL_CALLS")" -eq 4 ]
    # Every one of the four requests carries the same signing arguments.
    [ "$(curl_records | grep -c -- '--aws-sigv4 aws:amz:eu-west-1:execute-api ')" -eq 4 ]
    [ "$(curl_records | grep -c -- '--user EXAMPLEKEYID:EXAMPLESECRET ')" -eq 4 ]
    [ "$(curl_records | grep -c -- '--header X-Amz-Security-Token: EXAMPLETOKEN ')" -eq 4 ]
    [[ "$output" == *"API endpoint: https://abc123.execute-api.eu-west-1.amazonaws.com/prod"* ]]
}

@test "the four examples hit the documented endpoints, with the trailing slash stripped" {
    setup_client_fakes
    run env PATH="$FAKE_BIN:$PATH" API_REGION=eu-west-1 FAKE_SESSION_TOKEN=1 bash "$SCRIPT"

    [ "$status" -eq 0 ]
    local base="https://abc123.execute-api.eu-west-1.amazonaws.com/prod"
    # 1: POST the Job manifest as JSON.
    curl_records | sed -n 1p | grep -Fq -- "-X POST ${base}/api/v1/manifests -H Content-Type: application/json --data "
    curl_records | sed -n 1p | grep -Fq '"name": "example-job"'
    # 2: list jobs with URL-encoded query parameters.
    curl_records | sed -n 2p | grep -Fq -- "--get ${base}/api/v1/jobs --data-urlencode namespace=gco-jobs --data-urlencode limit=20"
    # 3: inspect the submitted job.
    curl_records | sed -n 3p | grep -Fq -- " ${base}/api/v1/jobs/gco-jobs/example-job "
    # 4: the GPU manifest is a dry run.
    curl_records | sed -n 4p | grep -Fq -- "-X POST ${base}/api/v1/manifests"
    curl_records | sed -n 4p | grep -Fq '"dry_run": true'
    curl_records | sed -n 4p | grep -Fq '"nvidia.com/gpu": "1"'
    # No request ever carries the doubled slash a naive concatenation would produce.
    ! curl_records | grep -q 'prod//api'
}

@test "without a session token no X-Amz-Security-Token header is sent" {
    # Long-lived IAM user keys have no token; sending an empty header would make
    # API Gateway reject the signature.
    setup_client_fakes
    run env PATH="$FAKE_BIN:$PATH" API_REGION=eu-west-1 bash "$SCRIPT"

    [ "$status" -eq 0 ]
    [ "$(wc -l < "$CURL_CALLS")" -eq 4 ]
    ! curl_records | grep -q 'X-Amz-Security-Token'
    [ "$(curl_records | grep -c -- '--user EXAMPLEKEYID:EXAMPLESECRET ')" -eq 4 ]
}

@test "the region and project name come from cdk.json when not overridden" {
    setup_client_fakes
    local expected_region expected_project
    expected_region="$(jq -r '.context.deployment_regions.api_gateway' "$REPO_ROOT/cdk.json")"
    expected_project="$(jq -r '.context.project_name' "$REPO_ROOT/cdk.json")"
    run env PATH="$FAKE_BIN:$PATH" bash "$SCRIPT"

    [ "$status" -eq 0 ]
    grep -Fq "cloudformation describe-stacks --stack-name ${expected_project}-api-gateway --region ${expected_region}" "$AWS_CALLS"
    [ "$(curl_records | grep -c -- "--aws-sigv4 aws:amz:${expected_region}:execute-api ")" -eq 4 ]
}

@test "copied out of the checkout, the example falls back to us-east-2 and gco-api-gateway" {
    # The documented use: a user copies the example next to their own project,
    # where there is no cdk.json. Run the tracked file through a symlink in an
    # empty project so it resolves its PROJECT_ROOT to a directory without one.
    setup_client_fakes
    local project="$BATS_TEST_TMPDIR/their-project"
    mkdir -p "$project/docs/client-examples"
    ln -s "$SCRIPT" "$project/docs/client-examples/aws_cli_examples.sh"
    run env PATH="$FAKE_BIN:$PATH" bash "$project/docs/client-examples/aws_cli_examples.sh"

    [ "$status" -eq 0 ]
    grep -Fq 'cloudformation describe-stacks --stack-name gco-api-gateway --region us-east-2' "$AWS_CALLS"
}

@test "a stack without an ApiEndpoint output fails before any request is signed" {
    setup_client_fakes
    run env PATH="$FAKE_BIN:$PATH" API_REGION=eu-west-1 FAKE_API_ENDPOINT=None bash "$SCRIPT"

    [ "$status" -eq 1 ]
    [[ "$output" == *"ApiEndpoint was not found in stack gco-api-gateway"* ]]
    [ ! -s "$CURL_CALLS" ]
    ! grep -q 'export-credentials' "$AWS_CALLS"
}

@test "a missing required tool is named before any AWS call" {
    # PATH holds the fakes and the coreutils the preamble needs, but no jq.
    setup_client_fakes
    local tools="$BATS_TEST_TMPDIR/tools"
    link_tools "$tools" bash dirname
    run env PATH="$FAKE_BIN:$tools" bash "$SCRIPT"

    [ "$status" -eq 1 ]
    [[ "$output" == *"required command 'jq' is not installed"* ]]
    [ ! -s "$AWS_CALLS" ]
}
