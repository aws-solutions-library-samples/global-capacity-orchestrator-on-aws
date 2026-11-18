#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for .github/scripts/install-calico.sh
# -----------------------------------------------------------------------------
# The shared Calico install for the integration:kind:* jobs. `curl` is a fake
# that writes a fixture manifest to its --output path (or fails), `kubectl` a
# fake that records every call and fails a scripted number of rollout waits;
# `sha256sum` is real, so the pinned-checksum gate is exercised for real.
#
# Run:  bats tests/BATS/test_install_calico.bats
# -----------------------------------------------------------------------------

load 'helpers.sh'

SCRIPT="$REPO_ROOT/.github/scripts/install-calico.sh"

setup() {
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export CALLS="$BATS_TEST_TMPDIR/calls"
    export ROLLOUT_COUNT="$BATS_TEST_TMPDIR/rollouts"
    : > "$CALLS"
    printf '0\n' > "$ROLLOUT_COUNT"
    export FIXTURE_MANIFEST="$BATS_TEST_TMPDIR/fixture-calico.yaml"
    printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: calico-fixture\n' > "$FIXTURE_MANIFEST"
    FIXTURE_SHA256="$(sha256sum "$FIXTURE_MANIFEST" | awk '{print $1}')"
    # FAKE_CURL=fail: the download fails.
    write_stub "$FAKE_BIN" curl <<'FAKE_CURL'
#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$CALLS"
[ "${FAKE_CURL:-ok}" = "fail" ] && exit 22
output=""
while [ "$#" -gt 0 ]; do
    [ "$1" = "--output" ] && { output="$2"; shift; }
    shift
done
cp "$FIXTURE_MANIFEST" "$output"
FAKE_CURL
    # FAKE_ROLLOUT_FAILURES: how many calico-node rollout waits fail before
    # one succeeds (default 0; "always" never succeeds).
    write_stub "$FAKE_BIN" kubectl <<'FAKE_KUBECTL'
#!/usr/bin/env bash
printf 'kubectl %s\n' "$*" >> "$CALLS"
case "$*" in
    *"rollout status daemonset/calico-node"*)
        count=$(( $(cat "$ROLLOUT_COUNT") + 1 ))
        printf '%s\n' "$count" > "$ROLLOUT_COUNT"
        failures="${FAKE_ROLLOUT_FAILURES:-0}"
        [ "$failures" = "always" ] && exit 1
        [ "$count" -le "$failures" ] && exit 1
        ;;
    *" describe "*|*" get events "*) exit 1 ;;
esac
exit 0
FAKE_KUBECTL
}

run_install() {
    run env PATH="$FAKE_BIN:$PATH" RUNNER_TEMP="$BATS_TEST_TMPDIR" \
        CALICO_VERSION=v3.33.0 CALICO_SHA256="$FIXTURE_SHA256" CALICO_RETRY_DELAY_SECONDS=0 \
        "$@" bash "$SCRIPT"
}

@test "install-calico.sh passes bash -n and shellcheck" {
    bash -n "$SCRIPT"
    command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "the pinned manifest is downloaded, verified, applied and waited for" {
    run_install
    [ "$status" -eq 0 ]
    [[ "$output" == *"OK"* ]]
    [[ "$output" == *"Calico is ready."* ]]
    grep -q "^curl .*--retry-all-errors .*--remove-on-error --output ${BATS_TEST_TMPDIR}/calico.yaml https://raw.githubusercontent.com/projectcalico/calico/v3.33.0/manifests/calico.yaml$" "$CALLS"
    [ "$(grep -c "^kubectl apply -f ${BATS_TEST_TMPDIR}/calico.yaml$" "$CALLS")" = "2" ]
    grep -q '^kubectl -n kube-system rollout status daemonset/calico-node --timeout=4m$' "$CALLS"
    grep -q '^kubectl -n kube-system rollout status deployment/calico-kube-controllers --timeout=2m$' "$CALLS"
    ! grep -q ' describe ' "$CALLS"
}

@test "a rollout that is ready on a later attempt passes after a re-apply" {
    run_install FAKE_ROLLOUT_FAILURES=1
    [ "$status" -eq 0 ]
    [[ "$output" == *"Calico not ready on attempt 1/3."* ]]
    [[ "$output" == *"Retrying in 0s..."* ]]
    [[ "$output" == *"Calico is ready."* ]]
    [ "$(grep -c '^kubectl apply' "$CALLS")" = "3" ]
}

@test "a rollout that never becomes ready fails with every diagnostic" {
    run_install FAKE_ROLLOUT_FAILURES=always CALICO_ROLLOUT_ATTEMPTS=2
    [ "$status" -eq 1 ]
    [[ "$output" == *"Calico not ready on attempt 2/2."* ]]
    [[ "$output" == *"::error::Calico did not become ready after 2 attempts."* ]]
    [[ "$output" == *"===== Diagnostics: describe calico-node ====="* ]]
    # One pause between the two attempts, none after the last.
    [ "$(grep -o 'Retrying in' <<<"$output" | wc -l | tr -d ' ')" = "1" ]
    grep -q '^kubectl -n kube-system get pods -o wide$' "$CALLS"
    grep -q '^kubectl -n kube-system describe daemonset/calico-node$' "$CALLS"
    grep -q '^kubectl -n kube-system describe pods -l k8s-app=calico-node$' "$CALLS"
    grep -q '^kubectl -n kube-system get events --sort-by=.lastTimestamp$' "$CALLS"
}

@test "a manifest that does not match CALICO_SHA256 is never applied" {
    run_install CALICO_SHA256=0000000000000000000000000000000000000000000000000000000000000000
    [ "$status" -ne 0 ]
    ! grep -q '^kubectl' "$CALLS"
}

@test "a failed download stops before kubectl" {
    run_install FAKE_CURL=fail
    [ "$status" -ne 0 ]
    ! grep -q '^kubectl' "$CALLS"
}

@test "missing pins are refused before anything is downloaded" {
    run env -u CALICO_VERSION PATH="$FAKE_BIN:$PATH" CALICO_SHA256=abc bash "$SCRIPT"
    [ "$status" -ne 0 ]
    [[ "$output" == *"CALICO_VERSION must name the pinned Calico release"* ]]
    run env -u CALICO_SHA256 PATH="$FAKE_BIN:$PATH" CALICO_VERSION=v3.33.0 bash "$SCRIPT"
    [ "$status" -ne 0 ]
    [[ "$output" == *"CALICO_SHA256 must be the pinned manifest checksum"* ]]
    [ ! -s "$CALLS" ]
}

@test "without RUNNER_TEMP the manifest lands in TMPDIR" {
    run env -u RUNNER_TEMP PATH="$FAKE_BIN:$PATH" TMPDIR="$BATS_TEST_TMPDIR/tmp" \
        CALICO_VERSION=v3.33.0 CALICO_SHA256="$FIXTURE_SHA256" bash -c 'mkdir -p "$TMPDIR" && exec bash "$1"' _ "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q "^kubectl apply -f ${BATS_TEST_TMPDIR}/tmp/calico.yaml$" "$CALLS"
}
