# Helm Installer

Installs and manages Helm charts on [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) clusters during [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) deployment. Supports KEDA, Volcano, KubeRay, Kueue, and more.

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

Create/update convergence and delete teardown use separate [CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) custom resources:

- Create/update starts the base-manifest → Helm → post-Helm state machine and returns immediately.
- Delete starts a dedicated reverse-order Helm teardown state machine and waits for it to finish before EKS access entries or the cluster are removed.

## How It Works

### Create/Update

1. Applies base Kubernetes manifests.
2. Loads default chart configs from `charts.yaml` and merges property overrides.
3. Runs one `helm upgrade --install` task per chart.
4. Applies post-Helm (CRD-dependent) manifests and registers the regional ALB.

### Delete

1. Lists and stops every visible create/update convergence execution, then repeats that cancellation on each 15-second completion poll to catch executions omitted by [Step Functions](https://docs.aws.amazon.com/step-functions/latest/dg/welcome.html)' eventually consistent listing.
2. Always waits 16 minutes so any [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) invocation already in flight reaches its hard 15-minute limit.
3. Scales `gco-system/health-monitor` to zero and waits for all replicas to terminate, preventing endpoint-registry recreation during teardown.
4. Uninstalls charts in reverse install order while the EKS API and Helm installer access entry still exist. Each serialized uninstall has a two-minute Helm deadline and 150-second subprocess cap so all supported releases fit within CloudFormation's one-hour custom-resource ceiling.
5. Only after synchronous teardown succeeds does CloudFormation deregister the [ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html) from [Global Accelerator](https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html), delete its [SSM](https://docs.aws.amazon.com/systems-manager/latest/userguide/what-is-systems-manager.html) registry entry, and remove the convergence trigger/EKS access.

Helm's explicit `release: not found` result is idempotent success; generic `not found` output is not. Every other uninstall error is returned as a CloudFormation failure so teardown cannot silently leave live releases or external resources behind. Transient failures can be retried by retrying stack deletion after the underlying Kubernetes condition is resolved.

## Packaging

Runs as a container Lambda (see `Dockerfile`). The image includes `helm` and `kubectl` binaries on x86_64.

## Charts (from `charts.yaml`)

| Chart | Namespace | Default |
|-------|-----------|---------|
| KEDA | `keda` | Enabled |
| AWS [EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html) Device Plugin | `kube-system` | Enabled |
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
| `KedaOperatorRoleArn` | No | [IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction.html) role ARN for KEDA [IRSA](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html) |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CLUSTER_NAME` | Yes | EKS cluster name |
| `REGION` | Yes | AWS region |
| `TEARDOWN_STATE_MACHINE_ARN` | Teardown provider only | Ordered uninstall state machine polled during stack deletion |
| `INSTALL_STATE_MACHINE_ARN` | Teardown provider only | Background convergence state machine whose running executions are stopped and drained before teardown |

## IAM Permissions

- `eks:DescribeCluster` on the EKS cluster
- `sts:GetCallerIdentity` (for EKS token generation)
- Kubernetes RBAC: cluster-admin or equivalent for Helm operations
- `states:StartExecution`/`states:DescribeExecution` on the teardown state machine
- `states:ListExecutions` on the one regional install state machine
- `states:StopExecution`/`states:DescribeExecution` on executions belonging to that install state machine

## Dependencies

- `boto3`, `pyyaml`, `urllib3` (see `requirements.txt`)
- Helm v4.2.4, kubectl v1.36.4 (installed in Docker image)
