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
  - [Manifest Submission](#manifest-submission)
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

GCO (Global Capacity Orchestrator on AWS) is a multi-region Kubernetes platform built on AWS EKS Auto Mode, designed for AI/ML workload orchestration with GPU support.

> **Looking for the *why*?** This document describes *what* the architecture is. The reasoning behind significant decisions — the trade-offs, the alternatives, and the context that forced each choice — is recorded in the [Architecture Decision Records](adr/README.md).

## Components

### 1. Global Layer

**AWS Global Accelerator**

- Private acceleration plane behind the IAM-authenticated global API
- Registers each region's internal platform ALB as an endpoint
- Exposes only a TCP/443 listener; it is a Layer 4 pass-through and never terminates TLS
- Automatic health-based regional routing and failover
- DDoS protection via AWS Shield
- Carries proxy-signed HTTPS requests over the AWS network to the ALB TLS listener

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
  - `neuron-pool`: AWS Inferentia and Trainium workloads
  - `cpu-general-pool`: general CPU workloads with project-specific limits

**Application Load Balancer**

- One internal application ALB per region, selected through the platform IngressClass
- HTTPS/443 listener with a short-lived regional ACM leaf issued by the deployment-local private root
- Leaf identity is `backend.<project>.gco.internal`; backend clients send and verify it through explicit SNI while connecting to dynamic accelerator or ALB DNS names
- Registered with Global Accelerator and recorded in the global-region SSM registry
- Routes `/api/v1/*` and `/inference/*` through authenticated platform services
- Ownership is verified by account, region, load-balancer type/scheme, EKS cluster tags, and platform-Ingress tags before a regional proxy forwards traffic
- Terminates private-root TLS; the final ALB-to-Kubernetes-pod target-group hop remains HTTP

**Regional API Gateway Bridge** (separate stack)

- Created in every workload region because the centralized aggregator cannot join arbitrary regional VPCs
- Regional REST API uses AWS-managed TLS and IAM authentication (SigV4)
- Its resource policy always admits the exact aggregator role
- `api_gateway.regional_api_enabled=true` additionally admits IAM-authorized principals from the deployment account for direct region-pinned access; it does not control bridge deployment
- VPC Lambda resolves and verifies the internal ALB from `/<project>/alb-hostname-<region>`
- The Lambda signs the request with HMAC and uses private-root TLS to the ALB
- Proxies `/api/v1/*` and `/inference/*` without a VPC Link or Network Load Balancer

**Amazon EFS (Elastic File System)**

- Shared storage accessible by all pods in the cluster
- Encrypted at rest (AWS KMS) and in transit (TLS)
- Dynamic provisioning via EFS CSI Driver with `basePath: "/dynamic"`
- Each PVC automatically gets its own access point (UID/GID: 1000, permissions: 755)
- EFS CSI Driver add-on with IRSA for secure access
- PersistentVolumeClaim `gco-shared-storage` available in `default`, `gco-jobs`, and `gco-system` namespaces

**Amazon FSx for Lustre** (Optional)

- High-performance parallel file system for ML training workloads
- Encrypted at rest by default (AWS-managed keys)
- Enable via: `gco stacks fsx enable`
- Static provisioning with pre-created PersistentVolumes bound to each namespace
- PersistentVolumeClaim `gco-fsx-storage` available in `default`, `gco-jobs`, and `gco-system` namespaces when enabled
- Supports S3 data repository integration for seamless data import/export

### 3. Global API Gateway Layer

**Global API Gateway** (gco-api-gateway stack)

- Single authenticated entry point for all regions
- IAM authentication (SigV4) required for all requests
- Lambda proxy signs each exact backend request with a short-lived HMAC envelope
- Forwards requests to Global Accelerator

**Lambda Proxy**

- Retrieves the backend HMAC signing key from Secrets Manager through a bounded cache
- Reads only the public private-root trust bundle from project-scoped SSM
- Allowlists supported end-to-end headers
- Signs the version, timestamp, nonce, method, exact path/query, and body digest
- Never transmits the reusable signing key
- Uses strict private-root TLS through Global Accelerator with explicit SNI/hostname assertion
- Retries only safe read-only methods

**Cross-Region Aggregator**

- Discovers deterministic `<project>-regional-api-<region>` CloudFormation stacks and their `RegionalApiEndpoint` outputs
- Validates each endpoint as that region's AWS `execute-api` HTTPS `/prod` URL
- Signs every regional request with SigV4 and uses the AWS-managed API Gateway TLS chain
- Fails closed when any required bridge cannot be discovered; bounded stale discovery is allowed only within the configured process cache window
- Never reads the HMAC secret, ALB-hostname registry, public private-root trust bundle, or root secret; each regional VPC proxy owns the HMAC/private-root ALB hop

### 4. Kubernetes Layer

**Namespaces:**

- `gco-system`: All platform services (health monitor, manifest processor) run here
- `gco-jobs`: User workloads submitted via the API are deployed here

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

**Service Account & RBAC**

- `gco-service-account`: Used by all platform services
- `gco-cluster-role`: Cluster-wide permissions
- Least-privilege access model

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
- Per-chart retry (4 attempts, exponential backoff, 5-min max delay)
- 14-minute timeout per chart task; 2-hour execution timeout overall
- Async custom-resource provider polls the execution every 60 seconds
- Eliminates the old single-Lambda 15-minute ceiling — slow charts
  (cold image pulls) retry independently without failing the deploy
- Charts installed in dependency order:
  - KEDA (mandatory)
  - AWS EFA and Neuron device plugins
  - Volcano and KubeRay
  - cert-manager
  - kube-prometheus-stack when cluster observability is enabled
  - Kueue last, after its dependencies
  - Slurm/Slinky and YuniKorn only when their opt-in flags are enabled

**Function Flow:**

1. CloudFormation triggers Lambda via Custom Resource
2. Lambda generates EKS authentication token
3. Connects to EKS private endpoint
4. Applies manifests from embedded directory
5. Reports success/failure to CloudFormation

## Data Flow

### Manifest Submission

```text
User → API Gateway (IAM Auth, AWS-managed TLS) → Lambda Proxy
  → Global Accelerator (TCP/443 pass-through) → Internal Regional ALB (private-root TLS)
  → Kubernetes Ingress → Manifest Processor Pod (HTTP target group)
  → Kubernetes API → Workload Scheduled → Node Provisioned
```

### Authentication Flow

```text
User Request (SigV4 signed) → API Gateway (AWS-managed TLS + IAM Auth)
  → Lambda Proxy retrieves the HMAC signing key and public root bundle
  → Lambda signs the exact backend request with a short-lived envelope
  → Private-root TLS traverses Global Accelerator unchanged to the ALB
  → Backend middleware validates freshness, integrity, body digest, and nonce replay
  → Manifest Processor processes request
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

GCO synthesizes five cdk-nag policy-validation rule packs:

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
- **Private Backend Transit**: Global proxy → Global Accelerator → ALB and regional VPC proxy → ALB use deployment-local private-root TLS with explicit `backend.<project>.gco.internal` SNI and hostname verification; Global Accelerator is Layer 4 and does not terminate TLS
- **Post-Termination Hop**: ALB target groups use HTTP to Kubernetes pods after the authenticated TLS listener terminates the connection
- **Private-Key Boundary**: Only the certificate-manager role can read the customer-managed-KMS-encrypted root secret; backend clients read public SSM trust only
- **Request Authentication**: HMAC adds integrity, freshness, and replay defense, not encryption
- **EFS Transit**: TLS-enabled mounts
- **Secrets**: Kubernetes secrets encrypted in etcd
- **Logs**: CloudWatch Logs encrypted

## Scalability

### Horizontal Scaling

**Application Layer:**

- Health Monitor: 2-10 replicas (HPA)
- Manifest Processor: 3-20 replicas (HPA)
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
- Global Accelerator registration and the global-region SSM registry connect each regional backend to the shared API path
- A regional compute failure does not require another regional cluster to remain healthy

## High Availability

### Regional HA

- **Multi-AZ networking**: The VPC spans every supported AZ; EKS and the internal ALB use multi-AZ infrastructure
- **NAT Gateways**: 2 for redundancy
- **ALB**: Multi-AZ by default
- **EKS Control Plane**: Multi-AZ managed by AWS

### Application HA

- **Multiple Replicas**: All services have 2+ replicas
- **Pod Anti-Affinity**: Spreads pods across nodes (preferred scheduling)
- **Topology Spread Constraints**: Distributes pods across availability zones
- **Pod Disruption Budgets**: Ensures minimum availability during voluntary disruptions
  - Health Monitor: minAvailable=1
  - Manifest Processor: minAvailable=2
- **Health Checks**: Liveness, readiness, and startup probes
- **Graceful Shutdown**: preStop hooks allow in-flight requests to complete
- **Rolling Updates**: Zero-downtime deployments with maxUnavailable=0
- **Auto-Healing**: Kubernetes restarts failed pods

### Global HA

- **Multi-Region**: Deploy to 2+ regions
- **Global Accelerator**: Automatic failover
- **Health-Based Routing**: Routes away from unhealthy regions

## Cost Optimization

### Compute Costs

- **EKS Auto Mode**: Pay only for provisioned nodes
- **Karpenter**: Efficient bin-packing
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
  Prometheus (default `50Gi`), Grafana (`10Gi`), and Alertmanager (`5Gi`).
- **Retention-bounded**: `cluster_observability.prometheus.retention` (default
  `15d`) caps how much of the TSDB volume fills; persistence sizes are
  configurable per component.
- **No load balancer**: Grafana is private (`ClusterIP`, no ALB), so there are no
  load-balancer hours — access is via `gco monitoring open` port-forward.
- **Opt out** with `gco monitoring disable` to remove the stack and its volumes.
  See [`docs/MONITORING.md`](MONITORING.md#cost) for the full breakdown.

## Disaster Recovery

### Backup Strategy

- **EKS**: Control plane backed up by AWS
- **Manifests**: Stored in Lambda package (version controlled)
- **Application State**: User responsibility

### Recovery Procedures

**Regional Failure:**

1. Global Accelerator routes new backend requests to another healthy registered region
2. Operators investigate and restore the failed regional stack
3. Actual recovery time depends on health-check convergence, workload state, and replacement capacity; no fixed sub-minute RTO is guaranteed

**Cluster Failure:**

1. Redeploy stack: `cdk deploy gco-REGION`
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
The WAF per-source-IP rate rule, Lambda concurrency, Global Accelerator health,
ALB target capacity, manifest-processor replicas, and Kubernetes API throughput
can impose lower limits.

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
