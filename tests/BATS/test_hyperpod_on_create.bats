#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for gco/stacks/assets/hyperpod/on_create.sh
# -----------------------------------------------------------------------------
# The script is the OnCreate lifecycle step HyperPod runs on every instance
# it provisions for GCO's EKS-orchestrated clusters. With EKS as the
# orchestrator the service bootstraps the kubelet itself, so the script's whole
# contract is: never fail, and leave a record of the run on the node's host
# log. `tee` and `hostname` are fakes that record their argv, so the host log
# path (/var/log) is asserted without writing there, and the hostname line is
# deterministic.
#
# Run:  bats tests/BATS/test_hyperpod_on_create.bats
# -----------------------------------------------------------------------------

load 'helpers.sh'

SCRIPT="$REPO_ROOT/gco/stacks/assets/hyperpod/on_create.sh"
HOST_LOG="/var/log/gco-hyperpod-on-create.log"

setup() {
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export CALLS="$BATS_TEST_TMPDIR/calls"
    export LOG_COPY="$BATS_TEST_TMPDIR/host-log"
    : > "$CALLS"
    # tee: record argv, then behave like `tee -a` with the host log redirected
    # into the scratch directory (bash builtins only, so the fake cannot
    # recurse into itself through PATH).
    write_stub "$FAKE_BIN" tee <<'FAKE'
#!/usr/bin/env bash
printf 'tee %s\n' "$*" >> "$CALLS"
while IFS= read -r line; do
    printf '%s\n' "$line"
    printf '%s\n' "$line" >> "$LOG_COPY"
done
FAKE
    write_stub "$FAKE_BIN" hostname <<'FAKE'
#!/usr/bin/env bash
printf 'hostname %s\n' "$*" >> "$CALLS"
echo ip-10-0-1-23.ec2.internal
FAKE
}

# run_script [env options...]: options go first so `-u NAME` works on both GNU
# and BSD env, which accept flags only ahead of the NAME=VALUE assignments.
run_script() {
    run env "$@" PATH="$FAKE_BIN:$PATH" bash "$SCRIPT"
}

@test "on_create.sh passes bash -n and shellcheck" {
    bash -n "$SCRIPT"
    command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "a run succeeds and records start, host, instance group, orchestrator and done" {
    run_script SAGEMAKER_INSTANCE_GROUP_NAME=gco-hyperpod

    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 5 ]
    [[ "${lines[0]}" =~ ^\[[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\]\ gco\ hyperpod\ on_create:\ start$ ]]
    [ "${lines[1]}" = "hostname: ip-10-0-1-23.ec2.internal" ]
    [ "${lines[2]}" = "instance group: gco-hyperpod" ]
    [ "${lines[3]}" = "orchestrator: eks (no bootstrap performed by this script)" ]
    [[ "${lines[4]}" =~ ^\[[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\]\ gco\ hyperpod\ on_create:\ done$ ]]
    grep -qx -- "hostname " "$CALLS"
}

@test "the record is appended to the node's host log, never truncating it" {
    run_script SAGEMAKER_INSTANCE_GROUP_NAME=gco-hyperpod

    [ "$status" -eq 0 ]
    grep -qx -- "tee -a $HOST_LOG" "$CALLS"
    # What tee received is exactly what stood on stdout.
    [ "$(cat "$LOG_COPY")" = "$output" ]
}

@test "an instance group HyperPod did not name is logged as unknown" {
    run_script -u SAGEMAKER_INSTANCE_GROUP_NAME

    [ "$status" -eq 0 ]
    [ "${lines[2]}" = "instance group: unknown" ]
}
