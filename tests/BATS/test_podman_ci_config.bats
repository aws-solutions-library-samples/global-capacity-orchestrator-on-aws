#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for .github/scripts/podman_ci_config.sh
# -----------------------------------------------------------------------------
# The script writes ~/.config/containers/containers.conf for the
# integration:dev-alias:podman job, which tries two OCI runtime configurations
# because each fails in a way the other survives:
#
#   crun  needs cgroups = "disabled" (no systemd user session in CI), but breaks
#         when the runner's podman is newer than its crun ("unknown version
#         specified" at the first RUN).
#   runc  is immune to that skew but podman refuses to combine it with
#         cgroups = "disabled" ("not compatible with NoCgroups").
#
# The pairing is what matters: a crun config missing `cgroups = "disabled"`, or a
# runc config that includes it, silently reintroduces one of those CI failures.
# These tests pin both shapes, plus the argument handling and the fact that HOME
# is respected (the job relies on that to rewrite the config between attempts).
#
# `runc` is faked with a PATH shim so nothing real is required.
#
# Run:  bats tests/BATS/test_podman_ci_config.bats
# -----------------------------------------------------------------------------

SCRIPT=".github/scripts/podman_ci_config.sh"

setup() {
    FAKE_HOME="$(mktemp -d)"
    SHIMDIR="$(mktemp -d)"
    CONFIG="$FAKE_HOME/.config/containers/containers.conf"
}

teardown() {
    rm -rf "$FAKE_HOME" "$SHIMDIR"
}

# Put a fake `runc` on PATH so the runc branch can run on any machine.
make_runc_shim() {
    cat > "$SHIMDIR/runc" <<'SHIM'
#!/usr/bin/env bash
echo "runc version 1.3.6"
SHIM
    chmod +x "$SHIMDIR/runc"
}

# -- Static checks ------------------------------------------------------------

@test "script exists and is executable" {
    [ -f "$SCRIPT" ]
    [ -x "$SCRIPT" ]
}

@test "script passes shellcheck" {
    if ! command -v shellcheck >/dev/null 2>&1; then
        skip "shellcheck not installed"
    fi
    run shellcheck -x "$SCRIPT"
    [ "$status" -eq 0 ]
}

# -- Argument handling --------------------------------------------------------

@test "missing runtime argument fails with a usage message" {
    run env HOME="$FAKE_HOME" bash "$SCRIPT"
    [ "$status" -ne 0 ]
    [[ "$output" == *"usage"* ]]
}

@test "unknown runtime is rejected with exit 2 and names the valid options" {
    run env HOME="$FAKE_HOME" bash "$SCRIPT" containerd
    [ "$status" -eq 2 ]
    [[ "$output" == *"unknown runtime"* ]]
    [[ "$output" == *"crun"* ]]
    [[ "$output" == *"runc"* ]]
}

@test "a rejected runtime writes no config file" {
    run env HOME="$FAKE_HOME" bash "$SCRIPT" containerd
    [ "$status" -eq 2 ]
    [ ! -f "$CONFIG" ]
}

# -- crun configuration -------------------------------------------------------

@test "crun writes the config under \$HOME, creating the directory" {
    [ ! -d "$FAKE_HOME/.config/containers" ]
    run env HOME="$FAKE_HOME" bash "$SCRIPT" crun
    [ "$status" -eq 0 ]
    [ -f "$CONFIG" ]
}

@test "crun config disables cgroups, which crun requires without systemd" {
    run env HOME="$FAKE_HOME" bash "$SCRIPT" crun
    [ "$status" -eq 0 ]
    grep -q 'cgroups = "disabled"' "$CONFIG"
}

@test "crun config sets the cgroupfs manager and file events logger" {
    run env HOME="$FAKE_HOME" bash "$SCRIPT" crun
    [ "$status" -eq 0 ]
    grep -q 'cgroup_manager = "cgroupfs"' "$CONFIG"
    grep -q 'events_logger = "file"' "$CONFIG"
}

@test "crun config pins no runtime, leaving podman its default" {
    run env HOME="$FAKE_HOME" bash "$SCRIPT" crun
    [ "$status" -eq 0 ]
    ! grep -q '^runtime =' "$CONFIG"
}

# -- runc configuration -------------------------------------------------------

@test "runc config pins runtime = runc" {
    make_runc_shim
    run env HOME="$FAKE_HOME" PATH="$SHIMDIR:$PATH" bash "$SCRIPT" runc
    [ "$status" -eq 0 ]
    grep -q 'runtime = "runc"' "$CONFIG"
}

@test "runc config never disables cgroups, which podman rejects with runc" {
    make_runc_shim
    run env HOME="$FAKE_HOME" PATH="$SHIMDIR:$PATH" bash "$SCRIPT" runc
    [ "$status" -eq 0 ]
    # podman fails with "requested OCI runtime runc is not compatible with
    # NoCgroups" if these are combined, so the absence here is the point.
    ! grep -q 'cgroups = "disabled"' "$CONFIG"
}

@test "runc config keeps the cgroupfs manager" {
    make_runc_shim
    run env HOME="$FAKE_HOME" PATH="$SHIMDIR:$PATH" bash "$SCRIPT" runc
    [ "$status" -eq 0 ]
    grep -q 'cgroup_manager = "cgroupfs"' "$CONFIG"
}

@test "runc reports the resolved runtime version for debugging" {
    make_runc_shim
    run env HOME="$FAKE_HOME" PATH="$SHIMDIR:$PATH" bash "$SCRIPT" runc
    [ "$status" -eq 0 ]
    [[ "$output" == *"runc version 1.3.6"* ]]
}

@test "runc fails clearly when runc is not installed" {
    # Mirror a runner image without runc: a PATH carrying only the few external
    # commands the script needs, so the `command -v runc` lookup misses. An
    # empty PATH would instead fail at `mkdir` (or at bash itself) and prove
    # nothing, so bash is invoked by absolute path and the utilities are
    # symlinked in.
    mkdir -p "$SHIMDIR/bin"
    for util in mkdir cat; do
        ln -s "$(command -v "$util")" "$SHIMDIR/bin/$util"
    done
    [ ! -e "$SHIMDIR/bin/runc" ]

    run env HOME="$FAKE_HOME" PATH="$SHIMDIR/bin" /bin/bash "$SCRIPT" runc
    [ "$status" -eq 1 ]
    [[ "$output" == *"runc is not installed"* ]]
}

# -- Rewriting between attempts ----------------------------------------------

@test "a second call replaces the previous configuration" {
    # The job writes the crun config, then rewrites it as runc for the second
    # attempt, so stale keys must not survive.
    make_runc_shim
    run env HOME="$FAKE_HOME" bash "$SCRIPT" crun
    [ "$status" -eq 0 ]
    grep -q 'cgroups = "disabled"' "$CONFIG"

    run env HOME="$FAKE_HOME" PATH="$SHIMDIR:$PATH" bash "$SCRIPT" runc
    [ "$status" -eq 0 ]
    grep -q 'runtime = "runc"' "$CONFIG"
    ! grep -q 'cgroups = "disabled"' "$CONFIG"
}

@test "each configuration echoes which runtime it wrote" {
    run env HOME="$FAKE_HOME" bash "$SCRIPT" crun
    [ "$status" -eq 0 ]
    [[ "$output" == *"containers.conf (crun)"* ]]
}

@test "the written config is valid TOML-style key/value lines only" {
    run env HOME="$FAKE_HOME" bash "$SCRIPT" crun
    [ "$status" -eq 0 ]
    # Every non-blank line is a [section] header or a key = value pair.
    run grep -vE '^\[[a-z]+\]$|^[a-z_]+ = ".*"$|^$' "$CONFIG"
    [ "$status" -ne 0 ]
}
