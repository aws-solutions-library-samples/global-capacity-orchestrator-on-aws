#!/usr/bin/env bash
# AWS PCS node lifecycle action (nodeBootstrapped, EVERY_BOOT): make sure the
# GCO API's Slurm identity exists on the node (optionally) and has a home
# directory on the shared /home.
#
# PCS's maintained mount-efs.sh mounts the regional EFS access point at /home
# just before this runs. The access point's root is owned by the identity but
# empty, and a Slurm job submitted through the GCO API starts in the identity's
# home (its working directory, where slurm-<jobid>.out lands): without the
# directory slurmd cannot chdir and the job fails. Nothing logs the identity in
# through PAM, so mkhomedir never fires; this creates the directory itself.
#
# slurmd also has to resolve the job's uid to a user. The default identity
# (uid 1000) is the AMI's first user, but an identity of your own may exist on
# no AMI: --create-user adds the group and the user to the node (the local
# passwd/group files; the home stays the shared directory) when the uid is not
# taken yet, and refuses a uid or a name that already belongs to someone else.
#
# Idempotent and safe on every boot: an existing directory keeps its contents;
# only ownership and mode are asserted; an existing matching user is left
# alone. Arguments are named flags, like the AWS-maintained scripts
# (cng-node-lifecycle-actions-vetted-scripts):
#   --home-base DIR   where the shared home filesystem is mounted (default /home)
#   --name NAME       the identity's user name (its directory is DIR/NAME)
#   --dir BASENAME    the directory's basename when it is not NAME (DIR/BASENAME)
#   --uid UID --gid GID   numeric owner of the directory (and of the user)
#   --mode MODE       directory mode (default 0750)
#   --create-user     create the group and user on the node when uid is free
#   --shell SHELL     the created user's login shell (default /bin/bash)
#   --gecos TEXT      the created user's GECOS field (default: "GCO Slurm identity")
set -euo pipefail

HOME_BASE=/home
NAME=""
DIR_NAME=""
UID_VALUE=""
GID_VALUE=""
MODE=0750
CREATE_USER=no
SHELL_VALUE=/bin/bash
GECOS="GCO Slurm identity"

usage() {
    echo "usage: $0 --name NAME --uid UID --gid GID [--home-base DIR] [--dir BASENAME] [--mode MODE] [--create-user [--shell SHELL] [--gecos TEXT]]" >&2
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --home-base) HOME_BASE="${2:-}"; shift 2 ;;
        --name) NAME="${2:-}"; shift 2 ;;
        --dir) DIR_NAME="${2:-}"; shift 2 ;;
        --uid) UID_VALUE="${2:-}"; shift 2 ;;
        --gid) GID_VALUE="${2:-}"; shift 2 ;;
        --mode) MODE="${2:-}"; shift 2 ;;
        --create-user) CREATE_USER=yes; shift ;;
        --shell) SHELL_VALUE="${2:-}"; shift 2 ;;
        --gecos) GECOS="${2:-}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

[ -n "$NAME" ] && [ -n "$UID_VALUE" ] && [ -n "$GID_VALUE" ] || usage
if ! [[ "$NAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
    echo "invalid user name: $NAME" >&2
    exit 1
fi
DIR_NAME="${DIR_NAME:-$NAME}"
if ! [[ "$DIR_NAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
    echo "invalid directory name: $DIR_NAME" >&2
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
if [[ "$SHELL_VALUE" != /* ]]; then
    echo "shell must be an absolute path: $SHELL_VALUE" >&2
    exit 1
fi
if [[ "$GECOS" == *:* ]]; then
    echo "gecos must not contain ':': $GECOS" >&2
    exit 1
fi
if ! mountpoint -q "$HOME_BASE"; then
    echo "$HOME_BASE is not a mounted filesystem; the shared home must be mounted first" >&2
    exit 1
fi

TARGET="$HOME_BASE/$DIR_NAME"

# ensure_user (--create-user): the identity resolves on this node. Add the
# group and the user when the uid is free; a uid or name that already belongs
# to someone else is a configuration error, not something to paper over
# (slurmd would run the job as whoever owns the uid).
ensure_user() {
    local existing_name existing_uid existing_group
    existing_name="$(getent passwd "$UID_VALUE" | cut -d: -f1 || true)"
    if [ -n "$existing_name" ]; then
        if [ "$existing_name" != "$NAME" ]; then
            echo "uid $UID_VALUE already belongs to user $existing_name, not $NAME" >&2
            exit 1
        fi
        echo "user $NAME (uid=$UID_VALUE) already exists"
        return 0
    fi
    existing_uid="$(getent passwd "$NAME" | cut -d: -f3 || true)"
    if [ -n "$existing_uid" ]; then
        echo "user $NAME already exists with uid $existing_uid, not $UID_VALUE" >&2
        exit 1
    fi
    existing_group="$(getent group "$GID_VALUE" | cut -d: -f1 || true)"
    if [ -z "$existing_group" ]; then
        if [ -n "$(getent group "$NAME" | cut -d: -f3 || true)" ]; then
            echo "group $NAME already exists with another gid; cannot create gid $GID_VALUE" >&2
            exit 1
        fi
        groupadd --gid "$GID_VALUE" "$NAME"
        echo "created group $NAME (gid=$GID_VALUE)"
    fi
    useradd --uid "$UID_VALUE" --gid "$GID_VALUE" --home-dir "$TARGET" --no-create-home \
        --shell "$SHELL_VALUE" --comment "$GECOS" "$NAME"
    echo "created user $NAME (uid=$UID_VALUE gid=$GID_VALUE shell=$SHELL_VALUE)"
}

if [ "$CREATE_USER" = yes ]; then
    ensure_user
fi

if [ ! -d "$TARGET" ]; then
    install -d -m "$MODE" -o "$UID_VALUE" -g "$GID_VALUE" "$TARGET"
    echo "created $TARGET for uid=$UID_VALUE gid=$GID_VALUE mode=$MODE"
else
    chown "$UID_VALUE:$GID_VALUE" "$TARGET"
    chmod "$MODE" "$TARGET"
    echo "verified $TARGET for uid=$UID_VALUE gid=$GID_VALUE mode=$MODE"
fi
