#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for .github/scripts/dev_alias_live.sh
# -----------------------------------------------------------------------------
# Static checks, argument parsing, --help, the --no-runtime refusal (which masks
# the runtimes with PATH stubs, so it runs hermetically) — and the runtime mode
# itself, run against a fixture checkout with a faked container runtime that
# answers `info`, `build`, `image inspect` and `run` the way the proof expects.
# The real setup-dev-alias.sh is what installs the function; only the runtime
# behind it is faked. The live container proofs (docker / finch / podman build
# gco-dev and run the generated function for real) execute in
# integration-tests.yml, not here — they need a real runtime and a multi-minute
# image build.
#
# Run:  bats tests/BATS/test_dev_alias_live.bats
# -----------------------------------------------------------------------------

load 'helpers.sh'

SCRIPT="$REPO_ROOT/.github/scripts/dev_alias_live.sh"

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

# -- Runtime mode against a faked runtime -------------------------------------
# The fixture is a throwaway "checkout" holding (a symlink to) the real
# setup-dev-alias.sh; HOME is throwaway too, because the proof pre-creates
# ~/.aws for the generated function's mount. The fake runtime logs every
# invocation and is steered through environment variables:
#   FAKE_RT_INFO      exit status of `<rt> info`            (default 0)
#   FAKE_RT_INSPECT   exit status of `<rt> image inspect`   (default 0)
#   FAKE_RT_BUILD_FAILURES  how many `<rt> build` calls fail before succeeding
#   FAKE_RT_VERSION_REPLY   what `gco --version` prints through the function
#                           ("" for nothing; "fail" to exit 1)
#   FAKE_RT_VALIDATE_REPLY  what `gco dag validate` prints ("fail" to exit 1)
#   FAKE_RT_SOCKET          "none" makes every socket probe of the setup script
#                           fail, so the function shares no socket
#   FAKE_RT_DETECT_REPLY    what the runtime-detection proof prints in a
#                           container with the socket ("fail" to exit 125)
#   FAKE_RT_DETECT_NO_SOCKET_REPLY  the same in a container without it
#   FAKE_RT_PROOF_BUILD_FAILURES    how many builds through the socket fail
#   FAKE_RT_PROOF_INSPECT   exit status of `<rt> image inspect` for the image
#                           built through the socket (default 0)

make_live_fixture() {
    FIXTURE="$BATS_TEST_TMPDIR/checkout"
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    FAKE_HOME="$BATS_TEST_TMPDIR/home"
    export RT_CALLS="$BATS_TEST_TMPDIR/runtime-calls"
    : > "$RT_CALLS"
    mkdir -p "$FIXTURE/scripts" "$FAKE_BIN" "$FAKE_HOME"
    ln -s "$REPO_ROOT/scripts/setup-dev-alias.sh" "$FIXTURE/scripts/setup-dev-alias.sh"
    # The setup script builds <its checkout>/Dockerfile.dev; the fake runtime
    # never reads it, but the setup script checks that it exists.
    printf 'FROM scratch\n' > "$FIXTURE/Dockerfile.dev"
    write_stub "$FAKE_BIN" docker <<'FAKE_RUNTIME'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$RT_CALLS"
case "${1:-}" in
    info) exit "${FAKE_RT_INFO:-0}" ;;
    --version) echo "Docker version 28.0.0, build fake" ;;
    image)
        case "${2:-} ${3:-}" in
            "inspect gco-dev-alias-live-build:"*) exit "${FAKE_RT_PROOF_INSPECT:-0}" ;;
            "rm "*) exit 0 ;;
        esac
        exit "${FAKE_RT_INSPECT:-0}"
        ;;
    build)
        if [ "$(grep -c '^build ' "$RT_CALLS")" -le "${FAKE_RT_BUILD_FAILURES:-0}" ]; then
            echo "fake docker: build failed (registry timeout)" >&2
            exit 1
        fi
        ;;
    run)
        # The generated function ends its argv with `gco <args>`; answer the
        # two commands the proof sends through it, the setup script's socket
        # probes, and the commands the socket proof runs in the same container.
        case "$*" in
            *"target=/var/run/docker.sock"*" sh -c "*)
                [ "${FAKE_RT_SOCKET:-}" != "none" ] || exit 3
                printf 'server=Docker/28.0.0\ngco-socket-ok\n'
                ;;
            *" python -c "*)
                case "$*" in
                    *":/var/run/docker.sock "*) reply="${FAKE_RT_DETECT_REPLY-docker}" ;;
                    *) reply="${FAKE_RT_DETECT_NO_SOCKET_REPLY-None
No container runtime is reachable from the gco-dev container: the gco function mounted no runtime socket}" ;;
                esac
                if [ "$reply" = "fail" ]; then
                    echo "fake docker: cannot start container" >&2
                    exit 125
                fi
                printf '%s\n' "$reply"
                ;;
            *" docker build "*)
                if [ "$(grep -c ' docker build ' "$RT_CALLS")" -le "${FAKE_RT_PROOF_BUILD_FAILURES:-0}" ]; then
                    echo "fake docker: failed to resolve source metadata (i/o timeout)" >&2
                    exit 1
                fi
                ;;
            *" gco --version")
                case "${FAKE_RT_VERSION_REPLY-gco 7.6.4}" in
                    fail) echo "fake docker: cannot start container" >&2; exit 125 ;;
                    *) printf '%s\n' "${FAKE_RT_VERSION_REPLY-gco 7.6.4}" ;;
                esac
                ;;
            *" gco dag validate "*)
                case "${FAKE_RT_VALIDATE_REPLY-DAG dev-alias-live-probe is valid}" in
                    fail) echo "fake docker: cannot start container" >&2; exit 125 ;;
                    *) printf '%s\n' "${FAKE_RT_VALIDATE_REPLY-DAG dev-alias-live-probe is valid}" ;;
                esac
                ;;
        esac
        ;;
esac
exit 0
FAKE_RUNTIME
    stub_noop "$FAKE_BIN" sleep
}

run_live() {
    # run_live [VAR=value ...] -- <script args...>
    local vars=()
    while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
        vars+=("$1")
        shift
    done
    [ "$#" -gt 0 ] && shift
    run env PATH="$FAKE_BIN:$PATH" HOME="$FAKE_HOME" \
        GCO_DEV_ALIAS_LIVE_REPO_ROOT="$FIXTURE" "${vars[@]}" bash "$SCRIPT" "$@"
}

@test "the runtime proof builds the image, installs the function and runs both checks through it" {
    make_live_fixture
    run_live -- docker

    [ "$status" -eq 0 ]
    [[ "$output" == *"=== preflight: docker ==="* ]]
    [[ "$output" == *"Docker version 28.0.0"* ]]
    [[ "$output" == *"setup-dev-alias.sh builds gco-dev from Dockerfile.dev with docker"* ]]
    [[ "$output" == *"gco --version -> gco 7.6.4"* ]]
    [[ "$output" == *"DAG dev-alias-live-probe is valid"* ]]
    [[ "$output" == *"Socket mount      : /var/run/docker.sock -> /var/run/docker.sock (BuildKit)"* ]]
    [[ "$output" == *"detect_container_runtime() in the container -> docker"* ]]
    [[ "$output" == *"docker image inspect gco-dev-alias-live-build:"*"-> present in the runtime store"* ]]
    [[ "$output" == *"  | No container runtime is reachable from the gco-dev container"* ]]
    [[ "$output" == *"ALL CHECKS PASSED for docker"* ]]
    # The socket proof built with CDK's platform through the function's own
    # container, then removed its image; the --no-socket function ran without
    # the socket mount.
    grep -qE -- "^run .* -v /var/run/docker.sock:/var/run/docker.sock .* docker build --platform linux/amd64 -t gco-dev-alias-live-build:[0-9]+ " "$RT_CALLS"
    grep -q '^image rm gco-dev-alias-live-build:' "$RT_CALLS"
    [ "$(grep -c ' python -c ' "$RT_CALLS")" -eq 2 ]
    [ "$(grep ' python -c ' "$RT_CALLS" | grep -c 'docker.sock')" -eq 1 ]
    # The build ran once, with the fixture as its context; the function then
    # ran the CLI from the fixture root with the DAG path forwarded as given.
    [ "$(grep -c '^build ' "$RT_CALLS")" -eq 1 ]
    grep -q -- "-t gco-dev" "$RT_CALLS"
    grep -qE -- "^run .* -v ${FIXTURE}:/workspace .* gco --version$" "$RT_CALLS"
    grep -qE -- "^run .* gco dag validate \.gco_dev_alias_live\.[0-9]+/ci-dag\.yaml$" "$RT_CALLS"
    # The throwaway DAG fixture was removed from the checkout on exit, and
    # ~/.aws was pre-created for the mount.
    [ -z "$(compgen -G "$FIXTURE/.gco_dev_alias_live.*" || true)" ]
    [ -d "$FAKE_HOME/.aws" ]
}

@test "--skip-build reuses an existing image and tells the setup script not to build" {
    make_live_fixture
    run_live -- docker --skip-build --image my-dev:latest

    [ "$status" -eq 0 ]
    [[ "$output" == *"using existing image: my-dev:latest (--skip-build); setup-dev-alias.sh --no-build"* ]]
    [[ "$output" == *"ALL CHECKS PASSED for docker"* ]]
    grep -q '^image inspect my-dev:latest$' "$RT_CALLS"
    ! grep -q '^build ' "$RT_CALLS"
    grep -qE -- "^run .* my-dev:latest gco --version$" "$RT_CALLS"
}

@test "--image=NAME is accepted as well" {
    make_live_fixture
    run_live -- --image=other-dev docker --skip-build

    [ "$status" -eq 0 ]
    grep -q '^image inspect other-dev$' "$RT_CALLS"
}

@test "--skip-build fails when the image is not present" {
    make_live_fixture
    run_live FAKE_RT_INSPECT=1 -- docker --skip-build

    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: --skip-build set but image 'gco-dev' was not found in docker"* ]]
}

@test "a runtime that is not on PATH fails preflight" {
    make_live_fixture
    rm -f "$FAKE_BIN/docker"
    local tools="$BATS_TEST_TMPDIR/tools"
    path_without "$tools" docker
    run env PATH="$FAKE_BIN:$tools" HOME="$FAKE_HOME" GCO_DEV_ALIAS_LIVE_REPO_ROOT="$FIXTURE" \
        bash "$SCRIPT" docker

    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: docker is not on PATH"* ]]
}

@test "a runtime whose daemon does not answer fails preflight" {
    make_live_fixture
    run_live FAKE_RT_INFO=1 -- docker

    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: docker is installed but 'docker info' does not answer"* ]]
    ! grep -q '^build ' "$RT_CALLS"
}

@test "a missing setup script is reported before anything runs" {
    make_live_fixture
    rm -f "$FIXTURE/scripts/setup-dev-alias.sh"
    run_live -- docker

    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: setup script not found or not executable: ${FIXTURE}/scripts/setup-dev-alias.sh"* ]]
    [ ! -s "$RT_CALLS" ]
}

@test "a setup run that fails once is retried with a growing delay" {
    make_live_fixture
    # The setup script retries the build three times itself, so three failed
    # builds are one failed setup run; the fourth build succeeds.
    run_live FAKE_RT_BUILD_FAILURES=3 -- docker

    [ "$status" -eq 0 ]
    [[ "$output" == *"setup-dev-alias.sh failed (attempt 1/3); retrying in 15s"* ]]
    [[ "$output" != *"setup-dev-alias.sh failed (attempt 2/3)"* ]]
    [[ "$output" == *"ALL CHECKS PASSED for docker"* ]]
    [ "$(grep -c '^build ' "$RT_CALLS")" -eq 4 ]
}

@test "a build that keeps failing stops after three attempts" {
    make_live_fixture
    run_live FAKE_RT_BUILD_FAILURES=99 -- docker

    [ "$status" -eq 1 ]
    [[ "$output" == *"retrying in 15s"* ]]
    [[ "$output" == *"retrying in 30s"* ]]
    [[ "$output" == *"FAIL: setup-dev-alias.sh failed to build gco-dev / install the gco function for docker (after 3 attempts)"* ]]
    [[ "$output" != *"ALL CHECKS PASSED"* ]]
}

@test "--skip-build fails when the setup script cannot install the function" {
    make_live_fixture
    # The setup script refuses an image name it cannot write into the profile
    # safely; the fake runtime happily "finds" any image, so the refusal is the
    # setup script's own.
    run_live -- docker --skip-build --image "$(printf 'gco-dev\nrm -rf ~')"

    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: setup-dev-alias.sh failed to install the gco function for docker"* ]]
}

@test "a setup script that writes no function block is caught" {
    make_live_fixture
    rm -f "$FIXTURE/scripts/setup-dev-alias.sh"
    printf '#!/usr/bin/env bash\nexit 0\n' > "$FIXTURE/scripts/setup-dev-alias.sh"
    chmod +x "$FIXTURE/scripts/setup-dev-alias.sh"
    run_live -- docker --skip-build

    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: setup script did not write a gco function block"* ]]
}

@test "a CLI that fails through the function is reported with its output" {
    make_live_fixture
    run_live FAKE_RT_VERSION_REPLY=fail -- docker

    [ "$status" -eq 1 ]
    [[ "$output" == *"  | fake docker: cannot start container"* ]]
    [[ "$output" == *"FAIL: 'gco --version' failed through the generated function"* ]]
}

@test "a CLI that prints nothing through the function is reported" {
    make_live_fixture
    run_live FAKE_RT_VERSION_REPLY= -- docker

    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: 'gco --version' produced no output"* ]]
}

@test "a DAG validation that fails through the function is reported with its output" {
    make_live_fixture
    run_live FAKE_RT_VALIDATE_REPLY=fail -- docker

    [ "$status" -eq 1 ]
    [[ "$output" == *"gco --version -> gco 7.6.4"* ]]
    [[ "$output" == *"FAIL: 'gco dag validate .gco_dev_alias_live."*"/ci-dag.yaml' failed through the generated function"* ]]
}

@test "a DAG validation that does not say the DAG is valid fails the proof" {
    make_live_fixture
    run_live FAKE_RT_VALIDATE_REPLY="manifest not found: probe-job.yaml" -- docker

    [ "$status" -eq 1 ]
    [[ "$output" == *"  | manifest not found: probe-job.yaml"* ]]
    [[ "$output" == *"FAIL: expected 'is valid' (workspace bind mount or cwd not wired correctly)"* ]]
}

@test "--no-runtime fails a setup script that refuses without the guidance" {
    make_live_fixture
    rm -f "$FIXTURE/scripts/setup-dev-alias.sh"
    printf '#!/usr/bin/env bash\necho "something else went wrong" >&2\nexit 1\n' > "$FIXTURE/scripts/setup-dev-alias.sh"
    chmod +x "$FIXTURE/scripts/setup-dev-alias.sh"
    run_live -- --no-runtime

    [ "$status" -eq 1 ]
    [[ "$output" == *"  | something else went wrong"* ]]
    [[ "$output" == *"FAIL: expected 'no container runtime' guidance in the output"* ]]
}

@test "--no-runtime fails a setup script that refuses but still writes the function" {
    make_live_fixture
    rm -f "$FIXTURE/scripts/setup-dev-alias.sh"
    cat > "$FIXTURE/scripts/setup-dev-alias.sh" <<'FAKE_SETUP'
#!/usr/bin/env bash
# Honour --rc <file> the way the real script does, then misbehave: refuse
# with the right words but leave a function block behind anyway.
while [ "$#" -gt 0 ]; do
    case "$1" in
        --rc) printf '# >>> gco >>>\ngco() { :; }\n# <<< gco <<<\n' > "$2"; shift 2 ;;
        *) shift ;;
    esac
done
echo "error: no container runtime found" >&2
exit 1
FAKE_SETUP
    chmod +x "$FIXTURE/scripts/setup-dev-alias.sh"
    run_live -- --no-runtime

    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: rc must not contain a gco function block when no runtime is available"* ]]
}

# -- The runtime socket proof -------------------------------------------------
@test "a function that shares no socket fails the socket proof with the setup summary" {
    make_live_fixture
    run_live FAKE_RT_SOCKET=none -- docker --skip-build
    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: the generated function shares no runtime socket with the container (Socket mount      : none (no Docker-compatible API answered at /var/run/docker.sock))"* ]]
}

@test "a function whose run line does not end in gco \"\$@\" cannot be turned into a runner" {
    make_live_fixture
    rm -f "$FIXTURE/scripts/setup-dev-alias.sh"
    cat > "$FIXTURE/scripts/setup-dev-alias.sh" <<'FAKE_SETUP'
#!/usr/bin/env bash
while [ "$#" -gt 0 ]; do
    case "$1" in
        --rc)
            cat > "$2" <<'BLOCK'
# >>> gco >>>
gco() {
    docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD:/workspace" -w /workspace gco-dev gco "${@}"
}
# <<< gco <<<
BLOCK
            shift 2 ;;
        *) shift ;;
    esac
done
echo "  Socket mount      : /var/run/docker.sock -> /var/run/docker.sock (BuildKit)"
FAKE_SETUP
    chmod +x "$FIXTURE/scripts/setup-dev-alias.sh"
    run_live -- docker --skip-build
    [ "$status" -eq 1 ]
    [[ "$output" == *"DAG dev-alias-live-probe is valid"* ]]
    [[ "$output" == *"FAIL: could not derive a command runner from the generated function"* ]]
}

@test "runtime detection that cannot run in the container is reported with its output" {
    make_live_fixture
    run_live FAKE_RT_DETECT_REPLY=fail -- docker --skip-build
    [ "$status" -eq 1 ]
    [[ "$output" == *"  | fake docker: cannot start container"* ]]
    [[ "$output" == *"FAIL: the CLI's runtime detection could not run in the dev container"* ]]
}

@test "runtime detection that finds nothing through the socket fails the proof" {
    make_live_fixture
    run_live FAKE_RT_DETECT_REPLY=None -- docker --skip-build
    [ "$status" -eq 1 ]
    [[ "$output" == *"detect_container_runtime() in the container -> None"* ]]
    [[ "$output" == *"FAIL: the CLI found no container runtime through the shared socket"* ]]
}

@test "a Dockerfile.dev without a FROM line leaves nothing to build the proof on" {
    make_live_fixture
    printf '# no base image\n' > "$FIXTURE/Dockerfile.dev"
    run_live -- docker --skip-build
    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: no FROM line in ${FIXTURE}/Dockerfile.dev to build the proof image on"* ]]
}

@test "a build through the socket that fails once is retried" {
    make_live_fixture
    run_live FAKE_RT_PROOF_BUILD_FAILURES=1 -- docker --skip-build
    [ "$status" -eq 0 ]
    [[ "$output" == *"  | fake docker: failed to resolve source metadata (i/o timeout)"* ]]
    [[ "$output" == *"the build through the socket failed (attempt 1/3); retrying in 15s"* ]]
    [[ "$output" == *"ALL CHECKS PASSED for docker"* ]]
}

@test "a build through the socket that keeps failing stops after three attempts" {
    make_live_fixture
    run_live FAKE_RT_PROOF_BUILD_FAILURES=99 -- docker --skip-build
    [ "$status" -eq 1 ]
    [[ "$output" == *"retrying in 30s"* ]]
    [[ "$output" == *"FAIL: 'docker build' through the shared socket failed (after 3 attempts)"* ]]
}

@test "an image the host runtime cannot see fails the proof" {
    make_live_fixture
    run_live FAKE_RT_PROOF_INSPECT=1 -- docker --skip-build
    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: the image built through the socket is not in docker's image store, so CDK could not push it"* ]]
}

# The --no-socket proof runs the setup script a second time; a wrapper around
# the real one misbehaves only for that run.
wrap_setup_for_no_socket() {
    local behaviour="$1"
    rm -f "$FIXTURE/scripts/setup-dev-alias.sh"
    cat > "$FIXTURE/scripts/setup-dev-alias.sh" <<WRAPPER
#!/usr/bin/env bash
args=()
for arg in "\$@"; do
    if [ "\$arg" = "--no-socket" ]; then
        case "$behaviour" in
            fail) echo "fake setup: refused" >&2; exit 1 ;;
            ignore) continue ;;
        esac
    fi
    args+=("\$arg")
done
exec bash "$REPO_ROOT/scripts/setup-dev-alias.sh" "\${args[@]}"
WRAPPER
    chmod +x "$FIXTURE/scripts/setup-dev-alias.sh"
    printf 'FROM scratch\n' > "$FIXTURE/Dockerfile.dev"
}

@test "a --no-socket install that fails is reported" {
    make_live_fixture
    wrap_setup_for_no_socket fail
    run_live -- docker --skip-build
    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: setup-dev-alias.sh --no-socket failed to install the gco function for docker"* ]]
}

@test "a --no-socket function that still shares the socket fails the proof" {
    make_live_fixture
    wrap_setup_for_no_socket ignore
    run_live -- docker --skip-build
    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: the --no-socket function still shares a runtime socket"* ]]
}

@test "runtime detection that cannot run without the socket is reported" {
    make_live_fixture
    run_live FAKE_RT_DETECT_NO_SOCKET_REPLY=fail -- docker --skip-build
    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: the CLI's runtime detection could not run in the --no-socket dev container"* ]]
}

@test "a runtime found without the socket fails the --no-socket proof" {
    make_live_fixture
    run_live FAKE_RT_DETECT_NO_SOCKET_REPLY=docker -- docker --skip-build
    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: the CLI found a container runtime although the function shares no socket"* ]]
}

@test "the --no-socket proof needs the CLI's dev-container advice" {
    make_live_fixture
    run_live FAKE_RT_DETECT_NO_SOCKET_REPLY=None -- docker --skip-build
    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: expected the dev container's 'no runtime' advice from the CLI"* ]]
}

@test "--expect-socket accepts the socket discovery chose" {
    make_live_fixture
    run_live -- docker --skip-build --expect-socket /var/run/docker.sock
    [ "$status" -eq 0 ]
    [[ "$output" == *"ALL CHECKS PASSED for docker"* ]]
}

@test "--expect-socket refuses any other socket" {
    make_live_fixture
    run_live -- docker --skip-build --expect-socket=/run/user/1000/docker.sock
    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: expected the function to share /run/user/1000/docker.sock (Socket mount      : /var/run/docker.sock -> /var/run/docker.sock (BuildKit))"* ]]
}

@test "--expect-socket without a value exits non-zero" {
    run bash "$SCRIPT" docker --expect-socket
    [ "$status" -ne 0 ]
    [[ "$output" == *"--expect-socket needs a value"* ]]
}
