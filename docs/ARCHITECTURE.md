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
- [Scale Potential](#scale-potential-back-of-the-envelope-calculation)

## Overview

GCO (Global Capacity Orchestrator on AWS) is a multi-region Kubernetes platform built on AWS EKS Auto Mode, designed for AI/ML workload orchestration with GPU support.

## Components

### 1. Global Layer

**AWS Global Accelerator**

- Single global endpoint for all regions
- Automatic health-based routing
- DDoS protection via AWS Shield
- Reduces latency by routing to nearest healthy region

### 2. Regional Layer

Each region contains:

**VPC Configuration**

- 3 Availability Zones
- Public subnets (24-bit CIDR) for ALB
- Private subnets (24-bit CIDR) for EKS nodes
- 2 NAT Gateways for high availability
- VPC endpoints for AWS services
- VPC Flow Logs enabled (CloudWatch Logs, 30-day retention)

**EKS Auto Mode Cluster**

- Kubernetes 1.36
- Managed control plane
- Control plane logging enabled (API, Audit, Authenticator, Controller Manager, Scheduler)
- Auto-scaling compute via nodepools:
  - `system`: Core Kubernetes components
  - `general-purpose`: Standard workloads
  - `gpu-x86`: NVIDIA GPU instances (g4dn, g5)
  - `gpu-arm`: ARM64 GPU instances (g5g)
  - `inference`: Long-running inference pods (WhenEmpty consolidation)
  - `gpu-efa-pool`: EFA-enabled instances for distributed training (p4d, p5)

**Application Load Balancer**

- Internet-facing
- Security group restricts to Global Accelerator IPs only
- Routes traffic to Kubernetes services via Ingress

**Regional API Gateway** (created by regional stack)

- REST API with regional endpoint
- IAM authentication (SigV4)
- VPC Link to internal NLB for direct service access
- Endpoints:
  - `POST /api/v1/manifests` - Submit manifest
  - `GET /api/v1/manifests` - List manifests
  - `GET /api/v1/health` - Health check

**Network Load Balancer (Internal)**

- Private subnets only
- Routes regional API Gateway → Kubernetes services
- Cross-zone load balancing enabled

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
- Lambda proxy adds secret header for backend validation
- Forwards requests to Global Accelerator

**Lambda Proxy**

- Retrieves auth secret from Secrets Manager
- Adds X-GCO-Auth-Token header to requests
- Forwards to Global Accelerator endpoint

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
- Charts installed: KEDA (mandatory), kueue, Volcano, KubeRay

**Function Flow:**

1. CloudFormation triggers Lambda via Custom Resource
2. Lambda generates EKS authentication token
3. Connects to EKS private endpoint
4. Applies manifests from embedded directory
5. Reports success/failure to CloudFormation

## Data Flow

### Manifest Submission

```text
User → API Gateway (IAM Auth) → Lambda Proxy → Global Accelerator
  → Regional ALB → Kubernetes Ingress → Manifest Processor Pod
  → Kubernetes API → Workload Scheduled → Node Provisioned
```

### Authentication Flow

```text
User Request (SigV4 signed) → API Gateway (IAM Auth)
  → Validates SigV4 signature and IAM permissions
  → Lambda Proxy retrieves secret from Secrets Manager
  → Lambda adds X-GCO-Auth-Token header
  → Request forwarded to Global Accelerator
  → Regional ALB validates secret header
  → Manifest Processor processes request
```

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

GCO is validated against multiple compliance frameworks using CDK-nag:

- **AWS Solutions**: Best practices for AWS architectures
- **HIPAA Security**: Healthcare compliance requirements
- **NIST 800-53 Rev 5**: Federal security controls
- **PCI DSS 3.2.1**: Payment card industry standards
- **Serverless**: Best practices for serverless architectures

All compliance checks run during `cdk synth` and deployment. Suppressions are documented in `gco/stacks/nag_suppressions.py` with justifications for each exception.

### Network Security

**Layers of Defense:**

1. Global Accelerator (DDoS protection)
2. ALB Security Group (Global Accelerator IPs only)
3. VPC isolation (private subnets)
4. Security groups (least-privilege)

**EKS Cluster Security:**

- Private endpoint enabled
- Public endpoint enabled (for kubectl access)
- Cluster security group controls access
- Pod security standards enforced

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
- **In Transit**: TLS 1.2+ for all connections (including EFS mounts)
- **Secrets**: Kubernetes secrets encrypted in etcd
- **Logs**: CloudWatch Logs encrypted

## Scalability

### Horizontal Scaling

**Application Layer:**

- Health Monitor: 2-10 replicas (HPA)
- Manifest Processor: 3-20 replicas (HPA)
- User workloads: Unlimited (within nodepool limits)

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

- Deploy to additional regions independently
- Global Accelerator automatically includes new regions
- No cross-region dependencies

## High Availability

### Regional HA

- **Multi-AZ**: All components span 3 AZs
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

## Disaster Recovery

### Backup Strategy

- **EKS**: Control plane backed up by AWS
- **Manifests**: Stored in Lambda package (version controlled)
- **Application State**: User responsibility

### Recovery Procedures

**Regional Failure:**

1. Global Accelerator routes to healthy region
2. No manual intervention required
3. RTO: < 1 minute

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

## Scale Potential: Back-of-the-Envelope Calculation

This section provides a theoretical upper bound for GCO's throughput when deployed globally. These numbers illustrate why a multi-region orchestration platform matters for large-scale AI/ML workloads.

### Assumptions

| Parameter | Value | Notes |
|-----------|-------|-------|
| AWS Regions | 34 | Commercial regions (38 total minus 2 for GovCloud and 2 for Sovereign) |
| Nodes per cluster | 100,000 | EKS hard limit |
| GPUs per g5.xlarge | 1 | Single A10G GPU |
| GPUs per g5.48xlarge | 8 | Eight A10G GPUs |

### Maximum Concurrent GPU Jobs (Global)

**Conservative estimate (g5.xlarge, 1 GPU each):**

```text
34 regions × 100,000 nodes × 1 GPU = 3,400,000 concurrent GPU jobs
```

**High-density estimate (g5.48xlarge, 8 GPUs each):**

```text
34 regions × 100,000 nodes × 8 GPUs = 27,200,000 concurrent GPUs
```

### Submission Throughput (assuming 1,000 manifests/sec)

```text
34 regions × 1,000 manifests/sec = 34,000 job submissions/second
```

### What This Means

| Metric | Single Region | Full Global (34 Regions) |
|--------|---------------|--------------------------|
| Max GPU nodes | 100,000 | 3,400,000 |
| Job submission rate | 1,000/sec | 34,000/sec |

### Real-World Considerations

These are theoretical maximums. Actual limits depend on:

- **AWS Service Quotas**: Default limits are much lower; requires quota increases
- **EC2 Capacity**: GPU instance availability varies by region and time
- **Cost**: Running at full scale would cost millions per hour
- **nodepool Limits**: Current config limits GPU pools to 1,000-1,500 vCPUs per region

**Current nodepool limits (per region):**

- `gpu-x86-pool`: 1,000 vCPUs / 4,000Gi memory (~166 g5.xlarge nodes)
- `gpu-arm-pool`: 500 vCPUs / 2,000Gi memory (125 g5g.xlarge nodes)

To increase throughput, increase nodepool limits in the manifests and request AWS quota increases.

### Why This Matters

Traditional single-cluster approaches hit scaling walls quickly. GCO's multi-region architecture means:

1. **No single point of failure** - One region's issues don't affect others
2. **Linear horizontal scaling** - Add regions to add capacity
3. **Geographic distribution** - Run jobs closer to data sources
4. **Capacity arbitrage** - Route to regions with available GPU capacity
