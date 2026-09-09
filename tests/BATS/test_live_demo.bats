#!/usr/bin/env bats
# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for demo/live_demo.sh and demo/lib_demo.sh
# ─────────────────────────────────────────────────────────────────────────────
# These tests source the actual lib_demo.sh library and call its real
# functions, so the tests exercise the same code the demo runs.
#
# Run:  bats tests/BATS/test_live_demo.bats
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT="demo/live_demo.sh"
LIB="demo/lib_demo.sh"

# ── Syntax & Structure ───────────────────────────────────────────────────────

@test "live_demo.sh exists and is executable" {
    [ -f "$SCRIPT" ]
    [ -x "$SCRIPT" ]
}

@test "lib_demo.sh exists" {
    [ -f "$LIB" ]
}

@test "live_demo.sh passes bash -n syntax check" {
    bash -n "$SCRIPT"
}

@test "lib_demo.sh passes bash -n syntax check" {
    bash -n "$LIB"
}

@test "live_demo.sh passes shellcheck" {
    command -v shellcheck &>/dev/null || skip "shellcheck not installed"
    # -x follows `source` directives so shellcheck can resolve lib_demo.sh
    # and suppress the SC1091 info warning. Matches the lint workflow.
    shellcheck -x "$SCRIPT"
}

@test "lib_demo.sh passes shellcheck" {
    command -v shellcheck &>/dev/null || skip "shellcheck not installed"
    # -x kept consistent with the sibling shellcheck tests in this repo,
    # including the repo-wide lint:shellcheck:shell job.
    shellcheck -x "$LIB"
}

# ── Source the library for all functional tests ──────────────────────────────

setup() {
    # Source the real library — all functions below call the actual code.
    # Force no-color mode so output assertions are predictable.
    export TERM=dumb
    source "$LIB"
    setup_colors  # Will set all color vars to "" because TERM=dumb
}

# ── Display Helpers (calling real functions from lib_demo.sh) ─────────────────

@test "feature_status returns 'enabled' for true" {
    result=$(feature_status "true")
    [[ "$result" == *"enabled"* ]]
}

@test "feature_status returns 'disabled' for false" {
    result=$(feature_status "false")
    [[ "$result" == *"disabled"* ]]
}

@test "narrate outputs indented text" {
    result=$(narrate "hello world")
    [ "$result" = "  hello world" ]
}

@test "highlight includes arrow marker" {
    result=$(highlight "test item")
    [[ "$result" == *"▸"* ]]
    [[ "$result" == *"test item"* ]]
}

@test "success includes checkmark" {
    result=$(success "it worked")
    [[ "$result" == *"✓"* ]]
    [[ "$result" == *"it worked"* ]]
}

@test "warn includes warning symbol" {
    result=$(warn "something broke")
    [[ "$result" == *"⚠"* ]]
    [[ "$result" == *"something broke"* ]]
}

@test "spacer outputs an empty line" {
    result=$(spacer)
    [ "$result" = "" ]
}

@test "banner contains the title text" {
    result=$(banner "Test Title")
    [[ "$result" == *"Test Title"* ]]
}

@test "section_header contains number and title" {
    result=$(section_header "3" "MY SECTION")
    [[ "$result" == *"[3]"* ]]
    [[ "$result" == *"MY SECTION"* ]]
}

@test "section counter increments correctly with inline arithmetic" {
    run bash -c '
        SECTION=0
        SECTION=$((SECTION + 1)); echo "$SECTION"
        SECTION=$((SECTION + 1)); echo "$SECTION"
        SECTION=$((SECTION + 1)); echo "$SECTION"
    '
    [ "$status" -eq 0 ]
    result_lines=($output)
    [ "${result_lines[0]}" = "1" ]
    [ "${result_lines[1]}" = "2" ]
    [ "${result_lines[2]}" = "3" ]
}

@test "run_cmd propagates the wrapped command status" {
    run run_cmd "bash -c 'exit 42'"
    [ "$status" -eq 42 ]
    [[ "$output" == *"Command exited with code 42"* ]]
}

@test "inference generation wait retries the exact global completion contract" {
    local counter="$BATS_TEST_TMPDIR/inference-attempts"
    local argv_log="$BATS_TEST_TMPDIR/inference-argv"
    printf '0\n' > "$counter"
    gco() {
        local count
        printf '%s\n' "$*" >> "$argv_log"
        count=$(cat "$counter")
        count=$((count + 1))
        printf '%s\n' "$count" > "$counter"
        [ "$count" -ge 2 ]
    }
    sleep() { :; }

    run wait_for_inference_generation demo-llm 4 0

    [ "$status" -eq 0 ]
    [ "$(cat "$counter")" -eq 2 ]
    [ "$(wc -l < "$argv_log" | tr -d ' ')" -eq 2 ]
    while IFS= read -r invocation; do
        [[ "$invocation" == "inference invoke demo-llm -p Reply with ready. --max-tokens 1" ]]
        [[ "$invocation" != *" -r "* ]]
        [[ "$invocation" != *" --region "* ]]
    done < "$argv_log"
}

@test "inference generation wait fails at its bounded attempt limit" {
    local counter="$BATS_TEST_TMPDIR/inference-exhausted-attempts"
    printf '0\n' > "$counter"
    gco() {
        local count
        count=$(cat "$counter")
        printf '%s\n' "$((count + 1))" > "$counter"
        return 1
    }
    sleep() { :; }

    run wait_for_inference_generation demo-llm 3 0

    [ "$status" -ne 0 ]
    [ "$(cat "$counter")" -eq 3 ]
}

# ── Pause Duration Logic (calling real setup_pauses) ──────────────────────────

@test "setup_pauses defaults to 3/5 without GCO_DEMO_FAST" {
    unset GCO_DEMO_FAST
    setup_pauses
    [ "$PAUSE_SHORT" = "3" ]
    [ "$PAUSE_LONG" = "5" ]
}

@test "setup_pauses uses 1/2 with GCO_DEMO_FAST=1" {
    export GCO_DEMO_FAST=1
    setup_pauses
    [ "$PAUSE_SHORT" = "1" ]
    [ "$PAUSE_LONG" = "2" ]
    unset GCO_DEMO_FAST
}

# ── Color Setup (calling real setup_colors) ──────────────────────────────────

@test "setup_colors sets empty strings when TERM=dumb" {
    export TERM=dumb
    setup_colors
    [ "$BOLD" = "" ]
    [ "$RED" = "" ]
    [ "$GREEN" = "" ]
    [ "$RESET" = "" ]
}

# ── Feature Detection (calling real detect_features against cdk.json) ─────────

@test "detect_features sets all scheduler flags from cdk.json" {
    detect_features "cdk.json"
    [ "$VOLCANO_ENABLED" = "true" ] || [ "$VOLCANO_ENABLED" = "false" ]
    [ "$KUEUE_ENABLED" = "true" ] || [ "$KUEUE_ENABLED" = "false" ]
    [ "$YUNIKORN_ENABLED" = "true" ] || [ "$YUNIKORN_ENABLED" = "false" ]
    [ "$SLURM_ENABLED" = "true" ] || [ "$SLURM_ENABLED" = "false" ]
}

@test "detect_features sets storage flags from cdk.json" {
    detect_features "cdk.json"
    [ "$FSX_ENABLED" = "true" ] || [ "$FSX_ENABLED" = "false" ]
    [ "$VALKEY_ENABLED" = "true" ] || [ "$VALKEY_ENABLED" = "false" ]
}

@test "detect_region reads a valid AWS region" {
    unset GCO_DEMO_REGION
    detect_region "cdk.json"
    [[ "$REGION" =~ ^[a-z]{2}-[a-z]+-[0-9]+$ ]]
}

@test "detect_region respects GCO_DEMO_REGION override" {
    export GCO_DEMO_REGION=ap-southeast-1
    detect_region "cdk.json"
    [ "$REGION" = "ap-southeast-1" ]
    unset GCO_DEMO_REGION
}

@test "detect_endpoint_access returns a valid EKS value" {
    detect_endpoint_access "cdk.json"
    [[ "$ENDPOINT_ACCESS" =~ ^(PRIVATE|PUBLIC|PUBLIC_AND_PRIVATE)$ ]]
}

# ── ARN Helpers (calling real functions from lib_demo.sh) ─────────────────────

@test "is_assumed_role matches assumed-role ARNs" {
    is_assumed_role "arn:aws:sts::123456789012:assumed-role/MyRole/session"
}

@test "is_assumed_role rejects IAM user ARNs" {
    ! is_assumed_role "arn:aws:iam::123456789012:user/developer"
}

@test "is_assumed_role rejects IAM role ARNs" {
    ! is_assumed_role "arn:aws:iam::123456789012:role/MyRole"
}

@test "extract_role_name gets role from assumed-role ARN" {
    result=$(extract_role_name "arn:aws:sts::123456789012:assumed-role/MyAdminRole/session")
    [ "$result" = "MyAdminRole" ]
}

@test "extract_role_name handles hyphens and underscores" {
    result=$(extract_role_name "arn:aws:sts::111111111111:assumed-role/My_Complex-Role/user@corp.com")
    [ "$result" = "My_Complex-Role" ]
}

@test "build_role_arn constructs correct IAM role ARN" {
    result=$(build_role_arn "MyRole" "123456789012")
    [ "$result" = "arn:aws:iam::123456789012:role/MyRole" ]
}

# ── Manifest Integrity (validates real files) ─────────────────────────────────

@test "all referenced example manifests exist on disk" {
    for f in \
        examples/volcano-gang-job.yaml \
        examples/kueue-job.yaml \
        examples/yunikorn-job.yaml \
        examples/slurm-cluster-job.yaml \
        examples/fsx-lustre-job.yaml \
        examples/valkey-cache-job.yaml \
        examples/efs-output-job.yaml; do
        [ -f "$f" ]
    done
}

@test "all referenced manifests parse as valid YAML" {
    command -v python3 &>/dev/null || skip "python3 not installed"
    for f in \
        examples/volcano-gang-job.yaml \
        examples/kueue-job.yaml \
        examples/yunikorn-job.yaml \
        examples/slurm-cluster-job.yaml \
        examples/fsx-lustre-job.yaml \
        examples/valkey-cache-job.yaml \
        examples/efs-output-job.yaml; do
        python3 -c "import yaml; list(yaml.safe_load_all(open('$f')))"
    done
}

@test "all referenced manifests target gco-jobs namespace" {
    for f in \
        examples/volcano-gang-job.yaml \
        examples/kueue-job.yaml \
        examples/yunikorn-job.yaml \
        examples/slurm-cluster-job.yaml \
        examples/fsx-lustre-job.yaml \
        examples/valkey-cache-job.yaml \
        examples/efs-output-job.yaml; do
        grep -q "namespace: gco-jobs" "$f"
    done
}

# ── Script Completeness ──────────────────────────────────────────────────────

@test "script contains all expected demo sections" {
    for section in "FLEET OVERVIEW" "CAPACITY DISCOVERY" "VOLCANO" "KUEUE" "YUNIKORN" "SLURM" \
                   "FSx FOR LUSTRE" "VALKEY" "AURORA PGVECTOR" "VECTOR STORE" \
                   "INFERENCE" "EFS" "Demo Complete"; do
        grep -q "$section" "$SCRIPT"
    done
}

@test "fleet overview uses aggregate status and states the MCP policy boundary" {
    grep -q 'gco status --with-costs --with-policy' "$SCRIPT"
    grep -q 'base fleet document.*MCP server' "$SCRIPT"
    grep -q 'Policy comparison is CLI-only' "$SCRIPT"
    run grep -q 'The same document.*MCP server' "$SCRIPT"
    [ "$status" -ne 0 ]
}

@test "inference demo uses the current pinned vLLM image" {
    grep -q 'vllm/vllm-openai:v0.28.0' "$SCRIPT"
    run grep -q 'vllm/vllm-openai:v0.25.1' "$SCRIPT"
    [ "$status" -ne 0 ]
}

@test "inference section fails closed around deploy invoke and delete" {
    grep -q "gco inference deploy" "$SCRIPT"
    grep -q "wait_for_inference_generation" "$SCRIPT"
    grep -q "gco inference invoke" "$SCRIPT"
    grep -q "gco inference delete" "$SCRIPT"
    grep -q "INFERENCE_INVOKE_OK" "$SCRIPT"
    grep -q "INFERENCE_DELETE_OK" "$SCRIPT"
    grep -q "report_inference_lifecycle_result" "$SCRIPT"
    grep -q "cleanup_demo_inference_on_exit" "$SCRIPT"
    run grep -E 'run_cmd "gco inference (invoke|delete).*" \|\| true' "$SCRIPT"
    [ "$status" -ne 0 ]

    local delete_line report_line
    delete_line=$(grep -n 'if run_cmd "gco inference delete' "$SCRIPT" | cut -d: -f1)
    report_line=$(grep -n 'if ! report_inference_lifecycle_result' "$SCRIPT" | cut -d: -f1)
    [ "$delete_line" -lt "$report_line" ]
}

@test "inference polling and global-route warm-up are bounded" {
    grep -q "seq 1 50" "$SCRIPT"
    grep -q 'wait_for_inference_generation "\$INFERENCE_NAME" 4 10' "$SCRIPT"
}

@test "cleanup handles Volcano vcjob custom resource type" {
    grep -q "kubectl delete vcjob" "$SCRIPT"
}

@test "live_demo.sh sources lib_demo.sh" {
    grep -q "source.*lib_demo.sh" "$SCRIPT"
}

@test "fallback inference cleanup warns with an actionable command on failure" {
    gco() { return 1; }

    run cleanup_inference_endpoint demo-llm

    [ "$status" -ne 0 ]
    [[ "$output" == *"endpoint 'demo-llm' may still be running"* ]]
    [[ "$output" == *"gco inference delete demo-llm -y"* ]]
}

@test "inference lifecycle report suppresses success on either failure" {
    run report_inference_lifecycle_result 1 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"Endpoint deployed, invoked, and torn down"* ]]

    run report_inference_lifecycle_result 0 1
    [ "$status" -ne 0 ]
    [[ "$output" != *"Endpoint deployed, invoked, and torn down"* ]]
    [[ "$output" == *"false-success recording"* ]]

    run report_inference_lifecycle_result 1 0
    [ "$status" -ne 0 ]
    [[ "$output" != *"Endpoint deployed, invoked, and torn down"* ]]
    [[ "$output" == *"incomplete lifecycle recording"* ]]
}

@test "vector store section is gated and exercises status, ingest, and search" {
    # The claim is "globally replicated semantic search", so the section must
    # actually ingest a corpus and run a query rather than only report state.
    grep -q 'if \[ "\$VECTOR_STORE_ENABLED" = "true" \]; then' "$SCRIPT"
    grep -q 'gco vector status --output table' "$SCRIPT"
    grep -q 'gco vector ingest --demo --wait --output table' "$SCRIPT"
    grep -q 'gco vector search .* --top-k 5 --output table' "$SCRIPT"
    grep -q 'fi  # VECTOR_STORE' "$SCRIPT"
}

@test "vector store section demonstrates a regional replica read" {
    # Local-replica reads are the whole point of the global table, so the
    # recording must show a query bound to a specific region.
    grep -q 'gco vector search .*--region \$REGION' "$SCRIPT"
}

@test "vector store success claim requires both ingest and search to succeed" {
    # Either half failing makes "ingest once, query in every region" false.
    grep -q 'VECTOR_INGESTED=0' "$SCRIPT"
    grep -q 'VECTOR_SEARCHED=0' "$SCRIPT"
    grep -q 'if \[ "\$VECTOR_INGESTED" -eq 1 \] && \[ "\$VECTOR_SEARCHED" -eq 1 \]; then' "$SCRIPT"
    grep -q 'report_feature_result "\$VECTOR_PROVEN" "Vector store"' "$SCRIPT"
}

@test "vector store appears in the feature summary and the closing recap" {
    grep -q 'feature_status "\$VECTOR_STORE_ENABLED"' "$SCRIPT"
    grep -q 'Globally replicated vector store with semantic search' "$SCRIPT"
}
