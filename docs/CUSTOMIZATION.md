# Customization Guide

This guide shows you how to customize GCO (Global Capacity Orchestrator on AWS) for your specific needs.

## Table of Contents

- [Deployment Regions](#deployment-regions)
  - [Understanding Stack Regions](#understanding-stack-regions)
  - [Configuring Deployment Regions](#configuring-deployment-regions)
  - [Environment Variables](#environment-variables)
- [Adding Regions](#adding-regions)
- [EKS Cluster Configuration](#eks-cluster-configuration)
  - [Endpoint Access Modes](#endpoint-access-modes)
  - [Configuring Endpoint Access](#configuring-endpoint-access)
  - [Job Submission with Private Endpoints](#job-submission-with-private-endpoints)
- [Configuring GPU Nodepools](#configuring-gpu-nodepools)
  - [Modify Instance Types](#modify-instance-types)
  - [Adjust GPU Limits](#adjust-gpu-limits)
  - [Configure Spot Instances](#configure-spot-instances)
  - [Add Taints for GPU Nodes](#add-taints-for-gpu-nodes)
- [Customizing Services](#customizing-services)
  - [Health Monitor](#health-monitor)
  - [Manifest Processor](#manifest-processor)
  - [Adjust Replica Counts](#adjust-replica-counts)
- [Security Policy Configuration](#security-policy-configuration)
  - [Security Policy Toggles](#security-policy-toggles)
  - [Allowed Resource Kinds](#allowed-resource-kinds)
- [Adding Kubernetes Manifests](#adding-kubernetes-manifests)
- [Modifying Network Configuration](#modifying-network-configuration)
  - [Change VPC CIDR](#change-vpc-cidr)
  - [Add VPC Endpoints](#add-vpc-endpoints)
  - [Modify Security Groups](#modify-security-groups)
- [Adjusting Resource Limits](#adjusting-resource-limits)
  - [Pod Resource Requests/Limits](#pod-resource-requestslimits)
  - [nodepool Limits](#nodepool-limits)
  - [Lambda Configuration](#lambda-configuration)
- [Enabling Additional Features](#enabling-additional-features)
- [Helm Chart Configuration](#helm-chart-configuration)
  - [Enable EKS Logging](#enable-eks-logging)
  - [Add CloudWatch Container Insights](#add-cloudwatch-container-insights)
  - [Enable AWS Load Balancer Controller](#enable-aws-load-balancer-controller)
  - [Add Prometheus Monitoring](#add-prometheus-monitoring)
- [FSx for Lustre Configuration](#fsx-for-lustre-configuration)
  - [Enable FSx](#enable-fsx)
  - [Configure FSx Storage](#configure-fsx-storage)
  - [Using FSx in Jobs](#using-fsx-in-jobs)
- [Configure Valkey Cache](#configure-valkey-cache)
  - [Using Valkey in Jobs](#using-valkey-in-jobs)
- [Configure Aurora pgvector](#configure-aurora-pgvector)
  - [Using Aurora pgvector in Jobs](#using-aurora-pgvector-in-jobs)
- [Infrastructure Version Constants](#infrastructure-version-constants)
- [CDK-nag Compliance](#cdk-nag-compliance)
  - [Enabled Frameworks](#enabled-frameworks)
  - [Customizing Suppressions](#customizing-suppressions)
  - [Adding New Suppressions](#adding-new-suppressions)
- [Configuration Best Practices](#configuration-best-practices)
- [Troubleshooting Customizations](#troubleshooting-customizations)
- [Queue Processor (SQS Consumer)](#queue-processor-sqs-consumer)
  - [Queue Processor Configuration](#configuration)
  - [Disabling the Built-In Consumer](#disabling-the-built-in-consumer)
  - [How It Works](#how-it-works)
  - [Security Parity with the REST Path](#security-parity-with-the-rest-path)
- [Cost Optimization](#cost-optimization)

## Deployment Regions

GCO deploys multiple stacks to different AWS regions. All regions are configurable via `cdk.json`.

### Understanding Stack Regions

| Stack | Default Region | Purpose |
|-------|---------------|---------|
| `gco-global` | us-east-2 | Global Accelerator, SSM parameters for cross-region coordination |
| `gco-api-gateway` | us-east-2 | Edge-optimized API Gateway with IAM authentication |
| `gco-monitoring` | us-east-2 | Cross-region CloudWatch dashboards and alarms |
| `gco-{region}` | (configurable) | Regional EKS clusters, ALBs, and workload infrastructure |

**Why separate regions?**

- Global infrastructure (API Gateway, Global Accelerator) is kept separate from workload regions
- Prevents resource conflicts and simplifies management
- Edge-optimized API Gateway uses CloudFront, so the "home" region has minimal latency impact
- Allows workload regions to be added/removed without affecting global infrastructure

### Configuring Deployment Regions

Edit `cdk.json` to customize where each stack type deploys:

```json
{
  "context": {
    "deployment_regions": {
      "global": "us-east-2",
      "api_gateway": "us-east-2",
      "monitoring": "us-east-2",
      "regional": [
        "us-east-1",
        "us-west-2",
        "eu-west-1"
      ]
    }
  }
}
```

**Configuration Options:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `global` | string | `us-east-2` | Region for Global Accelerator and SSM parameters |
| `api_gateway` | string | `us-east-2` | Region for API Gateway stack |
| `monitoring` | string | `us-east-2` | Region for Monitoring stack |
| `regional` | array | `["us-east-1"]` | Regions for EKS cluster deployment |

**Example: Deploy everything to us-west-2:**

```json
{
  "context": {
    "deployment_regions": {
      "global": "us-west-2",
      "api_gateway": "us-west-2",
      "monitoring": "us-west-2",
      "regional": ["us-west-2"]
    }
  }
}
```

**Example: Multi-region with EU compliance:**

```json
{
  "context": {
    "deployment_regions": {
      "global": "eu-west-1",
      "api_gateway": "eu-west-1",
      "monitoring": "eu-west-1",
      "regional": [
        "eu-west-1",
        "eu-central-1"
      ]
    }
  }
}
```

### Environment Variables

The CLI also supports environment variables for region configuration:

```bash
# Override API Gateway region
export GCO_API_GATEWAY_REGION=us-west-2

# Override default region for CLI commands
export GCO_DEFAULT_REGION=us-west-2

# Override global region
export GCO_GLOBAL_REGION=us-west-2

# Override monitoring region
export GCO_MONITORING_REGION=us-west-2
```

**Configuration precedence (highest to lowest):**

1. Environment variables (`GCO_*`)
2. User config file (`~/.gco/config.yaml`)
3. Project config (`cdk.json`)
4. Default values

## Adding Regions

### 1. Update CDK Configuration

Edit `cdk.json` to add new regional deployments:

```json
{
  "context": {
    "deployment_regions": {
      "global": "us-east-2",
      "api_gateway": "us-east-2",
      "monitoring": "us-east-2",
      "regional": [
        "us-east-1",
        "us-west-2",
        "eu-west-1",
        "ap-southeast-1"
      ]
    }
  }
}
```

### 2. Deploy to New Region

CDK bootstrap runs automatically during deploy if the new region hasn't been bootstrapped yet. Just deploy:

```bash
gco stacks deploy-all -y
```

If you prefer to bootstrap manually first:

```bash
gco stacks bootstrap -r ap-southeast-1
```

### 3. Verify Global Accelerator

The new region is automatically added to Global Accelerator endpoints.

```bash
aws globalaccelerator list-endpoint-groups \
  --listener-arn $(aws cloudformation describe-stacks \
    --stack-name gco-global \
    --query 'Stacks[0].Outputs[?OutputKey==`GlobalAcceleratorListenerArn`].OutputValue' \
    --output text)
```

## EKS Cluster Configuration

### Endpoint Access Modes

GCO supports two EKS API endpoint access modes:

| Mode | Security | kubectl Access | Job Submission |
|------|----------|----------------|----------------|
| `PRIVATE` (default) | Most secure | Requires VPN/bastion/SSM | Via API Gateway or SQS |
| `PUBLIC_AND_PRIVATE` | Less secure | Direct from internet | All methods |

**Recommendation:** Use `PRIVATE` for production environments. Job submission works seamlessly via the API Gateway or SQS queues, which are the recommended patterns anyway.

### Configuring Endpoint Access

Edit `cdk.json` to change the endpoint access mode:

```json
{
  "context": {
    "eks_cluster": {
      "endpoint_access": "PRIVATE"
    }
  }
}
```

**Configuration Options:**

| Value | Description |
|-------|-------------|
| `PRIVATE` | EKS API only accessible from within VPC (default, most secure) |
| `PUBLIC_AND_PRIVATE` | EKS API accessible from internet and VPC |

**Example: Enable public access for development:**

```json
{
  "context": {
    "eks_cluster": {
      "endpoint_access": "PUBLIC_AND_PRIVATE"
    }
  }
}
```

After changing, redeploy the regional stacks:

```bash
gco stacks deploy gco-us-east-1 -y
```

### Job Submission with Private Endpoints

With `PRIVATE` endpoint access (the default), you have several secure options for submitting jobs:

**1. SQS Submission (Recommended)**

Submit jobs to a regional SQS queue - the most reliable method for region targeting:

```bash
# Submit to specific region
gco jobs submit-sqs examples/simple-job.yaml --region us-east-1

# Auto-select optimal region based on capacity
gco jobs submit-sqs examples/simple-job.yaml --auto-region
```

**2. API Gateway Submission**

Submit via the IAM-authenticated API Gateway:

```bash
gco jobs submit examples/simple-job.yaml
```

**3. Direct kubectl Access (requires network access to VPC)**

For direct kubectl access with private endpoints, you need network connectivity to the VPC:

- **AWS SSM Session Manager**: Connect to a bastion host or directly to nodes
- **VPN**: Site-to-site or client VPN to the VPC
- **Bastion Host**: EC2 instance in the VPC with kubectl configured
- **AWS Cloud9**: IDE in the VPC with kubectl access

```bash
# Example: Using SSM to port-forward to the cluster
aws ssm start-session --target i-bastion-instance-id

# Then on the bastion:
aws eks update-kubeconfig --name gco-us-east-1 --region us-east-1
kubectl apply -f job.yaml
```

## Configuring GPU Nodepools

### Modify Instance Types

Edit `lambda/kubectl-applier-simple/manifests/40-nodepool-gpu-x86.yaml`:

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: gpu-x86
spec:
  template:
    spec:
      requirements:
        - key: karpenter.k8s.aws/instance-family
          operator: In
          values:
            - g5      # NVIDIA A10G GPUs
            - g4dn    # NVIDIA T4 GPUs
            - p3      # NVIDIA V100 GPUs (add this)
            - p4d     # NVIDIA A100 GPUs (add this)
        
        - key: karpenter.k8s.aws/instance-size
          operator: In
          values:
            - xlarge
            - 2xlarge
            - 4xlarge  # Add larger sizes
```

### Adjust GPU Limits

```yaml
spec:
  limits:
    cpu: "1000"
    memory: 1000Gi
    nvidia.com/gpu: "50"  # Increase GPU limit
```

### Configure Spot Instances

```yaml
spec:
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 30s
  
  template:
    spec:
      requirements:
        - key: karpenter.sh/capacity-type
          operator: In
          values:
            - spot        # Enable spot instances
            - on-demand   # Keep on-demand as fallback
```

### Add Taints for GPU Nodes

```yaml
spec:
  template:
    spec:
      taints:
        - key: nvidia.com/gpu
          value: "true"
          effect: NoSchedule
```

Then use tolerations in your workloads:

```yaml
apiVersion: v1
kind: Pod
spec:
  tolerations:
    - key: nvidia.com/gpu
      operator: Equal
      value: "true"
      effect: NoSchedule
  nodeSelector:
    karpenter.k8s.aws/instance-family: g5
```

### GPU Time-Slicing (Fractional GPUs)

You can share a single GPU across multiple pods using NVIDIA time-slicing. The NVIDIA device plugin is already installed (as a standalone DaemonSet, with EKS Auto Mode providing the GPU drivers), but time-slicing is not enabled by default. To enable it, apply a ConfigMap that sets the number of replicas per physical GPU (e.g., `replicas: 4` makes one GPU appear as four schedulable units). The kube-scheduler can then place several lightweight workloads onto one GPU node. Note that Karpenter does not currently account for time-slicing replicas when provisioning nodes ([kubernetes-sigs/karpenter#2140](https://github.com/kubernetes-sigs/karpenter/issues/2140)), so it may over-provision initially.

See `examples/gpu-timeslicing-job.yaml` for a complete example with setup instructions.

## Customizing Services

### Health Monitor

#### Resource Thresholds

The health monitor compares cluster utilization against configurable thresholds in `cdk.json`. When any threshold is exceeded, the cluster reports as `unhealthy`.

```json
"resource_thresholds": {
  "cpu_threshold": 60,
  "memory_threshold": 60,
  "gpu_threshold": -1,
  "pending_pods_threshold": 10,
  "pending_requested_cpu_vcpus": 100,
  "pending_requested_memory_gb": 200,
  "pending_requested_gpus": -1
}
```

| Threshold | Default | Description |
|-----------|---------|-------------|
| `cpu_threshold` | 60 | CPU utilization % (0-100, or -1 to disable) |
| `memory_threshold` | 60 | Memory utilization % (0-100, or -1 to disable) |
| `gpu_threshold` | -1 | GPU utilization % (0-100, or -1 to disable) |
| `pending_pods_threshold` | 10 | Max pending pods before unhealthy (-1 to disable) |
| `pending_requested_cpu_vcpus` | 100 | Max vCPUs requested by pending pods (-1 to disable) |
| `pending_requested_memory_gb` | 200 | Max GB memory requested by pending pods (-1 to disable) |
| `pending_requested_gpus` | -1 | Max GPUs requested by pending pods (-1 to disable) |

Set a threshold to `-1` to disable that check entirely. GPU thresholds are disabled by default because inference endpoints naturally saturate GPU resources and should not trigger unhealthy status.

After changing thresholds, redeploy the regional stack:

```bash
gco stacks deploy gco-us-east-1 -y
```

#### Custom Health Checks

Edit `gco/services/health_monitor.py`:

```python
# Add custom health checks
@app.get("/healthz/custom")
async def custom_health_check():
    # Your custom logic
    return {"status": "healthy", "custom_metric": 42}
```

Rebuild and redeploy:

```bash
# Rebuild and deploy
gco stacks deploy-all -y
```

#### Global Accelerator Health Check

Global Accelerator uses HTTP health checks to determine if a region is healthy. The health check path is configured in `cdk.json`:

```json
"global_accelerator": {
  "health_check_path": "/api/v1/health",
  "health_check_interval": 30,
  "client_affinity": "NONE"
}
```

| Setting | Default | Description |
|---|---|---|
| `health_check_path` | `/api/v1/health` | HTTP path GA uses to check ALB health. Must be in `UNAUTHENTICATED_PATHS` in `gco/services/auth_middleware.py` |
| `health_check_interval` | `30` | Seconds between health checks |
| `health_check_grace_period` | `30` | Seconds to wait before first health check |
| `health_check_timeout` | `5` | Seconds before a health check times out |
| `client_affinity` | `NONE` | Listener client affinity. `NONE` spreads connections across endpoints for even load distribution; `SOURCE_IP` pins each client IP to the same endpoint for session stickiness |

The `/api/v1/health` endpoint returns 200 when the cluster is within resource thresholds and 503 when overloaded. This enables intelligent routing — GA automatically routes traffic away from overloaded regions.

The health check path must be listed in `UNAUTHENTICATED_PATHS` in `gco/services/auth_middleware.py` so GA can reach it without the secret header. A CI test (`tests/test_health_check_coverage.py`) validates this automatically.

#### Client Affinity

Global Accelerator listeners support two client-affinity modes, controlled by the `client_affinity` knob under `global_accelerator` in `cdk.json`:

- `NONE` (default): GA may route each new connection to any healthy endpoint. This maximizes even load distribution across regions and is the right choice for stateless request/response traffic.
- `SOURCE_IP`: GA pins connections from the same source IP to the same endpoint group for as long as it stays healthy. Use this when a workload keeps per-client state on a single region (for example, sticky sessions). Note that affinity is broken when an endpoint becomes unhealthy or the endpoint set changes.

The value is validated at synth time — anything other than `NONE` or `SOURCE_IP` raises a `ConfigValidationError`. See the [AWS Global Accelerator client affinity docs](https://docs.aws.amazon.com/global-accelerator/latest/dg/about-listeners.html#about-listeners-client-affinity) for details.

#### Inference Health Watchdog

The inference monitor includes a health watchdog that protects the ALB from unhealthy endpoints. If an inference endpoint has zero ready replicas for longer than the configured threshold, the watchdog removes its Ingress to prevent the unhealthy target group from marking the ALB as unhealthy in Global Accelerator.

Configure in `cdk.json`:

```json
"inference_monitor": {
  "reconcile_interval": 15,
  "unhealthy_threshold_seconds": 300
}
```

| Setting | Default | Description |
|---|---|---|
| `reconcile_interval` | `15` | Seconds between reconciliation cycles |
| `unhealthy_threshold_seconds` | `300` | Seconds an endpoint can be unready before its Ingress is removed (5 minutes) |

When the endpoint recovers (pods become ready), the Ingress is automatically re-created on the next reconciliation cycle. The watchdog logs a warning when it removes an Ingress so operators can investigate.

#### ALB Architecture

GCO uses a single ALB per region for all traffic (platform services and inference endpoints). All Ingresses share the `gco` ingress group via `IngressClassParams`.

| Component | Ingress Group | Services |
|---|---|---|
| Platform ALB | `gco` | health-monitor, manifest-processor, inference endpoints |

The inference health watchdog ensures that unhealthy inference endpoints don't tank the ALB's health in Global Accelerator. When an endpoint goes unhealthy, the watchdog removes its Ingress (and the associated target group) before GA can detect the failure. This keeps the ALB healthy for platform traffic even when inference endpoints are down.

The GA registration Lambda deterministically registers only this single platform ALB. On every deploy, it scrubs any stale endpoints (e.g., leftover NLBs from Helm charts) to ensure GA routes exclusively to the correct ALB.

### Manifest Processor

Edit `gco/services/manifest_processor.py`:

```python
# Add custom validation
def validate_manifest(manifest: dict) -> bool:
    # Your custom validation logic
    if "custom_field" not in manifest:
        raise ValueError("Missing custom_field")
    return True
```

## Security Policy Configuration

The `manifest_processor` section in `cdk.json` includes a `manifest_security_policy` object and an `allowed_kinds` list that control which Kubernetes manifest patterns are accepted or rejected.

This policy is enforced on **both** submission paths:

- the REST manifest processor (`POST /api/v1/manifests`)
- the SQS queue processor (`gco jobs submit-sqs`)

CDK wires the same configuration values into both services at deploy time, so a single policy change applies uniformly. An attacker holding `sqs:SendMessage` on the job queue cannot use the SQS path to bypass the checks enforced by the REST path.

### Security Policy Toggles

Each toggle controls a specific security check. Set a toggle to `false` to allow the corresponding pattern, or `true` to block it.

```json
"manifest_processor": {
  "manifest_security_policy": {
    "block_privileged": true,
    "block_privilege_escalation": true,
    "block_host_network": true,
    "block_host_pid": true,
    "block_host_ipc": true,
    "block_host_path": true,
    "block_added_capabilities": true,
    "block_run_as_root": false
  }
}
```

| Toggle | Default | What It Controls |
|--------|---------|-----------------|
| `block_privileged` | `true` | Rejects containers with `securityContext.privileged: true` |
| `block_privilege_escalation` | `true` | Rejects containers with `allowPrivilegeEscalation: true` |
| `block_host_network` | `true` | Rejects pods with `hostNetwork: true` |
| `block_host_pid` | `true` | Rejects pods with `hostPID: true` |
| `block_host_ipc` | `true` | Rejects pods with `hostIPC: true` |
| `block_host_path` | `true` | Rejects pods with `hostPath` volumes |
| `block_added_capabilities` | `true` | Rejects containers with `capabilities.add` entries |
| `block_run_as_root` | `false` | Rejects containers or pods with `runAsUser: 0` |

`block_run_as_root` defaults to `false` because many GPU and ML container images require root. Enable it if your security posture requires non-root execution.

**Example: Allow runAsUser: 0**

Many GPU containers (NVIDIA CUDA, PyTorch) run as root by default. The default configuration already allows this (`block_run_as_root: false`). If you previously enabled it and need to revert:

```json
"manifest_security_policy": {
  "block_run_as_root": false
}
```

### Allowed Resource Kinds

The `allowed_kinds` list controls which Kubernetes resource kinds can be submitted through the manifest processor. Manifests with a `kind` not in this list are rejected.

```json
"manifest_processor": {
  "allowed_kinds": ["Job", "CronJob", "Deployment", "StatefulSet", "DaemonSet", "Service", "ConfigMap", "Pod"]
}
```

The default list covers the most common workload and service types. Modify it to match your needs.

**Example: Restrict to only Jobs**

If your platform only runs batch workloads:

```json
"allowed_kinds": ["Job"]
```

All other kinds (Deployment, Service, etc.) will be rejected.

**Example: Add a custom kind like NetworkPolicy**

If you need users to submit NetworkPolicy resources:

```json
"allowed_kinds": ["Job", "CronJob", "Deployment", "StatefulSet", "DaemonSet", "Service", "ConfigMap", "Pod", "NetworkPolicy"]
```

After changing any security policy or allowed_kinds settings, redeploy the regional stack:

> **Note:** These settings apply to the **manifest processor** and **queue processor** validation layers. They do not affect containers or resources created outside the job submission APIs (e.g., by platform operators via kubectl directly or by Helm charts).

```bash
gco stacks deploy gco-us-east-1 -y
```

### Adjust Replica Counts

Edit the deployment manifests:

`lambda/kubectl-applier-simple/manifests/30-health-monitor.yaml`:

```yaml
spec:
  replicas: 5  # Increase from 2 to 5
```

Or use Horizontal Pod Autoscaler:

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: health-monitor-hpa
  namespace: gco-system
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: health-monitor
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

## Adding Kubernetes Manifests

Manifests are applied in sorted filename order. The naming convention encodes both
the deployment phase and the resource group:

```text
NN-group-name.yaml      # Main pass (applied before Helm)
post-helm-name.yaml     # Post-Helm pass (applied after Helm installs CRDs)
```

**Number ranges:**

- `00-09` — Foundation (namespaces, service accounts, RBAC, network policies)
- `10-19` — Networking (IngressClass, Ingress)
- `20-29` — Storage (EFS, FSx, Valkey)
- `30-39` — System services (health-monitor, manifest-processor, inference-monitor)
- `40-49` — NodePools (GPU, EFA, Neuron, CPU)
- `50-59` — Device plugins (NVIDIA)
- `post-helm-*` — Resources requiring Helm CRDs (KEDA ScaledJob, etc.)

**Optional features:** Files with unreplaced `{{PLACEHOLDER}}` values are automatically
skipped, so you can gate features on CDK config without touching the handler.

See `lambda/kubectl-applier-simple/manifests/README.md` for the full file listing.

### 1. Create Your Manifest

Create a new manifest named 33-my-service.yaml in the existing `lambda/kubectl-applier-simple/manifests/` directory:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-service
  namespace: gco-system
spec:
  replicas: 3
  selector:
    matchLabels:
      app: my-service
  template:
    metadata:
      labels:
        app: my-service
    spec:
      serviceAccountName: gco-service-account
      containers:
        - name: my-service
          image: {{MY_SERVICE_IMAGE}}  # Placeholder for CDK
          ports:
            - containerPort: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: my-service
  namespace: gco-system
spec:
  selector:
    app: my-service
  ports:
    - port: 80
      targetPort: 8080
```

### 2. Add Image to CDK Stack

Edit `gco/stacks/regional_stack.py`:

```python
# In _create_container_images method
self.my_service_image = ecr_assets.DockerImageAsset(
    self, "MyServiceImage",
    directory=".",
    file="path/to/my-service-dockerfile",
    platform=ecr_assets.Platform.LINUX_AMD64
)

# In _create_kubectl_lambda method, add to ImageReplacements
"ImageReplacements": {
    "{{HEALTH_MONITOR_IMAGE}}": self.health_monitor_image.image_uri,
    "{{MANIFEST_PROCESSOR_IMAGE}}": self.manifest_processor_image.image_uri,
    "{{MY_SERVICE_IMAGE}}": self.my_service_image.image_uri,  # Add this
    ...
}
```

### 3. Rebuild Lambda Package

```bash
rm -rf lambda/kubectl-applier-simple-build
mkdir -p lambda/kubectl-applier-simple-build
cp lambda/kubectl-applier-simple/handler.py lambda/kubectl-applier-simple-build/
cp -r lambda/kubectl-applier-simple/manifests lambda/kubectl-applier-simple-build/
pip3 install kubernetes pyyaml urllib3 -t lambda/kubectl-applier-simple-build/
```

### 4. Deploy

```bash
gco stacks deploy-all -y
```

## Modifying Network Configuration

### Change VPC CIDR

Edit `gco/stacks/regional_stack.py`:

```python
self.vpc = ec2.Vpc(
    self, "GCOVpc",
    vpc_name=f"{config.get_project_name()}-vpc-{region}",
    max_azs=3,
    ip_addresses=ec2.IpAddresses.cidr("10.1.0.0/16"),  # Custom CIDR
    nat_gateways=2,
    subnet_configuration=[
        ec2.SubnetConfiguration(
            name="PublicSubnet",
            subnet_type=ec2.SubnetType.PUBLIC,
            cidr_mask=24
        ),
        ec2.SubnetConfiguration(
            name="PrivateSubnet",
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
            cidr_mask=22  # Larger subnets for more IPs
        )
    ]
)
```

### Add VPC Endpoints

```python
# Add S3 endpoint
self.vpc.add_gateway_endpoint(
    "S3Endpoint",
    service=ec2.GatewayVpcEndpointAwsService.S3
)

# Add ECR endpoints
self.vpc.add_interface_endpoint(
    "EcrEndpoint",
    service=ec2.InterfaceVpcEndpointAwsService.ECR
)
```

### Modify Security Groups

```python
# Add custom ingress rule
alb_security_group.add_ingress_rule(
    peer=ec2.Peer.ipv4("YOUR-OFFICE-IP/32"),
    connection=ec2.Port.tcp(443),
    description="Allow from office"
)
```

## Adjusting Resource Limits

### Pod Resource Requests/Limits

Edit deployment manifests:

```yaml
spec:
  containers:
    - name: health-monitor
      resources:
        requests:
          cpu: "500m"      # Increase from 100m
          memory: "512Mi"  # Increase from 128Mi
        limits:
          cpu: "2000m"     # Increase from 500m
          memory: "2Gi"    # Increase from 512Mi
```

### nodepool Limits

Edit nodepool manifests:

```yaml
spec:
  limits:
    cpu: "2000"      # Total CPUs across all nodes
    memory: 2000Gi   # Total memory
```

### Lambda Configuration

Edit `gco/stacks/regional_stack.py`:

```python
kubectl_lambda = lambda_.Function(
    self, "KubectlApplierFunction",
    runtime=lambda_.Runtime.PYTHON_3_14,
    handler="handler.lambda_handler",
    code=lambda_.Code.from_asset("lambda/kubectl-applier-simple-build"),
    timeout=Duration.minutes(10),  # Increase from 5
    memory_size=1024,              # Increase from 512
    ...
)
```

## Helm Chart Configuration

GCO installs scheduler and infrastructure Helm charts via the `helm` section in `cdk.json`. Each chart can be toggled independently:

```json
{
  "context": {
    "helm": {
      "keda": { "enabled": true },
      "aws_efa_device_plugin": { "enabled": true },
      "aws_neuron_device_plugin": { "enabled": true },
      "volcano": { "enabled": true },
      "kuberay": { "enabled": true },
      "cert_manager": { "enabled": true },
      "slurm": { "enabled": false },
      "yunikorn": { "enabled": false },
      "kueue": { "enabled": true }
    }
  }
}
```

| Chart | Default | Description |
|-------|---------|-------------|
| `keda` | Enabled | Event-driven autoscaling (SQS, Prometheus, etc.) |
| `aws_efa_device_plugin` | Enabled | EFA device management for high-performance networking |
| `aws_neuron_device_plugin` | Enabled | Trainium/Inferentia device management |
| `volcano` | Enabled | Gang scheduling for distributed training |
| `kuberay` | Enabled | Ray distributed computing operator |
| `cert_manager` | Enabled | TLS certificate management |
| `slurm` | Disabled | Slurm on Kubernetes (operator + cluster) |
| `yunikorn` | Disabled | App-aware scheduler with hierarchical queues |
| `kueue` | Enabled | Job queueing with quotas and fair sharing |

A useful subset of charts is enabled by default, but every cluster is different. Experiment to find which tools best suit your workloads and disable the ones you don't need — each enabled chart runs controller pods that consume CPU and memory on your system nodes. For example, if you don't use gang scheduling, disable Volcano. If you don't need event-driven autoscaling, disable KEDA. Fewer charts means less system overhead and faster deploys.

> **⚠️ Helm charts install asynchronously — give them time to finish.**
>
> Chart installation is **deliberately decoupled from the CloudFormation deploy**. When `gco stacks deploy-all` reports the regional stack as `CREATE_COMPLETE`, that means the install has been *kicked off* — not that every chart is ready. The charts are then installed one at a time by a Step Functions state machine in the background, and full convergence can take **10–30+ minutes** depending on how many charts are enabled and how fast their images pull (some third-party images come from `docker.io` and can be slow, e.g. Volcano).
>
> This is intentional: a slow or failing chart must **never** roll back and destroy the freshly-created EKS cluster. Each chart installs independently — one slow or broken chart does not block the rest.
>
> Monitor convergence and inspect per-chart results at any time:
>
> ```bash
> # Per-chart status (reads the status each chart task records in SSM)
> gco stacks addons status -r <region>
> gco stacks addons status --all-regions
> ```
>
> If a chart shows as failed (for example, a transient image-pull timeout), re-converge the add-on layer without touching the cluster:
>
> ```bash
> gco stacks addons install -r <region>
> gco stacks addons install --all-regions
> ```
>
> Workloads that depend on a specific scheduler/operator (Volcano, Kueue, KubeRay, etc.) should wait until that chart shows `installed` before they are submitted.

See [Schedulers & Orchestrators](SCHEDULERS.md) for detailed guidance on each tool.

### Get Volcano's docker.io images off the rate-limited path (ECR mirror)

A few add-on charts pull their images directly from Docker Hub (`docker.io`) — most notably **Volcano** (`volcanosh/vc-*`). On a cold cluster those anonymous pulls are slow and subject to Docker Hub rate limits, which can make Volcano's install time out and retry. GCO can mirror those images into the project's **own ECR** (under the `gco/*` prefix) and point Volcano at the mirror, so the cluster makes fast, same-account ECR pulls with the pull-only node role it already has. This is **off by default** and needs **no Docker Hub credential** — it's a static mirror you refresh when the chart version changes.

> **Why a mirror and not an ECR pull-through cache?** ECR pull-through cache for Docker Hub *requires* a stored Docker Hub credential (anonymous Docker Hub PTC isn't supported), and on EKS Auto Mode the pull-only, service-managed node role complicates cache-miss imports. Mirroring sidesteps both: no credential, and the images are plain `gco/*` ECR repos the nodes can already pull.

When enabled, the regional stack injects one Volcano value override — `basic.image_registry` → `<account>.dkr.ecr.<region>.amazonaws.com/<ecr_namespace>` — so every Volcano image (controller, scheduler, admission webhook, and the pre-install admission-init hook, which all render from `basic.image_registry`) resolves from ECR. It creates **no** CloudFormation resources; the mirror is populated by `gco stacks deploy` (automatically, see below) or the `scripts/mirror_images.py` tool.

**1. Enable it in `cdk.json`.** Flip `enabled` (the default `ecr_namespace` of `gco/dockerhub` is fine):

```json
{
  "context": {
    "volcano_image_mirror": {
      "enabled": true,
      "ecr_namespace": "gco/dockerhub"
    }
  }
}
```

**2. Deploy.** `gco stacks deploy <stack>` / `deploy-all` **auto-mirrors** the images into ECR (per region) right before the regional stack's Helm install — so a fresh install just works, with no separate step. The copy is idempotent and skips images already present, so repeat deploys cost only a couple of ECR lookups. From a machine with a container runtime (Docker Buildx, Finch, or skopeo) and AWS credentials; the source pull from Docker Hub is anonymous and one-time.

**3. Converge / check.** If Volcano had previously failed, re-converge without touching the cluster:

```bash
gco stacks addons install -r <region>
gco stacks addons status -r <region>
```

**Mirror manually (optional).** To pre-seed a region before enabling, or to re-mirror after a version bump, run the tool directly:

```bash
python scripts/mirror_images.py --region us-east-1
python scripts/mirror_images.py --region us-east-1 --dry-run   # preview only
```

It reads the image set and pinned tag from `lambda/helm-installer/charts.yaml`, creates the `gco/<...>` ECR repositories if needed, and copies each image preserving the **full multi-arch manifest list** (via `docker buildx imagetools create`, Finch `--all-platforms`, or `skopeo copy --all`, whichever the runtime supports) — so both amd64 and arm64 (Graviton) nodes find a matching image. A plain `docker pull`/`push` would drop every architecture except the build host's, so it is never used.

Notes:

- `ecr_namespace` must start with `gco/` so it inherits the project's `gco/*` node-pull access, ECR replication, and trusted-registry allow-list. The toggle is validated at synth time — an `ecr_namespace` outside `gco/` or an invalid ECR path fails fast.
- If an enabled mirror can't complete during deploy (no container runtime, network, credentials), the deploy aborts **before** CloudFormation rather than bringing up a cluster whose Volcano images aren't in ECR.
- It's a **static** mirror: when you bump the Volcano chart `version`/`image_tag_version` in `charts.yaml`, the next `gco stacks deploy` re-mirrors the new tag (or run `scripts/mirror_images.py` to do it out-of-band).
- This only changes where images are *pulled from* — Volcano's behavior and versions are unchanged.
- The mirror is a **general** tool — to mirror another chart's docker.io-only image down the road, see "HOW TO ADD AN IMAGE TO THE MIRROR" in `cli/_image_mirror.py`.

For the full reference — architecture, the multi-arch copy strategy, the MCP tools (`images_mirror_plan` / `images_mirror_status` / `images_mirror`), troubleshooting, and how to add another chart's image — see [Image Mirror](IMAGE_MIRROR.md).

## Enabling Additional Features

### Enable EKS Logging

Edit `gco/stacks/regional_stack.py`:

```python
self.cluster = eks.Cluster(
    self, "GCOEksCluster",
    cluster_name=cluster_config.cluster_name,
    version=eks.KubernetesVersion.V1_35,
    vpc=self.vpc,
    compute=eks.ComputeConfig(
        node_pools=["system", "general-purpose"]
    ),
    endpoint_access=eks.EndpointAccess.PUBLIC_AND_PRIVATE,
    role=cluster_admin_role,
    vpc_subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],
    logging=eks.ClusterLoggingTypes.all()  # Enable all logging
)
```

### Add CloudWatch Container Insights

```bash
# Apply Container Insights DaemonSet
kubectl apply -f https://raw.githubusercontent.com/aws-samples/amazon-cloudwatch-container-insights/latest/k8s-deployment-manifest-templates/deployment-mode/daemonset/container-insights-monitoring/quickstart/cwagent-fluentd-quickstart.yaml
```

### Enable AWS Load Balancer Controller

Already included in EKS Auto Mode! Just add annotations to your Ingress:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  annotations:
    alb.ingress.kubernetes.io/scheme: internet-facing
    alb.ingress.kubernetes.io/target-type: ip
```

### Add Prometheus Monitoring

```bash
# Install Prometheus using Helm
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace
```

## Cost Tracking Setup

GCO includes built-in cost visibility via the `gco costs` CLI commands. These use AWS Cost Explorer to show spend filtered by the `Project: GCO` tag that CDK applies to all resources.

### Activating Cost Allocation Tags

Cost Explorer requires tags to be explicitly activated before they can be used for filtering. This is a one-time setup per AWS account:

1. Open the [AWS Billing Console → Cost Allocation Tags](https://us-east-1.console.aws.amazon.com/billing/home#/tags)
2. Under "User-defined cost allocation tags", search for `Project`
3. Select the `Project` tag and click "Activate"
4. Optionally also activate `Environment` and `Owner` for more granular filtering
5. Wait ~24 hours for the tag data to appear in Cost Explorer

### Verifying Cost Tracking

After activation, verify with:

```bash
# Should show costs filtered by Project:GCO tag
gco costs summary

# If tags haven't propagated yet, use --all for total account costs
gco costs summary --all
```

### Available Cost Commands

```bash
gco costs summary              # Spend by AWS service
gco costs regions              # Spend by region
gco costs trend --days 14      # Daily cost trend with chart
gco costs workloads            # Real-time running workload estimates
gco costs forecast             # 30-day cost forecast
```

See [CLI Reference](CLI.md#costs-commands) for full details.

## FSx for Lustre Configuration

FSx for Lustre provides high-performance parallel file system storage ideal for ML training workloads that require high throughput and low latency.

### Lustre Version Compatibility

GCO uses Lustre 2.15 which is compatible with EKS Auto Mode's Bottlerocket nodes (kernel 6.x).

| Lustre Version | Kernel 5.x (AL2) | Kernel 6.x (AL2023/Bottlerocket) |
|----------------|------------------|----------------------------------|
| 2.10           | ✅ Yes           | ❌ No                            |
| 2.12           | ✅ Yes           | ✅ Yes                           |
| 2.15           | ✅ Yes           | ✅ Yes                           |

See [AWS Lustre Client Compatibility Matrix](https://docs.aws.amazon.com/fsx/latest/LustreGuide/lustre-client-matrix.html) for details.

### Enable FSx

```bash
# Enable FSx for Lustre
gco stacks fsx enable -y

# Redeploy the stack
gco stacks deploy gco-us-east-1 -y
```

### Configure FSx Storage

Edit `cdk.json` to customize FSx storage settings:

```json
{
  "context": {
    "fsx_lustre": {
      "enabled": true,
      "storage_capacity_gib": 1200,
      "deployment_type": "SCRATCH_2",
      "file_system_type_version": "2.15",
      "data_compression_type": "LZ4",
      "per_unit_storage_throughput": 200
    }
  }
}
```

**Deployment Types:**

- `SCRATCH_1`: Temporary storage, no replication (cheapest)
- `SCRATCH_2`: Temporary storage with better burst performance (recommended for most workloads)
- `PERSISTENT_1`: Persistent storage with data replication
- `PERSISTENT_2`: Latest persistent storage with higher throughput

**File System Type Version:**

- `2.15`: Latest version, recommended (default)
- `2.12`: Compatible with kernel 6.x

**Storage Capacity:**

- Minimum: 1200 GiB for SCRATCH_2
- Must be in increments of 2400 GiB for SCRATCH_2

### Using FSx in Jobs

Jobs can mount FSx storage using the pre-created PVC:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: fsx-training-job
  namespace: gco-jobs
spec:
  template:
    spec:
      containers:
      - name: training
        image: your-training-image
        volumeMounts:
        - name: fsx-scratch
          mountPath: /scratch
        resources:
          requests:
            cpu: "4"
            memory: "16Gi"
      
      volumes:
      - name: fsx-scratch
        persistentVolumeClaim:
          claimName: gco-fsx-storage
      
      restartPolicy: Never
```

**Available PVCs:**

- `gco-fsx-storage` in `default` namespace
- `gco-fsx-storage` in `gco-jobs` namespace
- `gco-fsx-storage` in `gco-system` namespace

See `examples/fsx-lustre-job.yaml` for a complete example.

### Configure Valkey Cache

GCO can deploy an ElastiCache Serverless Valkey cache in each regional stack for low-latency key-value storage. Use cases include prompt caching for inference, session state, feature stores, and shared state across pods.

Enable via CLI:

```bash
# Enable Valkey
gco stacks valkey enable -y

# Enable with custom settings
gco stacks valkey enable --max-storage 10 --max-ecpu 10000 -y

# Check current status
gco stacks valkey status

# Disable
gco stacks valkey disable -y

# Redeploy to apply
gco stacks deploy-all -y
```

Or edit `cdk.json` directly:

```json
{
  "context": {
    "valkey": {
      "enabled": true,
      "max_data_storage_gb": 5,
      "max_ecpu_per_second": 5000,
      "snapshot_retention_limit": 1
    }
  }
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `false` | Enable/disable Valkey cache |
| `max_data_storage_gb` | 5 | Maximum data storage in GB (auto-scales from 1 GB) |
| `max_ecpu_per_second` | 5000 | Maximum ElastiCache Processing Units per second |
| `snapshot_retention_limit` | 1 | Number of daily snapshots to retain |

After enabling, redeploy the regional stack:

```bash
gco stacks deploy gco-us-east-1 -y
```

### Using Valkey in Jobs

When Valkey is enabled, GCO creates a `gco-valkey` ConfigMap in each namespace with the endpoint and port. Reference it in your pod spec — no need to look up or hardcode endpoint URLs:

```yaml
env:
- name: VALKEY_ENDPOINT
  valueFrom:
    configMapKeyRef:
      name: gco-valkey
      key: endpoint
- name: VALKEY_PORT
  valueFrom:
    configMapKeyRef:
      name: gco-valkey
      key: port
```

The same manifest works in any region — the ConfigMap resolves to the local Valkey endpoint automatically.

For use outside the cluster (scripts, Lambda functions), the endpoint is also stored in SSM at `/{project}/valkey-endpoint-{region}`.

See `examples/valkey-cache-job.yaml` for a complete working example.

### Configure Aurora pgvector

GCO can deploy an Aurora Serverless v2 PostgreSQL cluster with the pgvector extension in each regional stack for vector similarity search. Use cases include RAG (retrieval-augmented generation), semantic search, embedding storage, and similarity queries for AI/ML workloads.

Aurora Serverless v2 supports scaling to 0 ACU — the cluster automatically pauses after a period of inactivity and resumes in ~15 seconds on the first connection. You pay only for storage while paused. This is ideal for dev/test environments and workloads that can tolerate a brief cold start.

Enable via CLI:

```bash
# Enable Aurora pgvector
gco stacks aurora enable -y

# Enable with custom settings
gco stacks aurora enable --min-acu 2 --max-acu 32 --deletion-protection -y

# Check current status
gco stacks aurora status

# Disable
gco stacks aurora disable -y

# Redeploy to apply
gco stacks deploy-all -y
```

Or edit `cdk.json` directly:

```json
{
  "context": {
    "aurora_pgvector": {
      "enabled": true,
      "min_acu": 0,
      "max_acu": 16,
      "backup_retention_days": 7,
      "deletion_protection": false
    }
  }
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `false` | Enable/disable Aurora pgvector |
| `min_acu` | 0 | Minimum Aurora Capacity Units (0 = scale to zero / auto-pause) |
| `max_acu` | 16 | Maximum Aurora Capacity Units |
| `backup_retention_days` | 7 | Number of days to retain automated backups |
| `deletion_protection` | `false` | Enable deletion protection (recommended for production) |

Set `min_acu` to `0.5` or higher to disable auto-pause and keep the cluster always warm.

After enabling, redeploy the regional stack:

```bash
gco stacks deploy gco-us-east-1 -y
```

### Using Aurora pgvector in Jobs

When Aurora pgvector is enabled, GCO creates a `gco-aurora-pgvector` ConfigMap in each namespace with the endpoint, port, secret ARN, and database name. Reference it in your pod spec:

```yaml
env:
- name: AURORA_ENDPOINT
  valueFrom:
    configMapKeyRef:
      name: gco-aurora-pgvector
      key: endpoint
- name: AURORA_PORT
  valueFrom:
    configMapKeyRef:
      name: gco-aurora-pgvector
      key: port
- name: AURORA_SECRET_ARN
  valueFrom:
    configMapKeyRef:
      name: gco-aurora-pgvector
      key: secret_arn
- name: AURORA_DATABASE
  valueFrom:
    configMapKeyRef:
      name: gco-aurora-pgvector
      key: database
```

Credentials are stored in AWS Secrets Manager. Pods retrieve them using the ServiceAccountRole's IRSA permissions — no static credentials needed. The same manifest works in any region because the ConfigMap resolves to the local Aurora endpoint automatically.

The cluster includes both a writer and a reader instance for high availability. The reader auto-scales with the writer. Use the `reader_endpoint` for read-heavy workloads (similarity searches, embedding lookups) and the `endpoint` for writes (inserts, DDL).

For use outside the cluster (scripts, Lambda functions), the endpoint is also stored in SSM at `/{project}/aurora-pgvector-endpoint-{region}`.

See `examples/aurora-pgvector-job.yaml` for a complete working example that creates the pgvector extension, an embeddings table with an HNSW index, and runs a similarity search.

## Infrastructure Version Constants

All pinned infrastructure versions — EKS add-on versions, Lambda runtime, Aurora PostgreSQL engine version — are centralised in `gco/stacks/constants.py`. This is the single source of truth for version-pinned components.

When updating a version:

1. Edit the constant in `gco/stacks/constants.py`
2. Run `pytest tests/test_regional_stack.py` to verify synthesis
3. Run `pytest tests/test_nag_compliance.py` to verify compliance
4. Redeploy with `gco stacks deploy-all -y`

The monthly `deps-scan` workflow (`.github/scripts/dependency-scan.sh`) checks these constants against the latest available versions and opens a GitHub issue when updates are available.

## CDK-nag Compliance

GCO uses CDK-nag to validate infrastructure against multiple compliance frameworks during synthesis and deployment.

### Enabled Frameworks

The following compliance frameworks are enabled in `app.py`:

| Framework | Description |
|-----------|-------------|
| AWS Solutions | Best practices for AWS architectures |
| HIPAA Security | Healthcare compliance requirements |
| NIST 800-53 Rev 5 | Federal security controls |
| PCI DSS 3.2.1 | Payment card industry standards |
| Serverless | Best practices for serverless architectures |

All checks run automatically during `cdk synth` and `cdk deploy`.

### Customizing Suppressions

Suppressions are centralized in `gco/stacks/nag_suppressions.py`. Each suppression includes:

- The rule ID being suppressed
- A detailed reason explaining why the suppression is justified
- The specific resources the suppression applies to (when applicable)

To view current suppressions:

```bash
# View all suppressions
cat gco/stacks/nag_suppressions.py
```

### Adding New Suppressions

If you add new infrastructure that triggers cdk-nag warnings, add suppressions with proper justification:

```python
# In gco/stacks/nag_suppressions.py

def add_my_custom_suppressions(stack: Stack) -> None:
    """Add suppressions for my custom resources."""
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            NagPackSuppression(
                id="AwsSolutions-XXX",
                reason="Detailed explanation of why this suppression is justified. "
                "Include references to documentation or architectural decisions.",
            ),
        ],
    )
```

Then call your function in `apply_all_suppressions()`:

```python
def apply_all_suppressions(stack: Stack, stack_type: str = "regional") -> None:
    # ... existing code ...
    add_my_custom_suppressions(stack)
```

### Disabling Compliance Checks

To disable specific frameworks (not recommended for production):

```python
# In app.py, comment out unwanted checks
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
cdk.Aspects.of(app).add(HIPAASecurityChecks(verbose=True))
# cdk.Aspects.of(app).add(NIST80053R5Checks(verbose=True))  # Disabled
# cdk.Aspects.of(app).add(PCIDSS321Checks(verbose=True))    # Disabled
cdk.Aspects.of(app).add(ServerlessChecks(verbose=True))
```

## Configuration Best Practices

### 1. Use Configuration Files

Store configuration in `cdk.json` context:

```json
{
  "context": {
    "project_name": "gco",
    "deployment_regions": {
      "global": "us-east-2",
      "api_gateway": "us-east-2",
      "monitoring": "us-east-2",
      "regional": ["us-east-1"]
    },
    "enable_monitoring": true,
    "enable_gpu": true,
    "gpu_instance_families": ["g5", "g4dn"],
    "max_gpu_nodes": 10
  }
}
```

### 2. Environment-Specific Configuration

```python
# In your stack
env = self.node.try_get_context("environment") or "dev"

if env == "prod":
    replicas = 5
    instance_types = ["g5.2xlarge"]
else:
    replicas = 2
    instance_types = ["g4dn.xlarge"]
```

### 3. Version Control

- Commit all configuration changes
- Tag releases
- Use feature branches for major changes

### 4. Test in Development First

```bash
# Deploy to dev account
export AWS_PROFILE=dev
gco stacks deploy-all -y

# Test thoroughly
kubectl apply -f test-workload.yaml

# Then deploy to prod
export AWS_PROFILE=prod
gco stacks deploy-all -y
```

## EFA (Elastic Fabric Adapter) Configuration

EFA enables high-performance inter-node communication for distributed training and high-performance LLM inference. GCO installs the EFA device plugin by default, and creates an EFA-optimized nodepool for instances like `p4d.24xlarge`, `p5.48xlarge`, and `p6` (B200/B300). On EKS Auto Mode the GPU AMI already ships the EFA and NVIDIA kernel drivers, so no separate networking operator is required.

### Disable EFA

EFA is enabled by default. To disable it, edit the `helm` section in `cdk.json`:

```json
{
  "context": {
    "helm": {
      "aws_efa_device_plugin": { "enabled": false }
    }
  }
}
```

Then redeploy:

```bash
gco stacks deploy-all -y
```

When enabled, this provides:

- AWS EFA Kubernetes Device Plugin (advertises `vpc.amazonaws.com/efa` resources)
- EFA-optimized nodepool (`gpu-efa-pool`) for p4d/p5 instances

### Using EFA in Jobs

Request EFA devices in your pod spec:

```yaml
resources:
  requests:
    nvidia.com/gpu: "8"
    vpc.amazonaws.com/efa: "4"
  limits:
    nvidia.com/gpu: "8"
    vpc.amazonaws.com/efa: "4"
```

Set environment variables for NCCL to use EFA:

```yaml
env:
- name: FI_PROVIDER
  value: "efa"
- name: NCCL_SOCKET_IFNAME
  value: "eth0"
- name: FI_EFA_USE_DEVICE_RDMA
  value: "1"
```

See `examples/efa-distributed-training.yaml` for a complete example.

### Supported Instance Types

| Instance Type | EFA Networking | GPUs (Total GPU Memory) | Use Case |
|--------------|---------------|------------------------|----------|
| `p4d.24xlarge` | 400 Gbps (4x EFA) | 8x A100 (320 GB HBM2e) | Distributed training, fine-tuning |
| `p5.48xlarge` | 3,200 Gbps (32x EFA) | 8x H100 (640 GB HBM3) | Large-scale training, high-performance inference |
| `p5e.48xlarge` | 3,200 Gbps (32x EFA) | 8x H200 (1,128 GB HBM3e) | Large-scale training, high-performance inference |
| `p6-b200.48xlarge` | 3.2 Tbps (8x EFAv4) | 8x B200 (1,432 GB HBM3e) | Large-scale training and inference |
| `p6-b300.48xlarge` | 6.4 Tbps EFAv4 | 8x B300 Ultra (2,144 GB HBM3e) | Large-scale training and inference |
| `p6e-gb200` | 28.8 Tbps (EFAv4 UltraServer) | GB200 NVL72 | Largest-scale training and inference |

### NIXL Support

With EFA enabled, GCO supports NVIDIA Inference Xfer Library (NIXL) for high-performance LLM inference. NIXL enables high-throughput, low-latency KV-cache transfer between nodes. It integrates with vLLM, SGLang, and NVIDIA Dynamo. Requires EFA installer v1.47.0+ which is included in EKS-optimized AMIs — EKS Auto Mode automatically uses these AMIs, so no manual AMI configuration is needed.

## AWS Trainium and Inferentia Configuration

GCO includes built-in support for AWS Trainium and Inferentia accelerators. These are purpose-built ML chips designed by AWS that use the Neuron SDK instead of CUDA. GCO installs the Neuron device plugin by default and creates a dedicated Neuron nodepool for trn1, trn2, trn3, and inf2 instances.

### How It Works

- The Neuron device plugin (installed via Helm chart) advertises `aws.amazon.com/neuron` resources on Neuron-capable nodes
- The Neuron nodepool (`lambda/kubectl-applier-simple/manifests/44-nodepool-neuron.yaml`) provisions trn1, trn2, trn3, and inf2 instances
- A `aws.amazon.com/neuron` taint prevents non-Neuron workloads from scheduling on these nodes
- Pods must explicitly tolerate the taint and request `aws.amazon.com/neuron` resources

### Supported Instance Types

| Instance Type | Neuron Devices (Chips) | NeuronCores | Accelerator Memory | Use Case |
|--------------|----------------------|-------------|-------------------|----------|
| `inf2.xlarge` | 1 (Inferentia2) | 2 | 32 GB | Single-model inference |
| `inf2.24xlarge` | 6 (Inferentia2) | 12 | 192 GB | Multi-model inference |
| `inf2.48xlarge` | 12 (Inferentia2) | 24 | 384 GB | Large model inference |
| `trn1.2xlarge` | 1 (Trainium) | 2 | 32 GB | Small-scale training |
| `trn1.32xlarge` | 16 (Trainium) | 32 | 512 GB | Distributed training (1,600 Gbps EFA) |
| `trn2.3xlarge` | 1 (Trainium2) | 8 | 96 GB | Medium-scale training |
| `trn2.48xlarge` | 16 (Trainium2) | 128 | 1.5 TB | Large-scale training (3.2 Tbps EFA) |

### Using Neuron in Jobs

Request Neuron devices in your pod spec:

```yaml
resources:
  requests:
    aws.amazon.com/neuron: 1
  limits:
    aws.amazon.com/neuron: 1
tolerations:
- key: aws.amazon.com/neuron
  operator: Equal
  value: "true"
  effect: NoSchedule
```

Container images must include the Neuron runtime — use images from `public.ecr.aws/neuron/`.

See `examples/trainium-job.yaml` and `examples/inferentia-job.yaml` for complete examples.

## Troubleshooting Customizations

### Changes Not Applied

1. Rebuild Lambda package if you modified manifests
2. Force update: `gco stacks deploy-all -y`
3. Check CloudFormation events for errors

### Image Build Failures

1. Ensure Finch/Docker is running
2. Check Dockerfile syntax
3. Verify base image availability

### Manifest Application Failures

1. Check Lambda logs: `aws logs tail /aws/lambda/gco-*-KubectlApplier*`
2. Validate YAML syntax
3. Ensure image placeholders match CDK configuration

## Regional API Gateway (Private Access)

When public access is disabled (internal ALB only), you can enable regional API Gateways to provide authenticated API access via VPC Lambdas.

### Enable Regional APIs

Edit `cdk.json` to enable regional API Gateways:

```json
{
  "context": {
    "api_gateway": {
      "regional_api_enabled": true
    }
  }
}
```

Then redeploy:

```bash
gco stacks deploy-all -y
```

### How It Works

Regional API Gateways deploy a VPC Lambda in each region that can reach the internal ALB directly:

```text
User → Regional API Gateway (IAM Auth) → VPC Lambda → Internal ALB → EKS pods
```

This bypasses the need for Global Accelerator and public ALB exposure.

### Using Regional APIs

**CLI:**

```bash
# Use --regional-api flag
gco --regional-api jobs list --region us-east-1
gco --regional-api jobs submit job.yaml --region us-east-1

# Or set environment variable
export GCO_REGIONAL_API=true
gco jobs list --region us-east-1
```

**When to Use:**

- ALB is internal-only (no public exposure)
- Maximum security posture required
- Compliance requirements prohibit public endpoints
- Direct regional access preferred over global routing

### Security Considerations

Regional APIs maintain the same security model as the global API:

- IAM authentication (SigV4) at API Gateway
- Secret header injection by VPC Lambda
- Backend validation of secret header
- No public exposure of ALB or EKS API

---

## Queue Processor (SQS Consumer)

GCO ships with a built-in queue processor that automatically consumes manifests submitted via `gco jobs submit-sqs`. It uses a KEDA ScaledJob that scales consumer pods based on SQS queue depth — zero pods when the queue is empty, up to `max_concurrent_jobs` when messages are waiting.

### Configuration

Queue-processor-specific settings live in `cdk.json` under `queue_processor`. Validation policy (namespace allowlist, resource caps, image registry allowlist, security toggles) lives under `job_validation_policy` because the REST manifest processor reads the same values — see [Security Policy Configuration](#security-policy-configuration) for that section.

```json
"queue_processor": {
  "enabled": true,
  "polling_interval": 10,
  "max_concurrent_jobs": 10,
  "messages_per_job": 1,
  "successful_jobs_history": 20,
  "failed_jobs_history": 10
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `true` | Set to `false` to disable the built-in consumer entirely |
| `polling_interval` | `10` | How often KEDA checks the SQS queue for new messages (seconds) |
| `max_concurrent_jobs` | `10` | Maximum consumer pods running in parallel |
| `messages_per_job` | `1` | Number of SQS messages that trigger one consumer pod |
| `successful_jobs_history` | `20` | How many completed consumer jobs to keep in history |
| `failed_jobs_history` | `10` | How many failed consumer jobs to keep in history |

For namespace allowlisting, resource caps, and security toggles (shared between both services):

```json
"job_validation_policy": {
  "allowed_namespaces": ["default", "gco-jobs"],
  "resource_quotas": {
    "max_cpu_per_manifest": "10",
    "max_memory_per_manifest": "32Gi",
    "max_gpu_per_manifest": 4
  }
}
```

### Disabling the Built-In Consumer

If you want to implement your own SQS consumer (e.g., with custom validation logic, different scaling behavior, or a different processing pipeline), set `enabled` to `false`:

```json
"queue_processor": {
  "enabled": false
}
```

Then redeploy. On a fresh deploy this prevents the ScaledJob from being created. If you're disabling it on an existing cluster, you'll also need to manually delete the ScaledJob:

```bash
kubectl delete scaledjob sqs-queue-processor -n gco-system
```

Then deploy your own KEDA ScaledJob. See `examples/keda-scaled-job.yaml` for a starting point. The SQS queue URL is available as a CloudFormation output (`JobQueueUrl`) and the KEDA operator already has IRSA permissions to read queue metrics.

### How It Works

1. User runs `gco jobs submit-sqs manifest.yaml --region us-east-1`
2. CLI sends the manifest(s) as a JSON message to the regional SQS queue
3. KEDA detects the message and spins up a queue-processor pod
4. The pod reads the message, validates the manifest(s), and applies them via the Kubernetes API
5. On success, the message is deleted from SQS
6. On failure, the message returns to the queue after the visibility timeout (5 min) and eventually moves to the DLQ after 3 failed attempts

### Security Parity with the REST Path

The queue processor enforces the same security checks as the REST manifest processor. Both paths read the same `job_validation_policy.manifest_security_policy` section in `cdk.json`, so a single toggle flip (for example setting `block_run_as_root: true`) applies to both submission paths at the next deploy.

The checks enforced on both paths are:

- Namespace allowlist
- Resource kind allowlist (`allowed_kinds`)
- Privileged pods and containers (`block_privileged`)
- Privilege escalation (`block_privilege_escalation`)
- Host namespace access — `hostNetwork`, `hostPID`, `hostIPC`
- `hostPath` volumes (`block_host_path`)
- Added Linux capabilities (`block_added_capabilities`)
- `runAsUser: 0` (`block_run_as_root`, off by default)
- Image registry allowlist (`trusted_registries`, `trusted_dockerhub_orgs`)
- Resource caps (CPU, memory, GPU summed across all container kinds)

See [Security Policy Configuration](#security-policy-configuration) for the full reference and JSON examples.

---

## Cost Optimization

### Spot vs On-Demand

Spot instances can save 60-70% over on-demand for GPU workloads. GCO's nodepools support both:

```yaml
# In nodepool manifests
- key: "karpenter.sh/capacity-type"
  operator: In
  values: ["spot", "on-demand"]  # Spot preferred, on-demand fallback
```

Use spot for fault-tolerant workloads and for everything else use on-demand.

### Storage Costs

| Storage | Pricing Model | Best For | Rough Cost |
|---------|--------------|----------|------------|
| EFS | Per GB stored + throughput | General purpose, small datasets | ~$0.30/GB/month |
| FSx Lustre | Per GB provisioned | HPC, large datasets, high throughput | ~$0.14/GB/month (SCRATCH_2) |

FSx is cheaper per GB but you pay for provisioned capacity (minimum 1.2 TB). EFS scales to zero cost when empty.

### Scale-to-Zero Savings

Inference endpoints with KEDA can scale to zero when idle, eliminating GPU costs during off-hours:

```yaml
minReplicaCount: 0   # No GPU cost when idle
cooldownPeriod: 300  # Scale down after 5 min of no traffic
```

A single `g5.xlarge` (1x A10G GPU) costs ~$1.00/hour on-demand. Scale-to-zero during 12 hours of off-peak saves ~$360/month per endpoint.

### Sample Monthly Costs

These estimates cover compute costs only. Add ~$250-300/month per region for fixed infrastructure (EKS cluster fee, NAT Gateways, ALB, Global Accelerator). Storage and data transfer costs are additional.

| Deployment | Config | Compute Cost | Total (with infra) |
|-----------|--------|-------------|-------------------|
| Small | 1 region, 2× g5.xlarge spot (2 GPUs) | ~$450-600/mo | ~$700-900/mo |
| Medium | 2 regions, 8× g5.2xlarge spot (8 GPUs), EFS + FSx | ~$2,100-2,800/mo | ~$3,000-4,000/mo |
| Large | 4 regions, 4× p4d.24xlarge spot (32 GPUs total), EFS + FSx | ~$29,000-38,000/mo | ~$31,000-40,000/mo |

Costs vary significantly by instance type, spot availability, and utilization. Use `gco costs summary` and `gco costs forecast` for actual spend tracking.

---

**Need Help?** Check [TROUBLESHOOTING.md](TROUBLESHOOTING.md) or open a [GitHub issue](https://github.com/awslabs/global-capacity-orchestrator-on-aws/issues).
