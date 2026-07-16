# KubeRay Integration

GCO includes the [KubeRay Operator](https://ray-project.github.io/kuberay/) for running [Ray](https://www.ray.io/) distributed computing workloads on Kubernetes. KubeRay is enabled by default.

## Overview

Ray is a framework for distributed computing that handles training, hyperparameter tuning, reinforcement learning, and model serving. KubeRay manages Ray clusters as Kubernetes custom resources, handling scaling, fault tolerance, and lifecycle management.

**When to use KubeRay:**

- Distributed training with Ray Train (PyTorch, TensorFlow, XGBoost)
- Hyperparameter tuning with Ray Tune
- Model serving with Ray Serve
- Data processing with Ray Data
- Reinforcement learning with Ray RLlib
- Any workload that benefits from Ray's actor-based distributed computing model

## What Gets Deployed

KubeRay operator is installed via Helm chart in the `ray-system` namespace:

| Component | Description |
|-----------|-------------|
| kuberay-operator | Manages RayCluster, RayJob, and RayService CRDs |

The operator watches all namespaces for Ray custom resources.

### Version Compatibility

KubeRay 1.6.x supports current Ray releases. GCO's examples use Ray 2.56.0; keep
`spec.rayVersion` and every head/worker image tag identical when upgrading.

See the [KubeRay upgrade guide](https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/upgrade-guide.html) for compatibility details and known-bad Ray release ranges.

## Key Concepts

### RayCluster

A long-running Ray cluster with a head node and autoscaling worker groups:

```yaml
apiVersion: ray.io/v1
kind: RayCluster
metadata:
  name: my-cluster
  namespace: gco-jobs
spec:
  rayVersion: '2.56.0'
  headGroupSpec:
    rayStartParams:
      dashboard-host: '0.0.0.0'
      num-cpus: '0'  # Head node doesn't run tasks
    template:
      spec:
        containers:
        - name: ray-head
          image: rayproject/ray:2.56.0
          resources:
            requests:
              cpu: "2"
              memory: "4Gi"
            limits:
              cpu: "4"
              memory: "8Gi"
  workerGroupSpecs:
  - groupName: cpu-workers
    replicas: 2
    minReplicas: 0
    maxReplicas: 8
    rayStartParams:
      num-cpus: '2'
    template:
      spec:
        containers:
        - name: ray-worker
          image: rayproject/ray:2.56.0
          resources:
            requests:
              cpu: "2"
              memory: "4Gi"
            limits:
              cpu: "4"
              memory: "8Gi"
  - groupName: gpu-workers
    replicas: 0
    minReplicas: 0
    maxReplicas: 4
    rayStartParams:
      num-gpus: '1'
    template:
      spec:
        containers:
        - name: ray-worker
          image: rayproject/ray:2.56.0
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

### RayJob

A one-shot job that creates a Ray cluster, runs a script, and tears down:

```yaml
apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: training-job
  namespace: gco-jobs
spec:
  entrypoint: "python /home/ray/train.py"
  runtimeEnvYAML: |
    pip:
      - torch
      - transformers
  activeDeadlineSeconds: 3600  # Kill after 1 hour if still running
  rayClusterSpec:
    # ... same as RayCluster spec
  shutdownAfterJobFinishes: true
  ttlSecondsAfterFinished: 600  # Keep for 10 min after completion for log inspection
```

Note: `ttlSecondsAfterFinished` applies to both success and failure. If you need more time to debug failed jobs, increase this value or set `shutdownAfterJobFinishes: false` and clean up manually.

### RayService

A long-running serving deployment with automatic scaling and zero-downtime upgrades:

```yaml
apiVersion: ray.io/v1
kind: RayService
metadata:
  name: my-model
  namespace: gco-inference
spec:
  serveConfigV2: |
    applications:
      - name: my-app
        import_path: serve_model:app
        deployments:
          - name: Model
            num_replicas: 2
            ray_actor_options:
              num_gpus: 1
  rayClusterSpec:
    # ... cluster spec
```

## Run the Example

```bash
# Create a Ray cluster
kubectl apply -f examples/ray-cluster.yaml

# Wait for it to be ready
kubectl get raycluster -n gco-jobs -w

# Port-forward to the Ray dashboard
kubectl port-forward svc/ray-cluster-head-svc 8265:8265 -n gco-jobs

# Open http://localhost:8265 in your browser

# Submit a job via the Ray CLI
ray job submit --address http://localhost:8265 -- \
  python -c "import ray; ray.init(); print(ray.cluster_resources())"
```

## Distributed Training with Checkpointing

For long-running training jobs, mount shared storage (EFS or FSx) for checkpointing so progress survives node failures and spot interruptions:

```yaml
workerGroupSpecs:
- groupName: gpu-workers
  replicas: 4
  template:
    spec:
      containers:
      - name: ray-worker
        image: rayproject/ray:2.56.0
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
        - name: shared-storage
          mountPath: /checkpoints
      tolerations:
      - key: nvidia.com/gpu
        operator: Equal
        value: "true"
        effect: NoSchedule
      volumes:
      - name: shared-storage
        persistentVolumeClaim:
          claimName: gco-shared-storage  # EFS PVC (available by default)
```

In your training script, use Ray Train's checkpoint API:

```python
from ray.train import Checkpoint
# Save checkpoint to shared storage
checkpoint = Checkpoint.from_directory("/checkpoints/run-001")
```

## Autoscaling

KubeRay supports Ray's built-in autoscaler. Workers scale up when tasks are queued and scale down when idle:

- `minReplicas: 0` — scale to zero when no work
- `maxReplicas: 8` — cap at 8 workers
- Combined with Karpenter, new nodes are provisioned automatically when workers need to scale up

## Security

### Dashboard Access

The Ray dashboard (`dashboard-host: '0.0.0.0'`) listens on all interfaces and has no built-in authentication. It provides full cluster access including job submission and code execution.

**Do not expose the dashboard via a Service or Ingress without authentication.** Use `kubectl port-forward` for local access, or place an OAuth proxy / Istio auth policy in front of it for shared access.

To disable the dashboard entirely:

```yaml
rayStartParams:
  dashboard-host: '127.0.0.1'  # Localhost only
```

### Namespace Restrictions

The KubeRay operator watches all namespaces by default. To restrict which namespaces can create Ray clusters, use Kubernetes RBAC to limit who can create `RayCluster`, `RayJob`, and `RayService` resources:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ray-user
rules:
- apiGroups: ["ray.io"]
  resources: ["rayclusters", "rayjobs", "rayservices"]
  verbs: ["get", "list", "create", "delete"]
```

### Pod Security

Ray workers execute arbitrary user code. Apply pod security standards to limit what containers can do:

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  allowPrivilegeEscalation: false
  capabilities:
    drop: ["ALL"]
```

GCO's default network policies add an exact-label rule for the shipped `ray-cluster`: ingress and egress are allowed only between pods carrying `ray.io/cluster: ray-cluster`. Ray allocates additional worker and object-manager ports dynamically, so this peer-scoped rule intentionally does not restrict ports; it does not admit unrelated `gco-jobs` pods. If you change `metadata.name`, add an equivalent peer-scoped policy for the new `ray.io/cluster` label.

## Customization

Edit `lambda/helm-installer/charts.yaml` under `kuberay-operator`:

```yaml
kuberay-operator:
  enabled: true   # Set to false to disable
  version: "1.6.1"
  values:
    watchNamespace: ""  # Watch all namespaces (or set to "gco-jobs" to restrict)
```

## Cleanup

```bash
kubectl delete raycluster ray-cluster -n gco-jobs
```

## Further Reading

- [Ray Documentation](https://docs.ray.io/)
- [KubeRay Documentation](https://ray-project.github.io/kuberay/)
- [KubeRay Upgrade & Compatibility Guide](https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/upgrade-guide.html)
- [Ray Train (Distributed Training)](https://docs.ray.io/en/latest/train/train.html)
- [Ray Train Checkpointing](https://docs.ray.io/en/latest/train/user-guides/checkpoints.html)
- [Ray Serve (Model Serving)](https://docs.ray.io/en/latest/serve/index.html)
- [Ray Tune (Hyperparameter Tuning)](https://docs.ray.io/en/latest/tune/index.html)
