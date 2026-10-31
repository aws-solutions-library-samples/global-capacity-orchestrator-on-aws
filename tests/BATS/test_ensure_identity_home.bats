#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for gco/stacks/assets/pcs/ensure-identity-home.sh
# -----------------------------------------------------------------------------
# The script is the GCO-owned AWS PCS node lifecycle action that creates the
# API's Slurm identity's home directory on the shared /home right after PCS's
# maintained mount-efs.sh mounts it, and with --create-user adds the identity
# to the node first. `mountpoint`, `install`, `chown`, `chmod`, `getent`,
# `groupadd` and `useradd` are fakes that record their argv and act on a
# scratch directory (the passwd/group fakes read PASSWD/GROUP fixtures), so
# every branch — create, verify, the argument refusals, the unmounted base,
# and every user-creation outcome — runs without root or a real mount.
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
    # getent passwd|group KEY: answer from the PASSWD / GROUP fixtures (one
    # passwd(5) / group(5) line each), matching by name or numeric id; exit 2
    # when absent, as the real getent does.
    export PASSWD="$BATS_TEST_TMPDIR/passwd"
    export GROUP="$BATS_TEST_TMPDIR/group"
    : > "$PASSWD"
    : > "$GROUP"
    write_stub "$FAKE_BIN" getent <<'FAKE'
#!/usr/bin/env bash
printf 'getent %s\n' "$*" >> "$CALLS"
case "$1" in
    passwd) db="$PASSWD"; idfield=3 ;;
    group) db="$GROUP"; idfield=3 ;;
    *) exit 1 ;;
esac
awk -F: -v key="$2" -v idfield="$idfield" '$1 == key || $idfield == key { print; found = 1 } END { exit found ? 0 : 2 }' "$db"
FAKE
    write_stub "$FAKE_BIN" groupadd <<'FAKE'
#!/usr/bin/env bash
printf 'groupadd %s\n' "$*" >> "$CALLS"
FAKE
    write_stub "$FAKE_BIN" useradd <<'FAKE'
#!/usr/bin/env bash
printf 'useradd %s\n' "$*" >> "$CALLS"
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

@test "--dir names a directory other than the user name and is validated" {
    run_script --home-base "$HOME_BASE" --name hpcuser --dir hpc --uid 2000 --gid 3000 --create-user

    [ "$status" -eq 0 ]
    [ -d "$HOME_BASE/hpc" ]
    [ ! -d "$HOME_BASE/hpcuser" ]
    grep -qx -- "useradd --uid 2000 --gid 3000 --home-dir $HOME_BASE/hpc --no-create-home --shell /bin/bash --comment GCO Slurm identity hpcuser" "$CALLS"

    run_script --home-base "$HOME_BASE" --name hpcuser --dir "../etc" --uid 2000 --gid 3000
    [ "$status" -eq 1 ]
    [[ "$output" == *"invalid directory name: ../etc"* ]]
}

@test "missing required flags print usage" {
    run_script --home-base "$HOME_BASE" --name ec2-user --uid 1000

    [ "$status" -eq 2 ]
    [[ "$output" == *"usage:"* ]]
    ! grep -q '^install' "$CALLS"
}

@test "an unknown argument is refused" {
    run_script --home-base "$HOME_BASE" --name ec2-user --uid 1000 --gid 1000 --password x

    [ "$status" -eq 2 ]
    [[ "$output" == *"unknown argument: --password"* ]]
}

@test "without --create-user the node's users are not even looked up" {
    run_script --home-base "$HOME_BASE" --name hpc --uid 2000 --gid 3000

    [ "$status" -eq 0 ]
    ! grep -q '^getent' "$CALLS"
    ! grep -q '^useradd' "$CALLS"
}

@test "--create-user adds the group and the user, then the home" {
    run_script --home-base "$HOME_BASE" --name hpc --uid 2000 --gid 3000 --create-user \
        --shell /bin/zsh --gecos "HPC identity"

    [ "$status" -eq 0 ]
    [[ "${lines[0]}" == "created group hpc (gid=3000)" ]]
    [[ "${lines[1]}" == "created user hpc (uid=2000 gid=3000 shell=/bin/zsh)" ]]
    [[ "${lines[2]}" == "created $HOME_BASE/hpc for uid=2000 gid=3000 mode=0750" ]]
    grep -qx -- "getent passwd 2000" "$CALLS"
    grep -qx -- "getent passwd hpc" "$CALLS"
    grep -qx -- "getent group 3000" "$CALLS"
    grep -qx -- "groupadd --gid 3000 hpc" "$CALLS"
    grep -qx -- "useradd --uid 2000 --gid 3000 --home-dir $HOME_BASE/hpc --no-create-home --shell /bin/zsh --comment HPC identity hpc" "$CALLS"
    # The user is created before the directory, so install can chown to it.
    [ "$(grep -n '^useradd' "$CALLS" | cut -d: -f1)" -lt "$(grep -n '^install' "$CALLS" | cut -d: -f1)" ]
}

@test "--create-user defaults the shell and GECOS and reuses an existing group" {
    echo "hpcgrp:x:3000:" > "$GROUP"
    run_script --home-base "$HOME_BASE" --name hpc --uid 2000 --gid 3000 --create-user

    [ "$status" -eq 0 ]
    ! grep -q '^groupadd' "$CALLS"
    grep -qx -- "useradd --uid 2000 --gid 3000 --home-dir $HOME_BASE/hpc --no-create-home --shell /bin/bash --comment GCO Slurm identity hpc" "$CALLS"
}

@test "--create-user leaves a matching existing user alone" {
    echo "hpc:x:2000:3000::/home/hpc:/bin/bash" > "$PASSWD"
    run_script --home-base "$HOME_BASE" --name hpc --uid 2000 --gid 3000 --create-user

    [ "$status" -eq 0 ]
    [[ "${lines[0]}" == "user hpc (uid=2000) already exists" ]]
    ! grep -q '^useradd' "$CALLS"
    ! grep -q '^groupadd' "$CALLS"
    [ -d "$HOME_BASE/hpc" ]
}

@test "--create-user refuses a uid that belongs to another user" {
    echo "ec2-user:x:2000:2000::/home/ec2-user:/bin/bash" > "$PASSWD"
    run_script --home-base "$HOME_BASE" --name hpc --uid 2000 --gid 3000 --create-user

    [ "$status" -eq 1 ]
    [[ "$output" == *"uid 2000 already belongs to user ec2-user, not hpc"* ]]
    ! grep -q '^useradd' "$CALLS"
    [ ! -d "$HOME_BASE/hpc" ]
}

@test "--create-user refuses a name that exists with another uid" {
    echo "hpc:x:1500:1500::/home/hpc:/bin/bash" > "$PASSWD"
    run_script --home-base "$HOME_BASE" --name hpc --uid 2000 --gid 3000 --create-user

    [ "$status" -eq 1 ]
    [[ "$output" == *"user hpc already exists with uid 1500, not 2000"* ]]
    ! grep -q '^useradd' "$CALLS"
}

@test "--create-user refuses a group name that exists with another gid" {
    echo "hpc:x:3500:" > "$GROUP"
    run_script --home-base "$HOME_BASE" --name hpc --uid 2000 --gid 3000 --create-user

    [ "$status" -eq 1 ]
    [[ "$output" == *"group hpc already exists with another gid; cannot create gid 3000"* ]]
    ! grep -q '^groupadd' "$CALLS"
    ! grep -q '^useradd' "$CALLS"
}

@test "a relative shell and a GECOS with a colon are refused" {
    run_script --home-base "$HOME_BASE" --name hpc --uid 2000 --gid 3000 --create-user --shell zsh
    [ "$status" -eq 1 ]
    [[ "$output" == *"shell must be an absolute path: zsh"* ]]

    run_script --home-base "$HOME_BASE" --name hpc --uid 2000 --gid 3000 --create-user --gecos "a:b"
    [ "$status" -eq 1 ]
    [[ "$output" == *"gecos must not contain ':': a:b"* ]]
    ! grep -q '^useradd' "$CALLS"
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
