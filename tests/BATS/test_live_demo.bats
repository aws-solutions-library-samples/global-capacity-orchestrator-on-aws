#!/usr/bin/env bats
# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for demo/live_demo.sh and demo/lib_demo.sh
# ─────────────────────────────────────────────────────────────────────────────
# These tests source the actual lib_demo.sh library and call its real
# functions, so the tests exercise the same code the demo runs.
#
# Run:  bats tests/BATS/test_live_demo.bats
# ─────────────────────────────────────────────────────────────────────────────

load 'helpers.sh'

SCRIPT="$REPO_ROOT/demo/live_demo.sh"
LIB="$REPO_ROOT/demo/lib_demo.sh"

# The demo downloads its EFS sample into this fixed path (it is what the
# recorded narration shows). The runs below fake that download, so refuse to
# start if the directory already exists — it would be a real one.
DEMO_RESULTS_DIR="/tmp/gco-demo-results"

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
    if [ -e "$DEMO_RESULTS_DIR" ]; then
        echo "refusing to run: $DEMO_RESULTS_DIR already exists (a real demo download?)" >&2
        return 1
    fi
    # Source the real library — all functions below call the actual code.
    # Force no-color mode so output assertions are predictable.
    export TERM=dumb
    source "$LIB"
    setup_colors  # Will set all color vars to "" because TERM=dumb
}

teardown() {
    rm -rf "$DEMO_RESULTS_DIR"
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

# ── Whole runs against faked tools ───────────────────────────────────────────
# live_demo.sh is a narrated walk through a deployment; every command it runs
# is `gco` or `kubectl`. The runs below execute the tracked script from a
# fixture checkout with both faked well enough to answer the demo, scripted
# through environment variables so each branch of the walk can be provoked:
# which features cdk.json enables, whether the cluster answers, how the
# inference pod comes up, and which gco commands fail.

make_demo_fixture() {
    # make_demo_fixture <all|none> — creates $FIXTURE and $FAKE_BIN.
    FIXTURE="$BATS_TEST_TMPDIR/checkout"
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export GCO_CALLS="$BATS_TEST_TMPDIR/gco-calls"
    export KUBECTL_CALLS="$BATS_TEST_TMPDIR/kubectl-calls"
    : > "$GCO_CALLS"
    : > "$KUBECTL_CALLS"
    mkdir -p "$FIXTURE" "$FAKE_BIN"
    local enabled=false
    [ "$1" = "all" ] && enabled=true
    cat > "$FIXTURE/cdk.json" <<JSON
{"context":{"project_name":"gco","deployment_regions":{"regional":["us-east-1"]},
"eks_cluster":{"endpoint_access":"${FIXTURE_ENDPOINT_ACCESS:-PUBLIC}"},
"helm":{"volcano":{"enabled":${enabled}},"kueue":{"enabled":${enabled}},
"yunikorn":{"enabled":${enabled}},"slurm":{"enabled":${enabled}}},
"fsx_lustre":{"enabled":${enabled}},"valkey":{"enabled":${enabled}},
"aurora_pgvector":{"enabled":${enabled}},"vector_store":{"enabled":${enabled}}}}
JSON

    # FAKE_GCO_FAIL: ';'-separated argv prefixes that fail (e.g. "inference
    # deploy;vector search"). Everything else answers like a healthy fleet.
    write_stub "$FAKE_BIN" gco <<'FAKE_GCO'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GCO_CALLS"
fails="${FAKE_GCO_FAIL:-}"
while [ -n "$fails" ]; do
    prefix="${fails%%;*}"
    case "$*" in
        "$prefix"*) echo "fake gco: '$*' failed as scripted" >&2; exit 1 ;;
    esac
    [ "$fails" = "$prefix" ] && break
    fails="${fails#*;}"
done
case "${1:-} ${2:-}" in
    "--version ") echo "gco 7.6.4" ;;
    "stacks list") printf '%s\n' "${FAKE_GCO_STACKS-gco-us-east-1 CREATE_COMPLETE}" ;;
    "inference deploy") echo "endpoint demo-llm registered" ;;
    "inference status") echo "demo-llm: Pending (GPU node provisioning)" ;;
    "inference invoke") echo "1) elastic capacity 2) one API" ;;
    "inference delete") echo "endpoint demo-llm deleted" ;;
    "files download")
        # `gco files download <job> <dest> ...`: the demo cats <dest>/results.json.
        mkdir -p "$4"
        printf '{"status": "ok"}\n' > "$4/results.json"
        ;;
    *) echo "ok: $*" ;;
esac
FAKE_GCO
    # FAKE_NODES: ready | empty | fail | after-setup (fails until the fixture's
    # setup-cluster-access.sh has run). FAKE_LEFTOVER_ROUNDS: how many times the
    # gco-jobs namespace still shows a stale pod. FAKE_OLD_PODS_ROUNDS: how many
    # times a Terminating inference pod from a previous run is still there.
    # FAKE_INFERENCE_POD: comma-separated readiness polls (none|pending|ready),
    # the last entry repeating.
    write_stub "$FAKE_BIN" kubectl <<'FAKE_KUBECTL'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$KUBECTL_CALLS"
count() { grep -cF -- "$1" "$KUBECTL_CALLS"; }
case "$*" in
    "version --client -o json") echo '{"clientVersion":{"gitVersion":"v1.35.0"}}' ;;
    "get nodes --request-timeout=5s")
        case "${FAKE_NODES:-ready}" in
            ready) printf 'NAME STATUS\nnode-1 Ready\n' ;;
            empty) echo "No resources found" ;;
            after-setup)
                if [ -e "$BATS_TEST_TMPDIR/setup-cluster-access.ran" ]; then
                    printf 'NAME STATUS\nnode-1 Ready\n'
                else
                    echo "Unable to connect to the server" >&2; exit 1
                fi
                ;;
            *) echo "Unable to connect to the server" >&2; exit 1 ;;
        esac
        ;;
    "config view --minify -o jsonpath={.clusters[0].cluster.server}") echo "https://expected.eks.example" ;;
    "get pods -n gco-jobs --no-headers")
        if [ "$(count "get pods -n gco-jobs --no-headers")" -le "${FAKE_LEFTOVER_ROUNDS:-0}" ]; then
            echo "stale-job-x7k2 0/1 Terminating 0 9m"
        fi
        ;;
    "get pods -n gco-inference -l app=demo-llm --no-headers")
        if ! grep -q "^inference deploy" "$GCO_CALLS"; then
            # Before the deploy: leftovers from a previous run, if scripted.
            if [ "$(count "get pods -n gco-inference")" -le "${FAKE_OLD_PODS_ROUNDS:-0}" ]; then
                echo "demo-llm-old 0/1 Terminating 0 12m"
            fi
            exit 0
        fi
        # The pre-deploy wait looked FAKE_OLD_PODS_ROUNDS+1 times; the rest
        # are readiness polls, numbered from 0.
        polls="${FAKE_INFERENCE_POD:-ready}"
        poll=$(( $(count "get pods -n gco-inference") - ${FAKE_OLD_PODS_ROUNDS:-0} - 2 ))
        while [ "$poll" -gt 0 ] && [ "${polls#*,}" != "$polls" ]; do
            polls="${polls#*,}"
            poll=$((poll - 1))
        done
        case "${polls%%,*}" in
            ready) echo "demo-llm-7c9d 1/1 Running 0 4m" ;;
            pending) echo "demo-llm-7c9d 0/1 Pending 0 1m" ;;
            *) : ;;
        esac
        ;;
    "get pods"*) echo "example-pod-1 1/1 Running 0 1m" ;;
    "get vcjob"*|"get clusterqueue"*|"get localqueue"*|"get workloads"*) echo "demo-resource Ready" ;;
    "logs"*) echo "fake log line" ;;
    *) : ;;
esac
FAKE_KUBECTL
    write_stub "$FAKE_BIN" aws <<'FAKE_AWS'
#!/usr/bin/env bash
case "${1:-}" in
    eks) echo "https://expected.eks.example" ;;
    *) echo "123456789012" ;;
esac
FAKE_AWS
    stub_noop "$FAKE_BIN" sleep clear
}

run_demo() {
    # run_demo [VAR=value ...] — a fast, non-interactive run from $FIXTURE.
    run env PATH="$FAKE_BIN:$PATH" TERM=xterm COLUMNS=140 \
        GCO_DEMO_FAST=1 GCO_DEMO_NONINTERACTIVE=1 \
        "$@" \
        bash -c 'cd "$1" && exec bash "$2"' _ "$FIXTURE" "$SCRIPT"
}

@test "a full-topology run demonstrates every enabled feature and reports the lifecycle" {
    make_demo_fixture all
    run_demo FAKE_LEFTOVER_ROUNDS=1 FAKE_OLD_PODS_ROUNDS=1 FAKE_INFERENCE_POD=none,pending,ready

    [ "$status" -eq 0 ]
    [[ "$output" == *"Infrastructure deployed (stacks detected)"* ]]
    [[ "$output" == *"kubectl connected to cluster (1 node(s) ready)"* ]]
    [[ "$output" == *"Terminal width: 140 columns"* ]]
    [[ "$output" == *"Terminal may not support colors"* ]]
    [[ "$output" == *"Cleanup complete."* ]]
    [[ "$output" == *"Inference endpoint queued for deployment."* ]]
    local section
    for section in "FLEET OVERVIEW" "CAPACITY DISCOVERY" "VOLCANO" "KUEUE" "YUNIKORN" "SLURM" \
            "FSx FOR LUSTRE" "VALKEY" "AURORA PGVECTOR" "VECTOR STORE" "EFS" "INFERENCE"; do
        [[ "$output" == *"$section"* ]]
    done
    [[ "$output" == *"Demonstrated 4 scheduler(s)"* ]]
    [[ "$output" == *"FSx for Lustre: sub-millisecond latency"* ]]
    [[ "$output" == *"Serverless Valkey: zero management"* ]]
    [[ "$output" == *"Serverless Aurora pgvector"* ]]
    [[ "$output" == *"Globally replicated vector search"* ]]
    [[ "$output" == *'{"status": "ok"}'* ]]
    # The pod was absent, then Pending, then Running: both waiting branches ran.
    [[ "$output" == *"demo-llm: Pending (GPU node provisioning)"* ]]
    [[ "$output" == *"demo-llm-7c9d 0/1 Pending"* ]]
    [[ "$output" == *"Waiting for pod to be ready (attempt 2/50)"* ]]
    [[ "$output" == *"Endpoint deployed, invoked, and torn down — full lifecycle."* ]]
    [[ "$output" == *"Demo Complete"* ]]
    [[ "$output" == *"Skipping cleanup."* ]]
    # Leftovers were force-deleted before the deploy, never after.
    grep -q '^delete pods -n gco-inference -l app=demo-llm --force --grace-period=0$' "$KUBECTL_CALLS"
    grep -q '^inference deploy demo-llm -i vllm/vllm-openai:v0.28.0 --gpu-count 1 --replicas 1 -r us-east-1' "$GCO_CALLS"
    grep -q '^inference delete demo-llm -y$' "$GCO_CALLS"
}

@test "a bare deployment skips every optional section" {
    make_demo_fixture none
    run_demo

    [ "$status" -eq 0 ]
    local section
    for section in "VOLCANO" "KUEUE" "YUNIKORN" "SLURM" "FSx FOR LUSTRE" "VALKEY" "AURORA PGVECTOR" "VECTOR STORE"; do
        [[ "$output" != *"$section —"* ]]
    done
    [[ "$output" != *"Demonstrated"*"scheduler(s)"* ]]
    [[ "$output" == *"EFS — Persistent Shared Storage"* ]]
    [[ "$output" == *"Demo Complete"* ]]
}

@test "the SKIP_* switches remove their sections from the walk and the recap" {
    make_demo_fixture all
    run_demo SKIP_COSTS=1 SKIP_CAPACITY=1 SKIP_SCHEDULERS=1 SKIP_INFERENCE=1

    [ "$status" -eq 0 ]
    [[ "$output" != *"FLEET OVERVIEW"* ]]
    [[ "$output" != *"CAPACITY DISCOVERY"* ]]
    [[ "$output" != *"VOLCANO"* ]]
    [[ "$output" != *"INFERENCE —"* ]]
    [[ "$output" != *"Capacity discovery and auto-region job placement"* ]]
    [[ "$output" != *"Inference endpoint deploy, invoke, and teardown"* ]]
    [[ "$output" == *"FSx for Lustre high-performance storage"* ]]
    # The pre-demo sweep still clears a stale endpoint; nothing is deployed.
    ! grep -q '^inference deploy' "$GCO_CALLS"
}

@test "a live presentation degrades unproven features to warnings and keeps going" {
    make_demo_fixture all
    run_demo FAKE_GCO_FAIL="jobs submit-direct;vector ingest"

    [ "$status" -eq 0 ]
    local label
    for label in "YuniKorn" "Slurm" "FSx for Lustre" "Valkey" "Aurora pgvector" "Vector store"; do
        [[ "$output" == *"${label} did not run: its workload could not be submitted."* ]]
    done
    [[ "$output" != *"Refusing to publish"* ]]
    # No corpus, so no semantic query was attempted.
    ! grep -q '^vector search' "$GCO_CALLS"
    [[ "$output" == *"Demo Complete"* ]]
}

@test "a guarded recording refuses to publish a feature whose workload never ran" {
    make_demo_fixture all
    local manifest label
    while IFS='|' read -r manifest label; do
        : > "$GCO_CALLS"
        : > "$KUBECTL_CALLS"
        run_demo GCO_DEMO_GUARDED_RECORDING=1 FAKE_GCO_FAIL="jobs submit-direct examples/${manifest}"
        echo "case: $manifest"
        [ "$status" -eq 1 ]
        [[ "$output" == *"${label} did not run: its workload could not be submitted."* ]]
        [[ "$output" == *"Refusing to publish a recording that claims an unproven feature."* ]]
        [[ "$output" != *"Demo Complete"* ]]
        # The armed EXIT fallback removed the inference endpoint on the way out.
        [ "$(grep -c '^inference delete demo-llm -y$' "$GCO_CALLS")" -eq 2 ]
    done <<'CASES'
yunikorn-job.yaml|YuniKorn
slurm-cluster-job.yaml|Slurm
fsx-lustre-job.yaml|FSx for Lustre
valkey-cache-job.yaml|Valkey
aurora-pgvector-job.yaml|Aurora pgvector
CASES
}

@test "a guarded recording needs the vector store to ingest and to answer a query" {
    make_demo_fixture all
    run_demo GCO_DEMO_GUARDED_RECORDING=1 FAKE_GCO_FAIL="vector ingest"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Vector store did not run"* ]]
    ! grep -q '^vector search' "$GCO_CALLS"

    : > "$GCO_CALLS"
    : > "$KUBECTL_CALLS"
    run_demo GCO_DEMO_GUARDED_RECORDING=1 FAKE_GCO_FAIL="vector search"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Vector store did not run"* ]]
    grep -q '^vector ingest --demo --wait' "$GCO_CALLS"
    grep -q '^vector search' "$GCO_CALLS"
}

@test "an inference deployment that is never accepted ends the demo after five attempts" {
    make_demo_fixture none
    run_demo FAKE_GCO_FAIL="inference deploy"

    [ "$status" -eq 1 ]
    [ "$(grep -c '^inference deploy' "$GCO_CALLS")" -eq 5 ]
    [[ "$output" == *"fake gco: 'inference deploy"*"failed as scripted"* ]]
    [[ "$output" == *"Inference deployment was not accepted after 5 attempts."* ]]
    [[ "$output" != *"FLEET OVERVIEW"* ]]
}

@test "an endpoint that never becomes ready fails the lifecycle after the bounded wait" {
    make_demo_fixture none
    run_demo FAKE_INFERENCE_POD=pending

    [ "$status" -eq 1 ]
    [[ "$output" == *"Waiting for pod to be ready (attempt 49/50)"* ]]
    [[ "$output" == *"Endpoint did not become Kubernetes-ready within the bounded wait."* ]]
    [[ "$output" == *"Inference generation failed; refusing to publish a false-success recording."* ]]
    ! grep -q '^inference invoke' "$GCO_CALLS"
    grep -q '^inference delete demo-llm -y$' "$GCO_CALLS"
}

@test "a ready pod whose global route never answers is reported, not celebrated" {
    make_demo_fixture none
    run_demo FAKE_GCO_FAIL="inference invoke"

    [ "$status" -eq 1 ]
    [[ "$output" == *"Inference pod is Kubernetes-ready."* ]]
    [[ "$output" == *"The end-to-end inference route did not become ready after 4 attempts."* ]]
    [[ "$output" == *"Inference generation failed"* ]]
    [ "$(grep -c '^inference invoke demo-llm -p Reply with ready. --max-tokens 1$' "$GCO_CALLS")" -eq 4 ]
}

@test "an endpoint that cannot be deleted fails the lifecycle and the exit fallback warns" {
    make_demo_fixture none
    run_demo FAKE_GCO_FAIL="inference delete"

    [ "$status" -eq 1 ]
    [[ "$output" == *"Live LLM response from a GPU that didn't exist minutes ago."* ]]
    [[ "$output" == *"Inference cleanup failed; refusing to publish an incomplete lifecycle recording."* ]]
    [[ "$output" == *"WARNING: inference endpoint 'demo-llm' may still be running."* ]]
    [[ "$output" == *"Run: gco inference delete demo-llm -y"* ]]
}

@test "preflight failures can be forced past interactively and the cleanup prompt honoured" {
    make_demo_fixture none
    # Not a single deployed stack, so preflight fails; the presenter types
    # "force", presses Enter at the two pauses that remain with every optional
    # section skipped, and answers "y" to the cleanup prompt.
    run env PATH="$FAKE_BIN:$PATH" TERM=xterm COLUMNS=140 GCO_DEMO_FAST=1 \
        FAKE_GCO_STACKS="" SKIP_COSTS=1 SKIP_CAPACITY=1 SKIP_SCHEDULERS=1 SKIP_INFERENCE=1 \
        bash -c 'cd "$1" && printf "force\n\n\ny\n" | exec bash "$2"' _ "$FIXTURE" "$SCRIPT"

    [ "$status" -eq 0 ]
    [[ "$output" == *"No deployed stacks detected"* ]]
    [[ "$output" == *"1 check(s) failed. Fix the issues above before demoing."* ]]
    [[ "$output" == *"Continuing despite failures"* ]]
    [[ "$output" == *"Press Enter to continue..."* ]]
    [[ "$output" == *"Demo jobs cleaned up."* ]]
    grep -q '^delete job -n gco-jobs -l project=gco --ignore-not-found=true$' "$KUBECTL_CALLS"
    grep -q '^delete vcjob -n gco-jobs distributed-training --ignore-not-found=true$' "$KUBECTL_CALLS"
    [ ! -e "$DEMO_RESULTS_DIR" ]
}

@test "declining to force past a preflight failure exits" {
    make_demo_fixture none
    run env PATH="$FAKE_BIN:$PATH" TERM=xterm COLUMNS=140 GCO_DEMO_FAST=1 FAKE_GCO_STACKS="" \
        bash -c 'cd "$1" && printf "\n" | exec bash "$2"' _ "$FIXTURE" "$SCRIPT"

    [ "$status" -eq 1 ]
    [[ "$output" == *"Press Enter to exit, or type 'force' to continue anyway:"* ]]
    [[ "$output" != *"Continuing despite failures"* ]]
    ! grep -q '^inference' "$GCO_CALLS"
}

@test "a guarded recording never force-continues a preflight failure" {
    make_demo_fixture none
    run_demo GCO_DEMO_GUARDED_RECORDING=1 FAKE_NODES=fail

    [ "$status" -eq 1 ]
    [[ "$output" == *"kubectl cannot reach the pre-authorized cluster"* ]]
    [[ "$output" == *"auto-setup is disabled"* ]]
    [[ "$output" == *"Guarded recording mode never force-continues preflight failures."* ]]
    ! grep -q '^inference' "$GCO_CALLS"
}

@test "an unreachable cluster is auto-configured through setup-cluster-access.sh when present" {
    make_demo_fixture none
    mkdir -p "$FIXTURE/scripts"
    cat > "$FIXTURE/scripts/setup-cluster-access.sh" <<'FAKE_SETUP'
#!/usr/bin/env bash
printf '%s\n' "$*" > "$BATS_TEST_TMPDIR/setup-cluster-access.ran"
FAKE_SETUP
    run_demo FAKE_NODES=after-setup

    [ "$status" -eq 0 ]
    [[ "$output" == *"Attempting to configure cluster access..."* ]]
    [[ "$output" == *"kubectl connected (auto-configured via setup-cluster-access.sh)"* ]]
    [ "$(cat "$BATS_TEST_TMPDIR/setup-cluster-access.ran")" = "gco-us-east-1 us-east-1" ]
}

@test "an unreachable cluster that auto-configuration cannot fix is a preflight failure" {
    make_demo_fixture none
    mkdir -p "$FIXTURE/scripts"
    printf '#!/usr/bin/env bash\nexit 0\n' > "$FIXTURE/scripts/setup-cluster-access.sh"
    run_demo FAKE_NODES=fail

    # Non-interactive runs force past the failure, so the run itself completes.
    [ "$status" -eq 0 ]
    [[ "$output" == *"Attempting to configure cluster access..."* ]]
    [[ "$output" == *"kubectl cannot reach the cluster"* ]]
    [[ "$output" == *"./scripts/setup-cluster-access.sh gco-us-east-1 us-east-1"* ]]
    [[ "$output" == *"Continuing despite failures"* ]]
}

@test "an unreachable cluster without the setup script names the fix" {
    make_demo_fixture none
    run_demo FAKE_NODES=fail

    [ "$status" -eq 0 ]
    [[ "$output" != *"Attempting to configure cluster access..."* ]]
    [[ "$output" == *"kubectl cannot reach the cluster"* ]]
    [[ "$output" == *"Fix: ./scripts/setup-cluster-access.sh gco-us-east-1 us-east-1"* ]]
}

@test "a scaled-to-zero cluster and a private endpoint are reported, not fatal" {
    FIXTURE_ENDPOINT_ACCESS=PRIVATE make_demo_fixture none
    run_demo FAKE_NODES=empty

    [ "$status" -eq 0 ]
    [[ "$output" == *"EKS endpoint access is PRIVATE"* ]]
    [[ "$output" == *"kubectl connected to cluster (0 nodes — will scale on demand)"* ]]
}

@test "a terminal narrower than 120 columns is a warning graded by how narrow" {
    make_demo_fixture none
    run_demo COLUMNS=100
    [ "$status" -eq 0 ]
    [[ "$output" == *"Terminal width: 100 columns (120+ recommended)"* ]]
    [[ "$output" == *"Widen your terminal for best presentation appearance."* ]]

    : > "$GCO_CALLS"
    : > "$KUBECTL_CALLS"
    run_demo COLUMNS=60
    [ "$status" -eq 0 ]
    [[ "$output" == *"Terminal width: 60 columns (120+ recommended)"* ]]
    [[ "$output" == *"Output may wrap and look messy."* ]]
}

@test "a colour terminal passes the colour check" {
    make_demo_fixture none
    # The check is `[ -t 1 ]`, so stdout has to be a real terminal: run the
    # demo under a pseudo-terminal and relay its exit status.
    run env PATH="$FAKE_BIN:$PATH" TERM=xterm COLUMNS=140 GCO_DEMO_FAST=1 GCO_DEMO_NONINTERACTIVE=1 \
        python3 -c 'import os, pty, sys; sys.exit(os.waitstatus_to_exitcode(pty.spawn(sys.argv[1:])))' \
        bash -c 'cd "$1" && exec bash "$2"' _ "$FIXTURE" "$SCRIPT"

    [ "$status" -eq 0 ]
    [[ "$output" == *"Terminal supports colors"* ]]
    [[ "$output" == *"Demo Complete"* ]]
}

@test "the demo refuses to start outside a checkout" {
    make_demo_fixture none
    rm -f "$FIXTURE/cdk.json"
    run_demo

    [ "$status" -eq 1 ]
    [[ "$output" == *"cdk.json not found"* ]]
    [[ "$output" == *"Cannot continue without cdk.json. Exiting."* ]]
    [ ! -s "$GCO_CALLS" ]
}

@test "the demo refuses to start without jq, gco or kubectl" {
    make_demo_fixture none
    # PATH keeps everything except the three tools; each case then adds back
    # the two it is not testing (the real jq, the fakes for the other two).
    local tools="$BATS_TEST_TMPDIR/tools"
    path_without "$tools" jq gco kubectl
    local missing message present
    while IFS='|' read -r missing message; do
        present="$BATS_TEST_TMPDIR/present-$missing"
        mkdir -p "$present"
        [ "$missing" = "jq" ] || ln -s "$(command -v jq)" "$present/jq"
        [ "$missing" = "gco" ] || ln -s "$FAKE_BIN/gco" "$present/gco"
        [ "$missing" = "kubectl" ] || ln -s "$FAKE_BIN/kubectl" "$present/kubectl"
        run env PATH="$present:$tools" TERM=xterm COLUMNS=140 GCO_DEMO_NONINTERACTIVE=1 \
            bash -c 'cd "$1" && exec bash "$2"' _ "$FIXTURE" "$SCRIPT"
        echo "case: $missing"
        [ "$status" -eq 1 ]
        [[ "$output" == *"${message} not installed"* ]]
        [[ "$output" == *"Cannot continue without"* ]]
    done <<'CASES'
jq|jq
gco|GCO CLI
kubectl|kubectl
CASES
}
