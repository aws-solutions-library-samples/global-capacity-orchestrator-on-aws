# Apache YuniKorn Integration

GCO includes [Apache YuniKorn](https://yunikorn.apache.org/) as a scheduler for multi-tenant AI/ML clusters. YuniKorn is opt-in — enable it in `cdk.json` to deploy.

## Overview

YuniKorn runs as a secondary scheduler alongside the default kube-scheduler. Pods must explicitly set `schedulerName: yunikorn` to be scheduled by YuniKorn — there is no auto-injection. This means YuniKorn coexists cleanly with Volcano, Kueue, and the default scheduler without interfering with system pods or other schedulers.

**When to use YuniKorn:**

- Multi-tenant clusters with competing teams needing guaranteed resource access
- Hierarchical resource quota management (org → team → project)
- Fair-sharing across queues with preemption for high-priority jobs
- Gang scheduling for distributed training (all-or-nothing pod placement)
- Organizations that need queue-based scheduling similar to YARN or Slurm partitions

## What Gets Deployed

YuniKorn is installed via Helm chart in the `yunikorn` namespace:

| Component | Description |
|-----------|-------------|
| yunikorn-scheduler | App-aware scheduler with queue management |
| yunikorn-web | Web UI for queue and application monitoring |

The admission controller is disabled by default to prevent interference with other GCO schedulers. Pods must explicitly set `schedulerName: yunikorn` to use YuniKorn.

The `gco-jobs` namespace is created automatically by GCO during stack deployment.

## How It Works

```text
┌──────────────────────────────────────────────────────┐
│                    EKS Cluster                       │
│                                                      │
│  ┌────────────────────────────────────────────────┐  │
│  │              YuniKorn Scheduler                │  │
│  │                                                │  │
│  │  root queue                                    │  │
│  │  ├── gpu-workloads (guaranteed: 32 CPU, 64G)   │  │
│  │  ├── cpu-workloads (guaranteed: 16 CPU, 32G)   │  │
│  │  └── default      (max: 32 CPU, 64G)           │  │
│  └────────────────────────────────────────────────┘  │
│                                                      │
│  ┌──────────────┐  ┌──────────────┐                  │
│  │ Admission    │  │ YuniKorn     │                  │
│  │ Controller   │  │ Web UI       │                  │
│  └──────────────┘  └──────────────┘                  │
└──────────────────────────────────────────────────────┘
```

YuniKorn uses annotations on pods to determine queue placement and scheduling behavior:

- `yunikorn.apache.org/app-id` — groups pods into an application
- `yunikorn.apache.org/queue` — places the app in a specific queue
- `yunikorn.apache.org/task-groups` — enables gang scheduling

## Enable YuniKorn

Edit `cdk.json`:

```json
{
  "context": {
    "helm": {
      "yunikorn": { "enabled": true }
    }
  }
}
```

Then deploy: `gco stacks deploy-all -y`

### Verify

```bash
kubectl get pods -n yunikorn
kubectl get svc -n yunikorn
```

### 4. Access the Web UI

```bash
kubectl port-forward svc/yunikorn-service -n yunikorn 9889:9889
```

Open <http://localhost:9889> to view queues, applications, and nodes.

**Warning:** The Web UI shows queue configurations, application details, node allocations, and resource usage. Do not expose it via a Service or Ingress without authentication. Use `kubectl port-forward` for local access, or place an OAuth proxy in front of it for shared access.

## Queue Configuration

The default queue configuration is set in `lambda/helm-installer/charts.yaml` under the yunikornDefaults.queues.yaml key.

### Resource Units

YuniKorn uses `vcore` for CPU resources in queue configuration. 1 vcore = 1 Kubernetes CPU core. This is a convention inherited from YARN. In pod specs, you still use the standard Kubernetes `cpu` field.

| Queue Config | Kubernetes Equivalent |
|-------------|----------------------|
| `vcore: 32` | `cpu: "32"` (32 cores) |
| `memory: 64G` | `memory: "64Gi"` |

### Placement Rules

The default config includes:

```yaml
placementrules:
  - name: tag
    value: namespace
    create: true
```

This means:

- Pods are routed to a queue matching their namespace name (e.g., pods in `gco-jobs` go to `root.gco-jobs`)
- `create: true` auto-creates child queues under `root` for namespaces that don't have an explicit queue
- Auto-created queues inherit the parent's ACLs and have no resource limits by default

For production, disable `create: true` and define explicit queues for each namespace to maintain resource control.

### Example: Team-based queues

```yaml
yunikornDefaults:
  queues.yaml: |
    partitions:
      - name: default
        placementrules:
          - name: tag
            value: namespace
            create: false  # Don't auto-create queues
        queues:
          - name: root
            submitacl: '*'
            queues:
              - name: team-a
                submitacl: 'team-a-group'
                resources:
                  guaranteed:
                    memory: 128G
                    vcore: 64
                    nvidia.com/gpu: 4
                  max:
                    memory: 256G
                    vcore: 128
                    nvidia.com/gpu: 8
              - name: team-b
                submitacl: 'team-b-group'
                resources:
                  guaranteed:
                    memory: 64G
                    vcore: 32
                    nvidia.com/gpu: 2
                  max:
                    memory: 128G
                    vcore: 64
                    nvidia.com/gpu: 4
```

### Queue properties

| Property | Description |
|----------|-------------|
| `guaranteed` | Minimum resources reserved for this queue |
| `max` | Maximum resources the queue can use |
| `submitacl` | Who can submit to this queue (`*` = everyone, or group/user names) |
| `properties.preemption.policy` | Preemption behavior (see below) |

### Preemption Policies

| Policy | Description |
|--------|-------------|
| `default` | Standard preemption — higher-priority apps can preempt lower-priority ones across queues |
| `fence` | Prevents preemption across queue boundaries — protects guaranteed resources in multi-tenant setups. Use this for teams that need hard isolation. |
| `disabled` | No preemption — jobs wait until resources are naturally freed |

## Submit Jobs to YuniKorn

### Basic job

Add the `schedulerName` and YuniKorn annotations to your pod spec:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: my-job
  namespace: gco-jobs
  annotations:
    yunikorn.apache.org/app-id: my-job
    yunikorn.apache.org/queue: root.gpu-workloads
spec:
  activeDeadlineSeconds: 3600
  template:
    metadata:
      annotations:
        yunikorn.apache.org/app-id: my-job
        yunikorn.apache.org/queue: root.gpu-workloads
    spec:
      schedulerName: yunikorn
      containers:
      - name: worker
        image: python:3.14-slim
        command: ["python", "-c", "print('Hello from YuniKorn!')"]
        resources:
          requests:
            cpu: "1"
            memory: "2Gi"
          limits:
            cpu: "2"
            memory: "4Gi"
      restartPolicy: Never
```

### Gang scheduling

Gang scheduling ensures all pods in a group are scheduled together or not at all — critical for distributed training where partial placement wastes resources.

```yaml
metadata:
  annotations:
    yunikorn.apache.org/task-group-name: training-group
    yunikorn.apache.org/task-groups: |
      [{
        "name": "training-group",
        "minMember": 4,
        "minResource": {
          "cpu": "4",
          "memory": "8Gi",
          "nvidia.com/gpu": "1"
        }
      }]
    yunikorn.apache.org/schedulingPolicyParameters: "placeholderTimeoutInSeconds=300"
```

**Placeholder timeout:** When `placeholderTimeoutInSeconds` expires (300s = 5 minutes), YuniKorn releases the placeholder pods and the application moves to a failed state. The job does not automatically retry — you need to resubmit it or use a Kubernetes Job with `backoffLimit`.

**Deadlock warning:** If two gang-scheduled jobs each need 4 GPUs but only 6 are available, neither can start. Mitigations:

- Size `minMember` conservatively relative to cluster capacity
- Use priority classes so higher-priority jobs can preempt lower-priority placeholders
- Set `placeholderTimeoutInSeconds` to release stuck placeholders
- Monitor pending applications via the Web UI or REST API

### Distributed Training with Checkpointing

Mount shared storage for checkpointing in gang-scheduled training jobs:

```yaml
spec:
  schedulerName: yunikorn
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

### Priority scheduling

YuniKorn respects Kubernetes PriorityClasses. Higher-priority jobs can preempt lower-priority ones (unless the queue uses `preemption.policy: fence` or `disabled`):

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: high-priority-training
value: 1000000
globalDefault: false
description: "High priority for critical training jobs"
```

## Run the Example

```bash
# Apply the example (creates 3 jobs: basic, GPU, and gang-scheduled)
kubectl apply -f examples/yunikorn-job.yaml

# Watch pods get scheduled
kubectl get pods -n gco-jobs -l app=yunikorn-demo -w

# Check application status via REST API
kubectl port-forward svc/yunikorn-service -n yunikorn 9889:9889
curl http://localhost:9889/ws/v1/apps
```

## Coexistence with Other Schedulers

GCO deploys YuniKorn alongside Volcano, Kueue, KEDA, and Slurm. They coexist because they operate at different layers:

- **YuniKorn** schedules pods that explicitly set `schedulerName: yunikorn`. It manages its own queue hierarchy and resource accounting. The admission controller is disabled, so no auto-injection occurs.
- **Volcano** schedules pods that have `schedulerName: volcano`. Volcano jobs are not affected by YuniKorn.
- **Kueue** is an admission controller, not a scheduler. It suspends/unsuspends Jobs based on quota. Once a Job is unsuspended, the pod goes to whichever scheduler is configured (`yunikorn`, `volcano`, or `default-scheduler`).
- **Slurm** runs its own scheduling inside slurmd pods. Slurm jobs don't interact with YuniKorn.
- **KEDA** creates Jobs based on external events. Those Jobs use the default scheduler unless explicitly configured otherwise.

**How it works without the admission controller:** Pods without an explicit `schedulerName` use the default kube-scheduler. Only pods that set `schedulerName: yunikorn` (via annotations in the manifest) are handled by YuniKorn. This means:

- Standard Kubernetes Jobs → default kube-scheduler
- Jobs with `schedulerName: yunikorn` → YuniKorn
- Volcano Jobs → Volcano (explicit `schedulerName: volcano`)
- Slurm worker pods → default kube-scheduler (managed by Slinky operator)
- System pods (KEDA, Kueue, cert-manager) → default kube-scheduler (no interference)

**GPU quota coordination:** If you define GPU quotas in both YuniKorn queues and Kueue ClusterQueues, ensure the total doesn't exceed physical GPU count. See [SCHEDULERS.md](SCHEDULERS.md) for the full coexistence guide.

## Monitoring

### REST API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /ws/v1/queues` | Queue hierarchy and resource usage |
| `GET /ws/v1/apps` | Application list and status |
| `GET /ws/v1/nodes` | Node capacity and allocations |
| `GET /ws/v1/clusters` | Cluster-level overview |
| `GET /ws/v1/history/apps` | Historical application data |

### Prometheus metrics

YuniKorn exposes Prometheus metrics at `:9090/ws/v1/metrics`. Key metrics:

| Metric | Description |
|--------|-------------|
| `yunikorn_scheduler_queue_app_count` | Apps per queue |
| `yunikorn_scheduler_queue_resource_usage` | Resource consumption per queue |
| `yunikorn_scheduler_scheduling_latency` | Scheduling decision latency |
| `yunikorn_scheduler_queue_pending_resource` | Pending resource requests per queue |

## Security

### Queue Access Control

Restrict who can submit to each queue using `submitacl`:

```yaml
queues:
  - name: team-a
    submitacl: 'team-a-group'  # Only team-a-group members can submit
  - name: shared
    submitacl: '*'             # Anyone can submit
```

For production, avoid `submitacl: '*'` on queues with GPU resources. Restrict access to specific groups or users.

### RBAC

YuniKorn queue configuration is managed via the Helm chart values (ConfigMap). Restrict who can modify the ConfigMap:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: yunikorn-config-admin
  namespace: yunikorn
rules:
- apiGroups: [""]
  resources: ["configmaps"]
  resourceNames: ["yunikorn-defaults"]
  verbs: ["update", "patch"]
```

### Network Policies

YuniKorn components communicate within the `yunikorn` namespace. The scheduler needs access to the Kubernetes API server. No special network policies are needed beyond GCO's defaults.

## Customization

Edit `lambda/helm-installer/charts.yaml` under `yunikorn`:

```yaml
yunikorn:
  enabled: true   # Set to false to disable
  version: "1.8.0"
```

## Cleanup

```bash
kubectl delete -f examples/yunikorn-job.yaml
```

## Further Reading

- [Apache YuniKorn Documentation](https://yunikorn.apache.org/docs/)
- [YuniKorn Design Architecture](https://yunikorn.apache.org/docs/design/architecture)
- [Queue Configuration Guide](https://yunikorn.apache.org/docs/user_guide/queue_config)
- [Gang Scheduling](https://yunikorn.apache.org/docs/user_guide/gang_scheduling)
- [Resource Quota Management](https://yunikorn.apache.org/docs/user_guide/resource_quota_management)
- [Placement Rules](https://yunikorn.apache.org/docs/user_guide/placement_rules)
- [Preemption](https://yunikorn.apache.org/docs/design/preemption)
