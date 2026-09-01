# KEDA Integration

GCO includes [KEDA](https://keda.sh/) (Kubernetes Event-Driven Autoscaling) for scaling workloads based on external event sources. KEDA is a mandatory platform component (it cannot be disabled) and powers GCO's built-in SQS queue processor as well as GPU-based inference autoscaling.

## Overview

KEDA extends Kubernetes with event-driven autoscaling. It can scale Deployments, Jobs, and custom resources from zero to N based on metrics from external systems like SQS, Kafka, [Prometheus](https://prometheus.io/docs/introduction/overview/), CloudWatch, and 60+ other sources.

**When to use KEDA:**

- Scale-to-zero workloads that should only run when there's work to do
- SQS-triggered job processing (GCO's built-in queue processor uses this)
- Autoscaling based on custom metrics (Prometheus, CloudWatch, Datadog)
- Event-driven architectures where workloads respond to external signals
- Cost optimization by scaling GPU workloads to zero when idle

## What Gets Deployed

KEDA is installed via Helm chart in the `keda` namespace:

| Component | Description |
|-----------|-------------|
| keda-operator | Watches ScaledObject/ScaledJob CRDs and manages HPA/Jobs |
| keda-metrics-apiserver | Exposes external metrics to the Kubernetes metrics API |
| keda-admission-webhooks | Validates KEDA custom resources |

The KEDA operator has an [IRSA](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html) role with permissions to read SQS queue metrics for the GCO job queue.

## How GCO Uses KEDA

GCO's built-in SQS queue processor (`lambda/kubectl-applier-simple/manifests/post-helm-sqs-consumer.yaml`) is a KEDA ScaledJob:

```text
User runs: gco jobs submit-sqs manifest.yaml --region us-east-1
  → Manifest sent to regional SQS queue
  → KEDA detects message, spins up consumer pod
  → Consumer applies manifest to cluster
  → Message deleted, pod terminates
  → KEDA scales back to zero when queue is empty
```

This is configured automatically — no user setup needed.

## Key Concepts

### ScaledObject

Scales a Deployment based on an external metric:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: my-scaler
  namespace: gco-jobs
spec:
  scaleTargetRef:
    name: my-deployment
  minReplicaCount: 0
  maxReplicaCount: 10
  triggers:
  - type: prometheus
    metadata:
      serverAddress: http://prometheus:9090
      metricName: http_requests_total
      query: sum(rate(http_requests_total[2m]))
      threshold: '100'
```

### ScaledJob

Creates Kubernetes Jobs in response to events (scale-to-zero capable). For SQS, `queueLength` is KEDA's target number of queued messages per active Job; it does **not** mean one worker will receive or drain that many messages:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledJob
metadata:
  name: sqs-processor
  namespace: gco-jobs
spec:
  jobTargetRef:
    template:
      spec:
        containers:
        - name: processor
          image: my-processor:latest
        restartPolicy: Never
  pollingInterval: 10
  maxReplicaCount: 20
  triggers:
  - type: aws-sqs-queue
    metadata:
      queueURL: "https://sqs.us-east-1.amazonaws.com/123456789/my-queue"
      queueLength: "5"
      awsRegion: "us-east-1"
      identityOwner: operator
```

### Common Triggers

| Trigger | Use Case |
|---------|----------|
| `aws-sqs-queue` | Process messages from SQS (used by GCO queue processor) |
| `prometheus` | Scale based on Prometheus metrics |
| `aws-cloudwatch` | Scale based on CloudWatch metrics |
| `cron` | Time-based scaling (e.g., scale up during business hours) |
| `kafka` | Process Kafka topic messages |
| `cpu` / `memory` | Scale based on resource utilization |

## Run the Safe Scaling Example

The shipped example is deliberately a **non-consuming scaler demonstration**, not a queue worker. Its credentialless pod only logs that KEDA created a Job; it contains no SQS receive/delete code and never drains messages.

1. Create a disposable demo queue and synthetic messages.
2. Grant the KEDA operator read-only queue-metric access to that demo queue.
3. Replace the obvious demo URL/region in the manifest, apply it, and observe scaling:

```bash
kubectl apply -f examples/keda-scaled-job.yaml
kubectl get scaledjob sqs-scaling-observer -n gco-jobs
kubectl get pods -n gco-jobs -l app=sqs-scaling-observer
```

> **Do not point this example at GCO's `JobQueueUrl`.** The example cannot consume or delete messages, so a real submission would remain queued and repeatedly trigger observer Jobs. Use GCO's built-in queue processor for real manifest submissions, or implement a custom consumer that validates and processes a message before acknowledging it.

## Scale Inference on GPU Utilization

GPU utilization is not a native Kubernetes HPA metric (Resource metrics are limited to CPU and memory). GCO routes GPU-based inference autoscaling through KEDA's `aws-cloudwatch` scaler, which reads the per-pod GPU metrics the `amazon-cloudwatch-observability` agent publishes to CloudWatch ContainerInsights.

When you deploy an endpoint with a GPU autoscaling metric:

```bash
gco inference deploy my-llm \
  -i vllm/vllm-openai:v0.28.0 \
  --replicas 2 --gpu-count 1 \
  --min-replicas 1 --max-replicas 8 \
  --autoscale-metric gpu:60
```

the inference monitor materializes a `ScaledObject` equivalent to:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: my-llm
  namespace: gco-inference
spec:
  scaleTargetRef:
    name: my-llm
  minReplicaCount: 1
  maxReplicaCount: 8
  triggers:
  - type: aws-cloudwatch
    metadata:
      namespace: ContainerInsights
      metricName: pod_gpu_utilization
      dimensionName: ClusterName;Namespace;PodName
      dimensionValue: <cluster>;gco-inference;my-llm
      targetMetricValue: "60"
      minMetricValue: "0"
      metricStat: Average
      awsRegion: <region>
      identityOwner: operator
```

CPU/memory targets supplied alongside a GPU metric become native `cpu`/`memory` triggers on the same `ScaledObject`, so an endpoint can scale on a mix of signals. The same path backs Mooncake per-role autoscaling (`--mooncake-autoscale`), where each role's `{name}-{role}` Deployment is targeted independently.

The KEDA operator's IRSA role is granted read-only CloudWatch access (`cloudwatch:GetMetricData`, `cloudwatch:GetMetricStatistics`, `cloudwatch:ListMetrics`) so the scaler can read these metrics. CloudWatch metric-read APIs do not support resource-level IAM scoping.

## Scale Inference to Zero

KEDA can scale inference endpoints to zero when there's no traffic, saving GPU costs:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: inference-scaler
  namespace: gco-inference
spec:
  scaleTargetRef:
    name: my-llm-deployment
  minReplicaCount: 0
  maxReplicaCount: 4
  cooldownPeriod: 300          # Wait 5 min before scaling down
  triggers:
  - type: prometheus
    metadata:
      serverAddress: http://prometheus:9090
      metricName: inference_requests
      query: sum(rate(http_requests_total{service="my-llm"}[5m]))
      threshold: '1'           # Scale up on any traffic
```

## Security

### RBAC for ScaledObject/ScaledJob Creation

Anyone who can create a `ScaledObject` or `ScaledJob` in a namespace can trigger arbitrary scaling — including scaling Deployments to hundreds of replicas or creating Jobs that consume GPU resources. Restrict creation to platform admins:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: keda-user
rules:
- apiGroups: ["keda.sh"]
  resources: ["scaledobjects", "scaledjobs", "triggerauthentications"]
  verbs: ["create", "get", "list", "delete"]
```

### IRSA Permissions

The KEDA operator has an IRSA role with permissions to read SQS queue metrics for the GCO job queue (scoped to the specific queue ARNs) and read-only CloudWatch access (`GetMetricData`, `GetMetricStatistics`, `ListMetrics`) for GPU-based inference autoscaling. The CloudWatch metric-read APIs do not support resource-level scoping, so they are granted account-wide but remain read-only.

If you add custom triggers that access other AWS services (e.g., Kinesis, DynamoDB), you'll need to extend the IRSA role or create a separate `TriggerAuthentication` with its own credentials.

### Namespace Restrictions

The KEDA operator watches all namespaces by default (`watchNamespace: ""`). To restrict it to specific namespaces:

```yaml
keda:
  values:
    watchNamespace: "gco-jobs"  # Only watch gco-jobs namespace
```

This prevents users in other namespaces from creating ScaledObjects that trigger scaling.

## Customization

Edit `lambda/helm-installer/charts.yaml` under `keda`:

```yaml
keda:
  enabled: true   # IGNORED — KEDA is a mandatory platform component
  version: "2.19.0"
  values:
    watchNamespace: ""  # Watch all namespaces
```

KEDA cannot be disabled: it backs the built-in SQS queue processor and is the only metrics bridge for GPU/CloudWatch-driven autoscaling. The `enabled` toggle is retained for chart-config uniformity but is ignored for KEDA — the chart always installs.

To disable the built-in SQS consumer while keeping KEDA, edit `cdk.json`:

```json
"queue_processor": {
  "enabled": false
}
```

## Cleanup

```bash
kubectl delete scaledjob sqs-scaling-observer -n gco-jobs
```

## Further Reading

- [KEDA Documentation](https://keda.sh/docs/)
- [Scalers](https://keda.sh/docs/latest/scalers/) — full list of 60+ event sources
- [AWS SQS Scaler](https://keda.sh/docs/latest/scalers/aws-sqs/)
- [ScaledJob](https://keda.sh/docs/latest/concepts/scaling-jobs/)
- [Authentication](https://keda.sh/docs/latest/concepts/authentication/)
