# Keep-Warm GPU Nodes

A workload that runs many short GPU Jobs back to back, such as a
hyperparameter search, can spend more time waiting for nodes than training.
GCO's default GPU pool, `gpu-x86-pool`, removes an empty node 30 seconds after
its last pod finishes, so each Job waits for a new instance and pulls its
image again. The keep-warm pool keeps an empty node for a window you set, so
the next Job runs on the same node without either wait, and it records how
long each node sat idle so you can account for that cost.

## Table of Contents

- [How it works](#how-it-works)
- [Submitting to the keep-warm pool](#submitting-to-the-keep-warm-pool)
- [Reading the idle time](#reading-the-idle-time)
- [Setting the window](#setting-the-window)
- [Cost](#cost)
- [Limits](#limits)

## How it works

The `gpu-keep-warm-pool` NodePool
([`47-nodepool-gpu-keep-warm.yaml`](../lambda/kubectl-applier-simple/manifests/47-nodepool-gpu-keep-warm.yaml))
offers the same instance families as `gpu-x86-pool`. It differs in three ways:

- **Opt-in only.** Its nodes carry a `gco.aws/keep-warm=true:NoSchedule`
  taint and a `gco.aws/keep-warm: "true"` label. Only a pod that tolerates the
  taint and selects the label runs there, so other GPU Jobs keep using
  `gpu-x86-pool`.
- **A longer wait before removal.** Karpenter removes an empty node only after
  it has had no pod added or removed for `consolidateAfter`, which is set from
  the cdk.json `gpu_keep_warm.window` (default 10 minutes). A finished Job's
  pod counts as removed, and DaemonSet pods do not count at all.
- **Expired nodes reclaimed together.** The pool's disruption budget lets
  Karpenter remove every node whose window has passed at once, so idle time is
  not stretched by a 10% budget when many nodes go idle together. Other
  disruption reasons, such as drift, stay limited to 10% of the nodes, as on
  `gpu-x86-pool`.

When the next opted-in pod is created, the Kubernetes scheduler places it on
the waiting node if it fits, before Karpenter would consider launching
anything. That pod therefore needs no new NodeClaim, and its image is already
cached on the node.

## Submitting to the keep-warm pool

Add `--keep-warm` to any submit command:

```bash
gco jobs submit trial.yaml --keep-warm
gco jobs submit-direct trial.yaml -r us-east-1 --keep-warm
gco jobs submit-sqs trial.yaml -r us-east-1 --keep-warm
gco jobs submit-queue trial.yaml -r us-east-1 --keep-warm
gco queue submit trial.yaml -r us-east-1 --keep-warm
```

`--keep-warm` adds the node selector and the toleration to every Job,
CronJob and Pod in the manifest. It refuses, before anything is submitted, a
manifest it cannot place: another kind, a pod without an `nvidia.com/gpu`
request, or a pod whose node selector asks for something no keep-warm node
has, such as another NodePool, workload type or architecture, an instance
type outside the pool's families, or SageMaker HyperPod capacity. A selector
the pool can satisfy, such as an instance type in its families or a zone,
stays as it is.
The MCP server's `submit_job_api` and `submit_job_sqs` tools take
`keep_warm: true` for the same effect.

A caller of the API writes the two fields itself, which is what
`--keep-warm` adds:

```yaml
spec:
  template:
    spec:
      nodeSelector:
        gco.aws/keep-warm: "true"
        # Pin the instance type so consecutive Jobs fit the same node.
        node.kubernetes.io/instance-type: g6.xlarge
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
        - key: gco.aws/keep-warm
          operator: Equal
          value: "true"
          effect: NoSchedule
      containers:
        - name: trial
          image: pytorch/pytorch:2.14.1-cuda12.6-cudnn9-runtime
          resources:
            limits:
              nvidia.com/gpu: 1
```

Every submission path applies the same check to a manifest that selects the
pool: it must tolerate the pool's taint, its pods must request
`nvidia.com/gpu`, its node selector must not also ask for something no
keep-warm node has, and it must not be a Deployment or StatefulSet, whose
pods never finish and would hold the node for good. A Job labelled for the
`gco-hyperpod` Kueue queue is refused too, because that queue sends its pods
to HyperPod nodes. A DaemonSet may select the pool without a GPU.

Reuse only happens when the next pod fits the waiting node. Pin
`node.kubernetes.io/instance-type` so every Job of a run asks for the same
instance type. Without the pin Karpenter picks the cheapest instance that
fits each Job, and a later Job that fits the waiting node still lands on it.

## Reading the idle time

The health monitor records, on the Job whose pod finished last on a node, how
long that node then sat idle. It writes the record onto the Job as the
`gco.aws/keep-warm-idle` annotation, and Get Job reports it parsed as a
`keep_warm` block ([API.md](API.md#get-job)):

```bash
gco jobs get trial-007 -r us-east-1          # a "Keep-warm idle time" table
gco -o json jobs get trial-007 -r us-east-1  # the keep_warm block
gco -o yaml jobs get trial-007 -r us-east-1  # the same block as YAML
```

Each interval starts when that Job's pod finished. It ends in one of two ways:

- **`reused`**: the next pod was bound to the node. `until` is when.
- **`reclaimed`**: the node is gone. `until` is when the health monitor
  noticed, which is at most one 30-second loop after the instance stopped.

`idle_seconds` totals the closed intervals, so a caller that runs many Jobs
can add up its idle node time Job by Job. The idle time after a run's last
Job is the window plus the time EKS Auto Mode takes to remove the node: in a
live validation run with the default 600-second window, that interval read
1,057 seconds. The interval stays open until the node is gone.

The health monitor rebuilds the intervals on every loop from the pods each
node still lists. An interval shorter than one loop is therefore recorded
too, such as the few seconds between two Jobs that a script submits back to
back, as long as both pods still exist when the health monitor looks.

The record lives as long as the Job does: read it before you delete the Job,
or before `ttlSecondsAfterFinished` deletes it. Records are written only for
Jobs in `gco-jobs`. The health monitor's Role there
(`gco-health-monitor-keep-warm-records` in
[`02-rbac.yaml`](../lambda/kubectl-applier-simple/manifests/02-rbac.yaml))
lets it read and patch Jobs. Another namespace in
`job_validation_policy.allowed_namespaces` needs the same Role and RoleBinding
there; until then the health monitor logs once that it may not patch Jobs in
that namespace. A bare Pod has no Job to hold a record, so its idle time is
not recorded.

## Setting the window

```json
"gpu_keep_warm": {
  "window": "10m"
}
```

`window` is a duration of hours, minutes and seconds, such as `90s`, `10m` or
`1h30m`, from 30 seconds to 24 hours. Redeploy the regional stacks to apply a
change (`gco stacks deploy-all -y`). It applies to every node of the pool,
including nodes that are already waiting.

Choose a window a little longer than the longest gap you expect between the
end of one Job and the start of the next, including any time your own tooling
needs to decide what to run next.

## Cost

A waiting node is billed at its full instance price until its instance
terminates. Each run's last node idles for the whole window and then for the
minutes Karpenter takes to remove it, and so does every node that no later Job
fits. In a live validation run with the default 10-minute window, the last
node sat idle for 1,057 seconds, about 17.5 minutes. At that rate a
`g6.xlarge` adds about 30% of its hourly price in idle time at the end of
every run. Spot nodes can still be interrupted while they wait. The interval
then ends as `reclaimed`, and the next Job gets a new node.

## Limits

- The pool offers `gpu-x86-pool`'s amd64 NVIDIA families only. Arm64
  (`g5g`), EFA, Neuron and inference workloads keep their own pools.
- A pod that tolerates every taint (`operator: Exists` without a key) can be
  scheduled onto a keep-warm node without selecting it, and keeps the node busy
  while it runs.
- The node agents GCO installs tolerate the pool's taint: the EKS add-ons'
  node agents, node-exporter, the DCGM exporter and the EFA device plugin. A
  keep-warm node is monitored like any GPU node. A DaemonSet of your own needs
  a toleration for `gco.aws/keep-warm` to run there.
- When several Jobs share a multi-GPU node, the record goes to the Job whose
  pod finished last. Earlier Jobs left the node busy, not idle.
- The pool's `limits` (1000 vCPUs, 4000 GiB of memory) are separate from
  `gpu-x86-pool`'s. Edit the manifest to change them, as for the other pools
  ([CUSTOMIZATION.md](CUSTOMIZATION.md)).
