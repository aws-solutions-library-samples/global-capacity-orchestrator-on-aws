# GCO Kubernetes Manifests

Applied to each regional EKS cluster by the `kubectl-applier` Lambda
(`../handler.py`) during CDK deployment. The handler globs this directory and
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

- **Feature gating by placeholder.** Every `{{VARIABLE}}` is substituted at
  deploy time (see [Template Variables](#template-variables)). If a file still
  contains an `UPPER_SNAKE` placeholder *after* substitution, the handler skips
  it — that is how optional features (FSx, Valkey, Aurora pgvector, cluster
  observability, the queue processor) turn themselves off.
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
| `00-09` | Foundation | Namespaces, service accounts, RBAC, network policies, resource quotas |
| `10-19` | Networking | IngressClass, Ingress |
| `20-29` | Storage | EFS, FSx Lustre, cluster-shared bucket, Valkey, Aurora pgvector, observability gp3 |
| `30-39` | System services | health-monitor, manifest-processor, inference-monitor |
| `40-49` | NodePools | GPU (x86, ARM), inference, EFA (training + mooncake), Neuron, CPU |
| `50-59` | Device plugins & GPU observability | NVIDIA device plugin, DCGM exporter |
| `post-helm-*` | Post-Helm | Resources needing Helm CRDs: KEDA ScaledJob, Prometheus monitors, Grafana dashboards/rotation, Kueue metrics RBAC |

## Files

### Foundation (00–09)

| File | Contents |
|------|----------|
| `00-namespaces.yaml` | `gco-system`, `gco-jobs`, `gco-inference` namespaces |
| `01-serviceaccounts.yaml` | `gco-service-account` in `gco-jobs` and `gco-inference` (IRSA role-ARN annotation; token automount disabled) |
| `02-rbac.yaml` | Per-service `ClusterRole`/`Role` + platform-service `ServiceAccount`s + bindings (least-privilege) |
| `03-network-policies.yaml` | Default-deny ingress + allow rules for ALB, DNS, HTTPS egress |
| `04-resource-quotas.yaml` | `ResourceQuota` + `LimitRange` for `gco-jobs` (namespace CPU/memory/GPU/pod caps + per-container defaults) |

### Networking (10–19)

| File | Contents |
|------|----------|
| `10-ingressclass.yaml` | `IngressClassParams` (ALB group) + `IngressClass` |
| `11-ingress.yaml` | `gco-ingress` routing to health-monitor and manifest-processor |

### Storage (20–29)

| File | Contents |
|------|----------|
| `20-storage-efs.yaml` | EFS `StorageClass` + PVCs in all namespaces (dynamic provisioning) |
| `21-storage-fsx.yaml` | FSx Lustre `StorageClass` + PVs + PVCs — **skipped when FSx disabled** |
| `22-storage-cluster-shared-bucket.yaml` | `gco-cluster-shared-bucket` `ConfigMap` (name/ARN/region) in all namespaces — always present |
| `23-storage-valkey.yaml` | Valkey endpoint `ConfigMap` in all namespaces — **skipped when Valkey disabled** |
| `24-storage-aurora-pgvector.yaml` | `gco-aurora-pgvector` `ConfigMap` (endpoint/port/secret/db) in all namespaces — **skipped when Aurora pgvector disabled** |
| `25-storage-observability-gp3.yaml` | `gco-observability-gp3` `StorageClass` backing Prometheus/Grafana/Alertmanager PVCs — **skipped when observability disabled** |

### System Services (30–39)

| File | Contents |
|------|----------|
| `30-health-monitor.yaml` | `Deployment` + `PodDisruptionBudget` + `Service` |
| `31-manifest-processor.yaml` | `Deployment` + `PodDisruptionBudget` + `Service` |
| `32-inference-monitor.yaml` | `Deployment` + `PodDisruptionBudget` |

### NodePools (40–49)

| File | Contents |
|------|----------|
| `40-nodepool-gpu-x86.yaml` | x86_64 GPU pool (g4dn, g5, g6, g6e, g6f, gr6, gr6f, g7, g7e, p3, p3dn) — on-demand + spot |
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
| `51-dcgm-exporter.yaml` | DCGM exporter `DaemonSet` + device-counters `ConfigMap` (GPU metrics for Prometheus) — **skipped when observability disabled** |

### Post-Helm (applied after Helm installs CRDs)

| File | Contents |
|------|----------|
| `post-helm-sqs-consumer.yaml` | KEDA `ScaledJob` for the SQS queue processor — **skipped when queue_processor disabled** |
| `post-helm-monitoring-servicemonitors.yaml` | `ServiceMonitor`s (schedulers/operators + DCGM) and `PodMonitor`s (GCO services) — **skipped when observability disabled** |
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
