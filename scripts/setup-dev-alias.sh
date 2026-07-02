#!/usr/bin/env bash
#
# setup-dev-alias.sh — install a `gco` shell function that runs the GCO CLI
# inside the dev container against your current working directory.
#
# Why a function instead of a bare alias? A function forwards arguments and
# pipes correctly, attaches a TTY only when one is present (so it also works
# in scripts and CI), and bakes in the correct container socket for the
# runtime you actually have — which a copy-pasted `docker run ...` alias does
# not. The block is written between marker lines, so re-running this script
# updates it in place instead of appending duplicates.
#
# By default it also builds (or refreshes) the dev image from Dockerfile.dev
# with the detected runtime before installing the function, so a single run
# takes a fresh clone all the way to a working `gco`. Re-running always rebuilds
# (cached layers make that cheap), which transparently replaces a stale local
# image. Pass --no-build to skip the build when you manage the image yourself.
#
set -euo pipefail

MARKER_BEGIN="# >>> gco >>>"
MARKER_END="# <<< gco <<<"
IMAGE="gco-dev"
FORCED_RUNTIME=""
RC_FILE=""
PRINT_ONLY=0
NO_BUILD=0

# The dev image is built from Dockerfile.dev at the repository root. Resolve it
# from this script's own location so the build works from any working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCKERFILE="$REPO_ROOT/Dockerfile.dev"

log()  { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
setup-dev-alias.sh — install a `gco` shell function for the dev container.

Usage: scripts/setup-dev-alias.sh [options]

  -p, --print          Print the shell function to stdout and exit (no build, no writes).
  -r, --runtime NAME   Force a runtime (docker|finch|podman) vs auto-detecting.
      --rc PATH        Target this rc file instead of the one inferred from $SHELL.
      --image NAME     Dev image to build and run (default: gco-dev).
      --no-build       Skip building the dev image; assume it already exists.
  -h, --help           Show this help and exit.

By default the script builds (or refreshes) the dev image from Dockerfile.dev
with the detected runtime, then installs the `gco` function. Detection prefers
docker, then finch, then podman (the first whose daemon answers `<rt> info`).
GCO_CONTAINER_RUNTIME or CDK_DOCKER override detection.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        -p|--print) PRINT_ONLY=1; shift ;;
        -r|--runtime) [ "$#" -ge 2 ] || die "--runtime needs a value"; FORCED_RUNTIME="$2"; shift 2 ;;
        --runtime=*) FORCED_RUNTIME="${1#*=}"; shift ;;
        --rc) [ "$#" -ge 2 ] || die "--rc needs a value"; RC_FILE="$2"; shift 2 ;;
        --rc=*) RC_FILE="${1#*=}"; shift ;;
        --image) [ "$#" -ge 2 ] || die "--image needs a value"; IMAGE="$2"; shift 2 ;;
        --image=*) IMAGE="${1#*=}"; shift ;;
        --no-build) NO_BUILD=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

# Detection mirrors cli/_container_runtime.py: only accept a runtime whose
# daemon actually answers `<rt> info`.
runtime_responds() {
    command -v "$1" >/dev/null 2>&1 || return 1
    "$1" info >/dev/null 2>&1
}

detect_runtime() {
    local rt
    for rt in docker finch podman; do
        if runtime_responds "$rt"; then
            printf '%s\n' "$rt"
            return 0
        fi
    done
    return 1
}

resolve_runtime() {
    local override=""
    if [ -n "$FORCED_RUNTIME" ]; then
        override="$FORCED_RUNTIME"
    elif [ -n "${GCO_CONTAINER_RUNTIME:-}" ]; then
        override="$GCO_CONTAINER_RUNTIME"
    elif [ -n "${CDK_DOCKER:-}" ]; then
        override="$CDK_DOCKER"
    fi
    if [ -n "$override" ]; then
        runtime_responds "$override" || warn "runtime '$override' is not answering '$override info' yet; using it anyway"
        printf '%s\n' "$override"
        return 0
    fi
    detect_runtime
}

socket_args_for() {
    case "$1" in
        docker) printf -- '-v /var/run/docker.sock:/var/run/docker.sock ' ;;
        podman) printf -- '-v %s:/var/run/docker.sock ' "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock" ;;
        *) : ;;
    esac
}

socket_desc_for() {
    case "$1" in
        docker) printf '%s' "host Docker socket -> /var/run/docker.sock" ;;
        podman) printf '%s' "Podman socket (${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock) -> /var/run/docker.sock" ;;
        *) printf '%s' "none ($1 has no host socket to share)" ;;
    esac
}

choose_rc_file() {
    if [ -n "$RC_FILE" ]; then
        printf '%s\n' "$RC_FILE"
        return
    fi
    case "$(basename "${SHELL:-sh}")" in
        zsh)  printf '%s\n' "$HOME/.zshrc" ;;
        bash) printf '%s\n' "$HOME/.bashrc" ;;
        *)    printf '%s\n' "$HOME/.profile" ;;
    esac
}

emit_block() {
    local rt="$1" socket="$2" image="$3"
    cat <<EOF
$MARKER_BEGIN
# Run the \`gco\` CLI inside the GCO dev container, against \$PWD.
# Managed by scripts/setup-dev-alias.sh — re-run that script to regenerate
# (for example after switching container runtimes). Runtime: $rt.
gco() {
    if [ -t 0 ] && [ -t 1 ]; then
        $rt run --rm -it -v "\$HOME/.aws:/root/.aws:ro" -v "\$PWD:/workspace" ${socket}-w /workspace $image gco "\$@"
    else
        $rt run --rm -i -v "\$HOME/.aws:/root/.aws:ro" -v "\$PWD:/workspace" ${socket}-w /workspace $image gco "\$@"
    fi
}
$MARKER_END
EOF
}

build_image() {
    local rt="$1"
    [ -f "$DOCKERFILE" ] || die "cannot build '$IMAGE': $DOCKERFILE not found."
    log "Building the '$IMAGE' image from Dockerfile.dev with $rt ..."
    log "(the first build can take a few minutes; re-runs reuse cached layers and just refresh what changed)"
    "$rt" build -f "$DOCKERFILE" -t "$IMAGE" "$REPO_ROOT" || die "$rt failed to build '$IMAGE' from $DOCKERFILE."
    log "Image '$IMAGE' is ready."
    log ""
}

install_block() {
    local rc="$1" block="$2" tmp
    tmp="$(mktemp)"
    if [ -f "$rc" ]; then
        awk -v b="$MARKER_BEGIN" -v e="$MARKER_END" '
            $0 == b { skip = 1 }
            skip != 1 { print }
            $0 == e { skip = 0 }
        ' "$rc" > "$tmp"
        if [ -s "$tmp" ]; then printf '\n' >> "$tmp"; fi
    fi
    printf '%s\n' "$block" >> "$tmp"
    mv "$tmp" "$rc"
}

runtime="$(resolve_runtime || true)"
[ -n "$runtime" ] || die "no container runtime found. Install Docker, Finch, or Podman and start it, then re-run (or force one with --runtime NAME)."

socket="$(socket_args_for "$runtime")"

# Podman does not resolve a bare, locally-built image name: an image built with
# `podman build -t gco-dev` is stored as `localhost/gco-dev`, but `podman run
# gco-dev` treats the unqualified name as remote and searches the configured
# registries (docker.io, quay.io, ...) instead of local storage. Prefix
# `localhost/` so the emitted `podman run` finds the image you built locally.
# Names that already carry a registry/namespace (contain a `/`) are untouched,
# and docker/finch — which do resolve bare local names — keep the plain name.
image_ref="$IMAGE"
if [ "$runtime" = "podman" ]; then
    case "$IMAGE" in
        */*) : ;;
        *)   image_ref="localhost/$IMAGE" ;;
    esac
fi

block="$(emit_block "$runtime" "$socket" "$image_ref")"

if [ "$PRINT_ONLY" -eq 1 ]; then
    printf '%s\n' "$block"
    exit 0
fi

# Build (or refresh) the dev image before wiring up the function, so a single
# run takes a fresh clone all the way to a working `gco`. Always rebuilding also
# means a stale local image is transparently replaced. Skipped with --no-build.
if [ "$NO_BUILD" -eq 1 ]; then
    log "Skipping the image build (--no-build); assuming '$image_ref' already exists."
    log ""
else
    build_image "$runtime"
fi

rc="$(choose_rc_file)"
install_block "$rc" "$block"

log "Installed the 'gco' dev-container function."
log ""
log "  Container runtime : $runtime"
log "  Socket mount      : $(socket_desc_for "$runtime")"
log "  Dev image         : $image_ref"
log "  Shell profile     : $rc"
log ""
log "Activate it in this shell:  source \"$rc\""
log "Then try:                   gco --help"
if [ -z "$socket" ]; then
    log ""
    log "Note: $runtime runs containers inside a VM and exposes no host daemon socket the"
    log "container can reach (a bind-mounted socket connects to nothing across the VM"
    log "boundary), so the function omits the socket mount. Everyday commands — jobs,"
    log "status, costs, inference, and non-build stacks operations — work as-is."
    log ""
    log "Build-heavy commands ('gco stacks deploy-all', image builds) need a container"
    log "daemon at CDK synth time, so run them on the host with $runtime as the builder:"
    log ""
    log "    CDK_DOCKER=$runtime gco stacks deploy-all -y"
    log ""
    log "Run that against a host install of the GCO CLI whose deps match the lockfile"
    log "(e.g. a project virtualenv), so the pinned aws-cdk-lib / cdk-nag are used."
fi
