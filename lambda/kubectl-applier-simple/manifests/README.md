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
- [Platform Workload Contract](#platform-workload-contract)
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
| `00-19` | Foundation & networking | Namespaces, service accounts, RBAC, network policies, resource quotas, priority classes |
| `20-29` | Storage | [EFS](https://docs.aws.amazon.com/efs/latest/ug/whatisefs.html), FSx Lustre, cluster-shared bucket, Valkey, Aurora pgvector, observability gp3 |
| `30-39` | System services | health-monitor, manifest-processor, inference-monitor, inference-proxy, cost-monitor — every Deployment follows the [platform workload contract](#platform-workload-contract) |
| `40-49` | NodePools | GPU (x86, ARM), inference, [EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html) (training + mooncake), Neuron, CPU |
| `50-59` | GPU observability | DCGM exporter |
| `post-helm-*` | Post-Helm | Resources needing Helm CRDs: cert-manager API workload certificates, Gateway API entrypoint, KEDA ScaledJob, Prometheus monitors, Grafana dashboards/rotation, Kueue metrics RBAC |

## Files

### Foundation & Networking (00–19)

| File | Contents |
|------|----------|
| `00-namespaces.yaml` | `gco-system`, `gco-jobs`, `gco-inference` namespaces |
| `01-serviceaccounts.yaml` | `gco-service-account` in `gco-jobs` and `gco-inference` ([IRSA](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html) role-ARN annotation; token automount disabled) |
| `02-rbac.yaml` | Per-service `ClusterRole`/`Role` + platform-service `ServiceAccount`s + bindings (least-privilege); the two pre-created health-monitor election `Lease`s (`gco-health-monitor-alb-sync`, `gco-health-monitor-webhooks`) so the Role grants `get`/`update` on named objects instead of `create` on every Lease |
| `03-network-policies.yaml` | Default-deny ingress + allow rules for [ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html), DNS, HTTPS egress |
| `04-resource-quotas.yaml` | `ResourceQuota` + `LimitRange` for `gco-jobs` (namespace CPU/memory/GPU/pod caps + per-container defaults) |
| `05-priority-classes.yaml` | `gco-platform-critical` `PriorityClass` (value 1000000) — referenced by every platform-service pod spec (30–34 + the post-Helm SQS consumer) so control-plane pods preempt default-priority user workloads under node pressure instead of being starved by them |

### Storage (20–29)

| File | Contents |
|------|----------|
| `20-storage-efs.yaml` | EFS `StorageClass` + PVCs in all namespaces (dynamic provisioning) |
| `21-storage-fsx.yaml` | FSx Lustre `StorageClass` + PVs + PVCs — **pruned when FSx is disabled** |
| `22-storage-cluster-shared-bucket.yaml` | `gco-cluster-shared-bucket` `ConfigMap` (name/ARN/region) in all namespaces — always present |
| `23-storage-valkey.yaml` | Valkey endpoint `ConfigMap` in all namespaces — **pruned when Valkey is disabled** |
| `24-storage-aurora-pgvector.yaml` | `gco-aurora-pgvector` `ConfigMap` (endpoint/port/secret/db) in all namespaces — **pruned when Aurora pgvector is disabled** |
| `25-storage-observability-gp3.yaml` | `gco-observability-gp3` `StorageClass` backing Prometheus/Grafana/Alertmanager PVCs — **pruned when observability is disabled** |
| `26-storage-vector-store.yaml` | `gco-vector-store` `ConfigMap` (table/index/embedding-model/region) in all namespaces, pointing pods at their local global-table replica — **pruned when the vector store is disabled** |
| `27-storage-regional-shared-bucket.yaml` | `gco-regional-shared-bucket` `ConfigMap` (name/ARN/region) in all namespaces, pointing pods at their own region's always-on general-purpose bucket — always present |

### System Services (30–39)

| File | Contents |
|------|----------|
| `30-health-monitor.yaml` | `Deployment` + `PodDisruptionBudget` + TLS-only `Service`; the application binds pod loopback and a same-image sidecar hot-reloads the cert-manager leaf on port 8443 |
| `31-manifest-processor.yaml` | `Deployment` + `PodDisruptionBudget` + TLS-only `Service`; the application binds pod loopback and a same-image sidecar hot-reloads the cert-manager leaf on port 8443 |
| `32-inference-monitor.yaml` | Leader-elected reconciler `Deployment` (two replicas: leader + hot standby) probed on its :9090 Prometheus endpoint + `PodDisruptionBudget` |
| `33-inference-proxy.yaml` | Dedicated inference `Deployment` with a hot-reloading TLS proxy sidecar (three replicas on create; HPA owns updates) + per-container application CPU/memory and TLS CPU `HorizontalPodAutoscaler` signals (TLS defaults: `100m` request, 70% target; configurable through `cdk.json` `inference_proxy`) + one-disruption-at-a-time `PodDisruptionBudget` + 15-minute stream-drain lifecycle + TLS-only `Service` |
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

### GPU Observability (50–59)

| File | Contents |
|------|----------|
| `50-dcgm-exporter.yaml` | DCGM exporter `DaemonSet` + device-counters `ConfigMap` (GPU metrics for Prometheus) — **pruned when observability is disabled** |

There is deliberately no NVIDIA device plugin manifest: EKS Auto Mode ships
the device plugin built into the node (it is not visible as a DaemonSet) and
advertises `nvidia.com/gpu` natively. A community device plugin cannot
initialize NVML on Auto Mode nodes (the runtime never injects the NVIDIA
driver libraries for it) and crash-loops, permanently failing convergence —
the applier's legacy sweep instead deletes the DaemonSet GCO used to ship
here from upgraded clusters.

### Post-Helm (applied after Helm installs CRDs)

| File | Contents |
|------|----------|
| `post-helm-api-workload-certificates.yaml` | Namespaced self-signed `Issuer` plus rotating ECDSA `Certificate` resources for health-monitor, manifest-processor, and inference-proxy; generated TLS Secrets are mounted only by the hot-reloading TLS proxy sidecars |
| `post-helm-gateway.yaml` | Gateway API entrypoint: `GatewayClass`; default `TargetGroupConfiguration` (`/healthz` HTTPS checks + 900-second drain) plus one service-level configuration tagging each AWS target group as health-monitor, manifest-processor, or inference-proxy; `LoadBalancerConfiguration` (internal HTTPS ALB, `gco.aws/gateway` ownership tag, TLS certificate); `Gateway` `gco-system/gco-gateway`; and the shared `HTTPRoute` routing `/api/v1/health` + `/api/v1/metrics` + `/healthz` to health-monitor, `/inference` to inference-proxy, and everything else to manifest-processor via the `/` catch-all — the ALB re-encrypts traffic to each pod's TLS-only proxy sidecar. Every prefix must name a Service that actually serves it; `tests/test_gateway_route_coverage.py` fails otherwise |
| `post-helm-sqs-consumer.yaml` | KEDA `ScaledJob` for the [SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html) queue processor, with separate projected AWS STS and Kubernetes API service-account tokens — **skipped when queue_processor disabled** |
| `post-helm-grafana-cost-dashboard.yaml` | The *GCO Cost (OpenCost)* Grafana dashboard `ConfigMap` (sidecar-imported) — **skipped and pruned when cost monitoring is disabled** |
| `post-helm-monitoring-servicemonitors.yaml` | `ServiceMonitor`s (schedulers/operators + DCGM) and `PodMonitor`s (GCO services, including inference-proxy) — **skipped when observability disabled** |
| `post-helm-monitoring-kueue-rbac.yaml` | `ClusterRoleBinding` letting Prometheus scrape Kueue's authenticated metrics endpoint — **skipped when observability disabled** |
| `post-helm-grafana-dashboards.yaml` | Curated GCO Grafana dashboard `ConfigMap`s (GPU/DCGM, schedulers, KEDA, services) — **skipped when observability disabled** |
| `post-helm-grafana-credential-rotation.yaml` | `CronJob` (+ `ServiceAccount`/`Role`/`RoleBinding`) that rotates the Grafana admin password — **skipped when observability disabled** |
| `post-helm-kubeflow-trainer-runtimes.yaml` | Kubeflow Trainer `ClusterTrainingRuntime` blueprints (`torch-distributed`), shipped here instead of the chart's kubectl-download hook Job so the bytes are pinned and reviewable — **skipped and pruned when the trainer chart is disabled** (`{{KUBEFLOW_TRAINER_ENABLED}}`) |
| `post-helm-kueue-default-queues.yaml` | Default Kueue topology for `gco-jobs`: `ResourceFlavor` `gco-default-flavor`, `ClusterQueue` `gco-cluster-queue` (quota from the namespace `ResourceQuota` values) and `LocalQueue` `gco-default`, so a Job labelled `kueue.x-k8s.io/queue-name: gco-default` is admitted without hand-applied queue objects — **skipped and pruned when Kueue is disabled** |
| `post-helm-mlflow-network.yaml` | `NetworkPolicy` pair letting pods labelled `gco.io/mlflow-client` in `gco-jobs` reach the in-cluster MLflow tracking server on port 5000 through the namespace's default-deny posture — **skipped and pruned when MLflow is disabled** |
| `post-helm-slurm-network.yaml` | `NetworkPolicy` rules that let slurmctld/slurmd/slurmrestd reach each other and let client pods call the Slurm REST API inside zero-trust `gco-jobs` (the Slinky charts ship none) — **skipped and pruned when Slurm is disabled** |

## Platform Workload Contract

The five `gco-system` Deployments (`30`–`34`) and the queue-processor
`ScaledJob` share one pod shape, pinned by
`tests/test_platform_workload_contract.py` so a new service or an edit to an
old one cannot quietly drop part of it:

| Property | Contract | Why |
|----------|----------|-----|
| Replicas | Fixed per service (`replicas`), except the inference proxy whose HPA owns the count after creation (`gco.aws/hpa-controls-replicas: "true"`); the manifest-processor count comes from `cdk.json` `manifest_processor.replicas` | Control loops gain availability, not throughput, from replicas; only request-path services autoscale |
| Rollout | `RollingUpdate` with `maxUnavailable: 0`, `maxSurge: 1`; `revisionHistoryLimit: 3`; `gco.aws/deployment-timestamp` on the pod template so every deploy rolls exactly once (cost-monitor: `Recreate`, one replica, single writer by design) | Zero-unavailability rollouts without a second back-to-back revision |
| Disruption | `PodDisruptionBudget` with `maxUnavailable: 1` for every multi-replica Deployment | One eviction at a time whatever the replica count; `minAvailable: 2` on a 10-replica HPA target would have allowed eight |
| Placement | Soft `topologySpreadConstraints` (zone, hostname) plus preferred `podAntiAffinity` for every multi-replica Deployment | Spread across nodes and AZs without blocking scheduling on a small cluster |
| Images | `imagePullPolicy: IfNotPresent` — CDK publishes each image under a content-hash tag | A cached layer is always the right layer, and a restart never depends on the registry |
| Probes | `startupProbe` (≥120 s budget), `livenessProbe`, `readinessProbe` on every container; API containers probe their loopback listener through `python -c`, the TLS sidecar through `tcpSocket`/HTTPS, the inference-monitor through its :9090 metrics endpoint | Every container has an honest liveness signal; readiness stays shallow (process up, background task alive) so a dependency blip never turns the whole tier unready behind the ALB |
| Resources | `requests` and `limits` (CPU + memory) on every container; every `/tmp` emptyDir carries a `sizeLimit` | No unbounded pod can starve a node |
| Security | `runAsNonRoot` uid/gid 1000, `RuntimeDefault` seccomp, `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `automountServiceAccountToken: false` with an explicit projected token for RBAC-bound accounts, `enableServiceLinks: false` | Least privilege, and no Service env-var injection into unrelated pods |
| Identity | Dedicated ServiceAccount with IRSA annotation plus a Pod Identity association; `AWS_ROLE_ARN` / `AWS_WEB_IDENTITY_TOKEN_FILE` on the credentialed container only (`eks.amazonaws.com/skip-containers` excludes the TLS sidecar) | The sidecar never holds AWS credentials it does not use |
| Shutdown | `terminationGracePeriodSeconds` > preStop sleep + `GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS`; every FastAPI service passes that budget to uvicorn | In-flight requests and streams drain before the kubelet kills the pod |
| Network | Default-deny ingress in `gco-system`; each workload is selected by exactly the allow rules it needs (`03-network-policies.yaml`, `34-cost-monitor.yaml`) | A workload no policy selects is unreachable, so the contract test requires one |

## Template Variables

All `{{VARIABLE}}` placeholders are replaced by the kubectl-applier Lambda at
deploy time using values from the CDK stack
(`gco/stacks/regional_stack.py`). Files with unreplaced `UPPER_SNAKE`
placeholders are automatically skipped — the mechanism that conditionally
enables FSx, Valkey, Aurora pgvector, cluster observability, and the queue
processor. The required inference TLS settings use a quoted CPU-quantity token
(`{{INFERENCE_PROXY_TLS_CPU_REQUEST}}`) and an unquoted integer HPA token
(`{{INFERENCE_PROXY_TLS_CPU_TARGET_UTILIZATION}}`); the regional stack always
supplies both from `cdk.json` defaults or overrides.

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
