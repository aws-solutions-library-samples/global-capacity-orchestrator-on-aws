#!/usr/bin/env bash
# AWS PCS node lifecycle action (nodeBootstrapped, EVERY_BOOT): make sure the
# GCO API's Slurm identity has a home directory on the shared /home.
#
# PCS's maintained mount-efs.sh mounts the regional EFS access point at /home
# just before this runs. The access point's root is owned by the identity but
# empty, and a Slurm job submitted through the GCO API starts in the identity's
# home (its working directory, where slurm-<jobid>.out lands): without the
# directory slurmd cannot chdir and the job fails. Nothing logs the identity in
# through PAM, so mkhomedir never fires; this creates the directory itself.
#
# Idempotent and safe on every boot: an existing directory keeps its contents;
# only ownership and mode are asserted. Arguments are named flags, like the
# AWS-maintained scripts (cng-node-lifecycle-actions-vetted-scripts):
#   --home-base DIR   where the shared home filesystem is mounted (default /home)
#   --name NAME       the identity's user name (its directory is DIR/NAME)
#   --uid UID --gid GID   numeric owner of the directory
#   --mode MODE       directory mode (default 0750)
set -euo pipefail

HOME_BASE=/home
NAME=""
UID_VALUE=""
GID_VALUE=""
MODE=0750

usage() {
    echo "usage: $0 --name NAME --uid UID --gid GID [--home-base DIR] [--mode MODE]" >&2
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --home-base) HOME_BASE="${2:-}"; shift 2 ;;
        --name) NAME="${2:-}"; shift 2 ;;
        --uid) UID_VALUE="${2:-}"; shift 2 ;;
        --gid) GID_VALUE="${2:-}"; shift 2 ;;
        --mode) MODE="${2:-}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

[ -n "$NAME" ] && [ -n "$UID_VALUE" ] && [ -n "$GID_VALUE" ] || usage
if ! [[ "$NAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
    echo "invalid user name: $NAME" >&2
    exit 1
fi
if ! [[ "$UID_VALUE" =~ ^[0-9]+$ && "$GID_VALUE" =~ ^[0-9]+$ ]]; then
    echo "uid and gid must be numeric: uid=$UID_VALUE gid=$GID_VALUE" >&2
    exit 1
fi
if ! [[ "$MODE" =~ ^0?[0-7]{3}$ ]]; then
    echo "invalid mode: $MODE" >&2
    exit 1
fi
if ! mountpoint -q "$HOME_BASE"; then
    echo "$HOME_BASE is not a mounted filesystem; the shared home must be mounted first" >&2
    exit 1
fi

TARGET="$HOME_BASE/$NAME"
if [ ! -d "$TARGET" ]; then
    install -d -m "$MODE" -o "$UID_VALUE" -g "$GID_VALUE" "$TARGET"
    echo "created $TARGET for uid=$UID_VALUE gid=$GID_VALUE mode=$MODE"
else
    chown "$UID_VALUE:$GID_VALUE" "$TARGET"
    chmod "$MODE" "$TARGET"
    echo "verified $TARGET for uid=$UID_VALUE gid=$GID_VALUE mode=$MODE"
fi
