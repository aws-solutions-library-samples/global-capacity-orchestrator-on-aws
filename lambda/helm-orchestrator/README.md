# Helm Orchestrator

CloudFormation custom-resource provider that drives the Helm-install Step
Functions state machine. It does no Helm or Kubernetes work itself — that
happens in the per-chart state-machine tasks (the `helm-installer` Lambda). The
orchestrator only starts an execution and reports completion back to
CloudFormation.

Keeping the heavy lifting in Step Functions (one task per chart, with per-chart
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

CloudFormation Custom Resource via a CDK async `cr.Provider` — this Lambda
supplies both the provider's `onEvent` and `isComplete` handlers. Runs on stack
Create, Update, and Delete.

## How It Works

### `on_event`

- **Create/Update**: starts a state-machine execution whose input carries the
  chart configuration (`ClusterName`, `Region`, `EnabledCharts`, `Charts`,
  `KedaOperatorRoleArn`). Returns a stable `PhysicalResourceId` and stashes the
  started `ExecutionArn` so the framework passes it through to `is_complete`.
- **Delete**: no-op. Charts are torn down with the cluster.

### `is_complete`

- **Create/Update**: polls the started execution via `describe_execution`.
  Reports complete on `SUCCEEDED`; raises (failing the resource) on `FAILED`,
  `TIMED_OUT`, or `ABORTED`; returns "not complete" while `RUNNING` so the
  provider framework keeps polling.
- **Delete**: returns complete immediately.

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
| `KedaOperatorRoleArn` | No | IAM role ARN for KEDA IRSA |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `STATE_MACHINE_ARN` | Yes | ARN of the Helm-install state machine to execute |

## IAM Permissions

- `states:StartExecution` on the Helm-install state machine (`on_event`)
- `states:DescribeExecution` on its executions (`is_complete`)

## Dependencies

- `boto3` (AWS Lambda Python runtime built-in)
