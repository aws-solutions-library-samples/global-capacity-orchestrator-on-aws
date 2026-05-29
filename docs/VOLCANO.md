# Volcano Integration

GCO includes [Volcano](https://volcano.sh/) as a batch scheduler for AI/ML and HPC workloads. Volcano is enabled by default and provides gang scheduling, fair-share queuing, and job lifecycle management purpose-built for compute-intensive jobs.

## Overview

Volcano extends Kubernetes with a `batch.volcano.sh/v1alpha1 Job` CRD and a custom scheduler that integrates with kube-scheduler via plugins. It doesn't replace the default scheduler — it augments it.

**When to use Volcano:**

- Distributed training that requires gang scheduling (all-or-nothing pod placement)
- Batch workloads that need fair-share scheduling across teams
- Jobs with master/worker topology (e.g., PyTorch distributed, MPI)
- Workloads that benefit from backfill scheduling to maximize cluster utilization

## What Gets Deployed

Volcano is installed via Helm chart in the `volcano-system` namespace:

| Component | Description |
|-----------|-------------|
| vc-controller-manager | Manages Volcano Job lifecycle and task states |
| vc-scheduler | Custom scheduler with gang, DRF, binpack plugins |
| vc-webhook-manager | Admission webhook for Volcano Job validation |

The `gco-jobs` namespace is created automatically by GCO during stack deployment.

## Key Concepts

### Volcano Jobs

A Volcano Job groups multiple tasks (master, workers) into a single unit. The `minAvailable` field enables gang scheduling — the job only starts when all required pods can be placed.

The `schedulerName: volcano` field is required for gang scheduling and Volcano-specific features. If omitted, the job goes to the default kube-scheduler and pods schedule individually — gang guarantees silently don't apply.

```yaml
apiVersion: batch.volcano.sh/v1alpha1
kind: Job
metadata:
  name: my-training
  namespace: gco-jobs
spec:
  minAvailable: 3        # Gang: all 3 pods must schedule together
  schedulerName: volcano
  queue: default
  maxRetry: 3            # Cap retries to prevent infinite restart loops
  tasks:
  - name: master
    replicas: 1
    template:
      spec:
        containers:
        - name: master
          image: python:3.14-slim
          resources:
            requests:
              cpu: "2"
              memory: "4Gi"
            limits:
              cpu: "4"
              memory: "8Gi"
  - name: worker
    replicas: 2
    template:
      spec:
        containers:
        - name: worker
          image: python:3.14-slim
          resources:
            requests:
              cpu: "2"
              memory: "4Gi"
            limits:
              cpu: "4"
              memory: "8Gi"
```

For GPU tasks, `limits.nvidia.com/gpu` must equal `requests.nvidia.com/gpu`:

```yaml
containers:
- name: gpu-worker
  image: nvidia/cuda:12.6.3-base-ubuntu24.04
  resources:
    requests:
      cpu: "4"
      memory: "16Gi"
      nvidia.com/gpu: "1"
    limits:
      cpu: "8"
      memory: "32Gi"
      nvidia.com/gpu: "1"
  tolerations:
  - key: nvidia.com/gpu
    operator: Equal
    value: "true"
    effect: NoSchedule
```

### Queues

Volcano queues provide resource quotas and fair-share scheduling. The `capability` field sets a hard cap on resources the queue can consume:

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: ml-training
spec:
  weight: 1
  capability:
    cpu: "100"
    memory: "200Gi"
    nvidia.com/gpu: "8"
```

**Important:** Queue `capability` values are hard caps enforced by Volcano. If you set `nvidia.com/gpu: "8"` but only have 4 physical GPUs, jobs requesting more than 4 will pend indefinitely. Coordinate queue capacity values with your actual cluster resources and with Kueue ClusterQueue quotas to avoid overcommitment (see [Coexistence with Kueue](#coexistence-with-kueue-and-slurm)).

### Job Policies

Control how Volcano reacts to pod events:

```yaml
policies:
  - event: PodEvicted
    action: RestartJob      # Restart entire job if a pod is evicted
  - event: PodFailed
    action: RestartJob      # Restart on failure
  - event: TaskCompleted
    action: CompleteJob     # Mark job done when tasks finish
```

Available actions: `RestartJob`, `AbortJob`, `CompleteJob`, `TerminateJob`. Available events: `PodEvicted`, `PodFailed`, `TaskCompleted`, `OutOfSync`, `CommandIssued`, `JobUpdated`.

**Warning:** Using `RestartJob` on `PodFailed` without `maxRetry` can cause infinite restart loops if the failure is deterministic (e.g., OOM, bad code). Always set `maxRetry` on the Job spec to cap retries.

### Gang Scheduling Deadlocks

Gang scheduling guarantees all-or-nothing placement, but this can cause deadlocks. If two gang-scheduled jobs each need 4 GPUs but only 6 are available, neither can start — classic resource deadlock.

Mitigation strategies:

- Set `minAvailable` conservatively relative to cluster capacity
- Use priority-based preemption so higher-priority jobs can evict lower-priority ones
- Enable backfill scheduling (configured by default in GCO) to fill gaps with smaller jobs
- Monitor pending jobs and queue depth to detect deadlocks early

## Run the Example

```bash
kubectl apply -f examples/volcano-gang-job.yaml
kubectl get vcjob -n gco-jobs
kubectl get pods -n gco-jobs -l volcano.sh/job-name=distributed-training
kubectl logs -n gco-jobs -l volcano.sh/job-name=distributed-training
```

## Distributed Training with Checkpointing

For long-running gang-scheduled training, mount shared storage so progress survives restarts. This is especially important with `RestartJob` policies — when a job restarts, all pods are recreated and all in-memory state is lost.

```yaml
tasks:
- name: worker
  replicas: 4
  template:
    spec:
      containers:
      - name: trainer
        image: python:3.14-slim
        resources:
          requests:
            cpu: "4"
            memory: "16Gi"
            nvidia.com/gpu: "1"
          limits:
            cpu: "8"
            memory: "32Gi"
            nvidia.com/gpu: "1"
        volumeMounts:
        - name: checkpoints
          mountPath: /checkpoints
      tolerations:
      - key: nvidia.com/gpu
        operator: Equal
        value: "true"
        effect: NoSchedule
      volumes:
      - name: checkpoints
        persistentVolumeClaim:
          claimName: gco-shared-storage  # EFS PVC (available by default)
```

## Coexistence with Kueue and Slurm

GCO deploys Volcano, Kueue, and Slurm simultaneously. They operate at different layers:

- **Volcano** is a pod scheduler — it decides *where* and *when* pods run, with gang scheduling guarantees. Volcano jobs use `schedulerName: volcano` and bypass the default kube-scheduler.
- **Kueue** is an admission controller — it decides *whether* a job is allowed to start based on quota. Kueue manages standard Kubernetes Jobs, not Volcano Jobs.
- **Slurm** is a separate scheduling system — it manages its own worker pods and job queue.

**Key point:** Volcano Jobs are not subject to Kueue admission control. Kueue only manages resources for jobs it admits (those with `kueue.x-k8s.io/queue-name` labels). Volcano manages its own resource accounting via Queues.

**Avoiding GPU overcommitment:** If you configure both Volcano Queues and Kueue ClusterQueues with GPU quotas, ensure the sum doesn't exceed your physical GPU count. For example, if you have 8 GPUs:

- Volcano Queue: `nvidia.com/gpu: "4"` (for gang-scheduled training)
- Kueue ClusterQueue: `nominalQuota: 4` (for standard batch jobs)

## Monitoring

### From kubectl

Replace `<VCJOB_NAME>` with a Volcano Job name from `kubectl get vcjob`:

```bash
kubectl get vcjob -n gco-jobs                       # Job status
kubectl get queue                                   # Queue status and capacity
kubectl get podgroup -n gco-jobs                    # Gang scheduling groups
kubectl describe vcjob <VCJOB_NAME> -n gco-jobs     # Detailed job info
```

### Prometheus metrics

Volcano exposes metrics on the scheduler and controller. Key metrics:

| Metric | Description |
|--------|-------------|
| `volcano_queue_allocated_count` | Resources allocated per queue |
| `volcano_queue_pending_count` | Pending jobs per queue |
| `volcano_scheduler_e2e_scheduling_latency` | End-to-end scheduling latency |
| `volcano_job_status_count` | Jobs by status (pending, running, completed, failed) |

## Security

### RBAC for Job Creation

Restrict who can create Volcano Jobs and manage Queues:

```yaml
# Allow users to create Volcano Jobs in gco-jobs
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: volcano-job-submitter
  namespace: gco-jobs
rules:
- apiGroups: ["batch.volcano.sh"]
  resources: ["jobs"]
  verbs: ["create", "get", "list", "delete"]

---
# Only admins can create or modify Queues (cluster-scoped)
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: volcano-queue-admin
rules:
- apiGroups: ["scheduling.volcano.sh"]
  resources: ["queues"]
  verbs: ["create", "update", "patch", "delete"]
```

### Network Policies

Distributed Volcano jobs need inter-pod communication (e.g., master↔worker on custom ports). GCO's default network policies allow intra-namespace traffic in `gco-jobs`. If you add stricter policies, ensure Volcano task pods can communicate on the ports your training framework uses (e.g., 23456 for PyTorch distributed, 2222 for MPI).

## Scheduler Plugins

GCO configures Volcano with the following default scheduler configuration. Plugins in the same tier run in parallel; tiers run sequentially:

```yaml
actions: "enqueue, allocate, backfill"
tiers:
  # Tier 1: Priority, gang scheduling, and conformance checks
  - plugins:
      - name: priority      # Job priority ordering
      - name: gang          # All-or-nothing scheduling
      - name: conformance   # Validates job specs
  # Tier 2: Resource allocation and node selection
  - plugins:
      - name: drf           # Dominant Resource Fairness across queues
      - name: predicates    # Node filtering (resources, taints, affinity)
      - name: proportion    # Queue resource proportional sharing
      - name: nodeorder     # Node scoring for optimal placement
      - name: binpack       # Pack pods onto fewer nodes to reduce fragmentation
```

| Plugin | Required | Description |
|--------|----------|-------------|
| `gang` | Yes (for gang scheduling) | All-or-nothing scheduling for distributed jobs |
| `priority` | Recommended | Job priority ordering |
| `conformance` | Recommended | Validates job specs before scheduling |
| `drf` | Optional | Dominant Resource Fairness across queues |
| `predicates` | Recommended | Node filtering (resources, taints, affinity) |
| `proportion` | Optional | Queue resource proportional sharing |
| `nodeorder` | Optional | Node scoring for optimal placement |
| `binpack` | Optional | Pack pods onto fewer nodes to reduce fragmentation |

The `backfill` action allows smaller jobs to fill gaps while larger gang-scheduled jobs wait for resources, improving overall cluster utilization.

## Customization

Edit `lambda/helm-installer/charts.yaml` under `volcano`:

```yaml
volcano:
  enabled: true   # Set to false to disable
  version: "1.14.1"
  values:
    custom:
      scheduler_config:
        actions: "enqueue, allocate, backfill"
        tiers:
          - plugins:
              - name: priority
              - name: gang
```

## Cleanup

```bash
kubectl delete -f examples/volcano-gang-job.yaml
```

## Further Reading

- [Volcano Documentation](https://volcano.sh/en/docs/)
- [Gang Scheduling](https://volcano.sh/en/docs/v1-10-0/plugins/#gang)
- [Queue Management](https://volcano.sh/en/docs/queue/)
- [Job Policies](https://volcano.sh/en/docs/vcjob/)
- [Network Topology Aware Scheduling](https://volcano.sh/en/docs/network_topology_aware_scheduling/)
