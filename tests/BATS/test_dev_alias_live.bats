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
    image) exit "${FAKE_RT_INSPECT:-0}" ;;
    build)
        if [ "$(grep -c '^build ' "$RT_CALLS")" -le "${FAKE_RT_BUILD_FAILURES:-0}" ]; then
            echo "fake docker: build failed (registry timeout)" >&2
            exit 1
        fi
        ;;
    run)
        # The generated function ends its argv with `gco <args>`; answer the
        # two commands the proof sends through it.
        case "$*" in
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
    [[ "$output" == *"ALL CHECKS PASSED for docker"* ]]
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
