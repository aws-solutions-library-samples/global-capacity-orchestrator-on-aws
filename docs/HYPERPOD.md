# Amazon SageMaker HyperPod

GCO can attach an [Amazon SageMaker HyperPod](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod.html)
cluster to a Region's EKS cluster. HyperPod's instance groups join `gco-<region>`
as nodes, so every scheduler GCO already runs (Kueue, Volcano, Slinky, the
Kubeflow Trainer) can place work on them, while HyperPod supplies what the
Karpenter node pools do not: deep health checks before a node takes work, a
health-monitoring agent that catches GPU, Neuron, kernel and network faults
while it runs, automatic reboot or replacement of a faulty node, and
[Training Plans](#training-plans) for GPU capacity reserved ahead of time.

HyperPod is **off by default** (`"hyperpod": {"enabled": false}` in
`cdk.json`). Nothing changes for a deployment until it is turned on.

## Table of Contents

- [What gets deployed](#what-gets-deployed)
- [How work reaches HyperPod nodes](#how-work-reaches-hyperpod-nodes)
- [Enabling it](#enabling-it)
- [Configuration reference](#configuration-reference)
- [Training Plans](#training-plans)
- [Preflight and status](#preflight-and-status)
- [Security](#security)
- [Live release validation](#live-release-validation)
- [Limitations](#limitations)

## What gets deployed

When `hyperpod.enabled` is true for a Region, the regional stack
(`gco/stacks/hyperpod_cluster.py`) adds:

| Resource | Purpose |
|----------|---------|
| `AWS::SageMaker::Cluster` `<project>-hyperpod` | The HyperPod cluster, with `Orchestrator.Eks` pointing at the regional EKS cluster and `NodeRecovery` from the block (`Automatic` by default). One instance group per `hyperpod.instance_groups` entry. |
| Execution role | Trusted by `sagemaker.amazonaws.com`; carries the AWS-managed `AmazonSageMakerClusterInstanceRolePolicy` plus the ENI, ECR and Pod Identity actions HyperPod documents for EKS orchestration, and read access to the lifecycle script. |
| `AWS::EKS::AccessEntry` of type `HYPERPOD_LINUX` | How the nodes authenticate to the cluster. The EKS cluster is access-entries only; this entry takes no access policies. |
| Security group `<project>-hyperpod-sg-<region>` | All traffic within the group (training traffic, EFA) and all traffic to and from the EKS cluster security group (kubelet and control plane, both ways). |
| Lifecycle script | `on_create.sh` (a logging no-op; EKS orchestration bootstraps the kubelet itself) placed under `hyperpod/lifecycle/` in the regional shared bucket by a custom resource, because `SourceS3Uri` is a prefix HyperPod copies whole onto every node. |
| Kubernetes objects (via the kubectl-applier) | The fixed-name `aws-hyperpod` namespace, the health-monitoring agent `DaemonSet`s and the `deep-health-check` and `job-auto-restart` RBAC the service expects (`10-hyperpod-rbac.yaml`, `51-hyperpod-health-monitoring-agent.yaml`), and a Kueue `ResourceFlavor` / `ClusterQueue` / `LocalQueue` for the nodes (`post-helm-kueue-hyperpod-queue.yaml`). |
| `nvidia-device-plugin` Helm chart | Advertises the GPUs on HyperPod nodes; EKS Auto Mode's built-in plugin covers only Auto Mode nodes. Pinned to the HyperPod nodes by label. |

Every instance group's nodes carry the labels
`gco.aws/capacity-provider=hyperpod` and
`gco.aws/hyperpod-instance-group=<name>` and the taint
`gco.aws/hyperpod=true:NoSchedule`. The taint is what keeps platform pods and
ordinary jobs on the Karpenter node pools: only work that asks for HyperPod
lands on HyperPod.

The cluster is created after the regional add-on convergence starts, so the
namespace, agent and RBAC are applying before the first node boots, and it is
deleted before the add-on layer unwinds on stack deletion.

## How work reaches HyperPod nodes

**Kueue (recommended).** The gated manifest ships a `LocalQueue` `gco-hyperpod`
in `gco-jobs` whose `ClusterQueue` admits onto the `gco-hyperpod-flavor`
`ResourceFlavor`. The flavor carries the node label as `nodeLabels` and the
HyperPod taint as a toleration, and Kueue injects both into every admitted
workload. A Job needs nothing but the queue name:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: train-on-hyperpod
  namespace: gco-jobs
  labels:
    kueue.x-k8s.io/queue-name: gco-hyperpod
spec:
  suspend: true
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: train
          image: public.ecr.aws/docker/library/python:3.12-slim
          command: ["python", "-c", "print('hello from hyperpod')"]
          resources:
            limits:
              nvidia.com/gpu: 1
```

The `ClusterQueue`'s nominal quotas come from `hyperpod.queue`; they are
admission caps, the instance groups are the real limit.

**Any other scheduler.** Set the node selector and toleration yourself:

```yaml
nodeSelector:
  gco.aws/capacity-provider: hyperpod
tolerations:
  - key: gco.aws/hyperpod
    operator: Equal
    value: "true"
    effect: NoSchedule
```

**Inference.** `gco inference deploy --capacity-type hyperpod` (and the MCP
`deploy_inference` tool) pins an endpoint's model pods to HyperPod nodes the
same way.

The jobs API reports where a job's pods landed (`GET /api/v1/jobs/{name}`,
`gco jobs get`): `node_capacity_type` is `hyperpod` for a HyperPod node, and
the node's HyperPod labels (`sagemaker.amazonaws.com/node-health-status`,
`sagemaker.amazonaws.com/deep-health-check-status`, the instance group) are
included in `node_labels`.

## Enabling it

```bash
# Turn it on in cdk.json (writes hyperpod.enabled and the instance groups you pass)
gco stacks hyperpod enable --instance-type ml.g5.8xlarge --instance-count 1
gco stacks deploy-all -y

# Or for one deploy only, without touching cdk.json
gco stacks deploy-all -y --enable hyperpod
```

`gco stacks deploy` and `deploy-all` run the HyperPod preflight first (see
below) when the Region has HyperPod on; `--skip-hyperpod-preflight` bypasses
it. Turning HyperPod off (`gco stacks hyperpod disable` and a redeploy)
deletes the cluster and its instances; `removal_policy: retain` keeps the
cluster, the execution role and the security group when the stack is deleted.

A per-Region overlay, `hyperpod_regions`, deep-merges over the global block:

```json
"hyperpod": {"enabled": false, "node_recovery": "Automatic"},
"hyperpod_regions": {
  "us-east-1": {
    "enabled": true,
    "availability_zone": "us-east-1b",
    "instance_groups": [{"name": "p5", "instance_type": "ml.p5.48xlarge", "instance_count": 4}]
  }
}
```

## Configuration reference

All keys live under `hyperpod` in `cdk.json` (and under `hyperpod_regions.<region>`
for a per-Region overlay). Every key is validated at synth time by
`gco/hyperpod_config.py`; unknown keys fail by name.

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `false` | Deploy the HyperPod cluster in this Region. |
| `cluster_name` | `null` (`<project>-hyperpod`) | `ClusterName`; 1–63 letters, digits and hyphens. |
| `node_recovery` | `"Automatic"` | `Automatic` reboots or replaces a node the health checks fail; `None` only labels it. |
| `availability_zone` | `null` (first EKS-supported private subnet) | The zone the instance groups launch into. HyperPod clusters are zonal; set this to match a Training Plan or Capacity Block. |
| `instance_groups[]` | one group `accelerated`, `ml.g5.8xlarge`, count 1 | The instance groups; names must be unique, an enabled block needs at least one. |
| `instance_groups[].name` | `accelerated` | `InstanceGroupName`. |
| `instance_groups[].instance_type` | `ml.g5.8xlarge` | A HyperPod (`ml.*`) instance type. |
| `instance_groups[].instance_count` | `1` | Instances in the group (0–10000; 0 keeps the group defined but empty). |
| `instance_groups[].threads_per_core` | `1` | 1 disables hyperthreading, 2 keeps it. |
| `instance_groups[].deep_health_checks` | `["InstanceStress", "InstanceConnectivity"]` | `OnStartDeepHealthChecks` run when an instance is created or replaced; `[]` skips them. |
| `instance_groups[].training_plan_arn` | `null` | A SageMaker Training Plan whose reserved capacity this group launches into (see below). |
| `instance_groups[].root_volume_size_gb` | `100` | Root EBS volume size of every instance in the group. |
| `queue.max_cpu` / `max_memory` / `max_gpu` | `"1000"` / `"4Ti"` / `"64"` | Nominal quotas of the `gco-hyperpod-queue` Kueue ClusterQueue (Kubernetes quantities). |
| `removal_policy` | `"destroy"` | `retain` keeps the cluster, execution role and security group on stack deletion. |

The run-scoped CDK context `hyperpod_overrides` (a JSON object) deep-merges
over the block for one deploy; the live release validation uses it to turn
HyperPod on without editing `cdk.json`.

## Training Plans

A [SageMaker Training Plan](https://docs.aws.amazon.com/sagemaker/latest/dg/reserve-capacity-with-training-plans.html)
reserves GPU capacity for a fixed window, the SageMaker counterpart of an EC2
Capacity Block. `gco capacity` searches and lists them next to the Capacity
Block commands:

```bash
# Offerings for 4x ml.p5.48xlarge for a week, starting on or after a date, usable by a HyperPod cluster
gco capacity find-training-plans -i ml.p5.48xlarge -c 4 -r us-east-1 --duration-hours 168 --start-after 2026-11-10

# Plans the account already holds
gco capacity training-plans -r us-east-1
```

To launch an instance group into a plan, put its ARN in
`instance_groups[].training_plan_arn` (the group's `instance_type` and
`availability_zone` must match the plan). Purchasing a plan is an account
commitment and stays a console or `aws sagemaker create-training-plan`
action; GCO reports the offerings and never buys one.

## Preflight and status

```bash
gco stacks hyperpod preflight -r us-east-1   # or --all-regions
gco stacks hyperpod status -r us-east-1
```

The preflight checks, per Region, what would make the deploy fail or the
nodes never join: the Region publishes a health-monitoring agent image, the
account's `<instance type> for cluster usage` Service Quotas cover every
instance group's count, each `training_plan_arn` exists and is `Active` for
the group's instance type, the EKS cluster is `ACTIVE` and uses access
entries, and the regional VPC has a private subnet in the requested zone. An
AWS error while checking is a warning; only a fact that would break the
deploy fails it. `status` shows the block as configured and, when the stack
is deployed, the cluster's `ClusterStatus`, its instance groups with their
current counts, and the node health labels of the nodes that joined.

## Security

- The nodes authenticate with the `HYPERPOD_LINUX` access entry of their
  execution role only; no `aws-auth` ConfigMap, no cluster-admin grants.
- The execution role holds the AWS-managed cluster-instance policy, the
  documented EKS-node actions, and read access to the lifecycle prefix; the
  nodes get no credentials for the regional bucket beyond that prefix.
- The security group admits security-group sources only (itself and the EKS
  cluster group), never a CIDR.
- The health-monitoring agent runs non-root with every capability dropped,
  a read-only root filesystem and `RuntimeDefault` seccomp; its host `/var/log`,
  `/dev/kmsg` and `/etc/localtime` mounts are the agent's contract.
- The `aws-hyperpod` namespace is labelled for the `privileged` Pod Security
  level because of those mounts; nothing else runs there.

## Live release validation

Every release run deploys HyperPod through the run-scoped `hyperpod_overrides`
context and the `hyperpod` action proves it end to end: the cluster is
`InService`, every instance is `Running`, every node carries
`sagemaker.amazonaws.com/node-health-status=Schedulable`, and a Job submitted
through the GCO API to the `gco-hyperpod` queue completes on a HyperPod node
(the jobs API's placement report says so). Teardown proves the cluster is
gone; the final inventory counts HyperPod clusters as a residual category.
See [LIVE_RELEASE_VALIDATION.md](LIVE_RELEASE_VALIDATION.md).

## Limitations

- EKS orchestration only. HyperPod's Slurm orchestration is not a GCO Slurm
  backend; [AWS PCS](PCS.md) and [Slinky](SLURM_OPERATOR.md) are.
- One Availability Zone per cluster, as HyperPod requires.
- The EFA and Neuron device plugins GCO installs target EC2 instance types;
  EFA on HyperPod `ml.p*`/`ml.trn*` nodes needs their node affinities
  extended (tracked as a follow-up).
- HyperPod's job auto-restart integrates with the Kubeflow Training
  Operator's `PyTorchJob` (v1); GCO ships the Kubeflow Trainer v2
  (`TrainJob`), so auto-restart applies to `PyTorchJob`s you install the
  operator for yourself.
- HyperPod task governance (HyperPod's own Kueue install) is not installed;
  GCO's Kueue is the single queueing layer.
