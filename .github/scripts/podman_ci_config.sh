#!/usr/bin/env bash
# =============================================================================
# podman_ci_config.sh — write ~/.config/containers/containers.conf for CI
# =============================================================================
#
# Rootless podman on a GitHub runner has no systemd user session, so the OCI
# runtime must be configured to avoid sd-bus calls. Two configurations work, and
# each fails in a way the other survives — so integration:dev-alias:podman tries
# one, then the other, instead of depending on whichever runtime pairing the
# runner image happened to ship.
#
#   crun  cgroup_manager=cgroupfs + cgroups=disabled. This is the long-standing
#         configuration. It breaks when the runner's apt podman is newer than its
#         crun: podman emits an OCI spec version crun does not recognize and
#         every container fails to start at the first RUN with
#         "unknown version specified". That is deterministic inside a given
#         runner, so retrying the build cannot clear it — the same commit passes
#         or fails depending on which image version the job landed on.
#
#   runc  cgroup_manager=cgroupfs, cgroups left at the default. runc ships on
#         GitHub's Ubuntu images (Docker depends on it) and is unaffected by the
#         crun version skew. It cannot be combined with cgroups=disabled:
#         podman rejects that pairing outright with
#         "requested OCI runtime runc is not compatible with NoCgroups", which is
#         why this is a whole alternative configuration rather than a one-line
#         override.
#
# Usage: podman_ci_config.sh <crun|runc>
# =============================================================================

set -euo pipefail

runtime="${1:?usage: podman_ci_config.sh <crun|runc>}"
config_dir="${HOME}/.config/containers"
config="${config_dir}/containers.conf"

mkdir -p "${config_dir}"

case "${runtime}" in
  crun)
    cat > "${config}" <<'CONF'
[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"

[containers]
cgroups = "disabled"
CONF
    ;;
  runc)
    if ! command -v runc >/dev/null 2>&1; then
      echo "podman_ci_config: runc is not installed; cannot use this configuration" >&2
      exit 1
    fi
    # cgroups is deliberately left at the default: runc refuses to run when it
    # is "disabled".
    cat > "${config}" <<'CONF'
[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"
runtime = "runc"

[containers]
CONF
    ;;
  *)
    echo "podman_ci_config: unknown runtime '${runtime}' (expected crun or runc)" >&2
    exit 2
    ;;
esac

echo "--- containers.conf (${runtime}) ---"
cat "${config}"
if command -v "${runtime}" >/dev/null 2>&1; then
  "${runtime}" --version | head -1
fi
