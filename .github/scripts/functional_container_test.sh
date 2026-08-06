#!/usr/bin/env bash
# =============================================================================
# functional_container_test.sh — boot a distroless service image under the
# deployment manifests' pod-equivalent runtime constraints and assert its
# serving contract.
# =============================================================================
#
# What it asserts, in order:
#   1. The container boots to a serving state under the same constraints the
#      pod securityContext enforces: read-only root filesystem, tmpfs /tmp,
#      uid:gid 1000:1000, all capabilities dropped, no-new-privileges.
#   2. Each --probe "path=code[=body-substring]" returns the expected HTTP
#      status (and body substring when given) — covering liveness/readiness
#      endpoints, auth fail-closed 503s, and degraded-dependency 503s.
#   3. Each --exec-python payload runs inside the live container via
#      `docker exec` — the same mechanism kubelet uses for exec probes and
#      preStop hooks. The images ship no shell, so python is the only
#      executable; this proves the manifests' ["python", "-c", ...] command
#      shapes actually work against the running distroless container.
#   4. Optional --min-uptime N: the service must still be serving N seconds
#      after boot (guards "boots then crash-loops on unreachable deps").
#   5. `docker stop` (SIGTERM, then SIGKILL after --stop-timeout) exits with
#      --expect-stop-exit: 0 for the uvicorn services' graceful shutdown,
#      143 for inference-monitor which has no SIGTERM handler today.
#
# Kubernetes-dependent behavior (RBAC, reconciliation, NetworkPolicy) is out
# of scope here — integration:kind:cluster-e2e owns it. This script owns the
# container-level contract that kind deliberately does not probe.
#
# Usage:
#   functional_container_test.sh --image svc:ci --name svc-fn --host-port 18080 \
#     [--container-port 8080] [--wait-path /healthz] [--env K=V]... \
#     [--kubeconfig FILE] [--probe "path=code[=substring]"]... \
#     [--exec-python "code"]... [--min-uptime N] \
#     [--expect-stop-exit 0] [--stop-timeout 30]
# =============================================================================

set -euo pipefail

IMAGE=""
NAME=""
HOST_PORT=""
CONTAINER_PORT="8080"
WAIT_PATH="/healthz"
KUBECONFIG_FILE=""
MIN_UPTIME="0"
EXPECT_STOP_EXIT="0"
STOP_TIMEOUT="30"
ENVS=()
PROBES=()
EXEC_SNIPPETS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --image)            IMAGE="$2"; shift 2 ;;
    --name)             NAME="$2"; shift 2 ;;
    --host-port)        HOST_PORT="$2"; shift 2 ;;
    --container-port)   CONTAINER_PORT="$2"; shift 2 ;;
    --wait-path)        WAIT_PATH="$2"; shift 2 ;;
    --env)              ENVS+=("$2"); shift 2 ;;
    --kubeconfig)       KUBECONFIG_FILE="$2"; shift 2 ;;
    --probe)            PROBES+=("$2"); shift 2 ;;
    --exec-python)      EXEC_SNIPPETS+=("$2"); shift 2 ;;
    --min-uptime)       MIN_UPTIME="$2"; shift 2 ;;
    --expect-stop-exit) EXPECT_STOP_EXIT="$2"; shift 2 ;;
    --stop-timeout)     STOP_TIMEOUT="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "${IMAGE}" ] || [ -z "${NAME}" ] || [ -z "${HOST_PORT}" ]; then
  echo "required: --image, --name, --host-port" >&2
  exit 2
fi

fail() {
  echo "::error::${NAME}: $1"
  echo "===== ${NAME}: container logs ====="
  docker logs "${NAME}" 2>&1 || true
  exit 1
}

cleanup() { docker rm -f "${NAME}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# Pod-equivalent runtime constraints, mirroring the container securityContext
# in lambda/kubectl-applier-simple/manifests/3*.yaml: readOnlyRootFilesystem,
# runAsUser/runAsGroup 1000, capabilities drop ALL, allowPrivilegeEscalation
# false, and the /tmp emptyDir the manifests mount.
run_args=(
  -d --name "${NAME}"
  --read-only
  --tmpfs "/tmp:rw,size=64m,mode=1777"
  --user 1000:1000
  --cap-drop ALL
  --security-opt no-new-privileges
  -p "127.0.0.1:${HOST_PORT}:${CONTAINER_PORT}"
)
for pair in ${ENVS[@]+"${ENVS[@]}"}; do
  run_args+=(-e "${pair}")
done
if [ -n "${KUBECONFIG_FILE}" ]; then
  # A static kubeconfig pointing at an unreachable apiserver: the kubernetes
  # client parses it at startup without connecting, which is exactly what
  # lets these services reach their serving state outside a cluster.
  run_args+=(-v "${KUBECONFIG_FILE}:/kubeconfig:ro" -e KUBECONFIG=/kubeconfig)
fi

docker run "${run_args[@]}" "${IMAGE}"

# --- 1. Wait for the serving state --------------------------------------
base_url="http://127.0.0.1:${HOST_PORT}"
deadline=$((SECONDS + 60))
while :; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "${base_url}${WAIT_PATH}" || true)"
  [ "${code}" = "200" ] && break
  if [ "$(docker inspect -f '{{.State.Running}}' "${NAME}" 2>/dev/null)" != "true" ]; then
    fail "container exited during startup (last ${WAIT_PATH} code: ${code})"
  fi
  [ "${SECONDS}" -ge "${deadline}" ] && fail "${WAIT_PATH} never returned 200 within 60s (last: ${code})"
  sleep 2
done
echo "${NAME}: serving (${WAIT_PATH} -> 200)"

# --- 2. HTTP contract probes ---------------------------------------------
for spec in ${PROBES[@]+"${PROBES[@]}"}; do
  path="${spec%%=*}"
  rest="${spec#*=}"
  expected_code="${rest%%=*}"
  expected_body=""
  [ "${rest}" != "${expected_code}" ] && expected_body="${rest#*=}"

  body_file="$(mktemp)"
  actual_code="$(curl -s -o "${body_file}" -w '%{http_code}' "${base_url}${path}" || true)"
  if [ "${actual_code}" != "${expected_code}" ]; then
    echo "response body: $(cat "${body_file}")"
    rm -f "${body_file}"
    fail "GET ${path}: expected HTTP ${expected_code}, got ${actual_code}"
  fi
  if [ -n "${expected_body}" ] && ! grep -q "${expected_body}" "${body_file}"; then
    echo "response body: $(cat "${body_file}")"
    rm -f "${body_file}"
    fail "GET ${path}: body does not contain '${expected_body}'"
  fi
  rm -f "${body_file}"
  if [ -n "${expected_body}" ]; then
    echo "${NAME}: GET ${path} -> ${actual_code} (body matches \"${expected_body}\") OK"
  else
    echo "${NAME}: GET ${path} -> ${actual_code} OK"
  fi
done

# --- 3. Kubelet exec-command shapes (probes / preStop hooks) --------------
for snippet in ${EXEC_SNIPPETS[@]+"${EXEC_SNIPPETS[@]}"}; do
  if ! docker exec "${NAME}" python -c "${snippet}"; then
    fail "exec command failed in live container: python -c '${snippet}'"
  fi
  echo "${NAME}: exec python -c '${snippet}' OK"
done

# --- 4. Stability under unreachable dependencies --------------------------
if [ "${MIN_UPTIME}" -gt 0 ]; then
  sleep "${MIN_UPTIME}"
  if [ "$(docker inspect -f '{{.State.Running}}' "${NAME}")" != "true" ]; then
    fail "container died within ${MIN_UPTIME}s of becoming healthy"
  fi
  code="$(curl -s -o /dev/null -w '%{http_code}' "${base_url}${WAIT_PATH}" || true)"
  [ "${code}" = "200" ] || fail "${WAIT_PATH} degraded to ${code} after ${MIN_UPTIME}s"
  echo "${NAME}: still serving after ${MIN_UPTIME}s with unreachable dependencies OK"
fi

# --- 5. Shutdown contract --------------------------------------------------
docker stop -t "${STOP_TIMEOUT}" "${NAME}" >/dev/null
exit_code="$(docker inspect -f '{{.State.ExitCode}}' "${NAME}")"
if [ "${exit_code}" != "${EXPECT_STOP_EXIT}" ]; then
  fail "SIGTERM shutdown: expected exit ${EXPECT_STOP_EXIT}, got ${exit_code}"
fi
echo "${NAME}: SIGTERM shutdown exited ${exit_code} OK"
echo "${NAME}: functional container test PASSED"
