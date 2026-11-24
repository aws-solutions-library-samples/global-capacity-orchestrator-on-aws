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

load 'helpers.sh'

SCRIPT="$REPO_ROOT/scripts/setup-dev-alias.sh"

# Make a fake runtime executable on PATH. It answers `<rt> info` with the exit
# code requested via $3 (so detection can be steered), `<rt> build` with the
# optional exit code in $4 (default 0, so a build failure can be simulated) and
# `<rt> run` with the optional exit code in $5 (default 0; a non-zero code fails
# every container start, socket probes included), exits 0 otherwise, and logs
# every invocation's args to $GCO_SHIM_LOG when set.
#
# A `<rt> run ... --mount type=bind,source=PATH,target=/var/run/docker.sock
# IMAGE sh -c PROBE` is the setup script's socket probe; the shim answers it
# the way the dev image would, steered through environment variables:
#   GCO_SHIM_SOCKETS        space-separated PATH=SERVER pairs: only these paths
#                           answer, each with that /_ping Server header (empty
#                           for none). Unset: every path answers, as Docker
#                           Engine for docker, Libpod for podman, and with no
#                           Server header (finch-daemon) for finch.
#   GCO_SHIM_LABEL_REQUIRED a probe answers only with --security-opt label=disable
#   GCO_SHIM_PROBE_SILENT   the container exits 0 without running the probe
#   GCO_SHIM_INSPECT        exit code of `<rt> image inspect` (default 0)
#   GCO_SHIM_CONTEXT_HOST   what `docker context inspect --format ...` prints
#   GCO_SHIM_PODMAN_REMOTE  what `podman info --format ...` prints
make_shim() {
    local dir="$1" name="$2" code="$3" build_code="${4:-0}" run_code="${5:-0}"
    cat > "$dir/$name" <<SHIM
#!/usr/bin/env bash
if [ -n "\${GCO_SHIM_LOG:-}" ]; then printf '%s\n' "\$*" >> "\$GCO_SHIM_LOG"; fi
if [ "\$1" = "info" ] && [ "\${2:-}" = "--format" ]; then printf '%s\n' "\${GCO_SHIM_PODMAN_REMOTE:-}"; exit $code; fi
if [ "\$1" = "info" ]; then exit $code; fi
if [ "\$1" = "build" ]; then exit $build_code; fi
if [ "\$1" = "image" ] && [ "\${2:-}" = "inspect" ]; then exit "\${GCO_SHIM_INSPECT:-0}"; fi
if [ "\$1" = "context" ] && [ "\${2:-}" = "inspect" ]; then printf '%s\n' "\${GCO_SHIM_CONTEXT_HOST:-}"; exit 0; fi
if [ "\$1" = "run" ]; then
    [ "$run_code" -eq 0 ] || exit $run_code
    case "\$*" in
        *"target=/var/run/docker.sock"*" sh -c "*) ;;
        *) exit 0 ;;
    esac
    [ -z "\${GCO_SHIM_PROBE_SILENT:-}" ] || exit 0
    if [ -n "\${GCO_SHIM_LABEL_REQUIRED:-}" ]; then
        case "\$*" in *"label=disable"*) ;; *) exit 3 ;; esac
    fi
    source_path="\$(printf '%s\n' "\$*" | sed -n 's/.*type=bind,source=\(.*\),target=\/var\/run\/docker.sock.*/\1/p' | head -1)"
    case "$name" in docker) server="Docker/28.0.0" ;; podman) server="Libpod/5.6.0" ;; *) server="" ;; esac
    if [ -n "\${GCO_SHIM_SOCKETS+set}" ]; then
        found=0
        for pair in \${GCO_SHIM_SOCKETS}; do
            if [ "\${pair%%=*}" = "\$source_path" ]; then server="\${pair#*=}"; found=1; fi
        done
        [ "\$found" -eq 1 ] || exit 3
    fi
    [ -z "\$server" ] || printf 'server=%s\n' "\$server"
    echo gco-socket-ok
    exit 0
fi
exit 0
SHIM
    chmod +x "$dir/$name"
}

setup() {
    SHIMDIR="$(mktemp -d)"
    RCFILE="$(mktemp -u)"
    # Every runtime the script may call is a shim unless a test replaces it,
    # so no test depends on (or starts containers with) a real runtime.
    make_shim "$SHIMDIR" docker 0
    make_shim "$SHIMDIR" finch 0
    make_shim "$SHIMDIR" podman 0
    export PATH="$SHIMDIR:$PATH"
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

@test "--print --runtime finch omits the socket when the Finch VM serves none" {
    make_shim "$SHIMDIR" finch 0 0 1
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --print --runtime finch
    [ "$status" -eq 0 ]
    [[ "$output" == *"finch run"* ]]
    [[ "$output" != *"/var/run/docker.sock"* ]]
    [[ "$output" != *"DOCKER_BUILDKIT"* ]]
}

@test "--print --runtime finch mounts finch-daemon's socket and the classic builder" {
    make_shim "$SHIMDIR" finch 0
    run bash "$SCRIPT" --print --runtime finch
    [ "$status" -eq 0 ]
    tty_line="$(printf '%s\n' "$output" | grep -- 'run --rm -it')"
    notty_line="$(printf '%s\n' "$output" | grep -- 'run --rm -i ')"
    for line in "$tty_line" "$notty_line"; do
        [[ "$line" == *"finch run"* ]]
        [[ "$line" == *"-v '/run/finch.sock:/var/run/docker.sock' -e DOCKER_BUILDKIT=0 "* ]]
        [[ "$line" != *"label=disable"* ]]
    done
}

@test "the socket probe uses --mount, so a missing socket is never created" {
    make_shim "$SHIMDIR" finch 0 0 1
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    run bash "$SCRIPT" --print --runtime finch --image my-dev
    [ "$status" -eq 0 ]
    grep -q '^run --rm --mount type=bind,source=/run/finch.sock,target=/var/run/docker.sock my-dev sh -c docker version' "$GCO_SHIM_LOG"
    grep -q '^run --rm --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock my-dev sh -c docker version' "$GCO_SHIM_LOG"
    ! grep -q -- '-v /var/run/docker.sock' "$GCO_SHIM_LOG"
    # finch (nerdctl) has no SELinux labels, so it is never retried without them.
    ! grep -q 'label=disable' "$GCO_SHIM_LOG"
}

@test "Docker Engine keeps BuildKit; Podman and finch-daemon get the classic builder" {
    run bash "$SCRIPT" --print --runtime docker
    [ "$status" -eq 0 ]
    [[ "$output" != *"DOCKER_BUILDKIT"* ]]
    local rt
    for rt in podman finch; do
        run bash "$SCRIPT" --print --runtime "$rt"
        [ "$status" -eq 0 ]
        [[ "$output" == *"-e DOCKER_BUILDKIT=0 "* ]]
    done
}

@test "a docker CLI whose daemon is Podman gets the classic builder too" {
    GCO_SHIM_SOCKETS="/var/run/docker.sock=Libpod/5.6.0" run bash "$SCRIPT" --print --runtime docker
    [ "$status" -eq 0 ]
    [[ "$output" == *"-v '/var/run/docker.sock:/var/run/docker.sock' -e DOCKER_BUILDKIT=0 "* ]]
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

@test "--runtime=, --image= and --rc= take their value after the equals sign" {
    bash "$SCRIPT" --no-build --runtime=docker --image=eq/img:dev "--rc=$RCFILE" >/dev/null
    grep -q 'docker run' "$RCFILE"
    grep -q 'eq/img:dev gco' "$RCFILE"
    [ "$(grep -c '# >>> gco >>>' "$RCFILE")" -eq 1 ]
}

@test "the equals and the two-argument spellings of an option produce the same block" {
    run bash "$SCRIPT" --print --runtime=podman --image=my/gco:dev
    [ "$status" -eq 0 ]
    local spelled_out="$output"
    run bash "$SCRIPT" --print --runtime podman --image my/gco:dev
    [ "$status" -eq 0 ]
    [ "$output" = "$spelled_out" ]
}

@test "an unknown option fails with the option named and a pointer to --help" {
    run bash "$SCRIPT" --print --runtime docker --bogus
    [ "$status" -eq 1 ]
    [[ "$output" == *"error: unknown option: --bogus (try --help)"* ]]
    [ ! -e "$RCFILE" ]
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
# `gco autopilot` lazily installs the selected Claude Code, Codex, or OpenCode
# pin and keeps generated/session state under ~/.claude, ~/.gco, and (OpenCode's
# session database) ~/.local/share/opencode. The emitted function must carry
# all four persistence mounts (npm-global named volume, ~/.claude, ~/.gco, the
# OpenCode named volume) and CLAUDE_CONFIG_DIR on every runtime, or installs and
# sessions evaporate with the --rm container.

@test "emitted block carries the autopilot persistence mounts on every runtime" {
    local rt
    for rt in docker finch podman; do
        run bash "$SCRIPT" --print --runtime "$rt"
        [ "$status" -eq 0 ]
        [[ "$output" == *'-v gco-dev-tools:/root/.npm-global'* ]]
        [[ "$output" == *'.claude:/root/.claude'* ]]
        [[ "$output" == *'.gco:/root/.gco'* ]]
        [[ "$output" == *'-v gco-dev-opencode:/root/.local/share/opencode'* ]]
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
        [[ "$line" == *'gco-dev-opencode:/root/.local/share/opencode'* ]]
        [[ "$line" == *'CLAUDE_CONFIG_DIR=/root/.claude'* ]]
    done
}

@test "OpenCode session state is a named volume, never a host directory mount" {
    # The host's own ~/.local/share/opencode may belong to a different OpenCode
    # release; sharing one SQLite database across versions is not safe, so the
    # function must not bind-mount it (and must not pre-create it either).
    run bash "$SCRIPT" --print --runtime docker
    [ "$status" -eq 0 ]
    [[ "$output" != *'.local/share/opencode:/root/.local/share/opencode'* ]]
    mkdir_line="$(printf '%s\n' "$output" | grep -- 'mkdir -p')"
    [ -n "$mkdir_line" ]
    [[ "$mkdir_line" != *'opencode'* ]]
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
    grep -q -- 'gco-dev-opencode:/root/.local/share/opencode' "$GCO_SHIM_LOG"
    grep -q -- 'CLAUDE_CONFIG_DIR=/root/.claude' "$GCO_SHIM_LOG"
    [ ! -e "$HOME/.local/share/opencode" ]
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

@test "autopilot controls are forwarded by name on every runtime and TTY branch" {
    local rt line name tty_line notty_line
    local names=(
        GCO_AUTOPILOT_ENGINE
        GCO_AUTOPILOT_MODEL
        GCO_AUTOPILOT_CODEX_MODEL
        GCO_AUTOPILOT_OPENCODE_MODEL
        GCO_AUTOPILOT_SMALL_FAST_MODEL
        GCO_AUTOPILOT_CONFIG_DIR
    )

    for rt in docker finch podman; do
        run bash "$SCRIPT" --print --runtime "$rt"
        [ "$status" -eq 0 ]
        tty_line="$(printf '%s\n' "$output" | grep -- 'run --rm -it')"
        notty_line="$(printf '%s\n' "$output" | grep -- 'run --rm -i ')"
        [ -n "$tty_line" ]
        [ -n "$notty_line" ]

        for line in "$tty_line" "$notty_line"; do
            [[ "$line" == *"$rt run"* ]]
            for name in "${names[@]}"; do
                [[ "$line" == *"-e $name "* ]]
                [[ "$line" != *"-e $name="* ]]
            done
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

# -- Shell profile selection ---------------------------------------------------
# Without --rc the script writes into the login shell's own rc file, chosen by
# the basename of $SHELL. HOME is pointed at a scratch directory so the test
# never touches the real profile.

@test "zsh users get ~/.zshrc without --rc" {
    run env HOME="$SHIMDIR" SHELL=/bin/zsh bash "$SCRIPT" --runtime docker --no-build
    [ "$status" -eq 0 ]
    [[ "$output" == *"Shell profile     : $SHIMDIR/.zshrc"* ]]
    grep -qF '# >>> gco >>>' "$SHIMDIR/.zshrc"
    [ ! -e "$SHIMDIR/.bashrc" ]
    [ ! -e "$SHIMDIR/.profile" ]
}

@test "bash users get ~/.bashrc without --rc" {
    run env HOME="$SHIMDIR" SHELL=/usr/local/bin/bash bash "$SCRIPT" --runtime docker --no-build
    [ "$status" -eq 0 ]
    [[ "$output" == *"Shell profile     : $SHIMDIR/.bashrc"* ]]
    grep -qF '# >>> gco >>>' "$SHIMDIR/.bashrc"
    [ ! -e "$SHIMDIR/.zshrc" ]
}

@test "any other POSIX shell falls back to ~/.profile without --rc" {
    run env HOME="$SHIMDIR" SHELL=/bin/ksh bash "$SCRIPT" --runtime docker --no-build
    [ "$status" -eq 0 ]
    [[ "$output" == *"Shell profile     : $SHIMDIR/.profile"* ]]
    grep -qF '# >>> gco >>>' "$SHIMDIR/.profile"
}

@test "an empty SHELL falls back to ~/.profile too" {
    # Bash fills an unset SHELL from the login shell before the script runs,
    # so an empty value is the only way the ${SHELL:-sh} default is reached.
    run env SHELL= HOME="$SHIMDIR" bash "$SCRIPT" --runtime docker --no-build
    [ "$status" -eq 0 ]
    [[ "$output" == *"Shell profile     : $SHIMDIR/.profile"* ]]
    grep -qF '# >>> gco >>>' "$SHIMDIR/.profile"
}

@test "--uninstall without --rc removes the block from the shell's own rc file" {
    env HOME="$SHIMDIR" SHELL=/bin/zsh bash "$SCRIPT" --runtime docker --no-build >/dev/null
    grep -qF '# >>> gco >>>' "$SHIMDIR/.zshrc"
    run env HOME="$SHIMDIR" SHELL=/bin/zsh bash "$SCRIPT" --uninstall
    [ "$status" -eq 0 ]
    ! grep -qF '# >>> gco >>>' "$SHIMDIR/.zshrc"
}

@test "install summary describes the podman socket mount" {
    XDG_RUNTIME_DIR=/run/user/4242 run bash "$SCRIPT" --runtime podman --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Socket mount      : /run/user/4242/podman/podman.sock -> /var/run/docker.sock (classic builder: DOCKER_BUILDKIT=0)"* ]]
    [[ "$output" == *"This daemon has no BuildKit API, so the function also sets DOCKER_BUILDKIT=0:"* ]]
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

# -- Install-time notes --------------------------------------------------------
@test "install on an SELinux-enforcing host labels the bind mounts and says so" {
    make_shim "$SHIMDIR" docker 0
    # selinuxenabled exits 0 only on an enforcing host; fake exactly that.
    printf '#!/usr/bin/env bash\nexit 0\n' > "$SHIMDIR/selinuxenabled"
    chmod +x "$SHIMDIR/selinuxenabled"
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"SELinux           : enforcing host detected; bind mounts carry the ',z' shared label"* ]]
    grep -q -- ':ro,z' "$RCFILE"
}

@test "install notes an AWS file-path variable that the mount may not cover" {
    make_shim "$SHIMDIR" docker 0
    PATH="$SHIMDIR:$PATH" AWS_CONFIG_FILE="$SHIMDIR/elsewhere/config" \
        run bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Note: you have an AWS file-path variable set."* ]]
    [[ "$output" == *"only readable in the container when it lives under ~/.aws"* ]]
}

@test "install on Finch without finch-daemon explains the missing socket and the host-side builder" {
    make_shim "$SHIMDIR" finch 0 0 1
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --runtime finch --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Socket mount      : none (this Finch VM serves no Docker-compatible socket)"* ]]
    [[ "$output" == *"Note: this Finch VM serves no Docker-compatible socket the container can reach"* ]]
    [[ "$output" == *"From v1.8.0, Finch runs finch-daemon in its VM; after upgrading Finch, re-run"* ]]
    [[ "$output" == *"CDK_DOCKER=finch gco stacks deploy-all -y"* ]]
    [[ "$output" == *"so the pinned aws-cdk-lib / cdk-nag are used."* ]]
    ! grep -q '/var/run/docker.sock' "$RCFILE"
}

@test "install with a runtime the script has no socket for omits the mount and names the host-side builder" {
    make_shim "$SHIMDIR" nerdctl 0
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --runtime nerdctl --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Socket mount      : none (nerdctl has no host socket to share)"* ]]
    [[ "$output" == *"Note: this script knows no socket of nerdctl to share with the container, so"* ]]
    [[ "$output" == *"run them on the host with nerdctl as the builder:"* ]]
    [[ "$output" == *"CDK_DOCKER=nerdctl gco stacks deploy-all -y"* ]]
    [[ "$output" != *"finch-daemon"* ]]
    [[ "$output" != *"VM serves no"* ]]
    ! grep -q '/var/run/docker.sock' "$RCFILE"
}

@test "install on Finch with finch-daemon mounts its socket and says builds run in the container" {
    make_shim "$SHIMDIR" finch 0
    run bash "$SCRIPT" --runtime finch --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Socket mount      : /run/finch.sock -> /var/run/docker.sock (classic builder: DOCKER_BUILDKIT=0)"* ]]
    [[ "$output" == *"so build-heavy commands such as 'gco stacks deploy-all' and"* ]]
    [[ "$output" == *"so the function also sets DOCKER_BUILDKIT=0"* ]]
    [[ "$output" != *"CDK_DOCKER=finch gco stacks deploy-all -y"* ]]
    grep -q -- "-v '/run/finch.sock:/var/run/docker.sock' -e DOCKER_BUILDKIT=0 " "$RCFILE"
}

@test "Finch's VM link to finch-daemon is the fallback when /run/finch.sock does not answer" {
    GCO_SHIM_SOCKETS="/var/run/docker.sock=" run bash "$SCRIPT" --runtime finch --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Socket mount      : /var/run/docker.sock -> /var/run/docker.sock (classic builder: DOCKER_BUILDKIT=0)"* ]]
}

@test "on Finch the socket is checked after the image build, against the built image" {
    make_shim "$SHIMDIR" finch 0
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" --runtime finch --rc "$RCFILE"
    [ "$status" -eq 0 ]
    build_line="$(grep -n '^build ' "$GCO_SHIM_LOG" | head -1 | cut -d: -f1)"
    probe_line="$(grep -n '^run --rm --mount ' "$GCO_SHIM_LOG" | head -1 | cut -d: -f1)"
    [ -n "$build_line" ]
    [ -n "$probe_line" ]
    [ "$build_line" -lt "$probe_line" ]
}

# -- Socket discovery ---------------------------------------------------------
@test "docker: /var/run/docker.sock wins when it answers" {
    GCO_SHIM_CONTEXT_HOST=unix:///run/user/1000/docker.sock run bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Socket mount      : /var/run/docker.sock -> /var/run/docker.sock (BuildKit)"* ]]
    grep -q -- "-v '/var/run/docker.sock:/var/run/docker.sock' -w /workspace" "$RCFILE"
}

@test "docker: rootless Docker's context socket is the fallback" {
    GCO_SHIM_SOCKETS="/run/user/1000/docker.sock=Docker/28.0.0" GCO_SHIM_CONTEXT_HOST=unix:///run/user/1000/docker.sock \
        run bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Socket mount      : /run/user/1000/docker.sock -> /var/run/docker.sock (BuildKit)"* ]]
    grep -q -- "-v '/run/user/1000/docker.sock:/var/run/docker.sock' " "$RCFILE"
}

@test "docker: a unix DOCKER_HOST is tried before the context, and tcp endpoints never" {
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    GCO_SHIM_SOCKETS="/srv/docker/api.sock=Docker/28.0.0" DOCKER_HOST=unix:///srv/docker/api.sock GCO_SHIM_CONTEXT_HOST=tcp://10.0.0.5:2376 \
        run bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Socket mount      : /srv/docker/api.sock -> /var/run/docker.sock (BuildKit)"* ]]
    ! grep -q 'source=tcp' "$GCO_SHIM_LOG"
}

@test "podman: the socket podman info reports is tried first" {
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    GCO_SHIM_PODMAN_REMOTE=unix:///run/user/501/podman/podman.sock XDG_RUNTIME_DIR=/run/user/4242 \
        run bash "$SCRIPT" --runtime podman --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Socket mount      : /run/user/501/podman/podman.sock -> /var/run/docker.sock (classic builder: DOCKER_BUILDKIT=0)"* ]]
    [ "$(grep -c 'source=/run/user/501/podman/podman.sock' "$GCO_SHIM_LOG")" -eq 1 ]
    ! grep -q 'source=/run/user/4242' "$GCO_SHIM_LOG"
}

@test "podman: a reported path without a scheme is used as is, and duplicates are probed once" {
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    GCO_SHIM_SOCKETS="/run/podman/podman.sock=Libpod/5.6.0" GCO_SHIM_PODMAN_REMOTE=/run/user/4242/podman/podman.sock XDG_RUNTIME_DIR=/run/user/4242 \
        run bash "$SCRIPT" --runtime podman --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Socket mount      : /run/podman/podman.sock -> /var/run/docker.sock"* ]]
    [ "$(grep -c 'source=/run/user/4242/podman/podman.sock,target=/var/run/docker.sock localhost/gco-dev' "$GCO_SHIM_LOG")" -eq 2 ]
}

@test "podman: a socket SELinux keeps the container off is retried without label separation" {
    GCO_SHIM_LABEL_REQUIRED=1 run bash "$SCRIPT" --runtime podman --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"(classic builder: DOCKER_BUILDKIT=0), SELinux label separation off"* ]]
    [[ "$output" == *"also passes '--security-opt label=disable'"* ]]
    grep -q -- "podman.sock:/var/run/docker.sock' --security-opt label=disable -e DOCKER_BUILDKIT=0 " "$RCFILE"
}

@test "podman: no answering API names the service to start and keeps the host builder" {
    GCO_SHIM_SOCKETS="" XDG_RUNTIME_DIR=/run/user/4242 run bash "$SCRIPT" --runtime podman --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Socket mount      : none (no Docker-compatible API answered at /run/user/4242/podman/podman.sock, /run/podman/podman.sock)"* ]]
    [[ "$output" == *"systemctl --user enable --now podman.socket"* ]]
    [[ "$output" == *"CDK_DOCKER=podman gco stacks deploy-all -y"* ]]
    ! grep -q '/var/run/docker.sock' "$RCFILE"
}

@test "docker: no answering API says to start the daemon or pass --socket" {
    GCO_SHIM_SOCKETS="" run bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Socket mount      : none (no Docker-compatible API answered at /var/run/docker.sock)"* ]]
    [[ "$output" == *"Start the daemon, or pass --socket PATH with the path its containers see"* ]]
    [[ "$output" != *"podman.socket"* ]]
}

@test "a container that exits 0 without answering the probe is not a socket" {
    GCO_SHIM_PROBE_SILENT=1 run bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Socket mount      : none (no Docker-compatible API answered at /var/run/docker.sock)"* ]]
}

@test "--socket PATH is the only path probed" {
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    run bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE" --socket /opt/rd/docker.sock
    [ "$status" -eq 0 ]
    [[ "$output" == *"Socket mount      : /opt/rd/docker.sock -> /var/run/docker.sock (BuildKit)"* ]]
    [ "$(grep -c 'target=/var/run/docker.sock' "$GCO_SHIM_LOG")" -eq 1 ]
    run bash "$SCRIPT" --print --runtime docker --socket=/opt/rd/docker.sock
    [[ "$output" == *"-v '/opt/rd/docker.sock:/var/run/docker.sock' "* ]]
}

@test "--socket needs a value, a single line, and no --no-socket" {
    run bash "$SCRIPT" --runtime docker --socket
    [ "$status" -ne 0 ]
    [[ "$output" == *"--socket needs a value"* ]]
    run bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE" --socket $'/a\n/b'
    [ "$status" -ne 0 ]
    [[ "$output" == *"--socket must not contain line breaks"* ]]
    run bash "$SCRIPT" --runtime docker --no-build --rc "$RCFILE" --socket /a --no-socket
    [ "$status" -ne 0 ]
    [[ "$output" == *"--socket and --no-socket cannot be combined"* ]]
    [ ! -e "$RCFILE" ]
}

@test "--no-socket probes nothing, mounts nothing and names the host-side builder" {
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    run bash "$SCRIPT" --runtime podman --no-build --rc "$RCFILE" --no-socket
    [ "$status" -eq 0 ]
    [[ "$output" == *"Socket mount      : none (--no-socket)"* ]]
    [[ "$output" == *"Note: the function shares no runtime socket with the container, so it cannot"* ]]
    [[ "$output" == *"CDK_DOCKER=podman gco stacks deploy-all -y"* ]]
    ! grep -q 'target=/var/run/docker.sock' "$GCO_SHIM_LOG"
    ! grep -q '/var/run/docker.sock' "$RCFILE"
}

@test "--print before the image is built shares no socket and says why" {
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    GCO_SHIM_INSPECT=1 run bash "$SCRIPT" --print --runtime docker
    [ "$status" -eq 0 ]
    [[ "$output" == *"'gco-dev' is not built yet, so no runtime socket could be checked"* ]]
    [[ "$output" != *"/var/run/docker.sock"* ]]
    ! grep -q 'target=/var/run/docker.sock' "$GCO_SHIM_LOG"
}

@test "--print with --no-socket skips the image check" {
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    GCO_SHIM_INSPECT=1 run bash "$SCRIPT" --print --runtime docker --no-socket
    [ "$status" -eq 0 ]
    [[ "$output" != *"is not built yet"* ]]
    ! grep -q '^image inspect' "$GCO_SHIM_LOG"
}
