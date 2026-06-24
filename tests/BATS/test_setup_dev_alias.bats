#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for scripts/setup-dev-alias.sh
# -----------------------------------------------------------------------------
# Exercises runtime detection, per-runtime socket selection, the emitted shell
# function, and idempotent rc-file installation. Container runtimes are faked
# with PATH shims so nothing real is required.
#
# Run:  bats tests/BATS/test_setup_dev_alias.bats
# -----------------------------------------------------------------------------

SCRIPT="scripts/setup-dev-alias.sh"

# Make a fake runtime executable on PATH. It answers `<rt> info` with the exit
# code requested via $3 (so detection can be steered), exits 0 otherwise, and
# logs every invocation's args to $GCO_SHIM_LOG when that var is set.
make_shim() {
    local dir="$1" name="$2" code="$3"
    cat > "$dir/$name" <<SHIM
#!/usr/bin/env bash
if [ -n "\${GCO_SHIM_LOG:-}" ]; then printf '%s\n' "\$*" >> "\$GCO_SHIM_LOG"; fi
if [ "\$1" = "info" ]; then exit $code; fi
exit 0
SHIM
    chmod +x "$dir/$name"
}

setup() {
    SHIMDIR="$(mktemp -d)"
    RCFILE="$(mktemp -u)"
}

teardown() {
    rm -rf "$SHIMDIR"
    rm -f "$RCFILE"
}

# -- Static checks ------------------------------------------------------------
@test "setup-dev-alias.sh passes bash -n" {
    bash -n "$SCRIPT"
}

@test "setup-dev-alias.sh passes shellcheck" {
    command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "--help prints usage and exits 0" {
    run bash "$SCRIPT" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"Usage:"* ]]
}

# -- --print emits a function block (no file writes) --------------------------
@test "--print --runtime docker emits a docker block with the docker socket" {
    run bash "$SCRIPT" --print --runtime docker
    [ "$status" -eq 0 ]
    [[ "$output" == *"# >>> gco >>>"* ]]
    [[ "$output" == *"# <<< gco <<<"* ]]
    [[ "$output" == *"docker run"* ]]
    [[ "$output" == *"/var/run/docker.sock:/var/run/docker.sock"* ]]
    [[ "$output" == *"-w /workspace"* ]]
    [[ "$output" == *'gco "$@"'* ]]
}

@test "--print --runtime finch omits the docker socket mount" {
    run bash "$SCRIPT" --print --runtime finch
    [ "$status" -eq 0 ]
    [[ "$output" == *"finch run"* ]]
    [[ "$output" != *"/var/run/docker.sock"* ]]
}

@test "--print --runtime podman maps the podman socket onto docker.sock" {
    run bash "$SCRIPT" --print --runtime podman
    [ "$status" -eq 0 ]
    [[ "$output" == *"podman run"* ]]
    [[ "$output" == *"podman.sock:/var/run/docker.sock"* ]]
}

@test "--print emits both a TTY (-it) and a non-TTY (-i) branch" {
    run bash "$SCRIPT" --print --runtime docker
    [ "$status" -eq 0 ]
    [[ "$output" == *"run --rm -it"* ]]
    [[ "$output" == *"run --rm -i "* ]]
}

@test "--print with a custom --image references that image" {
    run bash "$SCRIPT" --print --runtime docker --image my/gco:dev
    [ "$status" -eq 0 ]
    [[ "$output" == *"my/gco:dev gco"* ]]
}

@test "--print does not create the rc file" {
    run bash "$SCRIPT" --print --runtime docker --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [ ! -f "$RCFILE" ]
}

# -- Runtime detection precedence (docker > finch > podman) -------------------
@test "detect: docker wins when its daemon answers" {
    make_shim "$SHIMDIR" docker 0
    make_shim "$SHIMDIR" finch 0
    make_shim "$SHIMDIR" podman 0
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --print
    [ "$status" -eq 0 ]
    [[ "$output" == *"docker run"* ]]
}

@test "detect: falls back to finch when docker info fails" {
    make_shim "$SHIMDIR" docker 1
    make_shim "$SHIMDIR" finch 0
    make_shim "$SHIMDIR" podman 0
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --print
    [ "$status" -eq 0 ]
    [[ "$output" == *"finch run"* ]]
}

@test "detect: falls back to podman when docker and finch fail" {
    make_shim "$SHIMDIR" docker 1
    make_shim "$SHIMDIR" finch 1
    make_shim "$SHIMDIR" podman 0
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --print
    [ "$status" -eq 0 ]
    [[ "$output" == *"podman run"* ]]
}

@test "detect: no runtime available exits non-zero with guidance" {
    make_shim "$SHIMDIR" docker 1
    make_shim "$SHIMDIR" finch 1
    make_shim "$SHIMDIR" podman 1
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --print
    [ "$status" -ne 0 ]
    [[ "$output" == *"no container runtime"* ]]
}

# -- Overrides ----------------------------------------------------------------
@test "GCO_CONTAINER_RUNTIME overrides detection" {
    make_shim "$SHIMDIR" docker 0
    make_shim "$SHIMDIR" finch 0
    GCO_CONTAINER_RUNTIME=finch PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --print
    [ "$status" -eq 0 ]
    [[ "$output" == *"finch run"* ]]
}

@test "CDK_DOCKER overrides detection" {
    make_shim "$SHIMDIR" docker 0
    make_shim "$SHIMDIR" podman 0
    CDK_DOCKER=podman PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --print
    [ "$status" -eq 0 ]
    [[ "$output" == *"podman run"* ]]
}

@test "--runtime beats GCO_CONTAINER_RUNTIME" {
    make_shim "$SHIMDIR" docker 0
    make_shim "$SHIMDIR" finch 0
    GCO_CONTAINER_RUNTIME=finch PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --print --runtime docker
    [ "$status" -eq 0 ]
    [[ "$output" == *"docker run"* ]]
}

# -- Idempotent rc-file install -----------------------------------------------
@test "install writes exactly one marked block" {
    bash "$SCRIPT" --runtime docker --rc "$RCFILE" >/dev/null
    [ "$(grep -c '# >>> gco >>>' "$RCFILE")" -eq 1 ]
    [ "$(grep -c '# <<< gco <<<' "$RCFILE")" -eq 1 ]
    grep -q 'gco()' "$RCFILE"
}

@test "install is idempotent across repeated runs" {
    bash "$SCRIPT" --runtime docker --rc "$RCFILE" >/dev/null
    bash "$SCRIPT" --runtime docker --rc "$RCFILE" >/dev/null
    [ "$(grep -c '# >>> gco >>>' "$RCFILE")" -eq 1 ]
    [ "$(grep -c '# <<< gco <<<' "$RCFILE")" -eq 1 ]
}

@test "install preserves pre-existing rc content" {
    printf '%s\n' "export FOO=bar" > "$RCFILE"
    bash "$SCRIPT" --runtime docker --rc "$RCFILE" >/dev/null
    grep -q 'export FOO=bar' "$RCFILE"
    [ "$(grep -c '# >>> gco >>>' "$RCFILE")" -eq 1 ]
}

@test "re-running with a different image replaces the block in place" {
    bash "$SCRIPT" --runtime docker --image old/img --rc "$RCFILE" >/dev/null
    bash "$SCRIPT" --runtime docker --image new/img --rc "$RCFILE" >/dev/null
    grep -q 'new/img gco' "$RCFILE"
    ! grep -q 'old/img gco' "$RCFILE"
    [ "$(grep -c '# >>> gco >>>' "$RCFILE")" -eq 1 ]
}

@test "the installed rc block passes bash -n" {
    bash "$SCRIPT" --runtime docker --rc "$RCFILE" >/dev/null
    bash -n "$RCFILE"
}

# -- The emitted function actually runs the runtime correctly -----------------
@test "emitted function forwards args and mounts the workspace (non-TTY branch)" {
    make_shim "$SHIMDIR" docker 0
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    bash "$SCRIPT" --print --runtime docker > "$SHIMDIR/block.sh"
    PATH="$SHIMDIR:$PATH"
    source "$SHIMDIR/block.sh"
    run gco --version
    [ "$status" -eq 0 ]
    grep -q 'run --rm -i' "$GCO_SHIM_LOG"
    grep -q -- '-w /workspace' "$GCO_SHIM_LOG"
    grep -q 'gco --version' "$GCO_SHIM_LOG"
}
