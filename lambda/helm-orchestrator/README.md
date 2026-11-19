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
- **Delete**: no-op for this resource. Helm releases are uninstalled by the
  separate, synchronous `HelmTeardownProvider`
  (`lambda/helm-installer/teardown_provider.py`), which waits on a
  reverse-order teardown state machine so releases that own webhooks and
  load balancers are gone before the cluster is.

There is no `isComplete` handler: the resource is considered created as soon as
the execution has been started, so a slow or failing chart can never block (or
roll back) the cluster.

### Replay input

Before it starts the execution, `on_event` stores the exact execution input in
SSM so `gco stacks addons install` can replay it, and records the started
execution and the input's SHA-256 in `/<project>/addons/<region>/_execution`.
The input is canonical JSON, at most 256 KiB (Step Functions' execution-input
limit, checked before anything is written). The stored copy is that JSON
compressed with zlib and base64-encoded, because SSM refuses a value that
contains `{{`:

- When the encoding fits one parameter value (8 KiB), it is the value of
  `/<project>/addons/<region>/_input`, the only form earlier releases wrote.
- When it does not, it is split into 8 KiB parts written to
  `_input.<sha256[:16]>.<k>` (k = 1..n), and `_input` then holds the header
  `chunked:v1:<n>:<sha256>`, where the SHA-256 is that of the whole encoded
  value. The parts are written before the header, so a deployment that fails
  part-way leaves `_input` naming parts that are still complete. Once the
  execution is recorded, the parts of the previous header that the new value
  does not use are deleted; a failure to read or delete them is only logged,
  and `gco stacks destroy-all` removes every parameter under
  `/<project>/addons/<region>`.

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
| `Charts` | No | Per-chart config overrides as one JSON string, decoded into the execution input (an object is accepted too). CloudFormation turns every number and boolean in an object property into a string, which chart values rendered with `toYaml` (a Secret volume's `optional` and `defaultMode`, a sidecar's ports and probes) cannot survive |
| `KedaOperatorRoleArn` | No | [IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction.html) role ARN for KEDA [IRSA](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html) |
| `ProjectName` | No | Project prefix used for the SSM replay parameter |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `STATE_MACHINE_ARN` | Yes | ARN of the Helm-install state machine to execute |

## IAM Permissions

- `states:StartExecution` on the Helm-install state machine (`on_event`)
- `ssm:PutParameter`, `ssm:GetParameter` and `ssm:DeleteParameter` on
  `/<project>/addons/*` (the replay input and its parts, the execution record,
  the teardown fence)

## Dependencies

- `boto3` (AWS Lambda Python runtime built-in)
