# GitOps with Argo CD

GCO can run [Argo CD](https://argo-cd.readthedocs.io/en/stable/) in each
regional cluster to reconcile tenant workloads from Git. It is installed from
the upstream `argo-cd` Helm chart by the helm installer, **off by default**,
fenced to the tenant namespaces `gco-jobs` and `gco-inference`, optionally
handed one repository path per cluster (the GitOps hand-off), and reached
through a port-forward over the private EKS API endpoint.

## Table of Contents

- [Overview](#overview)
- [Enabling Argo CD](#enabling-argo-cd)
- [Configuration](#configuration)
- [The fence](#the-fence)
- [The GitOps hand-off](#the-gitops-hand-off)
  - [What a tenant repository may contain](#what-a-tenant-repository-may-contain)
  - [Sync policy](#sync-policy)
  - [Private repositories](#private-repositories)
- [Scaling the repo server](#scaling-the-repo-server)
- [Opening the UI](#opening-the-ui)
- [Adding Applications by hand](#adding-applications-by-hand)
- [Operating Argo CD](#operating-argo-cd)
  - [Status](#status)
  - [Disabling](#disabling)
- [Verification](#verification)
- [Limitations](#limitations)

## Overview

With `helm.argocd.enabled` on, each regional stack installs:

- **The `argo-cd` chart** (pinned in
  [`lambda/helm-installer/charts.yaml`](../lambda/helm-installer/charts.yaml))
  as release `argocd` in namespace `argocd`, in **namespaced mode**:
  `createClusterRoles: false`, so the controllers get no ClusterRoles, and the
  chart's `in-cluster` cluster Secret registers the hosting cluster for
  `gco-jobs` and `gco-inference` only. `resource.respectRBAC: normal` keeps
  the controller from watching kinds it may not read. Dex, notifications and
  the ApplicationSet controller are off, `exec` and status badges are
  disabled, the chart's NetworkPolicies are not rendered, and the CRDs are
  kept on uninstall. `server.insecure: true` serves plain HTTP inside the
  cluster: the UI has no Service outside the cluster and is only reached
  through `kubectl port-forward`.
- **The tenant RBAC and the fence**
  ([`post-helm-argocd-access.yaml`](../lambda/kubectl-applier-simple/manifests/README.md)):
  Role/RoleBinding `gco-argocd-read` (read everything in the two tenant
  namespaces, for the application controller and the API server) and
  `gco-argocd-deploy` (the tenant workload allow-list, for the controller
  only), plus the `AppProject` `gco-tenants`.
- **The GitOps hand-off**, when `gitops.repo_url` is set
  (`post-helm-argocd-gitops.yaml`): one root `Application`, `gco-gitops-root`,
  pointing Argo CD at a repository path.

GCO's own platform — the services, NodePools, network policies and Helm
charts — stays under CloudFormation and the applier. Argo CD is for tenant
workloads.

## Enabling Argo CD

```json
"helm": {
  "argocd": {"enabled": true}
}
```

Then `gco stacks deploy` (or `deploy-all`). To try it for one deploy without
editing `cdk.json`, pass `--enable argocd`; the next plain deploy removes it
again (see [run-scoped enablement overrides](CUSTOMIZATION.md#run-scoped-enablement-overrides)).

## Configuration

The block lives under `context.helm.argocd`. Every knob has a default:

```json
"argocd": {
  "enabled": false,
  "source_repos": ["*"],
  "gitops": {
    "repo_url": "",
    "revision": "HEAD",
    "path": ".",
    "sync_policy": "manual"
  },
  "repo_server": {
    "replicas": 1,
    "autoscaling": {
      "enabled": false,
      "max_replicas": 5,
      "cpu_target_utilization_percentage": 70
    }
  }
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `false` | Install Argo CD. An absent block also means off |
| `source_repos` | `["*"]` | The `gco-tenants` project's `sourceRepos`: repository URLs or glob patterns Applications may read from. `*` admits any repository; the fence is on destinations and kinds |
| `gitops.repo_url` | `""` | Git repository URL (`https://`, `ssh://` or `git@…`). Setting it adds the root Application; it must be admitted by `source_repos` |
| `gitops.revision` | `"HEAD"` | Branch, tag or commit to track |
| `gitops.path` | `"."` | Repository directory to sync. `{region}` and `{cluster_name}` are substituted per cluster, so one repository can hold one overlay directory per cluster |
| `gitops.sync_policy` | `"manual"` | `manual` or `automated` (see [Sync policy](#sync-policy)) |
| `repo_server.replicas` | `1` | Repo-server replicas (1–50); the autoscaler's floor when it is on |
| `repo_server.autoscaling.enabled` | `false` | Hand the repo-server replica count to a CPU HorizontalPodAutoscaler |
| `repo_server.autoscaling.max_replicas` | `5` | Autoscaler ceiling (1–100, at least `replicas`) |
| `repo_server.autoscaling.cpu_target_utilization_percentage` | `70` | Target average CPU, in percent of the repo-server container's CPU request (1–100) |

Unknown keys, wrong types, a path that leaves the repository (a leading `/`
or `..`), a repository `source_repos` would refuse and inexact integers fail
at synthesis with the offending path in the message. The integers are checked
even while autoscaling is off, so a typo surfaces before someone turns it on.

## The fence

`AppProject` `gco-tenants` is the project the root Application and the
examples use:

- **Sources:** `source_repos`.
- **Destinations:** the hosting cluster (`https://kubernetes.default.svc`) in
  `gco-jobs` and `gco-inference` only. The platform namespace `gco-system` is
  never a destination.
- **`clusterResourceWhitelist: []`:** nothing cluster-scoped (Namespaces,
  CRDs, ClusterRoles, NodePools) can be synced.
- **`namespaceResourceBlacklist`:** `ResourceQuota`, `LimitRange`,
  `NetworkPolicy`, `Role` and `RoleBinding` — the kinds GCO uses as guardrails
  inside the tenant namespaces.

The Kubernetes RBAC says the same thing independently, and it is the layer
that holds whatever project an Application names: the application controller
has no ClusterRole, so outside its own `argocd` namespace it can read only the
two tenant namespaces and write only the allow-listed kinds there, never the
five guardrail kinds (a unit test pins the RBAC list to the project
blacklist). The project repeats the fence in Argo CD's terms, so a violating
sync fails with a clear message instead of a `Forbidden` from the apiserver.

## The GitOps hand-off

Set `gitops.repo_url` and each cluster gets `Application` `gco-gitops-root`:
project `gco-tenants`, source `repo_url` @ `revision` / `path` (placeholders
rendered for that cluster), destination the hosting cluster with `gco-jobs`
as the namespace for manifests that carry none, and the configured sync
policy. It has **no resources finalizer**: deleting it (or unsetting
`repo_url`) detaches the workloads from Git, it never deletes them.

```json
"argocd": {
  "enabled": true,
  "source_repos": ["https://github.com/example-org/*"],
  "gitops": {
    "repo_url": "https://github.com/example-org/gco-tenants.git",
    "revision": "main",
    "path": "clusters/{cluster_name}",
    "sync_policy": "automated"
  }
}
```

A single-directory fixture that syncs through the fence ships in
[`examples/gitops/hello-job`](../examples/gitops/README.md).

### What a tenant repository may contain

Namespaced workloads of the allow-listed kinds whose namespace is `gco-jobs`,
`gco-inference` or omitted: core objects (ConfigMaps, Secrets,
ServiceAccounts, Services, PersistentVolumeClaims, Pods), Deployments,
StatefulSets and DaemonSets, Jobs and CronJobs, HorizontalPodAutoscalers,
PodDisruptionBudgets, Ingresses and Gateway API routes, and the namespaced
kinds of the operators GCO installs (Kueue `LocalQueue`s, Kubeflow Trainer
`TrainJob`s and JobSets, KubeRay, KEDA, Volcano, Prometheus operator
monitors and rules, cert-manager). A kind outside the list fails the sync with
a `Forbidden`; to admit one, bind an extra `Role` naming it to the
`argocd-application-controller` ServiceAccount of the `argocd` namespace in
the tenant namespace.

The [job admission policy](CUSTOMIZATION.md#security-policy-configuration)
enforced by GCO's manifest API does **not** apply to objects Argo CD writes
directly — the fence, the namespace `ResourceQuota`/`LimitRange` and the RBAC
are the controls on this path. Keep the repository's write access as tight as
you would keep `kubectl` access to `gco-jobs`.

Nested Applications ("app of apps") cannot be synced from a tenant
repository: `gco-tenants` does not admit the `argocd` namespace as a
destination, so tenants cannot widen their own fence. An operator creates
further Applications by hand (see [below](#adding-applications-by-hand)).

### Sync policy

- `manual` (default) — Argo CD computes drift and shows it in the UI; a human
  presses Sync. A safe first step for a new repository.
- `automated` — Argo CD syncs every detected change with `selfHeal: true`
  (live drift is reverted to Git) and `prune: false` (objects removed from Git
  are **not** deleted from the cluster). Turning prune on is a decision for
  the repository's own Application objects, not a GCO knob.

### Private repositories

Public repositories need nothing. For a private one, create an Argo CD
[repository Secret](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/#repositories)
in the `argocd` namespace — labelled `argocd.argoproj.io/secret-type:
repository`, with the `url` and a token (`username`/`password`) or an
`sshPrivateKey` — through `kubectl` or the UI's *Settings → Repositories*.
The repo server clones from inside the cluster, so the Git host must be
reachable from the cluster's private subnets.

## Scaling the repo server

The repo server renders every Application's manifests (plain YAML, Kustomize,
Helm) and is the tier that grows with repository size and sync volume; the
application controller opens a new connection to it per request, so extra
replicas share the work. Give it a fixed size with `repo_server.replicas`, or
hand the count to an autoscaler:

```json
"argocd": {
  "enabled": true,
  "repo_server": {
    "replicas": 2,
    "autoscaling": {"enabled": true, "max_replicas": 8, "cpu_target_utilization_percentage": 70}
  }
}
```

With autoscaling on, the stack passes the chart its own HorizontalPodAutoscaler
values: `argocd-repo-server` between `replicas` and `max_replicas`, on the CPU
of the `repo-server` container (a `ContainerResource` metric; memory is left
out because the repo server keeps its rendering memory once it has grown and
would never scale back in), with a damped behavior — at most two pods more a
minute after a minute of sustained load, one pod fewer every two minutes after
five quiet ones. The chart then leaves the Deployment's replica count to the
HPA. Metrics come from the cluster's metrics-server add-on. The target is a
percentage of the container's CPU request (`repoServer.resources` in
`charts.yaml`, `50m` as shipped); raise that request alongside the ceiling if
your renders are heavy.

The other components keep one replica: the application controller is a
StatefulSet that shards by cluster and GCO registers one cluster, and the API
server is reached through a port-forward to a single pod. `gco gitops status`
shows the configured scaling.

## Opening the UI

```bash
gco gitops password --via-ssm auto -y     # the generated admin password
gco gitops open --via-ssm auto -y         # http://localhost:8080, sign in as admin
```

`open` port-forwards `svc/argocd-server` over the private EKS API endpoint,
through an SSM-managed instance with `--via-ssm` (an instance id, or `auto` for
a self-terminating ephemeral bastion), like `gco monitoring open`. Argo CD
writes the `admin` password into `argocd-initial-admin-secret` on first start;
change it (the UI's *User Info → Update Password*) and delete that Secret, as
Argo CD recommends. Single sign-on is not configured: add an OIDC provider or
Dex through the chart values in `charts.yaml` (`configs.cm`) if you need it.

`gco gitops screenshot` logs in to the API and captures the Applications view
as a PNG without a browser sign-in; see [the CLI reference](CLI.md#gitops-commands)
for every option.

## Adding Applications by hand

Operators with cluster access create further Applications in the `argocd`
namespace, in project `gco-tenants` so the fence applies:

```bash
kubectl apply -f examples/argocd-gitops-job.yaml
kubectl get application gco-gitops-hello -n argocd      # Synced / Healthy
kubectl delete -f examples/argocd-gitops-job.yaml       # also deletes the Job
```

[`examples/argocd-gitops-job.yaml`](../examples/README.md) syncs
`examples/gitops/hello-job` into `gco-jobs` and carries the resources
finalizer, so deleting the Application deletes what it synced.

## Operating Argo CD

### Status

```bash
gco gitops status                     # every regional deployment region
gco gitops status -r us-west-2 --output json
```

The command reads `cdk.json` and `charts.yaml` only: the toggle, the chart
pin, the fence, the hand-off with its path rendered per Region, the
repo-server scaling and how to reach the UI. A run-scoped `--enable argocd`
deploy is not visible to it. The
[`gitops_status`](../gco_mcp/tools/README.md#gitopspy) MCP tool returns the
same document. Sync and health state are in the UI.

### Disabling

Set `enabled: false` and deploy. Before it uninstalls the chart, the helm
installer deletes every `argoproj.io` object: Argo CD processes each
Application's resources finalizer while its controller is still running, so an
Application that carries one deletes what it synced, and the root Application,
which carries none, leaves its workloads in place. The chart is then
uninstalled (the CRDs stay), and the applier prunes the RBAC, the project and
the root Application. A stack delete runs the same cleanup.

## Verification

- Unit tests pin the block's schema, the chart values the stack derives (the
  repo-server autoscaler included), the chart entry and its posture, the
  post-Helm manifests and their prune inventories, and the CLI
  (`tests/test_argocd_config.py`, `tests/test_platform_addon_charts.py`,
  `tests/test_gitops_cli.py`, `tests/test_cluster_ui.py`, the Argo CD cases in
  `tests/test_kubectl_applier.py`).
- The kind CI job `integration:kind:platform-addons` installs the pinned
  chart with the shipped values and a repo-server autoscaler, proves the HPA
  owns the replica count, syncs `examples/gitops/hello-job` from the pull
  request's own commit, proves the project and RBAC fences, cascades the
  example Application through the installer's cleanup and captures the UI
  ([`.github/CI.md`](../.github/CI.md#the-platform-add-ons-against-the-real-charts)).
- The [example harness](EXAMPLE_VALIDATION.md) runs
  `examples/argocd-gitops-job.yaml` on a live deployment, pinned to the commit
  under validation.

## Limitations

- **One cluster per Argo CD.** Each Region reconciles its own path (hence the
  `{region}` and `{cluster_name}` placeholders); hub-and-spoke across
  clusters is not automated.
- **The fence is namespace-scoped.** Cluster-scoped objects, CRDs and
  NodePools cannot be synced; platform changes go through `cdk.json` and
  `gco stacks deploy`.
- **The hand-off bypasses the manifest API's admission policy** (see
  [What a tenant repository may contain](#what-a-tenant-repository-may-contain)).
- **Local admin only** until you configure single sign-on.
- **The UI is private.** There is no ingress; `gco gitops open` is the way in.
