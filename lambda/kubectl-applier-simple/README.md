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

The handler's `_SUPPORTED_MANIFEST_KINDS` is the authoritative list:

- **Core and apps:** Namespace, ServiceAccount, ConfigMap, Secret, Service, Pod, Deployment, StatefulSet, DaemonSet, Job, CronJob, HorizontalPodAutoscaler (`autoscaling/v2`), PodDisruptionBudget, PriorityClass, ResourceQuota, LimitRange, Lease, StorageClass, PersistentVolume, PersistentVolumeClaim, NetworkPolicy, APIService, CustomResourceDefinition, DeviceClass
- **RBAC:** ClusterRole, ClusterRoleBinding, Role, RoleBinding
- **Gateway API and AWS Load Balancer Controller:** GatewayClass, Gateway, HTTPRoute, LoadBalancerConfiguration, TargetGroupConfiguration
- **Karpenter / EKS Auto Mode:** NodePool, EC2NodeClass
- **cert-manager:** ClusterIssuer (cluster-scoped), Issuer, Certificate — GCO's internal CA chain and its TLS leaves
- **Admission policy:** ValidatingAdmissionPolicy, ValidatingAdmissionPolicyBinding (both cluster-scoped) — the issuance fence on the internal CA (`08-internal-ca-issuance.yaml`)
- **Prometheus Operator:** ServiceMonitor, PodMonitor
- **KEDA:** ScaledJob, ScaledObject
- **Kueue:** ClusterQueue, LocalQueue, ResourceFlavor
- **Kubeflow Trainer:** ClusterTrainingRuntime
- **Argo CD and Crossplane:** AppProject, Application, Function

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

Requires a build step to package dependencies into `kubectl-applier-simple-build/`.
`gco stacks deploy` runs it automatically (`StackManager._build_kubectl_lambda`
in `cli/stacks.py`). By hand, install from the pinned `requirements.txt` for the
Lambda platform rather than your laptop's:

```bash
rm -rf lambda/kubectl-applier-simple-build
mkdir -p lambda/kubectl-applier-simple-build
cp lambda/kubectl-applier-simple/handler.py lambda/kubectl-applier-simple/requirements.txt lambda/kubectl-applier-simple-build/
cp -r lambda/kubectl-applier-simple/manifests lambda/kubectl-applier-simple-build/
python3 -m pip install -r lambda/kubectl-applier-simple/requirements.txt \
  -t lambda/kubectl-applier-simple-build/ --upgrade \
  --platform manylinux2014_x86_64 --only-binary=:all:
```
