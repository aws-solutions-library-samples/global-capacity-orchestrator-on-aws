# Helm Installer

Installs and manages Helm charts on EKS clusters during CDK deployment. Supports KEDA, NVIDIA DRA Driver, Volcano, KubeRay, Kueue, and more.

## Table of Contents

- [Trigger](#trigger)
- [How It Works](#how-it-works)
- [Packaging](#packaging)
- [Charts](#charts-from-chartsyaml)
- [CloudFormation Properties](#cloudformation-properties)
- [Environment Variables](#environment-variables)
- [IAM Permissions](#iam-permissions)
- [Dependencies](#dependencies)

## Trigger

CloudFormation Custom Resource — runs on stack Create, Update, and Delete.

## How It Works

### Create/Update

1. Loads default chart configs from `charts.yaml`
2. Merges any CloudFormation property overrides
3. Configures kubeconfig with EKS token authentication
4. Runs `helm upgrade --install` for each enabled chart (with `--wait`)

### Delete

Uninstalls charts in reverse order. Always returns SUCCESS to prevent stuck stacks.

## Packaging

Runs as a container Lambda (see `Dockerfile`). The image includes `helm` and `kubectl` binaries on x86_64.

## Charts (from `charts.yaml`)

| Chart | Namespace | Default |
|-------|-----------|---------|
| KEDA | `keda` | Enabled |
| NVIDIA DRA Driver | `nvidia-dra-driver` | Enabled |
| NVIDIA Network Operator | `nvidia-network-operator` | Enabled |
| AWS EFA Device Plugin | `kube-system` | Enabled |
| Volcano | `volcano-system` | Enabled |
| KubeRay Operator | `ray-system` | Enabled |
| Kueue | `kueue-system` | Enabled (OCI) |

## CloudFormation Properties

| Property | Required | Description |
|----------|----------|-------------|
| `ClusterName` | Yes | EKS cluster name |
| `Region` | Yes | AWS region |
| `Charts` | No | Dict of chart config overrides |
| `EnabledCharts` | No | List of chart names to enable |
| `KedaOperatorRoleArn` | No | IAM role ARN for KEDA IRSA |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CLUSTER_NAME` | Yes | EKS cluster name |
| `REGION` | Yes | AWS region |

## IAM Permissions

- `eks:DescribeCluster` on the EKS cluster
- `sts:GetCallerIdentity` (for EKS token generation)
- Kubernetes RBAC: cluster-admin or equivalent for Helm operations

## Dependencies

- `boto3`, `pyyaml`, `urllib3` (see `requirements.txt`)
- Helm v4.2.1, kubectl v1.36.2 (installed in Docker image)
