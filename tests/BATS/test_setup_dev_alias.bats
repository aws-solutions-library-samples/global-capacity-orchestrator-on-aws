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
# code requested via $3 (so detection can be steered) and `<rt> build` with the
# optional exit code in $4 (default 0, so a build failure can be simulated),
# exits 0 otherwise, and logs every invocation's args to $GCO_SHIM_LOG when set.
make_shim() {
    local dir="$1" name="$2" code="$3" build_code="${4:-0}"
    cat > "$dir/$name" <<SHIM
#!/usr/bin/env bash
if [ -n "\${GCO_SHIM_LOG:-}" ]; then printf '%s\n' "\$*" >> "\$GCO_SHIM_LOG"; fi
if [ "\$1" = "info" ]; then exit $code; fi
if [ "\$1" = "build" ]; then exit $build_code; fi
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
@test "setup-dev-alias.sh remains executable" {
    [ -x "$SCRIPT" ]
}

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

@test "--print --runtime podman qualifies a bare image with localhost/" {
    # podman won't resolve a bare locally-built name; it needs localhost/.
    run bash "$SCRIPT" --print --runtime podman
    [ "$status" -eq 0 ]
    [[ "$output" == *"localhost/gco-dev gco"* ]]
}

@test "--print --runtime podman leaves a registry-qualified image untouched" {
    run bash "$SCRIPT" --print --runtime podman --image my/gco:dev
    [ "$status" -eq 0 ]
    [[ "$output" == *"my/gco:dev gco"* ]]
    [[ "$output" != *"localhost/my/gco:dev"* ]]
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

@test "generated runtime cannot inject commands into the sourced profile" {
    marker="$SHIMDIR/runtime-injected"
    malicious="docker; touch $marker"
    bash "$SCRIPT" --print --runtime "$malicious" > "$SHIMDIR/runtime-block.sh" 2>/dev/null
    bash -n "$SHIMDIR/runtime-block.sh"
    source "$SHIMDIR/runtime-block.sh"
    ! gco --version >/dev/null 2>&1
    [ ! -e "$marker" ]
}

@test "generated image cannot inject commands into the sourced profile" {
    marker="$SHIMDIR/image-injected"
    make_shim "$SHIMDIR" docker 0
    PATH="$SHIMDIR:$PATH"
    bash "$SCRIPT" --print --runtime docker --image "gco; touch $marker" > "$SHIMDIR/image-block.sh"
    bash -n "$SHIMDIR/image-block.sh"
    source "$SHIMDIR/image-block.sh"
    run gco --version
    [ "$status" -eq 0 ]
    [ ! -e "$marker" ]
}

@test "generated podman socket mount quotes XDG_RUNTIME_DIR" {
    marker="$SHIMDIR/socket-injected"
    make_shim "$SHIMDIR" podman 0
    PATH="$SHIMDIR:$PATH"
    malicious_dir="$SHIMDIR/socket'; touch $marker; #"
    XDG_RUNTIME_DIR="$malicious_dir" \
        bash "$SCRIPT" --print --runtime podman > "$SHIMDIR/socket-block.sh"
    bash -n "$SHIMDIR/socket-block.sh"
    source "$SHIMDIR/socket-block.sh"
    run gco --version
    [ "$status" -eq 0 ]
    [ ! -e "$marker" ]
}

@test "line breaks cannot inject profile markers across reinstalls" {
    payload=$'gco-dev\n# <<< gco <<<\nprintf PROFILE_MARKER_INJECTION'
    run bash "$SCRIPT" --runtime docker --no-build --image "$payload" --rc "$RCFILE"
    [ "$status" -ne 0 ]
    [[ "$output" == *"must not contain line breaks"* ]]
    [ ! -e "$RCFILE" ]
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
    bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE" >/dev/null
    [ "$(grep -c '# >>> gco >>>' "$RCFILE")" -eq 1 ]
    [ "$(grep -c '# <<< gco <<<' "$RCFILE")" -eq 1 ]
    grep -q 'gco()' "$RCFILE"
}

@test "install is idempotent across repeated runs" {
    bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE" >/dev/null
    bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE" >/dev/null
    [ "$(grep -c '# >>> gco >>>' "$RCFILE")" -eq 1 ]
    [ "$(grep -c '# <<< gco <<<' "$RCFILE")" -eq 1 ]
}

@test "install preserves pre-existing rc content" {
    printf '%s\n' "export FOO=bar" > "$RCFILE"
    bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE" >/dev/null
    grep -q 'export FOO=bar' "$RCFILE"
    [ "$(grep -c '# >>> gco >>>' "$RCFILE")" -eq 1 ]
}

@test "re-running with a different image replaces the block in place" {
    bash "$SCRIPT" --runtime docker --no-build --image old/img --rc "$RCFILE" >/dev/null
    bash "$SCRIPT" --runtime docker --no-build --image new/img --rc "$RCFILE" >/dev/null
    grep -q 'new/img gco' "$RCFILE"
    ! grep -q 'old/img gco' "$RCFILE"
    [ "$(grep -c '# >>> gco >>>' "$RCFILE")" -eq 1 ]
}

@test "the installed rc block passes bash -n" {
    bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE" >/dev/null
    bash -n "$RCFILE"
}

# -- Image build (always builds unless --no-build / --print) ------------------
@test "install builds the dev image by default with the resolved runtime" {
    make_shim "$SHIMDIR" docker 0
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --runtime docker --rc "$RCFILE"
    [ "$status" -eq 0 ]
    # The build ran: it tagged the image and pointed at Dockerfile.dev.
    grep -q '^build ' "$GCO_SHIM_LOG"
    grep -q -- '-t gco-dev' "$GCO_SHIM_LOG"
    grep -qE -- '-f .*Dockerfile\.dev' "$GCO_SHIM_LOG"
    # ...and the function was still installed afterwards.
    [ "$(grep -c '# >>> gco >>>' "$RCFILE")" -eq 1 ]
}

@test "--no-build installs the function without building" {
    make_shim "$SHIMDIR" docker 0
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    ! grep -q '^build ' "$GCO_SHIM_LOG"
    [ "$(grep -c '# >>> gco >>>' "$RCFILE")" -eq 1 ]
}

@test "--print never builds the image" {
    make_shim "$SHIMDIR" docker 0
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --print --runtime docker
    [ "$status" -eq 0 ]
    ! grep -q '^build ' "$GCO_SHIM_LOG"
}

@test "a failed image build aborts before writing the rc file" {
    # info succeeds so the runtime is accepted; build fails. Retries are
    # disabled so the assertion is about the abort, not the backoff.
    make_shim "$SHIMDIR" docker 0 1
    GCO_DEV_IMAGE_BUILD_ATTEMPTS=1 PATH="$SHIMDIR:$PATH" \
        run bash "$SCRIPT" --runtime docker --rc "$RCFILE"
    [ "$status" -ne 0 ]
    [[ "$output" == *"failed to build"* ]]
    [ ! -f "$RCFILE" ]
}

# -- Build retry (transient registry failures are self-healing) ---------------
#
# The build's first act is resolving the Dockerfile.dev base image from Docker
# Hub. A single i/o timeout there used to fail the whole job, so the build is
# retried; these tests pin both halves of that contract.

# A runtime shim whose `build` fails the first $1 times and then succeeds,
# tracking attempts in a counter file so the retry loop is observable.
make_flaky_build_shim() {
    local dir="$1" name="$2" failures="$3"
    cat > "$dir/$name" <<SHIM
#!/usr/bin/env bash
if [ -n "\${GCO_SHIM_LOG:-}" ]; then printf '%s\n' "\$*" >> "\$GCO_SHIM_LOG"; fi
if [ "\$1" = "info" ]; then exit 0; fi
if [ "\$1" = "build" ]; then
    count=0
    [ -f "$dir/build_count" ] && count="\$(cat "$dir/build_count")"
    count=\$((count + 1))
    printf '%s' "\$count" > "$dir/build_count"
    if [ "\$count" -le $failures ]; then exit 1; fi
    exit 0
fi
exit 0
SHIM
    chmod +x "$dir/$name"
}

@test "a transient build failure is retried and then succeeds" {
    make_flaky_build_shim "$SHIMDIR" docker 1
    GCO_DEV_IMAGE_BUILD_ATTEMPTS=3 GCO_DEV_IMAGE_BUILD_RETRY_DELAY=0 \
        PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --runtime docker --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"build attempt 1/3 failed"* ]]
    [[ "$output" == *"is ready"* ]]
    # Exactly two build invocations: the failure and the retry that worked.
    [ "$(cat "$SHIMDIR/build_count")" = "2" ]
    [ -f "$RCFILE" ]
}

@test "a persistently failing build stops after exhausting the attempts" {
    make_flaky_build_shim "$SHIMDIR" docker 99
    GCO_DEV_IMAGE_BUILD_ATTEMPTS=2 GCO_DEV_IMAGE_BUILD_RETRY_DELAY=0 \
        PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --runtime docker --rc "$RCFILE"
    [ "$status" -ne 0 ]
    [[ "$output" == *"failed to build"* ]]
    [ "$(cat "$SHIMDIR/build_count")" = "2" ]
    [ ! -f "$RCFILE" ]
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

# -- Autopilot persistence mounts ----------------------------------------------
#
# `gco autopilot` installs the pinned Claude Code on first use and keeps
# session state under ~/.claude and ~/.gco. The emitted function must carry
# the three persistence mounts (npm-global named volume, ~/.claude, ~/.gco)
# and CLAUDE_CONFIG_DIR on every runtime, or those installs and sessions
# evaporate with the --rm container.

@test "emitted block carries the autopilot persistence mounts on every runtime" {
    local rt
    for rt in docker finch podman; do
        run bash "$SCRIPT" --print --runtime "$rt"
        [ "$status" -eq 0 ]
        [[ "$output" == *'-v gco-dev-tools:/root/.npm-global'* ]]
        [[ "$output" == *'.claude:/root/.claude'* ]]
        [[ "$output" == *'.gco:/root/.gco'* ]]
        [[ "$output" == *'-e CLAUDE_CONFIG_DIR=/root/.claude'* ]]
    done
}

@test "emitted block carries the mounts in both TTY and non-TTY branches" {
    run bash "$SCRIPT" --print --runtime docker
    [ "$status" -eq 0 ]
    tty_line="$(echo "$output" | grep -- 'run --rm -it')"
    notty_line="$(echo "$output" | grep -- 'run --rm -i ')"
    for line in "$tty_line" "$notty_line"; do
        [[ "$line" == *'gco-dev-tools:/root/.npm-global'* ]]
        [[ "$line" == *'CLAUDE_CONFIG_DIR=/root/.claude'* ]]
    done
}

@test "emitted function pre-creates the host state dirs before running" {
    make_shim "$SHIMDIR" docker 0
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    bash "$SCRIPT" --print --runtime docker > "$SHIMDIR/block.sh"
    # Point HOME at a scratch dir so the function's mkdir -p is observable
    # (and never touches the real test-runner home).
    HOME="$SHIMDIR/home"
    mkdir -p "$HOME"
    PATH="$SHIMDIR:$PATH"
    source "$SHIMDIR/block.sh"
    run gco --version
    [ "$status" -eq 0 ]
    [ -d "$HOME/.claude" ]
    [ -d "$HOME/.gco" ]
    grep -q -- 'gco-dev-tools:/root/.npm-global' "$GCO_SHIM_LOG"
    grep -q -- 'CLAUDE_CONFIG_DIR=/root/.claude' "$GCO_SHIM_LOG"
}

# -- AWS credential support ----------------------------------------------------
#
# A mounted ~/.aws used to be the only credential source that survived the
# container boundary: AWS_PROFILE, exported SSO/assume-role sessions, static
# keys, and web-identity setups were all dropped, which reads as "credentials
# not found" — or silently targets a different account than the host shell.
# The bare `-e NAME` form forwards each variable only when the caller has it
# set, so an unset variable never becomes an empty override in the container.

@test "emitted block forwards the profile and region credential env vars" {
    run bash "$SCRIPT" --print --runtime docker
    [ "$status" -eq 0 ]
    [[ "$output" == *'-e AWS_PROFILE'* ]]
    [[ "$output" == *'-e AWS_REGION'* ]]
    [[ "$output" == *'-e AWS_DEFAULT_REGION'* ]]
}

@test "emitted block forwards static-key and session credential env vars" {
    run bash "$SCRIPT" --print --runtime docker
    [ "$status" -eq 0 ]
    [[ "$output" == *'-e AWS_ACCESS_KEY_ID'* ]]
    [[ "$output" == *'-e AWS_SECRET_ACCESS_KEY'* ]]
    [[ "$output" == *'-e AWS_SESSION_TOKEN'* ]]
}

@test "emitted block forwards role and web-identity credential env vars" {
    run bash "$SCRIPT" --print --runtime docker
    [ "$status" -eq 0 ]
    [[ "$output" == *'-e AWS_ROLE_ARN'* ]]
    [[ "$output" == *'-e AWS_WEB_IDENTITY_TOKEN_FILE'* ]]
}

@test "credential env vars are forwarded by name only, never with a value" {
    # `-e NAME=` would override the container value with an empty string when
    # the host has the variable unset; `-e NAME` passes through only when set.
    run bash "$SCRIPT" --print --runtime docker
    [ "$status" -eq 0 ]
    [[ "$output" != *'-e AWS_PROFILE='* ]]
    [[ "$output" != *'-e AWS_ACCESS_KEY_ID='* ]]
}

@test "credential forwarding is present on every runtime and both TTY branches" {
    local rt
    for rt in docker finch podman; do
        run bash "$SCRIPT" --print --runtime "$rt"
        [ "$status" -eq 0 ]
        tty_line="$(echo "$output" | grep -- 'run --rm -it')"
        notty_line="$(echo "$output" | grep -- 'run --rm -i ')"
        for line in "$tty_line" "$notty_line"; do
            [[ "$line" == *'-e AWS_PROFILE'* ]]
            [[ "$line" == *'-e AWS_SESSION_TOKEN'* ]]
        done
    done
}

@test "an exported AWS_PROFILE reaches the runtime invocation" {
    make_shim "$SHIMDIR" docker 0
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    bash "$SCRIPT" --print --runtime docker > "$SHIMDIR/block.sh"
    HOME="$SHIMDIR/home"
    mkdir -p "$HOME"
    PATH="$SHIMDIR:$PATH"
    source "$SHIMDIR/block.sh"
    AWS_PROFILE=some-sso-profile run gco status
    [ "$status" -eq 0 ]
    grep -q -- '-e AWS_PROFILE' "$GCO_SHIM_LOG"
}

@test "~/.aws is mounted read-only by default" {
    run bash "$SCRIPT" --print --runtime docker
    [ "$status" -eq 0 ]
    [[ "$output" == *'.aws:/root/.aws:ro'* ]]
}

@test "--aws-writable mounts ~/.aws read-write for in-container sso login" {
    run bash "$SCRIPT" --print --runtime docker --aws-writable
    [ "$status" -eq 0 ]
    [[ "$output" == *'.aws:/root/.aws:rw'* ]]
    [[ "$output" != *'.aws:/root/.aws:ro'* ]]
}

@test "the emitted function pre-creates ~/.aws so env-only auth has a mount source" {
    # Hosts that authenticate purely through env vars, OIDC, or instance
    # metadata have no ~/.aws; without this the runtime materialises a
    # root-owned directory on the host instead.
    make_shim "$SHIMDIR" docker 0
    bash "$SCRIPT" --print --runtime docker > "$SHIMDIR/block.sh"
    HOME="$SHIMDIR/home"
    mkdir -p "$HOME"
    [ ! -d "$HOME/.aws" ]
    PATH="$SHIMDIR:$PATH"
    source "$SHIMDIR/block.sh"
    run gco --version
    [ "$status" -eq 0 ]
    [ -d "$HOME/.aws" ]
}

# -- Uninstall -----------------------------------------------------------------

@test "--uninstall removes the managed block and preserves other rc content" {
    printf 'export KEEP_ME=1\n' > "$RCFILE"
    bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE"
    grep -qF '# >>> gco >>>' "$RCFILE"
    run bash "$SCRIPT" --uninstall --rc "$RCFILE"
    [ "$status" -eq 0 ]
    ! grep -qF '# >>> gco >>>' "$RCFILE"
    ! grep -qF 'gco()' "$RCFILE"
    grep -qF 'export KEEP_ME=1' "$RCFILE"
}

@test "--uninstall is a no-op success when no block is installed" {
    printf 'export KEEP_ME=1\n' > "$RCFILE"
    run bash "$SCRIPT" --uninstall --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Nothing to remove"* ]]
    grep -qF 'export KEEP_ME=1' "$RCFILE"
}

@test "--uninstall needs no container runtime" {
    # Removing an rc block must work on a machine whose runtime is gone or
    # broken, so uninstall runs before any detection or build.
    printf 'export KEEP_ME=1\n' > "$RCFILE"
    bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE"
    # Keep the base system dirs (bash, awk, grep, mktemp) but drop the dirs
    # container runtimes install into, so no runtime is discoverable.
    run env PATH="$SHIMDIR:/usr/bin:/bin" bash "$SCRIPT" --uninstall --rc "$RCFILE"
    [ "$status" -eq 0 ]
    ! command -v docker >/dev/null 2>&1 || true
    ! grep -qF '# >>> gco >>>' "$RCFILE"
}

# -- fish shell ----------------------------------------------------------------

@test "fish shell fails loudly instead of writing an unusable block" {
    # fish cannot parse POSIX function syntax, and ~/.profile (the old
    # fallback) is a file fish never reads — a silent no-op install.
    run env SHELL=/opt/homebrew/bin/fish bash "$SCRIPT" --runtime docker --no-build
    [ "$status" -ne 0 ]
    [[ "$output" == *"fish"* ]]
    [[ "$output" == *"--print"* ]]
}

@test "fish users can still target an explicit rc file with --rc" {
    run env SHELL=/usr/bin/fish bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    grep -qF '# >>> gco >>>' "$RCFILE"
}
