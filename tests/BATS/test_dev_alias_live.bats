#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for .github/scripts/dev_alias_live.sh
# -----------------------------------------------------------------------------
# Static checks plus the container-less code paths: argument parsing, --help,
# and the --no-runtime refusal (which masks the runtimes with PATH stubs, so it
# runs hermetically). The live container proofs (docker / finch / podman build
# gco-dev and run the generated function) execute in integration-tests.yml, not
# here — they need a real runtime and a multi-minute image build.
#
# Run:  bats tests/BATS/test_dev_alias_live.bats
# -----------------------------------------------------------------------------

SCRIPT=".github/scripts/dev_alias_live.sh"

# -- Static checks ------------------------------------------------------------
@test "dev_alias_live.sh passes bash -n" {
    bash -n "$SCRIPT"
}

@test "dev_alias_live.sh passes shellcheck" {
    command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

# -- Argument handling --------------------------------------------------------
@test "--help prints the header docs and exits 0" {
    run bash "$SCRIPT" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"dev_alias_live.sh"* ]]
    [[ "$output" == *"gco dag validate"* ]]
}

@test "an unknown option exits non-zero with a usage hint" {
    run bash "$SCRIPT" --bogus
    [ "$status" -ne 0 ]
    [[ "$output" == *"usage:"* ]]
}

@test "no runtime and no --no-runtime exits non-zero" {
    run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    [[ "$output" == *"runtime"* ]]
}

@test "--image without a value exits non-zero" {
    run bash "$SCRIPT" --image
    [ "$status" -ne 0 ]
}

# -- --no-runtime refusal (hermetic; masks runtimes with PATH stubs) ----------
@test "--no-runtime proves the setup script refuses and writes no rc block" {
    run bash "$SCRIPT" --no-runtime
    [ "$status" -eq 0 ]
    [[ "$output" == *"no container runtime"* ]]
    [[ "$output" == *"PASS"* ]]
}
