# GCO Kubernetes Manifests

Applied to each regional [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) cluster by the `kubectl-applier` [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html)
(`../handler.py`) during [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) deployment. The handler globs this directory and
applies files in **sorted filename order**, so the numeric prefix controls
sequencing — there is no hardcoded file list, so adding a manifest never
requires a handler change.

## Table of Contents

- [How Manifests Are Applied](#how-manifests-are-applied)
- [Naming Convention](#naming-convention)
- [File Groups](#file-groups)
- [Files](#files)
- [Template Variables](#template-variables)
- [Adding New Manifests](#adding-new-manifests)

## How Manifests Are Applied

Two passes, driven by the convergence pipeline:

1. **Main pass** — every `NN-*.yaml` file, applied before the Helm charts
   install. Runs in sorted filename order.
2. **Post-Helm pass** — every `post-helm-*.yaml` file, applied *after* Helm
   installs the CRDs those resources depend on (KEDA, Prometheus Operator,
   Kueue, etc.).

Two behaviors are worth knowing:

- **Feature convergence by placeholder.** Every `{{VARIABLE}}` is substituted
  at deploy time (see [Template Variables](#template-variables)). If a file still
  contains an `UPPER_SNAKE` placeholder *after* substitution, the handler skips
  applying that file and deletes only the exact resources inventoried for that
  disabled feature. [FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html) convergence removes the three managed PVCs, PVs, and
  StorageClass; Valkey and [Aurora](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/CHAP_AuroraOverview.html) convergence removes their namespaced
  ConfigMaps; observability convergence removes its managed storage, exporters,
  dashboards, monitors, rotation job, service account, and bindings; queue
  convergence removes its managed `ScaledJob`. Missing resources are no-ops,
  while any other deletion error fails the deployment rather than leaving stale
  resources silently active. Disabling FSx detaches the managed Kubernetes
  storage objects and can disrupt workloads that still reference those claims;
  it does not erase the external FSx file system itself.
- **Numbers order files, they don't identify them.** The prefix only sets apply
  order within a pass. Gaps between decade blocks are intentional headroom for
  future inserts; a missing number has no effect.

## Naming Convention

```text
NN-group-name.yaml          # main pass (applied before Helm)
post-helm-name.yaml         # post-Helm pass (applied after Helm installs CRDs)
```

The `post-helm-` prefix is the only signal the handler needs — no handler
change is required to add a new CRD-dependent resource, just use the prefix.

## File Groups

| Range | Group | Description |
|-------|-------|-------------|
| `00-19` | Foundation & networking | Namespaces, service accounts, RBAC, network policies, resource quotas |
| `20-29` | Storage | [EFS](https://docs.aws.amazon.com/efs/latest/ug/whatisefs.html), FSx Lustre, cluster-shared bucket, Valkey, Aurora pgvector, observability gp3 |
| `30-39` | System services | health-monitor, manifest-processor, inference-monitor |
| `40-49` | NodePools | GPU (x86, ARM), inference, [EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html) (training + mooncake), Neuron, CPU |
| `50-59` | Device plugins & GPU observability | NVIDIA device plugin, DCGM exporter |
| `post-helm-*` | Post-Helm | Resources needing Helm CRDs: Gateway API entrypoint, KEDA ScaledJob, Prometheus monitors, Grafana dashboards/rotation, Kueue metrics RBAC |

## Files

### Foundation & Networking (00–19)

| File | Contents |
|------|----------|
| `00-namespaces.yaml` | `gco-system`, `gco-jobs`, `gco-inference` namespaces |
| `01-serviceaccounts.yaml` | `gco-service-account` in `gco-jobs` and `gco-inference` ([IRSA](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html) role-ARN annotation; token automount disabled) |
| `02-rbac.yaml` | Per-service `ClusterRole`/`Role` + platform-service `ServiceAccount`s + bindings (least-privilege) |
| `03-network-policies.yaml` | Default-deny ingress + allow rules for [ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html), DNS, HTTPS egress |
| `04-resource-quotas.yaml` | `ResourceQuota` + `LimitRange` for `gco-jobs` (namespace CPU/memory/GPU/pod caps + per-container defaults) |

### Storage (20–29)

| File | Contents |
|------|----------|
| `20-storage-efs.yaml` | EFS `StorageClass` + PVCs in all namespaces (dynamic provisioning) |
| `21-storage-fsx.yaml` | FSx Lustre `StorageClass` + PVs + PVCs — **pruned when FSx is disabled** |
| `22-storage-cluster-shared-bucket.yaml` | `gco-cluster-shared-bucket` `ConfigMap` (name/ARN/region) in all namespaces — always present |
| `23-storage-valkey.yaml` | Valkey endpoint `ConfigMap` in all namespaces — **pruned when Valkey is disabled** |
| `24-storage-aurora-pgvector.yaml` | `gco-aurora-pgvector` `ConfigMap` (endpoint/port/secret/db) in all namespaces — **pruned when Aurora pgvector is disabled** |
| `25-storage-observability-gp3.yaml` | `gco-observability-gp3` `StorageClass` backing Prometheus/Grafana/Alertmanager PVCs — **pruned when observability is disabled** |

### System Services (30–39)

| File | Contents |
|------|----------|
| `30-health-monitor.yaml` | `Deployment` + `PodDisruptionBudget` + `Service` |
| `31-manifest-processor.yaml` | `Deployment` + `PodDisruptionBudget` + `Service` |
| `32-inference-monitor.yaml` | `Deployment` + `PodDisruptionBudget` |
| `33-inference-proxy.yaml` | Dedicated inference `Deployment` (three replicas on create; HPA owns updates) + CPU/memory `HorizontalPodAutoscaler` + two-pod `PodDisruptionBudget` + 15-minute stream-drain lifecycle + `Service` |
| `34-cost-monitor.yaml` | Cost monitor `ServiceAccount` + single-replica `Recreate` `Deployment` + `Service` + three `NetworkPolicy` rules (manifest-processor ingress/egress, [OpenCost](https://opencost.io/) egress) — **skipped and pruned when cost monitoring is disabled** |

### NodePools (40–49)

| File | Contents |
|------|----------|
| `40-nodepool-gpu-x86.yaml` | x86_64 GPU pool (g4dn, g5, g6, g6e, g6f, gr6, gr6f, g7, g7e) — on-demand + spot; deprecated V100 p3/p3dn families are observation-only and excluded from new scheduling |
| `41-nodepool-gpu-arm.yaml` | ARM64 GPU pool (g5g) — on-demand + spot |
| `42-nodepool-inference.yaml` | Inference GPU pool (g4dn, g5, g6, g6e, g6f, gr6, gr6f, g7, g7e) — on-demand + spot, WhenEmpty consolidation |
| `43-nodepool-efa.yaml` | EFA pool (p4d, p4de, p5/p5e/p5en, p6-b200/p6-b300/p6e-gb200) — high-performance distributed training (keeps p4d) |
| `44-nodepool-neuron.yaml` | Neuron pool (trn1, trn1n, trn2, inf1, inf2) — AWS Trainium/Inferentia |
| `45-nodepool-cpu-general.yaml` | General CPU pool (c/m/r families) — spot-preferred, no GPUs |
| `46-nodepool-mooncake-efa.yaml` | Mooncake EFA pool (p5/p5e/p5en, p6-b200/p6-b300/p6e-gb200) — disaggregated/store/both inference over RoCE; excludes A100-40GB p4d |

### Device Plugins & GPU Observability (50–59)

| File | Contents |
|------|----------|
| `50-nvidia-device-plugin.yaml` | NVIDIA device plugin `DaemonSet` (advertises `nvidia.com/gpu`) |
| `51-dcgm-exporter.yaml` | DCGM exporter `DaemonSet` + device-counters `ConfigMap` (GPU metrics for Prometheus) — **pruned when observability is disabled** |

### Post-Helm (applied after Helm installs CRDs)

| File | Contents |
|------|----------|
| `post-helm-gateway.yaml` | Gateway API entrypoint: `GatewayClass`, `TargetGroupConfiguration` (`/healthz` target-group health checks + 900-second deregistration drain), `LoadBalancerConfiguration` (internal HTTPS ALB, `gco.aws/gateway` ownership tag, TLS certificate), `Gateway` `gco-system/gco-gateway`, and the shared `HTTPRoute` routing `/api/v1/health` + `/api/v1/metrics` + `/healthz` to health-monitor, `/inference` to inference-proxy, and everything else to manifest-processor via the `/` catch-all — applied after the AWS Load Balancer Controller chart installs the Gateway API CRDs. Every prefix must name a Service that actually serves it; `tests/test_gateway_route_coverage.py` fails otherwise |
| `post-helm-sqs-consumer.yaml` | KEDA `ScaledJob` for the [SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html) queue processor — **skipped when queue_processor disabled** |
| `post-helm-grafana-cost-dashboard.yaml` | The *GCO Cost (OpenCost)* Grafana dashboard `ConfigMap` (sidecar-imported) — **skipped and pruned when cost monitoring is disabled** |
| `post-helm-monitoring-servicemonitors.yaml` | `ServiceMonitor`s (schedulers/operators + DCGM) and `PodMonitor`s (GCO services, including inference-proxy) — **skipped when observability disabled** |
| `post-helm-monitoring-kueue-rbac.yaml` | `ClusterRoleBinding` letting Prometheus scrape Kueue's authenticated metrics endpoint — **skipped when observability disabled** |
| `post-helm-grafana-dashboards.yaml` | Curated GCO Grafana dashboard `ConfigMap`s (GPU/DCGM, schedulers, KEDA, services) — **skipped when observability disabled** |
| `post-helm-grafana-credential-rotation.yaml` | `CronJob` (+ `ServiceAccount`/`Role`/`RoleBinding`) that rotates the Grafana admin password — **skipped when observability disabled** |

## Template Variables

All `{{VARIABLE}}` placeholders are replaced by the kubectl-applier Lambda at
deploy time using values from the CDK stack
(`gco/stacks/regional_stack.py`). Files with unreplaced `UPPER_SNAKE`
placeholders are automatically skipped — the mechanism that conditionally
enables FSx, Valkey, Aurora pgvector, cluster observability, and the queue
processor.

Lower- or mixed-case double-brace tokens (e.g. Grafana dashboard legends like
`{{gpu}}` or `{{Hostname}}`) are **not** placeholders — the handler's skip
check matches only `UPPER_SNAKE`, so those are applied verbatim.

## Adding New Manifests

- **Standard resource**: add a file with the appropriate `NN-` prefix for its
  group. Pick any free number in the group's decade — order is all that
  matters.
- **Requires a Helm CRD** (KEDA, Prometheus Operator, Kueue, KubeRay, …): use
  the `post-helm-` prefix.
- **Optional feature**: gate it with a template variable that is left
  unreplaced (and therefore skips the file) when the feature is disabled.
