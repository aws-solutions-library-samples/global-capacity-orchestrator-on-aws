# EKS Capabilities

Opt-in [EKS Capabilities](https://docs.aws.amazon.com/eks/latest/userguide/capabilities.html) —
the AWS-managed [AWS Controllers for Kubernetes (ACK)](https://aws-controllers-k8s.github.io/docs/)
and [kro](https://kro.run/) installations GCO can attach to each regional
cluster, with the IAM roles and the tenant RBAC they need and a
configured-versus-live status view.

Looking for Argo CD or Crossplane? GCO runs both itself, from their upstream
Helm charts: see [GitOps with Argo CD](GITOPS.md) and [Crossplane](CROSSPLANE.md).

## Table of Contents

- [Overview](#overview)
- [What each capability is](#what-each-capability-is)
- [Cost](#cost)
- [Prerequisites](#prerequisites)
- [Configuration](#configuration)
  - [ACK](#ack)
  - [kro](#kro)
  - [Run-scoped overrides](#run-scoped-overrides)
- [What the regional stack creates](#what-the-regional-stack-creates)
  - [IAM](#iam)
  - [Kubernetes objects](#kubernetes-objects)
- [Using the capabilities](#using-the-capabilities)
- [Operating capabilities](#operating-capabilities)
  - [Status and drift](#status-and-drift)
  - [Disabling a capability](#disabling-a-capability)
- [Verification](#verification)
- [Limitations](#limitations)

## Overview

EKS Capabilities are Kubernetes-native platform features that run inside
Amazon EKS rather than on your nodes. AWS installs, patches and scales them;
the cluster sees only their custom resource definitions and the objects you
create. Each one is an `AWS::EKS::Capability` resource attached to a cluster,
and a cluster can carry at most one of each type.

GCO models the two it offers as independent, **off-by-default** knobs in
`cdk.json` (`eks_capabilities.ack`, `eks_capabilities.kro`). Enabling a type
makes every selected regional stack synthesize one capability IAM role and one
capability for its cluster; the shipped `cdk.json` synthesizes exactly the
template it did before — no role, no capability, no extra Kubernetes object.
kro gets one layer more: the tenant RBAC that lets it compose workloads in the
tenant namespaces (`07-kro-tenant-access.yaml`).

## What each capability is

| Type | cdk.json key | What it does | What GCO adds |
|------|--------------|--------------|---------------|
| ACK | `ack` | Manage AWS resources (S3 buckets, SQS queues, RDS databases, IAM roles, …) as Kubernetes custom resources, continuously reconciled | The capability role with exactly the AWS permissions you configure: managed policies on the role itself, or per-service roles it may assume (IAM Role Selectors) |
| kro | `kro` | Compose Kubernetes (and ACK) resources into higher-level custom APIs with `ResourceGraphDefinition`s | The capability role (no AWS permissions) and the RBAC to compose the tenant workload kinds in `gco-jobs` and `gco-inference` |

## Cost

Capabilities are billed for every hour each one is active on a cluster, and
the Kubernetes resources they manage (ACK resources, kro instances) are billed
hourly as well; see the [Amazon EKS pricing page](https://aws.amazon.com/eks/pricing/).
A capability enabled for two regions is two capabilities. This is why every
type ships disabled and why `regions` lets you enable one on a subset of
clusters. The AWS resources ACK creates are billed as usual.

## Prerequisites

- **Regions.** Capabilities are available in the commercial AWS Regions where
  EKS is available. `regions` lets you exclude a Region.
- **ACK permissions.** ACK can call no AWS API until you grant it something:
  a managed policy in `iam_policy_arns`, or roles in `assume_role_arns` that
  its IAM Role Selectors point at.
- **kubectl access** is not required to enable or inspect a capability.
  `gco stacks capabilities status` reads the EKS API only; creating ACK
  resources and kro instances is ordinary `kubectl apply` against the cluster.

## Configuration

The block lives in `cdk.json` under `context.eks_capabilities`. Every knob
has a default; only what you set changes. The full schema with defaults:

```json
"eks_capabilities": {
  "ack": {
    "enabled": false,
    "regions": [],
    "disabled_services": [],
    "enable_cross_namespace": false,
    "assume_role_arns": [],
    "iam_policy_arns": []
  },
  "kro": {
    "enabled": false,
    "regions": []
  }
}
```

`regions: []` means every regional deployment region; a non-empty list
selects a subset and must name regions from `deployment_regions.regional`.
Unknown keys (including the retired `argocd` block), wrong types and
malformed ARNs fail at synthesis with the offending path in the message.

### ACK

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `false` | Attach the ACK capability |
| `regions` | `[]` | Regional deployment regions to attach it to (empty = all) |
| `disabled_services` | `[]` | ACK service controllers to leave out (ACK's `disabledServices`) |
| `enable_cross_namespace` | `false` | ACK's cross-namespace resource references |
| `assume_role_arns` | `[]` | IAM role ARNs the capability role may `sts:AssumeRole` — the per-service roles ACK's [IAM Role Selectors](https://aws-controllers-k8s.github.io/docs/guides/iam-role-selector) point at, the documented least-privilege model. An ARN may carry a `*` pattern; that pattern is the only wildcard the role gets |
| `iam_policy_arns` | `[]` | Managed IAM policies attached to the capability role itself — the documented simple permission setup. AWS managed policies (`arn:aws:iam::aws:policy/…`) follow the stack's partition; customer managed ones are used verbatim |

Empty `assume_role_arns` and `iam_policy_arns` grant the capability role no
AWS permissions at all. A minimal block that lets ACK manage SQS queues:

```json
"eks_capabilities": {
  "ack": {
    "enabled": true,
    "iam_policy_arns": ["arn:aws:iam::aws:policy/AmazonSQSFullAccess"]
  }
}
```

### kro

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `false` | Attach the kro capability |
| `regions` | `[]` | Regional deployment regions to attach it to |

kro composes existing Kubernetes and ACK resources; its role needs no AWS
permissions. What it may compose is decided by Kubernetes RBAC, below.

### Run-scoped overrides

For one deploy without editing `cdk.json` — the same idea as
[`--enable`](CUSTOMIZATION.md#run-scoped-enablement-overrides) — pass a JSON
object as the `eks_capabilities_overrides` CDK context. It is deep-merged
over the `cdk.json` block, so partial objects work and the merged result is
validated exactly like the file:

```bash
cdk deploy gco-us-east-1 --context 'eks_capabilities_overrides={"kro": {"enabled": true}}'
```

The [live-validation harness](LIVE_RELEASE_VALIDATION.md) (`--eks-capabilities`)
and the [example harness](EXAMPLE_VALIDATION.md) use this to prove
capabilities from a clean checkout. As with `--enable`, the next plain deploy
without the context removes what the override created.

## What the regional stack creates

### IAM

One **capability role** per enabled type, trusted by
`capabilities.eks.amazonaws.com` for `sts:AssumeRole` and `sts:TagSession`
(the documented trust policy). Roles carry only what you configured:

| Role | Grants |
|------|--------|
| `EksCapabilityAckRole` | `sts:AssumeRole` on exactly `assume_role_arns`; the managed policies in `iam_policy_arns` |
| `EksCapabilityKroRole` | nothing |

No role gets a wildcard or a managed policy the operator did not spell out:
cdk-nag's `IAM5` finding is acknowledged only for ARN patterns you wrote into
`cdk.json`, and `IAM4` only per AWS managed policy you listed.

The `AWS::EKS::Capability` resources are named `<project_name>-<type>`
(`gco-ack`, `gco-kro`) with `DeletePropagationPolicy: RETAIN`, the only value
the service supports: removing a capability leaves the objects it created in
the cluster and in AWS. The stack exports `EksCapability<Type>Arn` and
`EksCapability<Type>RoleArn`.

### Kubernetes objects

EKS gives each capability an access entry with its own access policy; for
kro that is `AmazonEKSKROPolicy` — `ResourceGraphDefinition`s and their
instances — but no permission to create the resources an RGD composes. GCO
grants the tenant allow-list instead of cluster-admin, in one base-pass
manifest of the kubectl-applier
([`lambda/kubectl-applier-simple/manifests/`](../lambda/kubectl-applier-simple/manifests/README.md))
that is gated on a token the regional stack emits only when kro is enabled
for that region:

| Manifest | Gate | Objects |
|----------|------|---------|
| `07-kro-tenant-access.yaml` | kro enabled | ClusterRole/Binding `gco-kro-read` (get/list/watch on the tenant workload kinds cluster-wide, never Secrets, so kro can watch what it owns); Role/RoleBinding `gco-kro-compose` in `gco-jobs` and `gco-inference` (the apply/prune verbs on the same kinds, including Secrets there). All bound to the Kubernetes user the capability acts as, `arn:<partition>:sts::<account>:assumed-role/<kro capability role>/KRO` |

`ResourceQuota`, `LimitRange`, `NetworkPolicy`, `Role` and `RoleBinding` are
absent on purpose — the guardrail kinds of the tenant namespaces — so an RGD
instance can never widen its own ceiling, open the network posture or grant
itself permissions, and nothing is writable in `gco-system`. The write rules
are the same list Argo CD's and Crossplane's tenant grants use; a unit test
pins the three to each other. To compose another kind (an ACK resource, for
instance), bind an extra Role naming it to the same user in the tenant
namespace. Disabling kro prunes exactly these objects on the next deploy.

## Using the capabilities

Both are used through their own custom resources, applied with `kubectl`:

- **ACK** — create a resource of an ACK service API in a tenant namespace;
  the controller creates and reconciles the AWS resource with the capability
  role's permissions. [`examples/ack-sqs-queue.yaml`](../examples/README.md)
  is an SQS `Queue` in `gco-jobs`; it needs a grant that covers SQS (the
  `AmazonSQSFullAccess` block above). Delete the object and ACK deletes the
  queue.
- **kro** — apply a `ResourceGraphDefinition` (a new API), then instances of
  it. [`examples/kro-batch-api.yaml`](../examples/README.md) defines a
  `BatchJob` API in `kro.run/v1alpha1` whose instances compose a Job in their
  own namespace, and [`examples/kro-batch-job.yaml`](../examples/README.md)
  is one instance in `gco-jobs`.

Objects written by ACK and kro do not pass through the GCO manifest API, so
its [job admission policy](CUSTOMIZATION.md#security-policy-configuration)
does not apply to them; the namespace `ResourceQuota`/`LimitRange` and the
RBAC above are the controls on this path.

## Operating capabilities

### Status and drift

```bash
gco stacks capabilities status                      # first deployment region
gco stacks capabilities status --all-regions --output json
```

For each type the command reports whether `cdk.json` enables it for the
region, whether it is attached (`ListCapabilities` / `DescribeCapability`),
its status and version, and a `drift` sentence when the two disagree:
configured but not attached (deploy), attached but disabled (the next deploy
removes it), or a status other than `ACTIVE` (with the capability's health
issues). Capabilities on the cluster that GCO did not create appear under
`unmanaged`. The command exits nonzero on drift, so it works as a check. The
[`eks_capabilities_status`](../gco_mcp/tools/README.md#stackspy) MCP tool
returns the same document.

### Disabling a capability

Set `enabled: false` (or remove the region from `regions`) and deploy. The
stack deletes the `AWS::EKS::Capability`; with `RETAIN` propagation the
objects the tool created stay: ACK resources keep existing in AWS and stop
reconciling, kro instances stay as they are. The applier prunes GCO's kro
RBAC. Delete the ACK and kro objects yourself first if you want them gone.

## Verification

- Unit tests pin the config schema, the synthesized roles (managed-policy
  attachments included), capabilities and outputs, the kro token and prune
  inventory, and the CLI (`tests/test_eks_capabilities.py`,
  `tests/test_eks_capabilities_cli.py`, the kro cases in
  `tests/test_kubectl_applier.py`).
- The kind CI job `integration:kind:cluster-e2e` renders
  `07-kro-tenant-access.yaml` with the stack's own helpers, proves the fence
  by impersonating the kro user, and runs the applier's disable-path prune
  ([`.github/CI.md`](../.github/CI.md#the-kro-capabilitys-tenant-rbac)).
- The [example harness](EXAMPLE_VALIDATION.md) proves the capabilities on a
  live cluster: `kro-batch-job` enables kro, applies the companion API and an
  instance, and waits for the composed Job to complete; `ack-sqs-queue`
  enables ACK with the SQS grant and waits for the queue to sync.
- The [live-validation harness](LIVE_RELEASE_VALIDATION.md) with
  `--eks-capabilities ack,kro` requires each capability attached, `ACTIVE`
  and drift-free in every Region.

## Limitations

- **One capability of each type per cluster** is an EKS limit; GCO's
  deterministic names (`<project>-<type>`) make a second GCO deployment in
  the same account collide only if it targets the same cluster, which it
  never does.
- **Delete propagation is `RETAIN` only.** Nothing GCO does can make
  removing a capability delete what the tool created.
- **kro composes the tenant allow-list only.** Cluster-scoped objects, CRDs,
  NodePools and the guardrail kinds are outside the grant by design; platform
  changes go through `cdk.json` and `gco stacks deploy`.
- **`gco stacks capabilities status` reads the AWS API only.** The state of
  individual ACK resources and kro instances is in their own `status`.
- **Commercial partitions only**, following EKS Capabilities' own
  availability.
