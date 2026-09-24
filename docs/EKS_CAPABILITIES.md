# EKS Capabilities

Opt-in [EKS Capabilities](https://docs.aws.amazon.com/eks/latest/userguide/capabilities.html) —
the AWS-managed [Argo CD](https://argo-cd.readthedocs.io/en/stable/),
[AWS Controllers for Kubernetes (ACK)](https://aws-controllers-k8s.github.io/docs/)
and [kro](https://kro.run/) installations GCO can attach to each regional
cluster — plus GCO's declarative GitOps hand-off, which points the hosted
Argo CD at a fenced repository path: by default a GCO-managed per-cluster
[AWS CodeCommit](https://docs.aws.amazon.com/codecommit/latest/userguide/welcome.html)
repository you fill with one command, or a repository of your own.

## Table of Contents

- [Overview](#overview)
- [What each capability is](#what-each-capability-is)
- [Cost](#cost)
- [Prerequisites](#prerequisites)
- [Configuration](#configuration)
  - [Argo CD](#argo-cd)
  - [ACK](#ack)
  - [kro](#kro)
  - [Run-scoped overrides](#run-scoped-overrides)
- [Identity Center bootstrap](#identity-center-bootstrap)
- [What the regional stack creates](#what-the-regional-stack-creates)
  - [IAM](#iam)
  - [Kubernetes objects](#kubernetes-objects)
- [The GitOps hand-off](#the-gitops-hand-off)
  - [Where the manifests come from](#where-the-manifests-come-from)
  - [Pushing to the CodeCommit repository](#pushing-to-the-codecommit-repository)
  - [What the tenant repository may contain](#what-the-tenant-repository-may-contain)
  - [Sync policy](#sync-policy)
  - [Repository access](#repository-access)
- [Argo CD UI](#argo-cd-ui)
- [Operating capabilities](#operating-capabilities)
  - [Status and drift](#status-and-drift)
  - [Disabling a capability](#disabling-a-capability)
- [Multi-cluster note (hub and spoke)](#multi-cluster-note-hub-and-spoke)
- [Verification](#verification)
- [Limitations](#limitations)

## Overview

EKS Capabilities are Kubernetes-native platform features — continuous
deployment, AWS resource management, resource composition — that run inside
Amazon EKS rather than on your nodes. AWS installs, patches and scales them;
the cluster sees only their custom resource definitions and the objects you
create. Each one is an `AWS::EKS::Capability` resource attached to a cluster,
and a cluster can carry at most one of each type.

GCO models them as three independent, **off-by-default** knobs in `cdk.json`
(`eks_capabilities.argocd`, `.ack`, `.kro`). Enabling a type makes every
selected regional stack synthesize one capability IAM role and one capability
for its cluster, and the shipped `cdk.json` synthesizes exactly the template
it did before — no role, no capability, no extra Kubernetes object.

Argo CD gets one layer more. On its own the hosted Argo CD does not register
the cluster it is attached to and holds no Kubernetes permissions, so GCO
also applies the wiring that makes it usable (`07-argocd-cluster-access.yaml`:
cluster registration and least-privilege RBAC) and, when
`eks_capabilities.argocd.gitops` is enabled, a **GitOps hand-off**
(`08-argocd-gitops.yaml`): a fenced `AppProject` and one root `Application`
per cluster pointing Argo CD at a repository path. Out of the box that
repository is one the regional stack creates for the cluster in AWS
CodeCommit and the capability role may pull without any credential;
`gco stacks capabilities gitops push` mirrors a local directory into it.
Point the hand-off at a repository of your own instead with
`gitops.source: git`. GCO's own platform — the services, NodePools, network
policies and Helm charts — stays under CloudFormation and the applier; the
hand-off is for tenant workloads.

## What each capability is

| Type | cdk.json key | What it does | What GCO adds |
|------|--------------|--------------|---------------|
| Argo CD | `argocd` | GitOps continuous deployment: reconciles Kubernetes manifests from Git, Helm or OCI sources into clusters, with a hosted UI behind IAM Identity Center | Cluster registration, read-all + tenant-namespace RBAC for the capability role, the optional GitOps hand-off with its per-cluster CodeCommit repository and `gitops push`, and `argocd bootstrap-identity` for the Identity Center inputs |
| ACK | `ack` | Manage AWS resources (S3 buckets, RDS databases, IAM roles, queues, …) as Kubernetes custom resources, continuously reconciled | The capability role, optionally allowed to assume per-service ACK roles |
| kro | `kro` | Compose Kubernetes (and ACK) resources into higher-level custom APIs with `ResourceGraphDefinition`s | The capability role only; kro needs no AWS permissions |

## Cost

Capabilities are billed for every hour each one is active on a cluster, and
some Kubernetes resources they manage (Argo CD Applications, ACK resources,
kro instances) are billed hourly as well; see the
[Amazon EKS pricing page](https://aws.amazon.com/eks/pricing/). A capability
enabled for two regions is two capabilities. This is why every type ships
disabled and why `regions` lets you enable one on a subset of clusters.

The Argo CD UI can additionally be made private through an
`eks-capabilities` interface VPC endpoint (`vpce_ids`), which bills per
AZ-hour like any PrivateLink endpoint. The GCO-managed CodeCommit repository
behind the default GitOps hand-off is within
[CodeCommit's free tier](https://aws.amazon.com/codecommit/pricing/) for the
first five active users per account per month.

## Prerequisites

- **Argo CD needs IAM Identity Center.** The hosted Argo CD authenticates
  only through Identity Center; there are no local users. You need an
  Identity Center instance ARN and at least one user or group id from its
  identity store, both in `eks_capabilities.argocd`; synthesis fails
  without them. [`gco stacks capabilities argocd bootstrap-identity`](#identity-center-bootstrap)
  discovers (or, in an account without one, creates) the instance, creates
  the group and writes both into `cdk.json`; by hand it is
  `aws sso-admin list-instances` plus `aws identitystore list-users` /
  `list-groups`.
- **Regions.** Capabilities are available in the commercial AWS Regions
  where EKS is available. `regions` lets you exclude a Region.
- **A Git repository is not one.** The default GitOps hand-off
  (`gitops.source: codecommit`) creates a CodeCommit repository per cluster
  and grants the capability role read on it; nothing to host, no credential
  to store. With `source: git`, public repositories need nothing and private
  ones need credentials in Secrets Manager (see
  [Repository access](#repository-access)).
- **kubectl access** is not required for anything here. `gco stacks
  capabilities` reads the EKS API (and, for `gitops push`, the CodeCommit
  API) only, and the Argo CD UI is reached through its hosted URL.

## Configuration

The block lives in `cdk.json` under `context.eks_capabilities`. Every knob
has a default; only what you set changes. The full schema with defaults:

```json
"eks_capabilities": {
  "argocd": {
    "enabled": false,
    "regions": [],
    "idc_instance_arn": "",
    "idc_region": "",
    "rbac_role_mappings": [],
    "vpce_ids": [],
    "repo_credentials_secret_arns": [],
    "repo_credentials_kms_key_arns": [],
    "gitops": {
      "enabled": false,
      "source": "codecommit",
      "repo_url": "",
      "revision": "HEAD",
      "path": "",
      "destination_namespaces": ["gco-jobs", "gco-inference"],
      "sync_policy": "manual",
      "codecommit": {
        "removal_policy": "destroy"
      }
    }
  },
  "ack": {
    "enabled": false,
    "regions": [],
    "disabled_services": [],
    "enable_cross_namespace": false,
    "assume_role_arns": []
  },
  "kro": {
    "enabled": false,
    "regions": []
  }
}
```

`regions: []` means every regional deployment region; a non-empty list
selects a subset and must name regions from
`deployment_regions.regional`. Unknown keys, wrong types and inconsistent
combinations fail at synthesis with the offending path in the message.

### Argo CD

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `false` | Attach the Argo CD capability to the selected clusters |
| `regions` | `[]` | Regional deployment regions to attach it to (empty = all) |
| `idc_instance_arn` | `""` | IAM Identity Center instance ARN (`arn:aws:sso:::instance/ssoins-…`). **Required when enabled** |
| `idc_region` | `""` | Region of the Identity Center instance when it is not the cluster's region |
| `rbac_role_mappings` | `[]` | `[{"role": "ADMIN"\|"EDITOR"\|"VIEWER", "identities": [{"id": "<identity-store id>", "type": "SSO_USER"\|"SSO_GROUP"}]}]`. **At least one entry when enabled** — an instance nobody can sign in to is a billed no-op |
| `vpce_ids` | `[]` | Interface VPC endpoint ids for `com.amazonaws.<region>.eks-capabilities`; makes the UI/API private to the VPC. Empty keeps the public endpoint |
| `repo_credentials_secret_arns` | `[]` | Secrets Manager secret ARNs the capability role may read (private Git repositories with `gitops.source: git`). An ARN may end in `*` to cover the random suffix |
| `repo_credentials_kms_key_arns` | `[]` | Customer-managed KMS key ARNs those secrets use; grants `kms:Decrypt` via Secrets Manager only. Needs `repo_credentials_secret_arns`; not needed for the AWS-managed `aws/secretsmanager` key |
| `gitops` | see below | The [GitOps hand-off](#the-gitops-hand-off) |

A minimal working block — the Identity Center values are what
[`argocd bootstrap-identity --write-cdk-json`](#identity-center-bootstrap)
writes:

```json
"eks_capabilities": {
  "argocd": {
    "enabled": true,
    "idc_instance_arn": "arn:aws:sso:::instance/ssoins-1234567890abcdef",
    "rbac_role_mappings": [
      {"role": "ADMIN", "identities": [{"id": "94482468-1041-70b0-…", "type": "SSO_USER"}]},
      {"role": "VIEWER", "identities": [{"id": "d4d82468-b071-70e8-…", "type": "SSO_GROUP"}]}
    ],
    "gitops": {"enabled": true}
  }
}
```

With `gitops.enabled: true` and nothing else, each selected cluster gets its
own CodeCommit repository and a root Application reading it; see
[The GitOps hand-off](#the-gitops-hand-off).

### ACK

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `false` | Attach the ACK capability |
| `regions` | `[]` | Regional deployment regions to attach it to |
| `disabled_services` | `[]` | ACK service controllers to leave out (ACK's `disabledServices`) |
| `enable_cross_namespace` | `false` | ACK's cross-namespace resource references |
| `assume_role_arns` | `[]` | IAM role ARNs the capability role may `sts:AssumeRole` — the per-service roles ACK's [IAM Role Selectors](https://aws-controllers-k8s.github.io/docs/guides/iam-role-selector) point at. Empty grants the capability role no AWS permissions at all, so ACK can create nothing until you add roles here |

### kro

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `false` | Attach the kro capability |
| `regions` | `[]` | Regional deployment regions to attach it to |

kro composes existing Kubernetes and ACK resources; its role needs no AWS
permissions.

### Run-scoped overrides

For one deploy without editing `cdk.json` — the same idea as
[`--enable`](CUSTOMIZATION.md#run-scoped-enablement-overrides) — pass a JSON
object as the `eks_capabilities_overrides` CDK context. It is deep-merged
over the `cdk.json` block, so partial objects work and the merged result is
validated exactly like the file:

```bash
cdk deploy gco-us-east-1 --context 'eks_capabilities_overrides={"kro": {"enabled": true}}'
```

The [live-validation harness](LIVE_RELEASE_VALIDATION.md) uses this to prove
capabilities from a clean checkout (`--eks-capabilities`). As with `--enable`,
the next plain deploy without the context removes what the override created.

## Identity Center bootstrap

The hosted Argo CD signs users in only through IAM Identity Center, so the
capability cannot be enabled without an instance ARN and a mapped user or
group. Rather than assembling those by hand from `aws sso-admin` and
`aws identitystore`, run:

```bash
gco stacks capabilities argocd bootstrap-identity                                  # discover, print the fragment
gco stacks capabilities argocd bootstrap-identity --user alice --write-cdk-json    # discover, map, write cdk.json
gco stacks capabilities argocd bootstrap-identity --create-account-instance -y     # account with no Identity Center yet
gco stacks capabilities argocd bootstrap-identity --identity SSO_GROUP:d4d8… --write-cdk-json
```

What it does, in order:

1. **Finds the instance** visible from the account — first in `--idc-region`
   (default: the cluster's region), then in every Region Identity Center
   serves, since an account has at most one instance and it may live
   anywhere. `--instance-arn` pins a specific one.
2. **Creates an account instance when there is none** and you pass
   `--create-account-instance`. An
   [account instance](https://docs.aws.amazon.com/singlesignon/latest/userguide/account-instances-identity-center.html)
   is the Identity Center flavour a standalone account or an Organizations
   member account can create for itself with `sso-admin:CreateInstance`; it
   is named `<project>-identity-center`, tagged `gco:project`, and is **not**
   a GCO stack resource — it outlives every deploy and is deleted only by
   hand (`aws sso-admin delete-instance`). A management account cannot
   create one this way; it enables the organization instance from the
   console and then runs the command without the flag.
3. **Ensures one group** (`<project>-argocd-admins` by default, `--group`
   to rename) in the instance's identity store and maps it to `--role`
   (`ADMIN` by default). `--user NAME` adds existing users to it, repeatably.
   Against an organization instance owned by another account (where the
   member account cannot create groups) use `--identity SSO_USER:<id>` /
   `SSO_GROUP:<id>` to map identities that already exist instead.
4. **Prints the `cdk.json` fragment**, or with `--write-cdk-json` writes
   `idc_instance_arn`, `idc_region` and the role mapping through the
   managed-config engine (validated, atomic, audited, idempotent — a second
   run reports no-ops). It never flips `enabled`; that stays your decision.

The one thing the command cannot do is set a password: Identity Center
requires the person to do that through the console (or the invitation email
for a new user). A user who wants to open the UI signs in once there, and
from then on the group membership decides what Argo CD lets them do.

## What the regional stack creates

### IAM

One **capability role** per enabled type, trusted by
`capabilities.eks.amazonaws.com` for `sts:AssumeRole` and `sts:TagSession`
(the documented trust policy). Roles carry only what you configured:

| Role | Grants |
|------|--------|
| `EksCapabilityArgoCdRole` | `codecommit:GitPull` on exactly the cluster's GCO-managed GitOps repository (when the hand-off uses `source: codecommit`); `secretsmanager:GetSecretValue` / `DescribeSecret` on exactly `repo_credentials_secret_arns`; `kms:Decrypt` on exactly `repo_credentials_kms_key_arns`, conditioned on `kms:ViaService = secretsmanager.<region>.amazonaws.com` |
| `EksCapabilityAckRole` | `sts:AssumeRole` on exactly `assume_role_arns` |
| `EksCapabilityKroRole` | nothing |

No role gets a wildcard the operator did not spell out; cdk-nag's `IAM5`
finding is acknowledged only for ARN patterns you wrote into `cdk.json`.

The `AWS::EKS::Capability` resources are named `<project_name>-<type>`
(`gco-argocd`, `gco-ack`, `gco-kro`) with `DeletePropagationPolicy: RETAIN`,
the only value the service supports: removing a capability leaves the
objects it created in the cluster. Argo CD runs in the `argocd` namespace.
The stack exports `EksCapability<Type>Arn`, `EksCapability<Type>RoleArn` and,
for Argo CD, `EksCapabilityArgoCdServerUrl`.

When the GitOps hand-off uses `source: codecommit`, the stack also creates
one **CodeCommit repository** per selected cluster, `<cluster>-gitops`
(`gco-us-east-1-gitops` for the default project name), seeded on its `main`
branch from [`examples/gitops/codecommit-seed`](../examples/gitops/README.md)
so the root Application is `Synced`/`Healthy` before the first push. Its
removal policy follows `gitops.codecommit.removal_policy`: `destroy` (default)
deletes it with the stack, `retain` keeps the history. The stack exports
`EksCapabilityArgoCdGitOpsRepositoryName` and
`EksCapabilityArgoCdGitOpsRepositoryCloneUrlHttp`.

The applier's convergence pipeline depends on every capability, so the
Argo CD manifests below are applied only after EKS has installed the
`argoproj.io` custom resource definitions.

### Kubernetes objects

Two manifests in the base pass of the kubectl-applier
([`lambda/kubectl-applier-simple/manifests/`](../lambda/kubectl-applier-simple/manifests/README.md))
are gated on tokens the regional stack emits only when the matching feature
is on for that region:

| Manifest | Gate | Objects |
|----------|------|---------|
| `07-argocd-cluster-access.yaml` | Argo CD enabled | `argocd` Namespace; the `local-cluster` Secret registering the hosting cluster **by EKS cluster ARN** (the hosted capability identifies clusters by ARN, not by `kubernetes.default.svc`); ClusterRole/Binding `gco-argocd-read-all` (get/list/watch everything — Argo CD's cluster cache needs cluster-wide read for discovery, drift and health); Role/RoleBinding `gco-argocd-deploy` in `gco-jobs` and `gco-inference` — an explicit allow-list of the namespaced workload kinds a tenant repository deploys (core, apps, batch, autoscaling, PodDisruptionBudget, Ingress, Gateway API routes, and the namespaced kinds of the operators GCO installs: Kueue, Kubeflow Trainer + JobSet, KubeRay, KEDA, Volcano, Prometheus operator, cert-manager), with no wildcard and no `ResourceQuota`, `LimitRange`, `NetworkPolicy`, `Role` or `RoleBinding`. All bindings name the Kubernetes group EKS maps the capability role's access entry to, `eks-access-entry:<role ARN>` |
| `08-argocd-gitops.yaml` | GitOps hand-off enabled | `AppProject` `gco-tenants` and `Application` `gco-gitops-root` (see below) |

The platform namespace `gco-system` is deliberately not writable by Argo CD.
Disabling a feature prunes exactly these objects on the next deploy, except
the `argocd` Namespace, which holds every Application an operator may have
created by hand.

## The GitOps hand-off

`eks_capabilities.argocd.gitops` points each selected cluster's Argo CD at a
repository path:

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `false` | Create the project and root Application (requires `argocd.enabled`) |
| `source` | `"codecommit"` | Where the manifests come from: `codecommit` — a GCO-managed CodeCommit repository per cluster; `git` — the repository in `repo_url` |
| `repo_url` | `""` | Git repository URL (`https://`, `ssh://` or `git@…`). **Required with `source: git`**, rejected with `source: codecommit` (GCO names that repository itself) |
| `revision` | `"HEAD"` | Branch, tag or commit Argo CD tracks (`HEAD` follows the CodeCommit repository's `main`) |
| `path` | `""` | Repository path to sync; empty means the source's default — `.` for `codecommit`, `clusters/{region}` for `git`. `{region}` and `{cluster_name}` are substituted per cluster, so one shared repository can hold a per-cluster overlay directory |
| `destination_namespaces` | `["gco-jobs", "gco-inference"]` | Namespaces the project may deploy into; only these two are allowed (they are the namespaces GCO grants Argo CD write RBAC in). The first is the root Application's default namespace |
| `sync_policy` | `"manual"` | `manual` or `automated` |
| `codecommit.removal_policy` | `"destroy"` | What `cdk destroy` does with the GCO-managed repository: `destroy` deletes it (your checkout is the source of truth), `retain` keeps it |

Per cluster, GCO creates:

- **`AppProject` `gco-tenants`** — the fence. Sources: exactly the one
  repository URL (the cluster's CodeCommit clone URL, or `repo_url`).
  Destinations: this cluster (by ARN) in each `destination_namespaces`
  entry. `clusterResourceWhitelist: []`, so nothing cluster-scoped
  (Namespaces, CRDs, ClusterRoles, NodePools) can be synced.
  `namespaceResourceBlacklist` refuses `ResourceQuota`, `LimitRange`,
  `NetworkPolicy`, `Role` and `RoleBinding` — the kinds GCO uses as
  guardrails inside the tenant namespaces. The Kubernetes Role in
  `07-argocd-cluster-access.yaml` leaves the same five kinds out (a unit test
  pins the two lists to each other), so the Argo CD fence and the apiserver
  agree on what Git may never touch.
- **`Application` `gco-gitops-root`** — project `gco-tenants`, source that
  repository @ `revision` / `path`, destination this cluster and the first
  tenant namespace, and the configured sync policy. It has **no
  resources-finalizer**: deleting the Application (or disabling the
  hand-off) detaches the workloads from Git, it never deletes them.

A single-directory fixture that syncs cleanly through this fence ships in
[`examples/gitops/tenant-smoke`](../examples/gitops/README.md).

### Where the manifests come from

`gitops.source` picks one of two repository models:

| | `codecommit` (default) | `git` |
|---|---|---|
| Repository | One [CodeCommit](https://docs.aws.amazon.com/codecommit/latest/userguide/welcome.html) repository per selected cluster, `<cluster>-gitops`, created by the regional stack and seeded with a README | Any Git repository you host (`repo_url`), shared across clusters |
| How Argo CD reads it | The hosted Argo CD's [direct CodeCommit integration](https://docs.aws.amazon.com/eks/latest/userguide/argocd-configure-repositories.html): the capability role's `codecommit:GitPull` grant on exactly that repository. No repository Secret, no credential, no public endpoint of yours | Anonymous HTTPS for public repositories; Secrets Manager credentials or CodeConnections for private ones ([Repository access](#repository-access)) |
| Default `path` | `.` — the repository root | `clusters/{region}` — a per-cluster overlay directory in the shared repository |
| How manifests get there | [`gco stacks capabilities gitops push`](#pushing-to-the-codecommit-repository) mirrors a local directory into the branch through the CodeCommit API | Your own Git workflow |
| Lifecycle | Follows the stack: `removal_policy: destroy` (default) deletes the repository with `cdk destroy`, `retain` keeps it | Yours |

`codecommit` is the batteries-included path: `gitops.enabled: true` is the
whole configuration, and the operator's local directory (typically a
directory in the same repository as `cdk.json`) is the source of truth — the
CodeCommit repository only ever holds what was last pushed, per cluster,
which is why the stack may delete it by default. It is also what the
[live-validation harness](LIVE_RELEASE_VALIDATION.md) exercises, so a
release proves the exact path a fresh deployment takes.

`git` is for teams that already run GitOps from a repository with its own
review flow: point `repo_url` at it, keep one overlay directory per cluster
(or set `path` to a single directory to deploy the same manifests
everywhere), and manage access as described below.

### Pushing to the CodeCommit repository

```bash
gco stacks capabilities gitops push --path ./manifests                # first deployment region
gco stacks capabilities gitops push --path ./manifests -A             # every deployment region
gco stacks capabilities gitops push --path ./manifests -A --dry-run   # plan only
gco stacks capabilities gitops push --path examples/gitops/tenant-smoke -r us-west-2 -y
```

After the push, the branch (`main` unless `--branch`) holds exactly the
directory's files: new and changed files are written, files that
disappeared from the directory are deleted, unchanged files are left alone,
and nothing is committed when nothing changed. When the directory is inside
a Git work tree the file list is `git ls-files` (tracked plus
untracked-but-not-ignored), so `.gitignore` applies; outside one every
regular file is taken. Symlinks, files over 6 MiB and an empty directory are
refused before anything is written, and trees larger than one CodeCommit
commit can carry (100 files or 15 MiB) land as a numbered series of commits
(`… (part 1/3)`). Each region receives its own commit; `--output json`
returns the per-region plan and commit ids.

The push is the CodeCommit API (`GetBranch`, `GetDifferences`,
`CreateCommit`) called with your AWS credentials — there is no Git remote
helper, `git-remote-codecommit` or HTTPS credential helper to install, and
the caller needs only those three actions on the repository. Argo CD sees the
new commit within its polling interval (or immediately on the next refresh)
and applies it under `sync_policy.automated`, or shows it as `OutOfSync`
for a human to press Sync under `manual`. The
[`gitops_push`](../gco_mcp/tools/README.md#stackspy) MCP tool performs the
same push for agents (it needs `GCO_ENABLE_INFRASTRUCTURE_DEPLOY`, like every
tool that changes infrastructure).

### What the tenant repository may contain

Anything the fence admits: namespaced workloads (Jobs, Deployments,
Services, ConfigMaps, Secrets, KEDA `ScaledJob`s, Kubeflow `TrainJob`s,
`RayCluster`s …) whose namespace is one of `destination_namespaces` or is
omitted (the Application's destination namespace is applied). The
[job admission policy](CUSTOMIZATION.md#security-policy-configuration)
enforced by GCO's manifest API does **not** apply to objects Argo CD writes
directly — the fence, the namespace `ResourceQuota`/`LimitRange`, and the
Argo CD RBAC are the controls on this path. Keep the tenant repository's
write access as tight as you would keep `kubectl` access to `gco-jobs`.

The Kubernetes side of that RBAC is an allow-list, not namespace-admin: a
kind outside it (say a CRD from an operator you installed yourself) fails
the sync with a `Forbidden` from the apiserver rather than being granted
silently. To admit one, bind an additional `Role` naming that kind to the
same subject — the Kubernetes group `eks-access-entry:<capability role
ARN>` — in the tenant namespace; the capability role's ARN is in
`gco stacks capabilities status` and the `EksCapabilityArgoCdRoleArn` stack
output.

Use the root Application as an "app of apps" if you want more structure:
commit further `Application` objects under `path`, each in project
`gco-tenants` — Argo CD reconciles them into the `argocd` namespace only if
the project allows that destination, which `gco-tenants` does not, so nested
Applications must be created by an operator with cluster access rather than
synced from the tenant repository. This is intentional: tenants cannot
widen their own fence.

### Sync policy

- `manual` (default) — Argo CD computes drift and shows it in the UI;
  a human presses Sync. Safe first step for a new repository.
- `automated` — Argo CD syncs on every detected change with `selfHeal: true`
  (live drift is reverted to Git) and `prune: false` (objects removed from
  Git are **not** deleted from the cluster). Turning prune on is a decision
  to make inside the repository's own Application objects, not a GCO knob.

### Repository access

With `source: codecommit` there is nothing to configure: the capability role
pulls the GCO-managed repository with its own IAM identity, and whoever runs
`gitops push` needs `codecommit:GetBranch`, `GetDifferences` and
`CreateCommit` on it (the stack's `EksCapabilityArgoCdGitOpsRepositoryName`
output names it).

With `source: git`, public repositories need nothing. For private ones,
store the credential in Secrets Manager (a `{"username": …, "password": …}`
token or an SSH key), list the secret ARN in `repo_credentials_secret_arns`
(and the key in `repo_credentials_kms_key_arns` when it is customer-managed),
deploy, then create the Argo CD repository Secret in the `argocd` namespace
referencing that ARN as the
[EKS Argo CD documentation](https://docs.aws.amazon.com/eks/latest/userguide/integration-secrets-manager.html)
describes (`argocd.argoproj.io/secret-type: repository` with a `secretArn`
field). [CodeConnections](https://docs.aws.amazon.com/eks/latest/userguide/argocd-configure-repositories.html)
(GitHub, GitLab, Bitbucket with managed authentication) is also supported by
the hosted Argo CD; grant `codeconnections:UseConnection` on the connection
in the role by hand if you use it — GCO's role carries only the grants
listed under [IAM](#iam). Note that the hosted Argo CD runs outside your VPC,
so a Git server reachable only from inside it cannot be a source; use
CodeCommit or a CodeConnections host instead.

## Argo CD UI

The hosted Argo CD serves its UI at a URL EKS publishes on the capability
(`DescribeCapability` → `configuration.argoCd.serverUrl`; also the
`EksCapabilityArgoCdServerUrl` stack output). Sign-in is IAM Identity Center
with the users and groups in `rbac_role_mappings` (the group
[`bootstrap-identity`](#identity-center-bootstrap) creates, for instance);
there is nothing to port-forward and no password to fetch. With `vpce_ids`
configured the URL is private to the VPC, so open it from a host inside it.

```bash
gco stacks capabilities argocd open                  # resolve the URL and open your browser
gco stacks capabilities argocd open --print-url      # just print it (also --output json)
gco stacks capabilities argocd screenshot -o images/argocd-ui.png
```

`screenshot` captures a full-page PNG of the Applications view with
Playwright's Chromium (`pip install 'gco[diagrams]'` and
`playwright install chromium` once). The browser profile persists per region
under `~/.gco/argocd-browser/<region>` (`$GCO_ARGOCD_BROWSER_PROFILE_DIR`
overrides the root), so the first run opens a window for you to sign in with
Identity Center and later runs can pass `--headless`. The
[`argocd_ui_url`](../gco_mcp/tools/README.md#stackspy) MCP tool exposes the
URL to agents. See [`gco stacks capabilities`](CLI.md#gco-stacks-capabilities)
for every option.

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
issues). The Argo CD row carries the server URL and the rendered GitOps
hand-off; capabilities on the cluster that GCO did not create appear under
`unmanaged`. The command exits nonzero on drift, so it works as a check. The
[`eks_capabilities_status`](../gco_mcp/tools/README.md#stackspy) MCP tool
returns the same document.

### Disabling a capability

Set `enabled: false` (or remove the region from `regions`) and deploy. The
stack deletes the `AWS::EKS::Capability`; with `RETAIN` propagation the
objects the tool created stay in the cluster — Argo CD's Applications keep
their last synced state and stop reconciling, ACK resources keep existing in
AWS and stop reconciling, kro instances stay as they are. The applier prunes
GCO's own wiring (the RBAC, the cluster Secret, the project and root
Application) and leaves the `argocd` Namespace. Delete tenant objects
yourself first if you want them gone.

## Multi-cluster note (hub and spoke)

GCO attaches one Argo CD per selected cluster and registers only that
cluster as a target: each Region's tenant workloads are reconciled by its
own hosted instance from its own `path` (hence the `{region}` placeholder).
This keeps the failure and permission domain per Region, matching how the
rest of GCO treats Regions as independent.

A hub-and-spoke layout — one Argo CD reconciling several clusters — is
possible with the hosted capability (register the remote cluster by ARN,
create an EKS access entry for the hub's capability role on it, and grant
RBAC there), but GCO does not automate it: the remote access entries, their
RBAC and the cross-Region project destinations would have to be authored by
hand, and a hub Region's outage would stall deployments everywhere. If you
build one, keep `regions` down to the hub and register spokes from the hub's
`argocd` namespace as the
[AWS documentation](https://docs.aws.amazon.com/eks/latest/userguide/argocd-register-clusters.html)
describes.

## Verification

- Unit tests pin the config schema, the synthesized roles, capabilities and
  outputs, the applier tokens and prune inventories, and the CLI
  (`tests/test_eks_capabilities.py`, `tests/test_eks_capabilities_cli.py`,
  the Argo CD cases in `tests/test_kubectl_applier.py`).
- The kind CI job (`integration:kind:cluster-e2e`) installs the pinned
  upstream `argoproj.io` CRDs, applies both manifests rendered with the
  regional stack's own token renderer, proves the RBAC fence by
  impersonating the access-entry group, and runs the applier's disable-path
  prune ([`.github/CI.md`](../.github/CI.md)).
- The [live-validation harness](LIVE_RELEASE_VALIDATION.md) proves the whole
  path self-contained, with nothing prepared in the account beforehand:
  `--eks-capabilities argocd` makes its `argocd-identity` action create a
  run-scoped Identity Center account instance and group (or reuse an instance
  you name), the deploy enables the capability and the CodeCommit-backed
  hand-off, the `eks-capabilities` action requires each capability `ACTIVE`,
  checks the cluster wiring through the tunnelled kubectl session, pushes
  `examples/gitops/tenant-smoke` into each cluster's repository with the same
  code as `gitops push`, and waits for the root Application to report that
  exact commit `Synced` and `Healthy`. Teardown deletes the group and the
  instance it created. The unit tests in
  `tests/test_gitops_push.py` and `tests/test_argocd_identity.py` pin the
  push planner and the Identity Center bootstrap against stubbed clients.

## Limitations

- **One capability of each type per cluster** is an EKS limit; GCO's
  deterministic names (`<project>-<type>`) make a second GCO deployment in
  the same account collide only if it targets the same cluster, which it
  never does.
- **Delete propagation is `RETAIN` only.** Nothing GCO does can make
  removing a capability delete what the tool created.
- **No local Argo CD users.** Identity Center is the only sign-in.
  `argocd bootstrap-identity` can create an account instance where there is
  none, but a management account of an Organization enables its
  organization instance from the console, and nothing here can set a user's
  password — the person does that once in the Identity Center console.
- **`gitops push` is a mirror, not a merge.** The branch ends up equal to the
  local directory; two people pushing different directories to the same
  cluster overwrite each other. Keep the directory under version control of
  your own (it usually lives next to `cdk.json`) and treat the CodeCommit
  repository as a per-cluster deployment target.
- **The GitOps fence is namespace-scoped.** Tenant repositories cannot ship
  cluster-scoped objects, CRDs or NodePools through the hand-off by design.
  Platform changes go through `cdk.json` and `gco stacks deploy`.
- **The hand-off bypasses the GCO manifest API's admission policy** (see
  [What the tenant repository may contain](#what-the-tenant-repository-may-contain)).
- **`gco stacks capabilities status` reads the AWS API only.** Argo CD sync
  and health state are visible in the hosted UI and checked by the live
  harness, not by the CLI.
- **Commercial partitions only**, following EKS Capabilities' own
  availability.
