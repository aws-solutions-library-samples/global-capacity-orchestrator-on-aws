#!/usr/bin/env bash
#
# dev_alias_live.sh — LIVE proof that scripts/setup-dev-alias.sh generates a
# working `gco` shell function against a real container runtime, using the real
# gco-dev image.
#
# tests/BATS/test_setup_dev_alias.bats mocks the runtimes and only inspects the
# emitted text. This script closes that gap end to end: it builds the real
# gco-dev image ("setup GCO"), installs the generated function into a throwaway
# rc, sources it in a fresh shell, and proves through that function:
#   * `gco --version`                — the real CLI runs (and the arg reaches it)
#   * `gco dag validate ci-dag.yaml` — an offline command that reads files from
#                                      the mounted workspace via a *relative*
#                                      path, proving arg-forwarding, the
#                                      $PWD -> /workspace bind mount, and
#                                      cwd=/workspace all at once.
#
# Modes:
#   dev_alias_live.sh <docker|finch|podman> [--skip-build] [--image NAME]
#       Build (or reuse, with --skip-build) the dev image and run the proof.
#   dev_alias_live.sh --no-runtime
#       Prove the script refuses (non-zero exit + guidance, no rc block) when no
#       container runtime answers.
#
# Privilege note: on Linux, finch talks to a root-owned daemon, so the finch CI
# job invokes this via sudo. docker (docker group) and podman (rootless) run it
# as the normal user. This script never calls sudo itself.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SETUP="$REPO_ROOT/scripts/setup-dev-alias.sh"
DOCKERFILE="$REPO_ROOT/Dockerfile.dev"

RUNTIME=""
MODE="runtime"
IMAGE="gco-dev"
SKIP_BUILD=0

die()  { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
note() { printf '\n=== %s ===\n' "$*"; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-runtime) MODE="none"; shift ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --image) [ "$#" -ge 2 ] || die "--image needs a value"; IMAGE="$2"; shift 2 ;;
        --image=*) IMAGE="${1#*=}"; shift ;;
        docker|finch|podman) RUNTIME="$1"; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "usage: dev_alias_live.sh <docker|finch|podman> [--skip-build] [--image NAME] | --no-runtime (got: $1)" ;;
    esac
done

[ -x "$SETUP" ] || die "setup script not found or not executable: $SETUP"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
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
# Setup GCO: build (or reuse) the real dev image.
# ---------------------------------------------------------------------------
if [ "$SKIP_BUILD" -eq 1 ]; then
    note "using existing image: $IMAGE (--skip-build)"
    "$RUNTIME" image inspect "$IMAGE" >/dev/null 2>&1 \
        || die "--skip-build set but image '$IMAGE' was not found in $RUNTIME"
else
    note "setup GCO: build $IMAGE from Dockerfile.dev with $RUNTIME"
    "$RUNTIME" build -f "$DOCKERFILE" -t "$IMAGE" "$REPO_ROOT" \
        || die "$RUNTIME failed to build $IMAGE from Dockerfile.dev"
fi

# ---------------------------------------------------------------------------
# Generate the gco function into a throwaway rc.
# ---------------------------------------------------------------------------
note "generate the gco function (setup-dev-alias.sh --runtime $RUNTIME --image $IMAGE)"
rc="$WORK/rc"
"$SETUP" --runtime "$RUNTIME" --image "$IMAGE" --rc "$rc" >/dev/null
grep -q '>>> gco >>>' "$rc" || die "setup script did not write a gco function block"

# ---------------------------------------------------------------------------
# Build a workspace fixture. The DAG references a manifest by *relative* path;
# DagDefinition.validate() checks both the DAG and the manifest exist (relative
# to cwd), so a successful validate proves the $PWD -> /workspace bind mount and
# cwd=/workspace are wired correctly.
# ---------------------------------------------------------------------------
ws="$WORK/ws"
mkdir -p "$ws"
printf '# placeholder job manifest for the dev-alias live proof\n' >"$ws/probe-job.yaml"
cat >"$ws/ci-dag.yaml" <<'YAML'
name: dev-alias-live-probe
steps:
  - name: probe
    manifest: probe-job.yaml
YAML

home="$WORK/home"
mkdir -p "$home/.aws"   # the generated function bind-mounts ~/.aws read-only

# ---------------------------------------------------------------------------
# Proof 1: the real CLI runs through the generated function.
# ---------------------------------------------------------------------------
note "prove the real gco CLI runs through the generated function"
ver="$(cd "$ws" && HOME="$home" bash --noprofile --norc -c ". '$rc'; gco --version" 2>&1)" \
    || die "'gco --version' failed through the generated function"
printf '  gco --version -> %s\n' "$ver"
[ -n "$ver" ] || die "'gco --version' produced no output"

# ---------------------------------------------------------------------------
# Proof 2: arg-forwarding + workspace bind mount + cwd, via an offline command.
# The relative path resolves only if $PWD was mounted at /workspace and cwd is
# /workspace; gco dag validate then reads both the DAG and its manifest.
# ---------------------------------------------------------------------------
note "prove arg-forwarding + workspace bind mount + cwd via 'gco dag validate ci-dag.yaml'"
out="$(cd "$ws" && HOME="$home" bash --noprofile --norc -c ". '$rc'; gco dag validate ci-dag.yaml" 2>&1)" \
    || { printf '%s\n' "$out" | sed 's/^/  | /'; die "'gco dag validate ci-dag.yaml' failed through the generated function"; }
printf '%s\n' "$out" | sed 's/^/  | /'
printf '%s\n' "$out" | grep -qi 'is valid' \
    || die "expected 'is valid' (workspace bind mount or cwd not wired correctly)"

note "ALL CHECKS PASSED for $RUNTIME"
