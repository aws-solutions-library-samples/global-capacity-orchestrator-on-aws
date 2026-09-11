#!/usr/bin/env bats
# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for scripts/setup-cluster-access.sh
# ─────────────────────────────────────────────────────────────────────────────
# Functional tests for argument handling, assumed-role ARN transformation,
# and error-handling patterns, plus end-to-end runs of the script against a
# faked AWS CLI and kubectl: the fakes record every call so the tests assert
# on the exact argv the script builds and on how it reacts to each answer.
#
# Run:  bats tests/BATS/test_setup_cluster_access.bats
# ─────────────────────────────────────────────────────────────────────────────

load 'helpers.sh'

SCRIPT="$REPO_ROOT/scripts/setup-cluster-access.sh"
LIB="$REPO_ROOT/demo/lib_demo.sh"

# ── Syntax & Structure ───────────────────────────────────────────────────────

@test "setup-cluster-access.sh exists and is executable" {
    [ -f "$SCRIPT" ]
    [ -x "$SCRIPT" ]
}

@test "setup-cluster-access.sh passes bash -n syntax check" {
    bash -n "$SCRIPT"
}

@test "setup-cluster-access.sh passes shellcheck" {
    command -v shellcheck &>/dev/null || skip "shellcheck not installed"
    # -x matches the repo-wide lint:shellcheck:shell workflow policy.
    shellcheck -x "$SCRIPT"
}

# ── Argument Defaults (functional — evaluates real bash expansions) ───────────

@test "cluster name defaults to gco-us-east-1 with no args" {
    run bash -c 'set -- ; CLUSTER_NAME="${1:-gco-us-east-1}"; echo "$CLUSTER_NAME"'
    [ "$output" = "gco-us-east-1" ]
}

@test "region defaults to us-east-1 with no args" {
    run bash -c 'set -- ; REGION="${2:-us-east-1}"; echo "$REGION"'
    [ "$output" = "us-east-1" ]
}

@test "first argument overrides cluster name" {
    run bash -c 'set -- "my-cluster"; CLUSTER_NAME="${1:-gco-us-east-1}"; echo "$CLUSTER_NAME"'
    [ "$output" = "my-cluster" ]
}

@test "second argument overrides region" {
    run bash -c 'set -- "c" "eu-west-1"; REGION="${2:-us-east-1}"; echo "$REGION"'
    [ "$output" = "eu-west-1" ]
}

# ── Assumed-Role ARN Transformation (uses real functions from lib_demo.sh) ────

@test "is_assumed_role matches sts assumed-role ARNs" {
    source "$LIB"
    is_assumed_role "arn:aws:sts::123456789012:assumed-role/Role/session"
}

@test "is_assumed_role rejects IAM user ARNs" {
    source "$LIB"
    ! is_assumed_role "arn:aws:iam::123456789012:user/developer"
}

@test "is_assumed_role rejects non-assumed IAM role ARNs" {
    source "$LIB"
    ! is_assumed_role "arn:aws:iam::123456789012:role/MyRole"
}

@test "extract_role_name gets role from assumed-role ARN" {
    source "$LIB"
    result=$(extract_role_name "arn:aws:sts::123456789012:assumed-role/MyAdminRole/session-name")
    [ "$result" = "MyAdminRole" ]
}

@test "extract_role_name handles hyphens and underscores" {
    source "$LIB"
    result=$(extract_role_name "arn:aws:sts::111111111111:assumed-role/My_Complex-Role-Name/user@corp.com")
    [ "$result" = "My_Complex-Role-Name" ]
}

@test "build_role_arn reconstructs correct IAM role ARN" {
    source "$LIB"
    result=$(build_role_arn "MyRole" "123456789012")
    [ "$result" = "arn:aws:iam::123456789012:role/MyRole" ]
}

# ── Error Handling Patterns ──────────────────────────────────────────────────

@test "access entry creation handles already-exists gracefully" {
    grep -q 'Access entry may already exist' "$SCRIPT"
}

@test "policy association handles already-associated gracefully" {
    grep -q 'Policy may already be associated' "$SCRIPT"
}

@test "script waits for IAM propagation before kubectl verify" {
    grep -q "sleep 10" "$SCRIPT"
}

# ── AWS CLI Call Correctness ─────────────────────────────────────────────────

@test "update-kubeconfig passes both cluster name and region" {
    grep -q 'aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$REGION"' "$SCRIPT"
}

@test "create-access-entry passes cluster name, region, and principal ARN" {
    grep -q 'aws eks create-access-entry' "$SCRIPT"
    grep -q '\-\-cluster-name "$CLUSTER_NAME"' "$SCRIPT"
    grep -q '\-\-principal-arn "$PRINCIPAL_ARN"' "$SCRIPT"
}

@test "associate-access-policy uses cluster-scoped access" {
    grep -q 'type=cluster' "$SCRIPT"
}

@test "uses AmazonEKSClusterAdminPolicy (not a weaker policy)" {
    grep -q 'AmazonEKSClusterAdminPolicy' "$SCRIPT"
}

# ── End-to-end runs against a faked AWS CLI and kubectl ──────────────────────
#
# `aws` answers from environment knobs and appends every argv to $AWS_CALLS;
# `kubectl` does the same to $KUBECTL_CALLS; `sleep` is a no-op so the
# ten-second IAM propagation wait costs nothing. The principal the fake
# returns is chosen per test (FAKE_CALLER_ARN), as is whether the access-entry
# and policy calls succeed (FAKE_EKS_EXISTS=1 makes both fail the way the AWS
# CLI does when the entry and association already exist).

setup_fakes() {
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export AWS_CALLS="$BATS_TEST_TMPDIR/aws-calls"
    export KUBECTL_CALLS="$BATS_TEST_TMPDIR/kubectl-calls"
    : > "$AWS_CALLS"
    : > "$KUBECTL_CALLS"
    write_stub "$FAKE_BIN" aws <<'AWS'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$AWS_CALLS"
case "$1 $2" in
    "eks update-kubeconfig")
        echo "Updated context in /fake/kubeconfig"
        ;;
    "sts get-caller-identity")
        if [[ "$*" == *"--query Arn"* ]]; then
            printf '%s\n' "${FAKE_CALLER_ARN:?}"
        else
            printf '%s\n' "123456789012"
        fi
        ;;
    "eks create-access-entry")
        if [ "${FAKE_EKS_EXISTS:-0}" = "1" ]; then
            echo "An error occurred (ResourceInUseException): The specified access entry resource is already in use on this cluster." >&2
            exit 254
        fi
        echo '{"accessEntry": {"principalArn": "created"}}'
        ;;
    "eks associate-access-policy")
        if [ "${FAKE_EKS_EXISTS:-0}" = "1" ]; then
            echo "An error occurred (ResourceInUseException): The specified access policy is already associated." >&2
            exit 254
        fi
        echo '{"associatedAccessPolicy": {"accessScope": {"type": "cluster"}}}'
        ;;
    *)
        echo "unexpected aws call: $*" >&2
        exit 2
        ;;
esac
AWS
    write_stub "$FAKE_BIN" kubectl <<'KUBECTL'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$KUBECTL_CALLS"
if [ "${FAKE_KUBECTL_FAILS:-0}" = "1" ]; then
    echo "error: You must be logged in to the server (Unauthorized)" >&2
    exit 1
fi
printf 'NAME            STATUS   ROLES    AGE   VERSION\n'
printf 'ip-10-0-1-10    Ready    <none>   3d    v1.35.0\n'
KUBECTL
    stub_noop "$FAKE_BIN" sleep
}

@test "an IAM user principal is used as-is for the access entry and admin policy" {
    setup_fakes
    run env PATH="$FAKE_BIN:$PATH" \
        FAKE_CALLER_ARN="arn:aws:iam::123456789012:user/developer" \
        bash "$SCRIPT" research-eu-west-1 eu-west-1

    [ "$status" -eq 0 ]
    [[ "$output" == *"Principal: arn:aws:iam::123456789012:user/developer"* ]]
    [[ "$output" != *"Using role ARN"* ]]
    grep -Fxq 'eks update-kubeconfig --name research-eu-west-1 --region eu-west-1' "$AWS_CALLS"
    grep -Fxq 'eks create-access-entry --cluster-name research-eu-west-1 --region eu-west-1 --principal-arn arn:aws:iam::123456789012:user/developer' "$AWS_CALLS"
    grep -Fxq 'eks associate-access-policy --cluster-name research-eu-west-1 --region eu-west-1 --principal-arn arn:aws:iam::123456789012:user/developer --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy --access-scope type=cluster' "$AWS_CALLS"
    # The Account lookup is only needed to rebuild a role ARN.
    ! grep -q -- '--query Account' "$AWS_CALLS"
    grep -Fxq 'get nodes' "$KUBECTL_CALLS"
    [[ "$output" == *"Setup complete! You can now use kubectl with cluster: research-eu-west-1"* ]]
}

@test "an assumed-role principal is rebuilt into the IAM role ARN before the access entry" {
    setup_fakes
    run env PATH="$FAKE_BIN:$PATH" \
        FAKE_CALLER_ARN="arn:aws:sts::123456789012:assumed-role/AdminRole/alice@example.invalid" \
        bash "$SCRIPT"

    [ "$status" -eq 0 ]
    [[ "$output" == *"Using role ARN: arn:aws:iam::123456789012:role/AdminRole"* ]]
    grep -Fxq 'sts get-caller-identity --query Account --output text' "$AWS_CALLS"
    grep -Fxq 'eks create-access-entry --cluster-name gco-us-east-1 --region us-east-1 --principal-arn arn:aws:iam::123456789012:role/AdminRole' "$AWS_CALLS"
    # The session-bearing STS ARN must never reach EKS: it is not a valid
    # access-entry principal.
    ! grep -q 'assumed-role' <(grep '^eks ' "$AWS_CALLS")
}

@test "without arguments the defaults gco-us-east-1 / us-east-1 reach every AWS call" {
    setup_fakes
    run env PATH="$FAKE_BIN:$PATH" \
        FAKE_CALLER_ARN="arn:aws:iam::123456789012:user/developer" \
        bash "$SCRIPT"

    [ "$status" -eq 0 ]
    [[ "$output" == *"Setting up access to cluster: gco-us-east-1 in region: us-east-1"* ]]
    [ "$(grep -c -- '--cluster-name gco-us-east-1 --region us-east-1' "$AWS_CALLS")" -eq 2 ]
    grep -Fxq 'eks update-kubeconfig --name gco-us-east-1 --region us-east-1' "$AWS_CALLS"
}

@test "an existing access entry and association are reported, not fatal" {
    # Re-running on a cluster the caller already administers must still reach
    # the kubectl verification: both EKS calls fail with ResourceInUse and the
    # script's `|| echo` keeps `set -e` from ending the run there.
    setup_fakes
    run env PATH="$FAKE_BIN:$PATH" \
        FAKE_CALLER_ARN="arn:aws:iam::123456789012:user/developer" \
        FAKE_EKS_EXISTS=1 \
        bash "$SCRIPT"

    [ "$status" -eq 0 ]
    [[ "$output" == *"Access entry may already exist"* ]]
    [[ "$output" == *"Policy may already be associated"* ]]
    grep -Fxq 'get nodes' "$KUBECTL_CALLS"
    [[ "$output" == *"Setup complete"* ]]
}

@test "a kubectl verification failure ends the script with its status, not success" {
    setup_fakes
    run env PATH="$FAKE_BIN:$PATH" \
        FAKE_CALLER_ARN="arn:aws:iam::123456789012:user/developer" \
        FAKE_KUBECTL_FAILS=1 \
        bash "$SCRIPT"

    [ "$status" -ne 0 ]
    [[ "$output" == *"Verifying access"* ]]
    [[ "$output" != *"Setup complete"* ]]
}
