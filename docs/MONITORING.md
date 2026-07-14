# Cluster Observability

In-cluster observability installs a self-hosted
[`kube-prometheus-stack`](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack)
(Prometheus + Alertmanager + Grafana + `kube-state-metrics` + `node-exporter` +
the Prometheus Operator) on **every regional EKS cluster**, through the existing
Helm-installer pipeline. It answers "what is happening **inside** this cluster
right now" at Prometheus resolution — per-GPU DCGM series, scheduler queue depth,
KEDA scaler lag, and per-pod GCO service metrics.

Unlike most optional features, cluster observability is **on by default**. A
stock deployment installs it in each region; operators opt out with
`gco monitoring disable` (or `cluster_observability.enabled = false` in
`cdk.json`).

> **Region placeholders.** Examples use `us-east-1` for a regional cluster.
> Substitute your own regions from `deployment_regions.regional` in `cdk.json`.

## Table of Contents

- [Relationship to the CloudWatch monitoring stack](#relationship-to-the-cloudwatch-monitoring-stack)
- [Cost](#cost)
- [What it provisions](#what-it-provisions)
- [Enabling and disabling](#enabling-and-disabling)
- [Accessing Grafana on a private cluster](#accessing-grafana-on-a-private-cluster)
- [Managing Grafana users](#managing-grafana-users)
- [Admin credential rotation](#admin-credential-rotation)
- [Curated dashboards](#curated-dashboards)

## Relationship to the CloudWatch monitoring stack

GCO ships **two** complementary observability surfaces:

- **`gco-monitoring` (CloudWatch)** — a cross-region CloudWatch dashboard, alarm,
  and SNS surface. It answers "is the platform up, are alarms firing, what does
  the fleet look like across regions" using AWS-native metrics (Global
  Accelerator, API Gateway, Lambda, SQS, DynamoDB, Container Insights node/GPU
  aggregates). This stack is unaffected by the `cluster_observability` toggle.
- **Cluster observability (this feature)** — per-cluster Prometheus/Grafana at a
  cardinality CloudWatch does not surface: per-GPU DCGM series, scheduler queue
  depth, KEDA scaler lag, per-pod GCO service RED metrics.

Reach for CloudWatch for cross-region platform health; reach for cluster
observability to debug what one cluster is doing in detail.

## Cost

Cluster observability is **on by default**, so its cost applies to every regional
cluster unless you opt out. The drivers, per region:

- **EBS (gp3) persistent volumes** — Prometheus TSDB (default `50Gi`), Grafana
  database + dashboards (`10Gi`), and Alertmanager (`5Gi`). These are the
  standing cost: roughly the gp3 rate for ~65 GiB per region, plus snapshots if
  you enable them. Sizes are configurable under
  `cluster_observability.{prometheus,grafana,alertmanager}.persistence_size`, and
  Prometheus retention (default `15d`) bounds how much of the 50 GiB fills.
- **Compute** — the component pods (Prometheus, Grafana, Alertmanager, operator,
  `kube-state-metrics`) plus a `node-exporter` and DCGM-exporter DaemonSet pod on
  each node. These are small (tens to low-hundreds of millicores) but scale with
  node count because the DaemonSets run one pod per node.
- **Data transfer** — scrape traffic stays in-cluster (no cross-AZ storage
  reads), so egress cost is negligible. There is no public endpoint and no ALB,
  so no load-balancer hours.

To eliminate the cost entirely, `gco monitoring disable` then redeploy — the
stack and its EBS volumes are removed. See also
[`docs/ARCHITECTURE.md`](ARCHITECTURE.md#cost-optimization).

## What it provisions

Per regional cluster, when enabled:

- The `kube-prometheus-stack` chart in the `monitoring` namespace (Prometheus,
  Alertmanager, Grafana, `kube-state-metrics`, `node-exporter`, operator).
- A gated `gco-observability-gp3` StorageClass backing the persistent volumes.
- `ServiceMonitor`s for the schedulers/operators (KEDA, Volcano, Kueue, KubeRay,
  YuniKorn) and the DCGM GPU exporter, plus `PodMonitor`s for the GCO services
  (health-monitor, manifest-processor, inference-monitor), which expose
  Prometheus `/metrics`.
- A standalone DCGM exporter DaemonSet on GPU nodes for per-GPU metrics.
- Curated Grafana dashboards (see [below](#curated-dashboards)).
- A credential-rotation CronJob (see
  [Admin credential rotation](#admin-credential-rotation)).

Grafana uses **Grafana-native authentication** (its own user database) with self
sign-up and anonymous access disabled. All three UIs (Grafana, Prometheus,
Alertmanager) are `ClusterIP` Services — there is **no public endpoint**.

## Enabling and disabling

```bash
gco monitoring status              # show the current cdk.json toggle + config
gco monitoring disable             # opt out (removed on next deploy)
gco monitoring enable              # opt back in
gco stacks deploy gco-us-east-1    # apply the change to a region
```

`enable` / `disable` only edit `cdk.json`; the change takes effect on the next
`gco stacks deploy` (or `deploy-all`).

## Accessing Grafana on a private cluster

The EKS API endpoint defaults to **PRIVATE** (`eks_cluster.endpoint_access =
"PRIVATE"`), and Grafana has no public endpoint. Access is therefore a
`kubectl port-forward` over an authenticated session to the private API server —
which means you must first have network reachability into the VPC (VPN, bastion,
or AWS Systems Manager). `gco monitoring open` handles the forward and can build
the SSM tunnel for you:

```bash
# From inside the VPC (VPN / bastion / Cloud9): the endpoint is reachable, so a
# plain forward works. Browse http://localhost:3000.
gco monitoring open --region us-east-1

# From a laptop with no VPC route: tunnel to the private API endpoint through an
# SSM-managed instance in the VPC, then port-forward Grafana over that tunnel.
gco monitoring open --region us-east-1 --via-ssm i-0123456789abcdef0
```

`--via-ssm` opens an `AWS-StartPortForwardingSessionToRemoteHost` session to the
cluster's API endpoint on a local port, then runs `kubectl port-forward` against
`https://localhost:8443` with the real endpoint as the TLS server name. It
requires the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
locally and an SSM-managed instance in the VPC that can reach the endpoint.

Other components:

```bash
gco monitoring open --service prometheus --region us-east-1     # localhost:9090
gco monitoring open --service alertmanager --region us-east-1   # localhost:9093
```

If the endpoint is private and you pass no `--via-ssm`, `open` prints the
SSM/VPN/bastion options and still attempts the forward in case you already have
connectivity. To allow direct kubectl from outside the VPC instead, set
`eks_cluster.endpoint_access` to `PUBLIC_AND_PRIVATE` and redeploy (less secure).

## Managing Grafana users

Grafana uses its own user database, so users are managed through Grafana's admin
HTTP API rather than Cognito. These commands talk to Grafana over an active
`gco monitoring open` port-forward (default `http://localhost:3000`); the admin
credential is read from the chart-generated `kube-prometheus-stack-grafana`
Secret, or passed with `--admin-password` / `$GCO_GRAFANA_ADMIN_PASSWORD`.

```bash
# In one terminal:
gco monitoring open --region us-east-1 --via-ssm i-0123456789abcdef0

# In another:
gco monitoring users list
gco monitoring users add --username alice --email alice@example.com --generate-password
gco monitoring users remove --username alice --yes
```

## Admin credential rotation

The Grafana subchart auto-generates a strong random admin password into the
`kube-prometheus-stack-grafana` Secret on install; GCO never authors it. A
scheduled in-cluster `CronJob` (`gco-grafana-admin-password-rotation`, in the
`monitoring` namespace) then rotates it so the standing credential is refreshed
rather than living unchanged for the cluster's lifetime.

The CronJob resets the live password through Grafana's admin API and updates the
Secret that `gco monitoring users` reads (a restart cannot rotate it — Grafana
persists the password in its own database and only seeds it from the environment
on first start). Its ServiceAccount is least-privilege: `get`/`patch` on that one
Secret, no `pods/exec`, no AWS permissions.

The cadence is configurable and defaults to monthly:

```json
"cluster_observability": {
  "grafana": { "admin_password_rotation_schedule": "0 4 1 * *" }
}
```

Rotation is transparent to the CLI, which always re-reads the current password
from the Secret.

## Curated dashboards

Four GCO dashboards are imported automatically by the Grafana sidecar (they are
ConfigMaps labeled `grafana_dashboard: "1"`), alongside the stock
kube-prometheus-stack cluster/node/pod dashboards:

- **GCO GPU (DCGM)** — per-GPU utilization, framebuffer memory, temperature, and
  power draw.
- **GCO Schedulers & Queues** — pending pods, Kueue pending workloads, active
  Jobs.
- **GCO KEDA Autoscaling** — active scalers and scaler errors.
- **GCO Services** — request rate and p95 latency per GCO service, plus inference
  monitor reconcile/error counts.
