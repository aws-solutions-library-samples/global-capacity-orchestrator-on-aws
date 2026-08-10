# Slurm Operator (Slinky) Integration

GCO deploys a ready-to-use Slurm cluster on Kubernetes using the [Slinky Slurm Operator](https://github.com/SlinkyProject/slurm-operator) by SchedMD. Slurm is opt-in — enable it in `cdk.json` to deploy.

## Overview

The Slinky project bridges Slurm and Kubernetes, letting you run `sbatch`, `srun`, and `salloc` inside your EKS cluster.

**When to use Slurm on EKS:**

- Teams already familiar with Slurm workflows (sbatch, srun, salloc)
- HPC workloads that benefit from Slurm's deterministic scheduling
- Mixed environments where both Slurm and Kubernetes workloads share GPU capacity
- Organizations migrating from on-premises Slurm clusters to the cloud

## Enable Slurm

Edit `cdk.json`:

```json
{
  "context": {
    "helm": {
      "slurm": { "enabled": true }
    }
  }
}
```

Then deploy: `gco stacks deploy-all -y`

This installs three Helm charts:

1. **[cert-manager](https://cert-manager.io/docs/)** (v1.20.1) — TLS certificates (already enabled by default)
2. **slinky-slurm-operator** (v1.1.0) — Kubernetes operator for Slurm cluster CRDs
3. **slinky-slurm** (v1.1.0) — a Slurm cluster (`gco-slurm`) in `gco-jobs`

## What Gets Deployed

The default Slurm cluster (`gco-slurm`) includes:

| Component | Replicas | Image Tag | Resources (req/limit) | Description |
|-----------|----------|-----------|----------------------|-------------|
| Controller (slurmctld) | 1 | `25.11-ubuntu24.04` | 500m/1 CPU, 512Mi/1Gi | Head node — schedules jobs, manages state |
| Workers (slurmd) | 2 | `25.11-ubuntu24.04` | 1/2 CPU, 2Gi/4Gi | Execute Slurm jobs |
| REST API (slurmrestd) | 1 | `25.11-ubuntu24.04` | 100m/500m CPU, 128Mi/256Mi | HTTP API for programmatic job submission |

The `gco-jobs` namespace is created automatically by GCO during stack deployment.

**Note on HA:** The default deployment runs a single controller replica. If the controller pod restarts, Kubernetes regenerates it quickly (typically faster than Slurm's native HA failover). The Slinky operator monitors the controller and restarts it automatically. For production workloads, monitor the controller pod and enable Slurm accounting to persist job state across restarts.

```text
┌─────────────────────────────────────────────────────┐
│                    EKS Cluster                      │
│                                                     │
│  ┌──────────────┐  ┌──────────────┐                 │
│  │  Slurm       │  │  Slurm       │                 │
│  │  Operator    │  │  Webhook     │                 │
│  └──────┬───────┘  └──────────────┘                 │
│         │ manages                                   │
│  ┌──────▼────────────────────────────────────┐      │
│  │        gco-slurm (gco-jobs namespace)     │      │
│  │                                           │      │
│  │  ┌────────────┐  ┌─────────────────────┐  │      │
│  │  │ Controller │  │ Workers (NodeSet)   │  │      │
│  │  │ (slurmctld)│  │ slurmd × 2 replicas │  │      │
│  │  └────────────┘  └─────────────────────┘  │      │
│  │                   ┌─────────────────────┐ │      │
│  │                   │ REST API            │ │      │
│  │                   │ (slurmrestd)        │ │      │
│  │                   └─────────────────────┘ │      │
│  └───────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────┘
```

## Verify the Cluster

```bash
kubectl get pods -n slurm-operator
kubectl get pods -n gco-jobs -l slinky.slurm.net/cluster=gco-slurm
kubectl exec -n gco-jobs deploy/slinky-slurm-controller -- sinfo
```

## Submit Slurm Jobs

### Run the example job

```bash
kubectl apply -f examples/slurm-cluster-job.yaml
kubectl logs job/slurm-test -n gco-jobs -f
```

The gco-jobs namespace default-denies pod-to-pod traffic, so Slurm
connectivity is label-gated by the cluster-managed policies applied with the
Slurm toggle (`post-helm-slurm-network.yaml`): Slinky's own pods
(`app.kubernetes.io/part-of: slurm`) may talk to each other, and any pod
labeled `gco.aws/slurm-client: "true"` — like the example job — may reach the
REST API on port 6820. A job pod without that label cannot reach slurmrestd.

### Interactive access via the controller pod

The login set is disabled by default to avoid creating an unnecessary NLB.
Use `kubectl exec` to get an interactive shell on the controller pod:

```bash
kubectl exec -it -n gco-jobs deploy/slinky-slurm-controller -- bash

# Inside the controller pod:
sinfo                        # View cluster and partition status
srun hostname                # Run a quick single-task test
squeue                       # Check the job queue
sacct                        # View completed jobs (requires accounting)
```

### Submit from outside the cluster via REST API

The Slurm REST API (slurmrestd) supports JWT authentication. By default, GCO does not expose the REST API externally — use `kubectl port-forward` for local access.

**Warning:** Never expose slurmrestd via a Service or Ingress without authentication. The REST API allows arbitrary job submission and cluster management.

```bash
# Port-forward the REST API (local access only)
kubectl port-forward -n gco-jobs svc/gco-slurm-restapi 6820:6820

# Discover available API versions
curl http://localhost:6820/slurm/

# Submit a job (API version must match your Slurm release)
curl -X POST http://localhost:6820/slurm/v0.0.41/job/submit \
  -H "Content-Type: application/json" \
  -d '{"job": {"name": "api-test", "ntasks": 1, "script": "#!/bin/bash\nhostname"}}'
```

The API version in the URL (e.g., `v0.0.41`) is tied to the Slurm release. Use the discovery endpoint (`/slurm/`) to find the correct version.

## GPU Workloads

Add a GPU-enabled NodeSet in `lambda/helm-installer/charts.yaml` under `slinky-slurm`:

```yaml
nodeSets:
  - name: workers
    replicas: 2
    # ... existing CPU workers
  - name: gpu-workers
    replicas: 2
    slurmd:
      image:
        repository: ghcr.io/slinkyproject/slurmd
        tag: "25.11-ubuntu24.04"
      resources:
        requests:
          cpu: "4"
          memory: "16Gi"
          nvidia.com/gpu: "1"
        limits:
          cpu: "8"
          memory: "32Gi"
          nvidia.com/gpu: "1"
    podSpec:
      tolerations:
      - key: nvidia.com/gpu
        operator: Equal
        value: "true"
        effect: NoSchedule
```

Then submit GPU jobs: `sbatch --gres=gpu:1 --wrap="nvidia-smi"`

## Autoscaling

The Slurm operator supports scaling worker NodeSets based on queue depth. Combined with [Karpenter](https://karpenter.sh/) ([EKS Auto Mode](https://docs.aws.amazon.com/eks/latest/userguide/automode.html)):

1. Jobs queue up in Slurm → operator scales up worker pods → Karpenter provisions nodes
2. Queue drains → operator scales down workers → Karpenter consolidates nodes

Configure scaling bounds on the NodeSet:

```yaml
nodeSets:
  - name: workers
    replicas: 2        # Desired count
    # minReplicas: 0   # Scale to zero when idle (requires HPA)
    # maxReplicas: 16  # Cap at 16 workers
    scalingMode: StatefulSet
```

For HPA-based autoscaling, expose Slurm metrics via the Slinky metrics exporter and configure a HorizontalPodAutoscaler targeting the NodeSet.

## Coexistence with Kueue and YuniKorn

GCO deploys Slurm alongside [Kueue](https://kueue.sigs.k8s.io/), [Volcano](https://volcano.sh/), and [YuniKorn](https://yunikorn.apache.org/). They operate independently:

- **Slurm** manages its own worker pods and job queue. Slurm jobs run inside slurmd pods, not as standalone Kubernetes Jobs.
- **Kueue** manages Kubernetes-native Jobs via admission control. Kueue does not manage Slurm worker pods.
- **Volcano** schedules pods with `schedulerName: volcano`. Slurm pods use the default scheduler.
- **YuniKorn** schedules pods without an explicit `schedulerName`. Slurm worker pods are managed by the Slinky operator.

**GPU isolation:** By default, Slurm workers don't request GPUs (CPU-only). GPU resources are available to Kueue/Volcano/YuniKorn-managed jobs. If you add GPU NodeSets to Slurm, those GPUs are reserved by the slurmd pods. See [SCHEDULERS.md](SCHEDULERS.md) for the full coexistence guide.

## Monitoring

### From inside the cluster

```bash
kubectl exec -n gco-jobs deploy/slinky-slurm-controller -- sinfo
kubectl exec -n gco-jobs deploy/slinky-slurm-controller -- squeue
kubectl exec -n gco-jobs deploy/slinky-slurm-controller -- scontrol show job
```

### Prometheus metrics

The Slinky metrics exporter collects Slurm telemetry from the REST API. Key metrics:

| Metric | Description |
|--------|-------------|
| `slurm_queue_pending` | Pending jobs per partition |
| `slurm_node_state` | Node states (idle, allocated, down, drain) |
| `slurm_job_state` | Job states (pending, running, completed, failed) |

### Pod-level monitoring

```bash
kubectl get pods -n gco-jobs -l slinky.slurm.net/cluster=gco-slurm -w
kubectl describe pod -n gco-jobs -l slinky.slurm.net/component=controller
```

## Security

### Controller Pod Access

Restrict who can `kubectl exec` into the controller pod using Kubernetes RBAC:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: slurm-user
  namespace: gco-jobs
rules:
- apiGroups: [""]
  resources: ["pods/exec"]
  verbs: ["create"]
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list"]
```

### REST API Authentication

For production, enable JWT authentication on slurmrestd by adding `AuthType=auth/jwt` to the Slurm configuration. Generate tokens with `scontrol token` from the controller pod.

### Network Policies

GCO's default network policies in `gco-jobs` allow egress to AWS APIs and HTTPS. Slurm components communicate on ports 6817 (slurmctld), 6818 (slurmd), 6819 (slurmdbd), and 6820 (slurmrestd).

## Accounting

Enable Slurm accounting to persist job history across controller restarts:

```yaml
slinky-slurm:
  values:
    accounting:
      enabled: true
      storageConfig:
        host: mariadb
        database: slurm_acct_db
        username: slurm
        passwordKeyRef:
          name: mariadb-password
          key: password
```

This deploys MariaDB + slurmdbd. The Helm chart creates a PVC for MariaDB data. Default password is in a Kubernetes Secret — change it for production.

Query accounting: `kubectl exec -n gco-jobs deploy/slinky-slurm-controller -- sacct --format=JobID,JobName,State,Elapsed,MaxRSS`

## Customization

### Scaling workers

```yaml
# lambda/helm-installer/charts.yaml under slinky-slurm
nodeSets:
  - name: workers
    replicas: 8  # Scale up from 2 to 8
```

### Disable the Slurm cluster

```yaml
slinky-slurm:
  enabled: false
```

### cert-manager Compatibility

GCO installs cert-manager v1.20.1. If your cluster already has cert-manager, disable the bundled one:

```yaml
cert-manager:
  enabled: false
```

Requires cert-manager v1.12+.

## Configuration Reference

### Operator Helm values (`slinky-slurm-operator`)

| Value | Default | Description |
|-------|---------|-------------|
| `operator.replicas` | 1 | Operator pod replicas |
| `operator.image.tag` | v1.1.0 | Operator image version |
| `webhook.enabled` | true | Enable admission webhook |
| `certManager.enabled` | true | Use cert-manager for TLS |
| `crds.enabled` | true | Let chart manage CRDs |

### Cluster Helm values (`slinky-slurm`)

| Value | Default | Description |
|-------|---------|-------------|
| `clusterName` | gco-slurm | Slurm cluster name |
| `controller.slurmctld.image.tag` | 25.11-ubuntu24.04 | Controller image |
| `nodeSets[].replicas` | 2 | Worker pods per NodeSet |
| `nodeSets[].slurmd.image.tag` | 25.11-ubuntu24.04 | Worker image |
| `loginsets.slinky.enabled` | false | Login set (creates NLB — disabled by default) |
| `restapi.slurmrestd.image.tag` | 25.11-ubuntu24.04 | REST API image |
| `accounting.enabled` | false | Enable job accounting (MariaDB) |

## Further Reading

- [Slinky Project](https://github.com/SlinkyProject)
- [Slurm Documentation](https://slurm.schedmd.com/documentation.html)
- [Slurm REST API](https://slurm.schedmd.com/rest_api.html)
- [Running Slurm on Amazon EKS with Slinky](https://aws.amazon.com/blogs/containers/running-slurm-on-amazon-eks-with-slinky/) (AWS blog)
- [Slurm on EKS Blueprint](https://awslabs.github.io/ai-on-eks/docs/blueprints/training/GPUs/slinky-slurm)
