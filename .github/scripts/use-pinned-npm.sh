#!/usr/bin/env bash
# Install and verify the exact npm release declared by packageManager.
set -euo pipefail

MANIFEST="${1:-package.json}"
if [ ! -f "$MANIFEST" ]; then
    echo "npm manifest not found: $MANIFEST" >&2
    exit 1
fi

PACKAGE_MANAGER=$(node -e '
const fs = require("node:fs");
const manifest = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
process.stdout.write(String(manifest.packageManager || ""));
' "$MANIFEST")

if ! [[ "$PACKAGE_MANAGER" =~ ^npm@([0-9]+\.){2}[0-9]+$ ]]; then
    echo "packageManager must declare an exact npm version: $MANIFEST" >&2
    exit 1
fi
REQUIRED_VERSION="${PACKAGE_MANAGER#npm@}"
CURRENT_VERSION=$(npm --version 2>/dev/null || true)

if [ "$CURRENT_VERSION" != "$REQUIRED_VERSION" ]; then
    npm install --global "npm@${REQUIRED_VERSION}" \
        --ignore-scripts --no-audit --no-fund
fi

CURRENT_VERSION=$(npm --version)
if [ "$CURRENT_VERSION" != "$REQUIRED_VERSION" ]; then
    echo "npm version mismatch: expected $REQUIRED_VERSION, found $CURRENT_VERSION" >&2
    exit 1
fi
printf 'Using npm %s declared by %s\n' "$CURRENT_VERSION" "$MANIFEST"
