# Distributed Training with Kubeflow Trainer

GCO includes [Kubeflow Trainer v2](https://github.com/kubeflow/trainer) for multi-node distributed training through the `TrainJob` API. The trainer is enabled by default.

## Table of Contents

- [Overview](#overview)
- [What Gets Deployed](#what-gets-deployed)
- [Submitting a TrainJob](#submitting-a-trainjob)
- [Validation Semantics](#validation-semantics)
- [GPU Training](#gpu-training)
- [Gang Scheduling with Kueue](#gang-scheduling-with-kueue)
- [Spot Capacity Guidance](#spot-capacity-guidance)
- [Runtimes](#runtimes)
- [Troubleshooting](#troubleshooting)

## Overview

Kubeflow Trainer v2 separates *what to train* from *how the cluster runs it*. You submit a small `TrainJob` that names a runtime blueprint and declares your image, command, and node count; the trainer controller compiles that into a [JobSet](https://jobset.sigs.k8s.io/) with the correct `torchrun` rendezvous wiring, indexed pods, and restart semantics.

Trainer v2 supersedes the legacy training-operator (`PyTorchJob`/`TFJob`/`MPIJob`): one `TrainJob` kind plus reusable runtime blueprints replaces the per-framework CRD zoo. If you are coming from a `PyTorchJob`, the short version: there is no master/worker split (`spec.trainer.numNodes` peers, rank 0 plays the master), the `MASTER_ADDR`/`RANK` env wiring is replaced by injected `PET_*` variables that `torchrun` consumes natively, and pod-level customization moves into `runtimePatches` or a custom runtime.

**When to use TrainJob:**

- Multi-node PyTorch training without hand-writing JobSets or StatefulSets
- Distributed workloads that need consistent rendezvous wiring across teams
- Gang-scheduled training through the platform's default Kueue queue
- Anything you previously ran as a `PyTorchJob`

For single-node GPU jobs, a plain `batch/v1` Job ([`examples/gpu-job.yaml`](../examples/gpu-job.yaml)) remains the simplest path. For hand-rolled multi-pod topologies, see [`examples/multi-gpu-training.yaml`](../examples/multi-gpu-training.yaml) (indexed Job + headless Service) and [`examples/efa-distributed-training.yaml`](../examples/efa-distributed-training.yaml).

## What Gets Deployed

The `kubeflow-trainer` Helm chart (pinned in `lambda/helm-installer/charts.yaml`) installs into the `kubeflow-trainer` namespace:

| Component | Description |
|-----------|-------------|
| trainer-controller-manager | Compiles TrainJobs into JobSets; reconciles status |
| JobSet controller (subchart) | The replicated-Job engine TrainJobs compile to |
| CRDs | `TrainJob`, `TrainingRuntime`, `ClusterTrainingRuntime` (chart templates, so upgrades refresh them) |

The `torch-distributed` `ClusterTrainingRuntime` blueprint is applied through GCO's post-Helm kubectl pass rather than the chart's own post-install hook (the upstream hook downloads kubectl into an alpine pod at run time — an unpinned network fetch). Same bytes, pinned and reviewable, pruned automatically when the trainer is disabled.

## Submitting a TrainJob

TrainJobs ride the same validated submission paths as every other workload — the kind is in the platform allowlist, pinned to the exact `trainer.kubeflow.org/v1alpha1` group/version:

```bash
gco jobs submit-sqs examples/kubeflow-trainjob.yaml --region us-east-1
```

The shipped example runs a 2-node CPU-sized `torchrun` all-reduce that proves the distributed plumbing end to end. The essential shape:

```yaml
apiVersion: trainer.kubeflow.org/v1alpha1
kind: TrainJob
metadata:
  name: my-training-run
  namespace: gco-jobs
spec:
  runtimeRef:
    name: torch-distributed
  trainer:
    image: pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime
    numNodes: 2
    numProcPerNode: 1
    resourcesPerNode:
      requests:
        cpu: "500m"
        memory: "1Gi"
    command: ["torchrun", "/workspace/train.py"]
```

`torchrun` inherits the rendezvous configuration (`PET_*` environment) the controller injects, so your training script only needs `torch.distributed.init_process_group()`.

### What the controller creates

A TrainJob compiles to a JobSet whose single child Job `<name>-node-0` runs `numNodes` indexed pods; the completion index is the node rank (`PET_NODE_RANK`). The `gco jobs` commands understand this automatically:

```bash
gco jobs get my-training-run -r us-east-1              # status from TrainJob conditions
gco jobs logs my-training-run -r us-east-1             # rank 0 logs
gco jobs logs my-training-run -r us-east-1 --node 1    # rank 1 logs
gco jobs delete my-training-run -r us-east-1           # cascades to JobSet, Jobs, pods
```

## Validation Semantics

TrainJobs pass the same security pipeline as every other submission, on both the REST and SQS paths:

- **Image trust:** `spec.trainer.image` plus every container reachable through `runtimePatches` must clear the trusted-registry allowlist.
- **Resource caps:** the per-manifest CPU/memory/GPU budget counts `resourcesPerNode` multiplied by `numNodes` — a 16-node job is sixteen nodes of spend, not one.
- **Security policy:** hostPath volumes, privileged containers, added capabilities, and the other `manifest_security_policy` toggles are enforced across every pod-spec view a TrainJob can produce, including complete pod specs nested inside `runtimePatches`.
- **Accelerator tolerations:** a GPU request must carry a matching toleration; for TrainJobs the toleration lives in a runtime patch (see below) while the request lives in `spec.trainer`, and the validators union both sides across views.

If the trainer addon is disabled, submission fails with an actionable message pointing at the `helm.kubeflow_trainer` toggle instead of an inscrutable discovery error.

## GPU Training

`spec.trainer` carries images and per-node resources but not tolerations, so accelerator jobs add them through a runtime patch:

```yaml
spec:
  runtimeRef:
    name: torch-distributed
  trainer:
    numNodes: 2
    numProcPerNode: 1
    resourcesPerNode:
      limits:
        nvidia.com/gpu: "1"
  runtimePatches:
    - trainingRuntimeSpec:
        template:
          spec:
            replicatedJobs:
              - name: node
                template:
                  spec:
                    template:
                      spec:
                        tolerations:
                          - key: nvidia.com/gpu
                            operator: Exists
                            effect: NoSchedule
```

EKS Auto Mode provisions GPU capacity on demand; check availability first with `gco capacity check --instance-type g4dn.xlarge --region us-east-1`. For multi-node GPU training use `nccl` as the process-group backend (the CPU example uses `gloo`).

## Gang Scheduling with Kueue

Kueue's default integrations already include `trainer.kubeflow.org/trainjob`, so gang admission needs only a label:

```yaml
metadata:
  labels:
    kueue.x-k8s.io/queue-name: gco-default
```

A labeled TrainJob is admitted all-or-nothing: every node's quota is reserved before any pod starts, which prevents the classic deadlock where half a gang holds GPUs waiting for peers that cannot schedule. Unlabeled TrainJobs run immediately, subject only to namespace quota. See [KUEUE.md](KUEUE.md) for queue topology and quotas.

## Spot Capacity Guidance

Distributed training on Spot is a cost/interruption trade:

- **Metadata-cheap, restart-tolerant jobs** (the shipped example, short fine-tunes with frequent checkpoints) tolerate Spot well. A reclaimed node fails its rank; the JobSet restart policy re-runs the gang from the last checkpoint.
- **Long gangs without checkpointing** should stay on On-Demand: one interruption discards the whole gang's progress, and large gangs multiply the interruption probability.
- Checkpoint to durable storage — the same-region [regional-shared bucket](REGIONAL_SHARED_BUCKET.md) (preferred for checkpoints: no cross-region egress), the [cluster-shared bucket](CLUSTER_SHARED_BUCKET.md), [EFS](CONCEPTS.md#storage-options), or [FSx for Lustre](CUSTOMIZATION.md#configure-fsx-for-lustre) — never to node-local disk. Note that both shared buckets are `RemovalPolicy.DESTROY`, so copy anything you need to keep before destroying a stack.
- Track spend with the built-in [cost monitoring](COST_MONITORING.md); track run metrics with [MLflow](MONITORING.md#mlflow-experiment-tracking).

## Runtimes

`runtimeRef` names a `ClusterTrainingRuntime` blueprint. GCO ships `torch-distributed` (PyTorch/torchrun). Listing what is available:

```bash
kubectl get clustertrainingruntime
```

Platform operators can add custom runtimes (different base images, initContainers, node selectors) by applying additional `ClusterTrainingRuntime` objects; user-facing TrainJobs stay unchanged. Runtime pod templates ship with `automountServiceAccountToken: false` — training pods get no Kubernetes API token unless a TrainJob opts back in through a runtime patch.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `TrainJob requires the kubeflow-trainer addon` on submit | Trainer chart disabled | Set `"kubeflow_trainer": {"enabled": true}` under `helm` in cdk.json and redeploy |
| Pods Pending, no nodes | Missing accelerator toleration | Add the toleration via `runtimePatches` (GPU variant above) |
| `no matching toleration` rejection at submit | GPU requested without toleration | Same fix — the validator catches it before the cluster does |
| Job rejected for resource caps | `numNodes` multiplied the budget | Lower per-node requests or raise `job_validation_policy.resource_quotas` |
| Labeled job stays suspended | Kueue quota exhausted | `kubectl get clusterqueue gco-cluster-queue -o yaml` for usage vs quota |

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for platform-wide debugging and [`examples/kubeflow-trainjob.yaml`](../examples/kubeflow-trainjob.yaml) for the full annotated example.
