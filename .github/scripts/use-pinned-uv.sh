#!/usr/bin/env bash
# Install and verify the uv release Dockerfile.dev pins (ARG UV_VERSION).
#
# That ARG is the repository's one uv pin: the dev container ships it and the
# monthly dependency scan compares it with astral-sh/uv's releases, so CI jobs
# that need uv read it from there instead of installing whatever PyPI serves.
set -euo pipefail

DOCKERFILE="${1:-Dockerfile.dev}"
if [ ! -f "$DOCKERFILE" ]; then
    echo "Dockerfile not found: $DOCKERFILE" >&2
    exit 1
fi
REQUIRED_VERSION="$(sed -n 's/^ARG UV_VERSION=\([^[:space:]]*\)[[:space:]]*$/\1/p' "$DOCKERFILE" | head -1)"
if ! [[ "$REQUIRED_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ARG UV_VERSION must pin an exact uv release: $DOCKERFILE" >&2
    exit 1
fi
pip install "uv==${REQUIRED_VERSION}"
CURRENT_VERSION="$(uv --version 2>/dev/null | awk '{print $2}')" || CURRENT_VERSION=""
if [ "$CURRENT_VERSION" != "$REQUIRED_VERSION" ]; then
    echo "uv version mismatch: expected $REQUIRED_VERSION, found ${CURRENT_VERSION:-nothing}" >&2
    exit 1
fi
printf 'Using uv %s pinned by %s\n' "$CURRENT_VERSION" "$DOCKERFILE"
