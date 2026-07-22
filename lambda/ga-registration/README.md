# GA Registration

Converges the regional Gateway API ALB after the Gateway manifests are applied: registers the ALB created for `gco-system/gco-gateway` with AWS Global Accelerator (when configured) and always publishes the ALB hostname to SSM Parameter Store for cross-region discovery.

## Table of Contents

- [Trigger](#trigger)
- [How It Works](#how-it-works)
- [CloudFormation Properties](#cloudformation-properties)
- [IAM Permissions](#iam-permissions)

## Trigger

Step Functions convergence task (`Action` events) and CloudFormation Custom Resource — runs on stack Create, Update, and Delete.

## How It Works

### Create/Update

1. Waits for the AWS Load Balancer Controller to provision an active internal ALB for the `gco-system/gco-gateway` Gateway (up to 14 minutes)
2. Resolves the ALB from the Gateway status address; while the status is still empty, falls back to the exact `gco.aws/gateway` + `elbv2.k8s.aws/cluster` ownership tag pair (cluster-only matches, NLBs, and internet-facing load balancers are rejected)
3. When `EndpointGroupArn` is configured, registers only that ALB with Global Accelerator, removes stale endpoint attachments, and enforces the HTTPS health-check contract
4. Always stores the ALB hostname in SSM at `/{ProjectName}/alb-hostname-{Region}`

### Delete

1. Removes all endpoints from the GA endpoint group (when configured)
2. Removes the ALB hostname from SSM

The ALB itself is deleted by the AWS Load Balancer Controller when the runtime teardown removes the Gateway resources.

## Input

Step Functions task event (`Action`, task properties) or CloudFormation Custom Resource event (RequestType, ResourceProperties).

## Output

CloudFormation response with `AlbArn` and `AlbHostname` on success.

## CloudFormation Properties

| Property | Required | Description |
|----------|----------|-------------|
| `ClusterName` | Yes | EKS cluster name |
| `Region` | Yes | AWS region for this cluster |
| `EndpointGroupArn` | No | Global Accelerator endpoint group ARN; omitted in partitions without Global Accelerator |
| `RegistryRegion` | No | Region for SSM endpoint-registry parameters (default: `us-east-2`; the legacy `GlobalRegion` alias remains accepted) |
| `ProjectName` | No | Project name for SSM paths (default: `gco`) |

## IAM Permissions

- `eks:DescribeCluster` on the EKS cluster
- `sts:GetCallerIdentity` (for EKS token generation)
- `elasticloadbalancing:DescribeLoadBalancers`, `elasticloadbalancing:DescribeTags`
- `globalaccelerator:DescribeEndpointGroup`, `globalaccelerator:AddEndpoints`, `globalaccelerator:RemoveEndpoints`, `globalaccelerator:UpdateEndpointGroup`
- `ssm:PutParameter`, `ssm:DeleteParameter` in the registry region
