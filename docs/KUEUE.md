# Kueue Integration

GCO includes [Kueue](https://kueue.sigs.k8s.io/) for Kubernetes-native job queueing with resource quotas, fair sharing, and priority scheduling. Kueue is enabled by default.

## Overview

Kueue complements the default kube-scheduler by handling job-level admission control. It doesn't replace the scheduler — it decides *when* jobs are allowed to start based on available quota, then lets kube-scheduler handle pod placement. This makes it the least disruptive scheduler option.

**When to use Kueue:**

- Job queueing with resource quotas per team or project
- Fair sharing of GPU resources across multiple users
- Priority-based job admission with preemption
- Workloads using standard Kubernetes Job, or integrations with [Volcano](https://volcano.sh/), Ray, etc.
- Teams that want quota management without replacing the default scheduler

## What Gets Deployed

Kueue is installed via Helm chart in the `kueue-system` namespace:

| Component | Description |
|-----------|-------------|
| kueue-controller-manager | Manages ClusterQueue, LocalQueue, and Workload CRDs |
| kueue-webhook | Admission webhook for job validation |

The `enablePlainPod: true` setting is on by default, which lets Kueue manage standalone pods (not just Jobs). This is useful for managing Ray head pods, inference servers, or any long-running workload that isn't wrapped in a Job.

## Key Concepts

### Resource Model

```text
ClusterQueue (cluster-wide quotas)
  └── ResourceFlavor (maps to node types)
  └── LocalQueue (namespace-scoped, points to ClusterQueue)
        └── Jobs (labeled with queue name)
```

### ResourceFlavor

Defines a type of resource (e.g., CPU nodes, GPU nodes):

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: ResourceFlavor
metadata:
  name: gpu-flavor
spec:
  nodeLabels:
    eks.amazonaws.com/instance-gpu-count: "1"
  tolerations:
  - key: nvidia.com/gpu
    operator: Equal
    value: "true"
    effect: NoSchedule
```

### ClusterQueue

Defines cluster-wide resource quotas:

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: cluster-queue
spec:
  namespaceSelector:
    matchLabels:
      kueue-enabled: "true"  # Only accept from labeled namespaces
  queueingStrategy: BestEffortFIFO
  resourceGroups:
  - coveredResources: ["cpu", "memory"]
    flavors:
    - name: default-flavor
      resources:
      - name: "cpu"
        nominalQuota: 100
      - name: "memory"
        nominalQuota: 200Gi
  - coveredResources: ["nvidia.com/gpu"]
    flavors:
    - name: gpu-flavor
      resources:
      - name: "nvidia.com/gpu"
        nominalQuota: 8
```

### LocalQueue

Namespace-scoped queue that routes jobs to a ClusterQueue:

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: LocalQueue
metadata:
  name: user-queue
  namespace: gco-jobs
spec:
  clusterQueue: cluster-queue
```

### Submitting Jobs

Add the `kueue.x-k8s.io/queue-name` label and set resource limits to your Job:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: my-job
  namespace: gco-jobs
  labels:
    kueue.x-k8s.io/queue-name: user-queue
spec:
  activeDeadlineSeconds: 3600  # Kill after 1 hour to prevent quota hogging
  template:
    spec:
      containers:
      - name: worker
        image: python:3.14-slim
        command: ["python", "-c", "print('Hello from Kueue!')"]
        resources:
          requests:
            cpu: "1"
            memory: "2Gi"
          limits:
            cpu: "2"
            memory: "4Gi"
      restartPolicy: Never
```

Kueue holds the job in a suspended state until quota is available, then unsuspends it for scheduling. Always set `limits` alongside `requests` — Kueue's admission control is based on `requests`, but without `limits` a job can consume far more than its quota allocation.

## Run the Example

The `gco-jobs` namespace is created automatically by GCO during stack deployment.

```bash
# Apply queues and example jobs
kubectl apply -f examples/kueue-job.yaml

# Check queue status
kubectl get clusterqueue
kubectl get localqueue -n gco-jobs

# Check workload admission
kubectl get workloads -n gco-jobs

# Watch jobs
kubectl get jobs -n gco-jobs -w
```

## Priority and Preemption

Kueue supports priority-based admission and preemption. Higher-priority jobs can preempt lower-priority ones to reclaim quota.

### Create priority classes

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: WorkloadPriorityClass
metadata:
  name: high-priority
value: 1000
description: "Critical training jobs"

---
apiVersion: kueue.x-k8s.io/v1beta1
kind: WorkloadPriorityClass
metadata:
  name: low-priority
value: 100
description: "Exploratory or dev jobs"
```

### Submit a prioritized job

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: critical-training
  namespace: gco-jobs
  labels:
    kueue.x-k8s.io/queue-name: user-queue
    kueue.x-k8s.io/priority-class: high-priority
spec:
  template:
    spec:
      containers:
      - name: trainer
        image: python:3.14-slim
        command: ["python", "-c", "print('High priority job')"]
        resources:
          requests:
            cpu: "4"
            nvidia.com/gpu: "1"
          limits:
            cpu: "8"
            nvidia.com/gpu: "1"
      restartPolicy: Never
```

### Configure preemption on ClusterQueues

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: team-queue
spec:
  preemption:
    reclaimWithinCohort: Any          # Reclaim borrowed resources from other queues
    borrowWithinCohort:
      policy: LowerPriority           # Only preempt lower-priority workloads
      maxPriorityThreshold: 500       # Don't preempt workloads above this priority
    withinClusterQueue: LowerPriority # Preempt lower-priority jobs in same queue
  # ... resource groups
```

## Multi-Team Quotas

Create separate ClusterQueues for different teams sharing a cohort:

```yaml
# Team A: 4 GPUs guaranteed, can borrow up to 4 more when Team B is idle
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: team-a-queue
spec:
  cohort: shared-gpus
  preemption:
    reclaimWithinCohort: Any
    withinClusterQueue: LowerPriority
  resourceGroups:
  - coveredResources: ["nvidia.com/gpu"]
    flavors:
    - name: gpu-flavor
      resources:
      - name: "nvidia.com/gpu"
        nominalQuota: 4       # Guaranteed 4 GPUs
        borrowingLimit: 4     # Can use up to 8 total when others are idle
        lendingLimit: 2       # Only lend 2 of our 4 GPUs (keep 2 guaranteed)

---
# Team B: 4 GPUs guaranteed, can borrow up to 4 more when Team A is idle
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: team-b-queue
spec:
  cohort: shared-gpus
  preemption:
    reclaimWithinCohort: Any
    withinClusterQueue: LowerPriority
  resourceGroups:
  - coveredResources: ["nvidia.com/gpu"]
    flavors:
    - name: gpu-flavor
      resources:
      - name: "nvidia.com/gpu"
        nominalQuota: 4
        borrowingLimit: 4
        lendingLimit: 2
```

**How borrowing works:** The total physical GPUs in this cohort is 8 (4 + 4). When both teams are active, each gets their guaranteed 4. When Team B is idle, Team A can borrow up to `lendingLimit` (2) of Team B's GPUs, for a total of 6. `borrowingLimit` caps how much a team can borrow — it doesn't guarantee that capacity is available. The actual borrowable amount is constrained by what other queues in the cohort are willing to lend.

## KubeRay Integration

To route Ray workloads through Kueue queues, add the queue label to your RayJob:

```yaml
apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: ray-training
  namespace: gco-jobs
  labels:
    kueue.x-k8s.io/queue-name: user-queue
spec:
  entrypoint: "python train.py"
  rayClusterSpec:
    # ... cluster spec
  shutdownAfterJobFinishes: true
  suspend: true  # Let Kueue control when the job starts
```

Kueue will hold the RayJob in suspended state until quota is available, then unsuspend it. The [KubeRay](https://docs.ray.io/en/latest/cluster/kubernetes/index.html) operator then creates the Ray cluster and runs the job.

## Monitoring

Kueue exposes [Prometheus](https://prometheus.io/docs/introduction/overview/) metrics. Key metrics to watch:

| Metric | Description |
|--------|-------------|
| `kueue_pending_workloads` | Workloads waiting for admission |
| `kueue_admitted_active_workloads` | Currently running admitted workloads |
| `kueue_cluster_queue_resource_usage` | Resource consumption per ClusterQueue |
| `kueue_cluster_queue_nominal_quota` | Configured quota per ClusterQueue |
| `kueue_admission_wait_time_seconds` | Time workloads spend waiting for admission |

Access metrics via the controller manager's metrics endpoint:

```bash
kubectl port-forward -n kueue-system svc/kueue-controller-manager-metrics-service 8443:8443
```

## Security

### Namespace Restrictions

Use `namespaceSelector` on ClusterQueues to control which namespaces can submit jobs:

```yaml
spec:
  namespaceSelector:
    matchLabels:
      kueue-enabled: "true"
```

Then label allowed namespaces:

```bash
kubectl label namespace gco-jobs kueue-enabled=true
```

### RBAC for LocalQueue Creation

Restrict who can create LocalQueues (which connect namespaces to ClusterQueues):

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kueue-queue-admin
rules:
- apiGroups: ["kueue.x-k8s.io"]
  resources: ["localqueues"]
  verbs: ["create", "delete", "patch"]
```

Only platform admins should be able to create LocalQueues. Regular users only need permission to create Jobs with the `kueue.x-k8s.io/queue-name` label.

## Customization

Edit `lambda/helm-installer/charts.yaml` under `kueue`:

```yaml
kueue:
  enabled: true   # Set to false to disable
  version: "0.17.0"
  values:
    enablePlainPod: true  # Manage standalone pods (useful for Ray, inference servers)
```

## Cleanup

```bash
kubectl delete -f examples/kueue-job.yaml
```

## Further Reading

- [Kueue Documentation](https://kueue.sigs.k8s.io/docs/)
- [Concepts](https://kueue.sigs.k8s.io/docs/concepts/)
- [Run a Job](https://kueue.sigs.k8s.io/docs/tasks/run/jobs/)
- [Preemption](https://kueue.sigs.k8s.io/docs/concepts/preemption/)
- [Cohort Borrowing](https://kueue.sigs.k8s.io/docs/concepts/cluster_queue/#cohort)
- [WorkloadPriorityClass](https://kueue.sigs.k8s.io/docs/concepts/workload_priority_class/)
- [KubeRay Integration](https://kueue.sigs.k8s.io/docs/tasks/run/rayjobs/)
