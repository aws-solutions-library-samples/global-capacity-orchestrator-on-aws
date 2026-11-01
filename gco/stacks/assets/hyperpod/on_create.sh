#!/bin/bash
# SageMaker HyperPod node lifecycle script (OnCreate) for GCO's EKS-orchestrated
# clusters.
#
# HyperPod runs this once on every instance it provisions, before the node
# joins the regional EKS cluster. With EKS as the orchestrator the service
# bootstraps the kubelet itself, so GCO needs nothing from this step beyond a
# record that it ran: the service requires an OnCreate script per instance
# group, and the sample in awsome-distributed-training is this same no-op.
#
# Keep it free of anything that could fail: a non-zero exit here puts the
# instance into a failed state and, with NodeRecovery=Automatic, HyperPod
# replaces it and runs this script again.
set -euo pipefail

LOG_FILE="/var/log/gco-hyperpod-on-create.log"

{
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] gco hyperpod on_create: start"
  echo "hostname: $(hostname)"
  echo "instance group: ${SAGEMAKER_INSTANCE_GROUP_NAME:-unknown}"
  echo "orchestrator: eks (no bootstrap performed by this script)"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] gco hyperpod on_create: done"
} | tee -a "$LOG_FILE"
