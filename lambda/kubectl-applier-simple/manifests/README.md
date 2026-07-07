# GCO Kubernetes Manifests

Applied to the EKS cluster by the `kubectl-applier` Lambda during CDK deployment.
Files are applied in **sorted filename order**, so the numeric prefix controls sequencing.

## Table of Contents

- [Naming Convention](#naming-convention)
- [File Groups](#file-groups)
- [Files](#files)
- [Template Variables](#template-variables)
- [Adding New Manifests](#adding-new-manifests)

## Naming Convention

```text
NN-group-name.yaml          # main pass (applied before Helm)
post-helm-name.yaml         # post-Helm pass (applied after Helm installs CRDs)
```

The `post-helm-` prefix is the signal to the handler — no handler changes needed
when adding new CRD-dependent resources, just use the prefix.

## File Groups

| Range | Group | Description |
|-------|-------|-------------|
| `00-09` | Foundation | Namespaces, service accounts, RBAC, network policies |
| `10-19` | Networking | IngressClass, Ingress |
| `20-29` | Storage | EFS, FSx Lustre, Valkey ConfigMap |
| `30-39` | System services | health-monitor, manifest-processor, inference-monitor |
| `40-49` | NodePools | GPU (x86, ARM), inference, EFA (training + mooncake), Neuron, CPU |
| `50-59` | Device plugins | NVIDIA device plugin |
| `post-helm-*` | Post-Helm | Resources requiring Helm CRDs (KEDA ScaledJob, etc.) |

## Files

### Foundation (00–09)

| File | Contents |
|------|----------|
| `00-namespaces.yaml` | `gco-system`, `gco-jobs`, `gco-inference` namespaces |
| `01-serviceaccounts.yaml` | `gco-service-account` in `gco-jobs` and `gco-inference` with IRSA annotation |
| `02-rbac.yaml` | Per-service `ClusterRole`/`Role` + `ServiceAccount` + bindings (least-privilege) |
| `03-network-policies.yaml` | Default-deny ingress + allow rules for ALB, DNS, HTTPS egress |

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
| `22-storage-valkey.yaml` | Valkey endpoint `ConfigMap` in all namespaces — **skipped when Valkey disabled** |

### System Services (30–39)

| File | Contents |
|------|----------|
| `30-health-monitor.yaml` | `Deployment` + `PodDisruptionBudget` + `Service` |
| `31-manifest-processor.yaml` | `Deployment` + `PodDisruptionBudget` + `Service` |
| `32-inference-monitor.yaml` | `Deployment` + `PodDisruptionBudget` |

### NodePools (40–49)

| File | Contents |
|------|----------|
| `40-nodepool-gpu-x86.yaml` | x86_64 GPU pool (g4dn, g5, g6, g6e, p3) — on-demand + spot |
| `41-nodepool-gpu-arm.yaml` | ARM64 GPU pool (g5g) — on-demand |
| `42-nodepool-inference.yaml` | Inference GPU pool — on-demand only, WhenEmpty consolidation |
| `43-nodepool-efa.yaml` | EFA pool (p4d, p5/p5e/p5en, p6-b200/p6-b300/p6e-gb200) — high-performance distributed training (keeps p4d) |
| `44-nodepool-neuron.yaml` | Neuron pool (trn1, trn1n, trn2, inf2) — AWS Trainium/Inferentia |
| `45-nodepool-cpu-general.yaml` | General CPU pool (c/m/r families) — spot-preferred, no GPUs |
| `46-nodepool-mooncake-efa.yaml` | Mooncake EFA pool (p5/p5e/p5en, p6-b200/p6-b300/p6e-gb200) — disaggregated/store/both inference over RoCE; excludes A100-40GB p4d |

### Device Plugins (50–59)

| File | Contents |
|------|----------|
| `50-nvidia-device-plugin.yaml` | NVIDIA device plugin `DaemonSet` |

### Post-Helm (applied after Helm installs CRDs)

| File | Contents |
|------|----------|
| `post-helm-sqs-consumer.yaml` | KEDA `ScaledJob` for SQS queue processor — **skipped when queue_processor disabled** |

## Template Variables

All `{{VARIABLE}}` placeholders are replaced by the kubectl-applier Lambda at deploy time
using values from the CDK stack. Files with unreplaced placeholders are automatically skipped
(used to conditionally enable FSx, Valkey, and the queue processor).

## Adding New Manifests

- **Standard resource**: add a file with the appropriate `NN-` prefix
- **Requires a Helm CRD** (e.g. KEDA, Volcano, KubeRay): use the `post-helm-` prefix
- **Optional feature**: use a template variable that will be left unreplaced when disabled
