# Crossplane

GCO can run [Crossplane](https://docs.crossplane.io/) v2 in each regional
cluster, together with the [Crossview](https://github.com/crossplane-contrib/crossview)
dashboard, so platform teams can publish their own Kubernetes APIs that
compose tenant workloads. Both are installed from their upstream Helm charts
by the helm installer, **off by default**, behind one `cdk.json` toggle.
Crossplane may compose the tenant workload kinds in `gco-jobs` and
`gco-inference` only, and the dashboard is reached through a port-forward
over the private EKS API endpoint.

## Table of Contents

- [Overview](#overview)
- [Enabling Crossplane](#enabling-crossplane)
- [What GCO installs](#what-gco-installs)
- [Composing tenant workloads](#composing-tenant-workloads)
  - [An example API](#an-example-api)
  - [What a Composition may create](#what-a-composition-may-create)
- [Providers and AWS resources](#providers-and-aws-resources)
- [The Crossview dashboard](#the-crossview-dashboard)
- [Operating Crossplane](#operating-crossplane)
  - [Status](#status)
  - [Disabling](#disabling)
- [Verification](#verification)
- [Limitations](#limitations)

## Overview

Crossplane v2 turns a `CompositeResourceDefinition` (XRD) into a new API and
a `Composition` into what an instance of it — a composite resource, or XR —
becomes. XRs are namespaced by default and compose into their own namespace,
which is what lets GCO fence them: an XR in `gco-jobs` may create Jobs,
Deployments and the other tenant kinds in `gco-jobs`, and an XR anywhere else
cannot create them at all. GCO ships the composition function the Crossplane
docs use and no provider packages: AWS resources come from the
[ACK EKS Capability](EKS_CAPABILITIES.md) instead.

## Enabling Crossplane

```json
"helm": {
  "crossplane": {"enabled": true}
}
```

Then `gco stacks deploy` (or `deploy-all`); `--enable crossplane` does the
same for one deploy without editing `cdk.json` (see
[run-scoped enablement overrides](CUSTOMIZATION.md#run-scoped-enablement-overrides)).
The one toggle installs both charts; an absent block means off.

## What GCO installs

- **The `crossplane` chart** (pinned in
  [`lambda/helm-installer/charts.yaml`](../lambda/helm-installer/charts.yaml))
  as release `crossplane` in namespace `crossplane-system`: one replica of
  Crossplane and of its RBAC manager, bounded requests and limits, and pods
  annotated so Karpenter does not disrupt them mid-reconcile.
- **The `crossview` chart** as release `crossview` in the same namespace,
  installed right after Crossplane: authentication mode `none`, no database,
  `rbac.create: false` (GCO binds its permissions instead), a `crossview`
  ServiceAccount, and a CORS origin of `http://localhost:3001`, the port
  `gco crossplane open` binds.
- **[`post-helm-crossplane.yaml`](../lambda/kubectl-applier-simple/manifests/README.md)**,
  applied after the charts:
  - `Function` `crossplane-contrib-function-go-templating` — the
    [templated-YAML function](https://github.com/crossplane-contrib/function-go-templating),
    pinned by tag, which Crossplane pulls from `xpkg.crossplane.io` and runs
    in `crossplane-system`;
  - ClusterRole `gco-crossplane-read`, aggregated into Crossplane's own role
    through `rbac.crossplane.io/aggregate-to-crossplane`: get/list/watch on
    the tenant workload kinds cluster-wide, so Crossplane can watch what it
    composes;
  - Role/RoleBinding `gco-crossplane-compose` in `gco-jobs` and
    `gco-inference`, bound to the `crossplane` ServiceAccount: the apply and
    prune verbs on the same kinds, only there;
  - the Crossview bindings: Crossplane's `crossplane-view` ClusterRole (every
    Crossplane type and the per-XRD view roles, Secrets excluded) and
    `gco-crossview-read` for the CRDs it discovers and the composed tenant
    kinds it draws. Read-only throughout.

Crossplane's upstream ClusterRole already covers Deployments, Services,
ServiceAccounts, ConfigMaps and Secrets cluster-wide for its own package
runtime; that grant is the chart's, and it is what runs functions and
providers, not what XRs compose.

## Composing tenant workloads

### An example API

[`examples/crossplane-batch-api.yaml`](../examples/README.md) defines a
namespaced `BatchJob` API in `examples.gco.io/v1alpha1` (XRD
`batchjobs.examples.gco.io`) and the Composition `batchjobs-go-templating`,
which renders one `batch/v1` Job per XR with the go-templating function.
[`examples/crossplane-batch-job.yaml`](../examples/README.md) is one XR in
`gco-jobs`:

```bash
kubectl apply -f examples/crossplane-batch-api.yaml
kubectl get xrd batchjobs.examples.gco.io                    # ESTABLISHED True
kubectl apply -f examples/crossplane-batch-job.yaml
kubectl get batchjob.examples.gco.io -n gco-jobs             # READY True once the Job completes
kubectl logs job/gco-crossplane-hello -n gco-jobs
kubectl delete -f examples/crossplane-batch-job.yaml         # deletes the composed Job too
```

The composed Job is named after the XR and carries no
`ttlSecondsAfterFinished`: Crossplane keeps composed resources in line with
their XR, so a Job the TTL controller removed would simply be composed again.
The XR's `message` reaches the container as an environment variable and the
XRD limits it to plain characters, so an XR cannot inject into the rendered
YAML or the command. XRDs and Compositions are cluster-scoped, so defining an
API is a cluster-administrator action; grant tenants the XR kind with
ordinary RBAC.

### What a Composition may create

The tenant allow-list — the same list the Argo CD and kro grants use (a unit
test pins the three to each other): core objects (ConfigMaps, Secrets,
ServiceAccounts, Services, PersistentVolumeClaims, Pods), Deployments,
StatefulSets and DaemonSets, Jobs and CronJobs, HorizontalPodAutoscalers,
PodDisruptionBudgets, Ingresses and Gateway API routes, and the namespaced
kinds of the operators GCO installs. `ResourceQuota`, `LimitRange`,
`NetworkPolicy`, `Role` and `RoleBinding` are absent on purpose, so an XR can
never widen its namespace's ceiling, open the network posture or grant
permissions, and nothing is writable in `gco-system`. An XR there — or in any
namespace but the two — reports the apiserver's `forbidden` in its
conditions and composes nothing.

To compose another kind (an ACK resource, for instance), bind an extra `Role`
naming it to the `crossplane` ServiceAccount of `crossplane-system` in the
tenant namespace.

Objects Crossplane composes do not pass through the GCO manifest API, so its
[job admission policy](CUSTOMIZATION.md#security-policy-configuration) does
not apply to them; the namespace `ResourceQuota`/`LimitRange` and the RBAC
above are the controls on this path.

## Providers and AWS resources

No provider package ships. A Crossplane AWS provider would need its own IAM
identity and permissions and a much wider cluster footprint than composing
tenant workloads does. For AWS resources, enable the
[ACK EKS Capability](EKS_CAPABILITIES.md): ACK resources are namespaced
Kubernetes objects, so a Composition can compose them once the ACK kinds are
bound to Crossplane in the tenant namespace. Installing a provider yourself
(`kubectl apply` a `Provider`) works as upstream documents; its IAM and RBAC
are then yours to configure.

## The Crossview dashboard

```bash
gco crossplane open --via-ssm auto -y     # http://localhost:3001
```

`open` port-forwards `svc/crossview-service` over the private EKS API
endpoint, through an SSM-managed instance with `--via-ssm` (an instance id,
or `auto` for a self-terminating ephemeral bastion). The dashboard shows the
XRDs, Compositions, composite resources, functions and providers, their
health, and the resource graph from an XR to what it composed. It runs
without a login: the port-forward is the access control, and its RBAC is
read-only, so nothing reached through it can change the cluster.

`gco crossplane screenshot` captures the landing page as a PNG; see
[the CLI reference](CLI.md#crossplane-commands) for every option.

## Operating Crossplane

### Status

```bash
gco crossplane status [--output json]
```

The command reads `cdk.json`, `charts.yaml` and the shipped post-Helm
manifest only: the toggle, the two chart pins, the composition functions GCO
installs and how to reach the dashboard. The
[`crossplane_status`](../gco_mcp/tools/README.md#crossplanepy) MCP tool
returns the same document. The state of XRs and packages is in their own
`status` (`kubectl get xrd,composition,function`) and in Crossview.

### Disabling

Set `enabled: false` and deploy. Before it uninstalls the charts, the helm
installer deletes every object of Crossplane's own API groups in dependency
order — the namespaced usages first, then the definitions and Compositions,
and the packages in a pass of their own once those are gone — with one retry
that strips finalizers if a delete stalls. Deleting an XRD makes Crossplane delete its composite
resources, and with them what they composed, before the definition goes, all
while Crossplane is still running. Crossview and Crossplane are then
uninstalled, and the applier prunes the Function and the RBAC above.
Crossplane installs its CRDs itself (its init container) rather than through
the chart, so they stay behind. A stack delete runs the same cleanup.

## Verification

- Unit tests pin both chart entries and their posture, the toggle's
  selection and install order, the post-Helm manifest and its prune
  inventory, the installer's cleanup order, and the CLI
  (`tests/test_platform_addon_charts.py`, `tests/test_crossplane_cli.py`,
  `tests/test_cluster_ui.py`, the Crossplane cases in
  `tests/test_kubectl_applier.py`).
- The kind CI job `integration:kind:platform-addons` installs both pinned
  charts with the shipped values, waits for the function to turn Healthy,
  composes the example BatchJob to completion, proves an XR in `gco-system`
  is refused, proves the Crossplane and Crossview fences, captures the
  dashboard and tears down the way the stack does
  ([`.github/CI.md`](../.github/CI.md#the-platform-add-ons-against-the-real-charts)).
- The [example harness](EXAMPLE_VALIDATION.md) runs
  `examples/crossplane-batch-job.yaml` with its companion API on a live
  deployment.

## Limitations

- **No providers ship**, and Crossplane creates no AWS resources on its own;
  use ACK.
- **Compositions compose the tenant allow-list only**, into the XR's own
  namespace, and only in `gco-jobs` and `gco-inference`.
- **The composition path bypasses the manifest API's admission policy** (see
  [What a Composition may create](#what-a-composition-may-create)).
- **Crossview has no login.** It relies on the port-forward and on its
  read-only RBAC.
- **One Crossplane replica.** A restart pauses reconciliation briefly and
  loses nothing: the state lives in the API server.
