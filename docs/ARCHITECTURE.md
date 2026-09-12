# Architecture Documentation

## Table of Contents

- [Overview](#overview)
- [Components](#components)
  - [Global Layer](#1-global-layer)
  - [Regional Layer](#2-regional-layer)
  - [Global API Gateway Layer](#3-global-api-gateway-layer)
  - [Kubernetes Layer](#4-kubernetes-layer)
  - [Lambda Layer](#5-lambda-layer)
- [Data Flow](#data-flow)
  - [Manifest Submission](#manifest-submission-aws-partition-global-path)
  - [Authentication Flow](#authentication-flow)
  - [Node Provisioning](#node-provisioning-eks-auto-mode)
- [Security Architecture](#security-architecture)
  - [Network Security](#network-security)
  - [IAM Security](#iam-security)
  - [Data Security](#data-security)
- [Scalability](#scalability)
  - [Horizontal Scaling](#horizontal-scaling)
  - [Vertical Scaling](#vertical-scaling)
  - [Regional Scaling](#regional-scaling)
- [High Availability](#high-availability)
  - [Regional HA](#regional-ha)
  - [Application HA](#application-ha)
  - [Global HA](#global-ha)
- [Cost Optimization](#cost-optimization)
- [Disaster Recovery](#disaster-recovery)
- [Shared Storage (EFS)](#shared-storage-efs)
- [Scale Potential](#scale-potential-capacity-envelope)

## Overview

GCO (Global Capacity Orchestrator on AWS) is a multi-region Kubernetes platform built on AWS [EKS Auto Mode](https://docs.aws.amazon.com/eks/latest/userguide/automode.html), designed for AI/ML workload orchestration with GPU support.

> **Looking for the *why*?** This document describes *what* the architecture is. The reasoning behind significant decisions — the trade-offs, the alternatives, and the context that forced each choice — is recorded in the [Architecture Decision Records](adr/README.md).

## Components

### 1. Global Layer

**AWS [Global Accelerator](https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html)** (commercial `aws` partition only)

- Private acceleration plane behind the IAM-authenticated global API
- Registers each region's internal platform ALB as an endpoint
- Exposes only a TCP/443 listener; it is a Layer 4 pass-through and never terminates TLS
- Automatic health-based regional routing and failover
- DDoS protection via AWS Shield
- Carries proxy-signed HTTPS requests over the AWS network to the ALB TLS listener

Other AWS partitions omit the accelerator, listener, endpoint groups, and
registration resources. Their global stack retains shared state and registries,
while workload traffic uses the IAM-authenticated regional API bridges.

### 2. Regional Layer

Each region contains:

**VPC Configuration**

- Spans every supported Availability Zone in the region (one public + one private subnet per AZ)
- Public subnets host NAT gateways; the platform ALB is not internet-facing
- Private subnets host EKS nodes, VPC Lambdas, and the internal platform ALB
- 2 NAT Gateways for high availability
- VPC endpoints for AWS services
- VPC Flow Logs enabled (CloudWatch Logs, 30-day retention)

**EKS Auto Mode Cluster**

- Kubernetes 1.36
- Managed control plane
- Private API endpoint by default; public API access is disabled by the stock configuration
- Control plane logging enabled (API, Audit, Authenticator, Controller Manager, Scheduler)
- Auto-scaling compute via built-in and custom NodePools:
  - `system`, `general-purpose`: EKS Auto Mode built-ins
  - `gpu-x86-pool`: NVIDIA x86 GPU workloads
  - `gpu-arm-pool`: NVIDIA ARM64 GPU workloads
  - `gpu-inference-pool`: long-running inference workloads
  - `gpu-efa-pool`: EFA-enabled distributed GPU workloads
  - `mooncake-efa-pool`: EFA-enabled disaggregated inference
  - `neuron-pool`: AWS [Inferentia](https://aws.amazon.com/ai/machine-learning/inferentia/) and [Trainium](https://aws.amazon.com/ai/machine-learning/trainium/) workloads
  - `cpu-general-pool`: general CPU workloads with project-specific limits

**Application Load Balancer**

- One internal application ALB per region, provisioned by the AWS Load Balancer Controller from the `gco-system/gco-gateway` Gateway API resources (`GatewayClass`, `LoadBalancerConfiguration`, `TargetGroupConfiguration`, `Gateway`, `HTTPRoute`)
- HTTPS/443 listener with a short-lived regional ACM leaf issued by the deployment-local private root
- Leaf identity is `backend.<project>.gco.internal`; backend clients send and verify it through explicit SNI while connecting to dynamic accelerator or ALB DNS names
- Registered with Global Accelerator when the deployment partition is `aws`, and recorded in the global-region SSM registry in every partition
- Routes `/api/v1/*` and `/inference/*` through authenticated platform services via the shared `HTTPRoute`
- Ownership is verified by account, region, load-balancer type/scheme, EKS cluster tags, and the exact `gco.aws/gateway` ownership tag before a regional proxy forwards traffic
- Terminates private-root TLS, then re-encrypts target traffic to TLS-only proxy sidecars on pod port 8443 with HTTPS `/healthz` target-group checks. Each sidecar hot-reloads its projected leaf and forwards only over pod loopback; ALB does not validate the self-signed workload leaves. HMAC proves trusted-proxy key possession and request integrity on protected paths, while API Gateway IAM authenticates the original caller.

**Regional API Gateway Bridge** (separate stack)

- Created in every workload region because the centralized aggregator cannot join arbitrary regional VPCs
- Regional REST API uses AWS-managed TLS and IAM authentication ([SigV4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html))
- Its resource policy always admits the exact aggregator role
- In the commercial `aws` partition, `api_gateway.regional_api_enabled=true` additionally admits IAM-authorized principals from the deployment account for direct region-pinned access
- In other partitions, same-account direct access is enabled automatically because this bridge is the supported workload ingress when Global Accelerator is absent
- The buffered Python VPC Lambda resolves and verifies the internal ALB from `/<project>/alb-hostname-<region>` for `/api/v1/*`
- A separate Node.js 24 VPC Lambda applies the same HMAC, private-root TLS, and ALB-ownership controls while streaming `/inference/*` responses
- Neither path requires a VPC Link or Network Load Balancer

**Amazon EFS (Elastic File System)**

- Shared storage accessible by all pods in the cluster
- Encrypted at rest (AWS KMS) and in transit (TLS)
- Dynamic provisioning via EFS CSI Driver with `basePath: "/dynamic"`
- Each PVC automatically gets its own access point (UID/GID: 1000, permissions: 755)
- EFS CSI Driver add-on with [IRSA](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html) for secure access
- PersistentVolumeClaim `gco-shared-storage` available in `default`, `gco-jobs`, and `gco-system` namespaces

**Amazon [FSx for Lustre](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html)** (Optional)

- High-performance parallel file system for ML training workloads
- Encrypted at rest by default (AWS-managed keys)
- Enable via: `gco stacks fsx enable`
- Static provisioning with pre-created PersistentVolumes bound to each namespace
- PersistentVolumeClaim `gco-fsx-storage` available in `default`, `gco-jobs`, and `gco-system` namespaces when enabled
- Supports S3 data repository integration for seamless data import/export

### 3. Global API Gateway Layer

**Global API Gateway** (gco-api-gateway stack)

- Single authenticated aggregation entry point in every partition
- IAM authentication (SigV4) required for all requests
- In the commercial `aws` partition, an edge-optimized API adds buffered `/api/v1/*` and streaming `/inference/*` proxy paths through Global Accelerator
- In other partitions, a regional API exposes only `/api/v1/global/*` aggregation routes; callers use each workload region's IAM API for control-plane and inference traffic

**Lambda Proxy**

- Retrieves the backend HMAC signing key from [Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html) through a bounded cache
- Reads only the public private-root trust bundle from project-scoped SSM
- Allowlists supported end-to-end headers
- Signs the version, timestamp, nonce, method, exact path/query, and body digest
- Never transmits the reusable signing key
- Uses strict private-root TLS through Global Accelerator with explicit SNI/hostname assertion when the deployment partition is `aws`
- Is not created outside `aws`; regional VPC proxies provide the equivalent HMAC and private-root TLS hop
- Retries only safe read-only methods

**Cross-Region Aggregator**

- Discovers deterministic `<project>-regional-api-<region>` CloudFormation stacks and their `RegionalApiEndpoint` outputs
- Validates each endpoint as that region's AWS `execute-api` HTTPS `/prod` URL
- Signs every regional request with SigV4 and uses the AWS-managed API Gateway TLS chain
- Fails closed when any required bridge cannot be discovered; bounded stale discovery is allowed only within the configured process cache window
- Never reads the HMAC secret, ALB-hostname registry, public private-root trust bundle, or root secret; each regional VPC proxy owns the HMAC/private-root ALB hop

### 4. Kubernetes Layer

**Namespaces:**

- `gco-system`: Platform services (health monitor, manifest processor, inference monitor, inference proxy, and cost monitor)
- `gco-jobs`: User batch and training workloads submitted through the control API
- `gco-inference`: Managed model-serving workloads reconciled by the inference monitor

**Health Monitor Service**

- 2 replicas for high availability
- Pod anti-affinity spreads replicas across nodes/AZs
- PodDisruptionBudget ensures at least 1 replica during disruptions
- Monitors cluster and workload health
- Exposes `/healthz` and `/readyz` endpoints
- Reports metrics to CloudWatch

**Manifest Processor Service**

- 3 replicas for high throughput
- Pod anti-affinity spreads replicas across nodes/AZs
- PodDisruptionBudget ensures at least 2 replicas during disruptions
- Validates and processes manifest submissions
- Queues manifests for application
- Tracks manifest lifecycle

**Inference Proxy Service**

- 3 replicas and a PodDisruptionBudget with at least 2 available
- Own image, ServiceAccount, IAM role, NetworkPolicies, and ClusterIP Service
- Validates the HMAC envelope and serving-path allowlist
- Reads only the exact endpoint record from DynamoDB and streams model responses
- Has no Kubernetes RoleBinding and shares no worker lifecycle with the manifest processor

**Cost Monitor Service** (when cost monitoring is enabled — the default)

- Single-replica `Recreate` Deployment: the scheduled reporter is a singleton writer with deterministic per-window report keys, so restarts and rollouts converge instead of double-counting
- Queries the in-cluster [OpenCost](https://opencost.io/) allocation API and writes interval-aligned [Parquet](https://parquet.apache.org/docs/) reports to the central cost report bucket in the monitoring region
- Serves report listing and ad-hoc generation through the manifest API's authenticated `/api/v1/cost/*` proxy
- IRSA role scoped to the deterministic cost report bucket ARN plus `kms:ViaService`-conditioned key use; no Kubernetes RBAC binding
- Default-deny ingress with an explicit allow from the manifest processor only

**Service Accounts & RBAC**

- `gco-health-monitor-sa`: Read-only cluster health plus narrowly named self-healing resources
- `gco-manifest-processor-sa`: Job-namespace Kubernetes writes and control-plane table access
- `gco-inference-monitor-sa`: Inference-namespace reconciliation permissions
- `gco-inference-proxy-sa`: Exact AWS secret/endpoint-read access and no Kubernetes RBAC binding
- `gco-cost-monitor-sa`: Cost report bucket write access only; no Kubernetes RBAC binding
- `gco-service-account`: General job and inference workload identity

### 5. Lambda Layer

**kubectl Applier Lambda**

- Python 3.14 runtime
- Runs in VPC private subnets
- Security group allows access to EKS cluster
- IAM role with EKS cluster admin access
- Applies Kubernetes manifests during stack deployment

**Helm Installer (Step Functions)**

- State machine with one task per Helm chart in `charts.yaml` order
- Each chart task invokes a Docker-based Lambda (kubectl + helm + awscli)
- Per-chart retry (4 attempts, 30-second initial interval, exponential
  backoff, 5-min max delay)
- 16-minute timeout per chart task; 2-hour execution timeout overall
- The custom-resource provider is fire-and-forget: its `on_event` handler
  starts the execution and returns, so CloudFormation never waits on Helm.
  Stack deletion is the exception — a delete-only provider waits on a
  reverse-order teardown state machine so releases that own webhooks and
  load balancers are gone before the cluster is
- Eliminates the old single-Lambda 15-minute ceiling — slow charts
  (cold image pulls) retry independently without failing the deploy
- Charts installed in dependency order (`lambda/helm-installer/charts.yaml`
  is the source of truth; the toggles live under `helm.<chart>.enabled` in
  `cdk.json`):
  - AWS Load Balancer Controller (creates the shared ALB from the Gateway API resources)
  - [KEDA](https://keda.sh/) (mandatory)
  - AWS [EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html) and Neuron device plugins
  - [Volcano](https://volcano.sh/) and KubeRay
  - [cert-manager](https://cert-manager.io/docs/)
  - Slurm/Slinky and [YuniKorn](https://yunikorn.apache.org/) only when their opt-in flags are enabled
  - kube-prometheus-stack when cluster observability is enabled
  - OpenCost when cost monitoring is enabled (after kube-prometheus-stack,
    whose [Prometheus](https://prometheus.io/docs/introduction/overview/) Operator CRDs its ServiceMonitor needs)
  - MLflow when cluster observability is enabled and `cluster_observability.mlflow.enabled` is left on
  - Kubeflow Trainer
  - [Kueue](https://kueue.sigs.k8s.io/) last, after its dependencies

**Function Flow:**

1. CloudFormation triggers Lambda via Custom Resource
2. Lambda generates EKS authentication token
3. Connects to EKS private endpoint
4. Applies manifests from embedded directory
5. Reports success/failure to CloudFormation

## Data Flow

### Manifest Submission (`aws` partition global path)

```text
User → API Gateway (IAM Auth, AWS-managed TLS) → Lambda Proxy
  → Global Accelerator (TCP/443 pass-through) → Internal Regional ALB (private-root TLS)
  → Gateway API HTTPRoute → Manifest Processor Pod (HTTP target group)
  → Kubernetes API → Workload Scheduled → Node Provisioned
```

### Inference Invocation (`aws` partition global path)

```text
User → API Gateway (IAM Auth, AWS-managed TLS) → Streaming Inference Lambda
  → Global Accelerator (TCP/443 pass-through) → Internal Regional ALB (private-root TLS)
  → Gateway API HTTPRoute → Dedicated Inference Proxy Pod (HTTP target group)
  → Endpoint ClusterIP Service → Model Pod → streamed response
```

Outside the commercial `aws` partition, control-plane and inference requests use
the selected regional API Gateway directly; its VPC Lambda performs the same
HMAC-signed, private-root-TLS hop to the internal ALB.

### Authentication Flow

```text
User Request (SigV4 signed) → API Gateway (AWS-managed TLS + IAM Auth)
  → Lambda Proxy retrieves the HMAC signing key and public root bundle
  → Lambda signs the exact backend request with a short-lived envelope
  → Private-root TLS traverses Global Accelerator unchanged to the ALB (`aws` partition)
  → or the regional API's VPC Lambda connects directly to the ALB (other partitions)
  → Backend middleware validates freshness, integrity, body digest, and nonce replay
  → Manifest Processor handles `/api/v1/*`; Inference Proxy handles `/inference/*`
```

For `/api/v1/global/*`, the API invokes the aggregator instead. The aggregator
uses AWS-managed TLS and SigV4 to each regional API Gateway; that bridge's VPC
Lambda then performs the HMAC-signed, private-root-TLS hop to its internal ALB.

### Node Provisioning (EKS Auto Mode)

```text
Pod Pending → Karpenter detects unschedulable pod
  → Evaluates nodepool requirements
  → Provisions EC2 instance matching requirements
  → Joins instance to cluster
  → Pod scheduled on new node
```

## Security Architecture

### Compliance Frameworks

GCO synthesizes five [cdk-nag](https://github.com/cdklabs/cdk-nag) policy-validation rule packs:

- **AWS Solutions**: Best practices for AWS architectures
- **HIPAA Security**: Healthcare compliance requirements
- **NIST 800-53 Rev 5**: Federal security controls
- **PCI DSS 3.2.1**: Payment card industry standards
- **Serverless**: Best practices for serverless architectures

The rule packs run during `cdk synth` and deployment. They are automated control checks, not certifications. Acknowledgments are documented in `gco/stacks/nag_suppressions.py` with a scoped reason for each accepted finding.

### Network Security

**Layers of Defense:**

1. API Gateway IAM authorization, account-scoped resource policy, WAF, and throttling
2. Request-bound HMAC authentication between trusted proxies and backend services
3. Internal ALB and private-subnet isolation
4. Security groups, Kubernetes NetworkPolicies, and RBAC

**EKS Cluster Security:**

- Private endpoint enabled
- Public endpoint disabled by default
- Cluster security group controls VPC access
- Pod security controls and admission-time workload validation enforced

**In-cluster network isolation** (`lambda/kubectl-applier-simple/manifests/03-network-policies.yaml`, enforced on EKS Auto Mode by the network policy controller that `06-network-policy-controller.yaml` switches on — `cdk.json` `eks_cluster.network_policy_enforcement`, default `true` — and by Calico in the kind CI job):

| Namespace | Ingress | Egress |
|-----------|---------|--------|
| `gco-system` | Default deny. Each platform Deployment is admitted on exactly the port it serves: 8443 (TLS proxy sidecars, targeted by the ALB) for health-monitor, manifest-processor, inference-proxy; 9090 (Prometheus metrics) for inference-monitor; 8080 from the manifest processor only for cost-monitor | DNS, HTTPS (AWS APIs, Kubernetes API), the inference proxy's path to `gco-inference` model pods, the cost monitor's path to OpenCost |
| `gco-jobs` | Default deny from other namespaces; every pod in the namespace may reach every other pod on any port (distributed training, Ray, Volcano, Slurm, Kubeflow choose their own ports). With Slurm enabled, the Slinky operator's namespace is admitted to slurmrestd's port 6820, the API it reconciles NodeSets through | DNS, HTTPS to any destination (S3, DynamoDB, ECR, CloudWatch, Bedrock, model hubs, package indexes — GCO's own tables and shared bucket live in the global region, so a VPC-only rule could never carry the platform's traffic), the in-VPC ranges from `vpc_endpoint_cidrs` on any port (Valkey, Aurora, EFS/FSx, VPC endpoints), plus the opt-in MLflow and Slurm client rules |
| `gco-inference` | Model pods accept traffic only from the authenticated inference proxy and from each other (Mooncake KV-transfer, PD proxy) | DNS, HTTPS (model pulls, AWS APIs), the Mooncake master ports |

Rules on probed ports name the port but no source: the ALB is not a pod and the kubelet probes from the node's host network, which no selector can express. DNS rules likewise allow port 53 to any destination because Auto Mode answers cluster DNS from a per-node service rather than CoreDNS pods. NetworkPolicies are additive, so an operator who needs a path GCO does not ship adds a policy rather than switching enforcement off.

One VPC CNI property shapes how egress rules and Services are written. The agent evaluates a pod's egress before kube-proxy translates a ClusterIP to a pod address, so an egress rule whose peers are pods admits the pods' addresses but not the ClusterIP in front of them; the network policy controller adds a Service's ClusterIP only when that Service's `spec.selector` matches the rule's `podSelector` (headless Services resolve to pod addresses and need nothing). Every ClusterIP Service GCO's inference monitor creates therefore carries `gco.io/type: inference` in its selector — the label the `allow-inference-proxy-to-inference` and `allow-inference-internal` peers select on — and a new egress rule that must reach a Service through its ClusterIP has to name a `podSelector` the Service's own selector satisfies (or admit the address range). Endpoints created before this label existed keep their old selector; re-deploying the endpoint refreshes it. The first live run with enforcement on found this the hard way: a healthy model pod answered the kubelet while the inference proxy's requests to its Service timed out.

**VPC endpoints** (`cdk.json` `vpc_endpoints`): each regional VPC gets free S3 and DynamoDB gateway endpoints by default, so the platform's largest data path (models, datasets, checkpoints, MLflow artifacts, cost reports) stays inside the VPC and off the NAT gateways' per-GB metering. Interface (PrivateLink) endpoints for STS, ECR, CloudWatch, SQS, SSM, Secrets Manager, KMS, EKS, EFS, and Bedrock are opt-in because they bill per AZ-hour. Cross-region calls to the global region's tables, buckets, and parameters still leave through the NAT gateways.

### IAM Security

**Principle of Least Privilege:**

- Lambda Role: EKS describe + cluster admin access entry
- Service Account: Kubernetes RBAC-controlled
- API Gateway: IAM authentication required
- Users: Explicit access entries required

**Access Entry Model:**

- No aws-auth ConfigMap
- IAM principals explicitly granted access
- Policy-based permissions (AmazonEKSClusterAdminPolicy)
- Audit trail via CloudTrail

### Data Security

- **At Rest**: EBS volumes and EFS encrypted with AWS KMS
- **Client and AWS API Transit**: AWS-managed TLS protects API Gateway and AWS service API connections; aggregator-to-regional-API calls also require SigV4
- **Private Backend Transit**: In `aws`, global proxy → Global Accelerator → ALB uses deployment-local private-root TLS; in every partition, regional VPC proxy → ALB uses the same trust and explicit `backend.<project>.gco.internal` SNI/hostname verification. Global Accelerator is Layer 4 and does not terminate TLS.
- **Workload Target Transit**: The ALB terminates its private-root client connection and re-encrypts every target hop to cert-manager-backed HTTPS listeners on health-monitor, manifest-processor, and inference-proxy. ALB target TLS encrypts traffic but does not validate the deployment-local self-signed workload certificates and is not mTLS.
- **Private-Key Boundary**: Only the certificate-manager role can read the customer-managed-KMS-encrypted root secret; backend clients read public SSM trust only
- **Request Authentication**: HMAC adds integrity, freshness, and replay defense, not encryption
- **EFS Transit**: TLS-enabled mounts
- **Secrets**: Kubernetes secrets encrypted in etcd
- **Logs**: CloudWatch Logs encrypted

## Scalability

### Horizontal Scaling

**Application Layer:**

- Health Monitor: fixed 2 replicas (webhook delivery and ALB sync are
  leader-elected through Kubernetes Leases, so the second pod is a hot standby);
  Inference Monitor: 2 (leader + standby); Cost Monitor: 1 — no HPAs, these are
  control loops, not request-serving tiers
- Manifest Processor: `cdk.json` `manifest_processor.replicas` (default 3),
  with an opt-in CPU HorizontalPodAutoscaler (`manifest_processor.autoscaling`,
  off by default because the API tier is I/O-bound and every replica also runs
  the central queue worker)
- Inference Proxy: `inference_proxy.min_replicas`–`max_replicas` (default 3–10)
  via the `inference-proxy-hpa` HorizontalPodAutoscaler on application CPU and
  memory plus TLS-sidecar CPU
- User workload scale is bounded by configured NodePool limits, Kubernetes quotas, AWS service quotas, and available EC2 capacity

**Compute Layer:**

- EKS Auto Mode automatically provisions nodes
- nodepool limits configurable per instance type
- Supports 1000s of pods per cluster

### Vertical Scaling

**Cluster Limits:**

- Control plane: Fully managed by AWS
- Nodes: Up to 100,000 per cluster (EKS limit)
- Pods: 110 per node (default)

### Regional Scaling

- Add configured regional stacks independently after the global control-plane stacks exist
- In `aws`, Global Accelerator registration plus the global-region SSM registry connect each regional backend to the shared API path
- In other partitions, the SSM registry and regional IAM APIs connect clients and the aggregator without Global Accelerator
- A regional compute failure does not require another regional cluster to remain healthy

## High Availability

### Regional HA

- **Multi-AZ networking**: The VPC spans every supported AZ; EKS and the internal ALB use multi-AZ infrastructure
- **NAT Gateways**: 2 for redundancy
- **ALB**: Multi-AZ by default
- **EKS Control Plane**: Multi-AZ managed by AWS

### Application HA

- **Multiple Replicas**: Every request-path or reconciliation service runs 2+ replicas; the cost monitor is the one single-replica service (a periodic reporter whose restart loses nothing)
- **Pod Anti-Affinity**: Spreads pods across nodes (preferred scheduling)
- **Topology Spread Constraints**: Distributes every multi-replica platform service (health monitor, manifest processor, inference monitor, inference proxy) across availability zones and nodes
- **Pod Disruption Budgets**: `maxUnavailable: 1` on every multi-replica platform Deployment, so one voluntary disruption at a time whatever the replica count (a `minAvailable` budget on an autoscaled Deployment would widen as the HPA scales up); the single-replica cost monitor carries `karpenter.sh/do-not-disrupt` instead
- **Health Checks**: Startup, liveness, and readiness probes on every container
- **Graceful Shutdown**: preStop hooks plus a uvicorn drain budget (`GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS`) inside `terminationGracePeriodSeconds` let in-flight requests and streams complete
- **Rolling Updates**: Zero-downtime deployments with maxUnavailable=0, one surge pod, and three retained revisions
- **Auto-Healing**: Kubernetes restarts failed pods

### Global HA

- **Multi-Region**: Deploy to 2+ regions
- **Commercial `aws` partition**: Global Accelerator provides health-based routing and failover
- **Other partitions**: The aggregate API can query every required regional bridge, but direct regional callers select a region explicitly; no accelerator-based automatic failover is claimed

## Cost Optimization

### Compute Costs

- **EKS Auto Mode**: Pay only for provisioned nodes
- **[Karpenter](https://karpenter.sh/)**: Efficient bin-packing
- **Spot Instances**: Supported for fault-tolerant workloads
- **ARM Instances**: 20% cost savings for compatible workloads

### Network Costs

- **VPC Endpoints**: Reduce NAT Gateway costs
- **Private Subnets**: Minimize data transfer
- **Regional Deployment**: Keep traffic within region

### Storage Costs

- **EBS**: gp3 volumes (cost-effective)
- **EFS**: Pay-per-use elastic storage (no pre-provisioning)
- **ECR**: Lifecycle policies for image cleanup
- **Logs**: Retention policies to control costs

### Observability Costs

- **Cluster observability is on by default** — each regional cluster runs
  `kube-prometheus-stack`, whose standing cost is the gp3 EBS volumes backing
  Prometheus (default `50Gi`), [Grafana](https://grafana.com/docs/grafana/latest/) (`10Gi`), and Alertmanager (`5Gi`).
- **Retention-bounded**: `cluster_observability.prometheus.retention` (default
  `15d`) caps how much of the TSDB volume fills; persistence sizes are
  configurable per component.
- **No load balancer**: Grafana is private (`ClusterIP`, no ALB), so there are no
  load-balancer hours — access is via `gco monitoring open` port-forward.
- **Opt out** with `gco monitoring disable` to remove the stack and its volumes.
  See [`docs/MONITORING.md`](MONITORING.md#cost) for the full breakdown.

### Cost Monitoring & Cost-Aware Scheduling

- **Per-cluster allocation**: OpenCost (one pod per region) allocates node and
  volume list prices to namespaces from Prometheus usage data, rendered in the
  *GCO Cost (OpenCost)* Grafana dashboard.
- **Durable analytics**: the per-region cost-monitor service writes
  interval-aligned Parquet reports to a central, lifecycle-managed S3 bucket;
  a Glue table with partition projection plus an [Athena](https://docs.aws.amazon.com/athena/latest/ug/what-is.html) workgroup make them
  queryable across regions (`gco costs k8s …`) with no dashboard server or
  crawler.
- **Spot price gating**: central-queue jobs may carry a max spot price for an
  instance type; the regional worker defers dispatch until the market clears
  the cap — without blocking other queued work.
- On by default alongside cluster observability; see
  [`docs/COST_MONITORING.md`](COST_MONITORING.md).

## Disaster Recovery

### Backup Strategy

- **EKS**: Control plane backed up by AWS
- **Manifests**: Stored in Lambda package (version controlled)
- **Application State**: User responsibility

### Recovery Procedures

**Regional Failure:**

1. In `aws`, Global Accelerator routes new backend requests to another healthy registered region; elsewhere callers select another healthy regional API endpoint
2. Operators investigate and restore the failed regional stack
3. Actual recovery time depends on health-check convergence or client failover, workload state, and replacement capacity; no fixed sub-minute RTO is guaranteed

**Cluster Failure:**

1. Redeploy the regional stack: `gco stacks deploy gco-REGION -y`
2. Manifests automatically reapplied
3. RTO: under 1 hour

**Complete Failure:**

1. Deploy to new region
2. Update Global Accelerator
3. RTO: under 1 hour

## Shared Storage (EFS)

### Overview

Amazon EFS provides shared, persistent storage for all pods in the cluster. This enables:

- Job outputs that persist after pod termination
- Data sharing between pods and jobs
- Checkpoint storage for ML training workloads

### Architecture

```text
┌─────────────────────────────────────────────────────────┐
│                    EFS File System                      │
│                  (Encrypted at rest)                    │
│  ┌─────────────────────────────────────────────────┐    │
│  │  Access Point: /gco-jobs                        │    │
│  │  - UID/GID: 1000                                │    │
│  │  - Permissions: 755                             │    │
│  └─────────────────────────────────────────────────┘    │
└────────────────────┬────────────────────────────────────┘
                     │ TLS (encryption in transit)
                     │
┌────────────────────▼────────────────────────────────────┐
│              EFS CSI Driver (IRSA)                      │
│  - Runs in kube-system namespace                        │
│  - Uses IAM role for secure access                      │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│           PersistentVolumeClaim                         │
│  - Name: gco-shared-storage                             │
│  - Available in: default, gco-jobs, gco-system          │
│  - Access Mode: ReadWriteMany                           │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│                    Pods                                 │
│  - Mount at /outputs or custom path                     │
│  - Read/write access for all pods                       │
└─────────────────────────────────────────────────────────┘
```

### Usage

Jobs can mount the shared storage to persist outputs:

```yaml
spec:
  containers:
  - name: worker
    volumeMounts:
    - name: shared-storage
      mountPath: /outputs
  volumes:
  - name: shared-storage
    persistentVolumeClaim:
      claimName: gco-shared-storage
```

See `examples/efs-output-job.yaml` for a complete example.

### Security

- **Encryption at Rest**: AWS KMS managed key
- **Encryption in Transit**: TLS via EFS CSI driver
- **Access Control**: File system policy restricts to VPC
- **IRSA**: EFS CSI driver uses IAM role (no static credentials)

## Scale Potential: Capacity Envelope

GCO scales by adding regional EKS stacks, but there is no defensible fixed
"regions × EKS maximum" job count. A deployable capacity estimate must use the
regions actually configured and the lowest applicable limit at each layer.

### Request Path

The stock global API stage is configured for 1,000 requests/second with a
2,000-request burst (both configurable in `cdk.json`). That is one shared API
Gateway stage limit; it is **not multiplied by the number of backend regions**.
The WAF per-source-IP rate rule, Lambda concurrency, accelerator health (in
`aws`), regional API capacity, ALB target capacity, manifest-processor replicas,
and Kubernetes API throughput can impose lower limits.

### Compute Path

For each configured region, usable workload capacity is bounded by all of:

- Custom NodePool CPU, memory, architecture, accelerator, and instance-family limits
- EC2 On-Demand/Spot quotas and real-time capacity for the requested instance types
- EKS and Kubernetes service quotas
- Namespace/resource quotas and GCO manifest-validation policy
- Storage, network, and scheduler throughput
- Budget and organizational controls

The safe planning formula is therefore:

```text
regional usable capacity = min(NodePool limits, service quotas, available EC2 capacity,
                               workload-policy limits, operational budget)
global usable capacity   = sum(regional usable capacity for configured healthy regions)
```

Use `gco capacity`, AWS Service Quotas, and the deployed NodePool manifests to
measure those inputs. Request quota increases and validate load incrementally;
do not treat an AWS theoretical cluster maximum or the current count of AWS
regions as deployable capacity.
