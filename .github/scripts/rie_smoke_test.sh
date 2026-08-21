#!/usr/bin/env bash
# =============================================================================
# rie_smoke_test.sh — invoke a Lambda container image through the bundled
# Runtime Interface Emulator and assert the handler's deterministic reply.
# =============================================================================
#
# What it asserts, in order:
#   1. The image boots the Lambda runtime under the platform's filesystem
#      contract: read-only root with a writable /tmp only (the run also
#      proves the precompiled/PYTHONDONTWRITEBYTECODE images never need
#      bytecode writes at cold start).
#   2. The RIE endpoint accepts an Invoke within --boot-timeout seconds —
#      i.e. the CMD handler string resolves and the handler module's full
#      import graph loads inside the image, not merely on the CI runner.
#   3. POSTing --event returns the exact error envelope the handler is
#      designed to raise for that synthetic event (--expect-error-type plus
#      --expect-message-substring), proving request decode and dispatch runs
#      offline with no AWS credentials or network beyond localhost.
#
# The probe events are chosen so the handler raises deterministically BEFORE
# its first AWS SDK call; a handler that suddenly reaches the network here
# fails the assertion with a different error type, which is the point.
#
# Usage:
#   rie_smoke_test.sh --image lambda-img:ci --name lambda-rie --host-port 19000 \
#     --event '{"RequestType": "CiSmoke"}' \
#     --expect-error-type ValueError \
#     --expect-message-substring "Unsupported certificate manager event" \
#     [--env K=V]... [--boot-timeout 90]
# =============================================================================
set -euo pipefail

IMAGE=""
NAME=""
HOST_PORT=""
EVENT=""
EXPECT_ERROR_TYPE=""
EXPECT_MESSAGE_SUBSTRING=""
BOOT_TIMEOUT="90"
ENVS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --image)                     IMAGE="$2"; shift 2 ;;
    --name)                      NAME="$2"; shift 2 ;;
    --host-port)                 HOST_PORT="$2"; shift 2 ;;
    --event)                     EVENT="$2"; shift 2 ;;
    --expect-error-type)         EXPECT_ERROR_TYPE="$2"; shift 2 ;;
    --expect-message-substring)  EXPECT_MESSAGE_SUBSTRING="$2"; shift 2 ;;
    --boot-timeout)              BOOT_TIMEOUT="$2"; shift 2 ;;
    --env)                       ENVS+=("$2"); shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

for required in IMAGE NAME HOST_PORT EVENT EXPECT_ERROR_TYPE EXPECT_MESSAGE_SUBSTRING; do
  if [ -z "${!required}" ]; then
    echo "missing required argument: --$(echo "$required" | tr '[:upper:]_' '[:lower:]-')" >&2
    exit 2
  fi
done

ENV_FLAGS=()
for kv in ${ENVS[@]+"${ENVS[@]}"}; do
  ENV_FLAGS+=(--env "$kv")
done

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cleanup
# The AWS Lambda base images bundle the Runtime Interface Emulator and their
# entrypoint execs it whenever AWS_LAMBDA_RUNTIME_API is unset, so a plain
# `docker run` boots the real runtime API front door on port 8080.
# --read-only + tmpfs /tmp mirrors the deployed filesystem contract.
docker run -d \
  --name "$NAME" \
  --read-only \
  --tmpfs /tmp \
  -p "${HOST_PORT}:8080" \
  ${ENV_FLAGS[@]+"${ENV_FLAGS[@]}"} \
  "$IMAGE" >/dev/null

INVOKE_URL="http://127.0.0.1:${HOST_PORT}/2015-03-31/functions/function/invocations"
RESPONSE=""
deadline=$((SECONDS + BOOT_TIMEOUT))
until RESPONSE="$(curl -sS --max-time 30 -X POST "$INVOKE_URL" -d "$EVENT" 2>/dev/null)" \
    && [ -n "$RESPONSE" ]; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "::error::RIE endpoint for ${IMAGE} did not answer within ${BOOT_TIMEOUT}s" >&2
    docker logs "$NAME" >&2 || true
    exit 1
  fi
  if [ -z "$(docker ps -q --filter "name=^${NAME}$")" ]; then
    echo "::error::container ${NAME} exited before serving an invocation" >&2
    docker logs "$NAME" >&2 || true
    exit 1
  fi
  sleep 2
done

echo "RIE response: $RESPONSE"

if ! RESPONSE="$RESPONSE" \
  EXPECT_ERROR_TYPE="$EXPECT_ERROR_TYPE" \
  EXPECT_MESSAGE_SUBSTRING="$EXPECT_MESSAGE_SUBSTRING" \
  python3 - <<'PY'
import json
import os
import sys

response = os.environ["RESPONSE"]
try:
    envelope = json.loads(response)
except ValueError:
    sys.exit(f"RIE reply is not JSON: {response[:400]}")
if not isinstance(envelope, dict):
    sys.exit(f"RIE reply is not an error envelope: {response[:400]}")

error_type = envelope.get("errorType", "")
error_message = envelope.get("errorMessage", "")
expected_type = os.environ["EXPECT_ERROR_TYPE"]
expected_substring = os.environ["EXPECT_MESSAGE_SUBSTRING"]

if error_type != expected_type:
    sys.exit(
        f"expected errorType {expected_type!r}, got {error_type!r} "
        f"(message: {error_message[:200]!r})"
    )
if expected_substring not in error_message:
    sys.exit(
        f"expected errorMessage to contain {expected_substring!r}, "
        f"got {error_message[:400]!r}"
    )
print(f"handler raised {error_type} as designed: {error_message[:120]}")
PY
then
  docker logs "$NAME" >&2 || true
  exit 1
fi
