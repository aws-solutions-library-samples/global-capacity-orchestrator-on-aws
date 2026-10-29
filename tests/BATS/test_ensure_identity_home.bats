#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for gco/stacks/assets/pcs/ensure-identity-home.sh
# -----------------------------------------------------------------------------
# The script is the GCO-owned AWS PCS node lifecycle action that creates the
# API's Slurm identity's home directory on the shared /home right after PCS's
# maintained mount-efs.sh mounts it. `mountpoint`, `install`, `chown` and
# `chmod` are fakes that record their argv and act on a scratch directory, so
# every branch — create, verify, the four argument refusals and the unmounted
# base — runs without root or a real mount.
#
# Run:  bats tests/BATS/test_ensure_identity_home.bats
# -----------------------------------------------------------------------------

load 'helpers.sh'

SCRIPT="$REPO_ROOT/gco/stacks/assets/pcs/ensure-identity-home.sh"

setup() {
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export CALLS="$BATS_TEST_TMPDIR/calls"
    : > "$CALLS"
    HOME_BASE="$BATS_TEST_TMPDIR/home"
    mkdir -p "$HOME_BASE"
    # mountpoint: mounted unless MOUNTED=no.
    write_stub "$FAKE_BIN" mountpoint <<'FAKE'
#!/usr/bin/env bash
printf 'mountpoint %s\n' "$*" >> "$CALLS"
[ "${MOUNTED:-yes}" = "yes" ]
FAKE
    # install -d -m MODE -o UID -g GID DIR: record and create the directory.
    write_stub "$FAKE_BIN" install <<'FAKE'
#!/usr/bin/env bash
printf 'install %s\n' "$*" >> "$CALLS"
mkdir -p "${@: -1}"
FAKE
    write_stub "$FAKE_BIN" chown <<'FAKE'
#!/usr/bin/env bash
printf 'chown %s\n' "$*" >> "$CALLS"
FAKE
    write_stub "$FAKE_BIN" chmod <<'FAKE'
#!/usr/bin/env bash
printf 'chmod %s\n' "$*" >> "$CALLS"
FAKE
}

run_script() {
    run env PATH="$FAKE_BIN:$PATH" bash "$SCRIPT" "$@"
}

@test "ensure-identity-home.sh passes bash -n and shellcheck" {
    bash -n "$SCRIPT"
    command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "a missing home is created with the requested owner and mode" {
    run_script --home-base "$HOME_BASE" --name ec2-user --uid 1000 --gid 1000

    [ "$status" -eq 0 ]
    [ "$output" = "created $HOME_BASE/ec2-user for uid=1000 gid=1000 mode=0750" ]
    [ -d "$HOME_BASE/ec2-user" ]
    grep -qx -- "mountpoint -q $HOME_BASE" "$CALLS"
    grep -qx -- "install -d -m 0750 -o 1000 -g 1000 $HOME_BASE/ec2-user" "$CALLS"
    ! grep -q '^chown' "$CALLS"
}

@test "an existing home keeps its contents and only has ownership and mode asserted" {
    mkdir -p "$HOME_BASE/hpc"
    touch "$HOME_BASE/hpc/keep.txt"
    run_script --home-base "$HOME_BASE" --name hpc --uid 2000 --gid 3000 --mode 0755

    [ "$status" -eq 0 ]
    [ "$output" = "verified $HOME_BASE/hpc for uid=2000 gid=3000 mode=0755" ]
    [ -f "$HOME_BASE/hpc/keep.txt" ]
    grep -qx -- "chown 2000:3000 $HOME_BASE/hpc" "$CALLS"
    grep -qx -- "chmod 0755 $HOME_BASE/hpc" "$CALLS"
    ! grep -q '^install' "$CALLS"
}

@test "the home base defaults to /home" {
    # /home is not a mount point in the sandbox; the fake says it is, and the
    # fake install only records, so nothing is written outside the tmpdir.
    write_stub "$FAKE_BIN" install <<'FAKE'
#!/usr/bin/env bash
printf 'install %s\n' "$*" >> "$CALLS"
FAKE
    run_script --name ec2-user --uid 1000 --gid 1000

    [ "$status" -eq 0 ]
    grep -qx -- "mountpoint -q /home" "$CALLS"
    grep -qx -- "install -d -m 0750 -o 1000 -g 1000 /home/ec2-user" "$CALLS"
}

@test "missing required flags print usage" {
    run_script --home-base "$HOME_BASE" --name ec2-user --uid 1000

    [ "$status" -eq 2 ]
    [[ "$output" == *"usage:"* ]]
    ! grep -q '^install' "$CALLS"
}

@test "an unknown argument is refused" {
    run_script --home-base "$HOME_BASE" --name ec2-user --uid 1000 --gid 1000 --shell /bin/zsh

    [ "$status" -eq 2 ]
    [[ "$output" == *"unknown argument: --shell"* ]]
}

@test "an invalid user name is refused" {
    run_script --home-base "$HOME_BASE" --name "../etc" --uid 1000 --gid 1000

    [ "$status" -eq 1 ]
    [[ "$output" == *"invalid user name: ../etc"* ]]
    ! grep -q '^install' "$CALLS"
}

@test "non-numeric ids are refused" {
    run_script --home-base "$HOME_BASE" --name ec2-user --uid ten --gid 1000

    [ "$status" -eq 1 ]
    [[ "$output" == *"uid and gid must be numeric: uid=ten gid=1000"* ]]
}

@test "an invalid mode is refused" {
    run_script --home-base "$HOME_BASE" --name ec2-user --uid 1000 --gid 1000 --mode 7777

    [ "$status" -eq 1 ]
    [[ "$output" == *"invalid mode: 7777"* ]]
}

@test "an unmounted home base is refused before anything is created" {
    MOUNTED=no run_script --home-base "$HOME_BASE" --name ec2-user --uid 1000 --gid 1000

    [ "$status" -eq 1 ]
    [[ "$output" == *"$HOME_BASE is not a mounted filesystem"* ]]
    [ ! -d "$HOME_BASE/ec2-user" ]
}
