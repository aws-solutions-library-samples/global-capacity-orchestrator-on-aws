# Cost Monitoring & Cost-Aware Scheduling

Cost monitoring gives GCO deployments per-cluster Kubernetes cost allocation,
durable cross-region cost analytics, and cost-aware job dispatch:

- **[OpenCost](https://opencost.io/) per region** — installed into each
  regional cluster's `monitoring` namespace alongside `kube-prometheus-stack`,
  allocating node/PV list prices to namespaces, controllers, and pods from
  Prometheus usage data.
- **A Grafana cost dashboard** — the curated *GCO Cost (OpenCost)* dashboard
  renders cluster hourly/monthly cost, per-namespace allocation, top spenders,
  and node cost by instance type from OpenCost's Prometheus metrics.
- **The `cost-monitor` service** — a singleton Deployment in `gco-system` on
  each regional cluster that writes scheduled Parquet allocation reports to
  the central cost report bucket and serves ad-hoc reporting through the GCO
  API (`/api/v1/cost/*`).
- **Centralized Athena analytics** — the monitoring stack provisions an S3
  cost report bucket, an [AWS Glue](https://docs.aws.amazon.com/glue/latest/dg/what-is-glue.html)
  table with partition projection over the report layout, and an
  [Amazon Athena](https://docs.aws.amazon.com/athena/latest/ug/what-is.html)
  workgroup, so `gco costs k8s` aggregates cost across every region without
  any dashboard server, crawler, or scheduled repair job.
- **Spot price-aware scheduling** — jobs submitted to the central queue can
  carry a spot price cap; the regional worker holds them until the market
  clears the cap.

Like cluster observability, cost monitoring is **on by default**. It requires
cluster observability (OpenCost reads usage from the in-cluster Prometheus),
so disabling observability switches the whole cost pipeline off with it.

> **Region placeholders.** Examples use `us-east-1` for a regional cluster and
> `us-east-2` for the monitoring region. Substitute your own values from
> `deployment_regions` in `cdk.json`.

## Table of Contents

- [Architecture](#architecture)
- [Cost of cost monitoring](#cost-of-cost-monitoring)
- [Enabling and disabling](#enabling-and-disabling)
- [Configuration](#configuration)
- [Accessing the cost dashboards](#accessing-the-cost-dashboards)
- [Cost reports in S3](#cost-reports-in-s3)
- [Cross-region analytics with Athena](#cross-region-analytics-with-athena)
- [Ad-hoc reports and the cost API](#ad-hoc-reports-and-the-cost-api)
- [Spot price-aware scheduling](#spot-price-aware-scheduling)
- [Release validation](#release-validation)
- [Troubleshooting](#troubleshooting)

## Architecture

```text
             regional cluster (per region)                     monitoring region
┌─────────────────────────────────────────────────┐   ┌─────────────────────────────────┐
│  kube-prometheus-stack (monitoring ns)          │   │  Cost report bucket (S3, KMS)   │
│    Prometheus ◄── ServiceMonitor ── OpenCost    │   │   reports/region=…/date=…/*.pq  │
│    Grafana ── "GCO Cost (OpenCost)" dashboard   │   │   adhoc/region=…/date=…/*.pq    │
│                        ▲                        │   │   athena-results/               │
│                        │ /allocation/compute    │   │            ▲                    │
│  cost-monitor (gco-system ns)  ─────────────────┼───┼── Parquet ─┘                    │
│    scheduled interval reports + ad-hoc API      │   │                                 │
│                        ▲                        │   │  Glue database + table          │
│  manifest API /api/v1/cost/* (proxy)            │   │   (partition projection)        │
└─────────────────────────────────────────────────┘   │  Athena workgroup <project>-cost│
                         ▲                            └─────────────────────────────────┘
                         │ SigV4 (API Gateway)                        ▲
                    gco costs report …                          gco costs k8s …
```

Data flows one direction: OpenCost allocates usage from Prometheus, the
cost-monitor normalizes allocation windows into Parquet rows and writes them
to the central bucket, and Athena reads the bucket across all regions. The
Grafana dashboard is served entirely from in-cluster Prometheus metrics and
works even if the S3 pipeline is down.

Scheduled report object keys are **deterministic per window** (aligned to the
report interval), so a worker restart, rollout overlap, or retry converges on
the same object instead of double-counting a window in Athena.

## Cost of cost monitoring

The feature is deliberately lightweight:

- **OpenCost** — one pod per region (cost model + UI containers, requests of
  tens of millicores / ~110 Mi). It queries the Prometheus you already run
  with cluster observability.
- **cost-monitor** — one small pod per region.
- **S3** — Parquet allocation reports are a few KiB per interval per region.
  Lifecycle rules transition reports to STANDARD_IA (default 90 days) and
  expire them (default 365 days); Athena query results expire after 30 days.
- **Athena** — pay-per-query over kilobyte-scale Parquet with partition
  pruning; canned CLI queries scan only the days they aggregate.
- **Glue** — a single database and table definition (no crawler).

## Enabling and disabling

Cost monitoring follows `cost_monitoring.enabled` in `cdk.json` **and**
requires `cluster_observability.enabled` (both default `true`):

| `cluster_observability.enabled` | `cost_monitoring.enabled` | Effective |
|---|---|---|
| `true` | `true` | **On** (default) |
| `true` | `false` | Off |
| `false` | `true` | Off — OpenCost has no Prometheus to read |
| `false` | `false` | Off |

Flip the toggle in `cdk.json`, then redeploy (`gco stacks deploy-all`).
Disabling prunes the OpenCost release, the cost-monitor Deployment, its
NetworkPolicies, and the Grafana cost dashboard from each cluster on the next
deploy, and skips the bucket/Glue/Athena resources in the monitoring stack.

## Configuration

The `cost_monitoring` block in `cdk.json`:

```json
"cost_monitoring": {
  "enabled": true,
  "reports": {
    "interval_minutes": 60,
    "retention_days": 365,
    "transition_to_infrequent_access_days": 90
  },
  "athena": {
    "query_results_retention_days": 30
  }
}
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master toggle (conjunct with `cluster_observability.enabled`). |
| `reports.interval_minutes` | `60` | Cadence of scheduled allocation reports (5-1440). Each report covers the most recent *completed* interval-aligned window. |
| `reports.retention_days` | `365` | S3 lifecycle expiration for report objects. |
| `reports.transition_to_infrequent_access_days` | `90` | S3 lifecycle transition to STANDARD_IA. Must be smaller than `retention_days` (validated at synth). |
| `athena.query_results_retention_days` | `30` | S3 lifecycle expiration for Athena query results under `athena-results/`. |

OpenCost itself needs no credentials or per-account configuration: it prices
nodes and volumes from the public AWS list-price API and reads usage from the
in-cluster Prometheus. The chart pin and hardening values live in
`lambda/helm-installer/charts.yaml` (`opencost`); the cluster identity is
injected per region by the regional stack.

## Accessing the cost dashboards

All access is private, over the same SSM-tunneled port-forward path as
Grafana and Prometheus (see [MONITORING.md](MONITORING.md#accessing-grafana-on-a-private-cluster)).

**Grafana cost dashboard** (recommended):

```bash
# Direct URL to the GCO Cost dashboard; --via-ssm auto provisions a
# self-terminating bastion when you are outside the VPC.
gco costs dashboard --region us-east-1 --via-ssm auto -y
# → http://localhost:3000/d/gco-cost/gco-cost-opencost
```

**Native OpenCost UI** (per-workload drill-down):

```bash
gco costs dashboard --service opencost --region us-east-1 --via-ssm auto -y
# → http://localhost:9091
```

Equivalent `gco monitoring open` targets exist for scripting: `--service
opencost` (UI, local `9091`) and `--service opencost-api` (allocation API,
local `9003`). Local ports deliberately avoid the Prometheus forward's `9090`
so both tunnels can run side by side.

The *GCO Cost (OpenCost)* dashboard panels: cluster hourly cost
(compute + storage), projected monthly cost, node hourly cost split
(CPU/RAM/GPU), hourly cost by namespace, top-10 namespaces, node cost by
instance type, and persistent volume cost.

## Cost reports in S3

The monitoring stack owns one central bucket:

```text
s3://<project>-cost-reports-<account>-<monitoring-region>/
├── reports/                          # scheduled reports (the Athena table)
│   └── region=us-east-1/
│       └── date=2026-07-26/
│           └── allocation-20260726T090000Z-20260726T100000Z.parquet
├── adhoc/                            # user-requested reports (never aggregated)
│   └── region=us-east-1/date=2026-07-26/allocation-…-<nonce>.parquet
└── athena-results/                   # workgroup-enforced query results
```

The layout is Hive-partitioned (`region=…/date=…`) so the Glue table's
partition projection resolves partitions directly from object keys — no
crawler, no `MSCK REPAIR TABLE`, no partition-management Lambda. New report
objects are queryable the moment they land.

Each Parquet row is one namespace's accumulated allocation for one window:

| Column | Type | Notes |
|---|---|---|
| `window_start`, `window_end` | timestamp | Interval-aligned UTC window bounds |
| `cluster` | string | `<project>-<region>` |
| `namespace` | string | Includes OpenCost's `__idle__` / `__unallocated__` synthetic rows |
| `cpu_core_hours`, `cpu_cost` | double | Allocated CPU and its cost |
| `ram_gib_hours`, `ram_cost` | double | Allocated memory and its cost |
| `gpu_hours`, `gpu_cost` | double | Allocated GPUs and their cost |
| `pv_cost`, `network_cost`, `load_balancer_cost` | double | Storage and network line items |
| `shared_cost`, `external_cost` | double | OpenCost shared/external allocations |
| `total_cost` | double | Window total for the namespace |
| `total_efficiency` | double | OpenCost utilization efficiency (0-1) |

Partition columns `region` and `date` come from the object key. The bucket is
KMS-encrypted (customer-managed key), versioned, blocks public access,
denies insecure transport, and writes server access logs to a dedicated
lifecycle-expired log bucket. Each region's cost-monitor role is granted
write access by literal deterministic ARN — no cross-region lookups, and the
monitoring stack can safely deploy after the regional stacks (the service
retries its next scheduled write until the bucket exists).

## Cross-region analytics with Athena

The `gco costs k8s` commands run canned aggregations in the
`<project>-cost` workgroup against the `<project>_cost.allocation_reports`
table (results land under `athena-results/`, KMS-encrypted and enforced by
the workgroup):

```bash
# Cost by namespace across every region (last 7 days)
gco costs k8s namespaces

# Cost by deployment region over 30 days
gco costs k8s regions --days 30

# Daily trend for one namespace
gco costs k8s trend -n gco-jobs --days 30

# Hourly trend over the last two days
gco costs k8s trend --granularity hourly --days 2

# Top 5 spenders by region
gco costs k8s top -n 5 --by region
```

These need only AWS credentials with Athena/Glue/S3 access in the monitoring
region — no tunnel and no GCO API round-trip. `gco --output json costs k8s …`
emits machine-readable rows. For anything beyond the canned queries, point
any Athena client at the same workgroup, database, and table.

Note the complementary split with the existing Cost Explorer commands:
`gco costs summary|regions|trend|forecast` report *billed AWS spend* by
service/region tag, while `gco costs k8s …` reports *allocated Kubernetes
cost* by namespace/cluster from OpenCost list-price data. Use the former for
the bill, the latter for who-spent-it inside the clusters.

## Ad-hoc reports and the cost API

Each region's manifest API exposes the authenticated cost surface, proxied to
the in-cluster cost-monitor:

- `GET /api/v1/cost/status` — OpenCost health, the live returning-data probe,
  bucket, cadence, and the last scheduled report.
- `GET /api/v1/cost/reports` — recent scheduled (or `adhoc=true`) report
  objects for the region.
- `POST /api/v1/cost/reports` — generate a report for the trailing
  `window_hours` (1-168) now.

```bash
gco costs report status  -r us-east-1
gco costs report list    -r us-east-1 --limit 50
gco costs report generate -r us-east-1 --window-hours 48 --show-rows
```

Ad-hoc reports land under `adhoc/` — deliberately outside the scheduled
prefix the Athena table reads, so a user-requested window that overlaps
scheduled windows can never double-count in aggregations.

`--region` pins the call to that region's API bridge (in the commercial
partition direct bridge access requires `api_gateway.regional_api_enabled=true`;
in other partitions it is always available). Without `--region` the request
rides the global API to the nearest healthy region, and the response names
the region that answered. Endpoint details and response shapes:
[API.md — Cost Reporting](API.md#cost-reporting).

## Spot price-aware scheduling

Jobs submitted to the **central (DynamoDB-backed) queue** can carry a spot
price cap, targeting cost-sensitive workloads at the queue built for global
placement:

```bash
gco queue submit job.yaml -r us-east-1 \
  --max-spot-price 0.50 --spot-instance-type g5.xlarge
```

Semantics:

- The cap is set at submission time (`max_spot_price` USD/hour +
  `spot_instance_type`, always together) and stored on the queue record.
- On every polling pass, the target region's queue worker looks up the
  instance type's most recent spot price in each Availability Zone
  ([`ec2:DescribeSpotPriceHistory`](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeSpotPriceHistory.html))
  and takes the **minimum across zones** — a capacity-flexible job can land
  in whichever zone clears its cap. Lookups are cached briefly, and an
  *unknown* price always defers (never dispatches) the job.
- While the market price is above the cap the job stays `queued`; the worker
  records a throttled observation so `gco queue get <id>` shows the gate,
  the last observed price, and when it was checked.
- The moment the price clears the cap, dispatch proceeds through the normal
  claim/lease pipeline. The gate controls *when* the job dispatches, not
  *where* its pods run — pair it with a spot-tolerant pod spec (the GPU
  NodePools already prefer spot capacity).
- Price-gated jobs never block other work: the worker fetches a wider
  candidate window than its per-pass apply budget, so dispatchable jobs
  behind a run of gated ones still proceed.
- A gated job waits indefinitely by design. Cancel it with
  `gco queue cancel <id>` if the price never clears, or resubmit with a
  higher cap.

The gate applies to the central queue only. The regional SQS path and direct
manifest submission dispatch immediately, as before.

## Release validation

The live release-validation harness includes an `opencost` action (after
`central-queue`): for every deployed region it polls `/api/v1/cost/status`
until OpenCost is healthy **and returning allocation data** (bounded), then
generates an ad-hoc report and verifies the Parquet object exists in the cost
report bucket. An unhealthy or data-less OpenCost fails validation. When
cost monitoring is disabled in `cdk.json`, the action records that and passes.
See [LIVE_RELEASE_VALIDATION.md](LIVE_RELEASE_VALIDATION.md).

## Troubleshooting

**`gco costs report status` shows `opencost_healthy: false`.**
Check the OpenCost pod in the `monitoring` namespace
(`kubectl -n monitoring get pods -l app.kubernetes.io/name=opencost`). The
chart installs only when both toggles are on; confirm with
`gco monitoring status` and `cost_monitoring` in `cdk.json`.

**`opencost_healthy: true` but `opencost_returning_data: false`.**
OpenCost is up but Prometheus has no usable usage data yet. On a fresh
deploy allow a few scrape cycles. If it persists, verify Prometheus targets
(`gco monitoring open --service prometheus`, then check *Status → Targets*
for the opencost ServiceMonitor and cadvisor).

**`gco costs k8s …` returns no rows.**
Scheduled reports accrue once per `reports.interval_minutes`; a new
deployment has data after the first completed interval. Confirm objects with
`gco costs report list -r <region>`, and that your credentials can query the
`<project>-cost` Athena workgroup in the monitoring region.

**Athena errors about the workgroup or database.**
The monitoring stack owns them (`gco stacks deploy <project>-monitoring`).
The CLI derives their names from `project_name`, so a non-default project
name needs its matching deployment.

**Ad-hoc generation returns 503.**
The manifest API cannot reach the cost-monitor service — cost monitoring is
disabled in that region, or the `cost-monitor` Deployment in `gco-system` is
unhealthy (`kubectl -n gco-system get pods -l app=cost-monitor`).

**A gated job never dispatches.**
`gco queue get <id>` shows the cap and the last observed price. If the
market simply never clears your cap, cancel and resubmit with a higher cap,
a different instance type, or a different target region (compare with
`gco capacity spot-prices -t g5.xlarge`).
