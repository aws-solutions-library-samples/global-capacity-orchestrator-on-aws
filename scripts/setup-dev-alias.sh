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
AWS_WRITABLE=0
UNINSTALL=0

# AWS environment variables forwarded into the container, in the bare `-e NAME`
# form: every runtime (docker, finch/nerdctl, podman) passes such a variable
# through only when it is set in the caller's environment, so an unset variable
# never becomes an empty override inside the container. Without this list a
# mounted ~/.aws was the ONLY credential source that worked — `AWS_PROFILE`,
# SSO/`assume-role` sessions exported into the shell, static keys, and
# web-identity/OIDC setups were all silently dropped at the container boundary,
# which surfaces as "credentials not found" or, worse, as operating against the
# wrong account than the host shell was pointed at.
#
# Keep this list to variables the SDK and CLI actually read. Values naming a
# path OUTSIDE ~/.aws (AWS_CONFIG_FILE, AWS_SHARED_CREDENTIALS_FILE,
# AWS_WEB_IDENTITY_TOKEN_FILE, AWS_CA_BUNDLE) are forwarded because they are
# frequently set to a path under $HOME/.aws; when they point elsewhere the file
# is not mounted, and the installer prints that caveat.
AWS_FORWARDED_ENV=(
    AWS_PROFILE
    AWS_DEFAULT_PROFILE
    AWS_REGION
    AWS_DEFAULT_REGION
    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY
    AWS_SESSION_TOKEN
    AWS_CREDENTIAL_EXPIRATION
    AWS_ROLE_ARN
    AWS_ROLE_SESSION_NAME
    AWS_WEB_IDENTITY_TOKEN_FILE
    AWS_CONFIG_FILE
    AWS_SHARED_CREDENTIALS_FILE
    AWS_CA_BUNDLE
    AWS_ENDPOINT_URL
    AWS_USE_FIPS_ENDPOINT
    AWS_USE_DUALSTACK_ENDPOINT
    AWS_RETRY_MODE
    AWS_MAX_ATTEMPTS
    AWS_EC2_METADATA_DISABLED
    GCO_DEFAULT_REGION
)

# The dev image is built from Dockerfile.dev at the repository root. Resolve it
# from this script's own location so the build works from any working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCKERFILE="$REPO_ROOT/Dockerfile.dev"

log()  { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# Values supplied through --runtime, --image, CDK_DOCKER, or
# XDG_RUNTIME_DIR are persisted into a shell profile and therefore cross a
# second shell-parsing boundary. Emit simple image/runtime names unchanged for
# readability, but POSIX-single-quote anything containing shell syntax. The
# sed replacement turns each embedded apostrophe into the safe '\'' sequence.
shell_quote() {
    printf "'"
    printf '%s' "$1" | sed "s/'/'\\\\''/g"
    printf "'"
}

shell_word() {
    case "$1" in
        ""|*[!A-Za-z0-9_./:@+-]*) shell_quote "$1" ;;
        *) printf '%s' "$1" ;;
    esac
}

require_single_line() {
    local label="$1" value="$2"
    case "$value" in
        *$'\n'*|*$'\r'*) die "$label must not contain line breaks" ;;
    esac
}

usage() {
    cat <<'EOF'
setup-dev-alias.sh — install a `gco` shell function for the dev container.

Usage: scripts/setup-dev-alias.sh [options]

  -p, --print          Print the shell function to stdout and exit (no build, no writes).
  -r, --runtime NAME   Force a runtime (docker|finch|podman) vs auto-detecting.
      --rc PATH        Target this rc file instead of the one inferred from $SHELL.
      --image NAME     Dev image to build and run (default: gco-dev).
      --no-build       Skip building the dev image; assume it already exists.
      --aws-writable   Mount ~/.aws read-write so `aws sso login` can run in
                       the container and cache its token for the host (default:
                       read-only).
      --uninstall      Remove the managed block from the rc file and exit.
  -h, --help           Show this help and exit.

By default the script builds (or refreshes) the dev image from Dockerfile.dev
with the detected runtime, then installs the `gco` function. Detection prefers
docker, then finch, then podman (the first whose daemon answers `<rt> info`).
GCO_CONTAINER_RUNTIME or CDK_DOCKER override detection.

AWS credentials: the function mounts ~/.aws and forwards the standard AWS
environment variables (AWS_PROFILE, AWS_REGION, static keys, session tokens,
role/web-identity settings, endpoint and retry overrides) only when the calling
shell has them set. So `AWS_PROFILE=prod gco status`, an exported SSO or
assume-role session, static keys, and a plain ~/.aws/config all work the same
way inside the container as they do on the host.
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
        --aws-writable) AWS_WRITABLE=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
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
    local mount=""
    case "$1" in
        docker) mount="/var/run/docker.sock:/var/run/docker.sock" ;;
        podman) mount="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock:/var/run/docker.sock" ;;
        *) return 0 ;;
    esac
    printf -- '-v %s ' "$(shell_quote "$mount")"
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
        # fish cannot parse POSIX function syntax, so writing this block into a
        # fish config would produce a file fish errors on at every new shell —
        # and writing it to ~/.profile (which fish does not read) would look
        # like a successful install that silently never provides `gco`. Fail
        # loudly with the two real options instead.
        fish) die "fish shell cannot source this POSIX function.
Either run the CLI through a POSIX shell:
    bash -lc 'gco --help'
or add a fish wrapper of your own using the printed command as the body:
    scripts/setup-dev-alias.sh --print
Pass --rc PATH to install into a specific file anyway." ;;
        *)    printf '%s\n' "$HOME/.profile" ;;
    esac
}

# Remove the managed block, leaving any surrounding rc content untouched.
remove_block() {
    local rc="$1" tmp
    [ -f "$rc" ] || { log "Nothing to remove: $rc does not exist."; return 0; }
    if ! grep -qF "$MARKER_BEGIN" "$rc"; then
        log "Nothing to remove: no gco block found in $rc."
        return 0
    fi
    tmp="$(mktemp)"
    awk -v b="$MARKER_BEGIN" -v e="$MARKER_END" '
        $0 == b { skip = 1 }
        skip != 1 { print }
        $0 == e { skip = 0 }
    ' "$rc" > "$tmp"
    mv "$tmp" "$rc"
    log "Removed the 'gco' function block from $rc."
    log "Open a new shell (or unset it with: unset -f gco) to finish."
}

# SELinux-enforcing hosts (Fedora, RHEL, CentOS Stream and friends) deny a
# container access to every bind mount unless the mount carries a relabel
# option. Without it `gco` starts and then fails on "permission denied" for
# /workspace and ~/.aws, which reads like a GCO bug rather than a host policy.
# `z` (lowercase) applies a shared label so several containers — and the host —
# keep access; `Z` would relabel exclusively and break other consumers of
# ~/.aws. Non-SELinux hosts get no suffix, keeping their command lines clean.
mount_suffix_for_host() {
    if command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled 2>/dev/null; then
        printf ',z'
    fi
}

# Emit one `-e NAME` flag per forwarded AWS variable (see AWS_FORWARDED_ENV).
aws_env_args() {
    local name
    for name in "${AWS_FORWARDED_ENV[@]}"; do
        printf -- '-e %s ' "$name"
    done
}

emit_block() {
    local rt="$1" socket="$2" image="$3" aws_env="$4" mount_opts="$5" rt_word image_word
    rt_word="$(shell_word "$rt")"
    image_word="$(shell_word "$image")"
    # Three persistence mounts make `gco autopilot` (and anything else that
    # keeps state under ~/.gco) survive the --rm container lifecycle:
    #   gco-dev-tools -> /root/.npm-global   named volume; the pinned Claude
    #                                        Code that autopilot installs on
    #                                        first use persists across runs
    #   ~/.claude     -> /root/.claude       host dir; CLAUDE_CONFIG_DIR keeps
    #                                        onboarding state and session
    #                                        transcripts there, so sessions
    #                                        resume across container runs
    #   ~/.gco        -> /root/.gco          host dir; GCO CLI config/cache and
    #                                        the generated autopilot MCP config
    # The host dirs are pre-created so a root-owned mount point is never
    # created on Linux hosts.
    cat <<EOF
$MARKER_BEGIN
# Run the \`gco\` CLI inside the GCO dev container, against \$PWD.
# Managed by scripts/setup-dev-alias.sh — re-run that script to regenerate
# after switching container runtimes or image names.
# ~/.aws is created (empty is fine) so hosts that authenticate purely through
# environment variables, an OIDC/web-identity file, or instance metadata still
# get a valid mount source instead of the runtime materialising a root-owned
# directory on the host. Credential env vars are forwarded with bare \`-e NAME\`,
# so each is passed only when the calling shell actually has it set.
gco() {
    mkdir -p "\$HOME/.aws" "\$HOME/.claude" "\$HOME/.gco"
    if [ -t 0 ] && [ -t 1 ]; then
        $rt_word run --rm -it -v "\$HOME/.aws:/root/.aws:${mount_opts}" -v "\$HOME/.claude:/root/.claude" -v "\$HOME/.gco:/root/.gco" -v gco-dev-tools:/root/.npm-global -e CLAUDE_CONFIG_DIR=/root/.claude ${aws_env}-v "\$PWD:/workspace" ${socket}-w /workspace $image_word gco "\$@"
    else
        $rt_word run --rm -i -v "\$HOME/.aws:/root/.aws:${mount_opts}" -v "\$HOME/.claude:/root/.claude" -v "\$HOME/.gco:/root/.gco" -v gco-dev-tools:/root/.npm-global -e CLAUDE_CONFIG_DIR=/root/.claude ${aws_env}-v "\$PWD:/workspace" ${socket}-w /workspace $image_word gco "\$@"
    fi
}
$MARKER_END
EOF
}

# Number of times to try the image build, and the backoff between tries.
# The build's first act is resolving the Dockerfile.dev base image from Docker
# Hub, which is a public registry the build does not control: a single DNS or
# TCP timeout there ("failed to resolve source metadata ... i/o timeout") fails
# an otherwise healthy build. Retrying makes that transient class self-healing
# while a genuine build error still fails on the last attempt with its own
# output. Override the count to 1 to disable retries.
BUILD_ATTEMPTS="${GCO_DEV_IMAGE_BUILD_ATTEMPTS:-3}"
BUILD_RETRY_DELAY="${GCO_DEV_IMAGE_BUILD_RETRY_DELAY:-15}"

build_image() {
    local rt="$1"
    [ -f "$DOCKERFILE" ] || die "cannot build '$IMAGE': $DOCKERFILE not found."
    log "Building the '$IMAGE' image from Dockerfile.dev with $rt ..."
    log "(the first build can take a few minutes; re-runs reuse cached layers and just refresh what changed)"

    local attempt=1
    while true; do
        if "$rt" build -f "$DOCKERFILE" -t "$IMAGE" "$REPO_ROOT"; then
            break
        fi
        if [ "$attempt" -ge "$BUILD_ATTEMPTS" ]; then
            die "$rt failed to build '$IMAGE' from $DOCKERFILE."
        fi
        log "build attempt $attempt/$BUILD_ATTEMPTS failed; retrying in ${BUILD_RETRY_DELAY}s ..."
        log "(usually a transient registry/network error pulling the base image)"
        sleep "$BUILD_RETRY_DELAY"
        attempt=$((attempt + 1))
    done

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

# Uninstall is pure rc-file surgery: it must work on a machine whose container
# runtime is gone or broken, so it runs before any runtime detection or build.
if [ "$UNINSTALL" -eq 1 ]; then
    remove_block "$(choose_rc_file)"
    exit 0
fi

runtime="$(resolve_runtime || true)"
[ -n "$runtime" ] || die "no container runtime found. Install Docker, Finch, or Podman and start it, then re-run (or force one with --runtime NAME)."
require_single_line "container runtime" "$runtime"
if [ "$runtime" = "podman" ]; then
    require_single_line "XDG_RUNTIME_DIR" "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
fi

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
require_single_line "image name" "$image_ref"

mount_suffix="$(mount_suffix_for_host)"
if [ "$AWS_WRITABLE" -eq 1 ]; then
    # Writable ~/.aws lets `aws sso login` / `aws configure` run INSIDE the
    # container and cache their tokens where the host can reuse them. Opt-in:
    # the read-only default keeps a container that runs third-party tooling
    # from rewriting the operator's credential files.
    aws_mount_opts="rw${mount_suffix}"
else
    aws_mount_opts="ro${mount_suffix}"
fi

block="$(emit_block "$runtime" "$socket" "$image_ref" "$(aws_env_args)" "$aws_mount_opts")"

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
log "  AWS credentials   : ~/.aws mounted $([ "$AWS_WRITABLE" -eq 1 ] && printf 'read-write' || printf 'read-only') + AWS_PROFILE/AWS_REGION/keys/session"
log "                      forwarded from your shell when set"
if [ -n "$mount_suffix" ]; then
    log "  SELinux           : enforcing host detected; bind mounts carry the ',z' shared label"
fi
log ""
if [ "$AWS_WRITABLE" -eq 0 ]; then
    log "Note: ~/.aws is mounted read-only, so an SSO/session token that expires must be"
    log "refreshed on the host ('aws sso login'); re-run with --aws-writable to allow the"
    log "container to refresh and cache it instead."
fi
if [ -n "${AWS_CONFIG_FILE:-}${AWS_SHARED_CREDENTIALS_FILE:-}${AWS_WEB_IDENTITY_TOKEN_FILE:-}" ]; then
    log ""
    log "Note: you have an AWS file-path variable set. It is forwarded, but the file is"
    log "only readable in the container when it lives under ~/.aws (the mounted path)."
    log "Copy or symlink it under ~/.aws, or add your own -v mount to the function."
fi
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
