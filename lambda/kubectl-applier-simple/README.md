# Kubectl Applier Simple

Applies Kubernetes manifests to [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) clusters during [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) deployment. Pure Python implementation using the `kubernetes` client library — no Docker or kubectl binary required.

## Table of Contents

- [Trigger](#trigger)
- [How It Works](#how-it-works)
- [Supported Resource Kinds](#supported-resource-kinds)
- [CloudFormation Properties](#cloudformation-properties)
- [Environment Variables](#environment-variables)
- [IAM Permissions](#iam-permissions)
- [Dependencies](#dependencies)
- [Build](#build)

## Trigger

[CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) Custom Resource — runs on stack Create, Update, and Delete.

## How It Works

### Create/Update

1. Configures a Kubernetes client with EKS token authentication
2. Reads all YAML files from the `manifests/` directory (sorted by filename)
3. Replaces placeholders (e.g., image URIs) with values from CloudFormation properties
4. Applies each resource with create-or-patch idempotency. Deployment creates
   retain their manifest `spec.replicas` seed; deployments carrying the
   `gco.aws/hpa-controls-replicas: "true"` ownership annotation omit that field
   from update patches so the HPA's scale-subresource decision is not reset.
5. Restarts key deployments in `gco-system` to pick up new images

### Delete

Always returns SUCCESS to prevent stuck stacks. Optionally skips resource deletion via `SkipDeletionOnStackDelete`.

## Supported Resource Kinds

Namespace, ServiceAccount, ClusterRole, ClusterRoleBinding, Role, RoleBinding, Deployment, DaemonSet, HorizontalPodAutoscaler (`autoscaling/v2`), Service, ConfigMap, Secret, GatewayClass, Gateway, HTTPRoute, LoadBalancerConfiguration, TargetGroupConfiguration, StorageClass, PersistentVolume, PersistentVolumeClaim, PodDisruptionBudget, NetworkPolicy, NodePool, EC2NodeClass, APIService, DeviceClass, ScaledJob, ScaledObject.

## CloudFormation Properties

| Property | Required | Description |
|----------|----------|-------------|
| `ClusterName` | Yes | EKS cluster name |
| `Region` | Yes | AWS region |
| `ImageReplacements` | No | Dict of placeholder → value mappings |
| `SkipDeletionOnStackDelete` | No | If `"true"`, skip resource deletion on stack delete |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CLUSTER_NAME` | Yes | EKS cluster name |
| `REGION` | Yes | AWS region |

## IAM Permissions

- `eks:DescribeCluster` on the EKS cluster
- `sts:GetCallerIdentity` (for EKS token generation)
- Kubernetes RBAC: cluster-admin or equivalent for manifest application

## Dependencies

- `boto3`, `kubernetes`, `PyYAML`, `urllib3` (see `requirements.txt`)

## Build

Requires a build step to package dependencies into `kubectl-applier-simple-build/`:

```bash
rm -rf lambda/kubectl-applier-simple-build
mkdir -p lambda/kubectl-applier-simple-build
cp lambda/kubectl-applier-simple/handler.py lambda/kubectl-applier-simple-build/
cp -r lambda/kubectl-applier-simple/manifests lambda/kubectl-applier-simple-build/
pip3 install kubernetes pyyaml urllib3 -t lambda/kubectl-applier-simple-build/
```

The GCO CLI handles this automatically during `gco stacks deploy`.
