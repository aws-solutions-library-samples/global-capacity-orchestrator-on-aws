# Helm Orchestrator

[CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) custom-resource provider that kicks off the Helm-install Step
Functions state machine. It does no Helm or Kubernetes work itself — that
happens in the per-chart state-machine tasks (the `helm-installer` [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html)). The
orchestrator only **starts** an execution and immediately reports the resource
created back to CloudFormation; it never waits for the charts to finish.

This is deliberately **fire-and-forget**. The cluster's CloudFormation lifecycle
is decoupled from the helm batch entirely: a slow chart (e.g. volcano retrying
docker.io image pulls) would otherwise keep the execution `RUNNING` past
CloudFormation's ~1-hour custom-resource ceiling, at which point CloudFormation
declares "did not receive a response" and rolls back — destroying the
freshly-created [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) cluster over a recoverable add-on problem. Charts converge in
the background; per-chart status is written to [SSM](https://docs.aws.amazon.com/systems-manager/latest/userguide/what-is-systems-manager.html) (`/<project>/addons/<region>/<chart>`)
by the installer tasks and surfaced via `gco stacks addons status`, and a
degraded add-on layer is re-converged with `gco stacks addons install`.

Keeping the heavy lifting in [Step Functions](https://docs.aws.amazon.com/step-functions/latest/dg/welcome.html) (one task per chart, with per-chart
retry) means no single Lambda invocation is bound by the 15-minute Lambda
limit — the reliability win over the old single-Lambda installer.

## Table of Contents

- [Trigger](#trigger)
- [How It Works](#how-it-works)
- [Packaging](#packaging)
- [CloudFormation Properties](#cloudformation-properties)
- [Environment Variables](#environment-variables)
- [IAM Permissions](#iam-permissions)
- [Dependencies](#dependencies)

## Trigger

CloudFormation Custom Resource via a [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) `cr.Provider` configured with only an
`onEvent` handler (no `isComplete` waiter). Runs on stack Create, Update, and
Delete.

## How It Works

### `on_event`

- **Create/Update**: starts a state-machine execution whose input carries the
  chart configuration (`ClusterName`, `Region`, `EnabledCharts`, `Charts`,
  `KedaOperatorRoleArn`) and returns immediately. Returns a stable
  `PhysicalResourceId` and exposes the started `ExecutionArn` as a resource
  attribute (`Data.ExecutionArn`) purely for observability — it does not gate
  resource completion.
- **Delete**: no-op. Charts are torn down with the cluster.

There is no `isComplete` handler: the resource is considered created as soon as
the execution has been started, so a slow or failing chart can never block (or
roll back) the cluster.

## Packaging

Plain (zip) Lambda — pure boto3, no Helm/kubectl binaries. The actual Helm work
runs in the `helm-installer` container Lambda invoked by the state machine.

## CloudFormation Properties

Passed through verbatim as the state-machine execution input.

| Property | Required | Description |
|----------|----------|-------------|
| `ClusterName` | Yes | EKS cluster name |
| `Region` | Yes | AWS region |
| `EnabledCharts` | No | List of chart names to install |
| `Charts` | No | Dict of per-chart config overrides |
| `KedaOperatorRoleArn` | No | [IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction.html) role ARN for KEDA [IRSA](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html) |
| `ProjectName` | No | Project prefix used for the SSM replay parameter |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `STATE_MACHINE_ARN` | Yes | ARN of the Helm-install state machine to execute |

## IAM Permissions

- `states:StartExecution` on the Helm-install state machine (`on_event`)
- `ssm:PutParameter` on `/<project>/addons/*` (best-effort execution-input replay)

## Dependencies

- `boto3` (AWS Lambda Python runtime built-in)
