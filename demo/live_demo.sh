#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# GCO Live Feature Demonstration
# ─────────────────────────────────────────────────────────────────────────────
# This script is designed to run in a visible terminal during a live
# presentation. It walks through GCO's core capabilities automatically,
# with narrated output and pauses between sections so the audience can
# follow along.
#
# The script reads cdk.json to detect which optional features (schedulers,
# FSx, Valkey) are enabled and only demos what's actually deployed.
#
# Usage:
#   bash demo/live_demo.sh                              # Standard run
#   GCO_DEMO_REGION=us-west-2 bash demo/live_demo.sh    # Override region
#   GCO_DEMO_FAST=1 bash demo/live_demo.sh              # Shorter pauses
#   SKIP_COSTS=1 bash demo/live_demo.sh                 # Skip cost section
#   SKIP_SCHEDULERS=1 bash demo/live_demo.sh            # Skip scheduler demos
#
# See demo/LIVE_DEMO.md for full documentation and maintenance guide.
# ─────────────────────────────────────────────────────────────────────────────

# Exit immediately on errors, treat unset variables as errors, and propagate
# failures through pipes (e.g., "cmd | sed" fails if cmd fails).
set -euo pipefail

# ── Load Shared Library ──────────────────────────────────────────────────────
# All helper functions (colors, display, feature detection, ARN helpers) live
# in lib_demo.sh so they can be shared with record_demo.sh and tested
# directly by BATS without duplication.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=demo/lib_demo.sh
source "${SCRIPT_DIR}/lib_demo.sh"

# Initialize colors and pause durations.
setup_colors
setup_pauses

# Longer wait for pods to pull images and complete (not affected by FAST mode
# because pods need real time regardless of presentation speed).
WAIT_FOR_POD="${GCO_DEMO_FAST:+15}"
WAIT_FOR_POD="${WAIT_FOR_POD:-30}"

# ── Preflight Validation ─────────────────────────────────────────────────────
# Before the demo starts, we automatically check every prerequisite.
# This prevents embarrassing failures mid-presentation. Each check prints
# a pass/fail/warn line, and at the end we show a summary. If anything
# critical failed, the presenter can bail out or type "force" to continue.

CDK_JSON="cdk.json"

# Counters for the summary line at the end of preflight.
PREFLIGHT_PASS=0
PREFLIGHT_FAIL=0
PREFLIGHT_WARN=0

# preflight_pass: Green checkmark — this prerequisite is satisfied.
preflight_pass() {
    echo "  ${GREEN}${BOLD}✓${RESET} $1"
    PREFLIGHT_PASS=$((PREFLIGHT_PASS + 1))
}

# preflight_fail: Red X — this prerequisite is missing. Second arg is the fix.
preflight_fail() {
    echo "  ${RED}${BOLD}✗${RESET} $1"
    echo "    ${DIM}Fix: $2${RESET}"
    PREFLIGHT_FAIL=$((PREFLIGHT_FAIL + 1))
}

# preflight_warn: Yellow bang — not ideal but won't block the demo.
preflight_warn() {
    echo "  ${YELLOW}${BOLD}!${RESET} $1"
    echo "    ${DIM}$2${RESET}"
    PREFLIGHT_WARN=$((PREFLIGHT_WARN + 1))
}

# Clear the screen for a clean start.
clear

banner "GCO — Global Capacity Orchestrator on AWS"

echo "  ${BOLD}Preflight Check${RESET}"
narrate "Validating environment before starting the demo..."
spacer

# ── Check 1: cdk.json exists ────────────────────────────────────────────────
# cdk.json is the project config file. If it's missing, we're not in the
# repo root and nothing else will work.
if [ -f "$CDK_JSON" ]; then
    preflight_pass "cdk.json found"
else
    preflight_fail "cdk.json not found" "Run this script from the repo root"
    echo ""
    echo "  ${RED}Cannot continue without cdk.json. Exiting.${RESET}"
    exit 1
fi

# ── Check 2: jq installed ───────────────────────────────────────────────────
# jq is used to parse cdk.json and detect which features are enabled.
if command -v jq &>/dev/null; then
    preflight_pass "jq installed ($(jq --version 2>&1))"
else
    preflight_fail "jq not installed" "brew install jq  (macOS) or  apt install jq  (Linux)"
    echo ""
    echo "  ${RED}Cannot continue without jq. Exiting.${RESET}"
    exit 1
fi

# ── Check 3: GCO CLI installed ──────────────────────────────────────────────
# The gco CLI is the main interface we demo. Without it, there's no demo.
if command -v gco &>/dev/null; then
    GCO_VER=$(gco --version 2>&1 | head -1)
    preflight_pass "GCO CLI installed ($GCO_VER)"
else
    preflight_fail "GCO CLI not installed" "pipx install -e .  (from repo root)"
    echo ""
    echo "  ${RED}Cannot continue without gco CLI. Exiting.${RESET}"
    exit 1
fi

# ── Check 4: kubectl installed ──────────────────────────────────────────────
# kubectl is needed to watch pods, get logs, and interact with the cluster
# during the scheduler and storage demos.
if command -v kubectl &>/dev/null; then
    KUBECTL_VER=$(kubectl version --client -o json 2>/dev/null \
        | jq -r '.clientVersion.gitVersion // "unknown"' 2>/dev/null || echo "unknown")
    preflight_pass "kubectl installed ($KUBECTL_VER)"
else
    preflight_fail "kubectl not installed" "https://kubernetes.io/docs/tasks/tools/"
    echo ""
    echo "  ${RED}Cannot continue without kubectl. Exiting.${RESET}"
    exit 1
fi

# ── Read config values needed for remaining checks ──────────────────────────
# Uses library functions for region and endpoint detection.
detect_region "$CDK_JSON"
detect_endpoint_access "$CDK_JSON"

# ── Check 5: Infrastructure deployed ────────────────────────────────────────
# Verify that GCO stacks have been deployed. We check the output of
# "gco stacks list" for known stack name patterns.
STACK_CHECK=$(gco stacks list 2>&1 || true)
if echo "$STACK_CHECK" | grep -qi \
    "gco-.*east\|gco-.*west\|gco-.*eu\|deployed\|CREATE_COMPLETE\|UPDATE_COMPLETE"; then
    preflight_pass "Infrastructure deployed (stacks detected)"
else
    preflight_fail "No deployed stacks detected" "gco stacks deploy-all -y"
fi

# ── Check 6: EKS endpoint access mode ───────────────────────────────────────
# For the demo, we need kubectl to reach the EKS API server from the
# presenter's laptop. This requires PUBLIC or PUBLIC_AND_PRIVATE mode.
# PRIVATE mode means kubectl only works from inside the VPC.
if [ "$ENDPOINT_ACCESS" = "PUBLIC_AND_PRIVATE" ] || [ "$ENDPOINT_ACCESS" = "PUBLIC" ]; then
    preflight_pass "EKS endpoint access: $ENDPOINT_ACCESS"
else
    preflight_warn "EKS endpoint access is $ENDPOINT_ACCESS" \
        "kubectl may not work from this machine. Set to PUBLIC_AND_PRIVATE in cdk.json and redeploy."
fi

# ── Check 7: kubectl can reach the cluster ──────────────────────────────────
# Actually try to talk to the cluster. If it fails, we attempt to auto-
# configure access using the setup script.
KUBECTL_TEST=$(kubectl get nodes --request-timeout=5s 2>&1 || true)
if echo "$KUBECTL_TEST" | grep -qiE "NAME|Ready|STATUS"; then
    # Cluster responded and has nodes
    NODE_COUNT=$(echo "$KUBECTL_TEST" | grep -c "Ready" 2>/dev/null || echo "0")
    preflight_pass "kubectl connected to cluster ($NODE_COUNT node(s) ready)"
elif echo "$KUBECTL_TEST" | grep -qi "no resources found"; then
    # Cluster responded but has zero nodes (normal for scale-to-zero)
    preflight_pass "kubectl connected to cluster (0 nodes — will scale on demand)"
else
    # kubectl can't reach the cluster — try auto-configuring
    if [ -f "./scripts/setup-cluster-access.sh" ]; then
        narrate "  Attempting to configure cluster access..."
        bash ./scripts/setup-cluster-access.sh "gco-$REGION" "$REGION" 2>&1 || true
        KUBECTL_RETRY=$(kubectl get nodes --request-timeout=5s 2>&1 || true)
        if echo "$KUBECTL_RETRY" | grep -qiE "NAME|Ready|no resources found"; then
            preflight_pass "kubectl connected (auto-configured via setup-cluster-access.sh)"
        else
            preflight_fail "kubectl cannot reach the cluster" \
                "./scripts/setup-cluster-access.sh gco-$REGION $REGION"
        fi
    else
        preflight_fail "kubectl cannot reach the cluster" \
            "./scripts/setup-cluster-access.sh gco-$REGION $REGION"
    fi
fi

# ── Check 8: Terminal width ─────────────────────────────────────────────────
# The demo output looks best at 120+ columns. Narrower terminals cause
# wrapping that makes the output harder to read for the audience.
# Check COLUMNS env var first (set by asciinema), then fall back to tput.
TERM_COLS="${COLUMNS:-$(tput cols 2>/dev/null || echo "80")}"
if [ "$TERM_COLS" -ge 120 ]; then
    preflight_pass "Terminal width: ${TERM_COLS} columns"
elif [ "$TERM_COLS" -ge 90 ]; then
    preflight_warn "Terminal width: ${TERM_COLS} columns (120+ recommended)" \
        "Widen your terminal for best presentation appearance."
else
    preflight_warn "Terminal width: ${TERM_COLS} columns (120+ recommended)" \
        "Output may wrap and look messy. Widen your terminal window."
fi

# ── Check 9: Color support ──────────────────────────────────────────────────
# Verify the terminal supports colors. Without colors the demo still works
# but looks much less polished.
if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
    preflight_pass "Terminal supports colors"
else
    preflight_warn "Terminal may not support colors" \
        "Try: TERM=xterm-256color bash demo/live_demo.sh"
fi

# ── Read feature flags from cdk.json ────────────────────────────────────────
# Uses detect_features() from lib_demo.sh to set the global flag variables.
detect_features "$CDK_JSON"
detect_region "$CDK_JSON"

# ── Preflight Summary ───────────────────────────────────────────────────────
# Show the pass/fail/warn totals. If anything critical failed, give the
# presenter a chance to bail out or force-continue.

spacer
echo "  ${DIM}──────────────────────────────────────────────────────────────${RESET}"
echo "  ${BOLD}Results:${RESET}  ${GREEN}${PREFLIGHT_PASS} passed${RESET}  ${RED}${PREFLIGHT_FAIL} failed${RESET}  ${YELLOW}${PREFLIGHT_WARN} warnings${RESET}"
echo "  ${DIM}──────────────────────────────────────────────────────────────${RESET}"

if [ "$PREFLIGHT_FAIL" -gt 0 ]; then
    spacer
    echo "  ${RED}${BOLD}$PREFLIGHT_FAIL check(s) failed. Fix the issues above before demoing.${RESET}"
    spacer
    echo "  ${DIM}Press Enter to exit, or type 'force' to continue anyway:${RESET}"
    if [ "${GCO_DEMO_NONINTERACTIVE:-}" = "1" ]; then
        force_input="force"
    else
        read -r force_input
    fi
    if [ "$force_input" != "force" ]; then
        exit 1
    fi
    warn "Continuing despite failures — some demo sections may break."
fi

# ── Feature Summary ──────────────────────────────────────────────────────────
# Show the audience which features are enabled so they know what to expect.

spacer
echo "  ${BOLD}One API. Every Accelerator. Any Region.${RESET}"
spacer
narrate "This live demonstration walks through GCO's core capabilities."
narrate "The script auto-detects which features are enabled in your deployment."
spacer
echo "  ${BOLD}Region:${RESET}          $REGION"
echo "  ${BOLD}Volcano:${RESET}         $(feature_status "$VOLCANO_ENABLED")"
echo "  ${BOLD}Kueue:${RESET}           $(feature_status "$KUEUE_ENABLED")"
echo "  ${BOLD}YuniKorn:${RESET}        $(feature_status "$YUNIKORN_ENABLED")"
echo "  ${BOLD}Slurm:${RESET}           $(feature_status "$SLURM_ENABLED")"
echo "  ${BOLD}FSx Lustre:${RESET}      $(feature_status "$FSX_ENABLED")"
echo "  ${BOLD}Valkey:${RESET}          $(feature_status "$VALKEY_ENABLED")"
echo "  ${BOLD}Aurora pgvector:${RESET} $(feature_status "$AURORA_PGVECTOR_ENABLED")"
spacer

pause_for_audience

# ── Pre-Demo Cleanup ─────────────────────────────────────────────────────────
# Delete leftover jobs from previous demo runs and wait for their pods to
# disappear. Volcano's webhook rejects updates to existing jobs, so we need
# a clean slate. Stale `Terminating` pods also count against the GPU/memory
# resource quota until they're fully gone — skipping the wait makes the
# next Kueue or Volcano submit fail with a quota error. Runs silently.
narrate "Cleaning up any leftover jobs from previous runs..."
kubectl delete jobs --all -n gco-jobs --ignore-not-found=true >/dev/null 2>&1 || true
kubectl delete vcjob --all -n gco-jobs --ignore-not-found=true >/dev/null 2>&1 || true
gco inference delete demo-llm -y >/dev/null 2>&1 || true

# Wait up to 30s for the job-owned pods to actually disappear. Without this,
# a fresh submit can hit "forbidden: exceeded quota" because the old pod's
# GPU/CPU/memory requests are still reserved during the termination window.
#
# Note on the pipeline shape: ``grep -c`` still prints ``0`` to stdout when
# there are no matches, but exits 1. Under ``set -euo pipefail`` that would
# kill the script silently right after "Cleaning up any leftover jobs".
# Running the substitution inside ``|| true`` neutralizes the exit code
# without doubling-up the output the way ``|| echo 0`` would.
for _ in $(seq 1 30); do
    LEFTOVER=$(
        { kubectl get pods -n gco-jobs --no-headers 2>/dev/null \
            | grep -cEv '^(gco-|slinky-)'; } || true
    )
    if [ "${LEFTOVER:-0}" -eq 0 ]; then
        break
    fi
    sleep 1
done
success "Cleanup complete."
spacer

# ── Pre-Deploy Inference (background) ────────────────────────────────────────
# Deploy the inference endpoint now so the GPU node provisions while we demo
# costs, capacity, schedulers, and storage. By the time we reach the inference
# section, the model should be loaded and ready to serve.
if [ "${SKIP_INFERENCE:-}" != "1" ]; then
    INFERENCE_NAME="demo-llm"
    # Wait for any leftover pods from previous runs to fully terminate
    narrate "Waiting for previous inference pods to terminate..."
    for _ in $(seq 1 20); do
        OLD_PODS=$(kubectl get pods -n gco-inference -l app="$INFERENCE_NAME" --no-headers 2>/dev/null || true)
        if [ -z "$OLD_PODS" ]; then
            break
        fi
        # Force-delete stuck Terminating pods after a few attempts
        if echo "$OLD_PODS" | grep -q "Terminating"; then
            kubectl delete pods -n gco-inference -l app="$INFERENCE_NAME" --force --grace-period=0 >/dev/null 2>&1 || true
        fi
        sleep 3
    done
    narrate "Pre-deploying inference endpoint (GPU will provision in background)..."
    # Retry deploy in case the previous endpoint hasn't been fully cleaned up yet
    for _ in $(seq 1 5); do
        DEPLOY_OUTPUT=$(gco inference deploy "$INFERENCE_NAME" -i vllm/vllm-openai:v0.22.0 \
            --gpu-count 1 --replicas 1 -r "$REGION" \
            --extra-args '--model' --extra-args 'facebook/opt-125m' \
            2>&1 || true)
        if echo "$DEPLOY_OUTPUT" | grep -qi "registered\|success"; then
            break
        fi
        sleep 5
    done
    success "Inference endpoint queued for deployment."
    spacer
fi

# ═════════════════════════════════════════════════════════════════════════════
# SECTION: Cost Visibility
# ═════════════════════════════════════════════════════════════════════════════
# This section always runs (unless SKIP_COSTS=1). It shows the audience that
# GCO has built-in cost tracking — no separate tool needed.

if [ "${SKIP_COSTS:-}" != "1" ]; then

SECTION=$((SECTION + 1)); section_header "$SECTION" "COST VISIBILITY" "$GREEN"

narrate "Before we touch any workloads, let's see what the platform costs."
narrate "GCO tracks spend by service, region, and day — all from the CLI."
spacer

highlight "Total spend by AWS service"
run_cmd "gco costs summary --days 7"
sleep "$PAUSE_SHORT"

highlight "Where is the money going geographically?"
run_cmd "gco costs regions --days 7"
sleep "$PAUSE_SHORT"

highlight "Daily cost trend with inline chart"
run_cmd "gco costs trend --days 7"
sleep "$PAUSE_SHORT"

highlight "What are running workloads costing right now?"
run_cmd "gco costs workloads" || true
sleep "$PAUSE_SHORT"

success "Full cost visibility without leaving the terminal."
narrate "This data comes from AWS Cost Explorer, filtered by GCO resource tags."

pause_for_audience

fi  # SKIP_COSTS

# ═════════════════════════════════════════════════════════════════════════════
# SECTION: Capacity Discovery
# ═════════════════════════════════════════════════════════════════════════════
# Shows how GCO finds GPU capacity across regions and routes jobs to where
# resources are actually available.

if [ "${SKIP_CAPACITY:-}" != "1" ]; then

SECTION=$((SECTION + 1)); section_header "$SECTION" "CAPACITY DISCOVERY — Find GPUs Across Regions" "$GREEN"

narrate "GPU availability varies by region and changes constantly."
narrate "GCO checks Spot Placement Scores and instance availability"
narrate "across all configured regions to find where GPUs are right now."
spacer

highlight "Check GPU availability in a specific region"
run_cmd "gco capacity check --instance-type g4dn.xlarge --region $REGION" || true
sleep "$PAUSE_SHORT"

highlight "Find the best region for GPU workloads"
run_cmd "gco capacity recommend-region --gpu" || true
sleep "$PAUSE_SHORT"

highlight "Submit a job with automatic region selection"
narrate "The CLI analyzes capacity across all regions, picks the best one,"
narrate "and places the job on that region's SQS queue automatically."
run_cmd "gco jobs submit-sqs examples/simple-job.yaml --auto-region" || true
sleep "$PAUSE_SHORT"

highlight "Check the SQS queue status across all regions"
run_cmd "gco jobs queue-status --all-regions" || true
sleep "$PAUSE_SHORT"

success "Capacity-aware job placement without manual region selection."

pause_for_audience

fi  # SKIP_CAPACITY

# ═════════════════════════════════════════════════════════════════════════════
# SECTION: Schedulers
# ═════════════════════════════════════════════════════════════════════════════
# GCO supports multiple Kubernetes schedulers simultaneously. Each one is
# opt-in via cdk.json. We only demo the ones that are actually enabled.
# This section is skipped entirely if SKIP_SCHEDULERS=1.

if [ "${SKIP_SCHEDULERS:-}" != "1" ]; then

# Track how many schedulers we demo for the summary at the end.
SCHEDULER_COUNT=0

# ── Volcano ──────────────────────────────────────────────────────────────────
# Volcano is a CNCF batch scheduler for Kubernetes. Its main feature is
# "gang scheduling" — all pods in a distributed training job must be
# schedulable at the same time, or none of them start. This prevents
# deadlocks in multi-node training.

if [ "$VOLCANO_ENABLED" = "true" ]; then

SECTION=$((SECTION + 1)); section_header "$SECTION" "VOLCANO — Gang Scheduling for Distributed Training" "$MAGENTA"

narrate "Volcano is a Kubernetes-native batch scheduler built for AI/ML."
narrate "Its killer feature: gang scheduling — all pods in a distributed"
narrate "training job start together, or none of them start at all."
narrate "This prevents deadlocks where half the workers are waiting forever."
spacer

highlight "Submitting a Volcano gang-scheduled job (1 master + 2 workers)"
run_cmd "gco jobs submit-direct examples/volcano-gang-job.yaml -r $REGION -n gco-jobs" || true
sleep "$PAUSE_SHORT"

narrate "Volcano ensures all 3 pods are co-scheduled atomically."
narrate "Let's watch them come up together..."
spacer

highlight "Checking job status"
countdown "Waiting for pods to schedule" "$WAIT_FOR_POD"
run_cmd "kubectl get pods -n gco-jobs -l volcano.sh/job-name=distributed-training --no-headers 2>/dev/null || echo '  (pods not yet visible — node provisioning in progress)'"

highlight "Volcano job status"
run_cmd "kubectl get vcjob -n gco-jobs --no-headers 2>/dev/null || echo '  (checking Volcano job status...)'"

success "Gang scheduling ensures distributed training jobs don't deadlock."
# Release resource-quota reservations held by this job's pods so the next
# scheduler section doesn't hit "exceeded quota" on submit.
kubectl delete vcjob distributed-training -n gco-jobs --ignore-not-found=true >/dev/null 2>&1 || true
SCHEDULER_COUNT=$((SCHEDULER_COUNT + 1))

pause_for_audience

fi  # VOLCANO

# ── Kueue ────────────────────────────────────────────────────────────────────
# Kueue is the Kubernetes-native job queueing system (SIG Scheduling).
# It manages resource quotas per team/namespace and holds jobs in a queue
# until the cluster has enough resources to run them.

if [ "$KUEUE_ENABLED" = "true" ]; then

SECTION=$((SECTION + 1)); section_header "$SECTION" "KUEUE — Quota-Based Job Queueing" "$MAGENTA"

narrate "Kueue is the Kubernetes-native job queueing system."
narrate "It manages resource quotas, fair-sharing between teams, and"
narrate "holds jobs in a queue until cluster resources are available."
narrate "Think of it as a resource-aware admission controller for batch jobs."
spacer

highlight "Submitting a Kueue-managed job"
run_cmd "gco jobs submit-direct examples/kueue-job.yaml -r $REGION -n gco-jobs" || true
sleep "$PAUSE_SHORT"

highlight "Checking Kueue queue status"
run_cmd "kubectl get clusterqueue --no-headers 2>/dev/null || echo '  (ClusterQueue not yet created — will be created by the manifest)'"
run_cmd "kubectl get localqueue -n gco-jobs --no-headers 2>/dev/null || echo '  (LocalQueue not yet created)'"

countdown "Waiting for workload admission" "$PAUSE_SHORT"

highlight "Kueue workloads (jobs waiting or admitted)"
run_cmd "kubectl get workloads -n gco-jobs --no-headers 2>/dev/null || echo '  (no workloads yet)'"

success "Kueue prevents resource overcommit and enforces team quotas."
# Release resource-quota reservations from these jobs before the next section.
kubectl delete job kueue-sample-job kueue-gpu-job -n gco-jobs --ignore-not-found=true >/dev/null 2>&1 || true
SCHEDULER_COUNT=$((SCHEDULER_COUNT + 1))

pause_for_audience

fi  # KUEUE

# ── YuniKorn ─────────────────────────────────────────────────────────────────
# Apache YuniKorn provides hierarchical queues and fair-sharing for
# multi-tenant clusters. Teams get guaranteed resource shares with the
# ability to borrow unused capacity from other teams.

if [ "$YUNIKORN_ENABLED" = "true" ]; then

SECTION=$((SECTION + 1)); section_header "$SECTION" "YUNIKORN — App-Aware Fair Scheduling" "$MAGENTA"

narrate "Apache YuniKorn brings hierarchical queues and fair-sharing"
narrate "to Kubernetes. It's designed for multi-tenant clusters where"
narrate "multiple teams compete for GPU resources."
narrate "YuniKorn also supports gang scheduling and preemption."
spacer

highlight "Submitting a YuniKorn-scheduled job"
run_cmd "gco jobs submit-direct examples/yunikorn-job.yaml -r $REGION -n gco-jobs" || true
sleep "$PAUSE_SHORT"

highlight "Checking YuniKorn pod scheduling"
countdown "Waiting for YuniKorn to place pods" "$PAUSE_SHORT"
run_cmd "kubectl get pods -n gco-jobs -l app=yunikorn-demo --no-headers 2>/dev/null || echo '  (pods scheduling...)'"

success "YuniKorn provides enterprise-grade multi-tenant scheduling."
# Release resource-quota reservations from these jobs before the next section.
kubectl delete job yunikorn-sample-job yunikorn-gpu-job yunikorn-gang-job -n gco-jobs --ignore-not-found=true >/dev/null 2>&1 || true
SCHEDULER_COUNT=$((SCHEDULER_COUNT + 1))

pause_for_audience

fi  # YUNIKORN

# ── Slurm ────────────────────────────────────────────────────────────────────
# The Slinky Slurm Operator runs a full Slurm cluster inside Kubernetes.
# This lets teams with existing HPC workflows (sbatch scripts, etc.) run
# them on GCO without modification.

if [ "$SLURM_ENABLED" = "true" ]; then

SECTION=$((SECTION + 1)); section_header "$SECTION" "SLURM — HPC Batch Scheduling on Kubernetes" "$MAGENTA"

narrate "For teams coming from traditional HPC, GCO includes the Slinky"
narrate "Slurm Operator. It runs a full Slurm cluster inside Kubernetes,"
narrate "so existing sbatch scripts and workflows work unchanged."
narrate "This bridges the gap between HPC and cloud-native."
spacer

highlight "Submitting a Slurm batch job via Kubernetes"
run_cmd "gco jobs submit-direct examples/slurm-cluster-job.yaml -r $REGION -n gco-jobs" || true
sleep "$PAUSE_SHORT"

highlight "Checking Slurm job pod"
wait_for_job "slurm-test" "gco-jobs"
run_cmd "kubectl get pods -n gco-jobs -l job-name=slurm-test --no-headers 2>/dev/null || echo '  (Slurm job pod starting...)'"

highlight "Tailing Slurm job logs"
run_cmd "kubectl logs job/slurm-test -n gco-jobs --all-containers=true --tail=20 2>/dev/null || kubectl logs -n gco-jobs -l job-name=slurm-test --all-containers=true --tail=20 2>/dev/null || echo '  (no logs yet)'"

success "Existing HPC workflows run on Kubernetes without modification."
# Release resource-quota reservations so FSx / Valkey / EFS sections don't
# hit quota errors. slurm-test itself goes away quickly; we also clean up
# any Slurm-operator-owned workload pods that were spawned for this job.
kubectl delete job slurm-test -n gco-jobs --ignore-not-found=true >/dev/null 2>&1 || true
SCHEDULER_COUNT=$((SCHEDULER_COUNT + 1))

pause_for_audience

fi  # SLURM

# ── Scheduler Summary ────────────────────────────────────────────────────────

if [ "$SCHEDULER_COUNT" -gt 0 ]; then
    spacer
    echo "  ${GREEN}${BOLD}Demonstrated $SCHEDULER_COUNT scheduler(s) — plus KEDA running the SQS queue processor.${RESET}"
    narrate "GCO supports 5 schedulers simultaneously — pick the right"
    narrate "tool for each workload type, all on the same cluster."
    spacer
fi

fi  # SKIP_SCHEDULERS

# ═════════════════════════════════════════════════════════════════════════════
# SECTION: FSx for Lustre
# ═════════════════════════════════════════════════════════════════════════════
# FSx for Lustre is a high-performance parallel file system. It provides
# hundreds of GB/s of throughput with sub-millisecond latency — critical
# for ML training on large datasets. This section only runs if FSx is
# enabled in cdk.json.

if [ "$FSX_ENABLED" = "true" ]; then

SECTION=$((SECTION + 1)); section_header "$SECTION" "FSx FOR LUSTRE — High-Performance Scratch Storage" "$BLUE"

narrate "ML training on large datasets needs serious I/O throughput."
narrate "FSx for Lustre provides hundreds of GB/s of throughput with"
narrate "sub-millisecond latency — purpose-built for HPC and ML."
narrate "GCO provisions it automatically and mounts it into every cluster."
spacer

highlight "Submitting a job that exercises FSx Lustre storage"
run_cmd "gco jobs submit-direct examples/fsx-lustre-job.yaml -r $REGION -n gco-jobs" || true

highlight "Watching the FSx job"
wait_for_job "fsx-lustre-example" "gco-jobs"
run_cmd "kubectl get pods -n gco-jobs -l example=fsx-lustre --no-headers 2>/dev/null || echo '  (pod scheduling...)'"

spacer
narrate "The job writes 10 MB of simulated training data, saves a checkpoint,"
narrate "reads it all back, and reports throughput numbers."
spacer

highlight "Checking job logs for I/O performance"
run_cmd "kubectl logs job/fsx-lustre-example -n gco-jobs --all-containers=true --tail=30 2>/dev/null || kubectl logs -n gco-jobs -l example=fsx-lustre --all-containers=true --tail=30 2>/dev/null || echo '  (no logs yet)'"

success "FSx for Lustre: sub-millisecond latency, hundreds of GB/s throughput."
# Release resource-quota reservations before Valkey/inference/EFS sections.
kubectl delete job fsx-lustre-example -n gco-jobs --ignore-not-found=true >/dev/null 2>&1 || true
narrate "Compare: EFS tops out around 10 GB/s. For large-scale training,"
narrate "FSx is the difference between hours and minutes."

pause_for_audience

fi  # FSX

# ═════════════════════════════════════════════════════════════════════════════
# SECTION: Valkey Cache
# ═════════════════════════════════════════════════════════════════════════════
# Valkey is the open-source successor to Redis. GCO deploys it as a
# serverless cache in each region. Common uses: prompt caching (saves
# 30-50% on inference costs), feature stores, and session state.

if [ "$VALKEY_ENABLED" = "true" ]; then

SECTION=$((SECTION + 1)); section_header "$SECTION" "VALKEY — Serverless In-Memory Cache" "$BLUE"

narrate "Valkey (the open-source Redis successor) runs as a serverless"
narrate "cache in each region. Use it for prompt caching, feature stores,"
narrate "session state, or any low-latency K/V access from your jobs."
narrate "The endpoint is injected automatically — no config needed in manifests."
spacer

highlight "Submitting a job that exercises the Valkey cache"
run_cmd "gco jobs submit-direct examples/valkey-cache-job.yaml -r $REGION -n gco-jobs" || true

highlight "Watching the Valkey job"
wait_for_job "valkey-cache-example" "gco-jobs"
run_cmd "kubectl get pods -n gco-jobs -l app=valkey-cache-example --no-headers 2>/dev/null || echo '  (pod scheduling...)'"

highlight "Valkey job output"
run_cmd "kubectl logs job/valkey-cache-example -n gco-jobs --all-containers=true --tail=20 2>/dev/null || kubectl logs -n gco-jobs -l app=valkey-cache-example --all-containers=true --tail=20 2>/dev/null || echo '  (no logs yet)'"

success "Serverless Valkey: zero management, auto-scaling, per-region."
# Release resource-quota reservations before the inference/EFS sections.
kubectl delete job valkey-cache-example -n gco-jobs --ignore-not-found=true >/dev/null 2>&1 || true
narrate "Prompt caching alone can cut inference costs by 30-50%."

pause_for_audience

fi  # VALKEY

# ═════════════════════════════════════════════════════════════════════════════
# SECTION: Aurora pgvector
# ═════════════════════════════════════════════════════════════════════════════
# Aurora Serverless v2 with pgvector provides a fully managed vector database
# for RAG, semantic search, and embedding storage. This section only runs if
# Aurora pgvector is enabled in cdk.json.

if [ "$AURORA_PGVECTOR_ENABLED" = "true" ]; then

SECTION=$((SECTION + 1)); section_header "$SECTION" "AURORA PGVECTOR — Serverless Vector Database" "$BLUE"

narrate "For RAG, semantic search, and embedding storage, GCO can deploy"
narrate "Aurora Serverless v2 with pgvector in each region. It auto-scales"
narrate "capacity and requires no instance management."
narrate "Credentials are in Secrets Manager — pods discover them via ConfigMap."
spacer

highlight "Submitting a job that exercises Aurora pgvector"
run_cmd "gco jobs submit-direct examples/aurora-pgvector-job.yaml -r $REGION -n gco-jobs" || true

highlight "Watching the Aurora pgvector job"
wait_for_job "aurora-pgvector-example" "gco-jobs"
run_cmd "kubectl get pods -n gco-jobs -l app=aurora-pgvector-example --no-headers 2>/dev/null || echo '  (pod scheduling...)'"

highlight "Aurora pgvector job output"
run_cmd "kubectl logs job/aurora-pgvector-example -n gco-jobs --all-containers=true --tail=20 2>/dev/null || kubectl logs -n gco-jobs -l app=aurora-pgvector-example --all-containers=true --tail=20 2>/dev/null || echo '  (no logs yet)'"

success "Serverless Aurora pgvector: vector search with zero management."
# Release resource-quota reservations before the next section.
kubectl delete job aurora-pgvector-example -n gco-jobs --ignore-not-found=true >/dev/null 2>&1 || true
narrate "pgvector supports HNSW and IVFFlat indexes for fast similarity search."

pause_for_audience

fi  # AURORA_PGVECTOR

# ═════════════════════════════════════════════════════════════════════════════
# SECTION: EFS Shared Storage
# ═════════════════════════════════════════════════════════════════════════════
# EFS (Elastic File System) is always deployed — it's the default shared
# storage for job outputs. This section always runs because EFS is a core
# feature, not optional.

SECTION=$((SECTION + 1)); section_header "$SECTION" "EFS — Persistent Shared Storage" "$BLUE"

narrate "When a Kubernetes pod terminates, its local data vanishes."
narrate "GCO mounts Amazon EFS into every cluster so job outputs,"
narrate "model checkpoints, and training artifacts persist beyond pod lifetime."
narrate "You can download results even after the job is long gone."
spacer

highlight "Submitting a job that writes results to shared EFS storage"
run_cmd "gco jobs submit-direct examples/efs-output-job.yaml -r $REGION -n gco-jobs" || true

highlight "Watching the EFS job"
wait_for_job "efs-output-example" "gco-jobs"
run_cmd "kubectl get pods -n gco-jobs -l example=efs-output --no-headers 2>/dev/null || echo '  (pod scheduling...)'"

highlight "Job logs — results written to /outputs on EFS"
run_cmd "kubectl logs job/efs-output-example -n gco-jobs --all-containers=true --tail=15 2>/dev/null || kubectl logs -n gco-jobs -l example=efs-output --all-containers=true --tail=15 2>/dev/null || echo '  (no logs yet)'"

spacer
narrate "The pod is gone, but the data lives on. Let's prove it."
spacer

highlight "Listing files on shared EFS storage"
run_cmd "gco files ls -r $REGION" || true

highlight "Downloading results to local machine"
run_cmd "gco files download efs-output-example /tmp/gco-demo-results -r $REGION && cat /tmp/gco-demo-results/results.json"

success "Persistent storage that survives pod termination."
narrate "Critical for ML checkpoints, training artifacts, and audit trails."

pause_for_audience

# ═════════════════════════════════════════════════════════════════════════════
# SECTION: Inference Endpoint
# ═════════════════════════════════════════════════════════════════════════════
# The inference endpoint was deployed at the start of the demo (before costs)
# so the GPU node could provision in the background. By now it should be
# ready. We just need to wait for readiness, invoke it, and clean up.
# Placed at the end to give the GPU node maximum time to provision.
# Skippable with SKIP_INFERENCE=1 (useful if no GPU quota is available).

if [ "${SKIP_INFERENCE:-}" != "1" ]; then

SECTION=$((SECTION + 1)); section_header "$SECTION" "INFERENCE — Live LLM on GCO" "$CYAN"

narrate "GCO isn't just for batch jobs — it also manages multi-region"
narrate "inference endpoints. We deployed a vLLM endpoint at the start"
narrate "of this demo so the GPU could provision while we covered other"
narrate "features. Let's see if it's ready."
spacer

highlight "Checking if the inference endpoint is ready"
narrate "The endpoint was deployed with a single command at the start."
narrate "EKS Auto Mode provisioned a GPU node, pulled the vLLM image,"
narrate "and loaded the facebook/opt-125m model — all automatically."

# Poll for the endpoint to become ready. Since we deployed it at the start,
# it's had several minutes to provision. We still poll in case it's not
# quite ready yet. Ignore Terminating pods from previous runs.
INFERENCE_READY=false
for attempt in $(seq 1 50); do
    POD_STATUS=$(kubectl get pods -n gco-inference -l app="$INFERENCE_NAME" --no-headers 2>/dev/null \
        | grep -v "Terminating" || true)
    if echo "$POD_STATUS" | grep -q "1/1.*Running"; then
        INFERENCE_READY=true
        break
    fi

    if [ -n "$POD_STATUS" ]; then
        echo "  ${DIM}$POD_STATUS${RESET}"
    else
        STATUS_OUTPUT=$(gco inference status "$INFERENCE_NAME" 2>&1 || true)
        echo "$STATUS_OUTPUT" | head -5 | sed 's/^/  /'
    fi

    if [ "$attempt" -lt 50 ]; then
        countdown "Waiting for pod to be ready (attempt $attempt/50)" 15
    fi
done

if [ "$INFERENCE_READY" = "true" ]; then
    success "Inference endpoint is live."
    spacer

    # Wait for the ALB target group to register and pass health checks.
    # The pod is Running but the ALB needs ~30s to register the target
    # and pass 2 consecutive health checks (15s interval).
    narrate "Waiting for ALB target group to register the new endpoint..."
    countdown "ALB target group warmup" 45

    highlight "Sending a prompt to the endpoint"
    narrate "This routes through API Gateway → Global Accelerator → ALB → vLLM pod."
    narrate "The entire path is IAM-authenticated (SigV4)."
    run_cmd "gco inference invoke $INFERENCE_NAME -p 'The benefits of GPU orchestration for ML workloads are: 1)' --max-tokens 80" || true
    sleep "$PAUSE_LONG"

    success "Live LLM response from a GPU that didn't exist minutes ago."
else
    warn "Endpoint not ready yet — GPU node may still be provisioning."
    narrate "In a real demo, give it another minute. For now, moving on."
fi

spacer
highlight "Cleaning up the inference endpoint"
narrate "This deletes the Deployment, Service, and Ingress. The GPU node"
narrate "scales back to zero automatically once the pod is gone."
run_cmd "gco inference delete $INFERENCE_NAME -y" || true

success "Endpoint deployed, invoked, and torn down — full lifecycle."

pause_for_audience

fi  # SKIP_INFERENCE

# ═════════════════════════════════════════════════════════════════════════════
# SECTION: Wrap-up
# ═════════════════════════════════════════════════════════════════════════════
# Summary of everything we covered, plus an optional cleanup step.

banner "Demo Complete"

echo "  ${BOLD}What we covered:${RESET}"
spacer
echo "  ${GREEN}✓${RESET} Cost visibility across services, regions, and workloads"
if [ "${SKIP_CAPACITY:-}" != "1" ]; then
    echo "  ${GREEN}✓${RESET} Capacity discovery and auto-region job placement"
fi

# Only show scheduler items if we didn't skip that section.
if [ "${SKIP_SCHEDULERS:-}" != "1" ]; then
    if [ "$VOLCANO_ENABLED" = "true" ]; then
        echo "  ${GREEN}✓${RESET} Volcano gang scheduling for distributed training"
    fi
    if [ "$KUEUE_ENABLED" = "true" ]; then
        echo "  ${GREEN}✓${RESET} Kueue quota-based job queueing"
    fi
    if [ "$YUNIKORN_ENABLED" = "true" ]; then
        echo "  ${GREEN}✓${RESET} YuniKorn app-aware fair scheduling"
    fi
    if [ "$SLURM_ENABLED" = "true" ]; then
        echo "  ${GREEN}✓${RESET} Slurm HPC batch scheduling on Kubernetes"
    fi
fi

if [ "$FSX_ENABLED" = "true" ]; then
    echo "  ${GREEN}✓${RESET} FSx for Lustre high-performance storage"
fi
if [ "$VALKEY_ENABLED" = "true" ]; then
    echo "  ${GREEN}✓${RESET} Valkey serverless in-memory cache"
fi
if [ "$AURORA_PGVECTOR_ENABLED" = "true" ]; then
    echo "  ${GREEN}✓${RESET} Aurora pgvector serverless vector database"
fi
echo "  ${GREEN}✓${RESET} EFS persistent shared storage"
if [ "${SKIP_INFERENCE:-}" != "1" ]; then
    echo "  ${GREEN}✓${RESET} Inference endpoint deploy, invoke, and teardown"
fi

spacer
echo "  ${BOLD}All of this runs on a single platform, deployed with one command:${RESET}"
echo "  ${CYAN}gco stacks deploy-all -y${RESET}"
spacer
echo "  ${DIM}Repository: https://github.com/awslabs/global-capacity-orchestrator-on-aws${RESET}"
spacer

# ── Cleanup Prompt ───────────────────────────────────────────────────────────
# Offer to delete all the demo jobs we just created. This keeps the cluster
# clean for the next demo run.

echo "  ${YELLOW}${BOLD}Clean up demo jobs?${RESET} ${DIM}(y/N)${RESET}"
if [ "${GCO_DEMO_NONINTERACTIVE:-}" = "1" ]; then
    cleanup="n"
else
    read -r cleanup
fi
case "$cleanup" in
    y|Y)
        narrate "Cleaning up demo jobs..."
        # Delete jobs by label (covers Volcano, FSx, EFS jobs with project=gco label)
        kubectl delete job -n gco-jobs -l project=gco --ignore-not-found=true 2>/dev/null || true
        # Delete specific jobs that may not have the project label
        kubectl delete job -n gco-jobs efs-output-example --ignore-not-found=true 2>/dev/null || true
        kubectl delete job -n gco-jobs valkey-cache-example --ignore-not-found=true 2>/dev/null || true
        kubectl delete job -n gco-jobs aurora-pgvector-example --ignore-not-found=true 2>/dev/null || true
        kubectl delete job -n gco-jobs kueue-sample-job --ignore-not-found=true 2>/dev/null || true
        kubectl delete job -n gco-jobs kueue-gpu-job --ignore-not-found=true 2>/dev/null || true
        kubectl delete job -n gco-jobs yunikorn-sample-job --ignore-not-found=true 2>/dev/null || true
        kubectl delete job -n gco-jobs yunikorn-gpu-job --ignore-not-found=true 2>/dev/null || true
        kubectl delete job -n gco-jobs yunikorn-gang-job --ignore-not-found=true 2>/dev/null || true
        kubectl delete job -n gco-jobs slurm-test --ignore-not-found=true 2>/dev/null || true
        # Volcano uses a custom resource type (vcjob), not a standard Job
        kubectl delete vcjob -n gco-jobs distributed-training --ignore-not-found=true 2>/dev/null || true
        # Clean up any downloaded files
        rm -rf /tmp/gco-demo-results 2>/dev/null || true
        success "Demo jobs cleaned up."
        ;;
    *)
        narrate "Skipping cleanup. Remove jobs manually with:"
        echo "  ${CYAN}kubectl delete jobs --all -n gco-jobs${RESET}"
        ;;
esac

spacer
echo "  ${DIM}Thanks for watching.${RESET}"
spacer
