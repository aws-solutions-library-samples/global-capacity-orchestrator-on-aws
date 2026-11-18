#!/usr/bin/env bash
# Install the pinned Calico release into the current kind cluster and wait for
# it to become ready. Every integration:kind:* job that enforces NetworkPolicy
# runs this one script (cluster-e2e, cost-pipeline, examples-smoke): kind's
# default kindnet does not enforce NetworkPolicy, and Calico does, so the
# shipped policies are really exercised.
#
# Inputs (environment):
#   CALICO_VERSION, CALICO_SHA256  the workflow-level pins in
#                                  integration-tests.yml (required)
#   RUNNER_TEMP                    where the manifest is downloaded (default:
#                                  TMPDIR, then /tmp)
#   CALICO_ROLLOUT_ATTEMPTS        rollout waits before giving up (default 3)
#   CALICO_RETRY_DELAY_SECONDS     pause between waits (default 15)
#
# The manifest is downloaded to a file and checked against CALICO_SHA256
# before anything is applied. `rollout status` is used rather than
# `kubectl wait --for=condition=Ready pods -l ...`, which answers "no matching
# resources found" when the pods are not scheduled yet, a race that fires
# within a second of the apply; rollout status waits for the workload to
# exist, then for it to become ready.
#
# The wait is retried because calico-node readiness is a known CI flake: the
# DaemonSet pulls its image from quay.io and runs the install-cni and
# mount-bpffs init containers, which can exceed one timeout window under
# runner image-pull latency or resource contention. The apply is idempotent,
# so each attempt re-applies (a no-op once the objects exist) and re-waits.
# After the last failed attempt the script prints the kube-system pods, the
# calico-node DaemonSet and pods, and recent events, so a genuine break is
# debuggable from the log rather than just "timed out".
#
# Budget: 3 attempts x (4m node + 2m controllers) plus two 15 s pauses, about
# 18.5 minutes at worst, inside every calling job's timeout-minutes.
set -euo pipefail

: "${CALICO_VERSION:?CALICO_VERSION must name the pinned Calico release}"
: "${CALICO_SHA256:?CALICO_SHA256 must be the pinned manifest checksum}"
attempts="${CALICO_ROLLOUT_ATTEMPTS:-3}"
delay="${CALICO_RETRY_DELAY_SECONDS:-15}"
calico_manifest="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/calico.yaml"

curl --fail --show-error --location \
  --connect-timeout 15 --max-time 60 \
  --retry 3 --retry-all-errors --retry-max-time 180 \
  --remove-on-error \
  --output "${calico_manifest}" \
  "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml"
echo "${CALICO_SHA256}  ${calico_manifest}" | sha256sum -c -
kubectl apply -f "${calico_manifest}"

for i in $(seq 1 "${attempts}"); do
  echo "::group::Calico rollout attempt ${i}/${attempts}"
  # Re-apply is idempotent; it nudges anything that failed to schedule.
  kubectl apply -f "${calico_manifest}"
  if kubectl -n kube-system rollout status daemonset/calico-node --timeout=4m \
     && kubectl -n kube-system rollout status deployment/calico-kube-controllers --timeout=2m; then
    echo "Calico is ready."
    echo "::endgroup::"
    exit 0
  fi
  echo "Calico not ready on attempt ${i}/${attempts}."
  echo "::endgroup::"
  if [ "${i}" -lt "${attempts}" ]; then
    echo "Retrying in ${delay}s..."
    sleep "${delay}"
  fi
done

echo "::error::Calico did not become ready after ${attempts} attempts."
echo "===== Diagnostics: kube-system pods ====="
kubectl -n kube-system get pods -o wide || true
echo "===== Diagnostics: describe calico-node ====="
kubectl -n kube-system describe daemonset/calico-node || true
kubectl -n kube-system describe pods -l k8s-app=calico-node || true
echo "===== Diagnostics: recent kube-system events ====="
kubectl -n kube-system get events --sort-by=.lastTimestamp | tail -50 || true
exit 1
