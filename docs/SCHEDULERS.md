# Supported Schedulers & Orchestrators

GCO ships with six scheduling and orchestration tools, each designed for different workload patterns. [KEDA](https://keda.sh/), [Volcano](https://volcano.sh/), [KubeRay](https://docs.ray.io/en/latest/cluster/kubernetes/index.html), [Kueue](https://kueue.sigs.k8s.io/), and [cert-manager](https://cert-manager.io/docs/) are enabled by default. Slurm and [YuniKorn](https://yunikorn.apache.org/) are opt-in.

## Quick Comparison

| Tool | Type | Enabled | Best For |
|------|------|---------|----------|
| [Volcano](VOLCANO.md) | Batch scheduler | Yes | Gang scheduling, distributed training |
| [Kueue](KUEUE.md) | Job queue manager | Yes | Resource quotas, fair sharing, priority admission |
| [KubeRay](KUBERAY.md) | Ray operator | Yes | Distributed computing, hyperparameter tuning, Ray Serve |
| [KEDA](KEDA.md) | Event-driven autoscaler | Yes | Scale-to-zero, SQS triggers, metric-based scaling |
| [Slurm (Slinky)](SLURM_OPERATOR.md) | HPC scheduler | Opt-in | sbatch/srun workflows, HPC migration, deterministic scheduling |
| [YuniKorn](YUNIKORN.md) | App-aware scheduler | Opt-in | Multi-tenant queues, hierarchical quotas, fair sharing |

> **Warning:** If you enable multiple schedulers with GPU quotas (Kueue ClusterQueues, Volcano Queues, YuniKorn queues, Slurm GPU NodeSets), ensure the total doesn't exceed your physical GPU count. See [GPU Quota Coordination](#gpu-quota-coordination) below — misconfiguration can deadlock your cluster.

## How They Relate

These tools operate at different layers and can be combined:

```text
                    ┌─────────────────────────────────┐
                    │         Job Submission          │
                    │  kubectl / gco CLI / REST API   │
                    └──────────────┬──────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
     ┌────────▼────────┐   ┌───────▼───────┐  ┌─────────▼────────┐
     │   Kueue         │   │   KEDA        │  │   Slurm (Slinky) │
     │   Admission &   │   │   Event-driven│  │   HPC scheduler  │
     │   quota control │   │   autoscaling │  │   sbatch/srun    │
     └────────┬────────┘   └───────┬───────┘  └─────────┬────────┘
              │                    │                    │
     ┌────────▼────────────────────▼────────────────────▼─────────┐
     │                    Pod Scheduling                          │
     │         kube-scheduler  /  Volcano  /  YuniKorn            │
     └────────────────────────────┬───────────────────────────────┘
                                  │
     ┌────────────────────────────▼───────────────────────────────┐
     │                    Node Provisioning                       │
     │                    Karpenter (EKS Auto Mode)               │
     └────────────────────────────────────────────────────────────┘
```

- **Kueue** controls *admission* — decides when a job is allowed to start based on quota
- **KEDA** controls *scaling* — creates jobs/replicas in response to external events
- **Volcano** and **YuniKorn** control *pod scheduling* — decide which node a pod runs on
- **Slurm** is a separate scheduling layer — manages its own job queue and worker allocation
- **KubeRay** manages Ray clusters — handles distributed computing lifecycle
- **[Karpenter](https://karpenter.sh/)** provisions nodes — all schedulers benefit from automatic node scaling

## Choosing the Right Tool

### "I need to run a simple batch job"

Use a standard Kubernetes Job. No special scheduler needed.
→ `examples/simple-job.yaml`

### "I need all pods to start together (gang scheduling)"

Use **Volcano** or **YuniKorn**. Both support gang scheduling where all pods in a group must be schedulable before any start.
→ `examples/volcano-gang-job.yaml` or `examples/yunikorn-job.yaml`

### "I need resource quotas per team"

Use **Kueue** for Kubernetes-native quota management, or **YuniKorn** for hierarchical queue-based quotas.
→ `examples/kueue-job.yaml`

### "I need to scale workloads based on SQS/Kafka/metrics"

Use **KEDA**. It scales Deployments and Jobs from zero based on 60+ event sources.
→ `examples/keda-scaled-job.yaml`

### "I need distributed training with Ray"

Use **KubeRay**. It manages Ray clusters with autoscaling worker groups.
→ `examples/ray-cluster.yaml`

### "My team uses Slurm and I want to keep sbatch/srun"

Use **Slurm (Slinky)**. GCO deploys a ready-to-use Slurm cluster by default.
→ `examples/slurm-cluster-job.yaml`

### "I need multi-tenant fair sharing with a web UI"

Use **YuniKorn**. It provides hierarchical queues, DRF fair sharing, and a built-in web dashboard.
→ `examples/yunikorn-job.yaml`

## Combining Tools

Common combinations:

| Combination | Use Case |
|-------------|----------|
| Kueue + Volcano | Quota-controlled gang-scheduled training jobs |
| KEDA + Kueue | Event-triggered jobs with quota admission control |
| KubeRay + Kueue | Ray clusters with resource quota management |
| Slurm + KEDA | Slurm workers that autoscale based on queue depth |
| Volcano + KEDA | Gang-scheduled jobs triggered by external events |

## Scheduler Coexistence

GCO deploys all six tools simultaneously. This works because they operate at different layers, but you need to understand how they interact to avoid resource conflicts.

### How Pod Routing Works

Each pod is handled by exactly one scheduler, determined by `schedulerName`:

| `schedulerName` | Set By | Scheduler |
|-----------------|--------|-----------|
| `yunikorn` | User (explicit in pod spec) | YuniKorn |
| `volcano` | User (explicit in Volcano Job spec) | Volcano |
| `default-scheduler` | Kubernetes default (or omitted) | kube-scheduler |

YuniKorn's admission controller is disabled in GCO. Pods must explicitly set `schedulerName: yunikorn` to use YuniKorn. This means:

- Standard Kubernetes Jobs → default kube-scheduler
- Jobs with `schedulerName: yunikorn` → YuniKorn
- Volcano Jobs → Volcano (explicit `schedulerName: volcano`)
- Slurm worker pods → default kube-scheduler
- System pods (KEDA, Kueue, cert-manager) → default kube-scheduler (no interference)

### GPU Quota Coordination

This is the most important thing to get right. GPU quotas are defined independently in multiple systems:

| System | Quota Mechanism | Scope |
|--------|----------------|-------|
| YuniKorn | Queue `max` resources | Pods scheduled by YuniKorn |
| Kueue | ClusterQueue `nominalQuota` | Jobs admitted by Kueue |
| Volcano | Queue `capability` | Volcano Jobs only |
| Slurm | NodeSet `replicas` × GPU per worker | Slurm jobs only |

**The quotas don't talk to each other.** If you configure 8 GPUs in YuniKorn queues, 8 in Kueue ClusterQueues, 4 in Volcano Queues, and 2 in Slurm GPU workers, you've promised 22 GPUs across systems — but you might only have 8 physical GPUs.

**Recommended approach:** Partition GPU capacity across the systems you actually use:

```text
Example: 8 physical GPUs
├── Slurm GPU workers: 0 (CPU-only by default, no GPU conflict)
├── Volcano Queue cap: 4 GPUs (for gang-scheduled training)
├── Kueue ClusterQueue: 4 GPUs (for standard batch jobs)
└── YuniKorn queue max: 8 GPUs (YuniKorn sees all pods, so set to total)
```

Since Kueue controls admission and YuniKorn controls scheduling, a Kueue-admitted job's pods flow through YuniKorn. Set YuniKorn's queue max to total cluster capacity, and use Kueue's quotas for the actual per-team limits.

### What to Disable If You Don't Need It

Not every team needs all six tools. Disable what you don't use to reduce complexity:

| If you only need... | Keep enabled | Disable |
|---------------------|-------------|---------|
| Simple batch jobs with quotas | Kueue, YuniKorn | Volcano, Slurm |
| Gang-scheduled distributed training | Volcano, Kueue | Slurm, YuniKorn |
| Slurm workflows (sbatch/srun) | Slurm, cert-manager | Volcano, YuniKorn |
| Ray distributed computing | KubeRay, Kueue | Volcano, Slurm |
| Event-driven scaling only | KEDA | Volcano, Slurm, YuniKorn |

KEDA and KubeRay are lightweight operators that don't conflict with anything — safe to leave enabled regardless.

## Configuration

A useful subset of schedulers is enabled by default, but every cluster is different. Experiment to find which tools best suit your workloads and disable the ones you don't need — each enabled chart runs controller pods that consume CPU and memory on your system nodes. Fewer charts means less overhead and faster deploys.

All schedulers are toggled via the `helm` section in `cdk.json`:

```json
{
  "context": {
    "helm": {
      "keda": { "enabled": true },
      "volcano": { "enabled": true },
      "kuberay": { "enabled": true },
      "kueue": { "enabled": true },
      "cert_manager": { "enabled": true },
      "slurm": { "enabled": false },
      "yunikorn": { "enabled": false }
    }
  }
}
```

Then redeploy: `gco stacks deploy-all -y`

## Limitations

- **Enabling all schedulers adds resource overhead.** Each scheduler runs controller pods that consume CPU and memory. If scheduler pods are stuck Pending, Karpenter needs to provision more system nodes — check `kubectl get nodes` and `kubectl get pods -A --field-selector=status.phase=Pending`.
- **No cross-scheduler quota enforcement.** GPU quotas in Kueue, Volcano, YuniKorn, and Slurm are independent. You must manually coordinate them to avoid overcommitting physical resources.
- **Gang scheduling deadlocks.** Both Volcano and YuniKorn support gang scheduling, but competing gang-scheduled jobs can deadlock if cluster capacity is insufficient. Monitor pending jobs and set appropriate `minAvailable`/`minMember` values.
- **Helm upgrade ordering.** Kueue's mutating webhook intercepts all Job and Deployment mutations. If Kueue's pod is down during a deploy, other chart upgrades can fail. GCO mitigates this by installing Kueue last and retrying with stale webhook cleanup.
