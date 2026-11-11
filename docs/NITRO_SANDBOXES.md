# AWS Nitro Sandboxes

An [AWS Nitro Sandbox](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing.html)
runs one pod in its own lightweight virtual machine, with its own Linux kernel,
isolated from the node and from every other pod by the Nitro hypervisor. Use it
for code you do not trust: code an AI agent generated, a tenant's container, a
build that runs third-party scripts. A container escape inside the sandbox
reaches the sandbox, not the node.

GCO runs sandboxed pods through its normal job paths. `gco jobs submit
--sandbox isolated` puts a CPU manifest in a sandbox without editing it, and a
manifest that already names a sandbox runtime class goes through as it is. GCO
rejects at submission the sandboxed manifests that could never start, which
would otherwise be accepted and then sit Pending or podless.

## Table of Contents

- [Run a job in a sandbox](#run-a-job-in-a-sandbox)
- [Requirements](#requirements)
- [What GCO checks at submission](#what-gco-checks-at-submission)
- [Queueing with Kueue](#queueing-with-kueue)
- [Confidential mode](#confidential-mode)
- [Sizing](#sizing)
- [What GCO does not do](#what-gco-does-not-do)
- [How it is tested](#how-it-is-tested)

## Run a job in a sandbox

Add `--sandbox isolated` to any submit command:

```bash
gco jobs submit examples/simple-job.yaml --region us-east-1 --sandbox isolated
gco jobs submit-sqs examples/simple-job.yaml --region us-east-1 --sandbox isolated
gco jobs submit-direct examples/simple-job.yaml -r us-east-1 --sandbox isolated
gco jobs submit-queue examples/simple-job.yaml -r us-east-1 --sandbox isolated
```

`--sandbox isolated` sets `runtimeClassName: isolated-sandbox` on the pod
template of every Pod, Job, CronJob, Deployment, StatefulSet and DaemonSet in
the submission, and leaves ConfigMaps and Services alone. It fails before
sending anything, rather than let a pod run outside the sandbox you asked for,
when a manifest:

- is a TrainJob, whose pods come from its training runtime, not from the
  manifest;
- already names a different runtime class;
- would break a [submission check](#what-gco-checks-at-submission), such as a
  GPU request.

The same holds for `submit-direct`, which skips GCO's server-side admission.
`--check-policy` judges the manifest as `--sandbox` changes it.

To write the runtime class yourself, set it on the pod template.
[`examples/nitro-sandbox-job.yaml`](../examples/nitro-sandbox-job.yaml) does
this, and its pod fails unless its disks are virtio-blk devices, which a
container on a Nitro instance never sees outside a sandbox:

```bash
gco jobs submit examples/nitro-sandbox-job.yaml --region us-east-1
gco jobs logs nitro-sandbox-job -r us-east-1
```

From an agent, the MCP `submit_job_api` and `submit_job_sqs` tools take
`sandbox="isolated"`.

## Requirements

- **EKS 1.36 or later.** `cdk.json` `kubernetes_version` sets the cluster's
  minor ([upgrading it](MAINTENANCE.md)).
- **A Region where EKS offers Nitro Sandboxes.** The
  [EKS User Guide](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing.html)
  lists the US commercial Regions. GCO does not keep its own list: EKS creates
  the `isolated-sandbox` and `confidential-sandbox` RuntimeClasses only where it
  supports them, and GCO asks the cluster for the RuntimeClass at submission
  (next section).
- **Nothing to enable.** EKS Auto Mode
  [provisions a sandbox-capable node](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-isolated-setup.html)
  when a sandboxed pod is pending, from an instance type that supports Nitro
  Enclaves.
- **CPU only.** A sandbox has no GPU or device passthrough, so GPU, Neuron and
  EFA work runs without a sandbox.

## What GCO checks at submission

The REST manifest processor and the SQS queue processor run the same checks,
from `gco.job_admission`, and reject with `Nitro Sandbox validation failed:`
followed by the reason. `gco jobs check-policy` reports all but the last,
which asks the cluster, under the `sandbox` check. Each one is a documented
[Nitro Sandbox constraint](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-limitations.html)
that otherwise fails late.

| Rejected | Why | Otherwise |
|---|---|---|
| A sandboxed pod that requests `nvidia.com/gpu`, `aws.amazon.com/neuron` or `vpc.amazonaws.com/efa` | No device passthrough | Pending forever: no sandbox node offers the device |
| `hostNetwork: true` | The sandbox has its own network stack | The pod fails to start |
| `hostUsers: false` | User namespaces are not supported on sandbox nodes | kubelet does not start the pod |
| A `sandbox.amazonaws.com/` annotation on a workload's own metadata | Kubernetes copies pod annotations from the pod template only | Accepted and ignored: a `network-isolated` annotation would leave the network open |
| A confidential pod without a `sandbox.amazonaws.com/policy` annotation, or without its own `automountServiceAccountToken` | See [Confidential mode](#confidential-mode) | The sandbox denies the pod |
| A confidential TrainJob | Its pods are built from the training runtime plus the job's patches, so the final pod spec a policy must match is not in the manifest | The sandbox denies the pods |
| A sandboxed manifest on a cluster without that RuntimeClass | EKS older than 1.36, or a Region without Nitro Sandboxes | The Job is accepted and its pods are refused at creation, so it has none |

The RuntimeClass check reads `node.k8s.io/runtimeclasses` with `get` on the two
sandbox classes only (the `gco-manifest-processor-cluster-read` ClusterRole),
and caches the answer for five minutes. Only a 404 counts as missing: if the
read fails for another reason, the submission goes ahead and the failure is
logged, because the API server would still refuse a pod whose RuntimeClass does
not exist.

## Queueing with Kueue

Each sandbox pod's RuntimeClass overhead includes one
`eks.amazonaws.com/nitro-sandbox` slot, and
[Kueue](KUEUE.md) adds that overhead to the workload's requests. A ClusterQueue
that does not cover a requested resource never admits the workload, so
`gco-cluster-queue` covers the slot, with the namespace pod quota as its
nominal quota. A sandboxed Job labeled `kueue.x-k8s.io/queue-name: gco-default`
is admitted like any other.

A ClusterQueue you create yourself needs the same entry under
`coveredResources`, or sandboxed Jobs queued to it stay suspended.

## Confidential mode

In [confidential mode](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-confidential-setup.html)
the node is untrusted: a Cedar policy pins the pod's image digests, arguments,
environment, mounts and security settings, and the sandbox denies a pod that
does not match it. `--sandbox` offers only `isolated` because GCO cannot write
that policy for you; `eks-nitro-sandbox-genpolicy` generates it from the exact
pod spec that runs.

GCO accepts a confidential manifest you prepared:

1. Set `runtimeClassName: confidential-sandbox` on the pod template.
2. Set `automountServiceAccountToken` on the pod template, `true` or `false`.
   GCO sets it to `false` when it is missing, and the policy pins the token
   mount, so a default added after you generate the policy gets the pod denied.
3. Run `eks-nitro-sandbox-genpolicy --profile confidential manifest.yaml` and
   put the output in the pod template's `sandbox.amazonaws.com/policy`
   annotation.
4. Submit with `gco jobs submit` (no `--sandbox`).

Confidentiality also depends on AWS KMS key policies that check the
attestation, which is outside GCO; the EKS guide's
[KMS section](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-confidential-setup.html#sandboxing-kms-attestation)
covers them.

## Sizing

From the EKS guide's
[autoscaling](https://docs.aws.amazon.com/eks/latest/userguide/autoscaling.html)
and [considerations](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-limitations.html)
pages:

- The scheduler adds a fixed memory overhead to each sandbox pod: 256 MiB for
  `isolated-sandbox`, 512 MiB for `confidential-sandbox`. It counts against
  the `gco-jobs` ResourceQuota, but not against GCO's per-manifest caps, which
  read the manifest's own requests.
- A node runs at most 5 sandboxes per vCPU, and their vCPUs together can be at
  most 5 times the node's.
- On instance types with two threads per core, two sandboxes never share a
  core, so request CPU in multiples of two vCPUs or use Graviton.
- A sandbox's CPU and memory are fixed at boot: in-place resize does not apply.
- `emptyDir` and writable-layer usage report as zero; set `ephemeral-storage`
  requests and limits.

AWS credentials come from EKS Pod Identity: the `gco-service-account` in
`gco-jobs` has an association. IMDS serves only placement paths inside a
sandbox, never credentials.

## What GCO does not do

- Generate Cedar policies or KMS key policies.
- Keep warm pools of sandbox nodes.
- Run inference endpoints in sandboxes: `gco inference deploy` does not take a
  runtime class.
- Support self-managed or managed node groups with a custom sandbox AMI. GCO
  clusters use EKS Auto Mode.

## How it is tested

- `tests/test_nitro_sandboxes.py`: every admission rule, the RuntimeClass
  check and its cache, the REST and SQS paths agreeing on each case,
  `--sandbox` on every submit command and MCP tool, and the
  `gco-cluster-queue` coverage.
- `tests/test_rbac_manifest.py`: the RuntimeClass read is `get` on the two
  sandbox classes and nothing more.
- The `sandboxes` action of the [live release validation](LIVE_RELEASE_VALIDATION.md)
  submits a Kueue-queued sandboxed Job through the API on a fresh deployment and
  requires it to finish inside a sandbox.
- [Example validation](EXAMPLE_VALIDATION.md) runs
  `examples/nitro-sandbox-job.yaml` through `gco jobs submit`.
