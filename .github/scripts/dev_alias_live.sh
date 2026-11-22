#!/usr/bin/env bash
#
# dev_alias_live.sh — LIVE proof that scripts/setup-dev-alias.sh generates a
# working `gco` shell function against a real container runtime, using the real
# gco-dev image.
#
# tests/BATS/test_setup_dev_alias.bats mocks the runtimes and only inspects the
# emitted text. This script closes that gap end to end: it runs
# scripts/setup-dev-alias.sh to build the real gco-dev image and install the
# generated function into a throwaway rc, sources it in a fresh shell, and
# proves through that function:
#   * `gco --version`                — the real CLI runs (and the arg reaches it)
#   * `gco dag validate <rel>/ci-dag.yaml` — an offline command that reads files
#                                      from the mounted workspace via a
#                                      *relative* path, proving arg-forwarding,
#                                      the $PWD -> /workspace bind mount, and
#                                      cwd=/workspace all at once.
#   * the runtime socket             — the function shares the runtime's
#                                      Docker-compatible API socket; in the same
#                                      container the CLI's own runtime detection
#                                      finds a daemon, and a `docker build` with
#                                      CDK's --platform and a RUN --mount leaves
#                                      its image in the runtime's own store,
#                                      which is where CDK pushes images from.
#   * --no-socket                    — a function installed without the socket
#                                      finds no runtime, and the CLI says how
#                                      the dev container gets one.
#
# Dockerfile.dev installs the CLI editable (`pip install -e .`) at /workspace,
# so the generated function must be run from the project directory: it mounts
# $PWD at /workspace, and gco resolves its source there. We therefore run from
# the repo root and drop the DAG fixture into a throwaway subdirectory of it.
#
# Modes:
#   dev_alias_live.sh <docker|finch|podman> [--skip-build] [--image NAME]
#                     [--expect-socket PATH]
#   dev_alias_live.sh --no-runtime
#
# --expect-socket PATH additionally requires the function to share exactly
# that socket, so a CI job pins which candidate socket discovery chose.
#
# Privilege note: on Linux, finch talks to a root-owned daemon, so the finch CI
# job invokes this via sudo. docker (docker group) and podman (rootless) run it
# as the normal user. This script never calls sudo itself.
set -euo pipefail

# The checkout to prove: normally the one this script lives in. The BATS suite
# points GCO_DEV_ALIAS_LIVE_REPO_ROOT at a disposable fixture (carrying the
# setup script and answering runtimes on PATH) so the tracked script runs in
# place without writing its DAG fixture into a real checkout.
REPO_ROOT="$(cd "${GCO_DEV_ALIAS_LIVE_REPO_ROOT:-$(dirname "${BASH_SOURCE[0]}")/../..}" && pwd)"
SETUP="$REPO_ROOT/scripts/setup-dev-alias.sh"

RUNTIME=""
MODE="runtime"
IMAGE="gco-dev"
SKIP_BUILD=0
EXPECT_SOCKET=""

die()  { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
note() { printf '\n=== %s ===\n' "$*"; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-runtime) MODE="none"; shift ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --image) [ "$#" -ge 2 ] || die "--image needs a value"; IMAGE="$2"; shift 2 ;;
        --image=*) IMAGE="${1#*=}"; shift ;;
        --expect-socket) [ "$#" -ge 2 ] || die "--expect-socket needs a value"; EXPECT_SOCKET="$2"; shift 2 ;;
        --expect-socket=*) EXPECT_SOCKET="${1#*=}"; shift ;;
        docker|finch|podman) RUNTIME="$1"; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "usage: dev_alias_live.sh <docker|finch|podman> [--skip-build] [--image NAME] [--expect-socket PATH] | --no-runtime (got: $1)" ;;
    esac
done

[ -x "$SETUP" ] || die "setup script not found or not executable: $SETUP"

WORK="$(mktemp -d)"
FIXTURE_DIR=""   # a throwaway subdir of the repo; set + cleaned up below
BUILD_TAG=""     # the image the socket proof builds; removed on exit
cleanup() {
    rm -rf "$WORK" 2>/dev/null || true
    [ -n "$FIXTURE_DIR" ] && rm -rf "$FIXTURE_DIR" 2>/dev/null
    [ -n "$BUILD_TAG" ] && "$RUNTIME" image rm "$BUILD_TAG" >/dev/null 2>&1
    return 0
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Mode: no-runtime — prove graceful refusal.
# ---------------------------------------------------------------------------
if [ "$MODE" = "none" ]; then
    note "no-runtime refusal"
    # Mask any real runtimes with stubs that fail `<rt> info`, so detection sees
    # "installed but not answering" for all three — the same code path as "not
    # installed at all". The rest of PATH still resolves awk/mktemp/etc.
    stub="$WORK/stub-bin"
    mkdir -p "$stub"
    for rt in docker finch podman; do
        printf '#!/bin/sh\nexit 1\n' >"$stub/$rt"
        chmod +x "$stub/$rt"
    done
    rc="$WORK/rc-none"
    set +e
    out="$(PATH="$stub:$PATH" GCO_CONTAINER_RUNTIME='' CDK_DOCKER='' "$SETUP" --rc "$rc" 2>&1)"
    code=$?
    set -e
    printf '%s\n' "$out" | sed 's/^/  | /'
    [ "$code" -ne 0 ] || die "expected a non-zero exit when no runtime answers (got 0)"
    printf '%s\n' "$out" | grep -qi 'no container runtime' \
        || die "expected 'no container runtime' guidance in the output"
    if [ -f "$rc" ] && grep -q '>>> gco >>>' "$rc"; then
        die "rc must not contain a gco function block when no runtime is available"
    fi
    printf 'PASS: script refused with guidance and wrote no gco function.\n'
    exit 0
fi

# ---------------------------------------------------------------------------
# Mode: runtime — preflight.
# ---------------------------------------------------------------------------
[ -n "$RUNTIME" ] || die "a runtime (docker|finch|podman) or --no-runtime is required"

note "preflight: $RUNTIME"
command -v "$RUNTIME" >/dev/null 2>&1 || die "$RUNTIME is not on PATH"
"$RUNTIME" info >/dev/null 2>&1 || die "$RUNTIME is installed but '$RUNTIME info' does not answer"
"$RUNTIME" --version || true

# ---------------------------------------------------------------------------
# Setup GCO: setup-dev-alias.sh builds the dev image and installs the function.
# ---------------------------------------------------------------------------
# Building the image is the setup script's own job, so the normal path lets it
# do exactly that: this is the end-to-end proof that `setup-dev-alias.sh` builds
# gco-dev *and* wires up the alias in a single run. --skip-build instead reuses
# an already-built image and tells the script to skip its build.
rc="$WORK/rc"
if [ "$SKIP_BUILD" -eq 1 ]; then
    note "using existing image: $IMAGE (--skip-build); setup-dev-alias.sh --no-build"
    "$RUNTIME" image inspect "$IMAGE" >/dev/null 2>&1 \
        || die "--skip-build set but image '$IMAGE' was not found in $RUNTIME"
    "$SETUP" --runtime "$RUNTIME" --image "$IMAGE" --no-build --rc "$rc" >"$WORK/setup.log" \
        || die "setup-dev-alias.sh failed to install the gco function for $RUNTIME"
else
    note "setup GCO: setup-dev-alias.sh builds $IMAGE from Dockerfile.dev with $RUNTIME and installs the function"
    # The build pulls the base image from Docker Hub, whose registry endpoints
    # intermittently time out on GitHub runners. Retry the whole setup run
    # (build + rc write, both idempotent; completed layers stay cached) so a
    # transient registry blip does not fail the job — or, in the podman job,
    # masquerade as an OCI-runtime configuration failure and burn one of its
    # crun/runc attempts.
    build_ok=0
    retry_delay=15
    for attempt in 1 2 3; do
        if "$SETUP" --runtime "$RUNTIME" --image "$IMAGE" --rc "$rc" | tee "$WORK/setup.log"; then
            build_ok=1
            break
        fi
        if [ "$attempt" -lt 3 ]; then
            printf 'setup-dev-alias.sh failed (attempt %s/3); retrying in %ss (usually a transient registry/network error)\n' \
                "$attempt" "$retry_delay" >&2
            sleep "$retry_delay"
            retry_delay=$((retry_delay * 2))
        fi
    done
    [ "$build_ok" -eq 1 ] \
        || die "setup-dev-alias.sh failed to build $IMAGE / install the gco function for $RUNTIME (after 3 attempts)"
fi
grep -q '>>> gco >>>' "$rc" || die "setup script did not write a gco function block"

# ---------------------------------------------------------------------------
# Workspace fixture. The dev image's editable install needs the repo at
# /workspace, so we run from the repo root (the function mounts $PWD) and drop
# the DAG fixture into a throwaway subdir of it. The DAG references its manifest
# by a path relative to cwd; DagDefinition.validate() checks both the DAG and
# the manifest exist, so a successful validate proves the bind mount surfaced
# the files and that cwd is /workspace.
# ---------------------------------------------------------------------------
FIXTURE_REL=".gco_dev_alias_live.$$"
FIXTURE_DIR="$REPO_ROOT/$FIXTURE_REL"
mkdir -p "$FIXTURE_DIR"
printf '# placeholder job manifest for the dev-alias live proof\n' >"$FIXTURE_DIR/probe-job.yaml"
cat >"$FIXTURE_DIR/ci-dag.yaml" <<YAML
name: dev-alias-live-probe
steps:
  - name: probe
    manifest: $FIXTURE_REL/probe-job.yaml
YAML

# Run under the real $HOME so that rootless podman's image store
# (~/.local/share/containers) is the same one the build above wrote to.
# Overriding HOME here would point podman at an empty store and it would try
# to *pull* the locally-built image (docker is daemon-wide and finch is
# rootful, so neither is HOME-sensitive — but podman is).
mkdir -p "$HOME/.aws"   # the generated function bind-mounts ~/.aws read-only

# ---------------------------------------------------------------------------
# Proof 1: the real CLI runs through the generated function (run from the repo
# root so the editable install resolves cli/ + gco/ at /workspace).
# ---------------------------------------------------------------------------
note "prove the real gco CLI runs through the generated function"
if ! ver="$(cd "$REPO_ROOT" && bash --noprofile --norc -c ". '$rc'; gco --version" 2>&1)"; then
    printf '%s\n' "$ver" | sed 's/^/  | /'
    die "'gco --version' failed through the generated function"
fi
printf '  gco --version -> %s\n' "$ver"
[ -n "$ver" ] || die "'gco --version' produced no output"

# ---------------------------------------------------------------------------
# Proof 2: arg-forwarding + workspace bind mount + cwd, via an offline command.
# The relative path resolves only if $PWD was mounted at /workspace and cwd is
# /workspace; gco dag validate then reads both the DAG and its manifest.
# ---------------------------------------------------------------------------
note "prove arg-forwarding + workspace bind mount + cwd via 'gco dag validate'"
if ! out="$(cd "$REPO_ROOT" && bash --noprofile --norc -c ". '$rc'; gco dag validate '$FIXTURE_REL/ci-dag.yaml'" 2>&1)"; then
    printf '%s\n' "$out" | sed 's/^/  | /'
    die "'gco dag validate $FIXTURE_REL/ci-dag.yaml' failed through the generated function"
fi
printf '%s\n' "$out" | sed 's/^/  | /'
printf '%s\n' "$out" | grep -qi 'is valid' \
    || die "expected 'is valid' (workspace bind mount or cwd not wired correctly)"

# ---------------------------------------------------------------------------
# Proof 3: the function shares a runtime socket that the container can build
# through. setup-dev-alias.sh found the socket by probing from inside the dev
# image. Here the CLI's own runtime detection must find a daemon in a container
# started exactly as the function starts it, and an image built there must land
# in the runtime's own store. gco_dev_run is the generated function with the
# trailing `gco` dropped, so it runs any command in that container.
# ---------------------------------------------------------------------------
note "prove the function shares a runtime socket the container builds through"
socket_line="$(grep -m1 'Socket mount' "$WORK/setup.log" | sed 's/^ *//' || true)"
printf '  %s\n' "${socket_line:-Socket mount: (not reported)}"
grep -q -- ':/var/run/docker.sock' "$rc" \
    || die "the generated function shares no runtime socket with the container (${socket_line:-no summary})"
if [ -n "$EXPECT_SOCKET" ] && ! grep -qF -- "-v '$EXPECT_SOCKET:/var/run/docker.sock'" "$rc"; then
    die "expected the function to share $EXPECT_SOCKET (${socket_line:-no summary})"
fi
command_runner() {
    if ! grep -q ' gco "\$@"$' "$1"; then
        sed 's/^/  | /' "$1"
        die "could not derive a command runner from the generated function"
    fi
    sed -n '/^gco() {/,/^}/p' "$1" | sed -e 's/^gco() {/gco_dev_run() {/' -e 's/ gco "\$@"$/ "$@"/' >"$2"
}
in_container() {
    (cd "$REPO_ROOT" && bash --noprofile --norc -c ". '$1'; gco_dev_run \"\$@\"" gco_dev_run "${@:2}")
}
run_fn="$WORK/gco-dev-run.sh"
command_runner "$rc" "$run_fn"
detect_py='from cli._container_runtime import container_runtime_error_message as message, detect_container_runtime as detect; runtime = detect(); print(runtime); print("" if runtime else message())'
# stdout only: some runtimes (Finch) warn on stderr for every container start.
if ! detected="$(in_container "$run_fn" python -c "$detect_py" 2>"$WORK/detect.err")"; then
    sed 's/^/  | /' "$WORK/detect.err"
    printf '%s\n' "$detected" | sed 's/^/  | /'
    die "the CLI's runtime detection could not run in the dev container"
fi
printf '  detect_container_runtime() in the container -> %s\n' "$(printf '%s\n' "$detected" | head -1)"
[ "$(printf '%s\n' "$detected" | head -1)" = "docker" ] \
    || die "the CLI found no container runtime through the shared socket"
base_image="$(sed -n 's/^FROM \([^ ]*\).*/\1/p' "$REPO_ROOT/Dockerfile.dev" | head -1)"
[ -n "$base_image" ] || die "no FROM line in $REPO_ROOT/Dockerfile.dev to build the proof image on"
printf 'FROM %s\nRUN --mount=type=cache,target=/var/cache/gco-proof echo cached > /var/cache/gco-proof/marker && cat /var/cache/gco-proof/marker\nCOPY probe-job.yaml /probe-job.yaml\n' \
    "$base_image" >"$FIXTURE_DIR/Dockerfile.build-proof"
BUILD_TAG="gco-dev-alias-live-build:$$"
built=0
retry_delay=15
for attempt in 1 2 3; do
    if out="$(in_container "$run_fn" docker build --platform linux/amd64 -t "$BUILD_TAG" -f "$FIXTURE_REL/Dockerfile.build-proof" "$FIXTURE_REL" 2>&1)"; then
        built=1
        break
    fi
    printf '%s\n' "$out" | tail -15 | sed 's/^/  | /'
    if [ "$attempt" -lt 3 ]; then
        printf 'the build through the socket failed (attempt %s/3); retrying in %ss (usually a transient registry error)\n' \
            "$attempt" "$retry_delay" >&2
        sleep "$retry_delay"
        retry_delay=$((retry_delay * 2))
    fi
done
[ "$built" -eq 1 ] || die "'docker build' through the shared socket failed (after 3 attempts)"
printf '  docker build --platform linux/amd64 (RUN --mount) through the socket -> %s\n' "$BUILD_TAG"
"$RUNTIME" image inspect "$BUILD_TAG" >/dev/null 2>&1 \
    || die "the image built through the socket is not in $RUNTIME's image store, so CDK could not push it"
printf '  %s image inspect %s -> present in the runtime store\n' "$RUNTIME" "$BUILD_TAG"

# ---------------------------------------------------------------------------
# Proof 4: a function installed with --no-socket shares none, the CLI in its
# container finds no runtime, and it says how the dev container gets one.
# ---------------------------------------------------------------------------
note "prove --no-socket shares no socket and the CLI explains the dev container's options"
rc_no_socket="$WORK/rc-no-socket"
"$SETUP" --runtime "$RUNTIME" --image "$IMAGE" --no-build --no-socket --rc "$rc_no_socket" >/dev/null \
    || die "setup-dev-alias.sh --no-socket failed to install the gco function for $RUNTIME"
if grep -q -- ':/var/run/docker.sock' "$rc_no_socket"; then
    die "the --no-socket function still shares a runtime socket"
fi
run_fn_no_socket="$WORK/gco-dev-run-no-socket.sh"
command_runner "$rc_no_socket" "$run_fn_no_socket"
if ! detected="$(in_container "$run_fn_no_socket" python -c "$detect_py" 2>"$WORK/detect.err")"; then
    sed 's/^/  | /' "$WORK/detect.err"
    printf '%s\n' "$detected" | sed 's/^/  | /'
    die "the CLI's runtime detection could not run in the --no-socket dev container"
fi
printf '%s\n' "$detected" | sed 's/^/  | /'
[ "$(printf '%s\n' "$detected" | head -1)" = "None" ] \
    || die "the CLI found a container runtime although the function shares no socket"
printf '%s\n' "$detected" | grep -q 'No container runtime is reachable from the gco-dev container' \
    || die "expected the dev container's 'no runtime' advice from the CLI"

note "ALL CHECKS PASSED for $RUNTIME"
